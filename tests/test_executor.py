"""Tests for the executor.

Everything runs against PaperExchange. There are no live calls anywhere in this
file and no network of any kind.

The three scenarios that matter most have their own classes below:
TestRestartDoesNotDuplicate, TestHaltCancelsOrders, TestStopLiquidates.
"""

import dataclasses

import pytest

from grid import executor
from grid.control import Control
from grid.executor import (BUY, SELL, BelowMinimumSize, CrossedSpread, DuplicateOrder, GridState,
                           LiveExchange, PaperExchange, cancel_plan_orders,
                           client_order_id, desired_orders, load_state,
                           make_exchange, occupied_levels, plan_order_ids,
                           run_once)
from grid.plan import GridPlan, Status

SYMBOL = "AAA-USDT"


@pytest.fixture
def control(tmp_path):
    c = Control(tmp_path / "control.json")
    c.initialise()
    return c


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "state"


@pytest.fixture
def plans_dir(tmp_path):
    return tmp_path / "plans"


def make_plan(**over):
    """Ladder 94..106 in steps of 1, so levels are easy to reason about."""
    kw = dict(symbol=SYMBOL, lower=94.0, upper=106.0, levels=12,
              quote_per_level=100.0, stop_below=90.0, max_quote_total=1300.0,
              # ARITHMETIC: the occupied-level and fill tests below reason
              # about whole-number rungs. Geometric has its own coverage.
              spacing_mode="arithmetic")
    kw.update(over)
    p = GridPlan(**kw)
    p.approve()
    return p


@pytest.fixture
def plan():
    return make_plan()


@pytest.fixture
def ex(tmp_path):
    e = PaperExchange(tmp_path / "paper.json", quote=1300.0)
    e.set_price(SYMBOL, 100.0)
    return e


def run(plan, ex, control, state_dir, plans_dir=None, **kw):
    return run_once(plan, ex, control, state_dir=state_dir,
                    plans_dir=plans_dir, notify=lambda m: None, **kw)


def open_ids(ex):
    return {o.client_order_id for o in ex.fetch_open_orders(SYMBOL)}


# ------------------------------------------------------------ order identity

class TestClientOrderId:

    def test_it_is_deterministic(self):
        assert client_order_id("abc", 3, BUY) == client_order_id("abc", 3, BUY)

    def test_plan_level_and_side_all_matter(self):
        base = client_order_id("abc", 3, BUY)
        assert client_order_id("xyz", 3, BUY) != base
        assert client_order_id("abc", 4, BUY) != base
        assert client_order_id("abc", 3, SELL) != base

    def test_it_fits_okx_limits(self):
        cid = client_order_id("abcdef1234", 11, SELL)
        assert 1 <= len(cid) <= 32 and cid.isalnum()

    def test_a_plans_ids_cover_every_level_and_side(self, plan):
        assert len(plan_order_ids(plan)) == 2 * len(plan.grid_prices)

    def test_two_plans_never_share_an_id(self):
        assert not plan_order_ids(make_plan()) & plan_order_ids(make_plan())


# -------------------------------------------------------------- paper exchange

class TestPaperExchange:

    def test_it_is_not_live(self, ex):
        assert ex.is_live is False

    def test_place_and_fetch(self, ex):
        ex.place_limit(SYMBOL, BUY, 99.0, 1.0, "cid1")
        assert [o.client_order_id for o in ex.fetch_open_orders(SYMBOL)] == ["cid1"]

    def test_a_duplicate_client_order_id_is_rejected(self, ex):
        """The exchange's own guard, behind the executor's."""
        ex.place_limit(SYMBOL, BUY, 99.0, 1.0, "cid1")
        with pytest.raises(DuplicateOrder):
            ex.place_limit(SYMBOL, BUY, 99.0, 1.0, "cid1")

    def test_a_buy_above_the_mark_is_refused(self, ex):
        with pytest.raises(CrossedSpread):
            ex.place_limit(SYMBOL, BUY, 101.0, 1.0, "x")

    def test_a_sell_below_the_mark_is_refused(self, ex):
        with pytest.raises(CrossedSpread):
            ex.place_limit(SYMBOL, SELL, 99.0, 1.0, "x")

    def test_cancel_closes_the_order(self, ex):
        o = ex.place_limit(SYMBOL, BUY, 99.0, 1.0, "cid1")
        ex.cancel(SYMBOL, o)
        assert ex.fetch_open_orders(SYMBOL) == []

    def test_cancelling_twice_is_harmless(self, ex):
        o = ex.place_limit(SYMBOL, BUY, 99.0, 1.0, "cid1")
        ex.cancel(SYMBOL, o)
        ex.cancel(SYMBOL, o)
        assert ex.fetch_open_orders(SYMBOL) == []

    def test_advance_fills_a_buy_it_reaches(self, ex):
        ex.place_limit(SYMBOL, BUY, 99.0, 1.0, "cid1")
        filled = ex.advance_to(SYMBOL, 98.5)
        assert [o.client_order_id for o in filled] == ["cid1"]
        assert ex.fetch_balance(SYMBOL).base == pytest.approx(1.0)

    def test_advance_does_not_fill_an_order_it_never_reaches(self, ex):
        ex.place_limit(SYMBOL, BUY, 95.0, 1.0, "cid1")
        assert ex.advance_to(SYMBOL, 99.0) == []

    def test_state_survives_a_reopen(self, tmp_path):
        p = tmp_path / "paper.json"
        a = PaperExchange(p, quote=500.0)
        a.set_price(SYMBOL, 100.0)
        a.place_limit(SYMBOL, BUY, 99.0, 1.0, "cid1")
        b = PaperExchange(p)
        assert [o.client_order_id for o in b.fetch_open_orders(SYMBOL)] == ["cid1"]

    def test_market_sell_is_recorded(self, ex):
        ex.credit(SYMBOL, base=2.0)
        rec = ex.market_sell(SYMBOL, 2.0)
        assert rec["type"] == "market"
        assert ex.fetch_balance(SYMBOL).base == pytest.approx(0.0)


class TestExchangeSelection:

    def test_paper_is_the_default(self, control):
        assert isinstance(make_exchange(control), PaperExchange)

    def test_live_only_when_control_says_so(self, control):
        control.set_live(True, confirm="I ACCEPT THE RISK")
        assert isinstance(make_exchange(control), LiveExchange)

    def test_the_live_client_refuses_to_do_anything(self):
        with pytest.raises(NotImplementedError, match="not built yet"):
            LiveExchange().place_limit(SYMBOL, BUY, 1.0, 1.0, "x")

    def test_a_live_exchange_is_refused_while_control_says_paper(
            self, plan, control, state_dir):
        st = run(plan, LiveExchange(), control, state_dir)
        assert st.halted is True
        assert "paper" in st.note


# ------------------------------------------------------------- ladder shaping

class TestDesiredOrders:

    def test_buys_go_below_the_mark_only(self, plan):
        want = desired_orders(plan, 100.0, 0.0)
        assert all(o.price < 100.0 for o in want.values() if o.side == BUY)

    def test_no_sells_when_nothing_is_held(self, plan):
        want = desired_orders(plan, 100.0, 0.0)
        assert not [o for o in want.values() if o.side == SELL]

    def test_sells_go_above_the_mark_only(self, plan):
        """A sell resting below the mark would fill as taker."""
        want = desired_orders(plan, 100.0, 3.0)
        sells = [o for o in want.values() if o.side == SELL]
        assert sells and all(o.price > 100.0 for o in sells)

    def test_it_is_pure(self, plan):
        a = desired_orders(plan, 100.0, 2.0)
        b = desired_orders(plan, 100.0, 2.0)
        assert {k: (v.side, v.price, v.size) for k, v in a.items()} == \
               {k: (v.side, v.price, v.size) for k, v in b.items()}

    def test_it_never_exceeds_max_quote_total(self, plan):
        want = desired_orders(plan, 100.0, 0.0)
        spend = sum(o.notional for o in want.values() if o.side == BUY)
        assert spend <= plan.max_quote_total + 1e-9

    def test_capital_already_deployed_reduces_the_budget(self):
        p = make_plan(max_quote_total=300.0)
        none_held = desired_orders(p, 100.0, 0.0)
        some_held = desired_orders(p, 100.0, 2.0)
        assert (sum(o.notional for o in some_held.values() if o.side == BUY)
                < sum(o.notional for o in none_held.values() if o.side == BUY))

    def test_a_tiny_budget_buys_the_nearest_level_first(self):
        p = make_plan(max_quote_total=100.0)
        buys = [o for o in desired_orders(p, 100.0, 0.0).values() if o.side == BUY]
        assert len(buys) == 1 and buys[0].price == 99.0

    def test_sells_never_exceed_the_units_held(self, plan):
        want = desired_orders(plan, 100.0, 1.5)
        assert sum(o.size for o in want.values() if o.side == SELL) <= 1.5 + 1e-9

    def test_no_order_sits_exactly_on_the_mark(self, plan):
        assert all(o.price != 100.0 for o in desired_orders(plan, 100.0, 2.0).values())


class TestOccupiedLevels:

    def test_nothing_held_means_nothing_occupied(self, plan):
        assert occupied_levels(plan, 100.0, 0.0) == []

    def test_a_lot_occupies_the_level_below_the_mark(self, plan):
        """One lot ~= 100/99 units; the nearest level below 100 is 99."""
        assert occupied_levels(plan, 100.0, 100.0 / 99.0) == [5]   # 94+5 = 99

    def test_lots_fill_downward_from_the_mark(self, plan):
        two = 100.0 / 99.0 + 100.0 / 98.0
        assert occupied_levels(plan, 100.0, two) == [4, 5]

    def test_an_occupied_level_gets_no_buy_order(self, plan):
        want = desired_orders(plan, 100.0, 100.0 / 99.0)
        assert client_order_id(plan.id, 5, BUY) not in want


# --------------------------------------------------------------- normal run

class TestNormalOperation:

    def test_it_places_the_ladder(self, plan, ex, control, state_dir):
        st = run(plan, ex, control, state_dir)
        assert st.open_buys > 0
        assert len(open_ids(ex)) == st.open_buys + st.open_sells

    def test_only_limit_orders(self, plan, ex, control, state_dir):
        """Rule 3: the executor never crosses the spread outside the stop."""
        run(plan, ex, control, state_dir)
        assert ex.market_sells == []

    def test_every_order_is_below_the_mark_when_flat(self, plan, ex, control,
                                                     state_dir):
        run(plan, ex, control, state_dir)
        assert all(o.side == BUY and o.price < 100.0
                   for o in ex.fetch_open_orders(SYMBOL))

    def test_the_plan_moves_to_running(self, plan, ex, control, state_dir,
                                       plans_dir):
        run(plan, ex, control, state_dir, plans_dir)
        assert plan.status is Status.RUNNING

    def test_a_fill_produces_a_sell_on_the_next_pass(self, plan, ex, control,
                                                     state_dir):
        run(plan, ex, control, state_dir)
        ex.advance_to(SYMBOL, 98.5)              # buys at 99 and 98 fill
        st = run(plan, ex, control, state_dir)
        assert st.open_sells > 0
        assert all(o.price > 98.5 for o in ex.fetch_open_orders(SYMBOL)
                   if o.side == SELL)

    def test_a_grid_can_complete_a_cycle(self, plan, ex, control, state_dir):
        run(plan, ex, control, state_dir)
        ex.advance_to(SYMBOL, 98.5)
        run(plan, ex, control, state_dir)
        before = ex.fetch_balance(SYMBOL).quote
        ex.advance_to(SYMBOL, 100.5)             # sells fill
        run(plan, ex, control, state_dir)
        assert ex.fetch_balance(SYMBOL).quote > before

    def test_it_never_exceeds_the_cap_across_runs(self, plan, ex, control,
                                                  state_dir):
        for price in (100.0, 98.0, 96.0, 95.0):
            ex.advance_to(SYMBOL, price)
            run(plan, ex, control, state_dir)
            resting = sum(o.notional for o in ex.fetch_open_orders(SYMBOL)
                          if o.side == BUY)
            held = ex.fetch_balance(SYMBOL).base * price
            assert resting + held <= plan.max_quote_total + 1e-6

    def test_a_plan_that_is_not_live_places_nothing(self, ex, control, state_dir):
        p = make_plan()
        p.reject() if p.status is Status.PROPOSED else p.stop(Status.STOPPED)
        st = run(p, ex, control, state_dir)
        assert ex.fetch_open_orders(SYMBOL) == []
        assert "not runnable" in st.note


# -------------------------------------------------------------- restart safety

class TestRestartDoesNotDuplicate:
    """A restarted bot must never double-buy because it forgot what it held."""

    def test_running_twice_places_nothing_new(self, plan, ex, control, state_dir):
        first = run(plan, ex, control, state_dir)
        before = open_ids(ex)
        second = run(plan, ex, control, state_dir)
        assert second.placed == []
        assert open_ids(ex) == before
        assert len(first.placed) > 0

    def test_ten_passes_change_nothing(self, plan, ex, control, state_dir):
        run(plan, ex, control, state_dir)
        snapshot = open_ids(ex)
        for _ in range(10):
            run(plan, ex, control, state_dir)
        assert open_ids(ex) == snapshot

    def test_a_fresh_process_sees_the_same_orders(self, plan, control, state_dir,
                                                  tmp_path):
        """The restart case: a brand-new exchange client over the same state,
        and a brand-new plan object with no memory of what it did."""
        path = tmp_path / "paper.json"
        a = PaperExchange(path, quote=1300.0)
        a.set_price(SYMBOL, 100.0)
        run(plan, a, control, state_dir)
        before = {o.client_order_id for o in a.fetch_open_orders(SYMBOL)}

        b = PaperExchange(path)
        reloaded = GridPlan.from_json(plan.to_json())
        st = run(reloaded, b, control, state_dir)

        assert st.placed == []
        assert {o.client_order_id for o in b.fetch_open_orders(SYMBOL)} == before

    def test_a_restart_after_a_fill_does_not_rebuy_that_level(
            self, plan, control, state_dir, tmp_path):
        """The specific double-buy: the level filled, the stock is held, and a
        forgetful bot would place another buy at the same price."""
        path = tmp_path / "paper.json"
        a = PaperExchange(path, quote=1300.0)
        a.set_price(SYMBOL, 100.0)
        run(plan, a, control, state_dir)
        a.advance_to(SYMBOL, 98.5)
        held = a.fetch_balance(SYMBOL).base
        assert held > 0

        b = PaperExchange(path)
        reloaded = GridPlan.from_json(plan.to_json())
        run(reloaded, b, control, state_dir)

        assert b.fetch_balance(SYMBOL).base == pytest.approx(held)
        occupied = occupied_levels(reloaded, 98.5, held)
        for i in occupied:
            assert client_order_id(reloaded.id, i, BUY) not in open_ids(b)

    def test_a_lost_response_does_not_double_place(self, plan, ex, control,
                                                   state_dir):
        """The order arrived; the confirmation did not. The next pass must not
        place a second one — the exchange rejects the duplicate id and the
        executor treats that as success, not as an error."""
        run(plan, ex, control, state_dir)
        resting = ex.fetch_open_orders(SYMBOL)[0]

        calls = []
        original = ex.fetch_open_orders
        ex.fetch_open_orders = lambda s: [o for o in original(s)
                                          if o.client_order_id
                                          != resting.client_order_id]
        st = run(plan, ex, control, state_dir)
        ex.fetch_open_orders = original

        assert resting.client_order_id not in st.placed
        assert len([o for o in ex.fetch_open_orders(SYMBOL)
                    if o.client_order_id == resting.client_order_id]) == 1


# ------------------------------------------------------------------- halt path

class TestHaltCancelsOrders:

    def test_a_global_halt_cancels_this_plans_orders(self, plan, ex, control,
                                                     state_dir):
        run(plan, ex, control, state_dir)
        assert open_ids(ex)
        control.halt_all("stop everything")
        st = run(plan, ex, control, state_dir)
        assert st.halted is True
        assert ex.fetch_open_orders(SYMBOL) == []
        assert st.cancelled

    def test_a_symbol_halt_cancels_it(self, plan, ex, control, state_dir):
        run(plan, ex, control, state_dir)
        control.halt_symbol(SYMBOL, "this one only")
        st = run(plan, ex, control, state_dir)
        assert st.halted and ex.fetch_open_orders(SYMBOL) == []

    def test_a_plan_halt_cancels_it(self, plan, ex, control, state_dir):
        run(plan, ex, control, state_dir)
        control.halt_plan(plan.id, "this plan only")
        st = run(plan, ex, control, state_dir)
        assert st.halted and ex.fetch_open_orders(SYMBOL) == []

    def test_a_halt_does_not_sell_the_position(self, plan, ex, control,
                                               state_dir):
        """Stopping is not the same as selling. control.halt_all says so too."""
        run(plan, ex, control, state_dir)
        ex.advance_to(SYMBOL, 98.5)
        held = ex.fetch_balance(SYMBOL).base
        control.halt_all("stop")
        run(plan, ex, control, state_dir)
        assert ex.fetch_balance(SYMBOL).base == pytest.approx(held)
        assert ex.market_sells == []

    def test_a_halt_leaves_another_plans_orders_alone(self, ex, control,
                                                      state_dir):
        mine, theirs = make_plan(), make_plan()
        run(mine, ex, control, state_dir)
        run(theirs, ex, control, state_dir)
        control.halt_plan(mine.id, "just mine")
        run(mine, ex, control, state_dir)
        surviving = open_ids(ex)
        assert not (surviving & plan_order_ids(mine))
        assert surviving & plan_order_ids(theirs)

    def test_a_missing_control_file_halts(self, plan, ex, tmp_path, state_dir):
        """control.py fails closed; the executor inherits that."""
        st = run(plan, ex, Control(tmp_path / "absent.json"), state_dir)
        assert st.halted is True

    def test_halting_is_idempotent(self, plan, ex, control, state_dir):
        run(plan, ex, control, state_dir)
        control.halt_all("stop")
        run(plan, ex, control, state_dir)
        st = run(plan, ex, control, state_dir)
        assert st.cancelled == [] and ex.fetch_open_orders(SYMBOL) == []


# -------------------------------------------------------------------- the stop

class TestStopLiquidates:

    @pytest.fixture
    def stopped(self, plan, ex, control, state_dir, plans_dir):
        run(plan, ex, control, state_dir, plans_dir)
        ex.advance_to(SYMBOL, 95.0)
        run(plan, ex, control, state_dir, plans_dir)
        ex.advance_to(SYMBOL, 89.0)              # below stop_below = 90
        return run(plan, ex, control, state_dir, plans_dir), plan, ex

    def test_it_reports_stopped_out(self, stopped):
        st, _, _ = stopped
        assert st.stopped_out is True

    def test_the_position_is_liquidated(self, stopped):
        _, _, ex = stopped
        assert ex.fetch_balance(SYMBOL).base == pytest.approx(0.0)

    def test_it_used_a_market_order(self, stopped):
        """The one place crossing the spread is allowed."""
        _, _, ex = stopped
        assert len(ex.market_sells) == 1
        assert ex.market_sells[0]["type"] == "market"

    def test_every_order_is_cancelled(self, stopped):
        _, _, ex = stopped
        assert ex.fetch_open_orders(SYMBOL) == []

    def test_the_plan_status_is_stopped_out(self, stopped):
        _, plan, _ = stopped
        assert plan.status is Status.STOPPED_OUT

    def test_it_notifies(self, plan, ex, control, state_dir):
        sent = []
        run_once(plan, ex, control, state_dir=state_dir, notify=sent.append)
        ex.advance_to(SYMBOL, 89.0)
        run_once(plan, ex, control, state_dir=state_dir, notify=sent.append)
        assert any("stopped out" in m for m in sent)

    def test_a_failing_notifier_does_not_undo_the_stop(self, plan, ex, control,
                                                       state_dir):
        def boom(_):
            raise RuntimeError("telegram down")
        run_once(plan, ex, control, state_dir=state_dir, notify=lambda m: None)
        ex.advance_to(SYMBOL, 89.0)
        with pytest.raises(RuntimeError):
            run_once(plan, ex, control, state_dir=state_dir, notify=boom)
        # The liquidation happened before the notification was attempted.
        assert ex.fetch_balance(SYMBOL).base == pytest.approx(0.0)
        assert plan.status is Status.STOPPED_OUT

    def test_exactly_at_the_stop_triggers(self, plan, ex, control, state_dir):
        ex.advance_to(SYMBOL, 90.0)
        assert run(plan, ex, control, state_dir).stopped_out is True

    def test_just_above_the_stop_does_not(self, plan, ex, control, state_dir):
        ex.advance_to(SYMBOL, 90.01)
        assert run(plan, ex, control, state_dir).stopped_out is False

    def test_a_stopped_grid_does_not_restart_itself(self, stopped, control,
                                                    state_dir):
        st, plan, ex = stopped
        ex.advance_to(SYMBOL, 100.0)             # price recovers
        again = run(plan, ex, control, state_dir)
        assert ex.fetch_open_orders(SYMBOL) == []
        assert again.stopped_out is False        # it is simply not runnable
        assert "not runnable" in again.note

    def test_stopping_out_with_no_position_still_halts(self, plan, ex, control,
                                                       state_dir, plans_dir):
        ex.advance_to(SYMBOL, 89.0)
        st = run(plan, ex, control, state_dir, plans_dir)
        assert st.stopped_out and ex.market_sells == []
        assert plan.status is Status.STOPPED_OUT


# ------------------------------------------------------------------- state

class TestStateSnapshot:

    def test_it_is_written(self, plan, ex, control, state_dir):
        run(plan, ex, control, state_dir)
        assert (state_dir / f"{plan.id}.json").exists()

    def test_it_can_be_read_back(self, plan, ex, control, state_dir):
        st = run(plan, ex, control, state_dir)
        got = load_state(plan.id, state_dir)
        assert got.symbol == SYMBOL and got.open_buys == st.open_buys

    def test_it_records_the_price_and_holdings(self, plan, ex, control,
                                              state_dir):
        run(plan, ex, control, state_dir)
        ex.advance_to(SYMBOL, 98.5)
        run(plan, ex, control, state_dir)
        got = load_state(plan.id, state_dir)
        assert got.price == 98.5 and got.units_held > 0

    def test_a_corrupt_snapshot_reads_as_none(self, state_dir):
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "x.json").write_text("{not json")
        assert load_state("x", state_dir) is None

    def test_a_missing_snapshot_reads_as_none(self, state_dir):
        assert load_state("nope", state_dir) is None

    def test_it_records_the_halt_reason(self, plan, ex, control, state_dir):
        control.halt_all("because")
        run(plan, ex, control, state_dir)
        assert "because" in load_state(plan.id, state_dir).note

    def test_it_marks_paper_runs_as_not_live(self, plan, ex, control, state_dir):
        assert run(plan, ex, control, state_dir).live is False


class TestNoDecisionsHere:
    """The executor reads parameters and obeys them."""

    def test_the_module_calls_no_model(self):
        """Checked against the import lines, not the prose — the docstring
        mentions `analyst.py` deliberately, to say where decisions belong."""
        import inspect
        imports = [ln.strip() for ln in inspect.getsource(executor).splitlines()
                   if ln.strip().startswith(("import ", "from "))]
        joined = " ".join(imports)
        for banned in ("anthropic", "rubric", "analyst", "backtest", "okx"):
            assert banned not in joined, f"executor must not import {banned}"

    def test_it_reaches_no_model_at_runtime(self):
        import sys
        assert "anthropic" not in dir(sys.modules["grid.executor"])

    def test_it_does_not_import_the_analyst_or_rubric(self):
        import sys
        mod = sys.modules["grid.executor"]
        assert not hasattr(mod, "rubric") and not hasattr(mod, "analyst")

    def test_cancel_only_touches_this_plan(self, ex, control, state_dir):
        mine, theirs = make_plan(), make_plan()
        run(mine, ex, control, state_dir)
        run(theirs, ex, control, state_dir)
        cancel_plan_orders(mine, ex)
        assert not (open_ids(ex) & plan_order_ids(mine))
        assert open_ids(ex) & plan_order_ids(theirs)


class TestAnInvalidPlanIsRefused:
    """README: "a plan that fails cannot run even if approved".

    I found this by making `plan.validate()` return []: the suite went red
    only in `TestValidation`, because nothing downstream
    actually enforced it — the executor placed 75 orders on a ladder that gives
    48% of gross to the exchange. Approval is not the last word: a plan is a
    hand-editable JSON file, so this is re-checked on every pass.
    """

    def invalid_plan(self):
        # 150 levels over 100-200: 0.334% at the top of the ladder, where fees
        # take 48% of gross against validate()'s 30% limit.
        p = GridPlan(symbol=SYMBOL, lower=100.0, upper=200.0, levels=150,
                     quote_per_level=100.0, stop_below=95.0,
                     max_quote_total=20000.0)
        p.approve()
        return p

    def test_the_fixture_really_is_invalid(self):
        assert self.invalid_plan().validate()

    def test_no_orders_are_placed(self, ex, control, state_dir):
        p = self.invalid_plan()
        ex.set_price(SYMBOL, 150.0)
        st = run(p, ex, control, state_dir)
        assert st.placed == []
        assert ex.fetch_open_orders(SYMBOL) == []

    def test_it_reports_halted_with_the_reason(self, ex, control, state_dir):
        p = self.invalid_plan()
        ex.set_price(SYMBOL, 150.0)
        st = run(p, ex, control, state_dir)
        assert st.halted is True
        assert "fails validation" in st.note
        assert "gives away" in st.note and "in fees" in st.note

    def test_an_existing_ladder_is_cancelled(self, ex, control, state_dir):
        """A plan edited into an invalid state stops trading."""
        good = make_plan()
        run(good, ex, control, state_dir)
        assert ex.fetch_open_orders(SYMBOL)
        good.levels = 150                      # hand-edited into nonsense
        good.upper = 200.0
        st = run(good, ex, control, state_dir)
        assert ex.fetch_open_orders(SYMBOL) == []
        assert st.cancelled

    def test_it_does_not_liquidate(self, ex, control, state_dir):
        """Stopping is not selling — same rule as control.halt_all."""
        good = make_plan()
        run(good, ex, control, state_dir)
        ex.advance_to(SYMBOL, 98.5)
        held = ex.fetch_balance(SYMBOL).base
        assert held > 0
        good.levels = 150
        good.upper = 200.0
        run(good, ex, control, state_dir)
        assert ex.fetch_balance(SYMBOL).base == pytest.approx(held)
        assert ex.market_sells == []

    def test_it_notifies(self, ex, control, state_dir):
        p = self.invalid_plan()
        ex.set_price(SYMBOL, 150.0)
        sent = []
        run_once(p, ex, control, state_dir=state_dir, notify=sent.append)
        assert any("invalid plan" in m for m in sent)

    def test_it_is_checked_every_pass_not_only_at_approval(self, ex, control,
                                                           state_dir):
        good = make_plan()
        run(good, ex, control, state_dir)
        good.stop_below = 999.0                # edited above the range
        st = run(good, ex, control, state_dir)
        assert st.halted is True


class TestSpotOnlyReachesTheExecutor:
    """Entry point 2: a hand-edited plan file pointing at a derivative.

    Before this check, `run_once` placed 6 orders on BTC-USDT-SWAP.
    """

    def swap_plan(self):
        p = GridPlan(symbol="BTC-USDT-SWAP", lower=94.0, upper=106.0, levels=12,
                     quote_per_level=100.0, stop_below=90.0,
                     max_quote_total=1300.0)
        p.approve()
        return p

    def test_no_orders_are_placed_on_a_swap(self, ex, control, state_dir):
        p = self.swap_plan()
        ex.set_price("BTC-USDT-SWAP", 100.0)
        st = run(p, ex, control, state_dir)
        assert st.placed == []
        assert ex.fetch_open_orders("BTC-USDT-SWAP") == []

    def test_it_halts_with_the_reason(self, ex, control, state_dir):
        p = self.swap_plan()
        ex.set_price("BTC-USDT-SWAP", 100.0)
        st = run(p, ex, control, state_dir)
        assert st.halted is True and "SPOT ONLY" in st.note

    def test_a_running_grid_edited_to_a_swap_is_stopped(self, ex, control,
                                                        state_dir):
        """The per-pass check: valid at approval, edited afterwards."""
        good = make_plan()
        run(good, ex, control, state_dir)
        assert ex.fetch_open_orders(SYMBOL)
        good.symbol = "AAA-USDT-SWAP"
        ex.set_price("AAA-USDT-SWAP", 100.0)
        st = run(good, ex, control, state_dir)
        assert st.halted is True
        assert ex.fetch_open_orders("AAA-USDT-SWAP") == []

    def test_it_notifies(self, ex, control, state_dir):
        p = self.swap_plan()
        ex.set_price("BTC-USDT-SWAP", 100.0)
        sent = []
        run_once(p, ex, control, state_dir=state_dir, notify=sent.append)
        assert any("SPOT ONLY" in m for m in sent)


class TestAnOrderBelowMinSizeNeverReachesTheExchange:
    """An order below minSz against PaperExchange: the plan must be
    rejected before it gets that far."""

    def test_the_exchange_itself_rejects_a_sub_minimum_order(self, ex):
        """First establish that the exchange really would reject it."""
        ex.min_size = 10.0
        with pytest.raises(BelowMinimumSize):
            ex.place_limit(SYMBOL, BUY, 99.0, 1.0, "cid1")

    def test_a_plan_whose_orders_are_too_small_is_gated_first(self, ex, control,
                                                              state_dir):
        """The gate must catch it, so the exchange never sees the order."""
        from grid.okx import Instrument
        from grid.sizing import order_size_problems
        p = make_plan(quote_per_level=0.5)
        instrument = Instrument(inst_id=SYMBOL, min_size=10.0, lot_size=0.001,
                                tick_size=0.01, base="AAA", quote="USDT",
                                state="live")
        problems = order_size_problems(p, 100.0, instrument)
        assert problems, "the gate should have refused this plan"
        assert "below the exchange minimum" in problems[0]

    def test_the_executor_places_nothing_when_the_exchange_refuses(self, ex,
                                                                   control,
                                                                   state_dir):
        """Belt and braces: even if a bad plan somehow reached the executor,
        the rejection is surfaced rather than swallowed."""
        ex.min_size = 10.0
        p = make_plan()
        sent = []
        st = run_once(p, ex, control, state_dir=state_dir, notify=sent.append)
        assert st.placed == []
        assert st.rejected
        assert any("rejected" in m for m in sent)
