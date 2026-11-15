#!/usr/bin/env bash
set -euo pipefail
[ -n "${TG_TOKEN:-}" ] && [ -n "${TG_CHAT:-}" ] || exit 0
MSG=$(journalctl -u nba-live.service -n 80 --no-pager 2>/dev/null || true)
curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
  -d "chat_id=${TG_CHAT}" \
  --data-urlencode "text=NBA live runner failed on $(hostname)
$MSG" >/dev/null || true
