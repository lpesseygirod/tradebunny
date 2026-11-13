"""What each page shows, computed from the store, the mandate and the prices.

The templates only lay things out. Every number they print is worked out
here, so it can be tested and so a page can never invent one.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import risk
from .data import Steward
from .mandate import Mandate
from .portfolio import Portfolio
from .store import Store

STANCE_LABEL = {"supportive": "Supportive", "neutral": "Neutral",
                "cautious": "Cautious", "against": "Against"}
DECISION_LABEL = {"buy": "Buy", "add": "Add", "hold": "Hold", "trim": "Trim", "sell": "Sell",
                  "watch": "Watch", "reject": "Avoid", "swap": "Swap", "keep": "Keep"}
DECISION_CLS = {"swap": "accent", "keep": "hold"}
ROLE_SUB = {"macro": "The economy", "thesis": "Five-point chokepoint test",
            "valuation": "Is it priced in?", "earnings": "The latest results",
            "redteam": "Pre-mortem: why did we lose 40%?"}
METRIC_LABEL = {"stock_vs_index_3m": "the stock vs the index in 3 months",
                "stock_vs_index_12m": "the stock vs the index in 12 months",
                "revenue_growth_next_q": "revenue growth next quarter",
                "op_margin_next_q": "operating margin next quarter"}
VOTE_LABEL = {"buy": "Buy", "wait": "Wait", "no": "No"}
VOTE_CLS = {"buy": "buy", "wait": "watch", "no": "sell"}
VERDICT_LABEL = {"send_to_committee": "Send to the committee", "keep_watching": "Keep watching", "drop": "Drop"}
VERDICT_CLS = {"send_to_committee": "hit", "keep_watching": "watch", "drop": "notyet"}
CAND_LABEL = {"new": "Waiting for its file", "shortlisted": "Shortlisted", "watching": "Watching",
              "dropped": "Dropped"}
CAND_CLS = {"new": "notyet", "shortlisted": "hit", "watching": "watch", "dropped": "reject"}
ALERT_LABEL = {"open": "Not judged yet", "noise": "Noise", "watch": "Noted", "convene": "Review asked",
               "failed": "Failed"}
ALERT_CLS = {"open": "notyet", "noise": "ok", "watch": "watch", "convene": "sell", "failed": "reject"}


def display_name(m: Mandate, store: Store, ticker: str) -> str:
    if ticker == "PORTFOLIO":
        return "Portfolio meeting"
    if ticker in m.universe() or m.rejection(ticker):
        return m.name_of(ticker)
    c = store.candidate(ticker)
    return c["name"] if c else ticker


# ------------------------------------------------------------------ format

def local(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone()


def day_label(iso_or_day: Optional[str], year: bool = False) -> str:
    if not iso_or_day:
        return ""
    if len(iso_or_day) == 10:
        d = date.fromisoformat(iso_or_day)
    else:
        lt = local(iso_or_day)
        if not lt:
            return iso_or_day
        d = lt.date()
    return f"{d.day} {d.strftime('%b')}" + (f" {d.year}" if year else "")


def time_label(iso: Optional[str]) -> str:
    lt = local(iso)
    return lt.strftime("%H:%M") if lt else ""


def stamp(iso: Optional[str]) -> str:
    return f"{day_label(iso)}, {time_label(iso)}" if iso else ""


def prices_label(steward: Steward) -> str:
    last = steward.last_update()
    if steward.mode == "snapshot":
        return "Snapshot" + (f" · {stamp(last)}" if last else "")
    return "Live" + (f" · {time_label(last)}" if last else "")


def pct(x: float, digits: int = 1) -> str:
    return f"{x * 100:.{digits}f}%"


def month_name() -> str:
    return datetime.now().strftime("%B")


# ------------------------------------------------------------------- money

def chart(snaps: list[dict], w: int = 700, h: int = 190) -> dict:
    if len(snaps) < 2:
        return {}
    vals = [s["value_eur"] for s in snaps] + [s["benchmark_eur"] for s in snaps]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.15 or hi * 0.002
    lo, hi = lo - pad, hi + pad
    n = len(snaps) - 1

    def y(v: float) -> float:
        return h - (v - lo) / (hi - lo) * h

    def path(key: str) -> str:
        return "M" + " L".join(f"{i / n * w:.1f},{y(s[key]):.1f}" for i, s in enumerate(snaps))
    k = min(5, len(snaps))
    picks = sorted({round(i * n / (k - 1)) for i in range(k)}) if k > 1 else [0]
    return {"w": w, "h": h, "pf": path("value_eur"), "bm": path("benchmark_eur"),
            "grid": [h * f for f in (0.18, 0.5, 0.82)],
            "labels": [day_label(snaps[i]["day"]) for i in picks],
            "span": f"{day_label(snaps[0]['day'])}–{day_label(snaps[-1]['day'])}"}


def meeting_slots(store: Store, m: Mandate) -> dict:
    used = store.meetings_this_month("idea")
    cap = m.limits.max_meetings_per_month
    return {"used": used, "cap": cap, "left": max(0, cap - used), "month": month_name()}


def watch_rows(pf: Portfolio, quotes: dict) -> list[dict]:
    rows = []
    for w in pf.watch_status(quotes):
        q = w["quote"]
        item = pf.m.watch(w["ticker"])
        inst = pf.m.instrument(w["ticker"])
        status, line = "notyet", ""
        if item and item.trigger_pe and q and q.forward_pe:
            line = f"Trigger fwd P/E ≤ {item.trigger_pe:g}× · now {q.forward_pe:.1f}×"
            if q.forward_pe <= item.trigger_pe:
                status = "hit"
            elif q.forward_pe <= item.trigger_pe * 1.1:
                status = "close"
        price_trigger = (item.trigger_price_eur if item else None) or (inst.entry_price_eur if inst else None)
        if price_trigger and q:
            pl = f"Trigger price ≤ €{price_trigger:,.0f} · now €{q.price_eur:,.2f}"
            line = f"{line} · {pl}" if line else pl
            if q.price_eur <= price_trigger:
                status = "hit"
            elif status != "hit" and q.price_eur <= price_trigger * 1.05:
                status = "close"
        if not line:
            status, line = "event", f"Event: {w['event'] or 'no trigger on file'}"
        rows.append({"ticker": w["ticker"], "name": w["name"], "grade": w["grade"],
                     "line": line, "status": status})
    order = {"hit": 0, "close": 1, "notyet": 2, "event": 3}
    return sorted(rows, key=lambda r: order[r["status"]])


STATUS_LABEL = {"hit": "Trigger hit", "close": "Close", "notyet": "Not yet", "event": "Event"}


def decision_row(mt: dict, m: Mandate) -> dict:
    d = mt.get("decision") or {}
    trig = d.get("buy_trigger") or ""
    sub = trig if trig and trig.lower() != "none" else (d.get("action_label") or mt.get("error") or "")
    if mt["status"] == "running":
        label, cls = "In session", "outline"
    elif d.get("decision"):
        label, cls = DECISION_LABEL[d["decision"]], DECISION_CLS.get(d["decision"], d["decision"])
    else:
        label, cls = mt["status"].capitalize(), "notyet"
    blocked = (mt.get("outcome") or {}).get("blocked_by") or []
    name = "Portfolio meeting" if mt["kind"] == "portfolio" else m.name_of(mt["ticker"])
    return {"id": mt["id"], "ticker": mt["ticker"], "name": name,
            "grade": d.get("grade") or "", "date": day_label(mt["started_at"]),
            "sub": sub, "label": label, "cls": cls,
            "trigger": trig if trig and trig.lower() != "none" else "",
            "sell_if": d.get("sell_if") or "", "cost": mt.get("cost_usd") or 0.0,
            "blocked": blocked if d.get("decision") in ("buy", "add", "sell", "trim") else [],
            "kind": mt["kind"], "backend": mt["backend"], "decision": d.get("decision")}


def proposal_rows(store: Store, m: Mandate) -> list[dict]:
    out = []
    for p in store.proposals("pending"):
        exp = local(p["expires_at"])
        days = (exp - datetime.now(exp.tzinfo)).days if exp else None
        out.append({"id": p["id"], "summary": p["summary"], "ticker": p["ticker"],
                    "name": (m.name_of(p["ticker"]) if p["ticker"] != "INDEX" else "Index ETFs")
                            if p["action"] != "swap" else
                            f"{m.name_of(p['data']['sell'])} → {m.name_of(p['data']['buy'])}",
                    "grade": p["data"].get("grade") or "", "meeting_id": p["meeting_id"],
                    "action": p["action"], "label": p["data"].get("action_label") or "",
                    "expires": f"expires in {days} day{'s' if days != 1 else ''}" if days is not None and days >= 1
                               else "expires today"})
    return out


def money(store: Store, pf: Portfolio, m: Mandate, quotes: dict) -> dict:
    book = pf.book(quotes)
    capital = float(store.get_meta("capital_eur", m.start_capital_eur))
    value = book.total
    bench = pf.benchmark_value(quotes)
    fees = sum(t["fee_eur"] for t in store.trades())
    gain, bench_gain = value / capital - 1, bench / capital - 1
    edge_pts = (gain - bench_gain) * 100
    cash_share = book.cash / value if value else 0
    started = store.get_meta("started_on")
    if edge_pts < 0 and cash_share > 0.3:
        why = (f"<b>{pct(cash_share, 0)} is still cash</b> while tranches 2 and 3 wait for their "
               f"dates, and the index is fully invested. Cash earns nothing here, so the gap "
               f"narrows as the plan fills.")
        why_word = "behind"
    elif edge_pts < 0:
        why, why_word = (f"the holdings have returned less than the index since {day_label(started)}, "
                         f"after <b>€{fees:,.0f} of fees</b>."), "behind"
    else:
        why, why_word = (f"the holdings have returned more than the index since {day_label(started)}, "
                         f"after <b>€{fees:,.0f} of fees</b>."), "ahead"

    held_ai = sum(v for t, v in book.holdings.items() if m.is_ai(t)) / value if value else 0
    tw_held = sum(v for t, v in book.holdings.items() if m.is_taiwan(t)) / value if value else 0
    tw_names = [t for t in book.holdings.keys() | book.pending.keys() if m.is_taiwan(t)]
    tw_done = sum(1 for t in store.trades() if t["ticker"] in tw_names and t["side"] == "buy")
    tw_total = tw_done + sum(1 for t in store.tranches("planned") if t["ticker"] in tw_names)
    n = risk.stock_count(book, m)
    slots = meeting_slots(store, m)
    cap_ai = m.limits.ai_cap.get(m.setting)
    meters = [
        {"name": "AI-linked stocks", "now": pct(held_ai), "limit": f"{pct(cap_ai, 0)} limit",
         "fill": held_ai / cap_ai, "sub": f"{pct(risk.ai_weight(book, m))} once all tranches are in",
         "cls": "red" if risk.ai_weight(book, m) > cap_ai else ""},
        {"name": "Taiwan (TSMC)", "now": pct(tw_held), "limit": f"{pct(m.limits.taiwan_cap, 0)} cap",
         "fill": tw_held / m.limits.taiwan_cap,
         "sub": f"Tranche {tw_done} of {tw_total} filled" if tw_total else "Not held", "cls": ""},
        {"name": "Number of stocks", "now": str(n), "limit": f"{m.limits.max_single_stocks} max",
         "fill": n / m.limits.max_single_stocks,
         "sub": (f"Room for {m.limits.max_single_stocks - n} more names" if n < m.limits.max_single_stocks
                 else "Full: a new name means selling one first"), "cls": ""},
        {"name": f"Meetings in {slots['month']}", "now": str(slots["used"]), "limit": f"{slots['cap']} slots",
         "fill": slots["used"] / slots["cap"], "cls": "amber",
         "sub": f"{slots['left']} slot{'s' if slots['left'] != 1 else ''} left this month"},
    ]
    positions = pf.positions(quotes)[:5]
    watch = watch_rows(pf, quotes)
    return {
        "value": value, "capital": capital, "gain_eur": value - capital, "gain": gain,
        "fees": fees, "bench": bench, "bench_gain": bench_gain, "edge_pts": edge_pts,
        "started": day_label(started, year=True), "chart": chart(store.snapshots()),
        "why": why, "why_word": why_word, "slots": slots, "meters": meters,
        "positions": positions, "watch": watch[:6],
        "chips": [w for w in watch if w["status"] != "event"][:3] or watch[:3],
        "decisions": [decision_row(x, m) for x in store.meetings(5)],
        "proposals": proposal_rows(store, m),
    }


# ----------------------------------------------------------------- meeting

STEPS = [("gate", "Gate", "Code"), ("blind", "Blind memos", "MS · TA · VA · EA · RT"),
         ("objections", "Objections", "RT"), ("rebuttal", "Rebuttals", "TA"),
         ("riskmemo", "Risk check", "FC · RO"), ("vote", "Vote", "All"), ("verdict", "Verdict", "CIO"),
         ("approval", "Your OK", "You")]


def _provenance(step: dict, facts: Optional[dict], backend: str) -> str:
    src = (step.get("data") or {}).get("sources") or []
    if src:
        return f"{len(src)} sources read · web"
    if step["role"] == "valuation" and facts:
        return f"Fwd P/E · price feed · {stamp(facts['quote'].get('as_of'))}"
    if step["role"] == "thesis" and backend == "scripted":
        return "Thesis from the October review"
    return "No figures quoted" if step["role"] == "macro" else "Scenario, no figures quoted"


def debate(store: Store, m: Mandate, mid: int) -> dict:
    mt = store.meeting(mid)
    steps = store.steps(mid)
    facts = next((s["data"] for s in steps if s["phase"] == "facts"), None)
    gate = next((s for s in steps if s["phase"] == "gate"), None)
    blind = [s for s in steps if s["phase"] == "blind"]
    objections = next((s for s in steps if s["phase"] == "cross" and s["role"] == "redteam"), None)
    rebuttal = next((s for s in steps if s["phase"] == "cross" and s["role"] == "thesis"), None)
    riskmemo = next((s for s in steps if s["role"] == "risk"), None)
    cio = next((s for s in steps if s["role"] == "cio"), None)
    engine = next((s for s in steps if s["phase"] == "risk" and s["role"] == "code"
                   and s.get("data") and "checks" in s["data"]), None)
    stopped = [s for s in steps if s["phase"] == "risk" and s["role"] == "code" and not s.get("data")]
    factcheck = next((s for s in steps if s["role"] == "checker"), None)
    proposal = next(iter(store.proposals(meeting_id=mid)), None)

    vote_steps = [x for x in steps if x["phase"] == "vote"]
    votes = [{"role": x["role"], **((x.get("data") or {}).get("output") or {})} for x in vote_steps]
    tally = {k: sum(1 for v in votes if v.get("vote") == k) for k in ("buy", "wait", "no")}
    present = {"gate": bool(gate), "blind": len(blind) >= 4, "objections": bool(objections),
               "rebuttal": bool(rebuttal), "riskmemo": bool(riskmemo),
               "vote": bool(votes) or (bool(cio) and not votes), "verdict": bool(cio),
               "approval": bool(engine) and (proposal is None or proposal["status"] != "pending")}
    running = mt["status"] == "running"
    first_missing = next((k for k, *_ in STEPS if not present[k]), None)
    waiting = proposal is not None and proposal["status"] == "pending"
    stepper = [{"key": k, "label": label, "sub": sub,
                "state": "done" if present[k] else ("now" if (running and k == first_missing)
                                                    or (waiting and k == "approval") else "")}
               for k, label, sub in STEPS]

    memos = []
    for s in blind:
        o = (s.get("data") or {}).get("output") or {}
        stance = "against" if s["role"] == "redteam" else o.get("stance", "neutral")
        memos.append({"role": s["role"], "sub": ROLE_SUB.get(s["role"], s["title"]),
                      "stance": stance, "stance_label": STANCE_LABEL.get(stance, stance),
                      "points": o.get("key_points") or [], "body": s["body"], "out": o,
                      "model": (s.get("data") or {}).get("model", ""),
                      "reused": s["title"].split("(reused: ")[1].rstrip(")") if "(reused: " in s["title"] else "",
                      "sources": (s.get("data") or {}).get("sources") or [],
                      "prov": _provenance(s, facts, mt["backend"])})
    order = ["macro", "thesis", "valuation", "earnings", "redteam"]
    memos.sort(key=lambda x: order.index(x["role"]) if x["role"] in order else 9)

    pairs = []
    if objections:
        answers = {r["rank"]: r for r in ((rebuttal or {}).get("data") or {}).get("output", {}).get("responses", [])}
        for o in objections["data"]["output"]["objections"]:
            pairs.append({"rank": o["rank"], "claim": o["claim"], "evidence": o["evidence"],
                          "test": o["test"], "answer": answers.get(o["rank"])})

    # The size the scenarios are measured at: what the risk engine allowed,
    # else what is held or planned now, else the minimum position.
    size = None
    if engine and engine["data"].get("allowed") and engine["data"].get("amount_eur", 0) > 0:
        size = engine["data"]["target_weight"]
    elif facts:
        size = facts.get("weight") or facts.get("target") or m.limits.min_position
    scen = None
    if riskmemo:
        o = riskmemo["data"]["output"]
        rows = o.get("scenarios") or []
        if isinstance(rows, dict):          # meetings recorded before scenario moves existed
            rows = []
        size = size or m.limits.min_position
        biggest = max((abs(r["stock_move_pct"]) for r in rows), default=1) or 1
        table = [{"name": r["scenario"], "move": r["stock_move_pct"],
                  "impact": r["stock_move_pct"] * size, "width": abs(r["stock_move_pct"]) / biggest * 100}
                 for r in rows]
        worst = min((t["impact"] for t in table), default=0.0)
        ai_after = None
        if facts and facts.get("ai"):
            ai_after = facts["ai_weight"] + max(0.0, size - facts.get("weight", 0.0))
        scen = {"rows": table, "size": size, "worst": worst, "overlap": o.get("overlap", ""),
                "recommendation": o.get("recommendation", ""), "body": riskmemo["body"],
                "ai_after": ai_after, "model": riskmemo["data"].get("model", "")}

    verdict = None
    trades = [t for t in store.trades() if t["meeting_id"] == mid]
    plan = [t for t in store.tranches(None) if t["meeting_id"] == mid]
    if cio:
        d = cio["data"]["output"]
        e = (engine or {}).get("data") or {}
        if e.get("allowed") and e.get("amount_eur", 0) > 0 and e.get("action") == "buy":
            capped = " (the Taiwan cap)" if any(c["name"] == "Taiwan cap" for c in e["checks"]) else ""
            size_line = f"Size <b>{pct(e['target_weight'])}</b>{capped}, in {m.limits.tranches} tranches"
        elif e.get("allowed") and e.get("action") == "sell":
            size_line = f"Sell <b>€{e['amount_eur']:,.0f}</b>"
        elif e and not e.get("allowed"):
            first = next((c for c in e["checks"] if not c["ok"] and c["blocking"]), None)
            size_line = "Blocked by the risk engine" + (f": {first['detail']}" if first else "")
        else:
            size_line = "No order"
        buys = [t for t in trades if t["side"] == "buy"]
        due = sorted(p["due"] for p in plan if p["status"] == "planned")
        trig = d["buy_trigger"] if d["buy_trigger"] and d["buy_trigger"].lower() != "none" else ""
        if buys and due:
            trigger_text = (f"Tranche 1 now. Tranches 2–{len(due) + 1} on "
                            + " and ".join(day_label(x) for x in due) + "." + (f" {trig}" if trig else ""))
        else:
            trigger_text = trig or "None: no buy is waiting on a trigger."
        verdict = {"d": d, "label": DECISION_LABEL[d["decision"]], "size_line": size_line,
                   "trigger": trigger_text, "body": cio["body"], "model": cio["data"].get("model", "")}

    orders = []
    k = 0
    for t in trades:
        if t["side"] == "buy" and t["note"].startswith("tranche"):
            k += 1
            label = f"{k} of {m.limits.tranches}"
        else:
            label = "Funding" if "funding" in t["note"] else t["side"].capitalize()
        orders.append({"label": label, "name": m.name_of(t["ticker"]), "when": stamp(t["ts"]),
                       "amount": t["amount_eur"], "fee": t["fee_eur"], "status": "Filled", "cls": "filled"})
    for p in plan:
        k += 1 if p["status"] != "cancelled" else 0
        orders.append({"label": p["note"].replace("tranche ", ""), "name": m.name_of(p["ticker"]),
                       "when": day_label(p["due"]), "amount": p["amount_eur"], "fee": m.fee_per_order_eur,
                       "status": {"planned": "Scheduled", "done": "Filled", "cancelled": "Cancelled"}[p["status"]],
                       "cls": {"planned": "notyet", "done": "filled", "cancelled": "sell"}[p["status"]]})

    if waiting and not orders:
        sz = proposal["data"].get("sizing", {})
        n = m.limits.tranches if proposal["action"] in ("buy", "add") else 1
        for i in range(n):
            orders.append({"label": f"{i + 1} of {n}" if n > 1 else proposal["action"].capitalize(),
                           "name": m.name_of(proposal["ticker"]),
                           "when": "On your OK" if i == 0 else f"+{m.limits.tranche_spacing_days * i} days",
                           "amount": (sz.get("tranche_eur") if n > 1 else proposal["amount_eur"]) or 0,
                           "fee": m.fee_per_order_eur, "status": "Awaiting your OK", "cls": "close"})

    eng = None
    if engine:
        e = engine["data"]
        if e["allowed"] and e["amount_eur"] > 0:
            line, cls = (f"ALLOWED · {'proposed' if e['action'] == 'buy' else 'sell'} "
                         f"€{e['amount_eur']:,.0f}" + (f" in {m.limits.tranches} tranches" if e["action"] == "buy" else "")), ""
        elif e["allowed"]:
            line, cls = "NO ORDER · " + (e["checks"][0]["detail"] if e["checks"] else ""), "none"
        else:
            line, cls = "BLOCKED · the verdict does not pass the mandate", "no"
        eng = {"checks": e["checks"], "line": line, "cls": cls, "funding": e.get("funding") or []}

    name = (facts or {}).get("quote", {}).get("name") or m.name_of(mt["ticker"])
    span = time_label(mt["started_at"]) + (f"–{time_label(mt['finished_at'])}" if mt.get("finished_at") else "")
    return {"mt": mt, "title": m.name_of(mt["ticker"]), "subtitle": name,
            "when": f"{day_label(mt['started_at'], year=True)}, {span}",
            "facts": facts, "facts_body": next((s["body"] for s in steps if s["phase"] == "facts"), ""),
            "gate": gate, "stepper": stepper, "memos": memos, "pairs": pairs, "scen": scen,
            "verdict": verdict, "engine": eng, "orders": orders, "stopped": stopped,
            "factcheck": (factcheck or {}).get("data", {}).get("output") if factcheck else None,
            "proposal": proposal, "postmortems": store.postmortems(mid),
            "running": running, "blind_count": len(blind),
            "votes": votes, "tally": tally, "VOTE_LABEL": VOTE_LABEL, "VOTE_CLS": VOTE_CLS,
            "dossier": _dossier_ref(store, mt["ticker"], mt["started_at"])}


def _dossier_ref(store: Store, ticker: str, before: str) -> Optional[dict]:
    """The research file the members read: the newest one written before the meeting."""
    return next((d for d in store.dossiers(ticker, "done") if d["created_at"] <= before), None)


# ----------------------------------------------------------------- tables

def statement(store: Store, capital: float, m: Mandate) -> list[dict]:
    rows, cash = [], capital
    started = store.get_meta("started_on")
    rows.append({"date": day_label(started), "desc": "Opening paper deposit", "amount": capital,
                 "fee": None, "cash": cash, "mid": None})
    for t in store.trades():
        if t["side"] == "buy":
            cash -= t["amount_eur"] + t["fee_eur"]
            amount = -t["amount_eur"]
        else:
            cash += t["amount_eur"] - t["fee_eur"]
            amount = t["amount_eur"]
        note = t["note"].split(".")[0]
        rows.append({"date": day_label(t["ts"]), "mid": t["meeting_id"],
                     "desc": f"{t['side'].capitalize()} {m.name_of(t['ticker'])} · {note}",
                     "amount": amount, "fee": -t["fee_eur"], "cash": cash})
    return list(reversed(rows))


def tranche_progress(store: Store, ticker: str) -> tuple[int, int]:
    done = sum(1 for t in store.trades() if t["ticker"] == ticker and t["side"] == "buy"
               and t["note"].startswith("tranche"))
    planned = sum(1 for t in store.tranches("planned") if t["ticker"] == ticker)
    return done, done + planned


def limits(book: Optional[risk.Book], m: Mandate, store: Store) -> list[dict]:
    lim = m.limits
    rows = []

    def status(ratio: float) -> str:
        # At the cap is "close", not "over": weights are floats, and a position
        # sized exactly to its cap must not read as a breach.
        return "over" if ratio > 1.0005 else "close" if ratio >= 0.8 else "ok"

    def row(name, sub, rule, fill, now, st=None):
        rows.append({"name": name, "sub": sub, "rule": rule, "fill": max(0.0, min(fill, 1.0)),
                     "now": now, "status": st or status(fill)})

    total = book.total if book else 0
    stock_w = [book.weight(t) for t in book.holdings.keys() | book.pending.keys()
               if m.is_stock(t)] if book else []
    held_w = [book.holdings[t] / total for t in book.holdings if m.is_stock(t)] if book and total else []
    if held_w:
        now = f"Now {pct(min(held_w), 2)}–{pct(max(held_w), 2)}"
    else:
        now = "Now no stocks"
    biggest = max(stock_w, default=0.0)
    row("Position size", f"Each stock {pct(lim.min_position, 0)}–{pct(lim.max_position, 0)} once all tranches are in",
        f"{pct(lim.min_position, 0)}–{pct(lim.max_position, 0)} each", biggest / lim.max_position, now,
        "over" if biggest > lim.max_position + lim.rebalance_band / 2 else
        "close" if biggest >= lim.max_position * 0.999 else "ok")
    tw = sum(book.weight(t) for t in book.holdings.keys() | book.pending.keys() if m.is_taiwan(t)) if book else 0
    tw_held = sum(book.holdings[t] for t in book.holdings if m.is_taiwan(t)) / total if total else 0
    row("Taiwan cap", "One risk we can't diversify", f"≤ {pct(lim.taiwan_cap, 0)}", tw / lim.taiwan_cap,
        f"Now {pct(tw_held, 2)} ({pct(tw)} planned)")
    cap = lim.ai_cap.get(m.setting)
    ai_c = risk.ai_weight(book, m) if book else 0
    ai_h = sum(book.holdings[t] for t in book.holdings if m.is_ai(t)) / total if total else 0
    names = ", ".join(i.name for i in m.instruments if i.ai)
    row("AI-linked stocks", f"{names} count", f"≤ {pct(cap, 0)}", ai_c / cap,
        f"Now {pct(ai_h)} ({pct(ai_c)} planned)")
    n = risk.stock_count(book, m) if book else 0
    row("Number of stocks", "Few enough to follow closely", f"≤ {lim.max_single_stocks}",
        n / lim.max_single_stocks, f"Now {n}", "over" if n > lim.max_single_stocks else "ok")
    done = sum(1 for t in store.trades() if t["side"] == "buy" and t["note"].startswith("tranche"))
    planned = len(store.tranches("planned"))
    row("Tranches", f"Every buy split in {lim.tranches}", f"{lim.tranches} per position",
        done / (done + planned) if done + planned else 0,
        f"Now {done} filled · {planned} scheduled", "ok")
    slots = meeting_slots(store, m)
    row("Meetings", "Idea meetings per month", f"{slots['cap']} / month", slots["used"] / slots["cap"],
        f"Now {slots['used']} used")
    costs = [x["cost_usd"] or 0 for x in store.meetings(200)]
    top = max(costs, default=0.0)
    scripted = all(x["backend"] == "scripted" for x in store.meetings(200))
    row("AI cost", "Per meeting, when Claude runs", f"≤ ${lim.meeting_budget_usd:g}",
        top / lim.meeting_budget_usd, f"Now ${top:.2f}" + (" (scripted)" if scripted else " at most"))
    from . import budget
    used = budget.spent(store)
    row("AI budget", "All AI use this month: meetings, files, scout, alerts",
        f"≤ ${lim.monthly_ai_budget_usd:g} / month",
        used / lim.monthly_ai_budget_usd if lim.monthly_ai_budget_usd else 1.0, f"Now ${used:.2f}")
    return rows


def mandate_info(path: Path) -> dict:
    from . import paths
    if not path.exists():
        return {"file": "mandate.example.yaml", "edited": "", "own": False}
    mt = datetime.fromtimestamp(path.stat().st_mtime)
    return {"file": "mandate.yaml", "edited": f"{mt.day} {mt.strftime('%b')}", "own": path != paths.EXAMPLE_MANDATE}


# ------------------------------------------------------------------- stock

def stock(store: Store, pf: Portfolio, m: Mandate, steward: Steward, ticker: str) -> dict:
    """Everything about one name: why we hold or watch it, every call and how it went."""
    from . import learning
    from .data import DataError
    try:
        q = steward.quote(ticker)
    except DataError:
        q = None                     # a rejected name may have no price on hand
    inst, watch, rej = m.instrument(ticker), m.watch(ticker), m.rejection(ticker)
    cand = store.candidate(ticker)
    held = pf.quantities().get(ticker, 0.0)
    pos = next((p for p in pf.positions() if p.ticker == ticker), None)
    if held:
        status, cls = "Held", "buy"
    elif watch and not m.is_shortlisted(ticker):
        status, cls = "Watch list", "watch"
    elif rej:
        status, cls = "Rejected", "reject"
    elif cand:
        status, cls = f"Scout's idea · {CAND_LABEL.get(cand['status'], cand['status']).lower()}", \
            CAND_CLS.get(cand["status"], "notyet")
    else:
        status, cls = "In the model portfolio" if inst else "Not in the mandate", "notyet"

    meetings = [x for x in store.meetings(500) if x["ticker"] == ticker]
    last = next((x for x in meetings if x["status"] == "done" and x.get("decision")), None)
    latest = None
    if last:
        steps = store.steps(last["id"])
        thesis = next((s for s in steps if s["role"] == "thesis" and s["phase"] == "blind"), None)
        cio = next((s for s in steps if s["role"] == "cio"), None)
        latest = {"id": last["id"], "date": day_label(last["started_at"], year=True),
                  "d": last["decision"], "label": DECISION_LABEL[last["decision"]["decision"]],
                  "thesis": ((thesis or {}).get("data") or {}).get("output", {}).get("one_line"),
                  "breakers": ((thesis or {}).get("data") or {}).get("output", {}).get("thesis_breakers") or [],
                  "rationale": (cio or {}).get("body", ""), "backend": last["backend"]}
    outcomes = {r["meeting_id"]: r for r in learning.outcomes(store, m) if r["ticker"] == ticker}
    history = []
    for x in meetings:
        d = x.get("decision") or {}
        o = outcomes.get(x["id"])
        history.append({"id": x["id"], "date": day_label(x["started_at"], year=True), "kind": x["kind"],
                        "decision": d.get("decision"), "label": DECISION_LABEL.get(d.get("decision"), x["status"].capitalize()),
                        "grade": d.get("grade") or "", "action": d.get("action_label") or x.get("error") or "",
                        "outcome": o, "backend": x["backend"]})
    trigger = next((w for w in watch_rows(pf, steward.quotes(pf.tickers_needed())) if w["ticker"] == ticker), None)
    breakers = list(dict.fromkeys((inst.breakers if inst else []) + ((latest or {}).get("breakers") or [])))
    trades = [t for t in store.trades(ticker)]
    return {
        "ticker": ticker, "name": display_name(m, store, ticker),
        "theme": m.theme_of(ticker) or ((cand or {}).get("data", {}).get("idea") or {}).get("theme", ""), "q": q,
        "status": status, "status_cls": cls, "pos": pos, "target": m.target(ticker),
        "ai": m.is_ai(ticker), "taiwan": m.is_taiwan(ticker),
        "why": (latest or {}).get("thesis") or (inst.thesis if inst else (watch.thesis if watch else
                ((cand or {}).get("data", {}).get("idea") or {}).get("chokepoint", ""))),
        "why_source": "the latest meeting" if (latest or {}).get("thesis") else
                      ("the scout" if cand and not inst and not (watch and not m.is_shortlisted(ticker))
                       else "the October committee review"),
        "grade": (latest or {}).get("d", {}).get("grade") or (inst.grade if inst else (watch.grade if watch else "")),
        "committee_trigger": (inst.trigger if inst else (watch.event if watch else "")),
        "rejected": rej.reason if rej else "", "latest": latest, "history": history,
        "trigger": trigger, "breakers": breakers,
        "trades": [{**t, "date": day_label(t["ts"])} for t in reversed(trades)],
        "planned": [t for t in store.tranches("planned") if t["ticker"] == ticker],
        "postmortems": [p for p in store.postmortems() if p["ticker"] == ticker],
        "news": steward.news(ticker, 8), "news_live": steward.mode == "live",
        "in_universe": ticker in m.universe(),
        "quant": _quant(store, steward, m, ticker),
        "dossier": store.latest_dossier(ticker), "alerts": alert_rows(store, m, ticker),
        "candidate": cand, "VERDICT_LABEL": VERDICT_LABEL, "VERDICT_CLS": VERDICT_CLS,
    }


# ---------------------------------------------------------------- pipeline

def candidate_rows(store: Store, m: Mandate) -> list[dict]:
    out = []
    for c in store.candidates():
        i = c["data"].get("idea") or {}
        d = store.latest_dossier(c["ticker"])
        mt = next((x for x in store.meetings(500) if x["ticker"] == c["ticker"]), None)
        out.append({"ticker": c["ticker"], "name": c["name"], "theme": i.get("theme", ""),
                    "why": i.get("chokepoint", ""), "question": i.get("open_question", ""),
                    "status": c["status"], "label": CAND_LABEL.get(c["status"], c["status"]),
                    "cls": CAND_CLS.get(c["status"], "notyet"), "found": day_label(c["created_at"]),
                    "dossier": d, "meeting": mt, "backend": c["data"].get("backend", "")})
    return out


def dossier_rows(store: Store, m: Mandate, limit: int = 30) -> list[dict]:
    out = []
    for d in store.dossiers()[:limit]:
        o = (d.get("data") or {}).get("output") or {}
        out.append({"id": d["id"], "ticker": d["ticker"], "name": display_name(m, store, d["ticker"]),
                    "date": day_label(d["created_at"]), "status": d["status"], "backend": d["backend"],
                    "reason": d["reason"], "verdict": o.get("verdict"),
                    "label": VERDICT_LABEL.get(o.get("verdict"), d["status"].capitalize()),
                    "cls": VERDICT_CLS.get(o.get("verdict"), "notyet"), "cost": d["cost_usd"] or 0.0,
                    "summary": o.get("summary", "")})
    return out


def alert_rows(store: Store, m: Mandate, ticker: Optional[str] = None) -> list[dict]:
    out = []
    for a in store.alerts(ticker):
        chk = a["data"].get("check") or {}
        out.append({"id": a["id"], "ticker": a["ticker"], "name": display_name(m, store, a["ticker"]),
                    "date": day_label(a["created_at"]), "kind": a["kind"], "reason": a["reason"],
                    "status": a["status"], "label": ALERT_LABEL.get(a["status"], a["status"]),
                    "cls": ALERT_CLS.get(a["status"], "notyet"), "meeting_id": a.get("meeting_id"),
                    "what": chk.get("what_happened", ""), "memo": chk.get("memo", ""),
                    "breaker": chk.get("breaker_fired", ""), "backend": a.get("backend") or "",
                    "sources": a["data"].get("sources") or []})
    return out


def pipeline(store: Store, pf: Portfolio, m: Mandate, quotes: dict) -> dict:
    from . import review, scan, screening
    last = store.get_meta("last_screen")
    nxt = None
    if last:
        nxt = day_label((datetime.fromisoformat(last["at"]) +
                         timedelta(days=m.limits.screen_every_days)).isoformat())
    pms = [x for x in store.meetings(500) if x["kind"] == "portfolio"]
    cands = candidate_rows(store, m)
    return {
        "last_screen": last, "next_screen": nxt, "screen_due": screening.screen_due(store, m),
        "candidates": cands, "n_short": sum(1 for c in cands if c["status"] == "shortlisted"),
        "dossiers": dossier_rows(store, m), "research_left": screening.research_left(store, m),
        "agenda": scan.agenda(store, pf, m, quotes), "slots": meeting_slots(store, m),
        "holding_used": store.meetings_this_month("holding"),
        "alerts": alert_rows(store, m), "portfolio_meetings": [decision_row(x, m) for x in pms],
        "portfolio_due": review.due(store), "queue": store.get_meta("review_queue", []),
        "pending": proposal_rows(store, m),
        "universe": [(t, m.name_of(t)) for t in m.universe()          # stocks only: funds need no file
                     if m.is_stock(t) and not store.candidate(t)],
    }


def dossier_page(store: Store, m: Mandate, did: int) -> Optional[dict]:
    d = store.dossier(did)
    if not d:
        return None
    o = (d.get("data") or {}).get("output") or {}
    others = [x for x in store.dossiers(d["ticker"]) if x["id"] != did]
    return {"d": d, "o": o, "name": display_name(m, store, d["ticker"]),
            "facts": (d.get("data") or {}).get("facts") or {},
            "sources": (d.get("data") or {}).get("sources") or [],
            "label": VERDICT_LABEL.get(o.get("verdict"), d["status"].capitalize()),
            "cls": VERDICT_CLS.get(o.get("verdict"), "notyet"),
            "date": day_label(d["created_at"], year=True), "others": others,
            "candidate": store.candidate(d["ticker"])}


PM_STEPS = [("facts", "The whole book", "Code"), ("risk", "Risk", "RO"), ("challenge", "Weakest", "RT"),
            ("decision", "Ranking", "CIO"), ("rules", "Swap rules", "Code"), ("approval", "Your OK", "You")]


def portfolio_meeting(store: Store, m: Mandate, mid: int) -> dict:
    mt = store.meeting(mid)
    steps = store.steps(mid)
    get = lambda phase, role: next((x for x in steps if x["phase"] == phase and x["role"] == role), None)
    facts, risk_s = get("facts", "code"), get("committee", "risk")
    red, cio, rules = get("committee", "redteam"), get("committee", "cio"), get("rules", "code")
    proposal = next(iter(store.proposals(meeting_id=mid)), None)
    present = {"facts": bool(facts), "risk": bool(risk_s), "challenge": bool(red), "decision": bool(cio),
               "rules": bool(rules), "approval": bool(rules) and (proposal is None or proposal["status"] != "pending")}
    running = mt["status"] == "running"
    first_missing = next((k for k, *_ in PM_STEPS if not present[k]), None)
    waiting = proposal is not None and proposal["status"] == "pending"
    stepper = [{"key": k, "label": lab, "sub": sub,
                "state": "done" if present[k] else ("now" if (running and k == first_missing)
                                                    or (waiting and k == "approval") else "")}
               for k, lab, sub in PM_STEPS]
    f = (facts or {}).get("data") or {}
    rows = {r["ticker"]: r for r in f.get("stocks", [])}
    ranking = []
    if cio:
        for v in cio["data"]["output"].get("ranking") or []:
            r = rows.get(v["ticker"], {})
            ranking.append({**v, "name": m.name_of(v["ticker"]), "weight": r.get("weight"),
                            "gain": r.get("gain_pct"), "grade": r.get("grade"), "ai": r.get("ai")})
    scen = None
    if risk_s:
        o = risk_s["data"]["output"]
        biggest = max((abs(x["stock_move_pct"]) for x in o.get("scenarios") or []), default=1) or 1
        scen = [{"name": x["scenario"], "move": x["stock_move_pct"],
                 "width": abs(x["stock_move_pct"]) / biggest * 100} for x in o.get("scenarios") or []]
    return {"mt": mt, "title": "Portfolio meeting", "subtitle": "The whole book, once a month",
            "when": f"{day_label(mt['started_at'], year=True)}, {time_label(mt['started_at'])}",
            "stepper": stepper, "facts": f, "facts_body": (facts or {}).get("body", ""),
            "risk": (risk_s or {}).get("data", {}).get("output") if risk_s else None, "scen": scen,
            "risk_body": (risk_s or {}).get("body", ""),
            "red": (red or {}).get("data", {}).get("output") if red else None,
            "cio": (cio or {}).get("data", {}).get("output") if cio else None, "cio_body": (cio or {}).get("body", ""),
            "ranking": ranking, "rules": (rules or {}).get("data"), "proposal": proposal,
            "running": running, "verdict": None,
            "stopped": [x for x in steps if x["phase"] == "rules" and not x.get("data")]}


def _quant(store: Store, steward: Steward, m: Mandate, ticker: str) -> dict:
    from . import quant
    try:
        return quant.Desk(steward, store).stock(ticker, m.benchmark)
    except Exception:
        return {}
