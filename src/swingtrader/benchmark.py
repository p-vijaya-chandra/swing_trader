"""Benchmarks: what the swing system has to beat.

A strategy result means nothing on its own. "-4.7% a year" and "+18% a year"
are both uninterpretable until you know what simply holding the market did
over the same years, and what the plainest systematic alternative did.

  buy_and_hold_index  the Nifty 100 itself. No survivorship bias: it is the
                      real index, including everything that was in it then.
  momentum_rotation   at each month-end hold the top-N names by 6-month
                      return (skipping the latest month), switching at the
                      next open and paying Groww's full costs at your account
                      size. Names that stay in the top N are kept, not resized,
                      so a winner keeps running while its momentum lasts. The
                      plainest rules-based momentum strategy there is.

The rotation picks from TODAY's universe file, so it carries survivorship bias:
it can only ever choose companies that went on to be in the Nifty 100 now. So
does the swing system - the two are comparable with each other, and both are
flattered against the index.

Each month-end decision is made by rotation_picks() from closes up to and
including that date only, and the tests hold it to that by re-deciding on data
truncated at the decision date.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .backtest.metrics import compute_metrics
from .config import Config
from .costs import CostModel
from .util import median


@dataclass
class BenchResult:
    name: str
    dates: List[str] = field(default_factory=list)
    equity: List[float] = field(default_factory=list)
    costs: float = 0.0
    trades: int = 0                                             # closed round trips
    picks: Dict[str, List[str]] = field(default_factory=dict)   # decision date -> names

    def metrics(self) -> Dict[str, object]:
        return compute_metrics(self.dates, self.equity, [], self.costs)


def buy_and_hold_index(index, start: str, end: str, capital: float) -> BenchResult:
    res = BenchResult("Nifty 100 buy-and-hold")
    base = None
    for d, c in zip(index.dates, index.close):
        if d < start or (end and d > end):
            continue
        base = base or c
        res.dates.append(d)
        res.equity.append(capital * c / base)
    return res


def rotation_picks(ds, date: str, top_n: int = 10, lookback: int = 126, skip: int = 21,
                   slot: float = float("inf"), min_turnover: float = 0.0,
                   min_history: int = 260, ma_filter: int = 0) -> List[str]:
    """The names to hold after `date`'s close, ranked by return from
    `lookback + skip` sessions ago to `skip` sessions ago. Reads nothing after
    `date`. An empty list means hold cash."""
    if ma_filter:
        ii = ds.index.pos(date) if ds.index is not None else None
        if ii is None or ii + 1 < ma_filter:
            return []
        avg = sum(ds.index.close[ii + 1 - ma_filter: ii + 1]) / ma_filter
        if ds.index.close[ii] < avg:
            return []
    scored = []
    for sym, s in ds.series.items():
        i = s.pos(date)
        if i is None or i < max(min_history, lookback + skip, 20):
            continue
        if s.close[i] > slot:
            continue                                  # one share would exceed the slot
        traded = median([s.close[k] * s.volume[k] for k in range(i - 19, i + 1)])
        if traded != traded or traded < min_turnover:
            continue
        then = s.close[i - skip - lookback]
        if then > 0:
            scored.append((s.close[i - skip] / then - 1.0, sym))
    scored.sort(reverse=True)
    return [sym for _, sym in scored[:top_n]]


def momentum_rotation(ds, cfg: Config, start: str, end: str = "", top_n: int = 10,
                      lookback: int = 126, skip: int = 21, ma_filter: int = 0,
                      capital: Optional[float] = None) -> BenchResult:
    capital = float(capital if capital is not None else cfg.get("capital", 100000.0))
    res = BenchResult(f"Momentum top-{top_n}, {ma_filter}-DMA filter" if ma_filter
                      else f"Momentum top-{top_n} monthly")
    costs = CostModel(cfg)
    min_turnover = float(cfg.get("universe.min_turnover_cr", 25.0)) * 1e7
    min_history = int(cfg.get("universe.min_history_bars", 260))

    dates = [d for d in ds.all_dates() if d >= start and (not end or d <= end)]
    if len(dates) < 30:
        return res
    month_end = {dates[i] for i in range(len(dates) - 1) if dates[i][:7] != dates[i + 1][:7]}
    cash = capital
    held: Dict[str, int] = {}
    last_px: Dict[str, float] = {}
    pending: Optional[List[str]] = None

    def px(sym: str, date: str, attr: str) -> float:
        s = ds.series[sym]
        i = s.pos(date)
        return float("nan") if i is None else getattr(s, attr)[i]

    for d in dates:
        # ---- carry out the last month-end's decision at this open
        if pending is not None:
            target, pending = pending, None
            for sym in [s for s in held if s not in target]:
                op = px(sym, d, "open")
                if math.isnan(op):
                    continue                          # no session for it today; next month
                fill = costs.fill_price(op, "SELL")
                cb = costs.charges(fill, held[sym], "SELL", ref_price=op)
                cash += fill * held[sym] - (cb.total - cb.slippage)
                res.costs += cb.total
                res.trades += 1                       # a round trip closed
                del held[sym]
            slot = (cash + sum(q * last_px.get(s, 0.0) for s, q in held.items())) / top_n
            for sym in target:
                op = px(sym, d, "open")
                if sym in held or math.isnan(op) or op <= 0:
                    continue
                fill = costs.fill_price(op, "BUY")
                qty = int(min(slot, cash * 0.995) // (fill * 1.004))
                if qty <= 0:
                    continue
                cb = costs.charges(fill, qty, "BUY", ref_price=op)
                outlay = fill * qty + (cb.total - cb.slippage)
                if outlay > cash:
                    continue
                cash -= outlay
                res.costs += cb.total
                held[sym] = qty

        # ---- mark at the close
        for sym in held:
            c = px(sym, d, "close")
            if not math.isnan(c):
                last_px[sym] = c
        res.dates.append(d)
        res.equity.append(cash + sum(q * last_px.get(s, 0.0) for s, q in held.items()))

        # ---- month-end decision, for the next open
        if d in month_end:
            pending = rotation_picks(ds, d, top_n, lookback, skip, res.equity[-1] / top_n,
                                     min_turnover, min_history, ma_filter)
            res.picks[d] = pending
    return res
