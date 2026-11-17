import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import live_account_report as report
from live_ledger import AttemptLedger


class LiveAccountReportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "attempts.db"

    def run_report(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            report.main(["--account", "account", "--db", str(self.path), *args])
        return json.loads(output.getvalue())

    def test_missing_database_is_never_created(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.run_report()
        self.assertFalse(self.path.exists())

    def test_read_does_not_recover_live_submission(self):
        ledger = AttemptLedger(str(self.path), "account")
        self.addCleanup(ledger.close_path)
        ledger.begin("game", "C", "OPEN", "signed-order", 50)
        self.assertEqual(self.run_report()["pending_tasks"], 0)
        self.assertEqual(ledger.rows()[0]["state"], "SUBMITTING")

    def test_receipt_normalizes_transaction_and_log_index(self):
        tx = "AB" * 32
        self.assertEqual(report.receipt_reference("0x" + tx + ":0007"),
                         "0x" + tx.lower() + ":7")
        for ref in (tx, "0x" + tx, "notes:7", "0x" + tx + ":-1"):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                report.receipt_reference(ref)

    def test_account_only_report_includes_prior_games_and_completed_on_request(self):
        ledger = AttemptLedger(str(self.path), "account")
        other = AttemptLedger(str(self.path), "another")
        try:
            ledger.handoff("older", "C", token="dog", market="condition1", outcome="CHI",
                           reason="force", size=4, entry=0.4)
            ledger.handoff("newer", "A", action="OPEN", order_id="signed", reserved_usd=3,
                           token="dog2", market="condition2", outcome="DAL", reason="unconfirmed_open")
            other.handoff("private", "D", token="fav", market="condition3", outcome="NYK",
                          reason="settle_hold", size=3, entry=0.6)
            ledger.apply_resolution("older", "C", 0)
        finally:
            ledger.close_path()
            other.close_path()
        pending = self.run_report()
        self.assertEqual((pending["pending_tasks"], pending["known_reserved_usd"]), (1, 3))
        self.assertEqual([(t["game_id"], t["status"], t["order_id"]) for t in pending["tasks"]],
                         [("newer", "unresolved_order", "signed")])
        all_rows = self.run_report("--all")
        self.assertEqual(all_rows["pending_tasks"], 1)
        closed, = [t for t in all_rows["tasks"] if t["state"] == "DONE"]
        self.assertEqual((closed["reason"], closed["settled_shares"], closed["status"]),
                         ("force", 4, "settled_zero"))
        self.assertAlmostEqual(closed["settlement_gross_pnl_usd"], -1.60)

    def test_partial_order_evidence_is_visible_without_releasing_reservation(self):
        ledger = AttemptLedger(str(self.path), "account")
        try:
            ledger.handoff("game", "A", action="OPEN", order_id="original", reserved_usd=3,
                           token="fav", market="condition", outcome="NYK", reason="unconfirmed_open")
            fill = {"id": "original-fill", "price": 0.5, "size": 2}
            ledger.apply_order("game", "A", "original", [fill])
            ledger.apply_order("game", "A", "original", [fill])
        finally:
            ledger.close_path()
        row, = self.run_report()["tasks"]
        self.assertEqual((row["status"], row["size"], row["reserved_usd"]), ("unresolved_order", 2, 2))
        self.assertEqual([(f["order_id"], f["fill_id"]) for f in row["recorded_fills"]],
                         [("original", "original-fill")])
        self.assertIsNone(row["settlement_gross_pnl_usd"])

    def test_receipt_booking_requires_lock_exact_token_and_matched_payout(self):
        ledger = AttemptLedger(str(self.path), "account")
        try:
            ledger.handoff("game", "D", token="fav", market="condition", outcome="NYK",
                           reason="settle_hold", size=4, entry=0.6)
            ledger.apply_resolution("game", "D", 1)
            ledger.begin("next", "C", "OPEN", "unrelated", 100)
        finally:
            ledger.close_path()
        receipt = "0x" + "a1" * 32 + ":0"
        argv = ["confirm-redemption", "--game", "game", "--channel", "D", "--receipt", receipt,
                "--shares", "4"]
        acquired = []

        @contextlib.contextmanager
        def fake_lock(account):
            acquired.append(account)
            yield

        with patch.object(report, "account_lock", side_effect=RuntimeError("another executor")), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.run_report(*argv, "--token", "fav", "--payout", "4")
        with patch.object(report, "account_lock", side_effect=fake_lock):
            for token, amount in (("wrong", "4"), ("fav", "2"), ("fav", "nan")):
                with self.subTest(token=token, amount=amount), \
                        contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    self.run_report(*argv, "--token", token, "--payout", amount)
            self.assertEqual(self.run_report()["tasks"][0]["status"], "redeemable")
            self.assertEqual(self.run_report(*argv, "--token", "fav", "--payout", "4")["status"], "redeemed")
            self.run_report(*argv, "--token", "fav", "--payout", "4")
        self.assertTrue(acquired)
        self.assertEqual(set(acquired), {"account"})
        self.assertEqual(self.run_report()["pending_tasks"], 0)
        row, = self.run_report("--all")["tasks"]
        self.assertEqual((row["redemption_ref"], row["settled_payout"]), (receipt, 4))
        self.assertAlmostEqual(row["settlement_gross_pnl_usd"], 1.60)
        ledger = AttemptLedger(str(self.path), "account", recover=False)
        try:
            self.assertEqual(ledger.rows("next")[0]["state"], "SUBMITTING")
        finally:
            ledger.close_path()

    def test_manual_zero_requires_review_exact_identity_and_venue_evidence(self):
        ledger = AttemptLedger(str(self.path), "account")
        self.addCleanup(ledger.close_path)
        ledger.handoff("game", "C", action="OPEN", order_id="signed", reserved_usd=3,
                       token="dog", market="condition", outcome="CHI", reason="unconfirmed_open")
        cmd = ["confirm-zero", "--game", "game", "--channel", "C", "--token", "dog"]
        proof = ["--order-id", "signed", "--reference", "venue-case:Q47"]
        with patch.object(report, "account_lock", side_effect=lambda account: contextlib.nullcontext()):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.run_report(*cmd, *proof)
            ledger.mark_review("game", "C", "awaiting signed order evidence")
            for args in (["--account", "ACCOUNT", *cmd, *proof], [*cmd, *proof, "--token", "other"],
                         [*cmd, *proof, "--order-id", "different"], [*cmd, *proof, "--reference", "404"]):
                with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    self.run_report(*args)
            self.assertEqual(self.run_report()["known_reserved_usd"], 3)
            self.run_report(*cmd, *proof)
            self.run_report(*cmd, *proof)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.run_report(*cmd, *proof, "--reference", "venue-case:Q99")
            row, = self.run_report("--all")["tasks"]
            self.assertEqual((row["zero_order_id"], row["zero_ref"], row["status"], row["reserved_usd"]),
                             ("signed", "venue-case:Q47", "zero_fill", 0))

    def test_link_reviewed_legacy_order_requires_evidence_and_keeps_both_ids(self):
        ledger = AttemptLedger(str(self.path), "account")
        self.addCleanup(ledger.close_path)
        local_id, venue_id = "0x" + "3a" * 32, "0x" + "4b" * 32
        ledger.handoff("game", "A", action="OPEN", order_id=local_id, reserved_usd=2,
                       token="dog", market="condition", outcome="CHI", reason="unconfirmed_open",
                       identity_verified=False)
        ledger.mark_review("game", "A", "legacy order identity unverified")
        task_args = ["--game", "game", "--channel", "A", "--token", "dog"]
        cmd = ["link-order", *task_args, "--local-id", local_id, "--venue-id", venue_id,
               "--reference", "venue-record:Q81"]
        with patch.object(report, "account_lock", return_value=contextlib.nullcontext()):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.run_report("confirm-zero", *task_args, "--order-id", local_id, "--reference", "venue-case:Q80")
            for option, invalid in (("--local-id", "wrong"), ("--venue-id", "partial"), ("--reference", "similar time")):
                with self.subTest(option=option), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    self.run_report(*cmd, option, invalid)
            before, = self.run_report()["tasks"]
            self.assertEqual((before["order_identity"], before["venue_order_id"]), ("unverified_legacy", None))
            self.run_report(*cmd)
            self.run_report(*cmd)
            after, = self.run_report()["tasks"]
            self.assertEqual((after["initial_order_id"], after["venue_order_id"], after["reserved_usd"]),
                             (local_id, venue_id, 2))
            link, = after["order_links"]
            self.assertEqual((link["old_id"], link["venue_id"], link["reference"]),
                             (local_id, venue_id, "venue-record:Q81"))
