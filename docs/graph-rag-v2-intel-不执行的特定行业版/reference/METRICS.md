# METRICS

> 口径以本文件为准。所有指标都必须能**按 `scope` 收口**统计
> （**`scope` 的当前实现 = `industry_pack_id`**；指标口径本身与领域无关，换维只改配置）；
> 无法按 `scope` 收口的指标要在此处标注"全局口径（不可按 `scope`）"，避免用全局数字掩盖单个作用域的问题。

Retrieval: Recall@K、MRR/nDCG(适用)、Evidence Recall、**跨 `scope` 命中数（必须恒为 0）**。
Evidence: entailment precision、citation precision/recall、unsupported claim rate、contradiction detection。
Agent: rounds、queries、nodes、parallelism、recovery、cache hit。
Context: precision/recall、useful density、token efficiency、counter-evidence coverage、Context-gap false retrieval。
Memory: hit utility、stale rate、revalidation pass、pollution rate、reuse gain、failure repetition、supersession accuracy。
System: latency、tokens、cost、quality。

结论安全与作用域隔离: critical-gap recall、**结论安全漏检（safety miss，必须为 0）**、澄清有效性（question utility）、
用户追问负担（同一会话被追问次数）、**跨用户/跨 `scope` 泄露次数（必须为 0）**、
**受限来源违规引用次数（必须为 0）**、记忆撤销后残留召回数（必须为 0）、审计覆盖率。

## 采集侧现实指标（必须与上面同表报告，不许只报问答侧）
- **发布时间精度分布**：`exact / date / url / discovered` 各自占比（`publish_time.py`）。
  精度为 `discovered` 的时间**不得**作为时效判定依据（Phase 10 P10-01）。
- **内部证据库准入率**：内容过闸门（`intel_topics._classification_admitted`）进入内部证据库
  （真表名 **`articles`**）的比例；以及 `admission_source='llm_semantic'` 语义兜底的占比。
- **结构化产物填充率**：`intel_article_events`（事件三元组）、`intel_subject_canonical`（实体归一）
  的填充率与行数（复核工具 `tools/structured_artifact_coverage_audit.py`）。
  **当前实测：7022 篇里事件三元组仅 3 行＝0.0%、实体归一 0 行。**
  任何图相关指标在填充率接近 0 时都必须同时报出填充率，否则数字无意义。
- **反爬止损影响面**：被 `antibot_detector` 判为"放弃派发"的信源数与它们覆盖的 `scope`/主题占比，
  用于解释"这个作用域最近证据变少"是采集限制还是世界真的没这事。
- **客观热度 vs 我们收到的（两组口径必须并排报告）**：
  客观口径来自 `trend_objective.py`（`objective_series` / `top_objective_terms`，基于 `intel_candidates`
  全量标题、按 domain 去重算独立媒体数），交付后由 **`GET /api/intel/trends/objective`**（主线实现）暴露；
  我方口径来自 `intel_topic_trends`（`GET /api/intel/trends`，只看我们收下并过闸门的内容）。
  两组并排用于量化信源偏差：差值就是"我们漏掉的"。**接口未就绪时标"接口未就绪"，不得用我方口径冒充客观口径。**

## 报告纪律
- 每项指标必须带**样本量**；样本不足写"样本不足"，不得用小数冒充结论。
- before/after 必须可复算（给出复算命令与数据快照标识）。
- 无收益项、BLOCKED 项、待确认项**一并列出**。
- 任何指标都不许为了好看而写进具体领域口径（指标体系必须保持领域无关）。
