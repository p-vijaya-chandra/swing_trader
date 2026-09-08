"""The self-improvement machinery: robust selection, the promotion gate, the
decay monitor, and the meta-labeller.

The common theme is refusing to be fooled by noise. Each test plants a known
signal (or a known absence of one) and checks the machinery reaches the right
conclusion."""
import math
import random
from dataclasses import dataclass

import pytest

from swingtrader.config import Config
from swingtrader.learn.journal import (attribution_suggestions, edge_decay_check,
                                       feature_attribution)
from swingtrader.learn.metalabel import MetaLabeler
from swingtrader.learn.promote import Champion, ChampionStore, evaluate_promotion
from swingtrader.learn.search import default_space, random_search, robust_pick
from swingtrader.learn.walkforward import make_folds, objective_score


@dataclass
class FakeTrade:
    r_multiple: float


class RowTrade:
    def __init__(self, r, **f):
        self.r_multiple = r
        self._f = f

    def as_row(self):
        return dict(r_multiple=self.r_multiple,
                    **{("f_" + k): v for k, v in self._f.items()})


# ------------------------------------------------------------------- folds
def test_folds_leave_an_embargo_gap():
    import datetime as dt
    dates = [(dt.date(2015, 1, 1) + dt.timedelta(days=i)).isoformat()
             for i in range(3800)
             if (dt.date(2015, 1, 1) + dt.timedelta(days=i)).weekday() < 5]
    folds = make_folds(dates, train_years=4, test_months=6, embargo_days=10)
    assert len(folds) >= 3
    for f in folds:
        gap = (dt.date.fromisoformat(f.test_start)
               - dt.date.fromisoformat(f.train_end)).days
        assert gap == 10, "a trade open across the split would leak train into test"
        assert f.test_start > f.train_end


def test_runt_final_fold_is_dropped():
    import datetime as dt
    dates = [(dt.date(2015, 1, 1) + dt.timedelta(days=i)).isoformat()
             for i in range(2000)
             if (dt.date(2015, 1, 1) + dt.timedelta(days=i)).weekday() < 5]
    folds = make_folds(dates, 4, 6, 10)
    for f in folds:
        span = (dt.date.fromisoformat(f.test_end)
                - dt.date.fromisoformat(f.test_start)).days
        assert span > 0.6 * 6 * 30, "a runt window would distort the median"


# ------------------------------------------------------------- robust pick
def test_robust_pick_rejects_a_lucky_spike():
    """The single best result in a search is usually the luckiest. Scoring by
    the median of a candidate's neighbours picks the centre of a good REGION
    instead - which is the difference between a walk-forward that degrades
    gracefully and one that falls off a cliff."""
    space = default_space()

    def obj(cfg):
        x = cfg.get("exit.trail_atr_mult")
        y = cfg.get("exit.init_stop_atr_mult")
        hill = 2.0 - ((x - 4.0) ** 2 + (y - 2.5) ** 2) / 4.0
        spike = 6.0 if (abs(x - 6.0) < 0.13 and abs(y - 1.5) < 0.13) else 0.0
        return hill + spike

    res = random_search(Config(), space, obj, n_samples=500, seed=5)
    peak = max(res, key=lambda r: r.score)
    rob = robust_pick(res, space, k=7)

    def on_spike(p):
        return (abs(p["exit.trail_atr_mult"] - 6.0) < 0.13
                and abs(p["exit.init_stop_atr_mult"] - 1.5) < 0.13)

    assert on_spike(peak.params), "fixture broken - the peak should be the spike"
    assert not on_spike(rob.params), "robust pick fell for the overfit spike"
    assert rob.params["exit.trail_atr_mult"] == pytest.approx(4.0, abs=1.0)


def test_objective_penalises_turnover():
    base = {"n_trades": 100, "cagr": 0.20, "max_drawdown": -0.20, "years": 5.0,
            "start_equity": 100000.0, "costs_total": 0.0}
    churny = dict(base, costs_total=50000.0)
    assert objective_score(base) > objective_score(churny)


def test_objective_rejects_thin_samples():
    m = {"n_trades": 3, "cagr": 5.0, "max_drawdown": -0.01, "years": 1.0,
         "start_equity": 100000.0, "costs_total": 0.0}
    assert objective_score(m, min_trades=10) == float("-inf")


# ------------------------------------------------------------ promotion gate
def test_gate_rejects_a_noise_edge():
    """20 independent pure-noise challengers; a 90% CI should let through only
    a small fraction. Anything much higher means the gate is not gating."""
    cfg = Config()
    promoted = 0
    for seed in range(20):
        rng = random.Random(500 + seed)
        noise = [FakeTrade(rng.gauss(0.0, 1.2)) for _ in range(120)]
        d = evaluate_promotion(cfg, None, 1.2, {"max_drawdown": -0.18}, noise)
        promoted += d.promoted
    assert promoted <= 4, f"{promoted}/20 pure-noise configs promoted"


def test_gate_accepts_a_real_edge():
    cfg = Config()
    rng = random.Random(1)
    good = [FakeTrade(rng.gauss(0.4, 1.1)) for _ in range(150)]
    d = evaluate_promotion(cfg, None, 1.2,
                           {"max_drawdown": -0.18, "expectancy_r": 0.4}, good)
    assert d.promoted


def test_gate_refuses_to_buy_return_with_drawdown():
    cfg = Config()
    rng = random.Random(2)
    good = [FakeTrade(rng.gauss(0.4, 1.1)) for _ in range(150)]
    champ = Champion(1, "", {}, "calmar_turnover", 1.0,
                     {"max_drawdown": -0.18, "expectancy_r": 0.35})
    d = evaluate_promotion(cfg, champ, 1.5,
                           {"max_drawdown": -0.35, "expectancy_r": 0.35}, good)
    assert not d.promoted and "drawdown" in d.reasons[0]


def test_gate_requires_a_minimum_sample():
    cfg = Config()
    rng = random.Random(3)
    good = [FakeTrade(rng.gauss(0.6, 1.0)) for _ in range(20)]
    d = evaluate_promotion(cfg, None, 1.2, {"max_drawdown": -0.10}, good)
    assert not d.promoted


def test_champion_store_round_trip(tmp_path):
    store = ChampionStore(str(tmp_path))
    assert store.load() is None
    c1 = store.promote({"exit.trail_atr_mult": 3.5}, "calmar_turnover", 1.1, {})
    assert c1.version == 1
    c2 = store.promote({"exit.trail_atr_mult": 4.0}, "calmar_turnover", 1.4, {})
    assert c2.version == 2
    assert store.load().params["exit.trail_atr_mult"] == 4.0
    store.audit({"promoted": True, "score": 1.4})
    assert store.history()


def test_champion_overlays_onto_a_config(tmp_path):
    store = ChampionStore(str(tmp_path))
    store.promote({"risk.max_positions": 9}, "o", 1.0, {})
    assert store.apply(Config()).get("risk.max_positions") == 9


# --------------------------------------------------------------- decay check
def test_decay_monitor_fires_only_on_a_real_break():
    rng = random.Random(4)
    base = [FakeTrade(rng.gauss(0.35, 1.3)) for _ in range(400)]
    healthy = edge_decay_check([FakeTrade(rng.gauss(0.35, 1.3)) for _ in range(60)], base)
    broken = edge_decay_check([FakeTrade(rng.gauss(-0.6, 1.3)) for _ in range(60)], base)
    assert not healthy.alert
    assert broken.alert and broken.action == "halve_risk"


def test_decay_monitor_abstains_on_thin_data():
    rng = random.Random(5)
    base = [FakeTrade(rng.gauss(0.3, 1.2)) for _ in range(400)]
    rep = edge_decay_check([FakeTrade(-2.0) for _ in range(4)], base)
    assert not rep.alert and "not enough" in rep.message


# -------------------------------------------------------------- attribution
def test_attribution_finds_a_planted_relationship_and_ignores_noise():
    rng = random.Random(6)
    trades = []
    for _ in range(400):
        a = rng.uniform(0.01, 0.09)
        trades.append(RowTrade(rng.gauss(0.7 - 14 * a, 1.2),
                               atr_pct=a, noise=rng.random()))
    attr = feature_attribution(trades)
    sug = " ".join(attribution_suggestions(attr))
    assert "atr_pct" in sug
    assert "monotonic - worth testing" in sug
    assert abs(attr["f_atr_pct"]["spread_r"]) > abs(attr["f_noise"]["spread_r"])


def test_attribution_ignores_absolute_price_levels():
    """'Trades where the price was above Rs 2,700 did better' is a statement
    about which stocks were expensive, not a condition you can screen on."""
    rng = random.Random(7)
    trades = [RowTrade(rng.gauss(0.2, 1.0), close=rng.uniform(100, 5000),
                       ema_fast=rng.uniform(100, 5000), atr_pct=rng.uniform(0.01, 0.09))
              for _ in range(300)]
    attr = feature_attribution(trades)
    assert "f_close" not in attr and "f_ema_fast" not in attr
    assert "f_atr_pct" in attr


# -------------------------------------------------------------- meta-labeller
def test_metalabeller_recovers_signal_and_lifts_out_of_sample_r():
    rng = random.Random(9)

    def make(n):
        out = []
        for _ in range(n):
            atr = rng.uniform(0.01, 0.09)
            adx = rng.uniform(10, 45)
            p = 1 / (1 + math.exp(-(-1.0 - 25 * (atr - 0.045) + 0.06 * (adx - 25))))
            win = rng.random() < p
            out.append(RowTrade(abs(rng.gauss(2, 1)) if win else -abs(rng.gauss(1, 0.3)),
                                atr_pct=atr, adx=adx, vol_ratio=rng.random()))
        return out

    train, test = make(600), make(600)
    m = MetaLabeler()
    assert m.fit(train, features=["f_atr_pct", "f_adx", "f_vol_ratio"], min_trades=150)

    coefs = dict(m.coefficients())
    assert coefs["f_adx"] > 0 and coefs["f_atr_pct"] < 0, "signs are wrong"
    assert abs(coefs["f_vol_ratio"]) < min(abs(coefs["f_adx"]), abs(coefs["f_atr_pct"]))

    def p_of(t):
        r = t.as_row()
        return m.predict_snapshot({"atr_pct": r["f_atr_pct"], "adx": r["f_adx"],
                                   "vol_ratio": r["f_vol_ratio"]})

    kept = [t.r_multiple for t in test if p_of(t) >= 0.55]
    every = [t.r_multiple for t in test]
    assert len(kept) > 20
    assert sum(kept) / len(kept) > sum(every) / len(every) + 0.2


def test_metalabeller_refuses_to_fit_on_a_thin_journal():
    rng = random.Random(10)
    few = [RowTrade(rng.gauss(0.2, 1.0), atr_pct=rng.random(), adx=rng.random())
           for _ in range(40)]
    assert MetaLabeler().fit(few, min_trades=150) is False


def test_metalabeller_abstains_rather_than_guessing():
    """A missing feature must not silently become a zero - abstain and let the
    rule layer decide alone."""
    rng = random.Random(11)
    trades = [RowTrade(rng.gauss(0.2, 1.0), atr_pct=rng.uniform(0.01, 0.09),
                       adx=rng.uniform(10, 45)) for _ in range(300)]
    m = MetaLabeler()
    assert m.fit(trades, features=["f_atr_pct", "f_adx"], min_trades=150)
    p = m.predict_snapshot({"atr_pct": 0.03})     # adx missing
    assert p != p, "should return NaN (abstain) when a feature is unavailable"
