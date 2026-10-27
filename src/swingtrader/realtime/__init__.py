"""Real-time execution: the same strategy, driven by a clock instead of a loop
over history, placing orders through a broker interface."""
from __future__ import annotations

import datetime as dt
import os
from typing import Iterable, Optional, Tuple

from ..config import Config
from ..costs import CostModel
from .audit import Audit, Notifier
from .calendar import TradingCalendar
from .clock import IST, RealClock, SimClock
from .daemon import Daemon
from .executor import LiveExecutor
from .guard import RiskGuard
from .market import CacheMarket, ReplayMarket

__all__ = ["build_executor", "build_replay", "build_live", "Daemon", "LiveExecutor",
           "SimClock", "RealClock", "IST"]


def build_executor(cfg: Config, broker, clock, market, state_dir: str, orders_dir: str,
                   universe: Optional[Iterable[str]] = None, echo: bool = True,
                   entry_filter=None) -> LiveExecutor:
    e = cfg.get("execution", {}) or {}
    guard = RiskGuard(cfg, state_dir, clock, set(universe or ()))
    audit = Audit(state_dir, clock, echo=echo)
    notify = Notifier(state_dir, clock, min_level=str(e.get("notify_level", "warn")))
    return LiveExecutor(cfg, broker, clock, market, guard, audit, notify,
                        state_dir=state_dir, orders_dir=orders_dir, entry_filter=entry_filter)


def build_replay(cfg: Config, ds, start: dt.date, state_dir: str, orders_dir: str,
                 echo: bool = False, gtt_enabled: bool = True) -> Tuple:
    """A complete live system over replayed bars: SimClock, intraday price path,
    paper broker, replay market data. Nothing here is mocked except time and
    the exchange; the executor and daemon are the production code."""
    from ..broker.paper import PaperBroker
    from .replay import BarReplayFeed

    os.makedirs(state_dir, exist_ok=True)
    clock = SimClock(dt.datetime.combine(start, dt.time(8, 0), tzinfo=IST))
    series = dict(ds.series)
    if ds.index is not None:
        series[cfg.get("regime.index_symbol", "NIFTY100")] = ds.index
    feed = BarReplayFeed(series, clock)
    broker = PaperBroker(feed.quotes, clock, CostModel(cfg),
                         cash=float(cfg.get("capital", 100000.0)),
                         state_path=os.path.join(state_dir, "paper_broker.json"))
    if not gtt_enabled:
        broker.tick_stops = False
    market = ReplayMarket(ds, clock)
    cal = TradingCalendar(sessions=ds.all_dates())
    ex = build_executor(cfg, broker, clock, market, state_dir, orders_dir,
                        universe=ds.series.keys(), echo=echo)
    return Daemon(ex, clock, cal, cfg), ex, broker, clock


def build_live(cfg: Config, broker, state_dir: str, orders_dir: str,
               universe: Iterable[str], echo: bool = True) -> Tuple:
    clock = RealClock()
    market = CacheMarket(cfg)
    cal = TradingCalendar(cfg.get("execution.holiday_file"))
    ex = build_executor(cfg, broker, clock, market, state_dir, orders_dir,
                        universe=universe, echo=echo)
    return Daemon(ex, clock, cal, cfg), ex, clock, cal
