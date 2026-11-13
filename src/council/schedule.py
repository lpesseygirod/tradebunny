"""The daily run, scheduled with macOS launchd.

`council schedule install` writes ~/Library/LaunchAgents/com.tradebunny.council.plist
and loads it: every evening at 22:15 (after the US close) it runs
`council daily`. That buys tranches you already approved, records the day's
value, and lets the council meet on its agenda. Every new trade it proposes
still waits for your OK. If the Mac is asleep at 22:15, launchd runs the job
when it wakes. Logs go to data/council/logs/daily.log.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

from . import paths

BASE_LABEL = "com.tradebunny.council"


def label() -> str:
    b = paths.book()
    return BASE_LABEL if b == "main" else f"{BASE_LABEL}.{b}"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label()}.plist"


def build(hour: int = 22, minute: int = 15) -> dict:
    root = paths.REPO
    logs = paths.data_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return {
        "Label": label(),
        "ProgramArguments": [sys.executable, "-m", "council.cli", "--book", paths.book(), "daily"],
        "WorkingDirectory": str(root),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "EnvironmentVariables": {"PYTHONPATH": str(root / "src"),
                                 "TRADEBUNNY_HOME": str(paths.home()),
                                 "PYTHONUNBUFFERED": "1",
                                 "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        "StandardOutPath": str(logs / "daily.log"),
        "StandardErrorPath": str(logs / "daily.log"),
        "ProcessType": "Background",
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install(hour: int = 22, minute: int = 15) -> Path:
    if paths.book() != "main":                # after the main book, one at a time
        minute = min(59, minute + {"autopilot": 20, "rules": 40}[paths.book()])
    p = plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    _launchctl("unload", str(p))                   # replace an older copy cleanly
    p.write_bytes(plistlib.dumps(build(hour, minute)))
    r = _launchctl("load", "-w", str(p))
    if r.returncode != 0:
        raise RuntimeError(f"launchctl load failed: {r.stderr.strip()}")
    return p


def remove() -> bool:
    p = plist_path()
    if not p.exists():
        return False
    _launchctl("unload", str(p))
    p.unlink()
    return True


def status() -> str:
    r = _launchctl("list", label())
    if r.returncode != 0:
        return "not scheduled"
    return "scheduled daily at " + "{Hour:02d}:{Minute:02d}".format(
        **plistlib.loads(plist_path().read_bytes())["StartCalendarInterval"])
