# -*- coding: utf-8 -*-
"""Collect full-game O/U price histories + scoring timelines for a date range.

Writes one row per game to data/totals_research/games.json:
  {gid, day, home, away, tip, final_total, total_tl[[el,total,wall]],
   lines:[{line, vol, over_tokens, under, over:[[t,price]]}]}

Why this exists: every totals study (the Q3-break Under, the fade signal,
the multi-line question) needs the same raw data, and rebuilding it from
the API each time wastes an hour.  Prices are last-trade prints.
"""
import sys, os, json, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nba_data as nd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "data", "totals_research", "games.json")
LINES = int(os.environ.get("COLLECT_LINES", "10"))


def total_timeline(plays):
    d = {}
    for p in plays:
        hs, as_ = p.get("homeScore"), p.get("awayScore")
        if hs is None or as_ is None:
            continue
        clk = (p.get("clock") or {}).get("displayValue")
        per = (p.get("period") or {}).get("number")
        if not clk or not per:
            continue
        el = nd.elapsed_of(per, clk)
        w = nd.parse_wall(p.get("wallclock") or clk)
        if el is None or w is None:
            continue
        d[el] = (el, int(hs) + int(as_), w)
    return [d[k] for k in sorted(d)]


def slug_date(iso):
    dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return (dt - datetime.timedelta(hours=5)).strftime("%Y-%m-%d")


def main(start, end):
    day = datetime.date.fromisoformat(start)
    stop = datetime.date.fromisoformat(end)
    games = []
    if os.path.exists(OUT):
        games = json.load(open(OUT))
    have = {g["gid"] for g in games}
    while day <= stop:
        ds = day.strftime("%Y%m%d")
        try:
            sb = nd.http_json("https://site.api.espn.com/apis/site/v2/sports/"
                              "basketball/nba/scoreboard?dates=" + ds, timeout=20)
        except Exception as e:
            print("SBERR", ds, repr(e)[:80], flush=True)
            day += datetime.timedelta(days=1)
            continue
        for ev in sb.get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            if (comp.get("status") or {}).get("type", {}).get("state") != "post":
                continue
            gid = str(ev.get("id"))
            if gid in have:
                continue
            try:
                s = nd.http_json("https://site.api.espn.com/apis/site/v2/sports/"
                                 "basketball/nba/summary?event=" + gid, timeout=20)
                comp = s["header"]["competitions"][0]
                tip = nd.parse_wall(comp["date"])
                home = next(c["team"]["abbreviation"] for c in comp["competitors"]
                            if c.get("homeAway") == "home")
                away = next(c["team"]["abbreviation"] for c in comp["competitors"]
                            if c.get("homeAway") == "away")
                sc = {c["team"]["abbreviation"]: int(c["score"])
                      for c in comp["competitors"] if c.get("score")}
                sd = slug_date(comp["date"])
                slug = f"nba-{nd.pm_abbr(away)}-{nd.pm_abbr(home)}-{sd}"
                evp = nd.http_json("https://gamma-api.polymarket.com/events?slug="
                                   + slug, timeout=20)
                if not evp:
                    print("no event", gid, slug, flush=True)
                    continue
                byl = {}
                for m in evp[0]["markets"]:
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
                    toks = json.loads(m.get("clobTokenIds") or "[]")
                    if len(toks) != 2:
                        continue
                    vol = float(m.get("volume") or 0)
                    cur = byl.get(line)
                    if cur is None or vol > cur["vol"]:
                        byl[line] = {"line": line, "vol": vol,
                                     "over_token": toks[0], "under": toks[1]}
                if not byl:
                    print("no lines", gid, slug, flush=True)
                    continue
                top = sorted(byl.values(), key=lambda x: -x["vol"])[:LINES]
                for o in top:
                    pts = nd.prices_history(o["over_token"],
                                            tip - 4 * 3600, tip + 6 * 3600)
                    o["over"] = [[int(t), float(p)] for t, p in pts]
                games.append(dict(gid=gid, day=sd, home=home, away=away,
                                  tip=tip, final_total=sum(sc.values()),
                                  total_tl=total_timeline(s.get("plays") or []),
                                  lines=top, n_lines=len(byl)))
                print("ok", gid, slug, "final", sum(sc.values()), flush=True)
            except Exception as ex:
                print("ERR", gid, repr(ex)[:140], flush=True)
        day += datetime.timedelta(days=1)
    tmp = OUT + ".tmp"
    json.dump(games, open(tmp, "w"), allow_nan=False)
    os.replace(tmp, OUT)
    print("saved", len(games), "->", OUT, flush=True)


if __name__ == "__main__":
    a = sys.argv[1:] or ["2026-10-20", "2026-11-20"]
    main(a[0], a[1])
