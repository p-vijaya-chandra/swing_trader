"""Data quality checks.

Each test plants one defect a real vendor actually produces and asserts it is
caught, plus a clean-data test asserting no false positives. False positives
matter as much as misses here: a validator that cries wolf on good data gets
ignored, and then it catches nothing.
"""
import pytest

from swingtrader.data.models import Series
from swingtrader.data.quality import ERROR, check_dataset, check_symbol


def rows(s):
    return [list(r) for r in s.to_rows()]


@pytest.fixture(scope="module")
def sample(dataset):
    """A dozen clean symbols plus the index. DONOR is one of them, copied and
    corrupted by each test so the original stays available as a control."""
    picked = {k: dataset.series[k] for k in sorted(dataset.series)[:12]}
    return picked, dataset.index


@pytest.fixture(scope="module")
def donor(sample):
    return sorted(sample[0])[0]


def kinds(report, symbol="TARGET"):
    return {i.kind for i in report.issues if i.symbol == symbol}


def build(sample, corrupt=None):
    clean, index = sample
    d = {k: Series.from_rows(k, rows(v)) for k, v in clean.items()}
    if corrupt:
        corrupt(d, clean)
    return check_dataset(d, index, min_bars=260)


def test_clean_data_raises_nothing(sample):
    rep = build(sample)
    assert not rep.errors, f"false positives on clean data: {[str(i) for i in rep.errors]}"
    assert not rep.warnings, f"spurious warnings: {[str(i) for i in rep.warnings]}"


def test_unadjusted_bonus_is_caught(sample, donor):
    """The one that matters most: a 1:2 bonus reads as a -50% day."""
    def corrupt(d, clean, donor=donor):
        r = rows(clean[donor])
        for k in range(len(r) // 2, len(r)):
            for j in range(1, 5):
                r[k][j] /= 2.0
        d["TARGET"] = Series.from_rows("TARGET", r)
    rep = build(sample, corrupt)
    assert "unadjusted_split" in kinds(rep)
    assert any(i.severity == ERROR and i.kind == "unadjusted_split" for i in rep.issues)


def test_split_ratios_other_than_half(sample, donor):
    for divisor in (2.0, 5.0, 10.0):
        def corrupt(d, clean, _div=divisor, donor=donor):
            r = rows(clean[donor])
            for k in range(len(r) // 2, len(r)):
                for j in range(1, 5):
                    r[k][j] /= _div
            d["TARGET"] = Series.from_rows("TARGET", r)
        assert "unadjusted_split" in kinds(build(sample, corrupt)), f"1:{divisor:g} missed"


def test_a_real_market_crash_is_not_flagged_as_a_split(sample, donor):
    """The discriminator is the index. If the whole market fell, a big single-day
    drop is a market event and must not be reported as bad data."""
    clean, index = sample
    d = {k: Series.from_rows(k, rows(v)) for k, v in clean.items()}
    idx_rows = rows(index)
    r = rows(clean[donor])
    hit = len(r) // 2
    date = r[hit][0]
    for row in r[hit:]:                         # stock halves, and stays halved ...
        for j in range(1, 5):
            row[j] *= 0.5
    d["TARGET"] = Series.from_rows("TARGET", r)
    for row in idx_rows:                        # ... and so does the index
        if row[0] >= date:
            for j in range(1, 5):
                row[j] *= 0.5
    rep = check_dataset(d, Series.from_rows("IDX", idx_rows), min_bars=260)
    assert "unadjusted_split" not in kinds(rep), "flagged a genuine market-wide crash"


def test_one_bar_price_glitch_is_reported(sample, donor):
    """A price that halves and immediately doubles back is a bad print, not a
    corporate action, and must not pass silently."""
    clean, index = sample
    d = {k: Series.from_rows(k, rows(v)) for k, v in clean.items()}
    r = rows(clean[donor])
    hit = len(r) // 2
    for j in range(1, 5):
        r[hit][j] *= 0.5
    d["TARGET"] = Series.from_rows("TARGET", r)
    rep = check_dataset(d, index, min_bars=260)
    assert kinds(rep) & {"unadjusted_split", "extreme_move"}


def test_index_own_moves_are_never_called_splits(sample):
    """Regression: the index is checked with no benchmark to compare against, so
    every large index move landed on a split ratio and was reported as bad data."""
    clean, index = sample
    idx_rows = rows(index)
    hit = len(idx_rows) // 2
    for row in idx_rows[hit:]:
        for j in range(1, 5):
            row[j] *= 0.5
    rep = check_dataset(clean, Series.from_rows("IDX", idx_rows), min_bars=260)
    assert "unadjusted_split" not in kinds(rep, "(index)")


def test_missing_sessions_are_caught(sample, donor):
    def corrupt(d, clean, donor=donor):
        d["TARGET"] = Series.from_rows(
            "TARGET", [x for k, x in enumerate(rows(clean[donor])) if k % 7])
    assert "missing_sessions" in kinds(build(sample, corrupt))


def test_frozen_bars_are_caught(sample, donor):
    def corrupt(d, clean, donor=donor):
        r = rows(clean[donor])
        for k in range(100, 125):
            r[k][1:5] = r[99][1:5]
        d["TARGET"] = Series.from_rows("TARGET", r)
    rep = build(sample, corrupt)
    assert "frozen_bars" in kinds(rep)
    assert any(i.kind == "frozen_bars" and i.severity == ERROR for i in rep.issues)


def test_short_history_is_caught(sample, donor):
    def corrupt(d, clean, donor=donor):
        d["TARGET"] = Series.from_rows("TARGET", rows(clean[donor])[-150:])
    assert "short_history" in kinds(build(sample, corrupt))


def test_stale_cache_is_caught(sample, donor):
    def corrupt(d, clean, donor=donor):
        d["TARGET"] = Series.from_rows("TARGET", rows(clean[donor])[:-40])
    assert "stale_cache" in kinds(build(sample, corrupt))


def test_missing_volume_is_caught(sample, donor):
    def corrupt(d, clean, donor=donor):
        r = rows(clean[donor])
        for k in range(len(r) // 2):
            r[k][5] = 0
        d["TARGET"] = Series.from_rows("TARGET", r)
    assert "missing_volume" in kinds(build(sample, corrupt))


def test_missing_index_is_an_error(sample):
    clean, _ = sample
    rep = check_dataset(clean, None, min_bars=260)
    assert any(i.kind == "index_missing" and i.severity == ERROR for i in rep.issues)


def test_uncached_universe_symbols_are_reported(sample):
    clean, index = sample
    rep = check_dataset(clean, index, min_bars=260,
                        universe=list(clean) + ["GHOST1", "GHOST2"])
    assert any(i.kind == "symbols_not_cached" for i in rep.issues)


def test_bad_symbols_lists_only_errors(sample, donor):
    def corrupt(d, clean, donor=donor):
        d["TARGET"] = Series.from_rows("TARGET", rows(clean[donor])[-150:])
    rep = build(sample, corrupt)
    assert "TARGET" in rep.bad_symbols()
    assert all(any(i.symbol == s and i.severity == ERROR for i in rep.issues)
               for s in rep.bad_symbols())


def test_empty_series_does_not_crash():
    issues = check_symbol("X", Series("X", [], [], [], [], [], []), {}, [])
    assert issues and issues[0].kind == "empty"


# ------------------------------------------- lessons from the first real data
def test_an_up_surge_on_a_split_ratio_is_a_warning_not_an_error(sample, donor):
    """Real data, 2017-10-25: PNB rose 46% when PSU bank recapitalisation was
    announced, landing near a 3:2 ratio while the index barely moved. Only a
    DROP can be an unadjusted split or bonus; an up-move is news until proven
    otherwise and must not knock a stock out of the universe."""
    from swingtrader.data.quality import _near_split_ratio
    assert _near_split_ratio(1.46) == 1.5, "fixture must land on a split ratio"

    def corrupt(d, clean, donor=donor):
        r = rows(clean[donor])
        mid = len(r) // 2
        # scale so the jump-day close is EXACTLY 1.46x the prior close - scaling
        # by a flat 1.46 would fold that day's natural return into the ratio
        # and could miss the split-ratio branch this test exists to exercise
        f = 1.46 * r[mid - 1][4] / r[mid][4]
        for k in range(mid, len(r)):
            for j in range(1, 5):
                r[k][j] *= f
        d["TARGET"] = Series.from_rows("TARGET", r)
    rep = build(sample, corrupt)
    assert "unadjusted_split" not in kinds(rep)
    assert "extreme_move" in kinds(rep)
    assert "TARGET" not in rep.bad_symbols()


def test_index_volume_is_not_checked(sample):
    """Yahoo's Nifty 100 series often has no volume, and the strategy never uses
    index volume - so it must not raise an error on the index."""
    clean, index = sample
    idx = [list(r) for r in index.to_rows()]
    for r in idx:
        r[5] = 0
    rep = check_dataset(clean, Series.from_rows("IDX", idx), min_bars=260)
    assert not [i for i in rep.issues if i.symbol == "(index)" and i.kind == "missing_volume"]


def _with_holiday(sample, date):
    """Every stock gets a flat, zero-volume bar on `date` - the shape of the
    election-day bars Yahoo inserted on 2014-04-24 and 2014-10-15."""
    clean, index = sample
    out = {}
    for k, v in clean.items():
        r = [list(x) for x in v.to_rows()]
        for x in r:
            if x[0] == date:
                x[5] = 0
        out[k] = Series.from_rows(k, r)
    return out, index


def test_vendor_filled_holidays_are_reported_once_not_per_symbol(sample):
    clean, index = sample
    day = sorted(next(iter(clean.values())).dates)[400]
    series, idx = _with_holiday(sample, day)
    from swingtrader.data.quality import phantom_sessions
    # needs 20+ symbols to call it market-wide; widen the sample by copying
    wide = {f"{k}{n}": v for n in range(2) for k, v in series.items()}
    assert phantom_sessions(wide) == [day]
    rep = check_dataset(wide, idx, min_bars=260)
    assert [i.kind for i in rep.issues if i.symbol == "(market)"] == ["phantom_sessions"]
    assert not [i for i in rep.issues if i.kind == "missing_volume"], \
        "a market holiday must not produce a missing-volume warning per symbol"


def test_one_stock_with_zero_volume_is_not_a_holiday(sample, donor):
    from swingtrader.data.quality import phantom_sessions
    clean, _ = sample
    wide = {f"{k}{n}": v for n in range(2) for k, v in clean.items()}
    r = rows(clean[donor])
    r[400][5] = 0
    wide["TARGET"] = Series.from_rows("TARGET", r)
    assert phantom_sessions(wide) == []


def _cache(tmp_path, cfg, series_map, index):
    from swingtrader.data.store import DataStore
    store = DataStore(str(tmp_path / "cache"))
    for s in series_map.values():
        store.save(s)
    store.save(Series.from_rows(cfg.get("regime.index_symbol"), index.to_rows()))
    return cfg.with_overrides({"data.cache_dir": str(tmp_path / "cache"),
                               "universe.exclude_file": str(tmp_path / "excluded.txt")})


def test_loader_drops_vendor_filled_holidays(tmp_path, cfg, dataset):
    from swingtrader.runtime import Dataset
    syms = sorted(dataset.series)[:30]
    day = dataset.series[syms[0]].dates[500]
    series = {}
    for k in syms:
        r = [list(x) for x in dataset.series[k].to_rows()]
        for x in r:
            if x[0] == day:
                x[5] = 0
        series[k] = Series.from_rows(k, r)
    c = _cache(tmp_path, cfg, series, dataset.index)
    ds = Dataset.load(c)
    assert all(day not in s.idx for s in ds.series.values())
    assert day not in ds.index.idx


def test_loader_honours_the_exclusions_file(tmp_path, cfg, dataset):
    """Regression: validate-data told you to write state/excluded.txt, and
    nothing ever read it - following its advice changed no backtest."""
    from swingtrader.runtime import Dataset
    syms = sorted(dataset.series)[:25]
    c = _cache(tmp_path, cfg, {k: dataset.series[k] for k in syms}, dataset.index)
    (tmp_path / "excluded.txt").write_text(
        f"# symbols failing swing validate-data\n{syms[0]}\n{syms[1]}  # demerger\n")
    ds = Dataset.load(c)
    assert syms[0] not in ds.series and syms[1] not in ds.series
    assert syms[2] in ds.series
