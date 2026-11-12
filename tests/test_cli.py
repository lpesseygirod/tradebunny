"""Tests for the entry point, the executor loop and the supervisor.

No network. The loop is driven with `max_passes` and a fake `sleep`, so the real
`run_loop` runs — signals, heartbeat writes, per-plan error isolation — without
anything waiting a minute.
"""

import os
import signal
import subprocess
import threading
import time

import pytest

from grid import cli
from grid.cli import build_parser, live_plans, main, run_loop, supervise
from grid.control import Control
from grid.executor import (STALE_AFTER_SECONDS, Heartbeat, PaperExchange,
                           load_heartbeat)
from grid.plan import GridPlan, Status

SYMBOL = "AAA-USDT"


@pytest.fixture
def control(tmp_path):
    c = Control(tmp_path / "control.json")
    c.initialise()
    return c


@pytest.fixture
def dirs(tmp_path):
    return {"plans_dir": tmp_path / "plans", "state_dir": tmp_path / "state",
            "heartbeat_path": tmp_path / "heartbeat.json"}


def make_plan(plans_dir, approve=True, **over):
    kw = dict(symbol=SYMBOL, lower=94.0, upper=106.0, levels=12,
              quote_per_level=100.0, stop_below=90.0, max_quote_total=1300.0)
    kw.update(over)
    p = GridPlan(**kw)
    if approve:
        p.approve()
    p.save(plans_dir)
    return p


@pytest.fixture
def ex(tmp_path):
    e = PaperExchange(tmp_path / "paper.json", quote=1300.0)
    e.set_price(SYMBOL, 100.0)
    return e


def loop(control, ex, dirs, passes=1, **kw):
    return run_loop(control, exchange=ex, interval=0.0, max_passes=passes,
                    sleep=lambda s: None, **dirs, **kw)


# ------------------------------------------------------------------- parser

class TestCommands:

    @pytest.mark.parametrize("cmd", ["init", "scan", "run", "web", "status",
                                     "halt", "resume", "check", "supervise"])
    def test_every_command_parses(self, cmd):
        assert build_parser().parse_args([cmd]).command == cmd

    def test_backtest_takes_a_symbol(self):
        args = build_parser().parse_args(["backtest", "BTC-USDT"])
        assert args.symbol == "BTC-USDT"

    def test_backtest_requires_a_symbol(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["backtest"])

    def test_a_command_is_required(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_every_command_has_a_handler(self):
        for cmd in ("init", "scan", "run", "web", "status", "halt", "resume",
                    "check", "supervise"):
            assert callable(build_parser().parse_args([cmd]).fn)

    def test_there_is_no_cron_or_schedule_command(self):
        """The analyst runs when asked, never on a timer."""
        import inspect
        src = inspect.getsource(cli)
        assert "crontab" not in src and "schedule" not in src.lower()


class TestInit:

    def test_it_creates_the_control_file(self, tmp_path, capsys):
        path = tmp_path / "control.json"
        assert main(["--control", str(path), "init"]) == 0
        assert path.exists()

    def test_it_reports_paper_mode(self, tmp_path, capsys):
        main(["--control", str(tmp_path / "c.json"), "init"])
        assert "paper" in capsys.readouterr().out.lower()

    def test_running_it_twice_is_harmless(self, tmp_path):
        p = tmp_path / "c.json"
        main(["--control", str(p), "init"])
        Control(p).halt_all("set earlier")
        main(["--control", str(p), "init"])
        assert Control(p).state().trading_enabled is False


# --------------------------------------------------------------- the run loop

class TestRunLoop:

    def test_it_applies_a_live_plan(self, control, ex, dirs):
        make_plan(dirs["plans_dir"])
        loop(control, ex, dirs)
        assert ex.fetch_open_orders(SYMBOL)

    def test_it_writes_a_heartbeat(self, control, ex, dirs):
        make_plan(dirs["plans_dir"])
        loop(control, ex, dirs)
        beat = load_heartbeat(dirs["heartbeat_path"])
        assert beat is not None and beat.passes == 1
        assert beat.pid == os.getpid()

    def test_the_heartbeat_names_the_plans(self, control, ex, dirs):
        make_plan(dirs["plans_dir"])
        loop(control, ex, dirs)
        assert load_heartbeat(dirs["heartbeat_path"]).plans == [SYMBOL]

    def test_it_counts_passes(self, control, ex, dirs):
        make_plan(dirs["plans_dir"])
        loop(control, ex, dirs, passes=3)
        assert load_heartbeat(dirs["heartbeat_path"]).passes == 3

    def test_repeated_passes_do_not_duplicate_orders(self, control, ex, dirs):
        make_plan(dirs["plans_dir"])
        loop(control, ex, dirs, passes=5)
        ids = [o.client_order_id for o in ex.fetch_open_orders(SYMBOL)]
        assert len(ids) == len(set(ids))

    def test_it_beats_even_with_no_plans(self, control, ex, dirs):
        """A quiet system still has to prove it is alive."""
        loop(control, ex, dirs)
        beat = load_heartbeat(dirs["heartbeat_path"])
        assert beat is not None and beat.plans == []

    def test_only_live_plans_are_applied(self, control, ex, dirs):
        make_plan(dirs["plans_dir"], approve=False)      # stays PROPOSED
        loop(control, ex, dirs)
        assert ex.fetch_open_orders(SYMBOL) == []

    def test_live_plans_selects_approved_and_running(self, dirs):
        d = dirs["plans_dir"]
        make_plan(d, approve=False)
        make_plan(d)
        assert [p.status for p in live_plans(d)] == [Status.APPROVED]

    def test_one_bad_plan_does_not_stop_the_others(self, control, ex, dirs):
        """A transient error on one symbol must not take the stop-loss away
        from every other grid."""
        good = make_plan(dirs["plans_dir"], symbol="GOOD-USDT")
        ex.set_price("GOOD-USDT", 100.0)
        make_plan(dirs["plans_dir"], symbol="BAD-USDT")   # no price set -> raises

        sent = []
        loop(control, ex, dirs, notify=sent.append)
        assert ex.fetch_open_orders("GOOD-USDT")
        assert any("BAD-USDT" in m for m in sent)

    def test_a_failure_is_recorded_on_the_heartbeat(self, control, ex, dirs):
        make_plan(dirs["plans_dir"], symbol="BAD-USDT")
        loop(control, ex, dirs, notify=lambda m: None)
        assert "BAD-USDT" in load_heartbeat(dirs["heartbeat_path"]).last_error

    def test_a_halt_cancels_through_the_loop(self, control, ex, dirs):
        make_plan(dirs["plans_dir"])
        loop(control, ex, dirs)
        control.halt_all("stop")
        loop(control, ex, dirs)
        assert ex.fetch_open_orders(SYMBOL) == []

    def test_the_stop_fires_through_the_loop(self, control, ex, dirs):
        make_plan(dirs["plans_dir"])
        loop(control, ex, dirs)
        ex.advance_to(SYMBOL, 89.0)
        sent = []
        loop(control, ex, dirs, notify=sent.append)
        assert ex.fetch_balance(SYMBOL).base == pytest.approx(0.0)
        assert any("stopped out" in m for m in sent)


class TestCleanShutdown:
    """SIGTERM must not cancel orders — stopping is not selling."""

    def test_sigterm_exits_without_cancelling(self, control, ex, dirs):
        make_plan(dirs["plans_dir"])
        loop(control, ex, dirs)                       # ladder is resting
        before = {o.client_order_id for o in ex.fetch_open_orders(SYMBOL)}
        assert before

        def fire():
            time.sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=fire, daemon=True).start()
        beat = run_loop(control, exchange=ex, interval=0.2, max_passes=None,
                        sleep=time.sleep, notify=lambda m: None, **dirs)

        assert beat.stopped_cleanly is True
        assert {o.client_order_id
                for o in ex.fetch_open_orders(SYMBOL)} == before

    def test_the_final_heartbeat_says_it_was_clean(self, control, ex, dirs):
        def fire():
            time.sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=fire, daemon=True).start()
        run_loop(control, exchange=ex, interval=0.2, sleep=time.sleep,
                 notify=lambda m: None, **dirs)
        assert load_heartbeat(dirs["heartbeat_path"]).stopped_cleanly is True

    def test_the_handler_is_removed_afterwards(self, control, ex, dirs):
        loop(control, ex, dirs)
        assert signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, signal.SIG_IGN)

    def test_it_survives_running_off_the_main_thread(self, control, ex, dirs):
        """signal.signal raises off-thread; the loop must not care."""
        out = {}

        def go():
            out["beat"] = loop(control, ex, dirs)

        t = threading.Thread(target=go)
        t.start()
        t.join(10)
        assert out["beat"].passes == 1


# ------------------------------------------------------------- the heartbeat

class TestHeartbeat:

    def test_a_fresh_beat_is_not_stale(self):
        b = Heartbeat()
        b.updated_at = cli.datetime.now(cli.timezone.utc).isoformat(timespec="seconds")
        assert b.is_stale() is False

    def test_an_old_beat_is_stale(self):
        from datetime import timedelta
        b = Heartbeat()
        old = cli.datetime.now(cli.timezone.utc) - timedelta(
            seconds=STALE_AFTER_SECONDS + 60)
        b.updated_at = old.isoformat(timespec="seconds")
        assert b.is_stale() is True

    def test_the_threshold_is_five_minutes(self):
        assert STALE_AFTER_SECONDS == 300

    def test_a_beat_with_no_timestamp_is_stale(self):
        assert Heartbeat().is_stale() is True

    def test_a_missing_file_reads_as_none(self, tmp_path):
        assert load_heartbeat(tmp_path / "nope.json") is None

    def test_a_corrupt_file_reads_as_none(self, tmp_path):
        p = tmp_path / "hb.json"
        p.write_text("{not json")
        assert load_heartbeat(p) is None


# -------------------------------------------------------------- the supervisor

class FakeProc:
    def __init__(self, code):
        self._code = code

    def wait(self):
        return self._code


class TestSupervisor:

    def test_a_clean_exit_is_not_restarted(self):
        spawned = []

        def spawn(argv):
            spawned.append(argv)
            return FakeProc(0)

        assert supervise(["x"], spawn=spawn, sleep=lambda s: None,
                         notify=lambda m: None) == 0
        assert len(spawned) == 1

    def test_a_crash_is_restarted(self):
        spawned = []

        def spawn(argv):
            spawned.append(argv)
            return FakeProc(1 if len(spawned) < 3 else 0)

        supervise(["x"], spawn=spawn, sleep=lambda s: None,
                  notify=lambda m: None)
        assert len(spawned) == 3

    def test_a_crash_notifies(self):
        sent = []
        codes = iter([1, 0])
        supervise(["x"], spawn=lambda a: FakeProc(next(codes)),
                  sleep=lambda s: None, notify=sent.append)
        assert any("died" in m for m in sent)

    def test_the_notification_says_what_is_at_risk(self):
        sent = []
        codes = iter([1, 0])
        supervise(["x"], spawn=lambda a: FakeProc(next(codes)),
                  sleep=lambda s: None, notify=sent.append)
        assert "no stop-loss" in sent[0]

    def test_it_backs_off(self):
        waits = []
        supervise(["x"], spawn=lambda a: FakeProc(1), sleep=waits.append,
                  notify=lambda m: None, max_restarts=4)
        assert waits == sorted(waits) and waits[-1] > waits[0]

    def test_backoff_is_capped(self):
        waits = []
        supervise(["x"], spawn=lambda a: FakeProc(1), sleep=waits.append,
                  notify=lambda m: None, max_restarts=30)
        assert max(waits) <= cli.MAX_RESTART_DELAY

    def test_it_gives_up_after_max_restarts(self):
        spawned = []
        supervise(["x"], spawn=lambda a: spawned.append(a) or FakeProc(1),
                  sleep=lambda s: None, notify=lambda m: None, max_restarts=3)
        assert len(spawned) == 3


# ------------------------------------------------------------------- status

class TestStatus:

    def test_it_warns_when_there_is_no_heartbeat(self, tmp_path, capsys):
        main(["--control", str(tmp_path / "c.json"), "init"])
        main(["--control", str(tmp_path / "c.json"), "status",
              "--heartbeat", str(tmp_path / "none.json"),
              "--state-dir", str(tmp_path / "state")])
        out = capsys.readouterr().out
        assert "NOT RUNNING" in out and "no stop-loss" in out

    def test_it_warns_when_the_heartbeat_is_stale(self, tmp_path, capsys):
        from datetime import timedelta
        main(["--control", str(tmp_path / "c.json"), "init"])
        hb = tmp_path / "hb.json"
        b = Heartbeat(pid=1)
        b.save(hb)
        old = (cli.datetime.now(cli.timezone.utc)
               - timedelta(seconds=STALE_AFTER_SECONDS + 60))
        import json
        raw = json.loads(hb.read_text())
        raw["updated_at"] = old.isoformat(timespec="seconds")
        hb.write_text(json.dumps(raw))

        main(["--control", str(tmp_path / "c.json"), "status",
              "--heartbeat", str(hb), "--state-dir", str(tmp_path / "s")])
        assert "STALE" in capsys.readouterr().out

    def test_it_reports_a_live_executor(self, tmp_path, capsys):
        main(["--control", str(tmp_path / "c.json"), "init"])
        hb = tmp_path / "hb.json"
        Heartbeat(pid=4242, passes=7).save(hb)
        main(["--control", str(tmp_path / "c.json"), "status",
              "--heartbeat", str(hb), "--state-dir", str(tmp_path / "s")])
        out = capsys.readouterr().out
        assert "alive" in out and "4242" in out


class TestHaltAndResume:

    def test_halt_stops_everything(self, tmp_path, capsys):
        p = tmp_path / "c.json"
        main(["--control", str(p), "init"])
        main(["--control", str(p), "halt"])
        s = Control(p).state()
        assert s.trading_enabled is False and s.analyst_enabled is False

    def test_halt_says_positions_are_not_sold(self, tmp_path, capsys):
        p = tmp_path / "c.json"
        main(["--control", str(p), "init"])
        main(["--control", str(p), "halt"])
        assert "NOT liquidated" in capsys.readouterr().out

    def test_resume_clears_halts(self, tmp_path):
        p = tmp_path / "c.json"
        main(["--control", str(p), "init"])
        main(["--control", str(p), "halt"])
        main(["--control", str(p), "resume"])
        assert Control(p).state().trading_enabled is True

    def test_resume_does_not_re_arm_live_trading(self, tmp_path, capsys):
        p = tmp_path / "c.json"
        main(["--control", str(p), "init"])
        main(["--control", str(p), "resume"])
        assert Control(p).is_live() is False
        assert "NOT re-armed" in capsys.readouterr().out


class TestRunCommandGuards:

    def test_run_refuses_without_a_control_file(self, tmp_path, capsys):
        code = main(["--control", str(tmp_path / "absent.json"), "run",
                     "--max-passes", "1"])
        assert code == 2
        assert "init" in capsys.readouterr().err

    def test_web_refuses_without_a_token(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("GRID_WEB_TOKEN", raising=False)
        main(["--control", str(tmp_path / "c.json"), "init"])
        assert main(["--control", str(tmp_path / "c.json"), "web"]) == 2
        assert "GRID_WEB_TOKEN" in capsys.readouterr().err


class TestACleanStopIsAlsoDown:
    """A cleanly stopped executor is still an executor that is not running."""

    def test_a_clean_stop_reads_as_down_immediately(self):
        b = Heartbeat(stopped_cleanly=True)
        b.updated_at = cli.datetime.now(cli.timezone.utc).isoformat(timespec="seconds")
        assert b.is_stale() is False      # fresh by age
        assert b.is_down() is True        # but we know it exited

    def test_a_running_executor_is_not_down(self):
        b = Heartbeat()
        b.updated_at = cli.datetime.now(cli.timezone.utc).isoformat(timespec="seconds")
        assert b.is_down() is False

    def test_status_reports_a_clean_stop(self, tmp_path, capsys):
        main(["--control", str(tmp_path / "c.json"), "init"])
        hb = tmp_path / "hb.json"
        Heartbeat(pid=7, passes=4, stopped_cleanly=True).save(hb)
        main(["--control", str(tmp_path / "c.json"), "status",
              "--heartbeat", str(hb), "--state-dir", str(tmp_path / "s")])
        out = capsys.readouterr().out
        assert "STOPPED" in out and "no stop-loss" in out

    def test_the_loop_marks_a_clean_stop(self, control, ex, dirs):
        """After SIGTERM the file says so, so the UI warns straight away."""
        import os as _os, signal as _sig, threading as _th, time as _t

        def fire():
            _t.sleep(0.05)
            _os.kill(_os.getpid(), _sig.SIGTERM)

        _th.Thread(target=fire, daemon=True).start()
        run_loop(control, exchange=ex, interval=0.2, sleep=_t.sleep,
                 notify=lambda m: None, **dirs)
        assert load_heartbeat(dirs["heartbeat_path"]).is_down() is True


class TestDriftCheckInTheLoop:
    """A grid can drift into a trending market and bleed without hitting its
    stop. This is the check for that, and it must not close anything."""

    def _trending_source(self, symbol):
        import random, time as _t

        class Src:
            def bars_per_day(self, bar):
                return 24.0

            def get_candles(self, inst_id, bar="1H", limit=300):
                r = random.Random(7)
                p, out = 100.0, []
                now = _t.time()
                for k in range(300):
                    p *= (1 + 0.004 + r.gauss(0, 0.004))
                    out.append({"ts": (now - (300 - k) * 3600) * 1000.0,
                                "open": p, "high": p * 1.004, "low": p * 0.996,
                                "close": p, "volume": 1.0})
                return out
        return Src()

    def _plan(self, plans_dir, approved_hours_ago=250.0):
        from datetime import datetime, timedelta, timezone
        p = GridPlan(symbol=SYMBOL, lower=94.0, upper=106.0, levels=12,
                     quote_per_level=100.0, stop_below=85.0,
                     max_quote_total=1300.0,
                     metrics={"adf_p": 0.001, "hurst": 0.30, "adx": 12.0})
        p.approve()
        # Backdated: drift is measured on bars SINCE approval, so a grid
        # approved seconds ago has no post-approval history to judge.
        p.decided_at = (datetime.now(timezone.utc)
                        - timedelta(hours=approved_hours_ago)
                        ).isoformat(timespec="seconds")
        p.save(plans_dir)
        return p

    def test_a_freshly_approved_grid_is_skipped_not_flagged(self, control, dirs):
        """No post-approval bars means nothing to judge. Silence is correct."""
        self._plan(dirs["plans_dir"], approved_hours_ago=0.0)
        sent = []
        reports = cli.check_drift(control, plans_dir=dirs["plans_dir"],
                                  source=self._trending_source(SYMBOL),
                                  notify=sent.append)
        assert sent == []
        assert reports and reports[0].skipped

    def test_a_decayed_thesis_notifies(self, control, dirs):
        p = self._plan(dirs["plans_dir"])
        sent = []
        cli.check_drift(control, plans_dir=dirs["plans_dir"],
                        source=self._trending_source(SYMBOL),
                        notify=sent.append)
        assert sent and "thesis" in sent[0]

    def test_the_alert_shows_before_and_after(self, control, dirs):
        self._plan(dirs["plans_dir"])
        sent = []
        cli.check_drift(control, plans_dir=dirs["plans_dir"],
                        source=self._trending_source(SYMBOL),
                        notify=sent.append)
        assert "at approval" in sent[0] and "->" in sent[0]

    def test_it_does_not_stop_the_grid(self, control, dirs):
        """Stopping is the owner's decision — same rule as halt_all."""
        p = self._plan(dirs["plans_dir"])
        cli.check_drift(control, plans_dir=dirs["plans_dir"],
                        source=self._trending_source(SYMBOL),
                        notify=lambda m: None)
        reloaded = live_plans(dirs["plans_dir"])
        assert len(reloaded) == 1
        assert reloaded[0].status is Status.APPROVED

    def test_the_alert_says_it_did_not_stop_anything(self, control, dirs):
        self._plan(dirs["plans_dir"])
        sent = []
        cli.check_drift(control, plans_dir=dirs["plans_dir"],
                        source=self._trending_source(SYMBOL),
                        notify=sent.append)
        assert "STILL RUNNING" in sent[0]

    def test_no_live_grids_means_no_alerts(self, control, dirs):
        sent = []
        cli.check_drift(control, plans_dir=dirs["plans_dir"],
                        source=self._trending_source(SYMBOL),
                        notify=sent.append)
        assert sent == []

    def test_a_fetch_failure_does_not_stop_the_check(self, control, dirs):
        class Broken:
            def get_candles(self, *a, **k):
                raise RuntimeError("503")
        self._plan(dirs["plans_dir"])
        assert cli.check_drift(control, plans_dir=dirs["plans_dir"],
                               source=Broken(), notify=lambda m: None) == []

    def test_the_loop_runs_it_on_the_first_pass(self, control, ex, dirs):
        self._plan(dirs["plans_dir"])
        sent = []
        run_loop(control, exchange=ex, interval=0.0, max_passes=1,
                 sleep=lambda s: None, notify=sent.append,
                 drift_source=self._trending_source(SYMBOL), **dirs)
        assert any("thesis" in m for m in sent)

    def test_the_loop_can_have_it_disabled(self, control, ex, dirs):
        self._plan(dirs["plans_dir"])
        sent = []
        run_loop(control, exchange=ex, interval=0.0, max_passes=1,
                 sleep=lambda s: None, notify=sent.append, drift_interval=0,
                 drift_source=self._trending_source(SYMBOL), **dirs)
        assert not any("thesis" in m for m in sent)

    def test_it_does_not_run_twice_in_a_day(self, control, ex, dirs):
        self._plan(dirs["plans_dir"])
        sent = []
        run_loop(control, exchange=ex, interval=0.0, max_passes=3,
                 sleep=lambda s: None, notify=sent.append,
                 drift_source=self._trending_source(SYMBOL), **dirs)
        assert sum(1 for m in sent if "thesis" in m) == 1


class TestFreshnessCommand:

    def test_it_parses(self):
        assert build_parser().parse_args(["freshness"]).command == "freshness"

    def test_drift_parses(self):
        assert build_parser().parse_args(["drift"]).command == "drift"

    def test_drift_takes_a_notify_flag(self):
        assert build_parser().parse_args(["drift", "--notify"]).notify is True


class TestServiceCommand:

    def test_it_parses_with_a_default_action(self):
        assert build_parser().parse_args(["service"]).action == "status"

    @pytest.mark.parametrize("action",
                             ["status", "install", "uninstall", "print"])
    def test_every_action_parses(self, action):
        assert build_parser().parse_args(["service", action]).action == action

    def test_an_unknown_action_is_refused(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["service", "restart"])

    def test_print_emits_a_valid_plist(self, capsys):
        import plistlib
        main(["service", "print"])
        out = capsys.readouterr().out
        assert plistlib.loads(out.encode())["Label"] == "com.gridpilot.executor"

    def test_status_reports_without_touching_launchd_state(self, capsys):
        code = main(["service", "status"])
        out = capsys.readouterr().out
        assert "launchd agent" in out
        assert code in (0, 1)

    def test_status_exits_nonzero_when_not_running(self, capsys, monkeypatch):
        from grid import service as svc
        monkeypatch.setattr(svc, "status", lambda *a, **k: svc.ServiceStatus(
            label="x", installed=False, loaded=False, plist="/tmp/x"))
        assert main(["service", "status"]) == 1


class TestTheLoopRotatesLogs:

    def test_rotation_runs_from_the_executor_loop(self, control, ex, dirs,
                                                  monkeypatch):
        """launchd never rotates; something that ticks has to."""
        calls = []
        from grid import service as svc
        monkeypatch.setattr(svc, "rotate_logs",
                            lambda *a, **k: calls.append(1) or [])
        run_loop(control, exchange=ex, interval=0.0, max_passes=2,
                 sleep=lambda s: None, notify=lambda m: None,
                 drift_interval=0, **dirs)
        assert len(calls) == 2

    def test_a_rotation_failure_does_not_stop_the_loop(self, control, ex, dirs,
                                                       monkeypatch):
        from grid import service as svc

        def boom(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(svc, "rotate_logs", boom)
        beat = run_loop(control, exchange=ex, interval=0.0, max_passes=1,
                        sleep=lambda s: None, notify=lambda m: None,
                        drift_interval=0, **dirs)
        assert beat.passes == 1


class TestGridCommand:
    """`cli grid <id>` prints the same thing the web shows."""

    def _seed(self, tmp_path, monkeypatch):
        from grid.store import PAPER, Store
        from grid import cli as climod
        plans = tmp_path / "plans"
        monkeypatch.setattr(climod, "PLANS_DIR", plans)
        p = GridPlan(symbol="ZZZ-USDT", lower=48.0, upper=56.0, levels=10,
                     quote_per_level=50.0, stop_below=44.0,
                     max_quote_total=550.0)
        p.approve()
        p.save(plans)
        db = Store(tmp_path / "grid.db")
        for side, px in (("buy", 50.0), ("sell", 52.0)):
            db.record_transaction(plan_id=p.id, symbol="ZZZ-USDT", mode=PAPER,
                                  side=side, price=px, qty=1.0, fee=0.04,
                                  client_order_id=f"{side}{px}")
        db.close()
        return p

    def test_it_prints_the_statement(self, tmp_path, monkeypatch, capsys):
        p = self._seed(tmp_path, monkeypatch)
        assert main(["grid", p.id, "--db", str(tmp_path / "grid.db")]) == 0
        out = capsys.readouterr().out
        assert "transaction(s), newest first" in out
        assert "run P&L" in out

    def test_it_can_be_found_by_symbol(self, tmp_path, monkeypatch, capsys):
        self._seed(tmp_path, monkeypatch)
        assert main(["grid", "ZZZ-USDT", "--db", str(tmp_path / "grid.db")]) == 0

    def test_the_mode_banner_is_unmissable(self, tmp_path, monkeypatch, capsys):
        p = self._seed(tmp_path, monkeypatch)
        main(["grid", p.id, "--db", str(tmp_path / "grid.db")])
        assert "PAPER — not real money" in capsys.readouterr().out

    def test_it_draws_the_ladder_with_the_stop(self, tmp_path, monkeypatch,
                                               capsys):
        p = self._seed(tmp_path, monkeypatch)
        main(["grid", p.id, "--db", str(tmp_path / "grid.db")])
        out = capsys.readouterr().out
        assert "ladder" in out and "x " in out

    def test_it_shows_buy_and_hold(self, tmp_path, monkeypatch, capsys):
        p = self._seed(tmp_path, monkeypatch)
        main(["grid", p.id, "--db", str(tmp_path / "grid.db")])
        assert "buy & hold" in capsys.readouterr().out

    def test_an_unknown_grid_exits_nonzero(self, tmp_path, monkeypatch, capsys):
        self._seed(tmp_path, monkeypatch)
        assert main(["grid", "NOPE", "--db", str(tmp_path / "grid.db")]) == 2

    def test_it_parses(self):
        assert build_parser().parse_args(["grid", "abc"]).plan_id == "abc"


def test_short_window_warning_tracks_the_configured_statistics_window():
    """A warning that fires on every correct run is one nobody reads."""
    from grid import analyst as _an
    from grid import cli as _cli

    assert _cli.EXPECTED_BARS == int(_an.STATISTICS_DAYS * 24)
    # A full, correct statistics window must NOT warn.
    assert _cli.window_warning(int(_an.STATISTICS_DAYS * 24), "1H") == ""
    # A genuinely truncated one still must.
    assert "SHORT DATA WINDOW" in _cli.window_warning(300, "1H")


def test_backtest_replays_a_saved_candle_file_without_the_network(monkeypatch, capsys):
    from pathlib import Path

    def no_network(*a, **k):
        raise AssertionError("backtest --candles must not call OKX")

    monkeypatch.setattr(cli.okx, "get_candles", no_network)
    monkeypatch.setattr(cli.okx, "get_history", no_network)
    sample = Path(__file__).resolve().parents[1] / "examples" / "candles" / "ETH-USDT_1H.json"
    code = main(["backtest", "ETH-USDT", "--candles", str(sample)])
    out = capsys.readouterr().out
    assert "data window: 300 bars" in out
    assert "ETH-USDT:" in out
    assert code in (0, 1)
