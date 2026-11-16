"""Read-only adapters for the executor ledger and live_runner session records."""

import hashlib
import json
import re
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path

from executor_ledger import cash_committed

UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
SCALE = Decimal(1_000_000)
IDENTITY = ("game_id", "game_label", "channel", "asset_id", "market", "outcome")


def utc(value, milliseconds=False):
    if value is None:
        return None
    n = Decimal(str(value))
    tick = n * (1000 if milliseconds else 1_000_000)
    return (
        (EPOCH + timedelta(microseconds=int(tick)))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def number(value):
    if value is None or value == "":
        return None
    n = Decimal(str(value))
    if not n.is_finite() or abs(n.adjusted()) > 100:
        raise ValueError("invalid quantity")
    s = format(n, "f")
    return (s.rstrip("0").rstrip(".") if "." in s else s) if n else "0"


def safe_journal_time(value):
    if not isinstance(value, str):
        return None
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (
        stamp.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        if stamp.tzinfo
        else None
    )


def maybe_number(value):
    try:
        return number(value)
    except (ArithmeticError, ValueError):
        return None


def micros(value):
    return number(Decimal(value) / SCALE) if value is not None else None


def key(prefix, *parts):
    payload = json.dumps(parts, ensure_ascii=True, separators=(",", ":"), default=str)
    return prefix + "-" + hashlib.sha256(payload.encode()).hexdigest()[:32]


def same_account(a, b):
    if isinstance(a, str) and isinstance(b, str) and re.fullmatch(r"0x[0-9a-fA-F]{40}", a):
        return a.lower() == b.lower()
    return a == b


def safe_id(value):
    if value is None:
        return None
    s = str(value)
    return s if re.fullmatch(r"[\w.:#-]{1,180}", s) else None


def label(value):
    if isinstance(value, str) and re.fullmatch(r"[\w '().:/&+-]{1,160}", value) and "://" not in value:
        return value
    return None


def identity(game=None, channel=None, asset=None, market=None, outcome=None, game_label=None):
    return dict(
        game_id=safe_id(game),
        game_label=label(game_label),
        channel=safe_id(channel),
        asset_id=safe_id(asset),
        market=label(market),
        outcome=label(outcome),
    )


def meta(ident, reserved=None):
    return dict(ident, reserved_quantity=reserved, pending_settlement=False, issues=[])


def issue(code, pid=None, aid=None):
    return dict(code=code, position_id=pid, activity_id=aid)


def line(id_, type_, time, time_kind, **values):
    return dict(
        dict(
            id=id_,
            type=type_,
            time=time,
            time_kind=time_kind,
            state=None,
            reason=None,
            order_id=None,
            attempt_id=None,
            limit_price=None,
            activity=None,
            evidence_ref=None,
        ),
        **values,
    )


def blank():
    return dict(
        activity=[],
        timeline=defaultdict(list),
        meta={},
        sources=[],
        available_cash=None,
        coverage_from=None,
        known_origin=False,
    )


def fill(
    ident,
    aid,
    pid,
    kind,
    source,
    evidence,
    qty,
    price,
    gross,
    fee,
    at,
    time_kind,
    order_id=None,
    fill_id=None,
    recorded_at=None,
    fee_unit="cash",
):
    reasons = []
    if pid is None:
        reasons.append(issue("unattributed", None, aid))
    if fee is None:
        reasons.append(issue("missing_fee", pid, aid))
    net = None
    if gross is not None and fee is not None:
        n = Decimal(gross)
        net = number((-n if kind == "buy" else n) - (Decimal(fee) if fee_unit == "cash" else 0))
    return dict(
        ident,
        id=aid,
        position_id=pid,
        kind=kind,
        evidence=evidence,
        source=source,
        order_id=safe_id(order_id),
        fill_id=safe_id(fill_id),
        quantity=qty,
        price=price,
        gross=gross,
        fee=fee,
        fee_unit=fee_unit if fee is not None else None,
        net_cash=net,
        time=at,
        time_kind=time_kind,
        recorded_at=recorded_at or at,
        revised_at=None,
        issues=reasons,
    )


def with_sports_fee(row):
    """Derive the cash-equivalent sports fee once, after overlapping evidence is resolved."""
    if (row["fee"] is not None or row["kind"] not in ("buy", "sell")
            or row["evidence"] == "inferred" or not row["game_id"]
            or row["price"] is None or row["quantity"] is None):
        return row
    price, qty = Decimal(row["price"]), Decimal(row["quantity"])
    if not 0 < price < 1 or qty <= 0:
        return row
    maker = row.get("liquidity_role") == "maker"
    with localcontext() as ctx:
        ctx.prec = max(40, qty.adjusted() + 10, len(qty.as_tuple().digits) + 2 * len(price.as_tuple().digits) + 8)
        fee = Decimal(0) if maker else (qty * Decimal("0.05") * price * (1 - price)).quantize(
            Decimal("0.00001"), rounding=ROUND_HALF_UP
        )
        gross = Decimal(row["gross"]) if row["gross"] is not None else None
        net = number((-gross if row["kind"] == "buy" else gross) - fee) if gross is not None else None
    return dict(row, fee=number(fee), fee_unit="cash", fee_basis="calculated_maker" if maker else "estimated_taker",
                net_cash=net, issues=[i for i in row["issues"] if i["code"] != "missing_fee"])


def rows(db, query, *args):
    return [dict(row) for row in db.execute(query, args)]


class WrongScope(ValueError):
    pass


class ReadDatabase:
    """PRAGMA data_version tracks all commits, including in-place external-fill edits."""

    def __init__(self, path):
        self.path = Path(path) if path else None
        self.db = self.file_id = self.version = self.result = self.scope = None

    def close(self):
        if self.db:
            self.db.close()
        self.db = self.file_id = self.version = None

    def read(self, fn, scope=None):
        if scope != self.scope:
            self.result = self.version = None
            self.scope = scope
        stat = self.path.stat()
        file_id = stat.st_dev, stat.st_ino
        if file_id != self.file_id:
            self.close()
            self.result = None
            self.db = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=0.25,
                isolation_level=None,
                check_same_thread=False,
            )
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA query_only=ON")
            self.file_id = file_id
        version = self.db.execute("PRAGMA data_version").fetchone()[0]
        if version != self.version:
            self.db.execute("BEGIN")
            try:
                result = fn(self.db)
            finally:
                self.db.execute("ROLLBACK")
            self.result, self.version = result, version
        return self.result


def executor(db, env, account):
    header = db.execute(
        "SELECT account,environment,starting_capital FROM ledger_meta WHERE singleton=1"
    ).fetchone()
    if (
        not header
        or header["environment"] != env
        or (account and not same_account(account, header["account"]))
    ):
        raise WrongScope
    if db.execute(
        "SELECT 1 FROM intents WHERE account!=? OR environment!=? LIMIT 1", (header["account"], env)
    ).fetchone():
        raise WrongScope
    snap = blank()
    intents = {r["id"]: r for r in rows(db, "SELECT * FROM intents")}
    attempts = {
        r["id"]: r
        for r in rows(db, "SELECT id,intent_id,order_id,state,created_at,limit_price FROM attempts")
    }
    order_intents = {r["order_id"]: intents[r["intent_id"]] for r in attempts.values() if r["order_id"]}
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    completed = (
        {r[0] for r in db.execute("SELECT game_id FROM completed_games")}
        if "completed_games" in tables
        else set()
    )
    owners = {}
    for r in intents.values():
        pid = safe_id(r["id"] if r["action"] == "OPEN" else r["position_id"])
        if not pid:
            raise ValueError("invalid position id")
        owners[r["id"]] = pid
        ident = identity(r["game_id"], r["channel"], r["asset"], r["market"])
        info = snap["meta"].setdefault(pid, meta(ident, "0"))
        if r["action"] == "CLOSE":
            info["reserved_quantity"] = micros(r["reserved"])
        info["pending_settlement"] = r["game_id"] in completed
        snap["timeline"][pid].append(
            line(
                key("signal", r["id"]),
                "signal",
                utc(r["signal_at"], True),
                "signal",
                state=r["action"],
                limit_price=micros(r["limit_price"]),
            )
        )
    seen_at = {}
    for e in rows(db, "SELECT seq,intent_id,attempt_id,at,state,reason FROM events ORDER BY seq"):
        intent = intents[e["intent_id"]]
        pid = owners[e["intent_id"]]
        at = utc(e["at"], True)
        attempt = attempts.get(e["attempt_id"], {})
        if e["reason"].startswith("confirmed fill "):
            seen_at[e["intent_id"], e["reason"][len("confirmed fill ") :]] = at
            continue
        if e["reason"].startswith("verified settlement "):
            continue
        snap["timeline"][pid].append(
            line(
                key("order", e["seq"]),
                "order",
                at,
                "recorded",
                state=safe_id(e["state"]),
                attempt_id=safe_id(e["attempt_id"]),
                order_id=safe_id(attempt.get("order_id")),
                limit_price=micros(attempt.get("limit_price", intent["limit_price"])),
            )
        )
    manual = []
    confirmed = {}
    t_kind = "simulated" if env == "paper" else "chain_confirmed"

    def append_row(r, intent, pid):
        asset, side = r["asset"], r["side"]
        if intent and (
            asset != intent["asset"]
            or side != ("BUY" if intent["action"] == "OPEN" else "SELL")
            or r.get("game_id") not in (None, "", intent["game_id"])
        ):
            pid = None
        ident = (
            {k: snap["meta"][pid][k] for k in IDENTITY}
            if pid
            else identity(r.get("game_id"), asset=asset)
        )
        aid = key("trade", r["order_id"], r["id"])
        a = fill(
            ident,
            aid,
            pid,
            side.lower(),
            "executor",
            "confirmed",
            micros(r["quantity"]),
            micros(r["price"]),
            micros(r["cost"]),
            micros(r["fee"]),
            utc(r["time"], True),
            t_kind,
            r["order_id"],
            r["id"],
            seen_at.get(((intent or {}).get("id"), r["id"])),
        )
        confirmed[r["order_id"], r["id"]] = a

    for r in rows(db, "SELECT * FROM fills ORDER BY rowid"):
        append_row(r, intents[r["intent_id"]], owners[r["intent_id"]])
    if "external_fills" in tables:
        for r in rows(
            db,
            """SELECT id,order_id,asset,side,quantity,price,notional AS cost,
                             fee,time,game_id,disposition FROM external_fills WHERE confirmed=1""",
        ):
            if r["disposition"] == "manual":
                manual.append(
                    dict(
                        order_id=r["order_id"],
                        game_id=r["game_id"],
                        channel=None,
                        asset_id=r["asset"],
                        kind=r["side"].lower(),
                    )
                )
                confirmed.pop((r["order_id"], r["id"]), None)
                continue
            if (r["order_id"], r["id"]) in confirmed:
                continue
            intent = order_intents.get(r["order_id"])
            append_row(r, intent, owners[intent["id"]] if intent else None)
    snap["activity"].extend(confirmed.values())
    for r in rows(db, "SELECT position_id,venue_event,leg_id,shares,payout,fee,at FROM settlements"):
        pid = owners[r["position_id"]]
        ident = {k: snap["meta"][pid][k] for k in IDENTITY}
        at = utc(r["at"], True)
        a = fill(
            ident,
            key("settlement", pid, r["venue_event"], r["leg_id"]),
            pid,
            "settlement",
            "executor",
            "confirmed",
            micros(r["shares"]),
            None,
            micros(r["payout"]),
            micros(r["fee"]),
            at,
            "simulated" if env == "paper" else "settlement_booked",
        )
        snap["activity"].append(a)
        snap["timeline"][pid].append(
            line(a["id"], "settlement", at, a["time_kind"], activity=a, evidence_ref=safe_id(r["leg_id"]))
        )
        snap["meta"][pid]["pending_settlement"] = False
    snap["available_cash"] = micros(header["starting_capital"] - cash_committed(db))
    # Opening intents create project inventory in this single-owner ledger.
    if intents:
        snap["coverage_from"] = utc(min(i["signal_at"] for i in intents.values()), True)
    snap["known_origin"] = True
    return snap, manual, header["account"]


EVENT_FIELDS = {
    "start": (),
    "heartbeat": (),
    "stopped": (),
    "resolve": ("slug", "tokens", "outcomes"),
    "buy": ("filled_shares", "entry", "usd", "fills", "fee", "fee_unit", "liquidity_role"),
    "sell": ("sold_shares", "exit", "fills", "fee", "fee_unit", "liquidity_role"),
    "buy_posted": ("limit",),
    "buy_gave_up": (),
    "sell_gave_up": (),
    "buy_unknown": (),
    "signal": (),
    "exit": (),
    "resume_position": (),
    "settle_hold": (),
    "no_exit": (),
    "reconcile_closed": ("held",),
    "reconcile_add": ("held", "extra", "accounted"),
}
COMMON = (
    "ev",
    "t",
    "live",
    "account",
    "gid",
    "ch",
    "outcome",
    "order_id",
    "attempt_id",
    "position_id",
    "asset_id",
)


class RunnerFiles:
    def __init__(self, path, market_journal=None):
        self.path = Path(path) if path else None
        self.journal = Path(market_journal) if market_journal else None
        if (
            not self.journal
            and self.path
            and self.path.name == "live_trade_final"
            and self.path.parent.name == "data"
        ):
            self.journal = self.path.resolve().parents[1] / "var" / "market_data"
        self.files = {}
        self.result = None
        self.scope = None

    def read(self, env, account, allow_untagged):
        scope = env, account, allow_untagged
        if self.scope != scope:
            self.result = None
        self.scope = scope
        if not self.path.exists():
            raise FileNotFoundError
        if self.path.is_file():
            files = [self.path]
            if self.path.name == "events.jsonl":
                files.extend(self.path.parent.glob("events.jsonl.[0-9]*"))
        else:
            files = sorted(
                p
                for p in self.path.rglob("events.jsonl*")
                if p.name == "events.jsonl" or re.fullmatch(r"events\.jsonl\.[0-9][0-9.-]*", p.name)
            )
        mapping_files = (
            set(self.journal.rglob("events-*.jsonl")) if self.journal and self.journal.is_dir() else set()
        )
        files.extend(sorted(mapping_files))
        changed = self.result is None or set(files) != set(self.files)
        for p in files:
            stat = p.stat()
            file_id = stat.st_dev, stat.st_ino
            prior = self.files.get(p)
            if prior and (file_id, stat.st_size, stat.st_mtime_ns) == prior[:3]:
                continue
            changed = True
            # Start over on replacement, truncation, or non-append rewrites.
            offset, records, errors = (
                prior[3:] if prior and file_id == prior[0] and stat.st_size > prior[1] else (0, [], 0)
            )
            with p.open("rb") as f:
                f.seek(offset)
                while True:
                    raw = f.readline()
                    if not raw.endswith(b"\n"):
                        break
                    offset = f.tell()
                    try:
                        # The high-volume public feed journal is only a market identity source.
                        if p in mapping_files and b'"mapping"' not in raw:
                            continue
                        r = json.loads(raw, parse_float=Decimal)
                        ev = r.get("ev")
                        if p in mapping_files:
                            if ev != "mapping":
                                continue
                            ev = "resolve"
                            r["ev"] = ev
                            r["t"] = None
                        if ev in EVENT_FIELDS:
                            record = {k: r[k] for k in COMMON + EVENT_FIELDS[ev] if k in r}
                            record["time"] = (
                                safe_journal_time(r.get("recorded_utc"))
                                if p in mapping_files
                                else utc(r["t"])
                            )
                            if not record["time"]:
                                raise ValueError("missing time")
                            records.append(record)
                    except (
                        ValueError,
                        AttributeError,
                        KeyError,
                        TypeError,
                        ArithmeticError,
                        OverflowError,
                    ):
                        errors += 1
            self.files[p] = file_id, stat.st_size, stat.st_mtime_ns, offset, records, errors
        for stale in set(self.files) - set(files):
            del self.files[stale]
        if changed:
            self.result = self.project(env, account, allow_untagged)
        return self.result

    def project(self, env, account, allow_untagged):
        public_market = defaultdict(dict)
        scoped = []
        declared = {
            r["account"]
            for data in self.files.values()
            for r in data[4]
            if isinstance(r.get("account"), str) and r["account"]
        }
        ambiguous = not account and (len(declared) > 1 or not allow_untagged)
        errors = any(data[5] for data in self.files.values())
        last_heartbeat = None
        sessions = defaultdict(list)
        for path, data in self.files.items():
            sessions[path.parent].extend(data[4])
        for path, records in sessions.items():
            is_live = owner = None
            for r in sorted(records, key=lambda row: (row["time"], row["ev"] != "start")):
                ev = r["ev"]
                game = safe_id(r.get("gid"))
                if ev == "start":
                    is_live = r.get("live")
                    owner = r.get("account")
                if ev == "resolve" and game:
                    market = public_market[game]
                    slug = safe_id(r.get("slug"))
                    if slug:
                        market["slug"] = slug
                    teams, assets = r.get("outcomes"), r.get("tokens")
                    if (
                        isinstance(teams, list)
                        and isinstance(assets, list)
                        and len(teams) == len(assets) == 2
                    ):
                        market["teams"] = [label(x) for x in teams]
                        market["assets"] = {label(x): safe_id(y) for x, y in zip(teams, assets)}
                live = r.get("live", is_live)
                if type(live) is not bool:
                    if ev in ("buy", "sell"):
                        errors = True
                    continue
                record_owner = r.get("account", owner)
                if ambiguous or (not record_owner and (not allow_untagged or len(declared) > 1)):
                    continue
                if live != (env == "live") or (
                    account and record_owner and not same_account(account, record_owner)
                ):
                    continue
                if ev == "heartbeat":
                    last_heartbeat = max(last_heartbeat or r["time"], r["time"])
                if ev not in ("resolve", "start", "heartbeat", "stopped"):
                    scoped.append((r["time"], path, r))
        scoped.sort(key=lambda row: row[0])
        return scoped, dict(public_market), bool(errors or ambiguous), last_heartbeat


def runner_ident(game, channel, outcome, asset, markets):
    market = markets.get(game, {})
    teams = market.get("teams") or []
    slug = market.get("slug", "")
    matchup = " vs ".join(teams) if len(teams) == 2 and all(teams) else None
    if not matchup and (m := re.fullmatch(r"nba-([a-z0-9]+)-([a-z0-9]+)-\d{4}-\d{2}-\d{2}", slug)):
        matchup = m[1].upper() + " vs " + m[2].upper()
    return identity(
        game, channel, asset or market.get("assets", {}).get(outcome), "胜负", outcome, matchup
    )


def attempt_rows(db, account):
    owners = [r[0] for r in db.execute("SELECT DISTINCT account FROM attempts")]
    if not account and len(owners) > 1:
        raise WrongScope
    matched = [o for o in owners if same_account(o, account)]
    if account and owners and len(matched) != 1:
        raise WrongScope
    cols = "game_id,channel,action,attempt_id,position_id,order_id,state,updated_at,signal_at"
    return (
        rows(db, f"SELECT {cols} FROM attempts WHERE account=?", next(iter(matched or owners), "")),
        len(owners) <= 1,
    )


def build_runner(scoped, markets, attempts, env):
    snap = blank()
    # Runner enforces one OPEN per game/channel. An explicit durable ID wins;
    # ambiguous historical positions must not be assigned by their asset alone.
    candidates = defaultdict(set)
    for _, _, r in scoped:
        if r["ev"] in ("buy", "resume_position") and r.get("position_id"):
            candidates[r.get("gid"), r.get("ch")].add(safe_id(r["position_id"]))
    attempt_orders = {}
    for r in attempts:
        if r["action"] == "OPEN" and r["order_id"]:
            attempt_orders[r["attempt_id"]] = r["order_id"]
        if r["action"] == "OPEN":
            pid = safe_id(r["position_id"] or r["attempt_id"])
            candidates[r["game_id"], r["channel"]].add(pid)

    def resolve_pid(g, ch, explicit=None):
        if explicit:
            return safe_id(explicit)
        opts = candidates[g, ch]
        if len(opts) == 1:
            return next(iter(opts))
        return key("runner-position", g, ch) if g and ch and not opts else None

    session_pids, posted_orders, opening_signals = {}, {}, {}
    dedup = {}
    for at, path, r in scoped:
        game, channel = safe_id(r.get("gid")), safe_id(r.get("ch"))
        ev, outcome = r["ev"], label(r.get("outcome"))
        local = (path, game, channel)
        pid = resolve_pid(game, channel, r.get("position_id") or session_pids.get(local))
        if ev in ("resume_position", "buy") and pid:
            session_pids[local] = pid
        ident = runner_ident(game, channel, outcome, r.get("asset_id"), markets)
        if pid:
            info = snap["meta"].setdefault(pid, meta(ident))
            info.update({k: v for k, v in ident.items() if v})
        order = safe_id(r.get("order_id"))
        if ev == "signal":
            opening_signals[local] = outcome
        elif ev == "resume_position":
            opening_signals.pop(local, None)
        if ev == "buy_posted":
            posted_orders[local] = order
        if ev == "buy":
            order = order or posted_orders.get(local) or safe_id(attempt_orders.get(r.get("attempt_id")))
        if ev in ("buy", "sell"):
            is_buy = ev == "buy"
            aid = key("runner-order", ev, order) if order else key("runner-trade", game, channel, ev, at)
            try:
                qty = number(r.get("filled_shares" if is_buy else "sold_shares"))
                price = number(r.get("entry" if is_buy else "exit"))
                gross = number(r.get("usd")) if is_buy else None
                if gross is None and qty is not None and price is not None:
                    gross = number(Decimal(qty) * Decimal(price))
            except (ValueError, InvalidOperation):
                qty = price = gross = None
            inferred = r.get("fills") == 0 or not qty or Decimal(qty) <= 0
            a = fill(
                ident,
                aid,
                pid,
                ev,
                "runner",
                "inferred" if inferred else "aggregate",
                qty,
                price,
                gross,
                maybe_number(r.get("fee")) if r.get("fee_unit") in ("shares", "cash") else None,
                at,
                "recorded" if env == "live" else "simulated",
                order,
                fee_unit=r.get("fee_unit") or "cash",
            )
            role = str(r.get("liquidity_role", "")).lower()
            if role in ("maker", "taker"):
                a["liquidity_role"] = role
            if is_buy and not inferred and pid and ((r.get("attempt_id") == pid and r.get("position_id") == pid)
                                                    or (outcome and opening_signals.pop(local, None) == outcome)):
                a["opens_position"] = True
            if inferred:
                a["issues"].append(issue("source_gap", pid, aid))
            if aid in dedup:
                prior = dedup[aid]
                if any(prior[k] != a[k] for k in ("position_id", "quantity", "price", "gross")):
                    prior.update(
                        evidence="inferred", issues=[issue("source_gap", prior["position_id"], aid)]
                    )
                continue
            dedup[aid] = a
            snap["activity"].append(a)
        elif pid:
            type_ = "signal" if ev in ("signal", "exit") else "order"
            if ev in ("resume_position", "reconcile_closed", "reconcile_add", "settle_hold", "no_exit"):
                type_ = "revision"
            if ev in ("settle_hold", "no_exit"):
                info["pending_settlement"] = True
            if ev in ("reconcile_closed", "reconcile_add"):
                flag = issue("balance_adjustment" if ev == "reconcile_add" else "balance_disappeared", pid)
                if flag not in info["issues"]:
                    info["issues"].append(flag)
            item = line(
                key("runner-event", ev, game, channel, at, order),
                type_,
                at,
                "signal" if type_ == "signal" else "recorded",
                state=ev,
                order_id=order,
                attempt_id=safe_id(r.get("attempt_id")),
                limit_price=maybe_number(r.get("limit")),
            )
            snap["timeline"][pid].append(item)
            if ev in ("reconcile_add", "reconcile_closed"):
                item["observed_quantity"] = maybe_number(r.get("held"))
                if ev == "reconcile_add":
                    item["added_quantity"] = maybe_number(r.get("extra"))
                    item["accounted_quantity"] = maybe_number(r.get("accounted"))
    if env == "live":
        buys = {a["position_id"] for a in snap["activity"] if a["kind"] == "buy"}
        for pid, timeline in snap["timeline"].items():
            if pid not in buys and any(
                t.get("state") in ("resume_position", "settle_hold", "no_exit") for t in timeline
            ):
                snap["meta"][pid]["issues"].append(issue("missing_cost", pid))
    for r in attempts:
        pid = resolve_pid(r["game_id"], r["channel"], r["position_id"])
        if not pid:
            continue
        snap["meta"].setdefault(pid, meta(runner_ident(r["game_id"], r["channel"], None, None, markets)))
        snap["timeline"][pid].append(
            line(
                key("attempt", r["attempt_id"]),
                "order",
                utc(r["updated_at"]),
                "recorded",
                state=safe_id(r["state"]),
                attempt_id=safe_id(r["attempt_id"]),
                order_id=safe_id(r["order_id"]),
            )
        )
    if env == "paper":
        # Older dry runs restored the real wallet's positions before watching a game.
        # Only a simulated opening event is evidence for these lifecycle observations.
        bought = {a["position_id"] for a in snap["activity"] if a["kind"] == "buy"}
        for pid in snap["meta"].keys() - bought:
            snap["meta"][pid]["pending_settlement"] = False
            snap["meta"][pid]["issues"] = []
    return snap


def combine(base, manual, runner, markets):
    snap = blank()
    snap["available_cash"] = base["available_cash"]
    snap["known_origin"] = base["known_origin"]
    snap["coverage_from"] = base["coverage_from"]
    snap["activity"] = list(base["activity"])
    snap["meta"] = {pid: dict(m, issues=list(m["issues"])) for pid, m in base["meta"].items()}
    snap["timeline"] = defaultdict(list, {pid: list(items) for pid, items in base["timeline"].items()})
    orders, by_game, by_asset = defaultdict(list), defaultdict(list), defaultdict(list)
    manual_orders = {m["order_id"] for m in manual}
    for a in base["activity"]:
        if a["order_id"]:
            orders[a["order_id"]].append(a)
            by_game[a["kind"], a["game_id"]].append(a)
            by_asset[a["kind"], a["asset_id"]].append(a)
    for m in manual:
        by_game[m["kind"], m["game_id"]].append(m)
        by_asset[m["kind"], m["asset_id"]].append(m)
    aliases = {}
    reviewed = set()
    runners = []
    for a in runner["activity"]:
        pid, order = a["position_id"], a["order_id"]
        if order and order in manual_orders:
            reviewed.add(pid)
            continue
        exact = orders.get(order, [])
        if exact:
            ids = {f["position_id"] for f in exact}
            qty = sum(Decimal(f["quantity"]) for f in exact)
            same = all(
                f["kind"] == a["kind"]
                and (not a["asset_id"] or f["asset_id"] == a["asset_id"])
                and (not f["game_id"] or not a["game_id"] or f["game_id"] == a["game_id"])
                and (not f["channel"] or not a["channel"] or f["channel"] == a["channel"])
                for f in exact
            )
            if len(ids) == 1 and a["quantity"] is not None and qty == Decimal(a["quantity"]) and same:
                owner = next(iter(ids))
                if pid and owner:
                    aliases[pid] = owner
                    snap["timeline"][owner].append(
                        line(
                            key("source-ref", a["id"]),
                            "revision",
                            a["time"],
                            "recorded",
                            state="runner_summary_matched",
                            order_id=order,
                        )
                    )
                    if a["outcome"]:
                        snap["meta"][owner]["outcome"] = a["outcome"]
                else:
                    reviewed.add(pid)
                continue
        else:
            pool = by_game.get((a["kind"], a["game_id"]), [])
            if a["asset_id"]:
                pool = pool + by_asset.get((a["kind"], a["asset_id"]), [])
            exact = [
                f
                for f in pool
                if not (order and f["order_id"])
                and (not a["asset_id"] or not f["asset_id"] or a["asset_id"] == f["asset_id"])
                and (not a["channel"] or not f["channel"] or a["channel"] == f["channel"])
                and (not a["game_id"] or not f["game_id"] or a["game_id"] == f["game_id"])
            ]
        if exact:
            a = dict(
                a,
                position_id=None,
                evidence="inferred",
                issues=[issue("unattributed", None, a["id"]), *[dict(i, position_id=None) for i in a["issues"]]],
            )
            reviewed.add(pid)
        runners.append(a)
    for a in runners:
        pid = a["position_id"]
        dst = aliases.get(pid, pid)
        if pid in reviewed and pid not in aliases:
            dst = None
        if dst != pid:
            flags = [dict(i, position_id=dst) for i in a["issues"]]
            if not dst:
                flags.append(issue("unattributed", None, a["id"]))
            a = dict(a, position_id=dst, issues=flags)
        snap["activity"].append(a)
    for pid, info in runner["meta"].items():
        dst = aliases.get(pid, pid)
        if pid in reviewed and pid not in aliases:
            continue
        if dst not in snap["meta"]:
            snap["meta"][dst] = dict(info, issues=list(info["issues"]))
        else:
            m = snap["meta"][dst]
            if info.get("pending_settlement"):
                m["pending_settlement"] = True
            m["issues"].extend(dict(i, position_id=dst) for i in info["issues"] if i not in m["issues"])
        snap["timeline"][dst].extend(runner["timeline"].get(pid, []))
    for m in snap["meta"].values():
        if m.get("game_id") in markets:
            match = runner_ident(m["game_id"], m["channel"], m["outcome"], m["asset_id"], markets)
            m["game_label"] = match["game_label"]
            assets = markets[m["game_id"]].get("assets", {})
            outcomes = [out for out, asset in assets.items() if asset and asset == m["asset_id"]]
            if len(outcomes) == 1 and not m["outcome"]:
                m["outcome"] = outcomes[0]

    def enriched(a):
        m = snap["meta"].get(a["position_id"], {})
        return dict(
            a,
            **{
                k: m[k]
                for k in IDENTITY
                if m.get(k) and (not a[k] or (k == "game_label" and a[k] == a["game_id"]))
            },
        )

    snap["activity"] = sorted(
        (with_sports_fee(enriched(a)) for a in snap["activity"]), key=lambda a: (a["time"], a["recorded_at"])
    )
    snap["timeline"] = {
        pid: list({i["id"]: i for i in items}.values()) for pid, items in snap["timeline"].items()
    }
    # A configured but empty opposite-environment archive is not a second book.
    # Verified duplicate summaries were removed above. Signals alone are not inventory.
    runner_exposure = any(a["source"] == "runner" for a in snap["activity"]) or any(
        m["issues"] or m["pending_settlement"]
        for pid, m in snap["meta"].items()
        if pid not in base["meta"]
    )
    if runner_exposure:
        snap.update(known_origin=False, coverage_from=None)
    return snap


class Sources:
    """One dashboard environment. A runner directory belongs to one configured account."""

    def __init__(
        self, ledger=None, runner=None, attempts=None, env="live", account=None, market_journal=None
    ):
        if env not in ("paper", "live"):
            raise ValueError("unknown environment")
        self.env, self.account = env, account
        self.ledger_owner = account
        self.ledger = ReadDatabase(ledger)
        self.attempts = ReadDatabase(attempts if env == "live" else None)
        self.runner = RunnerFiles(runner, market_journal)
        self.lock = threading.Lock()
        self.success = {}
        self.empty = blank()
        self.empty_markets = {}
        self.previous = self.projected = None

    def close(self):
        with self.lock:
            self.ledger.close()
            self.attempts.close()

    def read(self):
        with self.lock:
            return self._read()

    def _read(self):
        statuses, now = [], datetime.now(UTC)
        stamp = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")

        def read(name, source, read_fn, fallback):
            status = dict(
                name=name, status="unconfigured", last_success_at=None, last_event_at=None, reason=None
            )
            statuses.append(status)
            if not source.path:
                return fallback
            if name == "attempts" and not self.success.get(name) and not source.path.exists():
                # Legacy logs can be read before the runner creates its optional attempt ledger.
                return fallback
            try:
                result = read_fn()
                # Release a new health sample at heartbeat cadence during quiet periods.
                previous = self.success.get(name)
                if previous is None or (now - previous[0]).total_seconds() >= 10:
                    self.success[name] = now, stamp
                status.update(status="unknown", reason="写入端状态未知")
                return result
            except WrongScope:
                source.result = None
                status.update(status="down", reason="账户或环境不匹配")
                return fallback
            except (OSError, sqlite3.Error, ValueError, ArithmeticError, KeyError, TypeError):
                status.update(status="down", reason="数据源读取失败")
                return source.result if source.result is not None else fallback
            finally:
                status["last_success_at"] = self.success.get(name, (None, None))[1]

        base, manual, owner = read(
            "executor",
            self.ledger,
            lambda: self.ledger.read(lambda db: executor(db, self.env, self.ledger_owner)),
            (self.empty, (), self.ledger_owner),
        )
        if owner is not None:
            self.ledger_owner = owner
        attempts, allow_untagged = read(
            "attempts",
            self.attempts,
            lambda: self.attempts.read(
                lambda db: attempt_rows(db, owner or self.account), scope=owner or self.account
            ),
            ((), True),
        )
        if statuses[-1]["reason"] == "账户或环境不匹配":
            allow_untagged = False
        scoped, markets, damaged, beat = read(
            "runner",
            self.runner,
            lambda: self.runner.read(self.env, owner or self.account, allow_untagged),
            ((), self.empty_markets, False, None),
        )
        if damaged and statuses[-1]["status"] != "down":
            statuses[-1].update(status="delayed", reason="部分运行记录损坏或缺少环境标记")
        elif beat and statuses[-1]["status"] == "unknown":
            age = (now - datetime.fromisoformat(beat.replace("Z", "+00:00"))).total_seconds()
            if 0 <= age <= 90:
                statuses[-1].update(status="ok", reason=None)
        if self.previous is None or any(
            before is not after for before, after in zip(self.previous, (base, scoped, attempts, markets))
        ):
            runner = build_runner(scoped, markets, attempts, self.env)
            self.projected = combine(base, manual, runner, markets)
            self.last_event = {
                name: max((a["time"] for a in source["activity"]), default=None)
                for name, source in [("runner", runner), ("executor", base)]
            }
            self.last_event["attempts"] = max((utc(r["updated_at"]) for r in attempts), default=None)
            self.previous = base, scoped, attempts, markets
        for status in statuses:
            status["last_event_at"] = self.last_event.get(status["name"])
        result = dict(self.projected, sources=statuses)
        if damaged or any(s["status"] == "down" for s in statuses):
            result.update(known_origin=False, coverage_from=None)
        return result
