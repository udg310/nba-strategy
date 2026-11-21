#!/bin/zsh
# Launch the totals fade runner. Credentials stay in ~/.polymarket.
# Usage:  ./scripts/run_totals.sh            (dry run)
#         ./scripts/run_totals.sh --live     (real orders)
set -e
cd "$(dirname "$0")/.."
MODE="${1:---dry-run}"
PROXY="${NBA_PROXY:-http://127.0.0.1:9567}"
if [ -n "$PROXY" ]; then
  export HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY"
  if ! curl -s -m 5 -x "$PROXY" https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard >/dev/null; then
    echo "proxy $PROXY 不可用，先开 VPN"; exit 1
  fi
fi
LOG="data/totals_live/runner_$(date -u +%Y%m%d_%H%M%S).log"
mkdir -p data/totals_live
nohup ~/.polymarket/venv/bin/python -I scripts/totals_runner.py $MODE > "$LOG" 2>&1 &
echo "started pid $! mode=$MODE log=$LOG"
