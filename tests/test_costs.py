"""Cost model. These numbers decide whether the strategy is viable at Rs 1L,
so they get pinned down rather than assumed."""
import pytest

from swingtrader.costs import CostModel


def test_brokerage_takes_the_lower_of_percent_and_cap(cfg):
    cm = CostModel(cfg)
    assert cm.brokerage(10000) == pytest.approx(10.0)     # 0.1% = 10 < 20
    assert cm.brokerage(50000) == pytest.approx(20.0)     # 0.1% = 50, capped at 20
    assert cm.brokerage(0) == 0.0


def test_dp_charge_only_on_the_sell(cfg):
    cm = CostModel(cfg)
    assert cm.charges(100.0, 100, "BUY").dp == 0.0
    assert cm.charges(100.0, 100, "SELL").dp > 0.0


def test_stamp_duty_only_on_the_buy(cfg):
    cm = CostModel(cfg)
    assert cm.charges(100.0, 100, "BUY").stamp > 0.0
    assert cm.charges(100.0, 100, "SELL").stamp == 0.0


def test_slippage_always_works_against_you(cfg):
    cm = CostModel(cfg)
    assert cm.fill_price(100.0, "BUY") > 100.0
    assert cm.fill_price(100.0, "SELL") < 100.0


def test_round_trip_cost_falls_with_position_size(cfg):
    """The flat DP fee is why small positions are disproportionately expensive -
    this monotonicity is the entire argument for fewer, larger positions."""
    cm = CostModel(cfg)
    sizes = [5000, 10000, 20000, 50000, 200000]
    costs = [cm.round_trip_pct(s) for s in sizes]
    assert costs == sorted(costs, reverse=True)
    assert costs[0] > 0.010, "a Rs 5,000 position should cost over 1% to round trip"
    assert costs[-1] < 0.006


def test_round_trip_at_the_configured_position_size(cfg):
    """Regression pin: if this moves, the low-turnover defaults need revisiting."""
    cm = CostModel(cfg)
    pv = cfg.get("capital") / cfg.get("risk.max_positions")
    rt = cm.round_trip_pct(pv)
    assert 0.007 < rt < 0.011, f"round trip is {100*rt:.2f}%, expected ~0.84%"


def test_zero_quantity_costs_nothing(cfg):
    cm = CostModel(cfg)
    assert cm.charges(100.0, 0, "BUY").total == 0.0


def test_gst_applies_to_brokerage_not_to_stt(cfg):
    cm = CostModel(cfg)
    cb = cm.charges(100.0, 100, "BUY")
    expected_gst = (cb.brokerage + cb.exchange + cb.sebi) * cfg.get("costs.gst_pct")
    assert cb.gst == pytest.approx(expected_gst)
