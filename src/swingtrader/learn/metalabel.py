"""Meta-labelling: a learned second opinion on entries the rules already like.

The strategy proposes; this model vetoes. It never picks a stock, never sizes a
position and never overrides an exit. It answers one narrow question about a
signal the rules have ALREADY generated: given the conditions at entry, how
often did trades like this end profitably?

Why so narrow, and why logistic regression rather than something fancier:

  * The rule layer stays readable. If the model is switched off you still have a
    complete, inspectable strategy - not a hole where one used to be.
  * ~150-600 trades is a tiny sample. A gradient-boosted forest on 20 features
    and 300 rows will fit the noise perfectly and tell you so with a beautiful
    in-sample AUC. A regularised linear model on standardised features cannot
    hide as much, and its coefficients are readable as "this feature helps".
  * It is trained ONLY on trades that closed before the evaluation window, so
    turning it on cannot retroactively improve a backtest.

It is OFF by default. Turn it on when the journal has real trades in it, and
only if `swing learn --metalabel` shows it beating the unfiltered system out of
sample.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..util import NA, is_na, mean, stdev

DEFAULT_FEATURES = [
    "f_atr_pct", "f_adx", "f_mom", "f_roc_63", "f_roc_126", "f_rs_index",
    "f_pct_below_52w", "f_dist_ema_atr", "f_vol_ratio", "f_rsi_14",
    "f_ema_slow_slope", "entry_score", "entry_rank",
]


@dataclass
class MetaLabeler:
    features: List[str] = field(default_factory=list)
    mu: List[float] = field(default_factory=list)
    sd: List[float] = field(default_factory=list)
    weights: List[float] = field(default_factory=list)
    bias: float = 0.0
    n_train: int = 0
    train_accuracy: float = NA
    base_rate: float = NA

    # ------------------------------------------------------------------ design
    @staticmethod
    def _rows(trades: Sequence) -> List[Dict[str, Any]]:
        return [t.as_row() if hasattr(t, "as_row") else dict(t) for t in trades]

    def _design(self, rows: Sequence[Dict[str, Any]]) -> Tuple[List[List[float]], List[float]]:
        X, y = [], []
        for r in rows:
            vec = []
            ok = True
            for f in self.features:
                v = r.get(f)
                if not isinstance(v, (int, float)) or is_na(v):
                    ok = False
                    break
                vec.append(float(v))
            if not ok:
                continue
            label = r.get("r_multiple")
            if not isinstance(label, (int, float)) or is_na(label):
                continue
            X.append(vec)
            y.append(1.0 if label > 0 else 0.0)
        return X, y

    # -------------------------------------------------------------------- fit
    def fit(self, trades: Sequence, features: Optional[Sequence[str]] = None,
            l2: float = 1.0, epochs: int = 400, lr: float = 0.08,
            min_trades: int = 150) -> bool:
        rows = self._rows(trades)
        candidates = list(features) if features else list(DEFAULT_FEATURES)
        # keep only features actually present and varying
        usable = []
        for f in candidates:
            vals = [r.get(f) for r in rows
                    if isinstance(r.get(f), (int, float)) and not is_na(r.get(f))]
            if len(vals) >= max(min_trades // 2, 20) and stdev(vals) not in (0.0, NA) \
                    and not is_na(stdev(vals)):
                usable.append(f)
        self.features = usable
        if not usable:
            return False

        X, y = self._design(rows)
        if len(X) < min_trades:
            self.n_train = len(X)
            return False

        d = len(self.features)
        self.mu = [mean([row[j] for row in X]) for j in range(d)]
        self.sd = [max(stdev([row[j] for row in X]), 1e-9) for j in range(d)]
        Z = [[(row[j] - self.mu[j]) / self.sd[j] for j in range(d)] for row in X]

        w = [0.0] * d
        b = 0.0
        n = len(Z)
        for _ in range(epochs):
            gw = [0.0] * d
            gb = 0.0
            for i in range(n):
                z = b + sum(w[j] * Z[i][j] for j in range(d))
                p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
                err = p - y[i]
                gb += err
                for j in range(d):
                    gw[j] += err * Z[i][j]
            b -= lr * gb / n
            for j in range(d):
                # L2 shrinks coefficients toward zero: with a few hundred rows
                # this is the whole defence against fitting noise.
                w[j] -= lr * (gw[j] / n + l2 * w[j] / n)

        self.weights, self.bias, self.n_train = w, b, n
        self.base_rate = mean(y)
        correct = 0
        for i in range(n):
            p = self.predict_vector(Z[i], standardised=True)
            correct += int((p >= 0.5) == (y[i] >= 0.5))
        self.train_accuracy = correct / n
        return True

    # ---------------------------------------------------------------- predict
    def predict_vector(self, vec: Sequence[float], standardised: bool = False) -> float:
        if not self.weights:
            return NA
        if standardised:
            z = self.bias + sum(self.weights[j] * vec[j] for j in range(len(vec)))
        else:
            z = self.bias
            for j, f in enumerate(self.features):
                z += self.weights[j] * (vec[j] - self.mu[j]) / self.sd[j]
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))

    def predict_snapshot(self, snapshot: Dict[str, float], extra: Optional[Dict[str, float]] = None) -> float:
        """Score a live candidate. Missing feature => abstain (return NaN), and
        the caller then lets the rule layer decide alone."""
        if not self.weights:
            return NA
        vec = []
        for f in self.features:
            key = f[2:] if f.startswith("f_") else f
            v = snapshot.get(key, (extra or {}).get(f))
            if v is None:
                v = (extra or {}).get(key)
            if not isinstance(v, (int, float)) or is_na(v):
                return NA
            vec.append(float(v))
        return self.predict_vector(vec)

    def as_entry_filter(self, threshold: float = 0.5):
        """Return an entry_filter(candidate, ctx) -> bool for the engine."""
        def _filter(cand, ctx) -> bool:
            snap = dict(cand.snapshot)
            p = self.predict_snapshot(snap, {"entry_score": cand.score,
                                             "entry_rank": float(cand.rank)})
            if is_na(p):
                return True          # abstain rather than block on missing data
            return p >= threshold
        return _filter

    def coefficients(self) -> List[Tuple[str, float]]:
        """Standardised coefficients, largest influence first."""
        return sorted(zip(self.features, self.weights), key=lambda t: -abs(t[1]))

    # ------------------------------------------------------------------- I/O
    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"features": self.features, "mu": self.mu, "sd": self.sd,
                       "weights": self.weights, "bias": self.bias,
                       "n_train": self.n_train, "train_accuracy": self.train_accuracy,
                       "base_rate": self.base_rate}, fh, indent=2)

    @classmethod
    def load(cls, path: str) -> Optional["MetaLabeler"]:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return cls(**d)
