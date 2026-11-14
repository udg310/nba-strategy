"""CLI and HTTP compatibility checks using isolated paper accounts."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import build_opener, ProxyHandler, Request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


class ExecutorStartupTest(unittest.TestCase):
    def run_command(self, db, *args):
        env = os.environ.copy()
        env.pop("NBA_EXECUTOR_TOKEN", None)
        return subprocess.run(
            [sys.executable, "-I", "scripts/executor.py", "--db", str(db),
             "--account", str(uuid4()), "--listen", "127.0.0.1:0", *args],
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=60,
        )

    def test_recording_finishes_in_one_python_process(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory, "custom ledger.db")
            recording = Path(directory, "one game.json")
            recording.write_text(json.dumps([{
                "gid": "demo", "pre": 0.65, "fav": "Home",
                "outcomes": ["Home", "Away"], "prices": {"Home": [], "Away": []},
                "evs": [[2880, 1, 1000]],
            }]))
            result = self.run_command(db, "--recording", str(recording))
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["game_id"], "demo")
            self.assertTrue(report["finished"])
            self.assertTrue(Path(str(db) + ".token").is_file())

    def test_live_recording_fails_before_opening_database(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory, "account.db")
            result = self.run_command(db, "--mode", "live", "--recording", "test")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("paper", result.stderr)
            self.assertFalse(db.exists())

    def test_authenticated_order_api_reuses_an_intent_and_records_conflicts(self):
        from executor_api import serve, ExecutorAPI
        from executor_ledger import Ledger
        from executor_reports import Reports

        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory, "paper.db"), "local", "paper", 100_000_000, 5)
            try:
                api = ExecutorAPI(ledger, Reports(ledger), None)
                with serve(api, ("127.0.0.1", 0), "test-token") as (host, port):
                    transport = build_opener(ProxyHandler({}))

                    def request(path, body=None, authorized=True):
                        req = Request(f"http://{host}:{port}" + path,
                                      data=json.dumps(body).encode() if body is not None else None)
                        if authorized:
                            req.add_header("Authorization", "Bearer test-token")
                        with transport.open(req, timeout=5) as response:
                            return response.status, json.load(response)

                    self.assertEqual(request("/healthz", authorized=False)[1]["environment"], "paper")
                    path = "/v1/order-intents"
                    now = time.time_ns() // 1_000_000
                    body = dict(action="OPEN", channel="A", game_id="test", market="m", asset="a",
                                amount=1_000_000, limit_price=750_000, signal_at=now, deadline=now+15000)
                    with self.assertRaises(HTTPError) as unauthorized:
                        request(path, body, authorized=False)
                    self.assertEqual(unauthorized.exception.code, 401)
                    created, order = request(path, body)
                    self.assertEqual(created, 201)
                    duplicate, reused = request(path, body)
                    self.assertEqual((duplicate, reused["id"]), (200, order["id"]))
                    with self.assertRaises(HTTPError) as conflict:
                        request(path, dict(body, amount=2_000_000))
                    self.assertEqual(conflict.exception.code, 409)
                    detail = request(path + "/" + order["id"])[1]
                    self.assertEqual(detail["state"], "READY")
                    self.assertEqual(detail["reserved"], body["amount"])
                    metrics = request("/v1/metrics")[1]
                    self.assertEqual((metrics["duplicate_requests"], metrics["conflicting_requests"]), (1, 1))
            finally:
                ledger.close()


if __name__ == "__main__":
    unittest.main()
