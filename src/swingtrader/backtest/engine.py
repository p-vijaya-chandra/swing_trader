"""Event-driven daily-bar backtest.

Ordering inside a session is where backtests lie to you, so it is fixed and
explicit here:

  1. Fill orders queued at YESTERDAY's close, at TODAY's open (sells first, so
     the cash they free is available to buys).
  2. Check stops against today's bar, using the stop level as it stood at
     yesterday's close. A stop is never updated and then tested on the same bar.
  3. Update trailing stops and bar counters using today's close.
  4. Evaluate close-based exit rules -> queue for tomorrow's open.
  5. Generate entry signals -> size -> queue for tomorrow's open.
  6. Mark to market.

Nothing in steps 1-6 can read a bar later than the one being processed, and
every order is filled at a price that exists after the decision was made.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ..config import Config
from ..costs import CostModel
from ..data.models import Series
from ..features import Features
from ..portfolio import Position, RiskManager, Trade
from ..regime import RISK_OFF, RegimeModel, RegimeState
from ..rules import close_exit_reason, ratchet, trail_stop_level
from ..strategy import Candidate, SwingStrategy
from ..util import NA, is_na


@dataclass
class PendingOrder:
    symbol: str
    side: str                      # BUY / SELL
    qty: int                       # 0 on a SELL means "whatever is held"
    reason: str
    signal_close: float
    candidate: Optional[Candidate] = None


@dataclass
class DailyRecord:
    date: str
    equity: float
    cash: float
    invested: float
    n_positions: int
    drawdown: float
    regime: str
    exposure_target: float
    breadth: float
    halted: bool = False


@dataclass
class BacktestResult:
    daily: List[DailyRecord] = field(default_factory=list)
    trades: List[Trade] = field(default_factory=list)
    rejections: Dict[str, int] = field(default_factory=dict)
    costs_total: float = 0.0
    costs_breakdown: Dict[str, float] = field(default_factory=dict)
    start_equity: float = 0.0
    end_equity: float = 0.0
    halt_days: int = 0
    config_snapshot: Dict[str, object] = field(default_factory=dict)
    open_positions: List[Dict[str, object]] = field(default_factory=list)

    @property
    def equity_curve(self) -> List[float]:
        return [d.equity for d in self.daily]

    @property
    def dates(self) -> List[str]:
        return [d.date for d in self.daily]


class BacktestEngine:
    def __init__(self, cfg: Config, feats: Dict[str, Features],
                 index_series: Optional[Series], regime_model: Optional[RegimeModel] = None,
                 entry_filter=None):
        self.cfg = cfg
        self.feats = feats
        self.index = index_series
        self.strategy = SwingStrategy(cfg)
        self.risk = RiskManager(cfg)
        self.costs = CostModel(cfg)
        self.regime_model = regime_model
        # entry_filter(candidate, context) -> bool. This is the hook the learned
        # meta-label model plugs into; None means "take every triggered signal".
        self.entry_filter = entry_filter

        e = cfg.get("exit", {})
        self.trail_mult = float(e.get("trail_atr_mult", 3.0))
        self.trail_mult_off = float(e.get("trail_atr_mult_riskoff", 1.5))
        self.time_stop_bars = int(e.get("time_stop_bars", 12))
        self.time_stop_min_r = float(e.get("time_stop_min_r", 0.4))
        self.mom_exit_ema = int(e.get("momentum_exit_ema", 20))
        self.mom_exit_closes = int(e.get("momentum_exit_closes", 2))
        self.mom_exit_profit_only = bool(e.get("momentum_exit_only_if_profit", True))
        self.scale_out_r = float(e.get("scale_out_at_r", 0.0))
        self.scale_out_frac = float(e.get("scale_out_frac", 0.5))
        self.max_hold = int(e.get("max_hold_bars", 0))
        self.rank_exit = int(e.get("rank_exit_threshold", 45))

        self.max_new_per_day = int(cfg.get("entry.max_new_per_day", 3))
        self.max_gap = float(cfg.get("entry.max_gap_pct", 0.03))
        self.allow_intraday_stops = bool(cfg.get("backtest.allow_intraday_stops", True))

    # ------------------------------------------------------------------ helpers
    def _trading_dates(self, start: str, end: str) -> List[str]:
        if self.index is not None and len(self.index):
            ds = self.index.dates
        else:
            seen = set()
            for f in self.feats.values():
                seen.update(f.series.dates)
            ds = sorted(seen)
        return [d for d in ds if d >= start and (not end or d <= end)]

    def _price(self, sym: str, date: str, field_: str = "close") -> float:
        f = self.feats.get(sym)
        if f is None:
            return NA
        i = f.series.pos(date)
        if i is None:
            return NA
        return getattr(f.series, field_)[i]

    def _mark(self, positions: Dict[str, Position], date: str,
              last_price: Dict[str, float]) -> float:
        total = 0.0
        for sym, p in positions.items():
            px = self._price(sym, date)
            if is_na(px):
                px = last_price.get(sym, p.entry_price)   # halted / missing session
            else:
                last_price[sym] = px
            total += p.qty * px
        return total

    # --------------------------------------------------------------------- run
    def run(self, start: str = "", end: str = "", verbose: bool = False) -> BacktestResult:
        cfg = self.cfg
        start = start or cfg.get("backtest.start", "2015-01-01")
        end = end or cfg.get("backtest.end", "") or ""
        dates = self._trading_dates(start, end)
        res = BacktestResult()
        if not dates:
            return res

        if self.regime_model is None:
            self.regime_model = RegimeModel(cfg)
            self.regime_model.build(self.index, self.feats, dates)

        equity = cash = float(cfg.get("capital", 100000.0))
        res.start_equity = equity
        # Two peaks, deliberately.
        #   peak_report : all-time high. Drives the drawdown you are SHOWN. Never
        #                 reset - hiding a real drawdown from yourself is useless.
        #   peak_risk   : high since the last restart. Drives the circuit breaker
        #                 and the de-risking rule. Reset when a halt is released,
        #                 because otherwise a single -20% event leaves the system
        #                 permanently tripped: it resumes, immediately measures the
        #                 same old -20% against the same old peak, and halts again
        #                 on the very next bar. The breaker must judge the CURRENT
        #                 attempt, not one that has already been paid for.
        peak = equity
        peak_risk = equity
        positions: Dict[str, Position] = {}
        pending: List[PendingOrder] = []
        last_price: Dict[str, float] = {}
        cost_acc: Dict[str, float] = {}
        rejections: Dict[str, int] = {}
        rank_cache: Dict[str, Dict[str, int]] = {}
        learn_scale = 1.0
        halted = False
        halt_days = 0
        days_in_halt = 0
        days_since_resume = -1

        def book_costs(cb) -> float:
            for k, v in cb.as_dict().items():
                if k == "total":
                    continue
                cost_acc[k] = cost_acc.get(k, 0.0) + v
            return cb.total

        def close_position(p: Position, date: str, price: float, reason: str,
                           mae_r: float, mfe_r: float) -> None:
            nonlocal cash
            fill = self.costs.fill_price(price, "SELL")
            cb = self.costs.charges(fill, p.qty, "SELL", ref_price=price)
            c = book_costs(cb)
            proceeds = fill * p.qty - c
            cash += proceeds
            gross = (fill - p.entry_price) * p.qty + p.realised_pnl
            total_costs = p.costs_paid + c
            net = gross - c
            r = (net / p.initial_risk_value) if p.initial_risk_value > 0 else NA
            res.trades.append(Trade(
                symbol=p.symbol, sector=p.sector, setup=p.setup,
                entry_date=p.entry_date, exit_date=date,
                entry_price=p.entry_price, exit_price=fill, qty=p.original_qty or p.qty,
                initial_stop=p.initial_stop, risk_per_share=p.risk_per_share,
                gross_pnl=gross, costs=total_costs, net_pnl=net,
                r_multiple=r, bars_held=p.bars_held, exit_reason=reason,
                mae_r=mae_r, mfe_r=mfe_r, regime_at_entry=p.regime_at_entry,
                entry_rank=p.entry_rank, entry_score=p.entry_score,
                entry_snapshot=p.entry_snapshot))
            positions.pop(p.symbol, None)

        mae_track: Dict[str, float] = {}
        mfe_track: Dict[str, float] = {}

        for t, date in enumerate(dates):
            regime = self.regime_model.at(date)

            # ---------------------------------------------------- 1. fills
            sells = [o for o in pending if o.side == "SELL"]
            buys = [o for o in pending if o.side == "BUY"]
            pending = []

            for o in sells:
                p = positions.get(o.symbol)
                if p is None:
                    continue
                px = self._price(o.symbol, date, "open")
                if is_na(px):
                    pending.append(o)          # no session for this name; retry
                    continue
                close_position(p, date, px, o.reason,
                               mae_track.get(o.symbol, 0.0), mfe_track.get(o.symbol, 0.0))
                mae_track.pop(o.symbol, None)
                mfe_track.pop(o.symbol, None)

            drawdown = equity / peak_risk - 1.0 if peak_risk > 0 else 0.0
            invested_now = self._mark(positions, date, last_price)
            exposure_room = max(0.0, equity * regime.exposure - invested_now)

            for o in buys:
                if o.symbol in positions:
                    rejections["already_held"] = rejections.get("already_held", 0) + 1
                    continue
                op = self._price(o.symbol, date, "open")
                if is_na(op) or op <= 0:
                    rejections["no_open"] = rejections.get("no_open", 0) + 1
                    continue
                if o.signal_close > 0 and op / o.signal_close - 1.0 > self.max_gap:
                    # Chasing a gap ruins the stop distance the size was built on.
                    rejections["gap_too_big"] = rejections.get("gap_too_big", 0) + 1
                    continue
                cand = o.candidate
                stop = cand.stop_ref if cand else op * 0.95
                # Re-anchor the stop to the actual fill so risk stays exactly 1R
                if cand and cand.close > 0:
                    stop = op - (cand.close - cand.stop_ref)
                qty, why = self.risk.size(equity, cash, op, stop, positions,
                                          cand.sector if cand else "Other", drawdown,
                                          exposure_room, learn_scale, halted)
                if qty <= 0:
                    rejections[why] = rejections.get(why, 0) + 1
                    continue
                fill = self.costs.fill_price(op, "BUY")
                cb = self.costs.charges(fill, qty, "BUY", ref_price=op)
                c = book_costs(cb)
                outlay = fill * qty + c
                if outlay > cash:
                    qty = int(math.floor((cash * 0.995 - c) / fill))
                    if qty <= 0:
                        rejections["no_cash"] = rejections.get("no_cash", 0) + 1
                        continue
                    cb = self.costs.charges(fill, qty, "BUY", ref_price=op)
                    c = book_costs(cb)
                    outlay = fill * qty + c
                cash -= outlay
                rps = fill - stop
                p = Position(
                    symbol=o.symbol, sector=cand.sector if cand else "Other", qty=qty,
                    entry_price=fill, entry_date=date, setup=cand.setup if cand else "",
                    initial_stop=stop, stop=stop, risk_per_share=rps,
                    initial_risk_value=rps * qty, highest_high=self._price(o.symbol, date, "high"),
                    lowest_low=self._price(o.symbol, date, "low"), costs_paid=c,
                    entry_rank=cand.rank if cand else 0,
                    entry_score=cand.score if cand else 0.0,
                    regime_at_entry=regime.label,
                    entry_snapshot=dict(cand.snapshot) if cand else {},
                    original_qty=qty)
                positions[o.symbol] = p
                exposure_room = max(0.0, exposure_room - fill * qty)
                mae_track[o.symbol] = 0.0
                mfe_track[o.symbol] = 0.0

            # ------------------------------------------- 2. stops on today's bar
            for sym in list(positions):
                p = positions[sym]
                f = self.feats.get(sym)
                i = f.series.pos(date) if f else None
                if i is None:
                    continue
                o_, h_, l_ = f.series.open[i], f.series.high[i], f.series.low[i]

                if self.allow_intraday_stops and p.stop > 0:
                    # A trailing stop that has ratcheted above entry is a
                    # profit-taking exit, not a loss. Reporting both as "stop"
                    # hides which half of the system is actually working.
                    kind = "stop_trail" if p.stop > p.initial_stop + 1e-9 else "stop_initial"
                    if o_ <= p.stop:
                        # gapped through the stop: you get the open, not the stop
                        close_position(p, date, o_, kind + "_gap",
                                       mae_track.get(sym, 0.0), mfe_track.get(sym, 0.0))
                        mae_track.pop(sym, None); mfe_track.pop(sym, None)
                        continue
                    if l_ <= p.stop:
                        close_position(p, date, p.stop, kind,
                                       mae_track.get(sym, 0.0), mfe_track.get(sym, 0.0))
                        mae_track.pop(sym, None); mfe_track.pop(sym, None)
                        continue

                if self.scale_out_r > 0 and not p.scaled_out and p.risk_per_share > 0:
                    target = p.entry_price + self.scale_out_r * p.risk_per_share
                    if h_ >= target:
                        sell_qty = int(math.floor(p.qty * self.scale_out_frac))
                        if sell_qty > 0:
                            fillp = self.costs.fill_price(target, "SELL")
                            cb = self.costs.charges(fillp, sell_qty, "SELL", ref_price=target)
                            c = book_costs(cb)
                            cash += fillp * sell_qty - c
                            p.realised_pnl += (fillp - p.entry_price) * sell_qty
                            p.costs_paid += c
                            p.qty -= sell_qty
                            p.scaled_out = True
                            if p.qty <= 0:
                                positions.pop(sym, None)
                                continue

            # ------------------------------- 3. update trails and bar counters
            for sym, p in positions.items():
                f = self.feats.get(sym)
                i = f.series.pos(date) if f else None
                if i is None:
                    continue
                c_ = f.series.close[i]
                p.bars_held += 1
                p.highest_high = max(p.highest_high, f.series.high[i])
                p.lowest_low = min(p.lowest_low, f.series.low[i])
                if p.risk_per_share > 0:
                    mae_track[sym] = min(mae_track.get(sym, 0.0),
                                         (p.lowest_low - p.entry_price) / p.risk_per_share)
                    mfe_track[sym] = max(mfe_track.get(sym, 0.0),
                                         (p.highest_high - p.entry_price) / p.risk_per_share)
                atr = f.get("atr", i)
                if not is_na(atr) and atr > 0:
                    p.stop = ratchet(p.stop, trail_stop_level(
                        p.highest_high, atr, regime.label, self.cfg))
                ema = f.get("ema_pull", i)
                if not is_na(ema):
                    p.below_ema_closes = p.below_ema_closes + 1 if c_ < ema else 0

            # --------------------------------- 4. close-based exits -> tomorrow
            ranks = None
            if self.rank_exit > 0 and positions:
                ranked = self.strategy.rank_universe(self.feats, date, equity)
                ranks = {c.symbol: c.rank for c in ranked}
                rank_cache[date] = ranks

            for sym, p in list(positions.items()):
                px = self._price(sym, date)
                if is_na(px):
                    continue
                r_now = p.r_multiple(px)
                # Shared with the live planner - see rules.py. Dead money costs
                # you the trade you cannot take, because the slot and the heat
                # budget are already spent.
                reason = close_exit_reason(
                    self.cfg, p.bars_held, r_now, p.below_ema_closes,
                    px > p.entry_price,
                    ranks.get(sym, 10 ** 6) if ranks is not None else None)

                if reason:
                    pending.append(PendingOrder(sym, "SELL", 0, reason, px))

            # ------------------------------------ 5. entry signals -> tomorrow
            queued_syms = {o.symbol for o in pending}
            if regime.label != RISK_OFF and not halted:
                room = self.risk.max_positions - len(positions)
                if room > 0:
                    sigs = self.strategy.signals(self.feats, date, equity, regime)
                    taken = 0
                    for cand in sigs:
                        if taken >= min(self.max_new_per_day, room):
                            break
                        if cand.symbol in positions or cand.symbol in queued_syms:
                            continue
                        if self.entry_filter is not None:
                            ctx = {"regime": regime.label, "drawdown": drawdown,
                                   "n_positions": len(positions)}
                            if not self.entry_filter(cand, ctx):
                                rejections["meta_label"] = rejections.get("meta_label", 0) + 1
                                continue
                        pending.append(PendingOrder(cand.symbol, "BUY", 0,
                                                    f"entry:{cand.setup}", cand.close, cand))
                        taken += 1

            # ------------------------------------------------ 6. mark to market
            invested = self._mark(positions, date, last_price)
            equity = cash + invested
            peak = max(peak, equity)
            peak_risk = max(peak_risk, equity)
            dd_report = equity / peak - 1.0 if peak > 0 else 0.0
            dd = equity / peak_risk - 1.0 if peak_risk > 0 else 0.0
            was_halted = halted
            halted = self.risk.halt_state(dd, halted, days_in_halt,
                                          regime.label != RISK_OFF)
            if halted:
                halt_days += 1
                days_in_halt = days_in_halt + 1 if was_halted else 1
                days_since_resume = -1
            else:
                days_in_halt = 0
                if was_halted:
                    days_since_resume = 0
                    peak_risk = equity      # fresh start: judge the new attempt
                elif days_since_resume >= 0:
                    days_since_resume += 1
            learn_scale = self.risk.probation_scale(days_since_resume) if days_since_resume >= 0 else 1.0
            if halted != was_halted:
                rejections["halt_entered" if halted else "halt_released"] = \
                    rejections.get("halt_entered" if halted else "halt_released", 0) + 1

            res.daily.append(DailyRecord(
                date=date, equity=equity, cash=cash, invested=invested,
                n_positions=len(positions), drawdown=dd_report, regime=regime.label,
                exposure_target=regime.exposure, breadth=regime.breadth,
                halted=halted))

            if verbose and t % 250 == 0:
                print(f"  {date} equity={equity:,.0f} pos={len(positions)} {regime.label}")

        # ------------------------------------------------------------- wrap up
        final_date = dates[-1]
        for sym, p in list(positions.items()):
            px = self._price(sym, final_date)
            if is_na(px):
                px = last_price.get(sym, p.entry_price)
            res.open_positions.append({
                "symbol": sym, "qty": p.qty, "entry_date": p.entry_date,
                "entry_price": p.entry_price, "last_price": px, "stop": p.stop,
                "r_multiple": p.r_multiple(px), "bars_held": p.bars_held,
                "unrealised": (px - p.entry_price) * p.qty})

        res.halt_days = halt_days
        res.end_equity = equity
        res.rejections = rejections
        res.costs_breakdown = cost_acc
        res.costs_total = sum(cost_acc.values())
        res.config_snapshot = self.cfg.flatten()
        return res
