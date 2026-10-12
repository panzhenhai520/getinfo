# Phase 13 — Answer Composer & Final Verifier

## 目标
在**已交付**的【证据解析】与【词元影响力】之上，做本项目的**最终回答合成与终验**：
把已验证的证据按 claim 逐条合成为带引用的情报结论，并在出门前做最后一道校验——
**只用当前已验证证据回答**，没有证据就说"证据不足"。

- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **定位：** 依赖 Phase 02（证据层）、Phase 03（Verifier）、Phase 06（反证）、Phase 08（上下文）、
  Phase 09–10（记忆与复验）、Phase 11（skill 遥测）；被 Phase 16（UI）、Phase 17（E2E）依赖。
- **承接关系（关键）：** 本阶段**不是从零重写**。以下能力已交付，必须在它们之上做事：
  | 已交付能力 | 真实位置 | 表 | 接口 |
  |---|---|---|---|
  | 【证据解析】答案分解 + 归因（支持/反驳/无关） | `qa_attribution.QaAttributionService` | `qa_attribution_runs` / `qa_attribution_units` / `qa_attribution_links` | `GET|POST /api/qa/v1/runs/<run_id>/attribution` |
  | 【词元影响力】（遮挡法近似） | `QaAttributionService.token_influence(run_id, ...)` | `qa_token_influence` | `GET|POST /api/qa/v1/runs/<run_id>/token-influence` |
  | 引用校验阶段 | `qa_pipeline.py` 的 `citation_validation` 阶段（`qa_contracts.QA_STAGES` 已定义） | — | run 事件 `citation_added` |
  | run 落库 | `qa_storage.py`（`qa_runs` / `qa_stage_runs` / `qa_claims` / `qa_evidence` / `qa_claim_evidence` / `qa_conflicts`） | 同左 | `GET /api/qa/v1/runs/<run_id>` |

- **【词元影响力】的 method 纪律（硬要求）：** 该能力有多个后端，**必须在返回值与 UI 上标明 `method`**：
  | method 值 | 含义 | 可否称"归因" |
  |---|---|---|
  | `occlusion_logprob` | 遮挡法，真实对数概率落差 | 遮挡法归因（近似，但不是梯度） |
  | `occlusion_logprob_http` | 同上，走 HTTP 对数概率后端 | 同上 |
  | `occlusion_logprob_local` | 同上，走本地模型后端 | 同上 |
  | `occlusion_overlap_proxy` | **代理法：词汇重叠近似** | **只能称"近似/代理"，不得冒充梯度或真实归因** |
  - 严禁把 `occlusion_overlap_proxy` 呈现为"模型关注度/梯度归因"；API 与页面都必须把 method 原样透出。
  - **实测基线（A 机）：** A 机走的是 **`occlusion_logprob_http`**（本地 llama.cpp 端点真实生效，已算出影响力）；
    **仅在无对数概率时才降级为重叠代理并显式标注**。也就是说"代理法"是降级路径而不是默认路径，
    但它一旦出现就必须如实标注。B 机后端 → **待确认**（见 DECISION_LOG D-03 与待补记录）。

## 边界
**只用当前已验证证据回答。**

允许做：claim 级合成、引用绑定、反证与未决缺口呈现、终验、局部重试、证据不足输出。
不允许做：新增检索能力（→ Phase 04/14）、改写记忆（→ Phase 09）、作用域隔离强制（→ Phase 15）。

## 开始前
- [ ] 已读 MASTER_RULES
- [ ] 已读 V2 Architecture 相关章节
- [ ] 已读 IMPLEMENTATION_STATUS
- [ ] 前置 Gate 已通过（Phase 00 除外）
- [ ] 已实测【证据解析】与【词元影响力】在当前机器上真实可用，并记录实际 `method`（A/B 机后端可能不同 → 写 DECISION_LOG）
- [ ] 已确认 `citation_validation` 阶段当前在真实 run 里的行为与产出字段

## Must 子任务

### P13-01 claim-aware composer
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `claim-aware composer`；不得提前实现后续 Phase。
- **实现方法：**
  - 在 `qa_pipeline.py` 的 `synthesis` 阶段之上改造成**逐 claim 合成**：
    每个信息单元（claim）必须绑定 ≥1 条 `qa_evidence` 中状态为已验证的证据；绑定不上的 claim 不得出现在答案里。
  - 复用【证据解析】的信息单元切分结果（`qa_attribution_units`）作为 claim 切分的依据之一，
    **不要另造一套切分逻辑**；若需调整切分口径，改在 `qa_attribution` 内并回归其既有测试
    （`tests/test_qa_attribution.py`）。
  - 合成输入只允许来自 Phase 08 的 Context Pack（已选上下文），不得重新全量检索。
  - 未决/未消解冲突（Phase 06/10）必须以显式的"存在相反证据/尚未确定"段落呈现。
- **测试方式：** Unit（无证据的 claim 被剔除；有证据的 claim 必带引用）+ Integration（真实 run 端到端出答案）；
  失败路径：所有 claim 都无证据时必须走 P13-06，不得硬答。
- **验收标准：** 答案中每个事实性 claim 都能指回具体 `evidence_ref`；无证据 claim 数量为 0；
  不能回溯的句子一律不出现在答案正文；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P13-02 citation binding
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `citation binding`；不得提前实现后续 Phase。
- **实现方法：**
  - **必须复用并强化 `citation_validation` 阶段**（`qa_pipeline.py` 中已存在的阶段）：
    把合成产出的引用逐条校验 —— 引用目标存在、引用内容与主张一致（entailment，复用 Phase 03 的判定）、
    引用未被撤销/取代（Phase 10）。
  - 引用必须可回溯到：`evidence_ref → qa_evidence → articles.article_id → 原文 span`
    （证据池真表名是 **`articles`**，不存在 `intel_articles`）；只给"来源域名"不算可回溯。
  - 与【证据解析】的归因链路对齐：答案的每个信息单元 → 证据句（`qa_attribution_links`）→ 证据 → 文章。
    两条链路必须指向同一批证据，不一致即缺陷（提供一致性校验）。
  - `citation_validation` 增补的引用数、校验失败数与失败原因写入 run 事件（`citation_added` / 失败事件），
    供 Phase 16 展示与 METRICS 统计 `citation precision/recall`、`unsupported claim rate`。
- **测试方式：** Unit（引用校验的通过/失败矩阵；撤销/被取代证据不得被引用）+
  Integration（真实 run：合成引用数 vs 校验后引用数可对账）；失败路径：伪造/悬空 evidence_ref 必须被拦下。
- **验收标准：** 答案 0 悬空引用；每条引用可追到文章与 span；引用校验失败有可读原因并计入指标；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P13-03 counter-evidence/unresolved gaps
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `counter-evidence/unresolved gaps`；不得提前实现后续 Phase。
- **实现方法：**
  - 反证呈现：Phase 06 的 `REFUTES` / `CONTRADICTS` 关系、Phase 10 的 `MemoryContradiction`，
    必须在答案中以独立段落列出，**不得**被合成过程静默吞掉。
  - 未决缺口：Phase 07 未收敛的 gap 必须在答案末尾显式声明"以下问题本次未能确认"，并说明缺什么、
    去哪里可能拿到（例如"需要该包的付费/受限许可证来源"、"需要用户指定时间窗"）。
  - 【证据解析】里被标为"反驳/无关"的信息单元：反驳类必须在反证段可见，不得删除记录。
  - 反证覆盖进 METRICS 的 `counter-evidence coverage`；覆盖率为 0 且存在已知反证时必须判为失败。
- **测试方式：** Unit（有反证时必出反证段；无未决缺口时不产生空段）+ Integration（构造带反证的真实 run）；
  失败路径：反证存在但答案未呈现 → 测试必须失败。
- **验收标准：** 反证与未决缺口 100% 显式呈现；不产生"为凑格式"的空段落；覆盖率可统计；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P13-04 Final Verifier
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Final Verifier`；不得提前实现后续 Phase。
- **实现方法：**
  - 终验项（全部通过才允许输出）：
    1. 每个 claim 有已验证证据支撑（P13-01）；
    2. 引用全部通过 `citation_validation`（P13-02）；
    3. 反证与未决缺口已呈现（P13-03）；
    4. 未使用任何 `MEMORY_HINT`、未复验记忆、跨作用域证据（Phase 09/10/15）；
    5. 结论安全：不得给出无证据支撑的**高影响结论**（哪些类别属于高影响，由**作用域配置**声明，
       引擎不内建任何具体业务类别）；行动建议必须标注为"决策参考"且带条件与依据。
  - 终验结论写入 run 记录（含每项检查的 pass/fail 与理由），**不通过时不得输出答案正文**，
    转 P13-05 局部重试或 P13-06 不足输出。
  - 只有 Verifier 判定通过的证据才允许入图（Phase 03 硬规则），本阶段沿用同一判定服务，不另立标准。
- **测试方式：** Unit（五项各自的 pass/fail 用例）+ Integration（真实 run 终验记录可查）；
  失败路径：任一失败时确认没有答案正文流出（检查 SSE `answer_delta` 与最终 run 记录）。
- **验收标准：** 终验不通过时 0 字答案流出；终验记录可审计；跨 `scope`/未复验证据被拦下；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P13-05 local retry
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `local retry`；不得提前实现后续 Phase。
- **实现方法：**
  - 终验失败时**只做局部修补**：补充检索特定缺口、重组上下文包、重新绑定引用；
    **不得**重跑整个 run（避免成本爆炸与结果不可复现）。
  - 重试次数上限与预算上限配置化；超限即转 P13-06。
  - 每次局部重试写事件，说明"哪一项终验没过 → 补了什么 → 结果如何"，可回放。
  - 幂等：同一 run 的同一缺口重复重试不得产生重复证据行（沿用 `qa_runs` 幂等键约定）。
- **测试方式：** Unit（重试上限、只补缺口不重跑）+ Integration（构造终验失败 → 局部重试 → 通过）；
  失败路径：超限后必须转 P13-06 而不是继续重试。
- **验收标准：** 局部重试不触发全量重跑（有 trace 证明）；超限转不足输出；幂等成立；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P13-06 insufficient-evidence output
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `insufficient-evidence output`；不得提前实现后续 Phase。
- **实现方法：**
  - 证据不足时输出结构化"证据不足"回答：已确认什么（带引用）、**未能确认什么**、为什么拿不到
    （来源缺失/被反爬拦截/发布时间不可信/超出时间窗/不在本 `scope`）、建议用户提供什么（向用户澄清，见 Phase 14 ASK_USER）。
  - 明确禁止：用记忆、模型常识、其他 `scope` 的证据来"补上"结论。
  - 该输出同样要过终验（不能因为是"不足"就跳过检查），并计入 METRICS 的 `safety miss = 0` 相关统计。
- **测试方式：** Unit（不足场景下输出结构与措辞约束）+ Integration（真实"证据不足"场景端到端）；
  失败路径：证据不足时输出任何无引用结论 → 测试必须失败。
- **验收标准：** 证据不足场景 0 无引用结论；缺什么/怎么补写清楚；可被 Phase 16 页面正确渲染；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

## Phase 禁止事项
- 不得硬编码 benchmark 答案或删除/skip 测试。
- 不得把 BLOCKED 标为 PASS。
- 跨 Phase contract 变更必须先写 DECISION_LOG，并运行受影响回归。
- 不得在答案里使用未验证证据、未复验记忆、跨 `scope` 证据。
- 不得把 `occlusion_overlap_proxy`（代理法）标注为梯度或"模型真实关注度"。
- 不得删除或改写【证据解析】已经记录的"反驳/无关"信息单元来让答案显得干净。
- 不得为了"看起来有结论"而把行动建议写成确定性的高影响结论（哪些类别属于高影响由作用域配置声明）。

## Phase Gate
- [ ] 所有 Must 子任务 PASS
- [ ] Unit tests PASS
- [ ] Integration tests PASS
- [ ] Failure-path tests PASS
- [ ] Compatibility regression PASS（`tests/test_qa_attribution.py`、`tests/test_retrieval_evidence_policy.py` 等既有测试全绿）
- [ ] IMPLEMENTATION_STATUS / ACCEPTANCE_MATRIX 已更新
- [ ] 无未处理高优先级 blocker

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
