"""Read-only venue reconciliation for durable live account tasks.

A positive final payout certifies redeemable shares. Booking cash requires
the operator's independently verified receipt for the account, token and
full channel allocation. Neither a market winner nor a wallet balance
proves that the channel received a payout.
"""
import json
import sqlite3
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from nba_data import http_json


REVIEW_AFTER = 24 * 60 * 60
FINAL_STATES = {"CANCELED", "CANCELLED", "CANCELED_MARKET_RESOLVED", "EXPIRED", "MATCHED", "FILLED"}


def _decimal(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _terminal_size(order, order_id, token, side, market, confirmed_ids):
    if (not isinstance(order, dict) or str(order.get("id", "")).lower() != order_id.lower()
            or str(order.get("asset_id")) != token or str(order.get("side", "")).upper() != side
            or (market and str(order.get("market", "")).lower() != market.lower())):
        return None
    status = str(order.get("status", "")).upper().removeprefix("ORDER_STATUS_")
    matched = _decimal(order.get("size_matched"))
    if (status not in FINAL_STATES or matched is None or matched < 0
            or matched * 1000000 != (matched * 1000000).to_integral_value()):
        return None
    # associate_trades is optional in newer order snapshots. Cumulative size
    # still has to equal durable confirmed shares before the ledger finalizes.
    linked = order.get("associate_trades", [])
    if not isinstance(linked, list) or any(not isinstance(i, str) or i not in confirmed_ids for i in linked):
        return None
    if matched == 0 and (confirmed_ids or status in {"MATCHED", "FILLED"}):
        return None
    return matched


def order_evidence(client, order_id, token, side, market="", known_trade_ids=(), *, with_identity=False):
    """Return exact signed taker fills and, when evidenced, cumulative final shares."""
    from py_clob_client_v2.clob_types import TradeParams

    if not order_id or not token:
        return ([], None, False) if with_identity else ([], None)
    try:
        order = client.get_order(order_id)
    except Exception:
        order = None  # A 404 can precede a late fill. Always query trade history.
    try:
        trades = client.get_trades(TradeParams(asset_id=token))
    except Exception:
        return ([], None, False) if with_identity else ([], None)
    return parse_order_evidence(order, trades, order_id, token, side, market, known_trade_ids,
                                with_identity=with_identity)


def parse_order_evidence(order, trades, order_id, token, side, market="", known_trade_ids=(), *, with_identity=False):
    """Validate a single scoped trade read and optional signed-order snapshot."""
    if not order_id or not token or not isinstance(trades, list):
        return ([], None, False) if with_identity else ([], None)
    confirmed, conflicting = {}, set()
    unresolved = False
    for trade in trades:
        if not isinstance(trade, dict) or str(trade.get("taker_order_id", "")).lower() != order_id.lower():
            continue
        if (str(trade.get("side", "")).upper() != side
                or str(trade.get("asset_id", token)) != token
                or (market and str(trade.get("market", market)).lower() != market.lower())):
            unresolved = True
            continue
        status = str(trade.get("status", "")).upper()
        if status != "CONFIRMED":
            if status != "FAILED":
                unresolved = True
            continue
        identity = trade.get("id")
        size, price = _decimal(trade.get("size")), _decimal(trade.get("price"))
        if (not isinstance(identity, str) or not identity or size is None or size <= 0
                or price is None or not 0 < price <= 1):
            unresolved = True
            continue
        fill = dict(id=identity, size=size, price=price)
        matched_at = str(trade.get("match_time", ""))
        if matched_at.isdigit() and 0 < int(matched_at) < 10_000_000_000:
            fill["matched_at"] = int(matched_at)
        if identity in confirmed and confirmed[identity] != fill:
            conflicting.add(identity)
        confirmed[identity] = fill
    for identity in conflicting:
        del confirmed[identity]
    final = None
    if not unresolved and not conflicting:
        final = _terminal_size(order, order_id, token, side, market, set(known_trade_ids) | confirmed.keys())
    fills = list(confirmed.values())
    if with_identity:
        return fills, final, bool(fills) or final is not None
    return fills, final


def _binary_payouts(tokens, outcomes, prices):
    if not all(isinstance(a, list) and len(a) == 2 for a in (tokens, outcomes, prices)):
        return {}
    if (not all(isinstance(x, str) and x for x in tokens + outcomes)
            or len(set(tokens)) != 2 or len(set(outcomes)) != 2):
        return {}
    amounts = [_decimal(p) for p in prices]
    if amounts not in ([0, 1], [1, 0], [Decimal("0.5"), Decimal("0.5")]):
        return {}
    return dict(zip(zip(tokens, outcomes), map(float, amounts)))


def _clob_payouts(clob, market):
    if (not isinstance(clob, dict) or clob.get("closed") is not True
            or str(clob.get("condition_id", "")).lower() != market.lower()):
        return {}
    tokens = clob.get("tokens")
    if not isinstance(tokens, list) or len(tokens) != 2 or not all(isinstance(t, dict) for t in tokens):
        return {}
    winners = [t.get("winner") for t in tokens]
    if not all(isinstance(w, bool) for w in winners) or sum(winners) != 1:
        return {}
    if any(_decimal(t.get("price")) != int(w) for t, w in zip(tokens, winners)):
        return {}
    return _binary_payouts([t.get("token_id") for t in tokens], [t.get("outcome") for t in tokens],
                           [int(w) for w in winners])


def _market_payouts(client, market):
    if not market:
        return {}
    try:
        payouts = _clob_payouts(client.get_market(market), market)
        if payouts:
            return payouts
    except Exception:
        pass
    # A closed market with neither CLOB token marked winner can also be an
    # unresolved book. Only accept a split when Gamma explicitly confirms finality.
    query = urlencode({"condition_ids": market, "closed": "true"})
    markets = http_json("https://gamma-api.polymarket.com/markets?" + query, timeout=5)
    if not isinstance(markets, list) or len(markets) != 1:
        return {}
    result = markets[0]
    if (result.get("closed") is not True or result.get("umaResolutionStatus") != "resolved"
            or str(result.get("conditionId", "")).lower() != market.lower()):
        return {}
    arrays = [result.get(k) for k in ("clobTokenIds", "outcomes", "outcomePrices")]
    return _binary_payouts(*(json.loads(a) if isinstance(a, str) else a for a in arrays))


def reconcile_account(ledger, client, log, now=None, *, stop_event=None):
    """Poll each pending channel once. No orders, redemptions or retries are sent."""
    now = time.time() if now is None else now
    markets = {}
    storage_error = None
    for task in ledger.tasks(pending_only=True):
        if stop_event is not None and stop_event.is_set():
            break
        game, channel = task["game_id"], task["channel"]
        action, market = task["action"], task["market"]
        event = {"gid": game, "ch": channel}
        changed = False
        try:
            if task["state"] == "REDEEMABLE":
                # A shared wallet burn cannot identify which channel was paid.
                # Only the operator's verified receipt can confirm redemption.
                reason = "awaiting attributable redemption receipt"
            elif action in {"OPEN", "CLOSE"}:
                oid = task["order_id"]
                fills, final, verified = order_evidence(
                    client, oid, task["token"], "BUY" if action == "OPEN" else "SELL",
                    market, ledger.trade_ids(oid), with_identity=True)
                reason = "awaiting signed order terminal state and confirmed trades"
                if fills or final is not None:
                    changed = ledger.apply_order(game, channel, oid, fills, terminal_matched=final,
                                                  identity_verified=verified)
                    event.update(ev="account_order_update", order_id=oid)
            else:
                reason = "awaiting verified market resolution"
                if market not in markets:
                    markets[market] = {}  # Cache an unavailable market for this sweep, too.
                    markets[market] = _market_payouts(client, market)
                price = markets[market].get((task["token"], task["outcome"]))
                if price is not None:
                    changed = ledger.apply_resolution(game, channel, price)
                    event.update(ev="account_resolution", payout_price=price)
        except sqlite3.Error as exc:
            storage_error = exc
            continue
        except Exception as exc:
            reason = f"account reconciliation unavailable ({type(exc).__name__})"
        if changed:
            log(event)
        elif (task["state"] != "REVIEW" and not task.get("review_reason")
              and now - (task.get("updated_at") or task["created_at"]) >= REVIEW_AFTER):
            try:
                reviewed = ledger.mark_review(game, channel, reason)
            except sqlite3.Error as exc:
                storage_error = exc
                continue
            if reviewed:
                log(dict(event, ev="account_review", reason=reason))
    if storage_error is not None:
        raise storage_error
