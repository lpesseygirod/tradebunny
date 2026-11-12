"""The launchd agent that keeps the executor alive.

WHY A LaunchAgent AND NOT A LaunchDaemon
========================================

A LaunchDaemon runs at boot, as root, before anyone logs in. That is the wrong
shape for this program twice over:

  1. **No keychain.** API credentials live in the login keychain, which is
     locked until you log in. A daemon starting at boot would find no keys,
     fall back to whatever it could read, and run in a degraded state that
     looks identical to a healthy one.

  2. **Nobody is watching.** `RunAtLoad` on an agent means "when Lea logs in".
     That is deliberate: the executor places real orders and enforces the only
     stop-loss this system has, and it should not begin doing that while you
     are asleep and unable to notice it misbehaving. Trading starts when you
     are present to see it start.

KeepAlive
=========

    KeepAlive = { SuccessfulExit = false }

meaning: restart if it exits NON-zero, leave it alone if it exits zero. The
supervisor already treats a clean exit as "you stopped it on purpose" and does
not restart; this makes launchd agree. Without the `SuccessfulExit` key,
`KeepAlive = true` would restart the process every time you deliberately
stopped it, and you would end up fighting your own machine.

LOG ROTATION
============

launchd holds `StandardOutPath` open for the life of the job and does NOT
rotate it. Renaming the file therefore does nothing useful: launchd keeps
writing to the same inode under its new name, and the "current" log stays
empty forever.

So rotation here is copy-then-truncate: the content is copied to `.1`, and the
original is truncated **in place**, preserving the inode and launchd's file
handle. It is driven from the executor loop rather than from here, because the
loop ticks every 60 seconds and this module only runs when you invoke it.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

LABEL = "com.tradebunny.executor"

#: Rotate once a log passes this, keeping this many old files.
MAX_LOG_BYTES = 8 * 1024 * 1024
KEEP_LOGS = 5


def agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_path(label: str = LABEL) -> Path:
    return agents_dir() / f"{label}.plist"


def repo_root() -> Path:
    """The project directory — three levels up from this file."""
    return Path(__file__).resolve().parents[2]


def log_dir(home: Optional[Path] = None) -> Path:
    return Path(home or os.getenv("TRADEBUNNY_HOME") or repo_root()) / "data" / "logs"


def python_executable() -> str:
    """The venv interpreter if there is one, else whatever is running us.

    launchd starts with a minimal environment and no shell, so `python` alone
    would find the system interpreter and none of the dependencies.
    """
    venv = repo_root() / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def build_plist(label: str = LABEL, home: Optional[Path] = None,
                interval: float = 60.0) -> dict:
    """The agent definition, as a dict ready for `plistlib`."""
    root = repo_root()
    logs = log_dir(home)
    return {
        "Label": label,
        "ProgramArguments": [
            python_executable(), "-m", "grid.cli", "supervise",
            "--interval", str(int(interval)),
        ],
        "WorkingDirectory": str(root),
        # At LOGIN, not at boot. See the module docstring.
        "RunAtLoad": True,
        # Restart on a crash; respect a deliberate clean stop.
        "KeepAlive": {"SuccessfulExit": False},
        # A crash loop should not spin the CPU.
        "ThrottleInterval": 30,
        "StandardOutPath": str(logs / "executor.out.log"),
        "StandardErrorPath": str(logs / "executor.err.log"),
        "EnvironmentVariables": {
            "PYTHONPATH": str(root / "src"),
            "PYTHONUNBUFFERED": "1",
            "TRADEBUNNY_HOME": str(Path(home) if home else root),
            "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "ProcessType": "Background",
    }


def render(label: str = LABEL, home: Optional[Path] = None,
           interval: float = 60.0) -> bytes:
    return plistlib.dumps(build_plist(label, home, interval))


def install(label: str = LABEL, home: Optional[Path] = None,
            interval: float = 60.0, path: Optional[Path] = None) -> Path:
    """Write the plist. Does NOT load it.

    Loading starts the executor, which starts placing orders. That is a
    decision, not a side effect of writing a file, so it is left as an explicit
    second command.
    """
    target = Path(path) if path else plist_path(label)
    target.parent.mkdir(parents=True, exist_ok=True)
    log_dir(home).mkdir(parents=True, exist_ok=True)
    target.write_bytes(render(label, home, interval))
    return target


def uninstall(label: str = LABEL, path: Optional[Path] = None) -> bool:
    target = Path(path) if path else plist_path(label)
    if not target.exists():
        return False
    target.unlink()
    return True


# ------------------------------------------------------------------- status

@dataclass(frozen=True)
class ServiceStatus:
    """What launchd thinks of the agent."""

    label: str
    installed: bool
    loaded: bool
    pid: Optional[int] = None
    last_exit: Optional[int] = None
    plist: str = ""
    error: str = ""

    @property
    def running(self) -> bool:
        return self.loaded and self.pid is not None and self.pid > 0

    def explain(self) -> str:
        lines = [f"launchd agent {self.label}"]
        lines.append(f"  plist          {self.plist or '(not written)'}")
        lines.append(f"  installed      {'yes' if self.installed else 'no'}")
        if not self.installed:
            lines.append("  -> `grid service install` writes it")
            return "\n".join(lines)
        lines.append(f"  loaded         {'yes' if self.loaded else 'no'}")
        if not self.loaded:
            lines.append(f"  -> launchctl load -w {self.plist}")
            return "\n".join(lines)
        if self.running:
            lines.append(f"  RUNNING        pid {self.pid}")
        else:
            lines.append("  loaded but NOT RUNNING — launchd is holding the job "
                         "but no process is up")
        if self.last_exit is not None:
            lines.append(f"  last exit      {self.last_exit}"
                         + ("  (clean — KeepAlive will not restart it)"
                            if self.last_exit == 0 else
                            "  (non-zero — KeepAlive restarts this)"))
        if self.error:
            lines.append(f"  note           {self.error}")
        return "\n".join(lines)


def _launchctl_list(label: str) -> tuple[bool, dict[str, str], str]:
    """Ask launchd about one job. Returns (loaded, fields, error)."""
    try:
        p = subprocess.run(["launchctl", "list", label], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return False, {}, f"{type(e).__name__}: {e}"
    if p.returncode != 0:
        return False, {}, ""
    fields: dict[str, str] = {}
    for line in p.stdout.splitlines():
        line = line.strip().rstrip(";")
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        fields[k.strip().strip('"')] = v.strip().strip('"')
    return True, fields, ""


def status(label: str = LABEL, path: Optional[Path] = None) -> ServiceStatus:
    """Whether launchd has the agent, and whether it is actually running."""
    target = Path(path) if path else plist_path(label)
    loaded, fields, err = _launchctl_list(label)

    def as_int(key: str) -> Optional[int]:
        raw = fields.get(key)
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    return ServiceStatus(
        label=label, installed=target.exists(), loaded=loaded,
        pid=as_int("PID"), last_exit=as_int("LastExitStatus"),
        plist=str(target), error=err)


# ---------------------------------------------------------------- rotation

def rotate_logs(directory: Optional[Path] = None,
                max_bytes: int = MAX_LOG_BYTES, keep: int = KEEP_LOGS) -> list[Path]:
    """Copy-then-truncate any oversized log. Returns the files rotated.

    COPY AND TRUNCATE, NOT RENAME. launchd holds these files open for the life
    of the job; renaming one leaves launchd writing to the renamed inode and
    the live log permanently empty. Truncating in place keeps the inode, so the
    handle stays valid and new output lands where it should.
    """
    directory = Path(directory or log_dir())
    if not directory.exists():
        return []
    rotated = []
    for log in sorted(directory.glob("*.log")):
        try:
            if log.stat().st_size < max_bytes:
                continue
            for i in range(keep - 1, 0, -1):
                older, newer = log.with_suffix(f".log.{i}"), log.with_suffix(
                    f".log.{i + 1}")
                if older.exists():
                    older.replace(newer)
            shutil.copy2(log, log.with_suffix(".log.1"))
            with open(log, "r+") as fh:
                fh.truncate(0)
            rotated.append(log)
        except OSError:
            continue          # a log we cannot rotate must not stop the executor
    return rotated
