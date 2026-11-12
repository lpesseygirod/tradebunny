"""Volatility forecasting, markout instrumentation, and the Hurst demotion."""

import math

import pytest

from grid import markout, volatility
from grid.fees import FeeModel
from grid.markout import HORIZONS, Markout, curves, measure, signed_markout
from grid.volatility import GARCH, HAR, scaled_spacing


def series(n=3000, vol=0.01, seed=3, drift=0.0):
    import random
    r = random.Random(seed)
    p, out = 100.0, []
    for _ in range(n):
        p *= (1 + drift + r.gauss(0, vol))
        out.append(p)
    return out


class TestVolatilityForecast:

    def test_har_fits_and_forecasts(self):
        fc = volatility.har_forecast(series())
        assert fc is not None and fc.ok and fc.sigma > 0

    def test_it_tracks_the_true_volatility(self):
        calm = volatility.har_forecast(series(vol=0.002, seed=1))
        wild = volatility.har_forecast(series(vol=0.02, seed=1))
        assert wild.sigma > calm.sigma * 3

    def test_too_little_data_returns_none(self):
        assert volatility.har_forecast(series(n=100)) is None

    def test_the_ratio_is_relative_to_the_baseline(self):
        fc = volatility.har_forecast(series())
        assert fc.ratio == pytest.approx(fc.sigma / fc.baseline)

    def test_garch_is_available_and_agrees_roughly(self):
        s = series(n=1500)
        h, g = volatility.har_forecast(s), volatility.garch_forecast(s)
        assert g is not None and g.ok
        assert 0.3 < g.sigma / h.sigma < 3.0

    def test_garch_falls_back_to_har_through_the_facade(self):
        fc = volatility.forecast(series(n=400), model=GARCH)
        assert fc is not None and fc.model in (HAR, GARCH)

    def test_realised_vol_is_a_standard_deviation(self):
        s = series(vol=0.01, n=2000)
        assert volatility.realised_vol(s) == pytest.approx(0.01, rel=0.2)


class TestSpacingIsFloored:
    """The fee floor is structural and always wins."""

    def test_a_calm_forecast_cannot_go_below_the_floor(self):
        floor = FeeModel().min_viable_spacing(0.25)
        fc = volatility.har_forecast(series(vol=0.0005))
        sp, why = scaled_spacing(floor, fc, floor=floor)
        assert sp >= floor
        if sp == floor:
            assert "floor wins" in why

    def test_a_rising_volatility_forecast_widens_it(self):
        """A series whose volatility genuinely RISES at the end. A constant-
        volatility series is degenerate for HAR — its three regressors are
        collinear and the fit is not identified — so it cannot test this."""
        import random
        r = random.Random(5)
        p, out = 100.0, []
        for i in range(3000):
            vol = 0.004 if i < 2400 else 0.02      # regime change
            p *= (1 + r.gauss(0, vol))
            out.append(p)
        fc = volatility.har_forecast(out)
        assert fc.ratio > 1.0
        floor = 0.0005
        sp, _ = scaled_spacing(floor, fc, floor=floor)
        assert sp > floor

    def test_scaling_is_clamped(self):
        """A forecast wanting to quadruple the interval is a broken fit."""
        floor = 0.001
        class Fake:
            sigma, baseline, ok, model, n_obs = 1.0, 0.001, True, HAR, 999
            ratio = 1000.0
        sp, _ = scaled_spacing(floor, Fake(), floor=floor)
        assert sp <= floor * volatility.MAX_SCALE

    def test_no_forecast_leaves_spacing_alone(self):
        sp, why = scaled_spacing(0.01, None, floor=0.005)
        assert sp == 0.01 and "no usable" in why

    def test_a_failed_forecast_leaves_spacing_alone(self):
        bad = volatility.VolForecast(sigma=0.0, baseline=0.0, model=HAR,
                                     n_obs=0, ok=False)
        sp, _ = scaled_spacing(0.01, bad, floor=0.005)
        assert sp == 0.01


class TestItIsNeverADirectionalSignal:

    def test_nothing_returns_a_direction(self):
        import inspect
        src = inspect.getsource(volatility)
        for word in ("long", "short", "buy", "sell", "signal", "predict_return"):
            assert f"def {word}" not in src

    def test_the_rubric_does_not_consult_it(self):
        """Volatility decides how wide to space a ladder, never whether an
        asset is worth trading."""
        import ast, inspect
        from grid import rubric
        tree = ast.parse(inspect.getsource(rubric))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert "volatility" not in names

    def test_the_forecast_carries_no_sign(self):
        fc = volatility.har_forecast(series())
        assert fc.sigma > 0        # a magnitude, never a direction


class TestMarkout:

    def hourly(self, n=48, start=100.0, step=1.0):
        return [(i * 3600.0, start + i * step) for i in range(n)]

    def test_a_buy_that_rises_is_favourable(self):
        assert signed_markout("buy", 100.0, 110.0) == pytest.approx(0.10)

    def test_a_sell_that_rises_is_unfavourable(self):
        assert signed_markout("sell", 100.0, 110.0) == pytest.approx(-0.10)

    def test_a_sell_that_falls_is_favourable(self):
        assert signed_markout("sell", 100.0, 90.0) == pytest.approx(0.10)

    def test_it_measures_the_horizons_it_can_see(self):
        m = measure("X", "buy", 100.0, 0.0, 60.0, self.hourly())
        assert "+1h" in m.points and "+24h" in m.points

    def test_sub_bar_horizons_are_omitted_not_interpolated(self):
        """There is no price between one hourly candle and the next."""
        m = measure("X", "buy", 100.0, 0.0, 60.0, self.hourly())
        for h in ("+1s", "+10s", "+1m", "+5m", "+30m"):
            assert h not in m.points

    def test_short_horizons_do_not_absorb_a_long_observation(self):
        """The bug that made five horizons report an identical number."""
        m = measure("X", "buy", 100.0, 0.0, 60.0, self.hourly())
        assert m.points.get("+30m") != m.points.get("+1h") or \
            "+30m" not in m.points

    def test_buckets_by_time_to_fill(self):
        assert Markout("X", "buy", 1, 0, 10.0).bucket.startswith("fast")
        assert Markout("X", "buy", 1, 0, 7200.0).bucket.startswith("slow")
        assert Markout("X", "buy", 1, 0, 90000.0).bucket.startswith("very")

    def test_curves_are_segmented(self):
        ms = [measure("X", "buy", 100.0, 0.0, ttf, self.hourly())
              for ttf in (10.0, 10.0, 7200.0)]
        cs = curves(ms)
        assert {c.bucket.split()[0] for c in cs} == {"fast", "slow"}
        assert sum(c.n for c in cs) == 3

    def test_the_report_names_what_it_cannot_see(self):
        ms = [measure("X", "buy", 100.0, 0.0, 10.0, self.hourly())]
        assert "not observable at this bar size" in markout.report(ms)

    def test_no_fills_says_so(self):
        assert "no fills to measure" in markout.report([])

    def test_it_states_a_verdict_on_the_toxicity_claim(self):
        ms = ([measure("X", "buy", 100.0, 0.0, 10.0, self.hourly())] * 3
              + [measure("X", "buy", 100.0, 0.0, 7200.0, self.hourly())] * 3)
        r = markout.report(ms)
        assert "fast fills" in r and "claim is" in r

    def test_the_backtest_feeds_it(self):
        from grid.backtest import run
        from grid.plan import GridPlan
        p = GridPlan(symbol="T-USDT", lower=94.0, upper=106.0, levels=12,
                     quote_per_level=100.0, stop_below=90.0,
                     max_quote_total=1300.0, spacing_mode="arithmetic")
        closes = [106.0 - 0.4 * i for i in range(40)] + \
                 [90.4 + 0.4 * i for i in range(40)]
        r = run(p, closes, bars_per_day=24.0, slip=0.0, fill_margin=0.0,
                max_fills_per_bar=None)
        assert r.fills
        ms = markout.from_backtest(r.fills, closes, bar_seconds=3600.0)
        assert ms and any(m.points for m in ms)

    def test_time_to_fill_is_recorded(self):
        from grid.backtest import run
        from grid.plan import GridPlan
        p = GridPlan(symbol="T-USDT", lower=94.0, upper=106.0, levels=12,
                     quote_per_level=100.0, stop_below=90.0,
                     max_quote_total=1300.0, spacing_mode="arithmetic")
        closes = [106.0 - 0.4 * i for i in range(40)] + \
                 [90.4 + 0.4 * i for i in range(40)]
        r = run(p, closes, bars_per_day=24.0, slip=0.0, fill_margin=0.0,
                max_fills_per_bar=None)
        assert all("time_to_fill" in f for f in r.fills)


class TestTheoryDoc:

    def test_it_exists_and_cites_the_sources(self):
        import pathlib
        doc = (pathlib.Path(__file__).resolve().parents[1]
               / "docs" / "THEORY.md").read_text()
        for cite in ("Avellaneda", "Guéant", "Corsi", "Bollerslev", "Lo, A. W.",
                     "Dickey"):
            assert cite in doc

    def test_it_records_the_hurst_demotion_as_a_relaxation(self):
        import pathlib
        doc = (pathlib.Path(__file__).resolve().parents[1]
               / "docs" / "THEORY.md").read_text()
        assert "relaxation of a safety gate" in doc

    def test_it_audits_every_lookback(self):
        import pathlib
        doc = (pathlib.Path(__file__).resolve().parents[1]
               / "docs" / "THEORY.md").read_text()
        assert "Lookback audit" in doc and "CANDLES = 300" in doc
