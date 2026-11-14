"""Offline producer integration with a simulated local HTTP transport."""

import unittest
import json
from pathlib import Path
import tempfile
from unittest.mock import Mock

from executor_producer import GameSignals, run_recording


class ProducerTests(unittest.TestCase):
    def test_recorded_close_is_booked_before_advancing_source_clock(self):
        """A fast in-process replay must include exit proceeds in the next stake."""
        from unittest.mock import patch

        snapshots = [(10, 0, 100), (20, 1, 110), (30, 2, 120)]
        api = Mock()
        booked = []

        def read_intent(identifier):
            booked.append(identifier)
            return {"state": "FILLED"}

        api.intent.side_effect = read_intent
        api.finish.return_value = {"status": "MATCHED"}
        with tempfile.TemporaryDirectory() as tmp, patch("executor_producer.GameSignals") as signals:
            game = signals.return_value
            game.opened, game.close_intents = {}, {}

            def tick(elapsed, *args, **kwargs):
                if elapsed == 10:
                    game.opened["C"] = "entry"
                elif elapsed == 20:
                    game.close_intents["C"] = "exit"
                else:
                    self.assertIn("exit", booked)

            game.tick.side_effect = tick
            path = Path(tmp, "recording.json")
            path.write_text(json.dumps([dict(gid="g", fav="F", pre=.7, outcomes=["F","D"],
                                            prices={"F": [], "D": []}, evs=snapshots)]))
            with patch("builtins.print"):
                run_recording(api, path, None, None)

    def test_restart_preserves_amount_and_quote_after_lost_response(self):
        from urllib.error import URLError

        api = Mock(account="local", environment="paper")
        api.report.return_value = {"intents": []}
        decisions = []
        api.decisions.side_effect = lambda game: list(decisions)
        api.decision.side_effect = decisions.append
        api.submit.side_effect = [URLError("lost response"), {"id": "same-order"}]
        source = GameSignals("g", "m", 0.68, api, 2_000_000)
        with self.assertRaises(URLError):
            source.tick(2050, 6, 1000, {"fav": ("token", 0.75, 0.73)})
        recovered = GameSignals("g", "m", 0.68, api, 4_000_000)
        recovered.tick(2055, 6, 1010, {"fav": ("token", 0.74, 0.72)})
        calls = api.submit.call_args_list
        self.assertEqual(calls[0].args, calls[1].args)
        self.assertEqual(recovered.opened, {"D": "same-order"})

    def test_delayed_buy_is_posted_once_and_close_uses_confirmed_position(self):
        api = Mock(account="local", environment="paper")
        api.report.return_value = {"intents": []}
        api.decisions.return_value = []
        opened = {
            "id": "pos",
            "filled": 4_000_000,
            "cost": 3_000_000,
            "fee": 0,
            "state": "FILLED",
        }
        api.submit.return_value = opened
        api.intent.return_value = opened
        game = GameSignals("g", "m", 0.68, api, 2_000_000)

        def tick(el, margin):
            game.tick(
                el, margin, el, {"fav": ("tf", 0.75, 0.74), "dog": ("td", 0.25, 0.24)}
            )

        tick(1980, 3)
        tick(2040, 6)
        tick(2050, 7)
        tick(2070, -1)
        opens = [
            c.args[0]
            for c in api.submit.call_args_list
            if c.args[0]["action"] == "OPEN" and c.args[0]["channel"] == "D"
        ]
        closes = [
            c.args[0]
            for c in api.submit.call_args_list
            if c.args[0]["action"] == "CLOSE" and c.args[0]["channel"] == "D"
        ]
        self.assertEqual(len(opens), 1)
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0]["amount"], 4_000_000)
        self.assertEqual(closes[0]["position_id"], "pos")
        self.assertEqual(closes[0]["reason"], "margin_stop")


if __name__ == "__main__":
    unittest.main()
