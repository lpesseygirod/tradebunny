"""Graded confidence, and an admission that it is a guess.

WHAT IS A GATE AND WHAT IS A SCORE
==================================

Two kinds of check live in this system and they must never be confused.

**Structural gates stay hard.** They are not evidence about whether a grid will
work; they are facts about whether it can run at all, and no amount of good
statistics elsewhere compensates for failing one:

    fee coverage        spacing that cannot clear the fees loses on every cycle
    liquidity floor     resting orders that never fill are not a strategy
    stop-loss present   a grid without one is unbounded risk
    spot-only           funding on a perpetual eats the edge whatever else is true
    max exposure        the cap is the cap
    minimum order size  the exchange rejects the order; there is nothing to grade

Those live in `rubric.assess`, `plan.validate` and `sizing.py`, and a failure of
any one is still an outright rejection.

**Evidence is graded.** These are all measurements of the same underlying
question — does this asset oscillate around a level? — with error bars, and
treating each as a pass/fail threshold throws away most of what they say. An
ADF p of 0.049 and one of 0.0001 are not the same evidence, and the old rubric
scored them identically.

THE WEIGHTING, IN ONE PLACE
===========================

    ADF p-value                0.30   the only true hypothesis test here
    Hurst interval position    0.20   where the WHOLE CI sits vs the gate
    sub-window stability       0.20   did it hold throughout, or on average?
    walk-forward out-of-sample 0.15   did the fitted plan survive unseen data?
    ADX                        0.075  corroboration
    efficiency ratio           0.075  corroboration

ADF carries the most because it is the only component that tests a hypothesis
rather than describing the sample. Stability is weighted equal to Hurst because
a property that held for half the window is the failure mode that actually
costs money — the parameters are fitted to a regime that has ended. ADX and the
efficiency ratio share a small weight between them BECAUSE THEY ARE NOT
INDEPENDENT of Hurst: all three are computed from the same few hundred numbers
and move together, and weighting them equally would be counting one piece of
evidence three times.

THE NUMBER IS UNCALIBRATED
==========================

Nothing here has been checked against outcomes. "0.65" is a weighted sum of
six normalised measurements, not a probability, and until enough grids have
resolved to score it there is no reason to believe a 0.65 wins 65% of the time.
`calibration()` below reuses the Brier machinery from `ledger/scoring.py` to
answer that question once there is anything to answer it with, and the web UI
shows the result — including, loudly, when the sample is too small to mean
anything. An uncalibrated confidence number is decoration, and decoration on a
trading decision is worse than no number at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

#: Component weights. Sum to 1.0. See the module docstring for why.
WEIGHTS: dict[str, float] = {
    "adf": 0.30,
    "hurst": 0.20,
    "stability": 0.20,
    "walk_forward": 0.15,
    "adx": 0.075,
    "efficiency": 0.075,
}

#: Permissivity levels: the minimum confidence a proposal must reach to be shown.
LEVELS: dict[str, float] = {
    "strict": 0.65,
    "balanced": 0.50,
    "exploratory": 0.35,
}
DEFAULT_LEVEL = "strict"


def threshold_for(level: str) -> float:
    try:
        return LEVELS[level]
    except KeyError:
        raise ValueError(
            f"unknown permissivity {level!r}; expected one of "
            f"{', '.join(sorted(LEVELS))}") from None


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _ramp(value: float, zero_at: float, one_at: float) -> float:
    """Linear 0..1 between two anchors, in either direction."""
    if zero_at == one_at:
        return 0.0
    return _clamp01((value - zero_at) / (one_at - zero_at))


@dataclass(frozen=True)
class Confidence:
    """A graded 0-1 score and every component that produced it."""

    score: float
    components: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=lambda: dict(WEIGHTS))
    missing: tuple[str, ...] = ()

    def passes(self, level: str = DEFAULT_LEVEL) -> bool:
        return self.score >= threshold_for(level)

    def explain(self) -> str:
        parts = ", ".join(
            f"{k} {self.components.get(k, 0.0):.2f}x{self.weights.get(k, 0.0):.3g}"
            for k in WEIGHTS)
        return f"confidence {self.score:.2f} ({parts})"


def score_adf(pvalue: Optional[float]) -> Optional[float]:
    """1.0 at p=0.001 or below, 0.0 at p=0.05 or above, log-scaled between.

    Log rather than linear because the difference between p=0.04 and p=0.02 is
    not the same amount of evidence as between p=0.002 and p=0.001.
    """
    if pvalue is None or pvalue < 0:
        return None
    import math
    if pvalue >= 0.05:
        return 0.0
    if pvalue <= 0.001:
        return 1.0
    return _clamp01((math.log(0.05) - math.log(pvalue))
                    / (math.log(0.05) - math.log(0.001)))


def score_hurst(ci_low: Optional[float], ci_high: Optional[float],
                max_hurst: float = 0.45) -> Optional[float]:
    """Where the WHOLE interval sits relative to the gate.

    An interval that clears the gate by a wide margin scores near 1; one whose
    top edge merely grazes it scores near 0; one that straddles it scores 0.
    Using the interval rather than the point estimate is the difference between
    "this is mean-reverting" and "this might be, I cannot tell".
    """
    if ci_low is None or ci_high is None:
        return None
    if ci_high >= max_hurst:
        return 0.0
    # ci_high at the gate -> 0; the whole interval under 0.25 -> 1.
    return _ramp(ci_high, max_hurst, 0.25)


def score_stability(pass_fraction: Optional[float],
                    min_pass: float = 0.70) -> Optional[float]:
    """0 at the 70% floor, 1 when every sub-window passes."""
    if pass_fraction is None:
        return None
    return _ramp(pass_fraction, min_pass, 1.0)


def score_walk_forward(oos_return: Optional[float],
                       in_sample_return: Optional[float] = None,
                       generalises: Optional[bool] = None) -> Optional[float]:
    """Did the fitted plan survive data it never saw?

    A hard 0 when it did not generalise at all — that is the walk-forward gate
    speaking, and no amount of good ADF should paper over it. Above that, the
    score rises with the share of the fitted return that survived.
    """
    if oos_return is None:
        return None
    if generalises is False:
        return 0.0
    if oos_return <= 0:
        return 0.0
    if in_sample_return and in_sample_return > 0:
        retention = oos_return / in_sample_return
        return _ramp(retention, 0.5, 1.5)
    return 0.5


def score_adx(adx: Optional[float], max_adx: float = 25.0) -> Optional[float]:
    if adx is None:
        return None
    return _ramp(adx, max_adx, 10.0)


def score_efficiency(er: Optional[float], max_er: float = 0.30) -> Optional[float]:
    if er is None:
        return None
    return _ramp(er, max_er, 0.05)


def compute(measured: dict[str, Any], *, oos_return: Optional[float] = None,
            in_sample_return: Optional[float] = None,
            generalises: Optional[bool] = None,
            max_hurst: float = 0.45,
            min_stability: float = 0.70) -> Confidence:
    """Grade the evidence. Missing components score 0 and are named.

    A missing measurement is not neutral — it is an absence of evidence, and
    the safe reading of an absence is that the case has not been made.
    """
    raw = {
        "adf": score_adf(measured.get("adf_p")),
        "hurst": score_hurst(measured.get("hurst_ci_low"),
                             measured.get("hurst_ci_high"), max_hurst),
        "stability": score_stability(measured.get("stability_pass"),
                                     min_stability),
        "walk_forward": score_walk_forward(oos_return, in_sample_return,
                                           generalises),
        "adx": score_adx(measured.get("adx")),
        "efficiency": score_efficiency(measured.get("efficiency_ratio")),
    }
    missing = tuple(k for k, v in raw.items() if v is None)
    components = {k: (v if v is not None else 0.0) for k, v in raw.items()}
    score = sum(components[k] * w for k, w in WEIGHTS.items())
    return Confidence(score=_clamp01(score), components=components,
                      weights=dict(WEIGHTS), missing=missing)


# --------------------------------------------------------------- calibration

@dataclass(frozen=True)
class Calibration:
    """Whether the confidence numbers mean anything yet."""

    n: int
    brier: Optional[float]
    buckets: tuple[dict[str, Any], ...] = ()
    min_sample: int = 20

    @property
    def is_meaningful(self) -> bool:
        return self.n >= self.min_sample and self.brier is not None

    @property
    def verdict(self) -> str:
        if not self.is_meaningful:
            return (f"UNCALIBRATED — {self.n} resolved outcome(s), need "
                    f"{self.min_sample}. The confidence number is a weighted "
                    f"sum of measurements, not a probability. Treat it as "
                    f"decoration until this says otherwise.")
        if self.brier >= 0.25:
            return (f"NO INFORMATION — Brier {self.brier:.3f} over {self.n} "
                    f"outcomes. Always saying 50% scores 0.25, so these "
                    f"confidences are no better than a shrug.")
        return (f"calibrated — Brier {self.brier:.3f} over {self.n} outcomes "
                f"(below the 0.25 no-information line)")


def calibration(confidences: Sequence[float], hits: Sequence[bool],
                min_sample: int = 20) -> Calibration:
    """Score the confidence numbers against what actually happened.

    Reuses `ledger.scoring` rather than reimplementing it — the Brier score and
    the bucketing are the same arithmetic whether the forecast is about a stock
    or a grid, and two copies would drift.
    """
    from ledger.scoring import brier_score, calibration_buckets

    n = min(len(confidences), len(hits))
    if n == 0:
        return Calibration(n=0, brier=None, min_sample=min_sample)
    c, h = list(confidences)[:n], list(hits)[:n]
    try:
        b = brier_score(c, h)
    except ValueError:
        b = None
    try:
        buckets = tuple(calibration_buckets(c, h))
    except Exception:
        buckets = ()
    return Calibration(n=n, brier=b, buckets=buckets, min_sample=min_sample)
