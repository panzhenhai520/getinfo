# Phase 11 — Skill Registry & Router

## 目标
让 **AI 助手**按问题类型**只加载最小必要能力**：不问趋势就不要跑趋势 skill，
作用域 A 的问题就不要去碰作用域 B 的数据，A 机没有 RAGFlow 就不要路由到 RAGFlow 增强检索。

- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **定位：** 依赖 Phase 01（契约）、Phase 04（检索舰队）；被 Phase 12（skill 性能记忆）、
  Phase 13（合成时确认调用了哪些能力）、Phase 14（动作路由）、Phase 16（dashboard）依赖。
- **本项目的 Skill 是什么：** 一个**可被 Router 按需加载的能力单元**，不是"一段提示词"。例如：
  证据池检索、公网检索、客观趋势查询、海外标题雷达、RAGFlow 增强检索、结构化数据查询、向用户澄清。
  现役 `qa_task_templates.py` 与 `qa_question_templates.py` 是这套东西的**雏形**，
  本阶段要在其基础上把它变成有契约、有版本、有权限声明的注册表（不推倒重写）。

## 边界
**按任务加载最小必要能力。**

允许做：skill schema/version、Registry、Router、权限/成本/延迟约束、按需注入指令、性能遥测。
不允许做：具体 skill 的内部实现（归各自 Phase）、记忆写入（→ Phase 09）、答案合成（→ Phase 13）。

## 开始前
- [ ] 已读 MASTER_RULES
- [ ] 已读 V2 Architecture 相关章节
- [ ] 已读 IMPLEMENTATION_STATUS
- [ ] 前置 Gate 已通过（Phase 00 除外）
- [ ] 已探查 `qa_task_templates.py` / `qa_question_templates.py` 现有结构，确认可复用面（写进 DECISION_LOG）

## Must 子任务

### P11-01 Skill schema/version
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Skill schema/version`；不得提前实现后续 Phase。
- **实现方法：** 每个 skill 声明以下字段（建议放 `qa_skill_registry.py`，并与 `qa_contracts.py` 的常量风格一致）：
  | 字段 | 说明 |
  |---|---|
  | `skill_id` / `version` | 稳定 id + 语义版本；版本变化必须能被追踪 |
  | `input_schema` / `output_schema` | 机器可校验的入参与出参契约 |
  | `capabilities` | 能力声明（能查什么：内部证据库/结构化数据/公开网络/趋势信号/向用户澄清/外部检索增强） |
  | `scopes` | 允许服务的作用域集合（**当前实现 = `industry_pack_id` 列表**）；**空表示不允许被任何作用域调用**（fail-closed）。字段名用中性的 `scopes`，不得写成具体组织维度 |
  | `permission` | 是否需要外网、是否需要配额、是否需要 VPN 渲染、是否消耗爬取槽 |
  | `cost_estimate` / `latency_estimate` | 用于 Router 的预算裁剪 |
  | `requires_revalidation` | 其产出是否高风险（衔接 Phase 10 P10-06；"高风险"由作用域配置声明） |
  - 契约必须可校验（拒绝未知字段/非法枚举），不许用裸 dict 传递。
- **测试方式：** Unit（schema 校验的正例/反例；`scopes` 为空的 skill 不可被选中）。
- **验收标准：** schema 可校验；非法声明在加载期即报错，不是运行期才炸；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P11-02 Registry
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Registry`；不得提前实现后续 Phase。
- **实现方法：**
  - 注册表：一个可版本化的注册清单（文件或 DB 表，二选一并在 DECISION_LOG 说明）+ 运行时加载器。
  - 硬规则：`skill_id` 重复必须报错；未注册的 skill **不得**被 Router 选中；
    同 `skill_id` 多版本可共存，默认选最高兼容版本，回滚可指定版本。
  - 提供 `list_skills(industry_pack_id, capabilities)` 供 Router 候选筛选。
- **测试方式：** Unit（重复 id 报错、版本选择、按 `scope` 筛选）+ Integration（加载真实注册表并枚举）。
- **验收标准：** 未注册 skill 零调用（有测试证明）；重复 id 报错；版本可回滚；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P11-03 Router
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Router`；不得提前实现后续 Phase。
- **实现方法：**
  - 输入：问题意图（由 Phase 05 的 Query Interpreter 给出）+ `scope` + 预算 + 已知缺口；
    输出：**最小必要 skill 集合**（不是全集）+ 每个被选中的理由 + 被排除的理由。
  - 决策必须落 trace（进 `qa_events`），理由人类可读；禁止"因为全量加载所以都跑一遍"。
  - 排除规则必须显式：包不匹配、能力不需要、预算不够、缺少权限（如 A 机无 RAGFlow）。
  - 与 Phase 14 的动作路由分工：本阶段选"能力"，Phase 14 决定"这一跳具体调哪个动作/哪个信源"。
- **测试方式：** Unit（一段问题 → 期望 skill 集合；不该选的必须不在集合里）+ Integration（真实 run 的 trace 里能看到选择与排除理由）；
  失败路径：无可用 skill 时必须给出明确"无法回答/需澄清"，不得静默全跑。
- **验收标准：** 候选集始终是必要集（有反例测试）；trace 里可读到选择与排除理由；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P11-04 permission/cost/latency
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `permission/cost/latency`；不得提前实现后续 Phase。
- **实现方法：**
  - **必须复用** `qa_gateway.py` 现役的配额/降级能力，Router 不得绕过配额直接调用外部服务。
  - **A 机口径**：A 机是"无 RAGFlow 一级检索模式"，Router 必须能感知并**不路由**到 RAGFlow 相关 skill；
    B 机走 RAGFlow 网关（`192.168.0.64:8106`）。
  - 成本与延迟预算：为每类问题设预算上限；超预算时按 `cost_estimate`/`latency_estimate` 裁剪候选，
    并在 trace 中说明裁掉了什么、为什么。
  - 权限缺失时降级而不是失败（例如公网不可用则只用证据池），降级必须落 `qa_events` 的 `degraded` 事件
    （现役 `qa_contracts.QA_EVENT_TYPES` 已有该事件类型）。
- **测试方式：** Unit（A 机不得选 RAGFlow skill；超预算裁剪顺序正确）+ Integration（模拟配额用尽 → 降级可见）；
  失败路径：权限缺失时不得静默返回空结果冒充"没有相关情报"。
- **验收标准：** 配额被真实复用（有测试或调用链证据）；A/B 机路由差异有测试；降级在 trace 可见；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P11-05 on-demand instruction
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `on-demand instruction`；不得提前实现后续 Phase。
- **实现方法：**
  - skill 的指令片段按需注入：只有被 Router 选中的 skill 才把它的指令拼进提示词；
    未被选中的指令一律不进上下文（避免上下文被撑爆、避免能力误用）。
  - 指令片段随 skill `version` 一起版本化；提示词里必须能追到"这段指令来自哪个 skill 的哪个版本"。
  - 注入顺序与去重规则确定化（可回放），并把最终拼接结果的指纹写入 trace。
- **测试方式：** Unit（未选中 skill 的指令不出现在最终提示词）+ Integration（指纹可复现：同输入同指纹）。
- **验收标准：** 提示词中不含未选中 skill 的指令；指令来源可追溯；拼接可回放；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P11-06 performance telemetry
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `performance telemetry`；不得提前实现后续 Phase。
- **实现方法：**
  - 每次 skill 调用写一条记录：`skill_id`、`version`、`run_id`、`stage`、`latency_ms`、
    `tokens`、`cost`、`cache_hit`、`outcome(success/degraded/error)`、`error_code`。
  - 汇总口径对齐 `reference/METRICS.md` 的 Agent/System 两组（rounds/queries/cache hit；latency/tokens/cost/quality）。
  - 数据源进 `qa_events` / 现役 metrics 通道，供 `/api/qa/v1/metrics`（Phase 16 复用）读取；
    不得另建一套互不相通的统计。
- **测试方式：** Unit（字段完整性）+ Integration（跑一个真实 run 后统计能对上）；
  失败路径：skill 抛异常时也必须留下 telemetry（不许丢记录）。
- **验收标准：** 一次 run 的 skill 调用全部有记录；异常路径也有记录；`/api/qa/v1/metrics` 能取到；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

## Phase 禁止事项
- 不得硬编码 benchmark 答案或删除/skip 测试。
- 不得把 BLOCKED 标为 PASS。
- 跨 Phase contract 变更必须先写 DECISION_LOG，并运行受影响回归。
- 不得绕过 `qa_gateway` 的配额/降级直接调用外部服务。
- 不得在 A 机路由到 RAGFlow 相关 skill。
- 不得把"权限缺失/无可用 skill"静默表现为"没有相关情报"。

## Phase Gate
- [ ] 所有 Must 子任务 PASS
- [ ] Unit tests PASS
- [ ] Integration tests PASS
- [ ] Failure-path tests PASS
- [ ] Compatibility regression PASS（现役 `/api/chat/send` 与 `/api/qa/v1/*` 行为不变）
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
