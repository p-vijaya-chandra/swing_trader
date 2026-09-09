"""Market data providers.

The engine never imports these; only `swing fetch` does. That keeps the
backtest reproducible from the CSV cache and means a vendor outage cannot
change yesterday's results.
"""
from __future__ import annotations

import time
from typing import Dict, Iterable, Optional

from .models import Series


class ProviderError(RuntimeError):
    pass


class YFinanceProvider:
    """Yahoo Finance via yfinance. Free, and adequate for daily EOD swing work.

    Things you must know before trusting it:
      * Yahoo's NSE history is split/bonus adjusted, but not always promptly and
        not always correctly. Run `swing validate-data` after every fetch - an
        unadjusted bonus reads to the engine as a -50% day and stops out every
        holder.
      * auto_adjust=False, so OHLC are traded prices. Dividend adjustment is
        deliberately NOT applied: stops are placed on prices that existed, not
        on a total-return series.
      * Rate limits are real and unannounced. Symbols are fetched in batches
        with backoff, and anything that fails is retried individually.
      * NSE renames tickers (ZOMATO -> ETERNAL). A symbol returning nothing is
        usually a rename, not an outage.
    """

    def __init__(self, suffix: str = ".NS", index_map: Optional[Dict[str, str]] = None,
                 pause: float = 0.8, batch_size: int = 12, max_retries: int = 3):
        self.suffix = suffix
        self.index_map = index_map or {}
        self.pause = pause
        self.batch_size = batch_size
        self.max_retries = max_retries

    def _yahoo_symbol(self, symbol: str) -> str:
        if symbol in self.index_map:
            return self.index_map[symbol]
        if symbol.startswith("^"):
            return symbol
        return f"{symbol}{self.suffix}"

    @staticmethod
    def _rows_from_frame(df) -> list:
        """Pull OHLCV out of a yfinance frame, tolerating column-case drift."""
        cols = {str(c).lower(): c for c in df.columns}
        need = ("open", "high", "low", "close")
        if any(c not in cols for c in need):
            return []
        vcol = cols.get("volume")
        rows = []
        for ts, r in df.iterrows():
            o, h, l, c = (r[cols["open"]], r[cols["high"]],
                          r[cols["low"]], r[cols["close"]])
            if any(x != x for x in (o, h, l, c)):      # NaN row: symbol not traded
                continue
            rows.append([str(ts)[:10], o, h, l, c,
                         (r[vcol] if vcol and r[vcol] == r[vcol] else 0.0)])
        return rows

    def _fetch_one(self, yf, sym: str, start: str, end: str) -> Optional[Series]:
        ysym = self._yahoo_symbol(sym)
        try:
            df = yf.Ticker(ysym).history(start=start, end=end or None, interval="1d",
                                         auto_adjust=False, actions=False)
        except Exception as exc:                      # noqa: BLE001 - vendor raises anything
            print(f"    ! {sym} ({ysym}): {exc}")
            return None
        if df is None or len(df) == 0:
            return None
        ser = Series.from_rows(sym, self._rows_from_frame(df))
        return ser if len(ser) else None

    def _fetch_batch(self, yf, syms: list, start: str, end: str) -> Dict[str, Series]:
        """One multi-ticker request. Far kinder to the rate limiter than N requests."""
        mapping = {self._yahoo_symbol(s): s for s in syms}
        try:
            df = yf.download(list(mapping), start=start, end=end or None,
                             interval="1d", auto_adjust=False, actions=False,
                             group_by="ticker", progress=False, threads=False)
        except Exception as exc:                      # noqa: BLE001
            print(f"    ! batch failed ({exc}); falling back to individual requests")
            return {}
        if df is None or len(df) == 0:
            return {}
        out: Dict[str, Series] = {}
        for ysym, sym in mapping.items():
            try:
                sub = df[ysym] if len(mapping) > 1 else df
            except (KeyError, TypeError):
                continue
            ser = Series.from_rows(sym, self._rows_from_frame(sub))
            if len(ser):
                out[sym] = ser
        return out

    def fetch(self, symbols: Iterable[str], start: str, end: str = "") -> Dict[str, Series]:
        try:
            import yfinance as yf  # noqa
        except ImportError as exc:
            raise ProviderError(
                "yfinance is not installed. Run: pip install -r requirements.txt"
            ) from exc

        syms = list(symbols)
        out: Dict[str, Series] = {}
        pending = list(syms)

        for attempt in range(1, self.max_retries + 1):
            if not pending:
                break
            if attempt > 1:
                wait = self.pause * (2 ** (attempt - 1))
                print(f"  retry {attempt}/{self.max_retries} for {len(pending)} "
                      f"symbol(s) after {wait:.0f}s")
                time.sleep(wait)

            still: list = []
            # Batch first; anything the batch misses is retried one at a time,
            # because a single bad ticker can poison a whole multi-ticker request.
            for k in range(0, len(pending), self.batch_size):
                chunk = pending[k:k + self.batch_size]
                got = self._fetch_batch(yf, chunk, start, end) if len(chunk) > 1 else {}
                for sym in chunk:
                    ser = got.get(sym)
                    if ser is None:
                        ser = self._fetch_one(yf, sym, start, end)
                    if ser is not None:
                        out[sym] = ser
                        print(f"  + {sym:14s} {len(ser):5d} bars  "
                              f"{ser.dates[0]}..{ser.dates[-1]}")
                    else:
                        still.append(sym)
                    time.sleep(self.pause * 0.2)
                if self.pause and k + self.batch_size < len(pending):
                    time.sleep(self.pause)
            pending = still

        for sym in pending:
            print(f"  ! {sym}: no data after {self.max_retries} attempts")
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
