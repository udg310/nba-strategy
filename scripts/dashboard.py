"""Start 稻香, the anonymous read-only NBA dashboard. Run with --help for source paths."""

import argparse
import base64
import hashlib
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from dashboard_games import GamesFeed
from dashboard_markets import MarketDirectory
from dashboard_quotes import QuoteFeed
from dashboard_service import Dashboard
from dashboard_sources import Sources
from dashboard_store import HK, UTC, ResetRequired, VersionStore, encode, instant, stamp
from executor_lock import account_lock

API = "/api/dashboard/v1"
STATIC = ROOT / "dashboard"
# Reports start from the current Hong Kong day, so the board shows today's
# live P&L without needing the executor ledger's account origin.
LIVE_REPORT_START = datetime.now(HK).replace(hour=0, minute=0, second=0, microsecond=0)


def number(value, default, maximum):
    n = int(value) if value is not None else default
    if not 1 <= n <= maximum:
        raise ValueError("分页数量越界")
    return n


def page_args(route, env, params):
    cursor = params.get("cursor")
    query = {k: v for k, v in params.items() if k not in ("cursor", "at_version", "limit")}
    signature = hashlib.sha256(encode([env, route, sorted(query.items())]).encode()).hexdigest()
    version = params.get("at_version")
    offset = 0
    if cursor:
        try:
            version_in, offset, check = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
            if (
                not isinstance(version_in, str)
                or type(offset) is not int
                or offset < 0
                or check != signature
            ):
                raise ValueError
            if version and version != version_in:
                raise ValueError
            version = version_in
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("无效分页游标") from None

    def slice_page(items, ver):
        limit = number(params.get("limit"), 50, 200)
        end = offset + limit
        nxt = (
            base64.urlsafe_b64encode(encode([ver, end, signature]).encode()).decode()
            if end < len(items)
            else None
        )
        return items[offset:end], nxt

    return version, slice_page


class Query:
    def __init__(self, dashboard):
        self.app = dashboard

    def get(self, route, env, params):
        store = self.app.stores[env]
        if route == "/changes":
            if not params.get("after"):
                raise ValueError("缺少起始版本")
            return store.changes(params["after"], number(params.get("limit"), 100, 500))
        version, paginate = page_args(route, env, params)
        version, at, state = store.read(version)
        snap = state["snapshot"]
        if route == "/snapshot":
            data = snap
        elif route == "/activity":
            end = instant(snap["summary"]["day_start_at"])
            low = instant(params["from"]) if "from" in params else end - timedelta(days=1)
            high = instant(params["to"]) if "to" in params else end
            if high > end or low >= high:
                raise ValueError("历史区间须早于香港今日零点")
            if params.get("kind") not in (None, "buy", "sell", "settlement"):
                raise ValueError("交易类型无效")
            rows = [
                a
                for a in state["activity"]
                if low <= instant(a["time"]) < high
                and all(
                    a.get(k) == params[k]
                    for k in ("game_id", "channel", "kind", "position_id")
                    if k in params
                )
            ]
            rows.sort(key=lambda a: (instant(a["time"]), a["id"]), reverse=True)
            items, nxt = paginate(rows, version)
            data = {"items": items, "next_cursor": nxt}
        elif route == "/positions":
            view = params.get("view", "open")
            if view not in ("open", "history", "today"):
                raise ValueError("持仓视图无效")
            rows = (
                snap["open_positions"]
                if view == "open"
                else [
                    p
                    for p in state["positions"]
                    if p.get("has_exposure") and (view == "history" or p.get("active_today"))
                ]
            )
            rows = [
                p
                for p in rows
                if p.get("listed", True)
                and all(p.get(k) == params[k] for k in ("game_id", "channel") if k in params)
            ]
            sort = params.get(
                "sort",
                {"open": "abs_unrealized", "today": "abs_today", "history": "last_activity"}[view],
            )
            if sort not in (
                "abs_unrealized",
                "unrealized",
                "market_value",
                "abs_today",
                "last_activity",
            ):
                raise ValueError("排序无效")

            def key(p):
                if sort == "last_activity":
                    value = (
                        instant(p["last_activity_at"]).timestamp()
                        if p.get("last_activity_at")
                        else None
                    )
                else:
                    val = (
                        p.get("market_value")
                        if sort == "market_value"
                        else p[sort.removeprefix("abs_")]["value"]
                    )
                    value = None if val is None else Decimal(val)
                    if value is not None and sort.startswith("abs_"):
                        value = abs(value)
                return value is None, -value if value is not None else 0, p["id"]

            items, nxt = paginate(sorted(rows, key=key), version)
            data = {"items": items, "next_cursor": nxt}
        elif route.startswith("/positions/"):
            pid = route[len("/positions/") :]
            position = next((p for p in state["positions"] if p["id"] == pid), None)
            if not position:
                raise LookupError
            timeline, nxt = paginate(state["timeline"].get(pid, []), version)
            data = {"position": position, "timeline": timeline, "next_cursor": nxt}
        elif route == "/pnl/history":
            window = params.get("range", "today")
            if window not in ("today", "7d", "30d", "all"):
                raise ValueError("曲线时间范围无效")
            data = self.app.history(env, version, at, state, window)
        else:
            raise LookupError
        return version, at, data


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address, dashboard):
        self.dashboard = dashboard
        self.query = Query(dashboard)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logging.debug("dashboard http: " + fmt, *args)

    def send_body(self, status, mime, content):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == 503:
            self.send_header("Retry-After", "3")
        self.end_headers()
        self.wfile.write(content)

    def reply(self, status, body):
        self.send_body(status, "application/json; charset=utf-8", encode(body).encode())

    def error(self, status, code, msg):
        self.reply(status, dict(code=code, message=msg, retryable=status in (410, 503)))

    def do_GET(self):
        try:
            url = urlsplit(self.path)
            path = unquote(url.path)
            if not path.startswith(API + "/"):
                static = {
                    "/app.js": ("app.js", "text/javascript"),
                    "/styles.css": ("styles.css", "text/css"),
                }
                if path in static:
                    name, mime = static[path]
                elif path in ("/", "/games", "/games/", "/positions", "/activity") or path.startswith("/positions/"):
                    name, mime = "index.html", "text/html"
                else:
                    raise LookupError
                self.send_body(200, mime + "; charset=utf-8", (STATIC / name).read_bytes())
                return
            args = parse_qsl(url.query, keep_blank_values=True, max_num_fields=20)
            params = dict(args)
            if len(args) != len(params):
                raise ValueError("重复查询参数")
            env = params.get("env", "live")
            if env not in self.server.dashboard.stores:
                raise ValueError("未知环境")
            route = path[len(API) :]
            if route == "/stream":
                self.stream(env, params)
                return
            version, at, data = self.server.query.get(route, env, params)
            self.reply(200, {"env": env, "version": version, "as_of": at, "data": data})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except ResetRequired:
            self.error(410, "RESET_REQUIRED", "请重新加载当前快照")
        except (ValueError, OverflowError) as exc:
            self.error(400, "INVALID_ARGUMENT", str(exc))
        except LookupError:
            self.error(404, "NOT_FOUND", "未找到记录")
        except Exception:
            logging.exception("dashboard HTTP error")
            self.error(503, "UNAVAILABLE", "数据服务暂不可用")

    def stream(self, env, params):
        after = self.headers.get("Last-Event-ID") or params.get("after")
        if not after:
            raise ValueError("缺少起始版本")
        app = self.server.dashboard
        store = app.stores[env]
        # Validate syntax before sending headers; expired generations use an SSE reset frame.
        try:
            store.parse(after)
        except ResetRequired:
            pass
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.connection.settimeout(12)

        def send(name, data, version=None):
            frame = (
                f"id: {version}\n" if version else ""
            ) + f"event: {name}\ndata: {encode(data)}\n\n"
            self.wfile.write(frame.encode())
            self.wfile.flush()

        def heartbeat():
            ver, _, head = store.read()
            send(
                "heartbeat",
                dict(
                    env=env,
                    server_time=stamp(datetime.now(UTC)),
                    head_version=ver,
                    sources=head["snapshot"]["sources"],
                ),
            )

        try:
            while not app.stop_event.is_set():
                _, _, batch = store.changes(after, limit=100)
                if not batch["items"]:
                    heartbeat()
                    _, _, batch = store.changes(after, limit=100, wait=10)
                for change in batch["items"]:
                    send("change", change, change["version"])
                    after = change["version"]
        except ResetRequired as exc:
            send("reset", dict(env=env, reason=exc.reason, head_version=store.version()))
        except (OSError, sqlite3.Error):
            return


def options():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--live-ledger", type=Path)
    ap.add_argument("--paper-ledger", type=Path, default=ROOT / "var/executor/paper.db")
    ap.add_argument("--runner", type=Path, default=ROOT / "data/live_trade_final")
    ap.add_argument(
        "--no-runner", action="store_true", help="only read explicitly configured executor ledgers"
    )
    ap.add_argument("--attempts", type=Path, default=ROOT / "var/live_runner/attempts.db")
    ap.add_argument("--account", help="expected live ledger account")
    ap.add_argument("--cache", type=Path, default=ROOT / "var/dashboard/dashboard.db")
    ap.add_argument("--no-quotes", action="store_true", help="disable outbound price requests")
    return ap.parse_args()


def run(args):
    sources, stores = {}, {}
    for env, ledger in (("live", args.live_ledger), ("paper", args.paper_ledger)):
        runner = None if args.no_runner else args.runner
        attempts = args.attempts if runner and env == "live" else None
        account = args.account if env == "live" else None
        inputs = [ledger, runner, attempts]
        if any(path and path.resolve() == args.cache.resolve() for path in inputs):
            raise SystemExit("看板缓存须与交易源使用不同的路径")
        scope = hashlib.sha256(
            encode([env, *[str(p.resolve()) if p else None for p in inputs], account]).encode()
        ).hexdigest()
        sources[env] = Sources(
            ledger=ledger, runner=runner, attempts=attempts, env=env, account=account
        )
        stores[env] = VersionStore(args.cache, scope, env)
    app = Dashboard(sources, stores, QuoteFeed(not args.no_quotes), GamesFeed(),
                    MarketDirectory() if not args.no_quotes else None,
                    report_starts={"live": LIVE_REPORT_START})
    try:
        with WebServer((args.host, args.port), app) as server:
            app.start()
            logging.info("Daoxiang dashboard http://%s:%s/", *server.server_address)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.close()


def main():
    args = options()
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
    with account_lock(
        str(args.cache.resolve()), args.cache.resolve().parent / "locks", environment="dashboard"
    ):
        run(args)


if __name__ == "__main__":
    main()
