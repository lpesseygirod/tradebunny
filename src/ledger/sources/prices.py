"""Alpaca market data — used only to resolve predictions, never to generate them.

A free paper account gives you keys with no KYC. Data lives on a different host
from trading, which is the single most common setup mistake:
    data:    https://data.alpaca.markets
    trading: https://paper-api.alpaca.markets
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests

DATA_URL = "https://data.alpaca.markets/v2"


def _headers() -> dict[str, str]:
    key, secret = os.getenv("ALPACA_KEY_ID"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("ALPACA_KEY_ID and ALPACA_SECRET_KEY must be set in .env")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def daily_bars(symbol: str, start: str, end: str, feed: str = "iex") -> list[dict]:
    """Daily OHLCV bars, inclusive of both dates.

    The free tier serves the "iex" feed — a subset of consolidated volume. Good
    enough for scoring daily closes; not good enough for intraday work.
    """
    r = requests.get(
        f"{DATA_URL}/stocks/{symbol}/bars",
        headers=_headers(),
        # "all" includes DIVIDEND adjustments, not just splits. With "split" a stock
        # going ex-dividend inside a 10-day window shows a price drop equal to the
        # dividend, and the resolver scores that as a real loss — a false MISS. On a
        # high-yielder that is ~0.8pp of error, eight times the entire cost
        # adjustment and five times the kill-criteria threshold, and it is
        # directional: it penalises longs and flatters shorts.
        params={"start": start, "end": end, "timeframe": "1Day",
                "adjustment": "all", "feed": feed, "limit": 1000},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("bars") or []


def close_on_or_after(symbol: str, target: str, window_days: int = 7) -> Optional[tuple[str, float]]:
    """First available closing price on or after `target`.

    Returns (actual_date, close). Weekends and holidays are why this searches
    forward instead of demanding an exact date.
    """
    end = (date.fromisoformat(target) + timedelta(days=window_days)).isoformat()
    bars = daily_bars(symbol, target, end)
    if not bars:
        return None
    first = bars[0]
    return first["t"][:10], float(first["c"])


def pair_prices(ticker: str, benchmark: str, entry_date: str, exit_date: str
                ) -> Optional[dict[str, float]]:
    """Fetch the four prices needed to score one prediction.

    Returns None if any leg is missing, so a partial fetch can never produce a
    half-real score — an unresolvable prediction stays open rather than being
    scored on guessed data.
    """
    legs = {}
    for label, sym, day in (("entry", ticker, entry_date), ("exit", ticker, exit_date),
                            ("bench_entry", benchmark, entry_date),
                            ("bench_exit", benchmark, exit_date)):
        got = close_on_or_after(sym, day)
        if got is None:
            return None
        legs[label] = got[1]
        legs[f"{label}_date"] = got[0]

    # Each leg searches forward independently, so a halted ticker can resolve to a
    # later session than its benchmark. Scoring that would measure a date mismatch
    # rather than relative performance, so refuse and leave the prediction open.
    if (legs["entry_date"] != legs["bench_entry_date"]
            or legs["exit_date"] != legs["bench_exit_date"]):
        return None
    return legs


def check() -> dict:
    """Smoke test used by `cli check`."""
    try:
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=10)
        bars = daily_bars("SPY", start.isoformat(), end.isoformat())
        return {"ok": bool(bars), "n_bars": len(bars),
                "last_close": bars[-1]["c"] if bars else None}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
