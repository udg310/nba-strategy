# scripts/ — final framework (2026-11-14)

Start the paper strategy and executor together from the repository root with
`python scripts/executor.py --scoreboard`. See the [English](../README.md)
or [Chinese](../README.zh-CN.md) guide for recordings, account setup and the local API.

| File | Purpose |
|---|---|
| **`strategy.py`** | **the one strategy.** A/C/D, backtest, live replay. No other strategy file. |
| **`nba_data.py`** | data / clock / HTTP / compact bundle. **No strategy rules.** |
| `executor.py` | alternative single-process strategy + SQLite executor (paper/live) from the collaborator branch. |
| `live_runner.py` | **live execution**: one `GameTrader` per game + SQLite attempt ledger (one in-flight order per signal). |
| `market_ws.py` | Polymarket order-book WebSocket with per-token HTTP fallback; used by `live_runner.py --market-data observe|ws`. |
| `pm_creds.py` | credential helper; credentials stay in `~/.polymarket`. |
| `run_live.sh` | launcher for the live runner (`--live` for real orders). |
| `make_bundle.py` | rebuild `data/backtest_bundle.json.gz` from raw sources. |
| `replay_live.py` | replay the 14 historical + today's 9 live games. |
| `swing_probe.py` | paper-only book recorder around score changes (maker research). |
| `test_live_runner.py` | 10 regression tests: one-post/UNKNOWN, order-ID isolation, attempt uniqueness, restart recovery, sells, C30, D stop, balance reconciliation. |

Everything else that used to live here (pendulum, fusion, halftime totals,
NFL, account/fusion simulators, price strategy, research collector) is
superseded and was removed.

## Three channels

| | A 弱队领先 | C 比分接近弱队起势 | D 末节强队锁胜 |
|---|---|---|---|
| 赛前过滤 | 强队 ≥0.60 | 无 | 强队 ≥0.60 |
| 状态 | `m_fav ≤ −5` | `−5 ≤ m_fav < 0` | Q3 剩 6:00 起，`5 ≤ m_fav < 10` |
| 信号 | 120 秒比赛时钟内 `m_fav` 再降 ≥3 | 同左 | 取首个状态时点 |
| 买入 | 弱队 | 弱队 | 强队 |
| 入场价 | ≥0.10，无上限 | 同左 | 全成本 0.70–0.80 |
| 出场 | TP+20c / Q4 6:00 强平 | TP+20c / **入场后 30 分钟比赛时钟**按市价卖出 / Q4 6:00 强平 | 结算；**入场后 `m_fav < 0` 按市价卖出（margin_stop）** |
| 仓位 | 1.0× | 1.0× | 2.0× |

`m_fav` 恒等于强队减分；主客只用于旋转分差符号。

## Run

```sh
python3 -I scripts/strategy.py                 # 971-game backtest ($100 → $1,043.86)
python3 -I scripts/replay_live.py              # 14 + 9 live replay
python3 -I scripts/make_bundle.py              # rebuild the compact bundle
./scripts/run_live.sh --live                   # real orders (proxy required)
~/.polymarket/venv/bin/python -m unittest discover -s scripts -p 'test_live_runner.py'
```

## Hard invariants

1. `m_fav` is always favourite minus dog.
2. A/C buy the dog token; D buys the favourite token.
3. A/C exits are only `target`, `time` (C only), or `force`; there is no
   settlement path for the process channels.
4. D exits are only `settle` or `margin_stop`; no TP/price stop/force.
5. A game skipped by D's price filter is not retried later.

## Data

- `data/backtest_bundle.json.gz` — compact 1555-game replay dataset.
- `data/live_probe/2026-11-12`, `2026-11-13` — 14 historical live games.
- `data/live9_20261114.json` — today's 9 games.
- `data/live_full_prices*.json` — CLOB price caches.
- `data/live_trade_final/...` — real runner event logs.
- Raw `games.json`, `diverse_opponents.json`, `cache/` are **not in git**;
  rebuild the bundle with `make_bundle.py`.

## Per-game trader architecture

One `GameTrader` thread per live game. The main thread polls ESPN, resolves
each market, spawns the trader at tip-off, feeds snapshots, and releases it
when the game is final and flat. Each trader owns its CLOB client, score
history, pending entries, positions and exits; a trader crash is logged as
`game_err` and cannot affect another game. `ALL_POSITIONS` / `MAX_OPEN` /
confirmed-buy/sell are shared under a lock.
