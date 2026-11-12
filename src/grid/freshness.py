"""When was each input last true?

Every number this system acts on came from somewhere at some moment, and a
proposal built from yesterday's candles looks exactly like one built from this
minute's. That is the failure this module exists to make impossible: not wrong
data, which is loud, but STALE data, which is silent and produces a
confident-looking answer to a question about a market that has moved on.

THE RULE: STAMP AT FETCH, NEVER INFER
=====================================

A timestamp is recorded by whoever performed the fetch, at the moment of the
fetch, and stored alongside the data. It is never reconstructed afterwards from
a file's mtime. An mtime says when bytes were last written, which is a different
question with the same units: a cache rewritten unchanged, a file copied, a
directory restored from backup, all move the mtime without making the data any
newer. Inferring freshness from mtime is how a six-month-old candle series comes
to look like it arrived this morning.

TOLERANCES
==========

Each source has a different half-life, so each gets its own tolerance:

    candles       1 hour    the market moves; this is the hard gate
    tickers       1 hour    liquidity changes intraday
    fee tier      1 day     moves with 30-day volume; 7 days is the hard limit
    instruments   1 day     minSz changes only on a relisting

`candles` is the only one that BLOCKS a proposal outright, because it is the
only one whose staleness silently changes the answer rather than the framing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

#: Seconds after which each source is considered stale.
TOLERANCES: dict[str, float] = {
    "candles": 3_600.0,
    "tickers": 3_600.0,
    "fees": 86_400.0,
    "instruments": 86_400.0,
}

#: Beyond this a cached fee tier is not merely stale but unusable, and the
#: PESSIMISTIC fallback is used instead. See analyst.live_fees.
FEE_HARD_LIMIT_SECONDS = 7 * 86_400.0

#: The hard gate. Candles older than this cannot produce a proposal.
DEFAULT_MAX_DATA_AGE = 3_600.0


def now_ts() -> float:
    return time.time()


def iso(ts: Optional[float]) -> str:
    if ts is None:
        return "never"
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def human_duration(seconds: float) -> str:
    """A length of time, with no implied direction."""
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def human_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "never fetched"
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h ago"
    return f"{seconds / 86400:.1f}d ago"


@dataclass(frozen=True)
class Stamp:
    """One source, when it was fetched, and whether that is still good enough."""

    source: str
    fetched_at: Optional[float]
    tolerance: float
    detail: str = ""

    def age(self, now: Optional[float] = None) -> Optional[float]:
        if self.fetched_at is None:
            return None
        return max(0.0, (now if now is not None else now_ts()) - self.fetched_at)

    def is_fresh(self, now: Optional[float] = None) -> bool:
        a = self.age(now)
        return a is not None and a <= self.tolerance

    def describe(self, now: Optional[float] = None) -> str:
        a = self.age(now)
        mark = "ok " if self.is_fresh(now) else "STALE"
        return (f"{mark} {self.source:14} {human_age(a):>14}  "
                f"(tolerance {human_duration(self.tolerance)})"
                + (f"  {self.detail}" if self.detail else ""))


@dataclass
class FreshnessReport:
    """Every input, its age, and whether it is within tolerance.

    The point is to answer "is this current?" by looking rather than by
    reasoning. Anything not listed here is an input nobody is checking.
    """

    stamps: list[Stamp] = field(default_factory=list)

    def add(self, source: str, fetched_at: Optional[float],
            tolerance: Optional[float] = None, detail: str = "") -> Stamp:
        s = Stamp(source=source, fetched_at=fetched_at,
                  tolerance=(tolerance if tolerance is not None
                             else TOLERANCES.get(source, 3_600.0)),
                  detail=detail)
        self.stamps.append(s)
        return s

    def get(self, source: str) -> Optional[Stamp]:
        for s in self.stamps:
            if s.source == source:
                return s
        return None

    def all_fresh(self, now: Optional[float] = None) -> bool:
        return all(s.is_fresh(now) for s in self.stamps)

    def stale(self, now: Optional[float] = None) -> list[Stamp]:
        return [s for s in self.stamps if not s.is_fresh(now)]

    def explain(self, now: Optional[float] = None) -> str:
        if not self.stamps:
            return "no data sources recorded — nothing is being checked"
        lines = [s.describe(now) for s in self.stamps]
        bad = self.stale(now)
        lines.append("")
        lines.append("ALL FRESH" if not bad else
                     f"{len(bad)} STALE: " + ", ".join(s.source for s in bad))
        return "\n".join(lines)


def candle_age(candles: list[dict], now: Optional[float] = None,
               bar_ms: float = 3_600_000.0) -> Optional[float]:
    """How old the newest CANDLE is, in seconds.

    Deliberately measured from the bar's own timestamp rather than from when it
    was fetched. A fetch performed a second ago that returned a bar from
    yesterday is stale data freshly delivered, and it is the data's age that
    decides whether the answer means anything.

    One bar is added because `ts` marks the candle's OPEN: a 1H candle stamped
    09:00 covers 09:00-10:00 and is only complete at 10:00.
    """
    if not candles:
        return None
    newest = max(c.get("ts", 0.0) for c in candles)
    if newest <= 0:
        return None
    close_time = (newest + bar_ms) / 1000.0
    return max(0.0, (now if now is not None else now_ts()) - close_time)
