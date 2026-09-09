"""Configuration: JSONC (JSON + // comments) loaded into a dotted-path dict.

Dotted-path access matters more than it looks: the walk-forward optimiser
mutates parameters by path ("exit.trail_atr_mult"), so every tunable knob is
addressable by a string without any per-parameter plumbing.
"""
from __future__ import annotations

import copy
import json
import os
import re
from typing import Any, Dict

DEFAULTS: Dict[str, Any] = {
    "capital": 100000.0,

    "universe": {
        "file": "config/universe_nifty100.csv",
        "min_price": 30.0,
        "max_price_frac_of_equity": 0.22,   # skip names one share of which blows the position cap
        "min_turnover_cr": 25.0,            # 20d median traded value, in Rs crore
        "min_history_bars": 260,
    },

    "regime": {
        "index_symbol": "NIFTY100",
        "ma_long": 200,
        "ma_short": 50,
        "breadth_ma": 50,
        "risk_on_breadth": 0.45,            # >=45% of universe above its 50DMA
        "risk_off_breadth": 0.30,
        "exposure": {"risk_on": 1.0, "neutral": 0.55, "risk_off": 0.0},
        "chop_atr_pct_max": 0.030,          # index ATR% above this => treat as neutral at best
    },

    "screen": {
        "ema_fast": 50,
        "ema_slow": 200,
        "require_stacked_ema": True,        # close > ema50 > ema200
        "require_slope_ema_slow": True,
        "atr_pct_min": 0.012,
        "atr_pct_max": 0.070,
        "max_pct_below_52w_high": 0.25,
        "min_adx": 18.0,
        "rs_lookback": 63,                  # relative strength vs index
        "min_rs": 0.0,
    },

    "rank": {
        "mom_lookback": 90,
        "weights": {
            "mom_slope_r2": 0.55,
            "roc_126": 0.20,
            "rs_index": 0.15,
            "atr_pct": -0.10,               # negative weight = penalty
        },
        "top_n": 25,                        # candidate pool considered for entry
    },

    "entry": {
        "setups": ["breakout", "pullback"],
        "breakout_lookback": 20,
        "breakout_vol_mult": 1.2,           # volume vs 20d average on the trigger bar
        "pullback_rsi_len": 3,
        "pullback_rsi_max": 35.0,
        "pullback_ema": 20,
        "pullback_max_dist_atr": 1.0,       # must be within 1 ATR of the 20 EMA
        "max_gap_pct": 0.030,               # skip if next open gaps more than this above signal close
        "max_new_per_day": 3,
        "min_score_z": 0.0,
    },

    "exit": {
        "init_stop_atr_mult": 2.5,
        "min_stop_atr_mult": 1.5,           # a stop closer than this is noise, not risk
        "max_stop_atr_mult": 3.5,           # wider than this and the size is meaningless
        "trail_atr_mult": 3.0,              # chandelier from highest high since entry
        "trail_atr_mult_riskoff": 1.5,
        "use_structure_stop": True,         # also respect the swing low
        "structure_lookback": 10,
        # Exit defaults are deliberately SLOW. At Rs 1L the risk unit is about
        # Rs 1,200 while a round trip costs Rs 70-110, so every avoidable exit
        # burns 6-9% of an R. Turnover, not stock selection, is the binding
        # constraint on a small account - and holding while momentum persists is
        # the stated objective, so the trailing stop does the work by default.
        "time_stop_bars": 25,               # five weeks to prove itself
        "time_stop_min_r": 0.0,             # ... and only cut it if still underwater
        "momentum_exit_ema": 20,
        "momentum_exit_closes": 0,          # 0 = off; the chandelier trail exits instead
        "momentum_exit_only_if_profit": True,
        "scale_out_at_r": 0.0,              # 0 disables; e.g. 2.0 sells half at +2R
        "scale_out_frac": 0.5,
        "max_hold_bars": 0,                 # 0 = let winners run indefinitely
        "rank_exit_threshold": 0,           # 0 = off; enable to rotate on rank decay
    },

    "risk": {
        "risk_per_trade": 0.012,            # 1.2% of equity risked per position
        "max_positions": 6,                 # 6 x ~Rs 17k beats 10 x ~Rs 10k on cost
        "max_position_frac": 0.25,
        "max_portfolio_heat": 0.075,        # sum of open risk; fits 6 full-size positions
        "max_sector_positions": 3,
        "min_position_value": 8000.0,       # below this the flat DP + brokerage dominates
        "kelly_scaling": False,
        "derisk_on_drawdown": 0.10,         # portfolio DD past this halves risk per trade
        "derisk_factor": 0.5,
        "halt_on_drawdown": 0.20,           # stop opening new positions at this DD
        "resume_on_drawdown": 0.10,         # resume if the DD recovers to here, OR ...
        "halt_cooldown_days": 40,           # ... after this many sessions, once the
                                            #     market regime is risk-on again
        "post_halt_risk_factor": 0.5,       # restart at half size ...
        "post_halt_probation_days": 60,     # ... for this long
    },

    "costs": {
        # Groww cash/delivery. VERIFY against your contract note before trusting
        # backtest numbers - broker tariffs change and these are the 2025 published
        # rates. Every field is per-leg unless stated.
        "brokerage_pct": 0.001,             # 0.1% ...
        "brokerage_cap": 20.0,              # ... or Rs 20, whichever is LOWER
        "brokerage_min": 5.0,
        "stt_buy_pct": 0.001,
        "stt_sell_pct": 0.001,
        "exchange_txn_pct": 0.0000297,      # NSE cash
        "sebi_pct": 0.000001,
        "stamp_duty_buy_pct": 0.00015,
        "gst_pct": 0.18,                    # on brokerage + exchange + sebi
        "dp_charge_sell": 20.0,             # per scrip per day on delivery sells
        "ipft_pct": 0.000001,
        "slippage_bps": 12.0,               # each way; large caps at Rs 1L size
    },

    "backtest": {
        "start": "2015-01-01",
        "end": "",
        "warmup_bars": 260,
        "execution": "next_open",
        "allow_intraday_stops": True,
    },

    "learn": {
        "train_years": 4,
        "test_months": 6,
        "embargo_days": 10,
        "n_random_samples": 120,
        "objective": "calmar_turnover",
        "robust_neighbourhood": 5,          # score = median of the k best, not the single peak
        "promotion_min_edge": 0.05,         # challenger must beat champion by 5% of objective
        "promotion_min_trades": 40,
        "meta_label": {
            "enabled": False,
            "min_trades": 150,
            "threshold": 0.5,
            "l2": 1.0,
            "epochs": 400,
            "lr": 0.08,
        },
        "decay_window_trades": 60,
        "decay_alert_pct": 5.0,             # live expectancy under backtest 5th pct => alert
    },

    "data": {
        "cache_dir": "data_cache",
        "provider": "yfinance",
        "suffix": ".NS",
        "index_symbol_map": {"NIFTY100": "^CNX100", "NIFTY50": "^NSEI"},
    },

    "paths": {
        "runs_dir": "runs",
        "state_dir": "state",
        "orders_dir": "orders",
    },
}


_COMMENT_RE = re.compile(r'(?m)(?<!:)//.*?$|/\*.*?\*/', re.S)


def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments that fall outside string literals."""
    out, i, n = [], 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    """Nested config with dotted get/set, so any knob is addressable by string."""

    def __init__(self, data: Dict[str, Any] | None = None):
        self.data = deep_merge(DEFAULTS, data or {})

    @classmethod
    def load(cls, path: str | None) -> "Config":
        if not path:
            return cls()
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.loads(strip_jsonc(fh.read()))
        return cls(raw)

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, path: str, value: Any) -> None:
        parts = path.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def with_overrides(self, overrides: Dict[str, Any]) -> "Config":
        """Return a copy with dotted-path overrides applied (optimiser entry point)."""
        c = Config(copy.deepcopy(self.data))
        for k, v in overrides.items():
            c.set(k, v)
        return c

    def flatten(self, prefix: str = "") -> Dict[str, Any]:
        out: Dict[str, Any] = {}

        def walk(node: Any, pre: str) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, f"{pre}.{k}" if pre else k)
            else:
                out[pre] = node

        walk(self.data, prefix)
        return out

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, sort_keys=True)

    def __repr__(self) -> str:
        return f"Config(capital={self.get('capital')}, keys={list(self.data)})"
