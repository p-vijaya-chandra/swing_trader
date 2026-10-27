"""Replay daily bars as an intraday price path.

Used to drive the real-time executor through historical (or synthetic) sessions
without a market-data connection. Each session follows open -> first extreme ->
second extreme -> close, visiting the low first on up-days and the high first on
down-days - the conventional assumption that makes a day's stop and target
ordering unambiguous, and the same one the backtest's intraday stop logic
implicitly relies on.

It is a test instrument and a demonstration tool. It is not a model of how
prices actually move inside a day, and nothing measured on it says anything
about live slippage.
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, Optional, Sequence

from ..broker.base import NA, Quote
from .clock import Clock, session_bounds

# fraction of the session at which each waypoint is reached
_WAYPOINTS = (0.0, 0.30, 0.70, 1.0)


class BarReplayFeed:
    def __init__(self, series_map: Dict[str, object], clock: Clock,
                 open_hhmm: str = "09:15", close_hhmm: str = "15:30"):
        self.series = series_map
        self.clock = clock
        self.open_hhmm = open_hhmm
        self.close_hhmm = close_hhmm

    def _bar(self, symbol: str, day: dt.date):
        s = self.series.get(symbol)
        if s is None:
            return None, None
        i = s.pos(day.isoformat())
        return s, i

    def _path(self, o: float, h: float, l: float, c: float):
        first, second = (l, h) if c >= o else (h, l)
        return (o, first, second, c)

    def price_at(self, symbol: str, when: dt.datetime) -> Optional[Quote]:
        day = when.date()
        s, i = self._bar(symbol, day)
        if s is None:
            return None
        start, end = session_bounds(day, self.open_hhmm, self.close_hhmm)
        prev_close = s.close[i - 1] if (i is not None and i > 0) else NA

        if i is None:
            # no session for this symbol today (holiday, halt): last close, stale
            j = None
            for k in range(len(s.dates) - 1, -1, -1):
                if s.dates[k] < day.isoformat():
                    j = k
                    break
            if j is None:
                return None
            last = s.close[j]
            return Quote(symbol, last, NA, NA, NA, last,
                         ts=dt.datetime.fromisoformat(s.dates[j] + "T15:30:00+05:30").timestamp())

        o, h, l, c = s.open[i], s.high[i], s.low[i], s.close[i]
        if when < start:
            # pre-open: the session has not traded; show yesterday's close
            return Quote(symbol, prev_close, NA, NA, NA, prev_close,
                         ts=(start - dt.timedelta(days=1)).timestamp())
        frac = min(1.0, (when - start).total_seconds() / max(1.0, (end - start).total_seconds()))
        pts = self._path(o, h, l, c)
        for k in range(len(_WAYPOINTS) - 1):
            a, b = _WAYPOINTS[k], _WAYPOINTS[k + 1]
            if frac <= b:
                w = 0.0 if b == a else (frac - a) / (b - a)
                ltp = pts[k] + (pts[k + 1] - pts[k]) * w
                break
        else:
            ltp = c
        visited = pts[:k + 1] + (ltp,) if frac < 1.0 else pts
        return Quote(symbol, ltp, o, max(visited), min(visited), prev_close,
                     ts=min(when, end).timestamp())

    def quotes(self, symbols: Sequence[str]) -> Dict[str, Quote]:
        now = self.clock.now()
        out: Dict[str, Quote] = {}
        for sym in symbols:
            q = self.price_at(sym, now)
            if q is not None and q.valid():
                out[sym] = q
        return out

    def has_session(self, day: dt.date, index_symbol: Optional[str] = None) -> bool:
        ref = self.series.get(index_symbol) if index_symbol else None
        if ref is None and self.series:
            ref = next(iter(self.series.values()))
        return ref is not None and ref.pos(day.isoformat()) is not None
