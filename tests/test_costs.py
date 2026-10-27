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


def test_backtest_charges_slippage_exactly_once(cfg, dataset):
    """Regression: the engine deducted cb.total - which carries slippage as a
    line item - from fills that already had slippage applied, so every round
    trip paid it twice (1.08% instead of 0.84% at a Rs 17k position).

    Checked against the real engine: each trade's recorded costs must equal the
    explicit charges on its two fills. Those fill prices already include
    slippage, so any slippage inside `costs` is a second payment."""
    from swingtrader.runtime import run_backtest
    res = run_backtest(cfg, dataset, start="2016-01-01")
    cm = CostModel(cfg)
    checked = 0
    for t in res.trades:
        if t.exit_reason.startswith("scale"):
            continue
        explicit = (cm.charges(t.entry_price, t.qty, "BUY").total
                    + cm.charges(t.exit_price, t.qty, "SELL").total)
        assert t.costs == pytest.approx(explicit, rel=1e-6), (
            f"{t.symbol} {t.entry_date}: booked {t.costs:.2f} but explicit charges "
            f"are {explicit:.2f} - slippage is being paid again")
        checked += 1
    assert checked > 20
    # the cost REPORT still shows slippage, once, as its own line
    assert res.costs_breakdown.get("slippage", 0) > 0


def test_trade_pnl_reconciles_with_the_cash_ledger(cfg, dataset):
    """Closed trades plus what is still open must add up to the equity change.

    Each trade's net_pnl used to deduct only the exit leg's charges. The cash
    ledger paid both legs, so the equity curve was right while every per-trade
    statistic - win rate, profit factor, expectancy in R, and the learning
    loop's inputs - was flattered by the buy-side charges.
    """
    from swingtrader.runtime import run_backtest
    res = run_backtest(cfg, dataset, start="2016-01-01")
    assert len(res.trades) > 20
    closed = sum(t.net_pnl for t in res.trades)
    still_open = sum(p["unrealised"] + p["realised"] - p["costs_paid"]
                     for p in res.open_positions)
    change = res.end_equity - res.start_equity
    assert abs(change - (closed + still_open)) < 1.0, (change, closed, still_open)
    for t in res.trades:
        assert abs(t.net_pnl - (t.gross_pnl - t.costs)) < 1e-6
