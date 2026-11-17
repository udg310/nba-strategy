"""Offline regression coverage for the per-account reconciliation ledger."""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from live_ledger import AttemptLedger, SUBMITTING


def fill(fill_id, size, price=.5):
    return {"id": fill_id, "size": size, "price": price}


class ReconciliationStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "live.sqlite")
        self.ledger = AttemptLedger(self.path, "alice")
        self.addCleanup(lambda: self.ledger.close_path())

    def reopen(self):
        self.ledger.close_path()
        self.ledger = AttemptLedger(self.path, "alice")

    def handoff(self, channel="A", **kw):
        return self.ledger.handoff("game", channel, token="shared", market="m",
                                   outcome="home", reason="force", **kw)

    def task(self, channel="A"):
        return next(r for r in self.ledger.tasks(False) if r["channel"] == channel)

    def test_handoff_is_all_or_nothing_and_tombstone_blocks_replay(self):
        aid, _ = self.ledger.begin("game", "A", "OPEN", "signed", 1, reserved_usd=4)
        self.ledger.db.execute("""CREATE TRIGGER fail BEFORE UPDATE ON attempts
            BEGIN SELECT RAISE(ABORT, 'power lost'); END""")
        with self.assertRaises(sqlite3.DatabaseError):
            self.handoff(action="OPEN", order_id="signed")
        self.assertEqual(self.ledger.tasks(), [])
        self.reopen_after_dropping_trigger()
        self.assertEqual(self.ledger.open_rows()[0]["attempt_id"], aid)
        self.handoff(action="OPEN", order_id="signed")
        self.assertEqual(self.task()["reserved_usd"], 4)
        self.ledger.fill(aid)  # late callback from the pre-handoff owner
        self.assertEqual(self.ledger.open_rows(), [])
        self.assertTrue(self.ledger.apply_order("game", "A", "signed", [], 0))
        self.reopen()
        self.assertEqual(self.ledger.tasks(), [])
        self.assertEqual(self.handoff(action="OPEN", order_id="signed")["state"], "DONE")
        self.assertEqual(self.ledger.begin("game", "A", "CLOSE", "other", 2), (None, False))

    def reopen_after_dropping_trigger(self):
        self.ledger.db.execute("DROP TRIGGER fail")
        self.reopen()

    def test_buy_fills_commit_with_position_and_original_terminal_total(self):
        self.handoff(action="OPEN", order_id="buy", reserved_usd=5)
        self.ledger.db.execute("""CREATE TRIGGER fail BEFORE UPDATE ON reconcile_tasks
            BEGIN SELECT RAISE(ABORT, 'crash before position update'); END""")
        with self.assertRaises(sqlite3.DatabaseError):
            self.ledger.apply_order("game", "A", "buy", [fill("one", 2)], 3)
        self.reopen_after_dropping_trigger()
        self.assertFalse(self.ledger.apply_order("game", "A", "wrong", [fill("one", 2)], 2))
        self.assertTrue(self.ledger.apply_order("game", "A", "buy", [fill("one", 2)], 3))
        self.assertEqual(self.ledger.exposure(), ({("game", "A")}, 4, False, 1))
        self.reopen()
        self.ledger.mark_review("game", "A", "delayed receipt")
        self.ledger.apply_order("game", "A", "buy", [fill("one", 2), fill("two", 1, .8)])
        task = self.task()
        self.assertEqual((task["action"], task["size"], task["reserved_usd"]), ("HOLD", 3, 0))
        self.assertAlmostEqual(task["entry"], .6)
        self.assertEqual(task["reason"], "force")

    def test_active_sells_survive_missing_logs_retries_and_handoff(self):
        aid, _ = self.ledger.begin("game", "A", "CLOSE", "s1", 1)
        sold = fill("1", 1.5)
        self.assertEqual(self.ledger.record_sell_fills("s1", [sold, sold]), [sold])
        self.assertFalse(self.ledger.finish_sell_order(aid, "s1", 2))
        self.assertEqual(self.ledger.begin("game", "A", "CLOSE", "s2", 2), (None, False))
        self.assertTrue(self.ledger.finish_sell_order(aid, "s1", 1.5))
        _, ok = self.ledger.begin("game", "A", "CLOSE", "s2", 2)
        self.assertTrue(ok)
        self.ledger.record_sell_fills("s2", [fill("2", .5)])
        self.reopen()
        self.assertEqual(self.ledger.sold_size("game", "A"), 2)
        self.handoff(action="CLOSE", order_id="s2", size=2, entry=.4)
        self.handoff("C", size=4, entry=.7)
        self.assertFalse(self.ledger.apply_resolution("game", "A", 0))
        self.assertEqual(self.ledger.record_sell_fills("s2", [fill("3", .5)]), [])
        self.ledger.apply_order("game", "A", "s2", [fill("2", .5), fill("3", .5)], 1)
        self.assertEqual((self.task()["size"], self.task()["action"]), (1.5, "HOLD"))
        self.assertEqual((self.task()["sold_shares"], self.task()["sell_proceeds"]), (2.5, 1.25))
        self.assertEqual(self.task("C")["size"], 4)

    def test_legacy_partial_waits_for_all_fills_then_applies_only_difference(self):
        self.handoff(action="CLOSE", order_id="old", size=3, entry=.4, legacy_sold=1)
        self.ledger.apply_order("game", "A", "old", [fill("x", 1, .3)], 2)
        self.assertEqual((self.task()["state"], self.task()["size"]), ("REVIEW", 3))
        self.reopen()
        self.ledger.apply_order("game", "A", "old", [fill("y", 1, .5)])
        self.assertEqual((self.task()["action"], self.task()["size"], self.task()["sold_shares"]),
                         ("HOLD", 2, 2))
        self.assertAlmostEqual(self.task()["sell_proceeds"], .8)

    def test_conflicting_or_excess_fills_do_not_consume_another_position(self):
        self.handoff(action="CLOSE", order_id="s", size=2, entry=.4)
        self.ledger.apply_order("game", "A", "s", [fill("x", 1)])
        self.ledger.apply_order("game", "A", "s", [fill("x", 1, .7), fill("z", 2)], 3)
        self.assertEqual((self.task()["state"], self.task()["size"]), ("REVIEW", 1))
        self.assertEqual(self.task()["action"], "CLOSE")

    def test_resolution_and_redemption_require_all_and_unique_evidence(self):
        for ch in ("A", "C", "D"):
            self.handoff(ch, size=4, entry=.4)
        self.assertTrue(self.ledger.apply_resolution("game", "D", 0))
        self.assertEqual((self.task("D")["state"], self.task("D")["settled_shares"]), ("DONE", 4))
        self.assertFalse(self.ledger.confirm_redemption("game", "A", "tx", 4, 2))
        for ch in ("A", "C"):
            self.assertTrue(self.ledger.apply_resolution("game", ch, .5))
        self.ledger.mark_review("game", "A", "needs receipt")
        self.assertEqual(self.task()["state"], "REDEEMABLE")
        self.assertFalse(self.ledger.confirm_redemption("game", "A", "tx", 2, 1))
        self.assertFalse(self.ledger.confirm_redemption("game", "A", "tx", 4, .5))
        self.assertTrue(self.ledger.confirm_redemption("game", "A", "tx", 4, 2))
        self.reopen()
        self.assertTrue(self.ledger.confirm_redemption("game", "A", "tx", 4, 2))
        self.assertFalse(self.ledger.confirm_redemption("game", "C", "TX", 4, 2))
        self.assertEqual(self.ledger.exposure(), ({("game", "C")}, 0, False, 1.6))
        self.assertEqual((self.task()["settled_shares"], self.task()["settled_payout"]), (4, 2))

    def test_old_unknown_budget_stays_unknown_and_account_recovery_is_isolated(self):
        aid, _ = self.ledger.begin("game", "A", "OPEN", "old", 1)
        bob = AttemptLedger(self.path, "bob")
        self.addCleanup(bob.close_path)
        bob.fill(aid)
        self.assertEqual(self.ledger.rows()[0]["state"], SUBMITTING)
        self.assertEqual(bob.tasks(), [])
        bob.begin("game", "A", "OPEN", "other", 1, reserved_usd=3)
        self.ledger.exit_intent("game", "A", "force")
        self.reopen()
        self.assertEqual(bob.rows()[0]["state"], SUBMITTING)
        self.assertIsNone(bob.exit_intent("game", "A"))
        self.assertEqual(self.ledger.exit_intent("game", "A"), "force")
        self.assertEqual(self.ledger.exposure(), ({("game", "A")}, 0, True, 0))
        self.handoff(action="OPEN", order_id="old")
        self.assertEqual(self.ledger.exposure(), ({("game", "A")}, 0, True, 0))
        self.assertEqual(bob.exposure(), ({("game", "A")}, 3, False, 0))

    def test_acquisition_survives_log_loss_and_repeated_resume(self):
        aid, _ = self.ledger.begin("game", "A", "OPEN", "buy", 1)
        self.ledger.fill(aid, size=3.25, entry=.4)
        self.reopen()
        self.ledger.fill(aid)
        row = self.ledger.open_rows()[0]
        self.assertEqual((row["bought_size"], row["entry_price"]), (3.25, .4))

    def test_buy_receipts_before_handoff_and_legacy_linked_close(self):
        aid, _ = self.ledger.begin("game", "A", "OPEN", "b", 1, reserved_usd=5)
        self.ledger.record_buy_fills("b", [fill("buy1", 3)])
        self.reopen()
        self.assertEqual(self.ledger.record_buy_fills("b", []), [fill("buy1", 3)])
        self.ledger.fill(aid)  # pre-upgrade position with no quantity or log
        self.ledger.begin("game", "A", "CLOSE", "s", 2, position_id=aid)
        self.ledger.record_sell_fills("s", [fill("sell1", .5)])
        self.handoff(action="OPEN", order_id="b", position_id=aid, legacy_sold=1)
        self.assertEqual((self.task()["size"], self.task()["reserved_usd"]), (1.5, 3.5))
        self.ledger.apply_order("game", "A", "b", [fill("buy2", 1)], 4)
        self.reopen()
        self.assertEqual((self.task()["action"], self.task()["order_id"], self.task()["size"]),
                         ("CLOSE", "s", 2.5))
        self.assertFalse(self.ledger.apply_resolution("game", "A", .5))
        self.ledger.apply_order("game", "A", "s", [fill("sell2", 1.25)], 1.75)
        self.assertEqual((self.task()["action"], self.task()["size"]), ("HOLD", 2.25))
        self.assertEqual((self.task()["sold_shares"], self.task()["sell_proceeds"]), (1.75, .875))

    def test_close_just_before_worker_crash_survives_hold_handoff(self):
        self.ledger.begin("game", "A", "CLOSE", "orphaned", 1, position_id="pid")
        self.reopen()
        self.handoff(size=3, entry=.5, position_id="pid")
        self.assertEqual((self.task()["action"], self.task()["order_id"]), ("CLOSE", "orphaned"))
        self.assertFalse(self.ledger.apply_resolution("game", "A", .5))

    def test_signed_order_and_fills_cannot_be_owned_by_two_channels(self):
        self.ledger.begin("game", "A", "OPEN", "signed", 1)
        self.ledger.record_buy_fills("signed", [fill("shared-fill", 2)])
        self.assertEqual(self.ledger.begin("game", "C", "OPEN", "SIGNED", 2), (None, False))
        with self.assertRaises(ValueError):
            self.handoff("C", action="OPEN", order_id="signed", reserved_usd=2)
        self.handoff(action="OPEN", order_id="signed", reserved_usd=2)
        self.ledger.apply_order("game", "A", "signed", [], 2)
        with self.assertRaises(ValueError):
            self.handoff("C", action="OPEN", order_id="SIGNED", reserved_usd=2)
        self.assertEqual(len(self.ledger.tasks()), 1)

    def test_opens_original_schema_and_recovers_only_the_selected_account(self):
        path = os.path.join(os.path.dirname(self.path), "v1.sqlite")
        old = sqlite3.connect(path)
        old.execute("""CREATE TABLE attempts (account TEXT, game_id TEXT, channel TEXT,
            action TEXT, attempt_id TEXT PRIMARY KEY, position_id TEXT, order_id TEXT,
            state TEXT, attempt_no INTEGER, signal_at INTEGER, el_in REAL, updated_at INTEGER,
            UNIQUE(account,game_id,channel,action))""")
        old.executemany("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        [(user, "g", "A", "OPEN", user, "", "order", "SUBMITTING", 1, 1, 1, 1)
                         for user in ("alice", "bob")])
        old.commit()
        old.close()
        new = AttemptLedger(path, "alice")
        self.addCleanup(new.close_path)
        self.assertEqual(new.rows()[0]["state"], "UNKNOWN")
        self.assertEqual(new.rows()[0]["identity_verified"], 0)
        self.assertEqual(new.exposure(), ({("g", "A")}, 0, True, 0))
        bob = AttemptLedger(path, "bob", recover=False)
        self.addCleanup(bob.close_path)
        self.assertEqual(bob.rows()[0]["state"], "SUBMITTING")

    def test_only_reviewed_zero_without_contrary_facts_releases_reservation(self):
        self.handoff(action="OPEN", order_id="buy", reserved_usd=3)
        self.handoff("C", action="CLOSE", order_id="sell", size=4, entry=.4)
        self.assertFalse(self.ledger.confirm_zero("game", "A", "buy", "receipt"))
        for ch in ("A", "C"):
            self.ledger.mark_review("game", ch, "missing evidence")
        self.assertFalse(self.ledger.confirm_zero("game", "A", "other", "receipt"))
        self.assertFalse(self.ledger.confirm_zero("game", "A", "buy", ""))
        self.assertTrue(self.ledger.confirm_zero("game", "A", "buy", "receipt"))
        self.reopen()
        self.assertTrue(self.ledger.confirm_zero("game", "A", "buy", "receipt"))
        self.assertFalse(self.ledger.confirm_zero("game", "C", "sell", "RECEIPT"))
        self.assertTrue(self.ledger.confirm_zero("game", "C", "sell", "second"))
        self.assertEqual((self.task("C")["action"], self.task("C")["size"]), ("HOLD", 4))
        self.assertEqual(self.ledger.exposure(), ({("game", "C")}, 0, False, 1.6))
        self.assertEqual(self.task()["zero_ref"], "receipt")
        self.handoff("D", action="OPEN", order_id="maybe", reserved_usd=3)
        self.ledger.apply_order("game", "D", "maybe", [], 1)
        self.ledger.mark_review("game", "D", "delayed fills")
        self.assertFalse(self.ledger.confirm_zero("game", "D", "maybe", "third"))

    def test_candidate_identity_and_legacy_missing_cost_survive_recovery(self):
        aid, _ = self.ledger.begin("game", "A", "OPEN", "signed", 1, token="shared",
                                   market="m", outcome="home", reserved_usd=3)
        self.ledger.fill(aid)
        self.reopen()
        row = self.ledger.open_rows()[0]
        self.assertEqual((row["token"], row["market"], row["outcome"]), ("shared", "m", "home"))
        self.assertEqual(self.ledger.exposure(), ({("game", "A")}, 0, True, 0))
        self.ledger.fill(aid, size=3, entry=.4)
        self.assertEqual(self.ledger.exposure(), ({("game", "A")}, 0, False, 0))
        with self.assertRaises(ValueError):
            self.ledger.handoff("game", "A", token="different", market="m", outcome="away",
                                reason="force", size=3, entry=.4)
        self.assertEqual(self.ledger.tasks(), [])

    def test_process_kill_after_fill_insert_before_holdings_update(self):
        self.handoff(action="OPEN", order_id="b", reserved_usd=3)
        program = """
import os, sys
from live_ledger import AttemptLedger
ledger = AttemptLedger(sys.argv[1], 'alice')
def cut_power(statement):
    if statement.startswith('UPDATE reconcile_tasks SET'):
        os._exit(91)
ledger.db.set_trace_callback(cut_power)
ledger.apply_order('game', 'A', 'b', [{'id': 'one', 'size': 2, 'price': .5}], 2)
"""
        result = subprocess.run([sys.executable, "-c", program, self.path],
                                cwd=os.path.dirname(os.path.abspath(__file__)), timeout=15)
        self.assertEqual(result.returncode, 91)
        self.reopen()
        self.assertEqual(self.ledger.trade_ids("b"), set())
        self.assertEqual(self.ledger.exposure(), ({("game", "A")}, 3, False, 0))
        self.ledger.apply_order("game", "A", "b", [fill("one", 2)], 2)
        self.assertEqual((self.task()["size"], self.task()["action"]), (2, "HOLD"))

    def test_wallet_case_change_recovers_exposure_and_blocks_second_order(self):
        wallet = "0x" + "Ab" * 20
        original = AttemptLedger(self.path, wallet)
        alternate = AttemptLedger(self.path, wallet.lower(), recover=False)
        self.addCleanup(alternate.close_path)
        original.begin("game", "A", "OPEN", "signed", 1, reserved_usd=3)
        original.close_path()
        self.assertEqual(alternate.account, wallet)
        self.assertEqual(alternate.exposure(), ({("game", "A")}, 3, False, 0))
        self.assertEqual(alternate.begin("game", "A", "OPEN", "duplicate", 1), (None, False))
        restarted = AttemptLedger(self.path, wallet.upper())
        self.addCleanup(restarted.close_path)
        self.assertEqual(restarted.rows()[0]["state"], "UNKNOWN")
        self.assertEqual(restarted.account, wallet)

    def test_conflicting_historical_wallet_names_cannot_hide_an_attempt(self):
        wallet = "0x" + "Ab" * 20
        # Seed records written before namespace selection existed. The
        # account lookup must inspect holdings in every ownership table.
        self.ledger.db.execute("""INSERT INTO attempts
            (account, game_id, channel, action, attempt_id, order_id, state, signal_at, updated_at)
            VALUES (?, 'g', 'A', 'OPEN', 'a', 'signed', 'SUBMITTING', 1, 1)""", (wallet,))
        self.ledger.db.execute("INSERT INTO exit_intents VALUES (?, 'g', 'C', 'force')",
                               (wallet.lower(),))
        self.ledger.db.commit()
        for spelling in (wallet, wallet.lower(), wallet.upper()):
            with self.assertRaisesRegex(ValueError, "multiple account spellings"):
                AttemptLedger(self.path, spelling)
        row = self.ledger.db.execute("SELECT state FROM attempts WHERE attempt_id='a'").fetchone()
        self.assertEqual(row[0], "SUBMITTING")

    def test_existing_wallet_is_discovered_from_queued_holdings_alone(self):
        self.handoff(size=2, entry=.5)
        wallet = "0x" + "Ba" * 20
        self.ledger.db.execute("UPDATE reconcile_tasks SET account=?", (wallet,))
        self.ledger.db.commit()
        restarted = AttemptLedger(self.path, wallet.lower())
        self.addCleanup(restarted.close_path)
        self.assertEqual(restarted.account, wallet)
        self.assertEqual(restarted.exposure(), ({("game", "A")}, 0, False, 1))

    def test_legacy_link_preserves_reservation_original_id_and_evidence(self):
        aid, _ = self.ledger.begin("game", "A", "OPEN", "wrong", 1, reserved_usd=3)
        self.assertEqual(self.ledger.rows()[0]["identity_verified"], 1)
        self.ledger.db.execute("UPDATE attempts SET identity_verified=0 WHERE attempt_id=?", (aid,))
        self.ledger.db.commit()
        self.ledger.finish(aid, "REJECTED_ZERO")
        self.assertEqual(self.ledger.rows()[0]["state"], "SUBMITTING")
        self.handoff(action="OPEN", order_id="wrong")
        self.assertFalse(self.task()["identity_verified"])
        self.assertFalse(self.ledger.apply_order("game", "A", "wrong", [], 0))
        self.ledger.mark_review("game", "A", "no matching venue identity")
        self.assertFalse(self.ledger.confirm_zero("game", "A", "wrong", "absent"))
        self.handoff("C", action="CLOSE", order_id="other", size=1, entry=.5)
        self.assertFalse(self.ledger.link_order("game", "A", "wrong", "other", "ticket"))
        self.assertTrue(self.ledger.link_order("game", "A", "wrong", "venue", "ticket"))
        self.reopen()
        self.assertEqual(self.ledger.rows()[0]["order_id"], "wrong")
        self.assertEqual((self.task()["order_id"], self.task()["initial_order_id"]), ("venue", "wrong"))
        self.assertEqual(self.ledger.exposure()[1], 3.)
        self.assertFalse(self.ledger.apply_order("game", "A", "wrong", [], 0))
        self.ledger.apply_order("game", "A", "venue", [fill("f", 2)], 2)
        self.assertEqual((self.task()["size"], self.task()["action"]), (2., "HOLD"))
        self.assertTrue(self.ledger.link_order("game", "A", "wrong", "venue", "ticket"))
        self.assertFalse(self.ledger.link_order("game", "A", "wrong", "venue", "new-ticket"))

    def test_legacy_open_and_close_keep_separate_verified_identities(self):
        self.ledger.begin("game", "A", "OPEN", "old-buy", 1)
        self.ledger.begin("game", "A", "CLOSE", "old-sell", 2)
        self.ledger.db.execute("UPDATE attempts SET identity_verified=0")
        self.ledger.db.commit()
        self.handoff(action="OPEN", order_id="old-buy", reserved_usd=3)
        self.ledger.mark_review("game", "A", "identify buy")
        self.assertFalse(self.ledger.link_order("game", "A", "old-buy", "old-sell", "proof-b"))
        self.assertTrue(self.ledger.link_order("game", "A", "old-buy", "new-buy", "proof-b"))
        self.ledger.apply_order("game", "A", "new-buy", [fill("f", 2)], 2)
        self.assertEqual((self.task()["action"], self.task()["identity_verified"]), ("CLOSE", 0))
        self.ledger.mark_review("game", "A", "identify sell")
        self.assertFalse(self.ledger.link_order("game", "A", "old-sell", "new-buy", "proof-s"))
        self.assertFalse(self.ledger.link_order("game", "A", "old-sell", "new-sell", "proof-b"))
        self.assertTrue(self.ledger.link_order("game", "A", "old-sell", "old-sell", "proof-s"))
        self.assertTrue(self.ledger.apply_order("game", "A", "old-sell", [], 0))
        self.assertEqual((self.task()["action"], self.task()["size"]), ("HOLD", 2))

    def test_exact_venue_identity_can_reconcile_legacy_order_without_a_link(self):
        self.handoff(action="OPEN", order_id="old", identity_verified=False, reserved_usd=3)
        self.assertFalse(self.ledger.apply_order("game", "A", "unrelated", [], 0, identity_verified=True))
        self.assertTrue(self.ledger.apply_order("game", "A", "old", [], 0, identity_verified=True))
        self.assertEqual((self.task()["state"], self.task()["identity_verified"]), ("DONE", 1))

    def test_partial_confirmed_venue_fill_validates_identity_and_cannot_be_transplanted(self):
        self.handoff(action="OPEN", order_id="original", identity_verified=False, reserved_usd=3)
        self.ledger.apply_order("game", "A", "original", [fill("f1", 1)])
        row = self.task()
        self.assertEqual((row["identity_verified"], row["state"], row["review_reason"]), (1, "PENDING", ""))
        self.assertEqual(self.ledger.exposure(), ({("game", "A")}, 2.5, False, .5))
        self.ledger.mark_review("game", "A", "waiting for terminal size")
        self.assertFalse(self.ledger.link_order("game", "A", "original", "another", "ticket"))
        self.ledger.apply_order("game", "A", "original", [fill("f1", 1)], 1)
        self.assertEqual((self.task()["action"], self.task()["size"]), ("HOLD", 1))

    def test_close_gate_requires_exact_live_parent_and_available_shares(self):
        aid, _ = self.ledger.begin("game", "C", "OPEN", "buy", 1, token="shared", market="m",
                                   outcome="home", reserved_usd=5)
        self.ledger.record_buy_fills("buy", [fill("b", 4)])
        def sell(oid, amount=4, **changes):
            args = dict(position_id=aid, token="shared", market="m", outcome="home", requested_size=amount)
            args.update(changes)
            return self.ledger.begin("game", "C", "CLOSE", oid, 2, **args)
        self.assertEqual(sell("over", 5), (None, False))
        self.assertEqual(sell("wrong", position_id="another"), (None, False))
        first, ok = sell("s1")
        self.assertTrue(ok)
        self.assertEqual(sell("s2"), (None, False))
        self.ledger.record_sell_fills("s1", [fill("s", 4)])
        self.assertTrue(self.ledger.finish_sell_order(first, "s1", 4))
        self.ledger.close(first)
        self.assertEqual(sell("s2"), (None, False))
        self.ledger.record_buy_fills("buy", [fill("late", 2)])
        self.assertTrue(sell("s2", 2)[1])
        self.ledger.close(aid)
        self.assertEqual(sell("closed", 1), (None, False))

    def test_pre_identity_migration_hold_still_accepts_verified_market_resolution(self):
        self.handoff(size=4, entry=.5)
        with self.ledger.db:
            self.ledger.db.execute("UPDATE reconcile_tasks SET identity_verified=0")
        self.assertTrue(self.ledger.apply_resolution("game", "A", 1))
        self.assertEqual(self.task()["state"], "REDEEMABLE")

    def test_shared_wallet_reserves_other_channels_once_across_handoff(self):
        for channel in ("A", "C"):
            oid = "buy-" + channel
            aid, _ = self.ledger.begin("game", channel, "OPEN", oid, 1, token="shared",
                                       market="m", outcome="home", reserved_usd=2)
            self.ledger.record_buy_fills(oid, [fill("b", 4)])
            self.ledger.fill(aid, 4, .5)
        # A different account's holding and another token cannot reserve this wallet.
        other = AttemptLedger(self.path, "bob")
        self.addCleanup(other.close_path)
        other.handoff("elsewhere", "A", token="shared", market="m", outcome="home",
                       reason="settle_hold", size=20, entry=.5)
        self.ledger.handoff("game", "D", token="different", market="m", outcome="away",
                            reason="settle_hold", size=20, entry=.5)
        self.assertEqual(self.ledger.other_token_shares("game", "A", "shared"), 4)
        self.handoff("C", size=4, entry=.5)
        self.assertEqual(self.ledger.other_token_shares("game", "A", "shared"), 4)
        self.reopen()
        self.assertEqual(self.ledger.other_token_shares("game", "A", "shared"), 4)
        self.ledger.apply_resolution("game", "C", 1)
        self.ledger.confirm_redemption("game", "C", "verified-receipt", 4, 4)
        self.assertEqual(self.ledger.other_token_shares("game", "A", "shared"), 0)

    def test_unverified_legacy_claim_still_reserves_sibling_wallet_shares(self):
        aid, _ = self.ledger.begin("game", "C", "OPEN", "legacy", 1, token="shared",
                                   market="m", outcome="home")
        self.ledger.fill(aid, 4, .5)
        with self.ledger.db:
            self.ledger.db.execute("UPDATE attempts SET identity_verified=0 WHERE attempt_id=?", (aid,))
        self.ledger.handoff("game", "C", token="shared", market="m", outcome="home",
                            reason="legacy_log_unverified", action="OPEN", order_id="legacy")
        self.assertEqual(self.task("C")["size"], 0)
        self.assertEqual(self.task("C")["legacy_bought_size"], 4)
        self.assertEqual(self.ledger.other_token_shares("game", "A", "shared"), 4)

    def test_binding_legacy_asset_preserves_identity_and_rejects_conflicts_atomically(self):
        aid, _ = self.ledger.begin("game", "C", "OPEN", "legacy", 1)
        self.ledger.fill(aid, 4, .5)
        with self.ledger.db:
            self.ledger.db.execute("UPDATE attempts SET identity_verified=0 WHERE attempt_id=?", (aid,))
        self.ledger.bind_asset("game", "C", token="shared", market="m", outcome="home")
        row, = self.ledger.rows()
        self.assertEqual((row["token"], row["identity_verified"], row["bought_size"]), ("shared", 0, 4))
        with self.assertRaises(ValueError):
            self.ledger.bind_asset("game", "C", token="another", market="m", outcome="home")
        self.assertEqual(self.ledger.rows()[0], row)


if __name__ == "__main__":
    unittest.main()
