"""End-of-day market data for the executor.

CacheMarket is the live implementation: refresh the price cache incrementally,
validate it, and only then rebuild features. If validation finds a defect in a
symbol the book HOLDS, planning is refused for the day - a phantom -50% bar on a
held name would turn into a real exit order at tomorrow's open. Defects in
names the book does not hold just remove those names from consideration.

ReplayMarket serves a pre-built dataset whose "present" is the clock, for
tests and demonstrations. It can build features once over the whole history
because every feature is causal - the truncation tests prove a value on date d
is identical whether or not later bars exist.
"""
from __future__ import annotations

import datetime as dt
from typing import Iterable, List, Optional, Tuple

from ..config import Config
from ..runtime import Dataset, load_universe


class MarketData:
    def dataset(self) -> Dataset:
        raise NotImplementedError

    def last_session(self) -> str:
        raise NotImplementedError

    def refresh(self, day: dt.date, held: Iterable[str] = ()) -> Tuple[bool, str]:
        raise NotImplementedError

    def close_of(self, symbol: str, date: str) -> float:
        f = self.dataset().series.get(symbol)
        if f is None:
            return float("nan")
        i = f.pos(date)
        if i is None:
            # last close on or before `date`
            for k in range(len(f.dates) - 1, -1, -1):
                if f.dates[k] <= date:
                    return f.close[k]
            return float("nan")
        return f.close[i]


class ReplayMarket(MarketData):
    def __init__(self, ds: Dataset, clock, close_hhmm: str = "15:30"):
        self.ds = ds
        self.clock = clock
        self.close_hhmm = close_hhmm
        self._dates = ds.all_dates()

    def dataset(self) -> Dataset:
        return self.ds

    def last_session(self) -> str:
        now = self.clock.now()
        today = now.date().isoformat()
        after_close = now >= self.clock.at(now.date(), self.close_hhmm)
        last = ""
        for d in self._dates:
            if d < today or (d == today and after_close):
                last = d
            else:
                break
        return last

    def refresh(self, day: dt.date, held: Iterable[str] = ()) -> Tuple[bool, str]:
        return True, "replay data"


class CacheMarket(MarketData):
    def __init__(self, cfg: Config, fetch_start: str = "2014-01-01"):
        self.cfg = cfg
        self.fetch_start = fetch_start
        self._ds: Optional[Dataset] = None
        self.excluded: List[str] = []

    def dataset(self) -> Dataset:
        if self._ds is None:
            self._ds = Dataset.load(self.cfg)
            for s in self.excluded:
                self._ds.series.pop(s, None)
        return self._ds

    def last_session(self) -> str:
        ds = self.dataset()
        dates = ds.all_dates()
        return dates[-1] if dates else ""

    def refresh(self, day: dt.date, held: Iterable[str] = ()) -> Tuple[bool, str]:
        from ..data.providers import ProviderError, get_provider
        from ..data.quality import check_dataset
        from ..data.store import DataStore

        cfg = self.cfg
        symbols, _, _ = load_universe(cfg.get("universe.file"))
        index_sym = cfg.get("regime.index_symbol", "NIFTY100")
        store = DataStore(cfg.get("data.cache_dir", "data_cache"))
        try:
            prov = get_provider(cfg.get("data.provider", "yfinance"), cfg)
        except ProviderError as exc:
            return False, f"data provider unavailable: {exc}"

        groups = {}
        for sym in symbols + [index_sym]:
            cached = store.load(sym)
            start = self.fetch_start if cached is None or not len(cached) else \
                (dt.date.fromisoformat(cached.dates[-1]) - dt.timedelta(days=5)).isoformat()
            groups.setdefault(start, []).append(sym)
        got = {}
        for start, syms in groups.items():
            got.update(prov.fetch(syms, start, ""))
        for ser in got.values():
            store.upsert(ser)

        series = store.load_many(symbols)
        index = store.load(index_sym)
        if index is None or not len(index):
            return False, "index series missing after refresh"
        if index.dates[-1] != day.isoformat():
            return False, (f"latest index bar is {index.dates[-1]}, not {day.isoformat()} - "
                           f"the vendor has not published today's close yet")
        rep = check_dataset(series, index,
                            min_bars=int(cfg.get("universe.min_history_bars", 260)))
        bad = set(rep.bad_symbols())
        held_bad = sorted(bad & set(held))
        if held_bad:
            return False, (f"data defects in HELD symbols {held_bad}: refusing to plan "
                           f"on them. Run `swing validate-data` and fix the source.")
        self.excluded = sorted(b for b in bad if not b.startswith("("))
        self._ds = None                       # rebuild features from the fresh cache
        note = f"; excluded {len(self.excluded)} symbol(s) with data defects" \
            if self.excluded else ""
        return True, f"refreshed {len(got)} symbols{note}"
