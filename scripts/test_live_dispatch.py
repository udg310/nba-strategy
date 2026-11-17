"""CLI isolation and pre-POST checks using an offline exchange and ephemeral keys."""
import contextlib
import importlib.util
import io
import itertools
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

import executor_clob
import live_runner as lr


RES = {"fav": "Fav", "outcomes": ["Fav", "Dog"], "tokens": ["712", "713"],
       "pre": .7, "market": "demo"}
HAS_SDK = importlib.util.find_spec("py_clob_client_v2") is not None


class DryRunTests(unittest.TestCase):
    def test_default_and_dry_cli_isolate_account_and_logs(self):
        for args in ([], ["--dry-run"]):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as root:
                daily = Path(root, "data", "live_trade_final", lr.TODAY)
                old = daily / "prior" / "events.jsonl"
                old.parent.mkdir(parents=True)
                old.write_text(json.dumps(dict(ev="buy", gid="g", ch="C", outcome="Dog",
                                               entry=.40, filled_shares=4, el_in=100)))
                live_db = Path(root, "var", "live_runner", "attempts.db")
                ledger = lr.AttemptLedger(str(live_db), "fixture")
                ledger.begin("g", "C", "OPEN", "old", 1)
                ledger.close_path()
                original = live_db.read_bytes()
                g = dict(gid="g", state="in", start="", period=4, detail="5:00",
                         home="HOME", away="AWAY", home_score=99, away_score=98)
                public = Mock()
                forbidden = Mock(side_effect=AssertionError("private client in dry mode"))
                with (patch.object(sys, "argv", ["live_runner.py", *args]),
                      patch.object(lr, "ROOT", root),
                      patch.object(lr, "OUTDIR", str(daily / "current")),
                      patch.object(lr, "make_public_client", create=True, return_value=public),
                      patch.object(lr, "make_client", forbidden),
                      patch.object(lr, "resolve", return_value=RES),
                      patch.object(lr, "scoreboard", side_effect=itertools.chain(
                          [[g]], itertools.repeat([{**g, "state": "post"}], 500),
                          [SystemExit("dry-run never released its trader")])),
                      patch.object(lr, "POLL", .001),
                      contextlib.redirect_stdout(io.StringIO())):
                    lr.main()
                self.assertEqual(live_db.read_bytes(), original)
                self.assertEqual(list(daily.glob("*/events.jsonl")), [old])
                self.assertTrue(list(Path(root, "data", "dry_run").glob("*/*/events.jsonl")))
                public.get_balance_allowance.assert_not_called()
                public.create_market_order.assert_not_called()
                public.post_order.assert_not_called()

    def test_dry_trader_refuses_buy_and_sell_even_with_authenticated_client(self):
        private = Mock()
        with (patch.object(lr, "make_client", return_value=private),
              patch.object(lr, "make_public_client", create=True, return_value=private)):
            trader = lr.GameTrader("g", RES, "HOME", False)
            self.assertEqual(trader._post_buy({})[0], "rejected")
            self.assertEqual(trader._post_sell({})[0], "rejected")
        self.assertEqual(private.mock_calls, [])

    def test_dispatch_requires_explicit_live_mode(self):
        private = Mock()
        with self.assertRaises(PermissionError):
            lr.market_order(private, "713", "SELL", 2, .53, ".01")
        self.assertEqual(private.mock_calls, [])


@unittest.skipUnless(HAS_SDK, "pinned SDK required for offline signing")
class SignedDispatchTests(unittest.TestCase):
    def setUp(self):
        from eth_account import Account
        from py_clob_client_v2.client import ClobClient
        self.wallet = Account.create()
        self.client = ClobClient("https://fixture.invalid", chain_id=137,
                                 key=self.wallet.key.hex(), signature_type=0)
        self.negative = False
        self.version = 2
        self.book_tick = "0.01"
        self.ask = ".53"
        self.cash = 30.
        self.venue_ids = []
        self.signed_prices = []
        self.gid = "g"
        self.client.get_order_book = lambda token: dict(
            asks=[{"price": self.ask}], bids=[], tick_size=self.book_tick)
        self.client.get_balance_allowance = lambda params: {"balance": str(int(self.cash * 1e6))}
        def fixture_get(url, **kwargs):
            if "neg-risk" in url:
                return {"neg_risk": self.negative}
            if "markets-by-token" in url:
                return {"condition_id": "demo"}
            if url.endswith("/version"):
                return {"version": self.version}
            if "clob-markets" in url:
                return {"t": [{"t": "713"}], "nr": self.negative, "mts": self.book_tick}
            raise AssertionError("unexpected SDK GET " + url)
        self.client._get = fixture_get
        self.client.post_order = self.venue_post
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = lr.AttemptLedger(str(Path(self.tmp.name) / "attempts.db"), "fixture")
        self.addCleanup(self.ledger.close_path)
        self.enterContext(patch.object(lr, "LEDGER", self.ledger))
        self.enterContext(patch.object(lr, "make_client", return_value=self.client))
        self.enterContext(patch.object(lr, "log"))
        self.trader = lr.GameTrader(self.gid, RES, "HOME", True)
        self.order = dict(token="713", ch="C", outcome="Dog", limit=.53, usd=3.73,
                          deadline=time.time() + 60)

    def venue_post(self, signed, order_type):
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        from py_clob_client_v2.config import get_contract_config
        from py_clob_client_v2.order_utils.exchange_order_builder_v2 import ExchangeOrderBuilderV2
        contracts = get_contract_config(137)
        exchange = contracts.neg_risk_exchange_v2 if self.negative else contracts.exchange_v2
        if self.version == 3:
            exchange = contracts.exchange_v3
        domain = ExchangeOrderBuilderV2(exchange, 137, self.client.signer,
                                        domain_version=str(self.version))
        data = domain.build_order_typed_data(signed)
        signer = Account.recover_message(encode_typed_data(full_message=data),
                                         signature=signed.signature)
        self.assertEqual(signer, self.wallet.address)
        oid = domain.build_order_hash(data)
        self.assertEqual(lr.LEDGER.rows(self.gid)[0]["order_id"], oid)
        self.venue_ids.append(oid)
        self.signed_prices.append(Decimal(signed.makerAmount) / Decimal(signed.takerAmount))
        return {"success": True, "orderID": oid}

    def assert_accepted(self):
        status, info = self.trader._post_buy(self.order)
        self.assertEqual((status, info.get("state")), ("posted", "ACCEPTED"), info)
        self.assertEqual(self.venue_ids, [info["order_id"]])
        self.assertLessEqual(self.signed_prices[0], Decimal(str(self.order["limit"])))
        self.assertEqual(executor_clob.classify_post({"success": True, "orderID": "wrong"},
                                                   info["order_id"])["state"], "UNKNOWN")

    def test_normal_market_id_and_exact_signed_limit_at_signal_ceiling(self):
        self.assert_accepted()

    def test_live_cli_posts_one_signed_order_with_account_lock(self):
        g = dict(gid=self.gid, state="in", start="", period=2, detail="6:00",
                 home="HOME", away="AWAY", home_score=54, away_score=51)
        timeout = time.monotonic() + 4
        def scoreboard():
            if time.monotonic() > timeout:
                raise SystemExit("live fixture did not release its trader")
            return [{**g, "state": "post" if self.venue_ids else "in"}]

        def signal(trader, game, now):
            if game["state"] == "in" and not trader.inflight:
                trader.inflight["C"] = dict(self.order, posted=False, tries=0, style="process",
                                             tp=.2, t0=int(now), el_sig=900)

        self.client.get_order = lambda _: None
        self.client.get_trades = lambda _: []
        with (patch.object(sys, "argv", ["live_runner.py", "--live"]),
              patch.object(lr, "ROOT", self.tmp.name),
              patch.object(lr, "load_env", return_value={"POLY_FUNDER": self.wallet.address}),
              patch.object(lr, "account_lock", return_value=contextlib.nullcontext()) as lock,
              patch.object(lr, "scoreboard", side_effect=scoreboard),
              patch.object(lr, "resolve", return_value=RES), patch.object(lr, "BOARD_ENABLED", False),
              patch.object(lr, "POLL", .005), patch.object(lr.GameTrader, "_find_signals", signal)):
            lr.main()
        lock.assert_called_once_with(self.wallet.address)
        self.assertEqual(len(self.venue_ids), 1)
        self.assertLessEqual(self.signed_prices[0], Decimal(".53"))

    def test_v3_uses_v3_contract_even_for_negative_risk_market(self):
        self.version, self.negative = 3, True
        self.assert_accepted()

    def test_unsupported_contract_version_refuses_to_sign(self):
        self.version = 4
        with patch.object(self.client, "create_market_order", wraps=self.client.create_market_order) as sign:
            self.assertEqual(self.trader._post_buy(self.order)[0], "rejected")
            sign.assert_not_called()

    def test_sdk_400_documented_fok_miss_releases_cash(self):
        import httpx
        from py_clob_client_v2.exceptions import PolyApiException
        response = httpx.Response(400, json={"error": "FOK orders are filled or killed"})
        self.client.post_order = Mock(side_effect=PolyApiException(response))
        status, info = self.trader._post_buy(self.order)
        self.assertEqual((status, info.get("retryable")), ("rejected", True))
        self.assertEqual(self.ledger.rows(self.gid)[0]["state"], lr.REJECTED_ZERO)
        self.assertEqual(self.ledger.exposure()[1], 0)

    def test_sdk_400_other_error_preserves_original_reservation(self):
        import httpx
        from py_clob_client_v2.exceptions import PolyApiException
        response = httpx.Response(400, json={"error": "duplicate order"})
        self.client.post_order = Mock(side_effect=PolyApiException(response))
        self.assertEqual(self.trader._post_buy(self.order)[0], "unknown")
        self.assertEqual(self.ledger.rows(self.gid)[0]["state"], lr.UNKNOWN)
        self.assertEqual(self.ledger.exposure()[1], self.order["usd"])

    def test_negative_risk_market_id_and_finer_book_tick(self):
        self.negative, self.book_tick, self.ask = True, "0.001", ".529"
        self.order["limit"] = .537
        self.assert_accepted()
        self.assertLess(self.signed_prices[0], Decimal(".531"))

    def test_cannot_cross_frozen_or_off_tick_limit(self):
        for ask, ceiling in [(".54", .53), (".534", .537)]:
            self.ask, self.order["limit"], self.book_tick = ask, ceiling, "0.001"
            self.assertEqual(self.trader._post_buy(self.order)[0], "rejected")
        self.assertEqual(self.venue_ids, [])
        self.assertEqual(self.ledger.rows(self.gid), [])

    def test_signing_crosses_deadline(self):
        create = self.client.create_market_order
        def sign(*args):
            signed = create(*args)
            clock.return_value = self.order["deadline"] + 1
            return signed
        self.client.create_market_order = sign
        with patch.object(lr.time, "time", return_value=time.time()) as clock:
            self.assertEqual(self.trader._post_buy(self.order)[0], "rejected")
        self.assertEqual(self.venue_ids, [])
        self.assertEqual(self.ledger.rows(self.gid), [])

    def test_sdk_server_time_read_cannot_delay_post_past_deadline(self):
        import httpx
        from py_clob_client_v2.clob_types import ApiCreds
        from py_clob_client_v2.http_helpers import helpers
        self.client.set_api_creds(ApiCreds("test", "Zml4dHVyZQ==", "test"))
        self.client.use_server_time = True
        del self.client.post_order  # exercise the SDK's actual headers and HTTP calls
        methods = []
        def venue(request):
            methods.append(request.method)
            if request.url.path == "/time":
                clock.return_value = self.order["deadline"] + 1
                return httpx.Response(200, json={"time": int(clock.return_value)})
            return httpx.Response(200, json={"success": True})
        with (httpx.Client(transport=httpx.MockTransport(venue)) as http,
              patch.object(helpers, "_http_client", http),
              patch.object(lr.time, "time", return_value=time.time()) as clock):
            self.assertEqual(self.trader._post_buy(self.order)[0], "rejected")
        self.assertEqual(methods, ["GET"])
        self.assertEqual(self.ledger.rows(self.gid)[0]["state"], lr.REJECTED_ZERO)

    def test_cash_reserved_by_another_game_cannot_be_spent(self):
        self.cash = 5.
        self.ledger.begin("other", "C", "OPEN", "inflight", 1, reserved_usd=4.)
        self.assertEqual(self.trader._post_buy(self.order)[0], "rejected")
        self.assertEqual(self.venue_ids, [])
        self.assertEqual(self.ledger.rows(self.gid), [])
