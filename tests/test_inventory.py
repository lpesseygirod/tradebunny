"""Inventory-aware sizing: the hard bound, and size skewing."""

import pytest

from grid import inventory
from grid.backtest import run
from grid.inventory import (assess, buy_multiplier, max_inventory_for,
                            sell_multiplier)
from grid.plan import GridPlan

NAIVE = dict(slip=0.0, fill_margin=0.0, max_fills_per_bar=None)


def plan(**over):
    kw = dict(symbol="T-USDT", lower=94.0, upper=106.0, levels=12,
              quote_per_level=100.0, stop_below=90.0, max_quote_total=1300.0,
              spacing_mode="arithmetic")
    kw.update(over)
    return GridPlan(**kw)


def straight_down(start=106.0, stop=91.0, step=0.4):
    out, px = [start], start
    while px > stop:
        px -= step
        out.append(px)
    return out


class TestTheHardBound:
    """GLFT: a market maker at maximum inventory does not quote that side."""

    def test_it_stops_buying_at_q_max(self):
        free = run(plan(), straight_down(), bars_per_day=24.0,
                   max_inventory=1e9, **NAIVE)
        bound = run(plan(), straight_down(), bars_per_day=24.0,
                    max_inventory=5.0, **NAIVE)
        assert free.peak_inventory > 10.0
        assert bound.peak_inventory < 6.5

    def test_it_records_the_bars_it_refused_to_buy(self):
        r = run(plan(), straight_down(), bars_per_day=24.0, max_inventory=5.0,
                **NAIVE)
        assert r.bars_at_bound > 0

    def test_a_generous_bound_changes_nothing(self):
        a = run(plan(), straight_down(), bars_per_day=24.0, max_inventory=1e9,
                **NAIVE)
        b = run(plan(), straight_down(), bars_per_day=24.0,
                max_inventory=1e8, **NAIVE)
        assert a.peak_inventory == pytest.approx(b.peak_inventory)

    def test_the_DEFAULT_bound_actually_binds(self):
        """THE POINT OF THE BOUND. A bound that never engages is decorative.

        Previously `Q_max` was `max_quote_total / lower`, reachable only if
        every rung filled at exactly the bottom price. `bars_at_bound` was zero
        on all 29 pairs of the universe. It is now derived from the real
        maximum, so on a decline through every rung it must engage.
        """
        bounded = run(plan(), straight_down(), bars_per_day=24.0, **NAIVE)
        unbounded = run(plan(), straight_down(), bars_per_day=24.0,
                        max_inventory=1e9, **NAIVE)
        assert bounded.bars_at_bound > 0
        assert bounded.peak_inventory < unbounded.peak_inventory

    def test_the_overshoot_is_at_most_one_lot(self):
        """The bound stops INITIATING past Q_max; a fill in flight cannot be
        partially refused, so inventory can end one lot above it."""
        q_max = 5.0
        r = run(plan(), straight_down(), bars_per_day=24.0,
                max_inventory=q_max, **NAIVE)
        one_lot = plan().quote_per_level / plan().lower
        assert r.peak_inventory <= q_max + one_lot

    def test_a_tighter_bound_holds_less(self):
        tight = run(plan(), straight_down(), bars_per_day=24.0,
                    max_inventory=3.0, **NAIVE)
        loose = run(plan(), straight_down(), bars_per_day=24.0,
                    max_inventory=9.0, **NAIVE)
        assert tight.peak_inventory < loose.peak_inventory

    def test_it_resumes_buying_as_inventory_falls(self):
        """The bound is a gate, not a latch."""
        closes = straight_down() + [96.0, 100.0, 104.0, 100.0, 96.0, 93.0]
        r = run(plan(), closes, bars_per_day=24.0, max_inventory=5.0, **NAIVE)
        assert r.n_cycles > 0            # it sold, then bought again

    def test_the_bound_is_recorded_on_the_result(self):
        r = run(plan(), straight_down(), bars_per_day=24.0, max_inventory=5.0,
                **NAIVE)
        assert r.max_inventory == 5.0

    def test_the_default_bound_is_a_fraction_of_the_REAL_maximum(self):
        p = plan()
        real_max = sum(p.quote_per_level / px for px in p.grid_prices)
        assert max_inventory_for(p) == pytest.approx(
            real_max * inventory.BOUND_FRACTION)

    def test_the_old_formula_overstated_the_maximum(self):
        """Why the bound never bound: the old Q_max was unreachable.

        `max_quote_total / lower` assumes every rung fills at the BOTTOM of the
        range. Every rung above the bottom produces fewer units than that, so
        the real maximum is strictly smaller and the old bound sat above
        anything the grid could reach.
        """
        p = plan()
        old = p.max_quote_total / p.lower
        real_max = sum(p.quote_per_level / px for px in p.grid_prices)
        assert real_max < old

    def test_an_explicit_bound_wins(self):
        assert max_inventory_for(plan(max_inventory=3.0)) == 3.0

    def test_a_fraction_tightens_it(self):
        p = plan()
        assert max_inventory_for(p, fraction=0.5) == pytest.approx(
            max_inventory_for(p, fraction=1.0) / 2)

    def test_the_bound_fraction_is_the_documented_default(self):
        assert inventory.BOUND_FRACTION == 0.6
        p = plan()
        assert max_inventory_for(p) == pytest.approx(
            max_inventory_for(p, fraction=inventory.BOUND_FRACTION))


class TestBuyMultiplier:

    def test_full_size_below_the_bound_without_skew(self):
        assert buy_multiplier(1.0, 10.0, skew=False) == 1.0

    def test_zero_at_the_bound(self):
        assert buy_multiplier(10.0, 10.0, skew=False) == 0.0

    def test_zero_past_the_bound(self):
        assert buy_multiplier(12.0, 10.0, skew=False) == 0.0

    def test_skewing_ramps_linearly(self):
        assert buy_multiplier(0.0, 10.0, skew=True) == pytest.approx(1.0)
        assert buy_multiplier(5.0, 10.0, skew=True) == pytest.approx(0.5)
        assert buy_multiplier(10.0, 10.0, skew=True) == pytest.approx(0.0)

    def test_skewing_subsumes_the_bound(self):
        """It reaches exactly zero at Q_max, so the bound needs no separate
        check when skewing is on."""
        assert buy_multiplier(10.0, 10.0, skew=True) == 0.0

    def test_it_never_goes_negative(self):
        assert buy_multiplier(50.0, 10.0, skew=True) == 0.0

    def test_a_zero_bound_quotes_nothing(self):
        assert buy_multiplier(0.0, 0.0) == 0.0


class TestSellMultiplier:

    def test_unskewed_is_always_one(self):
        for q in (0.0, 5.0, 50.0):
            assert sell_multiplier(q, 10.0, skew=False) == 1.0

    def test_skewing_grows_with_inventory(self):
        assert sell_multiplier(0.0, 10.0, True) == pytest.approx(1.0)
        assert sell_multiplier(5.0, 10.0, True) == pytest.approx(1.5)
        assert sell_multiplier(10.0, 10.0, True) == pytest.approx(2.0)

    def test_it_is_capped_at_double(self):
        """Unbounded above if Q_max is ever misconfigured small."""
        assert sell_multiplier(1000.0, 1.0, True) == 2.0

    def test_it_never_goes_negative(self):
        assert sell_multiplier(-100.0, 10.0, True) >= 0.0


class TestSkewingInTheBacktest:

    def test_it_is_off_by_default(self):
        import inspect
        assert inspect.signature(run).parameters["size_skew"].default is False

    def test_skewing_holds_less_inventory(self):
        flat = run(plan(), straight_down(), bars_per_day=24.0,
                   max_inventory=5.0, size_skew=False, **NAIVE)
        skewed = run(plan(), straight_down(), bars_per_day=24.0,
                     max_inventory=5.0, size_skew=True, **NAIVE)
        assert skewed.peak_inventory < flat.peak_inventory

    def test_skewing_never_reaches_the_hard_bound(self):
        """Because size ramps to zero before it, smoothly."""
        r = run(plan(), straight_down(), bars_per_day=24.0, max_inventory=5.0,
                size_skew=True, **NAIVE)
        assert r.bars_at_bound == 0

    def test_it_is_recorded_on_the_result(self):
        r = run(plan(), straight_down(), bars_per_day=24.0, size_skew=True,
                **NAIVE)
        assert r.size_skew is True


class TestInventoryState:

    def test_it_reports_utilisation(self):
        assert assess(5.0, 10.0).utilisation == pytest.approx(0.5)

    def test_at_the_bound_it_says_so_loudly(self):
        s = assess(10.0, 10.0)
        assert s.at_bound and "AT MAX INVENTORY" in s.explain()

    def test_below_the_bound_it_reports_the_ratio(self):
        assert "50%" in assess(5.0, 10.0).explain()

    def test_skewed_it_reports_both_multipliers(self):
        assert "x0.50" in assess(5.0, 10.0, skew=True).explain()


class TestTheBoundActuallyBinds:
    """A bound that never binds must fail its own test.

    Before this, `Q_max = max_quote_total / lower` was reachable only if every
    rung filled at exactly the bottom price. Across the 29-pair universe and
    2 500 out-of-sample bars, `bars_at_bound` was zero on every single pair —
    the one thing standing between the account and an unbounded left tail was
    not attached to anything.
    """

    def test_bars_at_bound_is_positive_on_a_straight_decline(self):
        """THE REGRESSION TEST. A decline through every rung must hit it."""
        r = run(plan(), straight_down(), bars_per_day=24.0, **NAIVE)
        assert r.bars_at_bound > 0, (
            "the inventory bound did not engage on a straight decline through "
            "every rung — it is decorative, not a constraint")

    def test_the_bound_caps_inventory_below_the_unbounded_run(self):
        bounded = run(plan(), straight_down(), bars_per_day=24.0, **NAIVE)
        unbounded = run(plan(), straight_down(), bars_per_day=24.0,
                        max_inventory=1e9, **NAIVE)
        assert bounded.peak_inventory < unbounded.peak_inventory
        assert bounded.peak_inventory <= bounded.max_inventory * 1.5

    def test_the_old_formula_would_fail_this_test(self):
        """Pin the defect so it cannot come back silently."""
        p = plan()
        old_q_max = p.max_quote_total / p.lower       # the formula that failed
        r = run(p, straight_down(), bars_per_day=24.0,
                max_inventory=old_q_max, **NAIVE)
        assert r.bars_at_bound == 0, (
            "the old formula is supposed to be unreachable — if this now "
            "binds, the premise of the fix has changed")
