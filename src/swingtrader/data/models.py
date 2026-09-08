"""Price series container."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence


@dataclass
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class Series:
    """Column-oriented OHLCV for one symbol, plus a date -> position index.

    Column layout (rather than a list of Bar objects) is what keeps the pure
    Python indicator pass fast enough to run a 10-year, 100-symbol walk-forward
    without numpy.
    """

    __slots__ = ("symbol", "dates", "open", "high", "low", "close", "volume", "idx")

    def __init__(self, symbol: str, dates: List[str], o: List[float], h: List[float],
                 l: List[float], c: List[float], v: List[float]):
        n = len(dates)
        if not (len(o) == len(h) == len(l) == len(c) == len(v) == n):
            raise ValueError(f"{symbol}: ragged OHLCV columns")
        self.symbol = symbol
        self.dates = dates
        self.open = o
        self.high = h
        self.low = l
        self.close = c
        self.volume = v
        self.idx: Dict[str, int] = {d: i for i, d in enumerate(dates)}

    def __len__(self) -> int:
        return len(self.dates)

    def pos(self, date: str) -> Optional[int]:
        return self.idx.get(date)

    def bar(self, i: int) -> Bar:
        return Bar(self.dates[i], self.open[i], self.high[i], self.low[i],
                   self.close[i], self.volume[i])

    def slice_to(self, date: str) -> "Series":
        """History up to and including `date` - the only safe way to look back."""
        i = self.idx.get(date)
        if i is None:
            i = -1
            for j, d in enumerate(self.dates):
                if d <= date:
                    i = j
                else:
                    break
            if i < 0:
                return Series(self.symbol, [], [], [], [], [], [])
        j = i + 1
        return Series(self.symbol, self.dates[:j], self.open[:j], self.high[:j],
                      self.low[:j], self.close[:j], self.volume[:j])

    @staticmethod
    def from_rows(symbol: str, rows: Sequence[Sequence]) -> "Series":
        """rows: iterable of (date, o, h, l, c, v); sorted and de-duplicated."""
        clean = {}
        for r in rows:
            d = str(r[0])[:10]
            try:
                o, h, l, c, v = (float(r[1]), float(r[2]), float(r[3]),
                                 float(r[4]), float(r[5]))
            except (TypeError, ValueError):
                continue
            if not all(x > 0 for x in (o, h, l, c)):
                continue
            # Guard against vendor glitches that would fabricate signals.
            if h < max(o, c) or l > min(o, c) or h < l:
                continue
            clean[d] = (o, h, l, c, v)
        ds = sorted(clean)
        return Series(symbol, ds,
                      [clean[d][0] for d in ds], [clean[d][1] for d in ds],
                      [clean[d][2] for d in ds], [clean[d][3] for d in ds],
                      [clean[d][4] for d in ds])

    def to_rows(self) -> List[List]:
        return [[self.dates[i], self.open[i], self.high[i], self.low[i],
                 self.close[i], self.volume[i]] for i in range(len(self.dates))]
