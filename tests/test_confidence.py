"""Tests for the graded confidence score and its calibration."""

import pytest

from grid import confidence as conf


class TestWeights:

    def test_they_sum_to_one(self):
        assert sum(conf.WEIGHTS.values()) == pytest.approx(1.0)

    def test_adf_carries_the_most(self):
        """It is the only component that tests a hypothesis."""
        assert conf.WEIGHTS["adf"] == max(conf.WEIGHTS.values())

    def test_the_correlated_descriptives_share_a_small_weight(self):
        """ADX and efficiency are not independent of Hurst — all three come
        from the same few hundred numbers."""
        assert (conf.WEIGHTS["adx"] + conf.WEIGHTS["efficiency"]
                < conf.WEIGHTS["hurst"])

    def test_stability_is_weighted_like_hurst(self):
        assert conf.WEIGHTS["stability"] == conf.WEIGHTS["hurst"]


class TestLevels:

    def test_the_three_levels(self):
        assert conf.LEVELS == {"strict": 0.65, "balanced": 0.50,
                               "exploratory": 0.35}

    def test_strict_is_the_default(self):
        assert conf.DEFAULT_LEVEL == "strict"

    def test_an_unknown_level_raises(self):
        with pytest.raises(ValueError, match="unknown permissivity"):
            conf.threshold_for("yolo")


class TestComponentScores:

    def test_adf_is_graded_not_binary(self):
        assert conf.score_adf(0.0001) > conf.score_adf(0.01) > conf.score_adf(0.04)

    def test_adf_at_or_above_the_alpha_scores_zero(self):
        assert conf.score_adf(0.05) == 0.0 and conf.score_adf(0.9) == 0.0

    def test_hurst_uses_the_whole_interval(self):
        """A CI that straddles the gate scores zero however good the point."""
        assert conf.score_hurst(0.10, 0.52) == 0.0
        assert conf.score_hurst(0.10, 0.20) > 0.5

    def test_a_tighter_interval_scores_higher(self):
        assert conf.score_hurst(0.10, 0.30) > conf.score_hurst(0.10, 0.42)

    def test_stability_is_zero_at_the_floor_and_one_at_perfect(self):
        assert conf.score_stability(0.70) == 0.0
        assert conf.score_stability(1.0) == 1.0

    def test_a_failed_walk_forward_scores_zero(self):
        assert conf.score_walk_forward(0.05, 0.10, generalises=False) == 0.0

    def test_a_negative_out_of_sample_scores_zero(self):
        assert conf.score_walk_forward(-0.01, 0.10, generalises=True) == 0.0

    def test_retaining_more_scores_higher(self):
        assert (conf.score_walk_forward(0.09, 0.10, True)
                > conf.score_walk_forward(0.06, 0.10, True))

    def test_missing_inputs_return_none(self):
        assert conf.score_adf(None) is None
        assert conf.score_hurst(None, None) is None
        assert conf.score_stability(None) is None


class TestCompute:

    def strong(self):
        return {"adf_p": 0.0005, "hurst_ci_low": 0.05, "hurst_ci_high": 0.20,
                "stability_pass": 1.0, "adx": 10.0, "efficiency_ratio": 0.05}

    def weak(self):
        return {"adf_p": 0.049, "hurst_ci_low": 0.30, "hurst_ci_high": 0.449,
                "stability_pass": 0.70, "adx": 24.9, "efficiency_ratio": 0.29}

    def test_strong_evidence_scores_high(self):
        c = conf.compute(self.strong(), oos_return=0.10, in_sample_return=0.10,
                         generalises=True)
        assert c.score > 0.9

    def test_weak_evidence_scores_low(self):
        c = conf.compute(self.weak(), oos_return=0.001, in_sample_return=0.10,
                         generalises=True)
        assert c.score < 0.35

    def test_the_score_is_bounded(self):
        for m in (self.strong(), self.weak(), {}):
            assert 0.0 <= conf.compute(m).score <= 1.0

    def test_missing_components_score_zero_and_are_named(self):
        """Absence of evidence is not neutral."""
        c = conf.compute({})
        assert c.score == 0.0
        assert set(c.missing) == set(conf.WEIGHTS)

    def test_every_component_is_reported(self):
        assert set(conf.compute(self.strong()).components) == set(conf.WEIGHTS)

    def test_a_failed_walk_forward_zeroes_that_component(self):
        m = self.strong()
        good = conf.compute(m, oos_return=0.1, in_sample_return=0.1,
                            generalises=True)
        bad = conf.compute(m, oos_return=0.1, in_sample_return=0.1,
                           generalises=False)
        assert bad.components["walk_forward"] == 0.0
        # The whole of whatever that component was worth is lost.
        assert good.score - bad.score == pytest.approx(
            conf.WEIGHTS["walk_forward"] * good.components["walk_forward"])

    def test_retaining_exactly_the_fitted_return_is_middling_not_perfect(self):
        """The ramp runs 0.5x retention -> 0, 1.5x -> 1. Matching the fit
        exactly is a pass, not a triumph."""
        assert conf.score_walk_forward(0.10, 0.10, True) == pytest.approx(0.5)
        assert conf.score_walk_forward(0.15, 0.10, True) == pytest.approx(1.0)

    def test_the_thresholds_discriminate(self):
        strong = conf.compute(self.strong(), oos_return=0.1,
                              in_sample_return=0.1, generalises=True)
        weak = conf.compute(self.weak(), oos_return=0.001,
                            in_sample_return=0.10, generalises=True)
        assert strong.passes("strict")
        assert not weak.passes("exploratory")

    def test_explain_lists_the_components(self):
        assert "adf" in conf.compute(self.strong()).explain()


class TestCalibration:
    """An uncalibrated confidence number is decoration."""

    def test_no_outcomes_is_uncalibrated(self):
        c = conf.calibration([], [])
        assert not c.is_meaningful and "UNCALIBRATED" in c.verdict

    def test_a_small_sample_is_uncalibrated(self):
        c = conf.calibration([0.7] * 5, [True] * 5)
        assert not c.is_meaningful
        assert "need 20" in c.verdict

    def test_it_reuses_the_ledger_brier(self):
        from ledger.scoring import brier_score
        confs = [0.7] * 25
        hits = [True] * 18 + [False] * 7
        assert conf.calibration(confs, hits).brier == pytest.approx(
            brier_score(confs, hits))

    def test_a_useless_forecast_is_called_out(self):
        """Always saying 50% scores 0.25 — anything at or above that carries
        no information."""
        c = conf.calibration([0.5] * 30, [True] * 15 + [False] * 15)
        assert c.brier >= 0.25
        assert "NO INFORMATION" in c.verdict

    def test_a_good_forecast_is_recognised(self):
        c = conf.calibration([0.9] * 30, [True] * 28 + [False] * 2)
        assert c.brier < 0.25 and "calibrated" in c.verdict

    def test_buckets_are_reported(self):
        c = conf.calibration([0.3, 0.5, 0.7, 0.9] * 8, [True, False] * 16)
        assert c.buckets
