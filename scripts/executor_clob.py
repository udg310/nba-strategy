"""In-process signing and exchange transport for Polygon CTF V2."""

import base64
import json
import logging
import os
import threading
import time
import urllib.request
from contextvars import ContextVar
from dataclasses import asdict
from decimal import Decimal, ROUND_FLOOR
from importlib.metadata import version

MICRO = 1_000_000
CHAIN = 137
HOST = "https://clob.polymarket.com"
EVENT = "OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)"
_operation = ContextVar("clob_operation", default=None)
_hook_lock = threading.Lock()


class ExpiredBeforePost(Exception):
    pass


class CallDeadline:
    """One SDK call. Abandonment and permission for its POST are mutually exclusive."""

    def __init__(self, expires, stop):
        self.expires = expires
        self.stop = stop
        self._lock = threading.Lock()
        self._abandoned = False
        self._posted = False

    def remaining(self, limit):
        with self._lock:
            if self._abandoned or self.stop.is_set():
                raise InterruptedError("CLOB operation stopped")
            seconds = self.expires - time.monotonic()
            if seconds <= 0:
                raise TimeoutError("CLOB operation timed out")
            return min(seconds, limit)

    def begin_post(self, deadline):
        with self._lock:
            if self._posted:
                raise RuntimeError("CLOB POST already started")
            if (self._abandoned or self.stop.is_set() or time.monotonic() >= self.expires
                    or time.time_ns() // 1_000_000 >= deadline):
                raise ExpiredBeforePost
            self._posted = True

    def abandon(self):
        with self._lock:
            self._abandoned = True
            return self._posted


def remaining(limit):
    operation = _operation.get()
    if operation is None:
        return limit
    return operation["budget"].remaining(limit)


def _before_request(request):
    operation = _operation.get()
    if operation is None:
        return
    if request.method != "GET":
        if request.method != "POST" or operation["op"] != "post":
            raise RuntimeError("unexpected CLOB write")
        operation["budget"].begin_post(operation["deadline"])
        # httpcore may spend time in DNS, TLS or the pool after HTTPX hands
        # off the request. Recheck before writing its headers on the socket.
        def check_before_headers(event, _info):
            if event.endswith(".send_request_headers.started"):
                remaining(5)
                if time.time_ns() // 1_000_000 >= operation["deadline"]:
                    raise TimeoutError("order deadline elapsed before headers")
        request.extensions["trace"] = check_before_headers
    timeout = remaining(5)
    request.extensions["timeout"] = dict.fromkeys(("connect", "read", "write", "pool"), timeout)


def client():
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds

    if version("py-clob-client-v2") != "1.2.2":
        raise RuntimeError("SDK version mismatch")
    # The pinned SDK shares its HTTPX connection pool across clients. A
    # request hook applies the calling worker's budget to every nested read
    # (including pagination) and checks the wall-clock deadline at the POST.
    from py_clob_client_v2.http_helpers import helpers

    with _hook_lock:
        hooks = helpers._http_client.event_hooks["request"]
        if _before_request not in hooks:
            hooks.append(_before_request)
            # The SDK otherwise logs the full error response body on HTTP errors.
            helpers.logger.disabled = True
            # HTTP/2 compression debug output can contain authenticated headers.
            for name in ("httpx", "httpcore", "hpack", "h2"):
                logging.getLogger(name).setLevel(logging.CRITICAL + 1)
    env = os.environ
    creds = ApiCreds(
        env["POLY_API_KEY"], env["POLY_API_SECRET"], env["POLY_API_PASSPHRASE"]
    )

    class ExecutionClient(ClobClient):
        def _resolve_transactions_hashes(self, response):
            # The ledger reconciler already polls confirmed fills independently.
            # Return the original POST receipt without the SDK's 30s polling loop.
            return response

    return ExecutionClient(
        HOST,
        CHAIN,
        key=env["POLY_PRIVATE_KEY"],
        creds=creds,
        signature_type=int(env.get("POLY_SIGNATURE_TYPE", "3")),
        funder=env["POLY_FUNDER"],
        retry_on_error=False,
    )


def rpc(method, params):
    body = json.dumps(dict(jsonrpc="2.0", id=1, method=method, params=params)).encode()
    request = urllib.request.Request(
        os.environ["EXECUTOR_POLYGON_RPC_URL"],
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=remaining(7)) as response:
        result = json.load(response)
    return result["result"]


def prepare(c, i):
    from py_clob_client_v2.clob_types import (
        AssetType,
        BalanceAllowanceParams,
        MarketOrderArgsV2,
        OrderType,
        PartialCreateOrderOptions,
    )
    from py_clob_client_v2.config import get_contract_config
    from py_clob_client_v2.order_utils.exchange_order_builder_v2 import (
        ExchangeOrderBuilderV2,
    )

    asset = i["asset"]
    funder = os.environ["POLY_FUNDER"].lower()
    if (
        i["account"].lower() != funder
        or i["environment"] != "live"
        or not asset.isdecimal()
    ):
        raise ValueError("unexpected account, environment, or asset")
    # The live runner targets CTF token IDs. Pin the chain, signing domain,
    # wallet mode, and token-to-condition mapping before signing.
    info = c.get_clob_market_info(i["market"])
    book = c.get_order_book(asset)
    market = str(book["market"]).lower()
    if (
        market != i["market"].lower()
        or str(book["asset_id"]) != asset
        or asset not in {str(t["t"]) for t in info["t"]}
    ):
        raise ValueError("market/asset mismatch")
    buying = i["action"] == "OPEN"
    side = "BUY" if buying else "SELL"
    levels = book["asks" if buying else "bids"]
    price = Decimal(i["limit_price"]) / MICRO
    best = (min if buying else max)(Decimal(level["price"]) for level in levels)
    if buying and best > price or not buying and best < price:
        return {"reject": "price outside limit", "retryable": True}
    if buying:
        effective = best + Decimal(".005") + Decimal(".05") * best * (1 - best)
        if i["channel"] == "D" and not Decimal(".70") <= effective <= Decimal(".80"):
            return {"reject": "D effective price outside band"}
        if i["channel"] in ("A", "C") and effective < Decimal(".10"):
            return {"reject": "price below strategy floor"}
    tick = str(c.get_tick_size(asset))
    if price % Decimal(tick):
        return {"reject": "limit is off tick"}
    # CTF FOK BUYs allow at most four decimals in the taker share amount,
    # which the SDK's finer tick precision can exceed. A coarser BUY limit
    # must round down (BUY price is a hard ceiling).
    if buying and Decimal(tick) < Decimal(".01"):
        tick = "0.01"
        price = price.quantize(Decimal(tick), rounding=ROUND_FLOOR)
        if best > price:
            return {
                "reject": "no ask within supported BUY precision",
                "retryable": True,
            }
    neg = bool(info.get("nr", False))
    contracts = get_contract_config(CHAIN)
    address = contracts.neg_risk_exchange_v2 if neg else contracts.exchange_v2
    params = BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL if buying else AssetType.CONDITIONAL,
        token_id=None if buying else asset,
    )
    ba = c.get_balance_allowance(params)
    balance = int(ba["balance"])
    allowance = ba.get("allowance")
    if allowance is None:
        allowance = {k.lower(): v for k, v in ba["allowances"].items()}.get(
            address.lower(), 0
        )
    reserved = i["reserved"]
    if min(balance, int(allowance)) < max(reserved, i["pending_reservations"]):
        return {"reject": "insufficient venue balance or allowance"}
    amount = Decimal(reserved) / MICRO
    # Keep collateral fees inside the frozen budget. For the V2 fee formula
    # with exponent >= 1, fee/notional <= market rate for every crossed price.
    if buying:
        fd = info.get("fd") or {}
        rate = Decimal(str(fd.get("r", 0)))
        if rate < 0 or (rate and Decimal(str(fd["e"])) < 1):
            raise ValueError("unsupported market fee schedule")
        amount = amount / (1 + rate)
    amount = amount.quantize(Decimal(".01"), rounding=ROUND_FLOOR)
    if amount <= 0:
        return {"reject": "amount below exchange precision"}
    signed = c.create_market_order(
        MarketOrderArgsV2(
            token_id=asset,
            side=side,
            amount=float(amount),
            price=float(price),
            order_type=OrderType.FOK,
        ),
        PartialCreateOrderOptions(tick_size=tick, neg_risk=neg, version=2),
    )
    builder = ExchangeOrderBuilderV2(address, CHAIN, c.signer, domain_version="2")
    oid = builder.build_order_hash(builder.build_order_typed_data(signed))
    return {"order_id": oid, "signed": asdict(signed)}


def classify_post(reply, oid):
    result = {"state": "UNKNOWN", "order_id": oid}
    if not isinstance(reply, dict):
        return result
    received = str(reply.get("orderID") or reply.get("order_id") or "").lower()
    if received and received != oid.lower():
        result["reason"] = "exchange order hash differs; review required"
        return result
    if (
        not received
        and not any(reply.get(k) for k in ("tradeIDs", "transactionsHashes"))
        and reply.get("success") is not True
    ):
        # Documented FOK-unfillable messages guarantee zero execution. Errors
        # such as "duplicate order", 404 and generic 4xx remain ambiguous.
        msg = str(reply.get("error") or reply.get("errorMsg") or "").strip()
        if msg in {
            "order couldn't be fully filled. FOK orders are fully filled or killed.",
            "FOK orders are filled or killed",
        }:
            return {
                "state": "REJECTED_ZERO",
                "order_id": oid,
                "reason": "FOK unfilled",
                "retryable": True,
            }
    if received and (
        reply.get("success") is True
        or str(reply.get("status", "")).upper() in {"DELAYED", "MATCHED", "LIVE"}
    ):
        result["state"] = "ACCEPTED"
    return result


def confirmed_legs(receipt, wallet, exchanges, topic, timestamp):
    """Extract only the wallet's own fills from trusted CTF V2 exchange events."""
    if not receipt or int(receipt["status"], 16) != 1:
        raise ValueError("settlement receipt unavailable")
    legs = []
    for log in receipt["logs"]:
        topics = log["topics"]
        if (
            len(topics) != 4
            or topics[0].lower() != topic
            or log["address"].lower() not in exchanges
            or "0x" + topics[2][-40:].lower() != wallet
        ):
            continue
        data = bytes.fromhex(log["data"][2:])
        if len(data) != 7 * 32 or log.get("removed"):
            raise ValueError("unexpected settlement event")
        side, token, maker, taker, fee, _, _ = (
            int.from_bytes(data[n : n + 32], "big") for n in range(0, len(data), 32)
        )
        if side not in (0, 1) or min(maker, taker) == 0:
            raise ValueError("invalid settlement amounts")
        cash, qty = (maker, taker) if side == 0 else (taker, maker)
        legs.append(
            {
                "id": receipt["transactionHash"].lower()
                + ":"
                + str(int(log["logIndex"], 16)),
                "order_id": topics[1].lower(),
                "asset": str(token),
                "side": "BUY" if side == 0 else "SELL",
                "quantity": qty,
                "notional": cash,
                "price": (cash * MICRO + qty // 2) // qty,
                "fee": fee,
                "time": timestamp,
                "confirmed": True,
            }
        )
    return legs


def settlement_reader():
    from eth_utils import keccak
    from py_clob_client_v2.config import get_contract_config

    contracts = get_contract_config(CHAIN)
    exchanges = {contracts.exchange_v2.lower(), contracts.neg_risk_exchange_v2.lower()}
    topic = "0x" + keccak(text=EVENT).hex()
    cache = {}

    def read(trade):
        status = str(trade["status"]).upper().removeprefix("TRADE_STATUS_")
        if status != "CONFIRMED":
            return []
        txid = trade["transaction_hash"].lower()
        if txid not in cache:
            receipt = rpc("eth_getTransactionReceipt", [txid])
            if not receipt or receipt["transactionHash"].lower() != txid:
                raise ValueError("receipt mismatch")
            # Stable event time even if the trade API groups several trades
            # in one transaction or returns them on different pages.
            block = rpc("eth_getBlockByHash", [receipt["blockHash"], False])
            cache[txid] = confirmed_legs(
                receipt,
                os.environ["POLY_FUNDER"].lower(),
                exchanges,
                topic,
                int(block["timestamp"], 16) * 1000,
            )
            if not cache[txid]:
                raise ValueError(
                    "account trade without a supported CTF V2 settlement leg"
                )
        return cache[txid]

    return read


def order_snapshot(c, oid, asset, side, fills_for_order):
    order = c.get_order(oid)
    if (
        not isinstance(order, dict)
        or str(order["id"]).lower() != oid.lower()
        or str(order["asset_id"]) != asset
        or order["side"].upper() != side
    ):
        raise ValueError("order identity unavailable")
    raw = Decimal(str(order["size_matched"])) * MICRO
    if raw < 0 or raw != raw.to_integral_value():
        raise ValueError("unexpected matched quantity precision")
    terminal = str(order["status"]).upper().removeprefix("ORDER_STATUS_") in {
        "CANCELED",
        "CANCELLED",
        "EXPIRED",
        "MATCHED",
        "FILLED",
    }
    if (
        terminal
        and raw == 0
        and str(order["status"]).upper().endswith(("MATCHED", "FILLED"))
    ):
        raise ValueError("matched status without quantity")
    return {
        "order_id": oid,
        "state": "FINAL" if terminal else "OPEN",
        "total_filled": int(raw),
        "fills": fills_for_order(order),
    }


def execute(op, payload, budget):
    token = _operation.set({"op": op, "deadline": payload.get("deadline"), "budget": budget})
    try:
        remaining(35)
        return _execute(op, payload)
    finally:
        _operation.reset(token)


def _execute(op, payload):
    from py_clob_client_v2.clob_types import OrderType, TradeParams
    from py_clob_client_v2.order_utils.model.order_data_v2 import SignedOrderV2
    from py_clob_client_v2.constants import END_CURSOR

    c = client()
    if op == "probe":
        if int(rpc("eth_chainId", []), 16) != CHAIN:
            raise ValueError("wrong RPC chain")
        c.get_trades_paginated()
        return {"ok": True}
    if op == "prepare":
        return prepare(c, payload)
    if op == "post":
        if time.time_ns() // 1_000_000 >= payload["deadline"]:
            return {"state": "REJECTED_ZERO", "reason": "expired before transmission"}
        # post_order (unlike create_and_post_market_order) never resigns and
        # resubmits after a version mismatch. The client has HTTP retries off.
        from py_clob_client_v2.exceptions import PolyApiException

        try:
            reply = c.post_order(SignedOrderV2(**payload["signed"]), OrderType.FOK)
        except ExpiredBeforePost:
            return {"state": "REJECTED_ZERO", "reason": "expired before transmission"}
        except PolyApiException as exc:
            if exc.status_code != 400 or not isinstance(exc.error_msg, dict):
                raise
            reply = exc.error_msg
        return classify_post(reply, payload["order_id"])
    read = settlement_reader()
    if op == "lookup":
        oid = payload["order_id"]

        def relevant(order):
            if (
                str(order["maker_address"]).lower() != os.environ["POLY_FUNDER"].lower()
                or str(order["market"]).lower() != payload["market"].lower()
            ):
                raise ValueError("venue owner or market mismatch")
            legs = {}
            for trade_id in set(order.get("associate_trades") or []):
                for t in c.get_trades(TradeParams(id=trade_id)):
                    if str(t["id"]) == trade_id:
                        for leg in read(t):
                            if leg["order_id"].lower() == oid.lower():
                                legs[leg["id"]] = leg
            return list(legs.values())

        return order_snapshot(
            c,
            oid,
            payload["asset"],
            "BUY" if payload["action"] == "OPEN" else "SELL",
            relevant,
        )
    if op == "trades":
        # Only advance the time watermark over fully confirmed/failed trades.
        # Keep a fixed end while paging, and replay a five-minute overlap.
        cursor = payload.get("cursor", "")
        state = json.loads(base64.urlsafe_b64decode(cursor)) if cursor else {}
        after = int(state.get("after", 0))
        now = int(time.time())
        before = int(state.get("before", now))
        if before <= after:
            return {"fills": [], "next": cursor}
        result = c.get_trades_paginated(
            TradeParams(after=max(0, after - 300), before=before),
            next_cursor=state.get("page"),
        )
        legs = {}
        pending = state.get("pending")
        for t in result["trades"]:
            status = str(t["status"]).upper().removeprefix("TRADE_STATUS_")
            if status not in ("CONFIRMED", "FAILED"):
                ts = int(t["match_time"])
                pending = min(pending, ts) if pending is not None else ts
            for leg in read(t):
                legs[leg["id"]] = leg
        token = result["next_cursor"]
        if not token:
            raise ValueError("missing pagination token")
        if token == END_CURSOR:
            new_state = {
                "after": min(before, pending) if pending is not None else before
            }
        else:
            new_state = {
                "after": after,
                "before": before,
                "page": token,
                "pending": pending,
            }
        new_cursor = base64.urlsafe_b64encode(json.dumps(new_state).encode()).decode()
        return {
            "fills": list(legs.values()),
            "next": new_cursor,
            "complete": token == END_CURSOR,
        }
    raise ValueError("unsupported operation")
