"""Accounting examples from the 稻香 contract, including incomplete source coverage."""

import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from dashboard_model import baselines, game_results, project


MIDNIGHT = datetime(2026, 11, 15, 16, tzinfo=timezone.utc)
NOW = MIDNIGHT + timedelta(hours=4)


def stamp(t):
    return t.isoformat().replace("+00:00", "Z")


def event(id, kind, qty, gross, fee, at=NOW, pid="P", **fields):
    return dict(
        dict(
            id=id,
            kind=kind,
            position_id=pid,
            asset_id=pid,
            channel="A",
            game_id="G",
            game_label="G",
            market="M",
            outcome="W",
            order_id=None,
            fill_id=id,
            source="executor",
            time=stamp(at),
            recorded_at=stamp(at),
            revised_at=None,
            time_kind="simulated",
            evidence="confirmed",
            quantity=qty,
            price=None,
            gross=gross,
            fee=fee,
            fee_unit="cash",
            net_cash=None,
            issues=[],
        ),
        **fields
    )


def book(*activity, **fields):
    return dict(
        dict(
            activity=list(activity),
            timeline={},
            meta={},
            known_origin=True,
            coverage_from=stamp(MIDNIGHT - timedelta(days=2)),
            sources=[],
            available_cash="100",
            account_label="test",
        ),
        **fields
    )


def quote(bid, at=NOW, source=None):
    return dict(bid=bid, ask="0.8", observed_at=stamp(at), source_at=stamp(source or at))


class ProjectionTests(unittest.TestCase):
    def test_replaying_projected_activity_preserves_order_with_equal_timestamps(self):
        trades = [event("a", "buy", "10", "4", "0"), event("z", "sell", "8", "6", "0")]
        data = book(*trades)
        view = project(data, {"P": quote(".5")}, NOW)
        replay = project(dict(data, activity=view["activity"]), {"P": quote(".5")}, NOW)
        self.assertEqual(replay["snapshot"]["summary"], view["snapshot"]["summary"])
        self.assertEqual(replay["timeline"], view["timeline"])

    def setUp(self):
        self.buy = event("1", "buy", "10", "4.00", "0.10", at=NOW - timedelta(seconds=3))
        self.sell = event("2", "sell", "4", "2.40", "0.04", at=NOW - timedelta(seconds=2))
        self.redeem = event("3", "settlement", "6", "6", ".06")

    def test_examples_and_pending_settlement(self):
        data = book(self.sell, self.buy, meta={"P": {"pending_settlement": True}})
        report = project(data, {"P": quote(".5")}, NOW)
        (p,) = report["snapshot"]["open_positions"]
        self.assertEqual(
            (p["quantity"], p["remaining_cost"], p["state"]), ("6", "2.46", "pending_settlement")
        )
        self.assertEqual(
            [p[k]["value"] for k in ("realized", "unrealized", "today", "total")],
            ["0.72", "0.54", "1.26", "1.26"],
        )
        self.assertEqual(report["activity"][0]["net_cash"], "2.36")
        self.assertEqual(report["timeline"]["P"][0]["activity"]["id"], "2")
        detail = report["timeline"]["P"][0]
        self.assertEqual(
            (detail["remaining_quantity"], detail["remaining_cost"], detail["realized_change"]),
            ("6", "2.46", "0.72"),
        )
        settled = project(book(self.buy, self.sell, self.redeem), {}, NOW)
        self.assertEqual(settled["snapshot"]["summary"]["total"]["value"], "4.2")
        self.assertTrue(settled["positions"][0]["active_today"])
        self.assertEqual(settled["snapshot"]["open_positions"], [])
        self.redeem.update(gross="0", fee="0")
        self.assertEqual(
            project(book(self.buy, self.sell, self.redeem), {}, NOW)["positions"][0]["realized"][
                "value"
            ],
            "-1.74",
        )

    def test_share_fees_and_weighted_average_after_partial_exit(self):
        self.buy.update(fee="2", fee_unit="shares", price="0.4")
        self.sell.update(fee="0")
        another = event("3", "buy", "4", "3.20", "0", price=".80")
        p = project(book(self.buy, self.sell, another), {"P": quote("0.8")}, NOW)["positions"][0]
        self.assertEqual((p["quantity"], p["remaining_cost"], p["unit_cost"]), ("8", "5.2", "0.65"))
        self.assertEqual((p["realized"]["value"], p["unrealized"]["value"]), ("0.4", "1.2"))
        self.assertAlmostEqual(Decimal(p["avg_entry_price"]), Decimal("7.2") / Decimal(14))

    def test_midnight_with_no_trades_uses_only_archived_bid(self):
        day = timedelta(days=1)
        self.buy.update(time=stamp(NOW - day))
        self.sell.update(time=stamp(NOW - day + timedelta(seconds=1)))
        data = book(self.sell, self.buy)
        opening = baselines(data, {"P": quote(".50", MIDNIGHT - timedelta(seconds=1))}, MIDNIGHT)
        p = project(data, {"P": quote(".45")}, NOW, opening)["positions"][0]
        self.assertEqual(p["today"]["value"], "-0.3")
        self.assertTrue(p["active_today"])
        self.assertEqual(project(data, {"P": quote(".45")}, NOW)["positions"][0]["today"]["value"], None)
        opening = baselines(data, {"P": quote(".50", MIDNIGHT + timedelta(seconds=1))}, MIDNIGHT)
        self.assertIsNone(opening["P"]["value"])
        data["activity"].append(event("3", "settlement", "6", "6", "0", at=MIDNIGHT))
        result = project(data, {}, NOW, baselines(data, {"P": quote(".50", MIDNIGHT)}, MIDNIGHT))
        self.assertEqual(result["positions"][0]["today"]["value"], "3")
        self.assertEqual([a["id"] for a in result["snapshot"]["today_activity"]], ["3"])

    def test_closed_before_today_needs_no_archived_quote(self):
        data = book(self.buy, self.sell, self.redeem)
        result = project(data, {}, NOW + timedelta(days=1))
        self.assertEqual(result["positions"][0]["today"]["value"], "0")
        self.assertFalse(result["positions"][0]["active_today"])
        self.assertEqual(result["snapshot"]["summary"]["today"]["value"], "0")

    def test_new_position_can_use_resolution_without_a_book_or_redemption(self):
        self.buy["opens_position"] = True
        data = book(self.buy, known_origin=False, payouts={"P": {"price": "1", "effective_at": stamp(NOW)}})
        view = project(data, {}, NOW)
        p = view["positions"][0]
        self.assertEqual((p["today"]["value"], p["realized"]["value"], p["quantity"]), ("5.9", "0", "10"))
        self.assertEqual((p["quote"]["status"], p["market_value"]), ("resolved", "10"))
        self.assertEqual(len(view["activity"]), 1)
        data["payouts"]["P"]["price"] = "0"
        self.assertEqual(project(data, {}, NOW)["positions"][0]["today"]["value"], "-4.1")
        self.assertIsNone(project(data, {}, NOW-timedelta(seconds=1))["positions"][0]["market_value"])

    def test_recorded_buy_price_survives_an_undistributed_exit(self):
        self.buy["price"] = "0.4"
        data = book(self.buy, meta={"P":{"issues":[{"code":"shared_exit","position_id":"P"}]}})
        p = project(data, {}, NOW)["positions"][0]
        self.assertEqual(p["avg_entry_price"], "0.4")
        self.assertIsNone(p["realized"]["value"])

    def test_joint_sale_counts_once_and_requires_balance_evidence(self):
        def fill(i, pid, kind, qty, gross):
            return event(i, kind, qty, gross, "0", pid=pid, source="runner", evidence="aggregate",
                         channel=pid, asset_id="asset", opens_position=kind == "buy")
        data = book(fill("1", "A", "buy", "4", "2"), fill("2", "C", "buy", "6", "1.8"),
                    fill("3", "C", "sell", "9", "5.4"), timeline={
                        "C": [dict(id="add", time=stamp(NOW), type="revision", state="reconcile_add",
                                   added_quantity="4", observed_quantity="10", accounted_quantity="6")],
                        "A": [dict(id="close", time=stamp(NOW), type="revision", state="reconcile_closed",
                                   observed_quantity="1")]})
        view = project(data, {"asset": quote(".5")}, NOW)
        self.assertEqual(view["snapshot"]["summary"]["total"]["value"], "2.1")
        self.assertEqual(view["snapshot"]["summary"]["total"]["covered_count"], 2)
        self.assertEqual(game_results(data, view)["G"]["total"]["value"], "2.1")
        self.assertEqual(view["snapshot"]["summary"]["today"]["value"], "2.1")
        for p in view["positions"]:
            self.assertEqual(p["shared_exit"]["quantity"], "1")
            self.assertIsNone(p["total"]["value"])
            self.assertEqual(p["issues"][0]["code"], "shared_exit")
        data["timeline"]["A"][0]["observed_quantity"] = "2"
        view = project(data, {"asset": quote(".5")}, NOW)
        self.assertIsNone(view["positions"][0]["shared_exit"])
        self.assertIsNone(view["snapshot"]["summary"]["total"]["value"])

    def test_partial_missing_fee_stale_no_bid_and_unattributed(self):
        other = event("a", "buy", "3", "1", None, pid="A")
        old = event("b", "buy", "2", "1", "0", pid="B")
        review = event("c", "buy", "7", "1", "0", pid=None)
        result = project(
            book(self.buy, other, old, review),
            {"P": quote(".5"), "A": quote(None), "B": quote(".5", source=NOW - timedelta(seconds=15))},
            NOW,
        )
        summary = result["snapshot"]["summary"]["total"]
        self.assertIsNone(summary["value"])
        self.assertEqual((summary["known_subtotal"], summary["covered_count"]), ("0.9", 1))
        self.assertTrue(
            {"missing_fee", "stale_quote", "unattributed", "no_bid"}
            <= {i["code"] for i in summary["issues"]}
        )
        self.assertEqual(len(result["snapshot"]["today_activity"]), 4)

    def test_missing_origin_and_order_only_do_not_create_verified_profit(self):
        raw = book(known_origin=False, coverage_from=None, meta={"pending": {"reserved_quantity": None}})
        summary = project(raw, {}, NOW)["snapshot"]["summary"]
        self.assertIsNone(summary["total"]["value"])
        self.assertEqual(summary["open_count"], 0)
        self.assertEqual(project(book(), {}, NOW)["snapshot"]["summary"]["total"]["value"], "0")

    def test_metadata_gap_excludes_known_numbers_but_archived_fills_replay_cleanly(self):
        data = book(self.buy, meta={"P": {"issues": [{"code": "source_gap", "position_id": "P"}]}})
        p = project(data, {"P": quote("0.5")}, NOW)["positions"][0]
        self.assertIsNone(p["total"]["value"])
        self.assertEqual(p["state"], "review")
        midnight = NOW + timedelta(seconds=3)
        self.assertEqual(baselines(data, {"P": quote(".5")}, midnight)["P"]["value"], "0.9")

    def test_pending_order_in_finished_game_never_counts_as_holding(self):
        data = book(meta={"void": {"pending_settlement": True}})
        result = project(data, {}, NOW)
        self.assertEqual(result["snapshot"]["summary"]["total"]["total_count"], 0)
        self.assertEqual(game_results(data, result)[None]["position_count"], 0)

    def test_estimated_entry_fee_flows_into_realized_and_marked_metrics(self):
        self.buy.update(fee="0.12", fee_basis="estimated_taker")
        data = book(self.buy, self.sell)
        view = project(data, {"P": quote(".5")}, NOW)
        realized = view["snapshot"]["summary"]["realized"]
        self.assertEqual((realized["value"], realized["status"]), ("0.712", "estimated"))
        total = game_results(data, view)["G"]["total"]
        self.assertEqual((total["value"], total["status"], total["includes_estimated_fee"]),
                         ("1.24", "marked", True))

    def test_before_fee_estimates_are_separate_from_net_pnl(self):
        self.buy.update(fee=None, fee_unit=None, evidence="aggregate")
        result = project(book(self.buy, self.sell), {"P": quote(".5")}, NOW)
        (p,) = result["positions"]
        self.assertIsNone(p["total"]["value"])
        self.assertEqual(p["before_fees"], {"realized": "0.8", "unrealized": "0.6", "total": "1.4"})
        self.assertEqual(result["snapshot"]["summary"]["total"]["known_subtotal"], None)

    def test_game_results_count_closed_and_unknown_rows_without_inventing_positions(self):
        other = event("z", "buy", "2", "1", "0", pid=None, game_id="other")
        data = book(self.buy, self.sell, self.redeem, other)
        games = game_results(data, project(data, {}, NOW))
        self.assertEqual(
            (games["G"]["total"]["value"], games["G"]["trade_count"], games["G"]["position_count"]),
            ("4.2", 3, 1),
        )
        self.assertIsNone(games["other"]["realized"]["value"])
        self.assertEqual(games[None]["total"]["value"], "0")
        other.update(game_id=None)
        games = game_results(data, project(data, {}, NOW))
        self.assertIsNone(games[None]["total"]["value"])
        self.assertEqual(games["G"]["realized"]["known_subtotal"], "4.2")

    def test_corrected_fee_replaces_original_and_is_on_timeline(self):
        corrected = dict(self.buy, fee="0.2", revised_at=stamp(NOW))
        data = book(corrected, self.sell, self.buy)
        result = project(data, {"P": quote(".5")}, NOW)
        self.assertEqual(result["positions"][0]["total"]["value"], "1.16")
        items = result["timeline"]["P"]
        self.assertEqual([i["type"] for i in items], ["revision", "fill", "fill"])
        self.assertEqual(len(result["activity"]), 2)

    def test_historical_cutoff_before_coverage_and_empty_source_quality(self):
        data = book(coverage_from=stamp(NOW + timedelta(days=1)))
        self.assertIsNone(project(data, {}, NOW)["snapshot"]["summary"]["total"]["value"])
        data = book(sources=[{"name": "executor", "status": "down"}])
        view = project(data, {}, NOW)
        self.assertIsNone(game_results(data, view)[None]["total"]["value"])


if __name__ == "__main__":
    unittest.main()
