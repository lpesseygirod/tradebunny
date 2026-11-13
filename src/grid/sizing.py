"""How much to commit, and — more usefully — WHY that number.

Four separate limits decide a position size, and the useful output is not the
number but which one bound. A size held down by the concurrency cap means "add
capital or run fewer grids"; the same number held down by negative Kelly means
"do not take this trade at all". They look identical if only the total is printed.

    kelly           what the edge justifies, if the edge estimate is right
    risk            what a stop-out may cost, from analyst.size_grid
    exposure cap    control.json's max_total_quote
    concurrency     an equal share across max_concurrent_grids

and beneath all of them a FLOOR: the minimum the exchange will actually accept.
A size under that is not a small position, it is a rejected order.

ON KELLY
========

Kelly maximises long-run log wealth GIVEN that the win rate, the win size and
the loss size are known exactly. None of the three is known here. `p` is a
confidence score that has never been calibrated against an outcome, `win_pct`
is a walk-forward return from one 60/40 split, and `loss_pct` is a modelled
stop-out. Full Kelly on estimates that soft is a recipe for ruin — the
criterion is famously unforgiving of an overstated `p`, and overstating `p` is
exactly what an uncalibrated confidence score does.

So `kelly_fraction` returns the full number, because hiding it would be worse,
and `KELLY_MULTIPLIER` cuts it to a quarter for anything acted on. Quarter
Kelly gives up about a quarter of the theoretical growth rate in exchange for
roughly halving the volatility, and it survives a `p` that is wrong by a
material margin, which this one may well be.

A NEGATIVE KELLY IS NOT A SMALL POSITION
========================================

When `f <= 0` the arithmetic is saying the bet has negative expectation: at
this win rate and this payoff, betting anything loses money in the long run,
and the mathematically correct size is zero.

The candidate is still SHOWN, because taking it off the page would be deciding
for me rather than informing me — and because `p` here is an
uncalibrated confidence score, so a negative `f` is itself an estimate. What
must not happen is a positive number appearing without the reason: so
`kelly_full` keeps its negative sign, `negative_kelly` is set, and
NEGATIVE_KELLY_WARNING goes in `warnings` for the caller to render somewhere
unmissable. The size shown is the minimum of the OTHER constraints — a cap,
explicitly not a recommendation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .plan import GridPlan

#: Fraction of full Kelly to actually act on. See the module docstring.
KELLY_MULTIPLIER = 0.25

#: Safety multiple over the bare exchange minimum. 1.0 would mean every order
#: sits exactly on the minimum, and one adverse tick of rounding rejects it.
MIN_CAPITAL_SAFETY = 1.5

#: Sizing modes. See `calibration_size`.
KELLY_MODE = "kelly"
CALIBRATION_MODE = "calibration"
SIZING_MODES = (KELLY_MODE, CALIBRATION_MODE)

CALIBRATION_LABEL = (
    "CALIBRATION RUN — sized to be runnable, not optimal. At this capital the "
    "goal is measuring execution quality (fill rate, slippage, fee accuracy, "
    "uptime), not returns. P&L at this size is noise.")

NEGATIVE_KELLY_WARNING = (
    "KELLY SAYS DO NOT TAKE THIS — at the estimated win rate and payoff this "
    "grid has negative expectation, and the mathematically correct size is "
    "zero. Any number shown below is a cap, not a recommendation.")


def kelly_fraction(win_pct: float, loss_pct: float, p: float) -> float:
    """Full Kelly. May be negative, and a negative answer is the useful one.

        b = win_pct / loss_pct
        f = (p * b - (1 - p)) / b

    `win_pct` and `loss_pct` are magnitudes, both positive: what a win gains
    and what a loss costs, as fractions of the stake. `p` is the probability of
    the win.

    Returns the FULL fraction. Apply `KELLY_MULTIPLIER` before acting on it.
    """
    if loss_pct <= 0:
        raise ValueError("loss_pct must be positive — a bet that cannot lose "
                         "is not a bet, and Kelly divides by this")
    if win_pct <= 0:
        # No upside: the bet is pure loss and Kelly's answer is "less than
        # nothing". Return the negative rather than raising, so callers
        # display the warning instead of crashing.
        return -1.0
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"p must be a probability in [0, 1], got {p!r}")
    b = win_pct / loss_pct
    return (p * b - (1.0 - p)) / b


def min_viable_capital(plan: GridPlan, price: float, min_size: float,
                       safety: float = MIN_CAPITAL_SAFETY) -> float:
    """Smallest total capital that can actually place this ladder.

        levels x minSz x price x safety

    `min_size` is in BASE units, which is how OKX quotes it, so the price is
    what converts it to money. Counts every rung including both endpoints —
    understating the requirement is the wrong direction to be wrong in.
    """
    if price <= 0 or min_size <= 0:
        raise ValueError("price and min_size must be positive")
    return len(plan.grid_prices) * min_size * price * safety


@dataclass(frozen=True)
class CalibrationSize:
    """A ladder sized to be PLACEABLE, not to be optimal.

    Kelly answers "how much should I bet to grow fastest?". At 50 EUR that is
    the wrong question — the answer is "less than the exchange will accept" —
    and the right one is "what is the smallest thing that will actually run,
    so I can measure whether the machinery works?".

    This does not weaken Kelly. It sits beside it, is selected explicitly in
    config, and every proposal it produces still shows what Kelly would have
    said, including when Kelly says do not trade at all.
    """

    quote_per_level: float
    levels: int
    total: float
    feasible: bool
    reason: str = ""
    min_size_quote: float = 0.0

    def explain(self) -> str:
        if not self.feasible:
            return f"not runnable at this capital: {self.reason}"
        return (f"{self.levels} levels x {self.quote_per_level:.2f} = "
                f"{self.total:.2f} (minimum order {self.min_size_quote:.2f})")


def calibration_size(capital: float, min_size_quote: float,
                     levels_that_fit: int, safety: float = MIN_CAPITAL_SAFETY
                     ) -> CalibrationSize:
    """Size by feasibility.

        quote_per_level = minSz_in_quote x 1.5
        levels          = min(levels_that_fit_the_range,
                              capital / quote_per_level)

    Total deployed is capped at `capital`. If even two levels cannot be funded
    the answer is that this pair cannot be calibrated at this size — which is
    useful information, not a failure, and is why `feasible` exists.
    """
    if capital <= 0:
        return CalibrationSize(0.0, 0, 0.0, False, "no capital available")
    if min_size_quote <= 0:
        return CalibrationSize(0.0, 0, 0.0, False,
                               "no minimum order size known for this pair")
    if levels_that_fit < 2:
        return CalibrationSize(0.0, 0, 0.0, False,
                               "the range does not fit two levels")

    per_level = min_size_quote * safety
    affordable = int(capital // per_level)
    levels = min(levels_that_fit, affordable)
    if levels < 2:
        return CalibrationSize(
            per_level, 0, 0.0, False,
            f"{capital:.2f} funds {affordable} level(s) at {per_level:.2f} "
            f"each — a grid needs at least 2, so this pair cannot be "
            f"calibrated at this capital. Try one with a smaller minimum "
            f"order size.")
    return CalibrationSize(quote_per_level=per_level, levels=levels,
                           total=per_level * levels, feasible=True,
                           min_size_quote=min_size_quote)


@dataclass(frozen=True)
class SizeRecommendation:
    """A number, every constraint that produced it, and which one bound."""

    recommended: float
    binding: str
    constraints: dict[str, float] = field(default_factory=dict)
    kelly_full: float = 0.0
    kelly_quarter: float = 0.0
    #: What a stop-out costs, as a fraction of what is deployed. The divisor
    #: that turns Kelly's fraction-at-risk into an amount.
    loss_fraction: float = 0.0
    negative_kelly: bool = False
    min_capital: float = 0.0
    available: float = 0.0
    fundable: bool = True
    warnings: tuple[str, ...] = ()

    def explain(self) -> str:
        lines = [f"recommended {self.recommended:.2f} "
                 f"(bound by: {self.binding})"]
        for name, value in sorted(self.constraints.items(), key=lambda kv: kv[1]):
            mark = "  <-- binds" if name == self.binding else ""
            lines.append(f"    {name:22} {value:12.2f}{mark}")
        lines.append(f"    {'minimum viable':22} {self.min_capital:12.2f}"
                     f"{'  <-- NOT MET' if not self.fundable else ''}")
        lines.append(f"    {'capital available':22} {self.available:12.2f}")
        lines.append(f"  Kelly full {self.kelly_full:+.3f}, "
                     f"quarter {self.kelly_quarter:+.3f}")
        if self.loss_fraction > 0:
            lines.append(f"  a stop-out costs {self.loss_fraction:.1%} of what "
                         f"is deployed, so {self.kelly_quarter:+.3f} at risk "
                         f"means {self.kelly_quarter / self.loss_fraction:+.3f} "
                         f"deployed")
        for w in self.warnings:
            lines.append(f"  {w}")
        return "\n".join(lines)


def recommend(*, available_quote: float, risk_size: float, kelly_full: float,
              max_total_quote: float, max_concurrent_grids: int,
              min_capital: float, loss_fraction: float) -> SizeRecommendation:
    """Combine every limit and report which one bound.

    KELLY IS A FRACTION AT RISK, NOT AN AMOUNT DEPLOYED. I had this wrong at
    first and it cost a factor of ten.

    In a classic bet, losing means losing the whole stake, so "risk f* of the
    bankroll" and "stake f* of the bankroll" are the same instruction. A grid
    is not that bet: when the stop fires it loses `loss_fraction` of what is
    deployed — about 10% — not all of it. So deploying `f* x capital` puts only
    `f* x loss_fraction x capital` at risk, which at a 10% stop distance is a
    tenth of the intended bet.

        capital_at_risk = quarter_kelly x available_quote
        deployed        = capital_at_risk / loss_fraction

    With quarter Kelly 0.02, capital 2000 and a 10% loss fraction: 40 at risk,
    400 deployed. The previous code deployed 40 and risked 4.

    The recommendation is the MINIMUM of the four caps, then checked against
    the exchange floor. It is never raised to meet that floor — if the smallest
    placeable ladder costs more than the caps allow, the answer is that this
    grid cannot be run at this capital, not that the caps should be ignored.
    """
    warnings: list[str] = []
    negative = kelly_full <= 0.0
    if negative:
        warnings.append(NEGATIVE_KELLY_WARNING)

    if loss_fraction <= 0:
        raise ValueError("loss_fraction must be positive — it converts Kelly's "
                         "fraction-at-risk into an amount to deploy")

    kelly_quarter = kelly_full * KELLY_MULTIPLIER
    constraints = {
        "risk-based": max(0.0, risk_size),
        "exposure cap": max(0.0, max_total_quote),
        "concurrency share": (max(0.0, available_quote / max_concurrent_grids)
                              if max_concurrent_grids > 0 else available_quote),
    }
    # A negative Kelly contributes no positive cap — it is a reason not to
    # trade, not a size. Including it as 0 would make it "bind" and hide
    # whichever real constraint is also biting.
    if not negative:
        capital_at_risk = kelly_quarter * available_quote
        constraints["quarter Kelly"] = max(0.0, capital_at_risk / loss_fraction)

    binding = min(constraints, key=lambda k: constraints[k])
    recommended = constraints[binding]
    if negative:
        # NOT zeroed. A negative Kelly is a loud warning, not a silent
        # rejection: I still want to see these, because
        # "the arithmetic says don't" is information I should weigh rather
        # than have taken away. The other constraints still cap the number, and
        # `negative_kelly` drives the label.
        binding = f"{binding} (Kelly negative — see warning)"

    fundable = recommended >= min_capital and min_capital > 0
    if not fundable:
        warnings.append(
            f"the smallest placeable ladder needs {min_capital:.2f} but the "
            f"binding constraint ({binding}) allows only {recommended:.2f} — "
            f"this grid cannot be run at this capital")
        recommended = 0.0

    return SizeRecommendation(
        recommended=recommended, binding=binding, constraints=constraints,
        kelly_full=kelly_full, kelly_quarter=kelly_quarter,
        loss_fraction=loss_fraction,
        negative_kelly=negative, min_capital=min_capital,
        available=available_quote, fundable=fundable,
        warnings=tuple(warnings))


def order_size_problems(plan: GridPlan, price: float,
                        instrument: Optional[Any]) -> list[str]:
    """Is every order on this ladder large enough for the exchange to accept?

    THE GATE. Not graded, not weighted: an order below `minSz` is rejected by
    OKX, so a plan built from them cannot trade at all. This was previously
    approximated by a flat 10 USDT floor, which is wrong for any instrument
    whose minimum is larger — ZEC-USDT's is 12.25 USDT.
    """
    if instrument is None:
        return [f"no instrument specification for {plan.symbol} — cannot "
                f"confirm the exchange will accept these order sizes"]
    if price <= 0:
        return [f"no usable price for {plan.symbol}"]

    problems: list[str] = []
    # The SMALLEST order on the ladder is at the highest rung: a fixed quote
    # amount buys fewest units where price is greatest.
    top = max(plan.grid_prices)
    units_at_top = plan.quote_per_level / top
    if units_at_top < instrument.min_size:
        needed = instrument.min_size * top
        problems.append(
            f"{plan.quote_per_level:.2f} per level buys {units_at_top:.8g} "
            f"{instrument.base or 'units'} at the top of the ladder ({top:.6g}), "
            f"below the exchange minimum of {instrument.min_size:g} — every "
            f"order at that rung would be rejected. Needs at least "
            f"{needed:.2f} per level.")
    if not instrument.is_live:
        problems.append(
            f"{plan.symbol} is not live on the exchange (state "
            f"{instrument.state!r})")
    return problems
