"""Time, as the executor sees it.

Everything in the live layer asks a Clock for "now" and sleeps through it.
RealClock is the wall clock in IST. SimClock is a clock you push forward by
hand, which is how a full trading day - pre-open, open, six hours of stop
monitoring, the close, end-of-day planning - runs in a test in milliseconds,
through exactly the code that will run against a real broker.

IST is a fixed UTC+05:30 with no daylight saving, so a fixed offset is exact
and avoids depending on a tz database the host may not have.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Optional

IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")


def parse_hhmm(s: str) -> dt.time:
    parts = [int(x) for x in s.strip().split(":")]
    while len(parts) < 3:
        parts.append(0)
    return dt.time(parts[0], parts[1], parts[2])


class Clock:
    def now(self) -> dt.datetime:
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        raise NotImplementedError

    def today(self) -> dt.date:
        return self.now().date()

    def at(self, day: dt.date, hhmm: str) -> dt.datetime:
        return dt.datetime.combine(day, parse_hhmm(hhmm), tzinfo=IST)

    def epoch(self) -> float:
        return self.now().timestamp()


class RealClock(Clock):
    """Wall clock in IST. sleep() is interruptible so SIGTERM stops the daemon
    promptly instead of after its next sixty-second nap."""

    def __init__(self) -> None:
        self._stop = threading.Event()

    def now(self) -> dt.datetime:
        return dt.datetime.now(IST)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._stop.wait(seconds)

    def interrupt(self) -> None:
        self._stop.set()

    @property
    def interrupted(self) -> bool:
        return self._stop.is_set()


class SimClock(Clock):
    """A clock that only moves when told to."""

    def __init__(self, start: dt.datetime):
        if start.tzinfo is None:
            start = start.replace(tzinfo=IST)
        self._now = start.astimezone(IST)
        self.interrupted = False

    def now(self) -> dt.datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._now = self._now + dt.timedelta(seconds=seconds)

    def set(self, when: dt.datetime) -> None:
        if when.tzinfo is None:
            when = when.replace(tzinfo=IST)
        self._now = when.astimezone(IST)

    def interrupt(self) -> None:
        self.interrupted = True


def epoch_now() -> float:
    return time.time()


def session_bounds(day: dt.date, open_hhmm: str = "09:15",
                   close_hhmm: str = "15:30") -> tuple:
    return (dt.datetime.combine(day, parse_hhmm(open_hhmm), tzinfo=IST),
            dt.datetime.combine(day, parse_hhmm(close_hhmm), tzinfo=IST))


def iso(day: dt.date) -> str:
    return day.isoformat()


def maybe_date(s: Optional[str]) -> Optional[dt.date]:
    return dt.date.fromisoformat(s[:10]) if s else None
