"""Proposals: the council suggests, you decide, then code trades.

A verdict the risk engine allows becomes a proposal, not an order. It waits
for you, and expires after `proposal_days` because prices move. When you
approve, the risk engine runs AGAIN at the current price and book: an order
that passed a week ago but breaks a limit today is refused, not placed.

Approving a buy approves its whole plan: the first tranche goes now and the
other two are bought on their dates by the daily review. Rejecting a
proposal, or a later sale of the stock, cancels any tranches still planned.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import risk
from .broker import BrokerError
from .data import DataError
from .mandate import Mandate
from .portfolio import Portfolio
from .store import Store, now


class ApprovalError(RuntimeError):
    pass


def _expiry(m: Mandate) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=m.limits.proposal_days)
            ).isoformat(timespec="seconds")


def summary_for(action: str, name: str, s: risk.Sizing, m: Mandate) -> str:
    if action in ("buy", "add"):
        return (f"Buy {name}: €{s.amount_eur:,.0f} in {m.limits.tranches} tranches "
                f"(€{s.tranche_eur:,.0f} now), ending at {s.target_weight * 100:.1f}% of the portfolio")
    if action == "trim":
        return f"Trim {name}: sell half, about €{s.amount_eur:,.0f}"
    if action == "sell":
        return f"Sell all of {name}: about €{s.amount_eur:,.0f}"
    return f"{action} {name}"


def propose(store: Store, m: Mandate, *, meeting_id: Optional[int], ticker: str,
            decision: dict, sizing: risk.Sizing) -> int:
    action = decision["decision"]
    return store.add_proposal(
        meeting_id=meeting_id, ticker=ticker, action=action,
        conviction=decision.get("conviction", "none"), amount_eur=sizing.amount_eur,
        summary=summary_for(action, m.name_of(ticker), sizing, m),
        data={"sizing": {"amount_eur": sizing.amount_eur, "tranche_eur": sizing.tranche_eur,
                         "target_weight": sizing.target_weight, "funding": sizing.funding,
                         "checks": [asdict(c) for c in sizing.checks]},
              "grade": decision.get("grade"), "action_label": decision.get("action_label"),
              "breaker": decision.get("breaker_triggered", ""), "sell_if": decision.get("sell_if")},
        expires_at=_expiry(m))


def propose_deadline(store: Store, m: Mandate, spare_eur: float) -> int:
    return store.add_proposal(
        meeting_id=None, ticker="INDEX", action="deadline", conviction="none",
        amount_eur=spare_eur,
        summary=(f"Watch-list deadline ({m.limits.watchlist_deadline}): move €{spare_eur:,.0f} "
                 f"of unused reserve into the index ETFs"),
        data={}, expires_at=_expiry(m))


def expire(store: Store) -> int:
    n = 0
    t = now()
    for p in store.proposals("pending"):
        if p["expires_at"] < t and store.decide_proposal(p["id"], "expired"):
            n += 1
    return n


def reject(store: Store, pid: int, why: str = "") -> dict:
    p = store.proposal(pid)
    if not p or not store.decide_proposal(pid, "rejected", {"why": why}):
        raise ApprovalError("This proposal is no longer waiting for an answer.")
    if p["meeting_id"]:
        store.add_step(p["meeting_id"], "approval", "you", "You said no",
                       why or "Proposal rejected. Nothing was traded.")
    return {"status": "rejected"}


def approve(store: Store, pf: Portfolio, m: Mandate, pid: int) -> dict:
    """Re-check every rule now, then trade. Raises ApprovalError if it cannot."""
    expire(store)
    p = store.proposal(pid)
    if not p or p["status"] != "pending":
        raise ApprovalError("This proposal is no longer waiting for an answer "
                            f"({p['status'] if p else 'not found'}).")
    book = pf.book()
    trades: list[dict] = []
    try:
        if p["action"] in ("buy", "add"):
            s = risk.size_buy(p["ticker"], p["conviction"], book, m)
            if not s.allowed:
                why = "; ".join(c.detail for c in s.blocked_by)
                _fail(store, p, f"The rules no longer allow it: {why}")
            index_need = sum(eur for src, eur in s.funding if src.startswith("index"))
            if index_need > 0:
                trades += _sell_index(pf, m, index_need, p["meeting_id"])
            trades += pf.plan(p["ticker"], s.amount_eur, p["meeting_id"],
                              note=p["data"].get("action_label") or "")
        elif p["action"] in ("trim", "sell"):
            s = risk.size_sell(p["ticker"], p["action"], p["data"].get("breaker", ""), book, m)
            if not s.allowed:
                _fail(store, p, "; ".join(c.detail for c in s.blocked_by))
            store.cancel_tranches(p["ticker"])
            t = pf.sell(p["ticker"], None if p["action"] == "sell" else s.amount_eur,
                        p["meeting_id"], note=f"breaker: {p['data'].get('breaker')}")
            trades += [t] if t else []
        elif p["action"] == "deadline":
            trades += _buy_index(pf, m, p["amount_eur"])
            store.set_meta("deadline_done", now()[:10])
        else:
            _fail(store, p, f"Unknown action {p['action']}")
    except (BrokerError, DataError) as e:
        _fail(store, p, f"The order could not be placed: {e}", trades)

    planned = [x for x in store.tranches("planned")
               if p["meeting_id"] and x["meeting_id"] == p["meeting_id"]]
    result = {"trades": trades, "planned": planned}
    store.decide_proposal(pid, "approved", result)
    if p["meeting_id"]:
        body = "\n".join(f"- {t['side'].upper()} {t['qty']:.4f} {t['ticker']} at {t['price']:,.2f} "
                         f"{t['currency']} = €{t['amount_eur']:,.2f} (fee €{t['fee_eur']:.2f}, "
                         f"{t.get('broker', 'paper')})" for t in trades)
        body += "".join(f"\n- Planned: {x['note']} on {x['due']}, €{x['amount_eur']:,.2f}" for x in planned)
        store.add_step(p["meeting_id"], "approval", "you", "You approved", body,
                       {"proposal": pid, "trades": trades, "planned": planned})
    return {"status": "approved", "trades": trades, "planned": planned}


def _fail(store: Store, p: dict, why: str, trades: Optional[list] = None):
    store.decide_proposal(p["id"], "failed", {"why": why, "trades": trades or []})
    if p["meeting_id"]:
        store.add_step(p["meeting_id"], "approval", "you", "Approved, but not placed", why)
    raise ApprovalError(why)


def _index(m: Mandate) -> list:
    return [i for i in m.instruments if i.bucket == "index"]


def _sell_index(pf: Portfolio, m: Mandate, amount: float, mid) -> list[dict]:
    held = {i.ticker: v for i in _index(m) if (v := pf.book().holdings.get(i.ticker, 0.0)) > 0}
    total = sum(held.values()) or 1.0
    out = []
    for t, v in held.items():
        tr = pf.sell(t, amount * v / total, mid, note="funding a new position")
        if tr:
            out.append(tr)
    return out


def _buy_index(pf: Portfolio, m: Mandate, amount: float) -> list[dict]:
    idx = _index(m)
    total_w = sum(i.weights.get(m.setting) for i in idx) or 1.0
    out = []
    for i in idx:
        t = pf.buy(i.ticker, amount * i.weights.get(m.setting) / total_w,
                   note="watch-list deadline: reserve moved to the index")
        if t:
            out.append(t)
    return out
