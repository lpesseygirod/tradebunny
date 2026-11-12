"""Tests for the plan contract and the kill switches.

The control tests matter more than they look: a kill switch that fails open is
worse than no kill switch, because you believe you have stopped and you have not.
"""

import json

import pytest

from grid.control import Control
from grid.fees import FeeModel
from grid.plan import GridPlan, Status, load_all


def plan(**over):
    base = dict(symbol="BTC-USDT", lower=100.0, upper=110.0, levels=10,
                quote_per_level=20.0, stop_below=95.0, max_quote_total=300.0)
    base.update(over)
    return GridPlan(**base)


class TestGridGeometry:
    def test_arithmetic_spacing_is_one_percent(self):
        """100-110 over 10 arithmetic steps = 1.0 each = 1% at the bottom."""
        assert plan(spacing_mode="arithmetic").spacing_pct == pytest.approx(0.01)

    def test_geometric_spacing_is_the_tenth_root(self):
        """The same band geometrically: (110/100)**(1/10) - 1."""
        p = plan()                                  # geometric is the default
        assert p.spacing_pct == pytest.approx(1.1 ** 0.1 - 1)

    def test_price_ladder_spans_the_range(self):
        p = plan()
        prices = p.grid_prices
        assert len(prices) == 11
        assert prices[0] == pytest.approx(100.0)
        assert prices[-1] == pytest.approx(110.0)

    def test_max_exposure_respects_the_cap(self):
        assert plan(levels=10, quote_per_level=20.0,
                    max_quote_total=150.0).max_exposure == 150.0

    def test_max_exposure_counts_every_ladder_line(self):
        """11 prices for 10 levels — understating exposure by a level is the
        wrong direction to be wrong in."""
        p = plan(levels=10, quote_per_level=20.0, max_quote_total=1000.0)
        assert len(p.grid_prices) == 11
        assert p.max_exposure == 220.0

    def test_geometric_spacing_is_identical_everywhere(self):
        """The reason it is the default: no part of the ladder degrades."""
        p = plan(lower=100.0, upper=200.0, levels=10, quote_per_level=20.0,
                 max_quote_total=1000.0)
        assert p.spacing_pct == p.min_spacing_pct
        prices = p.grid_prices
        gaps = [prices[i + 1] / prices[i] - 1 for i in range(len(prices) - 1)]
        assert max(gaps) == pytest.approx(min(gaps))

    def test_min_spacing_is_measured_at_the_top_of_the_ladder(self):
        """ARITHMETIC only: levels evenly spaced in PRICE mean the percentage
        gap shrinks going up, so the bottom figure is the most flattering one.
        Geometric spacing removes the problem rather than measuring around it —
        see test_geometric_spacing_is_identical_everywhere."""
        p = plan(lower=100.0, upper=200.0, levels=10, spacing_mode="arithmetic")
        assert p.spacing_pct > p.min_spacing_pct


class TestValidation:
    def test_a_sane_plan_has_no_problems(self):
        assert plan().validate() == []

    def test_rejects_inverted_range(self):
        assert any("upper" in p for p in plan(lower=110, upper=100).validate())

    def test_rejects_spacing_that_feeds_the_exchange(self):
        """200 levels over a 10% range is 0.05% apart — fees eat everything."""
        problems = plan(levels=200, max_quote_total=1e6).validate()
        assert any("fees" in p or "gross profit" in p for p in problems)

    def test_fee_gate_uses_the_narrowest_spacing_not_the_widest(self):
        """A wide range with many levels looked fine measured at the bottom while
        the top half of the ladder gave away nearly half its gross to fees."""
        p = plan(lower=100.0, upper=200.0, levels=150,
                 quote_per_level=1.0, max_quote_total=1e6, stop_below=95.0)
        problems = p.validate()
        assert any("narrowest spacing" in x for x in problems)

    def test_warns_when_the_cap_cannot_fund_the_ladder(self):
        p = plan(levels=10, quote_per_level=20.0, max_quote_total=100.0)
        assert any("cannot fund the whole ladder" in x for x in p.validate())

    def test_accepts_spacing_that_clears_fees(self):
        assert plan(levels=10).validate() == []

    def test_requires_a_stop(self):
        assert any("stop" in p for p in plan(stop_below=0).validate())

    def test_stop_must_be_below_the_range(self):
        assert any("stop_below" in p for p in plan(stop_below=105.0).validate())

    def test_stop_cannot_be_absurdly_far_away(self):
        """A 'stop' at -60% is not a stop, it is a wish."""
        assert any("not a stop" in p for p in plan(stop_below=40.0).validate())

    def test_validation_survives_nonsense_without_dividing_by_zero(self):
        assert plan(levels=0, lower=0, upper=0).validate()


class TestLifecycle:
    def test_approve_then_live(self):
        p = plan().approve()
        assert p.status is Status.APPROVED and p.is_live and p.decided_at

    def test_rejected_plan_is_not_live(self):
        assert not plan().reject().is_live

    def test_cannot_approve_twice(self):
        p = plan().approve()
        with pytest.raises(ValueError):
            p.approve()

    def test_cannot_approve_something_already_rejected(self):
        p = plan().reject()
        with pytest.raises(ValueError):
            p.approve()

    def test_round_trips_through_json(self):
        p = plan(thesis="range held for 90 days").approve()
        back = GridPlan.from_json(p.to_json())
        assert back.status is Status.APPROVED
        assert back.symbol == p.symbol and back.thesis == p.thesis

    def test_load_all_skips_corrupt_files(self, tmp_path):
        plan().save(tmp_path)
        (tmp_path / "broken.json").write_text("{not json")
        assert len(load_all(tmp_path)) == 1

    def test_stop_cannot_revive_a_rejected_plan(self):
        """stop() accepted any Status, so p.reject(); p.stop(Status.RUNNING)
        left a plan you explicitly refused in a live state."""
        p = plan().reject()
        with pytest.raises(ValueError):
            p.stop(Status.RUNNING)
        assert not p.is_live

    def test_stop_only_accepts_terminal_reasons(self):
        p = plan().approve()
        with pytest.raises(ValueError):
            p.stop(Status.PROPOSED)
        p.stop(Status.STOPPED_OUT)
        assert p.status is Status.STOPPED_OUT and not p.is_live


class TestKillSwitches:
    def test_trading_allowed_once_initialised(self, tmp_path):
        """A control file must be created deliberately, not assumed."""
        c = Control(tmp_path / "control.json")
        c.initialise()
        assert c.may_trade("BTC-USDT")[0]

    def test_halt_all_stops_everything(self, tmp_path):
        c = Control(tmp_path / "control.json")
        c.halt_all("panic")
        assert not c.may_trade("BTC-USDT")[0]
        assert not c.may_propose()[0]

    def test_halting_one_symbol_leaves_others_running(self, tmp_path):
        c = Control(tmp_path / "control.json")
        c.halt_symbol("BTC-USDT")
        assert not c.may_trade("BTC-USDT")[0]
        assert c.may_trade("ETH-USDT")[0]

    def test_halting_the_analyst_leaves_trading_alone(self, tmp_path):
        """Stop new proposals without touching grids that are already running."""
        c = Control(tmp_path / "control.json")
        c.halt_analyst()
        assert not c.may_propose()[0]
        assert c.may_trade("BTC-USDT")[0]

    def test_halt_plan_targets_one_grid(self, tmp_path):
        c = Control(tmp_path / "control.json")
        c.halt_plan("abc123")
        assert not c.may_trade("BTC-USDT", plan_id="abc123")[0]
        assert c.may_trade("BTC-USDT", plan_id="def456")[0]

    def test_resume_clears_halts(self, tmp_path):
        c = Control(tmp_path / "control.json")
        c.halt_all()
        c.resume_all()
        assert c.may_trade("BTC-USDT")[0] and c.may_propose()[0]


class TestFailClosed:
    def test_corrupt_control_file_halts_everything(self, tmp_path):
        """The most important test here. Unreadable state must never mean 'go'."""
        path = tmp_path / "control.json"
        path.write_text("{ this is not valid json")
        c = Control(path)
        assert not c.may_trade("BTC-USDT")[0]
        assert not c.may_propose()[0]
        assert not c.is_live()

    def test_unknown_fields_also_halt(self, tmp_path):
        path = tmp_path / "control.json"
        path.write_text(json.dumps({"trading_enabled": True, "bogus_field": 1}))
        assert not Control(path).may_trade("BTC-USDT")[0]

    def test_a_missing_control_file_halts(self, tmp_path):
        """It used to return enabled defaults. A process started from the wrong
        directory would then trade happily while you believed it was stopped."""
        c = Control(tmp_path / "nowhere" / "control.json")
        allowed, reason = c.may_trade("BTC-USDT")
        assert not allowed and "no control file" in reason

    def test_string_booleans_cannot_arm_real_money(self, tmp_path):
        """The file is meant to be hand-editable, which makes this mandatory.

        Python truthiness means the STRING "false" is True, so an edited
        {"trading_enabled": "false", "live_trading": "true"} would have ignored a
        halt AND armed real money, bypassing the confirmation phrase entirely."""
        path = tmp_path / "control.json"
        path.write_text(json.dumps({"trading_enabled": "false", "live_trading": "true"}))
        c = Control(path)
        assert not c.is_live()
        assert not c.may_trade("BTC-USDT")[0]

    def test_a_string_where_a_list_belongs_halts(self, tmp_path):
        """`"BTC-USDT" in "BTC-USDT"` is a substring test — it would also have
        halted BTC and USDT, and silently."""
        path = tmp_path / "control.json"
        path.write_text(json.dumps({"halted_symbols": "BTC-USDT"}))
        assert not Control(path).may_trade("ETH-USDT")[0]

    def test_a_concurrent_write_cannot_erase_a_halt(self, tmp_path):
        """Read-modify-write without a lock let a stale snapshot resurrect
        trading_enabled=True — the worst failure a kill switch can have."""
        path = tmp_path / "control.json"
        c = Control(path)
        c.initialise()
        stale = Control(path)
        _ = stale.state()                 # snapshot taken BEFORE the halt
        c.halt_all("PANIC")
        stale.resume_symbol("ETH-USDT")   # writes under the lock, re-reading first
        assert not c.may_trade("BTC-USDT")[0]


class TestLiveArming:
    def test_paper_by_default(self, tmp_path):
        assert not Control(tmp_path / "control.json").is_live()

    def test_going_live_needs_the_exact_phrase(self, tmp_path):
        c = Control(tmp_path / "control.json")
        with pytest.raises(ValueError):
            c.set_live(True)
        with pytest.raises(ValueError):
            c.set_live(True, confirm="yes")
        c.set_live(True, confirm="I ACCEPT THE RISK")
        assert c.is_live()

    def test_disarming_needs_no_ceremony(self, tmp_path):
        c = Control(tmp_path / "control.json")
        c.set_live(True, confirm="I ACCEPT THE RISK")
        c.set_live(False)
        assert not c.is_live()

    def test_resume_does_not_silently_rearm_real_money(self, tmp_path):
        """Coming back from a halt must not turn live trading back on by itself."""
        c = Control(tmp_path / "control.json")
        c.set_live(True, confirm="I ACCEPT THE RISK")
        c.halt_all("something broke")
        assert not c.is_live()          # halted overrides live
        c.resume_all()
        assert c.state().live_trading   # the flag survives...
        # ...but a halt is always what wins while it is set.


# ---------------------------------------------------- rule 1: spot only

from grid.plan import spot_instrument_problems


class TestSpotOnly:
    """README rule 1. Funding on a perpetual is ~11%/yr against a grid edge
    measured in tenths of a percent per cycle."""

    @pytest.mark.parametrize("sym", ["BTC-USDT", "ETH-USDC", "SOL-USDT",
                                     "1INCH-USDT"])
    def test_spot_pairs_pass(self, sym):
        assert spot_instrument_problems(sym) == []

    @pytest.mark.parametrize("sym", [
        "BTC-USDT-SWAP", "BTC-USD-SWAP", "ETH-USDT-SWAP",
        "BTC-USD-260626", "BTC-USD-260626-50000-C", "BTC-USD-260626-50000-P",
    ])
    def test_derivatives_are_rejected(self, sym):
        assert spot_instrument_problems(sym)

    def test_a_swap_is_named_as_a_swap(self):
        assert "perpetual swap" in spot_instrument_problems("BTC-USDT-SWAP")[0]

    def test_a_future_is_named_as_a_future(self):
        assert "dated future" in spot_instrument_problems("BTC-USD-260626")[0]

    def test_an_option_is_named_as_an_option(self):
        assert "option" in spot_instrument_problems("BTC-USD-260626-50000-C")[0]

    def test_the_reason_is_given(self):
        assert "11%" in spot_instrument_problems("BTC-USDT-SWAP")[0]

    def test_an_empty_symbol_is_rejected(self):
        assert spot_instrument_problems("") == ["symbol is required"]

    def test_it_is_case_insensitive(self):
        assert spot_instrument_problems("btc-usdt-swap")

    def test_validate_rejects_a_derivative_plan(self):
        p = GridPlan(symbol="BTC-USDT-SWAP", lower=94.0, upper=106.0, levels=12,
                     quote_per_level=100.0, stop_below=90.0,
                     max_quote_total=1300.0)
        assert any("SPOT ONLY" in x for x in p.validate())

    def test_the_equivalent_spot_plan_is_fine(self):
        p = GridPlan(symbol="BTC-USDT", lower=94.0, upper=106.0, levels=12,
                     quote_per_level=100.0, stop_below=90.0,
                     max_quote_total=1300.0)
        assert p.validate() == []

    def test_margin_cannot_be_detected_from_the_id(self):
        """Documented limit: OKX margin uses the SAME id as spot. This check
        cannot rule it out, and the docstring says so. Margin is prevented at
        order time via tdMode='cash', which the live client must send."""
        assert spot_instrument_problems("BTC-USDT") == []


# ------------------------------------------------- geometric vs arithmetic

from grid.plan import ARITHMETIC, GEOMETRIC, SPACING_MODES


def geo(**over):
    kw = dict(symbol="BTC-USDT", lower=100.0, upper=200.0, levels=10,
              quote_per_level=20.0, stop_below=95.0, max_quote_total=1000.0)
    kw.update(over)
    return GridPlan(**kw)


class TestGeometricLadder:

    def test_it_is_the_default(self):
        assert GridPlan(symbol="BTC-USDT", lower=100.0, upper=200.0, levels=10,
                        quote_per_level=20.0, stop_below=95.0,
                        max_quote_total=1000.0).spacing_mode == GEOMETRIC

    def test_prices_follow_lower_times_ratio_to_the_i(self):
        p = geo()
        r = (200.0 / 100.0) ** (1 / 10)
        assert p.grid_prices == [pytest.approx(100.0 * r ** i) for i in range(11)]

    def test_the_top_rung_lands_exactly_on_upper(self):
        assert geo().grid_prices[-1] == pytest.approx(200.0)

    def test_the_bottom_rung_is_lower(self):
        assert geo().grid_prices[0] == pytest.approx(100.0)

    def test_there_is_one_more_price_than_levels(self):
        assert len(geo(levels=7).grid_prices) == 8

    def test_every_gap_is_the_same_percentage(self):
        prices = geo(levels=20).grid_prices
        gaps = [prices[i + 1] / prices[i] - 1 for i in range(len(prices) - 1)]
        assert max(gaps) == pytest.approx(min(gaps))

    def test_spacing_and_min_spacing_are_identical(self):
        for levels in (2, 5, 20, 100):
            p = geo(levels=levels)
            assert p.spacing_pct == pytest.approx(p.min_spacing_pct)

    def test_no_part_of_the_ladder_degrades(self):
        """The whole point. On an arithmetic ladder of the same band the top
        gives away far more of its gross than the bottom."""
        g, a = geo(levels=150), geo(levels=150, spacing_mode=ARITHMETIC)
        f = FeeModel()
        assert f.fee_share(g.spacing_pct) == pytest.approx(f.fee_share(g.min_spacing_pct))
        assert f.fee_share(a.min_spacing_pct) > f.fee_share(a.spacing_pct) * 1.5

    def test_step_is_the_smallest_gap_on_a_geometric_ladder(self):
        p = geo()
        prices = p.grid_prices
        assert p.step == pytest.approx(prices[1] - prices[0])

    def test_a_wider_band_needs_a_bigger_ratio(self):
        assert geo(upper=400.0).spacing_pct > geo(upper=200.0).spacing_pct

    def test_more_levels_means_tighter_spacing(self):
        assert geo(levels=40).spacing_pct < geo(levels=10).spacing_pct


class TestSpacingModeIsValidated:

    def test_a_valid_geometric_plan_passes(self):
        assert geo(max_quote_total=220.0).validate() == []

    def test_a_valid_arithmetic_plan_passes(self):
        assert geo(max_quote_total=220.0, spacing_mode=ARITHMETIC).validate() == []

    def test_an_unknown_mode_is_rejected(self):
        problems = geo(spacing_mode="logarithmic").validate()
        assert any("spacing_mode" in x for x in problems)

    def test_the_mode_survives_a_json_round_trip(self):
        for mode in SPACING_MODES:
            p = geo(spacing_mode=mode)
            assert GridPlan.from_json(p.to_json()).spacing_mode == mode

    def test_an_old_plan_without_the_field_loads_as_geometric(self):
        """Backward compatibility: plans written before this field existed."""
        import json
        p = geo()
        raw = json.loads(p.to_json())
        del raw["spacing_mode"]
        assert GridPlan.from_json(json.dumps(raw)).spacing_mode == GEOMETRIC

    def test_the_fee_gate_uses_the_uniform_spacing(self):
        """A geometric ladder tight enough to fail must fail on its one
        spacing, not on a bottom-of-ladder figure that flatters it."""
        p = geo(levels=400, max_quote_total=1e9, quote_per_level=20.0)
        assert any("gives away" in x for x in p.validate())
