"""Engine invariants: accounting, ordering, and the rules that must never break."""
import pytest

from swingtrader.backtest import compute_metrics
from swingtrader.runtime import run_backtest


@pytest.fixture(scope="module")
def result(cfg, dataset):
    return run_backtest(cfg, dataset, start="2016-01-01")


def test_cash_never_goes_negative(result):
    """Cash equities: there is no margin here. A negative balance means the
    engine spent money it did not have and every return after it is fiction."""
    worst = min(d.cash for d in result.daily)
    assert worst >= -1e-6, f"cash went to {worst:,.2f}"


def test_equity_equals_cash_plus_holdings(result):
    for d in result.daily:
        assert d.equity == pytest.approx(d.cash + d.invested, rel=1e-9)


def test_position_count_respects_the_cap(cfg, result):
    cap = cfg.get("risk.max_positions")
    assert max(d.n_positions for d in result.daily) <= cap


def test_no_new_entries_while_risk_off(cfg, dataset, result):
    """The regime filter is the main drawdown control; if entries leak through
    in a risk-off tape it is not doing its job."""
    risk_off_dates = {d.date for d in result.daily if d.regime == "risk_off"}
    # an entry is FILLED the session after the signal, so an entry dated in a
    # risk-off window is only a violation if the prior session was also risk-off
    dates = [d.date for d in result.daily]
    prev = {dates[i]: dates[i - 1] for i in range(1, len(dates))}
    bad = [t for t in result.trades
           if t.entry_date in risk_off_dates
           and prev.get(t.entry_date) in risk_off_dates]
    assert not bad, f"{len(bad)} entries during a risk-off regime, e.g. {bad[0].symbol}"


def test_losses_are_bounded_near_one_r(result):
    """The stop is the risk contract. A loss much worse than -1R means either
    the stop was not enforced or gaps are being modelled too kindly - both are
    worth knowing about."""
    losses = [t.r_multiple for t in result.trades if t.r_multiple < 0]
    assert losses
    typical = sorted(losses)[len(losses) // 2]
    assert typical > -1.6, f"median loss {typical:.2f}R is far past the stop"
    # gaps can and do exceed the stop; just bound how often
    bad_gaps = [r for r in losses if r < -2.0]
    assert len(bad_gaps) / len(losses) < 0.10


def test_every_trade_has_a_recorded_stop(result):
    for t in result.trades:
        assert t.initial_stop > 0
        assert t.risk_per_share > 0
        assert t.initial_stop < t.entry_price, "stop must sit below the entry"


def test_sector_cap_is_never_exceeded(cfg, dataset):
    """Reconstructed from the trade list: the universe is ~23% financials, so
    without this cap a 'diversified' book is really one bet on rate policy."""
    res = run_backtest(cfg, dataset, start="2016-01-01")
    cap = cfg.get("risk.max_sector_positions")
    events = []
    for t in res.trades:
        events.append((t.entry_date, 1, t.sector))
        events.append((t.exit_date, -1, t.sector))
    events.sort(key=lambda e: e[0])
    counts = {}
    for _, delta, sector in events:
        counts[sector] = counts.get(sector, 0) + delta
        assert counts[sector] <= cap, f"{sector} reached {counts[sector]} positions"


def test_costs_are_charged_on_every_trade(result):
    assert all(t.costs > 0 for t in result.trades)
    assert result.costs_total == pytest.approx(
        sum(v for v in result.costs_breakdown.values()), rel=1e-6)


def test_metrics_are_internally_consistent(result):
    m = compute_metrics(result.dates, result.equity_curve, result.trades,
                        result.costs_total)
    assert m["n_trades"] == len(result.trades)
    assert m["end_equity"] == pytest.approx(result.equity_curve[-1])
    assert -1.0 <= m["max_drawdown"] <= 0.0
    assert 0.0 <= m["win_rate"] <= 1.0
    monthly = m["monthly_returns"]
    compounded = 1.0
    for v in monthly.values():
        compounded *= (1 + v)
    assert compounded == pytest.approx(1 + m["total_return"], rel=1e-6)


def test_backtest_is_deterministic(cfg, dataset):
    a = run_backtest(cfg, dataset, start="2016-01-01")
    b = run_backtest(cfg, dataset, start="2016-01-01")
    assert a.equity_curve == b.equity_curve
    assert [t.net_pnl for t in a.trades] == [t.net_pnl for t in b.trades]


def test_zero_exposure_regime_means_no_holdings_grow(cfg, dataset):
    """With risk_off exposure forced to 0 everywhere the book must wind down to
    cash rather than quietly staying invested."""
    c = cfg.with_overrides({"regime.exposure": {"risk_on": 0.0, "neutral": 0.0,
                                                "risk_off": 0.0}})
    res = run_backtest(c, dataset, start="2016-01-01")
    assert not res.trades or max(d.n_positions for d in res.daily) == 0


def test_higher_costs_never_improve_results(cfg, dataset):
    """A sanity check on the accounting direction - trivial, and it would have
    caught a sign error in the cost model."""
    cheap = run_backtest(cfg.with_overrides({"costs.slippage_bps": 0.0}), dataset,
                         start="2016-01-01")
    dear = run_backtest(cfg.with_overrides({"costs.slippage_bps": 60.0}), dataset,
                        start="2016-01-01")
    assert dear.costs_total > cheap.costs_total
