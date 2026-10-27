"""Backtest-versus-live parity.

Runs the production executor and daemon over replayed sessions, runs the
backtest engine over the same sessions, and compares the trades. If the two
disagree about which trades happen, then the live system is not trading the
strategy the backtest validated - which is the single most expensive way a
systematic setup fails, because nothing about it looks broken.

Expected, legitimate differences: live fills happen seconds after the open
rather than exactly at it, and broker stops fire on the first poll at or below
the trigger rather than at the trigger price. Those move prices by basis
points. They should not change WHICH trades occur.
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Dict, List

from ..backtest import compute_metrics
from ..config import Config
from ..learn import Journal
from ..runtime import Dataset, run_backtest
from . import IST, build_replay


def compare(cfg: Config, ds: Dataset, first: int, last: int, workdir: str,
            echo: bool = False) -> Dict[str, object]:
    dates = ds.all_dates()
    start, end = dates[first], dates[last]

    daemon, ex, broker, clock = build_replay(
        cfg, ds, dt.date.fromisoformat(start), os.path.join(workdir, "state"),
        os.path.join(workdir, "orders"), echo=echo)
    daemon.run(until=dt.datetime.combine(dt.date.fromisoformat(end), dt.time(17, 0),
                                         tzinfo=IST))
    rows, _ = Journal(ex.journal_path).read()
    live_closed = {(r["symbol"], r["entry_date"]): r for r in rows}
    live_open = {(p.symbol, p.entry_date): p for p in ex.book.positions}
    live_entries = set(live_closed) | set(live_open)

    bt = run_backtest(cfg, ds, start=start, end=end)
    bt_closed = {(t.symbol, t.entry_date): t for t in bt.trades}
    bt_open = {(p["symbol"], p["entry_date"]): p for p in bt.open_positions}
    bt_entries = set(bt_closed) | set(bt_open)

    both = live_entries & bt_entries
    exit_match = sum(1 for k in set(live_closed) & set(bt_closed)
                     if live_closed[k]["exit_date"] == bt_closed[k].exit_date)
    px_err = [abs(live_closed[k]["entry_price"] / bt_closed[k].entry_price - 1)
              for k in set(live_closed) & set(bt_closed)]
    # Initial stop of every trade both systems took. This is the sharpest check
    # there is: a stop that is not re-anchored to the fill shows up here even in
    # a window where no exit happens to diverge because of it.
    stop_gap = []
    for k in both:
        lv = live_closed[k]["initial_stop"] if k in live_closed else live_open[k].initial_stop
        bv = bt_closed[k].initial_stop if k in bt_closed else None
        if bv is None:
            continue
        stop_gap.append(abs(lv / bv - 1))

    eq_live = ex.equity(end)
    m = compute_metrics(bt.dates, bt.equity_curve, bt.trades, bt.costs_total)
    return {
        "window": (start, end), "sessions": last - first + 1,
        "live_entries": len(live_entries), "backtest_entries": len(bt_entries),
        "matched_entries": len(both),
        "only_live": sorted(live_entries - bt_entries),
        "only_backtest": sorted(bt_entries - live_entries),
        "closed_in_both": len(set(live_closed) & set(bt_closed)),
        "same_exit_date": exit_match,
        "median_entry_price_gap": sorted(px_err)[len(px_err) // 2] if px_err else 0.0,
        "max_initial_stop_gap": max(stop_gap) if stop_gap else 0.0,
        "equity_live": eq_live, "equity_backtest": m.get("end_equity"),
        "executor": ex,
    }


def summary_lines(r: Dict[str, object]) -> List[str]:
    eq_l, eq_b = r["equity_live"], r["equity_backtest"]
    return [
        f"window            {r['window'][0]} .. {r['window'][1]} ({r['sessions']} sessions)",
        f"entries           live {r['live_entries']}, backtest {r['backtest_entries']}, "
        f"matched {r['matched_entries']}",
        f"only in live      {r['only_live'] or '-'}",
        f"only in backtest  {r['only_backtest'] or '-'}",
        f"same exit date    {r['same_exit_date']} of {r['closed_in_both']} closed in both",
        f"entry price gap   median {100*r['median_entry_price_gap']:.3f}%",
        f"initial stop gap  max {100*r['max_initial_stop_gap']:.3f}%",
        f"ending equity     live {eq_l:,.0f} vs backtest {eq_b:,.0f} "
        f"({100*(eq_l/eq_b-1):+.2f}%)",
    ]
