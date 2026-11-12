"""The validation gate. Every candidate the AI produces passes through here.

This module is where the discipline lives. It is intentionally strict: it is much
better to record two good predictions a week than ten sloppy ones, because the
sloppy ones poison the scorecard and you learn nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)

    def reject(self, reason: str) -> "Verdict":
        self.ok = False
        self.reasons.append(reason)
        return self


# source_published_at is REQUIRED. Leaving it optional gave the model an opt-out
# from the point-in-time rule: omit the field and any story, however stale, passes
# the freshness gate.
REQUIRED = ("ticker", "direction", "magnitude_pct", "benchmark",
            "horizon_days", "confidence", "thesis", "falsifier",
            "source_published_at")

# Hedging language that signals the model is not actually committing to anything.
WEASEL = re.compile(
    r"\b(may|might|could|possibly|potentially|perhaps|is expected to|"
    r"some analysts|it is believed|likely to be)\b", re.I)


def validate(
    cand: dict[str, Any],
    *,
    universe: set[str],
    benchmarks: set[str],
    open_tickers: set[str] | None = None,
    now: Optional[datetime] = None,
    min_confidence: float = 0.5,
    max_confidence: float = 0.9,
    min_magnitude: float = 0.5,
    max_magnitude: float = 15.0,
    max_source_age_hours: int = 72,
) -> Verdict:
    """Check one AI-generated candidate against every rule in docs/LEDGER.md."""
    now = now or datetime.now(timezone.utc)
    v = Verdict(ok=True)

    # 1. Falsifiability — every field present, or it is an opinion, not a prediction.
    for f in REQUIRED:
        if cand.get(f) in (None, "", []):
            v.reject(f"missing required field: {f}")
    if not v.ok:
        return v

    ticker = str(cand["ticker"]).upper().strip()

    # 2. Fixed universe — no cherry-picking illiquid names that moved on no volume.
    if ticker not in universe:
        v.reject(f"{ticker} is outside the configured universe")
    if str(cand["benchmark"]).upper() not in benchmarks:
        v.reject(f"benchmark {cand['benchmark']} is not a configured benchmark")
    if ticker == str(cand["benchmark"]).upper():
        v.reject("a ticker cannot be its own benchmark")

    # 3. No first-order predictions — the whole edge is in the second-order link.
    subjects = {str(s).upper().strip() for s in cand.get("source_subjects", [])}
    if ticker in subjects:
        v.reject(f"first-order: {ticker} is the subject of the source article")

    # 4. The chain must actually be a chain, not a restatement of the headline.
    # The isinstance check matters: a plain string "a -> b -> c" has len 12 and
    # sailed through a bare `len(chain) < 3`, then got stored raw.
    chain = cand.get("chain") or []
    if not isinstance(chain, (list, tuple)):
        v.reject(f"chain must be a list of links, got {type(chain).__name__}")
    elif len(chain) < 3:
        v.reject("causal chain needs at least 3 links (source → mechanism → target)")

    # 5. Ranges — a 45% confidence is noise, a 97% confidence is a lie.
    conf = float(cand["confidence"])
    if not min_confidence <= conf <= max_confidence:
        v.reject(f"confidence {conf:.2f} outside [{min_confidence}, {max_confidence}]")

    mag = abs(float(cand["magnitude_pct"]))
    if not min_magnitude <= mag <= max_magnitude:
        v.reject(f"magnitude {mag:.1f}% outside [{min_magnitude}%, {max_magnitude}%]")

    if str(cand["direction"]) not in ("long", "short"):
        v.reject(f"direction must be long or short, got {cand['direction']!r}")

    horizon = int(cand["horizon_days"])
    if not 3 <= horizon <= 60:
        v.reject(f"horizon {horizon} outside [3, 60] trading days")

    # 6. Point-in-time — stale news has already been priced.
    pub = cand.get("source_published_at")
    if pub:
        try:
            published = datetime.fromisoformat(str(pub).replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            age = (now - published).total_seconds() / 3600.0
            if age > max_source_age_hours:
                v.reject(f"source is {age:.0f}h old (limit {max_source_age_hours}h)")
            if age < -1:
                v.reject("source publish time is in the future — clock or parsing bug")
        except ValueError:
            v.reject(f"unparseable source_published_at: {pub!r}")

    # 7. The thesis must commit. Hedged prose cannot be scored.
    thesis = str(cand["thesis"])
    if len(thesis) < 80:
        v.reject("thesis too short to contain an actual argument")
    if WEASEL.search(thesis):
        v.reject("thesis hedges — state the mechanism as a claim that can be wrong")

    # 8. A falsifier is what separates a prediction from a horoscope.
    if len(str(cand["falsifier"])) < 40:
        v.reject("falsifier must concretely describe what would disprove the thesis")

    # 9. No doubling up — one open prediction per ticker keeps the sample independent.
    if open_tickers and ticker in open_tickers:
        v.reject(f"{ticker} already has an open prediction")

    return v


def lock_time(now: datetime, hour_utc: int = 20) -> datetime:
    """When the decision window closes: the US close on the day the signal fires."""
    lock = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    return lock + timedelta(days=1) if lock <= now else lock


def resolves_on(now: datetime, horizon_days: int) -> str:
    """Approximate calendar date for a horizon given in trading days.

    Deliberately crude — five trading days is about seven calendar days. The
    resolver checks that real price bars exist before scoring, so a date landing
    on a holiday costs nothing.
    """
    return (now + timedelta(days=round(horizon_days * 7 / 5))).date().isoformat()
