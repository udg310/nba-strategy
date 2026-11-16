"""Live NBA scoreboard, independent of which games have strategy positions."""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from dashboard_store import UTC, stamp


TEAM_NAMES = {
    "Hawks": "老鹰", "Celtics": "凯尔特人", "Nets": "篮网", "Hornets": "黄蜂",
    "Bulls": "公牛", "Cavaliers": "骑士", "Mavericks": "独行侠", "Nuggets": "掘金",
    "Pistons": "活塞", "Warriors": "勇士", "Rockets": "火箭", "Pacers": "步行者",
    "Clippers": "快船", "Lakers": "湖人", "Grizzlies": "灰熊", "Heat": "热火",
    "Bucks": "雄鹿", "Timberwolves": "森林狼", "Pelicans": "鹈鹕", "Knicks": "尼克斯",
    "Thunder": "雷霆", "Magic": "魔术", "76ers": "76人", "Suns": "太阳",
    "Trail Blazers": "开拓者", "Kings": "国王", "Spurs": "马刺", "Raptors": "猛龙",
    "Jazz": "爵士", "Wizards": "奇才",
}


def team_name(name):
    if not isinstance(name, str):
        return None
    if name in TEAM_NAMES:
        return TEAM_NAMES[name]
    return next((cn for nickname, cn in TEAM_NAMES.items() if name.endswith(" " + nickname)), name)


def recorded_names():
    root = Path(__file__).resolve().parents[1] / "data"
    names = {}
    for path in [*root.glob("live9_*.json"), root / "live_recent.json"]:
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            teams = row.get("outcomes")
            if row.get("gid") and isinstance(teams, list) and len(teams) == 2 and all(
                isinstance(t, str) and t in TEAM_NAMES for t in teams
            ):
                names[str(row["gid"])] = " 对 ".join(map(team_name, teams))
    return names


def public_json(url, timeout=4, system_proxy=False):
    # Both the scoreboard and the public book require no trading identity.
    proxy = os.environ.get("NBA_PROXY")
    proxies = {"http": proxy, "https": proxy} if proxy else None if proxy is None and system_proxy else {}
    opener = build_opener(ProxyHandler(proxies))
    with opener.open(
        Request(
            url, headers={"User-Agent": "daoxiang-dashboard/1.0", "Accept": "application/json"}
        ),
        timeout=timeout,
    ) as response:
        return json.load(response)


def fetch_day(date):
    base = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
    return public_json(base + "?" + urlencode({"dates": date.strftime("%Y%m%d")}))


def competitors(comp):
    by_side = {item["homeAway"]: item["team"] for item in comp["competitors"]}
    result = []
    for side in ("away", "home"):
        team = by_side[side]
        result.append(team_name(team.get("displayName") or team["abbreviation"]))
    return result


def live_fixtures(payload):
    games = {}
    names = {}
    if not isinstance(payload.get("events"), list):
        raise ValueError("scoreboard events missing")
    for event in payload["events"]:
        comp = event["competitions"][0]
        status = comp.get("status") or event["status"]
        game_id = str(event["id"])
        if comp.get("competitors"):
            away, home = competitors(comp)
            names[game_id] = away + " 对 " + home
        if status["type"]["state"] != "in":
            games[game_id] = None
            continue
        games[game_id] = {
            "game_id": game_id,
            "game_label": names[game_id],
            "home": home,
            "away": away,
        }
    return games, names


class GamesFeed:
    def __init__(self, reader=fetch_day, interval=3, start=True):
        self.reader, self.interval = reader, interval
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.days = {}
        self.names = recorded_names()
        self.missing = {}
        self.source = {
            "name": "scoreboard",
            "status": "unknown",
            "last_success_at": None,
            "last_event_at": None,
            "reason": "正在获取 NBA 赛程",
        }
        self.thread = None
        self.lookup_thread = None
        if start:
            self.thread = threading.Thread(target=self._work, daemon=True, name="nba-scoreboard")
            self.thread.start()
            self.lookup_thread = threading.Thread(target=self._backfill, daemon=True, name="nba-matchups")
            self.lookup_thread.start()

    def refresh(self, now, pool):
        # US game date may still be yesterday in UTC, including midway through overtime.
        dates = [(now - timedelta(days=1)).date(), now.date()]
        tasks = {d: pool.submit(self.reader, d) for d in dates}
        results = {}
        for d, future in tasks.items():
            try:
                results[d] = live_fixtures(future.result())
            except (HTTPException, OSError, ValueError, TypeError, KeyError, IndexError):
                continue
        with self.lock:
            before = self._games()
            for d in list(self.days.keys() - set(dates)):
                del self.days[d]
            for d, (rows, names) in results.items():
                self.names.update(names)
                prior = self.days.get(d, {})
                self.days[d] = {
                    gid: (
                        dict(
                            row,
                            updated_at=(
                                prior[gid]["updated_at"]
                                if prior.get(gid)
                                and all(prior[gid].get(k) == v for k, v in row.items())
                                else stamp(now)
                            ),
                        )
                        if row
                        else None
                    )
                    for gid, row in rows.items()
                }
            after = self._games()
            ok = len(results) == len(dates)
            self.source = {
                "name": "scoreboard",
                "status": "ok" if ok else "delayed" if results or before else "down",
                "last_success_at": stamp(now) if ok else self.source["last_success_at"],
                "last_event_at": stamp(now) if before != after else self.source["last_event_at"],
                "reason": None if ok else "赛程暂未完整返回，保留上次已知的比赛列表",
            }

    def _games(self):
        merged = {}
        for d in sorted(self.days):
            merged.update(self.days[d])
        return [row for _, row in sorted(merged.items()) if row is not None]

    def read(self):
        with self.lock:
            return self._games(), dict(self.source)

    def matchups(self, known, wanted):
        with self.lock:
            for gid, title in known.items():
                self.names.setdefault(gid, title)
            for gid in wanted:
                if gid and gid.isascii() and gid.isdigit() and len(gid) <= 16 and gid not in self.names:
                    self.missing.setdefault(gid, 0)
            return {gid: self.names[gid] for gid in wanted if gid in self.names}

    def _backfill(self):
        while not self.stop.is_set():
            with self.lock:
                for gid in self.names.keys() & self.missing.keys():
                    del self.missing[gid]
                gid = next((g for g, retry in self.missing.items() if retry <= time.monotonic()), None)
                if gid:
                    self.missing[gid] = time.monotonic() + 300
            if gid:
                try:
                    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
                    header = public_json(url + "?" + urlencode({"event": gid}))["header"]
                    if str(header["id"]) == gid:
                        name = " 对 ".join(competitors(header["competitions"][0]))
                        with self.lock:
                            self.names[gid] = name
                except (HTTPException, OSError, ValueError, TypeError, KeyError, IndexError):
                    pass
            self.stop.wait(1)

    def _work(self):
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="nba-scores") as pool:
            while not self.stop.is_set():
                self.refresh(datetime.now(UTC), pool)
                self.stop.wait(self.interval)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join()
        if self.lookup_thread:
            self.lookup_thread.join()
