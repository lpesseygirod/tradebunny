"""The council's own agenda, and the evening job that runs every stage.

The scan picks meetings in this order, until the month's slots run out:

  0. holdings the sentinel flagged (results or a big drop that may break
     the thesis): an urgent review
  1. watch-list names whose buy trigger is HIT (forward P/E or price at or
     below the committee's entry level), and holdings at their ADD level
  2. watch-list names that are CLOSE to their trigger (within 10% on P/E,
     5% on price), and the scout's shortlisted ideas
  3. holdings the monthly portfolio meeting asked the committee to review
  4. holdings not reviewed for `review_holdings_every_days` (about quarterly)

A name discussed in the last 14 days is skipped: the council does not
re-argue the same stock every morning. Before a meeting, the stock gets a
research file if it has none or it is stale, within the weekly budget.

`daily` is what the scheduled job runs, in this order:

    bookkeeping   expire stale proposals, buy approved tranches, measure
                  past decisions, the watch-list deadline
    watch         earnings and big drops on what we own (stage 5)
    scout         once a week, new ideas (stage 1), then research files
                  for them (stage 2)
    portfolio     once a month, the whole book; may propose one swap
    committee     the agenda above (stage 3)
    post-mortems  decisions that clearly went wrong

Every trade any of it proposes still waits for your OK (stage 4), except
on the Autopilot book.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from . import approvals, learning, review, screening, views, watch
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

    def recent(t: str) -> Optional[dict]:
        last = last_meeting(store, t)
        return last if last and _days_since(last["started_at"]) < RECENT_DAYS else None

    # 0. Urgent: the sentinel asked for the committee.
    for a in watch.to_review(store):
        t = a["ticker"]
        if any(x["ticker"] == t for x in picked):
            continue
        if hold_left <= 0:
            skipped.append({"ticker": t, "name": m.name_of(t), "why": "Flagged by the sentinel, but no review slot left"})
            continue
        picked.append({"ticker": t, "name": m.name_of(t), "kind": "holding", "alert": a["id"],
                       "reason": f"Urgent: {a['reason'].lower()}, the sentinel asked for a review", "rank": -1})
        hold_left -= 1

    for w in views.watch_rows(pf, quotes):
        last = last_meeting(store, w["ticker"])
        if w["status"] not in ("hit", "close") or any(x["ticker"] == w["ticker"] for x in picked):
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

    # The scout's shortlisted ideas that no committee has discussed yet.
    for w in m.shortlist():
        if any(x["ticker"] == w.ticker for x in picked) or last_meeting(store, w.ticker):
            continue
        if idea_left <= 0:
            skipped.append({"ticker": w.ticker, "name": w.name, "why": "Shortlisted, but no idea-meeting slot left"})
            continue
        picked.append({"ticker": w.ticker, "name": w.name, "kind": "idea",
                       "reason": f"New idea from the scout: {w.thesis}", "rank": 1})
        idea_left -= 1

    # Holdings the portfolio meeting asked the committee to look at again.
    for q in store.get_meta("review_queue", []):
        t = q["ticker"]
        if t not in held or any(x["ticker"] == t for x in picked):
            continue
        if recent(t):
            continue
        if hold_left <= 0:
            skipped.append({"ticker": t, "name": m.name_of(t), "why": f"{q['why']}; no review slot left"})
            continue
        picked.append({"ticker": t, "name": m.name_of(t), "kind": "holding",
                       "reason": q["why"], "rank": 1.5})
        hold_left -= 1

    due = []
    for t in held:
        if not m.is_stock(t) or any(x["ticker"] == t for x in picked):
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
        if screening.needs_research(store, m, c["ticker"]) and screening.research_left(store, m) > 0:
            screening.write_dossier(store, steward, m, c["ticker"], backend, "Before the committee meets")
        mid = Meeting(store, steward, m, pf, backend).run(c["ticker"])
        if c.get("alert"):
            store.update_alert(c["alert"], meeting_id=mid)
        ids.append(mid)
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
            dl = approvals.propose_deadline(store, m, spare)
            if m.autopilot:
                approvals.autopilot(store, pf, m, dl)
                events.append(f"Autopilot moved €{spare:,.0f} of unused reserve into the index")
            else:
                events.append(f"Proposed: move €{spare:,.0f} of unused reserve into the index (needs your OK)")
        else:
            store.set_meta("deadline_done", today)
    events += watch.run(store, steward, m, pf, backend)
    if backend is not None:
        events += stages(store, steward, m, pf, backend)
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


def stages(store: Store, steward: Steward, m: Mandate, pf: Portfolio, backend: Backend) -> list[str]:
    """The scout (weekly), research files for new ideas, the portfolio meeting (monthly)."""
    events = []
    if screening.screen_due(store, m):
        r = screening.run_screen(store, steward, m, backend)
        if r.get("error"):
            events.append(f"Scout: failed ({r['error']})")
        else:
            events.append("Scout: " + (", ".join(x["name"] for x in r["added"]) + " suggested"
                                       if r["added"] else "no new ideas this week"))
    for did in screening.research_new_ideas(store, steward, m, backend):
        d = store.dossier(did)
        v = (d["data"].get("output") or {}).get("verdict", d["status"])
        events.append(f"Research file on {m.name_of(d['ticker']) if d['ticker'] in m.universe() else d['ticker']}: "
                      f"{v.replace('_', ' ')}")
    if pf.started and review.due(store):
        mid = review.PortfolioMeeting(store, steward, m, pf, backend).run()
        mt = store.meeting(mid)
        o = mt.get("outcome") or {}
        events.append(f"Portfolio meeting #{mid}: " + (o.get("summary") or mt["status"]))
    return events
