"""Show the live runner's cross-day queue and record verified account evidence."""

import argparse
import json
import math
import re
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from executor_lock import account_lock
from live_ledger import AttemptLedger


DEFAULT_DB = Path(__file__).resolve().parents[1] / "var" / "live_runner" / "attempts.db"
HANDOFF_KINDS = {"unconfirmed_open": "unconfirmed_entry", "settle_hold": "planned_settlement",
                 "force": "forced_exit_incomplete"}


def receipt_reference(value):
    """One finalized Polygon event, normalized for the ledger's duplicate check."""
    match = re.fullmatch(r"(0x[0-9a-f]{64}):([0-9]+)", value, re.IGNORECASE)
    if not match:
        raise ValueError("receipt must be a transaction hash and decimal log index (0xHASH:INDEX)")
    return f"{match[1].lower()}:{int(match[2])}"


def _venue_reference(value):
    if not re.fullmatch(r"venue-(case|record):\S+", value):
        raise ValueError("reference must identify a verified venue-case:ID or venue-record:ID")
    return value


def _status(task):
    state = task["state"]
    if state == "DONE":
        if task["redemption_ref"]:
            return "redeemed"
        if task["payout_price"] == 0:
            return "settled_zero"
        if task["sold_shares"]:
            return "sold"
        if task["initial_action"] == "OPEN" and task["terminal_matched"] == 0:
            return "zero_fill"
        return "complete"
    if state == "REVIEW":
        return "manual_review"
    if state == "REDEEMABLE":
        return "redeemable"
    return "unresolved_order" if task["action"] in ("OPEN", "CLOSE") else "awaiting_resolution"


def _account_name(db, requested):
    # Live rows use the original spelling of POLY_FUNDER. Refuse to silently
    # combine different ledger namespaces if old runs used different casing.
    matches = [row[0] for row in db.execute("""
        SELECT account FROM attempts WHERE account=? COLLATE NOCASE
        UNION SELECT account FROM reconcile_tasks WHERE account=? COLLATE NOCASE
    """, (requested, requested))]
    if requested in matches:
        return requested
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ValueError("multiple account spellings in this ledger; supply the exact stored account")
    if db.execute("SELECT 1 FROM attempts UNION ALL SELECT 1 FROM reconcile_tasks LIMIT 1").fetchone():
        raise ValueError("account has no records in this ledger; verify the account and database path")
    return requested


def account_report(path, account, include_done=False):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")  # The task, reservation and fill lists use the same WAL snapshot.
        account = _account_name(db, account)
        rows = db.execute("""
            SELECT * FROM reconcile_tasks WHERE account=? AND (? OR state!='DONE')
            ORDER BY created_at,game_id,channel
        """, (account, include_done))
        tasks = {}
        for row in rows:
            task = dict(row)
            task["handoff_kind"] = HANDOFF_KINDS.get(task["reason"], "exit_incomplete")
            task["status"] = _status(task)
            task["venue_order_id"] = task["order_id"] if task["identity_verified"] and task["order_id"] else None
            task["order_identity"] = None
            if task["order_id"]:
                task["order_identity"] = "verified" if task["identity_verified"] else "unverified_legacy"
            task["settlement_gross_pnl_usd"] = None
            if task["settled_shares"] > 0 and task["entry"] > 0:
                task["settlement_gross_pnl_usd"] = task["settled_payout"] - task["entry"] * task["settled_shares"]
            task["recorded_fills"] = []
            task["order_links"] = []
            tasks[(task["game_id"], task["channel"])] = task
        for fill in db.execute("""
            SELECT f.game_id,f.channel,f.action,f.order_id,f.fill_id,f.size,f.price
            FROM order_fills AS f JOIN reconcile_tasks AS t USING (account,game_id,channel)
            WHERE t.account=? AND (? OR t.state!='DONE')
            ORDER BY f.game_id,f.channel,f.action,f.order_id,f.fill_id
        """, (account, include_done)):
            tasks[(fill["game_id"], fill["channel"])]["recorded_fills"].append(dict(fill))
        for link in db.execute("""
            SELECT l.game_id,l.channel,l.action,l.old_id,l.venue_id,l.reference,l.created_at
            FROM order_links AS l JOIN reconcile_tasks AS t USING (account,game_id,channel)
            WHERE t.account=? AND (? OR t.state!='DONE') ORDER BY l.created_at,l.action
        """, (account, include_done)):
            tasks[(link["game_id"], link["channel"])]["order_links"].append(dict(link))

    pending = [task for task in tasks.values() if task["state"] != "DONE"]
    return {
        "account": account,
        "pending_tasks": len(pending),
        "known_reserved_usd": sum(task["reserved_usd"] for task in pending),
        "unknown_reservations": sum(task["action"] == "OPEN" and task["initial_reserved_usd"] <= 0
                                    for task in pending),
        "tasks": list(tasks.values()),
    }


@contextmanager
def _evidence_ledger(args):
    with account_lock(args.account):
        # An operator write requires the exact namespace stored by the runner.
        with closing(sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("""
                SELECT token FROM reconcile_tasks WHERE account=? AND game_id=? AND channel=?
            """, (args.account, args.game, args.channel)).fetchone()
            if not row or row[0] != args.token:
                raise ValueError("account, game, channel and token must match the exact ledger task")
        ledger = AttemptLedger(str(args.db), args.account, recover=False)
        try:
            yield ledger
        finally:
            ledger.close_path()


def _record_redemption(args):
    ref = receipt_reference(args.receipt)
    if not all(math.isfinite(n) and n > 0 for n in (args.shares, args.payout)):
        raise ValueError("shares and total payout must be finite and positive")
    with _evidence_ledger(args) as ledger:
        if not ledger.confirm_redemption(args.game, args.channel, ref, args.shares, args.payout):
            raise ValueError("receipt, shares or payout conflict with the task or a prior redemption")
    return {"account": args.account, "game_id": args.game, "channel": args.channel,
            "status": "redeemed", "redemption_ref": ref, "shares": args.shares, "payout_usd": args.payout}


def _record_zero(args):
    ref = _venue_reference(args.reference)
    with _evidence_ledger(args) as ledger:
        if not ledger.confirm_zero(args.game, args.channel, args.order_id, ref):
            raise ValueError("zero-fill evidence must match the reviewed order and its recorded history")
    return {"account": args.account, "game_id": args.game, "channel": args.channel,
            "zero_order_id": args.order_id, "zero_ref": ref}


def _link_order(args):
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", args.venue_id):
        raise ValueError("venue order ID must be the full 32-byte exchange order hash")
    ref = (receipt_reference(args.reference) if args.reference.lower().startswith("0x")
           else _venue_reference(args.reference))
    with _evidence_ledger(args) as ledger:
        if not ledger.link_order(args.game, args.channel, args.local_id, args.venue_id, ref):
            raise ValueError("link must identify the reviewed legacy order and agree with any existing evidence")
    return {"account": args.account, "game_id": args.game, "channel": args.channel,
            "old_id": args.local_id, "venue_id": args.venue_id, "reference": ref}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="existing live_runner SQLite ledger")
    parser.add_argument("--account", required=True, help="public funding account (POLY_FUNDER)")
    parser.add_argument("--all", action="store_true", help="include completed tasks in the report")
    commands = parser.add_subparsers(dest="command")
    confirm = commands.add_parser("confirm-redemption", help="record a previously verified redemption receipt")
    zero = commands.add_parser("confirm-zero", help="attest to the venue's verified final zero fill for this signed order")
    link = commands.add_parser("link-order", help="attest to a verified link from a legacy local ID to the venue order")
    for command, operation in ((confirm, _record_redemption), (zero, _record_zero), (link, _link_order)):
        command.set_defaults(operation=operation)
        command.add_argument("--game", required=True)
        command.add_argument("--channel", required=True)
        command.add_argument("--token", required=True)
    confirm.add_argument("--receipt", required=True, help="Polygon transaction hash and log index (0xHASH:INDEX)")
    confirm.add_argument("--shares", required=True, type=float)
    confirm.add_argument("--payout", required=True, type=float, help="total USD received for those shares")
    zero.add_argument("--order-id", required=True, help="verified venue order ID in the reviewed task")
    zero.add_argument("--reference", required=True, help="verified final-zero evidence (venue-case:ID or venue-record:ID)")
    link.add_argument("--local-id", required=True, help="unverified order ID currently shown for the task")
    link.add_argument("--venue-id", required=True, help="verified exchange order hash for that exact placement")
    link.add_argument("--reference", required=True, help="venue-case:ID, venue-record:ID or Polygon 0xHASH:LOG_INDEX")
    args = parser.parse_args(argv)
    try:
        if args.command:
            if args.all:
                parser.error("--all applies to the report only")
            result = args.operation(args)
        else:
            result = account_report(args.db, args.account, args.all)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
