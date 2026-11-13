"""The executor — reads an approved plan and applies it. No AI, no judgement.

The other half of this system reasons. This half does not. It contains no model
call, no threshold it may reinterpret, and no branch that depends on anything
other than the plan, the exchange and the control file. Any decision belongs
in `analyst.py` and needs my approval before it runs.

Four rules, each of which exists because of a specific way grid bots lose money:

  1. **State comes from the exchange, never from memory.** `run_once` begins by
     asking the exchange what orders are open and what is held. A bot that
     caches its position and restarts will re-buy a ladder it already owns, and
     the second copy is bought with money the plan never allocated. Nothing is
     remembered between calls; everything is re-derived.

  2. **Idempotent by construction.** Every order carries a `clientOrderId` that
     is a deterministic hash of (plan id, level index, side). Running twice
     produces the same ids, and an id that is already resting is left alone
     rather than replaced. A retry after a timeout — the case where the bot
     does not know whether its order arrived — cannot double-place.

  3. **Limit orders only.** The grid rests maker orders and never crosses the
     spread. The single exception is the stop, which is a market sell, because
     a limit order during a breakout is a request to not be filled. That
     exception is in exactly one place and is guarded.

  4. **Paper by default.** `control.is_live()` must be explicitly true, which
     itself requires the confirmation phrase in `control.set_live`. A live
     exchange handed to an executor whose control file says paper is refused.

WHAT "OCCUPIED" MEANS. The exchange reports one number for the base balance, not
a list of lots. To decide which ladder levels already hold stock, the executor
assumes the lots sit at the levels nearest below the current price and works
downward until the balance is used up. That inference is the
conservative one: assuming lots sit high marks MORE levels as occupied (a lot
costs fewer units at a higher price), which places FEWER new buy orders. Being
wrong in this direction under-deploys capital; being wrong the other way would
buy a level twice.

STATE FOR THE UI is a JSON snapshot per plan under `data/state/`, written at the
end of every run. Plans and control are files too, so the UI reads the same
bytes the executor wrote. Fills and events also go to the SQLite store
(`store.py`) when one is passed, which is what the app's trade list reads.

PAPER TRADING. `PaperExchange` is a local simulated exchange. In the executor
loop, `sync_paper_market` feeds it the real OKX last price every pass, funds
each approved grid with its approved amount, and fills resting orders the
price reaches. Nothing is ever sent to OKX: the live order client
(`LiveExchange`) is not built.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from . import execution
from . import store as gridstore
from .control import Control
from .plan import GridPlan, Status

log = logging.getLogger(__name__)

STATE_DIR = Path(os.getenv("TRADEBUNNY_HOME", ".")).resolve() / "data" / "state"
HEARTBEAT_PATH = Path(os.getenv("TRADEBUNNY_HOME", ".")).resolve() / "data" / "heartbeat.json"

#: A heartbeat older than this means the executor is not running. That is not a
#: cosmetic problem: while it is down, every open grid has no stop-loss, because
#: the stop is enforced by `run_once` and by nothing else. The exchange holds
#: the resting limit orders but knows nothing about `plan.stop_below`.
STALE_AFTER_SECONDS = 300

BUY = "buy"
SELL = "sell"

#: Balances below this are rounding dust, not a position.
DUST = 1e-9


# --------------------------------------------------------------- order model

@dataclass(frozen=True)
class Order:
    client_order_id: str
    symbol: str
    side: str                 # BUY | SELL
    price: float
    size: float               # base units
    order_id: str = ""
    status: str = "open"

    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass(frozen=True)
class Balance:
    base: float = 0.0         # units of the asset
    quote: float = 0.0        # units of the quote currency


def client_order_id(plan_id: str, level_index: int, side: str) -> str:
    """Deterministic id for one (plan, level, side) slot.

    The whole idempotency guarantee rests on this being a pure function of
    those three things: the same slot always produces the same id, so a retry
    collides with the resting order instead of adding a second one.

    OKX accepts 1-32 alphanumeric characters for clOrdId, so this is a
    hex digest, truncated, with a letter in front.
    """
    raw = f"{plan_id}:{level_index}:{side}"
    return "g" + hashlib.sha256(raw.encode()).hexdigest()[:23]


def plan_order_ids(plan: GridPlan) -> set[str]:
    """Every id this plan could ever use.

    Orders cannot be attributed to a plan by decoding the hash, so instead the
    full set is enumerated and matched. That also means an order placed by a
    different plan on the same symbol is never touched.
    """
    return {client_order_id(plan.id, i, side)
            for i in range(len(plan.grid_prices))
            for side in (BUY, SELL)}


# ------------------------------------------------------------ the interface

class Exchange(Protocol):
    """What the executor needs. Implemented by PaperExchange and, eventually,
    a live OKX client. Kept small: the executor should not be able to
    do anything more interesting than this."""

    is_live: bool

    def fetch_price(self, symbol: str) -> float: ...
    def fetch_open_orders(self, symbol: str) -> list[Order]: ...
    def fetch_balance(self, symbol: str) -> Balance: ...
    def place_limit(self, symbol: str, side: str, price: float, size: float,
                    client_order_id: str) -> Order: ...
    def cancel(self, symbol: str, order: Order) -> None: ...
    def market_sell(self, symbol: str, size: float) -> dict[str, Any]: ...


# ------------------------------------------------------------ paper exchange

class PaperExchange:
    """A local exchange. The default, and what every test runs against.

    Persists to JSON so that "restart the bot" is a real scenario rather than a
    simulated one: a test can build a second PaperExchange over the same file
    and it sees exactly what the first one left behind.
    """

    is_live = False

    def __init__(self, path: Optional[Path] = None, *, quote: float = 0.0,
                 price: float = 0.0, fee: float = 0.0008,
                 min_size: float = 0.0):
        self.path = Path(path) if path else None
        self.fee = fee
        #: The instrument's minSz. Zero disables the check, which is the right
        #: default for the many tests that predate it; set it to model a real
        #: instrument's floor.
        self.min_size = min_size
        self.orders: dict[str, Order] = {}
        self.balances: dict[str, Balance] = {}
        self.prices: dict[str, float] = {}
        self.fills: list[dict[str, Any]] = []
        self.market_sells: list[dict[str, Any]] = []
        #: Plan ids this paper account has already been funded for. See `fund`.
        self.funded: list[str] = []
        self._seq = 0
        self._default_quote = quote
        if price:
            self.prices["*"] = price
        self._load()

    # ------------------------------------------------------------ persistence

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        raw = json.loads(self.path.read_text())
        self.orders = {k: Order(**v) for k, v in raw.get("orders", {}).items()}
        self.balances = {k: Balance(**v) for k, v in raw.get("balances", {}).items()}
        self.prices = raw.get("prices", {})
        self.fills = raw.get("fills", [])
        self.market_sells = raw.get("market_sells", [])
        self.funded = raw.get("funded", [])
        self._seq = raw.get("seq", 0)

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "orders": {k: asdict(v) for k, v in self.orders.items()},
            "balances": {k: asdict(v) for k, v in self.balances.items()},
            "prices": self.prices, "fills": self.fills,
            "market_sells": self.market_sells, "funded": self.funded,
            "seq": self._seq,
        }
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------- interface

    def fetch_price(self, symbol: str) -> float:
        if symbol in self.prices:
            return float(self.prices[symbol])
        if "*" in self.prices:
            return float(self.prices["*"])
        raise RuntimeError(f"no paper price set for {symbol}; call set_price first")

    def fetch_open_orders(self, symbol: str) -> list[Order]:
        return [o for o in self.orders.values()
                if o.symbol == symbol and o.status == "open"]

    def fetch_balance(self, symbol: str) -> Balance:
        return self.balances.get(symbol, Balance(0.0, self._default_quote))

    def place_limit(self, symbol: str, side: str, price: float, size: float,
                    client_order_id: str) -> Order:
        if side not in (BUY, SELL):
            raise ValueError(f"bad side {side!r}")
        if size <= 0 or price <= 0:
            raise ValueError("price and size must be positive")
        if self.min_size and size < self.min_size:
            raise BelowMinimumSize(
                f"{size:g} is below the {self.min_size:g} minimum order size "
                f"for {symbol} — the exchange would reject this")
        existing = self.orders.get(client_order_id)
        if existing is not None and existing.status == "open":
            # The exchange's own idempotency: a duplicate clOrdId is rejected
            # rather than accepted as a second order. OKX behaves this way too,
            # which is the belt to the executor's braces.
            raise DuplicateOrder(
                f"clOrdId {client_order_id} already resting on {symbol}")
        # Crossing the spread with a limit order would fill as taker. The
        # executor must never do this; refusing here makes that a test failure
        # rather than a silent extra 0.02% on every cycle.
        mark = self.fetch_price(symbol)
        if side == BUY and price > mark:
            raise CrossedSpread(f"buy limit {price} is above the mark {mark}")
        if side == SELL and price < mark:
            raise CrossedSpread(f"sell limit {price} is below the mark {mark}")

        self._seq += 1
        o = Order(client_order_id=client_order_id, symbol=symbol, side=side,
                  price=float(price), size=float(size),
                  order_id=f"paper-{self._seq}", status="open")
        self.orders[client_order_id] = o
        self._save()
        return o

    def cancel(self, symbol: str, order: Order) -> None:
        got = self.orders.get(order.client_order_id)
        if got is not None and got.status == "open":
            self.orders[order.client_order_id] = Order(
                **{**asdict(got), "status": "cancelled"})
            self._save()

    def market_sell(self, symbol: str, size: float) -> dict[str, Any]:
        price = self.fetch_price(symbol)
        bal = self.fetch_balance(symbol)
        size = min(size, bal.base)
        proceeds = price * size * (1 - self.fee)
        self.balances[symbol] = Balance(base=max(0.0, bal.base - size),
                                        quote=bal.quote + proceeds)
        rec = {"symbol": symbol, "side": SELL, "size": size, "price": price,
               "proceeds": proceeds, "type": "market"}
        self.market_sells.append(rec)
        self._save()
        return rec

    # ------------------------------------------------------- test/paper helpers

    def fetch_fills(self, symbol: str, since_index: int = 0) -> list[dict]:
        """Fills recorded so far. Optional on the Exchange protocol — an
        exchange that cannot report fills yields no quality samples rather
        than invented ones."""
        mine = [x for x in self.fills if x.get("symbol") == symbol]
        return mine[since_index:]

    def set_price(self, symbol: str, price: float) -> None:
        self.prices[symbol] = float(price)
        self._save()

    def fund(self, symbol: str, plan_id: str, quote: float) -> bool:
        """Give an approved plan its paper money, once.

        A paper grid needs quote currency to buy with, and the only fair
        amount is the one that was approved. Recorded by plan id so a restart,
        or a second plan on the same symbol, is never funded twice. Returns
        True if money was added on this call.
        """
        if plan_id in self.funded or quote <= 0:
            return False
        self.funded.append(plan_id)
        self.credit(symbol, quote=quote)      # credit() saves
        return True

    def credit(self, symbol: str, base: float = 0.0, quote: float = 0.0) -> None:
        b = self.fetch_balance(symbol)
        self.balances[symbol] = Balance(b.base + base, b.quote + quote)
        self._save()

    def advance_to(self, symbol: str, price: float) -> list[Order]:
        """Move the mark and fill every resting order the move reaches.

        A buy at 99 fills when the price trades down to 99 or below; a sell at
        101 fills when it trades up to 101 or above. This is what makes the
        paper exchange worth having — a grid can actually cycle against it.
        """
        filled: list[Order] = []
        for cid, o in list(self.orders.items()):
            if o.symbol != symbol or o.status != "open":
                continue
            if (o.side == BUY and price <= o.price) or (o.side == SELL and price >= o.price):
                bal = self.fetch_balance(symbol)
                if o.side == BUY:
                    cost = o.price * o.size * (1 + self.fee)
                    self.balances[symbol] = Balance(bal.base + o.size,
                                                    bal.quote - cost)
                else:
                    proceeds = o.price * o.size * (1 - self.fee)
                    self.balances[symbol] = Balance(max(0.0, bal.base - o.size),
                                                    bal.quote + proceeds)
                self.orders[cid] = Order(**{**asdict(o), "status": "filled"})
                self.fills.append({**asdict(o), "status": "filled"})
                filled.append(o)
        self.prices[symbol] = float(price)
        self._save()
        return filled


class DuplicateOrder(RuntimeError):
    """A clientOrderId that is already resting. Never a reason to place again."""


class CrossedSpread(RuntimeError):
    """A limit order priced through the mark — it would fill as taker."""


class BelowMinimumSize(RuntimeError):
    """An order smaller than the instrument's minSz. OKX rejects these, and so
    does the paper exchange, so a backtest cannot quietly assume they filled."""


class LiveExchange:
    """Placeholder for the real OKX private client.

    Not implemented. Every method raises, so there is no path by
    which an incomplete live client can place a real order — a stub that
    silently did nothing would be worse, and one written against an API this
    repo has never been able to call would be worse still. Building it is its
    own task, with its own verification against a funded account.
    """

    is_live = True

    def _refuse(self, *a: Any, **k: Any):
        raise NotImplementedError(
            "the live OKX client is not built yet. Trade on PaperExchange, or "
            "implement this against the OKX private API and verify it against a "
            "funded account before enabling live trading.")

    fetch_price = fetch_open_orders = fetch_balance = _refuse
    place_limit = cancel = market_sell = _refuse


def make_exchange(control: Control, path: Optional[Path] = None) -> Exchange:
    """Paper unless the control file explicitly says live."""
    if control.is_live():
        return LiveExchange()
    return PaperExchange(path)


# ------------------------------------------------------------- paper market

def sync_paper_market(exchange: PaperExchange, plans: list[GridPlan],
                      prices: Callable[[], dict[str, float]],
                      store: Optional[Any] = None,
                      held: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Bring the paper exchange up to the real market before a pass.

    For every plan: fund it once with its approved amount, then move the paper
    mark to the real last price, filling every resting order that price
    reaches (`PaperExchange.advance_to`). `prices` returns {symbol: last
    price}; the loop passes `cli.okx_last_prices`, so this module still never
    talks to an exchange itself. Without this the paper exchange has
    no price, no money and no fills, and `run_once` cannot run.

    One sample per pass, so a wick that comes and goes between two passes is
    missed. That under-counts fills rather than inventing them.

    A plan in `held` (see `admit`) still gets a price, because its stop is
    still enforced, but is not funded until it is admitted.

    Returns {symbol: problem} for symbols that got no fresh price. The caller
    must skip those plans: a paper stop-loss checked against a stale price
    proves nothing.
    """
    if not plans:
        return {}
    try:
        quotes = prices()
    except Exception as e:
        why = f"no market price ({type(e).__name__}: {e})"
        return {p.symbol: why for p in plans}

    problems: dict[str, str] = {}
    for plan in plans:
        if plan.status is Status.APPROVED and plan.id not in (held or {}) and exchange.fund(
                plan.symbol, plan.id, plan.max_quote_total):
            log.info("%s: paper account funded with %g", plan.symbol,
                     plan.max_quote_total)
            _event(store, gridstore.EXECUTOR,
                   f"{plan.symbol} paper grid funded with {plan.max_quote_total:.2f}",
                   symbol=plan.symbol, plan_id=plan.id)
        price = quotes.get(plan.symbol)
        if not price or price <= 0:
            problems[plan.symbol] = f"no market price for {plan.symbol}"
            continue
        exchange.advance_to(plan.symbol, float(price))
    return problems


# --------------------------------------------------------------- safety caps

def admit(plans: list[GridPlan], control: Control) -> dict[str, str]:
    """Hold back any grid that breaks the caps in control.json.

    Returns {plan id: reason}. A held plan places no orders (see `run_once`).
    The caps mean what they mean everywhere else in the code:

      max_concurrent_grids   how many grids may run at once
      max_total_quote        the most any one grid may commit
                             (the same check the approval page makes)

    Grids already running keep their place ahead of newly approved ones, and
    older approvals ahead of newer ones, so lowering a cap holds the newest
    grids first.
    """
    s = control.state()
    order = {Status.RUNNING: 0, Status.APPROVED: 1}
    ranked = sorted(plans, key=lambda p: (order.get(p.status, 2),
                                          p.decided_at or "", p.id))
    held: dict[str, str] = {}
    admitted = 0
    for plan in ranked:
        if plan.max_quote_total > s.max_total_quote + 1e-9:
            held[plan.id] = (f"held: commits {plan.max_quote_total:g}, over the "
                             f"{s.max_total_quote:g} per-grid cap in control.json")
        elif admitted >= s.max_concurrent_grids:
            held[plan.id] = (f"held: {s.max_concurrent_grids} grid(s) already "
                             f"running, the max_concurrent_grids cap")
        else:
            admitted += 1
    return held


# ----------------------------------------------------------------- run state

@dataclass
class GridState:
    """The snapshot the UI reads. Written at the end of every run."""

    plan_id: str
    symbol: str
    status: str
    price: float = 0.0
    units_held: float = 0.0
    quote_deployed: float = 0.0
    open_buys: int = 0
    open_sells: int = 0
    occupied_levels: list[int] = field(default_factory=list)
    placed: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    stopped_out: bool = False
    halted: bool = False
    note: str = ""
    updated_at: str = ""
    live: bool = False

    def save(self, directory: Path = STATE_DIR) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        path = directory / f"{self.plan_id}.json"
        fd, tmp = tempfile.mkstemp(dir=str(directory), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(asdict(self), f, indent=2)
            os.replace(tmp, path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path


def load_state(plan_id: str, directory: Path = STATE_DIR) -> Optional[GridState]:
    path = Path(directory) / f"{plan_id}.json"
    if not path.exists():
        return None
    try:
        return GridState(**json.loads(path.read_text()))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None      # a corrupt snapshot is a UI problem, never a trading one


# ------------------------------------------------------------------- helpers

@dataclass
class Heartbeat:
    """Proof the executor is alive, written after every pass.

    The web page reads this and shouts if it goes stale. It is the only way to
    tell a calm market from a dead process, and the two look identical from the
    outside right up until a grid breaks its range with nothing watching.
    """

    pid: int = 0
    started_at: str = ""
    updated_at: str = ""
    passes: int = 0
    plans: list[str] = field(default_factory=list)
    last_error: str = ""
    stopped_cleanly: bool = False

    def save(self, path: Path = HEARTBEAT_PATH) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(asdict(self), f, indent=2)
            os.replace(tmp, path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        if not self.updated_at:
            return float("inf")
        try:
            then = datetime.fromisoformat(self.updated_at)
        except ValueError:
            return float("inf")
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - then).total_seconds())

    def is_stale(self, now: Optional[datetime] = None) -> bool:
        """Nothing has been written for longer than the threshold."""
        return self.age_seconds(now) > STALE_AFTER_SECONDS

    def is_down(self, now: Optional[datetime] = None) -> bool:
        """Not running, by any available evidence.

        Staleness is an inference — nothing has been heard for a while. But a
        heartbeat marked `stopped_cleanly` is a fact: the loop caught SIGTERM
        and exited. Waiting out the full five minutes before saying so would
        stay quiet during the window in which the grids are already unguarded.
        """
        return self.stopped_cleanly or self.is_stale(now)


def load_heartbeat(path: Path = HEARTBEAT_PATH) -> Optional[Heartbeat]:
    """The heartbeat, or None if there is not one to read.

    None and stale mean the same thing to a caller — the executor is not
    running — and both must render as a warning, never as silence.
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        return Heartbeat(**json.loads(path.read_text()))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _notify(message: str) -> None:
    """Best effort. A failed notification must never leave a stop half-done."""
    try:
        from ledger import notify as _n
        _n.send(message)
    except Exception as e:
        log.warning("could not notify: %s: %s", type(e).__name__, e)


def occupied_levels(plan: GridPlan, price: float, units: float) -> list[int]:
    """Which ladder levels are inferred to hold stock. See the module docstring.

    Walks the levels below the mark from the top down, consuming the balance.
    """
    out: list[int] = []
    remaining = units
    ladder = plan.grid_prices
    for i in range(len(ladder) - 1, -1, -1):
        if remaining <= DUST:
            break
        if ladder[i] >= price:
            continue
        lot = plan.quote_per_level / ladder[i]
        if remaining >= lot * 0.5:      # a partial fill still occupies the level
            out.append(i)
            remaining -= lot
    return sorted(out)


def desired_orders(plan: GridPlan, price: float, units: float) -> dict[str, Order]:
    """The complete set of orders that SHOULD be resting, keyed by clOrdId.

    Pure: same plan, same price, same balance, same answer. That is what makes
    the reconciliation in `run_once` idempotent — it compares this against the
    exchange and moves the difference, rather than acting on events it might
    process twice.
    """
    ladder = plan.grid_prices
    occupied = set(occupied_levels(plan, price, units))
    want: dict[str, Order] = {}

    # --- sells: above the mark, lowest first, until the balance is used up ---
    # Above the mark ONLY. Resting a sell below the current price would fill
    # immediately as a taker, which is rule 3.
    remaining = units
    for i, level in enumerate(ladder):
        if level <= price or remaining <= DUST:
            continue
        size = min(plan.quote_per_level / level, remaining)
        if size <= DUST:
            continue
        cid = client_order_id(plan.id, i, SELL)
        want[cid] = Order(client_order_id=cid, symbol=plan.symbol, side=SELL,
                          price=level, size=size)
        remaining -= size

    # --- buys: below the mark, on unoccupied levels, nearest first ----------
    # Budget is the plan's cap less what is already deployed, marked at the
    # current price. `max_quote_total` is a hard ceiling on the whole grid, not
    # a per-run allowance.
    budget = plan.max_quote_total - units * price
    for i in range(len(ladder) - 1, -1, -1):
        level = ladder[i]
        if level >= price or i in occupied:
            continue
        if budget < plan.quote_per_level:
            break
        size = plan.quote_per_level / level
        cid = client_order_id(plan.id, i, BUY)
        want[cid] = Order(client_order_id=cid, symbol=plan.symbol, side=BUY,
                          price=level, size=size)
        budget -= plan.quote_per_level

    return want


def cancel_plan_orders(plan: GridPlan, exchange: Exchange) -> list[str]:
    """Cancel every open order belonging to this plan, and nothing else."""
    mine = plan_order_ids(plan)
    cancelled = []
    for o in exchange.fetch_open_orders(plan.symbol):
        if o.client_order_id in mine:
            exchange.cancel(plan.symbol, o)
            cancelled.append(o.client_order_id)
    return cancelled


# --------------------------------------------------------------------- run

def _event(store: Optional[Any], kind: str, title: str, **kw: Any) -> None:
    """Persist one event. Best effort — a full disk must not stop a stop-out."""
    if store is None:
        return
    try:
        store.record_event(kind, title, **kw)
    except Exception:
        log.warning("could not record event %r", title, exc_info=True)


def _record_fills(plan: GridPlan, exchange: Exchange, q: Any,
                  fees_maker: float, store: Optional[Any] = None) -> None:
    """Sample execution quality from any fills the exchange will report.

    Best effort and silent on failure: an exchange with no fill history simply
    contributes no samples. Inventing them would defeat the point, which is
    that these numbers are measured rather than assumed.
    """
    fetch = getattr(exchange, "fetch_fills", None)
    if fetch is None:
        return
    seen = q.seen_fills if isinstance(getattr(q, "seen_fills", None), dict) else {}
    start = int(seen.get(plan.symbol, 0))
    try:
        rows = fetch(plan.symbol, start)
    except Exception:
        return
    for r in rows:
        price = float(r.get("price", 0.0) or 0.0)
        size = float(r.get("size", 0.0) or 0.0)
        if price <= 0 or size <= 0:
            continue
        predicted = price * size * fees_maker
        is_paper = not bool(getattr(exchange, "is_live", False))
        q.record_fill(execution.Fill(
            symbol=plan.symbol, side=str(r.get("side", "")),
            intended_price=price, actual_price=price, size=size,
            fee_paid=predicted, fee_predicted=predicted,
            is_paper=is_paper))
        if store is not None:
            try:
                new_id = store.record_transaction(
                    plan_id=plan.id, symbol=plan.symbol,
                    mode=gridstore.PAPER if is_paper else gridstore.LIVE,
                    side=str(r.get("side", "")), price=price, qty=size,
                    fee=predicted, level_price=price,
                    client_order_id=str(r.get("client_order_id", "")))
                if new_id is not None:
                    store.record_event(
                        gridstore.FILL,
                        f"{plan.symbol} {r.get('side','')} {size:g} @ {price:g}",
                        symbol=plan.symbol, plan_id=plan.id)
            except Exception:
                log.warning("could not record transaction", exc_info=True)
    seen[plan.symbol] = start + len(rows)
    q.seen_fills = seen


def run_once(plan: GridPlan, exchange: Exchange, control: Control, *,
             state_dir: Path = STATE_DIR, plans_dir: Optional[Path] = None,
             notify: Callable[[str], None] = _notify,
             quality: Optional[Any] = None,
             quality_path: Optional[Path] = None,
             store: Optional[Any] = None,
             held: str = "") -> GridState:
    """One pass. Safe to call repeatedly; safe to call after a crash.

        >>> run_once(plan, PaperExchange(), Control())

    `held` is the reason `admit` gave for holding this grid back, if any: the
    stop is still enforced, but no order is placed and resting ones are
    cancelled.

    Returns the snapshot it wrote, which is also what the UI reads.
    """
    # Execution quality is the signal at small capital; see execution.py.
    q = quality if quality is not None else execution.load(
        quality_path or execution.EXECUTION_PATH)
    q.record_pass(ok=True)

    state = GridState(plan_id=plan.id, symbol=plan.symbol,
                      status=plan.status.value, live=bool(exchange.is_live))

    # --- 0. paper unless explicitly live --------------------------------
    # Checked before anything else touches the exchange. A live client with a
    # control file that says paper is a configuration error, and the safe
    # reading of a configuration error is "do nothing".
    if exchange.is_live and not control.is_live():
        state.halted = True
        state.note = ("refusing to use a live exchange while control.json says "
                      "paper — set live_trading deliberately, or pass a "
                      "PaperExchange")
        log.error("%s: %s", plan.symbol, state.note)
        state.save(state_dir)
        return state

    # --- 1. am I allowed to trade this at all? --------------------------
    allowed, why = control.may_trade(plan.symbol, plan.id)
    if not allowed:
        state.halted = True
        state.note = why
        state.cancelled = cancel_plan_orders(plan, exchange)
        log.info("%s halted (%s); cancelled %d order(s)",
                 plan.symbol, why, len(state.cancelled))
        state.save(state_dir)
        return state

    if not plan.is_live:
        state.note = f"plan is {plan.status.value}, not runnable"
        state.cancelled = cancel_plan_orders(plan, exchange)
        state.save(state_dir)
        return state

    # --- 1b. the plan must still be valid -------------------------------
    # README: "a plan that fails cannot run even if approved". Approval
    # validates, but approval is not the last word — a plan is a JSON file I
    # can edit by hand, which is a feature of `plan.py`, and
    # a file edited into a state that gives half the gross to the exchange must
    # not be executed just because it was sound when it was signed off. Checked
    # every pass, not once, because the file can change between passes.
    #
    # Treated like a halt: stop adding to the position, cancel the resting
    # ladder, do NOT liquidate. Stopping is not selling, exactly as in
    # `control.halt_all`.
    problems = plan.validate()
    if problems:
        state.halted = True
        state.note = "plan fails validation: " + "; ".join(problems)
        state.cancelled = cancel_plan_orders(plan, exchange)
        log.error("%s: refusing to run an invalid plan — %s", plan.symbol,
                  state.note)
        notify(f"⛔ {plan.symbol}: refusing to run an invalid plan.\n"
               + "\n".join(problems)
               + "\nOrders cancelled; the position was NOT sold.")
        state.save(state_dir)
        return state

    # --- 2. reconstruct from the exchange -------------------------------
    open_orders = exchange.fetch_open_orders(plan.symbol)
    balance = exchange.fetch_balance(plan.symbol)
    units = max(0.0, balance.base)
    price = float(exchange.fetch_price(plan.symbol))
    state.price, state.units_held = price, units
    state.quote_deployed = units * price

    # --- 3. the stop — the only place a market order is allowed ---------
    if price <= plan.stop_below:
        state.cancelled = cancel_plan_orders(plan, exchange)
        sold = None
        if units > DUST:
            sold = exchange.market_sell(plan.symbol, units)
        if plan.status is not Status.STOPPED_OUT:
            plan.stop(Status.STOPPED_OUT)
            if plans_dir is not None:
                plan.save(plans_dir)
        state.stopped_out = True
        state.status = plan.status.value
        state.units_held = 0.0
        state.quote_deployed = 0.0
        state.note = (f"stop hit: {price:g} at or below {plan.stop_below:g}; "
                      f"liquidated {units:g} units")
        log.warning("%s STOPPED OUT at %g (stop %g)", plan.symbol, price,
                    plan.stop_below)
        _event(store, gridstore.STOP_OUT,
               f"{plan.symbol} stopped out at {price:g}",
               body=state.note, symbol=plan.symbol, plan_id=plan.id,
               severity="critical")
        notify(f"⛔ {plan.symbol} stopped out at {price:g} "
               f"(stop {plan.stop_below:g}). "
               f"{'Sold ' + format(units, 'g') + ' units. ' if sold else ''}"
               f"Grid halted; it will not restart on its own.")
        state.save(state_dir)
        return state

    # --- 3b. the caps in control.json -----------------------------------
    # After the stop, so a held grid that already holds stock is still
    # protected. Like a halt: no new orders, resting ones cancelled, nothing
    # sold.
    if held:
        state.halted = True
        state.note = held
        state.cancelled = cancel_plan_orders(plan, exchange)
        log.warning("%s: %s", plan.symbol, held)
        state.save(state_dir)
        return state

    # --- 4. reconcile the ladder ----------------------------------------
    want = desired_orders(plan, price, units)
    mine = plan_order_ids(plan)
    resting = {o.client_order_id: o for o in open_orders
               if o.client_order_id in mine}

    for cid, o in resting.items():
        if cid not in want:
            exchange.cancel(plan.symbol, o)
            state.cancelled.append(cid)
            q.record_cancelled()

    for cid, o in want.items():
        if cid in resting:
            continue                 # already there: leave it exactly alone
        try:
            exchange.place_limit(plan.symbol, o.side, o.price, o.size, cid)
            state.placed.append(cid)
            q.record_placed()
        except DuplicateOrder:
            # The exchange knows about an order this pass did not see — a
            # response that was lost in transit, most likely. Not placing it
            # twice is the entire point of the deterministic id.
            log.info("%s: %s already resting, leaving it", plan.symbol, cid)
        except CrossedSpread as e:
            # By construction `desired_orders` never prices through the mark, so
            # reaching here means the mark moved mid-pass or the ladder is wrong.
            # Either way orders are not going on, and I need to know about it.
            log.warning("%s: refusing to cross the spread: %s", plan.symbol, e)
            state.rejected.append(cid)
            notify(f"⚠️ {plan.symbol}: order rejected — {e}")
        except Exception as e:
            # The exchange said no: insufficient balance, below the minimum
            # size, instrument suspended. A grid quietly failing to place its
            # orders looks identical to a quiet market, so it is announced.
            log.error("%s: order %s rejected: %s: %s", plan.symbol, cid,
                      type(e).__name__, e)
            state.rejected.append(cid)
            _event(store, gridstore.ORDER_REJECTED,
                   f"{plan.symbol}: order rejected by the exchange",
                   body=f"{type(e).__name__}: {e}", symbol=plan.symbol,
                   plan_id=plan.id, severity="warning")
            notify(f"⚠️ {plan.symbol}: order rejected by the exchange — "
                   f"{type(e).__name__}: {e}")

    still_open = [o for o in exchange.fetch_open_orders(plan.symbol)
                  if o.client_order_id in mine]
    state.open_buys = sum(1 for o in still_open if o.side == BUY)
    state.open_sells = sum(1 for o in still_open if o.side == SELL)
    state.occupied_levels = occupied_levels(plan, price, units)

    if plan.status is Status.APPROVED:
        plan.status = Status.RUNNING
        if plans_dir is not None:
            plan.save(plans_dir)
    state.status = plan.status.value

    # --- 5. execution quality, then the snapshot ------------------------
    # The rate the PLAN assumed — fee accuracy is exactly the
    # question of whether that prediction held.
    _record_fills(plan, exchange, q, plan.maker_fee or 0.0008, store)
    q.save(quality_path or execution.EXECUTION_PATH)

    state.save(state_dir)
    log.info("%s @ %g — %d buys, %d sells resting (placed %d, cancelled %d)",
             plan.symbol, price, state.open_buys, state.open_sells,
             len(state.placed), len(state.cancelled))
    return state
