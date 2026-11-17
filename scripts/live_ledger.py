# -*- coding: utf-8 -*-
"""Durable per-account order attempts, final-game handoffs and confirmed fills."""
import math
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

SUBMITTING = "SUBMITTING"
ACCEPTED = "ACCEPTED"
UNKNOWN = "UNKNOWN"
FILLED = "FILLED"
CLOSED = "CLOSED"
REJECTED_ZERO = "REJECTED_ZERO"
PARTIAL_FINAL = "PARTIAL_FINAL"
HANDED_OFF = "HANDED_OFF"

OPEN_STATES = (SUBMITTING, ACCEPTED, UNKNOWN, PARTIAL_FINAL, FILLED)
# SQLite REAL round-off only; this is far below one micro-share or USDC unit.
EPS = 1e-9
UNVERIFIED_REASON = "legacy order ID requires a verified venue order link"


def _amount(value, *, positive=False, price=False):
    if isinstance(value, bool):
        raise ValueError("invalid numeric evidence")
    number = float(value)
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        raise ValueError("invalid quantity or price")
    if price and number > 1:
        raise ValueError("out-of-range outcome price")
    return number


class AttemptLedger:
    def __init__(self, path, account, *, recover=True):
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self._initialize(account)
            if recover:
                self.recover()
        except BaseException:
            self.db.close()
            raise

    def _initialize(self, account):
        with self._write():
            self.db.execute("""CREATE TABLE IF NOT EXISTS attempts (
                account TEXT NOT NULL, game_id TEXT NOT NULL, channel TEXT NOT NULL,
                action TEXT NOT NULL, attempt_id TEXT PRIMARY KEY,
                position_id TEXT NOT NULL DEFAULT '', order_id TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL, attempt_no INTEGER NOT NULL DEFAULT 1,
                signal_at INTEGER NOT NULL, el_in REAL, updated_at INTEGER NOT NULL,
                reserved_usd REAL, bought_size REAL, entry_price REAL,
                token TEXT NOT NULL DEFAULT '', market TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL DEFAULT '',
                identity_verified INTEGER NOT NULL DEFAULT 0,
                UNIQUE(account, game_id, channel, action)
            )""")
            columns = {r["name"] for r in self.db.execute("PRAGMA table_info(attempts)")}
            migrations = dict.fromkeys(("reserved_usd", "bought_size", "entry_price"), "REAL")
            migrations.update(dict.fromkeys(("token", "market", "outcome"), "TEXT NOT NULL DEFAULT ''"))
            migrations["identity_verified"] = "INTEGER NOT NULL DEFAULT 0"
            migrations.update(requested_size="REAL", terminal_matched="REAL")
            for column, sql_type in migrations.items():
                if column not in columns:
                    self.db.execute(f"ALTER TABLE attempts ADD COLUMN {column} {sql_type}")
            if "filled_shares" in columns and "bought_size" not in columns:
                # Old production rows are gross estimates with unverified order IDs.
                # Preserve them for review, never use them to authorize a new sell.
                self.db.execute("""UPDATE attempts SET bought_size=filled_shares,
                    entry_price=filled_price WHERE action='OPEN'""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS attempt_history AS
                SELECT * FROM attempts WHERE 0""")
            self.db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS prior_attempt_id
                ON attempt_history(attempt_id)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS reconcile_tasks (
                account TEXT NOT NULL, game_id TEXT NOT NULL, channel TEXT NOT NULL,
                token TEXT NOT NULL, market TEXT NOT NULL, outcome TEXT NOT NULL,
                reason TEXT NOT NULL, review_reason TEXT NOT NULL DEFAULT '',
                order_id TEXT NOT NULL COLLATE NOCASE, position_id TEXT NOT NULL,
                initial_order_id TEXT NOT NULL, next_close_order_id TEXT NOT NULL,
                identity_verified INTEGER NOT NULL DEFAULT 0,
                next_close_verified INTEGER NOT NULL DEFAULT 0,
                initial_action TEXT NOT NULL, action TEXT NOT NULL, state TEXT NOT NULL,
                size REAL NOT NULL, entry REAL NOT NULL, reserved_usd REAL NOT NULL,
                initial_reserved_usd REAL NOT NULL, legacy_sold REAL NOT NULL DEFAULT 0,
                applied_order_size REAL NOT NULL DEFAULT 0,
                applied_order_usd REAL NOT NULL DEFAULT 0, terminal_matched REAL,
                sold_shares REAL NOT NULL DEFAULT 0, sell_proceeds REAL NOT NULL DEFAULT 0,
                payout_price REAL, settled_shares REAL NOT NULL DEFAULT 0,
                settled_payout REAL NOT NULL DEFAULT 0, redemption_ref TEXT COLLATE NOCASE,
                zero_ref TEXT COLLATE NOCASE, zero_order_id TEXT,
                zero_blocked INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                PRIMARY KEY(account, game_id, channel), UNIQUE(account, redemption_ref),
                UNIQUE(account, zero_ref)
            )""")
            task_columns = {r["name"] for r in self.db.execute("PRAGMA table_info(reconcile_tasks)")}
            if "legacy_bought_size" not in task_columns:
                self.db.execute("ALTER TABLE reconcile_tasks ADD COLUMN legacy_bought_size REAL")
            for column in ("identity_verified", "next_close_verified"):
                if column not in task_columns:
                    self.db.execute(f"ALTER TABLE reconcile_tasks ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
            if "identity_verified" not in task_columns:
                self.db.execute("""UPDATE reconcile_tasks SET state='REVIEW',review_reason=?
                    WHERE action IN ('OPEN','CLOSE') AND state!='DONE'""", (UNVERIFIED_REASON,))
            self.db.execute("""CREATE TABLE IF NOT EXISTS order_links (
                account TEXT NOT NULL, game_id TEXT NOT NULL, channel TEXT NOT NULL,
                action TEXT NOT NULL, old_id TEXT NOT NULL COLLATE NOCASE,
                venue_id TEXT NOT NULL COLLATE NOCASE, reference TEXT NOT NULL COLLATE NOCASE,
                created_at INTEGER NOT NULL, PRIMARY KEY(account,game_id,channel,action),
                UNIQUE(account,venue_id), UNIQUE(account,reference)
            )""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS order_fills (
                account TEXT NOT NULL, game_id TEXT NOT NULL, channel TEXT NOT NULL,
                action TEXT NOT NULL, order_id TEXT NOT NULL COLLATE NOCASE,
                fill_id TEXT NOT NULL, size REAL NOT NULL, price REAL NOT NULL,
                PRIMARY KEY(account, order_id, fill_id)
            )""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS exit_intents (
                account TEXT NOT NULL, game_id TEXT NOT NULL, channel TEXT NOT NULL,
                reason TEXT NOT NULL, PRIMARY KEY(account, game_id, channel)
            )""")
            self.db.execute("""CREATE INDEX IF NOT EXISTS task_risk
                ON reconcile_tasks(account) WHERE state != 'DONE'""")
            fill_columns = {r["name"] for r in self.db.execute("PRAGMA table_info(order_fills)")}
            for column in ("recorded_at", "matched_at"):
                if column not in fill_columns:
                    self.db.execute(f"ALTER TABLE order_fills ADD COLUMN {column} INTEGER")
            self.db.execute("""CREATE INDEX IF NOT EXISTS open_risk
                ON attempts(account, action, state)""")
            self.db.execute("""CREATE INDEX IF NOT EXISTS signed_attempts
                ON attempts(account, order_id COLLATE NOCASE)""")
            self.db.execute("""CREATE INDEX IF NOT EXISTS fills_position
                ON order_fills(account, game_id, channel, action)""")
            # Pin the first spelling even when two connections open a new
            # wallet before its first attempt. Existing rows keep their exact
            # key so every per-account query sees the historical allocation.
            self.db.execute("""CREATE TABLE IF NOT EXISTS ledger_accounts (
                account TEXT PRIMARY KEY COLLATE NOCASE NOT NULL
            )""")
            matches = set()
            for table in ("ledger_accounts", "attempts", "reconcile_tasks", "order_fills", "exit_intents", "order_links"):
                matches.update(row[0] for row in self.db.execute(
                    f"SELECT DISTINCT account COLLATE BINARY FROM {table} WHERE account=? COLLATE NOCASE",
                    (account,)))
            if len(matches) > 1:
                raise ValueError("multiple account spellings in this ledger; reconcile the stored allocations "
                                 "before resuming this wallet: " + ", ".join(sorted(matches)))
            self.account = next(iter(matches), account)
            self.db.execute("INSERT OR IGNORE INTO ledger_accounts(account) VALUES (?)", (self.account,))

    @contextmanager
    def _write(self):
        # Acquire the writer before reading the previous owner's state. Also
        # rolls back earlier inserts on an aborted update, exception or crash.
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            yield

    def recover(self):
        with self._write():
            return self.db.execute(
                "UPDATE attempts SET state=?, updated_at=? WHERE account=? AND state=?",
                (UNKNOWN, int(time.time()), self.account, SUBMITTING)).rowcount

    def _task(self, game, channel):
        return self.db.execute(
            "SELECT * FROM reconcile_tasks WHERE account=? AND game_id=? AND channel=?",
            (self.account, game, channel)).fetchone()

    def _other_owner(self, game, channel, order_id):
        """A signed order may have at most one local allocation within an account."""
        if not order_id:
            return False
        params = self.account, order_id, game, channel
        return self.db.execute("""
            SELECT 1 FROM attempts WHERE account=? AND order_id=? COLLATE NOCASE
              AND (game_id,channel)!=(?,?)
            UNION ALL
            SELECT 1 FROM attempt_history WHERE account=? AND order_id=? COLLATE NOCASE
              AND (game_id,channel)!=(?,?)
            UNION ALL
            SELECT 1 FROM order_fills WHERE account=? AND order_id=?
              AND (game_id,channel)!=(?,?)
            UNION ALL
            SELECT 1 FROM reconcile_tasks WHERE account=?
              AND ? COLLATE NOCASE IN (initial_order_id,order_id,next_close_order_id)
              AND (game_id,channel)!=(?,?)
            UNION ALL
            SELECT 1 FROM order_links WHERE account=? AND ? COLLATE NOCASE IN (old_id,venue_id)
              AND (game_id,channel)!=(?,?) LIMIT 1
            """, params * 5).fetchone() is not None

    def begin(self, game, channel, action, order_id, signal_at, el_in=None,
              position_id="", *, reserved_usd=None, token="", market="", outcome="", requested_size=None):
        """Store the locally signed order before POST, respecting prior ownership."""
        key = (self.account, game, channel, action)
        if reserved_usd is not None:
            reserved_usd = _amount(reserved_usd)
        with self._write():
            if self._task(game, channel) or self._other_owner(game, channel, order_id):
                return None, False
            if not order_id or self.db.execute("""SELECT 1 FROM attempt_history WHERE account=?
                    AND order_id=? COLLATE NOCASE UNION ALL SELECT 1 FROM attempts
                    WHERE account=? AND order_id=? COLLATE NOCASE LIMIT 1""",
                    (self.account, order_id) * 2).fetchone():
                return None, False
            row = self.db.execute(
                "SELECT * FROM attempts WHERE account=? AND game_id=? AND channel=? AND action=?",
                key).fetchone()
            if action == "CLOSE" and requested_size is not None:
                requested_size = _amount(requested_size, positive=True)
                parent = self.db.execute("""SELECT * FROM attempts WHERE account=? AND
                    game_id=? AND channel=? AND action='OPEN'""", key[:3]).fetchone()
                if (not parent or not parent["identity_verified"] or parent["state"] not in OPEN_STATES or not position_id
                        or position_id != (parent["position_id"] or parent["attempt_id"])
                        or not self._matches_asset(parent, token, market, outcome)):
                    return None, False
                bought = parent["bought_size"]
                sold = self.db.execute("""SELECT coalesce(sum(size),0) FROM order_fills WHERE
                    account=? AND game_id=? AND channel=? AND action='CLOSE'""", key[:3]).fetchone()[0]
                if bought is None or requested_size > bought - sold + EPS:
                    return None, False
            aid = uuid.uuid4().hex.upper()
            now = int(time.time())
            if row is None:
                self.db.execute("""INSERT INTO attempts
                    (account,game_id,channel,action,attempt_id,position_id,order_id,state,
                     attempt_no,signal_at,el_in,updated_at,reserved_usd,token,market,outcome,identity_verified)
                    VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,1)""",
                    (*key, aid, position_id, order_id, SUBMITTING, int(signal_at),
                     el_in, now, reserved_usd, token, market, outcome))
            elif ((row["state"] == REJECTED_ZERO and
                   (action == "CLOSE" or row["attempt_no"] < 2)) or
                  (action == "CLOSE" and (row["state"] == PARTIAL_FINAL or
                   (row["state"] == CLOSED and row["terminal_matched"] is not None)))):
                token, market, outcome = token or row["token"], market or row["market"], outcome or row["outcome"]
                if not self._matches_asset(row, token, market, outcome):
                    return None, False
                self.db.execute("INSERT INTO attempt_history SELECT * FROM attempts WHERE attempt_id=?",
                                (row["attempt_id"],))
                self.db.execute("""UPDATE attempts SET attempt_id=?, order_id=?, state=?,
                    attempt_no=attempt_no+1, signal_at=?, el_in=?, updated_at=?, reserved_usd=?,
                    token=?,market=?,outcome=?,identity_verified=1 WHERE attempt_id=?""",
                    (aid, order_id, SUBMITTING, int(signal_at), el_in, now, reserved_usd,
                     token, market, outcome, row["attempt_id"]))
            else:
                return None, False
            self.db.execute("UPDATE attempts SET requested_size=?,terminal_matched=NULL WHERE attempt_id=?",
                            (requested_size, aid))
            return aid, True

    def finish(self, attempt_id, state, order_id=None, *, identity_verified=False):
        with self._write():
            self.db.execute("""UPDATE attempts SET state=?, updated_at=?, identity_verified=max(identity_verified,?)
                WHERE account=? AND attempt_id=? AND state!=?
                AND (? IS NULL OR order_id=? COLLATE NOCASE)
                AND (?!='REJECTED_ZERO' OR ((identity_verified OR ?) AND NOT EXISTS
                    (SELECT 1 FROM order_fills f WHERE f.account=attempts.account AND f.order_id=attempts.order_id)))""",
                (state, int(time.time()), int(identity_verified), self.account, attempt_id, HANDED_OFF,
                 order_id, order_id, state, int(identity_verified)))

    def fill(self, attempt_id, size=None, entry=None):
        if size is not None:
            size = _amount(size, positive=True)
        if entry is not None:
            entry = _amount(entry, price=True)
        with self._write():
            self.db.execute("""UPDATE attempts SET state=?, position_id=?, updated_at=?,
                bought_size=coalesce(bought_size,?), entry_price=coalesce(entry_price,?)
                WHERE account=? AND attempt_id=? AND state NOT IN (?,?)""",
                (FILLED, attempt_id, int(time.time()), size, entry, self.account,
                 attempt_id, HANDED_OFF, CLOSED))
            row = self.db.execute("SELECT game_id,channel FROM attempts WHERE account=? AND attempt_id=?",
                                  (self.account, attempt_id)).fetchone()
            if row:
                self.close_flat_position(*row)

    def close(self, attempt_id):
        self.finish(attempt_id, CLOSED)

    def rows(self, game=None):
        with self.lock:
            sql = "SELECT * FROM attempts WHERE account=?"
            params = [self.account]
            if game is not None:
                sql += " AND game_id=?"
                params.append(game)
            return [dict(r) for r in self.db.execute(sql + " ORDER BY signal_at", params)]

    def open_rows(self, game=None):
        return [r for r in self.rows(game)
                if r["action"] == "OPEN" and r["state"] in OPEN_STATES]

    def has_open(self, game, channel):
        return any(r["channel"] == channel for r in self.open_rows(game))

    def exit_intent(self, game, channel, reason=None):
        key = self.account, game, channel
        with self._write() if reason is not None else self.lock:
            if reason is not None:
                self.db.execute("INSERT OR IGNORE INTO exit_intents VALUES (?,?,?,?)",
                                (*key, reason))
            row = self.db.execute("""SELECT reason FROM exit_intents WHERE
                account=? AND game_id=? AND channel=?""", key).fetchone()
            return row["reason"] if row else None

    def tasks(self, pending_only=True):
        sql = "SELECT * FROM reconcile_tasks WHERE account=?"
        if pending_only:
            sql += " AND state!='DONE'"
        with self.lock:
            return [dict(r) for r in self.db.execute(
                sql + " ORDER BY created_at,game_id,channel", (self.account,))]

    def exposure(self):
        """One snapshot of occupied signals, unspent open budgets and queued held basis."""
        keys = set()
        reserved = basis = 0.
        unknown = False
        with self.lock:
            for row in self.db.execute("""
                SELECT game_id,channel,reserved_usd,size*entry AS basis,
                       action='OPEN' AND initial_reserved_usd=0 AS unknown_budget
                FROM reconcile_tasks WHERE account=? AND state!='DONE'
                UNION ALL
                SELECT game_id,channel,
                    CASE WHEN state='FILLED' THEN 0 ELSE max(0,coalesce(reserved_usd,0)-
                        coalesce((SELECT sum(f.size*f.price) FROM order_fills f
                          WHERE f.account=attempts.account AND f.order_id=attempts.order_id),0)) END,0,
                    CASE WHEN state='FILLED' THEN bought_size IS NULL OR entry_price IS NULL
                         ELSE coalesce(reserved_usd,0)=0 END
                FROM attempts WHERE account=? AND action='OPEN'
                AND state IN ('SUBMITTING','UNKNOWN','ACCEPTED','FILLED')""",
                    (self.account, self.account)):
                keys.add((row["game_id"], row["channel"]))
                reserved += row["reserved_usd"] or 0
                basis += row["basis"]
                unknown |= bool(row["unknown_budget"])
        return keys, reserved, unknown, basis

    @staticmethod
    def _matches_asset(row, token, market, outcome):
        return ((not row["token"] or row["token"] == token)
                and (not row["market"] or row["market"].lower() == market.lower())
                and (not row["outcome"] or row["outcome"] == outcome))

    def bind_asset(self, game, channel, *, token, market, outcome):
        """Complete legacy asset metadata while preserving its order verification state."""
        key = self.account, game, channel
        with self._write():
            rows = self.db.execute("""SELECT * FROM attempts
                WHERE account=? AND game_id=? AND channel=?""", key).fetchall()
            if any(not self._matches_asset(row, token, market, outcome) for row in rows):
                raise ValueError("recovered asset differs from original order")
            self.db.execute("""UPDATE attempts SET
                token=coalesce(nullif(token,''),?), market=coalesce(nullif(market,''),?),
                outcome=coalesce(nullif(outcome,''),?)
                WHERE account=? AND game_id=? AND channel=?""", (token, market, outcome, *key))

    def handoff(self, game, channel, *, token, market, outcome, reason, size=0,
                entry=0, reserved_usd=0, order_id="", action="HOLD", position_id="",
                legacy_sold=0, identity_verified=True):
        key = self.account, game, channel
        with self._write():
            existing = self._task(game, channel)
            if existing:
                if not self._matches_asset(existing, token, market, outcome):
                    raise ValueError("handoff asset differs from the existing task")
                return dict(existing)
            if action not in ("OPEN", "CLOSE", "HOLD"):
                raise ValueError("invalid handoff action")
            attempts = {r["action"]: r for r in self.db.execute("""SELECT * FROM attempts
                WHERE account=? AND game_id=? AND channel=?""", key)}
            if any(not self._matches_asset(r, token, market, outcome) for r in attempts.values()):
                raise ValueError("handoff asset differs from the original signed attempt")
            close = attempts.get("CLOSE")
            if close and close["state"] not in (SUBMITTING, ACCEPTED, UNKNOWN):
                close = None
            if close and position_id and close["position_id"] and position_id != close["position_id"]:
                raise ValueError("outstanding sell belongs to a different position")
            next_close = close["order_id"] if close and action == "OPEN" else ""
            if action == "HOLD" and close:
                action, order_id = "CLOSE", close["order_id"]
            if action != "HOLD" and not order_id:
                raise ValueError("order handoff requires an action and signed order ID")
            if self._other_owner(game, channel, order_id) or self._other_owner(game, channel, next_close):
                raise ValueError("signed order already belongs to another game or channel")
            size = _amount(size)
            entry = _amount(entry, price=True)
            legacy_sold = _amount(legacy_sold)
            if legacy_sold and action != "CLOSE" and not next_close:
                raise ValueError("legacy sell baseline requires a closing order")
            attempt = attempts.get(action)
            if attempt and attempt["order_id"].lower() != order_id.lower():
                raise ValueError("handoff does not match the persisted signed order")
            if attempt:
                identity_verified = attempt["identity_verified"]
            review_reason = ("legacy partial sell awaiting complete original order" if legacy_sold else "")
            if not identity_verified:
                review_reason = UNVERIFIED_REASON
            if action == "OPEN":
                reserved_usd = _amount((attempt and attempt["reserved_usd"]) or reserved_usd or 0)
            else:
                reserved_usd = 0
            totals = self.db.execute("""SELECT coalesce(sum(size),0),coalesce(sum(size*price),0)
                FROM order_fills WHERE account=? AND game_id=? AND channel=? AND action='CLOSE'""",
                key).fetchone()
            pre_size, pre_cost = self._order_totals(order_id) if action != "HOLD" else (0, 0)
            if action == "OPEN":
                if size and abs(size - pre_size) > EPS:
                    raise ValueError("known buy size must have matching durable fill IDs")
                size = max(0., pre_size - totals[0] - legacy_sold)
                entry = pre_cost / pre_size if pre_size else 0
            legacy_baseline = legacy_sold if action == "CLOSE" else 0
            now = int(time.time())
            self.db.execute("""INSERT INTO reconcile_tasks
                (account,game_id,channel,token,market,outcome,reason,order_id,position_id,
                 initial_action,action,state,size,entry,reserved_usd,initial_reserved_usd,
                 legacy_sold,applied_order_size,applied_order_usd,sold_shares,sell_proceeds,
                 review_reason,created_at,updated_at,initial_order_id,next_close_order_id,zero_blocked,
                 identity_verified,next_close_verified)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*key, token, market, outcome, reason, order_id, position_id or
                 (attempt and (attempt["position_id"] or attempt["attempt_id"])) or "",
                 action, action, "REVIEW" if review_reason else "PENDING", size, entry,
                 max(0., reserved_usd - pre_cost), reserved_usd, legacy_sold,
                 pre_size + legacy_baseline, pre_cost,
                 totals[0] + legacy_sold, totals[1],
                 review_reason,
                 now, now, order_id, next_close,
                 int(action == "OPEN" and bool(next_close or (attempt and attempt["state"] == FILLED))),
                 int(identity_verified), int(close["identity_verified"]) if next_close else 0))
            parent = attempts.get("OPEN")
            if parent and not parent["identity_verified"] and parent["bought_size"] is not None:
                self.db.execute("""UPDATE reconcile_tasks SET legacy_bought_size=?
                    WHERE account=? AND game_id=? AND channel=?""", (parent["bought_size"], *key))
            self.db.execute("""UPDATE attempts SET state=?,updated_at=? WHERE account=?
                AND game_id=? AND channel=?""", (HANDED_OFF, now, *key))
            return dict(self._task(game, channel))

    def _order_totals(self, order_id):
        return self.db.execute("""SELECT coalesce(sum(size),0),coalesce(sum(size*price),0)
            FROM order_fills WHERE account=? AND order_id=?""",
            (self.account, order_id)).fetchone()

    def trade_ids(self, order_id):
        with self.lock:
            return {r[0] for r in self.db.execute(
                "SELECT fill_id FROM order_fills WHERE account=? AND order_id=?",
                (self.account, order_id))}

    def sold_size(self, game, channel):
        with self.lock:
            return self.db.execute("""SELECT coalesce(sum(size),0) FROM order_fills
                WHERE account=? AND game_id=? AND channel=? AND action='CLOSE'""",
                (self.account, game, channel)).fetchone()[0]

    def other_token_shares(self, game, channel, token):
        """Reserve the other channels' recorded holdings when checking wallet coverage."""
        params = self.account, token, game, channel
        with self.lock:
            return self.db.execute("""
                SELECT coalesce(sum(size),0) FROM (
                    SELECT max(0,coalesce(bought_size,0) - coalesce((
                        SELECT sum(f.size) FROM order_fills f WHERE f.account=a.account
                          AND f.game_id=a.game_id AND f.channel=a.channel AND f.action='CLOSE'
                    ),0)) AS size
                    FROM attempts a WHERE account=? AND token=? AND (game_id,channel)!=(?,?)
                      AND action='OPEN' AND state IN ('SUBMITTING','ACCEPTED','UNKNOWN','FILLED','PARTIAL_FINAL')
                    UNION ALL
                    SELECT max(size,coalesce(legacy_bought_size,0)-sold_shares,0)
                    FROM reconcile_tasks WHERE account=? AND token=? AND (game_id,channel)!=(?,?)
                      AND state!='DONE'
                )""", params * 2).fetchone()[0]

    def close_flat_position(self, game, channel):
        """Caller owns a write transaction. An unfinished BUY can still acquire more."""
        self.db.execute("""UPDATE attempts SET state=?, updated_at=? WHERE account=?
            AND game_id=? AND channel=? AND action='OPEN' AND state=? AND identity_verified=1
            AND bought_size IS NOT NULL AND bought_size <= ? + (SELECT coalesce(sum(size),0)
            FROM order_fills WHERE account=? AND game_id=? AND channel=? AND action='CLOSE')""",
            (CLOSED, int(time.time()), self.account, game, channel, FILLED, EPS,
             self.account, game, channel))

    def _new_fills(self, row, fills):
        """Check the whole batch before exposing any facts to position accounting."""
        seen = {}
        new = []
        for f in fills:
            fid = f["id"]
            if not isinstance(fid, str) or not fid.strip():
                raise ValueError("missing fill ID")
            size = _amount(f["size"], positive=True)
            price = _amount(f["price"], price=True)
            facts = size, price
            if fid not in seen:
                old = self.db.execute("""SELECT size,price,game_id,channel,action
                    FROM order_fills WHERE account=? AND order_id=? AND fill_id=?""",
                    (self.account, row["order_id"], fid)).fetchone()
                if old:
                    if tuple(old)[2:] != (row["game_id"], row["channel"], row["action"]):
                        raise ValueError("fill is owned by another position")
                    seen[fid] = old["size"], old["price"]
            if fid in seen:
                if seen[fid] != facts:
                    raise ValueError("conflicting size or price for recorded fill ID")
            else:
                seen[fid] = facts
                new.append({**f, "size": size, "price": price})
        return new

    def _insert_fills(self, row, new):
        self.db.executemany("""INSERT INTO order_fills
            (account,game_id,channel,action,order_id,fill_id,size,price,recorded_at,matched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ((self.account, row["game_id"], row["channel"], row["action"],
              row["order_id"], f["id"], f["size"], f["price"], int(time.time()), f.get("matched_at")) for f in new))

    def _record_active_fills(self, order_id, fills, action, *, confirm_legacy=False):
        with self._write():
            rows = self.db.execute("""SELECT * FROM attempts WHERE account=? AND
                order_id=? COLLATE NOCASE AND action=? AND
                (state IN ('SUBMITTING','ACCEPTED','UNKNOWN') OR
                 (action='OPEN' AND state='FILLED'))""", (self.account, order_id, action)).fetchall()
            if len(rows) != 1:
                return []
            new = self._new_fills(rows[0], fills)
            requested = rows[0]["requested_size"]
            if action == "CLOSE" and requested is not None:
                confirmed, _ = self._order_totals(order_id)
                confirmed += sum(f["size"] for f in new)
                if confirmed > requested + EPS:
                    raise ValueError("sell receipts exceed the signed quantity")
                if abs(requested - confirmed) <= EPS:
                    self.db.execute("""UPDATE attempts SET state=?,terminal_matched=?,updated_at=?
                        WHERE attempt_id=?""", (PARTIAL_FINAL, confirmed, int(time.time()), rows[0]["attempt_id"]))
            self._insert_fills(rows[0], new)
            legacy_purchase = (action == "OPEN" and rows[0]["state"] == FILLED
                               and not rows[0]["identity_verified"])
            if (new or confirm_legacy) and not rows[0]["identity_verified"] and (not legacy_purchase or confirm_legacy):
                self.db.execute("UPDATE attempts SET identity_verified=1 WHERE attempt_id=? AND account=?",
                                (rows[0]["attempt_id"], self.account))
            if action == "OPEN":
                total, cost = self._order_totals(order_id)
                if total and (not legacy_purchase or confirm_legacy):
                    self.db.execute("""UPDATE attempts SET bought_size=?,entry_price=?,
                        position_id=attempt_id WHERE account=? AND attempt_id=?""",
                        (total, cost / total, self.account, rows[0]["attempt_id"]))
                return [dict(r) for r in self.db.execute("""SELECT fill_id AS id,size,price
                    FROM order_fills WHERE account=? AND order_id=? ORDER BY fill_id""",
                    (self.account, order_id))]
            self.close_flat_position(rows[0]["game_id"], rows[0]["channel"])
            return new

    def record_sell_fills(self, order_id, fills):
        """Only newly committed sells may reduce the in-memory position."""
        return self._record_active_fills(order_id, fills, "CLOSE")

    def record_buy_fills(self, order_id, fills, *, confirm_legacy=False):
        """Return all confirmed buy receipts for comparison with the signed order's final size."""
        return self._record_active_fills(order_id, fills, "OPEN", confirm_legacy=confirm_legacy)

    def finish_sell_order(self, attempt_id, order_id, total_matched):
        with self._write():
            matched = _amount(total_matched)
            total, _ = self._order_totals(order_id)
            if not matched or abs(total - matched) > EPS:
                return False
            return bool(self.db.execute("""UPDATE attempts SET state=?,updated_at=?,terminal_matched=?
                WHERE account=? AND attempt_id=? AND order_id=? COLLATE NOCASE
                AND action='CLOSE' AND state IN ('SUBMITTING','ACCEPTED','UNKNOWN','PARTIAL_FINAL')""",
                (PARTIAL_FINAL, int(time.time()), matched, self.account, attempt_id, order_id)).rowcount)

    def _update_task(self, row, **updates):
        if all(row[k] == v for k, v in updates.items()):
            return False
        updates["updated_at"] = int(time.time())
        assignments = ",".join(f"{k}=?" for k in updates)
        self.db.execute(f"UPDATE reconcile_tasks SET {assignments} "
                        "WHERE account=? AND game_id=? AND channel=?",
                        (*updates.values(), self.account, row["game_id"], row["channel"]))
        return True

    def _review(self, row, reason):
        return self._update_task(row, review_reason=reason,
                                 state="REDEEMABLE" if row["state"] == "REDEEMABLE" else "REVIEW")

    def _order_conflict(self, row, reason):
        self._update_task(row, state="REVIEW", review_reason=reason, zero_blocked=1)
        return False

    def mark_review(self, game, channel, reason):
        with self._write():
            row = self._task(game, channel)
            return bool(row and row["state"] != "DONE" and self._review(row, reason))

    def apply_order(self, game, channel, order_id, fills, terminal_matched=None, *, identity_verified=False):
        with self._write():
            row = self._task(game, channel)
            if (not row or row["action"] not in ("OPEN", "CLOSE") or not order_id
                    or row["order_id"].lower() != order_id.lower()):
                return False
            terminal = row["terminal_matched"]
            try:
                if terminal_matched is not None:
                    observed = _amount(terminal_matched)
                    if terminal is not None and abs(observed - terminal) > EPS:
                        raise ValueError("conflicting terminal matched totals")
                    terminal = observed
                new = self._new_fills(row, fills)
            except (KeyError, ValueError, TypeError) as exc:
                return self._order_conflict(row, str(exc))
            # The old signer hashed a different EIP-712 domain. A quantity (or
            # an empty response for that hash) cannot establish its identity.
            # The caller supplies either exact confirmed trade facts or an
            # explicitly validated, matching venue order response.
            verified = bool(row["identity_verified"] or identity_verified or new)
            if not verified:
                return False
            previous_size, previous_usd = self._order_totals(order_id)
            total_size = previous_size + sum(f["size"] for f in new)
            total_usd = previous_usd + sum(f["size"] * f["price"] for f in new)
            if row["reason"] == "legacy_log_unverified":
                # Old production could overwrite sales or report a balance-derived
                # purchase. Keep independently identified receipts alongside that claim.
                self._insert_fills(row, new)
                changed = self._update_task(row, state="REVIEW", zero_blocked=1,
                                            identity_verified=int(verified), terminal_matched=terminal,
                                            review_reason="legacy holding requires allocation review")
                return bool(new) or changed
            if terminal is not None and total_size - terminal > EPS:
                return self._order_conflict(row, "confirmed fills exceed terminal matched size")
            if terminal == 0 and row["zero_blocked"]:
                return self._order_conflict(row, "zero-fill evidence conflicts with prior order facts")
            final = terminal is not None and abs(total_size - terminal) <= EPS
            delta_size = total_size - row["applied_order_size"]
            delta_usd = total_usd - row["applied_order_usd"]
            changes = dict(identity_verified=1, terminal_matched=terminal)
            if row["review_reason"] == UNVERIFIED_REASON:
                changes.update(state="PENDING", review_reason="")
            if row["action"] == "CLOSE" and row["legacy_sold"] and not final:
                self._insert_fills(row, new)
                changes.update(state="REVIEW", review_reason="legacy partial sell awaiting complete original order")
                changed = self._update_task(row, **changes)
                return bool(new) or changed
            if delta_size < -EPS or (row["action"] == "CLOSE" and delta_size - row["size"] > EPS):
                return self._order_conflict(row, "confirmed size conflicts with the channel's remaining holding")
            self._insert_fills(row, new)
            size = row["size"]
            changes.update(applied_order_size=total_size, applied_order_usd=total_usd)
            if row["action"] == "OPEN":
                if total_size:
                    changes["entry"] = total_usd / total_size
                size = max(0., total_size - row["sold_shares"])
                changes["reserved_usd"] = max(0., row["initial_reserved_usd"] - total_usd)
            else:
                size = max(0., size - delta_size)
                changes.update(sold_shares=row["sold_shares"] + delta_size,
                               sell_proceeds=row["sell_proceeds"] + delta_usd)
            changes["size"] = size
            if final:
                if row["action"] == "OPEN":
                    # An old FILLED attempt may need to reconstruct its gross purchase
                    # before the linked sell can be accounted for. The game handed
                    # over both signed IDs and all prior sell facts atomically.
                    if row["sold_shares"] - total_size > EPS:
                        return self._order_conflict(row, "recovered purchase smaller than known sales")
                    # Task size already excludes confirmed sales at handoff.
                changes.update(action="HOLD", state="PENDING" if size > EPS else "DONE",
                               reserved_usd=0., review_reason="")
                if row["action"] == "OPEN" and row["next_close_order_id"]:
                    oid = row["next_close_order_id"]
                    sold, proceeds = self._order_totals(oid)
                    changes.update(action="CLOSE", state="PENDING", order_id=oid,
                                   terminal_matched=None, applied_order_size=sold + row["legacy_sold"],
                                   applied_order_usd=proceeds, zero_blocked=0,
                                   identity_verified=row["next_close_verified"])
                    if row["legacy_sold"]:
                        changes.update(state="REVIEW", review_reason="legacy partial sell awaiting complete original order")
                    if not row["next_close_verified"]:
                        changes.update(state="REVIEW", review_reason=UNVERIFIED_REASON)
            return self._update_task(row, **changes)

    def link_order(self, game, channel, old_id, venue_id, reference):
        """Audit a verified venue identity without modifying the original signed attempt."""
        if not all(isinstance(v, str) and v.strip() for v in (old_id, venue_id, reference)):
            return False
        old_id, venue_id, reference = old_id.strip(), venue_id.strip(), reference.strip()
        key = self.account, game, channel
        with self._write():
            prior = self.db.execute("""SELECT venue_id,reference FROM order_links WHERE
                account=? AND game_id=? AND channel=? AND old_id=?""", (*key, old_id)).fetchone()
            if prior:
                return (prior["venue_id"].lower() == venue_id.lower()
                        and prior["reference"].lower() == reference.lower())
            row = self._task(game, channel)
            if (not row or row["state"] != "REVIEW" or row["action"] not in ("OPEN", "CLOSE")
                    or row["identity_verified"] or row["order_id"].lower() != old_id.lower()
                    or self._used_reference(reference) or self._other_owner(game, channel, venue_id)):
                return False
            if old_id.lower() != venue_id.lower():
                # Never transplant receipts or a terminal quantity from one
                # order to another. Old log-only sells keep their baseline
                # until every confirmed trade of the linked order is proven.
                legacy_baseline = row["legacy_sold"] if row["action"] == "CLOSE" else 0
                if (self._order_totals(old_id)[0] or self._order_totals(venue_id)[0]
                        or row["applied_order_size"] > legacy_baseline or row["applied_order_usd"]
                        or row["terminal_matched"] is not None):
                    return False
                if self.db.execute("""
                    SELECT 1 FROM attempts WHERE account=? AND order_id=? COLLATE NOCASE
                    UNION ALL SELECT 1 FROM reconcile_tasks WHERE account=?
                        AND ? COLLATE NOCASE IN (initial_order_id,next_close_order_id,order_id)
                    UNION ALL SELECT 1 FROM order_links WHERE account=?
                        AND ? COLLATE NOCASE IN (old_id,venue_id) LIMIT 1""",
                        (self.account, venue_id) * 3).fetchone():
                    return False
            self.db.execute("""INSERT INTO order_links
                (account,game_id,channel,action,old_id,venue_id,reference,created_at)
                VALUES (?,?,?,?,?,?,?,?)""", (*key, row["action"], row["order_id"], venue_id,
                                              reference, int(time.time())))
            # Remain in review until the original order has actual terminal
            # evidence; an identity link alone releases no cash or shares.
            self._update_task(row, order_id=venue_id, identity_verified=1,
                              review_reason="verified venue order linked; awaiting order evidence")
            return True

    def confirm_zero(self, game, channel, order_id, reference):
        """Book an operator-verified final-zero receipt for the reviewed signed order."""
        if not isinstance(order_id, str) or not isinstance(reference, str) or not reference.strip():
            return False
        reference = reference.strip()
        with self._write():
            row = self._task(game, channel)
            if not row or row["order_id"].lower() != order_id.lower():
                return False
            if row["zero_ref"]:
                return (row["zero_ref"].lower() == reference.lower()
                        and row["zero_order_id"].lower() == order_id.lower())
            if (row["state"] != "REVIEW" or not row["identity_verified"] or row["action"] not in ("OPEN", "CLOSE")
                    or row["zero_blocked"] or row["applied_order_size"] or row["terminal_matched"]
                    or self._order_totals(order_id)[0] or self._used_reference(reference)):
                return False
            return self._update_task(row, action="HOLD", state="PENDING" if row["size"] else "DONE",
                                     reserved_usd=0., terminal_matched=0., review_reason="",
                                     zero_ref=reference, zero_order_id=row["order_id"])

    def _used_reference(self, reference):
        return self.db.execute("""SELECT 1 FROM reconcile_tasks WHERE account=?
            AND (redemption_ref=? OR zero_ref=?)
            UNION ALL SELECT 1 FROM order_links WHERE account=? AND reference=? LIMIT 1""",
            (self.account, reference, reference, self.account, reference)).fetchone()

    def apply_resolution(self, game, channel, price):
        try:
            price = _amount(price, price=True)
        except (ValueError, TypeError):
            return False
        with self._write():
            row = self._task(game, channel)
            if (not row or row["action"] != "HOLD" or row["state"] == "DONE"
                    or row["reason"] == "legacy_log_unverified"
                    or row["payout_price"] is not None or row["size"] <= 0):
                return False
            changes = dict(payout_price=price, review_reason="", state="REDEEMABLE")
            if price == 0:
                changes.update(size=0., settled_shares=row["size"], state="DONE")
            return self._update_task(row, **changes)

    def confirm_redemption(self, game, channel, reference, shares, payout):
        if not isinstance(reference, str) or not reference.strip():
            return False
        reference = reference.strip()
        try:
            shares = _amount(shares, positive=True)
            payout = _amount(payout, positive=True)
        except (ValueError, TypeError):
            return False
        with self._write():
            row = self._task(game, channel)
            if not row:
                return False
            if row["state"] == "DONE":
                return (bool(row["redemption_ref"]) and row["redemption_ref"].lower() == reference.lower()
                        and abs(row["settled_shares"] - shares) <= EPS
                        and abs(row["settled_payout"] - payout) <= EPS)
            if (row["state"] != "REDEEMABLE" or abs(row["size"] - shares) > EPS or
                    abs(shares * row["payout_price"] - payout) > EPS):
                return False
            if self._used_reference(reference):
                return False
            return self._update_task(row, state="DONE", size=0., settled_shares=shares,
                                     settled_payout=payout, redemption_ref=reference, review_reason="")

    def close_path(self):
        with self.lock:
            self.db.close()
