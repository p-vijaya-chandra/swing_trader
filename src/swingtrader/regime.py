"""Market regime classifier.

This is the highest-value component in the whole system and it is worth being
blunt about why. On a long-only cash book you cannot short and you cannot hedge
cheaply. Essentially all of your catastrophic drawdown risk is "held a basket
of high-beta large caps through a market that fell 25%". Stock selection cannot
save you there; only refusing to hold can. Every serious drawdown reduction in
this system comes from this file, not from the entry logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from . import indicators as ind
from .config import Config
from .data.models import Series
from .features import Features
from .util import NA, is_na

RISK_ON, NEUTRAL, RISK_OFF = "risk_on", "neutral", "risk_off"


@dataclass
class RegimeState:
    date: str
    label: str
    exposure: float
    breadth: float
    index_above_long: bool
    index_above_short: bool
    index_atr_pct: float
    reason: str


class RegimeModel:
    """Classifies each session as risk_on / neutral / risk_off.

    Three inputs, deliberately few:
      1. Index above its long MA          - the primary bull/bear switch
      2. Index above its short MA         - intermediate trend
      3. Breadth: share of the universe above its own 50 DMA - confirms that
         the index move is broad rather than five mega-caps carrying a rotting
         market, which is a recurring Nifty failure mode.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ma_long = int(cfg.get("regime.ma_long", 200))
        self.ma_short = int(cfg.get("regime.ma_short", 50))
        self.breadth_ma = int(cfg.get("regime.breadth_ma", 50))
        self.on_breadth = float(cfg.get("regime.risk_on_breadth", 0.45))
        self.off_breadth = float(cfg.get("regime.risk_off_breadth", 0.30))
        self.exposure_map = cfg.get("regime.exposure", {})
        self.chop_atr_max = float(cfg.get("regime.chop_atr_pct_max", 0.03))
        self.states: Dict[str, RegimeState] = {}

    def build(self, index_series: Optional[Series], feats: Dict[str, Features],
              dates: List[str]) -> Dict[str, RegimeState]:
        """Precompute the regime for every session in `dates`."""
        if index_series is None or len(index_series) < self.ma_long + 5:
            for d in dates:
                self.states[d] = RegimeState(d, RISK_ON, 1.0, NA, True, True, NA,
                                             "no index data - regime filter disabled")
            return self.states

        c = index_series.close
        ma_l = ind.sma(c, self.ma_long)
        ma_s = ind.sma(c, self.ma_short)
        atr_i = ind.atr(index_series.high, index_series.low, c, 14)
        atr_pct = [NA if (is_na(atr_i[i]) or c[i] <= 0) else atr_i[i] / c[i]
                   for i in range(len(c))]

        # breadth: fraction of symbols trading above their own breadth MA
        above: Dict[str, List[int]] = {}
        for sym, f in feats.items():
            ema_b = ind.sma(f.series.close, self.breadth_ma)
            above[sym] = [1 if (not is_na(ema_b[i]) and f.series.close[i] > ema_b[i]) else 0
                          for i in range(len(f.series))]

        for d in dates:
            i = index_series.pos(d)
            if i is None or is_na(ma_l[i]):
                self.states[d] = RegimeState(d, NEUTRAL, self.exposure_map.get(NEUTRAL, 0.5),
                                             NA, False, False, NA, "insufficient index history")
                continue

            n_tot = n_up = 0
            for sym, f in feats.items():
                j = f.series.pos(d)
                if j is None or j < self.breadth_ma:
                    continue
                n_tot += 1
                n_up += above[sym][j]
            breadth = (n_up / n_tot) if n_tot >= 20 else NA

            above_long = c[i] > ma_l[i]
            above_short = (not is_na(ma_s[i])) and c[i] > ma_s[i]
            ap = atr_pct[i]

            if not above_long:
                label, reason = RISK_OFF, f"index below {self.ma_long}DMA"
            elif (not is_na(breadth)) and breadth < self.off_breadth:
                label, reason = RISK_OFF, f"breadth {breadth:.0%} below {self.off_breadth:.0%}"
            elif not above_short:
                label, reason = NEUTRAL, f"index below {self.ma_short}DMA"
            elif (not is_na(breadth)) and breadth < self.on_breadth:
                label, reason = NEUTRAL, f"breadth {breadth:.0%} under risk-on threshold"
            elif (not is_na(ap)) and ap > self.chop_atr_max:
                label, reason = NEUTRAL, f"index ATR {ap:.1%} above chop cap"
            else:
                label, reason = RISK_ON, "index above both MAs, breadth healthy"

            self.states[d] = RegimeState(
                d, label, float(self.exposure_map.get(label, 0.0)),
                breadth, above_long, above_short, ap, reason)
        return self.states

    def at(self, date: str) -> RegimeState:
        st = self.states.get(date)
        if st is None:
            return RegimeState(date, NEUTRAL, self.exposure_map.get(NEUTRAL, 0.5),
                               NA, False, False, NA, "unknown date")
        return st
