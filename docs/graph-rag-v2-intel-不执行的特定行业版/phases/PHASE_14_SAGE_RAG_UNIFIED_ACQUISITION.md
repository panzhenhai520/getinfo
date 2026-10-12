# Phase 14 — SAGE-RAG Unified Acquisition（统一情报采集）

## 目标
把 **AI 助手**在一次提问里能做的**所有采集动作**统一到一套动作契约下，让 Phase 07 的 Gap 驱动多跳
有统一的"可选项清单"可以选，而不是每个能力各写一套调用：

**内部证据库 / 结构化数据 / 公开网络 / 趋势信号 / 向用户澄清**
（动作集与领域无关；**具体接什么源由作用域配置决定**）

- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **定位：** 依赖 Phase 04（检索舰队）、Phase 07（Gap 分析）、Phase 11（skill 注册表与 Router）；
  被 Phase 13（合成前确认证据来源）、Phase 16（Search Trace 展示）依赖。

## 边界
**统一 内部证据库 / 结构化数据 / 公开网络 / 趋势信号 / 向用户澄清。**

允许做：动作契约、动作效用估计、各通道路由、向用户澄清路由、缺口不重复、结论安全与作用域权重。
不允许做：各来源内部实现（→ 采集侧现役代码）、答案合成（→ Phase 13）、记忆（→ Phase 09–12）。

## 动作集（五类，**与领域无关**；引擎层只认这五类）
> 动作类型的划分依据是"证据从**哪一类通道**来"，不是"接哪个具体行业源"。**具体接什么源由配置决定。**

| 动作类型 | 语义（领域无关） | 真实实现入口（当前接的源） |
|---|---|---|
| `EVIDENCE_POOL`<br>内部证据库 | 在**已准入**的内部证据库里做检索（真表名 **`articles`**，见 `sqlite_database.py:439`；不存在 `intel_articles`） | `qa_retrieval.ArticleRetriever`；准入闸门 `intel_topics._classification_admitted` |
| `STRUCTURED_DATA`<br>结构化数据 | 查结构化数据。**内部结构化产物与外部结构化数据源都归入本动作**，接哪一个源**由作用域配置选择** | 内部：`article_intel_classifications`、`intel_article_events`（事件三元组）、`intel_subject_canonical`（实体归一）；外部：`financial_realtime_query`、`tradingagents_cn_data_adapter`（**可用范围待确认**） |
| `PUBLIC_WEB`<br>公开网络 | 公开网络的检索与正文获取：搜索引擎候选、页面渲染、真浏览器兜底 | `intel_candidates`、`intel_reports`、`remote_pipeline_client` |
| `TREND`<br>趋势信号 | 客观趋势与热榜信号：世界在说什么，而不是"我们收下了什么" | `trend_objective.py`（基于 `intel_candidates` 全量标题算独立媒体数）、`buzzing_radar.py`（海外标题雷达） |
| `ASK_USER`<br>向用户澄清 | 向用户澄清（`scope`、`TimeWindow`、实体口径） | `qa_gateway` 的 run 事件 `action_required`（现役已有该事件类型） |

> **检索增强 provider 不是动作类型：** RAGFlow 增强检索（`qa_ragflow_client.py`）是 `EVIDENCE_POOL`
> 的一个 **provider 开关**，不是第六类动作。A 机为"无 RAGFlow 一级检索模式"，该 provider 关闭；
> B 机走网关 `192.168.0.64:8106`。**任何一路都必须能单独缺席，不允许成为硬依赖。**
>
> **两层分离纪律（`00_REWRITE_SPEC.md` 第二节，优先级最高）：** 上表是**通用引擎层**，
> 动作类型必须保持领域无关；任何具体领域维度（实体词表、来源类别与权威级别、时效策略、关键词锚点）
> 一律作为**作用域配置**进入，不得写进动作契约本身。
>
> **与原始动作集的对应：** 原始的五类动作（KB / 内部结构化数据 / DB / Web / 向用户追问）
> 在本项目下中性化为上表五类；其中"内部结构化数据"与"DB"合并为 `STRUCTURED_DATA`
> ——因为二者的差别是**接哪个源**（配置项），不是**证据从哪类通道来**（类型差异）。
>
> **作用域口径：** 下文出现的 `industry_pack_id` 是**中性契约字段 `scope`（作用域/租户键）的当前实现**。
> 契约与算法只依赖 `scope`，不得假设它一定是"行业"；换成客户、部门、项目维度同样成立。

## 开始前
- [ ] 已读 MASTER_RULES
- [ ] 已读 V2 Architecture 相关章节
- [ ] 已读 IMPLEMENTATION_STATUS
- [ ] 前置 Gate 已通过（Phase 00 除外）
- [ ] **现实核查**：结构化产物填充率极低（实测 A 机 7022 篇里事件三元组仅 3 行＝0.0%、实体归一 0 行）。
      `STRUCTURED_DATA` 的**内部**数据源在事件抽取回填（`docs/爬虫改进实施步骤.md` 阶段 7）完成前
      **只能返回空/极少结果**，因此本阶段必须让它在低填充率下**优雅降级并明确说明**，而不是假装查过。

## Must 子任务

### P14-01 Action schema
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Action schema`；不得提前实现后续 Phase。
- **实现方法：** 动作统一契约（与 `qa_contracts.py` 的常量/枚举风格一致）：
  `action_id`、`action_type`（上表五类）、`target`（作用域 / 实体 / 关键词 / 信源 / 用户澄清项）、
  `required_evidence`（这个动作要补哪类缺口）、`expected_gap_reduction`、`cost_estimate`、
  `latency_estimate`、`permission`、`source_kind`（**具体源由配置选择**）、
  `fallback_chain`（如 `PUBLIC_WEB` 失败 → 订阅源 → 真浏览器渲染）。
  - 动作类型枚举必须与上表**完全一致**（五类）；新增动作类型要改本文件与 DECISION_LOG。
  - `action_type` 里**不得出现任何具体领域词**（不许有行业名/业务对象名）。
- **测试方式：** Unit（枚举非法值被拒；`fallback_chain` 校验；`action_type` 的领域无关性检查）。
- **验收标准：** 五类动作全部有契约定义；非法动作被拒；契约可序列化/回放；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P14-02 ExpectedGapReduction utility
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `ExpectedGapReduction utility`；不得提前实现后续 Phase。
- **实现方法：**
  - `expected_gap_reduction = 历史成功率 × 缺口类型匹配权重 × 时效匹配度 ÷ (cost × latency 归一化)`。
  - 输入全部来自已验证的统计：Phase 12 的信源可靠性与 skill 性能记忆、Phase 11 遥测；**不得凭感觉给常数**。
  - 无历史数据时返回 `unknown`（置信度低），并让 Phase 07 的回退策略决定是否仍然尝试。
  - 效用值必须可解释：返回各分量，便于 Phase 16 展示"为什么选了这个动作"。
- **测试方式：** Unit（分量计算、缺历史数据的 unknown 分支）+ Integration（真实统计接入后数值可复现）。
- **验收标准：** 效用值可分解、可复现；无历史数据不冒充高分；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P14-03 EVIDENCE_POOL / STRUCTURED_DATA / PUBLIC_WEB / TREND routes
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成四条数据类路由（内部证据库 / 结构化数据 / 公开网络 / 趋势信号）；不得提前实现后续 Phase。
- **实现方法：**
  - `EVIDENCE_POOL`（内部证据库）：走 `qa_retrieval.ArticleRetriever`，**必须带 `scope` 收口**
    （当前实现 = `industry_pack_id`）；
    不得对 `fallback_attribution` 之类的来源开特例（现役 `tests/test_retrieval_evidence_policy.py` 守着这条）。
  - `STRUCTURED_DATA`（结构化数据）：**接哪个源由作用域配置选择**，本动作只负责按 `source_kind` 分发。
    - 走**内部**数据源时：查内部结构化产物（事件三元组、实体归一等）。
      **低填充率处理**：返回结果里带 `coverage`（该作用域的三元组填充率）；低于阈值时明确回
      "结构化数据不足，建议改用 `EVIDENCE_POOL` 或 `PUBLIC_WEB`"，
      不允许静默返回空集让上层误判为"没有相关情报"。
    - 走**外部**数据源时：按配置声明的维度查询；必须标注 `as_of` 时间与来源许可。
      许可校验的真实口径见 Phase 15 P15-03：字段 `license_profile`、真身
      `config/financial_source_licenses.json`，**仅对配置声明需校验的作用域与来源类型生效**
      （现状 = `financial_markets` 的 RSS 源），未登记/复核过期即跳过、不发请求。
  - `PUBLIC_WEB`（公开网络）：复用现役降级链（普通请求 → 订阅源 → 报告页真浏览器渲染），
    被反爬拦截时按 `antibot_detector` 的止损口径处理，不得硬攻。
  - `TREND`（趋势信号）：只读趋势模块（`trend_objective.py` / `buzzing_radar.py`），
    **不得写回证据池**、不得产生候选、不占爬取槽；返回时必须与"我们收下的文章热度"分列，不混口径。
  - 四条路由都必须在 `fallback_chain` 内声明降级顺序，且降级要落 `qa_events` 的 `degraded` 事件。
- **测试方式：** Unit（四条路由的入参收口、跨作用域拒绝、低填充率分支）+ Integration（每条路由真实调一次）；
  失败路径：跨作用域请求必须被拒；被反爬拦时必须走止损而不是无限重试。
- **验收标准：** 四条路由真实可用；跨作用域 0 命中；低填充率有明确提示；降级可见；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P14-04 ASK_USER route
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `ask-user route`（向用户澄清）；不得提前实现后续 Phase。
- **实现方法：**
  - 触发条件（必须**穷尽列举后可判定**，不许"感觉不确定就问"）：
    1. 问题未指定作用域，且无法从上下文唯一确定；
    2. 时间窗缺失且结论对时间窗敏感（如"最近的变化"未给天数）；
    3. 需要的实体口径无法唯一归一（同一名称可能指多个实体）；
    4. 需要的证据受许可证限制，需用户确认是否可用；
    5. 两个作用域的答案口径冲突，需要用户择一。
  - 走现役 `qa_gateway` 的 `action_required` 事件（`qa_contracts.QA_EVENT_TYPES` 已定义），
    前端在 AI 助手对话里渲染为待澄清项；不新增第二套交互通道。
  - **问题负担控制**：一次最多问 1–3 个关键项、给可选项与默认值；同一个问题在一次会话里不得重复问
    （与 P14-05 的 no-repeat 共用去重记录）。
- **测试方式：** Unit（五类触发条件的正例；非触发场景不得提问）+ Integration（真实 run 出现澄清项并被回答后继续）；
  失败路径：重复追问同一项必须被去重拦截。
- **验收标准：** 五类触发各有测试；非必要不追问；同会话不重复追问；澄清项在页面上真实可见；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P14-05 Context Gap no-repeat
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Context Gap no-repeat`；不得提前实现后续 Phase。
- **实现方法：**
  - Gap 不得默认触发新搜索（MASTER_RULES 13）：先尝试**重组已有上下文**（重排、补选已抓到的证据、
    换检索视角），只有在重组后仍无增益时才发起新动作。
  - 去重：`seen` 集合按"动作类型 + target + 时间窗"指纹记；同一指纹在一次 run 内不得重复执行
    （与 Phase 07 的 `seen dedupe` 共用同一集合，不另建）。
  - 拒绝过的检索式也要进 `seen`（MASTER_RULES 14），避免重复垃圾检索。
  - 每个被跳过的动作都要在 trace 里写"为什么跳过"，便于 Phase 16 复看。
- **测试方式：** Unit（指纹生成与去重；重组优先于新搜索）+ Integration（真实 run 中同一 gap 不重复触发同一动作）；
  失败路径：人为注入重复 gap，必须只执行一次。
- **验收标准：** 重复动作 0 次；重组优先有 trace 证明；跳过理由可见；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P14-06 safety/scope weights
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `safety/scope weights`（结论安全与作用域权重）；不得提前实现后续 Phase。
- **实现方法：**
  - 权重规则（硬性，不是调参项）：
    1. **跨 `scope` 取证据的权重 = 0（禁止）**：任何动作的 target 若落在别的 `scope`，直接剔除候选并记事件；
    2. **无证据结论的权重 = 0**：动作的价值只体现在"提高证据覆盖率"，不得因为"能生成好看的答案"而加分；
    3. **禁止绕过许可证**：受限来源（付费/受限许可证）在未授权作用域内权重 = 0；
    4. **provider 能力差异**：当前部署未启用的检索增强 provider（如 A 机无 RAGFlow）权重 = 0；
    5. 结论安全权重优先于 cost/latency/utility（MASTER_RULES 17）。
  - 权重计算与动作筛选放在同一处，禁止在别处再写一份阈值。
- **测试方式：** Unit（五条规则各自的用例）+ Integration（构造跨作用域候选，验证被剔除且事件可查）；
  失败路径：把跨作用域候选塞进候选集必须导致测试失败。
- **验收标准：** 五条规则全部有测试；跨 `scope` 候选 0 进入执行；事件可审计；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

## Phase 禁止事项
- 不得硬编码 benchmark 答案或删除/skip 测试。
- 不得把 BLOCKED 标为 PASS。
- 跨 Phase contract 变更必须先写 DECISION_LOG，并运行受影响回归。
- 不得跨 `scope` 取证据，也不得用"全库搜"绕过作用域收口。
- 不得在未启用检索增强 provider 的部署（如 A 机）上启用该 provider。
- 不得把任何具体领域词（行业名、业务对象名、来源类别词）写进动作类型或路由算法。
- 不得因为结构化产物填充率低就把"查不到"包装成"没有相关情报"。
- 不得把向用户澄清当作常规步骤来回避检索工作。

## Phase Gate
- [ ] 所有 Must 子任务 PASS
- [ ] Unit tests PASS
- [ ] Integration tests PASS
- [ ] Failure-path tests PASS
- [ ] Compatibility regression PASS（`tests/test_retrieval_evidence_policy.py` 等既有检索策略测试全绿）
- [ ] IMPLEMENTATION_STATUS / ACCEPTANCE_MATRIX 已更新
- [ ] 无未处理高优先级 blocker

## 阶段结束报告
```text
Phase:
Status:
Changed files:
Migrations:
Tests run:
Metrics before/after:
Known limitations:
Blocked items:
Rollback method:
Next phase readiness:
```
