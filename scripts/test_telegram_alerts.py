"""Telegram notices only follow confirmed, account-matched fills."""

from contextlib import contextmanager
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock

from telegram_alerts import TelegramAlerts
from pm_creds import load_env
import pm_creds
from unittest.mock import patch


class LedgerStub:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE external_fills(order_id TEXT,id TEXT,confirmed INTEGER,
                quantity INTEGER,price INTEGER,side TEXT,time INTEGER);
            CREATE TABLE attempts(order_id TEXT,intent_id TEXT);
            CREATE TABLE intents(id TEXT,game_id TEXT,channel TEXT,action TEXT);
            INSERT INTO intents VALUES('i','game','D','OPEN');
            INSERT INTO attempts VALUES('order','i');
            INSERT INTO external_fills VALUES('order','old',1,1000000,750000,'BUY',1);
        """)

    @contextmanager
    def tx(self):
        yield self.db
        self.db.commit()


class TelegramTests(unittest.TestCase):
    def test_credential_helper_uses_current_session_and_deposit_default(self):
        with patch("pm_creds.ENV_PATH", "missing-env-file"), patch.dict(
                "os.environ", {"POLY_PRIVATE_KEY": "test-key", "POLY_FUNDER": "0xabc"}, clear=True):
            result = load_env()
        self.assertEqual(result["POLY_SIGNATURE_TYPE"], "3")
        self.assertEqual(result["POLY_FUNDER"], "0xabc")

    def test_credential_helper_calls_pinned_v2_api(self):
        package = ModuleType("py_clob_client_v2")
        client_module = ModuleType("py_clob_client_v2.client")

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.kwargs = kwargs

            def create_or_derive_api_key(self):
                return SimpleNamespace(api_key="key", api_secret="secret",
                                       api_passphrase="phrase")

        client_module.ClobClient = FakeClient
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                sys.modules, {"py_clob_client_v2": package,
                              "py_clob_client_v2.client": client_module}), patch.object(
                pm_creds, "load_env", return_value={"POLY_PRIVATE_KEY": "test",
                    "POLY_FUNDER": "0xabc", "POLY_SIGNATURE_TYPE": "3"}), patch.object(
                pm_creds, "CREDS_PATH", str(Path(directory, "creds.json"))), patch.object(
                sys, "argv", ["pm_creds.py"]):
            pm_creds.main()
            result = json.loads(Path(directory, "creds.json").read_text())
        self.assertEqual(result["api_key"], "key")
        self.assertEqual(result["signature_type"], 3)

    def test_new_confirmed_fill_sent_once_and_old_history_is_baselined(self):
        ledger = LedgerStub()
        reports = Mock()
        reports.alert_summary.return_value = ""
        alerts = TelegramAlerts(ledger, reports, token="token", chat_id="123")
        alerts.send = Mock()
        ledger.db.execute("INSERT INTO external_fills VALUES('order','new',1,2000000,740000,'BUY',2)")
        ledger.db.execute("INSERT INTO external_fills VALUES('order','pending',0,1000000,730000,'BUY',3)")
        ledger.db.execute("INSERT INTO external_fills VALUES('outside','other',1,1000000,700000,'BUY',4)")
        alerts.tick()
        alerts.tick()
        self.assertEqual(alerts.send.call_count, 1)
        self.assertIn("2.000000", alerts.send.call_args.args[0])
        self.assertIn("$0.7400", alerts.send.call_args.args[0])
        self.assertEqual(ledger.db.execute("SELECT count(*) FROM telegram_sent").fetchone()[0], 2)
        ledger.db.close()


if __name__ == "__main__":
    unittest.main()
