"""Has the thesis decayed since you approved it?

THE GAP THIS CLOSES
===================

Every gate in this system fires once, at proposal time, and then never again.
The executor enforces `stop_below` and nothing else. So a grid approved on a
beautifully mean-reverting asset can watch that asset turn into a trend and
keep trading it — buying every level down, never breaching a stop that sits
below the whole range — and bleed for weeks while looking, from the outside,
exactly like a grid doing its job.

The stop protects against a fast break. It does nothing about a slow one. This
module is the check for the slow one.

WHAT IT DOES NOT DO
===================

It does not close anything. Stopping a grid is the owner's decision, the same
principle as `control.halt_all` cancelling orders without liquidating: the
system is allowed to tell you the thesis has broken, and is not allowed to
decide what that means for your money. A drift alert is an argument, not an
action.

WHAT COUNTS AS DECAY
====================

Measured on the bars SINCE APPROVAL only, because that is the period the
original thesis is making a claim about. Any of:

    ADF no longer significant   the reversion claim itself has lapsed
    Hurst crossed the gate      it now trends
    ADX above 25                a directional trend is present
    >30% of bars outside range  the band no longer describes where it trades

The last one is the most practically important and the least statistical: a
grid whose price has left its range is not mean-reverting badly, it is not
grid-trading at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from . import metrics
from .plan import GridPlan

#: Share of recent bars that may sit outside the band before it is called drift.
MAX_OUTSIDE_FRACTION = 0.30

#: Fewest post-approval bars worth testing. Below this the statistics are noise
#: and a "drift alert" would be a coin flip.
MIN_BARS = 120


@dataclass(frozen=True)
class Reading:
    """One metric, then and now."""

    name: str
    before: Optional[float]
    after: Optional[float]
    threshold: float
    decayed: bool
    note: str = ""

    def describe(self) -> str:
        b = "n/a" if self.before is None else f"{self.before:.3f}"
        a = "n/a" if self.after is None else f"{self.after:.3f}"
        mark = "  DECAYED" if self.decayed else ""
        return f"  {self.name:22} {b:>8} -> {a:>8}{mark}"


@dataclass
class DriftReport:
    """Whether a live grid's thesis still holds, and how it changed."""

    symbol: str
    plan_id: str
    n_bars: int
    readings: list[Reading] = field(default_factory=list)
    outside_fraction: float = 0.0
    decayed: bool = False
    reasons: list[str] = field(default_factory=list)
    skipped: str = ""
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc)
        .isoformat(timespec="seconds"))

    def explain(self) -> str:
        if self.skipped:
            return f"{self.symbol}: not checked — {self.skipped}"
        head = ("THESIS DECAYED" if self.decayed else "thesis still holds")
        lines = [f"{self.symbol} ({self.plan_id}): {head}",
                 f"  measured on {self.n_bars} bars since approval",
                 "  metric                  at approval      now"]
        lines += [r.describe() for r in self.readings]
        lines.append(f"  {'bars outside range':22} "
                     f"{self.outside_fraction:>8.0%}"
                     + ("  DECAYED" if self.outside_fraction
                        > MAX_OUTSIDE_FRACTION else ""))
        for r in self.reasons:
            lines.append(f"  - {r}")
        return "\n".join(lines)

    def message(self) -> str:
        """The notification body. Deliberately says what it is NOT doing."""
        return (f"⚠️ {self.symbol}: the thesis this grid was approved on has "
                f"decayed.\n\n{self.explain()}\n\n"
                f"The grid is STILL RUNNING and no orders have been cancelled — "
                f"stopping is your decision. `grid stop {self.plan_id}` in the "
                f"web UI, or leave it if you disagree.")


def check_plan(plan: GridPlan, closes: Sequence[float], highs: Sequence[float],
               lows: Sequence[float], *, thresholds: Any = None
               ) -> DriftReport:
    """Re-run the evidence on post-approval bars and compare with approval.

    `closes`/`highs`/`lows` must already be sliced to the period since the plan
    was approved — see `bars_since`. Passing the full history would measure the
    regime that justified the grid, which is precisely the question not being
    asked.
    """
    from .rubric import Thresholds
    t = thresholds or Thresholds()
    rep = DriftReport(symbol=plan.symbol, plan_id=plan.id, n_bars=len(closes))

    if len(closes) < MIN_BARS:
        rep.skipped = (f"only {len(closes)} bars since approval, need "
                       f"{MIN_BARS} before the statistics mean anything")
        return rep

    before = plan.metrics or {}
    adf = metrics.adf_test(closes)
    hurst = metrics.hurst(closes)
    adx = metrics.adx(highs, lows, closes)

    adf_p = adf.pvalue if adf else None
    adf_decayed = adf_p is None or adf_p >= t.adf_alpha
    rep.readings.append(Reading(
        "ADF p-value", before.get("adf_p"), adf_p, t.adf_alpha, adf_decayed))
    if adf_decayed:
        rep.reasons.append(
            f"ADF p is now {adf_p:.3f} (was {before.get('adf_p', float('nan')):.3f}) "
            f"— the random-walk null is no longer rejected, so the reversion "
            f"claim this grid was approved on has lapsed"
            if adf_p is not None else
            "the ADF test can no longer be computed on recent bars")

    hurst_decayed = hurst is None or hurst > t.max_hurst
    rep.readings.append(Reading(
        "Hurst", before.get("hurst"), hurst, t.max_hurst, hurst_decayed))
    if hurst_decayed and hurst is not None:
        rep.reasons.append(
            f"Hurst has risen to {hurst:.3f}, above the {t.max_hurst} gate — "
            f"this asset now trends, and a grid in a trend buys all the way down")

    adx_decayed = adx is None or adx > t.max_adx
    rep.readings.append(Reading("ADX", before.get("adx"), adx, t.max_adx,
                                adx_decayed))
    if adx_decayed and adx is not None:
        rep.reasons.append(
            f"ADX has risen to {adx:.1f}, above {t.max_adx} — a directional "
            f"trend is present")

    inside = metrics.range_containment(closes, plan.lower, plan.upper)
    rep.outside_fraction = 1.0 - inside
    if rep.outside_fraction > MAX_OUTSIDE_FRACTION:
        rep.reasons.append(
            f"{rep.outside_fraction:.0%} of recent bars have traded OUTSIDE "
            f"{plan.lower:.6g}-{plan.upper:.6g} — the band no longer describes "
            f"where this trades, so the grid is mostly idle or mostly long")

    rep.decayed = bool(rep.reasons)
    return rep


def bars_since(candles: Sequence[dict], approved_at: Optional[str]
               ) -> list[dict]:
    """Candles from the approval timestamp onward.

    An unparseable or missing timestamp returns everything, and the caller's
    `MIN_BARS` check then decides. Guessing a start date would silently change
    which regime is being measured.
    """
    if not approved_at:
        return list(candles)
    try:
        cutoff = datetime.fromisoformat(approved_at).timestamp() * 1000.0
    except (ValueError, TypeError):
        return list(candles)
    return [c for c in candles if c.get("ts", 0.0) >= cutoff]
