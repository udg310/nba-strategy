# -*- coding: utf-8 -*-
"""SQLite attempt ledger for live_runner: one in-flight order per signal.

Ported from the executor's intents/attempts design, kept deliberately small:

- The signed order hash is written BEFORE the POST (`begin`).
- A 200 response whose outcome is unclear leaves the row `UNKNOWN`; the
  unique (account, game_id, channel, action) row means a second order for the
  same signal can never be inserted, so the runner cannot double-buy.
- On restart, leftover `SUBMITTING` rows become `UNKNOWN`; the game/channel
  is treated as already attempted and is not re-entered.
- Only a documented FOK-unfilled `REJECTED_ZERO` may be retried once.
"""
import os
import sqlite3
import threading
import time
import uuid

SUBMITTING = "SUBMITTING"
ACCEPTED = "ACCEPTED"
UNKNOWN = "UNKNOWN"
FILLED = "FILLED"
CLOSED = "CLOSED"
REJECTED_ZERO = "REJECTED_ZERO"
PARTIAL_FINAL = "PARTIAL_FINAL"

OPEN_STATES = (SUBMITTING, ACCEPTED, UNKNOWN, PARTIAL_FINAL, FILLED)


class AttemptLedger:
    def __init__(self, path, account):
        self.account = account
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS attempts (
            account TEXT NOT NULL,
            game_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            action TEXT NOT NULL,
            attempt_id TEXT PRIMARY KEY,
            position_id TEXT NOT NULL DEFAULT '',
            order_id TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            attempt_no INTEGER NOT NULL DEFAULT 1,
            signal_at INTEGER NOT NULL,
            el_in REAL,
            updated_at INTEGER NOT NULL,
            UNIQUE(account, game_id, channel, action)
        )""")
        self.db.commit()
        self.recover()

    def recover(self):
        """A sender that died mid-POST leaves SUBMITTING; treat it as UNKNOWN."""
        with self.lock:
            cur = self.db.execute(
                "UPDATE attempts SET state=?, updated_at=? WHERE state=?",
                (UNKNOWN, int(time.time()), SUBMITTING))
            self.db.commit()
            return cur.rowcount

    def begin(self, game, channel, action, order_id, signal_at, el_in=None,
              position_id=""):
        """Record SUBMITTING before the POST.  (attempt_id, True) or (None, False).

        A documented FOK-unfilled REJECTED_ZERO may be retried once.
        """
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM attempts WHERE account=? AND game_id=? "
                "AND channel=? AND action=?",
                (self.account, game, channel, action)).fetchone()
            now = int(time.time())
            if row is None:
                aid = uuid.uuid4().hex.upper()
                self.db.execute(
                    "INSERT INTO attempts (account,game_id,channel,action,"
                    "attempt_id,position_id,order_id,state,attempt_no,"
                    "signal_at,el_in,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.account, game, channel, action, aid, position_id,
                     order_id, SUBMITTING, 1, int(signal_at), el_in, now))
                self.db.commit()
                return aid, True
            if row["state"] == REJECTED_ZERO and row["attempt_no"] < 2:
                aid = uuid.uuid4().hex.upper()
                self.db.execute(
                    "UPDATE attempts SET attempt_id=?, order_id=?, state=?, "
                    "attempt_no=attempt_no+1, signal_at=?, el_in=?, updated_at=? "
                    "WHERE attempt_id=?",
                    (aid, order_id, SUBMITTING, int(signal_at), el_in, now,
                     row["attempt_id"]))
                self.db.commit()
                return aid, True
            return None, False

    def finish(self, attempt_id, state, order_id=None):
        with self.lock:
            self.db.execute(
                "UPDATE attempts SET state=?, order_id=coalesce(?,order_id), "
                "updated_at=? WHERE attempt_id=?",
                (state, order_id, int(time.time()), attempt_id))
            self.db.commit()

    def fill(self, attempt_id):
        with self.lock:
            self.db.execute(
                "UPDATE attempts SET state=?, position_id=?, updated_at=? "
                "WHERE attempt_id=?",
                (FILLED, attempt_id, int(time.time()), attempt_id))
            self.db.commit()

    def close(self, attempt_id):
        with self.lock:
            self.db.execute(
                "UPDATE attempts SET state=?, updated_at=? WHERE attempt_id=?",
                (CLOSED, int(time.time()), attempt_id))
            self.db.commit()

    def rows(self, game=None):
        with self.lock:
            if game is None:
                cur = self.db.execute(
                    "SELECT * FROM attempts WHERE account=? ORDER BY signal_at",
                    (self.account,))
            else:
                cur = self.db.execute(
                    "SELECT * FROM attempts WHERE account=? AND game_id=? "
                    "ORDER BY signal_at", (self.account, game))
            return [dict(r) for r in cur]

    def open_rows(self, game=None):
        return [r for r in self.rows(game)
                if r["action"] == "OPEN" and r["state"] in OPEN_STATES]

    def has_open(self, game, channel):
        return any(r["channel"] == channel for r in self.open_rows(game))

    def close_path(self):
        with self.lock:
            self.db.close()
