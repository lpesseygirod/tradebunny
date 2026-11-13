"""TradeBunny's single entry point.

    python -m grid.cli init          create the control file
    python -m grid.cli scan          ask the analyst, once, now
    python -m grid.cli run           the executor loop  (this must stay up)
    python -m grid.cli supervise     the executor loop, restarted if it dies
    python -m grid.cli web           the approval interface
    python -m grid.cli status        what is running, what is halted
    python -m grid.cli halt          stop everything
    python -m grid.cli resume        clear halts (never re-arms live trading)
    python -m grid.cli backtest SYM  replay a fitted grid over recent candles
    python -m grid.cli check         connectivity, used by `make check`

TWO PROCESSES, DIFFERENT LIFETIMES. Most of this file follows from that:

  the analyst   runs ONLY when I ask: the web button or `cli scan`. There is
                no cron entry for it, on purpose. A scanner on a timer produces
                a candidate every day whether or not the day deserved one, and
                the pressure to approve something is the failure this whole
                design is built to avoid. An empty day should cost nothing.

  the executor  runs CONTINUOUSLY, and must. `plan.stop_below` is enforced by
                `run_once` and by nothing else — the exchange holds the resting
                limit orders but has never heard of my stop. A stopped
                executor is therefore not "paused", it is every open grid
                running with no stop-loss, which is why it is supervised, why
                it writes a heartbeat, and why the web page shouts when that
                heartbeat goes stale.

SIGTERM DOES NOT CANCEL ORDERS. Stopping the process is not the same as closing
the position, exactly as `control.halt_all` does not liquidate. The resting
ladder is left where it is so a restart picks it up unchanged. What is lost
while it is down is the stop — hence the warning, not a fire sale.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import analyst, backtest as bt, drift, executor, freshness, metrics, okx, web
from . import confidence as conf
from . import execution, service
from . import store as gridstore
from .analyst import ScanReport
from .control import Control
from .executor import (HEARTBEAT_PATH, STATE_DIR, Heartbeat, PaperExchange,
                       load_heartbeat, make_exchange, run_once)
from .fees import FeeModel
from .plan import GridPlan, Status, load_all

log = logging.getLogger("grid.cli")

HOME = Path(os.getenv("TRADEBUNNY_HOME", ".")).resolve()
PLANS_DIR = HOME / "data" / "plans"
PAPER_PATH = HOME / "data" / "paper.json"

INTERVAL = 60.0
#: How often the regime-drift check runs inside the executor loop.
DRIFT_INTERVAL = 86_400.0
RESTART_DELAY = 5.0
MAX_RESTART_DELAY = 300.0


def notify(message: str) -> None:
    """Telegram, best effort. A failed notification never changes behaviour."""
    try:
        from ledger import notify as _n
        _n.send(message)
    except Exception as e:
        log.warning("could not notify (%s: %s). Message was: %s",
                    type(e).__name__, e, message)


# --------------------------------------------------------------- the run loop

def live_plans(plans_dir: Path = PLANS_DIR) -> list[GridPlan]:
    """Plans the executor should be applying: approved or already running."""
    return [p for p in load_all(plans_dir)
            if p.status in (Status.APPROVED, Status.RUNNING)]


def okx_last_prices() -> dict[str, float]:
    """Last traded price of every OKX spot pair, from public market data.

    The only thing paper trading takes from OKX. No order, balance or fill is
    ever sent there: the live order client (`executor.LiveExchange`) is not
    built.
    """
    return {sym: t["last"] for sym, t in okx.get_spot_tickers().items()}


def check_drift(control: Control, *, plans_dir: Path = PLANS_DIR,
                source=okx, bar: str = "1H",
                notify: Callable[[str], None] = notify) -> list[Any]:
    """Re-run the evidence on every live grid, using only post-approval bars.

    Notifies on decay. Does NOT stop anything — see drift.py.
    """
    reports = []
    for plan in live_plans(plans_dir):
        try:
            candles = source.get_candles(plan.symbol, bar, limit=300)
        except Exception as e:
            log.warning("drift check could not fetch %s: %s: %s",
                        plan.symbol, type(e).__name__, e)
            continue
        recent = drift.bars_since(candles, plan.decided_at)
        rep = drift.check_plan(
            plan, [c["close"] for c in recent], [c["high"] for c in recent],
            [c["low"] for c in recent])
        reports.append(rep)
        if rep.decayed:
            log.warning("%s: thesis decayed\n%s", plan.symbol, rep.explain())
            notify(rep.message())
        elif not rep.skipped:
            log.info("%s: thesis still holds", plan.symbol)
    return reports


def run_loop(control: Control, *, exchange=None, plans_dir: Path = PLANS_DIR,
             state_dir: Path = STATE_DIR, heartbeat_path: Path = HEARTBEAT_PATH,
             interval: float = INTERVAL, max_passes: Optional[int] = None,
             sleep: Callable[[float], None] = time.sleep,
             notify: Callable[[str], None] = notify,
             drift_interval: float = DRIFT_INTERVAL,
             drift_source=okx,
             paper_prices: Optional[Callable[[], dict[str, float]]] = None,
             store=None) -> Heartbeat:
    """Apply every live plan, every `interval` seconds, until told to stop.

    Returns the final heartbeat. `max_passes` and `sleep` exist so the tests can
    drive the real loop without waiting a minute per pass.

    Exceptions from one plan do not stop the others, and do not stop the loop:
    a transient exchange error on one symbol must not take the stop-loss away
    from every other grid. Anything that escapes the whole pass is recorded on
    the heartbeat and re-raised, so the supervisor can restart and tell me.

    Paper trading: with no `exchange` given, the loop builds the paper exchange
    itself and feeds it real OKX prices every pass (`sync_paper_market`), and
    records fills in the app's database. A caller that passes its own exchange
    (the tests) passes `paper_prices` and `store` explicitly if it wants them.
    """
    if exchange is None:
        exchange = make_exchange(control, PAPER_PATH)
        if isinstance(exchange, PaperExchange):
            if paper_prices is None:
                paper_prices = okx_last_prices
            if store is None:
                store = gridstore.Store()
    beat = Heartbeat(pid=os.getpid(),
                     started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    stopping = {"now": False}

    def _stop(signum, _frame):
        # No order cancellation here. See the module docstring.
        log.info("signal %s received — finishing this pass and exiting; "
                 "resting orders are left in place", signum)
        stopping["now"] = True

    installed = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
            installed.append(sig)
        except (ValueError, OSError):
            pass          # not the main thread (a test, or an embedded run)

    passes = 0
    was_held: set[str] = set()
    last_drift = None   # None = not run yet. Never compare against 0: time.monotonic()
                        # counts from boot, so a freshly started machine would skip the first check.
    try:
        while not stopping["now"]:
            plans = live_plans(plans_dir)

            # Daily: has the thesis behind any running grid decayed? The stop
            # only catches a fast break; this is the check for a slow one.
            if drift_interval > 0 and (last_drift is None or
                                       time.monotonic() - last_drift
                                       >= drift_interval):
                last_drift = time.monotonic()
                try:
                    check_drift(control, plans_dir=plans_dir,
                                source=drift_source, notify=notify)
                except Exception as e:
                    log.exception("drift check failed")
                    beat.last_error = f"drift: {type(e).__name__}: {e}"

            beat.plans = [p.symbol for p in plans]

            # The caps in control.json, checked every pass because the file
            # can change between passes. A newly held grid is announced once.
            held = executor.admit(plans, control)
            for plan in plans:
                if plan.id in held and plan.id not in was_held:
                    notify(f"⏸ {plan.symbol}: {held[plan.id]}")
            was_held = set(held)

            no_price: dict[str, str] = {}
            if paper_prices is not None and isinstance(exchange, PaperExchange):
                no_price = executor.sync_paper_market(
                    exchange, plans, paper_prices, store=store, held=held)

            for plan in plans:
                try:
                    if plan.symbol in no_price:
                        raise RuntimeError(no_price[plan.symbol])
                    run_once(plan, exchange, control, state_dir=state_dir,
                             plans_dir=plans_dir, notify=notify, store=store,
                             held=held.get(plan.id, ""))
                except Exception as e:
                    beat.last_error = f"{plan.symbol}: {type(e).__name__}: {e}"
                    log.exception("pass failed for %s", plan.symbol)
                    notify(f"⚠️ {plan.symbol}: executor pass failed — "
                           f"{type(e).__name__}: {e}")
            # launchd does not rotate its own logs; copy-and-truncate from
            # here, where something ticks regularly. Cheap: one stat per file.
            try:
                service.rotate_logs()
            except Exception:
                pass

            passes += 1
            beat.passes = passes
            beat.save(heartbeat_path)

            if max_passes is not None and passes >= max_passes:
                break
            if stopping["now"]:
                break
            # Sleep in slices so SIGTERM is answered in about a second rather
            # than up to a minute later.
            waited = 0.0
            while waited < interval and not stopping["now"]:
                step = min(1.0, interval - waited)
                sleep(step)
                waited += step
    finally:
        for sig in installed:
            try:
                signal.signal(sig, signal.SIG_DFL)
            except (ValueError, OSError):
                pass
        beat.stopped_cleanly = stopping["now"]
        beat.save(heartbeat_path)

    return beat


def supervise(argv: Sequence[str], *, notify: Callable[[str], None] = notify,
              max_restarts: Optional[int] = None,
              spawn=subprocess.Popen,
              sleep: Callable[[float], None] = time.sleep) -> int:
    """Keep `run` alive, restarting it with backoff and telling me when it dies.

    A supervisor rather than a `try/except` inside the loop, because the
    failures that matter here are the ones that kill the process: an OOM, a
    segfault in a C dependency, an unhandled exception during import. A crash
    with nothing watching means every grid is running without its stop.

    A clean exit (code 0, i.e. SIGTERM handled by the loop) is NOT restarted —
    that is me stopping it on purpose.
    """
    delay = RESTART_DELAY
    restarts = 0
    while True:
        proc = spawn(list(argv))
        code = proc.wait()
        if code == 0:
            log.info("executor exited cleanly; not restarting")
            return 0

        restarts += 1
        notify(f"🔴 TradeBunny executor died (exit {code}). "
               f"Restarting in {delay:.0f}s. While it is down your grids have "
               f"no stop-loss.")
        log.error("executor exited %s; restart %d in %.0fs", code, restarts, delay)
        if max_restarts is not None and restarts >= max_restarts:
            return code
        sleep(delay)
        delay = min(MAX_RESTART_DELAY, delay * 2)   # back off, do not hammer


# ------------------------------------------------------------------ commands

def cmd_init(args) -> int:
    control = Control(args.control)
    state = control.initialise()
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"control file: {control.path}")
    print(f"plans:        {PLANS_DIR}")
    print(control.describe())
    if not state.live_trading:
        print("\nPaper trading. `control.set_live(True, confirm=...)` to change "
              "that, deliberately.")
    return 0


#: The statistics window the analyst is CONFIGURED to use, in bars. Anything
#: much under this and the scan is measuring less history than it asked for —
#: a delisted pair, a short listing, a truncated cache.
#:
#: This tracks `analyst.STATISTICS_DAYS` rather than being a separate constant.
#: It was previously hardcoded to 17_000 (about two years) from when `scan`
#: fetched a single 300-bar page and any deep window was a bonus. Left at that
#: value it now fires on every correct run, and a warning that always fires is
#: one nobody reads. It is NOT a gate — it does not reject anything — and the
#: threshold below (90%) is unchanged.
EXPECTED_BARS = int(analyst.STATISTICS_DAYS * 24)


def window_warning(n_bars: int, bar: str, expected: int = EXPECTED_BARS) -> str:
    """Say loudly when the data window is not what it should be.

    Every result produced so far may have been measuring the same two trending
    months. The only way to know is to print the window with the result, every
    time, and complain when it is short.
    """
    if bar != "1H" or n_bars <= 0:
        # Nothing was scanned — that is a halt or an abort, and the freshness
        # of a window that does not exist is not the story.
        return ""
    if n_bars >= expected * 0.9:
        return ""
    days = n_bars / 24.0
    return (f"\n*** SHORT DATA WINDOW ***\n"
            f"    {n_bars} bars is about {days:.0f} days. Roughly {expected} "
            f"({expected / 24 / 365:.1f} years) was expected.\n"
            f"    `analyst.scan` and `cli backtest --deep` both page back "
            f"through get_history.\n"
            f"    Every statistic below is measuring that window and nothing "
            f"else. If it covers one regime,\n"
            f"    the result describes that regime, however confident the "
            f"numbers look.\n")


def cmd_scan(args) -> int:
    """The analyst, once, now. Never on a timer — see the module docstring."""
    control = Control(args.control)
    report = ScanReport()
    plans = analyst.scan(web.universe(), args.available, args.bar,
                         control=control, report=report)
    for p in plans:
        p.save(PLANS_DIR)
    print(report.explain())

    if report.mode == "calibration":
        print()
        print(execution.load().explain())

    floor = conf.threshold_for(report.level or conf.DEFAULT_LEVEL)
    print()
    # BOTH windows, always. They are different lengths on purpose, and a
    # reader who sees only one will read the band as a year-long claim when it
    # is a six-week one.
    print(f"statistics window: {report.window_bars} bars, "
          f"{report.window_from or '?'} to {report.window_to or '?'}")
    print(f"range window:      {report.range_window_bars} bars, "
          f"{report.range_window_from or '?'} to "
          f"{report.range_window_to or '?'}")
    warn = window_warning(report.window_bars, args.bar)
    if warn:
        print(warn)

    if args.show_all:
        print()
        print(report.show_all(floor))

    out = Path(args.results or "scan_results.md")
    out.write_text(_scan_markdown(report, floor, args))
    print(f"\nwrote {out}")
    if report.error:
        notify(f"⚠️ TradeBunny scan failed: {report.error}")
        return 1
    if plans:
        lines = "\n".join(
            f"• {p.symbol}  {p.lower:.6g}–{p.upper:.6g}  {p.levels} levels  "
            f"stop {p.stop_below:.6g}" for p in plans)
        print("\nAwaiting your approval in the web UI:")
        print(lines)
        notify(f"📋 TradeBunny: {len(plans)} proposal(s) awaiting approval\n\n"
               f"{lines}\n\nApprove or reject in the web UI.")
    return 0


def _scan_markdown(report, floor: float, args) -> str:
    """Every candidate and its score, written every run.

    Zero proposals with a best score of 0.48 and zero with a best of 0.05 are
    different results. This file is what tells them apart afterwards.
    """
    rows = sorted(report.candidates, key=lambda c: -c.confidence)
    lines = [
        "# Scan results",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
        f"· bar {args.bar} · capital {args.available:,.2f}",
        "",
        f"- **{len(rows)}** candidates evaluated, **{len(report.accepted)}** "
        f"proposed.",
        f"- Best confidence **{report.best_score():.2f}** against a "
        f"{report.level or 'strict'} threshold of **{floor:.2f}**.",
        f"- Statistics window (character: ADF, Hurst, ADX, efficiency): "
        f"**{report.window_bars} bars**, "
        f"{report.window_from or '?'} to {report.window_to or '?'}.",
        f"- Range window (the band, containment, touches, ATR): "
        f"**{report.range_window_bars} bars**, "
        f"{report.range_window_from or '?'} to "
        f"{report.range_window_to or '?'}.",
        f"- Fees: {report.fee_source or 'unknown'}",
    ]
    warn = window_warning(report.window_bars, args.bar)
    if warn:
        lines += ["", "> **SHORT DATA WINDOW.** " + " ".join(warn.split())]
    lines += [
        "",
        "| Pair | confidence | outcome | missed by | stats window | "
        "range window | reason |",
        "|---|---:|---|---:|---|---|---|",
    ]
    for c in rows:
        outcome = ("**proposed**" if c.proposed else
                   f"rejected: {c.stage}" if c.stage else "below threshold")
        miss = f"{c.missed_by:.2f}" if c.missed_by else ""
        reason = (c.reason or "").replace("|", "/")[:160]
        stats_w = (f"{c.n_bars}b {c.first_bar[:10]}..{c.last_bar[:10]}"
                   if c.n_bars else "-")
        range_w = (f"{c.n_bars_range}b {c.range_from[:10]}..{c.range_to[:10]}"
                   if c.n_bars_range else "-")
        lines.append(f"| `{c.symbol}` | {c.confidence:.2f} | {outcome} | "
                     f"{miss} | {stats_w} | {range_w} | {reason} |")
    lines += [
        "",
        "Confidence for a gate-rejected candidate omits the walk-forward "
        "component, which was never run — those scores are a lower bound.",
        "",
        "The two windows are deliberately different lengths. The statistics "
        "window asks whether this asset is the KIND of thing a grid works on; "
        "the range window asks where to put the band. A band drawn from the "
        "long window spans every regime in it and is untradeable — see "
        "`backtest_results.md`.",
    ]
    return "\n".join(lines) + "\n"


def cmd_run(args) -> int:
    control = Control(args.control)
    if not control.path.exists():
        print("no control file — run `init` first", file=sys.stderr)
        return 2
    print(f"executor starting (pid {os.getpid()}), every {args.interval:g}s. "
          f"SIGTERM exits without cancelling orders.")
    beat = run_loop(control, interval=args.interval, max_passes=args.max_passes)
    print(f"executor stopped after {beat.passes} pass(es)"
          f"{' cleanly' if beat.stopped_cleanly else ''}.")
    return 0


def cmd_supervise(args) -> int:
    inner = [sys.executable, "-m", "grid.cli", "run",
             "--control", str(args.control), "--interval", str(args.interval)]
    print("supervising:", " ".join(inner))
    return supervise(inner, max_restarts=args.max_restarts)


def cmd_web(args) -> int:
    token = os.getenv("GRID_WEB_TOKEN", "")
    if not token:
        print("GRID_WEB_TOKEN is not set. Generate one with:\n"
              "  python -c \"import secrets; print(secrets.token_urlsafe(24))\"",
              file=sys.stderr)
        return 2
    app = web.create_app(Control(args.control))
    print(f"http://0.0.0.0:{args.port}/?token={token}")
    app.run(host="0.0.0.0", port=args.port, threaded=True)
    return 0


def cmd_status(args) -> int:
    control = Control(args.control)
    print(control.describe())

    beat = load_heartbeat(args.heartbeat)
    print("\nexecutor")
    if beat is None:
        print("  NOT RUNNING — no heartbeat file. Your grids have no stop-loss.")
    elif beat.stopped_cleanly:
        print(f"  STOPPED — exited cleanly {beat.age_seconds():.0f}s ago after "
              f"{beat.passes} passes. Your grids have no stop-loss.")
    elif beat.is_stale():
        print(f"  STALE — last beat {beat.age_seconds():.0f}s ago (pid {beat.pid}). "
              f"Your grids have no stop-loss.")
    else:
        print(f"  alive — pid {beat.pid}, {beat.passes} passes, "
              f"last beat {beat.age_seconds():.0f}s ago")
    if beat and beat.last_error:
        print(f"  last error: {beat.last_error}")

    plans = load_all(PLANS_DIR)
    if not plans:
        print("\nno plans")
        return 0
    print("\nplans")
    for p in sorted(plans, key=lambda x: x.status.value):
        st = executor.load_state(p.id, args.state_dir)
        extra = ""
        if st:
            extra = (f"  {st.open_buys}b/{st.open_sells}s resting, "
                     f"{st.units_held:.6g} units")
        print(f"  {p.status.value:<12} {p.symbol:<12} {p.id}{extra}")
    return 0


def cmd_halt(args) -> int:
    control = Control(args.control)
    control.halt_all(args.reason)
    print(control.describe())
    print("\nPositions are NOT liquidated — stopping is not selling.")
    return 0


def cmd_resume(args) -> int:
    control = Control(args.control)
    control.resume_all()
    print(control.describe())
    print("\nLive trading is NOT re-armed by resume; that stays deliberate.")
    return 0


def cmd_backtest(args) -> int:
    """Fit a grid on recent candles and replay it, in-sample and out."""
    if args.candles:
        candles = json.loads(Path(args.candles).read_text())["candles"]
        if not args.deep:
            candles = candles[-300:]
    elif args.deep:
        import time as _t
        start = _t.time() * 1000 - args.years * 365 * 24 * 3600 * 1000
        candles = okx.get_history(args.symbol, args.bar, start_ts=start,
                                  allow_gaps=True)
    else:
        candles = okx.get_candles(args.symbol, args.bar, limit=300)
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    print(f"data window: {len(closes)} bars, "
          f"{freshness.iso(candles[0]['ts'] / 1000.0)} to "
          f"{freshness.iso(candles[-1]['ts'] / 1000.0)}")
    warn = window_warning(len(closes), args.bar)
    if warn:
        print(warn)

    wf = bt.walk_forward(closes, highs, lows, args.symbol, split=args.split,
                         bars_per_day=okx.bars_per_day(args.bar),
                         timestamps=[c["ts"] for c in candles])
    print(wf.explain())
    print()
    print(f"plan fitted on the first {wf.split_at} bars:")
    print(f"  windows {wf.windows()}")
    print(f"  range   {wf.plan.lower:.6g} – {wf.plan.upper:.6g}")
    print(f"  cycles  {wf.out_of_sample.n_cycles} out of sample"
          + ("   <-- a band no grid can trade shows up here as near-zero"
             if wf.out_of_sample.n_cycles < 5 else ""))
    print(f"  levels  {wf.plan.levels}, narrowest spacing "
          f"{wf.plan.min_spacing_pct:.3%}")
    print(f"  fees    {FeeModel().fee_share(wf.plan.min_spacing_pct):.1%} of gross")
    print(f"  stop    {wf.plan.stop_below:.6g}")
    print()
    print("in-sample :", wf.in_sample.explain().replace("\n", "\n             "))
    print("out-sample:", wf.out_of_sample.explain().replace("\n", "\n             "))
    if not wf.generalises:
        print("\nThis would NOT be proposed. " + wf.reason)
    return 0 if wf.generalises else 1


def cmd_grid(args) -> int:
    """The same bank-statement view the web shows, in the terminal."""
    plan = None
    for p in load_all(PLANS_DIR):
        if p.id == args.plan_id or p.symbol == args.plan_id:
            plan = p
            break
    if plan is None:
        print(f"no plan matching {args.plan_id!r}", file=sys.stderr)
        return 2

    db = gridstore.Store(args.db) if args.db else gridstore.Store()
    txs = db.transactions(plan.id, limit=0)
    mode = txs[0].mode if txs else gridstore.PAPER
    st = executor.load_state(plan.id, STATE_DIR)
    price = st.price if st else None
    tot = gridstore.totals(txs, marks={plan.symbol: price} if price else None)

    banner = ("PAPER — not real money" if mode == gridstore.PAPER
              else "LIVE — REAL MONEY")
    print("=" * 66)
    print(f"  {banner}")
    print("=" * 66)
    print(f"{plan.symbol}  ({plan.id})   status {plan.status.value}")
    print(f"  range      {plan.lower:.6g} – {plan.upper:.6g}")
    print(f"  levels     {plan.levels} · {plan.min_spacing_pct:.2%} apart")
    print(f"  committed  {plan.max_quote_total:,.2f}")
    print(f"  stop       {plan.stop_below:.6g}")
    if price:
        print(f"  price now  {price:.6g}")
    print()
    print("  ladder (# held, @ price here, x stop)")
    held = set(st.occupied_levels) if st and st.occupied_levels else set()
    prices = plan.grid_prices
    for i in range(len(prices) - 1, -1, -1):
        mark = "#" if i in held else "-"
        if price is not None and i > 0 and prices[i - 1] <= price <= prices[i]:
            mark = "@"
        print(f"    {mark} {prices[i]:>14.6g}")
    print(f"    x {plan.stop_below:>14.6g}")
    print()
    print(f"  realised {tot.realised:+,.2f}   unrealised {tot.unrealised:+,.2f}"
          f"   fees {-abs(tot.fees):,.4f}")
    if tot.buy_and_hold is not None:
        print(f"  buy & hold over the same period {tot.buy_and_hold:+,.2f}")
    print()
    if not txs:
        print("  no transactions yet")
        return 0
    print(f"  {len(txs)} transaction(s), newest first")
    print(f"  {'when':<17}{'side':<6}{'price':>12}{'qty':>12}"
          f"{'fee':>10}{'position':>12}{'run P&L':>12}")
    for t in txs:
        print(f"  {t.ts[:16].replace('T', ' '):<17}{t.side:<6}{t.price:>12,.6g}"
              f"{t.qty:>12,.6g}{t.fee:>10,.4f}{t.position:>12,.6g}"
              f"{t.realised:>12,.2f}")
    return 0


def cmd_service(args) -> int:
    """Install, remove, inspect or print the launchd agent."""
    action = args.action
    if action == "print":
        print(service.render().decode())
        return 0

    if action == "install":
        path = service.install(interval=args.interval)
        print(f"wrote {path}")
        print()
        print("NOT loaded — loading starts the executor, which starts placing")
        print("orders. Load it deliberately when you are ready:")
        print(f"  launchctl load -w {path}")
        print()
        print("It will then start at every LOGIN (not at boot: at boot the")
        print("keychain is locked and you are not awake to watch it).")
        return 0

    if action == "uninstall":
        path = service.plist_path()
        print(f"launchctl unload {path}")
        subprocess.run(["launchctl", "unload", str(path)],
                       capture_output=True)
        removed = service.uninstall()
        print("removed" if removed else "nothing to remove")
        return 0

    st = service.status()
    print(st.explain())
    logs = service.log_dir()
    print()
    print(f"logs           {logs}")
    if logs.exists():
        for p in sorted(logs.glob("*.log*")):
            print(f"  {p.name:28} {p.stat().st_size / 1024:8.1f} KB")
    else:
        print("  (none yet)")
    return 0 if st.running else 1


def cmd_execution(args) -> int:
    """Execution quality — the signal at small capital. P&L is not."""
    q = execution.load(args.path) if args.path else execution.load()
    print(q.explain())
    return 0


def cmd_freshness(args) -> int:
    """Every data source, when it was fetched, and whether it is in tolerance."""
    print("fetching each source to stamp it...")
    for label, fn in (("instruments", lambda: okx.get_instruments()),
                      ("tickers", lambda: okx.get_spot_tickers()),
                      ("candles", lambda: okx.get_candles("BTC-USDT", "1H",
                                                          limit=5))):
        try:
            fn()
        except Exception as e:
            print(f"  {label}: FETCH FAILED — {type(e).__name__}: {e}")
    try:
        analyst.live_fees()
    except Exception:
        pass

    model, fee_label = analyst.live_fees()
    rep = freshness.FreshnessReport()
    for src in ("candles", "tickers", "instruments"):
        rep.add(src, okx.fetched_at(src))
    rep.add("fees", okx.fetched_at("fees"), detail=fee_label)
    print()
    print(rep.explain())
    if okx.fetched_at("fees") is None:
        print("  (fees show as never fetched because the PESSIMISTIC fallback "
              "is in use — set OKX_API_KEY to read your real tier)")

    print()
    print("caches on disk")
    cache_dir = Path(os.getenv("TRADEBUNNY_HOME", ".")).resolve() / "data" / "candles"
    if cache_dir.exists():
        files = sorted(cache_dir.glob("*.json"))
        print(f"  {len(files)} cached candle series in {cache_dir}")
        import json as _json
        for p in files[:5]:
            try:
                d = _json.loads(p.read_text())
                print(f"    {p.name:24} {d.get('n', '?')} bars, written "
                      f"{d.get('written_at', 'unknown')}")
            except Exception:
                print(f"    {p.name:24} unreadable")
    else:
        print("  none")

    beat = load_heartbeat(args.heartbeat)
    print()
    print("executor heartbeat")
    print(f"  {'stopped' if (beat and beat.is_down()) else 'alive' if beat else 'NOT RUNNING'}"
          f"  {freshness.human_age(beat.age_seconds()) if beat else ''}")
    return 0 if rep.all_fresh() else 1


def cmd_drift(args) -> int:
    """Re-run the evidence on every live grid, using post-approval bars only."""
    control = Control(args.control)
    reports = check_drift(control, plans_dir=PLANS_DIR, bar=args.bar,
                          notify=notify if args.notify else (lambda m: None))
    if not reports:
        print("no live grids to check")
        return 0
    for r in reports:
        print(r.explain())
        print()
    decayed = [r for r in reports if r.decayed]
    print(f"{len(decayed)} of {len(reports)} grid(s) have a decayed thesis."
          + ("  Nothing has been stopped — that is your decision."
             if decayed else ""))
    return 1 if decayed else 0


def cmd_check(args) -> int:
    """Live connectivity against OKX PUBLIC endpoints. Used by `make check`."""
    ok = True
    print("OKX public market data")
    result = okx.check()
    for k, v in result.items():
        print(f"  {k:<24} {v}")
    ok = ok and bool(result.get("ok"))

    print("\nbars_per_day")
    for bar in ("1H", "4H", "1D"):
        print(f"  {bar:<24} {okx.bars_per_day(bar)}")

    print("\nfee model")
    f = FeeModel()
    print(f"  break-even spacing       {f.breakeven_spacing():.4%}")
    print(f"  min viable spacing       {f.min_viable_spacing():.4%}")

    print("\ncontrol")
    control = Control(args.control)
    state = control.state()
    print(f"  file                     {control.path}")
    print(f"  trading                  {'on' if state.trading_enabled else 'HALTED'}")
    print(f"  mode                     {'LIVE' if control.is_live() else 'paper'}")

    print("\nexecutor heartbeat")
    beat = load_heartbeat(args.heartbeat)
    if beat is None:
        print("  none — executor is not running")
    else:
        print(f"  age                      {beat.age_seconds():.0f}s "
              f"({'DOWN' if beat.is_down() else 'ok'})")

    print("\n" + ("OK" if ok else "FAILED — OKX public data unreachable"))
    return 0 if ok else 1


# ---------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="grid", description=__doc__.split("\n")[0])
    p.add_argument("--control", default=str(HOME / "data" / "control.json"),
                   help="path to control.json")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the control file").set_defaults(fn=cmd_init)

    s = sub.add_parser("scan", help="run the analyst once, now")
    s.add_argument("--available", type=float, default=2000.0,
                   help="capital available to the scan")
    s.add_argument("--bar", default="1H")
    s.add_argument("--show-all", action="store_true",
                   help="print every candidate with its confidence and the "
                        "gate that rejected it")
    s.add_argument("--results", default=None,
                   help="where to write the markdown report "
                        "(default scan_results.md)")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("run", help="the executor loop (keep this running)")
    s.add_argument("--interval", type=float, default=INTERVAL)
    s.add_argument("--max-passes", type=int, default=None,
                   help="stop after N passes (testing)")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("supervise", help="run the executor, restarting on crash")
    s.add_argument("--interval", type=float, default=INTERVAL)
    s.add_argument("--max-restarts", type=int, default=None)
    s.set_defaults(fn=cmd_supervise)

    s = sub.add_parser("web", help="the approval interface")
    s.add_argument("--port", type=int, default=int(os.getenv("GRID_WEB_PORT", "8080")))
    s.set_defaults(fn=cmd_web)

    s = sub.add_parser("status", help="what is running, what is halted")
    s.add_argument("--heartbeat", default=str(HEARTBEAT_PATH))
    s.add_argument("--state-dir", default=str(STATE_DIR))
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("halt", help="stop everything")
    s.add_argument("--reason", default="halted from the CLI")
    s.set_defaults(fn=cmd_halt)

    sub.add_parser("resume", help="clear halts").set_defaults(fn=cmd_resume)

    s = sub.add_parser("backtest", help="replay a fitted grid on recent candles")
    s.add_argument("symbol")
    s.add_argument("--bar", default="1H")
    s.add_argument("--split", type=float, default=0.6)
    s.add_argument("--deep", action="store_true",
                   help="use get_history for years of data, not the 300-bar "
                        "recent window")
    s.add_argument("--years", type=float, default=2.0)
    s.add_argument("--candles", metavar="FILE",
                   help="replay candles from a saved JSON file instead of "
                        "calling OKX (offline; see examples/)")
    s.set_defaults(fn=cmd_backtest)

    s = sub.add_parser("grid", help="every transaction on one grid")
    s.add_argument("plan_id", help="plan id or symbol")
    s.add_argument("--db", default=None)
    s.set_defaults(fn=cmd_grid)

    s = sub.add_parser("service", help="the launchd agent for the executor")
    s.add_argument("action", nargs="?", default="status",
                   choices=["status", "install", "uninstall", "print"])
    s.add_argument("--interval", type=float, default=INTERVAL)
    s.set_defaults(fn=cmd_service)

    s = sub.add_parser("execution",
                       help="fill rate, slippage, fee accuracy, uptime")
    s.add_argument("--path", default=None)
    s.set_defaults(fn=cmd_execution)

    s = sub.add_parser("freshness", help="how current is every data source?")
    s.add_argument("--heartbeat", default=str(HEARTBEAT_PATH))
    s.set_defaults(fn=cmd_freshness)

    s = sub.add_parser("drift", help="has any live grid's thesis decayed?")
    s.add_argument("--bar", default="1H")
    s.add_argument("--notify", action="store_true",
                   help="send the alert as well as printing it")
    s.set_defaults(fn=cmd_drift)

    s = sub.add_parser("check", help="live connectivity check")
    s.add_argument("--heartbeat", default=str(HEARTBEAT_PATH))
    s.set_defaults(fn=cmd_check)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S")
    args.control = Path(args.control)
    for attr in ("heartbeat", "state_dir"):
        if hasattr(args, attr):
            setattr(args, attr, Path(getattr(args, attr)))
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
