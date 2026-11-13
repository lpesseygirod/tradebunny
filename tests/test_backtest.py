"""Tests for the grid backtester.

The generators come from `test_metrics.py` deliberately: the rubric decides an
asset is grid-worthy from those same series, so the backtester had better agree
with it. If `oscillating()` passes the rubric but loses money on replay, one of
the two is wrong.

The character of each series is known by construction, so the expected sign of
every result here is known in advance rather than read off the output.
"""

import dataclasses
import inspect

import pytest

from grid import backtest
from grid.backtest import BacktestResult, run
from grid.fees import FeeModel
from grid.plan import GridPlan

from test_metrics import oscillating, trending


BARS_PER_DAY = 24.0

#: Execution assumptions switched OFF. Tests of a MECHANISM — lot pairing, the
#: capital cap, the fee-model correspondence — pass this so they isolate the
#: thing under test from slippage and the per-bar fill cap. The execution
#: assumptions have their own tests further down.
NAIVE = dict(slip=0.0, fill_margin=0.0, max_fills_per_bar=None)


def make_plan(**over):
    """A plan sized to `oscillating()`: centre 100, stationary sd ~2.3.

    12 levels of 1.0 across 94–106 gives ~0.95% spacing at the narrowest point,
    comfortably above the 0.16% break-even and inside the fee gate.
    """
    kw = dict(symbol="T-USDT", lower=94.0, upper=106.0, levels=12,
              quote_per_level=100.0, stop_below=90.0, max_quote_total=1300.0,
              # ARITHMETIC on purpose: these tests reason about a ladder of
              # whole numbers 94..106, which is what makes their expected
              # fills readable. Geometric spacing (the default everywhere
              # else) has its own tests below.
              spacing_mode="arithmetic")
    kw.update(over)
    return GridPlan(**kw)


def flat(n=300, price=100.0):
    return [price] * n


def falling(n=300):
    """A clean downtrend that must breach the stop."""
    return trending(n=n, start=100.0, drift=-0.004, noise=0.004, seed=1)


# ------------------------------------------------------------- premise checks

class TestThePlanUnderTestIsSane:
    """If the plan itself is invalid the results below mean nothing."""

    def test_it_passes_its_own_validation(self):
        assert make_plan().validate() == []

    def test_the_ladder_is_as_expected(self):
        p = make_plan()
        assert p.grid_prices == [94.0 + i for i in range(13)]
        assert p.step == 1.0

    def test_the_series_visits_the_ladder(self):
        s = oscillating()
        assert min(s) < 99.0 and max(s) > 101.0

    def test_the_falling_series_actually_breaches_the_stop(self):
        assert min(falling()) < make_plan().stop_below


# ------------------------------------------------------------- the basics

class TestOscillatingSeries:
    """The case the whole strategy exists for."""

    @pytest.fixture
    def result(self):
        return run(make_plan(), oscillating(), bars_per_day=BARS_PER_DAY)

    def test_it_makes_money(self, result):
        assert result.total_return_pct > 0

    def test_it_completes_several_cycles(self, result):
        assert result.n_cycles >= 5

    def test_it_does_not_stop_out(self, result):
        assert result.stopped_out is False

    def test_gross_profit_is_positive(self, result):
        assert result.gross_profit > 0

    def test_it_paid_fees_to_earn_that(self, result):
        assert result.fees_paid > 0

    def test_net_is_smaller_than_gross(self, result):
        """Fees are real; a backtest where they are not is not measuring them."""
        assert 0 < result.net_profit < result.gross_profit


class TestDownwardTrendStopsOut:

    @pytest.fixture
    def result(self):
        return run(make_plan(), falling(), bars_per_day=BARS_PER_DAY)

    def test_stopped_out_is_set(self, result):
        assert result.stopped_out is True

    def test_it_ends_holding_nothing(self, result):
        """A stop that leaves inventory behind is not a stop."""
        assert result.final_units_held == 0.0

    def test_it_loses_money(self, result):
        assert result.total_return_pct < 0

    def test_the_replay_halts_early(self, result):
        assert result.days < (len(falling()) - 1) / BARS_PER_DAY

    def test_it_bought_on_the_way_down_before_stopping(self, result):
        """The grid buys into a decline — that is the risk being measured."""
        assert result.fees_paid > 0


class TestFeeShareMatchesTheFeeModel:
    """`fees_as_pct_of_gross` must mean what `FeeModel.fee_share()` means."""

    @pytest.fixture
    def result(self):
        return run(make_plan(), oscillating(), bars_per_day=BARS_PER_DAY)

    def test_within_one_point_of_fee_share_at_the_ladder_spacing(self):
        """Naive execution: with slippage on, `gross` is reduced by the slip
        and the ratio is no longer the fee model's own quantity."""
        result = run(make_plan(), oscillating(), bars_per_day=BARS_PER_DAY,
                     **NAIVE)
        p, f = make_plan(), FeeModel()
        # Levels are evenly spaced in price, so relative spacing varies down the
        # ladder. Every cycle sits between these two bounds.
        widest = f.fee_share(p.spacing_pct)
        narrowest = f.fee_share(p.min_spacing_pct)
        assert result.fees_as_pct_of_gross == pytest.approx(widest, abs=0.01)
        assert result.fees_as_pct_of_gross == pytest.approx(narrowest, abs=0.01)

    def test_it_is_a_fraction_not_a_percentage(self, result):
        """0.16 means 16%. Matching fees.py and rubric.py."""
        assert 0.0 < result.fees_as_pct_of_gross < 1.0

    def test_a_wider_grid_gives_less_away(self):
        """The direction has to be right, not just the magnitude."""
        tight = run(make_plan(levels=12), oscillating(), bars_per_day=BARS_PER_DAY)
        wide = run(make_plan(levels=4), oscillating(), bars_per_day=BARS_PER_DAY)
        assert wide.fees_as_pct_of_gross < tight.fees_as_pct_of_gross

    def test_higher_fees_take_a_bigger_share(self):
        cheap = run(make_plan(), oscillating(), bars_per_day=BARS_PER_DAY,
                    fees=FeeModel(maker=0.0008, taker=0.0010))
        dear = run(make_plan(), oscillating(), bars_per_day=BARS_PER_DAY,
                   fees=FeeModel(maker=0.0040, taker=0.0050))
        assert dear.fees_as_pct_of_gross > cheap.fees_as_pct_of_gross


class TestFlatSeries:

    @pytest.fixture
    def result(self):
        return run(make_plan(), flat(), bars_per_day=BARS_PER_DAY)

    def test_no_cycles(self, result):
        assert result.n_cycles == 0

    def test_return_is_zero(self, result):
        assert result.total_return_pct == pytest.approx(0.0, abs=1e-9)

    def test_no_fees_because_nothing_traded(self, result):
        assert result.fees_paid == 0.0

    def test_no_drawdown(self, result):
        assert result.max_drawdown_pct == pytest.approx(0.0, abs=1e-12)

    def test_fee_share_is_zero_not_a_division_error(self, result):
        assert result.fees_as_pct_of_gross == 0.0

    def test_a_flat_series_off_the_ladder_also_does_nothing(self):
        assert run(make_plan(), flat(price=200.0), bars_per_day=BARS_PER_DAY).n_cycles == 0


# ------------------------------------------------- closes only, fair pairing

class TestOnlyClosesAreUsed:
    """The signature is the guarantee: highs and lows cannot be passed in."""

    def test_run_takes_no_high_or_low_parameter(self):
        params = set(inspect.signature(run).parameters)
        assert "highs" not in params and "lows" not in params
        assert params == {"plan", "closes", "bars_per_day", "fees",
                          "liquidation_maker", "slip", "fill_margin",
                          "max_fills_per_bar", "size_skew", "max_inventory",
                          "level_weights", "exit_signal"}

    def test_a_signalled_exit_cannot_smuggle_wicks_into_fills(self):
        """`exit_signal` is a per-bar flag, not a price series.

        The caller may compute it from highs and lows — an ADX reading, say —
        and that is fine, because a signal decides WHETHER to liquidate, not at
        what price. The liquidation still happens at the bar's CLOSE. Nothing
        about it lets a fill be claimed from a wick, which is the invariant
        this class exists to protect.
        """
        p = GridPlan(symbol="T-USDT", lower=94.0, upper=106.0, levels=12,
                     quote_per_level=100.0, stop_below=50.0,
                     max_quote_total=1300.0, spacing_mode="arithmetic")
        closes = [100.0, 98.0, 96.0, 99.0, 101.0]
        flags = [False, False, True, False, False]
        r = run(p, closes, bars_per_day=24.0, exit_signal=flags)
        assert r.signalled_exit is True
        # Exited at bar 2, so bar 3 and 4 were never replayed.
        assert r.bars_replayed == 2
        # And the exit price was that bar's close, not its low.
        assert r.final_units_held == 0.0

    def test_a_wick_below_a_level_between_two_closes_books_no_fill(self):
        """Price dips under 99 and closes back above it. A low-based backtest
        would claim a buy here; there was never a close beneath the level."""
        closes = [100.0, 99.5, 100.0, 99.5, 100.0]
        assert run(make_plan(), closes, bars_per_day=BARS_PER_DAY).n_cycles == 0

    def test_a_close_through_a_level_does_book_a_fill(self):
        closes = [100.0, 98.5, 100.5]
        r = run(make_plan(), closes, bars_per_day=BARS_PER_DAY)
        assert r.n_cycles == 1


class TestLotPairing:

    def test_it_closes_the_nearest_lot_not_the_cheapest(self):
        """Down to 95.5 opens lots at 99, 98, 97, 96. Back up to 99.2 crosses
        four sell levels, and each pairs with the buy directly beneath it:
        96->97, 97->98, 98->99, leaving the 99 lot open.

        Selling the CHEAPEST open lot at each level would book 6.25 instead of
        3.09 — the same three cycles, double the profit, none of it real.
        """
        r = run(make_plan(), [100.0, 95.5, 99.2], bars_per_day=BARS_PER_DAY,
                **NAIVE)
        assert r.n_cycles == 3
        adjacent = sum((s - b) * (100.0 / b)
                       for b, s in ((96.0, 97.0), (97.0, 98.0), (98.0, 99.0)))
        cheapest = sum((s - 96.0) * (100.0 / 96.0) for s in (97.0, 98.0, 99.0))
        assert r.gross_profit == pytest.approx(adjacent)
        assert r.gross_profit < cheapest
        assert r.final_units_held == pytest.approx(100.0 / 99.0)

    def test_no_cycle_ever_earns_more_than_one_grid_step(self):
        """The general form of the test above, over a real series.

        A cycle spanning two steps means a sell was paired with a lot further
        down the ladder than the one beneath it — cherry-picking. One step is
        the most a correctly paired cycle can earn.
        """
        p = make_plan()
        r = run(p, oscillating(), bars_per_day=BARS_PER_DAY, **NAIVE)
        most_per_cycle = p.step * (p.quote_per_level / p.lower)
        assert r.n_cycles > 0
        assert r.gross_profit / r.n_cycles <= most_per_cycle + 1e-9

    def test_one_open_lot_per_level(self):
        """Two down-crosses of the same level without a sell between them must
        not stack two lots on one grid line."""
        closes = [100.0, 98.5, 98.9, 98.5, 100.5]
        r = run(make_plan(), closes, bars_per_day=BARS_PER_DAY)
        assert r.n_cycles == 1

    def test_a_level_rearms_after_its_lot_is_sold(self):
        closes = [100.0, 98.5, 100.5, 98.5, 100.5]
        r = run(make_plan(), closes, bars_per_day=BARS_PER_DAY)
        assert r.n_cycles == 2

    def test_selling_up_through_a_level_with_nothing_below_does_nothing(self):
        closes = [100.0, 105.5]
        r = run(make_plan(), closes, bars_per_day=BARS_PER_DAY)
        assert r.n_cycles == 0 and r.gross_profit == 0.0


class TestCapitalCap:

    def test_it_stops_buying_when_the_budget_runs_out(self):
        """A walk from 100 to 94.5 crosses all 13 levels. With room for one buy
        it makes one, not thirteen."""
        r = run(make_plan(max_quote_total=150.0), [100.0, 94.5],
                bars_per_day=BARS_PER_DAY, **NAIVE)
        assert r.final_units_held == pytest.approx(100.0 / 99.0)

    def test_the_fee_is_part_of_the_spend(self):
        """A budget of exactly one level's notional cannot fund that level: the
        maker fee has to come from somewhere. Ignoring it here would let every
        backtest deploy slightly more capital than the plan allows."""
        r = run(make_plan(max_quote_total=100.0), [100.0, 94.5],
                bars_per_day=BARS_PER_DAY, **NAIVE)
        assert r.final_units_held == 0.0
        assert r.fees_paid == 0.0

    def test_it_fills_from_the_top_down(self):
        """Price reaches the higher levels first, so those are the orders that
        consume a limited budget — not the cheaper ones further down."""
        r = run(make_plan(max_quote_total=150.0), [100.0, 94.5],
                bars_per_day=BARS_PER_DAY, **NAIVE)
        assert r.final_units_held == pytest.approx(100.0 / 99.0)   # 99, not 94

    def test_equity_never_implies_negative_cash(self):
        p = make_plan(max_quote_total=300.0)
        r = run(p, [100.0, 94.5, 100.5], bars_per_day=BARS_PER_DAY, **NAIVE)
        assert all(e > 0 for e in r.equity_curve)

    def test_a_bigger_budget_buys_more_of_the_ladder(self):
        small = run(make_plan(max_quote_total=200.0), [100.0, 94.5],
                    bars_per_day=BARS_PER_DAY, **NAIVE)
        big = run(make_plan(max_quote_total=1300.0), [100.0, 94.5],
                  bars_per_day=BARS_PER_DAY, **NAIVE)
        assert big.final_units_held > small.final_units_held


class TestTheStop:

    def test_it_exits_at_the_breaching_close_not_at_the_stop_price(self):
        """Price gaps from 95 straight to 80, well through the 90 stop.

        Liquidating at 90 would invent 10 points of price that never traded.
        """
        r = run(make_plan(), [100.0, 95.0, 80.0], bars_per_day=BARS_PER_DAY)
        assert r.stopped_out is True
        assert r.equity_curve[-1] < 1300.0

    def test_a_close_exactly_on_the_stop_triggers_it(self):
        """'at or below' — an exact touch is a breach."""
        r = run(make_plan(), [100.0, 95.0, 90.0], bars_per_day=BARS_PER_DAY)
        assert r.stopped_out is True

    def test_just_above_the_stop_does_not_trigger(self):
        r = run(make_plan(), [100.0, 95.0, 90.01], bars_per_day=BARS_PER_DAY)
        assert r.stopped_out is False

    def test_the_curve_ends_at_the_stop_bar(self):
        r = run(make_plan(), [100.0, 95.0, 80.0, 120.0, 130.0],
                bars_per_day=BARS_PER_DAY)
        assert len(r.equity_curve) == 3       # a halted grid does not recover

    def test_liquidation_pays_taker_by_default(self):
        """A forced exit crosses the spread; it is not a resting order."""
        closes = [100.0, 95.0, 80.0]
        taker = run(make_plan(), closes, bars_per_day=BARS_PER_DAY)
        maker = run(make_plan(), closes, bars_per_day=BARS_PER_DAY,
                    liquidation_maker=True)
        assert taker.fees_paid > maker.fees_paid
        assert taker.total_return_pct < maker.total_return_pct


class TestDrawdown:

    def test_a_decline_registers_one(self):
        r = run(make_plan(), [100.0, 96.0, 92.0], bars_per_day=BARS_PER_DAY)
        assert r.max_drawdown_pct > 0

    def test_it_is_a_fraction(self):
        r = run(make_plan(), falling(), bars_per_day=BARS_PER_DAY)
        assert 0.0 < r.max_drawdown_pct <= 1.0

    def test_it_measures_peak_to_trough_not_start_to_end(self):
        """Up, down hard, then partly back. The drawdown is from the peak."""
        r = run(make_plan(), oscillating(), bars_per_day=BARS_PER_DAY)
        curve = r.equity_curve
        worst = max((max(curve[:i + 1]) - e) / max(curve[:i + 1])
                    for i, e in enumerate(curve))
        assert r.max_drawdown_pct == pytest.approx(worst)


class TestHorizon:

    def test_days_follows_bars_per_day(self):
        closes = oscillating(n=241)
        hourly = run(make_plan(), closes, bars_per_day=24.0)
        daily = run(make_plan(), closes, bars_per_day=1.0)
        assert hourly.days == pytest.approx(10.0)
        assert daily.days == pytest.approx(240.0)

    def test_bars_per_day_is_required(self):
        """No default, for the same reason rubric.assess has none."""
        with pytest.raises(TypeError):
            run(make_plan(), oscillating())

    def test_a_nonsense_interval_is_refused(self):
        with pytest.raises(ValueError):
            run(make_plan(), oscillating(), bars_per_day=0)


class TestPurity:
    """Same inputs, same numbers, forever."""

    def test_two_runs_agree_exactly(self):
        s = oscillating()
        a = run(make_plan(), s, bars_per_day=BARS_PER_DAY)
        b = run(make_plan(), s, bars_per_day=BARS_PER_DAY)
        assert a == b

    def test_it_does_not_mutate_the_plan(self):
        p = make_plan()
        before = p.to_json()
        run(p, oscillating(), bars_per_day=BARS_PER_DAY)
        assert p.to_json() == before

    def test_it_does_not_mutate_the_prices(self):
        s = oscillating()
        copy = list(s)
        run(make_plan(), s, bars_per_day=BARS_PER_DAY)
        assert s == copy

    def test_the_module_imports_nothing_that_could_do_io(self):
        src = inspect.getsource(backtest)
        for banned in ("import requests", "import random", "open(",
                       "datetime.now", "import os", "import time"):
            assert banned not in src


class TestResultShape:

    @pytest.fixture
    def result(self):
        return run(make_plan(), oscillating(), bars_per_day=BARS_PER_DAY)

    def test_it_is_frozen(self, result):
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.n_cycles = 999

    def test_the_equity_curve_is_immutable(self, result):
        assert isinstance(result.equity_curve, tuple)

    def test_the_curve_starts_at_the_starting_capital(self, result):
        assert result.equity_curve[0] == 1300.0

    def test_one_curve_point_per_bar(self, result):
        assert len(result.equity_curve) == 300

    def test_return_agrees_with_the_curve(self, result):
        assert result.total_return_pct == pytest.approx(
            result.equity_curve[-1] / result.equity_curve[0] - 1.0)

    def test_to_metrics_is_all_floats_for_the_plan_field(self, result):
        m = result.to_metrics()
        assert all(isinstance(v, float) for v in m.values())
        GridPlan(**{**dataclasses.asdict(make_plan()), "backtest": m})

    def test_explain_mentions_the_outcome(self, result):
        assert "cycles" in result.explain()


class TestDegenerateInputs:

    def test_empty_prices(self):
        r = run(make_plan(), [], bars_per_day=BARS_PER_DAY)
        assert r.n_cycles == 0 and r.days == 0.0

    def test_a_single_price_cannot_cross_anything(self):
        r = run(make_plan(), [100.0], bars_per_day=BARS_PER_DAY)
        assert r.n_cycles == 0 and r.total_return_pct == 0.0

    def test_an_unbuildable_ladder_is_refused(self):
        with pytest.raises(ValueError):
            run(make_plan(lower=106.0, upper=94.0), oscillating(),
                bars_per_day=BARS_PER_DAY)

    def test_a_one_level_grid_is_refused(self):
        with pytest.raises(ValueError):
            run(make_plan(levels=1), oscillating(), bars_per_day=BARS_PER_DAY)


class TestAgreementWithTheRubric:
    """The backtester and the rubric must not disagree about the same series."""

    def test_the_series_the_rubric_likes_is_the_one_that_profits(self):
        osc = run(make_plan(), oscillating(), bars_per_day=BARS_PER_DAY)
        tr = run(make_plan(), falling(), bars_per_day=BARS_PER_DAY)
        assert osc.total_return_pct > 0 > tr.total_return_pct
        assert osc.n_cycles > tr.n_cycles


# ===========================================================================
# Walk-forward
# ===========================================================================

import statistics

from grid.backtest import WalkForwardResult, walk_forward

from test_metrics import hl_from_closes


def regime_change(drift=-0.004, n_osc=180, n_tr=120):
    """The fixture this whole feature exists to catch.

    First 60%: an OU oscillation — exactly the character the rubric selects for,
    and a grid fitted here will look excellent. Last 40%: a clean trend starting
    from the final oscillating price, so the join is continuous and the only
    thing that changes is the regime.

    The parameters are not wrong when they are chosen. They are wrong later,
    which is the whole problem with fitting and scoring on one stretch of data.
    """
    head = oscillating(n=n_osc)
    tail = trending(n=n_tr, start=head[-1], drift=drift, noise=0.004, seed=7)
    return head + tail


def stationary(n=400, seed=3):
    """A control: one regime throughout, so the fit SHOULD carry forward.

    Without this, every assertion about `generalises` would also pass against a
    function that returned False unconditionally.
    """
    return oscillating(n=n, seed=seed)


def fit_loses_then_market_recovers():
    """In-sample loss, out-of-sample gain — the case a literal reading of the rule mishandles.

    An oscillation that ends with a hard slide (the stop widened so it does not
    fire), then a normal oscillation back around the original centre. The fit
    loses money on its own training data; the unseen stretch happens to profit.
    """
    head = oscillating(n=190, seed=3)
    centre = statistics.median(head)
    slide = [head[-1] * (1 - 0.10 * i / 12) for i in range(1, 13)]
    tail = oscillating(n=130, seed=8)
    tail = [x - statistics.median(tail) + centre for x in tail]
    return head + slide + tail, (len(head) + len(slide))


def wf(closes, split=0.6, **kw):
    highs, lows = hl_from_closes(closes)
    return walk_forward(closes, highs, lows, "T-USDT", split=split,
                        bars_per_day=BARS_PER_DAY, **kw)


class TestRegimeChangeIsCaught:
    """Tuned on an oscillation, tested on a trend: must NOT generalise."""

    def test_the_fixture_really_does_change_regime(self):
        c = regime_change()
        head, tail = c[:180], c[180:]
        assert min(head) > min(tail)                  # the trend leaves the range
        assert abs(tail[-1] / tail[0] - 1) > 0.20     # and it is a strong one

    def test_the_fit_looks_good_in_sample(self):
        """The point of the test is that the in-sample number is attractive."""
        assert wf(regime_change()).in_sample.total_return_pct > 0

    def test_a_downtrend_does_not_generalise(self):
        r = wf(regime_change(drift=-0.004))
        assert r.generalises is False
        assert r.out_of_sample.total_return_pct < 0

    def test_the_downtrend_stops_the_grid_out(self):
        assert wf(regime_change(drift=-0.004)).out_of_sample.stopped_out is True

    def test_an_uptrend_does_not_generalise_either(self):
        """Price leaves the top of the range and simply never comes back, so the
        grid earns nothing further. Not a loss — but not the fitted return."""
        r = wf(regime_change(drift=+0.004))
        assert r.generalises is False
        assert r.out_of_sample.n_cycles == 0

    def test_the_reason_is_stated(self):
        r = wf(regime_change())
        assert r.reason
        assert "DOES NOT GENERALISE" in r.explain()


class TestPositiveControl:
    """One regime throughout: the verdict must be able to come back True."""

    def test_a_stationary_series_generalises(self):
        r = wf(stationary(), split=0.5)
        assert r.generalises is True
        assert r.reason == ""

    def test_it_kept_most_of_the_fitted_return(self):
        assert wf(stationary(), split=0.5).retention >= 0.5

    def test_both_halves_traded(self):
        r = wf(stationary(), split=0.5)
        assert r.in_sample.n_cycles > 0 and r.out_of_sample.n_cycles > 0

    def test_the_verdict_is_not_hardcoded(self):
        """The two fixtures differ only in their second half."""
        assert wf(stationary(), split=0.5).generalises is True
        assert wf(regime_change()).generalises is False


class TestTheVerdictRule:

    def test_positive_but_under_half_fails(self):
        """Profitable out of sample and still a fail: retention is under half.
        Making money is not the bar; keeping the fitted edge is."""
        r = wf(stationary(n=300, seed=4), split=0.6)
        assert r.out_of_sample.total_return_pct > 0
        assert 0 < r.retention < 0.5
        assert r.generalises is False
        assert "retained only" in r.reason

    def test_a_losing_fit_never_generalises(self):
        """in-sample -4.8%, out-of-sample +1.0%.

        Read literally, "positive AND at least half the in-sample return" is
        satisfied: +1.0% clears half of -4.8%, because half of a negative
        number is a negative bar. That would report a plan which lost money on
        its own training data as generalising. There is no edge to carry.
        """
        closes, n_in = fit_loses_then_market_recovers()
        r = wf(closes, split=n_in / len(closes), stop_pct=0.30)
        assert r.in_sample.total_return_pct < 0
        assert r.out_of_sample.total_return_pct > 0
        assert r.out_of_sample.total_return_pct > 0.5 * r.in_sample.total_return_pct
        assert r.generalises is False
        assert "lost money in-sample" in r.reason

    def test_retention_is_zero_when_the_fit_lost(self):
        closes, n_in = fit_loses_then_market_recovers()
        assert wf(closes, split=n_in / len(closes), stop_pct=0.30).retention == 0.0


class TestNothingLeaksFromOutOfSample:
    """Out of sample is a replay, not a re-fit. If anything is re-tuned, this is where it shows."""

    def test_the_plan_ignores_the_out_of_sample_half(self):
        head = oscillating(n=180)
        a = head + trending(n=120, start=head[-1], drift=-0.004, seed=7)
        b = head + trending(n=120, start=head[-1], drift=+0.004, seed=7)
        pa, pb = wf(a).plan, wf(b).plan
        assert (pa.lower, pa.upper, pa.levels) == (pb.lower, pb.upper, pb.levels)
        assert pa.stop_below == pb.stop_below

    def test_the_in_sample_result_ignores_it_too(self):
        head = oscillating(n=180)
        a = head + trending(n=120, start=head[-1], drift=-0.004, seed=7)
        b = head + trending(n=120, start=head[-1], drift=+0.004, seed=7)
        assert wf(a).in_sample == wf(b).in_sample

    def test_the_split_index_is_where_it_says(self):
        r = wf(regime_change())
        assert r.split_at == 180
        assert len(r.in_sample.equity_curve) == 180

    def test_split_controls_the_boundary(self):
        assert wf(stationary(n=400), split=0.75).split_at == 300


class TestHighsAndLowsStayOutOfTheFills:
    """They describe the regime. They must never produce a fill."""

    def test_absurd_wicks_do_not_change_either_result(self):
        closes = regime_change()
        tight = walk_forward(closes, *hl_from_closes(closes, spread=0.0001),
                             "T-USDT", bars_per_day=BARS_PER_DAY)
        wild = walk_forward(closes, *hl_from_closes(closes, spread=0.40),
                            "T-USDT", bars_per_day=BARS_PER_DAY)
        assert tight.in_sample == wild.in_sample
        assert tight.out_of_sample == wild.out_of_sample
        assert tight.generalises == wild.generalises

    def test_they_do_reach_the_recorded_metrics(self):
        """Confirming they are used for what they are for."""
        closes = regime_change()
        tight = walk_forward(closes, *hl_from_closes(closes, spread=0.0001),
                             "T-USDT", bars_per_day=BARS_PER_DAY)
        wild = walk_forward(closes, *hl_from_closes(closes, spread=0.40),
                            "T-USDT", bars_per_day=BARS_PER_DAY)
        assert wild.plan.metrics["atr_pct"] > tight.plan.metrics["atr_pct"]

    def test_mismatched_lengths_are_refused(self):
        closes = stationary(n=100)
        highs, lows = hl_from_closes(closes)
        with pytest.raises(ValueError, match="same length"):
            walk_forward(closes, highs[:-1], lows, "T", bars_per_day=BARS_PER_DAY)


class TestTheFittedPlan:

    @pytest.fixture
    def plan(self):
        return wf(stationary(), split=0.5).plan

    def test_it_is_valid(self, plan):
        assert plan.validate() == []

    def test_the_range_comes_from_suggest_range_on_the_in_sample_half(self):
        closes = stationary()
        r = wf(closes, split=0.5)
        from grid import metrics
        assert (r.plan.lower, r.plan.upper) == metrics.suggest_range(closes[:r.split_at])

    def test_spacing_clears_the_fee_floor_at_its_narrowest_point(self, plan):
        floor = FeeModel().min_viable_spacing(fee_share_of_gross=0.25)
        assert plan.min_spacing_pct >= floor

    def test_fees_stay_within_a_quarter_of_gross(self, plan):
        assert FeeModel().fee_share(plan.min_spacing_pct) <= 0.25

    def test_it_is_the_tightest_spacing_that_still_clears_the_floor(self, plan):
        """One more level would breach the fee cap — so no cycles are being
        left on the table for the sake of a round number."""
        floor = FeeModel().min_viable_spacing(fee_share_of_gross=0.25)
        tighter = GridPlan(**{**dataclasses.asdict(plan), "levels": plan.levels + 1})
        assert tighter.min_spacing_pct < floor

    def test_the_stop_sits_below_the_range(self, plan):
        assert plan.stop_below < plan.lower

    def test_the_ladder_is_fully_funded(self, plan):
        assert plan.max_quote_total == pytest.approx(
            len(plan.grid_prices) * plan.quote_per_level)

    def test_provenance_is_recorded(self, plan):
        assert plan.metrics["in_sample_bars"] > 0
        assert set(plan.metrics) >= {"hurst", "adx", "atr_pct", "efficiency_ratio"}
        assert "Walk-forward" in plan.thesis

    def test_the_plan_round_trips_as_json(self, plan):
        assert GridPlan.from_json(plan.to_json()).levels == plan.levels


class TestWalkForwardGuards:

    def test_split_must_be_a_fraction(self):
        for bad in (0.0, 1.0, -0.5, 2.0):
            with pytest.raises(ValueError, match="split"):
                wf(stationary(), split=bad)

    def test_too_few_in_sample_bars_is_refused(self):
        with pytest.raises(ValueError, match="in-sample"):
            wf(stationary(n=30), split=0.1)

    def test_too_few_out_of_sample_bars_is_refused(self):
        with pytest.raises(ValueError, match="out-of-sample"):
            wf(stationary(n=100), split=0.99)

    def test_bars_per_day_is_still_required(self):
        closes = stationary()
        highs, lows = hl_from_closes(closes)
        with pytest.raises(TypeError):
            walk_forward(closes, highs, lows, "T")

    def test_the_result_is_frozen(self):
        r = wf(stationary(), split=0.5)
        assert isinstance(r, WalkForwardResult)
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.generalises = True


# ===========================================================================
# Execution quality
# ===========================================================================

class TestSlippage:
    """Every fill is a little worse than its level, on both legs."""

    def test_a_buy_fills_above_its_level(self):
        r = run(make_plan(), [100.0, 98.5], bars_per_day=BARS_PER_DAY,
                slip=0.01, fill_margin=0.0, max_fills_per_bar=None)
        # Level 99 bought at 99 * 1.01; quote_per_level buys fewer units.
        assert r.final_units_held == pytest.approx(100.0 / (99.0 * 1.01))

    def test_a_sell_fills_below_its_level(self):
        naive = run(make_plan(), [100.0, 98.5, 100.5], bars_per_day=BARS_PER_DAY,
                    **NAIVE)
        slipped = run(make_plan(), [100.0, 98.5, 100.5],
                      bars_per_day=BARS_PER_DAY, slip=0.01, fill_margin=0.0,
                      max_fills_per_bar=None)
        assert slipped.gross_profit < naive.gross_profit

    def test_it_costs_money_on_both_legs(self):
        s = oscillating()
        naive = run(make_plan(), s, bars_per_day=BARS_PER_DAY, **NAIVE)
        slipped = run(make_plan(), s, bars_per_day=BARS_PER_DAY, slip=0.0005,
                      fill_margin=0.0, max_fills_per_bar=None)
        assert slipped.total_return_pct < naive.total_return_pct

    def test_more_slip_is_monotonically_worse(self):
        s = oscillating()
        rs = [run(make_plan(), s, bars_per_day=BARS_PER_DAY, slip=x,
                  fill_margin=0.0, max_fills_per_bar=None).total_return_pct
              for x in (0.0, 0.0005, 0.0015, 0.003)]
        assert rs == sorted(rs, reverse=True)

    def test_enough_slip_destroys_the_edge(self):
        """A cycle earns one step; slip bigger than the step cannot pay."""
        s = oscillating()
        r = run(make_plan(), s, bars_per_day=BARS_PER_DAY, slip=0.02,
                fill_margin=0.0, max_fills_per_bar=None)
        assert r.total_return_pct < 0

    def test_the_default_is_five_basis_points(self):
        assert backtest.DEFAULT_SLIP == 0.0005

    def test_a_negative_slip_is_refused(self):
        with pytest.raises(ValueError, match="negative"):
            run(make_plan(), oscillating(), bars_per_day=BARS_PER_DAY,
                slip=-0.001)


class TestFillMargin:
    """A close that merely touches a level is a queue position, not a fill."""

    def test_a_bare_touch_does_not_fill(self):
        """Closing exactly on 99 is not evidence of a fill."""
        r = run(make_plan(), [100.0, 99.0], bars_per_day=BARS_PER_DAY,
                slip=0.0, fill_margin=0.001, max_fills_per_bar=None)
        assert r.final_units_held == 0.0

    def test_crossing_by_the_margin_does_fill(self):
        r = run(make_plan(), [100.0, 98.0], bars_per_day=BARS_PER_DAY,
                slip=0.0, fill_margin=0.001, max_fills_per_bar=None)
        assert r.final_units_held > 0

    def test_zero_margin_restores_the_bare_touch(self):
        r = run(make_plan(), [100.0, 99.0], bars_per_day=BARS_PER_DAY, **NAIVE)
        assert r.final_units_held > 0

    def test_it_applies_to_sells_too(self):
        opened = run(make_plan(), [100.0, 98.0, 99.0], bars_per_day=BARS_PER_DAY,
                     slip=0.0, fill_margin=0.001, max_fills_per_bar=None)
        assert opened.n_cycles == 0          # 99 only touched, never cleared

    def test_a_bigger_margin_books_fewer_cycles(self):
        s = oscillating()
        loose = run(make_plan(), s, bars_per_day=BARS_PER_DAY, slip=0.0,
                    fill_margin=0.0, max_fills_per_bar=None).n_cycles
        tight = run(make_plan(), s, bars_per_day=BARS_PER_DAY, slip=0.0,
                    fill_margin=0.002, max_fills_per_bar=None).n_cycles
        assert tight < loose

    def test_a_negative_margin_is_refused(self):
        with pytest.raises(ValueError, match="negative"):
            run(make_plan(), oscillating(), bars_per_day=BARS_PER_DAY,
                fill_margin=-0.001)


class TestGapHandling:
    """A bar that sweeps several levels must not book them all."""

    def test_a_sweep_fills_one_level_by_default(self):
        """100 -> 95.5 crosses 96, 97, 98, 99."""
        r = run(make_plan(), [100.0, 95.5], bars_per_day=BARS_PER_DAY,
                slip=0.0, fill_margin=0.0, max_fills_per_bar=1)
        # One lot, not four: 96 only.
        assert r.final_units_held == pytest.approx(100.0 / 96.0)

    def test_the_level_it_picks_is_nearest_the_close(self):
        """96 is where price ended, not 99 where it started."""
        r = run(make_plan(), [100.0, 95.5], bars_per_day=BARS_PER_DAY,
                slip=0.0, fill_margin=0.0, max_fills_per_bar=1)
        assert r.final_units_held == pytest.approx(100.0 / 96.0)
        assert r.final_units_held != pytest.approx(100.0 / 99.0)

    def test_unlimited_restores_the_full_sweep(self):
        r = run(make_plan(), [100.0, 95.5], bars_per_day=BARS_PER_DAY,
                slip=0.0, fill_margin=0.0, max_fills_per_bar=None)
        expected = sum(100.0 / lv for lv in (96.0, 97.0, 98.0, 99.0))
        assert r.final_units_held == pytest.approx(expected)

    def test_two_per_bar_is_between_the_two(self):
        held = [run(make_plan(), [100.0, 95.5], bars_per_day=BARS_PER_DAY,
                    slip=0.0, fill_margin=0.0,
                    max_fills_per_bar=k).final_units_held
                for k in (1, 2, 4)]
        assert held[0] < held[1] < held[2]

    def test_a_cap_below_one_is_refused(self):
        with pytest.raises(ValueError, match="at least 1"):
            run(make_plan(), oscillating(), bars_per_day=BARS_PER_DAY,
                max_fills_per_bar=0)

    def test_the_cap_reduces_cycles_over_a_real_series(self):
        s = oscillating()
        capped = run(make_plan(), s, bars_per_day=BARS_PER_DAY, slip=0.0,
                     fill_margin=0.0, max_fills_per_bar=1).n_cycles
        free = run(make_plan(), s, bars_per_day=BARS_PER_DAY, **NAIVE).n_cycles
        assert capped <= free


class TestExecutionAssumptionsAreRecorded:

    def test_the_result_carries_them(self):
        r = run(make_plan(), oscillating(), bars_per_day=BARS_PER_DAY,
                slip=0.0015, fill_margin=0.0003, max_fills_per_bar=2)
        assert (r.slip, r.fill_margin, r.max_fills_per_bar) == (0.0015, 0.0003, 2)

    def test_they_reach_to_metrics(self):
        r = run(make_plan(), oscillating(), bars_per_day=BARS_PER_DAY, slip=0.0015)
        assert r.to_metrics()["slip"] == 0.0015

    def test_explain_states_the_slip(self):
        r = run(make_plan(), oscillating(), bars_per_day=BARS_PER_DAY, slip=0.0015)
        assert "0.15% slip" in r.explain()

    def test_walk_forward_uses_the_same_assumptions_on_both_halves(self):
        r = wf(stationary(), split=0.5)
        assert r.in_sample.slip == r.out_of_sample.slip
        assert r.in_sample.fill_margin == r.out_of_sample.fill_margin

    def test_walk_forward_passes_them_through(self):
        closes = stationary()
        highs, lows = hl_from_closes(closes)
        r = walk_forward(closes, highs, lows, "T-USDT", split=0.5,
                         bars_per_day=BARS_PER_DAY, slip=0.0015)
        assert r.out_of_sample.slip == 0.0015


class TestLevelsForSpacingByMode:

    def test_geometric_respects_the_floor(self):
        from grid.backtest import levels_for_spacing
        from grid.plan import GridPlan
        n = levels_for_spacing(100.0, 200.0, 0.01, "geometric")
        p = GridPlan(symbol="B-USDT", lower=100.0, upper=200.0, levels=n,
                     quote_per_level=10.0, stop_below=95.0,
                     max_quote_total=10_000.0)
        assert p.min_spacing_pct >= 0.01

    def test_one_more_level_would_breach_it(self):
        from grid.backtest import levels_for_spacing
        from grid.plan import GridPlan
        n = levels_for_spacing(100.0, 200.0, 0.01, "geometric")
        tighter = GridPlan(symbol="B-USDT", lower=100.0, upper=200.0,
                           levels=n + 1, quote_per_level=10.0, stop_below=95.0,
                           max_quote_total=10_000.0)
        assert tighter.min_spacing_pct < 0.01

    def test_arithmetic_still_respects_its_floor(self):
        from grid.backtest import levels_for_spacing
        from grid.plan import GridPlan
        n = levels_for_spacing(100.0, 200.0, 0.01, "arithmetic")
        p = GridPlan(symbol="B-USDT", lower=100.0, upper=200.0, levels=n,
                     quote_per_level=10.0, stop_below=95.0,
                     max_quote_total=10_000.0, spacing_mode="arithmetic")
        assert p.min_spacing_pct >= 0.01

    def test_geometric_fits_more_levels_in_the_same_band(self):
        """Because no rung has to be wide enough to carry the whole top."""
        from grid.backtest import levels_for_spacing
        assert (levels_for_spacing(100.0, 200.0, 0.01, "geometric")
                > levels_for_spacing(100.0, 200.0, 0.01, "arithmetic"))

    def test_walk_forward_builds_the_mode_it_is_asked_for(self):
        closes = stationary()
        highs, lows = hl_from_closes(closes)
        for mode in ("geometric", "arithmetic"):
            r = walk_forward(closes, highs, lows, "T-USDT", split=0.5,
                             bars_per_day=BARS_PER_DAY, spacing_mode=mode)
            assert r.plan.spacing_mode == mode

    def test_walk_forward_defaults_to_geometric(self):
        closes = stationary()
        highs, lows = hl_from_closes(closes)
        r = walk_forward(closes, highs, lows, "T-USDT", split=0.5,
                         bars_per_day=BARS_PER_DAY)
        assert r.plan.spacing_mode == "geometric"


# --------------------------------------------------------------- two windows
#
# The band and the statistics are measured over deliberately different
# stretches of history. These tests pin that split, because collapsing it back
# to one window is a silent change that makes every backtest look better and
# every proposed band untradeable.

def _regime_series(n_old=2000, n_recent=1000):
    """A series that trades 100-200 for a long time, then settles at 50-55.

    The long window sees a 100-200 band that price has entirely left. The short
    window sees the band price is actually in.
    """
    import math
    closes, highs, lows = [], [], []
    for i in range(n_old):
        p = 150.0 + 50.0 * math.sin(i / 7.0)
        closes.append(p); highs.append(p * 1.01); lows.append(p * 0.99)
    for i in range(n_recent):
        p = 52.5 + 2.5 * math.sin(i / 5.0)
        closes.append(p); highs.append(p * 1.01); lows.append(p * 0.99)
    return closes, highs, lows


def test_band_comes_from_the_recent_window_not_the_whole_history():
    """The defect this split exists to fix: a band spanning dead regimes."""
    closes, highs, lows = _regime_series()
    # split so the whole recent regime is in-sample
    wf_long = backtest.walk_forward(closes, highs, lows, "X-USDT", split=0.9,
                                    bars_per_day=24.0, range_days=None)
    wf_short = backtest.walk_forward(closes, highs, lows, "X-USDT", split=0.9,
                                     bars_per_day=24.0, range_days=10.0)

    # The long window's band spans the abandoned 100-200 regime.
    assert wf_long.plan.upper / wf_long.plan.lower > 2.0
    # The short window's band is the one price is actually in.
    assert wf_short.plan.upper / wf_short.plan.lower < 1.3
    assert 45.0 < wf_short.plan.lower < 55.0
    assert 50.0 < wf_short.plan.upper < 60.0


def test_range_window_never_reaches_into_out_of_sample():
    """The band is fitted on recent data, but never on UNSEEN data."""
    closes, highs, lows = _regime_series()
    n = len(closes)
    wf = backtest.walk_forward(closes, highs, lows, "X-USDT", split=0.6,
                               bars_per_day=24.0, range_days=45.0)
    # The range window ends at the split, not after it.
    assert wf.n_bars_range <= wf.split_at
    assert wf.split_at == int(n * 0.6)
    # And the band is inside the range of the in-sample prices only.
    in_closes = closes[:wf.split_at]
    assert wf.plan.lower >= min(in_closes) * 0.999
    assert wf.plan.upper <= max(in_closes) * 1.001


def test_statistics_window_stays_long_while_the_band_goes_short():
    closes, highs, lows = _regime_series()
    wf = backtest.walk_forward(closes, highs, lows, "X-USDT", split=0.9,
                               bars_per_day=24.0, range_days=10.0)
    assert wf.n_bars_statistics == wf.split_at
    assert wf.n_bars_range == 240            # 10 days x 24 bars
    assert wf.n_bars_statistics > wf.n_bars_range
    # Recorded on the plan, both of them.
    assert wf.plan.metrics["statistics_bars"] == float(wf.n_bars_statistics)
    assert wf.plan.metrics["range_bars"] == float(wf.n_bars_range)


def test_range_window_falls_back_to_what_history_exists():
    """A short history fits on all of it rather than refusing."""
    closes, highs, lows = _regime_series(n_old=0, n_recent=200)
    wf = backtest.walk_forward(closes, highs, lows, "X-USDT", split=0.6,
                               bars_per_day=24.0, range_days=45.0)
    # 45 days would be 1080 bars; only 120 in-sample bars exist.
    assert wf.n_bars_range == wf.split_at == 120


def test_windows_are_reported_with_dates_and_bar_counts():
    closes, highs, lows = _regime_series()
    n = len(closes)
    step = 3_600_000
    ts = [1_700_000_000_000 + i * step for i in range(n)]
    wf = backtest.walk_forward(closes, highs, lows, "X-USDT", split=0.9,
                               bars_per_day=24.0, range_days=10.0,
                               timestamps=ts)
    line = wf.windows()
    assert "stats" in line and "range" in line
    assert f"{wf.n_bars_statistics} bars" in line
    assert f"{wf.n_bars_range} bars" in line
    assert wf.range_from and wf.range_to and wf.statistics_from
    # Dates appear in the explain() output too.
    assert "windows:" in wf.explain()
    # The range window ends where the statistics window ends.
    assert wf.range_to == wf.statistics_to
    # ...and starts later than it.
    assert wf.range_from > wf.statistics_from


def test_atr_is_measured_on_the_range_window():
    """ATR sets the stop, so it belongs to the band's window, not the long one."""
    closes, highs, lows = _regime_series()
    long_wf = backtest.walk_forward(closes, highs, lows, "X-USDT", split=0.9,
                                    bars_per_day=24.0, range_days=None)
    short_wf = backtest.walk_forward(closes, highs, lows, "X-USDT", split=0.9,
                                     bars_per_day=24.0, range_days=10.0)
    # The old regime swings +-50 on 150; the recent one +-2.5 on 52.5. Percentage
    # ATR is genuinely lower in the recent window, and the split must show it.
    assert short_wf.plan.metrics["atr_pct"] < long_wf.plan.metrics["atr_pct"]


def test_the_short_range_window_is_the_DEFAULT_not_an_opt_in():
    """A caller who passes nothing must still get the split.

    If this ever regresses to the long window by default, every backtest in the
    repo silently starts reporting the flattering numbers again.
    """
    assert backtest.DEFAULT_RANGE_DAYS == 45.0
    # The recent regime must be LONGER than the 1080-bar range window, or the
    # window reaches back past the regime change and legitimately spans both.
    closes, highs, lows = _regime_series(n_old=2000, n_recent=1500)
    wf = backtest.walk_forward(closes, highs, lows, "X-USDT", split=0.9,
                               bars_per_day=24.0)          # no range_days
    assert wf.n_bars_range == int(backtest.DEFAULT_RANGE_DAYS * 24)
    assert wf.n_bars_range < wf.n_bars_statistics
    # The whole range window sits inside the recent regime.
    assert wf.split_at - wf.n_bars_range >= 2000
    # And the band is the recent regime, not the union of both.
    assert wf.plan.upper / wf.plan.lower < 1.3


# ------------------------------------------------------------ rolling re-fit
#
# A single-fit backtest holds one band for the whole out-of-sample stretch.
# That is not the strategy — live, the analyst re-scans and proposes a fresh
# grid — so a stale-band artefact was being reported as a result.

def _drifting(n=3000, start=100.0, step=0.02):
    """A slow uptrend with oscillation: price leaves any fixed band."""
    import math
    closes = [start + step * i + 3.0 * math.sin(i / 9.0) for i in range(n)]
    highs = [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    return closes, highs, lows


def test_refitting_collapses_the_time_spent_outside_the_band():
    """THE ARTEFACT THIS FIXES, measured as its own number."""
    closes, highs, lows = _drifting()
    start = len(closes) - 1500

    held = backtest.rolling_walk_forward(
        closes, highs, lows, "X-USDT", bars_per_day=24.0, start_at=start,
        refit_days=1e6)                       # one band, never re-fitted
    refit = backtest.rolling_walk_forward(
        closes, highs, lows, "X-USDT", bars_per_day=24.0, start_at=start,
        refit_days=14.0)

    assert held.n_refits == 1
    assert refit.n_refits >= 4      # 1500 bars / (14d x 24) = 5 segments
    # Staleness collapses...
    assert refit.outside_band_pct < held.outside_band_pct
    # ...and the grid actually trades again.
    assert refit.n_cycles > held.n_cycles
    # Both replayed the same span, so the comparison is like-for-like.
    assert refit.bars_replayed == held.bars_replayed


def test_a_refit_never_sees_the_bars_it_then_replays():
    """Rolling origin, but still strictly out of sample."""
    closes, highs, lows = _drifting()
    start = len(closes) - 1000
    r = backtest.rolling_walk_forward(
        closes, highs, lows, "X-USDT", bars_per_day=24.0, start_at=start,
        refit_days=14.0, range_days=10.0)

    seg_bars = int(round(14.0 * 24))
    range_bars = int(round(10.0 * 24))
    for k, (lower, upper) in enumerate(r.bands):
        origin = start + k * seg_bars
        window = closes[max(0, origin - range_bars):origin]
        # The band is inside the range of prices that PRECEDED the segment.
        assert lower >= min(window) * 0.999
        assert upper <= max(window) * 1.001


def test_the_inventory_rule_is_explicit_and_costs_something():
    """Liquidating at each re-fit pays taker. Carrying hides that cost."""
    closes, highs, lows = _drifting()
    start = len(closes) - 1500
    liq = backtest.rolling_walk_forward(
        closes, highs, lows, "X-USDT", bars_per_day=24.0, start_at=start,
        inventory_rule=backtest.LIQUIDATE)
    carry = backtest.rolling_walk_forward(
        closes, highs, lows, "X-USDT", bars_per_day=24.0, start_at=start,
        inventory_rule=backtest.CARRY)

    assert liq.inventory_rule == backtest.LIQUIDATE
    assert liq.refit_costs > 0, "tearing down a grid must cost taker fees"
    assert carry.refit_costs == 0
    # The default is the one that pays.
    assert backtest.rolling_walk_forward(
        closes, highs, lows, "X-USDT", bars_per_day=24.0,
        start_at=start).inventory_rule == backtest.LIQUIDATE

    with pytest.raises(ValueError):
        backtest.rolling_walk_forward(closes, highs, lows, "X-USDT",
                                      bars_per_day=24.0, start_at=start,
                                      inventory_rule="pretend it never happened")


def test_bars_outside_band_counts_the_whole_span_not_just_replayed_bars():
    """A segment that stops out early still spent those bars outside."""
    closes, highs, lows = _drifting()
    start = len(closes) - 1500
    r = backtest.rolling_walk_forward(
        closes, highs, lows, "X-USDT", bars_per_day=24.0, start_at=start,
        refit_days=1e6)
    assert r.bars_replayed == len(closes) - start
    assert 0 < r.bars_outside_band <= r.bars_replayed


def test_bars_outside_band_is_measured_on_a_single_run_too():
    plan = GridPlan(symbol="T-USDT", lower=94.0, upper=106.0, levels=12,
                    quote_per_level=100.0, stop_below=50.0,
                    max_quote_total=1300.0, spacing_mode="arithmetic")
    closes = [100.0] * 10 + [130.0] * 20 + [100.0] * 10
    r = backtest.run(plan, closes, bars_per_day=24.0)
    assert r.bars_outside_band == 20
    assert r.bars_replayed == len(closes) - 1


def test_rolling_refit_is_the_DEFAULT_not_an_opt_in():
    """A caller who passes nothing must get re-fitting.

    If this regresses to a single fit, every rolling backtest silently starts
    measuring band staleness again and reporting it as a strategy result.
    """
    assert backtest.DEFAULT_REFIT_DAYS == 14.0
    closes, highs, lows = _drifting()
    start = len(closes) - 1500
    r = backtest.rolling_walk_forward(closes, highs, lows, "X-USDT",
                                      bars_per_day=24.0, start_at=start)
    assert r.n_refits >= 4, "the default must re-fit, not hold one band"
    expected = int(round(backtest.DEFAULT_REFIT_DAYS * 24))
    assert len(r.bands) == r.n_refits
    # Segments are the documented length.
    assert r.bars_replayed == 1500
    assert r.n_refits == -(-1500 // expected)


# ------------------------------------------- per-rung sizing and signal exits

def _flat_plan():
    return GridPlan(symbol="T-USDT", lower=94.0, upper=106.0, levels=12,
                    quote_per_level=100.0, stop_below=50.0,
                    max_quote_total=1300.0, spacing_mode="arithmetic")


def front_loaded(n, ratio=0.85):
    """Largest at the top rung, decreasing geometrically toward the bottom.

    Ladder index 0 is the LOWEST price, so the weights ascend with index.
    """
    return [ratio ** (n - 1 - i) for i in range(n)]


class TestLevelWeights:

    def test_the_weights_themselves_are_normalised_to_mean_one(self):
        """The invariant, tested on the weights rather than through a replay.

        `run` scales whatever it is given to mean 1.0, so the ladder's nominal
        commitment — `quote_per_level x (levels + 1)` — is identical to a flat
        one. Only the distribution across rungs moves.
        """
        p = _flat_plan()
        n = len(p.grid_prices)
        raw = front_loaded(n)
        scaled = [w * n / sum(raw) for w in raw]
        assert sum(scaled) == pytest.approx(n)
        assert sum(w * p.quote_per_level for w in scaled) == pytest.approx(
            p.max_quote_total)
        # Front-loaded really does mean largest at the top.
        assert scaled[-1] > scaled[0]
        assert scaled == sorted(scaled)

    def test_the_same_capital_is_deployed_when_cash_is_not_the_binding_limit(self):
        """With headroom, flat and front-loaded spend the same total.

        The plan is given more cash than the ladder needs. Without that, both
        runs stop at the cash cap but at DIFFERENT rungs — front-loading spends
        more at the top and runs out sooner — and the comparison measures the
        cap rather than the weighting.
        """
        p = GridPlan(symbol="T-USDT", lower=94.0, upper=106.0, levels=12,
                     quote_per_level=100.0, stop_below=50.0,
                     max_quote_total=2000.0, spacing_mode="arithmetic")
        # Starts ABOVE the ladder: a buy needs `level < previous close`, so a
        # sweep starting at 106.0 never fills the 106.0 rung — and that is
        # exactly the rung front-loading puts its largest weight on.
        closes = [110.0, 93.0]          # one sweep clean through every rung
        kw = dict(bars_per_day=24.0, slip=0.0, fill_margin=0.0,
                  max_fills_per_bar=None, max_inventory=1e9)
        flat = run(p, closes, **kw)
        weighted = run(p, closes, level_weights=front_loaded(
            len(p.grid_prices)), **kw)

        assert flat.peak_inventory > 0, "fixture must actually fill"
        flat_spent = p.max_quote_total - flat.equity_curve[-1] + \
            flat.final_units_held * closes[-1]
        weighted_spent = p.max_quote_total - weighted.equity_curve[-1] + \
            weighted.final_units_held * closes[-1]
        assert flat_spent == pytest.approx(weighted_spent, rel=0.01)
        # Same money, fewer units: it bought more high and less low, which is
        # the whole claim variant C makes.
        assert weighted.final_units_held < flat.final_units_held

    def test_front_loading_accumulates_less_inventory_on_the_way_down(self):
        """THE CLAIM VARIANT C MAKES. Buying small at the bottom means fewer
        units held at the worst prices."""
        p = _flat_plan()
        closes = [106.0 - 0.7 * i for i in range(20)]      # straight decline
        flat = run(p, closes, bars_per_day=24.0, slip=0.0, fill_margin=0.0,
                   max_fills_per_bar=None, max_inventory=1e9)
        weighted = run(p, closes, bars_per_day=24.0, slip=0.0, fill_margin=0.0,
                       max_fills_per_bar=None, max_inventory=1e9,
                       level_weights=front_loaded(len(p.grid_prices)))
        assert weighted.peak_inventory < flat.peak_inventory

    def test_a_uniform_weighting_is_identical_to_no_weighting(self):
        p = _flat_plan()
        closes = [106.0 - 0.7 * i for i in range(20)]
        a = run(p, closes, bars_per_day=24.0, max_inventory=1e9)
        b = run(p, closes, bars_per_day=24.0, max_inventory=1e9,
                level_weights=[1.0] * len(p.grid_prices))
        assert a.total_return_pct == pytest.approx(b.total_return_pct)
        assert a.peak_inventory == pytest.approx(b.peak_inventory)

    def test_a_wrong_length_is_rejected(self):
        with pytest.raises(ValueError, match="level_weights"):
            run(_flat_plan(), [100.0, 99.0], bars_per_day=24.0,
                level_weights=[1.0, 2.0])

    def test_negative_weights_are_rejected(self):
        p = _flat_plan()
        with pytest.raises(ValueError, match="negative"):
            run(p, [100.0, 99.0], bars_per_day=24.0,
                level_weights=[-1.0] * len(p.grid_prices))


class TestSignalledExit:

    def test_it_liquidates_and_stops(self):
        p = _flat_plan()
        closes = [106.0 - 0.7 * i for i in range(20)]
        flags = [False] * len(closes)
        flags[10] = True
        r = run(p, closes, bars_per_day=24.0, exit_signal=flags)
        assert r.signalled_exit is True
        assert r.bars_replayed == 10
        # It really had stock, and really got rid of it.
        assert run(p, closes[:11], bars_per_day=24.0).final_units_held > 0
        assert r.final_units_held == 0.0

    def test_it_pays_the_taker_fee(self):
        """A signalled exit is a market order, not a resting limit."""
        p = _flat_plan()
        closes = [106.0 - 0.7 * i for i in range(20)]
        flags = [False] * len(closes)
        flags[10] = True
        exited = run(p, closes, bars_per_day=24.0, exit_signal=flags)
        held = run(p, closes[:11], bars_per_day=24.0)
        assert held.final_units_held > 0, "fixture must hold stock to liquidate"
        # Same bars, but the exit crossed the spread on the way out.
        assert exited.fees_paid > held.fees_paid

    def test_the_signal_beats_the_price_stop_on_the_same_bar(self):
        """Leaving on evidence is the point; it must not be pre-empted."""
        p = GridPlan(symbol="T-USDT", lower=94.0, upper=106.0, levels=12,
                     quote_per_level=100.0, stop_below=100.0,
                     max_quote_total=1300.0, spacing_mode="arithmetic")
        closes = [106.0, 99.0]        # bar 1 is below the stop
        flags = [False, True]
        r = run(p, closes, bars_per_day=24.0, exit_signal=flags)
        assert r.signalled_exit is True
        assert r.stopped_out is False

    def test_no_signal_changes_nothing(self):
        p = _flat_plan()
        closes = [106.0 - 0.7 * i for i in range(20)]
        a = run(p, closes, bars_per_day=24.0)
        b = run(p, closes, bars_per_day=24.0,
                exit_signal=[False] * len(closes))
        assert a.total_return_pct == pytest.approx(b.total_return_pct)
        assert b.signalled_exit is False

    def test_a_wrong_length_is_rejected(self):
        with pytest.raises(ValueError, match="exit_signal"):
            run(_flat_plan(), [100.0, 99.0, 98.0], bars_per_day=24.0,
                exit_signal=[False, True])
