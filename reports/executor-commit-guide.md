# 从一条比分到一笔入账：逐文件读懂 fa4ea64

比较范围是父提交 `6fb4cde` 到 `fa4ea64`（Run NBA strategy and SQLite order execution in one Python process）。共 **27 个文件，22 个新增、5 个修改，增加 4,434 行、删除 44 行**。父提交已有 Python 策略、逐场交易脚本和历史数据，树中没有 Go 执行器源码、Go 模块或本次新增的 Python 执行器。本次差异是给已有策略增加另一条执行路径，并让旧脚本与新服务共用账户锁。文档中出现的 Go 路径、兼容格式和其他评审基线各有来历，不能计为父提交中被替换的文件。[固定提交差异](https://github.com/udg310/nba-strategy/commit/fa4ea6480484b90fea1c9d2ac2b90af09fe1caf6)

| 想回答的问题 | 阅读入口 |
| --- | --- |
| 为什么一次信号可能产生两张单，业务要求怎样处置？ | [统一需求及验收](executor-requirements.md) |
| 哪个组件持有发送权，报错后往哪走？ | [执行流程图](executor-flow.html) · [执行循环](#file-engine) · [事务账本](#file-ledger) |
| 哪些字段落在哪张表，每条 SQL 的用途是什么？ | [基础表结构](#file-schema) · [SQL 逐语句索引](#sql-catalog) |
| 启动、模拟回放和真实场所分别经过哪里？ | [服务入口](#file-main) · [策略生产者](#file-producer) · [交易场所](#file-venues) · [签名和交易所协议](#file-clob) |
| 哪些保证有具体离线断言？ | [测试文件与验收点](#tests) |

先区分四个编号。策略用比赛、通道和动作标记信号。账本的 `intent_id` 标记一次业务决定，`attempt_id` 标记为它争取到的一次发送机会，`order_id` 标记场所订单。同一订单还可有多条成交，每条以 `(order_id, fill_id)` 去重。因而“请求已受理”“已发单”“已确认成交”“账户侧已对平”分别有自己的证据。以下小节的增删行数均来自这一次 Git 差异。

[本次核验记录](#verification) · [SQL 全部条目](#sql-catalog)

## 运行、说明与历史需求

### 01 · `.github/workflows/executor.yml`（新增 17 行）

名为 `Python executor` 的 GitHub Actions 工作流在 push 和 pull request 上运行。矩阵覆盖 Ubuntu、Windows 和 macOS，使各自的文件锁和路径处理经过测试。每个任务用只读仓库权限检出代码，设置 Python 3.12，安装 `scripts/requirements-executor-live.txt`，再用 `unittest discover -s scripts -p "test*.py"` 运行脚本目录所有匹配测试，覆盖旧 runner。命令的 `-B` 禁止写 Python 字节码。安装实盘 SDK 使带依赖条件的离线签名测试可执行。该工作流未配置实盘账户或发单步骤。

### 02 · `.gitignore`（增加 1 行）

新增根目录规则 `/var/executor/`。默认模拟账本 `paper.db`、SQLite 辅助文件和 `paper.db.token` 都生成在此处，运行产物不会作为新的未跟踪文件出现。该匹配限定为仓库根下的目录，自定义 `--db` 放在其他位置需另行考虑。已经被跟踪的文件也不会因这条规则退出 Git。

### 03 · `README.md`（增加 109 行、删除 41 行）

| 阅读位置 | 父提交的介绍 | 本次面向使用者的变化 |
| --- | --- | --- |
| 首页与启动 | 以策略名为标题，列出策略、数据、旧实盘三处分工 | 标题直接给出 `python scripts/executor.py --scoreboard`。注明默认 paper、何时看到服务与策略启动提示、如何停止，以及仅运行服务的命令。增加中文入口 |
| 行情与本地数据 | 主要讲打包的回测记录、旧 runner 日志 | 增加默认本地代理、SQLite 与令牌位置、健康检查、整套九场录制回放和独立进程回放单场。paper 以限价和零费用成交，研究用历史回测另有命令 |
| 操作台 | 旧说明侧重每场一个交易线程 | 新增按令牌查询比赛与报告的 PowerShell 例子，列出意图、报告修订、指标、暂停开仓、追踪、人工复核和 D 兑付接口。交代金额/份额精度与时间戳 |
| 实盘与上线 | Run 小节直接列旧 shell 启动器 | 新增 SDK、Polygon RPC、密钥和令牌配置，解释先存签名身份再 POST、未知结果的恢复。给出停原写方、盘点敞口、一次初始化、按同一账户库重启与 WAL 备份顺序。另列开发测试命令和当时两份需求入口 |
| 策略、样本与旧脚本 | 有 A/C/D 规则、回测与 23 场回放、旧脚本修复等 | 将规则表改成英文，保留核心样本数字。区分 11 月 14 日账户实际 −$2.82 与按 D 止损回放估计 +$3.9–4.3。将收益论断限于样本。文件表补执行器、生产者、CLOB 模块，原启动器和逐场线程说明归入旧版一节 |
| 重复发单说明 | 用绝对措辞描述等待期间防重和成交确认 | 具体说明旧脚本依赖内存等待、按订单号查成交、特定条件下用余额增量兜底，以及超时和重启边界。实际成交均价也限定为取得成交明细时 |

仓库网页的回测收益仍描述历史研究结果。本提交修改的是文档和执行链，`scripts/strategy.py` 与比赛数据没有进入这份差异。

### 04 · `README.zh-CN.md`（新增 179 行）

新增完整中文手册，可与英文 README 互跳。开篇示范仓库根目录、代理、两条就绪日志、令牌、数据库与健康检查。无比赛时用九场录制验证，或在独立生产者进程指定比赛。操作章节给出 Bearer 请求、查比赛再查报告的 PowerShell 示例、端点清单和 D 兑付证据字段。实盘章节说明 SDK 依赖、只读探测、账户切换与固定账本。初始化与持久化重启是两个命令。后半部分对应策略规则、研究结果、文件和数据索引、旧版每场线程、六项旧修复与样本局限。旧版“少于 0.02 股关闭”仅出现在旧脚本说明中，新账本的零头政策仍有待实现。

### 05 · `reports/go-executor-live-reliability-requirements.md`（本提交新增 51 行）

这是标为“待排期”的后续需求，原文评审基线为 `e397a54`。第一项用 5.109588 股说明剩余份额：按 0.01 股步长卖出 5.10 后，实际仍有 0.009588 股。要求在开平仓尝试和相关成交均已核实后记录剩余敞口、结束自动退出并释放持仓名额，重启、重复请求和 A/C 同标的不能改变归属。列出的 0、0.009999、0.010000 和 0.019588 股是边界验收输入。现有账本按实际未平数量占名额，paper 也未模拟实盘的卖出步长。

第二项针对旧单查询失败或挂起占满四个工作位，提出按尝试持久化 1/2/4/8/16/30 秒退避、为发单和查询都保留执行机会、恢复同一调度计划、返回真实下次查单时间和失败原因。它还规定上游重放应沿用原截止时间，逾期未发与逾期已发须分别清理/对账。文末给出旧库升级和故障注入验收。当前代码已有工作位上限、账户同步门槛和截止检查，尚未提供这套公平调度。本文档整理后，旧文件从工作树移除，保留当时版本和统一入口。[固定提交原文](https://github.com/udg310/nba-strategy/blob/fa4ea6480484b90fea1c9d2ac2b90af09fe1caf6/reports/go-executor-live-reliability-requirements.md) · [统一需求](executor-requirements.md)

### 06 · `reports/go-order-execution-requirements.md`（本提交新增 127 行）

原始评审稿从比赛 `401909304` 的雷霆 D 单出发。日志记下预算 $3.73、`tries=2` 与本地 5.109588 股，复盘记录实际约 10.22 股，缺少两张可逐笔核对的外部订单号。另一个可重复的证据是父提交的离线模拟：首单已受理但成交及余额不可见，16 秒后旧 runner 放弃等待，再遇比分变化又提交同通道。两组证据的强度不同。

原稿要求同账户单一实盘写方，Python 提供信号，Go 承担风控、签名、订单、成交、恢复，SQLite 串行写入。依次定义开仓/退出业务键、尝试与明细标识、发包前后宕机、零成交重试前提、持久化预留、重叠分页、人工归属、账户暂停、指标与比赛报告。P1 另列实时面板与按场注入故障的回放。末尾列出建库、迁移、扩容条件、八类离线验收，以及影子运行、单一写方接管和回退。其 `go run`、goroutine 等是当时的技术方案。本提交实现绑定到 Python 模块。合并整理后可通过 Git 查看已移除的旧稿，当前要求集中在统一文档。[固定提交原文](https://github.com/udg310/nba-strategy/blob/fa4ea6480484b90fea1c9d2ac2b90af09fe1caf6/reports/go-order-execution-requirements.md) · [统一需求](executor-requirements.md)

### 07 · `scripts/README.md`（增加 7 行、删除 2 行）

原目录手册直接列 `live_runner.py` 和 `run_live.sh` 为实盘入口。本次在文件表前增加从根目录启动 paper 的命令、英中两份说明链接，在表中加入 `executor.py` 的策略/工作线程/本机 API 分工，并标记旧 runner 与旧 shell 启动器。后面的策略表、旧版运行示例和逐场线程介绍继续保留。读其 `--live` 示例时，应结合这一入口归属。

## 执行服务与数据流

<a id="file-main"></a>

### 08 · `scripts/executor.py`（新增 137 行）

- **命令行边界。** `loopback` 解析回环 IPv4 或 IPv6 与端口 0–65535。0 可分配临时端口。`options` 让 `--scoreboard`、`--recording`、`--init-live` 互斥。默认 paper/local、127.0.0.1:8787、初始 100,000,000 微美元和最多 5 个持仓。录制只允许 paper，初始化只允许 live。实盘须指定数据库和与 `POLY_FUNDER` 相符的账户。固定绝对路径是运行手册的部署约定，参数解析只检查是否提供路径。
- **建库与令牌。** `prepare_ledger` 创建目录，一次初始化以独占方式新建文件，普通实盘启动要求现有非空库。`bearer_token` 优先读环境变量。live 必须显式给出，paper 按数据库路径派生 `.token`，用独占创建或读回旧值，空令牌文件报错。
- **生命周期。** `run` 先锁账户，再开 Ledger 和 Reports。初始化模式到此退出。平时再选择 PaperVenue/LiveVenue、构造 Engine、恢复未决尝试、开放 API 并启动后台线程。构造实盘场所的探测失败会中止启动。之后 `engine.recover()` 的对账异常会记警告并继续提供服务，后台靠账户同步新鲜度限制发单。因此健康接口可读与全部订单已经核清是两件事。
- **业务及结束。** 录制模式运行完退出，实时模式跟随比分，两者均使用进程内 API。服务模式每 0.5 秒空闲等待信号。退出顺序依靠 `finally` 和上下文管理器依次停止工作线程、关闭 HTTP 服务和账本、释放账户锁。入口把 SIGTERM/可用时的 SIGBREAK 转成中断，常见配置、存储或账户冲突输出停止原因。

### 09 · `scripts/executor_api.py`（新增 144 行）

`route_parts` 先去查询串、按斜杠切分，再对每段 URL 解码，让已编码的比赛 ID 保持一个参数。`ExecutorAPI.dispatch` 处理 GET 健康探针（账本内 `SELECT 1`）、POST 开平仓意图、D 兑付和立即对账，其他路径交给 Reports。意图缺省账户或环境由服务补齐。新建返回 201，等价请求返回 200 并增加重复计数。不可兼容请求计冲突，风控拒绝单独抛出。兑付重放也返回 200，首次 201。立即对账可写本地观测与报告，交易所侧只读。

内置生产者通过 `request` 直接调同一分发函数，把风控与冲突转成带 JSON 详情的 HTTPError 409，复用独立客户端的跳过逻辑。`Handler.setup/respond/handle_request` 限制连接等待 15 秒，用恒时比较检查健康端点以外的令牌。只接受 JSON 对象、拒绝传输编码，决策与人工复核请求体上限 64 KiB，其余 128 KiB。参数错误、冲突、缺对象和内部错误分别映射为 400、409、404 和 500，缺鉴权为 401。成功与失败都返回带长度的 UTF-8 JSON。`serve` 按 IPv4/IPv6 创建服务器线程，停机时等在途 HTTP 请求完成后再交还账户锁。

<a id="file-clob"></a>

### 10 · `scripts/executor_clob.py`（新增 498 行）

这是实盘协议边界。下列行为均指固定提交中的实现。

| 操作 | 输入、检查及产生的证据 |
| --- | --- |
| `CallDeadline`、`remaining`、`_before_request` | 同时跟踪单调时钟预算、停止事件、订单墙钟截止和 POST 是否已进入传输。锁让放弃调用与获得传输机会互斥。同一操作只能进入一次 POST。只读操作出现写请求即报错。HTTPX 每次请求设置最长 5 秒的分段超时，发送请求头前重新检查期限 |
| `client`、`rpc`、`execute` | 客户端要求 SDK 1.2.2，固定 Polygon 链 137、CLOB 主机、凭证与签名模式。安装共享请求钩子、关闭自动重试和 POST 后 SDK 自行等待结算的循环，压低可能打印敏感数据的传输日志。`execute` 用上下文变量为一次调用设置操作和预算，并在退出时复原。`rpc` 读取配置的 Polygon RPC，单次读取最多 7 秒且受总预算限制 |
| `prepare` 验市场与价 | 要求实盘账户与资金账户一致、代币 ID 为十进制，核对订单簿与市场返回的条件/代币映射。买看最低卖价、卖看最高买价，越过限价可标可重试。买方计算成本后再查 D 的 0.70–0.80 或 A/C 的 0.10 下限。限价须满足场所 tick。细于 0.01 的买单向下取到 0.01，再验卖价仍可成交 |
| `prepare` 验资金与签名 | 选择普通或负风险 V2 合约，对买单查抵押资金，对卖单查该 token 的余额及 allowance。两者取小，必须覆盖本次及合计预留。买单按费率留出预算，拒绝不支持的费用曲线。金额或股数向下保留两位，小于最小精度拒绝。用 SDK 签 FOK（需全量成交，否则撤销）的 V2 市价单并从 typed data 计算订单哈希，返回签名字段与哈希 |
| `classify_post` 与 `_execute("post")` | POST 使用已签载荷。过期未传输可判零，结构化 400 进入分类。匹配哈希且 success 或状态为 DELAYED/MATCHED/LIVE 可判 ACCEPTED。仅两种明确 FOK 无法成交文案且无相反证据可标可重试的 REJECTED_ZERO。回执哈希冲突、泛化错误、重复单消息或证据不足保持 UNKNOWN |
| `confirmed_legs`、`settlement_reader` | 只解析账户交易状态 CONFIRMED 对应的成功链上回执，并核对交易哈希、可信 V2 合约、事件 topic 和资金钱包。已移除或格式错误的匹配日志报错。按 side 从七个 32 字节字段取现金、股数和费用，价格用整数四舍五入，ID 为“交易哈希:日志序号”，时间取出块时间。缓存同一读取过程中的交易回执。读到确认的账户交易却没有支持的己方明细时报错，避免静默漏记 |
| `order_snapshot`、`_execute("lookup")` | 核对订单 ID、方向、标的、钱包和市场。交易所累计匹配量必须可精确换算为微股。取消、到期、匹配或成交可成为 FINAL 快照，标为已匹配/成交却为零时报错。遍历订单的关联交易号，按本单哈希提取并去重链上明细。账本还会验证明细是否赶上累计量 |
| `_execute("probe"/"trades")` | 启动探测核对 RPC 链号并读取账户成交页。同步时用 Base64 JSON 游标保存 after、固定 before、翻页标记和最早未完成交易时间。回读前 300 秒以接住迟到明细，页内按成交 ID 去重。末页只推进到最早未决交易或窗口末端。缺少分页标记报错 |

<a id="file-engine"></a>

### 11 · `scripts/executor_engine.py`（新增 155 行）

1. `recover` 跨日期扫描账本待办，将遗留的 SUBMITTING 记成 UNKNOWN，再 `reconcile`。后者先同步账户成交，再用最多四个线程查询非 READY 的未决尝试，刷新受影响的报告。账户拉取或某次查单失败也会继续其他核查，最后抛出首个异常。这条入口没有发送操作。
2. `step` 读取意图。非 READY 交给 `_lookup`，仅活跃尝试按原身份查单并 `apply`。READY 先调用 `claim`，风控拒绝或竞争落败就返回。获权事务已提交才调用 `venue.submit`。异常或不能明确归为受理/零成交的结果记 UNKNOWN，再按当次尝试写回执。
3. `_sync_feed` 串行取得上次游标。每页由 `Reports.observe` 原子保存，最多追 128 页。遇空的下一游标或游标稳定才更新最近成功时间。中断、超过页数或读取失败将其置零。已提交的中间页仍可在下一轮续读。
4. `_poll_feed` 每 2 秒同步、刷新报告，并在告警摘要变化时输出。独立的 `_dispatch` 每 0.5 秒回收完成任务，以账本 `last_check,id` 顺序送入四个工作位。同一循环避免同时处理同一意图。账户数据未同步或超过 30 秒时跳过 READY，仍查已发订单。由此也会延后过期待发意图的清理。查单与发单共用这四个位置，未持久化失败退避或给新单保留位置。
5. `start` 避免重复启动，`stop` 设置共享停止事件并等待两个循环结束。线程池退出也会等待在途任务。实盘场所用自己的预算和后台 SDK 调用边界处理长时间无响应的网络操作。

<a id="file-ledger"></a>

### 12 · `scripts/executor_ledger.py`（新增 519 行）

账本是资金和股数的权威记录。`amount` 在开仓时是微美元预算、平仓时是微股份额。`filled` 是确认股数，`cost` 是确认成交金额（卖出时是所得），`fee` 单列。价格也乘以一百万。`Invalid` 表示输入不合约定，`Conflict` 表示与已有事实冲突，`Risk` 是风控拒绝这一类冲突。

| 函数组 | 事务内解决的问题 |
| --- | --- |
| `integer/string/boolean/identifier/public_intent/read_intent` | 严格区分整数和布尔值、校验范围、产生 26 位大写 Base32 字母表标识。外部表示省略未填写的可选 ID/理由。查不到意图抛 KeyError。金额/份额请求上限为 10¹⁵，时间按有符号 64 位整数校验 |
| `Ledger.__init__/_initialize/tx/close`、`execute_schema` | 单连接加可重入锁，SQLite 等锁上限 3 秒，启用 WAL、FULL 同步、外键。0 版装基础表，0/1 版加账户暂停表后升至 2。未知版本、库主/环境或初始资金不匹配停止。DDL 在同一事务内执行。`BEGIN IMMEDIATE` 也序列化其他连接的写者。嵌套调用用保存点，可只回滚内层操作 |
| `cash_committed/available/_check_hold` | 初始资金减开仓成本、费用、资金预留，加卖出净所得及兑付净额，再计入已标手工且确认的外部交易。已知费用超预算时可用额可以变负。暂停只挡新开仓。已确认且未归属的外部成交挡所有新开仓，也挡同资产平仓，其他资产可继续退出 |
| `_request/_reserve/create` | 只接受声明字段、A/C/D 与 OPEN/CLOSE。账户和环境须匹配，价格 1–1,000,000，信号时间为正、截止时间不早于它。首次开仓需可用资金与名额。首次退出需指向同场同通道同市场/标的、已核实终态、未兑付的开仓，数量恰为确认买入量。业务键命中时核对身份与原条款。同开仓改预算或限价冲突，等价重放复用旧 ID 和期限。创建时即已到期的请求记录 EXPIRED_UNSENT、预留为零（平仓仍验仓）。有效请求与预留、首次事件一次提交 |
| `_save_close_request/_existing_close` | 每个退出意图按 signal_at 留存请求条款。重放同一信号校验金额和限价。允许后续信号在已核实 PARTIAL_FINAL、REJECTED_ZERO 或 EXPIRED_UNSENT 时重新激活同一意图。要求信号晚于上次检查和原信号、期限仍有效、数量等于原目标或核实余量，检查未兑付和待核限制。只把真实余量重新预留，原成交保留 |
| `get/pending/claim/current_attempt/attempt_state/_bind_order` | 扫描 READY 和四种活跃态并按最早检查时间排序。领取 READY 时先检查到期，已有部分成交的到期单收为 PARTIAL_FINAL，其余为 EXPIRED_UNSENT，均释放预留。仍有效则重新核查暂停、未归属成交及开仓资金。产生唯一尝试并与 SUBMITTING、事件一起提交。重复领取返回未获权。后续只更新仍是当前且活跃的 attempt，已有交易所订单号和新号不符会报冲突 |
| `record_submit/_finish_attempt` | 回执能推进受理或未知。已经 ACCEPTED/PARTIAL 不让迟来 UNKNOWN 回退，也拒绝与已受理冲突的零成交声明。只有明确可重试的首次零成交、仍在窗口内，才允许第二次尝试。已核实的部分平仓可用原期限继续处理余量。结束不再重试则释放预留 |
| `record_fill/fill_cost/apply/update_intent/append_event` | 确认分录需订单、资产、方向和可选比赛 ID 一致，校验份额、价格、费用。金额优先用明确 notional，缺省按整数四舍五入计算。`(order_id,id)` 重放去重，确认数字被改写使整批回滚。成交、累计额、剩余预留和事件同步更新。`apply` 还记录本次尝试累计匹配量的历史最高值，吸收未最终确认但可见的明细。仅交易所 FINAL、确认明细和最高值三者数量一致时结清。完全核实的正数买入即 FILLED，不要求花完预算。卖出不足目标记 PARTIAL_FINAL 或回到 READY。零成交须与既有观测相容 |
| `settle` | 接受经核实的 D 仓位兑付。要求唯一的场所事件+兑付腿、操作者、证据、confirmed、股数及净额约束（费用≤兑付≤股数）。开仓已核实、无在途退出，兑付须正好覆盖买入减卖出余量。同一经济条款复用，冲突的仓位或兑付腿拒绝。一次写入兑付和事件。可用额与名额从账本重新计算 |

例如用 3,000,000 微美元开仓，价格为每股 750,000 微美元，手续费为零。受理时先冻结 3,000,000，丢回执时继续冻结。若先查到已确认的 1,000,000 微股，账本当场记入持仓并把资金预留降至 2,250,000。交易所订单仍可处于 OPEN。其余 3,000,000 微股确认时再增量入账，预算用足后预留为零。只有终态累计量、已确认明细和历史匹配最高值均为 4,000,000 微股时，这次尝试才结束跟踪，此后可用开仓 ID 和 `amount=4000000` 申请退出。余额回升、同 token 的另一通道成交，均不能替代这张卖单的独立成交证据。

### 13 · `scripts/executor_lock.py`（新增 35 行）

`lock_directory` 按系统选择用户配置目录，Windows 用 APPDATA，macOS 用用户的 Library/Application Support，其他平台取 XDG_CONFIG_HOME 或 ~/.config，随后进入 nba-executor/locks。`account_lock` 取“环境 + NUL + 小写账户”的 SHA-256 为文件名。它打开锁文件，在 Windows 非阻塞锁定首字节，在其他平台用非阻塞独占 flock。失败当即抛“已有执行器持有账户”的错误，关闭句柄释放内核锁。文件可留在磁盘。可传目录以隔离测试。互斥范围是共用锁文件的同一主机和用户。不同账本路径不能绕开同一身份的锁，其他主机或未接入该协议的写方则不在范围内。

<a id="file-producer"></a>

### 14 · `scripts/executor_producer.py`（新增 536 行）

- **两种接入。** `micros` 用 Decimal 将输入乘一百万后四舍五入。`StrategyAPI` 定义 submit/report/decisions/decision/intent/finish 等调用，`verify` 校对服务账户和模式，`base_budget` 取当前可用现金乘 `BASE_STAKE_FRAC`（5%）。内置客户端直接分发，独立程序的 `LocalExecutor` 仅接受回环 HTTP，无代理连接、10 秒超时，使用环境令牌或标准目录下对应模式的令牌文件。
- **每场恢复。** `GameSignals.__init__` 从报告恢复开仓/退出 ID 与已完成退出通道，从决策恢复冻结请求、首次请求时钟、首次看见成交的时钟、D 价格越界后整场跳过标记。比分历史 `events` 在当前实例内积累。可用这一区别判断进程恢复后哪些数据还能重建。
- **先存请求再提交。** `record` 保存比赛、通道、版本、源时间、输入、条件、结果及关联意图，并以整个事件的稳定 JSON 计算 source_id。`issue` 每个通道与动作先冻结预算/份额、限价及时间，再记录 REQUEST、提交、记录 ACCEPTED（意图已被执行器接受）。开仓机会 15 秒，退出窗口 900 秒。这里 signal_at 取创建请求时的墙钟，输入快照中的 source_time 是另一个字段。HTTP 409/422 记录 SKIP 和错误详情，其他网络错误由外层处理。重启重放读取第一次保存的请求，不能因为当前报价变了就改写它。
- **开仓。** `tick` 按策略通道读取相应方向的报价、调用既有 `process_signal/settle_signal`。A/C 过强平时限不再入场，终场也不新开。缺报价记 no_quote。对 ask 加估算入场成本后检验区间，D 首次遇到不合格价格记录 outside_entry_band 并封存该场。按基础预算乘权重，低于一美元跳过。买单限价取当前 ask。策略函数扫描已有比分事件，决策记录保留命中信号和当下快照，可比较两者时间。
- **退出。** 买入意图须达终态且有已确认股数，方可按实际成本含费/实际股数计算入场价。已有退出先扣去已售份额，在途者继续等。已核实部分终结、明确零成交或过期者可释放本地旧请求，以新的退出信号处理余量。首次观察成交时记录 FILL_OBSERVED，并把真实成交墙钟映射到比赛时钟，录制模式用入场快照避免混用加速时钟。A/C 按净卖价达到成本加 0.20、C 持有满 30 比赛分钟、终场或强平阈值的顺序判断。D 仅在强队分差转负时触发 margin_stop，终场未触发则记 HOLD 等待兑付。实际发卖单还需有 bid。
- **录制。** `run_recording` 可过滤一场比赛。按事件墙钟二分取最近的历史价格，同时作为买卖报价。每个新发现的开/平仓意图最多等 30 秒到终态再推进下一快照，否则抛超时。每场最后调用 finish。若报告 PENDING 再轮询最多 30 秒，随后打印当时报告，因此打印本身不保证 MATCHED。
- **实时。** `public_quote` 从公开 CLOB 订单簿取最低卖/最高买及市场。`run_scoreboard` 借用旧 runner 的公开比分、市场解析和球队映射函数，按强/弱与主客旋转分差，核对两个 token 均属同市场，再调用 tick。终场即登记比赛，若 A/C 仍有未决买卖或未平份额会继续跟踪。`follow_scoreboard` 捕捉超时和网络中断后按间隔恢复（HTTP 400/401/403 上抛）。`main` 要求实时与录制二选一，固定基础金额仅供 paper，间隔和金额须为正，启动时核对账户。模拟模式也可读实时公开行情。

### 15 · `scripts/executor_reports.py`（新增 442 行）

1. **账户证据与游标。** 初始化增加账外成交、feed 游标、策略决策、已结束比赛、报告修订、人工复核和待刷新比赛表及触发器。`_observe_fill` 校验订单号、明细、资产、方向、数量与价格。重复明细可从未确认升为确认、补上原来缺失的比赛 ID。同一确认明细的费用或金额若冲突，整页回滚。`observe` 与游标在一个事务内提交，`cursor` 给后台提供续读位置。
2. **对平规则。** `_game_summary` 按本地意图与尝试的订单号关联账户明细，并为各通道列计划、成交、费用、兑付与开仓余量。未绑定且无比赛 ID 的明细，可按资产在相关比赛中出现为待核线索。只有终场且至少观察过一次账户页才有资格 MATCHED。活跃意图、未确认或未处置的交易、身份异常、数量/金额/费用差额使报告 PENDING。手工成交仍可查，按手工归类后从策略的场所买卖总量中排除。报告对平与剩余仓位已清空也是不同条件。
3. **修订。** `summarize(finish=True)` 首次固定终场时间。已结束比赛与上次 JSON 内容按对象比较，只有实质变化才追加 revision，读取不会凭空增加版本。`refresh` 扫待刷新集合。新建或改变意图、账外成交新增/变更、首次账户页、兑付和认领外部单可触发对应比赛刷新。未知所属且多个比赛共用代币时可能需要刷新多场。
4. **可观测性。** `_metrics` 给未知尝试数、最老年龄、30 秒警告、5 分钟复核阈值、未归属数和最大发送至成交事件延迟，以及进程内重复/冲突计数。`alert_summary` 聚合未知滞留、未认领和最新报告买卖数量差。`_detail` 返回意图、历次尝试、事件线和首笔确认成交时间。活跃意图的 `next_check_estimate` 是当前时间加 1 秒的估算。库中没有下次查单排期。`_trace` 用订单号联查意图时间线、场所明细，无任何匹配时报 404。
5. **决策审计。** `_decision` 校验游戏、信号、版本、条件等字段，将输入以排序后的 JSON 保存。按 game_id 和 source_id 唯一，旧 JSON 键顺序不影响重放。同键内容有变则冲突。查询按源时间再按 source_id 排序。
6. **人工复核。** `_resolve` 要求 actor 和 evidence。`manual` 标记确实没有本地所有者的外部单。`bind` 凭方向、资产与可用比赛信息将已有外部证据绑定当前活跃尝试，已有相同绑定直接返回。`import` 只导入已绑定订单下已确认且未另行处置的指定明细，未知余量继续预留。`zero` 仅能结束 UNKNOWN，须本地成交/匹配最高值为零，且无相反外部证据。实质处置与复核记录同事务保存，重复 import 可留两次人工操作记录而成交仅计一次。
7. **路由。** `dispatch` 提供账户可用额、指标、意图明细、订单追踪、比赛集合、决策/报告/修订、待核成交与复核历史的 GET。处理决策写入、人工处置、终场登记、暂停/恢复开仓的 POST。比赛集合汇集意图、决策、终场和已带比赛 ID 的外部成交。暂停标记写进 execution_control，服务重启可读取。

<a id="file-schema"></a>

### 16 · `scripts/executor_schema.sql`（新增 44 行）

基础 DDL 由 Ledger 在 0 版数据库中执行。理解表的归属后，可在附录逐条查看 DDL、查询、更新和触发条件。[SQL 逐语句索引](#sql-catalog)

| 表与索引 | 数据由谁生成，约束保护什么 |
| --- | --- |
| `ledger_meta` | 单行绑定账户、环境与正数初始资金。重开库时核对三者，防止拿另一配置继续交易 |
| `intents`、`one_open/one_close/pending_intents` | 同一行保存原始条款、状态、剩余预留、已确认量/金额/费用及当前尝试。两个带条件唯一索引分别限制每账户/环境/比赛/通道一个开仓、每 position_id 一个退出。队列按状态与上次检查时间索引 |
| `close_requests` | `(intent_id,signal_at)` 保存每次退出信号的份额、限价和期限，重放旧信号仍可查回原条款 |
| `attempts`、`one_inflight` | 一条意图有多次尝试，每个序号唯一，已知订单号唯一。四类活跃状态触发部分唯一约束，让同一意图最多有一个仍可能成交的尝试。保留本次预留量、限价、时间及匹配最高值 |
| `fills`、`fills_by_intent` | 每条确认成交含方向、资产、数量、价格、实际金额和费用。主键 `(order_id,id)` 防止重叠分页重复入账，意图索引用于核对持仓 |
| `events`、`events_by_intent` | 用 seq 给同意图历史排序，存尝试号、时间、状态、原因，串起发送、观察与人工零成交证据 |
| `settlements` | position_id 主键关联开仓。`(venue_event,leg_id)` 另设唯一约束，防止同一兑付被重复归到第二个仓位。保留数量、兑付、费用、操作者、证据、时间 |

暂停状态表由账本版本迁移创建，账户成交/报告表由 Reports 创建，模拟订单表和加密签名表分别由对应 venue 创建。只列本文件会漏掉运行中的数据库对象。

<a id="file-venues"></a>

### 17 · `scripts/executor_venues.py`（新增 240 行）

- **统一出口。** 引擎对两种场所调用相同三项操作：`submit(intent)` 首次发送，`lookup(intent)` 查询原单，`trades(cursor)` 独立查账户成交。两种证据分别供本地分录和外部核对使用。
- **PaperVenue。** 创建 `paper_orders`，按 attempt 和 order_id 各自唯一。OPEN 用“冻结微美元 × 一百万 ÷ 限价”向下取整成微股，CLOSE 按冻结股数，全量按限价执行、手续费为零。尺寸不合格、到期、停机分别在插入前判零（停机标可重试）。订单号 `paper-<attempt>`、成交号 `match-<order>` 均稳定。`lookup` 通过 attempt 找原单，找不到抛异常。`trades` 校验非负十进制序号并每页取最多 100 笔，读完回传原游标。表留在磁盘，可独立于进程复原。
- **LiveVenue 建立环境。** 校验凭证、HTTPS Polygon RPC、32 字节标准 Base64 加密密钥与 AES 库，建 `live_prepared`，运行限时 20 秒的只读 probe。四个订单 SDK 位置与一个账户成交/探测位置独立。
- **隔离超时。** `_call` 创建单次预算、抢信号量并交给守护线程 `_run_sdk`。线程只拿序列化载荷/预算，不持账本。调用方退出时放弃晚到结果。卡死调用继续占其信号量直到实际返回。POST 尚未进入传输会抛 BeforeTransmission，已进入而结果未取得则保持不确定。对外返回经过简化的错误，避免记录凭证或 RPC 地址。
- **实盘提交顺序。** `submit` 同一尝试若已有准备记录，返回 UNKNOWN 与原订单号。否则聚合买单或同资产卖单的预留，调用 prepare，在 35 秒总预算内验证订单哈希与签名载荷。AES-GCM 使用随机 12 字节 nonce，以 attempt ID 为附加认证数据，将 nonce、密文、tag 连同明文订单号先提交到数据库。只增不覆盖。之后才可能执行一次 POST。准备/传输前可证未发按零成交处置，进入传输后断连则记 UNKNOWN 并保留哈希。
- **查回原身份。** `lookup` 优先取准备表哈希，无记录才用回执，冲突或都缺失时报错。按原订单、资产、方向、市场查证，预算 30 秒。`trades` 使用独立锁和预算拉账户页，对能由准备表找到的单补上本地比赛 ID。其他单留给报告归属。尾页有新游标时，下一次调用先返回稳定的同一游标，让引擎确认完整一轮读取。

### 18 · `scripts/live_runner.py`（增加 7 行）

旧入口 `main` 原本就在解析 `--live`、`--dry-run` 后直接构造客户端并运行。本次引入 `account_lock`，把原有运行主体提取为 `run(live)`。只有 `--live` 且没有 `--dry-run` 时，先读 `POLY_FUNDER` 并持有对应 live 锁再调用 `run(True)`。其余走 `run(False)`。因此同主机同用户的新版旧 runner 与新执行器抢同一实盘账户时只有一个取得锁。旧执行代码仍有自己的 JSONL、重试与持仓逻辑。新生产者只复用其中的公开比分/映射函数。

### 19 · `scripts/nba_data.py`（增加 1 行、删除 1 行）

HTTPS 上下文从 `ssl.create_default_context(cafile="/etc/ssl/cert.pem")` 改为 `ssl.create_default_context()`，让 Python/系统默认的 CA 配置验证 HTTPS，解决硬编码类 Unix 证书路径在其他平台不可用的问题。构造 HTTPSHandler 和 ProxyHandler 的方式、`NBA_PROXY` 的默认值都沿用。该改动影响复用公共数据模块的各个程序。策略阈值和报价解析未改。

### 20 · `scripts/requirements-executor-live.txt`（新增 4 行）

两项声明各附用途说明。`py-clob-client-v2==1.2.2` 固定 SDK，代码另在创建 client 时核验版本，签名数据、共享 HTTPX 客户端钩子和响应结构都依赖这一版本。`pycryptodome>=3.20,<4` 提供 Crypto.Cipher.AES，用 GCM 持久化已签载荷。PaperVenue 与常规模拟启动只需标准库。CI 安装本清单以执行附条件的 SDK 和加密契约测试。

<a id="tests"></a>

## 测试文件与验收点

以下逐一对应提交中新增的 7 个测试文件，共 41 个 `test_*` 方法。用例中的地址、签名用账户、交易所回执和网络延迟均为离线测试输入。每行写出输入情形和断言，可据此判断哪些保证已有直接覆盖。

### 21 · `scripts/test_executor_clob.py`（新增 184 行）

| 测试方法 | 输入 → 验收点 |
| --- | --- |
| `test_exact_contract_wallet_and_order_log` | 构造成功回执、可信合约和钱包主题，解析得到 token 713、4 股、3 美元、费用 3,000 微美元、价格 0.75，成交键为交易哈希加日志序号 15。替换钱包或可信合约集合时结果为空 |
| `test_delayed_and_unknown_responses` | delayed 且订单号匹配为 ACCEPTED。泛化失败、成功却回错 ID、重复订单文本均为 UNKNOWN。明确的 FOK 失败文本才判 REJECTED_ZERO |
| `test_lookup_cancellation_and_incomplete_details` | 订单已取消但 `size_matched=4`，即使明细暂空也应返回 FINAL、累计 4,000,000 微股供账本等待进一步核实。DELAYED 为 OPEN |
| `test_pinned_sdk_prepare_signs_real_payload_without_network` | 临时钱包加桩报价、余额和市场元数据，真实 SDK 离线签名可恢复出同一钱包并复算相同哈希。实际 maker 金额为正且低于 4 美元以预留费用。合计预留提升到 10 美元时拒绝，全程没有 POST。缺 SDK 时跳过 |

### 22 · `scripts/test_executor_engine.py`（新增 377 行）

用临时 SQLite 和 PaperVenue 驱动引擎，涉及实盘场所时注入离线 SDK/HTTPX。通过重开同一路径模拟进程接替，通过屏障和事件精确控制发送与关闭时机。

| 测试方法 | 输入 → 验收点 |
| --- | --- |
| `test_lost_ack_then_restart_after_deadline` | paper 已落单后抛断线，意图 UNKNOWN、3 美元继续冻结。关库、时钟推进一分钟后恢复并额外对账，同键仍返回原 ID，最终 4 股、预留归零、游标 1，恢复方 submit 调用数为零 |
| `test_interrupted_submission_quarantines_reservation` | 仅在账本领取而场所没有这张单，恢复查单失败后维持 UNKNOWN 和原预留，30 秒及 5 分钟相应告警均出现 |
| `test_claim_must_commit_before_send` | SQLite 切只读，领取事务报 OperationalError，验证外部 submit 从未被调用 |
| `test_paper_pagination_identity_and_duplicate_gate` | 同一尝试再次提交触发唯一约束。共 101 条订单分页为 100+1，再读则空页和相同游标。已到期的模拟提交判零 |
| `test_incomplete_feed_commits_cursor_and_keeps_gate_closed` | 模拟无休止的下一页，达到 128 页报追赶错误，游标已为 128、成功时间仍为零。恢复稳定页后才更新成功时间，两次手动核查都没有发单 |
| `test_four_workers_and_shutdown_waits` | 五个 READY 实际只有四个同时进入 submit。故意阻塞它们，stop 在释放前保持等待，放行后停止且未派发第五单 |
| `test_signed_identity_survives_lost_reply_and_matches_go_format` | POST 入口即能查到已持久化的哈希。测试按 nonce/密文/tag 和 attempt 解密，取回原签名。丢回执后重建场所，同一尝试仅一次 POST、可用原哈希只读查单，冲突回执报错。账户流仅补已知订单的比赛 ID，并在尾页游标处停止 |
| `test_shutdown_during_live_prepare_prevents_post` | 准备签名时触发停机，模拟 SDK 稍后才返回。没有调用 POST。首次明确未发且仍可重试，账本回到 READY |
| `test_post_hook_rechecks_deadline_and_shutdown` | 分别提供墙钟已到期和已收到停止信号，在传输钩子处两者都抛 ExpiredBeforePost |
| `test_wall_clock_timeout_abandons_before_transport_and_caps_stuck_workers` | 对不理会超时的 SDK 连续发起六次短预算调用，实际只启动四个受阻线程，调用方能及时退出，账户成交仍能读取。放行后四个旧调用都失去发送权，真实发送数为零 |
| `test_hung_post_after_handoff_cannot_block_stop_or_record_late_receipt` | POST 已取得传输权后挂起。stop 在一秒内结束，账本保留 UNKNOWN、订单号和 3 美元。替换引擎先恢复，迟到“可重试零成交”随后返回，也不能改写新账本。按原单查回 ACCEPTED 且全程只发一次 |
| `test_pinned_sdk_uses_guarded_transport_without_post_polling` | 用真实 SDK 和 HTTPX MockTransport，探测及发送合计恰好一个 GET 加一个 POST。请求的读超时大于零、不超过 5 秒。即使回执带未结算交易号也立即返回 ACCEPTED，不额外轮询 |

其中签名保存、准备中停机、挂起 SDK、已传输调用的四项测试需要 Crypto。最后的真实 SDK 传输用例需要 py_clob_client_v2。均可用 CI 安装的依赖执行。

### 23 · `scripts/test_executor_ledger.py`（新增 245 行）

测试用真实磁盘 SQLite、两个独立连接、受控时钟和可重复的成交明细。辅助方法 `claimed` 连续执行受理和抢占，`fill` 故意在同一快照放两份相同的成交。

| 测试方法 | 输入 → 验收点 |
| --- | --- |
| `test_replay_and_slot_and_cash_serialized_across_connections` | 两连接以 10 个线程重放 100 次同一请求，恰一条新意图、同一 26 字符 ID，开仓以本 ID 作 position_id。改预算冲突、超现金拒绝，两通道争最后名额只能成功一个 |
| `test_transactional_v1_upgrade_and_restart` | 检查外键、FULL 同步与 WAL。模拟 v1，错误账户尝试迁移时版本回滚，正确账户重开升 v2 且默认未暂停。改变原始资金重开被拒 |
| `test_atomic_schema_and_nested_transactions` | DDL 后故意执行坏 SQL，前面建表也回滚。嵌套事务改金额抛错时只撤销内层，外层新建意图仍保留原金额 |
| `test_validation_and_expiration` | 浮点价格、布尔金额、错误通道、空市场、溢出时间、超过上限金额、开平仓与 position_id 不匹配均拒绝。恰到截止点受理为 EXPIRED_UNSENT，不能领取、不冻钱，重放改期限也不能复活开仓 |
| `test_proof_of_zero_and_retry_budget` | UNKNOWN 不能再领取，得到可重试零成交证明后可建第二个尝试。旧尝试的迟来 ACCEPTED 被忽略。第二次仍零后终止并释放，累计尝试恰为二 |
| `test_only_one_sender_and_hold_rechecked_before_dispatch` | 受理后暂停或出现不明外部成交均能在 claim 前挡住开仓，原意图未变。划为手工后费用与成本扣进可用额。两连接并发领取 32 次仅一人获权。异标的疑单不阻平仓，新增同标的疑单阻止原退出发送 |
| `test_matched_watermark_survives_restart_and_stale_final` | FINAL 累计为零却附带尚未确认的 3 股匹配。重开库后旧零量快照及后来零成交声明无法推翻历史最高量。仍 ACCEPTED、冻结 3 美元，矛盾拒单回执报冲突 |
| `test_malformed_zero_snapshots_preserve_reservation` | 给 FINAL 快照塞字典/字符串/数字充当明细列表，或伪布尔、错误明细。均判输入无效，逐项核对意图与预留完全未变 |
| `test_partial_exit_retry_rearm_replay` | 买入 4 股后超量退出被拒。第一次确认卖 1 股后同窗口 READY 仅预留 3 股，第二次卖 1 股时已过期，余 2 股进入 PARTIAL_FINAL。旧请求保持旧结果，新信号只重启真实余量、仍用同一退出 ID，同信号改限价冲突 |
| `test_conflicting_fill_batch_rolls_back_and_fee_overrun_blocks_claim` | 同一成交 ID 的费用互相矛盾，全批回滚无分录。提交正确分录后因手续费使可用额成为 −1,000 微美元，另一个已受理开仓无法 claim |
| `test_settlement_requires_evidence_credits_once_and_releases_slot` | D 的 6 股兑付缺 confirmed 或覆盖股数错误时拒绝。经核实只入账一次，二次重放幂等、改费用冲突。扣费后现金 12,999,900 微美元，已兑付仓不能卖出，原来唯一的名额可用于下一开仓 |

### 24 · `scripts/test_executor_lock.py`（新增 16 行）

`test_legacy_writer_excludes_second_writer_and_releases` 在临时目录以 wallet 先持锁，用 WALLET 再取时抛 RuntimeError。第一层退出后再获取成功，验证账户大小写归一及句柄关闭后的释放。该用例在同一测试进程嵌套调用锁，检验的是共享锁协议与本机实现。没有实际启动旧二进制。

### 25 · `scripts/test_executor_producer.py`（新增 106 行）

| 测试方法 | 输入 → 验收点 |
| --- | --- |
| `test_recorded_close_is_booked_before_advancing_source_clock` | 用三个加速快照，在前两个分别创建开、平仓。进入第三个快照的回调时，已通过 API 看见退出终态。下一次定仓因此可基于已入账的退出所得 |
| `test_restart_preserves_amount_and_quote_after_lost_response` | 首次提交抛 URLError，但 REQUEST 已写。用更大的基础预算和不同报价重建 GameSignals，再次提交的整个请求与第一次逐字段相同，D 关联返回的原意图 |
| `test_delayed_buy_is_posted_once_and_close_uses_confirmed_position` | 多次输入 D 状态后强队变为落后，只出现一个 OPEN 和一个 CLOSE。退出量是模拟账本确认的 4 股，引用 pos，理由 margin_stop。该用例直接构造确认持仓。真正的丢回执查单由引擎测试覆盖 |

### 26 · `scripts/test_executor_reports.py`（新增 156 行）

| 测试方法 | 输入 → 验收点 |
| --- | --- |
| `test_late_fill_revises_finished_game_exactly_once` | 先对平 4 股出 v1，首笔时间与订单追踪时间线可查。后到另一张 6 股外部单且重复推送，v2 显示场所 10 股、本地 4 股、差 6 股并告警，重复读取不增版本。标记手工后 v3 对平、告警消失 |
| `test_entire_venue_page_rolls_back_on_inconsistency` | 成交由未确认升为确认后，再来旧状态不冲销已确认费用。下一页含一条新单和一条改价冲突，整页连游标都不推进 |
| `test_first_observation_and_existing_go_report` | 没见过账户页先终场，报告 PENDING。只改变已有 JSON 的缩进不增修订。第一次成功读取空账户页后刷新为 MATCHED。此处用测试生成的 JSON 检查兼容，不依赖一份父提交 Go 程序 |
| `test_operator_import_does_not_release_unknown_remainder` | UNKNOWN 已有相反外部成交，人工 zero 被拒。重复 bind 不重复记操作，重复 import 留两条审计但只认一次成交，仍 PARTIAL 并保留 2 美元预留。每项审计都有人员和证据 |
| `test_binding_an_untagged_fill_updates_other_games_with_the_same_asset` | 两场共用 token，无比赛标签的外部单先出现在另一场的待核清单。绑定真实尝试后，另一场追加新报告并移除误关联候选 |
| `test_audited_zero_without_contradictory_evidence` | 5 分钟未决产生告警，指标体现重复、冲突和未知。人工核实无相反证据后可标零，资金全释放，暂停和恢复开仓端点返回对应布尔值 |
| `test_decision_idempotency_and_existing_serialized_json` | 给旧 JSON 换序列化方式、重放空 intent_id 仍为同一决策。同 source_id 改理由冲突，inputs 为 null 无效。比赛能从决策集合被枚举 |

### 27 · `scripts/test_executor_startup.py`（新增 97 行）

| 测试方法 | 输入 → 验收点 |
| --- | --- |
| `test_recording_finishes_in_one_python_process` | 调用实际解释器和 CLI，以含空格的临时路径、随机模拟账户、动态端口运行单场无报价录制。子进程退出码 0、输出可解析的终场报告，并在自定义数据库旁生成令牌 |
| `test_live_recording_fails_before_opening_database` | 将 recording 与 live 同时交给实际 CLI，退出码非零、标准错误提示 paper 限制、数据库路径仍不存在 |
| `test_authenticated_order_api_reuses_an_intent_and_records_conflicts` | 真实回环 HTTP：健康检查可匿名，意图无令牌返回 401。授权首次 201、同请求 200 且复用 ID、改金额 409。详情仍 READY 并冻结原金额，指标分别记一次重复和冲突 |

独立复核范围可用 `git diff --name-status 6fb4cde fa4ea64` 查看，逐一还原历史文件用 `git show fa4ea64:<路径>`。本文对测试描述的是提交内的断言。三平台 CI 的设置、真实账户的验证结果、待排期需求的验收是不同层次的证据。

<a id="verification"></a>

## 本次核验记录

在 Windows 的临时目录用 Python 3.12.4 和已安装的实盘 SDK 依赖运行基线脚本。以下数据来自模拟盘，不使用账户凭证或真实交易所委托。

| 检查 | 结果 | 说明 |
| --- | --- | --- |
| 离线自动化测试 | 47 项通过 | 覆盖本提交新增的执行器测试及已有旧交易脚本测试。三平台 CI 配置见第 01 项，本次本地运行只代表 Windows |
| 九场录制回放 | 9 份报告全部为 `MATCHED` | 用独立临时账本运行单进程入口。各场报告的买、卖份额差额均为 0 |
| SQLite 语句清点 | 119 个应用与建库条目，20 个测试条目 | 从固定提交提取 SQL 原文、固定模板及调用处。使用 SQLite 3.45.3 在内存库建立表、索引和触发器，对其余查询检查能否编译。测试刻意对不存在的表执行的语句按预期故障单列 |
| 流程图 | Archify 3.2.0 生成的独立 HTML | 通过 Archify 的 validate、deliver、check 和 browser-check。断网打开后点击订单节点可见说明及上下游。外部源码链接需联网 |

九场中，`401909297` 与 `401909302` 各有 0 条订单意图，仍有比赛完成报告。雷霆场 `401909304` 有 6 条意图。整套录制共产生 24 条意图。这里的 `MATCHED` 表示本地分录与模拟场所流水对平，实盘余额、手续费、查单退避与不可交易零头仍须按[需求验收条件](executor-requirements.md)分别检查。

<a id="sql-catalog"></a>

## SQL 逐句手册

本节对应提交 `fa4ea64` 中的 SQLite 语句，按源码出现位置而非运行次数计数。代码块保留参数占位和实际查询条件，将 Python 的固定查询模板展开。每个位置列出绑定来源及其业务后果。表格数字含事务控制、PRAGMA、测试夹具以及触发器定义。

| 来源文件 | 条目 | 阅读内容 |
| --- | ---: | --- |
| [executor_schema.sql](#sql-ddl) | 13 | 基础表与索引 |
| [executor_ledger.py](#sql-ledger) | 44 | 连接、事务、预留、委托与兑付 |
| [executor_reports.py](#sql-reports) | 51 | 对账流水、触发器、报告、指标与复核 |
| [executor_venues.py](#sql-venues) | 10 | 模拟交易及实盘签名存档 |
| [executor_api.py](#sql-api) | 1 | 接口健康检查 |
| [test_executor_ledger.py](#sql-t-ledger) | 16 | 账本测试中的 SQL |
| [test_executor_engine.py](#sql-t-engine) | 2 | 执行引擎测试中的 SQL |
| [test_executor_reports.py](#sql-t-reports) | 2 | 报告测试中的 SQL |
| **合计** | **139** | **119 条应用及建库条目，20 条测试条目** |

**执行顺序。** 启动先为 SQLite 连接设置 WAL、同步级别和外键约束，在迁移事务内按版本运行基础 DDL，再为报表与选定交易场所建表。交易中按“创建意图与预留 → 领取发送权 → 保存实盘签名身份 → 单次传输 → 核查成交 → 结清或继续预留”的顺序写账。账户流水的分页游标与该页明细共同提交。

**计数与定位。** 两次调用同一事件查询分别列出。条件表达式里的两条事务指令分别列出，测试同一源码行上的两条迁移语句也分别列出。一个 `CREATE TRIGGER … BEGIN … END` 作为一条完整定义，内部的写入语句在该条中解释。`conn.rollback()` 以等效 SQL 展示。SQL 块可对照阅读，直接执行时需要调用处的参数、事务和表结构。

**模板与参数。** `?` 按给出的顺序取值，`:name` 按字典键取值。固定片段 `PENDING_SQL` 列出五个未决状态。`NO_OWNER` 是按精确外部订单号查本地尝试的反存在条件。`UNASSIGNED_COUNT` 在其上加入确认和待处置条件。`EVENTS` 是按序取意图时间线的共享查询。`execute_schema` 用 SQLite 的完整语句判定切分脚本，两处通用 `execute` 转发语句及末尾片段。`_rows` 执行调用点传入的查询并将结果转为字典。这些转发处的具体 SQL 已分别归到下列定义及调用点。

<a id="sql-ddl"></a>

### 基础表与索引 · `executor_schema.sql`

#### ddl-01　绑定账本的账户和初始资金

位置　`新库初始化` · [executor_schema.sql 第 1 行](../scripts/executor_schema.sql#L1)

```sql
CREATE TABLE ledger_meta (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), account TEXT NOT NULL, environment TEXT NOT NULL,
 starting_capital INTEGER NOT NULL CHECK(starting_capital>0)
);
```

singleton 只能取 1，因此一个库只属于一个账户和一种环境。starting_capital 以百万分之一货币单位保存且必须为正。重启时用这行核对启动参数，避免把另一账户的资金当作可用额度。

#### ddl-02　订单意图主表

位置　`新库初始化` · [executor_schema.sql 第 5 行](../scripts/executor_schema.sql#L5)

```sql
CREATE TABLE intents (
 id TEXT PRIMARY KEY, account TEXT NOT NULL, environment TEXT NOT NULL, game_id TEXT NOT NULL,
 channel TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN ('OPEN','CLOSE')), asset TEXT NOT NULL, market TEXT NOT NULL,
 position_id TEXT NOT NULL, reason TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>0), limit_price INTEGER NOT NULL,
 signal_at INTEGER NOT NULL, deadline INTEGER NOT NULL, state TEXT NOT NULL,
 reserved INTEGER NOT NULL CHECK(reserved>=0), filled INTEGER NOT NULL DEFAULT 0,
 cost INTEGER NOT NULL DEFAULT 0, fee INTEGER NOT NULL DEFAULT 0, last_check INTEGER NOT NULL,
 attempt_no INTEGER NOT NULL DEFAULT 0, attempt_id TEXT NOT NULL DEFAULT '', order_id TEXT NOT NULL DEFAULT ''
);
```

id 是本地身份。account/environment/game_id/channel/action 定位一次策略决策，market 和 asset 标识实际市场和标的。OPEN 的 amount 是预算，CLOSE 的 amount 是希望退出的份额。两者同为整数但量纲不同。position_id 把退出指向原开仓。signal_at/deadline 是 Unix 毫秒。reserved 对买单是剩余预留资金，对卖单是剩余预留份额。filled、cost、fee 保存累计确认量、成交金额和费用。last_check、attempt_no、attempt_id、order_id 保存最近处理进度。数据库检查动作、正数量和非负预留，更多状态约束由应用维护。

#### ddl-03　同一开仓决策只能建一次

位置　`新库初始化` · [executor_schema.sql 第 14 行](../scripts/executor_schema.sql#L14)

```sql
CREATE UNIQUE INDEX one_open ON intents(account,environment,game_id,channel) WHERE action='OPEN';
```

仅 OPEN 行进入此唯一索引，以账户、环境、比赛、通道作键。市场报价、策略版本、处理进程和信号时间都不产生第二个名额。A/C 即使交易同一资产，通道不同仍可分别开仓。

#### ddl-04　每个仓位只保留一个退出意图

位置　`新库初始化` · [executor_schema.sql 第 15 行](../scripts/executor_schema.sql#L15)

```sql
CREATE UNIQUE INDEX one_close ON intents(position_id) WHERE action='CLOSE';
```

只索引 CLOSE。相同 position_id 的止盈、强退及重试都复用同一退出意图。经核实可继续退出时新增尝试，避免另建退出意图重复卖。

#### ddl-05　冻结每次已接受的退出请求

位置　`新库初始化` · [executor_schema.sql 第 16 行](../scripts/executor_schema.sql#L16)

```sql
CREATE TABLE close_requests (
 intent_id TEXT NOT NULL REFERENCES intents(id), signal_at INTEGER NOT NULL, amount INTEGER NOT NULL,
 limit_price INTEGER NOT NULL, deadline INTEGER NOT NULL, PRIMARY KEY(intent_id,signal_at)
);
```

复合主键为意图和信号毫秒，外键指向 intents。保留请求数量、限价与截止时间。后续可按原信号区分重复请求和带新条件的剩余份额退出。

#### ddl-06　未决队列索引

位置　`新库初始化` · [executor_schema.sql 第 20 行](../scripts/executor_schema.sql#L20)

```sql
CREATE INDEX pending_intents ON intents(state,last_check);
```

按状态、上次检查时间索引，支持筛选等待发单或对账记录。查询仍用 id 破同时间的平局。索引本身不提供失败退避或优先级。

#### ddl-07　记录每一次取得发送权的尝试

位置　`新库初始化` · [executor_schema.sql 第 21 行](../scripts/executor_schema.sql#L21)

```sql
CREATE TABLE attempts (
 id TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES intents(id), number INTEGER NOT NULL,
 state TEXT NOT NULL, order_id TEXT, matched_high_water INTEGER NOT NULL DEFAULT 0,
 amount INTEGER NOT NULL, limit_price INTEGER NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 UNIQUE(intent_id,number), UNIQUE(order_id)
);
```

intent_id 外键关联同一业务意图，number 在意图内唯一。每次尝试保留自己的金额或数量、限价、状态、创建及更新时间。交易所订单号在非空时唯一，未知时可为 NULL。matched_high_water 保存该次尝试曾观察到的最大撮合量。

#### ddl-08　只允许一次可能成交的尝试

位置　`新库初始化` · [executor_schema.sql 第 27 行](../scripts/executor_schema.sql#L27)

```sql
CREATE UNIQUE INDEX one_inflight ON attempts(intent_id) WHERE state IN ('SUBMITTING','ACCEPTED','UNKNOWN','PARTIAL');
```

只将 SUBMITTING、ACCEPTED、UNKNOWN、PARTIAL 纳入唯一索引。任意一条处于这些状态，SQLite 就会阻止同一意图出现第二条此类尝试。

#### ddl-09　保存已确认的成交明细

位置　`新库初始化` · [executor_schema.sql 第 28 行](../scripts/executor_schema.sql#L28)

```sql
CREATE TABLE fills (
 order_id TEXT NOT NULL, id TEXT NOT NULL, intent_id TEXT NOT NULL REFERENCES intents(id),
 asset TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity>0),
 price INTEGER NOT NULL, cost INTEGER NOT NULL, fee INTEGER NOT NULL, time INTEGER NOT NULL,
 PRIMARY KEY(order_id,id)
);
```

主键为外部订单号与稳定成交 ID，重复到达只对应一笔经济成交。intent_id 指明实际入哪个仓位，asset、side、quantity、price、cost、fee、time 记录标的、方向、正份额、价格、金额、费用和成交时间。一张订单可拆成多条成交。

#### ddl-10　按本地意图找成交

位置　`新库初始化` · [executor_schema.sql 第 34 行](../scripts/executor_schema.sql#L34)

```sql
CREATE INDEX fills_by_intent ON fills(intent_id);
```

给 fills.intent_id 建普通索引，用于查询该意图的成交记录及最早确认成交时间。单笔成交去重另由复合主键负责。

#### ddl-11　追加意图状态事件

位置　`新库初始化` · [executor_schema.sql 第 35 行](../scripts/executor_schema.sql#L35)

```sql
CREATE TABLE events (
 seq INTEGER PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES intents(id), attempt_id TEXT NOT NULL,
 at INTEGER NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL
);
```

seq 为整型主键，用于同毫秒事件的排序。intent_id 指向意图，attempt_id 记当时的尝试，at、state、reason 还原发生时间、状态及原因。尚无尝试时 attempt_id 可为空字符串。

#### ddl-12　按意图读取时间线

位置　`新库初始化` · [executor_schema.sql 第 39 行](../scripts/executor_schema.sql#L39)

```sql
CREATE INDEX events_by_intent ON events(intent_id,seq);
```

复合索引先定位 intent_id 再按 seq 顺序访问，服务于详情和按外部订单号追溯。同一毫秒也有稳定的入账顺序。

#### ddl-13　记录有凭据的兑付

位置　`新库初始化` · [executor_schema.sql 第 40 行](../scripts/executor_schema.sql#L40)

```sql
CREATE TABLE settlements (
 position_id TEXT PRIMARY KEY REFERENCES intents(id), venue_event TEXT NOT NULL, leg_id TEXT NOT NULL,
 shares INTEGER NOT NULL, payout INTEGER NOT NULL, fee INTEGER NOT NULL, actor TEXT NOT NULL,
 evidence TEXT NOT NULL, at INTEGER NOT NULL, UNIQUE(venue_event,leg_id)
);
```

position_id 为主键和意图外键，同仓只结算一次。venue_event 与 leg_id 的组合也唯一，防止同一外部凭据用于多个仓位。shares、payout、fee、actor、evidence、at 保存份额、毛兑付款、费用、操作人、证据和入账时间。

<a id="sql-ledger"></a>

### 连接、事务、预留、委托与兑付 · `executor_ledger.py`

#### ledger-01　按本地意图 ID 读账

位置　`read_intent` · [executor_ledger.py 第 81 行](../scripts/executor_ledger.py#L81)

```sql
SELECT * FROM intents WHERE id=?
```

绑定　`(intent_id,)`

绑定一个 intent_id，返回整行供查询、抢占发送权和状态推进使用。缺行在调用者转为 KeyError，接口可映射 404。

#### ledger-02　追加一次状态变化

位置　`append_event` · [executor_ledger.py 第 88 行](../scripts/executor_ledger.py#L88)

```sql
INSERT INTO events(intent_id,attempt_id,at,state,reason)
VALUES(?,?,?,?,?)
```

绑定　`(intent['id'], intent['attempt_id'], now, intent['state'], reason)`

五个绑定值依次是意图 ID、当前尝试 ID、事件毫秒、当前状态和可读原因。与该次状态或金额变更同事务写入，回滚时不会留下孤立的成功记录。

#### ledger-03　持久化意图的最新汇总

位置　`update_intent` · [executor_ledger.py 第 94 行](../scripts/executor_ledger.py#L94)

```sql
UPDATE intents SET state=:state, reserved=:reserved, filled=:filled,
cost=:cost, fee=:fee, last_check=:last_check, attempt_no=:attempt_no,
attempt_id=:attempt_id, order_id=:order_id WHERE id=:id
```

绑定　`intent`

从已读意图绑定同名参数，写状态、预留、累计成交和费用、检查时间及当前尝试/订单标识。只覆盖 id 对应的一行。初始业务键、预算与截止时间不会在这条语句中改写。

#### ledger-04　检测外部对账表是否已经建立

位置　`_has_external_feed` · [executor_ledger.py 第 100 行](../scripts/executor_ledger.py#L100)

```sql
SELECT 1 FROM sqlite_master
WHERE type='table' AND name='external_fills'
```

查 SQLite 系统目录中的表名和类型。没有 external_fills 时，早期初始化或仅用账本的路径跳过后续依赖这张表的计算。

#### ledger-05　计算执行账本已占用的资金

位置　`cash_committed` · [executor_ledger.py 第 105 行](../scripts/executor_ledger.py#L105)

```sql
SELECT coalesce(sum(CASE action WHEN 'OPEN' THEN cost+fee+reserved
ELSE fee-cost END),0) - (SELECT coalesce(sum(payout-fee),0)
FROM settlements) FROM intents
```

每笔开仓计入已花金额、费用和未用完的预留。每笔卖出按费用减卖出收入计入，兑付按毛付款减费用抵减资金占用。两个聚合均用 coalesce 处理空账本。可用额度在应用中由初始资金减占用得到。

#### ledger-06　把已确认手工交易纳入资金口径

位置　`cash_committed` · [executor_ledger.py 第 109 行](../scripts/executor_ledger.py#L109)

```sql
SELECT coalesce(sum(CASE side WHEN 'BUY' THEN notional+fee
ELSE fee-notional END),0) FROM external_fills
WHERE confirmed=1 AND disposition='manual'
```

只取外部流水中已确认并标为 manual 的成交，买入增加占用，卖出收入扣除费用后减少占用。结果加到上一个聚合中，避免外部交易造成可用现金虚高。

#### ledger-07　读取全账户开仓暂停状态

位置　`_check_hold` · [executor_ledger.py 第 116 行](../scripts/executor_ledger.py#L116)

```sql
SELECT paused FROM execution_control WHERE singleton=1
```

只在 OPEN 的检查路径执行，读取单行开关。paused 为真时抛出风控冲突，已有持仓退出仍有独立的检查路径。

#### ledger-08　识别会影响发送的未归属成交

位置　`_check_hold` · [executor_ledger.py 第 120 行](../scripts/executor_ledger.py#L120)

```sql
SELECT 1 FROM external_fills f WHERE f.disposition='' AND f.confirmed=1
AND (?='OPEN' OR f.asset=?) AND NOT EXISTS
(SELECT 1 FROM attempts a WHERE a.order_id=f.order_id) LIMIT 1
```

绑定　`(action, asset)`

查已确认、尚未处置且没有本地尝试认领的外部明细。传 OPEN 时任何标的都影响账户现金，传 CLOSE 时只匹配指定资产。LIMIT 1 足以触发冻结。

#### ledger-09　登记已接受的退出条件

位置　`_save_close_request` · [executor_ledger.py 第 130 行](../scripts/executor_ledger.py#L130)

```sql
INSERT INTO close_requests(intent_id,signal_at,amount,limit_price,deadline)
VALUES(?,?,?,?,?)
```

绑定　`(intent_id, req['signal_at'], req['amount'], req['limit_price'], req['deadline'])`

按意图、信号毫秒、数量、限价和截止时间写入 close_requests。第一次创建和后续重新允许卖剩余量时都会留下可重放的请求记录。

#### ledger-10　识别同一退出信号的重放

位置　`_existing_close` · [executor_ledger.py 第 136 行](../scripts/executor_ledger.py#L136)

```sql
SELECT amount,limit_price FROM close_requests
WHERE intent_id=? AND signal_at=?
```

绑定　`(old['id'], req['signal_at'])`

绑定已有意图 ID 与请求信号时间。找到记录就核对数量与限价，相同返回原结果，矛盾报冲突。找不到才进入新退出条件的核实路径。

#### ledger-11　阻止已兑付仓位重新卖出

位置　`_existing_close` · [executor_ledger.py 第 152 行](../scripts/executor_ledger.py#L152)

```sql
SELECT 1 FROM settlements WHERE position_id=?
```

绑定　`(old['position_id'],)`

有新退出信号且旧尝试已终止时，以开仓 position_id 查询 settlements。存在记录即拒绝重新预留该仓位。

#### ledger-12　更新重新受理的退出条件

位置　`_existing_close` · [executor_ledger.py 第 160 行](../scripts/executor_ledger.py#L160)

```sql
UPDATE intents SET signal_at=:signal_at,deadline=:deadline,
limit_price=:limit_price,reason=:reason WHERE id=:id
```

绑定　`old`

仅更新旧退出意图的信号时间、截止时间、限价和理由。剩余量、READY 状态及其他汇总由紧随其后的更新在同一事务落库。这样保留原 intent_id 和累计已卖量。

#### ledger-13　找到回报所属意图

位置　`current_attempt` · [executor_ledger.py 第 168 行](../scripts/executor_ledger.py#L168)

```sql
SELECT intent_id FROM attempts WHERE id=?
```

绑定　`(attempt_id,)`

通过 attempt_id 反查 intent_id，随后重新读取主表并验证是否仍是当前活动尝试。迟到的旧尝试结果不应覆盖后一次尝试。

#### ledger-14　存储尝试状态与精确外部订单号

位置　`attempt_state` · [executor_ledger.py 第 176 行](../scripts/executor_ledger.py#L176)

```sql
UPDATE attempts SET state=?,order_id=nullif(?,''),updated_at=? WHERE id=?
```

绑定　`(state, intent['order_id'], now, intent['attempt_id'])`

按尝试主键更新状态、订单号和更新时间。nullif 将未知的空串转为 NULL，允许多笔尚未知身份的尝试存在，同时 UNIQUE(order_id) 仍约束已知订单号。

#### ledger-15　同一成交的重放检查

位置　`record_fill` · [executor_ledger.py 第 217 行](../scripts/executor_ledger.py#L217)

```sql
SELECT quantity,cost,fee,price FROM fills
WHERE order_id=? AND id=?
```

绑定　`(intent['order_id'], fill_id)`

以外部订单号和明细 ID 查询原数量、成交额、费用及价格。四项一致即复用，改变已确认事实时报冲突并回滚整个批次。

#### ledger-16　写入一笔已确认且尚未入账的明细

位置　`record_fill` · [executor_ledger.py 第 223 行](../scripts/executor_ledger.py#L223)

```sql
INSERT INTO fills(order_id,id,intent_id,asset,side,quantity,price,cost,fee,time)
VALUES(?,?,?,?,?,?,?,?,?,?)
```

绑定　`(intent['order_id'], fill_id, intent['id'], intent['asset'], side, quantity, price, cost, fee, timestamp)`

绑定订单、明细、意图、资产、方向、数量、价格、金额、费用和时间。写成功才更新内存中的累计份额/金额并随同一事务持久化。重复成交走前面的核对分支。

#### ledger-17　启用并核实 WAL

位置　`Ledger.__init__` · [executor_ledger.py 第 246 行](../scripts/executor_ledger.py#L246)

```sql
PRAGMA journal_mode=WAL
```

切换写前日志模式并读取 SQLite 实际返回的模式。没有得到 wal 则终止初始化。该设置关系到运行中复制数据库时还需考虑的 WAL 内容。

#### ledger-18　设置连接的落盘级别

位置　`Ledger.__init__` · [executor_ledger.py 第 248 行](../scripts/executor_ledger.py#L248)

```sql
PRAGMA synchronous=FULL
```

FULL 要求 SQLite 在相应提交阶段执行完整同步。目的是在发送交易所请求之前让已提交的本地发送权和预留更耐故障。

#### ledger-19　启用本连接外键检查

位置　`Ledger.__init__` · [executor_ledger.py 第 249 行](../scripts/executor_ledger.py#L249)

```sql
PRAGMA foreign_keys=ON
```

让迁移和日常写入执行各表已声明的 REFERENCES，例如成交的 intent_id 必须能在主表找到。

#### ledger-20　选择初始化或迁移分支

位置　`Ledger._initialize` · [executor_ledger.py 第 257 行](../scripts/executor_ledger.py#L257)

```sql
PRAGMA user_version
```

读取 SQLite 应用版本号。0 创建基础结构，低于 2 建暂停开关，已知版本按既定规则打开。出现未知版本会停止。

#### ledger-21　把初始账户绑定到新账本

位置　`Ledger._initialize` · [executor_ledger.py 第 262 行](../scripts/executor_ledger.py#L262)

```sql
INSERT INTO ledger_meta VALUES(1,?,?,?)
```

绑定　`(self.account, self.environment, self.capital)`

只在建新库时执行，三个参数为账户、环境和初始资本，singleton 固定为 1。后续资金计算以此数为基准。

#### ledger-22　迁移增加持久化暂停开关

位置　`Ledger._initialize` · [executor_ledger.py 第 264 行](../scripts/executor_ledger.py#L264)

```sql
CREATE TABLE execution_control (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
paused INTEGER NOT NULL CHECK(paused IN (0,1)))
```

版本低于 2 时建单行表。singleton 必须为 1，paused 只能为 0 或 1。与下面的初值及版本号同一事务执行。

#### ledger-23　初始化为允许新开仓

位置　`Ledger._initialize` · [executor_ledger.py 第 266 行](../scripts/executor_ledger.py#L266)

```sql
INSERT INTO execution_control VALUES(1,0)
```

插入固定主键 1 和初值 0。日后恢复服务时读取数据库中的开关值，已有版本 2 的库不会重复插入。

#### ledger-24　标记升级完成

位置　`Ledger._initialize` · [executor_ledger.py 第 267 行](../scripts/executor_ledger.py#L267)

```sql
PRAGMA user_version=2
```

将应用版本置 2。外层初始化事务含账户校验，后续校验失败时模式与版本变更一起回滚。

#### ledger-25　核对打开的账本身份

位置　`Ledger._initialize` · [executor_ledger.py 第 268 行](../scripts/executor_ledger.py#L268)

```sql
SELECT account,environment,starting_capital FROM ledger_meta
WHERE singleton=1
```

读取固定主键的账户、运行环境和初始资本。调用者逐项与启动参数比较，缺行或不匹配即拒绝打开，防止误接其他账户或改写资金基线。

#### ledger-26　为嵌套操作建立保存点

位置　`Ledger.tx` · [executor_ledger.py 第 283 行](../scripts/executor_ledger.py#L283)

```sql
SAVEPOINT nested
```

连接已处于事务时命名一个 nested 保存点。内层可独立撤销本段，外层仍控制最终提交。

#### ledger-27　在最外层先取得写事务

位置　`Ledger.tx` · [executor_ledger.py 第 283 行](../scripts/executor_ledger.py#L283)

```sql
BEGIN IMMEDIATE
```

BEGIN IMMEDIATE 把写者之间的竞争放在事务开始处，避免同一预留被两连接同时核准。与进程内可重入锁协作，读账辅助方法也复用这套事务包装。

#### ledger-28　成功结束一层嵌套

位置　`Ledger.tx` · [executor_ledger.py 第 286 行](../scripts/executor_ledger.py#L286)

```sql
RELEASE nested
```

RELEASE 正常释放本层保存点，变更进入外层事务，外层仍可整体回滚。

#### ledger-29　成功提交最外层事务

位置　`Ledger.tx` · [executor_ledger.py 第 286 行](../scripts/executor_ledger.py#L286)

```sql
COMMIT
```

把本次所有相关行一并提交。外部发单路径必须等到发送权已经提交才调用交易所。

#### ledger-30　撤回内层失败操作

位置　`Ledger.tx` · [executor_ledger.py 第 289 行](../scripts/executor_ledger.py#L289)

```sql
ROLLBACK TO nested
```

ROLLBACK TO 只撤销保存点之后的修改，并保留保存点本身。因此错误分支紧接着还要 RELEASE。

#### ledger-31　在异常路径清除保存点

位置　`Ledger.tx` · [executor_ledger.py 第 290 行](../scripts/executor_ledger.py#L290)

```sql
RELEASE nested
```

清除刚被回滚的内层保存点，再将异常交给调用方。若上层捕获，外层事务可以继续。

#### ledger-32　回滚外层事务

位置　`Ledger.tx` · [executor_ledger.py 第 292 行](../scripts/executor_ledger.py#L292)

```sql
ROLLBACK
```

该行调用 sqlite3.Connection.rollback()，等效事务指令如下。一次写入部分完成时，预留、状态、明细和审计记录共同撤销。

#### ledger-33　列出待调度意图

位置　`Ledger.pending` · [executor_ledger.py 第 305 行](../scripts/executor_ledger.py#L305)

```sql
SELECT * FROM intents WHERE state IN ('READY','SUBMITTING','ACCEPTED','UNKNOWN','PARTIAL') ORDER BY last_check,id
```

展开 PENDING_SQL 后，返回待发以及可能已有成交的五种状态，按 last_check、id 排序。调用者对 READY 尝试取得发送权，对其他活动状态查原订单。这里没有失败后的到期过滤。

#### ledger-34　验证新退出意图与开仓相符

位置　`Ledger._reserve` · [executor_ledger.py 第 331 行](../scripts/executor_ledger.py#L331)

```sql
SELECT filled,state FROM intents WHERE id=:position_id AND action='OPEN'
AND game_id=:game_id AND channel=:channel AND asset=:asset AND market=:market
AND NOT EXISTS (SELECT 1 FROM settlements WHERE position_id=intents.id)
```

绑定　`r`

绑定 position_id、比赛、通道、资产和市场。子查询排除已经兑付的开仓。上层还要求开仓处于已核实终态、请求数量等于确认开仓量，防止退出到其他仓位。

#### ledger-35　统计账户已占用的仓位名额

位置　`Ledger._reserve` · [executor_ledger.py 第 339 行](../scripts/executor_ledger.py#L339)

```sql
SELECT count(*) FROM intents o LEFT JOIN intents c
ON c.position_id=o.id AND c.action='CLOSE' WHERE o.action='OPEN'
AND (o.reserved>0 OR o.filled>coalesce(c.filled,0))
AND NOT EXISTS(SELECT 1 FROM settlements WHERE position_id=o.id)
```

按开仓关联其唯一平仓，统计仍有开仓预留或已买份额大于已卖份额的记录，排除已兑付者。空平仓以 0 份处理。当前任何正余量都会占位，包括不足卖出精度的零头。

#### ledger-36　查找已有退出意图

位置　`Ledger.create` · [executor_ledger.py 第 350 行](../scripts/executor_ledger.py#L350)

```sql
SELECT * FROM intents WHERE action='CLOSE'
AND position_id=:position_id
```

绑定　`r`

只在 CLOSE 创建路径使用。以 position_id 选择原意图，再由业务层判断是参数重放、冲突或可再次处理的剩余量。

#### ledger-37　查找已有开仓意图

位置　`Ledger.create` · [executor_ledger.py 第 353 行](../scripts/executor_ledger.py#L353)

```sql
SELECT * FROM intents WHERE action='OPEN'
AND account=:account AND environment=:environment
AND game_id=:game_id AND channel=:channel
```

绑定　`r`

按账户、环境、比赛和通道取既有 OPEN。与 one_open 索引一致。应用还会比较资产、市场、预算和限价，决定返回同一 ID 或报冲突。

#### ledger-38　创建可扫描的意图并保存预留

位置　`Ledger.create` · [executor_ledger.py 第 375 行](../scripts/executor_ledger.py#L375)

```sql
INSERT INTO intents(id,account,environment,game_id,channel,action,asset,market,
position_id,reason,amount,limit_price,signal_at,deadline,state,reserved,last_check)
VALUES(:id,:account,:environment,:game_id,:channel,:action,:asset,:market,
:position_id,:reason,:amount,:limit_price,:signal_at,:deadline,:state,:reserved,:last_check)
```

绑定　`r`

绑定规范化请求和新生成的 id。写入初始状态、期限、初始预留及检查时间。过期时写 EXPIRED_UNSENT 和零预留。与业务键检查、名额/现金核定及首个事件共享事务。

#### ledger-39　登记一次发送尝试

位置　`Ledger.claim` · [executor_ledger.py 第 398 行](../scripts/executor_ledger.py#L398)

```sql
INSERT INTO attempts(id,intent_id,number,state,amount,limit_price,created_at,updated_at)
VALUES(:attempt_id,:id,:attempt_no,:state,:reserved,:limit_price,:last_check,:last_check)
```

绑定　`i`

保存递增的尝试号、该次可用的预算或份额、限价及初始时间，状态为 SUBMITTING。意图也在本事务更新，唯一索引保障同一业务意图仅一个活动尝试。

#### ledger-40　取得本订单已经确认的累计量

位置　`Ledger.apply` · [executor_ledger.py 第 461 行](../scripts/executor_ledger.py#L461)

```sql
SELECT coalesce(sum(quantity),0) FROM fills
WHERE order_id=?
```

绑定　`(i['order_id'],)`

根据精确 order_id 求本地明细份额总和。无明细返回 0，结合交易所累计量和已观察上限判断能否结束或释放预留。

#### ledger-41　单调更新已观察撮合量并读回

位置　`Ledger.apply` · [executor_ledger.py 第 463 行](../scripts/executor_ledger.py#L463)

```sql
UPDATE attempts SET matched_high_water=max(matched_high_water,?,?,?)
WHERE id=? RETURNING matched_high_water
```

绑定　`(amount, total, visible_matched, attempt_id)`

max 比较原上限、本地已确认总量、快照申报总量和本次去重明细的可见总量。RETURNING 取本次事务内的新上限。较早快照的 0 不能推翻先前已经见到的成交。

#### ledger-42　拒绝重复兑付或凭据挪用

位置　`Ledger.settle` · [executor_ledger.py 第 499 行](../scripts/executor_ledger.py#L499)

```sql
SELECT position_id,venue_event,leg_id,shares,payout,fee FROM settlements
WHERE position_id=:position_id OR (venue_event=:venue_event AND leg_id=:leg_id)
```

绑定　`r`

按仓位或按外部事件与明细号任一命中取旧分录。仓位、外部键、份额、付款和费用相同则复用，互相矛盾则报冲突。

#### ledger-43　核对兑付时实际尚余份额

位置　`Ledger.settle` · [executor_ledger.py 第 511 行](../scripts/executor_ledger.py#L511)

```sql
SELECT coalesce(sum(filled),0),count(*) FILTER(WHERE state IN ('READY','SUBMITTING','ACCEPTED','UNKNOWN','PARTIAL'))
FROM intents WHERE position_id=? AND action='CLOSE'
```

绑定　`(i['id'],)`

按仓位汇总所有退出意图已卖量，并用 FILTER 数仍可能发出或成交的退出意图。存在未决退出、或兑付份额不等于已买减已卖时拒绝兑付。

#### ledger-44　入账经核实的兑付款

位置　`Ledger.settle` · [executor_ledger.py 第 516 行](../scripts/executor_ledger.py#L516)

```sql
INSERT INTO settlements(position_id,venue_event,leg_id,shares,payout,fee,actor,evidence,at)
VALUES(:position_id,:venue_event,:leg_id,:shares,:payout,:fee,:actor,:evidence,:at)
```

绑定　`r`

绑定仓位、外部事件、明细 ID、份额、付款、费用、操作人、证据和记录时间。与审计事件同一事务提交，此后资金、名额与赛后报告都能按原凭据重算。

<a id="sql-reports"></a>

### 对账流水、触发器、报告、指标与复核 · `executor_reports.py`

#### reports-01　保存账户成交源的原始明细

位置　`Reports.__init__` · [executor_reports.py 第 15 行](../scripts/executor_reports.py#L15)

```sql
CREATE TABLE IF NOT EXISTS external_fills (
 order_id TEXT NOT NULL,id TEXT NOT NULL,asset TEXT NOT NULL,side TEXT NOT NULL,
 quantity INTEGER NOT NULL,price INTEGER NOT NULL,notional INTEGER NOT NULL,fee INTEGER NOT NULL,time INTEGER NOT NULL,
 game_id TEXT NOT NULL DEFAULT '',confirmed INTEGER NOT NULL,disposition TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(order_id,id)
);
```

复合主键为交易所订单号和明细 ID。asset、side、quantity、price、notional、fee、time 保存外部经济事实。game_id 允许未知，confirmed 表明是否已确认，disposition 记录人工认定。它与本地 fills 独立，才能发现外部已成交而本地少记的情况。

#### reports-02　按资产查询外部明细

位置　`Reports.__init__` · [executor_reports.py 第 21 行](../scripts/executor_reports.py#L21)

```sql
CREATE INDEX IF NOT EXISTS external_fills_asset ON external_fills(asset);
```

普通索引提升同标的待核检查和报告重新归属查询。asset 只能用于筛选，不能单凭相同标的认领成交。

#### reports-03　保存单账户分页游标

位置　`Reports.__init__` · [executor_reports.py 第 22 行](../scripts/executor_reports.py#L22)

```sql
CREATE TABLE IF NOT EXISTS feed_progress (singleton INTEGER PRIMARY KEY CHECK(singleton=1), cursor TEXT NOT NULL);
```

一个数据库只对应一个账户，singleton=1 限定一行，cursor 存下一次拉取账户流水所需的不透明字符串。

#### reports-04　去重保存策略决策

位置　`Reports.__init__` · [executor_reports.py 第 23 行](../scripts/executor_reports.py#L23)

```sql
CREATE TABLE IF NOT EXISTS decisions (
 game_id TEXT NOT NULL,source_id TEXT NOT NULL,at INTEGER NOT NULL,body TEXT NOT NULL,
 PRIMARY KEY(game_id,source_id)
);
```

用 (game_id,source_id) 标识一次输入事件，at 是决策时间，body 是完整决策 JSON。相同输入重放时由业务层对照 JSON 内容，避免同键承载不同决定。

#### reports-05　记录比赛结束

位置　`Reports.__init__` · [executor_reports.py 第 27 行](../scripts/executor_reports.py#L27)

```sql
CREATE TABLE IF NOT EXISTS completed_games (game_id TEXT PRIMARY KEY, finished_at INTEGER NOT NULL);
```

game_id 唯一，finished_at 记首次登记的结束时间。这一行触发报告版本管理，比赛结束本身不会生成兑付成交。

#### reports-06　保留赛后报告历次版本

位置　`Reports.__init__` · [executor_reports.py 第 28 行](../scripts/executor_reports.py#L28)

```sql
CREATE TABLE IF NOT EXISTS game_reports (game_id TEXT NOT NULL,revision INTEGER NOT NULL,at INTEGER NOT NULL,body TEXT NOT NULL,
 PRIMARY KEY(game_id,revision));
```

主键为比赛加修订号。at 是生成时间，body 是当时完整报告 JSON。后续修正追加下一版，因此可以恢复差异被发现和被修复的顺序。

#### reports-07　记录人工复核动作

位置　`Reports.__init__` · [executor_reports.py 第 30 行](../scripts/executor_reports.py#L30)

```sql
CREATE TABLE IF NOT EXISTS review_actions (id INTEGER PRIMARY KEY,at INTEGER NOT NULL,actor TEXT NOT NULL,
 evidence TEXT NOT NULL,operation TEXT NOT NULL,subject TEXT NOT NULL,before_state TEXT NOT NULL,after_state TEXT NOT NULL);
```

递增主键决定读取顺序，at、actor、evidence 记录谁在何时依据什么操作。operation、subject、before_state、after_state 表明对象和结果。与受影响分录在一个事务内提交。

#### reports-08　排队等待重算的比赛

位置　`Reports.__init__` · [executor_reports.py 第 32 行](../scripts/executor_reports.py#L32)

```sql
CREATE TABLE IF NOT EXISTS dirty_reports (game_id TEXT PRIMARY KEY);
```

仅存 game_id 主键，多条触发器可把同一场比赛标记为待刷新而只保留一个任务。在实际生成报告后移除该标记。

#### reports-09　已结束比赛新添意图时请求刷新

位置　`Reports.__init__` · [executor_reports.py 第 33 行](../scripts/executor_reports.py#L33)

```sql
CREATE TRIGGER IF NOT EXISTS report_new_intent AFTER INSERT ON intents BEGIN
 INSERT OR IGNORE INTO dirty_reports SELECT game_id FROM completed_games WHERE game_id=NEW.game_id;
END;
```

AFTER INSERT 在新意图落库后运行。内部 INSERT OR IGNORE 从已结束比赛中选择 NEW.game_id。未结束的比赛选不到行，重复通知则复用已有待刷新项。

#### reports-10　报告相关字段变化时请求刷新

位置　`Reports.__init__` · [executor_reports.py 第 36 行](../scripts/executor_reports.py#L36)

```sql
CREATE TRIGGER IF NOT EXISTS report_changed_intent AFTER UPDATE ON intents
 WHEN (OLD.state,OLD.filled,OLD.cost,OLD.fee,OLD.order_id,OLD.limit_price)!=(NEW.state,NEW.filled,NEW.cost,NEW.fee,NEW.order_id,NEW.limit_price) BEGIN
 INSERT OR IGNORE INTO dirty_reports SELECT game_id FROM completed_games WHERE game_id=NEW.game_id;
END;
```

AFTER UPDATE 比较状态、成交量、金额、费用、订单号和限价。元组任一值变化才执行内部 INSERT OR IGNORE，仅将已结束的对应比赛加入待刷新表，频繁更新 last_check 本身不会持续触发。

#### reports-11　外部成交流入后标记受影响比赛

位置　`Reports.__init__` · [executor_reports.py 第 40 行](../scripts/executor_reports.py#L40)

```sql
CREATE TRIGGER IF NOT EXISTS report_new_fill AFTER INSERT ON external_fills BEGIN
 INSERT OR IGNORE INTO dirty_reports SELECT game_id FROM completed_games WHERE game_id=NEW.game_id OR game_id IN
 (SELECT game_id FROM intents WHERE asset=NEW.asset AND (NEW.game_id='' OR id IN(SELECT intent_id FROM attempts WHERE order_id=NEW.order_id)));
END;
```

内部 INSERT OR IGNORE 选已结束比赛。优先匹配 NEW.game_id，也匹配关联意图的标的和订单身份。外部比赛未知时相同标的的相关比赛均可刷新。这样迟到或未归属的成交也能出现在报告里。

#### reports-12　外部成交被确认、认领或修正时重算

位置　`Reports.__init__` · [executor_reports.py 第 44 行](../scripts/executor_reports.py#L44)

```sql
CREATE TRIGGER IF NOT EXISTS report_changed_fill AFTER UPDATE ON external_fills
 WHEN (OLD.game_id,OLD.confirmed,OLD.notional,OLD.fee,OLD.disposition)!=(NEW.game_id,NEW.confirmed,NEW.notional,NEW.fee,NEW.disposition) BEGIN
 INSERT OR IGNORE INTO dirty_reports SELECT game_id FROM completed_games WHERE game_id IN (NEW.game_id,OLD.game_id)
 OR game_id IN(SELECT game_id FROM intents WHERE asset=NEW.asset);
END;
```

比较 game_id、confirmed、notional、fee 和 disposition。内部 INSERT OR IGNORE 将新旧比赛及持有相同资产的已结束比赛加入队列。归属变化需要检查原先显示这条明细的报告。

#### reports-13　账户首次完成一页观察后刷新

位置　`Reports.__init__` · [executor_reports.py 第 49 行](../scripts/executor_reports.py#L49)

```sql
CREATE TRIGGER IF NOT EXISTS report_first_account_page AFTER INSERT ON feed_progress BEGIN
 INSERT OR IGNORE INTO dirty_reports SELECT game_id FROM completed_games;
END;
```

feed_progress 首次 INSERT 时运行。内部 INSERT OR IGNORE 将全部已结束比赛加入队列，以便此前因缺少账户流水而待核的报告重新评估。后续仅更新游标不会反复触发。

#### reports-14　兑付入账后更新相应报告

位置　`Reports.__init__` · [executor_reports.py 第 52 行](../scripts/executor_reports.py#L52)

```sql
CREATE TRIGGER IF NOT EXISTS report_settlement AFTER INSERT ON settlements BEGIN
 INSERT OR IGNORE INTO dirty_reports SELECT c.game_id FROM completed_games c JOIN intents i ON i.game_id=c.game_id WHERE i.id=NEW.position_id;
END;
```

settlements 新行的 position_id 关联到原意图，再关联已结束比赛。内部 INSERT OR IGNORE 只标记该比赛，以更新剩余份额与兑付款。

#### reports-15　订单获得归属后刷新受影响报告

位置　`Reports.__init__` · [executor_reports.py 第 55 行](../scripts/executor_reports.py#L55)

```sql
CREATE TRIGGER IF NOT EXISTS report_assigned_order AFTER UPDATE OF order_id ON attempts
 WHEN NEW.order_id IS NOT NULL AND NEW.order_id IS NOT OLD.order_id BEGIN
 INSERT OR IGNORE INTO dirty_reports
 SELECT c.game_id FROM completed_games c WHERE EXISTS (
   SELECT 1 FROM external_fills f WHERE f.order_id=NEW.order_id AND
    (f.game_id=c.game_id OR (f.game_id='' AND EXISTS
     (SELECT 1 FROM intents i WHERE i.game_id=c.game_id AND i.asset=f.asset))));
END;
```

仅当 attempts.order_id 更新为不同的非空值才运行。内部写入从 completed_games 选择受已存在外部明细影响的比赛：有 game_id 时直接匹配，未知时按持有相同标的的比赛检查。可修正人工绑定前被多场列为待认领的明细。

#### reports-16　先尝试插入一条外部流水

位置　`_observe_fill` · [executor_reports.py 第 100 行](../scripts/executor_reports.py#L100)

```sql
INSERT INTO external_fills(order_id,id,asset,side,quantity,price,notional,fee,time,game_id,confirmed)
VALUES(:order_id,:id,:asset,:side,:quantity,:price,:notional,:fee,:time,:game_id,:confirmed)
ON CONFLICT(order_id,id) DO NOTHING
```

绑定　`value`

用命名参数保存收到的完整明细。复合键冲突时 DO NOTHING，通过 rowcount 区分首次观察与重叠回读。没有覆盖既有已确认数据。

#### reports-17　对重复外部明细做有条件的升级

位置　`_observe_fill` · [executor_reports.py 第 109 行](../scripts/executor_reports.py#L109)

```sql
UPDATE external_fills SET confirmed=max(confirmed,:confirmed),
 notional=CASE WHEN :confirmed THEN :notional ELSE notional END,
 fee=CASE WHEN :confirmed THEN :fee ELSE fee END,
 game_id=CASE WHEN game_id='' THEN :game_id ELSE game_id END
WHERE order_id=:order_id AND id=:id AND asset=:asset AND side=:side AND quantity=:quantity AND price=:price
 AND (game_id='' OR :game_id='' OR game_id=:game_id)
 AND (confirmed=0 OR :confirmed=0 OR (fee=:fee AND notional=:notional))
```

绑定　`value`

confirmed 取新旧最大值。只用已确认版本更新金额和费用，只给原空比赛号补归属。WHERE 要求经济身份和数量、价格一致，也拒绝已确认金额或费用被悄悄改写。匹配零行时 Python 抛冲突，本页和游标一起回滚。

#### reports-18　建立单场报告的初始状态

位置　`_game_summary` · [executor_reports.py 第 123 行](../scripts/executor_reports.py#L123)

```sql
SELECT EXISTS(SELECT 1 FROM completed_games WHERE game_id=?), EXISTS(SELECT 1 FROM feed_progress)
```

绑定　`(game,)`

一个 SELECT 返回两个布尔值。第一个判断指定比赛有无终场标记，第二个判断账户是否已观察过分页。两者齐备才能开始判为 MATCHED，后续明细差异仍会把状态置为 PENDING。

#### reports-19　生成本地意图与仓位报告行

位置　`_game_summary` · [executor_reports.py 第 126 行](../scripts/executor_reports.py#L126)

```sql
SELECT i.channel,i.action,i.asset,i.id AS intent_id,i.position_id,i.state,i.amount AS planned,
       i.limit_price,i.filled AS ledger_quantity,i.cost AS ledger_cost,i.fee AS ledger_fee,
       coalesce(s.shares,0) AS settled_shares,coalesce(s.payout,0) AS settlement_payout,
       coalesce(s.fee,0) AS settlement_fee,
       CASE WHEN i.action='OPEN' THEN i.filled-coalesce(s.shares,0)-coalesce((
          SELECT sum(c.filled) FROM intents c WHERE c.action='CLOSE' AND c.position_id=i.id
       ),0) ELSE 0 END AS remaining_shares
FROM intents i LEFT JOIN settlements s ON s.position_id=i.id
WHERE i.game_id=? ORDER BY i.channel,i.action,i.id
```

绑定　`game`

按比赛读取通道、动作、原计划、限价、已入账数量、成本和费用。LEFT JOIN 带入可选兑付。子查询累计同仓平仓量，开仓余量 = 已买 − 已兑付 − 已卖。平仓行自身余量列为 0。coalesce 处理没有兑付或平仓的情况，排序确保报告稳定。

#### reports-20　汇集对账所用的外部成交及归属

位置　`_game_summary` · [executor_reports.py 第 146 行](../scripts/executor_reports.py#L146)

```sql
SELECT e.id,e.order_id,e.asset,e.side,e.game_id,e.quantity,e.price,e.notional,e.fee,
       e.time,e.confirmed,e.disposition,a.intent_id
FROM external_fills e LEFT JOIN attempts a ON a.order_id=e.order_id
LEFT JOIN intents i ON i.id=a.intent_id
WHERE i.game_id=? OR (i.id IS NULL AND (e.game_id=? OR (e.game_id='' AND EXISTS
      (SELECT 1 FROM intents x WHERE x.game_id=? AND x.asset=e.asset))))
ORDER BY e.time,e.order_id,e.id
```

绑定　`game, game, game`

从 external_fills 左连接 attempts 和 intents。已关联订单按所属意图的比赛取数。未知订单可按外部比赛号，或在比赛号为空时按本场资产列为待核。按时间、订单号、明细号排序。后续逐通道比较数量、金额和费用，手工交易单列。

#### reports-21　恢复上次账户流水进度

位置　`Reports.cursor` · [executor_reports.py 第 201 行](../scripts/executor_reports.py#L201)

```sql
SELECT cursor FROM feed_progress WHERE singleton=1
```

查单行 feed_progress 的 cursor。首次启动无行，调用方返回空串，从初始页开始。

#### reports-22　原子推进分页游标

位置　`Reports.observe` · [executor_reports.py 第 210 行](../scripts/executor_reports.py#L210)

```sql
INSERT INTO feed_progress VALUES(1,?)
ON CONFLICT(singleton) DO UPDATE SET cursor=excluded.cursor
```

绑定　`(cursor,)`

首次插入 singleton=1，已有行用 excluded.cursor 覆盖。它在本页所有成交核对通过后执行，同一事务失败时下次仍从前一个游标开始。

#### reports-23　记录首次终场时间

位置　`Reports.summarize` · [executor_reports.py 第 217 行](../scripts/executor_reports.py#L217)

```sql
INSERT OR IGNORE INTO completed_games VALUES(?,?)
```

绑定　`(game, now)`

INSERT OR IGNORE 利用 game_id 主键使重复 finish 请求不会覆盖首次时间，随后汇总该场报告。

#### reports-24　定位上一版报告

位置　`Reports.summarize` · [executor_reports.py 第 220 行](../scripts/executor_reports.py#L220)

```sql
SELECT revision,body FROM game_reports WHERE game_id=?
ORDER BY revision DESC LIMIT 1
```

绑定　`(game,)`

绑定比赛号按 revision 倒排取首行，只读修订号和 JSON。调用者解析后比较对象，字段顺序或缩进差异不会制造新版本。

#### reports-25　追加内容发生变化的新版本

位置　`Reports.summarize` · [executor_reports.py 第 226 行](../scripts/executor_reports.py#L226)

```sql
INSERT INTO game_reports VALUES(?,?,?,?)
```

绑定　`(game, revision, now, _json(report))`

依次绑定比赛、下一修订号、当前毫秒和排序序列化的报告正文。仅已结束比赛且相对上一版内容有变化时执行。

#### reports-26　结束一次待刷新任务

位置　`Reports.summarize` · [executor_reports.py 第 228 行](../scripts/executor_reports.py#L228)

```sql
DELETE FROM dirty_reports WHERE game_id=?
```

绑定　`(game,)`

按比赛号删除 dirty_reports 的任务，与汇总结果和可选的新修订同事务。若出错回滚，任务保留。

#### reports-27　获得本轮要重算的比赛

位置　`Reports.refresh` · [executor_reports.py 第 233 行](../scripts/executor_reports.py#L233)

```sql
SELECT game_id FROM dirty_reports ORDER BY game_id
```

先取按比赛号排序的待刷新列表，结束该读取事务，再逐场调用 summarize。减少一次事务占用所有重算的时间。

#### reports-28　生成值班告警摘要

位置　`Reports.alert_summary` · [executor_reports.py 第 239 行](../scripts/executor_reports.py#L239)

```sql
SELECT (SELECT coalesce(max(?-created_at),0) FROM attempts WHERE state IN ('SUBMITTING','UNKNOWN')),
(SELECT count(*) FROM external_fills f WHERE f.confirmed=1 AND f.disposition='' AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.order_id=f.order_id)),
(SELECT count(*) FROM game_reports r WHERE revision=(SELECT max(r2.revision)
 FROM game_reports r2 WHERE r2.game_id=r.game_id) AND
 (json_extract(body,'$.buy_difference')!=0 OR json_extract(body,'$.sell_difference')!=0))
```

绑定　`(now,)`

三个标量子查询分别给出未知/发送中尝试的最大年龄、已确认且没有认领的外部明细数、最新赛后报告中买卖数量有差额的比赛数。按每个比赛最大 revision 过滤旧报告。当前汇总只看总买卖数量差额，费用差或通道间互相抵消的数量差可能只反映在报告待核状态。

#### reports-29　统计未决尝试及超时级别

位置　`Reports._metrics` · [executor_reports.py 第 254 行](../scripts/executor_reports.py#L254)

```sql
SELECT count(*),coalesce(max(?-created_at),0),coalesce(sum(?-created_at>=30000),0),
coalesce(sum(?-created_at>=300000),0) FROM attempts WHERE state IN ('UNKNOWN','SUBMITTING')
```

绑定　`(now, now, now)`

在 SUBMITTING、UNKNOWN 范围统计总数、最老年龄、达到 30 秒及 5 分钟的数量。三个绑定参数均为本次采样的 now，空集最大值和计数按 0 展示。

#### reports-30　计算提交到可见成交状态的最大延迟

位置　`Reports._metrics` · [executor_reports.py 第 258 行](../scripts/executor_reports.py#L258)

```sql
SELECT coalesce(max(e.at-a.created_at),0) FROM events e
JOIN attempts a ON a.id=e.attempt_id
WHERE e.state IN ('FILLED','PARTIAL','PARTIAL_FINAL')
```

events 按 attempt_id 连 attempts，匹配 FILLED、PARTIAL、PARTIAL_FINAL 状态，取事件时间减尝试创建时间的最大值。指标是数据库观察到的最大时间差，并不等同于交易所网络往返延迟。

#### reports-31　统计需人工查归属的明细

位置　`Reports._metrics` · [executor_reports.py 第 261 行](../scripts/executor_reports.py#L261)

```sql
SELECT count(*) FROM external_fills f WHERE f.confirmed=1 AND f.disposition='' AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.order_id=f.order_id)
```

展开 UNASSIGNED_COUNT 和 NO_OWNER。只数已确认、尚未标 manual 且找不到对应 attempts.order_id 的外部明细，供 metrics 返回。单位是明细条数。

#### reports-32　读取意图全部发送尝试

位置　`Reports._detail` · [executor_reports.py 第 269 行](../scripts/executor_reports.py#L269)

```sql
SELECT id,number,coalesce(order_id,'') AS order_id,state,created_at,updated_at FROM attempts
WHERE intent_id=? ORDER BY number
```

绑定　`intent_id`

给定 intent_id，按尝试序号返回 ID、编号、订单号、状态及创建/更新时间。coalesce 将数据库中未知订单号的 NULL 转成接口空字符串。

#### reports-33　读取意图时间线

位置　`Reports._detail` · [executor_reports.py 第 273 行](../scripts/executor_reports.py#L273)

```sql
SELECT at,attempt_id,state,reason FROM events WHERE intent_id=? ORDER BY seq
```

绑定　`intent_id`

复用 EVENTS 模板，以 intent_id 过滤、seq 排序，返回每次处理的时间、尝试、状态及原因，用于订单详情。

#### reports-34　找到该意图最早已确认成交

位置　`Reports._detail` · [executor_reports.py 第 274 行](../scripts/executor_reports.py#L274)

```sql
SELECT min(time) FROM fills WHERE intent_id=?
```

绑定　`(intent_id,)`

取 fills.time 最小值，空集合产生 NULL。调用方在结果为真时返回 first_fill_at，用于把实际成交映射回比赛时间。

#### reports-35　比较重复决策的正文

位置　`Reports._decision` · [executor_reports.py 第 296 行](../scripts/executor_reports.py#L296)

```sql
SELECT body FROM decisions WHERE game_id=? AND source_id=?
```

绑定　`key`

以 game_id 与 source_id 查旧 JSON。应用解码、规范化后与本次正文对照：相同则复用，不同则返回冲突。

#### reports-36　写入首次观察到的决策

位置　`Reports._decision` · [executor_reports.py 第 298 行](../scripts/executor_reports.py#L298)

```sql
INSERT INTO decisions VALUES(?,?,?,?)
```

绑定　`(*key, decision['at'], raw)`

四列依表序为比赛、来源 ID、事件时间、完整正文。与上一步查询在同一事务内，之后可按比赛恢复为何触发、跳过或受理。

#### reports-37　将已核实的外部订单标为手工交易

位置　`Reports._resolve` · [executor_reports.py 第 315 行](../scripts/executor_reports.py#L315)

```sql
UPDATE external_fills SET disposition='manual'
WHERE order_id=? AND disposition='' AND NOT EXISTS
(SELECT 1 FROM attempts WHERE order_id=?)
```

绑定　`(order, order)`

仅更新给定订单下处置状态为空且未被任何本地尝试认领的外部明细，同一订单号绑定两次。匹配零行即冲突。对已确认的 manual 明细，账本会纳入账户现金计算。

#### reports-38　人工绑定前核查全部外部证据

位置　`Reports._resolve` · [executor_reports.py 第 332 行](../scripts/executor_reports.py#L332)

```sql
SELECT asset,side,game_id,disposition FROM external_fills WHERE order_id=?
```

绑定　`(order,)`

取订单号下所有明细的资产、方向、比赛及处置状态。调用者逐行匹配当前意图。标的/方向错、比赛冲突、已处置或者完全没有证据，都会拒绝绑定。

#### reports-39　人工补录时取精确已确认明细

位置　`Reports._resolve` · [executor_reports.py 第 344 行](../scripts/executor_reports.py#L344)

```sql
SELECT * FROM external_fills WHERE order_id=? AND id=?
AND confirmed=1 AND disposition=''
```

绑定　`(order, req['fill_id'])`

要求外部订单号、明细 ID 同时一致，并限定 confirmed=1 且未处置。结果交给 record_fill 的同一套去重逻辑，补录后仍需跟踪未知剩余量。

#### reports-40　人工判零前读取成交上限

位置　`Reports._resolve` · [executor_reports.py 第 354 行](../scripts/executor_reports.py#L354)

```sql
SELECT matched_high_water FROM attempts WHERE id=?
```

绑定　`(attempt,)`

按尝试主键读 matched_high_water。即便当前查单为空，过去曾见过正的撮合量也不允许把尝试改为零成交。

#### reports-41　在判零前搜索相反的账户证据

位置　`Reports._resolve` · [executor_reports.py 第 359 行](../scripts/executor_reports.py#L359)

```sql
SELECT 1 FROM external_fills f WHERE f.order_id=? OR
(f.asset=? AND f.disposition='' AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.order_id=f.order_id)) LIMIT 1
```

绑定　`(intent['order_id'], intent['asset'])`

匹配同一订单的任何外部明细，或同一标的上尚未处置、未能归到尝试的外部明细。LIMIT 1 命中就阻止判零，避免在归属不明时释放预留。

#### reports-42　持久化人工处置审计记录

位置　`Reports._resolve` · [executor_reports.py 第 375 行](../scripts/executor_reports.py#L375)

```sql
INSERT INTO review_actions(at,actor,evidence,operation,subject,before_state,after_state)
VALUES(?,?,?,?,?,?,?)
```

绑定　`(now, req['actor'], req['evidence'], op, subject, before, after)`

在绑定、补录、手工标识或核实零成交完成的同一事务中写时间、复核人、证据、操作、对象、前状态和后状态，支持事后复盘。

#### reports-43　按外部订单号找本地意图

位置　`Reports._trace` · [executor_reports.py 第 381 行](../scripts/executor_reports.py#L381)

```sql
SELECT intent_id FROM attempts WHERE order_id=?
```

绑定　`(order,)`

用 attempts.order_id 查所属 intent_id。未命中仍会取下一条查询的账户流水，因而可以追查尚未归属的外部订单。

#### reports-44　列出某外部订单的全部明细

位置　`Reports._trace` · [executor_reports.py 第 382 行](../scripts/executor_reports.py#L382)

```sql
SELECT id,order_id,side,asset,time,quantity,price,notional,fee,confirmed,disposition
FROM external_fills WHERE order_id=? ORDER BY time,id
```

绑定　`order`

精确按订单号过滤，按成交时间和明细 ID 排序，返回执行侧的标的、份额、价格、金额、费用、确认与处置状态，用于 trace。

#### reports-45　补充所归属意图的完整时间线

位置　`Reports._trace` · [executor_reports.py 第 387 行](../scripts/executor_reports.py#L387)

```sql
SELECT at,attempt_id,state,reason FROM events WHERE intent_id=? ORDER BY seq
```

绑定　`intent`

trace 复用与详情相同的 EVENTS，绑定上一步追溯得到的意图。只有外部明细且没有本地所有者时绑定空串，时间线为空。

#### reports-46　枚举已知比赛

位置　`Reports.dispatch` · [executor_reports.py 第 406 行](../scripts/executor_reports.py#L406)

```sql
SELECT game_id FROM intents UNION SELECT game_id FROM decisions
UNION SELECT game_id FROM completed_games UNION SELECT game_id FROM external_fills
WHERE game_id!='' ORDER BY game_id
```

用 UNION 合并意图、策略决策、终场记录和有比赛号的外部流水，自动去重再排序。即使某场只有跳过决策或终场报告也能被列出。

#### reports-47　按顺序恢复本场策略决策

位置　`Reports.dispatch` · [executor_reports.py 第 416 行](../scripts/executor_reports.py#L416)

```sql
SELECT body FROM decisions WHERE game_id=? ORDER BY at,source_id
```

绑定　`(game,)`

以比赛号取出决策正文，按 at 再按 source_id 排序。返回层逐行解析 JSON，重启或复盘时可恢复同一事件序列。

#### reports-48　读取单场修订历史

位置　`Reports.dispatch` · [executor_reports.py 第 419 行](../scripts/executor_reports.py#L419)

```sql
SELECT revision,at,body FROM game_reports
WHERE game_id=? ORDER BY revision
```

绑定　`(game,)`

按比赛筛选并按 revision 正序返回修订号、生成时间和原报告 JSON。调用者得到所有留存版本，便于比较迟到成交之前与之后的差额。

#### reports-49　列出仍无本地订单归属的外部明细

位置　`Reports.dispatch` · [executor_reports.py 第 424 行](../scripts/executor_reports.py#L424)

```sql
SELECT order_id,id,asset,side,quantity,price,fee,time,confirmed,
game_id,disposition FROM external_fills f WHERE NOT EXISTS (SELECT 1 FROM attempts a WHERE a.order_id=f.order_id) ORDER BY time,order_id,id
```

展开 NO_OWNER 后，反查 attempts，按时间、订单号、明细号排序。查询同时返回 disposition，已标手工但没有本地所有者的记录也可能出现在该列表。与只数未处置且已确认的告警指标口径不同。

#### reports-50　导出人工审核的操作历史

位置　`Reports.dispatch` · [executor_reports.py 第 428 行](../scripts/executor_reports.py#L428)

```sql
SELECT at,actor,evidence,operation,subject,before_state AS before,
after_state AS after FROM review_actions ORDER BY id
```

按 review_actions 递增 ID 返回时间、操作人、证据和对象，两个别名把 before_state、after_state 映射到接口的 before、after。

#### reports-51　切换是否接受新开仓

位置　`Reports.dispatch` · [executor_reports.py 第 440 行](../scripts/executor_reports.py#L440)

```sql
UPDATE execution_control SET paused=? WHERE singleton=1
```

绑定　`(paused,)`

用一个布尔参数更新 singleton=1 的 paused。决定持久化，后续建意图和取得发送权时检查，已发订单的核查和退出走各自路径。

<a id="sql-venues"></a>

### 模拟交易及实盘签名存档 · `executor_venues.py`

#### venues-01　建立模拟场所自己的订单记录

位置　`PaperVenue.__init__` · [executor_venues.py 第 42 行](../scripts/executor_venues.py#L42)

```sql
CREATE TABLE IF NOT EXISTS paper_orders(
seq INTEGER PRIMARY KEY, order_id TEXT NOT NULL UNIQUE, attempt TEXT NOT NULL UNIQUE,
game TEXT NOT NULL, asset TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL,
price INTEGER NOT NULL, filled_at INTEGER NOT NULL)
```

paper_orders 用 seq 整型主键模拟递增游标。外部订单号和 attempt 各自唯一。记录比赛、资产、方向、份额、价格和成交时间。与本地意图分表，测试可以独立统计实际创建了几张模拟订单。

#### venues-02　模拟交易所接受并成交一次

位置　`PaperVenue.submit` · [executor_venues.py 第 63 行](../scripts/executor_venues.py#L63)

```sql
INSERT INTO paper_orders(order_id,attempt,game,asset,side,qty,price,filled_at)
VALUES(?,?,?,?,?,?,?,?)
```

绑定　`(oid, intent['attempt_id'], intent['game_id'], intent['asset'], 'BUY' if buying else 'SELL', qty, price, now)`

写入 paper- 前缀的订单号、尝试 ID，以及按限价算出的成交数据。唯一约束阻止同一 attempt 重发。在该分支买单以预算除价格向下取整，卖单直接取全部预留份额，因此普通模拟撮合不能验证实盘的两位卖出精度。

#### venues-03　模拟按尝试查外部订单

位置　`PaperVenue.lookup` · [executor_venues.py 第 77 行](../scripts/executor_venues.py#L77)

```sql
SELECT * FROM paper_orders WHERE attempt=?
```

绑定　`(intent['attempt_id'],)`

用 attempt 而非时间或余额查一行。不在表内抛查单错误。找到后产生 FINAL 快照和稳定明细号。查单本身不会再创建订单。

#### venues-04　模拟账户成交分页

位置　`PaperVenue.trades` · [executor_venues.py 第 91 行](../scripts/executor_venues.py#L91)

```sql
SELECT * FROM paper_orders WHERE seq>? ORDER BY seq LIMIT 100
```

绑定　`(seq,)`

取严格大于上一 seq 的最多 100 行，升序返回。末行 seq 成为下一游标，空页沿用旧游标，供引擎识别本轮已追平。

#### venues-05　建立实盘签名存档表

位置　`LiveVenue.__init__` · [executor_venues.py 第 125 行](../scripts/executor_venues.py#L125)

```sql
CREATE TABLE IF NOT EXISTS live_prepared(
attempt TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE, sealed BLOB NOT NULL)
```

本地尝试 ID 是主键，离线算出的交易所订单号也唯一。sealed BLOB 保存由 nonce、密文和认证标签组成的加密载荷，以尝试 ID 为认证附加数据。数据库不需要保存签名私钥。

#### venues-06　预防对同一尝试重复传输

位置　`LiveVenue.submit` · [executor_venues.py 第 163 行](../scripts/executor_venues.py#L163)

```sql
SELECT order_id FROM live_prepared WHERE attempt=?
```

绑定　`(attempt,)`

签名前按 attempt 查询已有 prepared 身份。若存在，返回原订单号和 UNKNOWN，转入查证。该查询与后续的不可重复插入共同约束并发和恢复路径。

#### venues-07　取得发单前的总预留

位置　`LiveVenue.submit` · [executor_venues.py 第 166 行](../scripts/executor_venues.py#L166)

```sql
SELECT coalesce(sum(reserved),0) FROM intents
WHERE account=? AND environment=? AND action=? AND (action='OPEN' OR asset=?)
```

绑定　`(intent['account'], intent['environment'], intent['action'], intent['asset'])`

按账户、环境、动作汇总 reserved。OPEN 跨标的合计资金。CLOSE 仅合计同一标的的退出份额。该值传给签名适配器，与真实可用余额和授权额度比较。本句自身不会访问交易所。

#### venues-08　在发送前提交签名身份

位置　`LiveVenue.submit` · [executor_venues.py 第 189 行](../scripts/executor_venues.py#L189)

```sql
INSERT INTO live_prepared(attempt,order_id,sealed) VALUES(?,?,?)
```

绑定　`(attempt, oid, nonce + encrypted + tag)`

绑定 attempt、已计算的订单哈希和加密载荷，使用普通 INSERT。重复键不能覆盖原记录。事务完成之后才可能执行 POST，因此丢回执时仍有确定的追踪身份。

#### venues-09　按尝试恢复实盘查单身份

位置　`LiveVenue.lookup` · [executor_venues.py 第 208 行](../scripts/executor_venues.py#L208)

```sql
SELECT order_id FROM live_prepared WHERE attempt=?
```

绑定　`(intent['attempt_id'],)`

在 lookup 中从 prepared 表恢复订单号。如另有已入账回执则核对两者一致。无 prepared 行时调用者可用已绑定的外部订单号。缺乏可靠身份时保留未知，不据此重发。

#### venues-10　把账户流水映射回已知比赛

位置　`LiveVenue.trades` · [executor_venues.py 第 233 行](../scripts/executor_venues.py#L233)

```sql
SELECT game_id FROM intents i JOIN attempts a ON a.intent_id=i.id
JOIN live_prepared p ON p.attempt=a.id WHERE p.order_id=?
```

绑定　`(fill['order_id'],)`

intents 连 attempts，再连 live_prepared，通过带签名的准确 order_id 找 game_id。有命中才给该条外部回报添加本地比赛归属。资产名和时间不会单独作为认领凭据。

<a id="sql-api"></a>

### 接口健康检查 · `executor_api.py`

#### api-01　健康检查探测账本连接

位置　`ExecutorAPI.dispatch` · [executor_api.py 第 36 行](../scripts/executor_api.py#L36)

```sql
SELECT 1
```

HTTP 健康检查在账本事务里执行常量查询并取一行，成功才返回 ready 和运行环境。它能证明该连接可执行 SQL，不能证明市场行情新鲜或所有未决订单已经对平。

<a id="sql-t-ledger"></a>

### 账本测试中的 SQL · `test_executor_ledger.py`

#### t-ledger-01　验证外键约束实际开启

位置　`LedgerTest.test_transactional_v1_upgrade_and_restart` · [test_executor_ledger.py 第 71 行](../scripts/test_executor_ledger.py#L71)

```sql
PRAGMA foreign_keys
```

从测试连接读取生效设置，断言为 1。覆盖应用连接设置而不只是检查建表文本。

#### t-ledger-02　验证同步级别

位置　`LedgerTest.test_transactional_v1_upgrade_and_restart` · [test_executor_ledger.py 第 72 行](../scripts/test_executor_ledger.py#L72)

```sql
PRAGMA synchronous
```

读取当前同步配置，断言整数 2，即 FULL，确保测试使用的账本和预期的持久化配置一致。

#### t-ledger-03　验证 WAL 实际启用

位置　`LedgerTest.test_transactional_v1_upgrade_and_restart` · [test_executor_ledger.py 第 73 行](../scripts/test_executor_ledger.py#L73)

```sql
PRAGMA journal_mode
```

查询当前文件的日志模式并断言返回 wal，检测迁移或打开连接时设置是否生效。

#### t-ledger-04　构造缺少暂停表的旧库

位置　`LedgerTest.test_transactional_v1_upgrade_and_restart` · [test_executor_ledger.py 第 74 行](../scripts/test_executor_ledger.py#L74)

```sql
DROP TABLE execution_control
```

在测试临时账本删除 execution_control，配合下一条版本指令模拟迁移前状态。生产迁移不会执行这条删除。

#### t-ledger-05　标成版本 1

位置　`LedgerTest.test_transactional_v1_upgrade_and_restart` · [test_executor_ledger.py 第 75 行](../scripts/test_executor_ledger.py#L75)

```sql
PRAGMA user_version=1
```

把测试库的 user_version 回置 1，随后先用错误账户尝试打开，测试迁移失败是否连版本号一并回滚。

#### t-ledger-06　断言失败迁移未改变版本

位置　`LedgerTest.test_transactional_v1_upgrade_and_restart` · [test_executor_ledger.py 第 79 行](../scripts/test_executor_ledger.py#L79)

```sql
PRAGMA user_version
```

再次读取同一临时库的 user_version，应仍为 1，而非在账户校验失败时就被永久升级成 2。

#### t-ledger-07　断言正确迁移已提交

位置　`LedgerTest.test_transactional_v1_upgrade_and_restart` · [test_executor_ledger.py 第 82 行](../scripts/test_executor_ledger.py#L82)

```sql
PRAGMA user_version
```

用正确参数重新打开测试库，再读取 user_version 期望 2，与上一条失败路径相互对照。

#### t-ledger-08　确认升级时初始化了暂停值

位置　`LedgerTest.test_transactional_v1_upgrade_and_restart` · [test_executor_ledger.py 第 83 行](../scripts/test_executor_ledger.py#L83)

```sql
SELECT paused FROM execution_control
```

取迁移生成的唯一控制行，断言 paused 为 0。说明新表存在且有可用的初值。

#### t-ledger-09　在故障试验中创建临时对象

位置　`LedgerTest.test_atomic_schema_and_nested_transactions` · [test_executor_ledger.py 第 90 行](../scripts/test_executor_ledger.py#L90)

```sql
CREATE TABLE error_test (i);
```

作为传入 execute_schema 的第一条 DDL 创建 error_test。该建表属于外层事务，下一条故意失败时应一起回滚。

#### t-ledger-10　把故障注入同一段迁移

位置　`LedgerTest.test_atomic_schema_and_nested_transactions` · [test_executor_ledger.py 第 90 行](../scripts/test_executor_ledger.py#L90)

```sql
DELETE FROM missing;
```

第二条语句删除不存在的 missing 表的数据，预期触发 OperationalError，验证连续 DDL/DML 不会部分生效。仅在测试临时数据库执行。

#### t-ledger-11　核验 DDL 已随事务撤销

位置　`LedgerTest.test_atomic_schema_and_nested_transactions` · [test_executor_ledger.py 第 92 行](../scripts/test_executor_ledger.py#L92)

```sql
SELECT 1 FROM sqlite_master WHERE name='error_test'
```

到 SQLite 模式目录找 error_test，预期无行。这样能验证真实文件中没有留下上一步已成功执行过的建表结果。

#### t-ledger-12　验证内层保存点隔离

位置　`LedgerTest.test_atomic_schema_and_nested_transactions` · [test_executor_ledger.py 第 96 行](../scripts/test_executor_ledger.py#L96)

```sql
UPDATE intents SET amount=1
```

内层事务把测试意图金额改为 1 后故意抛异常。外层捕获后仍可提交。随后原意图存在且保留原金额。

#### t-ledger-13　在领取发送权前暂停开仓

位置　`LedgerTest.test_only_one_sender_and_hold_rechecked_before_dispatch` · [test_executor_ledger.py 第 136 行](../scripts/test_executor_ledger.py#L136)

```sql
UPDATE execution_control SET paused=1 WHERE singleton=1
```

测试直接将单行控制开关置 1，构造意图已接受、还未 dispatch 的时序。随后断言 claim 会因开仓暂停而被拒绝。

#### t-ledger-14　恢复开仓用于下一段并发测试

位置　`LedgerTest.test_only_one_sender_and_hold_rechecked_before_dispatch` · [test_executor_ledger.py 第 140 行](../scripts/test_executor_ledger.py#L140)

```sql
UPDATE execution_control SET paused=0 WHERE singleton=1
```

将同一行还原为 0，后续可以单独验证未归属外部成交和并发领取发送权的拦截效果。

#### t-ledger-15　构造已人工认领的外部交易

位置　`LedgerTest.test_only_one_sender_and_hold_rechecked_before_dispatch` · [test_executor_ledger.py 第 148 行](../scripts/test_executor_ledger.py#L148)

```sql
UPDATE external_fills SET disposition='manual' WHERE order_id='manual'
```

仅在夹具中为指定订单的明细设置 manual，解除该笔未归属成交造成的冻结。生产入口还需要记录操作人、证据和前后状态。

#### t-ledger-16　验证冲突批次没有部分入账

位置　`LedgerTest.test_conflicting_fill_batch_rolls_back_and_fee_overrun_blocks_claim` · [test_executor_ledger.py 第 218 行](../scripts/test_executor_ledger.py#L218)

```sql
SELECT count(*) FROM fills
```

计数所有本地成交行，预期为 0。一组明细中途发现前后矛盾时，早先插入的一条也必须从本事务回滚。

<a id="sql-t-engine"></a>

### 执行引擎测试中的 SQL · `test_executor_engine.py`

#### t-engine-01　制造数据库无法写入发送权的故障

位置　`EngineTests.test_claim_must_commit_before_send` · [test_executor_engine.py 第 88 行](../scripts/test_executor_engine.py#L88)

```sql
PRAGMA query_only=ON
```

把当前测试连接设为只读查询模式，再调用 step。断言发生 SQLite 写异常且 venue.submit 一次也没有调用，验证“先提交本地发送权”的故障边界。

#### t-engine-02　在模拟传输钩子里读取已存签名

位置　`EngineTests.test_signed_identity_survives_lost_reply_and_matches_go_format.exchange` · [test_executor_engine.py 第 171 行](../scripts/test_executor_engine.py#L171)

```sql
SELECT order_id,sealed FROM live_prepared
```

交易所替身从临时库取出 order_id 和 sealed，验证 POST 执行时身份已经持久化，解密后能还原预计签名。此用例只有一条 prepared 记录，所以无需 WHERE。

<a id="sql-t-reports"></a>

### 报告测试中的 SQL · `test_executor_reports.py`

#### t-reports-01　模拟相同报告的旧序列化格式

位置　`ReportsTest.test_first_observation_and_existing_go_report` · [test_executor_reports.py 第 85 行](../scripts/test_executor_reports.py#L85)

```sql
UPDATE game_reports SET body=?
```

绑定　`(json.dumps(report, indent=2),)`

按测试夹具把全部报告行的正文改成带缩进的 JSON。比较时按解析后的内容判断，应避免仅因 JSON 排版不同就新增修订。

#### t-reports-02　模拟已有的决策正文

位置　`ReportsTest.test_decision_idempotency_and_existing_serialized_json` · [test_executor_reports.py 第 145 行](../scripts/test_executor_reports.py#L145)

```sql
INSERT INTO decisions VALUES(?,?,?,?)
```

绑定　`('game', 'd', 1, json.dumps(decision))`

按表列顺序向临时库预插比赛、source_id、时间与旧格式 JSON。再次通过接口提交语义相同的决策应接受，实际内容不同则按冲突处理。
