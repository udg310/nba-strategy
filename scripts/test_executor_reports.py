"""Report revisions, durable fill observations and audited review workflows."""

import json
import tempfile
import unittest
from pathlib import Path

from executor_ledger import Conflict, Invalid, Ledger
from executor_reports import Reports


class ReportsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.ledger = Ledger(Path(directory.name, "ledger.db"), "local", "paper", 10_000_000, 2)
        self.addCleanup(self.ledger.close)
        self.reports = Reports(self.ledger)
        self.fill = dict(id="leg", order_id="venue", game_id="game", asset="token",
                         side="BUY", price=500_000, quantity=4_000_000, time=5, confirmed=True)

    def route(self, method, path, body=None):
        status, data = self.reports.dispatch(method, path.split("/"), body)
        self.assertEqual(status, 200)
        return data

    def claim(self):
        req = dict(account="local", environment="paper", game_id="game", channel="A",
                   action="OPEN", market="market", asset="token", amount=3_000_000,
                   limit_price=500_000, signal_at=1, deadline=10000)
        intent, _ = self.ledger.create(req, 1)
        claimed, fresh = self.ledger.claim(intent["id"], 2)
        self.assertTrue(fresh)
        return claimed

    def review(self, operation, **fields):
        return self.route("POST", "v1/review/resolve", dict(
            actor="operator", evidence="verified account receipt", operation=operation, **fields))

    def test_late_fill_revises_finished_game_exactly_once(self):
        i = self.claim()
        self.ledger.apply(i["attempt_id"], dict(state="FINAL", order_id="venue",
                          total_filled=self.fill["quantity"], fills=[self.fill]), 5)
        self.reports.observe([self.fill], "p1")
        report = self.route("POST", "v1/games/game/finish")
        self.assertEqual((report["status"], report["revision"]), ("MATCHED", 1))
        detail = self.route("GET", "v1/order-intents/" + i["id"])
        self.assertEqual(detail["first_fill_at"], 5)
        self.assertEqual(detail["attempts"][0]["order_id"], "venue")
        trace = self.route("GET", "v1/orders/venue/trace")
        self.assertEqual(trace["events"], detail["timeline"])
        second = dict(self.fill, order_id="late", quantity=6_000_000)
        self.reports.observe([second, second], "p2")
        self.reports.refresh()
        self.assertIn("reports_with_difference=1", self.reports.alert_summary(6))
        for _ in range(2):
            report = self.route("GET", "v1/games/game/report")
            self.assertEqual((report["revision"], report["status"]), (2, "PENDING"))
            self.assertEqual((report["venue_buy_quantity"], report["ledger_buy_quantity"],
                             report["buy_difference"]), (10_000_000, 4_000_000, 6_000_000))
            self.assertEqual(report["unattributed"][0]["order_id"], "late")
        self.review("manual", order_id="late")
        self.reports.refresh()
        revisions = self.route("GET", "v1/games/game/report/revisions")
        self.assertEqual([r["revision"] for r in revisions], [1, 2, 3])
        self.assertEqual(revisions[-1]["report"]["status"], "MATCHED")
        self.assertEqual(self.reports.alert_summary(6), "")

    def test_entire_venue_page_rolls_back_on_inconsistency(self):
        self.route("POST", "v1/games/game/finish")
        self.reports.observe([dict(self.fill, confirmed=False)], "p1")
        self.reports.observe([dict(self.fill, fee=10)], "p2")
        self.reports.observe([dict(self.fill, confirmed=False)], "p3")
        with self.assertRaises(Conflict):
            self.reports.observe([dict(self.fill, order_id="new"), dict(self.fill, price=5)], "bad")
        self.assertEqual(self.reports.cursor(), "p3")
        fills = self.route("GET", "v1/review/fills")
        self.assertEqual([(f["order_id"], f["fee"], f["confirmed"]) for f in fills], [("venue", 10, 1)])

    def test_first_observation_and_existing_go_report(self):
        report = self.route("POST", "v1/games/game/finish")
        self.assertEqual(report["status"], "PENDING")
        report.pop("revision")
        with self.ledger.tx() as db:
            db.execute("UPDATE game_reports SET body=?", (json.dumps(report, indent=2),))
        self.assertEqual(self.route("GET", "v1/games/game/report")["revision"], 1)
        self.reports.observe([], "p1")
        self.reports.refresh()
        revisions = self.route("GET", "v1/games/game/report/revisions")
        self.assertEqual([r["report"]["status"] for r in revisions], ["PENDING", "MATCHED"])

    def test_operator_import_does_not_release_unknown_remainder(self):
        i = self.claim()
        self.ledger.record_submit(i["attempt_id"], dict(state="UNKNOWN"), 3)
        fill = dict(self.fill, quantity=2_000_000)
        self.reports.observe([fill], "cursor")
        fields = dict(order_id="venue", attempt_id=i["attempt_id"])
        with self.assertRaises(Conflict):
            self.review("zero", **fields)
        self.review("bind", **fields)
        self.review("bind", **fields)
        for _ in range(2):
            self.review("import", fill_id="leg", **fields)
        row = self.ledger.get(i["id"])
        self.assertEqual((row["filled"], row["reserved"], row["state"]), (2_000_000, 2_000_000, "PARTIAL"))
        actions = self.route("GET", "v1/review/actions")
        self.assertEqual([a["operation"] for a in actions], ["bind", "import", "import"])
        self.assertTrue(all(a["actor"] == "operator" and a["evidence"] for a in actions))

    def test_binding_an_untagged_fill_updates_other_games_with_the_same_asset(self):
        i = self.claim()
        second, _ = self.ledger.create(dict(account="local", environment="paper", game_id="other",
                channel="A", action="OPEN", market="market", asset="token", amount=1_000_000,
                limit_price=500_000, signal_at=1, deadline=10_000), 2)
        self.assertNotEqual(i["id"], second["id"])
        self.ledger.record_submit(i["attempt_id"], dict(state="UNKNOWN"), 3)
        self.reports.observe([dict(self.fill, game_id="")], "p")
        report = self.route("POST", "v1/games/other/finish")
        self.assertEqual(len(report["unattributed"]), 1)
        self.review("bind", order_id="venue", attempt_id=i["attempt_id"])
        self.reports.refresh()
        revisions = self.route("GET", "v1/games/other/report/revisions")
        self.assertEqual(len(revisions), 2)
        self.assertEqual(revisions[-1]["report"]["venue_trades"], [])

    def test_audited_zero_without_contradictory_evidence(self):
        i = self.claim()
        self.ledger.record_submit(i["attempt_id"], dict(state="UNKNOWN"), 3)
        self.assertIn("unknown for 5m", self.reports.alert_summary(300_002))
        self.reports.duplicates = 3
        self.reports.conflicts = 1
        metrics = self.route("GET", "v1/metrics")
        self.assertEqual((metrics["duplicate_requests"], metrics["conflicting_requests"],
                          metrics["unknown_attempts"], metrics["review_over_5m"]), (3, 1, 1, 1))
        self.review("zero", attempt_id=i["attempt_id"])
        self.assertEqual(self.ledger.get(i["id"])["state"], "REJECTED_ZERO")
        self.assertEqual(self.route("GET", "v1/account")["available_cash"], 10_000_000)
        self.assertEqual(self.route("POST", "v1/account/pause-open"), {"open_paused": True})
        self.assertEqual(self.route("POST", "v1/account/resume-open"), {"open_paused": False})

    def test_decision_idempotency_and_existing_serialized_json(self):
        decision = dict(game_id="game", source_id="d", at=1, channel="D", version="v",
                        signal_id="s", result="SKIP", reason="band", inputs={"price": 0.2})
        with self.ledger.tx() as db:
            db.execute("INSERT INTO decisions VALUES(?,?,?,?)", ("game", "d", 1, json.dumps(decision)))
        self.route("POST", "v1/decisions", dict(decision, intent_id=""))
        self.assertEqual(self.route("GET", "v1/games/game/decisions"), [decision])
        with self.assertRaises(Conflict):
            self.route("POST", "v1/decisions", dict(decision, reason="different"))
        with self.assertRaises(Invalid):
            self.route("POST", "v1/decisions", dict(decision, inputs=None))
        self.assertEqual(self.route("GET", "v1/games"), ["game"])


if __name__ == "__main__":
    unittest.main()
