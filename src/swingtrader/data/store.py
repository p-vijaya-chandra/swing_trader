"""CSV-backed price cache.

Plain CSV on purpose: you can open it, diff it, and fix a bad print by hand.
At 100 symbols x 15 years that is ~40 MB, which is nothing.
"""
from __future__ import annotations

import csv
import os
from typing import Dict, Iterable, List, Optional

from .models import Series

HEADER = ["date", "open", "high", "low", "close", "volume"]


class DataStore:
    def __init__(self, cache_dir: str = "data_cache"):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def path(self, symbol: str) -> str:
        safe = symbol.replace("/", "_").replace("&", "_AMP_").replace("^", "_IDX_")
        return os.path.join(self.cache_dir, f"{safe}.csv")

    def has(self, symbol: str) -> bool:
        return os.path.exists(self.path(symbol))

    def save(self, series: Series) -> None:
        with open(self.path(series.symbol), "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(HEADER)
            w.writerows(series.to_rows())

    def load(self, symbol: str) -> Optional[Series]:
        p = self.path(symbol)
        if not os.path.exists(p):
            return None
        rows = []
        with open(p, "r", encoding="utf-8") as fh:
            r = csv.reader(fh)
            head = next(r, None)
            if head != HEADER:
                raise ValueError(f"{p}: unexpected header {head}")
            for row in r:
                if len(row) >= 6:
                    rows.append(row)
        return Series.from_rows(symbol, rows)

    def load_many(self, symbols: Iterable[str]) -> Dict[str, Series]:
        out: Dict[str, Series] = {}
        for s in symbols:
            ser = self.load(s)
            if ser is not None and len(ser) > 0:
                out[s] = ser
        return out

    def upsert(self, series: Series) -> Series:
        """Merge new bars into whatever is cached; new data wins on conflicts."""
        existing = self.load(series.symbol)
        if existing is None:
            self.save(series)
            return series
        merged = Series.from_rows(series.symbol, existing.to_rows() + series.to_rows())
        self.save(merged)
        return merged

    def symbols(self) -> List[str]:
        out = []
        for f in sorted(os.listdir(self.cache_dir)):
            if f.endswith(".csv"):
                out.append(f[:-4].replace("_AMP_", "&").replace("_IDX_", "^"))
        return out

    def coverage(self) -> Dict[str, tuple]:
        out = {}
        for s in self.symbols():
            ser = self.load(s)
            if ser and len(ser):
                out[s] = (ser.dates[0], ser.dates[-1], len(ser))
        return out
