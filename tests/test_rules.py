"""Tests for the validation gate and ledger immutability."""

from datetime import datetime, timedelta, timezone

import pytest

from ledger.rules import lock_time, resolves_on, validate
from ledger.store import Ledger

NOW = datetime(2026, 9, 4, 6, 15, tzinfo=timezone.utc)
UNIVERSE = {"WY", "NUE", "TTWO", "CNH", "AAPL"}
BENCHMARKS = {"XLB", "XLC", "XLI", "SPY"}


def candidate(**over):
    base = dict(
        ticker="WY", direction="long", magnitude_pct=2.0, benchmark="XLB",
        horizon_days=10, confidence=0.62,
        chain=["IKEA expansion", "particleboard demand up", "softwood pulp up", "WY"],
        thesis=("IKEA confirmed a 2027 Poland panel expansion. Pulpwood is the input "
                "under most pressure when panel capacity steps up, and integrated North "
                "American producers are the marginal supplier when European pulpwood "
                "tightens, so the effect lands on WY."),
        falsifier=("European pulpwood is sourced regionally and the link to North "
                   "American producers simply does not transmit."),
        source_subjects=["IKEA"],
        source_published_at=(NOW - timedelta(hours=12)).isoformat(),
    )
    base.update(over)
    return base


def check(cand, **kw):
    return validate(cand, universe=UNIVERSE, benchmarks=BENCHMARKS, now=NOW, **kw)


class TestValidate:
    def test_a_good_candidate_passes(self):
        assert check(candidate()).ok

    def test_rejects_first_order_prediction(self):
        """The single most important rule: never predict the article's own subject."""
        v = check(candidate(ticker="TTWO", source_subjects=["TTWO"], benchmark="XLC"))
        assert not v.ok
        assert any("first-order" in r for r in v.reasons)

    def test_rejects_ticker_outside_universe(self):
        v = check(candidate(ticker="GME"))
        assert not v.ok and any("universe" in r for r in v.reasons)

    def test_rejects_short_chain(self):
        v = check(candidate(chain=["news", "WY"]))
        assert not v.ok and any("chain" in r for r in v.reasons)

    def test_rejects_hedged_thesis(self):
        v = check(candidate(thesis="Demand for wood might potentially increase somewhat "
                                   "and this could possibly be good for producers of "
                                   "timber over the coming weeks or perhaps months."))
        assert not v.ok and any("hedges" in r for r in v.reasons)

    def test_rejects_weak_falsifier(self):
        v = check(candidate(falsifier="It could be wrong."))
        assert not v.ok and any("falsifier" in r for r in v.reasons)

    @pytest.mark.parametrize("conf", [0.3, 0.45, 0.95, 0.99])
    def test_rejects_out_of_range_confidence(self, conf):
        assert not check(candidate(confidence=conf)).ok

    def test_rejects_stale_news(self):
        old = (NOW - timedelta(hours=100)).isoformat()
        v = check(candidate(source_published_at=old))
        assert not v.ok and any("old" in r for r in v.reasons)

    def test_rejects_future_dated_source(self):
        future = (NOW + timedelta(hours=5)).isoformat()
        assert not check(candidate(source_published_at=future)).ok

    def test_rejects_duplicate_open_ticker(self):
        v = check(candidate(), open_tickers={"WY"})
        assert not v.ok and any("already has an open" in r for r in v.reasons)

    def test_rejects_missing_required_field(self):
        c = candidate()
        del c["falsifier"]
        v = check(c)
        assert not v.ok and any("falsifier" in r for r in v.reasons)

    def test_a_missing_publish_time_is_rejected_not_skipped(self):
        """It used to be optional, which handed the model an opt-out from the
        point-in-time rule: omit the field and any story, however stale, passed."""
        c = candidate()
        del c["source_published_at"]
        assert not check(c).ok

    @pytest.mark.parametrize("empty", [None, ""])
    def test_an_empty_publish_time_is_also_rejected(self, empty):
        assert not check(candidate(source_published_at=empty)).ok

    def test_a_string_chain_is_not_three_links(self):
        """len("source -> mechanism -> target") is 29, so a bare `len(chain) < 3`
        passed a string all the way through to storage."""
        v = check(candidate(chain="source -> mechanism -> target"))
        assert not v.ok and any("chain" in r for r in v.reasons)

    def test_rejects_self_benchmarking(self):
        assert not check(candidate(ticker="WY", benchmark="WY")).ok


class TestTiming:
    def test_lock_is_same_day_when_before_close(self):
        assert lock_time(NOW).date() == NOW.date()

    def test_lock_rolls_over_after_close(self):
        late = NOW.replace(hour=21)
        assert lock_time(late).date() == (NOW + timedelta(days=1)).date()

    def test_resolve_date_converts_trading_days_to_calendar(self):
        assert resolves_on(NOW, 10) == "2026-09-18"


class TestLedgerImmutability:
    def test_decision_refused_after_lock(self, tmp_path):
        led = Ledger(tmp_path / "t.db")
        pid = led.record(dict(
            **{k: v for k, v in candidate().items() if k != "source_subjects"},
            engine="equity", created_at=NOW.isoformat(),
            lock_at=NOW.isoformat(), resolves_on="2026-09-18"))
        # one second after the lock time
        assert not led.decide(pid, "bought", now=NOW + timedelta(seconds=1))
        assert led.get(pid)["decision"] == "pending"
        led.close()

    def test_decision_cannot_be_changed_once_made(self, tmp_path):
        led = Ledger(tmp_path / "t.db")
        pid = led.record(dict(
            **{k: v for k, v in candidate().items() if k != "source_subjects"},
            engine="equity", created_at=NOW.isoformat(),
            lock_at=(NOW + timedelta(hours=8)).isoformat(), resolves_on="2026-09-18"))
        assert led.decide(pid, "bought", now=NOW)
        assert not led.decide(pid, "skipped", now=NOW)   # no take-backs
        assert led.get(pid)["decision"] == "bought"
        led.close()

    def test_expired_decisions_lock_as_skipped(self, tmp_path):
        """Checks the resulting VALUE, not just the row count. Asserting only the
        count would pass even if lock_expired had written 'bought' — precisely the
        corruption the method exists to prevent."""
        led = Ledger(tmp_path / "t.db")
        pid = led.record(dict(
            **{k: v for k, v in candidate().items() if k != "source_subjects"},
            engine="equity", created_at=NOW.isoformat(),
            lock_at=NOW.isoformat(), resolves_on="2026-09-18"))
        assert led.lock_expired(now=NOW + timedelta(hours=1)) == 1
        row = led.get(pid)
        assert row["decision"] == "skipped"
        assert row["decided_at"]
        led.close()

    def test_tampering_with_the_twin_is_detectable(self, tmp_path):
        """The twin is the primary control. While it sat outside _HASHED_COLS it
        was possible to wait for an outcome, swap the twin to a ticker known to
        have lost, re-score, and flip beats_twin to True with no integrity warning."""
        led = Ledger(tmp_path / "t.db")
        pid = led.record(dict(
            **{k: v for k, v in candidate().items() if k != "source_subjects"},
            engine="equity", created_at=NOW.isoformat(), lock_at=NOW.isoformat(),
            resolves_on="2026-09-18", twin_ticker="DOW", twin_direction="long"))
        assert led.verify_integrity() == []
        led.conn.execute("UPDATE predictions SET twin_ticker='AAPL' WHERE id=?", (pid,))
        led.conn.commit()
        assert led.verify_integrity() == [pid]
        led.close()

    def test_resolve_rejects_unknown_fields(self, tmp_path):
        """A typo used to be silently dropped while still marking the row resolved
        — leaving a NULL score, un-rescorable, invisible to the scorecard."""
        led = Ledger(tmp_path / "t.db")
        pid = led.record(dict(
            **{k: v for k, v in candidate().items() if k != "source_subjects"},
            engine="equity", created_at=NOW.isoformat(), lock_at=NOW.isoformat(),
            resolves_on="2026-09-18"))
        with pytest.raises(ValueError):
            led.resolve(pid, net_excess=5.0)          # typo: net_excess_pct
        assert led.get(pid)["status"] == "open"       # still resolvable
        led.close()

    def test_resolve_requires_a_score(self, tmp_path):
        led = Ledger(tmp_path / "t.db")
        pid = led.record(dict(
            **{k: v for k, v in candidate().items() if k != "source_subjects"},
            engine="equity", created_at=NOW.isoformat(), lock_at=NOW.isoformat(),
            resolves_on="2026-09-18"))
        with pytest.raises(ValueError):
            led.resolve(pid, hit=1)
        led.close()

    def test_ticker_case_is_normalised_on_write(self, tmp_path):
        """A lowercase 'wy' was stored raw, so open_tickers() returned {'wy'} and
        the next run compared 'WY' against it, found nothing, and accepted a second
        open prediction on the same ticker."""
        led = Ledger(tmp_path / "t.db")
        led.record(dict(
            **{k: v for k, v in candidate(ticker="wy").items() if k != "source_subjects"},
            engine="equity", created_at=NOW.isoformat(), lock_at=NOW.isoformat(),
            resolves_on="2026-09-18"))
        assert led.open_tickers() == {"WY"}
        led.close()

    def test_a_prediction_cannot_be_created_already_resolved(self, tmp_path):
        led = Ledger(tmp_path / "t.db")
        with pytest.raises(ValueError):
            led.record(dict(
                **{k: v for k, v in candidate().items() if k != "source_subjects"},
                engine="equity", created_at=NOW.isoformat(), lock_at=NOW.isoformat(),
                resolves_on="2026-09-18", status="resolved", net_excess_pct=99.0))
        led.close()

    def test_tampering_is_detectable(self, tmp_path):
        led = Ledger(tmp_path / "t.db")
        pid = led.record(dict(
            **{k: v for k, v in candidate().items() if k != "source_subjects"},
            engine="equity", created_at=NOW.isoformat(),
            lock_at=NOW.isoformat(), resolves_on="2026-09-18"))
        assert led.verify_integrity() == []
        led.conn.execute("UPDATE predictions SET magnitude_pct=99 WHERE id=?", (pid,))
        led.conn.commit()
        assert led.verify_integrity() == [pid]     # the hash catches it
        led.close()

    def test_cannot_rescore_a_resolved_entry(self, tmp_path):
        led = Ledger(tmp_path / "t.db")
        pid = led.record(dict(
            **{k: v for k, v in candidate().items() if k != "source_subjects"},
            engine="equity", created_at=NOW.isoformat(),
            lock_at=NOW.isoformat(), resolves_on="2026-09-18"))
        led.resolve(pid, net_excess_pct=1.0, hit=1)
        with pytest.raises(ValueError):
            led.resolve(pid, net_excess_pct=5.0, hit=1)
        led.close()
