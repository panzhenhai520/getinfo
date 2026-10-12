# Phase 00 — Project Discovery & Baseline

## 目标
在不改一行生产代码的前提下，把**现役 AI 助手链路的真实样子**探清楚、冻下来：
现役模块与函数、现役接口契约、现役证据池与结构化产物的真实填充率、
以及一套可复跑的 benchmark 与五项 baseline 数字。
后续所有 Phase 的"有没有变好"都以本 Phase 冻结的数字为准。

**同时必须交付一条边界**：把**「通用引擎层」与「行业配置层」彻底分开**——
哪个模块属于引擎（任何作用域都成立）、哪个只是某个作用域的配置与数据。
这决定了 Phase 01 的契约能不能做到"换一个作用域不改引擎代码"。

## 边界
只读探查、冻结现役问答链路（本文档中"原 RAG"= 本项目现役问答链路）与 benchmark。

---

## 〇、引擎层与行业配置层分离（Phase 00 必须先认账）

规范 `00_REWRITE_SPEC.md` §二 是最高优先级原则：**不要把领域词换成"我们自己的行业词"**——
那只是换了病名，病没治好。正确做法是**把领域词中性化，行业只作为配置进入**。

| 层 | 内容 | 硬规则 |
|---|---|---|
| **通用引擎层** | 检索编队、Verifier、Evidence Graph、Gap Analyzer、Context Planner、Memory Graph、Answer Composer（本套文档全部 Phase 的算法、契约、图结构） | **行业无关**。只允许抽象类型：`Entity` / `Claim` / `Evidence` / `Source` / `Scope` / `TimeWindow` / `Relation` / `Verifier` / `Gap`。**不得出现任何具体行业词** |
| **行业配置层** | 关键词与锚点表、信源清单与权威级别、关系类型定义、时效策略、实体词表、分类权重 | **只以配置与数据进入**；算法不得依赖任何具体行业 |

**行业只允许出现在三个地方，且都是"作用域 / 配置"**：
① 准入闸门（关键词 / 锚点表，配置项，当前实现：`intel_topics._classification_admitted` + 配置词表）；
② 作用域隔离（Evidence 与 Memory 按 `scope` 收口；契约统一写 **`scope`**，
注明「**当前实现 = `industry_pack_id`**」，换成客户 / 部门 / 项目 / 产品线同样成立）；
③ 策略差异（权威级别、时效长短、关系类型写在配置里，不写在引擎里）。

**Phase 00 的交付要求**：资产清单必须逐条标注每个模块属于哪一层；
凡属引擎层的，**不得出现任何具体行业词**；凡属配置层的，写清它从哪读、谁维护。
另需交付一条**静态检查**的输入（黑名单关键词表），供 Phase 01 在 CI 里跑。

> 注意：下面「作用于配置层的基线数据」（10 个作用域名）是**配置层事实**，
> 记录它是为了证明"引擎与配置分离"确有必要（其中 2 个作用域没有种子文件），
> **不是**允许把行业词写进引擎。

## 开始前
- [ ] 已读 MASTER_RULES
- [ ] 已读 V2 Architecture 相关章节
- [ ] 已读 IMPLEMENTATION_STATUS
- [ ] 前置 Gate 已通过（Phase 00 除外）
- [ ] 只读连接 A / B 机与本地库，**不做任何写入**（回填类工具一律 dry-run）

---

## 本项目真实前置（必须原样记录，不得当作已解决）

> 这四条不是"已知风险"，是**实测事实**。Phase 00 的职责就是把它们写进 tracking，
> 让后续 Phase 不会误以为"图已经能建、时间已经可信"。

1. **结构化产物填充率极低，建图前必须先补抽取**
   - 实测：A 机 7022 篇里**事件三元组仅 3 行（0.0%）、实体归一 0 行**
     （表：`intel_article_events`、`intel_subject_canonical`）。
   - 判据：**此时建图 = 建空图**。
   - 前置任务：`docs/爬虫改进实施步骤.md` **阶段 7（事件抽取回填）** 与
     **阶段 8（知识图谱归并 `kg_nodes` / `kg_edges`）**。
   - 对后续 Phase 的含义：Phase 02 的事件/实体类证据、Phase 04 的图路、
     Phase 06 的 Evidence Graph、Phase 07 的多跳，**都必须能表达"该数据源当前为空"**
     并降级，而不是把空图当正常图继续跑。
2. **发布时间（已在修，需按精度理解）**
   - 历史：`articles.published_at_utc` 曾全表为空（0/6105）。
   - 现状：RSS 与列表页已改为在**采集当刻**写入发布时间（`publish_time.py`），
     并按精度标注 `exact / date / url / discovered`（常量见 `publish_time.py`：
     `PRECISION_EXACT` / `PRECISION_DATE` / `PRECISION_URL` / `PRECISION_DISCOVERED`）。
   - 含义：**时间粒度与精度直接决定 Freshness 判定的可信度**。Phase 02 的 provenance、
     Phase 03 的时间 verifier、Phase 07 的 `MISSING_TIME_LINK`、Phase 10 的时效判定，
     都必须读精度，不允许把 `date` 精度当 `exact` 用。
3. **证据池准入闸门（不得绕过）**
   - 内容要进 AI 证据池，必须过关键词 / 锚点闸门与正文质量闸门：
     `intel_topics._classification_admitted`（被 `qa_retrieval.ArticleRetriever._rows` 调用，
     统计口径落在 `excluded["quality_gate"]`）。
   - 支持语义兜底：`admission_source='llm_semantic'`（由
     `intel_classifier.classify_article_id` 写入，仅当 LLM 真被调用且判定属于本作用域）。
   - 零作用域信号的内容**不得**进 AI 证据池（守卫用例：`tests/test_retrieval_evidence_policy.py`）。
4. **作用域隔离收口**
   - 证据检索按 `scope` 收口，**当前实现 = `industry_pack_id`**；
     **跨作用域取证据属于越权**，必须在 Phase 00 的资产清单里写清"哪些表带 `industry_pack_id`"。

**另需在探查中确认（不许猜）——作用于配置层的基线数据**：
数据库里有 **10 个作用域在跑**（`financial_markets`、`invest_mgmt`、`automotive_industry`、
`healthcare_news`、`education_news`、`family_office`、`ai_news`、`bolean_security_compute`、
`short_video_news`、`embodied_ai`），而 `config/industry_packs/` 种子里**只有 8 个业务包 JSON**
加 1 个 `schema.json`——`invest_mgmt` 与 `embodied_ai` **没有种子文件，只能手工在后台维护**。
这两组数字必须原样写进资产清单：它正是"引擎与配置分离"要解决的问题，
也决定了 Phase 01 的 `ScopeConfig` **不得要求配置必须以文件形式存在**。
（这些作用域名是**配置层事实**，只出现在本清单与配置文件中，
**不得**出现在任何引擎层算法描述或契约字段里。）

---

## 子任务
### P00-01 环境/目录/依赖探查
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `环境/目录/依赖探查`；不得提前实现后续 Phase。
  产出"现役链路资产清单"，明确每个模块的真实路径、入口函数、所依赖的表，
  **并按「通用引擎层 / 行业配置层」两栏标注每个模块归属**。
- **实现方法：**
  - 问答链路：`qa_gateway.QaGatewayService`（`create_run` / `get_run` / `events` / `cancel` / `retry`）、
    `qa_orchestrator.QaAgentOrchestrator`（`FULL_STAGES` / `FAST_STAGES` / `DEGRADABLE_STAGES`）、
    `qa_pipeline.build_qa_stage_handlers`（stage 处理器：`plan`、`level1_retrieval`、
    `level1_draft`、`level2_retrieval`、`level2_research`、`conflict_review`、`synthesis`、
    `citation_validation`）。
  - 检索与生成：`qa_retrieval.ArticleRetriever.retrieve`、`qa_level1.QaLevel1Generator.generate`、
    `qa_research.QaRagflowResearchService.retrieve` / `research`、`qa_ragflow_client.QaRagflowResearchClient`、
    `qa_relevance.py`、`qa_query_normalize.py`、`qa_policy_evidence.py`。
  - 韧性与安全：`qa_resilience.QaPersistentResilience`（`STAGE_BUDGET_SECONDS` / 熔断 / 限流 / 缓存）、
    `qa_security.validate_outbound_url` / `untrusted_block`。
  - 采集与结构化：`intel_worker.py` / `intel_light_scanner.py`（**注意：采集链路没有
    名为 `intel_pipeline` 的文件，别再按旧稿找**）、`intel_classifier`、`intel_topics`、
    `trend_objective`、`buzzing_radar`、`antibot_detector`、`publish_time`。
  - 表：`qa_runs` / `qa_stage_runs` / `qa_claims` / `qa_evidence` / `qa_claim_evidence` /
    `qa_conflicts` / `qa_events` / `qa_retrieval_cache` / `qa_circuit_states` / `qa_audit_events`
    （DDL 见 `qa_schema.py`）；`articles` / `article_intel_classifications` /
    `intel_article_events` / `intel_subject_canonical` / `intel_topics` / `intel_evidence_groups`
    / `article_ragflow_documents`（DDL 见 `intel_schema.py`、`sqlite_database.py`）。
  - **必须记录机器差异**：A 机 = 无 RAGFlow 一级检索模式；B 机 = RAGFlow 网关
    `192.168.0.64:8106`；本地 embedding / 模型端点（bge-m3、`QA_LLM_BASE_URL_*`）各自的实际地址。
  - 只读查询当前熔断态：`SELECT * FROM qa_circuit_states`（历史故障：本地 RAGFlow 于
    2026-10-06T07:39Z 连续 9 次失败后打开，导致送进草稿的证据从 27~28 条掉到 7 条）。
- **测试方式：** 只读脚本 / 只读 SQL；把清单写成机器可校验的表格（模块 → 入口 → 表 → 开关名）；
  对缺失项记录为"未查到"，不得推断。
- **验收标准：** 资产清单覆盖问答 / 检索 / 韧性 / 采集 / 结构化五类且逐条可点开真实文件；
  **每条都标注了「引擎层 / 配置层」归属**，且引擎层条目中不出现具体行业词；
  三台机器的 RAGFlow 与模型端点差异写明；`qa_circuit_states` 现状有截图或查询输出。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P00-02 冻结原 API contract
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `冻结原 API contract`；不得提前实现后续 Phase。
  冻结问答 HTTP 接口与答案 JSON 契约，作为 Phase 17 回归的基线。
- **实现方法：**
  - HTTP 面：`qa_gateway.py` 的 `qa_bp` 路由（`/api/qa/v1/runs`（POST/GET）、
    `/api/qa/v1/runs/<run_id>`、`/events`、`/cancel`、`/retry`、`/attribution`、
    `/token-influence`、`/config`、`/settings`、`/health`、`/metrics`、`/audit`、`/feature-flags`）。
  - 数据面：`qa_contracts.validate_final_answer`（`citation_validation` 阶段的落点）、
    `qa_contracts` 中对 stage 名与终态答案字段的定义。
  - 落库面：`qa_storage` 对 stage 输出的可见性规则（例如
    `level2_retrieval → level2_research → conflict_review → synthesis → citation_validation`
    的可见链）。
  - 冻结方式：抓取真实响应的字段快照 + `qa_schema.qa_schema_checksum()`，两者一起入库留档。
- **测试方式：** 冻结前后各跑同一组请求，做字段级 diff（新增字段允许、删除/改名不允许）；
  `tests/test_qa_contracts.py` 全绿。
- **验收标准：** 契约快照（字段 + 类型 + 必填 + 示例值）与 schema checksum 双双留档；
  任何后续 Phase 改契约必须先写 `tracking/DECISION_LOG.md` 并跑本 Phase 的回归。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P00-03 建立固定 benchmark
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `建立固定 benchmark`；不得提前实现后续 Phase。
  固定"问题集 + 证据池快照"这一对，使所有前后对比可比。
- **实现方法：**
  - 问题集：按作用域（当前实现 = 每个行业包）各选 N 问（N 待确认，建议每作用域 10~15），
    覆盖 SIMPLE_FACT / 对比 /
    时序 / 因果 / 多跳五类；同时覆盖"该作用域当前无证据"的负例（用来验证不编答案）。
  - 证据池快照：`articles` + `article_intel_classifications` 的行数与内容 hash
    （`article_intel_classifications.article_content_hash` 可用），以及
    `intel_article_events` / `intel_subject_canonical` 的行数——**后者现在应为 3 行 / 0 行**，
    这个数字就是 Phase 02/06/07 能否开工的判据。
  - 机器分层：A 机（无 RAGFlow）、B 机（有 RAGFlow）、本地开发机各留一份快照。
- **测试方式：** benchmark 冻结后跑两遍，确认同输入同输出（问答允许 LLM 抖动，
  但证据集合与 stage 链路必须一致；抖动范围记录下来）。
- **验收标准：** benchmark 文件（问题集 + 快照 hash + 机器标记）入库并打 tag；
  冻结后不得为提高成绩偷改（MASTER_RULES 第 7 条）。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P00-04 记录 Recall/Citation/Quality/Latency/Cost baseline
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `记录 Recall/Citation/Quality/Latency/Cost baseline`；
  不得提前实现后续 Phase。
- **实现方法：**
  - 五项指标定义：
    - **Recall**：benchmark 问题集上，`level1_retrieval` / `level2_retrieval` 送进模型的
      证据条数与被引用的证据条数（`qa_evidence` 行数、终答 `citations` 长度）。
    - **Citation**：`citation_validation` 的通过率、可回溯率（`evidence_ref → article_id → url`
      闭环比例），以及跨作用域引用数（**必须为 0**）。
    - **Quality**：`qa_runs.degraded` 与 `qa_runs.degradation_json` 的降级比例
      （历史事故：B 机因白名单与超时未配置，全程降级、耗时百毫秒级 → 说明模型从未被调用）。
    - **Latency**：`qa_stage_runs.latency_ms` 按 stage 分组的中位数 / P95，
      以及 `qa_orchestrator` 的 `over_budget` / `STAGE_BUDGET_SECONDS` 关系。
    - **Cost**：`qa_stage_runs.token_usage_json` 汇总 + LLM 调用次数。
  - 取证纪律：**不看接口通不通，看 `qa_stage_runs` 里 `level1_draft` / `synthesis`
    的耗时与 `qa_runs.degradation_json`**——耗时百毫秒级 + `degraded=1` 就说明没调到模型。
  - 已知会污染对比的变量：本地 RAGFlow 熔断（`qa_circuit_states`）。做任何前后对比前
    必须先确认 RAGFlow 在线，否则数字不可归因。
- **测试方式：** 用 benchmark 跑 N 次，按 machine × stage 出具 baseline 表；
  对每个数字标注取证 SQL 与查询时间。
- **验收标准：** 五项指标在 A / B / 本地各自有可复跑的 baseline 表；
  "RAGFlow 在线状态"作为每次测量的前置条件被记录；数字与 evidence 写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

---

## Phase 禁止事项
- 不得硬编码 benchmark 答案或删除/skip 测试。
- 不得把 BLOCKED 标为 PASS。
- 跨 Phase contract 变更必须先写 DECISION_LOG，并运行受影响回归。
- （本项目追加）不得在 Phase 00 写任何生产数据；不得为了"看起来有图"而把
  `intel_article_events` / `intel_subject_canonical` 的空表填充率改写成模糊表述。
- （本项目追加）不得绕过 `intel_topics._classification_admitted` 直接把未准入内容算作证据。
- （本项目追加）**不得把某个作用域的具体词表、信源、权威级别写进引擎层的资产条目或模块描述**——
  这些一律归到配置层栏位。

## Phase Gate
- [ ] 所有 Must 子任务 PASS
- [ ] Unit tests PASS
- [ ] Integration tests PASS
- [ ] Failure-path tests PASS
- [ ] Compatibility regression PASS
- [ ] IMPLEMENTATION_STATUS / ACCEPTANCE_MATRIX 已更新
- [ ] 无未处理高优先级 blocker
- [ ] 现役链路资产清单、契约快照、benchmark、五项 baseline 均已留档
- [ ] 资产清单已按「引擎层 / 配置层」两栏标注；引擎层条目零具体行业词
- [ ] 黑名单关键词表（供 Phase 01 的 CI 静态检查使用）已产出

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
