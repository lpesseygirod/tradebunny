"""One council meeting about one stock, from the gate to the trade.

    gate        code: is it in the universe, is the monthly meeting cap free?
    facts       code: the fact sheet, the only numbers anyone may cite
    blind       macro, thesis, valuation and the red team's pre-mortem,
                written in parallel, none seeing the others
    cross       the red team reads the thesis and ranks three objections;
                the thesis analyst gets one rebuttal
    committee   the risk officer's exposure memo, then the CIO's verdict
    risk        code: the risk engine sizes the verdict or blocks it
    approval    a proposal that waits for YOU; nothing trades until you say yes

Every step is written to the store the moment it exists, so the interface
shows the debate as it happens and keeps it afterwards exactly as it was.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Optional

from . import agents, approvals, learning, risk
from .agents import ROLES, Backend, RoleRefused
from .data import DataError, Steward
from .mandate import Mandate
from .portfolio import Portfolio
from .store import Store

log = logging.getLogger(__name__)


class OverBudget(RuntimeError):
    pass


def _md_list(items) -> str:
    return "\n".join(f"- {x}" for x in items) if items else "- none"


class Meeting:
    def __init__(self, store: Store, steward: Steward, mandate: Mandate,
                 portfolio: Portfolio, backend: Backend):
        self.store, self.steward, self.m = store, steward, mandate
        self.pf, self.backend = portfolio, backend
        self.cost = 0.0

    # ------------------------------------------------------------ context

    def prior_view(self, ticker: str) -> dict:
        i = self.m.instrument(ticker)
        if i:
            return {"grade": i.grade, "thesis": i.thesis, "trigger": i.trigger,
                    "entry_price_eur": i.entry_price_eur, "breakers": list(i.breakers),
                    "trigger_pe": None, "status": "in the model portfolio"}
        w = self.m.watch(ticker)
        if w:
            return {"grade": w.grade, "thesis": w.thesis, "trigger": w.event,
                    "entry_price_eur": w.trigger_price_eur, "breakers": [],
                    "trigger_pe": w.trigger_pe, "status": "on the watch list"}
        return {}

    def fact_sheet(self, ticker: str) -> dict:
        quotes = self.steward.quotes(self.pf.tickers_needed())
        q = quotes[ticker]
        if self.steward.mode == "live" and not q.is_fresh():
            raise DataError(f"{ticker}'s price is from {q.as_of}: too old to decide on")
        book = self.pf.book(quotes)
        qd = asdict(q)
        qd["from_high"] = q.from_high
        held = book.holdings.get(ticker, 0.0)
        top = sorted(((t, v / book.total) for t, v in book.holdings.items()), key=lambda x: -x[1])
        return {
            "ticker": ticker, "name": self.m.name_of(ticker),
            "quote": qd, "data_mode": self.steward.mode,
            "held_eur": held, "weight": book.weight(ticker),
            "target": self.m.target(ticker),
            "ai": self.m.is_ai(ticker), "taiwan": self.m.is_taiwan(ticker),
            "portfolio_eur": book.total, "cash_eur": book.cash,
            "ai_weight": risk.ai_weight(book, self.m),
            "stock_count": risk.stock_count(book, self.m),
            "holdings": [{"ticker": t, "name": self.m.name_of(t), "weight": w} for t, w in top],
            "prior": self.prior_view(ticker),
            "setting": self.m.setting,
        }

    def facts_md(self, f: dict) -> str:
        q = f["quote"]
        pe = f"{q['forward_pe']:.1f}" if q.get("forward_pe") else "not available"
        tpe = f"{q['trailing_pe']:.1f}" if q.get("trailing_pe") else "not available"
        fh = f"{q['from_high'] * 100:.0f}% below its 52-week high" if q.get("from_high") is not None else ""
        lim = self.m.limits
        lines = [
            f"# Fact sheet: {f['name']} ({f['ticker']})",
            f"Source: {q['source']}, fetched {q['as_of']}. These are the only numbers you may cite as facts.",
            "",
            f"- Price: {q['price']:,.2f} {q['currency']} ({q['price_eur']:,.2f} EUR)",
            f"- Forward P/E: {pe}; trailing P/E: {tpe}",
            f"- 52-week range: {q.get('low_52w') or '?'} to {q.get('high_52w') or '?'} {q['currency']}. {fh}",
            f"- Held now: {f['held_eur']:,.0f} EUR, {f['weight'] * 100:.1f}% of the portfolio "
            f"(model target {f['target'] * 100:.1f}%)",
            f"- AI-sensitive: {'yes' if f['ai'] else 'no'}; Taiwan: {'yes' if f['taiwan'] else 'no'}",
            "",
            "## The portfolio",
            f"Paper portfolio {f['portfolio_eur']:,.0f} EUR, {f['setting']} setting, "
            f"{f['stock_count']} of {lim.max_single_stocks} single stocks, AI-sensitive stocks "
            f"{f['ai_weight'] * 100:.1f}% (cap {lim.ai_cap.get(self.m.setting) * 100:.0f}%).",
            "Largest holdings: " + ", ".join(f"{h['name']} {h['weight'] * 100:.1f}%" for h in f["holdings"][:8]),
            "",
            "## The committee's last view (October 2026)",
        ]
        p = f["prior"]
        if p:
            lines += [f"- Status: {p['status']}; grade {p.get('grade') or 'none'}",
                      f"- Thesis: {p.get('thesis') or '-'}",
                      f"- Trigger: {p.get('trigger') or '-'}",
                      f"- Thesis breakers: {'; '.join(p.get('breakers') or []) or '-'}"]
        else:
            lines.append("- None on file.")
        past = learning.history_text(self.store, self.m, f["ticker"])
        if past:
            lines += ["", "## The council's past calls on this name, and how they turned out", past]
        lessons = learning.lessons_text(self.store)
        if lessons:
            lines += ["", "## Lessons the principal approved from past post-mortems (apply them)", lessons]
        lines += ["", "## Hard limits (enforced by code, not by you)",
                  f"Positions {lim.min_position * 100:.0f}-{lim.max_position * 100:.0f}%, Taiwan max "
                  f"{lim.taiwan_cap * 100:.0f}%, at most {lim.max_single_stocks} single stocks, "
                  f"limit orders in {lim.tranches} tranches, 5-year horizon, euro investor in Spain."]
        return "\n".join(lines)

    # ------------------------------------------------------------- running

    def _ask(self, mid: int, task: str, role_key: str, prompt: str, schema, ctx: dict):
        role = ROLES[role_key]
        res = self.backend.run(role, prompt, schema, {**ctx, "task": task})
        self.cost += res.cost_usd
        self.store.set_cost(mid, self.cost)
        if self.cost > self.m.limits.meeting_budget_usd:
            raise OverBudget(f"the meeting has cost ${self.cost:.2f}, over the "
                             f"${self.m.limits.meeting_budget_usd:.2f} budget")
        return res

    def _record(self, mid: int, phase: str, role_key: str, title: str, res, body: str) -> dict:
        data = {"output": agents.schema_json(res.output),
                "sources": [s.model_dump() for s in res.sources],
                "research": res.research,
                "model": res.model, "cost_usd": round(res.cost_usd, 4)}
        self.store.add_step(mid, phase, role_key, title, body, data)
        return data["output"]

    def run(self, ticker: str, meeting_id: Optional[int] = None) -> int:
        held = self.pf.quantities().get(ticker, 0.0) > 0
        kind = "holding" if held else "idea"
        mid = meeting_id or self.store.start_meeting(ticker, kind, self.backend.name)
        try:
            return self._run(mid, ticker, kind)
        except OverBudget as e:
            self.store.add_step(mid, "risk", "code", "Meeting stopped: over budget", str(e))
            self.store.finish_meeting(mid, "stopped", cost_usd=self.cost, error=str(e))
        except (RoleRefused, DataError) as e:
            self.store.add_step(mid, "risk", "code", "Meeting stopped", str(e))
            self.store.finish_meeting(mid, "stopped", cost_usd=self.cost, error=str(e))
        except Exception as e:
            log.exception("meeting %s failed", mid)
            self.store.add_step(mid, "risk", "code", "Meeting failed",
                                f"{type(e).__name__}: {e}")
            self.store.finish_meeting(mid, "failed", cost_usd=self.cost,
                                      error=f"{type(e).__name__}: {e}")
        return mid

    def _run(self, mid: int, ticker: str, kind: str) -> int:
        # --- gate --------------------------------------------------------
        used = self.store.meetings_this_month("idea") - (1 if kind == "idea" else 0)
        checks = risk.gate(ticker, self.m, used if kind == "idea" else 0)
        self.store.add_step(mid, "gate", "code", "Mandate gate", "",
                            {"checks": [asdict(c) for c in checks]})
        if any(not c.ok for c in checks):
            reason = next(c.detail for c in checks if not c.ok)
            self.store.finish_meeting(mid, "stopped", error=reason)
            return mid

        # --- facts -------------------------------------------------------
        f = self.fact_sheet(ticker)
        facts = self.facts_md(f)
        self.store.add_step(mid, "facts", "code", "Fact sheet", facts, f)
        ctx = {"name": f["name"], "ticker": ticker, "quote": f["quote"], "ai": f["ai"],
               "taiwan": f["taiwan"],
               "prior": f["prior"], "held_eur": f["held_eur"]}
        task = ("Should the council add to this holding, hold it, trim it or sell it?"
                if kind == "holding" else "Should the council buy this stock?")

        # --- blind round -------------------------------------------------
        blind = {
            "macro": ("macro", agents.MacroMemo,
                      f"{facts}\n\n{task} Write your regime memo and what it means for this stock."),
            "thesis": ("thesis", agents.ThesisMemo,
                       f"{facts}\n\n{task} Write the thesis memo with the five-point test."),
            "valuation": ("valuation", agents.ValuationMemo,
                          f"{facts}\n\n{task} Is it already priced in?"),
            "premortem": ("redteam", agents.Premortem,
                          f"{facts}\n\n{task} You have not seen anyone's case for it. Write a "
                          "pre-mortem: two years from now this position lost 40%. Why?"),
        }
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {k: ex.submit(self._ask, mid, k, role, prompt, schema, ctx)
                       for k, (role, schema, prompt) in blind.items()}
            res = {k: fut.result() for k, fut in futures.items()}

        out = {}
        out["macro"] = self._record(mid, "blind", "macro", "Regime memo", res["macro"],
                                    res["macro"].output.memo)
        t = res["thesis"].output
        out["thesis"] = self._record(mid, "blind", "thesis", "Thesis memo", res["thesis"],
                                     f"**{t.one_line}**\n\n{t.memo}")
        out["valuation"] = self._record(mid, "blind", "valuation", "Is it priced in?",
                                        res["valuation"], res["valuation"].output.memo)
        pm = res["premortem"].output
        out["premortem"] = self._record(
            mid, "blind", "redteam", "Pre-mortem", res["premortem"],
            pm.story + "\n\n**What would prove the bear right**\n" +
            _md_list(pm.what_would_prove_the_bear_right))

        # --- cross-examination -------------------------------------------
        r = self._ask(mid, "objections", "redteam",
                      f"{facts}\n\n## The thesis analyst's memo\n{json.dumps(out['thesis'], indent=1)}"
                      f"\n\n## Your own pre-mortem\n{pm.story}\n\nRank your top three objections "
                      "to this thesis, each with evidence and a test that would prove it right.",
                      agents.Objections, {**ctx, "thesis": out["thesis"]})
        objections = self._record(mid, "cross", "redteam", "Top objections", r,
                                  "\n".join(f"{o.rank}. **{o.claim}** {o.evidence}"
                                            for o in r.output.objections))
        r = self._ask(mid, "rebuttal", "thesis",
                      f"{facts}\n\n## Your thesis\n{json.dumps(out['thesis'], indent=1)}\n\n"
                      f"## The red team's objections\n{json.dumps(objections, indent=1)}\n\n"
                      "You get one rebuttal. Answer each objection; concede where the evidence "
                      "is against you.",
                      agents.Rebuttal, {**ctx, "objections": objections})
        rebuttal = self._record(mid, "cross", "thesis", "Rebuttal", r, r.output.memo)

        # --- committee ---------------------------------------------------
        memos = json.dumps({**out, "objections": objections, "rebuttal": rebuttal}, indent=1)
        notes = "\n\n".join(f"### {ROLES[k_role].title}\n{res[k].research}"
                             for k, k_role in (("macro", "macro"), ("thesis", "thesis"),
                                               ("valuation", "valuation"), ("premortem", "redteam"))
                             if res[k].research)
        r = self._ask(mid, "factcheck", "checker",
                      f"{facts}\n\n## Memos\n{memos}\n\n## The members' research notes\n"
                      f"{notes or '(none: no member researched the web)'}\n\nCheck the claims "
                      "the decision will rest on.", agents.FactCheck,
                      {**ctx, "valuation": out["valuation"]})
        check = self._record(mid, "committee", "checker", "Fact check", r, r.output.summary)
        record = f"{facts}\n\n## Memos\n{memos}\n\n## Fact check\n{json.dumps(check, indent=1)}"
        r = self._ask(mid, "risk", "risk", record + "\n\nWrite the portfolio-impact memo.",
                      agents.RiskMemo, ctx)
        risk_memo = self._record(mid, "committee", "risk", "Portfolio impact", r, r.output.memo)
        r = self._ask(mid, "cio", "cio",
                      record + f"\n\n## Risk officer\n{json.dumps(risk_memo, indent=1)}\n\n"
                      f"{task} Decide, grade it and answer every ranked objection.",
                      agents.CIODecision,
                      {**ctx, "valuation": out["valuation"], "objections": objections})
        decision = self._record(mid, "committee", "cio", "Decision", r, r.output.rationale)

        # --- the risk engine -------------------------------------------------
        outcome = self.execute(mid, ticker, decision)
        self.store.finish_meeting(mid, "done", cost_usd=self.cost,
                                  decision=decision, outcome=outcome)
        # Day zero of the scorekeeper: the stock and the index, today.
        learning.record_start(self.store, self.steward, self.m, mid, ticker)
        return mid

    def execute(self, mid: int, ticker: str, d: dict) -> dict:
        book = self.pf.book()
        action = d["decision"]
        if action in ("buy", "add"):
            # Code checks the CIO kept the committee's own rules, then sizes.
            rules, conviction = risk.policy(ticker, d, self.m, self.steward.quote(ticker))
            s = risk.size_buy(ticker, conviction, book, self.m)
            s.checks = rules + s.checks
            s.allowed = not s.blocked_by
        elif action in ("trim", "sell"):
            s = risk.size_sell(ticker, action, d.get("breaker_triggered", ""), book, self.m)
        else:
            s = risk.Sizing(ticker=ticker, action="none", allowed=True,
                            checks=[risk.Check("no order", True,
                                               f"The CIO decided to {action}: nothing to trade")])
        self.store.add_step(mid, "risk", "code", "Risk engine", "",
                            {"allowed": s.allowed, "action": s.action,
                             "amount_eur": s.amount_eur, "tranche_eur": s.tranche_eur,
                             "target_weight": s.target_weight, "funding": s.funding,
                             "checks": [asdict(c) for c in s.checks]})
        pid = None
        if s.allowed and s.action in ("buy", "sell") and s.amount_eur > 0:
            pid = approvals.propose(self.store, self.m, meeting_id=mid, ticker=ticker,
                                    decision=d, sizing=s)
            p = self.store.proposal(pid)
            self.store.add_step(mid, "approval", "code", "Waiting for your OK", p["summary"],
                                {"proposal": pid, "expires_at": p["expires_at"]})
        return {"allowed": s.allowed, "action": s.action, "amount_eur": s.amount_eur,
                "blocked_by": [c.detail for c in s.blocked_by], "proposal": pid}
