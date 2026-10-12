# Phase 03 — Verifier Layer

## 目标
给"进图"这件事装一道闸：候选证据**只有通过验证**才能成为 Evidence Graph 里的边。
验证要回答八个问题（`01_V2_ARCHITECTURE.md` §10 的行业化版本）：
是否真的支持该情报事实、是否只是关键词撞词、实体是否同一、时间是否适用、
是否存在否定、来源是否够权威、是否只是二手转述、是否把相关关系当成了因果。

## 边界
候选证据验证后才能入图。

## 开始前
- [ ] 已读 MASTER_RULES
- [ ] 已读 V2 Architecture 相关章节
- [ ] 已读 IMPLEMENTATION_STATUS
- [ ] 前置 Gate 已通过（Phase 00 除外）

---

## 本项目真实基础（可直接承接，不要从零重写）

1. **相关性打分已有**：`qa_retrieval.ArticleRetriever.retrieve` 的关键词 / 短语 / 金额 /
   文号锚点打分，`qa_relevance.py`，以及政策原文的
   `ArticleRetriever._policy_exact_evidence` / `_policy_registry_rows` 与 `qa_policy_evidence.py`。
   P03-01 是**把现役打分接进 Verifier 契约**，不是另写一个 reranker 打分器。
2. **蕴含判定已有第一版**：【证据解析】的归因链路（`qa_attribution.py` 的
   `QaAttributionService`）已经让 LLM 对每个信息单元 × 每条证据句判
   `support / refute / irrelevant`，并落 `qa_attribution_links.relation` /
   `confidence` / `recall_score` / `method`。**这就是 entailment 的现役实现**，
   P03-02 应复用它并把结论提升为可入图的 `verification_status`。
   ⚠️ 边界不变：归因失败绝不影响问答主流程；近似法必须标注 `method`（不得冒充梯度）。
3. **时间与精度已可读**：`publish_time.py` 在采集当刻写入发布时间，精度
   `exact / date / url / discovered`。P03-03 的时间 verifier 必须读精度：
   `exact` 可判"是否落在时间窗"；`date` 只能判到日；`discovered` 只能当弱证据。
4. **权威级已有**：`authority_level`（来自 `intel_evidence_group_articles` /
   `article_ragflow_documents`，见 `qa_retrieval._rows` 的
   `COALESCE(egp.authority_level, ard.authority_level, 1)`）。不新增第二套权威口径。
5. **实体归一当前 0 行**：`intel_subject_canonical` 为空 →
   实体一致性 verifier 现阶段只能做**字面/别名级**判定；等
   `docs/爬虫改进实施步骤.md` 阶段 7 达标后再接 `subject_key` 精确判定（标注为待前置）。
6. **作用域隔离是硬闸**：证据的 `scope`（**当前实现 = `industry_pack_id`**）与本次 run
   不一致 → **直接判 REFUTE 并丢弃**，不是降权。

> **术语约定（引擎层硬规则）**：本阶段属于**通用引擎层**，正文一律用中性类型
> （`Evidence` / `Source` / `Scope` / `TimeWindow` / `Verifier`），**不得出现任何具体行业词**；
> 权威级别、时效长短、关系类型等差异一律由 Phase 01 的 `ScopeConfig` 提供。

---

## 子任务
### P03-01 relevance/reranker
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `relevance/reranker`；不得提前实现后续 Phase。
  产出 `relevance ∈ [0,1]` 与可读原因，作为 EvidenceScore 的第一个维度。
- **实现方法：**
  - 复用：`ArticleRetriever._article_evidence(row, score=..., method=..., reason=..., source_type=...)`
    已经是现役的"证据 + 分数 + 方法 + 原因"打包函数，Verifier 直接消费它，不重算。
  - 打分口径拓展（若需要）走"等价替换优先"：先把现役硬编码系数提成配置表并**逐项等值**，
    再谈调优（与 `docs/爬虫改进实施步骤.md` 阶段 11 的纪律一致）。
  - 明确区分"关键词命中"与"语义相关"：reranker 输出必须带 `method`，
    让下游能识别"只是词面相似"。
  - **待确认**：是否引入 RAGFlow 侧 reranker（`qa_ragflow_client` 是否暴露）——
    先探查再决定，不得假定。
- **测试方式：** 单测：`method` 必填；分数域 [0,1]；同输入同分（可复现）；
  对"标题撞词但正文无关"的样本必须给低分（构造负例）。
- **验收标准：** 相关性有分数、有方法、有原因三件套；benchmark 上 top-1 正确率不低于现役基线。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P03-02 entailment/NLI
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `entailment/NLI`；不得提前实现后续 Phase。
  判断"这条证据是否真的支持 / 反驳这条情报事实"，产出 `entailment` 与
  `verification_status`（`SUPPORTED` / `REFUTED` / `INSUFFICIENT` / `UNVERIFIED`）。
- **实现方法：**
  - 第一版**复用** `qa_attribution` 的 LLM 判定结果（`relation` ∈
    `support` / `refute` / `irrelevant`；`confidence`）映射到 `entailment` 与
    `verification_status`；映射表要写死并单测覆盖。
  - 组合顺序按 `01_V2_ARCHITECTURE.md` §10：`Reranker + NLI + Rule + LLM Verifier`，
    **不许只靠一个 LLM Judge**。
  - 规则层要拦三类典型误判：
    ① 时间不符（如"某年政策"被用来支持"本月变化"）；
    ② 主体不符（同名不同主体）；
    ③ 把"相关"当"因果"（仅共现、无机制或时序论证）。
  - **不得**让 LLM 自由生成的内容直接成为 Verified Evidence（MASTER_RULES 第 11 条）：
    LLM 只能给判定与理由，不能凭空产出证据文本。
  - 失败隔离：NLI 不可用时降级为 `UNVERIFIED`（该证据**不入图**），而不是当作通过。
- **测试方式：** 单测覆盖四类判定映射；构造"相关当因果""时间不符""主体不符"三个负例；
  端到端：拿现役 run 的 `qa_attribution_links` 回灌，确认 verifier 结论与人工判定一致率。
- **验收标准：** 每条进图证据都有 `verification_status` + `reason`；
  负例全部被拦；NLI 失败路径不产生"假通过"。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P03-03 entity/time/negation/source verifier
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `entity/time/negation/source verifier`；不得提前实现后续 Phase。
  四个独立可测的小 verifier，各自能单独拒绝一条候选证据。
- **实现方法：**
  - **entity verifier**：现阶段做字面 + 别名判定（可复用
    `qa_query_normalize.expand_terms` 的多写法展开），
    主体规范化（`intel_subject_canonical.subject_key`）**当前 0 行**，
    精确判定标记为**待前置**（`docs/爬虫改进实施步骤.md` 阶段 7）。
  - **time verifier**：读 `published_at` + `published_precision`；
    命中 `plan.time_window` 才允许 `SUPPORTED`；`precision='date'` 只判到日、
    `'discovered'` 只能作弱支撑（不得单独支撑时效类结论）；
    时间窗判断自身不确定时**宁可标 INSUFFICIENT 也不硬滤空**（与现役"加窗不加硬过滤"的取舍一致）。
  - **negation verifier**：规则优先（否定词 / 豁免 / 除外 / 不适用 / 失效 / 废止 等），
    识别"证据说的是相反结论"（例如"某政策已废止"被误当成"该政策有效"）。
  - **source verifier**：权威级（`authority_level`）、来源类型、是否一手
    （政策原文 vs 二手转述）、许可证 / 来源合规闸门、
    以及**作用域一致性**（`scope` 不一致 → 判 REFUTE 并丢弃 + 记越权日志）。
- **测试方式：** 每个 verifier 至少 3 个正例 + 3 个负例单测；
  真库抽检：随机 30 条证据跑四个 verifier，人工核对拒绝理由；
  非法作用域一致性用例（构造跨作用域证据，断言被丢弃且不落图）。
- **验收标准：** 四个 verifier 可独立开关、独立统计拒绝原因分布；
  跨作用域证据 100% 被拒；时间精度的三档行为有测试证据。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P03-04 EvidenceScore/reason/cache
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `EvidenceScore/reason/cache`；不得提前实现后续 Phase。
- **实现方法：**
  - 加权公式（第一版可解释优先，`01_V2_ARCHITECTURE.md` §11）：
    `EvidenceScore = wr×Relevance + we×Entailment + ws×SourceQuality
    + wf×Freshness + wi×Independence − wc×ContradictionRisk`，所有维度 ∈ [0,1]。
  - `Freshness` 必须**读 `published_precision`**：`exact` 全权重、`date` 降权、
    `url` / `discovered` 进一步降权（具体系数**待确认**，先给保守默认值并写明依据）。
  - `Independence` 依赖 P02-04 的转载归组：同一 evidence group 内的多条只算一次。
  - `reason`：每条分数必须能拆回各维度贡献（可解释），并落
    `qa_evidence.payload_json`（或 `qa_stage_runs.details_json`）。
  - `cache`：复用 `qa_resilience.QaPersistentResilience.cache_get` / `cache_put`
    与 `qa_retrieval_cache`（键含 `namespace` / `industry_pack_id` / `kb_version` / `scope_hash`），
    保证"同一证据默认禁止重复验证"（`01_V2_ARCHITECTURE.md` §5 第 6 条）。
  - Claim 级聚合字段：`support_count` / `independent_source_count` /
    `support_mass` / `refute_mass`，落 `qa_claims.payload_json`。
- **测试方式：** 单测：分数域、各维度独立贡献、权重改变后分数单调变化、
  缓存命中不重算、跨作用域缓存不串；`Freshness` 在三档精度下的差异用例。
- **验收标准：** 每条证据有分数 + 可拆解原因；重复验证命中缓存；
  Claim 级四个聚合字段可 SQL 查询。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

---

## Phase 禁止事项
- 不得硬编码 benchmark 答案或删除/skip 测试。
- 不得把 BLOCKED 标为 PASS。
- 跨 Phase contract 变更必须先写 DECISION_LOG，并运行受影响回归。
- （本项目追加）**未过 Verifier 的证据不得入图**；不得为了"图好看"降低验证门槛。
- （本项目追加）不得用单一 LLM Judge 替代 Reranker + Rule 组合。
- （本项目追加）不得把跨作用域证据降权留用——只能拒。

## Phase Gate
- [ ] 所有 Must 子任务 PASS
- [ ] Unit tests PASS
- [ ] Integration tests PASS
- [ ] Failure-path tests PASS
- [ ] Compatibility regression PASS
- [ ] IMPLEMENTATION_STATUS / ACCEPTANCE_MATRIX 已更新
- [ ] 无未处理高优先级 blocker
- [ ] 拒绝原因分布可查（含 `quality_gate` / 跨作用域 / 时间不符 / 二手转述四类）
- [ ] 实体精确判定的"待前置"状态已显式记录（`intel_subject_canonical` 当前 0 行）

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
