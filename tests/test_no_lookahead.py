"""The most important test in the suite.

If the engine can see even one bar into the future, every performance number it
produces is fiction, and the failure is silent - a lookahead backtest looks
*better*, not broken.

The test: truncate the entire dataset at a cut date and re-run. Everything that
happened before the cut had no access to data after it, so if the engine is
honest the equity curve and the trade list up to the cut must be bit-identical.
If any component peeks ahead, the two runs diverge.
"""
import pytest

from swingtrader.backtest import BacktestEngine
from swingtrader.features import build_features
from swingtrader.regime import RegimeModel
from swingtrader.runtime import Dataset, run_backtest

CUT = "2018-06-29"


@pytest.fixture(scope="module")
def truncated(cfg, dataset):
    series = {k: v.slice_to(CUT) for k, v in dataset.series.items()}
    index = dataset.index.slice_to(CUT)
    return Dataset(series, index, dataset.sectors, dataset.names, synthetic=True)


def test_equity_curve_identical_before_cut(cfg, dataset, truncated):
    full = run_backtest(cfg, dataset, start="2016-01-01")
    trunc = run_backtest(cfg, truncated, start="2016-01-01")

    a = {d.date: d.equity for d in full.daily if d.date <= CUT}
    b = {d.date: d.equity for d in trunc.daily if d.date <= CUT}

    assert a, "no sessions before the cut - fixture is wrong"
    assert set(a) == set(b), "the two runs cover different sessions"
    for date in a:
        assert a[date] == pytest.approx(b[date], abs=1e-9), (
            f"equity on {date} changed when future data was removed: "
            f"{a[date]} vs {b[date]} - something is reading ahead")


def test_trades_identical_before_cut(cfg, dataset, truncated):
    full = run_backtest(cfg, dataset, start="2016-01-01")
    trunc = run_backtest(cfg, truncated, start="2016-01-01")

    def key(ts):
        return [(t.symbol, t.entry_date, t.exit_date, round(t.qty, 6),
                 round(t.net_pnl, 6), t.exit_reason)
                for t in ts if t.exit_date <= CUT]

    assert key(full.trades) == key(trunc.trades)


def test_features_do_not_change_when_future_is_removed(cfg, dataset, truncated):
    """Indicators must be causal too - a rolling window that accidentally
    centres itself would pass the equity test only by luck."""
    ff = build_features(dataset.series, dataset.index, dataset.sectors, cfg)
    ft = build_features(truncated.series, truncated.index, truncated.sectors, cfg)
    sym = sorted(ff)[0]
    a, b = ff[sym], ft[sym]
    i = a.series.pos(CUT)
    j = b.series.pos(CUT)
    assert i is not None and j is not None
    for col in ("ema_fast", "ema_slow", "atr", "mom", "adx", "roc_126",
                "donchian_high", "hh_52w", "rs_index"):
        va, vb = a.get(col, i), b.get(col, j)
        if va != va and vb != vb:      # both NaN
            continue
        assert va == pytest.approx(vb, rel=1e-9), f"{col} differs at the cut"


def test_regime_labels_do_not_change(cfg, dataset, truncated):
    ff = build_features(dataset.series, dataset.index, dataset.sectors, cfg)
    ft = build_features(truncated.series, truncated.index, truncated.sectors, cfg)
    ra = RegimeModel(cfg); ra.build(dataset.index, ff, dataset.all_dates())
    rb = RegimeModel(cfg); rb.build(truncated.index, ft, truncated.all_dates())
    checked = 0
    for d in truncated.all_dates():
        if d > CUT:
            continue
        assert ra.at(d).label == rb.at(d).label, f"regime label changed on {d}"
        checked += 1
    assert checked > 100
