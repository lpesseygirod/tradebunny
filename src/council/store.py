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
    kind TEXT NOT NULL,              -- idea | holding | portfolio (ticker PORTFOLIO)
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
    note TEXT DEFAULT '',
    broker TEXT DEFAULT 'paper'
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
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY,
    meeting_id INTEGER,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,            -- buy | sell | trim | deadline
    conviction TEXT DEFAULT 'none',
    amount_eur REAL NOT NULL,
    summary TEXT NOT NULL,
    data TEXT,                       -- JSON: the sizing the risk engine proposed
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected | expired | failed
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    result TEXT                      -- JSON: what happened when it was answered
);
CREATE TABLE IF NOT EXISTS marks (
    meeting_id INTEGER NOT NULL,
    horizon_days INTEGER NOT NULL,   -- 0 = the day of the decision; then 30, 91, 182, 365
    day TEXT NOT NULL,               -- when it was measured
    price_eur REAL NOT NULL,
    bench_eur REAL NOT NULL,
    PRIMARY KEY (meeting_id, horizon_days)
);
CREATE TABLE IF NOT EXISTS postmortems (
    id INTEGER PRIMARY KEY,
    meeting_id INTEGER NOT NULL,     -- the decision being examined
    ticker TEXT NOT NULL,
    reason TEXT NOT NULL,            -- why it was triggered
    status TEXT NOT NULL,            -- running | done | failed
    backend TEXT NOT NULL,
    created_at TEXT NOT NULL,
    cost_usd REAL DEFAULT 0,
    data TEXT                        -- JSON: what happened, the auditor's findings, sources
);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY,
    postmortem_id INTEGER,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',  -- proposed | approved | rejected
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS candidates (
    ticker TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,            -- new | shortlisted | watching | dropped
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT                        -- JSON: the scout's idea, why the status changed
);
CREATE TABLE IF NOT EXISTS dossiers (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    status TEXT NOT NULL,            -- running | done | failed
    backend TEXT NOT NULL,
    reason TEXT DEFAULT '',          -- why it was written
    created_at TEXT NOT NULL,
    cost_usd REAL DEFAULT 0,
    data TEXT                        -- JSON: the file, its sources, the numbers it saw
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    kind TEXT NOT NULL,              -- drop | earnings
    key TEXT NOT NULL UNIQUE,        -- one alert per event, e.g. drop:IFX.DE:2026-11-12
    reason TEXT NOT NULL,
    status TEXT NOT NULL,            -- open | noise | watch | convene | failed
    backend TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    cost_usd REAL DEFAULT 0,
    meeting_id INTEGER,              -- the review it escalated to
    data TEXT
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

    # ----------------------------------------------------------- proposals

    def add_proposal(self, *, meeting_id, ticker, action, conviction, amount_eur,
                     summary, data, expires_at) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO proposals(meeting_id,ticker,action,conviction,amount_eur,summary,"
                "data,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (meeting_id, ticker, action, conviction, amount_eur, summary,
                 json.dumps(data), now(), expires_at))
            return int(cur.lastrowid)

    @staticmethod
    def _proposal(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["data"] = json.loads(d["data"]) if d["data"] else {}
        d["result"] = json.loads(d["result"]) if d["result"] else None
        return d

    def proposal(self, pid: int) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        return self._proposal(r) if r else None

    def proposals(self, status: Optional[str] = None, meeting_id: Optional[int] = None) -> list[dict]:
        sql, args, where = "SELECT * FROM proposals", [], []
        if status:
            where.append("status=?"); args.append(status)
        if meeting_id is not None:
            where.append("meeting_id=?"); args.append(meeting_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._conn() as c:
            return [self._proposal(r) for r in c.execute(sql + " ORDER BY id DESC", args).fetchall()]

    def decide_proposal(self, pid: int, status: str, result=None) -> bool:
        """Answer a pending proposal once. False if it was not pending."""
        with self._conn() as c:
            n = c.execute("UPDATE proposals SET status=?, decided_at=?, result=? "
                          "WHERE id=? AND status='pending'",
                          (status, now(), json.dumps(result) if result is not None else None,
                           pid)).rowcount
        return n == 1

    # --------------------------------------------------------------- marks

    def add_mark(self, meeting_id: int, horizon_days: int, day: str,
                 price_eur: float, bench_eur: float) -> None:
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO marks(meeting_id,horizon_days,day,price_eur,bench_eur) "
                      "VALUES(?,?,?,?,?)", (meeting_id, horizon_days, day, price_eur, bench_eur))

    def marks(self, meeting_id: Optional[int] = None) -> list[dict]:
        sql, args = "SELECT * FROM marks", ()
        if meeting_id is not None:
            sql, args = sql + " WHERE meeting_id=?", (meeting_id,)
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql + " ORDER BY meeting_id, horizon_days", args).fetchall()]

    # --------------------------------------------------------- post-mortems

    def add_postmortem(self, meeting_id: int, ticker: str, reason: str, backend: str) -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO postmortems(meeting_id,ticker,reason,status,backend,created_at) "
                            "VALUES(?,?,?,?,?,?)", (meeting_id, ticker, reason, "running", backend, now()))
            return int(cur.lastrowid)

    def finish_postmortem(self, pid: int, status: str, data, cost_usd: float = 0.0) -> None:
        with self._conn() as c:
            c.execute("UPDATE postmortems SET status=?, data=?, cost_usd=? WHERE id=?",
                      (status, json.dumps(data), cost_usd, pid))

    def postmortems(self, meeting_id: Optional[int] = None) -> list[dict]:
        sql, args = "SELECT * FROM postmortems", ()
        if meeting_id is not None:
            sql, args = sql + " WHERE meeting_id=?", (meeting_id,)
        with self._conn() as c:
            rows = c.execute(sql + " ORDER BY id DESC", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data"]) if d["data"] else {}
            out.append(d)
        return out

    def postmortem(self, pid: int) -> Optional[dict]:
        return next((p for p in self.postmortems() if p["id"] == pid), None)

    # -------------------------------------------------------------- lessons

    def add_lesson(self, postmortem_id: Optional[int], text: str) -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO lessons(postmortem_id,text,created_at) VALUES(?,?,?)",
                            (postmortem_id, text, now()))
            return int(cur.lastrowid)

    def lessons(self, status: Optional[str] = None) -> list[dict]:
        sql, args = "SELECT * FROM lessons", ()
        if status:
            sql, args = sql + " WHERE status=?", (status,)
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql + " ORDER BY id", args).fetchall()]

    def decide_lesson(self, lid: int, status: str) -> bool:
        with self._conn() as c:
            return c.execute("UPDATE lessons SET status=?, decided_at=? WHERE id=? AND status='proposed'",
                             (status, now(), lid)).rowcount == 1

    # ----------------------------------------------------------- candidates

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["data"] = json.loads(d["data"]) if d.get("data") else {}
        return d

    def add_candidate(self, ticker: str, name: str, data: Any) -> bool:
        """False if the scout already suggested this name once."""
        with self._conn() as c:
            n = c.execute("INSERT OR IGNORE INTO candidates(ticker,name,status,created_at,updated_at,data) "
                          "VALUES(?,?,?,?,?,?)", (ticker, name, "new", now(), now(), json.dumps(data))).rowcount
        return n == 1

    def set_candidate(self, ticker: str, status: str, note: str = "") -> None:
        cand = self.candidate(ticker)
        if not cand:
            return
        data = cand["data"]
        data.setdefault("history", []).append({"at": now(), "status": status, "note": note})
        with self._conn() as c:
            c.execute("UPDATE candidates SET status=?, updated_at=?, data=? WHERE ticker=?",
                      (status, now(), json.dumps(data), ticker))

    def candidate(self, ticker: str) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM candidates WHERE ticker=?", (ticker,)).fetchone()
        return self._row(r) if r else None

    def candidates(self, status: Optional[str] = None) -> list[dict]:
        sql, args = "SELECT * FROM candidates", ()
        if status:
            sql, args = sql + " WHERE status=?", (status,)
        with self._conn() as c:
            return [self._row(r) for r in c.execute(sql + " ORDER BY created_at DESC", args).fetchall()]

    # -------------------------------------------------------- research files

    def add_dossier(self, ticker: str, backend: str, reason: str = "") -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO dossiers(ticker,status,backend,reason,created_at) VALUES(?,?,?,?,?)",
                            (ticker, "running", backend, reason, now()))
            return int(cur.lastrowid)

    def finish_dossier(self, did: int, status: str, data: Any, cost_usd: float = 0.0) -> None:
        with self._conn() as c:
            c.execute("UPDATE dossiers SET status=?, data=?, cost_usd=? WHERE id=?",
                      (status, json.dumps(data), cost_usd, did))

    def dossier(self, did: int) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM dossiers WHERE id=?", (did,)).fetchone()
        return self._row(r) if r else None

    def dossiers(self, ticker: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
        sql, args, where = "SELECT * FROM dossiers", [], []
        if ticker:
            where.append("ticker=?"); args.append(ticker)
        if status:
            where.append("status=?"); args.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._conn() as c:
            return [self._row(r) for r in c.execute(sql + " ORDER BY id DESC", args).fetchall()]

    def latest_dossier(self, ticker: str) -> Optional[dict]:
        return next(iter(self.dossiers(ticker, "done")), None)

    # --------------------------------------------------------------- alerts

    def add_alert(self, ticker: str, kind: str, key: str, reason: str, data: Any = None) -> Optional[int]:
        """None if this event was already raised (one alert per event)."""
        with self._conn() as c:
            cur = c.execute("INSERT OR IGNORE INTO alerts(ticker,kind,key,reason,status,created_at,data) "
                            "VALUES(?,?,?,?,?,?,?)", (ticker, kind, key, reason, "open", now(),
                                                      json.dumps(data or {})))
            return int(cur.lastrowid) if cur.rowcount == 1 else None

    def update_alert(self, aid: int, **kw: Any) -> None:
        if "data" in kw:
            kw["data"] = json.dumps(kw["data"])
        cols = ", ".join(f"{k}=?" for k in kw)
        with self._conn() as c:
            c.execute(f"UPDATE alerts SET {cols} WHERE id=?", (*kw.values(), aid))

    def alert(self, aid: int) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()
        return self._row(r) if r else None

    def alerts(self, ticker: Optional[str] = None, limit: int = 100) -> list[dict]:
        sql, args = "SELECT * FROM alerts", ()
        if ticker:
            sql, args = sql + " WHERE ticker=?", (ticker,)
        with self._conn() as c:
            return [self._row(r) for r in c.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()]

    # ----------------------------------------------------------- snapshots

    def snapshot(self, day: str, value_eur: float, benchmark_eur: float) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO snapshots(day,value_eur,benchmark_eur) VALUES(?,?,?) "
                      "ON CONFLICT(day) DO UPDATE SET value_eur=excluded.value_eur, "
                      "benchmark_eur=excluded.benchmark_eur", (day, value_eur, benchmark_eur))

    def snapshots(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM snapshots ORDER BY day").fetchall()]
