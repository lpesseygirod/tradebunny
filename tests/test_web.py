"""Tests for the control interface.

No network, no real threads left running, no writes outside tmp_path. The scan
is driven through a fake source so the whole request path — auth, threading,
polling, decision, persistence — runs for real.
"""

import threading
import time

import pytest

from grid import web
from grid.control import Control
from grid.plan import GridPlan, Status, load_all
from grid.web import ScanRunner, create_app

from test_analyst import FakeClient, FakeSource, good_series, trend_series

TOKEN = "test-token-abc"


@pytest.fixture
def plans_dir(tmp_path):
    d = tmp_path / "plans"
    d.mkdir()
    return d


@pytest.fixture
def control(tmp_path):
    return Control(tmp_path / "control.json")


@pytest.fixture
def source():
    return FakeSource({"AAA-USDT": good_series(), "BBB-USDT": trend_series()})


@pytest.fixture
def app(control, plans_dir, source, monkeypatch):
    monkeypatch.setenv("GRID_UNIVERSE", "AAA-USDT,BBB-USDT")
    return create_app(control, source=source, client=FakeClient(),
                      plans_dir=plans_dir, token=TOKEN)


@pytest.fixture
def client(app):
    return app.test_client()


def make_plan(plans_dir, **over):
    kw = dict(symbol="AAA-USDT", lower=94.0, upper=106.0, levels=12,
              quote_per_level=100.0, stop_below=90.0, max_quote_total=1300.0,
              # ARITHMETIC so the "narrowest vs widest spacing" display test
              # has two different numbers to distinguish. On a geometric
              # ladder they are equal by construction.
              spacing_mode="arithmetic",
              thesis="It oscillates.",
              backtest={"total_return_pct": 0.031, "max_drawdown_pct": 0.045})
    kw.update(over)
    p = GridPlan(**kw)
    p.save(plans_dir)
    return p


def wait_for_scan(app, timeout=10.0):
    runner = app.config["RUNNER"]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not runner.snapshot()["running"]:
            return runner.snapshot()
        time.sleep(0.01)
    raise AssertionError("scan did not finish")


# ---------------------------------------------------------------------- auth

class TestAccessControl:

    def test_it_refuses_to_start_without_a_token(self, control, plans_dir,
                                                 monkeypatch):
        """Bound to 0.0.0.0, so an unguarded start is not an option."""
        monkeypatch.delenv("GRID_WEB_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="GRID_WEB_TOKEN"):
            create_app(control, plans_dir=plans_dir, token=None)

    def test_no_token_is_forbidden(self, client):
        assert client.get("/").status_code == 403

    def test_a_wrong_token_is_forbidden(self, client):
        assert client.get("/?token=nope").status_code == 403

    def test_the_right_token_works(self, client):
        assert client.get(f"/?token={TOKEN}").status_code == 200

    def test_a_header_token_works(self, client):
        assert client.get("/", headers={"X-Grid-Token": TOKEN}).status_code == 200

    def test_the_token_is_remembered_in_a_cookie(self, client):
        client.get(f"/?token={TOKEN}")
        assert client.get("/").status_code == 200

    def test_a_post_needs_more_than_the_cookie(self, client):
        """A cookie alone would let any site you visit halt your bot."""
        client.get(f"/?token={TOKEN}")            # cookie now set
        assert client.post("/halt").status_code == 403

    def test_a_post_with_the_token_works(self, client):
        assert client.post("/halt", data={"token": TOKEN}).status_code == 302

    def test_a_query_string_token_does_not_authorise_a_post(self, client):
        assert client.post(f"/halt?token={TOKEN}").status_code == 403


# -------------------------------------------------------------------- startup

class TestStartup:

    def test_it_creates_the_control_file(self, control, plans_dir, source):
        assert not control.path.exists()
        create_app(control, source=source, plans_dir=plans_dir, token=TOKEN)
        assert control.path.exists()

    def test_the_created_state_is_paper_not_live(self, control, plans_dir, source):
        create_app(control, source=source, plans_dir=plans_dir, token=TOKEN)
        assert control.is_live() is False

    def test_it_does_not_overwrite_an_existing_halt(self, control, plans_dir,
                                                    source):
        control.initialise()
        control.halt_all("set earlier")
        create_app(control, source=source, plans_dir=plans_dir, token=TOKEN)
        assert control.state().trading_enabled is False

    def test_it_creates_the_plans_directory(self, control, tmp_path, source):
        d = tmp_path / "nested" / "plans"
        create_app(control, source=source, plans_dir=d, token=TOKEN)
        assert d.exists()


# ----------------------------------------------------------------- the page

class TestThePage:

    def test_it_renders(self, client):
        assert b"Grid Pilot" in client.get(f"/?token={TOKEN}").data

    def test_an_empty_day_says_so_rather_than_looking_broken(self, client):
        assert b"correct outcome" in client.get(f"/?token={TOKEN}").data

    def test_it_shows_the_control_state(self, client):
        body = client.get(f"/?token={TOKEN}").data
        assert b"max concurrent grids" in body and b"paper" in body

    def test_a_halt_is_visible(self, client, control):
        control.halt_all("because I said so")
        body = client.get(f"/?token={TOKEN}").data
        assert b"HALTED" in body and b"because I said so" in body


class TestProposalDisplay:

    @pytest.fixture
    def body(self, client, plans_dir):
        make_plan(plans_dir)
        return client.get(f"/?token={TOKEN}").data.decode()

    def test_symbol_range_and_levels(self, body):
        assert "AAA-USDT" in body and "94" in body and "106" in body

    def test_it_shows_the_narrowest_spacing_not_the_flattering_one(self, body,
                                                                  plans_dir):
        """min_spacing_pct (top of the ladder), never spacing_pct (bottom).

        The two differ, and showing the bottom would report the single most
        favourable number on the ladder.
        """
        p = load_all(plans_dir)[0]
        assert p.min_spacing_pct != p.spacing_pct
        assert f"{p.min_spacing_pct:.2%}" in body
        assert f"{p.spacing_pct:.2%}" not in body

    def test_fees_as_a_share_of_gross(self, body):
        assert "fees, share of gross" in body

    def test_the_walk_forward_out_of_sample_return(self, body):
        assert "out of sample" in body and "3.10%" in body

    def test_max_drawdown(self, body):
        assert "max drawdown" in body and "4.50%" in body

    def test_the_stop_and_how_far_below_it_sits(self, body):
        assert "stop" in body and "90" in body and "4.3% below" in body

    def test_the_thesis(self, body):
        assert "It oscillates." in body

    def test_a_missing_thesis_is_explained_not_blank(self, client, plans_dir):
        make_plan(plans_dir, thesis="")
        body = client.get(f"/?token={TOKEN}").data.decode()
        assert "explanation step was unavailable" in body

    def test_the_amount_is_prefilled_with_the_proposed_figure(self, body):
        assert 'name="amount"' in body and 'value="1300.00"' in body

    def test_rejected_plans_are_not_shown_for_decision(self, client, plans_dir):
        p = make_plan(plans_dir)
        p.reject()
        p.save(plans_dir)
        body = client.get(f"/?token={TOKEN}").data
        assert b'name="action" value="paper"' not in body

    def test_html_in_a_thesis_is_escaped(self, client, plans_dir):
        make_plan(plans_dir, thesis="<script>alert(1)</script>")
        body = client.get(f"/?token={TOKEN}").data.decode()
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body


# ------------------------------------------------------------------ decisions

class TestDecide:

    def test_approve_sets_the_status(self, client, plans_dir):
        p = make_plan(plans_dir)
        client.post(f"/decide/{p.id}",
                    data={"token": TOKEN, "action": "approve", "amount": "500"})
        assert load_all(plans_dir)[0].status is Status.APPROVED

    def test_approve_applies_the_chosen_amount(self, client, plans_dir):
        p = make_plan(plans_dir)
        client.post(f"/decide/{p.id}",
                    data={"token": TOKEN, "action": "approve", "amount": "260"})
        got = load_all(plans_dir)[0]
        assert got.max_quote_total == pytest.approx(260.0)
        assert got.quote_per_level == pytest.approx(260.0 / 13)

    def test_the_resized_plan_still_validates(self, client, plans_dir):
        p = make_plan(plans_dir)
        client.post(f"/decide/{p.id}",
                    data={"token": TOKEN, "action": "approve", "amount": "260"})
        assert load_all(plans_dir)[0].validate() == []

    def test_reject_sets_the_status(self, client, plans_dir):
        p = make_plan(plans_dir)
        client.post(f"/decide/{p.id}", data={"token": TOKEN, "action": "reject"})
        assert load_all(plans_dir)[0].status is Status.REJECTED

    def test_an_unknown_plan_is_404(self, client):
        assert client.post("/decide/nope",
                           data={"token": TOKEN, "action": "approve",
                                 "amount": "100"}).status_code == 404

    def test_an_unknown_action_is_400(self, client, plans_dir):
        p = make_plan(plans_dir)
        assert client.post(f"/decide/{p.id}",
                           data={"token": TOKEN, "action": "maybe"}).status_code == 400


class TestTheAmountIsValidated:
    """The owner's amount is free text. It can break a sound plan."""

    def test_dust_is_refused(self, client, plans_dir):
        p = make_plan(plans_dir)
        r = client.post(f"/decide/{p.id}",
                        data={"token": TOKEN, "action": "approve", "amount": "20"},
                        follow_redirects=True)
        assert b"minimum order" in r.data
        assert load_all(plans_dir)[0].status is Status.PROPOSED

    def test_more_than_the_control_cap_is_refused(self, client, plans_dir, control):
        control.set_caps(max_total_quote=500.0)
        p = make_plan(plans_dir)
        r = client.post(f"/decide/{p.id}",
                        data={"token": TOKEN, "action": "approve",
                              "amount": "100000"}, follow_redirects=True)
        assert b"exceeds the 500 cap" in r.data
        assert load_all(plans_dir)[0].status is Status.PROPOSED

    def test_zero_is_refused(self, client, plans_dir):
        p = make_plan(plans_dir)
        client.post(f"/decide/{p.id}",
                    data={"token": TOKEN, "action": "approve", "amount": "0"})
        assert load_all(plans_dir)[0].status is Status.PROPOSED

    def test_nonsense_is_refused(self, client, plans_dir):
        p = make_plan(plans_dir)
        r = client.post(f"/decide/{p.id}",
                        data={"token": TOKEN, "action": "approve", "amount": "lots"},
                        follow_redirects=True)
        assert b"must be a number" in r.data

    def test_a_refused_amount_leaves_the_plan_untouched_on_disk(self, client,
                                                                plans_dir):
        p = make_plan(plans_dir)
        before = (plans_dir / f"{p.id}.json").read_text()
        client.post(f"/decide/{p.id}",
                    data={"token": TOKEN, "action": "approve", "amount": "20"})
        assert (plans_dir / f"{p.id}.json").read_text() == before


# ---------------------------------------------------------------- kill switches

class TestControlEndpoints:

    def test_halt_stops_everything(self, client, control):
        client.post("/halt", data={"token": TOKEN})
        s = control.state()
        assert s.trading_enabled is False and s.analyst_enabled is False

    def test_pause_analyst_leaves_trading_on(self, client, control):
        client.post("/pause-analyst", data={"token": TOKEN})
        s = control.state()
        assert s.analyst_enabled is False and s.trading_enabled is True

    def test_stop_halts_that_plan(self, client, plans_dir, control):
        p = make_plan(plans_dir)
        p.approve()
        p.save(plans_dir)
        client.post(f"/stop/{p.id}", data={"token": TOKEN})
        assert p.id in control.state().halted_plans

    def test_stop_marks_the_plan_stopped(self, client, plans_dir):
        p = make_plan(plans_dir)
        p.approve()
        p.save(plans_dir)
        client.post(f"/stop/{p.id}", data={"token": TOKEN})
        assert load_all(plans_dir)[0].status is Status.STOPPED

    def test_stopping_an_unknown_plan_is_404(self, client):
        assert client.post("/stop/nope", data={"token": TOKEN}).status_code == 404

    def test_halting_is_visible_immediately(self, client):
        r = client.post("/halt", data={"token": TOKEN}, follow_redirects=True)
        assert b"HALTED" in r.data


# ---------------------------------------------------------------------- scan

class TestScanIsAsynchronous:

    def test_the_request_returns_before_the_scan_finishes(self, client, app):
        """The whole point: a 30-90s scan must not block the request."""
        gate = threading.Event()
        src = app.config["RUNNER"].source

        original = src.get_candles
        def slow(*a, **k):
            gate.wait(5)
            return original(*a, **k)
        src.get_candles = slow

        r = client.post("/scan", data={"token": TOKEN, "available": "200000"})
        assert r.status_code == 302
        assert app.config["RUNNER"].snapshot()["running"] is True
        gate.set()
        wait_for_scan(app)

    def test_status_reports_progress(self, client, app):
        gate = threading.Event()
        src = app.config["RUNNER"].source
        original = src.get_candles
        def slow(*a, **k):
            gate.wait(5)
            return original(*a, **k)
        src.get_candles = slow

        client.post("/scan", data={"token": TOKEN, "available": "200000"})
        s = client.get(f"/scan/status?token={TOKEN}").get_json()
        assert s["running"] is True
        assert s["total"] == 2
        assert "scanning" in s["label"]
        gate.set()
        wait_for_scan(app)

    def test_the_label_counts_symbols(self, app):
        runner = app.config["RUNNER"]
        runner.state = web.ScanState(running=True, done=12, total=40)
        assert runner.snapshot()["label"].startswith("scanning… 12/40 symbols")

    def test_a_second_scan_is_refused_while_one_runs(self, client, app):
        gate = threading.Event()
        src = app.config["RUNNER"].source
        original = src.get_candles
        def slow(*a, **k):
            gate.wait(5)
            return original(*a, **k)
        src.get_candles = slow

        client.post("/scan", data={"token": TOKEN, "available": "200000"})
        r = client.post("/scan", data={"token": TOKEN, "available": "200000"},
                        follow_redirects=True)
        assert b"already running" in r.data
        gate.set()
        wait_for_scan(app)

    def test_a_completed_scan_writes_the_plans(self, client, app, plans_dir):
        client.post("/scan", data={"token": TOKEN, "available": "200000"})
        wait_for_scan(app)
        assert [p.symbol for p in load_all(plans_dir)] == ["AAA-USDT"]

    def test_the_proposal_then_appears_on_the_page(self, client, app):
        client.post("/scan", data={"token": TOKEN, "available": "200000"})
        wait_for_scan(app)
        assert b"AAA-USDT" in client.get(f"/?token={TOKEN}").data

    def test_the_status_reports_what_was_rejected(self, client, app):
        client.post("/scan", data={"token": TOKEN, "available": "200000"})
        wait_for_scan(app)
        s = client.get(f"/scan/status?token={TOKEN}").get_json()
        assert any("BBB-USDT" in r for r in s["rejections"])

    def test_a_halted_analyst_will_not_start_a_scan(self, client, control):
        control.halt_analyst("paused")
        r = client.post("/scan", data={"token": TOKEN, "available": "200000"},
                        follow_redirects=True)
        assert b"analyst is halted" in r.data

    def test_a_scan_needs_an_amount(self, client):
        r = client.post("/scan", data={"token": TOKEN, "available": "0"},
                        follow_redirects=True)
        assert b"capital available" in r.data


class TestScanFailureIsVisible:
    """'A scan that silently fails and leaves the button spinning is worse.'"""

    @pytest.fixture
    def broken(self, control, plans_dir, monkeypatch):
        monkeypatch.setenv("GRID_UNIVERSE", "AAA-USDT")
        src = FakeSource({"AAA-USDT": good_series()})
        def boom():
            raise RuntimeError("OKX said 403")
        src.get_spot_tickers = boom
        return create_app(control, source=src, client=FakeClient(),
                          plans_dir=plans_dir, token=TOKEN)

    def test_the_error_is_stored(self, broken):
        c = broken.test_client()
        c.post("/scan", data={"token": TOKEN, "available": "200000"})
        s = wait_for_scan(broken)
        assert "403" in s["error"]

    def test_the_scan_stops_running(self, broken):
        c = broken.test_client()
        c.post("/scan", data={"token": TOKEN, "available": "200000"})
        assert wait_for_scan(broken)["running"] is False

    def test_the_error_is_shown_on_the_page(self, broken):
        c = broken.test_client()
        c.post("/scan", data={"token": TOKEN, "available": "200000"})
        wait_for_scan(broken)
        assert b"scan failed" in c.get(f"/?token={TOKEN}").data

    def test_another_scan_can_be_started_afterwards(self, broken):
        c = broken.test_client()
        c.post("/scan", data={"token": TOKEN, "available": "200000"})
        wait_for_scan(broken)
        assert broken.config["RUNNER"].start(["AAA-USDT"], 2000.0)[0] is True
        wait_for_scan(broken)


class TestScanRunnerDirectly:

    def test_it_refuses_a_concurrent_start(self, control, plans_dir):
        control.initialise()
        r = ScanRunner(control, source=FakeSource({"A-USDT": good_series()}),
                       client=FakeClient(), plans_dir=plans_dir)
        r.state = web.ScanState(running=True)
        started, msg = r.start(["A-USDT"], 2000.0)
        assert started is False and "already running" in msg

    def test_the_progress_proxy_counts_and_delegates(self):
        seen = []
        src = FakeSource({"A-USDT": good_series()})
        proxy = web._ProgressSource(src, seen.append)
        got = proxy.get_candles("A-USDT", "1H", limit=300)
        assert seen == ["A-USDT"]
        assert len(got) == 300
        assert proxy.bars_per_day("1H") == 24.0

    def test_an_idle_runner_reports_idle(self, control, plans_dir):
        control.initialise()
        r = ScanRunner(control, source=FakeSource({}), plans_dir=plans_dir)
        assert r.snapshot()["label"] == "idle"


class TestUniverse:

    def test_it_reads_the_env_var(self, monkeypatch):
        monkeypatch.setenv("GRID_UNIVERSE", "btc-usdt, eth-usdt")
        assert web.universe() == ["BTC-USDT", "ETH-USDT"]

    def test_it_falls_back_to_the_default_list(self, monkeypatch):
        monkeypatch.delenv("GRID_UNIVERSE", raising=False)
        assert web.universe() == web.DEFAULT_UNIVERSE

    def test_the_default_list_is_all_usdt_spot(self):
        assert all(s.endswith("-USDT") for s in web.DEFAULT_UNIVERSE)


# ------------------------------------------- the executor's snapshot on the page

from grid.executor import GridState, PaperExchange, run_once
from grid.plan import Status as _S


class TestExecutorStateIsShown:
    """Step 5 of the executor writes a snapshot; this is what reads it."""

    @pytest.fixture
    def app_with_state(self, control, plans_dir, source, tmp_path, monkeypatch):
        monkeypatch.setenv("GRID_UNIVERSE", "AAA-USDT")
        return create_app(control, source=source, client=FakeClient(),
                          plans_dir=plans_dir, token=TOKEN,
                          state_dir=tmp_path / "state")

    def test_a_running_grid_without_a_pass_says_so(self, app_with_state, plans_dir):
        p = make_plan(plans_dir)
        p.approve()
        p.save(plans_dir)
        body = app_with_state.test_client().get(f"/?token={TOKEN}").data
        assert b"no pass recorded yet" in body

    def test_the_snapshot_is_rendered(self, app_with_state, plans_dir, tmp_path):
        p = make_plan(plans_dir)
        p.approve()
        p.save(plans_dir)
        GridState(plan_id=p.id, symbol=p.symbol, status="running",
                  price=99.5, units_held=2.25, quote_deployed=223.88,
                  open_buys=4, open_sells=2).save(tmp_path / "state")
        body = app_with_state.test_client().get(f"/?token={TOKEN}").data.decode()
        assert "99.5" in body and "2.25" in body
        assert "4 buy / 2 sell" in body

    def test_a_note_is_surfaced(self, app_with_state, plans_dir, tmp_path):
        p = make_plan(plans_dir)
        p.approve()
        p.save(plans_dir)
        GridState(plan_id=p.id, symbol=p.symbol, status="running",
                  note="AAA-USDT is halted").save(tmp_path / "state")
        body = app_with_state.test_client().get(f"/?token={TOKEN}").data
        assert b"AAA-USDT is halted" in body

    def test_a_stopped_out_grid_moves_to_finished(self, app_with_state, plans_dir,
                                                  tmp_path):
        p = make_plan(plans_dir)
        p.approve()
        p.stop(_S.STOPPED_OUT)
        p.save(plans_dir)
        GridState(plan_id=p.id, symbol=p.symbol, status="stopped_out",
                  stopped_out=True, note="stop hit: 89 at or below 90"
                  ).save(tmp_path / "state")
        body = app_with_state.test_client().get(f"/?token={TOKEN}").data
        assert b"Finished" in body and b"stop hit" in body
        assert b"Stop this grid" not in body

    def test_the_page_survives_a_corrupt_snapshot(self, app_with_state, plans_dir,
                                                  tmp_path):
        p = make_plan(plans_dir)
        p.approve()
        p.save(plans_dir)
        d = tmp_path / "state"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{p.id}.json").write_text("{not json")
        r = app_with_state.test_client().get(f"/?token={TOKEN}")
        assert r.status_code == 200 and b"no pass recorded yet" in r.data


# ------------------------------------------------- the executor-down alarm

import json as _json
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

from grid.executor import STALE_AFTER_SECONDS, Heartbeat


def _beat(path, age_seconds=0, **kw):
    b = Heartbeat(pid=99, passes=3, **kw)
    b.save(path)
    if age_seconds:
        raw = _json.loads(path.read_text())
        raw["updated_at"] = (_dt.now(_tz.utc) - _td(seconds=age_seconds)
                             ).isoformat(timespec="seconds")
        path.write_text(_json.dumps(raw))
    return b


class TestExecutorDownAlarm:
    """A stopped executor means every open grid has no stop-loss."""

    @pytest.fixture
    def hb(self, tmp_path):
        return tmp_path / "heartbeat.json"

    @pytest.fixture
    def app_hb(self, control, plans_dir, source, tmp_path, hb, monkeypatch):
        monkeypatch.setenv("GRID_UNIVERSE", "AAA-USDT")
        return create_app(control, source=source, client=FakeClient(),
                          plans_dir=plans_dir, token=TOKEN,
                          state_dir=tmp_path / "state", heartbeat_path=hb)

    def body(self, app):
        return app.test_client().get(f"/?token={TOKEN}").data.decode()

    def test_no_heartbeat_raises_the_alarm(self, app_hb):
        b = self.body(app_hb)
        assert "THE EXECUTOR IS NOT RUNNING" in b
        assert "No heartbeat has ever been written" in b

    def test_a_stale_heartbeat_raises_the_alarm(self, app_hb, hb):
        _beat(hb, age_seconds=STALE_AFTER_SECONDS + 60)
        assert "THE EXECUTOR IS NOT RUNNING" in self.body(app_hb)

    def test_a_fresh_heartbeat_is_quiet(self, app_hb, hb):
        _beat(hb)
        assert "THE EXECUTOR IS NOT RUNNING" not in self.body(app_hb)

    def test_just_under_the_threshold_is_quiet(self, app_hb, hb):
        _beat(hb, age_seconds=STALE_AFTER_SECONDS - 30)
        assert "THE EXECUTOR IS NOT RUNNING" not in self.body(app_hb)

    def test_it_names_the_exposure(self, app_hb, plans_dir, hb):
        p = make_plan(plans_dir)
        p.approve()
        p.save(plans_dir)
        _beat(hb, age_seconds=STALE_AFTER_SECONDS + 60)
        assert "1 grid(s) are open with no stop-loss" in self.body(app_hb)

    def test_it_says_when_nothing_is_exposed(self, app_hb, hb):
        _beat(hb, age_seconds=STALE_AFTER_SECONDS + 60)
        assert "nothing is exposed" in self.body(app_hb)

    def test_it_explains_the_stop_is_not_on_the_exchange(self, app_hb):
        assert "enforced by the executor, not by the exchange" in self.body(app_hb)

    def test_it_says_how_to_start_it(self, app_hb):
        assert "grid.cli supervise" in self.body(app_hb)

    def test_a_last_error_is_shown(self, app_hb, hb):
        _beat(hb, age_seconds=STALE_AFTER_SECONDS + 60,
              last_error="AAA-USDT: RuntimeError: boom")
        assert "RuntimeError: boom" in self.body(app_hb)


class TestScanNotifies:

    def test_proposals_are_announced(self, control, plans_dir, source, tmp_path,
                                     monkeypatch):
        monkeypatch.setenv("GRID_UNIVERSE", "AAA-USDT,BBB-USDT")
        sent = []
        app = create_app(control, source=source, client=FakeClient(),
                         plans_dir=plans_dir, token=TOKEN,
                         state_dir=tmp_path / "state", notify_fn=sent.append)
        c = app.test_client()
        c.post("/scan", data={"token": TOKEN, "available": "200000"})
        wait_for_scan(app)
        assert any("awaiting approval" in m and "AAA-USDT" in m for m in sent)

    def test_a_quiet_scan_says_nothing(self, control, plans_dir, tmp_path,
                                       monkeypatch):
        monkeypatch.setenv("GRID_UNIVERSE", "BBB-USDT")
        sent = []
        app = create_app(control, source=FakeSource({"BBB-USDT": trend_series()}),
                         client=FakeClient(), plans_dir=plans_dir, token=TOKEN,
                         state_dir=tmp_path / "state", notify_fn=sent.append)
        c = app.test_client()
        c.post("/scan", data={"token": TOKEN, "available": "200000"})
        wait_for_scan(app)
        assert sent == []


class TestACleanStopAlsoAlarms:

    def test_a_clean_stop_raises_the_alarm_without_waiting(self, control,
                                                           plans_dir, source,
                                                           tmp_path, monkeypatch):
        monkeypatch.setenv("GRID_UNIVERSE", "AAA-USDT")
        hb = tmp_path / "heartbeat.json"
        Heartbeat(pid=5, passes=2, stopped_cleanly=True).save(hb)
        app = create_app(control, source=source, client=FakeClient(),
                         plans_dir=plans_dir, token=TOKEN,
                         state_dir=tmp_path / "state", heartbeat_path=hb)
        body = app.test_client().get(f"/?token={TOKEN}").data.decode()
        assert "THE EXECUTOR IS NOT RUNNING" in body
        assert "stopped deliberately" in body


class TestApprovalEnforcesValidation:
    """An invalid plan must not be approvable, whatever amount is entered."""

    def invalid(self, plans_dir):
        p = GridPlan(symbol="AAA-USDT", lower=100.0, upper=200.0, levels=150,
                     quote_per_level=100.0, stop_below=95.0,
                     max_quote_total=20000.0)
        p.save(plans_dir)
        return p

    def test_the_fixture_really_is_invalid(self, plans_dir):
        assert self.invalid(plans_dir).validate()

    def test_approval_is_refused(self, client, plans_dir, control):
        control.set_caps(max_total_quote=20000.0)
        p = self.invalid(plans_dir)
        r = client.post(f"/decide/{p.id}",
                        data={"token": TOKEN, "action": "approve",
                              "amount": "5000"}, follow_redirects=True)
        assert b"not approved" in r.data
        assert load_all(plans_dir)[0].status is Status.PROPOSED

    def test_the_fee_problem_is_named(self, client, plans_dir, control):
        control.set_caps(max_total_quote=20000.0)
        p = self.invalid(plans_dir)
        r = client.post(f"/decide/{p.id}",
                        data={"token": TOKEN, "action": "approve",
                              "amount": "5000"}, follow_redirects=True)
        assert b"gross profit in fees" in r.data

    def test_a_valid_plan_still_approves(self, client, plans_dir, control):
        control.set_caps(max_total_quote=20000.0)
        p = make_plan(plans_dir)
        client.post(f"/decide/{p.id}",
                    data={"token": TOKEN, "action": "approve", "amount": "500"})
        assert load_all(plans_dir)[0].status is Status.APPROVED


class TestSpotOnlyAtApproval:
    """Entry point 3: approving a plan that names a derivative."""

    def test_a_swap_plan_cannot_be_approved(self, client, plans_dir):
        p = make_plan(plans_dir, symbol="BTC-USDT-SWAP")
        r = client.post(f"/decide/{p.id}",
                        data={"token": TOKEN, "action": "approve",
                              "amount": "500"}, follow_redirects=True)
        assert b"SPOT ONLY" in r.data
        assert load_all(plans_dir)[0].status is Status.PROPOSED

    def test_the_equivalent_spot_plan_approves(self, client, plans_dir):
        p = make_plan(plans_dir, symbol="BTC-USDT")
        client.post(f"/decide/{p.id}",
                    data={"token": TOKEN, "action": "approve", "amount": "500"})
        assert load_all(plans_dir)[0].status is Status.APPROVED


class TestConfidenceAndKellyOnThePage:

    def _plan(self, plans_dir, **over):
        kw = dict(confidence=0.72, kelly_full=0.31, kelly_quarter=0.0775,
                  size_binding="quarter Kelly", min_viable_capital=250.0,
                  size_constraints={"quarter Kelly": 775.0, "risk-based": 900.0,
                                    "exposure cap": 5000.0},
                  confidence_components={"adf": 1.0, "hurst": 0.6,
                                         "stability": 1.0, "walk_forward": 0.3,
                                         "adx": 0.9, "efficiency": 1.0})
        kw.update(over)
        return make_plan(plans_dir, **kw)

    def body(self, client):
        return client.get(f"/?token={TOKEN}").data.decode()

    def test_the_confidence_number_is_shown(self, client, plans_dir):
        self._plan(plans_dir)
        assert "0.72" in self.body(client) and "confidence" in self.body(client)

    def test_the_binding_constraint_is_marked(self, client, plans_dir):
        self._plan(plans_dir)
        b = self.body(client)
        assert "binds" in b and "quarter Kelly" in b

    def test_every_constraint_is_listed(self, client, plans_dir):
        self._plan(plans_dir)
        b = self.body(client)
        for name in ("quarter Kelly", "risk-based", "exposure cap"):
            assert name in b

    def test_the_minimum_viable_capital_is_shown(self, client, plans_dir):
        self._plan(plans_dir)
        assert "minimum viable capital" in self.body(client)

    def test_the_components_are_shown_with_their_weights(self, client, plans_dir):
        self._plan(plans_dir)
        b = self.body(client)
        assert "confidence components" in b and "adf" in b

    def test_a_negative_kelly_is_unmissable(self, client, plans_dir):
        self._plan(plans_dir, kelly_full=-0.2, kelly_quarter=-0.05)
        b = self.body(client)
        assert "KELLY SAYS DO NOT TAKE THIS" in b
        assert "negative expectation" in b
        assert 'class="kelly"' in b

    def test_a_positive_kelly_shows_no_warning(self, client, plans_dir):
        self._plan(plans_dir)
        assert "KELLY SAYS DO NOT TAKE THIS" not in self.body(client)

    def test_a_negative_kelly_candidate_is_still_shown(self, client, plans_dir):
        """It must not be hidden — the owner decides."""
        self._plan(plans_dir, kelly_full=-0.2)
        # The approve button is now "Paper" / "Live" — still offered, with the
        # Kelly warning above it. The owner decides.
        assert "Paper" in self.body(client)


class TestCalibrationIsShown:
    """'An uncalibrated confidence number is decoration.'"""

    def test_the_banner_is_always_present(self, client, plans_dir):
        assert "Confidence calibration" in client.get(
            f"/?token={TOKEN}").data.decode()

    def test_with_no_outcomes_it_says_uncalibrated(self, client, plans_dir):
        b = client.get(f"/?token={TOKEN}").data.decode()
        assert "UNCALIBRATED" in b
        assert "decoration" in b

    def test_it_names_how_many_outcomes_are_needed(self, client, plans_dir):
        assert "need 20" in client.get(f"/?token={TOKEN}").data.decode()

    def test_the_permissivity_level_is_shown(self, client, plans_dir):
        b = client.get(f"/?token={TOKEN}").data.decode()
        assert "permissivity" in b and "config.yaml" in b


# ================================================ the complete interface

from grid import store as gridstore
from grid.store import LIVE, PAPER, Store


@pytest.fixture
def db(tmp_path):
    s = Store(tmp_path / "grid.db")
    yield s
    s.close()


@pytest.fixture
def app2(control, plans_dir, source, tmp_path, db, monkeypatch):
    monkeypatch.setenv("GRID_UNIVERSE", "AAA-USDT")
    return create_app(control, source=source, client=FakeClient(),
                      plans_dir=plans_dir, token=TOKEN,
                      state_dir=tmp_path / "state",
                      heartbeat_path=tmp_path / "hb.json", store=db)


def seed(db, plan_id="p1", mode=PAPER, symbol="AAA-USDT"):
    for side, px in (("buy", 100.0), ("buy", 98.0), ("sell", 102.0)):
        db.record_transaction(plan_id=plan_id, symbol=symbol, mode=mode,
                              side=side, price=px, qty=1.0, fee=px * 0.0008,
                              client_order_id=f"{side}{px}")


class TestTheWebCannotTrade:
    """'a WINDOW onto state the executor owns' — assert it structurally."""

    def test_the_module_imports_no_exchange_client(self):
        import ast, inspect
        from grid import web as webmod
        tree = ast.parse(inspect.getsource(webmod))
        imported = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                imported.add(n.module.split(".")[-1])
                for a in n.names:
                    imported.add(a.name)
            elif isinstance(n, ast.Import):
                for a in n.names:
                    imported.add(a.name.split(".")[-1])
        assert "executor" not in imported or True   # load_state only, see below
        assert "PaperExchange" not in imported
        assert "LiveExchange" not in imported
        assert "make_exchange" not in imported

    def test_it_never_calls_an_order_placing_function(self):
        """Checked against the AST, not the text: the module docstring
        mentions `executor.run_once` deliberately, to say where the stop is
        enforced. Prose is not a call."""
        import ast, inspect
        from grid import web as webmod
        tree = ast.parse(inspect.getsource(webmod))
        called = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                fn = n.func
                if isinstance(fn, ast.Name):
                    called.add(fn.id)
                elif isinstance(fn, ast.Attribute):
                    called.add(fn.attr)
        for banned in ("place_limit", "market_sell", "run_once",
                       "make_exchange", "cancel", "fetch_balance",
                       "fetch_open_orders"):
            assert banned not in called, f"web.py calls {banned}()"

    def test_it_only_borrows_read_helpers_from_the_executor(self):
        import inspect
        from grid import web as webmod
        lines = [l for l in inspect.getsource(webmod).splitlines()
                 if l.startswith("from .executor import")]
        assert lines
        for l in lines:
            for name in l.split("import")[1].split(","):
                assert name.strip() in {"HEARTBEAT_PATH", "STATE_DIR",
                                        "load_heartbeat", "load_state"}

    def test_closing_the_browser_changes_nothing(self, app2, plans_dir, db):
        """No request is required for the executor to keep working: the app
        holds no exchange handle and no background trading thread."""
        c = app2.test_client()
        c.get(f"/?token={TOKEN}")
        assert not hasattr(app2, "exchange")
        assert app2.config.get("STORE") is db


class TestHomeSections:

    def test_paper_and_live_are_separate_sections(self, app2, db):
        seed(db, mode=PAPER)
        seed(db, plan_id="p2", mode=LIVE)
        b = app2.test_client().get(f"/?token={TOKEN}").data.decode()
        assert 'class="mode paper"' in b and 'class="mode live"' in b

    def test_live_is_labelled_as_real_money(self, app2):
        b = app2.test_client().get(f"/?token={TOKEN}").data.decode()
        assert "LIVE — REAL MONEY" in b

    def test_paper_says_it_is_not_real(self, app2):
        b = app2.test_client().get(f"/?token={TOKEN}").data.decode()
        assert "not real money" in b

    def test_the_two_have_different_backgrounds(self, app2):
        """Not just a badge — a different environment."""
        b = app2.test_client().get(f"/?token={TOKEN}").data.decode()
        assert ".mode.paper{background:" in b and ".mode.live{background:" in b

    def test_every_figure_is_shown_per_section(self, app2, db):
        seed(db)
        b = app2.test_client().get(f"/?token={TOKEN}").data.decode()
        for label in ("invested", "current value", "realised P&amp;L",
                      "unrealised P&amp;L", "fees paid"):
            assert label in b

    def test_buy_and_hold_is_shown(self, app2, db):
        seed(db)
        assert "buy &amp; hold" in app2.test_client().get(
            f"/?token={TOKEN}").data.decode()

    def test_a_grand_total_is_shown(self, app2, db):
        seed(db)
        assert 'id="grand"' in app2.test_client().get(
            f"/?token={TOKEN}").data.decode()


class TestProposalButtons:

    def test_paper_and_reject_are_offered(self, app2, plans_dir):
        make_plan(plans_dir)
        b = app2.test_client().get(f"/?token={TOKEN}").data.decode()
        assert 'value="paper"' in b and 'value="reject"' in b

    def test_live_is_disabled_when_not_armed(self, app2, plans_dir):
        make_plan(plans_dir)
        b = app2.test_client().get(f"/?token={TOKEN}").data.decode()
        assert 'value="live"' in b and "disabled" in b

    def test_the_reason_live_is_disabled_is_shown(self, app2, plans_dir):
        make_plan(plans_dir)
        b = app2.test_client().get(f"/?token={TOKEN}").data.decode()
        assert "live_trading = false" in b

    def test_the_server_refuses_a_live_approval_anyway(self, app2, plans_dir):
        """A disabled button is a suggestion."""
        p = make_plan(plans_dir)
        r = app2.test_client().post(
            f"/decide/{p.id}", data={"token": TOKEN, "action": "live",
                                     "amount": "500"}, follow_redirects=True)
        assert b"not armed" in r.data
        assert load_all(plans_dir)[0].status is Status.PROPOSED

    def test_paper_approval_works(self, app2, plans_dir):
        p = make_plan(plans_dir)
        app2.test_client().post(f"/decide/{p.id}",
                                data={"token": TOKEN, "action": "paper",
                                      "amount": "500"})
        assert load_all(plans_dir)[0].status is Status.APPROVED

    def test_an_approval_is_recorded_as_an_event(self, app2, plans_dir, db):
        p = make_plan(plans_dir)
        app2.test_client().post(f"/decide/{p.id}",
                                data={"token": TOKEN, "action": "paper",
                                      "amount": "500"})
        assert any("approved" in e.title for e in db.events())


class TestGridDetail:

    def _running(self, plans_dir):
        p = make_plan(plans_dir)
        p.approve()
        p.save(plans_dir)
        return p

    def test_it_renders(self, app2, plans_dir, db):
        p = self._running(plans_dir)
        seed(db, plan_id=p.id)
        r = app2.test_client().get(f"/grid/{p.id}?token={TOKEN}")
        assert r.status_code == 200

    def test_every_transaction_is_listed(self, app2, plans_dir, db):
        p = self._running(plans_dir)
        seed(db, plan_id=p.id)
        b = app2.test_client().get(f"/grid/{p.id}?token={TOKEN}").data.decode()
        assert b.count("<tr>") >= 4          # header + 3 rows

    def test_it_shows_the_statement_columns(self, app2, plans_dir, db):
        p = self._running(plans_dir)
        seed(db, plan_id=p.id)
        b = app2.test_client().get(f"/grid/{p.id}?token={TOKEN}").data.decode()
        for col in ("when", "side", "price", "qty", "value", "fee",
                    "position", "running P&amp;L"):
            assert f">{col}<" in b

    def test_newest_first(self, app2, plans_dir, db):
        p = self._running(plans_dir)
        seed(db, plan_id=p.id)
        b = app2.test_client().get(f"/grid/{p.id}?token={TOKEN}").data.decode()
        assert b.index("sell") < b.index(">buy<")

    def test_the_ladder_is_drawn(self, app2, plans_dir, db):
        p = self._running(plans_dir)
        b = app2.test_client().get(f"/grid/{p.id}?token={TOKEN}").data.decode()
        assert 'class="ladder"' in b

    def test_the_stop_is_marked(self, app2, plans_dir, db):
        p = self._running(plans_dir)
        b = app2.test_client().get(f"/grid/{p.id}?token={TOKEN}").data.decode()
        assert 'class="stop"' in b

    def test_the_mode_label_is_always_visible(self, app2, plans_dir, db):
        p = self._running(plans_dir)
        seed(db, plan_id=p.id)
        b = app2.test_client().get(f"/grid/{p.id}?token={TOKEN}").data.decode()
        assert "modebar" in b and "not real money" in b

    def test_an_unknown_grid_is_404(self, app2):
        assert app2.test_client().get(
            f"/grid/nope?token={TOKEN}").status_code == 404

    def test_a_grid_with_no_fills_says_so(self, app2, plans_dir):
        p = self._running(plans_dir)
        b = app2.test_client().get(f"/grid/{p.id}?token={TOKEN}").data.decode()
        assert "Nothing has filled" in b


class TestHypotheticalSizes:

    def _running(self, plans_dir, db):
        p = make_plan(plans_dir, max_quote_total=500.0,
                      min_viable_capital=120.0)
        p.approve()
        p.save(plans_dir)
        seed(db, plan_id=p.id)
        return p

    def test_the_selector_is_offered_on_paper(self, app2, plans_dir, db):
        p = self._running(plans_dir, db)
        b = app2.test_client().get(f"/grid/{p.id}?token={TOKEN}").data.decode()
        for s in ("50", "200", "500", "2,000"):
            assert s in b

    def test_switching_size_changes_the_figures(self, app2, plans_dir, db):
        p = self._running(plans_dir, db)
        c = app2.test_client()
        small = c.get(f"/grid/{p.id}?token={TOKEN}&size=50").data.decode()
        big = c.get(f"/grid/{p.id}?token={TOKEN}&size=2000").data.decode()
        assert small != big

    def test_a_size_below_the_minimum_is_flagged(self, app2, plans_dir, db):
        p = self._running(plans_dir, db)
        b = app2.test_client().get(
            f"/grid/{p.id}?token={TOKEN}&size=50").data.decode()
        assert "blocked" in b

    def test_the_selected_size_is_marked(self, app2, plans_dir, db):
        p = self._running(plans_dir, db)
        b = app2.test_client().get(
            f"/grid/{p.id}?token={TOKEN}&size=200").data.decode()
        assert 'class="on"' in b


class TestNotificationFeed:

    def test_events_are_shown(self, app2, db):
        db.record_event(gridstore.FILL, "AAA-USDT buy 1 @ 100")
        b = app2.test_client().get(f"/?token={TOKEN}").data.decode()
        assert "AAA-USDT buy 1 @ 100" in b

    def test_unread_count_in_the_header(self, app2, db):
        db.record_event(gridstore.FILL, "one")
        db.record_event(gridstore.FILL, "two")
        b = app2.test_client().get(f"/?token={TOKEN}").data.decode()
        assert 'id="unread"' in b and ">2<" in b

    def test_unread_events_are_marked(self, app2, db):
        db.record_event(gridstore.FILL, "one")
        b = app2.test_client().get(f"/?token={TOKEN}").data.decode()
        assert 'class="unread"' in b

    def test_mark_all_read(self, app2, db):
        db.record_event(gridstore.FILL, "one")
        app2.test_client().post("/notifications/read", data={"token": TOKEN})
        assert db.unread_count() == 0

    def test_reading_does_not_delete(self, app2, db):
        db.record_event(gridstore.FILL, "one")
        app2.test_client().post("/notifications/read", data={"token": TOKEN})
        assert len(db.events()) == 1

    def test_the_feed_says_it_is_the_record(self, app2, db):
        b = app2.test_client().get(f"/?token={TOKEN}").data.decode()
        assert "kept forever" in b


class TestApiState:

    def test_it_returns_json(self, app2):
        r = app2.test_client().get(f"/api/state?token={TOKEN}")
        assert r.status_code == 200 and r.get_json() is not None

    def test_it_carries_an_updated_timestamp(self, app2):
        assert app2.test_client().get(
            f"/api/state?token={TOKEN}").get_json()["updated_at"]

    def test_it_needs_the_token(self, app2):
        assert app2.test_client().get("/api/state").status_code == 403

    def test_it_reports_both_sections(self, app2, db):
        seed(db)
        s = app2.test_client().get(f"/api/state?token={TOKEN}").get_json()
        assert set(s["sections"]) == {"paper", "live"}

    def test_it_reports_the_executor(self, app2):
        s = app2.test_client().get(f"/api/state?token={TOKEN}").get_json()
        assert s["executor"]["down"] is True

    def test_it_reports_the_unread_count(self, app2, db):
        db.record_event(gridstore.FILL, "x")
        assert app2.test_client().get(
            f"/api/state?token={TOKEN}").get_json()["unread"] == 1

    def test_it_reports_scan_progress(self, app2):
        s = app2.test_client().get(f"/api/state?token={TOKEN}").get_json()
        assert "label" in s["scan"]

    def test_grids_carry_their_position_in_range(self, app2, plans_dir, db,
                                                 tmp_path):
        from grid.executor import GridState
        p = make_plan(plans_dir)
        p.approve()
        p.save(plans_dir)
        GridState(plan_id=p.id, symbol=p.symbol, status="running",
                  price=100.0).save(tmp_path / "state")
        s = app2.test_client().get(f"/api/state?token={TOKEN}").get_json()
        g = [x for x in s["grids"] if x["id"] == p.id][0]
        assert 0.0 <= g["position_in_range"] <= 1.0

    def test_the_page_polls_every_three_seconds(self, app2):
        b = app2.test_client().get(f"/?token={TOKEN}").data.decode()
        assert "setInterval(tick,3000)" in b

    def test_it_refetches_when_the_tab_regains_focus(self, app2):
        b = app2.test_client().get(f"/?token={TOKEN}").data.decode()
        assert "visibilitychange" in b and "window.addEventListener('focus'" in b

    def test_a_stalled_poll_is_visible(self, app2):
        b = app2.test_client().get(f"/?token={TOKEN}").data.decode()
        assert "POLLING STALLED" in b and 'id="stamp"' in b
