"""The benchmarks are only useful if they are held to the engine's standards:
no lookahead, real costs, and the same fills at the next open."""
import pytest

from swingtrader.benchmark import buy_and_hold_index, momentum_rotation
from swingtrader.runtime import Dataset

CUT = "2018-07-04"          # a few sessions into a month, so fills fall before it
START = "2016-01-01"


def cut_at(dataset, date):
    series = {k: v.slice_to(date) for k, v in dataset.series.items()}
    return Dataset(series, dataset.index.slice_to(date), dataset.sectors, dataset.names,
                   synthetic=True)


@pytest.fixture(scope="module")
def truncated(dataset):
    return cut_at(dataset, CUT)


def test_buy_and_hold_is_the_index_rebased(dataset):
    r = buy_and_hold_index(dataset.index, START, "", 100000.0)
    idx = dict(zip(dataset.index.dates, dataset.index.close))
    base = idx[r.dates[0]]
    for d, e in zip(r.dates, r.equity):
        assert e == pytest.approx(100000.0 * idx[d] / base)


def test_rotation_cannot_see_the_future(cfg, dataset, truncated):
    full = momentum_rotation(dataset, cfg, START)
    trunc = momentum_rotation(truncated, cfg, START)
    a = {d: e for d, e in zip(full.dates, full.equity) if d <= CUT}
    b = dict(zip(trunc.dates, trunc.equity))
    assert len(a) > 300 and set(a) == set(b)
    for d in a:
        assert a[d] == pytest.approx(b[d], abs=1e-9), (
            f"rotation equity on {d} changed when future data was removed")
    assert full.trades > 20, "fixture too small to exercise rebalancing"


@pytest.mark.parametrize("ma", [0, 200])
def test_each_decision_reads_nothing_after_its_date(cfg, dataset, ma):
    """The equity comparison above cannot see a month-end decision that peeks
    a few sessions ahead: its effect lands after the cut. So re-make every
    decision on data truncated at its own date, and demand the same answer."""
    from swingtrader.benchmark import rotation_picks
    full = momentum_rotation(dataset, cfg, START, ma_filter=ma)
    assert len(full.picks) > 20
    for d, names in full.picks.items():
        slot = full.equity[full.dates.index(d)] / 10
        again = rotation_picks(cut_at(dataset, d), d, slot=slot,
                               min_turnover=cfg.get("universe.min_turnover_cr") * 1e7,
                               min_history=cfg.get("universe.min_history_bars"),
                               ma_filter=ma)
        assert again == names, f"the {d} decision changed when later data was removed"
    if ma:
        assert any(full.picks.values()) and not all(full.picks.values()), \
            "fixture never switched the filter both ways"


def test_every_filter_reads_nothing_after_the_decision_date(dataset):
    """On synthetic data every name clears the liquidity floor, so a filter that
    peeked would never change an answer above. Make each one bind - turnover
    floor at the median name, slot below the dearest third - and re-check."""
    from swingtrader.benchmark import rotation_picks
    from swingtrader.util import median
    dates = [d for d in dataset.index.dates if d >= START][::40][:12]
    assert len(dates) == 12
    for d in dates:
        cut = cut_at(dataset, d)
        traded, closes = [], []
        for s in cut.series.values():
            i = s.pos(d)
            if i is not None and i >= 20:
                traded.append(median([s.close[k] * s.volume[k] for k in range(i - 19, i + 1)]))
                closes.append(s.close[i])
        traded.sort(); closes.sort()
        kw = dict(min_turnover=traded[len(traded) // 2],
                  slot=closes[2 * len(closes) // 3], min_history=200)
        picks = rotation_picks(cut, d, **kw)
        assert picks, f"filters left nothing on {d}"
        assert rotation_picks(dataset, d, **kw) == picks, (
            f"the {d} decision changed when later data was removed")


def test_rotation_pays_costs(cfg, dataset):
    zero = cfg.with_overrides({f"costs.{k}": 0.0 for k in (
        "brokerage_pct", "brokerage_cap", "brokerage_min", "stt_buy_pct", "stt_sell_pct",
        "exchange_txn_pct", "sebi_pct", "stamp_duty_buy_pct", "dp_charge_sell",
        "ipft_pct", "slippage_bps")})
    paid = momentum_rotation(dataset, cfg, START)
    free = momentum_rotation(dataset, zero, START)
    assert paid.costs > 0 and free.costs == 0
    assert paid.equity[-1] < free.equity[-1]


def test_trend_filter_sits_in_cash_below_the_average(cfg, dataset):
    """With the filter on, a month-end close under the index's 200-DMA must
    leave the book entirely in cash for the following month."""
    from swingtrader import indicators as ind
    r = momentum_rotation(dataset, cfg, START, ma_filter=200)
    idx = dataset.index
    sma = ind.sma(idx.close, 200)
    dates = r.dates
    checked = 0
    for k in range(len(dates) - 1):
        d = dates[k]
        if d[:7] == dates[k + 1][:7]:
            continue                                  # not a month-end
        i = idx.pos(d)
        if sma[i] == sma[i] and idx.close[i] < sma[i]:
            # flat from the next session's close to the following month-end
            nxt = [j for j in range(k + 2, len(dates)) if dates[j][:7] == dates[k + 1][:7]]
            if len(nxt) > 1:
                flat = {round(r.equity[j], 6) for j in nxt}
                assert len(flat) == 1, f"book not in cash in the month after {d}"
                checked += 1
    assert checked >= 1, "fixture never closed a month below its 200-DMA"
