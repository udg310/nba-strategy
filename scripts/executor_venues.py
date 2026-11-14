"""Durable paper exchange and the in-process live CLOB venue."""

import base64
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

import executor_clob
from executor_ledger import MAX_AMOUNT, MONEY_SCALE


SUBMIT_TIMEOUT = 35
LOOKUP_TIMEOUT = 30


class BeforeTransmission(RuntimeError):
    """The SDK request hook has definitively closed this attempt before sending."""


def _run_sdk(op, raw_payload, budget, slot, done, result):
    # This thread owns its input and has no reference to a venue or ledger.
    # A stuck OS resolver must never keep the service or its account lock alive.
    try:
        result.append(executor_clob.execute(op, json.loads(raw_payload), budget))
    except Exception:
        # SDK exceptions and RPC URLs may contain credentials.
        pass
    finally:
        slot.release()
        done.set()


class PaperVenue:
    def __init__(self, ledger):
        self.ledger = ledger
        self.stop_requested = threading.Event()
        with ledger.tx() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS paper_orders(
                seq INTEGER PRIMARY KEY, order_id TEXT NOT NULL UNIQUE, attempt TEXT NOT NULL UNIQUE,
                game TEXT NOT NULL, asset TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL,
                price INTEGER NOT NULL, filled_at INTEGER NOT NULL)""")

    def submit(self, intent):
        price = intent["limit_price"]
        reserved = intent["reserved"]
        if not intent["attempt_id"] or not 0 < price <= MONEY_SCALE or reserved <= 0:
            return {"state": "REJECTED_ZERO", "reason": "invalid order size or price"}
        buying = intent["action"] == "OPEN"
        qty = reserved * MONEY_SCALE // price if buying else reserved
        if not 0 < qty <= MAX_AMOUNT:
            return {"state": "REJECTED_ZERO", "reason": "quantity out of range"}
        oid = "paper-" + intent["attempt_id"]
        with self.ledger.tx() as db:
            now = time.time_ns() // 1_000_000
            if self.stop_requested.is_set():
                return {"state": "REJECTED_ZERO", "reason": "stopped before submit", "retryable": True}
            if intent["deadline"] <= now:
                return {"state": "REJECTED_ZERO", "reason": "expired before submit"}
            db.execute("""INSERT INTO paper_orders(order_id,attempt,game,asset,side,qty,price,filled_at)
                VALUES(?,?,?,?,?,?,?,?)""", (oid, intent["attempt_id"], intent["game_id"],
                                            intent["asset"], "BUY" if buying else "SELL",
                                            qty, price, now))
        return {"state": "ACCEPTED", "order_id": oid}

    @staticmethod
    def _fill(row):
        return dict(id="match-" + row["order_id"], order_id=row["order_id"], game_id=row["game"],
                    asset=row["asset"], side=row["side"], quantity=row["qty"], price=row["price"],
                    time=row["filled_at"], fee=0, confirmed=True)

    def lookup(self, intent):
        with self.ledger.tx() as db:
            row = db.execute("SELECT * FROM paper_orders WHERE attempt=?",
                             (intent["attempt_id"],)).fetchone()
        if row is None:
            raise LookupError("paper order unavailable")
        return {"state": "FINAL", "order_id": row["order_id"], "total_filled": row["qty"],
                "fills": [self._fill(row)]}

    def trades(self, cursor):
        if cursor and not re.fullmatch(r"[+]?[0-9]+", cursor):
            raise ValueError("invalid paper cursor")
        seq = int(cursor or "0")
        if seq >= 2**63:
            raise ValueError("invalid paper cursor")
        with self.ledger.tx() as db:
            rows = db.execute("SELECT * FROM paper_orders WHERE seq>? ORDER BY seq LIMIT 100",
                              (seq,)).fetchall()
        return [self._fill(row) for row in rows], str(rows[-1]["seq"] if rows else seq)


class LiveVenue:
    def __init__(self, ledger):
        self.ledger = ledger
        self.stop_requested = threading.Event()
        self._feed = threading.Lock()
        self._finished = ""
        # Account-feed capacity stays independent of the four intent workers.
        # A detached call holds its slot until it actually returns.
        self._order_slots = threading.BoundedSemaphore(4)
        self._feed_slot = threading.BoundedSemaphore(1)
        for name in ("POLY_PRIVATE_KEY", "POLY_FUNDER", "POLY_API_KEY", "POLY_API_SECRET",
                     "POLY_API_PASSPHRASE", "EXECUTOR_POLYGON_RPC_URL", "EXECUTOR_ORDER_KEY"):
            if not os.getenv(name):
                raise ValueError(f"{name} required in live mode")
        endpoint = urlsplit(os.environ["EXECUTOR_POLYGON_RPC_URL"])
        if endpoint.scheme != "https" or not endpoint.hostname:
            raise ValueError("EXECUTOR_POLYGON_RPC_URL must be an HTTPS Polygon RPC endpoint")
        try:
            self._key = base64.b64decode(os.environ["EXECUTOR_ORDER_KEY"], validate=True)
            if len(self._key) != 32:
                raise ValueError
        except ValueError:
            raise ValueError("EXECUTOR_ORDER_KEY must be a base64 encoded 32-byte key") from None
        try:
            from Crypto.Cipher import AES
        except ImportError:
            raise RuntimeError("install scripts/requirements-executor-live.txt for live mode") from None
        self._aes = AES
        with ledger.tx() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS live_prepared(
                attempt TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE, sealed BLOB NOT NULL)""")
        if not self._call("probe", {}, time.monotonic() + 20).get("ok"):
            raise RuntimeError("live venue probe failed")

    def _call(self, op, payload, expires):
        budget = executor_clob.CallDeadline(expires, self.stop_requested)
        slot = self._feed_slot if op in ("trades", "probe") else self._order_slots
        done = threading.Event()
        result = []
        raw = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        try:
            while not slot.acquire(timeout=budget.remaining(.05)):
                pass
            try:
                threading.Thread(target=_run_sdk, args=(op, raw, budget, slot, done, result),
                                 name=f"clob-{op}", daemon=True).start()
            except BaseException:
                slot.release()
                raise
            while not done.wait(budget.remaining(.05)):
                pass
            # An outcome that arrived too late is reconciled from the identity
            # already stored. This worker alone can ever report it to the ledger.
            budget.remaining(.05)
            if result:
                return result[0]
        except (InterruptedError, TimeoutError):
            pass
        posted = budget.abandon()
        if op == "post" and not posted:
            raise BeforeTransmission("submission stopped before transmission")
        raise RuntimeError(f"CLOB {op} failed (check SDK, credentials, chain and connectivity)")

    def submit(self, intent):
        expires = time.monotonic() + SUBMIT_TIMEOUT
        attempt = intent["attempt_id"]
        with self.ledger.tx() as db:
            row = db.execute("SELECT order_id FROM live_prepared WHERE attempt=?", (attempt,)).fetchone()
            if row is not None:
                return {"state": "UNKNOWN", "order_id": row[0], "reason": "attempt already prepared"}
            reserved = db.execute("""SELECT coalesce(sum(reserved),0) FROM intents
                WHERE account=? AND environment=? AND action=? AND (action='OPEN' OR asset=?)""",
                (intent["account"], intent["environment"], intent["action"], intent["asset"])).fetchone()[0]
        try:
            prepared = self._call("prepare", {**intent, "pending_reservations": reserved}, expires)
        except RuntimeError:
            return {"state": "REJECTED_ZERO", "reason": "preparation failed before transmission",
                    "retryable": True}
        if prepared.get("reject"):
            return {"state": "REJECTED_ZERO", "reason": prepared["reject"],
                    "retryable": prepared.get("retryable", False)}
        oid = prepared["order_id"]
        signed = prepared["signed"]
        if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", oid) or not isinstance(signed, dict) or not signed:
            raise ValueError("invalid locally signed order")
        # Go's NewGCMWithRandomNonce stores nonce || ciphertext || tag, with
        # the attempt ID as the authenticated associated data.
        nonce = secrets.token_bytes(12)
        cipher = self._aes.new(self._key, self._aes.MODE_GCM, nonce=nonce)
        cipher.update(attempt.encode())
        raw = json.dumps(signed, separators=(",", ":"), allow_nan=False).encode()
        encrypted, tag = cipher.encrypt_and_digest(raw)
        with self.ledger.tx() as db:
            db.execute("INSERT INTO live_prepared(attempt,order_id,sealed) VALUES(?,?,?)",
                       (attempt, oid, nonce + encrypted + tag))
        # This insert-only gate commits before the single POST. A crash leaves
        # enough identity for read-only lookup, even when no receipt exists.
        if self.stop_requested.is_set():
            return {"state": "REJECTED_ZERO", "reason": "stopped before transmission", "retryable": True,
                    "order_id": oid}
        try:
            reply = self._call("post", {"order_id": oid, "signed": signed,
                                       "deadline": intent["deadline"]}, expires)
        except BeforeTransmission:
            return {"state": "REJECTED_ZERO", "order_id": oid, "reason": "stopped before transmission",
                    "retryable": True}
        except RuntimeError:
            return {"state": "UNKNOWN", "order_id": oid, "reason": "submission outcome unavailable"}
        return {**reply, "order_id": oid}

    def lookup(self, intent):
        with self.ledger.tx() as db:
            row = db.execute("SELECT order_id FROM live_prepared WHERE attempt=?",
                             (intent["attempt_id"],)).fetchone()
        receipt = intent.get("order_id", "")
        oid = row[0] if row else receipt
        if not oid:
            raise LookupError("prepared order identity unavailable")
        if receipt and receipt.lower() != oid.lower():
            raise ValueError("receipt and signed order hash differ")
        return self._call("lookup", {"order_id": oid, "asset": intent["asset"],
                                     "action": intent["action"], "market": intent["market"]},
                          time.monotonic() + LOOKUP_TIMEOUT)

    def trades(self, cursor):
        with self._feed:
            if cursor and cursor == self._finished:
                self._finished = ""
                return [], cursor
            page = self._call("trades", {"cursor": cursor}, time.monotonic() + LOOKUP_TIMEOUT)
            following = page.get("next")
            if not following:
                raise ValueError("missing account reconciliation cursor")
            fills = page["fills"]
            if fills:
                with self.ledger.tx() as db:
                    for fill in fills:
                        row = db.execute("""SELECT game_id FROM intents i JOIN attempts a ON a.intent_id=i.id
                            JOIN live_prepared p ON p.attempt=a.id WHERE p.order_id=?""",
                            (fill["order_id"],)).fetchone()
                        if row:
                            fill["game_id"] = row[0]
            if page.get("complete") and following != cursor:
                self._finished = following
            return fills, following
