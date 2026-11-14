#!/bin/zsh
# Launch the live runner in the background. Credentials stay in ~/.polymarket.
# Usage:  ./scripts/run_live.sh            (dry run)
#         ./scripts/run_live.sh --live     (real orders)
set -e
cd "$(dirname "$0")/.."
MODE="${1:---dry-run}"
PROXY="${NBA_PROXY:-http://127.0.0.1:9567}"
if ! curl -s -m 5 -x "$PROXY" https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard >/dev/null; then
  echo "proxy $PROXY 不可用，先开 VPN"; exit 1
fi
LOG="data/live_trade_final/runner_$(date -u +%Y%m%d_%H%M%S).log"
mkdir -p data/live_trade_final
nohup ~/.polymarket/venv/bin/python -I scripts/live_runner.py $MODE > "$LOG" 2>&1 &
echo "started pid $! mode=$MODE log=$LOG"
