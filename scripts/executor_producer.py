"""Produce NBA strategy decisions and feed them to the local executor.

python -I scripts/executor_producer.py --recording data/live9_20261114.json --game GAME_ID
python -I scripts/executor_producer.py --scoreboard
"""

import argparse
import bisect
import hashlib
import json
import os
import sys
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import strategy as S
import nba_data as nd
from market_ws import MarketBooks
from relay_observer import RelayObserver

MICRO = 1_000_000
VERSION = "acd-2026-11-14"
TERMINAL = {"FILLED", "PARTIAL_FINAL", "REJECTED_ZERO", "EXPIRED_UNSENT"}


def micros(value):
    return int((Decimal(str(value)) * MICRO).to_integral_value(rounding=ROUND_HALF_UP))


class StrategyAPI:
    """Shared strategy operations for in-process and standalone clients."""

    def submit(self, body):
        return self.request("/v1/order-intents", body)

    def report(self, game):
        return self.request(f"/v1/games/{quote(game, safe='')}/report")

    def decisions(self, game):
        return self.request(f"/v1/games/{quote(game, safe='')}/decisions")

    def decision(self, body):
        return self.request("/v1/decisions", body)

    def intent(self, identifier):
        return self.request("/v1/order-intents/" + quote(identifier, safe=""))

    def finish(self, game):
        return self.request(f"/v1/games/{quote(game, safe='')}/finish", {})

    def verify(self):
        data = self.request("/v1/account")
        if (data["account"], data["environment"]) != (self.account, self.environment):
            raise ValueError("producer account or mode does not match executor")

    def base_budget(self):
        cash = self.request("/v1/account")["available_cash"]
        return int(cash * Decimal(str(S.BASE_STAKE_FRAC)))


class LocalExecutor(StrategyAPI):
    def __init__(self, url, account, environment):
        self.base = url.rstrip("/")
        parts = urlsplit(self.base)
        if parts.scheme != "http" or parts.hostname not in (
            "127.0.0.1",
            "localhost",
            "::1",
        ):
            raise ValueError("executor must be a loopback HTTP address")
        self.account = account.lower() if environment == "live" else account
        self.environment = environment
        token = os.environ.get("NBA_EXECUTOR_TOKEN")
        if not token:
            token = (
                (ROOT / "var" / "executor" / f"{environment}.db.token")
                .read_text()
                .strip()
            )
        if not token:
            raise ValueError("NBA_EXECUTOR_TOKEN is empty")
        self.token = token
        self.transport = build_opener(ProxyHandler({}))

    def request(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(
            self.base + path,
            data=data,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )
        with self.transport.open(req, timeout=10) as resp:
            return json.load(resp)


class GameSignals:
    """Tracks strategy history; the executor remains authoritative for all fills."""

    def __init__(self, game, market, pre, api, base=None):
        self.game, self.market, self.pre, self.api, self.base = (
            game,
            market,
            pre,
            api,
            base,
        )
        self.events = []
        self.opened = {}
        self.close_intents = {}
        self.closed = set()
        self.frozen = {}
        self.entry_elapsed = {}
        self.entry_wall = {}
        self.fill_elapsed = {}
        self.fill_source_wall = {}
        self.skipped = set()
        report = api.report(game)
        for row in report["intents"]:
            if row["action"] == "OPEN":
                self.opened[row["channel"]] = row["intent_id"]
            else:
                self.close_intents[row["channel"]] = row["intent_id"]
                if row["state"] == "FILLED":
                    self.closed.add(row["channel"])
        for d in api.decisions(game):
            ch = d["channel"]
            if d["result"] == "FILL_OBSERVED":
                self.fill_elapsed[ch] = d["inputs"]["entry_elapsed"]
                self.fill_source_wall[ch] = d["inputs"]["source_time"]
            if d["result"] == "REQUEST" and d["inputs"].get("action") == "OPEN":
                self.entry_elapsed[ch] = d["inputs"]["elapsed"]
                self.entry_wall[ch] = d["inputs"]["source_time"]
            if d["result"] == "REQUEST":
                payload = d["inputs"]["request"]
                self.frozen[ch, payload["action"]] = payload
            if (
                ch == "D"
                and d["result"] == "SKIP"
                and d["reason"] == "outside_entry_band"
            ):
                self.skipped.add(ch)

    def record(self, ch, sig_id, result, reason, inputs, intent=""):
        # Record IDs are tied to the input snapshot so a restarted reader can replay it.
        event = dict(
            game_id=self.game,
            at=int(inputs["source_time"] * 1000),
            channel=ch,
            signal_id=sig_id,
            version=VERSION,
            result=result,
            reason=reason,
            inputs=inputs,
            intent_id=intent,
        )
        event["source_id"] = hashlib.sha256(
            json.dumps(event, sort_keys=True).encode()
        ).hexdigest()
        self.api.decision(event)

    def issue(self, ch, action, reason, asset, price, amount, inputs):
        key = ch, action
        sig_id = f"{self.game}/{ch}/{action}"
        if key not in self.frozen:
            now = time.time_ns() // 1_000_000
            payload = dict(
                account=self.api.account,
                environment=self.api.environment,
                game_id=self.game,
                channel=ch,
                action=action,
                asset=asset,
                market=self.market,
                amount=amount,
                limit_price=price,
                signal_at=now,
                deadline=now + (15000 if action == "OPEN" else 900000),
                reason=reason,
            )
            if action == "CLOSE":
                payload["position_id"] = self.opened[ch]
            self.frozen[key] = payload
        payload = self.frozen[key]
        info = dict(inputs, action=action, request=payload)
        self.record(ch, sig_id, "REQUEST", reason, info)
        try:
            accepted = self.api.submit(payload)
        except HTTPError as exc:
            if exc.code not in (409, 422):
                raise
            self.record(
                ch,
                sig_id,
                "SKIP",
                f"executor_{exc.code}",
                dict(info, detail=exc.read(4096).decode("utf-8", "replace")),
            )
            return
        self.record(ch, sig_id, "ACCEPTED", reason, info, accepted["id"])
        if action == "OPEN":
            self.opened[ch] = accepted["id"]
            self.entry_elapsed[ch] = inputs["elapsed"]
            self.entry_wall[ch] = inputs["source_time"]
        else:
            self.close_intents[ch] = accepted["id"]

    def tick(self, elapsed, margin, wall, quotes, final=False, score=None):
        if not self.events or self.events[-1][:2] != (elapsed, margin):
            self.events.append((elapsed, margin, wall))
        for ch, rule in S.CHANNELS.items():
            asset, ask, bid = quotes.get(rule["side"], ("", None, None))
            inputs = dict(
                elapsed=elapsed,
                margin=margin,
                source_time=wall,
                score=score,
                pregame_price=self.pre,
                ask=ask,
                bid=bid,
            )
            key = f"{self.game}/{ch}/OPEN"
            if ch not in self.opened and ch not in self.skipped and not final:
                signal = (
                    S.process_signal if rule["style"] == "process" else S.settle_signal
                )(self.events, self.pre, rule)
                if signal is None:
                    continue
                if ch in ("A", "C") and elapsed >= S.FORCE_EL:
                    continue
                if not asset or ask is None:
                    self.record(ch, key, "SKIP", "no_quote", inputs)
                    continue
                effective = ask + S.entry_cost(ask)
                floor, cap = rule.get("price_min", S.PRICE_FLOOR), rule.get("price_max")
                inputs.update(
                    signal=signal,
                    effective_price=effective,
                    minimum=floor,
                    maximum=cap,
                    state_interval=rule["state"],
                    lookback_seconds=S.SIGNAL_WIN,
                    minimum_change=S.SIGNAL_K,
                    pregame_minimum=rule["pre_min"],
                )
                if effective < floor or (cap is not None and effective > cap):
                    self.record(ch, key, "SKIP", "outside_entry_band", inputs)
                    if ch == "D":
                        self.skipped.add(ch)
                    continue
                base = self.base if self.base is not None else self.api.base_budget()
                amount = int(base * rule["weight"])
                if amount < MICRO:
                    self.record(
                        ch,
                        key,
                        "SKIP",
                        "below_minimum_order",
                        dict(inputs, amount=amount),
                    )
                    continue
                self.issue(
                    ch, "OPEN", "score_signal", asset, micros(ask), amount, inputs
                )
                continue
            if (
                ch not in self.opened
                or ch in self.closed
                or wall < self.entry_wall.get(ch, wall)
            ):
                continue
            if not final and not bid:
                continue
            pos = self.api.intent(self.opened[ch])
            # Another fragment can arrive before the opening order is terminal.
            if pos["state"] not in TERMINAL or pos["filled"] <= 0:
                continue
            remaining = pos["filled"]
            if ch in self.close_intents:
                exit_order = self.api.intent(self.close_intents[ch])
                remaining -= exit_order["filled"]
                if remaining <= 0:
                    self.closed.add(ch)
                    continue
                if exit_order["state"] not in {
                    "PARTIAL_FINAL",
                    "REJECTED_ZERO",
                    "EXPIRED_UNSENT",
                }:
                    continue
                self.frozen.pop((ch, "CLOSE"), None)
            if ch not in self.fill_elapsed:
                # Map an actual fill back to its scoring snapshot. During accelerated
                # paper playback the simulated exchange and source clocks differ.
                first = pos.get("first_fill_at", 0) / 1000
                mapped = self.entry_elapsed.get(ch, elapsed)
                if not self.market.startswith(
                    "recorded/"
                ) and first >= self.entry_wall.get(ch, wall):
                    for game_elapsed, _, source_time in reversed(self.events):
                        if source_time <= first:
                            mapped = game_elapsed
                            break
                self.fill_elapsed[ch] = mapped
                self.fill_source_wall[ch] = wall
                self.record(
                    ch,
                    key,
                    "FILL_OBSERVED",
                    "confirmed_fill",
                    dict(inputs, entry_elapsed=mapped),
                    pos["id"],
                )
            if wall < self.fill_source_wall.get(ch, wall):
                continue
            entry = (pos["cost"] + pos["fee"]) / pos["filled"]
            reason = None
            if ch == "D":
                if margin < 0:
                    reason = "margin_stop"
                elif final:
                    self.record(
                        ch, key, "HOLD", "await_market_resolution", inputs, pos["id"]
                    )
            elif bid and bid - S.exit_cost(bid) >= entry + S.TP:
                reason = "target"
            elif (
                rule.get("game_hold")
                and elapsed - self.fill_elapsed[ch] >= rule["game_hold"]
            ):
                reason = "time"
            elif final or elapsed >= S.FORCE_EL:
                reason = "force"
            if reason and bid:
                self.issue(
                    ch,
                    "CLOSE",
                    reason,
                    pos.get("asset", asset),
                    micros(bid),
                    remaining,
                    inputs,
                )


def run_recording(api, path, game_id, base):
    with open(path, encoding="utf-8") as source:
        games = json.load(source)
    if game_id:
        games = [g for g in games if str(g["gid"]) == game_id]
        if not games:
            raise ValueError(f"game {game_id} missing in recording")
    for g in games:
        game = str(g["gid"])
        producer = GameSignals(game, f"recorded/{game}", g["pre"], api, base)
        fav = g["fav"]
        dog = next(name for name in g["outcomes"] if name != fav)
        histories = {
            name: sorted((p[0], p[1]) for p in g["prices"][name]) for name in (fav, dog)
        }
        events = g["evs"]
        for i, (elapsed, margin, wall) in enumerate(events):
            quotes = {}
            for side, name in (("fav", fav), ("dog", dog)):
                history = histories[name]
                idx = bisect.bisect_right(history, (wall, float("inf"))) - 1
                if idx >= 0:
                    price = history[idx][1]
                    quotes[side] = (f"recorded/{game}/{name}", price, price)
            previous_orders = set(producer.opened.values()) | set(producer.close_intents.values())
            producer.tick(elapsed, margin, wall, quotes, final=i == len(events) - 1)
            # The source clock is accelerated. Book entries AND exit proceeds
            # before sizing orders at the next historical snapshot.
            orders = set(producer.opened.values()) | set(producer.close_intents.values())
            for identifier in orders - previous_orders:
                deadline = time.monotonic() + 30
                while api.intent(identifier)["state"] not in TERMINAL:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"paper order {identifier} pending")
                    time.sleep(0.1)
        report = api.finish(game)
        until = time.monotonic() + 30
        while report["status"] == "PENDING" and time.monotonic() < until:
            time.sleep(0.2)
            report = api.report(game)
        print(json.dumps(report, ensure_ascii=False))


def public_quote(token):
    book = nd.http_json(
        "https://clob.polymarket.com/book?" + urlencode({"token_id": token})
    )
    asks = [float(row["price"]) for row in book.get("asks", ())]
    bids = [float(row["price"]) for row in book.get("bids", ())]
    return str(book["market"]), (
        str(token),
        min(asks, default=None),
        max(bids, default=None),
    )


def run_scoreboard(api, base, interval, books=None, relay=None):
    # These functions read public ESPN and Polymarket endpoints. This process
    # never constructs a legacy GameTrader or imports account credentials.
    from live_runner import scoreboard, resolve, outcome_abbr

    tracked = {}
    finished = set()
    while True:
        for snap in scoreboard():
            game = str(snap["gid"])
            score_seen = time.monotonic()
            final = snap["state"] == "post"
            if relay is not None and snap["state"] == "in":
                relay.subscribe(game)
            if final and game in finished:
                continue
            if game not in tracked:
                if snap["state"] != "in" and not (
                    final and api.report(game)["intents"]
                ):
                    if final:
                        api.finish(game)
                        finished.add(game)
                    continue
                market = resolve(snap, None)
                if not market or market.get("pre") is None:
                    continue
                by_side = {
                    "fav" if name == market["fav"] else "dog": token
                    for name, token in zip(market["outcomes"], market["tokens"])
                }
                if len(by_side) != 2:
                    continue
                home = outcome_abbr(market, snap, snap["home"])
                if not home:
                    continue
                condition, _ = public_quote(by_side["fav"])
                if books is not None:
                    for token in by_side.values():
                        books.subscribe(token, condition)
                    books.start()
                tracked[game] = (
                    GameSignals(game, condition, market["pre"], api, base),
                    by_side,
                    home == market["fav"],
                )
                if relay is not None:
                    relay.register_market(game, by_side, books)
            producer, tokens, home_fav = tracked[game]
            elapsed = nd.elapsed_of(snap["period"], snap["detail"])
            if elapsed is None:
                if not final:
                    continue
                elapsed = max(2880, producer.events[-1][0] if producer.events else 0)
            quotes = {}
            for side, token in tokens.items():
                try:
                    market, levels = (books.quote(token, producer.market) if books is not None
                                      else public_quote(token))
                except (URLError, TimeoutError):
                    continue
                if market != producer.market:
                    raise ValueError("token no longer matches the resolved condition")
                quotes[side] = levels
            home, away = snap["home_score"], snap["away_score"]
            if relay is not None:
                relay.observe_http(snap, quotes, score_seen)
            wall = time.time()
            producer.tick(
                elapsed,
                (home - away) * (1 if home_fav else -1),
                wall,
                quotes,
                final=final,
                score={"home": home, "away": away},
            )
            if final:
                report = api.finish(game)
                # A submitted buy can confirm after the final score. Keep
                # monitoring an A/C position until its forced exit is booked.
                outstanding = any(
                    row["channel"] in ("A", "C")
                    and (
                        row["state"] not in TERMINAL
                        or row.get("remaining_shares", 0) > 0
                    )
                    for row in report["intents"]
                )
                if not outstanding:
                    finished.add(game)
                    tracked.pop(game)
        time.sleep(interval)


def follow_scoreboard(api, base=None, interval=3, market_data="http", score_data="http"):
    books = MarketBooks(market_data, http_quote=public_quote) if market_data != "http" else None
    relay = None
    if score_data == "observe":
        origin = os.getenv("NBA_BRIGHTSPORTS_URL")
        if not origin:
            raise ValueError("set NBA_BRIGHTSPORTS_URL for score observation")
        relay = RelayObserver(origin)
    try:
        while True:
            try:
                run_scoreboard(api, base, interval, books, relay)
            except (TimeoutError, URLError) as exc:
                if isinstance(exc, HTTPError) and exc.code in (400, 401, 403):
                    raise
                print(
                    f"Input interrupted ({type(exc).__name__}), restoring from executor",
                    file=sys.stderr,
                )
                time.sleep(interval)
    finally:
        if books:
            books.stop()
        if relay:
            relay.stop()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--recording", help="Recorded game array (paper executor only)")
    group.add_argument(
        "--scoreboard",
        action="store_true",
        help="Poll public ESPN scores and Polymarket books",
    )
    p.add_argument("--game", help="Filter a recording by game ID")
    p.add_argument("--url", default="http://127.0.0.1:8787")
    p.add_argument("--account", default="local")
    p.add_argument("--environment", choices=["paper", "live"], default="paper")
    p.add_argument(
        "--base-usd", type=Decimal, help="Optional paper-only base stake in dollars"
    )
    p.add_argument("--interval", type=float, default=3)
    p.add_argument("--market-data", choices=("http", "observe", "ws"), default="http")
    p.add_argument("--score-data", choices=("http", "observe"), default="http")
    args = p.parse_args()
    if args.environment == "live" and (args.recording or args.base_usd is not None):
        p.error("recordings and --base-usd are paper-only")
    if args.recording and (args.market_data != "http" or args.score_data != "http"):
        p.error("market and score data modes require --scoreboard")
    if args.interval <= 0 or (args.base_usd is not None and args.base_usd <= 0):
        p.error("stake and interval must be positive")
    api = LocalExecutor(args.url, args.account, args.environment)
    api.verify()
    base = micros(args.base_usd) if args.base_usd is not None else None
    if args.recording:
        run_recording(api, args.recording, args.game, base)
    else:
        follow_scoreboard(api, base, args.interval, args.market_data, args.score_data)


if __name__ == "__main__":
    main()
