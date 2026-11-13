"""The council's interface, following the "TradeBunny web design" mock-ups.

    /                 Money: are we beating the index? convene, rules, watch list
    /meetings/<id>    Meeting: one debate, round by round, live while it runs
    /meetings         Meetings: what has the council decided?
    /portfolio        Portfolio: what do I own?
    /pipeline         Pipeline: the five stages, from the scout to the watch
    /research/<id>    one research file
    /rules            Rules: what can't happen?

Binds to 127.0.0.1. If COUNCIL_WEB_TOKEN is set, every action (convening a
meeting, running the review) needs it; open the page once with ?token=...
"""

from __future__ import annotations

import html
import os
import re
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)

from . import (agents, approvals, learning, mandate as mandate_mod, paths, risk, scan,
               screening, views)
from .data import DataError, Steward
from .meeting import Meeting
from .portfolio import Portfolio
from .review import TICKER as PORTFOLIO, PortfolioMeeting
from .store import Store, now

ROLE_STYLE = {
    "macro": ("Macro strategist", "MS", "blue"),
    "thesis": ("Thesis analyst", "TA", "green"),
    "valuation": ("Valuation analyst", "VA", "amber"),
    "redteam": ("Red team", "RT", "red"),
    "earnings": ("Earnings analyst", "EA", "teal"),
    "scout": ("Scout", "SC", "blue"),
    "analyst": ("Research analyst", "AN", "green"),
    "sentinel": ("Sentinel", "SN", "amber"),
    "risk": ("Risk officer", "RO", "violet"),
    "cio": ("Chair / CIO", "CIO", "ink"),
    "checker": ("Fact-checker", "FC", "slate"),
    "you": ("You", "YOU", "ink"),
    "autopilot": ("Autopilot", "AP", "ink"),
    "code": ("Rules (code)", "</>", "slate"),
}


# ------------------------------------------------------------- markdown

def md(text: Optional[str]) -> str:
    """A small, safe markdown subset. Model and web text is escaped first,
    so nothing in a memo can inject HTML into the page."""
    if not text:
        return ""
    out, para, items, kind = [], [], [], None

    def inline(s: str) -> str:
        s = html.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
                   r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>', s)
        return s

    def flush():
        nonlocal para, items, kind
        if para:
            out.append("<p>" + " ".join(inline(p) for p in para) + "</p>")
            para = []
        if items:
            tag = "ol" if kind == "ol" else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{inline(i)}</li>" for i in items) + f"</{tag}>")
            items, kind = [], None

    for line in text.splitlines():
        s = line.strip()
        if not s:
            flush()
        elif m := re.match(r"^#{1,4}\s+(.*)", s):
            flush()
            out.append(f"<h3>{inline(m.group(1))}</h3>")
        elif m := re.match(r"^[-*]\s+(.*)", s):
            if para or kind == "ol":
                flush()
            kind = "ul"
            items.append(m.group(1))
        elif m := re.match(r"^\d+[.)]\s+(.*)", s):
            if para or kind == "ul":
                flush()
            kind = "ol"
            items.append(m.group(1))
        else:
            if items:
                flush()
            para.append(s)
    flush()
    return "".join(out)


def eur(x: Optional[float], digits: int = 0, sign: bool = False) -> str:
    if x is None:
        return "—"
    s = f"€{abs(x):,.{digits}f}"
    if x < 0:
        return "−" + s
    return ("+" + s) if sign else s


def pct(x: Optional[float], digits: int = 1, sign: bool = False) -> str:
    if x is None:
        return "—"
    v = x * 100
    if sign:
        return ("+" if v >= 0 else "−") + f"{abs(v):.{digits}f}%"
    return f"{v:.{digits}f}%"


def arrow(x: float) -> str:
    return "▲" if x > 1e-9 else "▼" if x < -1e-9 else ""


def resolve(text: str, m: mandate_mod.Mandate) -> str:
    """'asml', 'ASML.AS', 'air liquide' -> the mandate's ticker. Unknown
    names pass through unchanged so the gate can say why they are refused."""
    t = text.strip()
    if not t:
        return t
    pool = [(i.ticker, i.name) for i in m.instruments] + [(w.ticker, w.name) for w in m.all_watch()] \
        + [(r.ticker, r.name) for r in m.rejected]
    low = t.lower()
    for tk, name in pool:
        if tk.lower() == low:
            return tk
    for tk, name in pool:
        if tk.split(".")[0].lower() == low or name.lower() == low:
            return tk
    hits = [tk for tk, name in pool if low in name.lower()]
    return hits[0] if len(hits) == 1 else t.upper()


# ------------------------------------------------------------------ app

def default_backend():
    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return agents.ClaudeBackend()
    return agents.ScriptedBackend()


def create_app(store: Optional[Store] = None, steward: Optional[Steward] = None,
               mandate: Optional[mandate_mod.Mandate] = None,
               backend_factory: Callable[[], agents.Backend] = default_backend,
               token: Optional[str] = None, run_async: bool = True,
               book: str = "main", peer: Optional[dict] = None) -> Flask:
    """One book's app. `peer` is the other book ({name, url, store, mandate}),
    summarised on the Money page so the two can be compared."""
    paths.load_env()
    app = Flask(__name__)
    app.secret_key = os.getenv("COUNCIL_SECRET") or os.urandom(24)
    token = token if token is not None else os.getenv("COUNCIL_WEB_TOKEN", "")
    store = store or Store(paths.db_path(book))
    steward = steward or Steward(snapshot=os.getenv("COUNCIL_OFFLINE") == "1")
    m = mandate or mandate_mod.load(paths.mandate_path(book))
    screening.attach(m, store)                  # the scout's shortlist joins the universe
    pf = Portfolio(store, steward, m)
    backend_name = backend_factory().name

    app.jinja_env.filters.update(md=md, eur=eur, pct=pct, arrow=arrow, day=views.day_label,
                                 stamp=views.stamp)
    app.jinja_env.globals.update(ROLE_STYLE=ROLE_STYLE, mandate=m, STATUS_LABEL=views.STATUS_LABEL)

    @app.context_processor
    def inject():
        latest = store.meetings(1)
        return {"backend_name": backend_name, "data_mode": steward.mode,
                "book": book, "autopilot": m.autopilot, "peer_url": (peer or {}).get("url"),
                "prices_label": views.prices_label(steward),
                "latest_meeting": latest[0]["id"] if latest else None,
                "started": pf.started, "needs_token": bool(token) and not session.get("ok")}

    @app.before_request
    def auth():
        if token and request.args.get("token") == token:
            session["ok"] = True
        if request.method == "POST" and token and not session.get("ok"):
            abort(403)

    def quotes():
        return steward.quotes(pf.tickers_needed())

    def _peer_summary() -> Optional[dict]:
        if not peer:
            return None
        ppf = Portfolio(peer["store"], steward, peer["mandate"])
        if not ppf.started:
            return {"name": peer["name"], "url": peer["url"], "started": False}
        q = steward.quotes(ppf.tickers_needed())
        cap = float(peer["store"].get_meta("capital_eur", peer["mandate"].start_capital_eur))
        value, bench = ppf.book(q).total, ppf.benchmark_value(q)
        own = [t for t in peer["store"].trades() if t["meeting_id"]]
        return {"name": peer["name"], "url": peer["url"], "started": True, "value": value,
                "gain": value / cap - 1, "edge_pts": (value - bench) / cap * 100,
                "meetings": len(peer["store"].meetings(500)), "trades": len(own),
                "pending": len(peer["store"].proposals("pending"))}

    # -------------------------------------------------------------- pages

    @app.get("/")
    def dashboard():
        if not pf.started:
            return render_template("welcome.html")
        try:
            q = quotes()
        except DataError as e:
            return render_template("error.html", error=str(e)), 503
        names = [(t, m.name_of(t)) for t in m.universe()]
        approvals.expire(store)
        return render_template("dashboard.html", v=views.money(store, pf, m, q), names=names,
                               peer_v=_peer_summary(),
                               agenda=scan.agenda(store, pf, m, q),
                               last_scan=store.get_meta("last_scan"),
                               scanning=bool(store.get_meta("scanning")))

    @app.get("/meetings")
    def meetings():
        rows = [views.decision_row(x, m) for x in store.meetings(500)]
        f = request.args.get("f", "all")
        groups = {"buy": ("buy", "add"), "watch": ("watch", "hold"), "avoid": ("reject",)}
        shown = [r for r in rows if f == "all" or r["decision"] in groups.get(f, ())]
        count = lambda keys: sum(1 for r in rows if r["decision"] in keys)
        first = store.meetings(500)[-1]["started_at"] if rows else None
        return render_template("meetings.html", rows=shown, f=f, total=len(rows),
                               since=views.day_label(first), cost=sum(r["cost"] for r in rows),
                               buys=count(groups["buy"]), watches=count(groups["watch"]),
                               avoids=count(groups["avoid"]))

    def _meeting_ctx(mt: dict) -> tuple[dict, str]:
        if mt["kind"] == "portfolio":
            return views.portfolio_meeting(store, m, mt["id"]), "_book.html"
        return views.debate(store, m, mt["id"]), "_debate.html"

    @app.get("/meetings/<int:mid>")
    def meeting_page(mid: int):
        mt = store.meeting(mid)
        if not mt:
            abort(404)
        ctx, partial = _meeting_ctx(mt)
        return render_template("meeting.html", partial=partial, **ctx)

    @app.get("/meetings/<int:mid>/live")
    def meeting_live(mid: int):
        mt = store.meeting(mid)
        if not mt:
            abort(404)
        ctx, partial = _meeting_ctx(mt)
        return jsonify(status=mt["status"], cost=mt["cost_usd"], html=render_template(partial, **ctx))

    @app.get("/pipeline")
    def pipeline():
        if not pf.started:
            return redirect(url_for("dashboard"))
        try:
            q = quotes()
        except DataError as e:
            return render_template("error.html", error=str(e)), 503
        return render_template("pipeline.html", p=views.pipeline(store, pf, m, q), busy=busy_label())

    @app.get("/research/<int:did>")
    def research_page(did: int):
        v = views.dossier_page(store, m, did)
        if not v:
            abort(404)
        return render_template("research.html", r=v)

    @app.get("/portfolio")
    def portfolio_page():
        if not pf.started:
            return redirect(url_for("dashboard"))
        q = quotes()
        book = pf.book(q)
        capital = float(store.get_meta("capital_eur", m.start_capital_eur))
        positions = pf.positions(q)
        invested = sum(p.value_eur for p in positions)
        paid = sum(p.cost_eur for p in positions)
        progress = {p.ticker: views.tranche_progress(store, p.ticker) for p in positions}
        trades = store.trades()
        return render_template(
            "portfolio.html", book=book, capital=capital, positions=positions, progress=progress,
            invested=invested, paid=paid, statement=views.statement(store, capital, m),
            tranches=store.tranches("planned"), started=views.day_label(store.get_meta("started_on")),
            fees=sum(t["fee_eur"] for t in trades), n_trades=len(trades),
            promised=sum(book.pending.values()))

    @app.get("/rules")
    def rules():
        book = pf.book(quotes()) if pf.started else None
        rows = views.limits(book, m, store)
        flagged = {}
        for x in store.meetings(500):
            d = x.get("decision") or {}
            if d.get("breaker_triggered"):
                flagged.setdefault(x["ticker"], x["id"])
        return render_template("rules.html", rows=rows, lim=m.limits, flagged=flagged,
                               info=views.mandate_info(paths.mandate_path()),
                               checked=datetime.now().strftime("%H:%M"),
                               ok=sum(1 for r in rows if r["status"] == "ok"),
                               close=sum(1 for r in rows if r["status"] == "close"),
                               over=sum(1 for r in rows if r["status"] == "over"))

    @app.get("/stocks/<ticker>")
    def stock_page(ticker: str):
        if not pf.started:
            return redirect(url_for("dashboard"))
        t = resolve(ticker, m)
        known = t in m.universe() or m.rejection(t) or pf.quantities().get(t) or store.candidate(t)
        if not known:
            abort(404)
        try:
            v = views.stock(store, pf, m, steward, t)
        except DataError as e:
            return render_template("error.html", error=str(e)), 503
        return render_template("stock.html", s=v)

    @app.get("/scorecard")
    def scorecard():
        rows = learning.outcomes(store, m)
        return render_template("scorecard.html", rows=rows, sc=learning.scorecard(store, m),
                               due=learning.due_postmortems(store, m),
                               pms=store.postmortems(), proposed=store.lessons("proposed"),
                               approved=store.lessons("approved"), horizons=learning.HORIZONS,
                               pm_left=max(0, m.limits.max_postmortems_per_month
                                           - learning.postmortems_this_month(store)))

    @app.get("/postmortems/<int:pid>")
    def postmortem_page(pid: int):
        pm = store.postmortem(pid)
        if not pm:
            abort(404)
        lessons = [x for x in store.lessons() if x["postmortem_id"] == pid]
        return render_template("postmortem.html", pm=pm, lessons=lessons,
                               mt=store.meeting(pm["meeting_id"]))

    @app.post("/postmortems")
    def run_postmortem():
        mid = int(request.form.get("meeting_id", 0))
        case = next((c for c in learning.due_postmortems(store, m) if c["meeting_id"] == mid), None)
        if not case:
            flash("That decision has no post-mortem due.")
            return redirect(url_for("scorecard"))
        if learning.postmortems_this_month(store) >= m.limits.max_postmortems_per_month:
            flash("No post-mortem slot left this month.")
            return redirect(url_for("scorecard"))
        pid = learning.run_postmortem(Store(store.path), steward, m, case, backend_factory())
        return redirect(url_for("postmortem_page", pid=pid))

    @app.post("/lessons/<int:lid>/<action>")
    def decide_lesson(lid: int, action: str):
        if action not in ("approve", "reject"):
            abort(400)
        if store.decide_lesson(lid, "approved" if action == "approve" else "rejected"):
            flash("Lesson approved: every member reads it before every meeting." if action == "approve"
                  else "Lesson rejected.")
        return redirect(request.form.get("next") or url_for("scorecard"))

    @app.get("/how")
    def how():
        return render_template("how.html", lim=m.limits)

    # ------------------------------------------------------------ actions

    @app.post("/proposals/<int:pid>/approve")
    def approve(pid: int):
        try:
            r = approvals.approve(store, pf, m, pid)
            n = len(r["trades"])
            flash(f"Approved. {n} order{'s' if n != 1 else ''} placed"
                  + (f"; {len(r['planned'])} more tranches scheduled." if r["planned"] else "."))
        except approvals.ApprovalError as e:
            flash(f"Not placed: {e}")
        return redirect(request.form.get("next") or url_for("dashboard"))

    @app.post("/proposals/<int:pid>/reject")
    def reject(pid: int):
        try:
            approvals.reject(store, pid)
            flash("Rejected. Nothing was traded.")
        except approvals.ApprovalError as e:
            flash(str(e))
        return redirect(request.form.get("next") or url_for("dashboard"))

    @app.post("/scan")
    def run_scan():
        if store.get_meta("scanning"):
            flash("The council is already meeting.")
            return redirect(url_for("meetings"))
        plan = scan.agenda(store, pf, m, quotes())
        if not plan["picked"]:
            flash("Nothing on the agenda today: no trigger is hit or close, and no holding is due for review.")
            return redirect(url_for("dashboard"))

        def work():
            store.set_meta("scanning", True)
            try:
                scan.run(Store(store.path), steward, m, pf, backend_factory())
            finally:
                store.set_meta("scanning", False)
        if run_async:
            threading.Thread(target=work, daemon=True).start()
        else:
            work()
        flash("The council is meeting on " + ", ".join(c["name"] for c in plan["picked"])
              + ". Proposals will wait for your OK.")
        return redirect(url_for("meetings"))

    def busy_label() -> str:
        """What is running in the background; a flag older than 30 minutes is
        from a server that stopped mid-run, so it no longer blocks."""
        b = store.get_meta("busy") or {}
        if not isinstance(b, dict) or not b.get("at"):
            return ""
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(b["at"])).total_seconds()
        return b["label"] if age < 1800 else ""

    def _background(label: str, fn) -> None:
        """Run a long stage in a thread (AI work takes minutes), one at a time."""
        def work():
            store.set_meta("busy", {"label": label, "at": now()})
            try:
                fn()
            finally:
                store.set_meta("busy", {})
        if run_async:
            threading.Thread(target=work, daemon=True).start()
        else:
            work()

    def _busy() -> bool:
        if busy_label():
            flash(f"Busy: {busy_label()}. Try again when it is done.")
            return True
        return False

    @app.post("/screen")
    def run_screen():
        if _busy():
            return redirect(url_for("pipeline"))
        b = backend_factory()

        def go():
            st2 = Store(store.path)
            screening.run_screen(st2, steward, m, b)
            screening.research_new_ideas(st2, steward, m, b)
        _background("the scout is looking for ideas", go)
        flash("The scout is looking. New ideas get a research file right after, within the weekly budget.")
        return redirect(url_for("pipeline"))

    @app.post("/research")
    def run_research():
        t = resolve(request.form.get("ticker", ""), m)
        if not t or not (t in m.universe() or store.candidate(t)):
            flash("Pick a name the council follows: the model portfolio, the watch list or the scout's ideas.")
            return redirect(url_for("pipeline"))
        if screening.research_left(store, m) <= 0:
            flash(f"This week's {m.limits.max_research_files_per_week} research files are used.")
            return redirect(url_for("pipeline"))
        if _busy():
            return redirect(url_for("pipeline"))
        b = backend_factory()
        _background(f"writing the research file on {views.display_name(m, store, t)}",
                    lambda: screening.write_dossier(Store(store.path), steward, m, t, b, "Asked for by you"))
        flash(f"Writing the research file on {views.display_name(m, store, t)}. It appears below when done.")
        return redirect(request.form.get("next") or url_for("pipeline"))

    @app.post("/candidates/<ticker>/drop")
    def drop_candidate(ticker: str):
        if screening.drop(store, ticker):
            flash(f"Dropped {views.display_name(m, store, ticker)}: it leaves the shortlist.")
        return redirect(request.form.get("next") or url_for("pipeline"))

    @app.post("/portfolio-meeting")
    def run_portfolio_meeting():
        if not pf.started:
            return redirect(url_for("dashboard"))
        b = backend_factory()
        mid = store.start_meeting(PORTFOLIO, "portfolio", b.name)
        pm = PortfolioMeeting(Store(store.path), steward, m, pf, b)
        if run_async:
            threading.Thread(target=pm.run, args=(mid,), daemon=True).start()
        else:
            pm.run(mid)
        return redirect(url_for("meeting_page", mid=mid))

    @app.post("/start")
    def start():
        pf.start()
        flash("Paper portfolio opened: the first of three tranches is bought.")
        return redirect(url_for("dashboard"))

    @app.post("/review")
    def review():
        for e in pf.review():
            flash(e)
        return redirect(url_for("dashboard"))

    @app.post("/meetings")
    def convene():
        ticker = resolve(request.form.get("ticker", ""), m)
        if not ticker:
            flash("Type a ticker or a company name first.")
            return redirect(url_for("dashboard"))
        backend = backend_factory()
        held = pf.quantities().get(ticker, 0) > 0
        mid = store.start_meeting(ticker, "holding" if held else "idea", backend.name)
        mt = Meeting(Store(store.path), steward, m, pf, backend)
        if run_async:
            threading.Thread(target=mt.run, args=(ticker, mid), daemon=True).start()
        else:
            mt.run(ticker, mid)
        return redirect(url_for("meeting_page", mid=mid))

    return app


def create_books(steward: Optional[Steward] = None, **kw) -> Flask:
    """Both books in one app: yours at /, the Autopilot at /autopilot/."""
    from werkzeug.middleware.dispatcher import DispatcherMiddleware
    os.environ.setdefault("COUNCIL_SECRET", os.urandom(24).hex())   # one session for both
    steward = steward or Steward(snapshot=os.getenv("COUNCIL_OFFLINE") == "1")
    main_store, auto_store = Store(paths.db_path("main")), Store(paths.db_path("autopilot"))
    main_m = mandate_mod.load(paths.mandate_path("main"))
    auto_path = paths.mandate_path("autopilot")
    if not auto_path.exists():
        mandate_mod.initialise(book="autopilot")
    auto_m = mandate_mod.load(auto_path)
    main_app = create_app(main_store, steward, main_m, book="main",
                          peer={"name": "Autopilot", "url": "/autopilot/", "store": auto_store,
                                "mandate": auto_m}, **kw)
    auto_app = create_app(auto_store, steward, auto_m, book="autopilot",
                          peer={"name": "With you", "url": "/", "store": main_store,
                                "mandate": main_m}, **kw)
    main_app.wsgi_app = DispatcherMiddleware(main_app.wsgi_app, {"/autopilot": auto_app.wsgi_app})
    return main_app


def main() -> None:
    port = int(os.getenv("COUNCIL_PORT", "8090"))
    app = create_books()
    print(f"TradeBunny council on http://127.0.0.1:{port}/  (Autopilot: /autopilot/)")
    app.run(host="127.0.0.1", port=port, threaded=True)


if __name__ == "__main__":
    main()
