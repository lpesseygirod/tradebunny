"""The daily scan. NO AI, at runtime or anywhere else in this package.

There used to be a model call here that wrote the approval note. It is gone.
Every number in a proposal came from `metrics.py` and `rubric.py` already; the
model was only ever rephrasing them, and a paragraph of fluent prose attached to
a trading decision invites you to read the prose instead of the numbers. The
explanation is now a template over the same measurements, which means it cannot
say anything the measurements do not, cannot be unavailable, cannot cost money,
and cannot drift.

`rubric.ANALYST_BRIEF` survives as documentation — it is the clearest statement
of what the gates are for — but nothing sends it anywhere.

    the MEASUREMENTS decide       the TEMPLATE says
    -----------------------       -----------------
    which instrument              what the numbers mean, in words
    the range
    the spacing and level count
    the stop
    the position size

An empty result is a correct and common outcome. Nothing here manufactures a
candidate to fill a quota — the caps are ceilings, never targets. Every
rejection is logged with the gate that caused it, so a quiet day can be told
apart from a broken scraper.
"""

from __future__ import annotations

import time as _time

import logging
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import confidence as conf
from . import execution, freshness, metrics, okx, rubric, sizing
from .backtest import WalkForwardResult, levels_for_spacing, walk_forward
from .control import Control
from .fees import FeeModel
from .plan import GEOMETRIC, GridPlan, spot_instrument_problems
from .rubric import Assessment

log = logging.getLogger(__name__)

#: Fraction of available capital risked on one grid, if the stop is hit.
RISK_PER_TRADE = 0.02

#: The stop sits below the range by whichever is larger: three ATRs, or a fifth
#: of the range width.
#:
#: Two terms because either alone fails. Three ATRs alone puts the stop inside
#: ordinary noise on a quiet asset, and the grid is liquidated by a wobble it
#: was built to trade. A fixed fraction of the range alone ignores how violent
#: the asset actually is. Taking the maximum means the binding constraint is
#: whichever risk is real for that instrument.
STOP_ATR_MULTIPLE = 3.0
STOP_RANGE_FRACTION = 0.20

#: Smallest per-level order worth placing, in quote currency. Below this the
#: exchange minimum bites and a "valid" plan cannot actually execute.
MIN_QUOTE_PER_LEVEL = 10.0

#: Fallback fee rates, used when the account's real tier cannot be read.
#:
#: These are the EEA-regulated rates — 2.5x the global base tier — and they are
#: deliberately the PESSIMISTIC end. An unavailable fee rate is an absence of
#: information, and the safe reading of an absence is the expensive one: if we
#: guessed the cheap tier and were wrong, every gate downstream would pass
#: grids whose spacing cannot pay for the trading. Guessing expensive only ever
#: rejects candidates that a later, successful fetch would restore.
FALLBACK_MAKER = 0.0020
FALLBACK_TAKER = 0.0035

#: How long a fetched fee tier is trusted. Tiers move with 30-day volume, so a
#: day is generous; the point of expiring at all is that a tier which got worse
#: must not go unnoticed for the life of a process.
FEE_TTL_SECONDS = 86_400

#: Reference instrument for the fee lookup. OKX spot fees are set by the
#: account's VIP tier, not per pair, so one lookup covers the whole scan rather
#: than one authenticated call per symbol.
FEE_REFERENCE_INSTRUMENT = "BTC-USDT"

#: Where the permissivity setting lives. Read at scan time, not import time,
#: so editing config.yaml takes effect without a restart.
CONFIG_PATH = Path(os.getenv("TRADEBUNNY_HOME", ".")).resolve() / "config.yaml"


def sizing_mode(path: Optional[Path] = None) -> str:
    """"kelly" (default) or "calibration". Falls back to kelly on anything
    unclear — a malformed config must not silently switch how money is sized."""
    try:
        import yaml
        raw = yaml.safe_load(Path(path or CONFIG_PATH).read_text()) or {}
        mode = str((raw.get("grid") or {}).get("mode", sizing.KELLY_MODE))
        return mode if mode in sizing.SIZING_MODES else sizing.KELLY_MODE
    except Exception:
        return sizing.KELLY_MODE


def permissivity(path: Optional[Path] = None) -> str:
    """The configured permissivity level, or 'strict' if anything is unclear.

    Fails to the STRICTEST setting. A missing or malformed config must not
    quietly widen what gets shown.
    """
    try:
        import yaml
        raw = yaml.safe_load(Path(path or CONFIG_PATH).read_text()) or {}
        level = str((raw.get("grid") or {}).get("permissivity", conf.DEFAULT_LEVEL))
        conf.threshold_for(level)          # raises if unknown
        return level
    except Exception:
        return conf.DEFAULT_LEVEL


#: (fetched_at, FeeModel, source) per instrument, for this process only.
_FEE_CACHE: dict[str, tuple[float, FeeModel, str]] = {}


def clear_fee_cache() -> None:
    """Forget the cached tier. For tests and for `cli check`."""
    _FEE_CACHE.clear()


def live_fees(inst_id: str = FEE_REFERENCE_INSTRUMENT, *, source: Any = okx,
              now: Optional[float] = None) -> tuple[FeeModel, str]:
    """The account's real fee model, or the pessimistic fallback.

    Returns (model, source) where `source` is a short string recorded on every
    plan and every backtest, so a result can never be read without knowing what
    it assumed. Cached for `FEE_TTL_SECONDS` per process.

    Never raises. A fee lookup that fails must not stop a scan — it must make
    the scan stricter.
    """
    now = time.time() if now is None else now
    hit = _FEE_CACHE.get(inst_id)
    if hit and (now - hit[0]) < FEE_TTL_SECONDS:
        return hit[1], hit[2]

    try:
        got = source.get_trade_fee(inst_id)
        model = FeeModel(maker=got["maker"], taker=got["taker"])
        label = (f"okx:{got.get('level') or 'live'} "
                 f"maker {got['maker']:.4%} taker {got['taker']:.4%}")
        log.info("fee tier for %s: %s", inst_id, label)
    except Exception as e:
        model = FeeModel(maker=FALLBACK_MAKER, taker=FALLBACK_TAKER)
        label = (f"fallback:EEA maker {FALLBACK_MAKER:.4%} "
                 f"taker {FALLBACK_TAKER:.4%}")
        log.warning("could not read the live fee tier (%s: %s) — falling back "
                    "to the PESSIMISTIC EEA rates. Grids will be held to wider "
                    "spacing than they might need, which is the safe direction.",
                    type(e).__name__, e)

    _FEE_CACHE[inst_id] = (now, model, label)
    return model, label

#: OKX's page size for `/market/candles`, and nothing more than that. It is
#: the FALLBACK depth, used only when the data source cannot serve deep
#: history. 300 hourly bars is 12 days, which is well inside the ~1 month
#: crossover where crypto momentum turns into reversal — judging an asset's
#: character on it would be measuring the current swing, not the asset.
CANDLES = 300

#: THE STATISTICS WINDOW. Long. What the asset IS: ADF, the Hurst diagnostic,
#: ADX, efficiency ratio, rolling stability, the HAR volatility forecast. These
#: are slow properties and a short window cannot see them.
STATISTICS_DAYS = 365.0

#: THE RANGE WINDOW. Short. Where price is NOW: `suggest_range`, containment,
#: touches, ATR. A grid trades the band it is in today, and a year-long window
#: answers "what band?" with the union of every regime in the year — on LTC
#: that is 66-126, which no ladder can trade.
RANGE_DAYS = 45.0

MAX_PROPOSALS = 3


def _now_ms() -> float:
    return _time.time() * 1000.0

#: HARD GATE. Candles whose newest bar closed longer ago than this cannot
#: produce a proposal. Stale data does not look stale — it produces a
#: confident-looking answer about a market that has moved on.
MAX_DATA_AGE = freshness.DEFAULT_MAX_DATA_AGE


@dataclass
class Candidate:
    """Every pair the scan looked at, scored, whatever happened to it.

    "Zero proposals" is not one outcome. A best score of 0.48 that missed the
    threshold and a best of 0.05 that failed every gate are different worlds,
    and until this existed they printed the same way: silence.
    """

    symbol: str
    confidence: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    stage: str = ""                  # which gate rejected it, "" if proposed
    reason: str = ""
    missed_by: Optional[float] = None    # how far under the threshold
    # The STATISTICS window — the long one.
    n_bars: int = 0
    first_bar: str = ""
    last_bar: str = ""
    # The RANGE window — the short one the band was actually drawn from.
    n_bars_range: int = 0
    range_from: str = ""
    range_to: str = ""
    proposed: bool = False

    def windows(self) -> str:
        """Both windows on one line. Printed on every proposal and rejection."""
        stats = f"{self.n_bars} bars"
        if self.first_bar and self.last_bar:
            stats += f" {self.first_bar[:10]}..{self.last_bar[:10]}"
        rng = f"{self.n_bars_range} bars"
        if self.range_from and self.range_to:
            rng += f" {self.range_from[:10]}..{self.range_to[:10]}"
        return f"stats {stats} | range {rng}"

    def describe(self, floor: float) -> str:
        if self.proposed:
            verdict = "PROPOSED"
        elif self.stage:
            verdict = f"rejected: {self.stage}"
        else:
            verdict = "below threshold"
        miss = (f"  (missed by {self.missed_by:.2f})"
                if self.missed_by is not None and self.missed_by > 0 else "")
        head = f"  {self.symbol:14} {self.confidence:5.2f}  {verdict}{miss}"
        lines = [head]
        if self.reason:
            lines.append(f"      {self.reason[:150]}")
        if self.n_bars:
            lines.append(f"      {self.windows()}")
        return "\n".join(lines)


@dataclass(frozen=True)
class Rejection:
    """Why one symbol did not become a proposal."""

    symbol: str
    stage: str                       # fetch | gate | walk_forward | sizing | validate
    reasons: tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.symbol}: [{self.stage}] " + "; ".join(self.reasons)


@dataclass
class ScanReport:
    """Everything the scan looked at and what happened to it.

    Pass one into `scan(report=...)` to get the rejections back structurally as
    well as in the log. This is the answer to "why was today quiet?", and it
    matters that the answer is *recorded* rather than reconstructed: an empty
    day and a day where every fetch 403'd look identical from the outside.
    """

    considered: int = 0
    fetched: int = 0
    #: Set when the scan aborted outright rather than merely finding nothing.
    #: An empty day and a broken data source must never look alike.
    error: str = ""
    #: How many survivors were dropped by the Benjamini-Hochberg correction.
    n_corrected: int = 0
    #: Ages of every input at scan time, for the freshness panel.
    freshness: Optional[Any] = None
    #: EVERY candidate, scored, including the ones a gate rejected.
    candidates: list[Candidate] = field(default_factory=list)
    #: The data window actually used, so a result can never be read without
    #: knowing what it measured.
    window_bars: int = 0
    window_from: str = ""
    window_to: str = ""
    #: The RANGE window, reported separately — it is the one the band came from.
    range_window_bars: int = 0
    range_window_from: str = ""
    range_window_to: str = ""
    #: Which sizing mode was in force.
    mode: str = ""
    #: Fills recorded so far, and whether that is enough to leave calibration.
    fills_recorded: int = 0
    #: Permissivity level in force, and the candidates that scored below it.
    #: Recorded so a quiet day can be read as "here is what I am NOT showing
    #: you and why" rather than as silence.
    level: str = ""
    below_threshold: list[tuple[str, float]] = field(default_factory=list)
    #: Which fee rates this scan used, and whether they were live or the
    #: pessimistic fallback. Recorded because every gate depends on them.
    fee_source: str = ""
    rejections: list[Rejection] = field(default_factory=list)
    accepted: list[str] = field(default_factory=list)
    halted: bool = False
    thesis_failures: int = 0

    def add(self, symbol: str, stage: str, *reasons: str) -> None:
        r = Rejection(symbol, stage, tuple(reasons))
        self.rejections.append(r)
        log.info("rejected %s", r)

    def candidate(self, symbol: str) -> Candidate:
        for c in self.candidates:
            if c.symbol == symbol:
                return c
        c = Candidate(symbol=symbol)
        self.candidates.append(c)
        return c

    def best_score(self) -> float:
        return max((c.confidence for c in self.candidates), default=0.0)

    def show_all(self, floor: float = 0.65) -> str:
        """Every pair, its score, and what stopped it."""
        if not self.candidates:
            return "no candidates evaluated"
        rows = sorted(self.candidates, key=lambda c: -c.confidence)
        lines = [f"{len(rows)} candidate(s), threshold {floor:.2f}, "
                 f"best score {self.best_score():.2f}"]
        if self.window_bars:
            lines.append(f"statistics window: {self.window_bars} bars, "
                         f"{self.window_from} to {self.window_to}")
        if self.range_window_bars:
            lines.append(f"range window:      {self.range_window_bars} bars, "
                         f"{self.range_window_from} to {self.range_window_to}")
        lines.append("")
        lines += [c.describe(floor) for c in rows]
        return "\n".join(lines)

    def by_stage(self, stage: str) -> list[Rejection]:
        return [r for r in self.rejections if r.stage == stage]

    def explain(self) -> str:
        lines = [f"scanned {self.considered}, fetched {self.fetched}, "
                 f"proposed {len(self.accepted)}"]
        if self.fee_source:
            lines.append(f"  fees: {self.fee_source}")
        if self.level:
            lines.append(f"  permissivity: {self.level} "
                         f"(>= {conf.threshold_for(self.level):.2f})")
        for sym, sc in self.below_threshold:
            lines.append(f"  below threshold: {sym} confidence {sc:.2f}")
        if self.n_corrected:
            lines.append(f"  {self.n_corrected} dropped by multiple-testing "
                         f"correction over {self.considered} symbols")
        if self.halted:
            lines.append("  analyst is HALTED — nothing was scanned")
        if self.error:
            lines.append(f"  ABORTED: {self.error}")
        for r in self.rejections:
            lines.append(f"  {r}")
        if self.thesis_failures:
            lines.append(f"  {self.thesis_failures} plan(s) returned without a thesis")
        return "\n".join(lines)


def plan_prices_top(lower: float, upper: float, levels: int,
                    spacing_mode: str) -> float:
    """Top rung of the ladder — where a fixed quote amount buys fewest units,
    and therefore where the minimum-size constraint bites first."""
    return upper


def round_down_to_round_number(x: float) -> float:
    """Largest 'tidy' number <= x — two significant figures.

        1234.5 -> 1200      345.6 -> 340      87.3 -> 87      9.74 -> 9.7

    Always rounds DOWN. Rounding a position size up, even to something neat,
    spends capital the risk calculation did not allow for.
    """
    if x <= 0 or not math.isfinite(x):
        return 0.0
    step = 10.0 ** (math.floor(math.log10(x)) - 1)
    return math.floor(x / step) * step


def stop_price(lower: float, upper: float, atr_pct: Optional[float]) -> float:
    """Where the stop goes, in price.

        stop_below = lower - max(3 * ATR_in_price, 0.20 * (upper - lower))

    ATR is measured as a fraction of price, so it is converted at `lower` —
    the stop sits at the bottom of the ladder, and that is the price the
    distance should be measured against.

    A missing ATR falls back to the range-width term alone rather than to a
    guess: an unmeasurable ATR is not evidence of a calm asset.
    """
    if upper <= lower:
        raise ValueError(f"upper ({upper}) must exceed lower ({lower})")
    atr_price = (atr_pct or 0.0) * lower
    distance = max(STOP_ATR_MULTIPLE * atr_price,
                   STOP_RANGE_FRACTION * (upper - lower))
    return lower - distance


def stop_distance_fraction(lower: float, upper: float,
                           atr_pct: Optional[float]) -> float:
    """The same stop, expressed as a fraction of `lower`.

    `walk_forward` takes `stop_pct`, so the fitted plan and the proposed plan
    have to describe the same stop in the two different units they each want.
    """
    return (lower - stop_price(lower, upper, atr_pct)) / lower


def size_grid(available_quote: float, upper: float, stop_below: float,
              max_total_quote: float, max_concurrent_grids: int,
              risk_per_trade: float = RISK_PER_TRADE) -> float:
    """Capital for one grid, sized by what a stop-out ACTUALLY costs.

        avg_entry     = (upper + stop_below) / 2
        loss_fraction = (avg_entry - stop_below) / avg_entry
        quote         = available * risk_per_trade / loss_fraction

    The previous version measured the stop distance from `lower` and treated
    the grid as one position opened there. That is not how a grid dies. It
    accumulates on the way down — buying at every level it passes — so when the
    stop fires it is holding its LARGEST position at its WORST average price,
    with lots stretching from the top of the range down to the stop. Measuring
    from `lower` understated the real loss by roughly 60%, which means the
    "2% risk" was closer to 5%.

    Approximating the average entry as the midpoint of (upper, stop_below) is
    the honest simplification: lots are spread roughly evenly across that span
    by the time the stop is hit. It is not exact, but it errs toward assuming
    more exposure rather than less.

    Conviction does not enter into it — there is no term here for how good the
    candidate looked, deliberately, because that is the term that turns a bad
    day into a large bad day.

    Then two ceilings, both from `control.json`: the absolute cap, and an equal
    share of what is available across the maximum number of concurrent grids —
    so a first proposal cannot consume the budget the next two would need.
    """
    if stop_below <= 0 or upper <= stop_below:
        raise ValueError(
            f"stop_below ({stop_below}) must be positive and below upper "
            f"({upper}) — without that the risk calculation divides by zero "
            f"or flips sign")

    avg_entry = (upper + stop_below) / 2.0
    loss_fraction = (avg_entry - stop_below) / avg_entry
    if loss_fraction <= 0:
        raise ValueError("loss fraction must be positive")

    quote = available_quote * risk_per_trade / loss_fraction
    quote = min(quote, max_total_quote)
    if max_concurrent_grids > 0:
        quote = min(quote, available_quote / max_concurrent_grids)
    return round_down_to_round_number(max(0.0, quote))


# ------------------------------------------------------------------ the words

def _label(value: Optional[float], bands: list[tuple[float, str]],
           fallback: str = "unmeasured") -> str:
    """First label whose ceiling `value` is under."""
    if value is None:
        return fallback
    for ceiling, word in bands:
        if value < ceiling:
            return word
    return bands[-1][1]


def describe(a: Assessment, plan: GridPlan, wf: WalkForwardResult, *,
             spacing: float, n_bars: int, n_bars_range: Optional[int] = None,
             fees: Optional[FeeModel] = None) -> str:
    """The approval note, rendered from the measurements. No model involved.

    Every clause here is a direct restatement of a number that a gate already
    tested. That is the point: the note cannot claim anything the measurements
    do not support, because there is nothing in it that is not a measurement.
    """
    m = a.measured
    hurst, adx, er = m.get("hurst"), m.get("adx"), m.get("efficiency_ratio")
    atr, touches = m.get("atr_pct"), m.get("touches")
    containment = m.get("containment")
    cycles = m.get("cycles_per_day") or 0.0
    fee_share = m.get("fee_share") or 0.0

    reversion = _label(hurst, [(0.40, "strongly mean-reverting"),
                               (0.45, "mean-reverting"),
                               (0.55, "borderline"), (1.01, "trending")])
    trend = _label(adx, [(15.0, "no trend"), (25.0, "a weak trend"),
                         (1e9, "a clear trend")])
    travel = _label(er, [(0.10, "almost none of its movement"),
                         (0.20, "little of its movement"),
                         (0.30, "some of its movement"),
                         (1.01, "most of its movement")])
    vigour = _label(atr, [(0.01, "quiet"), (0.03, "active"),
                          (0.08, "volatile"), (1.01, "very volatile")])

    # Computed from the SAME spacing the rubric scored, not from
    # `plan.spacing_pct`. The two differ — the candidate spacing is the fee
    # floor, the plan's is the ladder's actual bottom gap — and quoting one
    # here while `a.notes` quotes the other put two different daily yields in
    # the same paragraph.
    fees = fees or FeeModel()
    daily_yield = cycles * fees.cycle_profit_pct(spacing) if spacing > 0 else 0.0
    stop_gap = (plan.lower - plan.stop_below) / plan.lower if plan.lower else 0.0
    range_width = ((plan.upper - plan.lower) / plan.lower) if plan.lower else 0.0

    lines = []
    if plan.sizing_mode == sizing.CALIBRATION_MODE:
        lines.append(sizing.CALIBRATION_LABEL)
        lines.append(f"For comparison: {plan.kelly_would_say}")
    lines += [
        f"{plan.symbol} has traded between {plan.lower:.6g} and {plan.upper:.6g} "
        f"— a band {range_width:.1%} wide that held "
        f"{(containment or 0):.0%} of the last {n_bars_range or n_bars} "
        f"bars and was tested at its edges {int(touches or 0)} times.",

        (f"An augmented Dickey-Fuller test rejects the random-walk null at "
         f"p={m['adf_p']:.4f} — this is the primary evidence, and it holds "
         f"after correcting for the {int(m.get('n_candidates') or 1)} symbols "
         f"scanned. Hurst sits in [{m['hurst_ci_low']:.3f}, "
         f"{m['hurst_ci_high']:.3f}] with 95% confidence, entirely below the "
         f"0.45 gate, and {m['stability_pass']:.0%} of sub-windows are "
         f"mean-reverting individually rather than only on average."
         if m.get('adf_p') is not None and m.get('hurst_ci_low') is not None
         and m.get('stability_pass') is not None else
         "Statistical evidence unavailable; see the recorded metrics."),

        f"It is {vigour} ({atr:.2%} average true range per bar) but goes nowhere: "
        f"Hurst {hurst:.3f} reads as {reversion}, ADX {adx:.1f} shows {trend}, "
        f"and an efficiency ratio of {er:.3f} means {travel} is in one direction. "
        f"That combination — movement without displacement — is what a grid is for."
        if None not in (atr, hurst, adx, er) else
        "Some statistics could not be measured; see the recorded metrics.",

        f"The ladder is {plan.levels} levels {plan.min_spacing_pct:.2%} apart at "
        f"its narrowest, where fees take {fee_share:.0%} of gross profit per "
        f"cycle. At roughly {cycles:.2f} cycles a day that is about "
        f"{daily_yield:.2%} a day gross of slippage, if the range holds.",

        f"Fees assumed: {plan.maker_fee:.3%} maker, {plan.taker_fee:.3%} taker "
        f"({plan.fee_source or 'unrecorded'}). Every number above depends on "
        f"them.",

        # Both windows, on every proposal. The statistics and the band are
        # measured over deliberately different stretches of history, and a
        # reader who assumes one window is reading the band as a year-long
        # claim when it is a six-week one.
        (f"Measured over two windows: the CHARACTER above (ADF, Hurst, ADX, "
         f"efficiency) from {n_bars} bars"
         + (f" {wf.statistics_from[:10]} to {wf.statistics_to[:10]}"
            if wf.statistics_from else "")
         + f", the BAND and its ATR from the most recent "
           f"{n_bars_range or n_bars} bars"
         + (f" {wf.range_from[:10]} to {wf.range_to[:10]}"
            if wf.range_from else "")
         + ". The first says whether this asset is the kind of thing a grid "
           "works on; the second says where to put it."),

        f"The stop is {plan.stop_below:.6g}, {stop_gap:.1%} below the bottom of "
        f"the range. Below that the range thesis is wrong and the grid "
        f"liquidates rather than keeps buying. Committing "
        f"{plan.max_quote_total:.6g} across {len(plan.grid_prices)} levels, "
        f"sized so a stop-out costs about {RISK_PER_TRADE:.0%} of the capital "
        f"you made available.",

        f"Against it: fitted on the first 60% of the window this returned "
        f"{wf.in_sample.total_return_pct:+.2%} over {wf.in_sample.n_cycles} "
        f"cycles, and on the untouched last 40% "
        f"{wf.out_of_sample.total_return_pct:+.2%} over "
        f"{wf.out_of_sample.n_cycles} cycles — "
        f"{wf.retention:.0%} of the fitted return survived. "
        f"A grid earns nothing if price leaves the band and stays out, and the "
        f"worst case is a slow break downward, which costs the stop.",
    ]
    if a.notes:
        lines.append("Notes: " + "; ".join(a.notes) + ".")
    return "\n\n".join(lines)


# ------------------------------------------------------------------- the scan

def scan(universe: list[str], available_quote: float, bar: str = "1H", *,
         control: Optional[Control] = None,
         client: Any = None,
         fees: Optional[FeeModel] = None,
         thresholds: Optional[rubric.Thresholds] = None,
         spacing_mode: str = GEOMETRIC,
         max_data_age: float = MAX_DATA_AGE,
         statistics_days: float = STATISTICS_DAYS,
         range_days: float = RANGE_DAYS,
         source: Any = okx,
         report: Optional[ScanReport] = None) -> list[GridPlan]:
    """Today's grid proposals, or an empty list.

        >>> for p in scan(["BTC-USDT", "ETH-USDT"], available_quote=500.0):
        ...     print(p.summary())

    Every returned plan is PROPOSED and needs your approval before anything
    happens. Nothing here places an order.

    TWO WINDOWS. `statistics_days` (long) decides whether the asset is the
    KIND of thing a grid works on. `range_days` (short) decides WHERE the band
    goes. They are deliberately different lengths and both are printed on every
    proposal and every rejection — see `Candidate.windows`.

    `source` is the market-data module (defaults to `grid.okx`), injected so
    the tests can run the real logic with no network. `client` is accepted and
    ignored — it used to be the Anthropic client and is kept only so existing
    callers do not break. `report` collects the structured rejections.
    """
    # The account's REAL fee tier, not the constant. Everything downstream —
    # the candidate spacing, the ATR floor, the level count, the fee gate —
    # is derived from this, so reading it wrong is not a rounding error.
    # An explicit `fees` argument still wins, for tests and experiments.
    control = control or Control()
    report = report if report is not None else ScanReport()
    report.considered = len(universe)
    mode = sizing_mode()
    report.mode = mode
    exec_quality = execution.load()
    report.fills_recorded = exec_quality.n_fills

    if fees is None:
        fees, fee_source = live_fees(source=source)
    else:
        fee_source = f"explicit maker {fees.maker:.4%} taker {fees.taker:.4%}"
    report.fee_source = fee_source

    fr = freshness.FreshnessReport()
    for src in ("candles", "tickers", "instruments", "fees"):
        fr.add(src, source.fetched_at(src) if hasattr(source, "fetched_at")
               else None)
    report.freshness = fr

    # Fail closed, and before spending a single request. `may_propose` is the
    # analyst's own kill switch: a missing or corrupt control file halts too.
    allowed, why = control.may_propose()
    if not allowed:
        report.halted = True
        log.warning("analyst halted, proposing nothing: %s", why)
        return []

    if available_quote <= 0 or not universe:
        log.info("nothing to scan (available_quote=%s, universe=%d)",
                 available_quote, len(universe))
        return []

    state = control.state()
    bars_per_day = source.bars_per_day(bar)          # raises on an unknown interval
    spacing = fees.min_viable_spacing(fee_share_of_gross=0.25, maker=True)

    # One call for the whole universe rather than one per symbol.
    try:
        instruments = source.get_instruments()
    except Exception as e:
        report.error = (f"could not fetch instrument specifications, scan "
                        f"aborted: {type(e).__name__}: {e}")
        log.error("%s", report.error)
        return []

    try:
        tickers = source.get_spot_tickers()
    except Exception as e:
        report.error = (f"could not fetch tickers, scan aborted: "
                        f"{type(e).__name__}: {e}")
        log.error("%s", report.error)
        return []

    survivors: list[tuple[Assessment, GridPlan, WalkForwardResult]] = []

    for symbol in universe:
        # --- 0. spot only, before spending a request on it ----------------
        # `plan.validate()` would catch this at the end anyway, but a derivative
        # in the universe would otherwise be rejected by the LIQUIDITY gate —
        # `get_spot_tickers()` has no entry for it, so its volume reads as 0 —
        # and "24h volume 0" is a misleading reason for "this is a swap".
        not_spot = spot_instrument_problems(symbol)
        if not_spot:
            report.add(symbol, "universe", *not_spot)
            continue

        # --- 1. data ------------------------------------------------------
        # DEEP history for the statistics window. `get_candles` alone caps at
        # one OKX page — 300 bars, 12 days on 1H — which is far too short to
        # judge whether an asset mean-reverts. `get_history` pages back through
        # `/market/history-candles` and caches. A source without it (the test
        # fakes) falls back to a single page, and the windows below then
        # collapse to whatever it returned.
        try:
            if hasattr(source, "get_history"):
                candles = source.get_history(
                    symbol, bar,
                    start_ts=_now_ms() - statistics_days * 86_400_000.0)
            else:
                candles = source.get_candles(symbol, bar, limit=CANDLES)
        except Exception as e:
            report.add(symbol, "fetch", f"{type(e).__name__}: {e}")
            continue
        if len(candles) < 120:
            report.add(symbol, "fetch",
                       f"only {len(candles)} candles — need 120+ for the statistics")
            continue

        # --- 1b. is the data current? --------------------------------------
        # A HARD gate, before any statistic is computed. Measured from the
        # candle's own timestamp, not from when the fetch happened: a fetch a
        # second ago that returned yesterday's bar is stale data freshly
        # delivered.
        age = freshness.candle_age(candles, bar_ms=okx.bar_ms(bar))
        if age is None or age > max_data_age:
            report.add(symbol, "stale_data",
                       f"newest candle closed {freshness.human_age(age)}, over "
                       f"the {freshness.human_duration(max_data_age)} limit — "
                       f"refusing to build a proposal from data this old, "
                       f"because it would look exactly like a fresh one")
            continue

        # --- 1c. is the instrument tradeable at all? -----------------------
        instrument = instruments.get(symbol)
        if instrument is None:
            report.add(symbol, "instrument",
                       f"no instrument specification for {symbol} — it may have "
                       f"been delisted")
            continue
        state_problem = instrument.state_problem
        if state_problem:
            report.add(symbol, "instrument", state_problem)
            continue

        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]

        # THE SPLIT. Statistics get everything; the band gets only the recent
        # tail. `min` rather than an error, so a thinly-listed pair with three
        # weeks of history still gets assessed on what exists — the window is
        # reported either way, so a short one is visible rather than implied.
        range_bars = max(20, min(len(closes),
                                 int(round(range_days * bars_per_day))))
        r_closes, r_highs = closes[-range_bars:], highs[-range_bars:]
        r_lows = lows[-range_bars:]
        volume = tickers.get(symbol, {}).get("quote_volume_24h", 0.0)
        report.fetched += 1

        cand = report.candidate(symbol)
        cand.n_bars = len(candles)
        cand.first_bar = freshness.iso(candles[0]["ts"] / 1000.0)
        cand.last_bar = freshness.iso(candles[-1]["ts"] / 1000.0)
        cand.n_bars_range = range_bars
        cand.range_from = freshness.iso(candles[-range_bars]["ts"] / 1000.0)
        cand.range_to = cand.last_bar
        if len(candles) > report.window_bars:
            report.window_bars = len(candles)
            report.window_from, report.window_to = cand.first_bar, cand.last_bar
            report.range_window_bars = range_bars
            report.range_window_from = cand.range_from
            report.range_window_to = cand.range_to

        # --- 2. the gates -------------------------------------------------
        a = rubric.assess(symbol, closes, highs, lows,
                          range_closes=r_closes, range_highs=r_highs,
                          range_lows=r_lows, spacing=spacing,
                          bars_per_day=bars_per_day, quote_volume_24h=volume,
                          thresholds=thresholds, fees=fees,
                          n_candidates=len(universe))
        # Score it even though it failed. A gate rejection at 0.48 and one at
        # 0.05 are different worlds, and reporting both as "rejected" hides
        # which. The walk-forward component is necessarily absent here — it
        # was never run — so these scores are a lower bound.
        cand.components = dict(conf.compute(a.measured).components)
        cand.confidence = conf.compute(a.measured).score

        if not a.passed:
            cand.stage = "gate"
            cand.reason = a.gate_failures[0]
            report.add(symbol, "gate", *a.gate_failures)
            continue
        if a.suggested_range is None:                 # defensive; assess gates this
            report.add(symbol, "gate", "no range could be derived")
            continue

        lower, upper = a.suggested_range
        stop_below = stop_price(lower, upper, a.measured.get("atr_pct"))
        stop_distance = stop_distance_fraction(lower, upper,
                                               a.measured.get("atr_pct"))

        # --- 3. does it survive data it was not fitted on? ----------------
        try:
            wf = walk_forward(closes, highs, lows, symbol, split=0.6,
                              bars_per_day=bars_per_day, fees=fees,
                              stop_pct=stop_distance,
                              spacing_mode=spacing_mode,
                              range_days=range_days,
                              timestamps=[c["ts"] for c in candles])
        except ValueError as e:
            report.add(symbol, "walk_forward", str(e))
            continue
        if not wf.generalises:
            cand.stage = "walk_forward"
            cand.reason = wf.reason
            report.add(symbol, "walk_forward", wf.reason)
            continue

        # --- 4. grade the evidence -----------------------------------------
        # Structural gates already passed; this scores the EVIDENCE, which is
        # a matter of degree rather than pass/fail. See confidence.py.
        c = conf.compute(a.measured,
                         oos_return=wf.out_of_sample.total_return_pct,
                         in_sample_return=wf.in_sample.total_return_pct,
                         generalises=wf.generalises)
        cand.confidence = c.score
        cand.components = dict(c.components)

        # --- 5. size it -----------------------------------------------------
        levels = levels_for_spacing(lower, upper, spacing, spacing_mode)
        risk_size = size_grid(available_quote, upper, stop_below,
                              state.max_total_quote,
                              state.max_concurrent_grids)

        # Kelly: walk-forward out-of-sample return as the win, the modelled
        # stop-out as the loss, the confidence score as p. All three are
        # estimates, which is why only a quarter of the answer is acted on.
        avg_entry = (upper + stop_below) / 2.0
        loss_fraction = (avg_entry - stop_below) / avg_entry
        kelly_full = sizing.kelly_fraction(
            win_pct=max(wf.out_of_sample.total_return_pct, 0.0),
            loss_pct=loss_fraction, p=c.score)

        price_now = closes[-1]
        min_cap = 0.0
        if instrument is not None:
            probe = GridPlan(symbol=symbol, lower=lower, upper=upper,
                             levels=levels, spacing_mode=spacing_mode,
                             quote_per_level=1.0, stop_below=stop_below,
                             max_quote_total=1.0)
            min_cap = sizing.min_viable_capital(probe, price_now,
                                                instrument.min_size)

        rec = sizing.recommend(
            available_quote=available_quote, risk_size=risk_size,
            kelly_full=kelly_full, max_total_quote=state.max_total_quote,
            max_concurrent_grids=state.max_concurrent_grids,
            min_capital=min_cap, loss_fraction=loss_fraction)

        calib = None
        if mode == sizing.CALIBRATION_MODE and instrument is not None:
            # Feasibility, not growth. Kelly is still computed above and is
            # still shown on the proposal — this replaces the SIZE, not the
            # analysis.
            min_size_quote = instrument.min_size * max(plan_prices_top(
                lower, upper, levels, spacing_mode), price_now)
            calib = sizing.calibration_size(
                min(available_quote, state.max_total_quote), min_size_quote,
                levels)
            if not calib.feasible:
                cand.stage = "calibration"
                cand.reason = calib.reason
                report.add(symbol, "calibration", calib.reason)
                continue
            levels = calib.levels
            per_level = calib.quote_per_level
            quote = calib.total
        else:
            quote = rec.recommended
            per_level = quote / (levels + 1) if quote > 0 else 0.0

        # The MIN_QUOTE_PER_LEVEL floor is a Kelly-mode guard. Calibration mode
        # deliberately sizes at the exchange minimum, which is below that flat
        # floor by construction — it has its own feasibility check above.
        if calib is None and (quote <= 0 or per_level < MIN_QUOTE_PER_LEVEL):
            cand.stage = "sizing"
            cand.reason = ("; ".join(rec.warnings) if rec.warnings else
                           f"{quote:g} over {levels + 1} levels is "
                           f"{per_level:.2f} each, under the "
                           f"{MIN_QUOTE_PER_LEVEL:g} minimum order")
            report.add(symbol, "sizing", f"confidence {c.score:.2f}; "
                       + cand.reason)
            continue

        plan = GridPlan(
            symbol=symbol, lower=lower, upper=upper, levels=levels,
            spacing_mode=spacing_mode,
            quote_per_level=per_level, stop_below=stop_below,
            # Exact, so float drift cannot trip validate()'s funding check.
            max_quote_total=per_level * (levels + 1),
            metrics={k: float(v) for k, v in a.measured.items() if v is not None},
            backtest=wf.out_of_sample.to_metrics(),
            maker_fee=fees.maker, taker_fee=fees.taker, fee_source=fee_source,
            confidence=c.score, confidence_components=dict(c.components),
            kelly_full=rec.kelly_full, kelly_quarter=rec.kelly_quarter,
            size_binding=rec.binding,
            size_constraints={k: float(v) for k, v in rec.constraints.items()},
            min_viable_capital=rec.min_capital,
            sizing_mode=mode,
            kelly_would_say=(
                f"Kelly would deploy {rec.constraints.get('quarter Kelly', 0.0):,.2f} "
                f"(quarter Kelly {rec.kelly_quarter:+.3f} at risk, "
                f"{loss_fraction:.1%} loss fraction)"
                if not rec.negative_kelly else
                f"Kelly says DO NOT TRADE THIS — full Kelly "
                f"{rec.kelly_full:+.3f} is negative expectation"),
        )

        # --- 6a. the plan must pass its own validation --------------------
        problems = plan.validate(fees=fees)
        if problems:
            report.add(symbol, "validate", *problems)
            continue

        # --- 6b. the exchange must accept every order on the ladder --------
        # A HARD gate, not graded: an order under minSz is rejected outright,
        # so there is nothing here to be more or less confident about.
        order_problems = sizing.order_size_problems(plan, price_now, instrument)
        if order_problems:
            cand.stage = "min_order_size"
            cand.reason = order_problems[0]
            report.add(symbol, "min_order_size", *order_problems)
            continue

        a.measured["n_bars"] = float(len(closes))
        a.measured["confidence"] = c.score
        survivors.append((a, plan, wf))

    # --- 4b. multiple-testing correction --------------------------------
    # Each survivor cleared ADF at p<0.05 on its own. Across N symbols that is
    # not the same claim: testing 30 assets at 5% expects about 1.5 to clear by
    # luck, so on a quiet day the one candidate that "passed" is more likely to
    # be noise than signal. Benjamini-Hochberg controls the false DISCOVERY
    # rate over the whole batch, and it has to be applied here rather than in
    # `assess` because it needs every p-value at once.
    alpha = (thresholds or rubric.Thresholds()).adf_alpha
    tested = [x for x in survivors if x[0].adf_pvalue is not None]
    if tested:
        keep = metrics.benjamini_hochberg([x[0].adf_pvalue for x in tested], alpha)
        rejected = {id(x[0]) for x, k in zip(tested, keep) if not k}
        if rejected:
            for a, plan, wf in list(survivors):
                if id(a) in rejected:
                    report.add(
                        plan.symbol, "multiple_testing",
                        f"ADF p={a.adf_pvalue:.4f} clears {alpha:.0%} on its own "
                        f"but not after correcting for the {len(universe)} "
                        f"symbols tested in this scan — at that rate roughly "
                        f"{len(universe) * alpha:.1f} would pass by chance")
            survivors = [x for x in survivors if id(x[0]) not in rejected]
        report.n_corrected = len(rejected)

    if not survivors:
        log.info("no candidates survived; %d rejected", len(report.rejections))
        return []

    # --- 6c. the permissivity threshold -----------------------------------
    # A DISPLAY filter, applied last. Everything above it is a structural gate
    # or a measurement; nothing here relaxes either. What falls below the line
    # is logged with its score, so "why was today quiet" has a numeric answer.
    level = permissivity()
    report.level = level
    floor = conf.threshold_for(level)
    kept = []
    for a, plan, wf in survivors:
        if plan.confidence >= floor:
            kept.append((a, plan, wf))
        else:
            cand = report.candidate(plan.symbol)
            cand.missed_by = floor - plan.confidence
            cand.reason = (f"scored {plan.confidence:.2f}, "
                           f"{cand.missed_by:.2f} under the {level} "
                           f"threshold of {floor:.2f}")
            report.below_threshold.append((plan.symbol, plan.confidence))
            log.info("%s scored %.2f, below the %s threshold of %.2f",
                     plan.symbol, plan.confidence, level, floor)
    survivors = kept
    if not survivors:
        log.info("nothing cleared the %s threshold (%.2f)", level, floor)
        return []

    # --- 6d. rank, then cap -----------------------------------------------
    # Capped by the control file as well as by MAX_PROPOSALS: proposing more
    # grids than the executor is allowed to run concurrently would hand you
    # approvals that cannot all be acted on.
    limit = min(MAX_PROPOSALS, max(0, state.max_concurrent_grids))
    survivors.sort(key=lambda t: t[0].score, reverse=True)
    survivors = survivors[:limit]

    # --- 5. the explanation, last -----------------------------------------
    # A template over numbers that are already decided. It cannot fail, cannot
    # be unavailable, and cannot say anything the measurements do not.
    for a, plan, wf in survivors:
        plan.thesis = describe(a, plan, wf, spacing=spacing,
                               n_bars=int(a.measured.get("n_bars") or 0),
                               n_bars_range=a.n_bars_range or None,
                               fees=fees)

    plans = [p for _, p, _ in survivors]
    for p in plans:
        report.candidate(p.symbol).proposed = True
    report.accepted = [p.symbol for p in plans]
    log.info("proposing %d plan(s): %s", len(plans), ", ".join(report.accepted))
    return plans
