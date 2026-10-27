"""The live executor must trade what the backtest measured.

Runs the production executor and daemon over 46 replayed sessions and the
backtest engine over the same sessions, then compares trade by trade. This is
the test that caught the stop re-anchoring bug: the live system was quietly
placing every initial stop at the plan's level instead of re-anchoring it to the
price actually paid, which changes each trade's risk without changing anything
you would notice by looking at the order sheet.
"""
import pytest

from swingtrader.realtime.parity import compare


@pytest.mark.slow
def test_live_executor_matches_the_backtest(cfg, dataset, tmp_path):
    r = compare(cfg, dataset, 700, 745, str(tmp_path))
    assert r["backtest_entries"] >= 3, "window too quiet to mean anything"
    assert r["only_live"] == [], f"live took trades the backtest did not: {r['only_live']}"
    assert r["only_backtest"] == [], f"live missed trades: {r['only_backtest']}"
    assert r["same_exit_date"] == r["closed_in_both"]
    assert r["max_initial_stop_gap"] < 0.002, (
        f"initial stops differ by up to {100*r['max_initial_stop_gap']:.2f}% - the stop is "
        f"not being re-anchored to the fill the way the backtest does it")
    assert r["equity_live"] == pytest.approx(r["equity_backtest"], rel=0.005)
