"""Shared public order books. Quote I/O never blocks trade ingestion."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode

from dashboard_store import UTC, stamp
from dashboard_games import public_json


def book(asset):
    data = public_json(
        "https://clob.polymarket.com/book?" + urlencode({"token_id": asset}), timeout=3, system_proxy=True
    )
    if str(data["asset_id"]) != asset:
        raise ValueError("unexpected order book")

    def best(side, choose):
        levels = [Decimal(str(row["price"])) for row in data[side] if Decimal(str(row["size"])) > 0]
        if any(not price.is_finite() or not 0 <= price <= 1 for price in levels):
            raise ValueError("invalid order book")
        return format(choose(levels), "f") if levels else None

    source_at = data.get("timestamp")
    if source_at is not None:
        source_at = stamp(datetime.fromtimestamp(int(source_at) / 1000, UTC))
    return {
        "bid": best("bids", max),
        "ask": best("asks", min),
        "observed_at": stamp(datetime.now(UTC)),
        "source_at": source_at,
    }


class QuoteFeed:
    def __init__(self, enabled=True, reader=book):
        self.enabled, self.reader = enabled, reader
        self.lock = threading.Lock()
        self.wanted, self.books, self.failed = {}, {}, set()
        self.stop = threading.Event()
        self.thread = None
        if enabled:
            self.thread = threading.Thread(target=self._work, name="dashboard-quotes", daemon=True)
            self.thread.start()

    def watch(self, env, assets):
        with self.lock:
            self.wanted[env] = {str(a) for a in assets if a}

    def read(self, env):
        with self.lock:
            assets = self.wanted.get(env, set())
            quotes = {k: self.books[k] for k in assets if k in self.books}
            times = [q["observed_at"] for q in quotes.values()]
            status, reason = "ok", None
            if not self.enabled:
                status, reason = "unconfigured", "尚未启用行情采集"
            elif self.failed & assets:
                status, reason = "delayed", "部分标的盘口读取失败"
            elif assets - quotes.keys():
                status, reason = "unknown", "等候首次盘口"
            return quotes, {
                "name": "quotes",
                "status": status,
                "reason": reason,
                "last_success_at": max(times, default=None),
                "last_event_at": max(times, default=None),
            }

    def _work(self):
        pending, next_at = {}, {}
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="book") as pool:
            while not self.stop.is_set():
                with self.lock:
                    assets = set().union(*self.wanted.values())
                    for asset in list(self.books.keys() - assets):
                        del self.books[asset]
                    self.failed.intersection_update(assets)
                for asset, future in list(pending.items()):
                    if not future.done():
                        continue
                    del pending[asset]
                    try:
                        result = future.result()
                        with self.lock:
                            if asset in assets:
                                self.books[asset] = result
                                self.failed.discard(asset)
                    except Exception:
                        with self.lock:
                            if asset in assets:
                                self.failed.add(asset)
                now = time.monotonic()
                for asset in assets - pending.keys():
                    if next_at.get(asset, 0) <= now:
                        pending[asset] = pool.submit(self.reader, asset)
                        next_at[asset] = now + 2
                for asset in next_at.keys() - assets:
                    del next_at[asset]
                self.stop.wait(0.1)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join()
