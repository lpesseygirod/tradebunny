"""How the council learns: by checking past calls against what happened.

The scorekeeper from the committee design, with its warning in mind: with a
few decisions a year, an AI that "learns from experience" mostly learns from
luck. So nothing here changes a rule or a prompt by itself. It measures,
explains and proposes lessons that only I can approve.

  1. Tracking. Every decision is marked on the day it was made and again
     after about 1, 3, 6 and 12 months, against the same money in MSCI
     World. Refused names count too: avoiding a stock that then beats the
     index by 20 points is a mistake, just a quiet one.
  2. Judging. Each mark is right, wrong or too close to call.
  3. Post-mortems when a decision clearly went wrong: what happened, did the
     council see it coming (red team named it? CIO answered it?), and was it
     bad judgment, bad luck or just too early.
  4. Lessons. A post-mortem may propose one or two. I approve or reject them;
     approved lessons are read by every member before every meeting.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

from . import agents
from .agents import ROLES, Role, RoleRefused
from .data import DataError, Steward
from .mandate import Mandate
from .store import Store, now

HORIZONS = [30, 91, 182, 365]
NOISE_PTS = 2.0          # within ±2 points of the index: too close to call
BAD_PTS = -15.0          # a buy this far behind the index gets a post-mortem
MISSED_PTS = 20.0        # a refused name this far ahead gets one too

BULLISH = {"buy", "add", "hold"}
BEARISH = {"watch", "reject", "sell", "trim"}


# -------------------------------------------------------------- tracking

def record_start(store: Store, steward: Steward, m: Mandate, meeting_id: int, ticker: str) -> None:
    """Mark the day of the decision: the stock and the index, in euros."""
    try:
        q = steward.quotes([ticker, m.benchmark])
    except DataError:
        return
    store.add_mark(meeting_id, 0, now()[:10], q[ticker].price_eur, q[m.benchmark].price_eur)


def _age_days(day: str) -> float:
    return (datetime.now(timezone.utc).date() - datetime.fromisoformat(day).date()).days


def update_marks(store: Store, steward: Steward, m: Mandate) -> int:
    """Add the 1, 3, 6 and 12-month marks that are due. Returns how many.

    Taken at the first daily review after a horizon passes, so a "3-month"
    mark uses the price of the first run after day 91.
    """
    starts = {x["meeting_id"]: x for x in store.marks() if x["horizon_days"] == 0}
    have = {(x["meeting_id"], x["horizon_days"]) for x in store.marks()}
    due = [(mid, h) for mid, s in starts.items() for h in HORIZONS
           if (mid, h) not in have and _age_days(s["day"]) >= h]
    if not due:
        return 0
    tickers = {store.meeting(mid)["ticker"] for mid, _ in due}
    try:
        q = steward.quotes(list(tickers) + [m.benchmark])
    except DataError:
        return 0
    for mid, h in due:
        t = store.meeting(mid)["ticker"]
        store.add_mark(mid, h, now()[:10], q[t].price_eur, q[m.benchmark].price_eur)
    return len(due)


# --------------------------------------------------------------- judging

def excess_pts(start: dict, mark: dict) -> tuple[float, float, float]:
    """(stock return, index return, stock minus index), in percentage points."""
    s = (mark["price_eur"] / start["price_eur"] - 1) * 100
    b = (mark["bench_eur"] / start["bench_eur"] - 1) * 100
    return s, b, s - b


def judge(decision: str, excess: float) -> str:
    """right | wrong | noise. Bought names should beat the index; refused or
    sold names should not."""
    if abs(excess) < NOISE_PTS:
        return "noise"
    if decision in BULLISH:
        return "right" if excess > 0 else "wrong"
    return "right" if excess < 0 else "wrong"


def outcomes(store: Store, m: Mandate) -> list[dict]:
    """Every tracked decision with its latest mark and the verdict on it."""
    by: dict[int, list[dict]] = {}
    for x in store.marks():
        by.setdefault(x["meeting_id"], []).append(x)
    out = []
    for mid, marks in by.items():
        mt = store.meeting(mid)
        d = (mt or {}).get("decision") or {}
        if not d.get("decision"):
            continue
        start = next((x for x in marks if x["horizon_days"] == 0), None)
        later = [x for x in marks if x["horizon_days"] > 0]
        row = {"meeting_id": mid, "ticker": mt["ticker"], "name": m.name_of(mt["ticker"]),
               "decision": d["decision"], "grade": d.get("grade"), "date": start["day"] if start else "",
               "marks": {}, "latest": None, "verdict": "pending"}
        for x in later:
            s, b, e = excess_pts(start, x)
            row["marks"][x["horizon_days"]] = {"stock": s, "index": b, "excess": e, "day": x["day"]}
        if later:
            h = max(row["marks"])
            row["latest"] = {"horizon": h, **row["marks"][h]}
            row["verdict"] = judge(d["decision"], row["latest"]["excess"])
        out.append(row)
    return sorted(out, key=lambda r: -r["meeting_id"])


def due_postmortems(store: Store, m: Mandate) -> list[dict]:
    """Decisions that clearly went wrong and have not been examined yet."""
    done = {p["meeting_id"] for p in store.postmortems() if p["status"] != "failed"}
    out = []
    for r in outcomes(store, m):
        if r["meeting_id"] in done or not r["latest"]:
            continue
        e = r["latest"]["excess"]
        months = round(r["latest"]["horizon"] / 30.4)
        if r["decision"] in ("buy", "add", "hold") and e <= BAD_PTS:
            out.append({**r, "reason": f"Behind the index by {abs(e):.0f} points after {months} months"})
        elif r["decision"] in ("watch", "reject") and e >= MISSED_PTS:
            out.append({**r, "reason": f"Refused, then beat the index by {e:.0f} points after {months} months"})
    for mt in store.meetings(500):          # a breaker that fired is examined too
        d = mt.get("decision") or {}
        if d.get("breaker_triggered") and mt["id"] not in done and mt["id"] not in {x["meeting_id"] for x in out}:
            out.append({"meeting_id": mt["id"], "ticker": mt["ticker"], "name": m.name_of(mt["ticker"]),
                        "decision": d["decision"], "reason": f"Thesis breaker fired: {d['breaker_triggered']}"})
    return out


# ----------------------------------------------------------- post-mortem

class PostMortem(BaseModel):
    what_happened: str = Field(description="Two or three sentences, with dates and sources from your research")
    cause: str = Field(description="The main reason it went this way, in one sentence")
    foreseen: Literal["yes", "partly", "no"] = Field(description="Did anyone on the council name this risk or chance?")
    foreseen_by: str = Field(description="Which member and where, e.g. 'Red team, objection 2', or empty")
    cio_weighed_it: Literal["yes", "dismissed", "not_raised"] = Field(
        description="Did the CIO's answers take it seriously, wave it away, or was it never raised?")
    judgment: Literal["bad_judgment", "bad_luck", "too_early", "right_after_all"] = Field(
        description="bad_judgment: the evidence was there; bad_luck: unforeseeable; too_early: the thesis may still play out")
    lessons: list[str] = Field(description="Zero to two lessons, each one sentence the council can apply next time. "
                                           "Only lessons that would have changed the decision; none if it was luck.")
    memo: str = Field(description="Markdown, under 200 words")


AUDITOR = Role(
    "auditor", "Post-mortem auditor", "PM", agents.OPUS, True,
    "You run the council's post-mortems. You read a past decision exactly as it was "
    "recorded (memos, objections, verdict) and what happened since, research what "
    "actually drove the result, and judge it honestly. Separate judgment from luck: a "
    "sound decision can lose and a careless one can win. Check whether the risk that "
    "happened was named at the time, by whom, and whether the CIO answered it or "
    "dismissed it. Propose a lesson only if it would have changed the decision; never "
    "a lesson from one unlucky outcome. " + agents.GUARD)


def _record_text(store: Store, mid: int) -> str:
    parts = []
    for s in store.steps(mid):
        if s["phase"] in ("blind", "cross", "committee", "vote"):
            out = (s.get("data") or {}).get("output")
            parts.append(f"### {ROLES.get(s['role'], AUDITOR).title}: {s['title']}\n"
                         + json.dumps(out, indent=1, ensure_ascii=False))
    return "\n\n".join(parts)


def _scripted(case: dict, objections: list[dict]) -> PostMortem:
    return PostMortem(
        what_happened=f"{case['name']}: {case['reason'].lower()} (scripted: no research).",
        cause="Not researched: scripted post-mortems cannot know what happened.",
        foreseen="partly" if objections else "no",
        foreseen_by=f"Red team, objection 1: {objections[0]['claim']}" if objections else "",
        cio_weighed_it="yes" if objections else "not_raised",
        judgment="too_early", lessons=[],
        memo="Scripted post-mortem: it lists what the council recorded at the time. "
             "With an AI key, the auditor researches what really happened and proposes lessons.")


def run_postmortem(store: Store, steward: Steward, m: Mandate, case: dict,
                   backend: agents.Backend) -> int:
    mid = case["meeting_id"]
    pid = store.add_postmortem(mid, case["ticker"], case["reason"], backend.name)
    mt = store.meeting(mid)
    obj_step = next((s for s in store.steps(mid) if s["phase"] == "cross" and s["role"] == "redteam"), None)
    objections = (obj_step or {}).get("data", {}).get("output", {}).get("objections", []) if obj_step else []
    marks = [{"after_days": x["horizon_days"], "day": x["day"], "price_eur": round(x["price_eur"], 2),
              "index_eur": round(x["bench_eur"], 2)} for x in store.marks(mid)]
    facts = {"decision": mt.get("decision"), "decided_on": mt["started_at"][:10],
             "why_examined": case["reason"], "marks": marks}
    try:
        if backend.name == "scripted":
            out, sources, cost = _scripted(case, objections), [], 0.0
        else:
            prompt = (f"# Post-mortem: {case['name']} ({case['ticker']})\n\n"
                      f"## What happened since (code-measured, euros)\n{json.dumps(facts, indent=1)}\n\n"
                      f"## The meeting as it was recorded\n{_record_text(store, mid)}\n\n"
                      "Research what drove the result, then judge the decision.")
            res = backend.run(AUDITOR, prompt, PostMortem, {"task": "postmortem"})
            out, sources, cost = res.output, res.sources, res.cost_usd
        out.lessons = [x.strip() for x in out.lessons if x.strip()][:2]
        for text in out.lessons:
            store.add_lesson(pid, text)
        store.finish_postmortem(pid, "done", {"facts": facts, "output": out.model_dump(),
                                              "sources": [s.model_dump() for s in sources]}, cost)
    except (RoleRefused, DataError, Exception) as e:          # recorded, never fatal
        store.finish_postmortem(pid, "failed", {"facts": facts, "error": f"{type(e).__name__}: {e}"})
    return pid


def postmortems_this_month(store: Store) -> int:
    month = now()[:7]
    return sum(1 for p in store.postmortems() if p["created_at"].startswith(month) and p["status"] != "failed")


# --------------------------------------------------------------- lessons

def lessons_text(store: Store) -> str:
    """What every member reads before a meeting: only lessons I approved."""
    approved = store.lessons("approved")
    if not approved:
        return ""
    return "\n".join(f"- {x['text']}" for x in approved)


def history_text(store: Store, m: Mandate, ticker: str, before: Optional[int] = None) -> str:
    """The council's past calls on this name and how they turned out."""
    rows = [r for r in outcomes(store, m) if r["ticker"] == ticker and r["meeting_id"] != before][:3]
    lines = []
    for r in rows:
        if r["latest"]:
            lt = r["latest"]
            res = (f"after {round(lt['horizon'] / 30.4)} months: stock {lt['stock']:+.1f}%, "
                   f"index {lt['index']:+.1f}%, {r['verdict']}")
        else:
            res = "too recent to judge"
        lines.append(f"- {r['date']}: {r['decision']} (grade {r['grade']}), {res}")
    return "\n".join(lines)


# ------------------------------------------------------------- scorecard

def _brier(pairs: list[tuple[float, bool]]) -> Optional[float]:
    if not pairs:
        return None
    return sum((p - (1.0 if hit else 0.0)) ** 2 for p, hit in pairs) / len(pairs)


def scorecard(store: Store, m: Mandate) -> dict:
    rows = [r for r in outcomes(store, m) if r["latest"]]
    judged = [r for r in rows if r["verdict"] != "noise"]
    right = sum(1 for r in judged if r["verdict"] == "right")
    bought = [r["latest"]["excess"] for r in rows if r["decision"] in ("buy", "add")]
    refused = [r["latest"]["excess"] for r in rows if r["decision"] in ("watch", "reject")]
    avg = lambda xs: sum(xs) / len(xs) if xs else None

    # Thesis analyst: does "70% likely to hold" come true about 70% of the time?
    thesis_pairs, val = [], {"no": [], "partly": [], "yes": []}
    for r in rows:
        for s in store.steps(r["meeting_id"]):
            out = (s.get("data") or {}).get("output") or {}
            if s["role"] == "thesis" and s["phase"] == "blind" and "probability_holds_12m" in out:
                thesis_pairs.append((out["probability_holds_12m"] / 100, r["latest"]["excess"] >= 0))
            if s["role"] == "valuation" and out.get("priced_in") in val:
                val[out["priced_in"]].append(r["latest"]["excess"])

    pms = [p for p in store.postmortems() if p["status"] == "done"]
    seen = [p for p in pms if p["data"]["output"]["foreseen"] in ("yes", "partly")]
    by_red = [p for p in seen if "red team" in p["data"]["output"]["foreseen_by"].lower()]
    dismissed = [p for p in pms if p["data"]["output"]["cio_weighed_it"] == "dismissed"]
    judgments: dict[str, int] = {}
    for p in pms:
        k = p["data"]["output"]["judgment"]
        judgments[k] = judgments.get(k, 0) + 1

    return {
        "measured": len(rows), "judged": len(judged), "right": right,
        "hit_rate": right / len(judged) if judged else None,
        "bought_avg": avg(bought), "refused_avg": avg(refused),
        "n_bought": len(bought), "n_refused": len(refused),
        "thesis_brier": _brier(thesis_pairs), "n_thesis": len(thesis_pairs),
        "val": {k: avg(v) for k, v in val.items()}, "val_n": {k: len(v) for k, v in val.items()},
        "postmortems": len(pms), "foreseen": len(seen), "by_red": len(by_red),
        "dismissed": len(dismissed), "judgments": judgments,
        "enough": len(judged) >= 10,
    }


# ------------------------------------------------------------- forecasts

FORECAST_DAYS = {"stock_vs_index_3m": 91, "stock_vs_index_12m": 365}
QUARTER_GIVE_UP_DAYS = 200          # no new quarter by then: the forecast is void


def _value(store: Store, desk, p: dict) -> tuple[Optional[float], str]:
    """The measured value for a forecast, or (None, why not yet)."""
    if p["metric"] in FORECAST_DAYS:
        h = FORECAST_DAYS[p["metric"]]
        marks = {x["horizon_days"]: x for x in store.marks(p["meeting_id"])}
        if 0 in marks and h in marks:
            return excess_pts(marks[0], marks[h])[2], f"measured at the {h}-day mark"
        return None, "not due"
    q = desk.quarters(p["ticker"]) if desk else {}
    later = sorted(d for d in q if d > p["made_on"])
    if not later:
        return None, "no new quarter yet"
    d = later[0]
    cur = q[d]
    if p["metric"] == "op_margin_next_q":
        if cur.get("op") is None or not cur.get("revenue"):
            return None, "no operating income reported"
        return cur["op"] / cur["revenue"] * 100, f"quarter ending {d}"
    year_ago = [x for x in q if x[:4] == str(int(d[:4]) - 1) and x[5:7] == d[5:7]]
    if not year_ago or not q[year_ago[0]].get("revenue"):
        return None, "no year-ago quarter to compare"
    return (cur["revenue"] / q[year_ago[0]]["revenue"] - 1) * 100, f"quarter ending {d} vs {year_ago[0]}"


def resolve_forecasts(store: Store, steward: Steward, m: Mandate) -> int:
    """Score every open forecast whose answer is known. Returns how many."""
    from . import quant
    desk = quant.Desk(steward, store)
    n = 0
    for p in store.predictions("open"):
        try:
            v, why = _value(store, desk, p)
        except Exception:
            continue
        if v is None:
            age = _age_days(p["made_on"])
            if p["metric"] not in FORECAST_DAYS and age > QUARTER_GIVE_UP_DAYS:
                store.resolve_prediction(p["id"], "void", None, why)
                n += 1
            continue
        hit = v > p["threshold"] if p["above"] else v < p["threshold"]
        store.resolve_prediction(p["id"], "hit" if hit else "miss", v, why)
        n += 1
    return n


def forecast_scores(store: Store) -> dict[str, dict]:
    """Per member: forecasts resolved, hit rate, Brier score (0 perfect, 0.25 a coin flip)."""
    out: dict[str, dict] = {}
    for p in store.predictions():
        r = out.setdefault(p["role"], {"made": 0, "resolved": 0, "hits": 0, "pairs": [], "open": 0})
        r["made"] += 1
        if p["status"] == "open":
            r["open"] += 1
        elif p["status"] in ("hit", "miss"):
            r["resolved"] += 1
            r["hits"] += p["status"] == "hit"
            r["pairs"].append((p["probability"] / 100, p["status"] == "hit"))
    for r in out.values():
        r["brier"] = _brier(r.pop("pairs"))
        r["hit_rate"] = r["hits"] / r["resolved"] if r["resolved"] else None
    return out


def forecast_record_text(store: Store, minimum: int = 5) -> str:
    """For the fact sheet: only members with enough resolved forecasts to mean something."""
    lines = []
    for role, r in sorted(forecast_scores(store).items()):
        if r["resolved"] >= minimum and r["brier"] is not None:
            lines.append(f"- {ROLES[role].title}: {r['resolved']} forecasts resolved, {r['hits']} came true, "
                         f"Brier {r['brier']:.2f} (0 is perfect, 0.25 is a coin flip)")
    return "\n".join(lines)


# --------------------------------------------------------------- my calls

def your_calls(store: Store, m: Mandate) -> list[dict]:
    """Every proposal I answered, with my reason and how the stock did since.
    Approving a buy or rejecting a sale is a bullish call; the reverse is bearish."""
    marks = {r["meeting_id"]: r for r in outcomes(store, m)}
    out = []
    for p in store.proposals():
        res = p.get("result") or {}
        if p["status"] not in ("approved", "rejected") or res.get("by", "you") != "you" or not p["meeting_id"]:
            continue
        if p["action"] not in ("buy", "add", "sell", "trim"):
            continue
        bullish = (p["status"] == "approved") == (p["action"] in ("buy", "add"))
        o = marks.get(p["meeting_id"]) or {}
        latest = o.get("latest")
        verdict = "pending"
        if latest:
            verdict = judge("buy" if bullish else "reject", latest["excess"])
        out.append({"id": p["id"], "meeting_id": p["meeting_id"], "name": m.name_of(p["ticker"]),
                    "summary": p["summary"], "status": p["status"], "why": res.get("why") or "",
                    "decided": (p.get("decided_at") or "")[:10], "latest": latest, "verdict": verdict})
    return out
