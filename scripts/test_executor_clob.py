"""Offline contracts for the order bridge. These tests have no account credentials."""

import unittest
import importlib.util
import os
from unittest.mock import Mock, patch

import executor_clob as bridge


class BridgeContracts(unittest.TestCase):
    def test_exact_contract_wallet_and_order_log(self):
        fake_topic = "0x" + "1" * 64
        wallet = "0x" + "2" * 40
        contract = "0x" + "3" * 40
        order = "0x" + "4" * 64
        receipt = {
            "status": "0x1",
            "transactionHash": "0x" + "5" * 64,
            "logs": [
                {
                    "address": contract,
                    "logIndex": "0xf",
                    "topics": [
                        fake_topic,
                        order,
                        "0x" + "0" * 24 + wallet[2:],
                        "0x" + "0" * 64,
                    ],
                    "data": "0x"
                    + "".join(
                        f"{i:064x}" for i in (0, 713, 3000000, 4000000, 3000, 0, 0)
                    ),
                }
            ],
        }
        args = (receipt, wallet, {contract}, fake_topic, 123456789)
        self.assertEqual(
            bridge.confirmed_legs(*args),
            [
                {
                    "id": receipt["transactionHash"] + ":15",
                    "order_id": order,
                    "asset": "713",
                    "side": "BUY",
                    "price": 750000,
                    "quantity": 4000000,
                    "notional": 3000000,
                    "fee": 3000,
                    "time": 123456789,
                    "confirmed": True,
                }
            ],
        )
        self.assertEqual(
            bridge.confirmed_legs(receipt, contract, {contract}, fake_topic, 1), []
        )
        self.assertEqual(
            bridge.confirmed_legs(receipt, wallet, {wallet}, fake_topic, 1), []
        )

    def test_delayed_and_unknown_responses(self):
        oid = "0x" + "a" * 64
        self.assertEqual(
            bridge.classify_post(
                {"success": True, "status": "delayed", "orderID": oid}, oid
            )["state"],
            "ACCEPTED",
        )
        self.assertEqual(
            bridge.classify_post({"status": "failed"}, oid)["state"], "UNKNOWN"
        )
        self.assertEqual(
            bridge.classify_post({"success": True, "orderID": "different"}, oid)[
                "state"
            ],
            "UNKNOWN",
        )
        self.assertEqual(
            bridge.classify_post({"error": "FOK orders are filled or killed"}, oid)[
                "state"
            ],
            "REJECTED_ZERO",
        )
        self.assertEqual(
            bridge.classify_post({"error": "order already exists"}, oid)["state"],
            "UNKNOWN",
        )

    def test_lookup_cancellation_and_incomplete_details(self):
        order = {
            "id": "o",
            "asset_id": "713",
            "side": "BUY",
            "status": "CANCELED",
            "size_matched": "4",
        }
        c = Mock()
        c.get_order.return_value = order
        state = bridge.order_snapshot(c, "o", "713", "BUY", lambda _: [])
        self.assertEqual((state["state"], state["total_filled"]), ("FINAL", 4000000))
        order["status"] = "DELAYED"
        self.assertEqual(
            bridge.order_snapshot(c, "o", "713", "BUY", lambda _: [])["state"], "OPEN"
        )

    @unittest.skipUnless(
        importlib.util.find_spec("py_clob_client_v2"), "live SDK dependency"
    )
    def test_pinned_sdk_prepare_signs_real_payload_without_network(self):
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        from py_clob_client_v2.order_utils.exchange_order_builder_v2 import (
            ExchangeOrderBuilderV2,
        )
        from py_clob_client_v2.order_utils.model.order_data_v2 import SignedOrderV2
        from py_clob_client_v2.config import get_contract_config

        wallet = Account.create()
        with patch.dict(
            os.environ,
            {
                "POLY_FUNDER": wallet.address,
                "POLY_PRIVATE_KEY": wallet.key.hex(),
                "POLY_API_KEY": "fixture",
                "POLY_API_PASSPHRASE": "fixture",
                "POLY_API_SECRET": "fixture",
                "POLY_SIGNATURE_TYPE": "0",
            },
        ):
            c = bridge.client()
            c.get_order_book = lambda _: {
                "market": "market",
                "asset_id": "713",
                "asks": [{"price": ".73"}],
            }
            c.get_balance_allowance = lambda _: {
                "balance": "9000000",
                "allowance": "9000000",
            }
            # Exercise the SDK's real market info, caches, amount rounding, signing, and hash.
            c._get = lambda url, **kw: {
                "t": [{"t": "713"}],
                "mts": "0.01",
                "fd": {"r": 0.05, "e": 1},
            }
            c._post = Mock(side_effect=AssertionError("signing sent a POST"))
            intent = dict(
                account=wallet.address,
                environment="live",
                asset="713",
                market="market",
                action="OPEN",
                limit_price=750000,
                channel="D",
                reserved=4000000,
                pending_reservations=4000000,
            )
            p = bridge.prepare(c, intent)
            signed = SignedOrderV2(**p["signed"])
            domain = ExchangeOrderBuilderV2(
                get_contract_config(137).exchange_v2, 137, c.signer
            )
            data = domain.build_order_typed_data(signed)
            self.assertEqual(
                Account.recover_message(
                    encode_typed_data(full_message=data), signature=signed.signature
                ),
                wallet.address,
            )
            self.assertEqual(domain.build_order_hash(data), p["order_id"])
            self.assertGreater(int(signed.makerAmount), 0)
            self.assertLess(int(signed.makerAmount), 4000000)
            self.assertEqual(
                bridge.prepare(c, {**intent, "pending_reservations": 10000000})[
                    "reject"
                ],
                "insufficient venue balance or allowance",
            )
            c._post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
