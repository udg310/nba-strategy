"""Run the NBA execution service and optional strategy in one Python process."""

import argparse
from contextlib import closing
import ipaddress
import logging
import os
from pathlib import Path
import secrets
import signal
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent))

from executor_api import ExecutorAPI, serve
from executor_engine import Engine
from executor_ledger import Conflict, Ledger
from executor_lock import account_lock
from executor_producer import follow_scoreboard, run_recording
from executor_reports import Reports
from executor_venues import LiveVenue, PaperVenue
from telegram_alerts import TelegramAlerts


def loopback(value):
    try:
        host, port = value.rsplit(":", 1)
        addr = ipaddress.ip_address(host.removeprefix("[").removesuffix("]"))
        port = int(port)
        if not addr.is_loopback or not 0 <= port <= 65535:
            raise ValueError
        return str(addr), port
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a loopback IP and port") from exc


def options():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--scoreboard", "-scoreboard", action="store_true")
    source.add_argument("--recording", "-recording", type=Path, help="replay a paper recording and exit")
    source.add_argument("--init-live", "-init-live", action="store_true", help="create a fresh live ledger and exit")
    parser.add_argument("--mode", "-mode", choices=("paper", "live"), default="paper")
    parser.add_argument("--account", "-account", default="local")
    parser.add_argument("--db", "-db", type=Path)
    parser.add_argument("--listen", "-listen", type=loopback, default="127.0.0.1:8787")
    parser.add_argument("--capital", "-capital", type=int, default=100_000_000, help="initial cash in millionths")
    parser.add_argument("--max-open", "-max-open", type=int, default=5)
    parser.add_argument("--market-data", choices=("http", "observe", "ws"), default="http")
    parser.add_argument("--score-data", choices=("http", "observe"), default="http")
    args = parser.parse_args()
    if args.recording and args.mode != "paper":
        parser.error("--recording requires paper mode")
    if args.init_live and args.mode != "live":
        parser.error("--init-live requires live mode")
    if args.market_data != "http" and not args.scoreboard:
        parser.error("--market-data requires --scoreboard")
    if args.score_data != "http" and not args.scoreboard:
        parser.error("--score-data requires --scoreboard")
    if args.mode == "live":
        args.account = args.account.lower()
        if (not args.db or not args.account or args.account == "local"
                or args.account != os.getenv("POLY_FUNDER", "").lower()):
            parser.error("live mode requires --db and --account matching POLY_FUNDER")
    args.db = args.db or Path("var/executor/paper.db")
    return args


def prepare_ledger(args):
    args.db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.init_live:
        # Exclusive creation also prevents inadvertently reinitializing an old ledger.
        args.db.touch(mode=0o600, exist_ok=False)
    elif args.mode == "live" and (not args.db.is_file() or not args.db.stat().st_size):
        raise ValueError("existing live ledger required; initialize once with --init-live")


def bearer_token(db, mode):
    token = os.getenv("NBA_EXECUTOR_TOKEN")
    if token:
        return token
    if mode == "live":
        raise ValueError("set NBA_EXECUTOR_TOKEN for live execution")
    path = Path(str(db) + ".token")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        token = path.read_text(encoding="utf8").strip()
        if not token:
            raise ValueError("empty bearer token file")
        return token
    token = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w", encoding="utf8") as destination:
        destination.write(token + "\n")
    return token


def run(args):
    with account_lock(args.account, environment=args.mode):
        prepare_ledger(args)
        with closing(Ledger(args.db, args.account, args.mode, args.capital, args.max_open)) as ledger:
            reports = Reports(ledger)
            if args.init_live:
                logging.info("initialized live ledger %s", args.db)
                return
            token = bearer_token(args.db, args.mode)
            venue = LiveVenue(ledger) if args.mode == "live" else PaperVenue(ledger)
            engine = Engine(ledger, reports, venue)
            try:
                engine.recover()
            except Exception as exc:
                logging.warning("startup reconciliation pending: %s", exc)
            api = ExecutorAPI(ledger, reports, engine)
            alerts = None
            if os.getenv("NBA_TELEGRAM_BOT_TOKEN") or os.getenv("NBA_TELEGRAM_CHAT_ID"):
                alerts = TelegramAlerts(ledger, reports)
            with serve(api, args.listen, token) as addr:
                try:
                    engine.start()
                    if alerts:
                        alerts.start()
                    logging.info("executor ready on %s:%s mode=%s", addr[0], addr[1], args.mode)
                    if args.scoreboard or args.recording:
                        logging.info("strategy started")
                    if args.recording:
                        run_recording(api, args.recording, None, None)
                    elif args.scoreboard:
                        follow_scoreboard(api, market_data=args.market_data,
                                          score_data=args.score_data)
                    else:
                        # Periodic wakeups also deliver console signals on Windows.
                        idle = threading.Event()
                        while not idle.wait(0.5):
                            pass
                finally:
                    if alerts:
                        alerts.stop()
                    engine.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    try:
        run(options())
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError, Conflict) as exc:
        sys.exit(f"executor stopped: {exc}")
