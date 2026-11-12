"""Where an approved order goes.

    PaperBroker   fills at the last real price, with the mandate's flat fee.
                  The default, and the only broker that has been run.
    IBKRBroker    limit orders to Interactive Brokers through IB Gateway on
                  this Mac (`ib_async`). Built and tested against a fake;
                  NOT yet run against a real IBKR account. Use an IBKR paper
                  account (Gateway port 4002) first. Real money needs
                  broker.live: true plus the confirmation phrase in the
                  mandate, and every order still needs your approval.

Nothing here decides anything: a broker is only called by `approvals.py`
after you approve, or for a tranche of a plan you already approved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from .data import Quote
from .mandate import BrokerConfig, Mandate


class BrokerError(RuntimeError):
    """The broker could not place or fill an order. Nothing was recorded."""


@dataclass
class Fill:
    qty: float
    price: float            # listing currency
    currency: str
    price_eur: float
    fee_eur: float
    broker: str
    order_id: str = ""


class Broker(Protocol):
    name: str
    is_live: bool

    def execute(self, ticker: str, side: str, qty: float, quote: Quote) -> Fill: ...


class PaperBroker:
    name = "paper"
    is_live = False

    def __init__(self, fee_eur: float):
        self.fee_eur = fee_eur

    def execute(self, ticker: str, side: str, qty: float, quote: Quote) -> Fill:
        if qty <= 0:
            raise BrokerError("quantity must be positive")
        return Fill(qty=qty, price=quote.price, currency=quote.currency,
                    price_eur=quote.price_eur, fee_eur=self.fee_eur, broker=self.name)


# Yahoo suffix -> IBKR exchange and currency. Override per name with `ibkr:`
# in the mandate ("SYMBOL:EXCHANGE:CURRENCY") when this guess is wrong.
IBKR_VENUES = {".AS": ("AEB", "EUR"), ".MI": ("BVME", "EUR"), ".DE": ("IBIS", "EUR"),
               ".PA": ("SBF", "EUR"), ".HE": ("HEX", "EUR"), ".CO": ("CPH", "DKK"),
               ".T": ("TSEJ", "JPY"), ".L": ("LSE", "GBP"), ".SW": ("EBS", "CHF")}


def ibkr_contract_spec(ticker: str, override: Optional[str] = None) -> tuple[str, str, str]:
    if override:
        sym, exch, ccy = override.split(":")
        return sym, exch, ccy
    for suffix, (exch, ccy) in IBKR_VENUES.items():
        if ticker.endswith(suffix):
            return ticker[: -len(suffix)], exch, ccy
    return ticker, "SMART", "USD"


class IBKRBroker:
    """Limit order at the last price, wait for the fill, record what filled."""

    name = "ibkr"

    def __init__(self, cfg: BrokerConfig, mandate: Mandate, ib=None, lib=None,
                 wait_seconds: float = 60):
        self.cfg, self.m = cfg, mandate
        self.is_live = cfg.live
        self.wait_seconds = wait_seconds
        self._ib = ib            # a connected client, or a fake in tests
        self._lib = lib          # the ib_async module, or a fake in tests

    def _client(self):
        if self._ib is not None:
            return self._ib
        ib = self._library().IB()
        try:
            ib.connect(self.cfg.host, self.cfg.port, clientId=self.cfg.client_id, timeout=10)
        except Exception as e:
            raise BrokerError(f"IB Gateway is not reachable on {self.cfg.host}:{self.cfg.port} "
                              f"({type(e).__name__}). Start it and log in first.") from e
        self._ib = ib
        return ib

    def _library(self):
        if self._lib is None:
            try:
                import ib_async
            except ImportError as e:
                raise BrokerError("ib_async is not installed: pip install ib_async") from e
            self._lib = ib_async
        return self._lib

    def execute(self, ticker: str, side: str, qty: float, quote: Quote) -> Fill:
        ib = self._client()
        mod = self._library()
        x = self.m.instrument(ticker) or self.m.watch(ticker)
        sym, exch, ccy = ibkr_contract_spec(ticker, getattr(x, "ibkr", None))
        if ccy != quote.currency:
            raise BrokerError(f"{ticker}: IBKR line trades in {ccy}, the quote is in "
                              f"{quote.currency}. Set `ibkr:` for it in the mandate.")
        contract = mod.Stock(sym, exch, ccy)
        order = mod.LimitOrder(side.upper(), round(qty, 4), round(quote.price, 4), tif="DAY")
        trade = ib.placeOrder(contract, order)
        waited = 0.0
        while not trade.isDone() and waited < self.wait_seconds:
            ib.sleep(1)
            waited += 1
        filled = float(trade.orderStatus.filled or 0)
        if filled <= 0:
            ib.cancelOrder(order)
            raise BrokerError(f"{ticker}: the limit order at {quote.price:,.2f} {ccy} did not fill "
                              f"within {self.wait_seconds:.0f}s and was cancelled")
        if not trade.isDone():
            ib.cancelOrder(order)                  # keep the part that filled
        price = float(trade.orderStatus.avgFillPrice or quote.price)
        fee = sum(float(getattr(f.commissionReport, "commission", 0) or 0) for f in trade.fills)
        rate = quote.price / quote.price_eur if quote.price_eur else 1.0
        return Fill(qty=filled, price=price, currency=ccy, price_eur=price / rate,
                    fee_eur=(fee / rate) if fee else self.m.fee_per_order_eur,
                    broker=self.name, order_id=str(getattr(trade.order, "orderId", "")))


def make_broker(m: Mandate) -> Broker:
    if m.broker.kind == "ibkr":
        return IBKRBroker(m.broker, m)
    return PaperBroker(m.fee_per_order_eur)
