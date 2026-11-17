# Deployment (Hong Kong VPS)

## 1. Server
- Ubuntu 24.04 LTS, 1–2 vCPU, 2 GB RAM, 20+ GB disk; Hong Kong region.
- Open SSH only. Use key login; disable password auth.
- Verify direct access to ESPN / Gamma / CLOB:
  `curl -I https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard`
  `curl -I https://gamma-api.polymarket.com/`
  `curl -I https://clob.polymarket.com/`
  If CLOB needs a proxy, set `HTTPS_PROXY` in `/etc/nba-live.env`.

## 2. Install or update

Run from a trusted checkout containing these deployment scripts. On a fresh
host, keep this checkout outside `/opt/nba-strategy`. The installer creates
that directory for the `nba` user. An existing deployment must be owned and
writable by `nba`. Run updates between trading sessions.

```sh
sudo bash deploy/install.sh main
```

Both installs and updates fetch the named branch from
`https://github.com/udg310/nba-strategy.git`. The default is `main`. For a
reproducible release, use the full 40-character SHA of a published commit.
For example, to install the commit checked out in your installer copy:

```sh
release_sha=$(git rev-parse --verify HEAD)
sudo bash deploy/install.sh "$release_sha"
sudo -u nba git -C /opt/nba-strategy rev-parse --verify HEAD
```

The installer prints the current and target SHAs and checks out the target
with a detached HEAD. Updates require a fast-forward from the installed SHA.
A missing target, divergent history, or local edits stop the update. The
checkout preserves the virtual environment, runtime logs and ignored state.

After the next live startup, compare the last recorded startup SHA with the
installed SHA above. This reads the complete structured event from disk:

```sh
find /opt/nba-strategy/data/live_trade_final -name events.jsonl | sort |
  while IFS= read -r log; do jq -r 'select(.ev == "start" and .live == true) | .git_sha' "$log"; done |
  tail -n 1
```

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
Look for `live=false` in the startup event under `data/dry_run/`.
The public market client leaves the account balance empty. Ctrl+C to stop.

## 5. Live daily
The timer starts the service at **01:30 Asia/Hong_Kong** every day; the runner
waits for tip-off, trades, then exits on `day_over`.
```sh
systemctl list-timers nba-live.timer
systemctl status nba-live.service
journalctl -u nba-live.service -f
```

Final games hand unresolved orders and positions to the account ledger. The
runner checks the queue at startup, during the session, and at the final
handoff. Between sessions, inspect the durable queue with the public funding
address and the read-only report:

```sh
sudo -u nba /opt/nba-strategy/.venv/bin/python /opt/nba-strategy/scripts/live_account_report.py --account PUBLIC_FUNDING_ADDRESS
```

For a legacy holding in review, compare the original claim and recorded
fills with the venue receipts. A confirmed market payout price and the
receipt for an actual wallet redemption are separate facts.

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

## 8. Disable automatic updates

A trading host must not upgrade `systemd`/`udev`/`libc` mid-session. On
2026-11-16 `unattended-upgrades` upgraded 200+ packages at 06:29, restarted
`networkd`/`cloudmonitor`, and crashed the runner into a restart loop
06:30–06:45. Disable it and hold the restart-prone packages:

```sh
sudo systemctl disable --now unattended-upgrades
sudo systemctl disable --now apt-daily.timer apt-daily-upgrade.timer
sudo systemctl mask unattended-upgrades
sudo apt-mark hold systemd udev libc6 libssl3t64
```

Upgrade manually in an off-hours maintenance window instead (held packages
show as "held back" until `apt-mark unhold`). Never reboot during a session.

## 9. Safety
- One runner only; `account_lock` enforces it.
- Attempt ledger: `/opt/nba-strategy/var/live_runner/attempts.db`.
- Event logs: `/opt/nba-strategy/data/live_trade_final/<UTC date>/<run>/events.jsonl`.
- Do not commit `~/.polymarket` or the ledger to git.
