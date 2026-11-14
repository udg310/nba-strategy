"""Transactional SQLite order ledger. Money and shares use integer millionths."""

from contextlib import contextmanager
from pathlib import Path
import secrets
import sqlite3
from threading import RLock


MONEY_SCALE = 1_000_000
MAX_AMOUNT = 10**15
INT64_MAX = 2**63 - 1
ACTIVE = ("SUBMITTING", "ACCEPTED", "UNKNOWN", "PARTIAL")
PENDING_SQL = "('READY','SUBMITTING','ACCEPTED','UNKNOWN','PARTIAL')"
INTENT_FIELDS = (
    "account", "environment", "game_id", "channel", "action", "asset", "market",
    "position_id", "reason", "amount", "limit_price", "signal_at", "deadline",
)


class Invalid(ValueError):
    """An invalid request or response from the venue."""


class Conflict(Exception):
    """The request contradicts previously recorded information."""


class Risk(Conflict):
    """The ledger cannot release the requested capital or shares."""


def identifier():
    # Same 26-character RFC 4648 alphabet as crypto/rand.Text in the old writer.
    return "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567") for _ in range(26))


def integer(data, key, low=-(2**63), high=INT64_MAX):
    value = data.get(key, 0)
    if type(value) is not int or not low <= value <= high:
        raise Invalid(f"invalid {key}")
    return value


def string(data, key):
    value = data.get(key, "")
    if not isinstance(value, str):
        raise Invalid(f"invalid {key}")
    return value


def boolean(data, key):
    value = data.get(key, False)
    if type(value) is not bool:
        raise Invalid(f"invalid {key}")
    return value


def execute_schema(conn, sql):
    """Execute DDL inside the caller's transaction, including trigger bodies."""
    statement = ""
    for section in sql.split(";")[:-1]:
        statement += section + ";"
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    tail = statement + sql.rsplit(";", 1)[-1]
    if tail.strip():
        conn.execute(tail)


def public_intent(intent):
    result = dict(intent)
    for key in ("reason", "order_id", "attempt_id", "position_id"):
        if not result[key]:
            del result[key]
    return result


def read_intent(conn, intent_id):
    row = conn.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone()
    if row is None:
        raise KeyError(intent_id)
    return dict(row)


def append_event(conn, intent, now, reason):
    conn.execute("""INSERT INTO events(intent_id,attempt_id,at,state,reason)
                    VALUES(?,?,?,?,?)""",
                 (intent["id"], intent["attempt_id"], now, intent["state"], reason))


def update_intent(conn, intent):
    conn.execute("""UPDATE intents SET state=:state, reserved=:reserved, filled=:filled,
                    cost=:cost, fee=:fee, last_check=:last_check, attempt_no=:attempt_no,
                    attempt_id=:attempt_id, order_id=:order_id WHERE id=:id""", intent)


def _has_external_feed(conn):
    return conn.execute("""SELECT 1 FROM sqlite_master
                           WHERE type='table' AND name='external_fills'""").fetchone() is not None


def cash_committed(conn):
    used = conn.execute("""SELECT coalesce(sum(CASE action WHEN 'OPEN' THEN cost+fee+reserved
                           ELSE fee-cost END),0) - (SELECT coalesce(sum(payout-fee),0)
                           FROM settlements) FROM intents""").fetchone()[0]
    if _has_external_feed(conn):
        used += conn.execute("""SELECT coalesce(sum(CASE side WHEN 'BUY' THEN notional+fee
                                ELSE fee-notional END),0) FROM external_fills
                                WHERE confirmed=1 AND disposition='manual'""").fetchone()[0]
    return used


def _check_hold(conn, action, asset):
    if action == "OPEN" and conn.execute(
        "SELECT paused FROM execution_control WHERE singleton=1"
    ).fetchone()[0]:
        raise Risk("opening orders paused")
    if _has_external_feed(conn) and conn.execute(
        """SELECT 1 FROM external_fills f WHERE f.disposition='' AND f.confirmed=1
           AND (?='OPEN' OR f.asset=?) AND NOT EXISTS
           (SELECT 1 FROM attempts a WHERE a.order_id=f.order_id) LIMIT 1""",
        (action, asset),
    ).fetchone():
        raise Risk("unassigned external fill under review")


def _save_close_request(conn, intent_id, req):
    conn.execute("""INSERT INTO close_requests(intent_id,signal_at,amount,limit_price,deadline)
                    VALUES(?,?,?,?,?)""",
                 (intent_id, req["signal_at"], req["amount"], req["limit_price"], req["deadline"]))


def _existing_close(conn, old, req, now):
    terms = conn.execute("""SELECT amount,limit_price FROM close_requests
                            WHERE intent_id=? AND signal_at=?""",
                         (old["id"], req["signal_at"])).fetchone()
    if terms is not None:
        if tuple(terms) != (req["amount"], req["limit_price"]):
            raise Conflict("exit terms differ for original signal")
        return old

    remainder = old["amount"] - old["filled"]
    correct_quantity = req["amount"] in (old["amount"], remainder)
    rearmable = old["state"] in ("PARTIAL_FINAL", "REJECTED_ZERO", "EXPIRED_UNSENT")
    if (not rearmable or req["signal_at"] <= max(old["last_check"], old["signal_at"])
            or req["deadline"] <= now):
        if not correct_quantity:
            raise Conflict("exit amount differs")
        return old
    if conn.execute("SELECT 1 FROM settlements WHERE position_id=?", (old["position_id"],)).fetchone():
        raise Risk("position already settled")
    if remainder <= 0 or not correct_quantity:
        raise Risk("exit exceeds verified remainder")
    _check_hold(conn, "CLOSE", req["asset"])
    _save_close_request(conn, old["id"], req)
    old.update({key: req[key] for key in ("signal_at", "deadline", "limit_price", "reason")})
    old.update(state="READY", reserved=remainder, last_check=now)
    conn.execute("""UPDATE intents SET signal_at=:signal_at,deadline=:deadline,
                    limit_price=:limit_price,reason=:reason WHERE id=:id""", old)
    update_intent(conn, old)
    append_event(conn, old, now, "fresh exit signal for verified remainder")
    return old


def current_attempt(conn, attempt_id):
    row = conn.execute("SELECT intent_id FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    if row is None:
        raise KeyError(attempt_id)
    intent = read_intent(conn, row["intent_id"])
    return intent, intent["attempt_id"] == attempt_id and intent["state"] in ACTIVE


def attempt_state(conn, intent, state, now):
    conn.execute("""UPDATE attempts SET state=?,order_id=nullif(?,''),updated_at=? WHERE id=?""",
                 (state, intent["order_id"], now, intent["attempt_id"]))


def _bind_order(intent, order_id):
    if order_id:
        if intent["order_id"] and intent["order_id"] != order_id:
            raise Conflict("exchange order ID mismatch")
        intent["order_id"] = order_id


def _finish_attempt(intent, terminal, retryable, now):
    retry_zero = terminal == "REJECTED_ZERO" and retryable and intent["attempt_no"] == 1
    retry_exit = terminal == "PARTIAL_FINAL" and intent["action"] == "CLOSE" and intent["filled"] < intent["amount"]
    if (retry_zero or retry_exit) and now < intent["deadline"]:
        intent["state"] = "READY"
    else:
        intent.update(state=terminal, reserved=0)


def fill_cost(quantity, price):
    return (quantity * price + MONEY_SCALE // 2) // MONEY_SCALE


def record_fill(conn, intent, fill):
    if not isinstance(fill, dict):
        raise Invalid("expected a fill object")
    side = "BUY" if intent["action"] == "OPEN" else "SELL"
    if (string(fill, "order_id") != intent["order_id"] or string(fill, "asset") != intent["asset"]
            or string(fill, "side").upper() != side or string(fill, "game_id") not in ("", intent["game_id"])):
        raise Conflict("fill identity disagrees with order")
    if not boolean(fill, "confirmed"):
        return
    fill_id = string(fill, "id")
    if not fill_id:
        raise Invalid("confirmed fill requires ID")
    quantity = integer(fill, "quantity", 1, MAX_AMOUNT)
    price = integer(fill, "price", 1, MONEY_SCALE)
    fee = integer(fill, "fee", 0, MAX_AMOUNT)
    cost = integer(fill, "notional", 0, MAX_AMOUNT) or fill_cost(quantity, price)
    timestamp = integer(fill, "time")
    existing = conn.execute("""SELECT quantity,cost,fee,price FROM fills
                               WHERE order_id=? AND id=?""", (intent["order_id"], fill_id)).fetchone()
    if existing is not None:
        if tuple(existing) != (quantity, cost, fee, price):
            raise Conflict("confirmed fill changed")
        return
    conn.execute("""INSERT INTO fills(order_id,id,intent_id,asset,side,quantity,price,cost,fee,time)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""", (intent["order_id"], fill_id, intent["id"], intent["asset"],
                                                       side, quantity, price, cost, fee, timestamp))
    intent["filled"] += quantity
    intent["cost"] += cost
    intent["fee"] += fee
    spent = intent["filled"] if side == "SELL" else intent["cost"] + intent["fee"]
    intent["reserved"] = max(0, intent["amount"] - spent)
    append_event(conn, intent, intent["last_check"], "confirmed fill " + fill_id)


class Ledger:
    def __init__(self, path, account, environment, capital, max_open):
        if (not isinstance(account, str) or not account or not isinstance(environment, str)
                or not environment or type(capital) is not int or not 0 < capital <= MAX_AMOUNT
                or type(max_open) is not int or max_open < 1):
            raise Invalid("invalid ledger configuration")
        self.account, self.environment = account, environment
        self.capital, self.max_open = capital, max_open
        self._lock = RLock()
        self._db = sqlite3.connect(path, timeout=3, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            if self._db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise RuntimeError("could not initialize SQLite WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA foreign_keys=ON")
            with self.tx() as conn:
                self._initialize(conn)
        except BaseException:
            self._db.close()
            raise

    def _initialize(self, conn):
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            raise ValueError(f"unsupported ledger version {version}")
        if version == 0:
            execute_schema(conn, Path(__file__).with_name("executor_schema.sql").read_text(encoding="utf-8"))
            conn.execute("INSERT INTO ledger_meta VALUES(1,?,?,?)", (self.account, self.environment, self.capital))
        if version < 2:
            conn.execute("""CREATE TABLE execution_control (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                            paused INTEGER NOT NULL CHECK(paused IN (0,1)))""")
            conn.execute("INSERT INTO execution_control VALUES(1,0)")
            conn.execute("PRAGMA user_version=2")
        owner = conn.execute("""SELECT account,environment,starting_capital FROM ledger_meta
                                WHERE singleton=1""").fetchone()
        if owner is None:
            raise KeyError("ledger_meta")
        if (owner["account"], owner["environment"]) != (self.account, self.environment):
            raise Conflict("database belongs to another account or environment")
        if owner["starting_capital"] != self.capital:
            raise Conflict(f"starting capital differs from initial ledger configuration ({owner['starting_capital']} micros)")

    @contextmanager
    def tx(self):
        # BEGIN IMMEDIATE also serializes writers using other SQLite connections.
        with self._lock:
            conn = self._db
            nested = conn.in_transaction
            conn.execute("SAVEPOINT nested" if nested else "BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("RELEASE nested" if nested else "COMMIT")
            except BaseException:
                if nested:
                    conn.execute("ROLLBACK TO nested")
                    conn.execute("RELEASE nested")
                else:
                    conn.rollback()
                raise

    def close(self):
        with self._lock:
            self._db.close()

    def get(self, intent_id):
        with self.tx() as conn:
            return public_intent(read_intent(conn, intent_id))

    def pending(self):
        with self.tx() as conn:
            return [public_intent(row) for row in conn.execute(
                f"SELECT * FROM intents WHERE state IN {PENDING_SQL} ORDER BY last_check,id")]

    def available(self):
        with self.tx() as conn:
            return self.capital - cash_committed(conn)

    def _request(self, req):
        if not isinstance(req, dict) or req.keys() - set(INTENT_FIELDS):
            raise Invalid("expected an order-intent object")
        r = {key: string(req, key) for key in INTENT_FIELDS[:9]}
        if (r["account"] != self.account or r["environment"] != self.environment
                or any(not r[key].strip() for key in ("game_id", "asset", "market"))):
            raise Invalid("missing identifiers or wrong account/environment")
        if r["channel"] not in ("A", "C", "D") or r["action"] not in ("OPEN", "CLOSE"):
            raise Invalid("unsupported strategy channel or action")
        if (r["action"] == "CLOSE") != bool(r["position_id"]):
            raise Invalid("CLOSE requires its opening position ID")
        r["amount"] = integer(req, "amount", 1, MAX_AMOUNT)
        r["limit_price"] = integer(req, "limit_price", 1, MONEY_SCALE)
        r["signal_at"] = integer(req, "signal_at", 1)
        r["deadline"] = integer(req, "deadline", r["signal_at"])
        return r

    def _reserve(self, conn, r):
        if r["action"] == "CLOSE":
            row = conn.execute("""SELECT filled,state FROM intents WHERE id=:position_id AND action='OPEN'
                                  AND game_id=:game_id AND channel=:channel AND asset=:asset AND market=:market
                                  AND NOT EXISTS (SELECT 1 FROM settlements WHERE position_id=intents.id)""", r).fetchone()
            if row is None or r["amount"] != row["filled"] or row["state"] not in ("FILLED", "PARTIAL_FINAL"):
                raise Risk("full-position exit requires a reconciled opening and its confirmed quantity")
            return
        if r["amount"] > self.capital - cash_committed(conn):
            raise Risk("insufficient available cash")
        slots = conn.execute("""SELECT count(*) FROM intents o LEFT JOIN intents c
                                ON c.position_id=o.id AND c.action='CLOSE' WHERE o.action='OPEN'
                                AND (o.reserved>0 OR o.filled>coalesce(c.filled,0))
                                AND NOT EXISTS(SELECT 1 FROM settlements WHERE position_id=o.id)""").fetchone()[0]
        if slots >= self.max_open:
            raise Risk("position limit reached")

    def create(self, req, now):
        r = self._request(req)
        with self.tx() as conn:
            if r["action"] == "CLOSE":
                previous = conn.execute("""SELECT * FROM intents WHERE action='CLOSE'
                                           AND position_id=:position_id""", r).fetchone()
            else:
                previous = conn.execute("""SELECT * FROM intents WHERE action='OPEN'
                                           AND account=:account AND environment=:environment
                                           AND game_id=:game_id AND channel=:channel""", r).fetchone()
            if previous is not None:
                old = dict(previous)
                if any(old[key] != r[key] for key in ("asset", "market", "game_id", "channel")):
                    raise Conflict("position identity differs")
                if r["action"] == "CLOSE":
                    old = _existing_close(conn, old, r, now)
                elif old["amount"] != r["amount"] or old["limit_price"] != r["limit_price"]:
                    raise Conflict("immutable order terms differ")
                return public_intent(old), False

            expired = now >= r["deadline"]
            if not expired:
                _check_hold(conn, r["action"], r["asset"])
            if not expired or r["action"] == "CLOSE":
                self._reserve(conn, r)
            r.update(id=identifier(), state="EXPIRED_UNSENT" if expired else "READY",
                     reserved=0 if expired else r["amount"], last_check=now)
            if r["action"] == "OPEN":
                r["position_id"] = r["id"]
            conn.execute("""INSERT INTO intents(id,account,environment,game_id,channel,action,asset,market,
                            position_id,reason,amount,limit_price,signal_at,deadline,state,reserved,last_check)
                            VALUES(:id,:account,:environment,:game_id,:channel,:action,:asset,:market,
                            :position_id,:reason,:amount,:limit_price,:signal_at,:deadline,:state,:reserved,:last_check)""", r)
            if r["action"] == "CLOSE":
                _save_close_request(conn, r["id"], r)
            intent = read_intent(conn, r["id"])
            append_event(conn, intent, now, "intent recorded")
            return public_intent(intent), True

    def claim(self, intent_id, now):
        with self.tx() as conn:
            i = read_intent(conn, intent_id)
            if i["state"] != "READY":
                return public_intent(i), False
            i["last_check"] = now
            if now >= i["deadline"]:
                i.update(state="PARTIAL_FINAL" if i["filled"] else "EXPIRED_UNSENT", reserved=0)
            else:
                _check_hold(conn, i["action"], i["asset"])
                if i["action"] == "OPEN" and cash_committed(conn) > self.capital:
                    raise Risk("reconciled debits exceed capital")
                i.update(state="SUBMITTING", attempt_no=i["attempt_no"] + 1, attempt_id=identifier(), order_id="")
                conn.execute("""INSERT INTO attempts(id,intent_id,number,state,amount,limit_price,created_at,updated_at)
                                VALUES(:attempt_id,:id,:attempt_no,:state,:reserved,:limit_price,:last_check,:last_check)""", i)
            update_intent(conn, i)
            append_event(conn, i, now, "dispatch decision")
            return public_intent(i), i["state"] == "SUBMITTING"

    def record_submit(self, attempt_id, result, now):
        with self.tx() as conn:
            i, active = current_attempt(conn, attempt_id)
            if not active:
                return
            if not isinstance(result, dict):
                raise Invalid("expected a submit result object")
            _bind_order(i, string(result, "order_id"))
            state = string(result, "state")
            reason = string(result, "reason")
            retryable = boolean(result, "retryable")
            accepted = i["state"] in ("PARTIAL", "ACCEPTED")
            if state in ("ACCEPTED", "UNKNOWN"):
                if not accepted:
                    i["state"] = state
            elif state == "REJECTED_ZERO":
                if accepted:
                    raise Conflict("rejection contradicts prior acceptance")
                _finish_attempt(i, state, retryable, now)
            else:
                raise Invalid("invalid submit result")
            i["last_check"] = now
            attempt_state(conn, i, state if state == "REJECTED_ZERO" else i["state"], now)
            update_intent(conn, i)
            append_event(conn, i, now, state + " " + reason)

    def apply(self, attempt_id, snapshot, now):
        with self.tx() as conn:
            i, active = current_attempt(conn, attempt_id)
            if not active:
                return
            if not isinstance(snapshot, dict):
                raise Invalid("expected an order snapshot object")
            total = integer(snapshot, "total_filled", 0)
            state = string(snapshot, "state")
            order = string(snapshot, "order_id")
            retryable = boolean(snapshot, "retryable")
            if state not in ("FINAL", "OPEN", "REJECTED_ZERO"):
                raise Invalid("invalid order snapshot")
            if not order and state != "REJECTED_ZERO":
                raise Invalid("lookup requires exact exchange identity")
            _bind_order(i, order)
            previous = i["state"]
            i["last_check"] = now
            seen = set()
            visible_matched = 0
            fills = snapshot.get("fills")
            if fills is not None and not isinstance(fills, list):
                raise Invalid("expected a list of fills")
            for fill in fills or []:
                record_fill(conn, i, fill)
                fill_id = string(fill, "id")
                if not fill_id:
                    raise Invalid("matched fill requires ID")
                if fill_id not in seen:
                    visible_matched += integer(fill, "quantity", 1, MAX_AMOUNT - visible_matched)
                    seen.add(fill_id)
            amount = conn.execute("""SELECT coalesce(sum(quantity),0) FROM fills
                                     WHERE order_id=?""", (i["order_id"],)).fetchone()[0]
            matched = conn.execute("""UPDATE attempts SET matched_high_water=max(matched_high_water,?,?,?)
                                      WHERE id=? RETURNING matched_high_water""",
                                   (amount, total, visible_matched, attempt_id)).fetchone()[0]
            terminal = None
            if state == "REJECTED_ZERO":
                if matched:
                    raise Conflict("zero-fill rejection contradicts known fills")
                terminal = "REJECTED_ZERO"
            elif state == "FINAL" and amount == total == matched:
                if amount == 0:
                    terminal = "REJECTED_ZERO"
                elif i["action"] == "CLOSE" and i["filled"] < i["amount"]:
                    terminal = "PARTIAL_FINAL"
                else:
                    terminal = "FILLED"
            if terminal:
                _finish_attempt(i, terminal, retryable, now)
            else:
                i["state"] = "PARTIAL" if amount else "ACCEPTED"
            attempt_state(conn, i, terminal or i["state"], now)
            update_intent(conn, i)
            if previous != i["state"] or terminal:
                append_event(conn, i, now, "exchange " + (terminal or i["state"]))

    def settle(self, req, now):
        fields = ("position_id", "venue_event", "leg_id", "shares", "payout", "fee", "actor", "evidence", "confirmed")
        if not isinstance(req, dict) or req.keys() - set(fields):
            raise Invalid("expected settlement object")
        r = {key: string(req, key) for key in ("position_id", "venue_event", "leg_id", "actor", "evidence")}
        if (not boolean(req, "confirmed") or not r["position_id"]
                or any(not r[key].strip() for key in ("venue_event", "leg_id", "actor", "evidence"))):
            raise Invalid("confirmed redemption proof, unique leg and position required")
        r["shares"] = integer(req, "shares", 1, MAX_AMOUNT)
        r["payout"] = integer(req, "payout", 0, r["shares"])
        r["fee"] = integer(req, "fee", 0, r["payout"])
        with self.tx() as conn:
            prior = conn.execute("""SELECT position_id,venue_event,leg_id,shares,payout,fee FROM settlements
                                    WHERE position_id=:position_id OR (venue_event=:venue_event AND leg_id=:leg_id)""", r).fetchone()
            if prior is not None:
                if any(prior[key] != r[key] for key in prior.keys()):
                    raise Conflict("redemption leg or position already settled")
                return False
            i = read_intent(conn, r["position_id"])
            if i["action"] != "OPEN":
                raise KeyError(r["position_id"])
            if i["channel"] != "D" or i["state"] not in ("FILLED", "PARTIAL_FINAL"):
                raise Risk("settlement requires a confirmed D position")
            _check_hold(conn, "CLOSE", i["asset"])
            sold, pending = conn.execute(f"""SELECT coalesce(sum(filled),0),count(*) FILTER(WHERE state IN {PENDING_SQL})
                                             FROM intents WHERE position_id=? AND action='CLOSE'""", (i["id"],)).fetchone()
            if pending or r["shares"] != i["filled"] - sold:
                raise Risk("redemption does not cover verified unreserved shares")
            r["at"] = now
            conn.execute("""INSERT INTO settlements(position_id,venue_event,leg_id,shares,payout,fee,actor,evidence,at)
                            VALUES(:position_id,:venue_event,:leg_id,:shares,:payout,:fee,:actor,:evidence,:at)""", r)
            append_event(conn, i, now, f"verified settlement {r['venue_event']} {r['leg_id']}")
            return True
