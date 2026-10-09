# Graph-RAG Agent：智能深度检索、上下文工程与长期记忆方案 V2.0

> V2.0 在 V1.0 的 Graph Engineering、Evidence Graph、Gap-driven
> Multi-hop RAG 和 SAGE-RAG Evidence Acquisition 基础上，引入 Context
> Engineering 与 Memory Engineering。 **规范架构：Execution Graph +
> Evidence Graph + Context Graph + Memory Graph。**

## 0. V2.0 核心升级

V2.0 把 Shared Research State 改为按任务组装的 Context
Pack，并新增跨会话 Memory Graph。Planner 拆成 Research Planner、Context
Planner、Memory Planner；Gap 拆成 Evidence Gap、Context Gap 和 Memory
Opportunity；Retrieval Fleet 上方增加 Skill Registry/Skill Router。

关键原则：没有找到证据，与证据已经存在但没有送给正确
Agent，是两种不同失败；过去验证过与当前仍成立也不是一回事。Memory 是
Research Accelerator，不是 Truth Oracle。

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

## 1.3 Context Graph

表示某个 Agent
当前真正需要看到的信息。节点：AgentTask/ContextItem/ClaimSummary/EvidenceDigest/Constraint/SkillInstruction/WorkingState/CounterEvidence；边：NEEDED_BY/SUPPORTS_TASK/CONSTRAINS/SUMMARIZES/COUNTERS/DERIVED_FROM。输出是受预算控制的
Context Pack，而不是完整 Research State。

## 1.4 Memory Graph

跨 Research Session
保存长期可复用知识与经验。节点：MemoryItem、VerifiedClaimMemory、EntityMemory、EpisodicResearchMemory、StrategyMemory、FailureMemory、QueryPatternMemory、SourceMemory、SkillPerformanceMemory、UserApprovedDomainRule。关系：ABOUT/DERIVED_FROM/VALIDATED_BY/SUPERSEDES/CONTRADICTS/EXPIRED_BY/HELPED_RESOLVE/FAILED_ON/APPLIES_TO。

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
memory_id/type/content/entity_ids/source_evidence_ids/confidence/valid_from/valid_until/freshness_class/last_verified_at/status/superseded_by/reuse_count。状态：ACTIVE/STALE/SUPERSEDED/CONTRADICTED/EXPIRED/REVOKED。旧
Memory 不物理覆盖，以版本关系保存演化。

# 3. Memory 类型

1.  **Verified Claim Memory**：高复用、已验证 Claim + Evidence
    指针，不保存模型"印象"。
2.  **Episodic Research Memory**：问题类型、成功拆解、关键
    Gap、有效检索路径、停止原因。
3.  **Failure Memory**：无效 query、失败 route、false-positive
    source、语义漂移扩展；必须绑定
    corpus_version/retrieval_config_version/time，环境变化后允许重试。
4.  **Strategy
    Memory**：某类问题哪些检索组合更有效；属于策略经验而非事实。
5.  **Source
    Memory**：authority/domain/update_frequency/reliability/known
    limitations。
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

# 5. Context Utility 与预算

``` text
ContextUtility(item, task) =
  Relevance × EvidenceStrength × TaskNecessity × Freshness × Diversity / TokenCost
```

Memory Item 再乘 MemoryConfidence × MemoryFreshness。Context Planner 在
token budget 下选择最大价值集合，并保留输出/工具预算。禁止再把固定 top_k
当成 Context 策略。

# 6. 三类缺口

**Evidence Gap**：当前 Evidence Graph
真正缺事实。动作：SEARCH/DB/EMR/GRAPH/WEB/ASK_PATIENT。

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

``` text
skills/
├── bm25_search/
├── semantic_search/
├── graph_traversal/
├── sql_query/
├── emr_search/
├── web_search/
├── causal_reasoning/
├── clinical_evidence/
├── contradiction_resolution/
├── citation_verification/
└── patient_inquiry/
```

每个 Skill 声明
skill_id/description/input_schema/output_schema/preconditions/cost_class/latency_class/permissions/version。Skill
Router 根据 task_type + gap_type + historical skill performance +
budget + permissions 选择最小必要集合；Skill 指令按需加载。

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
BM25 | Vector | Graph | DB | EMR | Web | Ask Patient   (parallel)
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

# 10. Memory Recall 与 Revalidation

Planning Recall 在 Research Planner 前优先召回
Strategy/Failure/QueryPattern Memory，避免旧事实污染规划。Evidence
Recall 在 Gap 出现后可召回 Verified Claim/Source Memory，但先标记
MEMORY_HINT，经 Freshness Gate + Verifier 后才可进入当前 Evidence
Graph。

Freshness 示例：数学定义 LONG；历史事实 LONG；临床指南
VERSION_SENSITIVE；药品说明 MEDIUM；软件/API SHORT；新闻/价格/库存
VERY_SHORT；患者当前状态 SESSION/ENCOUNTER_BOUND。

硬规则：status!=ACTIVE 不作证据；source_version_changed 必须
revalidate；freshness_required 必须 revalidate；high_stakes 默认
revalidate。

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
  - PrivacyRisk - StalenessRisk - DuplicationPenalty

MemoryRecallScore =
  SemanticRelevance × TaskApplicability × Confidence × Freshness × HistoricalUtility
  - ContradictionRisk
```

防污染：未验证自由生成内容不得成为事实 Memory；必须保存
provenance；高风险事实重新验证；外部网页指令不得升级为系统规则；写入/失效/覆盖全部
audit；支持按 source/entity/session 撤销污染 Memory。

# 12. Memory Graph 数据库

新增：memory_item、memory_version、memory_entity_link、memory_evidence_link、memory_relation、memory_recall_log、memory_write_decision、memory_validation、memory_contradiction、memory_usage_stat、skill_performance_memory、source_reliability_memory。

memory_item
至少：id/memory_type/canonical_content/confidence/freshness_class/valid_from/valid_until/last_verified_at/status/scope/created_from_session_id/created_at。

禁止只用一个向量库保存全部 Memory。推荐：Relational metadata + vector
index + graph relations + source/evidence provenance。

# 13. SAGE-RAG 四图统一

``` text
Clinical Dialogue
 ↓
State Interpreter
 ↓
Research Planner
 ↓
Evidence Graph ← Memory Hint/Revalidation
 ↓
Gap Analyzer
 ├─ Evidence Gap → KB/EMR/DB/Web/Ask Patient
 ├─ Context Gap  → Context Pack 重组
 └─ Safety-Critical Gap → Priority Override
 ↓
Clinical Reasoner → Final Verifier
```

Evidence Acquisition
Action：retrieve_document/retrieve_emr/graph_traverse/query_database/web_search/ask_patient。

``` text
Utility(action) =
 ExpectedGapReduction × TaskImportance × EvidenceReliability
 / (Latency + Cost + UserBurden)
```

医疗加入
ClinicalRisk/SafetyWeight/DiagnosticDiscrimination。SafetyCritical=true
时安全规则覆盖普通成本效用。

关键收益：若信息已在 Evidence Graph 中但只是 Context
Gap，系统应重组上下文而不是再次询问患者，从而减少无意义追问。

# 14. 医疗 Memory 边界

scope：GLOBAL_KNOWLEDGE / ORGANIZATION / PATIENT_LONGITUDINAL /
ENCOUNTER / SESSION。

指南知识可为 GLOBAL_KNOWLEDGE；医院规则为 ORGANIZATION；长期病史可
PATIENT_LONGITUDINAL 但使用时检查时间；本次胸痛持续时间只能
ENCOUNTER；某轮搜索失败为 SESSION 或带 corpus/version 的 EPISODIC。

患者当前症状、生命体征、用药状态不得无条件跨 encounter 当成当前事实。

# 15. 可观测性 UI

必须提供四个视图：Execution Graph View、Evidence Graph View、Context
Inspector、Memory Graph View。

Context Inspector 显示：为何加入、token 数、来自 Evidence 还是
Memory、哪些 item 被预算挤出、是否 revalidated。

Memory View
显示：类型、来源、confidence、freshness、last_verified、reuse_count、status、superseded
chain。

Search Trace
新增：memory_recalled/memory_revalidated/context_items_selected/context_items_dropped/context_tokens/skills_loaded。

# 16. V2.0 新增评价指标

Context：Context Precision、Context Recall、Useful Context
Density、Context Token Efficiency、Counter-evidence
Coverage、Context-gap False Retrieval Rate。

Memory：Memory Hit Utility、Stale Memory Rate、Revalidation Pass
Rate、Memory Pollution Rate、Memory Reuse Gain、Failure Repetition
Rate、Supersession Accuracy。

系统级：Answer Quality、Evidence Recall、Unsupported Claim
Rate、Latency、Cost、Rounds、Queries、Patient Burden。必须比较启用/禁用
Context Graph 与 Memory Graph 的 ablation。

# 17. V2.0 新增实施阶段

## Phase 11 --- Context Graph

-   [ ] ContextItem schema
-   [ ] Context Planner
-   [ ] Context Utility
-   [ ] Token Budget
-   [ ] Counter-evidence reservation
-   [ ] Context Pack Builder
-   [ ] Context Inspector UI
-   [ ] Context Gap 检测

验收：同一任务不再把完整 Research State 塞给所有
Agent；在答案质量不下降前提下降低无关 context/token；Context Gap
不误触发新检索。

## Phase 12 --- Skill Registry

-   [ ] Skill schema/version
-   [ ] Skill Router
-   [ ] permission/cost/latency metadata
-   [ ] on-demand skill context
-   [ ] Skill performance telemetry

## Phase 13 --- Memory Graph Core

-   [ ] Memory schema/relations
-   [ ] Write Gate
-   [ ] Recall
-   [ ] provenance
-   [ ] TTL/freshness
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
-   [ ] Source reliability
-   [ ] Skill performance memory
-   [ ] corpus/config version-aware retry

## Phase 16 --- SAGE-RAG Memory Safety

-   [ ] scope enforcement
-   [ ] encounter boundary
-   [ ] patient-longitudinal freshness
-   [ ] safety override
-   [ ] audit/replay

## Phase 17 --- V2 Ablation

比较：V1 baseline；+Context Graph；+Memory Recall；+Failure
Memory；+Strategy Memory；完整四图。至少报告质量、Evidence
Recall、Unsupported Claim、Context Tokens、Latency、Cost、Query
数、Failure Repetition、Stale Memory Rate、临床 Patient Burden。

# 18. V2.0 核心伪代码

``` python
def research_v2(question, session, budget):
    state = init_state(question, session, budget)
    interpretation = interpret_query(question)

    planning_memory = memory_recall(
        interpretation,
        types=["STRATEGY", "FAILURE", "QUERY_PATTERN"]
    )
    plan = research_planner(interpretation, planning_memory)

    while not state.should_stop():
        tasks = select_ready_tasks(state)
        skills = skill_router(tasks, state.skill_performance, budget)
        results = run_parallel(tasks, skills)

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
                hints = recall_relevant_memory(gap)
                valid_hints = revalidate_if_needed(hints)
                state.enqueue(plan_acquisition(gap, valid_hints))

        if is_answerable(state):
            break

    pack = context_planner.build_pack(
        task="ANSWER",
        evidence_graph=state.evidence_graph,
        memory_hints=state.validated_memory,
        token_budget=budget.context_tokens
    )
    draft = compose_answer(pack)
    answer = final_verify_and_revise(draft, state.evidence_graph)

    candidates = memory_planner.extract_candidates(state, answer)
    memory_write_gate(candidates)
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
-   [ ] 医疗 encounter/patient-longitudinal/global scope 严格隔离。
-   [ ] 可 replay 任意回答：执行→证据→上下文→记忆。
-   [ ] V2 ablation 证明新增层带来质量/效率/重复失败率中的可测收益。

# 20. V2.0 最终目标

系统不再只是"会调用很多检索工具的 LLM"，而是：

> **一个拥有执行状态、当前证据状态、任务上下文状态和长期记忆状态的研究系统；它知道下一步应该获取什么证据、把什么送给哪个
> Agent、哪些历史经验值得复用、哪些旧知识必须重新验证，并知道什么时候停止。**

对于 SAGE-RAG，最终形态是：

**Agentic Evidence + Context + Memory Acquisition Engine**

------------------------------------------------------------------------

# 附录 A：V1.0 完整基线（未被 V2.0 明确替代的条款继续有效）

# Graph-RAG Agent：多跳智能检索改造方案 V1.0

> 目标：把传统"Query → Top-K → LLM"的 RAG
> 改造成能够规划、并行搜索、建立证据图、验证、发现缺口、自适应继续检索，并在证据充分后回答的智能检索系统。\
> 本文结合《Graph Engineering: Stop Chaining Your
> Agents》的图式编排思想，并针对 RAG / SAGE-RAG /
> 医疗主动问诊场景做工程化扩展。

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
Graph Route unavailable
→ BM25 + Vector + Structured Route continue
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
6.  同一 Evidence 默认禁止重复验证。

------------------------------------------------------------------------

# 6. Query Interpreter

第一步不是 embedding，而是理解问题结构。

输出：

``` json
{
  "intent": "causal_comparison",
  "entities": [],
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
DIAGNOSTIC
MULTI_HOP
SYNTHESIS
```

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
药物 A 为什么可能导致症状 B？

拆解：

H1：A 的作用机制是什么？
H2：B 的病理机制是什么？
H3：A 是否影响 B 对应 pathway？
H4：是否存在临床证据？
H5：有没有反证或替代解释？
```

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

## 8.1 BM25 Hunter

适合：

``` text
药名
疾病名
编码
法规
标准
专业术语
精确短语
```

------------------------------------------------------------------------

## 8.2 Semantic Hunter

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

## 8.3 Graph Hunter

负责：

``` text
Entity → Relation → Entity → Evidence
```

例如：

``` text
Drug
  ↓ inhibits
Pathway
  ↓ related_to
Symptom
```

这是 Multi-hop 检索的重要路径。

------------------------------------------------------------------------

## 8.4 Metadata Hunter

处理：

``` text
时间
科室
来源级别
人群
文档类型
版本
```

------------------------------------------------------------------------

## 8.5 Query Expansion Hunter

只负责生成：

``` text
同义词
缩写
上下位概念
专业术语
相关实体
```

不能负责最终回答。

------------------------------------------------------------------------

## 8.6 Structured Hunter

针对：

``` text
SQL
API
EMR
HIS
LIS
PACS
业务数据库
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
4.  时间是否适用；
5.  是否存在否定；
6.  来源是否符合要求；
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
  "missing": "independent_clinical_evidence",
  "priority": 0.91,
  "suggested_queries": [],
  "suggested_routes": ["bm25", "graph"],
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
发布时间
版本
样本人群
实体是否一致
定义是否一致
证据独立性
是否存在更新版本
```

无法消解则保留：

``` text
UNRESOLVED_CONTRADICTION
```

最终回答明确呈现不确定性。

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
```

失败时：

``` text
只重跑 Composer
或
只重跑相关 Verifier
```

不重跑整个 Research Graph。

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
临床分析
跨来源综合
复杂研究
```

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

------------------------------------------------------------------------

# 20. 对 SAGE-RAG 的特殊升级

这是最值得做的改造。

临床问诊里：

``` text
Evidence Gap
≈
Clinical Information Gap
```

Gap Analyzer 得到缺口后，不一定继续搜索知识库。

它可以决定：

``` text
Gap
 ├─ Knowledge RAG
 ├─ Historical EMR
 ├─ Structured HIS DB
 ├─ Knowledge Graph
 └─ Ask Patient
```

例如：

``` text
Claim：
需要排除急性冠脉风险

已有：
年龄 ✓
胸痛 ✓
部位 ✓
持续时间 ?
活动关系 ?
伴随出汗 ?
呼吸困难 ?
```

Gap Analyzer：

``` text
“持续时间”能不能从当前 EMR 找到？
 ├─ YES → EMR Hunter
 └─ NO
     ↓
   Patient Inquiry Candidate
```

因此可以把：

``` text
RAG
+
主动问诊
```

统一为：

# Evidence Acquisition Graph

------------------------------------------------------------------------

# 21. EIG 的统一升级

原来：

``` text
EIG → 选择下一个患者问题
```

可以升级为：

``` text
EIG / Utility
→ 选择下一种 Evidence Acquisition Action
```

Action：

``` text
retrieve_document
retrieve_emr
graph_traverse
query_database
ask_patient
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

在医疗场景：

``` text
Importance
```

可进一步加入：

``` text
clinical_risk
safety_weight
diagnostic_discrimination
```

这样系统就能决定：

> 这个信息缺口是查病历更划算，还是查知识库，还是直接问患者最有效。

------------------------------------------------------------------------

# 22. Safety Priority

对于医疗 RAG，普通 Utility 不能覆盖安全规则。

建议：

``` text
Hard Safety Trigger
   ↓
高风险 Gap
   ↓
优先获取关键安全证据
```

例如危险信号：

``` text
胸痛 + 呼吸困难 + 冷汗
```

不能因为：

``` text
UserBurden 高
```

就降低追问优先级。

因此：

``` text
SafetyCritical = true
```

时：

``` text
Priority Override
```

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
session_id
round
created_at
model
prompt_version
corpus_version
retrieval_config_version
```

这样可以 Replay。

------------------------------------------------------------------------

# 26. 核心伪代码

``` python
def research(question, budget):
    state = init_research_state(question, budget)

    interpretation = interpret_query(question)
    mode = route_mode(interpretation)

    if mode == "FAST":
        return fast_rag(question)

    plan = build_research_plan(interpretation)
    state.add_plan(plan)

    while not state.should_stop():

        tasks = select_ready_tasks(state)

        results = run_parallel(tasks)

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

Graph DB unavailable：

``` text
DEGRADE
```

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

## Clinical

``` text
Critical Gap Recall
Safety Miss Rate
Question Utility
Patient Burden
Information Gain
```

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

------------------------------------------------------------------------

# 32. 分阶段工程实施

## Phase 0 --- Baseline Freeze

-   [ ] 固定当前 RAGFlow 配置。
-   [ ] 固定 bge-m3。
-   [ ] 固定 reranker。
-   [ ] 建立 Benchmark。
-   [ ] 保存当前 Recall / Accuracy / Latency。

验收：

``` text
可以重复跑出 baseline。
```

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

------------------------------------------------------------------------

## Phase 2 --- Verifier

-   [ ] relevance verifier。
-   [ ] entailment verifier。
-   [ ] entity/time verifier。
-   [ ] rejection reason。
-   [ ] verification cache。

------------------------------------------------------------------------

## Phase 3 --- Parallel Retrieval Fleet

-   [ ] BM25 Hunter。
-   [ ] Semantic Hunter。
-   [ ] Graph Hunter。
-   [ ] Structured Hunter。
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

------------------------------------------------------------------------

## Phase 9 --- SAGE-RAG Unified Acquisition

加入：

``` text
Knowledge Retriever
EMR Retriever
Database Retriever
Patient Inquiry
```

统一进入：

``` text
Action Candidate
```

-   [ ] Action Utility。
-   [ ] EIG。
-   [ ] Safety override。
-   [ ] User burden。
-   [ ] Clinical priority。

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
```

------------------------------------------------------------------------

# 33. 建议的 SAGE-RAG 最终结构

``` text
                    Clinical Question / Dialogue
                              │
                              ▼
                      State Interpreter
                              │
                              ▼
                       Clinical Planner
                              │
                              ▼
                       Evidence Gaps
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
 Knowledge RAG          Historical EMR        Patient Inquiry
        │                     │                     │
        └───────────────┬─────┴─────────────┬───────┘
                        ▼                   ▼
                   Evidence Verifier    Safety Verifier
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
                          │            Clinical Response
                          └──────────────────┘
```

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
10. 在 SAGE-RAG 中，把"检索"和"问患者"统一成 Evidence Acquisition。

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

而对于 SAGE-RAG：

# Agentic Evidence Acquisition Engine
