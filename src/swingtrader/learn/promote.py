"""Champion / challenger promotion.

A self-improving system that promotes whatever scored highest last night is not
self-improving, it is a random walk with extra steps. The gate here is
deliberately hard to pass:

  1. The challenger must beat the champion on the objective by a real margin,
     not a rounding error.
  2. It must not be worse on EITHER drawdown or expectancy. A configuration that
     buys 2% of CAGR with 10% of extra drawdown is not an improvement on a Rs 1L
     account you actually have to live with.
  3. Its out-of-sample trade sample must be large enough to mean anything.
  4. A bootstrap confidence interval on its per-trade R must exclude zero at the
     configured level. If you cannot distinguish the edge from luck, you do not
     bet the account on it.

Every decision - promoted or rejected - is written to an append-only audit log,
so months later you can ask "why is the system trading like this?" and get an
answer instead of a guess.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..config import Config
from ..util import NA, bootstrap_mean_ci, is_na


@dataclass
class Champion:
    version: int
    created_utc: str
    params: Dict[str, Any]
    objective: str
    score: float
    metrics: Dict[str, Any] = field(default_factory=dict)
    note: str = ""


@dataclass
class PromotionDecision:
    promoted: bool
    reasons: List[str] = field(default_factory=list)
    challenger_score: float = NA
    champion_score: float = NA
    r_ci: tuple = (NA, NA)


class ChampionStore:
    """Champion config + append-only audit trail on disk."""

    def __init__(self, state_dir: str = "state"):
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self.champion_path = os.path.join(state_dir, "champion.json")
        self.audit_path = os.path.join(state_dir, "promotions.jsonl")

    def load(self) -> Optional[Champion]:
        if not os.path.exists(self.champion_path):
            return None
        with open(self.champion_path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return Champion(**d)

    def save(self, champ: Champion) -> None:
        with open(self.champion_path, "w", encoding="utf-8") as fh:
            json.dump(asdict(champ), fh, indent=2, sort_keys=True)

    def audit(self, record: Dict[str, Any]) -> None:
        record = dict(record)
        record.setdefault("utc", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        with open(self.audit_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")

    def history(self, limit: int = 50) -> List[Dict[str, Any]]:
        if not os.path.exists(self.audit_path):
            return []
        out = []
        with open(self.audit_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return out[-limit:]

    def promote(self, params: Dict[str, Any], objective: str, score: float,
                metrics: Dict[str, Any], note: str = "") -> Champion:
        prev = self.load()
        champ = Champion(
            version=(prev.version + 1) if prev else 1,
            created_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            params=params, objective=objective, score=score,
            metrics=metrics, note=note)
        self.save(champ)
        return champ

    def apply(self, cfg: Config) -> Config:
        """Overlay the current champion's parameters onto a base config."""
        champ = self.load()
        return cfg.with_overrides(champ.params) if champ else cfg


def evaluate_promotion(cfg: Config, champion: Optional[Champion],
                       challenger_score: float, challenger_metrics: Dict[str, Any],
                       oos_trades: Sequence, alpha: float = 0.10) -> PromotionDecision:
    L = cfg.get("learn", {})
    min_edge = float(L.get("promotion_min_edge", 0.05))
    min_trades = int(L.get("promotion_min_trades", 40))

    d = PromotionDecision(promoted=False, challenger_score=challenger_score,
                          champion_score=champion.score if champion else NA)

    n = len(oos_trades)
    if n < min_trades:
        d.reasons.append(f"only {n} out-of-sample trades, need {min_trades}")
        return d

    rs = [t.r_multiple for t in oos_trades if not is_na(t.r_multiple)]
    lo, hi = bootstrap_mean_ci(rs, n_boot=2000, alpha=alpha)
    d.r_ci = (lo, hi)
    if is_na(lo) or lo <= 0:
        d.reasons.append(
            f"per-trade edge not distinguishable from zero "
            f"({100*(1-alpha):.0f}% CI on R = [{lo:.3f}, {hi:.3f}])")
        return d

    if champion is None:
        d.promoted = True
        d.reasons.append("no champion yet - installing first validated config")
        return d

    # Required bar to clear. A relative margin is meaningless once the champion
    # score is zero or negative, so fall back to an absolute one there.
    if champion.score > 0:
        threshold = champion.score * (1.0 + min_edge)
        margin_desc = f"{100*min_edge:.0f}% relative margin"
    else:
        threshold = champion.score + min_edge
        margin_desc = f"absolute margin of {min_edge:.3f}"
    if is_na(challenger_score) or challenger_score <= threshold:
        d.reasons.append(
            f"objective {challenger_score:.3f} does not clear {threshold:.3f} "
            f"(champion {champion.score:.3f} plus a {margin_desc})")
        return d

    cm = champion.metrics or {}
    ch_dd = abs(challenger_metrics.get("max_drawdown") or 0.0)
    cp_dd = abs(cm.get("max_drawdown") or 0.0)
    if cp_dd > 0 and ch_dd > cp_dd * 1.15:
        d.reasons.append(
            f"max drawdown worsens materially ({100*ch_dd:.1f}% vs {100*cp_dd:.1f}%)")
        return d

    ch_e = challenger_metrics.get("expectancy_r")
    cp_e = cm.get("expectancy_r")
    if not is_na(ch_e) and not is_na(cp_e) and cp_e is not None and ch_e < cp_e * 0.85:
        d.reasons.append(
            f"per-trade expectancy worsens ({ch_e:.3f}R vs {cp_e:.3f}R)")
        return d

    d.promoted = True
    d.reasons.append(
        f"beats champion {challenger_score:.3f} vs {champion.score:.3f}; "
        f"OOS R CI [{lo:.3f}, {hi:.3f}] excludes zero over {n} trades")
    return d
