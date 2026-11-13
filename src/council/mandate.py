"""The mandate: the rulebook every other part of the council reads.

Loaded strictly. An unknown key, a missing limit or weights that add up to
more than 100% raise, so a typo in the YAML stops the council instead of
silently loosening a limit. Only the principal edits the file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from . import paths

Setting = Literal["conservative", "growth"]
Bucket = Literal["index", "bonds", "gold", "stock"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PerSetting(Strict):
    conservative: float
    growth: float

    def get(self, setting: Setting) -> float:
        return getattr(self, setting)


class Limits(Strict):
    min_position: float = Field(gt=0, lt=1)
    max_position: float = Field(gt=0, lt=1)
    max_single_stocks: int = Field(gt=0)
    taiwan_cap: float = Field(gt=0, lt=1)
    ai_cap: PerSetting
    min_cash: float = Field(ge=0, lt=1)
    cash_target: PerSetting
    rebalance_band: float = Field(gt=0, lt=1)
    tranches: int = Field(ge=1, le=6)
    tranche_spacing_days: int = Field(ge=1)
    max_meetings_per_month: int = Field(ge=1)
    max_loss: float = Field(gt=0, lt=1)
    meeting_budget_usd: float = Field(gt=0)
    watchlist_deadline: str
    proposal_days: int = Field(default=7, ge=1)          # an unanswered proposal expires
    max_holding_reviews_per_month: int = Field(default=4, ge=0)
    review_holdings_every_days: int = Field(default=90, ge=7)
    max_postmortems_per_month: int = Field(default=2, ge=0)
    # The staged council: scout, research files, watch, portfolio meeting.
    screen_every_days: int = Field(default=7, ge=1)       # the scout's weekly look
    max_new_ideas: int = Field(default=3, ge=0)           # per screening
    max_research_files_per_week: int = Field(default=4, ge=0)
    research_refresh_days: int = Field(default=90, ge=7)  # a file older than this is rewritten
    big_drop: float = Field(default=0.10, gt=0, lt=1)     # a one-day fall that triggers a check
    max_urgent_reviews_per_day: int = Field(default=1, ge=0)
    monthly_ai_budget_usd: float = Field(default=30.0, ge=0)   # all AI stages together, per book

    @model_validator(mode="after")
    def _order(self) -> "Limits":
        if self.min_position > self.max_position:
            raise ValueError("min_position is above max_position")
        return self


class BrokerConfig(Strict):
    """Where approved orders go. Paper unless deliberately changed."""
    kind: Literal["paper", "ibkr"] = "paper"
    host: str = "127.0.0.1"
    port: int = 4002                 # IB Gateway paper; 4001 is the live gateway
    client_id: int = 17
    live: bool = False               # True only with the confirmation below
    confirm: str = ""                # must read "I ACCEPT THE RISK" for live money

    @model_validator(mode="after")
    def _live(self) -> "BrokerConfig":
        if self.live and self.confirm != "I ACCEPT THE RISK":
            raise ValueError("broker.live needs confirm: 'I ACCEPT THE RISK'")
        return self


class Instrument(Strict):
    ticker: str
    name: str
    bucket: Bucket
    weights: PerSetting
    theme: str = ""                  # two or three words, e.g. "chip film"
    ai: bool = False
    taiwan: bool = False
    grade: Optional[str] = None
    thesis: str = ""
    trigger: str = ""
    entry_price_eur: Optional[float] = None
    breakers: list[str] = []
    ibkr: Optional[str] = None       # "SYMBOL:EXCHANGE:CURRENCY" if the default guess is wrong


class WatchItem(Strict):
    ticker: str
    name: str
    theme: str = ""
    grade: str = "not_graded"
    thesis: str = ""
    trigger_pe: Optional[float] = None
    trigger_price_eur: Optional[float] = None
    event: str = ""
    ai: bool = False
    taiwan: bool = False
    ibkr: Optional[str] = None


class Rejected(Strict):
    ticker: str
    name: str
    reason: str


class Mandate(Strict):
    name: str
    base_currency: Literal["EUR"]
    start_capital_eur: float = Field(gt=0)
    setting: Setting
    benchmark: str
    fee_per_order_eur: float = Field(ge=0)
    broker: BrokerConfig = BrokerConfig()
    # Autopilot: the council approves its own proposals. The risk engine still
    # re-checks every rule; only your approval step is skipped. Paper only.
    autopilot: bool = False
    # Rules only: the comparison book. No AI at all: members restate the
    # committee's notes and code applies the triggers. It approves itself.
    rules_only: bool = False
    limits: Limits
    instruments: list[Instrument]
    watchlist: list[WatchItem] = []
    rejected: list[Rejected] = []
    # The scout's shortlist, read from the store each time: names a research
    # file sent to the committee. Not part of the YAML; see `use_shortlist`.
    _shortlist: Optional[Callable[[], list[WatchItem]]] = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _check(self) -> "Mandate":
        for s in ("conservative", "growth"):
            total = sum(i.weights.get(s) for i in self.instruments)
            total += self.limits.cash_target.get(s)
            if total > 1.0 + 1e-9:
                raise ValueError(f"{s} weights plus cash add up to {total:.0%}")
            stocks = [i for i in self.instruments
                      if i.bucket == "stock" and i.weights.get(s) > 0]
            if len(stocks) > self.limits.max_single_stocks:
                raise ValueError(f"{s} holds {len(stocks)} single stocks")
        if self.autopilot and self.broker.kind != "paper":
            raise ValueError("autopilot trades paper money only: set broker.kind to paper")
        if self.rules_only and not self.autopilot:
            raise ValueError("the rules-only book approves itself: set autopilot: true")
        tickers = [i.ticker for i in self.instruments] + [w.ticker for w in self.watchlist]
        if len(tickers) != len(set(tickers)):
            raise ValueError("a ticker appears twice")
        return self

    # ------------------------------------------------------------- lookups

    def use_shortlist(self, provider: Callable[[], list[WatchItem]]) -> "Mandate":
        """Let the scout's shortlisted names into the universe. They are
        debated like watch-list names; every buy still passes the risk engine
        and waits for your OK."""
        self._shortlist = provider
        return self

    def shortlist(self) -> list[WatchItem]:
        if not self._shortlist:
            return []
        own = {w.ticker for w in self.watchlist} | {i.ticker for i in self.instruments} \
            | {r.ticker for r in self.rejected}
        return [w for w in self._shortlist() if w.ticker not in own]

    def all_watch(self) -> list[WatchItem]:
        return list(self.watchlist) + self.shortlist()

    def target(self, ticker: str) -> float:
        i = self.instrument(ticker)
        return i.weights.get(self.setting) if i else 0.0

    def instrument(self, ticker: str) -> Optional[Instrument]:
        return next((i for i in self.instruments if i.ticker == ticker), None)

    def watch(self, ticker: str) -> Optional[WatchItem]:
        return next((w for w in self.all_watch() if w.ticker == ticker), None)

    def rejection(self, ticker: str) -> Optional[Rejected]:
        return next((r for r in self.rejected if r.ticker == ticker), None)

    def universe(self) -> list[str]:
        """Every ticker the council may hold a meeting about."""
        return [i.ticker for i in self.instruments] + [w.ticker for w in self.all_watch()]

    def is_shortlisted(self, ticker: str) -> bool:
        return any(w.ticker == ticker for w in self.shortlist())

    def theme_of(self, ticker: str) -> str:
        x = self.instrument(ticker) or self.watch(ticker)
        return x.theme if x else ""

    def name_of(self, ticker: str) -> str:
        x = self.instrument(ticker) or self.watch(ticker) or self.rejection(ticker)
        return x.name if x else ticker

    def is_ai(self, ticker: str) -> bool:
        x = self.instrument(ticker) or self.watch(ticker)
        return bool(x and x.ai)

    def is_taiwan(self, ticker: str) -> bool:
        x = self.instrument(ticker) or self.watch(ticker)
        return bool(x and x.taiwan)

    def is_stock(self, ticker: str) -> bool:
        i = self.instrument(ticker)
        return i is None or i.bucket == "stock"     # watch-list names are stocks

    def reserve_target(self) -> float:
        used = sum(i.weights.get(self.setting) for i in self.instruments)
        return max(0.0, 1.0 - used - self.limits.cash_target.get(self.setting))


def load(path: Optional[Path] = None) -> Mandate:
    path = Path(path) if path else paths.mandate_path()
    if not path.exists():
        path = paths.EXAMPLE_MANDATE
    return Mandate.model_validate(yaml.safe_load(path.read_text()))


def initialise(overwrite: bool = False, book: str = "") -> Path:
    """Copy the example to the private, git-ignored mandate file. The Autopilot
    book gets the same rules with `autopilot: true`."""
    book = book or paths.book()
    target = paths.mandate_path(book)
    if overwrite or not target.exists():
        text = paths.EXAMPLE_MANDATE.read_text()
        if book == "autopilot":
            text += ("\n# This book approves its own proposals. Paper money only.\n"
                     "autopilot: true\n")
        elif book == "rules":
            text += ("\n# The comparison book: no AI, the committee's rules only. Approves itself.\n"
                     "autopilot: true\nrules_only: true\n")
        target.write_text(text)
    return target
