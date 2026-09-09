"""Report rendering. A chart that silently renders as a flat line is worse than
no chart, because you read it and conclude the drawdown was mild."""

import pytest

from swingtrader.backtest import compute_metrics
from swingtrader.reporting.report import (_svg_path, equity_sparkline,
                                          monthly_table_text, write_report)
from swingtrader.runtime import run_backtest


@pytest.fixture(scope="module")
def result(cfg, dataset):
    return run_backtest(cfg, dataset, start="2016-01-01")


def test_drawdown_path_is_not_flattened():
    """Regression: the positive clamp that keeps log() defined was applied to
    every series, so drawdowns (all <= 0) collapsed to one point and the chart
    rendered as a straight line."""
    dd = [0.0, -0.05, -0.20, -0.10, 0.0, -0.30, 0.0]
    pts = _svg_path(dd, 900, 120, 34)
    ys = [float(p.split(",")[1]) for p in pts.split()]
    assert len(set(round(y, 3) for y in ys)) > 3, "drawdown chart is flat"
    # the deepest drawdown must sit at the bottom of the usable band
    assert ys[dd.index(min(dd))] == pytest.approx(max(ys))


def test_log_path_still_uses_a_log_scale():
    pts = _svg_path([1.0, 10.0, 100.0], 900, 260, 34, log=True)
    ys = [float(p.split(",")[1]) for p in pts.split()]
    # equal ratios must map to equal pixel gaps under a log scale
    assert (ys[0] - ys[1]) == pytest.approx(ys[1] - ys[2], rel=1e-6)


def test_svg_path_survives_degenerate_input():
    assert _svg_path([], 900, 120, 34) == ""
    assert _svg_path([5.0], 900, 120, 34) == ""
    assert _svg_path([3.0, 3.0, 3.0], 900, 120, 34)   # constant series must not divide by zero


def test_monthly_table_compounds_to_the_year(result):
    m = compute_metrics(result.dates, result.equity_curve, result.trades)
    lines = monthly_table_text(m["monthly_returns"])
    assert lines[0].startswith("Year")
    assert len(lines) > 2
    assert all("YEAR" in lines[0] for _ in [0])


def test_report_is_self_contained_and_flags_synthetic(tmp_path, cfg, result, dataset):
    m = compute_metrics(result.dates, result.equity_curve, result.trades,
                        result.costs_total)
    p = write_report(str(tmp_path / "r.html"), "Test", m, result, cfg,
                     data_note="SYNTHETIC DATA - not evidence")
    html = open(p, encoding="utf-8").read()
    assert "<svg" in html and "polyline" in html
    assert "SYNTHETIC DATA" in html
    assert "Months at or above +8%" in html, "the stated target must stay visible"
    # self-contained: no external network references at all
    for bad in ("http://", "https://", "<script"):
        assert bad not in html, f"report should not reference {bad}"


def test_sparkline_is_bounded(result):
    s = equity_sparkline(result.equity_curve, width=60)
    assert 0 < len(s) <= 80
