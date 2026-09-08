"""Parameter search that tries not to overfit.

Two ideas do most of the work here.

RANDOM BEATS GRID. With ~15 tunable parameters a grid is combinatorially
hopeless, and most parameters barely matter. Random sampling spends its budget
across the dimensions that do.

NEVER TAKE THE PEAK. The single best-scoring parameter set in a search is
almost always the luckiest, not the best: it sits on a spike that will not be
there next year. So candidates are re-scored by the MEDIAN of their k nearest
neighbours in normalised parameter space, and the winner is the centre of the
best-performing *region*. A configuration whose neighbours are all mediocre is
a fluke; one surrounded by good neighbours is an edge with tolerance around it.
That single change is the difference between a walk-forward that degrades
gracefully and one that falls off a cliff out of sample.
"""
from __future__ import annotations

import json
import math
import os
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..config import Config
from ..util import NA, is_na, median


@dataclass
class ParamSpec:
    path: str
    kind: str                       # "float" | "int" | "choice" | "bool"
    low: float = 0.0
    high: float = 1.0
    step: float = 0.0
    choices: Optional[List[object]] = None

    def sample(self, rng: random.Random) -> object:
        if self.kind == "choice":
            return rng.choice(self.choices or [None])
        if self.kind == "bool":
            return rng.random() < 0.5
        if self.kind == "int":
            return int(rng.randint(int(self.low), int(self.high)))
        v = rng.uniform(self.low, self.high)
        if self.step:
            v = round(v / self.step) * self.step
        return round(v, 6)

    def normalise(self, value: object) -> float:
        """Map to [0,1] so distances across mixed parameter types are comparable."""
        if self.kind in ("choice", "bool"):
            opts = self.choices if self.kind == "choice" else [False, True]
            try:
                return (opts.index(value)) / max(1, len(opts) - 1)
            except (ValueError, AttributeError):
                return 0.0
        if self.high == self.low:
            return 0.0
        try:
            return (float(value) - self.low) / (self.high - self.low)
        except (TypeError, ValueError):
            return 0.0


class ParamSpace:
    def __init__(self, specs: Sequence[ParamSpec]):
        self.specs = list(specs)

    @classmethod
    def from_json(cls, path: str) -> "ParamSpace":
        from ..config import strip_jsonc
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.loads(strip_jsonc(fh.read()))
        specs = []
        for path_, spec in raw.items():
            specs.append(ParamSpec(
                path=path_, kind=spec.get("kind", "float"),
                low=float(spec.get("low", 0)), high=float(spec.get("high", 1)),
                step=float(spec.get("step", 0) or 0), choices=spec.get("choices")))
        return cls(specs)

    def sample(self, rng: random.Random) -> Dict[str, object]:
        return {s.path: s.sample(rng) for s in self.specs}

    def vector(self, params: Dict[str, object]) -> List[float]:
        return [s.normalise(params.get(s.path)) for s in self.specs]

    def paths(self) -> List[str]:
        return [s.path for s in self.specs]


@dataclass
class SearchResult:
    params: Dict[str, object]
    score: float
    robust_score: float = NA
    metrics: Dict[str, object] = field(default_factory=dict)


def random_search(base: Config, space: ParamSpace, evaluate: Callable[[Config], float],
                  n_samples: int = 100, seed: int = 11,
                  metrics_fn: Optional[Callable[[Config], Dict[str, object]]] = None,
                  progress: bool = False) -> List[SearchResult]:
    rng = random.Random(seed)
    results: List[SearchResult] = []
    seen = set()
    for i in range(n_samples):
        params = space.sample(rng)
        key = tuple(sorted((k, str(v)) for k, v in params.items()))
        if key in seen:
            continue
        seen.add(key)
        cfg = base.with_overrides(params)
        try:
            score = evaluate(cfg)
        except Exception as exc:                       # noqa: BLE001
            if progress:
                print(f"    sample {i}: failed ({exc})")
            continue
        m = metrics_fn(cfg) if metrics_fn else {}
        results.append(SearchResult(params, score, NA, m))
        if progress and (i + 1) % 20 == 0:
            best = max((r.score for r in results), default=float("-inf"))
            print(f"    {i+1}/{n_samples} sampled, best so far {best:.3f}")
    return results


def robust_pick(results: Sequence[SearchResult], space: ParamSpace,
                k: int = 5) -> Optional[SearchResult]:
    """Pick the centre of the best-performing REGION, not the highest peak."""
    finite = [r for r in results if r.score > float("-inf") and not is_na(r.score)]
    if not finite:
        return None
    if len(finite) <= k or k <= 1:
        return max(finite, key=lambda r: r.score)

    vecs = [space.vector(r.params) for r in finite]
    for i, r in enumerate(finite):
        dists = []
        for j, _ in enumerate(finite):
            if i == j:
                continue
            d = math.sqrt(sum((a - b) ** 2 for a, b in zip(vecs[i], vecs[j])))
            dists.append((d, finite[j].score))
        dists.sort(key=lambda t: t[0])
        neigh = [s for _, s in dists[:k - 1]] + [r.score]
        # Median, not mean: one catastrophic neighbour should not veto a good
        # region, and one spectacular neighbour should not rescue a bad one.
        r.robust_score = median(neigh)
    return max(finite, key=lambda r: (r.robust_score, r.score))


DEFAULT_SPACE = {
    "rank.mom_lookback":            {"kind": "int",   "low": 60,   "high": 150},
    "rank.top_n":                   {"kind": "int",   "low": 10,   "high": 40},
    "screen.atr_pct_max":           {"kind": "float", "low": 0.04, "high": 0.10, "step": 0.005},
    "screen.min_adx":               {"kind": "float", "low": 12.0, "high": 30.0, "step": 1.0},
    "screen.max_pct_below_52w_high": {"kind": "float", "low": 0.10, "high": 0.40, "step": 0.02},
    "entry.breakout_lookback":      {"kind": "int",   "low": 10,   "high": 60},
    "entry.breakout_vol_mult":      {"kind": "float", "low": 1.0,  "high": 2.0, "step": 0.1},
    "entry.max_new_per_day":        {"kind": "int",   "low": 1,    "high": 4},
    "exit.init_stop_atr_mult":      {"kind": "float", "low": 1.5,  "high": 4.0, "step": 0.25},
    "exit.trail_atr_mult":          {"kind": "float", "low": 2.0,  "high": 6.0, "step": 0.25},
    "exit.time_stop_bars":          {"kind": "int",   "low": 10,   "high": 45},
    "exit.momentum_exit_closes":    {"kind": "int",   "low": 0,    "high": 4},
    "risk.risk_per_trade":          {"kind": "float", "low": 0.005, "high": 0.020, "step": 0.001},
    "risk.max_positions":           {"kind": "int",   "low": 4,    "high": 10},
    "regime.risk_on_breadth":       {"kind": "float", "low": 0.35, "high": 0.60, "step": 0.05},
}


def write_default_space(path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(DEFAULT_SPACE, fh, indent=2, sort_keys=True)


def default_space() -> ParamSpace:
    specs = []
    for p, s in DEFAULT_SPACE.items():
        specs.append(ParamSpec(path=p, kind=s.get("kind", "float"),
                               low=float(s.get("low", 0)), high=float(s.get("high", 1)),
                               step=float(s.get("step", 0) or 0), choices=s.get("choices")))
    return ParamSpace(specs)
