"""Signal generation: screen -> rank -> trigger.

The three stages do different jobs and it is worth keeping them separate:

  SCREEN  is a veto. It answers "is this name tradeable at all today?" - liquid
          enough, trending, not insanely volatile, not a falling knife. It is
          binary and it never gets to express a preference.

  RANK    is a preference. Among names that pass, which have the strongest
          persistent trend? This is a cross-sectional z-score blend dominated by
          the annualised-slope x R-squared momentum measure.

  TRIGGER is timing. A high-ranked name is not a buy until price does something
          specific today. Without a trigger you buy extended names at random
          points and your stop distance is arbitrary.

Buying rank without a trigger is the single most common way retail momentum
systems end up with a 45% max drawdown.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..config import Config
from ..features import Features
from ..regime import RISK_OFF, RegimeState
from ..rules import initial_stop
from ..util import NA, is_na, zscores


@dataclass
class Candidate:
    symbol: str
    date: str
    setup: str
    score: float
    rank: int
    close: float
    atr: float
    stop_ref: float
    sector: str
    snapshot: Dict[str, float] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)


class SwingStrategy:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        s = cfg.get("screen", {})
        self.min_price = float(cfg.get("universe.min_price", 30.0))
        self.min_turnover = float(cfg.get("universe.min_turnover_cr", 25.0)) * 1e7
        self.min_history = int(cfg.get("universe.min_history_bars", 260))
        self.require_stacked = bool(s.get("require_stacked_ema", True))
        self.require_slope = bool(s.get("require_slope_ema_slow", True))
        self.atr_min = float(s.get("atr_pct_min", 0.012))
        self.atr_max = float(s.get("atr_pct_max", 0.070))
        self.max_below_52w = float(s.get("max_pct_below_52w_high", 0.25))
        self.min_adx = float(s.get("min_adx", 18.0))
        self.min_rs = float(s.get("min_rs", 0.0))

        self.weights: Dict[str, float] = dict(cfg.get("rank.weights", {}))
        self.top_n = int(cfg.get("rank.top_n", 25))

        e = cfg.get("entry", {})
        self.setups = list(e.get("setups", ["breakout", "pullback"]))
        self.brk_lookback = int(e.get("breakout_lookback", 20))
        self.brk_vol_mult = float(e.get("breakout_vol_mult", 1.2))
        self.pull_rsi_max = float(e.get("pullback_rsi_max", 35.0))
        self.pull_max_dist = float(e.get("pullback_max_dist_atr", 1.0))
        self.min_score_z = float(e.get("min_score_z", 0.0))

        self.init_stop_mult = float(cfg.get("exit.init_stop_atr_mult", 2.5))
        self.min_stop_mult = float(cfg.get("exit.min_stop_atr_mult", 1.5))
        self.max_stop_mult = float(cfg.get("exit.max_stop_atr_mult", 3.5))
        self.use_structure_stop = bool(cfg.get("exit.use_structure_stop", True))

    # ------------------------------------------------------------------ screen
    def passes_screen(self, f: Features, i: int, equity: float) -> Optional[str]:
        """Return None if the name is tradeable, else a short rejection reason."""
        if i < self.min_history:
            return "history"
        c = f.series.close[i]
        if c < self.min_price:
            return "price"

        max_frac = float(self.cfg.get("universe.max_price_frac_of_equity", 0.22))
        if equity > 0 and c > equity * max_frac:
            # One share already breaches the position cap; at Rs 1L this quietly
            # removes names like BOSCHLTD, and pretending otherwise would make
            # the backtest unimplementable.
            return "share_price_too_large"

        need = ("ema_fast", "ema_slow", "atr", "atr_pct", "mom", "adx", "turnover_med")
        if not f.ready(i, need):
            return "warmup"

        if f.get("turnover_med", i) < self.min_turnover:
            return "liquidity"

        ef, es = f.get("ema_fast", i), f.get("ema_slow", i)
        if self.require_stacked and not (c > ef > es):
            return "not_stacked"
        if self.require_slope:
            sl = f.get("ema_slow_slope", i)
            if is_na(sl) or sl < 0:
                return "slow_ema_falling"

        ap = f.get("atr_pct", i)
        if ap < self.atr_min:
            return "too_quiet"
        if ap > self.atr_max:
            return "too_volatile"

        pb = f.get("pct_below_52w", i)
        if not is_na(pb) and pb > self.max_below_52w:
            return "far_from_high"

        if f.get("adx", i) < self.min_adx:
            return "weak_trend"

        rs = f.get("rs_index", i)
        if not is_na(rs) and rs < self.min_rs:
            return "lagging_index"

        return None

    # -------------------------------------------------------------------- rank
    def rank_universe(self, feats: Dict[str, Features], date: str,
                      equity: float) -> List[Candidate]:
        """Cross-sectional ranking of everything that passes the screen today."""
        eligible: List[tuple] = []
        for sym, f in feats.items():
            i = f.series.pos(date)
            if i is None:
                continue
            if self.passes_screen(f, i, equity) is not None:
                continue
            eligible.append((sym, f, i))

        if not eligible:
            return []

        raw: Dict[str, List[float]] = {}
        for name in self.weights:
            col = {"mom_slope_r2": "mom", "atr_pct": "atr_pct", "rs_index": "rs_index",
                   "roc_126": "roc_126", "roc_63": "roc_63", "adx": "adx",
                   "roc_21": "roc_21", "pct_below_52w": "pct_below_52w"}.get(name, name)
            raw[name] = [f.get(col, i) for _, f, i in eligible]

        zs = {name: zscores(vals) for name, vals in raw.items()}

        scored: List[tuple] = []
        for k, (sym, f, i) in enumerate(eligible):
            total, wsum = 0.0, 0.0
            ok = True
            for name, w in self.weights.items():
                z = zs[name][k]
                if is_na(z):
                    # A missing input must not be silently treated as average;
                    # drop the name rather than flatter it.
                    ok = False
                    break
                total += w * z
                wsum += abs(w)
            if not ok or wsum == 0:
                continue
            scored.append((total / wsum, sym, f, i))

        scored.sort(key=lambda t: -t[0])

        out: List[Candidate] = []
        for rank, (score, sym, f, i) in enumerate(scored, start=1):
            atr = f.get("atr", i)
            close = f.series.close[i]
            # Shared with the live planner - see rules.initial_stop
            stop = initial_stop(close, atr, f.get("structure_low", i), self.cfg)
            out.append(Candidate(sym, date, "", score, rank, close, atr, stop,
                                 f.sector, f.snapshot(i)))
        return out

    # ----------------------------------------------------------------- trigger
    def trigger(self, f: Features, i: int) -> Optional[str]:
        """Does today's bar fire an entry setup? Returns the setup name."""
        c = f.series.close[i]

        if "breakout" in self.setups:
            dh = f.get("donchian_high", i)
            vr = f.get("vol_ratio", i)
            if not is_na(dh) and c > dh:
                if is_na(vr) or vr >= self.brk_vol_mult:
                    return "breakout"

        if "pullback" in self.setups:
            rsi_f = f.get("rsi_fast", i)
            dist = f.get("dist_ema_atr", i)
            prev_high = f.series.high[i - 1] if i > 0 else NA
            # Reclaim bar: dipped to the mean, then closed back above yesterday's
            # high. Buying the dip itself, without the reclaim, is how you catch
            # the one name that keeps going.
            if (not is_na(rsi_f) and not is_na(dist) and not is_na(prev_high)
                    and abs(dist) <= self.pull_max_dist
                    and c > prev_high):
                lo = min(f.get("rsi_fast", j) for j in range(max(0, i - 3), i + 1)
                         if not is_na(f.get("rsi_fast", j)))
                if lo <= self.pull_rsi_max:
                    return "pullback"

        return None

    def signals(self, feats: Dict[str, Features], date: str, equity: float,
                regime: RegimeState) -> List[Candidate]:
        """Ranked, triggered entry candidates for `date` (to be filled next open)."""
        if regime.label == RISK_OFF:
            return []
        ranked = self.rank_universe(feats, date, equity)
        pool = ranked[: self.top_n] if self.top_n > 0 else ranked
        out: List[Candidate] = []
        for cand in pool:
            if cand.score < self.min_score_z:
                continue
            f = feats[cand.symbol]
            i = f.series.pos(date)
            if i is None:
                continue
            setup = self.trigger(f, i)
            if setup is None:
                continue
            cand.setup = setup
            cand.reasons.append(f"rank {cand.rank}, score {cand.score:.2f}, {setup}")
            out.append(cand)
        return out
