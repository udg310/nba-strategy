"""Read-only 稻香 portfolio projection. Inputs and outputs use decimal strings."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
from functools import lru_cache


# Hong Kong has used UTC+08 year-round throughout the NBA trading data coverage.
HK = timezone(timedelta(hours=8), "Asia/Hong_Kong")
LIVE_REPORT_START = datetime(2026, 11, 15, tzinfo=HK)
ZERO = Fraction(0)
IDENTITY = ("game_id", "game_label", "asset_id", "channel", "market", "outcome")


@lru_cache(maxsize=16384)
def _time(value):
    when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if when.tzinfo is None:
        raise ValueError("Dashboard timestamps require an explicit UTC offset")
    return when.astimezone(timezone.utc)


def _stamp(when):
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _num(value):
    if value is None:
        return None
    try:
        number = Decimal(str(value))
        return Fraction(number) if number.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


def _decimal(number):
    if number is None:
        return None
    # Moving-average divisions may recur. Carry exact fractions until the wire
    # boundary, where a recurring decimal is rounded to at least 50 digits.
    with localcontext() as ctx:
        ctx.prec = max(50, len(str(abs(number.numerator))) + len(str(number.denominator)) + 8)
        value = format(Decimal(number.numerator) / Decimal(number.denominator), "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


def _issue(code, pid=None, aid=None):
    return {"code": code, "position_id": pid, "activity_id": aid}


def _issues(*groups):
    unique = {}
    for group in groups:
        for item in group:
            key = (item["code"], item.get("position_id"), item.get("activity_id"))
            unique[key] = _issue(*key)
    return list(unique.values())


@dataclass
class Value:
    amount: Fraction | None = ZERO
    issues: list = field(default_factory=list)
    marked: bool = False
    estimated_fee: bool = False

    def add(self, other, sign=1):
        if self.amount is None or other.amount is None:
            return Value(None, _issues(self.issues, other.issues))
        return Value(self.amount + sign * other.amount, marked=self.marked or other.marked,
                     estimated_fee=self.estimated_fee or other.estimated_fee)


def _metric(values, extra=()):
    amounts = [v.amount for v in values if v.amount is not None]
    reasons = _issues(extra, *(v.issues for v in values))
    complete = len(amounts) == len(values) and not reasons
    subtotal = _decimal(sum(amounts, ZERO))
    estimated = any(v.estimated_fee for v in values if v.amount is not None)
    status = (
        ("marked" if any(v.marked for v in values) else "estimated" if estimated else "verified")
        if complete
        else ("partial" if amounts else "unavailable")
    )
    return {
        "value": subtotal if complete else None,
        "known_subtotal": subtotal if amounts and not complete else None,
        "status": status,
        "includes_estimated_fee": estimated,
        "covered_count": len(amounts),
        "total_count": len(values),
        "issues": reasons,
    }


def _from_metric(metric):
    return Value(_num(metric["value"]), list(metric.get("issues", [])), metric["status"] == "marked",
                 metric.get("includes_estimated_fee", False))


def _quote(raw, now):
    item = {k: raw.get(k) for k in ("bid", "ask", "observed_at", "source_at")}
    item.update(status="missing", valid_until=None)
    if item["observed_at"] is None:
        return item
    observed = _time(item["observed_at"])
    source = _time(item["source_at"]) if item["source_at"] else observed
    # A later sample cannot value an earlier cutoff, even with an old source timestamp.
    if observed > now or source > now:
        return dict.fromkeys(("bid", "ask", "observed_at", "source_at", "valid_until")) | {
            "status": "missing"
        }
    expiry = min(source, observed) + timedelta(seconds=15)
    bid = _num(item["bid"])
    item["valid_until"] = _stamp(expiry)
    if now >= expiry:
        item["status"] = "stale"
    elif bid is None or not 0 < bid <= 1:
        item["status"] = "no_bid"
    else:
        item["status"] = "fresh"
    return item


def valuation(raw, books, asset, when):
    payout = (raw.get("payouts") or {}).get(asset)
    if payout and _time(payout["effective_at"]) <= when and _num(payout["price"]) in (ZERO, Fraction(1, 2), 1):
        return dict(bid=None, ask=None, observed_at=payout["effective_at"], source_at=payout["effective_at"],
                    valid_until=None, status="resolved", payout=payout["price"])
    return _quote(books.get(asset, {}), when)


def _latest_trades(raw):
    by_id = {}
    for row in raw.get("activity", ()):
        # A correction may move the business date across the cutoff. Resolve
        # its identity first, then select the revised business-time history.
        old = by_id.get(row["id"])
        if old and _time(old.get("revised_at") or old["recorded_at"]) > _time(
            row.get("revised_at") or row["recorded_at"]
        ):
            continue
        by_id[row["id"]] = row
    return by_id.values()


def reporting_period(raw, start):
    """Keep in-period records and the cost history needed by continuing positions."""
    latest = list(_latest_trades(raw))
    activity = [r for r in latest if _time(r["time"]) >= start]
    retained_ids = {r["id"] for r in activity}
    timeline = {}
    for pid, events in raw["timeline"].items():
        retained = [e for e in events if _time(e["time"]) >= start
                    and (not e.get("activity") or e["activity"]["id"] in retained_ids)]
        if retained:
            timeline[pid] = retained
    members = set(timeline) | {r["position_id"] for r in activity if r.get("position_id")}
    earlier = [r for r in latest if r.get("position_id") in members and _time(r["time"]) < start]
    coverage = raw.get("coverage_from")
    if coverage and len(coverage) == 10:
        coverage = _stamp(datetime.fromisoformat(coverage).replace(tzinfo=HK))
    return dict(raw, activity=activity, timeline=timeline, report_from=_stamp(start),
                coverage_from=_stamp(max(start, _time(coverage))) if coverage else None,
                meta={p: m for p, m in raw["meta"].items() if p in members},
                opening_trades=dict(_group(_trade_rows({"activity": earlier}, start))))


def _trade_rows(raw, cutoff, exclusive=False):
    rows = []
    within_timestamp = defaultdict(int)
    for source in _latest_trades(raw):
        when = _time(source["time"])
        if when > cutoff or (exclusive and when == cutoff):
            continue
        row = dict(source)
        # A chain-confirmation timestamp may cover several fills. Retain the
        # ledger's ordering when reprojecting a published reverse-sorted list.
        group = (row.get("position_id"), when)
        row.setdefault("source_sequence", within_timestamp[group])
        within_timestamp[group] += 1
        row["issues"] = _issues(row.get("issues", []))
        fee, gross = _num(row.get("fee")), _num(row.get("gross"))
        if fee is None or (fee and row.get("fee_unit") not in ("shares", "cash")):
            row["issues"] = _issues(
                row["issues"], [_issue("missing_fee", row.get("position_id"), row["id"])]
            )
            row["net_cash"] = None
        elif gross is not None and row.get("net_cash") is None:
            cash_fee = fee if row.get("fee_unit") == "cash" else ZERO
            row["net_cash"] = _decimal((-gross if row["kind"] == "buy" else gross) - cash_fee)
        rows.append(row)
    return sorted(rows, key=lambda row: (_time(row["time"]), row["source_sequence"]))


class Holding:
    def __init__(self, pid):
        self.id = pid
        self.quantity = ZERO
        self.cost = Value()
        self.realized = Value()
        self.entry_qty = ZERO
        self.entry_amount = Value()
        self.sold = False
        self.gaps = []

    def apply(self, row):
        aid = row["id"]
        reason = lambda code: _issue(code, self.id, aid)
        if row.get("evidence") == "inferred" or any(
            i["code"] in ("unattributed", "source_gap") for i in row["issues"]
        ):
            self.gaps = _issues(self.gaps, row["issues"], [reason("source_gap")])
            return
        quantity, fee = _num(row.get("quantity")), _num(row.get("fee"))
        if quantity is None or quantity <= 0 or (fee is not None and fee < 0):
            self.gaps = _issues(self.gaps, [reason("source_gap")])
            return
        buy = row["kind"] == "buy"
        share_fee = fee if row.get("fee_unit") == "shares" else ZERO
        if share_fee is None:
            self.gaps = _issues(self.gaps, [reason("missing_fee")])
            return
        cash = _num(row.get("net_cash"))
        problems = [i for i in row["issues"] if i["code"] in ("missing_fee", "missing_cost")]
        if cash is None and not problems:
            problems.append(reason("missing_cost"))
        payment = Value(None, problems) if problems else Value(-cash if buy else cash,
                        estimated_fee=row.get("fee_basis") == "estimated_taker")
        if buy:
            if share_fee > quantity:
                self.gaps = _issues(self.gaps, [reason("source_gap")])
                return
            self.quantity += quantity - share_fee
            self.cost = self.cost.add(payment)
            self.entry_qty += quantity
            price, gross = _num(row.get("price")), _num(row.get("gross"))
            amount = quantity * price if price is not None else gross
            self.entry_amount = self.entry_amount.add(Value(amount))
            return
        self.sold = True
        outgoing = quantity + share_fee
        if outgoing > self.quantity:
            self.gaps = _issues(self.gaps, [reason("missing_cost")])
            return
        basis = (
            Value(self.cost.amount * outgoing / self.quantity, estimated_fee=self.cost.estimated_fee)
            if self.cost.amount is not None
            else self.cost
        )
        gain = payment.add(basis, -1)
        self.realized = self.realized.add(gain)
        self.quantity -= outgoing
        self.cost = self.cost.add(basis, -1) if self.quantity else Value()
        return gain.amount

    def measures(self, quote, gaps):
        if self.gaps or gaps:
            missing = Value(None, _issues(self.gaps, gaps))
            return missing, missing, missing, None
        market = ZERO
        if not self.quantity:
            floating = Value()
        else:
            issue = {"missing": "no_quote", "stale": "stale_quote", "no_bid": "no_bid"}.get(
                quote["status"]
            )
            mark_price = quote.get("payout") if quote["status"] == "resolved" else quote["bid"]
            market = None if issue else self.quantity * _num(mark_price)
            mark = Value(market, [_issue(issue, self.id)] if issue else [], marked=True)
            floating = mark.add(self.cost, -1)
        return self.realized, floating, self.realized.add(floating), market


def _group(rows):
    grouped = defaultdict(list)
    for row in rows:
        if row.get("position_id"):
            grouped[row["position_id"]].append(row)
    return grouped


def _balance(pid, rows, detail=None, prior=()):
    holding = Holding(pid)
    for row in prior:
        holding.apply(row)
    holding.realized = Value()
    for row in rows:
        gain = holding.apply(row)
        if detail is not None:
            detail[row["id"]] = {
                "remaining_quantity": _decimal(holding.quantity) if not holding.gaps else None,
                "remaining_cost": _decimal(holding.cost.amount) if not holding.gaps else None,
                "realized_change": _decimal(gain) if not holding.gaps else None,
            }
    return holding


def _before_fees(pid, trades, quote, gap, prior=()):
    """A separate estimate for incomplete fees, never aggregated into net P&L."""
    if gap or not any(t.get("fee") is None for t in (*prior, *trades)):
        return None
    estimate = Holding(pid)
    for index, t in enumerate((*prior, *trades)):
        if index == len(prior):
            estimate.realized = Value()
        gross = _num(t.get("gross"))
        if gross is None or t.get("fee_unit") == "shares":
            return None
        cash = -gross if t["kind"] == "buy" else gross
        estimate.apply(
            dict(
                t,
                net_cash=_decimal(cash),
                fee="0",
                fee_unit="cash",
                issues=[i for i in t["issues"] if i["code"] != "missing_fee"],
            )
        )
    if not trades:
        estimate.realized = Value()
    if estimate.gaps:
        return None
    realized, unrealized, total, _ = estimate.measures(quote, [])
    return {
        "realized": _decimal(realized.amount),
        "unrealized": _decimal(unrealized.amount),
        "total": _decimal(total.amount),
    }


def baselines(raw, quotes_at_midnight, midnight):
    """Rebuild totals immediately before midnight with bids valid at midnight.

    Pass the latest archived observation at/before the boundary for each asset.
    Replay corrected business-time records. Do not reuse current metadata, cash,
    position state, or a quote first observed after the boundary.
    """
    result = {}
    prior = raw.get("opening_trades", {})
    grouped = _group(_trade_rows(raw, midnight, exclusive=True))
    for pid in set(prior) | set(grouped):
        rows, earlier = grouped.get(pid, []), prior.get(pid, [])
        holding = _balance(pid, rows, prior=earlier)
        quote = valuation(raw, quotes_at_midnight, (rows or earlier)[-1].get("asset_id"), midnight)
        total = holding.measures(quote, [])[2]
        if not raw.get("known_origin") and not (earlier or rows)[0].get("opens_position"):
            total = Value(None, _issues(total.issues, [_issue("no_day_baseline", pid)]))
        result[pid] = _metric([total])
    return result


def _timeline(raw, rows, now, detail):
    timeline = {}
    for pid in set(raw.get("timeline", {})) | set(rows):
        activities = {r["id"]: r for r in rows.get(pid, [])}
        attached = set()
        revised = set()
        items = []
        for item in raw.get("timeline", {}).get(pid, []):
            if _time(item["time"]) > now:
                continue
            row = item.get("activity")
            if row and item["type"] == "revision":
                revised.add((row["id"], item["time"]))
            if row and item["type"] in ("fill", "settlement"):
                if row["id"] not in activities:
                    continue
                attached.add(row["id"])
                row = activities[row["id"]]
                item = dict(
                    item, activity=row, time=row["time"], time_kind=row["time_kind"], **detail[row["id"]]
                )
            items.append(item)
        for aid, row in activities.items():
            revision = row.get("revised_at")
            if revision and (aid, revision) not in revised and _time(revision) <= now:
                items.append(
                    {
                        "id": "revision:" + aid,
                        "type": "revision",
                        "time": revision,
                        "time_kind": "recorded",
                        "state": None,
                        "reason": "流水已修订",
                        "order_id": row.get("order_id"),
                        "attempt_id": None,
                        "limit_price": None,
                        "activity": row,
                        "evidence_ref": row.get("fill_id"),
                    }
                )
            if aid in attached:
                continue
            items.append(
                {
                    "id": "timeline:" + aid,
                    "type": "settlement" if row["kind"] == "settlement" else "fill",
                    "time": row["time"],
                    "time_kind": row["time_kind"],
                    "state": None,
                    "reason": None,
                    "order_id": row.get("order_id"),
                    "attempt_id": None,
                    "limit_price": None,
                    "activity": row,
                    "evidence_ref": row.get("fill_id"),
                    **detail[aid],
                }
            )
        timeline[pid] = sorted(items, key=lambda e: (_time(e["time"]), e["id"]), reverse=True)
    return timeline


def _coverage_gaps(raw, now):
    start = raw.get("coverage_from")
    if start and len(start) == 10:
        start = _stamp(datetime.fromisoformat(start).replace(tzinfo=HK))
    sources = [s for s in raw.get("sources", []) if s.get("name") in ("runner", "executor")]
    working = [s for s in sources if s.get("status") != "unconfigured"]
    if (
        not raw.get("known_origin")
        or (start and _time(start) > now)
        or (sources and not working)
        or any(s.get("status") == "down" for s in working)
    ):
        return [_issue("source_gap")]
    return []


def _context_gaps(coverage, activities):
    gaps = list(coverage)
    for row in activities:
        if not row.get("position_id"):
            gaps.append(_issue("unattributed", aid=row["id"]))
    return gaps


def reconciled_channels(raw, grouped, now):
    """Recognize two same-side runner openings reconciled against the same wallet balance."""
    candidates = defaultdict(list)
    for pid, rows in grouped.items():
        sides = {(r.get("game_id"), r.get("outcome")) for r in rows}
        if len(sides) == 1:
            candidates[next(iter(sides))].append(pid)
    checked, shared = set(), {}
    for (gid, side), pids in candidates.items():
        if not gid or not side or len(pids) != 2:
            continue
        if any(pid in raw.get("opening_trades", {}) for pid in pids):
            continue
        rows = [r for pid in pids for r in grouped[pid]]
        assets = {r["asset_id"] for r in rows if r.get("asset_id")}
        if (len(assets) > 1 or len({r.get("channel") for r in rows}) != 2
                or any(r.get("source") != "runner" or r["evidence"] != "aggregate"
                       or r["kind"] not in ("buy", "sell") or r.get("issues") or not r.get("channel")
                       or r.get("fee_unit") != "cash" or _num(r.get("net_cash")) is None
                       or (_num(r.get("quantity")) or ZERO) <= 0 for r in rows)):
            continue
        buys = [r for r in rows if r["kind"] == "buy"]
        if len(buys) != 2 or {r["position_id"] for r in buys} != set(pids):
            continue
        if any(i["code"] not in ("balance_adjustment", "balance_disappeared")
               for pid in pids for i in raw.get("meta", {}).get(pid, {}).get("issues", [])):
            continue
        events = [(pid, e) for pid in pids for e in raw.get("timeline", {}).get(pid, [])
                  if e.get("state") in ("reconcile_add", "reconcile_closed") and _time(e["time"]) <= now]
        adds = [(pid, e) for pid, e in events if e["state"] == "reconcile_add"]
        closes = [e for _, e in events if e["state"] == "reconcile_closed"]
        if len(adds) != 1 or len(closes) != 1:
            continue
        (owner, add), = adds
        close, = closes
        quantities = {r["position_id"]: _num(r["quantity"]) for r in buys}
        peer = next(pid for pid in pids if pid != owner)
        net = dict(quantities)
        for row in rows:
            if row["kind"] == "sell":
                net[row["position_id"]] -= _num(row["quantity"])
        rest = sum(net.values(), ZERO)
        observed = _num(close.get("observed_quantity"))
        if (not any(r["kind"] == "sell" for r in rows) or _time(close["time"]) < max(_time(r["time"]) for r in rows)
                or any(_time(r["time"]) < max(_time(b["time"]) for b in buys) for r in rows if r["kind"] == "sell")
                or _time(close["time"]) < _time(add["time"]) or observed is None or rest < 0
                or abs(observed - rest) > Fraction(1, 1000000)
                or _num(add.get("added_quantity")) != quantities[peer]
                or _num(add.get("accounted_quantity")) != quantities[owner]
                or _num(add.get("observed_quantity")) != sum(quantities.values(), ZERO)):
            continue
        if min(net.values()) >= 0:
            checked.update(pids)
        else:
            shared[tuple(sorted(pids))] = (rows, rest, next(iter(assets), None))
    return checked, shared


def include_joint_members(metric, joint):
    # The pooled amount appears once, while coverage is expressed in underlying positions.
    for group in joint:
        extra = len(group["position_ids"]) - 1
        metric["total_count"] += extra
        if group["total"]["value"] is not None:
            metric["covered_count"] += extra


def project(raw, quotes, now, baseline=None):
    """Project one account/environment, as of an aware datetime, without I/O."""
    if now.tzinfo is None:
        raise ValueError("Dashboard clock requires an explicit UTC offset")
    midnight = now.astimezone(HK).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = _trade_rows(raw, now)
    grouped = _group(rows)
    checked, shared = reconciled_channels(raw, grouped, now)
    joint = {}
    for members, (fills, rest, asset) in shared.items():
        q = valuation(raw, quotes, asset, now)
        mark = _num(q.get("payout") if q["status"] == "resolved" else q["bid"]) if q["status"] in ("fresh", "resolved") else None
        cash = sum((_num(r["net_cash"]) for r in fills), ZERO)
        estimated = any(r.get("fee_basis") == "estimated_taker" for r in fills)
        total = (Value(cash + rest * (mark or ZERO), marked=bool(rest), estimated_fee=estimated)
                 if not rest or mark is not None else Value(None, [_issue("no_quote", members[0])]))
        new_today = all(grouped[pid][0].get("opens_position") and _time(grouped[pid][0]["time"]) >= midnight for pid in members)
        for pid in members:
            joint[pid] = dict(position_ids=list(members), quantity=_decimal(rest), net_cash=_decimal(cash),
                              total=_metric([total]), opened_today=new_today)
    detail = {}
    meta = raw.get("meta", {})
    opening = baselines(raw, {}, midnight) if baseline is None else baseline
    measures = {k: [] for k in ("today", "realized", "unrealized", "total")}
    positions = []
    open_positions = []
    marked_times = []
    for pid in sorted(set(meta) | set(grouped) | set(raw.get("timeline", {}))):
        trades = grouped.get(pid, [])
        flags = meta.get(pid, {})
        identity = {key: flags.get(key) for key in IDENTITY}
        for row in trades:
            for key in IDENTITY:
                if row.get(key) is not None:
                    identity[key] = row[key]
        prior = raw.get("opening_trades", {}).get(pid, [])
        holding = _balance(pid, trades, detail, prior)
        quote = valuation(raw, quotes, identity["asset_id"], now)
        gap = _issues(flags.get("issues", []))
        if pid in checked:
            gap = [i for i in gap if i["code"] not in ("balance_adjustment", "balance_disappeared")]
        elif pid in joint:
            gap = [_issue("shared_exit", pid)]
            holding.gaps = [i for i in holding.gaps if i["code"] != "missing_cost"]
        realized, floating, total, market = holding.measures(quote, gap)
        unsure = bool(gap or holding.gaps)
        qty = None if unsure else holding.quantity
        had_trade_today = bool(trades and _time(trades[-1]["time"]) >= midnight)
        active_today = bool(had_trade_today or qty or unsure)
        has_exposure = bool(trades or prior or unsure)
        if not active_today:
            today = Value()
        elif pid in opening:
            start = _from_metric(opening[pid])
            if start.amount is None:
                start = Value(None, _issues(start.issues, [_issue("no_day_baseline", pid)]))
            today = total.add(start, -1)
        elif trades and (raw.get("known_origin") or trades[0].get("opens_position")) and _time(trades[0]["time"]) >= midnight:
            today = total
        else:
            today = Value(None, _issues(total.issues, [_issue("no_day_baseline", pid)]))
        quality = _issues(total.issues, today.issues, gap, holding.gaps)
        state = "open"
        if unsure:
            state = "review"
        elif not qty:
            state = "closed" if trades else "review"
        elif flags.get("pending_settlement") or quote["status"] == "resolved":
            state = "pending_settlement"
        elif total.issues and (holding.cost.amount is None or holding.realized.amount is None):
            state = "review"
        elif holding.sold:
            state = "partially_closed"
        p = dict(
            identity,
            id=pid,
            opened_at=trades[0]["time"] if trades else None,
            last_activity_at=trades[-1]["time"] if trades else None,
            issues=quality,
            state=state,
            listed=state not in ("review", "pending_settlement"),
            quantity=_decimal(qty),
            reserved_quantity=flags.get("reserved_quantity"),
            avg_entry_price=(
                _decimal(holding.entry_amount.amount / holding.entry_qty)
                if holding.entry_qty and holding.entry_amount.amount is not None
                else None
            ),
            unit_cost=(
                _decimal(holding.cost.amount / qty) if qty and holding.cost.amount is not None else None
            ),
            remaining_cost=_decimal(holding.cost.amount) if not unsure else None,
            shared_exit=joint.get(pid),
            before_fees=_before_fees(pid, trades, quote, gap, prior),
            market_value=_decimal(market),
            quote=quote,
            active_today=active_today,
            has_exposure=has_exposure,
        )
        for name, value in (
            ("realized", realized),
            ("unrealized", floating),
            ("today", today),
            ("total", total),
        ):
            p[name] = _metric([value])
            if has_exposure and (name != "today" or active_today):
                group = joint.get(pid)
                if group and (name == "total" or (name == "today" and group["opened_today"])):
                    if pid == group["position_ids"][0]:
                        measures[name].append(_from_metric(group["total"]))
                else:
                    measures[name].append(value)
        positions.append(p)
        if has_exposure and (qty is None or qty > 0):
            open_positions.append(p)
            if quote["status"] in ("fresh", "resolved") and market is not None:
                marked_times.append(quote["observed_at"])
    open_positions.sort(
        key=lambda p: (
            p["unrealized"]["value"] is None,
            -abs(_num(p["unrealized"]["value"]) or ZERO),
            p["id"],
        )
    )
    rows.sort(key=lambda r: (_time(r["time"]), r["id"]), reverse=True)
    today_rows = [r for r in rows if _time(r["time"]) >= midnight]
    coverage_gaps = _coverage_gaps(raw, now)
    context = _context_gaps(coverage_gaps, rows)
    summary = {key: _metric(values, context) for key, values in measures.items()}
    pooled = [joint[members[0]] for members in shared]
    include_joint_members(summary["total"], pooled)
    include_joint_members(summary["today"], (g for g in pooled if g["opened_today"]))
    listed = [p for p in open_positions if p["listed"]]
    hidden_quote_deadlines = {}
    for p in open_positions:
        if p["listed"] or p["quote"]["status"] != "fresh":
            continue
        quantity = (p["shared_exit"] or p)["quantity"]
        if (_num(quantity) or ZERO) > 0:
            gid = p["game_id"] or ""
            expiry = p["quote"]["valid_until"]
            if gid not in hidden_quote_deadlines or _time(expiry) < _time(hidden_quote_deadlines[gid]):
                hidden_quote_deadlines[gid] = expiry
    summary.update(
        available_cash=raw.get("available_cash"),
        day_start_cash=raw.get("opening_cash", {}).get(midnight.date().isoformat()),
        day_start_at=_stamp(midnight),
        valued_at=min(marked_times, key=_time) if marked_times else None,
        open_count=len(listed),
        review_count=sum(p["has_exposure"] and p["state"] == "review" for p in positions)
        + sum(not r.get("position_id") for r in rows),
    )
    channel_values = defaultdict(list)
    for p in positions:
        if p["channel"] and p["has_exposure"] and p["active_today"]:
            channel_values[p["channel"]].append(_from_metric(p["today"]))
    snapshot = {
        "account_label": raw.get("account_label"),
        "currency": raw.get("currency", "USD"),
        "timezone": "Asia/Hong_Kong",
        "business_date": midnight.date().isoformat(),
        "coverage_from": raw.get("coverage_from"),
        "report_from": raw.get("report_from"),
        "summary": summary,
        "today_by_channel": {
            ch: _metric(values, _context_gaps(coverage_gaps, [r for r in today_rows if r["channel"] in (ch, None)]))
            for ch, values in channel_values.items()
        },
        "sources": raw.get("sources", []),
        "open_positions": listed,
        "hidden_quote_deadlines": hidden_quote_deadlines,
        "today_activity": today_rows,
    }
    return {
        "snapshot": snapshot,
        "valuation_positions": open_positions,
        "positions": positions,
        "activity": rows,
        "timeline": _timeline(raw, grouped, now, detail),
    }


def game_results(raw, view):
    """Group the projected book by game. None is the fallback for an untraded fixture."""
    positions = defaultdict(list)
    activities = defaultdict(list)
    for p in view["positions"]:
        if p["has_exposure"]:
            positions[p.get("game_id")].append(p)
    for row in view["activity"]:
        activities[row.get("game_id")].append(row)
    coverage = [
        i
        for i in view["snapshot"]["summary"]["total"]["issues"]
        if i["code"] == "source_gap" and i.get("activity_id") is None and i.get("position_id") is None
    ]
    result = {}
    for gid in set(positions) | set(activities) | {None}:
        group = positions[gid] if gid else []
        rows = activities[gid] if gid else []
        # Unattributed fills with an unknown game could belong to any fixture.
        reasons = _context_gaps(coverage, rows + activities.get(None, []))
        if positions.get(None):
            reasons = _issues(reasons, [_issue("unattributed", p["id"]) for p in positions[None]])
        result[gid] = {
            key: _metric([_from_metric(p["shared_exit"]["total"] if key == "total" and p.get("shared_exit") else p[key])
                          for p in group if key != "total" or not p.get("shared_exit")
                          or p["id"] == p["shared_exit"]["position_ids"][0]], reasons)
            for key in ("total", "realized", "unrealized")
        }
        result[gid].update(trade_count=len(rows), position_count=len(group))
        include_joint_members(result[gid]["total"], (p["shared_exit"] for p in group
                              if p.get("shared_exit") and p["id"] == p["shared_exit"]["position_ids"][0]))
    return result
