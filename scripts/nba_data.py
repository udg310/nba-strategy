# -*- coding: utf-8 -*-
"""Data / clock / HTTP plumbing for the NBA band strategy.  NO strategy here.

Anything that decides *what to trade* or *when to exit* belongs in
`strategy.py` only.  Every trading rule lives there, in one place.

Provided here (pure data plumbing):
  load_data()        -> [{x: game, plays: ESPN plays, dog: dog price history}]
  parse_wall(t)      -> epoch seconds from ESPN wallclock / ISO string / number
  elapsed_of(p, c)   -> game-clock seconds elapsed at (period, "6:17")
  margin_timeline()  -> [(elapsed, home_minus_away, wall)] at score changes
  wall_at_el()       -> interpolated wall time for a given game-clock elapsed

Direction convention (do not break this):
  `home_minus_away` is ORIENTED ONLY; the caller flips it to the favourite's
  point of view with `home_favorite`. Nothing else in this file assumes which
  side is strong.
"""
import json, gzip, os, glob, bisect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = sorted(glob.glob(os.path.join(ROOT, "data/cache/*.json.gz")))
BUNDLE = os.path.join(ROOT, "data/backtest_bundle.json.gz")


def load_data_raw(require_dog=True):
    """Join the raw sources into one game list.

    Returns rows of {"x": game, "plays": ESPN play-by-play, "dog": dog prints}.
    `x["history"]` holds the PREGAME FAVOURITE's price prints.

    require_dog=False keeps games that have no dog price history. Only the
    dog-side channels need it; a favourite-side channel runs on ~60% more
    games this way (584 extra of 1555).
    """
    games = json.load(open(os.path.join(ROOT, "data/games.json")))
    games = [x for x in games if x.get("history") and x.get("states")]
    dogs = json.load(open(os.path.join(ROOT, "data/diverse_opponents.json")))
    dh = {}
    for r in dogs.get("results", []):
        if r.get("history"):
            dh[str(r["game_id"])] = sorted((int(p[0]), float(p[1])) for p in r["history"])
    plays = {}
    for fp in CACHE:
        try:
            with gzip.open(fp) as f:
                d = json.load(f)
        except Exception:
            continue
        if isinstance(d, dict) and isinstance(d.get("header"), dict):
            gid = str(d["header"].get("id") or "")
            if d.get("plays") and gid:
                plays[gid] = d["plays"]
    out = []
    for x in games:
        gid = x["id"]
        if gid not in plays:
            continue
        if require_dog and gid not in dh:
            continue
        out.append({"x": x, "plays": plays[gid], "dog": dh.get(gid, [])})
    return out


def load_data(require_dog=True):
    """Prefer the compact gzipped backtest bundle, then the raw sources.

    The bundle is the same rows as `load_data_raw`, gzipped.  It keeps the
    repository small enough for GitHub while remaining a complete replay
    dataset; `scripts/make_bundle.py` rebuilds it from the raw files.
    """
    if os.path.exists(BUNDLE):
        with gzip.open(BUNDLE, "rt") as f:
            data = json.load(f)
        return [g for g in data if g.get("dog")] if require_dog else data
    return load_data_raw(require_dog)


def parse_wall(t):
    if t is None:
        return None
    if isinstance(t, (int, float)):
        return float(t)
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def elapsed_of(period, clock):
    """Game-clock seconds elapsed. Period base + (720 - remaining).

    Live scoreboard labels ("Halftime", "End of 1st") are handled; play-by-play
    clocks are always "m:ss" and take the arithmetic path.

    Overtime matters: periods 5+ used to return None, which silently truncated
    the score timeline at the end of regulation. Every OT game (56 of 971) then
    looked like a TIE and its winner was recorded backwards. OT periods are 5
    minutes, so period 5 starts at 2880.
    """
    dlow = (clock or "").lower()
    if "halftime" in dlow:
        return 1440.0
    # "End of the 1st  Overtime" must not be read as "end of 1st quarter".
    if "overtime" in dlow and period and period >= 5:
        return 2880.0 + 300.0 * (period - 5) + 300.0
    for marker, sec in (("end of 1st", 720.0), ("end of 2nd", 1440.0),
                        ("end of 3rd", 2400.0)):
        if marker in dlow:
            return sec
    part = (clock or "").split(" - ")[0].strip()
    try:
        if ":" in part:
            mm, ss = part.split(":")
            rem = int(mm) * 60 + float(ss)
        else:
            rem = float(part)
    except Exception:
        return None
    if period and period >= 5:                       # overtime: 5-minute periods
        return 2880.0 + 300.0 * (period - 5) + (300.0 - rem)
    base = {1: 0, 2: 720, 3: 1680, 4: 2400}.get(period)
    if base is None:
        return None
    return base + (720.0 - rem)


def margin_timeline(plays):
    """[(elapsed, home_minus_away, wall)] at each score change, sorted."""
    evs = []
    for p in plays:
        if not isinstance(p, dict):
            continue
        hs = p.get("homeScore")
        as_ = p.get("awayScore")
        per = (p.get("period") or {}).get("number")
        clk = (p.get("clock") or {}).get("displayValue")
        w = parse_wall(p.get("wallclock"))
        if None in (hs, as_, per) or not clk or w is None:
            continue
        el = elapsed_of(per, clk)
        if el is None:
            continue
        evs.append((el, hs - as_, w))
    evs.sort(key=lambda e: (e[0], e[2]))
    return evs


def wall_at_el(evs, el):
    """Interpolate the wall time at a given game-clock elapsed."""
    if not evs:
        return None
    els = [e[0] for e in evs]
    i = bisect.bisect_right(els, el)
    if i == 0:
        return evs[0][2]
    if i >= len(evs):
        return evs[-1][2]
    e0, e1 = evs[i - 1], evs[i]
    if e1[0] == e0[0]:
        return e0[2]
    f = (el - e0[0]) / (e1[0] - e0[0])
    return e0[2] + f * (e1[2] - e0[2])


def margin_at(evs, el):
    """Home-minus-away margin at the last score change at or before `el`."""
    i = bisect.bisect_right([e[0] for e in evs], el) - 1
    return evs[i][1] if i >= 0 else None


# ---------------- team-name mapping (one source of truth) ----------------
# ESPN abbreviation -> Polymarket slug token. Only mismatches are listed.
ESPN2PM = {"WSH": "was", "NY": "nyk", "SA": "sas", "GS": "gsw", "NO": "nop",
           "UTAH": "uta", "BKN": "bkn"}

# Polymarket outcome display name -> ESPN abbreviation.
# Matching is LONGEST-KEY-FIRST: "Hornets" must not resolve via "nets".
NICK = {
    "atlanta": "ATL", "hawks": "ATL",
    "boston": "BOS", "celtics": "BOS",
    "brooklyn": "BKN", "nets": "BKN",
    "charlotte": "CHA", "hornets": "CHA",
    "chicago": "CHI", "bulls": "CHI",
    "cleveland": "CLE", "cavaliers": "CLE",
    "dallas": "DAL", "mavericks": "DAL",
    "denver": "DEN", "nuggets": "DEN",
    "detroit": "DET", "pistons": "DET",
    "golden state": "GS", "warriors": "GS",
    "houston": "HOU", "rockets": "HOU",
    "indiana": "IND", "pacers": "IND",
    "los angeles clippers": "LAC", "clippers": "LAC",
    "los angeles lakers": "LAL", "lakers": "LAL",
    "memphis": "MEM", "grizzlies": "MEM",
    "miami": "MIA", "heat": "MIA",
    "milwaukee": "MIL", "bucks": "MIL",
    "minnesota": "MIN", "timberwolves": "MIN",
    "new orleans": "NO", "pelicans": "NO",
    "new york": "NY", "knicks": "NY",
    "oklahoma city": "OKC", "thunder": "OKC",
    "orlando": "ORL", "magic": "ORL",
    "philadelphia": "PHI", "76ers": "PHI", "sixers": "PHI",
    "phoenix": "PHX", "suns": "PHX",
    "portland": "POR", "trail blazers": "POR", "blazers": "POR",
    "sacramento": "SAC", "kings": "SAC",
    "san antonio": "SA", "spurs": "SA",
    "toronto": "TOR", "raptors": "TOR",
    "utah": "UTA", "jazz": "UTA",
    "washington": "WSH", "wizards": "WSH",
}
_NICK_KEYS = sorted(NICK, key=len, reverse=True)


def pm_abbr(abbr):
    """ESPN abbreviation -> Polymarket slug token."""
    a = (abbr or "").upper()
    return ESPN2PM.get(a, a.lower())


def abbr_of_name(name):
    """Polymarket outcome name ('Trail Blazers') -> ESPN abbr ('POR')."""
    n = (name or "").lower()
    for k in _NICK_KEYS:
        if k in n:
            return NICK[k]
    return None


# ---------------- HTTP (one proxy-aware client for probe + backtest) ----------------
import ssl, urllib.request

PROXY = os.environ.get("NBA_PROXY", "http://127.0.0.1:9567")
_CTX = ssl.create_default_context()
_OPENER = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=_CTX),
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))


def http_json(url, timeout=15, ua="nba-research"):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def prices_history(token, start_ts, end_ts, fidelity=1, timeout=15):
    """[(epoch, price)] prints for one CLOB token. Sorted, both shapes handled."""
    d = http_json(f"https://clob.polymarket.com/prices-history?market={token}"
                  f"&startTs={int(start_ts)}&endTs={int(end_ts)}&fidelity={fidelity}",
                  timeout=timeout)
    out = []
    for p in d.get("history") or []:
        if isinstance(p, dict):
            ts, pr = p.get("t"), p.get("p")
        else:
            ts, pr = p[0], p[1]
        if ts is None or pr is None:
            continue
        out.append((float(ts), float(pr)))
    out.sort()
    return out


def pregame_fav(outcomes, tokens, tip_ts, hours=4):
    """Favourite = higher price at tip, using the LAST print at or before tip.

    This replaces the old `pts[0]` bug (first point of a 4-hour window, which
    could be hours pre-tip and pick the wrong side).
    Returns (fav_name, [(name, price)]).
    """
    px = []
    for name, tok in zip(outcomes, tokens):
        try:
            pts = prices_history(tok, tip_ts - hours * 3600, tip_ts + 600)
        except Exception:
            pts = []
        before = [(t, p) for t, p in pts if t <= tip_ts]
        val = before[-1][1] if before else (pts[0][1] if pts else None)
        px.append((name, val))
    if any(v is None for _, v in px):
        return None, px
    return max(px, key=lambda x: x[1])[0], px


# ---------------- live day rebuild (complete CLOB history) ----------------
LIVE_CACHE = os.path.join(ROOT, "data", "live_full_prices.json")


def load_live_cache():
    try:
        return json.load(open(LIVE_CACHE))
    except Exception:
        return {}


def save_live_cache(c):
    json.dump(c, open(LIVE_CACHE, "w"))


def fetch_game_prices(outcomes, tokens, tip):
    """Full print history per outcome: 4h before tip to 6h after."""
    out = {}
    for name, tok in zip(outcomes, tokens):
        try:
            pts = prices_history(tok, tip - 4 * 3600, tip + 6 * 3600)
        except Exception:
            pts = []
        out[name] = [[t, p] for t, p in pts]
    return out


def load_live_day(day, cache):
    """Rebuild one live day from CLOB history.

    The probe only records while it is running, so its price files start
    75-135 minutes after tip and cannot be used to test a Q1 strategy. This
    rebuilds the whole game from `prices-history`, which is the same kind of
    data as the historical backtest (trade prints).
    """
    D = os.path.join(ROOT, "data", "live_probe", day)
    if not os.path.isdir(D):
        return []
    res = {}
    rp = os.path.join(D, "resolution.jsonl")
    if os.path.exists(rp):
        for line in open(rp):
            r = json.loads(line)
            res.setdefault(r["gid"], r)
    games = []
    for gid, r in sorted(res.items()):
        fp = os.path.join(D, "summaries", f"{gid}.json")
        if not os.path.exists(fp):
            continue
        s = json.load(open(fp))
        comp = s["header"]["competitions"][0]
        home_abbr = next((c["team"].get("abbreviation")
                          for c in comp["competitors"] if c["homeAway"] == "home"), None)
        plays = s.get("plays") or []
        tip = parse_wall(comp.get("date"))
        if not plays or tip is None:
            continue
        if gid not in cache:
            cache[gid] = fetch_game_prices(r["outcomes"], r["tokens"], tip)
        series = cache[gid]
        evs = margin_timeline(plays)
        if len(evs) < 10:
            continue
        at_tip = []
        for name in r["outcomes"]:
            ph = series.get(name) or []
            before = [(t, p) for t, p in ph if t <= tip]
            at_tip.append((name, before[-1][1] if before else None))
        if any(p is None for _, p in at_tip):
            continue
        fav = max(at_tip, key=lambda x: x[1])[0]
        pre = dict(at_tip)[fav]
        home_out = next((o for o in r["outcomes"]
                         if (abbr_of_name(o) or "").upper() == (home_abbr or "").upper()), None)
        hf = (fav == home_out)
        evs = [(el, (hm if hf else -hm), w) for el, hm, w in evs]
        prices = {k: sorted((int(t), float(p)) for t, p in v) for k, v in series.items()}
        games.append({"gid": gid, "day": day, "fav": fav, "pre": pre,
                      "outcomes": r["outcomes"], "evs": evs, "prices": prices})
    return games
