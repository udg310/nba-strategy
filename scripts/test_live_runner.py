# -*- coding: utf-8 -*-
"""Regression tests for the live execution fixes (run under the venv).

    ~/.polymarket/venv/bin/python -m unittest discover -s scripts -p 'test_live_runner.py'

Covers: one post per signal, trade-record confirmation / order-ID isolation,
own-size sells + dust, D margin stop, C game-clock time exit, resume ledger.
"""
import json, os, shutil, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import live_runner as lr

RES = {"fav": "Fav", "outcomes": ["Fav", "Dog"], "tokens": ["tf", "td"], "pre": 0.7}


class FakeClient:
    def __init__(self):
        self.trades = []

    def get_trades(self, params):
        return list(self.trades)


class LiveRunnerFixes(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.fake = FakeClient()
        lr.log = lambda r: self.logs.append(dict(r))
        lr.make_client = lambda: self.fake
        lr.RETRY_GAP = 0.005
        lr.token_balance = lambda client, tok: 4.46
        self.n = 0
        self.orders = []
        self.tmp = tempfile.mkdtemp(prefix="lr-ledger-")
        lr.LEDGER = lr.AttemptLedger(os.path.join(self.tmp, "attempts.db"), "test")

    def tearDown(self):
        if lr.LEDGER is not None:
            lr.LEDGER.close_path()
            lr.LEDGER = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _order(self, side="SELL", fill=None, response=None):
        def f(client, token, s, amount, price, tick, on_prepared=None):
            self.n += 1
            oid = f"o{self.n}"
            self.orders.append((oid, s, amount, price, tick))
            if on_prepared:
                on_prepared(oid)
            if fill is not None:
                self.fake.trades.append({
                    "id": f"t{self.n}", "status": "CONFIRMED", "side": side,
                    "size": str(fill if fill >= 0 else amount),
                    "price": str(price), "match_time": str(int(time.time())),
                    "taker_order_id": oid})
            resp = response if response is not None else {"success": True, "orderID": oid}
            return resp, oid
        return f

    def _inflight(self, key="C", token="td", el=300.0):
        now = time.time()
        return {"token": token, "outcome": "Dog", "ch": key, "usd": 3.73, "limit": 0.79,
                "deadline": now + 15, "tries": 0, "style": "process",
                "tp": 0.20, "ask": 0.76, "posted": False, "order_id": None,
                "el_sig": el, "seen": set(), "t0": int(now) - 1, "base_bal": 0.0}

    # ---- one post + trade confirmation ----
    def test_buy_posts_once_and_opens_from_trade(self):
        lr.book = lambda client, tok: (0.76, 0.75, "0.01")
        lr.market_order = self._order("SELL", fill=None)   # response only
        t = lr.GameTrader("g1", RES, "HOME", True)
        q = self._inflight()
        t.inflight["C"] = q
        now = time.time()
        t._work_inflight(None, now)
        self.assertEqual(len(self.orders), 1)
        t._work_inflight(None, now + 3)
        self.assertEqual(len(self.orders), 1, "must not repost")
        oid = self.orders[0][0]
        self.fake.trades = [{"id": "tb", "status": "CONFIRMED", "side": "BUY",
                             "size": "5.109588", "price": "0.73",
                             "match_time": str(int(now)),
                             "taker_order_id": oid}]
        t._work_inflight(None, now + 6)
        self.assertEqual(len(t.positions), 1)
        self.assertAlmostEqual(t.positions[0]["size"], 5.109588)
        self.assertAlmostEqual(t.positions[0]["entry"], 0.73)

    # ---- A and C on the same token must not claim each other's fill ----
    def test_order_id_isolation(self):
        lr.book = lambda client, tok: (0.50, 0.49, "0.01")
        lr.market_order = self._order("SELL", fill=None)
        t = lr.GameTrader("g2", RES, "HOME", True)
        now = time.time()
        for ch in ("A", "C"):
            t.inflight[ch] = self._inflight(ch)
        t._work_inflight(None, now)
        oA, oC = self.orders[0][0], self.orders[1][0]
        self.fake.trades = [
            {"id": "ta", "status": "CONFIRMED", "side": "BUY", "size": "3.227273",
             "price": "0.44", "match_time": str(int(now)), "taker_order_id": oA},
            {"id": "tc", "status": "CONFIRMED", "side": "BUY", "size": "5.555556",
             "price": "0.27", "match_time": str(int(now)), "taker_order_id": oC},
        ]
        t._work_inflight(None, now + 3)
        by = {p["ch"]: p for p in t.positions}
        self.assertAlmostEqual(by["A"]["size"], 3.227273)
        self.assertAlmostEqual(by["C"]["size"], 5.555556)

    # ---- sell only this position's shares; dust closes ----
    def test_sell_own_size_and_dust(self):
        lr.book = lambda client, tok: (0.62, 0.61, "0.01")
        lr.token_balance = lambda client, tok: 8.782829
        lr.market_order = self._order("SELL", fill=-1)
        t = lr.GameTrader("g3", RES, "HOME", True)
        pA = {"gid": "g3", "ch": "A", "token": "td", "outcome": "Dog",
              "entry": 0.44, "size": 3.227273, "t_in": time.time(),
              "el_in": 100.0, "style": "process", "tp": 0.20, "selling": False}
        pC = {"gid": "g3", "ch": "C", "token": "td", "outcome": "Dog",
              "entry": 0.27, "size": 5.555556, "t_in": time.time(),
              "el_in": 100.0, "style": "process", "tp": 0.20,
              "selling": True, "exit_reason": "target"}
        t.positions[:] = [pA, pC]
        t._sell(pC)
        self.assertEqual(len(self.orders), 1)
        self.assertAlmostEqual(self.orders[0][2], 5.55)
        self.assertNotIn(pC, t.positions)
        self.assertIn(pA, t.positions)

    # ---- D margin stop ----
    def test_d_margin_stop(self):
        lr.book = lambda client, tok: (0.75, 0.70, "0.01")
        lr.market_order = self._order("SELL", fill=-1)
        t = lr.GameTrader("g4", RES, "HOME", True)
        now = time.time()
        t.hist = [(now, 2200.0, -1.0, ("h", "a"))]
        p = {"gid": "g4", "ch": "D", "token": "tf", "outcome": "Fav",
             "entry": 0.69, "size": 4.46, "t_in": now - 120,
             "el_in": 2000.0, "style": "settle", "tp": None, "selling": False}
        t.positions.append(p)
        t._manage_exits({"state": "in", "period": 4, "detail": "6:00",
                         "home_score": 80, "away_score": 81}, now)
        self.assertNotIn(p, t.positions)
        self.assertTrue(any(r.get("reason") == "margin_stop" for r in self.logs))
        self.assertTrue(any(r.get("ev") == "sell" for r in self.logs))

    # ---- C 30 game-clock minute exit ----
    def test_c_game_clock_time_exit(self):
        lr.book = lambda client, tok: (0.52, 0.50, "0.01")
        lr.market_order = self._order("SELL", fill=-1)
        t = lr.GameTrader("g5", RES, "HOME", True)
        now = time.time()
        p = {"gid": "g5", "ch": "C", "token": "td", "outcome": "Dog",
             "entry": 0.40, "size": 4.46, "t_in": now - 100,
             "el_in": 0.0, "style": "process", "tp": 0.20, "selling": False}
        t.positions.append(p)
        # period 3, 6:00 -> elapsed 1800 >= 30*60 (corrected clock)
        t._manage_exits({"state": "in", "period": 3, "detail": "6:00",
                         "home_score": 60, "away_score": 60}, now)
        self.assertNotIn(p, t.positions)
        self.assertTrue(any(r.get("reason") == "time" for r in self.logs))

    # ---- resume buys - sold ----
    def test_resume_ledger(self):
        base = os.path.join(lr.ROOT, "data", "live_trade_final", "_test_resume")
        shutil.rmtree(base, ignore_errors=True)
        os.makedirs(os.path.join(base, "run1"))
        with open(os.path.join(base, "run1", "events.jsonl"), "w") as f:
            for r in [
                {"ev": "buy", "gid": "g1", "ch": "A", "outcome": "Dog",
                 "entry": 0.5, "filled_shares": 10, "t": 100.0,
                 "el_in": 300.0},
                {"ev": "sell", "gid": "g1", "ch": "A", "outcome": "Dog",
                 "sold_shares": 4},
                {"ev": "buy", "gid": "g1", "ch": "C", "outcome": "Dog",
                 "entry": 0.3, "filled_shares": 5, "t": 200.0,
                 "el_in": 400.0},
                {"ev": "buy", "gid": "g1", "ch": "D", "outcome": "Fav",
                 "entry": 0.75, "filled_shares": 2},
                {"ev": "settle_hold", "gid": "g1", "ch": "D", "outcome": "Fav"},
            ]:
                f.write(json.dumps(r) + "\n")
        old = lr.TODAY
        lr.TODAY = "_test_resume"
        try:
            plan, done = lr.resume_plan()
        finally:
            lr.TODAY = old
            shutil.rmtree(base, ignore_errors=True)
        self.assertEqual(plan[("g1", "A", "Dog")][1], 6.0)
        self.assertEqual(plan[("g1", "C", "Dog")][1], 5.0)
        self.assertNotIn(("g1", "D", "Fav"), plan)
        self.assertIn(("g1", "D"), done)

    # ---- a 200 with success:false is UNKNOWN, never a retryable rejection ----
    def test_buy_200_success_false_is_posted_no_retry(self):
        lr.book = lambda client, tok: (0.76, 0.75, "0.01")
        lr.market_order = self._order(
            "BUY", fill=None, response={"success": False, "status": "unmatched"})
        t = lr.GameTrader("gb", RES, "HOME", True)
        q = self._inflight()
        t.inflight["C"] = q
        now = time.time()
        t._work_inflight(None, now)
        self.assertEqual(len(self.orders), 1)
        self.assertTrue(q.get("posted"))
        t._work_inflight(None, now + 3)
        self.assertEqual(len(self.orders), 1, "UNKNOWN must not repost")
        row = lr.LEDGER.rows("gb")[0]
        self.assertEqual(row["state"], "UNKNOWN")

    # ---- one open attempt per game/channel, including after a restart ----
    def test_duplicate_open_blocked(self):
        aid, ok = lr.LEDGER.begin("gd", "C", "OPEN", "oid1", 1, el_in=100)
        self.assertTrue(ok)
        aid2, ok2 = lr.LEDGER.begin("gd", "C", "OPEN", "oid2", 2, el_in=200)
        self.assertFalse(ok2)
        self.assertIsNone(aid2)
        self.assertTrue(lr.LEDGER.has_open("gd", "C"))
        t = lr.GameTrader("gd", RES, "HOME", True)
        self.assertIn("C", t.done)

    def test_recover_submitting_unknown(self):
        aid, ok = lr.LEDGER.begin("gr", "A", "OPEN", "o", 1, el_in=100)
        self.assertTrue(ok)
        lr.LEDGER.recover()
        row = lr.LEDGER.rows("gr")[0]
        self.assertEqual(row["state"], "UNKNOWN")
        self.assertTrue(lr.LEDGER.has_open("gr", "A"))

    # ---- token shares we did not account for are added to the position ----
    def test_balance_extra_added(self):
        lr.book = lambda client, tok: (0.60, 0.50, "0.01")
        lr.token_balance = lambda client, tok: 5.957
        lr.market_order = self._order("SELL", fill=-1)
        t = lr.GameTrader("gx", RES, "HOME", True)
        now = time.time()
        p = {"gid": "gx", "ch": "C", "token": "td", "outcome": "Dog",
             "entry": 0.40, "size": 2.979, "t_in": now - 10,
             "el_in": 100.0, "style": "process", "tp": 0.20, "selling": False}
        t.positions.append(p)
        t._manage_exits({"state": "in", "period": 2, "detail": "6:00",
                         "home_score": 50, "away_score": 50}, now)
        self.assertAlmostEqual(p["size"], 5.957, places=6)
        self.assertTrue(any(r.get("ev") == "reconcile_add" for r in self.logs))

    # ---- a buy still in flight for the same token is not folded into C ----
    def test_reconcile_add_skipped_when_buy_inflight(self):
        lr.book = lambda client, tok: (0.60, 0.50, "0.01")
        lr.token_balance = lambda client, tok: 5.957   # A's + C's shares
        lr.market_order = self._order("SELL", fill=-1)
        t = lr.GameTrader("gq", RES, "HOME", True)
        now = time.time()
        p = {"gid": "gq", "ch": "C", "token": "td", "outcome": "Dog",
             "entry": 0.40, "size": 2.979, "t_in": now - 10,
             "el_in": 100.0, "style": "process", "tp": 0.20, "selling": False}
        t.positions.append(p)
        # A's buy for the same token is posted but its fill is not recorded yet
        t.inflight["A"] = {"token": "td", "ch": "A", "posted": True,
                           "outcome": "Dog"}
        t._manage_exits({"state": "in", "period": 2, "detail": "6:00",
                         "home_score": 50, "away_score": 50}, now)
        self.assertAlmostEqual(p["size"], 2.979, places=6)
        self.assertFalse(any(r.get("ev") == "reconcile_add" for r in self.logs))

    # ---- a final unsellable position is closed, not retried forever ----
    def test_finished_unsellable_position_closes(self):
        lr.book = lambda client, tok: (None, None, "0.01")   # no bid at all
        lr.token_balance = lambda client, tok: 7.36
        lr.market_order = self._order("SELL", fill=None)
        old = lr.SELL_WINDOW
        lr.SELL_WINDOW = 0.05
        try:
            t = lr.GameTrader("gx", RES, "HOME", True)
            p = {"gid": "gx", "ch": "C", "token": "td", "outcome": "Dog",
                 "entry": 0.50, "size": 7.36, "t_in": time.time(),
                 "el_in": 100.0, "style": "process", "tp": 0.20,
                 "selling": False}
            t.positions.append(p)
            t._sell(p, finished=True)
        finally:
            lr.SELL_WINDOW = old
        self.assertNotIn(p, t.positions)
        self.assertTrue(any(r.get("ev") == "reconcile_closed" for r in self.logs))


if __name__ == "__main__":
    unittest.main()
