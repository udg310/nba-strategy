"""Dispatch durable intents and reconcile account activity in the same process."""

from concurrent.futures import ThreadPoolExecutor
import logging
import threading
import time

from executor_ledger import Risk


def now_ms():
    return time.time_ns() // 1_000_000


class Engine:
    def __init__(self, ledger, reports, venue, now=now_ms):
        self.ledger = ledger
        self.reports = reports
        self.venue = venue
        self.now = now
        self._feed = threading.Lock()
        self._cycle = threading.Lock()
        self._last_feed = 0
        self._stop = getattr(venue, "stop_requested", threading.Event())
        self._threads = []

    def recover(self):
        for intent in self.ledger.pending():
            if intent["state"] == "SUBMITTING":
                self.ledger.record_submit(intent["attempt_id"], {
                    "state": "UNKNOWN",
                    "reason": "previous sender exited before recording a receipt",
                }, self.now())
        self.reconcile()

    def _lookup(self, intent):
        if intent["state"] in {"SUBMITTING", "ACCEPTED", "UNKNOWN", "PARTIAL"}:
            snapshot = self.venue.lookup(intent)
            self.ledger.apply(intent["attempt_id"], snapshot, self.now())

    def step(self, intent_id):
        intent = self.ledger.get(intent_id)
        if intent["state"] != "READY":
            self._lookup(intent)
            return
        try:
            intent, claimed = self.ledger.claim(intent_id, self.now())
        except Risk:
            return
        if not claimed:
            return
        # The claim and reservation have committed before any exchange call.
        try:
            result = self.venue.submit(intent)
        except Exception:
            result = {"state": "UNKNOWN", "reason": "submission outcome unavailable"}
        if result.get("state") not in {"ACCEPTED", "REJECTED_ZERO"}:
            result = {**result, "state": "UNKNOWN"}
        self.ledger.record_submit(intent["attempt_id"], result, self.now())

    def _sync_feed(self):
        with self._feed:
            try:
                cursor = self.reports.cursor()
                for _ in range(128):
                    if self._stop.is_set():
                        raise RuntimeError("account reconciliation stopped")
                    fills, following = self.venue.trades(cursor)
                    self.reports.observe(fills, following)
                    if not following or cursor == following:
                        self._last_feed = self.now()
                        return
                    cursor = following
                raise RuntimeError("account trade pagination is still catching up")
            except Exception:
                self._last_feed = 0
                raise

    def reconcile(self):
        """Read account activity and existing attempts, even when the feed fails."""
        with self._cycle:
            first_error = None
            try:
                self._sync_feed()
            except Exception as exc:
                first_error = exc
            with ThreadPoolExecutor(max_workers=4) as workers:
                jobs = [workers.submit(self._lookup, intent) for intent in self.ledger.pending()
                        if intent["state"] != "READY"]
                for job in jobs:
                    try:
                        job.result()
                    except Exception as exc:
                        first_error = first_error or exc
            self.reports.refresh()
            if first_error is not None:
                raise first_error

    def _poll_feed(self):
        last_alert = ""
        while not self._stop.wait(2):
            try:
                self._sync_feed()
                self.reports.refresh()
                alert = self.reports.alert_summary(self.now())
                if alert and alert != last_alert:
                    logging.warning("execution requires review: %s", alert)
                last_alert = alert
            except Exception as exc:
                if not self._stop.is_set():
                    logging.warning("account reconciliation: %s", exc)

    def _dispatch(self):
        active = {}
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="order") as workers:
            while not self._stop.wait(.5):
                for intent_id, job in list(active.items()):
                    if job.done():
                        del active[intent_id]
                        try:
                            job.result()
                        except Exception as exc:
                            logging.warning("order processing %s: %s", intent_id, exc)
                if len(active) >= 4:
                    continue
                try:
                    items = self.ledger.pending()
                except Exception as exc:
                    logging.warning("read execution queue: %s", exc)
                    continue
                for intent in items:
                    if len(active) == 4 or self._stop.is_set():
                        break
                    intent_id = intent["id"]
                    if intent_id in active:
                        continue
                    last = self._last_feed
                    if intent["state"] == "READY" and (not last or self.now() - last > 30_000):
                        continue
                    active[intent_id] = workers.submit(self.step, intent_id)

    def start(self):
        if self._threads:
            return
        self._stop.clear()
        for run in (self._poll_feed, self._dispatch):
            thread = threading.Thread(target=run, name=run.__name__)
            thread.start()
            self._threads.append(thread)

    def stop(self):
        self._stop.set()
        for thread in self._threads:
            thread.join()
        self._threads.clear()
