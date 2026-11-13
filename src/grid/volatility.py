"""Forecasting volatility, and scaling the ladder by it.

WHY THIS AND NOT A RETURN FORECAST
==================================

Fixed spacing is a bet that volatility is constant. It is not: crypto realised
volatility moves by multiples over weeks, and a ladder spaced for a calm month
is either idle or overrun in a violent one.

Volatility is also the one thing in this literature that is genuinely
forecastable. Realised volatility is strongly autocorrelated and its
out-of-sample R-squared is large; expected RETURNS at these horizons are not
forecastable to any useful degree, and the gap between the two is not close.
That asymmetry is the whole justification for this module, and it is also its
boundary:

    **USED FOR SIZING AND SPACING ONLY. NEVER AS A DIRECTIONAL SIGNAL.**

A volatility forecast says how far price will move, not which way. Nothing here
returns a direction, and `rubric` never consults it to decide whether an asset
is worth trading — only how wide to space a ladder on one that already passed.

TWO ESTIMATORS
==============

HAR-RV (Corsi 2009) regresses tomorrow's realised volatility on its own daily,
weekly and monthly averages. It is three OLS coefficients, it captures the
long-memory behaviour of volatility without a long-memory model, and it is
hard to beat. It is the default here because it degrades gracefully: with
little data it is still a sensible weighted average.

GARCH(1,1) (Bollerslev 1986) is offered via `arch` when installed. It usually
performs comparably to HAR on daily data and is more fiddly to fit, so it is
the alternative rather than the default.

Both are floored by the fee-based minimum spacing. A volatility forecast can
suggest a spacing tighter than the fees allow; the fee floor is a structural
gate and wins every time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

HAR = "har"
GARCH = "garch"

#: Bounds on how far a forecast may move the spacing. A forecast is an
#: estimate, and an estimate that wants to quadruple the grid interval is
#: more likely to be a broken fit than a real regime.
MIN_SCALE = 0.5
MAX_SCALE = 3.0


@dataclass(frozen=True)
class VolForecast:
    """A one-step-ahead volatility forecast, per bar, as a fraction."""

    sigma: float                 # forecast volatility per bar
    baseline: float              # realised volatility over the window
    model: str
    n_obs: int
    ok: bool = True
    note: str = ""

    @property
    def ratio(self) -> float:
        """Forecast relative to the recent average. 1.0 is 'as usual'."""
        return (self.sigma / self.baseline) if self.baseline > 0 else 1.0


def _returns(prices: Sequence[float]) -> list[float]:
    out = []
    for i in range(1, len(prices)):
        a, b = prices[i - 1], prices[i]
        if a > 0 and b > 0:
            out.append(math.log(b / a))
    return out


def realised_vol(prices: Sequence[float], window: Optional[int] = None
                 ) -> Optional[float]:
    """Standard deviation of log returns over the last `window` bars."""
    r = _returns(prices)
    if window:
        r = r[-window:]
    if len(r) < 5:
        return None
    m = sum(r) / len(r)
    var = sum((x - m) ** 2 for x in r) / (len(r) - 1)
    return math.sqrt(max(0.0, var))


def har_forecast(prices: Sequence[float], bars_per_day: float = 24.0
                 ) -> Optional[VolForecast]:
    """HAR-RV: regress realised vol on its daily, weekly and monthly averages.

    Corsi (2009). Implemented directly rather than pulled in from a library —
    it is an OLS on three columns, and the explicitness matters more here than
    the saving.

    ONE KNOWN DEGENERACY: on a series of genuinely CONSTANT volatility the three
    regressors are collinear, the least-squares fit is not identified, and the
    minimum-norm solution can return a forecast well below the realised level
    (measured: 0.69x on a synthetic constant-vol series). Real price series are
    heteroskedastic enough that this does not arise — the live OKX pairs return
    ratios of 0.80-1.20 — but a forecast far below 1.0 on data that looks
    calm is this artefact, not a signal. `MIN_SCALE` bounds the damage.
    """
    try:
        import numpy as np
    except ImportError:
        return None
    r = np.asarray(_returns(prices), dtype=float)
    d = max(1, int(round(bars_per_day)))
    w, m = d * 7, d * 30
    if len(r) < m + d + 5:
        return None

    # Realised vol over each horizon, ending at each point in time.
    sq = r ** 2
    def rv(n: int) -> "np.ndarray":
        c = np.cumsum(np.insert(sq, 0, 0.0))
        return np.sqrt(np.maximum(0.0, (c[n:] - c[:-n]) / n))

    rv_d, rv_w, rv_m = rv(d), rv(w), rv(m)
    k = min(len(rv_d), len(rv_w), len(rv_m)) - 1
    if k < 30:
        return None
    X = np.column_stack([np.ones(k), rv_d[-k - 1:-1], rv_w[-k - 1:-1],
                         rv_m[-k - 1:-1]])
    y = rv_d[-k:]
    try:
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    except Exception:
        return None
    nxt = float(beta[0] + beta[1] * rv_d[-1] + beta[2] * rv_w[-1]
                + beta[3] * rv_m[-1])
    base = float(rv_m[-1])
    if not math.isfinite(nxt) or nxt <= 0:
        return VolForecast(sigma=base, baseline=base, model=HAR, n_obs=k,
                           ok=False, note="HAR produced a non-positive forecast")
    return VolForecast(sigma=nxt, baseline=base, model=HAR, n_obs=k)


def garch_forecast(prices: Sequence[float]) -> Optional[VolForecast]:
    """GARCH(1,1) via `arch`, if it is installed. Bollerslev (1986)."""
    try:
        import numpy as np
        from arch import arch_model
    except ImportError:
        return None
    r = np.asarray(_returns(prices), dtype=float)
    if len(r) < 250:
        return None
    scaled = r * 100.0          # arch prefers percent returns numerically
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = arch_model(scaled, vol="Garch", p=1, q=1, mean="Zero",
                             rescale=False).fit(disp="off", show_warning=False)
            f = res.forecast(horizon=1, reindex=False)
            sigma = float(np.sqrt(f.variance.values[-1, 0])) / 100.0
    except Exception as e:
        return VolForecast(sigma=0.0, baseline=0.0, model=GARCH, n_obs=len(r),
                           ok=False, note=f"{type(e).__name__}: {e}")
    base = float(np.std(r, ddof=1))
    if not math.isfinite(sigma) or sigma <= 0:
        return VolForecast(sigma=base, baseline=base, model=GARCH,
                           n_obs=len(r), ok=False,
                           note="GARCH produced a non-positive forecast")
    return VolForecast(sigma=sigma, baseline=base, model=GARCH, n_obs=len(r))


def forecast(prices: Sequence[float], model: str = HAR,
             bars_per_day: float = 24.0) -> Optional[VolForecast]:
    if model == GARCH:
        return garch_forecast(prices) or har_forecast(prices, bars_per_day)
    return har_forecast(prices, bars_per_day)


def scaled_spacing(base_spacing: float, fc: Optional[VolForecast], *,
                   floor: float, min_scale: float = MIN_SCALE,
                   max_scale: float = MAX_SCALE) -> tuple[float, str]:
    """Scale the grid interval by the volatility forecast, floored by fees.

    Returns (spacing, why). The fee floor is a STRUCTURAL gate: spacing that
    cannot clear the fees loses on every cycle regardless of how quiet the
    market is, so a forecast is never allowed to reach below it.
    """
    if fc is None or not fc.ok or fc.baseline <= 0:
        return max(base_spacing, floor), "no usable volatility forecast"
    scale = max(min_scale, min(max_scale, fc.ratio))
    want = base_spacing * scale
    if want < floor:
        return floor, (f"volatility forecast wanted {want:.3%} "
                       f"({scale:.2f}x) but the fee floor is {floor:.3%} — "
                       f"the floor wins, it is structural")
    return want, (f"volatility forecast {fc.sigma:.3%}/bar against a "
                  f"{fc.baseline:.3%} baseline, scaling spacing {scale:.2f}x "
                  f"({fc.model.upper()}, {fc.n_obs} observations)")
