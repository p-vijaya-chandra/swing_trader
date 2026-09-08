"""Market data providers.

The engine never imports these; only `swing fetch` does. That keeps the
backtest reproducible from the CSV cache and means a vendor outage cannot
change yesterday's results.
"""
from __future__ import annotations

import time
from typing import Dict, Iterable, List, Optional

from .models import Series


class ProviderError(RuntimeError):
    pass


class YFinanceProvider:
    """Yahoo Finance via yfinance. Free, adequate for daily EOD swing work.

    Caveats you must know before trusting it:
      * Yahoo's NSE history is split/bonus adjusted but occasionally carries bad
        prints; `Series.from_rows` drops incoherent bars.
      * Use auto_adjust=False and take raw OHLC, then rely on Yahoo's own split
        handling. Dividend adjustment is deliberately NOT applied: swing stops
        are placed on traded prices, not total-return prices.
      * Rate limits are real. Batch, and cache.
    """

    def __init__(self, suffix: str = ".NS", index_map: Optional[Dict[str, str]] = None,
                 pause: float = 0.6):
        self.suffix = suffix
        self.index_map = index_map or {}
        self.pause = pause

    def _yahoo_symbol(self, symbol: str) -> str:
        if symbol in self.index_map:
            return self.index_map[symbol]
        if symbol.startswith("^"):
            return symbol
        return f"{symbol}{self.suffix}"

    def fetch(self, symbols: Iterable[str], start: str, end: str = "") -> Dict[str, Series]:
        try:
            import yfinance as yf  # noqa
        except ImportError as exc:
            raise ProviderError(
                "yfinance is not installed. Run: pip install -r requirements.txt"
            ) from exc

        out: Dict[str, Series] = {}
        syms = list(symbols)
        for i, sym in enumerate(syms):
            ysym = self._yahoo_symbol(sym)
            try:
                t = yf.Ticker(ysym)
                df = t.history(start=start, end=end or None, interval="1d",
                               auto_adjust=False, actions=False)
            except Exception as exc:                      # noqa: BLE001 - vendor can raise anything
                print(f"  ! {sym} ({ysym}): {exc}")
                continue
            if df is None or len(df) == 0:
                print(f"  ! {sym} ({ysym}): no data returned")
                continue
            rows = []
            for ts, r in df.iterrows():
                rows.append([str(ts)[:10], r["Open"], r["High"], r["Low"],
                             r["Close"], r.get("Volume", 0.0)])
            ser = Series.from_rows(sym, rows)
            if len(ser):
                out[sym] = ser
                print(f"  + {sym}: {len(ser)} bars {ser.dates[0]}..{ser.dates[-1]}")
            if self.pause and i < len(syms) - 1:
                time.sleep(self.pause)
        return out


class CSVDirProvider:
    """Import bars you already have (broker export, paid vendor, bhavcopy dump).

    Expects <dir>/<SYMBOL>.csv with a header containing date/open/high/low/close
    /volume in any order and any capitalisation.
    """

    def __init__(self, directory: str):
        self.directory = directory

    def fetch(self, symbols: Iterable[str], start: str, end: str = "") -> Dict[str, Series]:
        import csv
        import os
        out: Dict[str, Series] = {}
        for sym in symbols:
            p = os.path.join(self.directory, f"{sym}.csv")
            if not os.path.exists(p):
                continue
            with open(p, "r", encoding="utf-8") as fh:
                rd = csv.DictReader(fh)
                cols = {c.lower().strip(): c for c in (rd.fieldnames or [])}
                need = ["date", "open", "high", "low", "close"]
                if any(c not in cols for c in need):
                    print(f"  ! {sym}: missing columns, found {rd.fieldnames}")
                    continue
                rows = []
                for r in rd:
                    d = str(r[cols["date"]])[:10]
                    if d < start or (end and d > end):
                        continue
                    rows.append([d, r[cols["open"]], r[cols["high"]], r[cols["low"]],
                                 r[cols["close"]], r.get(cols.get("volume", ""), 0) or 0])
            ser = Series.from_rows(sym, rows)
            if len(ser):
                out[sym] = ser
        return out


def get_provider(name: str, cfg) -> object:
    name = (name or "").lower()
    if name in ("yfinance", "yahoo"):
        return YFinanceProvider(suffix=cfg.get("data.suffix", ".NS"),
                                index_map=cfg.get("data.index_symbol_map", {}))
    if name.startswith("csvdir:"):
        return CSVDirProvider(name.split(":", 1)[1])
    raise ProviderError(f"unknown provider '{name}' (use 'yfinance' or 'csvdir:/path')")
