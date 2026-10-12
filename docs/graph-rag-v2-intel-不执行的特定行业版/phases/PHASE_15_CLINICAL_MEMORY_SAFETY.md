# Phase 15 — 记忆安全与作用域隔离（Memory Safety & Scope Isolation）

> **改名说明：** 本阶段原名为他方项目的阶段名 "Memory Safety"（文件名 `PHASE_15_CLINICAL_MEMORY_SAFETY.md`
> 保留不动，以保持与原目录的文件树一致）。内容**整体改写**为：
> **记忆不得跨 `scope`、跨用户泄露；来源合规；可撤销与可审计。**
> 契约统一使用中性字段 **`scope`（作用域/租户键；当前实现 = `industry_pack_id`）**——
> 规则里**不出现任何具体组织/行业维度**，换维只改配置、不改算法。
> 原阶段语义按 `00_REWRITE_SPEC.md` 的对照表中性化为「**记忆作用域：`user_id` + `scope` + 会话 三层隔离**」
> 与「**一次问答 run 的边界**」。

## 目标
让 **AI 助手**的记忆体系**不可能越权**：
一个用户看不到别的用户的记忆，一个 `scope` 看不到别的 `scope` 的证据与记忆，
受限许可的来源不会被违规使用，出过问题的记忆能被撤销且整条链路可审计。

- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **定位：** 依赖 Phase 09（记忆底座）、Phase 10（复验与撤销）；被 Phase 16（Memory View / 审计展示）、
  Phase 17（发布前安全验收）依赖。
- **底线口径（"越权即事故"）：** 跨用户/跨 `scope` 的数据不是"过滤掉"，而是**根本查不到**；
  任何读写在缺少三键之一时一律 **fail-closed**（拒绝），不做"缺省放宽"。

## 边界
**记忆作用域（`user_id` + `scope` + 会话）、来源合规、可撤销与可审计。**

允许做：作用域强制、隔离对抗验证、长期时效与敏感源标注、结论安全 override、最小化日志、审计与回放。
不允许做：记忆 schema 本体（→ Phase 09）、时效与冲突算法（→ Phase 10）、答案合成（→ Phase 13）。

## 开始前
- [ ] 已读 MASTER_RULES
- [ ] 已读 V2 Architecture 相关章节
- [ ] 已读 IMPLEMENTATION_STATUS
- [ ] 前置 Gate 已通过（Phase 00 除外）
- [ ] 已确认 Phase 09 的三键字段（`owner_user_id` / `industry_pack_id` / `session_id`）在记忆表与召回接口上都已存在
- [ ] 已确认现役 `qa_runs` 的 `owner_user_id`、`industry_pack_id` 字段与 `qa_audit_events` 表可用

## 本项目真实的隔离边界（改写依据）
契约层统一使用中性的 **`scope`（作用域/租户键）**；**当前实现 = `industry_pack_id`**。
下表把 `scope` 落到现役的真实字段上：

| 维度 | 隔离键 | 现役依据 |
|---|---|---|
| 用户 | `owner_user_id` | `qa_runs.owner_user_id`、`qa_feedback.owner_user_id`、`qa_bridge_sessions.owner_user_id` |
| 作用域 | `scope`（**当前实现 = `industry_pack_id`**） | `qa_runs.industry_pack_id`、`qa_evidence` 检索收口、`qa_retrieval_cache.industry_pack_id` |
| 会话 / run | `session_id` / `run_id` | `qa_runs` 主键、`qa_stage_runs`、`qa_events` |
| 来源许可 | `license_profile` | 作用域配置 JSON 的每个源 + 信源 metadata；真身 `config/financial_source_licenses.json`（schema `financial-source-license-v1`）；强制范围见 P15-03 |
| 审计 | 事件流 | `qa_audit_events`（含 `industry_pack_id`） |

> 换维提示：把 `scope` 的当前实现从 `industry_pack_id` 换成客户、部门、项目维度时，
> 本节与 P15-01 的作用域过滤器**不需要改算法**，只改作用域配置即可
> ——这是 `00_REWRITE_SPEC.md` 第二节两层分离的落地要求。

## Must 子任务

### P15-01 记忆作用域强制（scope enforcement）
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `scope enforcement`；不得提前实现后续 Phase。
- **实现方法：**
  - 所有记忆读写入口（Phase 09 的 Write Gate、`POST /api/qa/v1/memory/recall`、Phase 10 的 revoke、
    Phase 12 的策略读取）统一经过一个**作用域过滤器**：
    `WHERE owner_user_id=? AND scope=? AND (scope_kind<>'USER_SESSION' OR session_id=?)`
    （`scope` 的当前实现 = `industry_pack_id` 字段）。
  - 该过滤条件**只能有一处实现**（一个函数 / 一个 repository 方法），禁止各调用点自己拼 SQL。
  - 缺任一必需键 → 立即拒绝（400 / 抛错），不做"缺省放宽为全作用域/全用户"。
  - 作用域提升（`USER_SESSION → USER_SCOPE`）必须是显式动作 + 事件记录，不允许自动提升。
- **测试方式：** Unit（三键缺失各自被拒；作用域 SQL 只在一处）+ Integration（真实召回路径全部走过滤器）；
  失败路径：直接构造跨 `scope` SQL 必须查不到数据。
- **验收标准：** 三键缺失 100% 被拒；过滤器是唯一入口（有测试守门）；显式提升有事件；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P15-02 隔离强制与对抗验证（isolation enforcement）
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `isolation enforcement`（本项目口径为"会话/run 边界 + 跨 `scope` 隔离"）；不得提前实现后续 Phase。
- **实现方法：**
  - **跨作用域对抗测试（必须成体系，不能只测一例）**：造 2 个用户 × 3 个 `scope`
    （**从运行时真实存在的作用域里任取，不写死任何具体行业**），
    交叉发起"读写对方的记忆/证据"请求，期望**全部为 0 命中**，且每次越权尝试在 `qa_audit_events` 里留痕。
  - **会话/run 边界**：一次 run 内的临时上下文（`RUN_BOUND`）不得自动写入长期记忆；
    跨 run 复用必须经过 Write Gate 与显式提升。
  - **证据侧同样收口**：检索必须带 `scope`；跨 `scope` 证据"能看到"即事故
    （现役 `tests/test_retrieval_evidence_policy.py` 守着检索侧策略，本阶段补"跨 `scope`"这一维）。
  - 越权尝试的响应必须是"查不到/无权限"，且响应体不得泄露对方 `scope`/用户的存在性信息（不返回对方名称）。
- **测试方式：** Unit（边界用例矩阵）+ Integration（对抗脚本全绿）+ 失败路径（故意去掉作用域收口 → 测试必须失败）。
- **验收标准：** 跨用户/跨 `scope` 读写 0 命中；越权尝试全部留审计；响应不泄露对方存在性；
  有测试能在收口被破坏时报警；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P15-03 长期时效与来源许可（longitudinal freshness & source license）
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `longitudinal freshness` 与来源许可校验的**读取与呈现**；不得提前实现后续 Phase。
- **实现方法：**
  - 长期记忆（`USER_SCOPE` / `SCOPE_GLOBAL`）必须带 freshness 档与来源发布时间精度
    （`exact/date/url/discovered`，见 `publish_time.py` 与 Phase 10 P10-01）；
    精度为 `discovered` 的长期记忆在呈现时必须标"时间不可信，仅作背景"。
  - **来源许可（已验证的真实口径，2026-10-08 实测确认）：**
    - 信源侧字段名就是 **`license_profile`**（出现在作用域配置 JSON 的每个源里，以及信源 metadata 中）。
    - 真身文件：`config/financial_source_licenses.json`，schema **`financial-source-license-v1`**。
      每条含 **14 项必填**：`source_type` / `source_id` / `owner_role` / `legal_review_owner_role` /
      `cost_type` / `billing_owner_role` / `credential` /
      `permissions{cache,display,redistribute}` / `quota` / `reviewed_on` / `review_due_on` /
      `authorization_status` / `production_allowed` / `production_approval_required` / `evidence_urls`。
    - **强制范围（关键，不要扩大）：仅对 `origin_pack_id == 'financial_markets'` 的 RSS 源做许可证校验。**
      其它作用域、其它来源类型**根本不检查**。
      > 引擎层写法要求：这条"哪些作用域需要许可校验"必须是**作用域配置声明的开关**，
      > 引擎只读开关；当前配置值 = `financial_markets` 的 RSS 源。换维/换包只改配置。
    - **未登记或复核过期 → 扫描时直接跳过，连请求都不发**（与现状一致）。
    - 因此**不要**写"字段缺失即视为受限、fail-closed 会误伤公开来源"——那是错误口径。
      正确口径：**仅对配置声明需校验的作用域（当前 = `financial_markets`）的 RSS 源做许可证校验，
      未登记即拒绝（与现状一致）；其余来源按公开处理。**
  - 呈现侧：命中许可校验的网络来源，其记忆/证据在对外输出时必须遵守
    `permissions{cache,display,redistribute}` 与该作用域的许可证结论；
    受限结果**不得**被其他 `scope` 读取（与 P15-01 的作用域过滤叠加生效）。
- **测试方式：** Unit（license_profile 解析、14 项必填校验、复核过期判定、开关作用域外不校验）+
  Integration（配置声明需校验的作用域里未登记源被跳过；**未声明校验的作用域不受影响**）；
  失败路径：许可证复核过期时必须跳过该源而不是照常请求。
- **验收标准：** 校验范围严格等于配置声明的作用域（不多校验、不少校验）；
  未登记/过期源连请求都不发；未声明校验的来源按公开正常处理（有测试证明不被误伤）；
  低精度时间有显式标注；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P15-04 结论安全 override（SafetyCritical override）
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `SafetyCritical override`；不得提前实现后续 Phase。
- **实现方法：**
  - **结论安全规则优先于 cost / latency / 用户体验 utility**（MASTER_RULES 17）。
    当安全判定与预算裁剪冲突时，安全判定胜出，且裁剪必须让路并记录原因。
  - 强制阻断项（任一命中即不允许输出结论）：
    1. 结论无已验证证据支撑（衔接 Phase 13 P13-04）；
    2. 使用了跨 `scope` 证据或未复验记忆；
    3. 使用了按 `permissions{cache,display,redistribute}` 不允许该用途的来源；
       （另注：在配置声明需许可校验的作用域里，未登记/复核过期的来源**根本不会被请求**，
       因此不可能进入证据与记忆——这一层由采集侧闸门保证，见 P15-03）
    4. 结论属于作用域配置声明的**高影响类别**却缺少支撑证据
       （引擎不内建任何具体业务类别词；"哪些算高影响"是配置项）。
  - 行动建议必须以"决策参考"形态输出，标注前提条件与依据引用，**不得**写成确定性结论。
  - 该 override 是**运行时硬门**，不是提示词里的一句叮嘱；必须能在测试中被单独触发与验证。
- **测试方式：** Unit（四类阻断项各自用例；安全判定胜出预算裁剪）+ Integration（真实 run 触发阻断）；
  失败路径：安全判定被预算裁剪覆盖 → 测试必须失败。
- **验收标准：** 四类阻断 0 漏检；安全优先有测试；行动建议措辞符合"决策参考"口径；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P15-05 最小化日志（privacy-minimal logs）
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `privacy-minimal logs`；不得提前实现后续 Phase。
- **实现方法：**
  - 日志与事件**不得**记录：密钥/凭据（LLM API key、RAGFlow KB/App id、`credential_ref` 等）、
    其它 `scope` 或其它用户的问题原文、受限来源正文。
  - 允许记录：id、状态、耗时、计数、错误码、脱敏后的摘要。
  - 现役契约里已有禁止外发字段清单（`qa_contracts._FORBIDDEN_CLIENT_FIELDS`），本阶段把它扩展到**日志侧**，
    并加一条自动检查（扫描日志样例里是否出现敏感键与其值）。
  - 记忆/审计日志同样适用：`qa_memory_events` 只存 id 与理由码，不存正文原文。
- **测试方式：** Unit（敏感键扫描器）+ Integration（跑一个真实 run 后全量扫描日志与事件表）；
  失败路径：故意写入一个假密钥，扫描器必须报出。
- **验收标准：** 日志与事件表 0 敏感值；扫描器可复跑；越权内容不入日志；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P15-06 可撤销与可审计（audit/replay）
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `audit/replay`；不得提前实现后续 Phase。
- **实现方法：**
  - **可撤销**：任何记忆都能被撤销（Phase 10 P10-06 接口），撤销后召回 0 命中、
    但审计链路仍可完整回溯（行不删、事件不删）。
  - **可审计**：下列动作全部写入 `qa_audit_events`（含 `industry_pack_id`，即 `scope` 的当前实现）：
    记忆写入/召回/复验/取代/撤销、跨 `scope` 访问被拒、受限来源被拒、结论安全阻断。
    每条要有：谁（`owner_user_id`）、哪个 `scope`、什么动作、结果、时间。
  - **可回放**：给定 `run_id`，能按时间顺序重建"检索了什么 → 用了哪些证据 → 出了什么结论 → 是否被阻断"，
    回放不依赖外部网络（用已落库数据）。
  - 回放能力复用现役 run 事件流（`qa_events` / `qa_stage_runs`），不新建第二套事件表。
- **测试方式：** Unit（事件字段完整性、时间序）+ Integration（真实 run 全链路回放）；
  失败路径：撤销后回放仍能查到"这条记忆曾存在并被谁撤销"。
- **验收标准：** 撤销后召回 0 命中且审计可查；回放可离线重建；跨 `scope`/受限/安全阻断全部留痕；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

## Phase 禁止事项
- 不得硬编码 benchmark 答案或删除/skip 测试。
- 不得把 BLOCKED 标为 PASS。
- 跨 Phase contract 变更必须先写 DECISION_LOG，并运行受影响回归。
- 不得以"缺省"为由放宽作用域（缺键必须拒绝）。
- 不得让 `permissions{cache,display,redistribute}` 不允许的来源进入对应输出用途。
- 不得把许可校验范围私自扩大或缩小（只校验配置声明需校验的作用域与来源类型；现状 = `financial_markets` 的 RSS 源）。
- 不得因为成本/延迟/体验而覆盖结论安全判定。
- 不得在日志、事件表、记忆负载里写入密钥或其它 `scope` 的内容原文。
- 不得用物理删除来"处理"违规数据（审计必须可回溯）。
- 不得把任何具体组织/行业维度写进隔离规则本身（规则只认 `scope`，换维只改配置）。

## Phase Gate
- [ ] 所有 Must 子任务 PASS
- [ ] Unit tests PASS
- [ ] Integration tests PASS
- [ ] Failure-path tests PASS
- [ ] 跨作用域对抗测试 PASS（跨用户/跨 `scope` 0 命中）
- [ ] Compatibility regression PASS
- [ ] IMPLEMENTATION_STATUS / ACCEPTANCE_MATRIX 已更新
- [ ] 无未处理高优先级 blocker（含"结论安全/作用域隔离 blocker"必须为 0）

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
