"""Per-symbol indicator bundles, computed once per (config, dataset) pair.

Everything the strategy needs is precomputed into aligned arrays indexed by bar
position. The backtest then only ever reads index i on date t, which makes
lookahead bias structurally hard to write: there is no path from the engine to
a future bar.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from . import indicators as ind
from .config import Config
from .data.models import Series
from .util import NA, is_na


class Features:
    """Aligned indicator arrays for one symbol."""

    __slots__ = ("symbol", "series", "sector", "cols")

    def __init__(self, symbol: str, series: Series, sector: str, cols: Dict[str, List[float]]):
        self.symbol = symbol
        self.series = series
        self.sector = sector
        self.cols = cols

    def __len__(self) -> int:
        return len(self.series)

    def get(self, name: str, i: int) -> float:
        col = self.cols.get(name)
        if col is None or i < 0 or i >= len(col):
            return NA
        return col[i]

    def ready(self, i: int, names: Sequence[str]) -> bool:
        return all(not is_na(self.get(n, i)) for n in names)

    def snapshot(self, i: int) -> Dict[str, float]:
        """Feature vector at bar i - stored on every trade for the learning loop."""
        s = self.series
        out = {k: v[i] for k, v in self.cols.items() if i < len(v)}
        out["close"] = s.close[i]
        return out


REQUIRED = ("ema_fast", "ema_slow", "atr", "mom", "adx", "roc_126")


def build_features(series_map: Dict[str, Series], index_series: Optional[Series],
                   sectors: Dict[str, str], cfg: Config) -> Dict[str, Features]:
    ema_fast_n = int(cfg.get("screen.ema_fast", 50))
    ema_slow_n = int(cfg.get("screen.ema_slow", 200))
    pull_ema_n = int(cfg.get("entry.pullback_ema", 20))
    mom_n = int(cfg.get("rank.mom_lookback", 90))
    rs_n = int(cfg.get("screen.rs_lookback", 63))
    brk_n = int(cfg.get("entry.breakout_lookback", 20))
    rsi_fast_n = int(cfg.get("entry.pullback_rsi_len", 3))
    struct_n = int(cfg.get("exit.structure_lookback", 10))

    idx_close = index_series.close if index_series else None
    idx_pos = index_series.idx if index_series else {}

    out: Dict[str, Features] = {}
    for sym, s in series_map.items():
        n = len(s)
        if n < 30:
            continue
        c, h, l, v = s.close, s.high, s.low, s.volume

        ema_fast = ind.ema(c, ema_fast_n)
        ema_slow = ind.ema(c, ema_slow_n)
        ema_pull = ind.ema(c, pull_ema_n)
        atr14 = ind.atr(h, l, c, 14)
        atr_pct = [NA if (is_na(atr14[i]) or c[i] <= 0) else atr14[i] / c[i] for i in range(n)]
        adx14 = ind.adx(h, l, c, 14)
        rsi_fast = ind.rsi(c, rsi_fast_n)
        rsi14 = ind.rsi(c, 14)
        roc_63 = ind.roc(c, 63)
        roc_126 = ind.roc(c, 126)
        roc_21 = ind.roc(c, 21)
        mom = ind.annualised_slope_r2(c, mom_n)
        hh_52w = ind.rolling_max(h, 252)
        donch = ind.donchian_high(h, brk_n)
        struct_low = ind.donchian_low(l, struct_n)
        vol_avg = ind.sma(v, 20)
        turnover = [c[i] * v[i] for i in range(n)]
        turnover_med = ind.rolling_median(turnover, 20)

        # slope of the slow EMA over 21 bars, normalised by price
        ema_slow_slope = [NA] * n
        for i in range(21, n):
            a, b = ema_slow[i - 21], ema_slow[i]
            if not is_na(a) and not is_na(b) and a > 0:
                ema_slow_slope[i] = (b / a - 1.0)

        pct_below_52w = [NA] * n
        for i in range(n):
            hi = hh_52w[i]
            if not is_na(hi) and hi > 0:
                pct_below_52w[i] = 1.0 - c[i] / hi

        dist_ema_atr = [NA] * n
        for i in range(n):
            if not is_na(ema_pull[i]) and not is_na(atr14[i]) and atr14[i] > 0:
                dist_ema_atr[i] = (c[i] - ema_pull[i]) / atr14[i]

        vol_ratio = [NA] * n
        for i in range(n):
            if not is_na(vol_avg[i]) and vol_avg[i] > 0:
                vol_ratio[i] = v[i] / vol_avg[i]

        # relative strength vs the index over rs_n bars, aligned by DATE so a
        # symbol with a shorter history or a missing session can never borrow
        # the index return from the wrong day
        rs_index = [NA] * n
        if idx_close is not None:
            for i in range(rs_n, n):
                pi, pj = idx_pos.get(s.dates[i]), idx_pos.get(s.dates[i - rs_n])
                if pi is None or pj is None:
                    continue
                ic0, ic1 = idx_close[pj], idx_close[pi]
                if ic0 <= 0 or c[i - rs_n] <= 0:
                    continue
                rs_index[i] = (c[i] / c[i - rs_n]) / (ic1 / ic0) - 1.0

        gap = [NA] * n
        for i in range(1, n):
            if c[i - 1] > 0:
                gap[i] = s.open[i] / c[i - 1] - 1.0

        out[sym] = Features(sym, s, sectors.get(sym, "Other"), {
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "ema_pull": ema_pull,
            "ema_slow_slope": ema_slow_slope,
            "atr": atr14,
            "atr_pct": atr_pct,
            "adx": adx14,
            "rsi_fast": rsi_fast,
            "rsi_14": rsi14,
            "roc_21": roc_21,
            "roc_63": roc_63,
            "roc_126": roc_126,
            "mom": mom,
            "hh_52w": hh_52w,
            "pct_below_52w": pct_below_52w,
            "donchian_high": donch,
            "structure_low": struct_low,
            "vol_avg": vol_avg,
            "vol_ratio": vol_ratio,
            "turnover_med": turnover_med,
            "dist_ema_atr": dist_ema_atr,
            "rs_index": rs_index,
            "gap": gap,
        })
    return out
