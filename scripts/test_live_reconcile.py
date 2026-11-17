"""Offline sweeps against temporary account ledgers and scripted venue reads."""
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from live_ledger import AttemptLedger
import live_reconcile as recon


def trade(trade_id, order_id, size, **changes):
    row = dict(id=trade_id, taker_order_id=order_id, status="CONFIRMED",
               side="BUY", size=size, price="0.4", asset_id="t", market="m")
    return dict(row, **changes)


def order(**changes):
    row = dict(id="signed", asset_id="t", market="m", side="BUY", status="CANCELED",
               size_matched="0", associate_trades=[])
    return dict(row, **changes)


class Venue:
    def __init__(self):
        self.orders = {}
        self.trades = []
        self.markets = {}

    def get_order(self, oid):
        return self.orders[oid]

    def get_trades(self, params):
        if params.asset_id != "t":
            raise AssertionError("expected account trades scoped to the original token")
        return self.trades

    def get_market(self, condition):
        return self.markets[condition]


class SweepTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "ledger.db")
        self.ledger = AttemptLedger(self.path, "fund")
        self.addCleanup(lambda: self.ledger.close_path())
        self.venue = Venue()
        self.logs = []
        self.gamma = self.enter_patch(patch.object(recon, "http_json", return_value=[]))
        # The small dependency-free runner suite can exercise the same venue
        # boundary as the SDK venv (TradeParams is a plain SDK dataclass).
        try:
            from py_clob_client_v2.clob_types import TradeParams
        except ImportError:
            self.enter_patch(patch.dict("sys.modules", {"py_clob_client_v2.clob_types":
                                       SimpleNamespace(TradeParams=SimpleNamespace)}))

    def enter_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def handoff(self, channel, **params):
        self.ledger.handoff("g", channel, token="t", market="m", outcome="home",
                            reason="force_unfilled", **params)

    def poll(self, now=None):
        recon.reconcile_account(self.ledger, self.venue, self.logs.append, now)
        return {row["channel"]: row for row in self.ledger.tasks(pending_only=False)}

    def test_reviewed_order_restarts_and_applies_late_batches_once_by_signed_id(self):
        self.handoff("A", action="OPEN", order_id="signed", reserved_usd=5)
        self.handoff("C", action="OPEN", order_id="other", reserved_usd=6)
        future = time.time() + 86500
        self.assertEqual(self.poll(future)["A"]["state"], "REVIEW")
        events = len(self.logs)
        self.poll(future)
        self.assertEqual(len(self.logs), events)
        # A 404 on the order endpoint can coexist with a confirmed account trade.
        self.venue.trades = [trade("one", "signed", "2"), trade("one", "signed", "2"),
                             trade("pending", "signed", "1", status="MINED"),
                             trade("wrong-token", "signed", "2", asset_id="z"),
                             trade("wrong-side", "signed", "2", side="SELL"),
                             trade("someone", "third", "3")]
        rows = self.poll()
        self.assertAlmostEqual(rows["A"]["size"], 2)
        self.assertAlmostEqual(rows["C"]["size"], 0)
        self.assertAlmostEqual(rows["C"]["reserved_usd"], 6)
        self.ledger.close_path()
        self.ledger = AttemptLedger(self.path, "fund")
        self.venue.trades = [trade("two", "signed", "1")]
        self.venue.orders["signed"] = order(status="MATCHED", size_matched="3",
                                            associate_trades=["one", "two"])
        rows = self.poll()
        self.assertEqual(rows["A"]["action"], "HOLD")
        self.assertAlmostEqual(rows["A"]["size"], 3)
        self.assertAlmostEqual(rows["A"]["reserved_usd"], 0)
        self.assertAlmostEqual(self.poll()["A"]["size"], 3)

    def test_only_proven_terminal_zero_releases_reserve(self):
        self.handoff("A", action="OPEN", order_id="signed", reserved_usd=5)
        for invalid in [order(id="wrong"), order(market="other"), order(asset_id="other"),
                        order(side="SELL"), order(status="LIVE"), order(status="MATCHED"),
                        order(associate_trades=["unconfirmed"]), order(size_matched="NaN"),
                        order(size_matched="-1"), order(associate_trades=None)]:
            with self.subTest(invalid=invalid):
                self.venue.orders["signed"] = invalid
                row = self.poll()["A"]
                self.assertEqual(row["action"], "OPEN")
                self.assertAlmostEqual(row["reserved_usd"], 5)
        terminal = order(status="CANCELED_MARKET_RESOLVED")
        del terminal["associate_trades"]  # Deprecated field in the current order API.
        self.venue.orders["signed"] = terminal
        self.assertEqual(self.poll()["A"]["state"], "DONE")

    def test_legacy_zero_identity_is_verified_through_the_actual_sweep(self):
        self.handoff("A", action="OPEN", order_id="signed", reserved_usd=5, identity_verified=False)
        for invalid in (None, order(id="other"), order(side="SELL"), order(market="other"),
                        order(status="LIVE"),
                        order(associate_trades=["pending"])):
            self.venue.orders["signed"] = invalid
            self.assertEqual(self.poll()["A"]["reserved_usd"], 5)
        self.venue.orders["signed"] = order()
        for _ in range(3):
            row = self.poll()["A"]
            self.assertEqual((row["state"], row["reserved_usd"]), ("DONE", 0))
        self.handoff("C", action="CLOSE", order_id="sell", size=4, entry=.5, identity_verified=False)
        self.venue.orders["sell"] = order(id="sell", side="SELL")
        row = self.poll()["C"]
        self.assertEqual((row["action"], row["size"], row["reserved_usd"]), ("HOLD", 4, 0))
        self.handoff("D", action="OPEN", order_id="conflict", reserved_usd=5, identity_verified=False)
        self.venue.orders["conflict"] = order(id="conflict", size_matched="1")
        self.poll()
        self.venue.orders["conflict"] = order(id="conflict", size_matched="0")
        self.assertEqual(self.poll()["D"]["reserved_usd"], 5)

    def test_partial_close_cannot_consume_other_channels_shares(self):
        self.handoff("A", action="CLOSE", order_id="signed", size=4, entry=.3)
        self.handoff("C", size=4, entry=.6)
        self.venue.orders["signed"] = order(status="CANCELED", side="SELL",
                                            size_matched="1", associate_trades=["one", "missing"])
        self.venue.trades = [trade("one", "signed", "1", side="SELL")]
        rows = self.poll()
        self.assertEqual(rows["A"]["action"], "CLOSE")
        self.assertAlmostEqual(rows["A"]["size"], 3)
        self.venue.orders["signed"]["associate_trades"] = ["one"]
        rows = self.poll()
        self.assertEqual(rows["A"]["action"], "HOLD")
        self.assertAlmostEqual(rows["A"]["size"], 3)
        self.assertAlmostEqual(rows["C"]["size"], 4)

    def test_closed_binary_winner_certifies_value_without_crediting_cash(self):
        for channel in ("A", "C"):
            self.handoff(channel, size=4, entry=.4)
        market = dict(condition_id="m", closed=True, tokens=[
            dict(token_id="t", outcome="home", price=1, winner=True),
            dict(token_id="s", outcome="away", price=0, winner=False)])
        self.venue.markets["m"] = market
        rows = self.poll()
        self.assertTrue(all(r["state"] == "REDEEMABLE" for r in rows.values()))
        self.assertTrue(all(r["payout_price"] == 1 for r in rows.values()))
        rows = self.poll(time.time() + 86500)
        self.assertEqual(rows["A"]["state"], "REDEEMABLE")
        self.assertTrue(rows["A"]["review_reason"])

    def test_only_final_market_and_matching_token_can_certify_split_or_loss(self):
        self.handoff("A", size=4, entry=.4)
        self.venue.markets["m"] = dict(condition_id="m", closed=True, tokens=[
            dict(token_id="t", outcome="home", price=0, winner=False),
            dict(token_id="s", outcome="away", price=1, winner=False)])
        gamma = dict(closed=True, conditionId="m", umaResolutionStatus="resolved",
                     clobTokenIds='["s","t"]', outcomes='["away","home"]',
                     outcomePrices='["0.5","0.5"]')
        for invalid in [dict(gamma, closed=False), dict(gamma, conditionId="other"),
                        dict(gamma, umaResolutionStatus="proposed"),
                        dict(gamma, clobTokenIds='["s","z"]'),
                        dict(gamma, outcomes='["home","away"]'),
                        dict(gamma, outcomePrices='["0","0"]')]:
            self.gamma.return_value = [invalid]
            self.assertEqual(self.poll()["A"]["state"], "PENDING")
        self.gamma.return_value = [gamma]
        self.assertEqual(self.poll()["A"]["payout_price"], .5)
        self.handoff("C", size=2, entry=.3)
        self.venue.markets["m"]["tokens"][1]["winner"] = True
        self.assertEqual(self.poll()["C"]["state"], "DONE")

    def test_failure_on_one_task_does_not_hide_another_order(self):
        self.handoff("A", size=1, entry=.2)
        self.handoff("C", action="OPEN", order_id="signed", reserved_usd=5)
        self.venue.orders["signed"] = order()
        self.gamma.side_effect = TimeoutError()
        rows = self.poll(time.time() + 86500)
        self.assertEqual(rows["A"]["state"], "REVIEW")
        self.assertEqual(rows["C"]["state"], "DONE")

    def test_failed_review_write_still_checks_the_following_task(self):
        self.handoff("A", size=1, entry=.2)
        self.handoff("C", action="OPEN", order_id="signed", reserved_usd=5)
        self.venue.orders["signed"] = order()
        with patch.object(self.ledger, "mark_review", side_effect=sqlite3.OperationalError("failed write")):
            with self.assertRaises(sqlite3.OperationalError):
                self.poll(time.time() + 86500)
        rows = {r["channel"]: r for r in self.ledger.tasks(pending_only=False)}
        self.assertEqual(rows["A"]["state"], "PENDING")
        self.assertEqual(rows["C"]["state"], "DONE")

    def test_shutdown_during_sweep_defers_remaining_rows_until_next_start(self):
        for channel in ("A", "C"):
            self.handoff(channel, action="OPEN", order_id=channel, reserved_usd=5)
            self.venue.orders[channel] = order(id=channel)
        stopping = threading.Event()
        recon.reconcile_account(self.ledger, self.venue, lambda _: stopping.set(), stop_event=stopping)
        self.assertEqual(len(self.ledger.tasks(pending_only=True)), 1)
        self.assertTrue(all(r["state"] == "DONE" for r in self.poll().values()))

    def test_trade_read_failure_or_unconfirmed_match_prevents_zero_release(self):
        for trades in (False, None, [trade("t1", "signed", "1", status="MINED")]):
            with self.subTest(trades=trades):
                self.assertEqual(recon.parse_order_evidence(order(), trades, "signed", "t", "BUY", "m"),
                                 ([], None))


if __name__ == "__main__":
    unittest.main()
