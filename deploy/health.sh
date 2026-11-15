#!/usr/bin/env bash
set -euo pipefail
BASE=/opt/nba-strategy
DAY=$(date -u +%F)
DIR="$BASE/data/live_trade_final/$DAY"

send() {
  [ -n "${TG_TOKEN:-}" ] && [ -n "${TG_CHAT:-}" ] || return 0
  curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    -d "chat_id=${TG_CHAT}" --data-urlencode "text=$1" >/dev/null || true
}

# Only judge the runner while a slate is live or about to start.
GAMES=$(curl -s -m 10 ${NBA_PROXY:+-x "$NBA_PROXY"} \
  "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard" 2>/dev/null \
  | jq '[.events[] | select(.status.type.state=="in" or .status.type.state=="pre")] | length' 2>/dev/null || echo 0)

if [ "${GAMES:-0}" -gt 0 ]; then
  if ! systemctl is-active --quiet nba-live.service; then
    send "NBA live runner is NOT active while $GAMES game(s) are pre/in on $(hostname)"
  elif [ -d "$DIR" ] && ! find "$DIR" -name events.jsonl -newermt '-8 minutes' | grep -q .; then
    send "NBA live runner has no heartbeat in 8 minutes on $(hostname)"
  fi
fi
