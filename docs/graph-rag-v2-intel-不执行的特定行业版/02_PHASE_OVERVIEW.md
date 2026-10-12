# PHASE OVERVIEW

> 本表是 Graph-RAG V2 在**本项目（行业情报采集与分析平台）**下的阶段骨架。
> 落点只有一件事：**让 AI 助手（对话框里的问答入口）真正聪明**——用户提问后能自己检索、
> 自己研究、自己推理，最后给出**每句都能回溯到证据**的答案。
>
> 术语与规范见 `00_REWRITE_SPEC.md`；唯一架构依据仍是 `01_V2_ARCHITECTURE.md`。

|Phase|名称|边界|前置|
|---|---|---|---|
|00|Project Discovery & Baseline|只读探查、冻结现役问答链路与 benchmark|无|
|01|Contracts & Research State|建立四图共享的类型契约（含作用域配置接口）和可回放 run|00|
|02|Evidence Layer|Chunk 升级为可验证 Evidence（正文 / 段落 / 结构化事件）|01|
|03|Verifier Layer|候选证据验证后才能入图|02|
|04|Parallel Retrieval Fleet|多路检索编队（证据库检索 / 语义检索 / 外部增强检索 / 结构化数据 / 公开网络 / 趋势信号）；**具体接哪个源由作用域配置决定**|03|
|05|Research Planner & Execution Graph|问题拆成真实依赖 DAG|04|
|06|Evidence Graph & Contradiction|显式维护当前情报事实状态|05|
|07|Gap Analyzer & Dynamic Multi-hop|Evidence Gap 驱动下一跳和收敛|06|
|08|Context Graph & Context Planner|按任务组装最小有效 Context|07|
|09|Memory Graph Core|跨会话长期记忆及 Write Gate|08|
|10|Revalidation & Memory Conflict|时效、冲突、失效和污染撤销|09|
|11|Skill Registry & Router|按任务加载最小必要能力|10|
|12|Episodic Failure Strategy Memory|学习成功和失败检索经验|11|
|13|Answer Composer & Final Verifier|只用当前已验证证据回答|12|
|14|Unified Intelligence Acquisition（原 SAGE-RAG）|统一采集：证据库 / 结构化数据 / 公开网络 / 向用户澄清（ASK_USER）/ 趋势信号|13|
|15|Memory Safety & Tenant Isolation|记忆作用域（user_id + industry_pack_id + 会话）与越权隔离|14|
|16|Observability UI|四图和决策全过程可解释|15|
|17|E2E Ablation Release|证明收益、兼容、可回滚发布|16|

**Gate：** 当前 Phase 的 Must 子任务未全部 PASS，不进入依赖它的下一阶段。

---

## 一、本项目的三件事，决定了阶段顺序

1. **现役链路必须沿用真实模块名**，不许另起新名：
   `qa_gateway`（问答网关 / 配额 / 降级）、`qa_retrieval`（证据检索 + `ArticleRetriever`）、
   `qa_attribution`（证据解析 / 词元影响力）、`qa_storage`（run 落库）、
   `qa_ragflow_client`（RAGFlow 增强检索）、`qa_pipeline` 的 `citation_validation` 阶段
   （引用校验，**不是独立模块**）、`intel_worker.py` / `intel_light_scanner.py`（采集）、
   `intel_classifier`（分类）、`trend_objective` / `buzzing_radar`（客观趋势）、
   `antibot_detector`（反爬识别与止损）、`publish_time.py`（发布时间与精度）。
   证据池真表名是 **`articles`**（**不存在 `intel_articles`**）。
2. **现役问答模式：A 机是"无 RAGFlow 一级检索模式"**，B 机走 RAGFlow 网关
   （`192.168.0.64:8106`）。所以 Phase 04 的每一路检索都必须能**单独缺席**，
   不允许任何一路成为硬依赖。
3. **证据池准入不是"有行就算"**：内容要进 AI 证据池必须过关键词 / 锚点闸门
   与正文质量闸门（`intel_topics._classification_admitted`），并支持
   `admission_source='llm_semantic'` 的语义兜底。

## 一之二、两层分离（改写后的最高优先级约束）

| 层 | 内容 | 硬规则 |
|---|---|---|
| **通用引擎层**（所有 Phase 的算法、契约、图结构） | 检索编队、Verifier、Evidence Graph、Gap Analyzer、Context Planner、Memory Graph、Answer Composer | **行业无关**。只允许抽象类型：`Entity` / `Claim` / `Evidence` / `Source` / `Scope` / `TimeWindow` / `Relation` / `Verifier` / `Gap`。**不得出现任何具体行业词** |
| **作用域配置层**（每个作用域各自提供，不改引擎） | 关键词与锚点表、信源清单与权威级别、关系类型定义、时效策略、实体词表、分类权重 | **只以配置与数据进入**；算法不得依赖任何具体作用域 |

隔离维度在契约中统一写 **`scope`**（作用域 / 租户键），并注明
「**当前实现 = `industry_pack_id`**」——换成客户 / 部门 / 项目 / 产品线同样成立。
作用域配置接口 `ScopeConfig` 由 **Phase 01** 定义（见该阶段 P01-01）。


## 二、当前现实（决定了哪个 Phase 现在就能做）

| 现实 | 实测数字 | 影响哪些 Phase |
|---|---|---|
| 结构化产物填充率极低 | A 机 7022 篇里**事件三元组仅 3 行（0.0%）、实体归一 0 行** | Phase 02/04/06/07 的"图"路现在只能建空图 → **先做前置**（`docs/爬虫改进实施步骤.md` 阶段 7 抽取回填、阶段 8 建图） |
| 发布时间曾全表为空 | `published_at_utc` 0/6105；现已改为 RSS 与列表页在**采集当刻**写入（`publish_time.py`），并按精度标注 `exact/date/url/discovered` | Phase 02（provenance）、Phase 03（时间 verifier）、Phase 07/10（时效与再验证） |
| 可解释性已交付 | 【证据解析】（答案分解 + 归因）与【词元影响力】（遮挡法近似）已上线，落 `qa_attribution_units` / `qa_attribution_links` / `qa_token_influence` | Phase 13、Phase 16 是**承接已有基础**，不是从零重写 |
| 作用域与配置分离 | 数据库里 **10 个作用域在跑**，而 `config/industry_packs/` 种子里只有 **8 个业务包 JSON**（2 个无种子文件，只能后台手工维护）——**这是配置层事实，不是引擎词** | Phase 00 基线、Phase 01 的 `ScopeConfig`（契约不得要求配置必须以文件形式存在） |
| 多租户已收口 | 证据检索与记忆均按 `scope`（当前实现 = `industry_pack_id`）收口，跨作用域取证据属越权 | Phase 01/02/03/15 |

**结论：Phase 00/01/02/03 现在就能做；Phase 04 里"证据库检索 / 语义检索 / 结构化数据 /
公开网络 / 趋势信号"五类现在就能做，外部增强检索与关系遍历两类按机器能力与前置到位情况
分别放行；Phase 05–08 依赖 04，且其中涉及关系遍历的部分必须等阶段 7/8 达标。**

## 三、谁来读、按什么顺序读

1. `00_MASTER_RULES.md` → 本文件 → `tracking/IMPLEMENTATION_STATUS.md` → 当前 Phase。
2. 从第一个非 PASS 的 Must 子任务继续，不重新规划已完成阶段。
3. 每个子任务走固定循环：`Inspect → Design within boundary → Implement → Unit Test →
   Integration Test → Failure Test → Regression → Record evidence → Update status`。
