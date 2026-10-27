"""Groww adapter, written against growwapi 1.5.0 (verified by reading its
source, not from memory).

Facts about the SDK this file is built around:
  * Every call takes `timeout`, and it defaults to None - i.e. wait forever. An
    unattended process must never make a call without one; `_call` enforces it.
  * Only requests.Timeout is wrapped (as GrowwAPITimeoutException). Connection
    errors and non-JSON gateway responses escape as raw `requests` exceptions,
    so classification here is by exception type name, not by catching one base.
  * get_order_list() sends no paging parameters unless `segment` is given, and
    never sends page_size. Always pass segment, and page until a page is empty.
  * GrowwAPI.get_access_token() returns the token STRING (its docstring says dict).
  * GrowwAPI(token) makes a network call and prints a banner on construction.
  * Smart orders (GTT) exist and persist across sessions - which is why they are
    the default protective stop: if this process is down, the stop is not.

Things that could NOT be verified from where this was written, because the API
was unreachable. Run `swing live probe` on your own machine before going live;
it calls each read-only endpoint and reports whether every parser understood
the response:
  * the exact field names in holdings, margin, LTP/OHLC, order and GTT responses
  * whether LTP keys are "NSE_RELIANCE" (get_ltp's docstring) or "NSE:RELIANCE"
    (get_ohlc's docstring - the SDK contradicts itself)
  * whether a GTT may be placed against shares bought the same day (T+0)

Every parser here either understands a response or raises. None of them guess
an empty answer: a holdings call that silently returned "nothing" would look,
to the executor, exactly like every stop firing at once.

Static IP: since 1 April 2026 order placement through broker APIs must come
from an IP address whitelisted with the broker. A 403 on an order almost
certainly means the request did not come from that address.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from .base import (BUY, LIMIT, MARKET, SELL, SL, SL_M, STOP_ACTIVE, STOP_CANCELLED,
                   WORKING, AuthError, Broker, BrokerError, GROWW_SMART_STATUS,
                   Holding, OrderRequest, OrderState, Quote, StopState, is_our_ref,
                   normalise_order_state, round_to_tick)

NA = float("nan")


# ------------------------------------------------------------------- TOTP
def totp(secret_b32: str, for_time: Optional[float] = None, digits: int = 6,
         period: int = 30, digest=hashlib.sha1) -> str:
    """RFC 6238 time-based one-time password, stdlib only.

    With a TOTP-type API key this is what makes the daily login unattended:
    Groww access tokens expire every morning, and a process that needs a human
    to type a code at 06:05 is not automated.
    """
    s = secret_b32.strip().replace(" ", "").upper()
    key = base64.b32decode(s + "=" * (-len(s) % 8))
    counter = int((time.time() if for_time is None else for_time) // period)
    mac = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


# ---------------------------------------------------------- error handling
def _classify(exc: BaseException) -> str:
    name = type(exc).__name__
    if name == "GrowwAPIAuthenticationException":
        return "auth"
    if name == "GrowwAPIAuthorisationException":
        return "forbidden"
    if name == "GrowwAPIRateLimitException":
        return "rate"
    if name in ("GrowwAPITimeoutException", "Timeout", "ReadTimeout", "ConnectTimeout"):
        return "timeout"
    if name in ("ConnectionError", "ChunkedEncodingError", "ProtocolError",
                "RemoteDisconnected", "SSLError"):
        return "conn"
    if name == "GrowwAPINotFoundException":
        return "notfound"
    if name == "GrowwAPIBadRequestException":
        return "badrequest"
    if name in ("JSONDecodeError", "ValueError"):
        return "badresponse"
    if name == "GrowwAPIException":
        return "api"
    return "other"


def _num(v: Any, default: float = NA) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _pick(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    return default


def _list_in(resp: Any, *preferred: str) -> List[dict]:
    """Find the record list in a response whose wrapper key is not documented."""
    if isinstance(resp, list):
        return resp
    if not isinstance(resp, dict):
        raise BrokerError(f"unrecognised response type {type(resp).__name__}")
    for k in preferred:
        if isinstance(resp.get(k), list):
            return resp[k]
    lists = [v for v in resp.values() if isinstance(v, list)]
    if len(lists) == 1:
        return lists[0]
    if not lists and not resp:
        return []
    raise BrokerError(f"unrecognised list response, keys={sorted(resp)[:12]}")


class GrowwBroker(Broker):
    name = "groww"
    is_live = True

    SEGMENT = "CASH"
    EXCHANGE = "NSE"
    PRODUCT = "CNC"

    def __init__(self, cfg, api: Any = None, token_factory: Optional[Callable[[], str]] = None,
                 api_factory: Optional[Callable[[str], Any]] = None,
                 sleeper: Callable[[float], None] = time.sleep):
        e = cfg.get("execution", {}) or {}
        self.timeout = float(e.get("api_timeout_s", 10))
        self.retries = int(e.get("api_retries", 3))
        self.min_interval = float(e.get("api_min_interval_s", 0.15))
        self.tick_size = float(e.get("tick_size", 0.05))
        self.stop_mode = str(e.get("stop_mode", "gtt"))
        self.gtt_duration = str(e.get("gtt_duration", "GTC"))
        self.day_stop_fallback = bool(e.get("day_stop_fallback", True))
        self.ltp_sep = str(e.get("ltp_symbol_separator", "_"))
        self.api = api
        self._token_factory = token_factory or self._token_from_env
        self._api_factory = api_factory or self._default_api
        self._sleep = sleeper
        self._last_call = 0.0

    # ---------------------------------------------------------------- session
    @staticmethod
    def _token_from_env() -> str:
        from growwapi import GrowwAPI            # optional dependency
        key = os.environ.get("GROWW_API_KEY", "").strip()
        totp_secret = os.environ.get("GROWW_TOTP_SECRET", "").strip()
        secret = os.environ.get("GROWW_API_SECRET", "").strip()
        token = os.environ.get("GROWW_ACCESS_TOKEN", "").strip()
        if key and totp_secret:
            return GrowwAPI.get_access_token(api_key=key, totp=totp(totp_secret))
        if key and secret:
            return GrowwAPI.get_access_token(api_key=key, secret=secret)
        if token:
            return token
        raise AuthError("no Groww credentials: set GROWW_API_KEY with GROWW_TOTP_SECRET "
                        "(unattended) or GROWW_API_SECRET, or a daily GROWW_ACCESS_TOKEN")

    @staticmethod
    def _default_api(token: str) -> Any:
        from growwapi import GrowwAPI
        return GrowwAPI(token)

    def connect(self) -> None:
        try:
            token = self._token_factory()
        except BrokerError:
            raise
        except Exception as exc:                        # noqa: BLE001
            raise AuthError(f"login failed: {type(exc).__name__}: {exc}") from exc
        self.api = self._api_factory(token)

    # ------------------------------------------------------------ call guard
    def _call(self, method: str, write: bool = False, **kwargs: Any) -> Any:
        if self.api is None:
            self.connect()
        kwargs.setdefault("timeout", self.timeout)
        reauthed = False
        last: Optional[BaseException] = None
        for attempt in range(self.retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                self._sleep(wait)
            self._last_call = time.monotonic()
            try:
                return getattr(self.api, method)(**kwargs)
            except Exception as exc:                    # noqa: BLE001 - SDK raises anything
                last = exc
                kind = _classify(exc)
                if kind == "auth" and not reauthed:
                    reauthed = True
                    self.connect()                      # token expired: re-login once
                    continue
                if kind == "auth":
                    raise AuthError(f"{method}: authentication failed after re-login") from exc
                if kind == "forbidden":
                    raise BrokerError(
                        f"{method}: 403 forbidden. For order calls this almost always "
                        f"means the request did not come from the static IP whitelisted "
                        f"with Groww (mandatory for API orders since 1 April 2026).",
                        code="FORBIDDEN") from exc
                if kind == "rate":
                    self._sleep(min(8.0, 0.5 * (2 ** attempt)))
                    continue
                if kind in ("timeout", "conn", "badresponse"):
                    if write:
                        # The request may have reached the exchange. Never retry
                        # blind: the caller must look the order up by reference.
                        raise BrokerError(f"{method}: {kind} - outcome unknown",
                                          ambiguous=True, code=kind.upper()) from exc
                    self._sleep(min(8.0, 0.5 * (2 ** attempt)))
                    continue
                if kind == "notfound":
                    raise BrokerError(f"{method}: not found", code="NOT_FOUND") from exc
                msg = getattr(exc, "msg", None) or str(exc)
                code = getattr(exc, "code", "") or kind.upper()
                raise BrokerError(f"{method}: {msg}", code=str(code)) from exc
        raise BrokerError(f"{method}: gave up after {self.retries + 1} attempts "
                          f"({type(last).__name__})", retryable=True)

    # ---------------------------------------------------------------- account
    def holdings(self) -> Dict[str, Holding]:
        resp = self._call("get_holdings_for_user")
        rows = _list_in(resp, "holdings")
        out: Dict[str, Holding] = {}
        for r in rows:
            sym = _pick(r, "trading_symbol", "tradingSymbol", "symbol")
            qty = _pick(r, "quantity", "qty", "total_quantity")
            if sym is None or qty is None:
                raise BrokerError(f"holding row not understood, keys={sorted(r)}")
            q = int(round(_num(qty, 0.0)))
            if q > 0:
                out[str(sym)] = Holding(str(sym), q,
                                        _num(_pick(r, "average_price", "avg_price"), 0.0))
        return out

    def cash_available(self) -> float:
        resp = self._call("get_available_margin_details")
        eq = resp.get("equity_margin_details") if isinstance(resp, dict) else None
        v = _pick(eq or {}, "cnc_balance_available", "cnc_available")
        if v is None:
            v = _pick(resp, "clear_cash", "available_cash")
        if v is None:
            raise BrokerError(f"margin response not understood, keys={sorted(resp)[:12]}")
        return _num(v)

    # ------------------------------------------------------------ market data
    def _key(self, sym: str) -> str:
        return f"{self.EXCHANGE}{self.ltp_sep}{sym}"

    @staticmethod
    def _strip(key: str) -> str:
        for sep in ("_", ":"):
            pre = "NSE" + sep
            if key.startswith(pre):
                return key[len(pre):]
        return key

    def quotes(self, symbols: Sequence[str]) -> Dict[str, Quote]:
        out: Dict[str, Quote] = {}
        syms = list(dict.fromkeys(symbols))
        now = time.time()
        for k in range(0, len(syms), 50):               # documented batch limit
            chunk = tuple(self._key(s) for s in syms[k:k + 50])
            ltp = self._call("get_ltp", exchange_trading_symbols=chunk, segment=self.SEGMENT)
            ohlc = self._call("get_ohlc", exchange_trading_symbols=chunk, segment=self.SEGMENT)
            if not isinstance(ltp, dict):
                raise BrokerError("LTP response not understood")
            for key, v in ltp.items():
                sym = self._strip(str(key))
                px = _num(v if not isinstance(v, dict) else _pick(v, "ltp", "last_price"))
                o = (ohlc or {}).get(key) or (ohlc or {}).get(
                    key.replace("_", ":")) or (ohlc or {}).get(key.replace(":", "_")) or {}
                out[sym] = Quote(sym, px, _num(_pick(o, "open")), _num(_pick(o, "high")),
                                 _num(_pick(o, "low")), _num(_pick(o, "close")), ts=now)
        return {s: q for s, q in out.items() if q.valid()}

    # ----------------------------------------------------------------- orders
    def _order_from(self, r: dict, fallback: Optional[OrderRequest] = None) -> OrderState:
        oid = _pick(r, "groww_order_id", "order_id")
        if oid is None:
            raise BrokerError(f"order response has no order id, keys={sorted(r)}")
        qty = int(_num(_pick(r, "quantity"), fallback.qty if fallback else 0))
        filled = int(_num(_pick(r, "filled_quantity", "filled_qty"), 0))
        raw = str(_pick(r, "order_status", "status", default="") or "")
        return OrderState(
            order_id=str(oid),
            ref_id=str(_pick(r, "order_reference_id", default=fallback.ref_id if fallback else "")),
            symbol=str(_pick(r, "trading_symbol", default=fallback.symbol if fallback else "")),
            side=str(_pick(r, "transaction_type", default=fallback.side if fallback else "")),
            qty=qty,
            order_type=str(_pick(r, "order_type", default=fallback.order_type if fallback else "")),
            state=normalise_order_state(raw, filled, qty), raw_status=raw,
            filled_qty=filled,
            avg_price=_num(_pick(r, "average_fill_price", "avg_fill_price", "average_price"), 0.0),
            price=_num(_pick(r, "price"), 0.0),
            trigger_price=_num(_pick(r, "trigger_price"), NA),
            message=str(_pick(r, "remark", "message", default="") or ""))

    def place_order(self, req: OrderRequest) -> OrderState:
        price = 0.0
        if req.order_type in (LIMIT, SL):
            price = round_to_tick(req.price, self.tick_size,
                                  "down" if req.side == BUY else "up")
        trig = None
        if req.order_type in (SL, SL_M) and req.trigger_price is not None:
            trig = round_to_tick(req.trigger_price, self.tick_size,
                                 "up" if req.side == SELL else "down")
        resp = self._call("place_order", write=True, validity="DAY",
                          exchange=self.EXCHANGE, order_type=req.order_type,
                          product=self.PRODUCT, quantity=int(req.qty),
                          segment=self.SEGMENT, trading_symbol=req.symbol,
                          transaction_type=req.side, order_reference_id=req.ref_id,
                          price=price, trigger_price=trig)
        st = self._order_from(resp, fallback=req)
        if st.ref_id != req.ref_id:
            st.ref_id = req.ref_id
        return st

    def cancel_order(self, order_id: str) -> OrderState:
        self._call("cancel_order", write=True, groww_order_id=order_id, segment=self.SEGMENT)
        return self.get_order(order_id)

    def get_order(self, order_id: str) -> OrderState:
        return self._order_from(self._call("get_order_detail", segment=self.SEGMENT,
                                           groww_order_id=order_id))

    def find_order_by_ref(self, ref_id: str) -> Optional[OrderState]:
        try:
            r = self._call("get_order_status_by_reference", segment=self.SEGMENT,
                           order_reference_id=ref_id)
        except BrokerError as e:
            if e.code == "NOT_FOUND":
                return None
            raise
        oid = _pick(r, "groww_order_id")
        return self.get_order(str(oid)) if oid else None

    def orders_today(self) -> List[OrderState]:
        out: List[OrderState] = []
        seen = set()
        for page in range(40):
            rows = _list_in(self._call("get_order_list", segment=self.SEGMENT, page=page),
                            "order_list", "orders")
            fresh = [r for r in rows if _pick(r, "groww_order_id") not in seen]
            if not fresh:
                break
            for r in fresh:
                seen.add(_pick(r, "groww_order_id"))
                out.append(self._order_from(r))
        return out

    # ----------------------------------------------------------------- stops
    def _gtt_from(self, r: dict) -> StopState:
        sid = _pick(r, "smart_order_id", "id")
        if sid is None:
            raise BrokerError(f"smart order not understood, keys={sorted(r)}")
        raw = str(_pick(r, "status", "smart_order_status", default="") or "")
        child = ""
        for k, v in (r.items() if isinstance(r, dict) else []):
            if k.endswith("order_id") and k != "smart_order_id" and v:
                child = str(v)
        return StopState(stop_id=str(sid), ref_id=str(_pick(r, "reference_id", default="")),
                         symbol=str(_pick(r, "trading_symbol", default="")),
                         qty=int(_num(_pick(r, "quantity"), 0)),
                         trigger=_num(_pick(r, "trigger_price")),
                         state=GROWW_SMART_STATUS.get(raw.upper(), STOP_ACTIVE),
                         kind="gtt", raw_status=raw, child_order_id=child)

    def place_stop(self, symbol: str, qty: int, trigger: float, ref_id: str) -> StopState:
        trig = round_to_tick(trigger, self.tick_size, "up")
        if self.stop_mode == "gtt":
            try:
                r = self._call("create_smart_order", write=True, smart_order_type="GTT",
                               segment=self.SEGMENT, trading_symbol=symbol, quantity=int(qty),
                               product_type=self.PRODUCT, exchange=self.EXCHANGE,
                               duration=self.gtt_duration, reference_id=ref_id,
                               trigger_price=f"{trig:.2f}", trigger_direction="DOWN",
                               order={"order_type": MARKET, "transaction_type": SELL})
                st = self._gtt_from(r)
                st.ref_id = st.ref_id or ref_id
                return st
            except BrokerError as e:
                if e.ambiguous or not self.day_stop_fallback:
                    raise
                # e.g. a GTT refused against shares that have not settled yet:
                # fall back to a same-day exchange stop and retry GTT tomorrow.
        o = self.place_order(OrderRequest(symbol, SELL, int(qty), SL_M, ref_id,
                                          trigger_price=trig, reason="stop"))
        return StopState(o.order_id, ref_id, symbol, int(qty), trig,
                         STOP_ACTIVE if o.state == WORKING else STOP_CANCELLED,
                         kind="day", raw_status=o.raw_status)

    def modify_stop(self, stop: StopState, qty: int, trigger: float) -> StopState:
        trig = round_to_tick(trigger, self.tick_size, "up")
        if stop.kind == "gtt":
            r = self._call("modify_smart_order", write=True, smart_order_id=stop.stop_id,
                           smart_order_type="GTT", segment=self.SEGMENT, quantity=int(qty),
                           trigger_price=f"{trig:.2f}", trigger_direction="DOWN",
                           order={"order_type": MARKET, "transaction_type": SELL})
            st = self._gtt_from(r) if isinstance(r, dict) and _pick(r, "smart_order_id") else stop
            st.qty, st.trigger = int(qty), trig
            return st
        self._call("modify_order", write=True, order_type=SL_M, segment=self.SEGMENT,
                   groww_order_id=stop.stop_id, quantity=int(qty), trigger_price=trig)
        return StopState(stop.stop_id, stop.ref_id, stop.symbol, int(qty), trig,
                         STOP_ACTIVE, "day")

    def cancel_stop(self, stop: StopState) -> StopState:
        if stop.kind == "gtt":
            self._call("cancel_smart_order", write=True, segment=self.SEGMENT,
                       smart_order_type="GTT", smart_order_id=stop.stop_id)
        else:
            o = self.cancel_order(stop.stop_id)
            if o.state != "CANCELLED":
                raise BrokerError(f"stop {stop.stop_id} not cancelled ({o.raw_status})",
                                  code="NOT_ACTIVE")
        return StopState(stop.stop_id, stop.ref_id, stop.symbol, stop.qty, stop.trigger,
                         STOP_CANCELLED, stop.kind, "CANCELLED")

    def stops(self) -> List[StopState]:
        out: List[StopState] = []
        seen = set()
        for page in range(40):
            rows = _list_in(self._call("get_smart_order_list", smart_order_type="GTT",
                                       segment=self.SEGMENT, page=page, page_size=50),
                            "smart_orders", "orders", "order_list")
            fresh = [r for r in rows if _pick(r, "smart_order_id") not in seen]
            if not fresh:
                break
            for r in fresh:
                seen.add(_pick(r, "smart_order_id"))
                st = self._gtt_from(r)
                if is_our_ref(st.ref_id):
                    out.append(st)
            if len(rows) < 50:
                break
        for o in self.orders_today():
            if o.order_type in (SL, SL_M) and o.side == SELL and is_our_ref(o.ref_id):
                state = {"WORKING": STOP_ACTIVE, "FILLED": "DONE",
                         "CANCELLED": STOP_CANCELLED, "REJECTED": "FAILED"}[o.state]
                out.append(StopState(o.order_id, o.ref_id, o.symbol, o.qty,
                                     _num(o.trigger_price), state, "day", o.raw_status,
                                     o.order_id if o.state == "FILLED" else ""))
        return out

    # ------------------------------------------------------------------ probe
    def probe(self, symbols: Iterable[str] = ("RELIANCE", "INFY")) -> Dict[str, Any]:
        """Call every read-only endpoint once; return raw responses and whether
        each parser understood them. Places no orders."""
        report: Dict[str, Any] = {"raw": {}, "parsed": {}, "errors": {}}
        syms = tuple(symbols)
        calls = {
            "holdings": ("get_holdings_for_user", {}, self.holdings),
            "margin": ("get_available_margin_details", {}, self.cash_available),
            "ltp": ("get_ltp", {"exchange_trading_symbols": tuple(self._key(s) for s in syms),
                                "segment": self.SEGMENT}, lambda: self.quotes(syms)),
            "ohlc": ("get_ohlc", {"exchange_trading_symbols": tuple(self._key(s) for s in syms),
                                  "segment": self.SEGMENT}, None),
            "orders": ("get_order_list", {"segment": self.SEGMENT, "page": 0}, self.orders_today),
            "gtt": ("get_smart_order_list", {"smart_order_type": "GTT", "segment": self.SEGMENT,
                                             "page": 0, "page_size": 50}, self.stops),
        }
        for name, (method, kwargs, parser) in calls.items():
            try:
                report["raw"][name] = self._call(method, **kwargs)
            except Exception as exc:                    # noqa: BLE001
                report["errors"][name] = f"{type(exc).__name__}: {exc}"
                continue
            if parser is None:
                continue
            try:
                parsed = parser()
                report["parsed"][name] = (len(parsed) if hasattr(parsed, "__len__")
                                          else parsed)
            except Exception as exc:                    # noqa: BLE001
                report["errors"][name + "_parse"] = f"{type(exc).__name__}: {exc}"
        return report
