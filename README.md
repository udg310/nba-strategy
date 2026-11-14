# NBA / Polymarket in-game strategy — A + C + D

Fixed version: **2026-11-14**. One strategy implementation lives in
`scripts/strategy.py`; all data/clock/HTTP plumbing lives in `scripts/nba_data.py`;
live execution is `scripts/live_runner.py`.

## Rules

`m_fav` always means **favourite score minus dog score**. Home/away only
rotates the sign; it enters no rule.

| | A 弱队领先 | C 比分接近弱队起势 | D 末节强队锁胜 |
|---|---|---|---|
| 赛前过滤 | 强队 ≥0.60 | 无 | 强队 ≥0.60 |
| 状态 | `m_fav ≤ −5` | `−5 ≤ m_fav < 0` | Q3 剩 6:00 起，`5 ≤ m_fav < 10` |
| 信号 | 120 秒比赛时钟内 `m_fav` 再降 ≥3 | 同左 | 取首个状态时点，不等待 |
| 买入 | 弱队 | 弱队 | 强队 |
| 入场价 | ≥0.10，无上限 | 同左 | 全成本 0.70–0.80；不合格放弃整场 |
| 出场 | TP+20c / 无止损 / Q4 6:00 强平 | TP+20c / 无价格止损 / **入场后 30 分钟比赛时钟**按市价卖出 / Q4 6:00 强平 | 持有到结算；**入场后 `m_fav < 0`（强队被反超）按市价卖出** |
| 仓位 | 1.0× | 1.0× | 2.0× |

Cost model: the exchange measured on-chain charges about `0.10·p·(1−p)` per
round trip plus the tick spread. `strategy.entry_cost(p)` / `exit_cost(p)`
charge half of that per side.

## Backtest (971 games with dog prices, 1555 total for D)

| Channel | n | per trade | full-loss | exit reasons |
|---|---:|---:|---:|---|
| A | 600 | +11.3% | 14.3% | target 392 / force 208 |
| C | 822 | +10.9% | 9.9% | target 437 / **time 326** / force 59 |
| D | 159 | **+10.1%** | **0.0%** | settle 90 / **margin_stop 69** |

Account (5% base stake, D 2.0×, `MAX_OPEN=8`): **$100 → $1,043.86**;
mark-to-market drawdown **$69.3**; peak exposure $45; 1580 fills.
Per-game P&L +$1.057, bootstrap 95% [$0.769, $1.345].

The D margin stop is the key D change: settlement-only was +6.5%/18.2%
full-loss; stopping when the favourite actually trails is +10.1%/0% full-loss.

## Live replay

| Sample | n | per trade | account (from $100) | drawdown |
|---|---:|---:|---:|---:|
| 14 historical live games (11-12/13) | 19 | +8.4% | $109.71 | $16.3 |
| 9 games on 2026-11-14 | 12 | +15.5% | $110.79 | $7.1 |
| 23 combined | 31 | +11.1% | **$120.49** | $16.3 |

On the actual 2026-11-14 $30 account (before the C30 replay alignment) the D
margin stop changed the day from **−$2.82** to about **+$3.9–4.3**: the Thunder
D settled at −$7.61, but the margin stop (m_fav < 0, print 0.675) cut it to
−$0.88; the Rockets D was **not** stopped (a temporary 2-point deficit that
recovered) and settled +$0.80.

## Files

| File | Purpose |
|---|---|
| `scripts/strategy.py` | the one strategy: A/C/D, backtest, live replay |
| `scripts/nba_data.py` | data / clock / HTTP / compact bundle loader |
| `scripts/live_runner.py` | live execution; one `GameTrader` per game |
| `scripts/pm_creds.py` | credential helper |
| `scripts/run_live.sh` | launcher (checks the proxy; `--live` for real orders) |
| `scripts/make_bundle.py` | rebuild `data/backtest_bundle.json.gz` |
| `scripts/replay_live.py` | replay the 14 historical + today's 9 live games |
| `scripts/swing_probe.py` | paper-only book recorder for maker research |
| `scripts/test_live_runner.py` | regression tests for the execution fixes |

## Run

```sh
python3 -I scripts/strategy.py                 # 971-game backtest + account
python3 -I scripts/replay_live.py              # 14 + 9 live replay
./scripts/run_live.sh --live                   # real orders (needs the proxy)
~/.polymarket/venv/bin/python -m unittest discover -s scripts -p 'test_live_runner.py'
```

## Data

- `data/backtest_bundle.json.gz` — compact 1555-game replay dataset (42.8 MB).
  It contains the same rows as `load_data_raw`: game metadata + favourite
  price history, ESPN plays, dog price history.
- `data/live_probe/2026-11-12`, `2026-11-13` — the 14 historical live games.
- `data/live9_20261114.json` — today's 9 games (built ESPN + Gamma + CLOB).
- `data/live_full_prices*.json` — CLOB price caches for the live replays.
- `data/live_trade_final/2026-11-13`, `2026-11-14` — real runner event logs.
- The raw `data/games.json`, `data/diverse_opponents.json` and
  `data/cache/` are excluded from git; run `scripts/make_bundle.py` to rebuild
  the bundle after collecting new games.

## Per-game trader architecture

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

## Execution fixes in this version

1. **No duplicate orders**: one post per signal. Fills are confirmed from
   `get_trades` (order ID / `taker_order_id`), never from the lagging token
   balance; a posted order is never re-posted while its outcome is unknown.
2. **Sell accounting**: each position sells only its own shares; the trade
   record confirms the sell; a <0.02-share dust remainder is closed instead of
   retrying for 15 minutes.
3. **A/C share the same token**: they no longer liquidate each other; each
   channel sells its own size.
4. **400 `invalid amounts`**: market BUY orders force tick 0.01 (the 0.001
   tick's rounding config asks for 5-decimal shares, which the server rejects).
5. **Entry price** is the actual fill (weighted), not the ask.
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
- The strategy is positive-expectancy but fat-tailed; A/C full losses are still
  ~10–14%, and a small account will see lumpy daily P&L.
- The actual 2026-11-14 day also contained one non-runner Pelicans round trip
  (−$0.11); only the runner should trade the account.
