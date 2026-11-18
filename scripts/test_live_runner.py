# -*- coding: utf-8 -*-
"""Regression tests for the live execution fixes (run under the venv).

    ~/.polymarket/venv/bin/python -m unittest discover -s scripts -p 'test_live_runner.py'

Covers: one post per signal, trade-record confirmation / order-ID isolation,
own-size sells + dust, D margin stop, C game-clock time exit, resume ledger.
"""
import json, os, shutil, sys, tempfile, time, unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import live_runner as lr

RES = {"fav": "Fav", "outcomes": ["Fav", "Dog"], "tokens": ["tf", "td"], "pre": 0.7}


class FakeClient:
    def __init__(self):
        self.trades = []
        self.orders = {}

    def get_trades(self, params):
        return list(self.trades)

    def get_order(self, oid):
        return self.orders.get(oid)

    def get_balance_allowance(self, params):
        return {"balance": "100000000"}


class LiveRunnerFixes(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.fake = FakeClient()
        self.n = 0
        self.orders = []
        self.tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="lr-ledger-"))
        ledger = lr.AttemptLedger(os.path.join(self.tmp, "attempts.db"), "test")
        self.addCleanup(ledger.close_path)
        self.enterContext(patch.multiple(lr, ROOT=self.tmp, LEDGER=ledger,
            log=lambda rec: self.logs.append(dict(rec)), make_client=lambda: self.fake,
            token_balance=lambda client, tok: 100., book=lr.book, market_order=lr.market_order,
            ALL_POSITIONS=[], CONFIRMED={"buy": False, "sell": False}))

    def hold(self, trader, position, confirmed=None):
        size = position["size"] if confirmed is None else confirmed
        oid = "buy-" + trader.gid + position["ch"]
        aid, ok = lr.LEDGER.begin(trader.gid, position["ch"], "OPEN", oid, 1,
                                   token=position["token"], outcome=position["outcome"],
                                   reserved_usd=size * position["entry"])
        self.assertTrue(ok)
        lr.LEDGER.record_buy_fills(oid, [dict(id="fill-" + oid, size=size,
                                              price=position["entry"])])
        lr.LEDGER.fill(aid, size, position["entry"])
        position.update(position_id=aid, fill_shares=size)
        trader.positions.append(position)

    def _order(self, side="SELL", fill=None, response=None):
        def f(client, token, s, amount, price, tick, on_prepared=None, *, live=False, **kwargs):
            self.assertTrue(live)
            self.n += 1
            oid = f"o{self.n}"
            if on_prepared:
                on_prepared(oid)
            self.orders.append((oid, s, amount, price, tick))
            if fill is not None:
                self.assertEqual(side, s)
                filled = fill if fill >= 0 else amount
                self.fake.trades.append({
                    "id": f"t{self.n}", "status": "CONFIRMED", "side": s, "asset_id": token,
                    "size": str(filled),
                    "price": str(price), "match_time": str(int(time.time())),
                    "taker_order_id": oid})
                self.fake.orders[oid] = dict(id=oid, status="MATCHED", side=s, asset_id=token,
                                             size_matched=str(filled), associate_trades=[f"t{self.n}"])
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

    # ---- sell only this position's shares; unsold dust remains owned ----
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
        self.hold(t, pA)
        self.hold(t, pC)
        t._sell(pC)
        self.assertEqual(len(self.orders), 1)
        self.assertAlmostEqual(self.orders[0][2], 5.55)
        self.assertIn(pC, t.positions)
        self.assertAlmostEqual(pC["size"], .005556)
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
        self.hold(t, p)
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
        self.hold(t, p)
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
        self.assertEqual(plan[("g1", "D", "Fav")][1], 2.0)
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
        trader = lr.GameTrader("gc", RES, "HOME", True)
        position = dict(ch="C", token="td", outcome="Dog", entry=.40, size=4)
        self.hold(trader, position)
        ownership = dict(requested_size=4, position_id=position["position_id"], token="td", outcome="Dog")
        a1, ok1 = lr.LEDGER.begin("gc", "C", "CLOSE", "o1", 1, **ownership)
        self.assertTrue(ok1)
        self.assertFalse(lr.LEDGER.begin("gc", "C", "CLOSE", "o2", 2, **ownership)[1])
        lr.LEDGER.finish(a1, lr.REJECTED_ZERO, "o1", identity_verified=True)
        a2, ok2 = lr.LEDGER.begin("gc", "C", "CLOSE", "o2", 2, **ownership)
        self.assertTrue(ok2, "proven zero releases the original reservation")
        self.assertNotEqual(a1, a2)
        self.assertTrue(lr.LEDGER.begin("gc", "A", "OPEN", "b1", 1)[1])
        self.assertFalse(lr.LEDGER.begin("gc", "A", "OPEN", "b2", 2)[1])

    # ---- a FOK that never fills is re-posted, not waited out ----
    def test_sell_reposts_when_fok_never_fills(self):
        lr.book = lambda client, tok: (0.70, 0.65, "0.01")
        lr.market_order = self._order()
        lr.token_balance = lambda client, tok: 4.46
        t = lr.GameTrader("gr", RES, "HOME", True)
        p = {"gid": "gr", "ch": "C", "token": "td", "outcome": "Dog",
             "entry": 0.40, "size": 4.46, "t_in": time.time(),
             "el_in": 100.0, "style": "process", "tp": 0.20,
             "selling": True, "exit_reason": "target"}
        self.hold(t, p)
        t._sell(p)
        t._sell(p)
        self.assertEqual(len(self.orders), 1)
        oid = self.orders[0][0]
        self.fake.orders[oid] = dict(id=oid, asset_id="td", side="SELL", status="CANCELED",
                                     size_matched="0", associate_trades=[])
        t._sell(p)
        t._sell(p)
        self.assertEqual(len(self.orders), 2)

    # ---- ownership: A/C/D share one token, each position owns its own size ----
    def test_sell_posts_our_size_not_the_shared_balance(self):
        lr.book = lambda client, tok: (0.55, 0.50, "0.01")
        lr.market_order = self._order("SELL", fill=-1)
        lr.token_balance = lambda client, tok: 8.0      # ours 4 + sibling 4
        t = lr.GameTrader("gl", RES, "HOME", True)
        sib = {"gid": "gl", "ch": "A", "token": "td", "outcome": "Dog",
               "entry": 0.40, "size": 4.0, "t_in": time.time(),
               "el_in": 300.0, "style": "process", "tp": 0.20,
               "selling": False}
        p = {"gid": "gl", "ch": "D", "token": "td", "outcome": "Dog",
             "entry": 0.55, "size": 4.0, "t_in": time.time(),
             "el_in": 1300.0, "style": "settle", "tp": None,
             "selling": True, "exit_reason": "margin_stop"}
        self.hold(t, sib)
        self.hold(t, p)
        t._sell(p)
        self.assertTrue(self.orders, "the stop must post a sell")
        self.assertAlmostEqual(self.orders[0][2], 4.0,
                               msg="must sell our size, never the shared balance")
        self.assertAlmostEqual(sib["size"], 4.0, msg="the sibling is untouched")

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
        self.hold(t, sib)
        self.hold(t, p)
        t._manage_exits({"state": "in", "period": 3, "detail": "6:00",
                         "home_score": 70, "away_score": 72}, now)
        self.assertFalse(any(r.get("ev") == "reconcile_add" for r in self.logs))
        self.assertEqual(self.orders, [])
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
        self.hold(t, sib)
        self.fake.trades = [{"id": "tx", "status": "CONFIRMED", "side": "BUY",
                             "size": "4.0", "price": "0.40",
                             "match_time": str(int(time.time())),
                             "taker_order_id": "someone-else"}]
        self.assertEqual(t._fills("td", "BUY", set(), int(time.time()) - 5,
                                  "our-order"), [])
        # A sole holder still needs its original signed order ID.
        t.positions = []
        got = t._fills("td", "BUY", set(), int(time.time()) - 5, "our-order")
        self.assertEqual(got, [])
        self.assertEqual(len(t._fills("td", "BUY", set(), 0, "someone-else")), 1)

    # ---- the sell is checked against what its own order bought ----
    def test_sell_flags_a_size_beyond_its_own_fill(self):
        lr.book = lambda client, tok: (0.55, 0.50, "0.01")
        lr.market_order = self._order("SELL", fill=-1)
        lr.token_balance = lambda client, tok: 40.0

        def run(game, size, fill, adopted=0.0):
            self.logs = []
            t = lr.GameTrader(game, RES, "HOME", True)
            p = {"gid": game, "ch": "D", "token": "td", "outcome": "Dog",
                 "entry": 0.55, "size": size, "t_in": time.time(),
                 "el_in": 1300.0, "style": "settle", "tp": None,
                 "selling": True,
                 "fill_shares": fill, "adopted": adopted}
            self.hold(t, p, confirmed=fill)
            t._sell(p)
            return [r for r in self.logs if r.get("ev") == "size_unbacked"]

        self.assertEqual(run("owned", 4.0, 4.0), [])
        self.assertEqual(len(run("adopted", 6.0, 4.0, 2.0)), 1)
        flagged = run("unbacked", 6.0, 4.0)
        self.assertEqual(len(flagged), 1, "unbacked size must be logged")
        self.assertAlmostEqual(flagged[0]["fill_shares"], 4.0)
        self.assertEqual(len(self.orders), 1)

    # ---- a shared token must not make one channel sell another's shares ----
    def test_shared_token_never_reposts_an_unresolved_sale(self):
        lr.book = lambda client, tok: (0.55, 0.50, "0.01")
        lr.market_order = self._order()
        t = lr.GameTrader("gs", RES, "HOME", True)
        sib = dict(gid=t.gid, ch="A", token="td", outcome="Dog", entry=.40, size=4.,
                   el_in=100., style="process", tp=.20, selling=False)
        p = dict(sib, ch="D", style="settle", tp=None, selling=True, exit_reason="margin_stop")
        self.hold(t, sib)
        self.hold(t, p)
        for wallet in (8., 8., 4.):
            lr.token_balance = lambda client, tok: wallet
            t._sell(p)
        self.assertEqual(len(self.orders), 1)
        self.assertEqual((self.orders[0][2], sib["size"]), (4., 4.))

    def test_wallet_extra_never_increases_sole_position(self):
        lr.book = lambda client, tok: (0.60, 0.50, "0.01")
        lr.token_balance = lambda client, tok: 5.957
        lr.market_order = self._order("SELL", fill=-1)
        t = lr.GameTrader("gx", RES, "HOME", True)
        now = time.time()
        p = {"gid": "gx", "ch": "C", "token": "td", "outcome": "Dog",
             "entry": 0.40, "size": 2.979, "t_in": now - 10,
             "el_in": 100.0, "style": "process", "tp": 0.20, "selling": False}
        self.hold(t, p)
        t._manage_exits({"state": "in", "period": 2, "detail": "6:00",
                         "home_score": 50, "away_score": 50}, now)
        self.assertAlmostEqual(p["size"], 2.979, places=6)
        self.assertEqual(self.orders, [])

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
        self.hold(t, p)
        # A's buy for the same token is posted but its fill is not recorded yet
        t.inflight["A"] = {"token": "td", "ch": "A", "posted": True,
                           "outcome": "Dog"}
        t._manage_exits({"state": "in", "period": 2, "detail": "6:00",
                         "home_score": 50, "away_score": 50}, now)
        self.assertAlmostEqual(p["size"], 2.979, places=6)
        self.assertFalse(any(r.get("ev") == "reconcile_add" for r in self.logs))

    def test_finished_unsellable_position_handed_to_account(self):
        lr.book = lambda client, tok: (None, None, "0.01")   # no bid at all
        lr.token_balance = lambda client, tok: 7.36
        lr.market_order = self._order("SELL", fill=None)
        t = lr.GameTrader("gx", RES, "HOME", True)
        p = {"gid": "gx", "ch": "C", "token": "td", "outcome": "Dog",
             "entry": 0.50, "size": 7.36, "t_in": time.time(),
             "el_in": 100.0, "style": "process", "tp": 0.20,
             "selling": False}
        self.hold(t, p)
        t._manage_exits(dict(state="post", period=4, detail="0:00"), time.time())
        self.assertNotIn(p, t.positions)
        self.assertEqual([r["size"] for r in lr.LEDGER.tasks()], [7.36])
        self.assertEqual(self.orders, [])

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

    # ---- optional streamed top-of-book: ws uses it, observe only compares ----
    class _Books:
        def __init__(self, cached):
            self.cached_value = cached
            self.subscribed = []

        def subscribe(self, token, condition):
            self.subscribed.append((token, condition))

        def start(self):
            return False

        def cached(self, token):
            return self.cached_value

    def test_market_quote_ws_uses_cache_and_falls_back(self):
        books = self._Books(("td", .70, .30))
        old = (lr.MARKET_DATA, lr.MARKET_BOOKS, lr.book)
        self.addCleanup(lambda: setattr(lr, "MARKET_DATA", old[0]))
        self.addCleanup(lambda: setattr(lr, "MARKET_BOOKS", old[1]))
        self.addCleanup(lambda: setattr(lr, "book", old[2]))
        lr.MARKET_DATA, lr.MARKET_BOOKS = "ws", books
        lr.book = lambda client, token: (.90, .80, ".001")
        self.assertEqual(lr._market_quote(None, "td", "cond"), (.70, .30, "0.01"))
        books.cached_value = None
        self.assertEqual(lr._market_quote(None, "td", "cond"), (.90, .80, ".001"))
        self.assertEqual(books.subscribed, [("td", "cond"), ("td", "cond")])

    def test_market_quote_observe_uses_http_and_logs(self):
        books = self._Books(("td", .60, .40))
        old = (lr.MARKET_DATA, lr.MARKET_BOOKS, lr.book)
        self.addCleanup(lambda: setattr(lr, "MARKET_DATA", old[0]))
        self.addCleanup(lambda: setattr(lr, "MARKET_BOOKS", old[1]))
        self.addCleanup(lambda: setattr(lr, "book", old[2]))
        lr.MARKET_DATA, lr.MARKET_BOOKS = "observe", books
        lr.book = lambda client, token: (.70, .30, ".01")
        self.assertEqual(lr._market_quote(None, "td", "cond"), (.70, .30, ".01"))
        row = [r for r in self.logs if r.get("ev") == "market_observe"][0]
        self.assertEqual(row["ws"], [.60, .40])
        self.assertEqual(row["http"], [.70, .30])

    # ---- one line per game: wallet vs what the trader thinks it holds ----
    def test_reconcile_summary_reports_leftover_shares(self):
        self.enterContext(patch.object(lr, "RECONCILE_GRACE", 0))
        lr.token_balance = lambda client, tok: 3.5      # never attributed
        t = lr.GameTrader("gm", RES, "HOME", True)
        t.tokens_seen.add("td")
        t.log_reconcile()
        row = [r for r in self.logs if r.get("ev") == "reconcile_summary"][0]
        self.assertFalse(row["clean"])
        self.assertAlmostEqual(row["tokens"]["td"]["held"], 3.5)
        # a clean game says so and stays quiet about the numbers
        self.logs = []
        lr.token_balance = lambda client, tok: 0.0
        t.log_reconcile()
        row = [r for r in self.logs if r.get("ev") == "reconcile_summary"][0]
        self.assertTrue(row["clean"])


if __name__ == "__main__":
    unittest.main()
