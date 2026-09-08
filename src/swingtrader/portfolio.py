"""Positions, position sizing and risk constraints.

Sizing here is risk-first, not capital-first: you decide what a losing trade
costs (a fixed fraction of equity), and the stop distance then determines the
quantity. A volatile name and a quiet name lose the same rupees when they are
wrong, which is what makes the R-multiple statistics in the learning loop
comparable across names and across time.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import Config
from .util import NA, is_na


@dataclass
class Position:
    symbol: str
    sector: str
    qty: int
    entry_price: float
    entry_date: str
    setup: str
    initial_stop: float
    stop: float
    risk_per_share: float
    initial_risk_value: float
    highest_high: float
    lowest_low: float
    bars_held: int = 0
    scaled_out: bool = False
    realised_pnl: float = 0.0
    costs_paid: float = 0.0
    entry_rank: int = 0
    entry_score: float = 0.0
    regime_at_entry: str = ""
    entry_snapshot: Dict[str, float] = field(default_factory=dict)
    below_ema_closes: int = 0
    original_qty: int = 0

    @property
    def cost_basis(self) -> float:
        return self.qty * self.entry_price

    def r_multiple(self, price: float) -> float:
        """Unrealised P&L in units of the ORIGINAL risk taken."""
        if self.risk_per_share <= 0:
            return NA
        return (price - self.entry_price) / self.risk_per_share

    def open_risk(self, price: float) -> float:
        """Rupees still at risk: distance to the current stop, floored at zero
        once the stop is above entry (a locked-in winner risks nothing more)."""
        return max(0.0, (price - self.stop)) * self.qty if self.stop < price else 0.0

    def risk_to_stop(self) -> float:
        return max(0.0, self.entry_price - self.stop) * self.qty


@dataclass
class Trade:
    """A completed round trip. This record is the raw material the learning
    loop consumes, so it stores the feature snapshot taken at entry."""
    symbol: str
    sector: str
    setup: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    qty: int
    gross_pnl: float
    costs: float
    net_pnl: float
    r_multiple: float
    bars_held: int
    exit_reason: str
    mae_r: float
    mfe_r: float
    regime_at_entry: str
    entry_rank: int
    entry_score: float
    entry_snapshot: Dict[str, float] = field(default_factory=dict)

    def as_row(self) -> Dict[str, object]:
        d = {k: getattr(self, k) for k in (
            "symbol", "sector", "setup", "entry_date", "exit_date", "entry_price",
            "exit_price", "qty", "gross_pnl", "costs", "net_pnl", "r_multiple",
            "bars_held", "exit_reason", "mae_r", "mfe_r", "regime_at_entry",
            "entry_rank", "entry_score")}
        for k, v in self.entry_snapshot.items():
            d[f"f_{k}"] = v
        return d


class RiskManager:
    """Turns a signal into a quantity, or refuses it."""

    def __init__(self, cfg: Config):
        r = cfg.get("risk", {})
        self.risk_per_trade = float(r.get("risk_per_trade", 0.01))
        self.max_positions = int(r.get("max_positions", 8))
        self.max_position_frac = float(r.get("max_position_frac", 0.22))
        self.max_heat = float(r.get("max_portfolio_heat", 0.06))
        self.max_sector = int(r.get("max_sector_positions", 3))
        self.min_position_value = float(r.get("min_position_value", 4000.0))
        self.derisk_dd = float(r.get("derisk_on_drawdown", 0.10))
        self.derisk_factor = float(r.get("derisk_factor", 0.5))
        self.halt_dd = float(r.get("halt_on_drawdown", 0.20))
        self.resume_dd = float(r.get("resume_on_drawdown", 0.10))
        self.halt_cooldown = int(r.get("halt_cooldown_days", 40))
        self.post_halt_factor = float(r.get("post_halt_risk_factor", 0.5))
        self.post_halt_days = int(r.get("post_halt_probation_days", 60))

    def probation_scale(self, days_since_resume: int) -> float:
        """Restart small after a halt. Getting back to full size immediately is
        how a system that just lost 20% goes on to lose 30%."""
        if self.post_halt_days <= 0 or days_since_resume < 0:
            return 1.0
        return self.post_halt_factor if days_since_resume < self.post_halt_days else 1.0

    def risk_fraction(self, drawdown: float, learn_scale: float = 1.0) -> float:
        """Risk per trade, cut when the equity curve is in trouble.

        Halving risk inside a drawdown is not superstition: it converts the
        deepest part of the curve from geometric decay into a slow bleed, and
        it is the cheapest drawdown control available on a long-only book.
        """
        f = self.risk_per_trade * learn_scale
        if self.derisk_dd > 0 and drawdown <= -abs(self.derisk_dd):
            f *= self.derisk_factor
        return f

    def halt_state(self, drawdown: float, currently_halted: bool,
                   days_halted: int = 0, regime_risk_on: bool = False) -> bool:
        """Circuit breaker with a REACHABLE resume condition.

        A bare threshold ("stop trading below -20%") is a one-way trapdoor, and
        so is a pure drawdown-recovery resume. Once you stop taking entries the
        equity curve flatlines, the all-time peak never updates, the drawdown
        never recovers, and the system is switched off permanently and silently.
        A backtest will happily report this as "low volatility" rather than
        "dead since 2020".

        So there are two ways back in, and the second one does not depend on
        making money while forbidden from trading:
          1. the drawdown recovers past -resume_dd (open positions can do this), or
          2. the cooldown elapses AND the market regime is risk-on again.

        This mirrors what a disciplined discretionary trader actually does:
        stop, wait out the damage, and restart small when conditions turn.
        """
        if self.halt_dd <= 0:
            return False
        if not currently_halted:
            return drawdown <= -abs(self.halt_dd)
        if drawdown > -abs(self.resume_dd):
            return False
        if self.halt_cooldown > 0 and days_halted >= self.halt_cooldown and regime_risk_on:
            return False
        return True

    def halted(self, drawdown: float) -> bool:
        """Stateless check, kept for sizing calls that have no halt context."""
        return self.halt_dd > 0 and drawdown <= -abs(self.halt_dd)

    def size(self, equity: float, cash: float, price: float, stop: float,
             positions: Dict[str, Position], sector: str, drawdown: float,
             exposure_cap: float, learn_scale: float = 1.0,
             halted: bool = False) -> tuple:
        """Return (qty, reason). qty == 0 means the trade is refused."""
        if price <= 0 or stop >= price:
            return 0, "bad_stop"
        if len(positions) >= self.max_positions:
            return 0, "max_positions"
        if sum(1 for p in positions.values() if p.sector == sector) >= self.max_sector:
            return 0, "sector_cap"
        if halted:
            return 0, "drawdown_halt"

        risk_per_share = price - stop
        risk_budget = equity * self.risk_fraction(drawdown, learn_scale)

        # portfolio heat: total rupees at risk across all open positions
        current_heat = sum(p.risk_to_stop() for p in positions.values())
        heat_room = max(0.0, equity * self.max_heat - current_heat)
        if heat_room <= 0:
            return 0, "heat_cap"
        risk_budget = min(risk_budget, heat_room)

        qty = int(math.floor(risk_budget / risk_per_share))
        if qty <= 0:
            return 0, "risk_too_small"

        # position value cap
        qty = min(qty, int(math.floor(equity * self.max_position_frac / price)))
        # cash cap, leaving a small buffer for charges
        qty = min(qty, int(math.floor(cash * 0.995 / price)))
        if qty <= 0:
            return 0, "no_cash"

        if exposure_cap is not None and exposure_cap >= 0:
            qty = min(qty, int(math.floor(max(0.0, exposure_cap) / price)))
            if qty <= 0:
                return 0, "exposure_cap"

        if qty * price < self.min_position_value:
            # Below this, the fixed DP charge and Rs 20 brokerage make the
            # round trip cost more than the expected edge on the trade.
            return 0, "below_min_value"

        return qty, "ok"
