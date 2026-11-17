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

SIGNAL TIMING — evaluated on every tick, not only on score changes: the 120s
lookback keeps moving, so a threshold can be crossed while the score sits
still. Score-change-only evaluation missed 39% of the backtest's signals.

FILLS — the conditional-token balance is the ground truth. A FAK that
partially fills raises 400 and the order object disappears, but the shares are
real. `done` is set only after a confirmed fill, so a failed entry stays
retryable and a crashed worker is logged as worker_err instead of vanishing.

COST — real exchange fee measured on-chain 2026-11-13: about 0.10*p*(1-p) per
round trip on top of the tick spread (strategy.entry_cost / exit_cost).

Usage
  python3 -I scripts/live_runner.py --dry-run
  python3 -I scripts/live_runner.py --live
Must run under ~/.polymarket/venv/bin/python (has py_clob_client_v2).
"""
import argparse, json, math, os, queue, sys, threading, time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nba_data as nd
import strategy as S
from executor_lock import account_lock
from live_ledger import (AttemptLedger, SUBMITTING, ACCEPTED, UNKNOWN,
                         FILLED, CLOSED, REJECTED_ZERO)
from executor_clob import classify_post

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


def _frac_of(ch):
    if ch == "L":
        return LOCK_FRAC
    if ch == "DL":
        return DOGLOCK_FRAC
    return STAKE_FRAC * S.CHANNELS[ch]["weight"]


# ---- ownership -------------------------------------------------------------
# One signal = one position = one order (the ledger enforces one OPEN per
# game/channel).  A position therefore records the signal that produced it
# (`el_in`), the order (`position_id` = attempt id) and the shares that order
# actually bought (`fill_shares`).  `size` starts at `fill_shares`, falls as
# the position is sold, and only grows when unmatched wallet shares are
# adopted (`adopted`, sole-holder case only).
#
# A/C/D/DL buy the same DOG token and the exchange sells fungible shares, so
# there is no "sell order X's shares" instruction -- the id decides WHICH
# signal may sell and on what rule, the quantity decides how many leave the
# wallet.  That quantity is the position's own `size`, never the balance,
# which is the sum of every channel holding the token.


BUY_WINDOW = 15          # the price is flat for ~15s, then drifts 2-3c/min
SELL_WINDOW = 900
# `_sell` runs inside the game's trader thread, so a sell that cannot fill
# freezes every other position in that game -- including a DL margin stop on
# a collapsing lead.  A stop must not hold the thread for the full window.
STOP_SELL_WINDOW = 120
RECONCILE_GRACE = 5      # seconds to let the balance API catch up before reporting
REPOST_AFTER = 30        # seconds to wait before re-checking an unfilled sell
MAX_SLIP = 0.03
RETRY_GAP = 3
DUST = 0.02              # shares; below this a position is closed for accounting

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


class PreparedAfterPost(Exception):
    pass


def log(rec):
    rec = dict(rec)
    rec.setdefault("t", time.time())
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


def make_client():
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds
    env = load_env()
    creds = json.load(open(os.path.expanduser("~/.polymarket/creds.json")))
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


def _signed_market_order(client, token, side, amount, price_limit, tick):
    """Build and sign the FOK order WITHOUT posting it; return (signed, oid).

    The order hash is the identity the exchange's trade record uses as
    `taker_order_id`, so it can be written to the attempt ledger before the
    POST. If the POST outcome is unclear the reconciler still knows what to
    look for.
    """
    from py_clob_client_v2.clob_types import (MarketOrderArgsV2, OrderType,
                                              PartialCreateOrderOptions)
    from py_clob_client_v2.order_builder.builder import ExchangeOrderBuilderV2
    d = 3 if float(tick or "0.01") < 0.01 else 2
    args = MarketOrderArgsV2(token_id=token, amount=round(amount, 2), side=side,
                             price=round(price_limit, d), order_type=OrderType.FOK)
    options = PartialCreateOrderOptions(tick_size=str(tick))
    signed = client.create_market_order(args, options)
    address = getattr(getattr(client, "builder", None), "funder", None) \
        or load_env()["POLY_FUNDER"]
    builder = ExchangeOrderBuilderV2(address, 137, client.signer,
                                     domain_version="2")
    oid = builder.build_order_hash(builder.build_order_typed_data(signed))
    return signed, oid


def market_order(client, token, side, amount, price_limit, tick,
                 on_prepared=None):
    """amount = USD for BUY, shares for SELL; price_limit caps slippage.

    Market BUY orders are capped by the server at 2dp for the maker (USDC)
    amount and 4dp for the taker (shares) amount.  The 0.001 tick's rounding
    config asks for 5dp, which the server rejects with "invalid amounts".
    Buys therefore pass tick 0.01 explicitly; sells keep the book tick so a
    bid of 0.015 can still be hit.

    `on_prepared(oid)` runs after signing, BEFORE `post_order`; it is where the
    attempt ledger is written. It may raise to abort the submission.
    """
    from py_clob_client_v2.clob_types import OrderType
    signed, oid = _signed_market_order(client, token, side, amount,
                                       price_limit, tick)
    if on_prepared is not None:
        on_prepared(oid)
    res = client.post_order(signed, OrderType.FOK)
    return res, oid


def _price_step(tick):
    return 10 ** -(3 if float(tick or "0.01") < 0.01 else 2)


def _round_up_tick(x, tick):
    step = _price_step(tick)
    return round(math.ceil((x - 1e-9) / step) * step, 3 if step < 0.01 else 2)


def order_matched(client, order_id):
    """Shares the exchange says THIS order matched, or None if unknown.

    The per-order record is the only ownership evidence that is not shared:
    the token balance is the sum of every channel holding the token AND it
    lags, so using it to decide "did my sell fill?" both double-sells and
    sells a sibling's shares.
    """
    if not order_id:
        return None
    try:
        o = client.get_order(order_id)
    except Exception:
        return None
    if not isinstance(o, dict):
        return None
    for k in ("size_matched", "sizeMatched", "matched_size", "filled_size"):
        v = o.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    st = str(o.get("status") or "").upper()
    if st in ("CANCELED", "CANCELLED", "UNMATCHED", "EXPIRED"):
        return 0.0
    return None


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
        self.client = make_client()        # own client: no cross-thread state
        self.q = queue.Queue(maxsize=1)
        self.stop_evt = threading.Event()
        self.hist = []
        self.done = set()
        self.pending = set()
        self.positions = []
        self.inflight = {}
        self.sell_threads = []       # sells in flight, so stop() can wait
        self.tokens_seen = set()     # every token this game ever held
        self.seen_ids = set()      # trade IDs already attributed to a position
        if LEDGER is not None:
            # A persisted OPEN attempt means this game/channel was already
            # sent once; never enter it again, even after a restart.
            for row in LEDGER.open_rows(gid):
                self.done.add(row["channel"])

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
        self.join_sells(timeout=20)

    def flat(self):
        return not self.positions and not self.inflight

    def log_reconcile(self):
        """One line per game: the wallet vs what this trader says it holds.

        Every token this game touched must be back to zero once the trader is
        flat.  Anything still in the wallet there was never attributed to a
        channel -- invisible to the P&L, invisible to the stop, but real.
        """
        if not self.tokens_seen:
            return
        held = {tok: token_balance(self.client, tok)
                for tok in sorted(self.tokens_seen)}
        if any(h is not None and h > DUST for h in held.values()):
            time.sleep(RECONCILE_GRACE)       # the balance API lags a sell
            held = {tok: (token_balance(self.client, tok)
                          if h is not None and h > DUST else h)
                    for tok, h in held.items()}
        out = {}
        for tok, h in held.items():
            accounted = round(sum(p["size"] for p in self.positions
                                  if p["token"] == tok), 6)
            out[tok[:12]] = {"held": None if h is None else round(h, 6),
                             "accounted": accounted}
        log({"ev": "reconcile_summary", "gid": self.gid, "tokens": out,
             "clean": all(v["held"] is None or v["held"] <= DUST
                          for v in out.values())})

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
        if not self.live or len(self.hist) < 3:
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
            pool, cap = _slot_pool(key)
            with LOCK:
                if pool is None:
                    n_open = sum(1 for x in ALL_POSITIONS
                                 if _slot_pool(x["ch"])[0] is None)
                else:
                    n_open = sum(1 for x in ALL_POSITIONS if x["ch"] == pool)
            if n_open >= cap:
                if pool is not None:
                    # one lock per game: when the slots are full, skip this
                    # game rather than buy later at a higher (worse) price.
                    self.done.add(key)
                log({"ev": "signal_skip_cap", "gid": self.gid, "ch": key})
                continue
            bal = balance(self.client)
            if bal is None:
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
                                  "limit": min(ask + MAX_SLIP, 0.99),
                                  "deadline": now + BUY_WINDOW, "tries": 0,
                                  "style": ch["style"], "tp": ch["tp"],
                                  "ask": ask, "posted": False, "order_id": None,
                                  "el_sig": sig["el"],
                                  # trade IDs that existed before this signal:
                                  # anything newer is this order's fill.
                                  "seen": self._trade_ids(tok),
                                  "t0": int(time.time()),
                                  "base_bal": token_balance(self.client, tok)}
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

    def _trade_ids(self, token):
        return {t.get("id") for t in self._trades(token) if t.get("id")}

    def _fills(self, token, side, seen, t0, order_id=None):
        """Confirmed trades that belong to this action, not an earlier one.

        The token balance is deliberately not used to decide whether an order
        filled: it lags by seconds and that lag is exactly what caused the
        duplicate Thunder buy.  When the order response carries an orderID we
        match the trade's taker_order_id exactly; otherwise per-action `seen`
        IDs plus match_time separate A's fill from C's on the same token.
        """
        out, loose = [], []
        for t in self._trades(token):
            if (t.get("status") or "").upper() != "CONFIRMED":
                continue
            if (t.get("side") or "").upper() != side:
                continue
            tid = t.get("id")
            if not tid or tid in self.seen_ids:
                continue
            try:
                mt = int(t.get("match_time") or 0)
            except Exception:
                mt = 0
            if order_id:
                if str(t.get("taker_order_id") or "") == str(order_id):
                    out.append(t)
                elif tid not in seen and (not mt or mt >= int(t0) - 2):
                    loose.append(t)
            else:
                if tid in seen:
                    continue
                if mt and mt < int(t0) - 2:
                    continue
                out.append(t)
        if out:
            return out
        # If the response's orderID format ever differs from the trade's
        # taker_order_id, accept a single unambiguous new trade rather than
        # silently dropping the fill -- but only when nothing else on this
        # token could have produced it.  With A/C/DL all holding the dog, one
        # unclaimed trade on a shared token is usually the OTHER channel's,
        # so guessing there is worse than waiting for the record.
        if not (order_id and len(loose) == 1):
            return []
        oid = str(order_id)
        others = [x for x in self.positions if x["token"] == token]
        others += [x for x in self.inflight.values()
                   if x["token"] == token
                   and str(x.get("order_id") or "") != oid]
        return loose if not others else []

    def _open_position(self, q, key, shares, entry, nfills):
        self.inflight.pop(key, None)
        self.pending.discard(key)
        self.done.add(key)
        pos = {"gid": self.gid, "ch": key, "token": q["token"],
               "outcome": q["outcome"], "entry": entry, "size": shares,
               "t_in": time.time(), "el_in": q.get("el_sig"),
               "position_id": q.get("attempt_id"),
               "style": q["style"], "tp": q["tp"], "selling": False,
               # what THIS order bought -- the anchor every sell is checked
               # against, and the audit link back to the signal
               "fill_shares": shares, "adopted": 0.0}
        self.positions.append(pos)
        self.tokens_seen.add(pos["token"])
        with LOCK:
            ALL_POSITIONS.append(pos)
            first = not CONFIRMED["buy"]
            CONFIRMED["buy"] = True
        if LEDGER is not None and q.get("attempt_id"):
            LEDGER.fill(q["attempt_id"], shares, entry)
        log({"ev": "buy", "gid": self.gid, "ch": key, "outcome": q["outcome"],
             "filled_shares": shares, "entry": entry,
             "usd": round(shares * entry, 2), "tries": q["tries"],
             "order_id": q.get("order_id"), "fills": nfills,
             "attempt_id": q.get("attempt_id"),
             "position_id": q.get("attempt_id"),
             "el_in": q.get("el_sig")})
        if first:
            log({"ev": "CONFIRMED_BUY", "gid": self.gid, "ch": key,
                 "shares": shares, "entry": entry})

    def _work_inflight(self, g, now):
        for key, q in list(self.inflight.items()):
            # 1) a confirmed trade ends the signal, before any retry decision.
            #    Nothing is checked before the order is posted: a trade that
            #    appears then belongs to another action on this token.
            fills = (self._fills(q["token"], "BUY", q["seen"], q["t0"],
                                 q.get("order_id"))
                     if q.get("posted") else [])
            if fills:
                shares = sum(float(t["size"]) for t in fills)
                notional = sum(float(t["size"]) * float(t["price"]) for t in fills)
                if shares > 0:
                    for t in fills:
                        if t.get("id"):
                            q["seen"].add(t["id"])
                            self.seen_ids.add(t["id"])
                    self._open_position(q, key, shares, notional / shares, len(fills))
                    continue

            # 2) deadline.  If an order was posted but the trade API never
            #    showed it, use the balance delta once -- only when no other
            #    position shares this token, so the delta cannot be theirs.
            if now > q["deadline"]:
                # Only another POSTED buy on this token can move the balance
                # behind our back.  Existing positions do not change it (and
                # sells run on this same thread), so the delta from base_bal
                # is exactly our fill even when A/C/DL share the token.
                others_inflight = [x for k2, x in self.inflight.items()
                                   if k2 != key and x["token"] == q["token"]
                                   and x.get("posted")]
                bal = (token_balance(self.client, q["token"])
                       if not others_inflight else None)
                if (q.get("posted") and bal is not None
                        and q.get("base_bal") is not None
                        and bal > q["base_bal"] + 1e-9):
                    shares = bal - q["base_bal"]
                    # Consume the trade IDs we are about to write off; they
                    # must not be attributed to a later signal on this token.
                    for t in self._trades(q["token"]):
                        if ((t.get("side") or "").upper() == "BUY"
                                and t.get("id")):
                            self.seen_ids.add(t["id"])
                    self._open_position(q, key, shares, q.get("ask") or 0.5, 0)
                    continue
                self.inflight.pop(key, None)
                self.pending.discard(key)
                if LEDGER is not None and q.get("attempt_id"):
                    LEDGER.finish(q["attempt_id"], UNKNOWN)
                log({"ev": "buy_gave_up", "gid": self.gid, "ch": key,
                     "tries": q["tries"], "posted": q.get("posted"),
                     "order_id": q.get("order_id"),
                     "attempt_id": q.get("attempt_id"),
                     "reason": q.get("last_err")})
                continue

            # 3) never post a second order for the same signal.  The balance
            #    and trade APIs can lag; a retry in that window doubles the
            #    position (Thunder D, 2026-11-14).
            if q.get("posted"):
                continue

            with LOCK:
                # Locks (L, DL) each have their own slot pool; A/C/D must not
                # be crowded out by them (and vice versa).  The signal-time
                # check above uses the same split, so keep it consistent here.
                pool, cap = _slot_pool(q.get("ch"))
                if pool is None:
                    n_open = sum(1 for x in ALL_POSITIONS
                                 if _slot_pool(x["ch"])[0] is None)
                else:
                    n_open = sum(1 for x in ALL_POSITIONS if x["ch"] == pool)
                if n_open >= cap:
                    q["last_err"] = "open cap"
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
        ask, _, tick = book(self.client, q["token"])
        if ask is None:
            return "rejected", {"err": "no_ask"}
        if ask > q["limit"] + 1e-9:
            return "rejected", {"err": f"ask {ask:.3f} > {q['limit']:.3f}"}
        # Force 0.01 for BUY market orders (see market_order docstring).
        btick = "0.01" if float(tick or "0.01") < 0.01 else str(tick)
        limit = min(_round_up_tick(ask + MAX_SLIP, btick), 0.99)
        if limit < ask:
            return "rejected", {"err": f"limit {limit:.3f} < ask {ask:.3f}"}
        prepared = {}

        def on_prepared(oid):
            # Runs after signing and BEFORE the POST.  The ledger row is the
            # one-in-flight gate: if it already exists, abort the submission.
            prepared["oid"] = oid
            q["order_id"] = oid
            if LEDGER is not None:
                aid, ok = LEDGER.begin(self.gid, q["ch"], "OPEN", oid,
                                       int(time.time()), el_in=q.get("el_sig"))
                if not ok:
                    raise DuplicateAttempt
                prepared["aid"] = aid
                q["attempt_id"] = aid

        try:
            res, oid = market_order(self.client, q["token"], "BUY",
                                    q["usd"], limit, btick, on_prepared)
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
    def _exit_reason(self, p, g, bid, m_now, el, finished):
        """The one place that decides WHY a position leaves.  Live and dry
        runs share it, so a dry run cannot drift from what live would do."""
        if p["style"] != "process":
            if g["state"] == "post":
                return "settle_hold"
            try:
                spec = channel_spec(p["ch"])
            except KeyError:      # a resumed row with an unknown channel
                spec = {}
            stop = spec.get("stop_margin")
            stop_hi = spec.get("stop_margin_hi")
            if m_now is not None:
                if stop is not None and m_now < stop:
                    return "margin_stop"
                if stop_hi is not None and m_now > stop_hi:
                    return "margin_stop"
            return None
        if (bid is not None and p.get("tp") is not None
                and (bid - S.exit_cost(bid)) >= p["entry"] + p["tp"]):
            return "target"
        hold = S.CHANNELS[p["ch"]].get("game_hold")
        if (hold and p.get("el_in") is not None and el is not None
                and el - p["el_in"] >= hold):
            return "time"
        if finished or (el is not None and el >= S.FORCE_EL):
            return "force"
        return None

    def _start_sell(self, p, reason, finished, bid=None, m_now=None):
        """Hand the sell to its own thread.

        `_sell` runs for the whole sell window waiting on a FOK.  Doing that
        inline froze the game's only trader thread, so one un-fillable sell
        also stopped every other position in that game -- including a DL
        margin stop on a collapsing lead.  `p["selling"]` is still the
        one-sell-at-a-time gate, now set under the lock.
        """
        with LOCK:
            if p.get("selling") or time.time() < p.get("_retry_after", 0):
                return
            p["selling"] = True
            p["exit_reason"] = reason
        log({"ev": "exit", "gid": self.gid, "ch": p["ch"],
             "outcome": p["outcome"], "reason": reason, "bid": bid,
             "entry": p["entry"], "size": p["size"], "m_fav": m_now})
        th = threading.Thread(target=self._sell, args=(p, finished),
                              daemon=True, name=f"sell-{self.gid}-{p['ch']}")
        self.sell_threads.append(th)
        th.start()

    def join_sells(self, timeout=45):
        for th in list(self.sell_threads):
            th.join(timeout=timeout)
        self.sell_threads = [th for th in self.sell_threads if th.is_alive()]

    def _manage_exits(self, g, now):
        m_now = self.hist[-1][2] if self.hist else None
        el = nd.elapsed_of(g["period"], g["detail"])
        finished = (g["state"] == "post") or (el is not None and el >= 2880)
        for p in list(self.positions):
            if p.get("selling"):
                continue
            # A dry run restores positions from the log exactly like a live
            # one, so everything below would fire against the REAL wallet.
            # Dry mode decides the same way and only reports.
            if not self.live:
                reason = self._exit_reason(p, g, book(self.client, p["token"])[1],
                                           m_now, el, finished)
                if reason and not p.get("_dry_logged"):
                    p["_dry_logged"] = True
                    log({"ev": "dry_exit", "gid": self.gid, "ch": p["ch"],
                         "outcome": p["outcome"], "reason": reason,
                         "entry": p["entry"], "size": p["size"],
                         "m_fav": m_now, "note": "dry run: nothing was sold"})
                continue
            if p["style"] != "process":
                reason = self._exit_reason(p, g, None, m_now, el, finished)
                if reason == "margin_stop":
                    self._start_sell(p, reason, finished,
                                     bid=book(self.client, p["token"])[1],
                                     m_now=m_now)
                elif reason == "settle_hold":
                    log({"ev": "settle_hold", "gid": self.gid, "ch": p["ch"],
                         "outcome": p["outcome"], "entry": p["entry"],
                         "size": p["size"], "note": "held to resolution"})
                    self._drop(p)
                continue
            if p["size"] < DUST:
                log({"ev": "reconcile_closed", "gid": self.gid, "ch": p["ch"],
                     "outcome": p["outcome"], "why": "dust", "size": p["size"]})
                self._drop(p)
                continue
            held = token_balance(self.client, p["token"])
            if held is not None and held < DUST:
                # The balance API lags right after a buy; require 30s of
                # consistently zero shares before declaring the position gone.
                if p.get("zero_since") is None:
                    p["zero_since"] = now
                    continue
                if now - p["zero_since"] >= 30:
                    log({"ev": "reconcile_closed", "gid": self.gid, "ch": p["ch"],
                         "outcome": p["outcome"], "why": "balance gone",
                         "held": held, "size": p["size"]})
                    self._drop(p)
                    continue
            else:
                p.pop("zero_since", None)
            # A missed fill (e.g. an earlier POST whose trade record was not
            # attributed) shows up as token shares we do not account for.
            # Add the excess to this position so it is sold/managed instead
            # of sitting invisible in the wallet.
            ours = sum(x["size"] for x in self.positions
                       if x["token"] == p["token"])
            # A/C share the dog token.  If another channel's BUY is still in
            # flight (posted but its trade record has not landed yet), the
            # excess shares belong to that order, not to this position --
            # folding them in here is what let C swallow A's fills.  Only
            # reconcile shares that no pending order can explain.
            pending_same = any(q["token"] == p["token"]
                               for q in self.inflight.values())
            if held is not None and held > ours + DUST and not pending_same:
                extra = held - ours
                # Shares whose own order record never arrived.  Only fold them
                # in when this position is the sole holder of the token; with
                # A/C/DL all on the dog, an unattributed lot is far more
                # likely to belong to another channel than to this one.
                siblings = [x for x in self.positions
                            if x["token"] == p["token"] and x is not p]
                if siblings:
                    if not p.get("_excess_logged"):
                        p["_excess_logged"] = True
                        log({"ev": "unattributed_excess", "gid": self.gid,
                             "ch": p["ch"], "held": held,
                             "accounted": round(ours, 6),
                             "extra": round(extra, 6),
                             "holders": [x["ch"] for x in siblings]})
                else:
                    p["size"] += extra
                    p["adopted"] = (p.get("adopted") or 0.0) + extra
                    log({"ev": "reconcile_add", "gid": self.gid, "ch": p["ch"],
                         "outcome": p["outcome"], "held": held,
                         "accounted": round(ours, 6),
                         "extra": round(extra, 6),
                         "size": round(p["size"], 6),
                         "fill_shares": round(p.get("fill_shares") or 0, 6)})
            _, bid, _ = book(self.client, p["token"])
            if bid is None and finished:
                log({"ev": "no_exit", "gid": self.gid, "ch": p["ch"],
                     "outcome": p["outcome"], "size": p["size"],
                     "note": "final, no bid"})
                self._drop(p)
                continue
            if bid is None and not finished:
                continue
            reason = self._exit_reason(p, g, bid, m_now, el, finished)
            if reason is None:
                continue
            self._start_sell(p, reason, finished, bid=bid)

    def _sell(self, p, finished=False):
        """Sell THIS position's shares only.

        The token balance may be shared by A and C; selling floor(balance)
        would liquidate the other channel too.  We post the position's own
        size, and confirm from the trade record.  A full sell leaves < 0.01
        share of dust (size precision is 2dp), which is treated as closed
        instead of retrying for 15 minutes.
        """
        window = (STOP_SELL_WINDOW if p.get("exit_reason") == "margin_stop"
                  else SELL_WINDOW)
        deadline = time.time() + window
        seen = self._trade_ids(p["token"])
        t0 = int(time.time())
        last = "no_attempt"
        posted = False
        posted_at = 0.0
        posted_unknown = False
        order_id = None
        while time.time() < deadline and not self.stop_evt.is_set():
            fills = self._fills(p["token"], "SELL", seen, t0,
                                order_id if posted else None)
            if fills:
                sold = sum(float(t["size"]) for t in fills)
                notional = sum(float(t["size"]) * float(t["price"]) for t in fills)
                px = notional / sold if sold else None
                for t in fills:
                    if t.get("id"):
                        seen.add(t["id"])
                        self.seen_ids.add(t["id"])
                p["size"] = max(0.0, p["size"] - sold)
                with LOCK:
                    first = not CONFIRMED["sell"]
                    CONFIRMED["sell"] = True
                log({"ev": "sell", "gid": self.gid, "ch": p["ch"],
                     "outcome": p["outcome"], "reason": p.get("exit_reason"),
                     "sold_shares": sold, "exit": px,
                     "remaining": round(p["size"], 6), "fills": len(fills),
                     "order_id": order_id,
                     "position_id": p.get("position_id"),
                     "el_in": p.get("el_in")})
                if first:
                    log({"ev": "CONFIRMED_SELL", "gid": self.gid, "ch": p["ch"],
                         "sold_shares": sold, "exit": px})
                self._close_attempt(p)
                if p["size"] < DUST:
                    self._drop(p)
                    return
                posted = False
                posted_unknown = False
                order_id = None
                t0 = int(time.time())
                continue

            # A posted order is in flight: wait for its trade record.  For an
            # ambiguous (timeout) post, after 30s re-check the balance; if the
            # shares are still there, the order did not execute and a retry is
            # safe.  A second sell of already-sold shares would be rejected by
            # the exchange anyway, so this cannot double the position.
            if posted:
                # A FOK that cannot fill (thin book at the limit, or the bid
                # moved between the exit and the post) is killed on arrival yet
                # still reports "posted".  Waiting out the full SELL_WINDOW on
                # it freezes the position -- and blocks _manage_exits via
                # p["selling"] -- while the price runs away.  After 30s check
                # the shares again; if they are still ours the order did not
                # fill, so repost at the new bid.  Selling already-sold shares
                # would be rejected by the exchange, so this cannot double.
                if time.time() - posted_at > REPOST_AFTER:
                    # Repost only on positive evidence that OUR order matched
                    # nothing.  The wallet balance is not evidence: it is the
                    # sum of every channel on this token and it lags exactly
                    # when it matters, so a filled sell reads as "still mine"
                    # and the repost sells a sibling's shares.  No evidence
                    # (API error, no order id) means wait, not guess.
                    matched = order_matched(self.client, order_id)
                    if matched is not None and matched <= DUST:
                        posted = False
                        posted_unknown = False
                        order_id = None
                        last = "exchange reports 0 matched"
                time.sleep(RETRY_GAP)
                continue

            status, info = self._post_sell(p)
            if status in ("posted", "unknown"):
                posted = True
                posted_at = time.time()
                posted_unknown = (status == "unknown")
                order_id = info.get("order_id")
                last = (f"{status} {info.get('size')}@{info.get('bid')} "
                        f"{info.get('order_id')}")
            else:
                last = info.get("err")
                if info.get("terminal"):
                    # nothing to sell: stop this attempt instead of hammering
                    # the book every RETRY_GAP for the whole window.  Once the
                    # game is final this must still close the position, or the
                    # trader never goes flat and the day never ends.
                    with LOCK:
                        p["selling"] = False
                    # do not respawn a sell thread on the very next tick:
                    # nothing changed, and the lead the stop reacted to has
                    # not had time to recover
                    p["_retry_after"] = time.time() + 30
                    log({"ev": "sell_gave_up", "gid": self.gid, "ch": p["ch"],
                         "outcome": p["outcome"],
                         "reason": p.get("exit_reason"),
                         "remaining": round(p["size"], 6), "last": last})
                    if finished:
                        log({"ev": "reconcile_closed", "gid": self.gid,
                             "ch": p["ch"], "outcome": p["outcome"],
                             "why": "game over, not ours to sell",
                             "size": round(p["size"], 6)})
                        self._drop(p)
                    return
            time.sleep(RETRY_GAP)
        with LOCK:
            p["selling"] = False
        self._close_attempt(p)
        if p["size"] < DUST:
            self._drop(p)
            return
        # Unsellable positions must not retry forever.  A collapsed bid (no_bid
        # / below the min tick) or an empty balance can never be sold, and once
        # the game is final there is no bid at all.  Close them at zero so the
        # trader reaches flat and day_over can fire instead of blocking until
        # RuntimeMaxSec.
        last_s = str(last)
        if finished or last_s.startswith("qty 0"):
            log({"ev": "reconcile_closed", "gid": self.gid, "ch": p["ch"],
                 "outcome": p["outcome"],
                 "why": "balance gone" if last_s.startswith("qty 0")
                        else "game over, unsellable",
                 "size": round(p["size"], 6), "last": last})
            self._drop(p)
            return
        log({"ev": "sell_gave_up", "gid": self.gid, "ch": p["ch"],
             "outcome": p["outcome"], "reason": p.get("exit_reason"),
             "remaining": round(p["size"], 6), "last": last})

    def _close_attempt(self, p):
        if LEDGER is not None and p.get("close_attempt_id"):
            LEDGER.close(p["close_attempt_id"])
            p.pop("close_attempt_id", None)

    def _post_sell(self, p):
        bal = token_balance(self.client, p["token"])
        # Check the position still owns what it is about to sell: the shares
        # this signal's order bought, plus anything the wallet proved was ours
        # (`adopted`).  Anything beyond that is unaccounted for and gets said
        # out loud rather than silently sold as if it were this signal's.
        fill = p.get("fill_shares")
        if fill is not None:
            backed = fill + (p.get("adopted") or 0.0)
            if p["size"] > backed + DUST:
                log({"ev": "size_unbacked", "gid": self.gid, "ch": p["ch"],
                     "size": round(p["size"], 6),
                     "fill_shares": round(fill, 6),
                     "adopted": round(p.get("adopted") or 0.0, 6),
                     "position_id": p.get("position_id"),
                     "el_in": p.get("el_in")})
        # Size the sell from THIS position, never from the wallet balance:
        # the balance is shared with any other channel holding this token.
        qty = math.floor(p["size"] * 100) / 100
        if bal is not None:
            # Refuse -- never shrink to fit -- when the wallet no longer backs
            # our own position.  Shrinking to a shared balance is what let a
            # stale A record sell C's shares: the wallet held 4 (C's) and the
            # clamp happily posted a 4-share sell against them.  The only safe
            # reading of "the wallet holds less than I do plus my neighbours"
            # is that OUR shares are gone.
            others = sum(x["size"] for x in self.positions
                         if x["token"] == p["token"] and x is not p)
            if bal - others < qty - DUST:
                log({"ev": "sell_short_of_wallet", "gid": self.gid,
                     "ch": p["ch"], "own": round(qty, 6),
                     "bal": round(bal, 6), "others": round(others, 6),
                     "position_id": p.get("position_id")})
                return "rejected", {"err": "own shares not in the wallet",
                                    "terminal": True}
        if qty <= 0:
            return "rejected", {"err": f"qty {qty} bal {bal}"}
        _, bid, tick = book(self.client, p["token"])
        if bid is None:
            return "rejected", {"err": "no_bid"}
        tick = str(tick or "0.01")
        limit = max(_round_down_tick(bid, tick), _price_step(tick))
        prepared = {}

        def on_prepared(oid):
            prepared["oid"] = oid
            p["order_id"] = oid
            if LEDGER is not None:
                aid, ok = LEDGER.begin(self.gid, p["ch"], "CLOSE", oid,
                                       int(time.time()), el_in=p.get("el_in"),
                                       position_id=p.get("position_id", ""))
                if not ok:
                    raise DuplicateAttempt
                prepared["aid"] = aid
                p["close_attempt_id"] = aid

        try:
            res, oid = market_order(self.client, p["token"], "SELL", qty,
                                    limit, tick, on_prepared)
        except DuplicateAttempt:
            row = next((r for r in (LEDGER.rows(self.gid) if LEDGER else [])
                        if r["channel"] == p["ch"] and r["action"] == "CLOSE"),
                       None)
            if row:
                p["close_attempt_id"] = row["attempt_id"]
            return "posted", {"err": "duplicate CLOSE", "size": qty, "bid": bid,
                              "order_id": row["order_id"] if row else None}
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
        # sells run on their own thread now, so both lists move concurrently
        with LOCK:
            if p in self.positions:
                self.positions.remove(p)
            if p in ALL_POSITIONS:
                ALL_POSITIONS.remove(p)


# ------------------------------------------------------------------ resume
def resume_plan():
    """Fills already logged today: channel done marks + open positions.

    A buy is not necessarily still open: an earlier run may have sold it.
    Rebuild each position as bought - sold, so a restart does not resurrect
    a closed position (the old code only looked at buys).
    """
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
            elif ev in ("reconcile_closed", "no_exit", "settle_hold"):
                k = (r.get("gid"), r.get("ch"), r.get("outcome"))
                if k not in pos:
                    cand = [kk for kk in pos
                            if kk[0] == r.get("gid") and kk[1] == r.get("ch")]
                    if len(cand) == 1:
                        k = cand[0]
                if k in pos:
                    pos[k]["sold"] = pos[k]["size"]
    # Durable attempts catch a buy that was submitted before the process
    # died and never reached the event log.  The channel is marked done so
    # it cannot be entered again; the position is restored from the token
    # balance / order's trade record in the main loop.
    if LEDGER is not None:
        # A ledger OPEN row stays FILLED after the position is sold (the CLOSE
        # row is what closes), so the log is the authority: if this game and
        # channel already produced a buy event, the ledger row is the same
        # buy and must not be restored a second time.  That double count
        # resurrected a fully-sold A as 4 shares and added C twice (12 shares
        # restored against a wallet holding 4).
        seen_ev = {(k[0], k[1]) for k in pos}
        for row in LEDGER.open_rows():
            gk = (row["game_id"], row["channel"])
            if gk in seen_ev:
                continue
            k = (row["game_id"], row["channel"], None)
            if k in pos:
                continue
            pos[k] = {"entry": None, "size": 0.0, "sold": 0.0,
                      "t_in": row["signal_at"], "el_in": row["el_in"],
                      "order_id": row["order_id"],
                      "attempt_id": row["attempt_id"],
                      "position_id": row["position_id"]}
            done.add(gk)
    open_pos = {}
    for k, v in pos.items():
        rem = v["size"] - v["sold"]
        if rem > DUST or (v.get("attempt_id") and v.get("order_id")):
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
    LEDGER = AttemptLedger(db, funder)
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
    client = make_client()
    bal = balance(client)
    log({"ev": "start", "live": live, "balance": bal, "stake_frac": STAKE_FRAC,
         "stake_now": {k: round((bal or 0) * STAKE_FRAC * w, 2)
                       for k, w in (("A", 1.0), ("C", 1.0), ("D", 2.0))},
         "max_open": MAX_OPEN, "outdir": OUTDIR,
         "lock": {"on": LOCK_ENABLED, "frac": LOCK_FRAC, "margin": LOCK_MARGIN},
         "doglock": {"on": DOGLOCK_ENABLED, "frac": DOGLOCK_FRAC,
                     "margin": DOGLOCK_MARGIN, "min_el": DOGLOCK_MIN_EL,
                     "price": [DOGLOCK_PRICE_MIN, DOGLOCK_PRICE_MAX],
                     "stop_lead": DOGLOCK_STOP_LEAD}})
    if live and (bal or 0) < 2.0:
        log({"ev": "abort", "why": "balance too low", "balance": bal})
        return

    start_board()
    filled, _done = resume_plan()
    resumed = set()
    traders = {}
    saw_live = False
    tick = 0

    while True:
        tick += 1
        try:
            games = scoreboard()
        except Exception as e:
            log({"ev": "espn_err", "err": str(e)[:120]})
            time.sleep(8)
            continue
        now = time.time()
        live_games = [g for g in games if g["state"] == "in"]
        if live_games:
            saw_live = True

        # ---- spawn one trader per live game, then hand it the snapshot ----
        for g in live_games:
            t = traders.get(g["gid"])
            if t is None:
                res = resolve(g, client)
                if not res or not res.get("fav"):
                    continue
                t = GameTrader(g["gid"], res, g["home"], live)
                # Group by token: A and C can hold the same outcome.  The
                # recorded sizes split the token balance; handing the whole
                # balance to each channel would double-count it.
                by_tok = {}
                for (gid, ch, outcome), (entry, size, t_in, el_in,
                                          order_id, attempt_id, position_id) \
                        in list(filled.items()):
                    if gid != g["gid"]:
                        continue
                    t.done.add(ch)                 # that channel already filled
                    if (gid, ch) in resumed:
                        continue
                    if outcome is None:
                        # Attempt rows do not carry the outcome; derive it
                        # from the channel side and the resolved market.
                        outcome = res["fav"] if channel_spec(ch)["side"] == "fav" \
                            else next(o for o in res["outcomes"] if o != res["fav"])
                    if outcome not in res["outcomes"]:
                        continue
                    tok = res["tokens"][res["outcomes"].index(outcome)]
                    by_tok.setdefault(tok, []).append(
                        (ch, outcome, entry, size, t_in, el_in,
                         order_id, attempt_id, position_id))
                for tok, items in by_tok.items():
                    held = token_balance(client, tok)
                    if held is None:
                        held = sum(x[3] for x in items)
                    if held is None or held < DUST:
                        continue
                    ev_sizes = sum(x[3] for x in items if x[3] > 0)
                    total = ev_sizes
                    scale = held / total if total > 0 else 1.0
                    remaining = max(0.0, held - ev_sizes)
                    for ch, outcome, entry, size, t_in, el_in, order_id, \
                            attempt_id, position_id in items:
                        if size > 0:
                            # rebuilt from the event log (bought - sold),
                            # scaled so the set cannot exceed the wallet
                            alloc = size * scale
                        else:
                            # ledger-only attempt: the log never recorded it,
                            # so it may only take shares the wallet is not
                            # already accounting for.  Never at its full fill
                            # size -- those shares are usually the channels
                            # that ARE in the log, already counted above.
                            want = remaining
                            if attempt_id and LEDGER is not None:
                                led_shares, led_px = LEDGER.filled_of(attempt_id)
                                if led_shares and led_shares > DUST:
                                    want = min(led_shares, remaining)
                                    if (entry is None or entry <= 0) and led_px:
                                        entry = led_px
                            alloc = min(want, remaining)
                            remaining = max(0.0, remaining - alloc)
                        if alloc < DUST:
                            continue
                        # An attempt without an event has no entry price;
                        # recover it from the order's confirmed trade.
                        if (entry is None or entry <= 0) and order_id:
                            try:
                                from py_clob_client_v2.clob_types import TradeParams
                                for tr in client.get_trades(
                                        TradeParams(asset_id=tok)) or []:
                                    if str(tr.get("taker_order_id") or "") == str(order_id) \
                                            and (tr.get("status") or "").upper() == "CONFIRMED":
                                        entry = float(tr["price"])
                                        break
                            except Exception:
                                pass
                        pos = {"gid": g["gid"], "ch": ch, "token": tok,
                               "outcome": outcome, "entry": entry or 0.5,
                               "size": alloc, "t_in": t_in or time.time(),
                               "el_in": el_in,
                               "position_id": position_id or attempt_id,
                               "style": channel_spec(ch)["style"],
                               "tp": channel_spec(ch)["tp"], "selling": False,
                               "fill_shares": alloc, "adopted": 0.0}
                        t.positions.append(pos)
                        with LOCK:
                            ALL_POSITIONS.append(pos)
                        if LEDGER is not None and attempt_id:
                            LEDGER.fill(attempt_id, alloc, entry or 0.5)
                        resumed.add((g["gid"], ch))
                        log({"ev": "resume_position", "gid": g["gid"], "ch": ch,
                             "outcome": outcome, "entry": pos["entry"],
                             "size": alloc, "held": held, "recorded": size,
                             "el_in": el_in, "attempt_id": attempt_id,
                             "position_id": pos["position_id"]})
                t.start()
                traders[g["gid"]] = t
            t.put(g)

        # ---- release a trader once its game is final and it holds nothing ----
        for gid, t in list(traders.items()):
            g = next((x for x in games if x["gid"] == gid), None)
            if g is None or g["state"] != "post":
                continue
            t.put(g)                       # last snapshot: lets it force-exit
            time.sleep(0.3)
            if t.flat():
                t.log_reconcile()
                t.stop()
                t.join(timeout=45)
                traders.pop(gid, None)
                log({"ev": "trader_released", "gid": gid,
                     "remaining": len(traders)})

        if tick % 100 == 0:
            b = balance(client)
            with LOCK:
                snap = [(p["gid"], p["ch"], p["outcome"], p["entry"], p["size"])
                        for p in ALL_POSITIONS]
            log({"ev": "heartbeat", "balance": round(b, 2) if b else None,
                 "open": len(snap), "traders": len(traders),
                 "positions": snap, "confirmed": dict(CONFIRMED)})

        pending_games = [x for x in games if x["state"] in ("pre", "in")]
        if saw_live and games and not pending_games and not traders:
            log({"ev": "day_over", "games": len(games)})
            break
        time.sleep(POLL)

    for gid, t in list(traders.items()):
        t.stop()
        t.join(timeout=30)
    log({"ev": "stopped", "open": len(ALL_POSITIONS), "balance": balance(client),
         "traders": len(traders), "confirmed_buy": CONFIRMED["buy"],
         "confirmed_sell": CONFIRMED["sell"]})
    if not CONFIRMED["buy"]:
        log({"ev": "WARN", "msg": "no real fill observed this session"})
    elif not CONFIRMED["sell"]:
        log({"ev": "WARN", "msg": "a fill happened but no exit was confirmed"})


if __name__ == "__main__":
    main()
