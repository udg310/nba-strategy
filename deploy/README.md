# Deployment (Hong Kong VPS)

## 1. Server
- Ubuntu 24.04 LTS, 1–2 vCPU, 2 GB RAM, 20+ GB disk; Hong Kong region.
- Open SSH only. Use key login; disable password auth.
- Verify direct access to ESPN / Gamma / CLOB:
  `curl -I https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard`
  `curl -I https://gamma-api.polymarket.com/`
  `curl -I https://clob.polymarket.com/`
  If CLOB needs a proxy, set `HTTPS_PROXY` in `/etc/nba-live.env`.

## 2. Install
```sh
sudo bash /opt/nba-strategy/deploy/install.sh
```
(or clone first, then run `deploy/install.sh`).

## 3. Credentials
```sh
sudo -u nba mkdir -p /home/nba/.polymarket
sudo -u nba chmod 700 /home/nba/.polymarket
# copy env + creds.json here, then:
sudo chmod 600 /home/nba/.polymarket/env /home/nba/.polymarket/creds.json
sudo chown nba:nba /home/nba/.polymarket/*
```

## 4. Dry-run
```sh
sudo -u nba /opt/nba-strategy/.venv/bin/python -I /opt/nba-strategy/scripts/live_runner.py --dry-run
```
Check: `start` with balance, no exceptions. Ctrl+C to stop.

## 5. Live daily
The timer starts the service at **01:30 Asia/Hong_Kong** every day; the runner
waits for tip-off, trades, then exits on `day_over`.
```sh
systemctl list-timers nba-live.timer
systemctl status nba-live.service
journalctl -u nba-live.service -f
```

## 6. Health / alerts
`nba-health.timer` runs every 15 min; it only alerts while games are `pre/in`.
Set `TG_TOKEN` and `TG_CHAT` in `/etc/nba-live.env` to receive Telegram messages.
`nba-live-alert.service` sends the last 80 journal lines if the service fails.

## 7. Manual control
```sh
sudo systemctl start nba-live.service     # start now
sudo systemctl stop nba-live.service      # stop
sudo systemctl disable --now nba-live.timer  # stop the daily schedule
```

## 8. Safety
- One runner only; `account_lock` enforces it.
- Attempt ledger: `/opt/nba-strategy/var/live_runner/attempts.db`.
- Event logs: `/opt/nba-strategy/data/live_trade_final/<UTC date>/<run>/events.jsonl`.
- Do not commit `~/.polymarket` or the ledger to git.
