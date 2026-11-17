"""Collect read-only inputs and project the website's queryable state."""

import bisect
import logging
import threading
import time
from datetime import datetime, timedelta

from dashboard_games import team_name
from dashboard_model import baselines, game_results, project, reporting_period
from dashboard_store import HK, UTC, instant, stamp


class Dashboard:
    def __init__(self, sources, stores, quotes, games, markets=None, report_starts=None):
        self.sources, self.stores, self.quotes, self.games = sources, stores, quotes, games
        self.markets = markets
        self.report_starts = report_starts or {}
        self.stop_event = threading.Event()
        self.thread = None
        self.previous_inputs = {}
        self.next_projection = {}
        self.last_quotes = {}
        self.curves = {}
        self.cache_lock = threading.Lock()

    def poll(self, env, now=None, *, publish=True):
        raw = self.sources[env].read()
        if env in self.report_starts:
            raw = reporting_period(raw, self.report_starts[env])
        # Take the time after reading the feeds so their newest observations are eligible.
        quotes, quote_source = self.quotes.read(env)
        fixtures, games_source = self.games.read()
        now = now or datetime.now(UTC)
        raw = dict(
            raw,
            sources=[*raw["sources"], quote_source],
            account_label=raw.get("account_label")
            or ("策略实盘" if env == "live" else "策略模拟盘"),
        )
        store = self.stores[env]
        if env not in self.previous_inputs and store.head:
            # Preserve a recent observation on restart. The model still checks
            # both market and collection timestamps against its 15-second limit.
            quotes = {**store.quotes_at(now), **quotes}
        if (
            not fixtures
            and not games_source["last_success_at"]
            and games_source["status"] != "ok"
            and store.head
        ):
            snap = store.head["snapshot"]
            fixtures = [
                {k: g.get(k) for k in ("game_id", "game_label", "home", "away", "updated_at")}
                for g in snap.get("live_games", [])
            ]
            previous_score = snap.get("games_source") or {}
            games_source = dict(
                games_source,
                last_success_at=previous_score.get("last_success_at"),
                last_event_at=previous_score.get("last_event_at"),
            )
        # Retain the first observed correction timestamp across subsequent reads and process restarts.
        previous_rows = store.objects["activity"]
        identities = [*raw["activity"], *raw["meta"].values()]
        known = {}
        saved = [] if env in self.previous_inputs else [*previous_rows.values(), *store.objects["position"].values()]
        for r in [*saved, *identities]:
            title = r.get("game_label") or ""
            for sep in (" 对 ", " vs ", " @ "):
                if sep in title and r.get("game_id"):
                    known[r["game_id"]] = " 对 ".join(team_name(t) for t in title.split(sep))
                    break
        names = self.games.matchups(known, {r.get("game_id") for r in identities})
        saved_markets = (store.head or {}).get("origin", {}).get("markets", {})
        market_days = {r["game_id"]: None for r in identities if r.get("game_id") and r.get("market") == "胜负"}
        for r in raw["activity"]:
            if r.get("game_id") in market_days and market_days[r["game_id"]] is None:
                market_days[r["game_id"]] = instant(r["time"]).date()
        markets = self.markets.read(market_days, saved_markets) if self.markets else saved_markets
        raw["markets"] = markets
        raw["payouts"] = {asset: payout for m in markets.values() for asset, payout in m["payouts"].items()}

        def named(row):
            gid = row.get("game_id")
            title = names.get(gid) or row.get("game_label")
            item = dict(row, game_label=title if title and title != gid and not title.isdigit() else None)
            if not item.get("asset_id") and item.get("market") == "胜负" and gid in markets:
                item["asset_id"] = markets[gid]["assets"].get(item.get("outcome"))
            return item

        raw["meta"] = {pid: named(m) for pid, m in raw["meta"].items()}

        def revision(row):
            old = previous_rows.get(row["id"])
            if not old or row.get("revised_at"):
                return row
            if all(old.get(k) == v for k, v in row.items() if k not in ("revised_at", "game_label")):
                return dict(row, revised_at=old["revised_at"]) if old.get("revised_at") else row
            return dict(row, revised_at=stamp(now))

        raw["activity"] = [revision(named(row)) for row in raw["activity"]]
        midnight = now.astimezone(HK).replace(hour=0, minute=0, second=0, microsecond=0)
        previous = self.previous_inputs.get(env)
        if previous:
            # Reading a quiet database successfully does not change its financial state.
            keys = {r["name"]: r for r in previous[0]["sources"]}

            def sample(source, old):
                if (
                    old
                    and source.get("last_success_at")
                    and old.get("last_success_at")
                    and all(
                        source.get(k) == old.get(k) for k in ("status", "reason", "last_event_at")
                    )
                    and (
                        instant(source["last_success_at"]) - instant(old["last_success_at"])
                    ).total_seconds()
                    < 10
                ):
                    return dict(source, last_success_at=old["last_success_at"])
                return source

            raw["sources"] = [sample(r, keys.get(r["name"])) for r in raw["sources"]]
            games_source = sample(games_source, previous[3])
        inputs = (raw, quotes, fixtures, games_source, midnight.date())
        if publish and previous == inputs:
            deadline = self.next_projection.get(env)
            if deadline is None or now < deadline:
                return
        opening = baselines(raw, store.quotes_at(midnight), midnight)
        view = project(raw, quotes, now, opening)
        snap = view["snapshot"]
        summaries = game_results(raw, view)
        snap["live_games"] = [
            dict(game, **summaries.get(game["game_id"], summaries[None])) for game in fixtures
        ]
        snap["games_source"] = games_source
        if not publish:
            return view
        self.quotes.watch(env, [p.get("asset_id") for p in view["valuation_positions"] if p["quote"]["status"] != "resolved"])
        prior = self.last_quotes.get(env, {})
        changes = {k: v for k, v in quotes.items() if prior.get(k) != v}
        origin = {
            k: raw.get(k) for k in ("known_origin", "coverage_from", "account_label", "currency", "payouts", "markets")
        }
        if raw.get("report_from"):
            origin.update(report_from=raw["report_from"], opening_trades=raw["opening_trades"])
        store.publish(view, origin, changes, now)
        self.previous_inputs[env] = inputs
        self.last_quotes[env] = quotes
        deadlines = [
            instant(p["quote"]["valid_until"])
            for p in view["valuation_positions"]
            if p["quote"].get("valid_until") and p["quote"]["status"] == "fresh"
        ]
        # A source clock can be slightly ahead of the collector; reconsider future rows at their timestamp.
        deadlines.extend(instant(a["time"]) for a in raw["activity"] if instant(a["time"]) > now)
        self.next_projection[env] = min(
            (d + timedelta(milliseconds=1) for d in deadlines if d >= now), default=None
        )

    def start(self):
        for env in self.stores:
            self.poll(env)
        self.thread = threading.Thread(target=self._work, name="dashboard-ingest", daemon=True)
        self.thread.start()

    def _work(self):
        while not self.stop_event.is_set():
            start = time.monotonic()
            for env in self.stores:
                try:
                    self.poll(env)
                except Exception:
                    logging.exception("看板采集失败 (%s)", env)
            self.stop_event.wait(max(0.05, 0.5 - (time.monotonic() - start)))

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()
        self.games.close()
        if self.markets:
            self.markets.close()
        self.quotes.close()
        for source in self.sources.values():
            source.close()
        for store in self.stores.values():
            store.close()

    def history(self, env, version, as_of, state, window):
        now = instant(as_of)
        snapshot = state["snapshot"]
        midnight = now.astimezone(HK).replace(hour=0, minute=0, second=0, microsecond=0)
        sizes = {"today": 60, "7d": 900, "30d": 3600, "all": 86400}
        step = sizes[window]
        activity = sorted(state["activity"], key=lambda a: instant(a["time"]))
        if window == "all":
            origin = snapshot.get("coverage_from") or (activity[0]["time"] if activity else as_of)
            start = (
                instant(origin).astimezone(HK).replace(hour=0, minute=0, second=0, microsecond=0)
            )
        else:
            start = midnight - timedelta(days={"today": 0, "7d": 6, "30d": 29}[window])
        if snapshot.get("report_from"):
            floor = instant(snapshot["report_from"])
            start = max(start, floor)
            if window == "all" and not snapshot.get("coverage_from"):
                start = floor
        cache_key = (env, window, version)
        with self.cache_lock:
            if cache_key in self.curves:
                return self.curves[cache_key]
        seq = self.stores[env].parse(version)
        times = [instant(a["time"]) for a in activity]
        raw = dict(state["origin"], timeline={}, meta={}, sources=[], available_cash=None)
        adjustments = [(pid, e) for pid, items in state["timeline"].items() for e in items
                       if e.get("state") in ("reconcile_closed", "reconcile_add")]
        pos = 0
        tick = start
        prior_key = measures = None
        points = []
        while tick < now:
            end = min(tick + timedelta(seconds=step), now)
            if end == now:
                summary = snapshot["summary"]
                measures = {k: summary[k] for k in ("total", "realized", "unrealized")}
            else:
                # Snapshot just before the exclusive bucket end; midnight executions belong to the next day.
                t = end - timedelta(microseconds=1)
                pos = bisect.bisect_right(times, t, lo=pos)
                quotes = self.stores[env].quotes_at(t, seq)
                coverage = raw.get("coverage_from")
                before_coverage = coverage and t < instant(coverage)
                # Empty books and expired books can be reused over consecutive unchanged buckets.
                resolved = tuple(k for k, v in (raw.get("payouts") or {}).items() if instant(v["effective_at"]) <= t)
                observed = [(pid, e) for pid, e in adjustments if instant(e["time"]) <= t]
                key = (pos, bool(before_coverage), resolved, len(observed)) if not quotes else None
                if key is None or key != prior_key or measures is None:
                    if before_coverage or (not coverage and pos == 0):
                        no = {
                            "value": None,
                            "known_subtotal": None,
                            "status": "unavailable",
                            "covered_count": 0,
                            "total_count": 0,
                            "issues": [
                                {"code": "source_gap", "position_id": None, "activity_id": None}
                            ],
                        }
                        measures = {k: no for k in ("total", "realized", "unrealized")}
                    else:
                        observations, flags = {}, {}
                        for pid, e in observed:
                            observations.setdefault(pid, []).append(e)
                            code = "balance_adjustment" if e["state"] == "reconcile_add" else "balance_disappeared"
                            flags.setdefault(pid, {"issues": []})["issues"].append(dict(code=code, position_id=pid))
                        then = project(dict(raw, activity=activity[:pos], timeline=observations, meta=flags), quotes, t, baseline={})[
                            "snapshot"
                        ]["summary"]
                        measures = {k: then[k] for k in ("total", "realized", "unrealized")}
                prior_key = key
            points.append(dict(measures, at=stamp(end)))
            tick = end
        result = {
            "range": window,
            "from": stamp(start),
            "to": stamp(now),
            "bucket_seconds": step,
            "points": points,
        }
        with self.cache_lock:
            if len(self.curves) >= 10:
                self.curves.pop(next(iter(self.curves)))
            self.curves[cache_key] = result
        return result
