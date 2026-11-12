"""Kill switches — the three ways to make everything stop.

Design principle: **fail closed.** Every check here answers "am I allowed to trade?"
and returns False whenever it cannot be sure. A corrupt file, a missing directory,
a permissions error — all of them halt trading rather than permit it. The expensive
failure is a bot that keeps trading when you wanted it stopped; a bot that stops
when it didn't need to costs you nothing but a restart.

The state lives in a plain JSON file, deliberately. It must be changeable when the
Telegram bot is down, when the executor is wedged, and from a phone over SSH. You
can stop everything with a text editor, and that is the point.

Three levels:
    1. halt_symbol("BTC-USDT")  one instrument stops; everything else continues
    2. halt_analyst()           no new proposals; running grids keep running
    3. halt_all()               everything stops, orders cancelled
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Resolved against LEDGER_HOME so a systemd unit, a cron job or a container with a
# different working directory reads the SAME file the CLI wrote. A relative default
# here would silently give each process its own view of whether trading is halted.
DEFAULT_PATH = Path(os.getenv("LEDGER_HOME", ".")).resolve() / "data" / "control.json"


class _FileLock:
    """Portable exclusive lock, so a read-modify-write cannot lose a halt.

    Two processes (the CLI, the web UI, the executor) each doing
    state() -> mutate -> _write() can interleave: the second writes its stale
    snapshot and resurrects `trading_enabled=True`, erasing a halt someone just
    set. That is the worst possible failure for a kill switch, so every mutator
    holds this across the whole cycle.

    O_CREAT|O_EXCL rather than fcntl, because this has to work on Windows too.
    """

    def __init__(self, target: Path, timeout: float = 5.0):
        self.lock_path = target.with_suffix(target.suffix + ".lock")
        self.timeout = timeout
        self.fd: Optional[int] = None

    def __enter__(self) -> "_FileLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                # A lock older than the timeout is assumed stale (the holder died
                # mid-write); breaking it is safer than blocking a halt forever.
                try:
                    if time.time() - self.lock_path.stat().st_mtime > self.timeout:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError(f"could not acquire {self.lock_path}")
                time.sleep(0.02)

    def __exit__(self, *exc: Any) -> None:
        if self.fd is not None:
            os.close(self.fd)
        self.lock_path.unlink(missing_ok=True)


@dataclass
class ControlState:
    trading_enabled: bool = True
    analyst_enabled: bool = True
    halted_symbols: list[str] = field(default_factory=list)
    halted_plans: list[str] = field(default_factory=list)
    reason: str = ""
    updated_at: str = ""

    # Safety caps. The executor checks these before every order; they are the
    # backstop for a plan that slipped through validation with silly numbers.
    max_concurrent_grids: int = 3
    max_total_quote: float = 500.0
    live_trading: bool = False        # explicit opt-in; paper until you flip it


class Control:
    """Reads and writes the control file. Every read is fresh — no caching,
    because a cached 'enabled' is exactly the bug that ignores a stop."""

    def __init__(self, path: str | Path = DEFAULT_PATH):
        self.path = Path(path)

    # ----------------------------------------------------------------- read

    @staticmethod
    def _halted(reason: str) -> ControlState:
        return ControlState(trading_enabled=False, analyst_enabled=False,
                            live_trading=False, reason=reason)

    @staticmethod
    def _parse(raw: dict[str, Any]) -> ControlState:
        """Strictly typed load. Raises on anything unexpected, so the caller halts.

        json.loads gives us whatever is in the file, and Python truthiness is the
        trap: the STRING "false" is truthy, so a hand-edited
        {"trading_enabled": "false", "live_trading": "true"} would ignore a halt
        AND arm real money without the confirmation phrase. The file is meant to be
        editable by hand, which makes validating it mandatory rather than optional.
        """
        spec = {f.name: f.type for f in fields(ControlState)}
        unknown = set(raw) - set(spec)
        if unknown:
            raise ValueError(f"unknown field(s): {sorted(unknown)}")

        out: dict[str, Any] = {}
        for key, value in raw.items():
            hint = spec[key]
            if hint == "bool" or hint is bool:
                if not isinstance(value, bool):
                    raise TypeError(f"{key} must be a JSON boolean, got {value!r}")
            elif hint == "list[str]":
                if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                    raise TypeError(f"{key} must be a list of strings, got {value!r}")
            elif hint == "int" or hint is int:
                if not isinstance(value, int) or isinstance(value, bool):
                    raise TypeError(f"{key} must be an integer, got {value!r}")
            elif hint == "float" or hint is float:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise TypeError(f"{key} must be a number, got {value!r}")
                value = float(value)
            elif hint == "str" or hint is str:
                if not isinstance(value, str):
                    raise TypeError(f"{key} must be a string, got {value!r}")
            out[key] = value
        return ControlState(**out)

    def state(self) -> ControlState:
        """Current state. Any problem at all returns a fully-halted state.

        A MISSING file halts too. It used to return enabled defaults, which meant a
        process started from the wrong directory — or one whose control file had
        been moved or deleted — would cheerfully trade while you believed everything
        was stopped. Call initialise() once to create the file deliberately.
        """
        try:
            if not self.path.exists():
                return self._halted(
                    f"no control file at {self.path} — run `initialise` to create one")
            return self._parse(json.loads(self.path.read_text()))
        except Exception as e:
            return self._halted(f"control file unusable, halted for safety: {e}")

    def initialise(self, overwrite: bool = False) -> ControlState:
        """Create the control file with safe defaults: enabled, but paper only."""
        if self.path.exists() and not overwrite:
            return self.state()
        s = ControlState()
        with _FileLock(self.path):
            self._write(s)
        return s

    def may_trade(self, symbol: str, plan_id: str = "") -> tuple[bool, str]:
        """The question the executor asks before every single order."""
        s = self.state()
        if not s.trading_enabled:
            return False, s.reason or "trading halted globally"
        if symbol in s.halted_symbols:
            return False, f"{symbol} is halted"
        if plan_id and plan_id in s.halted_plans:
            return False, f"plan {plan_id} is halted"
        return True, ""

    def may_propose(self) -> tuple[bool, str]:
        """The question the daily analyst asks before it starts thinking."""
        s = self.state()
        if not s.analyst_enabled:
            return False, s.reason or "analyst halted"
        return True, ""

    def is_live(self) -> bool:
        """True only when real money is explicitly enabled. Defaults to paper."""
        s = self.state()
        return bool(s.live_trading and s.trading_enabled)

    # ---------------------------------------------------------------- write

    def _write(self, s: ControlState) -> None:
        """Atomic write — a half-written control file would fail closed and halt
        the bot, so we never leave one on disk."""
        s.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(asdict(s), f, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _mutate(self, apply) -> ControlState:
        """Read-modify-write under an exclusive lock.

        Without the lock, a `resume_symbol` holding a snapshot taken before a
        concurrent `halt_all` will write that stale snapshot back and silently
        undo the halt. Every mutator therefore goes through here.
        """
        with _FileLock(self.path):
            if self.path.exists():
                try:
                    s = self._parse(json.loads(self.path.read_text()))
                except Exception:
                    # Rebuild from safe defaults rather than propagating a corrupt
                    # state — but stay halted so nothing resumes by accident.
                    s = self._halted("previous control file was unusable and was reset")
            else:
                s = ControlState()
            apply(s)
            self._write(s)
            return s

    def halt_all(self, reason: str = "manual stop") -> ControlState:
        """Level 3. Everything stops. The executor cancels open orders on its
        next loop. Positions are NOT liquidated — stopping is not the same as
        selling, and force-selling into a panic is usually the worse mistake."""
        def f(s: ControlState) -> None:
            s.trading_enabled = False
            s.analyst_enabled = False
            s.reason = reason
        return self._mutate(f)

    def halt_symbol(self, symbol: str, reason: str = "") -> ControlState:
        """Level 1. One instrument stops; every other grid carries on."""
        def f(s: ControlState) -> None:
            if symbol not in s.halted_symbols:
                s.halted_symbols.append(symbol)
            if reason:
                s.reason = reason
        return self._mutate(f)

    def halt_plan(self, plan_id: str, reason: str = "") -> ControlState:
        def f(s: ControlState) -> None:
            if plan_id not in s.halted_plans:
                s.halted_plans.append(plan_id)
            if reason:
                s.reason = reason
        return self._mutate(f)

    def halt_analyst(self, reason: str = "") -> ControlState:
        """Level 2. No new proposals. Anything already running keeps running."""
        def f(s: ControlState) -> None:
            s.analyst_enabled = False
            if reason:
                s.reason = reason
        return self._mutate(f)

    # --------------------------------------------------------------- resume

    def resume_all(self) -> ControlState:
        """Clear every halt. Deliberately does NOT enable live trading — coming
        back from a stop should never silently re-arm real money."""
        def f(s: ControlState) -> None:
            s.trading_enabled = True
            s.analyst_enabled = True
            s.halted_symbols = []
            s.halted_plans = []
            s.reason = ""
        return self._mutate(f)

    def resume_symbol(self, symbol: str) -> ControlState:
        def f(s: ControlState) -> None:
            s.halted_symbols = [x for x in s.halted_symbols if x != symbol]
        return self._mutate(f)

    def set_live(self, live: bool, confirm: str = "") -> ControlState:
        """Arm or disarm real money. Turning it ON requires typing the phrase,
        so it cannot happen through a stray call or a mistyped CLI flag."""
        if live and confirm != "I ACCEPT THE RISK":
            raise ValueError(
                "enabling live trading requires confirm='I ACCEPT THE RISK'")
        def f(s: ControlState) -> None:
            s.live_trading = bool(live)
        return self._mutate(f)

    def set_caps(self, max_concurrent_grids: Optional[int] = None,
                 max_total_quote: Optional[float] = None) -> ControlState:
        def f(s: ControlState) -> None:
            if max_concurrent_grids is not None:
                s.max_concurrent_grids = int(max_concurrent_grids)
            if max_total_quote is not None:
                s.max_total_quote = float(max_total_quote)
        return self._mutate(f)

    def describe(self) -> str:
        s = self.state()
        mode = "LIVE — real money" if self.is_live() else "paper"
        return "\n".join([
            f"mode              {mode}",
            f"trading           {'on' if s.trading_enabled else 'HALTED'}",
            f"analyst           {'on' if s.analyst_enabled else 'HALTED'}",
            f"halted symbols    {', '.join(s.halted_symbols) or '—'}",
            f"halted plans      {', '.join(s.halted_plans) or '—'}",
            f"max grids         {s.max_concurrent_grids}",
            f"max total quote   {s.max_total_quote:g}",
            f"reason            {s.reason or '—'}",
            f"updated           {s.updated_at or 'never'}",
        ])
