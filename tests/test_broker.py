"""Broker layer: the paper exchange, the Groww adapter (against a stand-in with
growwapi 1.5.0's exact signatures), the risk guard, and TOTP."""
import datetime as dt
import os

import pytest

from swingtrader.broker import (BUY, FILLED, LIMIT, MARKET, REJECTED, SELL,
                                SL_M, STOP_ACTIVE, STOP_DONE, BrokerError, OrderRequest,
                                Quote, make_ref_id, normalise_order_state, round_to_tick)
from swingtrader.broker.groww import GrowwBroker, totp
from swingtrader.broker.paper import PaperBroker
from swingtrader.config import Config
from swingtrader.costs import CostModel
from swingtrader.data.models import Series
from swingtrader.realtime.clock import IST, SimClock
from swingtrader.realtime.guard import RiskGuard
from swingtrader.realtime.replay import BarReplayFeed


# --------------------------------------------------------------- fixtures
@pytest.fixture
def market():
    """One symbol, one up-day: open 102, dips to 90 (by ~11:07), rallies to 110, closes 108."""
    s = Series("X", ["2026-10-26", "2026-10-27"], [100, 102], [105, 110], [99, 90],
               [101, 108], [1e6, 1e6])
    clock = SimClock(dt.datetime(2026, 10, 27, 9, 15, 5, tzinfo=IST))
    feed = BarReplayFeed({"X": s}, clock)
    return clock, feed


@pytest.fixture
def paper(market, tmp_path):
    clock, feed = market
    return PaperBroker(feed.quotes, clock, CostModel(Config()), cash=100000,
                       state_path=str(tmp_path / "pb.json"))


def at(clock, hh, mm, ss=0):
    clock.set(dt.datetime(2026, 10, 27, hh, mm, ss, tzinfo=IST))


# ------------------------------------------------------------- base utils
def test_reference_ids_are_deterministic_and_purpose_specific():
    a = make_ref_id("2026-10-27", "INFY", "entry")
    assert a == make_ref_id("2026-10-27", "INFY", "entry")
    assert a != make_ref_id("2026-10-27", "INFY", "exit")
    assert a != make_ref_id("2026-10-28", "INFY", "entry")
    assert len(a) == 16 and a.isalnum()


def test_tick_rounding_direction():
    assert round_to_tick(100.07, 0.05, "down") == 100.05     # a buy never pays more
    assert round_to_tick(100.01, 0.05, "up") == 100.05       # a stop never gives more room
    assert round_to_tick(100.05, 0.05, "up") == 100.05


def test_partial_fill_labelled_executed_stays_working():
    assert normalise_order_state("EXECUTED", 3, 10) == "WORKING"
    assert normalise_order_state("DELIVERY_AWAITED", 10, 10) == FILLED
    assert normalise_order_state("SOMETHING_NEW", 0, 10) == "WORKING"


# ------------------------------------------------------------ paper broker
def test_marketable_limit_fills_with_slippage(paper):
    o = paper.place_order(OrderRequest("X", BUY, 10, LIMIT, "STREF0000000001A", price=105.0))
    assert o.state == FILLED and o.filled_qty == 10
    assert 102.0 < o.avg_price < 102.3


def test_limit_below_market_rests(paper, market):
    o = paper.place_order(OrderRequest("X", BUY, 10, LIMIT, "STREF0000000002A", price=95.0))
    assert o.state == "WORKING"
    clock, _ = market
    at(clock, 10, 50)          # price has fallen through 95 by now
    paper.tick()
    assert paper.get_order(o.order_id).state == FILLED


def test_selling_unheld_shares_is_rejected(paper):
    o = paper.place_order(OrderRequest("X", SELL, 1, MARKET, "STREF0000000003A"))
    assert o.state == REJECTED and "insufficient holdings" in o.message


def test_duplicate_reference_is_refused(paper):
    paper.place_order(OrderRequest("X", BUY, 1, LIMIT, "STREF0000000004A", price=105.0))
    with pytest.raises(BrokerError) as e:
        paper.place_order(OrderRequest("X", BUY, 1, LIMIT, "STREF0000000004A", price=105.0))
    assert e.value.code == "DUPLICATE_REF"


def test_off_tick_prices_are_rejected_like_the_exchange(paper):
    o = paper.place_order(OrderRequest("X", BUY, 1, LIMIT, "STREF0000000005A", price=104.03))
    assert o.state == REJECTED and "tick" in o.message


def test_orders_outside_market_hours_are_rejected(paper, market):
    clock, _ = market
    at(clock, 16, 0)
    o = paper.place_order(OrderRequest("X", BUY, 1, MARKET, "STREF0000000006A"))
    assert o.state == REJECTED and o.message == "market closed"


def test_gtt_stop_fires_and_sells_once(paper, market):
    clock, _ = market
    paper.place_order(OrderRequest("X", BUY, 50, LIMIT, "STREF0000000007A", price=105.0))
    st = paper.place_stop("X", 50, 95.0, "STREF0000000007S")
    assert st.state == STOP_ACTIVE
    # the path reaches 95 at about 10:21 on its way down to the day's low
    for m in range(16, 120):
        at(clock, 9 + m // 60, m % 60)
        paper.tick()
    fired = paper.get_stop(st)
    assert fired.state == STOP_DONE
    assert paper.holdings() == {}
    child = paper.get_order(fired.child_order_id)
    assert child.state == FILLED and 94.0 < child.avg_price <= 95.0
    with pytest.raises(BrokerError) as e:      # the race behind double-sells
        paper.cancel_stop(fired)
    assert e.value.code == "NOT_ACTIVE"


def test_paper_state_survives_restart(paper, market, tmp_path):
    clock, feed = market
    paper.place_order(OrderRequest("X", BUY, 7, LIMIT, "STREF0000000008A", price=105.0))
    again = PaperBroker(feed.quotes, clock, CostModel(Config()),
                        state_path=str(tmp_path / "pb.json"))
    assert again.holdings()["X"].qty == 7
    assert again.find_order_by_ref("STREF0000000008A").state == FILLED


# ----------------------------------------------------------------- guard
@pytest.fixture
def guard(tmp_path, market):
    clock, _ = market
    return RiskGuard(Config(), str(tmp_path), clock, {"X"})


def quote(ltp=100.0, ts=None, clock=None):
    return Quote("X", ltp, ts=ts if ts is not None else clock.epoch())


def test_guard_caps_and_kill_switch(guard, market, tmp_path):
    clock, _ = market
    q = quote(clock=clock)
    ok = guard.check(OrderRequest("X", BUY, 100, LIMIT, "r", price=100.5), q, 50000)
    assert ok.ok
    big = guard.check(OrderRequest("X", BUY, 400, LIMIT, "r", price=100.5), q, 1e9)
    assert not big.ok and "per-order cap" in big.reason
    far = guard.check(OrderRequest("X", BUY, 10, LIMIT, "r", price=120.0), q, 1e9)
    assert not far.ok and "from LTP" in far.reason
    poor = guard.check(OrderRequest("X", BUY, 100, LIMIT, "r", price=100.5), q, 5000)
    assert not poor.ok and "available cash" in poor.reason
    alien = guard.check(OrderRequest("ZZZ", BUY, 1, LIMIT, "r", price=100.0), q, 1e9)
    assert not alien.ok and "universe" in alien.reason
    stale = guard.check(OrderRequest("X", BUY, 1, LIMIT, "r", price=100.0),
                        quote(ts=clock.epoch() - 3600), 1e9)
    assert not stale.ok and "old" in stale.reason

    open(os.path.join(str(tmp_path), "KILL"), "w").close()
    killed = guard.check(OrderRequest("X", SELL, 1, MARKET, "r"), q)
    assert not killed.ok and "kill switch" in killed.reason
    # a protective exit must still be possible while frozen
    assert guard.check(OrderRequest("X", SELL, 1, MARKET, "r"), q, protective=True).ok


def test_guard_daily_order_cap_does_not_block_protective_exits(guard, market):
    clock, _ = market
    q = quote(clock=clock)
    req = OrderRequest("X", BUY, 1, LIMIT, "r", price=100.0)
    for _ in range(guard.max_orders_per_day):
        guard.record(req, 100.0)
    assert not guard.check(req, q, 1e9).ok
    assert guard.check(OrderRequest("X", SELL, 1, MARKET, "r"), q, protective=True).ok


def test_live_needs_both_switches(tmp_path, market, monkeypatch):
    clock, _ = market
    monkeypatch.delenv("SWING_LIVE_CONFIRM", raising=False)
    paper_cfg = RiskGuard(Config(), str(tmp_path), clock)
    assert not paper_cfg.live_allowed().ok
    live_cfg = RiskGuard(Config().with_overrides({"execution.mode": "live"}), str(tmp_path), clock)
    assert not live_cfg.live_allowed().ok                 # config alone is not enough
    monkeypatch.setenv("SWING_LIVE_CONFIRM", "yes")
    assert live_cfg.live_allowed().ok
    assert not paper_cfg.live_allowed().ok                # env alone is not enough


def test_canary_caps_quantity(tmp_path, market):
    clock, _ = market
    g = RiskGuard(Config().with_overrides({"execution.canary_qty": 1}), str(tmp_path), clock)
    assert g.apply_canary(37) == 1
    assert RiskGuard(Config(), str(tmp_path), clock).apply_canary(37) == 37


# --------------------------------------------------------------------- TOTP
@pytest.mark.parametrize("t,expected", [(59, "94287082"), (1111111109, "07081804"),
                                        (1111111111, "14050471"), (1234567890, "89005924"),
                                        (2000000000, "69279037"), (20000000000, "65353130")])
def test_totp_matches_rfc6238(t, expected):
    assert totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", t, digits=8) == expected


# ------------------------------------------------- Groww adapter, fake SDK
class GrowwAPITimeoutException(Exception):
    pass


class GrowwAPIAuthenticationException(Exception):
    pass


class GrowwAPIRateLimitException(Exception):
    pass


class GrowwAPIAuthorisationException(Exception):
    pass


class GrowwAPINotFoundException(Exception):
    pass


class FakeGroww:
    """Keyword signatures copied from growwapi 1.5.0's GrowwAPI."""

    def __init__(self, script=None):
        self.calls, self.script, self.orders = [], dict(script or {}), {}

    def _scripted(self, name):
        seq = self.script.get(name)
        if seq:
            nxt = seq.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        return None

    def place_order(self, validity, exchange, order_type, product, quantity, segment,
                    trading_symbol, transaction_type, order_reference_id=None, price=0.0,
                    trigger_price=None, timeout=None):
        self.calls.append(("place_order", dict(locals())))
        r = self._scripted("place_order")
        if r is not None:
            return r
        oid = f"G{len(self.orders) + 1:04d}"
        self.orders[order_reference_id] = oid
        return {"groww_order_id": oid, "order_status": "OPEN",
                "order_reference_id": order_reference_id}

    def get_order_status_by_reference(self, segment, order_reference_id, timeout=None):
        self.calls.append(("by_ref", dict(locals())))
        if order_reference_id in self.orders:
            return {"groww_order_id": self.orders[order_reference_id], "order_status": "EXECUTED"}
        raise GrowwAPINotFoundException()

    def get_order_detail(self, segment, groww_order_id, timeout=None):
        self.calls.append(("detail", dict(locals())))
        return {"groww_order_id": groww_order_id, "order_status": "EXECUTED",
                "filled_quantity": 10, "quantity": 10, "average_fill_price": 101.5,
                "trading_symbol": "INFY", "transaction_type": "BUY", "order_type": "LIMIT"}

    def get_holdings_for_user(self, timeout=None):
        self.calls.append(("holdings", dict(locals())))
        r = self._scripted("holdings")
        return r if r is not None else {"holdings": [
            {"trading_symbol": "INFY", "quantity": 10, "average_price": 101.5}]}

    def get_ltp(self, exchange_trading_symbols, segment, timeout=None):
        self.calls.append(("ltp", dict(locals())))
        return {k: 100.0 + i for i, k in enumerate(exchange_trading_symbols)}

    def get_ohlc(self, exchange_trading_symbols, segment, timeout=None):
        return {k: {"open": 99.0, "high": 101.0, "low": 98.0, "close": 98.5}
                for k in exchange_trading_symbols}

    def create_smart_order(self, smart_order_type, segment, trading_symbol, quantity,
                           product_type, exchange, duration, reference_id=None,
                           trigger_price=None, trigger_direction=None, order=None,
                           child_legs=None, net_position_quantity=None, target=None,
                           stop_loss=None, transaction_type=None, timeout=None):
        self.calls.append(("gtt", dict(locals())))
        r = self._scripted("gtt")
        if r is not None:
            return r
        return {"smart_order_id": "gtt_1", "status": "ACTIVE", "reference_id": reference_id,
                "trading_symbol": trading_symbol, "quantity": quantity,
                "trigger_price": trigger_price}


def mk(api, logins=None):
    return GrowwBroker(Config(), api=api,
                       token_factory=(lambda: (logins.append(1) or "tok")) if logins is not None
                       else (lambda: "tok"),
                       api_factory=lambda t: api, sleeper=lambda s: None)


def test_groww_order_uses_cnc_cash_day_and_a_timeout():
    api = FakeGroww()
    st = mk(api).place_order(OrderRequest("INFY", BUY, 10, LIMIT, "STAAAAAAAAAAAAAA",
                                          price=1234.567))
    kw = api.calls[-1][1]
    assert (kw["product"], kw["segment"], kw["validity"], kw["exchange"]) == \
        ("CNC", "CASH", "DAY", "NSE")
    assert kw["price"] == 1234.55, "buy limit must round DOWN to the tick"
    assert kw["timeout"] is not None, "the SDK's default timeout is infinite"
    assert kw["order_reference_id"] == "STAAAAAAAAAAAAAA"
    assert st.state == "WORKING"


def test_groww_write_timeout_is_ambiguous_and_never_retried():
    api = FakeGroww({"place_order": [GrowwAPITimeoutException()]})
    with pytest.raises(BrokerError) as e:
        mk(api).place_order(OrderRequest("INFY", BUY, 10, LIMIT, "STAAAAAAAAAAAAAB", price=100))
    assert e.value.ambiguous
    assert sum(1 for c in api.calls if c[0] == "place_order") == 1


def test_groww_reads_retry_through_timeouts_and_rate_limits():
    api = FakeGroww({"holdings": [GrowwAPITimeoutException(), GrowwAPIRateLimitException()]})
    assert mk(api).holdings()["INFY"].qty == 10


def test_groww_relogs_in_once_on_expired_token():
    logins = []
    api = FakeGroww({"holdings": [GrowwAPIAuthenticationException()]})
    mk(api, logins).holdings()
    assert len(logins) == 1


def test_groww_403_explains_static_ip_rule():
    api = FakeGroww({"place_order": [GrowwAPIAuthorisationException()]})
    with pytest.raises(BrokerError) as e:
        mk(api).place_order(OrderRequest("INFY", BUY, 1, MARKET, "STAAAAAAAAAAAAAC"))
    assert "static IP" in str(e.value)


def test_groww_unreadable_holdings_raise_instead_of_returning_empty():
    """An empty answer here would look exactly like every position being sold."""
    api = FakeGroww({"holdings": [{"unexpected": {"shape": 1}}]})
    with pytest.raises(BrokerError):
        mk(api).holdings()


def test_groww_order_lookup_by_reference():
    api = FakeGroww()
    b = mk(api)
    b.place_order(OrderRequest("INFY", BUY, 10, LIMIT, "STAAAAAAAAAAAAAD", price=100))
    found = b.find_order_by_ref("STAAAAAAAAAAAAAD")
    assert found is not None and found.state == FILLED and found.filled_qty == 10
    assert b.find_order_by_ref("STNEVERPLACED000") is None


def test_groww_quotes_parse_either_symbol_separator():
    q = mk(FakeGroww()).quotes(["RELIANCE", "INFY"])
    assert set(q) == {"RELIANCE", "INFY"}
    assert q["RELIANCE"].open == 99.0


def test_groww_stop_is_a_gtt_sell_rounded_up():
    api = FakeGroww()
    st = mk(api).place_stop("INFY", 10, 95.01, "STAAAAAAAAAAAAAE")
    kw = [c for c in api.calls if c[0] == "gtt"][-1][1]
    assert kw["smart_order_type"] == "GTT" and kw["trigger_direction"] == "DOWN"
    assert kw["trigger_price"] == "95.05", "a stop trigger must round UP to the tick"
    assert kw["order"] == {"order_type": "MARKET", "transaction_type": "SELL"}
    assert kw["product_type"] == "CNC"
    assert st.kind == "gtt"


def test_groww_falls_back_to_a_day_stop_when_gtt_is_refused():
    class Refused(Exception):
        pass
    Refused.__name__ = "GrowwAPIException"
    api = FakeGroww({"gtt": [Refused("GTT not allowed on unsettled quantity")]})
    st = mk(api).place_stop("INFY", 10, 95.0, "STAAAAAAAAAAAAAF")
    assert st.kind == "day"
    kw = api.calls[-1][1]
    assert kw["order_type"] == SL_M and kw["transaction_type"] == SELL
    assert kw["trigger_price"] == 95.0

