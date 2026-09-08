"""Performance statistics.

Opinionated about which numbers matter for a Rs 1L swing book:

  * MONTHLY return distribution, not just CAGR. A system that averages 3% a
    month with a 9% standard deviation loses money in roughly one month in
    three, and you need to see that before you trade it, not after.
  * MAX DRAWDOWN and time-to-recover, because those are what actually make
    people abandon a working system.
  * COST DRAG as an explicit line item.
  * EXPECTANCY IN R, because that is the only number that stays comparable when
    position sizes change.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

from ..util import NA, is_na, mean, median, percentile, stdev

TRADING_DAYS = 252


def _returns(equity: Sequence[float]) -> List[float]:
    out = []
    for i in range(1, len(equity)):
        if equity[i - 1] > 0:
            out.append(equity[i] / equity[i - 1] - 1.0)
    return out


def monthly_returns(dates: Sequence[str], equity: Sequence[float]) -> Dict[str, float]:
    """Month-end to month-end returns keyed YYYY-MM."""
    if not dates:
        return {}
    last_by_month: Dict[str, float] = {}
    order: List[str] = []
    for d, e in zip(dates, equity):
        m = d[:7]
        if m not in last_by_month:
            order.append(m)
        last_by_month[m] = e
    out: Dict[str, float] = {}
    prev = equity[0]
    for m in order:
        cur = last_by_month[m]
        out[m] = (cur / prev - 1.0) if prev > 0 else NA
        prev = cur
    return out


def drawdown_stats(equity: Sequence[float], dates: Sequence[str]) -> Dict[str, object]:
    peak, mdd = -1e18, 0.0
    peak_i = trough_i = 0
    cur_peak_i = 0
    longest = 0
    under_since: Optional[int] = None
    for i, e in enumerate(equity):
        if e > peak:
            peak, cur_peak_i = e, i
            if under_since is not None:
                longest = max(longest, i - under_since)
                under_since = None
        else:
            if under_since is None:
                under_since = i
            dd = e / peak - 1.0 if peak > 0 else 0.0
            if dd < mdd:
                mdd, peak_i, trough_i = dd, cur_peak_i, i
    if under_since is not None:
        longest = max(longest, len(equity) - under_since)
    return {
        "max_drawdown": mdd,
        "max_dd_peak_date": dates[peak_i] if dates else "",
        "max_dd_trough_date": dates[trough_i] if dates else "",
        "longest_underwater_days": longest,
    }


def compute_metrics(dates: Sequence[str], equity: Sequence[float], trades: Sequence,
                    costs_total: float = 0.0, rf_annual: float = 0.065) -> Dict[str, object]:
    """rf_annual defaults to 6.5% - an Indian system must beat a liquid FD, not zero."""
    m: Dict[str, object] = {}
    if len(equity) < 2:
        return {"error": "not enough data"}

    start_eq, end_eq = equity[0], equity[-1]
    n_days = len(equity)
    years = n_days / TRADING_DAYS
    rets = _returns(equity)

    m["start_equity"] = start_eq
    m["end_equity"] = end_eq
    m["total_return"] = end_eq / start_eq - 1.0 if start_eq > 0 else NA
    m["cagr"] = ((end_eq / start_eq) ** (1 / years) - 1.0) if (years > 0 and start_eq > 0
                                                               and end_eq > 0) else NA
    m["years"] = years
    m["n_days"] = n_days

    sd = stdev(rets)
    m["ann_vol"] = sd * math.sqrt(TRADING_DAYS) if not is_na(sd) else NA
    mu = mean(rets)
    rf_daily = (1 + rf_annual) ** (1 / TRADING_DAYS) - 1
    m["sharpe"] = ((mu - rf_daily) / sd * math.sqrt(TRADING_DAYS)) if (
        not is_na(sd) and sd > 0) else NA
    downside = [r for r in rets if r < 0]
    dsd = stdev(downside) if len(downside) > 2 else NA
    m["sortino"] = ((mu - rf_daily) / dsd * math.sqrt(TRADING_DAYS)) if (
        not is_na(dsd) and dsd > 0) else NA

    dd = drawdown_stats(equity, dates)
    m.update(dd)
    mdd = abs(dd["max_drawdown"])
    m["calmar"] = (m["cagr"] / mdd) if (mdd > 0 and not is_na(m["cagr"])) else NA

    mr = monthly_returns(dates, equity)
    vals = [v for v in mr.values() if not is_na(v)]
    m["monthly_returns"] = mr
    m["monthly_mean"] = mean(vals)
    m["monthly_median"] = median(vals)
    m["monthly_std"] = stdev(vals)
    m["monthly_best"] = max(vals) if vals else NA
    m["monthly_worst"] = min(vals) if vals else NA
    m["monthly_win_rate"] = (sum(1 for v in vals if v > 0) / len(vals)) if vals else NA
    m["months_ge_8pct"] = sum(1 for v in vals if v >= 0.08)
    m["n_months"] = len(vals)
    m["monthly_p05"] = percentile(vals, 5)
    m["monthly_p95"] = percentile(vals, 95)

    # ------------------------------------------------------------- trade stats
    n = len(trades)
    m["n_trades"] = n
    m["trades_per_year"] = n / years if years > 0 else NA
    if n:
        pnls = [t.net_pnl for t in trades]
        rs = [t.r_multiple for t in trades if not is_na(t.r_multiple)]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        m["win_rate"] = len(wins) / n
        m["avg_win"] = mean(wins) if wins else 0.0
        m["avg_loss"] = mean(losses) if losses else 0.0
        m["payoff_ratio"] = (abs(m["avg_win"] / m["avg_loss"])
                             if losses and m["avg_loss"] != 0 else NA)
        gp = sum(wins)
        gl = abs(sum(losses))
        m["profit_factor"] = (gp / gl) if gl > 0 else NA
        m["expectancy_rs"] = mean(pnls)
        m["expectancy_r"] = mean(rs) if rs else NA
        m["r_std"] = stdev(rs) if len(rs) > 2 else NA
        m["best_trade_r"] = max(rs) if rs else NA
        m["worst_trade_r"] = min(rs) if rs else NA
        m["avg_bars_held"] = mean([t.bars_held for t in trades])
        m["median_bars_held"] = median([t.bars_held for t in trades])
        # System Quality Number: expectancy relative to its own noise. Below ~1.5
        # you cannot distinguish the edge from luck at this sample size.
        if not is_na(m.get("r_std")) and m["r_std"] > 0:
            m["sqn"] = (m["expectancy_r"] / m["r_std"]) * math.sqrt(min(n, 100))
        else:
            m["sqn"] = NA
        by_reason: Dict[str, List[float]] = {}
        for t in trades:
            by_reason.setdefault(t.exit_reason, []).append(t.r_multiple)
        m["exit_reason_stats"] = {
            k: {"n": len(v), "avg_r": mean(v)} for k, v in sorted(by_reason.items())}
        by_setup: Dict[str, List[float]] = {}
        for t in trades:
            by_setup.setdefault(t.setup or "?", []).append(t.r_multiple)
        m["setup_stats"] = {
            k: {"n": len(v), "avg_r": mean(v), "win_rate":
                sum(1 for x in v if x > 0) / len(v)} for k, v in sorted(by_setup.items())}
        m["gross_profit"] = gp
        m["gross_loss"] = gl
    else:
        for k in ("win_rate", "avg_win", "avg_loss", "payoff_ratio", "profit_factor",
                  "expectancy_rs", "expectancy_r", "sqn", "avg_bars_held"):
            m[k] = NA
        m["exit_reason_stats"] = {}
        m["setup_stats"] = {}

    m["costs_total"] = costs_total
    m["cost_drag_pct_of_start"] = costs_total / start_eq if start_eq > 0 else NA
    m["cost_per_trade"] = (costs_total / n) if n else NA
    gross_profit_net_of_costs = end_eq - start_eq
    m["costs_vs_net_profit"] = (costs_total / gross_profit_net_of_costs
                                if gross_profit_net_of_costs > 0 else NA)
    return m


def summary_lines(m: Dict[str, object]) -> List[str]:
    def pct(k, d=2):
        v = m.get(k)
        return "n/a" if is_na(v) or v is None else f"{100*v:.{d}f}%"

    def num(k, d=2):
        v = m.get(k)
        return "n/a" if is_na(v) or v is None else f"{v:.{d}f}"

    return [
        f"Period            : {m.get('n_days')} sessions ({num('years',1)} years)",
        f"Equity            : {m.get('start_equity'):,.0f} -> {m.get('end_equity'):,.0f}",
        f"Total return      : {pct('total_return')}",
        f"CAGR              : {pct('cagr')}",
        f"Ann. volatility   : {pct('ann_vol')}",
        f"Sharpe (rf 6.5%)  : {num('sharpe')}",
        f"Sortino           : {num('sortino')}",
        f"Max drawdown      : {pct('max_drawdown')}  ({m.get('max_dd_peak_date')} -> {m.get('max_dd_trough_date')})",
        f"Longest underwater: {m.get('longest_underwater_days')} sessions",
        f"Calmar            : {num('calmar')}",
        "",
        f"Monthly mean      : {pct('monthly_mean')}   median {pct('monthly_median')}",
        f"Monthly std dev   : {pct('monthly_std')}",
        f"Monthly best/worst: {pct('monthly_best')} / {pct('monthly_worst')}",
        f"Positive months   : {pct('monthly_win_rate',1)} of {m.get('n_months')}",
        f"Months >= +8%     : {m.get('months_ge_8pct')} of {m.get('n_months')}",
        "",
        f"Trades            : {m.get('n_trades')} ({num('trades_per_year',1)}/yr)",
        f"Win rate          : {pct('win_rate',1)}",
        f"Payoff ratio      : {num('payoff_ratio')}",
        f"Profit factor     : {num('profit_factor')}",
        f"Expectancy        : {num('expectancy_r')} R  ({m.get('expectancy_rs') if is_na(m.get('expectancy_rs')) else format(m.get('expectancy_rs'), ',.0f')} per trade)",
        f"SQN               : {num('sqn')}",
        f"Avg bars held     : {num('avg_bars_held',1)}",
        "",
        f"Total costs       : {m.get('costs_total'):,.0f}  ({pct('cost_drag_pct_of_start')} of starting capital)",
        f"Cost per trade    : {m.get('cost_per_trade') if is_na(m.get('cost_per_trade')) else format(m.get('cost_per_trade'), ',.0f')}",
    ]
