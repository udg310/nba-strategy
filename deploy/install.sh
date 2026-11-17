#!/usr/bin/env bash
# Install or update on an Ubuntu 24.04 host. Run as root.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
if (( $# > 1 )); then
  echo "Usage: install.sh [BRANCH_OR_FULL_SHA]" >&2
  exit 2
fi
release=${1-main}
exec 9>/run/lock/nba-strategy-install.lock
flock -n 9 || { echo "Another deployment is running" >&2; exit 1; }

apt-get update
apt-get install -y python3.12 python3.12-venv git curl jq

id -u nba >/dev/null 2>&1 || adduser --disabled-password --gecos "" nba

if [ ! -e /opt/nba-strategy ]; then
  install -d -o nba -g nba /opt/nba-strategy
fi
sudo -u nba -H bash -s -- https://github.com/udg310/nba-strategy.git \
  /opt/nba-strategy "$release" < "$(dirname -- "$0")/checkout-release.sh"

sudo -u nba -H python3.12 -m venv /opt/nba-strategy/.venv
sudo -u nba -H /opt/nba-strategy/.venv/bin/pip install -q \
  -r /opt/nba-strategy/scripts/requirements-executor-live.txt

mkdir -p /var/log/nba-live /home/nba/.polymarket
chown -R nba:nba /var/log/nba-live /home/nba/.polymarket

if [ ! -f /etc/nba-live.env ]; then
  umask 077
  cat > /etc/nba-live.env <<'ENV'
# Empty = direct. Set only if the server needs a local proxy.
NBA_PROXY=
HTTPS_PROXY=
HTTP_PROXY=
STAKE_FRAC=0.05
# Optional Telegram alerts
TG_TOKEN=
TG_CHAT=
ENV
fi
chmod 600 /etc/nba-live.env

cp /opt/nba-strategy/deploy/nba-live.service \
   /opt/nba-strategy/deploy/nba-live.timer \
   /opt/nba-strategy/deploy/nba-health.service \
   /opt/nba-strategy/deploy/nba-health.timer \
   /opt/nba-strategy/deploy/nba-live-alert.service \
   /etc/systemd/system/
chmod +x /opt/nba-strategy/deploy/health.sh /opt/nba-strategy/deploy/alert.sh

systemctl daemon-reload
systemctl enable --now nba-live.timer nba-health.timer

echo
echo "Installed. Next:"
echo "  1) copy ~/.polymarket/env and ~/.polymarket/creds.json to /home/nba/.polymarket (chmod 600)"
echo "  2) dry-run: sudo -u nba /opt/nba-strategy/.venv/bin/python -I /opt/nba-strategy/scripts/live_runner.py --dry-run"
echo "  3) the timer starts the live runner daily at 01:30 Asia/Hong_Kong"
