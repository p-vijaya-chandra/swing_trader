"""Data quality checks for real vendor history.

Free EOD data is good enough to trade on and bad enough to ruin a backtest, and
the failure mode is always the same: it looks fine. Every defect below produces
a *plausible* equity curve rather than an error, so nothing downstream will tell
you something went wrong.

The four that actually matter for this system:

  UNADJUSTED SPLIT OR BONUS. A 1:2 bonus halves the price overnight. To the
  engine that is a -50% day. Measured on this system: any position held through
  it gaps straight through its stop, and the ATR computed from that bar is
  inflated 4.0x immediately, still 1.65x twenty sessions later, and does not
  decay back until roughly sixty. Because risk-based sizing divides by ATR, the
  next position opened in that name is sized 0.32x what it should be - the error
  propagates well past the bad bar. Indian large caps issue bonuses regularly,
  so this is not a rare edge case. Detected by comparing the move against the
  index on the same day: a stock that fell 50% while the index was flat did not
  fall 50%.

  MISSING SESSIONS. A symbol quietly skipping days shifts every lookback window
  behind it. A "20-day breakout" computed over 20 rows spanning 32 calendar days
  is not the indicator you think it is.

  STALE PRICES. Vendors forward-fill suspended scrips. Frozen bars have zero
  true range, so ATR decays - measured at 0.71x after twenty frozen sessions,
  which sizes the next position 1.42x too large. The system takes an oversized
  position in the least tradeable name on the board, which is exactly backwards.

  SHORT OR STALE HISTORY. A symbol with 200 bars cannot produce a 200 EMA, and
  a cache that stopped updating three weeks ago will happily generate today's
  orders from three-week-old prices.

Run `swing validate-data` after every fetch. It exits non-zero on anything that
would corrupt results, so it can gate a cron job.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

ERROR, WARN, INFO = "error", "warn", "info"

# Ratios a genuine corporate action lands on. Indian bonus issues are commonly
# 1:1, 1:2, 3:5; splits run 1:2, 1:5, 1:10.
_SPLIT_RATIOS = [1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 6, 1 / 8, 1 / 10, 1 / 20,
                 2 / 3, 2 / 5, 3 / 5, 3 / 4, 5 / 8,
                 2.0, 3.0, 4.0, 5.0, 10.0, 20.0, 3 / 2, 5 / 2, 5 / 3, 4 / 3]


@dataclass
class Issue:
    symbol: str
    kind: str
    severity: str
    detail: str
    dates: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        where = f" [{', '.join(self.dates[:4])}{' ...' if len(self.dates) > 4 else ''}]" \
            if self.dates else ""
        return f"{self.severity.upper():5s} {self.symbol:14s} {self.kind:22s} {self.detail}{where}"


@dataclass
class QualityReport:
    issues: List[Issue] = field(default_factory=list)
    n_symbols: int = 0
    n_bars: int = 0
    first_date: str = ""
    last_date: str = ""
    index_present: bool = False

    def add(self, *issues: Issue) -> None:
        self.issues.extend(issues)

    @property
    def errors(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == ERROR]

    @property
    def warnings(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == WARN]

    def bad_symbols(self) -> List[str]:
        """Symbols with at least one error - candidates to drop from the universe."""
        return sorted({i.symbol for i in self.errors})

    def by_kind(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for i in self.issues:
            out[i.kind] = out.get(i.kind, 0) + 1
        return out


def _near_split_ratio(r: float, tol: float = 0.04) -> Optional[float]:
    for t in _SPLIT_RATIOS:
        if abs(r - t) / t < tol:
            return t
    return None


def check_symbol(symbol: str, series, index_returns: Dict[str, float],
                 index_dates: Sequence[str], min_bars: int = 260,
                 max_last_date: str = "", stale_run: int = 5,
                 jump_pct: float = 0.35, detect_splits: bool = True) -> List[Issue]:
    out: List[Issue] = []
    n = len(series)
    c = series.close

    if n == 0:
        return [Issue(symbol, "empty", ERROR, "no bars at all")]

    if n < min_bars:
        out.append(Issue(symbol, "short_history", ERROR,
                         f"{n} bars, need {min_bars} for the slow moving average"))

    # ---- stale cache -------------------------------------------------------
    if max_last_date and series.dates[-1] < max_last_date:
        idx_after = [d for d in index_dates if d > series.dates[-1]]
        if len(idx_after) > 5:
            out.append(Issue(symbol, "stale_cache", ERROR,
                             f"last bar {series.dates[-1]}, {len(idx_after)} sessions "
                             f"behind the index (through {max_last_date})"))

    # ---- duplicate / unsorted dates ---------------------------------------
    if len(set(series.dates)) != n:
        out.append(Issue(symbol, "duplicate_dates", ERROR,
                         f"{n - len(set(series.dates))} duplicated dates"))
    if any(series.dates[i] >= series.dates[i + 1] for i in range(n - 1)):
        out.append(Issue(symbol, "unsorted_dates", ERROR, "dates are not ascending"))

    # ---- missing sessions vs the index calendar ---------------------------
    if index_dates:
        lo, hi = series.dates[0], series.dates[-1]
        expected = [d for d in index_dates if lo <= d <= hi]
        have = set(series.dates)
        missing = [d for d in expected if d not in have]
        if expected and len(missing) > max(10, 0.02 * len(expected)):
            out.append(Issue(symbol, "missing_sessions", ERROR,
                             f"{len(missing)} of {len(expected)} index sessions absent "
                             f"({100*len(missing)/len(expected):.1f}%)", missing[:6]))
        elif missing:
            out.append(Issue(symbol, "missing_sessions", WARN,
                             f"{len(missing)} sessions absent", missing[:6]))

    # ---- unadjusted corporate actions and bad prints ----------------------
    # Split detection is off for the index itself: there is no benchmark to
    # compare it against, so a genuine market-wide crash lands on a ratio like
    # 1/2 and gets reported as bad data. Indices are not split-adjusted anyway.
    splits, prints_ = [], []
    for i in range(1, n) if detect_splits else ():
        if c[i - 1] <= 0:
            continue
        r = c[i] / c[i - 1]
        if 1 - jump_pct <= r <= 1 + jump_pct:
            continue
        d = series.dates[i]
        idx_r = index_returns.get(d)
        # If the index barely moved, a 40% single-day move in a Nifty 100 name is
        # a corporate action or a bad print - not a market event.
        idx_quiet = idx_r is not None and abs(idx_r) < 0.05
        target = _near_split_ratio(r)
        if target is not None and (idx_quiet or idx_r is None):
            splits.append((d, r, target))
        else:
            prints_.append((d, r))

    if splits:
        desc = ", ".join(f"{d} x{r:.3f}~{t:.3f}" for d, r, t in splits[:3])
        out.append(Issue(
            symbol, "unadjusted_split", ERROR,
            f"{len(splits)} overnight jump(s) landing on a split/bonus ratio while the "
            f"index was quiet: {desc}. The engine reads this as a crash: holders gap "
            f"through their stops, and ATR stays inflated (~4x immediately, ~1.65x "
            f"20 sessions later) so positions sized off it are wrong for weeks.",
            [d for d, _, _ in splits[:6]]))
    if prints_:
        out.append(Issue(
            symbol, "extreme_move", WARN,
            f"{len(prints_)} day(s) moving more than {100*jump_pct:.0f}% that do not "
            f"match a split ratio - verify against the exchange before trusting them",
            [d for d, _ in prints_[:6]]))

    # ---- frozen bars -------------------------------------------------------
    run, runs = 1, []
    for i in range(1, n):
        same = (c[i] == c[i - 1] and series.high[i] == series.high[i - 1]
                and series.low[i] == series.low[i - 1])
        if same:
            run += 1
        else:
            if run >= stale_run:
                runs.append((series.dates[i - 1], run))
            run = 1
    if run >= stale_run:
        runs.append((series.dates[-1], run))
    if runs:
        worst = max(r for _, r in runs)
        out.append(Issue(symbol, "frozen_bars", ERROR if worst >= 10 else WARN,
                         f"{len(runs)} run(s) of identical OHLC, longest {worst} sessions. "
                         f"True range is zero across these, so ATR decays (~0.71x after "
                         f"20 frozen bars) and sizes the next position ~1.4x too large.",
                         [d for d, _ in runs[:6]]))

    # ---- volume ------------------------------------------------------------
    zero_vol = [series.dates[i] for i in range(n) if series.volume[i] <= 0]
    if len(zero_vol) > max(5, 0.02 * n):
        out.append(Issue(symbol, "missing_volume", ERROR,
                         f"{len(zero_vol)} of {n} bars have no volume - the liquidity "
                         f"screen cannot work on this symbol", zero_vol[:6]))
    elif zero_vol:
        out.append(Issue(symbol, "missing_volume", WARN,
                         f"{len(zero_vol)} bars with no volume", zero_vol[:6]))

    # ---- implausible price level ------------------------------------------
    if min(c) <= 0:
        out.append(Issue(symbol, "non_positive_price", ERROR, "close <= 0 present"))

    return out


def index_return_map(index_series) -> Dict[str, float]:
    if index_series is None or len(index_series) < 2:
        return {}
    out: Dict[str, float] = {}
    c = index_series.close
    for i in range(1, len(index_series)):
        if c[i - 1] > 0:
            out[index_series.dates[i]] = c[i] / c[i - 1] - 1.0
    return out


def check_dataset(series_map: Dict[str, object], index_series, min_bars: int = 260,
                  universe: Optional[Sequence[str]] = None) -> QualityReport:
    rep = QualityReport()
    rep.n_symbols = len(series_map)
    rep.index_present = index_series is not None and len(index_series) > 0

    index_dates = list(index_series.dates) if rep.index_present else []
    idx_ret = index_return_map(index_series)
    last_dates = [s.dates[-1] for s in series_map.values() if len(s)]
    max_last = max(last_dates) if last_dates else ""
    if index_dates:
        max_last = max(max_last, index_dates[-1])

    if not rep.index_present:
        rep.add(Issue("(index)", "index_missing", ERROR,
                      "no index series cached. The regime filter is the main drawdown "
                      "control on a long-only book and it will be DISABLED without it."))
    else:
        rep.add(*check_symbol("(index)", index_series, {}, [], min_bars=min_bars,
                              max_last_date="", detect_splits=False))

    for sym in sorted(series_map):
        s = series_map[sym]
        rep.n_bars += len(s)
        rep.add(*check_symbol(sym, s, idx_ret, index_dates, min_bars=min_bars,
                              max_last_date=max_last))

    if universe:
        missing = [s for s in universe if s not in series_map]
        if missing:
            sev = ERROR if len(missing) > 0.25 * len(universe) else WARN
            rep.add(Issue("(universe)", "symbols_not_cached", sev,
                          f"{len(missing)} of {len(universe)} universe symbols have no "
                          f"data: {', '.join(missing[:8])}"
                          f"{' ...' if len(missing) > 8 else ''}"))

    all_dates = [d for s in series_map.values() for d in (s.dates[:1] + s.dates[-1:])]
    if all_dates:
        rep.first_date, rep.last_date = min(all_dates), max(all_dates)
    return rep
