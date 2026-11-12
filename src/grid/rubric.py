"""The selection rubric — what the analyst checks before proposing a grid.

Two layers, deliberately separated:

  GATES   hard disqualifiers. Any single failure and the asset is not a candidate,
          regardless of how good everything else looks. No score can override a gate.

  SCORE   among assets that pass every gate, a 0-1 fit score for ranking.

The gates come first because grid trading's failure mode is not "mediocre returns",
it is ruin: a trending market makes a grid accumulate losing positions until the
capital is gone. Ranking cannot protect you from that — only refusing to trade can.

Thresholds below are sourced, not invented. Where the literature gives a number
(ADX 25, Hurst 0.45) it is used directly; where it gives only direction, the value
is chosen conservatively and marked as a judgement call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from . import metrics
from .fees import FeeModel


@dataclass(frozen=True)
class Thresholds:
    """Every tunable number in one place, so an experiment can vary them."""

    # --- statistical evidence ------------------------------------------------
    #: The PRIMARY gate. Everything below it is corroboration.
    adf_alpha: float = 0.05
    #: Bootstrap replications for the Hurst interval.
    hurst_boots: int = 1000
    #: Sub-window length for the stability check, and the share that must pass.
    stability_window: int = 150
    min_stability_pass: float = 0.70

    # --- the two windows -----------------------------------------------------
    #: CHARACTER is a slow property and needs a long window: a year, well past
    #: the ~1 month crypto momentum-reversal crossover, so a single regime
    #: cannot masquerade as a permanent trait.
    statistics_days: float = 365.0
    #: The BAND is a fast property and needs a short one. A grid trades where
    #: price is NOW. Deriving a band from a year of history is what produced
    #: the LTC 66-126 result — statistically impeccable, and untradeable.
    range_days: float = 45.0
    #: The short window must not CONTRADICT the long one. Power is fine at 45
    #: days of hourly bars, so a stark disagreement is evidence the character
    #: has changed, not evidence of a small sample.
    consistency_max_adf_p: float = 0.50

    # --- gates ---------------------------------------------------------------
    max_hurst: float = 0.45          # above this, momentum persists (sourced)
    max_adx: float = 25.0            # above this, market is trending (sourced)
    max_efficiency: float = 0.30     # above this, price is going somewhere (judgement)
    min_containment: float = 0.85    # band must actually contain recent price
    min_touches: int = 4             # an untested range is a guess
    #: Minimum ATR, as a multiple of the SPACING ACTUALLY USED. Left as None
    #: it is derived per assessment; set it to a number to pin it for an
    #: experiment.
    #:
    #: It used to be a hardcoded 0.008, which silently assumed the cheap OKX
    #: base fee tier: at 0.08% maker the minimum viable spacing is 0.64% and
    #: 0.008 is 1.25x that. On the EEA tier (0.20% maker) the spacing is 1.62%,
    #: so a 0.8% ATR asset cannot cross a level at all — and the old constant
    #: would have passed it anyway. The floor has to move with the fees.
    min_atr_pct: Optional[float] = None
    min_atr_spacing_multiple: float = 1.25
    max_atr_pct: float = 0.15        # beyond this it will blow through the range
    max_fee_share: float = 0.30      # fees may not take more than 30% of gross
    min_cycles_per_day: float = 0.5  # fewer than this and capital sits idle
    min_quote_volume_24h: float = 5_000_000.0   # liquidity floor, USDT

    # --- scoring weights (sum to 1.0) ---------------------------------------
    w_reversion: float = 0.30        # is it mean-reverting?
    w_choppiness: float = 0.25       # does it move without going anywhere?
    w_yield: float = 0.20            # how much does it earn per day?
    w_range_quality: float = 0.15    # is the band well-evidenced?
    w_liquidity: float = 0.10        # will the limit orders actually fill?


@dataclass
class Assessment:
    symbol: str
    passed: bool
    gate_failures: list[str] = field(default_factory=list)
    score: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    measured: dict[str, Optional[float]] = field(default_factory=dict)
    suggested_range: Optional[tuple[float, float]] = None
    #: The same band with recent bars weighted more heavily. When it disagrees
    #: with `suggested_range`, the range has moved.
    weighted_range: Optional[tuple[float, float]] = None
    #: How many candidates were evaluated in the scan this assessment belongs
    #: to. A p-value of 0.04 means something different when it is one of 30.
    n_candidates: int = 1
    #: How many bars each window actually used.
    n_bars_statistics: int = 0
    n_bars_range: int = 0
    #: Where the short window contradicts the long one.
    inconsistencies: list[str] = field(default_factory=list)
    #: What the Hurst gates WOULD have rejected, now that they are diagnostics
    #: rather than gates. Kept so the two can be compared on live scans.
    hurst_would_reject: list[str] = field(default_factory=list)
    #: The raw ADF p-value, kept so the scan can apply a Benjamini-Hochberg
    #: correction across the whole batch — which needs every p-value at once
    #: and so cannot be done here.
    adf_pvalue: Optional[float] = None
    notes: list[str] = field(default_factory=list)

    def explain(self) -> str:
        if not self.passed:
            return (f"{self.symbol}: REJECTED\n  " +
                    "\n  ".join(self.gate_failures))
        parts = ", ".join(f"{k} {v:.2f}" for k, v in self.components.items())
        return f"{self.symbol}: score {self.score:.2f}  ({parts})"


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def assess(
    symbol: str,
    closes: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    *,
    range_closes: Optional[Sequence[float]] = None,
    range_highs: Optional[Sequence[float]] = None,
    range_lows: Optional[Sequence[float]] = None,
    spacing: float,
    bars_per_day: float,
    quote_volume_24h: float = 0.0,
    lower: Optional[float] = None,
    upper: Optional[float] = None,
    thresholds: Optional[Thresholds] = None,
    fees: Optional[FeeModel] = None,
    n_candidates: int = 1,
) -> Assessment:
    """Run every gate, then score.

    `spacing` is the proposed grid gap as a fraction.
    `bars_per_day` MUST match the candle interval the prices came from (24 for
    hourly bars, 1 for daily). It has no default on purpose: ATR is measured per
    bar, and silently assuming one bar per day understates the cycle rate by 24x
    on hourly candles, which rejects every good intraday candidate.
    """
    t = thresholds or Thresholds()
    f = fees or FeeModel()
    a = Assessment(symbol=symbol, passed=True, n_candidates=n_candidates)

    # TWO WINDOWS, TWO QUESTIONS.
    #
    #   the LONG window  -> is this asset mean-reverting BY CHARACTER?
    #                       ADF, Hurst, ADX, efficiency ratio, stability.
    #   the SHORT window -> where is it trading NOW?
    #                       the band, containment, touches, ATR, the stop.
    #
    # Using one window for both is what produced a 66-126 band on LTC: a year
    # of history describes a year of price, and no grid can trade that.
    # Defaulting the short series to the long one keeps every existing caller
    # working, and such a caller simply gets the old single-window behaviour.
    r_closes = list(range_closes) if range_closes is not None else list(closes)
    r_highs = list(range_highs) if range_highs is not None else list(highs)
    r_lows = list(range_lows) if range_lows is not None else list(lows)
    a.n_bars_statistics = len(closes)
    a.n_bars_range = len(r_closes)

    if len(closes) < 120:
        a.passed = False
        a.gate_failures.append(
            f"only {len(closes)} bars — need 120+ for the statistics to mean anything")
        return a

    # --- measure -------------------------------------------------------------
    # The primary evidence: does the data reject a random walk at all?
    adf = metrics.adf_test(closes)
    a.adf_pvalue = adf.pvalue if adf else None

    # Hurst WITH an interval. The point estimate alone cannot distinguish
    # "reverting" from "we cannot tell".
    hci = metrics.hurst_ci(closes, n_boot=t.hurst_boots)

    # Did the property hold throughout, or only on average?
    stab = metrics.rolling_stability(
        closes, window=min(t.stability_window, max(60, len(closes) // 2)),
        metric_fn=metrics.hurst,
        passes=lambda v: v < t.max_hurst)

    h = metrics.hurst(closes)
    vr = metrics.variance_ratio(closes)
    er = metrics.efficiency_ratio(closes)
    ad = metrics.adx(highs, lows, closes)
    # ATR on the SHORT window. It decides whether price crosses a level often
    # enough to earn, and where the stop goes — both questions about the
    # current regime, not about the asset's character over a year.
    atr = metrics.atr_pct(r_highs, r_lows, r_closes)

    # `is not None`, not truthiness — a caller passing only `lower` must be an
    # error, not a silent fallback to the auto-derived band.
    band_supplied = lower is not None and upper is not None
    if (lower is None) != (upper is None):
        a.passed = False
        a.gate_failures.append("pass both lower and upper, or neither")
        return a
    if band_supplied and upper <= lower:
        a.passed = False
        a.gate_failures.append(f"upper ({upper}) must exceed lower ({lower})")
        return a

    unweighted, weighted = metrics.suggest_range_both(r_closes)
    a.weighted_range = weighted
    band = (lower, upper) if band_supplied else unweighted
    if band is None:
        a.passed = False
        a.gate_failures.append("could not derive a range from the price history")
        return a
    lo, hi = band
    a.suggested_range = (lo, hi)

    # The ATR floor tracks the spacing, which tracks the fee tier.
    min_atr = (t.min_atr_pct if t.min_atr_pct is not None
               else t.min_atr_spacing_multiple * spacing)

    # Containment and touches ask "does price live in this band", which is a
    # question about the recent window the band was drawn from.
    containment = metrics.range_containment(r_closes, lo, hi)
    touches = metrics.range_touches(r_closes, lo, hi)
    cycles = metrics.expected_cycles_per_day(atr or 0.0, spacing, bars_per_day)
    fee_share = f.fee_share(spacing) if spacing > 0 else 1.0

    drift = 0.0
    if weighted and band:
        mid_w = (weighted[0] + weighted[1]) / 2.0
        mid_u = (band[0] + band[1]) / 2.0
        drift = (mid_w / mid_u - 1.0) if mid_u else 0.0

    a.measured = {"adf_stat": adf.statistic if adf else None,
                  "adf_p": adf.pvalue if adf else None,
                  "hurst_ci_low": hci.low if hci else None,
                  "hurst_ci_high": hci.high if hci else None,
                  "hurst_ci_width": hci.width if hci else None,
                  "stability_mean": stab.mean if stab else None,
                  "stability_sd": stab.stdev if stab else None,
                  "stability_pass": stab.pass_fraction if stab else None,
                  "stability_windows": float(stab.n_windows) if stab else None,
                  "range_drift": drift,
                  "hurst_would_reject": float(len(a.hurst_would_reject)),
                  "n_candidates": float(n_candidates),
                  "hurst": h, "variance_ratio": vr, "efficiency_ratio": er,
                  "adx": ad, "atr_pct": atr, "min_atr_pct": min_atr,
                  "maker_fee": f.maker, "taker_fee": f.taker,
                  "containment": containment,
                  "touches": float(touches), "cycles_per_day": cycles,
                  "fee_share": fee_share, "quote_volume_24h": quote_volume_24h}

    # --- consistency: does the recent window agree with the long one? -------
    #
    # The long window says what this asset IS. The short window says what it is
    # doing now. When they disagree, the long window is describing a character
    # the asset has stopped having — and a grid is placed in the present, not
    # in the average of the last year.
    #
    # Only STARK disagreements count. 45 days of hourly bars is over a thousand
    # observations, so a clear trend there is a real change rather than a small
    # sample, but a marginal reading is still just noise and is not gated on.
    if range_closes is not None and len(r_closes) >= 120:
        s_adx = metrics.adx(r_highs, r_lows, r_closes)
        s_er = metrics.efficiency_ratio(r_closes)
        s_adf = metrics.adf_test(r_closes)

        a.measured["short_adx"] = s_adx
        a.measured["short_efficiency_ratio"] = s_er
        a.measured["short_adf_p"] = s_adf.pvalue if s_adf else None

        # NOTE: short_adx is RECORDED but NOT gated on, and that is a
        # measured fact rather than an oversight. ADX is Wilder-smoothed with
        # a decay of (13/14) per bar, so after ~1000 bars it has forgotten
        # everything older entirely: on LTC, ETH and XRP the 8760-bar and
        # 1080-bar readings agree to the last digit of a double (49.185266602
        # on LTC). A "does the short window disagree" test on ADX therefore
        # fires exactly when the long-window ADX gate has already fired, and
        # adding it would make this check look stronger than it is. The long
        # ADX gate below is untouched and still rejects trending assets.
        #
        # ADF and the efficiency ratio ARE window-sensitive (LTC: p 0.324 long
        # vs 0.738 short; ETH efficiency 0.019 vs 0.086), so they carry the
        # check.
        if s_er is not None and s_er > t.max_efficiency:
            a.inconsistencies.append(
                f"the last {len(r_closes)} bars show an efficiency ratio of "
                f"{s_er:.3f} (> {t.max_efficiency}) — price is travelling now, "
                f"not oscillating")
        if s_adf is not None and s_adf.pvalue >= t.consistency_max_adf_p:
            a.inconsistencies.append(
                f"ADF on the last {len(r_closes)} bars gives p={s_adf.pvalue:.3f} "
                f"— no evidence of reversion in the window the band is drawn "
                f"from, whatever the long window says")

    # --- gates ---------------------------------------------------------------
    # A missing statistic is a failure, not a pass. We do not trade on absent data.
    #
    # PRIMARY: the Augmented Dickey-Fuller test. This is the only gate here
    # that tests a hypothesis rather than compares a descriptive number to a
    # threshold, and it asks exactly the question the strategy depends on —
    # is there a level this thing returns to, or is it a random walk? The
    # Hurst, ADX and efficiency gates below corroborate it. They are not four
    # independent votes: all four are computed from the same few hundred
    # numbers and move together, so treating them as independent evidence
    # overstates the case by a lot.
    if adf is None:
        a.gate_failures.append(
            "ADF test could not be computed — no evidence against a random walk")
    elif adf.pvalue >= t.adf_alpha:
        a.gate_failures.append(
            f"ADF p={adf.pvalue:.3f} (statistic {adf.statistic:.2f}) does not "
            f"reject a random walk at {t.adf_alpha:.0%} — there is no "
            f"statistical evidence this asset reverts to a level, whatever the "
            f"descriptive numbers say")

    # HURST IS A DIAGNOSTIC, NOT A GATE.
    #
    # It was a gate and it rejected 24 of 28 candidates in a live scan, which
    # made it the binding constraint on the whole system. It is also the
    # least-supported statistic in this framework: the estimate is acutely
    # sensitive to the lag range (Lo 1991 makes the same point about R/S and
    # short-range dependence), the same series can read 0.43 or 0.67 depending
    # on choices nobody has justified, and no forward-predictive validation of
    # it exists here or, as far as I can establish, anywhere.
    #
    # ADF is the primary gate instead: it tests a hypothesis, reports an error
    # rate, and is what "mean-reverting" actually means. Hurst is still
    # measured, still reported with its interval, and what it WOULD have
    # rejected is recorded in `hurst_would_reject` so the two can be compared
    # on real scans rather than argued about.
    if hci is None:
        a.hurst_would_reject.append("Hurst interval could not be bootstrapped")
    elif not hci.certainly_below(t.max_hurst):
        a.hurst_would_reject.append(
            f"Hurst {hci.point:.3f} but its 95% interval is "
            f"[{hci.low:.3f}, {hci.high:.3f}], which reaches above the "
            f"{t.max_hurst} gate")

    # Did it hold THROUGHOUT?
    if stab is None:
        a.gate_failures.append("stability could not be measured")
    elif not stab.is_stable(t.min_stability_pass):
        a.gate_failures.append(
            f"only {stab.pass_fraction:.0%} of {stab.n_windows} sub-windows are "
            f"mean-reverting (need {t.min_stability_pass:.0%}); Hurst averages "
            f"{stab.mean:.3f} but swings by {stab.stdev:.3f} across the window "
            f"— the average is describing a regime, not a property")

    if h is None:
        a.hurst_would_reject.append("Hurst could not be computed")
    elif h > t.max_hurst:
        a.hurst_would_reject.append(
            f"Hurst {h:.3f} > {t.max_hurst} — this asset trends, and a grid in a "
            f"trend accumulates losses until the capital is gone")

    if ad is None:
        a.gate_failures.append("ADX could not be computed")
    elif ad > t.max_adx:
        a.gate_failures.append(f"ADX {ad:.1f} > {t.max_adx} — directional trend present")

    if er is None:
        a.gate_failures.append("efficiency ratio could not be computed")
    elif er > t.max_efficiency:
        a.gate_failures.append(
            f"efficiency ratio {er:.3f} > {t.max_efficiency} — price is travelling, "
            f"not oscillating")

    # Variance ratio is deliberately NOT a gate — it is horizon-dependent, and a
    # genuine oscillation reads as trending when measured inside its own cycle.
    # It informs the notes instead. See metrics.variance_ratio for the numbers.

    if atr is None:
        a.gate_failures.append("ATR could not be computed")
    elif atr < min_atr:
        a.gate_failures.append(
            f"ATR {atr:.2%} too small — needs {min_atr:.2%} to cross a "
            f"{spacing:.2%} grid level often enough to earn anything "
            f"({t.min_atr_spacing_multiple:g}x the spacing, which is set by "
            f"the {f.maker:.3%} maker fee)")
    elif atr > t.max_atr_pct:
        a.gate_failures.append(
            f"ATR {atr:.2%} too large — it will break the range rather than oscillate in it")

    # Only meaningful for a band the caller chose. `suggest_range` trims to a fixed
    # percentile, so an auto-derived band contains ~90% BY CONSTRUCTION and the gate
    # can never fire — it passed 300 out of 300 pure exponential uptrends. For auto
    # bands the trend discrimination is done by Hurst, ADX, efficiency ratio and the
    # touch count, all of which do reject a trend.
    if band_supplied and containment < t.min_containment:
        a.gate_failures.append(
            f"band holds only {containment:.0%} of recent bars (need {t.min_containment:.0%}) "
            f"— this is a line through a trend, not a range")

    if touches < t.min_touches:
        a.gate_failures.append(
            f"range tested only {touches} times (need {t.min_touches}) — untested boundary")

    if fee_share > t.max_fee_share:
        a.gate_failures.append(
            f"fees take {fee_share:.0%} of gross at {spacing:.2%} spacing "
            f"(limit {t.max_fee_share:.0%}) — widen the spacing")

    if cycles < t.min_cycles_per_day:
        a.gate_failures.append(
            f"~{cycles:.2f} cycles/day expected — capital would sit idle")

    if quote_volume_24h < t.min_quote_volume_24h:
        a.gate_failures.append(
            f"24h volume {quote_volume_24h:,.0f} below {t.min_quote_volume_24h:,.0f} "
            f"— limit orders may not fill and the spread will eat the edge")

    # The Hurst diagnostic is attached BEFORE the early return, so a rejected
    # candidate still carries it. Comparing what ADF rejected against what
    # Hurst would have rejected is the whole point of keeping it.
    for why in a.hurst_would_reject:
        a.notes.append(f"Hurst diagnostic (NOT a gate): {why}")

    if a.gate_failures:
        a.passed = False
        return a

    # --- score (only reached by assets that passed every gate) ---------------
    # Each component is normalised to 0-1 where 1 is ideal for a grid.
    # Denominators are guarded: setting a threshold to zero is the natural way to
    # disable a gate for a backtest, and it used to raise ZeroDivisionError here.
    reversion = _clamp01((t.max_hurst - h) / t.max_hurst) if t.max_hurst > 0 else 1.0
    choppiness = _clamp01(1.0 - er / t.max_efficiency) if t.max_efficiency > 0 else 1.0
    net_per_cycle = f.cycle_profit_pct(spacing)
    daily_yield = cycles * net_per_cycle
    yield_score = _clamp01(daily_yield / 0.01)                     # 1%/day caps the scale
    range_quality = _clamp01(0.5 * containment +
                             0.5 * min(1.0, touches / 10.0))
    liquidity = (_clamp01(quote_volume_24h / (t.min_quote_volume_24h * 10))
                 if t.min_quote_volume_24h > 0 else 1.0)

    a.components = {"reversion": reversion, "choppiness": choppiness,
                    "yield": yield_score, "range_quality": range_quality,
                    "liquidity": liquidity}
    a.score = (t.w_reversion * reversion + t.w_choppiness * choppiness +
               t.w_yield * yield_score + t.w_range_quality * range_quality +
               t.w_liquidity * liquidity)

    # The Hurst gate already rejected anything above max_hurst (0.45), so a
    # "transitional band" note here could only ever fire on the exact boundary.
    # What is still worth flagging is a reading close to the gate, and a variance
    # ratio that points the other way.
    if hci is not None and hci.width > 0.20:
        a.notes.append(
            f"the Hurst interval is {hci.width:.2f} wide — the reading passes "
            f"but is imprecise")
    if stab is not None and stab.stdev > 0.10:
        a.notes.append(
            f"Hurst varies by {stab.stdev:.3f} across sub-windows — the "
            f"property is intermittent even though enough windows pass")
    if abs(drift) > 0.05:
        a.notes.append(
            f"the recency-weighted band sits {drift:+.1%} from the unweighted "
            f"one — the range has been moving, so it may not hold")
    if h > t.max_hurst * 0.9:
        a.notes.append(f"Hurst {h:.3f} is close to the {t.max_hurst} gate — marginal")
    if vr is not None and vr > 1.0:
        a.notes.append(
            f"variance ratio {vr:.2f} > 1 disagrees with the Hurst reading — the "
            f"reversion horizon may not match the measurement horizon")
    a.notes.append(f"expected ~{daily_yield:.3%}/day gross of slippage, before any breakout")

    # Window disagreements are appended LAST, on purpose. They are real gate
    # failures and they reject, but they are not the HEADLINE reason: callers
    # show `gate_failures[0]`, and a pair that fails the primary ADF gate on
    # the long window AND disagrees across windows should report the ADF
    # failure. Listing the disagreement first made the consistency check look
    # like the thing doing the rejecting when the primary evidence gate had
    # already rejected on its own — measured on live BTC, ETH, LTC and DOT,
    # all four fail both.
    for why in a.inconsistencies:
        a.gate_failures.append(f"windows disagree: {why}")

    return a


def rank(assessments: Sequence[Assessment], top: int = 3) -> list[Assessment]:
    """Best candidates first. Rejected assets never appear, at any score."""
    return sorted([a for a in assessments if a.passed],
                  key=lambda a: a.score, reverse=True)[:top]


# DOCUMENTATION ONLY. This was the system prompt for a model call in analyst.py.
# That call is gone — TradeBunny has no AI at runtime — and nothing sends this
# anywhere. It is kept because it is the clearest single statement of what the
# gates below are for and why each one exists. If you change a threshold in
# `Thresholds`, change the matching sentence here too.
ANALYST_BRIEF = """\
You are selecting instruments for a SPOT grid trading bot. You are not predicting
direction — a grid does not care which way price goes, only that it oscillates.

THE CORE IDEA. A grid earns from path length and is harmed by net displacement.
The ideal asset moves a great deal and ends up where it started. This means
"volatile" is NOT the selection criterion people assume: a volatile asset in a
trend is the worst possible case, because the grid buys all the way down and
accumulates losses until the capital is exhausted. You want high movement AND low
directionality, which are separate properties.

HARD GATES — any single failure disqualifies, regardless of everything else:
  - Hurst exponent must be < 0.45. Above 0.55 the asset trends; 0.45-0.55 is
    transitional and not tradeable.
  - ADX must be < 25. Above that a directional trend is present.
  - Efficiency ratio must be < 0.30. Higher means price is travelling, not oscillating.
  - The proposed band must contain at least 85% of recent bars, and must have been
    touched and rejected at least 4 times. An untested range is a guess.
  - ATR must be large enough to cross a grid level regularly, and small enough not
    to break the range.
  - Fees must take no more than 30% of gross profit per cycle at the chosen spacing.
  - Liquidity must be sufficient for resting limit orders to fill.

RANGE SETTING. Derive the band from where the asset has actually traded, never from
the return you would like to earn. Setting a range because the projected profit looks
attractive is the most common way a grid is mis-specified.

THE STOP IS NOT OPTIONAL. Every proposal states the price at which the range thesis
is invalidated. Without it a range trade silently becomes a forced long-term holding.
A 20% drawdown takes roughly 31 winning cycles at 0.8% spacing to recover.

WHAT TO OUTPUT. For each candidate: the measured statistics, the proposed band and
why it is that band, the spacing and how much of the gross profit fees consume, the
stop and what would invalidate the thesis, and the expected cycles per day. If
nothing passes the gates, propose nothing — that is a correct and common answer.
"""
