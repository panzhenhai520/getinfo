# Phase 14 — SAGE-RAG Unified Acquisition

## 边界
统一 KB/EMR/DB/Web/患者追问。

## 开始前
- [ ] 已读 MASTER_RULES
- [ ] 已读 V2 Architecture 相关章节
- [ ] 已读 IMPLEMENTATION_STATUS
- [ ] 前置 Gate 已通过（Phase 00 除外）

## 子任务
### P14-01 Action schema
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Action schema`；不得提前实现后续 Phase。
- **实现方法：** 先探查并复用现有组件；新增部分采用 typed contract、配置化参数、版本化与可追溯日志。
- **测试方式：** Unit + Integration；涉及长任务增加 timeout/retry/idempotency；涉及接口增加兼容回归；构造至少一个失败路径。
- **验收标准：** 功能真实运行；自动测试通过；无未解释回归；边界条件符合 V2；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P14-02 ExpectedGapReduction utility
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `ExpectedGapReduction utility`；不得提前实现后续 Phase。
- **实现方法：** 先探查并复用现有组件；新增部分采用 typed contract、配置化参数、版本化与可追溯日志。
- **测试方式：** Unit + Integration；涉及长任务增加 timeout/retry/idempotency；涉及接口增加兼容回归；构造至少一个失败路径。
- **验收标准：** 功能真实运行；自动测试通过；无未解释回归；边界条件符合 V2；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P14-03 KB/EMR/DB/Web routes
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `KB/EMR/DB/Web routes`；不得提前实现后续 Phase。
- **实现方法：** 先探查并复用现有组件；新增部分采用 typed contract、配置化参数、版本化与可追溯日志。
- **测试方式：** Unit + Integration；涉及长任务增加 timeout/retry/idempotency；涉及接口增加兼容回归；构造至少一个失败路径。
- **验收标准：** 功能真实运行；自动测试通过；无未解释回归；边界条件符合 V2；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P14-04 ask-patient route
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `ask-patient route`；不得提前实现后续 Phase。
- **实现方法：** 先探查并复用现有组件；新增部分采用 typed contract、配置化参数、版本化与可追溯日志。
- **测试方式：** Unit + Integration；涉及长任务增加 timeout/retry/idempotency；涉及接口增加兼容回归；构造至少一个失败路径。
- **验收标准：** 功能真实运行；自动测试通过；无未解释回归；边界条件符合 V2；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P14-05 Context Gap no-repeat
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Context Gap no-repeat`；不得提前实现后续 Phase。
- **实现方法：** 先探查并复用现有组件；新增部分采用 typed contract、配置化参数、版本化与可追溯日志。
- **测试方式：** Unit + Integration；涉及长任务增加 timeout/retry/idempotency；涉及接口增加兼容回归；构造至少一个失败路径。
- **验收标准：** 功能真实运行；自动测试通过；无未解释回归；边界条件符合 V2；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P14-07 重复提问识别与思路调整确认（Repeat-Question Realignment）
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成本子任务；不得提前实现后续 Phase。**需求来源：用户在 2026-10-11 明确提出的产品要求**——
  「多轮对话中，用户对同一问题或相似问题问了两次以上，说明对上次答案不满意，系统应当有交互，
  询问是否更改/调整回答问题的思路」。
- **触发判据（必须可复算，纯规则，不得用模型）：**
  - **同题/相似题判定**：与**同一会话（scope_key 含 session）**内历史问题相比，满足"归一化后词面重叠 ≥ 阈值"
    **或**"命中同一主体 + 同一命题意图"（复用 P07/P10 的实体与命题口径，不另造一套）；
    阈值、窗口轮数与"相似"的口径全部配置化（`QA_REPEAT_QUESTION_*`），并写进回执可复算。
  - **不满意信号**：至少命中一条可解释依据——① 上一次回答**没有新增已核验证据**（复用 P07 的 `new_verified_claims` 口径）；
    ② 上一次回答被标注降档/无证据（复用 P08 的 `degradation_reasons`）；③ 上一次停止原因是 `NO_GAIN`/`BUDGET_EXHAUSTED`/`MAX_DEPTH`；
    ④ 上一次缺口未解决率高于阈值。**依据必须逐条落到回执里，不许空口说"用户不满意"。**
  - **不得误报**：不同问题（词面重叠低且主体不同）**绝不触发**；跨会话（新会话开始）**不算重复**；
    用户已明确"按原思路/不用改"之后，同一议题在 TTL 内**不再追问**。
- **交互要求（一次、给选项、可拒绝）：**
  - 同一会话同一议题**最多交互一次**（幂等，重复触发只记一次）；
  - 交互内容必须是**具体选项**而不是空泛询问，选项至少包含：换检索通道、扩大/收窄时间窗、
    提高来源权威要求、放宽或收紧证据门槛、换分析口径（如事件 vs 趋势）、**保持原思路**；
  - 每个选项要写清**预期影响与代价**（例如"扩大时间窗会引入更旧证据"），供用户判断；
  - 用户选择落到**可执行配置**（会话级指令），下一次同题运行**真的按新思路执行**，并把"这次为什么换了思路"写进 SearchTrace；
  - 用户选择"保持原思路"时，记录为**明确的用户决定**（不是沉默默认），后续不再追问同一议题。
- **失败路径：** 判定或交互构造抛错**不得阻断回答**（只记账并照常出答案，回执带 `realignment_error`）；
  重复检测在无会话上下文（单轮 API 调用）时**自动跳过**并如实标注。
- **实现方法：** 先探查并复用现有组件（P01 会话与 SearchTrace、P07 缺口与停止原因、P08 降级标注、P09/P10 记忆与作用域、
  P14-05 的 no-repeat 口径）；新增部分采用 typed contract、配置化参数、版本化与可追溯日志；
  交互文案与选项表**语言中立**（本项目中文环境用中文），不写死行业。
- **测试方式：** Unit + Integration；必须覆盖：同一问题问两次、相似问法、不同问题不触发、跨会话不触发、
  用户拒绝后再问不重复追问、单轮调用跳过、判定抛错不阻断、选项落到配置后**下次运行真的改变行为**、
  幂等（重复触发只记一次）；构造至少一个失败路径。
- **验收标准：** 功能真实运行；自动测试通过；无未解释回归；边界条件符合 V2；**真机/快照上能给出
  "重复提问检出数与交互次数分布"**；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P14-06 clinical/safety weights
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `clinical/safety weights`；不得提前实现后续 Phase。
- **实现方法：** 先探查并复用现有组件；新增部分采用 typed contract、配置化参数、版本化与可追溯日志。
- **测试方式：** Unit + Integration；涉及长任务增加 timeout/retry/idempotency；涉及接口增加兼容回归；构造至少一个失败路径。
- **验收标准：** 功能真实运行；自动测试通过；无未解释回归；边界条件符合 V2；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

## Phase 禁止事项
- 不得硬编码 benchmark 答案或删除/skip 测试。
- 不得把 BLOCKED 标为 PASS。
- 跨 Phase contract 变更必须先写 DECISION_LOG，并运行受影响回归。

## Phase Gate
- [ ] 所有 Must 子任务 PASS
- [ ] Unit tests PASS
- [ ] Integration tests PASS
- [ ] Failure-path tests PASS
- [ ] Compatibility regression PASS
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
