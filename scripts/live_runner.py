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

POLL = 3

STAKE_FRAC = float(os.environ.get("STAKE_FRAC", "0.05"))
MIN_ORDER = 1.0
MAX_OPEN = 5
CASH_RESERVE = 0.20

BUY_WINDOW = 15          # the price is flat for ~15s, then drifts 2-3c/min
SELL_WINDOW = 900
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
                      retry_on_error=True)


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


def market_order(client, token, side, amount, price_limit, tick):
    """amount = USD for BUY, shares for SELL; price_limit caps slippage.

    Market BUY orders are capped by the server at 2dp for the maker (USDC)
    amount and 4dp for the taker (shares) amount.  The 0.001 tick's rounding
    config asks for 5dp, which the server rejects with "invalid amounts".
    Buys therefore pass tick 0.01 explicitly; sells keep the book tick so a
    bid of 0.015 can still be hit.
    """
    from py_clob_client_v2.clob_types import (MarketOrderArgsV2, OrderType,
                                              PartialCreateOrderOptions)
    d = 3 if float(tick or "0.01") < 0.01 else 2
    args = MarketOrderArgsV2(token_id=token, amount=round(amount, 2), side=side,
                             price=round(price_limit, d), order_type=OrderType.FOK)
    return client.create_and_post_market_order(
        args, PartialCreateOrderOptions(tick_size=str(tick)), OrderType.FOK)


def _order_id(res):
    if not isinstance(res, dict):
        return None
    for k in ("orderID", "orderId", "order_id", "id"):
        if res.get(k):
            return str(res[k])
    return None


def _post_status(res):
    """Classify a CLOB order response.

    posted   -> the exchange accepted/matched it; wait for the trade record
    rejected -> it definitely did not fill; a retry cannot double the position
    unknown  -> it may have reached the book; never repost while unknown
    """
    if not isinstance(res, dict):
        return "unknown", None
    if res.get("tradeIDs") or res.get("transactionsHashes"):
        return "posted", None
    if res.get("success") is True:
        return "posted", None
    state = str(res.get("status") or "").lower()
    if state in ("matched", "filled", "delayed"):
        return "posted", None
    if res.get("success") is False or state in (
            "canceled", "cancelled", "unmatched", "expired", "failed"):
        return "rejected", str(res.get("errorMsg") or res.get("error") or res)[:120]
    return "unknown", None


def _price_step(tick):
    return 10 ** -(3 if float(tick or "0.01") < 0.01 else 2)


def _round_up_tick(x, tick):
    step = _price_step(tick)
    return round(math.ceil((x - 1e-9) / step) * step, 3 if step < 0.01 else 2)


def _round_down_tick(x, tick):
    step = _price_step(tick)
    return round(math.floor((x + 1e-9) / step) * step, 3 if step < 0.01 else 2)


# ------------------------------------------------------------------ market
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
        self.seen_ids = set()      # trade IDs already attributed to a position

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
        return not self.positions and not self.inflight

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
        for key in ("A", "C", "D"):
            if key in self.done or key in self.pending:
                continue
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
                log({"ev": "signal_skip_band", "gid": self.gid, "ch": key,
                     "ask": ask, "el": sig["el"], "m": sig["margin"]})
                continue
            with LOCK:
                n_open = len(ALL_POSITIONS)
            if n_open >= MAX_OPEN:
                log({"ev": "signal_skip_cap", "gid": self.gid, "ch": key})
                continue
            bal = balance(self.client)
            if bal is None:
                continue
            usd = min(bal * STAKE_FRAC * ch["weight"], bal * (1 - CASH_RESERVE))
            if usd < MIN_ORDER:
                log({"ev": "signal_skip_cash", "gid": self.gid, "ch": key,
                     "usd": round(usd, 2), "balance": round(bal, 2)})
                continue
            self.pending.add(key)
            self.inflight[key] = {"token": tok, "outcome": side_out, "usd": usd,
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
        # silently dropping the fill.  Two candidates means A and C are both
        # in flight; guessing there is worse than waiting.
        return loose if (order_id and len(loose) == 1) else []

    def _open_position(self, q, key, shares, entry, nfills):
        self.inflight.pop(key, None)
        self.pending.discard(key)
        self.done.add(key)
        pos = {"gid": self.gid, "ch": key, "token": q["token"],
               "outcome": q["outcome"], "entry": entry, "size": shares,
               "t_in": time.time(), "el_in": q.get("el_sig"),
               "style": q["style"], "tp": q["tp"], "selling": False}
        self.positions.append(pos)
        with LOCK:
            ALL_POSITIONS.append(pos)
            first = not CONFIRMED["buy"]
            CONFIRMED["buy"] = True
        log({"ev": "buy", "gid": self.gid, "ch": key, "outcome": q["outcome"],
             "filled_shares": shares, "entry": entry,
             "usd": round(shares * entry, 2), "tries": q["tries"],
             "order_id": q.get("order_id"), "fills": nfills,
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
                same = [p for p in self.positions if p["token"] == q["token"]]
                same += [x for k2, x in self.inflight.items()
                         if k2 != key and x["token"] == q["token"]]
                bal = token_balance(self.client, q["token"]) if not same else None
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
                log({"ev": "buy_gave_up", "gid": self.gid, "ch": key,
                     "tries": q["tries"], "posted": q.get("posted"),
                     "order_id": q.get("order_id"),
                     "reason": q.get("last_err")})
                continue

            # 3) never post a second order for the same signal.  The balance
            #    and trade APIs can lag; a retry in that window doubles the
            #    position (Thunder D, 2026-11-14).
            if q.get("posted"):
                continue

            with LOCK:
                if len(ALL_POSITIONS) >= MAX_OPEN:
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
        try:
            res = market_order(self.client, q["token"], "BUY",
                               q["usd"], limit, btick)
        except Exception as e:
            status = getattr(e, "status_code", None)
            if status and 400 <= int(status) < 500:
                return "rejected", {"err": str(e)[:120], "status": status}
            return "unknown", {"err": str(e)[:120]}
        status, err = _post_status(res)
        if status == "rejected":
            return "rejected", {"err": err, "res": res}
        return status, {"err": err, "ask": ask, "limit": limit,
                        "order_id": _order_id(res), "res": res}

    # ---- exit ----
    def _manage_exits(self, g, now):
        for p in list(self.positions):
            if p["style"] != "process":
                if p.get("selling"):
                    continue
                # D thesis: favourite leading 5-10.  If the lead turns
                # negative, the thesis is broken: exit at the market rather
                # than riding a -100% settlement.  This is a margin stop,
                # not a price stop.
                stop = S.CHANNELS.get(p["ch"], {}).get("stop_margin")
                if stop is not None and self.hist:
                    m_now = self.hist[-1][2]
                    if m_now < stop:
                        p["selling"] = True
                        p["exit_reason"] = "margin_stop"
                        _, bid, _ = book(self.client, p["token"])
                        log({"ev": "exit", "gid": self.gid, "ch": p["ch"],
                             "outcome": p["outcome"], "reason": "margin_stop",
                             "bid": bid, "entry": p["entry"],
                             "size": p["size"], "m_fav": m_now})
                        self._sell(p)
                        continue
                if g["state"] == "post":
                    log({"ev": "settle_hold", "gid": self.gid, "ch": p["ch"],
                         "outcome": p["outcome"], "entry": p["entry"],
                         "size": p["size"], "note": "held to resolution"})
                    self._drop(p)
                continue
            if p.get("selling"):
                continue
            if p["size"] < DUST:
                log({"ev": "reconcile_closed", "gid": self.gid, "ch": p["ch"],
                     "outcome": p["outcome"], "why": "dust", "size": p["size"]})
                self._drop(p)
                continue
            el = nd.elapsed_of(g["period"], g["detail"])
            finished = (g["state"] == "post") or (el is not None and el >= 2880)
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
            _, bid, _ = book(self.client, p["token"])
            if bid is None and finished:
                log({"ev": "no_exit", "gid": self.gid, "ch": p["ch"],
                     "outcome": p["outcome"], "size": p["size"],
                     "note": "final, no bid"})
                self._drop(p)
                continue
            if bid is None and not finished:
                continue
            reason = None
            game_hold = S.CHANNELS[p["ch"]].get("game_hold")
            if bid is not None and (bid - S.exit_cost(bid)) >= p["entry"] + p["tp"]:
                reason = "target"
            elif (game_hold and p.get("el_in") is not None and el is not None
                  and el - p["el_in"] >= game_hold):
                reason = "time"
            elif finished or (el is not None and el >= S.FORCE_EL):
                reason = "force"
            if reason is None:
                continue
            p["selling"] = True
            p["exit_reason"] = reason
            log({"ev": "exit", "gid": self.gid, "ch": p["ch"], "reason": reason,
                 "bid": bid, "entry": p["entry"], "size": p["size"]})
            self._sell(p)

    def _sell(self, p):
        """Sell THIS position's shares only.

        The token balance may be shared by A and C; selling floor(balance)
        would liquidate the other channel too.  We post the position's own
        size, and confirm from the trade record.  A full sell leaves < 0.01
        share of dust (size precision is 2dp), which is treated as closed
        instead of retrying for 15 minutes.
        """
        deadline = time.time() + SELL_WINDOW
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
                     "remaining": round(p["size"], 6), "fills": len(fills)})
                if first:
                    log({"ev": "CONFIRMED_SELL", "gid": self.gid, "ch": p["ch"],
                         "sold_shares": sold, "exit": px})
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
                if posted_unknown and time.time() - posted_at > 30:
                    same = [x for x in self.positions
                            if x["token"] == p["token"] and x is not p]
                    bal = token_balance(self.client, p["token"]) if not same else None
                    if bal is not None and bal >= p["size"] - DUST:
                        posted = False
                        posted_unknown = False
                        order_id = None
                        last = "no fill after 30s"
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
            time.sleep(RETRY_GAP)
        p["selling"] = False
        if p["size"] < DUST:
            self._drop(p)
            return
        log({"ev": "sell_gave_up", "gid": self.gid, "ch": p["ch"],
             "outcome": p["outcome"], "reason": p.get("exit_reason"),
             "remaining": round(p["size"], 6), "last": last})

    def _post_sell(self, p):
        bal = token_balance(self.client, p["token"])
        qty = math.floor(p["size"] * 100) / 100
        if bal is not None and bal < qty:
            qty = math.floor(bal * 100) / 100
        if qty <= 0:
            return "rejected", {"err": f"qty {qty} bal {bal}"}
        _, bid, tick = book(self.client, p["token"])
        if bid is None:
            return "rejected", {"err": "no_bid"}
        tick = str(tick or "0.01")
        limit = max(_round_down_tick(bid, tick), _price_step(tick))
        try:
            res = market_order(self.client, p["token"], "SELL", qty, limit, tick)
        except Exception as e:
            status = getattr(e, "status_code", None)
            if status and 400 <= int(status) < 500:
                return "rejected", {"err": str(e)[:120], "status": status}
            return "unknown", {"err": str(e)[:120], "size": qty, "bid": bid}
        status, err = _post_status(res)
        if status == "rejected":
            return "rejected", {"err": err, "res": res}
        return status, {"err": err, "size": qty, "bid": bid,
                        "order_id": _order_id(res), "res": res}

    def _drop(self, p):
        if p in self.positions:
            self.positions.remove(p)
        with LOCK:
            if p in ALL_POSITIONS:
                ALL_POSITIONS.remove(p)


# ------------------------------------------------------------------ resume
def resume_plan():
    """Fills already logged today: channel done marks + open positions.

    A buy is not necessarily still open: an earlier run may have sold it.
    Rebuild each position as bought - sold, so a restart does not resurrect
    a closed position (the old code only looked at buys).
    """
    base = os.path.join(ROOT, "data", "live_trade_final", TODAY)
    pos, done = {}, set()
    if not os.path.isdir(base):
        return pos, done
    for run in sorted(os.listdir(base)):
        fp = os.path.join(base, run, "events.jsonl")
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
    open_pos = {}
    for k, v in pos.items():
        rem = v["size"] - v["sold"]
        if rem > DUST:
            open_pos[k] = (v["entry"], rem, v.get("t_in"), v.get("el_in"))
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
            return run(live=True)
    return run(live=False)


def run(live):
    client = make_client()
    bal = balance(client)
    log({"ev": "start", "live": live, "balance": bal, "stake_frac": STAKE_FRAC,
         "stake_now": {k: round((bal or 0) * STAKE_FRAC * w, 2)
                       for k, w in (("A", 1.0), ("C", 1.0), ("D", 2.0))},
         "max_open": MAX_OPEN, "outdir": OUTDIR})
    if live and (bal or 0) < 2.0:
        log({"ev": "abort", "why": "balance too low", "balance": bal})
        return

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
                for (gid, ch, outcome), (entry, size, t_in, el_in) in list(filled.items()):
                    if gid != g["gid"]:
                        continue
                    t.done.add(ch)                 # that channel already filled
                    if (gid, ch) in resumed or outcome not in res["outcomes"]:
                        continue
                    tok = res["tokens"][res["outcomes"].index(outcome)]
                    by_tok.setdefault(tok, []).append(
                        (ch, outcome, entry, size, t_in, el_in))
                for tok, items in by_tok.items():
                    held = token_balance(client, tok)
                    if held is None:
                        held = sum(x[3] for x in items)
                    if held is None or held < DUST:
                        continue
                    total = sum(x[3] for x in items)
                    if total <= 0:
                        continue
                    scale = held / total
                    for ch, outcome, entry, size, t_in, el_in in items:
                        alloc = size * scale
                        if alloc < DUST:
                            continue
                        pos = {"gid": g["gid"], "ch": ch, "token": tok,
                               "outcome": outcome, "entry": entry or 0.5,
                               "size": alloc, "t_in": t_in or time.time(),
                               "el_in": el_in,
                               "style": S.CHANNELS[ch]["style"],
                               "tp": S.CHANNELS[ch]["tp"], "selling": False}
                        t.positions.append(pos)
                        with LOCK:
                            ALL_POSITIONS.append(pos)
                        resumed.add((g["gid"], ch))
                        log({"ev": "resume_position", "gid": g["gid"], "ch": ch,
                             "outcome": outcome, "entry": pos["entry"],
                             "size": alloc, "held": held, "recorded": size,
                             "el_in": el_in})
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
