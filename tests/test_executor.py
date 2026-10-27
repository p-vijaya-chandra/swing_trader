"""Live executor scenarios.

Each test stages a failure that really happens in production - a crash
between sending an order and recording it, a timeout whose outcome is unknown,
a stop that fires while you are cancelling it, a broker stop that never fires,
a position sold by hand in the app - and asserts the system does the safe
thing. They run the production executor and daemon against the paper exchange
under a simulated clock; only time and the exchange are simulated.
"""
import datetime as dt
import os
from types import SimpleNamespace

import pytest

from swingtrader.broker import (BUY, LIMIT, SELL, STOP_ACTIVE, BrokerError, OrderRequest,
                                make_ref_id, round_to_tick)
from swingtrader.live import DailyPlan, LivePosition, OrderLine, plan_to_json
from swingtrader.realtime import IST, build_executor, build_replay
from swingtrader.realtime.calendar import TradingCalendar
from swingtrader.realtime.daemon import Daemon


# ----------------------------------------------------------------- helpers
def find(ds, pred, start=900):
    """First (session index, symbol) whose bar on that session satisfies pred."""
    dates = ds.all_dates()
    for i in range(start, len(dates) - 1):
        for sym in sorted(ds.series):
            s = ds.series[sym]
            j = s.pos(dates[i])
            if j and pred(s, j):
                return i, sym
    raise AssertionError("no session matches the scenario")


def calm_open(s, j):
    """Opens within 1% of the prior close and does not sink 4% intraday."""
    return abs(s.open[j] / s.close[j - 1] - 1) < 0.01 and s.low[j] > s.open[j] * 0.96


def dips_then_recovers(s, j):
    """An up-day whose low (visited first on the replay path) is >= 4% below the open."""
    return s.close[j] >= s.open[j] and s.low[j] <= s.open[j] * 0.96


@pytest.fixture
def rig(cfg, dataset, tmp_path):
    def make(i, gtt=True):
        dates = dataset.all_dates()
        day = dt.date.fromisoformat(dates[i])
        state, orders = str(tmp_path / "state"), str(tmp_path / "orders")
        daemon, ex, broker, clock = build_replay(cfg, dataset, day, state, orders)
        broker.tick_stops = gtt
        return SimpleNamespace(daemon=daemon, ex=ex, broker=broker, clock=clock, day=day,
                               i=i, dates=dates, state=state, orders=orders, cfg=cfg,
                               ds=dataset)
    return make


def at(r, hh, mm, ss=0):
    r.clock.set(dt.datetime.combine(r.day, dt.time(hh, mm, ss), tzinfo=IST))


def bar(r, sym):
    s = r.ds.series[sym]
    return s, s.pos(r.dates[r.i])


def write_plan(r, buys=(), exits=(), as_of=None, exposure=1.0):
    as_of = as_of or r.dates[r.i - 1]
    orders = [OrderLine("SELL_EXIT", sym, 0, "MARKET", reason="time_stop") for sym in exits]
    for sym in buys:
        s = r.ds.series[sym]
        sc = s.close[s.pos(as_of)]
        orders.append(OrderLine(
            "BUY", sym, 10, "LIMIT", price=sc * 1.03, trigger=sc * 0.95,
            meta={"signal_close": sc, "stop_ref": sc * 0.95,
                  "sector": r.ds.sectors.get(sym, "Other"), "setup": "breakout",
                  "rank": 1, "score": 1.0, "snapshot": {"atr_pct": 0.02}}))
    plan_to_json(DailyPlan(as_of=as_of, regime="risk_on", regime_reason="test",
                           exposure_target=exposure, equity=100000.0, cash=100000.0,
                           halted=False, orders=orders),
                 os.path.join(r.orders, f"{as_of}.plan.json"))


def hold(r, sym, qty, entry, stop, days_ago=5):
    """A position the system opened earlier, present in both book and broker."""
    r.broker.state["holdings"][sym] = {"qty": qty, "avg": entry}
    r.broker.state["cash"] -= qty * entry
    r.broker._save()
    r.ex.book.positions.append(LivePosition(
        symbol=sym, sector=r.ds.sectors.get(sym, "Other"), qty=qty, entry_price=entry,
        entry_date=r.dates[r.i - days_ago], stop=stop, initial_stop=stop,
        risk_per_share=entry - stop))
    r.ex.book.cash -= qty * entry
    r.ex.save()


def start_day(r):
    at(r, 8, 50)
    r.ex.begin_session(r.day)
    r.ex.prep()
    at(r, 9, 15, 5)


def orders(r, sym, side=None):
    return [o for o in r.broker.orders_today() if o.symbol == sym and (side is None or o.side == side)]


def events(r, name, sym=None):
    return [e for e in r.ex.audit.read(r.day.isoformat())
            if e["event"] == name and (sym is None or e.get("symbol") == sym)]


def monitor_until(r, hh, mm):
    end = dt.datetime.combine(r.day, dt.time(hh, mm), tzinfo=IST)
    while r.clock.now() < end:
        r.clock.sleep(30)
        r.ex.monitor()


# ------------------------------------------------------------------ entries
def test_entry_fills_and_its_stop_is_reanchored_to_the_actual_price(rig, dataset):
    """Regression: a dict-merge ordering bug overwrote the re-anchored stop with
    the plan's (and the quantity with 0). The stop must sit the planned DISTANCE
    below the price actually traded, exactly as in the backtest engine."""
    i, sym = find(dataset, calm_open)
    r = rig(i)
    write_plan(r, buys=[sym])
    start_day(r)
    ltp = r.broker.quotes([sym])[sym].ltp
    r.ex.open_session()
    p = r.ex.book.get(sym)
    assert p is not None and p.stop_id, "filled and protected"
    sc = dataset.series[sym].close[dataset.series[sym].pos(r.dates[i - 1])]
    want = round_to_tick(ltp - (sc - sc * 0.95), 0.05, "up")
    assert p.stop == pytest.approx(want, abs=1e-6)
    assert r.broker.get_stop(r.ex._stop_obj(p)).state == STOP_ACTIVE
    rec = next(iter(r.ex.st["entries"].values()))
    assert rec["qty"] == p.qty > 0


def test_gap_up_beyond_the_limit_is_skipped(rig, dataset):
    i, sym = find(dataset, lambda s, j: s.open[j] / s.close[j - 1] - 1 > 0.035)
    r = rig(i)
    write_plan(r, buys=[sym])
    start_day(r)
    r.ex.open_session()
    assert not orders(r, sym)
    assert events(r, "entry_skipped", sym)[0]["why"] == "gap_too_big"


def test_cash_constrained_entry_shrinks_instead_of_being_rejected(rig, dataset):
    """Brokers block funds at the limit price plus charges. Sizing against the
    last price produced rejections - trades the backtest took went missing."""
    i, sym = find(dataset, calm_open)
    r = rig(i)
    r.broker.state["cash"] = 15000.0
    r.broker._save()
    r.ex.book.cash = 15000.0
    write_plan(r, buys=[sym])
    start_day(r)
    r.ex.open_session()
    buys = orders(r, sym, BUY)
    assert len(buys) == 1 and buys[0].state == "FILLED"
    assert buys[0].filled_qty * buys[0].avg_price <= 15000.0


# --------------------------------------------------------- crash & ambiguity
def test_restart_during_the_open_does_not_duplicate_orders(rig, dataset, cfg):
    i, sym = find(dataset, calm_open)
    r = rig(i)
    write_plan(r, buys=[sym])
    start_day(r)
    r.ex.open_session()
    # the process dies before "open" is marked done; a fresh one starts from disk
    ex2 = build_executor(cfg, r.broker, r.clock, r.ex.market, r.state, r.orders,
                         universe=dataset.series.keys(), echo=False)
    ex2.begin_session(r.day)
    ex2.open_session()
    assert len(orders(r, sym, BUY)) == 1
    assert sum(1 for p in ex2.book.positions if p.symbol == sym) == 1


def test_order_sent_just_before_a_crash_is_adopted_not_resent(rig, dataset):
    """Write-ahead: the intent hit disk, the order reached the broker, and the
    process died before recording the order id. The restart must find it by
    reference, not send a second one."""
    i, sym = find(dataset, calm_open)
    r = rig(i)
    write_plan(r, buys=[sym])
    start_day(r)
    s, j = bar(r, sym)
    sc = s.close[j - 1]
    ref = make_ref_id(r.day.isoformat(), sym, "entry")
    limit = round_to_tick(sc * 1.02, 0.05, "down")
    r.broker.place_order(OrderRequest(sym, BUY, 20, LIMIT, ref, price=limit))
    r.ex.st["entries"][ref] = {
        "symbol": sym, "qty": 20, "ref_px": sc, "stop": sc * 0.95, "stop_dist": sc * 0.05,
        "limit": limit, "state": "placing", "order_id": "", "placed_ts": r.clock.epoch(),
        "sector": "Other", "setup": "", "rank": 1, "score": 1.0, "snapshot": {},
        "signal_close": sc, "stop_ref": sc * 0.95}
    r.ex.save()
    r.ex.open_session()
    r.ex.monitor()
    assert len(orders(r, sym, BUY)) == 1
    assert r.ex.book.get(sym).qty == 20 and r.ex.book.get(sym).stop_id


def test_ambiguous_placement_is_resolved_by_reference(rig, dataset, monkeypatch):
    """A timeout on a write means 'unknown', never 'failed'."""
    i, sym = find(dataset, calm_open)
    r = rig(i)
    write_plan(r, buys=[sym])
    start_day(r)
    real = r.broker.place_order

    def lands_then_times_out(req):
        real(req)
        raise BrokerError("read timed out", ambiguous=True)
    monkeypatch.setattr(r.broker, "place_order", lands_then_times_out)
    r.ex.open_session()
    assert len(orders(r, sym, BUY)) == 1
    assert r.ex.book.get(sym) is not None


# ------------------------------------------------------------------- stops
def test_exit_does_not_double_sell_when_the_stop_already_fired(rig, dataset):
    """The race behind most double-sells: the plan says exit at the open, but
    the broker's stop fires first. The cancel fails, so no second sell goes out."""
    i, sym = find(dataset, lambda s, j: True)
    r = rig(i)
    s, j = bar(r, sym)
    hold(r, sym, 20, entry=s.open[j] * 1.10, stop=s.open[j] * 1.02)   # opens below the stop
    write_plan(r, exits=[sym])
    at(r, 8, 50)
    r.ex.begin_session(r.day)
    r.ex.prep()
    at(r, 9, 15, 1)
    r.broker.tick()                      # the broker's GTT fires at the open
    at(r, 9, 15, 5)
    r.ex.open_session()
    assert len(orders(r, sym, SELL)) == 1, "exactly one sell: the stop's own"
    assert r.ex.book.get(sym) is None
    assert events(r, "exit_aborted_stop_live", sym)
    assert events(r, "position_closed", sym)[0]["reason"] == "stop"


def test_stop_that_fires_at_the_open_is_booked_not_re_protected(rig, dataset):
    """Regression, found by replay: a stop that fired at the open looks, to a
    naive "is there an active stop?" check, exactly like a missing one. The
    executor then tried to place a new stop for shares already sold - a false
    UNPROTECTED alarm. Fired stops must be booked before anything is re-placed."""
    i, sym = find(dataset, lambda s, j: True)
    r = rig(i)
    s, j = bar(r, sym)
    hold(r, sym, 20, entry=s.open[j] * 1.10, stop=s.open[j] * 1.02)
    write_plan(r)                         # nothing planned for this symbol
    at(r, 8, 50)
    r.ex.begin_session(r.day)
    r.ex.prep()
    at(r, 9, 15, 1)
    r.broker.tick()                      # gaps through the stop at the open
    at(r, 9, 15, 5)
    r.ex.open_session()                  # ensure_stops runs here
    assert r.ex.book.get(sym) is None
    assert events(r, "position_closed", sym)[0]["reason"] == "stop"
    assert not events(r, "stop_failed", sym)
    assert not any("UNPROTECTED" in a for a in r.ex.notify.sent)


def test_watchdog_exits_when_the_broker_stop_never_fires(rig, dataset):
    i, sym = find(dataset, dips_then_recovers)
    r = rig(i, gtt=False)                # broker accepts the GTT but never fires it
    s, j = bar(r, sym)
    hold(r, sym, 20, entry=s.open[j] * 1.02, stop=s.open[j] * 0.98)
    write_plan(r)
    start_day(r)
    r.ex.open_session()
    monitor_until(r, 12, 30)
    assert r.ex.book.get(sym) is None
    assert len(orders(r, sym, SELL)) == 1
    assert events(r, "position_closed", sym)[0]["reason"] == "stop_watchdog"
    assert any("has not fired" in a for a in r.ex.notify.sent)


def test_position_without_any_broker_stop_is_exited_on_breach(rig, dataset, monkeypatch):
    i, sym = find(dataset, dips_then_recovers)
    r = rig(i)
    s, j = bar(r, sym)

    def refused(*a, **k):
        raise BrokerError("smart orders unavailable")
    monkeypatch.setattr(r.broker, "place_stop", refused)
    hold(r, sym, 20, entry=s.open[j] * 1.02, stop=s.open[j] * 0.98)
    write_plan(r)
    start_day(r)
    assert any("UNPROTECTED" in a for a in r.ex.notify.sent)
    r.ex.open_session()
    monitor_until(r, 12, 30)
    assert r.ex.book.get(sym) is None
    assert events(r, "position_closed", sym)[0]["reason"] == "stop_unprotected"


def test_trailing_stops_only_ever_move_up(rig, dataset):
    i, sym = find(dataset, calm_open)
    r = rig(i)
    s, j = bar(r, sym)
    hold(r, sym, 20, entry=s.open[j], stop=round_to_tick(s.open[j] * 0.95, 0.05, "up"))
    start_day(r)
    st = r.broker.get_stop(r.ex._stop_obj(r.ex.book.get(sym)))
    p = r.ex.book.get(sym)
    p.stop = st.trigger * 0.9           # a lower stop must never be pushed to the broker
    r.ex.ensure_stops()
    assert r.broker.get_stop(r.ex._stop_obj(p)).trigger == st.trigger
    assert p.stop == st.trigger          # the book adopts the higher broker-side level


# ---------------------------------------------------- reconciliation & safety
def test_position_sold_by_hand_is_reconciled_without_new_orders(rig, dataset):
    i, sym = find(dataset, calm_open)
    r = rig(i)
    s, j = bar(r, sym)
    hold(r, sym, 20, entry=s.close[j - 1], stop=s.close[j - 1] * 0.9)
    r.broker.state["holdings"][sym]["qty"] = 0          # sold in the Groww app
    r.broker._save()
    start_day(r)
    assert r.ex.book.get(sym) is None
    assert events(r, "position_closed", sym)[0]["reason"] == "closed_externally"
    assert not orders(r, sym)


def test_unexplained_quantity_mismatch_blocks_new_entries(rig, dataset):
    i, a = find(dataset, calm_open)
    _, b = find(dataset, lambda s, j: calm_open(s, j) and s.symbol != a, start=i)
    r = rig(i)
    s, j = bar(r, a)
    hold(r, a, 20, entry=s.close[j - 1], stop=s.close[j - 1] * 0.9)
    r.broker.state["holdings"][a]["qty"] = 12            # partly sold by hand
    r.broker._save()
    write_plan(r, buys=[b])
    start_day(r)
    assert "mismatch" in r.ex.st["blocked"]
    r.ex.open_session()
    assert not orders(r, b, BUY)
    assert any("mismatch" in a for a in r.ex.notify.sent)


def test_your_own_holdings_are_never_touched(rig, dataset):
    i, sym = find(dataset, calm_open)
    r = rig(i)
    r.broker.state["holdings"][sym] = {"qty": 50, "avg": 100.0}   # a long-term holding
    r.broker._save()
    write_plan(r, buys=[sym])
    start_day(r)
    r.ex.open_session()
    assert not orders(r, sym)
    assert not [st for st in r.broker.stops() if st.symbol == sym]
    assert r.ex.st["foreign"][sym] == 50


def test_kill_switch_blocks_entries_but_keeps_stops(rig, dataset):
    i, a = find(dataset, calm_open)
    _, b = find(dataset, lambda s, j: calm_open(s, j) and s.symbol != a, start=i)
    r = rig(i)
    s, j = bar(r, a)
    hold(r, a, 20, entry=s.close[j - 1], stop=round_to_tick(s.close[j - 1] * 0.9, 0.05, "up"))
    write_plan(r, buys=[b])
    open(os.path.join(r.state, "KILL"), "w").close()
    start_day(r)
    r.ex.open_session()
    assert not orders(r, b)
    assert r.broker.get_stop(r.ex._stop_obj(r.ex.book.get(a))).state == STOP_ACTIVE


def test_stale_plan_places_no_orders(rig, dataset):
    i, a = find(dataset, calm_open)
    _, b = find(dataset, lambda s, j: calm_open(s, j) and s.symbol != a, start=i)
    r = rig(i)
    s, j = bar(r, a)
    hold(r, a, 20, entry=s.close[j - 1], stop=s.close[j - 1] * 0.9)
    write_plan(r, buys=[b], exits=[a], as_of=r.dates[i - 2])    # two sessions old
    start_day(r)
    r.ex.open_session()
    assert not orders(r, a, SELL) and not orders(r, b)
    assert any("plan is from" in w for e in events(r, "entries_skipped") for w in e["why"])


def test_missing_the_opening_window_means_exits_only(rig, dataset):
    i, a = find(dataset, calm_open)
    _, b = find(dataset, lambda s, j: calm_open(s, j) and s.symbol != a, start=i)
    r = rig(i)
    s, j = bar(r, a)
    hold(r, a, 20, entry=s.close[j - 1], stop=s.close[j - 1] * 0.9)
    write_plan(r, buys=[b], exits=[a])
    start_day(r)
    r.ex.open_session(late=True)
    assert len(orders(r, a, SELL)) == 1
    assert not orders(r, b)


# ------------------------------------------------------------------- daemon
def test_daemon_restart_mid_session_does_not_repeat_the_open(rig, dataset, cfg):
    i, sym = find(dataset, calm_open)
    r = rig(i)
    write_plan(r, buys=[sym])
    r.daemon.run(until=dt.datetime.combine(r.day, dt.time(11, 0), tzinfo=IST))
    assert len(orders(r, sym, BUY)) == 1
    ex2 = build_executor(cfg, r.broker, r.clock, r.ex.market, r.state, r.orders,
                         universe=dataset.series.keys(), echo=False)
    d2 = Daemon(ex2, r.clock, TradingCalendar(sessions=dataset.all_dates()), cfg)
    d2.run(until=dt.datetime.combine(r.day, dt.time(17, 0), tzinfo=IST))
    assert len(orders(r, sym, BUY)) == 1
    assert {"prep", "open", "close", "eod"} <= set(ex2.st["done"])
    assert os.path.exists(os.path.join(r.orders, f"{r.day.isoformat()}.plan.json"))


def test_daemon_started_after_the_window_does_not_enter(rig, dataset):
    i, sym = find(dataset, calm_open)
    r = rig(i)
    write_plan(r, buys=[sym])
    at(r, 10, 30)
    r.daemon.run(until=dt.datetime.combine(r.day, dt.time(12, 0), tzinfo=IST))
    assert not orders(r, sym)
    assert any("missed the opening window" in w
               for e in events(r, "entries_skipped") for w in e["why"])


def test_heartbeat_is_written(rig, dataset):
    i, sym = find(dataset, calm_open)
    r = rig(i)
    r.daemon.run(until=dt.datetime.combine(r.day, dt.time(9, 30), tzinfo=IST))
    assert os.path.exists(os.path.join(r.state, "heartbeat.json"))


def test_restart_after_0915_still_reconciles_before_entering(rig, dataset):
    """Regression: a daemon starting at 09:20 used to mark prep as 'missed' and
    go straight to the open - placing entries without reconciling holdings."""
    i, a = find(dataset, calm_open)
    _, b = find(dataset, lambda s, j: calm_open(s, j) and s.symbol != a, start=i)
    r = rig(i)
    s, j = bar(r, a)
    hold(r, a, 20, entry=s.close[j - 1], stop=s.close[j - 1] * 0.9)
    r.broker.state["holdings"][a]["qty"] = 12            # partly sold by hand overnight
    r.broker._save()
    write_plan(r, buys=[b])
    at(r, 9, 20)
    r.daemon.run(until=dt.datetime.combine(r.day, dt.time(9, 40), tzinfo=IST))
    assert events(r, "reconciled"), "prep must run, late, before the open"
    assert not orders(r, b, BUY), "the mismatch must block the entry"
