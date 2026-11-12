"""The council's own agenda: what to discuss, chosen by rules, not by mood.

The scan picks meetings in this order, until the month's slots run out:

  1. watch-list names whose buy trigger is HIT (forward P/E or price at or
     below the committee's entry level)
  2. watch-list names that are CLOSE to their trigger (within 10% on P/E,
     5% on price)
  3. holdings whose ADD level is reached (e.g. ASML near EUR 1,450)
  4. holdings not reviewed for `review_holdings_every_days` (about quarterly)

A name discussed in the last 14 days is skipped: the council does not
re-argue the same stock every morning. Event-only triggers (results dates,
filings) are left to you: no rule can tell from a price that an event
happened, so you convene those meetings by hand.

`daily` is what the scheduled job runs: expire stale proposals, buy the
tranches you already approved, propose the watch-list deadline move when it
comes, then the scan. Every trade that follows still waits for your OK.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from . import approvals, learning, views
from .agents import Backend
from .data import Steward
from .mandate import Mandate
from .meeting import Meeting
from .portfolio import Portfolio
from .store import Store, now

RECENT_DAYS = 14


def _days_since(iso: Optional[str]) -> float:
    if not iso:
        return float("inf")
    then = datetime.fromisoformat(iso)
    return (datetime.now(timezone.utc) - then).total_seconds() / 86400


def last_meeting(store: Store, ticker: str) -> Optional[dict]:
    return next((x for x in store.meetings(500)
                 if x["ticker"] == ticker and x["status"] == "done"), None)


def agenda(store: Store, pf: Portfolio, m: Mandate, quotes: dict) -> dict:
    """What the council would discuss now, and why the rest is not on it."""
    lim = m.limits
    idea_left = max(0, lim.max_meetings_per_month - store.meetings_this_month("idea"))
    hold_left = max(0, lim.max_holding_reviews_per_month - store.meetings_this_month("holding"))
    picked, skipped = [], []
    held = pf.quantities()

    for w in views.watch_rows(pf, quotes):
        last = last_meeting(store, w["ticker"])
        if w["status"] not in ("hit", "close"):
            continue
        if w["ticker"] in held:
            # A holding at its add level is reviewed now, not at the quarter.
            if w["status"] != "hit":
                continue
            if last and _days_since(last["started_at"]) < RECENT_DAYS:
                skipped.append({"ticker": w["ticker"], "name": w["name"],
                                "why": f"Discussed {views.day_label(last['started_at'])}; waits {RECENT_DAYS} days"})
            elif hold_left <= 0:
                skipped.append({"ticker": w["ticker"], "name": w["name"],
                                "why": "Add level reached, but no review slot left this month"})
            else:
                picked.append({"ticker": w["ticker"], "name": w["name"], "kind": "holding",
                               "reason": "Add level reached: " + w["line"], "rank": 1})
                hold_left -= 1
            continue
        reason = ("Buy trigger hit: " if w["status"] == "hit" else "Close to its trigger: ") + w["line"]
        if last and _days_since(last["started_at"]) < RECENT_DAYS:
            skipped.append({"ticker": w["ticker"], "name": w["name"],
                            "why": f"Discussed {views.day_label(last['started_at'])}; waits {RECENT_DAYS} days"})
        elif idea_left <= 0:
            skipped.append({"ticker": w["ticker"], "name": w["name"],
                            "why": "No idea-meeting slot left this month"})
        else:
            picked.append({"ticker": w["ticker"], "name": w["name"], "kind": "idea",
                           "reason": reason, "rank": 0 if w["status"] == "hit" else 1})
            idea_left -= 1

    due = []
    for t in held:
        if not m.is_stock(t):
            continue
        last = last_meeting(store, t)
        bought = next((x["ts"] for x in store.trades(t) if x["side"] == "buy"), None)
        since = last["started_at"] if last else bought      # the clock starts at the first buy
        age = _days_since(since)
        if age >= lim.review_holdings_every_days:
            due.append((age, t, last, since))
    for age, t, last, since in sorted(due, key=lambda x: -x[0]):
        why = (f"Bought {views.day_label(since)}, never reviewed" if not last
               else f"Last reviewed {views.day_label(last['started_at'])}")
        if hold_left <= 0:
            skipped.append({"ticker": t, "name": m.name_of(t), "why": f"{why}; no review slot left this month"})
            continue
        picked.append({"ticker": t, "name": m.name_of(t), "kind": "holding",
                       "reason": f"Quarterly review: {why.lower()}", "rank": 2})
        hold_left -= 1
    picked.sort(key=lambda x: x["rank"])
    return {"picked": picked, "skipped": skipped}


def run(store: Store, steward: Steward, m: Mandate, pf: Portfolio, backend: Backend,
        limit: Optional[int] = None) -> list[int]:
    plan = agenda(store, pf, m, steward.quotes(pf.tickers_needed()))
    picked = plan["picked"][:limit] if limit else plan["picked"]
    store.set_meta("last_scan", {"at": now(), "picked": picked, "skipped": plan["skipped"]})
    ids = []
    for c in picked:
        ids.append(Meeting(store, steward, m, pf, backend).run(c["ticker"]))
    return ids


def daily(store: Store, steward: Steward, m: Mandate, pf: Portfolio,
          backend: Optional[Backend] = None) -> list[str]:
    events = []
    n = approvals.expire(store)
    if n:
        events.append(f"{n} unanswered proposal{'s' if n != 1 else ''} expired")
    events += pf.review()
    n = learning.update_marks(store, steward, m)
    if n:
        events.append(f"Scorekeeper: {n} past decision{'s' if n != 1 else ''} measured against the index")
    today = now()[:10]
    if (today >= m.limits.watchlist_deadline and not store.get_meta("deadline_done")
            and not any(p["action"] == "deadline" for p in store.proposals("pending"))):
        spare = pf.spare_reserve()
        if spare > 1:
            approvals.propose_deadline(store, m, spare)
            events.append(f"Proposed: move €{spare:,.0f} of unused reserve into the index (needs your OK)")
        else:
            store.set_meta("deadline_done", today)
    if backend is not None:
        ids = run(store, steward, m, pf, backend)
        scan = store.get_meta("last_scan") or {}
        for c, mid in zip(scan.get("picked", []), ids):
            mt = store.meeting(mid)
            d = (mt.get("decision") or {}).get("decision", mt["status"])
            events.append(f"Meeting #{mid} on {c['name']} ({c['reason']}): {d}")
        if not ids:
            events.append("Scan: nothing for the council to discuss today")
    if backend is not None:
        left = m.limits.max_postmortems_per_month - learning.postmortems_this_month(store)
        for case in learning.due_postmortems(store, m)[:max(0, left)]:
            pid = learning.run_postmortem(store, steward, m, case, backend)
            pm = store.postmortem(pid)
            events.append(f"Post-mortem #{pid} on {case['name']}: {case['reason']}"
                          + (f" ({len(store.lessons('proposed'))} lessons waiting for your OK)"
                             if pm["status"] == "done" else " (failed)"))
    pending = store.proposals("pending")
    if pending:
        events.append(f"{len(pending)} proposal{'s' if len(pending) != 1 else ''} waiting for your OK")
    return events
