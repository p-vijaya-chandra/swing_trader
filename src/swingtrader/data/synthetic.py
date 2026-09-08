"""Synthetic market generator.

PURPOSE: verify the pipeline end-to-end (indicators, backtest, walk-forward,
learning loop, reports) without a data vendor. Regimes, sector factors and a
slow-moving alpha process make the tape *shaped* like an equity market, which
is enough to exercise every code path.

IT IS NOT EVIDENCE. Any CAGR, Sharpe or hit-rate produced on synthetic bars
tells you the code runs, not that the strategy makes money. Never quote a
synthetic backtest as a performance expectation.
"""
from __future__ import annotations

import math
import random
from typing import Dict, List, Sequence

from .models import Series

# Regime -> (daily drift, daily vol, expected duration in trading days)
REGIMES = {
    "bull": (0.00090, 0.0088, 210),
    "chop": (0.00000, 0.0115, 85),
    "bear": (-0.00135, 0.0180, 60),
}
# Explicit regime transition matrix. Combined with the durations above this
# lands near 55% bull / 32% chop / 13% bear, which is roughly how the Nifty
# tape has actually divided up - not a permanent bull market.
TRANSITIONS = {
    "bull": {"chop": 0.72, "bear": 0.28},
    "chop": {"bull": 0.72, "bear": 0.28},
    "bear": {"bull": 0.40, "chop": 0.60},
}


def _next_regime(rng, current: str) -> str:
    row = TRANSITIONS[current]
    keys = list(row)
    return rng.choices(keys, weights=[row[k] for k in keys])[0]


def _trading_dates(start_year: int, n: int) -> List[str]:
    import datetime as dt
    d = dt.date(start_year, 1, 1)
    out: List[str] = []
    while len(out) < n:
        if d.weekday() < 5:
            # crude holiday proxy: NSE loses roughly 13 weekdays a year
            if not (d.month == 1 and d.day == 26) and not (d.month == 8 and d.day == 15) \
               and not (d.month == 10 and d.day == 2) and not (d.month == 12 and d.day == 25):
                out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def generate(symbols: Sequence[str], sectors: Dict[str, str], n_bars: int = 2600,
             start_year: int = 2015, seed: int = 42,
             index_symbol: str = "NIFTY100") -> Dict[str, Series]:
    rng = random.Random(seed)
    dates = _trading_dates(start_year, n_bars)

    # --- market regime path -------------------------------------------------
    regime = "bull"
    mkt_ret: List[float] = []
    regimes: List[str] = []
    for _ in range(n_bars):
        mu, sd, dur = REGIMES[regime]
        if rng.random() < 1.0 / dur:
            regime = _next_regime(rng, regime)
            mu, sd, dur = REGIMES[regime]
        regimes.append(regime)
        mkt_ret.append(rng.gauss(mu, sd))

    # --- sector factors -----------------------------------------------------
    sec_names = sorted(set(sectors.values()))
    sec_ret: Dict[str, List[float]] = {}
    for s in sec_names:
        a = 0.0
        path = []
        for _ in range(n_bars):
            # AR(1) with phi=0.985 -> stationary sd ~0.00035/day, i.e. roughly
            # 9% a year of sector dispersion. Enough to rotate, not enough to
            # hand a momentum ranker a free lunch.
            a = 0.985 * a + rng.gauss(0.0, 0.00006)
            path.append(a + rng.gauss(0.0, 0.0055))
        sec_ret[s] = path

    out: Dict[str, Series] = {}

    def build(sym: str, rets: Sequence[float], px0: float, base_vol: float,
              turnover_cr: float = 250.0) -> Series:
        """turnover_cr: typical daily traded value in Rs crore. Modelling
        turnover rather than share count is what keeps the liquidity screen
        meaningful - share volume alone is meaningless without the price."""
        close, o, h, l, v = [], [], [], [], []
        px = px0
        for i, r in enumerate(rets):
            prev = px
            px = max(0.5, px * math.exp(r))
            close.append(px)
            rng_pct = abs(rng.gauss(0.0, base_vol)) + abs(r) * 0.6 + 0.004
            hi = max(prev, px) * (1 + rng_pct * rng.uniform(0.3, 1.0))
            lo = min(prev, px) * (1 - rng_pct * rng.uniform(0.3, 1.0))
            op = min(max(prev * math.exp(rng.gauss(0, base_vol * 0.45)), lo), hi)
            o.append(op)
            h.append(max(hi, op, px))
            l.append(min(lo, op, px))
            # Volume derived from a target traded VALUE, expanding on big moves.
            shares = (turnover_cr * 1e7) / max(px, 1.0)
            v.append(max(1000.0, shares * math.exp(rng.gauss(0, 0.40)) * (1 + 3 * abs(r))))
        return Series(sym, dates, o, h, l, close, v)

    # --- index --------------------------------------------------------------
    out[index_symbol] = build(index_symbol, mkt_ret, 8000.0, 0.006, turnover_cr=0.0)

    # --- constituents -------------------------------------------------------
    for sym in symbols:
        sec = sectors.get(sym, "Other")
        beta = rng.uniform(0.55, 1.55)
        idio_vol = rng.uniform(0.010, 0.022)
        px0 = rng.choice([65, 120, 240, 480, 900, 1800, 3200])
        # Nifty 100 daily traded value: fat right tail, median a few hundred crore.
        turnover_cr = math.exp(rng.gauss(math.log(280.0), 0.85))
        alpha = 0.0
        rets = []
        for i in range(n_bars):
            # AR(1) alpha: this is what makes relative strength persist, i.e.
            # it is the *reason* a momentum ranker can work at all. phi=0.992
            # with this innovation sd gives a stationary alpha sd of ~0.0008/day
            # (~13% a year of cross-sectional spread), which is the right order
            # of magnitude for large caps. Turning this up is how you fool
            # yourself into believing a momentum system prints 60% CAGR.
            alpha = 0.992 * alpha + rng.gauss(0.0, 0.00010)
            rets.append(beta * mkt_ret[i] + 0.6 * sec_ret[sec][i]
                        + alpha + rng.gauss(0.0, idio_vol))
        out[sym] = build(sym, rets, float(px0), idio_vol, turnover_cr=turnover_cr)

    return out


def regime_summary(seed: int = 42, n_bars: int = 2600) -> Dict[str, int]:
    rng = random.Random(seed)
    regime, counts = "bull", {k: 0 for k in REGIMES}
    for _ in range(n_bars):
        _, _, dur = REGIMES[regime]
        if rng.random() < 1.0 / dur:
            regime = _next_regime(rng, regime)
        counts[regime] += 1
    return counts
