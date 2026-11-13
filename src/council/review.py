"""The monthly portfolio meeting: the whole book, not one stock.

With the portfolio full (10 of 10 stocks), a new idea only makes sense if
it beats something already owned, so once a month the council looks at
everything together:

    facts       code: holdings with weight, gain, latest grade and research
                file; the bench (buy verdicts blocked only by the 10-stock
                limit); the shortlist
    risk        concentration, holdings that are really one bet, what each
                scenario does to the WHOLE portfolio
    challenge   the red team names the weakest holdings
    decision    the CIO ranks every holding keep / review / replace, may
                propose ONE swap, and picks holdings to review next
    rules       code checks the swap

The meeting never invents a trade. A swap only pairs two verdicts the
committee already reached:

  - the name sold was graded C or D at its latest review, or has a thesis
    breaker on record (a price drop alone never sells)
  - the name bought has a buy verdict (grade A, or B on its trigger) from
    the last 90 days, still valid at today's price
  - after the sale, the risk engine allows the buy

A holding the CIO wants to replace but that isn't marked down yet goes on
the review list instead. A swap is a normal proposal: on my book it waits
for my approval.
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

from . import agents, approvals, budget, quant, risk
from .agents import ROLES, ScenarioMove
from .data import Steward
from .mandate import Mandate
from .portfolio import Portfolio
from .store import Store, now

TICKER = "PORTFOLIO"
BENCH_DAYS = 90
MAX_QUEUE = 3                    # holdings sent back to the committee per meeting


# ----------------------------------------------------------------- answers

class BookMemo(BaseModel):
    concentration: str = Field(description="One or two sentences: where the money is concentrated")
    same_bets: list[str] = Field(description="Groups of holdings that are really one bet, e.g. "
                                             "'ASML, TSMC, Prysmian: AI capex'")
    scenarios: list[ScenarioMove] = Field(description="Three to five worlds, base case first, and the WHOLE "
                                                      "portfolio's move in each, in percent")
    weakest_link: str = Field(description="The holding that adds the most risk for its expected return")
    memo: str = Field(description="Markdown, under 200 words")


class Weak(BaseModel):
    ticker: str
    why: str = Field(description="One sentence with evidence")


class Challenge(BaseModel):
    weakest: list[Weak] = Field(description="Up to three held stocks, weakest first")
    best_outside: str = Field(description="The best name the council does not own, from the bench or shortlist "
                                          "given, and why; or 'none'")
    memo: str = Field(description="Markdown, under 150 words")


class HoldingView(BaseModel):
    ticker: str
    verdict: Literal["keep", "review", "replace"] = Field(
        description="keep: still earns its place; review: the committee should look again; replace: "
                    "sell it for a bench name")
    reason: str = Field(description="One sentence")


class PortfolioDecision(BaseModel):
    ranking: list[HoldingView] = Field(description="Every single stock held, strongest first")
    swap_sell: str = Field(description="The ticker to sell in a swap, or empty for no swap")
    swap_buy: str = Field(description="The bench ticker to buy in its place, or empty")
    swap_reason: str = Field(description="Why the swap improves the portfolio; empty if none")
    review_next: list[str] = Field(description="Up to three held tickers for the committee to review next")
    memo: str = Field(description="Markdown, under 200 words")


# ------------------------------------------------------------------- facts

def latest_verdict(store: Store, ticker: str) -> Optional[dict]:
    mt = next((x for x in store.meetings(500) if x["ticker"] == ticker and x["status"] == "done"
               and x.get("decision")), None)
    if not mt:
        return None
    return {"meeting_id": mt["id"], "date": mt["started_at"][:10], **mt["decision"],
            "blocked_by": (mt.get("outcome") or {}).get("blocked_by") or []}


def bench(store: Store, steward: Steward, m: Mandate, pf: Portfolio) -> list[dict]:
    """Names a committee wanted to buy, blocked only by the stock limit."""
    since = (datetime.now(timezone.utc) - timedelta(days=BENCH_DAYS)).isoformat(timespec="seconds")
    held = pf.quantities()
    out, seen = [], set()
    for mt in store.meetings(500):
        t = mt["ticker"]
        if t in seen or t in held or t == TICKER or mt["status"] != "done":
            continue
        seen.add(t)                       # only the latest verdict on each name counts
        d = mt.get("decision") or {}
        if mt["started_at"] < since or d.get("decision") not in ("buy", "add"):
            continue
        blocked = (mt.get("outcome") or {}).get("blocked_by") or []
        if blocked and not all("single stocks" in b for b in blocked):
            continue
        rules, _ = risk.policy(t, d, m, steward.quote(t))
        out.append({"ticker": t, "name": m.name_of(t), "grade": d.get("grade"),
                    "conviction": d.get("conviction"), "meeting_id": mt["id"], "date": mt["started_at"][:10],
                    "still_valid": all(c.ok for c in rules if c.blocking),
                    "why_blocked": "; ".join(blocked) or "not blocked"})
    return out


def facts(store: Store, steward: Steward, m: Mandate, pf: Portfolio) -> dict:
    quotes = steward.quotes(pf.tickers_needed())
    book = pf.book(quotes)
    desk = quant.Desk(steward, store)
    rows = []
    for p in pf.positions(quotes):
        if not m.is_stock(p.ticker):
            continue
        v = latest_verdict(store, p.ticker)
        rf = store.latest_dossier(p.ticker)
        i = m.instrument(p.ticker)
        rows.append({"ticker": p.ticker, "name": p.name, "weight": round(book.weight(p.ticker), 4),
                     "target": round(p.target, 4), "gain_pct": round(p.gain_pct, 4),
                     "ai": m.is_ai(p.ticker), "taiwan": m.is_taiwan(p.ticker),
                     "grade": (v or {}).get("grade") or (i.grade if i else None),
                     "grade_from": f"meeting #{v['meeting_id']} on {v['date']}" if v else "the October review",
                     "last_decision": (v or {}).get("decision"),
                     "breaker_fired": (v or {}).get("breaker_triggered") or "",
                     "research_file": (((rf or {}).get("data") or {}).get("output") or {}).get("verdict"),
                     "thesis": i.thesis if i else "", **_risk_numbers(desk, p.ticker, m.benchmark)})
    other = {"index": 0.0, "bonds": 0.0, "gold": 0.0}
    for t, val in book.holdings.items():
        i = m.instrument(t)
        if i and i.bucket in other:
            other[i.bucket] += val / book.total
    try:
        bk = desk.book({t: v / book.total for t, v in book.holdings.items() if v > 0})
    except Exception:
        bk = {}
    return {"total_eur": round(book.total, 2), "cash": round(book.cash / book.total, 4),
            "book_vol": bk.get("vol"), "book_var_1m": bk.get("var_1m"), "book_weeks": bk.get("weeks"),
            "other": {k: round(v, 4) for k, v in other.items()},
            "stocks": rows, "stock_count": risk.stock_count(book, m),
            "max_stocks": m.limits.max_single_stocks,
            "ai_weight": round(risk.ai_weight(book, m), 4), "ai_cap": m.limits.ai_cap.get(m.setting),
            "bench": bench(store, steward, m, pf),
            "shortlist": [{"ticker": w.ticker, "name": w.name, "theme": w.theme} for w in m.shortlist()],
            "setting": m.setting}


def _risk_numbers(desk: quant.Desk, ticker: str, benchmark: str) -> dict:
    try:
        q = desk.stock(ticker, benchmark)
    except Exception:
        q = {}
    return {k: q.get(k) for k in ("vol_1y", "beta", "max_drawdown_3y", "pe_vs_avg", "implied_growth")}


def facts_md(f: dict, m: Mandate) -> str:
    lines = [f"# Portfolio meeting, {now()[:10]}",
             f"Paper portfolio €{f['total_eur']:,.0f}; {f['stock_count']} of {f['max_stocks']} single stocks; "
             f"AI-linked {f['ai_weight'] * 100:.1f}% of a {f['ai_cap'] * 100:.0f}% cap; cash "
             f"{f['cash'] * 100:.1f}%; index {f['other']['index'] * 100:.1f}%, bonds "
             f"{f['other']['bonds'] * 100:.1f}%, gold {f['other']['gold'] * 100:.1f}%.",
             ("The holdings as they are now (quant desk, weekly prices over about 2 years): volatility "
              f"{f['book_vol'] * 100:.0f}% a year; in the worst 5% of months the book lost "
              f"{abs(f['book_var_1m']) * 100:.1f}% or more." if f.get("book_vol") and f.get("book_var_1m") is not None
              else "No portfolio volatility yet: not enough price history."),
             "", "## Single stocks held",
             "| Stock | Weight (with planned tranches) | Target | Gain | Grade | From | Research file | Breaker fired "
             "| Volatility | Beta | P/E vs own average | Growth the price implies |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    f1 = lambda x, d=0, sign=False: "n/a" if x is None else (f"{x * 100:+.{d}f}%" if sign else f"{x * 100:.{d}f}%")
    for r in f["stocks"]:
        lines.append(f"| {r['name']} ({r['ticker']}) | {r['weight'] * 100:.1f}% | {r['target'] * 100:.1f}% | "
                     f"{r['gain_pct'] * 100:+.1f}% | {r['grade'] or '-'} | {r['grade_from']} | "
                     f"{r['research_file'] or '-'} | {r['breaker_fired'] or '-'} | {f1(r.get('vol_1y'))} | "
                     f"{'n/a' if r.get('beta') is None else format(r['beta'], '.2f')} | "
                     f"{f1(r.get('pe_vs_avg'), 0, True)} | {f1(r.get('implied_growth'), 1)} |")
    lines += ["", "## The bench: committee buy verdicts blocked only by the stock limit"]
    lines += [f"- {b['name']} ({b['ticker']}): grade {b['grade']}, meeting #{b['meeting_id']} on {b['date']}"
              + ("" if b["still_valid"] else " (its trigger is no longer met at today's price)")
              for b in f["bench"]] or ["- none"]
    lines += ["", "## The scout's shortlist (not yet graded by a committee: cannot be bought from here)"]
    lines += [f"- {s['name']} ({s['ticker']}): {s['theme']}" for s in f["shortlist"]] or ["- none"]
    lines += ["", "## Swap rules (enforced by code)",
              "A swap sells a holding graded C or D at its latest review, or one whose thesis breaker "
              "fired, and buys a bench name. Anything else goes to the review list first."]
    return "\n".join(lines)


# ---------------------------------------------------------------- scripted

GRADE_RANK = {"A": 0, "B": 1, None: 2, "": 2, "not_graded": 2, "C": 3, "D": 4}


def _scripted_book(f: dict) -> BookMemo:
    """The whole book's risk, computed: each stock's move scaled by its beta."""
    stocks = f["stocks"]
    beta = lambda r: max(0.3, min(r.get("beta") if r.get("beta") is not None else 1.0, 2.5))
    o = f["other"]

    def world(stock_move, ai_move=None, index=0.0, bonds=0.0, gold=0.0):
        pts = sum(r["weight"] * ((ai_move if (ai_move is not None and r["ai"]) else stock_move) * beta(r))
                  for r in stocks)
        return round(pts + o["index"] * index + o["bonds"] * bonds + o["gold"] * gold)
    top = sorted(stocks, key=lambda r: -r["weight"])[:3]
    ai = [r["name"] for r in stocks if r["ai"]]
    riskiest = max(stocks, key=lambda r: r["weight"] * beta(r) * (r.get("vol_1y") or 0.3), default=None)
    return BookMemo(
        concentration="Largest: " + ", ".join(f"{r['name']} {r['weight'] * 100:.1f}%" for r in top) + ".",
        same_bets=[f"AI capex: {', '.join(ai)}"] if len(ai) > 1 else [],
        scenarios=[ScenarioMove(scenario="Base case", stock_move_pct=world(6, index=6, bonds=3, gold=2)),
                   ScenarioMove(scenario="Markets fall 20%", stock_move_pct=world(-20, index=-20, bonds=2, gold=5)),
                   ScenarioMove(scenario="AI spending cut", stock_move_pct=world(-5, ai_move=-30, index=-8)),
                   ScenarioMove(scenario="Recession", stock_move_pct=world(-25, index=-22, bonds=4, gold=6)),
                   ScenarioMove(scenario="Rates up", stock_move_pct=world(-10, index=-10, bonds=-3, gold=-2))],
        weakest_link=(f"{riskiest['name']}: the most risk for its weight (size x beta x volatility)."
                      if riskiest else "-"),
        memo="Computed by code, not an AI: each stock's move is scaled by its beta, AI-linked stocks fall 30% "
             "in an AI spending cut, and the index, bonds and gold move by fixed amounts.")


def _scripted_challenge(f: dict) -> Challenge:
    """The weakest holdings, computed: lowest grade first, then the most expensive
    against their own history, then the weakest momentum."""
    rank = lambda r: (-GRADE_RANK.get(r["grade"], 2), -(r.get("pe_vs_avg") or 0), r["gain_pct"])
    weak = sorted(f["stocks"], key=rank)[:3]
    return Challenge(weakest=[_weak(r) for r in weak],
                     best_outside=(f["bench"][0]["name"] if f["bench"] else "none"),
                     memo="Computed by code: the lowest grades on file, then the stocks priced furthest "
                          "above their own average P/E.")


def _weak(r: dict) -> Weak:
    pe = r.get("pe_vs_avg")
    return Weak(ticker=r["ticker"], why=f"Grade {r['grade'] or 'none'} on file"
                + (f"; P/E {pe * 100:+.0f}% vs its own average" if pe is not None else "")
                + f"; {r['gain_pct'] * 100:+.1f}% since bought.")


def _scripted_decision(f: dict) -> PortfolioDecision:
    ranked = sorted(f["stocks"], key=lambda r: (GRADE_RANK.get(r["grade"], 2), -r["gain_pct"]))
    views = []
    for r in ranked:
        g = r["grade"]
        verdict = "keep" if g in ("A", "B") else "review" if g in (None, "", "not_graded") else "replace"
        views.append(HoldingView(ticker=r["ticker"], verdict=verdict, reason=f"Grade {g or 'none'} on file (scripted)."))
    sell = next((r for r in reversed(ranked) if r["grade"] in ("C", "D") or r["breaker_fired"]), None)
    buy = next((b for b in f["bench"] if b["still_valid"]), None)
    return PortfolioDecision(
        ranking=views, swap_sell=sell["ticker"] if sell and buy else "",
        swap_buy=buy["ticker"] if sell and buy else "",
        swap_reason=f"Replace a grade {sell['grade']} holding with a committee buy." if sell and buy else "",
        review_next=[v.ticker for v in views if v.verdict != "keep"][:2],
        memo="Scripted: holdings ranked by the grade on file. No research; a real meeting weighs the whole book.")


# ------------------------------------------------------------------ the run

class OverBudget(RuntimeError):
    pass


class PortfolioMeeting:
    def __init__(self, store: Store, steward: Steward, m: Mandate, pf: Portfolio, backend: agents.Backend):
        self.store, self.steward, self.m, self.pf, self.backend = store, steward, m, pf, backend
        self.cost = 0.0

    def _ask(self, mid: int, role: agents.Role, prompt: str, schema, scripted, use_ai: bool = True):
        if self.backend.name == "scripted" or not use_ai:
            return agents.Result(output=scripted, model="scripted" if self.backend.name == "scripted" else "code")
        res = self.backend.run(role, prompt, schema, {"task": "portfolio"})
        self.cost += res.cost_usd
        self.store.set_cost(mid, self.cost)
        if self.cost > self.m.limits.meeting_budget_usd:
            raise OverBudget(f"the meeting has cost ${self.cost:.2f}, over the "
                             f"${self.m.limits.meeting_budget_usd:.2f} budget")
        return res

    def _record(self, mid: int, phase: str, role: str, title: str, res, body: str) -> dict:
        out = agents.schema_json(res.output)
        self.store.add_step(mid, phase, role, title, body,
                            {"output": out, "sources": [s.model_dump() for s in res.sources],
                             "model": res.model, "cost_usd": round(res.cost_usd, 4)})
        return out

    def run(self, meeting_id: Optional[int] = None) -> int:
        mid = meeting_id or self.store.start_meeting(TICKER, "portfolio", self.backend.name)
        try:
            self._run(mid)
        except Exception as e:                       # recorded, never fatal
            self.store.add_step(mid, "rules", "code", "Meeting stopped", f"{type(e).__name__}: {e}")
            self.store.finish_meeting(mid, "stopped" if isinstance(e, OverBudget) else "failed",
                                      cost_usd=self.cost, error=f"{type(e).__name__}: {e}")
        return mid

    def _run(self, mid: int) -> None:
        f = facts(self.store, self.steward, self.m, self.pf)
        text = facts_md(f, self.m)
        self.store.add_step(mid, "facts", "code", "The whole book", text, f)
        task = "This is the monthly portfolio meeting: the whole book, not one stock."

        # The book's risk and its weakest holdings are arithmetic on the facts: code.
        r = self._ask(mid, ROLES["risk"], "", BookMemo, _scripted_book(f), use_ai=False)
        book = self._record(mid, "committee", "risk", "The whole book", r, r.output.memo)

        red = replace(ROLES["redteam"], web=False)
        r = self._ask(mid, red, "", Challenge, _scripted_challenge(f), use_ai=False)
        challenge = self._record(mid, "committee", "redteam", "The weakest holdings", r, r.output.memo)

        r = self._ask(mid, ROLES["cio"],
                      f"{text}\n\n## Risk officer\n{json.dumps(book, indent=1)}\n\n## Red team\n"
                      f"{json.dumps(challenge, indent=1)}\n\n{task} Rank every single stock held keep, review "
                      "or replace. Propose at most one swap, only within the swap rules, and pick up to three "
                      "holdings for the committee to review next.", PortfolioDecision, _scripted_decision(f),
                      use_ai=swap_possible(f) and budget.allows(self.store, self.m, self.backend))
        d = self._record(mid, "committee", "cio", "Decision", r, r.output.memo)

        outcome = self.rules(mid, f, d)
        self.store.finish_meeting(mid, "done", cost_usd=self.cost,
                                  decision={"decision": "swap" if outcome.get("proposal") else "keep",
                                            "action_label": outcome["summary"], "grade": ""},
                                  outcome=outcome)

    def rules(self, mid: int, f: dict, d: dict) -> dict:
        held = {r["ticker"]: r for r in f["stocks"]}
        queue = []
        for t in (d.get("review_next") or [])[:3]:
            if t in held:
                queue.append({"ticker": t, "why": "Asked for by the portfolio meeting", "meeting_id": mid})
        for v in d.get("ranking") or []:
            if v["verdict"] in ("review", "replace") and v["ticker"] in held \
                    and v["ticker"] not in {q["ticker"] for q in queue}:
                queue.append({"ticker": v["ticker"], "why": f"Portfolio meeting: {v['verdict']}", "meeting_id": mid})

        checks: list[risk.Check] = []
        sell, buy = (d.get("swap_sell") or "").strip(), (d.get("swap_buy") or "").strip()
        pid = None
        if sell or buy:
            s_row = held.get(sell)
            checks.append(risk.Check("Sold name is held", bool(s_row), f"{sell or '-'} is "
                                     + ("held" if s_row else "not a held single stock"), value=sell or "-"))
            marked = bool(s_row) and (s_row["grade"] in ("C", "D") or bool(s_row["breaker_fired"]))
            checks.append(risk.Check("Sold name was marked down", marked,
                                     "Graded C or D at its latest review, or a thesis breaker fired"
                                     + ("" if marked else ": not yet. It goes on the review list first."),
                                     value=f"grade {(s_row or {}).get('grade') or '-'}"))
            if s_row and not marked:
                queue = [{"ticker": sell, "why": "The portfolio meeting would replace it", "meeting_id": mid}] \
                    + [q for q in queue if q["ticker"] != sell]
            b_row = next((b for b in f["bench"] if b["ticker"] == buy), None)
            checks.append(risk.Check("Bought name is on the bench", bool(b_row),
                                     "A committee verdict to buy in the last 90 days" if b_row
                                     else f"{buy or '-'} has no committee verdict to buy", value=buy or "-"))
            if b_row:
                checks.append(risk.Check("Its trigger still holds", b_row["still_valid"],
                                         "Checked at today's price", value="yes" if b_row["still_valid"] else "no"))
            sizing = None
            if all(c.ok for c in checks):
                sizing = size_swap(self.pf, self.m, sell, buy, b_row["conviction"] or "starter")
                checks += sizing.checks
            if all(c.ok for c in checks if c.blocking) and sizing and sizing.allowed:
                pid = propose_swap(self.store, self.m, mid, sell, buy, b_row, sizing, d.get("swap_reason", ""))
                if self.m.autopilot:
                    approvals.autopilot(self.store, self.pf, self.m, pid)
        else:
            checks.append(risk.Check("No swap", True, "The CIO proposed no swap", value="keep"))

        queue = queue[:MAX_QUEUE]                 # the committee's review slots are few
        if queue:
            existing = {q["ticker"] for q in self.store.get_meta("review_queue", [])}
            self.store.set_meta("review_queue", self.store.get_meta("review_queue", [])
                                + [q for q in queue if q["ticker"] not in existing])
        p = self.store.proposal(pid) if pid else None
        summary = (p["summary"] if p else
                   ("Swap blocked by the rules" if (sell or buy) else "Keep the book as it is"))
        self.store.add_step(mid, "rules", "code", "Rules (code)", "",
                            {"checks": [asdict(c) for c in checks], "proposal": pid,
                             "review_queue": queue, "summary": summary})
        if p and not self.m.autopilot:
            self.store.add_step(mid, "approval", "code", "Waiting for your OK", p["summary"],
                                {"proposal": pid, "expires_at": p["expires_at"]})
        return {"allowed": bool(pid), "proposal": pid, "summary": summary,
                "review_queue": [q["ticker"] for q in queue],
                "blocked_by": [c.detail for c in checks if c.blocking and not c.ok]}


# ----------------------------------------------------------------- swaps

def after_sale(book: risk.Book, sell: str, fee: float) -> risk.Book:
    """The book as it would be once `sell` is sold in full."""
    proceeds = book.holdings.get(sell, 0.0) - fee
    return risk.Book(cash=book.cash + proceeds,
                     holdings={t: v for t, v in book.holdings.items() if t != sell},
                     pending={t: v for t, v in book.pending.items() if t != sell},
                     peak_value=book.peak_value)


def size_swap(pf: Portfolio, m: Mandate, sell: str, buy: str, conviction: str) -> risk.Sizing:
    return risk.size_buy(buy, conviction if conviction in ("starter", "core") else "starter",
                         after_sale(pf.book(), sell, m.fee_per_order_eur), m)


def propose_swap(store: Store, m: Mandate, mid: int, sell: str, buy: str, b_row: dict,
                 s: risk.Sizing, reason: str) -> int:
    summary = (f"Swap: sell all of {m.name_of(sell)}, buy {m.name_of(buy)} for €{s.amount_eur:,.0f} "
               f"in {m.limits.tranches} tranches (€{s.tranche_eur:,.0f} now)")
    return store.add_proposal(
        meeting_id=mid, ticker=buy, action="swap", conviction=b_row.get("conviction") or "starter",
        amount_eur=s.amount_eur, summary=summary,
        data={"sell": sell, "buy": buy, "buy_meeting": b_row["meeting_id"], "reason": reason,
              "grade": b_row.get("grade"), "action_label": "Swap",
              "sizing": {"amount_eur": s.amount_eur, "tranche_eur": s.tranche_eur,
                         "target_weight": s.target_weight, "funding": s.funding,
                         "checks": [asdict(c) for c in s.checks]}},
        expires_at=approvals._expiry(m))


def swap_possible(f: dict) -> bool:
    """True when a swap is possible: a valid bench name and a marked-down holding.
    Only then is the AI CIO needed; otherwise code ranks the book."""
    return (any(b["still_valid"] for b in f.get("bench") or [])
            and any(r["grade"] in ("C", "D") or r["breaker_fired"] for r in f.get("stocks") or []))


def due(store: Store) -> bool:
    return store.meetings_this_month("portfolio") == 0


def pop_review_queue(store: Store, ticker: str) -> None:
    store.set_meta("review_queue", [q for q in store.get_meta("review_queue", []) if q["ticker"] != ticker])
