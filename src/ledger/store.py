"""The ledger itself: an append-only SQLite store.

Immutability is enforced here rather than by convention, because the entire value
of the journal depends on entries not being editable after the outcome is known.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id                  TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    lock_at             TEXT NOT NULL,
    resolves_on         TEXT NOT NULL,
    engine              TEXT NOT NULL,
    ticker              TEXT NOT NULL,
    company             TEXT,
    direction           TEXT NOT NULL,
    magnitude_pct       REAL NOT NULL,
    benchmark           TEXT NOT NULL,
    horizon_days        INTEGER NOT NULL,
    confidence          REAL NOT NULL,
    thesis              TEXT NOT NULL,
    chain               TEXT NOT NULL,
    falsifier           TEXT NOT NULL,
    source_url          TEXT,
    source_title        TEXT,
    source_published_at TEXT,
    twin_ticker         TEXT,
    twin_direction      TEXT,
    content_hash        TEXT NOT NULL,
    decision            TEXT NOT NULL DEFAULT 'pending',
    decided_at          TEXT,
    size_eur            REAL,
    fill_price          REAL,
    status              TEXT NOT NULL DEFAULT 'open',
    entry_price         REAL,
    exit_price          REAL,
    bench_entry         REAL,
    bench_exit          REAL,
    net_excess_pct      REAL,
    hit                 INTEGER,
    twin_net_excess_pct REAL,
    twin_hit            INTEGER,
    resolved_at         TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id             TEXT PRIMARY KEY,
    ran_at         TEXT NOT NULL,
    kind           TEXT NOT NULL,
    n_candidates   INTEGER NOT NULL DEFAULT 0,
    n_predictions  INTEGER NOT NULL DEFAULT 0,
    n_resolved     INTEGER NOT NULL DEFAULT 0,
    rejections     TEXT,
    error          TEXT,
    notes          TEXT
);

CREATE INDEX IF NOT EXISTS idx_pred_status ON predictions(status);
CREATE INDEX IF NOT EXISTS idx_pred_resolves ON predictions(resolves_on);
"""

# Columns that may change after creation, and the phase that may change them.
_DECISION_COLS = {"decision", "decided_at", "size_eur", "fill_price"}
_OUTCOME_COLS = {
    "status", "entry_price", "exit_price", "bench_entry", "bench_exit",
    "net_excess_pct", "hit", "twin_net_excess_pct", "twin_hit", "resolved_at",
}
# Everything else is frozen and covered by content_hash.
_HASHED_COLS = [
    "id", "created_at", "lock_at", "resolves_on", "engine", "ticker", "direction",
    "magnitude_pct", "benchmark", "horizon_days", "confidence", "thesis",
    "chain", "falsifier", "source_url", "source_title", "company",
    "source_published_at",
    # The twin is the project's primary control. Left unhashed, the twin could be
    # swapped after the outcome for a ticker known to have lost, re-scored, and
    # `beats_twin` flipped to True with no integrity warning.
    "twin_ticker", "twin_direction",
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def content_hash(row: dict[str, Any]) -> str:
    """Hash the immutable fields so tampering is detectable after the fact."""
    payload = json.dumps({k: row.get(k) for k in _HASHED_COLS}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Ledger:
    def __init__(self, path: str | Path = "data/ledger.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- writing

    def record(self, pred: dict[str, Any]) -> str:
        """Write a validated prediction. Never call this with unvalidated input —
        run it through rules.validate() first."""
        row = dict(pred)
        row.setdefault("id", f"{datetime.now(timezone.utc):%Y-%m-%d}-{uuid.uuid4().hex[:6]}")
        row.setdefault("created_at", utcnow())
        # Normalise case at the point of storage. `validate` uppercases into a local
        # and never writes it back, so a lowercase "wy" was stored as-is; the next
        # run then compared "WY" against {"wy"}, found no match, and accepted a
        # second open prediction on the same ticker — breaking the independence
        # the scorecard assumes.
        for col in ("ticker", "benchmark", "twin_ticker"):
            if row.get(col):
                row[col] = str(row[col]).upper().strip()
        # An outcome may never be supplied at creation time.
        preset = {c for c in _OUTCOME_COLS if row.get(c) is not None} - {"status"}
        if preset or row.get("status") not in (None, "open"):
            raise ValueError(
                f"a prediction cannot be created already resolved: {sorted(preset)}")
        if isinstance(row.get("chain"), (list, tuple)):
            row["chain"] = json.dumps(list(row["chain"]))
        row["content_hash"] = content_hash(row)

        cols = [c for c in row if c in self._columns()]
        placeholders = ",".join("?" for _ in cols)
        self.conn.execute(
            f"INSERT INTO predictions ({','.join(cols)}) VALUES ({placeholders})",
            [row[c] for c in cols],
        )
        self.conn.commit()
        return row["id"]

    def decide(self, pred_id: str, decision: str, now: Optional[datetime] = None,
               size_eur: Optional[float] = None, fill_price: Optional[float] = None) -> bool:
        """Mark a prediction bought or skipped. Refuses after lock_at, and refuses
        to overwrite a decision that already exists. Returns True if it stuck."""
        if decision not in ("bought", "skipped"):
            raise ValueError("decision must be 'bought' or 'skipped'")
        now = now or datetime.now(timezone.utc)
        row = self.get(pred_id)
        if row is None:
            return False
        if row["decision"] != "pending":
            return False
        # Normalise to aware UTC. A naive stored timestamp raised TypeError here
        # while lock_expired compared the same column as a string and silently
        # succeeded — the two methods disagreed about the same row.
        lock_at = datetime.fromisoformat(row["lock_at"])
        if lock_at.tzinfo is None:
            lock_at = lock_at.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if now > lock_at:
            return False
        self.conn.execute(
            "UPDATE predictions SET decision=?, decided_at=?, size_eur=?, fill_price=? WHERE id=?",
            (decision, now.isoformat(timespec="seconds"), size_eur, fill_price, pred_id),
        )
        self.conn.commit()
        return True

    def lock_expired(self, now: Optional[datetime] = None) -> int:
        """Freeze undecided predictions past their lock time as 'skipped'.

        Run this every evening. Without it, a prediction could be marked bought
        after its outcome is visible, which would quietly corrupt the record.
        """
        now = now or datetime.now(timezone.utc)
        cur = self.conn.execute(
            "UPDATE predictions SET decision='skipped', decided_at=? "
            "WHERE decision='pending' AND lock_at < ?",
            (now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")),
        )
        self.conn.commit()
        return cur.rowcount

    def resolve(self, pred_id: str, **outcome: Any) -> None:
        """Write the outcome once. Refuses to rescore an already-resolved entry."""
        row = self.get(pred_id)
        if row is None:
            raise KeyError(pred_id)
        if row["status"] == "resolved":
            raise ValueError(f"{pred_id} is already resolved; entries are scored once")
        # Reject unknown keys rather than dropping them. Silently discarding a typo
        # like `net_excess=5.0` still marked the row resolved — with a NULL score,
        # permanently un-rescorable, invisible to the scorecard. The prediction
        # vanished from the record with no error at all.
        unknown = set(outcome) - _OUTCOME_COLS
        if unknown:
            raise ValueError(f"unknown outcome field(s): {sorted(unknown)}")
        if outcome.get("net_excess_pct") is None:
            raise ValueError("net_excess_pct is required to resolve a prediction")
        fields = dict(outcome)
        fields["status"] = "resolved"
        fields["resolved_at"] = utcnow()
        sets = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE predictions SET {sets} WHERE id=?", [*fields.values(), pred_id]
        )
        self.conn.commit()

    def log_run(self, kind: str, **kw: Any) -> None:
        """Record that a run happened — including runs that produced nothing.

        A quiet day and a crashed scraper must not look identical.
        """
        self.conn.execute(
            "INSERT INTO runs (id, ran_at, kind, n_candidates, n_predictions, "
            "n_resolved, rejections, error, notes) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex, utcnow(), kind,
                kw.get("n_candidates", 0), kw.get("n_predictions", 0),
                kw.get("n_resolved", 0),
                json.dumps(kw.get("rejections", [])),
                kw.get("error"), kw.get("notes"),
            ),
        )
        self.conn.commit()

    # ---------------------------------------------------------------- reading

    def get(self, pred_id: str) -> Optional[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM predictions WHERE id=?", (pred_id,))
        return cur.fetchone()

    def open_predictions(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM predictions WHERE status='open' ORDER BY created_at DESC"))

    def due(self, today: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM predictions WHERE status='open' AND resolves_on<=? "
            "ORDER BY resolves_on", (today,)))

    def resolved(self, engine: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM predictions WHERE status='resolved'"
        args: list[Any] = []
        if engine:
            sql += " AND engine=?"
            args.append(engine)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY resolved_at DESC", args)]

    def open_tickers(self) -> set[str]:
        return {r["ticker"] for r in self.open_predictions()}

    def verify_integrity(self) -> list[str]:
        """Recompute every content hash. Returns the ids of any tampered rows."""
        bad = []
        for r in self.conn.execute("SELECT * FROM predictions"):
            row = dict(r)
            if content_hash(row) != row["content_hash"]:
                bad.append(row["id"])
        return bad

    def _columns(self) -> set[str]:
        return {r[1] for r in self.conn.execute("PRAGMA table_info(predictions)")}
