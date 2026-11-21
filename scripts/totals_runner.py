# -*- coding: utf-8 -*-
"""Totals fade runner — the O/U overreaction experiment.

THE SIGNAL (discovered 2026-11-21 on 30 games; see reports/)
  second half (game clock >= 20:00), the Over price of a liquid full-game
  O/U line is in 0.20-0.60, and it has risen >= 15c in the last 5 minutes.
  Buy the UNDER and hold to settlement.  One entry per line; the dog-side
  A/C/D system is not used here.

WHY IT MAY EXIST
  Polymarket totals are thin and anchored to the pregame line.  A scoring
  run pulls retail money into the Over; the printed price and the top of
  book overshoot.  The fade bets the run does not change the final total
  as much as the price implies.

WHAT THIS RUNNER IS FOR
  The backtest used last-trade prints; this runner measures whether the
  edge survives the REAL book.  It logs, for every signal:
    over_mid / over_bid / over_ask / under_ask / under ask depth
  and the settlement.  The first live day is a measurement, not a profit
  target.  --dry-run logs paper trades only.

SAFETY
  - Max 3 concurrent positions, one per line.
  - 3% of equity per trade, floored at the 5-share minimum, capped by
    the 35% cash reserve.
  - No stops, no exits: hold to settlement (the Under pays 1 or 0).
  - A POST whose outcome is unknown is never re-posted.
"""
import argparse
import json
import os
import queue
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nba_data as nd
import live_runner as L          # the canonical client / book / order helpers


def _envf(name, default):
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return float(default)


STAKE_FRAC = _envf("TOTALS_STAKE_FRAC", 0.03)
MAX_CONCURRENT = int(_envf("TOTALS_MAX_CONCURRENT", 3))
CASH_RESERVE = _envf("TOTALS_CASH_RESERVE", 0.35)
MIN_SHARES = _envf("TOTALS_MIN_SHARES", 5)
RISE_WINDOW = _envf("TOTALS_RISE_WINDOW", 300)
RISE_CENTS = _envf("TOTALS_RISE_CENTS", 0.15)
PRICE_LO = _envf("TOTALS_PRICE_LO", 0.20)
PRICE_HI = _envf("TOTALS_PRICE_HI", 0.60)
EL_MIN = _envf("TOTALS_EL_MIN", 1200)
LINES_PER_GAME = int(_envf("TOTALS_LINES", 2))
POLL = _envf("TOTALS_POLL", 10)
MAIN_POLL = _envf("TOTALS_MAIN_POLL", 15)
HEARTBEAT = _envf("TOTALS_HEARTBEAT", 60)
BUY_SLIP = _envf("TOTALS_BUY_SLIP", 0.02)

LOCK = threading.Lock()
ALL_POSITIONS = []
RESERVED = 0
CLIENT = None
OUTDIR = None
DATE = None
LOG_LOCK = threading.Lock()
_tried = {}


def today_dir():
    return os.path.join(ROOT, "data", "totals_live",
                        datetime.now(timezone.utc).strftime("%Y-%m-%d"))


def log(rec):
    rec = dict(rec)
    rec.setdefault("t", round(time.time(), 3))
    line = json.dumps(rec, ensure_ascii=False, default=str)
    with LOG_LOCK:
        try:
            with open(os.path.join(OUTDIR, "events.jsonl"), "a") as f:
                f.write(line + "\n")
        except Exception:
            pass
        print(line, flush=True)


# ------------------------------------------------------------------ discovery
def scoreboard(date=None):
    """ESPN scoreboard; `date` is YYYYMMDD (US date).  None = ESPN's today."""
    url = ("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/"
           "scoreboard")
    if date:
        url += "?dates=" + date
    d = nd.http_json(url)
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


def _slug_dates(start):
    """The US game date, plus the day before (a UTC midnight game is the
    previous US date; trying both avoids an off-by-one)."""
    from datetime import timedelta
    try:
        dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        dt = datetime.now(timezone.utc).replace(tzinfo=None)
    for off in (0, -1):
        yield (dt + timedelta(days=off)).strftime("%Y-%m-%d")


def resolve_totals(g):
    """Find the full-game O/U ladder for this ESPN game."""
    a = nd.pm_abbr(g["away"])
    h = nd.pm_abbr(g["home"])
    for d in _slug_dates(g.get("start")):
        for slug in (f"nba-{a}-{h}-{d}", f"nba-{h}-{a}-{d}"):
            try:
                ev = nd.http_json(
                    "https://gamma-api.polymarket.com/events?slug=" + slug)
            except Exception:
                continue
            if not ev:
                continue
            ev = ev[0]
            lines = []
            for m in ev.get("markets") or []:
                q = m.get("question") or ""
                if ": O/U " not in q:
                    continue
                if any(x in q for x in ("1Q ", "2Q ", "3Q ", "4Q ",
                                        "1H ", "2H ", "Team Total")):
                    continue
                try:
                    line = float(q.split(": O/U ")[1])
                except Exception:
                    continue
                try:
                    outs = json.loads(m.get("outcomes") or "[]")
                    toks = json.loads(m.get("clobTokenIds") or "[]")
                except Exception:
                    continue
                if len(outs) != 2 or len(toks) != 2:
                    continue
                if (outs[0] or "").lower() != "over":
                    outs, toks = outs[::-1], toks[::-1]
                try:
                    price = float(json.loads(m.get("outcomePrices") or "[]")[0])
                except Exception:
                    price = 0.5
                lines.append({"line": line, "over": toks[0], "under": toks[1],
                              "vol": float(m.get("volume") or 0),
                              "price": price})
            if not lines:
                continue
            # the liquid core: top volume, then the lines closest to 50/50.
            # Those are the ones that will matter once the game is running.
            top = sorted(lines, key=lambda x: -x["vol"])[:8]
            top = sorted(top, key=lambda x: abs(x["price"] - 0.5))[:LINES_PER_GAME]
            return {"slug": slug, "title": ev.get("title"), "lines": top}
    return None


# ------------------------------------------------------------------ trader
class TotalsTrader(threading.Thread):
    def __init__(self, g, res, live):
        super().__init__(daemon=True, name=f"totals-{g['gid']}")
        self.game = dict(g)
        self.res = res
        self.live = live
        self.client = L.make_client()
        self.q = queue.Queue(maxsize=4)
        self.lines = {}
        for x in res["lines"]:
            self.lines[x["line"]] = dict(x, hist=deque(), done=False,
                                         pending=False, pos=None)
        self.positions = []
        self.finished = False
        self._stop = False

    def put(self, g):
        try:
            self.q.put_nowait(g)
        except queue.Full:
            pass

    def stop(self):
        self._stop = True

    def run(self):
        while not self._stop:
            try:
                g = self.q.get(timeout=30)
            except queue.Empty:
                continue
            try:
                self.tick(g)
            except Exception as e:
                log({"ev": "trader_err", "gid": self.game["gid"],
                     "err": repr(e)[:200]})

    # ---- per tick ----
    def tick(self, g):
        el = nd.elapsed_of(g.get("period"), g.get("detail"))
        total = int(g.get("home_score") or 0) + int(g.get("away_score") or 0)
        if g.get("state") == "post":
            self.settle(total)
            self.finished = True
            return
        if el is None or el < EL_MIN:
            return
        now = time.time()
        for line, x in self.lines.items():
            if x["done"] or x["pending"]:
                continue
            oa, ob, _ = L.book(self.client, x["over"])
            ua, ub, _ = L.book(self.client, x["under"])
            if oa is None or ob is None or not (0 < oa < 1 and 0 < ob < 1):
                continue
            mid = (oa + ob) / 2
            x["hist"].append((now, mid))
            while x["hist"] and now - x["hist"][0][0] > RISE_WINDOW + 120:
                x["hist"].popleft()
            old = None
            for t, m in reversed(x["hist"]):
                if t <= now - RISE_WINDOW:
                    old = m
                    break
            if old is None:
                continue
            if not (PRICE_LO <= mid <= PRICE_HI):
                continue
            if mid - old < RISE_CENTS:
                continue
            if ua is None:
                log({"ev": "signal_skip_no_ask", "gid": self.game["gid"],
                     "line": line, "over_mid": round(mid, 4)})
                continue
            self.enter(x, mid, old, ua, el, total, g,
                       over_bid=ob, over_ask=oa)

    # ---- entry ----
    def enter(self, x, mid, old, under_ask, el, total, g,
              over_bid=None, over_ask=None):
        global RESERVED
        # Reserve a concurrency slot WITHOUT holding the lock across the
        # order round-trip: one slow POST must not freeze another game.
        with LOCK:
            if len(ALL_POSITIONS) + RESERVED >= MAX_CONCURRENT:
                log({"ev": "signal_skip_cap", "gid": self.game["gid"],
                     "line": x["line"],
                     "open": len(ALL_POSITIONS) + RESERVED})
                return
            RESERVED += 1
        pos = None
        paper = False
        try:
            bal = L.balance(self.client)
            if bal is None:
                return
            with LOCK:
                deployed = sum(p["shares"] * p["entry"]
                               for p in ALL_POSITIONS)
            equity = bal + deployed
            usd = min(equity * STAKE_FRAC, bal * (1 - CASH_RESERVE))
            usd = max(usd, MIN_SHARES * under_ask)      # exchange 5-share floor
            if usd < 1 or usd > bal * (1 - CASH_RESERVE):
                log({"ev": "signal_skip_cash", "gid": self.game["gid"],
                     "line": x["line"], "usd": round(usd, 2),
                     "bal": round(bal, 2)})
                return
            x["pending"] = True
            depth = L.book_depth(self.client, x["under"]) or {}
            log({"ev": "signal", "gid": self.game["gid"],
                 "slug": self.res.get("slug"), "line": x["line"],
                 "over_mid": round(mid, 4), "over_5m_ago": round(old, 4),
                 "rise": round(mid - old, 4),
                 "over_bid": over_bid, "over_ask": over_ask,
                 "under_ask": under_ask, "usd": round(usd, 2),
                 "shares_min": round(usd / under_ask, 2) if under_ask else None,
                 "el": el, "total": total, "period": g.get("period"),
                 "under_ask_notional": depth.get("ask_notional"),
                 "under_asks": depth.get("asks"), "live": self.live})
            if not self.live:
                pos = self._pos(x, under_ask,
                                usd / under_ask if under_ask else 0,
                                under_ask)
                paper = True
                x["done"] = True
                return
            base = L.token_balance(self.client, x["under"])
            t0 = int(time.time())
            oid = None
            try:
                res, signed = L.market_order(
                    self.client, x["under"], "BUY", usd,
                    min(under_ask + BUY_SLIP, 0.99), "0.01")
                if isinstance(res, dict):
                    oid = res.get("orderID") or res.get("order_id")
                log({"ev": "buy_posted", "gid": self.game["gid"],
                     "line": x["line"], "under_ask": under_ask,
                     "usd": round(usd, 2), "order_id": oid or signed,
                     "limit": round(min(under_ask + BUY_SLIP, 0.99), 3)})
            except Exception as e:
                # A POST whose outcome is unknown must never be re-sent.
                log({"ev": "buy_error", "gid": self.game["gid"],
                     "line": x["line"], "err": repr(e)[:200]})
            shares = price = None
            for _ in range(30):
                fills = self._fills(x["under"], oid, t0)
                if fills:
                    shares = sum(float(t["size"]) for t in fills)
                    notional = sum(float(t["size"]) * float(t["price"])
                                   for t in fills)
                    price = notional / shares if shares else under_ask
                    break
                cur = L.token_balance(self.client, x["under"])
                if base is not None and cur is not None and cur > base + 0.4:
                    shares = cur - base
                    price = under_ask
                    break
                time.sleep(2)
            x["done"] = True
            if not shares:
                log({"ev": "buy_gave_up", "gid": self.game["gid"],
                     "line": x["line"], "order_id": oid})
                return
            pos = self._pos(x, price, shares, under_ask)
        finally:
            x["pending"] = False
            with LOCK:
                RESERVED = max(0, RESERVED - 1)
                if pos is not None:
                    ALL_POSITIONS.append(pos)
            if pos is not None:
                self.positions.append(pos)
                log({"ev": "paper_buy" if paper else "buy", **pos,
                     "usdc": round(pos["shares"] * pos["entry"], 2)})

    def _fills(self, token, oid, t0):
        try:
            from py_clob_client_v2.clob_types import TradeParams
            trades = self.client.get_trades(TradeParams(asset_id=token)) or []
        except Exception:
            return []
        out = []
        for t in trades:
            if (t.get("status") or "").upper() != "CONFIRMED":
                continue
            if (t.get("side") or "").upper() != "BUY":
                continue
            if oid and str(t.get("taker_order_id") or "") != str(oid):
                continue
            try:
                mt = int(t.get("match_time") or 0)
            except Exception:
                mt = 0
            if mt and mt < t0 - 2:
                continue
            out.append(t)
        return out

    def _pos(self, x, entry, shares, under_ask):
        return {"gid": self.game["gid"], "line": x["line"],
                "token": x["under"], "entry": entry, "shares": shares,
                "under_ask_at_signal": under_ask,
                "t_in": time.time(), "side": "UNDER", "settled": False}

    # ---- settlement ----
    def settle(self, total):
        for p in self.positions:
            if p.get("settled"):
                continue
            p["settled"] = True
            won = total <= p["line"]
            payout = 1.0 if won else 0.0
            pnl = (payout - p["entry"]) * p["shares"]
            tok_bal = L.token_balance(self.client, p["token"])
            log({"ev": "settle", **p, "final_total": total, "won": won,
                 "payout": payout, "pnl": round(pnl, 4),
                 "token_balance": tok_bal,
                 "usdc": round((L.balance(self.client) or 0), 2),
                 "redeem_needed": tok_bal is not None and tok_bal > 0.5})
            with LOCK:
                try:
                    ALL_POSITIONS.remove(p)
                except ValueError:
                    pass
        self.positions = []


# ------------------------------------------------------------------ main
def _prune_tried():
    now = time.time()
    for k in [k for k, v in _tried.items() if now - v > 3600]:
        _tried.pop(k, None)


def run(live):
    global CLIENT, OUTDIR
    OUTDIR = today_dir()
    os.makedirs(OUTDIR, exist_ok=True)
    CLIENT = L.make_client()
    bal = L.balance(CLIENT)
    log({"ev": "start", "live": live, "balance": bal,
         "stake_frac": STAKE_FRAC, "max_open": MAX_CONCURRENT,
         "cash_reserve": CASH_RESERVE, "rise_window": RISE_WINDOW,
         "rise_cents": RISE_CENTS, "price": [PRICE_LO, PRICE_HI],
         "el_min": EL_MIN, "lines_per_game": LINES_PER_GAME,
         "outdir": OUTDIR})
    traders = {}
    last_hb = 0.0
    while True:
        try:
            games = scoreboard(DATE)
        except Exception as e:
            log({"ev": "scoreboard_err", "err": repr(e)[:160]})
            time.sleep(MAIN_POLL)
            continue
        for g in games:
            gid = g["gid"]
            if gid in traders:
                traders[gid].put(g)
                continue
            if g.get("state") not in ("pre", "in"):
                continue
            now = time.time()
            if now - _tried.get(gid, 0) < 300:
                continue
            _tried[gid] = now
            res = resolve_totals(g)
            if not res:
                log({"ev": "no_totals_market", "gid": gid,
                     "away": g.get("away"), "home": g.get("home")})
                continue
            tr = TotalsTrader(g, res, live)
            traders[gid] = tr
            tr.start()
            log({"ev": "trader_start", "gid": gid, "slug": res.get("slug"),
                 "title": res.get("title"),
                 "lines": [x["line"] for x in res["lines"]]})
        for gid, tr in list(traders.items()):
            if tr.finished and not tr.positions:
                tr.stop()
                traders.pop(gid, None)
                log({"ev": "trader_done", "gid": gid})
        now = time.time()
        if now - last_hb >= HEARTBEAT:
            last_hb = now
            with LOCK:
                n_open = len(ALL_POSITIONS)
                expo = round(sum(p["shares"] * p["entry"]
                                 for p in ALL_POSITIONS), 2)
            log({"ev": "heartbeat", "balance": round(L.balance(CLIENT) or 0, 2),
                 "open": n_open, "exposure": expo, "traders": len(traders),
                 "games": len(games)})
        if not games and not traders:
            time.sleep(30)
            continue
        if (games and all(g.get("state") == "post" for g in games)
                and not traders):
            log({"ev": "day_over"})
            return
        time.sleep(MAIN_POLL)


def selftest():
    """Discovery + book smoke test, no orders, no trader loop."""
    client = L.make_client()
    games = scoreboard(DATE)
    print("games", len(games), "balance", L.balance(client))
    for g in games:
        if g.get("state") not in ("pre", "in"):
            continue
        res = resolve_totals(g)
        print(g["gid"], g.get("away"), "@", g.get("home"), g.get("state"),
              "->", res and res.get("slug"))
        if not res:
            continue
        for x in res["lines"]:
            oa, ob, tick = L.book(client, x["over"])
            ua, ub, _ = L.book(client, x["under"])
            print(f"  line {x['line']:6.1f} over bid/ask {ob}/{oa} "
                  f"under bid/ask {ub}/{ua} tick {tick}")
            print("   under depth", L.book_depth(client, x["under"]))
        return
    print("no pre/in game with a totals market")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--date", default=None, help="ESPN date YYYYMMDD (default: today)")
    ap.add_argument("--duration", type=float, default=0,
                    help="stop after N seconds (smoke test)")
    a = ap.parse_args()
    global DATE
    DATE = a.date
    if a.selftest:
        selftest()
        return
    if a.duration:
        threading.Timer(a.duration, lambda: os._exit(0)).start()
    # live only when explicitly asked; dry-run is the safe default
    run(live=bool(a.live and not a.dry_run))


if __name__ == "__main__":
    main()
