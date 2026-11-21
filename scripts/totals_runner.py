# -*- coding: utf-8 -*-
"""Q3-break totals runner — the "default line is too high" experiment.

THE SIGNAL (found 2026-11-21; validated on 166 OOS games 2026-10-20..11-11)
  At the end of Q3 (game clock 2040-2280s), for every full-game O/U line:
    P(Over) = historical P(Q4 remaining points > line - current_total)
  Take the line with the LOWEST P(Over).  If
      P(Over) <= P_MAX  and  market Over mid >= P(Over) + GAP
  buy the UNDER and hold to settlement.  One entry per game.
  Symmetrically the Over side exists, but in the sample it is weaker
  (87% win at 87% price); this runner only takes the Under side.

WHY IT MAY EXIST
  At the Q3 break the market's deep-OTM lines are anchored to the pregame
  total and are not fully repriced.  Retail buys the cheap Over lottery
  ticket; the Under of the safest line is left at 0.75-0.87 while the
  historical Q4 distribution says 0.90-0.99.  The edge is the stale branch,
  not a prediction of the final total.

MEASURED SO FAR (prints, not executable asks)
  166 OOS games: 36 entries, 91.7% Under win, avg price 83.1%, +10.5%/trade
  50 combined:  94.0% win, +13.8%/trade, bootstrap [+4.5%, +21.3%]
  3 losses were high-scoring Q4s (86-95 points).

WHAT THIS RUNNER ADDS
  It scans ALL full-game lines (not the pregame top-6), reads the real
  Under ask + top-3 depth at the break, and logs exactly what was available.
  The first live sessions are a measurement of the print->ask gap.

SAFETY
  Max 2 concurrent positions, one per game.
  Stake = 4% of equity, floored at the exchange 5-share minimum, capped by
  a 35% cash reserve.  No stop: hold to settlement.
  A POST whose outcome is unknown is never re-sent.
"""
import argparse
import bisect
import json
import os
import queue
import sys
import threading
import time
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nba_data as nd
import live_runner as L          # canonical client / book / order helpers


def _envf(name, default):
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return float(default)


STAKE_FRAC = _envf("TOTALS_STAKE_FRAC", 0.04)
MAX_CONCURRENT = int(_envf("TOTALS_MAX_CONCURRENT", 2))
CASH_RESERVE = _envf("TOTALS_CASH_RESERVE", 0.35)
MIN_SHARES = _envf("TOTALS_MIN_SHARES", 5)
P_MAX = _envf("TOTALS_P_MAX", 0.10)
GAP = _envf("TOTALS_GAP", 0.03)
EL_LO = _envf("TOTALS_EL_LO", 2040)
EL_HI = _envf("TOTALS_EL_HI", 2280)
UNDER_MAX = _envf("TOTALS_UNDER_MAX", 0.92)
TOP_CANDIDATES = int(_envf("TOTALS_TOP_CANDIDATES", 8))
# Lines taken per game.  Adjacent lines are nearly the same bet, so
# this is a *concentration* switch, not a diversification one.
LINES_PER_GAME = int(_envf("TOTALS_LINES_PER_GAME", 1))
BUY_SLIP = _envf("TOTALS_BUY_SLIP", 0.02)
MAIN_POLL = _envf("TOTALS_MAIN_POLL", 12)
HEARTBEAT = _envf("TOTALS_HEARTBEAT", 60)
MODEL_PATH = os.environ.get("TOTALS_MODEL",
                            os.path.join(ROOT, "data", "totals_model.json"))

LOCK = threading.Lock()
ALL_POSITIONS = []
RESERVED = 0
CLIENT = None
OUTDIR = None
DATE = None
MODEL = None
LOG_LOCK = threading.Lock()
_tried = {}


def us_date():
    """The US game date (ET), which is what the ESPN ?dates= query wants."""
    return (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y%m%d")


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


# ------------------------------------------------------------------ model
def load_model():
    with open(MODEL_PATH) as f:
        m = json.load(f)
    return {"bin": m["bin"], "cells": m["cells"],
            "cuts": {int(k): v for k, v in m["cuts"].items()}}


def pmodel(el, S, needed):
    """P(Q4 remaining > needed) from the 1555-game empirical table."""
    bi = min(23, max(0, int(el // MODEL["bin"])))
    lo, hi = MODEL["cuts"][bi]
    k = 0 if S < lo else (1 if S < hi else 2)
    v = MODEL["cells"].get(f"{bi},{k}")
    if not v:
        return None
    return 1.0 - bisect.bisect_right(v, needed) / len(v)


# ------------------------------------------------------------------ discovery
def scoreboard(date=None):
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
    from datetime import timedelta
    try:
        dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        dt = datetime.now(timezone.utc).replace(tzinfo=None)
    for off in (0, -1):
        yield (dt + timedelta(days=off)).strftime("%Y-%m-%d")


def resolve_totals(g):
    """Every full-game O/U line, with both token ids."""
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
            byl = {}
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
                vol = float(m.get("volume") or 0)
                cur = byl.get(line)
                if cur is None or vol > cur["vol"]:
                    byl[line] = {"line": line, "over": toks[0],
                                 "under": toks[1], "vol": vol, "price": price}
            if not byl:
                continue
            return {"slug": slug, "title": ev.get("title"),
                    "lines": sorted(byl.values(), key=lambda x: x["line"])}
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
        self.lines = {x["line"]: dict(x) for x in res["lines"]}
        self.break_done = False
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
        if self.break_done:
            return
        if el is None or el < EL_LO or el > EL_HI:
            return
        # One shot per game, at the Q3 break.
        self.break_done = True
        cands = []
        for line, x in self.lines.items():
            pm = pmodel(el, total, line - total)
            if pm is None or pm > P_MAX:
                continue
            cands.append((pm, x))
        if not cands:
            log({"ev": "break_no_candidate", "gid": self.game["gid"],
                 "el": el, "total": total, "lines": len(self.lines)})
            return
        cands.sort(key=lambda t: t[0])
        log({"ev": "break_scan", "gid": self.game["gid"], "el": el,
             "total": total, "candidates": [(round(p, 4), x["line"])
                                            for p, x in cands[:TOP_CANDIDATES]]})
        taken = 0
        for pm, x in cands[:TOP_CANDIDATES]:
            if taken >= LINES_PER_GAME:
                break
            oa, ob, _ = L.book(self.client, x["over"])
            ua, ub, _ = L.book(self.client, x["under"])
            if oa is None or ob is None or not (0 < oa < 1 and 0 < ob < 1):
                continue
            mid = (oa + ob) / 2
            if mid < pm + GAP:
                log({"ev": "skip_gap", "gid": self.game["gid"],
                     "line": x["line"], "pm": round(pm, 4),
                     "over_mid": round(mid, 4)})
                continue
            if ua is None or ua > UNDER_MAX:
                log({"ev": "skip_under_price", "gid": self.game["gid"],
                     "line": x["line"], "under_ask": ua})
                continue
            depth = L.book_depth(self.client, x["under"]) or {}
            asks = depth.get("asks") or []
            avail = sum(s for _p, s in asks[:3])
            if avail < MIN_SHARES:
                log({"ev": "skip_depth", "gid": self.game["gid"],
                     "line": x["line"], "avail": avail})
                continue
            if self.enter(x, pm, mid, oa, ob, ua, el, total, depth):
                taken += 1

    # ---- entry ----
    def enter(self, x, pm, mid, oa, ob, under_ask, el, total, depth):
        global RESERVED
        with LOCK:
            if len(ALL_POSITIONS) + RESERVED >= MAX_CONCURRENT:
                log({"ev": "skip_cap", "gid": self.game["gid"],
                     "line": x["line"],
                     "open": len(ALL_POSITIONS) + RESERVED})
                return False
            RESERVED += 1
        pos = None
        paper = False
        try:
            bal = L.balance(self.client)
            if bal is None:
                return False
            with LOCK:
                deployed = sum(p["shares"] * p["entry"]
                               for p in ALL_POSITIONS)
            equity = bal + deployed
            usd = min(equity * STAKE_FRAC, bal * (1 - CASH_RESERVE))
            usd = max(usd, MIN_SHARES * under_ask)
            if usd < 1 or usd > bal * (1 - CASH_RESERVE):
                log({"ev": "skip_cash", "gid": self.game["gid"],
                     "line": x["line"], "usd": round(usd, 2),
                     "bal": round(bal, 2)})
                return False
            x["pending"] = True
            log({"ev": "signal", "gid": self.game["gid"],
                 "slug": self.res.get("slug"), "line": x["line"],
                 "pm_over": round(pm, 4), "gap": round(mid - pm, 4),
                 "over_bid": ob, "over_ask": oa, "over_mid": round(mid, 4),
                 "under_ask": under_ask, "usd": round(usd, 2),
                 "shares_min": round(usd / under_ask, 2) if under_ask else None,
                 "el": el, "total": total, "period": self.game.get("period"),
                 "under_asks": depth.get("asks"),
                 "under_ask_notional": depth.get("ask_notional"),
                 "live": self.live})
            if not self.live:
                pos = self._pos(x, under_ask,
                                usd / under_ask if under_ask else 0,
                                under_ask)
                paper = True
                return True
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
                # An unknown POST is never re-sent.
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
            if not shares:
                log({"ev": "buy_gave_up", "gid": self.game["gid"],
                     "line": x["line"], "order_id": oid})
                return False
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
        # Never `return` from inside `finally`: it swallows a real exception
        # and would report a failed entry as a success.
        return pos is not None

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
def run(live):
    global CLIENT, OUTDIR, MODEL
    OUTDIR = today_dir()
    os.makedirs(OUTDIR, exist_ok=True)
    MODEL = load_model()
    CLIENT = L.make_client()
    bal = L.balance(CLIENT)
    log({"ev": "start", "live": live, "balance": bal,
         "strategy": "q3_break_under", "p_max": P_MAX, "gap": GAP,
         "el": [EL_LO, EL_HI], "stake_frac": STAKE_FRAC,
         "max_open": MAX_CONCURRENT, "under_max": UNDER_MAX,
         "lines_per_game": LINES_PER_GAME,
         "model": MODEL_PATH, "outdir": OUTDIR})
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
                 "title": res.get("title"), "lines": len(res["lines"])})
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
    global MODEL
    MODEL = load_model()
    client = L.make_client()
    games = scoreboard(DATE)
    print("model", MODEL_PATH, "cells", len(MODEL["cells"]))
    print("sample P(2160, S=160, line=235) =", round(pmodel(2160, 160, 75), 4))
    print("games", len(games), "balance", L.balance(client))
    for g in games:
        if g.get("state") not in ("pre", "in"):
            continue
        res = resolve_totals(g)
        print(g["gid"], g.get("away"), "@", g.get("home"), g.get("state"),
              "->", res and res.get("slug"), "lines", res and len(res["lines"]))
        if not res:
            continue
        for x in res["lines"][:4]:
            oa, ob, tick = L.book(client, x["over"])
            ua, ub, _ = L.book(client, x["under"])
            print(f"  line {x['line']:6.1f} over {ob}/{oa} under {ub}/{ua}")
        print("  depth sample", L.book_depth(client, res["lines"][0]["under"]))
        return
    print("no pre/in game with a totals market")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--date", default=None, help="ESPN date YYYYMMDD")
    ap.add_argument("--duration", type=float, default=0,
                    help="stop after N seconds (smoke test)")
    a = ap.parse_args()
    global DATE
    DATE = a.date or us_date()
    if a.selftest:
        selftest()
        return
    if a.duration:
        threading.Timer(a.duration, lambda: os._exit(0)).start()
    run(live=bool(a.live and not a.dry_run))


if __name__ == "__main__":
    main()
