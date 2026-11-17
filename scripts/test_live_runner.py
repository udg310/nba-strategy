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
        # what GET /order/{id} answers; None means "no evidence"
        self.matched = None

    def get_trades(self, params):
        return list(self.trades)

    def get_order(self, oid):
        if self.matched is None:
            return None
        return {"size_matched": str(self.matched)}


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

    # ---- DL margin stop is the mirror image of D's ----
    def test_doglock_margin_stop_exits_when_lead_collapses(self):
        lr.book = lambda client, tok: (0.42, 0.40, "0.01")
        lr.market_order = self._order("SELL", fill=-1)
        lr.token_balance = lambda client, tok: 5.0      # the wallet backs it
        spec = dict(lr.DOGLOCK_CH, stop_margin_hi=-5.0)
        orig = lr.channel_spec

        def patched(ch):
            return spec if ch == "DL" else orig(ch)
        lr.channel_spec = patched
        try:
            t = lr.GameTrader("g5", RES, "HOME", True)
            now = time.time()
            # the dog led by 14 on entry and has now collapsed to +2
            t.hist = [(now, 2200.0, -2.0, ("h", "a"))]
            p = {"gid": "g5", "ch": "DL", "token": "td", "outcome": "Dog",
                 "entry": 0.55, "size": 5.0, "t_in": now - 900,
                 "el_in": 1300.0, "style": "settle", "tp": None,
                 "selling": False}
            t.positions.append(p)
            t._manage_exits({"state": "in", "period": 4, "detail": "3:00",
                             "home_score": 90, "away_score": 92}, now)
        finally:
            lr.channel_spec = orig
        self.assertNotIn(p, t.positions)
        self.assertTrue(any(r.get("reason") == "margin_stop" for r in self.logs))
        self.assertTrue(any(r.get("ev") == "sell" for r in self.logs))

    def test_doglock_margin_stop_ships_off(self):
        self.assertIsNone(lr.DOGLOCK_CH["stop_margin_hi"])

    # ---- a dry run must never touch the wallet ----
    def test_dry_run_never_sells_a_resumed_position(self):
        lr.book = lambda client, tok: (0.52, 0.50, "0.01")
        lr.market_order = self._order("SELL", fill=-1)
        t = lr.GameTrader("g2", RES, "HOME", False)      # live=False
        now = time.time()
        t.hist = [(now, 3000.0, -3.0, ("h", "a"))]
        p = {"gid": "g2", "ch": "C", "token": "td", "outcome": "Dog",
             "entry": 0.40, "size": 4.0, "t_in": now - 100,
             "el_in": 0.0, "style": "process", "tp": 0.20, "selling": False}
        t.positions.append(p)
        # force-exit time: a live trader would sell here
        t._manage_exits({"state": "in", "period": 4, "detail": "1:00",
                         "home_score": 90, "away_score": 88}, now)
        self.assertEqual(self.orders, [], "a dry run must post nothing")
        self.assertIn(p, t.positions)
        self.assertTrue(any(r.get("ev") == "dry_exit" for r in self.logs))

    # ---- a sold position must not come back on restart ----
    def test_resume_does_not_relive_a_closed_channel(self):
        base = os.path.join(lr.ROOT, "data", "live_trade_final", "_test_relive")
        shutil.rmtree(base, ignore_errors=True)
        os.makedirs(os.path.join(base, "run1"))
        with open(os.path.join(base, "run1", "events.jsonl"), "w") as f:
            for r in [
                {"ev": "buy", "gid": "g1", "ch": "A", "outcome": "Dog",
                 "entry": 0.50, "filled_shares": 4, "t": 100.0},
                {"ev": "buy", "gid": "g1", "ch": "C", "outcome": "Dog",
                 "entry": 0.50, "filled_shares": 4, "t": 101.0},
                {"ev": "sell", "gid": "g1", "ch": "A", "outcome": "Dog",
                 "sold_shares": 4},
            ]:
                f.write(json.dumps(r) + "\n")
        # both OPEN rows stay FILLED after the sell -- only the CLOSE row closes
        for ch in ("A", "C"):
            aid, ok = lr.LEDGER.begin("g1", ch, "OPEN", f"o{ch}", 100)
            self.assertTrue(ok)
            lr.LEDGER.fill(aid, 4.0, 0.5)
        old = lr.TODAY
        lr.TODAY = "_test_relive"
        try:
            plan, done = lr.resume_plan()
        finally:
            lr.TODAY = old
            shutil.rmtree(base, ignore_errors=True)
        self.assertNotIn(("g1", "A", "Dog"), plan, "A was sold: must not return")
        self.assertNotIn(("g1", "A", None), plan, "nor from its ledger row")
        self.assertEqual(plan[("g1", "C", "Dog")][1], 4.0)
        self.assertNotIn(("g1", "C", None), plan, "C must not be counted twice")

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

    # ---- cross-UTC-midnight restart must still find yesterday's buys ----
    def test_resume_reads_previous_day(self):
        root = os.path.join(lr.ROOT, "data", "live_trade_final")
        yd = os.path.join(root, "2026-11-19", "_test_resumeA")
        td = os.path.join(root, "2026-11-20", "_test_resumeB")
        os.makedirs(yd, exist_ok=True)
        os.makedirs(td, exist_ok=True)
        with open(os.path.join(yd, "events.jsonl"), "w") as f:
            f.write(json.dumps({"ev": "buy", "gid": "gYday", "ch": "C",
                                "outcome": "Dog", "entry": 0.29,
                                "filled_shares": 8.0, "el_in": 290.0}) + "\n")
        old = lr.TODAY
        lr.TODAY = "2026-11-20"          # today's dir holds no trace of it
        try:
            plan, done = lr.resume_plan()
        finally:
            lr.TODAY = old
            shutil.rmtree(yd, ignore_errors=True)
            shutil.rmtree(td, ignore_errors=True)
        self.assertIn(("gYday", "C", "Dog"), plan)
        self.assertEqual(plan[("gYday", "C", "Dog")][0], 0.29)

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

    # ---- a sell may retry; a buy may not (duplicate OPEN stays blocked) ----
    def test_ledger_allows_repeated_close(self):
        a1, ok1 = lr.LEDGER.begin("gc", "C", "CLOSE", "o1", 1)
        self.assertTrue(ok1)
        a2, ok2 = lr.LEDGER.begin("gc", "C", "CLOSE", "o2", 2)
        self.assertTrue(ok2, "a second sell attempt must be allowed")
        self.assertNotEqual(a1, a2)
        self.assertTrue(lr.LEDGER.begin("gc", "A", "OPEN", "b1", 1)[1])
        self.assertFalse(lr.LEDGER.begin("gc", "A", "OPEN", "b2", 2)[1])

    # ---- a FOK that never fills is re-posted, not waited out ----
    def test_sell_reposts_when_fok_never_fills(self):
        old = (lr.SELL_WINDOW, lr.REPOST_AFTER, lr.RETRY_GAP,
               lr.book, lr.market_order, lr.token_balance)
        lr.SELL_WINDOW, lr.REPOST_AFTER, lr.RETRY_GAP = 1.0, 0.02, 0.005
        posts = []

        def fake_post(client, token, side, amount, price, tick, on_prepared=None):
            posts.append(price)
            oid = f"o{len(posts)}"
            if on_prepared:
                on_prepared(oid)
            return {"success": True, "orderID": oid}, oid

        lr.book = lambda client, tok: (0.55, 0.50, "0.01")
        lr.market_order = fake_post
        lr.token_balance = lambda client, tok: 4.46
        self.fake.matched = 0.0        # the exchange killed the FOK: 0 matched
        try:
            t = lr.GameTrader("gr", RES, "HOME", True)
            p = {"gid": "gr", "ch": "C", "token": "td", "outcome": "Dog",
                 "entry": 0.40, "size": 4.46, "t_in": time.time(),
                 "el_in": 100.0, "style": "process", "tp": 0.20,
                 "selling": True, "exit_reason": "target"}
            t.positions.append(p)
            t._sell(p)
        finally:
            (lr.SELL_WINDOW, lr.REPOST_AFTER, lr.RETRY_GAP,
             lr.book, lr.market_order, lr.token_balance) = old
        self.assertGreaterEqual(len(posts), 2,
                                "must repost after the FOK misses")

    # ---- DL entry: no chasing a stale crossing, no re-pricing after a band miss ----
    def _dl_signal(self, wall_age, ask=0.60, enable=True):
        old = (lr.book, lr.book_depth, lr.balance, lr.market_order,
               lr.DOGLOCK_ENABLED)
        lr.book = lambda client, tok: (ask, ask - 0.01, "0.01")
        lr.book_depth = lambda client, tok: {}
        lr.balance = lambda client: 100.0
        lr.market_order = self._order("BUY", fill=-1)
        lr.DOGLOCK_ENABLED = enable
        try:
            t = lr.GameTrader("gd", RES, "HOME", True)
            now = time.time()
            t.hist = [(now - wall_age - 2, 790.0, -11.0, ("h", "a")),
                      (now - wall_age, 800.0, -13.0, ("h", "a")),
                      (now, 810.0, -13.0, ("h", "a"))]
            g = {"state": "in", "period": 2, "detail": "6:00",
                 "home_score": 40, "away_score": 53}
            t._find_signals(g, now)
        finally:
            (lr.book, lr.book_depth, lr.balance, lr.market_order,
             lr.DOGLOCK_ENABLED) = old
        return "DL" in t.pending, "DL" in t.done

    def test_doglock_enters_fresh_crossing(self):
        pending, done = self._dl_signal(wall_age=5)
        self.assertTrue(pending, "a fresh 12+ dog crossing must enter")

    def test_doglock_skips_stale_crossing(self):
        pending, done = self._dl_signal(wall_age=lr.DOGLOCK_CHASE + 60)
        self.assertFalse(pending, "must not chase a crossing seen minutes ago")
        self.assertFalse(done)

    def test_doglock_band_miss_skips_game(self):
        pending, done = self._dl_signal(wall_age=5, ask=0.30)   # below 0.45
        self.assertFalse(pending)
        self.assertTrue(done, "a band miss at the crossing must end DL for the game")

    def test_doglock_disabled_by_default_flag(self):
        pending, _ = self._dl_signal(wall_age=5, enable=False)
        self.assertFalse(pending)

    # ---- ownership: A/C/DL share one token, each position owns its own size ----
    def test_sell_posts_our_size_not_the_shared_balance(self):
        lr.book = lambda client, tok: (0.55, 0.50, "0.01")
        lr.market_order = self._order("SELL", fill=-1)
        lr.token_balance = lambda client, tok: 8.0      # ours 4 + sibling 4
        t = lr.GameTrader("gl", RES, "HOME", True)
        sib = {"gid": "gl", "ch": "A", "token": "td", "outcome": "Dog",
               "entry": 0.40, "size": 4.0, "t_in": time.time(),
               "el_in": 300.0, "style": "process", "tp": 0.20,
               "selling": False}
        p = {"gid": "gl", "ch": "DL", "token": "td", "outcome": "Dog",
             "entry": 0.55, "size": 4.0, "t_in": time.time(),
             "el_in": 1300.0, "style": "settle", "tp": None,
             "selling": True, "exit_reason": "margin_stop"}
        t.positions.extend([sib, p])
        t._sell(p)
        self.assertTrue(self.orders, "the stop must post a sell")
        self.assertAlmostEqual(self.orders[0][2], 4.0,
                               msg="must sell our size, never the shared balance")
        self.assertAlmostEqual(sib["size"], 4.0, msg="the sibling is untouched")

    def test_stale_position_never_sells_a_siblings_shares(self):
        # A and C each bought 4; A's sell already filled, but A's record is
        # stale (trade API lagged).  The wallet now holds only C's 4.  Selling
        # A's 4 now would take C's shares -- refuse, do not shrink to fit.
        lr.book = lambda client, tok: (0.55, 0.50, "0.01")
        lr.market_order = self._order("SELL", fill=-1)
        lr.token_balance = lambda client, tok: 4.0
        t = lr.GameTrader("gz", RES, "HOME", True)
        sib = {"gid": "gz", "ch": "C", "token": "td", "outcome": "Dog",
               "entry": 0.40, "size": 4.0, "t_in": time.time(),
               "el_in": 300.0, "style": "process", "tp": 0.20,
               "selling": False}
        p = {"gid": "gz", "ch": "A", "token": "td", "outcome": "Dog",
             "entry": 0.40, "size": 4.0, "t_in": time.time(),
             "el_in": 300.0, "style": "process", "tp": 0.20,
             "selling": True, "exit_reason": "target"}
        t.positions.extend([sib, p])
        t._sell(p)
        self.assertEqual(self.orders, [], "must not sell the sibling's shares")
        self.assertTrue(any(r.get("ev") == "sell_short_of_wallet"
                            for r in self.logs))
        self.assertAlmostEqual(sib["size"], 4.0)

    def test_excess_is_not_folded_into_a_sibling_channel(self):
        lr.book = lambda client, tok: (0.60, 0.50, "0.01")
        lr.token_balance = lambda client, tok: 10.0     # 8 accounted + 2 loose
        lr.market_order = self._order("SELL", fill=-1)
        t = lr.GameTrader("gy", RES, "HOME", True)
        now = time.time()
        sib = {"gid": "gy", "ch": "A", "token": "td", "outcome": "Dog",
               "entry": 0.40, "size": 4.0, "t_in": now - 10,
               "el_in": 300.0, "style": "process", "tp": 0.20,
               "selling": False}
        p = {"gid": "gy", "ch": "C", "token": "td", "outcome": "Dog",
             "entry": 0.40, "size": 4.0, "t_in": now - 10,
             "el_in": 300.0, "style": "process", "tp": 0.20,
             "selling": False}
        t.positions.extend([sib, p])
        t._manage_exits({"state": "in", "period": 3, "detail": "6:00",
                         "home_score": 70, "away_score": 72}, now)
        self.assertFalse(any(r.get("ev") == "reconcile_add" for r in self.logs))
        self.assertTrue(any(r.get("ev") == "unattributed_excess"
                            for r in self.logs))
        self.assertAlmostEqual(p["size"], 4.0)
        self.assertAlmostEqual(sib["size"], 4.0)

    def test_loose_fill_is_refused_on_a_shared_token(self):
        # a single unclaimed trade on a token another channel holds is theirs
        lr.token_balance = lambda client, tok: 8.0
        t = lr.GameTrader("gf", RES, "HOME", True)
        sib = {"gid": "gf", "ch": "A", "token": "td", "outcome": "Dog",
               "entry": 0.40, "size": 4.0, "t_in": time.time(),
               "el_in": 300.0, "style": "process", "tp": 0.20,
               "selling": False}
        t.positions.append(sib)
        self.fake.trades = [{"id": "tx", "status": "CONFIRMED", "side": "BUY",
                             "size": "4.0", "price": "0.40",
                             "match_time": str(int(time.time())),
                             "taker_order_id": "someone-else"}]
        self.assertEqual(t._fills("td", "BUY", set(), int(time.time()) - 5,
                                  "our-order"), [])
        # with no other holder on the token the fallback still recovers it
        t.positions = []
        got = t._fills("td", "BUY", set(), int(time.time()) - 5, "our-order")
        self.assertEqual(len(got), 1)

    # ---- the sell is checked against what its own order bought ----
    def test_sell_flags_a_size_beyond_its_own_fill(self):
        lr.book = lambda client, tok: (0.55, 0.50, "0.01")
        lr.market_order = self._order("SELL", fill=-1)
        lr.token_balance = lambda client, tok: 40.0

        def run(size, fill, adopted=0.0):
            self.logs = []
            t = lr.GameTrader("gv", RES, "HOME", True)
            p = {"gid": "gv", "ch": "DL", "token": "td", "outcome": "Dog",
                 "entry": 0.55, "size": size, "t_in": time.time(),
                 "el_in": 1300.0, "style": "settle", "tp": None,
                 "selling": True, "position_id": "A1",
                 "fill_shares": fill, "adopted": adopted}
            t.positions.append(p)
            t._sell(p)
            return [r for r in self.logs if r.get("ev") == "size_unbacked"]

        self.assertEqual(run(4.0, 4.0), [], "own fill: nothing to flag")
        self.assertEqual(run(6.0, 4.0, 2.0), [], "adopted by the wallet: fine")
        flagged = run(6.0, 4.0)
        self.assertEqual(len(flagged), 1, "unbacked size must be logged")
        self.assertAlmostEqual(flagged[0]["fill_shares"], 4.0)
        self.assertEqual(flagged[0]["position_id"], "A1")

    # ---- a shared token must not make one channel sell another's shares ----
    def _sweep(self, matched, balance=8.0, sibling=4.0):
        """Run one sell with a sibling position on the same token; return posts."""
        old = (lr.SELL_WINDOW, lr.REPOST_AFTER, lr.RETRY_GAP,
               lr.book, lr.market_order, lr.token_balance)
        lr.SELL_WINDOW, lr.REPOST_AFTER, lr.RETRY_GAP = 0.4, 0.02, 0.005
        posts = []

        def fake_post(client, token, side, amount, price, tick, on_prepared=None):
            posts.append(amount)
            oid = f"o{len(posts)}"
            if on_prepared:
                on_prepared(oid)
            return {"success": True, "orderID": oid}, oid
        lr.book = lambda client, tok: (0.55, 0.50, "0.01")
        lr.market_order = fake_post
        lr.token_balance = lambda client, tok: balance
        self.fake.matched = matched
        try:
            t = lr.GameTrader("gs", RES, "HOME", True)
            sib = {"gid": "gs", "ch": "A", "token": "td", "outcome": "Dog",
                   "entry": 0.40, "size": sibling, "t_in": time.time(),
                   "el_in": 100.0, "style": "process", "tp": 0.20,
                   "selling": False}
            p = {"gid": "gs", "ch": "DL", "token": "td", "outcome": "Dog",
                 "entry": 0.55, "size": 4.0, "t_in": time.time(),
                 "el_in": 1300.0, "style": "settle", "tp": None,
                 "selling": True, "exit_reason": "margin_stop"}
            t.positions.extend([sib, p])
            t._sell(p)
        finally:
            (lr.SELL_WINDOW, lr.REPOST_AFTER, lr.RETRY_GAP,
             lr.book, lr.market_order, lr.token_balance) = old
        return posts

    def test_repost_ignores_the_shared_balance(self):
        # The exchange says our sell matched 0 -> repost is safe.
        self.assertGreaterEqual(len(self._sweep(matched=0.0)), 2)
        # The exchange says our sell filled, but the balance API still shows
        # every channel's shares.  Reposting here would sell A's position:
        # one post only, no matter what the balance reads.
        self.assertEqual(len(self._sweep(matched=4.0, balance=8.0)), 1)
        # No evidence at all -> wait, never guess.
        self.assertEqual(len(self._sweep(matched=None, balance=8.0)), 1)

    def test_repost_needs_exchange_evidence(self):
        self.assertEqual(lr.order_matched(self.fake, ""), None)
        self.fake.matched = 2.5
        self.assertAlmostEqual(lr.order_matched(self.fake, "o1"), 2.5)

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

    # ---- lock channel: fires on a 12-point favourite lead, buys the favourite ----
    def test_lock_signal_and_spec(self):
        evs = [(0, 0, 0), (100, 11, 100), (200, 12, 200), (300, 20, 300)]
        s = lr.lock_signal(evs)
        self.assertIsNotNone(s)
        self.assertEqual(s["margin"], 12)
        self.assertEqual(s["el"], 200)
        self.assertIsNone(lr.lock_signal([(0, 0, 0), (100, 11, 100)]))
        spec = lr.channel_spec("L")
        self.assertEqual(spec["side"], "fav")
        self.assertEqual(spec["style"], "settle")

    # ---- dog lock: fires only from Q2 on, on the dog's side, in its own pool ----
    def test_doglock_signal_gates_clock_and_side(self):
        # a Q1 12-point dog lead must NOT trigger
        q1 = [(100, -12, 100), (400, -15, 400)]
        self.assertIsNone(lr.doglock_signal(q1))
        # the same lead once Q2 starts does
        q2 = [(100, -12, 100), (760, -13, 760), (900, -14, 900)]
        s = lr.doglock_signal(q2)
        self.assertIsNotNone(s)
        self.assertEqual(s["margin"], -13)
        self.assertEqual(s["el"], 760)
        # favourite leads never trigger it
        self.assertIsNone(lr.doglock_signal([(760, 12, 760), (900, 14, 900)]))
        spec = lr.channel_spec("DL")
        self.assertEqual(spec["side"], "dog")
        self.assertEqual(spec["style"], "settle")

    def test_doglock_has_own_slot_pool_and_frac(self):
        self.assertEqual(lr._slot_pool("DL"), ("DL", lr.MAX_OPEN_DOGLOCK))
        self.assertEqual(lr._slot_pool("L"), ("L", lr.MAX_OPEN_LOCK))
        self.assertEqual(lr._slot_pool("A"), (None, lr.MAX_OPEN))
        self.assertAlmostEqual(lr._frac_of("DL"), lr.DOGLOCK_FRAC)
        # A/C/D keep their own weighting, untouched by the dog lock
        self.assertAlmostEqual(lr._frac_of("D"),
                               lr.STAKE_FRAC * lr.S.CHANNELS["D"]["weight"])

    # ---- back-to-back same teams: the neighbour night's market is rejected ----
    def test_market_must_match_game_date(self):
        start = "2026-11-17T01:00Z"          # this game's tip-off (UTC)
        same_night = {"endDate": "2026-11-17T01:00:00Z"}
        next_night = {"endDate": "2026-11-18T01:00:00Z"}
        self.assertTrue(lr._market_matches_game(same_night, start))
        self.assertFalse(lr._market_matches_game(next_night, start))
        # unknown / unparseable dates must not break resolution
        self.assertTrue(lr._market_matches_game({}, start))
        self.assertTrue(lr._market_matches_game({"endDate": "garbage"}, start))


if __name__ == "__main__":
    unittest.main()
