"""The data steward: every number the council uses, with its source and time.

Agents never produce a price or a multiple. They may only cite the fact sheet
this module builds, so an invented P/E has nowhere to come from. Every quote
carries `source` and `as_of`, and `is_fresh` decides whether it is recent
enough to trade on.

Two sources:
  live      Yahoo Finance through `yfinance` (no key), cached for a few minutes
  snapshot  a saved file of real quotes, for tests and the offline demo
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from . import paths

CACHE_SECONDS = 15 * 60
FRESH_SECONDS = 3 * 24 * 3600        # weekends: Friday's close is still usable

FX_PAIRS = {"USD": "EURUSD=X", "JPY": "EURJPY=X", "DKK": "EURDKK=X",
            "CHF": "EURCHF=X", "GBP": "EURGBP=X"}


@dataclass
class Quote:
    ticker: str
    name: str
    currency: str
    price: float                     # in the listing currency
    price_eur: float
    forward_pe: Optional[float] = None
    trailing_pe: Optional[float] = None
    high_52w: Optional[float] = None
    low_52w: Optional[float] = None
    source: str = "Yahoo Finance"
    as_of: str = ""                  # ISO time the number was fetched
    prev_close: Optional[float] = None   # yesterday's close, listing currency

    @property
    def change_1d(self) -> Optional[float]:
        """Today's move against yesterday's close (-0.12 = down 12%)."""
        if not self.prev_close:
            return None
        return self.price / self.prev_close - 1.0

    @property
    def from_high(self) -> Optional[float]:
        """How far below its 52-week high, as a fraction (0.27 = 27% below)."""
        if not self.high_52w:
            return None
        return 1.0 - self.price / self.high_52w

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        try:
            then = datetime.fromisoformat(self.as_of)
        except ValueError:
            return float("inf")
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - then).total_seconds())

    def is_fresh(self, now: Optional[datetime] = None) -> bool:
        return self.age_seconds(now) <= FRESH_SECONDS


class DataError(RuntimeError):
    """A number the council needs could not be obtained."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _num(x) -> Optional[float]:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if f == f and f > 0 else None      # drop NaN, zero, negatives


class Steward:
    """Fetches and caches quotes. `snapshot=True` reads the saved file only."""

    def __init__(self, snapshot: bool = False, snapshot_path: Optional[Path] = None,
                 cache_path: Optional[Path] = None):
        self.snapshot = snapshot
        self.snapshot_path = Path(snapshot_path or paths.SNAPSHOT)
        self.cache_path = cache_path
        self._cache: dict[str, tuple[float, Quote]] = {}
        self._fx: dict[str, tuple[float, float]] = {}
        self.saved_at = ""
        self._news: dict[str, tuple[float, list[dict]]] = {}
        if snapshot:
            self._load_snapshot()

    @property
    def mode(self) -> str:
        return "snapshot" if self.snapshot else "live"

    # ------------------------------------------------------------- snapshot

    def _load_snapshot(self) -> None:
        raw = json.loads(self.snapshot_path.read_text())
        self.saved_at = raw.get("saved_at", "")
        far = float("inf")
        for q in raw["quotes"]:
            self._cache[q["ticker"]] = (far, Quote(**q))
        for ccy, rate in raw.get("fx", {}).items():
            self._fx[ccy] = (far, float(rate))

    def save_snapshot(self, tickers: Iterable[str], path: Optional[Path] = None) -> Path:
        quotes = self.quotes(tickers)
        path = Path(path or self.snapshot_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "note": "Real Yahoo Finance quotes saved for the offline demo and tests.",
            "saved_at": _now(),
            "fx": {c: r for c, (_, r) in self._fx.items()},
            "quotes": [asdict(q) for q in quotes.values()],
        }
        path.write_text(json.dumps(payload, indent=2))
        return path

    # ---------------------------------------------------------------- live

    def fx(self, currency: str) -> float:
        """Units of `currency` per euro."""
        if currency == "EUR":
            return 1.0
        hit = self._fx.get(currency)
        if hit and (self.snapshot or time.time() - hit[0] < CACHE_SECONDS):
            return hit[1]
        if self.snapshot:
            raise DataError(f"no {currency} rate in the snapshot")
        pair = FX_PAIRS.get(currency) or f"EUR{currency}=X"     # e.g. EURTWD=X for TSMC's statements
        import yfinance as yf
        rate = _num(yf.Ticker(pair).fast_info.get("lastPrice"))
        if not rate:
            raise DataError(f"no rate for {pair}")
        self._fx[currency] = (time.time(), rate)
        return rate

    def _fetch(self, ticker: str) -> Quote:
        import yfinance as yf
        tk = yf.Ticker(ticker)
        fi = tk.fast_info
        price = _num(fi.get("lastPrice"))
        currency = (fi.get("currency") or "").upper()
        if not price or not currency:
            raise DataError(f"no price for {ticker}")
        try:
            info = tk.info or {}
        except Exception:
            info = {}
        rate = self.fx(currency)
        return Quote(
            ticker=ticker, name=str(info.get("shortName") or ticker).strip(),
            currency=currency, price=price, price_eur=price / rate,
            forward_pe=_num(info.get("forwardPE")),
            trailing_pe=_num(info.get("trailingPE")),
            high_52w=_num(fi.get("yearHigh")), low_52w=_num(fi.get("yearLow")),
            prev_close=_num(fi.get("previousClose")), as_of=_now())

    def quote(self, ticker: str) -> Quote:
        return self.quotes([ticker])[ticker]

    def quotes(self, tickers: Iterable[str]) -> dict[str, Quote]:
        tickers = list(dict.fromkeys(tickers))
        out: dict[str, Quote] = {}
        missing = []
        for t in tickers:
            hit = self._cache.get(t)
            if hit and (self.snapshot or time.time() - hit[0] < CACHE_SECONDS):
                out[t] = hit[1]
            else:
                missing.append(t)
        if missing and self.snapshot:
            raise DataError(f"not in the snapshot: {', '.join(missing)}")
        if missing:
            # FX first, once, so the threads do not race for it.
            with ThreadPoolExecutor(max_workers=8) as ex:
                results = list(ex.map(self._safe_fetch, missing))
            errors = []
            for t, q in zip(missing, results):
                if isinstance(q, Quote):
                    self._cache[t] = (time.time(), q)
                    out[t] = q
                else:
                    errors.append(f"{t}: {q}")
            if errors:
                raise DataError("could not price " + "; ".join(errors))
        return out

    def news(self, ticker: str, limit: int = 8) -> list[dict]:
        """Recent headlines from Yahoo Finance: title, source, date, link.

        Headlines are other people's words: shown as data and given to the
        council as data, never as instructions. Empty offline or on error,
        so a missing feed never stops a page or a meeting.
        """
        if self.snapshot:
            return []
        hit = self._news.get(ticker)
        if hit and time.time() - hit[0] < 30 * 60:
            return hit[1][:limit]
        out: list[dict] = []
        try:
            import yfinance as yf
            for x in yf.Ticker(ticker).news or []:
                c = x.get("content", x) if isinstance(x, dict) else {}
                url = ((c.get("canonicalUrl") or {}).get("url")
                       or (c.get("clickThroughUrl") or {}).get("url") or "")
                title = (c.get("title") or "").strip()
                if not title or not url.startswith("http"):
                    continue
                out.append({"title": title, "url": url, "date": c.get("pubDate") or "",
                            "source": (c.get("provider") or {}).get("displayName") or ""})
        except Exception:
            out = []
        out.sort(key=lambda n: n["date"], reverse=True)
        self._news[ticker] = (time.time(), out)
        return out[:limit]

    def earnings_dates(self, ticker: str) -> list[str]:
        """Recent and upcoming results dates (YYYY-MM-DD), newest first.
        Empty offline or on error: a missing calendar never stops the run."""
        if self.snapshot:
            return []
        try:
            import yfinance as yf
            df = yf.Ticker(ticker).get_earnings_dates(limit=8)
            if df is None:
                return []
            days = sorted({str(ix)[:10] for ix in df.index}, reverse=True)
            return [d for d in days if len(d) == 10]
        except Exception:
            return []

    def last_update(self) -> str:
        """When the newest number in hand was fetched (ISO), for the header."""
        if self.snapshot:
            return self.saved_at
        stamps = [q.as_of for _, q in self._cache.values() if q.as_of]
        return max(stamps) if stamps else ""

    def _safe_fetch(self, ticker: str):
        try:
            return self._fetch(ticker)
        except Exception as e:          # reported together by `quotes`
            return f"{type(e).__name__}: {e}"
