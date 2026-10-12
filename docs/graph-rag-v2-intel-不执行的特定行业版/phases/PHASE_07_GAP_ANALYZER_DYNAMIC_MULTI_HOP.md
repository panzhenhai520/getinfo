# Phase 07 — Gap Analyzer & Dynamic Multi-hop

## 目标
这是整个"助手真正聪明"的核心：每轮检索收拢之后，不问"还要不要再搜一次"，
而问**"为了可靠回答原问题，哪条必要情报事实还缺什么类型的证据？"**
缺什么补什么、补到够了就停、补不动了就诚实说缺。

## 边界
Evidence Gap 驱动下一跳和收敛。

> **术语约定（引擎层硬规则）**：本阶段属于**通用引擎层**，正文一律用中性类型
> （`Claim` / `Evidence` / `Scope` / `TimeWindow` / `Gap` / `Verifier`），
> **不得出现任何具体行业词**；作用域差异（时效策略、权威级别、允许的关系类型）
> 一律由 Phase 01 的 `ScopeConfig` 提供。作用域统一写 `scope`
> （**当前实现 = `industry_pack_id`**）。

## 开始前
- [ ] 已读 MASTER_RULES
- [ ] 已读 V2 Architecture 相关章节
- [ ] 已读 IMPLEMENTATION_STATUS
- [ ] 前置 Gate 已通过（Phase 00 除外）

---

## 本项目真实前置

1. **多跳依赖时间硬约束与结构化产物**：
   - 事件 / 实体层当前为 0.0%（`intel_article_events` 3 行、`intel_subject_canonical` 0 行），
     因此"实体 A → 关系 → 实体 B → 证据"这类跳**现在跳不动**；
     本 Phase 的**多跳调度框架**可以做，但跳的"落点"必须能拿空数据源兜住。
   - 依赖 `docs/爬虫改进实施步骤.md` **阶段 7（抽取回填）** → **阶段 8（建图）**
     → **阶段 9（规则引擎 + 多跳子查询分解）** → **阶段 10（留痕 + 递归重规划）**。
   - 建议动手顺序（该文档结论）：**阶段 4（时间前置）→ 阶段 5（调整思路生效）→
     阶段 7（抽取回填）→ 阶段 8（建图）**；本 Phase 排在它们之后。
2. **`qa_reasoning_traces`（每跳留痕）当前不存在**，是 `docs/爬虫改进实施步骤.md`
   阶段 10 的待实现项，字段建议：
   `hop_index` / `sub_query_id` / `partial_answer` / `used_evidence_refs` /
   `missing_links_json` / `next_queries_json`。**表名与字段待确认。**
3. **预算硬约束**：`qa_resilience.STAGE_BUDGET_SECONDS` 与 P05-05 的 `plan.budget`
   是硬闸；多跳必须受其约束，超限时降级为单跳并**明确告知用户**。

---

## 子任务
### P07-01 Gap taxonomy/priority
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Gap taxonomy/priority`；不得提前实现后续 Phase。
- **实现方法：**
  - Gap 结构（对齐 `01_V2_ARCHITECTURE.md` §12，行业化改写）：
    ```json
    {
      "gap_id": "G12",
      "claim_id": "C4",
      "missing": "independent_first_party_evidence",
      "priority": 0.91,
      "suggested_queries": [],
      "suggested_routes": ["EVIDENCE_STORE", "PUBLIC_NETWORK"],
      "reason": "只有一条二手转述来源"
    }
    ```
  - Gap 类型（中性口径，**取代原架构中他方场景的类型**）：

    | Gap 类型 | 本项目含义 |
    |---|---|
    | `NO_EVIDENCE` | 该 claim 一条证据都没有 |
    | `LOW_RELEVANCE` | 命中的都不够相关（关键词撞词） |
    | `LOW_AUTHORITY` | 只有低权威来源，缺一手 / 官方发布 |
    | `SINGLE_SOURCE` | 只有单一来源，缺独立来源 |
    | `SECONDHAND_ONLY` | 只有二手转述，缺原始文件 |
    | `MISSING_ENTITY_LINK` | 主体未归并，跨文章接不上（**依赖阶段 7/8**） |
    | `MISSING_TIME_LINK` | 时间链断裂 / 时间精度不足（`date` 或 `discovered`） |
    | `STALE_EVIDENCE` | 证据过期（时效类结论不能用旧文支撑） |
    | `CONTRADICTION` | 存在未消解冲突 |
    | `AMBIGUOUS_ENTITY` | 主体指代不清（同名不同主体） |
    | `MISSING_CAUSAL_BRIDGE` | 缺因果链中间环节 |
    | `MISSING_COUNTEREVIDENCE` | 只有顺证，没有反证 |
    | `COVERAGE_MISS` | 外部在热、自家没采到（来自 `buzzing_radar.coverage_audit`） |

  - `priority` 加权维度（可配，默认值需写明依据）：是否阻塞最终结论、
    是否属时效类、来源缺口严重程度、修复成本（哪一路能补）。
  - **Gap 来源**：P06-03 的 `claim coverage` 结果、P03-03 各 verifier 的拒绝原因分布、
    P03-04 的 `ContradictionRisk`、`buzzing_radar.coverage_audit` 的覆盖差。
- **测试方式：** 单测：每类 Gap 至少一个构造用例；priority 排序可复现；
  同一 claim 产生多个 Gap 时去重合并。
- **验收标准：** 真实 run 上能产出 Gap 列表，每条都有类型、优先级、理由、建议路数；
  Gap 类型分布可 SQL 汇总。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P07-02 suggested route/evidence requirement
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `suggested route/evidence requirement`；不得提前实现后续 Phase。
  把"缺什么"翻译成"去哪一路、用什么检索式、要拿到什么样的证据才算补上"。
- **实现方法：**
  - 路由候选即 Phase 04 的**数据源类别**（领域无关）：`EVIDENCE_STORE`（P04-01）/
    `SEMANTIC_INDEX`（P04-02）/ `EXTERNAL_AUGMENT`（P04-03，A 机当前未绑定可用源）/
    `GRAPH_TRAVERSAL`（P04-03，等前置）/ `STRUCTURED_STORE`（P04-04）/
    `PUBLIC_NETWORK`（P04-04 子路 4b）/ `TREND_SIGNAL`（P04-04 子路 4c）；
    **各类当前绑定哪个源由 `ScopeConfig` 决定，引擎不写死**；
    检索式由 `qa_research.QaResearchQueryPlanner.expand` 生成。
  - `evidence_requirement` 必须写明**验收条件**（拿到什么算补上了），例如：
    - `SECONDHAND_ONLY` → 需要 `article_ragflow_documents.doc_type='official_policy'`
      对应的一手原始文件，或 `PUBLIC_NETWORK` 类命中该权威源的原始文件。
    - `STALE_EVIDENCE` → 需要 `published_at` 落在 `plan.time_window` 内且
      `published_precision ∈ {exact, date}`。
    - `MISSING_COUNTEREVIDENCE` → 需要至少一条 `REFUTES` 边。
  - **路由选择要能解释**：为什么不先去成本最低的那一类
    （默认顺序：证据库 → 结构化 → 公开网络 → 外部增强；**该顺序属策略，应可配置**）。
  - 不允许建议"跨作用域"取证据（`scope` 恒定，跨作用域即越权）。
- **测试方式：** 单测：每类 Gap 都能产出非空路由与非空检索式；
  `evidence_requirement` 可被下游判真伪；某类在当前作用域未绑定可用源时不会被建议为
  可执行路由（或建议后被 P04-06 标为不可用）；替换配置绑定后路由随之变化而引擎代码不变。
- **验收标准：** 每个 Gap 的"补法"是具体可执行的检索动作，而不是"再搜搜看"。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P07-03 Next-hop Planner
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Next-hop Planner`；不得提前实现后续 Phase。
  下一跳不是 `hop = hop + 1`，而是 **Gap → 最佳检索动作**。
- **实现方法：**
  - 完整循环（`01_V2_ARCHITECTURE.md` §13，原样保留结构）：
    ```
    PLAN → FAN OUT → VERIFY → MERGE → GAP ANALYSIS
      ├─ gaps → GENERATE NEXT HOPS → FAN OUT（回到 VERIFY）
      └─ sufficient → ANSWER
    ```
  - 现役接入点：多跳的每一跳最终仍落到 `qa_orchestrator` 的 stage 执行上
    （`level2_retrieval` / `level2_research` / `conflict_review` 可被复用为"跳"的载体，
    或新增 `hop_*` 阶段；**取哪种待确认**，若新增阶段必须同步 `qa_contracts.QA_STAGES`
    与 `qa_storage` 的阶段可见链）。
  - 递归重规划：依据 `qa_reasoning_traces` 重新规划（阶段 10 待实现），
    **必须受熔断与预算约束**，且 trace 写库失败不得影响主流程。
  - 每跳必须产出：该跳的 part 结论 + 用了哪些证据 + 缺什么链接 + 下一批检索式。
  - 图跳（`MISSING_ENTITY_LINK` / `MISSING_CAUSAL_BRIDGE`）在阶段 7/8 达标前
    **必须识别为"当前无法补"并停止该跳**，不得空转多轮。
- **测试方式：** 单测：单 Gap → 单跳；多 Gap → 多跳且受预算约束；
  图跳在空数据源下立即停止并给出 stop reason；
  超预算降级为单跳并输出"降级说明"（用户可见）。
- **验收标准：** 多跳问题每跳都有证据与留痕；缺失链接能触发下一轮补检；
  预算耗尽时停止并给出已完成部分。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P07-04 seen dedupe
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `seen dedupe`；不得提前实现后续 Phase。
  多跳最容易出的问题不是"搜不到"，而是"同一批垃圾搜了五遍"。
- **实现方法：**
  - seen 集合跨跳累积（不是每跳重置）；被拒候选同样进 seen
    （MASTER_RULES 第 14 条）。
  - 复用 P02-03 的机制：`qa_retrieval_cache`（`namespace` / `industry_pack_id` /
    `kb_version` / `scope_hash`）+ P01-02 的 `qa_search_trace.rejected_json`。
  - 同一 round 内**同一 query 默认禁止重复执行**（`01_V2_ARCHITECTURE.md` §5 第 5 条）；
    同一证据**默认禁止重复验证**（第 6 条，靠 P03-04 的缓存）。
  - 指纹口径统一：`canonical_http_url`（URL 归一）、`content_hash`（内容）、
    `event_hash`（事件，**当前表为空，待前置**）。
  - 去重要记指标：`duplicate_search_rate`（重复检索率）、`rejected_repeat_rate`
    （被拒候选重复出现率），供 Phase 17 消融对比。
- **测试方式：** 单测：同一 query 二次执行不重复检索；被拒候选不再出现；
  跨作用域缓存不串；三跳之后 seen 集合大小与唯一候选数吻合。
- **验收标准：** 多跳场景下重复检索率可量化并显著下降；
  任意两个 round 的候选集合差异可解释。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P07-05 no-gain convergence
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `no-gain convergence`；不得提前实现后续 Phase。
- **实现方法：**
  - 收敛判据（第一版，`01_V2_ARCHITECTURE.md` §14）：
    连续两轮满足 `new_verified_claims == 0` **且** `resolved_high_priority_gaps == 0`
    → `STOP_NO_GAIN`。
  - 量纲定义（必须写死，否则无法判定）：
    - `new_verified_claims`：本轮新入图且 `verification_status` 达标的 claim 数。
    - `resolved_high_priority_gaps`：本轮被解决且 `priority ≥ 阈值` 的 Gap 数
      （阈值**待确认**，建议与 P07-01 的优先级分档对齐）。
  - 与预算收敛的关系：预算是硬停（`STOP_BUDGET_EXHAUSTED`），
    no-gain 是软停（`STOP_NO_GAIN`），两者都要记录 stop reason，
    且要在终答里区分"够了"与"停了但不够"。
  - 收敛参数（两轮、优先级阈值、最大跳数）必须可配，默认值需在 tracking 里写明依据。
- **测试方式：** 单测：构造"连续两轮零增益 → 停"；"一轮零增益一轮有增益 → 不停"；
  "预算先耗尽 → 硬停且 reason 正确"；收敛参数切换后行为可复现。
- **验收标准：** 多跳不再无限循环；停止原因与实际状态一致；
  benchmark 上多跳问题的平均跳数下降而答案质量不降。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P07-06 stop reasons
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `stop reasons`；不得提前实现后续 Phase。
- **实现方法：**
  - 停止原因取值（`01_V2_ARCHITECTURE.md` §14，保留原集合并行业化）：
    `ANSWERABLE`（够答了）/ `BUDGET_EXHAUSTED`（预算耗尽）/
    `MAX_DEPTH`（达到最大跳数）/ `NO_GAIN`（无增益收敛）/
    `UNRESOLVABLE_CONTRADICTION`（冲突无法消解）。
  - **`STOP` 不代表一定输出确定答案**：允许"当前证据不足"，
    且必须给出**缺什么**（引用 P07-01 的 Gap 列表），而不是一句"信息不足"。
  - 落点：`qa_runs.degradation_json` / 新增 `stop_reason` 字段（**待确认**）+ SearchTrace；
    用户可见层要能看到"为什么停在这里"。
  - 与 Phase 13 的衔接：`UNRESOLVABLE_CONTRADICTION` 与"证据不足"必须被
    Answer Composer 显式呈现（不得被 LLM 自行润色成肯定句）。
- **测试方式：** 单测：五种 stop reason 各有用例；终答里 stop reason 与 Gap 列表必现；
  "证据不足"输出不得包含无证据支撑的结论句。
- **验收标准：** 每次 run 都有唯一 stop reason 且可解释；用户看到的不确定性有依据。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

---

## Phase 禁止事项
- 不得硬编码 benchmark 答案或删除/skip 测试。
- 不得把 BLOCKED 标为 PASS。
- 跨 Phase contract 变更必须先写 DECISION_LOG，并运行受影响回归。
- （本项目追加）禁止用固定 `hop_count` 代替 Gap 驱动循环。
- （本项目追加）多跳必须按 `depends_on` 顺序执行：上一跳结果作为下一跳的
  实体 / 过滤条件，LLM 只做兜底（对应 `docs/爬虫改进实施步骤.md` 阶段 9 第 4 条）。
- （本项目追加）不得跨作用域补证据（多跳最容易在这里越权）。
- （本项目追加）多跳必须受 `qa_resilience` 预算约束；超限降级为单跳并明确告知用户。

## Phase Gate
- [ ] 所有 Must 子任务 PASS
- [ ] Unit tests PASS
- [ ] Integration tests PASS
- [ ] Failure-path tests PASS
- [ ] Compatibility regression PASS
- [ ] IMPLEMENTATION_STATUS / ACCEPTANCE_MATRIX 已更新
- [ ] 无未处理高优先级 blocker
- [ ] 多跳 run 的每一跳都有 trace 且能在前端展开
- [ ] `duplicate_search_rate` / 平均跳数 / 收敛原因分布有前后对比数字
- [ ] 图类 Gap 在阶段 7/8 未达标时的"立即停止"行为有测试证据

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
