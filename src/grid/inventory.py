"""Inventory-aware sizing. The one place the ladder decides HOW MUCH.

THE ASYMMETRY THIS EXISTS TO FIX
================================

A grid is a naive market maker. It earns the spread on every round trip — that
revenue is LINEAR in the number of fills — while carrying inventory costs it
roughly

    (1/2) x gamma x q^2 x sigma^2

which is QUADRATIC in the position q. Two functions, one linear and one
quadratic, cross exactly once. Past that crossing point every additional fill
has negative expected value: the spread collected is smaller than the risk
taken on to collect it.

Placing identical size at every rung implicitly assumes gamma = 0 — risk
neutrality with unlimited capital.

Two corrections, in order of how much they matter:

  1. A HARD BOUND. At maximum inventory, stop quoting the buy side entirely.
     This is the boundary result in Guéant-Lehalle-Fernandez-Tapia: the optimal
     quote on the side that would increase an already-maximal position is no
     quote at all. It is trivial to implement and it is the change that
     prevents the ruinous case rather than merely improving the average one.

  2. SIZE SKEWING. Avellaneda-Stoikov skews the PRICE of quotes toward
     unwinding inventory. A grid cannot do that — its prices are a fixed
     lattice chosen in advance — so the lattice equivalent is to modulate SIZE
     at those fixed prices:

         buy_size_i  = base x max(0, 1 - q/Q_max)
         sell_size_j = base x max(0, 1 + q/Q_max)

     which is linear in q, mean-reverts inventory toward zero, and hits exactly
     zero on the buy side at q = Q_max, so it respects the hard bound for free
     rather than needing a separate check.

See `docs/THEORY.md` for the derivation and citations.

DEFAULTS
========

The hard bound is ON. It is a safety property and there is no version of this
system where running past maximum inventory is correct.

Skewing is OFF by default, because it changes every fill size and its benefit
is a variance reduction that should be measured on my own pairs rather than
assumed. `backtest.run(size_skew=True)` A/Bs it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


#: How much of the ladder's REAL maximum inventory the bound sits at.
#:
#: Not 1.0. At 1.0 the bound equals the most the grid could ever hold, so it is
#: reachable only if every single rung fills — and with the old
#: `max_quote_total / lower` formula, only if every rung filled at exactly the
#: bottom price, which cannot happen. Measured across 29 pairs and 2 500 bars,
#: `bars_at_bound` was zero everywhere: the bound was decorative.
#:
#: 0.6 leaves the grid four tenths of its ladder unused at the bound on
#: purpose: the bound exists to stop the left tail, and a bound that only
#: engages after the grid is fully loaded engages too late to do that.
BOUND_FRACTION = 0.6


def max_inventory_for(plan, *, fraction: float = BOUND_FRACTION) -> float:
    """Q_max in BASE units for one plan.

        Q_max = fraction * sum(quote_per_level / price_i) over all buy levels

    THE REAL MAXIMUM, not a proxy for it. A grid buys `quote_per_level` worth
    at each rung, so the most base it can ever hold is the sum of
    `quote_per_level / price_i` across the ladder. Each term is the units that
    rung's own fill produces, at that rung's own price.

    This replaces `max_quote_total / lower`, which assumed every rung filled at
    the BOTTOM of the range. That overstates the maximum by the ratio of the
    mean rung price to the lowest, so the bound sat above anything reachable
    and never bound. See `docs/THEORY.md`.

    `plan.max_inventory` overrides when set.
    """
    explicit = getattr(plan, "max_inventory", 0.0) or 0.0
    if explicit > 0:
        return explicit

    prices = [p for p in (getattr(plan, "grid_prices", None) or ()) if p > 0]
    if not prices:
        # No ladder to sum over. Fall back to the old proxy rather than
        # returning 0.0, because a Q_max of zero would refuse to buy at all —
        # failing OPEN into "trade nothing" rather than closed into "trade
        # unbounded", but still not what the caller meant.
        if plan.lower <= 0:
            return 0.0
        return (plan.max_quote_total / plan.lower) * max(0.0, fraction)

    total_units = sum(plan.quote_per_level / p for p in prices)
    return total_units * max(0.0, fraction)


def buy_multiplier(q: float, q_max: float, skew: bool = False) -> float:
    """How much of `base_size` to buy, given inventory q.

    Without skewing this is the boundary rule alone: full size until Q_max,
    nothing at or past it. With skewing it ramps linearly to zero at Q_max,
    which subsumes the boundary.
    """
    if q_max <= 0:
        return 0.0
    if q >= q_max:
        return 0.0                      # THE BOUNDARY. Do not quote this side.
    if not skew:
        return 1.0
    return max(0.0, 1.0 - q / q_max)


def sell_multiplier(q: float, q_max: float, skew: bool = False) -> float:
    """How much of `base_size` to offer, given inventory q.

    Skewed, this grows with inventory: the longer the position, the harder it works
    to get flat. It is capped at 2.0 (reached at q = Q_max) so a single rung
    can never try to sell more than twice its share — the cap matters because
    the formula is unbounded above if Q_max is ever misconfigured small.
    """
    if not skew:
        return 1.0
    if q_max <= 0:
        return 1.0
    return max(0.0, min(2.0, 1.0 + q / q_max))


@dataclass(frozen=True)
class InventoryState:
    """What the sizing decided, and why — for the UI and the tests."""

    q: float
    q_max: float
    at_bound: bool
    buy_mult: float
    sell_mult: float
    skew: bool

    @property
    def utilisation(self) -> float:
        return (self.q / self.q_max) if self.q_max > 0 else 0.0

    def explain(self) -> str:
        if self.at_bound:
            return (f"AT MAX INVENTORY: {self.q:.6g} of {self.q_max:.6g} base "
                    f"units. The buy side is not quoted — past this point each "
                    f"further fill costs more in inventory risk than the "
                    f"spread pays.")
        return (f"inventory {self.q:.6g} / {self.q_max:.6g} "
                f"({self.utilisation:.0%})"
                + (f", sizing buys x{self.buy_mult:.2f} and sells "
                   f"x{self.sell_mult:.2f}" if self.skew else ""))


def assess(q: float, q_max: float, skew: bool = False) -> InventoryState:
    return InventoryState(
        q=q, q_max=q_max, at_bound=(q_max > 0 and q >= q_max),
        buy_mult=buy_multiplier(q, q_max, skew),
        sell_mult=sell_multiplier(q, q_max, skew), skew=skew)
