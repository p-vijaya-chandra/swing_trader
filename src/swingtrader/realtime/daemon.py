"""The trading-day loop.

    08:50  prep     re-login, sync fired stops, reconcile with broker holdings,
                    make sure every position has exactly one protective stop
    09:15  open     exits at market, then entries (gap rule, re-sized, limit)
    ...    monitor  every poll: fills, stop fires, watchdog
    15:31  close    cancel unfilled entries, final sync
    16:15  eod      refresh + validate data, update the circuit breaker, write
                    tomorrow's plan, ratchet trailing stops at the broker

Each phase runs once per session, and completion is persisted, so the process
can be killed and restarted at any time: it picks up where it left off without
repeating an open. If it restarts after the opening window it executes exits
only - an entry at 11:00 is not the trade the backtest measured.

A failing phase is retried until its window closes, then abandoned with an
alert. The loop itself never dies on a broker error; protective stops live at
the broker and do not depend on it.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from typing import Callable, Dict, Optional

from ..broker.base import AuthError, BrokerError
from .calendar import TradingCalendar
from .clock import IST, Clock
from .executor import LiveExecutor, format_exc

PHASES = ("prep", "open", "close", "eod")


class Daemon:
    def __init__(self, executor: LiveExecutor, clock: Clock, calendar: TradingCalendar,
                 cfg, heartbeat_path: Optional[str] = None):
        e = cfg.get("execution", {}) or {}
        s = e.get("schedule", {}) or {}
        self.ex = executor
        self.clock = clock
        self.cal = calendar
        self.times = {"prep": s.get("prep", "08:50"), "open": s.get("open", "09:15:05"),
                      "open_deadline": s.get("open_deadline", "09:45"),
                      "close": s.get("close", "15:31"), "eod": s.get("eod", "16:15"),
                      "eod_deadline": s.get("eod_deadline", "23:00")}
        self.poll_s = float(e.get("poll_s", 30))
        self.retry_s = float(e.get("phase_retry_s", 60))
        self.heartbeat_path = heartbeat_path or os.path.join(executor.state_dir, "heartbeat.json")
        self._retry_after: Dict[str, float] = {}

    # ------------------------------------------------------------------ util
    def _t(self, day: dt.date) -> Dict[str, dt.datetime]:
        return {k: self.clock.at(day, v) for k, v in self.times.items()}

    def _window_end(self, phase: str, t: Dict[str, dt.datetime]) -> dt.datetime:
        # prep may run late but must never be skipped: the open depends on its
        # reconciliation, so a 09:20 restart runs prep THEN open, in that order.
        return {"prep": t["close"], "open": t["close"], "close": t["eod"],
                "eod": t["eod_deadline"]}[phase]

    def _heartbeat(self) -> None:
        hb = {"ts": self.clock.now().isoformat(timespec="seconds"),
              "wall": time.time(), "mode": self.ex.mode,
              "session": self.ex.st.get("session"), "done": self.ex.st.get("done", []),
              "positions": len(self.ex.book.positions)}
        tmp = self.heartbeat_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(hb, fh)
        os.replace(tmp, self.heartbeat_path)

    def _guarded(self, name: str, fn: Callable[[], object]) -> bool:
        try:
            fn()
            return True
        except AuthError as exc:
            self.ex.notify("critical", f"{name}: broker login failed ({exc}). Retrying.")
            self.ex.audit.log("phase_auth_error", phase=name, error=str(exc))
        except BrokerError as exc:
            self.ex.notify("warn", f"{name}: broker error ({exc}). Retrying.")
            self.ex.audit.log("phase_broker_error", phase=name, error=str(exc))
        except Exception as exc:                                # noqa: BLE001
            self.ex.notify("critical", f"{name}: unexpected {type(exc).__name__}: {exc}")
            self.ex.audit.log("phase_crash", phase=name, trace=format_exc())
        return False

    # ------------------------------------------------------------------- run
    def step(self) -> Optional[dt.datetime]:
        """Run whatever is due now. Returns when to wake next."""
        now = self.clock.now()
        day = now.date()
        if not self.cal.is_trading_day(day):
            return dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 1), tzinfo=IST)

        self.ex.begin_session(day)
        t = self._t(day)
        done = self.ex.st["done"]
        for phase in PHASES:
            if phase in done or now < t[phase]:
                continue
            if self.clock.epoch() < self._retry_after.get(phase, 0):
                continue
            if now >= self._window_end(phase, t):
                done.append(phase)
                self.ex.audit.log("phase_missed", phase=phase)
                if phase in ("prep", "eod"):
                    self.ex.notify("warn", f"{phase} did not complete in its window")
                continue
            if phase == "open" and "prep" not in done:
                break                    # prep failed this pass; never open without it
            if phase == "open":
                late = now > t["open_deadline"]
                ok = self._guarded("open", lambda: self.ex.open_session(late=late))
            else:
                fn = {"prep": self.ex.prep, "close": self.ex.close_session,
                      "eod": self.ex.eod}[phase]
                ok = self._guarded(phase, fn)
            if ok:
                done.append(phase)
                self.ex.save()
            else:
                self._retry_after[phase] = self.clock.epoch() + self.retry_s
            now = self.clock.now()

        in_session = t["open"] <= now < t["close"]
        if in_session and "open" in done and "close" not in done:
            self._guarded("monitor", self.ex.monitor)
        self._heartbeat()

        nxt = [t[p] for p in PHASES if p not in done and t[p] > now]
        nxt += [dt.datetime.fromtimestamp(v, IST) for p, v in self._retry_after.items()
                if p not in done and v > self.clock.epoch()]
        if in_session and "close" not in done:
            nxt.append(now + dt.timedelta(seconds=self.poll_s))
        if not nxt:
            nxt.append(dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 1), tzinfo=IST))
        return min(nxt)

    def run(self, until: Optional[dt.datetime] = None) -> None:
        while not getattr(self.clock, "interrupted", False):
            if until is not None and self.clock.now() >= until:
                break
            wake = self.step()
            if until is not None and wake > until:
                wake = until
            delay = (wake - self.clock.now()).total_seconds()
            self.clock.sleep(max(1.0, delay))
