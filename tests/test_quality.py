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
