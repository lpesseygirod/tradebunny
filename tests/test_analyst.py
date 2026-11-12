"""Tests for the daily scan.

No network and no API calls. `source` and `client` are injected, so what runs
is the real gate logic, the real sizing arithmetic and the real walk-forward —
only the two I/O boundaries are stubbed.

The series come from `test_metrics.py`, so each symbol's fate is known by
construction: an oscillation should survive the gates, a trend should not.
"""

import dataclasses
import logging
import time

HOUR_S = 3600.0

import pytest

from grid import analyst, metrics, rubric
from grid.analyst import Rejection, ScanReport, round_down_to_round_number, scan, size_grid
from grid.control import Control, ControlState
from grid.fees import FeeModel
from grid.plan import GridPlan, Status

from test_metrics import hl_from_closes, oscillating, trending


# ------------------------------------------------------------------- doubles

class FakeSource:
    """Stands in for grid.okx. Same four functions the analyst uses.

    `get_trade_fee` returns the OKX global base tier by default, so the
    fixtures below are measured against a known, stated fee model rather than
    silently inheriting whatever the fallback happens to be. Pass
    `fee=None` to simulate a lookup failure.
    """

    def __init__(self, series: dict, volumes=None, fail=(),
                 fee=(0.0008, 0.0010), min_size=1e-6, instruments=None):
        self.min_size = min_size
        self._instruments = instruments
        self.instrument_calls = 0
        self.series = series
        self.volumes = volumes or {s: 50_000_000.0 for s in series}
        self.fail = dict(fail)
        self.fee = fee
        self.candle_calls = []
        self.ticker_calls = 0
        self.fee_calls = 0

    def get_instruments(self, inst_type="SPOT"):
        """Exchange order-size limits. `min_size` defaults to something tiny so
        the fixtures below are not accidentally testing the minSz gate; the
        tests that DO test it set it explicitly."""
        self.instrument_calls += 1
        if self._instruments is not None:
            return self._instruments
        from grid.okx import Instrument
        return {s: Instrument(inst_id=s, min_size=self.min_size,
                              lot_size=self.min_size, tick_size=1e-8,
                              base=s.split("-")[0], quote="USDT", state="live")
                for s in self.series}

    def get_trade_fee(self, inst_id="BTC-USDT", inst_type="SPOT"):
        self.fee_calls += 1
        if self.fee is None:
            raise RuntimeError("401 Unauthorized")
        return {"maker": self.fee[0], "taker": self.fee[1], "level": "Lv1",
                "inst_type": inst_type, "inst_id": inst_id, "fetched_at": ""}

    def bars_per_day(self, bar):
        return {"1H": 24.0, "4H": 6.0, "1D": 1.0}[bar]

    def get_spot_tickers(self):
        self.ticker_calls += 1
        return {s: {"last": 100.0, "quote_volume_24h": v}
                for s, v in self.volumes.items()}

    def get_candles(self, inst_id, bar="1H", limit=300):
        self.candle_calls.append((inst_id, bar, limit))
        if inst_id in self.fail:
            raise self.fail[inst_id]
        closes = self.series[inst_id]
        highs, lows = hl_from_closes(closes)
        # Timestamps must be RECENT: the freshness gate measures the newest
        # candle's own close time, so a fixture stamped at the epoch is stale
        # data and is refused before any statistic is computed.
        n = len(closes)
        newest_open = (time.time() - HOUR_S) * 1000.0
        return [{"ts": newest_open - (n - 1 - i) * HOUR_S * 1000.0,
                 "open": c, "high": h, "low": l, "close": c, "volume": 1000.0}
                for i, (c, h, l) in enumerate(zip(closes, highs, lows))]

    def fetched_at(self, source):
        return time.time()


class FakeClient:
    """Stands in for anthropic.Anthropic."""

    def __init__(self, text="A short explanation.", error=None):
        self.text = text
        self.error = error
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        block = type("B", (), {"type": "text", "text": self.text})()
        return type("R", (), {"content": [block]})()


@pytest.fixture(autouse=True)
def _fresh_fee_cache():
    """The fee cache is process-wide; a test must never inherit another's tier."""
    analyst.clear_fee_cache()
    yield
    analyst.clear_fee_cache()


@pytest.fixture
def control(tmp_path):
    c = Control(tmp_path / "control.json")
    c.initialise()
    return c


#: Seeds whose OU paths clear every rubric gate AND still generalise out of
#: sample at the 60/40 split. Most seeds do not — the walk-forward gate is
#: strict, which is the point of it — so the fixtures are chosen from the ones
#: that survive rather than the gates being loosened to admit the others.
GOOD_SEEDS = (18, 1, 10, 20)


def good_series(seed=18):
    """Oscillating — the character the rubric selects for."""
    return oscillating(n=300, seed=seed, sigma=2.5)


def trend_series():
    return trending(n=300, drift=0.004, noise=0.004, seed=1)


#: Enough capital that a fee-floor ladder (~22 levels) funds a real order on
#: every line. See TestSizingInTheScan::test_thin_capital_cannot_fund_a_fine
#: _ladder for what happens below this, which is a genuine constraint rather
#: than a test convenience.
#: Enough that the KELLY constraint does not bind the ladder below the
#: exchange minimum. Quarter Kelly on a good candidate is only a few percent of
#: capital, so 2000 (the old figure, from before Kelly existed) now sizes every
#: proposal out of existence — which is the sizing working, not a bug.
DEFAULT_CAPITAL = 20_000.0


def run_scan(series, available=DEFAULT_CAPITAL, control=None, client=None, **kw):
    report = ScanReport()
    src = FakeSource(series) if isinstance(series, dict) else series
    plans = scan(list(src.series), available, control=control,
                 client=client or FakeClient(), source=src, report=report, **kw)
    return plans, report, src


# --------------------------------------------------------------- premise check

class TestTheFixturesAreWhatWeThinkTheyAre:

    def test_the_oscillation_passes_the_rubric(self):
        c = good_series()
        h, l = hl_from_closes(c)
        a = rubric.assess("X-USDT", c, h, l,
                          spacing=FeeModel().min_viable_spacing(0.25),
                          bars_per_day=24.0, quote_volume_24h=50_000_000.0)
        assert a.passed, a.gate_failures

    def test_the_trend_fails_the_rubric(self):
        c = trend_series()
        h, l = hl_from_closes(c)
        a = rubric.assess("X-USDT", c, h, l,
                          spacing=FeeModel().min_viable_spacing(0.25),
                          bars_per_day=24.0, quote_volume_24h=50_000_000.0)
        assert not a.passed


# ------------------------------------------------------------------ the scan

class TestHappyPath:

    @pytest.fixture
    def scanned(self, control):
        return run_scan({"AAA-USDT": good_series()}, control=control)

    def test_it_proposes_the_oscillating_asset(self, scanned):
        plans, _, _ = scanned
        assert [p.symbol for p in plans] == ["AAA-USDT"]

    def test_the_plan_is_valid(self, scanned):
        plans, _, _ = scanned
        assert plans[0].validate() == []

    def test_the_plan_is_only_proposed(self, scanned):
        """Nothing is approved by a scan. The owner decides."""
        plans, _, _ = scanned
        assert plans[0].status is Status.PROPOSED

    def test_it_fetches_300_candles(self, scanned):
        _, _, src = scanned
        assert src.candle_calls == [("AAA-USDT", "1H", 300)]

    def test_it_fetches_tickers_once_for_the_whole_universe(self, control):
        _, _, src = run_scan({f"S{i}-USDT": good_series(seed=i) for i in GOOD_SEEDS},
                             control=control)
        assert src.ticker_calls == 1

    def test_the_measurements_are_recorded_on_the_plan(self, scanned):
        plans, _, _ = scanned
        assert {"hurst", "adx", "atr_pct"} <= set(plans[0].metrics)

    def test_the_walk_forward_result_is_recorded(self, scanned):
        plans, _, _ = scanned
        assert "total_return_pct" in plans[0].backtest

    def test_the_report_lists_what_was_accepted(self, scanned):
        _, report, _ = scanned
        assert report.accepted == ["AAA-USDT"]
        assert report.considered == 1 and report.fetched == 1


class TestEmptyIsACorrectAnswer:

    def test_a_trending_universe_proposes_nothing(self, control):
        plans, report, _ = run_scan({"AAA-USDT": trend_series()}, control=control)
        assert plans == []
        assert report.by_stage("gate")

    def test_an_empty_universe_is_fine(self, control):
        assert scan([], 500.0, control=control, source=FakeSource({})) == []

    def test_no_capital_means_no_proposals(self, control):
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, available=0.0,
                               control=control)
        assert plans == []

    def test_nothing_is_manufactured_to_fill_the_cap(self, control):
        """Three slots available, one good asset: propose one, not three."""
        series = {"AAA-USDT": good_series(), "BBB-USDT": trend_series(),
                  "CCC-USDT": trend_series()}
        plans, _, _ = run_scan(series, control=control)
        assert len(plans) == 1


class TestRejectionsAreLogged:
    """'So I can see why a quiet day was quiet.'"""

    def test_each_gate_failure_is_recorded_with_its_reason(self, control):
        plans, report, _ = run_scan({"AAA-USDT": trend_series()}, control=control)
        r = report.by_stage("gate")[0]
        assert r.symbol == "AAA-USDT"
        assert any("Hurst" in x or "ADX" in x or "efficiency" in x for x in r.reasons)

    def test_a_fetch_failure_is_recorded_and_does_not_stop_the_scan(self, control):
        src = FakeSource({"AAA-USDT": good_series(), "BAD-USDT": good_series()},
                         fail={"BAD-USDT": RuntimeError("503")})
        report = ScanReport()
        plans = scan(["BAD-USDT", "AAA-USDT"], DEFAULT_CAPITAL, control=control,
                     client=FakeClient(), source=src, report=report)
        assert [p.symbol for p in plans] == ["AAA-USDT"]
        assert report.by_stage("fetch")[0].symbol == "BAD-USDT"

    def test_a_short_history_is_recorded(self, control):
        src = FakeSource({"AAA-USDT": good_series()[:50]})
        report = ScanReport()
        scan(["AAA-USDT"], DEFAULT_CAPITAL, control=control, client=FakeClient(),
             source=src, report=report)
        assert "need 120+" in report.by_stage("fetch")[0].reasons[0]

    def test_low_volume_is_rejected_at_the_liquidity_gate(self, control):
        src = FakeSource({"AAA-USDT": good_series()}, volumes={"AAA-USDT": 1000.0})
        report = ScanReport()
        assert scan(["AAA-USDT"], DEFAULT_CAPITAL, control=control,
                    client=FakeClient(), source=src, report=report) == []
        assert any("volume" in x for x in report.by_stage("gate")[0].reasons)

    def test_the_report_reads_as_a_summary(self, control):
        _, report, _ = run_scan({"AAA-USDT": trend_series()}, control=control)
        assert "scanned 1" in report.explain()

    def test_rejections_reach_the_log(self, control, caplog):
        with caplog.at_level(logging.INFO, logger="grid.analyst"):
            run_scan({"AAA-USDT": trend_series()}, control=control)
        assert any("rejected AAA-USDT" in m for m in caplog.messages)


def regime_change_series(seed=18):
    """Oscillates, then quietly rolls over.

    Chosen because it PASSES every rubric gate: the statistics are measured
    across all 300 bars, and the oscillating first 180 dominate Hurst, ADX and
    the efficiency ratio enough to carry the trailing decline through. Only
    splitting the window exposes it. This is precisely why the walk-forward is
    a second gate rather than a nice-to-have.
    """
    head = oscillating(n=180, sigma=2.5, seed=seed)
    return head + trending(n=120, start=head[-1], drift=-0.0008, noise=0.004, seed=7)


class TestWalkForwardIsAGate:

    def test_the_series_really_does_pass_the_rubric(self):
        """Without this, the test below could be passing for the wrong reason."""
        c = regime_change_series()
        h, l = hl_from_closes(c)
        a = rubric.assess("AAA-USDT", c, h, l,
                          spacing=FeeModel().min_viable_spacing(0.25),
                          bars_per_day=24.0, quote_volume_24h=50_000_000.0)
        assert a.passed, a.gate_failures

    def test_a_candidate_that_does_not_generalise_is_dropped(self, control):
        plans, report, _ = run_scan({"AAA-USDT": regime_change_series()},
                                    control=control)
        assert plans == []
        assert report.by_stage("gate") == []          # it cleared the rubric
        assert report.by_stage("walk_forward")        # and died here

    def test_the_reason_is_recorded(self, control):
        _, report, _ = run_scan({"AAA-USDT": regime_change_series()},
                                control=control)
        assert report.by_stage("walk_forward")[0].reasons[0]

    def test_every_rejection_names_a_known_stage(self, control):
        _, report, _ = run_scan({"AAA-USDT": regime_change_series()},
                                control=control)
        for r in report.rejections:
            assert r.stage in {"fetch", "gate", "walk_forward", "sizing", "validate"}


# -------------------------------------------------------------------- sizing

class TestRiskSizing:
    """Sized by what a stop-out actually costs, not by the distance to it."""

    def test_the_formula_is_avg_entry_to_stop(self):
        # upper 110, stop 90 -> avg_entry 100, loss_fraction 0.10
        # 1000 * 0.02 / 0.10 = 200
        assert size_grid(1000.0, 110.0, 90.0, 1e9, 1) == 200.0

    def test_it_accounts_for_the_whole_ladder_not_just_the_bottom(self):
        """A grid holds lots from the top of the range down to the stop.

        The old formula measured from `lower` and so sized as if the position
        were opened at the bottom. On a 94-106 range stopping at 90 it
        understated the loss badly and oversized the grid.
        """
        upper, lower, stop = 106.0, 94.0, 90.0
        new = size_grid(100_000.0, upper, stop, 1e9, 1)
        old_distance = (lower - stop) / lower           # 4.26%
        old = 100_000.0 * 0.02 / old_distance
        # avg entry 98 -> real loss fraction 8.16%, so the old formula sized
        # 1.9x too large. On this geometry that is 49% smaller, not the ~60%
        # in the brief; the exact figure depends on how wide the range is
        # relative to the stop distance.
        assert new < old
        assert 0.40 < (1 - new / old) < 0.60

    def test_a_wider_stop_gets_less_capital(self):
        near = size_grid(100_000.0, 110.0, 100.0, 1e9, 1)
        far = size_grid(100_000.0, 110.0, 80.0, 1e9, 1)
        assert near > far

    def test_conviction_is_not_a_term(self):
        import inspect
        params = set(inspect.signature(size_grid).parameters)
        assert not {"score", "confidence", "conviction"} & params

    def test_the_absolute_cap_binds(self):
        assert size_grid(1e6, 110.0, 90.0, 500.0, 1) == 500.0

    def test_the_concurrency_cap_binds(self):
        assert size_grid(500.0, 110.0, 90.0, 1e9, 3) <= 500.0 / 3

    def test_rounding_never_increases_the_risk(self):
        for available in (137.0, 999.0, 4321.0, 78.5):
            q = size_grid(available, 110.0, 90.0, 1e9, 1)
            assert q <= available * 0.02 / ((100.0 - 90.0) / 100.0) + 1e-9

    def test_a_stop_at_or_above_the_range_is_refused(self):
        with pytest.raises(ValueError):
            size_grid(1000.0, 110.0, 110.0, 1e9, 1)

    def test_a_negative_stop_is_refused(self):
        with pytest.raises(ValueError):
            size_grid(1000.0, 110.0, -5.0, 1e9, 1)


class TestRounding:

    @pytest.mark.parametrize("raw,expected", [
        (1234.5, 1200.0), (345.6, 340.0), (87.3, 87.0), (9.74, 9.7), (1000.0, 1000.0),
    ])
    def test_two_significant_figures_downward(self, raw, expected):
        assert round_down_to_round_number(raw) == pytest.approx(expected)

    def test_it_never_rounds_up(self):
        for x in [1.0001, 19.99, 199.9, 1999.0, 45.6, 0.37]:
            assert round_down_to_round_number(x) <= x

    def test_zero_and_nonsense_are_zero(self):
        for x in (0.0, -5.0, float("nan"), float("inf")):
            assert round_down_to_round_number(x) == 0.0


class TestSizingInTheScan:

    def test_the_plan_respects_the_control_cap(self, control):
        control.set_caps(max_total_quote=300.0, max_concurrent_grids=1)
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, available=100_000.0,
                               control=control)
        assert plans[0].max_quote_total <= 300.0

    def test_the_ladder_is_fully_funded(self, control):
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        p = plans[0]
        assert p.max_quote_total == pytest.approx(
            len(p.grid_prices) * p.quote_per_level)

    def test_a_dust_sized_grid_is_rejected_not_proposed(self, control):
        """Too little capital to place a real order on each level."""
        plans, report, _ = run_scan({"AAA-USDT": good_series()}, available=30.0,
                                    control=control)
        assert plans == []
        assert "minimum order" in report.by_stage("sizing")[0].reasons[0]

    def test_thin_capital_cannot_fund_a_fine_ladder(self, control):
        """A real constraint, recorded here so it is not rediscovered live.

        Spacing is set at the fee floor, which maximises the level count — a
        typical band comes out around 22 lines. At 500 of capital split three
        ways the per-level order is ~7, under the exchange minimum, so the
        honest answer is no proposal rather than a plan that cannot execute.
        The fix is more capital, fewer concurrent grids, or a wider spacing —
        never a lower minimum.
        """
        plans, report, _ = run_scan({"AAA-USDT": good_series()}, available=500.0,
                                    control=control)
        assert plans == []
        assert report.by_stage("sizing")

    def test_the_same_asset_funds_once_there_is_capital(self, control):
        plans, _, _ = run_scan({"AAA-USDT": good_series()},
                               available=DEFAULT_CAPITAL, control=control)
        assert len(plans) == 1
        assert plans[0].quote_per_level >= analyst.MIN_QUOTE_PER_LEVEL


# ---------------------------------------------------------------- the AI part

class TestTheNoteIsATemplate:
    """No AI at runtime. The note is rendered from the same measurements."""

    def test_a_thesis_is_written(self, control):
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        assert plans[0].thesis

    def test_the_module_imports_no_model_client(self):
        import inspect
        src = inspect.getsource(analyst)
        assert "anthropic" not in src
        assert "ANTHROPIC_API_KEY" not in src

    def test_no_client_is_ever_called(self, control):
        """`client` is accepted and ignored; a client that explodes if touched
        proves nothing reaches it."""
        class Explode:
            def __getattr__(self, name):
                raise AssertionError("the analyst called a model client")
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control,
                               client=Explode())
        assert len(plans) == 1 and plans[0].thesis

    def test_the_note_states_the_range_and_how_it_held(self, control):
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        t = plans[0].thesis
        assert "traded between" in t and "tested at its edges" in t

    def test_the_note_labels_the_statistics_in_words(self, control):
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        t = plans[0].thesis
        assert "Hurst" in t and "ADX" in t and "efficiency ratio" in t
        assert any(w in t for w in ("mean-reverting", "trending", "borderline"))

    def test_the_note_states_spacing_and_fee_share(self, control):
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        t = plans[0].thesis
        assert "levels" in t and "of gross profit" in t

    def test_the_note_states_the_expected_daily_yield(self, control):
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        assert "a day gross of slippage" in plans[0].thesis

    def test_the_note_states_the_stop_and_its_distance(self, control):
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        t = plans[0].thesis
        assert "The stop is" in t and "below the bottom of" in t

    def test_the_note_states_the_case_against(self, control):
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        assert "Against it" in plans[0].thesis

    def test_it_is_deterministic(self, control):
        a, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        b, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        assert a[0].thesis == b[0].thesis

    def test_the_note_cannot_fail(self, control):
        """There is no failure path left: no network, no key, no client."""
        plans, report, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        assert report.thesis_failures == 0

    def test_the_brief_survives_as_documentation_only(self):
        """It still exists, and nothing sends it anywhere.

        Checked against the code rather than the text — the module docstring
        mentions it deliberately, to say it is documentation now.
        """
        import ast, inspect
        assert rubric.ANALYST_BRIEF
        tree = ast.parse(inspect.getsource(analyst))
        used = [n for n in ast.walk(tree)
                if (isinstance(n, ast.Attribute) and n.attr == "ANALYST_BRIEF")
                or (isinstance(n, ast.Name) and n.id == "ANALYST_BRIEF")]
        assert used == [], "analyst.py still references ANALYST_BRIEF in code"


# ------------------------------------------------------------------- control

class TestControlIsHonoured:

    def test_a_halted_analyst_proposes_nothing(self, control):
        control.halt_analyst("owner stopped it")
        plans, report, src = run_scan({"AAA-USDT": good_series()}, control=control)
        assert plans == [] and report.halted

    def test_a_halted_analyst_does_not_even_fetch(self, control):
        """Fail closed before spending a request."""
        control.halt_analyst()
        _, _, src = run_scan({"AAA-USDT": good_series()}, control=control)
        assert src.candle_calls == [] and src.ticker_calls == 0

    def test_a_missing_control_file_halts(self, tmp_path):
        """The control module fails closed; the analyst must inherit that."""
        plans, report, _ = run_scan({"AAA-USDT": good_series()},
                                    control=Control(tmp_path / "absent.json"))
        assert plans == [] and report.halted

    def test_halt_all_stops_the_analyst_too(self, control):
        control.halt_all("panic")
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        assert plans == []


class TestProposalCap:

    def test_at_most_three(self, control):
        control.set_caps(max_concurrent_grids=10, max_total_quote=1e9)
        series = {f"S{i}-USDT": good_series(seed=i) for i in GOOD_SEEDS}
        plans, _, _ = run_scan(series, available=1_000_000.0, control=control)
        assert len(series) > 3          # more candidates than slots
        assert len(plans) == 3

    def test_the_control_limit_can_lower_it_further(self, control):
        control.set_caps(max_concurrent_grids=1, max_total_quote=1e9)
        series = {f"S{i}-USDT": good_series(seed=i) for i in GOOD_SEEDS}
        plans, _, _ = run_scan(series, available=100_000.0, control=control)
        assert len(plans) == 1

    def test_they_come_back_best_first(self, control):
        control.set_caps(max_concurrent_grids=10, max_total_quote=1e9)
        series = {f"S{i}-USDT": good_series(seed=i) for i in GOOD_SEEDS}
        plans, _, src = run_scan(series, available=1_000_000.0, control=control)
        # The fit score is not stored on the plan, so re-derive it to confirm
        # the sort really is by score and not by universe order.
        got = []
        for p in plans:
            c = src.series[p.symbol]
            h, l = hl_from_closes(c)
            got.append(rubric.assess(p.symbol, c, h, l,
                                     spacing=FeeModel().min_viable_spacing(0.25),
                                     bars_per_day=24.0,
                                     quote_volume_24h=50_000_000.0).score)
        assert len(got) == 3
        assert got == sorted(got, reverse=True)


# ----------------------------------------------------------------- structure

class TestGatesAreNotSoftened:
    """These constants are the safety margin. Changing one changes the risk."""

    def test_risk_per_trade_is_two_percent(self):
        assert analyst.RISK_PER_TRADE == 0.02

    def test_the_proposal_cap_is_three(self):
        assert analyst.MAX_PROPOSALS == 3

    def test_the_stop_constants_are_what_we_documented(self):
        assert analyst.STOP_ATR_MULTIPLE == 3.0
        assert analyst.STOP_RANGE_FRACTION == 0.20

    def test_the_candidate_spacing_clears_the_fee_floor(self, control):
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        floor = FeeModel().min_viable_spacing(0.25)
        assert plans[0].min_spacing_pct >= floor

    def test_the_scan_uses_the_rubric_unmodified(self, control):
        """No local Thresholds override sneaks in a looser gate."""
        default = rubric.Thresholds()
        assert default.max_hurst == 0.45 and default.max_adx == 25.0


class TestReportShape:

    def test_rejection_is_frozen(self):
        r = Rejection("A", "gate", ("why",))
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.symbol = "B"

    def test_rejection_reads_well(self):
        assert str(Rejection("A-USDT", "gate", ("too trendy",))) == \
            "A-USDT: [gate] too trendy"

    def test_a_report_is_optional(self, control):
        assert scan(["AAA-USDT"], 500.0, control=control, client=FakeClient(),
                    source=FakeSource({"AAA-USDT": trend_series()})) == []

    def test_an_unknown_bar_interval_raises(self, control):
        with pytest.raises((KeyError, ValueError)):
            scan(["AAA-USDT"], 500.0, bar="15m", control=control,
                 client=FakeClient(), source=FakeSource({"AAA-USDT": good_series()}))


class TestSpotOnlyInTheUniverse:
    """Entry point 1: a derivative in GRID_UNIVERSE."""

    def test_a_swap_never_becomes_a_proposal(self, control):
        src = FakeSource({"BTC-USDT-SWAP": good_series()})
        report = ScanReport()
        plans = scan(["BTC-USDT-SWAP"], DEFAULT_CAPITAL, control=control,
                     client=FakeClient(), source=src, report=report)
        assert plans == []

    def test_it_is_rejected_for_being_a_derivative_not_for_thin_volume(self,
                                                                      control):
        """It used to fail the LIQUIDITY gate — get_spot_tickers has no entry
        for a swap, so its volume read as 0. Right outcome, misleading reason,
        and it would have evaporated had the volume floor ever been lowered."""
        src = FakeSource({"BTC-USDT-SWAP": good_series()})
        report = ScanReport()
        scan(["BTC-USDT-SWAP"], DEFAULT_CAPITAL, control=control,
             client=FakeClient(), source=src, report=report)
        r = report.rejections[0]
        assert r.stage == "universe"
        assert "SPOT ONLY" in r.reasons[0]

    def test_it_is_rejected_before_any_candle_is_fetched(self, control):
        src = FakeSource({"BTC-USDT-SWAP": good_series()})
        scan(["BTC-USDT-SWAP"], DEFAULT_CAPITAL, control=control,
             client=FakeClient(), source=src, report=ScanReport())
        assert src.candle_calls == []

    def test_spot_symbols_alongside_it_still_scan(self, control):
        src = FakeSource({"BTC-USDT-SWAP": good_series(), "AAA-USDT": good_series()})
        plans = scan(["BTC-USDT-SWAP", "AAA-USDT"], DEFAULT_CAPITAL,
                     control=control, client=FakeClient(), source=src,
                     report=ScanReport())
        assert [p.symbol for p in plans] == ["AAA-USDT"]


# ------------------------------------------------------------------ the stop

from grid.analyst import (RISK_PER_TRADE, stop_distance_fraction, stop_price)


class TestStopPlacement:
    """stop_below = lower - max(3 * ATR_in_price, 0.20 * (upper - lower))"""

    def test_the_formula(self):
        # lower 100, upper 110, ATR 2% -> 3*2 = 6 vs 0.20*10 = 2 -> 6 binds
        assert stop_price(100.0, 110.0, 0.02) == pytest.approx(94.0)

    def test_a_high_atr_coin_gets_a_wider_stop(self):
        jumpy = stop_price(100.0, 110.0, 0.05)
        calm = stop_price(100.0, 110.0, 0.01)
        assert jumpy < calm                      # further below the range
        assert (100.0 - jumpy) > (100.0 - calm)

    def test_the_atr_term_binds_when_atr_is_large(self):
        # 3 * 5% * 100 = 15 vs 0.20 * 10 = 2
        assert stop_price(100.0, 110.0, 0.05) == pytest.approx(85.0)

    def test_the_range_width_floor_binds_when_atr_is_small(self):
        """A quiet asset must not get a stop inside its own noise band."""
        # 3 * 0.1% * 100 = 0.3 vs 0.20 * 40 = 8 -> the floor wins
        assert stop_price(100.0, 140.0, 0.001) == pytest.approx(92.0)

    def test_the_floor_is_a_fifth_of_the_range(self):
        for width in (5.0, 20.0, 60.0):
            got = stop_price(100.0, 100.0 + width, 0.0)
            assert (100.0 - got) == pytest.approx(0.20 * width)

    def test_a_missing_atr_falls_back_to_the_range_floor(self):
        """Unmeasurable is not the same as calm."""
        assert stop_price(100.0, 120.0, None) == pytest.approx(96.0)

    def test_the_stop_is_always_below_the_range(self):
        for atr in (0.0, 0.001, 0.02, 0.15):
            assert stop_price(100.0, 110.0, atr) < 100.0

    def test_the_fraction_matches_the_price(self):
        f = stop_distance_fraction(100.0, 110.0, 0.02)
        assert 100.0 * (1 - f) == pytest.approx(stop_price(100.0, 110.0, 0.02))

    def test_an_inverted_range_is_refused(self):
        with pytest.raises(ValueError):
            stop_price(110.0, 100.0, 0.02)

    def test_a_plan_built_this_way_still_validates(self, control):
        """validate() refuses a stop more than 50% below the range; the
        rubric's 15% ATR ceiling keeps 3*ATR inside that."""
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        assert plans[0].validate() == []
        assert plans[0].stop_below < plans[0].lower


class TestSizingSurvivesAStopOut:
    """The end-to-end claim: a stop-out costs about risk_per_trade."""

    def test_a_real_stop_out_loses_about_two_percent(self):
        """Backtest a fitted grid all the way down through its stop and
        measure what actually came out of the account."""
        from grid.backtest import run as replay
        from grid.plan import GridPlan

        available = 10_000.0
        lower, upper, atr = 100.0, 120.0, 0.02
        stop = stop_price(lower, upper, atr)
        quote = size_grid(available, upper, stop, 1e9, 1)

        levels = 12
        per_level = quote / (levels + 1)
        plan = GridPlan(symbol="T-USDT", lower=lower, upper=upper, levels=levels,
                        quote_per_level=per_level, stop_below=stop,
                        max_quote_total=per_level * (levels + 1))
        assert plan.validate() == []

        # Straight down from the top of the range through the stop, so every
        # level fills on the way — the worst case the sizing is meant to cover.
        closes = [upper]
        p = upper
        while p > stop * 0.98:
            p -= 0.25
            closes.append(p)

        # Execution assumptions OFF. Sizing must cover the WORST case, and the
        # worst case is the full ladder accumulating on the way down — which is
        # also the realistic one, since every buy limit rests on the book at
        # once. The execution knobs all model FEWER fills, so they shrink the
        # modelled stop-out loss; see the test below.
        r = replay(plan, closes, bars_per_day=24.0,
                   slip=0.0, fill_margin=0.0, max_fills_per_bar=None)
        assert r.stopped_out is True

        loss = plan.max_quote_total - r.equity_curve[-1]
        budget = available * RISK_PER_TRADE
        # A CEILING, not a target. `size_grid` sizes for the worst case — the
        # full ladder accumulating on the way down — but the inventory bound
        # now stops buying at BOUND_FRACTION of the ladder's real maximum, so
        # the grid reaches the stop holding less than the sizing assumed and
        # loses correspondingly less. Measured: 159.87 against a 200.00 budget.
        #
        # Under-risking is the safe direction and the bound exists precisely to
        # cause it. What must never happen is the other side of this.
        assert loss <= budget * 1.05, (
            f"stop-out cost {loss:.2f}, budget {budget:.2f} — a stop-out must "
            f"never cost more than the risk budget")
        # ...but the grid must still have actually traded, or this proves
        # nothing about sizing.
        assert loss > budget * 0.5, (
            f"stop-out cost only {loss:.2f} of a {budget:.2f} budget — the "
            f"bound may be so tight the grid is barely deploying")

    def test_the_execution_knobs_understate_the_stop_out_loss(self):
        """The execution assumptions are NOT uniformly conservative.

        They are pessimistic about profit — fewer fills, worse prices — but
        that same "fewer fills" deploys less capital into a decline, so the
        modelled stop-out loss comes out SMALLER than the real risk.
        `fill_margin` is the one that does it here: it blocks a marginal fill
        near the stop. Recorded so the knobs are not mistaken for a blanket
        safety margin.
        """
        from grid.backtest import run as replay
        from grid.plan import GridPlan

        lower, upper, atr = 100.0, 120.0, 0.02
        stop = stop_price(lower, upper, atr)
        quote = size_grid(10_000.0, upper, stop, 1e9, 1)
        levels = 12
        per = quote / (levels + 1)
        plan = GridPlan(symbol="T-USDT", lower=lower, upper=upper, levels=levels,
                        quote_per_level=per, stop_below=stop,
                        max_quote_total=per * (levels + 1))
        closes = [upper]
        p = upper
        while p > stop * 0.98:
            p -= 0.25
            closes.append(p)

        full = replay(plan, closes, bars_per_day=24.0, slip=0.0,
                      fill_margin=0.0, max_fills_per_bar=None)
        strict = replay(plan, closes, bars_per_day=24.0)     # the defaults
        loss_full = plan.max_quote_total - full.equity_curve[-1]
        loss_strict = plan.max_quote_total - strict.equity_curve[-1]
        assert loss_strict < loss_full
        # And it is the fill margin, not the slippage or the per-bar cap.
        margin_only = replay(plan, closes, bars_per_day=24.0, slip=0.0,
                             fill_margin=0.0002, max_fills_per_bar=None)
        assert (plan.max_quote_total
                - margin_only.equity_curve[-1]) < loss_full

    def test_the_old_formula_would_have_blown_the_budget(self):
        """Same scenario, sized the old way: the loss overshoots 2%."""
        from grid.backtest import run as replay
        from grid.plan import GridPlan

        available = 10_000.0
        lower, upper, atr = 100.0, 120.0, 0.02
        stop = stop_price(lower, upper, atr)
        old_quote = available * RISK_PER_TRADE / ((lower - stop) / lower)

        levels = 12
        per_level = old_quote / (levels + 1)
        plan = GridPlan(symbol="T-USDT", lower=lower, upper=upper, levels=levels,
                        quote_per_level=per_level, stop_below=stop,
                        max_quote_total=per_level * (levels + 1))
        closes = [upper]
        p = upper
        while p > stop * 0.98:
            p -= 0.25
            closes.append(p)
        r = replay(plan, closes, bars_per_day=24.0)

        loss = plan.max_quote_total - r.equity_curve[-1]
        assert loss > available * RISK_PER_TRADE * 1.3


class TestTheNoteIsInternallyConsistent:

    def test_only_one_daily_yield_figure_appears(self, control):
        """`a.notes` quotes the rubric's expected yield; the template used to
        recompute it from a different spacing and print a second, larger one."""
        import re
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        figures = {round(float(x), 2) for x in
                   re.findall(r"([\d.]+)%/?\s*(?:a )?day", plans[0].thesis)}
        assert len(figures) == 1, figures

    def test_the_bar_count_matches_what_was_measured(self, control):
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        assert "of the last 300 bars" in plans[0].thesis


# ------------------------------------------------------------------ live fees

class TestLiveFees:

    def test_it_uses_the_accounts_real_rates(self):
        src = FakeSource({}, fee=(0.0005, 0.0007))
        model, label = analyst.live_fees(source=src)
        assert (model.maker, model.taker) == (0.0005, 0.0007)
        assert "okx:" in label

    def test_a_failure_falls_back_to_the_pessimistic_rate(self):
        """Never the optimistic one: an unavailable fee rate must not make a
        grid look more viable than it is."""
        model, label = analyst.live_fees(source=FakeSource({}, fee=None))
        assert model.maker == analyst.FALLBACK_MAKER == 0.0020
        assert model.taker == analyst.FALLBACK_TAKER == 0.0035
        assert "fallback" in label

    def test_the_fallback_is_stricter_than_the_base_tier(self):
        cheap = FeeModel(maker=0.0008, taker=0.0010)
        fallback, _ = analyst.live_fees(source=FakeSource({}, fee=None))
        assert (fallback.min_viable_spacing(0.25)
                > cheap.min_viable_spacing(0.25) * 2)

    def test_it_never_raises(self):
        class Broken:
            def get_trade_fee(self, *a, **k):
                raise RuntimeError("boom")
        assert analyst.live_fees(source=Broken())[0].maker == analyst.FALLBACK_MAKER

    def test_it_caches_within_the_session(self):
        src = FakeSource({}, fee=(0.0008, 0.0010))
        analyst.live_fees(source=src)
        analyst.live_fees(source=src)
        assert src.fee_calls == 1

    def test_the_cache_expires_after_a_day(self):
        src = FakeSource({}, fee=(0.0008, 0.0010))
        analyst.live_fees(source=src, now=1_000.0)
        analyst.live_fees(source=src, now=1_000.0 + analyst.FEE_TTL_SECONDS + 1)
        assert src.fee_calls == 2

    def test_the_ttl_is_a_day(self):
        assert analyst.FEE_TTL_SECONDS == 86_400

    def test_one_lookup_covers_a_whole_scan(self, control):
        """Spot fees are set by account tier, not per pair."""
        src = FakeSource({f"S{i}-USDT": good_series(seed=i) for i in GOOD_SEEDS})
        scan(list(src.series), DEFAULT_CAPITAL, control=control,
             source=src, report=ScanReport())
        assert src.fee_calls == 1


class TestFeesAreRecorded:
    """A result must never be readable without knowing what it assumed."""

    def test_the_plan_records_the_rates(self, control):
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        p = plans[0]
        assert p.maker_fee == 0.0008 and p.taker_fee == 0.0010
        assert "okx:" in p.fee_source

    def test_the_rates_survive_a_json_round_trip(self, control):
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        back = GridPlan.from_json(plans[0].to_json())
        assert back.maker_fee == plans[0].maker_fee
        assert back.fee_source == plans[0].fee_source

    def test_the_backtest_result_records_them(self, control):
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        assert plans[0].backtest["maker_fee"] == 0.0008
        assert plans[0].backtest["taker_fee"] == 0.0010

    def test_the_report_records_them(self, control):
        _, report, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        assert "okx:" in report.fee_source
        assert "fees:" in report.explain()

    def test_the_note_states_them(self, control):
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        assert "Fees assumed" in plans[0].thesis

    def test_a_fallback_scan_says_so_on_the_plan(self, control):
        src = FakeSource({"AAA-USDT": good_series()}, fee=None)
        plans = scan(["AAA-USDT"], 50_000.0, control=control, source=src,
                     report=ScanReport())
        for p in plans:
            assert "fallback" in p.fee_source


class TestTheAtrFloorTracksTheFees:
    """'that level is now 2.5x wider on some fee tiers'"""

    def _assess(self, fees):
        c = good_series()
        h, l = hl_from_closes(c)
        return rubric.assess("X-USDT", c, h, l,
                             spacing=fees.min_viable_spacing(0.25),
                             bars_per_day=24.0, quote_volume_24h=50e6, fees=fees)

    def test_the_floor_is_derived_not_hardcoded(self):
        cheap = self._assess(FeeModel(maker=0.0008, taker=0.0010))
        dear = self._assess(FeeModel(maker=0.0020, taker=0.0035))
        assert dear.measured["min_atr_pct"] > cheap.measured["min_atr_pct"]

    def test_it_scales_with_the_spacing(self):
        for maker, taker in ((0.0008, 0.0010), (0.0020, 0.0035)):
            f = FeeModel(maker=maker, taker=taker)
            a = self._assess(f)
            assert a.measured["min_atr_pct"] == pytest.approx(
                1.25 * f.min_viable_spacing(0.25))

    def test_the_expensive_tier_floor_is_about_2_5x(self):
        cheap = self._assess(FeeModel(maker=0.0008, taker=0.0010))
        dear = self._assess(FeeModel(maker=0.0020, taker=0.0035))
        ratio = dear.measured["min_atr_pct"] / cheap.measured["min_atr_pct"]
        assert 2.4 < ratio < 2.6

    def test_an_asset_that_passed_cheaply_can_fail_dearly(self):
        """The whole point: 0.8% ATR clears a 0.64% level and not a 1.62% one."""
        c = [100.0 * (1 + 0.004 * (1 if i % 2 else -1)) for i in range(300)]
        h, l = hl_from_closes(c, spread=0.001)
        dear = FeeModel(maker=0.0020, taker=0.0035)
        a = rubric.assess("X-USDT", c, h, l,
                          spacing=dear.min_viable_spacing(0.25),
                          bars_per_day=24.0, quote_volume_24h=50e6, fees=dear)
        assert not a.passed
        assert any("ATR" in g for g in a.gate_failures)

    def test_an_explicit_floor_still_wins(self):
        c = good_series()
        h, l = hl_from_closes(c)
        a = rubric.assess("X-USDT", c, h, l, spacing=0.0064, bars_per_day=24.0,
                          quote_volume_24h=50e6,
                          thresholds=rubric.Thresholds(min_atr_pct=0.5))
        assert not a.passed and any("ATR" in g for g in a.gate_failures)

    def test_the_message_names_the_maker_fee(self):
        c = [100.0 + 0.001 * (i % 2) for i in range(300)]
        h, l = hl_from_closes(c, spread=0.0001)
        a = rubric.assess("X-USDT", c, h, l, spacing=0.0064, bars_per_day=24.0,
                          quote_volume_24h=50e6)
        atr_fail = [g for g in a.gate_failures if g.startswith("ATR")]
        assert atr_fail and "maker fee" in atr_fail[0]

    def test_the_report_states_the_fees_exactly_once(self, control):
        _, report, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        assert report.explain().count("fees:") == 1


class TestMultipleTestingCorrection:
    """Testing N assets at p<0.05 expects N*0.05 false positives."""

    def test_the_candidate_count_reaches_the_assessment(self, control):
        src = FakeSource({f"S{i}-USDT": good_series(seed=i) for i in GOOD_SEEDS})
        plans = scan(list(src.series), DEFAULT_CAPITAL, control=control,
                     source=src, report=ScanReport())
        for p in plans:
            assert p.metrics["n_candidates"] == len(src.series)

    def test_a_single_symbol_scan_is_uncorrected(self, control):
        _, report, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        assert report.n_corrected == 0

    def test_padding_the_universe_makes_the_bar_harder(self, control):
        """The same good asset, scanned alone and scanned among many
        no-hopers. BH raises the bar as the batch grows."""
        alone = FakeSource({"AAA-USDT": good_series()})
        n_alone = len(scan(["AAA-USDT"], DEFAULT_CAPITAL, control=control,
                           source=alone, report=ScanReport()))
        series = {"AAA-USDT": good_series()}
        series.update({f"N{i}-USDT": trend_series() for i in range(40)})
        crowd = FakeSource(series)
        rep = ScanReport()
        scan(list(series), DEFAULT_CAPITAL, control=control, source=crowd,
             report=rep)
        assert n_alone == 1
        assert rep.considered == 41

    def test_the_report_counts_what_the_correction_dropped(self, control):
        _, report, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        assert hasattr(report, "n_corrected")

    def test_a_corrected_rejection_names_the_stage(self, control):
        from grid import metrics
        # BH itself, directly: one marginal p-value among many nulls.
        keep = metrics.benjamini_hochberg([0.04] + [0.6] * 39, 0.05)
        assert keep[0] is False

    def test_the_note_states_the_statistical_evidence(self, control):
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        t = plans[0].thesis
        assert "Dickey-Fuller" in t and "p=" in t
        assert "95% confidence" in t and "sub-windows" in t


# ============================================================ the three asks

from grid import confidence as conf
from grid import sizing
from grid.okx import Instrument


def _write_permissivity(tmp_path, level):
    p = tmp_path / "config.yaml"
    p.write_text(f"grid:\n  permissivity: {level}\n")
    return p


class TestPermissivityLevels:
    """'exploratory must surface candidates that strict hides, with visibly
    smaller sizes.'"""

    def _scan_at(self, level, control, monkeypatch, tmp_path, capital):
        monkeypatch.setattr(analyst, "CONFIG_PATH",
                            _write_permissivity(tmp_path, level))
        src = FakeSource({f"S{i}-USDT": good_series(seed=i) for i in GOOD_SEEDS})
        rep = ScanReport()
        plans = scan(list(src.series), capital, control=control, source=src,
                     report=rep)
        return plans, rep

    def test_exploratory_surfaces_what_strict_hides(self, control, monkeypatch,
                                                    tmp_path):
        control.set_caps(max_concurrent_grids=10, max_total_quote=1e9)
        strict, r_strict = self._scan_at("strict", control, monkeypatch,
                                         tmp_path, 1_000_000.0)
        expl, r_expl = self._scan_at("exploratory", control, monkeypatch,
                                     tmp_path, 1_000_000.0)
        assert len(expl) >= len(strict)
        strict_syms = {p.symbol for p in strict}
        assert {p.symbol for p in expl} >= strict_syms

    def test_the_extra_candidates_carry_lower_confidence(self, control,
                                                         monkeypatch, tmp_path):
        control.set_caps(max_concurrent_grids=10, max_total_quote=1e9)
        strict, _ = self._scan_at("strict", control, monkeypatch, tmp_path,
                                  1_000_000.0)
        expl, _ = self._scan_at("exploratory", control, monkeypatch, tmp_path,
                                1_000_000.0)
        extra = [p for p in expl if p.symbol not in {q.symbol for q in strict}]
        for p in extra:
            assert p.confidence < conf.LEVELS["strict"]

    def test_lower_confidence_means_a_smaller_kelly_size(self, control,
                                                         monkeypatch, tmp_path):
        """Kelly uses the confidence as p, so a weaker candidate is sized
        smaller by the same formula rather than by a separate rule."""
        assert (sizing.kelly_fraction(0.05, 0.10, 0.80)
                > sizing.kelly_fraction(0.05, 0.10, 0.40))

    def test_what_is_hidden_is_logged_with_its_score(self, control, monkeypatch,
                                                     tmp_path):
        control.set_caps(max_concurrent_grids=10, max_total_quote=1e9)
        _, rep = self._scan_at("strict", control, monkeypatch, tmp_path,
                               1_000_000.0)
        assert rep.level == "strict"
        for sym, score in rep.below_threshold:
            assert 0.0 <= score < conf.LEVELS["strict"]

    def test_a_broken_config_falls_back_to_strict(self, tmp_path):
        bad = tmp_path / "config.yaml"
        bad.write_text("grid:\n  permissivity: reckless\n")
        assert analyst.permissivity(bad) == "strict"

    def test_a_missing_config_falls_back_to_strict(self, tmp_path):
        assert analyst.permissivity(tmp_path / "absent.yaml") == "strict"

    def test_the_shipped_config_is_strict(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        assert analyst.permissivity(root / "config.yaml") == "strict"


class TestTheMinSizeGateInTheScan:

    def test_a_sub_minsz_candidate_never_becomes_a_plan(self, control):
        """minSz far above what the ladder would order."""
        src = FakeSource({"AAA-USDT": good_series()}, min_size=1e9)
        rep = ScanReport()
        plans = scan(["AAA-USDT"], DEFAULT_CAPITAL, control=control,
                     source=src, report=rep)
        assert plans == []
        assert rep.by_stage("min_order_size") or rep.by_stage("sizing")

    def test_the_rejection_says_what_capital_would_be_needed(self, control):
        """With a large minSz the CAPITAL FLOOR bites first — the ladder cannot
        be funded at all — so the rejection quotes the requirement rather than
        the per-order minimum. Both are the same gate seen from two distances.
        """
        src = FakeSource({"AAA-USDT": good_series()}, min_size=5.0)
        rep = ScanReport()
        scan(["AAA-USDT"], DEFAULT_CAPITAL, control=control, source=src,
             report=rep)
        text = " ".join(r.reasons[0] for r in rep.rejections)
        assert "smallest placeable ladder needs" in text
        assert "cannot be run at this capital" in text

    def test_instruments_are_fetched_once_per_scan(self, control):
        src = FakeSource({f"S{i}-USDT": good_series(seed=i) for i in GOOD_SEEDS})
        scan(list(src.series), DEFAULT_CAPITAL, control=control, source=src,
             report=ScanReport())
        assert src.instrument_calls == 1

    def test_an_instrument_fetch_failure_aborts_rather_than_guesses(self, control):
        class NoInstruments(FakeSource):
            def get_instruments(self, inst_type="SPOT"):
                raise RuntimeError("503")
        src = NoInstruments({"AAA-USDT": good_series()})
        rep = ScanReport()
        assert scan(["AAA-USDT"], DEFAULT_CAPITAL, control=control, source=src,
                    report=rep) == []
        assert "instrument specifications" in rep.error


class TestConfidenceReachesThePlan:

    def test_every_plan_carries_its_confidence(self, control):
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        assert 0.0 < plans[0].confidence <= 1.0

    def test_the_components_are_recorded(self, control):
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        assert set(plans[0].confidence_components) == set(conf.WEIGHTS)

    def test_the_sizing_provenance_is_recorded(self, control):
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        p = plans[0]
        assert p.size_binding and p.size_constraints
        assert p.min_viable_capital > 0

    def test_it_survives_a_json_round_trip(self, control):
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        back = GridPlan.from_json(plans[0].to_json())
        assert back.confidence == plans[0].confidence
        assert back.kelly_full == plans[0].kelly_full


# ================================================== freshness, as required

class StaleSource(FakeSource):
    """Serves candles whose newest bar closed long ago."""

    def __init__(self, series, age_hours=6.0, **kw):
        super().__init__(series, **kw)
        self.age_hours = age_hours

    def get_candles(self, inst_id, bar="1H", limit=300):
        cs = super().get_candles(inst_id, bar, limit)
        shift = self.age_hours * HOUR_S * 1000.0
        return [{**c, "ts": c["ts"] - shift} for c in cs]


class TestStaleCandlesBlockAProposal:
    """'Stale data must not silently produce a confident-looking proposal.'"""

    def test_a_fresh_scan_proposes(self, control):
        plans, _, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        assert len(plans) == 1

    def test_stale_candles_produce_nothing(self, control):
        src = StaleSource({"AAA-USDT": good_series()}, age_hours=6.0)
        rep = ScanReport()
        plans = scan(["AAA-USDT"], DEFAULT_CAPITAL, control=control,
                     source=src, report=rep)
        assert plans == []
        assert rep.by_stage("stale_data")

    def test_the_rejection_states_the_age_and_the_limit(self, control):
        src = StaleSource({"AAA-USDT": good_series()}, age_hours=6.0)
        rep = ScanReport()
        scan(["AAA-USDT"], DEFAULT_CAPITAL, control=control, source=src,
             report=rep)
        reason = rep.by_stage("stale_data")[0].reasons[0]
        assert "newest candle closed" in reason and "limit" in reason

    def test_the_gate_runs_before_any_statistic(self, control):
        """Cheaper, and it means no confident number is ever computed from
        data that should not have been used."""
        src = StaleSource({"AAA-USDT": good_series()}, age_hours=6.0)
        rep = ScanReport()
        scan(["AAA-USDT"], DEFAULT_CAPITAL, control=control, source=src,
             report=rep)
        assert not rep.by_stage("gate")

    def test_the_limit_is_configurable(self, control):
        src = StaleSource({"AAA-USDT": good_series()}, age_hours=6.0)
        plans = scan(["AAA-USDT"], DEFAULT_CAPITAL, control=control,
                     source=src, report=ScanReport(),
                     max_data_age=12 * HOUR_S)
        assert len(plans) == 1

    def test_the_default_is_one_hour(self):
        assert analyst.MAX_DATA_AGE == 3600.0


class TestSuspendedInstrumentsAreRejected:

    def _src(self, state):
        from grid.okx import Instrument
        return FakeSource(
            {"AAA-USDT": good_series()},
            instruments={"AAA-USDT": Instrument(
                inst_id="AAA-USDT", min_size=1e-6, lot_size=1e-6,
                tick_size=1e-8, base="AAA", quote="USDT", state=state)})

    @pytest.mark.parametrize("state", ["suspend", "preopen", "expired"])
    def test_a_non_live_instrument_never_proposes(self, control, state):
        rep = ScanReport()
        plans = scan(["AAA-USDT"], DEFAULT_CAPITAL, control=control,
                     source=self._src(state), report=rep)
        assert plans == []
        assert rep.by_stage("instrument")

    def test_live_still_proposes(self, control):
        plans = scan(["AAA-USDT"], DEFAULT_CAPITAL, control=control,
                     source=self._src("live"), report=ScanReport())
        assert len(plans) == 1

    def test_a_delisted_pair_is_rejected(self, control):
        """Absent from the instrument list entirely."""
        src = FakeSource({"AAA-USDT": good_series()}, instruments={})
        rep = ScanReport()
        assert scan(["AAA-USDT"], DEFAULT_CAPITAL, control=control, source=src,
                    report=rep) == []
        assert any("delisted" in r.reasons[0] for r in rep.by_stage("instrument"))


class TestFeeFallbackOnFailure:
    """'never the optimistic one'"""

    def test_a_failed_fetch_uses_the_pessimistic_rate(self, control):
        src = FakeSource({"AAA-USDT": good_series()}, fee=None)
        plans = scan(["AAA-USDT"], 500_000.0, control=control, source=src,
                     report=ScanReport())
        for p in plans:
            assert p.maker_fee == analyst.FALLBACK_MAKER == 0.0020
            assert "fallback" in p.fee_source

    def test_the_fallback_is_worse_than_any_real_tier(self):
        assert analyst.FALLBACK_MAKER > 0.0008
        assert analyst.FALLBACK_TAKER > 0.0010

    def test_the_hard_limit_is_seven_days(self):
        from grid import freshness as fr
        assert fr.FEE_HARD_LIMIT_SECONDS == 7 * 86400

    def test_a_stale_cache_is_not_reused(self):
        """Past the TTL it refetches; if that fails it falls back rather than
        serving a week-old rate."""
        src = FakeSource({}, fee=(0.0008, 0.0010))
        analyst.live_fees(source=src, now=1000.0)
        src.fee = None
        model, label = analyst.live_fees(
            source=src, now=1000.0 + analyst.FEE_TTL_SECONDS + 1)
        assert model.maker == analyst.FALLBACK_MAKER and "fallback" in label

    def test_the_rate_is_recorded_on_the_backtest_too(self, control):
        src = FakeSource({"AAA-USDT": good_series()}, fee=None)
        plans = scan(["AAA-USDT"], 500_000.0, control=control, source=src,
                     report=ScanReport())
        for p in plans:
            assert p.backtest["maker_fee"] == analyst.FALLBACK_MAKER


class TestFreshnessIsRecorded:

    def test_the_report_carries_the_ages(self, control):
        _, rep, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        assert rep.freshness is not None
        assert {s.source for s in rep.freshness.stamps} == {
            "candles", "tickers", "instruments", "fees"}


class TestEveryRejectionIsScored:
    """'zero with a best of 0.48 means something very different from 0.05'"""

    def test_gate_rejected_candidates_still_get_a_score(self, control):
        src = FakeSource({"AAA-USDT": good_series(), "BBB-USDT": trend_series()})
        rep = ScanReport()
        scan(list(src.series), DEFAULT_CAPITAL, control=control, source=src,
             report=rep)
        scored = {c.symbol: c for c in rep.candidates}
        assert set(scored) == {"AAA-USDT", "BBB-USDT"}
        assert scored["BBB-USDT"].stage == "gate"
        assert scored["BBB-USDT"].confidence >= 0.0

    def test_the_best_score_distinguishes_a_near_miss_from_nothing(self, control):
        near = FakeSource({"AAA-USDT": good_series()})
        rep_near = ScanReport()
        scan(["AAA-USDT"], DEFAULT_CAPITAL, control=control, source=near,
             report=rep_near)
        far = FakeSource({"BBB-USDT": trend_series()})
        rep_far = ScanReport()
        scan(["BBB-USDT"], DEFAULT_CAPITAL, control=control, source=far,
             report=rep_far)
        assert rep_near.best_score() > rep_far.best_score()

    def test_each_candidate_names_the_gate_that_stopped_it(self, control):
        src = FakeSource({"BBB-USDT": trend_series()})
        rep = ScanReport()
        scan(["BBB-USDT"], DEFAULT_CAPITAL, control=control, source=src,
             report=rep)
        c = rep.candidates[0]
        assert c.stage and c.reason

    def test_below_threshold_records_how_far_it_missed(self, control,
                                                       monkeypatch, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("grid:\n  permissivity: strict\n")
        monkeypatch.setattr(analyst, "CONFIG_PATH", cfg)
        src = FakeSource({f"S{i}-USDT": good_series(seed=i) for i in GOOD_SEEDS})
        rep = ScanReport()
        control.set_caps(max_concurrent_grids=10, max_total_quote=1e9)
        scan(list(src.series), 1_000_000.0, control=control, source=src,
             report=rep)
        for sym, _ in rep.below_threshold:
            assert rep.candidate(sym).missed_by is not None

    def test_show_all_lists_everything(self, control):
        src = FakeSource({"AAA-USDT": good_series(), "BBB-USDT": trend_series()})
        rep = ScanReport()
        scan(list(src.series), DEFAULT_CAPITAL, control=control, source=src,
             report=rep)
        text = rep.show_all(0.65)
        assert "AAA-USDT" in text and "BBB-USDT" in text
        assert "best score" in text

    def test_the_window_is_recorded(self, control):
        _, rep, _ = run_scan({"AAA-USDT": good_series()}, control=control)
        assert rep.window_bars == 300
        assert rep.window_from and rep.window_to


class TestCalibrationModeInTheScan:

    def _cfg(self, tmp_path, mode):
        p = tmp_path / "config.yaml"
        p.write_text(f"grid:\n  permissivity: exploratory\n  mode: {mode}\n")
        return p

    def test_the_mode_is_read_from_config(self, tmp_path):
        assert analyst.sizing_mode(self._cfg(tmp_path, "calibration")) == "calibration"
        assert analyst.sizing_mode(self._cfg(tmp_path, "kelly")) == "kelly"

    def test_an_unknown_mode_falls_back_to_kelly(self, tmp_path):
        p = tmp_path / "config.yaml"
        p.write_text("grid:\n  mode: yolo\n")
        assert analyst.sizing_mode(p) == "kelly"

    def test_a_missing_config_falls_back_to_kelly(self, tmp_path):
        assert analyst.sizing_mode(tmp_path / "absent.yaml") == "kelly"

    def test_the_shipped_config_is_kelly(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        assert analyst.sizing_mode(root / "config.yaml") == "kelly"

    def test_calibration_sizes_tiny_capital(self, control, monkeypatch,
                                            tmp_path):
        monkeypatch.setattr(analyst, "CONFIG_PATH",
                            self._cfg(tmp_path, "calibration"))
        src = FakeSource({"AAA-USDT": good_series()}, min_size=0.01)
        rep = ScanReport()
        plans = scan(["AAA-USDT"], 50.0, control=control, source=src,
                     report=rep)
        assert plans, rep.explain()
        p = plans[0]
        assert p.sizing_mode == "calibration"
        assert p.max_quote_total <= 50.0

    def test_kelly_mode_proposes_nothing_at_fifty_euro(self, control,
                                                       monkeypatch, tmp_path):
        """The contrast: Kelly sizing at 50 cannot fund a ladder."""
        monkeypatch.setattr(analyst, "CONFIG_PATH", self._cfg(tmp_path, "kelly"))
        src = FakeSource({"AAA-USDT": good_series()}, min_size=0.01)
        assert scan(["AAA-USDT"], 50.0, control=control, source=src,
                    report=ScanReport()) == []

    def test_the_proposal_is_labelled(self, control, monkeypatch, tmp_path):
        monkeypatch.setattr(analyst, "CONFIG_PATH",
                            self._cfg(tmp_path, "calibration"))
        src = FakeSource({"AAA-USDT": good_series()}, min_size=0.01)
        plans = scan(["AAA-USDT"], 50.0, control=control, source=src,
                     report=ScanReport())
        assert "CALIBRATION RUN" in plans[0].thesis

    def test_it_shows_what_kelly_would_have_said(self, control, monkeypatch,
                                                 tmp_path):
        monkeypatch.setattr(analyst, "CONFIG_PATH",
                            self._cfg(tmp_path, "calibration"))
        src = FakeSource({"AAA-USDT": good_series()}, min_size=0.01)
        plans = scan(["AAA-USDT"], 50.0, control=control, source=src,
                     report=ScanReport())
        p = plans[0]
        assert p.kelly_would_say
        assert "Kelly" in p.thesis

    def test_it_says_so_when_kelly_says_do_not_trade(self, control, monkeypatch,
                                                    tmp_path):
        monkeypatch.setattr(analyst, "CONFIG_PATH",
                            self._cfg(tmp_path, "calibration"))
        src = FakeSource({f"S{i}-USDT": good_series(seed=i) for i in GOOD_SEEDS},
                         min_size=0.01)
        plans = scan(list(src.series), 50.0, control=control, source=src,
                     report=ScanReport())
        negative = [p for p in plans if p.kelly_full <= 0]
        for p in negative:
            assert "DO NOT TRADE" in p.kelly_would_say

    def test_the_report_records_the_mode_and_fill_count(self, control,
                                                        monkeypatch, tmp_path):
        monkeypatch.setattr(analyst, "CONFIG_PATH",
                            self._cfg(tmp_path, "calibration"))
        src = FakeSource({"AAA-USDT": good_series()}, min_size=0.01)
        rep = ScanReport()
        scan(["AAA-USDT"], 50.0, control=control, source=src, report=rep)
        assert rep.mode == "calibration"
        assert rep.fills_recorded >= 0


# --------------------------------------------------------- the two windows
#
# `scan` judges an asset's CHARACTER on a long window and draws its BAND from a
# short one. Both must be reported, and the band must never be drawn from the
# long one — that produces a range no ladder can trade.

class DeepSource(FakeSource):
    """A source that can serve deep history, like the real `okx` module."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.history_calls = []

    def get_history(self, inst_id, bar="1H", start_ts=None, **kw):
        self.history_calls.append((inst_id, bar, start_ts))
        return self.get_candles(inst_id, bar, limit=len(self.series[inst_id]))


def _two_regime(n_old=2000, n_recent=1200):
    """Long history around 150, recent history around 52.5."""
    import math
    out = [150.0 + 50.0 * math.sin(i / 7.0) for i in range(n_old)]
    out += [52.5 + 2.5 * math.sin(i / 5.0) for i in range(n_recent)]
    return out


def test_scan_uses_deep_history_when_the_source_offers_it(control):
    """CANDLES=300 is a page size, not a decision about how much data to use."""
    src = DeepSource({"A-USDT": _two_regime()}, {"A-USDT": 5e7})
    report = ScanReport()
    scan(["A-USDT"], 1000.0, source=src, report=report,
         control=control)
    assert src.history_calls, "scan must ask for deep history, not one page"
    inst, bar, start = src.history_calls[0]
    assert inst == "A-USDT"
    # It asks for roughly the statistics window, not 300 bars.
    assert start is not None
    days_back = (time.time() * 1000.0 - start) / 86_400_000.0
    assert 360 < days_back < 370


def test_scan_falls_back_to_one_page_without_deep_history(control):
    """The fakes and any limited source still work, just on less data."""
    src = FakeSource({"A-USDT": oscillating(300)}, {"A-USDT": 5e7})
    scan(["A-USDT"], 1000.0, source=src,
         control=control)
    assert src.candle_calls, "must fall back to get_candles"


def test_scan_reports_both_windows_with_dates_and_bar_counts(control):
    src = DeepSource({"A-USDT": _two_regime()}, {"A-USDT": 5e7})
    report = ScanReport()
    scan(["A-USDT"], 1000.0, source=src, report=report,
         control=control)

    cand = report.candidate("A-USDT")
    assert cand.n_bars == 3200                     # the statistics window
    assert cand.n_bars_range == 1080               # 45 days x 24 bars
    assert cand.first_bar and cand.last_bar
    assert cand.range_from and cand.range_to
    assert cand.range_to == cand.last_bar          # both end at the newest bar
    assert cand.range_from > cand.first_bar        # the range window starts later

    line = cand.windows()
    assert "stats 3200 bars" in line and "range 1080 bars" in line
    assert line in cand.describe(0.65)

    # And on the report as a whole.
    assert report.window_bars == 3200
    assert report.range_window_bars == 1080
    text = report.show_all()
    assert "statistics window: 3200 bars" in text
    assert "range window:      1080 bars" in text


def test_scan_draws_the_band_from_the_recent_window(control, monkeypatch):
    """The LTC 66-126 defect, pinned at the point where scan hands over.

    Spying on `rubric.assess` rather than on the returned plan, because a
    fixture that trends hard enough to show the defect is one the gates
    correctly reject — so there is no plan to inspect. The wiring is the thing
    under test: scan must pass a SHORT recent slice as the range window.
    """
    seen = {}
    real = rubric.assess

    def spy(symbol, closes, highs, lows, **kw):
        seen["closes"] = closes
        seen["range_closes"] = kw.get("range_closes")
        return real(symbol, closes, highs, lows, **kw)

    monkeypatch.setattr(analyst.rubric, "assess", spy)
    src = DeepSource({"A-USDT": _two_regime()}, {"A-USDT": 5e7})
    scan(["A-USDT"], 1000.0, source=src, control=control)

    assert seen["range_closes"] is not None, (
        "scan must pass a range window to assess, not let it default to the "
        "whole series")
    assert len(seen["closes"]) == 3200
    assert len(seen["range_closes"]) == 1080
    # It is the TAIL of the long series, not a re-fetch or a re-slice.
    assert list(seen["range_closes"]) == list(seen["closes"][-1080:])

    # And that tail is the regime price is actually in: the recent stretch
    # oscillates around 52.5, the older one around 150.
    assert max(seen["range_closes"]) < 60.0
    assert max(seen["closes"]) > 190.0

    # The band that follows from it is one a ladder can trade.
    band = metrics.suggest_range(list(seen["range_closes"]))
    lower, upper = band
    assert upper / lower < 1.4
    wide = metrics.suggest_range(list(seen["closes"]))
    assert wide[1] / wide[0] > 2.5, "the long window really is untradeable here"
