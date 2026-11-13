"""The risk engine: the council's only veto, in plain code.

Nothing is bought or sold until these functions agree. Each returns named
checks, so the interface can show which rule passed or blocked. No agent can
change a number here: the CIO's conviction only picks "starter" or "core",
and code turns that into an amount.

Rules, from the committee reports:
  - only names in the mandate's universe; rejected names never
  - no position below min_position, none above max_position; Taiwan capped
  - at most max_single_stocks single stocks: an 11th means selling one first
  - AI-sensitive stocks stay under the setting's AI cap
  - new names are paid for from the watch-list reserve, then the index core
  - cash never below min_cash
  - limit orders in tranches; planned tranches count as already committed
  - a sale needs a named thesis breaker: a price drop alone never sells
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from .mandate import Mandate

Conviction = Literal["none", "starter", "core"]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True
    value: str = ""                  # the short number the interface shows


@dataclass
class Book:
    """The portfolio as the risk engine sees it, all in euros."""

    cash: float
    holdings: dict[str, float] = field(default_factory=dict)   # market value
    pending: dict[str, float] = field(default_factory=dict)    # planned tranches
    peak_value: float = 0.0

    @property
    def total(self) -> float:
        return self.cash + sum(self.holdings.values())

    def committed(self, ticker: str) -> float:
        return self.holdings.get(ticker, 0.0) + self.pending.get(ticker, 0.0)

    def weight(self, ticker: str) -> float:
        return self.committed(ticker) / self.total if self.total else 0.0

    def free_cash(self) -> float:
        """Cash not yet promised to a scheduled tranche."""
        return self.cash - sum(self.pending.values())


@dataclass
class Sizing:
    ticker: str
    action: str                      # buy | sell | none
    allowed: bool
    amount_eur: float = 0.0          # total change, all tranches
    tranche_eur: float = 0.0         # first order
    tranches: int = 1
    target_weight: float = 0.0
    funding: list[tuple[str, float]] = field(default_factory=list)  # (source, eur)
    checks: list[Check] = field(default_factory=list)

    @property
    def blocked_by(self) -> list[Check]:
        return [c for c in self.checks if c.blocking and not c.ok]


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def stock_count(book: Book, m: Mandate) -> int:
    return sum(1 for t in book.holdings.keys() | book.pending.keys()
               if m.is_stock(t) and book.committed(t) > 1.0)


def ai_weight(book: Book, m: Mandate) -> float:
    if not book.total:
        return 0.0
    return sum(book.committed(t) for t in book.holdings.keys() | book.pending.keys()
               if m.is_ai(t)) / book.total


# --------------------------------------------------------------- the gate

def gate(ticker: str, m: Mandate, meetings_this_month: int) -> list[Check]:
    """Runs before any agent spends a token."""
    out = []
    rej = m.rejection(ticker)
    in_universe = ticker in m.universe()
    if rej:
        out.append(Check("universe", False, f"Rejected by the committee: {rej.reason}", value="rejected"))
    else:
        out.append(Check("universe", in_universe,
                         "In the mandate's universe" if in_universe else
                         "Not in the mandate. Add it to the watch list first, at the quarterly review.",
                         value="yes" if in_universe else "no"))
    cap = m.limits.max_meetings_per_month
    out.append(Check("meeting cap", meetings_this_month < cap,
                     f"{meetings_this_month} of {cap} idea meetings used this month",
                     value=f"{meetings_this_month} / {cap}"))
    return out


# ------------------------------------------------- the CIO's own rules

def policy(ticker: str, d: dict, m: Mandate, quote) -> tuple[list[Check], Conviction]:
    """Did the CIO's verdict keep the committee's rules? Checked by code.

      - only grade A, or grade B, may be bought
      - a name with a price or P/E trigger on file is bought only once the
        trigger is reached at today's price
      - 'core' conviction (a full position) needs grade A; B gets 'starter'
    """
    out: list[Check] = []
    grade, conviction = d.get("grade", "not_graded"), d.get("conviction", "none")
    ok = grade in ("A", "B")
    out.append(Check("Grade allows buying", ok, "Only grade A, or B on its trigger, may be bought",
                     value=f"grade {grade if grade != 'not_graded' else 'none'}"))
    w, i = m.watch(ticker), m.instrument(ticker)
    pe_t = w.trigger_pe if w else None
    price_t = (w.trigger_price_eur if w else None) or (i.entry_price_eur if i else None)
    if grade == "B" and (pe_t or price_t):
        hits, shown = [], []
        if pe_t and quote.forward_pe:
            hits.append(quote.forward_pe <= pe_t)
            shown.append(f"fwd P/E {quote.forward_pe:.1f} {'≤' if hits[-1] else '>'} {pe_t:g}")
        if price_t:
            hits.append(quote.price_eur <= price_t)
            shown.append(f"€{quote.price_eur:,.0f} {'≤' if hits[-1] else '>'} €{price_t:,.0f}")
        reached = any(hits)
        out.append(Check("Trigger reached", reached,
                         "A grade-B name is bought only once its trigger is reached"
                         + ("" if reached else ": not yet"), value="; ".join(shown) or "no data"))
    if conviction == "core" and grade != "A":
        conviction = "starter"
        out.append(Check("Conviction", True, "A full ('core') position needs grade A; sized as a starter",
                         blocking=False, value="core → starter"))
    return out, conviction


# ----------------------------------------------------------------- buying

def size_buy(ticker: str, conviction: Conviction, book: Book, m: Mandate,
             model_target: bool = False) -> Sizing:
    """How much the CIO's conviction may buy, or why it may buy nothing.

    `model_target` sizes to the mandate's model weight instead (used to build
    the starting portfolio and for scheduled rebalancing).
    """
    lim = m.limits
    s = Sizing(ticker=ticker, action="buy", allowed=False, tranches=lim.tranches)
    total = book.total
    if total <= 0:
        s.checks.append(Check("portfolio", False, "The portfolio has no value"))
        return s
    if conviction == "none" and not model_target:
        s.action = "none"
        s.checks.append(Check("conviction", False, "The CIO set no conviction"))
        return s

    is_stock = m.is_stock(ticker)
    now_w = book.weight(ticker)
    if model_target:
        target = m.target(ticker)
    else:
        target = lim.min_position if conviction == "starter" else lim.max_position
    if is_stock:
        target = min(target, lim.max_position)
        s.checks.append(Check(f"Position size {_pct(lim.min_position)[:-3]}-{_pct(lim.max_position)[:-3]}%", True,
                              f"Target {_pct(target)}, cap {_pct(lim.max_position)}",
                              value=f"{_pct(target)} ≤ {_pct(lim.max_position)}"))
    if m.is_taiwan(ticker) and target > lim.taiwan_cap:
        target = lim.taiwan_cap
        s.checks.append(Check("Taiwan cap", True, f"Cut to {_pct(lim.taiwan_cap)}",
                              value=f"{_pct(target)} ≤ {_pct(lim.taiwan_cap)}"))

    add = (target - now_w) * total
    if add < 0.002 * total:
        s.action = "none"
        s.checks.append(Check("Room to add", False,
                              f"Already at {_pct(now_w)} of a {_pct(target)} target",
                              blocking=True, value=f"{_pct(now_w)} of {_pct(target)}"))
        return s

    # Single-stock count: a new name at the limit means selling one first.
    if is_stock and book.committed(ticker) <= 1.0:
        n = stock_count(book, m)
        ok = n < lim.max_single_stocks
        s.checks.append(Check(f"Stocks ≤ {lim.max_single_stocks}", ok,
                              f"{n} of {lim.max_single_stocks} single stocks held" +
                              ("" if ok else ". Sell one of them first."),
                              value=f"{n} → {n + 1}"))

    # AI cap, counting planned tranches.
    if m.is_ai(ticker):
        cap = lim.ai_cap.get(m.setting)
        room = (cap - ai_weight(book, m)) * total
        if add > room:
            add = max(0.0, room)
        ok = add > 0.002 * total
        s.checks.append(Check(f"AI-linked total ≤ {_pct(cap)[:-3]}%", ok,
                              f"AI-sensitive stocks at {_pct(ai_weight(book, m))} of a "
                              f"{_pct(cap)} cap" + ("" if ok else ". Trim another AI name first."),
                              value=f"{_pct(ai_weight(book, m) + max(0.0, add) / total)}"))

    # The minimum position, after every cut above.
    final_w = now_w + add / total
    if is_stock:
        ok = final_w + 1e-9 >= lim.min_position
        s.checks.append(Check("Minimum size", ok,
                              f"Would end at {_pct(final_w)}; minimum {_pct(lim.min_position)}",
                              value=f"{_pct(final_w)} ≥ {_pct(lim.min_position)}"))

    # Funding: the reserve first, then the index core.
    reserve_room = book.free_cash() - lim.min_cash * total
    from_reserve = max(0.0, min(add, reserve_room))
    from_index = add - from_reserve
    if from_reserve > 0:
        s.funding.append(("watch-list reserve", from_reserve))
    if from_index > 0.01:
        index = [i.ticker for i in m.instruments if i.bucket == "index"]
        held_index = sum(book.holdings.get(t, 0.0) for t in index)
        ok = held_index >= from_index
        s.funding.append(("index ETFs (sold)", from_index))
        s.checks.append(Check("Funding", ok,
                              f"{from_index:,.0f} EUR comes from the index core" +
                              ("" if ok else ", which is not large enough"),
                              value=f"€{from_index:,.0f} from index"))
    else:
        s.checks.append(Check("Funding", True, "Paid from the watch-list reserve",
                              value=f"€{from_reserve:,.0f} reserve"))
    s.checks.append(Check(f"{lim.tranches} tranches planned", True,
                          f"Limit orders, {lim.tranche_spacing_days} days apart",
                          value=str(lim.tranches)))
    s.checks.append(Check("Cash covers tranche 1", book.free_cash() >= add / lim.tranches,
                          f"Cash stays at or above {_pct(lim.min_cash)}",
                          value=f"€{book.free_cash():,.0f}"))

    s.amount_eur = add
    s.tranche_eur = add / lim.tranches
    s.target_weight = final_w
    s.allowed = not s.blocked_by
    return s


# ---------------------------------------------------------------- selling

def size_sell(ticker: str, kind: Literal["trim", "sell"], breaker: str,
              book: Book, m: Mandate) -> Sizing:
    s = Sizing(ticker=ticker, action="sell", allowed=False, tranches=1)
    held = book.holdings.get(ticker, 0.0)
    s.checks.append(Check("Held", held > 0, f"{held:,.0f} EUR held" if held else "Not held",
                          value=f"€{held:,.0f}"))
    named = bool(breaker and breaker.strip())
    s.checks.append(Check("Thesis breaker named", named, value="yes" if named else "no", detail=
                          f"Breaker: {breaker}" if named else
                          "No thesis breaker named. A price drop alone never triggers a sale."))
    s.amount_eur = held / 2 if kind == "trim" else held
    s.tranche_eur = s.amount_eur
    s.target_weight = (held - s.amount_eur) / book.total if book.total else 0.0
    s.allowed = not s.blocked_by
    return s


# ---------------------------------------------------- whole-book health

def health(book: Book, m: Mandate, benchmark_return: Optional[float] = None) -> list[Check]:
    """Every rule, on the whole portfolio. Shown on the dashboard."""
    lim = m.limits
    total = book.total or 1.0
    out: list[Check] = []

    n = stock_count(book, m)
    out.append(Check("Single stocks", n <= lim.max_single_stocks,
                     f"{n} of {lim.max_single_stocks}"))

    aw, cap = ai_weight(book, m), lim.ai_cap.get(m.setting)
    out.append(Check("AI-sensitive stocks", aw <= cap + 1e-9, f"{_pct(aw)} of a {_pct(cap)} cap"))

    biggest = max(((t, book.weight(t)) for t in book.holdings if m.is_stock(t)),
                  key=lambda x: x[1], default=(None, 0.0))
    # Price can carry a position over the cap: that's drift, flagged for
    # review rather than force-sold.
    over = biggest[1] > lim.max_position + lim.rebalance_band / 2
    out.append(Check("Largest stock", not over,
                     f"{m.name_of(biggest[0]) if biggest[0] else '-'} at {_pct(biggest[1])}, "
                     f"cap {_pct(lim.max_position)}", blocking=False))

    tw = sum(book.weight(t) for t in book.holdings if m.is_taiwan(t))
    out.append(Check("Taiwan", tw <= lim.taiwan_cap + lim.rebalance_band / 2,
                     f"{_pct(tw)}, cap {_pct(lim.taiwan_cap)}"))

    cw = book.cash / total
    out.append(Check("Cash", cw >= lim.min_cash, f"{_pct(cw)}, floor {_pct(lim.min_cash)}"))

    for bucket in ("index", "bonds", "gold", "stock"):
        target = sum(i.weights.get(m.setting) for i in m.instruments if i.bucket == bucket)
        actual = sum(book.committed(t) for t in book.holdings.keys() | book.pending.keys()
                     if (m.instrument(t).bucket if m.instrument(t) else "stock") == bucket) / total
        drift = actual - target
        out.append(Check(f"Drift: {bucket}", abs(drift) <= lim.rebalance_band,
                         f"{_pct(actual)} vs target {_pct(target)}", blocking=False))

    if book.peak_value:
        dd = 1 - total / book.peak_value
        out.append(Check("Max loss", dd < lim.max_loss,
                         f"{_pct(max(0.0, dd))} below the peak; limit {_pct(lim.max_loss)}"
                         + ("" if dd < lim.max_loss else
                            ". Rule: move 10 points from stocks to euro bonds.")))
    return out
