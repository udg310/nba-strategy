"""Read-only source adapter regression tests, using the repository's actual schemas."""

import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from dashboard_sources import Sources

NOW = 1_800_000_000


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "runner" / "day" / "session" / "events.jsonl"
        self.log.parent.mkdir(parents=True)

    def append(self, *rows):
        with self.log.open("a", encoding="utf8") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")

    def sources(self, **kw):
        src = Sources(**kw)
        self.addCleanup(src.close)
        return src

    def test_runner_sessions_partial_lines_and_aggregate(self):
        self.append(
            dict(ev="start", live=True, t=NOW - 1, outdir="secret"),
            dict(
                ev="resolve",
                gid="g",
                outcomes=["Bulls", "Hawks"],
                tokens=["124", "125"],
                slug="nba-chi-atl-2027-01-15",
                t=NOW,
            ),
            dict(
                ev="buy",
                ch="A",
                gid="g",
                filled_shares=10,
                entry=0.4,
                outcome="Bulls",
                usd=4,
                fills=1,
                t=NOW + 1,
            ),
            dict(ev="CONFIRMED_BUY", gid="g", ch="A", t=NOW + 2),
        )
        src = self.sources(runner=self.log.parents[2])
        first = src.read()
        (buy,) = first["activity"]
        self.assertEqual((buy["quantity"], buy["asset_id"], buy["time_kind"]), ("10", "124", "recorded"))
        self.assertEqual((buy["fee"], buy["fee_basis"], buy["net_cash"]), ("0.12", "estimated_taker", "-4.12"))
        self.assertNotIn("secret", json.dumps(first))
        self.log = self.log.parents[1] / "next" / self.log.name
        self.log.parent.mkdir()
        self.append(
            dict(ev="start", live=True, t=NOW + 4),
            dict(ev="resume_position", gid="g", ch="A", outcome="Bulls", size=10, t=NOW + 5),
        )
        part = json.dumps(
            dict(ev="sell", gid="g", ch="A", outcome="Bulls", sold_shares=4, exit=0.6, t=NOW + 6)
        )
        with self.log.open("a") as stream:
            stream.write(part[:10])
        self.assertEqual(src.read()["activity"], first["activity"])
        with self.log.open("a") as stream:
            stream.write(part[10:] + "\n")
        self.append(dict(ev="settle_hold", gid="g", ch="A", t=NOW + 7))
        snap = src.read()
        self.assertEqual({a["position_id"] for a in snap["activity"]}, {buy["position_id"]})
        self.assertEqual(len(snap["activity"]), 2)
        sell = next(a for a in snap["activity"] if a["kind"] == "sell")
        self.assertEqual((sell["fee"], sell["net_cash"]), ("0.048", "2.352"))
        self.assertTrue(snap["meta"][buy["position_id"]]["pending_settlement"])
        self.assertFalse(self.sources(env="paper", runner=self.log.parents[2]).read()["activity"])

    def test_recorded_fees_maker_fills_and_unconfirmed_rows(self):
        trade = dict(ev="buy", ch="A", filled_shares=10, entry=0.4, usd=4, t=NOW)
        self.append(dict(ev="start", live=True, t=NOW - 1),
                    dict(trade, gid="zero", fee=0, fee_unit="cash"),
                    dict(trade, gid="maker", liquidity_role="maker"),
                    dict(trade, gid="shares", fee="0.2", fee_unit="shares"),
                    dict(trade, gid="unconfirmed", fills=0),
                    dict(trade, gid="no-price", entry=None),
                    dict(trade, gid="tiny", filled_shares="0.000001"))
        data = {r["game_id"]: r for r in self.sources(runner=self.log).read()["activity"]}
        self.assertEqual((data["zero"]["fee"], data["zero"]["net_cash"]), ("0", "-4"))
        self.assertNotIn("fee_basis", data["zero"])
        self.assertEqual((data["maker"]["fee"], data["maker"]["fee_basis"]), ("0", "calculated_maker"))
        self.assertEqual((data["shares"]["fee_unit"], data["shares"]["net_cash"]), ("shares", "-4"))
        self.assertEqual(data["tiny"]["fee"], "0")
        for gid in ("unconfirmed", "no-price"):
            self.assertIsNone(data[gid]["fee"])
            self.assertIsNone(data[gid]["net_cash"])

    def make_db(self):
        path = self.root / "executor.db"
        db = sqlite3.connect(path)
        db.executescript(Path(__file__).with_name("executor_schema.sql").read_text())
        db.executescript(
            """
            INSERT INTO ledger_meta VALUES(1,'account-1','live',100000000);
            INSERT INTO intents VALUES('p','account-1','live','g','A','OPEN','124','market','p','',
                4000000,400000,1800000000000,1800000010000,'FILLED',0,10000000,4000000,100000,
                1800000003000,1,'a','o');
            INSERT INTO attempts VALUES('a','p',1,'FILLED','o',0,4000000,400000,1800000000200,1800000003000);
            INSERT INTO events VALUES(1,'p','a',1800000003000,'FILLED','confirmed fill f');
            INSERT INTO fills VALUES('o','f','p','124','BUY',10000000,400000,4000000,100000,1800000002000);
            CREATE TABLE external_fills(order_id,id,asset,side,quantity,price,notional,fee,time,game_id,confirmed,disposition);
            INSERT INTO external_fills SELECT order_id,id,asset,side,quantity,price,cost,fee,time,'g',1,'' FROM fills;
        """
        )
        db.commit()
        db.close()
        return path

    def test_precedence_exact_order_and_scope(self):
        path = self.make_db()
        self.append(
            dict(ev="start", live=True, t=NOW - 1),
            dict(ev="buy", gid="g", ch="A", order_id="o", filled_shares=10, entry=0.4, t=NOW + 3),
            dict(ev="sell", gid="g", ch="A", sold_shares=4, exit=0.6, t=NOW + 6),
        )
        src = self.sources(ledger=path, runner=self.log, account="account-1")
        snap = src.read()
        self.assertEqual(len(snap["activity"]), 2)
        fills = {a["kind"]: a for a in snap["activity"]}
        self.assertEqual(fills["buy"]["net_cash"], "-4.1")
        self.assertEqual(fills["buy"]["time_kind"], "chain_confirmed")
        self.assertEqual(fills["sell"]["position_id"], fills["buy"]["position_id"])
        self.assertFalse(self.sources(ledger=path, account="other").read()["activity"])
        self.assertFalse(self.sources(ledger=path, env="paper").read()["activity"])

    def test_manual_and_unattributed_never_double_count(self):
        path = self.make_db()
        self.append(
            dict(ev="start", live=True, t=NOW - 1),
            dict(
                ev="buy", gid="other", ch="C", order_id="outside", filled_shares=1, entry=0.4, t=NOW + 3
            ),
        )
        src = self.sources(ledger=path, runner=self.log)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "INSERT INTO external_fills VALUES('outside','ff','124','BUY',1000000,400000,400000,0,1800000000000,'other',1,'')"
            )
        snap = src.read()
        self.assertEqual(len(snap["activity"]), 2)
        (external,) = [r for r in snap["activity"] if r["order_id"] == "outside"]
        self.assertIsNone(external["position_id"])
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE external_fills SET disposition='manual' WHERE order_id='outside'")
        self.assertEqual(len(src.read()["activity"]), 1)

    def test_origin_when_archive_empty_or_completely_matched(self):
        path = self.make_db()
        self.append(
            dict(ev="start", live=False, t=NOW - 1),
            dict(ev="buy", gid="g", ch="A", filled_shares=42, entry=0.5, t=NOW + 1),
        )
        src = self.sources(ledger=path, runner=self.log)
        self.assertTrue(src.read()["known_origin"])
        self.append(
            dict(ev="start", live=True, t=NOW + 3),
            dict(ev="buy", gid="g", ch="A", order_id="o", filled_shares=10, entry=0.4, t=NOW + 4),
        )
        self.assertTrue(src.read()["known_origin"])
        self.append(
            dict(ev="buy", gid="another", ch="A", order_id="other", filled_shares=1, entry=0.4, t=NOW + 5)
        )
        self.assertFalse(src.read()["known_origin"])

    def test_rotation_mapping_and_order_replay(self):
        self.append(
            dict(ev="start", live=True, t=NOW),
            dict(
                ev="buy",
                gid="401",
                ch="C",
                outcome="Bulls",
                order_id="oo",
                entry=0.4,
                filled_shares=2,
                t=NOW + 1,
            ),
        )
        journal = self.root / "public" / "session" / "events-00003.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text(
            json.dumps(
                dict(
                    ev="mapping",
                    gid="401",
                    outcomes=["Bulls", "Hawks"],
                    tokens=["9", "8"],
                    recorded_utc="2026-01-01T00:00:00Z",
                )
            )
            + "\n",
            encoding="utf8",
        )
        src = self.sources(runner=self.log.parent, market_journal=self.root / "public")
        first = src.read()["activity"][0]
        self.assertEqual(first["asset_id"], "9")
        self.log.rename(self.log.with_suffix(".jsonl.1"))
        self.append(
            dict(
                ev="buy",
                gid="401",
                ch="C",
                outcome="Bulls",
                order_id="oo",
                entry=0.4,
                filled_shares=2,
                t=NOW + 2,
            ),
            dict(ev="sell", gid="401", ch="C", outcome="Bulls", exit=0.6, sold_shares=1, t=NOW + 3),
        )
        result = src.read()
        self.assertEqual(result["activity"][0], first)
        self.assertEqual(len(result["activity"]), 2)
        self.assertEqual(
            result["activity"],
            self.sources(runner=self.log.parent, market_journal=self.root / "public").read()["activity"],
        )

    def test_executor_zero_settlement_and_partial_reservations(self):
        path = self.make_db()
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "INSERT INTO intents SELECT 'c',account,environment,game_id,channel,'CLOSE',asset,market,id,'',10000000,600000,signal_at+4000,deadline,'PARTIAL',6000000,4000000,2400000,40000,last_check+5000,1,'close-a','close-o' FROM intents WHERE id='p'"
            )
            db.execute(
                "INSERT INTO fills VALUES('close-o','c-f','c','124','SELL',4000000,600000,2400000,40000,1800000010000)"
            )
        src = self.sources(ledger=path)
        self.assertEqual(src.read()["meta"]["p"]["reserved_quantity"], "6")
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE intents SET reserved=0,state='PARTIAL_FINAL' WHERE id='c'")
            db.execute(
                "INSERT INTO settlements VALUES('p','event','leg',6000000,0,0,'actor','private receipt',1800000020000)"
            )
        snap = src.read()
        self.assertEqual(snap["meta"]["p"]["reserved_quantity"], "0")
        (settle,) = [a for a in snap["activity"] if a["kind"] == "settlement"]
        self.assertEqual((settle["quantity"], settle["net_cash"]), ("6", "0"))
        self.assertNotIn("private receipt", json.dumps(snap))

    def test_transient_read_failure_and_account_change(self):
        path = self.make_db()
        src = self.sources(ledger=path)
        first = src.read()
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("ALTER TABLE fills RENAME TO missing_fills")
        snap = src.read()
        self.assertEqual(snap["activity"], first["activity"])
        self.assertFalse(snap["known_origin"])
        self.assertEqual(snap["sources"][0]["status"], "down")
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE ledger_meta SET account='another'")
        self.assertFalse(src.read()["activity"])

    def test_attempt_states_alone_never_create_trades(self):
        path = self.root / "attempts.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE attempts(account,game_id,channel,action,attempt_id,position_id,order_id,state,updated_at,signal_at)"
            )
            db.execute(
                "INSERT INTO attempts VALUES('a','g','D','OPEN','opening','opening','oid','CLOSED',?,?)",
                (NOW, NOW - 10),
            )
        src = self.sources(attempts=path, account="a")
        snap = src.read()
        self.assertEqual(snap["activity"], [])
        self.assertEqual(snap["timeline"]["opening"][0]["state"], "CLOSED")
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE attempts SET state='UNKNOWN'")
        self.assertEqual(src.read()["timeline"]["opening"][0]["state"], "UNKNOWN")

    def test_simulated_and_missing_origin(self):
        self.append(
            dict(ev="start", live=False, t=NOW - 1),
            dict(ev="buy", gid="g", ch="A", filled_shares=2, entry=0.4, t=NOW + 1),
            dict(ev="settle_hold", gid="g", ch="A", t=NOW + 2),
        )
        snap = self.sources(runner=self.log, env="paper").read()
        self.assertEqual(snap["activity"][0]["time_kind"], "simulated")
        self.assertFalse(snap["known_origin"])
        self.assertIsNone(snap["coverage_from"])


if __name__ == "__main__":
    unittest.main()
