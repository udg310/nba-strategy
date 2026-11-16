"""Public NBA moneyline token identities and final redemption values."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import lru_cache
from http.client import HTTPException
from urllib.parse import urlencode

from nba_data import abbr_of_name, pm_abbr
from dashboard_games import fetch_day, public_json
from dashboard_store import instant, stamp


def event_field(value):
    return json.loads(value) if isinstance(value, str) else value


def match_market(events, slugs, sides, start):
    found = {}
    for event in events:
        slug = event.get("slug")
        teams = event.get("teams") or []
        if slug not in slugs or len(teams) != 2 or any(t.get("league", "").lower() != "nba" for t in teams):
            continue
        if {t.get("ordering"): t.get("abbreviation", "").lower() for t in teams} != sides:
            continue
        for m in event.get("markets", []):
            if m.get("slug") != slug or m.get("sportsMarketType") != "moneyline" or not m.get("conditionId"):
                continue
            tip = instant(m["gameStartTime"])
            names, tokens = event_field(m["outcomes"]), event_field(m["clobTokenIds"])
            if (abs(tip - start) > timedelta(minutes=15) or len(names) != 2 or len(tokens) != 2
                    or {pm_abbr(abbr_of_name(n)) for n in names} != set(sides.values())
                    or len(set(tokens)) != 2 or not all(isinstance(t, str) and t.isdigit() for t in tokens)):
                continue
            payouts = {}
            if m.get("closed") is True and m.get("umaResolutionStatus") == "resolved":
                prices = event_field(m["outcomePrices"])
                if sorted(prices) in (["0", "1"], ["0.5", "0.5"]) and m.get("closedTime") and m.get("updatedAt"):
                    closed = instant(m["closedTime"])
                    at = max(closed, instant(m["updatedAt"]))
                    statuses = event_field(m.get("umaResolutionStatuses") or [])
                    try:
                        oracle_end = instant(m["umaEndDate"])
                    except (KeyError, ValueError, TypeError, AttributeError):
                        oracle_end = None
                    # A later metadata edit is not a new payout. Backdate only when the
                    # automatic oracle endpoint and market close agree and no dispute is recorded.
                    if (m.get("automaticallyResolved") is True and start <= closed == oracle_end
                            and isinstance(statuses, list) and statuses and set(statuses) <= {"proposed","resolved"}):
                        at = closed
                    payouts = {t: {"price": p, "effective_at": stamp(at)} for t, p in zip(tokens, prices)}
            found[m["conditionId"]] = {"assets": dict(zip(names, tokens)), "payouts": payouts, "timing_version": 2}
    if len(found) != 1:
        raise ValueError("expected one matching NBA moneyline")
    return next(iter(found.values()))


@lru_cache(maxsize=32)
def schedule(day):
    return {str(e["id"]): e["competitions"][0] for e in fetch_day(day)["events"]}


def competition(gid, day):
    if day:
        for date in (day, day - timedelta(days=1)):
            comp = schedule(date).get(gid)
            if comp:
                return comp
    header = public_json("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?"
                         + urlencode({"event": gid}))["header"]
    if str(header["id"]) != gid:
        raise ValueError("unexpected ESPN game")
    return header["competitions"][0]


def lookup(gid, day=None):
    comp = competition(gid, day)
    start = instant(comp["date"])
    sides = {t["homeAway"]: pm_abbr(t["team"]["abbreviation"]) for t in comp["competitors"]}
    slugs = [f"nba-{first}-{second}-{day:%Y-%m-%d}" for day in (start, start - timedelta(days=1))
             for first, second in ((sides["away"], sides["home"]), (sides["home"], sides["away"]))]
    events = public_json("https://gamma-api.polymarket.com/events?" + urlencode({"slug": slugs}, doseq=True),
                         system_proxy=True)
    return match_market(events, slugs, sides, start)


class MarketDirectory:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dashboard-market")
        self.cache, self.pending, self.retry = {}, {}, {}

    def read(self, wanted, saved):
        # One ingest thread owns the directory. Only public I/O runs on workers.
        for gid, record in saved.items():
            self.cache.setdefault(gid, record)
        now = time.monotonic()
        for gid, future in list(self.pending.items()):
            if future.done():
                del self.pending[gid]
                try:
                    self.cache[gid] = future.result()
                except (HTTPException, OSError, ValueError, KeyError, TypeError, IndexError):
                    pass
                self.retry[gid] = now + 60
        for gid in sorted(wanted):
            if (gid.isascii() and gid.isdigit() and len(gid) <= 16 and gid not in self.pending
                    and self.retry.get(gid, 0) <= now and
                    (not self.cache.get(gid, {}).get("payouts") or self.cache[gid].get("timing_version") != 2)):
                self.pending[gid] = self.pool.submit(lookup, gid, wanted[gid])
        return {gid: self.cache[gid] for gid in wanted if gid in self.cache}

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)
