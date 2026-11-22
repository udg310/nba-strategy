# -*- coding: utf-8 -*-
"""Q4 totals trader — one thread per game, ESPN-driven, Polymarket-executed.

WHY ESPN DRIVES
  Polymarket's in-play score is late and occasionally wrong.  Every worker
  therefore polls ESPN for the authoritative score/clock and only uses
  Polymarket for the price it will trade against.  A stale PM score can never
  trigger a bet.

STRATEGY (frozen 2026-11-22, see scripts/totals_params.json)
  At each Q4 checkpoint (6:00 / 5:00 / 4:00 / 3:00 / 2:00) forecast the final
  total from the game state, subtract the market's default line (the ladder
  rung priced nearest 0.50), and divide by the historical error for that game
  shape.  |z| >= 0.4 means the market and the model disagree by more than the
  model's own noise.  One bet per game, the earliest qualifying checkpoint.
    forecast < line -> buy Under      forecast > line -> buy Over
  Over is only taken when the line is <= 20 points above the current total:
  the far Over sits 3-7 points too dear in this market.

  Overtime is not predicted, only priced: a 0-3 point margin at Q4 6:00 goes
  to OT 13.4% of the time and those games score ~23 points more.

RISK
  3% of the live balance per bet, compounding, one bet per game, no cap on
  concurrent positions.  Holds to settlement.  No daily loss limit.
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
import live_runner as L

P = json.load(open(os.path.join(ROOT, "scripts", "totals_params.json")))
COEF = P["model"]["coef"]
OT = P["overtime"]
SIG = {int(k.split("|")[0]): {} for k in P["sigma"]}
for k, v in P["sigma"].items():
    el, bucket = k.split("|")
    SIG[int(el)][bucket] = v
ENT = P["entry"]
CHECKPOINTS = ENT["checkpoints_el"]
Z_MIN = ENT["z_min"]
MAX_BETS = ENT["max_bets_per_game"]
STAKE_FRAC = ENT["stake_frac"]
UNDER_MAX = ENT["under_price_max"]
OVER_NEED_MAX = ENT["over_need_max"]
PRICE_MIN, PRICE_MAX = ENT["price_min"], ENT["price_max"]

MAIN_POLL = 12           # ESPN poll seconds
SETTLE_GRACE = 900       # wait this long after "post" before dropping a game
LOG_LOCK = threading.Lock()
LOCK = threading.Lock()
ALL_POSITIONS = []
RESERVED = 0
CLIENT = None
OUTDIR = None
DATE = None


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


def outdir():
    return os.path.join(ROOT, "data", "totals_live",
                        datetime.now(timezone.utc).strftime("%Y-%m-%d"))


# ------------------------------------------------------------------ model
def bucket(margin):
    return "close" if margin <= 5 else "mid" if margin <= 15 else "big"


def forecast(el, total, margin, recent):
    rt = 2880 - el
    pace = total / el * 60.0
    m = margin
    big21 = 1.0 if m >= 21 else 0.0
    x = [1.0, total, rt, total * rt / 2880.0, pace, pace * rt / 60.0,
         m, m * rt / 2880.0, abs(m), abs(m) * rt / 2880.0,
         recent, recent * rt / 300.0, big21, big21 * rt / 2880.0]
    base = total + sum(c * v for c, v in zip(COEF, x))
    if OT["enabled"] and el >= OT["window_el"]:
        p = OT["default_p"]
        for k in sorted(OT["p_by_margin"], key=int):
            if m <= int(k):
                p = OT["p_by_margin"][k]
                break
        base += p * (OT["ot_points"] - OT["nonot_points"])
    return base


def sigma(el, margin):
    b = bucket(margin)
    row = SIG.get(el)
    if row and b in row:
        return row[b]
    # nearest checkpoint with this bucket
    for k in sorted(SIG, key=lambda e: abs(e - el)):
        if b in SIG[k]:
            return SIG[k][b]
    return P["sigma_fallback"]


# ------------------------------------------------------------------ market
def scoreboard(date=None):
    url = ("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/"
           "scoreboard")
    if date:
        url += "?dates=" + date
    d = nd.http_json(url, timeout=15)
    out = []
    for ev in d.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        st = (comp.get("status") or {}).get("type", {}) or {}
        teams = comp.get("competitors", [])
        home = next((t for t in teams if t.get("homeAway") == "home"), None)
        away = next((t for t in teams if t.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        out.append({
            "gid": str(ev.get("id")),
            "state": st.get("state"),
            "detail": st.get("shortDetail", ""),
            "period": (comp.get("status") or {}).get("period", 0),
            "home": home["team"].get("abbreviation"),
            "away": away["team"].get("abbreviation"),
            "home_score": int(home.get("score") or 0),
            "away_score": int(away.get("score") or 0),
            "start": ev.get("date")})
    return out


def slug_dates(start):
    try:
        dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        dt = datetime.now(timezone.utc).replace(tzinfo=None)
    for off in (0, -1):
        yield (dt + timedelta(days=off)).strftime("%Y-%m-%d")


def resolve_totals(g):
    """Every full-game O/U rung, deduped by line, keeping the liquid one."""
    a = nd.pm_abbr(g["away"])
    h = nd.pm_abbr(g["home"])
    for d in slug_dates(g.get("start")):
        for slug in (f"nba-{a}-{h}-{d}", f"nba-{h}-{a}-{d}"):
            try:
                ev = nd.http_json(
                    "https://gamma-api.polymarket.com/events?slug=" + slug,
                    timeout=15)
            except Exception:
                continue
            if not ev:
                continue
            byl = {}
            for m in ev[0].get("markets") or []:
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
                    toks = toks[::-1]
                vol = float(m.get("volume") or 0)
                cur = byl.get(line)
                if cur is None or vol > cur["vol"]:
                    byl[line] = {"line": line, "over": toks[0],
                                 "under": toks[1], "vol": vol}
            if byl:
                return {"slug": slug, "title": ev[0].get("title"),
                        "lines": sorted(byl.values(), key=lambda x: x["line"])}
    return None


# ------------------------------------------------------------------ trader
class GameTrader(threading.Thread):
    """One ESPN-driven worker per game; owns its own margin account view."""

    def __init__(self, g, res, live):
        super().__init__(daemon=True, name=f"q4-{g['gid']}")
        self.game = dict(g)
        self.res = res
        self.live = live
        self.client = L.make_client()
        self.q = queue.Queue(maxsize=8)
        self.lines = {x["line"]: dict(x) for x in res["lines"]}
        self.positions = []
        self.bets = 0
        self.done_checkpoints = set()
        self.history = []          # [(el, total, wall)] for the recent-pace feature
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
                g = self.q.get(timeout=45)
            except queue.Empty:
                continue
            try:
                self.tick(g)
            except Exception as e:
                log({"ev": "trader_err", "gid": self.game["gid"],
                     "err": repr(e)[:200]})

    # ---- one ESPN snapshot ----
    def tick(self, g):
        total = int(g["home_score"]) + int(g["away_score"])
        margin = abs(int(g["home_score"]) - int(g["away_score"]))
        el = nd.elapsed_of(g.get("period"), g.get("detail"))
        if el is None:
            if g["state"] == "post":
                self.settle(total)
                self.finished = True
            return
        self.history.append((el, total))
        self.history = self.history[-40:]
        if g["state"] == "post" or el >= 2880:
            self.settle(total)
            self.finished = True
            return
        if self.bets >= MAX_BETS:
            return
        # fire once, at the first checkpoint we have passed without acting
        for cp in CHECKPOINTS:
            if cp in self.done_checkpoints:
                continue
            if el < cp:
                continue
            self.done_checkpoints.add(cp)
            self.evaluate(cp, el, total, margin)
            return

    def recent_points(self, el, total):
        cut = el - 300
        for e, t in reversed(self.history):
            if e <= cut:
                return max(0, total - t)
        return 0

    # ---- decision ----
    def evaluate(self, cp, el, total, margin):
        recent = self.recent_points(el, total)
        fc = forecast(el, total, margin, recent)
        sd = sigma(cp, margin)
        # quotes
        quotes = []
        for line, x in self.lines.items():
            oa, ob, _ = L.book(self.client, x["over"])
            if oa is None or ob is None or not (0 < oa < 1 and 0 < ob < 1):
                continue
            quotes.append({"line": line, "over_bid": ob, "over_ask": oa,
                           "mid": (oa + ob) / 2})
        if len(quotes) < 3:
            log({"ev": "no_quotes", "gid": self.game["gid"], "cp": cp})
            return
        quotes.sort(key=lambda q: q["line"])
        default = min(quotes, key=lambda q: abs(q["mid"] - 0.5))
        edge = fc - default["line"]
        z = edge / sd
        need = default["line"] - total
        rec = {"gid": self.game["gid"], "cp": cp, "el": el, "total": total,
               "margin": margin, "recent": recent,
               "forecast": round(fc, 2), "default_line": default["line"],
               "default_mid": round(default["mid"], 4),
               "edge": round(edge, 2), "sigma": round(sd, 2),
               "z": round(z, 3), "need": round(need, 1)}
        if abs(z) < Z_MIN:
            log({"ev": "skip_z", **rec})
            return
        want = "Under" if edge < 0 else "Over"
        if want == "Over" and need > OVER_NEED_MAX:
            log({"ev": "skip_far_over", **rec})
            return
        x = self.lines[default["line"]]
        if want == "Under":
            ua, ub, _ = L.book(self.client, x["under"])
            if ua is None:
                log({"ev": "skip_no_under_ask", **rec})
                return
            price = ua
            if price > UNDER_MAX:
                log({"ev": "skip_under_price", **rec,
                     "under_ask": price})
                return
            token = x["under"]
        else:
            price = default["over_ask"]
            if price > PRICE_MAX:
                log({"ev": "skip_over_price", **rec, "over_ask": price})
                return
            token = x["over"]
        if not (PRICE_MIN <= price <= PRICE_MAX):
            log({"ev": "skip_price_band", **rec, "price": price})
            return
        depth = L.book_depth(self.client, token) or {}
        avail = sum(s for _p, s in (depth.get("asks") or [])[:3])
        log({"ev": "signal", **rec, "side": want, "price": price,
             "ask_depth": round(avail, 1),
             "asks": depth.get("asks")})
        self.bets += 1
        self.enter(default["line"], token, want, price, cp, el, total, margin)

    # ---- execution ----
    def enter(self, line, token, side, price, cp, el, total, margin):
        global RESERVED
        with LOCK:
            RESERVED += 1
        pos = None
        try:
            bal = L.balance(self.client)
            if bal is None:
                log({"ev": "skip_no_balance", "gid": self.game["gid"]})
                return
            usd = bal * STAKE_FRAC
            if usd < 1.0:
                log({"ev": "skip_stake_too_small", "gid": self.game["gid"],
                     "balance": round(bal, 2), "stake": round(usd, 2)})
                return
            log({"ev": "signal_sized", "gid": self.game["gid"], "cp": cp,
                 "side": side, "line": line, "price": price,
                 "balance": round(bal, 2), "stake": round(usd, 2)})
            if not self.live:
                pos = self._pos(line, token, side, price,
                                usd / price if price else 0, cp, el)
                return
            base = L.token_balance(self.client, token)
            t0 = int(time.time())
            oid = None
            try:
                res, signed = L.market_order(
                    self.client, token, "BUY", usd,
                    min(price + 0.02, 0.99), "0.01")
                if isinstance(res, dict):
                    oid = res.get("orderID") or res.get("order_id")
                log({"ev": "buy_posted", "gid": self.game["gid"], "cp": cp,
                     "side": side, "line": line, "price": price,
                     "usd": round(usd, 2), "order_id": oid or signed})
            except Exception as e:
                log({"ev": "buy_error", "gid": self.game["gid"],
                     "err": repr(e)[:200]})
            shares = fill = None
            for _ in range(30):
                fills = self._fills(token, oid, t0)
                if fills:
                    shares = sum(float(t["size"]) for t in fills)
                    notional = sum(float(t["size"]) * float(t["price"])
                                   for t in fills)
                    fill = notional / shares if shares else price
                    break
                cur = L.token_balance(self.client, token)
                if base is not None and cur is not None and cur > base + 0.4:
                    shares, fill = cur - base, price
                    break
                time.sleep(2)
            if not shares:
                log({"ev": "buy_gave_up", "gid": self.game["gid"], "cp": cp,
                     "order_id": oid})
                return
            pos = self._pos(line, token, side, fill, shares, cp, el)
        finally:
            with LOCK:
                RESERVED = max(0, RESERVED - 1)
                if pos is not None:
                    ALL_POSITIONS.append(pos)
            if pos is not None:
                self.positions.append(pos)
                log({"ev": "paper_buy" if not self.live else "buy", **pos})

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

    def _pos(self, line, token, side, entry, shares, cp, el):
        return {"gid": self.game["gid"], "line": line, "token": token,
                "side": side, "entry": entry, "shares": shares,
                "checkpoint": cp, "el_in": el, "t_in": round(time.time(), 3),
                "settled": False}

    # ---- settlement ----
    def settle(self, total):
        for p in self.positions:
            if p.get("settled"):
                continue
            p["settled"] = True
            over_won = total > p["line"]
            won = over_won if p["side"] == "Over" else (not over_won)
            payout = 1.0 if won else 0.0
            pnl = (payout - p["entry"]) * p["shares"]
            tok_bal = L.token_balance(self.client, p["token"])
            log({"ev": "settle", **p, "final_total": total, "won": won,
                 "pnl": round(pnl, 4), "token_balance": tok_bal,
                 "usdc": round((L.balance(self.client) or 0), 2),
                 "needs_redeem": tok_bal is not None and tok_bal > 0.5})
            with LOCK:
                try:
                    ALL_POSITIONS.remove(p)
                except ValueError:
                    pass
        self.positions = []


# ------------------------------------------------------------------ main
def run(live):
    global CLIENT, OUTDIR, DATE
    OUTDIR = outdir()
    os.makedirs(OUTDIR, exist_ok=True)
    DATE = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y%m%d")
    CLIENT = L.make_client()
    bal = L.balance(CLIENT)
    log({"ev": "start", "live": live, "balance": bal,
         "strategy": "q4_totals_one_bet", "z_min": Z_MIN,
         "max_bets_per_game": MAX_BETS, "stake_frac": STAKE_FRAC,
         "checkpoints": CHECKPOINTS, "under_max": UNDER_MAX,
         "over_need_max": OVER_NEED_MAX, "params_version": P["version"],
         "outdir": OUTDIR})
    traders = {}
    seen_post = {}
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
            res = resolve_totals(g)
            if not res:
                log({"ev": "no_totals_market", "gid": gid,
                     "away": g.get("away"), "home": g.get("home")})
                continue
            tr = GameTrader(g, res, live)
            traders[gid] = tr
            tr.start()
            log({"ev": "trader_start", "gid": gid, "slug": res.get("slug"),
                 "title": res.get("title"), "lines": len(res["lines"])})
        now = time.time()
        for gid, tr in list(traders.items()):
            if tr.finished and not tr.positions:
                tr.stop()
                traders.pop(gid, None)
                log({"ev": "trader_done", "gid": gid})
        if now - last_hb >= 60:
            last_hb = now
            with LOCK:
                n = len(ALL_POSITIONS)
                expo = round(sum(p["shares"] * p["entry"] for p in ALL_POSITIONS), 2)
            log({"ev": "heartbeat", "balance": round(L.balance(CLIENT) or 0, 2),
                 "open": n, "exposure": expo, "traders": len(traders),
                 "games": len(games)})
        if games and all(g.get("state") == "post" for g in games) and not traders:
            log({"ev": "day_over"})
            return
        time.sleep(MAIN_POLL)


def selftest():
    client = L.make_client()
    games = scoreboard(DATE)
    print("date", DATE, "games", len(games), "balance", L.balance(client))
    # model sanity checks against known points
    for el, tot, marg, recent in ((2520, 167, 1, 22), (2520, 221, 5, 22),
                                  (2760, 198, 16, 20)):
        print(f"  forecast(el={el}, total={tot}, margin={marg}) = "
              f"{forecast(el, tot, marg, recent):.1f}  sigma={sigma(el, marg):.2f}")
    for g in games:
        if g.get("state") not in ("pre", "in"):
            continue
        res = resolve_totals(g)
        print(" ", g["gid"], g.get("away"), "@", g.get("home"), g.get("state"),
              "->", res and res.get("slug"), res and len(res["lines"]))
        if not res:
            continue
        x = res["lines"][len(res["lines"]) // 2]
        print("    mid line", x["line"], "book",
              L.book(client, x["over"]), L.book(client, x["under"]))
        return
    print("no pre/in game with totals")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--duration", type=float, default=0)
    a = ap.parse_args()
    global DATE
    DATE = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y%m%d")
    if a.selftest:
        selftest()
        return
    if a.duration:
        threading.Timer(a.duration, lambda: os._exit(0)).start()
    run(live=bool(a.live and not a.dry_run))


if __name__ == "__main__":
    main()
