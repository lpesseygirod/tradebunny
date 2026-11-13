"""Tests for the selection metrics.

Each is checked against a synthetic series whose character is known by construction:
a trend must score as trending, an oscillation must score as mean-reverting. If these
pass, the rubric is measuring what it claims to measure.
"""

import math
import random

import pytest

from grid import metrics
from grid.fees import FeeModel
from grid.rubric import Thresholds, assess, rank


# ----------------------------------------------------------------- generators

def trending(n=300, start=100.0, drift=0.004, noise=0.004, seed=1):
    r = random.Random(seed)
    p, out = start, []
    for _ in range(n):
        p *= (1 + drift + r.gauss(0, noise))
        out.append(p)
    return out


def oscillating(n=300, centre=100.0, theta=0.15, sigma=1.2, seed=4, **_):
    """Mean-reverting: an Ornstein-Uhlenbeck process — the textbook case.

    Deliberately NOT a sine wave. A smooth sine is locally *trending* (it moves
    steadily through its cycle), so it reads as trending on any horizon shorter
    than its period. OU reverts at every horizon, which is what a grid needs.
    """
    r = random.Random(seed)
    p, out = centre, []
    for _ in range(n):
        p += theta * (centre - p) + r.gauss(0, sigma)
        out.append(p)
    return out


def smooth_sine(n=300, centre=100.0, amp=6.0, period=25, noise=0.4, seed=2):
    """Kept to document the horizon trap in the variance-ratio tests."""
    r = random.Random(seed)
    return [centre + amp * math.sin(2 * math.pi * i / period) + r.gauss(0, noise)
            for i in range(n)]


def random_walk(n=300, start=100.0, noise=0.01, seed=3):
    r = random.Random(seed)
    p, out = start, []
    for _ in range(n):
        p *= (1 + r.gauss(0, noise))
        out.append(p)
    return out


def hl_from_closes(closes, spread=0.004):
    highs = [c * (1 + spread) for c in closes]
    lows = [c * (1 - spread) for c in closes]
    return highs, lows


# --------------------------------------------------------------------- tests

class TestHurst:
    def test_trending_series_scores_above_half(self):
        assert metrics.hurst(trending()) > 0.55

    def test_oscillating_series_scores_below_half(self):
        assert metrics.hurst(oscillating()) < 0.45

    def test_random_walk_lands_near_half(self):
        h = metrics.hurst(random_walk())
        assert 0.35 < h < 0.65

    def test_trend_scores_higher_than_oscillation(self):
        """The comparison the rubric actually relies on."""
        assert metrics.hurst(trending()) > metrics.hurst(oscillating())

    def test_returns_none_when_too_short(self):
        assert metrics.hurst([100.0] * 20) is None


class TestEfficiencyRatio:
    def test_straight_line_is_maximally_efficient(self):
        assert metrics.efficiency_ratio([100, 101, 102, 103, 104]) == pytest.approx(1.0)

    def test_round_trip_is_zero_efficiency(self):
        """Moved a lot, ended where it started — the ideal grid asset."""
        assert metrics.efficiency_ratio([100, 105, 100]) == pytest.approx(0.0)

    def test_oscillation_scores_far_below_trend(self):
        assert metrics.efficiency_ratio(oscillating()) < 0.15
        assert metrics.efficiency_ratio(trending()) > 0.60

    def test_flat_series_returns_none(self):
        assert metrics.efficiency_ratio([100.0] * 10) is None


class TestVarianceRatio:
    def test_true_mean_reversion_is_below_one_at_every_horizon(self):
        for k in (5, 10, 20):
            assert metrics.variance_ratio(oscillating(), k) < 1.0

    def test_reversion_strengthens_with_horizon(self):
        assert metrics.variance_ratio(oscillating(), 20) < metrics.variance_ratio(oscillating(), 5)

    def test_a_smooth_cycle_reads_as_trending_inside_its_own_period(self):
        """The horizon trap, pinned down: this is why VR is not a hard gate.

        A sine with a 25-bar period is locally monotonic, so at k=5 it looks
        strongly trending even though it goes nowhere over a full cycle."""
        s = smooth_sine(period=25)
        assert metrics.variance_ratio(s, 5) > 1.0     # looks like a trend...
        assert metrics.variance_ratio(s, 30) < 1.0    # ...but reverts beyond its period

    def test_short_series_returns_none(self):
        assert metrics.variance_ratio([100.0] * 10, 10) is None


class TestADX:
    def test_trend_registers_above_the_threshold(self):
        c = trending()
        h, l = hl_from_closes(c)
        assert metrics.adx(h, l, c) > 25

    def test_oscillation_registers_below_the_threshold(self):
        c = oscillating()
        h, l = hl_from_closes(c)
        assert metrics.adx(h, l, c) < 25

    def test_returns_none_on_short_input(self):
        assert metrics.adx([1, 2], [1, 2], [1, 2]) is None

    def test_uses_wilder_smoothing_not_a_trailing_average(self):
        """A hard trend followed by a quiet patch must still read as trending.

        With a simple mean of the last 14 DX values, an asset that trended hard
        and then went sideways for 14 bars scored ~10 ADX points too low and
        passed the anti-trend gate — the exact regime that destroys a grid.
        Wilder's recursive smoothing keeps the memory of the trend.
        """
        c = trending(n=200) + oscillating(n=20)
        h, l = hl_from_closes(c)
        assert metrics.adx(h, l, c) > 25


class TestATR:
    def test_measures_bar_range_as_fraction(self):
        c = [100.0] * 30
        h, l = hl_from_closes(c, spread=0.01)
        assert metrics.atr_pct(h, l, c) == pytest.approx(0.02, abs=1e-3)

    def test_bigger_swings_give_bigger_atr(self):
        c = oscillating()
        wide_h, wide_l = hl_from_closes(c, 0.02)
        narrow_h, narrow_l = hl_from_closes(c, 0.002)
        assert metrics.atr_pct(wide_h, wide_l, c) > metrics.atr_pct(narrow_h, narrow_l, c)


class TestRangeQuality:
    def test_containment_counts_bars_inside_the_band(self):
        assert metrics.range_containment([95, 100, 105, 110], 96, 106) == pytest.approx(0.5)

    def test_empty_series_is_zero_not_a_crash(self):
        assert metrics.range_containment([], 1, 2) == 0.0

    def test_oscillation_touches_both_boundaries_repeatedly(self):
        prices = oscillating(n=300, amp=6, period=25)
        lo, hi = metrics.suggest_range(prices, coverage=0.90)
        assert metrics.range_touches(prices, lo, hi) >= 4

    def test_suggested_range_contains_most_of_the_series(self):
        prices = oscillating()
        lo, hi = metrics.suggest_range(prices, coverage=0.90)
        assert metrics.range_containment(prices, lo, hi) >= 0.85

    def test_suggest_range_needs_enough_data(self):
        assert metrics.suggest_range([100, 101]) is None


class TestCycleEstimate:
    def test_more_movement_means_more_cycles(self):
        assert (metrics.expected_cycles_per_day(0.04, 0.008) >
                metrics.expected_cycles_per_day(0.01, 0.008))

    def test_wider_spacing_means_fewer_cycles(self):
        assert (metrics.expected_cycles_per_day(0.03, 0.02) <
                metrics.expected_cycles_per_day(0.03, 0.005))

    def test_zero_spacing_is_zero_not_infinity(self):
        assert metrics.expected_cycles_per_day(0.03, 0.0) == 0.0


class TestRubricGates:
    """The gates are the safety layer — these are the tests that matter most."""

    def _assess(self, closes, spacing=0.01, vol=50_000_000.0, bars_per_day=24.0):
        h, l = hl_from_closes(closes)
        return assess("TEST-USDT", closes, h, l, spacing=spacing,
                      bars_per_day=bars_per_day, quote_volume_24h=vol)

    def test_bars_per_day_scales_the_cycle_estimate(self):
        """Hourly candles must not be scored as if each bar were a day.

        ATR is measured per bar. Treating hourly bars as daily understated the
        cycle rate 24-fold and rejected every good intraday candidate."""
        series = oscillating(n=300)
        hourly = self._assess(series, bars_per_day=24.0)
        daily = self._assess(series, bars_per_day=1.0)
        assert (hourly.measured["cycles_per_day"] ==
                pytest.approx(daily.measured["cycles_per_day"] * 24))

    def test_containment_gate_applies_only_to_a_caller_supplied_band(self):
        """suggest_range trims to a percentile, so an auto band contains ~90% by
        construction — gating on it was tautological. A supplied band is checked."""
        series = oscillating(n=300)
        h, l = hl_from_closes(series)
        bad = assess("T-USDT", series, h, l, spacing=0.01, bars_per_day=24.0,
                     quote_volume_24h=5e7, lower=min(series), upper=min(series) * 1.001)
        assert not bad.passed
        assert any("band holds only" in r for r in bad.gate_failures)

    def test_half_a_band_is_an_error_not_a_silent_fallback(self):
        series = oscillating(n=300)
        h, l = hl_from_closes(series)
        a = assess("T-USDT", series, h, l, spacing=0.01, bars_per_day=24.0,
                   quote_volume_24h=5e7, lower=95.0)
        assert not a.passed
        assert any("both lower and upper" in r for r in a.gate_failures)

    def test_zeroed_thresholds_do_not_crash_the_scorer(self):
        """Setting a threshold to zero is the natural way to disable a gate for a
        backtest, and it used to raise ZeroDivisionError."""
        series = oscillating(n=300)
        h, l = hl_from_closes(series)
        t = Thresholds(min_quote_volume_24h=0.0)
        a = assess("T-USDT", series, h, l, spacing=0.01, bars_per_day=24.0,
                   quote_volume_24h=0.0, thresholds=t)
        assert 0.0 <= a.score <= 1.0

    def test_a_trending_asset_is_rejected(self):
        a = self._assess(trending())
        assert not a.passed
        assert any("Hurst" in r or "ADX" in r or "efficiency" in r
                   for r in a.gate_failures)

    def test_an_oscillating_asset_passes(self):
        a = self._assess(oscillating(n=300, amp=8, period=25))
        assert a.passed, a.gate_failures
        assert 0.0 <= a.score <= 1.0

    def test_illiquid_asset_is_rejected_however_good_the_statistics(self):
        a = self._assess(oscillating(n=300, amp=8, period=25), vol=1000.0)
        assert not a.passed
        assert any("volume" in r for r in a.gate_failures)

    def test_spacing_that_feeds_the_exchange_is_rejected(self):
        a = self._assess(oscillating(n=300, amp=8, period=25), spacing=0.001)
        assert not a.passed
        assert any("fees" in r for r in a.gate_failures)

    def test_too_short_a_history_is_rejected_not_guessed(self):
        a = self._assess(oscillating(n=50))
        assert not a.passed
        assert any("bars" in r for r in a.gate_failures)

    def test_missing_statistics_fail_closed(self):
        """A flat series makes several metrics undefined. That must reject."""
        a = self._assess([100.0] * 200)
        assert not a.passed

    def test_rejected_assets_never_appear_in_the_ranking(self):
        good = self._assess(oscillating(n=300, amp=8, period=25))
        bad = self._assess(trending())
        ranked = rank([good, bad])
        assert all(a.passed for a in ranked)
        assert bad not in ranked

    def test_assessment_explains_itself(self):
        a = self._assess(trending())
        assert "REJECTED" in a.explain()


# ===========================================================================
# Statistical framework
# ===========================================================================

def regime_split(n_rev=140, n_trend=180, drift=0.001, noise=0.01, seed=21):
    """Mean-reverting for its first stretch, trending for its second.

    Tuned so the FULL-WINDOW Hurst still passes the 0.45 gate (0.423) while
    only 56% of sub-windows do. That combination is the whole argument for the
    stability check, and it is narrow: push the drift up and the full window
    fails too, which proves nothing.

    The trending half rises about 22% — a real trend, not a driftless walk.
    """
    head = oscillating(n=n_rev, seed=seed)
    r = random.Random(seed + 1)
    p, tail = head[-1], []
    for _ in range(n_trend):
        p *= (1 + drift + r.gauss(0, noise))
        tail.append(p)
    return head + tail


class TestAdfTest:
    """The primary evidence: is the random-walk null rejected?"""

    def test_an_ou_process_rejects_the_random_walk(self):
        r = metrics.adf_test(oscillating())
        assert r is not None and r.pvalue < 0.05
        assert r.rejects_random_walk()

    def test_a_trend_does_not(self):
        r = metrics.adf_test(trending())
        assert r is not None and r.pvalue >= 0.05
        assert not r.rejects_random_walk()

    def test_a_random_walk_does_not(self):
        r = metrics.adf_test(random_walk(n=400))
        assert r is not None and not r.rejects_random_walk()

    def test_the_statistic_is_more_negative_for_reversion(self):
        assert metrics.adf_test(oscillating()).statistic < \
               metrics.adf_test(trending()).statistic

    def test_it_reports_critical_values(self):
        assert set(metrics.adf_test(oscillating()).critical_values) >= {"5%"}

    def test_too_short_a_series_returns_none(self):
        assert metrics.adf_test([100.0] * 10) is None

    def test_a_constant_series_returns_none(self):
        """No unit root to test, and no p-value to quote."""
        assert metrics.adf_test([100.0] * 100) is None

    def test_a_degenerate_series_returns_none_not_a_pass(self):
        """A rank-deficient regression has no usable p-value. Absent evidence
        must never read as evidence."""
        assert metrics.adf_test([100.0 + 0.001 * (i % 2) for i in range(300)]) is None

    def test_it_uses_a_constant_not_a_trend(self):
        """A series reverting to a RISING line is still trending to a grid.

        With regression="ct" this would be called stationary. With "c" it is
        not, which is the answer a grid needs.
        """
        r = random.Random(3)
        dev, series = 0.0, []
        for i in range(300):
            dev += 0.2 * (0 - dev) + r.gauss(0, 1.5)
            series.append(100.0 * (1.003 ** i) + dev)
        assert not metrics.adf_test(series).rejects_random_walk()


class TestHurstCI:

    def test_the_ou_interval_sits_entirely_below_the_gate(self):
        ci = metrics.hurst_ci(oscillating(), n_boot=300)
        assert ci.high < 0.45
        assert ci.certainly_below(0.45)

    def test_the_trend_interval_does_not(self):
        assert not metrics.hurst_ci(trending(), n_boot=300).certainly_below(0.45)

    def test_a_random_walk_interval_does_not(self):
        assert not metrics.hurst_ci(random_walk(n=400),
                                    n_boot=300).certainly_below(0.45)

    def test_the_point_estimate_lies_inside_its_own_interval(self):
        """It did not, with the textbook n**(1/3) block length — blocks of 7
        destroyed the dependence the estimator measures out to lag 40."""
        for series in (oscillating(), trending(), random_walk(n=400)):
            ci = metrics.hurst_ci(series, n_boot=300)
            assert ci.low <= ci.point <= ci.high

    def test_the_block_is_long_enough_for_the_lag_range(self):
        ci = metrics.hurst_ci(oscillating(), n_boot=100)
        assert ci.block >= 40 or ci.block >= len(oscillating()) // 6

    def test_the_bootstrap_is_not_iid(self):
        """An iid resample would centre every interval on 0.5 regardless of
        input. A block bootstrap keeps the OU interval far below it."""
        ci = metrics.hurst_ci(oscillating(), n_boot=300)
        assert ci.high < 0.5

    def test_it_is_deterministic_for_a_given_seed(self):
        a = metrics.hurst_ci(oscillating(), n_boot=200, seed=7)
        b = metrics.hurst_ci(oscillating(), n_boot=200, seed=7)
        assert (a.low, a.high) == (b.low, b.high)

    def test_more_boots_does_not_change_the_point_estimate(self):
        a = metrics.hurst_ci(oscillating(), n_boot=100)
        b = metrics.hurst_ci(oscillating(), n_boot=400)
        assert a.point == pytest.approx(b.point)

    def test_a_short_series_returns_none(self):
        assert metrics.hurst_ci([100.0] * 30) is None

    def test_width_is_high_minus_low(self):
        ci = metrics.hurst_ci(oscillating(), n_boot=100)
        assert ci.width == pytest.approx(ci.high - ci.low)


class TestRollingStability:

    def hurst_pass(self, v):
        return v < 0.45

    def test_a_stable_ou_passes_most_windows(self):
        st = metrics.rolling_stability(oscillating(n=400), 150, metrics.hurst,
                                       passes=self.hurst_pass)
        assert st.pass_fraction >= 0.70 and st.is_stable()

    def test_a_trend_fails_most_windows(self):
        st = metrics.rolling_stability(trending(n=400), 150, metrics.hurst,
                                       passes=self.hurst_pass)
        assert not st.is_stable()

    def test_the_regime_split_is_rejected(self):
        """The full-window average passes, the sub-windows do not."""
        s = regime_split()
        full = metrics.hurst(s)
        st = metrics.rolling_stability(s, 100, metrics.hurst,
                                       passes=self.hurst_pass)
        assert full < 0.45, f"full-window Hurst {full:.3f} should have passed"
        assert not st.is_stable(), (
            f"stability {st.pass_fraction:.0%} should have failed")

    def test_the_second_half_really_does_trend(self):
        """Otherwise the test above is only rejecting a random walk."""
        s = regime_split()
        tail = s[140:]
        assert tail[-1] / tail[0] > 1.10
        assert metrics.efficiency_ratio(tail) > metrics.efficiency_ratio(s[:140])

    def test_it_reports_the_spread(self):
        st = metrics.rolling_stability(regime_split(), 100, metrics.hurst,
                                       passes=self.hurst_pass)
        assert st.stdev > 0
        assert st.n_windows >= 3

    def test_too_short_a_series_returns_none(self):
        assert metrics.rolling_stability([100.0] * 50, 150, metrics.hurst,
                                         passes=self.hurst_pass) is None

    def test_values_are_kept_for_inspection(self):
        st = metrics.rolling_stability(oscillating(n=400), 150, metrics.hurst,
                                       passes=self.hurst_pass)
        assert len(st.values) == st.n_windows


class TestBenjaminiHochberg:

    def test_nothing_survives_when_nothing_is_significant(self):
        assert metrics.benjamini_hochberg([0.4, 0.5, 0.9], 0.05) == [False] * 3

    def test_a_very_small_p_survives(self):
        assert metrics.benjamini_hochberg([0.0001, 0.5, 0.9], 0.05)[0] is True

    def test_it_is_a_step_up_procedure(self):
        """0.04 alone clears 0.05; as one of 30 tests it does not."""
        assert metrics.benjamini_hochberg([0.04], 0.05) == [True]
        many = [0.04] + [0.5] * 29
        assert metrics.benjamini_hochberg(many, 0.05)[0] is False

    def test_it_is_less_severe_than_bonferroni(self):
        ps = [0.001, 0.008, 0.02, 0.5, 0.6]
        bh = metrics.benjamini_hochberg(ps, 0.05)
        bonferroni = [p <= 0.05 / len(ps) for p in ps]
        assert sum(bh) >= sum(bonferroni)

    def test_the_mask_is_in_the_callers_order(self):
        assert metrics.benjamini_hochberg([0.9, 0.0001], 0.05) == [False, True]

    def test_an_empty_list_is_fine(self):
        assert metrics.benjamini_hochberg([], 0.05) == []


class TestWilderAtr:

    def test_it_matches_a_hand_computed_wilder_average(self):
        closes = [100.0 + i for i in range(30)]
        highs = [c + 2 for c in closes]
        lows = [c - 2 for c in closes]
        trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                   abs(lows[i] - closes[i - 1])) for i in range(1, len(closes))]
        atr = sum(trs[:14]) / 14
        for tr in trs[14:]:
            atr = (atr * 13 + tr) / 14
        assert metrics.atr_pct(highs, lows, closes) == pytest.approx(
            atr / closes[-1])

    def test_it_differs_from_a_simple_mean_after_a_shock(self):
        closes = [100.0] * 40
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        highs[20] = 130.0                       # one violent bar, long ago
        simple = sum(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                         abs(lows[i] - closes[i - 1]))
                     for i in range(len(closes) - 14, len(closes))) / 14
        wilder = metrics.atr_pct(highs, lows, closes) * closes[-1]
        assert wilder > simple      # Wilder still remembers the shock

    def test_it_still_returns_a_fraction_of_price(self):
        closes = [100.0 + i * 0.1 for i in range(60)]
        highs = [c * 1.01 for c in closes]
        lows = [c * 0.99 for c in closes]
        assert 0.0 < metrics.atr_pct(highs, lows, closes) < 0.1

    def test_too_short_returns_none(self):
        assert metrics.atr_pct([1.0] * 5, [1.0] * 5, [1.0] * 5) is None


class TestRecencyWeightedRange:

    def test_an_unweighted_band_ignores_where_price_is_now(self):
        u, w = metrics.suggest_range_both([100.0 * (1.003 ** i) for i in range(300)])
        assert (w[0] + w[1]) / 2 > (u[0] + u[1]) / 2

    def test_a_stationary_series_gives_the_same_band_twice(self):
        u, w = metrics.suggest_range_both(oscillating())
        mid_u, mid_w = (u[0] + u[1]) / 2, (w[0] + w[1]) / 2
        assert mid_w == pytest.approx(mid_u, rel=0.03)

    def test_disagreement_means_the_range_has_moved(self):
        rising = [100.0 * (1.004 ** i) for i in range(300)]
        u, w = metrics.suggest_range_both(rising)
        assert abs((w[0] + w[1]) / (u[0] + u[1]) - 1) > 0.05

    def test_no_half_life_is_the_old_behaviour(self):
        s = oscillating()
        assert metrics.suggest_range(s) == metrics.suggest_range(s, half_life=None)

    def test_a_shorter_half_life_tracks_price_more_closely(self):
        rising = [100.0 * (1.004 ** i) for i in range(300)]
        fast = metrics.suggest_range(rising, half_life=20)
        slow = metrics.suggest_range(rising, half_life=200)
        assert (fast[0] + fast[1]) / 2 > (slow[0] + slow[1]) / 2

    def test_a_non_positive_half_life_is_refused(self):
        with pytest.raises(ValueError):
            metrics.suggest_range(oscillating(), half_life=0)


class TestTheRubricUsesTheStatistics:

    def _assess(self, series, **kw):
        h, l = hl_from_closes(series)
        return assess("X-USDT", series, h, l,
                      spacing=FeeModel().min_viable_spacing(0.25),
                      bars_per_day=24.0, quote_volume_24h=50e6, **kw)

    def test_an_ou_series_passes_every_statistical_gate(self):
        a = self._assess(oscillating(n=300, sigma=2.5, seed=18))
        assert a.passed, a.gate_failures
        assert a.measured["adf_p"] < 0.05
        assert a.measured["hurst_ci_high"] < 0.45
        assert a.measured["stability_pass"] >= 0.70

    def test_a_trend_fails_on_adf_first(self):
        a = self._assess(trending())
        assert not a.passed
        assert any(g.startswith("ADF") for g in a.gate_failures)

    def test_hurst_is_a_diagnostic_not_a_gate(self):
        """Demoted: it rejected 24 of 28 live candidates and is the
        least-supported statistic here. It is still measured and its verdict
        is still recorded — just not as a hard gate. See docs/THEORY.md."""
        a = self._assess(trending())
        assert not any("interval" in g for g in a.gate_failures)
        assert any("interval" in g for g in a.hurst_would_reject)

    def test_what_hurst_would_have_rejected_is_logged(self):
        a = self._assess(trending())
        assert a.hurst_would_reject
        assert any("Hurst diagnostic (NOT a gate)" in n for n in a.notes)

    def test_adf_is_now_the_primary_gate(self):
        a = self._assess(trending())
        assert not a.passed
        assert a.gate_failures[0].startswith("ADF")

    def test_a_good_series_still_passes_with_hurst_silent(self):
        a = self._assess(oscillating(n=300, sigma=2.5, seed=18))
        assert a.passed and not a.hurst_would_reject

    def test_a_random_walk_is_rejected(self):
        """The case four descriptive numbers can wave through."""
        a = self._assess(random_walk(n=400))
        assert not a.passed

    def test_the_regime_split_is_rejected_by_stability(self):
        a = self._assess(regime_split())
        assert not a.passed
        assert any("sub-windows" in g for g in a.gate_failures)

    def test_the_assessment_records_the_candidate_count(self):
        a = self._assess(oscillating(), n_candidates=30)
        assert a.n_candidates == 30
        assert a.measured["n_candidates"] == 30.0

    def test_the_adf_pvalue_is_kept_for_the_batch_correction(self):
        a = self._assess(oscillating())
        assert a.adf_pvalue is not None and a.adf_pvalue == a.measured["adf_p"]

    def test_both_bands_are_reported(self):
        a = self._assess(oscillating())
        assert a.suggested_range is not None and a.weighted_range is not None

    def test_range_drift_is_recorded(self):
        a = self._assess(oscillating())
        assert "range_drift" in a.measured

    def test_an_imprecise_but_passing_reading_is_noted(self):
        a = self._assess(oscillating(n=300, sigma=2.5, seed=18))
        if a.measured["hurst_ci_width"] > 0.20:
            assert any("imprecise" in n for n in a.notes)
