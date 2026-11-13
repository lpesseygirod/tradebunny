"""Quantitative measures for judging whether an instrument suits a grid.

The single insight this module exists to encode: **a grid needs high volatility AND
low directionality, and those are different things.** People conflate them and pick
"volatile" assets, which is how grids die — a volatile asset in a trend is the worst
possible case, because it accumulates losing positions quickly.

What I actually want is an asset that moves a lot and gets nowhere. Each function
here measures one half of that, and `rubric.py` combines them.

Pure Python, no numpy — these run over a few hundred bars and the dependency isn't
worth it. Every function takes plain lists and is unit-tested against synthetic
series whose answer is known.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import math
from typing import Optional, Sequence


# --------------------------------------------------------------------- helpers

def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _stdev(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _ols_slope(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Least-squares slope. Used for the Hurst log-log regression."""
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = _mean(xs), _mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / den


def returns(prices: Sequence[float]) -> list[float]:
    """Simple period-over-period returns."""
    return [prices[i] / prices[i - 1] - 1.0 for i in range(1, len(prices))
            if prices[i - 1] > 0]


# ------------------------------------------------------------ directionality

def hurst(prices: Sequence[float], min_lag: int = 2, max_lag: int = 40) -> Optional[float]:
    """Hurst exponent via the variance-of-lagged-differences method.

        H > 0.55  trending — momentum persists. BAD for a grid.
        H ~ 0.50  random walk — no edge either way.
        H < 0.45  mean-reverting — moves reverse. GOOD for a grid.

    The 0.45–0.55 band is transitional and the answer there is "don't trade".

    Needs roughly 100+ bars to be stable; under 50 it is noise. And it is
    descriptive, not predictive — it describes how the asset behaved, and regime
    changes only show up after they have started.
    """
    n = len(prices)
    if n < max_lag * 2 or n < 40:
        return None
    # Refuse rather than filter. Silently dropping non-positive prices would splice
    # the series together and manufacture a jump that never happened.
    if any(p <= 0 for p in prices):
        return None
    logp = [math.log(p) for p in prices]

    lags, devs = [], []
    for lag in range(min_lag, min(max_lag, len(logp) // 2)):
        diffs = [logp[i + lag] - logp[i] for i in range(len(logp) - lag)]
        sd = _stdev(diffs)
        if sd > 0:
            lags.append(math.log(lag))
            devs.append(math.log(sd))
    if len(lags) < 4:
        return None
    return _ols_slope(lags, devs)


def variance_ratio(prices: Sequence[float], k: int = 10) -> Optional[float]:
    """Lo-MacKinlay variance ratio at horizon k.

        VR < 1  mean reversion (k-period moves smaller than k independent moves)
        VR = 1  random walk
        VR > 1  trending

    IMPORTANT — this is horizon-dependent, and that is not a flaw to work around
    but a property to respect. A smooth oscillation with a 25-bar period reads as
    TRENDING at k=5, because over five bars it is moving steadily through its
    cycle; it only reads as mean-reverting at k>=20. Measured on real fixtures:

        Ornstein-Uhlenbeck  k=5: 0.70   k=10: 0.42   k=20: 0.23
        smooth sine (p=25)  k=5: 3.43   k=10: 4.47   k=20: 0.88
        trending series     k=5: 0.90   k=10: 0.98   k=20: 1.15

    Because a single k can therefore reject a genuinely good grid candidate, this
    is used as a CROSS-CHECK on the Hurst exponent, never as a standalone gate.
    Disagreement between the two is the useful signal: it means the reversion
    horizon and the measurement horizon do not match, and the asset deserves
    suspicion rather than automatic rejection.
    """
    r = returns(prices)
    if len(r) < k * 4:
        return None
    var1 = _stdev(r) ** 2
    if var1 == 0:
        return None
    rk = [sum(r[i:i + k]) for i in range(0, len(r) - k + 1)]
    return (_stdev(rk) ** 2) / (k * var1)


def efficiency_ratio(prices: Sequence[float]) -> Optional[float]:
    """Kaufman efficiency ratio: net movement divided by total path travelled.

        near 0  price thrashed about and ended where it started — IDEAL for a grid
        near 1  price marched in a straight line — worst case for a grid

    This is the most directly relevant single number in the module. A grid earns
    from path length while being harmed by net displacement, and this is exactly
    that ratio.
    """
    if len(prices) < 3:
        return None
    path = sum(abs(prices[i] - prices[i - 1]) for i in range(1, len(prices)))
    if path == 0:
        return None
    return abs(prices[-1] - prices[0]) / path


def adx(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        period: int = 14) -> Optional[float]:
    """Wilder's Average Directional Index — trend strength, ignoring direction.

        ADX < 20  no trend, choppy       GOOD for a grid
        ADX < 25  range-bound            acceptable
        ADX > 25  trending               AVOID
    """
    n = len(closes)
    if n < period * 3 or len(highs) != n or len(lows) != n:
        return None

    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, n):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))

    def smooth(xs: list[float]) -> list[float]:
        """Wilder smoothing: seed with a sum, then decay."""
        out = [sum(xs[:period])]
        for x in xs[period:]:
            out.append(out[-1] - out[-1] / period + x)
        return out

    str_, spdm, smdm = smooth(trs), smooth(plus_dm), smooth(minus_dm)
    dxs = []
    for i in range(len(str_)):
        if str_[i] == 0:
            continue
        pdi = 100 * spdm[i] / str_[i]
        mdi = 100 * smdm[i] / str_[i]
        if pdi + mdi > 0:
            dxs.append(100 * abs(pdi - mdi) / (pdi + mdi))
    if len(dxs) < period:
        return None
    # Wilder's ADX: seed with the mean of the FIRST `period` DX values, then smooth
    # forward recursively. A simple mean of the last `period` values is a 14-bar SMA
    # of DX — a different, much shorter-memory indicator that reads an asset which
    # trended hard and then went quiet for 14 bars as non-trending. Measured against
    # a reference implementation, ~5% of series landed on opposite sides of the
    # ADX-25 gate, biased toward wrongly ACCEPTING recent trends. That is precisely
    # the regime that destroys a grid.
    val = _mean(dxs[:period])
    for dx in dxs[period:]:
        val = (val * (period - 1) + dx) / period
    return val


# ---------------------------------------------------------------- volatility

def atr_pct(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
            period: int = 14) -> Optional[float]:
    """Average True Range as a fraction of price — how far it moves per bar.

    This is the revenue driver: it decides how many grid levels get crossed, and
    therefore how often a profitable cycle completes.
    """
    n = len(closes)
    if n < period + 1 or len(highs) != n or len(lows) != n:
        return None
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
               abs(lows[i] - closes[i - 1])) for i in range(1, n)]
    if len(trs) < period:
        return None
    # Wilder's smoothing, not a simple mean of the last `period` bars.
    #
    # Two reasons. It is what `adx()` below already uses, and having the two
    # gates disagree about what "average true range" means is the kind of
    # inconsistency that makes a rejection impossible to reason about. And it
    # is what every charting package plots, so a number read off TradingView
    # and a number from this file now refer to the same quantity.
    #
    # The practical difference: Wilder keeps a decaying memory of every bar
    # rather than a hard window, so a single violent bar 15 bars ago still
    # counts for something instead of dropping out of the average entirely.
    atr = _mean(trs[:period])
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr / closes[-1] if closes[-1] > 0 else None


def range_containment(prices: Sequence[float], lower: float, upper: float) -> float:
    """Fraction of bars that sat inside the proposed band.

    A band containing 95% of the last 200 bars is an observed range. One containing
    60% is a line drawn through the middle of a trend.
    """
    if not prices:
        return 0.0
    return sum(1 for p in prices if lower <= p <= upper) / len(prices)


def range_touches(prices: Sequence[float], lower: float, upper: float,
                  tolerance: float = 0.01) -> int:
    """How many times price approached either boundary and turned back.

    An untested range is a guess. A range that has held through eight touches is
    evidence — still not a guarantee, but evidence.
    """
    if not prices:
        return 0
    band = (upper - lower) * tolerance
    touches, armed = 0, None
    for p in prices:
        if p <= lower + band and armed != "low":
            touches, armed = touches + 1, "low"
        elif p >= upper - band and armed != "high":
            touches, armed = touches + 1, "high"
        elif lower + band * 3 < p < upper - band * 3:
            armed = None          # returned to the middle; the next touch counts
    return touches


# ===========================================================================
# Statistical evidence
# ===========================================================================
#
# Everything above this line is a descriptive statistic: a number that
# summarises how the series behaved. None of them carries a notion of
# CONFIDENCE, and four descriptive statistics agreeing is not four pieces of
# evidence — they are computed from the same 300 numbers and are heavily
# correlated with each other. A series can score Hurst 0.44, ADX 20 and
# efficiency 0.10 purely by chance.
#
# What follows tests hypotheses instead:
#
#   adf_test           is the random-walk null rejected? This is the actual
#                      question "mean-reverting" is asking, and it is the only
#                      one here with a p-value attached.
#   hurst_ci           how uncertain is the Hurst estimate? Gate on the bound,
#                      not the point.
#   rolling_stability  did the property hold THROUGHOUT, or only on average?
#   benjamini_hochberg when 30 assets are tested at p<0.05, roughly 1.5 pass by
#                      luck. Correct for it or stop quoting p-values.


@dataclass(frozen=True)
class AdfResult:
    """Augmented Dickey-Fuller. The null is a unit root, i.e. a random walk."""

    statistic: float
    pvalue: float
    n_lags: int
    n_obs: int
    critical_values: dict[str, float] = field(default_factory=dict)

    def rejects_random_walk(self, alpha: float = 0.05) -> bool:
        return self.pvalue < alpha


def adf_test(prices: Sequence[float], regression: str = "c",
             autolag: str = "AIC") -> Optional[AdfResult]:
    """Test the random-walk null directly.

        p < 0.05   the unit root is rejected — the series reverts to a level
        p >= 0.05  cannot reject a random walk — no evidence of reversion

    This is what "mean-reverting" means, stated as a hypothesis with an error
    rate, rather than as a descriptive number with an eyeballed threshold. It
    is the PRIMARY evidence in `rubric.assess`; Hurst, ADX and the efficiency
    ratio corroborate it.

    Run on log prices when they are all positive, because a grid cares about
    proportional moves and a unit-root test on raw prices is sensitive to the
    level. Falls back to raw prices otherwise.

    `regression="c"` includes a constant but no trend: a series reverting to a
    RISING line is still trending as far as a grid is concerned, and "ct" would
    call that stationary.
    """
    n = len(prices)
    if n < 30:
        return None
    try:
        import numpy as np
        from statsmodels.tsa.stattools import adfuller
    except ImportError:
        return None

    arr = np.asarray(prices, dtype=float)
    if not np.all(np.isfinite(arr)):
        return None
    if np.all(arr > 0):
        arr = np.log(arr)
    if float(np.std(arr)) == 0.0:
        return None            # a constant series has no unit root to test

    try:
        import warnings
        with warnings.catch_warnings():
            # A rank-deficient design means the regression is degenerate — a
            # perfectly periodic or near-constant series does it. There is no
            # usable p-value in that case, and "no p-value" must read as "no
            # evidence", never as a pass.
            warnings.simplefilter("error")
            # result_object is pinned: statsmodels 0.16 flips the default, and
            # a silently changed return shape would be a very quiet bug.
            try:
                out = adfuller(arr, regression=regression, autolag=autolag,
                               result_object=False)
            except TypeError:
                warnings.simplefilter("ignore", FutureWarning)
                out = adfuller(arr, regression=regression, autolag=autolag)
    except Exception:
        return None

    stat, pvalue, usedlag, nobs, crit = out[0], out[1], out[2], out[3], out[4]
    return AdfResult(statistic=float(stat), pvalue=float(pvalue),
                     n_lags=int(usedlag), n_obs=int(nobs),
                     critical_values={k: float(v) for k, v in dict(crit).items()})


@dataclass(frozen=True)
class HurstCI:
    """A Hurst estimate with an interval around it."""

    point: float
    low: float
    high: float
    n_boot: int
    block: int

    @property
    def width(self) -> float:
        return self.high - self.low

    def certainly_below(self, threshold: float) -> bool:
        """The whole interval sits under the threshold."""
        return self.high < threshold


def _hurst_from_log(logp, max_lag: int = 40) -> Optional[float]:
    """Vectorised Hurst on an already-logged numpy array. Used by the bootstrap,
    where the pure-Python version would be a thousand times too slow."""
    import numpy as np
    n = len(logp)
    max_lag = min(max_lag, n // 2)
    lags, devs = [], []
    for lag in range(2, max_lag):
        d = logp[lag:] - logp[:-lag]
        sd = float(np.std(d, ddof=1)) if len(d) > 1 else 0.0
        if sd > 0:
            lags.append(math.log(lag))
            devs.append(math.log(sd))
    if len(lags) < 4:
        return None
    x = np.asarray(lags)
    y = np.asarray(devs)
    xm, ym = x.mean(), y.mean()
    denom = float(((x - xm) ** 2).sum())
    if denom == 0:
        return None
    return float(((x - xm) * (y - ym)).sum() / denom)


def hurst_ci(prices: Sequence[float], n_boot: int = 1000,
             confidence: float = 0.95, block: Optional[int] = None,
             seed: int = 0) -> Optional[HurstCI]:
    """Hurst with a bootstrap confidence interval.

    MOVING BLOCK bootstrap, not iid. An iid resample of returns destroys the
    autocorrelation, and autocorrelation is precisely the thing Hurst measures
    — an iid bootstrap would produce an interval centred on 0.5 for every input
    and say nothing. Blocks of consecutive returns preserve local
    dependence while still varying the sample.

    BLOCK LENGTH MATTERS MORE THAN USUAL HERE, and the textbook rule is wrong
    for this statistic. The usual n**(1/3) gives blocks of ~7 for 300 bars,
    but Hurst is estimated from lagged differences out to lag 40 — so blocks of
    7 destroy every dependence the estimator is looking at, and the bootstrap
    reports the distribution of a random walk regardless of the input. Measured:
    an OU series with a point estimate of 0.175 produced an interval of
    [0.230, 0.562], not even containing its own point estimate.
    
    So the block must be at least as long as the longest lag measured. The lag
    range is tied to the block length here, which means a short series gets a
    shorter lag range and this point estimate can differ slightly from
    `hurst()`; `point` is always computed the same way as the interval so the
    two are comparable to each other.

    Gate on `high`, not `point`: an estimate of 0.44 with an interval reaching
    0.52 is not evidence of mean reversion, it is an inconclusive measurement
    that happened to land on a convenient number.
    """
    n = len(prices)
    if n < 60:
        return None
    try:
        import numpy as np
    except ImportError:
        return None

    arr = np.asarray(prices, dtype=float)
    if not np.all(np.isfinite(arr)) or np.any(arr <= 0):
        return None
    logp = np.log(arr)
    rets = np.diff(logp)
    if len(rets) < 30 or float(np.std(rets)) == 0.0:
        return None

    # Long enough to preserve dependence out to the longest lag measured, but
    # short enough to leave at least five blocks to resample.
    if block is not None:
        L = block
    else:
        L = max(40, int(round(len(rets) ** (1 / 3))))
    L = max(4, min(L, len(rets) // 5))
    max_lag = max(4, min(40, L))

    point = _hurst_from_log(logp, max_lag)
    if point is None:
        return None
    n_blocks = int(math.ceil(len(rets) / L))
    starts_max = len(rets) - L
    rng = np.random.default_rng(seed)

    estimates = []
    for _ in range(n_boot):
        starts = rng.integers(0, starts_max + 1, size=n_blocks)
        sample = np.concatenate([rets[s:s + L] for s in starts])[:len(rets)]
        boot_log = np.concatenate([[logp[0]], logp[0] + np.cumsum(sample)])
        h = _hurst_from_log(boot_log, max_lag)
        if h is not None:
            estimates.append(h)
    if len(estimates) < max(20, n_boot // 10):
        return None

    tail = (1.0 - confidence) / 2.0
    lo, hi = np.quantile(np.asarray(estimates), [tail, 1.0 - tail])
    return HurstCI(point=float(point), low=float(lo), high=float(hi),
                   n_boot=len(estimates), block=int(L))


@dataclass(frozen=True)
class Stability:
    """How a metric behaved across overlapping sub-windows."""

    mean: float
    stdev: float
    pass_fraction: float
    n_windows: int
    values: tuple[float, ...] = ()

    def is_stable(self, min_pass: float = 0.70) -> bool:
        return self.pass_fraction >= min_pass


def rolling_stability(prices: Sequence[float], window: int, metric_fn,
                      passes=None, step: Optional[int] = None
                      ) -> Optional[Stability]:
    """Compute `metric_fn` over overlapping sub-windows.

    A full-window average hides everything about WHEN a property held. A series
    that mean-reverts beautifully for four months and then trends for two can
    average out to a perfectly acceptable Hurst, and the average is describing
    a regime that has already ended. This asks the harder question: did the
    property hold throughout?

    `passes(value) -> bool` decides whether a sub-window counts as passing; the
    fraction that do is what `rubric` gates on. `mean` and `stdev` describe the
    spread, and a large stdev is a warning even when the mean is fine.
    """
    n = len(prices)
    if window < 20 or n < window + 1:
        return None
    step = step or max(1, window // 4)
    values = []
    for start in range(0, n - window + 1, step):
        v = metric_fn(prices[start:start + window])
        if v is not None:
            values.append(float(v))
    if len(values) < 3:
        return None
    passing = (sum(1 for v in values if passes(v)) / len(values)
               if passes is not None else 1.0)
    return Stability(mean=_mean(values), stdev=_stdev(values),
                     pass_fraction=passing, n_windows=len(values),
                     values=tuple(values))


def benjamini_hochberg(pvalues: Sequence[float], alpha: float = 0.05
                       ) -> list[bool]:
    """Which p-values survive a Benjamini-Hochberg correction at `alpha`.

    Testing 30 assets at p<0.05 independently expects about 1.5 false positives
    — so on a quiet day the one candidate that "passed" is more likely to be
    noise than signal. BH controls the false DISCOVERY rate: of the candidates
    accepted, at most `alpha` of them are expected to be spurious.

    I chose BH over Bonferroni. Bonferroni controls the probability of
    ANY false positive, which at 30 tests means a threshold of 0.0017 and
    rejects nearly everything. BH is the right trade when the cost of a missed
    opportunity is zero (there is always tomorrow) but the cost of a false one
    is real money.

    Returns a mask in the caller's original order.
    """
    n = len(pvalues)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: pvalues[i])
    keep = [False] * n
    largest_k = -1
    for rank, i in enumerate(order, start=1):
        if pvalues[i] <= alpha * rank / n:
            largest_k = rank
    for rank, i in enumerate(order, start=1):
        if rank <= largest_k:
            keep[i] = True
    return keep


def suggest_range(prices: Sequence[float], coverage: float = 0.90,
                  half_life: Optional[float] = None
                  ) -> Optional[tuple[float, float]]:
    """Propose a band containing `coverage` of recent prices, centred on the median.

    Derived from observed price rather than from what would look
    profitable — the most common way a grid is mis-specified is by drawing the
    range around a desired return instead of around where the asset has traded.

    `half_life`, in bars, weights recent prices more heavily: a bar that far
    back counts half as much as the newest one. None means every bar counts
    equally. See `suggest_range_both`.
    """
    if len(prices) < 20:
        return None
    tail = (1.0 - coverage) / 2.0
    if half_life is None:
        s = sorted(prices)
        lo = s[int(len(s) * tail)]
        hi = s[min(len(s) - 1, int(len(s) * (1.0 - tail)))]
        return (lo, hi) if hi > lo else None

    if half_life <= 0:
        raise ValueError("half_life must be positive, or None for no weighting")
    # Exponentially decayed percentile: the newest bar has weight 1 and a bar
    # `half_life` back has weight 0.5. A band drawn from two years of history
    # describes where the asset used to live; this one describes where it lives.
    n = len(prices)
    decay = 0.5 ** (1.0 / half_life)
    weights = [decay ** (n - 1 - i) for i in range(n)]
    order = sorted(range(n), key=lambda i: prices[i])
    total = sum(weights)
    lo = hi = prices[order[0]]
    cum = 0.0
    lo_set = False
    for i in order:
        cum += weights[i]
        if not lo_set and cum >= total * tail:
            lo, lo_set = prices[i], True
        if cum >= total * (1.0 - tail):
            hi = prices[i]
            break
    return (lo, hi) if hi > lo else None


def suggest_range_both(prices: Sequence[float], coverage: float = 0.90,
                       half_life: Optional[float] = None
                       ) -> tuple[Optional[tuple[float, float]],
                                  Optional[tuple[float, float]]]:
    """(unweighted, recency-weighted) bands for the same prices.

    When they agree the range is settled; when
    they diverge the asset has moved and the unweighted band is describing a
    place it no longer trades. `rubric.assess` records the disagreement so a
    drifting range shows up as a number rather than as a surprise.
    """
    # A quarter of the window, so the newest quarter carries about as much
    # weight as the whole of the rest. A fixed bar count cannot be right for
    # both a 300-bar scan and a 17,000-bar survey.
    hl = half_life if half_life is not None else max(2.0, len(prices) / 4.0)
    return (suggest_range(prices, coverage),
            suggest_range(prices, coverage, half_life=hl))


def expected_cycles_per_day(atr_fraction: float, spacing: float,
                            bars_per_day: float = 1.0) -> float:
    """Rough estimate of completed buy/sell cycles per day.

    Crude by construction — it assumes movement is distributed across levels rather
    than arriving in one jump — but it is the number that converts "this asset is
    volatile" into "this grid earns roughly X per day", which is the only form in
    which volatility is worth anything.
    """
    if spacing <= 0:
        return 0.0
    return max(0.0, (atr_fraction / spacing) * bars_per_day * 0.5)
