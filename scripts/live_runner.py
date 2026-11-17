# -*- coding: utf-8 -*-
"""Live execution for the final A+C+D framework — one trader per game.

ARCHITECTURE
  Main thread: one ESPN poll every 3s. It never places an order, so it can
  never be blocked by one. It spawns a trader when a game tips off, hands the
  trader each score snapshot, and RELEASES the trader once the game is final
  and the trader holds nothing. The number of traders therefore follows that
  day's slate instead of being fixed.

  GameTrader (one thread per live game): owns its own CLOB client, score
  history, signal state, pending entries and positions. A crash inside one
  trader is caught and logged as `game_err`; it cannot affect another game.
  Exits are checked here every tick, so 8 concurrent positions cost 8 parallel
  ~800ms checks instead of one serialised 6.4s pass.

RULES (identical to strategy.py)
  A  pregame favourite >=0.60, m_fav <= -5, dog extends >=3 within 120s  -> buy DOG, TP+20c, force Q4 6:00
  C  -5 <= m_fav < 0, same signal                                      -> buy DOG, TP+20c, 30 game-clock minutes, force Q4 6:00
  D  el>=2040, 5<=m_fav<10, entry price 0.70-0.80                      -> buy FAVOURITE, hold to settlement; if m_fav<0 after entry, sell (margin_stop)

FILLS — each position owns the confirmed receipts of its signed orders.
Unknown orders survive a restart. A wallet balance can cap an order but
cannot attribute a fill to a particular channel.

COST — real exchange fee measured on-chain 2026-11-13: about 0.10*p*(1-p) per
round trip on top of the tick spread (strategy.entry_cost / exit_cost).

Usage
  python3 -I scripts/live_runner.py --dry-run
  python3 -I scripts/live_runner.py --live
Must run under ~/.polymarket/venv/bin/python (has py_clob_client_v2).
"""
import argparse, json, math, os, queue, sys, threading, time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from concurrent.futures import ThreadPoolExecutor
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nba_data as nd
import strategy as S
from executor_lock import account_lock
from live_ledger import (AttemptLedger, SUBMITTING, ACCEPTED, UNKNOWN,
                         FILLED, CLOSED, REJECTED_ZERO)
from executor_clob import ExpiredBeforePost, classify_post, signed_order_id

POLL = 3

STAKE_FRAC = float(os.environ.get("STAKE_FRAC", "0.05"))
MIN_ORDER = 1.0
# Slot caps.  Set high (default: effectively unlimited) so a good slate is not
# throttled; the cash reserve and the stake sizing still bound total exposure.
MAX_OPEN = int(os.environ.get("MAX_OPEN", "999"))
CASH_RESERVE = 0.20

# Lock channel: buy the favourite once it leads by >= LOCK_MARGIN (any time),
# hold to settlement.  Separate pool and slot cap so it never crowds A/C/D.
LOCK_FRAC = float(os.environ.get("LOCK_FRAC", "0.10"))
MAX_OPEN_LOCK = int(os.environ.get("MAX_OPEN_LOCK", "999"))
# Read-only team board, started alongside the runner (best effort).
BOARD_HOST = os.environ.get("BOARD_HOST", "0.0.0.0")
BOARD_PORT = int(os.environ.get("BOARD_PORT", "8790"))
BOARD_ENABLED = os.environ.get("BOARD_ENABLED", "1") == "1"
BOARD_AUTH = os.environ.get("BOARD_AUTH", "")     # "user:pass"; required for a public host
LOCK_MARGIN = 12.0
LOCK_ENABLED = os.environ.get("LOCK_ENABLED", "0") == "1"
LOCK_CH = {"style": "settle", "side": "fav", "weight": 1.0, "tp": None,
           "price_min": 0.0, "price_max": 0.985}

# Dog lock: buy the DOG once it leads by >= DOGLOCK_MARGIN, hold to settlement.
# Backtest (n=349, 2025-26 + 2026-27): +10.3%/trade, 31.5% full loss, longest
# losing run 4 -- but only after a price floor: whatever is cheap at 12+ is
# often cheap for a reason (p<0.45 -> -17.5% in the wide sample), and a Q1
# trigger overlaps A/C and is the worst bucket (-0.9%).  Confirmations
# ("still leading 5 min later") cut the full-loss rate to 22% but the price
# has already run by then, so the return collapses to ~+4%: no free lunch.
DOGLOCK_ENABLED = os.environ.get("DOGLOCK_ENABLED", "0") == "1"
DOGLOCK_FRAC = float(os.environ.get("DOGLOCK_FRAC", "0.05"))
MAX_OPEN_DOGLOCK = int(os.environ.get("MAX_OPEN_DOGLOCK", "999"))
DOGLOCK_MARGIN = float(os.environ.get("DOGLOCK_MARGIN", "12"))
DOGLOCK_MIN_EL = float(os.environ.get("DOGLOCK_MIN_EL", "720"))   # Q2 onwards
DOGLOCK_PRICE_MIN = float(os.environ.get("DOGLOCK_PRICE_MIN", "0.45"))
DOGLOCK_PRICE_MAX = float(os.environ.get("DOGLOCK_PRICE_MAX", "0.985"))
# Optional margin stop: exit once the dog's lead falls below this many points.
# Negative = off (hold to settlement), which is what the backtest prefers:
# on 349 trades a +5 stop scores +13.6% vs +9.8% for settlement only while
# exits fill at the printed price, but it goes to +9.4% once the exit pays
# 4c of realistic slippage -- and a take-profit at any level is strictly
# worse (-$60 to -$113 on the same games), because DL's edge IS settlement.
# The stop still cuts portfolio drawdown by ~13%, so it buys a smoother
# curve with the EV, not a better one.
DOGLOCK_STOP_LEAD = float(os.environ.get("DOGLOCK_STOP_LEAD", "-1"))
# How long after the crossing a DL entry is still allowed, in wall seconds.
# Without it a runner that starts mid-game (or a book that is momentarily
# blank) would buy a dog that led by 12 ten minutes ago at a totally
# different price, which is not the trade the backtest measured.
DOGLOCK_CHASE = float(os.environ.get("DOGLOCK_CHASE", "180"))
DOGLOCK_CH = {"style": "settle", "side": "dog", "weight": 1.0, "tp": None,
              "price_min": DOGLOCK_PRICE_MIN, "price_max": DOGLOCK_PRICE_MAX,
              "stop_margin_hi": (-DOGLOCK_STOP_LEAD if DOGLOCK_STOP_LEAD >= 0
                                 else None)}


def lock_signal(evs):
    """Lock: the first moment the favourite leads by >= LOCK_MARGIN."""
    for el, m, w in evs:
        if m >= LOCK_MARGIN:
            return {"el": el, "margin": m, "wall": w}
    return None


def doglock_signal(evs):
    """Dog lock: the first moment the DOG leads by >= DOGLOCK_MARGIN, at or
    after DOGLOCK_MIN_EL.  A Q1 trigger is skipped on purpose: it is the same
    move A/C fires on, and it is the only bucket with a negative return."""
    for el, m, w in evs:
        if el >= DOGLOCK_MIN_EL and m <= -DOGLOCK_MARGIN:
            return {"el": el, "margin": m, "wall": w}
    return None


def channel_spec(ch):
    """Spec for a channel; 'L' and 'DL' live here, not in strategy.CHANNELS."""
    if ch == "L":
        return LOCK_CH
    if ch == "DL":
        return DOGLOCK_CH
    return S.CHANNELS[ch]


def _slot_pool(ch):
    """'L' and 'DL' each keep their own slot pool (and their own cap)."""
    if ch == "L":
        return "L", MAX_OPEN_LOCK
    if ch == "DL":
        return "DL", MAX_OPEN_DOGLOCK
    return None, MAX_OPEN


def _pool_full(channel, durable_keys):
    pool, cap = _slot_pool(channel)
    occupied = durable_keys | {(p["gid"], p["ch"]) for p in ALL_POSITIONS}
    return sum(_slot_pool(ch)[0] == pool for _, ch in occupied) >= cap


def _frac_of(ch):
    if ch == "L":
        return LOCK_FRAC
    if ch == "DL":
        return DOGLOCK_FRAC
    return STAKE_FRAC * S.CHANNELS[ch]["weight"]


# ---- ownership -------------------------------------------------------------
# Each channel's open attempt identifies its position. Confirmed receipts
# supply gross buys and sales. The token balance is shared across channels.


BUY_WINDOW = 15          # the price is flat for ~15s, then drifts 2-3c/min
MAX_SLIP = 0.03

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
RUN_ID = datetime.now(timezone.utc).strftime("%H%M%S")
OUTDIR = os.path.join(ROOT, "data", "live_trade_final", TODAY, RUN_ID)

LOCK = threading.Lock()
ALL_POSITIONS = []
CONFIRMED = {"buy": False, "sell": False}
LOG_LOCK = threading.Lock()
LEDGER = None            # AttemptLedger in live mode; None in dry-run/tests


class DuplicateAttempt(Exception):
    """An OPEN/CLOSE attempt already exists for this game/channel."""


def log(rec):
    rec = dict(rec)
    rec.setdefault("t", time.time())
    if LEDGER is not None:
        rec.setdefault("account", LEDGER.account)
    with LOG_LOCK:
        os.makedirs(OUTDIR, exist_ok=True)
        with open(os.path.join(OUTDIR, "events.jsonl"), "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    print(json.dumps(rec, ensure_ascii=False, default=str)[:250], flush=True)


# ------------------------------------------------------------------ client
def load_env():
    env = {}
    with open(os.path.expanduser("~/.polymarket/env")) as f:
        lines = f.readlines()
    for line in lines:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def make_public_client():
    from py_clob_client_v2.client import ClobClient
    return ClobClient(host="https://clob.polymarket.com", chain_id=137)


def make_client():
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds
    env = load_env()
    with open(os.path.expanduser("~/.polymarket/creds.json")) as f:
        creds = json.load(f)
    return ClobClient(host="https://clob.polymarket.com", chain_id=137,
                      key=env["POLY_PRIVATE_KEY"],
                      creds=ApiCreds(api_key=creds["api_key"],
                                     api_secret=creds["api_secret"],
                                     api_passphrase=creds["api_passphrase"]),
                      signature_type=int(env.get("POLY_SIGNATURE_TYPE", 3)),
                      funder=env["POLY_FUNDER"], use_server_time=True,
                      # No hidden SDK retry: a retried POST is a duplicate
                      # order when the first one actually matched.
                      retry_on_error=False)


def balance(client):
    from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
    try:
        ba = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        return int(ba.get("balance", "0")) / 1e6
    except Exception:
        return None


def token_balance(client, token, tries=2):
    from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
    for _ in range(tries):
        try:
            ba = client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token))
            return int(ba.get("balance", "0")) / 1e6
        except Exception:
            time.sleep(1)
    return None


def book_depth(client, token, n=5):
    """Best bid/ask + the top levels, so we can tell a thin book from a moving one."""
    try:
        b = client.get_order_book(token)
    except Exception:
        return None
    if not b:
        return None
    def lv(x):
        return (float(x["price"]), float(x["size"])) if isinstance(x, dict) else (float(x[0]), float(x[1]))
    asks = sorted((lv(x) for x in (b.get("asks") or [])))[:n]
    bids = sorted((lv(x) for x in (b.get("bids") or [])), reverse=True)[:n]
    return {"bids": bids, "asks": asks,
            "ask_notional": round(sum(p * s for p, s in asks[:1]), 2),
            "bid_notional": round(sum(p * s for p, s in bids[:1]), 2)}


def book(client, token):
    """Never raises: a transient drop must not kill a trader."""
    try:
        b = client.get_order_book(token)
    except Exception:
        return None, None, "0.01"
    if not b:
        return None, None, "0.01"
    asks = [float(a["price"]) for a in (b.get("asks") or [])]
    bids = [float(x["price"]) for x in (b.get("bids") or [])]
    return (min(asks) if asks else None), (max(bids) if bids else None), \
           (b.get("tick_size") or "0.01")


def _check_buy_deadline(deadline):
    if deadline is None or not math.isfinite(deadline) or time.time() >= deadline:
        raise ExpiredBeforePost("buy signal expired before submission")


_buy_deadline = ContextVar("live_runner_buy_deadline", default=None)


def _guard_buy_request(request):
    deadline = _buy_deadline.get()
    if deadline is None or request.method != "POST":
        return
    _check_buy_deadline(deadline)
    previous = request.extensions.get("trace")

    def trace(event, info):
        if event.endswith(".send_request_headers.started"):
            _check_buy_deadline(deadline)
        if previous is not None:
            previous(event, info)

    request.extensions["trace"] = trace


@contextmanager
def _venue_deadline(deadline):
    # post_order may fetch server time before it sends. Cover that fetch and
    # the connection setup without changing timeouts or other trader threads.
    from py_clob_client_v2.http_helpers import helpers
    if deadline is not None:
        with LOCK:
            hooks = helpers._http_client.event_hooks["request"]
            if _guard_buy_request not in hooks:
                hooks.append(_guard_buy_request)
    marker = _buy_deadline.set(deadline)
    try:
        yield
    finally:
        _buy_deadline.reset(marker)


def _signed_market_order(client, token, side, amount, price_limit, tick, *,
                         live=False, buy_ceiling=None, deadline=None):
    """Build and sign the FOK order WITHOUT posting it; return (signed, oid).

    The order hash is the identity the exchange's trade record uses as
    `taker_order_id`, so it can be written to the attempt ledger before the
    POST. If the POST outcome is unclear the reconciler still knows what to
    look for.
    """
    if not live:
        raise PermissionError("signing requires live mode")
    from py_clob_client_v2.clob_types import (MarketOrderArgsV2, OrderType,
                                              PartialCreateOrderOptions)
    if side == "BUY":
        _check_buy_deadline(deadline)
        ceiling = Decimal(str(buy_ceiling))
        price = Decimal(str(price_limit))
        if not (0 < price <= ceiling < 1) or price % Decimal(tick):
            raise ValueError("BUY price outside signal limit or off tick")
        # The SDK truncates taker shares. At the exact ceiling a 3.73 USD
        # order at .53 would sign 7.0377 shares, or > .53 USD per share.
        # Choose the largest cent lot whose shares fit signing precision.
        from py_clob_client_v2.order_builder.builder import ROUNDING_CONFIG
        cents = int(Decimal(str(amount)) * 100)
        rounding = ROUNDING_CONFIG[tick]
        _, maker, taker = client.builder.get_market_order_amounts(
            "BUY", cents / 100, price_limit, rounding)
        if maker > ceiling * taker:
            share_step = Decimal(1).scaleb(-rounding.amount)
            cent_lot, _ = (price * share_step * 100).as_integer_ratio()
            cents -= cents % cent_lot
        amount = cents / 100
        if amount < MIN_ORDER:
            raise ValueError("BUY below minimum at supported precision")
    # The pinned SDK refreshes this version cache on a version-mismatch receipt.
    version = client._ClobClient__resolve_version()
    if version not in (2, 3):
        raise ValueError(f"unsupported market order signing version {version}")
    neg_risk = version == 2 and client.get_neg_risk(token)
    args = MarketOrderArgsV2(token_id=token, amount=round(amount, 2), side=side,
                             price=price_limit, order_type=OrderType.FOK)
    options = PartialCreateOrderOptions(tick_size=str(tick), neg_risk=neg_risk, version=version)
    if side == "BUY":
        _check_buy_deadline(deadline)
    signed = client.create_market_order(args, options)
    if side == "BUY" and int(signed.makerAmount) > ceiling * int(signed.takerAmount):
        raise ValueError("signed BUY exceeds signal limit")
    oid = signed_order_id(client.signer, signed, version, neg_risk)
    return signed, oid


def market_order(client, token, side, amount, price_limit, tick,
                 on_prepared=None, *, live=False, buy_ceiling=None, deadline=None):
    """amount = USD for BUY, shares for SELL; price_limit caps slippage.

    Market BUY orders are capped by the server at 2dp for the maker (USDC)
    amount and 4dp for the taker (shares) amount.  The 0.001 tick's rounding
    config asks for 5dp, which the server rejects with "invalid amounts".
    Buys therefore pass tick 0.01 explicitly; sells keep the book tick so a
    bid of 0.015 can still be hit.

    `on_prepared(oid)` runs after signing, BEFORE `post_order`; it is where the
    attempt ledger is written. It may raise to abort the submission.
    """
    if not live:
        raise PermissionError("submission requires live mode")
    from py_clob_client_v2.clob_types import OrderType
    from py_clob_client_v2.exceptions import PolyApiException
    signed, oid = _signed_market_order(client, token, side, amount,
                                       price_limit, tick, live=live,
                                       buy_ceiling=buy_ceiling, deadline=deadline)
    if side == "BUY":
        _check_buy_deadline(deadline)
    if on_prepared is not None:
        on_prepared(oid)
    if side == "BUY":
        _check_buy_deadline(deadline)
    try:
        with _venue_deadline(deadline if side == "BUY" else None):
            res = client.post_order(signed, OrderType.FOK)
    except PolyApiException as exc:
        if exc.status_code != 400 or classify_post(exc.error_msg, oid)["state"] != REJECTED_ZERO:
            raise
        res = exc.error_msg
    if isinstance(res, dict):
        received = res.get("orderID") or res.get("order_id")
        if received:
            log({"ev": "order_receipt", "side": side, "token": token,
                 "order_id": oid, "venue_order_id": str(received)})
    return res, oid


def _price_step(tick):
    return 10 ** -(3 if float(tick or "0.01") < 0.01 else 2)


def _round_down_tick(x, tick):
    step = _price_step(tick)
    return round(math.floor((x + 1e-9) / step) * step, 3 if step < 0.01 else 2)


# ------------------------------------------------------------------ market
def _market_matches_game(mk, start_iso):
    """Does this market belong to *this* game?

    Polymarket slugs use the US game date while a game's `start` is UTC, so a
    back-to-back between the same teams a day apart can match the wrong slug.
    A market's own endDate equals its game's tip-off, so compare it to ours; a
    neighbouring game is a full day away and must be rejected.
    """
    end = mk.get("endDate") or mk.get("end_date_iso") or mk.get("end_date")
    if not end or not start_iso:
        return True          # cannot verify; keep previous behaviour
    try:
        a = datetime.fromisoformat(str(start_iso).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except Exception:
        return True
    return abs((b - a).total_seconds()) <= 6 * 3600


def resolve(g, client):
    a = nd.pm_abbr(g["away"])
    h = nd.pm_abbr(g["home"])
    base = g["start"] or (TODAY + "T00:00Z")
    from datetime import timedelta
    slugs = []
    for off in (0, -1):
        try:
            d = (datetime.fromisoformat(base.replace("Z", "+00:00")) +
                 timedelta(days=off)).strftime("%Y-%m-%d")
        except Exception:
            d = base[:10]
        slugs += [f"nba-{a}-{h}-{d}", f"nba-{h}-{a}-{d}"]
    for slug in slugs:
        try:
            ev = nd.http_json("https://gamma-api.polymarket.com/events?slug=" + slug)
        except Exception:
            continue
        if not ev:
            continue
        mk = next((m for m in (ev[0].get("markets") or []) if m.get("slug") == slug), None)
        if not mk:
            continue
        if not _market_matches_game(mk, base):
            continue          # slug matched a neighbouring game (back-to-back)
        outs, toks = mk.get("outcomes"), mk.get("clobTokenIds")
        try:
            outs = json.loads(outs) if isinstance(outs, str) else outs
            toks = json.loads(toks) if isinstance(toks, str) else toks
        except Exception:
            continue
        if not outs or not toks or len(outs) != 2:
            continue
        tip = nd.parse_wall(base)
        try:
            fav, px = nd.pregame_fav(outs, toks, tip) if tip else (None, None)
        except Exception:
            fav, px = None, None
        return {"outcomes": outs, "tokens": toks, "fav": fav, "slug": slug,
                "market": mk.get("conditionId", ""),
                "pre": dict(px).get(fav) if px and fav else None}
    return None


def scoreboard():
    d = nd.http_json("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard")
    out = []
    for ev in d.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        st = (comp.get("status") or {}).get("type", {})
        teams = comp.get("competitors", [])
        home = next((t for t in teams if t.get("homeAway") == "home"), None)
        away = next((t for t in teams if t.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        out.append({"gid": str(ev.get("id")), "state": st.get("state"),
                    "detail": st.get("shortDetail", ""),
                    "period": (comp.get("status") or {}).get("period", 0),
                    "home": home["team"].get("abbreviation"),
                    "away": away["team"].get("abbreviation"),
                    "home_score": int(home.get("score") or 0),
                    "away_score": int(away.get("score") or 0),
                    "start": ev.get("date")})
    return out


def archived_final(gid):
    """Recover a missing game's final from the ESPN summary for that exact ID."""
    from urllib.parse import urlencode
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?"
    header = nd.http_json(url + urlencode({"event": gid}), timeout=8)["header"]
    if str(header["id"]) != gid or len(header["competitions"]) != 1:
        raise ValueError("archive game identity mismatch")
    comp = header["competitions"][0]
    status = comp.get("status", {})
    if status.get("type", {}).get("state") != "post":
        return None
    sides = {c["homeAway"]: c for c in comp["competitors"]}
    def score(side):
        value = sides[side]["score"]
        return int(value.get("value") if isinstance(value, dict) else value)
    return dict(gid=gid, state="post", period=status["period"], detail="Final",
                start=comp["date"], home=sides["home"]["team"]["abbreviation"],
                away=sides["away"]["team"]["abbreviation"],
                home_score=score("home"), away_score=score("away"))


def outcome_abbr(res, game, abbr):
    for o in res["outcomes"]:
        if (nd.abbr_of_name(o) or "").upper() == (abbr or "").upper():
            return o
    return None


# ------------------------------------------------------------------ trader
class GameTrader(threading.Thread):
    """One thread per game. Owns everything that game needs."""

    def __init__(self, gid, res, home, live):
        super().__init__(daemon=True, name=f"trader-{gid}")
        self.gid = gid
        self.res = res
        self.home = home
        self.live = live
        self.client = make_client() if live else make_public_client()
        self.q = queue.Queue(maxsize=1)
        self.stop_evt = threading.Event()
        self.hist = []
        self.done = set()
        self.pending = set()
        self.positions = []
        self.inflight = {}
        self.final_processed = False
        if self.live and LEDGER is not None:
            try:
                self._restore_orders()
            except Exception:
                for p in list(self.positions):
                    self._drop(p)
                raise

    def _restore_orders(self):
        rows = LEDGER.rows(self.gid)
        closes = {r["channel"]: r for r in rows if r["action"] == "CLOSE"}
        active_parents = {r["channel"] for r in rows if r["action"] == "OPEN"
                          and r["state"] in (SUBMITTING, ACCEPTED, UNKNOWN, FILLED)}
        for key, close in closes.items():
            if key in active_parents or close["state"] not in (SUBMITTING, ACCEPTED, UNKNOWN):
                continue
            # A legacy sale with no active parent has no proven remaining allocation.
            outcome = close["outcome"] or (self.res["fav"] if channel_spec(key)["side"] == "fav"
                      else next(o for o in self.res["outcomes"] if o != self.res["fav"]))
            token = self.res["tokens"][self.res["outcomes"].index(outcome)]
            LEDGER.handoff(self.gid, key, action="CLOSE", order_id=close["order_id"],
                           token=token, market=self.res.get("market", ""), outcome=outcome,
                           reason="legacy_log_unverified", position_id=close["position_id"])
            self.done.add(key)
        for row in rows:
            if row["action"] != "OPEN":
                continue
            key = row["channel"]
            self.done.add(key)
            if row["state"] not in (SUBMITTING, ACCEPTED, UNKNOWN, FILLED):
                continue
            spec = channel_spec(key)
            outcome = row["outcome"] or (self.res["fav"] if spec["side"] == "fav"
                      else next(o for o in self.res["outcomes"] if o != self.res["fav"]))
            token = self.res["tokens"][self.res["outcomes"].index(outcome)]
            market = self.res.get("market", "")
            LEDGER.bind_asset(self.gid, key, token=token, market=market, outcome=outcome)
            close = closes.get(key)
            if not row["identity_verified"] and close:
                # Old production overwrote earlier CLOSE attempts. An apparently
                # complete gross buy does not prove what remains after those sells.
                LEDGER.handoff(self.gid, key, token=token, market=market, outcome=outcome,
                               reason="legacy_log_unverified", action="OPEN", order_id=row["order_id"],
                               position_id=row["attempt_id"], identity_verified=False)
                continue
            q = dict(ch=key, token=token, outcome=outcome, attempt_id=row["attempt_id"],
                     market=market, order_id=row["order_id"], t0=row["signal_at"],
                     el_sig=row["el_in"], tries=row["attempt_no"], posted=True,
                     usd=row["reserved_usd"], style=spec["style"], tp=spec["tp"])
            if row["state"] != FILLED or not row["identity_verified"]:
                self.inflight[key] = q
                if row["state"] == FILLED:
                    q["legacy_filled"] = row["bought_size"]
            if row["identity_verified"] and row["bought_size"] and row["entry_price"] is not None:
                if close and close["state"] in (SUBMITTING, ACCEPTED, UNKNOWN):
                    LEDGER.exit_intent(self.gid, key, "resume_close")
                self._sync_position(q, row["bought_size"], row["entry_price"])

    def _sync_position(self, q, shares, entry):
        key = q["ch"]
        net = max(0., round(shares - LEDGER.sold_size(self.gid, key), 6))
        p = next((p for p in self.positions if p["ch"] == key), None)
        if net <= 1e-9:
            if p is not None:
                self._drop(p)
            return
        if p is None:
            intent = LEDGER.exit_intent(self.gid, key)
            p = dict(gid=self.gid, ch=key, token=q["token"], outcome=q["outcome"],
                     position_id=q["attempt_id"], t_in=q["t0"], el_in=q.get("el_sig"),
                     style=q["style"], tp=q["tp"], selling=bool(intent), exit_reason=intent)
            self.positions.append(p)
            with LOCK:
                ALL_POSITIONS.append(p)
        p.update(size=net, entry=entry, fill_shares=shares)

    def put(self, snap):
        try:
            while not self.q.empty():
                self.q.get_nowait()
            self.q.put_nowait(snap)
        except Exception:
            pass

    def stop(self):
        self.stop_evt.set()
        self.put(None)

    def flat(self):
        return self.final_processed and not self.positions and not self.inflight

    def run(self):
        log({"ev": "trader_start", "gid": self.gid, "fav": self.res.get("fav"),
             "pre": self.res.get("pre"), "open0": len(self.positions)})
        while not self.stop_evt.is_set():
            try:
                snap = self.q.get(timeout=5)
            except queue.Empty:
                continue
            if snap is None:
                break
            try:
                self.tick(snap)
            except Exception as e:
                log({"ev": "game_err", "gid": self.gid, "err": str(e)[:200]})
        log({"ev": "trader_stop", "gid": self.gid, "open": len(self.positions)})

    def tick(self, g):
        now = time.time()
        if self._update_history(g, now):
            self._find_signals(g, now)
        self._work_inflight(g, now)
        self._manage_exits(g, now)
        self.final_processed = g["state"] == "post"

    # ---- state ----
    def _update_history(self, g, now):
        key = (g["home_score"], g["away_score"])
        if self.hist and self.hist[-1][3] == key:
            return False
        el = nd.elapsed_of(g["period"], g["detail"])
        if el is None:
            return False
        home_out = outcome_abbr(self.res, g, self.home)
        fav = self.res.get("fav")
        home_is_fav = (fav == home_out) if (fav and home_out) else True
        m = (g["home_score"] - g["away_score"]) if home_is_fav else \
            (g["away_score"] - g["home_score"])
        self.hist.append((now, el, m, key))
        return True

    def _events(self, g, now):
        evs = [(el, m, w) for (w, el, m, _k) in self.hist]
        el_now = nd.elapsed_of(g["period"], g["detail"])
        if el_now is not None and evs:
            evs = evs + [(el_now, evs[-1][1], now)]
        return evs

    # ---- entry ----
    def _find_signals(self, g, now):
        if not self.live or g["state"] == "post" or len(self.hist) < 3:
            return
        evs = self._events(g, now)
        pre = self.res.get("pre")
        for key in ("A", "C", "D", "L", "DL"):
            if key in self.done or key in self.pending:
                continue
            if key == "L":
                if not LOCK_ENABLED:
                    continue
                ch = LOCK_CH
                sig = lock_signal(evs)
            elif key == "DL":
                if not DOGLOCK_ENABLED:
                    continue
                ch = DOGLOCK_CH
                sig = doglock_signal(evs)
                if sig is not None and now - sig["wall"] > DOGLOCK_CHASE:
                    # the crossing is stale (runner started mid-game): the
                    # price no longer reflects the moment the backtest bought
                    sig = None
            else:
                ch = S.CHANNELS[key]
                sig = (S.process_signal(evs, pre, ch) if ch["style"] == "process"
                       else S.settle_signal(evs, pre, ch))
            if sig is None:
                continue
            side_out = self.res["fav"] if ch["side"] == "fav" else \
                next((o for o in self.res["outcomes"] if o != self.res["fav"]), None)
            if side_out is None:
                continue
            tok = self.res["tokens"][self.res["outcomes"].index(side_out)]
            ask, _, _ = book(self.client, tok)
            if ask is None:
                log({"ev": "no_ask", "gid": self.gid, "ch": key})
                continue
            depth = book_depth(self.client, tok) or {}
            eff = ask + S.entry_cost(ask)
            ok = ((eff >= S.PRICE_FLOOR) if ch["style"] == "process"
                  else ch["price_min"] <= eff <= ch["price_max"])
            if not ok:
                if key == "DL":
                    # the band is checked at the crossing moment; a miss there
                    # means the game is skipped, not re-priced later (the same
                    # convention D uses in the backtest)
                    self.done.add("DL")
                log({"ev": "signal_skip_band", "gid": self.gid, "ch": key,
                     "ask": ask, "el": sig["el"], "m": sig["margin"]})
                continue
            with LOCK:
                occupied, _, _, _ = LEDGER.exposure()
                full = _pool_full(key, occupied)
            if full:
                if _slot_pool(key)[0] is not None:
                    # one lock per game: when the slots are full, skip this
                    # game rather than buy later at a higher (worse) price.
                    self.done.add(key)
                log({"ev": "signal_skip_cap", "gid": self.gid, "ch": key})
                continue
            bal = balance(self.client)
            if bal is None or bal < 2:
                continue
            # Size off total equity (cash + open positions at cost), not cash
            # alone.  Otherwise every new position shrinks the next one, and
            # with the slot caps lifted a busy slate would size itself to dust.
            # CASH_RESERVE still caps each individual order.
            with LOCK:
                deployed = sum(p["size"] * p["entry"] for p in ALL_POSITIONS)
            equity = bal + deployed
            usd = min(equity * _frac_of(key), bal * (1 - CASH_RESERVE))
            if usd < MIN_ORDER:
                log({"ev": "signal_skip_cash", "gid": self.gid, "ch": key,
                     "usd": round(usd, 2), "balance": round(bal, 2)})
                continue
            self.pending.add(key)
            self.inflight[key] = {"token": tok, "outcome": side_out, "ch": key,
                                  "usd": usd,
                                  "limit": float(min(Decimal(str(ask)) + Decimal(str(MAX_SLIP)),
                                                     Decimal("0.99"))),
                                  "deadline": now + BUY_WINDOW, "tries": 0,
                                  "style": ch["style"], "tp": ch["tp"],
                                  "ask": ask, "posted": False, "order_id": None,
                                  "el_sig": sig["el"],
                                  "t0": int(time.time())}
            log({"ev": "signal", "gid": self.gid, "ch": key, "outcome": side_out,
                 "ask": ask, "usd": round(usd, 2), "el": sig["el"],
                 "m": sig["margin"],
                 # depth at the moment of the signal: distinguishes "no size" from
                 # "price ran away" when a fill later fails
                 "ask_notional": depth.get("ask_notional"),
                 "asks": depth.get("asks")})

    # ---- exchange trade records (the only reliable fill source) ----
    def _trades(self, token):
        try:
            from py_clob_client_v2.clob_types import TradeParams
            return self.client.get_trades(TradeParams(asset_id=token)) or []
        except Exception:
            return []

    def _fills(self, token, side, seen, t0, order_id=None):
        # Used for exact attribution even when multiple channels own this token.
        from live_reconcile import parse_order_evidence
        fills, _ = parse_order_evidence(None, self._trades(token), order_id, token,
                                        side, self.res.get("market", ""))
        return [f for f in fills if f["id"] not in seen]

    def _log_fills(self, p, fills, side, oid):
        for fill in fills:
            size, price = float(fill["size"]), float(fill["price"])
            data = (dict(filled_shares=size, entry=price, usd=size*price) if side == "buy"
                    else dict(sold_shares=size, exit=price, reason=p.get("exit_reason")))
            log(dict(ev=side, gid=self.gid, ch=p["ch"], outcome=p["outcome"],
                     asset_id=p["token"], order_id=oid, fill_id=fill["id"], fills=1,
                     position_id=p.get("position_id") or p.get("attempt_id"),
                     attempt_id=p.get("close_attempt_id") or p.get("attempt_id"),
                     el_in=p.get("el_in", p.get("el_sig")), **data))
        if fills:
            with LOCK:
                first = not CONFIRMED[side]
                CONFIRMED[side] = True
            if first:
                log(dict(ev="CONFIRMED_"+side.upper(), gid=self.gid, ch=p["ch"], order_id=oid))

    def _work_inflight(self, g, now):
        from live_reconcile import order_evidence
        for key, q in list(self.inflight.items()):
            if not q["posted"] and q.get("attempt_id"):
                # POST may have returned before the local acknowledgement write failed.
                row = next((r for r in LEDGER.open_rows(self.gid)
                            if r["attempt_id"] == q["attempt_id"]), None)
                if row:
                    q.update(posted=True, order_id=row["order_id"])
            if g and g["state"] == "post":
                if q["posted"]:
                    reason = ("legacy_log_unverified" if "legacy_filled" in q else
                              LEDGER.exit_intent(self.gid, key) or "pending_buy_at_final")
                    LEDGER.handoff(self.gid, key, action="OPEN", order_id=q["order_id"],
                                   token=q["token"], market=self.res.get("market", ""), outcome=q["outcome"],
                                   reason=reason,
                                   reserved_usd=q.get("usd") or 0, position_id=q["attempt_id"])
                for p in list(self.positions):
                    if p["ch"] == key:
                        self._drop(p)
                self.inflight.pop(key)
                self.pending.discard(key)
                self.done.add(key)
                continue
            if q.get("posted"):
                oid = q["order_id"]
                known = LEDGER.trade_ids(oid)
                fills, terminal = order_evidence(self.client, oid, q["token"], "BUY",
                                                  self.res.get("market", ""), known)
                receipts = LEDGER.record_buy_fills(oid, fills)
                size = sum(Decimal(str(f["size"])) for f in receipts)
                if "legacy_filled" in q:
                    if terminal is None:
                        continue
                    expected = q["legacy_filled"]
                    if not terminal or (expected is not None and abs(float(terminal) - expected) > 1e-9):
                        LEDGER.handoff(self.gid, key, action="OPEN", order_id=oid, token=q["token"],
                                       market=self.res.get("market", ""), outcome=q["outcome"],
                                       reason="legacy_log_unverified", position_id=q["attempt_id"])
                        self.inflight.pop(key)
                        self.pending.discard(key)
                        continue
                    if size != terminal:
                        continue
                    LEDGER.record_buy_fills(oid, [], confirm_legacy=True)
                    del q["legacy_filled"]
                if size:
                    cost = sum(float(f["size"]) * float(f["price"]) for f in receipts)
                    entry = cost / float(size)
                    self._sync_position(q, float(size), entry)
                    self._log_fills(q, [f for f in fills if f["id"] not in known], "buy", oid)
                if terminal is None or terminal != size:
                    continue
                if size:
                    LEDGER.fill(q["attempt_id"], float(size), entry)
                else:
                    LEDGER.finish(q["attempt_id"], REJECTED_ZERO, oid, identity_verified=True)
                self.done.add(key)
                self.inflight.pop(key)
                self.pending.discard(key)
                continue
            if now > q["deadline"]:
                self.inflight.pop(key)
                self.pending.discard(key)
                log(dict(ev="buy_gave_up", gid=self.gid, ch=key, posted=False))
                continue

            q["tries"] += 1
            status, info = self._post_buy(q)
            if status == "posted":
                q["posted"] = True
                q["order_id"] = info.get("order_id")
                q["ask"] = info.get("ask", q.get("ask"))
                log({"ev": "buy_posted", "gid": self.gid, "ch": key,
                     "ask": info.get("ask"), "limit": info.get("limit"),
                     "order_id": info.get("order_id"), "usd": round(q["usd"], 2)})
            elif status == "rejected":
                q["last_err"] = info.get("err")
                # A 400 "invalid amounts" can never succeed on retry.
                if "invalid amounts" in str(q["last_err"]).lower():
                    q["deadline"] = 0
                # otherwise the ask/limit may come back; retry next tick
            else:
                # The request may have reached the book.  Freeze this signal
                # and wait for the trade record instead of risking a double.
                q["posted"] = True
                q["order_id"] = info.get("order_id")
                q["last_err"] = f"unknown: {info.get('err')}"
                log({"ev": "buy_unknown", "gid": self.gid, "ch": key,
                     "err": info.get("err"), "order_id": q["order_id"]})

    def _post_buy(self, q):
        if not self.live:
            return "rejected", {"err": "dry run"}
        ask, _, tick = book(self.client, q["token"])
        if ask is None:
            return "rejected", {"err": "no_ask"}
        best = Decimal(str(ask))
        step = max(Decimal(str(tick or "0.01")), Decimal("0.01"))
        cap = min(Decimal(str(q["limit"])), best + Decimal(str(MAX_SLIP)), 1 - step)
        limit = float((cap / step).to_integral_value(rounding=ROUND_FLOOR) * step)
        if Decimal(str(limit)) < best:
            return "rejected", {"err": f"limit {limit:.3f} < ask {ask:.3f}"}
        prepared = {}

        def on_prepared(oid):
            # Runs after signing and BEFORE the POST.  The ledger row is the
            # one-in-flight gate: if it already exists, abort the submission.
            with LOCK:
                cash = balance(self.client)
                if cash is None or cash < 2:
                    raise ValueError("cash unknown" if cash is None else "cash below minimum")
                keys, reserved, unknown, _basis = LEDGER.exposure()
                if (self.gid, q["ch"]) in keys:
                    raise DuplicateAttempt
                if _pool_full(q["ch"], keys):
                    raise ValueError("open cap")
                available = max(0., cash - reserved) * (1 - CASH_RESERVE)
                if unknown or q["usd"] > available:
                    raise ValueError("insufficient unreserved cash")
                _check_buy_deadline(q["deadline"])
                aid, ok = LEDGER.begin(self.gid, q["ch"], "OPEN", oid,
                                       int(time.time()), el_in=q.get("el_sig"),
                                       reserved_usd=q["usd"], token=q["token"],
                                       market=self.res.get("market", ""), outcome=q["outcome"])
                if not ok:
                    raise DuplicateAttempt
                prepared["aid"] = aid
                q["attempt_id"] = aid
                prepared["oid"] = oid
                q["order_id"] = oid

        try:
            res, oid = market_order(self.client, q["token"], "BUY",
                                    q["usd"], limit, str(step), on_prepared,
                                    live=self.live, buy_ceiling=cap, deadline=q["deadline"])
        except ExpiredBeforePost as e:
            if prepared.get("aid"):
                LEDGER.finish(prepared["aid"], REJECTED_ZERO, prepared["oid"])
            return "rejected", {"err": str(e)}
        except DuplicateAttempt:
            q["last_err"] = "duplicate OPEN"
            q["deadline"] = 0
            return "rejected", {"err": "duplicate OPEN"}
        except Exception as e:
            if prepared.get("aid") and LEDGER is not None:
                LEDGER.finish(prepared["aid"], UNKNOWN)
            if prepared.get("oid"):
                # The POST started but its outcome is unavailable.  Freeze
                # this signal and reconcile from the trade record; never
                # send a second order.
                return "unknown", {"err": str(e)[:120],
                                   "order_id": prepared["oid"]}
            status = getattr(e, "status_code", None)
            if status and 400 <= int(status) < 500:
                return "rejected", {"err": str(e)[:120], "status": status}
            return "rejected", {"err": str(e)[:120]}

        c = classify_post(res, oid)
        state = c.get("state", "UNKNOWN")
        if LEDGER is not None and prepared.get("aid"):
            LEDGER.finish(prepared["aid"],
                          "REJECTED_ZERO" if state == "REJECTED_ZERO"
                          else ("ACCEPTED" if state == "ACCEPTED" else "UNKNOWN"),
                          oid)
        if state == "REJECTED_ZERO":
            # Documented FOK-unfilled -> zero execution, safe to retry once.
            return "rejected", {"err": c.get("reason") or "FOK unfilled",
                                "order_id": oid,
                                "retryable": c.get("retryable", False)}
        # ACCEPTED or UNKNOWN: the request was accepted by the exchange.
        # Never re-post; let the trade record / balance reconciliation decide.
        return "posted", {"err": None, "ask": ask, "limit": limit,
                          "order_id": oid, "res": res, "state": state}

    # ---- exit ----
    def _manage_exits(self, g, now):
        if not self.live:
            return
        final = g["state"] == "post"
        elapsed = nd.elapsed_of(g["period"], g["detail"])
        for p in list(self.positions):
            key = p["ch"]
            spec = channel_spec(key)
            reason = p.get("exit_reason") or LEDGER.exit_intent(self.gid, key)
            if not reason and p["style"] == "process":
                if final or (elapsed is not None and elapsed >= S.FORCE_EL):
                    reason = "force"
                elif (spec.get("game_hold") and elapsed is not None and p.get("el_in") is not None
                      and elapsed - p["el_in"] >= spec["game_hold"]):
                    reason = "time"
                else:
                    _, bid, _ = book(self.client, p["token"])
                    if bid is not None and bid - S.exit_cost(bid) >= p["entry"] + p["tp"]:
                        reason = "target"
            elif not reason and not final and self.hist:
                margin = self.hist[-1][2]
                if ((spec.get("stop_margin") is not None and margin < spec["stop_margin"])
                        or (spec.get("stop_margin_hi") is not None and margin > spec["stop_margin_hi"])):
                    reason = "margin_stop"
            if reason:
                if not p.get("selling"):
                    log(dict(ev="exit", gid=self.gid, ch=key, outcome=p["outcome"],
                             reason=reason, size=p["size"], entry=p["entry"]))
                p.update(selling=True, exit_reason=reason)
                self._sell(p, finished=final)
            if final and p in self.positions:
                LEDGER.handoff(self.gid, key, token=p["token"], market=self.res.get("market", ""),
                               outcome=p["outcome"], reason=reason or "settle_hold",
                               size=p["size"], entry=p["entry"], position_id=p["position_id"])
                log(dict(ev="account_handoff", gid=self.gid, ch=key, size=p["size"],
                         reason=reason or "settle_hold", position_id=p["position_id"]))
                self._drop(p)

    def _sell(self, p, finished=False):
        """Advance one sale without sleeping or abandoning the signed order."""
        from live_reconcile import order_evidence
        if not self.live or LEDGER is None:
            return
        key = p["ch"]
        LEDGER.exit_intent(self.gid, key, p.get("exit_reason") or "resume_close")
        row = next((r for r in LEDGER.rows(self.gid) if r["action"] == "CLOSE"
                    and r["channel"] == key and r["state"] in (SUBMITTING, ACCEPTED, UNKNOWN)), None)
        if row is None:
            status, info = self._post_sell(p)
            if status not in ("posted", "unknown"):
                return
            row = next((r for r in LEDGER.rows(self.gid)
                        if r["action"] == "CLOSE" and r["channel"] == key
                        and r["order_id"] == info["order_id"]), None)
            if row is None:
                return
        oid = row["order_id"]
        p["close_attempt_id"] = row["attempt_id"]
        receipts, terminal = order_evidence(self.client, oid, p["token"], "SELL",
                                            self.res.get("market", ""), LEDGER.trade_ids(oid))
        added = LEDGER.record_sell_fills(oid, receipts)
        # Even an interrupted write-to-memory transition is reconstructed by owner.
        gross = p.get("fill_shares")
        if gross is not None:
            p["size"] = max(0., round(gross - LEDGER.sold_size(self.gid, key), 6))
        self._log_fills(p, added, "sell", oid)
        if terminal == 0:
            LEDGER.finish(row["attempt_id"], REJECTED_ZERO, oid, identity_verified=True)
        elif terminal is not None:
            LEDGER.finish_sell_order(row["attempt_id"], oid, terminal)
        if p["size"] <= 1e-9:
            self._drop(p)

    def _post_sell(self, p):
        if not self.live:
            return "rejected", {"err": "dry run"}
        if LEDGER is None:
            return "rejected", {"err": "live ledger unavailable"}
        bal = token_balance(self.client, p["token"])
        # The channel owns its confirmed buys minus its confirmed sales.
        fill = p.get("fill_shares")
        if fill is not None:
            backed = fill - LEDGER.sold_size(self.gid, p["ch"])
            if p["size"] > backed + 1e-9:
                log({"ev": "size_unbacked", "gid": self.gid, "ch": p["ch"],
                     "size": round(p["size"], 6),
                     "fill_shares": round(fill, 6),
                     "position_id": p.get("position_id"),
                     "el_in": p.get("el_in")})
                return "rejected", {"err": "size exceeds confirmed holding"}
        # Size the sell from THIS position, never from the wallet balance:
        # the balance is shared with any other channel holding this token.
        qty = float(Decimal(str(p["size"])).quantize(Decimal("0.01"), rounding=ROUND_FLOOR))
        if bal is not None:
            others = LEDGER.other_token_shares(self.gid, p["ch"], p["token"])
            if bal - others < qty - 1e-9:
                log({"ev": "sell_short_of_wallet", "gid": self.gid, "ch": p["ch"],
                     "own": round(qty, 6), "bal": round(bal, 6), "others": round(others, 6),
                     "position_id": p.get("position_id")})
                # Keep the exit intent so a delayed balance can recover next tick.
                return "rejected", {"err": "wallet cannot cover recorded channel holdings"}
        if qty <= 0:
            return "rejected", {"err": f"qty {qty} bal {bal}"}
        _, bid, tick = book(self.client, p["token"])
        if bid is None:
            return "rejected", {"err": "no_bid"}
        tick = str(tick or "0.01")
        limit = max(_round_down_tick(bid, tick), _price_step(tick))
        prepared = {}

        def on_prepared(oid):
            if LEDGER is not None:
                aid, ok = LEDGER.begin(self.gid, p["ch"], "CLOSE", oid,
                                       int(time.time()), el_in=p.get("el_in"),
                                       position_id=p.get("position_id", ""),
                                       requested_size=qty, token=p["token"],
                                       outcome=p["outcome"], market=self.res.get("market", ""))
                if not ok:
                    raise DuplicateAttempt
                prepared["aid"] = aid
                p["close_attempt_id"] = aid
            prepared["oid"] = oid

        try:
            res, oid = market_order(self.client, p["token"], "SELL", qty,
                                    limit, tick, on_prepared, live=self.live)
        except DuplicateAttempt:
            row = next((r for r in (LEDGER.rows(self.gid) if LEDGER else [])
                        if r["channel"] == p["ch"] and r["action"] == "CLOSE"),
                       None)
            if row and row["state"] in (SUBMITTING, UNKNOWN, ACCEPTED) and row["position_id"] == p.get("position_id"):
                p["close_attempt_id"] = row["attempt_id"]
                return "posted", {"err": "awaiting original CLOSE", "size": qty, "bid": bid,
                                  "order_id": row["order_id"]}
            return "rejected", {"err": "CLOSE has no available confirmed shares"}
        except Exception as e:
            if prepared.get("aid") and LEDGER is not None:
                LEDGER.finish(prepared["aid"], UNKNOWN)
            if prepared.get("oid"):
                return "unknown", {"err": str(e)[:120], "size": qty,
                                   "bid": bid, "order_id": prepared["oid"]}
            status = getattr(e, "status_code", None)
            if status and 400 <= int(status) < 500:
                return "rejected", {"err": str(e)[:120], "status": status}
            return "rejected", {"err": str(e)[:120], "size": qty, "bid": bid}

        c = classify_post(res, oid)
        state = c.get("state", "UNKNOWN")
        if LEDGER is not None and prepared.get("aid"):
            LEDGER.finish(prepared["aid"],
                          "REJECTED_ZERO" if state == "REJECTED_ZERO"
                          else ("ACCEPTED" if state == "ACCEPTED" else "UNKNOWN"),
                          oid)
        if state == "REJECTED_ZERO":
            return "rejected", {"err": c.get("reason") or "FOK unfilled",
                                "size": qty, "bid": bid, "order_id": oid,
                                "retryable": c.get("retryable", False)}
        return "posted", {"err": None, "size": qty, "bid": bid,
                          "order_id": oid, "res": res, "state": state}

    def _drop(self, p):
        if p in self.positions:
            self.positions.remove(p)
        with LOCK:
            if p in ALL_POSITIONS:
                ALL_POSITIONS.remove(p)


# ------------------------------------------------------------------ resume
def resume_plan():
    """Recent historical log claims for review; SQLite owns tradable inventory."""
    pos, done = {}, set()
    # Read yesterday as well as today.  A session that began before UTC
    # midnight and was restarted after it used to find an empty directory,
    # lose every entry (defaulting them to 0.5) and let the C time exit fire
    # instantly at a collapsed price.  Older day first, so buy-then-sell
    # ordering still holds.
    from datetime import timedelta
    try:
        days = [(datetime.strptime(TODAY, "%Y-%m-%d")
                 + timedelta(days=off)).strftime("%Y-%m-%d") for off in (-1, 0)]
    except Exception:
        days = [TODAY]
    files = []
    for day in days:
        base = os.path.join(ROOT, "data", "live_trade_final", day)
        if os.path.isdir(base):
            files += [os.path.join(base, r, "events.jsonl")
                      for r in sorted(os.listdir(base))]
    for fp in files:
        if not os.path.exists(fp):
            continue
        with open(fp) as f:
            lines = f.readlines()
        for line in lines:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if (LEDGER and r.get("account")
                    and str(r["account"]).casefold() != LEDGER.account.casefold()):
                continue
            ev = r.get("ev")
            if ev == "buy" and r.get("filled_shares"):
                k = (r.get("gid"), r.get("ch"), r.get("outcome"))
                try:
                    size = float(r["filled_shares"])
                except Exception:
                    continue
                if k not in pos:
                    pos[k] = {"entry": r.get("entry"), "size": size,
                              "sold": 0.0, "t_in": r.get("t"),
                              "el_in": r.get("el_in")}
                done.add((r.get("gid"), r.get("ch")))
            elif ev == "sell":
                k = (r.get("gid"), r.get("ch"), r.get("outcome"))
                if k not in pos:      # older sell logs have no outcome
                    cand = [kk for kk in pos
                            if kk[0] == r.get("gid") and kk[1] == r.get("ch")]
                    if len(cand) == 1:
                        k = cand[0]
                if k in pos:
                    try:
                        pos[k]["sold"] += float(r.get("sold_shares") or 0)
                    except Exception:
                        pass
    # Historical logs supply hints for review only. A signed OPEN owns the
    # tradeable quantity; the caller never uses a log value to place a sell.
    open_pos = {}
    for k, v in pos.items():
        rem = v["size"] - v["sold"]
        if rem > 1e-9:
            open_pos[k] = (v["entry"], rem, v.get("t_in"), v.get("el_in"),
                           v.get("order_id"), v.get("attempt_id"),
                           v.get("position_id"))
    return open_pos, done


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    live = args.live and not args.dry_run
    if live:
        with account_lock(load_env()["POLY_FUNDER"]):
            return _live_run()
    return run(live=False)


def _live_run():
    """Live run with the durable attempt ledger around it."""
    global LEDGER
    funder = load_env()["POLY_FUNDER"]
    db = os.path.join(ROOT, "var", "live_runner", "attempts.db")
    LEDGER = AttemptLedger(db, funder, recover=False)
    recovered = LEDGER.recover()
    log({"ev": "ledger", "db": db, "recovered": recovered})
    try:
        return run(live=True)
    finally:
        LEDGER.close_path()
        LEDGER = None


def start_board():
    """Launch the read-only team board alongside the runner (best effort)."""
    if not BOARD_ENABLED:
        return
    import subprocess
    import sys as _sys
    try:
        cmd = [_sys.executable, "-I", os.path.join(ROOT, "scripts", "team_dashboard.py"),
               "--runner", os.path.join(ROOT, "data", "live_trade_final"),
               "--host", BOARD_HOST, "--port", str(BOARD_PORT)]
        if LEDGER is not None:
            cmd += ["--account", LEDGER.account]
        if BOARD_AUTH:
            cmd += ["--auth", BOARD_AUTH]
        if BOARD_HOST not in ("127.0.0.1", "localhost") and not BOARD_AUTH:
            log({"ev": "board_skip", "why": "public host without BOARD_AUTH"})
            return
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        log({"ev": "board_start", "host": BOARD_HOST, "port": BOARD_PORT})
    except Exception as e:
        log({"ev": "board_err", "err": str(e)[:150]})


def run(live):
    global OUTDIR
    if live:
        return _run(live=True)
    previous = OUTDIR
    OUTDIR = os.path.join(ROOT, "data", "dry_run", TODAY, RUN_ID)
    try:
        return _run(live=False)
    finally:
        OUTDIR = previous


def _run(live):
    client = make_client() if live else make_public_client()
    bal = balance(client) if live else None
    try:
        revision = subprocess.check_output(["git", "-C", ROOT, "rev-parse", "--verify", "HEAD"],
                                           stderr=subprocess.DEVNULL, text=True, timeout=3).strip()
    except (OSError, subprocess.SubprocessError):
        revision = None
    log({"ev": "start", "live": live, "git_sha": revision, "balance": bal, "stake_frac": STAKE_FRAC,
         "stake_now": {k: round((bal or 0) * STAKE_FRAC * w, 2)
                       for k, w in (("A", 1.0), ("C", 1.0), ("D", 2.0))},
         "max_open": MAX_OPEN, "outdir": OUTDIR,
         "lock": {"on": LOCK_ENABLED, "frac": LOCK_FRAC, "margin": LOCK_MARGIN},
         "doglock": {"on": DOGLOCK_ENABLED, "frac": DOGLOCK_FRAC,
                     "margin": DOGLOCK_MARGIN, "min_el": DOGLOCK_MIN_EL,
                     "price": [DOGLOCK_PRICE_MIN, DOGLOCK_PRICE_MAX],
                     "stop_lead": DOGLOCK_STOP_LEAD}})
    filled, done = resume_plan() if live else ({}, set())
    rows = LEDGER.rows() if live and LEDGER is not None else []
    active = [r for r in rows if r["state"] in (SUBMITTING, ACCEPTED, UNKNOWN)
              or (r["state"] == FILLED and r["action"] == "OPEN")]
    owned = {(r["game_id"], r["channel"]) for r in rows}
    reviewed = {(r["game_id"], r["channel"]) for r in LEDGER.tasks(False)} if live and LEDGER else set()
    missing_claims = {k[:2] for k in filled} - owned - reviewed
    recovery = {row["game_id"] for row in active} | {gid for gid, _ in missing_claims}
    pending = bool(live and LEDGER and LEDGER.tasks())
    if live and bal is not None and bal < 2 and not recovery and not pending:
        log({"ev": "abort", "why": "balance too low", "balance": bal})
        return
    if live:
        start_board()
    traders = {}
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="recovery") as pool:
        try:
            _poll_games(live, client, traders, pool, recovery, filled, done, pending)
        finally:
            for t in traders.values():
                t.stop()
            for t in traders.values():
                t.join()
    log({"ev": "stopped", "open": len(ALL_POSITIONS), "balance": balance(client) if live else None,
         "traders": len(traders), "confirmed_buy": CONFIRMED["buy"],
         "confirmed_sell": CONFIRMED["sell"]})
    if not CONFIRMED["buy"]:
        log({"ev": "WARN", "msg": "no real fill observed this session"})
    elif not CONFIRMED["sell"]:
        log({"ev": "WARN", "msg": "a fill happened but no exit was confirmed"})


def _account_sweep():
    from live_reconcile import reconcile_account
    if LEDGER.tasks():
        reconcile_account(LEDGER, make_client(), log)


def _handoff_without_market(gid, claims):
    """A verified ESPN final can outlive the market listing or its pregame prices."""
    rows = LEDGER.rows(gid)
    channels = {r["channel"] for r in rows}
    for key in channels:
        by_action = {r["action"]: r for r in rows if r["channel"] == key}
        buy, sell = by_action.get("OPEN"), by_action.get("CLOSE")
        if sell and sell["state"] not in (SUBMITTING, ACCEPTED, UNKNOWN):
            sell = None
        if buy and buy["state"] not in (SUBMITTING, ACCEPTED, UNKNOWN, FILLED):
            buy = None
        if not buy and not sell:
            continue
        row = buy or sell
        tradable = (buy and buy["identity_verified"] and buy["market"] and buy["token"] and buy["outcome"]
                    and (buy["state"] != FILLED or (buy["bought_size"] and buy["entry_price"] is not None)))
        held = bool(tradable and buy["state"] == FILLED)
        size = max(0., (row["bought_size"] or 0) - LEDGER.sold_size(gid, key)) if held else 0.
        LEDGER.handoff(gid, key, action="HOLD" if held else row["action"],
                       order_id="" if held else row["order_id"],
                       token=row["token"], market=row["market"], outcome=row["outcome"],
                       reason=("final_without_listing" if tradable else "legacy_log_unverified"),
                       size=size, entry=row["entry_price"] or 0, position_id=row["position_id"] or row["attempt_id"])
    for (game, key, outcome), record in claims.items():
        if game == gid and key not in channels:
            LEDGER.handoff(gid, key, action="HOLD", token="", market="", outcome=outcome or "",
                           reason="legacy_log_unverified", size=record[1], entry=record[0] or 0,
                           identity_verified=False)


def _poll_games(live, client, traders, pool, recovery, filled, done, pending):
    from datetime import timedelta
    hong_kong = timezone(timedelta(hours=8))
    slate_day = datetime.fromtimestamp(time.time(), hong_kong).date()
    last_check = float("-inf")
    check = None
    archives, retry, archived, finished = {}, {}, {}, set()
    saw_live = False
    tick = 0
    while True:
        tick += 1
        now = time.monotonic()
        if live and LEDGER is not None and now - last_check >= 60 and (check is None or check.done()):
            if check is not None:
                try:
                    check.result()
                except Exception as exc:
                    log(dict(ev="account_check_error", err=str(exc)[:200]))
            check = pool.submit(_account_sweep)
            last_check = now
        try:
            games = {g["gid"]: g for g in scoreboard()}
        except Exception as exc:
            log(dict(ev="espn_err", err=str(exc)[:160]))
            time.sleep(8)
            continue
        # A restart after the slate ended can exit immediately. The raw feed
        # must identify this Hong Kong day's games. Archived recovery results
        # and yesterday's log claims do not identify the new scheduled slate.
        current_slate = False
        if games and all(g["state"] == "post" for g in games.values()):
            try:
                starts = [datetime.fromisoformat(str(g.get("start")).replace("Z", "+00:00"))
                          for g in games.values()]
                current_slate = all(start.tzinfo is not None
                                    and start.astimezone(hong_kong).date() == slate_day
                                    and start.timestamp() <= time.time() for start in starts)
            except (ValueError, TypeError):
                pass
        wanted = (recovery | traders.keys()) - games.keys() - finished
        for gid in wanted - archives.keys() - archived.keys():
            if now >= retry.get(gid, 0):
                archives[gid] = pool.submit(archived_final, gid)
        for gid, future in list(archives.items()):
            if future.done():
                del archives[gid]
                retry[gid] = now + 60
                try:
                    g = future.result()
                    if g and gid in wanted:
                        archived[gid] = g
                except Exception as exc:
                    log(dict(ev="archive_error", gid=gid, err=str(exc)[:160]))
        for gid in wanted & archived.keys():
            games[gid] = archived[gid]
        for gid, g in games.items():
            if gid in finished:
                continue
            final = g["state"] == "post"
            saw_live |= g["state"] == "in"
            t = traders.get(gid)
            if t is None and (g["state"] == "in" or (final and gid in recovery)):
                res = resolve(g, client)
                if res and not res.get("fav") and gid in recovery and live:
                    favorites = set()
                    for r in LEDGER.open_rows(gid):
                        if (r["identity_verified"] and r["market"] and r["market"] == res.get("market")
                                and r["token"] in res["tokens"]):
                            i = res["tokens"].index(r["token"])
                            favorites.add(res["outcomes"][i if channel_spec(r["channel"])["side"] == "fav" else 1-i])
                    if len(favorites) == 1:
                        res = dict(res, fav=favorites.pop())
                if res and res.get("fav"):
                    try:
                        t = GameTrader(gid, res, g["home"], live)
                    except (ValueError, KeyError) as exc:
                        log(dict(ev="recovery_market_error", gid=gid, err=str(exc)[:160]))
                if t is None:
                    if final and gid in recovery and live:
                        _handoff_without_market(gid, filled)
                        recovery.discard(gid)
                        finished.add(gid)
                        log(dict(ev="account_handoff", gid=gid, reason="final_without_listing"))
                    continue
                t.done.update(ch for game, ch in done if game == gid)
                if live and LEDGER is not None:
                    owned = {r["channel"] for r in LEDGER.rows(gid)}
                    for (game, ch, outcome), record in filled.items():
                        if game != gid or ch in owned:
                            continue
                        entry, size = record[:2]
                        token = res["tokens"][res["outcomes"].index(outcome)] if outcome in res["outcomes"] else ""
                        LEDGER.handoff(gid, ch, token=token,
                                       market=res.get("market", "") if token else "", outcome=outcome or "",
                                       reason="legacy_log_unverified", size=size,
                                       entry=entry or 0, identity_verified=False)
                traders[gid] = t
                t.start()
            if t is not None:
                t.put(g)
                if final and t.flat():
                    t.stop()
                    t.join()
                    del traders[gid]
                    finished.add(gid)
                    recovery.discard(gid)
                    log(dict(ev="trader_released", gid=gid, remaining=len(traders)))
        if tick % 100 == 0:
            cash = balance(client) if live else None
            with LOCK:
                positions = [(p["gid"], p["ch"], p["outcome"], p["entry"], p["size"]) for p in ALL_POSITIONS]
            log(dict(ev="heartbeat", balance=cash, open=len(positions), traders=len(traders),
                     positions=positions, confirmed=dict(CONFIRMED)))
        if (games and all(g["state"] == "post" for g in games.values())
                and not recovery and not traders and (saw_live or current_slate)):
            if check is not None:
                try:
                    check.result()
                except Exception as exc:
                    log(dict(ev="account_check_error", err=str(exc)[:200]))
            if live and LEDGER is not None and LEDGER.tasks():
                # The earlier sweep can predate the last game's handoff.
                try:
                    _account_sweep()
                except Exception as exc:
                    log(dict(ev="account_check_error", err=str(exc)[:200]))
            log(dict(ev="day_over", games=len(games),
                     account_pending=len(LEDGER.tasks()) if live and LEDGER else 0))
            break
        time.sleep(POLL)


if __name__ == "__main__":
    main()
