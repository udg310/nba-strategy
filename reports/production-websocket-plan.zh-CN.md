# 实盘 WebSocket 方案：Polymarket 盘口与 ESPN 比分

**建议先接 Polymarket 盘口 WebSocket。ESPN 比分保留 HTTP 作为交易依据，同时接入 BrightSports 做同机对照。** 两路可以独立切换。当前基线 `432b672` 的比分主循环每轮处理后等待 3 秒，策略评估还会分别请求盘口价格与深度。目前没有本项目部署机上的双路延迟实测。[比分循环](../scripts/live_runner.py#L1389-L1406) · [轮询间隔](../scripts/live_runner.py#L1525) · [盘口读取](../scripts/live_runner.py#L550-L563)

## 1. Polymarket 盘口：WebSocket 和 HTTP 各有什么取舍

| 维度 | WebSocket 推送 | HTTP 轮询 |
| --- | --- | --- |
| 时效 | 长连接建立后，交易所发布盘口变化即可更新本地报价。比赛得分时，策略可立即读取当时已收到的盘口。 | 只能在下一次请求时看到当时的盘口，两次读取之间的短暂价格可能错过。 |
| 请求与一致性 | 按双方 token 订阅，在同一版本的本地订单簿上取买一、卖一和前五档。缓存新鲜时，一次信号评估可以做到 **0 次盘口 HTTP 请求**。 | 代码简单，单次全量快照便于校验。当前 `book()` 和 `book_depth()` 各请求一次，价格和深度可能对应不同时刻。 |
| 故障与成本 | 要维护订阅、快照和增量。断线期间可能漏更新，连接心跳也不能证明某个 token 的报价仍新鲜。 | 每次成功读取都能重建当前盘口，容易恢复。多场同时评估时，请求量和等待时间会增加。 |

**接入办法。** 建议增加 `--market-data` 的 `http`、`observe`、`ws` 三种模式，默认沿用 HTTP。观测模式只记录差异。启用推送后，基于完整快照应用价格档增量和 tick 变化，再从完整订单簿截取前五档。断线、发现缺口或该 token 报价超过建议初值 3 秒时，按需读取 HTTP；重连取得新快照后恢复使用推送。发单前仍独立复核价格、原始限价和信号期限。成交份额继续按订单回执核对，稻香页面沿用 SSE。[Polymarket 官方市场流](https://docs.polymarket.com/market-data/realtime-data) · [现有盘口函数](../scripts/live_runner.py#L255-L284)

## 2. ESPN 比分：BrightSports relay 值不值得用

**值得先做观测。** 它把 ESPN FastCast 的 NBA 主题 `scoreboard-basketball-nba` 还原成单场快照，再发布到 ntfy 的 `bs-<ESPN比赛ID>` 主题。我们的 runner 可先用 ESPN HTTP 发现当日比赛，再订阅相应主题的 WebSocket（`/bs-<比赛ID>/ws`），读取比分、主客队、节次、比赛时钟和终场标记。公开实例还经过 Cloudflare Tunnel；同机自建可让 runner 直接读本地 ntfy。[BrightSports README](https://github.com/gi-os/BrightSports/blob/91219161aa7f49de159b9a471b9279bbac6794a8/relay/README.md) · [ntfy 订阅接口](https://docs.ntfy.sh/subscribe/api/)

| 优势 | 代价与限制 |
| --- | --- |
| 有机会在下一轮 HTTP 查询前收到得分，保留两次轮询之间的连续比分变化。公开实例便于快速验证，自建可省去公网转发。 | FastCast 协议来自项目作者对 ESPN 客户端的观察。公开实例还增加第三方服务和网络跳数，每球能提前多久需要在我们的机器上实测。 |
| relay 已实现全量快照、增量补丁、重连和按场分发；ntfy 缓存能补读已经成功发布的消息。 | 转发的是变更后的比赛快照，一批补丁可能合并为一次快照。代码先保存新状态，再向 ntfy 发布；发布失败时只记日志。若丢的是最后一次终场变化，重新订阅也可能收不到它。 |
| 消息带有来源 `src`，可以分别统计实时补丁、全量快照和 HTTP 纠偏。 | 消息 `ts` 是 relay 准备发布快照的时间。部署示例配置的 HTTP 纠偏间隔为 300 秒，无法兜住几秒钟的交易窗口。自建还要维护 FastCast 适配、relay 和 ntfy。 |

以上按 BrightSports 提交 `9121916` 的实现核对。公开实例在部署机上的可达性、延迟和完整率仍待实测。[FastCast 接收逻辑](https://github.com/gi-os/BrightSports/blob/91219161aa7f49de159b9a471b9279bbac6794a8/relay/fastcast.py) · [发布与纠偏逻辑](https://github.com/gi-os/BrightSports/blob/91219161aa7f49de159b9a471b9279bbac6794a8/relay/main.py) · [部署配置](https://github.com/gi-os/BrightSports/blob/91219161aa7f49de159b9a471b9279bbac6794a8/relay/docker-compose.yml)

## 3. 怎么验证，再决定是否切换

1. **先比较到达时间。** 在同一进程给 HTTP 和 relay 打本机单调时钟时间戳，按比赛 ID、节次、主客比分及变化顺序匹配同一次得分。计算“HTTP 首次看到时间 − relay 首次看到时间”，正值表示 relay 先到。记录中位数、尾部延迟、谁先到的比例、单路漏报、改判及终场缺失；同时保存两路到达时各自看到的买一、卖一。这样能看出比分即使早到，盘口是否仍有差别。
2. **补好交易入口再切比分源。** 当前 `GameTrader.put` 会用最新快照覆盖排队中的旧快照。若 relay 要驱动交易，同场得分需按接收顺序处理，重复消息去重；重连补读只用于追平状态。遇到比分回撤、主客队不符或状态缺口，回到 ESPN HTTP 核对并重建基线。终场独立确认；同一得分只交给一个来源触发交易。[比赛队列与信号入口](../scripts/live_runner.py#L448-L506)
3. **两路分别验收。** 盘口用断线、过期和重订阅回放验证回退；比分用连续得分、改判和漏终场回放验证。若多场实测中 relay 稳定先到、覆盖完整，且回放不会重复触发，再考虑让它驱动交易。否则继续使用 ESPN HTTP，保留 relay 作观测；盘口 WebSocket 可单独启用。[实盘执行问题与验收](production-execution-issues.zh-CN.md)
