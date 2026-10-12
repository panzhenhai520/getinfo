# Phase 02 — Evidence Layer

## 目标
把"检索到的一段文字"升级为**可验证的证据对象**：能说清它来自哪篇文章（`article_id`）、
哪一段（`chunk_id` / span）、什么时候发布（含精度）、谁准它进池（准入来源）、
它支持哪条情报事实（claim）。**Chunk 不是证据，支持某条 claim 的最小 span 才是证据。**

## 边界
Chunk 升级为可验证 Evidence（文章 / 句子 / 结构化事件三类来源）。

## 开始前
- [ ] 已读 MASTER_RULES
- [ ] 已读 V2 Architecture 相关章节
- [ ] 已读 IMPLEMENTATION_STATUS
- [ ] 前置 Gate 已通过（Phase 00 除外）

---

## 本项目真实前置与现状（决定了本 Phase 的先做什么）

1. **证据池的"证据"目前主要是文章级 + 句子级，事件级几乎是空的。**
   - 实测：A 机 7022 篇里**事件三元组仅 3 行（0.0%）、实体归一 0 行**
     （`intel_article_events` / `intel_subject_canonical`）。
   - 因此本 Phase 的顺序是：**先把文章级 / 句子级证据做扎实**，
     事件级与实体级 Evidence **只定 schema 与校验器，等 `docs/爬虫改进实施步骤.md`
     阶段 7（事件抽取回填）、阶段 8（建图）达标后再接**。
   - 反例警告：不要因为"有 Evidence 表"就当作事件证据已就绪；`intel_article_events`
     行数必须作为 Evidence Layer 的巡检指标之一。
2. **时间是证据的一部分，且必须带精度。**
   - `publish_time.py` 在采集当刻写入发布时间，精度
     `exact / date / url / discovered`（`PRECISION_EXACT` / `PRECISION_DATE` /
     `PRECISION_URL` / `PRECISION_DISCOVERED`）。
   - 每条 Evidence 都要带 `published_at` + `published_precision`；
     `discovered`（仅知道抓取水位，不知道发布时刻）**不得**当 `exact` 用于时效判定。
3. **准入来源必须可追溯。**
   - 进池闸门：`intel_topics._classification_admitted`（关键词 / 锚点 + 相关性阈值，
     词表由作用域配置提供），语义兜底标记 `admission_source='llm_semantic'`（写入方
     `intel_classifier.classify_article_id`）。
   - 每条 Evidence 必须带 `admission_source`；没有它的证据不得进入 Phase 03 验证之后的入图环节。
4. **作用域隔离**：Evidence 必须带 `scope`（**当前实现 = `industry_pack_id`**），
   且与本次 run 一致，否则丢弃并记越权日志。

> **术语约定（引擎层硬规则）**：本阶段属于**通用引擎层**，正文一律用中性类型
> （`Evidence` / `Source` / `Scope` / `TimeWindow` / `Claim`），
> **不得出现任何具体行业词**；作用域差异一律由 Phase 01 的 `ScopeConfig` 提供。

---

## 子任务
### P02-01 Claim/Entity/Evidence/Source/Span schema
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Claim/Entity/Evidence/Source/Span schema`；不得提前实现后续 Phase。
- **实现方法：**
  - 落点复用现役表（**不要另建一套并行结构**）：
    - Evidence → `qa_evidence`（`run_id` / `evidence_ref` / `source_type` / `article_id` /
      `ragflow_kb_id` / `document_id` / `chunk_id` / `source_url` / `source_title` /
      `published_at` / `fetched_at` / `authority_level` / `content_hash` / `payload_json`）。
    - Claim → `qa_claims`（`claim_key` / `claim_text` / `claim_type` / `confidence` /
      `valid_from` / `valid_to` / `scope_json` / `verification_status`）。
    - Claim ↔ Evidence → `qa_claim_evidence`（`relationship` / `relevance_score`）。
    - Source → `qa_evidence` 的来源字段 + `intel_evidence_groups` /
      `intel_evidence_group_articles.authority_level`（权威级）；
      政策原文另见 `article_ragflow_documents`（`doc_type` / `doc_no` / `issuer` /
      `policy_title` / `effective_date` / `authority_level`）。
    - Span → 新增最小字段（`quote_span_start` / `quote_span_end` / `quote_text`），
      放进 `qa_evidence.payload_json` 或新增列（**待确认**，取决于是否要按 span 建索引）。
    - Entity → 现阶段走 `intel_subject_canonical.subject_key`（**当前 0 行**），
      在阶段 7 达标前只登记不产出。
  - 契约校验：复用 `qa_contracts.EVIDENCE_SCHEMA` / `CLAIM_SCHEMA` +
    `validate_level1_result` / `validate_level2_result`（它们已经是现役校验入口）。
  - `source_type` 取值域（**按数据源类别定义，领域无关**）：`evidence_store`（证据库检索）、
    `structured_store`（结构化产物）、`external_augment`（外部增强检索命中）、
    `public_network`（公开网络检索）、`trend_signal`（趋势信号）、
    `registry`（权威原始文件登记）。最终取值域**待确认**，
    必须与 `qa_retrieval._article_evidence(source_type=...)` 的现役取值一致；
    **每一类当前绑定的具体源由 `ScopeConfig.sources[]` 决定，引擎不写死。**
- **测试方式：** 单测：五类对象各自合法/非法用例；`source_type` 越界报错；
  缺 `scope` 或 `published_precision` 报错；与 `qa_contracts` 的兼容用例。
- **验收标准：** 一个真实 run 产出的每条 `qa_evidence` 都能回答"来自哪、什么时候、
  凭什么进池、支持哪条 claim"；schema 定义可机器校验。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P02-02 provenance
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `provenance`；不得提前实现后续 Phase。
  让每条证据的"出身"可逐级回溯，直到可点开的原文 URL。
- **实现方法：**
  - 回溯链（必须闭环，缺一环即视为不合格）：
    `evidence_ref → article_id → articles.url`（或 RAGFlow 的 `document_id` + `chunk_id`
    → `qa_ragflow_client.QaRagflowResearchClient.dataset_status` 体系）
    → `article_intel_classifications.score_details_json`（准入依据）
    → `published_at` + `published_precision`（时间依据）。
  - 权威级：`authority_level` 现役来源为
    `COALESCE(egp.authority_level, ard.authority_level, 1)`（见 `qa_retrieval._rows` 的 SQL），
    必须原样沿用，不新增第二套权威口径。
  - 二手转述识别：同一 `content_hash` / 同一 `event_hash` 出现在多个来源时，
    标记 `derived_from` 关系，供 Phase 03 的 source verifier 判"是否二手转述"。
  - `fetched_at` 与 `published_at` 必须分开记录（采集时刻 ≠ 发布时刻）。
- **测试方式：** 单测 + 真库抽检：随机 30 条 `qa_evidence`，人工核对回溯链每环；
  构造"URL 不可达 / article 已删除 / 缺 precision"三条失败路径。
- **验收标准：** 抽样 30 条证据的可回溯率 100%（或明确列出不可回溯原因与占比）；
  同一事实的多个来源能识别出转述关系。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P02-03 seen 与 confirmed
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `seen 与 confirmed`；不得提前实现后续 Phase。
  区分"看过"和"确认过"，避免重复垃圾检索（MASTER_RULES 第 14 条：rejected evidence
  仍属于 seen）。
- **实现方法：**
  - `seen`：本次 run（及跨 run 的同 query）见过的**所有**候选证据，
    含被 `intel_topics._classification_admitted` 拒掉的、被去重掉的、
    被 `qa_security.validate_outbound_url` 拦掉的；
    被拒原因复用 `ArticleRetriever._rows` 的 `excluded` 字典
    （现役键：`inactive` / `stale_activation` / `quality_gate` / `unsafe_url` / `policy_registry`）。
  - `confirmed`：经过 Phase 03 Verifier 之后入图的证据（`verification_status`）。
  - 存储：`qa_retrieval_cache`（`namespace` / `industry_pack_id` / `kb_version` / `scope_hash`）
    承担跨 run 的 seen 去重缓存；run 内 seen 记录进 `qa_search_trace.rejected_json`。
  - **去重要针对 seen，而不是 confirmed**（`01_V2_ARCHITECTURE.md` §2.9）。
- **测试方式：** 单测：同一 query 二次执行不重复检索（命中缓存）；
  被拒不等于没见过（第三轮不得重复拉同一批垃圾）；
  跨作用域的同 query **不得**命中缓存（`scope_hash` 必须含作用域键）。
- **验收标准：** 重复检索率可量化且显著下降；被拒候选在后续 round 不再重复出现；
  缓存跨作用域不串。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P02-04 fingerprint/去重
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `fingerprint/去重`；不得提前实现后续 Phase。
- **实现方法：**
  - 文章级：`qa_evidence.content_hash`、`article_intel_classifications.article_content_hash`、
    `articles` 侧既有去重逻辑；URL 归一用 `qa_retrieval.canonical_http_url`（现役）。
  - 事件级：`intel_article_events.event_hash`
    （`subject+action+object+type` 归一化的 sha256[:16]，用于把"同一事件的不同报道"聚簇）
    ——**当前该表只有 3 行，本项在阶段 7 达标前无法验收，标记为 BLOCKED-on-前置**。
  - 断言级：同一 `claim_id` 下重复的 `evidence_ref` 必须被
    `qa_claim_evidence` 的主键（`run_id, claim_key, evidence_ref, relationship`）挡掉。
  - 近重复：同一事件多家转载（标题近似、正文近似）→ 归为一个 evidence group
    （`intel_evidence_groups` / `intel_evidence_group_articles`），
    在证据计数里只算**一次独立来源**（这也直接决定 Phase 03 的 `independence` 维度）。
- **测试方式：** 单测 + 真库抽检：构造完全重复 / 标题微改 / 转载 / 同事件不同写法四类样本，
  断言去重结果与独立来源计数；事件级用例在阶段 7 完成后补跑。
- **验收标准：** 完全重复 0 残留；转载能归组；独立来源计数与实际来源数一致；
  事件级去重的"未达标"状态在 tracking 里显式记录，不得标 PASS。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

---

## Phase 禁止事项
- 不得硬编码 benchmark 答案或删除/skip 测试。
- 不得把 BLOCKED 标为 PASS。
- 跨 Phase contract 变更必须先写 DECISION_LOG，并运行受影响回归。
- （本项目追加）不得把 `intel_article_events` 的空表当作"事件证据已就绪"。
- （本项目追加）不得绕过 `intel_topics._classification_admitted` 把未准入文章写成 Evidence。
- （本项目追加）不得把 `published_precision='discovered'` 当作可信发布时间用于时效结论。

## Phase Gate
- [ ] 所有 Must 子任务 PASS
- [ ] Unit tests PASS
- [ ] Integration tests PASS
- [ ] Failure-path tests PASS
- [ ] Compatibility regression PASS
- [ ] IMPLEMENTATION_STATUS / ACCEPTANCE_MATRIX 已更新
- [ ] 无未处理高优先级 blocker
- [ ] `intel_article_events` / `intel_subject_canonical` 的当前行数已记录，
      事件级与实体级证据的"未就绪"状态在 tracking 中显式标注（含阻塞前置）

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
