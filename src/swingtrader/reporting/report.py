"""Backtest reporting: a self-contained HTML file plus terminal-friendly text.

No plotting library. The equity curve and drawdown are emitted as inline SVG,
so the report is one file you can email, open offline, or keep next to the run
that produced it. The alternative - a matplotlib dependency - buys nothing here
and breaks on exactly the machine you need it on.
"""
from __future__ import annotations

import html
import os
from typing import Any, Dict, List, Optional, Sequence

from ..util import fmt_inr, is_na

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def monthly_table_text(monthly: Dict[str, float]) -> List[str]:
    if not monthly:
        return ["(no monthly data)"]
    years = sorted({k[:4] for k in monthly})
    out = ["Year  " + " ".join(f"{m:>7}" for m in MONTHS) + f" {'YEAR':>8}"]
    for y in years:
        row = [f"{y}  "]
        comp = 1.0
        for mi in range(1, 13):
            key = f"{y}-{mi:02d}"
            v = monthly.get(key)
            if v is None or is_na(v):
                row.append(f"{'':>7}")
            else:
                comp *= (1 + v)
                row.append(f"{100*v:>6.1f}%")
        row.append(f"{100*(comp-1):>7.1f}%")
        out.append(" ".join(row))
    return out


def _svg_path(values: Sequence[float], w: int, h: int, pad: int,
              log: bool = False) -> str:
    import math
    if len(values) < 2:
        return ""
    # The positive clamp exists only to keep log() defined. Applying it
    # unconditionally silently flattened the drawdown chart to a straight line,
    # since every drawdown value is <= 0.
    vals = [math.log(max(v, 1e-9)) for v in values] if log else list(values)
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = pad + (w - 2 * pad) * i / (n - 1)
        y = h - pad - (h - 2 * pad) * (v - lo) / rng
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def equity_svg(dates: Sequence[str], equity: Sequence[float],
               drawdown: Sequence[float], width: int = 900) -> str:
    h1, h2, pad = 260, 120, 34
    eq_pts = _svg_path(equity, width, h1, pad, log=True)
    dd_pts = _svg_path(drawdown, width, h2, pad)
    if not eq_pts:
        return "<p>(not enough data to plot)</p>"
    lo, hi = min(equity), max(equity)
    mdd = min(drawdown) if drawdown else 0.0
    first, last = dates[0], dates[-1]
    return f"""
<svg viewBox="0 0 {width} {h1}" class="chart" role="img" aria-label="Equity curve">
  <rect x="0" y="0" width="{width}" height="{h1}" fill="none"/>
  <polyline points="{eq_pts}" fill="none" stroke="#1a6c4a" stroke-width="1.8"/>
  <text x="{pad}" y="16" class="lbl">equity (log scale)  high Rs {fmt_inr(hi)}</text>
  <text x="{pad}" y="{h1-8}" class="lbl">{html.escape(first)}</text>
  <text x="{width-pad}" y="{h1-8}" class="lbl" text-anchor="end">{html.escape(last)}</text>
  <text x="{width-pad}" y="16" class="lbl" text-anchor="end">low Rs {fmt_inr(lo)}</text>
</svg>
<svg viewBox="0 0 {width} {h2}" class="chart" role="img" aria-label="Drawdown">
  <polyline points="{dd_pts}" fill="none" stroke="#a33" stroke-width="1.4"/>
  <text x="{pad}" y="16" class="lbl">drawdown  worst {100*mdd:.1f}%</text>
</svg>"""


def equity_sparkline(equity: Sequence[float], width: int = 60) -> str:
    """ASCII sparkline for the terminal."""
    blocks = "_.-~=*#"
    if len(equity) < 2:
        return ""
    step = max(1, len(equity) // width)
    sampled = equity[::step]
    lo, hi = min(sampled), max(sampled)
    rng = (hi - lo) or 1.0
    return "".join(blocks[min(len(blocks) - 1,
                              int((v - lo) / rng * (len(blocks) - 1)))] for v in sampled)


def _fmt_pct(v, d=2):
    return "n/a" if v is None or is_na(v) else f"{100*v:.{d}f}%"


def _fmt_num(v, d=2):
    return "n/a" if v is None or is_na(v) else f"{v:.{d}f}"


def write_report(path: str, title: str, metrics: Dict[str, Any], result,
                 cfg, extra_sections: Optional[List[tuple]] = None,
                 data_note: str = "") -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    dates = result.dates
    equity = result.equity_curve
    dd = [d.drawdown for d in result.daily]

    warn = ""
    if data_note:
        warn = (f'<div class="warn"><strong>Data:</strong> {html.escape(data_note)}</div>')

    rows = []
    def row(label, value, note=""):
        rows.append(f"<tr><th>{html.escape(label)}</th><td>{value}</td>"
                    f"<td class='note'>{html.escape(note)}</td></tr>")

    row("Period", f"{metrics.get('n_days')} sessions ({_fmt_num(metrics.get('years'),1)} years)")
    row("Equity", f"Rs {fmt_inr(metrics.get('start_equity'))} &rarr; "
                  f"Rs {fmt_inr(metrics.get('end_equity'))}")
    row("CAGR", _fmt_pct(metrics.get("cagr")))
    row("Max drawdown", _fmt_pct(metrics.get("max_drawdown")),
        f"{metrics.get('max_dd_peak_date')} to {metrics.get('max_dd_trough_date')}")
    row("Longest underwater", f"{metrics.get('longest_underwater_days')} sessions",
        "how long you would have had to keep going while it was not working")
    row("Sharpe / Sortino", f"{_fmt_num(metrics.get('sharpe'))} / "
                            f"{_fmt_num(metrics.get('sortino'))}", "risk-free 6.5%")
    row("Calmar", _fmt_num(metrics.get("calmar")))
    row("Annual volatility", _fmt_pct(metrics.get("ann_vol")))

    row("Monthly mean / median", f"{_fmt_pct(metrics.get('monthly_mean'))} / "
                                 f"{_fmt_pct(metrics.get('monthly_median'))}")
    row("Monthly std dev", _fmt_pct(metrics.get("monthly_std")))
    row("Monthly best / worst", f"{_fmt_pct(metrics.get('monthly_best'))} / "
                                f"{_fmt_pct(metrics.get('monthly_worst'))}")
    row("Positive months", _fmt_pct(metrics.get("monthly_win_rate"), 1),
        f"of {metrics.get('n_months')} months")
    row("Months at or above +8%", f"{metrics.get('months_ge_8pct')} of {metrics.get('n_months')}",
        "the stated monthly target")

    row("Trades", f"{metrics.get('n_trades')}",
        f"{_fmt_num(metrics.get('trades_per_year'),1)} per year")
    row("Win rate", _fmt_pct(metrics.get("win_rate"), 1))
    row("Payoff ratio", _fmt_num(metrics.get("payoff_ratio")), "avg win / avg loss")
    row("Profit factor", _fmt_num(metrics.get("profit_factor")))
    row("Expectancy", f"{_fmt_num(metrics.get('expectancy_r'))} R",
        "average result per trade in units of risk")
    row("SQN", _fmt_num(metrics.get("sqn")), "below ~1.5 the edge is not distinguishable from luck")
    row("Avg holding period", f"{_fmt_num(metrics.get('avg_bars_held'),1)} sessions")
    row("Total costs", f"Rs {fmt_inr(metrics.get('costs_total'))}",
        f"{_fmt_pct(metrics.get('cost_drag_pct_of_start'))} of starting capital; "
        f"Rs {fmt_inr(metrics.get('cost_per_trade'))} per trade")

    exit_rows = "".join(
        f"<tr><td>{html.escape(k)}</td><td>{v['n']}</td><td>{v['avg_r']:+.2f}</td></tr>"
        for k, v in (metrics.get("exit_reason_stats") or {}).items())
    setup_rows = "".join(
        f"<tr><td>{html.escape(k)}</td><td>{v['n']}</td><td>{v['avg_r']:+.2f}</td>"
        f"<td>{100*v['win_rate']:.0f}%</td></tr>"
        for k, v in (metrics.get("setup_stats") or {}).items())

    cost_rows = "".join(
        f"<tr><td>{html.escape(k)}</td><td>Rs {fmt_inr(v)}</td></tr>"
        for k, v in sorted((result.costs_breakdown or {}).items(), key=lambda x: -x[1]))

    monthly = metrics.get("monthly_returns") or {}
    mt = "\n".join(monthly_table_text(monthly))

    extras = ""
    for heading, body in (extra_sections or []):
        extras += f"<h2>{html.escape(heading)}</h2>\n<pre>{html.escape(body)}</pre>\n"

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         margin: 0 auto; max-width: 980px; padding: 24px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  h2 {{ font-size: 16px; margin: 28px 0 8px; border-bottom: 1px solid #8884; padding-bottom: 4px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 8px 0 16px; }}
  th, td {{ text-align: left; padding: 5px 8px; border-bottom: 1px solid #8883; vertical-align: top; }}
  th {{ width: 210px; font-weight: 600; }}
  td.note, .note {{ color: #7a7a7a; font-size: 12.5px; }}
  pre {{ overflow-x: auto; background: #8881; padding: 12px; border-radius: 6px; font-size: 12.5px; }}
  .chart {{ width: 100%; height: auto; background: #8881; border-radius: 6px; margin-bottom: 10px; }}
  .lbl {{ font-size: 11px; fill: #888; }}
  .warn {{ background: #f6c34322; border-left: 3px solid #f6c343; padding: 10px 12px;
           border-radius: 4px; margin: 12px 0; }}
  .sub {{ color: #7a7a7a; margin: 0 0 16px; }}
</style></head><body>
<h1>{html.escape(title)}</h1>
<p class="sub">Generated by swingtrader &middot; Nifty 100 cash / Groww</p>
{warn}
{equity_svg(dates, equity, dd)}
<h2>Headline</h2>
<table>{''.join(rows)}</table>
<h2>Monthly returns</h2>
<pre>{html.escape(mt)}</pre>
<h2>Exits</h2>
<table><tr><th>Reason</th><th>N</th><th>Avg R</th></tr>{exit_rows}</table>
<h2>Setups</h2>
<table><tr><th>Setup</th><th>N</th><th>Avg R</th><th>Win rate</th></tr>{setup_rows}</table>
<h2>Where the money went in costs</h2>
<table><tr><th>Charge</th><th>Total</th></tr>{cost_rows}</table>
{extras}
<h2>Configuration</h2>
<pre>{html.escape(chr(10).join(f'{k} = {v}' for k, v in sorted(cfg.flatten().items())))}</pre>
</body></html>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path
