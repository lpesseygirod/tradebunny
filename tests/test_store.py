"""Tests for the shared transaction and event ledger."""

import pytest

from grid.store import LIVE, PAPER, Store, totals


@pytest.fixture
def db(tmp_path):
    s = Store(tmp_path / "grid.db")
    yield s
    s.close()


def fill(db, side, price, qty, plan="p1", mode=PAPER, fee=None, cid=None):
    return db.record_transaction(
        plan_id=plan, symbol="X-USDT", mode=mode, side=side, price=price,
        qty=qty, fee=price * qty * 0.0008 if fee is None else fee,
        client_order_id=cid or f"{side}-{price}-{qty}")


class TestTransactions:

    def test_a_fill_is_recorded(self, db):
        assert fill(db, "buy", 100.0, 1.0) is not None
        assert len(db.transactions("p1")) == 1

    def test_the_same_fill_twice_is_recorded_once(self, db):
        """The executor re-derives state every pass and may see a fill twice.
        A double-counted trade corrupts every figure downstream."""
        first = fill(db, "buy", 100.0, 1.0, cid="c1")
        again = db.record_transaction(
            plan_id="p1", symbol="X-USDT", mode=PAPER, side="buy", price=100.0,
            qty=1.0, fee=0.08, client_order_id="c1",
            ts=db.transactions("p1")[0].ts)
        assert first is not None and again is None
        assert len(db.transactions("p1")) == 1

    def test_running_position_after_each_row(self, db):
        for side, px in (("buy", 100.0), ("buy", 98.0), ("sell", 102.0)):
            fill(db, side, px, 1.0)
        txs = db.transactions("p1", newest_first=False)
        assert [t.position for t in txs] == [1.0, 2.0, 1.0]

    def test_running_realised_pnl(self, db):
        fill(db, "buy", 100.0, 1.0, fee=0.0)
        fill(db, "sell", 110.0, 1.0, fee=0.0)
        txs = db.transactions("p1", newest_first=False)
        assert txs[0].realised == 0.0
        assert txs[1].realised == pytest.approx(10.0)

    def test_realised_uses_average_cost(self, db):
        fill(db, "buy", 100.0, 1.0, fee=0.0)
        fill(db, "buy", 200.0, 1.0, fee=0.0)
        fill(db, "sell", 150.0, 1.0, fee=0.0)
        txs = db.transactions("p1", newest_first=False)
        assert txs[-1].realised == pytest.approx(0.0)   # avg cost 150

    def test_fees_reduce_realised_pnl(self, db):
        fill(db, "buy", 100.0, 1.0, fee=0.0)
        fill(db, "sell", 110.0, 1.0, fee=1.0)
        assert db.transactions("p1")[0].realised == pytest.approx(9.0)

    def test_newest_first_by_default(self, db):
        fill(db, "buy", 100.0, 1.0)
        fill(db, "buy", 99.0, 1.0)
        assert db.transactions("p1")[0].price == 99.0

    def test_running_totals_mean_the_same_either_direction(self, db):
        for px in (100.0, 99.0, 98.0):
            fill(db, "buy", px, 1.0)
        newest = db.transactions("p1", newest_first=True)
        oldest = db.transactions("p1", newest_first=False)
        assert newest[0].position == oldest[-1].position

    def test_modes_are_kept_apart(self, db):
        fill(db, "buy", 100.0, 1.0, plan="p1", mode=PAPER)
        fill(db, "buy", 100.0, 1.0, plan="p2", mode=LIVE)
        assert len(db.transactions(mode=PAPER)) == 1
        assert len(db.transactions(mode=LIVE)) == 1

    def test_an_unknown_mode_is_refused(self, db):
        with pytest.raises(ValueError, match="mode"):
            db.record_transaction(plan_id="p", symbol="X", mode="pretend",
                                  side="buy", price=1.0, qty=1.0)

    def test_an_unknown_side_is_refused(self, db):
        with pytest.raises(ValueError, match="side"):
            db.record_transaction(plan_id="p", symbol="X", mode=PAPER,
                                  side="hodl", price=1.0, qty=1.0)

    def test_there_is_no_delete(self, db):
        """Append-only: a trade that happened happened."""
        assert not hasattr(db, "delete_transaction")


class TestTotals:

    def test_an_empty_ledger_is_zero(self, db):
        assert totals(db.transactions("nope")).total == 0.0

    def test_unrealised_marks_to_the_current_price(self, db):
        fill(db, "buy", 100.0, 1.0, fee=0.0)
        t = totals(db.transactions("p1"), marks={"X-USDT": 110.0})
        assert t.unrealised == pytest.approx(10.0)
        assert t.current_value == pytest.approx(110.0)

    def test_buy_and_hold_over_the_same_period(self, db):
        """100 into the asset at 100, held to 120, is +20."""
        fill(db, "buy", 100.0, 1.0, fee=0.0)
        t = totals(db.transactions("p1"), marks={"X-USDT": 120.0})
        assert t.buy_and_hold == pytest.approx(20.0)

    def test_it_says_which_is_ahead(self, db):
        fill(db, "buy", 100.0, 1.0, fee=0.0)
        fill(db, "sell", 101.0, 1.0, fee=0.0)
        t = totals(db.transactions("p1"), marks={"X-USDT": 200.0})
        assert t.beats_buy_and_hold is False   # holding would have doubled

    def test_fees_are_totalled(self, db):
        fill(db, "buy", 100.0, 1.0, fee=0.5)
        fill(db, "sell", 110.0, 1.0, fee=0.5)
        assert totals(db.transactions("p1")).fees == pytest.approx(1.0)

    def test_scale_rederives_at_another_capital(self, db):
        fill(db, "buy", 100.0, 1.0, fee=0.0)
        fill(db, "sell", 110.0, 1.0, fee=0.0)
        one = totals(db.transactions("p1"))
        four = totals(db.transactions("p1"), scale=4.0)
        assert four.realised == pytest.approx(one.realised * 4)

    def test_scale_does_not_change_prices(self, db):
        fill(db, "buy", 100.0, 1.0, fee=0.0)
        t = totals(db.transactions("p1"), marks={"X-USDT": 100.0}, scale=10.0)
        assert t.position == pytest.approx(10.0)


class TestEvents:

    def test_an_event_is_recorded_and_unread(self, db):
        db.record_event("fill", "X filled")
        assert db.unread_count() == 1
        assert db.events()[0].unread

    def test_newest_first(self, db):
        db.record_event("fill", "first")
        db.record_event("fill", "second")
        assert db.events()[0].title == "second"

    def test_mark_one_read(self, db):
        i = db.record_event("fill", "X")
        assert db.mark_read(i) == 1
        assert db.unread_count() == 0

    def test_mark_all_read(self, db):
        for k in range(3):
            db.record_event("fill", str(k))
        assert db.mark_read() == 3
        assert db.unread_count() == 0

    def test_marking_read_twice_changes_nothing(self, db):
        i = db.record_event("fill", "X")
        db.mark_read(i)
        assert db.mark_read(i) == 0

    def test_reading_never_deletes(self, db):
        i = db.record_event("fill", "X")
        db.mark_read(i)
        assert len(db.events()) == 1        # still there, just read

    def test_unread_only_filter(self, db):
        a = db.record_event("fill", "a")
        db.record_event("fill", "b")
        db.mark_read(a)
        assert [e.title for e in db.events(unread_only=True)] == ["b"]

    def test_there_is_no_delete(self, db):
        """This is the record of what the system told me."""
        assert not hasattr(db, "delete_event")


class TestConcurrency:

    def test_two_connections_can_use_it_at_once(self, tmp_path):
        """WAL is the whole reason this is SQLite: the executor writes while
        the web reads."""
        writer = Store(tmp_path / "grid.db")
        reader = Store(tmp_path / "grid.db")
        fill(writer, "buy", 100.0, 1.0)
        assert len(reader.transactions("p1")) == 1
        writer.close()
        reader.close()

    def test_it_is_in_wal_mode(self, db):
        mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
