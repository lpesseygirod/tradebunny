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

    def test_the_watch_list_deadline_moves_the_reserve_to_the_index(self, pf, st, m):
        pf.review(on="2099-01-01")
        notes = [t["note"] for t in st.trades()]
        assert any("deadline" in n for n in notes)
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
        assert phases.count("blind") == 4 and phases.count("cross") == 2
        assert "committee" in phases and phases[-1] in ("risk", "trade")

    def test_a_rejected_name_never_reaches_an_agent(self, st, steward, m, pf):
        mt, steps = run(st, steward, m, pf, "SHA0.DE")
        assert mt["status"] == "stopped" and len(steps) == 1

    def test_an_approved_add_trades_through_the_risk_engine(self, st, steward, m, pf):
        before = len(st.trades())
        mt, steps = run(st, steward, m, pf, "TSM")
        assert mt["decision"]["decision"] == "add"
        assert len(st.trades()) == before + 1          # first tranche only
        assert any(s["phase"] == "trade" for s in steps)

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
            agents.ClaudeBackend(client=fc).run(agents.ROLES["valuation"], "f", agents.ValuationMemo, {})
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
                     b"Orders placed", b"overlap and scenarios", b"Five-point chokepoint test"):
            assert text in page, text

    def test_the_live_endpoint_returns_the_debate(self, client):
        client.post("/start")
        client.post("/meetings", data={"ticker": "HWM"})
        d = client.get("/meetings/1/live").get_json()
        assert d["status"] == "done" and "Blind memos" in d["html"]

    def test_every_page_renders(self, client):
        client.post("/start")
        client.post("/meetings", data={"ticker": "SHA0.DE"})
        for path in ("/", "/meetings", "/meetings/1", "/portfolio", "/rules"):
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
        assert len(rows) == 7 and all(r["status"] in ("ok", "close", "over") for r in rows)

    def test_the_meetings_filter(self, client):
        client.post("/start")
        client.post("/meetings", data={"ticker": "TSM"})
        client.post("/meetings", data={"ticker": "HWM"})
        page = client.get("/meetings?f=watch").data
        assert b"Howmet" in page and b"TSMC" not in page.split(b"<table>")[1]
