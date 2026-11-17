"""Offline restarts through the production runner and a reopened SQLite ledger."""
import json
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import live_runner as lr


GAME = "401123456"
ASSET = dict(token="dog", market="condition", outcome="Dog")
MARKET = dict(fav="Fav", pre=.7, outcomes=["Fav", "Dog"], tokens=["fav", "dog"],
              market="condition")


def snapshot(*, state="in", period=1, clock="10:00", score=12):
    return dict(gid=GAME, state=state, period=period, detail=clock, home="FAV", away="DOG",
                home_score=score, away_score=15, start="2026-11-17T00:00:00Z")


class EndOfFeed(BaseException):
    """Stop a polling run once all offline snapshots have been processed."""


class JoinedPool(ThreadPoolExecutor):
    """Complete a background lookup before advancing the fake score feed."""

    def submit(self, *args, **kwargs):
        operation = super().submit(*args, **kwargs)
        wait([operation])
        return operation


class RecoveryStartupTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "var/live_runner/attempts.db"
        self.posts = []
        self.client = Mock()
        self.client.get_order.return_value = None
        self.client.get_trades.return_value = []
        self.balance = Mock(return_value=100.)
        self.events = []
        self.archive = Mock(return_value=None)
        self.before_tick = []

        def deliver(trader, game):
            if game is not None:
                self.before_tick.append([(p["ch"], p["size"]) for p in trader.positions])
                trader.tick(game)

        for key, value in dict(ROOT=str(self.root), load_env=lambda: {"POLY_FUNDER": "alice"},
                               make_client=lambda: self.client, balance=self.balance,
                               ALL_POSITIONS=[], CONFIRMED={"buy": False, "sell": False},
                               log=self.events.append, BOARD_ENABLED=False, STAKE_FRAC=1.,
                               resolve=lambda *_: MARKET, book=lambda *_: (.52, .5, ".01"),
                               book_depth=lambda *_: {}, token_balance=lambda *_: 12.,
                               market_order=self.post).items():
            self.enterContext(patch.object(lr, key, value))
        self.enterContext(patch.object(lr, "archived_final", self.archive))
        self.enterContext(patch.object(lr, "ThreadPoolExecutor", JoinedPool))
        self.enterContext(patch("live_reconcile.http_json", return_value=[]))
        # Preserve the trading and startup logic while delivering ticks synchronously.
        self.enterContext(patch.object(lr.GameTrader, "start", lambda trader: None))
        self.enterContext(patch.object(lr.GameTrader, "put", deliver))
        self.enterContext(patch.object(lr.GameTrader, "join", lambda trader, **kwargs: None))
        self.enterContext(patch.object(lr.time, "sleep", lambda seconds: None))

    @contextmanager
    def db(self):
        ledger = lr.AttemptLedger(str(self.path), "alice", recover=False)
        try:
            yield ledger
        finally:
            ledger.close_path()

    def post(self, client, token, side, qty, price, tick, callback, **kwargs):
        oid = f"new-{len(self.posts)}"
        callback(oid)
        self.posts.append((side, token, qty))
        return {"success": True, "orderID": oid}, oid

    def open_order(self, ledger, channel, *, filled):
        oid = "buy-" + channel
        aid, ok = ledger.begin(GAME, channel, "OPEN", oid, 1, el_in=120,
                               reserved_usd=5, **ASSET)
        self.assertTrue(ok)
        if filled:
            ledger.record_buy_fills(oid, [dict(id=oid + "-trade", size=4., price=.5)])
            ledger.fill(aid, 4., .5)
        return aid

    def boot(self, *slates):
        remaining = iter(slates)
        def scoreboard():
            try:
                return next(remaining)
            except StopIteration:
                raise EndOfFeed
        lr.ALL_POSITIONS.clear()  # A new process has no prior in-memory positions.
        self.before_tick.clear()
        with patch.object(lr, "scoreboard", scoreboard):
            try:
                lr._live_run()
            except EndOfFeed:
                pass

    def test_two_restarts_restore_only_c_after_a_sell_commits_before_log(self):
        with self.db() as ledger:
            a = self.open_order(ledger, "A", filled=True)
            c = self.open_order(ledger, "C", filled=True)
            _, ok = ledger.begin(GAME, "A", "CLOSE", "sell-A", 2, position_id=a,
                                 requested_size=4., **ASSET)
            self.assertTrue(ok)
            ledger.record_sell_fills("sell-A", [dict(id="closed-a", size=4., price=.6)])
        # A crash left both buy events on disk but omitted the committed sale.
        log = self.root / "data/live_trade_final" / lr.TODAY / "000000/events.jsonl"
        log.parent.mkdir(parents=True)
        with log.open("w", encoding="utf-8") as f:
            for channel, aid in [("A", a), ("C", c)]:
                event = dict(ev="buy", gid=GAME, ch=channel, outcome="Dog", filled_shares=4.,
                             entry=.5, el_in=120, position_id=aid, order_id="buy-"+channel)
                print(json.dumps(event), file=f)
        for _ in range(2):
            self.boot([snapshot()])
            self.assertEqual(self.before_tick, [[("C", 4.)]])
            self.assertEqual(self.posts, [])
            with self.db() as ledger:
                self.assertEqual([(r["channel"], r["bought_size"]) for r in ledger.open_rows()],
                                 [("C", 4.)])
                self.assertEqual(ledger.sold_size(GAME, "A"), 4.)

    def check_cash_blocks_buys_and_allows_exit(self, cash):
        self.balance.return_value = cash
        with self.db() as ledger:
            c = self.open_order(ledger, "C", filled=True)
        with patch.object(lr.S, "process_signal", return_value={"el": 2500, "margin": -6, "wall": 1}):
            self.boot([snapshot()], [snapshot(score=13)],
                      [snapshot(period=4, clock="6:00", score=14)])
        self.assertEqual(self.posts, [("SELL", "dog", 4.)])
        with self.db() as ledger:
            close_row, = [r for r in ledger.rows() if r["action"] == "CLOSE"]
            self.assertEqual(close_row["position_id"], c)
            self.assertEqual([r["channel"] for r in ledger.open_rows()], ["C"])

    def test_low_cash_still_exits_existing_four_shares(self):
        self.check_cash_blocks_buys_and_allows_exit(1.50)

    def test_changed_market_listing_at_final_preserves_the_original_asset(self):
        with self.db() as ledger:
            self.open_order(ledger, "C", filled=True)
        changed = dict(MARKET, tokens=["other-fav", "other-dog"], market="other-market")
        with patch.object(lr, "resolve", return_value=changed):
            self.boot([snapshot()], [snapshot(state="post", period=4, clock="Final")])
        with self.db() as ledger:
            task, = ledger.tasks()
            self.assertEqual((task["action"], task["size"], task["token"], task["market"]),
                             ("HOLD", 4, "dog", "condition"))
        self.assertEqual(self.posts, [])

    def test_unknown_cash_still_exits_existing_four_shares(self):
        self.check_cash_blocks_buys_and_allows_exit(None)

    def test_known_empty_account_ends_and_logs_checked_out_sha(self):
        self.balance.return_value = 0.
        def git(*args):
            return subprocess.check_output(["git", "-C", str(self.root), *args],
                                           stderr=subprocess.PIPE, text=True).strip()
        git("init", "-q")
        git("-c", "user.name=Test", "-c", "user.email=test@localhost", "commit",
            "-qm", "release", "--allow-empty")
        with patch.object(lr, "scoreboard", side_effect=EndOfFeed) as scoreboard:
            lr._live_run()
            scoreboard.assert_not_called()
        start, = [event for event in self.events if event["ev"] == "start"]
        self.assertEqual(start["git_sha"], git("rev-parse", "HEAD"))
        self.assertEqual(self.posts, [])

    def test_absent_game_keeps_pending_order_then_verified_final_releases_zero(self):
        self.balance.return_value = 1.50
        with self.db() as ledger:
            self.open_order(ledger, "C", filled=False)
        self.boot([])
        with self.db() as ledger:
            self.assertIn((GAME, "C"), ledger.exposure()[0])
            self.assertEqual(ledger.exposure()[1], 5.)
        self.archive.return_value = snapshot(state="post", period=4, clock="Final")
        self.boot([], [])
        with self.db() as ledger:
            task, = ledger.tasks()
            self.assertEqual((task["channel"], task["order_id"], task["reserved_usd"]),
                             ("C", "buy-C", 5.))
        self.client.get_order.return_value = dict(id="buy-C", asset_id="dog", market="condition",
                                                  side="BUY", status="CANCELED", size_matched="0")
        self.boot([], [])
        with self.db() as ledger:
            self.assertEqual(ledger.exposure()[0:2], (set(), 0.))
            self.assertEqual(ledger.tasks(), [])
        self.assertEqual(self.posts, [])

    def test_prior_day_claims_do_not_end_run_before_new_slate_appears(self):
        # Hong Kong November 18, noon. The first feed still describes November 17.
        now = datetime.fromisoformat("2026-11-18T04:00:00+00:00").timestamp()
        old_final = dict(snapshot(state="post", clock="Final", period=4), gid="old")
        current = dict(snapshot(), start="2026-11-18T00:00:00Z")
        current_final = dict(current, state="post", clock="Final", period=4)
        with self.db() as ledger:
            ledger.handoff("old", "A", reason="unresolved_buy", action="OPEN",
                           order_id="old-buy", reserved_usd=5, **ASSET)
        with patch.object(lr.time, "time", return_value=now), \
             patch.object(lr, "resume_plan", return_value=({}, {("old", "A")})):
            self.boot([old_final], [current], [current_final])
        self.assertEqual(self.before_tick, [[], []])
        self.assertEqual(len([e for e in self.events if e["ev"] == "day_over"]), 1)
        self.assertEqual(self.posts, [])

    def test_all_final_startup_exits_only_for_confirmed_current_hong_kong_slate(self):
        now = datetime.fromisoformat("2026-11-18T04:00:00+00:00").timestamp()
        # Tipoff on the preceding UTC date still belongs to the same Hong Kong day.
        final = dict(snapshot(state="post", clock="Final", period=4),
                     start="2026-11-17T20:00:00Z")
        with patch.object(lr.time, "time", return_value=now):
            self.boot([final])
        self.assertEqual(len([e for e in self.events if e["ev"] == "day_over"]), 1)
        self.assertEqual(self.posts, [])

    def test_unusable_or_future_start_time_cannot_end_an_unseen_slate(self):
        now = datetime.fromisoformat("2026-11-18T04:00:00+00:00").timestamp()
        for start in (None, "invalid", "2026-11-18T00:00:00", "2026-11-18T05:00:00Z"):
            with self.subTest(start=start), patch.object(lr.time, "time", return_value=now), \
                 patch.object(lr, "resume_plan", return_value=({}, {("old", "A")})):
                self.events.clear()
                final = dict(snapshot(state="post", clock="Final", period=4), start=start)
                self.boot([final])
                self.assertFalse(any(e["ev"] == "day_over" for e in self.events))

    def test_archived_previous_day_recovery_does_not_end_new_slate(self):
        now = datetime.fromisoformat("2026-11-18T04:00:00+00:00").timestamp()
        with self.db() as ledger:
            self.open_order(ledger, "C", filled=False)
        self.archive.return_value = snapshot(state="post", period=4, clock="Final")
        with patch.object(lr.time, "time", return_value=now):
            self.boot([])
        with self.db() as ledger:
            self.assertEqual([r["order_id"] for r in ledger.tasks()], ["buy-C"])
        self.assertFalse(any(e["ev"] == "day_over" for e in self.events))
