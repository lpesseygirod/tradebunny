"""Tests for the launchd agent and its log rotation."""

import plistlib
import subprocess

import shutil

import pytest

from grid import service
from grid.service import (KEEP_LOGS, LABEL, MAX_LOG_BYTES, ServiceStatus,
                          build_plist, render, rotate_logs)


class TestThePlist:

    def test_it_is_valid_plist_xml(self):
        assert plistlib.loads(render())["Label"] == LABEL

    @pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil is a macOS tool")
    def test_it_lints(self, tmp_path):
        """plutil is the arbiter, not my own parser."""
        p = tmp_path / "a.plist"
        p.write_bytes(render())
        r = subprocess.run(["plutil", "-lint", str(p)], capture_output=True,
                           text=True)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_run_at_load_is_true(self):
        """Starts at LOGIN — it is an agent, not a daemon."""
        assert build_plist()["RunAtLoad"] is True

    def test_keep_alive_only_restarts_a_crash(self):
        """SuccessfulExit false: restart on non-zero, respect a clean stop."""
        assert build_plist()["KeepAlive"] == {"SuccessfulExit": False}

    def test_keep_alive_is_not_plain_true(self):
        """`KeepAlive = true` would fight a deliberate stop."""
        assert build_plist()["KeepAlive"] is not True

    def test_it_runs_the_supervisor_not_the_bare_loop(self):
        args = build_plist()["ProgramArguments"]
        assert "supervise" in args and "run" not in args

    def test_it_uses_the_venv_interpreter(self):
        """launchd has no shell and no PATH to find dependencies with."""
        assert build_plist()["ProgramArguments"][0].endswith("/python")

    def test_program_arguments_is_a_list_not_a_shell_string(self):
        """The repo path contains spaces; a shell string would split on them."""
        assert isinstance(build_plist()["ProgramArguments"], list)

    def test_logs_go_to_data_logs(self):
        p = build_plist()
        assert p["StandardOutPath"].endswith("data/logs/executor.out.log")
        assert p["StandardErrorPath"].endswith("data/logs/executor.err.log")

    def test_stdout_and_stderr_are_separate(self):
        p = build_plist()
        assert p["StandardOutPath"] != p["StandardErrorPath"]

    def test_pythonpath_is_set(self):
        assert build_plist()["EnvironmentVariables"]["PYTHONPATH"].endswith("/src")

    def test_output_is_unbuffered(self):
        """Otherwise a crash loses the last thing it said."""
        assert build_plist()["EnvironmentVariables"]["PYTHONUNBUFFERED"] == "1"

    def test_a_crash_loop_is_throttled(self):
        assert build_plist()["ThrottleInterval"] >= 10

    def test_the_interval_is_passed_through(self):
        assert "30" in build_plist(interval=30.0)["ProgramArguments"]


class TestInstallAndUninstall:

    def test_install_writes_the_file(self, tmp_path):
        p = service.install(path=tmp_path / "x.plist", home=tmp_path)
        assert p.exists() and plistlib.loads(p.read_bytes())["Label"] == LABEL

    def test_install_creates_the_log_directory(self, tmp_path):
        service.install(path=tmp_path / "x.plist", home=tmp_path)
        assert (tmp_path / "data" / "logs").is_dir()

    def test_install_does_not_load_it(self, tmp_path):
        """Loading starts the executor, which starts placing orders. That is a
        decision, not a side effect of writing a file."""
        import inspect
        src = inspect.getsource(service.install)
        assert "launchctl" not in src

    def test_uninstall_removes_it(self, tmp_path):
        p = service.install(path=tmp_path / "x.plist", home=tmp_path)
        assert service.uninstall(path=p) is True
        assert not p.exists()

    def test_uninstalling_nothing_is_harmless(self, tmp_path):
        assert service.uninstall(path=tmp_path / "absent.plist") is False


class TestStatus:

    def test_not_installed_is_reported(self, tmp_path):
        st = service.status(label="com.tradebunny.nope",
                            path=tmp_path / "absent.plist")
        assert not st.installed and not st.running
        assert "install" in st.explain()

    def test_installed_but_unloaded_says_how_to_load(self, tmp_path):
        p = service.install(path=tmp_path / "x.plist", home=tmp_path)
        st = service.status(label="com.tradebunny.definitely-not-loaded", path=p)
        assert st.installed and not st.loaded
        assert "launchctl load" in st.explain()

    def test_a_running_job_is_reported(self):
        st = ServiceStatus(label=LABEL, installed=True, loaded=True, pid=4242,
                           plist="/tmp/x.plist")
        assert st.running and "pid 4242" in st.explain()

    def test_loaded_without_a_pid_is_not_running(self):
        st = ServiceStatus(label=LABEL, installed=True, loaded=True, pid=None,
                           plist="/tmp/x.plist")
        assert not st.running and "NOT RUNNING" in st.explain()

    def test_a_clean_last_exit_is_explained(self):
        st = ServiceStatus(label=LABEL, installed=True, loaded=True, pid=0,
                           last_exit=0, plist="/tmp/x.plist")
        assert "will not restart" in st.explain()

    def test_a_crash_exit_is_explained(self):
        st = ServiceStatus(label=LABEL, installed=True, loaded=True, pid=0,
                           last_exit=1, plist="/tmp/x.plist")
        assert "KeepAlive restarts this" in st.explain()

    def test_it_asks_launchd_and_survives_a_missing_job(self):
        loaded, fields, err = service._launchctl_list("com.tradebunny.nonexistent")
        assert loaded is False and fields == {}


class TestLogRotation:
    """launchd holds these files open, so renaming is not rotation."""

    def big(self, path, n=None):
        path.write_text("x" * (n or MAX_LOG_BYTES + 10))
        return path

    def test_a_small_log_is_left_alone(self, tmp_path):
        log = tmp_path / "executor.out.log"
        log.write_text("short")
        assert rotate_logs(tmp_path) == []
        assert log.read_text() == "short"

    def test_an_oversized_log_is_rotated(self, tmp_path):
        log = self.big(tmp_path / "executor.out.log")
        assert rotate_logs(tmp_path) == [log]
        assert log.stat().st_size == 0
        assert (tmp_path / "executor.out.log.1").stat().st_size > 0

    def test_the_inode_is_preserved(self, tmp_path):
        """The key case. Renaming would leave launchd writing to the old inode and
        the live log permanently empty."""
        log = self.big(tmp_path / "executor.out.log")
        before = log.stat().st_ino
        rotate_logs(tmp_path)
        assert log.stat().st_ino == before

    def test_an_open_handle_keeps_working_across_a_rotation(self, tmp_path):
        """Simulates launchd: hold the file open, rotate, keep writing."""
        log = self.big(tmp_path / "executor.out.log")
        with open(log, "a") as fh:
            rotate_logs(tmp_path)
            fh.write("after rotation\n")
            fh.flush()
        assert "after rotation" in log.read_text()

    def test_history_is_kept_not_discarded(self, tmp_path):
        log = tmp_path / "executor.out.log"
        log.write_text("important" + "x" * MAX_LOG_BYTES)
        rotate_logs(tmp_path)
        assert "important" in (tmp_path / "executor.out.log.1").read_text()

    def test_old_generations_shift_along(self, tmp_path):
        log = tmp_path / "executor.out.log"
        (tmp_path / "executor.out.log.1").write_text("older")
        self.big(log)
        rotate_logs(tmp_path)
        assert (tmp_path / "executor.out.log.2").read_text() == "older"

    def test_it_keeps_a_bounded_number(self, tmp_path):
        log = tmp_path / "executor.out.log"
        for _ in range(KEEP_LOGS + 3):
            self.big(log)
            rotate_logs(tmp_path)
        assert len(list(tmp_path.glob("executor.out.log.*"))) <= KEEP_LOGS

    def test_both_streams_rotate(self, tmp_path):
        self.big(tmp_path / "executor.out.log")
        self.big(tmp_path / "executor.err.log")
        assert len(rotate_logs(tmp_path)) == 2

    def test_a_missing_directory_is_harmless(self, tmp_path):
        assert rotate_logs(tmp_path / "absent") == []

    def test_an_unrotatable_file_does_not_stop_the_others(self, tmp_path):
        good = self.big(tmp_path / "executor.out.log")
        bad = self.big(tmp_path / "executor.err.log")
        bad.chmod(0o444)
        try:
            rotate_logs(tmp_path)
        finally:
            bad.chmod(0o644)
        assert good.stat().st_size == 0
