# Graph-RAG Agent：智能深度检索、上下文工程与长期记忆方案 V2.0（行业情报版）

> V2.0 在 V1.0 的 Graph Engineering、Evidence Graph、Gap-driven
> Multi-hop RAG 和 SAGE-RAG Evidence Acquisition 基础上，引入 Context
> Engineering 与 Memory Engineering。 **规范架构：Execution Graph +
> Evidence Graph + Context Graph + Memory Graph。**
>
> 本版把原场景整体改写为**本项目（行业情报采集与分析平台）**场景：
> 服务对象是对话框里的 **AI 助手**，输入是用户关于某作用域内
> **实体与事件**的自然语言提问，
> 输出是**只用已验证证据、可回溯引用**的答案。

## 0. V2.0 核心升级

V2.0 把 Shared Research State 改为按任务组装的 Context
Pack，并新增跨会话 Memory Graph。Planner 拆成 Research Planner、Context
Planner、Memory Planner；Gap 拆成 Evidence Gap、Context Gap 和 Memory
Opportunity；Retrieval Fleet 上方增加 Skill Registry/Skill Router。

关键原则：没有找到证据，与证据已经存在但没有送给正确
Agent，是两种不同失败；过去验证过与当前仍成立也不是一回事。Memory 是
Research Accelerator，不是 Truth Oracle。

## 0.1 系统定位：本方案只为一件事服务——让 AI 助手真正聪明

本项目的现实形态：

- 采集侧：`intel_worker.py` / `intel_light_scanner.py` 按作用域（当前实现 = 行业包）持续采集，
  `intel_classifier.py` 分类，结果落到证据池表 `articles` 与
  `article_intel_classifications`；客观趋势由 `trend_objective.py` /
  `tools/buzzing_radar.py` 计算；`antibot_detector.py` 负责反爬识别与止损。
- 问答侧：用户提问 → `qa_gateway.py`（配额/降级/模式路由）→ `qa_retrieval.py`
  的 `ArticleRetriever` 取证 → 合成 → `qa_pipeline.py` 的 `citation_validation`
  节点校验引用 → 由 `qa_storage.py` 落库为一条 `qa_runs` run。
- 现役模式：**A 机为"无 RAGFlow 一级检索模式"**；B 机走 RAGFlow 网关
  （`192.168.0.64:8106`，经 `qa_ragflow_client.py`）。

因此本方案里出现的每一个 Phase，都必须能回答一句：
**它让用户在对话框里得到的那个答案，在哪一点上变强了？**
答不上来的子任务不做。

## 0.2 最高架构原则：引擎层与配置层彻底分离（行业无关）

> 本节优先级**高于**本文其他一切写法约定。原文的领域词一律中性化，
> **不得**把它们换成"我们自己的行业词"——那只是把别人的领域耦合换成我们的领域耦合。

本方案分两层，边界必须可机器校验：

| 层 | 内容 | 硬规则 |
|---|---|---|
| **通用引擎层** | 检索编队、Verifier、Evidence Graph、Gap Analyzer、Context Planner、Memory Graph、Answer Composer（即本文 §1–§20 与附录 A 的全部算法、契约、图结构） | **行业无关**。只允许出现抽象类型：`Entity` / `Claim` / `Evidence` / `Source` / `Scope` / `TimeWindow` / `Relation` / `Verifier` / `Gap`。**不得出现任何具体行业词**——既不许保留他方项目的领域词，也不许写入本项目任何具体行业的词（行业名、机构类型、产品名、标的物俗称一律禁止） |
| **作用域配置层** | 关键词与锚点表、信源清单与权威级别、关系类型定义、时效策略、实体词表、分类权重 | **只以配置与数据进入**；算法不得依赖任何具体作用域 |

**作用域（scope）只允许出现在三个地方，且都是"作用域 / 配置"**：

1. **准入闸门**——判断某条内容是否属于本作用域的合格证据。这是关键词/锚点表，
   是配置项（当前实现：`intel_topics._classification_admitted` + 作用域配置里的词表）；
2. **作用域隔离**——Evidence 与 Memory 必须按 `scope`（作用域键）收口。
   契约中统一写 **`scope`**，并注明「**当前实现 = `industry_pack_id`**」；
   换成任何其它维度（客户、部门、项目、产品线）都成立，**算法不得假设它一定是行业**。
   （早期文档曾把这个维度写作"租户隔离"，含义相同；**本包一律称"作用域隔离"**。）
3. **策略差异**——权威级别、时效长短、关系类型因作用域而异，一律写在配置里，
   不写在引擎里。

**因此 Phase 01 的契约必须包含「作用域配置接口」**：规定一个配置单元**需要提供什么**，
而不是把某个作用域的词写死。契约至少规定以下六个字段组：

``` text
ScopeConfig
├── scope_id            # 作用域标识（当前实现 = industry_pack_id）
├── keyword_anchors     # 关键词与锚点表（准入闸门用）
├── sources[]           # 信源清单：**只 4 个字段** source_id / source_type / authority_level / license
│                       #（连接细节不进契约，见 §0.2.2；信源侧许可证字段名为 license_profile）
├── relation_types[]    # 作用域允许的关系类型定义（供 Relation 归一用）
├── freshness_policy    # 时效策略：各证据类别的 freshness_class 与 TTL
├── entity_lexicon      # 实体词表（供 Entity 归一到 canonical_key）
└── classification_weights  # 分类权重/置信阈值
```

> 校验要求：引擎层代码与契约文件中**不得出现**任何具体行业词。
> 建议在 Phase 01 加一条静态检查（关键词黑名单扫描 + 契约 schema 只允许抽象类型），
> 并在 CI 中执行。黑名单同时覆盖两个方向：他方项目残留词与具体行业词。

### 0.2.1 已定决策：契约用 `scope`，实现用 `industry_pack_id`（Phase 01 硬边界）

**边界（不得越界）**：

``` text
契约层（类型定义 / ScopeConfig / Shared State / Search Trace / 缓存键 /
        记忆作用域枚举 / 指标名）
        → 一律用 scope
──────────────── 只允许一处映射函数 ────────────────
实现层（qa_retrieval.py / qa_storage.py / 真实列名
        article_intel_classifications.industry_pack_id 等）
        → 保持 industry_pack_id 不动
```

**硬规则**：**契约用 `scope`，实现用 `industry_pack_id`，两者之间只允许一处映射函数**
（一个函数 / 一个 repository 方法，例如 `resolve_scope_key(scope) -> industry_pack_id`）。
禁止在检索与存储的各个调用点各自把 `scope` 拆成 `industry_pack_id`。

**为什么不做参数抽象**：真做一层参数抽象，要改现役检索与存储的每一处查询——
那是跑在生产、且刚刚修好的链路（证据准入、证据解析、B 机降级修复）。
Phase 01 就动它，等于拿线上稳定性换一个"将来可能换维度"的可能性；
而契约层用 `scope` 已经能拿到全部收益（换维度时只改那处映射，不改引擎）。

> 这条与 Phase 15 的「作用域过滤条件**只能有一处实现**，禁止各调用点自己拼 SQL」
> 是同一原则的两个落点：**过滤只允许一处实现，映射也只允许一处实现。**

### 0.2.2 契约字段最小化：`ScopeConfig.sources[]` 只声明 4 个字段

`ScopeConfig.sources[]` 的每一项**只允许**出现下面 4 个契约字段：

``` text
sources[]:
  - source_id        # 稳定标识
  - source_type      # 来源类型（取值域由配置层枚举）
  - authority_level  # 权威级别（引擎只用它排序，不解释含义）
  - license          # 许可证声明（信源侧字段名为 license_profile，见 §14.1）
```

**连接细节（地址、凭据、超时、重试、代理等）留在配置层，不得进契约字段。**

理由：**连接细节里含凭据**。一旦让它进契约字段，就会顺着
**Shared State → Search Trace → 缓存键 → 日志**一路扩散：
Shared State 会被序列化进 replay 快照，Search Trace 会写进
`qa_search_trace` 这类轨迹表，缓存键会进 fingerprint，
日志会被采集——**这与 MASTER RULES 第 18 条「最小化日志、不得泄露密钥」直接冲突**。
引擎层只需要知道"有哪些源、多权威、许可证怎么规定"，
**不需要、也不允许知道怎么连**（怎么连由配置层的加载器负责，运行时注入）。

> 校验要求：`ScopeConfig` 的 schema 用 `additionalProperties: false` 收紧；
> 静态检查里加一条"契约字段中不得出现 `url` / `host` / `endpoint` / `token` /
> `api_key` / `credential` / `proxy` / `timeout` 等连接类字段名"。

## 0.3 本项目真实前置与约束（改写版新增，Phase 00/01/02 必须先认账）

下面六条是**实测到的现状**，不是设计意图。任何阶段在动手前都要先核对这几条，
否则会做出"结构完整但数据为空"的空转系统。

### 约束 1：结构化产物填充率极低，**必须先补抽取再建图**

实测（A 机）：7022 篇活跃文章里，**事件三元组仅 3 行（0.0%）、实体归一 0 行**。
这意味着：

- 此时执行 Evidence Graph 的图谱多跳、Graph Hunter、实体桥接，**等于查空图**；
- 所以 **Phase 02（Evidence Layer）之前必须先做前置回填**，见
  `docs/爬虫改进实施步骤.md` **阶段 7（事件抽取回填）** 与 **阶段 8（建图）**；
- 阶段 7 的目标产物：`intel_article_events`（主体/动作/客体/实体/事件时间/事件类型）、
  `intel_subject_canonical`（`subject_key` 规范化实体）、覆盖率达到活跃文章 ≥ 60%；
- 阶段 8 的目标产物：`kg_nodes` / `kg_edges`（**派生视图**，源表为准，可重跑幂等）。

**判定规则**：事件覆盖率 < 60% 时，任何依赖图遍历的子任务状态只能是 `BLOCKED`
或 `DEFERRED`，不得用"图能跑通"冒充 PASS。覆盖率巡检走
`tools/structured_artifact_coverage_audit.py`。

### 约束 2：发布时间缺失（已在修），时间粒度与精度决定 Freshness 的可信度

- 历史问题：`published_at_utc` 曾全表为空（0/6105）。
- 现状：RSS 与列表页已在**采集当刻**写入发布时间（`publish_time.py`），
  并按精度标注四档：`exact` / `date` / `url` / `discovered`
  （常量 `PRECISION_EXACT`、`PRECISION_DATE`、`PRECISION_URL`、`PRECISION_DISCOVERED`）。
- 因此本方案中所有 Freshness 判定必须**带上精度**：
  - `exact`：可参与"小时级"时效判定；
  - `date`：只能按天判定，不得推断当天先后；
  - `url`：从 URL 路径推断，可信度低，只作为兜底；
  - `discovered`：**采集发现时间 ≠ 发布时间**，禁止当作发布时间参与结论，
    只能用于"该文何时进入证据池"。
- **存量空缺口径（已定）**：`published_at_utc` 为空的历史文章，Freshness 标 `UNKNOWN`，
  **不得作为"最新变化"的证据**；但**仍可作为背景/历史证据参与检索**，
  只是不进入时效敏感推理（例如"最近 N 天发生了什么"这类问法不得引用它们）。
- **不需要专门的历史回填任务**：采集侧现已按 `exact/date/url/discovered` 精度写入
  （`publish_time.py`），新采集会自然覆盖存量，`UNKNOWN` 占比随时间单调下降；
  巡检时把它当作一个下降中的观测指标即可，不要为它单开一条工程线。

### 约束 3：证据池准入闸门（不是所有采集到的文章都能当证据）

文章要进 AI 证据池，必须通过两层闸门：

1. **作用域关键词 / 锚点闸门 + 正文质量闸门**：由 `intel_topics._classification_admitted`
   判定（词表来自作用域配置，属配置项），未通过的文章不计入可检索证据；
2. **语义兜底**：`intel_classifier.classify_article_id` 在带锚点但无强信号时，
   允许写入 `admission_source='llm_semantic'`，由 LLM 语义复核放行
   （见 `intel_topics.py` 中 `_classification_admitted` 对 `llm_semantic` 的分支）。

相关模块：`intel_admission.py`、`intel_content_quality_gate.py`。

**架构含义**：Evidence Graph 的 `Source` 节点必须带 `admission_source` 与闸门判定结果；
`llm_semantic` 放行的证据在引用时要标明其准入路径。LLM 自由放行**不得**被当成
Verified Evidence（对应 `00_MASTER_RULES.md` 第 11 条）。

### 约束 4：【证据解析】与【词元影响力】已交付，Phase 13/16 要**承接**而不是重写

- 已交付能力（`qa_attribution.py`，服务类 `QaAttributionService`）：
  - **证据解析**：把答案分解到句/声明粒度，并与证据条目做归因；
  - **词元影响力**：遮挡法（occlusion）近似，给出"哪些输入词元把答案推向了这个方向"。
  - 相关内部实现：`split_sentences`、`_OverlapScorer`、`_LogprobScorer`。
- 因此：
  - **Phase 13（Answer Composer + Final Verifier）**的"每个事实声明绑定证据"
    直接复用证据解析的分解结果，不要另起一套声明切分；
  - **Phase 16（Observability UI）**的可解释性面板直接消费词元影响力结果，
    不做第二套近似；
  - 需要补的是：把归因结果**接进四图**（Evidence→Claim 边、Answer→Evidence 边）
    与 run 级 replay。

### 约束 5：作用域隔离——按 `scope` 收口，跨作用域取证据属于越权

> 契约用中性词 **`scope`（作用域键）**；**当前实现 = `industry_pack_id`**。
> 换成客户、部门、项目、产品线任何维度都成立，算法不得假设它一定是行业（见 §0.2）。

- 隔离维度：**`scope`**（内容层，当前实现 `industry_pack_id`）+ **`user_id`**（用户层）+ 会话/run。
- 作用域配置位于 `config/industry_packs/*.json`，运行时由 `industry_packs.py` /
  `industry_pack_runtime.py` 加载；多作用域归属由 `intel_sources.py`、
  `intel_source_industries` 表维护。
- **真实基线（Phase 00/01 必须记录，勿写死数量）**：
  - **数据库里有 10 个作用域在跑**：`financial_markets`、`invest_mgmt`、
    `automotive_industry`、`healthcare_news`、`education_news`、`family_office`、
    `ai_news`、`bolean_security_compute`、`short_video_news`、`embodied_ai`；
  - **`config/industry_packs/` 种子里只有 8 个业务包 JSON**（另有 `schema.json`
    为结构定义，不是业务包）——`invest_mgmt` 与 `embodied_ai` **没有种子文件，
    只能手工在后台维护**；
  - 这组"库里有、种子里没有"的差异正是**引擎与配置分离**要解决的问题：
    配置必须能从数据侧完整导出/导入，否则引擎无法在无种子的作用域上正常工作。
    修复方式属 Phase 01 的范畴（配置导出/导入接口 + 一致性巡检），
    具体落库方式**待确认**。
- **硬规则**：证据检索（`qa_retrieval.ArticleRetriever`）与记忆召回都必须带
  `scope` 收口；任何一次跨作用域命中都是**事故**，不只是质量问题。

### 约束 6（衔接说明）：客观趋势与反爬止损要接进规划，而不是各跑各的

- `trend_objective.py` / `tools/buzzing_radar.py` 给出的是**客观趋势**
  （词频、覆盖度、热度），属于 `TREND` 类证据来源，适用于约束 1 未达标时
  "没有三元组也能给出趋势结论"的场景。
- `antibot_detector.py` 的止损结论要进入 Execution Graph 的 `failure_policy`：
  某来源被判定为反爬封锁时，该 route 应为 `DEGRADE`/`FALLBACK`，**不能**把
  "没抓到"当成"没有发生"写进结论。

## 0.4 术语映射说明（原场景 → 中性概念，便于日后复核）

`00_REWRITE_SPEC.md` 第二节的对照表已覆盖主要替换，本文严格按其执行。
下表只补充**对照表没有列、而本文必须做取舍**的映射，左列用原文标识
（英文标识 / 段落位置）指代，避免在本文中重复出现原场景领域词：

| 原文标识 | 本文写法（**中性概念**，不引入任何具体行业词） | 映射理由 |
|---|---|---|
| 对照表"用户"行 | 用户（提问的分析师/运营），必要时细分"提问用户" | 对话主体 |
| 对照表"分析师/助手"行 | 分析师（人）或 AI 助手（系统侧） | 判读者 |
| 对照表"一次问答 run"行 | 一次问答 run（`qa_runs` 一条记录） | 会话边界 |
| 对照表"会话"行 | 会话（同一用户连续对话），跨会话 = 跨 run / 跨会话 | 记忆边界 |
| 对照表"内部结构化数据集"行 | 内部结构化数据集：`articles` 证据池 + `article_intel_classifications` + `intel_article_events` / `intel_subject_canonical` | 内部数据源 |
| 对照表"权威来源"行（原 guideline 行） | **权威来源**：正文一律只写"高 `authority_level` 来源"，**不列举任何具体来源类型**（具体清单由作用域配置的 `sources[]` 给出） | 权威证据 |
| 对照表"结论层"行（原 diagnosis 行） | **评估 / 结论**（不写"研判"等偏业务的说法） | 结论层 |
| 对照表"结论安全"行（原 clinical safety 行） | **结论安全**：不得给出无证据支撑的**高影响结论**；**哪些类别算"高影响"由作用域配置声明**，引擎不内置 | 安全 |
| `HIS` / `LIS` / `PACS` 类目 | **内部业务库**（具体是哪些表由作用域配置声明） | 结构化来源 |
| `clinical_risk` | **`conclusion_risk`**（高影响结论被证据推翻或不可回溯的风险） | 安全权重 |
| `red flag` 危险信号 | **`HighStakesFlag` 高影响标记**（触发条件由作用域配置给出，引擎只认布尔位） | 优先覆盖 |
| `Patient Burden` | **`UserBurden` 用户负担**（澄清追问次数与打断感） | 成本项 |
| `patient inquiry` / `ASK_PATIENT` | 向用户澄清 / **`ASK_USER`** | 动作类型 |
| `DIAGNOSTIC` 问题类型 | **`HIGH_STAKES_ASSESSMENT`**（是否需要更高证据门槛的高影响评估） | 意图分类 |
| `independent_clinical_evidence` 缺口类型 | **`independent_authoritative_evidence`**（"权威"由作用域配置的 `authority_level` 定义，引擎不认具体来源） | 缺口口径 |
| `Safety Verifier` 节点 | **`Scope & Compliance Verifier`**（来源合规 + 作用域隔离 + 无证据结论检查） | 校验层 |
| 原文的机构组织维度 | **作用域 / 子主题**（`scope`；当前实现 = `industry_pack_id`） | 组织维度 |
| 原文的机构合规条款 | **作用域隔离与来源合规**（按 `scope` 收口、许可证闸门、不得跨作用域取证据） | 合规 |

> 若后续复审发现仍有未映射的领域词，**在该处补一行映射说明**，不要静默替换。
> 本文正文中一律不出现原场景的领域词汇；上表左列只保留英文标识与段落位置。
> 同时按 §0.2：**引擎层也不得出现我们自己的具体行业词**——本文所有示例
> 一律用 `Entity_A` / `Scope_1` / `TimeWindow` 这类占位符，或明确标注
> "由作用域配置提供"的配置片段。

## 0.5 已交付能力与本文档的衔接总表

| 已交付 | 真实位置 | 本文档承接位置 |
|---|---|---|
| 一级检索（无 RAGFlow） | `qa_gateway.py`、`qa_retrieval.py`（`ArticleRetriever`） | §8 Retrieval Fleet、Phase 04 |
| 证据解析 / 词元影响力 | `qa_attribution.py`（`QaAttributionService`） | Phase 13 答案合成、Phase 16 可解释面板 |
| 引用校验 | `qa_pipeline.py` 的 `citation_validation` 节点、`qa_contracts.py` | §17 Final Verifier、Phase 13 |
| run 落库与回放 | `qa_storage.py`（`qa_runs`） | §25 数据库设计、§19 可 replay |
| 采集与分类 | `intel_worker.py`、`intel_light_scanner.py`、`intel_classifier.py`、`intel_topics.py` | §13 统一情报采集、Phase 14 |
| 发布时间采集与精度 | `publish_time.py` | §10 Freshness、约束 2 |
| 客观趋势 | `trend_objective.py`、`tools/buzzing_radar.py` | §6 缺口动作 `TREND` |
| 反爬止损 | `antibot_detector.py` | §28 Node Failure Policy |
| 来源许可证闸门 | `config/financial_source_licenses.json`（schema `financial-source-license-v1`）、源字段 `license_profile` | §14.1、Phase 15 |
| RAGFlow 增强检索 | `qa_ragflow_client.py`（B 机 `192.168.0.64:8106`） | §19 与检索基础设施结合 |

# 1. 四图架构

``` text
Execution Graph
  谁执行 / 并行 / 依赖 / Retry / Barrier / Cycle / Stop
       ↓ controls
Evidence Graph
  当前 Claim / Evidence / Gap / Contradiction
       ├──────────────┐
       ↓              ↓
Context Graph      Memory Graph
当前该看什么       跨会话保留什么
Context Pack       复用/失效/重验证
```

## 1.1 Execution Graph

负责 Node/Edge
contract、fan-out/fan-in、条件路由、timeout、retry、fallback、failure
isolation、cycle、budget 和 stop。只有真实数据依赖才连边。

## 1.2 Evidence Graph

表示当前研究会话的事实状态。节点：Question/SubQuestion/Claim/Entity/Evidence/Source/Gap/Contradiction；边：REQUIRES/SUPPORTS/REFUTES/MENTIONS/DERIVED_FROM/DEPENDS_ON/CONTRADICTS/RESOLVES。Claim
必须追溯到 Evidence→Source→Chunk→Span。

在本项目里，`Source` 节点额外承载三样东西（见 §0.3 约束 2、3）：
`scope`（**当前实现 = `industry_pack_id`**）、`admission_source`（`rule_gate` / `llm_semantic`）、
`published_at_utc` + `publish_precision`。

## 1.3 Context Graph

表示某个 Agent
当前真正需要看到的信息。节点：AgentTask/ContextItem/ClaimSummary/EvidenceDigest/Constraint/SkillInstruction/WorkingState/CounterEvidence；边：NEEDED_BY/SUPPORTS_TASK/CONSTRAINS/SUMMARIZES/COUNTERS/DERIVED_FROM。输出是受预算控制的
Context Pack，而不是完整 Research State。

## 1.4 Memory Graph

跨 Research Session
保存长期可复用知识与经验。节点：MemoryItem、VerifiedClaimMemory、EntityMemory、EpisodicResearchMemory、StrategyMemory、FailureMemory、QueryPatternMemory、SourceMemory、SkillPerformanceMemory、UserApprovedDomainRule。关系：ABOUT/DERIVED_FROM/VALIDATED_BY/SUPERSEDES/CONTRADICTS/EXPIRED_BY/HELPED_RESOLVE/FAILED_ON/APPLIES_TO。

所有 MemoryItem 的 `scope` 必须至少包含 `user_id` 与作用域键
（**当前实现 = `industry_pack_id`**，见 §14）。

# 2. Memory Graph 硬规则

## 2.1 Memory 不能直接成为当前事实证据

``` text
Memory Hint
→ freshness/source-version/applicability gate
→ 必要时重新检索
→ Verifier
→ 当前 Evidence Graph
→ Answer
```

其中 `applicability` 在本项目里首先检查：**同一 `scope`**、
同一 `user_id` 作用域、以及提问时的时间窗是否覆盖该记忆的有效期。

## 2.2 Memory Write Gate

``` text
Candidate Memory
  ↓
Memory Write Gate
  ├─ DROP
  ├─ SESSION_ONLY
  ├─ PERSIST
  └─ PERSIST_WITH_TTL
```

未经 Evidence Verifier 的 LLM 自由总结禁止写成 VerifiedClaimMemory。

## 2.3 生命周期

每条 Memory 至少保存
memory_id/type/content/entity_ids/source_evidence_ids/confidence/valid_from/valid_until/freshness_class/last_verified_at/status/superseded_by/reuse_count，以及
**scope_user_id / scope_key / scope_session_id**（**当前实现 `scope_key` = `industry_pack_id`**）。状态：ACTIVE/STALE/SUPERSEDED/CONTRADICTED/EXPIRED/REVOKED。旧
Memory 不物理覆盖，以版本关系保存演化。

# 3. Memory 类型

1.  **Verified Claim Memory**：高复用、已验证 Claim + Evidence
    指针，不保存模型"印象"。必须带 `scope`，跨作用域不可复用。
2.  **Episodic Research Memory**：问题类型、成功拆解、关键
    Gap、有效检索路径、停止原因。
3.  **Failure Memory**：无效 query、失败 route、false-positive
    source、语义漂移扩展；必须绑定
    corpus_version/retrieval_config_version/scope/time，环境变化后允许重试。
4.  **Strategy
    Memory**：某类问题哪些检索组合更有效；属于策略经验而非事实。
5.  **Source
    Memory**：authority/domain/update_frequency/reliability/known
    limitations。本项目还应记录：该来源是否曾有反爬封锁记录
    （与 `antibot_detector.py` 联通）、是否走 RSS/API/列表页、发布日期精度分布。
6.  **Skill Performance
    Memory**：skill_id/task_type/success_rate/latency/cost/evidence_yield，供
    Skill Router 学习。

# 4. Context Engineering

``` text
Persistent State
├─ Evidence Graph
├─ Memory Graph
└─ Execution State
       ↓
 Context Planner
       ↓
  Context Graph
       ↓
  Context Pack
       ↓
     Agent
```

统一 Context
Pack：system_context、task_context、evidence_context、counter_evidence、memory_context、skill_context、working_memory、constraints、budget。BM25
Hunter 不应看到完整 Evidence Graph；Answer Composer 主要看到 Verified
Claims、Primary/Counter Evidence、Unresolved Gaps 和 Citation Map。

`constraints` 必须显式携带：当前 `scope`（**当前实现 = `industry_pack_id`**）、
`user_id`、`TimeWindow`、以及（如涉及）允许的 `Entity` 标识白名单。
这样 Agent 无法"顺手"跨作用域取料。

# 5. Context Utility 与预算

``` text
ContextUtility(item, task) =
  Relevance × EvidenceStrength × TaskNecessity × Freshness × Diversity / TokenCost
```

Memory Item 再乘 MemoryConfidence × MemoryFreshness × **ScopeMatch**。
Context Planner 在 token budget 下选择最大价值集合，并保留输出/工具预算。
禁止再把固定 top_k 当成 Context 策略。

其中 `Freshness` 的计算必须读 `publish_precision`（见 §0.3 约束 2）：
`discovered` 精度的条目 Freshness 上限受限，不能因为"刚入库"就显得很新。

# 6. 三类缺口

**Evidence Gap**：当前 Evidence Graph
真正缺事实。动作：SEARCH/DB/INTERNAL_STRUCTURED/GRAPH/WEB/ASK_USER/TREND。

> 说明：原文中面向机构内部数据源的动作（见 `00_REWRITE_SPEC.md` 对照表第 5 行）
> 对应本项目的**内部结构化数据集**（`articles` 证据池 +
> `article_intel_classifications` + `intel_article_events` + `intel_subject_canonical`）；
> 新增 `TREND` 动作，对应 `trend_objective.py` / `tools/buzzing_radar.py` 的客观趋势，
> 用于结构化产物填充率不足时仍能给出趋势类回答（见 §0.3 约束 1、约束 6）。

**Context Gap**：证据已存在，但当前 Agent
没拿到、摘要不足或缺反证。动作：REPACK_CONTEXT/EXPAND_EVIDENCE_SPAN/LOAD_COUNTEREVIDENCE/LOAD_SKILL。Context
Gap 默认不得触发昂贵新检索。

**Memory
Opportunity**：判断历史是否有可复用经验，以及本轮结果是否值得持久化。动作：RECALL_MEMORY/REVALIDATE_MEMORY/WRITE_MEMORY/DO_NOT_PERSIST。

# 7. 三层 Planner

-   Research Planner：需要解决哪些 SubQuestion/Claim？
-   Context Planner：当前 Node 应看到哪些 Evidence/Claim/Skill/Memory？
-   Memory Planner：召回哪些 Memory？哪些结果值得保存？哪些旧 Memory 应
    stale/supersede？

职责禁止混合。

# 8. Skill Registry + Skill Router

## 8.1 正式契约：8 个数据源类别名（唯一权威，已收敛）

**本文的正式契约名一律用下面这 8 个「数据源类别名」**，与 `phases/PHASE_01` /
`PHASE_04` / `PHASE_07` 完全一致：

| 正式类别名（契约） | 含义 | 当前实现绑定（**非契约**，可替换） |
|---|---|---|
| `EVIDENCE_STORE` | 证据库检索 | `qa_retrieval.ArticleRetriever.retrieve` / `_rows`（证据池表 `articles`） |
| `SEMANTIC_INDEX` | 语义检索 | 向量索引 + 现役嵌入端点（绑定 bge-m3） |
| `EXTERNAL_AUGMENT` | 外部增强检索 | `qa_ragflow_client.QaRagflowResearchClient`（B 机 RAGFlow 数据集） |
| `GRAPH_TRAVERSAL` | 关系遍历 | `kg_nodes` / `kg_edges`（**未建**）+ `intel_article_events` / `intel_subject_canonical` |
| `STRUCTURED_STORE` | 结构化数据 | `article_intel_classifications`、`intel_article_events`、`intel_subject_canonical`、`article_ragflow_documents` |
| `PUBLIC_NETWORK` | 公开网络 | `qa_retrieval.WebSearchService`（`default_web_search_service`）+ `qa_security.validate_outbound_url` |
| `TREND_SIGNAL` | 趋势信号 | `trend_objective.objective_series` / `top_objective_terms`；`tools/buzzing_radar.py` 的 `hot_terms` / `radar_report` / `coverage_audit` |
| `QUERY_EXPANSION` | 检索式扩展 | `qa_research.QaResearchQueryPlanner.expand`、`qa_query_normalize.expand_terms` / `parse_time_window` |
| （编队调度） | fan-out / 超时 / 熔断 / 降级 | **无动作名**；`qa_resilience.QaPersistentResilience`、`orchestrator` 的 `DEGRADABLE_STAGES` |

**收敛理由（写进规则）**：两套术语并存时，长程智能体在两份文档之间执行要反复翻译，
必然漂移；而且**类别名比 Hunter 名更中性**——`BM25` 是实现算法名，
`EVIDENCE_STORE` 是数据源类别，后者才符合"**引擎不绑定具体实现**"的原则
（见 §0.2）。所以契约以类别名为准，Hunter 名退化为**当前实现绑定的通俗叫法**。

> **硬规则**：**Hunter 名仅为当前实现绑定的通俗叫法，不得作为契约字段或 Gate 判据。**
> 任何 `route` / `category` / 契约枚举 / Gate 验收项一律写 8 个类别名。

### 8.1.1 Hunter 名 → 正式类别名 映射表（本文核实结果）

| 本文旧写法（Hunter 名） | 正式类别名 | 核实依据 |
|---|---|---|
| `BM25 Hunter` | `EVIDENCE_STORE` | 原文 §8.1 是"关键词/精确短语匹配"，即证据库检索；阶段文档 P04-01 标题即 `BM25 Hunter（EVIDENCE_STORE：证据库检索）` |
| `Semantic Hunter` | `SEMANTIC_INDEX` | 原文 §8.2 明确绑定 `bge-m3`；阶段文档 P04-02 标题即 `Semantic Hunter（SEMANTIC_INDEX）` |
| `Graph Hunter` | `GRAPH_TRAVERSAL` | 原文 §8.3 是 `Entity → Relation → Entity → Evidence`；阶段文档 P04-03 标题为 `Graph Hunter adapter（EXTERNAL_AUGMENT + GRAPH_TRAVERSAL 两类共用的 adapter 位）` |
| `Structured Hunter` | `STRUCTURED_STORE` | 原文 §8.6 面向 SQL / API / 内部结构化数据集；阶段文档 ⑤ `STRUCTURED_STORE` |
| `Query Expansion Hunter` | `QUERY_EXPANSION` | 原文 §8.5 只生成同义/缩写/上下位概念，不改语义；阶段文档 P04-05 |
| 趋势/热榜那一路（本文 §6 的 `TREND`） | `TREND_SIGNAL` | 阶段文档 P04-04 子路 4c；**元事实，非断言** |
| 公网检索那一路（本文 §8 的 `web_search/`） | `PUBLIC_NETWORK` | 阶段文档 P04-04 子路 4b |
| 外部知识库那一路（本文 §19 的 RAGFlow） | `EXTERNAL_AUGMENT` | 阶段文档 ③ `EXTERNAL_AUGMENT`（绑定 RAGFlow） |
| `Metadata Hunter` | **无独立类别** | ⚠️ 见下方差异项 D-4：阶段文档里没有独立的元数据类别，其过滤动作分散在 `EVIDENCE_STORE`（`ArticleRetriever._rows`）与 `SEMANTIC_INDEX`（P04-02 的"元数据前置过滤"）两处。**本文不擅自归口**，按差异项上报 |

> **差异项 D-4（只登记、不改阶段文档）**：原文 §8.4 `Metadata Hunter` 在 8 类里没有
> 对应项。若收敛时决定保留独立的元数据类别（如 `METADATA_FILTER`），需由阶段文档侧确认；
> 本文暂**不**把它写进契约，只在需求出现时按 `scope` / `TimeWindow` / 文档类型
> 作为**前置过滤参数**下发给 `EVIDENCE_STORE` 与 `SEMANTIC_INDEX`。

## 8.2 实现侧目录（`skills/`，非契约）

下面这棵树是**实现侧目录命名**，保留下来只为说明"当前这些技能由哪些目录/模块承担"。
**它不是契约**：契约与 Gate 一律用 §8.1 的类别名；收口时以类别名为准。

``` text
skills/
├── bm25_search/                     # → EVIDENCE_STORE
├── semantic_search/                 # → SEMANTIC_INDEX
├── graph_traversal/                 # → GRAPH_TRAVERSAL
├── sql_query/                       # → STRUCTURED_STORE
├── internal_structured_search/      # → STRUCTURED_STORE（查 articles / 分类 / 事件 / 实体归一）
├── trend_radar/                     # → TREND_SIGNAL（trend_objective + buzzing_radar）
├── web_search/                      # → PUBLIC_NETWORK
├── causal_reasoning/                # 非检索类技能：推理辅助
├── authority_evidence/              # 非检索类技能：按作用域配置的 authority_level 取权威证据
├── contradiction_resolution/        # 非检索类技能：冲突消解
├── citation_verification/           # 非检索类技能：引用校验
├── source_compliance_check/         # 非检索类技能：许可证/来源合规闸门（见 §14.1，字段 license_profile）
└── user_clarification/              # → ASK_USER（向用户澄清；ASK_USER 由 Phase 14 定义）
```

每个 Skill 声明
skill_id/description/**category**/input_schema/output_schema/preconditions/cost_class/latency_class/permissions/version。
其中 **`category` 必须取 §8.1 的 8 个类别名之一**（非检索类技能标 `NON_RETRIEVAL`）。
Skill Router 根据 task_type + gap_type + historical skill performance +
budget + permissions 选择最小必要集合；Skill 指令按需加载。

`permissions` 必须包含允许的 `scope` 列表：Skill 不得在未被授权的
作用域上执行（**当前实现 = 允许的 `industry_pack_id` 列表**）。

# 9. V2.0 主执行流

``` text
USER QUESTION
 ↓
Query Interpreter
 ↓
Planning Memory Recall
 ↓
Research Planner
 ↓
Execution Graph
 ↓
Gap/Task → Skill Router
 ↓
EVIDENCE_STORE | SEMANTIC_INDEX | GRAPH_TRAVERSAL | STRUCTURED_STORE
| PUBLIC_NETWORK | TREND_SIGNAL | ASK_USER   (parallel)
（契约名见 §8.1；`EXTERNAL_AUGMENT` / `QUERY_EXPANSION` 视配置可用性加入）
 ↓
Evidence Normalizer → Edge Verifier → Evidence Graph
 ↓
Contradiction + Gap Analyzer
 ├─ Evidence Gap → Next-hop Plan ─┐
 ├─ Context Gap  → Context Plan  ─┤→ loop
 └─ Answerable                    │
                                  ↓
                         Context Pack Builder
                                  ↓
                           Answer Composer
                                  ↓
                            Final Verifier
                                  ↓
                             Final Answer
                                  ↓
                           Memory Write Gate
                                  ↓
                             Memory Graph
```

对应现役实现：`qa_gateway.py`（入口/配额/降级）→ `qa_retrieval.py`
（`ArticleRetriever` 取证）→ 合成 → `qa_pipeline.py` 的 `citation_validation`
→ `qa_storage.py` 落库（`qa_runs`）。V2.0 的改造是在这条真实链路上**加层**，
不是另建一条平行链路。

# 10. Memory Recall 与 Revalidation

Planning Recall 在 Research Planner 前优先召回
Strategy/Failure/QueryPattern Memory，避免旧事实污染规划。Evidence
Recall 在 Gap 出现后可召回 Verified Claim/Source Memory，但先标记
MEMORY_HINT，经 Freshness Gate + Verifier 后才可进入当前 Evidence
Graph。

Freshness 分级**由作用域配置提供**，引擎只认分级本身：

``` text
时效分级（抽象）          典型语义
LONG                     长期稳定，不因时间失效
VERSION_SENSITIVE        随版本/口径变化，版本变了就要 revalidate
SHORT                    数周内有效
VERY_SHORT               数天内有效（发布当刻价值最高，之后只作背景）
RUN_BOUND / SESSION_BOUND 只对当前 run / 会话成立
```

> 配置实例（由作用域配置的 `freshness_policy` 声明，**引擎不内置**）：
> 例如某作用域把"高 `authority_level` 来源原文"标为 `VERY_SHORT`、"标准/规范类"标为
> `VERSION_SENSITIVE`、"产品/接口说明"标为 `SHORT`、
> "快变指标（价格/库存类）"标为 `VERY_SHORT`、
> "用户本次会话的关注点 / 当前 run 的约束（`Scope`、`TimeWindow`、实体标识）"
> 标为 `RUN_BOUND`。

硬规则：status!=ACTIVE 不作证据；source_version_changed 必须
revalidate；freshness_required 必须 revalidate；high_stakes 默认
revalidate；**`published_at_utc` 为空或精度为 `discovered` 的高时效问题，
默认 revalidate**。

# 11. Memory 冲突、Seen 与防污染

M1 支持 C、M2 反驳 C 时建立
MemoryContradiction，禁止"最新自动覆盖"。新证据确认 M2
后：M1.status=SUPERSEDED，建立 M1--SUPERSEDED_BY→M2。

维护三层集合：seen（本轮见过）、confirmed（本轮 verifier
通过）、remembered（经 Write Gate 跨会话保留）。不是所有 confirmed
都值得 remembered。

``` text
MemoryWriteUtility =
  ReuseProbability × Confidence × Stability × InformationValue
  - TenantLeakRisk - StalenessRisk - DuplicationPenalty

MemoryRecallScore =
  SemanticRelevance × TaskApplicability × Confidence × Freshness × HistoricalUtility
  - ContradictionRisk - ScopeMismatchPenalty
```

> 映射说明：原文的 `PrivacyRisk` 在本项目中即 **TenantLeakRisk**
> （跨作用域/跨用户泄露风险），并额外显式加入 `ScopeMismatchPenalty`
> （作用域不匹配惩罚），使 scope 不匹配的记忆天然排在后面。

防污染：未验证自由生成内容不得成为事实 Memory；必须保存
provenance；高风险事实重新验证；外部网页指令不得升级为系统规则；写入/失效/覆盖全部
audit；支持按 source/entity/session/**industry_pack** 撤销污染 Memory。

# 12. Memory Graph 数据库

新增：memory_item、memory_version、memory_entity_link、memory_evidence_link、memory_relation、memory_recall_log、memory_write_decision、memory_validation、memory_contradiction、memory_usage_stat、skill_performance_memory、source_reliability_memory。

memory_item
至少：id/memory_type/canonical_content/confidence/freshness_class/valid_from/valid_until/last_verified_at/status/scope/scope_user_id/scope_key/scope_session_id/created_from_session_id/created_from_run_id/created_at（**当前实现 `scope_key` = `industry_pack_id`**）。

> 说明：本项目的一次问答 run 落在 `qa_runs`，因此用 `created_from_run_id`
> 关联 run，`created_from_session_id` 关联同一用户的连续会话（`chat_history` 维度）。

禁止只用一个向量库保存全部 Memory。推荐：Relational metadata + vector
index + graph relations + source/evidence provenance。

向量索引必须带 `scope` 过滤条件（当前实现 = `industry_pack_id`），且**过滤在检索阶段生效**，
不允许"先跨包召回再过滤"。

# 13. 统一情报采集四图统一（原 SAGE-RAG 章节改写）

``` text
User Dialogue（对话框提问）
 ↓
State Interpreter            （识别意图、Scope、TimeWindow、Entity）
 ↓
Research Planner
 ↓
Evidence Graph ← Memory Hint/Revalidation
 ↓
Gap Analyzer
 ├─ Evidence Gap → KB/Internal DB/Web/Ask User/Trend
 ├─ Context Gap  → Context Pack 重组
 └─ High-Stakes Gap → Priority Override（高影响标记覆盖成本约束）
 ↓
Reasoner → Final Verifier
```

Evidence Acquisition
Action：retrieve_document / retrieve_internal_structured / graph_traverse / query_database / trend_radar / web_search / ask_user。

``` text
Utility(action) =
 ExpectedGapReduction × TaskImportance × EvidenceReliability
 / (Latency + Cost + UserBurden)
```

本项目在 `TaskImportance` 上追加三个**中性**权重：
`conclusion_risk`（高影响结论风险：无证据即给出高影响结论的风险）、
`scope_safety_weight`（作用域隔离与来源合规权重）、
`discriminative_power`（该动作区分两种评估/结论的能力）。

`HighStakesFlag = true`（命中作用域配置声明的 `high_stakes_patterns`）时，
安全规则覆盖普通成本效用。**判定模式来自配置，引擎只认布尔位**：

``` text
high_stakes_patterns:      # 作用域配置项，示例形态（非引擎内置）
  - pattern_id: HS_1
    match: <关系/事件类型 + 主体条件>
    action: PRIORITY_OVERRIDE
```

关键收益：若信息已在 Evidence Graph 中但只是 Context
Gap，系统应重组上下文而不是再次追问用户，从而减少无意义追问。

> 映射说明：原文这一节的 `SAGE-RAG 统一采集` 在本项目中落地为
> **统一情报采集**：证据池(KB) / 内部结构化数据(DB) / 公网(WEB) /
> 向用户澄清(ASK_USER) / 趋势与热榜(TREND)，对应 Phase 14。

# 14. 记忆边界（Memory Scope）与作用域隔离

scope 取值（引擎层的中性枚举）：`GLOBAL_KNOWLEDGE`（全平台公开通用知识）/
`ORGANIZATION`（全组织通用规则，跨作用域）/
`SCOPE`（**作用域级**：只对某一个作用域成立；**当前实现 = 单个 `industry_pack_id`**）/
`USER_LONGITUDINAL`（某用户长期偏好与关注）/
`RUN`（当前 run 的约束）/ `SESSION`。

- 公开通用知识可为 `GLOBAL_KNOWLEDGE`（哪些来源够格，由作用域配置声明）；
- 本项目内部的分类口径、信源权威等级、单包规则为 `SCOPE`；
- 用户长期关注（长期跟踪的实体、子主题、关注时间窗）可为 `USER_LONGITUDINAL`，
  但使用时必须检查时间与有效期；
- 本次会话的关注点、当前 run 的约束（`Scope`、`TimeWindow`、实体标识）只能 `RUN`/`SESSION`；
- 某轮检索失败为 `SESSION` 或带 corpus/version 的 `EPISODIC`。

**硬约束（越权即事故）**：

1. 任何 `SCOPE` / `USER_LONGITUDINAL` / `RUN` / `SESSION` 作用域的记忆，
   都不得跨 `scope` 复用（当前实现：不得跨 `industry_pack_id`）；
2. 任何记忆不得跨 `user_id` 召回，除非作用域明确为 `GLOBAL_KNOWLEDGE` 或
   `ORGANIZATION`，且内容本身不含用户私有上下文；
3. 用户当前会话的关注点（`Scope`、`TimeWindow`、实体标识）**不得无条件跨 run**
   当成当前事实；
4. 记忆的写入、召回、失效、撤销都要留 audit，并支持按
   `user_id` / `scope` / source / entity 撤销。

合规映射：原文的机构类合规条款（见 `00_REWRITE_SPEC.md` 对照表最后一行）在本项目中即
**作用域隔离与来源合规**——按 `scope` 收口、来源许可证闸门、不得跨作用域取证据。

### 14.1 来源许可证闸门（已实测确认，Phase 15 按此实现）

- **字段名**：信源侧就是 **`license_profile`**（在信源 metadata / 行业包 JSON 的
  每个源里都有）。
- **真身文件**：`config/financial_source_licenses.json`，
  schema 为 **`financial-source-license-v1`**。
- **必填字段（逐字）**：

``` text
source_type / source_id / owner_role / legal_review_owner_role /
cost_type / billing_owner_role / credential /
permissions{cache, display, redistribute} /
quota / reviewed_on / review_due_on /
authorization_status / production_allowed /
production_approval_required / evidence_urls
```

  > **计数口径**：按 `financial_source_license.py` 的 `REQUIRED_PROFILE_FIELDS`
  > 实测核对为 **15 个顶层必填字段**（早前文档里的"14 个"是计数失误，本包一律以 15 为准）。字段本身无遗漏。
  > `permissions` 是嵌套对象，内部要求 `cache` / `display` / `redistribute`
  > 三项齐全（`set(permissions) != {cache, display, redistribute}` 即校验失败）。

- **强制范围（关键）**：**只对 `origin_pack_id == 'financial_markets'` 的 RSS 源生效**。
  其它作用域、其它来源类型**根本不检查**。
- **未登记或复核过期**：**扫描时直接跳过，连请求都不发**。
- **因此不得写成"字段缺失即视为受限（fail-closed）"**——那会误伤公开源。
  正确口径是：**仅对 `origin_pack_id='financial_markets'` 的 RSS 源做许可证校验，
  未登记即拒绝（与现状一致）；其余来源按公开处理。**
- 相关实现：`intel_sources.py` 与 `config/industry_packs/*.json` 的源定义。

# 15. 可观测性 UI

必须提供四个视图：Execution Graph View、Evidence Graph View、Context
Inspector、Memory Graph View。

Context Inspector 显示：为何加入、token 数、来自 Evidence 还是
Memory、哪些 item 被预算挤出、是否 revalidated。

Memory View
显示：类型、来源、confidence、freshness、last_verified、reuse_count、status、superseded
chain，以及 **scope（user/pack/session）**。

Search Trace
新增：memory_recalled/memory_revalidated/context_items_selected/context_items_dropped/context_tokens/skills_loaded、`scope`、
`admission_source`、`publish_precision`。

UI 落地位置：与现役 `qa_runs` / 归因抽屉对齐，直接复用
`qa_attribution.py` 的证据解析与词元影响力结果（见 §0.3 约束 4）。

# 16. V2.0 新增评价指标

Context：Context Precision、Context Recall、Useful Context
Density、Context Token Efficiency、Counter-evidence
Coverage、Context-gap False Retrieval Rate。

Memory：Memory Hit Utility、Stale Memory Rate、Revalidation Pass
Rate、Memory Pollution Rate、Memory Reuse Gain、Failure Repetition
Rate、Supersession Accuracy、**Cross-Scope Leak Count（必须为 0）**、
**Cross-User Leak Count（必须为 0）**。

系统级：Answer Quality、Evidence Recall、Unsupported Claim
Rate、Latency、Cost、Rounds、Queries、**User Burden（澄清追问次数/打扰度）**。
必须比较启用/禁用 Context Graph 与 Memory Graph 的 ablation。

榜单分层必须按 `scope` 分别统计：单作用域高分不能掩盖另一作用域的空转
（尤其在事件填充率为 0 的包上）。

# 17. V2.0 新增实施阶段

> **编号说明（必读）**：本节沿用原 V2.0 文档的编号（Phase 11–17）。
> 本开发包的**权威 18 阶段**是 `02_PHASE_OVERVIEW.md` 与 `phases/` 下的
> Phase 00–17，名称如下（与本节为并行视角，不是替代关系）：
>
> | 权威 Phase（`phases/`） | 名称 |
> |---|---|
> | Phase 00 | PROJECT_DISCOVERY_BASELINE |
> | Phase 01 | CONTRACTS_RESEARCH_STATE |
> | Phase 02 | EVIDENCE_LAYER |
> | Phase 03 | VERIFIER_LAYER |
> | Phase 04 | PARALLEL_RETRIEVAL_FLEET |
> | Phase 05 | RESEARCH_PLANNER_EXECUTION_GRAPH |
> | Phase 06 | EVIDENCE_GRAPH_CONTRADICTION |
> | Phase 07 | GAP_ANALYZER_DYNAMIC_MULTI_HOP |
> | Phase 08 | CONTEXT_GRAPH_CONTEXT_PLANNER |
> | Phase 09 | MEMORY_GRAPH_CORE |
> | Phase 10 | REVALIDATION_MEMORY_CONFLICT |
> | Phase 11 | SKILL_REGISTRY_ROUTER |
> | Phase 12 | EPISODIC_FAILURE_STRATEGY_MEMORY |
> | Phase 13 | ANSWER_COMPOSER_FINAL_VERIFIER |
> | Phase 14 | 统一情报采集（原 SAGE_RAG_UNIFIED_ACQUISITION） |
> | Phase 15 | **记忆安全与作用域隔离**（Memory Safety & Scope Isolation；原 CLINICAL_MEMORY_SAFETY 改名） |
> | Phase 16 | OBSERVABILITY_UI |
> | Phase 17 | E2E_ABLATION_RELEASE |
>
> 两套编号的对应关系见本文末尾附录 A 的 §32 映射表。**Gate 规则不变**：
> 当前 Phase Must 未全 PASS，不进入依赖它的下一 Phase。
> **「两套编号并存」是已定决策，不合并**（保留原文结构 + 映射表）。
>
> **Phase 01 的 Must 必须包含 §0.2 的「作用域配置接口」契约（`ScopeConfig`）**：
> 规定配置单元需要提供什么（关键词/锚点、信源与权威级别、关系类型、时效策略、
> 实体词表、分类权重），以及"引擎层零行业词"的静态校验。
> 缺了这条，后面所有阶段都会把行业耦合重新焊回引擎。
> 同时必须落地 §0.2.1 的硬边界：**契约用 `scope`，实现用 `industry_pack_id`，
> 两者之间只允许一处映射函数**。
>
> ---
>
> **⚠️ 术语收口状态（第 5 条决策已采纳并执行）**：本文件的 **§8 已收敛**——
> 契约一律用 8 个数据源类别名（`EVIDENCE_STORE` / `SEMANTIC_INDEX` /
> `EXTERNAL_AUGMENT` / `GRAPH_TRAVERSAL` / `STRUCTURED_STORE` / `PUBLIC_NETWORK` /
> `TREND_SIGNAL` / `QUERY_EXPANSION`，编队调度无动作名），Hunter 名退化为
> **实现侧通俗叫法**，映射表见 §8.1.1。理由：两套术语并存会迫使长程智能体
> 在两份文档间反复翻译而漂移；且类别名比实现算法名更中性。
> **Hunter 名不得作为契约字段或 Gate 判据。**
>
> **阶段文档侧仍有不一致（只登记，本文件不改那两份，由 B / C 各自收口）**：
>
> | 编号 | 位置 | 与 8 类不一致的写法 | 建议口径 |
> |---|---|---|---|
> | D-1 | `phases/PHASE_14_*.md`（动作表 + `P14-03` 标题） | 用 5 类动作名 `EVIDENCE_POOL` / `STRUCTURED_DATA` / `PUBLIC_WEB` / `TREND` / `ASK_USER` | 前四个对应 `EVIDENCE_STORE` / `STRUCTURED_STORE` / `PUBLIC_NETWORK` / `TREND_SIGNAL`；`ASK_USER` 是 8 类之外的动作（Phase 14 自有），需明确它属于**动作**而非**检索类别** |
> | D-2 | `phases/PHASE_14_*.md` 第 31 行注 | 称 RAGFlow 增强检索"**不是动作类型**"，而 `PHASE_04` ③ 把它绑到 `EXTERNAL_AUGMENT` | **同一实现的归类冲突**，需二选一（本文按 `PHASE_04` 记为 `EXTERNAL_AUGMENT`） |
> | D-3 | `phases/PHASE_02_*.md` | 全小写 `public_network` / `trend_signal` | 与 UPPER_SNAKE 类别名大小写不一致 |
> | D-4 | 原文 §8.4 `Metadata Hunter` | 8 类里**无独立元数据类别**（过滤分散在 `EVIDENCE_STORE._rows` 与 `SEMANTIC_INDEX` 的"元数据前置过滤"） | 要么并入上述两类，要么新增 `METADATA_FILTER`；**本文暂不写进契约** |
> | D-5 | 原文 §4 总体架构图 | 图中有 `Specialist Hunter` | 8 类里无对应项；本文已在 §8.1.1 说明其为通用位，不再作为类别 |
> | D-6 | `phases/PHASE_05_*.md` `_CATEGORY_RULES` | 场景类别仍写死在引擎里（该文件已标注为待处理） | 与第 5 条决策一致：**暂不迁，标注但不改语义**，等基线跑完再谈 |
>
> 以上 D-1 ~ D-6 请统一广播；**本文件不擅自改阶段文档**。
>
> 收口原则（供统一时参照）：**引擎层动作名一律 UPPER_SNAKE 的数据源类别名**
> （即 `phases/PHASE_01` 定义的那一套），技能目录名作为其**实现侧目录**，
> 两者之间同样只允许一处映射表。

## Phase 11 --- Context Graph

-   [ ] ContextItem schema
-   [ ] Context Planner
-   [ ] Context Utility
-   [ ] Token Budget
-   [ ] Counter-evidence reservation
-   [ ] Context Pack Builder
-   [ ] Context Inspector UI
-   [ ] Context Gap 检测
-   [ ] constraints 携带 `scope`（当前实现 = `industry_pack_id`）/ `user_id` / `TimeWindow`（新增）

验收：同一任务不再把完整 Research State 塞给所有
Agent；在答案质量不下降前提下降低无关 context/token；Context Gap
不误触发新检索。

## Phase 12 --- Skill Registry

-   [ ] Skill schema/version
-   [ ] Skill Router
-   [ ] permission/cost/latency metadata（permissions 含允许的 `scope`）
-   [ ] on-demand skill context
-   [ ] Skill performance telemetry

## Phase 13 --- Memory Graph Core

-   [ ] Memory schema/relations（含 scope_user_id / scope_key）
-   [ ] Write Gate
-   [ ] Recall
-   [ ] provenance
-   [ ] TTL/freshness（读 `publish_precision`）
-   [ ] status lifecycle
-   [ ] vector+graph+relational retrieval

## Phase 14 --- Revalidation & Conflict

-   [ ] source-version detection
-   [ ] freshness gate
-   [ ] high-stakes revalidation
-   [ ] contradiction
-   [ ] supersession chain
-   [ ] polluted-memory revoke

## Phase 15 --- Episodic/Failure/Strategy Memory

-   [ ] Research episode summarizer（只能总结 trace，不可制造事实）
-   [ ] Failure Memory
-   [ ] Strategy Memory
-   [ ] Source reliability（含反爬封锁记录）
-   [ ] Skill performance memory
-   [ ] corpus/config version-aware retry

## Phase 16 --- 记忆安全与作用域隔离（原 SAGE-RAG Memory Safety）

> **名称统一（必须与 `tracking/IMPLEMENTATION_STATUS.md`、`ACCEPTANCE_MATRIX.md`、
> `reference/METRICS.md`、`schemas/CONTRACT_CHECKLIST.md` 完全一致）**：
> 本节在 V2.0 编号下叫 Phase 16，在权威 18 阶段编号里是 **Phase 15
> 「记忆安全与作用域隔离」（Memory Safety & Scope Isolation）**。
> 全文规则句一律用中性的 `scope`：**不得跨 `scope`、不得跨用户泄露**；
> 早期文档的"租户隔离"是同一维度的旧称，**不再使用**。
> 需要解释时只允许一处说明：**当前实现 = 按 `industry_pack_id` 作用域隔离**。

-   [ ] scope enforcement（作用域隔离：**不得跨 `scope` 取证据**，当前实现 = 不得跨 `industry_pack_id`）
-   [ ] run boundary
-   [ ] cross-scope / cross-user leak 检测与阻断
-   [ ] user-longitudinal freshness
-   [ ] conclusion-safety override（高影响标记优先）
-   [ ] **来源许可证闸门（见 §14.1）**：`license_profile` 字段透传；
      仅对 `origin_pack_id == 'financial_markets'` 的 RSS 源校验
      （`config/financial_source_licenses.json` / schema `financial-source-license-v1`）；
      未登记或复核过期 → 扫描时直接跳过、不发请求；**其余来源按公开处理，不得 fail-closed**
-   [ ] **作用域过滤只允许一处实现**（一个函数 / 一个 repository 方法，禁止各调用点自己拼 SQL）
-   [ ] audit/replay

**Gate（Phase 15 Gate = `ACCEPTANCE_MATRIX.md` 的 A15，措辞必须一致）**：

``` text
Phase 15 Gate：记忆安全与作用域隔离
验收口径：
  1. 跨用户 / 跨 scope 泄露 = 0；
  2. 许可校验范围严格等于配置声明的作用域（不多不少）；
  3. 审计覆盖率 100%。
未全 PASS，不得进入依赖它的下一 Phase（Gate 规则见 §17 与 MASTER_RULES 第 20 条）。
```

## Phase 17 --- V2 Ablation

比较：V1 baseline；+Context Graph；+Memory Recall；+Failure
Memory；+Strategy Memory；完整四图。至少报告质量、Evidence
Recall、Unsupported Claim、Context Tokens、Latency、Cost、Query
数、Failure Repetition、Stale Memory Rate、**User Burden（澄清追问次数）**、
**Cross-Scope Leak Count**。所有对比按 `scope` 分层给出。

# 18. V2.0 核心伪代码

``` python
def research_v2(question, user_id, scope, session, budget):
    # scope = 作用域键；当前实现即 industry_pack_id。引擎不假设它是行业。
    state = init_state(question, user_id, scope, session, budget)
    interpretation = interpret_query(question)

    planning_memory = memory_recall(
        interpretation,
        scope={"user_id": user_id, "scope_key": scope},
        types=["STRATEGY", "FAILURE", "QUERY_PATTERN"]
    )
    plan = research_planner(interpretation, planning_memory)

    while not state.should_stop():
        tasks = select_ready_tasks(state)
        skills = skill_router(tasks, state.skill_performance, budget)
        results = run_parallel(tasks, skills, scope=scope)

        candidates = normalize(results)
        fresh = dedupe_against_seen(candidates, state.seen)
        state.mark_seen(fresh)
        verified = verify_parallel(fresh)
        state.evidence_graph.merge(verified)

        contradictions = detect_contradictions(state.evidence_graph)
        gaps = analyze_gaps(state.evidence_graph, contradictions)

        for gap in gaps:
            if gap.type == "CONTEXT_GAP":
                state.enqueue(repack_context(gap))
            elif gap.type == "EVIDENCE_GAP":
                hints = recall_relevant_memory(gap)          # 带 scope 过滤
                valid_hints = revalidate_if_needed(hints)
                state.enqueue(plan_acquisition(gap, valid_hints))

        if is_answerable(state):
            break

    pack = context_planner.build_pack(
        task="ANSWER",
        evidence_graph=state.evidence_graph,
        memory_hints=state.validated_memory,
        constraints={
            "scope_key": scope,
            "user_id": user_id,
        },
        token_budget=budget.context_tokens
    )
    draft = compose_answer(pack)
    answer = final_verify_and_revise(draft, state.evidence_graph)

    candidates = memory_planner.extract_candidates(state, answer)
    memory_write_gate(candidates)                            # 强制写 scope
    return answer
```

# 19. V2.0 Definition of Done

-   [ ] 四图均有独立数据模型和可视化。
-   [ ] Agent 不再共享巨型 prompt/state。
-   [ ] Context Pack 可解释、可复现。
-   [ ] Evidence Gap 与 Context Gap 可区分。
-   [ ] Memory 不能绕过 Verifier 成为当前事实。
-   [ ] Memory 有 provenance、TTL、version、status、supersession。
-   [ ] 能记住失败路径且 corpus/config 更新后允许重新尝试。
-   [ ] Skill 按需加载并记录效果。
-   [ ] **`user_id` / `scope` / run 三层严格隔离，跨作用域越权与跨用户泄露计数为 0。**
-   [ ] 可 replay 任意回答：执行→证据→上下文→记忆。
-   [ ] V2 ablation 证明新增层带来质量/效率/重复失败率中的可测收益，且收益按 `scope` 分别成立。
-   [ ] **在事件三元组填充率仍为 0.0% 的包上，系统必须显式降级
-        （走 `TREND` 与文本检索路径），不得给出图推理结论。**

# 20. V2.0 最终目标

系统不再只是"会调用很多检索工具的 LLM"，而是：

> **一个拥有执行状态、当前证据状态、任务上下文状态和长期记忆状态的研究系统；它知道下一步应该获取什么证据、把什么送给哪个
> Agent、哪些历史经验值得复用、哪些旧知识必须重新验证，并知道什么时候停止。**

对于一个行业情报场景，最终形态是：

**Agentic Evidence + Context + Memory Acquisition Engine**——
用户在对话框里问"某个行业/某只票最近有什么关键变化"，
系统从证据图取已入库情报、从内部结构化数据集取分类与事件、
从趋势雷达取客观热度、必要时才向用户澄清，
每一步都过 Verifier，最终答案只用**已验证证据**，每条结论都能点回原文。

------------------------------------------------------------------------

# 附录 A：V1.0 完整基线（未被 V2.0 明确替代的条款继续有效）

# Graph-RAG Agent：多跳智能检索改造方案 V1.0（行业情报版）

> 目标：把传统"Query → Top-K → LLM"的 RAG
> 改造成能够规划、并行搜索、建立证据图、验证、发现缺口、自适应继续检索，并在证据充分后回答的智能检索系统。\
> 本文结合《Graph Engineering: Stop Chaining Your
> Agents》的图式编排思想，并针对 RAG / 统一情报采集 /
> 向用户澄清的问答场景做工程化扩展。
>
> 现役落点：`qa_gateway.py` → `qa_retrieval.py`（`ArticleRetriever`）→
> 合成 → `qa_pipeline.py` 的 `citation_validation` 节点 → `qa_storage.py`（`qa_runs`）。

------------------------------------------------------------------------

# 1. 核心思想

传统 Multi-hop RAG 常写成：

``` text
Question
  ↓
Search 1
  ↓
Rewrite
  ↓
Search 2
  ↓
Rewrite
  ↓
Search 3
  ↓
Answer
```

这类设计的问题是：

1.  跳数提前写死；
2.  每一步都依赖上一步，容易形成无意义串行；
3.  缺少显式证据状态；
4.  缺少验证器；
5.  不知道为什么继续检索；
6.  不知道什么时候停止；
7.  一个节点失败可能拖垮整条链；
8.  容易重复检索已经失败或已经见过的证据。

改造后的目标结构：

``` text
Question
  ↓
Query Interpreter
  ↓
Research Planner
  ↓
并行 Evidence Hunters
  ↓
Evidence Verifier
  ↓
Evidence Graph
  ↓
Gap Analyzer
  ├─ 仍有高优先级缺口
  │      ↓
  │   Dynamic Next-hop Planner
  │      ↓
  │   并行继续检索
  │
  └─ 证据充分 / 达到预算
         ↓
    Answer Composer
         ↓
    Final Verifier
         ↓
       Answer
```

核心变化：

> **检索的控制变量不再是"第几跳"，而是"当前还缺什么证据"。**

------------------------------------------------------------------------

# 2. 从 Graph Engineering 借鉴的原则

原文中最值得迁移到 RAG 的不是"多 Agent 数量"，而是图式编排本身。

## 2.1 Node 是任务，Edge 是真实依赖

如果 B 不读取 A 的结果，就不应该写成：

``` text
A → B
```

而应该：

``` text
A
   → Merge
 /
B
```

也就是并行。

------------------------------------------------------------------------

## 2.2 每个 Node 必须有 Contract

每个节点只负责一个明确任务：

``` yaml
node_id:
purpose:
input_schema:
output_schema:
timeout:
retry:
model_tier:
allowed_tools:
validation:
failure_policy:
```

例如：

``` yaml
node_id: evidence_hunter
purpose: 搜索一个明确证据缺口
input:
  gap_id:
  query:
  entities:
  source_constraints:
  scope:                   # 必填：作用域键（当前实现 = industry_pack_id）
output:
  evidence_candidates:
  unresolved:
  search_trace:
```

禁止节点一边搜索、一边验证、一边回答。

------------------------------------------------------------------------

## 2.3 Edge 也是数据 Contract

Edge 不是：

``` text
“然后执行下一步”
```

而是：

``` text
A 输出什么结构
B 需要什么结构
```

例如：

``` text
Retriever
   |
   | EvidenceCandidate[]
   v
Verifier
```

------------------------------------------------------------------------

## 2.4 独立任务必须 Fan-out

例如：

``` text
BM25
Vector
Graph
Structured DB
Trend Radar
External Search
```

如果它们互不依赖，就应该并行。

------------------------------------------------------------------------

## 2.5 Barrier 只在真正需要全局结果时出现

例如：

``` text
多个 Hunter
   ↓
Evidence Merge
```

这里才需要 fan-in。

不要为了"流程看起来整齐"而强制所有任务同步。

------------------------------------------------------------------------

## 2.6 Verifier 放在 Edge 上

不要只在最终回答阶段统一验证。

应该：

``` text
Retriever
   ↓
Candidate Evidence
   ↓
Verifier
   ├─ ACCEPT
   └─ REJECT
```

只有验证后的 Evidence 才进入 Evidence Graph。

------------------------------------------------------------------------

## 2.7 Failure Isolation

一个 Hunter 失败：

``` text
Graph Hunter FAILED
```

不能导致：

``` text
整个研究会话 FAILED
```

系统应该降级：

``` text
Graph Route unavailable（例如事件三元组填充率为 0.0%）
→ BM25 + Vector + Structured Route + Trend continue
```

------------------------------------------------------------------------

## 2.8 Cycle 必须收敛

允许：

``` text
检索 → 验证 → 发现 Gap → 再检索
```

但必须有：

``` text
max_rounds
max_queries
max_cost
max_latency
no_gain_stop
```

------------------------------------------------------------------------

## 2.9 去重要针对 Seen，而不是 Confirmed

这是非常重要的原则。

系统维护：

``` text
seen_evidence
confirmed_evidence
```

被 verifier 拒绝的东西仍然属于 `seen`。

否则下一轮会重复找到同一个垃圾结果。

------------------------------------------------------------------------

# 3. 两张图架构

整个系统必须区分：

## 3.1 Execution Graph

表示：

``` text
谁执行
什么时候执行
能否并行
什么时候汇合
失败怎么处理
何时循环
何时停止
```

它是任务编排图。

------------------------------------------------------------------------

## 3.2 Evidence Graph

表示：

``` text
我们现在知道什么
哪些 Claim 已经得到支持
哪些 Claim 被反驳
哪些实体相互关联
当前缺什么证据
哪些证据相互冲突
```

它是知识状态图。

推荐节点：

``` text
Question
SubQuestion
Claim
Entity
Evidence
Source
Gap
Contradiction
```

推荐边：

``` text
REQUIRES
SUPPORTS
REFUTES
MENTIONS
DERIVED_FROM
DEPENDS_ON
CONTRADICTS
RESOLVES
```

------------------------------------------------------------------------

# 4. 总体系统架构

``` text
                        User Question
                              │
                              ▼
                    Query Interpreter
                              │
                              ▼
                     Research Planner
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
      BM25 Hunter       Semantic Hunter      Graph Hunter
          │                   │                   │
          ├───────────┬───────┴───────┬───────────┤
                      ▼               ▼
               Structured Hunter   Specialist Hunter
                      └──────┬────────┘
                             ▼
                     Evidence Normalizer
                             │
                             ▼
                       Edge Verifier
                             │
                             ▼
                       Evidence Graph
                        /           \
              Contradiction      Gap Analyzer
                    │                │
                    └──────┬─────────┘
                           │
             ┌─────────────┴─────────────┐
             │                           │
        gaps remain                  answerable
             │                           │
     Dynamic Next-hop Planner             ▼
             │                    Answer Composer
             │                           │
             └──── fan-out again ────────┘
                                         ▼
                                  Final Verifier
                                         │
                                         ▼
                                      Answer
```

------------------------------------------------------------------------

# 5. Shared Research State

不要让多个 Agent 通过长聊天历史共享状态。

使用结构化状态：

``` json
{
  "question": {},
  "research_plan": {},
  "claims": [],
  "entities": [],
  "evidence": [],
  "relations": [],
  "gaps": [],
  "contradictions": [],
  "queries_executed": [],
  "seen_evidence": [],
  "failed_routes": [],
  "scope": "",
  "user_id": "",
  "budget": {},
  "convergence": {}
}
```

原则：

1.  原始证据永久保存；
2.  LLM 推论与原始 Evidence 分离；
3.  Claim 必须可追溯到 Evidence；
4.  Evidence 必须可追溯到 Source / Chunk / Span；
5.  同一 Query 默认禁止重复执行；
6.  同一 Evidence 默认禁止重复验证；
7.  **`scope`（作用域键）与 `user_id` 是状态的组成部分，跨 run 不得相互污染。**

------------------------------------------------------------------------

# 6. Query Interpreter

第一步不是 embedding，而是理解问题结构。

输出：

``` json
{
  "intent": "causal_comparison",
  "entities": [],
  "scope": "",
  "time_scope": {},
  "constraints": [],
  "required_claims": [],
  "freshness_required": false,
  "answer_type": "evidence_synthesis",
  "complexity": "deep"
}
```

建议分类：

``` text
SIMPLE_FACT
MULTI_ENTITY
COMPARISON
TEMPORAL
CAUSAL
MECHANISM
HIGH_STAKES_ASSESSMENT
MULTI_HOP
SYNTHESIS
```

> 映射说明：原文的问题类型 `DIAGNOSTIC`（原场景的结论判定类）在本项目中映射为
> `HIGH_STAKES_ASSESSMENT`——即"是否触发高影响标记、需要多严的证据门槛"的判定。

若：

``` text
complexity = simple
```

直接进入 Fast Path。

------------------------------------------------------------------------

# 7. Research Planner

Planner 不负责回答。

Planner 的任务是把问题拆为：

``` text
SubQuestion
Claim
Evidence Requirement
Dependency
```

例如：

``` text
问题：
Entity_A 最近为什么出现异常变化？

拆解：

H1：最近有哪些直接相关的**事实型证据**（来自本作用域配置的权威来源）？
H2：同期同作用域内其它 Entity 发生了什么？
H3：是否存在跨实体的**传导链**（Entity_A → Relation → Entity_B → Relation → Entity_C）？
H4：是否有高 `authority_level` 的**原始来源**支持？
H5：有没有反证或替代解释（其它 Relation 路径、整体基线漂移、一次性因素）？
```

> 上例中的 `Entity_A`、`Relation` 都是占位符。实际作用域内的实体词表、
> 关系类型、权威来源清单**全部来自 `ScopeConfig`**（见 §0.2），不写进引擎。

其中：

``` text
H1
H2
H4
H5
```

可以并行。

Planner 输出：

``` json
{
  "sub_questions": [],
  "dependencies": [],
  "parallel_groups": [],
  "required_evidence_types": []
}
```

------------------------------------------------------------------------

# 8. Retrieval Fleet

不要只使用一个 Retriever。

## 8.1 BM25 Hunter（→ `EVIDENCE_STORE`，Hunter 名为实现侧叫法）

适合：

``` text
Entity 规范名（canonical_key）
Entity 标识码 / 编号类标识
文号类标识（如"某某〔2026〕12 号"）
本作用域词表内的术语
规范 / 标准名称
精确短语
```

> 以上各类的具体词表由作用域配置提供（`entity_lexicon` 与 `keyword_anchors`），
> 引擎只负责按词表匹配。

------------------------------------------------------------------------

## 8.2 Semantic Hunter（→ `SEMANTIC_INDEX`）

例如：

``` text
bge-m3
```

适合：

``` text
不同表达
语义近似
自然语言问题
隐式概念
```

------------------------------------------------------------------------

## 8.3 Graph Hunter（→ `GRAPH_TRAVERSAL`）

负责：

``` text
Entity → Relation → Entity → Evidence
```

例如：

``` text
Entity_A
  ↓ <relation_type_1>
Entity_B
  ↓ <relation_type_2>
Entity_C
```

> `relation_type_*` 来自作用域配置的 `relation_types`；引擎只做遍历与打分，
> 不认识任何具体关系名。

这是 Multi-hop 检索的重要路径。

> **前置约束**：Graph Hunter 只有在 `intel_article_events` /
> `intel_subject_canonical` / `kg_edges` 有足够覆盖（活跃文章事件覆盖率 ≥ 60%）
> 时才可用；否则必须走 §2.7 的降级路径（见 §0.3 约束 1）。

------------------------------------------------------------------------

## 8.4 Metadata Hunter（**无独立类别**，见 §8.1.1 差异项 D-4）

处理：

``` text
发布时间（含精度 exact/date/url/discovered）
本作用域 / 子主题
来源级别
文档类型
版本
```

> `publish_precision = discovered` 的条目不得按发布时间参与时效排序，
> 只能用于"何时入库"（见 §0.3 约束 2）。

------------------------------------------------------------------------

## 8.5 Query Expansion Hunter（→ `QUERY_EXPANSION`）

只负责生成：

``` text
同义词
缩写
上下位概念
本作用域词表内的术语
相关实体 / 关联实体（按配置的 relation_types 扩展）
```

不能负责最终回答。

------------------------------------------------------------------------

## 8.6 Structured Hunter（→ `STRUCTURED_STORE`）

针对：

``` text
SQL
API
内部结构化数据集（articles 证据池）
article_intel_classifications（分类与闸门结论）
intel_article_events（事件三元组）
intel_subject_canonical（实体归一）
内部业务库表（具体表清单由作用域配置声明）
```

------------------------------------------------------------------------

# 9. Evidence Object

不要把 Chunk 当成最终证据。

定义：

``` json
{
  "evidence_id": "E102",
  "claim_candidate": "...",
  "source_id": "...",
  "chunk_id": "...",
  "quote_span": [120, 260],
  "entities": [],
  "relations": [],
  "retrieval_method": "bm25",
  "scope": "",
  "admission_source": "rule_gate",
  "published_at_utc": "",
  "publish_precision": "exact",
  "source_quality": 0.92,
  "relevance": 0.88,
  "entailment": 0.91,
  "freshness": 0.80,
  "independence": 0.70,
  "contradiction_risk": 0.05,
  "status": "SUPPORTED"
}
```

核心原则：

> Chunk 可以很长，但真正进入 Evidence Graph 的应该是支持某个 Claim
> 的最小证据 Span。

------------------------------------------------------------------------

# 10. Edge Verifier

Verifier 至少检查：

1.  是否真的支持 Claim；
2.  是否只是关键词相似；
3.  实体是否一致；
4.  时间是否适用（含发布时间精度是否足够支撑该时效判断）；
5.  是否存在否定；
6.  来源是否符合要求（含准入闸门与来源合规）；
7.  是否只是二手转述；
8.  是否把相关关系错误当成因果关系。

推荐组合：

``` text
Reranker
   +
NLI
   +
Rule
   +
LLM Verifier
```

不要只依赖一个 LLM Judge。

------------------------------------------------------------------------

# 11. Evidence Score

第一版可以使用可解释加权：

``` text
EvidenceScore =
  wr × Relevance
+ we × Entailment
+ ws × SourceQuality
+ wf × Freshness
+ wi × Independence
- wc × ContradictionRisk
```

所有维度：

``` text
[0, 1]
```

Claim 级别记录：

``` text
support_count
independent_source_count
support_mass
refute_mass
```

------------------------------------------------------------------------

# 12. Gap Analyzer

这是整个"聪明检索"的核心。

每一轮 fan-in 后，不要问：

``` text
还要不要再检索一次？
```

应该问：

``` text
为了可靠回答原问题，
哪些必要 Claim 仍缺少什么类型的证据？
```

Gap：

``` json
{
  "gap_id": "G12",
  "claim_id": "C4",
  "missing": "independent_authoritative_evidence",
  "priority": 0.91,
  "suggested_queries": [],
  "suggested_routes": ["bm25", "graph", "trend_radar"],
  "reason": "only one low-quality source"
}
```

Gap 类型：

``` text
NO_EVIDENCE
LOW_RELEVANCE
LOW_AUTHORITY
SINGLE_SOURCE
MISSING_ENTITY_LINK
MISSING_TIME_LINK
CONTRADICTION
AMBIGUOUS_ENTITY
MISSING_CAUSAL_BRIDGE
MISSING_COUNTEREVIDENCE
```

> 映射说明：原文的 `independent_clinical_evidence` 在本项目中为
> `independent_authoritative_evidence`（独立且权威来源；"权威"由作用域的
> `authority_level` 配置定义），语义等价、门槛口径由作用域配置决定。

------------------------------------------------------------------------

# 13. Dynamic Next-hop Planner

下一跳不是：

``` text
hop = hop + 1
```

而是：

``` text
Gap → Best Retrieval Action
```

例如：

``` text
G1 → BM25 Hunter
G2 → Graph Hunter
G3 → Semantic + Structured Hunter
G4 → Two independent Hunters
G5 → Trend Radar（结构化产物不足时的降级路径）
```

完整循环：

``` text
PLAN
 ↓
FAN OUT
 ↓
VERIFY
 ↓
MERGE
 ↓
GAP ANALYSIS
 ├─ gaps
 │    ↓
 │  GENERATE NEXT HOPS
 │    ↓
 │  FAN OUT
 │
 └─ sufficient
      ↓
    ANSWER
```

------------------------------------------------------------------------

# 14. 收敛与停止

必须同时拥有：

``` text
max_rounds
max_queries
max_tokens
max_latency
max_cost
```

以及 Evidence convergence。

建议第一版：

``` text
连续两轮：
new_verified_claims == 0
AND
resolved_high_priority_gaps == 0
→ STOP_NO_GAIN
```

停止原因：

``` text
ANSWERABLE
BUDGET_EXHAUSTED
MAX_DEPTH
NO_GAIN
UNRESOLVABLE_CONTRADICTION
```

注意：

> STOP 不代表一定输出确定答案。

允许：

``` text
当前证据不足
```

在本项目里还要允许并明确暴露第三种状态：

``` text
图推理不可用（事件填充率不足）→ 仅给文本/趋势层面的结论
```

------------------------------------------------------------------------

# 15. Contradiction Agent

当出现：

``` text
E1 SUPPORTS C
E2 REFUTES C
```

不要简单多数投票。

比较：

``` text
来源级别
发布时间（含精度）
版本
统计口径 / 覆盖范围
实体是否一致（同一实体、`canonical_key` 归一一致）
定义是否一致
证据独立性
是否存在更新版本
```

无法消解则保留：

``` text
UNRESOLVED_CONTRADICTION
```

最终回答明确呈现不确定性。

> 映射说明：原文的"样本人群"在本项目中映射为**统计口径/覆盖范围**
> （例如同一种度量的全口径与样本口径、同一事件的不同统计周期）。

------------------------------------------------------------------------

# 16. Answer Composer

Answer Composer 不读取大量原始 chunk。

它读取：

``` text
Question
Verified Claims
Evidence Graph
Unresolved Gaps
Contradictions
Citation Map
```

输出每个事实声明时绑定：

``` text
claim_id
→ evidence_id[]
→ source/chunk/span
```

落地建议：直接复用 `qa_attribution.py` 的**证据解析**结果做声明分解与归因，
不要另起一套切分逻辑（见 §0.3 约束 4）。

------------------------------------------------------------------------

# 17. Final Verifier

检查：

``` text
每个事实是否有证据？
证据是否真正支持？
有没有过度推理？
是否漏掉重要反证？
citation 是否正确？
有没有把推测写成事实？
是否出现了无证据支撑的高影响结论（`conclusion_risk`）？（本项目新增）
```

失败时：

``` text
只重跑 Composer
或
只重跑相关 Verifier
```

不重跑整个 Research Graph。

引用校验的现役落点：`qa_pipeline.py` 的 `citation_validation` 节点
（`qa_orchestrator.py`、`qa_contracts.py` 中同名阶段）。

------------------------------------------------------------------------

# 18. Fast / Standard / Deep 三种路径

## Fast

``` text
retrieve
→ rerank
→ answer
```

用于简单事实。

------------------------------------------------------------------------

## Standard

``` text
plan
→ 2~3 hunters
→ verify
→ answer
```

用于普通综合问题。

------------------------------------------------------------------------

## Deep

``` text
plan
→ retrieval fleet
→ evidence graph
→ gap loop
→ contradiction resolution
→ answer
→ final verifier
```

用于：

``` text
多跳
因果
跨实体传导链分析
跨来源综合
复杂研究
```

> 映射说明：原文 Deep 路径列举的分析类问题，在本项目中即**跨实体的因果与
> 传导链分析**（`Entity_A → Relation → Entity_B → Relation → Entity_C`），
> 关系类型由作用域配置的 `relation_types` 给出。

------------------------------------------------------------------------

# 19. 与 RAGFlow + bge-m3 + reranker 的结合

如果当前已有：

``` text
RAGFlow
Elasticsearch
BM25
bge-m3
bge-reranker-large
```

不需要推倒重做。

把它们定位为：

``` text
Retrieval Infrastructure
```

在上层增加：

``` text
Agentic Retrieval Control Plane
         │
 ┌───────┼─────────┐
 ▼       ▼         ▼
BM25   Vector    Graph/DB
 │       │         │
 └───────┴─────────┘
         ↓
   Evidence Layer
```

职责划分：

RAGFlow：

``` text
找到候选材料
```

Graph-RAG Agent：

``` text
为什么找
下一步找什么
证据是否可信
是否还缺关键证据
什么时候停止
```

本项目现状：**A 机为"无 RAGFlow 一级检索模式"**（走 `qa_gateway.py` +
`qa_retrieval.py`）；B 机走 RAGFlow 网关（`192.168.0.64:8106`，经
`qa_ragflow_client.py`）。两条路径必须产出**同构的 Evidence Object**，
否则后续四图与 Gate 无法复用。

------------------------------------------------------------------------

# 20. 对统一情报采集的特殊升级

这是最值得做的改造。

把"检索"和"向用户澄清"统一起来看：

``` text
Evidence Gap
≈
情报信息缺口
```

Gap Analyzer 得到缺口后，不一定继续搜索知识库。

它可以决定：

``` text
Gap
 ├─ 证据池检索（KB）
 ├─ 内部结构化数据集（articles + 分类 + 事件）
 ├─ 内部业务库（表清单由作用域配置声明）
 ├─ 知识图谱（kg_nodes / kg_edges）
 ├─ 趋势雷达（trend_objective / buzzing_radar）
 └─ 向用户澄清（Ask User）
```

例如：

``` text
Claim：
需要判断"事件 X 对 Entity_A 的实际影响"

已有：
原始来源原文 ✓
生效时间 ✓（精度 exact）
适用范围 ✓
执行细则 ?
同期可比案例 ?
历史同类事件的传导结果 ?
反证（未发生 / 已修订）?
```

Gap Analyzer：

``` text
"历史同类事件的传导结果"能不能从当前证据池里找到？
 ├─ YES → 结构化 Hunter / 向量检索
 └─ NO
      ↓
   向用户澄清候选（Ask User Candidate）
```

因此可以把：

``` text
RAG
+
主动澄清
```

统一为：

# Evidence Acquisition Graph（统一情报采集图）

------------------------------------------------------------------------

# 21. EIG 的统一升级

原来：

``` text
EIG → 选择下一个用户问题
```

可以升级为：

``` text
EIG / Utility
→ 选择下一种 Evidence Acquisition Action
```

Action：

``` text
retrieve_document
retrieve_internal_structured
graph_traverse
query_database
trend_radar
web_search
ask_user
```

定义近似效用：

``` text
Utility(action) =
 ExpectedGapReduction
 × Importance
 × EvidenceReliability
 /
 (Latency + Cost + UserBurden)
```

在本项目场景：

``` text
Importance
```

可进一步加入：

``` text
本作用域在 `TaskImportance` 上追加：

``` text
conclusion_risk
scope_safety_weight
discriminative_power
```

> 这三个权重都是中性量：具体取值与触发条件写在作用域配置里。

这样系统就能决定：

> 这个信息缺口是查内部结构化数据更划算，还是查证据池，还是直接问用户最有效。

------------------------------------------------------------------------

# 22. Safety Priority（结论安全优先）

对于面向高影响决策的 RAG，普通 Utility 不能覆盖安全规则。

建议：

``` text
Hard Safety Trigger（由作用域配置的 high_stakes_patterns 触发）
   ↓
高影响 Gap
   ↓
优先获取关键证据
```

例如（**示例形态，模式本身来自作用域配置**）：

``` text
<关系类型 = 高风险事件> + <主体命中 high_stakes 条件>
```

不能因为：

``` text
UserBurden 高
```

就降低澄清优先级。

因此：

``` text
HighStakesFlag = true
```

时：

``` text
Priority Override
```

> 映射说明：原文的 Safety Priority 段（安全优先 / 危险信号）在本项目中即
> **结论安全 / 高影响标记**：
> 不得给出无证据支撑的高影响结论；引用必须可回溯。
> 哪些模式算高影响，由作用域配置声明，引擎不内置任何具体模式。

------------------------------------------------------------------------

# 23. Search Trace

Agentic RAG 必须可解释。

每个动作记录：

``` json
{
  "round": 3,
  "gap_id": "G8",
  "query": "...",
  "route": "graph",
  "scope": "",
  "reason": "...",
  "results": 12,
  "accepted": 2,
  "rejected": 10,
  "new_claims": 1,
  "resolved_gap": true,
  "latency_ms": 480
}
```

UI 能回答：

``` text
为什么搜？
搜了什么？
为什么走这个 route？
找到了什么？
为什么接受？
为什么拒绝？
解决了哪个 Gap？
为什么继续？
为什么停止？
```

------------------------------------------------------------------------

# 24. 缓存与去重

至少实现：

``` text
Query Cache
Embedding Cache
Evidence Cache
Verification Cache
Entity Resolution Cache
```

Query Fingerprint：

``` text
normalized_query
+ constraints
+ scope
+ corpus_version
+ retrieval_config
```

Evidence Fingerprint：

``` text
source_id
+ chunk_id
+ span
+ normalized_claim
```

> 缓存键必须包含 `scope`，否则会把 A 作用域的缓存命中给 B 作用域，
> 构成跨包泄露。

------------------------------------------------------------------------

# 25. 数据库设计

建议：

``` text
research_session
research_node_run
research_edge

sub_question
claim
entity

evidence
evidence_claim_link
evidence_source

gap
contradiction

search_query
search_result
verification_result

answer_claim
citation
```

通用字段：

``` text
run_id                # 对应 qa_runs
session_id
user_id
industry_pack_id
round
created_at
model
prompt_version
corpus_version
retrieval_config_version
```

这样可以 Replay。

与现役表的对应关系（真实路径）：

| 新表 | 现役来源 / 落点 |
|---|---|
| research_session / research_node_run | `qa_runs`（`qa_storage.py`） |
| evidence_source | `articles`（证据池） |
| entity / claim 支撑 | `intel_subject_canonical`、`article_intel_classifications` |
| 图节点与边 | `kg_nodes` / `kg_edges`（见 `docs/爬虫改进实施步骤.md` 阶段 8） |
| 会话维度 | `chat_history` |
| 信源与包归属 | `intel_sources`、`intel_source_industries` |

------------------------------------------------------------------------

# 26. 核心伪代码

``` python
def research(question, user_id, scope, budget):
    state = init_research_state(question, user_id, scope, budget)

    interpretation = interpret_query(question)
    mode = route_mode(interpretation)

    if mode == "FAST":
        return fast_rag(question, scope=scope)

    plan = build_research_plan(interpretation)
    state.add_plan(plan)

    while not state.should_stop():

        tasks = select_ready_tasks(state)

        results = run_parallel(tasks, scope=scope)

        candidates = normalize_results(results)

        fresh = dedupe_against_seen(
            candidates,
            state.seen_evidence
        )

        state.mark_seen(fresh)

        verified = verify_evidence_parallel(fresh)

        state.merge_verified_evidence(verified)

        contradictions = detect_contradictions(state)
        state.update_contradictions(contradictions)

        gaps = analyze_gaps(state)
        state.update_gaps(gaps)

        if is_answerable(state):
            break

        next_tasks = plan_next_hops(
            gaps=gaps,
            evidence_graph=state.evidence_graph,
            budget=state.remaining_budget
        )

        if not next_tasks:
            state.stop_reason = "NO_GAIN"
            break

        state.enqueue(next_tasks)

    draft = compose_answer(state)

    verdict = final_verify(
        draft=draft,
        evidence_graph=state.evidence_graph
    )

    if not verdict.pass_:
        draft = revise_answer(
            draft,
            verdict,
            state
        )

    return draft
```

------------------------------------------------------------------------

# 27. Pipeline 与 Barrier

推荐：

``` text
Hunter Item A
  → Normalize
  → Verify
  → Evidence Insert

Hunter Item B
  → Normalize
  → Verify
  → Evidence Insert
```

各 Item 可以流水线执行。

只有以下阶段需要 barrier：

``` text
全局去重
Contradiction Detection
Gap Analysis
Answerability Decision
Final Synthesis
```

避免：

``` text
所有检索完成
→ 所有 Normalize
→ 所有 Verify
```

这种不必要大同步。

------------------------------------------------------------------------

# 28. Node Failure Policy

每个节点声明：

``` text
FAIL_FAST
RETRY
SKIP
FALLBACK
DEGRADE
```

例：

``` text
Vector Hunter timeout
→ RETRY 1
→ fallback BM25
```

Graph DB unavailable（或事件填充率不足）：

``` text
DEGRADE
```

来源被反爬封锁（`antibot_detector.py` 判定）：

``` text
FALLBACK / DEGRADE
```

**严禁**把"没抓到"写成"没有发生"。

但：

``` text
Answer Composer
```

必须要求 Evidence State 有效，否则 Fail Fast。

------------------------------------------------------------------------

# 29. 模型分层

不要全部调用最大模型。

``` text
Query classification       rule / small model
Query expansion            small model
Dense Retrieval            embedding
BM25                       non-LLM
Rerank                     reranker
Evidence extraction        medium model
NLI verification           NLI / medium model
Planner                    strong model
Gap Analyzer               strong model
Contradiction Resolver     strong model, conditional
Answer Composer            strong model
Final Verifier             medium/strong
```

编排本身尽量用代码。

------------------------------------------------------------------------

# 30. 评价指标

## Retrieval

``` text
Recall@K
MRR
nDCG
Evidence Recall
Claim Coverage
```

## Multi-hop

``` text
Hop Success Rate
Gap Resolution Rate
Useful Hop Rate
Redundant Query Rate
No-gain Round Rate
```

## Evidence

``` text
Entailment Precision
Citation Precision
Citation Recall
Unsupported Claim Rate
Contradiction Detection Rate
```

## Agent

``` text
Average Nodes
Average Rounds
Average Queries
Parallelism Ratio
Failure Recovery Rate
Cache Hit Rate
```

## Cost

``` text
Latency
LLM Tokens
Embedding Calls
Retriever Calls
Cost / Answer
```

## Conclusion & Clarification（原 Clinical 段）

``` text
Critical Gap Recall          # 关键缺口召回（高影响标记相关缺口是否被抓住）
Conclusion Safety Miss Rate  # 无证据高影响结论的漏检率（必须趋近 0）
Question Utility             # 澄清问题的有效性
User Burden                  # 用户负担（澄清追问次数）
Information Gain             # 每次澄清带来的信息增益
Cross-Scope Leak Count       # 跨作用域越权命中数（必须为 0）
Cross-User Leak Count        # 跨用户记忆泄露数（必须为 0）
```

> 映射说明：原文的 `Critical Gap Recall` / `Safety Miss Rate` 在本项目中分别指
> **高影响标记相关关键缺口的召回率**与**无证据高影响结论的漏检率**。

------------------------------------------------------------------------

# 31. Ablation Study

必须测试：

``` text
A0 普通 RAG
A1 + Hybrid Retrieval
A2 + Planner
A3 + Verifier
A4 + Evidence Graph
A5 + Gap Analyzer
A6 + Dynamic Multi-hop
A7 + Contradiction
A8 + Unified Evidence Acquisition
```

观察每一步：

``` text
准确率
Citation
Recall
Latency
Cost
```

是否真正提升。

**本项目附加要求**：A4/A6 依赖图数据，必须以事件三元组填充率为前提；
填充率不足的作用域上，A4/A6 的结果标注为"不可用"而不是"无提升"。

------------------------------------------------------------------------

# 32. 分阶段工程实施

## Phase 0 --- Baseline Freeze

-   [ ] 固定当前检索配置（A 机一级检索模式 / B 机 RAGFlow 网关）。
-   [ ] 固定 bge-m3。
-   [ ] 固定 reranker。
-   [ ] 建立 Benchmark（按 `scope` 分层）。
-   [ ] 保存当前 Recall / Accuracy / Latency。
-   [ ] **记录结构化产物覆盖率现状**（事件三元组行数、实体归一表行数、
        `published_at_utc` 非空数与精度分布）。
-   [ ] **记录作用域配置基线**：库内在跑的作用域清单 vs 有种子文件的作用域清单，
-        列出"库里有、种子里没有"的差异项（见 §0.3 约束 5）。

验收：

``` text
可以重复跑出 baseline。
```

**前置门槛**：本阶段的覆盖率记录是后续所有图相关阶段能否开工的判据，
必须先写进 `tracking/IMPLEMENTATION_STATUS.md`（见 §0.3 约束 1、约束 2、约束 5）。

------------------------------------------------------------------------

## Phase 1 --- Evidence Layer

实现：

``` text
Claim
Evidence
Source
Citation Span
```

-   [ ] Chunk → Evidence 转换。
-   [ ] Evidence provenance。
-   [ ] Evidence Score。
-   [ ] Citation map。
-   [ ] **Evidence 携带 `scope`（当前实现 = `industry_pack_id`）/ `admission_source` /
       `publish_precision`。**
-   [ ] **`ScopeConfig` 契约落地**（§0.2 的「作用域配置接口」）：
       `scope_id` / `keyword_anchors` / `relation_types[]` / `freshness_policy` /
       `entity_lexicon` / `classification_weights`；
       且 `sources[]` **只声明 4 个字段**：`source_id` / `source_type` /
       `authority_level` / `license`（信源侧字段名 `license_profile`）。
-   [ ] **连接细节不得进契约字段**（§0.2.2）：地址 / 凭据 / 超时 / 重试 / 代理一律留在
       配置层；`ScopeConfig` schema 用 `additionalProperties: false` 收紧，
       静态检查禁止 `url` / `host` / `endpoint` / `token` / `api_key` / `credential` /
       `proxy` / `timeout` 等连接类字段名出现在契约里
       （理由：连接细节含凭据，一旦进契约就会顺着 Shared State / Search Trace /
       缓存键 / 日志扩散，违反 MASTER RULES 第 18 条"最小化日志"）。
-   [ ] **`scope` ↔ `industry_pack_id` 单一映射函数**（§0.2.1 硬边界）：
       契约层一律 `scope`，实现层保持 `industry_pack_id`，**两者之间只允许一处映射**
       （一个函数 / 一个 repository 方法）；各调用点禁止自己拆解，并有测试守门
       （与 Phase 15 的"作用域过滤只允许一处实现"同源）。
-   [ ] **引擎层零行业词静态校验**接入 CI（黑名单扫描 + 契约只允许抽象类型）。

**前置门槛**：本阶段不依赖图数据，**当前条件下即可开工**；
但若要产出"最新变化"类结论，必须先确认 `published_at_utc` 的采集与精度
已按 `publish_time.py` 落地（见 §0.3 约束 2）。

------------------------------------------------------------------------

## Phase 2 --- Verifier

-   [ ] relevance verifier。
-   [ ] entailment verifier。
-   [ ] entity/time verifier。
-   [ ] rejection reason。
-   [ ] verification cache。
-   [ ] **准入闸门判定（`intel_topics._classification_admitted`）与
       `admission_source='llm_semantic'` 的标记透传。**

**前置门槛**：不依赖图数据，当前即可开工。事件抽取回填
（`docs/爬虫改进实施步骤.md` 阶段 7）与本阶段可并行推进。

------------------------------------------------------------------------

## Phase 3 --- Parallel Retrieval Fleet

-   [ ] BM25 Hunter。
-   [ ] Semantic Hunter。
-   [ ] Graph Hunter（**前置：事件覆盖率 ≥ 60%**）。
-   [ ] Structured Hunter。
-   [ ] Trend Radar Hunter（新增，降级路径）。
-   [ ] Query Expansion。
-   [ ] 并行执行。
-   [ ] 单节点故障不影响整体。

------------------------------------------------------------------------

## Phase 4 --- Research Planner

-   [ ] query classification。
-   [ ] sub-question decomposition。
-   [ ] dependency graph。
-   [ ] parallel group。
-   [ ] required evidence type。

------------------------------------------------------------------------

## Phase 5 --- Evidence Graph

-   [ ] Claim。
-   [ ] Entity。
-   [ ] Evidence。
-   [ ] Supports/Refutes。
-   [ ] Contradiction。
-   [ ] Graph query API。
-   [ ] **`scope` 分区与查询强制过滤（当前实现 = `industry_pack_id`）。**

------------------------------------------------------------------------

## Phase 6 --- Gap Analyzer

-   [ ] Gap taxonomy。
-   [ ] priority。
-   [ ] evidence requirement。
-   [ ] suggested route。
-   [ ] answerability。

------------------------------------------------------------------------

## Phase 7 --- Dynamic Multi-hop

-   [ ] next-hop planner。
-   [ ] seen dedupe。
-   [ ] no-gain convergence。
-   [ ] budget。
-   [ ] max rounds。
-   [ ] retry/fallback。

------------------------------------------------------------------------

## Phase 8 --- Answer + Final Verify

-   [ ] Claim-aware synthesis。
-   [ ] citation binding。
-   [ ] unsupported claim check。
-   [ ] contradiction presentation。
-   [ ] revise only affected stage。
-   [ ] **承接已交付的【证据解析】与【词元影响力】**（`qa_attribution.py`）。

------------------------------------------------------------------------

## Phase 9 --- 统一情报采集（原 SAGE-RAG Unified Acquisition）

加入：

``` text
Knowledge Retriever（证据池）
Internal Structured Retriever（articles / 分类 / 事件 / 实体归一）
Database Retriever（内部业务库表，由作用域配置声明）
Trend Radar（trend_objective / buzzing_radar）
User Clarification（向用户澄清）
```

统一进入：

``` text
Action Candidate
```

-   [ ] Action Utility。
-   [ ] EIG。
-   [ ] Conclusion-safety override（高影响标记优先）。
-   [ ] User burden。
-   [ ] Scope priority（按作用域的优先级）。

------------------------------------------------------------------------

## Phase 10 --- Observability UI

UI 展示：

``` text
Execution Graph
Evidence Graph
Round
Gap
Search Trace
Accepted Evidence
Rejected Evidence
Contradiction
Cost
Latency
Stop Reason
Memory Scope（user / pack / session）
```

------------------------------------------------------------------------

## 与权威 18 阶段的对应关系

本节（V1 附录）的 Phase 0–10 与 `02_PHASE_OVERVIEW.md` / `phases/` 的
Phase 00–17 是**两套并行视角**，对应关系如下（供实施时对照，不替代任何一方）：

| 本节 Phase | 权威 Phase（`phases/`） |
|---|---|
| Phase 0 Baseline Freeze | Phase 00 PROJECT_DISCOVERY_BASELINE |
| Phase 1 Evidence Layer | Phase 02 EVIDENCE_LAYER（契约部分对应 Phase 01） |
| Phase 2 Verifier | Phase 03 VERIFIER_LAYER |
| Phase 3 Parallel Retrieval Fleet | Phase 04 PARALLEL_RETRIEVAL_FLEET |
| Phase 4 Research Planner | Phase 05 RESEARCH_PLANNER_EXECUTION_GRAPH |
| Phase 5 Evidence Graph | Phase 06 EVIDENCE_GRAPH_CONTRADICTION |
| Phase 6 Gap Analyzer | Phase 07 GAP_ANALYZER_DYNAMIC_MULTI_HOP（缺口分类部分） |
| Phase 7 Dynamic Multi-hop | Phase 07 GAP_ANALYZER_DYNAMIC_MULTI_HOP（多跳部分） |
| Phase 8 Answer + Final Verify | Phase 13 ANSWER_COMPOSER_FINAL_VERIFIER |
| Phase 9 统一情报采集 | Phase 14 统一情报采集 |
| Phase 10 Observability UI | Phase 16 OBSERVABILITY_UI |

> **待确认**：上表中 Phase 01（CONTRACTS_RESEARCH_STATE）与 Phase 08–12、15、17
> 在本节没有一一对应的条目（本节是 V1 视角，未含 Context/Memory/Skill 层）。
> 正式实施以 `phases/` 与 `tracking/IMPLEMENTATION_STATUS.md` 为准。

------------------------------------------------------------------------

# 33. 建议的最终结构（统一情报采集）

``` text
                    User Question / Dialogue
                              │
                              ▼
                      State Interpreter
                              │
                              ▼
                   Industry Research Planner
                              │
                              ▼
                       Evidence Gaps
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
 Knowledge RAG       Internal Structured       User Clarification
 （证据池）           （分类/事件/实体）        （向用户澄清）
        │                     │                     │
        └───────────────┬─────┴─────────────┬───────┘
                        ▼                   ▼
                   Evidence Verifier   Compliance / Scope Verifier
                        │                   │
                        └──────────┬────────┘
                                   ▼
                           Evidence Graph
                                   │
                                   ▼
                              Gap Analyzer
                          ┌────────┴─────────┐
                          │                  │
                     unresolved         sufficient
                          │                  │
                     next action             ▼
                          │            Final Answer (带引用)
                          └──────────────────┘
```

> 映射说明：原文的 `Safety Verifier` 在本项目中映射为
> **Compliance / Scope Verifier**——检查来源合规、作用域隔离、
> 以及是否存在无证据的结论。

------------------------------------------------------------------------

# 34. 最重要的工程原则

如果只保留十条：

1.  不把 Multi-hop 写死为固定跳数。
2.  用 Gap 驱动下一跳。
3.  独立检索并行。
4.  每个 Node 只有一个职责。
5.  Node/Edge 都有 Schema。
6.  Evidence 必须先验证再入图。
7.  去重针对 Seen，而不是 Confirmed。
8.  循环必须有收敛条件。
9.  失败要局部化。
10. 把"检索"和"向用户澄清"统一成 Evidence Acquisition。

------------------------------------------------------------------------

# 35. 最终目标

最终系统不应该只是：

``` text
一个会调用很多工具的 LLM
```

而应该是：

``` text
一个有明确状态、有证据图、有缺口、有预算、
会选择下一种最有价值证据获取动作，
能够验证并知道什么时候停止的 Research System。
```

这才是真正意义上的：

# Agentic Multi-hop RAG

而对于本项目的 AI 助手：

# Agentic Evidence Acquisition Engine

**并且它必须做到**：任何一条结论都能点回原文；
没有证据时明确说"证据不足"，而不是给一个听起来合理的答案。
