"""Small statistics and date helpers.

Deliberately stdlib-only: the whole engine must run on a bare Python install so
that a broken pip or an offline VPS can never stop the daily routine.
"""
from __future__ import annotations

import datetime as _dt
import math
from typing import List, Optional, Sequence

NA = float("nan")


def is_na(x: Optional[float]) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def parse_date(s: str) -> _dt.date:
    return _dt.date.fromisoformat(s[:10])


def fmt_date(d: _dt.date) -> str:
    return d.isoformat()


def mean(xs: Sequence[float]) -> float:
    xs = [x for x in xs if not is_na(x)]
    return sum(xs) / len(xs) if xs else NA


def stdev(xs: Sequence[float], ddof: int = 1) -> float:
    xs = [x for x in xs if not is_na(x)]
    n = len(xs)
    if n - ddof <= 0:
        return NA
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - ddof))


def median(xs: Sequence[float]) -> float:
    xs = sorted(x for x in xs if not is_na(x))
    n = len(xs)
    if n == 0:
        return NA
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def percentile(xs: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile, q in [0, 100]."""
    xs = sorted(x for x in xs if not is_na(x))
    if not xs:
        return NA
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * (q / 100.0)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


def zscores(xs: Sequence[float]) -> List[float]:
    """Cross-sectional z-scores; NaNs stay NaN and are excluded from moments."""
    vals = [x for x in xs if not is_na(x)]
    if len(vals) < 2:
        return [NA] * len(xs)
    m = sum(vals) / len(vals)
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))
    if sd <= 0:
        return [0.0 if not is_na(x) else NA for x in xs]
    return [NA if is_na(x) else (x - m) / sd for x in xs]


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def safe_div(a: float, b: float, default: float = NA) -> float:
    if is_na(a) or is_na(b) or b == 0:
        return default
    return a / b


def linreg_slope_r2(ys: Sequence[float]) -> tuple:
    """OLS of ys on 0..n-1. Returns (slope, r2). NaN-safe on short input."""
    n = len(ys)
    if n < 3 or any(is_na(y) for y in ys):
        return NA, NA
    xbar = (n - 1) / 2.0
    ybar = sum(ys) / n
    sxx = syy = sxy = 0.0
    for i, y in enumerate(ys):
        dx = i - xbar
        dy = y - ybar
        sxx += dx * dx
        syy += dy * dy
        sxy += dx * dy
    if sxx == 0:
        return NA, NA
    slope = sxy / sxx
    r2 = 0.0 if syy == 0 else (sxy * sxy) / (sxx * syy)
    return slope, r2


def bootstrap_mean_ci(xs: Sequence[float], n_boot: int = 2000, alpha: float = 0.05,
                      seed: int = 7) -> tuple:
    """Percentile bootstrap CI for the mean. Used to gate strategy promotions."""
    import random
    vals = [x for x in xs if not is_na(x)]
    if len(vals) < 5:
        return NA, NA
    rng = random.Random(seed)
    n = len(vals)
    means = []
    for _ in range(n_boot):
        s = 0.0
        for _ in range(n):
            s += vals[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    return (percentile(means, 100 * alpha / 2), percentile(means, 100 * (1 - alpha / 2)))


def chunk_months(dates: Sequence[str]) -> List[str]:
    """Map ISO dates to YYYY-MM keys."""
    return [d[:7] for d in dates]


def fmt_inr(x: float) -> str:
    """Format a number in Indian digit grouping, e.g. 1,00,000.00."""
    if is_na(x):
        return "n/a"
    neg = x < 0
    x = abs(x)
    whole = int(x)
    frac = x - whole
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    out = f"{s}.{int(round(frac * 100)):02d}"
    return ("-" if neg else "") + out
