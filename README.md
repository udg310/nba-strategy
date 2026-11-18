# `python scripts/executor.py --scoreboard`

**Start paper trading.** Run the command above from the repository root on Windows, macOS or Linux. The paper executor uses the Python 3.12 standard library.

**English** · [简体中文](README.zh-CN.md)

### Live trading (this repository's live path)

`scripts/live_runner.py` is the live runner: one `GameTrader` per game, with a
SQLite attempt ledger at `var/live_runner/attempts.db`. The signed order hash
is written **before** the POST; a 200 response is never treated as a retryable
rejection; one game/channel can have only one in-flight OPEN attempt. On
restart, leftover `SUBMITTING` attempts become `UNKNOWN` and the channel is
not re-entered. C uses the **30-minute game clock** (not wall clock); D exits
on `m_fav < 0`.

```sh
export HTTPS_PROXY=http://127.0.0.1:9567   # CLOB SDK; direct endpoint is blocked
./scripts/run_live.sh --live
```

The order-book source is selected with `--market-data http|observe|ws` (or
`MARKET_DATA`); the default is `http`. `observe` keeps HTTP for trading and
logs stream/HTTP differences. `ws` uses a fresh streamed top-of-book and falls
back to HTTP when the book is missing, one-sided or older than 3 seconds.
Order submission still re-reads the venue over HTTP. Install
`scripts/requirements-executor-live.txt` for `websocket-client`.

The `scripts/executor.py` path above is a separate, alternative single-process
implementation.

## 稻香 · NBA trading dashboard

From the repository root, start the read-only dashboard with Python 3.12. It uses the standard library, listens on the local machine by default, and opens without a login.

```sh
python scripts/dashboard.py
```

| Page | What it shows |
| --- | --- |
| [Overview](http://127.0.0.1:8790/) | Account P&L, current holdings and every trade since 00:00 Hong Kong time, including price, quantity and time. |
| [Live games](http://127.0.0.1:8790/games) | One card per NBA game in progress, including games with no recorded trades. Total, realized and unrealized P&L update through Server-Sent Events (SSE). Cards leave the page when the game ends. |
| [Positions](http://127.0.0.1:8790/positions) | Entry prices, remaining shares, valuation and a per-position timeline of orders and fills. |
| [Trade history](http://127.0.0.1:8790/activity) | Earlier Hong Kong trading days, with filters for date, game, channel and trade type. |

**Reading P&L.** The dashboard uses recorded fees when available. An `≈` marks estimates, including sports taker fees calculated from shares and price. Open shares are valued at the current best bid or, once verified, the market's final payout price. Redemption enters realized P&L when the ledger records it. When a sale spans two channels, a reconciled combined result counts once in the total. A partial subtotal shows how many positions it covers.

**Data sources.** Defaults are runner logs in `data/live_trade_final/` and the paper ledger at `var/executor/paper.db`. Use `--live-ledger PATH` to read a live executor ledger. Add `--no-runner` to use the ledger alone. The dashboard keeps its own cache in `var/dashboard/dashboard.db`. Run `python scripts/dashboard.py --help` for the available options. [Dashboard requirements and API (Chinese)](reports/trading-dashboard-requirements.md)

## NBA / Polymarket in-game strategy — A + C + D

Strategy version **2026-11-14**. One Python process reads scores, evaluates the A/C/D strategy, executes orders and reconciles fills. After the console shows `executor ready` and `strategy started`, the strategy follows live ESPN scores and public Polymarket quotes and records simulated trades locally. Press **Ctrl+C** to stop.

Market data requests use `NBA_PROXY`, which defaults to `http://127.0.0.1:9567`. The first run creates the SQLite ledger at `var/executor/paper.db` and the default access token beside it in `paper.db.token`. Restarting reopens the same ledger. Check the mode and service status in the [local health check](http://127.0.0.1:8787/healthz).

To start only the execution service and connect a strategy manually, run `python scripts/executor.py`.

### Verify startup with recorded games

The repository includes a nine-game recording you can use when no games are in progress. If the live-score strategy is already running on the same paper account, press `Ctrl+C` to stop it, then run:

```powershell
python scripts/executor.py --recording data/live9_20261114.json
```

The service prints a report after each game and exits when the replay finishes.

To replay a single game with the processes running separately, start the executor, then run `python -I scripts/executor_producer.py --recording data/live9_20261114.json --game GAME_ID` with the ID from the recording. The simulated exchange fills at the requested limit price and charges zero fees. Use the research commands below for historical return analysis.

### Orders and post-game reconciliation

All endpoints except the health check require the `Authorization: Bearer <token>` header. The strategy reads the default token file or uses `NBA_EXECUTOR_TOKEN` when set. In PowerShell, use the following commands to list games and retrieve a report.

```powershell
$headers = @{ Authorization = 'Bearer ' + (Get-Content -Raw .\var\executor\paper.db.token).Trim() }
$games = Invoke-RestMethod http://127.0.0.1:8787/v1/games -Headers $headers
if ($games.Count) {
    Invoke-RestMethod "http://127.0.0.1:8787/v1/games/$($games[0])/report" -Headers $headers | ConvertTo-Json -Depth 8
}
```

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/order-intents`, `GET /v1/order-intents/{id}` | Create or reuse an entry or exit decision. Inspect attempts, order IDs, confirmed quantities, reservations and the event timeline. Amounts, prices and shares use millionths. Timestamps use Unix milliseconds. |
| `GET /v1/games/{id}/decisions`, `GET /v1/games/{id}/report/revisions` | Review the conditions that triggered or skipped a trade, report revisions for completed games, and differences caused by late fills. |
| `GET /v1/metrics`, `GET /v1/account` | Inspect pending orders, unattributed fills, latency, duplicate requests and available funds. |
| `POST /v1/account/pause-open`, `POST /v1/account/resume-open` | Pause or resume new entries. The service continues to track submitted orders and can still exit existing positions. |
| `POST /v1/reconcile`, `GET /v1/orders/{orderID}/trace` | Trigger read-only reconciliation or trace an exchange order ID to its owner. |
| `GET /v1/review/fills`, `POST /v1/review/resolve`, `GET /v1/review/actions` | Review unattributed fills and record the reviewer and evidence. You can link an order, enter confirmed fill details, identify an external manual trade or resolve a verified zero-fill attempt. |
| `POST /v1/settlements` | Record a verified payout on a D position with `position_id, venue_event, leg_id, shares, payout, fee, confirmed, actor, evidence`. The final score alone triggers the post-game report. |

### Start live trading and switch over

The Python executor manages submission rights, positions, order state and recovery. The Polymarket Python SDK handles signing and HTTP transport. The service reads Polygon CTF V2 contract receipts to reconcile fills. Repository tests use a simulated exchange, synthetic receipts and offline signing vectors. Before using a real account, verify the market version, wallet signature mode, available balance and order lookup results.

The executor saves the signed order ID in SQLite before submission. If the connection drops or shutdown interrupts a submission after transport begins, the outcome can be unknown. On restart, the executor keeps the reservation and looks up that order ID before releasing funds or shares.

1. Install the live dependencies in a dedicated Python virtual environment with `python -m pip install -r scripts/requirements-executor-live.txt` and run the executor with that interpreter. Set `POLY_PRIVATE_KEY`, `POLY_FUNDER`, `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE` and `POLY_SIGNATURE_TYPE` (default `3`) in the service environment. Also set `EXECUTOR_POLYGON_RPC_URL`, `EXECUTOR_ORDER_KEY` (a 32-byte key in standard Base64) and `NBA_EXECUTOR_TOKEN`.
2. Stop existing live order processes and reconcile the account’s order history, open positions and reserved funds. Keep the host, OS user, funding account and ledger path fixed. The process lock is local to that host and OS user, and older versions may run without it. Cross-check old JSONL entries that have no order ID against exchange records. A separate account with a verified empty position is an option for the first run.
3. **Initialize the live ledger once.** Replace the placeholders below. The funding account must match `POLY_FUNDER`. Use a fixed absolute path for `--db` and specify starting capital in millionths of a US dollar. The command creates the ledger and exits.

   ```sh
   python scripts/executor.py --mode live --account <FUNDING_ACCOUNT> --db <ABSOLUTE_LEDGER_PATH> --capital <CAPITAL_IN_MICRO_USD> --init-live
   ```

4. **Start or restart the service.** Reuse the same account, ledger path and starting capital. Replace `--init-live` with `--scoreboard`. Once the read-only account check succeeds, the service starts the live strategy in the same terminal.

   ```sh
   python scripts/executor.py --scoreboard --mode live --account <FUNDING_ACCOUNT> --db <ABSOLUTE_LEDGER_PATH> --capital <CAPITAL_IN_MICRO_USD>
   ```

5. Before stopping live trading, pause new entries and check pending orders and remaining shares. Keep the original ledger when restarting or handing over the account. SQLite uses a write-ahead log (WAL). Use SQLite’s online backup API while the service is running, or close all writers before copying the complete database.

### Development checks

```sh
python -m pip install -r scripts/requirements-executor-live.txt
python -B -m unittest discover -s scripts -p "test*.py"
```

Backend documentation in Chinese covers the [execution requirements and acceptance criteria](reports/executor-requirements.md), the [implementation guide for commit fa4ea64](reports/executor-commit-guide.md), and the [execution flow diagram](reports/executor-flow.html).

## Rules

`m_fav` is the **favourite’s score minus the underdog’s score**. Home and away determine how the scores are oriented when computing the margin. The same rules apply at either venue.

| | A — Underdog leads | C — Underdog gains in a close game | D — Favourite protects a late lead |
| --- | --- | --- | --- |
| Pregame filter | Favourite ≥0.60 | None | Favourite ≥0.60 |
| Game state | `m_fav ≤ −5` | `−5 ≤ m_fav < 0` | From 6:00 remaining in Q3, `5 ≤ m_fav < 10` |
| Signal | `m_fav` falls a further ≥3 points within 120 seconds of game time | Same as A | Take the first qualifying game state |
| Buy | Underdog | Underdog | Favourite |
| Entry price | ≥0.10, no ceiling | Same as A | All-in cost 0.70–0.80. Skip the game if the first entry fails this check. |
| Exit | Take profit at +20¢, no stop-loss, force exit at 6:00 remaining in Q4 | Take profit at +20¢, no price stop. Sell at market **30 game-clock minutes after entry**, or force exit at 6:00 remaining in Q4. | Hold to settlement. **Sell at market if `m_fav < 0` after entry** (the favourite falls behind). |
| Stake multiplier | 1.0× | 1.0× | 2.0× |

The cost model uses measured on-chain costs of about `0.10·p·(1−p)` per round trip plus the tick spread. `strategy.entry_cost(p)` and `exit_cost(p)` charge half the round-trip cost per side.

## Backtest (971 games with dog prices, 1555 total for D)

| Channel | n | per trade | full-loss | exit reasons |
|---|---:|---:|---:|---|
| A | 600 | +11.3% | 14.3% | target 392 / force 208 |
| C | 822 | +10.9% | 9.9% | target 437 / **time 326** / force 59 |
| D | 159 | **+10.1%** | **0.0%** | settle 90 / **margin_stop 69** |

Account (5% base stake, D 2.0×, `MAX_OPEN=8`): **$100 → $1,043.86**;
mark-to-market drawdown **$69.3**; peak exposure $45; 1580 fills.
Per-game P&L +$1.057, bootstrap 95% [$0.769, $1.345].

Holding D positions to settlement yielded +6.5% per trade with an 18.2% full-loss rate in this sample. Selling when the favourite fell behind yielded +10.1% per trade with a 0% full-loss rate.

## Live replay

| Sample | n | per trade | account (from $100) | drawdown |
|---|---:|---:|---:|---:|
| 14 historical live games (11-12/13) | 19 | +8.4% | $109.71 | $16.3 |
| 9 games on 2026-11-14 | 12 | +15.5% | $110.79 | $7.1 |
| 23 combined | 31 | +11.1% | **$120.49** | $16.3 |

On 2026-11-14, the $30 live account recorded **−$2.82**. A replay using the D margin stop, before aligning the C 30-minute exit, estimated a daily result of **+$3.9–4.3**. The Thunder D position lost $7.61 at settlement. Exiting when `m_fav < 0` at the recorded price of 0.675 would have limited the loss to $0.88. The Rockets recovered from a temporary 2-point dip, did not trigger the stop and settled at +$0.80.

## Files

| File | Purpose |
|---|---|
| `scripts/executor.py` | single-process strategy and execution service |
| `scripts/executor_producer.py` | reads scores and quotes, evaluates signals, and submits order intents to the executor |
| `scripts/executor_clob.py` | in-process Polymarket SDK adapter for live orders |
| `scripts/strategy.py` | the one strategy: A/C/D, backtest, live replay |
| `scripts/nba_data.py` | data / clock / HTTP / compact bundle loader |
| `scripts/live_runner.py` | legacy Python runner with one `GameTrader` per game |
| `scripts/pm_creds.py` | credential helper |
| `scripts/run_live.sh` | launcher for the legacy Python runner |
| `scripts/make_bundle.py` | rebuild `data/backtest_bundle.json.gz` |
| `scripts/replay_live.py` | replay the 14 historical games and the 9 games on 2026-11-14 |
| `scripts/swing_probe.py` | paper-only book recorder for maker research |
| `scripts/test_live_runner.py` | regression tests for the execution fixes |

## Backtest and replay commands

```sh
python3 -I scripts/strategy.py                 # 971-game backtest + account
python3 -I scripts/replay_live.py              # 14 + 9 live replay
```

## Data

- `data/backtest_bundle.json.gz` — compact 1555-game replay dataset (42.8 MB).
  It contains the same rows as `load_data_raw`: game metadata + favourite
  price history, ESPN plays, dog price history.
- `data/live_probe/2026-11-12`, `data/live_probe/2026-11-13` — the 14 historical live games.
- `data/live9_20261114.json` — the 9 games on 2026-11-14 (from ESPN, Gamma and the CLOB).
- `data/live_full_prices*.json` — CLOB price caches for the live replays.
- `data/live_trade_final/2026-11-13`, `data/live_trade_final/2026-11-14` — real runner event logs.
- The raw `data/games.json`, `data/diverse_opponents.json` and
  `data/cache/` are excluded from git; run `scripts/make_bundle.py` to rebuild
  the bundle after collecting new games.

## Legacy Python runner

The legacy runner starts with `./scripts/run_live.sh` for a dry run or `./scripts/run_live.sh --live` for real orders. Run its regression tests with `~/.polymarket/venv/bin/python -m unittest discover -s scripts -p 'test_live_runner.py'`.

### Concurrent games

`live_runner.py` runs **one `GameTrader` thread per live game**:

- The main thread polls ESPN every 3 s, resolves the Polymarket market,
  spawns a trader at tip-off, hands it each score snapshot, and releases the
  trader once the game is final and the trader holds nothing.
- Each trader owns its own CLOB client, score history, signal state, pending
  entries, positions and exit checks. A crash inside one trader is caught as
  `game_err` and cannot affect another game.
- Exits are checked per trader every tick, so N concurrent positions cost N
  parallel checks instead of one serialised pass.
- `ALL_POSITIONS`, `MAX_OPEN` and the confirmed-buy/sell flags are shared
  under a lock; cash is read from the account before each signal.

### Execution fixes in the legacy runner

1. **Duplicate orders.** During the waiting window, the runner tracks submitted requests in memory, checks `get_trades`, matches the order ID against `taker_order_id` when available and blocks another submission for the same signal. At the deadline it can check the balance increase if no other known position or pending order shares the token. The timeout and restart limitations of this path are documented in the [execution backend requirements (Chinese)](reports/executor-requirements.md).
2. **Sell accounting**: each position sells only its own shares; the trade
   record confirms the sell; a <0.02-share dust remainder is closed instead of
   retrying for 15 minutes.
3. **A/C share the same token**: they no longer liquidate each other; each
   channel sells its own size.
4. **400 `invalid amounts`**: market BUY orders force tick 0.01 (the 0.001
   tick's rounding config asks for 5-decimal shares, which the server rejects).
5. **Entry price.** When fill details are available, the runner uses the volume-weighted fill price.
6. **Resume** rebuilds buy−sold by channel, allocates a shared token balance by
   recorded size, and preserves the original entry time / game-clock entry for
   the C time exit.

## Known limitations

- The C 30-minute and D margin-stop parameters are selected on the same 971
  games; they are risk-shape choices, not out-of-sample proof.
- D has 159 backtest trades; the live D replay has only 2 captured trades
  (the 9-game Thunder/Rockets D entries were outside the minute-price band).
- Minute prints understate the live ask/bid; the live probe measured 29–59 s
  price lag and 3–4¢ adverse entry slippage.
- The strategy shows positive expectancy and fat-tailed returns in this sample; A/C full losses are still
  ~10–14%, and a small account will see lumpy daily P&L.
- The actual 2026-11-14 day also contained one non-runner Pelicans round trip
  (−$0.11); only the runner should trade the account.
