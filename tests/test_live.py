"""The live layer: position book accounting and the order sheet."""
import json
import os

import pytest

from swingtrader.live import (DailyPlan, LivePlanner, LivePosition, PositionBook,
                              render_plan_markdown, write_plan_csv)


def test_position_book_round_trip(tmp_path, cfg):
    path = str(tmp_path / "positions.json")
    b = PositionBook(capital_base=100000, cash=100000)
    b.positions.append(LivePosition(symbol="INFY", sector="IT", qty=10,
                                    entry_price=1500.0, entry_date="2025-01-02",
                                    stop=1400.0, initial_stop=1400.0,
                                    risk_per_share=100.0))
    b.cash -= 15000
    b.save(path)
    loaded = PositionBook.load(path)
    assert loaded.cash == pytest.approx(85000)
    assert loaded.get("INFY").qty == 10
    assert loaded.get("NOPE") is None


def test_missing_book_starts_flat(tmp_path, cfg):
    b = PositionBook.load(str(tmp_path / "nothing.json"), capital=250000)
    assert b.cash == 250000 and not b.positions


def test_plan_orders_exits_before_entries(cfg, dataset):
    """Exits free the cash the entries need, so the sheet must be worked in that
    order - and it must SAY so, because a person is executing it by hand."""
    feats = dataset.features(cfg)
    rm = dataset.regime(cfg)
    as_of = dataset.all_dates()[-1]
    book = PositionBook(capital_base=100000, cash=100000)
    plan = LivePlanner(cfg, feats, rm).plan(book, as_of)
    md = render_plan_markdown(plan, cfg, dataset.names)
    assert md.index("## 1. Exits") < md.index("## 3. New entries")
    assert "frees the cash" in md


def test_plan_never_exceeds_position_or_sector_caps(cfg, dataset):
    feats = dataset.features(cfg)
    rm = dataset.regime(cfg)
    book = PositionBook(capital_base=100000, cash=100000)
    for as_of in dataset.all_dates()[-120::20]:
        plan = LivePlanner(cfg, feats, rm).plan(book, as_of)
        buys = [o for o in plan.orders if o.action == "BUY"]
        assert len(buys) <= cfg.get("entry.max_new_per_day")
        assert len(buys) <= cfg.get("risk.max_positions")
        sectors = {}
        for o in buys:
            sec = next((w["sector"] for w in plan.watchlist if w["symbol"] == o.symbol),
                       "Other")
            sectors[sec] = sectors.get(sec, 0) + 1
        assert all(v <= cfg.get("risk.max_sector_positions") for v in sectors.values())


def test_plan_sizes_each_entry_to_the_risk_budget(cfg, dataset):
    feats = dataset.features(cfg)
    rm = dataset.regime(cfg)
    book = PositionBook(capital_base=100000, cash=100000)
    budget = 100000 * cfg.get("risk.risk_per_trade")
    found = 0
    for as_of in dataset.all_dates()[-400::10]:
        plan = LivePlanner(cfg, feats, rm).plan(book, as_of)
        for o in (x for x in plan.orders if x.action == "BUY"):
            assert o.risk <= budget * 1.02, f"{o.symbol} risks {o.risk:.0f} > {budget:.0f}"
            assert o.qty > 0 and o.value >= cfg.get("risk.min_position_value")
            found += 1
    assert found > 0, "no entries generated across the sample - test is vacuous"


def test_every_buy_is_paired_with_a_stop(cfg, dataset):
    """A position without a stop is the only genuinely unbounded risk here."""
    feats = dataset.features(cfg)
    rm = dataset.regime(cfg)
    book = PositionBook(capital_base=100000, cash=100000)
    for as_of in dataset.all_dates()[-400::10]:
        plan = LivePlanner(cfg, feats, rm).plan(book, as_of)
        buys = {o.symbol for o in plan.orders if o.action == "BUY"}
        stops = {o.symbol for o in plan.orders if o.action == "PLACE_STOP"}
        assert buys == stops, f"unstopped buys on {as_of}: {buys - stops}"


def test_risk_off_regime_produces_no_buys(cfg, dataset):
    feats = dataset.features(cfg)
    rm = dataset.regime(cfg)
    book = PositionBook(capital_base=100000, cash=100000)
    checked = 0
    for as_of in dataset.all_dates():
        if rm.at(as_of).label != "risk_off":
            continue
        plan = LivePlanner(cfg, feats, rm).plan(book, as_of)
        assert not [o for o in plan.orders if o.action == "BUY"]
        assert any("RISK-OFF" in n for n in plan.notes)
        checked += 1
        if checked >= 5:
            break
    assert checked >= 1, "no risk-off sessions in the fixture"


def test_halted_book_produces_no_buys(cfg, dataset):
    feats = dataset.features(cfg)
    rm = dataset.regime(cfg)
    book = PositionBook(capital_base=100000, cash=100000, halted=True)
    plan = LivePlanner(cfg, feats, rm).plan(book, dataset.all_dates()[-1])
    assert not [o for o in plan.orders if o.action == "BUY"]
    assert any("CIRCUIT BREAKER" in n for n in plan.notes)


def test_stale_book_is_flagged_not_silently_repriced(cfg, dataset):
    """Regression: a position left unmanaged long enough that its trailing stop
    already fired must be reported as out of sync, not printed with an
    impossible stop above the last traded price."""
    feats = dataset.features(cfg)
    rm = dataset.regime(cfg)
    dates = dataset.all_dates()
    entry_date = dates[-120]
    sym = next(s for s in sorted(feats) if feats[s].series.pos(entry_date) is not None)
    f = feats[sym]
    i = f.series.pos(entry_date)
    entry = f.series.close[i]
    book = PositionBook(capital_base=100000, cash=90000)
    book.positions.append(LivePosition(
        symbol=sym, sector=f.sector, qty=5, entry_price=entry,
        entry_date=entry_date, stop=entry * 0.90, initial_stop=entry * 0.90,
        risk_per_share=entry * 0.10))
    plan = LivePlanner(cfg, feats, rm).plan(book, dates[-1])
    h = next(x for x in plan.holdings if x["symbol"] == sym)
    if h["action"] == "stop_breached":
        assert any("already flat" in n for n in plan.notes)
    else:
        assert h["stop"] <= h["last"] * 1.0001, (
            "a live stop above the last price would already have triggered")


def test_plan_csv_is_written_and_parseable(tmp_path, cfg, dataset):
    import csv
    feats = dataset.features(cfg)
    rm = dataset.regime(cfg)
    as_of = dataset.all_dates()[-1]
    plan = LivePlanner(cfg, feats, rm).plan(
        PositionBook(capital_base=100000, cash=100000), as_of)
    p = str(tmp_path / "plan.csv")
    write_plan_csv(plan, p)
    with open(p) as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == len(plan.orders)
    for r in rows:
        assert r["as_of"] == as_of
        assert r["action"] in ("BUY", "SELL_EXIT", "UPDATE_STOP", "PLACE_STOP")
