"""Broker abstraction shared by the paper broker and the Groww adapter.

The executor only ever talks to this interface. That is what lets the exact
same order logic run against a simulated exchange in tests, against live prices
with fake fills (shadow mode), and against real money - and it is why a bug
found in paper trading is a bug fixed for live trading.

Three ideas carry most of the safety:

  DETERMINISTIC REFERENCE IDS. Every order the system intends to place gets an
  id derived from (session, symbol, purpose). If the process dies between
  sending an order and recording it, the restart recomputes the same id, asks
  the broker "do you already have this?", and does not place it twice.

  AMBIGUITY IS A FIRST-CLASS ERROR. A timeout on a write does not mean the
  order failed - it means you do not know. BrokerError(ambiguous=True) forces
  the caller to look the order up by reference before doing anything else.

  PARSE FAILURES NEVER DEFAULT TO EMPTY. If a holdings response cannot be read,
  the answer is an exception, not "you hold nothing". An executor that believes
  every position vanished will try to reconcile that belief into the account.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Sequence

NA = float("nan")

BUY, SELL = "BUY", "SELL"
MARKET, LIMIT, SL, SL_M = "MARKET", "LIMIT", "SL", "SL_M"

# Normalised order lifecycle
WORKING, FILLED, CANCELLED, REJECTED = "WORKING", "FILLED", "CANCELLED", "REJECTED"
TERMINAL = frozenset({FILLED, CANCELLED, REJECTED})

# Normalised protective-stop lifecycle
STOP_ACTIVE, STOP_TRIGGERED, STOP_DONE, STOP_CANCELLED, STOP_FAILED = (
    "ACTIVE", "TRIGGERED", "DONE", "CANCELLED", "FAILED")

# Groww's documented order statuses (Trade API annexures). Anything not listed
# is treated as WORKING - an unrecognised order is assumed able to fill, which
# is the conservative assumption when the alternative is placing a duplicate.
GROWW_ORDER_STATUS = {
    "NEW": WORKING, "ACKED": WORKING, "APPROVED": WORKING,
    "TRIGGER_PENDING": WORKING, "MODIFICATION_REQUESTED": WORKING,
    "CANCELLATION_REQUESTED": WORKING, "OPEN": WORKING, "PENDING": WORKING,
    "NA": WORKING,
    "EXECUTED": FILLED, "DELIVERY_AWAITED": FILLED, "COMPLETED": FILLED,
    "CANCELLED": CANCELLED,
    "REJECTED": REJECTED, "FAILED": REJECTED,
}

GROWW_SMART_STATUS = {
    "ACTIVE": STOP_ACTIVE, "TRIGGERED": STOP_TRIGGERED, "COMPLETED": STOP_DONE,
    "CANCELLED": STOP_CANCELLED, "EXPIRED": STOP_CANCELLED, "FAILED": STOP_FAILED,
}


class BrokerError(Exception):
    """retryable: safe to try again. ambiguous: a write may or may not have
    happened - look it up by reference before doing anything else."""

    def __init__(self, msg: str, retryable: bool = False, ambiguous: bool = False,
                 code: str = ""):
        super().__init__(msg)
        self.retryable = retryable
        self.ambiguous = ambiguous
        self.code = code


class AuthError(BrokerError):
    pass


@dataclass
class Quote:
    symbol: str
    ltp: float
    open: float = NA
    high: float = NA
    low: float = NA
    prev_close: float = NA
    ts: float = 0.0            # epoch seconds of the price; 0 = unknown

    def valid(self) -> bool:
        return self.ltp is not None and not math.isnan(self.ltp) and self.ltp > 0


@dataclass
class Holding:
    symbol: str
    qty: int
    avg_price: float = 0.0


@dataclass
class OrderRequest:
    symbol: str
    side: str
    qty: int
    order_type: str
    ref_id: str
    price: float = 0.0
    trigger_price: Optional[float] = None
    reason: str = ""


@dataclass
class OrderState:
    order_id: str
    ref_id: str
    symbol: str
    side: str
    qty: int
    order_type: str
    state: str
    raw_status: str = ""
    filled_qty: int = 0
    avg_price: float = 0.0
    price: float = 0.0
    trigger_price: Optional[float] = None
    message: str = ""
    ts: float = 0.0

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class StopState:
    stop_id: str
    ref_id: str
    symbol: str
    qty: int
    trigger: float
    state: str
    kind: str = "gtt"           # "gtt" (persists) or "day" (SL-M, expires at close)
    raw_status: str = ""
    child_order_id: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def normalise_order_state(raw_status: str, filled_qty: int, qty: int) -> str:
    """Map a broker status onto the normalised lifecycle, letting the fill
    quantity overrule the label when they disagree."""
    st = GROWW_ORDER_STATUS.get((raw_status or "").upper().strip(), WORKING)
    if st == FILLED and qty and 0 < filled_qty < qty:
        # A "complete" label on a partial fill is a broker quirk we must not
        # paper over: the unfilled remainder is not a position.
        return WORKING
    return st


def make_ref_id(*parts: object) -> str:
    """Deterministic, broker-safe reference: 'ST' + 14 upper-case hex chars.

    16 alphanumeric characters with no separators fits every reference-id
    constraint Indian brokers publish, and the 'ST' prefix is how the system
    recognises its own orders among anything else in the account.
    """
    digest = hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return "ST" + digest[:14].upper()


def is_our_ref(ref_id: str) -> bool:
    return bool(ref_id) and ref_id.startswith("ST") and len(ref_id) == 16


def round_to_tick(price: float, tick: float = 0.05, mode: str = "nearest") -> float:
    """Exchange prices must sit on the tick grid or the order is rejected.

    Direction matters: a BUY limit rounds DOWN (never pay more than intended), a
    SELL stop trigger rounds UP (never give the trade more room than intended).
    """
    if tick <= 0 or price is None or math.isnan(price):
        return price
    n = price / tick
    if mode == "down":
        n = math.floor(n + 1e-9)
    elif mode == "up":
        n = math.ceil(n - 1e-9)
    else:
        n = round(n)
    return round(n * tick, 2)


class Broker:
    """Interface. Implementations: PaperBroker, GrowwBroker."""

    name = "abstract"
    is_live = False

    # ---- account ------------------------------------------------------------
    def holdings(self) -> Dict[str, Holding]:
        raise NotImplementedError

    def cash_available(self) -> float:
        raise NotImplementedError

    # ---- market data --------------------------------------------------------
    def quotes(self, symbols: Sequence[str]) -> Dict[str, Quote]:
        raise NotImplementedError

    # ---- orders -------------------------------------------------------------
    def place_order(self, req: OrderRequest) -> OrderState:
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> OrderState:
        raise NotImplementedError

    def get_order(self, order_id: str) -> OrderState:
        raise NotImplementedError

    def find_order_by_ref(self, ref_id: str) -> Optional[OrderState]:
        raise NotImplementedError

    def orders_today(self) -> List[OrderState]:
        raise NotImplementedError

    # ---- protective stops ---------------------------------------------------
    def place_stop(self, symbol: str, qty: int, trigger: float, ref_id: str) -> StopState:
        raise NotImplementedError

    def modify_stop(self, stop: StopState, qty: int, trigger: float) -> StopState:
        raise NotImplementedError

    def cancel_stop(self, stop: StopState) -> StopState:
        raise NotImplementedError

    def stops(self) -> List[StopState]:
        """All of OUR stops the broker still knows about, any state."""
        raise NotImplementedError

    def get_stop(self, stop: StopState) -> StopState:
        for s in self.stops():
            if s.stop_id == stop.stop_id:
                return s
        return StopState(stop.stop_id, stop.ref_id, stop.symbol, stop.qty,
                         stop.trigger, STOP_CANCELLED, stop.kind, "MISSING")

    # ---- lifecycle ----------------------------------------------------------
    def tick(self) -> None:
        """Advance any internal simulation. A no-op for real brokers."""
        return None
