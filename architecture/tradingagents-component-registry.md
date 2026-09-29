# TradingAgents 组件复用 / 新增 / 禁止登记

机器可校验的完整登记表是 `architecture/tradingagents-component-registry.json`，本文件给出架构评审摘要。任何阶段 2 组件在实现前都必须在 JSON 登记中有明确 disposition、现有资产、新增资产和边界。

## 关键现状结论

`SharedLLMBroker + 轻量多角色编排` 当前已完成统一模型接入层，业务编排仍待后续任务实现：

- `SharedLLMBroker` 已作为进程内库实现，具备统一优先级、公平并发、取消、fast/deep profile、结构化 schema、预算和调用审计；它没有增加服务或端口。
- TradingAgents `v0.3.1` 已按签名标签、精确 commit、源码及依赖 hash 嵌入当前镜像；最小原始角色图和固定 fixture 已在镜像内运行通过。
- TradingAgents LLM Adapter 已把 13 个原始/辅助角色全部导向 `SharedLLMBroker`，完整 22 节点图已用真实 LangChain Runnable 编译，并验证普通、结构化及反思调用；每次调用透传研究、角色、模型、延迟和 hash 审计字段。
- CN Data Adapter 已把 29 个工具、17 类工具和 A 股/境内基金/港股显式来源链注册到项目边界；指数身份、成分、宽度、行业轮动与流动性代理，以及 ETF/开放式基金的净值、跟踪、费用、持仓、经理、份额和申赎状态均走同一快照/证据链。技术指标及 ETF 跟踪误差只从已保存 OHLCV 本地确定性计算；Tushare 尚未配置用户 Token。
- 金融研究 checkpoint 已写入同一 SQLite，大 checkpoint 与终极报告写入现有 data volume 并用 SHA-256 校验；新进程可回退到最近兼容 checkpoint。金融长期记忆复用现有 RAGFlow 客户端和专用 KB 配置，RAGFlow 不可用时只降级记忆，不影响报告读取。
- 现有 `IntelWorker` 已注册 `financial_snapshot`、`financial_research`、`financial_verify`、`market_overview`、`paper_backtest` 五类任务，并补齐续租心跳、取消传播和 lease-owner fencing；仍复用 `intel_jobs`，失败研究不会阻塞同批 RSS 任务。
- `FinancialMarketScheduler` 已嵌入现有 `IntelWorker`：按 XSHG、XSHE、XHKG 各自时钟在启动补跑、盘前、开盘、五分钟盘中窗口、午休、收盘后及显式事件中生成六个基准快照和四类市场/默认市场脉搏概览；同一持久窗口跨重启只入队一次，行情 tick 不触发完整多代理研究。
- 阶段 2 汇总关卡已校验 14 份组件验收证据并重跑 282 项离线回归；既有 live 验收中的腾讯 `0700.HK`、沪股 `600000.SH`、深股 `000001.SZ`、上证 `000001.SH`、深证 `399001.SZ` 和恒生 `HSI.HK` 均带 Provider、观测时间、证据覆盖率、报告状态及禁止执行字段。Tushare 因用户 Token 未配置保持显式跳过，不伪装为可用。
- 聊天入口已在默认 `legacy_chat` 进程内 seam 上完成服务器时间、金融意图、稳定标的、广义市场和实时事实路由；`POST /api/chat/send` 与原请求体保持不变，query-through 在调用 Provider 前先发 `status`，并仍以 `chunk/done` 完成。新鲜快照直接返回，盘中过期快照经现有 Provider Router 同步刷新并入同一 SQLite，失败只回退为 stale；简单事实不调用通用 LLM 或完整 TradingAgents 研究。
- 完整研究路由已按标的/Universe、as_of、图版本、Provider profile 和配置 hash 查找未过期终态报告；未命中时在同一 SQLite 事务内创建 `financial_research_runs` 和现有 `intel_jobs.financial_research`。并发同请求共享 run/job，失败或取消不回退为通用模型观点；实际研究 runner 仍按 4.1 Gateway 任务接入现有 `intel-worker`。
- 聊天历史 ÷ 操作已在原清洗后增加回答级金融快照审阅：按标的、指标、币种、调整口径和有效区间合并，输出 `verified_current/historical/stale/conflicted`；冲突值全部保留，不调模型猜数，不删除原会话。
- 首页已在原 Dashboard 内增加独立金融 Feed：只在有效包含金融能力且金融开关开启时展示，分别读取行情快照、金融资讯和最新终极报告，并以“事实/原文/研究观点”明确分层；普通家办资讯统计不纳入快照或报告，关闭开关不删除历史。
- 终极报告入口已复用原登录态改为受保护的只读视图：默认一屏投影对象、时点、评级、置信度、核心理由、反证、风险、覆盖率和缺口，并可按分析师、多空、Trader、风险和 Portfolio Manager 展开保存的完整公开角色产物；prompt、chain-of-thought、模型配置与内部 JSON 不出接口。
- 金融产品能力已统一为“有效行业包组合＋父子开关”的服务端投影；全局主导航仅按该投影显示金融专区、TradingAgents 报告、模拟交易和策略回测，报告 API、完整研究创建及 worker 使用同一门禁。运行中已 claim 的安全任务允许完成，关闭后的排队/新任务显式跳过，历史数据保留。
- 阶段 3 产品关卡已在同一应用表面贯通普通聊天、金融实时/研究分流、×/÷、Dashboard 与终极报告；标的契约现同时保存数据库 ID 和 Provider 无关 `instrument_key`，旧会话可读时补齐。真实 AKShare smoke 已将上证、深证和平安银行结构化快照写入临时同构 SQLite，浏览器 E2E 只访问回环夹具且完整呈现 5 组公开代理产物。SQLite 健康探针/重连/关闭已与 worker 心跳共用连接锁，避免长研究续租竞态。阶段汇总与独立全量回归各 393 项通过；阶段 5 已补齐纸面账本、无前视回测、可重算指标、受控 Dashboard 及固定策略三次复现/重启恢复/零券商网络总关卡，后续进入阶段 6 安全、灰度、监控和生产验收。
- 阶段 6.1 已为 8 个嵌入式 Provider 和 5 个官方金融 RSS 建立 13 份授权/费用档案，记录业务与法务负责人、费用/凭据责任、缓存/展示/再分发权限、额度及复核期。45 个 Provider 端点和 5 个 RSS URL 已与实现精确对账；生产环境无档案、错配、过期、缺显式审批或标记禁止生产时均在 Router、Provider 基类、RSS 注册/启用和扫描层 fail-closed。AKShare、Yahoo 和 easyquotation 当前禁止生产；Tushare、Alpha Vantage 等付费/Key 来源必须另行具备有效订阅、接口权限与平台运维持有的凭据。
- 阶段 6.2 已增加统一的公开输出安全边界：应用配置与聊天配置 Secret 在日志、指标、JSON、SSE 和报告投影前脱敏，公开 URL 删除大小写不敏感的凭据参数与 fragment 并拒绝 userinfo、localhost 和私网字面地址；SafeHTTPClient 继续对 DNS/IP 和每个重定向逐跳复核。金融 SSE 按事件类型白名单输出，Dashboard Feed/报告只用文本节点和受控链接。RSS/网页正文被固定标为不可信数据，股票与指数图明确禁止其修改配置、选择/扩展工具或触发订单，未注册工具确定性失败。83 项相关攻击回归、522 项全量回归、Chromium 恶意 DOM/URL 回放、架构关卡及无网络镜像验收均通过，证据见 `architecture/financial-security-acceptance.json`。
- 阶段 6.3 已在同一 `intel-worker` 服务和镜像内用 supervisor 将 10 类聊天/RSS/快照核心任务与研究、核验、回测 3 类长任务分为两个显式进程 lane；仍复用同一 `intel_jobs` 与 SQLite，不增加服务、端口、队列或数据库。SharedLLMBroker 为聊天保留互动容量，队列优先级随等待老化；Provider 统一执行总并发、单源并发、有界准入和 429 共享冷却；SQLite 使用 WAL 与 2 秒默认 busy timeout，报告制品写入受大小、并发和等待上限保护。慢研究、慢 LLM、RSS、回测和 Dashboard 组合负载中互动/RSS/Dashboard p95 均低于配置预算，46 项相关回归、530 项全量回归、架构关卡和无网络镜像验收通过，证据见 `architecture/financial-resource-isolation-acceptance.json`。
- 阶段 6.4 已在现有 SQLite 审计/状态表上提供只读金融健康聚合：覆盖 Provider 权限/健康、RSS 成功与滞后、快照新鲜度和市场覆盖、worker 租约/积压、LLM 超时/p95、报告完整性、核验冲突、调用预算及进程内资源隔离。管理员端点只返回聚合、阈值和安全定位 ID；普通 Dashboard 只接收服务端可用性原因，不暴露 prompt、问题、回答、载荷、错误全文、凭据或 Token。27 项相关回归、534 项全量回归、架构关卡和无网络镜像验收通过，证据见 `architecture/financial-health-acceptance.json`。
- 阶段 6.5 已把金融能力固化为十级累积灰度：全部关闭 → RSS → 只读快照 → Dashboard → AI 事实路由 → 单股研究 → 指数/市场研究 → 历史 ×/÷ 金融核验 → 自动研究 → 模拟/回测。首次可直接开启 RSS，之后每次只能前进一级，并要求上一层完成配置观察窗口、总体健康和阶段冒烟；跳级、无观察起点、观察不足、健康降级或冒烟失败均拒绝写入。RSS、Provider/快照、产品导航、AI、研究路由、worker、历史操作、自动研究和模拟入口使用同一阶段投影；任意层可立即回退，运行中安全任务沿既有策略结束，排队/新任务跳过，历史数据保留且不需要数据库回滚。管理员可从配置页和只读 `/api/intel/financial/rollout` 查看当前层、观察进度、下一层决策与安全健康摘要；无新服务、端口、队列、数据库或表，证据见 `architecture/financial-rollout-acceptance.json`。
- 阶段 6.6 已固化 fail-closed 恢复策略和隔离演练：Provider/LLM 故障立即回退灰度且不动数据库，损坏报告按 SHA-256 回退上一有效版本，worker 停止后持久任务进入 retry 并由新 lease owner 接管，migration 中断整体回滚后幂等继续。健康数据库即使误传批准也禁止被旧备份覆盖；只有 integrity 失败且明确批准才允许向隔离新路径恢复。冻结标签代码已能读取应用当前 additive schema，并在 smoke 后保留一条合成的基线后合法数据；RTO/RPO 与产品、开发、测试、运维四类自动证据均由最终关卡记录，但不伪造真实生产审批。全程不增加服务、端口、队列、数据库或表，证据见 `architecture/financial-production-recovery-acceptance.json`。
- AI 助手“最新信息”现把通用信息需求拆为独立 `quote/news` 通道，未知证券经受控多来源准入后才能查询；行情与新闻共享请求 cutoff，但各自保留来源、时点和失败边界。SpaceX 空库金样已闭合发现、`SPCX.US` 提升、行情、新闻、组合回答及同会话仅行情追问；目标未核验或通道全失败时禁止通用模型补造。现有金融健康面新增 8 类只读路由指标，重复迁移和子开关回退保持候选、证券、快照、文章及审计历史不丢失。52 项专项与 595 项全量回归通过，证据见 `architecture/financial-latest-information-acceptance.json` 和 `architecture/financial-latest-release-readiness.json`；真实灰度仍需独立生产授权。
- 原子主张抽取已按结构化字段、本地规则、受约束 SharedLLMBroker 补充的顺序实现；事实以 `pending` 幂等写入同一 SQLite，观点和条件预测不能伪装成事实，所有数值、单位、币种、时点和 source span 必须回指终态报告。抽取成功本身不代表事实已核验，仍须通过时效和多源冲突裁决。
- Temporal Judge 已复用服务器请求时钟、现有交易所日历、Provider 新鲜度元数据及同库 verdict 表，确定性输出当前、历史、被替代、过期或证据不足；盘中/收盘后、财务期间、重述、修订、拆股和指数有效期均有显式规则与版本化审计。
- 多源冲突 Judge 已在 Temporal Judge 后按稳定标的、指标、期间、币种、单位、复权和观测时点裁决；独立性以底层来源谱系而不是 Provider 名称计算，未知谱系不假定独立，冲突价格不平均、币种不隐式换算。未决冲突复用既有 verdict 表形成待人工复核视图，并且永远不能进入当前事实。
- FinancialAnswerComposer 已接入原 AI 助手 `chunk/done` 流：只把 Temporal 与多源冲突双裁决后的主张投影为事实，并把已保存终态 TradingAgents 报告明确标为研究观点；评级、置信度和报告时点原样保留，旧报告与新快照并存时优先新事实并披露报告旧时点。每项事实必须有外部来源或受登录保护的本地快照链接，缺证据、冲突和未核验数字不能进入当前事实；RAG 时效门禁仍待后续任务。
- 每次合法聊天请求已由应用服务器捕获一次 UTC 时钟；“今天/昨天/本周/现在”按用户时区提示解析为绝对范围并写入现有 `chat_financial_routes`，客户端时钟不能覆盖服务器时钟，同会话省略追问可跨午夜继承上一条已保存的绝对范围。
- 不部署上游 Web/CLI、Ollama、Redis、数据库或向量库；运行时不拉代码、不安装包。

因此，计划中标记 `reuse` 的是现有完整能力，`extend` 是现有组件的增量扩展，`add_embedded` 是明确需要新开发或嵌入但不产生新服务的能力。不能把“未写新服务”理解成“当前已经有完整实现”。

## 架构表面冻结

| 项目 | 阶段 2 决定 |
|---|---|
| Compose 服务 | 保持 `crawler`、`worker`、`intel-worker`、`redis` |
| 对外端口 | 只保留 `8003` |
| LLM | 复用 AI 助手当前 local OpenAI-compatible 配置 |
| 关系数据、队列、checkpoint | 同一个 `crawler_articles.db`；复用 `intel_jobs` 并增量建金融表 |
| 可选缓存/协调 | 复用现有 Redis，不作为事实或 checkpoint 主存储 |
| 语义记忆 | 复用现有 RAGFlow 专用 KB |
| 大型报告文件 | 复用现有 data volume，SQLite 保存路径与 hash |
| 登录与权限 | 复用 Flask 全局鉴权、`login_required`、`admin_required` |
| 任务执行 | 在现有 `intel-worker` 注册金融 job type |
| TradingAgents | 锁定 Python 包并嵌入当前镜像；运行时不拉代码 |
| 交易 | 只允许纸面模拟和回测；禁止真实 broker/order gateway |

## 分组决策

| 能力组 | disposition | 当前落点 | 阶段 2 增量 |
|---|---|---|---|
| Flask、SSE、Dashboard | `reuse/extend` | `firecrawl_app.py`、`chat_api.py`、`chat_route_orchestrator.py`、`financial_realtime_query.py`、`financial_full_research.py`、`financial_sse.py`、`financial_feed.py`、`financial_report_view.py`、`intel_api.py`、`mapindex_api.py`；`/api/chat/send` 已支持显式协商的 `financial-sse-v1`，首页已接入独立金融 Feed和受登录保护的终极报告一屏摘要及角色 section 展开 | 保留 `/api/chat/send`，后续接入全量主张核验和回答生成门禁 |
| 认证 | `reuse` | `user_database.py`、`decorators.py` | 金融 API 沿用现有用户/管理员权限 |
| 配置 | `extend` | `config.py`、`financial_config.py`、`financial_rollout.py`、`config/financial_source_licenses.json`、配置 API/页面、`.env.example`；已提供有效行业包感知的产品能力 API，并统一门禁菜单、报告、研究创建和 worker；十级灰度只允许在健康观察后逐级前进，任意层可立即回退且不需要数据库回滚；授权环境及生产审批只由部署配置提供 | 后续授权复核只更新可审计档案与部署审批，不把测试权限自动提升为生产权限 |
| SQLite | `extend` | `sqlite_database.py`、`financial_schema.py`、`financial_chat_history.py`、`intel_schema.py`、`intel_database.py`；金融回答已事务关联 route、稳定 `instrument_key`、快照和报告版本；共享连接健康探针、重连和关闭受同一 RLock 保护；WAL 读与写锁等待边界已通过竞争验收 | 继续复用同一数据库；默认 busy timeout 2 秒，长 I/O 不进入写事务 |
| 任务平台 | `extend` | `intel_jobs`、`IntelWorker` 已注册五类金融 handler，具备 lease/retry/dedupe/heartbeat/cancel、过期 owner 隔离和等待老化；同一 `intel-worker` 服务/镜像由 supervisor 启动 core 与 long-financial 两个进程 lane | 不增加 worker 服务、队列或端口；任务类型必须完整且互斥分配到 lane |
| RSS/网页 | `reuse` | source → scan → candidate → article → classification；5 个金融 RSS 已绑定精确 URL 授权档案，生产注册、启用和扫描均 fail-closed | 与 Provider 快照并行，由 EvidenceResolver 关联；仅按档案缓存并展示摘要/原文链接，不隐式再分发 |
| 安全 HTTP | `extend` | `SafeHTTPClient` 逐跳 SSRF/重定向/大小限制、固定 SDK HTTPS 主机、统一 Secret/查询参数脱敏；公开 SSE/JSON/报告和前端链接均受白名单投影 | 后续只随新 Provider 授权增加固定端点，不开放任意 URL 工具 |
| 本地 LLM 配置 | `reuse` | AI 助手 local model runtime | 所有代理复用相同 base_url/model_id/key 来源 |
| SharedLLMBroker | `add_embedded` | `shared_llm_broker.py` 已提供进程内 Broker，并为 `chat_clarification/chat_fact` 保留互动容量 | 由 TradingAgents LLM Adapter 接入，不开放端口；慢批任务不得占满互动槽 |
| TradingAgents 多角色图 | `add_embedded` | `v0.3.1` 上游图已锁定嵌入，LLM Adapter 已接入 Broker，完整角色图编译验收通过 | 增加 Gateway，由 intel-worker 执行 |
| 金融 Provider | `add_embedded` | 统一契约、AKShare/Tushare/Yahoo/Alpha Vantage/FRED/Polymarket/easyquotation/官方证据适配器已可用；8 个来源均有授权/费用档案并在 Router 与基类双层校验，所有外部源默认关闭，生产还需未过期档案和显式审批；共享控制器限制总并发、单源并发和准入等待，并把 429 Retry-After 投影为共享冷却 | 接入 EvidenceResolver 与 TradingAgents 数据工具，统一快照与显式回退；付费/Key 来源先由平台运维确认订阅、接口权限和凭据归属 |
| A/H 股、指数与基金数据工具 | `add_embedded` | `TradingAgentsCNDataAdapter` 已提供显式来源链、29 个工具、确定性指标、指数身份/成分/宽度/轮动/流动性、基金净值/披露/条款及 snapshot/evidence 追踪 | 后续随 Provider 授权扩展恒指成分和港股宽度来源 |
| 市场时间 | `add_embedded` | XSHG/XSHE/XHKG 2026 官方日历、显式模板回退、启动补跑、交易阶段窗口、事件刷新和跨重启永久去重已可用 | 后续接入研究路由；按年度更新官方日历 |
| 标的/Universe | `add_embedded` | 时态标的注册表及版本化 Universe Planner 已接入意图解析和研究路由；每个标的同时返回内部数据库 ID 与 `country:venue:asset_type:code` 稳定键，裸六位代码继续强制澄清 | 后续核验和模拟层必须沿用同一稳定键，不得退回仅代码关联 |
| 股票研究图 | `add_embedded` | `StockResearchGraph` 已运行四类项目分析师、原始多空/经理/Trader/三类风险/Portfolio Manager，支持持久 JSON checkpoint 恢复并保存 14 类 section | 后续由 Gateway/worker 注册任务并传入已验收的持久化回调 |
| 指数研究图 | `add_embedded` | `IndexMarketResearchGraph` 已用上证/深证/恒指专用模板运行 13 个角色并保存 15 类 section，支持市场时钟、覆盖率/延迟展示和持久 checkpoint 恢复 | 后续由 Gateway/worker 注册任务；继续补充有授权的恒指成分和港股宽度来源 |
| ETF/基金 | `add_embedded` | `FundETFResearch` 已将 ETF 与开放式基金分流：ETF 使用行情、跟踪指数/成分、费用、跟踪误差和流动性；开放式基金使用净值、份额、持仓披露、经理、申赎、费用和基准；A/C 份额、币种及披露滞后显式处理 | 后续由 Gateway/worker 注册研究任务并交给终极报告/核验层 |
| 时效冲突核验 | `add_embedded` | `financial_claim_extractor.py` 已把终态报告拆成可回溯、默认待核验的原子事实，并隔离观点/预测；`financial_temporal_judge.py` 与 `financial_conflict_judge.py` 已完成时效和多源裁决；`financial_answer_composer.py` 已把双裁决事实和终态报告接入原 SSE；`financial_rag_gate.py` 已把报告、当前事实、历史事实和显式人工裁决分开登记，RAGFlow 仅发现候选，检索后按同库映射、标的、服务器绝对时点、`effective_to`、最新事实裁决及报告版本回查；阶段 4 held-out 冲突集已覆盖价格、财务、公告、指数成分和宏观修订，TradingAgents 报告单源未晋升事实 | 后续在阶段 7 用真实授权源回放并校准告警阈值，不用 held-out 集反向调规则 |
| 纸面模拟/回测 | `add_embedded` | “开启模拟数据”是服务端唯一可信开关；纸面交易/回测请求进入既有 `intel_jobs/paper_backtest`。`FinancialPaperLedger` 已复用同库账户/订单/成交/持仓表，按报告版本、策略版本、信号和执行快照完整关联，原子更新现金/成交/持仓，支持部分成交、费用、滑点、限价/止损、停牌、涨跌停、撤单、幂等和账本守恒；指数必须由用户确认可交易代理。`FinancialPointInTimeBacktester` 仅消费显式固定且 hash 校验通过的同库历史快照，按 `observed_at/available_at`、数据截止版本及成分有效期/来源观察时间形成信号，下一可交易观测才执行；保存策略、成本、滑点、基准、随机种子和数据版本，处理拆股分红、停牌、低频基金净值与覆盖缺口，并拒绝隐式换汇。`FinancialBacktestAnalytics` 再次校验数据版本和逐笔交易的执行快照，重建现金、持仓及日净值，原子保存收益、回撤、波动、夏普、胜率、换手、费用/滑点、基准相对收益、覆盖率、净值曲线和交易日志摘要；样本或基准不足只保存不可用原因。`FinancialSimulationView` 在同一 Dashboard 按所有者投影账户、持仓、纸面订单/成交、净值曲线、回撤、覆盖限制和完整 JSON 导出，并让报告、运行和证据互相追溯；每个首页分类可按用户和稳定 ID 独立折叠，阻尼动画可中断且适配键盘、读屏器、减少动画和移动端。Stage 5 golden 在禁止全部网络连接的守卫中用三份独立 SQLite 得到相同固定 hash，并用两个独立无网络容器对同一挂载文件库执行 seed/resume，验证回测/指标/账户/订单/成交幂等、账本守恒、关闭开关后的只读历史与零新增写入 | 阶段 5 已完成；零真实订单，后续阶段 6 继续做全局安全、授权、负载、监控和生产演练 |
| RAGFlow 与报告文件 | `reuse/extend` | `FinancialArtifactStore` 已复用 SQLite 和 data volume 实现原子报告/checkpoint 附件及 hash，并限制单文件大小、并发 fsync 和 I/O 等待；`FinancialSemanticMemory` 已复用现有 RAGFlow 客户端，上传前先在既有 `financial_artifacts.metadata_json` 保存受控投影及文档映射，支持幂等上传、时效检索门禁和超时降级 | 运维配置现有 RAGFlow 中的金融专用 KB；由 long-financial lane 在报告生成后调用 |

## 明确禁止

- 第二套 LLM runtime、Ollama/vLLM 容器或新模型端口。
- 上游 TradingAgents Web、CLI 守护进程或独立 API。
- PostgreSQL、MySQL、MongoDB、Chroma、FAISS、Qdrant、Milvus、Weaviate。
- Celery、RQ、RabbitMQ 或第二套 scheduler/queue。
- 代理直接读取模型/Provider Key、任意访问 URL、绕过 Broker/Adapter 或静默换源。
- 运行时 `git clone`、`pip install` 或拉取未固定代码。
- 券商连接、真实订单、真实资金和把纸面成交显示为真实成交。

## 静态验收

执行：

```bash
python3 tools/check_tradingagents_architecture.py
```

检查器验证登记完整性、映射文件、基线 SQLite 表、Compose 服务集合、已发布/EXPOSE 端口和禁止依赖。后续提交若增加重复服务、端口或存储/队列依赖，应立即使验收失败。
