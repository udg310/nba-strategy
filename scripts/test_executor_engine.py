"""Offline engine, paper venue and signed-order recovery contracts."""

import base64
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import executor_clob
from executor_engine import Engine, now_ms
from executor_ledger import Ledger
from executor_reports import Reports
from executor_venues import BeforeTransmission, LiveVenue, PaperVenue


LIVE_ENV = {name: "fixture" for name in ("POLY_PRIVATE_KEY", "POLY_FUNDER", "POLY_API_KEY",
                                         "POLY_API_SECRET", "POLY_API_PASSPHRASE")}
LIVE_ENV.update(EXECUTOR_ORDER_KEY=base64.b64encode(bytes(range(32))).decode(),
                EXECUTOR_POLYGON_RPC_URL="https://rpc.invalid")


class EngineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "ledger.db"
        self.now = now_ms()
        self.req = dict(account="local", environment="paper", game_id="game", asset="713",
                        market="market", channel="D", action="OPEN", amount=3_000_000,
                        limit_price=750000, signal_at=self.now, deadline=self.now + 15000)
        self.open()
        self.addCleanup(lambda: self.ledger.close())

    def open(self):
        self.ledger = Ledger(self.path, "local", "paper", 100_000_000, 8)
        self.reports = Reports(self.ledger)
        self.venue = PaperVenue(self.ledger)
        self.engine = Engine(self.ledger, self.reports, self.venue, now=lambda: self.now)

    def create(self, **changes):
        return self.ledger.create({**self.req, **changes}, self.now)[0]["id"]

    def test_lost_ack_then_restart_after_deadline(self):
        intent_id = self.create()
        submit = self.venue.submit

        def lost(intent):
            submit(intent)
            raise OSError("response lost")

        self.venue.submit = lost
        self.engine.step(intent_id)
        row = self.ledger.get(intent_id)
        self.assertEqual((row["state"], row["reserved"]), ("UNKNOWN", 3_000_000))
        self.ledger.close()
        self.now += 60_000
        self.open()
        self.venue.submit = Mock(side_effect=AssertionError("unexpected repeat"))
        self.engine.recover()
        self.engine.reconcile()
        self.assertEqual(self.create(), intent_id)
        row = self.ledger.get(intent_id)
        self.assertEqual((row["filled"], row["reserved"], row["state"]), (4_000_000, 0, "FILLED"))
        self.assertEqual(row["order_id"], "paper-" + row["attempt_id"])
        self.assertEqual(self.reports.cursor(), "1")
        self.venue.submit.assert_not_called()

    def test_interrupted_submission_quarantines_reservation(self):
        intent_id = self.create()
        self.ledger.claim(intent_id, self.now)
        with self.assertRaisesRegex(LookupError, "paper order unavailable"):
            self.engine.recover()
        row = self.ledger.get(intent_id)
        self.assertEqual((row["state"], row["reserved"]), ("UNKNOWN", 3_000_000))
        self.assertIn("unknown for 30s", self.reports.alert_summary(self.now + 30000))
        self.assertIn("unknown for 5m", self.reports.alert_summary(self.now + 300000))

    def test_claim_must_commit_before_send(self):
        intent_id = self.create()
        self.venue.submit = Mock()
        with self.ledger.tx() as db:
            db.execute("PRAGMA query_only=ON")
        with self.assertRaises(sqlite3.OperationalError):
            self.engine.step(intent_id)
        self.venue.submit.assert_not_called()

    def test_paper_pagination_identity_and_duplicate_gate(self):
        intent_id = self.create()
        intent, _ = self.ledger.claim(intent_id, self.now)
        self.venue.submit(intent)
        with self.assertRaises(sqlite3.IntegrityError):
            self.venue.submit(intent)
        for n in range(100):
            self.venue.submit({**intent, "attempt_id": f"test-{n}"})
        page, cursor = self.venue.trades("")
        self.assertEqual((len(page), cursor), (100, "100"))
        page, cursor = self.venue.trades(cursor)
        self.assertEqual((len(page), cursor), (1, "101"))
        self.assertEqual(self.venue.trades(cursor), ([], cursor))
        self.assertEqual(self.venue.submit({**intent, "deadline": self.now})["state"], "REJECTED_ZERO")

    def test_incomplete_feed_commits_cursor_and_keeps_gate_closed(self):
        self.create()
        self.venue.trades = lambda cursor: ([], str(int(cursor or "0") + 1))
        self.venue.submit = Mock()
        with self.assertRaisesRegex(RuntimeError, "catching up"):
            self.engine.reconcile()
        self.assertEqual(self.reports.cursor(), "128")
        self.assertEqual(self.engine._last_feed, 0)
        self.venue.trades = lambda cursor: ([], cursor)
        self.engine.reconcile()
        self.assertEqual(self.engine._last_feed, self.now)
        self.venue.submit.assert_not_called()

    def test_four_workers_and_shutdown_waits(self):
        for n in range(5):
            self.create(game_id=str(n))
        self.engine.recover()
        entered = threading.Barrier(5, timeout=6)
        release = threading.Event()
        stopped = threading.Event()

        def submit(intent):
            entered.wait()
            release.wait(8)
            return {"state": "UNKNOWN"}

        self.venue.submit = Mock(side_effect=submit)
        self.engine.start()
        try:
            entered.wait()
            def stop():
                self.engine.stop()
                stopped.set()
            stopper = threading.Thread(target=stop)
            stopper.start()
            self.assertFalse(stopped.wait(.1))
        finally:
            release.set()
            self.engine.stop()
        self.assertTrue(stopped.wait(2))
        self.assertEqual(self.venue.submit.call_count, 4)

    @unittest.skipUnless(importlib.util.find_spec("Crypto"), "live encryption dependency")
    def test_signed_identity_survives_lost_reply_and_matches_go_format(self):
        from Crypto.Cipher import AES

        key = bytes(range(32))
        oid = "0x" + "aa" * 32
        intent_id = self.create()
        intent, _ = self.ledger.claim(intent_id, self.now)
        signed = {"signature": "fixture_signature"}
        posts = []

        def exchange(op, payload, budget):
            self.assertGreater(budget.expires, time.monotonic())
            if op == "probe":
                return {"ok": True}
            if op == "prepare":
                self.assertEqual(payload["pending_reservations"], 3_000_000)
                return {"order_id": oid, "signed": signed}
            if op == "post":
                budget.begin_post(payload["deadline"])
                with self.ledger.tx() as db:
                    stored, sealed = db.execute("SELECT order_id,sealed FROM live_prepared").fetchone()
                self.assertEqual(stored, oid)
                self.assertNotIn(signed["signature"].encode(), sealed)
                cipher = AES.new(key, AES.MODE_GCM, nonce=sealed[:12])
                cipher.update(intent["attempt_id"].encode())
                clear = cipher.decrypt_and_verify(sealed[12:-16], sealed[-16:])
                self.assertEqual(json.loads(clear), payload["signed"])
                posts.append(oid)
                raise OSError("simulated disconnect")
            if op == "lookup":
                self.assertEqual(payload["order_id"], oid)
                return {"state": "OPEN", "order_id": oid, "total_filled": 0, "fills": []}
            if op == "trades":
                return {"fills": [{"order_id": oid}, {"order_id": "outside"}],
                        "next": "watermark", "complete": True}
            self.fail(op)

        with patch.dict(os.environ, LIVE_ENV), patch.object(executor_clob, "execute", side_effect=exchange):
            live = LiveVenue(self.ledger)
            result = live.submit(intent)
            self.assertEqual((result["state"], result["order_id"]), ("UNKNOWN", oid))
            live = LiveVenue(self.ledger)
            self.assertEqual(live.submit(intent)["state"], "UNKNOWN")
            self.assertEqual(posts, [oid])
            self.assertEqual(live.lookup(intent)["order_id"], oid)
            with self.assertRaisesRegex(ValueError, "hash differ"):
                live.lookup({**intent, "order_id": "wrong"})
            self.assertEqual(live.trades(""), ([{"order_id": oid, "game_id": "game"},
                                                {"order_id": "outside"}], "watermark"))
            self.assertEqual(live.trades("watermark"), ([], "watermark"))

    @unittest.skipUnless(importlib.util.find_spec("Crypto"), "live encryption dependency")
    def test_shutdown_during_live_prepare_prevents_post(self):
        oid = "0x" + "bb" * 32
        preparing = threading.Event()
        posts = []

        def exchange(op, payload, budget):
            if op == "probe":
                return {"ok": True}
            if op == "trades":
                return {"fills": [], "next": "scanned", "complete": True}
            if op == "prepare":
                preparing.set()
                # The signing call started before stop and completed after it.
                self.assertTrue(budget.stop.wait(5))
                return {"order_id": oid, "signed": {"signature": "fixture"}}
            posts.append(op)
            self.fail(op)

        with patch.dict(os.environ, LIVE_ENV), patch.object(executor_clob, "execute", side_effect=exchange):
            intent_id = self.create()
            live = LiveVenue(self.ledger)
            engine = Engine(self.ledger, self.reports, live)
            engine.recover()
            engine.start()
            try:
                self.assertTrue(preparing.wait(5))
            finally:
                engine.stop()
            self.assertEqual(posts, [])
            self.assertEqual(self.ledger.get(intent_id)["state"], "READY")

    def test_post_hook_rechecks_deadline_and_shutdown(self):
        stop = threading.Event()
        request = Mock(method="POST", extensions={})

        def expire_before_wire(op, payload):
            if payload.get("stop"):
                stop.set()
            executor_clob._before_request(request)

        with patch.object(executor_clob, "_execute", side_effect=expire_before_wire):
            for payload in ({"deadline": self.now - 1}, {"deadline": self.now + 15000, "stop": True}):
                with self.assertRaises(executor_clob.ExpiredBeforePost):
                    executor_clob.execute("post", payload,
                                          executor_clob.CallDeadline(time.monotonic() + 35, stop))

    @unittest.skipUnless(importlib.util.find_spec("Crypto"), "live encryption dependency")
    def test_wall_clock_timeout_abandons_before_transport_and_caps_stuck_workers(self):
        release = threading.Event()
        calls, blocked, sent = [], [], []
        self.addCleanup(release.set)

        def exchange(op, payload, budget):
            if op == "probe":
                return {"ok": True}
            if op == "trades":
                return {"fills": [], "next": "watermark", "complete": True}
            calls.append(op)
            release.wait()  # An uncooperative SDK call with no socket timeout.
            try:
                budget.begin_post(payload["deadline"])
                sent.append(op)
            except executor_clob.ExpiredBeforePost:
                blocked.append(op)

        with patch.dict(os.environ, LIVE_ENV), patch.object(executor_clob, "execute", side_effect=exchange):
            live = LiveVenue(self.ledger)
            start = time.monotonic()
            for _ in range(6):
                with self.assertRaises(BeforeTransmission):
                    live._call("post", {"deadline": self.now + 15000}, time.monotonic() + .10)
            self.assertLess(time.monotonic() - start, 2)
            # The four stuck workers retain their slots. Account reconciliation
            # uses its reserved slot, and never waits behind the stuck senders.
            self.assertEqual(calls, ["post"] * 4)
            self.assertEqual(live.trades(""), ([], "watermark"))
            release.set()
            for _ in range(4):
                self.assertTrue(live._order_slots.acquire(timeout=1))
            self.assertEqual(len(blocked), 4)
            self.assertEqual(sent, [])

    @unittest.skipUnless(importlib.util.find_spec("Crypto"), "live encryption dependency")
    def test_hung_post_after_handoff_cannot_block_stop_or_record_late_receipt(self):
        oid = "0x" + "cd" * 32
        entered = threading.Event()
        release = threading.Event()
        late = threading.Event()
        self.addCleanup(release.set)
        posts = []

        def exchange(op, payload, budget):
            if op == "probe":
                return {"ok": True}
            if op == "trades":
                return {"fills": [], "next": "watermark", "complete": True}
            if op == "prepare":
                return {"order_id": oid, "signed": {"signature": "fixture"}}
            if op == "lookup":
                return {"state": "OPEN", "order_id": oid, "total_filled": 0, "fills": []}
            budget.begin_post(payload["deadline"])
            posts.append(oid)
            entered.set()
            release.wait()  # This POST will not return during shutdown.
            late.set()
            return {"state": "REJECTED_ZERO", "order_id": oid, "retryable": True}

        with patch.dict(os.environ, LIVE_ENV), patch.object(executor_clob, "execute", side_effect=exchange):
            intent_id = self.create()
            live = LiveVenue(self.ledger)
            engine = Engine(self.ledger, self.reports, live)
            engine.recover()
            engine.start()
            try:
                self.assertTrue(entered.wait(3))
            finally:
                start = time.monotonic()
                engine.stop()
            self.assertLess(time.monotonic() - start, 1)
            row = self.ledger.get(intent_id)
            self.assertEqual((row["state"], row["order_id"], row["reserved"]), ("UNKNOWN", oid, 3000000))
            # Start the replacement before the old call returns. Its reply must
            # never change this ledger or release the replacement's reservation.
            self.ledger.close()
            self.open()
            recovered = Engine(self.ledger, self.reports, LiveVenue(self.ledger))
            recovered.recover()
            release.set()
            self.assertTrue(late.wait(1))
            recovered.reconcile()
            self.assertEqual(self.ledger.get(intent_id)["state"], "ACCEPTED")
            self.assertEqual(posts, [oid])

    @unittest.skipUnless(importlib.util.find_spec("py_clob_client_v2"), "live SDK dependency")
    def test_pinned_sdk_uses_guarded_transport_without_post_polling(self):
        import httpx
        from eth_account import Account
        from py_clob_client_v2.http_helpers import helpers

        wallet = Account.create()
        order = "0x" + "44" * 32
        env = dict(POLY_PRIVATE_KEY=wallet.key.hex(), POLY_FUNDER=wallet.address,
                   POLY_API_KEY="fixture", POLY_API_SECRET="c2VjcmV0",
                   POLY_API_PASSPHRASE="fixture", POLY_SIGNATURE_TYPE="0")
        sent = []

        def transport(request):
            self.assertGreater(request.extensions["timeout"]["read"], 0)
            self.assertLessEqual(request.extensions["timeout"]["read"], 5)
            sent.append(request.method)
            if request.method == "POST":
                return httpx.Response(200, json={"success": True, "orderID": order,
                                                 "tradeIDs": ["not-settled-yet"]})
            return httpx.Response(200, json={"data": [], "next_cursor": "LTE="})

        stop = threading.Event()
        with (patch.dict(os.environ, env),
              httpx.Client(transport=httpx.MockTransport(transport)) as client,
              patch.object(helpers, "_http_client", client),
              patch.object(executor_clob, "rpc", return_value="0x89")):
            result = executor_clob.execute("probe", {}, executor_clob.CallDeadline(time.monotonic() + 20, stop))
            self.assertTrue(result["ok"])
            signed = dict(salt="123", maker=wallet.address, signer=wallet.address,
                          tokenId="713", makerAmount="3000000", takerAmount="4000000",
                          side=0, signatureType=0, timestamp="100", metadata="0x" + "00"*32,
                          builder="0x" + "00"*32, signature="0x00", expiration="0")
            result = executor_clob.execute("post", {
                "order_id": order, "signed": signed, "deadline": self.now + 15000,
            }, executor_clob.CallDeadline(time.monotonic() + 35, stop))
            self.assertEqual(result, {"state": "ACCEPTED", "order_id": order})
            self.assertEqual(sent, ["GET", "POST"])


if __name__ == "__main__":
    unittest.main()
