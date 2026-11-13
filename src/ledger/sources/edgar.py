"""SEC EDGAR client — Form 4 insider trades and Schedule 13D activist stakes.

Free, no API key. Two things to know:
  1. EDGAR returns 403 without a descriptive User-Agent containing a contact email.
  2. The rate limit is 10 requests/second and they do enforce it.

Filing timeliness is why this module exists at all:
    Form 4      2 business days   ← fresh enough to trade
    13D         5 business days   ← fresh enough to trade
    13G passive 5 business days
    13F         45 days after quarter end  ← stale; deliberately not used
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import requests

UA = None  # set by configure()
_LAST_CALL = 0.0
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


def configure(contact_email: str, app: str = "PredictionLedger") -> None:
    """EDGAR requires this. Without it every request comes back 403."""
    global UA
    UA = f"{app} ({contact_email})"


def _get(url: str, **params: Any) -> requests.Response:
    global _LAST_CALL
    if UA is None:
        raise RuntimeError("call edgar.configure(your_email) before making requests")
    gap = time.monotonic() - _LAST_CALL
    if gap < 0.15:                       # stay well under 10 req/s
        time.sleep(0.15 - gap)
    _LAST_CALL = time.monotonic()
    r = requests.get(url, params=params or None,
                     headers={"User-Agent": UA, "Accept-Encoding": "gzip, deflate"},
                     timeout=30)
    r.raise_for_status()
    return r


@dataclass
class Filing:
    form: str
    accession: str
    filed_at: str
    company: str
    cik: str
    url: str
    ticker: Optional[str] = None


def recent(form_type: str, count: int = 100) -> list[Filing]:
    """Most recently filed documents of one form type, newest first.

    form_type is an EDGAR form code: "4", "SC 13D", "SC 13G", "8-K".
    """
    r = _get("https://www.sec.gov/cgi-bin/browse-edgar",
             action="getcurrent", type=form_type, company="", dateb="",
             owner="include", count=count, output="atom")
    root = ET.fromstring(r.content)
    out: list[Filing] = []
    for entry in root.findall("a:entry", ATOM_NS):
        title = (entry.findtext("a:title", "", ATOM_NS) or "").strip()
        link_el = entry.find("a:link", ATOM_NS)
        link = link_el.get("href", "") if link_el is not None else ""
        updated = entry.findtext("a:updated", "", ATOM_NS) or ""
        # Titles look like: "4 - DOE JOHN (0001234567) (Reporting)"
        company, cik = title, ""
        if "(" in title and ")" in title:
            try:
                company = title.split(" - ", 1)[1].split(" (")[0].strip()
                cik = title.split("(")[1].split(")")[0].strip()
            except IndexError:
                pass
        out.append(Filing(form=form_type, accession=link.rsplit("/", 1)[-1],
                          filed_at=updated, company=company, cik=cik, url=link))
    return out


def form4_transactions(filing_url: str) -> list[dict[str, Any]]:
    """Parse the XML behind one Form 4 into individual transactions.

    Only code "P" (open-market purchase) counts as a real insider buy. Code "A"
    is a grant and means nothing — the executive did not choose to spend money.
    """
    idx = _get(filing_url).text
    xml_url = None
    for part in idx.split('"'):
        if part.endswith(".xml") and "form4" not in part.lower():
            base = filing_url.rsplit("/", 1)[0]
            xml_url = part if part.startswith("http") else f"{base}/{part.lstrip('/')}"
            break
    if not xml_url:
        return []

    root = ET.fromstring(_get(xml_url).content)
    issuer = root.find("issuer")
    ticker = (issuer.findtext("issuerTradingSymbol", "") or "").upper() if issuer is not None else ""
    owner = root.find(".//reportingOwnerId/rptOwnerName")
    rows: list[dict[str, Any]] = []
    for t in root.findall(".//nonDerivativeTransaction"):
        code = t.findtext(".//transactionCode", "")
        shares = float(t.findtext(".//transactionShares/value", "0") or 0)
        price = float(t.findtext(".//transactionPricePerShare/value", "0") or 0)
        rows.append({
            "ticker": ticker,
            "insider": owner.text if owner is not None else "",
            "code": code,
            "is_purchase": code == "P",
            "shares": shares,
            "price": price,
            "value_usd": shares * price,
            "date": t.findtext(".//transactionDate/value", ""),
        })
    return rows


def cluster_buys(lookback_filings: int = 300, min_insiders: int = 3,
                 min_total_usd: float = 250_000) -> list[dict[str, Any]]:
    """Find tickers where several insiders bought on the open market recently.

    Cluster buying is one of the more durable published anomalies — a single buy
    means little, three officers buying in the same window is a different signal.
    """
    by_ticker: dict[str, list[dict]] = {}
    for f in recent("4", count=lookback_filings):
        try:
            for tx in form4_transactions(f.url):
                if tx["is_purchase"] and tx["ticker"]:
                    by_ticker.setdefault(tx["ticker"], []).append(tx)
        except Exception:
            continue                       # one malformed filing must not kill the run

    clusters = []
    for ticker, txs in by_ticker.items():
        insiders = {t["insider"] for t in txs}
        total = sum(t["value_usd"] for t in txs)
        if len(insiders) >= min_insiders and total >= min_total_usd:
            clusters.append({
                "ticker": ticker, "n_insiders": len(insiders),
                "total_usd": total, "transactions": txs,
                "subjects": [ticker],
            })
    return sorted(clusters, key=lambda c: c["total_usd"], reverse=True)


def activist_stakes(count: int = 60) -> Iterator[dict[str, Any]]:
    """Recent Schedule 13D filings — 5%+ stakes taken with intent to influence.

    Due within 5 business days since the 2024 amendments (previously 10 calendar
    days), which makes these genuinely timely.
    """
    for f in recent("SC 13D", count=count):
        yield {
            "title": f"13D filed on {f.company}",
            "company": f.company,
            "url": f.url,
            "published_at": f.filed_at,
            "summary": f"Schedule 13D — a holder crossed 5% with stated intent to "
                       f"engage. Filed {f.filed_at}.",
            "subjects": [],
        }


def check() -> dict[str, Any]:
    """Smoke test used by `cli check`."""
    try:
        got = recent("4", count=5)
        return {"ok": bool(got), "n": len(got),
                "sample": got[0].company if got else None,
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
