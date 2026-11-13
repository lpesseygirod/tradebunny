"""Monthly AI budget: code decides whether the AI may be used at all.

Every AI call records its cost (meetings, research files, sentinel checks,
post-mortems, the scout). Before any stage calls the API, this month's total
is compared with `monthly_ai_budget_usd`. Over budget, the AI stages wait for
next month; bookkeeping, the risk engine, the quant desk and the rules keep
running because they are code.
"""

from __future__ import annotations

from .mandate import Mandate
from .store import Store, now


def spent(store: Store, month: str = "") -> float:
    month = month or now()[:7]
    total = sum(x["cost_usd"] or 0 for x in store.meetings(2000) if x["started_at"].startswith(month))
    total += sum(d["cost_usd"] or 0 for d in store.dossiers() if d["created_at"].startswith(month))
    total += sum(a["cost_usd"] or 0 for a in store.alerts(limit=2000) if a["created_at"].startswith(month))
    total += sum(p["cost_usd"] or 0 for p in store.postmortems() if p["created_at"].startswith(month))
    total += (store.get_meta("scout_spend") or {}).get(month, 0.0)
    return total


def left(store: Store, m: Mandate) -> float:
    return m.limits.monthly_ai_budget_usd - spent(store)


def allows(store: Store, m: Mandate, backend) -> bool:
    """True for scripted members (they cost nothing) or while money is left."""
    return getattr(backend, "name", "scripted") == "scripted" or left(store, m) > 0


def record_scout(store: Store, cost: float) -> None:
    month = now()[:7]
    s = store.get_meta("scout_spend") or {}
    s[month] = round(s.get(month, 0.0) + cost, 4)
    store.set_meta("scout_spend", s)
