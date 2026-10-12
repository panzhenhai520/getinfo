# Phase 01 — Contracts & Research State

## 目标
让四张图（Execution Graph / Evidence Graph / Context Graph / Memory Graph）共用**一套类型契约**，
并让每一次问答 run 都成为**可回放的研究状态**：给定同一个 run，能还原当时用了哪些证据、
走了哪些 stage、命中哪些开关、为什么这么答。
本 Phase 只建契约与状态容器，**不实现任何检索、验证、生成能力**。

## 边界
建立四图共享的类型契约和可回放 run（本文档中"session"= 一次问答 run）。

## 开始前
- [ ] 已读 MASTER_RULES
- [ ] 已读 V2 Architecture 相关章节（尤其 §0.2 两层分离、§0.3 真实前置）
- [ ] 已读 IMPLEMENTATION_STATUS
- [ ] 前置 Gate 已通过（Phase 00 除外）
- [ ] Phase 00 的契约快照与 benchmark 已留档

---

## 〇、两层分离（本 Phase 的宪法级约束）

> 依据：规范 `00_REWRITE_SPEC.md` **§二「两层彻底分离」**（明确标注优先级高于术语对照表）
> 与 `01_V2_ARCHITECTURE.md` §0.2。**不要把领域词换成"我们自己的行业词"**——
> 那只是换了病名；正确做法是**把领域词中性化，行业只作为配置进入**。

| 层 | 内容 | 硬规则 |
|---|---|---|
| **通用引擎层**（本文件定义的全部契约与图结构） | 检索编队、Verifier、Evidence Graph、Gap Analyzer、Context Planner、Memory Graph、Answer Composer | **行业无关**。只允许出现抽象类型：`Entity` / `Claim` / `Evidence` / `Source` / `Scope` / `TimeWindow` / `Relation` / `Verifier` / `Gap`。**不得出现任何具体行业词** |
| **作用域配置层**（由每个作用域各自提供，不改引擎） | 关键词与锚点表、信源清单与权威级别、关系类型定义、时效策略、实体词表、分类权重 | **只以配置与数据进入**；算法不得依赖任何具体作用域 |

**作用域（`scope`）只允许出现在三个地方，且都是"作用域 / 配置"**：

1. **准入闸门**——判断某条内容是否属于本作用域的合格证据。
   这是关键词 / 锚点表，是配置项（当前实现：`intel_topics._classification_admitted` + 作用域配置里的词表）；
2. **作用域隔离**——Evidence 与 Memory 必须按 `scope`（作用域 / 租户键）收口。
   契约中统一写 **`scope`**，并注明「**当前实现 = `industry_pack_id`**」；
   换成任何其它维度（客户 / 部门 / 项目 / 产品线）都成立，**算法不得假设它一定是行业**；
3. **策略差异**——权威级别、时效长短、关系类型因作用域而异，一律写在配置里，不写在引擎里。

> 校验要求：引擎层代码与契约文件中**不得出现**任何具体行业词。
> P01-01 必须交付一条静态检查（关键词黑名单扫描 + 契约 schema 只允许抽象类型），并在 CI 中执行；
> 黑名单同时覆盖两个方向：**他方项目残留词**与**具体行业词**。

---

## 本项目真实前置（契约必须能表达它们）

> 契约的价值就在这里：如果契约无法表达"这个数据源现在是空的 / 时间是不可信的"，
> 后续 Phase 会把空图当正常图继续跑。以下四条必须在 P01-01 ~ P01-04 里各有着落。

1. **结构化产物填充率极低**
   - 实测：A 机 7022 篇里**事件三元组仅 3 行（0.0%）、实体归一 0 行**
     （`intel_article_events`、`intel_subject_canonical`）。
   - 契约要求：Evidence / Edge 必须带 `source_kind` 与 `coverage`，
     能表达"结构化事件当前覆盖 0.0%"，且下游必须据此**降级而非空跑**。
   - 前置任务：`docs/爬虫改进实施步骤.md` **阶段 7（事件抽取回填）**、
     **阶段 8（建图 `kg_nodes` / `kg_edges`）**。
     **判定规则：事件覆盖率 < 60% 时，任何依赖图遍历的子任务状态只能是 `BLOCKED` 或
     `DEFERRED`，不得用"图能跑通"冒充 PASS**；覆盖率巡检走
     `tools/structured_artifact_coverage_audit.py`。
2. **发布时间按精度理解**
   - `publish_time.py` 在采集当刻写入发布时间，精度为
     `exact / date / url / discovered`（`PRECISION_EXACT` / `PRECISION_DATE` /
     `PRECISION_URL` / `PRECISION_DISCOVERED`）。
   - 契约要求：凡带时间的 Evidence / Claim / Memory 字段，**必须成对携带
     `time_precision`**；校验器拒绝"只有时间没有精度"的对象。
3. **证据池准入闸门**
   - 进池必须过 `intel_topics._classification_admitted`（关键词 / 锚点 + 正文质量），
     并支持语义兜底 `admission_source='llm_semantic'`
     （由 `intel_classifier.classify_article_id` 写入）。
   - 契约要求：Evidence 必须带 `admission_source` 与 `scope` 两个字段；
     `scope` 缺失或与本次 run 不一致 → **校验直接失败**（越权而非降级）。
4. **作用域与配置分离的真实基线（Phase 00 应已记录，这里定契约）**
   - （以下为**配置层事实**，只用于证明分离必要性，**不得进入引擎契约字段**）
     数据库里有 **10 个作用域在跑**，而 `config/industry_packs/` 种子里只有 **8 个业务包 JSON**；
     其中 2 个作用域没有种子文件，只能手工在后台维护。
   - 这正是"引擎与配置分离"要解决的问题：**契约只能要求"配置单元提供什么"，
     不能要求"配置以文件形式存在"**，否则这 2 个作用域无法接入。

---

## 子任务
### P01-01 Node/Edge contract（含「作用域配置接口」）
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Node/Edge contract`；不得提前实现后续 Phase。
  交付三样东西：①四图共用的 Node / Edge 抽象；②**作用域配置接口 `ScopeConfig`**；
  ③引擎层行业词静态检查。
- **实现方法：**
  - 复用 `qa_contracts.py`：`QA_CONTRACT_VERSION = "unified-qa-v1"`、
    `EVIDENCE_SCHEMA`、`CLAIM_SCHEMA`、`CONFLICT_SCHEMA`、`FINAL_ANSWER_SCHEMA`、
    `QA_EVENT_SCHEMA`，以及 `_validate` / `_validate_reference_integrity`。
  - 新增四图契约模块（建议文件名 `graph_contracts.py`；**最终命名待确认**，不得与
    `qa_contracts.py` 职责重叠）：
    - `GraphNode`：`node_id` / `node_type` / `scope` / `payload` / `provenance` / `version`；
    - `GraphEdge`：`edge_id` / `from` / `to` / `edge_type` / `weight` / `reason` /
      `verifier_status`。
  - 节点类型最小集：`Question` / `SubQuestion` / `Claim` / `Entity` / `Evidence` /
    `Source` / `Gap` / `Contradiction`。
  - 边类型最小集：`REQUIRES` / `SUPPORTS` / `REFUTES` / `MENTIONS` / `DERIVED_FROM` /
    `DEPENDS_ON` / `CONTRADICTS` / `RESOLVES`；**具体关系类型定义由 `ScopeConfig.relation_types`
    提供**（引擎只做归一，不内置某一作用域的关系词）。
  - **必须补的字段**（来自上节）：`source_kind`（如 `article` / `event` / `entity_link` /
    `registry` / `web` / `trend`，最终取值域**待确认**）、`coverage`、`time_precision`、
    `admission_source`、`scope`。
  - **作用域配置接口 `ScopeConfig`（本子任务的硬性交付，字段组固定为七组）：**
    ``` text
    ScopeConfig
    ├── scope_id                 # 作用域标识（当前实现 = industry_pack_id）
    ├── keyword_anchors          # 关键词与锚点表（准入闸门用）
    ├── sources[]                # 信源清单：source_id / source_type / authority_level / license
    ├── relation_types[]         # 作用域允许的关系类型定义（供 Relation 归一用）
    ├── freshness_policy         # 时效策略：各证据类别的 freshness_class 与 TTL
    ├── entity_lexicon           # 实体词表（供 Entity 归一到 canonical_key）
    └── classification_weights   # 分类权重 / 置信阈值
    ```
    要求：契约只规定"需要提供什么"，**不规定存储形式**（文件 / 后台维护 / DB 行都可），
    以覆盖"种子文件缺失、只能后台维护"的作用域。
  - **静态检查**：黑名单关键词扫描（覆盖他方残留词与具体行业词）+ schema 只允许抽象类型；
    落 `tests/` 并在 CI 执行。黑名单本身放配置，不在代码里硬编码某个行业的词。
  - 版本兼容：新契约版本号 `<待确认>`，必须能读旧 run（回放历史 run 不得报错）。
- **测试方式：** 纯函数单测：合法对象通过；缺 `scope`、缺 `time_precision`、
  边两端节点类型不匹配、引用不存在的 `node_id` 一律报错；
  `ScopeConfig` 缺任一字段组即报错；静态检查对故意植入的行业词用例报错；
  旧版本 run 回放用例。
- **验收标准：** 契约有机器可校验定义 + 校验器；`ScopeConfig` 七个字段组齐全且有校验；
  与 `qa_contracts` 的 EVIDENCE / CLAIM / CONFLICT 三对象做**双向映射用例**；
  静态检查在 CI 中可用；`tests/test_qa_contracts.py` 不回归。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P01-02 ResearchSession/SearchTrace
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `ResearchSession/SearchTrace`；不得提前实现后续 Phase。
  明确"研究状态"放在哪、以什么结构存、怎么回放。
- **实现方法：**
  - 现状：`qa_runs`（run 主体，含 `scope`（当前实现 = `industry_pack_id`）/ `owner_user_id` /
    `session_id` / `mode` / `status` / `current_stage` / `degraded` / `degradation_json` /
    `final_answer_json`）+ `qa_stage_runs`（每 stage 的 `input_hash` / `output_hash` /
    `latency_ms` / `token_usage_json` / `details_json`）+ `qa_events`（SSE 事件流，
    事件类型见 `qa_contracts.QA_EVENT_TYPES`）。
  - `ResearchSession` = **一次问答 run**（`qa_runs.id`）；`SearchTrace` = 该 run 的检索轨迹
    （查询、命中、被拒原因、每路是否缺席）。
  - 新增部分：检索轨迹表（建议 `qa_search_trace`，**表名待确认**），字段建议：
    `run_id` / `round` / `route`（**与领域无关的数据源类别名**：`EVIDENCE_STORE` /
    `SEMANTIC_INDEX` / `EXTERNAL_AUGMENT` / `GRAPH_TRAVERSAL` / `STRUCTURED_STORE` /
    `PUBLIC_NETWORK` / `TREND_SIGNAL` / `QUERY_EXPANSION`；**具体接哪个源由
    `ScopeConfig.sources[]` 决定**）/ `available` / `query` /
    `hits` / `rejected_json`（带原因，复用 `ArticleRetriever._rows` 的 `excluded` 计数字典）/
    `empty_source` / `latency_ms` / `created_at`。
  - **不得**把完整 Research State 广播给所有 Agent（MASTER_RULES 禁止项）。
- **测试方式：** 单测：写入轨迹幂等（同 run + 同 round + 同 route 不重复）；
  回放一个历史 run 能重建 state；轨迹写失败不得影响主流程（异常隔离用例）；
  某一路缺席时 `available=false` 且 `reason` 必填。
- **验收标准：** 任取一个真实 run，能从 `qa_runs` / `qa_stage_runs` / `qa_search_trace`
  还原"问了什么、走了哪几路、每路命中几条、被拒几条及原因、每 stage 耗时与 token"。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P01-03 版本字段 corpus/retrieval/prompt/model
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `版本字段 corpus/retrieval/prompt/model`；不得提前实现后续 Phase。
  没有这四个版本号，所有"变好了"都无法归因。
- **实现方法：** 在每个 run（及每条 Evidence / Claim）上记录：
  - `corpus_version`：证据池快照标识——`articles` + `article_intel_classifications` 的
    行数与内容 hash（可用 `article_intel_classifications.article_content_hash` 汇总），
    并**单独记录** `intel_article_events` / `intel_subject_canonical` 的行数
    （当前 3 / 0，用来证明图数据是否已补齐）。
  - `retrieval_version`：检索实现版本（`qa_retrieval.ArticleRetriever` 的检索式 / 打分口径，
    以及 `qa_query_normalize` 的时间窗与多写法展开版本；具体常量名**待确认**）。
  - `prompt_version`：`ragflow_llm_client.LLM_PROMPT_VERSION`、`FUSION_VERSION`、
    `intel_classifier` 的 `CLASSIFIER_VERSION` / `TOPIC_TAGGING_VERSION`。
  - `model_version`：`qa_provider_profiles.model_id`（draft / research / synthesis 三个 provider）
    + 每次调用的实际端点（避免"接口通但没调模型"这类误判）。
  - 兼容：字段为新增列或 `payload_json` 内字段皆可，但**必须可 SQL 查询**（便于按版本分组对比）。
- **测试方式：** 单测：同一 run 内四个版本号必填且稳定；跨 run 分组统计能出"版本 → 指标"表。
- **验收标准：** 能回答"某次指标变化是语料变了、检索变了、提示词变了还是模型变了"；
  四个版本号在 benchmark 报告里逐条出现。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P01-04 idempotency/trace/round/node-run
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `idempotency/trace/round/node-run`；不得提前实现后续 Phase。
- **实现方法：**
  - 幂等：`qa_runs` 已有 `UNIQUE(owner_user_id, idempotency_key)`；
    `qa_stage_runs` 已有 `UNIQUE(run_id, stage, attempt)`；
    `qa_evidence` 已有 `UNIQUE(run_id, evidence_ref)`；
    `qa_claims` 已有 `UNIQUE(run_id, claim_key, stage)`。
    本子任务要做的是**把幂等语义写进契约**（同 key 必须同结果，跨版本变化要允许重算）。
  - trace：`qa_orchestrator` 已有 stage 级 trace（`_emit` / `qa_events`），
    新增的是 round / node-run 维度（对应 Phase 05 的 Execution Graph 节点执行）。
  - round：多跳轮次（Phase 07）；现在只定义 `round` 字段与约束
    （同一 round 内同 query 默认禁止重复执行；同一 Evidence 默认禁止重复验证）。
  - node-run：Execution Graph 上"某个节点跑过一次"的记录（建议 `qa_node_runs`，
    **表名待确认**），字段建议 `node_run_id` / `run_id` / `round` / `node_id` / `attempt` /
    `status` / `input_hash` / `output_hash` / `error_code` / `latency_ms`。
  - 失败隔离：单节点失败不得让整条 run 崩（对应 `qa_orchestrator` 的
    `DEGRADABLE_STAGES = {"level2_retrieval", "level2_research"}` 降级语义）。
- **测试方式：** 单测：重复提交同 `idempotency_key` 返回同一 run；同 round 同 query 去重；
  节点失败后其余节点继续；重试后 `attempt` 递增且历史不被覆盖。
- **验收标准：** 幂等 / trace / round / node-run 四件事都有机器可校验约束 + 失败路径用例；
  重跑同一 run 不产生重复证据或重复 claim。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

---

## Phase 禁止事项
- 不得硬编码 benchmark 答案或删除/skip 测试。
- 不得把 BLOCKED 标为 PASS。
- 跨 Phase contract 变更必须先写 DECISION_LOG，并运行受影响回归。
- （本项目追加）不得用 LLM 自由生成的内容填充 Verified Evidence / Verified Edge。
- （本项目追加）契约不得提供"跨 `scope` 复用"的便利接口；跨作用域只能是报错。
- （本项目追加）**引擎层不得出现任何具体行业词**；作用域差异一律走 `ScopeConfig`。

## Phase Gate
- [ ] 所有 Must 子任务 PASS
- [ ] Unit tests PASS
- [ ] Integration tests PASS
- [ ] Failure-path tests PASS
- [ ] Compatibility regression PASS
- [ ] IMPLEMENTATION_STATUS / ACCEPTANCE_MATRIX 已更新
- [ ] 无未处理高优先级 blocker
- [ ] `ScopeConfig` 接口交付且字段组齐全（含"种子文件缺失、只能后台维护"的作用域可接入）
- [ ] 引擎层行业词静态检查在 CI 中执行且通过
- [ ] 四图契约可用"空数据源"与"低精度时间"两种状态表达，并有对应单测

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
