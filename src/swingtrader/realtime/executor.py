"""The live executor: turns last night's plan into broker orders and manages
the resulting positions through the session.

It reproduces the backtest's fill logic deliberately and exactly:
  * exits before entries, so freed cash is available to buys
  * the gap rule against the ACTUAL open: skip if it opened more than
    entry.max_gap_pct above the signal close
  * re-size at the open with the real price, and re-anchor the stop to it, so
    a trade risks the same 1R it did in the backtest
  * exposure capped by YESTERDAY's regime - all that is knowable at 09:15

And it handles what a backtest never sees:
  * write-ahead intent: every order is recorded on disk before it is sent, so a
    crash between "sent" and "recorded" resolves on restart by looking the
    order up by its deterministic reference instead of sending it again
  * ambiguous writes: a timeout is treated as "unknown", never as "failed"
  * partial fills: whatever filled becomes a position with its own stop
  * the stop race: a protective stop is cancelled BEFORE any other sell for the
    same shares, and if the cancel fails because the stop already fired, the
    other sell is not sent
  * external changes: positions closed or resized by hand are reconciled
    against broker holdings, and unexplained mismatches block new entries

Guiding rule when broker state is unclear: do not trade; protect; alert.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import traceback
from typing import Any, Dict, List, Optional

from ..broker.base import (BUY, CANCELLED, FILLED, LIMIT, MARKET, REJECTED, SELL,
                           STOP_ACTIVE, STOP_CANCELLED, STOP_DONE, STOP_FAILED,
                           STOP_TRIGGERED, AuthError, Broker, BrokerError,
                           OrderRequest, OrderState, StopState, make_ref_id,
                           round_to_tick)
from ..config import Config
from ..costs import CostModel
from ..live import (DailyPlan, LivePlanner, LivePosition, PositionBook, plan_from_json,
                    plan_to_json, render_plan_markdown, write_plan_csv)
from ..portfolio import Position, RiskManager
from ..regime import RISK_OFF
from .audit import Audit, Notifier
from .guard import RiskGuard
from .market import MarketData

NA = float("nan")


def _isnan(x: Any) -> bool:
    return isinstance(x, float) and x != x


class LiveExecutor:
    def __init__(self, cfg: Config, broker: Broker, clock, market: MarketData,
                 guard: RiskGuard, audit: Audit, notify: Notifier,
                 state_dir: str = "state", orders_dir: str = "orders",
                 entry_filter=None):
        self.cfg = cfg
        self.broker = broker
        self.clock = clock
        self.market = market
        self.guard = guard
        self.audit = audit
        self.notify = notify
        self.state_dir = state_dir
        self.orders_dir = orders_dir
        self.entry_filter = entry_filter
        os.makedirs(state_dir, exist_ok=True)
        os.makedirs(orders_dir, exist_ok=True)

        e = cfg.get("execution", {}) or {}
        self.capital = float(cfg.get("capital", 100000.0))
        self.entry_timeout_s = float(e.get("entry_timeout_s", 300))
        self.poll_s = float(e.get("poll_s", 30))
        self.grace_s = float(e.get("stop_grace_s", 90))
        self.emergency_tol = float(e.get("emergency_tolerance", 0.003))
        self.entry_buffer = float(e.get("entry_limit_buffer", 0.005))
        self.tick = float(e.get("tick_size", 0.05))
        self.skip_foreign = bool(e.get("skip_symbols_with_foreign_holdings", True))
        self.exit_settle_s = float(e.get("exit_settle_s", 20))
        self.cash_headroom = 0.006      # slippage + statutory charges + broker blocking margin
        self.max_gap = float(cfg.get("entry.max_gap_pct", 0.03))

        self.risk = RiskManager(cfg)
        self.costs = CostModel(cfg)
        self.mode = "live" if broker.is_live else "paper"
        self.book_path = os.path.join(state_dir, "positions.json")
        self.state_path = os.path.join(state_dir, "live_state.json")
        self.journal_path = os.path.join(
            state_dir, "journal.csv" if broker.is_live else "journal_paper.csv")
        self._written: Dict[str, str] = {}
        self.book = PositionBook.load(self.book_path, self.capital)
        if not self.book.peak_equity:
            self.book.peak_equity = self.book.cash
        self.st: Dict[str, Any] = self._load_state()

    # ================================================================ state
    def _default_state(self, day: str) -> Dict[str, Any]:
        return {"session": day, "done": [], "entries": {}, "exits": {}, "breach": {},
                "blocked": "", "foreign": {}, "risk_peak": self.capital,
                "days_in_halt": 0, "days_since_resume": -1,
                "equity_at_close": self.book.cash, "last_plan": "", "foreign_init": False}

    def _load_state(self) -> Dict[str, Any]:
        if os.path.exists(self.state_path):
            with open(self.state_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        return self._default_state("")

    def _atomic(self, path: str, obj: Any) -> None:
        text = json.dumps(obj, indent=1, sort_keys=True, default=str)
        if self._written.get(path) == text:
            return                  # unchanged: a 30-second poll should not rewrite disk
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)       # atomic: a crash never leaves half a file
        self._written[path] = text

    def save(self) -> None:
        from dataclasses import asdict
        self._atomic(self.book_path, {
            "capital_base": self.book.capital_base, "cash": self.book.cash,
            "halted": self.book.halted, "peak_equity": self.book.peak_equity,
            "risk_scale": self.book.risk_scale,
            "positions": [asdict(p) for p in self.book.positions]})
        self._atomic(self.state_path, self.st)

    def begin_session(self, day: dt.date) -> None:
        d = day.isoformat()
        if self.st.get("session") == d:
            return
        carry = {k: self.st.get(k) for k in ("foreign", "risk_peak", "days_in_halt",
                                              "days_since_resume", "equity_at_close",
                                              "last_plan", "foreign_init")
                 if k in self.st}
        self.st = self._default_state(d)
        self.st.update({k: v for k, v in carry.items() if v is not None})
        self.save()

    def today(self) -> str:
        return self.clock.today().isoformat()

    # ============================================================ helpers
    def _pos(self, symbol: str) -> Optional[LivePosition]:
        return self.book.get(symbol)

    def _exiting(self, symbol: str) -> bool:
        return any(x["symbol"] == symbol and x["state"] in ("placing", "working", "unknown")
                   for x in self.st["exits"].values())

    def _pending_entries(self) -> List[Dict[str, Any]]:
        return [e for e in self.st["entries"].values()
                if e["state"] in ("placing", "working", "unknown")]

    def _quote(self, symbol: str):
        try:
            return self.broker.quotes([symbol]).get(symbol)
        except BrokerError as exc:
            self.audit.log("quote_failed", symbol=symbol, error=str(exc))
            return None

    def _stop_obj(self, p: LivePosition) -> StopState:
        return StopState(p.stop_id, p.stop_ref, p.symbol, p.qty, p.stop, STOP_ACTIVE,
                         p.stop_kind or "gtt")

    def _sessions_between(self, start: str, end: str) -> int:
        try:
            dates = self.market.dataset().all_dates()
        except Exception:                                       # noqa: BLE001
            return 0
        return sum(1 for d in dates if start < d <= end)

    def _mark(self, p: LivePosition, at: str) -> float:
        px = self.market.close_of(p.symbol, at)
        return p.entry_price if _isnan(px) else px

    def equity(self, at: Optional[str] = None) -> float:
        at = at or self.market.last_session()
        total = self.book.cash
        for p in self.book.positions:
            px = self.market.close_of(p.symbol, at)
            total += p.qty * (p.entry_price if _isnan(px) else px)
        return total

    # ======================================================= reconciliation
    def reconcile(self) -> List[str]:
        """Compare the book with what the broker actually holds.

        Shares the system did not buy (your long-term holdings in the same demat)
        are recorded as 'foreign' and left alone. A bot position that vanished
        is closed in the book at the best price evidence available; a position
        that is merely smaller than expected is NOT guessed at - it blocks new
        entries until a human looks.
        """
        issues: List[str] = []
        holdings = self.broker.holdings()
        foreign: Dict[str, int] = self.st.setdefault("foreign", {})
        last = self.market.last_session()
        pending = {e["symbol"] for e in self._pending_entries()}

        for p in list(self.book.positions):
            have = holdings[p.symbol].qty if p.symbol in holdings else 0
            expect = p.qty + int(foreign.get(p.symbol, 0))
            if have == expect:
                continue
            if have > expect:
                foreign[p.symbol] = have - p.qty
                self.audit.log("foreign_shares", symbol=p.symbol, extra=have - expect)
                continue
            # T+1: shares bought in the latest session may not show in holdings yet
            if p.entry_date >= (last or p.entry_date):
                self.audit.log("unsettled", symbol=p.symbol, book=p.qty, broker=have)
                continue
            bot_have = have - int(foreign.get(p.symbol, 0))
            if bot_have <= 0:
                fill = self._find_sell_fill(p.symbol)
                px = fill.avg_price if fill else self.market.close_of(p.symbol, last)
                self._close_position(p.symbol, p.qty, px, "closed_externally")
                self.notify("warn", f"{p.symbol} is no longer held at the broker; closed in "
                                    f"the book at {px:,.2f} "
                                    f"({'from a sell fill' if fill else 'at the last close - verify'})")
            else:
                issues.append(f"{p.symbol}: book {p.qty}, broker {bot_have}")

        if not self.st.get("foreign_init"):
            for sym, h in holdings.items():
                if self._pos(sym) is None and sym not in pending:
                    foreign[sym] = h.qty
            self.st["foreign_init"] = True
            if foreign:
                self.audit.log("foreign_baseline", symbols=sorted(foreign))
        else:
            for sym, h in holdings.items():
                if self._pos(sym) is None and sym not in pending and sym not in foreign:
                    foreign[sym] = h.qty            # you bought something by hand
        for sym in list(foreign):
            if sym not in holdings:
                foreign.pop(sym)

        if issues:
            self.notify("critical", "position mismatch, new entries blocked: " + "; ".join(issues))
        self.audit.log("reconciled", positions=len(self.book.positions),
                       foreign=len(foreign), issues=issues)
        return issues

    def _find_sell_fill(self, symbol: str) -> Optional[OrderState]:
        best = None
        for o in self.broker.orders_today():
            if o.symbol == symbol and o.side == SELL and o.filled_qty > 0:
                if best is None or o.ts >= best.ts:
                    best = o
        return best

    # ============================================================= stops
    def _protect(self, p: LivePosition) -> bool:
        # The book holds exactly the tick-valid trigger the broker holds; any
        # difference between them is a modify call on every single pass.
        p.stop = round_to_tick(p.stop, self.tick, "up")
        p.stop_version += 1
        ref = make_ref_id(p.symbol, p.entry_date, "stop", p.stop_version)
        p.stop_ref, p.stop_id = ref, ""
        self.save()                                             # write-ahead
        try:
            s = self.broker.place_stop(p.symbol, p.qty, p.stop, ref)
        except BrokerError as exc:
            s = None
            if exc.ambiguous:
                s = next((x for x in self.broker.stops() if x.ref_id == ref), None)
            if s is None:
                self.notify("critical", f"{p.symbol} is UNPROTECTED: stop placement failed "
                                        f"({exc}). The watchdog will exit it if the stop "
                                        f"level trades.")
                self.audit.log("stop_failed", symbol=p.symbol, error=str(exc))
                return False
        p.stop_id, p.stop_kind = s.stop_id, s.kind
        self.audit.log("stop_placed", symbol=p.symbol, qty=p.qty, trigger=round(p.stop, 2),
                       kind=s.kind, stop_id=s.stop_id)
        self.save()
        return True

    def ensure_stops(self) -> None:
        """Exactly one active protective stop per position, at the right size and
        level. Missing ones are placed, duplicates cancelled, triggers ratcheted
        up (never down), and our stops on symbols we no longer hold cancelled.

        Fired stops are booked FIRST. A stop that triggered at the open looks,
        to a naive "is there an active stop?" check, exactly like a missing one -
        and re-protecting shares that were just sold is a false UNPROTECTED alarm
        at best, and at worst a sell order for shares you no longer own."""
        try:
            self._sync_stops()
            stops = self.broker.stops()
        except BrokerError as exc:
            self.audit.log("stops_unavailable", error=str(exc))
            return
        active: Dict[str, List[StopState]] = {}
        for s in stops:
            if s.state == STOP_ACTIVE:
                active.setdefault(s.symbol, []).append(s)
        for p in self.book.positions:
            if self._exiting(p.symbol):
                continue
            mine = active.get(p.symbol, [])
            keep = next((s for s in mine if s.stop_id == p.stop_id), mine[0] if mine else None)
            if keep is None:
                self._protect(p)
                continue
            for extra in mine:
                if extra is not keep:
                    try:
                        self.broker.cancel_stop(extra)
                        self.audit.log("stop_duplicate_cancelled", symbol=p.symbol,
                                       stop_id=extra.stop_id)
                    except BrokerError as exc:
                        self.audit.log("stop_cancel_failed", symbol=p.symbol, error=str(exc))
            p.stop_id, p.stop_kind = keep.stop_id, keep.kind
            if keep.ref_id:
                p.stop_ref = keep.ref_id
            if not _isnan(keep.trigger) and keep.trigger > p.stop + self.tick:
                p.stop = keep.trigger           # raised by hand at the broker: adopt it
            want = round_to_tick(p.stop, self.tick, "up")
            p.stop = want
            if keep.qty != p.qty or _isnan(keep.trigger) or keep.trigger < want - self.tick / 2:
                try:
                    s2 = self.broker.modify_stop(keep, p.qty, want)
                    p.stop_id = s2.stop_id or p.stop_id
                    self.audit.log("stop_modified", symbol=p.symbol, qty=p.qty,
                                   trigger=round(want, 2), was=keep.trigger)
                except BrokerError as exc:
                    self.notify("warn", f"{p.symbol}: could not move stop to {want:,.2f} "
                                        f"({exc}); old stop {keep.trigger} still active")
        held = {p.symbol for p in self.book.positions}
        for sym, ss in active.items():
            if sym in held:
                continue
            for s in ss:
                try:
                    self.broker.cancel_stop(s)
                    self.audit.log("stop_orphan_cancelled", symbol=sym, stop_id=s.stop_id)
                except BrokerError as exc:
                    self.audit.log("stop_orphan_cancel_failed", symbol=sym, error=str(exc))
        self.save()

    def _sync_stops(self) -> None:
        """Detect protective stops that fired at the broker and book the exit."""
        if not any(p.stop_id for p in self.book.positions):
            return
        by_id = {s.stop_id: s for s in self.broker.stops()}
        for p in list(self.book.positions):
            if not p.stop_id:
                continue
            s = by_id.get(p.stop_id)
            if s is None:
                if p.stop_kind != "day":
                    self.notify("warn", f"{p.symbol}: protective stop {p.stop_id} vanished "
                                        f"at the broker; it will be re-placed")
                p.stop_id = ""
                continue
            if s.state in (STOP_DONE, STOP_TRIGGERED):
                o = None
                if s.child_order_id:
                    try:
                        o = self.broker.get_order(s.child_order_id)
                    except BrokerError:
                        o = None
                if o is None:
                    o = self._find_sell_fill(p.symbol)
                if o is not None and o.filled_qty > 0 and (o.state == FILLED or o.terminal):
                    self._close_position(p.symbol, o.filled_qty, o.avg_price, "stop")
            elif s.state in (STOP_CANCELLED, STOP_FAILED):
                if p.stop_kind != "day":
                    self.notify("warn", f"{p.symbol}: protective stop is {s.raw_status or s.state}; "
                                        f"re-placing")
                p.stop_id = ""
        self.save()

    # ============================================================ exits
    def _exit(self, p: LivePosition, reason: str, emergency: bool = False) -> None:
        ref = make_ref_id(self.today(), p.symbol, "emergency" if emergency else "exit")
        if ref in self.st["exits"]:
            return
        if self.guard.kill_switch() and not emergency:
            self.audit.log("exit_skipped", symbol=p.symbol, why="kill switch")
            return
        if p.stop_id:
            # Cancel the protective stop FIRST. If it has already fired, the
            # shares are being sold by it - sending a second sell is how a
            # double-sell happens.
            try:
                self.broker.cancel_stop(self._stop_obj(p))
            except BrokerError as exc:
                self.audit.log("exit_aborted_stop_live", symbol=p.symbol, error=str(exc),
                               code=exc.code)
                self._sync_stops()
                return
            p.stop_id = ""
        q = self._quote(p.symbol)
        req = OrderRequest(p.symbol, SELL, p.qty, MARKET, ref, reason=reason)
        d = self.guard.check(req, q, protective=emergency)
        if not d.ok:
            self.audit.log("exit_blocked", symbol=p.symbol, why=d.reason)
            self._protect(p)
            return
        self.st["exits"][ref] = {"symbol": p.symbol, "qty": p.qty, "reason": reason,
                                 "state": "placing", "order_id": "",
                                 "placed_ts": self.clock.epoch()}
        self.save()                                             # write-ahead
        self._send(req, self.st["exits"][ref], kind="exit")
        self.guard.record(req, (q.ltp if q else 0.0) * p.qty)

    def _send(self, req: OrderRequest, rec: Dict[str, Any], kind: str) -> None:
        try:
            o = self.broker.place_order(req)
        except BrokerError as exc:
            if exc.ambiguous:
                o = self.broker.find_order_by_ref(req.ref_id)
                if o is None:
                    rec["state"] = "unknown"
                    self.audit.log(f"{kind}_ambiguous", symbol=req.symbol, ref=req.ref_id)
                    self.save()
                    return
            else:
                rec["state"] = "failed"
                rec["error"] = str(exc)
                self.audit.log(f"{kind}_failed", symbol=req.symbol, error=str(exc))
                if kind == "exit":
                    p = self._pos(req.symbol)
                    if p:
                        self._protect(p)
                    self.notify("critical", f"exit for {req.symbol} failed: {exc}")
                self.save()
                return
        rec["order_id"] = o.order_id
        rec["state"] = "working" if not o.terminal else rec["state"]
        if o.state == REJECTED:
            rec["state"] = "failed"
            rec["error"] = o.message
            self.audit.log(f"{kind}_rejected", symbol=req.symbol, why=o.message)
            if kind == "exit":
                p = self._pos(req.symbol)
                if p:
                    self._protect(p)
                self.notify("critical", f"exit for {req.symbol} rejected: {o.message}")
        else:
            rec["state"] = "working"
            self.audit.log(f"{kind}_placed", symbol=req.symbol, qty=req.qty,
                           type=req.order_type, price=round(req.price, 2) if req.price else None,
                           order_id=o.order_id, reason=req.reason)
        self.save()

    def _sync_exits(self) -> None:
        for ref, x in self.st["exits"].items():
            if x["state"] not in ("working", "unknown", "placing"):
                continue
            o = self._resolve(ref, x)
            if o is None:
                continue
            if o.state == FILLED:
                self._close_position(x["symbol"], o.filled_qty, o.avg_price, x["reason"])
                x["state"] = "done"
            elif o.state in (CANCELLED, REJECTED):
                if o.filled_qty > 0:
                    self._close_position(x["symbol"], o.filled_qty, o.avg_price, x["reason"])
                x["state"] = "failed"
                p = self._pos(x["symbol"])
                if p:
                    self._protect(p)
                    self.notify("critical", f"exit for {x['symbol']} {o.state.lower()}: "
                                            f"{o.message}; re-protected")
        self.save()

    def _resolve(self, ref: str, rec: Dict[str, Any]) -> Optional[OrderState]:
        try:
            if not rec.get("order_id"):
                o = self.broker.find_order_by_ref(ref)
                if o is None:
                    if self.clock.epoch() - rec.get("placed_ts", 0) > 120:
                        rec["state"] = "failed"
                        self.audit.log("order_never_arrived", ref=ref, symbol=rec["symbol"])
                    return None
                rec["order_id"] = o.order_id
                rec["state"] = "working"
            return self.broker.get_order(rec["order_id"])
        except BrokerError as exc:
            self.audit.log("order_lookup_failed", ref=ref, error=str(exc))
            return None

    # ========================================================== entries
    def _sizing_book(self, extra: List[Dict[str, Any]]) -> Dict[str, Position]:
        out: Dict[str, Position] = {}
        for p in self.book.positions:
            if self._exiting(p.symbol):
                continue
            out[p.symbol] = Position(
                symbol=p.symbol, sector=p.sector, qty=p.qty, entry_price=p.entry_price,
                entry_date=p.entry_date, setup=p.setup, initial_stop=p.initial_stop,
                stop=p.stop, risk_per_share=p.entry_price - p.initial_stop,
                initial_risk_value=(p.entry_price - p.initial_stop) * p.qty,
                highest_high=p.entry_price, lowest_low=p.entry_price, original_qty=p.qty)
        for e in extra:
            out[e["symbol"]] = Position(
                symbol=e["symbol"], sector=e["sector"], qty=e["qty"],
                entry_price=e["ref_px"], entry_date=self.today(), setup=e["setup"],
                initial_stop=e["stop"], stop=e["stop"],
                risk_per_share=e["ref_px"] - e["stop"],
                initial_risk_value=(e["ref_px"] - e["stop"]) * e["qty"],
                highest_high=e["ref_px"], lowest_low=e["ref_px"], original_qty=e["qty"])
        return out

    def _enter(self, line, plan: DailyPlan) -> None:
        sym, m = line.symbol, (line.meta or {})
        ref = make_ref_id(self.today(), sym, "entry")
        if ref in self.st["entries"]:
            return
        if self._pos(sym) is not None:
            self.audit.log("entry_skipped", symbol=sym, why="already held")
            return
        if self.skip_foreign and int(self.st.get("foreign", {}).get(sym, 0)) > 0:
            self.audit.log("entry_skipped", symbol=sym, why="you hold this symbol yourself")
            return
        if self.broker.find_order_by_ref(ref) is not None:
            self.audit.log("entry_exists", symbol=sym, ref=ref)
            # Sent before a crash: adopt it. The stop is anchored at fill time
            # from the stored stop distance, since the pre-crash quote is lost.
            self.st["entries"][ref] = {**self._entry_meta(m), "symbol": sym,
                                       "state": "unknown", "order_id": "", "stop": None,
                                       "placed_ts": self.clock.epoch()}
            return
        sc = float(m.get("signal_close") or 0.0)
        stop_ref = float(m.get("stop_ref") or 0.0)
        if sc <= 0 or stop_ref <= 0 or stop_ref >= sc:
            self.audit.log("entry_skipped", symbol=sym, why="plan line lacks signal metadata")
            return
        q = self._quote(sym)
        if q is None:
            self.audit.log("entry_skipped", symbol=sym, why="no quote")
            return
        opened = q.open if not _isnan(q.open) and q.open > 0 else q.ltp
        if opened / sc - 1.0 > self.max_gap or q.ltp / sc - 1.0 > self.max_gap:
            # Same rule as the backtest: chasing a gap ruins the stop distance
            # the size was built on.
            self.audit.log("entry_skipped", symbol=sym, why="gap_too_big",
                           open=round(opened, 2), signal_close=round(sc, 2))
            return
        ref_px = q.ltp
        stop = ref_px - (sc - stop_ref)                 # re-anchored, as in the engine
        equity = float(self.st.get("equity_at_close") or self.book.cash)
        risk_peak = float(self.st.get("risk_peak") or equity)
        drawdown = equity / risk_peak - 1.0 if risk_peak > 0 else 0.0
        pend = self._pending_entries()
        pend_value = sum(e["qty"] * e["ref_px"] for e in pend)
        try:
            broker_cash = self.broker.cash_available()
        except BrokerError as exc:
            self.audit.log("entry_skipped", symbol=sym, why=f"cash unavailable: {exc}")
            return
        cash = min(self.book.cash - pend_value, broker_cash)
        invested = sum(p.qty * self._mark(p, plan.as_of)
                       for p in self.book.positions if not self._exiting(p.symbol)) + pend_value
        room = max(0.0, equity * float(plan.exposure_target) - invested)
        qty, why = self.risk.size(equity, cash, ref_px, stop, self._sizing_book(pend),
                                  m.get("sector", "Other"), drawdown, room,
                                  float(self.book.risk_scale or 1.0), bool(self.book.halted))
        if qty <= 0:
            self.audit.log("entry_skipped", symbol=sym, why=why)
            return
        limit = round_to_tick(min(sc * (1 + self.max_gap), ref_px * (1 + self.entry_buffer)),
                              self.tick, "down")
        # Brokers block funds at the LIMIT price plus charges, not at the last
        # trade. Sizing against LTP let cash-constrained orders through that the
        # broker then rejected - trades the backtest took (it shrinks the
        # quantity to fit) silently went missing. Shrink here the same way.
        affordable = int(cash / (limit * (1.0 + self.cash_headroom)))
        if qty > affordable:
            self.audit.log("entry_resized_for_cash", symbol=sym, qty=qty, to=affordable,
                           cash=round(cash, 0))
            qty = affordable
        if qty <= 0 or qty * limit < self.risk.min_position_value:
            self.audit.log("entry_skipped", symbol=sym, why="not enough cash for a "
                           "worthwhile position after resizing")
            return
        qty = self.guard.apply_canary(qty)
        req = OrderRequest(sym, BUY, qty, LIMIT, ref, price=limit,
                           reason=f"{m.get('setup', '')} rank {m.get('rank', '')}")
        d = self.guard.check(req, q, cash)
        if not d.ok:
            self.audit.log("entry_blocked", symbol=sym, why=d.reason)
            return
        # Metadata FIRST, the computed values after: in a dict literal later keys
        # win, and the reverse order once silently replaced the re-anchored stop
        # with the plan's, and the quantity with 0 - which also hid pending
        # entries from the cash, exposure, sector and heat checks.
        rec = {**self._entry_meta(m), "symbol": sym, "qty": qty, "ref_px": ref_px,
               "stop": stop, "limit": limit, "state": "placing", "order_id": "",
               "placed_ts": self.clock.epoch()}
        self.st["entries"][ref] = rec
        self.save()                                             # write-ahead
        self._send(req, rec, kind="entry")
        self.guard.record(req, qty * limit)

    @staticmethod
    def _entry_meta(m: Dict[str, Any]) -> Dict[str, Any]:
        sc = float(m.get("signal_close") or 0.0)
        sr = float(m.get("stop_ref") or 0.0)
        return {"sector": m.get("sector", "Other"), "setup": m.get("setup", ""),
                "rank": int(m.get("rank") or 0), "score": float(m.get("score") or 0.0),
                "snapshot": m.get("snapshot") or {}, "signal_close": sc, "stop_ref": sr,
                "stop_dist": sc - sr, "ref_px": sc, "qty": 0}

    def _sync_entries(self, force_cancel: bool = False) -> None:
        now = self.clock.epoch()
        for ref, e in self.st["entries"].items():
            if e["state"] not in ("placing", "working", "unknown"):
                continue
            o = self._resolve(ref, e)
            if o is None:
                continue
            if o.state == FILLED:
                self._on_entry_fill(e, o.filled_qty, o.avg_price)
                continue
            if o.state in (CANCELLED, REJECTED):
                if o.filled_qty > 0:
                    self._on_entry_fill(e, o.filled_qty, o.avg_price)
                else:
                    e["state"] = "dead"
                    self.audit.log("entry_dead", symbol=e["symbol"], status=o.raw_status,
                                   why=o.message)
                continue
            if force_cancel or now - e.get("placed_ts", now) > self.entry_timeout_s:
                # The backtest fills at the open or not at all. A limit still
                # resting after the timeout means the price ran away: cancel.
                try:
                    self.broker.cancel_order(e["order_id"])
                except BrokerError as exc:
                    self.audit.log("entry_cancel_failed", symbol=e["symbol"], error=str(exc))
                try:
                    o2 = self.broker.get_order(e["order_id"])
                except BrokerError:
                    continue
                if o2.filled_qty > 0 and o2.terminal:
                    self._on_entry_fill(e, o2.filled_qty, o2.avg_price)
                elif o2.state == FILLED:
                    self._on_entry_fill(e, o2.filled_qty, o2.avg_price)
                elif o2.terminal:
                    e["state"] = "dead"
                    self.audit.log("entry_timeout", symbol=e["symbol"])
        self.save()

    def _on_entry_fill(self, e: Dict[str, Any], qty: int, price: float) -> None:
        if e.get("stop") is None:
            # adopted after a restart: anchor to the fill net of slippage, the
            # closest available stand-in for the quote the sizing used
            e["stop"] = price / (1 + self.costs.slippage_bps / 1e4) - float(e["stop_dist"])
        stop = float(e["stop"])
        if price <= stop:
            self.notify("critical", f"{e['symbol']} filled at {price:,.2f}, at or below its "
                                    f"stop {stop:,.2f}; exiting")
        charges = self.costs.charges(price, qty, BUY).total
        p = LivePosition(symbol=e["symbol"], sector=e.get("sector", "Other"), qty=int(qty),
                         entry_price=price, entry_date=self.today(), stop=stop,
                         initial_stop=stop, setup=e.get("setup", ""),
                         risk_per_share=max(price - stop, 1e-9),
                         entry_snapshot=e.get("snapshot") or {},
                         entry_rank=int(e.get("rank") or 0),
                         entry_score=float(e.get("score") or 0.0),
                         regime_at_entry=str(self.st.get("open_regime", "")))
        self.book.positions.append(p)
        self.book.cash -= price * qty + charges
        e["state"] = "filled"
        e["filled_qty"], e["fill_price"] = int(qty), price
        self.audit.log("entry_filled", symbol=p.symbol, qty=qty, price=round(price, 2),
                       stop=round(stop, 2), risk=round((price - stop) * qty, 0))
        self.notify("info", f"BOUGHT {qty} {p.symbol} @ {price:,.2f}, stop {stop:,.2f}")
        self.save()
        if price <= stop:
            self._exit(p, "filled_below_stop", emergency=True)
        else:
            self._protect(p)

    # ======================================================= book closing
    def _close_position(self, symbol: str, qty: int, price: float, reason: str) -> None:
        p = self._pos(symbol)
        if p is None or qty <= 0:
            return
        qty = min(int(qty), p.qty)
        charges = self.costs.charges(price, qty, SELL).total
        self.book.cash += price * qty - charges
        gross = (price - p.entry_price) * qty
        rps = p.risk_per_share or max(p.entry_price - p.initial_stop, 1e-9)
        r = (price - p.entry_price) / rps
        buy_charges = self.costs.charges(p.entry_price, qty, BUY).total
        row = {"symbol": p.symbol, "sector": p.sector, "setup": p.setup,
               "entry_date": p.entry_date, "exit_date": self.today(),
               "entry_price": p.entry_price, "exit_price": price, "qty": qty,
               "initial_stop": p.initial_stop, "risk_per_share": rps,
               "gross_pnl": gross, "costs": charges + buy_charges,
               "net_pnl": gross - charges - buy_charges,
               "r_multiple": (gross - charges - buy_charges) / (rps * qty),
               "bars_held": self._sessions_between(p.entry_date, self.today()),
               "exit_reason": reason, "regime_at_entry": p.regime_at_entry,
               "entry_rank": p.entry_rank, "entry_score": p.entry_score,
               "mae_r": NA, "mfe_r": NA, "live": 1, "mode": self.mode}
        for k, v in (p.entry_snapshot or {}).items():
            row[f"f_{k}"] = v
        from ..learn import Journal
        Journal(self.journal_path).append([row])
        if qty >= p.qty:
            self.book.positions = [x for x in self.book.positions if x.symbol != symbol]
        else:
            p.qty -= qty
        self.st["breach"].pop(symbol, None)
        self.audit.log("position_closed", symbol=symbol, qty=qty, price=round(price, 2),
                       r=round(r, 2), reason=reason, net=round(row["net_pnl"], 0))
        self.notify("info", f"SOLD {qty} {symbol} @ {price:,.2f} ({reason}), {r:+.2f}R")
        self.save()
        if qty < (p.qty + qty) and self._pos(symbol) is not None:
            self.ensure_stops()

    # =========================================================== watchdog
    def _watchdog(self) -> None:
        """Second line of defence behind the broker's own stop.

        A GTT can fail to fire - rejected child order, broker outage, an
        unsettled-shares rule. If price trades clearly through a stop and stays
        there past the grace period with the broker stop still ACTIVE, exit at
        market. An unprotected position gets no grace at all beyond one
        confirming poll."""
        live = [p for p in self.book.positions if not self._exiting(p.symbol)]
        if not live:
            return
        try:
            quotes = self.broker.quotes([p.symbol for p in live])
        except BrokerError as exc:
            self.audit.log("watchdog_no_quotes", error=str(exc))
            return
        now = self.clock.epoch()
        for p in live:
            q = quotes.get(p.symbol)
            if q is None:
                continue
            if q.ltp > p.stop:
                self.st["breach"].pop(p.symbol, None)
                continue
            first = self.st["breach"].setdefault(p.symbol, now)
            waited = now - first
            if not p.stop_id:
                if waited >= 0.9 * self.poll_s:
                    self.notify("critical", f"{p.symbol} traded through its stop "
                                            f"{p.stop:,.2f} with no broker stop in place; "
                                            f"exiting at market")
                    self._exit(p, "stop_unprotected", emergency=True)
                continue
            if waited >= self.grace_s and q.ltp <= p.stop * (1 - self.emergency_tol):
                try:
                    s = self.broker.get_stop(self._stop_obj(p))
                except BrokerError:
                    continue
                if s.state == STOP_ACTIVE:
                    self.notify("warn", f"{p.symbol}: broker stop {p.stop:,.2f} has not fired "
                                        f"{waited:.0f}s after price {q.ltp:,.2f} went through "
                                        f"it; exiting at market")
                    self._exit(p, "stop_watchdog", emergency=True)
        self.save()

    # ============================================================= phases
    def _latest_plan(self) -> Optional[DailyPlan]:
        files = sorted(f for f in os.listdir(self.orders_dir) if f.endswith(".plan.json"))
        if not files:
            return None
        try:
            return plan_from_json(os.path.join(self.orders_dir, files[-1]))
        except (OSError, ValueError, TypeError) as exc:
            self.notify("critical", f"could not read plan {files[-1]}: {exc}")
            return None

    def prep(self) -> None:
        self.audit.log("prep", mode=self.mode, positions=len(self.book.positions),
                       cash=round(self.book.cash, 0))
        self.st["blocked"] = ""
        try:
            self._sync_stops()
            issues = self.reconcile()
            if issues:
                self.st["blocked"] = "position mismatch: " + "; ".join(issues)
            self.ensure_stops()
        except AuthError:
            raise
        except BrokerError as exc:
            self.st["blocked"] = f"prep failed: {exc}"
            self.notify("critical", f"pre-open checks failed, entries blocked today: {exc}")
        self.save()

    def entry_blockers(self, plan: Optional[DailyPlan], late: bool) -> List[str]:
        out = []
        if self.guard.kill_switch():
            out.append("kill switch")
        if plan is None:
            out.append("no plan on disk")
        else:
            last = self.market.last_session()
            if plan.as_of != last:
                out.append(f"plan is from {plan.as_of} but the latest session is {last}")
            if plan.as_of >= self.today():
                out.append("plan is dated today or later")
        if self.st.get("blocked"):
            out.append(self.st["blocked"])
        if self.book.halted:
            out.append("drawdown circuit breaker")
        if late:
            out.append("missed the opening window")
        if self.broker.is_live:
            ok = self.guard.live_allowed()
            if not ok.ok:
                out.append(f"live trading not enabled: {ok.reason}")
        return out

    def open_session(self, late: bool = False) -> None:
        plan = self._latest_plan()
        self.st["open_regime"] = plan.regime if plan else ""
        exits = [o for o in (plan.orders if plan else []) if o.action == "SELL_EXIT"]
        buys = [o for o in (plan.orders if plan else []) if o.action == "BUY"]
        fresh_plan = plan is not None and plan.as_of == self.market.last_session()
        if exits and fresh_plan:
            for line in exits:
                p = self._pos(line.symbol)
                if p is not None:
                    self._exit(p, line.reason)
            self.broker.tick()
            self._sync_exits()
            waited = 0.0
            while waited < self.exit_settle_s and any(
                    x["state"] in ("working", "unknown") for x in self.st["exits"].values()):
                self.clock.sleep(2.0)
                waited += 2.0
                self.broker.tick()
                self._sync_exits()
        blockers = self.entry_blockers(plan, late)
        if blockers:
            if buys:
                self.audit.log("entries_skipped", n=len(buys), why=blockers)
                if not (len(blockers) == 1 and blockers[0] == "kill switch"):
                    self.notify("warn", f"skipped {len(buys)} planned entries: "
                                        + "; ".join(blockers))
        else:
            for line in buys:
                try:
                    self._enter(line, plan)
                except BrokerError as exc:
                    self.audit.log("entry_error", symbol=line.symbol, error=str(exc))
        self.broker.tick()
        self._sync_entries()
        self.ensure_stops()
        self.save()

    def monitor(self) -> None:
        self.broker.tick()
        self._sync_entries()
        self._sync_exits()
        self._sync_stops()
        self._watchdog()

    def close_session(self) -> None:
        self.broker.tick()
        self._sync_entries(force_cancel=True)
        self._sync_exits()
        self._sync_stops()
        self.save()

    def eod(self) -> Optional[DailyPlan]:
        today = self.clock.today()
        self.close_session()
        try:
            self.reconcile()
        except BrokerError as exc:
            self.notify("warn", f"end-of-day reconcile failed: {exc}")
        ok, msg = self.market.refresh(today, held=[p.symbol for p in self.book.positions])
        self.audit.log("data_refresh", ok=ok, msg=msg)
        if not ok:
            self.notify("warn", f"no plan for tomorrow: {msg}")
            return None
        as_of = self.market.last_session()
        if as_of != today.isoformat():
            self.audit.log("no_session_bar", as_of=as_of)
            return None
        ds = self.market.dataset()
        feats, rm = ds.features(self.cfg), ds.regime(self.cfg)
        self._update_risk_state(as_of, rm)

        plan = LivePlanner(self.cfg, feats, rm, entry_filter=self.entry_filter).plan(
            self.book, as_of)
        for o in plan.orders:
            if o.action == "UPDATE_STOP":
                p = self._pos(o.symbol)
                if p is not None and not _isnan(o.trigger) and o.trigger > p.stop:
                    p.stop = o.trigger
        missed = [o.symbol for o in plan.orders
                  if o.action == "SELL_EXIT" and o.reason == "stop_breached"]
        if missed:
            self.notify("critical", f"daily bars show stops traded through for {missed} but "
                                    f"no exit was recorded - check the broker. Exits are "
                                    f"scheduled for the open.")
        base = os.path.join(self.orders_dir, as_of)
        plan_to_json(plan, base + ".plan.json")
        with open(base + ".md", "w", encoding="utf-8") as fh:
            fh.write(render_plan_markdown(plan, self.cfg, ds.names,
                                          f"{self.mode} executor, data through {as_of}"))
        write_plan_csv(plan, base + ".csv")
        self.st["last_plan"] = as_of
        self.ensure_stops()
        buys = sum(1 for o in plan.orders if o.action == "BUY")
        exits = sum(1 for o in plan.orders if o.action == "SELL_EXIT")
        self.audit.log("plan_written", as_of=as_of, regime=plan.regime, buys=buys,
                       exits=exits, equity=round(self.st["equity_at_close"], 0))
        self.notify("info", f"{as_of} close: equity {self.st['equity_at_close']:,.0f}, "
                            f"{len(self.book.positions)} positions, regime {plan.regime}; "
                            f"tomorrow {buys} entries / {exits} exits planned")
        self.save()
        return plan

    def _update_risk_state(self, as_of: str, rm) -> None:
        """The backtest's circuit breaker, applied to the real equity curve."""
        equity = self.equity(as_of)
        self.book.peak_equity = max(float(self.book.peak_equity or equity), equity)
        risk_peak = max(float(self.st.get("risk_peak") or equity), equity)
        dd = equity / risk_peak - 1.0 if risk_peak > 0 else 0.0
        was = bool(self.book.halted)
        halted = self.risk.halt_state(dd, was, int(self.st.get("days_in_halt", 0)),
                                      rm.at(as_of).label != RISK_OFF)
        if halted:
            self.st["days_in_halt"] = int(self.st.get("days_in_halt", 0)) + 1 if was else 1
            self.st["days_since_resume"] = -1
        else:
            self.st["days_in_halt"] = 0
            if was:
                self.st["days_since_resume"] = 0
                risk_peak = equity
            elif int(self.st.get("days_since_resume", -1)) >= 0:
                self.st["days_since_resume"] = int(self.st["days_since_resume"]) + 1
        dsr = int(self.st.get("days_since_resume", -1))
        self.book.risk_scale = self.risk.probation_scale(dsr) if dsr >= 0 else 1.0
        self.book.halted = halted
        self.st["risk_peak"] = risk_peak
        self.st["equity_at_close"] = equity
        if halted != was:
            self.notify("critical" if halted else "warn",
                        f"circuit breaker {'ENGAGED' if halted else 'released'} at "
                        f"drawdown {100*dd:.1f}%")

    # =============================================================== status
    def status(self) -> Dict[str, Any]:
        return {
            "mode": self.mode, "session": self.st.get("session"),
            "phases_done": self.st.get("done", []), "blocked": self.st.get("blocked", ""),
            "kill_switch": self.guard.kill_switch(), "halted": self.book.halted,
            "cash": round(self.book.cash, 2), "positions": [
                {"symbol": p.symbol, "qty": p.qty, "entry": p.entry_price,
                 "stop": round(p.stop, 2), "protected": bool(p.stop_id),
                 "since": p.entry_date} for p in self.book.positions],
            "pending_entries": [e["symbol"] for e in self._pending_entries()],
            "last_plan": self.st.get("last_plan", ""),
        }


def format_exc() -> str:
    return traceback.format_exc(limit=6)
