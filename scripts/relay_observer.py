"""Compare BrightSports ntfy snapshots with ESPN HTTP, without trading on them."""

from collections import defaultdict
import json
import logging
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit


class RelayObserver:
    def __init__(self, url, output=Path("var/executor/score_observations.jsonl"),
                 clock=time.monotonic):
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("NBA_BRIGHTSPORTS_URL must be an HTTP(S) origin")
        self.origin = url.rstrip("/")
        self.output, self.clock = Path(output), clock
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.threads = {}
        self.relay = defaultdict(dict)
        self.http = defaultdict(dict)
        self.markets = {}
        self.output.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(period, home, away, state):
        return int(period), int(home), int(away), str(state)

    def _write(self, row):
        with self.lock:
            with self.output.open("a", encoding="utf-8") as file:
                file.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    def ingest(self, game, envelope):
        if envelope.get("event") != "message":
            return
        try:
            snap = json.loads(envelope["message"])
            if str(snap["id"]) != str(game) or snap.get("v") != 1:
                return
            key = self.key(snap["p"], snap["home"]["sc"], snap["away"]["sc"], snap["st"])
        except (KeyError, TypeError, ValueError):
            return
        arrival = self.clock()
        with self.lock:
            if key in self.relay[game]:
                return
            self.relay[game][key] = arrival
            http_at = self.http[game].get(key)
            market = self.markets.get(game)
        quotes = {}
        if market:
            tokens, books = market
            if books:
                quotes = {side: list(value[1:]) for side, token in tokens.items()
                          if (value := books.cached(token)) is not None}
        self._write(dict(source="relay", game=game, key=key, monotonic=arrival,
                         relay_source=snap.get("src"), relay_ts=snap.get("ts"),
                         quotes=quotes,
                         http_minus_relay_ms=round((http_at - arrival) * 1000) if http_at else None))

    def observe_http(self, snap, quotes, arrival=None):
        game = str(snap["gid"])
        try:
            key = self.key(snap["period"], snap["home_score"], snap["away_score"], snap["state"])
        except (KeyError, TypeError, ValueError):
            return
        arrival = self.clock() if arrival is None else arrival
        with self.lock:
            if key in self.http[game]:
                return
            self.http[game][key] = arrival
            relay_at = self.relay[game].get(key)
        self._write(dict(source="http", game=game, key=key, monotonic=arrival,
                         http_minus_relay_ms=round((arrival - relay_at) * 1000) if relay_at else None,
                         quotes={side: list(q[1:]) for side, q in quotes.items()}))

    def register_market(self, game, tokens, books):
        with self.lock:
            self.markets[str(game)] = (dict(tokens), books)

    def subscribe(self, game):
        game = str(game)
        with self.lock:
            if game in self.threads:
                return
            thread = threading.Thread(target=self._listen, args=(game,),
                                      name=f"relay-{game}", daemon=True)
            self.threads[game] = thread
            thread.start()

    def _listen(self, game):
        import websocket
        origin = urlsplit(self.origin)
        scheme = "wss" if origin.scheme == "https" else "ws"
        url = f"{scheme}://{origin.netloc}{origin.path}/bs-{game}/ws"
        proxy = urlsplit(os.environ.get("NBA_PROXY", "http://127.0.0.1:9567"))
        options = {"ping_interval": 20, "ping_timeout": 10}
        if proxy.hostname and origin.hostname not in ("127.0.0.1", "localhost", "::1"):
            options.update(http_proxy_host=proxy.hostname, http_proxy_port=proxy.port or 80)
        while not self.stop_event.is_set():
            app = websocket.WebSocketApp(url, on_message=lambda _ws, data: self._message(game, data),
                                        on_error=lambda _ws, err: logging.warning(
                                            "score relay %s: %s", game, type(err).__name__))
            with self.lock:
                self.threads[game + ":socket"] = app
            try:
                app.run_forever(**options)
            except Exception as exc:
                logging.warning("score relay reconnect %s: %s", game, type(exc).__name__)
            self.stop_event.wait(2)

    def _message(self, game, data):
        try:
            self.ingest(game, json.loads(data))
        except (ValueError, TypeError):
            pass

    def stop(self):
        self.stop_event.set()
        with self.lock:
            sockets = [value for key, value in self.threads.items() if key.endswith(":socket")]
            threads = [value for key, value in self.threads.items() if not key.endswith(":socket")]
        for sock in sockets:
            sock.close()
        for thread in threads:
            thread.join(timeout=2)
