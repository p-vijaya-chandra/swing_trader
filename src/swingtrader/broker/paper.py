"""A simulated exchange behind the real broker interface.

Runs in two settings:
  * replay  - prices from BarReplayFeed under a SimClock (tests, demos)
  * shadow  - prices from a LIVE broker's quotes, fills simulated here, so the
              whole real-time system can run against the actual market for
              weeks without one rupee at risk

It is deliberately strict, because a permissive simulator teaches the executor
habits that the real exchange will punish: selling shares you do not hold is
rejected, buying without funds is rejected, orders outside market hours are
rejected, and a protective stop that fires competes with any other sell for the
same shares - exactly the race that produces a double-sell in production.
"""
from __future__ import annotations

import json
import os
from typing import Callable, Dict, List, Optional, Sequence

from ..costs import CostModel
from .base import (BUY, CANCELLED, FILLED, LIMIT, MARKET, REJECTED, SELL, SL, SL_M,
                   STOP_ACTIVE, STOP_CANCELLED, STOP_DONE, STOP_TRIGGERED, WORKING,
                   Broker, BrokerError, Holding, OrderRequest, OrderState, Quote,
                   StopState)

QuoteSource = Callable[[Sequence[str]], Dict[str, Quote]]


class PaperBroker(Broker):
    name = "paper"
    is_live = False

    def __init__(self, quote_source: QuoteSource, clock, cost_model: CostModel,
                 cash: float = 100000.0, state_path: Optional[str] = None,
                 slippage_bps: Optional[float] = None,
                 session_open: str = "09:00", session_close: str = "15:30",
                 tick_size: float = 0.05):
        self.quote_source = quote_source
        self.clock = clock
        self.costs = cost_model
        self.slip = (cost_model.slippage_bps if slippage_bps is None else slippage_bps) / 1e4
        self.state_path = state_path
        self.session_open = session_open
        self.session_close = session_close
        self.tick_size = tick_size
        # False simulates a broker whose GTT never fires - used to prove the
        # executor's watchdog catches a failed stop on its own.
        self.tick_stops = True
        self.state = {"cash": float(cash), "holdings": {}, "orders": {}, "stops": {},
                      "seq": 0, "fills": []}
        if state_path and os.path.exists(state_path):
            with open(state_path, "r", encoding="utf-8") as fh:
                self.state = json.load(fh)

    # ------------------------------------------------------------- persistence
    def _save(self) -> None:
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.state_path)     # atomic: a crash never leaves half a file

    def _next(self, prefix: str) -> str:
        self.state["seq"] += 1
        return f"{prefix}{self.state['seq']:07d}"

    # ------------------------------------------------------------------ helpers
    def _on_tick(self, price: Optional[float]) -> bool:
        """The exchange rejects prices off the tick grid; so does this."""
        if price is None or not self.tick_size:
            return True
        n = price / self.tick_size
        return abs(n - round(n)) < 1e-6

    def _market_open(self) -> bool:
        now = self.clock.now()
        if now.weekday() >= 5:
            return False
        return (self.clock.at(now.date(), self.session_open) <= now
                <= self.clock.at(now.date(), self.session_close))

    def _quote(self, symbol: str) -> Optional[Quote]:
        q = self.quote_source([symbol]).get(symbol)
        return q if (q is not None and q.valid()) else None

    def _reserved_sell(self, symbol: str, exclude: str = "") -> int:
        return sum(o["qty"] - o["filled_qty"] for oid, o in self.state["orders"].items()
                   if oid != exclude and o["symbol"] == symbol and o["side"] == SELL
                   and o["state"] == WORKING)

    def _reserved_cash(self, exclude: str = "") -> float:
        out = 0.0
        for oid, o in self.state["orders"].items():
            if oid == exclude or o["side"] != BUY or o["state"] != WORKING:
                continue
            px = o["price"] or o.get("ref_px") or 0.0
            out += (o["qty"] - o["filled_qty"]) * px * (1 + self.slip) * 1.003
        return out

    @staticmethod
    def _to_state(o: dict) -> OrderState:
        return OrderState(order_id=o["id"], ref_id=o["ref"], symbol=o["symbol"],
                          side=o["side"], qty=o["qty"], order_type=o["type"],
                          state=o["state"], raw_status=o["state"],
                          filled_qty=o["filled_qty"], avg_price=o["avg_price"],
                          price=o["price"], trigger_price=o.get("trigger"),
                          message=o.get("msg", ""), ts=o.get("ts", 0.0))

    # ------------------------------------------------------------------ account
    def holdings(self) -> Dict[str, Holding]:
        return {s: Holding(s, h["qty"], h["avg"])
                for s, h in self.state["holdings"].items() if h["qty"] > 0}

    def cash_available(self) -> float:
        return self.state["cash"] - self._reserved_cash()

    def quotes(self, symbols: Sequence[str]) -> Dict[str, Quote]:
        return {k: v for k, v in self.quote_source(list(symbols)).items() if v.valid()}

    # ------------------------------------------------------------------- orders
    def place_order(self, req: OrderRequest) -> OrderState:
        for o in self.state["orders"].values():
            if o["ref"] == req.ref_id:
                # the exchange-side equivalent of a duplicate client order id
                raise BrokerError(f"duplicate reference {req.ref_id}", code="DUPLICATE_REF")
        oid = self._next("PO")
        q = self._quote(req.symbol)
        o = {"id": oid, "ref": req.ref_id, "symbol": req.symbol, "side": req.side,
             "qty": int(req.qty), "type": req.order_type, "price": float(req.price or 0.0),
             "trigger": req.trigger_price, "state": WORKING, "filled_qty": 0,
             "avg_price": 0.0, "ts": self.clock.epoch(),
             "day": self.clock.today().isoformat(), "reason": req.reason,
             "ref_px": q.ltp if q else 0.0, "triggered": False}

        def reject(msg: str) -> OrderState:
            o["state"], o["msg"] = REJECTED, msg
            self.state["orders"][oid] = o
            self._save()
            return self._to_state(o)

        if req.qty <= 0 or int(req.qty) != req.qty:
            return reject("quantity must be a positive whole number")
        if req.side not in (BUY, SELL):
            return reject(f"bad side {req.side}")
        if req.order_type not in (MARKET, LIMIT, SL, SL_M):
            return reject(f"bad order type {req.order_type}")
        if req.order_type in (LIMIT, SL) and not self._on_tick(req.price):
            return reject(f"price {req.price} is not a multiple of the tick {self.tick_size}")
        if req.order_type in (SL, SL_M) and not self._on_tick(req.trigger_price):
            return reject(f"trigger {req.trigger_price} is not on the tick grid")
        if not self._market_open():
            return reject("market closed")
        if q is None:
            return reject("no price for symbol")
        if req.side == SELL:
            held = self.state["holdings"].get(req.symbol, {}).get("qty", 0)
            if req.qty > held - self._reserved_sell(req.symbol):
                return reject(f"insufficient holdings: have {held}, "
                              f"{self._reserved_sell(req.symbol)} already reserved")
        else:
            px = req.price if req.order_type in (LIMIT, SL) and req.price else q.ltp
            need = req.qty * px * (1 + self.slip) * 1.003
            if need > self.state["cash"] - self._reserved_cash():
                return reject("insufficient funds")

        self.state["orders"][oid] = o
        self._match_order(o, q)
        self._save()
        return self._to_state(o)

    def cancel_order(self, order_id: str) -> OrderState:
        o = self.state["orders"].get(order_id)
        if o is None:
            raise BrokerError(f"no such order {order_id}", code="NOT_FOUND")
        if o["state"] == WORKING:
            o["state"] = CANCELLED
            self._save()
        return self._to_state(o)

    def get_order(self, order_id: str) -> OrderState:
        o = self.state["orders"].get(order_id)
        if o is None:
            raise BrokerError(f"no such order {order_id}", code="NOT_FOUND")
        return self._to_state(o)

    def find_order_by_ref(self, ref_id: str) -> Optional[OrderState]:
        for o in self.state["orders"].values():
            if o["ref"] == ref_id:
                return self._to_state(o)
        return None

    def orders_today(self) -> List[OrderState]:
        day = self.clock.today().isoformat()
        return [self._to_state(o) for o in self.state["orders"].values() if o["day"] == day]

    # --------------------------------------------------------------- stops (GTT)
    def place_stop(self, symbol: str, qty: int, trigger: float, ref_id: str) -> StopState:
        for s in self.state["stops"].values():
            if s["ref"] == ref_id:
                raise BrokerError(f"duplicate reference {ref_id}", code="DUPLICATE_REF")
        if not self._on_tick(trigger):
            raise BrokerError(f"trigger {trigger} is not on the tick grid", code="BAD_TICK")
        held = self.state["holdings"].get(symbol, {}).get("qty", 0)
        if qty <= 0 or qty > held:
            raise BrokerError(f"stop for {qty} {symbol} but only {held} held",
                              code="QTY_EXCEEDS_HOLDING")
        sid = self._next("PG")
        self.state["stops"][sid] = {"id": sid, "ref": ref_id, "symbol": symbol,
                                    "qty": int(qty), "trigger": float(trigger),
                                    "state": STOP_ACTIVE, "child": ""}
        self._save()
        return self._stop_state(self.state["stops"][sid])

    def modify_stop(self, stop: StopState, qty: int, trigger: float) -> StopState:
        s = self.state["stops"].get(stop.stop_id)
        if s is None or s["state"] != STOP_ACTIVE:
            raise BrokerError(f"stop {stop.stop_id} is not active", code="NOT_ACTIVE")
        if not self._on_tick(trigger):
            raise BrokerError(f"trigger {trigger} is not on the tick grid", code="BAD_TICK")
        s["qty"], s["trigger"] = int(qty), float(trigger)
        self._save()
        return self._stop_state(s)

    def cancel_stop(self, stop: StopState) -> StopState:
        s = self.state["stops"].get(stop.stop_id)
        if s is None:
            raise BrokerError(f"no such stop {stop.stop_id}", code="NOT_FOUND")
        if s["state"] != STOP_ACTIVE:
            # Already fired: the real API refuses to cancel a triggered GTT, and
            # the caller must learn that rather than assume the cancel worked.
            raise BrokerError(f"stop {stop.stop_id} already {s['state']}",
                              code="NOT_ACTIVE")
        s["state"] = STOP_CANCELLED
        self._save()
        return self._stop_state(s)

    def stops(self) -> List[StopState]:
        return [self._stop_state(s) for s in self.state["stops"].values()]

    @staticmethod
    def _stop_state(s: dict) -> StopState:
        return StopState(stop_id=s["id"], ref_id=s["ref"], symbol=s["symbol"],
                         qty=s["qty"], trigger=s["trigger"], state=s["state"],
                         kind="gtt", raw_status=s["state"], child_order_id=s["child"])

    # ----------------------------------------------------------------- matching
    def _fill(self, o: dict, price: float, ref_px: float) -> None:
        qty = o["qty"] - o["filled_qty"]
        h = self.state["holdings"].setdefault(o["symbol"], {"qty": 0, "avg": 0.0})
        charges = self.costs.charges(price, qty, o["side"]).total
        if o["side"] == BUY:
            cost = price * qty + charges
            if cost > self.state["cash"] + 1e-6:
                o["state"], o["msg"] = REJECTED, "insufficient funds at fill"
                return
            self.state["cash"] -= cost
            new_qty = h["qty"] + qty
            h["avg"] = (h["avg"] * h["qty"] + price * qty) / new_qty
            h["qty"] = new_qty
        else:
            if qty > h["qty"]:
                o["state"], o["msg"] = REJECTED, "insufficient holdings at fill"
                return
            self.state["cash"] += price * qty - charges
            h["qty"] -= qty
            if h["qty"] == 0:
                h["avg"] = 0.0
        o["avg_price"] = price
        o["filled_qty"] = o["qty"]
        o["state"] = FILLED
        o["fill_ts"] = self.clock.epoch()
        self.state["fills"].append({"order": o["id"], "symbol": o["symbol"],
                                    "side": o["side"], "qty": qty, "price": price,
                                    "charges": charges, "ref_px": ref_px,
                                    "ts": o["fill_ts"], "day": self.clock.today().isoformat()})

    def _match_order(self, o: dict, q: Optional[Quote]) -> None:
        if o["state"] != WORKING or q is None:
            return
        ltp = q.ltp
        buy = o["side"] == BUY
        if o["type"] in (SL, SL_M) and not o["triggered"]:
            hit = ltp >= o["trigger"] if buy else ltp <= o["trigger"]
            if not hit:
                return
            o["triggered"] = True
        kind = MARKET if o["type"] in (MARKET, SL_M) else LIMIT
        if kind == MARKET:
            self._fill(o, ltp * (1 + self.slip) if buy else ltp * (1 - self.slip), ltp)
            return
        lim = o["price"]
        if buy:
            cross = ltp * (1 + self.slip)
            if cross <= lim:
                self._fill(o, cross, ltp)
            elif ltp <= lim:
                self._fill(o, lim, ltp)
        else:
            cross = ltp * (1 - self.slip)
            if cross >= lim:
                self._fill(o, cross, ltp)
            elif ltp >= lim:
                self._fill(o, lim, ltp)

    def tick(self) -> None:
        """Match working orders and fire stops against current prices."""
        if not self._market_open():
            return
        syms = sorted({o["symbol"] for o in self.state["orders"].values()
                       if o["state"] == WORKING}
                      | {s["symbol"] for s in self.state["stops"].values()
                         if s["state"] == STOP_ACTIVE})
        if not syms:
            return
        quotes = self.quote_source(syms)
        changed = False
        for o in list(self.state["orders"].values()):
            if o["state"] == WORKING:
                before = o["state"]
                self._match_order(o, quotes.get(o["symbol"]))
                changed |= o["state"] != before
        for s in list(self.state["stops"].values()) if self.tick_stops else ():
            if s["state"] != STOP_ACTIVE:
                continue
            q = quotes.get(s["symbol"])
            if q is None or not q.valid() or q.ltp > s["trigger"]:
                continue
            s["state"] = STOP_TRIGGERED
            held = self.state["holdings"].get(s["symbol"], {}).get("qty", 0)
            free = held - self._reserved_sell(s["symbol"])
            qty = min(s["qty"], free)
            if qty <= 0:
                # Shares already gone (sold elsewhere): a real GTT fails here too.
                s["state"] = "FAILED"
                changed = True
                continue
            child = {"id": self._next("PO"), "ref": s["ref"] + "C", "symbol": s["symbol"],
                     "side": SELL, "qty": qty, "type": MARKET, "price": 0.0,
                     "trigger": None, "state": WORKING, "filled_qty": 0, "avg_price": 0.0,
                     "ts": self.clock.epoch(), "day": self.clock.today().isoformat(),
                     "reason": "gtt", "ref_px": q.ltp, "triggered": False}
            self.state["orders"][child["id"]] = child
            self._match_order(child, q)
            s["child"] = child["id"]
            s["state"] = STOP_DONE if child["state"] == FILLED else STOP_TRIGGERED
            changed = True
        if changed:
            self._save()
