"""Versioned dashboard read models and resumable, per-environment event streams."""

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


UTC = timezone.utc
HK = timezone(timedelta(hours=8), "Asia/Hong_Kong")


def stamp(value):
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def instant(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.astimezone(UTC)


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class ResetRequired(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


SCHEMA = """
CREATE TABLE IF NOT EXISTS scopes(scope TEXT PRIMARY KEY,generation TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS revisions(
 scope TEXT NOT NULL,seq INTEGER NOT NULL,at TEXT NOT NULL,event TEXT NOT NULL,
 PRIMARY KEY(scope,seq));
CREATE INDEX IF NOT EXISTS revision_time ON revisions(scope,at);
CREATE TABLE IF NOT EXISTS objects(
 scope TEXT NOT NULL,kind TEXT NOT NULL,key TEXT NOT NULL,seq INTEGER NOT NULL,value TEXT,
 PRIMARY KEY(scope,kind,key,seq));
CREATE TABLE IF NOT EXISTS quotes(
 scope TEXT NOT NULL,asset TEXT NOT NULL,at TEXT NOT NULL,seq INTEGER NOT NULL,value TEXT NOT NULL,
 PRIMARY KEY(scope,asset,at,seq));
"""


def changed(old, new):
    return [k for k, v in new.items() if v != old.get(k)], sorted(old.keys() - new.keys())


def unpack(header, positions, activity, timeline):
    snap = dict(header["snapshot"])
    snap["open_positions"] = [positions[k] for k in header["open"]]
    snap["today_activity"] = [activity[k] for k in header["today"]]
    return {
        "snapshot": snap,
        "positions": list(positions.values()),
        "activity": list(activity.values()),
        "timeline": timeline,
        "origin": header["origin"],
    }


class VersionStore:
    def __init__(self, path, scope, env):
        self.env, self.scope = env, scope
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.condition = threading.Condition(threading.RLock())
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO scopes VALUES(?,?)", (scope, uuid.uuid4().hex))
        self.generation = self.db.execute(
            "SELECT generation FROM scopes WHERE scope=?", (scope,)
        ).fetchone()[0]
        row = self.db.execute(
            "SELECT seq,at FROM revisions WHERE scope=? ORDER BY seq DESC LIMIT 1", (scope,)
        ).fetchone()
        self.seq, self.at = row if row else (0, None)
        self.objects = self._objects(self.seq)
        self.head = self._view(self.objects) if self.seq else None
        self.last_clean = None

    def _objects(self, seq):
        result = {kind: {} for kind in ("head", "position", "activity", "timeline")}
        rows = self.db.execute(
            """
            SELECT kind,key,value,max(seq) FROM objects WHERE scope=? AND seq<=?
            GROUP BY kind,key""",
            (self.scope, seq),
        )
        for kind, key, value, _ in rows:
            if value is not None:
                result[kind][key] = json.loads(value)
        return result

    @staticmethod
    def _view(objects):
        return unpack(
            objects["head"]["data"], objects["position"], objects["activity"], objects["timeline"]
        )

    def version(self, seq=None):
        return f"{self.generation}.{self.seq if seq is None else seq}"

    def parse(self, version):
        gen, sep, number = version.partition(".")
        if not sep or not number.isascii() or not number.isdigit() or len(number) > 18:
            raise ValueError("invalid version")
        if gen != self.generation:
            raise ResetRequired("generation_changed")
        seq = int(number)
        if seq > self.seq:
            raise ResetRequired("ahead")
        return seq

    def read(self, version=None):
        with self.condition:
            if not self.seq:
                raise RuntimeError("数据初始化中")
            seq = self.parse(version) if version is not None else self.seq
            if seq == self.seq:
                return self.version(), self.at, self.head
            row = self.db.execute(
                "SELECT at FROM revisions WHERE scope=? AND seq=?", (self.scope, seq)
            ).fetchone()
            if row is None:
                raise ResetRequired("expired")
            return self.version(seq), row[0], self._view(self._objects(seq))

    def publish(self, view, origin, quote_updates, now):
        snapshot = dict(view["snapshot"])
        positions = {p["id"]: p for p in view["positions"]}
        activity = {a["id"]: a for a in view["activity"]}
        opens = {p["id"]: p for p in snapshot.pop("open_positions")}
        today = snapshot.pop("today_activity")
        header = {
            "snapshot": snapshot,
            "open": list(opens),
            "today": [a["id"] for a in today],
            "origin": origin,
        }
        objects = {
            "head": {"data": header},
            "position": positions,
            "activity": activity,
            "timeline": view["timeline"],
        }
        with self.condition:
            previous = self.objects
            delta = {kind: changed(previous[kind], current) for kind, current in objects.items()}
            if not any(up or rm for up, rm in delta.values()) and not quote_updates:
                return self.version()
            seq = self.seq + 1
            old_header = previous["head"].get("data", {})
            old_snap = old_header.get("snapshot", {})
            patch = {k: v for k, v in snapshot.items() if old_snap.get(k) != v}
            p_up, p_rm = changed(
                {k: previous["position"][k] for k in old_header.get("open", [])}, opens
            )
            a_up, a_rm = delta["activity"]
            if p_up:
                patch["positions_upsert"] = [opens[k] for k in p_up]
            if p_rm:
                patch["positions_remove"] = p_rm
            if a_up:
                patch["activity_upsert"] = [activity[k] for k in a_up]
            if a_rm:
                patch["activity_remove"] = a_rm
            dirty = sorted(
                set(sum((delta[k][0] + delta[k][1] for k in ("position", "timeline")), []))
            )
            if dirty:
                patch["detail_dirty"] = dirty
            timestamps = [
                instant(a["time"])
                for k in a_up + a_rm
                for a in (activity.get(k), previous["activity"].get(k))
                if a
            ]
            if timestamps or "summary" in patch:
                patch["history_dirty_from"] = stamp(min([now] + timestamps))
            cause = "source"
            if a_up or a_rm:
                if a_rm or any(k in previous["activity"] for k in a_up):
                    cause = "correction"
                elif any(activity[k]["kind"] == "settlement" for k in a_up):
                    cause = "settlement"
                else:
                    cause = "trade"
            elif "business_date" in patch:
                cause = "clock"
            elif "live_games" in patch and not quote_updates:
                cause = "schedule"
            elif quote_updates or "summary" in patch:
                cause = "quote"
            elif dirty:
                cause = "order"
            event = {
                "env": self.env,
                "version": self.version(seq),
                "prev_version": self.version(),
                "published_at": stamp(now),
                "cause": cause,
                "patch": patch,
            }
            with self.db:
                for kind, (up, rm) in delta.items():
                    self.db.executemany(
                        "INSERT INTO objects VALUES(?,?,?,?,?)",
                        (
                            (
                                self.scope,
                                kind,
                                k,
                                seq,
                                encode(objects[kind][k]) if k in objects[kind] else None,
                            )
                            for k in up + rm
                        ),
                    )
                self.db.executemany(
                    "INSERT OR IGNORE INTO quotes VALUES(?,?,?,?,?)",
                    (
                        (self.scope, asset, stamp(instant(q["observed_at"])), seq, encode(q))
                        for asset, q in quote_updates.items()
                    ),
                )
                self.db.execute(
                    "INSERT INTO revisions VALUES(?,?,?,?)",
                    (self.scope, seq, stamp(now), encode(event)),
                )
                if self.last_clean is None or now - self.last_clean > timedelta(hours=1):
                    self._prune(now)
                    self.last_clean = now
            self.seq, self.at, self.objects = seq, stamp(now), objects
            self.head = dict(view, origin=origin)
            self.condition.notify_all()
            return event["version"]

    def _prune(self, now):
        last = self.db.execute(
            "SELECT max(seq) FROM revisions WHERE scope=? AND at<?",
            (self.scope, stamp(now - timedelta(days=1))),
        ).fetchone()[0]
        if last is None:
            return
        # Keep the boundary version and its state so clients can replay from it.
        self.db.execute(
            """DELETE FROM objects AS o WHERE scope=? AND seq<? AND EXISTS (
          SELECT 1 FROM objects n WHERE n.scope=o.scope AND n.kind=o.kind AND n.key=o.key
          AND n.seq>o.seq AND n.seq<=?)""",
            (self.scope, last, last),
        )
        self.db.execute("DELETE FROM revisions WHERE scope=? AND seq<?", (self.scope, last))

    def changes(self, after, limit=100, wait=0):
        with self.condition:
            seq = self.parse(after)
            if seq == self.seq and wait:
                self.condition.wait_for(lambda: self.seq != seq, timeout=wait)
            rows = self.db.execute(
                "SELECT seq,event FROM revisions WHERE scope=? AND seq>? ORDER BY seq LIMIT ?",
                (self.scope, seq, limit),
            ).fetchall()
            if rows and rows[0][0] != seq + 1:
                raise ResetRequired("expired")
            events = [json.loads(row[1]) for row in rows]
            end = rows[-1][0] if rows else seq
            return (
                self.version(),
                self.at,
                {"items": events, "next_after": self.version(end), "has_more": end < self.seq},
            )

    def quotes_at(self, when, seq=None):
        # All older quotes are invalid by design; an empty result yields a gap.
        with self.condition:
            rows = self.db.execute(
                """SELECT asset,value FROM quotes WHERE scope=? AND at>=? AND at<=?
                AND seq<=? ORDER BY at,seq""",
                (
                    self.scope,
                    stamp(when - timedelta(seconds=15)),
                    stamp(when),
                    self.seq if seq is None else seq,
                ),
            )
            return {asset: json.loads(value) for asset, value in rows}

    def close(self):
        with self.condition:
            self.db.close()
