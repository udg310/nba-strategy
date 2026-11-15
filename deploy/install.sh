#!/usr/bin/env bash
# One-time install on an Ubuntu 24.04 host. Run as root.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

apt-get update
apt-get install -y python3.12 python3.12-venv git curl jq

id -u nba >/dev/null 2>&1 || adduser --disabled-password --gecos "" nba

if [ -d /opt/nba-strategy/.git ]; then
  sudo -u nba -H git -C /opt/nba-strategy pull --ff-only
else
  sudo -u nba git clone https://github.com/udg310/nba-strategy /opt/nba-strategy
fi

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
echo "  3) the timer starts the live runner daily at 07:00 Asia/Hong_Kong"
