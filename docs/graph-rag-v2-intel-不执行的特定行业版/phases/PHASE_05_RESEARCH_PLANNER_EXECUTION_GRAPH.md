# Phase 05 — Research Planner & Execution Graph

## 目标
把用户那句"【Scope_1】在【TimeWindow】内最近有什么关键变化"拆成一张**真实依赖的有向无环图**：
哪些子问题必须先答、哪些可以并行、每个子问题需要什么类型的证据、总共花多少预算。
Planner **不负责回答**，它只负责把问题拆对、把依赖排对、把预算定对。

## 边界
问题拆成真实依赖 DAG（Execution Graph）。

> **术语约定（引擎层硬规则）**：本阶段属于**通用引擎层**，正文一律用中性类型
> （`Claim` / `Evidence` / `Scope` / `TimeWindow` / `Relation` / `Gap`），
> **不得出现任何具体行业词**；作用域差异一律由 Phase 01 的 `ScopeConfig` 提供。
> 作用域统一写 `scope`（**当前实现 = `industry_pack_id`**）。

## 开始前
- [ ] 已读 MASTER_RULES
- [ ] 已读 V2 Architecture 相关章节
- [ ] 已读 IMPLEMENTATION_STATUS
- [ ] 前置 Gate 已通过（Phase 00 除外）
- [ ] Phase 00 的 benchmark 问题集可用（本 Phase 的拆解质量要用它评）

---

## 本项目真实基础（现役 Planner 已经能做什么）

现役规划器在 `qa_planner.py`：`QaQueryPlanner.plan(request_payload)` 是唯一入口，内部已有
`_split_subquestions` / `_question_relationship` / `_question_category` / `_cluster_subquestions` /
`_retrieval_strategy` / `_answer_outline` / `_question_plan`。已能识别的关系类型与策略
（`_RELATION_LABELS` / `_RELATION_STRATEGIES`）：

| 关系 | 中文 | 现役策略（原样沿用） |
|---|---|---|
| `single` | 单一问题 | 围绕当前问题直接检索并回答，结论必须绑定证据 |
| `parallel` | 并列问题 | 按子问题分别检索和回答，最后合并共同依据，避免重复结论 |
| `progressive` | 递进问题 | 先解析政策或事实本身，再基于该依据分析影响、风险和应对 |
| `causal` | 因果问题 | 先确认事实依据，再分析原因、结果和中间链条 |
| `comparison` | 比较问题 | 先建立比较维度，再逐项核验证据并给出差异 |
| `parent_child` | 总分问题 | 先总览问题，再按主题分组回答；问题过多时引导用户多轮追问 |
| `overlap` | 交集问题 | 先界定共同范围和排除范围，只回答同时满足条件的部分 |
| `conflict` | 冲突问题 | 先列出不同口径，再按来源权威性、发布日期和适用范围裁决 |

现役分类（`_CATEGORY_RULES`）：`evidence_gap`（证据不足/待核验类）、
`risk_response`（风险与应对类）、`industry_impact`（影响类）、
`filing_collection`（**带特定场景口径，属配置层，须迁出引擎**）、
`subject_scope`（适用对象类）、`policy_content`（文件内容类）、`fact_check`（事实核验类）。

> ⚠️ **两层分离问题（必须在 P05-01 处理）**：`_CATEGORY_RULES` 现在**写死在引擎里**，
> 其中若干条（尤其 `filing_collection`）带特定场景口径——这违反规范 §二
> "行业只以配置进入"。本 Phase 要把它降级为**「配置层现状」**：
> 引擎只保留通用类别，场景类别与触发词一律移进 `ScopeConfig`（字段组
> `classification_weights` / `keyword_anchors`）。
> 迁移时机与兼容策略**待确认**，不在本 Phase 静默改；在迁移完成前，
> 这些类别**只作为"现役绑定"记录，不得当作引擎契约的一部分**。

**待补的四类**（见 `docs/爬虫改进实施步骤.md` 阶段 9，当前未做）：
`conditional_constraint`（条件约束）/ `causal`（因果链）/ `temporal_relation`（时序关系）/
`multi_hop`（多跳）。

**与【调整思路】的联动是 P0**：用户在规划阶段提的调整必须真正改变检索与生成
（`_normalize_adjustment_patch` → `_apply_plan_patch_to_plan`），
而现役实测是"调整内容确实到后端，但**检索式 / 子问题 / 证据完全没变**"——只换了回答模板。
本 Phase 的 `subquestions` / `queries` / `time_window` / `must_fetch_fulltext` 契约必须让
调整**落到检索条件上**（`docs/爬虫改进实施步骤.md` 阶段 5）。

---

## 子任务
### P05-01 Query Interpreter
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Query Interpreter`；不得提前实现后续 Phase。
  输出结构化的问题理解对象，**这一步不做 embedding、不检索**。
- **实现方法：**
  - 复用 `qa_planner.QaQueryPlanner.plan` 的规则层（`_question_category` /
    `_question_relationship` / `_date_scope` / `_looks_followup` / `_standalone_question` /
    `_context_focus`），LLM 只作兜底（现役取舍，继续保持）。
  - 输出结构（对齐 `01_V2_ARCHITECTURE.md` §6，领域中性化改写）：
    ```json
    {
      "intent": "evidence_gap",
      "scope": "",
      "entities": {"subjects": [], "orgs": [], "topics": []},
      "time_scope": {"window": "", "precision_required": ""},
      "constraints": [],
      "required_claims": [],
      "freshness_required": false,
      "answer_type": "evidence_synthesis",
      "complexity": "deep"
    }
    ```
    其中 `scope` 为作用域 / 租户键（**当前实现 = `industry_pack_id`**）；
    `intent` 取值来自作用域配置声明的意图集合（现役 `_CATEGORY_RULES` 是它的实现）。
  - 问题类型（第一版，**取代原架构中他方场景的类别**）：
    `SIMPLE_FACT`（单一事实核验）/ `MULTI_ENTITY`（多主体）/
    `COMPARISON`（横向对比，如两个主体 / 两个作用域）/ `TEMPORAL`（时序 / 变化）/
    `CAUSAL`（因果链）/ `MECHANISM`（传导机制）/ `JUDGEMENT`（研判 / 归因判断）/
    `MULTI_HOP`（多跳）/ `SYNTHESIS`（综述）。
  - 安全前置：`qa_planner.is_high_risk_policy_question` 与 `_INJECTION_RE`
    必须在解释阶段就生效（外部文档指令不得升级成系统规则）。
  - **作用域识别**：从问题里判出 `scope`；判不出时用用户当前所在作用域，
    并**不得跨作用域**（一次 run 只属于一个 `scope`）。
  - **配置外移（待确认）**：现役 `_CATEGORY_RULES` 里有若干带特定场景口径的类别
    （如 `filing_collection`）；按规范 §二 两层分离原则，这类词表应移到 `ScopeConfig`，
    引擎只保留通用类别。迁移时机与兼容策略**待确认**，不在本 Phase 静默改；
    在迁移前它们在文档中只以"现役绑定"出现。
  - `complexity = simple` → 直接进 Fast Path（P05-04）。
- **测试方式：** 单测：九类问题各 3 例；跟随问题（`_looks_followup`）与独立问题区分；
  注入文本被识别；作用域识别错误率统计（用 benchmark 问题集）。
- **验收标准：** 问题理解对象字段齐全且可机器校验；九类分类在 benchmark 上
  有可复现的准确率；`time_scope` 与 `published_precision` 三档精度口径一致。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P05-02 Subquestion/Claim decomposition
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Subquestion/Claim decomposition`；不得提前实现后续 Phase。
  拆出"要回答什么"（子问题）与"要证实什么"（claim），并给出每条的**证据要求**。
- **实现方法：**
  - 子问题：复用 `_split_subquestions` / `_cluster_subquestions`；
    输出 `sub_questions[]` + `required_evidence_types[]`。
  - Claim：Claim 的最终消费者是 Phase 06 的 Evidence Graph 与 Phase 13 的
    Answer Composer；`qa_level1.QaLevel1Generator.generate` 产出的 claims
    （模型自产 id `l1-c1..`，兜底指纹 `l1-evidence-N`）是现役第一版，
    需在拆解阶段就允许"预先声明 required_claims"，形成"要证什么 → 找到了什么"的对照。
  - 证据要求（中性口径）：一手原始文件 / 权威发布 / 结构化登记 / 趋势序列，
    以及**时效要求**（"发布即最高价值、几天后只作背景"这类策略由 `freshness_policy` 配置提供）。
  - 拆解示例（占位符口径，不含任何具体行业词）：
    ```
    问题：【Scope_1】在【TimeWindow】内的关键变化是什么，对【Entity_A】意味着什么？
    拆解：
    H1：本窗口内「事件」类证据有哪些？（先答事实）
    H2：这些事件的原始文件是什么、发布日期与生效日期分别是什么？（一手证据）
    H3：这些变化改变了哪些适用主体、哪些环节？（适用对象）
    H4：【Entity_A】在同一窗口的公开动作里，哪些与之直接相关？（关联事实）
    H5：有没有相反口径或"影响有限"的反证？（反证）
    ```
  - 标记可并行：H1、H2、H4、H5 可并行；H3 依赖 H1/H2 的结论。
  - **边界**：拆解不得直接给结论；不得替 Answer Composer 写答案文字。
- **测试方式：** 单测：拆解出子问题数量受上限约束；每条子问题都带
  `required_evidence_types`；claim 与子问题的映射完备；空问题不产生子问题。
- **验收标准：** benchmark 上"必要 claim 漏拆率"可量化且低于现役；
  每条子问题都能在 Phase 04 找到至少一路对应的检索来源。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P05-03 dependency/parallel groups
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `dependency/parallel groups`；不得提前实现后续 Phase。
  产出 `depends_on` 有向无环图与并行分组；依赖必须是**真实依赖**，不是"看起来顺序好看"。
- **实现方法：**
  - 输出：`dependencies[]`（`from` / `to` / `reason`）+ `parallel_groups[]`。
  - 独立任务必须 fan-out；barrier 只在真正需要全局结果时出现
    （`01_V2_ARCHITECTURE.md` §2.4 / §2.5）。
  - 参考实现方向：`docs/爬虫改进实施步骤.md` 阶段 9 的
    `qa_query_decompose.py`（**该文件当前不存在，是待实现项**），输出带 `depends_on` 的
    有向无环子查询图。**文件名与落点待确认**。
  - 与现役 stage 链的关系：现役 `qa_orchestrator.FULL_STAGES` 是**线性**的
    （`plan → level1_retrieval → level1_draft → level2_retrieval → level2_research →
    conflict_review → synthesis → citation_validation`）。本子任务要把"线性链"升级为
    "DAG 执行"，但**必须保留 FAST_STAGES / FULL_STAGES 作为 DAG 的两个特例**，
    否则兼容回归必炸。
  - 环检测：任何环一律报错并退化为顺序执行（Cycle 必须收敛，§2.8）。
  - 每个节点必须有 Contract（§2.2），节点级失败隔离（§2.7）。
- **测试方式：** 单测：依赖拓扑排序正确；有环输入被拒；并行组内互不依赖；
  节点失败不影响无依赖节点；Fast 路径仍走 FAST_STAGES 且行为不变。
- **验收标准：** 依赖图可机器校验（无环、无孤立必要节点）；
  可并行的子问题确实被并行调度（用 trace 里的开始 / 结束时间戳证明）。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P05-04 Fast/Standard/Deep
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Fast/Standard/Deep`；不得提前实现后续 Phase。
  三档路径共享同一套节点契约，只是"跑哪些节点、花多少预算"不同。
- **实现方法：**
  - 现役取值：`qa_contracts.QA_MODES = ("standard", "deep", "fast")`；
    `qa_orchestrator.stage_plan(mode, origin)` 决定 `FAST_STAGES` 或 `FULL_STAGES`。
  - Fast：`plan → level1_retrieval → level1_draft → synthesis → citation_validation`
    （现役 `FAST_STAGES`，**原样保留**）。
  - Standard：现役 `FULL_STAGES` 全链。
  - Deep：在 Standard 之上开启多跳（Phase 07）与 Gap 循环；**受 `qa_resilience`
    预算硬约束**。
  - `level2_enabled=false`（A 机无 RAGFlow 一级检索模式）时，把 `level2_*` /
    `conflict_review` 从阶段链上摘掉而不是空转
    （`docs/爬虫改进实施步骤.md` 阶段 6 第 4 条）。
  - 档位选择必须**可解释**：给用户看到"为什么走了 Deep"，
    且用户显式指定档位时不得被自动降档。
- **测试方式：** 单测：三档的 stage 序列与现役逐项一致；A 机模式下 stage 链正确裁剪；
  预算不足时 Deep → Standard 的降级有日志；显式档位不被覆盖。
- **验收标准：** 三档行为差异可复现；Fast 路径端到端仍通过 `citation_validation`
  （历史结论：跳过 synthesis 不可行，草稿不满足 FINAL_ANSWER 契约会让
  `citation_validation` 抛错）。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P05-05 budget/Execution Graph
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `budget/Execution Graph`；不得提前实现后续 Phase。
- **实现方法：**
  - 预算维度（`01_V2_ARCHITECTURE.md` §14）：`max_rounds` / `max_queries` /
    `max_tokens` / `max_latency` / `max_cost`，落 `plan.budget`。
  - 现役预算载体：`qa_resilience.STAGE_BUDGET_SECONDS`（stage 级秒数）+
    `qa_orchestrator` 的 `over_budget` 上报 + `qa_settings` 的运行时设置。
  - Token 预算必须与 `qa_level1` 的实际输出上限对齐（历史坑：`max_tokens=900` 截断导致
    草稿 JSON 不完整、每次白跑一次修复重试；现为可配 `QA_LEVEL1_LOCAL_MAX_TOKENS`，
    默认 2400）。**降预算不得低于完成一次合法输出的下限**。
  - Execution Graph 持久化：节点执行记录落 P01-04 的 node-run 表（**表名待确认**），
    并与 `qa_thinking` / `qa_events` 的事件流对齐（`qa_contracts.QA_EVENT_TYPES`）。
  - 节点级失败策略（§28）：重试 / 降级 / 跳过 / 中止，四选一必须显式声明。
- **测试方式：** 单测：超预算时按声明策略收敛；token 预算过低时不产生截断输出
  （构造用例）；DAG 执行记录可完整回放。
- **验收标准：** 任何一次 run 都能回答"预算花了多少、花在哪、有没有超、超了怎么收敛"；
  Execution Graph 与 SearchTrace 能对上。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

---

## Phase 禁止事项
- 不得硬编码 benchmark 答案或删除/skip 测试。
- 不得把 BLOCKED 标为 PASS。
- 跨 Phase contract 变更必须先写 DECISION_LOG，并运行受影响回归。
- （本项目追加）固定 `hop_count` 代替 Gap 驱动循环（MASTER_RULES 禁止项）。
- （本项目追加）不得把完整 Research State 广播给所有 Agent。
- （本项目追加）**不得让【调整思路】只改回答模板而不改检索式**——
  调整必须落到 `plan.queries` / `time_window` / `must_fetch_fulltext` / `output_form`。

## Phase Gate
- [ ] 所有 Must 子任务 PASS
- [ ] Unit tests PASS
- [ ] Integration tests PASS
- [ ] Failure-path tests PASS
- [ ] Compatibility regression PASS
- [ ] IMPLEMENTATION_STATUS / ACCEPTANCE_MATRIX 已更新
- [ ] 无未处理高优先级 blocker
- [ ] Fast / Standard 两档的 stage 序列与现役逐项一致（兼容回归通过）
- [ ] 【调整思路】改变 `plan.queries` / `time_window` 的端到端证据已留档

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
