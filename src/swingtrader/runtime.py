"""Dataset assembly and feature caching shared by the CLI, backtests and the
learning loop.

Feature construction is the expensive step, and the walk-forward optimiser
re-runs it thousands of times. Only a handful of parameters actually change the
feature arrays, so features are cached against exactly those. Everything else -
stops, sizing, exit thresholds - reuses the cached bundle.
"""
from __future__ import annotations

import csv
from typing import Dict, List, Optional, Tuple

from .config import Config
from .data.models import Series
from .data.store import DataStore
from .features import Features, build_features
from .regime import RegimeModel

FEATURE_KEYS = (
    "screen.ema_fast", "screen.ema_slow", "entry.pullback_ema", "rank.mom_lookback",
    "screen.rs_lookback", "entry.breakout_lookback", "entry.pullback_rsi_len",
    "exit.structure_lookback",
)


def load_universe(path: str) -> Tuple[List[str], Dict[str, str], Dict[str, str]]:
    """Return (symbols, sector_by_symbol, name_by_symbol)."""
    symbols, sectors, names = [], {}, {}
    with open(path, "r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            sym = (row.get("symbol") or "").strip()
            if not sym or sym.startswith("#"):
                continue
            symbols.append(sym)
            sectors[sym] = (row.get("sector") or "Other").strip()
            names[sym] = (row.get("name") or sym).strip()
    return symbols, sectors, names


class Dataset:
    """Price series + sector map + index, from cache or generated synthetically."""

    def __init__(self, series: Dict[str, Series], index: Optional[Series],
                 sectors: Dict[str, str], names: Dict[str, str], synthetic: bool = False):
        self.series = series
        self.index = index
        self.sectors = sectors
        self.names = names
        self.synthetic = synthetic
        self._feature_cache: Dict[tuple, Dict[str, Features]] = {}
        self._regime_cache: Dict[tuple, RegimeModel] = {}

    # ------------------------------------------------------------------ loaders
    @classmethod
    def load(cls, cfg: Config, synthetic: bool = False, seed: int = 14,
             n_bars: int = 2600) -> "Dataset":
        symbols, sectors, names = load_universe(cfg.get("universe.file"))
        index_sym = cfg.get("regime.index_symbol", "NIFTY100")

        if synthetic:
            from .data.synthetic import generate
            gen = generate(symbols, sectors, n_bars=n_bars, seed=seed,
                           index_symbol=index_sym)
            index = gen.pop(index_sym, None)
            return cls(gen, index, sectors, names, synthetic=True)

        store = DataStore(cfg.get("data.cache_dir", "data_cache"))
        series = store.load_many(symbols)
        index = store.load(index_sym)
        if not series:
            raise SystemExit(
                f"No cached price data in '{store.cache_dir}'.\n"
                f"Run:  swing fetch --start 2014-01-01\n"
                f"or:   swing backtest --synthetic   (pipeline demo, not real results)")
        if index is None:
            print(f"  ! no index series '{index_sym}' cached - regime filter disabled")
        return cls(series, index, sectors, names, synthetic=False)

    # ----------------------------------------------------------------- features
    def _feature_key(self, cfg: Config) -> tuple:
        return tuple(cfg.get(k) for k in FEATURE_KEYS)

    def features(self, cfg: Config) -> Dict[str, Features]:
        key = self._feature_key(cfg)
        cached = self._feature_cache.get(key)
        if cached is None:
            cached = build_features(self.series, self.index, self.sectors, cfg)
            self._feature_cache[key] = cached
        return cached

    def regime(self, cfg: Config, dates: Optional[List[str]] = None) -> RegimeModel:
        feats = self.features(cfg)
        rkey = self._feature_key(cfg) + (
            cfg.get("regime.ma_long"), cfg.get("regime.ma_short"),
            cfg.get("regime.breadth_ma"), cfg.get("regime.risk_on_breadth"),
            cfg.get("regime.risk_off_breadth"), cfg.get("regime.chop_atr_pct_max"),
            tuple(sorted((cfg.get("regime.exposure") or {}).items())))
        rm = self._regime_cache.get(rkey)
        if rm is None:
            rm = RegimeModel(cfg)
            rm.build(self.index, feats, dates or self.all_dates())
            self._regime_cache[rkey] = rm
        return rm

    def all_dates(self) -> List[str]:
        if self.index is not None and len(self.index):
            return list(self.index.dates)
        seen = set()
        for s in self.series.values():
            seen.update(s.dates)
        return sorted(seen)

    def coverage(self) -> str:
        ds = self.all_dates()
        if not ds:
            return "empty dataset"
        return (f"{len(self.series)} symbols, {len(ds)} sessions "
                f"{ds[0]} .. {ds[-1]}" + ("  [SYNTHETIC]" if self.synthetic else ""))


def run_backtest(cfg: Config, ds: Dataset, start: str = "", end: str = "",
                 entry_filter=None, verbose: bool = False):
    """Convenience wrapper used everywhere a single backtest is needed."""
    from .backtest import BacktestEngine
    feats = ds.features(cfg)
    rm = ds.regime(cfg)
    eng = BacktestEngine(cfg, feats, ds.index, rm, entry_filter=entry_filter)
    return eng.run(start=start, end=end, verbose=verbose)
