# 个人账户接入与 Telegram 告警

本指南使用当前统一入口 `scripts/executor.py`。比分交易依据仍是 ESPN HTTP；`--market-data ws` 使用 Polymarket 公开盘口推送，单个 token 没有完整快照、断线或 3 秒未更新时按需回退 HTTP。`--score-data observe` 只把 BrightSports 与 ESPN 的比分到达时间记入本地文件，不让 relay 触发交易。

## 1. 先验证模拟盘

在项目根目录的 PowerShell 中运行：

```powershell
python -m venv var\executor\venv
$py = ".\var\executor\venv\Scripts\python.exe"
& $py -m pip install -r scripts/requirements-executor-live.txt
$env:NBA_PROXY = ""  # 可直连时；需要代理时改成自己的 http://主机:端口
& $py scripts/executor.py --scoreboard --market-data observe
```

需要代理时，还要让签名 SDK 和 Polygon RPC 使用同一代理：`$env:HTTPS_PROXY = $env:NBA_PROXY; $env:HTTP_PROXY = $env:NBA_PROXY`。

`observe` 始终用 HTTP 报价交易，只记录推送差异。观察连接与报价后，可用 `--market-data ws` 在模拟盘验证断线回退。要比较比分来源，设置自己的 BrightSports/ntfy 地址并加上 `--score-data observe`：

```powershell
$env:NBA_BRIGHTSPORTS_URL = "https://你的-ntfy-地址"
& $py scripts/executor.py --scoreboard --market-data observe --score-data observe
```

比较数据写在 `var/executor/score_observations.jsonl`。同一场、节次、比分与状态的 `http_minus_relay_ms` 为正，表示 relay 先到。公开 relay 是第三方服务，只有确认可达、字段和完整率后才用于观测；本版本不会让 relay 驱动交易。

## 2. 接入自己的 Polymarket 账户

先在 Polymarket 个人资料里复制**账户钱包地址**，确认它与准备使用的 signer 配套。本项目目前要求你能提供该 signer 的私钥；没有可用 signer 时，不能用账号密码替代。2026 年 5 月 4 日以后创建的账户通常是 Deposit Wallet，对应签名类型 `3`；旧 Proxy/Safe 钱包需要按钱包实际类型设置 `1`/`2`。切勿把浏览器钱包地址误填成资金账户，也不要把私钥、API 密钥、Telegram token 发给任何人。参见 [Polymarket 钱包与身份验证](https://docs.polymarket.com/trading/wallets-auth)。

PowerShell 中先设置 signer 私钥、资金账户和签名类型。`Read-Host` 输入的值不会作为命令写入 shell 历史；以下环境变量只对当前终端有效。

```powershell
$env:POLY_PRIVATE_KEY = Read-Host "Signer 私钥（仅在本机输入）"
$env:POLY_FUNDER = Read-Host "Polymarket 账户钱包地址"
$env:POLY_SIGNATURE_TYPE = "3"
& $py scripts/pm_creds.py
$creds = Get-Content "$HOME\.polymarket\creds.json" -Raw | ConvertFrom-Json
$env:POLY_API_KEY = $creds.api_key
$env:POLY_API_SECRET = $creds.api_secret
$env:POLY_API_PASSPHRASE = $creds.api_passphrase
```

辅助脚本在本机生成或派生 CLOB L2 凭证，不会把私钥写入仓库。若账户使用旧钱包，先核对实际类型再改 `POLY_SIGNATURE_TYPE`。执行器还需要可访问的 Polygon HTTPS RPC 地址、用于加密待发订单的随机密钥，以及本机管理接口的独立令牌：

```powershell
$env:EXECUTOR_POLYGON_RPC_URL = Read-Host "Polygon HTTPS RPC URL"
$env:EXECUTOR_ORDER_KEY = & $py -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
$env:NBA_EXECUTOR_TOKEN = & $py -c "import secrets; print(secrets.token_urlsafe(32))"
```

**保存并在每次重启时恢复同一个 `EXECUTOR_ORDER_KEY` 和 `NBA_EXECUTOR_TOKEN`。** 更换订单加密密钥可能让未决订单无法恢复。不要把这些值贴到聊天、提交到 Git 或写进启动命令参数。建议使用系统的凭证保管工具。执行器账本也要保留原路径；独立空仓账户最容易首次接入。账户须有相应可用资金和交易权限。

第一次只初始化账本，不下单。下面示例的 `$10` 初始资金是 `10000000` 微美元；按自己愿意投入的额度修改，且不要超过账户的可用资金：

```powershell
$db = (Join-Path (Get-Location) "var\executor\my-live.db")
$capital = 10000000
& $py scripts/executor.py --init-live --mode live --account $env:POLY_FUNDER --db $db --capital $capital
```

之后在**同一账户、同一账本和相同初始资金**上启动。先用 `--market-data observe` 核对盘口，再改用 `ws`：

```powershell
& $py scripts/executor.py --scoreboard --mode live --account $env:POLY_FUNDER --db $db --capital $capital --market-data observe
# 验证后，下次启动可把 observe 改成 ws
```

看到 `executor ready` 和 `strategy started` 表示进程已运行；这不代表任何订单已经成交。`http://127.0.0.1:8787/healthz` 可看服务状态。核对余额、未决订单和成交请用 README 中的受令牌保护接口或只读看板。首次真实下单前，确认凭证对应的账户、SDK 探测、RPC、可用余额和订单查询都成功；不要让旧实盘脚本与此执行器同时操作同一账户。

## 3. 接自己的 Telegram

在 Telegram 找 [@BotFather](https://t.me/BotFather)，发 `/newbot` 创建机器人并保存 token。随后打开**自己的机器人**，给它发 `/start`。机器人不能主动与未联系过它的私聊用户开聊。用下面的命令读取自己的 `chat.id`；若返回多个更新，选自己私聊发送的那条。`getUpdates` 是 [Telegram 官方 Bot API](https://core.telegram.org/bots/api) 的方法。

```powershell
$env:NBA_TELEGRAM_BOT_TOKEN = Read-Host "Telegram Bot token"
$updates = Invoke-RestMethod "https://api.telegram.org/bot$($env:NBA_TELEGRAM_BOT_TOKEN)/getUpdates"
$updates.result | ForEach-Object { $_.message.chat | Select-Object id,type,username }
$env:NBA_TELEGRAM_CHAT_ID = Read-Host "你的私聊 chat.id"
& $py scripts/telegram_alerts.py
```

收到测试消息后，按第 2 节启动执行器即可。它在**交易所成交已确认**时通知比赛、策略通道、方向、份额和价格；执行器发现需人工核查的情况也会通知。发送失败只记本地日志，下一轮会重试，仍须以执行器账本和交易所为准。第一次启用时不会补发此前历史成交。Telegram 两个环境变量也需在每次重启时恢复。若 `NBA_PROXY` 需要代理，Telegram 请求同样走它。

## 使用边界

- `--market-data http` 是默认模式。`observe` 比较推送与 HTTP，`ws` 在缓存有效时使用推送。发单前，执行器仍用交易所 HTTP 重新核对市场、价格、余额和订单期限。
- 盘口 WebSocket 无可用的逐条序列号，客户端通过完整快照、增量最优价一致性、时间戳与 3 秒过期窗口发现常见不一致；不能证明绝无漏包。故障时回退 HTTP。
- 比分 relay 只观测。当前 ESPN HTTP 轮询仍可能错过两轮之间的短暂变化；未完成文档中的多场延迟和完整率验收前，不启用 relay 交易。
- 本项目只有离线回归验证，尚未用你的真实账户、Bot token 或部署网络做端到端实测。
