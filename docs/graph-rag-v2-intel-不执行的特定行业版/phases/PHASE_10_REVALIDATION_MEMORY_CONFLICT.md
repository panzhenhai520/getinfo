# Phase 10 — Revalidation & Memory Conflict

## 目标
让 Phase 09 的记忆**不会过期作废地继续被用**：判断时效、发现来源改版、把召回的记忆降级为"线索"并复验、
消解记忆之间的冲突、支持撤销与高风险强制复验。

本阶段回答的问题：**"助手记得的这条结论，今天还算数吗？"**

- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **定位：** 依赖 Phase 09；被 Phase 13、15、17 依赖。
- **现实前提：** 发布时间曾经全表为空（0/6105），现已做到 RSS 与列表页在**采集当刻**写入发布时间
  （`publish_time.py`），并按精度标注 `exact/date/url/discovered`。**精度直接决定本阶段判定的可信度**，
  精度为 `discovered` 的时间不得作为时效判定依据（只能降级为背景）。

## 边界
**时效、冲突、失效和污染撤销。**

允许做：freshness 策略与 TTL、来源改版检测、记忆复验、记忆冲突、取代关系、撤销与高风险钩子。
不允许做：记忆 schema（→ Phase 09）、经验记忆（→ Phase 12）、作用域隔离强制与审计回放（→ Phase 15）。

## 开始前
- [ ] 已读 MASTER_RULES
- [ ] 已读 V2 Architecture 相关章节
- [ ] 已读 IMPLEMENTATION_STATUS
- [ ] 前置 Gate 已通过（Phase 00 除外）
- [ ] 已确认 Phase 09 的 `qa_memory_items.status` 与 `qa_memory_events` 可用
- [ ] 已确认 `publish_time.py` 的精度字段名与取值（若字段名与本文不同，以实现为准并更新本文件）

## Must 子任务

### P10-01 freshness/TTL
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `freshness/TTL`；不得提前实现后续 Phase。
- **实现方法：**
  - freshness 策略按「记忆类型 × 来源类型」配置化（配置表或常量映射，禁止散落硬编码）。
    **档位本身来自作用域配置**（`freshness 策略`是配置层内容），引擎只实现"读取档位 → 判定"的机制：
    | 记忆类型 / 来源类别 | freshness 档 | 语义 |
    |---|---|---|
    | 权威来源（由作用域配置的 `authority_level` 判定） | `VERY_SHORT` | **首发即最高价值**；数日后只作背景，不得作为结论主依据 |
    | 长期制度性来源 | `LONG` | 有效期内持续可用，改版时重新取用 |
    | 用户本次会话的关注点 | `SESSION_BOUND` | 只在本 session 有效，跨 run 需显式提升为 `USER_SCOPE` |
    | 当前 run 的约束（`scope`、`TimeWindow`、实体口径） | `RUN_BOUND` | 不得写进长期记忆，随 run 结束失效 |
    | 高频更新的结构化数据（具体类型由配置声明） | `VERY_SHORT` | 秒级/分钟级失效，必须有 `as_of` 时间戳 |
    > **上表是"机制示例"不是"引擎写死的行业规则"**：具体哪些来源归入哪一档，一律由作用域配置声明；
    > 引擎不得内建任何具体来源类别或行业口径。
  - 生效时间统一用 `published_at_utc` + `publish_precision`；判定函数返回 `{freshness_state, reasons, effective_at, precision}`。
  - 精度为 `discovered` 或缺失时：`freshness_state` 一律降级为 `BACKGROUND_ONLY`，并在返回里显式标注原因。
  - TTL 到期只把状态迁移为 `EXPIRED/BACKGROUND_ONLY`，**不删行**。
- **测试方式：** Unit（各档正例/反例；`discovered` 精度强制降级）+ Integration（真实证据池 `articles` 上按作用域抽样统计各档分布）；
  失败路径：`published_at_utc` 为空时不得返回"新鲜"。
- **验收标准：** 六档策略各有测试；空时间/低精度一律不进"可作为主依据"档；抽样统计有可复核输出；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P10-02 source-version detection
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `source-version detection`；不得提前实现后续 Phase。
- **实现方法：**
  - 为每条来源证据保存 `content_hash`（正文规范化后哈希）与 `version_seq`；同一 URL 正文变化即产生新版本。
  - 依赖该记忆的证据来源改版时：把相关记忆标 `STALE` 并生成一条 `revalidate` 任务；
    旧版本证据**保留**，通过 `SUPERSEDED_BY` 关联（见 P10-05）。
  - 与现役采集侧的正文指纹/去重能力衔接（`intel_content_quality_gate.py`、文章去重路径）；
    若现役已有等价指纹字段，必须复用而不是再造一套（写进 DECISION_LOG）。
- **测试方式：** Unit（同一正文重复抓取不产生新版本；正文改动产生新版本）+ Integration（改版 → 记忆转 STALE → 触发 revalidate）；
  失败路径：抓取失败/正文为空时不得判为"来源改版"。
- **验收标准：** 版本序列可查；改版能正确传导到记忆状态；抓取失败不误判；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P10-03 MEMORY_HINT revalidation
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `MEMORY_HINT revalidation`；不得提前实现后续 Phase。
- **实现方法：**
  - 记忆召回到上下文时一律以 `MEMORY_HINT` 标记（不是 `EVIDENCE`）；`MEMORY_HINT` **不得**直接作为结论依据。
  - 复验流程：`MEMORY_HINT → 重新取证据 → Verifier（Phase 03）判定 → 通过则升级为 Verified Evidence，
    否则降级/丢弃并记录原因`。
  - 高风险类别（**由作用域配置声明的高影响记忆类型**，引擎不内建任何具体类别）**必须**复验，不允许跳过；
    低风险类别允许"带 hint 标记直接呈现"但仍不可进结论段。
  - 复验结果写入 `qa_memory_events`（`event_type='revalidation'`），含 `pass/fail` 与理由；
    通过率进 METRICS 的 `revalidation pass`。
- **测试方式：** Unit（`MEMORY_HINT` 不能出现在结论依据集合里）+ Integration（高风险记忆未复验时合成阶段被阻断）；
  失败路径：复验时来源已不可达（反爬/下线）必须记为 fail 而不是默认通过。
- **验收标准：** 结论段的每条依据都能指回 `qa_evidence`（不存在 hint 冒充证据）；高风险未复验必阻断；
  复验率与通过率可统计；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P10-04 MemoryContradiction
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `MemoryContradiction`；不得提前实现后续 Phase。
- **实现方法：**
  - 同一事实出现两条互斥记忆（例如同一来源的两版互斥表述）→ 建 `MemoryContradiction` 记录，
    结构与现役 `qa_conflicts` 保持同构（便于统一展示与消解），复用其检测/消解入口。
  - 冲突未消解前：两条记忆都**不得**进结论；答案中必须以"反证/未决"形式显式呈现（衔接 Phase 13 的 P13-03）。
  - 消解规则优先级：**权威层级（按作用域配置的 `authority_level` 排序，引擎不内建具体来源类别）**
    → 发布时间新旧 → 无法判定则保留未决。
- **测试方式：** Unit（同构冲突检测、优先级排序）+ Integration（造冲突 → 结论被阻断 → 消解后放行）；
  失败路径：两条同权威同时间的互斥记忆必须保持"未决"而不是随便选一条。
- **验收标准：** 冲突记录可查；未消解冲突 0 次进入结论；未决状态在答案中可见；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P10-05 SUPERSEDED_BY
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `SUPERSEDED_BY`；不得提前实现后续 Phase。
- **实现方法：**
  - 新版本取代旧版本时：旧记忆 `status=SUPERSEDED` + `superseded_by=<新 memory_id>`，
    并在 `qa_memory_links` 写 `relation='SUPERSEDES'`（新→旧），两个方向都可查。
  - 取代链必须能一直向上追到最新 ACTIVE 版本（提供 `resolve_latest(memory_id)`）。
  - 禁止"最新记忆自动无条件覆盖旧记忆"（MASTER_RULES 禁止项）：取代必须显式发生并留事件。
- **测试方式：** Unit（三节点取代链解析）+ Integration（取旧版本时自动解析到最新，并标注"已被取代"）；
  失败路径：环状取代（A→B→A）必须被检测并拒绝写入。
- **验收标准：** 取代链可双向查询；旧版本召回时显式标注已被取代；环状取代被拒；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P10-06 revoke/high-risk hook
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `revoke/high-risk hook`；不得提前实现后续 Phase。
- **实现方法：**
  - 在 `qa_gateway.py` 新增 `POST /api/qa/v1/memory/<memory_id>/revoke`，**理由必填**；
    撤销同时写 `qa_memory_events`（撤销人、理由、时间），行保留可审计（Phase 15 依赖）。
  - 撤销的传导：凡引用了该记忆的 run 与后续召回，都必须立刻不再返回；已产出的历史答案不做篡改，只标注。
  - 高风险钩子：记忆类型属于**作用域配置声明的高影响类别**时，
    写入即打 `requires_revalidation` 标记，且在这些记忆**未复验**时不允许进入结论依据集合。
    （引擎不得内建任何具体类别词；"哪些算高影响"是配置项。）
  - 该钩子与 P10-03 复用同一判定集合，禁止两处各写一份。
- **测试方式：** Unit（理由必填、高风险打标）+ Integration（撤销 → 召回为空 → 审计可查）；
  失败路径：撤销一条不存在的记忆返回明确错误且不产生脏事件。
- **验收标准：** 撤销接口真实可用；撤销后召回 0 命中且审计可回溯；高风险未被静默放行；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

## Phase 禁止事项
- 不得硬编码 benchmark 答案或删除/skip 测试。
- 不得把 BLOCKED 标为 PASS。
- 跨 Phase contract 变更必须先写 DECISION_LOG，并运行受影响回归。
- 不得让"最新记忆自动覆盖旧记忆"。
- 不得把发布时间精度为 `discovered` 的时间当作时效判定依据。
- 不得因复验来源不可达而默认判定通过。

## Phase Gate
- [ ] 所有 Must 子任务 PASS
- [ ] Unit tests PASS
- [ ] Integration tests PASS
- [ ] Failure-path tests PASS
- [ ] Compatibility regression PASS（现役问答链路与记忆接口无回归）
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
