"""Execution quality — the signal at small capital, where P&L is noise.

At 50 EUR a grid's profit and loss is a rounding error dressed up as evidence.
Two cycles either way swamps it, and a month of it tells you nothing about
whether the strategy works. What that month CAN tell you, and what nothing else
will, is whether the machinery works:

    fill rate       do resting orders actually fill, or sit at the back of a
                    queue while price passes through?
    slippage        does a fill arrive at the level price, or worse?
    fee accuracy    does `FeeModel` predict what OKX actually charges?
    uptime          is the executor up, and how often does it reconnect?

Every one of those is measurable in a handful of trades, none of them depends
on being right about the market, and all four are prerequisites for believing
any P&L figure that comes later. A strategy with a 40% fill rate is not a
strategy that made 2%, it is a strategy that did not run.

So this is displayed ABOVE profit and loss, and profit and loss at this size is
labelled as noise.

THE PAPER CAVEAT
================

`PaperExchange` fills at the level price by construction, so measured slippage
against it is exactly zero and measured fee accuracy is exactly perfect. Those
numbers are not evidence of good execution — they are evidence that the paper
exchange is doing what it says. The `is_paper` flag is recorded on every sample
and the summary refuses to claim otherwise.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

EXECUTION_PATH = (Path(os.getenv("LEDGER_HOME", ".")).resolve()
                  / "data" / "execution.json")

#: Fills required before calibration mode will hand over to live Kelly sizing.
#: Not a round number for its own sake: below about this many, a fill rate is
#: a coin-flip estimate and a slippage average is one bad print.
FILLS_REQUIRED = 50


@dataclass
class Fill:
    """One order that filled, and how well."""

    symbol: str
    side: str
    intended_price: float
    actual_price: float
    size: float
    fee_paid: float
    fee_predicted: float
    is_paper: bool = True
    at: str = field(default_factory=lambda: datetime.now(timezone.utc)
                    .isoformat(timespec="seconds"))

    @property
    def slippage(self) -> float:
        """Signed fraction, positive when the fill was WORSE than intended."""
        if self.intended_price <= 0:
            return 0.0
        raw = (self.actual_price - self.intended_price) / self.intended_price
        return raw if self.side == "buy" else -raw

    @property
    def fee_error(self) -> float:
        if self.fee_predicted <= 0:
            return 0.0
        return (self.fee_paid - self.fee_predicted) / self.fee_predicted


@dataclass
class ExecutionQuality:
    """Accumulated evidence about whether the machinery works."""

    orders_placed: int = 0
    orders_cancelled: int = 0
    fills: list[Fill] = field(default_factory=list)
    passes: int = 0
    passes_failed: int = 0
    reconnects: int = 0
    first_seen: str = ""
    last_seen: str = ""
    #: How many fills of each symbol have already been sampled, so a restart
    #: does not re-count the same ones.
    seen_fills: dict = field(default_factory=dict)

    # ------------------------------------------------------------- measures

    @property
    def n_fills(self) -> int:
        return len(self.fills)

    @property
    def fill_rate(self) -> Optional[float]:
        """Fills per order placed. None until an order has been placed."""
        if self.orders_placed <= 0:
            return None
        return self.n_fills / self.orders_placed

    @property
    def mean_slippage(self) -> Optional[float]:
        if not self.fills:
            return None
        return sum(f.slippage for f in self.fills) / len(self.fills)

    @property
    def worst_slippage(self) -> Optional[float]:
        if not self.fills:
            return None
        return max(f.slippage for f in self.fills)

    @property
    def mean_fee_error(self) -> Optional[float]:
        priced = [f for f in self.fills if f.fee_predicted > 0]
        if not priced:
            return None
        return sum(f.fee_error for f in priced) / len(priced)

    @property
    def uptime(self) -> Optional[float]:
        if self.passes <= 0:
            return None
        return 1.0 - (self.passes_failed / self.passes)

    @property
    def all_paper(self) -> bool:
        return all(f.is_paper for f in self.fills) if self.fills else True

    @property
    def fills_remaining(self) -> int:
        return max(0, FILLS_REQUIRED - self.n_fills)

    @property
    def ready_for_kelly(self) -> bool:
        """Enough fills to trust the execution numbers at all."""
        return self.n_fills >= FILLS_REQUIRED

    # -------------------------------------------------------------- record

    def record_placed(self, n: int = 1) -> None:
        self.orders_placed += n
        self._touch()

    def record_cancelled(self, n: int = 1) -> None:
        self.orders_cancelled += n
        self._touch()

    def record_fill(self, fill: Fill) -> None:
        self.fills.append(fill)
        self._touch()

    def record_pass(self, ok: bool = True) -> None:
        self.passes += 1
        if not ok:
            self.passes_failed += 1
        self._touch()

    def record_reconnect(self) -> None:
        self.reconnects += 1
        self._touch()

    def _touch(self) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.first_seen = self.first_seen or now
        self.last_seen = now

    # ------------------------------------------------------------ reporting

    def readiness(self) -> str:
        if self.ready_for_kelly:
            return (f"{self.n_fills} fills recorded — enough to switch from "
                    f"calibration to Kelly sizing, if the numbers above look "
                    f"sound to you.")
        return (f"{self.n_fills} of {FILLS_REQUIRED} fills recorded. "
                f"{self.fills_remaining} more before calibration mode will "
                f"hand over to Kelly sizing — below that, a fill rate is a "
                f"coin flip and a slippage average is one bad print.")

    def explain(self) -> str:
        def pct(x, dp=2):
            return "—" if x is None else f"{x * 100:.{dp}f}%"
        lines = [
            "EXECUTION QUALITY (the signal at this size — P&L is not)",
            f"  fills               {self.n_fills} of {self.orders_placed} "
            f"orders placed",
            f"  fill rate           {pct(self.fill_rate, 1)}",
            f"  mean slippage       {pct(self.mean_slippage, 3)}   "
            f"(positive is worse than the level price)",
            f"  worst slippage      {pct(self.worst_slippage, 3)}",
            f"  fee vs FeeModel     {pct(self.mean_fee_error, 2)} error",
            f"  executor uptime     {pct(self.uptime, 1)} over {self.passes} passes",
            f"  reconnects          {self.reconnects}",
        ]
        if self.all_paper and self.fills:
            lines.append("  NOTE: every fill is from PaperExchange, which fills "
                         "at the level price by construction — zero slippage "
                         "here is the simulator working, not the market.")
        lines.append("")
        lines.append(self.readiness())
        return "\n".join(lines)

    # ---------------------------------------------------------- persistence

    def save(self, path: Path = EXECUTION_PATH) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(self)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path


def load(path: Path = EXECUTION_PATH) -> ExecutionQuality:
    """The accumulated record, or an empty one. Never raises."""
    path = Path(path)
    if not path.exists():
        return ExecutionQuality()
    try:
        raw = json.loads(path.read_text())
        fills = [Fill(**f) for f in raw.pop("fills", [])]
        return ExecutionQuality(fills=fills, **raw)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return ExecutionQuality()
