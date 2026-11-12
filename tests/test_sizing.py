"""Tests for Kelly sizing, minimum viable capital and the minSz gate."""

import pytest

from grid import sizing
from grid.okx import Instrument
from grid.plan import GridPlan
from grid.sizing import (KELLY_MULTIPLIER, MIN_CAPITAL_SAFETY,
                         NEGATIVE_KELLY_WARNING, kelly_fraction,
                         min_viable_capital, order_size_problems, recommend)


def plan(**over):
    kw = dict(symbol="BTC-USDT", lower=100.0, upper=120.0, levels=10,
              quote_per_level=100.0, stop_below=90.0, max_quote_total=1100.0)
    kw.update(over)
    return GridPlan(**kw)


def inst(**over):
    kw = dict(inst_id="BTC-USDT", min_size=0.00001, lot_size=0.00000001,
              tick_size=0.1, base="BTC", quote="USDT", state="live")
    kw.update(over)
    return Instrument(**kw)


class TestKellyFraction:

    def test_the_formula(self):
        # b = 0.10/0.05 = 2; f = (0.6*2 - 0.4)/2 = 0.4
        assert kelly_fraction(0.10, 0.05, 0.6) == pytest.approx(0.4)

    def test_a_fair_coin_on_even_money_is_zero(self):
        assert kelly_fraction(0.10, 0.10, 0.5) == pytest.approx(0.0)

    def test_an_edge_gives_a_positive_fraction(self):
        assert kelly_fraction(0.10, 0.10, 0.6) > 0

    def test_no_edge_gives_a_negative_fraction(self):
        assert kelly_fraction(0.10, 0.10, 0.4) < 0

    def test_higher_confidence_bets_more(self):
        assert kelly_fraction(0.1, 0.05, 0.8) > kelly_fraction(0.1, 0.05, 0.6)

    def test_a_bigger_loss_bets_less(self):
        assert kelly_fraction(0.1, 0.20, 0.6) < kelly_fraction(0.1, 0.05, 0.6)

    def test_zero_upside_is_maximally_negative(self):
        assert kelly_fraction(0.0, 0.10, 0.9) == -1.0

    def test_a_zero_loss_is_refused(self):
        with pytest.raises(ValueError, match="loss_pct"):
            kelly_fraction(0.1, 0.0, 0.6)

    def test_a_probability_outside_zero_one_is_refused(self):
        for bad in (-0.1, 1.5):
            with pytest.raises(ValueError, match="probability"):
                kelly_fraction(0.1, 0.05, bad)

    def test_it_returns_full_kelly_not_the_fraction_acted_on(self):
        """The multiplier is applied by the caller, deliberately."""
        assert KELLY_MULTIPLIER == 0.25
        assert kelly_fraction(0.10, 0.05, 0.6) == pytest.approx(0.4)


class TestMinViableCapital:

    def test_it_counts_every_rung_and_the_safety_multiple(self):
        p = plan(levels=10)                     # 11 rungs
        got = min_viable_capital(p, price=100.0, min_size=0.001)
        assert got == pytest.approx(11 * 0.001 * 100.0 * MIN_CAPITAL_SAFETY)

    def test_the_safety_multiple_is_one_and_a_half(self):
        assert MIN_CAPITAL_SAFETY == 1.5

    def test_a_bigger_minimum_needs_more_capital(self):
        assert (min_viable_capital(plan(), 100.0, 0.01)
                > min_viable_capital(plan(), 100.0, 0.001))

    def test_a_higher_price_needs_more_capital(self):
        assert (min_viable_capital(plan(), 1000.0, 0.001)
                > min_viable_capital(plan(), 100.0, 0.001))

    def test_nonsense_inputs_are_refused(self):
        for price, ms in ((0.0, 0.001), (100.0, 0.0)):
            with pytest.raises(ValueError):
                min_viable_capital(plan(), price, ms)


class TestTheMinSizeGate:
    """A HARD gate. An order under minSz is rejected by the exchange."""

    def test_a_normal_plan_passes(self):
        assert order_size_problems(plan(), 110.0, inst()) == []

    def test_a_sub_minsz_plan_is_rejected(self):
        """DOGE-like: minSz is 10 base units. At the top rung (0.10) an order of
        0.50 buys 5 DOGE — half the minimum, so the exchange rejects it."""
        p = plan(symbol="DOGE-USDT", lower=0.08, upper=0.10,
                 quote_per_level=0.5, stop_below=0.07, max_quote_total=5.5)
        problems = order_size_problems(p, 0.09, inst(inst_id="DOGE-USDT",
                                                     min_size=10.0, base="DOGE"))
        assert problems and "below the exchange minimum" in problems[0]

    def test_exactly_at_the_minimum_is_accepted(self):
        """1.00 at the top rung buys exactly 10 DOGE — the boundary passes."""
        p = plan(symbol="DOGE-USDT", lower=0.08, upper=0.10,
                 quote_per_level=1.0, stop_below=0.07, max_quote_total=11.0)
        assert order_size_problems(p, 0.09, inst(min_size=10.0)) == []

    def test_it_measures_the_TOP_of_the_ladder(self):
        """A fixed quote amount buys fewest units where price is highest, so
        the top rung is the one that fails first."""
        p = plan(lower=100.0, upper=1000.0, levels=10, quote_per_level=50.0,
                 stop_below=90.0, max_quote_total=550.0)
        # 50 buys 0.05 at 1000 but 0.5 at 100 — a min_size of 0.1 fails only
        # at the top.
        assert order_size_problems(p, 500.0, inst(min_size=0.1))

    def test_the_message_says_what_is_needed(self):
        p = plan(symbol="DOGE-USDT", quote_per_level=1.0)
        problems = order_size_problems(p, 0.09, inst(min_size=10.0))
        assert "Needs at least" in problems[0]

    def test_a_missing_instrument_is_a_rejection_not_a_pass(self):
        """Unknown limits mean unknown acceptance. Fail closed."""
        assert order_size_problems(plan(), 110.0, None)

    def test_a_delisted_instrument_is_rejected(self):
        assert any("not live" in x
                   for x in order_size_problems(plan(), 110.0,
                                                inst(state="suspend")))

    def test_no_price_is_a_rejection(self):
        assert order_size_problems(plan(), 0.0, inst())


class TestRecommendation:
    """`loss_fraction=1.0` throughout: with a whole-stake loss, Kelly's
    fraction-at-risk and the amount deployed coincide, which isolates these
    tests from the conversion that TestKellyIsAFractionAtRisk covers."""

    def base(self, **over):
        kw = dict(available_quote=10_000.0, risk_size=800.0, kelly_full=0.30,
                  max_total_quote=5_000.0, max_concurrent_grids=3,
                  min_capital=100.0, loss_fraction=1.0)
        kw.update(over)
        return recommend(**kw)

    def test_it_takes_the_minimum_of_every_constraint(self):
        r = self.base()
        assert r.recommended == min(r.constraints.values())

    def test_it_names_the_binding_constraint(self):
        r = self.base(kelly_full=0.30)          # quarter = 0.075 -> 750
        assert "Kelly" in r.binding and r.recommended == pytest.approx(750.0)

    def test_the_risk_constraint_can_bind(self):
        r = self.base(kelly_full=2.0, risk_size=200.0)
        assert r.binding == "risk-based" and r.recommended == 200.0

    def test_the_exposure_cap_can_bind(self):
        r = self.base(kelly_full=4.0, risk_size=1e9, max_total_quote=300.0)
        assert r.binding == "exposure cap"

    def test_the_concurrency_share_can_bind(self):
        r = self.base(kelly_full=4.0, risk_size=1e9, max_total_quote=1e9,
                      max_concurrent_grids=10)
        assert r.binding == "concurrency share"
        assert r.recommended == pytest.approx(1000.0)

    def test_every_constraint_is_reported(self):
        assert set(self.base().constraints) == {
            "risk-based", "exposure cap", "concurrency share", "quarter Kelly"}

    def test_explain_shows_which_binds(self):
        assert "<-- binds" in self.base().explain()


class TestNegativeKelly:
    """'the label must be unmissable' — and it must not silently zero out."""

    def neg(self, **over):
        kw = dict(available_quote=10_000.0, risk_size=800.0, kelly_full=-0.4,
                  max_total_quote=5_000.0, max_concurrent_grids=3,
                  min_capital=100.0, loss_fraction=1.0)
        kw.update(over)
        return recommend(**kw)

    def test_it_is_flagged(self):
        assert self.neg().negative_kelly is True

    def test_the_warning_is_present_and_loud(self):
        r = self.neg()
        assert any("DO NOT TAKE THIS" in w for w in r.warnings)
        assert NEGATIVE_KELLY_WARNING in r.warnings

    def test_the_candidate_is_still_sized_by_the_other_constraints(self):
        """Not zeroed — the owner asked to still see these."""
        r = self.neg()
        assert r.recommended > 0
        assert r.recommended == 800.0        # the risk constraint

    def test_the_binding_label_mentions_kelly(self):
        assert "Kelly negative" in self.neg().binding

    def test_the_negative_number_is_not_replaced_with_a_positive_one(self):
        r = self.neg()
        assert r.kelly_full == -0.4 and r.kelly_quarter == pytest.approx(-0.1)

    def test_a_positive_kelly_carries_no_warning(self):
        assert recommend(available_quote=1e4, risk_size=800.0, kelly_full=0.3,
                         max_total_quote=5e3, max_concurrent_grids=3,
                         min_capital=100.0,
                         loss_fraction=1.0).warnings == ()


class TestTheCapitalFloor:

    def test_a_grid_below_the_floor_cannot_run(self):
        r = recommend(available_quote=1000.0, risk_size=50.0, kelly_full=0.3,
                      max_total_quote=5000.0, max_concurrent_grids=3,
                      min_capital=500.0, loss_fraction=1.0)
        assert r.fundable is False and r.recommended == 0.0

    def test_it_says_why(self):
        r = recommend(available_quote=1000.0, risk_size=50.0, kelly_full=0.3,
                      max_total_quote=5000.0, max_concurrent_grids=3,
                      min_capital=500.0, loss_fraction=1.0)
        assert any("cannot be run at this capital" in w for w in r.warnings)

    def test_the_floor_is_never_raised_to_meet_the_caps(self):
        """It reports impossibility rather than overriding a cap."""
        r = recommend(available_quote=1000.0, risk_size=50.0, kelly_full=0.3,
                      max_total_quote=5000.0, max_concurrent_grids=3,
                      min_capital=500.0, loss_fraction=1.0)
        assert r.recommended < r.min_capital


class TestKellyIsAFractionAtRisk:
    """The 10x bug: f* is what to RISK, not what to deploy.

    A grid stop-out loses `loss_fraction` of what is deployed, not all of it,
    so deploying `f* x capital` risked only a tenth of the intended bet.
    """

    def test_the_brief_case(self):
        """quarter_kelly 0.02, capital 2000, loss_fraction 0.10 -> 400."""
        r = recommend(available_quote=2000.0, risk_size=1e9, kelly_full=0.08,
                      max_total_quote=1e9, max_concurrent_grids=1,
                      min_capital=0.1, loss_fraction=0.10)
        assert r.kelly_quarter == pytest.approx(0.02)
        assert r.constraints["quarter Kelly"] == pytest.approx(400.0)

    def test_it_is_not_forty(self):
        """The old answer, kept as an explicit regression guard."""
        r = recommend(available_quote=2000.0, risk_size=1e9, kelly_full=0.08,
                      max_total_quote=1e9, max_concurrent_grids=1,
                      min_capital=0.1, loss_fraction=0.10)
        assert r.constraints["quarter Kelly"] != pytest.approx(40.0)

    def test_the_amount_at_risk_is_what_kelly_asked_for(self):
        for lf in (0.05, 0.10, 0.25):
            r = recommend(available_quote=2000.0, risk_size=1e9,
                          kelly_full=0.08, max_total_quote=1e9,
                          max_concurrent_grids=1, min_capital=0.1,
                          loss_fraction=lf)
            at_risk = r.constraints["quarter Kelly"] * lf
            assert at_risk == pytest.approx(0.02 * 2000.0)

    def test_a_tighter_stop_deploys_more(self):
        """Same risk budget, less lost per unit deployed."""
        tight = recommend(available_quote=2000.0, risk_size=1e9,
                          kelly_full=0.08, max_total_quote=1e9,
                          max_concurrent_grids=1, min_capital=0.1,
                          loss_fraction=0.05)
        wide = recommend(available_quote=2000.0, risk_size=1e9,
                         kelly_full=0.08, max_total_quote=1e9,
                         max_concurrent_grids=1, min_capital=0.1,
                         loss_fraction=0.20)
        assert (tight.constraints["quarter Kelly"]
                > wide.constraints["quarter Kelly"])

    def test_the_loss_fraction_is_recorded(self):
        r = recommend(available_quote=2000.0, risk_size=1e9, kelly_full=0.08,
                      max_total_quote=1e9, max_concurrent_grids=1,
                      min_capital=0.1, loss_fraction=0.10)
        assert r.loss_fraction == 0.10
        assert "deployed" in r.explain()

    def test_a_zero_loss_fraction_is_refused(self):
        with pytest.raises(ValueError, match="loss_fraction"):
            recommend(available_quote=2000.0, risk_size=1e9, kelly_full=0.08,
                      max_total_quote=1e9, max_concurrent_grids=1,
                      min_capital=0.1, loss_fraction=0.0)

    def test_a_real_stop_out_loses_about_the_kelly_budget(self):
        """End to end: build the plan the fixed maths asks for, replay it into
        a stop-out, and check what actually came out of the account."""
        from grid.backtest import run as replay

        capital, loss_fraction = 2000.0, 0.10
        r = recommend(available_quote=capital, risk_size=1e9, kelly_full=0.08,
                      max_total_quote=1e9, max_concurrent_grids=1,
                      min_capital=0.1, loss_fraction=loss_fraction)
        deployed = r.constraints["quarter Kelly"]
        assert deployed == pytest.approx(400.0)

        # A band whose stop sits ~10% under the average entry, matching the
        # loss_fraction the sizing assumed.
        upper, stop = 110.0, 90.0            # avg entry 100 -> 10% to the stop
        levels = 10
        per = deployed / (levels + 1)
        p = GridPlan(symbol="T-USDT", lower=95.0, upper=upper, levels=levels,
                     quote_per_level=per, stop_below=stop,
                     max_quote_total=per * (levels + 1))
        closes = [upper]
        px = upper
        while px > stop * 0.98:
            px -= 0.25
            closes.append(px)
        res = replay(p, closes, bars_per_day=24.0, slip=0.0, fill_margin=0.0,
                     max_fills_per_bar=None)
        assert res.stopped_out
        loss = p.max_quote_total - res.equity_curve[-1]
        assert loss == pytest.approx(40.0, rel=0.30), (
            f"stop-out cost {loss:.2f}, Kelly budgeted 40")
