"""Replay a GridPlan over historical closes.

A grid backtest is unusually easy to make lie, and every lie flatters the
result. The three that matter, and what this module does about them:

  1. **Intraday fills that never happened.** The tempting version of this code checks
     whether a candle's LOW touched a buy level and books the fill. But a low is
     a single print — it says the price traded there, not that a resting
     order was at the front of the queue when it did. On a wick through a level
     the only fair answer is "possibly". Booking it as a fill invents cycles, and
     cycles are the entire product here. **This module reads closing prices and
     nothing else.** `run()` does not accept highs or lows, so the mistake is
     not available. Fewer cycles will be counted than a real grid would have
     earned; that is the correct direction to be wrong in.

  2. **Cherry-picked lot pairing.** When price crosses up through a level and
     several lots are open below it, selling the CHEAPEST one books the biggest
     profit. It is also not what a grid does: each level's sell order is paired
     with the buy directly beneath it. This module always closes the
     highest-priced open lot below the sell level — the nearest one, the
     smallest profit available, and the one that actually corresponds to a
     resting order.

  3. **Free capital.** A grid that keeps buying through a decline needs the cash
     to do it. Starting cash is `plan.max_quote_total` and a buy that cannot be
     funded simply does not happen, exactly as it would not on the exchange.

The stop is honoured at the close that breaches it, not at `stop_below` itself.
Price gaps; assuming an exit at the stop price is the fourth lie.

THREE MORE, all about execution quality rather than about prices:

  5. **Slippage.** A limit order does not fill at exactly its price once the tick
     crossed to get filled and the fee-adjusted quote are counted.
     `slip` moves every buy up and every sell down, which is the direction that
     costs money on both legs of a cycle. At the default 5bps a round trip
     gives up 10bps — comparable to the maker fee itself, so a backtest that
     ignores it is out by roughly the whole fee model.

  6. **Bare touches.** A close that lands exactly on a level does not mean the
     order filled: it was somewhere in a queue. `fill_margin` requires the
     close to cross the level by a margin before the fill is booked.

  7. **Gaps.** A bar that sweeps several levels at once is the case where a
     backtest can invent the most cycles. `max_fills_per_bar` caps how many
     levels one bar may fill, and the one it picks is the level nearest the
     close — the last one price reached, not the most profitable.

     Caveat: resting buy limits DO all sit on the book
     simultaneously, so a genuine sweep through five levels would fill all
     five. Capping at one is therefore pessimistic rather than accurate. It is
     the default because the cost of being wrong in that direction is a grid
     that looks worse than it is, and the cost the other way is a grid that
     looks better. Set `max_fills_per_bar=None` for the unrestricted view.

Pure: no network, no clock, no file I/O, no randomness. Same inputs, same
numbers, forever.

All ratios are FRACTIONS, not percentages — 0.05 means 5%, matching `fees.py`
and `rubric.py`. The `_pct` suffixes are kept because they name the quantity,
not its scale.
"""

from __future__ import annotations
import datetime as _dt
import math


from dataclasses import dataclass
from typing import Any, Optional, Sequence

from . import inventory, metrics
from .fees import FeeModel
from .plan import ARITHMETIC, GEOMETRIC, GridPlan

#: Price concession per leg, as a fraction. 5bps each way = 10bps a round trip.
DEFAULT_SLIP = 0.0005

#: How far past a level the close must land before the fill is believed.
#: A judgement call: about the scale of a spread on a liquid pair, so a bare
#: touch is not counted but a real crossing is.
DEFAULT_FILL_MARGIN = 0.0002

#: Levels one bar may fill. See rule 7 above; pessimistic on purpose.
DEFAULT_MAX_FILLS_PER_BAR = 1

#: How many days of recent history the BAND is drawn from, when walk_forward is
#: not told otherwise. A grid trades the band it is in NOW, so the range comes
#: from a short recent window even though the asset's CHARACTER is judged on a
#: long one. See the "TWO WINDOWS" comment in `walk_forward`.
DEFAULT_RANGE_DAYS = 45.0


@dataclass(frozen=True)
class BacktestResult:
    """What replaying a plan produced. Immutable — a result is evidence."""

    total_return_pct: float        # fraction: final equity vs starting capital
    max_drawdown_pct: float        # fraction: worst peak-to-trough on the curve
    n_cycles: int                  # completed buy-then-sell round trips
    fees_paid: float               # quote currency, every leg including the stop
    gross_profit: float            # quote currency, completed cycles, before fees
    fees_as_pct_of_gross: float    # fraction; comparable to FeeModel.fee_share()
    days: float                    # length of the replay
    stopped_out: bool              # the stop fired and the grid was liquidated
    final_units_held: float        # base currency still held at the end
    equity_curve: tuple[float, ...]  # cash + marked inventory, one point per bar
    #: The fee rates this replay assumed. A return of +2% means nothing without
    #: them — the same series on a 2.5x fee tier can be a loss.
    maker_fee: float = 0.0
    taker_fee: float = 0.0
    #: Execution assumptions. A return quoted without these is not a number.
    slip: float = 0.0
    fill_margin: float = 0.0
    max_fills_per_bar: Optional[int] = None
    #: Inventory-aware sizing: the bound, whether skewing was on, and how many
    #: bars were spent refusing to buy because the bound was reached.
    max_inventory: float = 0.0
    size_skew: bool = False
    bars_at_bound: int = 0
    peak_inventory: float = 0.0
    #: Bars whose close sat outside [lower, upper]. THE MEASURE OF BAND
    #: STALENESS: a band fitted once and held has price walk out of it and stay
    #: out, and every bar spent outside is a bar the ladder could not trade.
    bars_outside_band: int = 0
    bars_replayed: int = 0
    #: True when `exit_signal` ended the replay, as opposed to the price stop.
    signalled_exit: bool = False
    #: Every fill, with the time its order rested — the input to markout.py.
    fills: tuple = ()

    @property
    def net_profit(self) -> float:
        """Quote currency gained or lost, marked to the final close."""
        if not self.equity_curve:
            return 0.0
        return self.equity_curve[-1] - self.equity_curve[0]

    def to_metrics(self) -> dict[str, float]:
        """Flat floats, for `GridPlan.backtest` (which is a dict[str, float])."""
        return {
            "total_return_pct": self.total_return_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "n_cycles": float(self.n_cycles),
            "fees_paid": self.fees_paid,
            "gross_profit": self.gross_profit,
            "fees_as_pct_of_gross": self.fees_as_pct_of_gross,
            "days": self.days,
            "stopped_out": float(self.stopped_out),
            "final_units_held": self.final_units_held,
            "maker_fee": self.maker_fee,
            "taker_fee": self.taker_fee,
            "slip": self.slip,
            "fill_margin": self.fill_margin,
            "max_fills_per_bar": float(self.max_fills_per_bar or 0),
        }

    def explain(self) -> str:
        head = "STOPPED OUT" if self.stopped_out else "ran to the end"
        return (
            f"{head} after {self.days:.1f}d · {self.n_cycles} cycles\n"
            f"return {self.total_return_pct:+.2%} · max drawdown "
            f"{self.max_drawdown_pct:.2%}\n"
            f"gross {self.gross_profit:+.2f} · fees {self.fees_paid:.2f} "
            f"({self.fees_as_pct_of_gross:.1%} of gross)\n"
            f"assumed {self.maker_fee:.3%} maker / {self.taker_fee:.3%} taker, "
            f"{self.slip:.2%} slip"
        )


def _empty(days: float, start_equity: float,
           fees: Optional[FeeModel] = None) -> BacktestResult:
    fees = fees or FeeModel()
    return BacktestResult(
        total_return_pct=0.0, max_drawdown_pct=0.0, n_cycles=0, fees_paid=0.0,
        gross_profit=0.0, fees_as_pct_of_gross=0.0, days=days, stopped_out=False,
        final_units_held=0.0, equity_curve=(start_equity,) if start_equity else (),
        maker_fee=fees.maker, taker_fee=fees.taker,
    )


def run(plan: GridPlan, closes: Sequence[float], *, bars_per_day: float,
        fees: Optional[FeeModel] = None,
        liquidation_maker: bool = False,
        slip: float = DEFAULT_SLIP,
        fill_margin: float = DEFAULT_FILL_MARGIN,
        max_fills_per_bar: Optional[int] = DEFAULT_MAX_FILLS_PER_BAR,
        size_skew: bool = False,
        max_inventory: Optional[float] = None,
        level_weights: Optional[Sequence[float]] = None,
        exit_signal: Optional[Sequence[bool]] = None) -> BacktestResult:
    """Replay `plan` over `closes` and report what it would have done.

        >>> run(plan, hourly_closes, bars_per_day=24.0).explain()

    `closes` must be in chronological order — oldest first, which is what
    `okx.get_candles` returns. Only closes are accepted: see the module
    docstring for why highs and lows are deliberately not a parameter.

    `bars_per_day` has no default, for the same reason `rubric.assess` has none:
    the caller is the only one who knows what interval these bars are, and a
    silent guess would misreport the horizon by 24x.

    `liquidation_maker` is False because a stop-out is not a patient order. The
    grid's own legs are resting limits and pay the maker fee; the forced exit
    on a breakout crosses the spread and pays taker. Set it True only to see
    how much that assumption costs.

    `level_weights` sizes each rung separately. One multiplier per ladder
    price, lowest price first, scaled so the mean is 1.0 — so the ladder's TOTAL
    capital is unchanged and only its distribution across rungs moves. None
    means a flat ladder.

    `exit_signal` is a per-bar flag, aligned with `closes`. When it is true the
    whole position is liquidated at that bar's close paying the TAKER fee, and
    the replay stops. It is how a signal-driven exit (a trend reading, say) is
    modelled as distinct from the price stop. The caller computes the signal, so
    `run` stays free of indicator logic and cannot peek: whatever lookahead the
    signal has is the caller's to avoid.

    `slip`, `fill_margin` and `max_fills_per_bar` are the execution-quality
    assumptions — see rules 5-7 in the module docstring. Set them to
    `0, 0, None` to reproduce a naive backtest, which is worth doing once to
    see the size of the difference.
    """
    fees = fees or FeeModel()

    # Structural checks only. A plan that merely fails `plan.validate()` — too
    # tight to be worth trading, say — is still worth replaying, because seeing
    # the bad number is how I learn it was a bad plan. But a plan whose ladder
    # cannot be constructed has no replay to run.
    if plan.upper <= plan.lower:
        raise ValueError(f"upper ({plan.upper}) must exceed lower ({plan.lower})")
    if plan.levels < 2:
        raise ValueError("a grid needs at least 2 levels")
    if plan.quote_per_level <= 0:
        raise ValueError("quote_per_level must be positive")
    if plan.max_quote_total <= 0:
        raise ValueError("max_quote_total must be positive")
    if bars_per_day <= 0:
        raise ValueError("bars_per_day must be positive")
    if slip < 0 or fill_margin < 0:
        raise ValueError("slip and fill_margin must not be negative — a "
                         "negative value would model being paid to trade")
    if max_fills_per_bar is not None and max_fills_per_bar < 1:
        raise ValueError("max_fills_per_bar must be at least 1, or None")

    n = len(closes)
    days = max(0.0, (n - 1) / bars_per_day)
    start_cash = float(plan.max_quote_total)
    if n < 2:
        return _empty(days, start_cash, fees)

    ladder = plan.grid_prices

    # Per-rung sizing. Normalised to mean 1.0 so the ladder's total commitment
    # is identical to a flat one and the only thing under test is WHERE that
    # capital sits.
    if level_weights is None:
        weights = [1.0] * len(ladder)
    else:
        weights = [float(w) for w in level_weights]
        if len(weights) != len(ladder):
            raise ValueError(
                f"level_weights has {len(weights)} entries for a ladder of "
                f"{len(ladder)} prices")
        if any(w < 0 for w in weights):
            raise ValueError("level_weights must not be negative")
        total = sum(weights)
        if total <= 0:
            raise ValueError("level_weights must not sum to zero")
        weights = [w * len(ladder) / total for w in weights]

    if exit_signal is not None and len(exit_signal) != n:
        raise ValueError(
            f"exit_signal has {len(exit_signal)} entries for {n} closes")

    maker_fee = fees.maker
    bar_seconds = 86_400.0 / bars_per_day if bars_per_day > 0 else 3_600.0
    # Q_max: the inventory past which the buy side is not quoted at all.
    q_max = (max_inventory if max_inventory is not None
             else inventory.max_inventory_for(plan))

    cash = start_cash
    units = 0.0
    open_lots: dict[int, float] = {}       # level index -> units held from that buy
    buy_fill_prices: dict[int, float] = {}  # level index -> price actually paid

    n_cycles = 0
    at_bound_bars = 0
    outside_bars = 0
    signalled_exit = False
    fill_log: list[dict[str, Any]] = []
    placed_at: dict[int, int] = {}      # level index -> bar it was armed
    gross_profit = 0.0                     # completed cycles only, before fees
    cycle_fees = 0.0                       # both legs of completed cycles only
    fees_paid = 0.0                        # every fee, including unmatched buys
    stopped_out = False

    equity = [start_cash]
    peak_units = 0.0

    for i in range(1, n):
        prev, price = float(closes[i - 1]), float(closes[i])

        if price > prev:
            # ---- crossed UP: sell into each level price actually cleared ----
            # `fill_margin`: the close must be ABOVE the level by the margin,
            # not merely level with it. A bare touch is a queue position, not
            # a fill.
            crossed = [i2 for i2, lv in enumerate(ladder)
                       if prev < lv <= price * (1.0 - fill_margin)]
            # Nearest the close first, so that a capped bar keeps the LAST
            # level price reached rather than the most profitable one.
            crossed.sort(key=lambda i2: -ladder[i2])
            if max_fills_per_bar is not None:
                crossed = crossed[:max_fills_per_bar]

            for idx in sorted(crossed):
                level = ladder[idx]
                # Nearest open lot BELOW this level. Taking the cheapest lot
                # instead would book a bigger profit than the grid could have
                # earned — see the module docstring.
                below = [j for j in open_lots if ladder[j] < level]
                if not below:
                    continue
                src = max(below)
                qty = open_lots.pop(src)
                buy_price = buy_fill_prices.pop(src, ladder[src])
                if size_skew:
                    # Sell harder the longer the position. Capped, and never more
                    # than the lot actually held.
                    qty = min(qty * inventory.sell_multiplier(
                        units, q_max, True), units)
                    if qty <= 0:
                        continue

                fill = level * (1.0 - slip)         # sold a little worse
                proceeds = fees.sell_proceeds(fill, qty, maker=True)
                sell_fee = fill * qty * maker_fee
                buy_fee = buy_price * qty * maker_fee

                fill_log.append({
                    "symbol": plan.symbol, "side": "sell", "price": fill,
                    "bar_index": i, "qty": qty,
                    "time_to_fill": (i - placed_at.get(src, i)) * bar_seconds})
                cash += proceeds
                units -= qty
                gross_profit += (fill - buy_price) * qty
                cycle_fees += buy_fee + sell_fee
                fees_paid += sell_fee
                n_cycles += 1

        elif price < prev:
            # ---- crossed DOWN: buy at each level price actually cleared ----
            crossed = [i2 for i2, lv in enumerate(ladder)
                       if price * (1.0 + fill_margin) <= lv < prev
                       and i2 not in open_lots]
            # Highest level first: when the cash cap or the per-bar cap bites,
            # it is the levels price reached FIRST that fill.
            crossed.sort(key=lambda i2: -ladder[i2])
            if max_fills_per_bar is not None:
                # Nearest the close, i.e. the last level price reached — the
                # pessimistic single fill in a sweep.
                crossed = crossed[-max_fills_per_bar:]

            for idx in sorted(crossed, reverse=True):
                level = ladder[idx]
                fill = level * (1.0 + slip)         # bought a little worse
                mult = inventory.buy_multiplier(units, q_max, size_skew)
                if mult <= 0.0:
                    # AT THE BOUND. Not a smaller order — no order.
                    at_bound_bars += 1
                    break
                qty = (plan.quote_per_level * weights[idx] / fill) * mult
                if qty <= 0:
                    continue
                cost = fees.buy_cost(fill, qty, maker=True)
                if cost > cash:
                    continue               # unfunded: the order simply does not fill
                cash -= cost
                units += qty
                open_lots[idx] = qty
                buy_fill_prices[idx] = fill
                fill_log.append({
                    "symbol": plan.symbol, "side": "buy", "price": fill,
                    "bar_index": i, "qty": qty,
                    "time_to_fill": (i - placed_at.get(idx, i)) * bar_seconds})
                placed_at[idx] = i
                fees_paid += fill * qty * maker_fee

        # ---- the signalled exit, before the price stop -------------------
        # Checked FIRST. The point of a signal exit is to leave on
        # evidence rather than after the loss, so if both fire on the same bar
        # the signal is what took the position out.
        if exit_signal is not None and exit_signal[i]:
            if units > 0:
                cash += fees.sell_proceeds(price, units, maker=False)
                fees_paid += price * units * fees.taker
                units = 0.0
            open_lots.clear()
            buy_fill_prices.clear()
            signalled_exit = True
            equity.append(cash)
            break

        # ---- the stop, checked after this bar's fills --------------------
        # Buys resting between the previous close and the stop would have filled
        # on the way down before the stop was reached, so they are booked first.
        # That makes the stop-out worse, not better, which is the realistic order.
        if price <= plan.stop_below:
            if units > 0:
                cash += fees.sell_proceeds(price, units, maker=liquidation_maker)
                rate = maker_fee if liquidation_maker else fees.taker
                fees_paid += price * units * rate
                units = 0.0
            open_lots.clear()
            buy_fill_prices.clear()
            stopped_out = True
            equity.append(cash)
            break

        if price < plan.lower or price > plan.upper:
            outside_bars += 1

        peak_units = max(peak_units, units)
        equity.append(cash + units * price)

    final_equity = equity[-1]
    total_return_pct = (final_equity / start_cash) - 1.0 if start_cash else 0.0

    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak)

    # Measured over completed cycles only. Including the buy fee of a lot that
    # is still open — or the taker fee of a stop-out — would compare a cost with
    # a gross profit it did not produce, and the number would no longer mean
    # what `FeeModel.fee_share()` means.
    fee_share = cycle_fees / gross_profit if gross_profit > 0 else 0.0

    # `days` counts the bars actually replayed; a stop-out ends the test early.
    replayed_days = max(0.0, (len(equity) - 1) / bars_per_day)

    return BacktestResult(
        total_return_pct=total_return_pct,
        max_drawdown_pct=max_dd,
        n_cycles=n_cycles,
        fees_paid=fees_paid,
        gross_profit=gross_profit,
        fees_as_pct_of_gross=fee_share,
        days=replayed_days,
        stopped_out=stopped_out,
        final_units_held=units,
        equity_curve=tuple(equity),
        maker_fee=fees.maker,
        taker_fee=fees.taker,
        slip=slip,
        fill_margin=fill_margin,
        max_fills_per_bar=max_fills_per_bar,
        max_inventory=q_max,
        size_skew=size_skew,
        bars_at_bound=at_bound_bars,
        bars_outside_band=outside_bars,
        bars_replayed=len(equity) - 1,
        signalled_exit=signalled_exit,
        peak_inventory=peak_units,
        fills=tuple(fill_log),
    )


# ---------------------------------------------------------------------------
# Walk-forward
# ---------------------------------------------------------------------------
#
# A single backtest over the whole history says almost nothing, because the
# parameters were chosen by looking at that history. Range, spacing and level
# count are all fitted quantities; fit them to the data they are scored on and a
# grid will look profitable on almost any series that happens to have oscillated.
#
# The split below is the cheapest fair test available: derive every parameter
# from the first stretch, then replay on the rest and never touch the dials
# again. It cannot prove an edge — one split on one asset is one sample — but it
# reliably catches the failure that matters, which is a plan tuned to a regime
# that then ended.


def levels_for_spacing(lower: float, upper: float, min_spacing: float,
                       spacing_mode: str = GEOMETRIC) -> int:
    """Most grid lines that fit in [lower, upper] without going tighter than
    `min_spacing` anywhere on the ladder.

    Measured at `GridPlan.min_spacing_pct` — the TOP of the ladder, where levels
    evenly spaced in PRICE are narrowest in PERCENT. Sizing off the bottom
    spacing instead is the mistake `plan.validate()` exists to catch: it reports
    the most flattering number on the ladder while the upper half quietly gives
    a much larger share of gross to the exchange.

        step / (upper - step) >= g   =>   step >= g * upper / (1 + g)

    On a GEOMETRIC ladder every gap is the same fraction, so the constraint is
    simply that the per-rung ratio not fall below 1 + g:

        (upper/lower)**(1/n) - 1 >= g   =>   n <= ln(upper/lower) / ln(1 + g)

    Lives here, and not inline in its two callers, because the analyst and the
    walk-forward must build the SAME ladder from the same band. Two copies of
    this formula drifting apart would mean the geometry that was validated is
    not the geometry that gets proposed.
    """
    if upper <= lower:
        raise ValueError(f"upper ({upper}) must exceed lower ({lower})")
    if min_spacing <= 0:
        raise ValueError("min_spacing must be positive")
    if spacing_mode == ARITHMETIC:
        return max(2, int((upper - lower) * (1.0 + min_spacing)
                          / (min_spacing * upper)))
    if lower <= 0:
        raise ValueError("lower must be positive for a geometric ladder")
    return max(2, int(math.log(upper / lower) / math.log1p(min_spacing)))


@dataclass(frozen=True)
class WalkForwardResult:
    """In-sample fit, out-of-sample verdict, and the plan that connects them."""

    plan: GridPlan                 # built from in-sample data only
    in_sample: BacktestResult      # the plan on the data that produced it
    out_of_sample: BacktestResult  # the plan on data it has never seen
    generalises: bool
    split_at: int                  # index where out-of-sample begins
    reason: str = ""               # why generalises is False

    # The two windows, kept separate and reported. See `walk_forward`.
    n_bars_statistics: int = 0     # bars the CHARACTER was judged on
    n_bars_range: int = 0          # bars the BAND was drawn from
    range_from: str = ""           # ISO dates, when timestamps were supplied
    range_to: str = ""
    statistics_from: str = ""
    statistics_to: str = ""

    @property
    def retention(self) -> float:
        """Fraction of the in-sample return that survived out of sample.

        Above 1.0 means it did better on unseen data, which is usually luck
        rather than a good sign. Below 0.5 fails.
        """
        if self.in_sample.total_return_pct <= 0:
            return 0.0
        return self.out_of_sample.total_return_pct / self.in_sample.total_return_pct

    def windows(self) -> str:
        """One line naming both windows, for tables, proposals and reports."""
        def span(n: int, a: str, b: str) -> str:
            return f"{n} bars" + (f" {a[:10]}..{b[:10]}" if a and b else "")
        return (f"stats {span(self.n_bars_statistics, self.statistics_from, self.statistics_to)}"
                f" | range {span(self.n_bars_range, self.range_from, self.range_to)}")

    def explain(self) -> str:
        verdict = "GENERALISES" if self.generalises else "DOES NOT GENERALISE"
        return (
            f"{self.plan.symbol}: {verdict}\n"
            f"  in-sample  {self.in_sample.total_return_pct:+.2%} "
            f"({self.in_sample.n_cycles} cycles)\n"
            f"  out-sample {self.out_of_sample.total_return_pct:+.2%} "
            f"({self.out_of_sample.n_cycles} cycles"
            f"{', STOPPED OUT' if self.out_of_sample.stopped_out else ''})\n"
            f"  retained {self.retention:.0%} of the fitted return\n"
            f"  windows: {self.windows()}"
            + (f"\n  {self.reason}" if self.reason else "")
        )


def walk_forward(closes: Sequence[float], highs: Sequence[float],
                 lows: Sequence[float], symbol: str, split: float = 0.6, *,
                 bars_per_day: float, fees: Optional[FeeModel] = None,
                 capital: float = 1000.0, coverage: float = 0.90,
                 max_fee_share: float = 0.25,
                 stop_pct: float = 0.05,
                 spacing_mode: str = GEOMETRIC,
                 slip: float = DEFAULT_SLIP,
                 fill_margin: float = DEFAULT_FILL_MARGIN,
                 max_fills_per_bar: Optional[int] = DEFAULT_MAX_FILLS_PER_BAR,
                 range_days: Optional[float] = DEFAULT_RANGE_DAYS,
                 timestamps: Optional[Sequence[float]] = None,
                 ) -> WalkForwardResult:
    """Fit a grid on the first `split` of history, then score it on the rest.

        >>> walk_forward(closes, highs, lows, "BTC-USDT", bars_per_day=24.0).explain()

    Nothing derived from the out-of-sample stretch reaches the plan: the range,
    the level count, the stop and the capital split are all computed from
    `closes[:split_at]` and then frozen. The out-of-sample stretch is replayed,
    not re-fit.

    `highs` and `lows` are used ONLY to describe the in-sample regime — the ATR
    and ADX recorded on the plan for provenance. They are never handed to
    `run()`, which takes closes alone so that no fill can be claimed from a wick.
    See the module docstring.

    `bars_per_day` has no default, exactly as in `run()` and `rubric.assess`.

    Spacing is set to the tightest value whose fee share stays within
    `max_fee_share` — tighter spacing means more cycles, and the fee floor is
    the binding constraint on how tight it can go.
    """
    fees = fees or FeeModel()

    n = len(closes)
    if not (0.0 < split < 1.0):
        raise ValueError(f"split must be in (0, 1), got {split}")
    if len(highs) != n or len(lows) != n:
        raise ValueError(
            f"closes, highs and lows must be the same length "
            f"({n}, {len(highs)}, {len(lows)})")

    split_at = int(n * split)
    in_closes = list(closes[:split_at])
    out_closes = list(closes[split_at:])
    if len(in_closes) < 20:
        raise ValueError(
            f"only {len(in_closes)} in-sample bars — suggest_range needs 20+ and "
            f"anything near that is fitting noise")
    if len(out_closes) < 2:
        raise ValueError(
            f"only {len(out_closes)} out-of-sample bars — nothing to verify against")

    # --- fit, on the in-sample stretch only ---------------------------------
    #
    # TWO WINDOWS, AND WHY THEY ARE NOT THE SAME WINDOW.
    #
    # The CHARACTER of an asset — is it mean-reverting, how volatile, does that
    # hold up across sub-periods — is a slow property and needs a long window to
    # measure. The BAND is not a property of the asset at all. It is a statement
    # about where price is RIGHT NOW, and a long window answers it with the
    # union of every regime in the sample.
    #
    # Concretely: a year of LTC hourly bars suggests 66-126, a band whose upper
    # edge is nearly double its lower. No grid can trade that. Rungs spaced to
    # clear fees are so far apart that price wanders between them for months and
    # the backtest reports near-zero cycles — which is why `n_cycles` is worth
    # reading next to any return figure here. The last 45 days of the same
    # series suggest 44-54, which is a band a ladder can actually work.
    #
    # So: statistics on `in_closes`, band on the TAIL of `in_closes`. The tail
    # is still strictly in-sample — nothing at or after `split_at` is touched —
    # so this tightens the fit without leaking the answer.
    range_bars = len(in_closes)
    if range_days and bars_per_day > 0:
        want = int(round(range_days * bars_per_day))
        # Never fewer than 20 bars (suggest_range's own floor) and never more
        # than there are: a short history falls back to using all of it rather
        # than refusing to fit.
        range_bars = max(20, min(len(in_closes), want))
    band_closes = in_closes[-range_bars:]

    band = metrics.suggest_range(band_closes, coverage=coverage)
    if band is None:
        raise ValueError("could not derive a range from the in-sample prices")
    lower, upper = band

    g = fees.min_viable_spacing(fee_share_of_gross=max_fee_share, maker=True)
    levels = levels_for_spacing(lower, upper, g, spacing_mode)

    quote_per_level = capital / (levels + 1)

    in_highs, in_lows = list(highs[:split_at]), list(lows[:split_at])
    band_highs, band_lows = in_highs[-range_bars:], in_lows[-range_bars:]
    range_start = split_at - range_bars

    def _iso(i: int) -> str:
        if not timestamps or not (0 <= i < len(timestamps)):
            return ""
        return _dt.datetime.fromtimestamp(
            float(timestamps[i]) / 1000.0,
            tz=_dt.timezone.utc).isoformat(timespec="seconds")

    plan = GridPlan(
        symbol=symbol,
        lower=lower,
        upper=upper,
        levels=levels,
        spacing_mode=spacing_mode,
        quote_per_level=quote_per_level,
        stop_below=lower * (1.0 - stop_pct),
        # Exact, so float drift cannot trip validate()'s funding check.
        max_quote_total=quote_per_level * (levels + 1),
        thesis=(f"Walk-forward fit on bars 0-{split_at} of {n}; "
                f"replayed untouched on the remaining {len(out_closes)}."),
        metrics={
            "in_sample_bars": float(split_at),
            # CHARACTER — long window.
            "hurst": metrics.hurst(in_closes) or 0.0,
            "adx": metrics.adx(in_highs, in_lows, in_closes) or 0.0,
            "efficiency_ratio": metrics.efficiency_ratio(in_closes) or 0.0,
            "statistics_bars": float(split_at),
            # BAND — short window. ATR sets the stop and the spacing sanity
            # check, so it belongs to the window the band came from.
            "atr_pct": metrics.atr_pct(band_highs, band_lows, band_closes) or 0.0,
            "range_bars": float(range_bars),
        },
    )

    # --- score: closes only, both halves, no re-tuning between them ---------
    # Both halves replayed under IDENTICAL execution assumptions — comparing a
    # clean in-sample run against a slipped out-of-sample one would measure the
    # assumptions, not the regime.
    execution = dict(slip=slip, fill_margin=fill_margin,
                     max_fills_per_bar=max_fills_per_bar)
    in_result = run(plan, in_closes, bars_per_day=bars_per_day, fees=fees,
                    **execution)
    out_result = run(plan, out_closes, bars_per_day=bars_per_day, fees=fees,
                     **execution)

    ins, oos = in_result.total_return_pct, out_result.total_return_pct

    # My rule is "out-of-sample positive AND at least half the in-sample
    # return". The `ins > 0` clause is extra: with a negative in-sample
    # return, "at least half of it" is a NEGATIVE bar that any positive result
    # clears, so a plan that lost money on the data it was fitted to would be
    # reported as generalising. There is no edge there to retain — it is a
    # coin-flip that landed the right way up. This only ever makes the test
    # harder to pass.
    if ins <= 0:
        generalises, reason = False, (
            f"the fit itself lost money in-sample ({ins:+.2%}); there is no edge "
            f"here to carry forward, whatever the out-of-sample number says")
    elif oos <= 0:
        generalises, reason = False, (
            f"out-of-sample return {oos:+.2%} is not positive"
            + (" — the grid stopped out" if out_result.stopped_out else ""))
    elif oos < 0.5 * ins:
        generalises, reason = False, (
            f"out-of-sample {oos:+.2%} retained only "
            f"{oos / ins:.0%} of the in-sample {ins:+.2%} (needs 50%)")
    else:
        generalises, reason = True, ""

    return WalkForwardResult(
        plan=plan, in_sample=in_result, out_of_sample=out_result,
        generalises=generalises, split_at=split_at, reason=reason,
        n_bars_statistics=split_at, n_bars_range=range_bars,
        statistics_from=_iso(0), statistics_to=_iso(split_at - 1),
        range_from=_iso(range_start), range_to=_iso(split_at - 1),
    )


# ---------------------------------------------------------------- rolling re-fit

#: How often the analyst is assumed to re-scan and propose a fresh grid.
#:
#: A single-fit backtest holds one band for the whole out-of-sample stretch.
#: That is not the strategy: live, the analyst re-scans and proposes a new grid,
#: and a band is only ever expected to be current for weeks. Holding a 45-day
#: band for 104 days measures band staleness, not the strategy.
DEFAULT_REFIT_DAYS = 14.0

#: What happens to inventory when a grid is torn down and re-ranged.
#:
#: LIQUIDATE is the default and it is the pessimistic choice. Tearing down a
#: grid means cancelling its resting orders; the inventory those orders bought
#: has to be either sold or carried, and selling it is a market order that pays
#: the TAKER fee and crosses the spread. Carrying it into a new ladder whose
#: rungs sit at different prices means the new grid starts with lots it has no
#: matching sell level for, which flatters the result by hiding the cost of
#: re-ranging.
#:
#: So the default charges the full cost of re-ranging every time. CARRY is
#: offered to measure what that assumption costs, not because it is safer.
LIQUIDATE = "liquidate"
CARRY = "carry"


@dataclass(frozen=True)
class RollingResult:
    """A grid re-fitted on a schedule, the way the analyst actually works."""

    symbol: str
    total_return_pct: float
    max_drawdown_pct: float
    n_cycles: int
    n_refits: int
    fees_paid: float
    refit_costs: float             # taker fees + spread paid tearing grids down
    bars_replayed: int
    bars_outside_band: int
    bars_at_bound: int
    peak_inventory: float
    stopped_out_count: int
    equity_curve: tuple[float, ...]
    bands: tuple[tuple[float, float], ...]
    inventory_rule: str = LIQUIDATE

    @property
    def outside_band_pct(self) -> float:
        return (self.bars_outside_band / self.bars_replayed
                if self.bars_replayed else 0.0)

    def explain(self) -> str:
        return (
            f"{self.symbol}: {self.total_return_pct:+.2%} over "
            f"{self.bars_replayed} bars, {self.n_refits} re-fits\n"
            f"  {self.n_cycles} cycles, max drawdown "
            f"{self.max_drawdown_pct:.2%}\n"
            f"  {self.bars_outside_band} bars outside the band "
            f"({self.outside_band_pct:.1%}), {self.bars_at_bound} at the "
            f"inventory bound\n"
            f"  {self.stopped_out_count} segment(s) stopped out\n"
            f"  re-ranging cost {self.refit_costs:.4g} in taker fees "
            f"({self.inventory_rule})")


def rolling_walk_forward(
        closes: Sequence[float], highs: Sequence[float], lows: Sequence[float],
        symbol: str, *, bars_per_day: float, start_at: int,
        fees: Optional[FeeModel] = None, capital: float = 1000.0,
        coverage: float = 0.90, max_fee_share: float = 0.25,
        stop_pct: float = 0.05, spacing_mode: str = GEOMETRIC,
        range_days: float = DEFAULT_RANGE_DAYS,
        refit_days: float = DEFAULT_REFIT_DAYS,
        inventory_rule: str = LIQUIDATE,
        slip: float = DEFAULT_SLIP, fill_margin: float = DEFAULT_FILL_MARGIN,
        max_fills_per_bar: Optional[int] = DEFAULT_MAX_FILLS_PER_BAR,
        size_skew: bool = False, bound_fraction: Optional[float] = None,
        ) -> RollingResult:
    """Replay `closes[start_at:]`, re-fitting the band every `refit_days`.

        >>> rolling_walk_forward(closes, highs, lows, "BTC-USDT",
        ...                      bars_per_day=24.0, start_at=6260).explain()

    Each segment is an ordinary `run()` over one re-fit interval, on a plan
    built from the `range_days` of history immediately BEFORE that segment
    starts. No segment ever sees its own prices before replaying them, so this
    is still strictly out-of-sample — it is walk-forward with a rolling origin
    rather than a single split.

    Capital compounds across segments. At each boundary the grid is torn down
    and inventory is handled per `inventory_rule`; see LIQUIDATE above for why
    the default pays the taker fee.
    """
    fees = fees or FeeModel()
    n = len(closes)
    if not (0 < start_at < n):
        raise ValueError(f"start_at must be inside the series (got {start_at} "
                         f"of {n})")
    if len(highs) != n or len(lows) != n:
        raise ValueError("closes, highs and lows must be the same length")
    if inventory_rule not in (LIQUIDATE, CARRY):
        raise ValueError(f"inventory_rule must be {LIQUIDATE!r} or {CARRY!r}")
    if refit_days <= 0:
        raise ValueError("refit_days must be positive")

    seg_bars = max(1, int(round(refit_days * bars_per_day)))
    range_bars = max(20, int(round(range_days * bars_per_day)))

    capital_now = float(capital)
    equity: list[float] = [capital_now]
    bands: list[tuple[float, float]] = []
    total_cycles = total_fees = refit_costs = 0.0
    total_outside = total_at_bound = total_bars = 0
    peak_inv = 0.0
    stopped = 0
    carried_units = 0.0

    origin = start_at
    while origin < n - 1:
        end = min(n, origin + seg_bars)

        # --- fit on the trailing window, which ends where this segment begins
        fit_from = max(0, origin - range_bars)
        band = metrics.suggest_range(list(closes[fit_from:origin]),
                                     coverage=coverage)
        if band is None or band[1] <= band[0]:
            origin = end
            continue
        lower, upper = band

        g = fees.min_viable_spacing(fee_share_of_gross=max_fee_share, maker=True)
        levels = levels_for_spacing(lower, upper, g, spacing_mode)
        if levels < 2:
            origin = end
            continue
        quote_per_level = capital_now / (levels + 1)
        if quote_per_level <= 0:
            break

        plan = GridPlan(
            symbol=symbol, lower=lower, upper=upper, levels=levels,
            spacing_mode=spacing_mode, quote_per_level=quote_per_level,
            stop_below=lower * (1.0 - stop_pct),
            max_quote_total=quote_per_level * (levels + 1),
            thesis=(f"Rolling re-fit: band from bars {fit_from}-{origin}, "
                    f"replayed on {origin}-{end}."),
        )
        bands.append((lower, upper))

        q_max = (inventory.max_inventory_for(plan, fraction=bound_fraction)
                 if bound_fraction is not None else None)
        seg = run(plan, list(closes[origin:end]), bars_per_day=bars_per_day,
                  fees=fees, slip=slip, fill_margin=fill_margin,
                  max_fills_per_bar=max_fills_per_bar, size_skew=size_skew,
                  max_inventory=q_max)

        # --- roll the books forward ---------------------------------------
        seg_end_equity = seg.equity_curve[-1]
        for e in seg.equity_curve[1:]:
            equity.append(e * (capital_now / plan.max_quote_total))

        total_cycles += seg.n_cycles
        total_fees += seg.fees_paid
        total_at_bound += seg.bars_at_bound
        # Measured over the WHOLE segment span, not just the bars `run` got
        # through. A segment that stops out early stops replaying, but those
        # bars still happened and price was still outside the band during them
        # — counting only the replayed ones would flatter the staleness figure
        # exactly when the band was at its most stale.
        total_bars += end - origin
        total_outside += sum(1 for px in closes[origin:end]
                             if px < lower or px > upper)
        peak_inv = max(peak_inv, seg.peak_inventory)
        if seg.stopped_out:
            stopped += 1

        # --- tear the grid down -------------------------------------------
        held = seg.final_units_held
        last_price = float(closes[min(end, n) - 1])
        if inventory_rule == LIQUIDATE and held > 0:
            # A market sell: crosses the spread and pays taker.
            cost = last_price * held * fees.taker
            refit_costs += cost
            seg_end_equity -= cost
        elif inventory_rule == CARRY:
            carried_units += held

        capital_now = seg_end_equity * (capital_now / plan.max_quote_total)
        equity[-1] = capital_now
        origin = end

    final = equity[-1]
    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak)

    return RollingResult(
        symbol=symbol,
        total_return_pct=(final / capital) - 1.0 if capital else 0.0,
        max_drawdown_pct=max_dd, n_cycles=int(total_cycles),
        n_refits=len(bands), fees_paid=total_fees, refit_costs=refit_costs,
        bars_replayed=total_bars, bars_outside_band=total_outside,
        bars_at_bound=total_at_bound, peak_inventory=peak_inv,
        stopped_out_count=stopped, equity_curve=tuple(equity),
        bands=tuple(bands), inventory_rule=inventory_rule)
