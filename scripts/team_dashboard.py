# -*- coding: utf-8 -*-
"""Read-only team board for the live runner.

Data comes only from what the runner already writes:
  data/live_trade_final/<UTC date>/<run>/events.jsonl   (trades, heartbeats)
The board shows the current wallet balance, today's P&L, open positions and
today's trades. No executor ledger, no writes, no credentials.

    python scripts/team_dashboard.py --host 127.0.0.1 --port 8790
"""
import argparse
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HK = timezone(timedelta(hours=8))
REFRESH = 9.0          # seconds between scans
AUTH = ""              # "user:pass"; empty = no auth (local only)


def _hk(ts):
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).astimezone(HK)
    except (TypeError, ValueError):
        return None


def read_events(runner_dir, days=3):
    base = Path(runner_dir)
    if not base.is_dir():
        return []
    today = datetime.now(timezone.utc).date()
    rows = []
    for off in range(days):
        day = base / (today - timedelta(days=off)).isoformat()
        if not day.is_dir():
            continue
        for run in sorted(day.iterdir()):
            fp = run / "events.jsonl"
            if not fp.is_file():
                continue
            try:
                text = fp.read_text(errors="ignore")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("t") is not None:
                    rows.append(r)
    rows.sort(key=lambda r: r.get("t") or 0)
    return rows


def build_state(runner_dir, now=None):
    now = now or datetime.now(timezone.utc)
    midnight = now.astimezone(HK).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = read_events(runner_dir)

    # latest wallet balance + the balance at the start of today (HK)
    balance = None
    day_start = None
    for r in rows:
        if r.get("ev") in ("start", "heartbeat", "stopped") and r.get("balance") is not None:
            try:
                balance = float(r["balance"])
            except (TypeError, ValueError):
                continue
            t = _hk(r.get("t"))
            if day_start is None and t is not None and t >= midnight:
                day_start = balance
    if day_start is None:
        day_start = balance

    # open positions: bought - sold per (game, channel)
    pos = {}
    for r in rows:
        gid, ch = r.get("gid"), r.get("ch")
        if not gid or not ch:
            continue
        k = (str(gid), ch)
        if r.get("ev") == "buy":
            try:
                qty = float(r.get("filled_shares") or 0)
                entry = float(r.get("entry") or 0)
            except (TypeError, ValueError):
                continue
            if qty <= 0:
                continue
            p = pos.setdefault(k, {"gid": k[0], "ch": ch, "outcome": r.get("outcome"),
                                   "qty": 0.0, "cost": 0.0})
            p["qty"] += qty
            p["cost"] += qty * entry
            if r.get("outcome"):
                p["outcome"] = r.get("outcome")
        elif r.get("ev") == "sell":
            p = pos.get(k)
            if not p:
                continue
            try:
                sold = float(r.get("sold_shares") or 0)
            except (TypeError, ValueError):
                sold = 0.0
            avg = p["cost"] / p["qty"] if p["qty"] else 0.0
            p["qty"] = max(0.0, p["qty"] - sold)
            p["cost"] = max(0.0, p["cost"] - sold * avg)
    open_pos = []
    for p in pos.values():
        if p["qty"] > 0.01:
            open_pos.append({"gid": p["gid"], "ch": p["ch"], "outcome": p["outcome"],
                             "qty": round(p["qty"], 2), "cost": round(p["cost"], 2),
                             "avg": round(p["cost"] / p["qty"], 3) if p["qty"] else 0})
    open_pos.sort(key=lambda x: (x["gid"], x["ch"]))

    # today's trades
    trades = []
    for r in rows:
        if r.get("ev") not in ("buy", "sell"):
            continue
        t = _hk(r.get("t"))
        if t is None or t < midnight:
            continue
        if r.get("ev") == "buy":
            qty, px = r.get("filled_shares"), r.get("entry")
        else:
            qty, px = r.get("sold_shares"), r.get("exit")
        trades.append({"time": t.strftime("%H:%M"), "gid": str(r.get("gid") or ""),
                       "ch": r.get("ch"), "outcome": r.get("outcome"),
                       "kind": r.get("ev"), "qty": round(float(qty or 0), 2),
                       "price": round(float(px or 0), 3)})
    trades.reverse()

    return {
        "as_of": now.astimezone(HK).strftime("%Y-%m-%d %H:%M:%S"),
        "balance": round(balance, 2) if balance is not None else None,
        "day_start": round(day_start, 2) if day_start is not None else None,
        "today_pnl": round(balance - day_start, 2) if balance is not None and day_start is not None else None,
        "open_count": len(open_pos),
        "open_positions": open_pos,
        "today_trades": trades,
    }


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>实盘看板</title>
<style>
body{font:14px/1.5 -apple-system,system-ui,sans-serif;margin:0;background:#0f1419;color:#e6e6e6}
.wrap{max-width:900px;margin:0 auto;padding:16px}
h1{font-size:16px;font-weight:600;margin:0 0 12px;color:#9fb3c8}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px}
.card{background:#1a2230;border-radius:10px;padding:12px}
.card .k{font-size:12px;color:#8aa0b6}
.card .v{font-size:22px;font-weight:700;margin-top:4px}
.pos{color:#4ade80}.neg{color:#f87171}
table{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:20px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #26303f}
th{color:#8aa0b6;font-weight:500;font-size:12px}
.buy{color:#60a5fa}.sell{color:#fbbf24}
.muted{color:#5b6b7c;font-size:12px;margin:8px 0}
</style></head><body><div class="wrap">
<h1>实盘看板 · <span id="asof"></span></h1>
<div class="cards">
 <div class="card"><div class="k">账户余额 (USDC)</div><div class="v" id="bal">—</div></div>
 <div class="card"><div class="k">今日盈亏</div><div class="v" id="pnl">—</div></div>
 <div class="card"><div class="k">持仓数</div><div class="v" id="cnt">—</div></div>
</div>
<div class="muted">持仓</div>
<table><thead><tr><th>比赛</th><th>通道</th><th>队</th><th>股数</th><th>均价</th><th>成本</th></tr></thead>
<tbody id="pos"></tbody></table>
<div class="muted">今日成交</div>
<table><thead><tr><th>时间</th><th>比赛</th><th>通道</th><th>队</th><th>买卖</th><th>股数</th><th>价格</th></tr></thead>
<tbody id="trd"></tbody></table>
</div>
<script>
async function tick(){
 try{const r=await fetch('/api/state',{cache:'no-store'});const s=await r.json();
  document.getElementById('asof').textContent=s.as_of||'';
  document.getElementById('bal').textContent=s.balance!=null?s.balance.toFixed(2):'—';
  const p=document.getElementById('pnl');
  if(s.today_pnl!=null){p.textContent=(s.today_pnl>=0?'+':'')+s.today_pnl.toFixed(2);
    p.className='v '+(s.today_pnl>=0?'pos':'neg');}else{p.textContent='—';p.className='v';}
  document.getElementById('cnt').textContent=s.open_count;
  document.getElementById('pos').innerHTML=(s.open_positions||[]).map(x=>
    `<tr><td>${x.gid}</td><td>${x.ch}</td><td>${x.outcome||''}</td><td>${x.qty}</td><td>${x.avg}</td><td>$${x.cost.toFixed(2)}</td></tr>`).join('')
    ||'<tr><td colspan="6" class="muted">无持仓</td></tr>';
  document.getElementById('trd').innerHTML=(s.today_trades||[]).map(x=>
    `<tr><td>${x.time}</td><td>${x.gid}</td><td>${x.ch}</td><td>${x.outcome||''}</td><td class="${x.kind}">${x.kind==='buy'?'买入':'卖出'}</td><td>${x.qty}</td><td>${x.price}</td></tr>`).join('')
    ||'<tr><td colspan="7" class="muted">今日暂无成交</td></tr>';
 }catch(e){}
}
tick();setInterval(tick,9000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    state = {}

    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if not AUTH:
            return True
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            import base64
            return base64.b64decode(h[6:]).decode("utf-8", "replace") == AUTH
        except Exception:
            return False

    def do_GET(self):
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="board"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            if self.path.startswith("/api/state"):
                body = json.dumps(self.state, ensure_ascii=False).encode()
                self._send(200, "application/json; charset=utf-8", body)
            elif self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", PAGE.encode())
            else:
                self._send(404, "application/json", b'{"error":"not found"}')
        except (BrokenPipeError, ConnectionResetError):
            pass


def scan_loop(runner_dir):
    while True:
        try:
            Handler.state = build_state(runner_dir)
        except Exception:                       # never let the board die
            pass
        time.sleep(REFRESH)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runner", type=Path, default=ROOT / "data/live_trade_final")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--auth", default="", help='"user:pass"; required when bound to a public host')
    args = ap.parse_args()
    global AUTH
    AUTH = args.auth
    Handler.state = build_state(args.runner)
    threading.Thread(target=scan_loop, args=(args.runner,), daemon=True).start()
    with ThreadingHTTPServer((args.host, args.port), Handler) as srv:
        print(f"team dashboard http://{args.host}:{args.port}/  (runner={args.runner})", flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    main()
