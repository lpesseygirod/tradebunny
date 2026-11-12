"""Tests for data freshness, instrument state, fee fallback and regime drift."""

import time

import pytest

from grid import drift, freshness
from grid.freshness import (FreshnessReport, Stamp, candle_age, human_age,
                            human_duration)
from grid.okx import Instrument
from grid.plan import GridPlan, Status

HOUR = 3600.0


class TestStamps:

    def test_a_fresh_stamp_is_fresh(self):
        s = Stamp("candles", time.time(), 3600.0)
        assert s.is_fresh() and s.age() < 5

    def test_an_old_stamp_is_stale(self):
        s = Stamp("candles", time.time() - 7200, 3600.0)
        assert not s.is_fresh()

    def test_never_fetched_is_stale_not_fresh(self):
        """Absent evidence of freshness is not evidence of freshness."""
        s = Stamp("candles", None, 3600.0)
        assert not s.is_fresh() and s.age() is None

    def test_the_age_is_measured_from_the_stamp_not_a_file(self):
        s = Stamp("candles", 1000.0, 3600.0)
        assert s.age(now=1500.0) == 500.0

    def test_describe_marks_stale_sources(self):
        assert "STALE" in Stamp("candles", None, 3600.0).describe()

    def test_a_tolerance_is_a_duration_not_a_time(self):
        """It used to render as '60m ago', which reads as a timestamp."""
        d = Stamp("candles", time.time(), 3600.0).describe()
        assert "tolerance 60m)" in d


class TestFreshnessReport:

    def test_it_lists_every_source(self):
        r = FreshnessReport()
        for src in ("candles", "tickers", "fees", "instruments"):
            r.add(src, time.time())
        assert len(r.stamps) == 4 and r.all_fresh()

    def test_it_names_what_is_stale(self):
        r = FreshnessReport()
        r.add("candles", time.time())
        r.add("fees", None)
        assert [s.source for s in r.stale()] == ["fees"]
        assert "1 STALE" in r.explain()

    def test_an_empty_report_says_nothing_is_checked(self):
        assert "nothing is being checked" in FreshnessReport().explain()

    def test_default_tolerances_are_per_source(self):
        r = FreshnessReport()
        assert r.add("candles", None).tolerance < r.add("fees", None).tolerance


class TestCandleAge:
    """Measured from the BAR's timestamp, not from when it was fetched."""

    def test_a_recent_bar_is_young(self):
        now = time.time()
        cs = [{"ts": (now - HOUR) * 1000.0}]
        assert candle_age(cs, now=now) == pytest.approx(0.0, abs=5)

    def test_an_old_bar_is_old_however_recently_it_was_fetched(self):
        now = time.time()
        cs = [{"ts": (now - 86400) * 1000.0}]
        assert candle_age(cs, now=now) > 80000

    def test_it_accounts_for_the_bar_not_being_closed_yet(self):
        """`ts` marks the OPEN, so a 1H bar stamped 09:00 closes at 10:00."""
        now = time.time()
        cs = [{"ts": (now - 1800) * 1000.0}]         # opened 30m ago
        assert candle_age(cs, now=now) == 0.0        # not yet closed

    def test_no_candles_is_unknown_not_fresh(self):
        assert candle_age([]) is None

    def test_it_uses_the_newest_bar(self):
        now = time.time()
        cs = [{"ts": (now - 86400) * 1000.0}, {"ts": (now - HOUR) * 1000.0}]
        assert candle_age(cs, now=now) < 60


class TestInstrumentState:
    """A delisted pair must never reach a proposal."""

    def inst(self, state):
        return Instrument(inst_id="X-USDT", min_size=1.0, lot_size=1.0,
                          tick_size=0.01, base="X", quote="USDT", state=state)

    def test_live_is_tradeable(self):
        assert self.inst("live").is_live
        assert self.inst("live").state_problem is None

    @pytest.mark.parametrize("state", ["suspend", "preopen", "expired", "test"])
    def test_everything_else_is_refused(self, state):
        i = self.inst(state)
        assert not i.is_live and i.state_problem

    def test_preopen_explains_the_specific_danger(self):
        """Orders rest in a book that is not matching."""
        assert "not matching" in self.inst("preopen").state_problem

    def test_an_unknown_state_is_also_refused(self):
        assert self.inst("something-new").state_problem


class TestHumanFormatting:

    def test_seconds_minutes_hours_days(self):
        assert human_age(30).endswith("s ago")
        assert human_age(600).endswith("m ago")
        assert human_age(7200).endswith("h ago")
        assert human_age(200000).endswith("d ago")

    def test_never_fetched(self):
        assert human_age(None) == "never fetched"

    def test_durations_carry_no_direction(self):
        assert "ago" not in human_duration(3600)


# --------------------------------------------------------------- regime drift

def plan_at(lower=94.0, upper=106.0, **metrics_over):
    m = {"adf_p": 0.001, "hurst": 0.30, "adx": 12.0}
    m.update(metrics_over)
    p = GridPlan(symbol="AAA-USDT", lower=lower, upper=upper, levels=12,
                 quote_per_level=100.0, stop_below=85.0,
                 max_quote_total=1300.0, metrics=m)
    p.approve()
    return p


def series(kind, n=300):
    import random
    r = random.Random(7)
    if kind == "reverting":
        p, out = 100.0, []
        for _ in range(n):
            p += 0.20 * (100.0 - p) + r.gauss(0, 1.2)
            out.append(p)
        return out
    p, out = 100.0, []
    for _ in range(n):
        p *= (1 + 0.004 + r.gauss(0, 0.004))
        out.append(p)
    return out


def hl(closes, spread=0.004):
    return ([c * (1 + spread) for c in closes], [c * (1 - spread) for c in closes])


class TestDriftDetection:

    def test_a_still_reverting_grid_is_not_flagged(self):
        cs = series("reverting")
        h, l = hl(cs)
        rep = drift.check_plan(plan_at(), cs, h, l)
        assert not rep.decayed, rep.explain()

    def test_a_grid_whose_asset_started_trending_is_flagged(self):
        cs = series("trending")
        h, l = hl(cs)
        rep = drift.check_plan(plan_at(), cs, h, l)
        assert rep.decayed
        assert rep.reasons

    def test_it_reports_before_and_after(self):
        cs = series("trending")
        h, l = hl(cs)
        rep = drift.check_plan(plan_at(adf_p=0.001, hurst=0.30, adx=12.0),
                               cs, h, l)
        names = {r.name for r in rep.readings}
        assert names == {"ADF p-value", "Hurst", "ADX"}
        for r in rep.readings:
            assert r.before is not None

    def test_the_adf_lapse_is_named(self):
        cs = series("trending")
        h, l = hl(cs)
        rep = drift.check_plan(plan_at(), cs, h, l)
        assert any("random-walk null is no longer rejected" in r
                   for r in rep.reasons)

    def test_price_leaving_the_range_is_drift(self):
        """The least statistical and most practical signal."""
        cs = [200.0 + i * 0.01 for i in range(300)]     # far above the band
        h, l = hl(cs)
        rep = drift.check_plan(plan_at(lower=94.0, upper=106.0), cs, h, l)
        assert rep.outside_fraction > drift.MAX_OUTSIDE_FRACTION
        assert any("OUTSIDE" in r for r in rep.reasons)

    def test_too_few_bars_is_skipped_not_guessed(self):
        cs = series("trending", n=50)
        h, l = hl(cs)
        rep = drift.check_plan(plan_at(), cs, h, l)
        assert rep.skipped and not rep.decayed

    def test_the_message_says_nothing_was_stopped(self):
        cs = series("trending")
        h, l = hl(cs)
        msg = drift.check_plan(plan_at(), cs, h, l).message()
        assert "STILL RUNNING" in msg and "your decision" in msg

    def test_it_does_not_touch_the_plan(self):
        cs = series("trending")
        h, l = hl(cs)
        p = plan_at()
        before = p.to_json()
        drift.check_plan(p, cs, h, l)
        assert p.to_json() == before
        assert p.status is Status.APPROVED

    def test_bars_since_slices_from_approval(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        cs = [{"ts": (now - timedelta(hours=k)).timestamp() * 1000.0}
              for k in range(10, 0, -1)]
        cutoff = (now - timedelta(hours=5)).isoformat(timespec="seconds")
        assert len(drift.bars_since(cs, cutoff)) == 5

    def test_an_unparseable_approval_time_keeps_everything(self):
        cs = [{"ts": 1.0}, {"ts": 2.0}]
        assert len(drift.bars_since(cs, "not-a-date")) == 2
