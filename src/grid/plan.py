"""The Plan — the command the thinking bot hands to the trading bot.

This is the contract between the two halves. The analyst produces a Plan, I
approve or reject it, and only an APPROVED plan is something the executor will
act on. The executor never reasons: it reads a Plan and does exactly what it says.

Keeping this a plain serialisable object matters for three reasons: I can write
one by hand to test the executor without involving any AI; I can read a plan and
see precisely what the bot will do before it does it; and the executor can rebuild
its state after a crash from the plan plus the exchange, never from local memory.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .fees import FeeModel

#: Instrument-id shapes on OKX:
#:     SPOT / MARGIN   BTC-USDT                    2 segments
#:     SWAP            BTC-USDT-SWAP               3, ends -SWAP
#:     FUTURES         BTC-USD-260626              3, dated
#:     OPTION          BTC-USD-260626-50000-C      5
#: Spot is the only 2-segment shape, so requiring exactly two segments rejects
#: every derivative without needing a list of suffixes to keep up to date.
SPOT_SEGMENTS = 2

#: How the rungs are spaced.
#:
#:   "geometric"  each level is a fixed PERCENTAGE above the last:
#:                price[i] = lower * (1 + s)**i.  Every gap is worth the same
#:                fraction, so `spacing_pct == min_spacing_pct` by construction
#:                and no part of the ladder is quietly less viable than the
#:                rest. This is the default.
#:
#:   "arithmetic" each level is a fixed PRICE above the last. Simpler to read,
#:                and wrong in a specific way: evenly spaced in price means the
#:                percentage gap SHRINKS going up. A 100-200 ladder with 150
#:                levels is 0.67% apart at the bottom and 0.33% at the top, so
#:                the upper half hands roughly twice as much of its gross to the
#:                exchange as the lower half. Kept as an option because the
#:                difference is worth being able to measure, not because it is
#:                a reasonable default.
GEOMETRIC = "geometric"
ARITHMETIC = "arithmetic"
SPACING_MODES = (GEOMETRIC, ARITHMETIC)


def spot_instrument_problems(symbol: str) -> list[str]:
    """Reject anything that is not an OKX SPOT instrument id.

    README rule 1: spot only, no margin, no futures. Funding on perpetuals runs
    around ±0.01% every 8 hours — roughly 11% annualised — which is many times
    the entire edge a grid earns from spacing, and it is charged whether or not
    the grid cycles. A grid on a perpetual bleeds continuously.

    IMPORTANT LIMIT OF THIS CHECK. OKX margin uses the SAME instrument id as
    spot: "BTC-USDT" is both. Margin is selected per ORDER, by `tdMode`, not by
    the id. So this function cannot rule out margin, and nothing that inspects
    an id ever could. The live client must send tdMode="cash" on every order;
    until it exists, that requirement is recorded here and in `LiveExchange`.
    """
    s = (symbol or "").strip().upper()
    if not s:
        return ["symbol is required"]

    parts = s.split("-")
    if len(parts) != SPOT_SEGMENTS or not all(parts):
        kind = ("a perpetual swap" if parts[-1] == "SWAP"
                else "an option" if len(parts) == 5
                else "a dated future" if len(parts) == 3 and parts[-1].isdigit()
                else "not a spot pair")
        return [f"{symbol!r} is {kind} — this system is SPOT ONLY. Funding on "
                f"derivatives runs about 11% a year, which is several times the "
                f"edge a grid earns, and it is charged whether the grid cycles "
                f"or not. A spot instrument id has exactly two segments, "
                f"e.g. BTC-USDT."]

    bad = [x for x in parts if not x.isalnum()]
    if bad:
        return [f"{symbol!r} is not a valid instrument id "
                f"(unexpected characters in {bad})"]
    return []


class Status(str, Enum):
    PROPOSED = "proposed"     # analyst produced it, awaiting my decision
    APPROVED = "approved"     # I said yes; executor may start
    REJECTED = "rejected"     # I said no; never runs
    RUNNING = "running"       # executor has live orders
    STOPPED = "stopped"       # I stopped it; orders cancelled
    STOPPED_OUT = "stopped_out"   # price broke the range; auto-liquidated
    COMPLETED = "completed"   # ran to its end date


@dataclass
class GridPlan:
    """One grid on one instrument. Every field the executor needs, and no more."""

    symbol: str                      # OKX instrument id, e.g. "BTC-USDT"
    lower: float                     # bottom of the range, in quote currency
    upper: float                     # top of the range
    levels: int                      # number of grid lines between them
    quote_per_level: float           # how much quote currency to spend per buy

    # Risk. Not optional — a grid without a stop is the strategy that eventually
    # loses everything it made, because it buys all the way down a breakout.
    stop_below: float                # hard exit price; liquidate and halt
    max_quote_total: float           # cap on total capital this grid may deploy

    #: "geometric" (default) or "arithmetic" — see SPACING_MODES above.
    spacing_mode: str = GEOMETRIC

    # The fee rates this plan was BUILT under. Recorded because spacing, the
    # ATR floor and the viability of the whole grid are derived from them: a
    # plan sized for 0.08% maker is not the same plan on a 0.20% tier, and a
    # stored plan read back without its fees cannot be judged at all.
    maker_fee: float = 0.0
    taker_fee: float = 0.0
    fee_source: str = ""

    #: Graded evidence score, 0-1. UNCALIBRATED until `confidence.calibration`
    #: says otherwise — recorded on every plan precisely so it can be scored
    #: against outcomes later.
    confidence: float = 0.0
    confidence_components: dict[str, float] = field(default_factory=dict)
    #: Full Kelly. Negative means negative expectation; see sizing.py.
    kelly_full: float = 0.0
    kelly_quarter: float = 0.0
    #: Which constraint decided the size, and what the alternatives were.
    size_binding: str = ""
    size_constraints: dict[str, float] = field(default_factory=dict)
    min_viable_capital: float = 0.0
    #: Q_max, in BASE units. Zero means "derive it" — see
    #: `inventory.max_inventory_for`. At this inventory the buy side stops
    #: being quoted entirely; see docs/THEORY.md.
    max_inventory: float = 0.0
    #: Modulate fill SIZE by inventory (the lattice equivalent of
    #: Avellaneda-Stoikov price skewing). OFF by default: it is a variance
    #: reduction that should be measured, not assumed.
    size_skew: bool = False

    #: "kelly" or "calibration". A calibration plan is sized to be runnable,
    #: not optimal, and says so on its face.
    sizing_mode: str = "kelly"
    #: What Kelly WOULD have said, recorded even when calibration sized it —
    #: including when Kelly says do not trade at all.
    kelly_would_say: str = ""

    # Provenance — why the analyst proposed this.
    thesis: str = ""
    metrics: dict[str, float] = field(default_factory=dict)
    backtest: dict[str, float] = field(default_factory=dict)

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc)
                            .isoformat(timespec="seconds"))
    status: Status = Status.PROPOSED
    decided_at: Optional[str] = None
    expires_at: Optional[str] = None

    # ---------------------------------------------------------------- derived

    @property
    def step(self) -> float:
        """Price gap between rungs. Only constant on an ARITHMETIC ladder.

        On a geometric ladder the gap grows with price, so a single number does
        not describe it; this returns the gap at the BOTTOM, which is the
        smallest one. Anything deciding viability wants `min_spacing_pct`.
        """
        if self.levels < 1:
            raise ValueError("levels must be >= 1")
        if self.is_geometric:
            return self.lower * self.ratio - self.lower
        return (self.upper - self.lower) / self.levels

    @property
    def is_geometric(self) -> bool:
        return self.spacing_mode != ARITHMETIC

    @property
    def ratio(self) -> float:
        """Multiplier between adjacent rungs on a geometric ladder.

        Chosen so the top rung lands exactly on `upper`:
        (upper/lower)**(1/levels).
        """
        if self.levels < 1:
            raise ValueError("levels must be >= 1")
        if self.lower <= 0 or self.upper <= 0:
            raise ValueError("prices must be positive for a geometric ladder")
        return (self.upper / self.lower) ** (1.0 / self.levels)

    @property
    def spacing_pct(self) -> float:
        """Spacing at the BOTTOM of the ladder — the most favourable number.

        On a GEOMETRIC ladder every gap is the same fraction, so this equals
        `min_spacing_pct` and the distinction stops mattering. On an ARITHMETIC
        one the gap shrinks as a percentage going up, and this is the widest
        (best) figure on the ladder — use `min_spacing_pct` for anything that
        decides whether the grid is viable.
        """
        if self.is_geometric:
            return self.ratio - 1.0
        return self.step / self.lower

    @property
    def min_spacing_pct(self) -> float:
        """Spacing at the NARROWEST point of the ladder — the worst case.

        This is what the fee gate must use. On a geometric ladder it is the
        same everywhere, which is the whole reason to prefer one.

        On an arithmetic ladder, measuring at the bottom reports the single most
        flattering figure: a 100-200 grid with 150 levels looks like 0.67%
        spacing (fees 24% of gross, passes) while the top of the same ladder is
        0.33% (fees 48%), and more than half the grid is then working for the
        exchange.
        """
        if self.is_geometric:
            return self.ratio - 1.0
        return self.step / (self.upper - self.step)

    @property
    def grid_prices(self) -> list[float]:
        """The price ladder, low to high. Buys rest below, sells above."""
        if self.is_geometric:
            r = self.ratio
            return [round(self.lower * r ** i, 10)
                    for i in range(self.levels + 1)]
        return [round(self.lower + i * self.step, 10) for i in range(self.levels + 1)]

    @property
    def max_exposure(self) -> float:
        """Worst case capital deployed: a buy resting on every ladder line.

        Counts `levels + 1` lines, not `levels` — the ladder has one more price
        than it has gaps, and understating exposure by a level is the wrong
        direction to be wrong in.
        """
        return min(len(self.grid_prices) * self.quote_per_level, self.max_quote_total)

    # ------------------------------------------------------------- validation

    def validate(self, fees: FeeModel | None = None,
                 min_fee_share: float = 0.30) -> list[str]:
        """Return a list of problems. An empty list means the plan is sane.

        The executor refuses to run a plan that fails this, so a bad proposal
        cannot reach the exchange even if I approve it by accident.
        """
        fees = fees or FeeModel()
        problems: list[str] = []

        # Rule 1 lives here rather than in the analyst so that it is re-checked
        # on every executor pass — a plan file edited to point at a perpetual
        # must stop the grid, not merely have been caught at approval.
        problems += spot_instrument_problems(self.symbol)

        if self.lower <= 0 or self.upper <= 0:
            problems.append("prices must be positive")
        if self.upper <= self.lower:
            problems.append(f"upper ({self.upper}) must exceed lower ({self.lower})")
        if self.levels < 2:
            problems.append("a grid needs at least 2 levels")
        if self.quote_per_level <= 0:
            problems.append("quote_per_level must be positive")
        if self.spacing_mode not in SPACING_MODES:
            problems.append(
                f"spacing_mode {self.spacing_mode!r} is not one of "
                f"{list(SPACING_MODES)}")

        if problems:
            return problems           # spacing maths would divide by nonsense

        # The fee constraint, measured at the NARROWEST point of the ladder. Gating
        # on the bottom spacing would pass grids whose upper half gives away half
        # its gross profit in fees.
        worst = self.min_spacing_pct
        share = fees.fee_share(worst)
        if share > min_fee_share:
            problems.append(
                f"narrowest spacing {worst:.3%} (at the top of the ladder) gives away "
                f"{share:.0%} of gross profit in fees (limit {min_fee_share:.0%}); "
                f"widen the range or use fewer levels")

        if self.stop_below <= 0:
            problems.append("stop_below is required — a grid without a stop is unbounded risk")
        elif self.stop_below >= self.lower:
            problems.append(
                f"stop_below ({self.stop_below}) must sit under lower ({self.lower})")
        elif self.stop_below < self.lower * 0.5:
            problems.append("stop_below is more than 50% under the range — that is not a stop")

        if self.max_quote_total < self.quote_per_level:
            problems.append("max_quote_total is smaller than a single level")
        elif self.max_quote_total < len(self.grid_prices) * self.quote_per_level:
            # Not fatal, but the grid will stop buying partway down and the plan
            # will not behave the way its numbers suggest.
            problems.append(
                f"max_quote_total {self.max_quote_total:g} cannot fund the whole "
                f"ladder ({len(self.grid_prices)} levels x {self.quote_per_level:g} "
                f"= {len(self.grid_prices) * self.quote_per_level:g}); the grid will "
                f"stop buying partway down")

        return problems

    # -------------------------------------------------------------- lifecycle

    def approve(self) -> "GridPlan":
        if self.status is not Status.PROPOSED:
            raise ValueError(f"only a proposed plan can be approved (is {self.status})")
        self.status = Status.APPROVED
        self.decided_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return self

    def reject(self) -> "GridPlan":
        if self.status is not Status.PROPOSED:
            raise ValueError(f"only a proposed plan can be rejected (is {self.status})")
        self.status = Status.REJECTED
        self.decided_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return self

    _TERMINAL = (Status.STOPPED, Status.STOPPED_OUT, Status.COMPLETED)

    def stop(self, reason: Status = Status.STOPPED) -> "GridPlan":
        """Halt a running grid. Terminal — a stopped plan is never restarted;
        the analyst must propose a fresh one, which I approve again.

        Guarded, because an unguarded stop() accepting any Status could revive a
        dead plan: `p.reject(); p.stop(Status.RUNNING)` would leave a plan I
        explicitly refused in a live state.
        """
        if reason not in self._TERMINAL:
            raise ValueError(
                f"stop() reason must be one of {[s.value for s in self._TERMINAL]}, "
                f"got {reason}")
        if self.status not in (Status.APPROVED, Status.RUNNING):
            raise ValueError(
                f"only an approved or running plan can be stopped (is {self.status})")
        self.status = reason
        return self

    @property
    def is_live(self) -> bool:
        return self.status in (Status.APPROVED, Status.RUNNING)

    # ------------------------------------------------------------ persistence

    def to_json(self) -> str:
        d = asdict(self)
        d["status"] = self.status.value
        return json.dumps(d, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "GridPlan":
        d = json.loads(text)
        d["status"] = Status(d.get("status", "proposed"))
        return cls(**d)

    def save(self, directory: str | Path = "data/plans") -> Path:
        p = Path(directory)
        p.mkdir(parents=True, exist_ok=True)
        path = p / f"{self.id}.json"
        path.write_text(self.to_json())
        return path

    def summary(self) -> str:
        """One-screen description, for the approval message on my phone."""
        return (
            f"{self.symbol}  {self.lower:g} – {self.upper:g}\n"
            f"{self.levels} levels · {self.spacing_pct:.2%} apart"
            f"{'' if self.is_geometric else ' (at the bottom)'}\n"
            f"{self.quote_per_level:g} per level · max {self.max_exposure:g}\n"
            f"stop {self.stop_below:g} "
            f"({(1 - self.stop_below / self.lower):.1%} below range)\n\n"
            f"{self.thesis}"
        )


def load_all(directory: str | Path = "data/plans") -> list[GridPlan]:
    p = Path(directory)
    if not p.exists():
        return []
    out = []
    for f in sorted(p.glob("*.json")):
        try:
            out.append(GridPlan.from_json(f.read_text()))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue          # a corrupt plan file must not stop the others running
    return out
