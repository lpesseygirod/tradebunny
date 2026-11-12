"""News ingestion — the 'search the internet' half of the morning run.

Alpaca's news endpoint is the primary source because you already have the keys for
price data, so it costs nothing extra. It carries a `symbols` field on every article,
which matters more than it looks: that list is what `rules.py` uses to reject
first-order predictions. Without knowing what an article is ABOUT, the whole
"never predict the article's subject" rule cannot be enforced.

Endpoint: GET https://data.alpaca.markets/v1beta1/news
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

NEWS_URL = "https://data.alpaca.markets/v1beta1/news"

# Headlines that are the market reacting, not the market being told something new.
# Trading these means chasing a move that already happened.
REACTIVE = (
    "shares jump", "shares fall", "stock surges", "stock plunges", "stock slides",
    "shares rise", "shares drop", "tumbles", "soars", "rallies", "sinks",
    "here's why", "what to know", "3 reasons", "5 stocks", "best stocks",
    "should you buy", "price target", "analyst rating", "upgraded to", "downgraded to",
)


def _headers() -> dict[str, str]:
    key, secret = os.getenv("ALPACA_KEY_ID"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("ALPACA_KEY_ID and ALPACA_SECRET_KEY must be set in .env")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def fetch(
    symbols: Optional[list[str]] = None,
    hours_back: int = 24,
    limit: int = 50,
    include_content: bool = True,
) -> list[dict[str, Any]]:
    """Raw articles from the last `hours_back` hours, newest first.

    Passing `symbols=None` returns general market news, which is what you want —
    restricting to your universe would only ever surface first-order stories about
    companies you already follow, and those are exactly the ones already priced.
    """
    start = (datetime.now(timezone.utc) - timedelta(hours=hours_back)) \
        .isoformat(timespec="seconds")
    params: dict[str, Any] = {
        "start": start,
        "sort": "desc",
        "limit": min(max(limit, 1), 50),
        "include_content": str(include_content).lower(),
        "exclude_contentless": "true",
    }
    if symbols:
        params["symbols"] = ",".join(symbols)

    out: list[dict[str, Any]] = []
    token = None
    while len(out) < limit:
        if token:
            params["page_token"] = token
        r = requests.get(NEWS_URL, headers=_headers(), params=params, timeout=30)
        r.raise_for_status()
        body = r.json()
        out.extend(body.get("news") or [])
        token = body.get("next_page_token")
        if not token:
            break
    return out[:limit]


def is_reactive(headline: str) -> bool:
    """True for headlines that report a price move rather than a cause.

    'Nucor shares jump 6%' tells you the move already happened. 'Nucor signs
    supply agreement with Ford' tells you something that has consequences.
    Only the second kind is tradeable at daily frequency.
    """
    h = headline.lower()
    return any(p in h for p in REACTIVE)


def to_items(articles: list[dict[str, Any]], max_items: int = 12) -> list[dict[str, Any]]:
    """Normalise into the shape `reason.generate()` expects, filtering hard.

    Filtering here is a cost control as much as a quality control: every article
    that reaches the model is tokens you pay for, and reactive headlines produce
    nothing but confident nonsense.
    """
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for a in articles:
        headline = (a.get("headline") or "").strip()
        if not headline or headline.lower() in seen:
            continue
        seen.add(headline.lower())

        if is_reactive(headline):
            continue

        subjects = [s.upper() for s in (a.get("symbols") or [])]
        # An article about nothing in particular has no causal chain to trace;
        # one about twenty companies is an index roundup, not a story.
        if not subjects or len(subjects) > 5:
            continue

        body = (a.get("content") or a.get("summary") or "").strip()
        if len(body) < 200:
            continue

        items.append({
            "title": headline,
            "summary": body[:1500],
            "url": a.get("url", ""),
            # The ORIGINAL publish time, never fetch time — using fetch time
            # silently creates lookahead bias in the ledger.
            "published_at": a.get("created_at", ""),
            "subjects": subjects,
            "source": a.get("source", ""),
        })
        if len(items) >= max_items:
            break

    return items


def gather(hours_back: int = 24, max_items: int = 12) -> list[dict[str, Any]]:
    """One call for the morning run: fetch, filter, normalise."""
    return to_items(fetch(hours_back=hours_back, limit=50), max_items=max_items)


def check() -> dict[str, Any]:
    """Smoke test used by `cli check`."""
    try:
        raw = fetch(hours_back=48, limit=10)
        kept = to_items(raw)
        return {"ok": bool(raw), "fetched": len(raw), "after_filtering": len(kept),
                "sample": kept[0]["title"] if kept else None}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
