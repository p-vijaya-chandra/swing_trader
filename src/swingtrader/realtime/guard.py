"""Pre-trade risk checks that sit between the strategy and the broker.

The strategy's own sizing rules already bound risk. These checks exist for the
day the strategy, the data or this code is wrong - a corrupted price, a sizing
bug, a plan built from stale data, a loop that re-fires. Each one is cheap and
each one is a hard stop rather than a warning.

Going live takes two independent switches, so no single typo can do it:
    config   execution.mode = "live"
    env      SWING_LIVE_CONFIRM=yes
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Dict, Optional, Set

from ..broker.base import BUY, LIMIT, SL, OrderRequest, Quote


@dataclass
class GuardDecision:
    ok: bool
    reason: str = ""


class RiskGuard:
    def __init__(self, cfg, state_dir: str, clock, universe: Optional[Set[str]] = None):
        e = cfg.get("execution", {}) or {}
        self.cfg = cfg
        self.clock = clock
        self.state_dir = state_dir
        self.universe = set(universe or ())
        cap = float(cfg.get("capital", 100000.0))
        self.capital = cap
        self.max_order_value = float(e.get("max_order_value_frac", 0.30)) * cap
        self.max_orders_per_day = int(e.get("max_orders_per_day", 20))
        self.max_daily_buy_value = float(e.get("max_daily_buy_frac", 0.80)) * cap
        self.max_price_dev = float(e.get("max_price_deviation", 0.05))
        self.max_quote_age_s = float(e.get("max_quote_age_s", 180))
        self.canary_qty = int(e.get("canary_qty", 0) or 0)
        self.mode = str(e.get("mode", "paper"))
        self.counter_path = os.path.join(state_dir, "guard_counters.json")
        self.kill_path = os.path.join(state_dir, "KILL")

    # ------------------------------------------------------------- switches
    def kill_switch(self) -> bool:
        """`touch state/KILL` stops all new orders within one poll. Protective
        stops stay in place - freezing must never mean leaving positions naked."""
        return os.path.exists(self.kill_path)

    def live_allowed(self) -> GuardDecision:
        if self.mode != "live":
            return GuardDecision(False, f"execution.mode is '{self.mode}'")
        if os.environ.get("SWING_LIVE_CONFIRM", "").strip().lower() != "yes":
            return GuardDecision(False, "SWING_LIVE_CONFIRM=yes is not set in the environment")
        return GuardDecision(True, "live trading enabled by config AND environment")

    def apply_canary(self, qty: int) -> int:
        """Cap every order at `canary_qty` shares - real orders, trivial size -
        for the first live week, while the plumbing proves itself."""
        return min(qty, self.canary_qty) if self.canary_qty > 0 else qty

    # ------------------------------------------------------------- counters
    def _counters(self) -> Dict[str, float]:
        today = self.clock.today().isoformat()
        try:
            with open(self.counter_path, "r", encoding="utf-8") as fh:
                c = json.load(fh)
        except (OSError, ValueError):
            c = {}
        if c.get("day") != today:
            c = {"day": today, "orders": 0, "buy_value": 0.0}
        return c

    def record(self, req: OrderRequest, value: float) -> None:
        c = self._counters()
        c["orders"] += 1
        if req.side == BUY:
            c["buy_value"] += value
        os.makedirs(self.state_dir, exist_ok=True)
        with open(self.counter_path, "w", encoding="utf-8") as fh:
            json.dump(c, fh)

    # ---------------------------------------------------------------- check
    def check(self, req: OrderRequest, quote: Optional[Quote],
              cash_available: Optional[float] = None,
              protective: bool = False) -> GuardDecision:
        """protective=True is for exits that REDUCE risk because a stop failed.
        Those bypass the kill switch and the daily caps - a freeze, or a bug
        that burned through the order budget, must never be what leaves a
        position naked - but still get every sanity check on the order itself."""
        if self.kill_switch() and not protective:
            return GuardDecision(False, "kill switch is on (state/KILL exists)")
        if req.qty <= 0 or int(req.qty) != req.qty:
            return GuardDecision(False, f"bad quantity {req.qty}")
        if self.universe and req.symbol not in self.universe:
            return GuardDecision(False, f"{req.symbol} is not in the trading universe")
        if quote is None or not quote.valid():
            return GuardDecision(False, f"no valid quote for {req.symbol}")
        age = self.clock.epoch() - (quote.ts or 0)
        if quote.ts and age > self.max_quote_age_s:
            return GuardDecision(False, f"quote for {req.symbol} is {age:.0f}s old")
        ref_px = req.price if req.order_type in (LIMIT, SL) and req.price else quote.ltp
        if req.order_type in (LIMIT, SL) and req.price:
            dev = abs(req.price / quote.ltp - 1.0)
            if dev > self.max_price_dev:
                return GuardDecision(False, f"limit {req.price:.2f} is {100*dev:.1f}% from "
                                            f"LTP {quote.ltp:.2f} (max {100*self.max_price_dev:.0f}%)")
        value = ref_px * req.qty
        if req.side == BUY and value > self.max_order_value:
            return GuardDecision(False, f"order value {value:,.0f} exceeds the per-order cap "
                                        f"{self.max_order_value:,.0f}")
        c = self._counters()
        if protective:
            return GuardDecision(True, "protective exit")
        if c["orders"] >= self.max_orders_per_day:
            return GuardDecision(False, f"{int(c['orders'])} orders already today "
                                        f"(cap {self.max_orders_per_day})")
        if req.side == BUY and c["buy_value"] + value > self.max_daily_buy_value:
            return GuardDecision(False, f"daily buy value would reach "
                                        f"{c['buy_value'] + value:,.0f} (cap "
                                        f"{self.max_daily_buy_value:,.0f})")
        if req.side == BUY and cash_available is not None and value > cash_available:
            return GuardDecision(False, f"order value {value:,.0f} exceeds available cash "
                                        f"{cash_available:,.0f}")
        return GuardDecision(True, "ok")
