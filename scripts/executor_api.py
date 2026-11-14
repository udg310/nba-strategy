"""Loopback operator API and direct access for the in-process strategy."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import hmac
import json
import logging
import socket
import threading
import time
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit

from executor_ledger import Conflict, Invalid, Risk
from executor_producer import StrategyAPI


def route_parts(path):
    return [unquote(part) for part in urlsplit(path).path.lstrip("/").split("/")]


class ExecutorAPI(StrategyAPI):
    def __init__(self, ledger, reports, engine):
        self.ledger = ledger
        self.reports = reports
        self.engine = engine
        self.account = ledger.account
        self.environment = ledger.environment
        self.counter_lock = threading.Lock()

    def dispatch(self, method, parts, body):
        now = time.time_ns() // 1_000_000
        if method == "GET" and parts == ["healthz"]:
            with self.ledger.tx() as db:
                db.execute("SELECT 1").fetchone()
            return 200, {"status": "ready", "environment": self.environment}
        if method == "POST" and parts == ["v1", "order-intents"]:
            req = dict(body)
            if req.get("account") in (None, ""):
                req["account"] = self.account
            if req.get("environment") in (None, ""):
                req["environment"] = self.environment
            try:
                intent, fresh = self.ledger.create(req, now)
            except Risk:
                raise
            except Conflict:
                with self.counter_lock:
                    self.reports.conflicts += 1
                raise
            if not fresh:
                with self.counter_lock:
                    self.reports.duplicates += 1
            return (201 if fresh else 200), intent
        if method == "POST" and parts == ["v1", "settlements"]:
            fresh = self.ledger.settle(body, now)
            return (201 if fresh else 200), {"position_id": body["position_id"], "recorded": True}
        if method == "POST" and parts == ["v1", "reconcile"]:
            self.engine.reconcile()
            return 200, {"status": "checked"}
        return self.reports.dispatch(method, parts, body)

    def request(self, path, body=None):
        """The built-in strategy uses the same operations without a socket."""
        try:
            _, result = self.dispatch("GET" if body is None else "POST", route_parts(path), body)
        except (Risk, Conflict) as exc:
            # The strategy handles rejected requests the same way on either transport.
            detail = BytesIO(json.dumps({"error": str(exc)}).encode())
            raise HTTPError(path, 409, str(exc), {}, detail) from exc
        return result


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(15)
        super().setup()

    def respond(self, status, data):
        raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def handle_request(self):
        parts = route_parts(self.path)
        if parts != ["healthz"] and not hmac.compare_digest(
            self.headers.get("Authorization", "").encode(), self.server.authorization
        ):
            self.respond(401, {"error": "bearer token required"})
            return
        try:
            size = int(self.headers.get("Content-Length", 0))
            limit = (64 if parts in (["v1", "decisions"], ["v1", "review", "resolve"]) else 128) * 1024
            if not 0 <= size <= limit or self.headers.get("Transfer-Encoding"):
                raise Invalid("invalid request body size or encoding")
            body = json.loads(self.rfile.read(size)) if size else {}
            if not isinstance(body, dict):
                raise Invalid("expected one JSON object")
            status, data = self.server.api.dispatch(self.command, parts, body)
        except ValueError as exc:
            status, data = 400, {"error": str(exc)}
        except (Risk, Conflict) as exc:
            status, data = 409, {"error": str(exc)}
        except KeyError:
            status, data = 404, {"error": "not found"}
        except Exception:
            logging.exception("executor request failed")
            status, data = 500, {"error": "ledger operation failed"}
        self.respond(status, data)

    do_GET = handle_request
    do_POST = handle_request

    def log_message(self, fmt, *args):
        # Diagnostic paths are already represented by decisions and order timelines.
        pass


class Server(ThreadingHTTPServer):
    # Finish in-flight ledger requests before releasing the account lock.
    daemon_threads = False


class IPv6Server(Server):
    address_family = socket.AF_INET6


@contextmanager
def serve(api, address, token):
    kind = IPv6Server if ":" in address[0] else Server
    with kind(address, Handler) as server:
        server.api = api
        server.authorization = ("Bearer " + token).encode()
        thread = threading.Thread(target=server.serve_forever, name="operator-api")
        thread.start()
        try:
            yield server.server_address
        finally:
            server.shutdown()
            thread.join()
