"""Purged, anchored-forward walk-forward evaluation.

The point of this file is to make it hard to lie to yourself.

A single backtest over 2015-2025 with parameters chosen by looking at
2015-2025 tells you nothing: with ~40 tunable knobs you can fit any curve you
like. What you need is the answer to a narrower, harder question - "if I had
chosen parameters using only data available at the time, what would the next
six months have looked like?" - asked repeatedly, and then stitched together.

Two details that are easy to skip and expensive to skip:

  PURGING/EMBARGO. A trade opened before the train/test boundary can still be
  open after it. Without a gap between train and test, the same trade appears
  in both, and the test window inherits information from the training window.
  We insert `embargo_days` of dead space between them.

  FRESH CAPITAL PER FOLD. Each test window starts from the configured capital,
  not from wherever the last fold ended. Otherwise one lucky early fold
  compounds into every later fold's position sizing and the OOS record becomes
  a story about that one fold.
"""
from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..backtest.metrics import compute_metrics
from ..config import Config
from ..util import NA, is_na, mean, median, stdev


@dataclass
class Fold:
    index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str

    def describe(self) -> str:
        return (f"fold {self.index}: train {self.train_start}..{self.train_end}  "
                f"test {self.test_start}..{self.test_end}")


def _shift_days(date: str, days: int) -> str:
    d = _dt.date.fromisoformat(date) + _dt.timedelta(days=days)
    return d.isoformat()


def _shift_months(date: str, months: int) -> str:
    d = _dt.date.fromisoformat(date)
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28,
                      31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return _dt.date(y, m, day).isoformat()


def make_folds(dates: Sequence[str], train_years: float, test_months: int,
               embargo_days: int, warmup_bars: int = 260) -> List[Fold]:
    """Rolling folds across the available history."""
    if not dates:
        return []
    first, last = dates[0], dates[-1]
    # leave room for indicator warm-up before the first training window
    origin = dates[min(warmup_bars, len(dates) - 1)]
    folds: List[Fold] = []
    train_start = origin
    i = 0
    while True:
        train_end = _shift_months(train_start, int(round(train_years * 12)))
        test_start = _shift_days(train_end, embargo_days)
        test_end = _shift_months(test_start, test_months)
        if test_start >= last:
            break
        folds.append(Fold(i, train_start, train_end, test_start,
                          min(test_end, last)))
        i += 1
        train_start = _shift_months(train_start, test_months)
        if train_end >= last:
            break
    return folds


# --------------------------------------------------------------------- scoring
def objective_score(metrics: Dict[str, object], name: str = "calmar_turnover",
                    min_trades: int = 10) -> float:
    """Turn a metrics dict into one number. Higher is better; -inf is a reject.

    `calmar_turnover` is the default because on a Rs 1L account the two things
    that actually kill you are depth of drawdown and trading costs, and Calmar
    alone is happy to reward a system that churns.
    """
    n = metrics.get("n_trades") or 0
    if n < min_trades:
        return float("-inf")
    cagr = metrics.get("cagr")
    if is_na(cagr) or cagr is None:
        return float("-inf")
    mdd = abs(metrics.get("max_drawdown") or 0.0)

    if name == "sharpe":
        s = metrics.get("sharpe")
        return float("-inf") if is_na(s) else float(s)
    if name == "expectancy_r":
        e = metrics.get("expectancy_r")
        return float("-inf") if is_na(e) else float(e)
    if name == "profit_factor":
        pf = metrics.get("profit_factor")
        return float("-inf") if is_na(pf) else float(pf)
    if name == "cagr":
        return float(cagr)

    # calmar_turnover (default)
    calmar = cagr / max(mdd, 0.05)
    years = metrics.get("years") or 1.0
    cost_drag = (metrics.get("costs_total") or 0.0) / max(
        metrics.get("start_equity") or 1.0, 1.0) / max(years, 0.25)
    # Charge the score for frictions explicitly: a config that gets there by
    # trading twice as much is worse than one that does not, even at equal Calmar.
    return calmar - 4.0 * cost_drag


@dataclass
class WalkForwardResult:
    folds: List[Fold] = field(default_factory=list)
    chosen_params: List[Dict[str, object]] = field(default_factory=list)
    fold_metrics: List[Dict[str, object]] = field(default_factory=list)
    oos_returns: List[Tuple[str, float]] = field(default_factory=list)
    oos_trades: List[object] = field(default_factory=list)
    stitched_metrics: Dict[str, object] = field(default_factory=dict)
    param_stability: Dict[str, object] = field(default_factory=dict)

    def summary(self) -> List[str]:
        out = [f"Folds evaluated   : {len(self.folds)}"]
        oos = [m.get("cagr") for m in self.fold_metrics if not is_na(m.get("cagr"))]
        if oos:
            out.append(f"OOS fold CAGR     : median {100*median(oos):.1f}%  "
                       f"best {100*max(oos):.1f}%  worst {100*min(oos):.1f}%")
            out.append(f"Folds positive    : {sum(1 for x in oos if x > 0)}/{len(oos)}")
        return out


class WalkForward:
    def __init__(self, cfg: Config, dataset, runner: Callable, verbose: bool = True):
        self.cfg = cfg
        self.ds = dataset
        self.runner = runner            # runner(cfg, ds, start, end) -> BacktestResult
        self.verbose = verbose
        L = cfg.get("learn", {})
        self.train_years = float(L.get("train_years", 4))
        self.test_months = int(L.get("test_months", 6))
        self.embargo_days = int(L.get("embargo_days", 10))
        self.objective = str(L.get("objective", "calmar_turnover"))
        self.min_trades = int(L.get("promotion_min_trades", 40))

    def folds(self) -> List[Fold]:
        return make_folds(self.ds.all_dates(), self.train_years, self.test_months,
                          self.embargo_days, int(self.cfg.get("backtest.warmup_bars", 260)))

    def evaluate(self, cfg: Config, start: str, end: str) -> Dict[str, object]:
        res = self.runner(cfg, self.ds, start, end)
        m = compute_metrics(res.dates, res.equity_curve, res.trades, res.costs_total)
        m["_result"] = res
        return m

    def run(self, select: Optional[Callable[[str, str], Config]] = None
            ) -> WalkForwardResult:
        """`select(train_start, train_end) -> Config` picks parameters using ONLY
        the training window. Pass None to evaluate the current config as-is."""
        out = WalkForwardResult()
        out.folds = self.folds()
        stitched_equity: List[float] = []
        stitched_dates: List[str] = []
        running = float(self.cfg.get("capital", 100000.0))

        for f in out.folds:
            cfg_f = select(f.train_start, f.train_end) if select else self.cfg
            out.chosen_params.append(
                {k: cfg_f.get(k) for k in sorted(cfg_f.flatten())} if select else {})
            m = self.evaluate(cfg_f, f.test_start, f.test_end)
            m["fold"] = f.index
            m["test_start"], m["test_end"] = f.test_start, f.test_end
            res = m.pop("_result")
            out.fold_metrics.append(m)
            out.oos_trades.extend(res.trades)

            # Stitch OOS windows into one continuous curve by compounding each
            # fold's RETURNS onto the running equity. Fresh capital per fold for
            # sizing, continuous curve for evaluation.
            eq = res.equity_curve
            if len(eq) >= 2 and eq[0] > 0:
                for i in range(1, len(eq)):
                    r = eq[i] / eq[i - 1] - 1.0
                    running *= (1.0 + r)
                    stitched_dates.append(res.dates[i])
                    stitched_equity.append(running)
                    out.oos_returns.append((res.dates[i], r))

            if self.verbose:
                cagr = m.get("cagr")
                print(f"  {f.describe()}  OOS CAGR "
                      f"{'n/a' if is_na(cagr) else format(100*cagr, '6.1f') + '%'}  "
                      f"trades {m.get('n_trades')}")

        if stitched_equity:
            start_eq = float(self.cfg.get("capital", 100000.0))
            out.stitched_metrics = compute_metrics(
                stitched_dates, [start_eq] + stitched_equity, out.oos_trades,
                sum(0.0 for _ in out.oos_trades))
        return out


def stability_report(param_sets: Sequence[Dict[str, object]],
                     keys: Sequence[str]) -> Dict[str, object]:
    """How much did each parameter jump around between folds?

    A parameter the optimiser re-picks wildly every six months is not a
    parameter, it is noise being fitted. Prefer to freeze those at a sensible
    prior rather than let them wander.
    """
    out: Dict[str, object] = {}
    for k in keys:
        vals = [p.get(k) for p in param_sets if isinstance(p.get(k), (int, float))]
        if len(vals) < 2:
            continue
        m = mean(vals)
        sd = stdev(vals)
        out[k] = {
            "mean": m, "std": sd, "min": min(vals), "max": max(vals),
            "cv": (sd / abs(m)) if (m and not is_na(sd)) else NA,
            "values": vals,
        }
    return out
