# Phase 09 — Memory Graph Core

## 目标
建立四图中的 **Memory Graph**：让本项目的 **AI 助手**（行业情报采集与分析系统中的对话入口）
在**跨 run / 跨会话**范围内记住稳定且已被验证过的东西，并把记忆变成"研究加速器"而不是"结论来源"。

本阶段只做"记忆能不能安全地存、能不能安全地取"这两件事；时效与冲突消解归 Phase 10，
经验型记忆（成功/失败策略）归 Phase 12。

- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **定位：** 依赖 Phase 01（四图共享类型契约与可回放 session）、Phase 02（证据层）；被 Phase 10、12、13、15、16 依赖。
- **本阶段结束时要能回答：** 一个用户在作用域 A（`scope`，当前实现 = 某个 `industry_pack_id`）里的追问，助手凭什么"记得"上次关注的是哪个实体/主题、
  以及为什么这条记忆**不可能**被另一个作用域的用户读到。

## 边界
**跨 run / 跨会话长期记忆及 Write Gate。**

允许做：
- 记忆对象的 schema / 版本 / 关系；（写入闸门的判定逻辑；召回接口；生命周期；三存储（向量/图/关系）的一致性）

不允许在本阶段做：
- 时效判定、TTL 到期降级、来源改版检测（→ Phase 10）
- 记忆冲突消解与 `SUPERSEDED_BY` 语义（→ Phase 10）
- 成功/失败经验记忆（→ Phase 12）
- 跨用户/跨 `scope` 的隔离强制与审计回放（→ Phase 15，本阶段只留出字段与接口位）

## 开始前
- [ ] 已读 MASTER_RULES
- [ ] 已读 V2 Architecture 相关章节
- [ ] 已读 IMPLEMENTATION_STATUS
- [ ] 前置 Gate 已通过（Phase 00 除外）
- [ ] 已确认 Phase 01 的 `session/trace id`、`schema version`、`version context` 三类字段可用

> **作用域口径（`00_REWRITE_SPEC.md` 第二节）：** 契约层统一使用中性字段 **`scope`（作用域/租户键）**，
> **当前实现 = `industry_pack_id`**；算法只依赖 `scope`，不得假设它一定是"行业"。
> 下文出现的 `industry_pack_id` 一律按此理解。

## Must 子任务

### P09-01 memory schema/version/relations
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `memory schema/version/relations`；不得提前实现后续 Phase。
- **实现方法：**
  - 在 `qa_schema.py` 中与现役问答表同址新增三张表（沿用现有 `CREATE TABLE IF NOT EXISTS` + 索引清单风格）：
    - `qa_memory_items`：`memory_id`、`owner_user_id`、`industry_pack_id`、`session_id`、
      `scope`、`memory_type`、`payload_json`、`source_run_id`、`source_evidence_ref`、
      `schema_version`、`status`、`confidence`、`created_at`、`updated_at`、`expires_at`、`superseded_by`
    - `qa_memory_links`：`from_memory_id`、`to_memory_id`、`relation`、`weight`、`created_at`；
      `relation ∈ {SUPPORTS, REFUTES, SUPERSEDES, RELATED}`
    - `qa_memory_events`：记忆生命周期事件（写入/召回/降级/撤销），供 Phase 15 审计回放
  - `scope ∈ {USER_SESSION, USER_SCOPE, SCOPE_GLOBAL}`：`USER_SESSION` 只能被同一 `session_id` 召回；
    `USER_SCOPE` 在同一 `owner_user_id` + 同一 `scope` 内可跨 run；`SCOPE_GLOBAL` 是该作用域内的公认事实。
    （命名一律用 `SCOPE`，不用任何具体组织维度词；`SCOPE` 的当前实现 = `industry_pack_id`。）
  - 索引按现役惯例建：`(owner_user_id, industry_pack_id, status)`、`(industry_pack_id, memory_type)`、
    `(source_run_id)`；登记进 `qa_schema.py` 的表清单（现役清单里已有 `qa_attribution_units` 等同款写法）。
    索引里出现 `industry_pack_id` 是"作用域键的当前实现"，不是把行业写进算法。
  - `schema_version` 必须可迁移可回滚：写 migration 与反向 migration，禁止只加正向 DDL。
- **测试方式：** Unit（DDL 幂等、索引存在、relation 枚举拒绝非法值）+ Integration（写入→按三键召回→回滚 migration）；
  构造失败路径：`scope=USER_SCOPE` 但 `owner_user_id` 为空时必须拒绝写入。
- **验收标准：** 三张表真实创建；`qa_schema.py` 的初始化清单包含三者；migration 正向/反向跑通且回滚后无残留表；
  非法 `scope`/`relation` 被拒绝并留下可读错误；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P09-02 memory types
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `memory types`；不得提前实现后续 Phase。
- **实现方法：** 定四类记忆，每类写清"什么能进、进门槛是什么"：
  1. **用户关注点**（`USER_PREFERENCE`）：用户长期关注的作用域、关注对象（实体标识与别名）、时间窗口径、输出口径偏好。
     门槛：只能由用户显式表达或连续多次 run 稳定出现，不接受助手推测。
  2. **作用域事实**（`SCOPE_FACT`）：**权威来源**里的事实。
     "权威"由**作用域配置**的 `authority_level` 定义（引擎不认任何具体来源类型）；
     门槛：必须绑定 `source_evidence_ref` 且该证据在 Phase 02 证据层 `validation` 通过；
     必须带 `published_at_utc` 与发布时间精度（`exact/date/url/discovered`，见 `publish_time.py`）。
  3. **实体归一**（`ENTITY`）：实体（机构/产品/人物/组织）的别名与归属（用于把指向同一实体的两个名称合并成同一节点）。
     门槛：别名必须能在证据里找到出处，不许凭常识补。
  4. **研究策略偏好**（`STRATEGY_PREFERENCE`）：某类问题该用哪种检索式、哪几个信源、是否需要 RAGFlow 增强。
     门槛：必须有成功 run 的 trace 支撑（Phase 12 复用同一记录）。
  - 类型常量集中定义在一个模块（建议 `qa_memory.py` 或复用 `qa_contracts.py` 的常量风格），
    Router 与 UI 都不许硬编码字符串。
- **测试方式：** Unit（四类各自的准入门槛，逐条正例/反例）+ Integration（每种类型走一遍写入→召回）；
  失败路径：`SCOPE_FACT` 缺 `published_at_utc`、`ENTITY` 别名无出处，都必须被拒。
- **验收标准：** 四类记忆都能真实写入并被召回；四类各自至少有一条反例测试证明门槛生效；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P09-03 Write Gate
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Write Gate`；不得提前实现后续 Phase。
- **实现方法：**
  - 新增写入门禁 `evaluate(candidate) -> {decision, reasons[]}`，`decision ∈ {ALLOW, REJECT, DEFER}`。
  - 必须逐条校验（全部为硬性条件，任一不过即 `REJECT`）：
    1. `source_run_id` 存在，且该 run 处于成功终态（读 `qa_runs`）；
    2. 记忆内容能回溯到 `qa_evidence` 中的一条已验证证据；
    3. `industry_pack_id`（= `scope` 的当前实现）非空，且与来源 run 的 `industry_pack_id` **完全一致**
       （跨 scope 即拒，作用域隔离的第一道闸）；
    4. `owner_user_id` 非空；
    5. LLM 自由生成内容不得直接成为记忆（对应 MASTER_RULES 11）：`payload_json` 必须携带 `derived_from`
       指回证据，否则 `DEFER` 到人工/后续 revalidate。
  - 每次判定写一条 `qa_memory_events`（`event_type='write_gate'`，含 `decision` 与 `reasons`），
    拒绝理由必须人类可读，不许只写 `invalid`。
  - 门禁是**唯一**写入口：任何绕过门禁直接 INSERT `qa_memory_items` 的路径都要在 code review 中判为缺陷，
    并补一条集成测试守住（参考现役 `tests/test_ingest_choke_point.py` 对唯一收口点的守门写法）。
- **测试方式：** Unit（五条硬性条件 × 正例/反例）+ Integration（真实 run → gate → 入库）；
  失败路径至少覆盖：run 未成功、证据未验证、跨 `scope`、缺 owner、LLM 直出无出处。
- **验收标准：** 五类拒绝路径各有测试且全部被拒；所有 ALLOW 记录都能在 `qa_memory_events` 里找到判定理由；
  没有任何测试外的写入路径；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P09-04 Recall API
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Recall API`；不得提前实现后续 Phase。
- **实现方法：**
  - 在 `qa_gateway.py`（现役问答 API 蓝图）新增 `POST /api/qa/v1/memory/recall`，
    入参：`owner_user_id`、`industry_pack_id`、`session_id`、`query`、`memory_types[]`、`limit`。
  - **三键收口**：`owner_user_id` / `industry_pack_id` / `session_id` 必须齐全；
    缺 `industry_pack_id` 直接 400（fail-closed，不允许"全包搜"降级）。
  - 返回结构每条记忆必须带：`memory_id`、`scope`、`memory_type`、`status`、`confidence`、
    `provenance`（`source_run_id` + `source_evidence_ref` + 来源 article_id）、`match_reason`。
  - 召回结果一律以 **MEMORY_HINT** 语义返回（不是证据）：调用方不得把它当 Verified Evidence 使用
    （该约束在 Phase 10 的 revalidation 里落地）。
  - 复用现役响应包装与错误码风格（与 `qa_gateway.py` 其它接口一致）。
- **测试方式：** Unit（三键缺失各自 400；`scope` 过滤正确）+ Integration（跨 run 召回同一用户同一 `scope` 的记忆；
  跨用户、跨 `scope` 召回必须 0 行）+ 权限失败路径（拿作用域 A 的记忆 id 在作用域 B 的上下文里请求）。
- **验收标准：** 接口真实可用并被 `curl`/测试脚本调用成功；缺任一键返回 400 且错误可读；
  跨用户/跨 `scope` 召回返回空列表（不是"隐藏"而是"查不到"）；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P09-05 lifecycle
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `lifecycle`；不得提前实现后续 Phase。
- **实现方法：**
  - 状态机：`ACTIVE → STALE → (ACTIVE | SUPERSEDED)`、`ACTIVE/STALE → REVOKED`、`ACTIVE → EXPIRED`。
  - 状态只能由显式迁移函数改变，禁止直接 UPDATE `status`；每次迁移写 `qa_memory_events`。
  - 迁移规则（本阶段只实现框架与 `ACTIVE↔REVOKED`、`EXPIRED`；`STALE` 的判定条件由 Phase 10 提供）。 
  - 记忆**不物理删除**：`REVOKED` 保留行 + 事件，保证审计可回溯（Phase 15 依赖）。
- **测试方式：** Unit（状态机合法/非法迁移矩阵）+ Integration（撤销后召回不再返回该条，但表内仍可查）；
  失败路径：非法迁移（`REVOKED → ACTIVE`）必须抛错而不是静默通过。
- **验收标准：** 状态机矩阵全部有测试；撤销后召回为 0、审计仍可查；无 DELETE 语句存在于记忆表路径；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P09-06 provenance/vector+graph+relational
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `provenance/vector+graph+relational`；不得提前实现后续 Phase。
- **实现方法：**
  - 同一批记忆要能同时被三种方式读到，且三边给出一致的 `memory_id` 与一致 `provenance`：
    - **向量侧**：复用现役检索侧的向量能力（`qa_retrieval.ArticleRetriever` / `qa_ragflow_client` 的向量路径），
      只索引**已验证**记忆的正文；
    - **图侧**：`qa_memory_links` 的邻接遍历（找支持/反驳/取代关系）；
    - **关系侧**：SQL 按 `owner_user_id + industry_pack_id + memory_type` 直接查。
  - 三边不一致即判为缺陷：必须提供一致性检查函数（对同一筛选条件三边取 id 集合做差集）。
  - `provenance` 字段统一为：`{source_run_id, source_evidence_ref, article_id, extracted_at, schema_version}`。
- **测试方式：** Unit（provenance 组装、差集函数）+ Integration（同条件三边取回同一集合）；
  失败路径：故意让向量索引滞后一条，差集函数必须报出差异（不许静默）。
- **验收标准：** 三边召回同一集合；一致性检查函数在人为注入偏差时能报错；`provenance` 完整率达 100%；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

## Phase 禁止事项
- 不得硬编码 benchmark 答案或删除/skip 测试。
- 不得把 BLOCKED 标为 PASS。
- 跨 Phase contract 变更必须先写 DECISION_LOG，并运行受影响回归。
- 不得让 LLM 自由生成内容直接成为 Verified Memory（必须过 Write Gate 且带回溯出处）。
- 不得以"找不到"为理由放行跨 `scope`/跨用户的记忆读写；跨 `scope` 必须是**查不到**，不是"过滤掉"。
- 不得物理删除记忆行来"清理数据"（破坏 Phase 15 的审计回放）。

## Phase Gate
- [ ] 所有 Must 子任务 PASS
- [ ] Unit tests PASS
- [ ] Integration tests PASS
- [ ] Failure-path tests PASS
- [ ] Compatibility regression PASS（现役 `/api/qa/v1/*` 接口无回归，`/api/chat/send` 行为不变）
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
