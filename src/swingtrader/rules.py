"""Exit rules as pure functions, shared by the backtest and the live planner.

This module exists to prevent the single most damaging class of bug in a
systematic trading setup: the backtest and the live script slowly drifting
apart until the thing you validated is not the thing you are trading. Both
callers import these functions; neither reimplements them.
"""
from __future__ import annotations

from typing import Optional

from .config import Config
from .regime import RISK_OFF
from .util import NA, is_na


def trail_stop_level(highest_high: float, atr: float, regime_label: str,
                     cfg: Config) -> float:
    """Chandelier trail: highest high since entry, less N ATR.

    Tightened in a risk-off regime, because the distribution of the next 20 days
    is different when the index is under its 200 DMA and pretending otherwise
    gives back the whole trend.
    """
    if is_na(atr) or atr <= 0 or is_na(highest_high):
        return NA
    mult = (float(cfg.get("exit.trail_atr_mult_riskoff", 1.5))
            if regime_label == RISK_OFF
            else float(cfg.get("exit.trail_atr_mult", 3.0)))
    return highest_high - mult * atr


def ratchet(current_stop: float, proposed: float) -> float:
    """A stop only ever moves up. Widening a stop to 'give the trade room' is
    how a 1R loss becomes a 4R loss, every time."""
    if is_na(proposed):
        return current_stop
    if is_na(current_stop):
        return proposed
    return max(current_stop, proposed)


def close_exit_reason(cfg: Config, bars_held: int, r_multiple: float,
                      below_ema_closes: int, in_profit: bool,
                      rank: Optional[int] = None) -> Optional[str]:
    """Close-based exits, evaluated at the close and acted on the next open.

    Order matters: the cheapest, most certain reason wins so the journal
    attributes the exit to the rule that actually fired.
    """
    max_hold = int(cfg.get("exit.max_hold_bars", 0))
    if max_hold > 0 and bars_held >= max_hold:
        return "max_hold"

    ts_bars = int(cfg.get("exit.time_stop_bars", 25))
    ts_min_r = float(cfg.get("exit.time_stop_min_r", 0.0))
    if ts_bars > 0 and bars_held >= ts_bars and not is_na(r_multiple) \
            and r_multiple < ts_min_r:
        return "time_stop"

    mom_closes = int(cfg.get("exit.momentum_exit_closes", 0))
    if mom_closes > 0 and below_ema_closes >= mom_closes:
        if not bool(cfg.get("exit.momentum_exit_only_if_profit", True)) or in_profit:
            return "momentum_lost"

    rank_thr = int(cfg.get("exit.rank_exit_threshold", 0))
    if rank_thr > 0 and rank is not None and rank > rank_thr \
            and not is_na(r_multiple) and r_multiple > 0:
        return "rank_decay"

    return None


def initial_stop(close: float, atr: float, structure_low: float, cfg: Config) -> float:
    """Initial stop: swing structure where available, clamped into an ATR band.

    Too tight and ordinary noise closes a trade that was right; too wide and the
    position size stops meaning anything.
    """
    if is_na(atr) or atr <= 0:
        return NA
    stop = close - float(cfg.get("exit.init_stop_atr_mult", 2.5)) * atr
    if bool(cfg.get("exit.use_structure_stop", True)) and not is_na(structure_low):
        lo = close - float(cfg.get("exit.max_stop_atr_mult", 3.5)) * atr
        hi = close - float(cfg.get("exit.min_stop_atr_mult", 1.5)) * atr
        stop = min(max(structure_low * 0.995, lo), hi)
    return stop
