"""The mandate: the rulebook every other part of the council reads.

Loaded strictly. An unknown key, a missing limit or weights that add up to
more than 100% raise, so a typo in the YAML stops the council instead of
silently loosening a limit. Only the principal edits the file.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

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

    @model_validator(mode="after")
    def _order(self) -> "Limits":
        if self.min_position > self.max_position:
            raise ValueError("min_position is above max_position")
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
    limits: Limits
    instruments: list[Instrument]
    watchlist: list[WatchItem] = []
    rejected: list[Rejected] = []

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
        tickers = [i.ticker for i in self.instruments] + [w.ticker for w in self.watchlist]
        if len(tickers) != len(set(tickers)):
            raise ValueError("a ticker appears twice")
        return self

    # ------------------------------------------------------------- lookups

    def target(self, ticker: str) -> float:
        i = self.instrument(ticker)
        return i.weights.get(self.setting) if i else 0.0

    def instrument(self, ticker: str) -> Optional[Instrument]:
        return next((i for i in self.instruments if i.ticker == ticker), None)

    def watch(self, ticker: str) -> Optional[WatchItem]:
        return next((w for w in self.watchlist if w.ticker == ticker), None)

    def rejection(self, ticker: str) -> Optional[Rejected]:
        return next((r for r in self.rejected if r.ticker == ticker), None)

    def universe(self) -> list[str]:
        """Every ticker the council may hold a meeting about."""
        return [i.ticker for i in self.instruments] + [w.ticker for w in self.watchlist]

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


def initialise(overwrite: bool = False) -> Path:
    """Copy the example to the private, git-ignored mandate file."""
    target = paths.mandate_path()
    if overwrite or not target.exists():
        shutil.copy(paths.EXAMPLE_MANDATE, target)
    return target
