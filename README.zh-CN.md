# `python scripts/executor.py --scoreboard`

**模拟盘一键启动。** 在项目根目录（例如 `E:\nba-strategy`）运行上面的命令。Windows、macOS 和 Linux 使用同一入口。模拟盘只需 Python 3.12 的标准库。

[English](README.md) · **简体中文**

## NBA / Polymarket 赛中交易策略（A、C、D）

策略版本 **2026-11-14**。比分采集、A/C/D 信号、订单执行和对账在同一个 Python 进程内运行。终端显示 `executor ready` 和 `strategy started` 后，策略会跟踪 ESPN 比分和 Polymarket 公开盘口，将模拟成交记入本地账本。按 **Ctrl+C** 停止。

行情请求使用 `NBA_PROXY`，默认代理为 `http://127.0.0.1:9567`。首次启动会创建 SQLite 账本 `var/executor/paper.db` 和默认访问令牌 `paper.db.token`。重启沿用原账本。在[本机健康检查](http://127.0.0.1:8787/healthz)中可以查看运行状态和账户模式。

仅启动执行服务、手动接入策略时，运行 `python scripts/executor.py`。

### 用历史比赛验证启动结果

没有正在进行的比赛时，可以用仓库自带的 9 场比赛记录验证启动流程。如果同一模拟账户正在运行实时策略，先按 `Ctrl+C`，再运行：

```powershell
python scripts/executor.py --recording data/live9_20261114.json
```

每场结束后会输出报告，全部回放完成后自动退出。

分开运行时，先启动执行服务，再运行 `python -I scripts/executor_producer.py --recording data/live9_20261114.json --game 比赛ID` 回放指定场次。模拟交易所按意图限价撮合并使用零手续费。历史收益研究使用下面的回测入口。

### 订单与赛后对账

除健康检查外，接口均需请求头 `Authorization: Bearer <token>`。生产者会读取默认令牌文件，也支持环境变量 `NBA_EXECUTOR_TOKEN`。从 PowerShell 查询当前比赛和单场报告：

```powershell
$headers = @{ Authorization = 'Bearer ' + (Get-Content -Raw .\var\executor\paper.db.token).Trim() }
$games = Invoke-RestMethod http://127.0.0.1:8787/v1/games -Headers $headers
if ($games.Count) {
    Invoke-RestMethod "http://127.0.0.1:8787/v1/games/$($games[0])/report" -Headers $headers | ConvertTo-Json -Depth 8
}
```

| 入口 | 用途 |
| --- | --- |
| `POST /v1/order-intents`、`GET /v1/order-intents/{id}` | 创建或复用一次开仓/平仓决策，查尝试、订单号、确认数量、预留和时间线。金额、价格、份额以百万分之一为单位，时间是 Unix 毫秒 |
| `GET /v1/games/{id}/decisions`、`GET /v1/games/{id}/report/revisions` | 查触发或跳过的条件、已结束比赛的报告版本，以及迟到成交导致的差额变动 |
| `GET /v1/metrics`、`GET /v1/account` | 查未决订单、未归属成交、延迟、重复请求和当前可用额度 |
| `POST /v1/account/pause-open`、`POST /v1/account/resume-open` | 暂停或恢复新的入场；接管前已经发出的订单继续核查，已持仓仍可退出 |
| `POST /v1/reconcile`、`GET /v1/orders/{orderID}/trace` | 触发只读对账，或按交易所订单号查归属 |
| `GET /v1/review/fills`、`POST /v1/review/resolve`、`GET /v1/review/actions` | 核对待认领成交并记录复核人和证据。可绑定订单、补录已确认明细、标记外部手工交易或处置已核实的零成交 |
| `POST /v1/settlements` | 为 D 仓位登记经核实的兑付，输入 `position_id, venue_event, leg_id, shares, payout, fee, confirmed, actor, evidence`。终场本身只生成赛后报告 |

### 实盘接入与切换

Python 执行器负责发单权、持仓、订单状态与恢复。签名和 HTTP 传输调用项目所用的 Polymarket Python SDK，读取 Polygon CTF V2 合约回执来核对成交。本仓库的验证使用模拟交易所、合成回执与离线签名向量。真实账户接入需先核对市场版本、钱包签名模式、可用余额与订单查询结果。

发送前，执行器先将已签名的订单号存入 SQLite。若请求进入网络传输后断线或停机，订单可能已经成交。重启后，执行器会保留对应的资金或份额，用原订单号核查结果。

1. 在专用 Python 虚拟环境安装 `python -m pip install -r scripts/requirements-executor-live.txt`，使用该环境中的 Python 运行执行器。凭证通过服务进程环境配置：`POLY_PRIVATE_KEY`、`POLY_FUNDER`、`POLY_API_KEY`、`POLY_API_SECRET`、`POLY_API_PASSPHRASE`、`POLY_SIGNATURE_TYPE`（默认 3）。另需 `EXECUTOR_POLYGON_RPC_URL`、`EXECUTOR_ORDER_KEY`（32 字节密钥的标准 Base64）和 `NBA_EXECUTOR_TOKEN`。
2. 停止既有实盘下单进程，核清账户历史订单、开放仓位和未决资金。使用同一台主机、同一系统用户、同一个资金账户和固定账本路径。旧版本、不同系统用户或不同主机上的程序无法共享此进程锁。旧 JSONL 中缺少订单身份的记录需要用交易所证据核对。首次使用可从经核对的独立空仓账户开始。
3. **首次初始化实盘账本。** 将下列占位符替换为实际值。账户须与 `POLY_FUNDER` 一致，`--db` 使用固定的绝对路径，初始资金以百万分之一美元为单位。此命令只需执行一次，建库后会退出。

   ```sh
   python scripts/executor.py --mode live --account <资金账户> --db <实盘账本绝对路径> --capital <初始资金微单位> --init-live
   ```

4. **日常启动或重启。** 沿用首次初始化的账户、账本路径和初始资金，将 `--init-live` 换为 `--scoreboard`。服务通过账户只读探测后，在同一终端自动启动实盘策略。

   ```sh
   python scripts/executor.py --scoreboard --mode live --account <资金账户> --db <实盘账本绝对路径> --capital <初始资金微单位>
   ```

5. 停止实盘时先暂停新开仓，核对未决委托与剩余份额。恢复或移交时继续使用原账本。账本包含 SQLite WAL，运行中备份使用 SQLite 在线备份接口，或在全部写方关闭后复制完整数据库。

### 开发验证

```sh
python -m pip install -r scripts/requirements-executor-live.txt
python -B -m unittest discover -s scripts -p "test*.py"
```

两份原始文档保留了当时的实现背景：[执行后端需求](reports/go-order-execution-requirements.md) 与[后续待排期事项](reports/go-executor-live-reliability-requirements.md)。

## 策略规则

`m_fav` 表示**强队得分减去弱队得分**。计算分差时要根据主客场确定符号，同一套规则适用于主场和客场。

| | A 弱队领先 | C 比分接近弱队起势 | D 末节强队锁胜 |
|---|---|---|---|
| 赛前过滤 | 强队 ≥0.60 | 无 | 强队 ≥0.60 |
| 状态 | `m_fav ≤ −5` | `−5 ≤ m_fav < 0` | Q3 剩 6:00 起，`5 ≤ m_fav < 10` |
| 信号 | 120 秒比赛时钟内 `m_fav` 再降 ≥3 | 同左 | 取首个状态时点，不等待 |
| 买入 | 弱队 | 弱队 | 强队 |
| 入场价 | ≥0.10，无上限 | 同左 | 全成本 0.70–0.80；不合格放弃整场 |
| 出场 | 盈利 20 美分止盈 / 无止损 / Q4 6:00 强平 | 盈利 20 美分止盈 / 无价格止损 / **入场后 30 分钟比赛时钟**按市价卖出 / Q4 6:00 强平 | 持有到结算；**入场后 `m_fav < 0`（强队被反超）按市价卖出** |
| 仓位 | 1.0× | 1.0× | 2.0× |

费用模型采用链上实测值。价格为 `p` 时，一次完整买卖的成本约为 `0.10·p·(1−p)` 加上买卖价差。`strategy.entry_cost(p)` 和 `exit_cost(p)` 分别计入一半成本。

## 历史回测（971 场有弱队报价，D 共用 1555 场）

| 通道 | 交易数 | 单笔平均收益率 | 全损率 | 出场原因及次数 |
| --- | ---: | ---: | ---: | --- |
| A | 600 | +11.3% | 14.3% | 止盈 392 / 强制平仓 208 |
| C | 822 | +10.9% | 9.9% | 止盈 437 / **持有时间到期 326** / 强制平仓 59 |
| D | 159 | **+10.1%** | **0.0%** | 结算 90 / **分差止损 69** |

基准下注比例为 5%，D 通道使用 2.0 倍仓位，`MAX_OPEN=8`。模拟账户从 **$100 增至 $1,043.86**，按市值计算的回撤为 **$69.3**，峰值敞口为 $45，共有 1580 笔成交。每场平均盈亏为 +$1.057，自助抽样（bootstrap）的 95% 区间为 [$0.769, $1.345]。

D 通道一直持有到结算时，单笔平均收益率为 +6.5%，全损率为 18.2%。加入强队落后即卖出的分差止损后，样本中的单笔平均收益率为 +10.1%，全损率为 0%。

## 实盘行情回放

| 样本 | 交易数 | 单笔平均收益率 | 账户期末金额（初始 $100） | 回撤 |
| --- | ---: | ---: | ---: | ---: |
| 11 月 12 至 13 日的 14 场历史比赛 | 19 | +8.4% | $109.71 | $16.3 |
| 2026-11-14 的 9 场比赛 | 12 | +15.5% | $110.79 | $7.1 |
| 23 场合计 | 31 | +11.1% | **$120.49** | $16.3 |

2026-11-14，$30 实盘账户当天实际亏损 **$2.82**。在对齐 C 通道 30 分钟出场规则前的复盘中，按 D 通道分差止损重算，当天预计盈利约 **$3.9–4.3**。雷霆的 D 仓位持有到结算亏损 $7.61，若在 `m_fav < 0`、记录价为 0.675 时卖出，亏损约 $0.88。火箭的分差曾短暂变动 2 分后恢复，未触发该止损，结算盈利 $0.80。

## 文件说明

| 文件 | 用途 |
| --- | --- |
| `scripts/executor.py` | Python 策略与订单执行服务的统一入口 |
| `scripts/executor_producer.py` | 读取比分和盘口、计算信号，向执行器提交交易意图 |
| `scripts/executor_clob.py` | 实盘进程内使用的 Polymarket SDK 适配器 |
| `scripts/strategy.py` | A/C/D 策略的统一实现，用于回测和实盘行情回放 |
| `scripts/nba_data.py` | 读取数据、处理比赛时钟和 HTTP 请求、加载压缩数据包 |
| `scripts/live_runner.py` | 旧版 Python 实盘执行，每场比赛运行一个 `GameTrader` |
| `scripts/pm_creds.py` | 交易凭证辅助工具 |
| `scripts/run_live.sh` | 启动旧版执行脚本并检查代理，`--live` 启用真实下单 |
| `scripts/make_bundle.py` | 重新生成 `data/backtest_bundle.json.gz` |
| `scripts/replay_live.py` | 回放历史 14 场和 2026-11-14 的 9 场比赛 |
| `scripts/swing_probe.py` | 在模拟模式下记录盘口，供做市研究使用 |
| `scripts/test_live_runner.py` | 旧版执行逻辑的回归测试 |

## 回测与回放命令

```sh
python3 -I scripts/strategy.py                 # 971 场回测和账户模拟
python3 -I scripts/replay_live.py              # 回放 14 + 9 场实盘行情
```

## 数据

- `data/backtest_bundle.json.gz` 是包含 1555 场比赛的压缩回放数据集（42.8 MB）。与 `load_data_raw` 读取的记录一致，包含比赛元数据、强队价格历史、ESPN 逐回合事件和弱队价格历史。
- `data/live_probe/2026-11-12` 和 `data/live_probe/2026-11-13` 保存 14 场历史比赛的实盘采集数据。
- `data/live9_20261114.json` 保存 2026-11-14 的 9 场比赛，数据来自 ESPN、Gamma 和 CLOB。
- `data/live_full_prices*.json` 是实盘行情回放使用的 CLOB 价格缓存。
- `data/live_trade_final/2026-11-13` 和 `data/live_trade_final/2026-11-14` 保存实盘执行脚本的事件日志。
- Git 忽略原始数据 `data/games.json`、`data/diverse_opponents.json` 和 `data/cache/`。采集新比赛后，运行 `scripts/make_bundle.py` 重新生成数据包。

## 旧版 Python 实盘脚本

运行 `./scripts/run_live.sh` 可试运行，运行 `./scripts/run_live.sh --live` 会提交真实订单。回归测试命令为 `~/.polymarket/venv/bin/python -m unittest discover -s scripts -p 'test_live_runner.py'`。

### 如何并行处理比赛

`live_runner.py` 为每场正在进行的比赛运行一个 `GameTrader` 线程。

- 主线程每 3 秒轮询一次 ESPN，查找对应的 Polymarket 市场。开赛时创建交易线程，将每次比分更新传给它；比赛结束且持仓清空后释放该线程。
- 每个线程有自己的 CLOB 客户端、比分历史、信号状态、待处理的入场、持仓和出场检查。一场比赛处理出错时，脚本记录 `game_err`，其他比赛继续运行。
- 每次更新时，各线程并行检查自己持仓的出场条件。
- `ALL_POSITIONS`、`MAX_OPEN` 和买卖确认标志由各线程在锁保护下共享。每次产生信号前读取一次账户可用资金。

### 旧版脚本的执行修复

1. **重复下单。** 脚本在内存中跟踪已发送的请求。等待窗口内，它查询 `get_trades`，优先核对订单号和 `taker_order_id`，并阻止同一信号再次发单。到期仍未查到成交时，脚本会在没有其他已知仓位或未决请求共用该标的时检查持仓增量。这条旧路径的超时和重启限制见[执行后端需求文档](reports/go-order-execution-requirements.md)。
2. **卖出记账。** 每个仓位只卖出自己的份额，按成交记录确认。剩余不足 0.02 股的零头直接在账本中关闭，避免持续重试 15 分钟。
3. **共享标的。** A、C 通道买入同一个代币时，各自仅卖出本通道持有的数量。
4. **处理 400 `invalid amounts` 错误。** 市价买单强制使用 0.01 的报价单位。0.001 的舍入配置会生成 5 位小数的份额，服务器会拒绝该精度。
5. **入场价。** 有成交明细时，使用按成交数量加权的实际成交均价。
6. **重启恢复。** 按通道从已买数量中扣除已卖数量，重建持仓。共享代币余额按各自记录的数量分配。保留原始入场时间和入场时的比赛时钟，让 C 通道的定时出场在重启后继续计时。

## 已知限制

- C 通道的 30 分钟持有时间和 D 通道的分差止损参数都用同一批 971 场比赛选定。这些参数反映了样本内的风险取舍，仍缺少样本外验证。
- D 通道有 159 笔回测交易；实盘行情回放只捕捉到 2 笔 D 交易。在 9 场样本中，雷霆和火箭的 D 入场价位落在分钟级记录价格覆盖范围之外。
- 分钟级价格记录低估了实时买卖报价的影响。实盘采集观察到 29–59 秒的价格滞后，以及 3–4 美分的不利入场滑点。
- 样本中的策略期望收益为正，收益分布有厚尾。A/C 通道的全损率仍约为 10–14%，小账户的每日盈亏波动较大。
- 2026-11-14 的实盘账户还包含一笔在执行脚本之外完成的鹈鹕往返交易（亏损 $0.11）。该交易账户应只由执行脚本下单。
