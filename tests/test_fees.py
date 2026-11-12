"""Tests for the fee model. This is the constraint the whole strategy rests on."""

import pytest

from grid.fees import OKX_SPOT_MAKER, OKX_SPOT_TAKER, FeeModel


@pytest.fixture
def okx():
    return FeeModel()


class TestBreakeven:
    def test_okx_maker_breakeven_is_about_16_basis_points(self, okx):
        """(1+f)/(1-f)-1 with f=0.0008 -> 0.16%."""
        assert okx.breakeven_spacing(maker=True) == pytest.approx(0.0016013, abs=1e-6)

    def test_okx_taker_breakeven_is_about_20_basis_points(self, okx):
        assert okx.breakeven_spacing(maker=False) == pytest.approx(0.0020020, abs=1e-6)

    def test_taker_costs_more_than_maker(self, okx):
        assert okx.breakeven_spacing(False) > okx.breakeven_spacing(True)

    def test_zero_fees_means_zero_breakeven(self):
        assert FeeModel(maker=0.0, taker=0.0).breakeven_spacing() == pytest.approx(0.0)

    def test_at_breakeven_spacing_profit_is_zero(self, okx):
        """The definition, checked directly rather than assumed."""
        assert okx.cycle_profit_pct(okx.breakeven_spacing()) == pytest.approx(0, abs=1e-9)


class TestCycleProfit:
    def test_spacing_below_breakeven_loses_money(self, okx):
        assert okx.cycle_profit_pct(0.001) < 0

    def test_spacing_above_breakeven_makes_money(self, okx):
        assert okx.cycle_profit_pct(0.008) > 0

    def test_eight_tenths_percent_spacing_nets_about_64_bps(self, okx):
        """0.8% gross, ~0.16% to fees, ~0.64% kept."""
        assert okx.cycle_profit_pct(0.008) == pytest.approx(0.00639, abs=2e-5)

    def test_profit_is_monotonic_in_spacing(self, okx):
        prev = -1.0
        for s in (0.002, 0.004, 0.008, 0.016):
            cur = okx.cycle_profit_pct(s)
            assert cur > prev
            prev = cur


class TestFeeShare:
    def test_tight_spacing_hands_most_profit_to_the_exchange(self, okx):
        """At 0.2% spacing the exchange takes ~80% of the gross."""
        assert okx.fee_share(0.002) > 0.75

    def test_wide_spacing_keeps_most_of_it(self, okx):
        assert okx.fee_share(0.02) < 0.10

    def test_min_viable_spacing_hits_its_target_share(self, okx):
        s = okx.min_viable_spacing(fee_share_of_gross=0.25)
        assert okx.fee_share(s) == pytest.approx(0.25, abs=0.005)

    def test_rejects_nonsense_share(self, okx):
        with pytest.raises(ValueError):
            okx.min_viable_spacing(fee_share_of_gross=1.5)

    def test_rejects_non_positive_spacing(self, okx):
        with pytest.raises(ValueError):
            okx.fee_share(0.0)


class TestLegs:
    def test_buy_costs_more_than_notional(self, okx):
        assert okx.buy_cost(100.0, 1.0) == pytest.approx(100.08)

    def test_sell_returns_less_than_notional(self, okx):
        assert okx.sell_proceeds(100.0, 1.0) == pytest.approx(99.92)

    def test_round_trip_at_flat_price_loses_both_fees(self, okx):
        """Buying and selling at the same price is not free — it costs ~0.16%."""
        loss = okx.buy_cost(100, 1) - okx.sell_proceeds(100, 1)
        assert loss == pytest.approx(0.16, abs=1e-9)


class TestRecovery:
    def test_a_20_percent_drawdown_needs_many_clean_cycles(self, okx):
        """The argument for a stop-loss, in one number."""
        n = okx.cycles_to_recover(0.20, spacing=0.008)
        assert 30 < n < 33

    def test_unprofitable_spacing_never_recovers(self, okx):
        assert okx.cycles_to_recover(0.10, spacing=0.001) == float("inf")


class TestValidation:
    @pytest.mark.parametrize("bad", [-0.01, 0.5, 1.0])
    def test_rejects_impossible_fee_rates(self, bad):
        with pytest.raises(ValueError):
            FeeModel(maker=bad)

    def test_okx_constants_are_what_we_documented(self):
        assert (OKX_SPOT_MAKER, OKX_SPOT_TAKER) == (0.0008, 0.0010)
