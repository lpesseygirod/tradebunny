"""The control interface — one local Flask app, one file, no build step.

This is the "you" in the diagram: the analyst proposes, the executor obeys, and
everything in between happens here. Nothing on this page trades. It approves,
rejects and stops; the executor does the rest by reading what was written.

WHERE THE STATE LIVES. Plans are JSON files under `data/plans/`, read and
written through `plan.save()` / `plan.load_all()`. That is deliberately the same
mechanism the executor uses — `plan.py` exists so state can be rebuilt from the
plan plus the exchange after a crash, never from a process's memory. Control
state is `data/control.json` via `control.Control`. There is no database here:
the grid side of this repo has never had one (`ledger/store.py` is the separate
news-prediction ledger, with `predictions` and `runs` tables and nothing about
grids), and adding a second source of truth that the executor does not read
would be the surest way to have the UI and the bot disagree about what is
running.

THE SCAN RUNS IN A THREAD. A full scan is 30-90 seconds — a candle fetch per
symbol, then one AI call per survivor — so `POST /scan` starts a worker and
returns immediately. The page polls `/scan/status` every 2s. One scan at a time;
a second request while one is running is refused rather than queued. If the
worker raises, the traceback's summary is stored and rendered on the page: a
spinner that never resolves tells you nothing, and the most likely failures here
(a 403 from OKX, a missing API key) are exactly the ones you need to read.

ACCESS. Bound to 0.0.0.0 so a phone on the same wifi can reach it, which makes
the token mandatory rather than optional — this page can halt trading and commit
capital. `GRID_WEB_TOKEN` must be set or the app refuses to start. Every POST
carries the token as a form field as well, so a page on another site cannot make
your browser act on your behalf using only the cookie.
"""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from flask import (Flask, Response, abort, flash, get_flashed_messages,
                   redirect, request, url_for)

from . import analyst, okx
from .analyst import ScanReport
from .control import Control
from . import confidence as conf
from . import freshness
from . import execution, sizing
from . import store as gridstore
from .executor import HEARTBEAT_PATH, STATE_DIR, load_heartbeat, load_state
from .fees import FeeModel
from .plan import GridPlan, Status, load_all

PLANS_DIR = Path(os.getenv("TRADEBUNNY_HOME", ".")).resolve() / "data" / "plans"

#: Capital levels the paper grids are tracked against. Switching between them
#: re-derives every figure from the SAME fills, so you can see what a given
#: capital would have produced without running it.
HYPOTHETICAL_SIZES = (50.0, 200.0, 500.0, 2000.0)

#: OKX spot instruments to scan. No grid universe exists in config.yaml — that
#: file's `universe` is the ledger's US equity list and is not applicable here.
DEFAULT_UNIVERSE = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "DOGE-USDT", "ADA-USDT",
    "LTC-USDT", "LINK-USDT", "DOT-USDT", "AVAX-USDT", "ATOM-USDT", "UNI-USDT",
    "FIL-USDT", "APT-USDT", "ARB-USDT", "OP-USDT", "NEAR-USDT", "INJ-USDT",
]


def universe() -> list[str]:
    raw = os.getenv("GRID_UNIVERSE", "")
    return [s.strip().upper() for s in raw.split(",") if s.strip()] or DEFAULT_UNIVERSE


# --------------------------------------------------------------- scan state

@dataclass
class ScanState:
    """What the poller reports. Guarded by `ScanRunner.lock`."""

    running: bool = False
    done: int = 0
    total: int = 0
    current: str = ""
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    proposed: list[str] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)
    below_threshold: list[tuple[str, float]] = field(default_factory=list)
    level: str = ""

    def as_dict(self) -> dict[str, Any]:
        label = "idle"
        if self.running:
            label = (f"scanning… {self.done}/{self.total} symbols"
                     + (f" ({self.current})" if self.current else ""))
        elif self.error:
            label = "scan failed"
        elif self.finished_at:
            label = (f"proposed {len(self.proposed)}"
                     if self.proposed else "finished — nothing passed the gates")
        return {"running": self.running, "done": self.done, "total": self.total,
                "current": self.current, "label": label, "error": self.error,
                "below_threshold": self.below_threshold,
                "level": self.level,
                "proposed": self.proposed, "rejections": self.rejections,
                "started_at": self.started_at, "finished_at": self.finished_at}


class _ProgressSource:
    """Wraps `grid.okx` to count symbols as they are fetched.

    A proxy rather than a progress callback threaded through `analyst.scan`,
    because the analyst's signature should not grow a UI concern — and one
    fetch per symbol is exactly the thing worth counting.
    """

    def __init__(self, inner, on_symbol):
        self._inner = inner
        self._on_symbol = on_symbol

    def bars_per_day(self, bar):
        return self._inner.bars_per_day(bar)

    def get_spot_tickers(self):
        return self._inner.get_spot_tickers()

    def get_instruments(self, inst_type="SPOT"):
        return self._inner.get_instruments(inst_type)

    def get_trade_fee(self, inst_id="BTC-USDT", inst_type="SPOT"):
        return self._inner.get_trade_fee(inst_id, inst_type)

    def get_candles(self, inst_id, bar="1H", limit=300):
        self._on_symbol(inst_id)
        return self._inner.get_candles(inst_id, bar, limit=limit)


class ScanRunner:
    """One scan at a time, in a daemon thread."""

    def __init__(self, control: Control, source=okx, client=None,
                 plans_dir: Path = PLANS_DIR, notify=None):
        self.notify = notify or _default_notify
        self.control = control
        self.source = source
        self.client = client
        self.plans_dir = plans_dir
        self.lock = threading.Lock()
        self.state = ScanState()
        self.thread: Optional[threading.Thread] = None

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return self.state.as_dict()

    def start(self, symbols: list[str], available_quote: float,
              bar: str = "1H") -> tuple[bool, str]:
        """Begin a scan. Returns (started, message)."""
        with self.lock:
            if self.state.running:
                return False, "a scan is already running"
            self.state = ScanState(running=True, total=len(symbols),
                                   started_at=_now())
        self.thread = threading.Thread(
            target=self._run, args=(symbols, available_quote, bar), daemon=True)
        self.thread.start()
        return True, f"scanning {len(symbols)} symbols"

    def _tick(self, symbol: str) -> None:
        with self.lock:
            self.state.done += 1
            self.state.current = symbol

    def _run(self, symbols: list[str], available_quote: float, bar: str) -> None:
        report = ScanReport()
        try:
            plans = analyst.scan(
                symbols, available_quote, bar,
                control=self.control, client=self.client,
                source=_ProgressSource(self.source, self._tick), report=report)
            for p in plans:
                p.save(self.plans_dir)
            if plans:
                lines = "\n".join(
                    f"• {p.symbol}  {p.lower:.6g}–{p.upper:.6g}  {p.levels} levels"
                    for p in plans)
                self.notify(f"📋 TradeBunny: {len(plans)} proposal(s) awaiting "
                            f"approval\n\n{lines}")
            with self.lock:
                self.state.proposed = [p.symbol for p in plans]
                self.state.rejections = [str(r) for r in report.rejections]
                self.state.below_threshold = list(report.below_threshold)
                self.state.level = report.level
                # `analyst.scan` returns [] rather than raising when it aborts —
                # right for a CLI run, but here it would render as "nothing
                # passed the gates", which is the silent failure this page
                # exists to prevent. The report carries the real reason.
                if report.error:
                    self.state.error = report.error
        except Exception as e:
            # Deliberately broad. Whatever went wrong, the owner needs to read
            # it on the page rather than watch a spinner forever. The one-line
            # summary goes to the UI; the full traceback to the log.
            with self.lock:
                self.state.error = f"{type(e).__name__}: {e}"
            print("scan failed:\n" + traceback.format_exc(), flush=True)
        finally:
            with self.lock:
                self.state.running = False
                self.state.current = ""
                self.state.finished_at = _now()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_notify(message: str) -> None:
    """Telegram, best effort. Never blocks a scan or changes its outcome."""
    try:
        from ledger import notify as _n
        _n.send(message)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            "could not notify (%s: %s): %s", type(e).__name__, e, message)


# ----------------------------------------------------------------- rendering

CSS = """
:root{--bg:#0f1115;--card:#171a21;--line:#252a35;--fg:#e6e8ee;--dim:#9aa3b2;
--good:#3fb950;--bad:#f85149;--warn:#d29922;--accent:#4c8dff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,
BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:0 0 4rem}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);
padding:.9rem 1rem;z-index:5}
h1{font-size:1.05rem;margin:0;letter-spacing:.02em}
main{padding:1rem;max-width:640px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:1rem;margin:0 0 1rem}
.sym{font-size:1.15rem;font-weight:600;margin:0 0 .5rem}
dl{display:grid;grid-template-columns:1fr auto;gap:.3rem .8rem;margin:.6rem 0}
dt{color:var(--dim);font-size:.85rem}
dd{margin:0;text-align:right;font-variant-numeric:tabular-nums;font-size:.9rem}
.thesis{color:var(--dim);font-size:.9rem;border-left:2px solid var(--line);
padding-left:.7rem;margin:.8rem 0}
.muted{color:var(--dim);font-size:.85rem}
.good{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}
form{margin:0}
input[type=number],input[type=text]{width:100%;padding:.7rem;font-size:1rem;
background:#0b0d11;color:var(--fg);border:1px solid var(--line);border-radius:8px}
label{display:block;font-size:.8rem;color:var(--dim);margin:.8rem 0 .3rem}
button{width:100%;padding:.8rem;font-size:1rem;font-weight:600;border:0;
border-radius:8px;margin-top:.5rem;cursor:pointer;background:var(--line);color:var(--fg)}
button.approve{background:var(--good);color:#04240c}
button.reject{background:#2a2f3a}
button.danger{background:var(--bad);color:#2b0705}
.row{display:flex;gap:.5rem}.row button{margin-top:0}
.alarm{background:#3d0d0a;border:2px solid var(--bad);border-radius:10px;
padding:1rem;margin:0 0 1rem;color:#ffd9d6}
.alarm b{color:#fff;display:block;font-size:1.05rem;margin-bottom:.35rem}
.flash{background:#1d2430;border:1px solid var(--accent);border-radius:8px;
padding:.7rem 1rem;margin:0 0 1rem;font-size:.9rem}
.flash.err{border-color:var(--bad)}
#status{font-size:.9rem;color:var(--dim);margin:.4rem 0 0}
/* PAPER and LIVE are different worlds. Not a badge — a whole environment:
   different ground, different border, a label that never scrolls away. */
.mode{border-radius:12px;padding:.9rem;margin:0 0 1.1rem;border:2px solid}
.mode.paper{background:#0d1a14;border-color:#2f6b4a}
.mode.live{background:#241014;border-color:var(--bad)}
.mode>h2{margin:0 0 .2rem;font-size:.95rem;letter-spacing:.14em;
text-transform:uppercase}
.mode.paper>h2{color:#5fd39b}
.mode.live>h2{color:#ff8b84}
.mode>.sub{margin:0 0 .8rem;font-size:.78rem;color:var(--dim)}
.modebar{position:sticky;top:0;z-index:4;font-size:.7rem;letter-spacing:.16em;
text-transform:uppercase;padding:.3rem .6rem;border-radius:6px;display:inline-block}
.modebar.paper{background:#2f6b4a;color:#eafff4}
.modebar.live{background:var(--bad);color:#fff}
.figs{display:grid;grid-template-columns:1fr auto;gap:.25rem .8rem;margin:0}
.figs dt{color:var(--dim);font-size:.82rem}
.figs dd{margin:0;text-align:right;font-variant-numeric:tabular-nums;
font-size:.9rem}
.total{font-size:1.5rem;font-weight:700;font-variant-numeric:tabular-nums}
.ladder{margin:.8rem 0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
font-size:.72rem;line-height:1.5}
.ladder div{display:flex;align-items:center;gap:.5rem;white-space:nowrap}
.ladder .bar{flex:1;height:2px;background:var(--line)}
.ladder .held .bar{background:var(--good);height:4px}
.ladder .here{color:var(--accent);font-weight:700}
.ladder .here .bar{background:var(--accent);height:3px}
.ladder .stop{color:var(--bad)}
.ladder .stop .bar{background:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:.76rem;
font-variant-numeric:tabular-nums}
th{text-align:right;color:var(--dim);font-weight:600;padding:.35rem .3rem;
border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--card)}
th:first-child,td:first-child{text-align:left}
td{padding:.3rem;border-bottom:1px solid #1d2129}
td.buy{color:var(--good)}td.sell{color:var(--accent)}
.scroll{max-height:70vh;overflow:auto;-webkit-overflow-scrolling:touch}
.feed li{list-style:none;padding:.5rem 0;border-bottom:1px solid var(--line)}
.feed li.unread{border-left:3px solid var(--accent);padding-left:.6rem}
.feed .k{font-size:.68rem;color:var(--dim);text-transform:uppercase;
letter-spacing:.08em}
.count{background:var(--accent);color:#04122e;border-radius:999px;
padding:.05rem .45rem;font-size:.7rem;font-weight:700;margin-left:.4rem}
.sizes{display:flex;gap:.4rem;flex-wrap:wrap;margin:.5rem 0}
.sizes a{padding:.3rem .7rem;border-radius:999px;background:var(--line);
color:var(--dim);text-decoration:none;font-size:.8rem}
.sizes a.on{background:var(--accent);color:#04122e;font-weight:700}
.blocked{color:var(--bad)}
a.card-link{color:inherit;text-decoration:none;display:block}
.stamp{font-size:.7rem;color:var(--dim);text-align:right;margin:.3rem 0 0}
.conf{display:flex;align-items:center;gap:.6rem;margin:.6rem 0}
.confbar{flex:1;height:8px;background:var(--line);border-radius:999px;overflow:hidden}
.confbar i{display:block;height:100%;background:var(--good)}
.confbar i.mid{background:var(--warn)}.confbar i.low{background:var(--bad)}
.confnum{font-variant-numeric:tabular-nums;font-weight:600;min-width:3.2rem;text-align:right}
.kelly{background:#3d0d0a;border:2px solid var(--bad);border-radius:8px;
padding:.7rem;margin:.7rem 0;color:#ffd9d6;font-size:.88rem}
.kelly b{color:#fff;display:block;margin-bottom:.25rem}
.binds{color:var(--warn);font-weight:600}
.fresh{display:flex;flex-wrap:wrap;gap:.4rem;margin:.4rem 0 0}
.fresh span{font-size:.72rem;padding:.15rem .5rem;border-radius:999px;
background:var(--line);color:var(--dim)}
.fresh span.bad{background:var(--bad);color:#fff}
.uncal{background:#3a2f10;border:1px solid var(--warn);border-radius:8px;
padding:.7rem 1rem;margin:0 0 1rem;font-size:.85rem;color:#ffe9b8}
.pill{display:inline-block;font-size:.72rem;padding:.15rem .5rem;border-radius:999px;
background:var(--line);color:var(--dim);margin-left:.4rem;vertical-align:middle}
.pill.live{background:var(--bad);color:#fff}
details{margin-top:.6rem}summary{cursor:pointer;color:var(--dim);font-size:.85rem}
pre{white-space:pre-wrap;word-break:break-word;font-size:.78rem;color:var(--dim)}
hr{border:0;border-top:1px solid var(--line);margin:1.5rem 0}
"""

POLL_JS = """
(function(){
  var box=document.getElementById('status');
  var stamp=document.getElementById('stamp');
  var btn=document.getElementById('scanbtn');
  var timer=null, scanning=false;

  function paint(s){
    if(!s) return;
    if(stamp){ stamp.textContent='updated '+new Date().toLocaleTimeString(); }
    var sc=s.scan||{};
    if(box){
      box.textContent = sc.error ? ('scan failed — '+sc.error) : (sc.label||'');
      box.className = sc.error ? 'bad' : 'muted';
    }
    if(btn){ btn.disabled = !!sc.running; }
    /* A scan that has just finished changes the proposal list, and an
       executor that has just filled changes every figure. Rather than
       patch the DOM piecemeal and risk it disagreeing with the server,
       reload once the run ends. */
    if(sc.running){ scanning=true; }
    else if(scanning){ scanning=false; location.reload(); }
    var u=document.getElementById('unread');
    if(u && typeof s.unread==='number'){
      u.textContent = s.unread>0 ? s.unread : '';
      u.style.display = s.unread>0 ? '' : 'none';
    }
    var g=document.getElementById('grand');
    if(g && typeof s.grand_total==='number'){
      g.textContent=(s.grand_total>0?'+':'')+s.grand_total.toFixed(2);
      g.className='total '+(s.grand_total>0?'good':(s.grand_total<0?'bad':''));
    }
  }

  function tick(){
    fetch('/api/state?token='+TOKEN,{cache:'no-store'})
      .then(function(r){return r.json()})
      .then(paint)
      .catch(function(e){
        if(stamp){ stamp.textContent='POLLING STALLED — '+e; }
      });
  }

  tick();
  timer=setInterval(tick,3000);
  /* Coming back to the tab should show current state immediately, not
     whatever was true up to three seconds before you looked. */
  document.addEventListener('visibilitychange',function(){
    if(!document.hidden){ tick(); }
  });
  window.addEventListener('focus',tick);
})();
"""


def _esc(s: Any) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _pct(x: Optional[float], digits: int = 2) -> str:
    return "—" if x is None else f"{x:.{digits}%}"


def _money(x: Optional[float], dp: int = 2) -> str:
    if x is None:
        return "—"
    sign = "+" if x > 0 else ""
    return f"{sign}{x:,.{dp}f}"


def _cls(x: Optional[float]) -> str:
    if x is None or abs(x) < 1e-9:
        return ""
    return "good" if x > 0 else "bad"


def _mode_section(mode: str, tot: Any, n_grids: int, extra: str = "") -> str:
    """One of the two worlds, rendered so it cannot be mistaken for the other.

    Real money and pretend money get different backgrounds, different border
    colours and a permanent uppercase heading — not a small badge that is easy
    to miss on a phone at a glance.
    """
    label = "PAPER — not real money" if mode == gridstore.PAPER else \
        "LIVE — REAL MONEY"
    sub = ("Simulated fills against PaperExchange. Nothing here touches an "
           "exchange." if mode == gridstore.PAPER else
           "These are real orders on OKX with real funds.")
    bh = ""
    if tot.buy_and_hold is not None:
        verdict = ("the grid is ahead" if tot.beats_buy_and_hold
                   else "buy-and-hold is ahead")
        bh = (f'<dt>buy &amp; hold, same period</dt>'
              f'<dd class="{_cls(tot.buy_and_hold)}">{_money(tot.buy_and_hold)}'
              f'</dd><dt>&nbsp;</dt><dd class="muted">{verdict}</dd>')
    return f"""
<section class="mode {mode}">
  <h2>{label}</h2>
  <p class="sub">{sub}</p>
  <p class="total {_cls(tot.total)}">{_money(tot.total)}</p>
  <dl class="figs">
    <dt>grids</dt><dd>{n_grids}</dd>
    <dt>invested</dt><dd>{_money(tot.invested)}</dd>
    <dt>current value</dt><dd>{_money(tot.current_value)}</dd>
    <dt>realised P&amp;L</dt>
      <dd class="{_cls(tot.realised)}">{_money(tot.realised)}</dd>
    <dt>unrealised P&amp;L</dt>
      <dd class="{_cls(tot.unrealised)}">{_money(tot.unrealised)}</dd>
    <dt>fees paid</dt><dd>{_money(-abs(tot.fees))}</dd>
    <dt>transactions</dt><dd>{tot.n_transactions}</dd>
    {bh}
  </dl>
  {extra}
</section>"""


def _ladder(plan: GridPlan, price: Optional[float],
            held_levels: Optional[set] = None) -> str:
    """The ladder drawn, with filled rungs marked and price located.

    Newest at the top means highest price at the top, which is how a ladder
    reads on a chart.
    """
    held = held_levels or set()
    rows = []
    prices = plan.grid_prices
    for i in range(len(prices) - 1, -1, -1):
        lv = prices[i]
        classes = []
        if i in held:
            classes.append("held")
        if price is not None and i > 0 and prices[i - 1] <= price <= lv:
            classes.append("here")
        mark = "@" if "here" in classes else ("#" if i in held else "-")
        rows.append(f'<div class="{" ".join(classes)}"><span>{mark} '
                    f'{lv:>12.6g}</span><span class="bar"></span></div>')
    stop = (f'<div class="stop"><span>x {plan.stop_below:>12.6g}</span>'
            f'<span class="bar"></span></div>')
    now = ("" if price is None else
           f'<p class="muted">price now {price:.6g}</p>')
    return f'<div class="ladder">{"".join(rows)}{stop}</div>{now}'


def _statement(txs: list, scale: float = 1.0) -> str:
    """Every transaction, like a bank statement. Newest first."""
    if not txs:
        return ('<p class="muted">No transactions yet. Nothing has filled on '
                'this grid.</p>')
    rows = []
    for t in txs:
        rows.append(
            f'<tr><td>{_esc(t.ts[5:16].replace("T", " "))}</td>'
            f'<td class="{t.side}">{t.side}</td>'
            f'<td>{t.price:,.6g}</td>'
            f'<td>{t.qty * scale:,.6g}</td>'
            f'<td>{t.value * scale:,.2f}</td>'
            f'<td>{t.fee * scale:,.4f}</td>'
            f'<td>{t.position * scale:,.6g}</td>'
            f'<td class="{_cls(t.realised)}">{t.realised * scale:,.2f}</td>'
            f'</tr>')
    return f"""
<div class="scroll"><table>
  <thead><tr><th>when</th><th>side</th><th>price</th><th>qty</th>
    <th>value</th><th>fee</th><th>position</th><th>running P&amp;L</th></tr></thead>
  <tbody>{"".join(rows)}</tbody>
</table></div>"""


def _feed(events: list, token: str, limit: int = 60) -> str:
    """Every event, newest first. Nothing is ever deleted."""
    if not events:
        return '<p class="muted">Nothing has happened yet.</p>'
    items = []
    for e in events[:limit]:
        items.append(
            f'<li class="{"unread" if e.unread else ""}">'
            f'<span class="k">{_esc(e.kind)} · {_esc(e.ts[5:16].replace("T", " "))}'
            f'</span><br>{_esc(e.title)}'
            + (f'<br><span class="muted">{_esc(e.body[:300])}</span>'
               if e.body else "")
            + "</li>")
    return f'<ul class="feed">{"".join(items)}</ul>'


def _size_selector(current: float, plan_id: str, token: str) -> str:
    links = "".join(
        f'<a class="{"on" if abs(s - current) < 1e-9 else ""}" '
        f'href="/grid/{_esc(plan_id)}?token={_esc(token)}&amp;size={s:g}">'
        f'{s:,.0f}</a>' for s in HYPOTHETICAL_SIZES)
    return (f'<p class="muted">Hypothetical capital — the same fills, sized as '
            f'if you had committed:</p><div class="sizes">{links}</div>')


def _proposal_card(p: GridPlan, token: str, fees: FeeModel,
                   live_armed: bool = False) -> str:
    bt = p.backtest or {}
    oos = bt.get("total_return_pct")
    dd = bt.get("max_drawdown_pct")
    below = 1.0 - (p.stop_below / p.lower) if p.lower else 0.0
    fee_share = fees.fee_share(p.min_spacing_pct) if p.min_spacing_pct > 0 else None

    oos_cls = "good" if (oos or 0) > 0 else "bad"
    fee_cls = "warn" if (fee_share or 0) > 0.25 else ""

    lvl = ("" if p.confidence >= 0.65 else
           " mid" if p.confidence >= 0.50 else " low")
    calib_block = ""
    if getattr(p, "sizing_mode", "kelly") == "calibration":
        calib_block = (
            f'<div class="uncal"><b>{_esc(sizing.CALIBRATION_LABEL.split(chr(8212))[0].strip())}</b><br>'
            f'{_esc(sizing.CALIBRATION_LABEL)}<br><br>'
            f'<b>For comparison:</b> {_esc(p.kelly_would_say)}</div>')
    kelly_block = ""
    if p.kelly_full <= 0:
        kelly_block = (
            f'<div class="kelly"><b>&#9888; KELLY SAYS DO NOT TAKE THIS</b>'
            f'Full Kelly is {p.kelly_full:+.3f} — at the estimated win rate and '
            f'payoff this grid has negative expectation, and the '
            f'mathematically correct size is zero. The amount below is a cap '
            f'from the other constraints, not a recommendation.</div>')
    rows = "".join(
        f'<dt>{_esc(k)}</dt><dd class="{"binds" if k in p.size_binding else ""}">'
        f'{v:,.2f}{" &larr; binds" if k in p.size_binding else ""}</dd>'
        for k, v in sorted((p.size_constraints or {}).items(),
                           key=lambda kv: kv[1]))
    return f"""
<div class="card">
  <p class="sym">{_esc(p.symbol)}</p>
  <div class="conf">
    <span>confidence</span>
    <span class="confbar"><i class="{lvl.strip()}"
      style="width:{max(2, min(100, p.confidence * 100)):.0f}%"></i></span>
    <span class="confnum">{p.confidence:.2f}</span>
  </div>
  {calib_block}
  {kelly_block}
  <dl>
    <dt>range</dt><dd>{p.lower:.6g} – {p.upper:.6g}</dd>
    <dt>levels</dt><dd>{p.levels}</dd>
    <dt>narrowest spacing</dt><dd>{_pct(p.min_spacing_pct)}</dd>
    <dt>fees, share of gross</dt>
      <dd class="{fee_cls}">{_pct(fee_share, 1)}</dd>
    <dt>walk-forward, out of sample</dt>
      <dd class="{oos_cls}">{_pct(oos)}</dd>
    <dt>max drawdown</dt><dd>{_pct(dd)}</dd>
    <dt>stop</dt><dd>{p.stop_below:.6g} <span class="muted">({_pct(below, 1)} below)</span></dd>
  </dl>
  <details><summary>why this size — every constraint, and which binds</summary>
    <dl>{rows}
      <dt>minimum viable capital</dt><dd>{p.min_viable_capital:,.2f}</dd>
      <dt>Kelly (full / quarter)</dt>
        <dd>{p.kelly_full:+.3f} / {p.kelly_quarter:+.3f}</dd>
    </dl></details>
  <details><summary>confidence components</summary><dl>{"".join(
      f'<dt>{_esc(k)}</dt><dd>{v:.2f} &times; {conf.WEIGHTS.get(k, 0):.3g}</dd>'
      for k, v in (p.confidence_components or {}).items())}</dl>
    <p class="muted">Weights are documented in <code>grid/confidence.py</code>.</p>
  </details>
  <p class="thesis">{_esc(p.thesis) if p.thesis
      else '<span class="muted">no thesis — the explanation step was unavailable. '
           'The measurements above are unaffected.</span>'}</p>
  <form method="post" action="/decide/{_esc(p.id)}">
    <input type="hidden" name="token" value="{_esc(token)}">
    <label for="amt-{_esc(p.id)}">amount to commit (quote currency)</label>
    <input id="amt-{_esc(p.id)}" type="number" name="amount" step="any" min="0"
           value="{p.max_quote_total:.2f}">
    <div class="row">
      <button class="approve" name="action" value="paper" style="flex:1">
        Paper</button>
      <button class="reject" name="action" value="reject" style="flex:1">
        Reject</button>
    </div>
    <button class="danger" name="action" value="live"
      {'' if live_armed else 'disabled'}>Live — REAL MONEY</button>
    {'' if live_armed else
     '<p class="muted">Live is disabled because <code>control.json</code> has '
     '<code>live_trading = false</code>. Arm it deliberately with '
     '<code>set_live(True, confirm=&quot;I ACCEPT THE RISK&quot;)</code>; the '
     'server refuses a live approval either way, so this button is not the '
     'only thing standing between you and real orders.</p>'}
  </form>
  <details><summary>plan {_esc(p.id)}</summary><pre>{_esc(p.to_json())}</pre></details>
</div>"""


def _running_card(p: GridPlan, token: str, halted: bool,
                  state_dir: Path = STATE_DIR) -> str:
    # The executor's own snapshot, written at the end of every pass. Read from
    # the same file it wrote rather than re-derived here, so the page cannot
    # disagree with the bot about what is resting.
    st = load_state(p.id, state_dir)
    if st is None:
        live_rows = ('<dt>executor</dt><dd class="muted">no pass recorded yet</dd>')
    else:
        live_rows = (
            f'<dt>last price</dt><dd>{st.price:.6g}</dd>'
            f'<dt>units held</dt><dd>{st.units_held:.6g}</dd>'
            f'<dt>deployed</dt><dd>{st.quote_deployed:.2f}</dd>'
            f'<dt>resting orders</dt><dd>{st.open_buys} buy / {st.open_sells} sell</dd>'
            f'<dt>last pass</dt><dd class="muted">{_esc(st.updated_at or "—")}</dd>'
            + (f'<dt>note</dt><dd class="bad">{_esc(st.note)}</dd>' if st.note else ""))
    return f"""
<div class="card">
  <p class="sym">{_esc(p.symbol)}
    <span class="pill">{_esc(p.status.value)}</span>
    {'<span class="pill live">halted</span>' if halted else ''}</p>
  <dl>
    <dt>range</dt><dd>{p.lower:.6g} – {p.upper:.6g}</dd>
    <dt>levels</dt><dd>{p.levels}</dd>
    <dt>committed</dt><dd>{p.max_quote_total:.2f}</dd>
    <dt>stop</dt><dd>{p.stop_below:.6g}</dd>
    {live_rows}
  </dl>
  <form method="post" action="/stop/{_esc(p.id)}">
    <input type="hidden" name="token" value="{_esc(token)}">
    <button class="danger">Stop this grid</button>
  </form>
</div>"""


def _heartbeat_banner(heartbeat_path: Path, running_grids: int) -> str:
    """The loudest thing on the page, and deliberately so.

    A stopped executor is not a paused system. `plan.stop_below` is enforced by
    `executor.run_once` and by nothing else — the exchange is holding limit
    orders and has never heard of the stop price. So while this banner is up,
    every open grid is running without a stop-loss, and the honest way to say
    that is to say it.
    """
    beat = load_heartbeat(heartbeat_path)
    if beat is not None and not beat.is_down():
        return ""
    if beat is None:
        detail = "No heartbeat has ever been written."
    elif beat.stopped_cleanly:
        detail = (f"It was stopped deliberately "
                  f"{beat.age_seconds():.0f}s ago after {beat.passes} passes.")
    else:
        detail = (f"Last heartbeat {beat.age_seconds():.0f}s ago "
                  f"(pid {beat.pid}, {beat.passes} passes)."
                  + (f" Last error: {_esc(beat.last_error)}" if beat.last_error else ""))
    exposure = (f"{running_grids} grid(s) are open with no stop-loss."
                if running_grids else
                "No grids are open, so nothing is exposed right now.")
    return (f'<div class="alarm"><b>⚠ THE EXECUTOR IS NOT RUNNING</b>'
            f'{detail} {exposure}<br><br>'
            f'The stop is enforced by the executor, not by the exchange. '
            f'Start it with <code>python -m grid.cli supervise</code>.</div>')


def _freshness_panel() -> str:
    """Age of every input, in the header. One glance, no reasoning."""
    from . import analyst as _an
    rep = freshness.FreshnessReport()
    for src in ("candles", "tickers", "instruments"):
        rep.add(src, okx.fetched_at(src))
    try:
        _, label = _an.live_fees()
    except Exception:
        label = ""
    rep.add("fees", okx.fetched_at("fees"), detail=label)
    chips = "".join(
        f'<span class="{"" if s.is_fresh() else "bad"}">{_esc(s.source)} '
        f'{_esc(freshness.human_age(s.age()))}</span>' for s in rep.stamps)
    return f'<div class="fresh">{chips}</div>'


def _execution_panel() -> str:
    """Execution quality, ABOVE profit and loss.

    At small capital P&L is noise and this is the signal, so it is rendered
    first and P&L is labelled accordingly.
    """
    q = execution.load()
    def pct(x, dp=1):
        return "—" if x is None else f"{x * 100:.{dp}f}%"
    note = ""
    if q.all_paper and q.fills:
        note = ('<p class="muted">Every fill is from PaperExchange, which fills '
                'at the level price by construction — zero slippage here is the '
                'simulator working, not the market.</p>')
    return f"""
<div class="card">
  <p class="sym">Execution quality</p>
  <p class="muted">The signal at small capital. P&amp;L at this size is noise.</p>
  <dl>
    <dt>fills</dt><dd>{q.n_fills} of {q.orders_placed} orders</dd>
    <dt>fill rate</dt><dd>{pct(q.fill_rate)}</dd>
    <dt>mean slippage</dt><dd>{pct(q.mean_slippage, 3)}</dd>
    <dt>worst slippage</dt><dd>{pct(q.worst_slippage, 3)}</dd>
    <dt>fee vs FeeModel</dt><dd>{pct(q.mean_fee_error, 2)}</dd>
    <dt>executor uptime</dt><dd>{pct(q.uptime)} over {q.passes} passes</dd>
    <dt>reconnects</dt><dd>{q.reconnects}</dd>
  </dl>
  {note}
  <div class="{'flash' if q.ready_for_kelly else 'uncal'}">{_esc(q.readiness())}</div>
</div>"""


def _calibration_banner(plans_dir: Path) -> str:
    """Whether the confidence numbers have earned any trust yet.

    Always rendered. A confidence score with no calibration behind it is
    decoration, and decoration on a trading decision is worse than no number,
    so the page says so until the data says otherwise.
    """
    resolved = []
    for p in load_all(plans_dir):
        if p.status in (Status.STOPPED_OUT, Status.COMPLETED) and p.confidence:
            resolved.append((p.confidence, p.status is Status.COMPLETED))
    cal = conf.calibration([c for c, _ in resolved], [h for _, h in resolved])
    cls = "uncal" if not cal.is_meaningful or (cal.brier or 1) >= 0.25 else "flash"
    extra = ""
    if cal.buckets:
        extra = "<br>" + " &middot; ".join(
            f"{b.get('label', '?')}: stated {b.get('stated', 0):.0%}, "
            f"realised {b.get('realised', 0):.0%} (n={b.get('n', 0)})"
            for b in cal.buckets if b.get("n"))
    return (f'<div class="{cls}"><b>Confidence calibration.</b> '
            f'{_esc(cal.verdict)}{extra}</div>')


def _page(*, control: Control, runner: ScanRunner, token: str,
          plans_dir: Path, fees: FeeModel, state_dir: Path = STATE_DIR,
          heartbeat_path: Path = HEARTBEAT_PATH, db=None) -> str:
    db = db if db is not None else gridstore.Store()
    payload = _state_payload(control, runner, db, plans_dir, state_dir,
                             heartbeat_path)
    marks = _marks(load_all(plans_dir), state_dir)
    paper_tot = gridstore.totals(db.transactions(mode=gridstore.PAPER, limit=0),
                                 marks=marks, mode=gridstore.PAPER)
    live_tot = gridstore.totals(db.transactions(mode=gridstore.LIVE, limit=0),
                                marks=marks, mode=gridstore.LIVE)

    state = control.state()
    plans = load_all(plans_dir)
    proposals = [p for p in plans if p.status is Status.PROPOSED]
    n_paper = 0 if state.live_trading else len(
        [p for p in plans if p.status in (Status.APPROVED, Status.RUNNING)])
    n_live = len([p for p in plans if p.status in (Status.APPROVED,
                                                   Status.RUNNING)]) - n_paper
    running = [p for p in plans
               if p.status in (Status.APPROVED, Status.RUNNING)]
    finished = [p for p in plans
                if p.status in (Status.STOPPED, Status.STOPPED_OUT)]
    scan = runner.snapshot()

    flashes = "".join(
        f'<div class="flash{" err" if cat == "error" else ""}">{_esc(m)}</div>'
        for cat, m in get_flashed_messages(with_categories=True))

    mode = ("LIVE — real money" if state.live_trading and state.trading_enabled
            else "paper")
    mode_cls = "pill live" if state.live_trading and state.trading_enabled else "pill"

    default_quote = max(0.0, state.max_total_quote * max(1, state.max_concurrent_grids))

    body = [f"""<!doctype html><html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>TradeBunny</title><style>{CSS}</style></head><body>
<header><h1>TradeBunny <span class="{mode_cls}">{_esc(mode)}</span>
<span class="count" id="unread"
  style="{'' if payload['unread'] else 'display:none'}">{payload['unread'] or ''}</span></h1>
<p id="status" class="muted">{_esc(scan['label'])}</p>
<p id="stamp" class="stamp">updated just now</p>
{_freshness_panel()}</header><main>
<div class="card">
  <p class="muted">Everything, paper and live together</p>
  <p class="total {_cls(payload['grand_total'])}" id="grand">
    {_money(payload['grand_total'])}</p>
</div>
{_mode_section(gridstore.PAPER, paper_tot, n_paper)}
{_mode_section(gridstore.LIVE, live_tot, n_live)}"""
             f"""{_heartbeat_banner(heartbeat_path, len(running))}"""
             f"""{_calibration_banner(plans_dir)}{_execution_panel()}{flashes}"""]

    # --- control -------------------------------------------------------
    body.append(f"""
<div class="card">
  <dl>
    <dt>trading</dt><dd class="{'good' if state.trading_enabled else 'bad'}">
      {'on' if state.trading_enabled else 'HALTED'}</dd>
    <dt>analyst</dt><dd class="{'good' if state.analyst_enabled else 'bad'}">
      {'on' if state.analyst_enabled else 'HALTED'}</dd>
    <dt>max concurrent grids</dt><dd>{state.max_concurrent_grids}</dd>
    <dt>max total quote</dt><dd>{state.max_total_quote:g}</dd>
    <dt>halted symbols</dt><dd>{_esc(', '.join(state.halted_symbols) or '—')}</dd>
    <dt>reason</dt><dd>{_esc(state.reason or '—')}</dd>
  </dl>
  <div class="row">
    <form method="post" action="/halt" style="flex:1">
      <input type="hidden" name="token" value="{_esc(token)}">
      <button class="danger">Halt everything</button></form>
    <form method="post" action="/pause-analyst" style="flex:1">
      <input type="hidden" name="token" value="{_esc(token)}">
      <button>Pause analyst</button></form>
  </div>
</div>""")

    # --- scan ----------------------------------------------------------
    err = (f'<div class="flash err">scan failed — {_esc(scan["error"])}</div>'
           if scan["error"] else "")
    body.append(f"""
<div class="card">{err}
  <form method="post" action="/scan">
    <input type="hidden" name="token" value="{_esc(token)}">
    <label for="aq">capital available to the scan</label>
    <input id="aq" type="number" name="available" step="any" min="0"
           value="{default_quote:.2f}">
    <button id="scanbtn" {'disabled' if scan['running'] else ''}>Run scan now</button>
  </form>
  <p class="muted">{len(universe())} symbols · 30-90 seconds · no AI, no API key</p>
  <p class="muted">permissivity <b>{_esc(analyst.permissivity())}</b>
    (confidence &ge; {conf.threshold_for(analyst.permissivity()):.2f}) —
    change it in <code>config.yaml</code></p>
  {_below_threshold_block(scan)}
  {_rejection_block(scan)}
</div>""")

    # --- proposals -----------------------------------------------------
    body.append("<hr><h1>Awaiting your decision</h1>")
    if proposals:
        body += [_proposal_card(p, token, fees, payload["live_armed"])
                 for p in proposals]
    else:
        body.append('<p class="muted">Nothing awaiting a decision. An empty '
                    'result is a correct outcome — the gates reject most days.</p>')

    # --- running -------------------------------------------------------
    body.append("<hr><h1>Running</h1>")
    if running:
        for p in running:
            card = _running_card(p, token, p.id in state.halted_plans, state_dir)
            body.append(f'<a class="card-link" href="/grid/{_esc(p.id)}'
                        f'?token={_esc(token)}">{card}</a>')
    else:
        body.append('<p class="muted">No grids running.</p>')

    if finished:
        body.append("<hr><h1>Finished</h1>")
        for p in finished:
            st = load_state(p.id, state_dir)
            body.append(
                f'<div class="card"><p class="sym">{_esc(p.symbol)}'
                f'<span class="pill">{_esc(p.status.value)}</span></p>'
                f'<p class="muted">{_esc(st.note if st else "—")}</p></div>')

    body.append("<hr><h1>Notifications</h1>")
    body.append(f"""
<div class="card">
  <form method="post" action="/notifications/read">
    <input type="hidden" name="token" value="{_esc(token)}">
    <button>Mark all read</button>
  </form>
  <p class="muted">Every event, kept forever. This is the record.</p>
  {_feed(db.events(limit=60), token)}
</div>""")

    body.append(f"</main><script>var TOKEN={_js_str(token)};{POLL_JS}</script>"
                "</body></html>")
    return "".join(body)


def _js_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("<", "\\x3c") + '"'


def _below_threshold_block(scan: dict[str, Any]) -> str:
    """What the permissivity setting is hiding, and by how much."""
    rows = scan.get("below_threshold") or []
    if not rows:
        return ""
    items = "".join(f"<li>{_esc(s)} — confidence {c:.2f}</li>" for s, c in rows)
    return (f'<details><summary>{len(rows)} candidate(s) scored below the '
            f'threshold</summary><ul class="muted">{items}</ul></details>')


def _rejection_block(scan: dict[str, Any]) -> str:
    if not scan["rejections"]:
        return ""
    items = "".join(f"<li>{_esc(r)}</li>" for r in scan["rejections"])
    return (f'<details><summary>why {len(scan["rejections"])} were rejected'
            f'</summary><ul class="muted">{items}</ul></details>')


# --------------------------------------------------------------------- app

def _marks(plans: list, state_dir: Path) -> dict[str, float]:
    """Last seen price per symbol, from the executor's own snapshots."""
    out: dict[str, float] = {}
    for p in plans:
        st = load_state(p.id, state_dir)
        if st and st.price > 0:
            out[p.symbol] = st.price
    return out


def _state_payload(control, runner, db, plans_dir: Path, state_dir: Path,
                   heartbeat_path: Path) -> dict[str, Any]:
    """Everything the page shows, as data. The HTML renders from this and the
    poller re-renders from the same shape, so the two cannot disagree."""
    plans = load_all(plans_dir)
    running = [p for p in plans if p.status in (Status.APPROVED, Status.RUNNING)]
    proposals = [p for p in plans if p.status is Status.PROPOSED]
    marks = _marks(plans, state_dir)
    beat = load_heartbeat(heartbeat_path)
    cstate = control.state()

    sections = {}
    for mode in (gridstore.PAPER, gridstore.LIVE):
        txs = db.transactions(mode=mode, limit=0)
        sections[mode] = gridstore.totals(txs, marks=marks, mode=mode)

    cards = []
    for p in running:
        st = load_state(p.id, state_dir)
        txs = db.transactions(p.id, limit=0)
        tot = gridstore.totals(txs, marks=marks)
        price = st.price if st else None
        pos = None
        if price and p.upper > p.lower:
            pos = max(0.0, min(1.0, (price - p.lower) / (p.upper - p.lower)))
        cards.append({
            "id": p.id, "symbol": p.symbol, "status": p.status.value,
            "lower": p.lower, "upper": p.upper, "price": price,
            "position_in_range": pos,
            "mode": gridstore.LIVE if cstate.live_trading else gridstore.PAPER,
            "pnl": tot.total, "cycles": len([t for t in txs if t.side == "sell"]),
            "committed": p.max_quote_total,
            "halted": p.id in cstate.halted_plans,
        })

    return {
        "updated_at": _now(),
        "grand_total": sum(s.total for s in sections.values()),
        "sections": {m: {
            "total": t.total, "invested": t.invested,
            "current_value": t.current_value, "realised": t.realised,
            "unrealised": t.unrealised, "fees": t.fees,
            "n_transactions": t.n_transactions,
            "buy_and_hold": t.buy_and_hold,
        } for m, t in sections.items()},
        "grids": cards,
        "proposals": [{"id": p.id, "symbol": p.symbol,
                       "confidence": p.confidence} for p in proposals],
        "scan": runner.snapshot(),
        "executor": {
            "down": beat is None or beat.is_down(),
            "age": None if beat is None else beat.age_seconds(),
            "passes": 0 if beat is None else beat.passes,
        },
        "unread": db.unread_count(),
        "live_armed": bool(cstate.live_trading and cstate.trading_enabled),
    }


def _grid_page(plan: GridPlan, db, token: str, state_dir: Path,
               size: float, fees: FeeModel) -> str:
    """The bank-statement view of one grid."""
    st = load_state(plan.id, state_dir)
    price = st.price if st else None
    txs = db.transactions(plan.id, limit=0)
    mode = txs[0].mode if txs else gridstore.PAPER

    # Hypothetical capital: scale the SAME fills. Only offered on paper, where
    # rerunning history at a different size is a thought experiment rather than
    # a claim about money that moved.
    scale = 1.0
    selector = ""
    blocked = ""
    if mode == gridstore.PAPER and plan.max_quote_total > 0:
        chosen = size if size in HYPOTHETICAL_SIZES else plan.max_quote_total
        scale = chosen / plan.max_quote_total
        selector = _size_selector(chosen, plan.id, token)
        per_level = chosen / max(1, len(plan.grid_prices))
        if plan.min_viable_capital and chosen < plan.min_viable_capital:
            blocked = (
                f'<p class="blocked">At {chosen:,.0f} the exchange minimum '
                f'would have blocked this ladder entirely — it needs '
                f'{plan.min_viable_capital:,.2f}. Every figure below is what '
                f'the fills WOULD have produced, not what could have been '
                f'placed.</p>')
        elif per_level < analyst.MIN_QUOTE_PER_LEVEL:
            blocked = (
                f'<p class="blocked">At {chosen:,.0f} each rung is '
                f'{per_level:,.2f}, under the {analyst.MIN_QUOTE_PER_LEVEL:g} '
                f'minimum order — those levels would have been rejected.</p>')

    tot = gridstore.totals(txs, marks={plan.symbol: price} if price else None,
                           scale=scale)
    held = set()
    if st and st.occupied_levels:
        held = set(st.occupied_levels)
    label = "PAPER — not real money" if mode == gridstore.PAPER else \
        "LIVE — REAL MONEY"

    return f"""<!doctype html><html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{_esc(plan.symbol)} · TradeBunny</title><style>{CSS}</style></head><body>
<header>
  <h1><a class="card-link" href="/?token={_esc(token)}">&larr;</a>
    {_esc(plan.symbol)}</h1>
  <span class="modebar {mode}">{label}</span>
</header><main>
<section class="mode {mode}">
  <h2>{label}</h2>
  <p class="total {_cls(tot.total)}">{_money(tot.total)}</p>
  <dl class="figs">
    <dt>range</dt><dd>{plan.lower:.6g} – {plan.upper:.6g}</dd>
    <dt>levels</dt><dd>{plan.levels} · {plan.min_spacing_pct:.2%} apart</dd>
    <dt>committed</dt><dd>{plan.max_quote_total * scale:,.2f}</dd>
    <dt>stop</dt><dd>{plan.stop_below:.6g}</dd>
    <dt>realised</dt><dd class="{_cls(tot.realised)}">{_money(tot.realised)}</dd>
    <dt>unrealised</dt>
      <dd class="{_cls(tot.unrealised)}">{_money(tot.unrealised)}</dd>
    <dt>fees</dt><dd>{_money(-abs(tot.fees))}</dd>
    <dt>position</dt><dd>{tot.position:,.6g}</dd>
    <dt>buy &amp; hold</dt>
      <dd class="{_cls(tot.buy_and_hold)}">{_money(tot.buy_and_hold)}</dd>
  </dl>
  {selector}{blocked}
</section>

<div class="card">
  <p class="sym">The ladder</p>
  {_ladder(plan, price, held)}
</div>

<div class="card">
  <p class="sym">Every transaction</p>
  <p class="muted">{len(txs)} transaction(s), newest first.</p>
  {_statement(txs, scale)}
</div>
</main></body></html>"""


def create_app(control: Optional[Control] = None, *, source=okx, client=None,
               plans_dir: Optional[Path] = None, token: Optional[str] = None,
               fees: Optional[FeeModel] = None,
               state_dir: Optional[Path] = None,
               heartbeat_path: Optional[Path] = None,
               notify_fn=None, store=None, db_path: Optional[Path] = None) -> Flask:
    """Build the app.

    Everything the app touches is injectable so the tests can drive the real
    request handling with no network, no threads left running and no writes
    outside a temporary directory.
    """
    token = token or os.getenv("GRID_WEB_TOKEN", "")
    if not token:
        raise RuntimeError(
            "GRID_WEB_TOKEN is not set. This app binds to 0.0.0.0 so a phone on "
            "the same wifi can reach it, and it can halt trading and commit "
            "capital — it will not start unguarded. Generate one with: "
            "python -c \"import secrets; print(secrets.token_urlsafe(24))\"")

    control = control or Control()
    plans_dir = Path(plans_dir) if plans_dir else PLANS_DIR
    state_dir = Path(state_dir) if state_dir else STATE_DIR
    heartbeat_path = Path(heartbeat_path) if heartbeat_path else HEARTBEAT_PATH
    fees = fees or FeeModel()
    runner = ScanRunner(control, source=source, client=client,
                        plans_dir=plans_dir, notify=notify_fn)
    # THE WEB IS A WINDOW, NOT AN ACTOR. It reads and writes this store and
    # the plan files; it has no exchange client and no path to one. See
    # test_web.py::TestTheWebCannotTrade.
    db = store if store is not None else gridstore.Store(
        db_path or gridstore.DB_PATH)

    # A missing control file halts everything by design. That is right for the
    # executor, which must never invent permission to trade — but this is the
    # interface whose job is to create it, so it does, deliberately and once.
    control.initialise()
    plans_dir.mkdir(parents=True, exist_ok=True)

    app = Flask(__name__)
    app.secret_key = secrets.token_hex(16)
    app.config.update(CONTROL=control, RUNNER=runner, PLANS_DIR=plans_dir,
                      TOKEN=token, FEES=fees, STATE_DIR=state_dir,
                      HEARTBEAT=heartbeat_path, STORE=db)

    def _authorised() -> bool:
        supplied = (request.form.get("token") or request.args.get("token")
                    or request.headers.get("X-Grid-Token")
                    or request.cookies.get("grid_token") or "")
        return hmac.compare_digest(supplied, token)

    @app.before_request
    def _guard():
        if request.endpoint == "static":
            return None
        # A POST must carry the token explicitly, not merely inherit the cookie.
        # Otherwise any page you visit could make your browser halt your bot or
        # approve a plan; the attacker cannot read the token, only send along a
        # cookie the browser attaches for them.
        if request.method == "POST":
            supplied = (request.form.get("token")
                        or request.headers.get("X-Grid-Token") or "")
            if not hmac.compare_digest(supplied, token):
                abort(403)
            return None
        if not _authorised():
            abort(403)
        return None

    @app.after_request
    def _remember(resp: Response) -> Response:
        # Set on ANY request that carried the token explicitly, including a POST
        # whose token came from the form. Only honouring `?token=` meant the
        # redirect after a POST could land on a `/` with no cookie and 403 —
        # the button would appear to do nothing.
        supplied = request.args.get("token") or request.form.get("token")
        if supplied and hmac.compare_digest(supplied, token) and resp.status_code < 400:
            resp.set_cookie("grid_token", token, httponly=True, samesite="Strict")
        return resp

    # ------------------------------------------------------------ routes

    @app.get("/")
    def index() -> str:
        return _page(control=control, runner=runner, token=token,
                     plans_dir=plans_dir, fees=fees, state_dir=state_dir,
                     heartbeat_path=heartbeat_path, db=db)

    @app.post("/scan")
    def start_scan():
        try:
            available = float(request.form.get("available", "0") or 0)
        except ValueError:
            available = 0.0
        if available <= 0:
            flash("enter the capital available to the scan", "error")
            return redirect(url_for("index"))

        allowed, why = control.may_propose()
        if not allowed:
            flash(f"analyst is halted: {why}", "error")
            return redirect(url_for("index"))

        started, msg = runner.start(universe(), available)
        flash(msg if started else f"not started — {msg}",
              "message" if started else "error")
        return redirect(url_for("index"))

    @app.get("/scan/status")
    def scan_status():
        return runner.snapshot()

    @app.post("/decide/<plan_id>")
    def decide(plan_id: str):
        plan = _find(plans_dir, plan_id)
        if plan is None:
            abort(404)
        action = request.form.get("action", "")

        if action == "reject":
            plan.reject()
            plan.save(plans_dir)
            db.record_event(gridstore.PROPOSAL, f"{plan.symbol} rejected",
                            symbol=plan.symbol, plan_id=plan.id)
            flash(f"{plan.symbol} rejected")
            return redirect(url_for("index"))

        if action not in ("approve", "paper", "live"):
            abort(400)

        try:
            amount = float(request.form.get("amount", ""))
        except ValueError:
            flash("amount must be a number", "error")
            return redirect(url_for("index"))

        problems = _resize(plan, amount, control, fees)
        if problems:
            # Re-validated because the owner's amount is free text: it can make
            # a sound plan unfundable, or exceed the cap in control.json. An
            # approval that cannot execute is worse than a refusal.
            flash(f"{plan.symbol} not approved — " + "; ".join(problems), "error")
            return redirect(url_for("index"))

        if action == "live" and not control.is_live():
            # The button is disabled in the UI; this is the server-side half of
            # the same rule, because a disabled button is a suggestion.
            flash("live trading is not armed — control.json says paper. "
                  "set_live(True, confirm='I ACCEPT THE RISK') first.", "error")
            return redirect(url_for("index"))

        plan.approve()
        plan.save(plans_dir)
        db.record_event(gridstore.PROPOSAL,
                        f"{plan.symbol} approved at {plan.max_quote_total:.2f}",
                        body=f"mode: {'live' if action == 'live' else 'paper'}",
                        symbol=plan.symbol, plan_id=plan.id)
        flash(f"{plan.symbol} approved at {plan.max_quote_total:.2f}")
        return redirect(url_for("index"))

    @app.post("/stop/<plan_id>")
    def stop(plan_id: str):
        plan = _find(plans_dir, plan_id)
        if plan is None:
            abort(404)
        control.halt_plan(plan_id, reason=f"stopped {plan.symbol} from the web UI")
        if plan.is_live:
            plan.stop(Status.STOPPED)
            plan.save(plans_dir)
        flash(f"{plan.symbol} stopped")
        return redirect(url_for("index"))

    @app.get("/api/state")
    def api_state():
        """Everything the page renders, as JSON, polled every 3 seconds."""
        return _state_payload(control, runner, db, plans_dir, state_dir,
                              heartbeat_path)

    @app.get("/grid/<plan_id>")
    def grid_detail(plan_id: str):
        plan = _find(plans_dir, plan_id)
        if plan is None:
            abort(404)
        try:
            size = float(request.args.get("size", "0") or 0)
        except ValueError:
            size = 0.0
        return _grid_page(plan, db, token, state_dir, size, fees)

    @app.post("/notifications/read")
    def mark_read():
        raw = request.form.get("id")
        n = db.mark_read(int(raw) if raw else None)
        flash(f"marked {n} notification(s) read")
        return redirect(url_for("index"))

    @app.post("/halt")
    def halt():
        control.halt_all("halted from the web UI")
        flash("everything halted. The executor cancels orders on its next loop; "
              "positions are NOT liquidated.", "error")
        return redirect(url_for("index"))

    @app.post("/pause-analyst")
    def pause_analyst():
        control.halt_analyst("analyst paused from the web UI")
        flash("analyst paused. Running grids continue.")
        return redirect(url_for("index"))

    return app


def _find(plans_dir: Path, plan_id: str) -> Optional[GridPlan]:
    for p in load_all(plans_dir):
        if p.id == plan_id:
            return p
    return None


def _resize(plan: GridPlan, amount: float, control: Control,
            fees: FeeModel) -> list[str]:
    """Apply the owner's chosen amount. Returns problems; empty means fine."""
    if amount <= 0:
        return ["amount must be positive"]

    state = control.state()
    if amount > state.max_total_quote:
        return [f"{amount:g} exceeds the {state.max_total_quote:g} cap in "
                f"control.json — raise the cap deliberately if you mean it"]

    lines = len(plan.grid_prices)
    per_level = amount / lines
    if per_level < analyst.MIN_QUOTE_PER_LEVEL:
        return [f"{amount:g} over {lines} levels is {per_level:.2f} each, under "
                f"the {analyst.MIN_QUOTE_PER_LEVEL:g} minimum order"]

    plan.quote_per_level = per_level
    plan.max_quote_total = per_level * lines      # exact, so validate() agrees
    return plan.validate(fees=fees)


def main() -> None:
    port = int(os.getenv("GRID_WEB_PORT", "8080"))
    app = create_app()
    print(f"TradeBunny on http://0.0.0.0:{port}/?token="
          f"{app.config['TOKEN']}", flush=True)
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
