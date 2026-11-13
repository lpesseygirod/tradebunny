"""The paper portfolio: real prices, real fees, no real money.

It starts from the committee report's model portfolio with a notional
capital, bought in three tranches a month apart (the report's entry plan),
and is valued every day against the benchmark: the same money put into the
MSCI World ETF on the first day. Beating that line, after fees, is the only
honest definition of the council making money.

Orders are limit orders at the last price. On paper that fills immediately;
the price, the euro amount and the flat fee are all recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import risk
from .broker import Broker, BrokerError, make_broker
from .data import DataError, Steward
from .mandate import Mandate
from .store import Store

DUST_EUR = 0.5


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


@dataclass
class Position:
    ticker: str
    name: str
    qty: float
    cost_eur: float           # what was paid, fees included
    value_eur: float
    weight: float
    target: float
    price_eur: float

    @property
    def gain_eur(self) -> float:
        return self.value_eur - self.cost_eur

    @property
    def gain_pct(self) -> float:
        return self.value_eur / self.cost_eur - 1 if self.cost_eur else 0.0


class Portfolio:
    def __init__(self, store: Store, steward: Steward, mandate: Mandate,
                 broker: Optional[Broker] = None):
        self.store, self.steward, self.m = store, steward, mandate
        self.broker = broker or make_broker(mandate)

    # ------------------------------------------------------------- state

    @property
    def started(self) -> bool:
        return self.store.get_meta("started_on") is not None

    def quantities(self) -> dict[str, float]:
        q: dict[str, float] = {}
        for t in self.store.trades():
            q[t["ticker"]] = q.get(t["ticker"], 0.0) + (t["qty"] if t["side"] == "buy" else -t["qty"])
        return {k: v for k, v in q.items() if v > 1e-9}

    def cash(self) -> float:
        c = float(self.store.get_meta("capital_eur", self.m.start_capital_eur))
        for t in self.store.trades():
            if t["side"] == "buy":
                c -= t["amount_eur"] + t["fee_eur"]
            else:
                c += t["amount_eur"] - t["fee_eur"]
        return c

    def cost_basis(self) -> dict[str, float]:
        """Average cost of what is still held, fees included."""
        basis: dict[str, float] = {}
        qty: dict[str, float] = {}
        for t in self.store.trades():
            k = t["ticker"]
            if t["side"] == "buy":
                basis[k] = basis.get(k, 0.0) + t["amount_eur"] + t["fee_eur"]
                qty[k] = qty.get(k, 0.0) + t["qty"]
            elif qty.get(k):
                share = min(1.0, t["qty"] / qty[k])
                basis[k] *= 1 - share
                qty[k] -= t["qty"]
        return basis

    def tickers_needed(self) -> list[str]:
        return list(dict.fromkeys(list(self.quantities()) + self.m.universe() + [self.m.benchmark]))

    def book(self, quotes: Optional[dict] = None) -> risk.Book:
        quotes = quotes or self.steward.quotes(self.tickers_needed())
        holdings = {t: q * quotes[t].price_eur for t, q in self.quantities().items()}
        pending: dict[str, float] = {}
        for tr in self.store.tranches("planned"):
            pending[tr["ticker"]] = pending.get(tr["ticker"], 0.0) + tr["amount_eur"]
        peak = max((s["value_eur"] for s in self.store.snapshots()), default=0.0)
        return risk.Book(cash=self.cash(), holdings=holdings, pending=pending, peak_value=peak)

    def positions(self, quotes: Optional[dict] = None) -> list[Position]:
        quotes = quotes or self.steward.quotes(self.tickers_needed())
        book = self.book(quotes)
        basis = self.cost_basis()
        out = []
        for t, qty in self.quantities().items():
            value = book.holdings[t]
            out.append(Position(ticker=t, name=self.m.name_of(t), qty=qty,
                                cost_eur=basis.get(t, value), value_eur=value,
                                weight=value / book.total if book.total else 0.0,
                                target=self.m.target(t), price_eur=quotes[t].price_eur))
        return sorted(out, key=lambda p: -p.value_eur)

    def benchmark_value(self, quotes: Optional[dict] = None) -> float:
        start = self.store.get_meta("benchmark_start_eur")
        capital = float(self.store.get_meta("capital_eur", self.m.start_capital_eur))
        if not start:
            return capital
        quotes = quotes or self.steward.quotes([self.m.benchmark])
        return capital * quotes[self.m.benchmark].price_eur / float(start)

    # ------------------------------------------------------------ orders

    def buy(self, ticker: str, amount_eur: float, meeting_id: Optional[int] = None,
            note: str = "") -> Optional[dict]:
        if amount_eur < DUST_EUR:
            return None
        q = self.steward.quote(ticker)
        if amount_eur + self.m.fee_per_order_eur > self.cash() + 1e-6:
            raise DataError(f"not enough cash for {amount_eur:,.2f} EUR of {ticker}")
        f = self.broker.execute(ticker, "buy", round(amount_eur / q.price_eur, 6), q)
        trade = dict(ticker=ticker, side="buy", qty=f.qty, price=f.price, currency=f.currency,
                     price_eur=f.price_eur, fee_eur=f.fee_eur, amount_eur=f.qty * f.price_eur,
                     meeting_id=meeting_id, note=note or "limit order at the last price",
                     broker=f.broker)
        trade["id"] = self.store.add_trade(**trade)
        return trade

    def sell(self, ticker: str, amount_eur: Optional[float] = None,
             meeting_id: Optional[int] = None, note: str = "") -> Optional[dict]:
        held = self.quantities().get(ticker, 0.0)
        if held <= 0:
            return None
        q = self.steward.quote(ticker)
        qty = held if amount_eur is None else min(held, round(amount_eur / q.price_eur, 6))
        if qty * q.price_eur < DUST_EUR:
            return None
        f = self.broker.execute(ticker, "sell", qty, q)
        trade = dict(ticker=ticker, side="sell", qty=f.qty, price=f.price, currency=f.currency,
                     price_eur=f.price_eur, fee_eur=f.fee_eur, amount_eur=f.qty * f.price_eur,
                     meeting_id=meeting_id, note=note or "limit order at the last price",
                     broker=f.broker)
        trade["id"] = self.store.add_trade(**trade)
        return trade

    def plan(self, ticker: str, total_eur: float, meeting_id: Optional[int] = None,
             note: str = "", start: Optional[date] = None) -> list[dict]:
        """Buy the first tranche now; schedule the rest a month apart."""
        lim = self.m.limits
        part = total_eur / lim.tranches
        done = []
        t = self.buy(ticker, part, meeting_id, note=f"tranche 1 of {lim.tranches}. {note}".strip())
        if t:
            done.append(t)
        start = start or datetime.now(timezone.utc).date()
        for k in range(1, lim.tranches):
            due = (start + timedelta(days=lim.tranche_spacing_days * k)).isoformat()
            self.store.plan_tranche(ticker, due, part, meeting_id,
                                    note=f"tranche {k + 1} of {lim.tranches}")
        return done

    # ------------------------------------------------------- lifecycle

    def start(self, capital: Optional[float] = None) -> list[dict]:
        """Open the paper account and buy the model portfolio's first tranche."""
        if self.started:
            return []
        capital = capital or self.m.start_capital_eur
        quotes = self.steward.quotes(self.m.universe() + [self.m.benchmark])
        self.store.set_meta("capital_eur", capital)
        self.store.set_meta("started_on", today())
        self.store.set_meta("benchmark_start_eur", quotes[self.m.benchmark].price_eur)
        self.store.set_meta("setting", self.m.setting)
        trades = []
        for inst in self.m.instruments:
            w = inst.weights.get(self.m.setting)
            if w > 0:
                trades += self.plan(inst.ticker, w * capital,
                                    note="model portfolio from the committee report")
        self.snapshot(quotes)
        return trades

    def snapshot(self, quotes: Optional[dict] = None) -> dict:
        quotes = quotes or self.steward.quotes(self.tickers_needed())
        value = self.book(quotes).total
        bench = self.benchmark_value(quotes)
        self.store.snapshot(today(), value, bench)
        return {"value_eur": value, "benchmark_eur": bench}

    def review(self, on: Optional[str] = None) -> list[str]:
        """Housekeeping: tranches you already approved, triggers, today's value."""
        on = on or today()
        events: list[str] = []
        for tr in self.store.tranches("planned"):
            if tr["due"] > on:
                continue
            try:
                t = self.buy(tr["ticker"], tr["amount_eur"], tr["meeting_id"], note=tr["note"])
                self.store.set_tranche(tr["id"], "done")
                if t:
                    events.append(f"Bought {tr['note']} of {self.m.name_of(tr['ticker'])}: "
                                  f"{t['amount_eur']:,.2f} EUR")
            except (DataError, BrokerError) as e:
                events.append(f"Skipped a tranche of {tr['ticker']}: {e}")

        quotes = self.steward.quotes(self.tickers_needed())
        for w in self.watch_status(quotes):
            if w["hit"]:
                events.append(f"Watch-list trigger hit: {w['name']} ({w['why']}). "
                              f"Hold a meeting before buying.")
        snap = self.snapshot(quotes)
        events.append(f"Value {snap['value_eur']:,.2f} EUR vs benchmark "
                      f"{snap['benchmark_eur']:,.2f} EUR")
        return events

    def spare_reserve(self) -> float:
        """Reserve not promised to any tranche, above the cash target."""
        book = self.book()
        return book.free_cash() - self.m.limits.cash_target.get(self.m.setting) * book.total

    def watch_status(self, quotes: dict) -> list[dict]:
        out = []
        for w in self.m.all_watch():
            q = quotes.get(w.ticker)
            hit, why = False, w.event or "no price trigger"
            if q and w.trigger_pe and q.forward_pe:
                why = f"forward P/E {q.forward_pe:.1f}, trigger {w.trigger_pe:g}"
                hit = q.forward_pe <= w.trigger_pe
            if q and w.trigger_price_eur:
                hit_p = q.price_eur <= w.trigger_price_eur
                why += f"; price {q.price_eur:,.2f} EUR, trigger {w.trigger_price_eur:,.0f}"
                hit = hit or hit_p
            out.append({"ticker": w.ticker, "name": w.name, "grade": w.grade,
                        "thesis": w.thesis, "hit": hit, "why": why,
                        "event": w.event, "quote": q})
        for inst in self.m.instruments:
            q = quotes.get(inst.ticker)
            if inst.entry_price_eur and q:
                out.append({"ticker": inst.ticker, "name": inst.name, "grade": inst.grade or "",
                            "thesis": inst.thesis, "hit": q.price_eur <= inst.entry_price_eur,
                            "why": f"price {q.price_eur:,.2f} EUR, add near "
                                   f"{inst.entry_price_eur:,.0f}", "event": "", "quote": q})
        return out
