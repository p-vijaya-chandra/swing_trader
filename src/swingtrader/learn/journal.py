"""Trade journal, feature attribution and edge-decay monitoring.

This is where "self-improving" stops being a slogan.

The optimiser tunes parameters against history. The journal does something the
optimiser cannot: it watches what the system is doing NOW, in your account, and
answers two questions.

  1. WHICH CONDITIONS ACTUALLY PAY? Every trade stores the feature vector that
     was true at entry. Bucketing realised R by feature decile shows you where
     the edge lives - and, more usefully, where it is reliably absent. That is
     evidence for tightening a filter, and it is evidence you can read, argue
     with, and reject. It does not silently rewrite your strategy.

  2. HAS THE EDGE GONE? Every strategy decays. The decay monitor compares recent
     live expectancy against the backtest's own bootstrap distribution. If live
     results fall below the 5th percentile of what the backtest said to expect,
     something has changed - and the response is to cut risk and investigate,
     automatically, before the drawdown makes the decision for you.
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..util import NA, bootstrap_mean_ci, is_na, mean, median, percentile, stdev


class Journal:
    """Append-only CSV of completed trades. Backtest and live use the same file
    format, so the same analysis runs over both."""

    def __init__(self, path: str = "state/journal.csv"):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def append(self, trades: Sequence) -> int:
        if not trades:
            return 0
        rows = [t.as_row() if hasattr(t, "as_row") else dict(t) for t in trades]
        keys: List[str] = []
        for r in rows:
            for k in r:
                if k not in keys:
                    keys.append(k)
        exists = os.path.exists(self.path)
        old_keys: List[str] = []
        old_rows: List[Dict[str, Any]] = []
        if exists:
            old_rows, old_keys = self.read()
            for k in keys:
                if k not in old_keys:
                    old_keys.append(k)
            keys = old_keys

        with open(self.path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            for r in old_rows + rows:
                w.writerow(r)
        return len(rows)

    def read(self) -> Tuple[List[Dict[str, Any]], List[str]]:
        if not os.path.exists(self.path):
            return [], []
        with open(self.path, "r", encoding="utf-8") as fh:
            rd = csv.DictReader(fh)
            keys = list(rd.fieldnames or [])
            rows = []
            for r in rd:
                out: Dict[str, Any] = {}
                for k, v in r.items():
                    if v is None or v == "":
                        out[k] = None
                        continue
                    try:
                        out[k] = float(v)
                    except (TypeError, ValueError):
                        out[k] = v
                rows.append(out)
        return rows, keys

    def clear(self) -> None:
        if os.path.exists(self.path):
            os.remove(self.path)


# Absolute rupee levels are meaningless across symbols: "trades where the price
# was above Rs 2,700 did better" is a statement about which stocks happened to be
# expensive, not about a condition you can screen on. Bucketing them produces
# confident-looking noise, so they are excluded from attribution entirely.
LEVEL_FEATURES = {
    "close", "ema_fast", "ema_slow", "ema_pull", "atr", "hh_52w",
    "donchian_high", "structure_low", "vol_avg", "turnover_med",
}


# --------------------------------------------------------------- attribution
def feature_attribution(trades: Sequence, features: Optional[Sequence[str]] = None,
                        n_buckets: int = 4, min_per_bucket: int = 8) -> Dict[str, Any]:
    """Realised R bucketed by entry-feature quantile.

    Read this as a hypothesis generator, never as an instruction. With 150 trades
    and 20 features you WILL find a spurious pattern; the point is to surface
    candidates that you then test properly with a walk-forward run.
    """
    rows: List[Dict[str, Any]] = []
    for t in trades:
        r = t.as_row() if hasattr(t, "as_row") else dict(t)
        rows.append(r)
    if not rows:
        return {}

    if features is None:
        features = sorted({k for r in rows for k in r
                           if k.startswith("f_")
                           and k[2:] not in LEVEL_FEATURES
                           and isinstance(r.get(k), (int, float))})

    out: Dict[str, Any] = {}
    for feat in features:
        pairs = [(r[feat], r.get("r_multiple")) for r in rows
                 if isinstance(r.get(feat), (int, float))
                 and isinstance(r.get("r_multiple"), (int, float))
                 and not is_na(r[feat]) and not is_na(r.get("r_multiple"))]
        if len(pairs) < n_buckets * min_per_bucket:
            continue
        pairs.sort(key=lambda p: p[0])
        size = len(pairs) // n_buckets
        buckets = []
        for b in range(n_buckets):
            lo = b * size
            hi = len(pairs) if b == n_buckets - 1 else (b + 1) * size
            chunk = pairs[lo:hi]
            if not chunk:
                continue
            rs = [r for _, r in chunk]
            buckets.append({
                "bucket": b + 1,
                "range": (chunk[0][0], chunk[-1][0]),
                "n": len(chunk),
                "avg_r": mean(rs),
                "win_rate": sum(1 for x in rs if x > 0) / len(rs),
            })
        if len(buckets) < 2:
            continue
        spread = buckets[-1]["avg_r"] - buckets[0]["avg_r"]
        out[feat] = {"buckets": buckets, "spread_r": spread,
                     "monotonic": all(buckets[i]["avg_r"] <= buckets[i + 1]["avg_r"]
                                      for i in range(len(buckets) - 1))
                     or all(buckets[i]["avg_r"] >= buckets[i + 1]["avg_r"]
                            for i in range(len(buckets) - 1))}
    return out


def attribution_suggestions(attr: Dict[str, Any], min_spread: float = 0.5,
                            min_bucket_n: int = 15) -> List[str]:
    """Turn attribution into plain-language candidates for a human to consider."""
    out: List[str] = []
    for feat, d in sorted(attr.items(), key=lambda kv: -abs(kv[1]["spread_r"])):
        if abs(d["spread_r"]) < min_spread:
            continue
        b = d["buckets"]
        worst = min(b, key=lambda x: x["avg_r"])
        best = max(b, key=lambda x: x["avg_r"])
        if worst["n"] < min_bucket_n:
            continue
        name = feat[2:] if feat.startswith("f_") else feat
        out.append(
            f"{name}: worst quartile [{worst['range'][0]:.4g}..{worst['range'][1]:.4g}] "
            f"averages {worst['avg_r']:+.2f}R over {worst['n']} trades, "
            f"best quartile averages {best['avg_r']:+.2f}R"
            + ("  (monotonic - worth testing as a filter)" if d["monotonic"]
               else "  (non-monotonic - probably noise)"))
    return out


# ---------------------------------------------------------------- edge decay
@dataclass
class DecayReport:
    n_recent: int
    recent_expectancy: float
    baseline_expectancy: float
    baseline_p05: float
    baseline_p50: float
    alert: bool
    action: str
    message: str


def edge_decay_check(recent_trades: Sequence, baseline_trades: Sequence,
                     window: int = 60, alert_pct: float = 5.0,
                     n_boot: int = 2000) -> DecayReport:
    """Is live performance still inside what the backtest predicted?

    The comparison is against the SAMPLING DISTRIBUTION of the baseline, not
    against its point estimate. A 60-trade run averaging below the backtest mean
    is completely normal; a 60-trade run below the 5th percentile of 60-trade
    samples drawn from the baseline is not.
    """
    def rs(ts):
        return [t.r_multiple if hasattr(t, "r_multiple") else t.get("r_multiple")
                for t in ts]

    recent = [r for r in rs(recent_trades)[-window:] if isinstance(r, (int, float))
              and not is_na(r)]
    base = [r for r in rs(baseline_trades) if isinstance(r, (int, float)) and not is_na(r)]

    if len(recent) < 10 or len(base) < 30:
        return DecayReport(len(recent), mean(recent) if recent else NA,
                           mean(base) if base else NA, NA, NA, False, "none",
                           f"not enough trades yet (live {len(recent)}, baseline {len(base)})")

    # Bootstrap: what does a window-sized sample from the baseline look like?
    import random
    rng = random.Random(17)
    n = len(recent)
    sims = []
    for _ in range(n_boot):
        s = 0.0
        for _ in range(n):
            s += base[rng.randrange(len(base))]
        sims.append(s / n)
    p05 = percentile(sims, alert_pct)
    p50 = percentile(sims, 50)
    live = mean(recent)

    alert = live < p05
    if alert:
        action = "halve_risk"
        msg = (f"Live expectancy {live:+.3f}R over the last {n} trades is below the "
               f"{alert_pct:.0f}th percentile ({p05:+.3f}R) of what the baseline "
               f"predicts for a {n}-trade window. Cut risk per trade by half and "
               f"re-run the walk-forward before adding size back.")
    elif live < p50:
        action = "watch"
        msg = (f"Live expectancy {live:+.3f}R is below the baseline median "
               f"({p50:+.3f}R) but inside normal variation. No action.")
    else:
        action = "none"
        msg = (f"Live expectancy {live:+.3f}R is at or above the baseline median "
               f"({p50:+.3f}R). Performing as designed.")
    return DecayReport(n, live, mean(base), p05, p50, alert, action, msg)
