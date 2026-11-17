# -*- coding: utf-8 -*-
"""Read-only live account board. Public quotes and reporting history are cached separately.

    python scripts/team_dashboard.py --host 127.0.0.1 --port 8790
"""
import argparse
import json
import logging
import sys
import threading
from datetime import datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The runner starts this script with python -I, which omits the script directory.
sys.path.insert(0, str(ROOT / "scripts"))
from dashboard_games import GamesFeed
from dashboard_markets import MarketDirectory
from dashboard_model import LIVE_REPORT_START
from dashboard_quotes import QuoteFeed
from dashboard_service import Dashboard
from dashboard_sources import Sources, key
from dashboard_store import HK, UTC, VersionStore, instant

REFRESH = 9.0
AUTH = ""


def create_dashboard(runner_dir, *, attempts=None, ledger=None, account=None, cache=None,
                     quotes=None, public=True):
    runner = Path(runner_dir).resolve()
    # The runner supplies data/live_trade_final. Custom paths stay scoped to
    # their own directory tree, including when replaying a test archive.
    state_dir = runner.parent.parent / "var" if runner.parent.name == "data" else runner.parent
    attempts = Path(attempts or state_dir / "live_runner/attempts.db").resolve()
    cache = Path(cache or state_dir / "team_dashboard/dashboard.db").resolve()
    ledger = Path(ledger).resolve() if ledger else None
    if cache in (attempts, ledger, runner):
        raise ValueError("看板缓存须与交易数据使用不同的路径")
    scope = key("team", str(runner), str(attempts), str(ledger), account)
    store = VersionStore(cache, scope, "live")
    source = Sources(ledger=ledger, runner=runner, attempts=attempts, account=account)
    return Dashboard({"live": source}, {"live": store}, quotes or QuoteFeed(public),
                     GamesFeed(start=False), MarketDirectory() if public else None,
                     report_starts={"live": LIVE_REPORT_START})


def numeric(value):
    return None if value is None else float(value)


def build_state(runner_dir, now=None, *, dashboard=None):
    now = now or datetime.now(UTC)
    if dashboard is None:
        # A one-shot reader can use the running collector's quote history
        # without creating competing revisions in its cache.
        app = create_dashboard(runner_dir, public=False)
        try:
            state = app.poll("live", now, publish=False)
        finally:
            app.close()
    else:
        dashboard.poll("live", now)
        state = dashboard.stores["live"].head
    snap = state["snapshot"]
    summary = snap["summary"]
    positions = [dict(gid=p["game_id"], ch=p["channel"], outcome=p["outcome"], qty=numeric(p["quantity"]),
                      avg=numeric(p["avg_entry_price"]), cost=numeric(p["remaining_cost"]),
                      market_value=numeric(p["market_value"])) for p in snap["open_positions"]]
    values = [p["market_value"] for p in state["positions"] if p["has_exposure"]
              and (p["quantity"] is None or Decimal(p["quantity"]) > 0)]
    trades = [dict(time=instant(r["time"]).astimezone(HK).strftime("%H:%M"), gid=r["game_id"],
                   ch=r["channel"], outcome=r["outcome"], kind=r["kind"], qty=numeric(r["quantity"]),
                   price=numeric(r["price"]), fee=numeric(r["fee"]), fee_unit=r["fee_unit"],
                   fee_basis=r.get("fee_basis"), time_kind=r["time_kind"]) for r in snap["today_activity"]]
    return dict(as_of=now.astimezone(HK).strftime("%Y-%m-%d %H:%M:%S"),
                balance=numeric(summary["available_cash"]), day_start=numeric(summary["day_start_cash"]),
                day_start_at=summary["day_start_at"], today_pnl=numeric(summary["today"]["value"]),
                today_pnl_metric=summary["today"], channels=snap["today_by_channel"],
                market_value=None if None in values else float(sum(map(Decimal, values))),
                open_count=summary["open_count"], review_count=summary["review_count"],
                open_positions=positions, today_trades=trades)


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>实盘看板</title>
<style>
body{font:14px/1.5 -apple-system,system-ui,sans-serif;margin:0;background:#0f1419;color:#e6e6e6}
.wrap{max-width:900px;margin:0 auto;padding:16px}
h1{font-size:16px;font-weight:600;margin:0 0 12px;color:#9fb3c8}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px;margin-bottom:16px}
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
 <div class="card"><div class="k">账户现金 (USDC)</div><div class="v" id="bal">—</div></div>
 <div class="card"><div class="k">持仓市值 (USDC)</div><div class="v" id="value">—</div></div>
 <div class="card"><div class="k" id="pnl-label">今日盈亏</div><div class="v" id="pnl">—</div><div class="k" id="coverage"></div></div>
 <div class="card"><div class="k">持仓数</div><div class="v" id="cnt">—</div></div>
</div>
<div class="muted" id="channels"></div>
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
  document.getElementById('value').textContent=s.market_value!=null?s.market_value.toFixed(2):'—';
  const p=document.getElementById('pnl');
  const m=s.today_pnl_metric, amount=m.value??m.known_subtotal;
  document.getElementById('pnl-label').textContent=m.status==='partial'?'今日盈亏（可算小计）':'今日盈亏';
  if(amount!=null){const v=Number(amount);p.textContent=(v>=0?'+':'')+v.toFixed(2);
    p.className='v '+(v>=0?'pos':'neg');}else{p.textContent='—';p.className='v';}
  const reasons={no_quote:'缺报价',stale_quote:'报价已过期',no_bid:'缺买一报价',no_day_baseline:'缺零点估值',
    missing_cost:'缺成本',missing_fee:'缺费用',source_gap:'数据不全',unattributed:'有待归属成交',shared_exit:'共用卖出待核'};
  const gaps=[...new Set(m.issues.map(i=>reasons[i.code]||'待核账'))];
  document.getElementById('coverage').textContent=[m.covered_count+'/'+m.total_count+' 个仓位',
    ...gaps,m.includes_estimated_fee?'含估算费用':''].filter(Boolean).join(' · ');
  document.getElementById('channels').textContent=Object.entries(s.channels).map(([ch,x])=>
    ch+' 通道 '+(x.value!=null?Number(x.value).toFixed(2):x.known_subtotal!=null?
      Number(x.known_subtotal).toFixed(2)+'（小计）':'—')).join('　');
  document.getElementById('cnt').textContent=s.open_count;
  document.getElementById('pos').innerHTML=(s.open_positions||[]).map(x=>
    `<tr><td>${x.gid}</td><td>${x.ch}</td><td>${x.outcome||''}</td><td>${x.qty??'—'}</td><td>${x.avg??'—'}</td><td>${x.cost!=null?'$'+x.cost.toFixed(2):'—'}</td></tr>`).join('')
    ||'<tr><td colspan="6" class="muted">无持仓</td></tr>';
  document.getElementById('trd').innerHTML=(s.today_trades||[]).map(x=>
    `<tr><td>${x.time}</td><td>${x.gid}</td><td>${x.ch}</td><td>${x.outcome||''}</td><td class="${x.kind}">${({buy:'买入',sell:'卖出',settlement:'兑付'})[x.kind]}</td><td>${x.qty??'—'}</td><td>${x.price??'—'}</td></tr>`).join('')
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


def scan_loop(runner_dir, app, stop):
    while not stop.wait(REFRESH):
        try:
            Handler.state = build_state(runner_dir, dashboard=app)
        except Exception:
            logging.exception("团队看板采集失败")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runner", type=Path, default=ROOT / "data/live_trade_final")
    ap.add_argument("--attempts", type=Path)
    ap.add_argument("--live-ledger", type=Path)
    ap.add_argument("--cache", type=Path)
    ap.add_argument("--account")
    ap.add_argument("--no-quotes", action="store_true", help="disable public market requests")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--auth", default="", help='"user:pass"; required when bound to a public host')
    args = ap.parse_args()
    global AUTH
    AUTH = args.auth
    with ThreadingHTTPServer((args.host, args.port), Handler) as srv:
        app = create_dashboard(args.runner, attempts=args.attempts, ledger=args.live_ledger,
                               cache=args.cache, account=args.account, public=not args.no_quotes)
        stop = threading.Event()
        thread = threading.Thread(target=scan_loop, args=(args.runner, app, stop), daemon=True)
        try:
            Handler.state = build_state(args.runner, dashboard=app)
            thread.start()
            print(f"team dashboard http://{args.host}:{srv.server_port}/  (runner={args.runner})", flush=True)
            srv.serve_forever()
        finally:
            stop.set()
            if thread.is_alive():
                thread.join()
            app.close()


if __name__ == "__main__":
    main()
