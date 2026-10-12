# Phase 04 — Parallel Retrieval Fleet（多路检索编队）

## 目标
把"一个 retriever 打天下"升级为**多路检索编队**：同一条查询计划同时派给多路，
各自在自己的数据源里找证据，再由 Phase 03 的 Verifier 统一收口（fan-in）。
编队的意义不是"路数多"，而是**每一路补的都是别的路找不到的那类证据**，
并且**任何一路缺席时整体仍能给出可用答案**。

## 边界
多路检索编队，按**与领域无关的数据源类别**组织：证据库检索 / 语义检索 / 外部增强检索 /
关系遍历 / 结构化数据 / 公开网络 / 趋势信号；**每一类具体接哪个源由作用域配置决定**，
编队并行且可降级。

## 开始前
- [ ] 已读 MASTER_RULES
- [ ] 已读 V2 Architecture 相关章节（§6 缺口动作、§8 Retrieval Fleet）
- [ ] 已读 IMPLEMENTATION_STATUS
- [ ] 前置 Gate 已通过（Phase 00 除外）
- [ ] 已确认本机是哪种模式（**A 机 = 无 RAGFlow 一级检索模式**；B 机 = 走 RAGFlow 网关）

---

## 〇、术语与作用域约定（引擎层硬规则，本 Phase 全程适用）

- 本 Phase 属于**通用引擎层**。编队按**与领域无关的数据源类别**定义，
  类别名一律中性：
  `EVIDENCE_STORE`（证据库检索）/ `SEMANTIC_INDEX`（语义检索）/
  `EXTERNAL_AUGMENT`（外部增强检索）/ `GRAPH_TRAVERSAL`（关系遍历）/
  `STRUCTURED_STORE`（结构化数据）/ `PUBLIC_NETWORK`（公开网络）/
  `TREND_SIGNAL`（趋势信号）/ `QUERY_EXPANSION`（检索式扩展）+ 编队调度。
  只使用抽象类型 `Evidence` / `Source` / `Scope` / `TimeWindow` / `Relation`，
  **不得把任何具体行业词写进算法**。
- **每一类里具体接哪个源，由作用域配置决定**（`ScopeConfig.sources[]`）。
  本文件中出现的具体实现名（如 `qa_retrieval.ArticleRetriever`）只是
  **「当前配置绑定」**——换个作用域可以换成别的源，引擎代码不变。
  读写这一栏时不要把绑定当成引擎的一部分。
- 作用域隔离统一写 **`scope`**（作用域 / 租户键），并注明「**当前实现 = `industry_pack_id`**」。
  换成任何其它维度（客户 / 部门 / 项目 / 产品线）都成立，算法不得假设它一定是行业。
- 每个作用域能提供什么（关键词与锚点、信源与权威级别、关系类型、时效策略、实体词表、
  分类权重）由 **Phase 01 的「作用域配置接口」（`ScopeConfig`）** 规定，本 Phase 只消费配置，
  不读死任何词表。

---

## 一、编队路由总览（类别 → 当前配置绑定）

| 类别（引擎层，领域无关） | 动作名 | 契约上查什么 | 当前配置绑定（真实文件 / 表，**可由配置替换**） | 承担子任务 | 就绪度 |
|---|---|---|---|---|---|
| ① 证据库检索 | `EVIDENCE_STORE` | 已过准入闸门的内容单元 | `qa_retrieval.ArticleRetriever.retrieve` / `_rows`（绑定：证据池表 `articles`） | P04-01 | **可用** |
| ② 语义检索 | `SEMANTIC_INDEX` | 向量近邻 | `intel_article_embeddings` + 现役嵌入端点（绑定：bge-m3） | P04-02 | **可用**（注意输入端截断） |
| ③ 外部增强检索 | `EXTERNAL_AUGMENT` | 作用域外的知识库命中 chunk | `qa_ragflow_client.QaRagflowResearchClient` / `qa_research.QaRagflowResearchService`（绑定：RAGFlow 数据集） | P04-03 | **A 机缺席**、B 机可用 |
| ④ 关系遍历 | `GRAPH_TRAVERSAL` | 实体 — 关系 — 实体邻域与路径 | `kg_nodes` / `kg_edges`（**未建**）+ `intel_article_events` / `intel_subject_canonical` | P04-03 | **等前置** |
| ⑤ 结构化数据 | `STRUCTURED_STORE` | 分类结论、事件三元组、实体归一、权威文件登记 | `article_intel_classifications`、`intel_article_events`、`intel_subject_canonical`、`article_ragflow_documents` | P04-04（子路 4a） | 分类与文件登记**可用**；事件 / 实体**等前置** |
| ⑥ 公开网络 | `PUBLIC_NETWORK` | 作用域外的公开原始文件 | `qa_retrieval.WebSearchService.search`（`default_web_search_service`）+ `qa_security.validate_outbound_url` | P04-04（子路 4b） | **可用** |
| ⑦ 趋势信号 | `TREND_SIGNAL` | 趋势序列与外部热榜（**元事实，非断言**） | `trend_objective.objective_series` / `top_objective_terms`；`buzzing_radar.hot_terms` / `radar_report` / `coverage_audit` / `publisher_ranking` | P04-04（子路 4c） | **可用** |
| ⑧ 检索式扩展 | `QUERY_EXPANSION` | 同义 / 缩写 / 多写法 / 时间窗 | `qa_research.QaResearchQueryPlanner.expand`、`qa_query_normalize.expand_terms` / `parse_time_window` | P04-05 | **可用** |
| ⑨ 编队调度 | — | fan-out / 超时 / 熔断 / 降级 | `qa_resilience.QaPersistentResilience`、`STAGE_BUDGET_SECONDS`、`qa_orchestrator.DEGRADABLE_STAGES` | P04-06 | **可用** |

> **读法**：左三列是引擎契约（任何作用域都成立）；第四列只是**当前绑定**。
> 例如 ③ 换一个部署可以绑到别的外部知识库，或干脆不绑（那一路自然缺席）——
> 引擎不做任何假设。
> **为什么 ⑥⑦ 挂在 P04-04 位**：⑤⑥⑦ 都属于"非证据池来源"的 adapter，
> 共用同一 adapter 契约（输入 = 结构化查询 / 外呼请求 + `scope`，
> 输出 = 带 `source_ref` 的 `Evidence` 或序列）。本版按
> `tracking/IMPLEMENTATION_STATUS.md` 现有的 **P04-01 ~ P04-06 六行**落位。
> **若决定把 ⑥ / ⑦ 单列为 `P04-07`（`PUBLIC_NETWORK`）/ `P04-08`（`TREND_SIGNAL`），
> 必须同步 tracking 两行——这一点待确认，见本文件末尾「待确认事项」。**

> **A 机的现实（必须写死进设计）**：A 机跑的是**"无 RAGFlow 一级检索模式"**，
> 即 ③ 外部增强检索这一类在当前配置下**未绑定可用源**，天然缺席；
> 此时 `qa_orchestrator.DEGRADABLE_STAGES = {"level2_retrieval", "level2_research"}`
> 会让 `level2_*` 阶段降级而不是失败。编队契约因此要求：
> **每一类都必须声明 `available` 与缺席原因**，**不允许任何一类成为硬依赖**。
> 另需注意：`qa_circuit_states` 里 `ragflow` 曾于 2026-10-06T07:39Z 连续 9 次失败后熔断，
> 导致送进草稿的证据从 27~28 条掉到 7 条——做任何"答案质量 / 耗时"前后对比前，
> 必须先确认该外部源在线，否则数字被这个变量污染。

---

## 子任务
### P04-01 BM25 Hunter（`EVIDENCE_STORE`：证据库检索）
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `BM25 Hunter`；不得提前实现后续 Phase。
  在**已准入的证据池**里做关键词 / 精确短语 / 编号 / 金额检索。
- **实现方法：**
  - 唯一入口 `qa_retrieval.ArticleRetriever.retrieve(plan, industry_pack_id=..., limit=...)`；
    候选集来自 `_rows(pack_id)`，其准入判据是
    `intel_topics._classification_admitted`（含 `admission_source='llm_semantic'` 语义兜底），
    被拒原因落在 `excluded` 字典（`inactive` / `stale_activation` / `quality_gate` /
    `unsafe_url` / `policy_registry`）——这些计数必须进 P01-02 的 SearchTrace。
  - 适合：主体名与机构名、编号类标识（政策文号，见 `_notice_variants`）、
    金额（`_amount_constraints`）、精确短语（`_query_phrases`）、锚点词
    （`_anchor_terms` / `_semantic_anchor_terms`）。
  - 多写法展开走 `qa_query_normalize.expand_terms`（简体 / 繁体 / 英文别名），
    **不去改写库里的正文**（现役取舍，继续保持）。
  - 时间维度按 Phase 02/03 的精度规则参与加权，**不做会滤空的硬过滤**（现役取舍）。
  - `scope` 强制：入口参数即作用域键；**当前实现 = `industry_pack_id`**。
- **测试方式：** 单测：锚点 / 短语 / 编号 / 金额四类命中；空命中返回空而不是编造；
  同 query 二次调用命中 `qa_retrieval_cache`；跨作用域请求被拒。
- **验收标准：** 关键词类问题的召回不低于现役基线；准入闸门不可绕过
  （`tests/test_retrieval_evidence_policy.py` 仍全绿）。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P04-02 Semantic Hunter（`SEMANTIC_INDEX`：语义检索）
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Semantic Hunter`；不得提前实现后续 Phase。
  用向量召回"换了说法、没说同一个词"的证据。
- **实现方法：**
  - 向量来源：`intel_article_embeddings`（bge-m3，1024 维）。
  - **输入端约束（实测坑）**：A 机 bge-m3 由 llama.cpp 提供（实测端点 `10.88.0.1:8082`，
    物理批大小 512），>512 token 输入会 500；现役客户端按 token 估算截断到 480
    （`INTEL_EMBEDDING_MAX_INPUT_TOKENS` 可调），并在服务端回"输入过长"时**缩小输入再试**。
    本子任务必须沿用该策略，不得原样重试。
  - 召回口径：`ArticleRetriever.__init__(database, semantic_search=...)` 已留出注入点
    ——优先接这里，避免新建并行链路。
  - 元数据前置过滤（阶段 11，P2）：按 `scope` / `TimeWindow` / 文档类型先过滤再算相似度；
    等价替换阶段要求输出与现役**逐条一致**，再谈调优。
  - 与 ① 的融合：加权融合（关键词 / 语义 / 时间 / 权威级），权重提成配置表，
    默认值必须与现役硬编码系数**逐项等值**。
- **测试方式：** 单测：超长输入自动截断后成功；端返回"输入过长"时的缩小重试；
  融合权重等价替换阶段用"结果逐条一致"做验收；缺少向量行时该路安全缺席。
- **验收标准：** 语义类问题召回明显高于纯关键词基线；嵌入端点异常时该路缺席而不拖垮整条 run。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P04-03 Graph Hunter adapter（`EXTERNAL_AUGMENT` + `GRAPH_TRAVERSAL` 两类共用的 adapter 位）
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Graph Hunter adapter`；不得提前实现后续 Phase。
  这个 adapter 位承担两个**数据源类别**，共用同一契约，缺一类不影响另一类：
  - ①`EXTERNAL_AUGMENT`（外部增强检索）：契约上是"作用域外知识库召回"，
    当前配置绑定 RAGFlow 数据集——B 机现在就能做，A 机**未绑定可用源**故缺席；
  - ②`GRAPH_TRAVERSAL`（关系遍历）：实体 — 关系 — 实体邻域与路径——**等前置**。
- **实现方法：**
  - `EXTERNAL_AUGMENT` 类（当前绑定 = RAGFlow 增强检索）：
    `qa_ragflow_client.QaRagflowResearchClient`（`configured()` / `health_check()` /
    `search_dataset(query, top_n=..., threshold=...)` / `dataset_status()` / `create_session()`），
    上层 `qa_research.QaRagflowResearchService.retrieve` / `research`，
    chunk 归一化 `normalize_ragflow_chunks`，回填 `enrich_ragflow_evidence_from_database`。
    产物 `source_type='ragflow_chunk'`（最终取值**待确认**，须与
    `qa_retrieval._article_evidence` 的现役取值域一致）。
    **绑定关系写在 `ScopeConfig.sources[]` 里，不写在引擎里**——换一个部署可以绑别的知识库，
    或者不绑。
  - `GRAPH_TRAVERSAL` 类（前置未达标）：
    数据来源 = `intel_article_events(subject, action, object)` +
    `intel_subject_canonical.subject_key` → `kg_nodes` / `kg_edges`
    （`docs/爬虫改进实施步骤.md` 阶段 8；**判定规则：事件覆盖率 < 60% 时状态只能
    是 `BLOCKED` / `DEFERRED`，不得用"图能跑通"冒充 PASS**）。
    **当前实测：事件三元组仅 3 行（0.0%）、实体归一 0 行 → 遍历现在只能返回空。**
    因此阶段 7 达标前本子任务只做两件事：
    ①把遍历的 adapter 契约定下来（输入 = 实体 / 主题 + `scope`，输出 = 邻域证据 + 路径）；
    ②必须**显式返回"数据源为空"（`empty_source`）而不是静默返回 0 条**，并计入 SearchTrace。
  - **A 机缺席语义**：`configured()` 为假时该类 `available=false` + `reason`，
    由 P04-06 统一处理，**这不是错误**。
  - 熔断联动：失败要写 `qa_circuit_states`（`QaPersistentResilience.circuit_failure`），
    恢复后 `circuit_success`；熔断打开期间不做无谓重试。
- **测试方式：** 单测：`configured()=false` 时安全缺席；`health_check` 失败时快速失败；
  熔断开时直接走降级分支；空表下返回 `empty_source`（不得返回伪造边）；
  替换配置绑定后引擎代码零改动（换源用例）；
  真机验收：B 机走一次真实外部检索并核对 chunk 归一化结果。
- **验收标准：** B 机 `EXTERNAL_AUGMENT` 类命中并产出可回溯 chunk；A 机该类缺席且整条 run 仍成功；
  空表状态可观测、可区分于"检索无结果"。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P04-04 Structured/DB/内部结构化数据 adapter（含 `PUBLIC_NETWORK` / `TREND_SIGNAL` 两个子路）
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Structured/DB` adapter；不得提前实现后续 Phase。
  这是"非证据库来源"的 adapter 位，含三个**数据源类别**子路（具体源由配置绑定）：
  - **4a `STRUCTURED_STORE`（结构化数据）**：从结构化数据集直接取事实（不经过正文检索）；
  - **4b `PUBLIC_NETWORK`（公开网络）**：作用域外的公开原始文件；
  - **4c `TREND_SIGNAL`（趋势信号）**：趋势序列与外部热榜。
- **实现方法（4a 内部结构化数据）：**
  - 可用数据源（现在就有）：
    - `article_intel_classifications`：`final_category`（`trend` / `event` / `other`）、
      `final_confidence`、`why_important`、`trend_summary`、`topic_tags_json`、
      `result_source`、`industry_pack_id` / `activation_id`。
    - `article_ragflow_documents`：权威文件登记（`doc_type='official_policy'` /
      `'official_interpretation'`、`doc_no`、`issuer`、`policy_title`、`effective_date`、
      `authority_level`）——精确检索走 `ArticleRetriever._policy_registry_rows` /
      `_policy_exact_evidence`。
    - `intel_evidence_groups` / `intel_evidence_group_articles`：证据分组与权威级。
    - `intel_topics` / `intel_topic_articles`：主题聚合。
  - 待前置的数据源（**必须标注，不得假装可用**）：
    - `intel_article_events`（当前 3 行）：事件时间轴与事件类型（`regulation` /
      `enforcement` / `release` / `market` / `transaction` / `other`）。
    - `intel_subject_canonical`（当前 0 行）：主体归并。
    - `kg_nodes` / `kg_edges`（尚未建）：邻域与路径。
    - 依赖 `docs/爬虫改进实施步骤.md` 阶段 7（抽取回填）与阶段 8（建图）。
- **实现方法（4b `PUBLIC_NETWORK` 公开网络）：**
  - 现役入口：`qa_retrieval.WebSearchService.search(queries, *, enabled=..., limit=...)`
    与 `default_web_search_service()`；provider 清单由现役配置提供（**待确认**）。
  - **出站必须过白名单** `qa_security.validate_outbound_url`
    （私网端点需显式加入白名单——历史事故：B 机因 `QA_LLM_BASE_URL_RAGFLOW=192.168.0.64`
    不在白名单导致 `INTERNAL_ERROR`）。
  - **外部内容不得升级为系统规则**：抓回正文必须过 `qa_security.untrusted_block`，
    注入模式（见 `qa_planner._INJECTION_RE`）要被识别（MASTER_RULES 第 16 条）。
  - 采集侧反爬：命中反爬 / 验证页时用 `antibot_detector` 识别并止损，
    不得把反爬页面当正文入库。
  - 时效：网络结果的发布时间同样按 `publish_time.py` 的精度体系标注
    （`exact` / `date` / `url` / `discovered`）。
  - `enabled=false` 时零外呼（现役参数），保持可关可回滚。
- **实现方法（4c `TREND_SIGNAL` 趋势信号）：**
  - 客观趋势（自家数据，可回溯）：`trend_objective.objective_series(...)`（序列与状态迁移，
    内部 `_state`）、`trend_objective.top_objective_terms(...)`、`is_domestic(domain)` 口径。
  - 外部热榜：`buzzing_radar.hot_terms(...)`、`radar_report(...)`、
    `coverage_audit(...)`（**覆盖差**：外部在热、自家没采到 → 直接是 Phase 07 的 Gap 输入）、
    `publisher_ranking(...)`、`feed` 抓取（`configured_feeds` / `fetch_feed` /
    `parse_feed_entries` / `clear_cache`）。
  - 产出形态是**序列 / 计数 / 榜单**，不是句子。因此：
    - 入 Evidence Graph 必须标 `source_type='trend'`，**禁止被当作事实断言**；
      它支撑"关注度变化"这类元事实，不支撑具体业务结论。
    - 必须带 `TimeWindow`（起止 + 窗口天数）与口径（词表来源），否则无法复现。
  - 缓存：热榜抓取走 feed 缓存，避免每次问答都打外部源。
- **测试方式：** 单测：分类聚合查询、权威文件精确查询、权威级排序；
  空表数据源返回 `empty_source`；跨作用域查询被拒；
  白名单外 URL 被拒、注入文本被阻断、反爬页面被识别、`enabled=false` 零外呼；
  趋势序列同窗口同输入同输出、热榜对固定 feed 快照回归。
- **验收标准：** 4a 的分类与权威文件查询能给出可回溯事实；
  4b 能带回可回溯的一手 URL 且零越权外呼；4c 能给出带窗口与口径的趋势证据，
  且趋势证据与事实证据在类型上可区分；事件 / 实体 / 图三类数据源在 tracking 中
  显式标为"未就绪（等阶段 7/8）"。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P04-05 Query Expansion
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Query Expansion`；不得提前实现后续 Phase。**扩展不能负责回答。**
- **实现方法：**
  - 现役入口：`qa_research.QaResearchQueryPlanner.expand(question, level1, plan, mode=..., max_queries=...)`；
    多写法展开 `qa_query_normalize.expand_terms`；时间窗 `qa_query_normalize.parse_time_window`。
  - 扩展维度：同义表述、缩写与全称、中英对照、上下位概念、相关实体
    （**具体词表由 `ScopeConfig` 提供，引擎不内置**）。
  - 扩展必须受 `max_queries` 预算约束，且每条扩展式都要进 SearchTrace（可解释）。
  - 用户的【调整思路】若含"看原文 / 逐段解释"等意图，扩展式里要追加专项检索式
    （与 `docs/爬虫改进实施步骤.md` 阶段 5 联动；该阶段为 P0）。
- **测试方式：** 单测：扩展式数量受预算约束；空输入不产生式；多写法展开只增不减
  （异常回退到原词表）；时间窗解析覆盖"最近一周 / 本月 / 某年某月"。
- **验收标准：** 扩展可解释（每条式能说出为什么加）；不引入语义漂移
  （扩展前后 top-1 相关性不下降）。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P04-06 fan-out/timeout/fallback
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `fan-out/timeout/fallback`；不得提前实现后续 Phase。
  编队的调度层：并行发出、各自超时、失败隔离、按需降级。
- **实现方法：**
  - 并行发出：各类输入都是同一条 `plan`（`plan.queries` / `plan.time_window` / `scope`），
    **互相不等待**；barrier 只出现在 Phase 03 的 fan-in（统一验证）。
  - 超时与预算：复用 `qa_resilience.STAGE_BUDGET_SECONDS` 与 `qa_orchestrator` 的
    `over_budget` 上报；超时一类只损失一类，不得中断全局。
  - 失败隔离与降级：`qa_orchestrator.DEGRADABLE_STAGES = {"level2_retrieval",
    "level2_research"}` 是现役降级集合；编队层要为每一**类**定义
    `available` / `degraded` / `skipped` 三态 + `reason`，并落 SearchTrace。
  - 熔断与限流：`QaPersistentResilience.circuit_before` / `circuit_failure` /
    `circuit_success` / `rate_limit` / `circuit_snapshot`。
  - 缓存：`cache_key` / `cache_get` / `cache_put`（键含 `scope` / `kb_version` /
    `scope_hash`），跨作用域不得命中。
  - **A 机缺席语义**：无 RAGFlow 一级检索模式下，`EXTERNAL_AUGMENT` 类与 `level2_*`
    阶段按降级处理，并必须在用户可见层说明"本次未使用外部增强检索"，不得静默。
- **测试方式：** 单测：单类超时不影响其他类；单类抛异常被隔离；熔断开时跳过；
  全类失败时给出"证据不足"而不是空答案；跨作用域缓存不命中。
- **验收标准：** 任一类缺席 / 超时 / 熔断都有明确三态与原因；
  `qa_runs.degraded` / `degradation_json` 能完整解释本次编队实际用了哪几类。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

---

## 待确认事项
1. **⑥ `PUBLIC_NETWORK` / ⑦ `TREND_SIGNAL` 是否单列为 `P04-07` / `P04-08`**：本版挂在
   P04-04 的子路 4b / 4c（与 tracking 现有六行一致）。若单列，需同步
   `tracking/IMPLEMENTATION_STATUS.md` 两行，并把 P04-04 的 4b / 4c 移出。
2. `qa_ragflow_client` 是否暴露 reranker 能力（影响 P03-01 与 P04-03 的边界）。
3. `PUBLIC_NETWORK` 类的 provider 清单与配额口径（**属配置层，不入引擎**）。
4. `source_type` 的最终取值域（须与 `qa_retrieval._article_evidence` 现役取值一致）。
5. 每类数据源的**配置绑定形态**：写进 `ScopeConfig.sources[]` 的哪几个字段
   （`source_id` / `source_type` / `authority_level` / `license` / 连接参数）。

---

## Phase 禁止事项
- 不得硬编码 benchmark 答案或删除/skip 测试。
- 不得把 BLOCKED 标为 PASS。
- 跨 Phase contract 变更必须先写 DECISION_LOG，并运行受影响回归。
- （本项目追加）**任何一类都不得成为硬依赖**：A 机在无 RAGFlow 一级检索模式下必须能正常出答案。
- （本项目追加）不得绕过 `intel_topics._classification_admitted` 把未准入内容送进编队。
- （本项目追加）不得跨作用域并发检索同一批证据；缓存键必须含 `scope`。
- （本项目追加）不得把 `TREND_SIGNAL` 类证据当作具体事实断言。
- （本项目追加）**引擎层不得出现任何具体行业词**；作用域差异一律走 `ScopeConfig`。
- （本项目追加）**不得把某一类的当前配置绑定（如某个具体知识库）写成引擎的固有假设**——
  绑定必须可在配置层替换或留空。

## Phase Gate
- [ ] 所有 Must 子任务 PASS
- [ ] Unit tests PASS
- [ ] Integration tests PASS
- [ ] Failure-path tests PASS
- [ ] Compatibility regression PASS
- [ ] IMPLEMENTATION_STATUS / ACCEPTANCE_MATRIX 已更新
- [ ] 无未处理高优先级 blocker
- [ ] A 机（无 RAGFlow）与 B 机（有 RAGFlow）各出一张"本次实际用了哪几路 + 缺席原因"的
      SearchTrace 截图
- [ ] 事件 / 实体 / 图三类数据源的"未就绪"状态已显式记录并绑定前置阶段
- [ ] 引擎层行业词静态检查（黑名单扫描）通过

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
