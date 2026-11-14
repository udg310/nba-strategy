# -*- coding: utf-8 -*-
"""Paper-only book recorder around NBA score changes.

Purpose: test whether the *maker* version of "eat the repricing" is viable.
The live runner trades with FOK taker orders, which pay the spread plus the
~0.10*p*(1-p) fee.  Before building any maker strategy we need the actual
book path around a score change: was the pre-event bid/ask still there after
the score, for how long, and what did the next 60 seconds look like?

This script places NO orders and needs no credentials.  It polls the ESPN
scoreboard, resolves the two Polymarket tokens, and on every score change
records the top of both books once per second for 60 seconds.

Usage (needs the proxy used by nba_data, NBA_PROXY or 127.0.0.1:9567):
    python3 -u scripts/swing_probe.py > data/live_swing.log 2>&1

Output:
    data/live_swing/<UTC date>/<game id>.jsonl
"""
import json, os, sys, threading, time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nba_data as nd
import live_runner as lr

POLL = 3
RECORD_SEC = 60
RECORD_FREQ = 1.0
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
OUT = os.path.join(ROOT, "data", "live_swing", TODAY)
LOCK = threading.Lock()


def log(rec):
    with LOCK:
        os.makedirs(OUT, exist_ok=True)
        with open(os.path.join(OUT, f"{rec.get('gid', 'unknown')}.jsonl"), "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    print(json.dumps(rec, ensure_ascii=False, default=str)[:220], flush=True)


def book(token):
    """Top 5 levels, with timestamps, through the same proxy as the runner."""
    try:
        b = nd.http_json("https://clob.polymarket.com/book?token_id=" + token)
    except Exception as e:
        return {"t": time.time(), "err": str(e)[:120]}
    def lv(x):
        return (float(x["price"]), float(x["size"])) if isinstance(x, dict) \
            else (float(x[0]), float(x[1]))
    asks = sorted((lv(x) for x in (b.get("asks") or [])))[:5]
    bids = sorted((lv(x) for x in (b.get("bids") or [])), reverse=True)[:5]
    return {"t": time.time(), "bids": bids, "asks": asks,
            "tick": b.get("tick_size"),
            "bid": bids[0][0] if bids else None,
            "ask": asks[0][0] if asks else None,
            "bid_notional": round(bids[0][0] * bids[0][1], 2) if bids else None,
            "ask_notional": round(asks[0][0] * asks[0][1], 2) if asks else None}


def record(gid, tick, token, label, score, period, detail):
    """Snapshot one token's book for RECORD_SEC after the score change."""
    t0 = time.time()
    while time.time() - t0 <= RECORD_SEC:
        b = book(token)
        b.update({"ev": "book", "gid": gid, "tick": tick, "token": token,
                  "label": label, "score": score, "period": period,
                  "detail": detail, "dt": round(time.time() - t0, 2)})
        log(b)
        time.sleep(RECORD_FREQ)


def main():
    games = {}
    tick = 0
    os.makedirs(OUT, exist_ok=True)
    log({"ev": "start", "out": OUT, "poll": POLL, "record_sec": RECORD_SEC})
    while True:
        tick += 1
        try:
            sb = lr.scoreboard()
        except Exception as e:
            log({"ev": "espn_err", "err": str(e)[:120]})
            time.sleep(8)
            continue
        for g in sb:
            if g["state"] != "in":
                games.pop(g["gid"], None)
                continue
            if g["gid"] not in games:
                res = lr.resolve(g, None)
                if not res or not res.get("fav") or len(res["tokens"]) != 2:
                    continue
                games[g["gid"]] = {"res": res, "score": None}
                log({"ev": "game", "gid": g["gid"], "fav": res["fav"],
                     "pre": res.get("pre"), "outcomes": res["outcomes"],
                     "tokens": res["tokens"]})
            st = games[g["gid"]]
            cur = (g["home_score"], g["away_score"])
            if st["score"] is not None and cur != st["score"]:
                dm_home = cur[0] - st["score"][0]
                dm_away = cur[1] - st["score"][1]
                log({"ev": "score", "gid": g["gid"], "from": st["score"],
                     "to": cur, "dh": dm_home, "da": dm_away,
                     "period": g["period"], "detail": g["detail"]})
                for tok, label in zip(st["res"]["tokens"], st["res"]["outcomes"]):
                    threading.Thread(
                        target=record,
                        args=(g["gid"], tick, tok, label, cur,
                              g["period"], g["detail"]),
                        daemon=True).start()
            st["score"] = cur
        time.sleep(POLL)


if __name__ == "__main__":
    main()
