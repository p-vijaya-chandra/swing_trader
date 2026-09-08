"""Transaction cost model for NSE cash delivery via Groww.

Why this file gets more care than it looks like it deserves:

At Rs 1,00,000 of capital with 8 positions, an average position is ~Rs 12,500.
The DP charge on a delivery sell (a flat ~Rs 20 + GST, per scrip, per day) is
*fixed*, so it costs 0.16% of that position all by itself. Add STT on both legs,
brokerage, and slippage and a round trip runs roughly 0.7-0.9%. A system that
turns the book over twice a month is paying something like 1.5-2% a month in
frictions before it makes a single rupee.

That is the difference between a strategy that looks good in a naive backtest
and one that survives a real contract note, and it is the main reason the
default configuration holds positions for weeks rather than days.

RATES ARE A SNAPSHOT AND MUST BE VERIFIED. Broker tariffs, STT and stamp duty
all change. Check your own contract note and update config/base.jsonc.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from .config import Config


@dataclass
class CostBreakdown:
    brokerage: float = 0.0
    stt: float = 0.0
    exchange: float = 0.0
    sebi: float = 0.0
    stamp: float = 0.0
    gst: float = 0.0
    dp: float = 0.0
    slippage: float = 0.0

    @property
    def statutory(self) -> float:
        return self.stt + self.exchange + self.sebi + self.stamp + self.gst

    @property
    def total(self) -> float:
        return (self.brokerage + self.stt + self.exchange + self.sebi
                + self.stamp + self.gst + self.dp + self.slippage)

    def as_dict(self) -> Dict[str, float]:
        return {"brokerage": self.brokerage, "stt": self.stt, "exchange": self.exchange,
                "sebi": self.sebi, "stamp": self.stamp, "gst": self.gst,
                "dp": self.dp, "slippage": self.slippage, "total": self.total}


class CostModel:
    def __init__(self, cfg: Config):
        c = cfg.get("costs", {})
        self.brokerage_pct = float(c.get("brokerage_pct", 0.001))
        self.brokerage_cap = float(c.get("brokerage_cap", 20.0))
        self.brokerage_min = float(c.get("brokerage_min", 5.0))
        self.stt_buy = float(c.get("stt_buy_pct", 0.001))
        self.stt_sell = float(c.get("stt_sell_pct", 0.001))
        self.exchange_pct = float(c.get("exchange_txn_pct", 0.0000297))
        self.sebi_pct = float(c.get("sebi_pct", 0.000001))
        self.stamp_pct = float(c.get("stamp_duty_buy_pct", 0.00015))
        self.gst_pct = float(c.get("gst_pct", 0.18))
        self.dp_sell = float(c.get("dp_charge_sell", 20.0))
        self.ipft_pct = float(c.get("ipft_pct", 0.000001))
        self.slippage_bps = float(c.get("slippage_bps", 12.0))

    def brokerage(self, turnover: float) -> float:
        """Groww delivery: 0.1% of turnover or Rs 20, whichever is LOWER."""
        if turnover <= 0:
            return 0.0
        b = min(turnover * self.brokerage_pct, self.brokerage_cap)
        return max(b, min(self.brokerage_min, turnover * self.brokerage_pct))

    def fill_price(self, ref_price: float, side: str) -> float:
        """Apply slippage against you on both sides. Never in your favour."""
        s = self.slippage_bps / 10000.0
        return ref_price * (1.0 + s) if side == "BUY" else ref_price * (1.0 - s)

    def charges(self, price: float, qty: int, side: str,
                ref_price: float | None = None) -> CostBreakdown:
        """Charges for one leg. `price` is the actual fill, `ref_price` the
        pre-slippage reference used to book slippage as an explicit cost."""
        turnover = price * qty
        cb = CostBreakdown()
        if qty <= 0 or turnover <= 0:
            return cb
        cb.brokerage = self.brokerage(turnover)
        cb.exchange = turnover * self.exchange_pct
        cb.sebi = turnover * (self.sebi_pct + self.ipft_pct)
        if side == "BUY":
            cb.stt = turnover * self.stt_buy
            cb.stamp = turnover * self.stamp_pct
        else:
            cb.stt = turnover * self.stt_sell
            cb.dp = self.dp_sell * (1.0 + self.gst_pct)
        cb.gst = (cb.brokerage + cb.exchange + cb.sebi) * self.gst_pct
        if ref_price is not None:
            cb.slippage = abs(price - ref_price) * qty
        return cb

    def round_trip_pct(self, position_value: float) -> float:
        """All-in round-trip cost as a fraction of position value.

        Use this to sanity check position sizing: if a round trip costs 0.9% and
        your average winner is +4%, frictions are eating a quarter of the edge.
        """
        if position_value <= 0:
            return 0.0
        px, qty = 100.0, max(1, int(position_value / 100.0))
        buy = self.charges(px, qty, "BUY")
        sell = self.charges(px, qty, "SELL")
        slip = 2.0 * (self.slippage_bps / 10000.0) * position_value
        return (buy.total + sell.total + slip) / position_value
