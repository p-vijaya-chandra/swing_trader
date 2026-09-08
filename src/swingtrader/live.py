"""Live operation: position book and the daily order plan.

The output of this module is a sheet you work through in the Groww app after
the close, before the next session. It is deliberately not an auto-trader:
Groww's retail API access is not something to assume, and on a Rs 1L account
the value of automation is small next to the cost of an unattended bug.

What it does guarantee is that the orders it prints came out of the same rules
the backtest validated - the exit logic is imported from `rules.py`, not
reimplemented here.
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .config import Config
from .costs import CostModel
from .features import Features
from .portfolio import RiskManager
from .regime import RISK_OFF, RegimeState
from .rules import close_exit_reason, ratchet, trail_stop_level
from .strategy import Candidate, SwingStrategy
from .util import NA, fmt_inr, is_na


@dataclass
class LivePosition:
    symbol: str
    sector: str
    qty: int
    entry_price: float
    entry_date: str
    stop: float
    initial_stop: float
    setup: str = ""
    risk_per_share: float = 0.0
    entry_snapshot: Dict[str, float] = field(default_factory=dict)
    note: str = ""


@dataclass
class PositionBook:
    capital_base: float = 100000.0
    cash: float = 100000.0
    positions: List[LivePosition] = field(default_factory=list)
    halted: bool = False
    peak_equity: float = 0.0
    risk_scale: float = 1.0

    @classmethod
    def load(cls, path: str, capital: float = 100000.0) -> "PositionBook":
        if not os.path.exists(path):
            return cls(capital_base=capital, cash=capital, peak_equity=capital)
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        pos = [LivePosition(**p) for p in d.get("positions", [])]
        return cls(capital_base=d.get("capital_base", capital),
                   cash=d.get("cash", capital), positions=pos,
                   halted=d.get("halted", False),
                   peak_equity=d.get("peak_equity", capital),
                   risk_scale=d.get("risk_scale", 1.0))

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"capital_base": self.capital_base, "cash": self.cash,
                       "halted": self.halted, "peak_equity": self.peak_equity,
                       "risk_scale": self.risk_scale,
                       "positions": [asdict(p) for p in self.positions]},
                      fh, indent=2, sort_keys=True)

    def get(self, symbol: str) -> Optional[LivePosition]:
        for p in self.positions:
            if p.symbol == symbol:
                return p
        return None

    def equity(self, price_of) -> float:
        total = self.cash
        for p in self.positions:
            px = price_of(p.symbol)
            total += p.qty * (p.entry_price if is_na(px) else px)
        return total


@dataclass
class OrderLine:
    action: str                 # SELL_EXIT / UPDATE_STOP / BUY / PLACE_STOP
    symbol: str
    qty: int
    order_type: str             # MARKET / LIMIT / GTT-SL
    price: float = NA
    trigger: float = NA
    reason: str = ""
    detail: str = ""
    value: float = NA
    risk: float = NA


@dataclass
class DailyPlan:
    as_of: str
    regime: str
    regime_reason: str
    exposure_target: float
    equity: float
    cash: float
    halted: bool
    orders: List[OrderLine] = field(default_factory=list)
    holdings: List[Dict[str, Any]] = field(default_factory=list)
    watchlist: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


class LivePlanner:
    def __init__(self, cfg: Config, feats: Dict[str, Features], regime_model,
                 entry_filter=None):
        self.cfg = cfg
        self.feats = feats
        self.regime_model = regime_model
        self.strategy = SwingStrategy(cfg)
        self.risk = RiskManager(cfg)
        self.costs = CostModel(cfg)
        self.entry_filter = entry_filter

    def _px(self, sym: str, date: str, field_: str = "close") -> float:
        f = self.feats.get(sym)
        if f is None:
            return NA
        i = f.series.pos(date)
        if i is None:
            return NA
        return getattr(f.series, field_)[i]

    def _replay_stop(self, f: Features, p: LivePosition, j: Optional[int], i: int,
                     regime_label: str = "") -> tuple:
        """Walk the trailing stop forward from entry to `as_of`.

        Returns (stop_now, bars_held, breach) where breach is None or
        (date, stop_level_at_that_moment). The manually recorded stop is honoured
        as a floor, so a stop you raised by hand is never quietly walked down.
        """
        if j is None or j > i:
            return p.stop, 0, None
        stop = p.initial_stop or p.stop
        hh = f.series.high[j]
        breach = None
        for k in range(j, i + 1):
            if k > j and f.series.low[k] <= stop and breach is None:
                breach = (f.series.dates[k], stop)
            hh = max(hh, f.series.high[k])
            atr_k = f.get("atr", k)
            if not is_na(atr_k) and atr_k > 0:
                # The regime label on THAT day, not today's. The trail tightens to
                # 1.5 ATR whenever the index is risk-off, so replaying the whole
                # hold under today's label computes a looser stop than the one the
                # engine actually held - and then disagrees with it about when the
                # position was stopped out.
                label = self.regime_model.at(f.series.dates[k]).label
                stop = ratchet(stop, trail_stop_level(hh, atr_k, label, self.cfg))
        return max(stop, p.stop), i - j, breach

    def plan(self, book: PositionBook, as_of: str) -> DailyPlan:
        regime: RegimeState = self.regime_model.at(as_of)
        equity = book.equity(lambda s: self._px(s, as_of))
        peak = max(book.peak_equity or equity, equity)
        drawdown = equity / peak - 1.0 if peak > 0 else 0.0

        plan = DailyPlan(as_of=as_of, regime=regime.label,
                         regime_reason=regime.reason,
                         exposure_target=regime.exposure, equity=equity,
                         cash=book.cash, halted=book.halted)

        if regime.label == RISK_OFF:
            plan.notes.append(
                "RISK-OFF: no new positions. Existing holdings trail on a tightened "
                f"{self.cfg.get('exit.trail_atr_mult_riskoff')} ATR stop.")
        if book.halted:
            plan.notes.append(
                "CIRCUIT BREAKER ACTIVE: no new positions until the drawdown "
                f"recovers past -{100*float(self.cfg.get('risk.resume_on_drawdown',0.10)):.0f}% "
                f"or {self.cfg.get('risk.halt_cooldown_days')} sessions pass with a "
                "risk-on regime.")

        ranks: Dict[str, int] = {}

        if int(self.cfg.get("exit.rank_exit_threshold", 0)) > 0:
            ranks = {c.symbol: c.rank
                     for c in self.strategy.rank_universe(self.feats, as_of, equity)}

        # ------------------------------------------------------- open holdings
        exiting = set()
        for p in book.positions:
            f = self.feats.get(p.symbol)
            i = f.series.pos(as_of) if f else None
            if i is None:
                plan.notes.append(f"{p.symbol}: no bar for {as_of} - check the data feed.")
                continue
            close = f.series.close[i]
            j = f.series.pos(p.entry_date)

            # Replay the trail bar by bar from entry, exactly as the backtest
            # does, rather than computing it once from the running high. Those
            # two are NOT the same thing: a position that ran up and then fell
            # back produces a one-shot trail sitting ABOVE the current price -
            # i.e. a stop that would already have fired days ago. Replaying
            # catches that and tells you your book is out of sync, instead of
            # printing an impossible stop and moving on.
            new_stop, bars_held, breach = self._replay_stop(f, p, j, i)

            if breach is not None:
                breach_date, breach_stop = breach
                # Show the level that actually fired, not today's ratcheted value.
                new_stop = breach_stop
                plan.notes.append(
                    f"{p.symbol}: the trailing stop at {breach_stop:,.2f} was breached "
                    f"on {breach_date}. If the GTT was live you are already flat - "
                    f"record it with `swing position close {p.symbol} --price <fill> "
                    f"--date {breach_date} --reason stop`. Listed as an exit below so "
                    f"the book cannot silently drift out of sync with your account.")

            atr = f.get("atr", i)
            rps = p.risk_per_share or max(p.entry_price - p.initial_stop, 1e-9)
            r_now = (close - p.entry_price) / rps

            below = 0
            ema_n = int(self.cfg.get("exit.momentum_exit_ema", 20))
            del ema_n
            for k in range(i, max(j or 0, i - 10) - 1, -1):
                e = f.get("ema_pull", k)
                if is_na(e) or f.series.close[k] >= e:
                    break
                below += 1

            reason = close_exit_reason(self.cfg, bars_held, r_now, below,
                                       close > p.entry_price, ranks.get(p.symbol))
            if breach is not None:
                reason = "stop_breached"

            plan.holdings.append({
                "symbol": p.symbol, "qty": p.qty, "entry": p.entry_price,
                "last": close, "stop": new_stop, "r": r_now,
                "bars": bars_held, "pnl": (close - p.entry_price) * p.qty,
                "pnl_pct": close / p.entry_price - 1.0,
                "action": reason or ("trail" if new_stop > p.stop else "hold")})

            if reason:
                exiting.add(p.symbol)
                plan.orders.append(OrderLine(
                    action="SELL_EXIT", symbol=p.symbol, qty=p.qty,
                    order_type="MARKET", price=close, reason=reason,
                    detail=f"held {bars_held} sessions, {r_now:+.2f}R",
                    value=close * p.qty))
            elif new_stop > p.stop + 1e-9:
                plan.orders.append(OrderLine(
                    action="UPDATE_STOP", symbol=p.symbol, qty=p.qty,
                    order_type="GTT-SL", trigger=new_stop, price=close,
                    reason="trail",
                    detail=f"raise stop {p.stop:,.2f} -> {new_stop:,.2f} "
                           f"(locks {((new_stop-p.entry_price)/rps):+.2f}R)"))

        # ------------------------------------------------------- new entries
        open_syms = {p.symbol for p in book.positions} - exiting
        n_open = len(open_syms)
        invested = sum(p.qty * (self._px(p.symbol, as_of) if not is_na(self._px(p.symbol, as_of))
                                else p.entry_price)
                       for p in book.positions if p.symbol not in exiting)
        exposure_room = max(0.0, equity * regime.exposure - invested)

        candidates = self.strategy.rank_universe(self.feats, as_of, equity)
        top_n = int(self.cfg.get("rank.top_n", 25))
        for c in candidates[:top_n]:
            f = self.feats[c.symbol]
            i = f.series.pos(as_of)
            setup = self.strategy.trigger(f, i) if i is not None else None
            plan.watchlist.append({
                "rank": c.rank, "symbol": c.symbol, "score": c.score,
                "close": c.close, "stop": c.stop_ref, "sector": c.sector,
                "triggered": setup or "", "held": c.symbol in open_syms,
                "atr_pct": c.snapshot.get("atr_pct", NA),
                "stop_pct": (c.close - c.stop_ref) / c.close if c.close else NA})

        if regime.label == RISK_OFF or book.halted:
            return plan

        sector_counts: Dict[str, int] = {}
        for p in book.positions:
            if p.symbol not in exiting:
                sector_counts[p.sector] = sector_counts.get(p.sector, 0) + 1

        max_new = int(self.cfg.get("entry.max_new_per_day", 3))
        taken = 0
        cash_left = book.cash + sum(o.value for o in plan.orders
                                    if o.action == "SELL_EXIT" and not is_na(o.value))
        sim_positions: Dict[str, Any] = {}

        for c in candidates:
            if taken >= max_new or n_open >= self.risk.max_positions:
                break
            if c.symbol in open_syms or c.symbol in exiting:
                continue
            if c.score < float(self.cfg.get("entry.min_score_z", 0.0)):
                continue
            f = self.feats[c.symbol]
            i = f.series.pos(as_of)
            if i is None:
                continue
            setup = self.strategy.trigger(f, i)
            if setup is None:
                continue
            c.setup = setup
            if self.entry_filter is not None and not self.entry_filter(
                    c, {"regime": regime.label, "drawdown": drawdown,
                        "n_positions": n_open}):
                continue
            if sector_counts.get(c.sector, 0) >= self.risk.max_sector:
                continue

            # Size against the SIGNAL close. The actual fill is tomorrow's open,
            # so the sheet also carries a max price beyond which the trade is off.
            qty, why = self.risk.size(equity, cash_left, c.close, c.stop_ref,
                                      sim_positions, c.sector, drawdown,
                                      exposure_room, book.risk_scale, book.halted)
            if qty <= 0:
                continue

            max_gap = float(self.cfg.get("entry.max_gap_pct", 0.03))
            limit = c.close * (1.0 + max_gap)
            risk_rs = (c.close - c.stop_ref) * qty
            plan.orders.append(OrderLine(
                action="BUY", symbol=c.symbol, qty=qty, order_type="LIMIT",
                price=limit, trigger=c.stop_ref, reason=f"{setup} (rank {c.rank})",
                detail=(f"score {c.score:+.2f}, ATR {100*c.snapshot.get('atr_pct', 0):.1f}%, "
                        f"stop {100*(c.close-c.stop_ref)/c.close:.1f}% away; "
                        f"SKIP if it opens above {limit:,.2f}"),
                value=c.close * qty, risk=risk_rs))
            plan.orders.append(OrderLine(
                action="PLACE_STOP", symbol=c.symbol, qty=qty, order_type="GTT-SL",
                trigger=c.stop_ref, reason="initial stop",
                detail="place immediately after the buy fills"))

            cash_left -= c.close * qty
            exposure_room = max(0.0, exposure_room - c.close * qty)
            sector_counts[c.sector] = sector_counts.get(c.sector, 0) + 1
            n_open += 1
            taken += 1

        if not any(o.action == "BUY" for o in plan.orders):
            plan.notes.append("No new entries qualify today. Doing nothing is a position.")
        return plan


# ------------------------------------------------------------------ rendering
def render_plan_markdown(plan: DailyPlan, cfg: Config, names: Dict[str, str],
                         universe_note: str = "") -> str:
    L: List[str] = []
    L.append(f"# Order plan for the session after {plan.as_of}")
    L.append("")
    L.append(f"- **Regime**: `{plan.regime}` - {plan.regime_reason}")
    L.append(f"- **Target gross exposure**: {100*plan.exposure_target:.0f}% of equity")
    L.append(f"- **Equity**: Rs {fmt_inr(plan.equity)}   **Cash**: Rs {fmt_inr(plan.cash)}")
    if plan.halted:
        L.append("- **CIRCUIT BREAKER: ACTIVE**")
    if universe_note:
        L.append(f"- Data: {universe_note}")
    L.append("")

    for n in plan.notes:
        L.append(f"> {n}")
    if plan.notes:
        L.append("")

    sells = [o for o in plan.orders if o.action == "SELL_EXIT"]
    stops = [o for o in plan.orders if o.action == "UPDATE_STOP"]
    buys = [o for o in plan.orders if o.action == "BUY"]
    newstops = {o.symbol: o for o in plan.orders if o.action == "PLACE_STOP"}

    L.append("## 1. Exits - place at market on open")
    if sells:
        L.append("")
        L.append("| Symbol | Qty | Approx value | Why |")
        L.append("|---|---:|---:|---|")
        for o in sells:
            L.append(f"| {o.symbol} | {o.qty} | Rs {fmt_inr(o.value)} | {o.reason} ({o.detail}) |")
    else:
        L.append("")
        L.append("_Nothing to exit._")
    L.append("")

    L.append("## 2. Stop updates - modify the existing GTT")
    if stops:
        L.append("")
        L.append("| Symbol | Qty | New trigger | Change |")
        L.append("|---|---:|---:|---|")
        for o in stops:
            L.append(f"| {o.symbol} | {o.qty} | Rs {fmt_inr(o.trigger)} | {o.detail} |")
    else:
        L.append("")
        L.append("_No stop changes._")
    L.append("")

    L.append("## 3. New entries")
    if buys:
        L.append("")
        L.append("| Symbol | Qty | Buy limit | Stop (GTT) | Value | Risk | Why |")
        L.append("|---|---:|---:|---:|---:|---:|---|")
        for o in buys:
            st = newstops.get(o.symbol)
            L.append(f"| {o.symbol} | {o.qty} | Rs {fmt_inr(o.price)} | "
                     f"Rs {fmt_inr(st.trigger if st else o.trigger)} | "
                     f"Rs {fmt_inr(o.value)} | Rs {fmt_inr(o.risk)} | {o.reason} |")
        L.append("")
        for o in buys:
            L.append(f"- **{o.symbol}** ({names.get(o.symbol, o.symbol)}): {o.detail}")
    else:
        L.append("")
        L.append("_No new entries._")
    L.append("")

    L.append("## 4. Current holdings")
    if plan.holdings:
        L.append("")
        L.append("| Symbol | Qty | Entry | Last | P&L | R | Stop | Held | Action |")
        L.append("|---|---:|---:|---:|---:|---:|---:|---:|---|")
        for h in sorted(plan.holdings, key=lambda x: -x["r"]):
            L.append(f"| {h['symbol']} | {h['qty']} | {h['entry']:,.2f} | {h['last']:,.2f} | "
                     f"Rs {fmt_inr(h['pnl'])} ({100*h['pnl_pct']:+.1f}%) | {h['r']:+.2f} | "
                     f"{h['stop']:,.2f} | {h['bars']}d | {h['action']} |")
    else:
        L.append("")
        L.append("_Flat._")
    L.append("")

    L.append("## 5. Watchlist (ranked, top of the screen)")
    L.append("")
    L.append("| # | Symbol | Score | Close | Stop | Stop % | ATR % | Sector | Triggered |")
    L.append("|---:|---|---:|---:|---:|---:|---:|---|---|")
    for w in plan.watchlist[:15]:
        L.append(f"| {w['rank']} | {w['symbol']}{' *held*' if w['held'] else ''} | "
                 f"{w['score']:+.2f} | {w['close']:,.2f} | {w['stop']:,.2f} | "
                 f"{100*w['stop_pct']:.1f}% | {100*(w['atr_pct'] or 0):.1f}% | "
                 f"{w['sector']} | {w['triggered'] or '-'} |")
    L.append("")
    L.append("---")
    L.append("")
    L.append("### How to work this sheet in Groww")
    L.append("")
    L.append("1. Do section 1 first (exits) - it frees the cash the buys need.")
    L.append("2. Then section 2 (stop updates). These are GTT modifications, not new orders.")
    L.append("3. Then section 3. Use a **limit** order at the stated price, never market:")
    L.append("   the limit is what stops you paying up for a gap that has already moved")
    L.append("   past the setup. If it does not fill, it does not fill - that is the rule")
    L.append("   working, not a missed trade.")
    L.append("4. The moment a buy fills, place its GTT stop. A position without a stop is")
    L.append("   the only genuinely unbounded risk in this system.")
    L.append("5. Record fills: `swing position add SYMBOL --qty N --price P --date YYYY-MM-DD`")
    L.append("")
    L.append("_Orders are generated by the same rules the backtest validated "
             "(`swingtrader/rules.py`). Prices are from the close of "
             f"{plan.as_of}; treat every limit as a ceiling, not a target._")
    return "\n".join(L)


def write_plan_csv(plan: DailyPlan, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["as_of", "action", "symbol", "qty", "order_type", "limit_price",
                    "stop_trigger", "reason", "detail", "value", "risk"])
        for o in plan.orders:
            w.writerow([plan.as_of, o.action, o.symbol, o.qty, o.order_type,
                        "" if is_na(o.price) else f"{o.price:.2f}",
                        "" if is_na(o.trigger) else f"{o.trigger:.2f}",
                        o.reason, o.detail,
                        "" if is_na(o.value) else f"{o.value:.2f}",
                        "" if is_na(o.risk) else f"{o.risk:.2f}"])
