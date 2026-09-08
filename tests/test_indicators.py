"""Indicator correctness, especially the optimised paths."""
import math
import random

import pytest

from swingtrader import indicators as I
from swingtrader.util import is_na, linreg_slope_r2


@pytest.fixture(scope="module")
def series():
    rng = random.Random(3)
    c = [100.0]
    for _ in range(800):
        c.append(max(1.0, c[-1] * math.exp(rng.gauss(0.0004, 0.014))))
    h = [x * (1 + abs(rng.gauss(0, 0.006))) for x in c]
    l = [x * (1 - abs(rng.gauss(0, 0.006))) for x in c]
    return c, h, l


def test_ema_nan_padding_then_values(series):
    c, _, _ = series
    e = I.ema(c, 20)
    assert all(is_na(v) for v in e[:19]), "EMA must be NaN until it has n bars"
    assert not is_na(e[19])
    assert len(e) == len(c)


def test_ema_seed_is_the_sma(series):
    c, _, _ = series
    e = I.ema(c, 20)
    assert e[19] == pytest.approx(sum(c[:20]) / 20)


def test_rolling_max_matches_naive(series):
    _, h, _ = series
    fast = I.rolling_max(h, 252)
    for i in range(len(h)):
        if i < 251:
            assert is_na(fast[i])
        else:
            assert fast[i] == pytest.approx(max(h[i - 251:i + 1]))


def test_rolling_min_matches_naive(series):
    _, _, l = series
    fast = I.rolling_min(l, 40)
    for i in range(39, len(l)):
        assert fast[i] == pytest.approx(min(l[i - 39:i + 1]))


def test_donchian_excludes_today(series):
    """The classic silent lookahead bug: including today's high in the window
    the breakout is compared against makes a breakout impossible to detect."""
    _, h, _ = series
    d = I.donchian_high(h, 20)
    for i in range(20, len(h)):
        assert d[i] == pytest.approx(max(h[i - 20:i])), "must exclude bar i"
    # a new high today must therefore be strictly above the donchian value
    spike = list(h)
    spike[100] = max(h[80:100]) * 1.5
    assert spike[100] > I.donchian_high(spike, 20)[100]


def test_incremental_slope_r2_matches_naive(series):
    c, _, _ = series
    n = 90
    fast = I.annualised_slope_r2(c, n)
    for i in range(n - 1, len(c), 37):
        win = [math.log(x) for x in c[i - n + 1:i + 1]]
        slope, r2 = linreg_slope_r2(win)
        assert fast[i] == pytest.approx((math.exp(slope * 252) - 1) * r2, rel=1e-9)


def test_atr_is_positive_and_padded(series):
    c, h, l = series
    a = I.atr(h, l, c, 14)
    assert all(is_na(v) for v in a[:13])
    assert all(v > 0 for v in a[14:])


def test_rsi_bounds(series):
    c, _, _ = series
    r = I.rsi(c, 14)
    vals = [v for v in r if not is_na(v)]
    assert vals and all(0.0 <= v <= 100.0 for v in vals)


def test_rsi_all_up_closes_is_100():
    r = I.rsi([float(i) for i in range(1, 60)], 14)
    assert r[-1] == pytest.approx(100.0)


def test_adx_bounds(series):
    c, h, l = series
    a = I.adx(h, l, c, 14)
    vals = [v for v in a if not is_na(v)]
    assert vals and all(0.0 <= v <= 100.0 for v in vals)


def test_short_input_never_raises():
    for fn in (lambda x: I.ema(x, 20), lambda x: I.rolling_max(x, 20),
               lambda x: I.annualised_slope_r2(x, 90), lambda x: I.rsi(x, 14)):
        assert fn([]) == [] or all(is_na(v) for v in fn([1.0, 2.0]))
