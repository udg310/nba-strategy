"""Exercise the runner-launched board against isolated session logs and account ledgers."""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import team_dashboard as board
from dashboard_quotes import QuoteFeed
from dashboard_sources import Sources
from dashboard_store import HK, stamp
from live_ledger import AttemptLedger


MIDNIGHT = datetime(2026, 11, 17, tzinfo=HK)
NOW = MIDNIGHT + timedelta(minutes=10)


class TeamDashboardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runner = self.root / "data/live_trade_final"
        self.log = self.runner / "2026-11-16/session/events.jsonl"
        self.log.parent.mkdir(parents=True)
        self.db = self.root / "var/live_runner/attempts.db"
        self.books = QuoteFeed(enabled=False)

    def append(self, when, ev, **fields):
        with self.log.open("a", encoding="utf8") as stream:
            stream.write(json.dumps(dict(fields, t=when.timestamp(), ev=ev)) + "\n")

    def buy(self, when, ch="A", asset="111", **extra):
        self.append(when, "buy", gid="g", ch=ch, outcome="Bulls", asset_id=asset,
                    filled_shares=10, entry=.5, **extra)

    def mark(self, asset, price, when):
        self.books.books[asset] = dict(bid=price, ask=None, observed_at=stamp(when), source_at=stamp(when))

    def app(self):
        app = board.create_dashboard(self.runner, quotes=self.books, public=False)
        self.addCleanup(app.close)
        return app

    def state(self, app, when=NOW):
        return board.build_state(self.runner, when, dashboard=app)

    def test_purchase_and_deposit_change_cash_only_and_marks_drive_pnl(self):
        self.append(MIDNIGHT, "start", live=True, balance=100)
        self.buy(MIDNIGHT, fee=0, fee_unit="cash")
        self.append(NOW, "heartbeat", balance=95)
        app = self.app()
        self.state(app)  # subscribes to the new position's public book
        self.mark("111", ".50", NOW)
        state = self.state(app)
        self.assertEqual((state["balance"], state["day_start"], state["market_value"], state["today_pnl"]),
                         (95, 100, 5, 0))
        later = NOW + timedelta(seconds=1)
        self.mark("111", ".60", later)
        self.append(later, "heartbeat", balance=195)
        state = self.state(app, later)
        self.assertEqual((state["balance"], state["market_value"], state["today_pnl"]), (195, 6, 1))
        self.assertEqual(state["today_pnl_metric"], app.stores["live"].head["snapshot"]["summary"]["today"])

    def test_midnight_baseline_survives_restart_and_includes_quiet_overnight_hold(self):
        prior = MIDNIGHT - timedelta(seconds=5)
        self.append(prior, "start", live=True, balance=100)
        self.buy(prior, fee=.1, fee_unit="cash")
        self.buy(prior, ch="C", asset="222", fee=0, fee_unit="cash")
        app = self.app()
        self.state(app, prior)
        self.mark("111", ".55", prior)
        self.mark("222", ".50", prior)
        self.state(app, prior)
        self.books = QuoteFeed(enabled=False)  # simulate a new process
        restarted = self.app()
        self.append(MIDNIGHT, "sell", gid="g", ch="A", outcome="Bulls", asset_id="111",
                    sold_shares=4, exit=.7, fee=.04, fee_unit="cash")
        self.state(restarted)
        self.mark("111", ".60", NOW)
        self.mark("222", ".55", NOW)
        state = self.state(restarted)
        # A: 2.80 - .04 + 6 * .60 - 5.50; C: 10 * (.55 - .50)
        self.assertAlmostEqual(state["today_pnl"], 1.36)
        self.assertEqual(state["today_trades"][0]["kind"], "sell")
        self.assertEqual(state["channels"]["A"]["value"], "0.86")

    def test_estimated_fee_and_missing_quotes_keep_partial_subtotal(self):
        self.append(MIDNIGHT, "start", live=True, balance=100)
        self.buy(MIDNIGHT)
        self.buy(MIDNIGHT, ch="C", asset="222", fee=0, fee_unit="cash")
        app = self.app()
        self.state(app)
        self.mark("111", ".60", NOW)
        state = self.state(app)
        metric = state["today_pnl_metric"]
        self.assertIsNone(state["today_pnl"])
        self.assertEqual((metric["known_subtotal"], metric["covered_count"], metric["total_count"]),
                         ("0.875", 1, 2))
        self.assertTrue(metric["includes_estimated_fee"])
        self.assertIsNone(state["market_value"])

    def test_one_shot_reuses_only_public_bids_valid_at_its_as_of_time(self):
        self.append(MIDNIGHT, "start", live=True, balance=100)
        self.buy(MIDNIGHT, fee=0, fee_unit="cash")
        app = self.app()
        self.state(app)
        self.mark("111", ".60", NOW)
        self.state(app)
        self.assertEqual(board.build_state(self.runner, NOW)["today_pnl"], 1)
        self.assertIsNone(board.build_state(self.runner, NOW + timedelta(seconds=20))["today_pnl"])

    def test_journal_partial_fills_and_replay_preserve_both_receipts(self):
        self.append(MIDNIGHT, "start", live=True, balance=100)
        for fid, qty in (("f1", 4), ("f2", 2), ("f1", 4)):
            self.append(NOW, "buy", gid="g", ch="A", order_id="buy", fill_id=fid, entry=.5,
                        filled_shares=qty, fee=0, fee_unit="cash")
        with self.sources() as src:
            self.assertEqual([(r["fill_id"], r["quantity"]) for r in src.read()["activity"]],
                             [("f1", "4"), ("f2", "2")])

    def sources(self):
        return closing(Sources(runner=self.runner, attempts=self.db))

    def test_durable_fills_cover_crash_before_log_and_use_exchange_match_date(self):
        self.append(MIDNIGHT, "start", live=True, balance=100)
        ledger = AttemptLedger(str(self.db), "wallet")
        self.addCleanup(ledger.close_path)
        aid, _ = ledger.begin("g", "A", "OPEN", "o", MIDNIGHT.timestamp(), token="111",
                              outcome="Bulls", market="m", reserved_usd=4)
        prior = MIDNIGHT - timedelta(seconds=1)
        with patch("live_ledger.time.time", return_value=NOW.timestamp()):
            ledger.record_buy_fills("o", [dict(id="f1", size=4, price=.5, matched_at=int(prior.timestamp())),
                                          dict(id="f2", size=2, price=.6)])
        # Earlier runner wrote one aggregate. Newer runner wrote a duplicate receipt
        # with its actual fee. Neither is an additional purchase.
        self.append(NOW, "buy", gid="g", ch="A", order_id="o", entry=.5, filled_shares=4)
        self.append(NOW, "buy", gid="g", ch="A", order_id="o", fill_id="f1", entry=.5,
                    filled_shares=4, fee=.03, fee_unit="cash")
        with self.sources() as src:
            fills = src.read()["activity"]
            self.assertEqual([(r["fill_id"], r["quantity"], r["position_id"]) for r in fills],
                             [("f1", "4", aid), ("f2", "2", aid)])
            self.assertEqual((fills[0]["fee"], fills[1]["fee_basis"]), ("0.03", "estimated_taker"))
            self.assertEqual((fills[0]["asset_id"], fills[0]["outcome"]), ("111", "Bulls"))
            self.assertEqual(datetime.fromisoformat(fills[0]["time"].replace("Z", "+00:00")), prior)
            self.assertEqual(datetime.fromisoformat(fills[1]["time"].replace("Z", "+00:00")), NOW)

    def test_settlement_uses_confirmed_redemption_once(self):
        self.append(MIDNIGHT, "start", live=True, balance=100)
        self.buy(MIDNIGHT, fee=0, fee_unit="cash", position_id="p", order_id="o")
        ledger = AttemptLedger(str(self.db), "wallet")
        self.addCleanup(ledger.close_path)
        with patch("live_ledger.time.time", return_value=NOW.timestamp()):
            ledger.handoff("g", "A", token="111", market="m", outcome="Bulls", reason="settle_hold",
                           position_id="p", action="HOLD", size=10, entry=.5)
            ledger.apply_resolution("g", "A", 1)
            with self.sources() as src:
                self.assertEqual([r["kind"] for r in src.read()["activity"]], ["buy"])
                ledger.confirm_redemption("g", "A", "receipt", 10, 10)
                self.assertEqual([(r["kind"], r["net_cash"]) for r in src.read()["activity"]],
                                 [("buy", "-5"), ("settlement", "10")])

    def test_verified_legacy_order_link_prevents_counting_an_aggregate_twice(self):
        self.append(MIDNIGHT, "start", live=True, balance=100)
        ledger = AttemptLedger(str(self.db), "wallet")
        self.addCleanup(ledger.close_path)
        aid, _ = ledger.begin("g", "A", "OPEN", "old", MIDNIGHT.timestamp(), token="111",
                              outcome="Bulls", market="m", reserved_usd=5)
        self.buy(NOW, fee=.1, fee_unit="cash", position_id=aid, order_id="old")
        # Materialize the existing operator-attested mapping and its receipts.
        with ledger.db:
            ledger.db.execute("INSERT INTO order_links VALUES ('wallet','g','A','OPEN','old','venue','ref',0)")
            ledger.db.execute("""INSERT INTO order_fills
                (account,game_id,channel,action,order_id,fill_id,size,price,recorded_at)
                VALUES ('wallet','g','A','OPEN','venue','f',10,.5,?)""", (int(NOW.timestamp()),))
        with self.sources() as src:
            rows = src.read()["activity"]
            self.assertEqual([(r["quantity"], r["net_cash"]) for r in rows], [("10", "-5.1")])

    def test_idless_legacy_summary_overlapping_unique_open_does_not_duplicate_basis(self):
        self.append(MIDNIGHT, "start", live=True, balance=100)
        self.buy(NOW, fee=0, fee_unit="cash")
        ledger = AttemptLedger(str(self.db), "wallet")
        self.addCleanup(ledger.close_path)
        ledger.begin("g", "A", "OPEN", "o", MIDNIGHT.timestamp(), token="111",
                     outcome="Bulls", market="m", reserved_usd=5)
        ledger.record_buy_fills("o", [dict(id="f", size=10, price=.5, matched_at=int(NOW.timestamp()))])
        with self.sources() as src:
            rows = src.read()["activity"]
            self.assertEqual([(r["quantity"], r["net_cash"]) for r in rows], [("10", "-5")])

    def test_actual_fee_from_single_fill_summary_keeps_matched_business_date(self):
        self.append(MIDNIGHT, "start", live=True, balance=100)
        self.buy(NOW, order_id="o", fee=.1, fee_unit="cash")
        ledger = AttemptLedger(str(self.db), "wallet")
        self.addCleanup(ledger.close_path)
        ledger.begin("g", "A", "OPEN", "o", MIDNIGHT.timestamp(), token="111",
                     outcome="Bulls", market="m", reserved_usd=5)
        prior = MIDNIGHT - timedelta(seconds=2)
        ledger.record_buy_fills("o", [dict(id="f", size=10, price=.5, matched_at=int(prior.timestamp()))])
        with self.sources() as src:
            (row,) = src.read()["activity"]
            self.assertEqual((row["fill_id"], row["fee"], row["time_kind"]), ("f", "0.1", "matched"))
            self.assertEqual(datetime.fromisoformat(row["time"].replace("Z", "+00:00")), prior)

    def test_old_undated_fills_only_use_date_of_covering_summary(self):
        self.append(MIDNIGHT, "start", live=True, balance=100)
        self.buy(MIDNIGHT - timedelta(seconds=1), order_id="o", fee=.1, fee_unit="cash", live=True)
        ledger = AttemptLedger(str(self.db), "wallet")
        self.addCleanup(ledger.close_path)
        ledger.begin("g", "A", "OPEN", "o", MIDNIGHT.timestamp(), token="111",
                     outcome="Bulls", market="m", reserved_usd=9)
        with ledger.db:
            ledger.db.executemany("""INSERT INTO order_fills
                (account,game_id,channel,action,order_id,fill_id,size,price,recorded_at)
                VALUES ('wallet','g','A','OPEN','o',?,?,.5,?)""",
                                  [("old", 10, None), ("late", 2, int(NOW.timestamp()))])
        with self.sources() as src:
            rows = src.read()["activity"]
            self.assertEqual([(r["quantity"], r["fee"]) for r in rows], [("10", "0.1"), ("2", "0.025")])
            self.assertLess(rows[0]["time"], stamp(MIDNIGHT))
            self.assertEqual(datetime.fromisoformat(rows[1]["time"].replace("Z", "+00:00")), NOW)

    def test_executor_and_runner_receipts_describe_one_purchase_and_redemption(self):
        ledger = AttemptLedger(str(self.db), "wallet")
        self.addCleanup(ledger.close_path)
        aid, _ = ledger.begin("g", "A", "OPEN", "order", MIDNIGHT.timestamp(), token="111",
                              outcome="Bulls", market="m", reserved_usd=5)
        ledger.record_buy_fills("order", [dict(id="one", size=4, price=.5),
                                          dict(id="two", size=6, price=.5)])
        ledger.handoff("g", "A", token="111", market="m", outcome="Bulls", reason="settle_hold",
                       position_id=aid, action="HOLD", size=10, entry=.5)
        ledger.apply_resolution("g", "A", 1)
        ledger.confirm_redemption("g", "A", "cash-receipt", 10, 10)
        path = self.root / "executor.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript(Path(__file__).with_name("executor_schema.sql").read_text())
            db.executescript("""
                INSERT INTO ledger_meta VALUES(1,'wallet','live',100000000);
                INSERT INTO intents VALUES('p','wallet','live','g','A','OPEN','111','m','p','',
                  5000000,500000,1800000000000,1800000010000,'FILLED',0,10000000,5000000,0,
                  1800000003000,1,'a','order');
                INSERT INTO attempts VALUES('a','p',1,'FILLED','order',0,5000000,500000,1800000000200,1800000003000);
                INSERT INTO fills VALUES ('order','one','p','111','BUY',4000000,500000,2000000,0,1800000003000),
                                         ('order','two','p','111','BUY',6000000,500000,3000000,0,1800000004000);
                INSERT INTO settlements VALUES('p','cash-receipt','leg',10000000,10000000,0,'actor','',1800000020000);
            """)
        with closing(Sources(ledger=path, attempts=self.db, runner=self.runner)) as src:
            self.assertEqual([(r["kind"], r["net_cash"]) for r in src.read()["activity"]],
                             [("buy", "-2"), ("buy", "-3"), ("settlement", "10")])


if __name__ == "__main__":
    unittest.main()
