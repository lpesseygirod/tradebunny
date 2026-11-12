"""The council from the terminal.

    python -m council.cli init          copy the mandate to data/council/ (private)
    python -m council.cli start         open the paper portfolio, first tranche
    python -m council.cli meet TICKER   hold a meeting and print the verdict
    python -m council.cli review        the daily review (tranches, triggers, value)
    python -m council.cli web           the interface on http://127.0.0.1:8090
    python -m council.cli demo          offline demo in a throwaway folder

Meetings use the Claude API when ANTHROPIC_API_KEY is set, and scripted
members otherwise. COUNCIL_OFFLINE=1 uses the saved price snapshot.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile


def _parts(offline: bool = False):
    from . import agents, data, mandate, portfolio, store, web
    m = mandate.load()
    steward = data.Steward(snapshot=offline or os.getenv("COUNCIL_OFFLINE") == "1")
    st = store.Store()
    pf = portfolio.Portfolio(st, steward, m)
    return m, steward, st, pf, web.default_backend()


def cmd_init(a) -> int:
    from . import mandate
    path = mandate.initialise(overwrite=a.force)
    print(f"mandate: {path}\nEdit it there; git ignores it.")
    return 0


def cmd_start(a) -> int:
    m, steward, st, pf, _ = _parts()
    trades = pf.start()
    if not trades:
        print("The paper portfolio is already open.")
        return 0
    print(f"Opened with {m.start_capital_eur:,.0f} EUR. Bought {len(trades)} first tranches; "
          f"{len(st.tranches())} more are scheduled.")
    return 0


def cmd_meet(a) -> int:
    from .meeting import Meeting
    m, steward, st, pf, backend = _parts()
    if not pf.started:
        print("Open the paper portfolio first: council start", file=sys.stderr)
        return 2
    print(f"Convening the council on {a.ticker} ({backend.name})...")
    mid = Meeting(st, steward, m, pf, backend).run(a.ticker)
    mt = st.meeting(mid)
    d = mt["decision"] or {}
    print(f"\nMeeting #{mid}: {mt['status']}" + (f" ({mt['error']})" if mt["error"] else ""))
    if d:
        print(f"  {d['decision'].upper()}  grade {d['grade']}  {d['action_label']}")
        print(f"  sell if: {d['sell_if']}")
        o = mt["outcome"] or {}
        print(f"  risk engine: {'allowed' if o.get('allowed') else 'blocked'} "
              f"{'; '.join(o.get('blocked_by') or [])}")
    print(f"  cost: ${mt['cost_usd']:.2f}")
    return 0


def cmd_review(a) -> int:
    m, steward, st, pf, _ = _parts()
    if not pf.started:
        print("Open the paper portfolio first: council start", file=sys.stderr)
        return 2
    for e in pf.review():
        print("-", e)
    return 0


def cmd_web(a) -> int:
    from . import web
    os.environ.setdefault("COUNCIL_PORT", str(a.port))
    web.main()
    return 0


def cmd_demo(a) -> int:
    home = tempfile.mkdtemp(prefix="council-demo-")
    os.environ["TRADEBUNNY_HOME"] = home
    os.environ["COUNCIL_OFFLINE"] = "1"
    os.environ.pop("ANTHROPIC_API_KEY", None)
    from .meeting import Meeting
    m, steward, st, pf, backend = _parts(offline=True)
    pf.start()
    for t in ("TSM", "ASML.AS", "HWM", "AI.PA"):
        Meeting(st, steward, m, pf, backend).run(t)
    print(f"Demo data in {home} (scripted members, saved prices). Opening the interface...")
    from . import web
    os.environ.setdefault("COUNCIL_PORT", str(a.port))
    web.main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="council", description="TradeBunny's investment council")
    sub = p.add_subparsers(dest="command", required=True)
    i = sub.add_parser("init")
    i.add_argument("--force", action="store_true", help="overwrite your mandate with the example")
    sub.add_parser("start")
    mt = sub.add_parser("meet")
    mt.add_argument("ticker")
    sub.add_parser("review")
    for name in ("web", "demo"):
        w = sub.add_parser(name)
        w.add_argument("--port", type=int, default=8090)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    return {"init": cmd_init, "start": cmd_start, "meet": cmd_meet, "review": cmd_review,
            "web": cmd_web, "demo": cmd_demo}[a.command](a)


if __name__ == "__main__":
    raise SystemExit(main())
