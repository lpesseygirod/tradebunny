"""Pure scoring functions. No I/O, no clock, no network — so they can be tested.

The whole project rests on these being right, so I kept them boring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence


@dataclass(frozen=True)
class Outcome:
    """The scored result of one prediction."""

    asset_return_pct: float
    benchmark_return_pct: float
    gross_excess_pct: float
    net_excess_pct: float
    hit: bool


def excess_return(
    entry_price: float,
    exit_price: float,
    benchmark_entry: float,
    benchmark_exit: float,
    direction: str,
    cost_bps: float = 10.0,
) -> Outcome:
    """Score one prediction against its benchmark, net of costs.

    `direction` is "long" or "short". A short prediction that falls relative to
    its benchmark is a hit, so the sign of the excess is flipped for shorts.

    `cost_bps` is round-trip trading friction in basis points, always subtracted
    from the excess — an edge smaller than its costs is not an edge.
    """
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    for name, price in (
        ("entry_price", entry_price),
        ("exit_price", exit_price),
        ("benchmark_entry", benchmark_entry),
        ("benchmark_exit", benchmark_exit),
    ):
        if price is None or price <= 0:
            raise ValueError(f"{name} must be a positive price, got {price!r}")

    asset = (exit_price / entry_price - 1.0) * 100.0
    bench = (benchmark_exit / benchmark_entry - 1.0) * 100.0
    gross = asset - bench
    if direction == "short":
        gross = -gross

    net = gross - (cost_bps / 100.0)
    return Outcome(
        asset_return_pct=asset,
        benchmark_return_pct=bench,
        gross_excess_pct=gross,
        net_excess_pct=net,
        hit=net > 0.0,
    )


def brier_score(confidences: Sequence[float], hits: Sequence[bool]) -> Optional[float]:
    """Mean squared error of the confidence forecasts. Lower is better.

    0.25 is the score for always saying 50%, so anything at or above 0.25 means
    the confidence numbers carry no information. This converges much faster than
    return metrics, which makes it the most useful early signal.
    """
    if len(confidences) != len(hits):
        raise ValueError("confidences and hits must be the same length")
    if not confidences:
        return None
    total = 0.0
    for p, hit in zip(confidences, hits):
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {p!r}")
        total += (p - (1.0 if hit else 0.0)) ** 2
    return total / len(confidences)


def calibration_buckets(
    confidences: Sequence[float], hits: Sequence[bool], n_buckets: int = 5
) -> list[dict]:
    """Group predictions by stated confidence and report the realised hit rate.

    This shows whether "70% confident" actually means anything.
    """
    if len(confidences) != len(hits):
        raise ValueError("confidences and hits must be the same length")
    buckets: list[dict] = []
    width = 1.0 / n_buckets
    # Assign by index rather than by scanning float ranges. `3 * 0.2` is
    # 0.6000000000000001, so p=0.6 satisfied `0.4 <= 0.6 < 0.6000000000000001` and
    # landed one bucket too low — visible in the output as a bucket labelled
    # (0.4, 0.6) reporting a stated confidence of 0.6. Since rules.py clamps
    # confidence to [0.5, 0.9] and models emit round numbers, 0.6 is among the most
    # common inputs there is.
    assigned: list[list[int]] = [[] for _ in range(n_buckets)]
    for j, p in enumerate(confidences):
        assigned[min(int(p * n_buckets), n_buckets - 1)].append(j)

    for i in range(n_buckets):
        lo, hi = i * width, (i + 1) * width
        idx = assigned[i]
        buckets.append(
            {
                "range": (round(lo, 3), round(hi, 3)),
                "n": len(idx),
                "stated": (sum(confidences[j] for j in idx) / len(idx)) if idx else None,
                "realised": (sum(1 for j in idx if hits[j]) / len(idx)) if idx else None,
            }
        )
    return buckets


@dataclass(frozen=True)
class Scorecard:
    n: int
    hit_rate: Optional[float]
    mean_net_excess: Optional[float]
    brier: Optional[float]
    twin_hit_rate: Optional[float]
    twin_mean_net_excess: Optional[float]
    taken: int
    taken_mean_net_excess: Optional[float]
    skipped_mean_net_excess: Optional[float]

    @property
    def beats_twin(self) -> Optional[bool]:
        """Did the reasoning beat its own coin flip? The only question that matters."""
        if self.mean_net_excess is None or self.twin_mean_net_excess is None:
            return None
        return self.mean_net_excess > self.twin_mean_net_excess

    @property
    def filter_value(self) -> Optional[float]:
        """How much my own bought/skipped judgement added, in pct points.

        Positive means my instinct helped. Negative means it cost me.
        """
        if self.taken_mean_net_excess is None or self.mean_net_excess is None:
            return None
        return self.taken_mean_net_excess - self.mean_net_excess


def _mean(xs: Iterable[float]) -> Optional[float]:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def build_scorecard(rows: Sequence[dict]) -> Scorecard:
    """Aggregate resolved predictions into the numbers the daily recap shows.

    Each row needs: net_excess_pct, hit, confidence, decision,
    twin_net_excess_pct, twin_hit.
    """
    resolved = [r for r in rows if r.get("net_excess_pct") is not None]
    if not resolved:
        return Scorecard(0, None, None, None, None, None, 0, None, None)

    hits = [bool(r["hit"]) for r in resolved]
    excesses = [float(r["net_excess_pct"]) for r in resolved]
    confs = [float(r["confidence"]) for r in resolved]

    twins = [r for r in resolved if r.get("twin_net_excess_pct") is not None]
    taken = [r for r in resolved if r.get("decision") == "bought"]
    skipped = [r for r in resolved if r.get("decision") != "bought"]

    return Scorecard(
        n=len(resolved),
        hit_rate=sum(hits) / len(hits),
        mean_net_excess=_mean(excesses),
        brier=brier_score(confs, hits),
        twin_hit_rate=(sum(1 for r in twins if r["twin_hit"]) / len(twins)) if twins else None,
        twin_mean_net_excess=_mean(float(r["twin_net_excess_pct"]) for r in twins),
        taken=len(taken),
        taken_mean_net_excess=_mean(float(r["net_excess_pct"]) for r in taken),
        skipped_mean_net_excess=_mean(float(r["net_excess_pct"]) for r in skipped),
    )


def sample_size_warning(n: int) -> Optional[str]:
    """Return a warning while the sample is too small to mean anything.

    Financial returns are noisy enough that a few dozen predictions cannot
    distinguish skill from luck. This keeps that in the recap so I don't forget it.
    """
    if n < 30:
        return f"{n} resolved — far too few to conclude anything. Keep collecting."
    if n < 100:
        return f"{n} resolved — still under the 100 needed before kill criteria apply."
    return None
