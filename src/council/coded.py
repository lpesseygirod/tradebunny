"""The parts of a meeting that code does better than an AI, or as well.

The AI is used only where reading and judgement are needed: research, the
thesis, the debate, the fact check and the verdict. These two steps are
plain arithmetic on numbers the code already has, so they cost nothing and
give the same answer every time:

  risk officer   which holdings are the same bet (correlation of weekly
                 returns, the AI and Taiwan flags) and what each world does
                 to the stock, scaled by its beta, then to the portfolio at
                 the size the rules allow
  the vote       each member's position after the debate, read from what
                 they wrote: their stance, what the thesis analyst conceded,
                 what the valuation analyst said about the price
"""

from __future__ import annotations

from typing import Optional

from . import quant
from .agents import RiskMemo, ScenarioMove

STANCE_VOTE = {"supportive": "buy", "neutral": "wait", "cautious": "wait", "against": "no"}
SAME_BET = 0.6                    # weekly-return correlation above this: really one bet


def _clamp(x: float, lo: int = -100, hi: int = 200) -> int:
    return int(max(lo, min(hi, round(x))))


def same_bets(desk: quant.Desk, ticker: str, holdings: list[dict], m) -> list[str]:
    """Held stocks that move with this one (correlation), or share its AI or Taiwan risk."""
    me = desk.raw(ticker) or {}
    mine = quant.returns([c for _, c in me.get("closes") or []])
    out = []
    for h in holdings:
        t = h["ticker"]
        if t == ticker or not m.is_stock(t):
            continue
        why = []
        other = desk.raw(t) or {}
        theirs = quant.returns([c for _, c in other.get("closes") or []])
        n = min(len(mine), len(theirs), 104)
        c = quant.corr(mine[-n:], theirs[-n:]) if n > 30 else None
        if c is not None and c >= SAME_BET:
            why.append(f"correlation {c:.2f}")
        if m.is_ai(ticker) and m.is_ai(t):
            why.append("both AI-linked")
        if m.is_taiwan(ticker) and m.is_taiwan(t):
            why.append("both Taiwan")
        if why:
            out.append(f"{h['name']} ({', '.join(why)})")
    return out


def risk_memo(f: dict, desk: quant.Desk, m, size: float) -> RiskMemo:
    """The risk officer's memo, computed. `size` is the portfolio weight it
    would have (what is held or planned, or the minimum position)."""
    q = f.get("quant") or {}
    b = q.get("beta") if q.get("beta") is not None else 1.0
    b = max(0.3, min(b, 2.5))
    rich = (q.get("pe_vs_avg") or 0) > 0.3 or (q.get("pe_now") or 0) > 35
    sc = [ScenarioMove(scenario="Base case", stock_move_pct=6),
          ScenarioMove(scenario="Markets fall 20%", stock_move_pct=_clamp(-20 * b)),
          ScenarioMove(scenario="AI spending cut", stock_move_pct=_clamp(-30 if f["ai"] else -6 * b)),
          ScenarioMove(scenario="Rates up", stock_move_pct=_clamp(-18 if rich else -8)),
          ScenarioMove(scenario="Recession", stock_move_pct=_clamp(-25 * b))]
    if f.get("taiwan"):
        sc.insert(3, ScenarioMove(scenario="Taiwan crisis", stock_move_pct=-45))
    bets = same_bets(desk, f["ticker"], f.get("holdings") or [], m)
    cap = m.limits.ai_cap.get(m.setting)
    ai_after = f["ai_weight"] + max(0.0, size - f.get("weight", 0.0)) if f["ai"] else f["ai_weight"]
    corr_book = f.get("quant_corr_book")
    worst = min(s.stock_move_pct for s in sc) * size
    stricter = (f["ai"] and ai_after > cap * 0.9) or (corr_book is not None and corr_book > 0.75)
    overlap = ("Same bet as " + "; ".join(bets) + "." if bets else
               "No holding moves closely with it: a new source of return.")
    memo = (f"Computed by code, not an AI. Beta {b:.2f}{' (capped)' if q.get('beta') and b != q['beta'] else ''}"
            f" scales the market scenarios; AI and Taiwan risks use fixed falls of 30% and 45%; a richly "
            f"priced stock falls more if rates rise. At {size * 100:.1f}% of the portfolio, the worst case "
            f"costs {abs(worst):.1f} points."
            + (f" Correlation with the whole portfolio: {corr_book:.2f}." if corr_book is not None else "")
            + (f" AI-linked stocks would reach {ai_after * 100:.1f}% of a {cap * 100:.0f}% cap." if f["ai"] else "")
            + (" Recommendation: stricter, " + ("near the AI cap." if f["ai"] and ai_after > cap * 0.9
                                                 else "it moves with what we own.") if stricter else ""))
    return RiskMemo(scenarios=sc, overlap=overlap, recommendation="stricter" if stricter else "proceed", memo=memo)


def votes(out: dict, rebuttal: dict, risk: dict) -> list[dict]:
    """Each member's position after the debate, read from what they wrote."""
    conceded = [r for r in (rebuttal.get("responses") or []) if r.get("concedes")]
    first_conceded = any(r.get("rank") == 1 for r in conceded)
    v = []

    def add(role: str, vote: str, reason: str):
        v.append({"role": role, "vote": vote, "reason": reason})

    s = (out.get("macro") or {}).get("stance", "neutral")
    add("macro", STANCE_VOTE.get(s, "wait"), f"The backdrop is {s} for this stock.")
    s = (out.get("thesis") or {}).get("stance", "neutral")
    t_vote = STANCE_VOTE.get(s, "wait")
    if first_conceded and t_vote == "buy":
        add("thesis", "wait", "Supportive, but conceded the red team's strongest objection.")
    else:
        add("thesis", t_vote, f"{s.capitalize()} after the rebuttal"
            + (f", with {len(conceded)} objection{'s' if len(conceded) != 1 else ''} conceded." if conceded else "."))
    p = (out.get("valuation") or {}).get("priced_in", "partly")
    add("valuation", {"no": "buy", "partly": "wait", "yes": "no"}[p], f"Priced in: {p}.")
    s = (out.get("earnings") or {}).get("stance", "neutral")
    add("earnings", STANCE_VOTE.get(s, "wait"), f"The latest results are {s} for the thesis.")
    if conceded:
        add("redteam", "no", f"The thesis analyst conceded {len(conceded)} of the objections.")
    else:
        add("redteam", "wait", "Every objection was answered, none conceded; the risks stand untested.")
    rec = risk.get("recommendation", "proceed")
    add("risk", "wait" if rec != "proceed" else "buy",
        "Stricter: " + risk.get("overlap", "") if rec != "proceed" else "Within every limit; " + risk.get("overlap", ""))
    return v


def acts(decision: Optional[str]) -> bool:
    return decision in ("buy", "add", "trim", "sell")
