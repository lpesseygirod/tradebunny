"""The investment council: mandate, risk engine, paper portfolio, meetings.

Offline: prices come from the saved snapshot, agents are scripted or fake.
"""

from types import SimpleNamespace

import pytest
import yaml

from council import agents, data, mandate, meeting, portfolio, risk, store
from council.paths import EXAMPLE_MANDATE


@pytest.fixture
def m():
    return mandate.load(EXAMPLE_MANDATE)


@pytest.fixture
def steward():
    return data.Steward(snapshot=True)


@pytest.fixture
def st(tmp_path):
    return store.Store(tmp_path / "council.db")


@pytest.fixture
def pf(st, steward, m):
    p = portfolio.Portfolio(st, steward, m)
    p.start()
    return p


def raw():
    return yaml.safe_load(EXAMPLE_MANDATE.read_text())


# ------------------------------------------------------------------ mandate

class TestMandate:
    def test_the_example_loads(self, m):
        assert m.setting == "conservative"
        assert round(m.reserve_target(), 3) == 0.07

    def test_an_unknown_key_is_refused(self):
        r = raw()
        r["limits"]["max_postion"] = 0.5          # a typo must not be ignored
        with pytest.raises(Exception):
            mandate.Mandate.model_validate(r)

    def test_weights_over_100_percent_are_refused(self):
        r = raw()
        r["instruments"][0]["weights"]["conservative"] = 0.9
        with pytest.raises(Exception):
            mandate.Mandate.model_validate(r)

    def test_more_stocks_than_the_limit_is_refused(self):
        r = raw()
        r["limits"]["max_single_stocks"] = 5
        with pytest.raises(Exception):
            mandate.Mandate.model_validate(r)


# --------------------------------------------------------------------- risk

def book_with(m, **holdings):
    return risk.Book(cash=10_000 - sum(holdings.values()), holdings=dict(holdings))


class TestRisk:
    def test_rejected_names_stop_at_the_gate(self, m):
        checks = risk.gate("SHA0.DE", m, 0)
        assert not checks[0].ok and "Rejected" in checks[0].detail

    def test_unknown_names_stop_at_the_gate(self, m):
        assert not risk.gate("GME", m, 0)[0].ok

    def test_the_monthly_meeting_cap(self, m):
        assert not risk.gate("HWM", m, m.limits.max_meetings_per_month)[1].ok

    def test_core_conviction_is_capped_at_the_max_position(self, m):
        s = risk.size_buy("HWM", "core", book_with(m), m)
        assert s.allowed and s.target_weight == pytest.approx(m.limits.max_position)

    def test_taiwan_never_above_its_cap(self, m):
        s = risk.size_buy("TSM", "core", book_with(m), m)
        assert s.target_weight == pytest.approx(m.limits.taiwan_cap)

    def test_an_eleventh_stock_needs_a_sale_first(self, m, pf):
        assert risk.stock_count(pf.book(), m) == 10
        s = risk.size_buy("HWM", "core", pf.book(), m)
        assert not s.allowed
        assert any(c.name.startswith("Stocks") for c in s.blocked_by)

    def test_the_ai_cap_counts_planned_tranches(self, m):
        b = risk.Book(cash=10_000, holdings={}, pending={"PRY.MI": 1000, "VMC": 900})
        s = risk.size_buy("ASML.AS", "core", b, m)      # 19% committed + 5% > 20%
        assert s.target_weight <= m.limits.ai_cap.conservative - 0.19 + 1e-9 or not s.allowed

    def test_a_sale_needs_a_named_thesis_breaker(self, m):
        b = book_with(m, **{"CME": 500})
        assert not risk.size_sell("CME", "sell", "", b, m).allowed
        assert risk.size_sell("CME", "sell", "A clearing competitor emerged", b, m).allowed

    def test_a_trim_sells_half(self, m):
        s = risk.size_sell("CME", "trim", "Volumes fell", book_with(m, **{"CME": 500}), m)
        assert s.amount_eur == pytest.approx(250)

    def test_new_names_are_funded_from_the_reserve_first(self, m):
        s = risk.size_buy("HWM", "starter", book_with(m), m)
        assert s.funding[0][0] == "watch-list reserve"


# ---------------------------------------------------------------- portfolio

class TestPortfolio:
    def test_start_buys_the_first_of_three_tranches(self, pf, st, m):
        assert len(st.trades()) == len([i for i in m.instruments if i.weights.conservative > 0])
        assert len(st.tranches()) == 2 * len(st.trades())

    def test_start_is_not_repeated(self, pf):
        assert pf.start() == []

    def test_fees_are_charged_per_order(self, pf, st, m):
        assert sum(t["fee_eur"] for t in st.trades()) == pytest.approx(len(st.trades()) * m.fee_per_order_eur)

    def test_committed_weights_match_the_model(self, pf, m):
        b = pf.book()
        for i in m.instruments:
            assert b.weight(i.ticker) == pytest.approx(i.weights.conservative, abs=0.002)

    def test_review_buys_due_tranches(self, pf, st):
        before = len(st.trades())
        pf.review(on="2027-03-01")          # after both tranches, before the deadline
        assert len(st.trades()) == before * 3
        assert st.tranches("planned") == []

    def test_the_watch_list_deadline_is_a_proposal_not_a_trade(self, pf, st, steward, m, monkeypatch):
        from council import approvals, scan
        pf.review(on="2027-03-01")                       # all tranches bought
        monkeypatch.setattr(scan, "now", lambda: "2099-01-01T00:00:00+00:00")
        before = len(st.trades())
        scan.daily(st, steward, m, pf)
        p = next(x for x in st.proposals("pending") if x["action"] == "deadline")
        assert len(st.trades()) == before                # nothing moved yet
        approvals.approve(st, pf, m, p["id"])
        assert any("deadline" in t["note"] for t in st.trades())
        assert st.get_meta("deadline_done")

    def test_the_benchmark_starts_at_the_capital(self, pf, m):
        assert pf.benchmark_value() == pytest.approx(m.start_capital_eur)

    def test_a_snapshot_is_written(self, pf, st):
        assert len(st.snapshots()) == 1


# ----------------------------------------------------------------- meetings

def run(st, steward, m, pf, ticker, backend=None):
    mt = meeting.Meeting(st, steward, m, pf, backend or agents.ScriptedBackend())
    return st.meeting(mt.run(ticker)), st.steps(st.meetings(1)[0]["id"])


class TestMeeting:
    def test_every_phase_is_recorded_in_order(self, st, steward, m, pf):
        mt, steps = run(st, steward, m, pf, "HWM")
        assert mt["status"] == "done"
        phases = [s["phase"] for s in steps]
        assert phases[:2] == ["gate", "facts"]
        assert phases.count("blind") == 5 and phases.count("cross") == 2
        assert phases.count("vote") == len(agents.VOTERS)
        assert phases.index("vote") < max(i for i, x in enumerate(phases) if x == "committee")  # CIO after the vote
        assert "committee" in phases and phases[-1] in ("risk", "trade")

    def test_a_rejected_name_never_reaches_an_agent(self, st, steward, m, pf):
        mt, steps = run(st, steward, m, pf, "SHA0.DE")
        assert mt["status"] == "stopped" and len(steps) == 1

    def test_a_verdict_waits_for_your_ok(self, st, steward, m, pf):
        from council import approvals
        before = len(st.trades())
        mt, steps = run(st, steward, m, pf, "TSM")
        assert mt["decision"]["decision"] == "add"
        assert len(st.trades()) == before                # nothing traded yet
        pid = mt["outcome"]["proposal"]
        assert st.proposal(pid)["status"] == "pending"
        approvals.approve(st, pf, m, pid)
        assert len(st.trades()) == before + 1            # first tranche only
        assert st.proposal(pid)["status"] == "approved"
        assert any(s["title"] == "You approved" for s in st.steps(mt["id"]))

    def test_the_red_team_runs_on_another_model(self):
        assert agents.ROLES["redteam"].model != agents.ROLES["thesis"].model

    def test_a_sell_without_a_breaker_is_blocked(self, st, steward, m, pf):
        class SellOnPrice(agents.ScriptedBackend):
            def _cio(self, c):
                d = super()._cio(c)
                return d.model_copy(update={"decision": "sell", "breaker_triggered": ""})
        before = len(st.trades())
        mt, _ = run(st, steward, m, pf, "CME", SellOnPrice())
        assert mt["outcome"]["allowed"] is False
        assert len(st.trades()) == before

    def test_the_budget_stops_an_expensive_meeting(self, st, steward, m, pf):
        class Expensive(agents.ScriptedBackend):
            def run(self, role, prompt, schema, context):
                r = super().run(role, prompt, schema, context)
                r.cost_usd = 5.0
                return r
        mt, _ = run(st, steward, m, pf, "HWM", Expensive())
        assert mt["status"] == "stopped" and "budget" in mt["error"]

    def test_a_refusal_stops_the_meeting_cleanly(self, st, steward, m, pf):
        class Refuses(agents.ScriptedBackend):
            def run(self, role, prompt, schema, context):
                raise agents.RoleRefused("declined")
        mt, _ = run(st, steward, m, pf, "HWM", Refuses())
        assert mt["status"] == "stopped"


# ------------------------------------------------------------ Claude wiring

class FakeClient:
    """Stands in for anthropic.Anthropic: records calls, returns canned replies."""

    def __init__(self, stop="end_turn"):
        self.calls = []
        self.stop = stop
        outer = self

        class Messages:
            def create(self, **kw):
                outer.calls.append(("create", kw))
                item = SimpleNamespace(url="https://example.com/q3", title="Q3 results", page_age="2026-10-30")
                return SimpleNamespace(
                    stop_reason="end_turn", model=kw["model"],
                    usage=SimpleNamespace(input_tokens=1000, output_tokens=500,
                                          server_tool_use=SimpleNamespace(web_search_requests=2)),
                    content=[SimpleNamespace(type="web_search_tool_result", content=[item]),
                             SimpleNamespace(type="text", text="- Revenue grew 40% (Q3, 30 Oct)")])

            def parse(self, **kw):
                outer.calls.append(("parse", kw))
                schema = kw["output_format"]
                ctx = {"task": "premortem", "name": "X", "prior": {}, "quote": {}, "ai": False}
                parsed = agents.ScriptedBackend()._premortem(ctx) if schema is agents.Premortem else None
                return SimpleNamespace(stop_reason=outer.stop, parsed_output=parsed, model=kw["model"],
                                       usage=SimpleNamespace(input_tokens=2000, output_tokens=800))

        self.beta = SimpleNamespace(messages=Messages())


class TestClaudeBackend:
    def test_research_then_a_structured_answer(self):
        fc = FakeClient()
        r = agents.ClaudeBackend(client=fc).run(agents.ROLES["redteam"], "facts", agents.Premortem, {})
        assert [c[0] for c in fc.calls] == ["create", "parse"]
        assert fc.calls[0][1]["tools"][0]["type"] == "web_search_20260209"
        assert fc.calls[1][1]["model"] == agents.RED_TEAM_MODEL
        assert r.sources[0].url == "https://example.com/q3"
        assert "Revenue grew" in fc.calls[1][1]["messages"][0]["content"]
        assert r.cost_usd > 0

    def test_roles_without_web_access_do_not_search(self):
        fc = FakeClient()
        with pytest.raises(agents.RoleRefused):          # parse returns None for this schema
            agents.ClaudeBackend(client=fc).run(agents.ROLES["risk"], "f", agents.RiskMemo, {})
        assert [c[0] for c in fc.calls] == ["parse"]

    def test_a_refusal_raises(self):
        fc = FakeClient(stop="refusal")
        with pytest.raises(agents.RoleRefused):
            agents.ClaudeBackend(client=fc).run(agents.ROLES["redteam"], "f", agents.Premortem, {})

    def test_fallbacks_are_on(self):
        fc = FakeClient()
        agents.ClaudeBackend(client=fc).run(agents.ROLES["redteam"], "f", agents.Premortem, {})
        assert all(c[1]["fallbacks"] == "default" for c in fc.calls)


# ---------------------------------------------------------------- interface

@pytest.fixture
def client(st, steward, m):
    from council import web
    app = web.create_app(store=st, steward=steward, mandate=m,
                         backend_factory=agents.ScriptedBackend, token="", run_async=False)
    app.config["TESTING"] = True
    return app.test_client()


class TestWeb:
    def test_the_first_visit_offers_to_open_the_portfolio(self, client):
        assert b"paper portfolio" in client.get("/").data

    def test_opening_it_shows_the_money_page(self, client, st):
        client.post("/start")
        page = client.get("/").data
        assert b"the index" in page and b"Convene the council" in page
        assert st.trades()

    def test_a_meeting_shows_the_whole_debate(self, client):
        client.post("/start")
        r = client.post("/meetings", data={"ticker": "tsm"})       # case-insensitive
        page = client.get(r.headers["Location"]).data
        for text in (b"Blind memos", b"Cross-examination", b"The verdict", b"Risk engine",
                     b"Proposed orders", b"Waiting for your OK", b"Fact-checker",
                     b"overlap and scenarios", b"Five-point chokepoint test"):
            assert text in page, text

    def test_the_live_endpoint_returns_the_debate(self, client):
        client.post("/start")
        client.post("/meetings", data={"ticker": "HWM"})
        d = client.get("/meetings/1/live").get_json()
        assert d["status"] == "done" and "Blind memos" in d["html"]

    def test_every_page_renders(self, client):
        client.post("/start")
        client.post("/meetings", data={"ticker": "SHA0.DE"})
        for path in ("/", "/meetings", "/meetings/1", "/portfolio", "/rules", "/how"):
            assert client.get(path).status_code == 200, path

    def test_actions_need_the_token_when_one_is_set(self, st, steward, m):
        from council import web
        app = web.create_app(store=st, steward=steward, mandate=m,
                             backend_factory=agents.ScriptedBackend, token="s3cret")
        c = app.test_client()
        assert c.post("/start").status_code == 403
        c.get("/?token=s3cret")
        assert c.post("/start").status_code == 302


def test_memo_text_cannot_inject_html():
    from council.web import md
    out = md('<script>alert(1)</script> **bold** [x](javascript:alert(1)) [ok](https://a.b)')
    assert "<script>" not in out and "javascript:" not in out.replace("[x](javascript:alert(1))", "")
    assert "<strong>bold</strong>" in out and 'href="https://a.b"' in out


class TestViews:
    def test_a_company_name_resolves_to_its_ticker(self, m):
        from council.web import resolve
        assert resolve("asml", m) == "ASML.AS"
        assert resolve("Air Liquide", m) == "AI.PA"
        assert resolve("tsm", m) == "TSM"
        assert resolve("GME", m) == "GME"           # unknown: the gate refuses it

    def test_scenario_impact_is_computed_by_code(self, st, steward, m, pf):
        from council import views
        mt, _ = run(st, steward, m, pf, "TSM")
        v = views.debate(st, m, mt["id"])
        row = next(r for r in v["scen"]["rows"] if r["name"] == "Taiwan crisis")
        assert row["impact"] == pytest.approx(row["move"] * v["scen"]["size"])

    def test_the_statement_ends_at_the_cash_balance(self, pf, st, m):
        from council import views
        rows = views.statement(st, m.start_capital_eur, m)
        assert rows[-1]["desc"] == "Opening paper deposit"
        assert rows[0]["cash"] == pytest.approx(pf.cash())

    def test_every_limit_has_a_status(self, pf, st, m):
        from council import views
        rows = views.limits(pf.book(), m, st)
        assert len(rows) == 8 and all(r["status"] in ("ok", "close", "over") for r in rows)

    def test_the_meetings_filter(self, client):
        client.post("/start")
        client.post("/meetings", data={"ticker": "TSM"})
        client.post("/meetings", data={"ticker": "HWM"})
        page = client.get("/meetings?f=watch").data
        assert b"Howmet" in page and b"TSMC" not in page.split(b"<table>")[1]


# ---------------------------------------------------------------- approvals

class TestApprovals:
    def proposal(self, st, steward, m, pf, ticker="TSM"):
        mt, _ = run(st, steward, m, pf, ticker)
        return mt["outcome"]["proposal"]

    def test_reject_trades_nothing(self, st, steward, m, pf):
        from council import approvals
        pid = self.proposal(st, steward, m, pf)
        before = len(st.trades())
        approvals.reject(st, pid)
        assert len(st.trades()) == before and st.proposal(pid)["status"] == "rejected"

    def test_a_proposal_is_answered_once(self, st, steward, m, pf):
        from council import approvals
        pid = self.proposal(st, steward, m, pf)
        approvals.approve(st, pf, m, pid)
        with pytest.raises(approvals.ApprovalError):
            approvals.approve(st, pf, m, pid)

    def test_an_expired_proposal_cannot_be_approved(self, st, steward, m, pf, monkeypatch):
        from council import approvals
        pid = self.proposal(st, steward, m, pf)
        monkeypatch.setattr(approvals, "now", lambda: "2999-01-01T00:00:00+00:00")
        with pytest.raises(approvals.ApprovalError):
            approvals.approve(st, pf, m, pid)
        assert st.proposal(pid)["status"] == "expired"

    def test_approval_rechecks_the_rules_at_todays_book(self, st, steward, m, pf):
        from council import approvals
        pid = self.proposal(st, steward, m, pf)           # TSMC add, allowed now
        st.plan_tranche("TSM", "2099-01-01", 2000.0)      # now TSMC is far over its cap
        with pytest.raises(approvals.ApprovalError):
            approvals.approve(st, pf, m, pid)
        assert st.proposal(pid)["status"] == "failed"


# ------------------------------------------------------ the CIO's own rules

class TestPolicy:
    def q(self, steward, t):
        return steward.quote(t)

    def test_a_grade_c_cannot_be_bought(self, m, steward):
        checks, _ = risk.policy("IFX.DE", {"grade": "C", "conviction": "starter"}, m, self.q(steward, "IFX.DE"))
        assert not checks[0].ok

    def test_a_b_name_waits_for_its_trigger(self, m, steward):
        q = self.q(steward, "HWM")                        # trigger fwd P/E 30
        checks, _ = risk.policy("HWM", {"grade": "B", "conviction": "starter"}, m, q)
        trig = next(c for c in checks if c.name == "Trigger reached")
        assert trig.ok == (q.forward_pe <= 30)

    def test_core_needs_grade_a(self, m, steward):
        _, conv = risk.policy("CME", {"grade": "B", "conviction": "core"}, m, self.q(steward, "CME"))
        assert conv == "starter"


# --------------------------------------------------------------------- scan

class TestScan:
    def test_hit_triggers_come_first(self, st, steward, m, pf):
        from council import scan
        plan = scan.agenda(st, pf, m, steward.quotes(pf.tickers_needed()))
        ranks = [c["rank"] for c in plan["picked"]]
        assert ranks == sorted(ranks)
        assert all(c["kind"] in ("idea", "holding") for c in plan["picked"])

    def test_the_monthly_caps_hold(self, st, steward, m, pf):
        from council import scan
        plan = scan.agenda(st, pf, m, steward.quotes(pf.tickers_needed()))
        ideas = [c for c in plan["picked"] if c["kind"] == "idea"]
        holds = [c for c in plan["picked"] if c["kind"] == "holding"]
        assert len(ideas) <= m.limits.max_meetings_per_month
        assert len(holds) <= m.limits.max_holding_reviews_per_month

    def test_a_name_just_discussed_is_skipped(self, st, steward, m, pf):
        from council import scan
        first = scan.agenda(st, pf, m, steward.quotes(pf.tickers_needed()))["picked"][0]
        run(st, steward, m, pf, first["ticker"])
        again = scan.agenda(st, pf, m, steward.quotes(pf.tickers_needed()))
        assert first["ticker"] not in [c["ticker"] for c in again["picked"]]

    def test_daily_runs_meetings_but_trades_nothing_new(self, st, steward, m, pf):
        from council import scan
        before = len(st.trades())
        scan.daily(st, steward, m, pf, agents.ScriptedBackend())
        assert len(st.trades()) == before                 # tranches not due yet, rest waits for you


# ------------------------------------------------------------------- broker

class FakeIB:
    """Stands in for ib_async: records the order and fills it."""
    def __init__(self, fill=True):
        self.orders, self.fill = [], fill
        outer = self

        class Trade:
            def __init__(self, order):
                self.order = order
                self.orderStatus = SimpleNamespace(filled=order.totalQuantity if outer.fill else 0,
                                                   avgFillPrice=order.lmtPrice)
                self.fills = [SimpleNamespace(commissionReport=SimpleNamespace(commission=1.25))] if outer.fill else []
            def isDone(self):
                return outer.fill
        self.Trade = Trade
        self.cancelled = []

    # ib_async module surface
    def Stock(self, sym, exch, ccy):
        return SimpleNamespace(symbol=sym, exchange=exch, currency=ccy)
    def LimitOrder(self, action, qty, price, tif="DAY"):
        return SimpleNamespace(action=action, totalQuantity=qty, lmtPrice=price, orderId=7)
    # IB client surface
    def placeOrder(self, contract, order):
        self.orders.append((contract, order))
        return self.Trade(order)
    def sleep(self, s):
        pass
    def cancelOrder(self, order):
        self.cancelled.append(order)


class TestBroker:
    def test_paper_is_the_default(self, m):
        from council.broker import PaperBroker, make_broker
        assert isinstance(make_broker(m), PaperBroker)

    def test_live_money_needs_the_phrase(self):
        r = raw()
        r["broker"] = {"kind": "ibkr", "live": True}
        with pytest.raises(Exception):
            mandate.Mandate.model_validate(r)

    def test_ibkr_sends_a_limit_order_at_the_last_price(self, m, steward):
        from council.broker import IBKRBroker
        fake = FakeIB()
        b = IBKRBroker(m.broker, m, ib=fake, lib=fake)
        q = steward.quote("ASML.AS")
        f = b.execute("ASML.AS", "buy", 0.5, q)
        contract, order = fake.orders[0]
        assert (contract.symbol, contract.exchange, contract.currency) == ("ASML", "AEB", "EUR")
        assert order.action == "BUY" and order.lmtPrice == round(q.price, 4)
        assert f.qty == 0.5 and f.broker == "ibkr"

    def test_an_unfilled_order_is_cancelled_and_raises(self, m, steward):
        from council.broker import BrokerError, IBKRBroker
        fake = FakeIB(fill=False)
        b = IBKRBroker(m.broker, m, ib=fake, lib=fake, wait_seconds=2)
        with pytest.raises(BrokerError):
            b.execute("TSM", "buy", 1, steward.quote("TSM"))
        assert fake.cancelled


class TestWebApprovals:
    def test_approve_from_the_money_page(self, client, st):
        client.post("/start")
        client.post("/meetings", data={"ticker": "TSM"})
        pid = st.proposals("pending")[0]["id"]
        assert b"Waiting for your OK" in client.get("/").data
        before = len(st.trades())
        client.post(f"/proposals/{pid}/approve")
        assert len(st.trades()) == before + 1
        assert b"The council proposes" not in client.get("/").data

    def test_reject_from_the_meeting_page(self, client, st):
        client.post("/start")
        client.post("/meetings", data={"ticker": "TSM"})
        pid = st.proposals("pending")[0]["id"]
        client.post(f"/proposals/{pid}/reject", data={"next": "/meetings/1"})
        assert st.proposal(pid)["status"] == "rejected"
        assert b"You said no" in client.get("/meetings/1").data

    def test_let_the_council_choose(self, client, st):
        client.post("/start")
        client.post("/scan")
        assert st.meetings(50)                            # it met on its own agenda
        assert st.get_meta("last_scan")["picked"]


def test_holdings_are_not_reviewed_the_day_they_are_bought(st, steward, m, pf):
    from council import scan
    plan = scan.agenda(st, pf, m, steward.quotes(pf.tickers_needed()))
    assert not [c for c in plan["picked"] if c["reason"].startswith("Quarterly review")]


def test_a_holding_at_its_add_level_is_reviewed_now(st, steward, m, pf):
    from council import scan
    q = steward.quote("ASML.AS")
    plan = scan.agenda(st, pf, m, steward.quotes(pf.tickers_needed()))
    asml = [c for c in plan["picked"] if c["ticker"] == "ASML.AS"]
    assert bool(asml) == (q.price_eur <= 1450)
    if asml:
        assert asml[0]["kind"] == "holding"


# ------------------------------------------- what structured output allows

class TestAnswerFormats:
    NAMES = ["MacroMemo", "ThesisMemo", "ValuationMemo", "Premortem", "Objections",
             "Rebuttal", "FactCheck", "RiskMemo", "CIODecision", "EarningsMemo", "Vote"]

    def test_no_free_form_fields_reach_the_api(self):
        """Structured output forces additionalProperties: false on every object,
        so a dict field would always come back empty."""
        import json
        from anthropic.lib._parse._transform import transform_schema
        for name in self.NAMES:
            sch = json.dumps(transform_schema(getattr(agents, name).model_json_schema()))
            assert '"additionalProperties": {' not in sch, name

    def test_the_macro_odds_have_fixed_fields(self):
        props = agents.ScenarioOdds.model_json_schema()["properties"]
        assert set(props) == {"peace", "stagflation", "ai_bust", "rate_cuts", "recession"}

    def test_an_overlong_answer_is_trimmed_not_rejected(self):
        raw = {"objections": [{"rank": r, "claim": f"c{r}", "evidence": "e", "test": "t"}
                              for r in (4, 2, 1, 3)]}
        out = agents.tidy(agents.Objections.model_validate(raw))
        assert [o.rank for o in out.objections] == [1, 2, 3]
        assert [o.claim for o in out.objections] == ["c1", "c2", "c3"]

    def test_percentages_are_clamped(self):
        risk_raw = {"scenarios": [{"scenario": "x", "stock_move_pct": -300}] * 8,
                    "overlap": "o", "recommendation": "proceed", "memo": "m"}
        out = agents.tidy(agents.RiskMemo.model_validate(risk_raw))
        assert len(out.scenarios) == 6 and out.scenarios[0].stock_move_pct == -100



# ----------------------------------------------------------------- learning

def age_decision(st, mid, days, start_factor):
    """Pretend decision #mid was made `days` ago at `start_factor` x today's price."""
    import sqlite3
    from datetime import date, timedelta
    day = (date.today() - timedelta(days=days)).isoformat()
    c = sqlite3.connect(st.path)
    c.execute("UPDATE marks SET day=?, price_eur=price_eur*? WHERE meeting_id=? AND horizon_days=0",
              (day, start_factor, mid))
    c.commit()
    c.close()


class TestLearning:
    def test_every_decision_is_marked_on_its_day(self, st, steward, m, pf):
        mt, _ = run(st, steward, m, pf, "HWM")
        assert [x["horizon_days"] for x in st.marks(mt["id"])] == [0]

    def test_marks_come_due_as_time_passes(self, st, steward, m, pf):
        from council import learning
        mt, _ = run(st, steward, m, pf, "TSM")
        age_decision(st, mt["id"], 100, 1.0)
        assert learning.update_marks(st, steward, m) == 2          # 1 and 3 months
        assert {x["horizon_days"] for x in st.marks(mt["id"])} == {0, 30, 91}

    def test_judging(self):
        from council.learning import judge
        assert judge("buy", 8) == "right" and judge("buy", -8) == "wrong"
        assert judge("watch", -8) == "right" and judge("reject", 25) == "wrong"
        assert judge("buy", 1) == "noise"

    def test_a_bad_buy_gets_a_post_mortem(self, st, steward, m, pf):
        from council import learning
        mt, _ = run(st, steward, m, pf, "TSM")                    # an add
        age_decision(st, mt["id"], 100, 1.3)                      # it has fallen ~23% since
        learning.update_marks(st, steward, m)
        due = learning.due_postmortems(st, m)
        assert [c["meeting_id"] for c in due] == [mt["id"]]

    def test_a_refused_winner_gets_one_too(self, st, steward, m, pf):
        from council import learning
        mt, _ = run(st, steward, m, pf, "HWM")                    # watch
        age_decision(st, mt["id"], 200, 0.7)                      # it has risen ~43% since
        learning.update_marks(st, steward, m)
        assert learning.due_postmortems(st, m)[0]["reason"].startswith("Refused")

    def test_a_post_mortem_reads_what_was_said_at_the_time(self, st, steward, m, pf):
        from council import learning
        mt, _ = run(st, steward, m, pf, "TSM")
        age_decision(st, mt["id"], 100, 1.3)
        learning.update_marks(st, steward, m)
        case = learning.due_postmortems(st, m)[0]
        pid = learning.run_postmortem(st, steward, m, case, agents.ScriptedBackend())
        pm = st.postmortem(pid)
        assert pm["status"] == "done"
        assert "objection 1" in pm["data"]["output"]["foreseen_by"]
        assert learning.due_postmortems(st, m) == []              # examined once

    def test_only_approved_lessons_reach_the_members(self, st, steward, m, pf):
        from council import meeting as meeting_mod
        a = st.add_lesson(None, "Check customer concentration before buying a supplier.")
        st.add_lesson(None, "A lesson nobody approved.")
        st.decide_lesson(a, "approved")
        mt = meeting_mod.Meeting(st, steward, m, pf, agents.ScriptedBackend())
        text = mt.facts_md(mt.fact_sheet("HWM"))
        assert "customer concentration" in text and "nobody approved" not in text

    def test_past_calls_are_in_the_next_fact_sheet(self, st, steward, m, pf):
        from council import learning, meeting as meeting_mod
        first, _ = run(st, steward, m, pf, "TSM")
        age_decision(st, first["id"], 100, 1.0)
        learning.update_marks(st, steward, m)
        mt = meeting_mod.Meeting(st, steward, m, pf, agents.ScriptedBackend())
        assert "past calls on this name" in mt.facts_md(mt.fact_sheet("TSM"))

    def test_the_scorecard(self, st, steward, m, pf):
        from council import learning
        a, _ = run(st, steward, m, pf, "TSM")
        b, _ = run(st, steward, m, pf, "HWM")
        age_decision(st, a["id"], 100, 0.8)                       # bought, then rose: right
        age_decision(st, b["id"], 100, 0.8)                       # refused, then rose: wrong
        learning.update_marks(st, steward, m)
        sc = learning.scorecard(st, m)
        assert sc["judged"] == 2 and sc["right"] == 1 and not sc["enough"]
        assert sc["thesis_brier"] is not None

    def test_the_scorecard_page(self, client, st):
        client.post("/start")
        client.post("/meetings", data={"ticker": "TSM"})
        lid = st.add_lesson(None, "Read the debate before approving.")
        page = client.get("/scorecard").data
        assert b"Is the council any good?" in page and b"Read the debate" in page
        client.post(f"/lessons/{lid}/approve")
        assert st.lessons("approved")



# ---------------------------------------------------------------- autopilot

@pytest.fixture
def auto_m():
    r = raw()
    r["autopilot"] = True
    return mandate.Mandate.model_validate(r)


class TestAutopilot:
    def test_it_approves_its_own_proposals(self, tmp_path, steward, auto_m):
        st = store.Store(tmp_path / "auto.db")
        pf = portfolio.Portfolio(st, steward, auto_m)
        pf.start()
        before = len(st.trades())
        mt, steps = run(st, steward, auto_m, pf, "TSM")
        assert len(st.trades()) == before + 1                   # traded without anyone
        p = st.proposals(meeting_id=mt["id"])[0]
        assert p["status"] == "approved" and p["result"]["by"] == "autopilot"
        assert any(s["title"] == "Approved by the autopilot" for s in steps)

    def test_it_still_obeys_the_risk_engine(self, tmp_path, steward, auto_m):
        st = store.Store(tmp_path / "auto.db")
        pf = portfolio.Portfolio(st, steward, auto_m)
        pf.start()
        before = len(st.trades())
        run(st, steward, auto_m, pf, "HWM")                     # an 11th stock: blocked
        assert len(st.trades()) == before

    def test_it_can_never_trade_real_money(self):
        r = raw()
        r["autopilot"] = True
        r["broker"] = {"kind": "ibkr"}
        with pytest.raises(Exception):
            mandate.Mandate.model_validate(r)

    def test_the_autopilot_book_gets_its_own_rules_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRADEBUNNY_HOME", str(tmp_path))
        from council import paths
        path = mandate.initialise(book="autopilot")
        assert path.parent.name == "council-autopilot"
        assert mandate.load(path).autopilot is True
        assert paths.db_path("autopilot") != paths.db_path("main")

    def test_both_books_in_one_app(self, tmp_path, monkeypatch, steward):
        monkeypatch.setenv("TRADEBUNNY_HOME", str(tmp_path))
        from council import web
        app = web.create_books(steward=steward, backend_factory=agents.ScriptedBackend, token="")
        c = app.test_client()
        c.post("/start")
        c.post("/autopilot/start")
        assert b"Meanwhile, on autopilot" in c.get("/").data
        page = c.get("/autopilot/").data
        assert b"approves its own proposals" in page and b"Meanwhile, with you approving" in page


# --------------------------------------------------------------- stock page

class TestStockPage:
    def test_a_held_stock_tells_its_whole_story(self, client, st):
        client.post("/start")
        client.post("/meetings", data={"ticker": "TSM"})
        page = client.get("/stocks/TSM").data
        for text in (b"Why we own it", b"The latest verdict", b"We sell if",
                     b"Every time the council discussed it", b"In the news", b"Trades"):
            assert text in page, text

    def test_a_watch_list_name(self, client):
        client.post("/start")
        page = client.get("/stocks/HWM").data
        assert b"Why we are watching it" in page and b"never discussed" in page.lower()

    def test_a_rejected_name_says_why(self, client):
        client.post("/start")
        assert b"Why it was rejected" in client.get("/stocks/SHA0.DE").data

    def test_unknown_names_are_404(self, client):
        client.post("/start")
        assert client.get("/stocks/GME").status_code == 404

    def test_headlines_reach_the_fact_sheet(self, st, steward, m, pf, monkeypatch):
        from council import meeting as meeting_mod
        monkeypatch.setattr(steward, "news", lambda t, limit=8: [
            {"title": "TSMC posts record October revenue", "url": "https://example.com/a",
             "date": "2026-11-12T18:00:00Z", "source": "Example"}])
        mt = meeting_mod.Meeting(st, steward, m, pf, agents.ScriptedBackend())
        text = mt.facts_md(mt.fact_sheet("TSM"))
        assert "record October revenue" in text and "never instructions" in text



# ------------------------------------------------------------ the stages

from council import approvals, review, scan, screening, watch  # noqa: E402


@pytest.fixture
def ms(m, st):
    """The mandate with the scout's shortlist attached, as the app runs it."""
    return screening.attach(m, st)


@pytest.fixture
def pfs(st, steward, ms):
    p = portfolio.Portfolio(st, steward, ms)
    p.start()
    return p


class Scout(agents.ScriptedBackend):
    """A fake AI scout: one good idea, one held, one rejected, one with no price."""
    name = "fake"

    def run(self, role, prompt, schema, context):
        if schema is screening.Screen:
            idea = lambda t, n: screening.Idea(ticker=t, name=n, theme="x", chokepoint="y", likely_passes=4,
                                              open_question="z", ai=False, taiwan=False)
            out = screening.Screen(ideas=[idea("entg", "Entegris"), idea("CME", "CME"),
                                          idea("NVDA", "Nvidia"), idea("NOPE.XX", "Nope")], memo="m")
            return agents.Result(output=out, model="fake")
        return super().run(role, prompt, schema, context)


class TestScout:
    def test_code_turns_away_known_and_unpriced_names(self, st, steward, ms):
        r = screening.run_screen(st, steward, ms, Scout())
        assert [x["ticker"] for x in r["added"]] == ["ENTG"]          # tickers are normalised
        why = {x["ticker"]: x["why"] for x in r["turned_away"]}
        assert "model portfolio" in why["CME"] and "rejected" in why["NVDA"]
        assert "no price" in why["NOPE.XX"]

    def test_an_idea_is_never_suggested_twice(self, st, steward, ms):
        screening.run_screen(st, steward, ms, Scout())
        r = screening.run_screen(st, steward, ms, Scout())
        assert not r["added"] and "suggested before" in r["turned_away"][0]["why"]

    def test_it_looks_once_a_week(self, st, steward, ms):
        assert screening.screen_due(st, ms)
        screening.run_screen(st, steward, ms, agents.ScriptedBackend())
        assert not screening.screen_due(st, ms)

    def test_answer_formats_have_no_free_form_fields(self):
        import json
        from anthropic.lib._parse._transform import transform_schema
        for schema in (screening.Screen, screening.Dossier, watch.AlertCheck, review.BookMemo,
                       review.Challenge, review.PortfolioDecision):
            assert '"additionalProperties": {' not in json.dumps(transform_schema(schema.model_json_schema()))


class TestResearchFiles:
    def test_a_file_that_says_send_shortlists_the_idea(self, st, steward, ms):
        b = agents.ScriptedBackend()
        screening.run_screen(st, steward, ms, b)
        assert "BESI.AS" not in ms.universe()                        # an idea is not yet debatable
        screening.research_new_ideas(st, steward, ms, b)
        assert st.candidate("BESI.AS")["status"] == "shortlisted"
        assert "BESI.AS" in ms.universe() and risk.gate("BESI.AS", ms, 0)[0].ok

    def test_dropping_an_idea_takes_it_out_of_the_universe(self, st, steward, ms):
        b = agents.ScriptedBackend()
        screening.run_screen(st, steward, ms, b)
        screening.research_new_ideas(st, steward, ms, b)
        assert screening.drop(st, "BESI.AS")
        assert "BESI.AS" not in ms.universe()

    def test_the_weekly_budget(self, st, steward, ms):
        b = agents.ScriptedBackend()
        for t in ("CME", "VMC", "MET", "ALV.DE"):
            screening.write_dossier(st, steward, ms, t, b)
        assert screening.research_left(st, ms) == 0
        assert not screening.needs_research(st, ms, "CME") and screening.needs_research(st, ms, "PRY.MI")

    def test_the_committee_reads_the_file(self, st, steward, ms, pfs):
        screening.write_dossier(st, steward, ms, "TSM", agents.ScriptedBackend())
        mt = meeting.Meeting(st, steward, ms, pfs, agents.ScriptedBackend())
        assert "## The research file" in mt.facts_md(mt.fact_sheet("TSM"))

    def test_a_failure_is_recorded_not_raised(self, st, steward, ms):
        class Broken(agents.ScriptedBackend):
            name = "fake"
            def run(self, *a, **k):
                raise RuntimeError("API down")
        d = st.dossier(screening.write_dossier(st, steward, ms, "TSM", Broken()))
        assert d["status"] == "failed" and "API down" in d["data"]["error"]

    def test_shortlisted_ideas_join_the_agenda(self, st, steward, ms, pfs):
        b = agents.ScriptedBackend()
        screening.run_screen(st, steward, ms, b)
        screening.research_new_ideas(st, steward, ms, b)
        plan = scan.agenda(st, pfs, ms, steward.quotes(pfs.tickers_needed()))
        assert any(c["ticker"] == "BESI.AS" and "scout" in c["reason"] for c in plan["picked"])


class TestCommittee:
    def test_the_cio_answers_the_members_who_disagree(self, st, steward, m, pf):
        mt, steps = run(st, steward, m, pf, "HWM")
        votes = [s for s in steps if s["phase"] == "vote"]
        assert {s["role"] for s in votes} == set(agents.VOTERS)
        assert mt["decision"]["dissent_answer"]

    def test_votes_and_the_risk_officer_cost_no_ai(self, st, steward, m, pf):
        mt, steps = run(st, steward, m, pf, "HWM")
        coded_steps = [s for s in steps if s["phase"] == "vote" or (s["role"] == "risk" and s["phase"] == "committee")]
        assert coded_steps and all(s["data"]["model"] == "code" for s in coded_steps)


def move(steward, ticker, change):
    """Pretend `ticker` moved by `change` today (e.g. -0.15)."""
    from dataclasses import replace as dc_replace
    exp, q = steward._cache[ticker]
    steward._cache[ticker] = (exp, dc_replace(q, prev_close=q.price / (1 + change)))


class Convene(agents.ScriptedBackend):
    name = "fake"

    def run(self, role, prompt, schema, context):
        if schema is watch.AlertCheck:
            return agents.Result(output=watch.AlertCheck(what_happened="w", breaker_fired="b",
                                                         verdict="convene", memo="m"), model="fake")
        return super().run(role, prompt, schema, context)


class TestWatch:
    def test_a_big_fall_raises_one_alert(self, st, steward, ms, pfs):
        move(steward, "CME", -0.15)
        assert len(watch.find_events(st, steward, ms, pfs)) == 1
        assert watch.find_events(st, steward, ms, pfs) == []            # never twice
        assert st.alerts()[0]["kind"] == "drop"

    def test_a_small_move_is_ignored(self, st, steward, ms, pfs):
        move(steward, "CME", -0.05)
        assert watch.find_events(st, steward, ms, pfs) == []

    def test_results_days_raise_an_alert(self, st, steward, ms, pfs, monkeypatch):
        from datetime import date
        today = date.today().isoformat()
        monkeypatch.setattr(steward, "earnings_dates", lambda t: [today] if t == "ASML.AS" else [])
        ids = watch.find_events(st, steward, ms, pfs)
        assert [st.alert(i)["ticker"] for i in ids] == ["ASML.AS"]

    def test_without_a_key_it_is_recorded_not_judged(self, st, steward, ms, pfs):
        move(steward, "CME", -0.15)
        events = watch.run(st, steward, ms, pfs, None)
        assert "needs the AI key" in events[0] and st.alerts()[0]["status"] == "open"

    def test_convene_calls_an_urgent_review_but_never_sells_by_itself(self, st, steward, ms, pfs):
        move(steward, "CME", -0.15)
        before = len(st.trades())
        events = watch.run(st, steward, ms, pfs, Convene())
        a = st.alerts()[0]
        assert a["status"] == "convene" and a["meeting_id"]
        assert st.meeting(a["meeting_id"])["kind"] == "holding" and "urgent review" in events[0]
        assert len(st.trades()) == before


class BuyAll(agents.ScriptedBackend):
    def _cio(self, c):
        return super()._cio(c).model_copy(update={"decision": "buy", "grade": "A", "conviction": "core"})


class MarkDown(agents.ScriptedBackend):
    def _cio(self, c):
        return super()._cio(c).model_copy(update={"decision": "hold", "grade": "C", "conviction": "none"})


class TestPortfolioMeeting:
    def test_it_ranks_the_whole_book(self, st, steward, ms, pfs):
        mid = review.PortfolioMeeting(st, steward, ms, pfs, agents.ScriptedBackend()).run()
        mt = st.meeting(mid)
        assert mt["status"] == "done" and mt["kind"] == "portfolio" and mt["decision"]["decision"] == "keep"
        cio = next(s for s in st.steps(mid) if s["role"] == "cio")
        assert len(cio["data"]["output"]["ranking"]) == risk.stock_count(pfs.book(), ms)
        assert len(st.get_meta("review_queue")) <= review.MAX_QUEUE
        assert not review.due(st)

    def test_a_blocked_buy_waits_on_the_bench(self, st, steward, ms, pfs):
        run(st, steward, ms, pfs, "HWM", BuyAll())                    # 10 of 10: blocked
        bench = review.bench(st, steward, ms, pfs)
        assert [b["ticker"] for b in bench] == ["HWM"]

    def test_a_swap_pairs_two_committee_verdicts(self, st, steward, ms, pfs):
        run(st, steward, ms, pfs, "HWM", BuyAll())
        run(st, steward, ms, pfs, "CME", MarkDown())
        mid = review.PortfolioMeeting(st, steward, ms, pfs, agents.ScriptedBackend()).run()
        o = st.meeting(mid)["outcome"]
        p = st.proposal(o["proposal"])
        assert p["action"] == "swap" and p["data"]["sell"] == "CME" and p["data"]["buy"] == "HWM"
        assert p["status"] == "pending"                               # waits for you
        approvals.approve(st, pfs, ms, p["id"])
        q = pfs.quantities()
        assert q.get("CME", 0) <= 1e-9 and q["HWM"] > 0
        assert risk.stock_count(pfs.book(), ms) == 10
        assert any(t["ticker"] == "HWM" for t in st.tranches("planned"))

    def test_a_holding_not_marked_down_is_reviewed_first(self, st, steward, ms, pfs):
        run(st, steward, ms, pfs, "HWM", BuyAll())

        class WantsSwap(agents.ScriptedBackend):
            name = "fake"
            def run(self, role, prompt, schema, context):
                if schema is review.PortfolioDecision:
                    out = review.PortfolioDecision(ranking=[], swap_sell="VMC", swap_buy="HWM",
                                                   swap_reason="r", review_next=[], memo="m")
                    return agents.Result(output=out, model="fake")
                if schema is review.BookMemo:
                    return agents.Result(output=review._scripted_book(review.facts(st, steward, ms, pfs)))
                return agents.Result(output=review.Challenge(weakest=[], best_outside="none", memo="m"))
        mid = review.PortfolioMeeting(st, steward, ms, pfs, WantsSwap()).run()
        o = st.meeting(mid)["outcome"]
        assert not o["proposal"] and "VMC" in o["review_queue"]
        plan = scan.agenda(st, pfs, ms, steward.quotes(pfs.tickers_needed()))
        assert any(c["ticker"] == "VMC" for c in plan["picked"])

    def test_a_swap_that_breaks_the_rules_sells_nothing(self, st, steward, ms, pfs):
        run(st, steward, ms, pfs, "HWM", BuyAll())
        run(st, steward, ms, pfs, "CME", MarkDown())
        mid = review.PortfolioMeeting(st, steward, ms, pfs, agents.ScriptedBackend()).run()
        pid = st.meeting(mid)["outcome"]["proposal"]
        pfs.buy("WM", 400)                                           # an 11th stock appears meanwhile
        cme = pfs.quantities()["CME"]
        with pytest.raises(approvals.ApprovalError):
            approvals.approve(st, pfs, ms, pid)
        assert pfs.quantities()["CME"] == cme                        # nothing was sold


class TestDailyStages:
    def test_the_evening_runs_every_stage_once(self, st, steward, ms, pfs):
        b = agents.ScriptedBackend()
        events = scan.daily(st, steward, ms, pfs, b)
        text = " ".join(events)
        assert "Scout:" in text and "Research file on" in text and "Portfolio meeting #" in text
        again = " ".join(scan.daily(st, steward, ms, pfs, b))
        assert "Scout:" not in again and "Portfolio meeting #" not in again

    def test_without_a_key_only_bookkeeping_runs(self, st, steward, ms, pfs):
        events = " ".join(scan.daily(st, steward, ms, pfs, None))
        assert "Scout" not in events and not st.candidates()


class TestStagePages:
    @pytest.fixture
    def c(self, st, steward, m):
        from council import web
        app = web.create_app(store=st, steward=steward, mandate=m,
                             backend_factory=agents.ScriptedBackend, token="", run_async=False)
        app.config["TESTING"] = True
        cl = app.test_client()
        cl.post("/start")
        return cl

    def test_the_pipeline_and_its_buttons(self, c, st):
        assert b"From idea to trade" in c.get("/pipeline").data
        c.post("/screen")
        page = c.get("/pipeline").data
        assert b"BE Semiconductor" in page and b"Shortlisted" in page
        did = st.dossiers()[0]["id"]
        assert b"five-point test" in c.get(f"/research/{did}").data
        assert b"Why the scout suggested it" in c.get("/stocks/BESI.AS").data
        c.post("/candidates/BESI.AS/drop")
        assert st.candidate("BESI.AS")["status"] == "dropped"

    def test_the_portfolio_meeting_page(self, c, st):
        r = c.post("/portfolio-meeting")
        page = c.get(r.headers["Location"]).data
        assert b"every holding ranked" in page and b"Swap rules" in page
        assert b"Portfolio meeting" in c.get("/meetings").data

    def test_a_stock_meeting_shows_the_vote(self, c):
        r = c.post("/meetings", data={"ticker": "TSM"})
        page = c.get(r.headers["Location"]).data
        for text in (b"The vote", b"Earnings analyst", b"To the members who voted otherwise"):
            assert text in page, text

    def test_research_from_the_stock_page(self, c, st):
        c.post("/research", data={"ticker": "TSM"})
        assert st.latest_dossier("TSM") and b"The research file" in c.get("/stocks/TSM").data


# ------------------------------------------------------------ quant desk

from council import learning, quant  # noqa: E402


class TestQuantMaths:
    def test_reverse_dcf_finds_the_growth_it_was_given(self):
        ev = quant.dcf_value(100.0, 0.12)
        assert quant.implied_growth(ev, 100.0) == pytest.approx(0.12, abs=1e-6)

    def test_no_growth_answer_for_negative_cash_flow(self):
        assert quant.implied_growth(1000.0, -5.0) is None

    def test_drawdown_beta_and_var(self):
        assert quant.max_drawdown([100, 120, 60, 90]) == pytest.approx(-0.5)
        b = [0.01, -0.02, 0.015, -0.01, 0.02, -0.005] * 10
        assert quant.beta([2 * x for x in b], b) == pytest.approx(2.0)
        assert quant.var_95([0.01] * 30 + [-0.05] * 10) < 0

    def test_the_desk_reads_real_saved_numbers_offline(self, steward, st, m):
        q = quant.Desk(steward, st).stock("ASML.AS", m.benchmark)
        for k in ("vol_1y", "beta", "revenue_cagr", "op_margin", "pe_avg", "implied_growth"):
            assert q.get(k) is not None, k
        assert any("Reverse DCF" in n for n in q["notes"])

    def test_statements_in_another_currency_are_converted(self, steward, st, m):
        q = quant.Desk(steward, st).stock("TSM", m.benchmark)        # trades in USD, reports in TWD
        assert 5 < q["pe_now"] < 200

    def test_the_fact_sheet_carries_the_desk(self, st, steward, m, pf):
        mt = meeting.Meeting(st, steward, m, pf, agents.ScriptedBackend())
        text = mt.facts_md(mt.fact_sheet("IFX.DE"))
        assert "## The quant desk" in text and "reverse DCF" in text and "correlation with our portfolio" in text

    def test_the_portfolio_meeting_sees_book_risk(self, st, steward, m, pf):
        f = review.facts(st, steward, m, pf)
        assert f["book_vol"] and f["book_var_1m"] < 0
        assert "worst 5% of months" in review.facts_md(f, m)


class TestForecasts:
    def forecast(self, st, mid, metric, above, threshold, prob, role="thesis"):
        return st.add_prediction(meeting_id=mid, role=role, ticker="HWM", metric=metric, above=int(above),
                                 threshold=threshold, probability=prob)

    def test_index_forecasts_are_scored_from_the_marks(self, st, steward, m, pf):
        mt, _ = run(st, steward, m, pf, "HWM")
        self.forecast(st, mt["id"], "stock_vs_index_3m", True, 0, 70)
        age_decision(st, mt["id"], 100, 0.8)              # bought 100 days ago, 20% cheaper: it beat the index
        learning.update_marks(st, steward, m)
        assert learning.resolve_forecasts(st, steward, m) == 1
        p = st.predictions()[0]
        assert p["status"] == "hit" and p["actual"] > 0
        s = learning.forecast_scores(st)["thesis"]
        assert s["resolved"] == 1 and s["brier"] == pytest.approx(0.09)

    def test_open_forecasts_wait(self, st, steward, m, pf):
        mt, _ = run(st, steward, m, pf, "HWM")
        self.forecast(st, mt["id"], "stock_vs_index_12m", True, 0, 60)
        assert learning.resolve_forecasts(st, steward, m) == 0

    def test_the_cio_sees_a_record_only_once_it_means_something(self, st):
        for i in range(5):
            pid = self.forecast(st, 1, "stock_vs_index_3m", True, 0, 80)
            st.resolve_prediction(pid, "hit" if i < 4 else "miss", 1.0)
        assert "Thesis analyst: 5 forecasts resolved, 4 came true" in learning.forecast_record_text(st)
        assert learning.forecast_record_text(st, minimum=6) == ""

    def test_scripted_members_make_no_forecasts(self, st, steward, m, pf):
        run(st, steward, m, pf, "HWM")
        assert st.predictions() == []

    def test_forecasts_are_clamped(self):
        raw = {"metric": "op_margin_next_q", "above": True, "threshold": 20, "probability": 100, "why": "w"}
        memo = agents.ScriptedBackend()._premortem({"name": "X", "prior": {}})
        memo.predictions = [agents.Prediction.model_validate(raw)] * 5
        out = agents.tidy(memo)
        assert len(out.predictions) == 3 and out.predictions[0].probability == 99


class TestSecondVerdict:
    def test_a_split_verdict_takes_the_cautious_side(self, st, steward, m, pf):
        class Split(agents.ScriptedBackend):
            name = "fake"
            n = 0
            def _cio(self, c):
                Split.n += 1
                d = super()._cio(c)
                return d.model_copy(update={"decision": "buy" if Split.n == 1 else "watch",
                                            "grade": "A", "conviction": "core"})
        mt, _ = run(st, steward, m, pf, "HWM", Split())
        assert Split.n == 2 and mt["decision"]["decision"] == "watch"
        assert "Two independent verdicts disagreed" in mt["decision"]["rationale"]

    def test_the_fact_sheet_is_cached_for_every_member(self):
        role = agents.ROLES["thesis"]
        system, prompt = agents.ClaudeBackend.split(role, "FACTS\n\nAsk", {"shared": "FACTS"})
        assert system[0]["cache_control"] and "FACTS" in system[0]["text"] and system[1]["text"] == role.system
        assert "FACTS" not in prompt and prompt.endswith("Ask")
        assert agents.ClaudeBackend.split(role, "other", {"shared": "FACTS"}) == (role.system, "other")


class TestYourCalls:
    def test_your_reason_is_kept_and_scored(self, st, steward, m, pf):
        from council import approvals
        mt, _ = run(st, steward, m, pf, "TSM")
        approvals.approve(st, pf, m, mt["outcome"]["proposal"], why="TSMC is the only 2nm foundry")
        calls = learning.your_calls(st, m)
        assert calls[0]["why"] == "TSMC is the only 2nm foundry" and calls[0]["verdict"] == "pending"
        assert any("Your reason" in s["body"] for s in st.steps(mt["id"]))

    def test_reject_with_a_reason_from_the_web(self, client, st):
        client.post("/start")
        r = client.post("/meetings", data={"ticker": "TSM"})
        pid = st.proposals("pending")[0]["id"]
        client.post(f"/proposals/{pid}/reject", data={"why": "Taiwan risk is too high for me"})
        assert st.proposal(pid)["result"]["why"] == "Taiwan risk is too high for me"
        assert b"Your calls" in client.get("/scorecard").data


class TestRulesOnlyBook:
    def test_it_must_approve_itself(self):
        r = raw()
        r["rules_only"] = True
        with pytest.raises(Exception):
            mandate.Mandate.model_validate(r)
        r["autopilot"] = True
        assert mandate.Mandate.model_validate(r).rules_only

    def test_it_never_runs_the_ai_stages(self, st, steward, m, pf):
        rm = m.model_copy(update={"autopilot": True, "rules_only": True})
        events = " ".join(scan.daily(st, steward, rm, pf, agents.ScriptedBackend(), stages_on=False))
        assert "Scout" not in events and "Portfolio meeting" not in events and not st.candidates()
        assert "Meeting #" in events                     # but it does meet, by the rules

    def test_the_web_refuses_ai_stages_and_uses_no_ai(self, st, steward, m):
        from council import web
        rm = m.model_copy(update={"autopilot": True, "rules_only": True})
        app = web.create_app(store=st, steward=steward, mandate=rm, backend_factory=lambda: (_ for _ in ()).throw(
            AssertionError("no AI here")), token="", run_async=False, book="rules")
        c = app.test_client()
        c.post("/start")
        c.post("/screen")
        assert not st.candidates()
        assert b"Rules only" in c.get("/").data

    def test_three_books_in_one_app(self, tmp_path, monkeypatch, steward):
        from council import web
        monkeypatch.setenv("TRADEBUNNY_HOME", str(tmp_path))
        app = web.create_books(steward=steward)
        c = app.test_client()
        for url in ("/", "/autopilot/", "/rules-only/"):
            assert c.get(url).status_code == 200, url


def test_the_cli_parses_every_command():
    from council import cli
    for argv in (["research", "TSM"], ["screen"], ["watch"], ["portfolio-meeting"], ["--book", "rules", "daily"]):
        assert cli.build_parser().parse_args(argv)


# ------------------------------------------------- AI only where needed

class Counting(agents.ScriptedBackend):
    """Pretends to be a real AI (name 'fake') and counts every call by task."""
    name = "fake"

    def __init__(self):
        self.calls = []

    def run(self, role, prompt, schema, context):
        self.calls.append((context.get("task"), context.get("research", True)))
        res = super().run(role, prompt, schema, context)
        res.cost_usd = 0.10
        return res


class NoAI(agents.ScriptedBackend):
    name = "fake"

    def run(self, *a, **k):
        raise AssertionError("the AI was called")


class TestAIOnlyWhereNeeded:
    def test_a_meeting_calls_the_ai_only_for_judgement(self, st, steward, m, pf):
        b = Counting()
        run(st, steward, m, pf, "HWM", b)
        tasks = [t for t, _ in b.calls]
        assert "vote" not in tasks and "risk" not in tasks                # code
        assert sorted(tasks) == sorted(["macro", "thesis", "valuation", "earnings", "premortem",
                                        "objections", "rebuttal", "factcheck", "cio"])
        assert all(not r for t, r in b.calls if t in ("objections", "rebuttal"))   # no second web search

    def test_the_macro_memo_is_reused_for_a_week(self, st, steward, m, pf):
        b = Counting()
        run(st, steward, m, pf, "HWM", b)
        b.calls.clear()
        mt, steps = run(st, steward, m, pf, "WM", b)
        assert "macro" not in [t for t, _ in b.calls]
        macro = next(s for s in steps if s["role"] == "macro")
        assert "reused" in macro["title"] and macro["data"]["cost_usd"] == 0

    def test_the_earnings_memo_waits_for_new_results(self, st, steward, m, pf, monkeypatch):
        from datetime import date, timedelta
        b = Counting()
        old = (date.today() - timedelta(days=40)).isoformat()
        monkeypatch.setattr(steward, "earnings_dates", lambda t: [old])
        run(st, steward, m, pf, "HWM", b)
        b.calls.clear()
        run(st, steward, m, pf, "HWM", b)                                  # same stock, no new results
        assert "earnings" not in [t for t, _ in b.calls]
        monkeypatch.setattr(steward, "earnings_dates", lambda t: [date.today().isoformat()])
        b.calls.clear()
        run(st, steward, m, pf, "HWM", b)                                  # new results: read them
        assert "earnings" in [t for t, _ in b.calls]

    def test_no_second_verdict_when_nothing_would_trade(self, st, steward, m, pf):
        b = Counting()
        mt, _ = run(st, steward, m, pf, "HWM", b)                          # scripted rule: watch
        assert mt["decision"]["decision"] == "watch"
        assert [t for t, _ in b.calls].count("cio") == 1

    def test_names_only_close_to_a_trigger_are_not_convened(self, st, steward, m, pf):
        w = {"ticker": "HWM", "name": "Howmet", "grade": "B", "line": "x", "status": "close"}
        import council.views as v
        orig = v.watch_rows
        v.watch_rows = lambda pf_, q: [w]
        try:
            plan = scan.agenda(st, pf, m, steward.quotes(pf.tickers_needed()))
        finally:
            v.watch_rows = orig
        assert not any(c["ticker"] == "HWM" for c in plan["picked"])
        assert any(s["ticker"] == "HWM" and "when the trigger is hit" in s["why"] for s in plan["skipped"])

    def test_a_market_wide_fall_is_noise_without_ai(self, st, steward, ms, pfs):
        move(steward, "ASML.AS", -0.15)                                   # beta about 1.3
        move(steward, ms.benchmark, -0.15)
        events = watch.run(st, steward, ms, pfs, NoAI())
        a = st.alerts()[0]
        assert a["status"] == "noise" and a["backend"] == "code" and "market-wide" in events[0]

    def test_a_company_specific_fall_still_gets_the_sentinel(self, st, steward, ms, pfs):
        move(steward, "CME", -0.15)
        watch.run(st, steward, ms, pfs, Convene())
        assert st.alerts()[0]["status"] == "convene"

    def test_the_portfolio_meeting_needs_no_ai_without_a_possible_swap(self, st, steward, ms, pfs):
        mid = review.PortfolioMeeting(st, steward, ms, pfs, NoAI()).run()
        mt = st.meeting(mid)
        assert mt["status"] == "done" and mt["cost_usd"] == 0
        assert all(s["data"].get("model") == "code" for s in st.steps(mid) if s["phase"] == "committee")


class TestMonthlyBudget:
    def over(self, m):
        return m.model_copy(update={"limits": m.limits.model_copy(update={"monthly_ai_budget_usd": 0.05})})

    def test_spending_is_added_up(self, st, steward, m, pf):
        from council import budget
        run(st, steward, m, pf, "HWM", Counting())
        assert budget.spent(st) == pytest.approx(0.9)                      # 9 calls x $0.10

    def test_over_budget_the_meeting_stops_at_the_gate(self, st, steward, m, pf):
        run(st, steward, m, pf, "HWM", Counting())
        mt, steps = run(st, steward, self.over(m), pf, "WM", NoAI())
        assert mt["status"] == "stopped" and "AI budget" in str(steps[0]["data"]["checks"])

    def test_over_budget_every_ai_stage_waits(self, st, steward, ms, pfs):
        from council import budget
        run(st, steward, ms, pfs, "HWM", Counting())
        mo = self.over(ms)
        assert not budget.allows(st, mo, NoAI()) and budget.allows(st, mo, agents.ScriptedBackend())
        assert "budget" in screening.run_screen(st, steward, mo, NoAI())["error"]
        d = st.dossier(screening.write_dossier(st, steward, mo, "TSM", NoAI()))
        assert d["status"] == "failed" and "budget" in d["data"]["error"]
        move(steward, "CME", -0.15)
        assert "budget" in watch.run(st, steward, mo, pfs, NoAI())[0]
