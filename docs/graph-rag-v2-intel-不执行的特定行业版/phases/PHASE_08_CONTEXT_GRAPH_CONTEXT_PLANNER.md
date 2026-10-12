# Phase 08 — Context Graph & Context Planner

## 目标
按任务组装**最小有效 Context**：把该问给模型的证据给够，把不该给的删掉。
上下文不是越多越好——塞太多会稀释注意力、抬成本、还可能把无关事实"传染"进答案。

## 边界
按任务组装最小有效 Context。

> **术语约定（引擎层硬规则）**：本阶段属于**通用引擎层**，正文一律用中性类型
> （`Claim` / `Evidence` / `Scope` / `TimeWindow` / `Gap`），
> **不得出现任何具体行业词**；作用域差异一律由 Phase 01 的 `ScopeConfig` 提供。
> 作用域统一写 `scope`（**当前实现 = `industry_pack_id`**）。

## 开始前
- [ ] 已读 MASTER_RULES
- [ ] 已读 V2 Architecture 相关章节
- [ ] 已读 IMPLEMENTATION_STATUS
- [ ] 前置 Gate 已通过（Phase 00 除外）

---

## 本项目真实基础（现役上下文链路）

| 环节 | 现役实现 |
|---|---|
| 证据 → 上下文文本 | `qa_research.render_research_context(question=..., level1=..., evidence=...)` |
| 草稿生成输入 | `qa_level1.QaLevel1Generator.generate(question=..., plan=..., evidence=..., profile=...)`，内部 `_messages` 控制消息数与字符上限 |
| 检索式规划 | `qa_research.QaResearchQueryPlanner.expand(...)` |
| 证据条数 | `ArticleRetriever.retrieve(..., limit=12)` / `_policy_exact_evidence(..., limit=6)` / `QaRagflowResearchService.retrieve(..., max_evidence=...)` |
| 输出上限 | `QA_LEVEL1_LOCAL_MAX_TOKENS`（默认 2400）——历史坑：900 会截断 JSON，导致每次白跑一次修复重试 |
| 预算 | `qa_resilience.STAGE_BUDGET_SECONDS` |

**已知取舍（保持）**：证据条数按问题类型收敛（事实核验类 3~5 条，见
`docs/爬虫改进实施步骤.md` 阶段 6 第 3 条）；`citation_map` / claim 校验 / 引用完整性一律不动。

---

## 子任务
### P08-01 ContextItem/Graph
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `ContextItem/Graph`；不得提前实现后续 Phase。
  定义"进上下文的最小单元"与它们之间的关系。
- **实现方法：**
  - `ContextItem` 最小字段（建议，**字段名待确认**）：
    `item_id` / `kind`（`claim` / `evidence` / `policy_fact` / `trend_series` /
    `conflict` / `memory_hint`）/ `content` / `token_estimate` / `source_ref`
    （指向 `evidence_ref` 或 `claim_key`）/ `industry_pack_id` / `time_precision` /
    `utility` / `included` / `exclusion_reason`。
  - `ContextGraph`：Item 之间的 `SUPPORTS` / `REFUTES` / `DEPENDS_ON` 关系，
    直接复用 Phase 06 的边（**不要另建一套关系定义**）。
  - **硬约束**：`industry_pack_id` 必须等于本次 run 的包；不等的一律不入选且记日志。
  - 只允许"已验证证据"进入 ContextItem（Phase 03 硬规则 + MASTER_RULES 第 11 条）。
- **测试方式：** 单测：Item 必填字段校验；跨作用域 Item 被拒；未验证证据无法构造 Item；
  `token_estimate` 与实际 token 数偏差在可接受范围（偏差阈值**待确认**）。
- **验收标准：** 任一 run 的上下文可导出为 Item 列表，每条的入选 / 排除都有理由。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P08-02 ContextUtility/token budget
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `ContextUtility/token budget`；不得提前实现后续 Phase。
- **实现方法：**
  - `ContextUtility`（第一版可解释加权，`01_V2_ARCHITECTURE.md` §5）建议维度：
    与当前 claim 的相关性、证据分（P03-04 的 `EvidenceScore`）、权威级、时效
    （**读 `published_precision`**）、独立性、是否反证（反证要**保底配额**，见 P08-04）。
  - token 预算：总量上限来自 `plan.budget.max_tokens`（P05-05）；
    分配优先级：必答 claim 的核心证据 > 反证 > 背景证据。
  - **下限保护**：不得把预算压到低于完成一次合法输出的下限
    （历史坑：`max_tokens` 过小 → JSON 截断 → 触发修复重试 → 白花 34~40 秒）。
  - 与现役 `limit` 参数的关系：`limit=12` / `limit=6` / `max_evidence` 是现役的
    "条数口径"，本子任务要把它们**统一到 token 预算口径**下，但**默认行为必须等价**
    （先等价替换，再调优）。
  - 超预算时的裁剪顺序必须**确定性**（同输入同裁剪结果）。
- **测试方式：** 单测：预算内 / 超预算 / 极小预算三种情况；裁剪顺序确定；
  等价替换阶段输出与现役逐条一致；反证不被裁掉（配额用例）。
- **验收标准：** 同等答案质量下 token 消耗下降；裁剪可解释；默认行为与现役等价。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P08-03 Context Pack Builder
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Context Pack Builder`；不得提前实现后续 Phase。
  按任务类型组装不同的包（事实核验包 / 对比包 / 时序包 / 因果包 / 综述包）。
- **实现方法：**
  - 现役落点：`qa_research.render_research_context`（上下文文本渲染）；
    Builder 的产物应作为它的**上游输入**，而不是替换它。
  - 包结构：`ContextPack = {pack_id, claim_key, items[], reserved[], gap_notes[], budget}`，
    其中 `gap_notes` 来自 P06-03 / P07-01（让模型知道"这里证据不足"，
    而不是让模型自己猜）。
  - 任务类型映射：Fast（`FAST_STAGES`）用小包；Standard / Deep 按 claim 分包装配；
    Deep 允许多轮包（与 Phase 07 的跳对应）。
  - **不得**把完整 Research State 广播进上下文（MASTER_RULES 禁止项）——
    只放该 claim 需要的 Item。
- **测试方式：** 单测：五类任务包结构正确；`gap_notes` 必现；
  同一个 claim 跨轮次不会拿到互相矛盾的包；包大小受预算约束。
- **验收标准：** 每种任务类型的包可复现、可解释；答案质量不下降而包体积可控。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P08-04 counter-evidence reservation
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `counter-evidence reservation`；不得提前实现后续 Phase。
  给反证留**固定配额**，防止裁剪逻辑把反证全裁掉、只留下顺证。
- **实现方法：**
  - 预留规则：每个 claim 的包里至少保留 `N` 条 `REFUTES` 边证据
    （`N` 默认值**待确认**，建议 1~2）与全部 `UNRESOLVED_CONTRADICTION`。
  - 反证来源：P06-02 的 `REFUTES` 边 + P06-04 的冲突节点 + P07-01 的
    `MISSING_COUNTEREVIDENCE`（后者是"缺反证"的显式声明）。
  - 现役基础：`qa_pipeline` 的 `conflict_review` 阶段与 `qa_synthesis` 的
    `conflicts[].evidence_refs`（终答里已能带冲突），本子任务把它前移到**上下文组装**阶段。
  - **结论安全**（对应术语表"结论安全"一行）：不得给出无证据支撑的投资 / 合规结论；
    反证被裁掉的 run 必须在 UI 上可见（Phase 16）。
- **测试方式：** 单测：预算极小时反证仍保留；无反证时 `gap_notes` 显式标注
  `MISSING_COUNTEREVIDENCE`；裁剪顺序不吞掉冲突节点。
- **验收标准：** 任何 run 的上下文里反证不会被挤掉（或有显式"无可用反证"声明）；
  `UNRESOLVED_CONTRADICTION` 100% 进入上下文。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P08-05 Context Gap
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Context Gap`；不得提前实现后续 Phase。
- **实现方法：**
  - `ContextGap` = "组装上下文时发现某条必要 claim 无 Item 可用"，
    它与 P07-01 的 Evidence Gap 是**两层不同的东西**：
    Evidence Gap 说"库里没有"，Context Gap 说"库里有但没进上下文 / 进不来（超预算、跨作用域、精度不足）"。
  - **硬规则：Context Gap 不得默认触发新搜索**；先尝试重组已有上下文
    （换裁剪顺序、换打包粒度、放宽非关键维度）。只有重组后仍缺，才把
    Context Gap 升级为 Evidence Gap 交给 P07-03。
  - 升级必须在 SearchTrace 里留痕（谁触发、为什么重组失败）。
- **测试方式：** 单测：纯 Context Gap（重组后解决）**不产生新检索**；
  重组失败后才升级为 Evidence Gap；升级路径可追踪。
- **验收标准：** 因"打包不当"导致的无效检索次数明显下降；
  每条 Context Gap 的处置（重组 / 升级）都有记录。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P08-06 selection trace
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `selection trace`；不得提前实现后续 Phase。
  让"为什么这几条进了上下文"完全可查——这是 Phase 16 的可解释性 UI 的数据来源。
- **实现方法：**
  - 每条 Item 记录：入选 / 排除、`utility` 各维度分值、裁剪原因
    （超预算 / 低于阈值 / 跨作用域 / 未验证 / 时效不足）、在包中的顺序。
  - 落点：`qa_stage_runs.details_json`（现役）+ P01-02 的 `qa_search_trace`
    （**表名待确认**）；与【证据解析】（`qa_attribution_units` /
    `qa_attribution_links`）**不要混用同一张表**，两者语义不同。
  - 与 Phase 16 的 Search Trace 视图对齐字段命名，避免前端二次映射。
  - 大对象不落库：只落 id + 分值 + 原因，正文按需回查
    （MASTER_RULES 第 18 条：日志不得泄露密钥或不必要的信息）。
- **测试方式：** 单测：每条 Item 都有 trace；排除项也有 trace；
  裁剪原因取值域固定；trace 写入失败不影响主流程。
- **验收标准：** 给定 run 与 claim，能复现"当时为什么选了这几条、为什么排除了那几条"。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

---

## Phase 禁止事项
- 不得硬编码 benchmark 答案或删除/skip 测试。
- 不得把 BLOCKED 标为 PASS。
- 跨 Phase contract 变更必须先写 DECISION_LOG，并运行受影响回归。
- （本项目追加）Context Gap 不得默认触发新搜索；先重组已有上下文。
- （本项目追加）不得把未验证证据、跨作用域证据放进上下文。
- （本项目追加）不得用压缩上下文的方式规避引用校验（`citation_map` / claim 校验不动）。
- （本项目追加）反证不得被预算裁剪静默丢弃。

## Phase Gate
- [ ] 所有 Must 子任务 PASS
- [ ] Unit tests PASS
- [ ] Integration tests PASS
- [ ] Failure-path tests PASS
- [ ] Compatibility regression PASS
- [ ] IMPLEMENTATION_STATUS / ACCEPTANCE_MATRIX 已更新
- [ ] 无未处理高优先级 blocker
- [ ] 等价替换验收通过（裁剪行为在默认配置下与现役 `limit` 口径结果一致）
- [ ] 反证配额与 `UNRESOLVED_CONTRADICTION` 保留有测试证据
- [ ] 每条 ContextItem 的 selection trace 可查

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
