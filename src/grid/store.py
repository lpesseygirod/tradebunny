"""The shared record: transactions and events, in SQLite.

WHY THIS EXISTS NOW AND DID NOT BEFORE
======================================

Everything else on the grid side persists as JSON files, deliberately: a plan is
a file you can read, edit and diff, and `plan.py` exists so the executor can
rebuild its state from the plan plus the exchange rather than from a database.
That is still true and those files are unchanged.

But three of the interface's requirements are not file-shaped:

  - a bank-statement view of EVERY transaction, in order, with a running
    position and running P&L after each one;
  - notifications that persist with unread state, so you can see what happened
    while you were away;
  - the executor writing while the web process reads, continuously.

Doing that with JSON means rewriting a whole file on every fill, which loses
writes under concurrency and cannot answer "the last 50 transactions for this
grid" without loading everything. So: SQLite, in WAL mode, which is built for
exactly one writer and many readers.

APPEND-ONLY
===========

`transactions` is never updated and never deleted. A trade that happened
happened. The running position and P&L are DERIVED at read time rather than
stored, so they cannot drift out of agreement with the rows they summarise.

`events` is never deleted either — it is the record of what the system told
you. Marking one read sets a timestamp; nothing is removed.

WHAT THIS IS NOT
================

It is not a source of truth about what is ON the exchange. The executor still
reconstructs open orders and balances from the exchange every pass, because a
database can be stale and an exchange cannot. This is a LEDGER of what
happened, not a cache of what is.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

DB_PATH = Path(os.getenv("TRADEBUNNY_HOME", ".")).resolve() / "data" / "grid.db"

PAPER = "paper"
LIVE = "live"

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id           TEXT    NOT NULL,
    symbol            TEXT    NOT NULL,
    mode              TEXT    NOT NULL CHECK (mode IN ('paper','live')),
    ts                TEXT    NOT NULL,
    side              TEXT    NOT NULL CHECK (side IN ('buy','sell')),
    price             REAL    NOT NULL,
    qty               REAL    NOT NULL,
    value             REAL    NOT NULL,
    fee               REAL    NOT NULL DEFAULT 0,
    level_price       REAL,
    client_order_id   TEXT,
    UNIQUE (plan_id, client_order_id, ts, side, price, qty)
);
CREATE INDEX IF NOT EXISTS ix_tx_plan ON transactions (plan_id, id);
CREATE INDEX IF NOT EXISTS ix_tx_mode ON transactions (mode, id);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT    NOT NULL,
    kind      TEXT    NOT NULL,
    symbol    TEXT    NOT NULL DEFAULT '',
    plan_id   TEXT    NOT NULL DEFAULT '',
    title     TEXT    NOT NULL,
    body      TEXT    NOT NULL DEFAULT '',
    severity  TEXT    NOT NULL DEFAULT 'info',
    read_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_ev_unread ON events (read_at, id);
"""

#: Event kinds. The same set that goes to Telegram.
PROPOSAL = "proposal"
FILL = "fill"
STOP_OUT = "stop_out"
ORDER_REJECTED = "order_rejected"
DRIFT = "drift"
EXECUTOR = "executor"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Transaction:
    """One fill, plus the running totals AFTER it. Derived, never stored."""

    id: int
    plan_id: str
    symbol: str
    mode: str
    ts: str
    side: str
    price: float
    qty: float
    value: float
    fee: float
    level_price: Optional[float] = None
    client_order_id: str = ""
    position: float = 0.0        # units held after this transaction
    cash: float = 0.0            # cumulative cash flow after this one
    realised: float = 0.0        # realised P&L after this one


@dataclass(frozen=True)
class Event:
    id: int
    ts: str
    kind: str
    symbol: str
    plan_id: str
    title: str
    body: str
    severity: str
    read_at: Optional[str]

    @property
    def unread(self) -> bool:
        return not self.read_at


class Store:
    """Transactions and events. One writer (the executor), many readers."""

    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=10.0,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL is the whole reason this is SQLite: the executor writes while the
        # web process reads, and neither should block the other.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------- transactions

    def record_transaction(self, *, plan_id: str, symbol: str, mode: str,
                           side: str, price: float, qty: float, fee: float = 0.0,
                           level_price: Optional[float] = None,
                           client_order_id: str = "",
                           ts: Optional[str] = None) -> Optional[int]:
        """Append one fill. Returns its id, or None if it was a duplicate.

        The UNIQUE constraint makes this idempotent: the executor re-derives
        state from the exchange every pass and may see the same fill twice, and
        a double-counted trade would corrupt every figure downstream.
        """
        if mode not in (PAPER, LIVE):
            raise ValueError(f"mode must be {PAPER!r} or {LIVE!r}, got {mode!r}")
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be buy or sell, got {side!r}")
        try:
            cur = self._conn.execute(
                "INSERT INTO transactions (plan_id, symbol, mode, ts, side, "
                "price, qty, value, fee, level_price, client_order_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, symbol, mode, ts or utcnow(), side, float(price),
                 float(qty), float(price) * float(qty), float(fee),
                 level_price, client_order_id))
            self._conn.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None            # already recorded; not an error

    def transactions(self, plan_id: Optional[str] = None,
                     mode: Optional[str] = None, limit: int = 1000,
                     newest_first: bool = True) -> list[Transaction]:
        """Transactions with running position, cash and realised P&L.

        The running figures are computed forwards from the first row, then the
        list is reversed if asked for newest-first — so "running P&L after this
        transaction" means the same thing whichever way you read it.
        """
        sql = "SELECT * FROM transactions"
        args: list[Any] = []
        where = []
        if plan_id:
            where.append("plan_id = ?")
            args.append(plan_id)
        if mode:
            where.append("mode = ?")
            args.append(mode)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id ASC"
        rows = self._conn.execute(sql, args).fetchall()

        out: list[Transaction] = []
        position = cash = realised = 0.0
        cost_basis = 0.0           # total cost of units currently held
        for r in rows:
            qty, price, fee = r["qty"], r["price"], r["fee"]
            if r["side"] == "buy":
                position += qty
                cost_basis += price * qty + fee
                cash -= price * qty + fee
            else:
                # Realise against the average cost of what is held. Average
                # cost, not FIFO: a grid's lots are fungible and the average is
                # what the position statement would show.
                avg = (cost_basis / position) if position > 0 else price
                sold = min(qty, position) if position > 0 else qty
                realised += (price - avg) * sold - fee
                cost_basis = max(0.0, cost_basis - avg * sold)
                position = max(0.0, position - qty)
                cash += price * qty - fee
            out.append(Transaction(
                id=r["id"], plan_id=r["plan_id"], symbol=r["symbol"],
                mode=r["mode"], ts=r["ts"], side=r["side"], price=price,
                qty=qty, value=r["value"], fee=fee,
                level_price=r["level_price"],
                client_order_id=r["client_order_id"] or "",
                position=position, cash=cash, realised=realised))
        if newest_first:
            out.reverse()
        return out[:limit] if limit else out

    def plan_ids(self, mode: Optional[str] = None) -> list[str]:
        sql = "SELECT DISTINCT plan_id FROM transactions"
        args: list[Any] = []
        if mode:
            sql += " WHERE mode = ?"
            args.append(mode)
        return [r[0] for r in self._conn.execute(sql, args).fetchall()]

    # ------------------------------------------------------------- events

    def record_event(self, kind: str, title: str, *, body: str = "",
                     symbol: str = "", plan_id: str = "",
                     severity: str = "info",
                     ts: Optional[str] = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO events (ts, kind, symbol, plan_id, title, body, "
            "severity) VALUES (?,?,?,?,?,?,?)",
            (ts or utcnow(), kind, symbol, plan_id, title, body, severity))
        self._conn.commit()
        return int(cur.lastrowid)

    def events(self, limit: int = 200, unread_only: bool = False) -> list[Event]:
        sql = "SELECT * FROM events"
        if unread_only:
            sql += " WHERE read_at IS NULL"
        sql += " ORDER BY id DESC LIMIT ?"
        return [Event(id=r["id"], ts=r["ts"], kind=r["kind"],
                      symbol=r["symbol"], plan_id=r["plan_id"],
                      title=r["title"], body=r["body"], severity=r["severity"],
                      read_at=r["read_at"])
                for r in self._conn.execute(sql, (limit,)).fetchall()]

    def unread_count(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM events WHERE read_at IS NULL").fetchone()[0])

    def mark_read(self, event_id: Optional[int] = None) -> int:
        """Mark one event, or all of them, read. Never deletes anything."""
        if event_id is None:
            cur = self._conn.execute(
                "UPDATE events SET read_at = ? WHERE read_at IS NULL",
                (utcnow(),))
        else:
            cur = self._conn.execute(
                "UPDATE events SET read_at = ? WHERE id = ? AND read_at IS NULL",
                (utcnow(), event_id))
        self._conn.commit()
        return cur.rowcount


# ------------------------------------------------------------- aggregation

@dataclass(frozen=True)
class Totals:
    """What one mode (paper or live), or one grid, is worth."""

    mode: str = ""
    invested: float = 0.0        # cash still deployed
    current_value: float = 0.0   # units held, marked to market
    realised: float = 0.0
    unrealised: float = 0.0
    fees: float = 0.0
    position: float = 0.0
    n_transactions: int = 0
    buy_and_hold: Optional[float] = None   # same money, bought once and held

    @property
    def total(self) -> float:
        return self.realised + self.unrealised

    @property
    def beats_buy_and_hold(self) -> Optional[bool]:
        if self.buy_and_hold is None:
            return None
        return self.total > self.buy_and_hold


def totals(transactions: Iterable[Transaction],
           marks: Optional[dict[str, float]] = None,
           mode: str = "", scale: float = 1.0) -> Totals:
    """Aggregate a transaction list, marked to `marks[symbol]`.

    `scale` re-derives every figure as if the same fills had been taken at a
    different capital — see the hypothetical-size selector. It multiplies
    quantities, so prices and the shape of the result are untouched.

    BUY-AND-HOLD is computed over the SAME period as the grid: the money the
    grid put in at its first transaction, invested in the asset at that price
    and held to the current mark. It is the honest comparison, because a grid
    that made 2% while the asset rose 40% did not work.
    """
    marks = marks or {}
    txs = list(transactions)
    if not txs:
        return Totals(mode=mode)

    ordered = sorted(txs, key=lambda t: t.id)
    position = cost_basis = realised = fees = 0.0
    first_price: dict[str, float] = {}
    deployed: dict[str, float] = {}
    for t in ordered:
        qty = t.qty * scale
        fee = t.fee * scale
        fees += fee
        first_price.setdefault(t.symbol, t.price)
        if t.side == "buy":
            position += qty
            cost_basis += t.price * qty + fee
            deployed[t.symbol] = deployed.get(t.symbol, 0.0) + t.price * qty
        else:
            avg = (cost_basis / position) if position > 0 else t.price
            sold = min(qty, position) if position > 0 else qty
            realised += (t.price - avg) * sold - fee
            cost_basis = max(0.0, cost_basis - avg * sold)
            position = max(0.0, position - qty)

    symbol = ordered[-1].symbol
    mark = marks.get(symbol, ordered[-1].price)
    current_value = position * mark
    unrealised = current_value - cost_basis

    bh = None
    if first_price.get(symbol) and deployed.get(symbol):
        units = deployed[symbol] / first_price[symbol]
        bh = units * mark - deployed[symbol]

    return Totals(mode=mode, invested=cost_basis, current_value=current_value,
                  realised=realised, unrealised=unrealised, fees=fees,
                  position=position, n_transactions=len(txs), buy_and_hold=bh)
