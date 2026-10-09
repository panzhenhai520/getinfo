# Phase 08 — Context Graph & Context Planner

## 边界
按任务组装最小有效 Context。

## 开始前
- [ ] 已读 MASTER_RULES
- [ ] 已读 V2 Architecture 相关章节
- [ ] 已读 IMPLEMENTATION_STATUS
- [ ] 前置 Gate 已通过（Phase 00 除外）

## 子任务
### P08-01 ContextItem/Graph
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `ContextItem/Graph`；不得提前实现后续 Phase。
- **实现方法：** 先探查并复用现有组件；新增部分采用 typed contract、配置化参数、版本化与可追溯日志。
- **测试方式：** Unit + Integration；涉及长任务增加 timeout/retry/idempotency；涉及接口增加兼容回归；构造至少一个失败路径。
- **验收标准：** 功能真实运行；自动测试通过；无未解释回归；边界条件符合 V2；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P08-02 ContextUtility/token budget
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `ContextUtility/token budget`；不得提前实现后续 Phase。
- **实现方法：** 先探查并复用现有组件；新增部分采用 typed contract、配置化参数、版本化与可追溯日志。
- **测试方式：** Unit + Integration；涉及长任务增加 timeout/retry/idempotency；涉及接口增加兼容回归；构造至少一个失败路径。
- **验收标准：** 功能真实运行；自动测试通过；无未解释回归；边界条件符合 V2；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P08-03 Context Pack Builder
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Context Pack Builder`；不得提前实现后续 Phase。
- **实现方法：** 先探查并复用现有组件；新增部分采用 typed contract、配置化参数、版本化与可追溯日志。
- **测试方式：** Unit + Integration；涉及长任务增加 timeout/retry/idempotency；涉及接口增加兼容回归；构造至少一个失败路径。
- **验收标准：** 功能真实运行；自动测试通过；无未解释回归；边界条件符合 V2；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P08-04 counter-evidence reservation
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `counter-evidence reservation`；不得提前实现后续 Phase。
- **实现方法：** 先探查并复用现有组件；新增部分采用 typed contract、配置化参数、版本化与可追溯日志。
- **测试方式：** Unit + Integration；涉及长任务增加 timeout/retry/idempotency；涉及接口增加兼容回归；构造至少一个失败路径。
- **验收标准：** 功能真实运行；自动测试通过；无未解释回归；边界条件符合 V2；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P08-05 Context Gap
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Context Gap`；不得提前实现后续 Phase。
- **实现方法：** 先探查并复用现有组件；新增部分采用 typed contract、配置化参数、版本化与可追溯日志。
- **测试方式：** Unit + Integration；涉及长任务增加 timeout/retry/idempotency；涉及接口增加兼容回归；构造至少一个失败路径。
- **验收标准：** 功能真实运行；自动测试通过；无未解释回归；边界条件符合 V2；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P08-06 selection trace
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `selection trace`；不得提前实现后续 Phase。
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