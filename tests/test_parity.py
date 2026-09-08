"""Backtest and live planner must agree.

The classic way a systematic setup fails is that the live script and the
backtest slowly diverge until the thing being traded is not the thing that was
validated. Both import swingtrader.rules; these tests check they stay in step.
"""
import pytest

from swingtrader.rules import (close_exit_reason, initial_stop, ratchet,
                               trail_stop_level)


def test_stop_only_ever_ratchets_up():
    assert ratchet(100.0, 105.0) == 105.0
    assert ratchet(100.0, 95.0) == 100.0, "a stop must never be widened"
    assert ratchet(float("nan"), 95.0) == 95.0
    assert ratchet(100.0, float("nan")) == 100.0


def test_trail_tightens_in_risk_off(cfg):
    normal = trail_stop_level(100.0, 2.0, "risk_on", cfg)
    tight = trail_stop_level(100.0, 2.0, "risk_off", cfg)
    assert tight > normal, "risk-off must pull the stop closer"
    assert normal == pytest.approx(100.0 - cfg.get("exit.trail_atr_mult") * 2.0)


def test_initial_stop_is_clamped_into_an_atr_band(cfg):
    """A structure stop 0.2 ATR away is noise, not risk - and a 10 ATR one makes
    the position size meaningless. Both get clamped."""
    close, atr = 100.0, 2.0
    tight = initial_stop(close, atr, structure_low=99.9, cfg=cfg)
    wide = initial_stop(close, atr, structure_low=50.0, cfg=cfg)
    min_d = cfg.get("exit.min_stop_atr_mult") * atr
    max_d = cfg.get("exit.max_stop_atr_mult") * atr
    assert close - tight == pytest.approx(min_d)
    assert close - wide == pytest.approx(max_d)


def test_time_stop_only_fires_on_a_stale_trade(cfg):
    bars = cfg.get("exit.time_stop_bars")
    assert close_exit_reason(cfg, bars, -0.3, 0, False) == "time_stop"
    assert close_exit_reason(cfg, bars, 1.5, 0, True) is None, "a winner must be left alone"
    assert close_exit_reason(cfg, bars - 1, -0.3, 0, False) is None


def test_momentum_exit_is_off_by_default_but_works_when_enabled(cfg):
    assert close_exit_reason(cfg, 5, 1.0, 9, True) is None
    c = cfg.with_overrides({"exit.momentum_exit_closes": 2})
    assert close_exit_reason(c, 5, 1.0, 2, True) == "momentum_lost"
    assert close_exit_reason(c, 5, -1.0, 2, False) is None, "profit-only by default"


def test_live_replay_finds_the_same_stop_exit_the_engine_did(cfg, dataset):
    """End-to-end parity, and the regression test for a real bug.

    The live planner used to compute the trailing stop in one shot from the
    running high. For a position that ran up and then fell back, that produces a
    stop ABOVE the current price - a level that would have fired days earlier -
    and the sheet printed it as a live stop instead of telling you the book was
    out of sync. It now replays the trail bar by bar, exactly as the engine
    does, so both must identify the same exit date.
    """
    from swingtrader.live import LivePlanner, LivePosition
    from swingtrader.runtime import run_backtest

    res = run_backtest(cfg, dataset, start="2016-01-01")
    stopped = [t for t in res.trades if t.exit_reason.startswith("stop_")]
    assert stopped, "no stop exits in the run - cannot test parity"

    feats = dataset.features(cfg)
    rm = dataset.regime(cfg)
    planner = LivePlanner(cfg, feats, rm)

    checked = 0
    for t in stopped[:12]:
        f = feats[t.symbol]
        entry_i = f.series.pos(t.entry_date)
        exit_i = f.series.pos(t.exit_date)
        if entry_i is None or exit_i is None:
            continue
        lp = LivePosition(symbol=t.symbol, sector=t.sector, qty=t.qty,
                          entry_price=t.entry_price, entry_date=t.entry_date,
                          stop=t.initial_stop, initial_stop=t.initial_stop,
                          risk_per_share=t.risk_per_share)
        _, bars, breach = planner._replay_stop(
            f, lp, entry_i, exit_i, rm.at(t.exit_date).label)
        assert breach is not None, (
            f"{t.symbol}: engine stopped out on {t.exit_date} but the live replay "
            f"sees no breach - the two have diverged")
        assert breach[0] == t.exit_date, (
            f"{t.symbol}: engine exited {t.exit_date}, live replay says {breach[0]}")
        assert bars == exit_i - entry_i
        checked += 1
    assert checked >= 3, f"only checked {checked} trades"


def test_live_replay_never_reports_a_stop_above_the_last_price(cfg, dataset):
    """A stop above the current price is by definition already triggered. If the
    planner ever prints one as live, the sheet is telling you to hold something
    your broker has already sold."""
    from swingtrader.live import LivePlanner, LivePosition
    from swingtrader.runtime import run_backtest

    res = run_backtest(cfg, dataset, start="2016-01-01")
    feats = dataset.features(cfg)
    rm = dataset.regime(cfg)
    planner = LivePlanner(cfg, feats, rm)
    as_of = res.dates[-1]

    checked = 0
    for t in res.trades[:40]:
        f = feats[t.symbol]
        entry_i, i = f.series.pos(t.entry_date), f.series.pos(as_of)
        if entry_i is None or i is None or i <= entry_i:
            continue
        lp = LivePosition(symbol=t.symbol, sector=t.sector, qty=t.qty,
                          entry_price=t.entry_price, entry_date=t.entry_date,
                          stop=t.initial_stop, initial_stop=t.initial_stop,
                          risk_per_share=t.risk_per_share)
        stop, _, breach = planner._replay_stop(f, lp, entry_i, i, rm.at(as_of).label)
        if breach is None:
            assert stop <= f.series.close[i] * 1.0001, (
                f"{t.symbol}: replay reports a live stop {stop:.2f} above the last "
                f"close {f.series.close[i]:.2f} without flagging a breach")
        checked += 1
    assert checked >= 5
