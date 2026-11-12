"""The committee secretary: everything the council says and does, on record.

One SQLite file. Meetings and their steps are append-only, so the interface
can replay any debate exactly as it happened, and the scorekeeper can later
ask which role was right. Each call opens its own connection, so the web
server and a meeting running in a background thread can share the file.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    kind TEXT NOT NULL,              -- idea | holding
    status TEXT NOT NULL,            -- running | done | stopped | failed
    backend TEXT NOT NULL,           -- claude | scripted
    started_at TEXT NOT NULL,
    finished_at TEXT,
    cost_usd REAL DEFAULT 0,
    decision TEXT,                   -- JSON: the CIO's verdict
    outcome TEXT,                    -- JSON: what the risk engine allowed and did
    error TEXT
);
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id),
    seq INTEGER NOT NULL,
    phase TEXT NOT NULL,             -- gate | facts | blind | cross | committee | risk | trade
    role TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',   -- markdown
    data TEXT,                       -- JSON
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,              -- buy | sell
    qty REAL NOT NULL,
    price REAL NOT NULL,             -- listing currency
    currency TEXT NOT NULL,
    price_eur REAL NOT NULL,
    fee_eur REAL NOT NULL,
    amount_eur REAL NOT NULL,        -- excluding the fee
    meeting_id INTEGER,
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tranches (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    due TEXT NOT NULL,               -- YYYY-MM-DD
    amount_eur REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',   -- planned | done | cancelled
    meeting_id INTEGER,
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS snapshots (
    day TEXT PRIMARY KEY,            -- one per day
    value_eur REAL NOT NULL,
    benchmark_eur REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path or paths.db_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ---------------------------------------------------------------- meta

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._conn() as c:
            r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set_meta(self, key: str, value: Any) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value)))

    # ------------------------------------------------------------ meetings

    def start_meeting(self, ticker: str, kind: str, backend: str) -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO meetings(ticker,kind,status,backend,started_at) "
                            "VALUES(?,?,?,?,?)", (ticker, kind, "running", backend, now()))
            return int(cur.lastrowid)

    def add_step(self, meeting_id: int, phase: str, role: str, title: str,
                 body: str = "", data: Any = None) -> int:
        with self._conn() as c:
            seq = c.execute("SELECT COALESCE(MAX(seq),0)+1 FROM steps WHERE meeting_id=?",
                            (meeting_id,)).fetchone()[0]
            cur = c.execute(
                "INSERT INTO steps(meeting_id,seq,phase,role,title,body,data,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (meeting_id, seq, phase, role, title, body,
                 json.dumps(data) if data is not None else None, now()))
            return int(cur.lastrowid)

    def finish_meeting(self, meeting_id: int, status: str, *, cost_usd: float = 0.0,
                       decision: Any = None, outcome: Any = None,
                       error: str = "") -> None:
        with self._conn() as c:
            c.execute("UPDATE meetings SET status=?, finished_at=?, cost_usd=?, decision=?, "
                      "outcome=?, error=? WHERE id=?",
                      (status, now(), cost_usd,
                       json.dumps(decision) if decision is not None else None,
                       json.dumps(outcome) if outcome is not None else None,
                       error, meeting_id))

    def set_cost(self, meeting_id: int, cost_usd: float) -> None:
        with self._conn() as c:
            c.execute("UPDATE meetings SET cost_usd=? WHERE id=?", (cost_usd, meeting_id))

    @staticmethod
    def _meeting(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in ("decision", "outcome"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        return d

    def meeting(self, meeting_id: int) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        return self._meeting(r) if r else None

    def meetings(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM meetings ORDER BY id DESC LIMIT ?",
                             (limit,)).fetchall()
        return [self._meeting(r) for r in rows]

    def steps(self, meeting_id: int) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM steps WHERE meeting_id=? ORDER BY seq",
                             (meeting_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data"]) if d["data"] else None
            out.append(d)
        return out

    def meetings_this_month(self, kind: str = "idea") -> int:
        month = now()[:7]
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM meetings WHERE kind=? AND started_at LIKE ? "
                             "AND status != 'stopped'", (kind, month + "%")).fetchone()[0]

    # -------------------------------------------------------------- trades

    def add_trade(self, **t: Any) -> int:
        t.setdefault("ts", now())
        cols = ",".join(t)
        with self._conn() as c:
            cur = c.execute(f"INSERT INTO trades({cols}) VALUES({','.join('?' * len(t))})",
                            tuple(t.values()))
            return int(cur.lastrowid)

    def trades(self, ticker: Optional[str] = None) -> list[dict]:
        sql, args = "SELECT * FROM trades", ()
        if ticker:
            sql, args = sql + " WHERE ticker=?", (ticker,)
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql + " ORDER BY id", args).fetchall()]

    # ------------------------------------------------------------ tranches

    def plan_tranche(self, ticker: str, due: str, amount_eur: float,
                     meeting_id: Optional[int] = None, note: str = "") -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO tranches(ticker,due,amount_eur,meeting_id,note) "
                            "VALUES(?,?,?,?,?)", (ticker, due, amount_eur, meeting_id, note))
            return int(cur.lastrowid)

    def tranches(self, status: Optional[str] = "planned") -> list[dict]:
        sql, args = "SELECT * FROM tranches", ()
        if status:
            sql, args = sql + " WHERE status=?", (status,)
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql + " ORDER BY due, id", args).fetchall()]

    def set_tranche(self, tranche_id: int, status: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE tranches SET status=? WHERE id=?", (status, tranche_id))

    def cancel_tranches(self, ticker: str) -> int:
        with self._conn() as c:
            return c.execute("UPDATE tranches SET status='cancelled' WHERE ticker=? "
                             "AND status='planned'", (ticker,)).rowcount

    # ----------------------------------------------------------- snapshots

    def snapshot(self, day: str, value_eur: float, benchmark_eur: float) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO snapshots(day,value_eur,benchmark_eur) VALUES(?,?,?) "
                      "ON CONFLICT(day) DO UPDATE SET value_eur=excluded.value_eur, "
                      "benchmark_eur=excluded.benchmark_eur", (day, value_eur, benchmark_eur))

    def snapshots(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM snapshots ORDER BY day").fetchall()]
