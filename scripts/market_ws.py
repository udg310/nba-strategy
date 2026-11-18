"""Public Polymarket market stream with per-token HTTP fallback."""

import json
import logging
import math
import os
import threading
import time
from urllib.parse import urlsplit


URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class MarketBooks:
    def __init__(self, mode="http", max_age=3, http_quote=None, clock=time.monotonic):
        if mode not in ("http", "observe", "ws"):
            raise ValueError("invalid market data mode")
        self.mode, self.max_age, self.http_quote, self.clock = mode, max_age, http_quote, clock
        self.lock = threading.RLock()
        self.tokens = {}  # token -> expected condition
        self.books = {}
        self.socket = None
        self.stop_event = threading.Event()
        self.thread = None
        self.start_lock = threading.Lock()

    def start(self):
        if self.mode == "http":
            return False
        with self.start_lock:
            if self.stop_event.is_set():
                return False
            if self.thread is not None and self.thread.is_alive():
                return False
            try:
                import websocket  # noqa: F401
            except ImportError as exc:
                raise RuntimeError("install websocket-client for market WebSocket") from exc
            self.thread = threading.Thread(target=self._run, name="market-ws", daemon=True)
            self.thread.start()
            return True

    def stop(self):
        self.stop_event.set()
        with self.lock:
            sock = self.socket
        if sock:
            sock.close()
        if self.thread:
            self.thread.join(timeout=5)

    def subscribe(self, token, condition):
        token, condition = str(token), str(condition)
        with self.lock:
            if token in self.tokens and str(self.tokens[token]).casefold() != condition.casefold():
                raise ValueError("token changed condition")
            fresh = token not in self.tokens
            self.tokens[token] = condition
            sock = self.socket
        if fresh and sock:
            try:
                sock.send(json.dumps({"assets_ids": [token], "operation": "subscribe"}))
            except Exception:
                self._invalidate()

    def _invalidate(self):
        with self.lock:
            self.books.clear()
            sock = self.socket
        if sock:
            sock.close()

    def _on_open(self, socket):
        with self.lock:
            self.socket = socket
            self.books.clear()
            tokens = list(self.tokens)
        if tokens:
            socket.send(json.dumps({"assets_ids": tokens, "type": "market"}))
        threading.Thread(target=self._heartbeat, args=(socket,), daemon=True,
                         name="market-ws-heartbeat").start()

    def _heartbeat(self, socket):
        while not self.stop_event.wait(10):
            with self.lock:
                if self.socket is not socket:
                    return
            try:
                socket.send("PING")
            except Exception:
                socket.close()
                return

    def _on_close(self, socket, *_):
        with self.lock:
            if self.socket is socket:
                self.socket = None
            self.books.clear()

    @staticmethod
    def _levels(rows):
        levels = {}
        for row in rows:
            price, size = float(row["price"]), float(row["size"])
            if not (math.isfinite(price) and math.isfinite(size)):
                raise ValueError("non-finite level")
            if not 0 < price < 1 or size < 0:
                raise ValueError("invalid level")
            if size:
                levels[price] = size
        return levels

    def ingest(self, message):
        if isinstance(message, str):
            if message == "PONG":
                return
            message = json.loads(message)
        if isinstance(message, list):
            for item in message:
                self.ingest(item)
            return
        if not isinstance(message, dict):
            return
        kind = message.get("event_type")
        if kind not in ("book", "price_change"):
            return
        condition = str(message.get("market") or "")
        updates = [message] if kind == "book" else message.get("price_changes", [])
        if not isinstance(updates, list):
            return
        with self.lock:
            for item in updates:
                if not isinstance(item, dict):
                    continue
                token = str(item.get("asset_id", ""))
                if token not in self.tokens:
                    continue
                if condition.casefold() != str(self.tokens[token]).casefold():
                    self.books.pop(token, None)
                    continue
                if kind == "book":
                    try:
                        bids, asks = self._levels(item["bids"]), self._levels(item["asks"])
                    except (KeyError, TypeError, ValueError):
                        self.books.pop(token, None)
                        continue
                    self.books[token] = dict(bids=bids, asks=asks, at=self.clock(),
                                              timestamp=int(item.get("timestamp") or 0))
                    continue
                book = self.books.get(token)
                if book is None:
                    continue  # Never build a partial book before its snapshot.
                try:
                    stamp = int(message.get("timestamp") or 0)
                    if stamp and book["timestamp"] and stamp < book["timestamp"]:
                        self.books.pop(token, None)
                        continue
                    side = {"BUY": "bids", "SELL": "asks"}[item["side"]]
                    price, size = float(item["price"]), float(item["size"])
                    if not (math.isfinite(price) and math.isfinite(size)):
                        raise ValueError("non-finite level")
                    if not 0 < price < 1 or size < 0:
                        raise ValueError("invalid level")
                    if size:
                        book[side][price] = size
                    else:
                        book[side].pop(price, None)
                    bid = max(book["bids"], default=None)
                    ask = min(book["asks"], default=None)
                    if ((item.get("best_bid") and float(item["best_bid"]) != bid)
                            or (item.get("best_ask") and float(item["best_ask"]) != ask)):
                        self.books.pop(token, None)
                        continue
                    book["at"] = self.clock()
                    book["timestamp"] = max(stamp, book["timestamp"])
                except (KeyError, TypeError, ValueError):
                    self.books.pop(token, None)

    def cached(self, token):
        with self.lock:
            book = self.books.get(str(token))
            if book is None or self.clock() - book["at"] > self.max_age:
                return None
            ask = min(book["asks"], default=None)
            bid = max(book["bids"], default=None)
            if ask is not None and bid is not None and ask <= bid:
                return None       # crossed book: let the caller fetch HTTP
            return str(token), ask, bid

    def quote(self, token, condition):
        self.subscribe(token, condition)
        cached = self.cached(token)
        if self.mode == "ws" and cached is not None:
            return condition, cached
        market, quote = self.http_quote(token)
        if str(market).casefold() != str(condition).casefold():
            raise ValueError("token no longer matches the resolved condition")
        if self.mode == "observe" and cached is not None and cached != quote:
            logging.info("market observe token=%s ws=%s http=%s", token, cached[1:], quote[1:])
        return market, quote

    def _run(self):
        import websocket
        while not self.stop_event.is_set():
            app = websocket.WebSocketApp(URL, on_open=self._on_open,
                on_message=lambda _ws, data: self._message(data), on_close=self._on_close,
                on_error=lambda _ws, err: logging.warning("market WebSocket: %s", type(err).__name__))
            proxy = urlsplit(os.environ.get("NBA_PROXY", "http://127.0.0.1:9567"))
            options = {"ping_interval": 20, "ping_timeout": 10}
            if proxy.hostname:
                options.update(http_proxy_host=proxy.hostname, http_proxy_port=proxy.port or 80)
            try:
                app.run_forever(**options)
            except Exception as exc:
                logging.warning("market WebSocket reconnect: %s", type(exc).__name__)
            finally:
                self._on_close(app)
            self.stop_event.wait(2)

    def _message(self, data):
        try:
            self.ingest(data)
        except Exception:
            logging.warning("market WebSocket malformed message")
