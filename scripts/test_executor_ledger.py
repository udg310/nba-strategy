"""Ledger contract tests, exercised against on-disk SQLite across restarts."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest

from executor_ledger import Conflict, Invalid, Ledger, Risk, execute_schema


class LedgerTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "ledger.db"
        self.store = self.open_store()
        self.req = dict(account="local", environment="paper", game_id="game",
                        channel="D", asset="token", market="market", action="OPEN",
                        amount=3_000_000, limit_price=500_000, signal_at=1, deadline=10000)

    def open_store(self):
        store = Ledger(self.path, "local", "paper", 10_000_000, 2)
        self.addCleanup(store.close)
        return store

    def claimed(self, **changes):
        intent, fresh = self.store.create({**self.req, **changes}, 2)
        self.assertTrue(fresh)
        intent, claimed = self.store.claim(intent["id"], 3)
        self.assertTrue(claimed)
        return intent

    def fill(self, intent, qty, now=5, state="FINAL", **changes):
        side = "BUY" if intent["action"] == "OPEN" else "SELL"
        order = side + intent["attempt_id"]
        fill = dict(id="f", order_id=order, asset="token", side=side, quantity=qty,
                    price=500000, time=now, confirmed=True, **changes)
        snap = dict(order_id=order, state=state, total_filled=qty, fills=[fill, fill])
        self.store.apply(intent["attempt_id"], snap, now)
        return self.store.get(intent["id"])

    def test_replay_and_slot_and_cash_serialized_across_connections(self):
        other = self.open_store()
        def create(index):
            store = (self.store, other)[index % 2]
            return store.create(self.req, 2)
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(create, range(100)))
        self.assertEqual(sum(fresh for _, fresh in results), 1)
        self.assertEqual(len({intent["id"] for intent, _ in results}), 1)
        first = results[0][0]
        self.assertRegex(first["id"], r"^[A-Z2-7]{26}$")
        self.assertNotIn("order_id", first)
        self.assertEqual(first["position_id"], first["id"])
        with self.assertRaises(Conflict):
            other.create(dict(self.req, amount=1), 2)
        with self.assertRaises(Risk):
            self.store.create(dict(self.req, channel="C", amount=7_000_001), 2)
        def compete(channel):
            try:
                return self.store.create(dict(self.req, channel=channel), 2)[1]
            except Risk:
                return False
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sum(pool.map(compete, ["A", "C"])), 1)
        self.assertEqual(len(self.store.pending()), 2)

    def test_transactional_v1_upgrade_and_restart(self):
        with self.store.tx() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            conn.execute("DROP TABLE execution_control")
            conn.execute("PRAGMA user_version=1")
        with self.assertRaises(Conflict):
            Ledger(self.path, "other", "paper", 10_000_000, 2)
        with self.store.tx() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        reopened = self.open_store()
        with reopened.tx() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT paused FROM execution_control").fetchone()[0], 0)
        with self.assertRaises(Conflict):
            Ledger(self.path, "local", "paper", 9999999, 2)

    def test_atomic_schema_and_nested_transactions(self):
        with self.assertRaises(sqlite3.OperationalError):
            with self.store.tx() as conn:
                execute_schema(conn, "CREATE TABLE error_test (i);\nDELETE FROM missing;")
        with self.store.tx() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='error_test'").fetchone())
            self.store.create(self.req, 1)
            try:
                with self.store.tx() as inner:
                    inner.execute("UPDATE intents SET amount=1")
                    raise ValueError()
            except ValueError:
                pass
        self.assertEqual(self.store.pending()[0]["amount"], self.req["amount"])

    def test_validation_and_expiration(self):
        for changes in [dict(limit_price=1.5), dict(amount=True), dict(channel="B"),
                        dict(market="  "), dict(deadline=2**63), dict(position_id="o"),
                        dict(amount=10**15 + 1), dict(action="CLOSE")]:
            with self.subTest(changes=changes), self.assertRaises(Invalid):
                self.store.create(dict(self.req, **changes), 2)
        expired, fresh = self.store.create(self.req, self.req["deadline"])
        self.assertTrue(fresh)
        self.assertEqual((expired["state"], expired["reserved"]), ("EXPIRED_UNSENT", 0))
        self.assertFalse(self.store.claim(expired["id"], 10001)[1])
        self.assertEqual(self.store.available(), 10_000_000)
        self.assertFalse(self.store.create(dict(self.req, deadline=20000), 5)[1])

    def test_proof_of_zero_and_retry_budget(self):
        i = self.claimed()
        first = i["attempt_id"]
        self.store.record_submit(first, dict(state="UNKNOWN"), 4)
        self.assertFalse(self.store.claim(i["id"], 5)[1])
        rejection = dict(state="REJECTED_ZERO", retryable=True)
        self.store.apply(first, rejection, 5)
        second, claimed = self.store.claim(i["id"], 6)
        self.assertTrue(claimed)
        self.assertNotEqual(first, second["attempt_id"])
        self.store.record_submit(first, dict(state="ACCEPTED", order_id="stale"), 7)
        self.store.record_submit(second["attempt_id"], rejection, 8)
        got = self.store.get(i["id"])
        self.assertEqual((got["state"], got["reserved"], got["attempt_no"]), ("REJECTED_ZERO", 0, 2))

    def test_only_one_sender_and_hold_rechecked_before_dispatch(self):
        from executor_reports import Reports

        reports = Reports(self.store)
        a, _ = self.store.create(self.req, 1)
        with self.store.tx() as db:
            db.execute("UPDATE execution_control SET paused=1 WHERE singleton=1")
        with self.assertRaises(Risk):
            self.store.claim(a["id"], 2)
        with self.store.tx() as db:
            db.execute("UPDATE execution_control SET paused=0 WHERE singleton=1")
        unknown = dict(id="f", order_id="manual", asset="other", side="BUY",
                       quantity=1_000_000, price=500000, confirmed=True, fee=10)
        reports.observe([unknown], "page")
        with self.assertRaises(Risk):
            self.store.claim(a["id"], 2)
        self.assertEqual(self.store.get(a["id"]), a)
        with self.store.tx() as db:
            db.execute("UPDATE external_fills SET disposition='manual' WHERE order_id='manual'")
        self.assertEqual(self.store.available(), 6_499_990)
        other = self.open_store()
        def claim(n):
            return (self.store, other)[n % 2].claim(a["id"], 3)[1]
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(claim, range(32))), 1)
        self.assertEqual(self.store.get(a["id"])["attempt_no"], 1)
        buy = self.fill(self.store.get(a["id"]), 4_000_000)
        exit_request = dict(self.req, action="CLOSE", position_id=buy["id"], amount=buy["filled"])
        reports.observe([dict(unknown, order_id="unrelated")], "page2")
        sell, _ = self.store.create(exit_request, 6)
        reports.observe([dict(unknown, order_id="same", asset="token")], "page3")
        with self.assertRaises(Risk):
            self.store.claim(sell["id"], 7)

    def test_matched_watermark_survives_restart_and_stale_final(self):
        i = self.claimed()
        f = dict(id="pending", order_id="o", asset="token", side="BUY", quantity=3000000, confirmed=False)
        self.store.apply(i["attempt_id"], dict(state="FINAL", order_id="o", total_filled=0, fills=[f]), 4)
        reopened = self.open_store()
        reopened.apply(i["attempt_id"], dict(state="FINAL", order_id="o", total_filled=0), 5)
        with self.assertRaises(Conflict):
            reopened.apply(i["attempt_id"], dict(state="REJECTED_ZERO", order_id="o"), 6)
        got = reopened.get(i["id"])
        self.assertEqual((got["state"], got["reserved"]), ("ACCEPTED", 3_000_000))
        with self.assertRaises(Conflict):
            reopened.record_submit(i["attempt_id"], dict(state="REJECTED_ZERO"), 7)

    def test_malformed_zero_snapshots_preserve_reservation(self):
        intent = self.claimed()
        for fields in ({'fills': {}}, {'fills': ''}, {'fills': 0}, {'fills': [None]},
                       {'total_filled': False}, {'state': 'FINAL', 'retryable': 1}):
            with self.subTest(fields=fields), self.assertRaises(Invalid):
                self.store.apply(intent['attempt_id'], dict({'state': 'FINAL', 'order_id': 'order'}, **fields), 4)
            self.assertEqual(self.store.get(intent['id']), intent)

    def test_partial_exit_retry_rearm_replay(self):
        buy = self.claimed()
        filled = self.fill(buy, 4_000_000)
        self.assertEqual((filled["filled"], filled["cost"]), (4_000_000, 2_000_000))
        req = dict(self.req, action="CLOSE", position_id=buy["id"], amount=4_000_000, deadline=10)
        with self.assertRaises(Risk):
            self.store.create(dict(req, amount=4_000_001), 7)
        sell = self.claimed(action="CLOSE", position_id=buy["id"], amount=4_000_000, deadline=10)
        partial = self.fill(sell, 1_000_000)
        self.assertEqual((partial["state"], partial["reserved"]), ("READY", 3_000_000))
        second, yes = self.store.claim(sell["id"], 6)
        self.assertTrue(yes)
        partial = self.fill(second, 1_000_000, now=12)
        self.assertEqual((partial["state"], partial["filled"]), ("PARTIAL_FINAL", 2_000_000))
        self.assertEqual(self.store.create(req, 13), (partial, False))
        rearm = dict(req, signal_at=14, deadline=100, amount=2_000_000, limit_price=450000)
        resumed, fresh = self.store.create(rearm, 15)
        self.assertFalse(fresh)
        self.assertEqual((resumed["id"], resumed["state"], resumed["reserved"]), (sell["id"], "READY", 2_000_000))
        self.assertEqual(self.store.create(req, 16)[0], resumed)
        with self.assertRaises(Conflict):
            self.store.create(dict(rearm, limit_price=460000), 17)

    def test_conflicting_fill_batch_rolls_back_and_fee_overrun_blocks_claim(self):
        a = self.claimed(amount=5_000_000)
        c, _ = self.store.create(dict(self.req, channel="C", amount=5_000_000), 1)
        f = dict(id="f", order_id="o", side="BUY", asset="token", quantity=10_000_000,
                 price=500000, fee=1000, confirmed=True)
        snap = dict(order_id="o", state="FINAL", total_filled=10_000_000, fills=[f, dict(f, fee=1001)])
        with self.assertRaises(Conflict):
            self.store.apply(a["attempt_id"], snap, 5)
        self.assertEqual(self.store.get(a["id"]), a)
        with self.store.tx() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM fills").fetchone()[0], 0)
        self.store.apply(a["attempt_id"], dict(snap, fills=[f]), 6)
        self.assertEqual(self.store.available(), -1000)
        with self.assertRaises(Risk):
            self.store.claim(c["id"], 7)

    def test_settlement_requires_evidence_credits_once_and_releases_slot(self):
        self.store.max_open = 1
        buy = self.fill(self.claimed(), 6_000_000)
        req = dict(position_id=buy["id"], venue_event="chain:tx", leg_id="out:1",
                   shares=6_000_000, payout=6_000_000, fee=100, actor="ops", evidence="receipt")
        with self.assertRaises(Invalid):
            self.store.settle(req, 6)
        req["confirmed"] = True
        with self.assertRaises(Risk):
            self.store.settle(dict(req, shares=6_000_001), 6)
        self.assertTrue(self.store.settle(req, 7))
        self.assertFalse(self.store.settle(req, 8))
        self.assertEqual(self.store.available(), 12_999_900)
        with self.assertRaises(Conflict):
            self.store.settle(dict(req, fee=0), 9)
        with self.assertRaises(Risk):
            self.store.create(dict(self.req, action="CLOSE", position_id=buy["id"], amount=req["shares"]), 9)
        self.assertTrue(self.store.create(dict(self.req, amount=12_999_900, channel="C"), 9)[1])


if __name__ == "__main__":
    unittest.main()
