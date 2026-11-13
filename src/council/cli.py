"""The council from the terminal.

    python -m council.cli init          copy the mandate to data/council/ (private)
    python -m council.cli start         open the paper portfolio, first tranche
    python -m council.cli meet TICKER   hold a meeting; a verdict becomes a proposal
    python -m council.cli scan          let the council pick its own agenda and meet
    python -m council.cli screen        the scout looks for new ideas (stage 1)
    python -m council.cli research TICKER   write a research file (stage 2)
    python -m council.cli watch         earnings and big drops on what we own (stage 5)
    python -m council.cli portfolio-meeting   the monthly look at the whole book
    python -m council.cli proposals     what is waiting for my OK
    python -m council.cli approve ID    approve a proposal: re-check the rules, then trade
    python -m council.cli reject ID     reject it: nothing is traded
    python -m council.cli review        approved tranches, triggers, today's value
    python -m council.cli daily         review + scan: what the scheduled job runs
    python -m council.cli schedule ...  install | remove | status: the daily job
    python -m council.cli scorecard     is the council any good? past calls vs the index
    python -m council.cli lesson approve|reject ID   decide a post-mortem's lesson
    python -m council.cli web           the interface on http://127.0.0.1:8090
    python -m council.cli demo          offline demo in a throwaway folder

Every command takes --book autopilot (or rules) to act on a book where the
council approves its own proposals (paper money only).

Meetings use the Claude API when ANTHROPIC_API_KEY is set (shell or .env),
scripted members otherwise. COUNCIL_OFFLINE=1 uses the saved price snapshot.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile


def _parts(offline: bool = False):
    from . import data, mandate, paths, portfolio, screening, store, web
    paths.load_env()
    if paths.book() != "main" and not paths.mandate_path().exists():
        mandate.initialise()          # the other books never run on the example rules
    m = mandate.load()
    steward = data.Steward(snapshot=offline or os.getenv("COUNCIL_OFFLINE") == "1")
    st = store.Store()
    screening.attach(m, st)           # the scout's shortlist joins the universe
    pf = portfolio.Portfolio(st, steward, m)
    from . import agents
    backend = agents.ScriptedBackend() if m.rules_only else web.default_backend()
    return m, steward, st, pf, backend


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
    from . import paths
    print(f"[{paths.BOOK_NAMES[paths.book()]}] "
          f"Opened with {m.start_capital_eur:,.0f} EUR. Bought {len(trades)} first tranches; "
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
        if o.get("proposal"):
            p = st.proposal(o["proposal"])
            print(f"\n  Proposal #{p['id']}: {p['summary']}")
            print(f"  Waiting for your OK: council approve {p['id']}   (or: council reject {p['id']})")
    print(f"  cost: ${mt['cost_usd']:.2f}")
    return 0


def cmd_scan(a) -> int:
    from . import scan
    m, steward, st, pf, backend = _parts()
    if not pf.started:
        print("Open the paper portfolio first: council start", file=sys.stderr)
        return 2
    plan = scan.agenda(st, pf, m, steward.quotes(pf.tickers_needed()))
    if not plan["picked"]:
        print("Nothing on the agenda today.")
    for c in plan["picked"]:
        print(f"- {c['name']}: {c['reason']}")
    if a.dry_run or not plan["picked"]:
        return 0
    print(f"\nThe council meets ({backend.name})...")
    for mid in scan.run(st, steward, m, pf, backend):
        mt = st.meeting(mid)
        d = mt["decision"] or {}
        print(f"  #{mid} {m.name_of(mt['ticker'])}: {d.get('decision', mt['status'])} "
              f"{d.get('action_label', mt.get('error') or '')}")
    return cmd_proposals(a)


def cmd_screen(a) -> int:
    from . import screening
    m, steward, st, pf, backend = _parts()
    print(f"The scout is looking ({backend.name})...")
    r = screening.run_screen(st, steward, m, backend)
    if r.get("error"):
        print(f"Failed: {r['error']}", file=sys.stderr)
        return 1
    for x in r["added"]:
        print(f"+ {x['name']} ({x['ticker']}): {x['theme']}")
    for x in r["turned_away"]:
        print(f"  turned away {x['name']} ({x['ticker']}): {x['why']}")
    if r["added"]:
        print("Research files next: council research TICKER (the daily job writes them too).")
    return 0


def cmd_research(a) -> int:
    from . import screening
    m, steward, st, pf, backend = _parts()
    print(f"Writing the research file on {a.ticker} ({backend.name})...")
    d = st.dossier(screening.write_dossier(st, steward, m, a.ticker, backend, "Asked for by you"))
    if d["status"] != "done":
        print(f"Failed: {d['data'].get('error')}", file=sys.stderr)
        return 1
    o = d["data"]["output"]
    print(o["summary"])
    print(f"Verdict: {o['verdict'].replace('_', ' ')}: {o['verdict_reason']}")
    return 0


def cmd_watch(a) -> int:
    from . import watch
    m, steward, st, pf, backend = _parts()
    events = watch.run(st, steward, m, pf, backend if backend.name == "claude" else None)
    print("\n".join(f"- {e}" for e in events) or "Nothing to report: no results days or big drops.")
    return 0


def cmd_portfolio_meeting(a) -> int:
    from . import review
    m, steward, st, pf, backend = _parts()
    if not pf.started:
        print("Open the paper portfolio first: council start", file=sys.stderr)
        return 2
    print(f"The portfolio meeting ({backend.name})...")
    mid = review.PortfolioMeeting(st, steward, m, pf, backend).run()
    mt = st.meeting(mid)
    o = mt.get("outcome") or {}
    print(f"Meeting #{mid}: {mt['status']}. {o.get('summary') or mt.get('error') or ''}")
    if o.get("review_queue"):
        print("Next for the committee: " + ", ".join(m.name_of(t) for t in o["review_queue"]))
    return 0


def cmd_proposals(a) -> int:
    from . import approvals
    m, steward, st, pf, _ = _parts()
    approvals.expire(st)
    ps = st.proposals("pending")
    if not ps:
        print("Nothing is waiting for your OK.")
    for p in ps:
        print(f"#{p['id']}  {p['summary']}  (expires {p['expires_at'][:10]})")
    return 0


def cmd_approve(a) -> int:
    from . import approvals
    m, steward, st, pf, _ = _parts()
    try:
        r = approvals.approve(st, pf, m, a.id)
    except approvals.ApprovalError as e:
        print(f"Not placed: {e}", file=sys.stderr)
        return 1
    for t in r["trades"]:
        print(f"{t['side'].upper()} {t['qty']:.4f} {t['ticker']} at {t['price']:,.2f} {t['currency']} "
              f"= €{t['amount_eur']:,.2f} ({t.get('broker', 'paper')})")
    for x in r["planned"]:
        print(f"Planned: {x['note']} on {x['due']}, €{x['amount_eur']:,.2f}")
    return 0


def cmd_reject(a) -> int:
    from . import approvals
    m, steward, st, pf, _ = _parts()
    try:
        approvals.reject(st, a.id)
    except approvals.ApprovalError as e:
        print(e, file=sys.stderr)
        return 1
    print("Rejected. Nothing was traded.")
    return 0


def cmd_daily(a) -> int:
    from . import scan
    m, steward, st, pf, backend = _parts()
    if not pf.started:
        print("Open the paper portfolio first: council start", file=sys.stderr)
        return 2
    from datetime import datetime
    print(f"TradeBunny daily run, {datetime.now():%Y-%m-%d %H:%M} ({backend.name})")
    if m.rules_only:                  # the comparison book: rules, no AI, always meets
        for e in scan.daily(st, steward, m, pf, None if a.no_scan else backend, stages_on=False):
            print("-", e)
        return 0
    meet = backend if not a.no_scan and backend.name == "claude" else None
    for e in scan.daily(st, steward, m, pf, meet):
        print("-", e)
    if not a.no_scan and backend.name != "claude":
        print("- The council did not meet: no AI key yet. Add ANTHROPIC_API_KEY to .env "
              "and it will meet on its agenda every evening.")
    return 0


def cmd_scorecard(a) -> int:
    from . import learning
    m, steward, st, pf, _ = _parts()
    sc = learning.scorecard(st, m)
    hr = f"{sc['hit_rate'] * 100:.0f}%" if sc["hit_rate"] is not None else "-"
    print(f"Decisions measured: {sc['measured']} · judged: {sc['judged']} · right: {sc['right']} ({hr})")
    if not sc["enough"]:
        print("Too early to judge: a score means something after about 10 judged decisions.")
    for r in learning.outcomes(st, m)[:20]:
        lt = r["latest"]
        res = (f"{lt['excess']:+.1f} pts vs index after {round(lt['horizon'] / 30.4)} mo: {r['verdict']}"
               if lt else "waiting for its first mark")
        print(f"  #{r['meeting_id']} {r['name']}: {r['decision']} on {r['date']} · {res}")
    for c in learning.due_postmortems(st, m):
        print(f"  Post-mortem due: {c['name']} (#{c['meeting_id']}): {c['reason']}")
    for x in st.lessons("proposed"):
        print(f"  Lesson waiting for your OK: #{x['id']} {x['text']}")
    return 0


def cmd_lesson(a) -> int:
    m, steward, st, pf, _ = _parts()
    ok = st.decide_lesson(a.id, "approved" if a.action == "approve" else "rejected")
    print(("Approved: every member reads it before every meeting." if a.action == "approve" else "Rejected.")
          if ok else "That lesson is not waiting for an answer.")
    return 0 if ok else 1


def cmd_schedule(a) -> int:
    from . import schedule
    if a.action == "install":
        print(f"Installed: {schedule.install()}\n{schedule.status()}")
    elif a.action == "remove":
        print("Removed." if schedule.remove() else "It was not installed.")
    else:
        print(schedule.status())
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
    for book in ("main", "autopilot", "rules"):
        os.environ["COUNCIL_BOOK"] = book
        m, steward, st, pf, backend = _parts(offline=True)
        pf.start()
        for t in ("TSM", "ASML.AS", "HWM", "AI.PA"):
            Meeting(st, steward, m, pf, backend).run(t)
        if m.rules_only:
            continue
        from . import review, screening
        screening.run_screen(st, steward, m, backend)              # stage 1: the scout
        screening.research_new_ideas(st, steward, m, backend)      # stage 2: research files
        for c in st.candidates("shortlisted")[:1]:                 # stage 3: one goes to committee
            Meeting(st, steward, m, pf, backend).run(c["ticker"])
        review.PortfolioMeeting(st, steward, m, pf, backend).run() # the monthly look
    os.environ["COUNCIL_BOOK"] = "main"
    print(f"Demo data in {home} (scripted members, saved prices). Opening the interface...")
    from . import web
    os.environ.setdefault("COUNCIL_PORT", str(a.port))
    web.main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="council", description="TradeBunny's investment council")
    p.add_argument("--book", choices=["main", "autopilot", "rules"], default=None,
                   help="main waits for your OK (default); autopilot approves itself; "
                        "rules is the no-AI comparison book")
    sub = p.add_subparsers(dest="command", required=True)
    i = sub.add_parser("init")
    i.add_argument("--force", action="store_true", help="overwrite your mandate with the example")
    sub.add_parser("start")
    mt = sub.add_parser("meet")
    mt.add_argument("ticker")
    sc = sub.add_parser("scan")
    sc.add_argument("--dry-run", action="store_true", help="show the agenda without meeting")
    sub.add_parser("screen")
    rs = sub.add_parser("research")
    rs.add_argument("ticker")
    sub.add_parser("watch")
    sub.add_parser("portfolio-meeting")
    sub.add_parser("proposals")
    for name in ("approve", "reject"):
        x = sub.add_parser(name)
        x.add_argument("id", type=int)
    d = sub.add_parser("daily")
    d.add_argument("--no-scan", action="store_true", help="review only, no meetings")
    sub.add_parser("review")
    sub.add_parser("scorecard")
    les = sub.add_parser("lesson")
    les.add_argument("action", choices=["approve", "reject"])
    les.add_argument("id", type=int)
    sch = sub.add_parser("schedule")
    sch.add_argument("action", choices=["install", "remove", "status"])
    for name in ("web", "demo"):
        w = sub.add_parser(name)
        w.add_argument("--port", type=int, default=8090)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if a.book:
        os.environ["COUNCIL_BOOK"] = a.book
    return {"init": cmd_init, "start": cmd_start, "meet": cmd_meet, "review": cmd_review,
            "scan": cmd_scan, "proposals": cmd_proposals, "approve": cmd_approve,
            "reject": cmd_reject, "daily": cmd_daily, "schedule": cmd_schedule, "scorecard": cmd_scorecard,
            "lesson": cmd_lesson, "web": cmd_web, "screen": cmd_screen, "research": cmd_research,
            "watch": cmd_watch, "portfolio-meeting": cmd_portfolio_meeting,
            "demo": cmd_demo}[a.command](a)


if __name__ == "__main__":
    raise SystemExit(main())
