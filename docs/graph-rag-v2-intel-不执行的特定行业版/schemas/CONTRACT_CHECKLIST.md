# CONTRACT CHECKLIST

核心对象检查：stable id、session/trace id、schema version、version context、provenance、validation、
serialization/replay、idempotency（适用）、audit（Memory / 结论安全 / 高影响动作）、
no-secret / no-cross-scope-content logging。

> **前置纪律（`00_REWRITE_SPEC.md` 第二节）：** 本清单描述的是**通用引擎层契约**，
> 必须**领域无关**。契约里只允许抽象类型
> `Entity / Claim / Evidence / Source / Scope / TimeWindow / Relation / Verifier / Gap`；
> 任何具体领域维度（来源类别与权威级别、时效策略、实体词表、关键词锚点）
> 一律以**作用域配置**进入，不得写进契约定义本身。

## 逐项口径
- **stable id**：Node / Edge / Skill / ContextItem / Memory / Action 一律用稳定 id；
  `skill_id`、`action_type`、`memory_type`、`relation` 等枚举集中定义，禁止调用点硬编码字符串。
- **session/trace id**：每个 run 必须带 `run_id` + `trace id`；可追溯到 `qa_runs` / `qa_stage_runs` / `qa_events`。
- **schema version**：每个对象带版本；migration 必须可回滚（Phase 17 P17-05 演练）。
- **version context**：`corpus_version` / `model_version` / `prompt_version` / `config_hash` 四元组；
  失败记忆与 skill 统计必须绑定它（Phase 12 P12-06、P12-05）。
- **scope（作用域/租户键）**：证据、记忆、动作的读写契约一律带中性的 **`scope`**
  （**当前实现 = `industry_pack_id`**）；算法只依赖 `scope`，不得假设它一定是"行业"。
- **provenance**：证据必须可回溯到 `evidence_ref → qa_evidence → articles.article_id → 原文 span`
  （内部证据库真表名是 **`articles`**，见 `sqlite_database.py:439`；**不存在 `intel_articles`**）；
  **只给域名不算可回溯**。记忆必须带 `source_run_id` + `source_evidence_ref`。
- **validation**：只有 Verifier 判定通过的证据才能入图/入结论（Phase 03 硬规则）；
  记忆必须过 Write Gate（Phase 09 P09-03）；`MEMORY_HINT` 不得冒充 Verified Evidence（Phase 10 P10-03）。
- **serialization/replay**：契约可序列化；给定 `run_id` 能离线重建全过程（Phase 15 P15-06）。
- **idempotency（适用）**：沿用 `qa_runs` 的 `UNIQUE(owner_user_id, idempotency_key)` 约定；
  重试（Phase 12 P12-06）、局部重试（Phase 13 P13-05）不得产生重复 run。
- **audit（Memory / 结论安全 / 高影响动作）**：记忆写入/召回/复验/取代/撤销、跨 `scope` 访问被拒、
  受限来源被拒、结论安全阻断，全部写 `qa_audit_events`（含 `industry_pack_id`，即 `scope` 的当前实现）。
- **no-secret**：不得记录密钥/凭据（LLM API key、RAGFlow KB/App id、`credential_ref` 等）；
  现役外发禁止字段清单见 `qa_contracts._FORBIDDEN_CLIENT_FIELDS`，本检查把它扩展到日志侧。
- **no-cross-scope-content**：日志、事件、记忆负载不得写入其它用户或其它 `scope` 的内容原文；
  只记 id、状态、计数、错误码与脱敏摘要。
- **scope enforcement（本项目必查项）**：任何证据/记忆/动作的读写契约都必须带
  `owner_user_id` + `scope`（会话级再加 `session_id`），缺任一即 fail-closed。
- **来源许可**：信源侧字段 **`license_profile`**（作用域配置 JSON 的每个源 + 信源 metadata），
  真身 `config/financial_source_licenses.json`（schema `financial-source-license-v1`，14 项必填，
  含 `permissions{cache,display,redistribute}` 与 `review_due_on`）。
  **校验范围由配置声明的开关决定，引擎不得内建**（当前配置值 = `origin_pack_id == 'financial_markets'`
  的 RSS 源）；未登记或复核过期 → 采集时跳过、不发请求。见 Phase 15 P15-03。
- **配置接口（Phase 01 交付物）**：作用域配置单元必须提供"关键词/锚点、来源清单与权威级别、
  关系类型定义、时效策略、实体词表、分类权重"——**引擎只读配置，不内建任何具体领域词**。
