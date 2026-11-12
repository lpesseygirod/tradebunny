"""Tests for the scoring maths. These run with no network and must always pass."""

import pytest

from ledger.scoring import (Scorecard, brier_score, build_scorecard,
                            calibration_buckets, excess_return, sample_size_warning)


class TestExcessReturn:
    def test_beating_the_benchmark_is_a_hit(self):
        # stock +10%, benchmark +2%  ->  +8% gross, +7.9% after 10bps
        o = excess_return(100, 110, 100, 102, "long")
        assert o.asset_return_pct == pytest.approx(10.0)
        assert o.benchmark_return_pct == pytest.approx(2.0)
        assert o.net_excess_pct == pytest.approx(7.9)
        assert o.hit

    def test_rising_less_than_the_benchmark_is_a_miss(self):
        """The core rule: going up is not enough."""
        o = excess_return(100, 101.1, 100, 102.4, "long")
        assert o.asset_return_pct > 0        # the stock rose
        assert not o.hit                     # and it still failed
        assert o.net_excess_pct < 0

    def test_short_direction_flips_the_sign(self):
        o = excess_return(100, 90, 100, 100, "short")
        assert o.net_excess_pct == pytest.approx(9.9)
        assert o.hit

    def test_short_is_scored_against_a_MOVING_benchmark(self):
        """With a flat benchmark, -(asset - bench) and -asset - bench are
        indistinguishable, so the previous short tests would pass even with the
        sign flip written wrongly. Only a moving benchmark separates them.

        Stock -10%, benchmark +5%: a short earned 15% relative, less costs."""
        o = excess_return(100, 90, 100, 105, "short")
        assert o.net_excess_pct == pytest.approx(14.9)
        assert o.hit

    def test_short_that_rises_is_a_miss(self):
        assert not excess_return(100, 110, 100, 100, "short").hit

    def test_costs_can_turn_a_marginal_win_into_a_loss(self):
        """A 0.05% edge does not survive 10bps of friction."""
        o = excess_return(100, 100.05, 100, 100, "long", cost_bps=10)
        assert o.gross_excess_pct > 0
        assert not o.hit

    def test_rejects_bad_direction(self):
        with pytest.raises(ValueError):
            excess_return(100, 110, 100, 102, "sideways")

    @pytest.mark.parametrize("bad", [0, -5, None])
    def test_rejects_non_positive_prices(self, bad):
        with pytest.raises(ValueError):
            excess_return(bad, 110, 100, 102, "long")


class TestBrier:
    def test_perfect_confident_forecaster_scores_zero(self):
        assert brier_score([1.0, 1.0, 0.0], [True, True, False]) == pytest.approx(0.0)

    def test_always_fifty_percent_scores_a_quarter(self):
        """0.25 is the useless baseline every other score is judged against."""
        assert brier_score([0.5] * 4, [True, False, True, False]) == pytest.approx(0.25)

    def test_confidently_wrong_scores_worst(self):
        assert brier_score([0.9], [False]) == pytest.approx(0.81)

    def test_empty_is_none_not_zero(self):
        assert brier_score([], []) is None

    def test_rejects_mismatched_lengths(self):
        with pytest.raises(ValueError):
            brier_score([0.5, 0.6], [True])

    def test_rejects_out_of_range_confidence(self):
        with pytest.raises(ValueError):
            brier_score([1.4], [True])


class TestCalibration:
    def test_buckets_report_stated_versus_realised(self):
        b = calibration_buckets([0.55, 0.58, 0.85, 0.88],
                                [True, False, True, True], n_buckets=5)
        mid = [x for x in b if x["range"] == (0.4, 0.6)][0]
        top = [x for x in b if x["range"] == (0.8, 1.0)][0]
        assert mid["n"] == 2 and mid["realised"] == pytest.approx(0.5)
        assert top["n"] == 2 and top["realised"] == pytest.approx(1.0)

    def test_empty_buckets_are_none_not_zero(self):
        """Asserts the exact bucket counts, not `all(... for x in b if x["n"] == 0)`
        — that filtered on a value the code under test produced, so it passed
        vacuously if every bucket was wrongly non-empty."""
        b = calibration_buckets([0.55], [True], n_buckets=5)
        assert [x["n"] for x in b] == [0, 0, 1, 0, 0]
        assert b[2]["realised"] == pytest.approx(1.0)
        assert all(b[i]["realised"] is None for i in (0, 1, 3, 4))

    def test_round_confidences_land_in_the_right_bucket(self):
        """3 * 0.2 is 0.6000000000000001, so p=0.6 satisfied
        `0.4 <= 0.6 < 0.6000000000000001` and fell a bucket too low — visible as a
        bucket labelled (0.4, 0.6) reporting a stated confidence of 0.6."""
        b = calibration_buckets([0.6], [True], n_buckets=5)
        assert [x["n"] for x in b] == [0, 0, 0, 1, 0]
        assert b[3]["stated"] == pytest.approx(0.6)

    @pytest.mark.parametrize("p,expected", [(0.3, 3), (0.7, 7), (0.5, 5), (1.0, 9)])
    def test_boundary_values_at_ten_buckets(self, p, expected):
        b = calibration_buckets([p], [True], n_buckets=10)
        assert b[expected]["n"] == 1


class TestScorecard:
    rows = [
        dict(net_excess_pct=-1.3, hit=False, confidence=0.62, decision="bought",
             twin_net_excess_pct=0.4, twin_hit=True),
        dict(net_excess_pct=1.8, hit=True, confidence=0.58, decision="skipped",
             twin_net_excess_pct=-0.9, twin_hit=False),
        dict(net_excess_pct=2.1, hit=True, confidence=0.55, decision="bought",
             twin_net_excess_pct=-0.3, twin_hit=False),
        dict(net_excess_pct=-2.2, hit=False, confidence=0.60, decision="skipped",
             twin_net_excess_pct=0.6, twin_hit=True),
    ]

    def test_aggregates(self):
        c = build_scorecard(self.rows)
        assert c.n == 4
        assert c.hit_rate == pytest.approx(0.5)
        assert c.mean_net_excess == pytest.approx(0.1)
        assert c.twin_mean_net_excess == pytest.approx(-0.05)
        assert c.beats_twin is True

    def test_filter_value_measures_the_owners_judgement(self):
        c = build_scorecard(self.rows)
        assert c.taken == 2
        assert c.taken_mean_net_excess == pytest.approx(0.4)
        assert c.filter_value == pytest.approx(0.3)   # her picks beat the average

    def test_unresolved_rows_are_excluded(self):
        c = build_scorecard(self.rows + [dict(net_excess_pct=None, hit=None,
                                              confidence=0.7, decision="pending")])
        assert c.n == 4

    def test_empty_is_safe(self):
        c = build_scorecard([])
        assert c.n == 0 and c.hit_rate is None and c.beats_twin is None


class TestSampleSizeWarning:
    def test_warns_hard_below_thirty(self):
        assert "far too few" in sample_size_warning(12)

    def test_warns_below_one_hundred(self):
        assert "kill criteria" in sample_size_warning(60)

    def test_silent_once_meaningful(self):
        assert sample_size_warning(150) is None
