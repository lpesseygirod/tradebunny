"""Calibration mode, execution quality, and show-every-rejection."""

import pytest

from grid import execution, sizing
from grid.execution import ExecutionQuality, Fill, FILLS_REQUIRED
from grid.sizing import CALIBRATION_LABEL, calibration_size


class TestCalibrationSizing:
    """Size by feasibility, not growth. Kelly is untouched beside it."""

    def test_the_formula(self):
        c = calibration_size(50.0, min_size_quote=1.0, levels_that_fit=22)
        assert c.quote_per_level == pytest.approx(1.5)      # minSz x 1.5
        assert c.levels == 22
        assert c.total == pytest.approx(33.0)

    def test_capital_caps_the_level_count(self):
        c = calibration_size(20.0, min_size_quote=1.0, levels_that_fit=22)
        assert c.levels == 13 and c.total <= 20.0

    def test_the_range_caps_the_level_count(self):
        c = calibration_size(1000.0, min_size_quote=1.0, levels_that_fit=6)
        assert c.levels == 6

    def test_total_never_exceeds_capital(self):
        for cap in (5.0, 17.0, 50.0, 500.0):
            c = calibration_size(cap, 1.0, 22)
            if c.feasible:
                assert c.total <= cap

    def test_too_little_capital_is_infeasible_not_silently_tiny(self):
        c = calibration_size(2.0, min_size_quote=1.0, levels_that_fit=22)
        assert not c.feasible and "at least 2" in c.reason

    def test_it_suggests_a_smaller_minimum(self):
        c = calibration_size(2.0, min_size_quote=1.0, levels_that_fit=22)
        assert "smaller minimum order size" in c.reason

    def test_a_lower_minsz_pair_fits_more_levels(self):
        """'Prefer pairs with the lowest minSz.'"""
        cheap = calibration_size(50.0, 0.5, 40)
        dear = calibration_size(50.0, 5.0, 40)
        assert cheap.levels > dear.levels

    def test_no_capital_is_infeasible(self):
        assert not calibration_size(0.0, 1.0, 22).feasible

    def test_an_unknown_minimum_is_infeasible(self):
        assert not calibration_size(50.0, 0.0, 22).feasible

    def test_it_does_not_touch_kelly(self):
        """Beside it, not instead of it."""
        assert sizing.KELLY_MULTIPLIER == 0.25
        assert sizing.kelly_fraction(0.1, 0.05, 0.6) == pytest.approx(0.4)


class TestTheCalibrationLabel:

    def test_it_says_what_it_is(self):
        assert "CALIBRATION RUN" in CALIBRATION_LABEL
        assert "runnable, not optimal" in CALIBRATION_LABEL

    def test_it_says_pnl_is_noise(self):
        assert "noise" in CALIBRATION_LABEL

    def test_it_names_the_things_being_measured(self):
        for word in ("fill rate", "slippage", "fee accuracy", "uptime"):
            assert word in CALIBRATION_LABEL


class TestExecutionQuality:

    def q(self, n_fills=0, placed=0, slip=0.0):
        e = ExecutionQuality()
        e.record_placed(placed)
        for i in range(n_fills):
            e.record_fill(Fill(symbol="A-USDT", side="buy",
                               intended_price=100.0,
                               actual_price=100.0 * (1 + slip), size=1.0,
                               fee_paid=0.08, fee_predicted=0.08))
        return e

    def test_fill_rate(self):
        assert self.q(n_fills=5, placed=10).fill_rate == pytest.approx(0.5)

    def test_fill_rate_is_unknown_before_any_order(self):
        assert ExecutionQuality().fill_rate is None

    def test_slippage_is_positive_when_worse(self):
        assert self.q(n_fills=3, placed=3, slip=0.001).mean_slippage > 0

    def test_a_sell_filling_low_is_also_positive_slippage(self):
        e = ExecutionQuality()
        e.record_fill(Fill(symbol="A", side="sell", intended_price=100.0,
                           actual_price=99.0, size=1.0, fee_paid=0.0,
                           fee_predicted=0.0))
        assert e.mean_slippage > 0

    def test_fee_error_against_the_model(self):
        e = ExecutionQuality()
        e.record_fill(Fill(symbol="A", side="buy", intended_price=100.0,
                           actual_price=100.0, size=1.0, fee_paid=0.10,
                           fee_predicted=0.08))
        assert e.mean_fee_error == pytest.approx(0.25)

    def test_uptime(self):
        e = ExecutionQuality()
        for ok in (True, True, True, False):
            e.record_pass(ok)
        assert e.uptime == pytest.approx(0.75)

    def test_reconnects_are_counted(self):
        e = ExecutionQuality()
        e.record_reconnect()
        assert e.reconnects == 1

    def test_paper_fills_are_flagged(self):
        assert self.q(n_fills=2, placed=2).all_paper

    def test_the_paper_caveat_is_stated(self):
        assert "simulator working, not the market" in self.q(2, 2).explain()

    def test_it_persists_and_reloads(self, tmp_path):
        e = self.q(n_fills=3, placed=6)
        p = e.save(tmp_path / "exec.json")
        back = execution.load(p)
        assert back.n_fills == 3 and back.orders_placed == 6

    def test_a_corrupt_file_reads_as_empty(self, tmp_path):
        p = tmp_path / "exec.json"
        p.write_text("{not json")
        assert execution.load(p).n_fills == 0


class TestTheFiftyFillGate:
    """'Refuse to switch until 50 fills are recorded.'"""

    def test_the_requirement_is_fifty(self):
        assert FILLS_REQUIRED == 50

    def test_below_it_is_not_ready(self):
        e = ExecutionQuality()
        for _ in range(49):
            e.record_fill(Fill("A", "buy", 1.0, 1.0, 1.0, 0.0, 0.0))
        assert not e.ready_for_kelly and e.fills_remaining == 1

    def test_at_it_is_ready(self):
        e = ExecutionQuality()
        for _ in range(50):
            e.record_fill(Fill("A", "buy", 1.0, 1.0, 1.0, 0.0, 0.0))
        assert e.ready_for_kelly and e.fills_remaining == 0

    def test_the_requirement_is_stated_in_words(self):
        e = ExecutionQuality()
        assert "0 of 50 fills" in e.readiness()
        assert "50 more" in e.readiness()

    def test_it_explains_why_fifty(self):
        assert "coin flip" in ExecutionQuality().readiness()

    def test_when_ready_it_says_so(self):
        e = ExecutionQuality()
        for _ in range(50):
            e.record_fill(Fill("A", "buy", 1.0, 1.0, 1.0, 0.0, 0.0))
        assert "enough to switch" in e.readiness()
