import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from dashboard import API, WebServer
from dashboard_games import GamesFeed
from dashboard_markets import match_market
from dashboard_service import Dashboard
from dashboard_store import UTC, VersionStore, stamp


NOW = datetime(2026, 11, 16, 16, 0, 5, tzinfo=UTC)


def fixture(gid, state="in", score="54", clock="6:02"):
    teams = [
        {"homeAway": s, "team": {"displayName": gid + s}, "score": n}
        for s, n in (("home", score), ("away", "50"))
    ]
    return {
        "id": gid,
        "competitions": [
            {
                "competitors": teams,
                "status": {
                    "type": {"state": state, "shortDetail": "Q3"},
                    "period": 3,
                    "displayClock": clock,
                },
            }
        ],
    }


def activity(i, when, side="buy"):
    buy = side == "buy"
    return dict(
        id=str(i),
        time=stamp(when),
        kind=side,
        evidence="confirmed",
        position_id="pos",
        asset_id="asset",
        game_id="one",
        game_label="One",
        market="match",
        channel="A",
        outcome="home",
        source="executor",
        order_id="o" + str(i),
        fill_id=str(i),
        recorded_at=stamp(when),
        revised_at=None,
        time_kind="simulated",
        fee_unit="cash",
        issues=[],
        quantity="10" if buy else "4",
        price="0.4" if buy else "0.6",
        gross="4" if buy else "2.4",
        fee="0.10" if buy else "0.04",
        net_cash="-4.10" if buy else "2.36",
    )


class Inputs:
    def __init__(self):
        self.rows = []
        self.quote = {}

    def read(self):
        return dict(
            activity=list(self.rows),
            timeline={},
            meta={},
            sources=[
                {
                    "name": "executor",
                    "status": "ok",
                    "last_event_at": None,
                    "last_success_at": None,
                    "reason": None,
                }
            ],
            known_origin=True,
            coverage_from=stamp(NOW - timedelta(days=2)),
            available_cash=None,
        )

    def close(self):
        pass


class Books:
    def __init__(self):
        self.quotes = {}

    def read(self, env):
        return dict(self.quotes), {
            "name": "quotes",
            "status": "ok",
            "last_success_at": None,
            "last_event_at": None,
            "reason": None,
        }

    def watch(self, env, assets):
        pass

    def close(self):
        pass


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cache.db"
        self.source = Inputs()
        self.books = Books()
        self.fixtures = [fixture("one"), fixture("two"), fixture("done", "post")]
        self.games = GamesFeed(lambda day: {"events": self.fixtures}, start=False)
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.games.refresh(NOW, self.pool)
        self.store = VersionStore(self.path, "scope", "live")
        self.app = Dashboard({"live": self.source}, {"live": self.store}, self.books, self.games)
        self.app.poll("live", NOW)
        self.http = WebServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.http.server_port) + API

    def tearDown(self):
        self.app.stop_event.set()
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.app.close()
        self.pool.shutdown()
        self.tmp.cleanup()

    def get(self, path, **params):
        with urlopen(self.url + path + "?" + urlencode(params), timeout=5) as response:
            return json.load(response)

    def test_live_reporting_floor_applies_to_history_positions_and_curves(self):
        start = NOW.replace(hour=16, minute=0, second=0) - timedelta(days=2)
        self.app.report_starts["live"] = start
        self.source.rows = [
            dict(activity(1, start - timedelta(seconds=1)), position_id="excluded"),
            activity(2, start),
            dict(activity(3, NOW), position_id="missing-basis", kind="sell"),
        ]
        self.app.poll("live", NOW)
        snap = self.get("/snapshot")["data"]
        self.assertEqual([r["id"] for r in snap["today_activity"]], ["3"])
        self.assertEqual([p["id"] for p in snap["open_positions"]], ["pos"])
        for view in ("open", "history"):
            self.assertEqual([p["id"] for p in self.get("/positions", view=view)["data"]["items"]], ["pos"])
        rows = self.get("/activity", **{"from": stamp(start-timedelta(days=1)), "to": stamp(start+timedelta(days=1))})
        self.assertEqual([r["id"] for r in rows["data"]["items"]], ["2"])
        for window in ("7d", "30d", "all"):
            curve = self.get("/pnl/history", range=window)["data"]
            self.assertEqual(curve["from"], stamp(start))
            self.assertEqual(curve["points"][-1]["total"], snap["summary"]["total"])

    def test_live_games_have_a_direct_page_and_navigation(self):
        with urlopen(self.url.removesuffix(API) + "/games?env=paper") as response:
            page = response.read().decode()
        self.assertIn('href="/games"', page)
        self.assertIn('id="gamesPanel"', page)

    def test_market_marks_use_corroborated_resolution_time_and_correct_team(self):
        tip = NOW - timedelta(days=2)
        end = stamp(NOW - timedelta(days=1))
        sides = {"home": "mil", "away": "atl"}
        market = dict(slug="s", sportsMarketType="moneyline", conditionId="condition", gameStartTime=stamp(tip),
                      outcomes=["Hawks","Bucks"], clobTokenIds=["12","21"], closed=True,
                      outcomePrices=["0","1"], closedTime=end, updatedAt=stamp(NOW), automaticallyResolved=True,
                      umaEndDate=end, umaResolutionStatus="resolved", umaResolutionStatuses='["proposed"]')
        event = dict(slug="s", teams=[dict(league="nba", ordering=k, abbreviation=v) for k,v in sides.items()],
                     markets=[market])
        def payouts():
            return match_market([event], ["s"], sides, tip)["payouts"]
        self.assertEqual(payouts()["12"], {"price":"0", "effective_at":end})
        market["umaResolutionStatuses"] = '["disputed","resolved"]'
        self.assertEqual(payouts()["12"]["effective_at"], stamp(NOW))
        market["umaResolutionStatus"] = "proposed"
        self.assertEqual(payouts(), {})

    def test_finished_game_names_remain_on_trades_and_positions(self):
        self.source.rows = [dict(activity(1, NOW), game_id="done", game_label=None)]
        self.app.poll("live", NOW)
        snap = self.get("/snapshot")["data"]
        self.assertEqual(snap["today_activity"][0]["game_label"], "doneaway 对 donehome")
        self.assertEqual(snap["open_positions"][0]["game_label"], "doneaway 对 donehome")
        self.assertNotIn("done", [g["game_id"] for g in snap["live_games"]])

    def test_all_games_update_independently_of_trading_and_replay(self):
        first = self.get("/snapshot")
        games = first["data"]["live_games"]
        self.assertEqual([g["game_id"] for g in games], ["one", "two"])
        self.assertTrue(all(g["total"]["value"] == "0" for g in games))
        self.source.rows = [activity(1, NOW), activity(2, NOW + timedelta(seconds=1), "sell")]
        t = NOW + timedelta(seconds=2)
        self.books.quotes = {
            "asset": {"bid": ".50", "ask": ".55", "observed_at": stamp(t), "source_at": None}
        }
        self.app.poll("live", t)
        delta = self.get("/changes", after=first["version"])["data"]
        patch = delta["items"][0]["patch"]
        self.assertEqual(patch["summary"]["total"]["value"], "1.26")
        one, two = patch["live_games"]
        self.assertEqual(
            (one["realized"]["value"], one["unrealized"]["value"], one["total"]["value"]),
            ("0.72", "0.54", "1.26"),
        )
        self.assertEqual(two["trade_count"], 0)
        self.assertEqual(len(patch["activity_upsert"]), 2)
        self.assertEqual(self.get("/snapshot", at_version=first["version"])["data"], first["data"])
        self.assertEqual(self.get("/changes", after=delta["next_after"])["data"]["items"], [])
        # The same durable version is sent through SSE on an actual TCP connection.
        with urlopen(self.url + "/stream?after=" + first["version"], timeout=4) as stream:
            self.assertEqual(stream.headers.get_content_type(), "text/event-stream")
            frame = [stream.readline().decode().strip() for _ in range(4)]
            self.assertEqual(frame[0], "id: " + delta["next_after"])
            self.assertEqual(json.loads(frame[2].removeprefix("data: ")), delta["items"][0])
        self.fixtures[0] = fixture("one", score="56", clock="5:45")
        self.games.refresh(t, self.pool)
        self.app.poll("live", t)
        score_only = self.get("/snapshot")
        game = score_only["data"]["live_games"][0]
        self.assertFalse({"home_score", "away_score", "period", "clock"} & game.keys())
        self.assertEqual(game["total"]["value"], "1.26")
        t += timedelta(seconds=1)
        self.books.quotes = {
            "asset": dict(self.books.quotes["asset"], bid=".45", observed_at=stamp(t))
        }
        self.app.poll("live", t)
        quote_only = self.get("/changes", after=score_only["version"])["data"]["items"][0]["patch"]
        self.assertNotIn("activity_upsert", quote_only)
        self.assertEqual(quote_only["live_games"][0]["total"]["value"], "0.96")
        self.assertEqual(quote_only["live_games"][1]["total"]["value"], "0")
        self.app.poll("live", t + timedelta(seconds=16))
        self.assertIsNone(self.get("/snapshot")["data"]["live_games"][0]["total"]["value"])
        self.fixtures[0] = fixture("one", "post")
        self.games.refresh(t, self.pool)
        self.app.poll("live", t)
        self.assertEqual(
            [x["game_id"] for x in self.get("/snapshot")["data"]["live_games"]], ["two"]
        )
        self.assertEqual(len(self.get("/positions")["data"]["items"]), 1)

    def test_hk_cutoff_full_day_and_pinned_pages(self):
        self.source.rows = [
            activity("y1", NOW - timedelta(minutes=2)),
            activity("y2", NOW - timedelta(minutes=1)),
        ]
        self.source.rows.extend(activity(i, NOW) for i in range(1001))
        self.app.poll("live", NOW)
        snapshot = self.get("/snapshot")["data"]
        self.assertEqual(snapshot["business_date"], "2026-11-17")
        self.assertEqual(len(snapshot["today_activity"]), 1001)
        first = self.get("/activity", limit=1)
        self.source.rows.append(activity("late", NOW - timedelta(seconds=30)))
        self.app.poll("live", NOW + timedelta(seconds=1))
        second = self.get("/activity", limit=1, cursor=first["data"]["next_cursor"])
        self.assertEqual(second["version"], first["version"])
        self.assertEqual(
            [first["data"]["items"][0]["id"], second["data"]["items"][0]["id"]], ["y2", "y1"]
        )
        with self.assertRaises(HTTPError) as err:
            self.get("/activity", cursor=first["data"]["next_cursor"], channel="B")
        self.assertEqual(err.exception.code, 400)
        self.app.poll("live", NOW + timedelta(days=1))
        self.assertEqual(self.get("/snapshot")["data"]["today_activity"], [])
        self.assertEqual(len(self.get("/activity", limit=200)["data"]["items"]), 200)

    def test_scoreboard_failure_and_recovery(self):
        def down(_):
            raise OSError

        self.games.reader = down
        self.games.refresh(NOW, self.pool)
        self.app.poll("live", NOW)
        snap = self.get("/snapshot")["data"]
        self.assertEqual(len(snap["live_games"]), 2)
        self.assertNotEqual(snap["games_source"]["status"], "ok")
        self.games.reader = lambda _: {"events": []}
        self.games.refresh(NOW, self.pool)
        self.app.poll("live", NOW)
        snap = self.get("/snapshot")["data"]
        self.assertEqual((snap["live_games"], snap["games_source"]["status"]), ([], "ok"))

    def test_restart_and_invalid_generation(self):
        before = self.get("/snapshot")
        reopened = VersionStore(self.path, "scope", "live")
        try:
            ver, _, state = reopened.read()
            self.assertEqual(ver, before["version"])
            self.assertEqual(state["snapshot"], before["data"])
        finally:
            reopened.close()
        with self.assertRaises(HTTPError) as err:
            self.get("/changes", after="another.1")
        self.assertEqual(err.exception.code, 410)

    def test_historical_revision_survives_an_unchanged_reread(self):
        self.source.rows = [activity(1, NOW)]
        self.app.poll("live", NOW)
        self.source.rows[0] = dict(self.source.rows[0], fee="0.20", net_cash="-4.20")
        t = NOW + timedelta(seconds=2)
        self.app.poll("live", t)
        version, _, view = self.store.read()
        self.assertEqual(view["activity"][0]["revised_at"], stamp(t))
        self.app.poll("live", t + timedelta(seconds=1))
        self.assertEqual(self.store.version(), version)


if __name__ == "__main__":
    unittest.main()
