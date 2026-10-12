# Graph-RAG Agent V2 — 行业情报版 · AI 编程智能体连续实施开发包

**无需另附 V1.0。** `01_V2_ARCHITECTURE.md` 是唯一架构依据。

本开发包服务于**本项目**：一个行业情报采集与分析平台，核心交互入口是**对话框里的 AI 助手**。
用户（分析师 / 运营）提问 → 助手做检索、研究、推理 → 给出**带引用的答案**。
本包里所有阶段、所有图、所有 Gate，最终都要落到同一件事上：**让这个助手真正聪明**。

> 术语与写法以 `00_REWRITE_SPEC.md` 为准。本目录是 `docs/graph-rag-v2/` 的行业情报改写版，
> 原目录保持不动，留作对照与回退。

## ⚠️ 最高架构原则：引擎层与配置层彻底分离

**不要把原场景的词替换成"我们自己的行业词"**——那只是把别人的领域耦合换成我们的。
正确做法是**把领域词中性化，行业只作为配置进入**：

| 层 | 内容 | 硬规则 |
|---|---|---|
| **通用引擎层** | 四图结构、检索编队、Verifier、Gap Analyzer、Context/Memory Planner、Answer Composer | **行业无关**。只允许抽象类型：`Entity` / `Claim` / `Evidence` / `Source` / `Scope` / `TimeWindow` / `Relation` / `Verifier` / `Gap`。**不得出现任何具体行业词** |
| **作用域配置层** | 关键词与锚点表、信源清单与权威级别、关系类型、时效策略、实体词表、分类权重 | **只以配置与数据进入**，不改引擎 |

- 作用域统一用中性的 **`scope`（作用域键）**，**当前实现 = `industry_pack_id`**；
  换成客户、部门、项目任何维度都成立，算法不得假设它一定是行业。
- **已定边界（Phase 01 硬规则）**：**契约层一律 `scope`，实现层保持 `industry_pack_id`
  （`qa_retrieval.py` / `qa_storage.py` / 真实列名不动），两者之间只允许一处映射函数。**
  理由：真做参数抽象要改现役检索与存储的每一处查询，那是刚修好的生产链路
  （证据准入、证据解析、B 机降级修复）；Phase 01 就动它等于拿线上稳定性换一个
  「将来可能换维度」的可能性，而契约层用 `scope` 已能拿到全部收益。
- **Phase 01 的契约必须包含「作用域配置接口」（`ScopeConfig`）**，详见
  `01_V2_ARCHITECTURE.md` §0.2 与 §0.2.1。

首次使用：把整个目录放入目标仓库（现已位于 `docs/graph-rag-v2-intel/`），将
`prompts/START_HERE.md` 内容交给 DeepSeek/Claude/Codex。
中断恢复：使用 `prompts/RESUME_PROMPT.md`，智能体从 tracking 的第一个非 PASS 项继续。

目录：
- MASTER_RULES：最高工程规则
- V2_ARCHITECTURE：完整架构（四图 + 18 阶段 + Gate）
- PHASE_OVERVIEW：18 阶段依赖
- phases：逐阶段可勾选执行卡
- prompts：首次/长程/恢复/单阶段提示词
- tracking：状态/决策/验收/blocker
- schemas/reference：契约检查和指标

## 开始之前必须知道的现状（真实前置）

1. **结构化产物填充率极低**：实测 A 机 7022 篇里**事件三元组只有 3 行（0.0%）、实体归一 0 行**。
   在这个数据基础上建图 = 建空图。前置任务见 `docs/爬虫改进实施步骤.md` 阶段 7（事件抽取回填）
   与阶段 8（建图）。
2. **发布时间已在补**：`published_at_utc` 曾全表为空（0/6105）；现已做到 RSS 与列表页在**采集当刻**
   写入发布时间（`publish_time.py`），并按精度标注 `exact/date/url/discovered`。
   存量仍为空的部分标 `UNKNOWN`：**不得作为"最新变化"的证据，但仍可作背景/历史证据参与检索**，
   只是不进时效敏感推理；新采集会自然覆盖，**不需要专门的历史回填任务**。
3. **来源许可证闸门（已实测确认）**：源字段名就是 **`license_profile`**，真身文件
   `config/financial_source_licenses.json`（schema **`financial-source-license-v1`**，
   `financial_source_license.py` 的 `REQUIRED_PROFILE_FIELDS` 共 **15 个顶层必填字段**）；
   强制范围**只对 `origin_pack_id == 'financial_markets'` 的 RSS 源生效**，其它作用域、
   其它来源类型根本不检查；未登记或复核过期 → **扫描时直接跳过，连请求都不发**；
   **其余来源按公开处理，不得 fail-closed**。详见 `01_V2_ARCHITECTURE.md` §14.1。
4. **证据池有准入闸门**：文章要进 AI 证据池，必须过作用域关键词/锚点闸门与正文质量闸门
   （`intel_topics._classification_admitted`，词表来自作用域配置），并支持
   `admission_source='llm_semantic'` 的语义兜底。
5. **可解释性已有基础**：【证据解析】（答案分解 + 归因）与【词元影响力】（遮挡法近似）已交付
   （`qa_attribution.py`），后续阶段应承接而非从零重写。
6. **作用域隔离**：证据检索与记忆都必须按 `scope` 收口（当前实现 = `industry_pack_id`），
   跨作用域取证据属于越权。**Phase 15 的正式名称是「记忆安全与作用域隔离」
   （Memory Safety & Scope Isolation）**，与 `tracking/`、`reference/METRICS.md` 一致。
7. **作用域配置有缺口**：库里**有 10 个作用域在跑**，而 `config/industry_packs/` 种子里
   **只有 8 个业务包 JSON**——`invest_mgmt` 与 `embodied_ai` 没有种子文件，只能手工在后台维护。
   这正是"引擎与配置分离"要解决的问题，Phase 00/01 必须先记录这条基线。

> ✅ **术语已收敛（第 5 条决策）**：`01_V2_ARCHITECTURE.md` §8 的契约名已统一到
> `phases/` 采用的 **8 个数据源类别名**——`EVIDENCE_STORE` / `SEMANTIC_INDEX` /
> `EXTERNAL_AUGMENT` / `GRAPH_TRAVERSAL` / `STRUCTURED_STORE` / `PUBLIC_NETWORK` /
> `TREND_SIGNAL` / `QUERY_EXPANSION`（编队调度无动作名）；`BM25 Hunter` 这类名字
> 只作**当前实现绑定的通俗叫法**，**不得作为契约字段或 Gate 判据**。
> 映射表见该文 §8.1.1，阶段文档侧尚存的 6 条差异见 §17 末尾（D-1 ~ D-6）。
> **两套 Phase 编号不合并（已定）。**

## 现役链路（改写时一律沿用这些真实名字）

`qa_gateway.py`（问答网关/配额/降级）、`qa_retrieval.py`（证据检索 + `ArticleRetriever`）、
`qa_attribution.py`（证据解析 / 词元影响力）、`qa_storage.py`（run 落库，`qa_runs`）、
`qa_ragflow_client.py`（RAGFlow 增强检索）、`qa_pipeline.py` 的 `citation_validation` 节点（引用校验）、
`intel_worker.py` / `intel_light_scanner.py`（采集）、`intel_classifier.py`（分类）、
`trend_objective.py` / `tools/buzzing_radar.py`（客观趋势）、`antibot_detector.py`（反爬识别与止损）。

现役问答模式：**A 机为"无 RAGFlow 一级检索模式"**，B 机走 RAGFlow 网关（`192.168.0.64:8106`）。

最终 DoD：Phase 00–17 Must 全 PASS；E2E/ablation/兼容/回滚通过；有真实指标报告；
**无跨作用域越权、无跨用户记忆泄露**等未解释安全 blocker。
