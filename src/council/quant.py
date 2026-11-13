"""The quant desk: the numbers the council argues about, computed by code.

Members used to look these up on the web, which is where mistakes crept in.
Now they come from Yahoo Finance statements and five years of weekly prices.

Per stock
  prices        1-year volatility, beta and correlation vs the benchmark
                (2 years of weekly returns), worst fall over 3 years,
                12-1 month momentum, 1-year return
  fundamentals  revenue CAGR, operating and FCF margins, net debt to
                EBITDA, approximate ROIC
  valuation     P/E at each year end vs today's and the 5-year average,
                and a reverse DCF (9% discount, 2.5% terminal growth)
Per portfolio
  volatility, historical 1-month VaR (95%), correlation of a candidate
  with what I already own

Approximations: historical P/E uses today's share count; returns are in
the listing currency, so they ignore euro moves.

Offline (tests, demo) the desk reads a saved file of real numbers.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import paths

DISCOUNT = 0.09                 # what a euro investor asks of a stock
TERMINAL = 0.025                # long-run growth after year 10
YEARS = 10
SNAPSHOT = paths.SNAPSHOT.parent / "quant_snapshot.json"


# ---------------------------------------------------------------- maths

def returns(closes: list[float]) -> list[float]:
    return [b / a - 1 for a, b in zip(closes, closes[1:]) if a and b]


def stdev(xs: list[float]) -> Optional[float]:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def cov(a: list[float], b: list[float]) -> Optional[float]:
    n = min(len(a), len(b))
    if n < 2:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (n - 1)


def corr(a: list[float], b: list[float]) -> Optional[float]:
    c, sa, sb = cov(a, b), stdev(a[-min(len(a), len(b)):]), stdev(b[-min(len(a), len(b)):])
    return c / (sa * sb) if c is not None and sa and sb else None


def beta(stock: list[float], bench: list[float]) -> Optional[float]:
    c, v = cov(stock, bench), stdev(bench[-min(len(stock), len(bench)):])
    return c / v ** 2 if c is not None and v else None


def max_drawdown(closes: list[float]) -> Optional[float]:
    """The worst fall from a peak, as a negative fraction (-0.35 = 35% down)."""
    if not closes:
        return None
    peak, worst = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        worst = min(worst, c / peak - 1)
    return worst


def cagr(first: float, last: float, years: float) -> Optional[float]:
    if not first or not last or first <= 0 or last <= 0 or years <= 0:
        return None
    return (last / first) ** (1 / years) - 1


def dcf_value(fcf: float, growth: float, discount: float = DISCOUNT,
              terminal: float = TERMINAL, years: int = YEARS) -> float:
    """Present value of `fcf` growing at `growth` for `years`, then at `terminal`."""
    pv, cf = 0.0, fcf
    for t in range(1, years + 1):
        cf *= 1 + growth
        pv += cf / (1 + discount) ** t
    return pv + cf * (1 + terminal) / (discount - terminal) / (1 + discount) ** years


def implied_growth(enterprise_value: float, fcf: float) -> Optional[float]:
    """The yearly free-cash-flow growth over 10 years that justifies the price.
    None when cash flow is negative: the question has no answer then."""
    if not fcf or fcf <= 0 or not enterprise_value or enterprise_value <= 0:
        return None
    lo, hi = -0.30, 0.80
    if dcf_value(fcf, lo) > enterprise_value:
        return lo
    if dcf_value(fcf, hi) < enterprise_value:
        return hi
    for _ in range(80):
        mid = (lo + hi) / 2
        if dcf_value(fcf, mid) < enterprise_value:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def var_95(weekly: list[float], weeks: int = 4) -> Optional[float]:
    """Historical 1-month value at risk: the 5th-percentile 4-week return."""
    if len(weekly) < weeks * 10:
        return None
    sums = sorted(sum(weekly[i:i + weeks]) for i in range(len(weekly) - weeks + 1))
    return sums[int(0.05 * len(sums))]


# ---------------------------------------------------------------- fetching

def _row(df, *names):
    """{year: value} for the first row of `df` that exists, newest first."""
    for n in names:
        if df is not None and n in getattr(df, "index", []):
            out = {}
            for col, v in df.loc[n].items():
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    continue
                if f == f:
                    out[str(col)[:10]] = f
            if out:
                return dict(sorted(out.items(), reverse=True))
    return {}


def fetch(ticker: str, fx) -> dict:
    """Raw inputs for one stock from Yahoo Finance. `fx(ccy)` = units per euro."""
    import yfinance as yf
    tk = yf.Ticker(ticker)
    info = tk.info or {}
    h = tk.history(period="5y", interval="1wk", auto_adjust=True)
    closes = [(str(i)[:10], float(c)) for i, c in h["Close"].items() if c == c]
    ccy, fin = (info.get("currency") or "").upper(), (info.get("financialCurrency") or info.get("currency") or "").upper()
    to_trading = fx(ccy) / fx(fin) if ccy and fin and ccy != fin else 1.0
    inc, cf, bs = tk.income_stmt, tk.cashflow, tk.balance_sheet
    return {
        "ticker": ticker, "currency": ccy, "financial_currency": fin, "to_trading": to_trading,
        "market_cap": info.get("marketCap"), "closes": closes,
        "revenue": _row(inc, "Total Revenue"), "operating_income": _row(inc, "Operating Income", "EBIT"),
        "ebitda": _row(inc, "EBITDA", "Normalized EBITDA"), "net_income": _row(inc, "Net Income"),
        "ebit": _row(inc, "EBIT", "Operating Income"), "fcf": _row(cf, "Free Cash Flow"),
        "debt": _row(bs, "Total Debt"),
        "cash": _row(bs, "Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"),
        "equity": _row(bs, "Stockholders Equity"),
        "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------- the desk

def _latest(d: dict) -> Optional[float]:
    return next(iter(d.values()), None)


def _weekly_from(closes: list, since: str) -> list[float]:
    return [c for day, c in closes if day >= since]


def analyse(raw: dict, bench: Optional[dict] = None) -> dict:
    """Everything the fact sheet shows, from the raw inputs. Pure: no network."""
    closes = raw.get("closes") or []
    px = [c for _, c in closes]
    out: dict = {"ticker": raw["ticker"], "source": "Yahoo Finance statements and weekly prices",
                 "as_of": raw.get("fetched", ""), "notes": []}
    if len(px) >= 60:
        last_day = closes[-1][0]
        y1 = str(int(last_day[:4]) - 1) + last_day[4:]
        y2 = str(int(last_day[:4]) - 2) + last_day[4:]
        y3 = str(int(last_day[:4]) - 3) + last_day[4:]
        r1 = returns(_weekly_from(closes, y1))
        out["vol_1y"] = stdev(r1) * math.sqrt(52) if stdev(r1) else None
        p1 = _weekly_from(closes, y1)
        out["return_1y"] = p1[-1] / p1[0] - 1 if len(p1) > 1 else None
        out["max_drawdown_3y"] = max_drawdown(_weekly_from(closes, y3))
        if len(px) >= 53:
            out["momentum_12_1"] = px[-5] / px[-53] - 1        # skip the last month, as usual
        if bench and bench.get("closes"):
            b = dict(bench["closes"])
            pairs = [(c, b[d]) for d, c in closes if d >= y2 and d in b]
            if len(pairs) > 30:
                rs, rb = returns([p for p, _ in pairs]), returns([q for _, q in pairs])
                out["beta"] = beta(rs, rb)
                out["corr_bench"] = corr(rs, rb)
    rev = raw.get("revenue") or {}
    years = sorted(rev)
    if len(years) >= 2:
        span = (int(years[-1][:4]) - int(years[0][:4])) or 1
        out["revenue_cagr"] = cagr(rev[years[0]], rev[years[-1]], span)
        out["revenue_years"] = f"{years[0][:4]}-{years[-1][:4]}"
    if rev:
        r_now = _latest(rev)
        oi = raw.get("operating_income") or {}
        if r_now and _latest(oi) is not None:
            out["op_margin"] = _latest(oi) / r_now
            olds = [k for k in sorted(oi) if k in rev]
            if len(olds) >= 2:
                out["op_margin_then"] = oi[olds[0]] / rev[olds[0]]
        if r_now and _latest(raw.get("fcf") or {}) is not None:
            out["fcf_margin"] = _latest(raw["fcf"]) / r_now
    debt, cash, ebitda = _latest(raw.get("debt") or {}), _latest(raw.get("cash") or {}), _latest(raw.get("ebitda") or {})
    if debt is not None and cash is not None:
        out["net_debt"] = debt - cash
        if ebitda and ebitda > 0:
            out["net_debt_ebitda"] = (debt - cash) / ebitda
    ebit, eq = _latest(raw.get("ebit") or {}), _latest(raw.get("equity") or {})
    if ebit is not None and eq is not None and debt is not None and cash is not None:
        capital = eq + debt - cash
        if capital > 0:
            out["roic"] = ebit * (1 - 0.21) / capital
            out["notes"].append("ROIC approximate: EBIT after a 21% tax over equity plus net debt")

    # Valuation history: P/E at each year end, with today's share count.
    mc, k = raw.get("market_cap"), raw.get("to_trading") or 1.0
    ni = raw.get("net_income") or {}
    if mc and px and ni:
        price_now = px[-1]
        hist = {}
        for day, n in ni.items():
            if n <= 0:
                continue
            p_then = next((c for d, c in reversed(closes) if d <= day), None)
            if p_then:
                hist[day[:4]] = mc * (p_then / price_now) / (n * k)
        if hist:
            out["pe_history"] = dict(sorted(hist.items()))
            out["pe_avg"] = sum(hist.values()) / len(hist)
            latest_ni = _latest(ni)
            if latest_ni and latest_ni > 0:
                out["pe_now"] = mc / (latest_ni * k)
                out["pe_vs_avg"] = out["pe_now"] / out["pe_avg"] - 1
            out["notes"].append("P/E history uses today's share count and last close per fiscal year")
    fcf = _latest(raw.get("fcf") or {})
    if mc and fcf:
        ev = mc + (out.get("net_debt") or 0.0) * k
        out["fcf_yield"] = fcf * k / mc
        out["implied_growth"] = implied_growth(ev, fcf * k)
        out["notes"].append(f"Reverse DCF: {YEARS} years, {DISCOUNT:.0%} discount, {TERMINAL:.1%} terminal growth")
    return out


def book_stats(weights: dict[str, float], raws: dict[str, dict]) -> dict:
    """The portfolio's weekly returns as held now: volatility and 1-month VaR."""
    series = {}
    for t, w in weights.items():
        cl = (raws.get(t) or {}).get("closes") or []
        if w > 0 and len(cl) > 60:
            series[t] = dict(cl)
    if not series:
        return {}
    days = sorted(set.intersection(*(set(s) for s in series.values())))[-105:]   # about 2 years
    if len(days) < 30:
        return {}
    tot = sum(weights[t] for t in series)
    port = []
    for a, b in zip(days, days[1:]):
        port.append(sum(weights[t] / tot * (series[t][b] / series[t][a] - 1) for t in series))
    return {"vol": stdev(port) * math.sqrt(52) if stdev(port) else None, "var_1m": var_95(port),
            "weeks": len(port), "covered": tot, "series": port}


def corr_to_book(raw: dict, book: dict) -> Optional[float]:
    """How a stock's weekly returns move with the portfolio's (book_stats)."""
    ser = book.get("series")
    cl = raw.get("closes") or []
    if not ser or len(cl) < 30:
        return None
    r = returns([c for _, c in cl])[-len(ser):]
    return corr(r, ser[-len(r):])


# -------------------------------------------------------------- the store

class Desk:
    """Fetches once a day per ticker; offline it reads the saved file."""

    def __init__(self, steward, store=None, snapshot: Optional[bool] = None, path: Optional[Path] = None):
        self.steward, self.store = steward, store
        self.snapshot = steward.snapshot if snapshot is None else snapshot
        self.path = Path(path or SNAPSHOT)
        self._raw: dict[str, dict] = {}
        if self.snapshot and self.path.exists():
            self._raw = json.loads(self.path.read_text()).get("raw", {})

    def raw(self, ticker: str) -> Optional[dict]:
        if ticker in self._raw:
            return self._raw[ticker]
        if self.snapshot:
            return None
        key = f"quant:{ticker}"
        today = datetime.now(timezone.utc).date().isoformat()
        hit = self.store.get_meta(key) if self.store else None
        if hit and hit.get("fetched", "")[:10] == today:
            self._raw[ticker] = hit
            return hit
        try:
            r = fetch(ticker, self.steward.fx)
        except Exception:
            return hit                    # yesterday's numbers beat none; None if never fetched
        if self.store:
            self.store.set_meta(key, r)
        self._raw[ticker] = r
        return r

    def quarters(self, ticker: str) -> dict[str, dict]:
        """{quarter end: {revenue, op}} from the quarterly statements, newest first.
        Used to score the members' forecasts. Empty offline."""
        if self.snapshot:
            return {}
        key = f"quarters:{ticker}"
        today = datetime.now(timezone.utc).date().isoformat()
        hit = self.store.get_meta(key) if self.store else None
        if hit and hit.get("fetched", "")[:10] == today:
            return hit["q"]
        try:
            import yfinance as yf
            df = yf.Ticker(ticker).quarterly_income_stmt
            rev, op = _row(df, "Total Revenue"), _row(df, "Operating Income", "EBIT")
            q = {d: {"revenue": rev[d], "op": op.get(d)} for d in rev}
        except Exception:
            return (hit or {}).get("q", {})
        if self.store:
            self.store.set_meta(key, {"fetched": today, "q": q})
        return q

    def stock(self, ticker: str, benchmark: str) -> dict:
        r = self.raw(ticker)
        if not r:
            return {}
        return analyse(r, self.raw(benchmark))

    def book(self, weights: dict[str, float]) -> dict:
        raws = {t: self.raw(t) for t in weights}
        return book_stats(weights, {t: r for t, r in raws.items() if r})

    def save(self, tickers: list[str]) -> Path:
        out = {t: r for t in tickers if (r := self.raw(t))}
        self.path.write_text(json.dumps({"note": "Real Yahoo Finance statements and weekly prices, saved "
                                                 "for the offline demo and tests.",
                                         "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                         "raw": out}))
        return self.path


# ------------------------------------------------------------- formatting

def _p(x: Optional[float], d: int = 1, sign: bool = False) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:+.{d}f}%" if sign else f"{x * 100:.{d}f}%"


def lines(q: dict, corr_book: Optional[float] = None) -> list[str]:
    """The quant desk as fact-sheet lines."""
    if not q:
        return ["- No quant data for this name."]
    pe_hist = q.get("pe_history") or {}
    out = [
        f"- Prices (weekly, listing currency): volatility {_p(q.get('vol_1y'), 0)} a year; beta "
        f"{q['beta']:.2f} vs the benchmark" if q.get("beta") is not None else
        f"- Prices (weekly, listing currency): volatility {_p(q.get('vol_1y'), 0)} a year",
        f"- Return 1 year {_p(q.get('return_1y'), 0, True)}; momentum 12-1 months {_p(q.get('momentum_12_1'), 0, True)}; "
        f"worst fall in 3 years {_p(q.get('max_drawdown_3y'), 0)}"
        + (f"; correlation with our portfolio {corr_book:.2f}" if corr_book is not None else ""),
        f"- Revenue growth {_p(q.get('revenue_cagr'))} a year ({q.get('revenue_years', '?')}); operating margin "
        f"{_p(q.get('op_margin'))} (was {_p(q.get('op_margin_then'))}); free-cash-flow margin {_p(q.get('fcf_margin'))}",
        f"- Net debt to EBITDA {q['net_debt_ebitda']:.1f}x" if q.get("net_debt_ebitda") is not None
        else "- Net debt to EBITDA: n/a",
        f"- Return on invested capital about {_p(q.get('roic'))}",
        "- P/E by year end: " + (", ".join(f"{y} {v:.0f}" for y, v in pe_hist.items()) or "n/a")
        + (f"; average {q['pe_avg']:.1f}, now {q['pe_now']:.1f} ({_p(q.get('pe_vs_avg'), 0, True)} vs its average)"
           if q.get("pe_now") and q.get("pe_avg") else ""),
        f"- Free-cash-flow yield {_p(q.get('fcf_yield'))}; reverse DCF: the price assumes free cash flow grows "
        f"{_p(q.get('implied_growth'))} a year for 10 years"
        if q.get("implied_growth") is not None else "- Reverse DCF: n/a (free cash flow is negative or missing)",
    ]
    if q.get("notes"):
        out.append("- Method: " + "; ".join(q["notes"]))
    return out
