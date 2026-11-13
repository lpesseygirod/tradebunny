"""Fee model — the constraint that decides whether a grid is viable at all.

Every buy/sell cycle must clear the exchange's cut twice. That single requirement
sets a hard floor on grid spacing, and most grid bots that lose money lose it here:
the spacing looked profitable on a chart and wasn't, once fees applied on both legs.

Derivation. Buy quantity Q at price B, sell at price S, fee rates f_b and f_s
charged on trade value:

    cash out  = Q * B * (1 + f_b)
    cash in   = Q * S * (1 - f_s)
    profit > 0   <=>   S * (1 - f_s) > B * (1 + f_b)
                 <=>   S / B > (1 + f_b) / (1 - f_s)

So the minimum gross spacing between two grid levels is

    g_min = (1 + f_b) / (1 - f_s) - 1

which for OKX spot at base tier is 0.160% all-maker, 0.200% all-taker.

Spot only. Margin and perpetual futures also charge borrowing
interest and funding, typically charged every 8 hours. At ±0.01% per funding
period that is roughly 11% annualised, which dwarfs trading fees and will quietly
eat a grid alive. A spot grid has no interest component at all, which is why this
module models spot and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass

# OKX spot, base (non-VIP) tier, verified September 2026.
OKX_SPOT_MAKER = 0.0008   # 0.08%
OKX_SPOT_TAKER = 0.0010   # 0.10%


@dataclass(frozen=True)
class FeeModel:
    """Fee rates as fractions of trade value (0.0008 == 0.08%)."""

    maker: float = OKX_SPOT_MAKER
    taker: float = OKX_SPOT_TAKER

    def __post_init__(self) -> None:
        for name, rate in (("maker", self.maker), ("taker", self.taker)):
            if not 0.0 <= rate < 0.5:
                raise ValueError(f"{name} fee {rate!r} outside [0, 0.5)")

    # ------------------------------------------------------------------ legs

    def buy_cost(self, price: float, qty: float, maker: bool = True) -> float:
        """Total cash leaving the account to acquire `qty` at `price`."""
        return price * qty * (1.0 + (self.maker if maker else self.taker))

    def sell_proceeds(self, price: float, qty: float, maker: bool = True) -> float:
        """Total cash arriving after selling `qty` at `price`."""
        return price * qty * (1.0 - (self.maker if maker else self.taker))

    # ------------------------------------------------------------ constraint

    def breakeven_spacing(self, maker: bool = True) -> float:
        """Minimum gross spacing, as a fraction, for a cycle to break even.

        A grid bot places resting limit orders, which fill as MAKER orders, so
        `maker=True` is the realistic case. Use `maker=False` only if the bot
        crosses the spread.
        """
        f = self.maker if maker else self.taker
        return (1.0 + f) / (1.0 - f) - 1.0

    def min_viable_spacing(self, fee_share_of_gross: float = 0.25,
                           maker: bool = True) -> float:
        """Spacing at which fees consume no more than `fee_share_of_gross`.

        Break-even is not a target — a grid running at break-even spacing does
        an enormous amount of work for nothing and loses on any slippage. The
        default caps fees at a quarter of gross profit, which is the point where
        the strategy has room to survive imperfect fills.

        Solved exactly rather than approximated. `breakeven / share` is close but
        always lands slightly ABOVE the cap it promises (0.25 -> an actual 25.12%),
        which is the wrong side to be wrong on for a limit.
        """
        if not 0.0 < fee_share_of_gross < 1.0:
            raise ValueError("fee_share_of_gross must be in (0, 1)")
        # fee_share(s) = k  =>  s = (1 - r) / (k - (1 - r)) with r = (1-f)/(1+f)
        f = self.maker if maker else self.taker
        r = (1.0 - f) / (1.0 + f)
        den = fee_share_of_gross - (1.0 - r)
        if den <= 0:
            raise ValueError(
                f"fee_share_of_gross {fee_share_of_gross} is unreachable at these fees")
        return (1.0 - r) / den

    def cycle_profit_pct(self, spacing: float, maker: bool = True) -> float:
        """Net profit of one buy-low/sell-high cycle, as a fraction of capital.

        `spacing` is the gross gap between the buy and sell level (0.008 == 0.8%).
        Returns a negative number when the spacing cannot clear the fees.
        """
        f = self.maker if maker else self.taker
        return (1.0 + spacing) * (1.0 - f) / (1.0 + f) - 1.0

    def fee_share(self, spacing: float, maker: bool = True) -> float:
        """What proportion of gross profit the exchange takes at this spacing.

        Above ~0.35 the strategy is mostly working for the exchange.
        """
        if spacing <= 0:
            raise ValueError("spacing must be positive")
        net = self.cycle_profit_pct(spacing, maker)
        return (spacing - net) / spacing

    def cycles_to_recover(self, drawdown_pct: float, spacing: float,
                          maker: bool = True) -> float:
        """How many winning cycles it takes to recover a loss of this size.

        A 20% drawdown at 0.8%
        spacing needs roughly 31 clean cycles to undo. This is the number that
        makes the case for a stop-loss.
        """
        net = self.cycle_profit_pct(spacing, maker)
        if net <= 0:
            return float("inf")
        return drawdown_pct / net


def describe(model: FeeModel | None = None) -> str:
    """Human-readable summary of what the fee model implies. Used by the CLI."""
    m = model or FeeModel()
    be = m.breakeven_spacing()
    viable = m.min_viable_spacing()
    lines = [
        f"Fees: {m.maker:.4%} maker, {m.taker:.4%} taker (spot)",
        f"Break-even spacing (maker both legs): {be:.4%}",
        f"Minimum viable spacing (fees <= 25% of gross): {viable:.4%}",
        "",
        "Spacing    net/cycle   fees as % of gross",
    ]
    for s in (0.002, 0.004, 0.006, 0.008, 0.010, 0.015, 0.020):
        lines.append(f"  {s:6.2%}    {m.cycle_profit_pct(s):+7.4%}    "
                     f"{m.fee_share(s):6.1%}")
    return "\n".join(lines)
