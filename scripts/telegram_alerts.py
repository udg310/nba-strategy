"""Optional Telegram notices for reconciled fills and execution review."""

import json
import logging
import os
import threading
import time
from urllib.request import Request, ProxyHandler, build_opener


class TelegramAlerts:
    def __init__(self, ledger, reports, token=None, chat_id=None):
        self.ledger, self.reports = ledger, reports
        self.token = token or os.getenv("NBA_TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("NBA_TELEGRAM_CHAT_ID")
        if not self.token or not self.chat_id:
            raise ValueError("set both NBA_TELEGRAM_BOT_TOKEN and NBA_TELEGRAM_CHAT_ID")
        proxy = os.getenv("NBA_PROXY", "http://127.0.0.1:9567")
        self.transport = build_opener(ProxyHandler({"https": proxy} if proxy else {}))
        self.stop_event = threading.Event()
        self.thread = None
        self.last_review = ""
        with ledger.tx() as db:
            first_use = db.execute("SELECT 1 FROM sqlite_master WHERE name='telegram_sent'").fetchone() is None
            db.execute("""CREATE TABLE IF NOT EXISTS telegram_sent (
                order_id TEXT NOT NULL, fill_id TEXT NOT NULL,
                PRIMARY KEY(order_id, fill_id))""")
            if first_use:
                # Existing history predates Telegram configuration. Only alert new fills.
                db.execute("""INSERT OR IGNORE INTO telegram_sent
                    SELECT order_id,id FROM external_fills WHERE confirmed=1""")

    def send(self, message):
        request = Request("https://api.telegram.org/bot" + self.token + "/sendMessage",
                          data=json.dumps({"chat_id": self.chat_id, "text": message}).encode(),
                          headers={"Content-Type": "application/json"})
        with self.transport.open(request, timeout=8) as response:
            body = json.load(response)
        if not body.get("ok"):
            raise RuntimeError("Telegram rejected alert")

    def _pending(self):
        with self.ledger.tx() as db:
            return [dict(row) for row in db.execute("""
                SELECT f.order_id, f.id AS fill_id, f.quantity, f.price, f.side,
                       i.game_id, i.channel, i.action
                FROM external_fills f JOIN attempts a ON a.order_id=f.order_id
                JOIN intents i ON i.id=a.intent_id
                LEFT JOIN telegram_sent s ON s.order_id=f.order_id AND s.fill_id=f.id
                WHERE f.confirmed=1 AND s.fill_id IS NULL
                ORDER BY f.time, f.id LIMIT 30""")]

    def tick(self):
        for row in self._pending():
            self.send(f"NBA 成交确认 | 比赛 {row['game_id']} | {row['channel']} {row['action']} "
                      f"| {row['side']} {row['quantity'] / 1_000_000:.6f} 份 "
                      f"@ ${row['price'] / 1_000_000:.4f} | 订单 {row['order_id']}")
            with self.ledger.tx() as db:
                db.execute("INSERT OR IGNORE INTO telegram_sent VALUES(?,?)",
                           (row["order_id"], row["fill_id"]))
        review = self.reports.alert_summary(time.time_ns() // 1_000_000)
        if review and review != self.last_review:
            self.send("NBA 执行需人工核查 | " + review)
        self.last_review = review

    def _run(self):
        while not self.stop_event.wait(3):
            try:
                self.tick()
            except Exception as exc:
                # Never print the URL: it contains the bot token.
                logging.warning("Telegram alert delivery: %s", type(exc).__name__)

    def start(self):
        self.thread = threading.Thread(target=self._run, name="telegram-alerts", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)


if __name__ == "__main__":
    # A credential check without starting the strategy or touching a ledger.
    token, chat = os.getenv("NBA_TELEGRAM_BOT_TOKEN"), os.getenv("NBA_TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise SystemExit("set NBA_TELEGRAM_BOT_TOKEN and NBA_TELEGRAM_CHAT_ID")
    notifier = TelegramAlerts.__new__(TelegramAlerts)
    notifier.token, notifier.chat_id = token, chat
    proxy = os.getenv("NBA_PROXY", "http://127.0.0.1:9567")
    notifier.transport = build_opener(ProxyHandler({"https": proxy} if proxy else {}))
    try:
        notifier.send("NBA 告警测试：Telegram 已连接。")
    except Exception as exc:
        raise SystemExit(f"Telegram test failed: {type(exc).__name__}") from None
    print("Telegram test alert sent")
