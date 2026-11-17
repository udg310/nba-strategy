"""Offline order and position lifecycle through the HTTP trader."""

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import live_runner as lr


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.db = lr.AttemptLedger(str(Path(self.tmp) / "test.db"), "test")
        self.addCleanup(self.db.close_path)
        self.client = Mock()
        self.trades = []
        self.client.get_trades.side_effect = lambda *_: list(self.trades)
        self.client.get_order.return_value = None
        self.sent = []

        def post(client, token, side, size, price, tick, callback, **options):
            oid = f"sell-{len(self.sent)+1}"
            callback(oid)
            self.sent.append((side, size))
            return {"success": True, "orderID": oid}, oid

        for k, v in dict(
            LEDGER=self.db,
            ROOT=self.tmp,
            make_client=lambda: self.client,
            market_order=post,
            book=lambda *_: (0.52, 0.50, ".01"),
            token_balance=lambda *_: 8,
            ALL_POSITIONS=[],
            log=Mock(),
        ).items():
            self.enterContext(patch.object(lr, k, v))
        self.market = dict(fav="Fav", pre=0.7, market="m", outcomes=["Fav", "Dog"], tokens=["f", "d"])
        self.g = dict(state="in", period=4, detail="6:00", home_score=80, away_score=75)

    def trader(self):
        return lr.GameTrader("game", self.market, "HOME", True)

    def buy(self, ch, n):
        oid = "buy-" + ch
        aid, ok = self.db.begin(
            "game", ch, "OPEN", oid, 1, reserved_usd=5, token="d", market="m", outcome="Dog"
        )
        self.assertTrue(ok)
        self.trades.append(
            dict(
                id=oid + "-1",
                size=n,
                price=".5",
                status="CONFIRMED",
                side="BUY",
                asset_id="d",
                market="m",
                taker_order_id=oid,
            )
        )
        return aid

    def test_stale_wallet_never_resells_sibling_and_restart_rebuilds_once(self):
        self.buy("A", "4")
        self.buy("C", "4")

        def order(oid):
            if oid.startswith("buy"):
                return dict(
                    id=oid, asset_id="d", market="m", side="BUY", status="MATCHED", size_matched="4"
                )

        self.client.get_order.side_effect = order
        t = self.trader()
        t._work_inflight(self.g, time.time())
        p = next(p for p in t.positions if p["ch"] == "A")
        t._sell(p)
        for _ in range(3):
            t._sell(p)
        self.assertEqual(self.sent, [("SELL", 4)])
        self.trades.append(
            dict(
                id="sale",
                size="4",
                price=".6",
                status="CONFIRMED",
                side="SELL",
                taker_order_id="sell-1",
                asset_id="d",
                market="m",
            )
        )
        t._sell(p)
        for _ in range(2):
            rebuilt = self.trader()
            self.assertEqual([(p["ch"], p["size"]) for p in rebuilt.positions], [("C", 4)])
        self.assertEqual(self.db.open_rows()[0]["channel"], "C")

    def test_late_buy_after_deadline_and_restart_sells_only_increment(self):
        self.buy("C", "4")
        first = self.trades.pop()
        t = self.trader()
        t._work_inflight(self.g, time.time())
        self.assertIn("C", t.inflight)
        self.trades.append(first)
        t._work_inflight(self.g, time.time())
        t._manage_exits(self.g, time.time())
        self.assertEqual(self.sent, [("SELL", 4)])
        self.trades.append(
            dict(
                id="sale",
                size="4",
                price=".6",
                status="CONFIRMED",
                side="SELL",
                taker_order_id="sell-1",
                asset_id="d",
                market="m",
            )
        )
        t._manage_exits(self.g, time.time())
        self.trades.append(dict(first, id="late", size="2"))
        rebuilt = self.trader()
        rebuilt._work_inflight(self.g, time.time())
        rebuilt._manage_exits(self.g, time.time())
        self.assertEqual(self.sent, [("SELL", 4), ("SELL", 2)])
        rebuilt._work_inflight(dict(self.g, state="post"), time.time())
        self.assertEqual(self.db.tasks()[0]["next_close_order_id"], "sell-2")

    def test_ack_write_failure_keeps_tracking_after_deadline_without_second_post(self):
        trader = self.trader()
        now = time.time()
        order = dict(
            ch="C",
            token="d",
            outcome="Dog",
            usd=2,
            limit=0.55,
            deadline=now + 15,
            tries=0,
            posted=False,
            t0=now,
            el_sig=1000,
            style="process",
            tp=0.20,
        )
        trader.inflight["C"] = order
        with patch.object(lr, "balance", return_value=100), patch.object(
            self.db, "finish", side_effect=sqlite3.OperationalError("interrupted")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                trader._work_inflight(self.g, now)
        oid = order["order_id"]
        self.trades.append(
            dict(
                id="late",
                size="4",
                price=".5",
                status="CONFIRMED",
                side="BUY",
                asset_id="d",
                market="m",
                taker_order_id=oid,
            )
        )
        trader._work_inflight(self.g, now + 20)
        self.assertEqual(self.sent, [("BUY", 2)])
        self.assertEqual([(p["ch"], p["size"]) for p in trader.positions], [("C", 4)])
        self.assertEqual(trader.inflight["C"]["order_id"], oid)

    def test_migrated_four_share_claim_survives_conflicting_zero(self):
        path = Path(self.tmp) / "production.db"
        with sqlite3.connect(path) as old:
            old.execute(
                """CREATE TABLE attempts (account TEXT,game_id TEXT,channel TEXT,action TEXT,
                attempt_id TEXT PRIMARY KEY,position_id TEXT,order_id TEXT,state TEXT,attempt_no INTEGER,
                signal_at INTEGER,el_in REAL,updated_at INTEGER,filled_shares REAL,filled_price REAL,
                UNIQUE(account,game_id,channel,action))"""
            )
            old.execute(
                """INSERT INTO attempts VALUES
                ('test','game','C','OPEN','original','original','old','FILLED',1,1,101,1,4,.5)"""
            )
        old.close()
        db = lr.AttemptLedger(str(path), "test")
        self.addCleanup(db.close_path)
        with patch.object(lr, "LEDGER", db):
            trader = self.trader()
            self.client.get_order.return_value = dict(
                id="old", asset_id="d", market="m", side="BUY", status="CANCELED", size_matched="0"
            )
            trader.tick(self.g)
            (row,) = db.tasks()
            self.assertEqual(
                (row["state"], row["legacy_bought_size"], row["order_id"]), ("REVIEW", 4, "old")
            )
            db.apply_order("game", "C", "old", [], 0, identity_verified=True)
            (row,) = db.tasks()
            self.assertEqual((row["state"], row["legacy_bought_size"]), ("REVIEW", 4))
            self.assertEqual(self.sent, [])

    def test_legacy_partial_receipts_wait_for_original_total_before_becoming_sellable(self):
        aid = self.buy("C", "2")
        self.db.fill(aid, 4, 0.5)
        with self.db.db:
            self.db.db.execute("UPDATE attempts SET identity_verified=0 WHERE attempt_id=?", (aid,))
        trader = self.trader()
        trader._work_inflight(self.g, time.time())
        self.assertEqual(trader.positions, [])
        self.assertEqual(self.db.open_rows()[0]["bought_size"], 4)
        self.trades[:] = [dict(self.trades[0], id="rest")]
        self.client.get_order.return_value = dict(
            id="buy-C", asset_id="d", market="m", side="BUY", status="MATCHED", size_matched="4"
        )
        trader = self.trader()
        trader._work_inflight(self.g, time.time())
        self.assertEqual(trader.positions[0]["size"], 4)
        self.assertEqual(self.db.open_rows()[0]["identity_verified"], 1)

    def test_final_without_market_listing_keeps_pending_sale_and_log_claim(self):
        aid = self.buy("C", "4")
        self.db.record_buy_fills("buy-C", [dict(id="b", size=4, price=0.5)])
        self.db.fill(aid, 4, 0.5)
        _, ok = self.db.begin(
            "game",
            "C",
            "CLOSE",
            "sale",
            2,
            position_id=aid,
            requested_size=4,
            token="d",
            market="m",
            outcome="Dog",
        )
        self.assertTrue(ok)
        lr._handoff_without_market("game", {("game", "A", "Dog"): (0.4, 2)})
        tasks = {r["channel"]: r for r in self.db.tasks()}
        self.assertEqual((tasks["C"]["order_id"], tasks["C"]["size"]), ("sale", 4))
        self.assertEqual((tasks["A"]["state"], tasks["A"]["size"]), ("REVIEW", 2))
        self.assertEqual(self.sent, [])
