"""Offline market-stream and score-observer contract tests."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from market_ws import MarketBooks
from relay_observer import RelayObserver
from executor_producer import run_scoreboard


class MarketStreamTests(unittest.TestCase):
    def test_snapshot_delta_and_stale_http_fallback(self):
        now = [10.0]
        reads = []

        def http(token):
            reads.append(token)
            return "condition", (token, 0.8, 0.2)

        stream = MarketBooks("ws", http_quote=http, clock=lambda: now[0])
        stream.subscribe("token", "condition")
        stream.ingest({"event_type": "price_change", "market": "condition",
                       "price_changes": [{"asset_id": "token", "price": ".3",
                                          "size": "5", "side": "BUY"}]})
        self.assertEqual(stream.quote("token", "condition")[1][1:], (0.8, 0.2))
        stream.ingest({"event_type": "book", "asset_id": "token", "market": "condition",
                       "timestamp": "100", "bids": [{"price": ".3", "size": "4"}],
                       "asks": [{"price": ".7", "size": "2"}]})
        self.assertEqual(stream.quote("token", "condition")[1][1:], (0.7, 0.3))
        stream.ingest({"event_type": "price_change", "market": "condition",
                       "timestamp": "101", "price_changes": [
                           {"asset_id": "token", "price": ".3", "size": "0", "side": "BUY",
                            "best_bid": ".4", "best_ask": ".7"}]})
        self.assertEqual(stream.quote("token", "condition")[1][1:], (0.8, 0.2))
        stream.ingest({"event_type": "book", "asset_id": "token", "market": "condition",
                       "timestamp": "102", "bids": [{"price": ".4", "size": "4"}],
                       "asks": [{"price": ".7", "size": "2"}]})
        now[0] += 4
        self.assertEqual(stream.quote("token", "condition")[1][1:], (0.8, 0.2))
        self.assertEqual(reads, ["token", "token", "token"])

    def test_observe_never_trades_on_stream_and_disconnect_clears_book(self):
        stream = MarketBooks("observe", http_quote=lambda t: ("c", (t, .8, .2)))
        stream.subscribe("t", "c")
        stream.ingest({"event_type": "book", "asset_id": "t", "market": "c",
                       "bids": [{"price": ".3", "size": "1"}],
                       "asks": [{"price": ".7", "size": "1"}]})
        self.assertEqual(stream.quote("t", "c")[1][1:], (.8, .2))
        stream._on_close(None)
        self.assertIsNone(stream.cached("t"))

    def test_book_validates_levels_and_condition_case(self):
        stream = MarketBooks("ws", http_quote=lambda t: ("c", (t, .8, .2)))
        stream.subscribe("t", "ABC")
        stream.ingest({"event_type": "book", "asset_id": "t", "market": "abc",
                       "bids": [{"price": ".3", "size": "1"}],
                       "asks": [{"price": ".7", "size": "1"}]})
        self.assertEqual(stream.cached("t")[1:], (.7, .3))
        stream.ingest({"event_type": "book", "asset_id": "t", "market": "abc",
                       "bids": [{"price": "nan", "size": "1"}],
                       "asks": [{"price": ".7", "size": "1"}]})
        self.assertIsNone(stream.cached("t"))

    def test_relay_records_matching_score_without_driving_strategy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "scores.jsonl")
            now = [10.0]
            relay = RelayObserver("https://example.com", path, clock=lambda: now[0])
            snap = {"gid": "1", "period": 2, "home_score": 53,
                    "away_score": 50, "state": "in"}
            payload = {"v": 1, "id": "1", "p": 2, "st": "in", "src": "patch",
                       "home": {"sc": 53}, "away": {"sc": 50}, "ts": 123}
            relay.ingest("1", {"event": "message", "message": json.dumps(payload)})
            now[0] = 12.0
            relay.observe_http(snap, {"fav": ("t", .7, .6)})
            relay.observe_http(snap, {})
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[-1]["http_minus_relay_ms"], 2000)
            self.assertEqual(rows[-1]["quotes"], {"fav": [.7, .6]})

    def test_scoreboard_uses_market_mode_but_only_http_score_for_strategy(self):
        snap = dict(gid="1", state="in", period=2, detail="6:00", home="H",
                    away="A", home_score=53, away_score=50)
        resolved = dict(pre=.7, fav="H", outcomes=["H", "A"], tokens=["fav", "dog"])
        books, relay, api = Mock(), Mock(), Mock()
        books.quote.side_effect = lambda token, condition: (
            "condition", (token, .75 if token == "fav" else .25, .7))
        with patch("live_runner.scoreboard", return_value=[snap]), patch(
                "live_runner.resolve", return_value=resolved), patch(
                "live_runner.outcome_abbr", return_value="H"), patch(
                "executor_producer.public_quote", return_value=("condition", ("fav", .75, .7))), patch(
                "executor_producer.GameSignals") as signals, patch(
                "executor_producer.nd.elapsed_of", return_value=1000), patch(
                "executor_producer.time.sleep", side_effect=StopIteration):
            signals.return_value.market = "condition"
            with self.assertRaises(StopIteration):
                run_scoreboard(api, None, 3, books, relay)
        self.assertEqual(books.subscribe.call_count, 2)
        self.assertEqual(books.quote.call_count, 2)
        self.assertEqual(signals.return_value.tick.call_args.kwargs["score"],
                         {"home": 53, "away": 50})
        relay.observe_http.assert_called_once()


if __name__ == "__main__":
    unittest.main()
