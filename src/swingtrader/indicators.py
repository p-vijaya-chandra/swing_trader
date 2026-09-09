"""Technical indicators over plain Python lists.

Every function returns a list the same length as the input, left-padded with
NaN until enough history exists. Keeping the shape stable means the backtest
can index indicators by bar position without off-by-one bookkeeping, which is
where most home-grown backtesters silently introduce lookahead bias.
"""
from __future__ import annotations

import math
from typing import List, Sequence

from .util import NA, is_na, linreg_slope_r2, median


def sma(xs: Sequence[float], n: int) -> List[float]:
    out = [NA] * len(xs)
    if n <= 0:
        return out
    run = 0.0
    for i, x in enumerate(xs):
        run += x
        if i >= n:
            run -= xs[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def ema(xs: Sequence[float], n: int) -> List[float]:
    """EMA seeded with the first n-bar SMA (standard charting convention)."""
    out = [NA] * len(xs)
    if n <= 0 or len(xs) < n:
        return out
    k = 2.0 / (n + 1.0)
    seed = sum(xs[:n]) / n
    out[n - 1] = seed
    prev = seed
    for i in range(n, len(xs)):
        prev = xs[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def wilder_smooth(xs: Sequence[float], n: int) -> List[float]:
    """Wilder's smoothing (RMA) - what ATR/RSI/ADX are actually defined on."""
    out = [NA] * len(xs)
    if n <= 0 or len(xs) < n:
        return out
    seed = sum(xs[:n]) / n
    out[n - 1] = seed
    prev = seed
    for i in range(n, len(xs)):
        prev = (prev * (n - 1) + xs[i]) / n
        out[i] = prev
    return out


def true_range(high: Sequence[float], low: Sequence[float], close: Sequence[float]) -> List[float]:
    out = [NA] * len(high)
    if not high:
        return out
    out[0] = high[0] - low[0]
    for i in range(1, len(high)):
        pc = close[i - 1]
        out[i] = max(high[i] - low[i], abs(high[i] - pc), abs(low[i] - pc))
    return out


def atr(high, low, close, n: int = 14) -> List[float]:
    return wilder_smooth(true_range(high, low, close), n)


def rsi(xs: Sequence[float], n: int = 14) -> List[float]:
    out = [NA] * len(xs)
    if len(xs) <= n:
        return out
    gains, losses = [0.0], [0.0]
    for i in range(1, len(xs)):
        d = xs[i] - xs[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = wilder_smooth(gains[1:], n)
    al = wilder_smooth(losses[1:], n)
    for i in range(len(ag)):
        if is_na(ag[i]) or is_na(al[i]):
            continue
        if al[i] == 0:
            out[i + 1] = 100.0
        else:
            rs = ag[i] / al[i]
            out[i + 1] = 100.0 - 100.0 / (1.0 + rs)
    return out


def adx(high, low, close, n: int = 14) -> List[float]:
    """Average Directional Index - trend strength, direction-agnostic."""
    m = len(high)
    out = [NA] * m
    if m < 2 * n + 2:
        return out
    plus_dm, minus_dm = [], []
    for i in range(1, m):
        up = high[i] - high[i - 1]
        dn = low[i - 1] - low[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
    tr = true_range(high, low, close)[1:]
    str_ = wilder_smooth(tr, n)
    sp = wilder_smooth(plus_dm, n)
    sm = wilder_smooth(minus_dm, n)
    dx = []
    for i in range(len(str_)):
        if is_na(str_[i]) or str_[i] == 0 or is_na(sp[i]) or is_na(sm[i]):
            dx.append(NA)
            continue
        pdi = 100.0 * sp[i] / str_[i]
        mdi = 100.0 * sm[i] / str_[i]
        s = pdi + mdi
        dx.append(0.0 if s == 0 else 100.0 * abs(pdi - mdi) / s)
    valid_from = next((i for i, v in enumerate(dx) if not is_na(v)), None)
    if valid_from is None:
        return out
    tail = dx[valid_from:]
    sm_dx = wilder_smooth(tail, n)
    for i, v in enumerate(sm_dx):
        out[valid_from + i + 1] = v
    return out


def roc(xs: Sequence[float], n: int) -> List[float]:
    """Rate of change over n bars, as a fraction (0.15 == +15%)."""
    out = [NA] * len(xs)
    for i in range(n, len(xs)):
        prev = xs[i - n]
        if prev and prev > 0:
            out[i] = xs[i] / prev - 1.0
    return out


def _monotonic_extreme(xs: Sequence[float], n: int, want_max: bool) -> List[float]:
    """O(len(xs)) rolling max/min via a monotonic deque.

    The naive slice-and-max version is O(n*window); with a 252-bar 52-week
    window over 100 symbols that alone dominated the walk-forward runtime.
    """
    from collections import deque
    out = [NA] * len(xs)
    dq: "deque" = deque()   # holds indices, values monotonic
    for i, x in enumerate(xs):
        while dq and ((xs[dq[-1]] <= x) if want_max else (xs[dq[-1]] >= x)):
            dq.pop()
        dq.append(i)
        if dq[0] <= i - n:
            dq.popleft()
        if i >= n - 1:
            out[i] = xs[dq[0]]
    return out


def rolling_max(xs: Sequence[float], n: int) -> List[float]:
    return _monotonic_extreme(xs, n, True)


def rolling_min(xs: Sequence[float], n: int) -> List[float]:
    return _monotonic_extreme(xs, n, False)


def rolling_median(xs: Sequence[float], n: int) -> List[float]:
    out = [NA] * len(xs)
    for i in range(len(xs)):
        if i >= n - 1:
            out[i] = median(xs[i - n + 1: i + 1])
    return out


def rolling_mean(xs: Sequence[float], n: int) -> List[float]:
    return sma(xs, n)


def rolling_std(xs: Sequence[float], n: int) -> List[float]:
    out = [NA] * len(xs)
    for i in range(len(xs)):
        if i >= n - 1:
            w = xs[i - n + 1: i + 1]
            m = sum(w) / n
            out[i] = math.sqrt(sum((v - m) ** 2 for v in w) / (n - 1)) if n > 1 else 0.0
    return out


def annualised_slope_r2(close: Sequence[float], n: int = 90) -> List[float]:
    """Clenow-style momentum: annualised exponential regression slope x R-squared.

    Fitting log price gives a compounding rate; multiplying by R^2 discounts
    names that got there in one gap and then went sideways. This is the single
    most reliable cross-sectional momentum ranker I know of for a liquid
    large-cap universe, and it is what the default strategy ranks on.

    Computed incrementally in O(len(close)). Because the regressor is always
    x = 0..n-1, sum(x) and sum(x^2) are constants and the cross-moment can be
    slid forward: dropping the oldest point shifts every remaining x down by 1,
    so S_xy loses (S_y - y_old) and gains (n-1) * y_new.
    """
    m = len(close)
    out = [NA] * m
    if n < 3 or m < n:
        return out
    logs = [math.log(c) if c > 0 else NA for c in close]
    if any(is_na(v) for v in logs):
        # fall back to the safe path if the series has non-positive prices
        for i in range(n - 1, m):
            win = logs[i - n + 1: i + 1]
            if any(is_na(v) for v in win):
                continue
            slope, r2 = linreg_slope_r2(win)
            if not is_na(slope) and not is_na(r2):
                out[i] = (math.exp(slope * 252.0) - 1.0) * r2
        return out

    xbar = (n - 1) / 2.0
    sxx = n * (n * n - 1) / 12.0            # sum (x - xbar)^2 for x = 0..n-1
    s_y = sum(logs[:n])
    s_yy = sum(v * v for v in logs[:n])
    s_xy = sum(k * logs[k] for k in range(n))

    def emit(i: int) -> None:
        sxy = s_xy - xbar * s_y
        syy = s_yy - s_y * s_y / n
        slope = sxy / sxx
        r2 = 0.0 if syy <= 0 else (sxy * sxy) / (sxx * syy)
        out[i] = (math.exp(slope * 252.0) - 1.0) * min(max(r2, 0.0), 1.0)

    emit(n - 1)
    for i in range(n, m):
        y_old = logs[i - n]
        y_new = logs[i]
        s_xy = s_xy - (s_y - y_old) + (n - 1) * y_new
        s_y = s_y - y_old + y_new
        s_yy = s_yy - y_old * y_old + y_new * y_new
        emit(i)
    return out


def rolling_percentile_rank(xs: Sequence[float], n: int) -> List[float]:
    """Where does today's value sit inside its own trailing n-bar range (0-100)?"""
    out = [NA] * len(xs)
    for i in range(n - 1, len(xs)):
        w = [v for v in xs[i - n + 1: i + 1] if not is_na(v)]
        if len(w) < 2 or is_na(xs[i]):
            continue
        below = sum(1 for v in w if v < xs[i])
        out[i] = 100.0 * below / (len(w) - 1)
    return out


def donchian_high(high: Sequence[float], n: int) -> List[float]:
    """Highest high of the PRIOR n bars, EXCLUDING today.

    Excluding today is the whole point: comparing today's close against a window
    that already contains today's high can never produce a breakout signal, and
    including it is the classic silent lookahead bug.
    """
    shifted = rolling_max(high, n)
    return [NA] + shifted[:-1] if shifted else []


def donchian_low(low: Sequence[float], n: int) -> List[float]:
    shifted = rolling_min(low, n)
    return [NA] + shifted[:-1] if shifted else []


def drawdown_series(equity: Sequence[float]) -> List[float]:
    out, peak = [], NA
    for v in equity:
        peak = v if is_na(peak) else max(peak, v)
        out.append(0.0 if peak <= 0 else v / peak - 1.0)
    return out
