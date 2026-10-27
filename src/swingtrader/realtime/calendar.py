"""Which days the exchange is open.

Weekends are easy. Holidays are not, and getting them wrong is not harmless: on
an exchange holiday a broker may accept an order as an after-market order and
fill it at the NEXT session's open, at a price the plan never considered.

So live mode refuses to start unless config/nse_holidays.csv lists at least one
holiday for the current year. NSE publishes the list each December; copy it in.
Nothing here guesses at holiday dates.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
from typing import Optional, Sequence, Set


class TradingCalendar:
    def __init__(self, holiday_file: Optional[str] = None,
                 sessions: Optional[Sequence[str]] = None):
        self.holidays: Set[dt.date] = set()
        self.sessions: Optional[Set[dt.date]] = (
            {dt.date.fromisoformat(d[:10]) for d in sessions} if sessions else None)
        self.source = holiday_file or ""
        if holiday_file and os.path.exists(holiday_file):
            with open(holiday_file, "r", encoding="utf-8") as fh:
                for row in csv.reader(fh):
                    if not row or row[0].strip().startswith("#") or row[0].strip() == "date":
                        continue
                    try:
                        self.holidays.add(dt.date.fromisoformat(row[0].strip()[:10]))
                    except ValueError:
                        continue

    def covers(self, year: int) -> bool:
        if self.sessions is not None:
            return True
        return any(d.year == year for d in self.holidays)

    def is_trading_day(self, day: dt.date) -> bool:
        if self.sessions is not None:           # replay: the data is the calendar
            return day in self.sessions
        return day.weekday() < 5 and day not in self.holidays

    def next_trading_day(self, day: dt.date) -> dt.date:
        d = day + dt.timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(d):
                return d
            d += dt.timedelta(days=1)
        return d
