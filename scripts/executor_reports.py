"""Account reconciliation, immutable game report revisions and operator review."""

import json
import sqlite3
import time

from executor_ledger import (
    Conflict, INT64_MAX, Invalid, MAX_AMOUNT, MONEY_SCALE, append_event, attempt_state,
    boolean, current_attempt, execute_schema, fill_cost, integer, public_intent, read_intent,
    record_fill, string, update_intent,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS external_fills (
 order_id TEXT NOT NULL,id TEXT NOT NULL,asset TEXT NOT NULL,side TEXT NOT NULL,
 quantity INTEGER NOT NULL,price INTEGER NOT NULL,notional INTEGER NOT NULL,fee INTEGER NOT NULL,time INTEGER NOT NULL,
 game_id TEXT NOT NULL DEFAULT '',confirmed INTEGER NOT NULL,disposition TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(order_id,id)
);
CREATE INDEX IF NOT EXISTS external_fills_asset ON external_fills(asset);
CREATE TABLE IF NOT EXISTS feed_progress (singleton INTEGER PRIMARY KEY CHECK(singleton=1), cursor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
 game_id TEXT NOT NULL,source_id TEXT NOT NULL,at INTEGER NOT NULL,body TEXT NOT NULL,
 PRIMARY KEY(game_id,source_id)
);
CREATE TABLE IF NOT EXISTS completed_games (game_id TEXT PRIMARY KEY, finished_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS game_reports (game_id TEXT NOT NULL,revision INTEGER NOT NULL,at INTEGER NOT NULL,body TEXT NOT NULL,
 PRIMARY KEY(game_id,revision));
CREATE TABLE IF NOT EXISTS review_actions (id INTEGER PRIMARY KEY,at INTEGER NOT NULL,actor TEXT NOT NULL,
 evidence TEXT NOT NULL,operation TEXT NOT NULL,subject TEXT NOT NULL,before_state TEXT NOT NULL,after_state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dirty_reports (game_id TEXT PRIMARY KEY);
CREATE TRIGGER IF NOT EXISTS report_new_intent AFTER INSERT ON intents BEGIN
 INSERT OR IGNORE INTO dirty_reports SELECT game_id FROM completed_games WHERE game_id=NEW.game_id;
END;
CREATE TRIGGER IF NOT EXISTS report_changed_intent AFTER UPDATE ON intents
 WHEN (OLD.state,OLD.filled,OLD.cost,OLD.fee,OLD.order_id,OLD.limit_price)!=(NEW.state,NEW.filled,NEW.cost,NEW.fee,NEW.order_id,NEW.limit_price) BEGIN
 INSERT OR IGNORE INTO dirty_reports SELECT game_id FROM completed_games WHERE game_id=NEW.game_id;
END;
CREATE TRIGGER IF NOT EXISTS report_new_fill AFTER INSERT ON external_fills BEGIN
 INSERT OR IGNORE INTO dirty_reports SELECT game_id FROM completed_games WHERE game_id=NEW.game_id OR game_id IN
 (SELECT game_id FROM intents WHERE asset=NEW.asset AND (NEW.game_id='' OR id IN(SELECT intent_id FROM attempts WHERE order_id=NEW.order_id)));
END;
CREATE TRIGGER IF NOT EXISTS report_changed_fill AFTER UPDATE ON external_fills
 WHEN (OLD.game_id,OLD.confirmed,OLD.notional,OLD.fee,OLD.disposition)!=(NEW.game_id,NEW.confirmed,NEW.notional,NEW.fee,NEW.disposition) BEGIN
 INSERT OR IGNORE INTO dirty_reports SELECT game_id FROM completed_games WHERE game_id IN (NEW.game_id,OLD.game_id)
 OR game_id IN(SELECT game_id FROM intents WHERE asset=NEW.asset);
END;
CREATE TRIGGER IF NOT EXISTS report_first_account_page AFTER INSERT ON feed_progress BEGIN
 INSERT OR IGNORE INTO dirty_reports SELECT game_id FROM completed_games;
END;
CREATE TRIGGER IF NOT EXISTS report_settlement AFTER INSERT ON settlements BEGIN
 INSERT OR IGNORE INTO dirty_reports SELECT c.game_id FROM completed_games c JOIN intents i ON i.game_id=c.game_id WHERE i.id=NEW.position_id;
END;
CREATE TRIGGER IF NOT EXISTS report_assigned_order AFTER UPDATE OF order_id ON attempts
 WHEN NEW.order_id IS NOT NULL AND NEW.order_id IS NOT OLD.order_id BEGIN
 INSERT OR IGNORE INTO dirty_reports
 SELECT c.game_id FROM completed_games c WHERE EXISTS (
   SELECT 1 FROM external_fills f WHERE f.order_id=NEW.order_id AND
    (f.game_id=c.game_id OR (f.game_id='' AND EXISTS
     (SELECT 1 FROM intents i WHERE i.game_id=c.game_id AND i.asset=f.asset))));
END;
"""

ACTIVE = {"READY", "SUBMITTING", "UNKNOWN", "ACCEPTED", "PARTIAL"}
NO_OWNER = "NOT EXISTS (SELECT 1 FROM attempts a WHERE a.order_id=f.order_id)"
UNASSIGNED_COUNT = f"SELECT count(*) FROM external_fills f WHERE f.confirmed=1 AND f.disposition='' AND {NO_OWNER}"
EVENTS = "SELECT at,attempt_id,state,reason FROM events WHERE intent_id=? ORDER BY seq"


def _now():
    return time.time_ns() // 1_000_000


def _rows(db, query, *args):
    return [dict(row) for row in db.execute(query, args)]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _fields(body, allowed):
    if not isinstance(body, dict) or body.keys() - allowed:
        raise Invalid("unexpected JSON fields")


def _observe_fill(db, fill):
    if not isinstance(fill, dict):
        raise Invalid("expected a venue fill")
    if not all(string(fill, k) for k in ("id", "order_id", "asset")):
        raise Invalid("missing fill identity")
    if fill.get("side") not in ("BUY", "SELL"):
        raise Invalid("invalid venue fill side")
    quantity = integer(fill, "quantity", 1, MAX_AMOUNT)
    price = integer(fill, "price", 1, MONEY_SCALE)
    value = dict(fill, confirmed=boolean(fill, "confirmed"), game_id=string(fill, "game_id"),
                 fee=integer(fill, "fee", 0, MAX_AMOUNT), time=integer(fill, "time"),
                 notional=integer(fill, "notional", 0, MAX_AMOUNT) or fill_cost(quantity, price))
    inserted = db.execute("""
        INSERT INTO external_fills(order_id,id,asset,side,quantity,price,notional,fee,time,game_id,confirmed)
        VALUES(:order_id,:id,:asset,:side,:quantity,:price,:notional,:fee,:time,:game_id,:confirmed)
        ON CONFLICT(order_id,id) DO NOTHING
    """, value)
    if inserted.rowcount:
        return
    # A standalone UPDATE preserves the legacy dirty-report trigger's OR IGNORE
    # policy when several fills in this page affect the same finished game.
    result = db.execute("""
        UPDATE external_fills SET confirmed=max(confirmed,:confirmed),
         notional=CASE WHEN :confirmed THEN :notional ELSE notional END,
         fee=CASE WHEN :confirmed THEN :fee ELSE fee END,
         game_id=CASE WHEN game_id='' THEN :game_id ELSE game_id END
        WHERE order_id=:order_id AND id=:id AND asset=:asset AND side=:side AND quantity=:quantity AND price=:price
         AND (game_id='' OR :game_id='' OR game_id=:game_id)
         AND (confirmed=0 OR :confirmed=0 OR (fee=:fee AND notional=:notional))
    """, value)
    if not result.rowcount:
        raise Conflict("inconsistent venue fill")


def _game_summary(db, game):
    finished, observed = db.execute("""
        SELECT EXISTS(SELECT 1 FROM completed_games WHERE game_id=?), EXISTS(SELECT 1 FROM feed_progress)
    """, (game,)).fetchone()
    lines = _rows(db, """
        SELECT i.channel,i.action,i.asset,i.id AS intent_id,i.position_id,i.state,i.amount AS planned,
               i.limit_price,i.filled AS ledger_quantity,i.cost AS ledger_cost,i.fee AS ledger_fee,
               coalesce(s.shares,0) AS settled_shares,coalesce(s.payout,0) AS settlement_payout,
               coalesce(s.fee,0) AS settlement_fee,
               CASE WHEN i.action='OPEN' THEN i.filled-coalesce(s.shares,0)-coalesce((
                  SELECT sum(c.filled) FROM intents c WHERE c.action='CLOSE' AND c.position_id=i.id
               ),0) ELSE 0 END AS remaining_shares
        FROM intents i LEFT JOIN settlements s ON s.position_id=i.id
        WHERE i.game_id=? ORDER BY i.channel,i.action,i.id
    """, game)
    report = dict(game_id=game, status="MATCHED" if finished and observed else "PENDING",
                  finished=bool(finished), intents=lines, venue_trades=[], unattributed=[],
                  ledger_buy_quantity=0, ledger_sell_quantity=0, venue_buy_quantity=0, venue_sell_quantity=0)
    by_id = {line["intent_id"]: line for line in lines}
    for line in lines:
        for key in ("settled_shares", "settlement_payout", "settlement_fee", "remaining_shares"):
            if line[key] == 0:
                del line[key]
        line.update(venue_quantity=0, venue_cost=0, venue_fee=0)
    trades = _rows(db, """
        SELECT e.id,e.order_id,e.asset,e.side,e.game_id,e.quantity,e.price,e.notional,e.fee,
               e.time,e.confirmed,e.disposition,a.intent_id
        FROM external_fills e LEFT JOIN attempts a ON a.order_id=e.order_id
        LEFT JOIN intents i ON i.id=a.intent_id
        WHERE i.game_id=? OR (i.id IS NULL AND (e.game_id=? OR (e.game_id='' AND EXISTS
              (SELECT 1 FROM intents x WHERE x.game_id=? AND x.asset=e.asset))))
        ORDER BY e.time,e.order_id,e.id
    """, game, game, game)
    for trade in trades:
        line = by_id.get(trade.pop("intent_id"))
        for key in ("disposition", "game_id", "notional"):
            if not trade[key]:
                del trade[key]
        trade["confirmed"] = bool(trade["confirmed"])
        report["venue_trades"].append(trade)
        if line is None:
            report["unattributed"].append(trade)
            if not trade.get("disposition"):
                report["status"] = "PENDING"
        if not trade["confirmed"]:
            report["status"] = "PENDING"
            continue
        if line is not None:
            side = "BUY" if line["action"] == "OPEN" else "SELL"
            if (trade["asset"] != line["asset"] or trade["side"] != side
                    or trade.get("game_id", game) != game):
                report["status"] = "PENDING"
            line["venue_quantity"] += trade["quantity"]
            line["venue_cost"] += trade.get("notional", 0)
            line["venue_fee"] += trade["fee"]
        if trade.get("disposition") != "manual":
            report["venue_" + trade["side"].lower() + "_quantity"] += trade["quantity"]
    for line in lines:
        line["quantity_difference"] = line["venue_quantity"] - line["ledger_quantity"]
        if (line["state"] in ACTIVE or line["quantity_difference"] or
                line["venue_cost"] != line["ledger_cost"] or line["venue_fee"] != line["ledger_fee"]):
            report["status"] = "PENDING"
        side = "buy" if line["action"] == "OPEN" else "sell"
        report[f"ledger_{side}_quantity"] += line["ledger_quantity"]
    for side in ("buy", "sell"):
        report[f"{side}_difference"] = report[f"venue_{side}_quantity"] - report[f"ledger_{side}_quantity"]
    return report


class Reports:
    def __init__(self, ledger):
        self.ledger = ledger
        self.duplicates = 0
        self.conflicts = 0
        with ledger.tx() as db:
            execute_schema(db, SCHEMA)

    def cursor(self):
        with self.ledger.tx() as db:
            row = db.execute("SELECT cursor FROM feed_progress WHERE singleton=1").fetchone()
            return row[0] if row else ""

    def observe(self, fills, cursor):
        if not isinstance(cursor, str):
            raise Invalid("expected venue cursor string")
        with self.ledger.tx() as db:
            for fill in fills:
                _observe_fill(db, fill)
            db.execute("""INSERT INTO feed_progress VALUES(1,?)
                          ON CONFLICT(singleton) DO UPDATE SET cursor=excluded.cursor""", (cursor,))

    def summarize(self, game, finish=False):
        with self.ledger.tx() as db:
            now = _now()
            if finish:
                db.execute("INSERT OR IGNORE INTO completed_games VALUES(?,?)", (game, now))
            report = _game_summary(db, game)
            if report["finished"]:
                prior = db.execute("""SELECT revision,body FROM game_reports WHERE game_id=?
                                      ORDER BY revision DESC LIMIT 1""", (game,)).fetchone()
                revision = prior[0] if prior else 0
                # Earlier Go versions serialized struct fields in declaration order.
                if prior is None or json.loads(prior[1]) != report:
                    revision += 1
                    db.execute("INSERT INTO game_reports VALUES(?,?,?,?)", (game, revision, now, _json(report)))
                report["revision"] = revision
            db.execute("DELETE FROM dirty_reports WHERE game_id=?", (game,))
            return report

    def refresh(self):
        with self.ledger.tx() as db:
            games = [r[0] for r in db.execute("SELECT game_id FROM dirty_reports ORDER BY game_id")]
        for game in games:
            self.summarize(game)

    def alert_summary(self, now):
        with self.ledger.tx() as db:
            age, unassigned, differences = db.execute(f"""
                SELECT (SELECT coalesce(max(?-created_at),0) FROM attempts WHERE state IN ('SUBMITTING','UNKNOWN')),
                       ({UNASSIGNED_COUNT}),
                       (SELECT count(*) FROM game_reports r WHERE revision=(SELECT max(r2.revision)
                        FROM game_reports r2 WHERE r2.game_id=r.game_id) AND
                        (json_extract(body,'$.buy_difference')!=0 OR json_extract(body,'$.sell_difference')!=0))
            """, (now,)).fetchone()
        if age < 30000 and not unassigned and not differences:
            return ""
        level = "unknown for 5m" if age >= 300000 else "unknown for 30s" if age >= 30000 else ""
        return f"{level} unassigned_fills={unassigned} reports_with_difference={differences}".strip()

    def _metrics(self):
        now = _now()
        with self.ledger.tx() as db:
            unknown, age, warnings, review = db.execute("""
                SELECT count(*),coalesce(max(?-created_at),0),coalesce(sum(?-created_at>=30000),0),
                       coalesce(sum(?-created_at>=300000),0) FROM attempts WHERE state IN ('UNKNOWN','SUBMITTING')
            """, (now, now, now)).fetchone()
            delay = db.execute("""SELECT coalesce(max(e.at-a.created_at),0) FROM events e
                                  JOIN attempts a ON a.id=e.attempt_id
                                  WHERE e.state IN ('FILLED','PARTIAL','PARTIAL_FINAL')""").fetchone()[0]
            no_owner = db.execute(UNASSIGNED_COUNT).fetchone()[0]
        return dict(duplicate_requests=self.duplicates, conflicting_requests=self.conflicts,
                    unknown_attempts=unknown, oldest_unknown_ms=age, warnings_over_30s=warnings,
                    review_over_5m=review, unattributed_fills=no_owner, max_submit_to_fill_ms=delay)

    def _detail(self, intent_id):
        with self.ledger.tx() as db:
            intent = public_intent(read_intent(db, intent_id))
            intent["attempts"] = _rows(db, """
                SELECT id,number,coalesce(order_id,'') AS order_id,state,created_at,updated_at FROM attempts
                WHERE intent_id=? ORDER BY number
            """, intent_id)
            intent["timeline"] = _rows(db, EVENTS, intent_id)
            first = db.execute("SELECT min(time) FROM fills WHERE intent_id=?", (intent_id,)).fetchone()[0]
            if first:
                intent["first_fill_at"] = first
            if intent["state"] in ACTIVE:
                intent["next_check_estimate"] = _now() + 1000
            return intent

    def _decision(self, body):
        strings = {"game_id", "source_id", "channel", "version", "signal_id", "result", "reason"}
        _fields(body, strings | {"at", "inputs", "intent_id"})
        if not all(string(body, k) for k in strings) or not isinstance(body.get("inputs"), dict):
            raise Invalid("invalid decision")
        integer(body, "at", 1, INT64_MAX)
        decision = dict(body)
        if not string(decision, "intent_id"):
            decision.pop("intent_id", None)
        try:
            raw = _json(decision)
        except (TypeError, ValueError) as err:
            raise Invalid("invalid decision JSON") from err
        with self.ledger.tx() as db:
            key = (decision["game_id"], decision["source_id"])
            prior = db.execute("SELECT body FROM decisions WHERE game_id=? AND source_id=?", key).fetchone()
            if prior is None:
                db.execute("INSERT INTO decisions VALUES(?,?,?,?)", (*key, decision["at"], raw))
            elif _json(json.loads(prior[0])) != raw:
                raise Conflict("conflicting decision")
        return decision

    def _resolve(self, req):
        _fields(req, {"actor", "evidence", "operation", "order_id", "attempt_id", "fill_id"})
        if (not all(isinstance(v, str) for v in req.values())
                or not req.get("actor", "").strip() or not req.get("evidence", "").strip()):
            raise Invalid("review requires an actor and evidence")
        op = req.get("operation")
        order, attempt = req.get("order_id", ""), req.get("attempt_id", "")
        if not order and op != "zero":
            raise Invalid("review requires an exchange order ID")
        now = _now()
        with self.ledger.tx() as db:
            if op == "manual":
                result = db.execute("""UPDATE external_fills SET disposition='manual'
                                       WHERE order_id=? AND disposition='' AND NOT EXISTS
                                       (SELECT 1 FROM attempts WHERE order_id=?)""", (order, order))
                if not result.rowcount:
                    raise Conflict("order has no unassigned fills")
                before, after, subject = "unattributed", "manual", order
            elif op in ("bind", "import", "zero"):
                intent, active = current_attempt(db, attempt)
                before, subject = intent["state"], attempt
                if not active or (order and intent["order_id"] and intent["order_id"] != order):
                    raise Conflict("review disagrees with current attempt")
                if op == "bind":
                    if before not in ("UNKNOWN", "SUBMITTING", "ACCEPTED"):
                        raise Conflict("attempt is not available for binding")
                    if intent["order_id"] == order:
                        return {"operation": op}
                    side = "BUY" if intent["action"] == "OPEN" else "SELL"
                    evidence = db.execute("""
                        SELECT asset,side,game_id,disposition FROM external_fills WHERE order_id=?
                    """, (order,)).fetchall()
                    if not evidence or any(asset != intent["asset"] or observed_side != side or disposition
                                           or (game and game != intent["game_id"])
                                           for asset, observed_side, game, disposition in evidence):
                        raise Conflict("venue evidence does not identify this attempt")
                    intent["order_id"] = order
                    after = before + " / " + order
                elif op == "import":
                    if intent["order_id"] != order or not req.get("fill_id"):
                        raise Conflict("fill must identify the bound order")
                    row = db.execute("""SELECT * FROM external_fills WHERE order_id=? AND id=?
                                        AND confirmed=1 AND disposition=''""", (order, req["fill_id"])).fetchone()
                    if row is None:
                        raise KeyError("confirmed fill not found")
                    intent["last_check"] = now
                    fill = dict(row)
                    fill["confirmed"] = True
                    record_fill(db, intent, fill)
                    after = intent["state"] = "PARTIAL"
                else:
                    matched = db.execute("SELECT matched_high_water FROM attempts WHERE id=?", (attempt,)).fetchone()[0]
                    if before != "UNKNOWN" or intent["filled"] or matched:
                        raise Conflict("zero contradicts known order state")
                    if order:
                        intent["order_id"] = order
                    evidence = db.execute(f"""SELECT 1 FROM external_fills f WHERE f.order_id=? OR
                        (f.asset=? AND f.disposition='' AND {NO_OWNER}) LIMIT 1
                    """, (intent["order_id"], intent["asset"])).fetchone()
                    if evidence:
                        raise Conflict("zero contradicts venue evidence")
                    after = intent["state"] = "REJECTED_ZERO"
                    intent.update(reserved=0, last_check=now)
                try:
                    attempt_state(db, intent, intent["state"], now)
                except sqlite3.IntegrityError as err:
                    raise Conflict("order ID already belongs to another attempt") from err
                update_intent(db, intent)
                if op == "zero":
                    append_event(db, intent, now, "operator-confirmed zero: " + req["evidence"])
            else:
                raise Invalid("unknown review operation")
            db.execute("""INSERT INTO review_actions(at,actor,evidence,operation,subject,before_state,after_state)
                          VALUES(?,?,?,?,?,?,?)""", (now, req["actor"], req["evidence"], op, subject, before, after))
        return {"operation": op}

    def _trace(self, order):
        with self.ledger.tx() as db:
            row = db.execute("SELECT intent_id FROM attempts WHERE order_id=?", (order,)).fetchone()
            fills = _rows(db, """SELECT id,order_id,side,asset,time,quantity,price,notional,fee,confirmed,disposition
                                 FROM external_fills WHERE order_id=? ORDER BY time,id""", order)
            if not row and not fills:
                raise KeyError("order not found")
            intent = row[0] if row else ""
            return dict(order_id=order, intent_id=intent, events=_rows(db, EVENTS, intent), venue_trades=fills)

    def dispatch(self, method, parts, body=None):
        """Return a compatibility API response. The HTTP layer translates ledger exceptions."""
        if not parts or parts[0] != "v1":
            return 404, {"error": "not found"}
        route = parts[1:]
        if method == "GET":
            if route == ["account"]:
                return 200, dict(account=self.ledger.account, environment=self.ledger.environment,
                                 available_cash=self.ledger.available())
            if route == ["metrics"]:
                return 200, self._metrics()
            if len(route) == 2 and route[0] == "order-intents":
                return 200, self._detail(route[1])
            if len(route) == 3 and route[0] == "orders" and route[2] == "trace":
                return 200, self._trace(route[1])
            if route == ["games"]:
                with self.ledger.tx() as db:
                    games = db.execute("""SELECT game_id FROM intents UNION SELECT game_id FROM decisions
                        UNION SELECT game_id FROM completed_games UNION SELECT game_id FROM external_fills
                        WHERE game_id!='' ORDER BY game_id""")
                    return 200, [row[0] for row in games]
            if route[:1] == ["games"] and len(route) >= 3:
                game = route[1]
                if route[2:] == ["report"]:
                    return 200, self.summarize(game)
                with self.ledger.tx() as db:
                    if route[2:] == ["decisions"]:
                        rows = db.execute("SELECT body FROM decisions WHERE game_id=? ORDER BY at,source_id", (game,))
                        return 200, [json.loads(r[0]) for r in rows]
                    if route[2:] == ["report", "revisions"]:
                        rows = db.execute("""SELECT revision,at,body FROM game_reports
                                             WHERE game_id=? ORDER BY revision""", (game,))
                        return 200, [dict(revision=r[0], at=r[1], report=json.loads(r[2])) for r in rows]
            if route == ["review", "fills"]:
                with self.ledger.tx() as db:
                    return 200, _rows(db, f"""SELECT order_id,id,asset,side,quantity,price,fee,time,confirmed,
                        game_id,disposition FROM external_fills f WHERE {NO_OWNER} ORDER BY time,order_id,id""")
            if route == ["review", "actions"]:
                with self.ledger.tx() as db:
                    return 200, _rows(db, """SELECT at,actor,evidence,operation,subject,before_state AS before,
                        after_state AS after FROM review_actions ORDER BY id""")
        if method == "POST":
            if route == ["decisions"]:
                return 200, self._decision(body)
            if route == ["review", "resolve"]:
                return 200, self._resolve(body)
            if len(route) == 3 and route[0] == "games" and route[2] == "finish":
                return 200, self.summarize(route[1], True)
            if route in (["account", "pause-open"], ["account", "resume-open"]):
                paused = route[1] == "pause-open"
                with self.ledger.tx() as db:
                    db.execute("UPDATE execution_control SET paused=? WHERE singleton=1", (paused,))
                return 200, {"open_paused": paused}
        return 404, {"error": "not found"}
