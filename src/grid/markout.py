"""Markout: was that fill any good?

WHAT A MARKOUT IS
=================

A fill looks identical whether it was a gift or a mistake. Markout tells them
apart: take the mid price some interval after the fill, and ask whether the
market moved toward the fill or away from it.

    buy  markout(h) = (mid(t+h) - fill_price) / fill_price
    sell markout(h) = (fill_price - mid(t+h)) / fill_price

Sign-adjusted, so POSITIVE always means the fill was favourable regardless of
side. A consistently negative markout means the grid is being adversely selected —
the counterparty knew something, or the quote was stale, and the spread
collected did not pay for it.

WHY SEGMENT BY TIME-TO-FILL
===========================

The practitioner claim this instrumentation exists to test is that FAST fills
are toxic and SLOW fills are favourable. The mechanism is intuitive: an order
that fills within seconds of being placed was probably picked off by someone
reacting to news the quote had not yet incorporated, while an order that
rested for an hour was filled by someone who wanted liquidity and paid the
spread for it.

If that is true, a wide-spaced grid should sit firmly in the favourable regime,
because its orders rest for a long time by construction. That is a prediction
about MY pairs and it is measurable, so this module measures it rather than
assuming it.

WHAT CAN AND CANNOT BE MEASURED HERE
====================================

The horizons I want are +1s, +10s, +1m, +5m, +30m. On HOURLY candles, the
first four are not observable at all — there is no price between one bar and
the next, and interpolating one would be inventing the measurement. Those
horizons are reported as absent rather than filled in, and become measurable
only against a live feed or sub-minute candles.

At hourly resolution the measurable horizons are +1h, +4h, +24h, and those are
included alongside. A markout curve with holes in it is more useful than a
smooth one that is partly fabricated.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

#: The horizons I want, plus the ones hourly candles can actually see.
HORIZONS: dict[str, float] = {
    "+1s": 1.0,
    "+10s": 10.0,
    "+1m": 60.0,
    "+5m": 300.0,
    "+30m": 1_800.0,
    "+1h": 3_600.0,
    "+4h": 14_400.0,
    "+24h": 86_400.0,
}

#: Time-to-fill buckets. "Fast" here means within one bar of being placed.
BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("fast (<1m)", 0.0, 60.0),
    ("quick (1-10m)", 60.0, 600.0),
    ("normal (10m-1h)", 600.0, 3_600.0),
    ("slow (1-6h)", 3_600.0, 21_600.0),
    ("very slow (>6h)", 21_600.0, float("inf")),
)


@dataclass(frozen=True)
class Markout:
    """One fill, and where the mid went afterwards."""

    symbol: str
    side: str
    fill_price: float
    filled_at: float               # unix seconds
    time_to_fill: float            # seconds the order rested before filling
    points: dict[str, float] = field(default_factory=dict)   # horizon -> signed

    @property
    def bucket(self) -> str:
        for name, lo, hi in BUCKETS:
            if lo <= self.time_to_fill < hi:
                return name
        return BUCKETS[-1][0]


def signed_markout(side: str, fill_price: float, later_mid: float) -> float:
    """Positive means the fill was favourable, whichever side it was."""
    if fill_price <= 0:
        return 0.0
    raw = (later_mid - fill_price) / fill_price
    return raw if side == "buy" else -raw


def measure(symbol: str, side: str, fill_price: float, filled_at: float,
            time_to_fill: float, prices: Sequence[tuple[float, float]],
            horizons: Optional[dict[str, float]] = None) -> Markout:
    """Build one markout from a (timestamp, price) series.

    A horizon with no observation at or after it is OMITTED, not interpolated.
    Inventing the price between two hourly candles would be inventing the
    measurement this module exists to take.
    """
    horizons = horizons or HORIZONS
    pts: dict[str, float] = {}
    for name, dt in horizons.items():
        target = filled_at + dt
        # The observation must land within HALF a horizon of the target.
        # Without a tolerance, hourly data answers "+1s" with the next bar's
        # close an hour later and every short horizon reports the same number —
        # a curve that looks measured and is not. Half a horizon is tight
        # enough that +30m cannot quietly absorb a +1h observation, and loose
        # enough that a bar landing exactly on the horizon still counts.
        latest = target + dt * 0.5
        found = None
        for ts, px in prices:
            if ts >= target:
                if ts <= latest:
                    found = px
                break
        if found is not None:
            pts[name] = signed_markout(side, fill_price, found)
    return Markout(symbol=symbol, side=side, fill_price=fill_price,
                   filled_at=filled_at, time_to_fill=time_to_fill, points=pts)


@dataclass(frozen=True)
class Curve:
    """The markout curve for one bucket of fills."""

    bucket: str
    n: int
    points: dict[str, float]              # horizon -> mean signed markout
    counts: dict[str, int]                # horizon -> how many observed

    def describe(self, horizons: Optional[Sequence[str]] = None) -> str:
        names = horizons or list(HORIZONS)
        cells = []
        for h in names:
            if h in self.points:
                cells.append(f"{h} {self.points[h]:+.4%}")
            else:
                cells.append(f"{h} —")
        return f"  {self.bucket:18} n={self.n:<5} " + "  ".join(cells)


def curves(markouts: Sequence[Markout]) -> list[Curve]:
    """Mean signed markout at each horizon, segmented by time-to-fill."""
    out = []
    for name, _, _ in BUCKETS:
        mine = [m for m in markouts if m.bucket == name]
        if not mine:
            continue
        pts: dict[str, float] = {}
        counts: dict[str, int] = {}
        for h in HORIZONS:
            vals = [m.points[h] for m in mine if h in m.points]
            if vals:
                pts[h] = sum(vals) / len(vals)
                counts[h] = len(vals)
        out.append(Curve(bucket=name, n=len(mine), points=pts, counts=counts))
    return out


def report(markouts: Sequence[Markout]) -> str:
    """The curve, and a verdict on the fast-is-toxic claim."""
    if not markouts:
        return ("no fills to measure. Markout needs fills, and a grid that has "
                "not traded has nothing to say about execution quality.")
    cs = curves(markouts)
    observed = sorted({h for c in cs for h in c.points},
                      key=lambda h: HORIZONS[h])
    missing = [h for h in HORIZONS if h not in observed]

    lines = [f"MARKOUT CURVE ({len(markouts)} fills), sign-adjusted — positive "
             f"means the fill was favourable", ""]
    lines += [c.describe(observed) for c in cs]
    if missing:
        lines.append("")
        lines.append(f"  not observable at this bar size: {', '.join(missing)}"
                     f" — there is no price between one candle and the next, "
                     f"and interpolating one would invent the measurement.")

    # The claim under test.
    fast = next((c for c in cs if c.bucket.startswith("fast")), None)
    slow = [c for c in cs if c.bucket.startswith(("slow", "very"))]
    if observed:
        h = observed[-1]
        f_val = fast.points.get(h) if fast else None
        s_vals = [c.points[h] for c in slow if h in c.points]
        lines.append("")
        if f_val is not None and s_vals:
            s_val = sum(s_vals) / len(s_vals)
            verdict = ("CONFIRMED for these pairs" if s_val > f_val
                       else "NOT confirmed for these pairs")
            lines.append(f"  fast fills {f_val:+.4%} vs slow fills "
                         f"{s_val:+.4%} at {h} — the 'fast fills are toxic' "
                         f"claim is {verdict}.")
        else:
            lines.append("  not enough fills in both the fast and slow buckets "
                         "to test the fast-is-toxic claim yet.")
    return "\n".join(lines)


def from_backtest(fills: Sequence[dict], closes: Sequence[float],
                  bar_seconds: float, start_ts: float = 0.0) -> list[Markout]:
    """Markouts for a backtest run, using the close series as the mid.

    A backtest's "mid" is the close of each bar, which is the only price it
    has. That makes the hourly horizons meaningful and the sub-minute ones
    absent, exactly as above.
    """
    series = [(start_ts + i * bar_seconds, float(p)) for i, p in enumerate(closes)]
    out = []
    for f in fills:
        idx = int(f.get("bar_index", 0))
        out.append(measure(
            symbol=str(f.get("symbol", "")), side=str(f.get("side", "buy")),
            fill_price=float(f.get("price", 0.0)),
            filled_at=start_ts + idx * bar_seconds,
            time_to_fill=float(f.get("time_to_fill", bar_seconds)),
            prices=series[idx + 1:]))
    return out
