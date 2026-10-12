# Phase 17 — E2E Ablation Release

## 目标
证明整套 Graph-RAG V2 **真的让 AI 助手变聪明了**，并且能安全发布、能回滚：
端到端跑通、做消融阶梯量化每个模块的贡献、出指标报告、做兼容回归、准备迁移与回滚。

- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **定位：** 依赖 Phase 00–16 全部 Gate；是发布前的最后一道。
- **现实前提（必须先读）：** 结构化产物填充率极低——实测 A 机 7022 篇里**事件三元组仅 3 行（0.0%）、
  实体归一 0 行**。**在这个状态下建图＝建空图**，因此：
  - 依赖图的消融档（Evidence Graph / Context Graph / Memory Graph 的图结构收益）
    **必须等前置完成**：`docs/爬虫改进实施步骤.md` 阶段 7（事件抽取回填）与阶段 8（建图）；
  - 不依赖图的能力（证据池检索、证据解析、词元影响力、客观趋势、发布时间采集）**现在就可以做消融**，
    本阶段应当**先出这部分可交付的结论**，把图相关结论标为 BLOCKED 并写明解除条件；
  - **严禁**用桩数据/合成数据把图收益"测出来"（MASTER_RULES：不得用 mock 冒充真实集成验收）。

## 边界
**证明收益、兼容、可回滚发布。**

允许做：E2E 套件、消融阶梯、指标报告、兼容回归、迁移与回滚、最终发布报告。
不允许做：新增功能（发现缺陷回到对应 Phase 修）。

## 开始前
- [ ] 已读 MASTER_RULES
- [ ] 已读 V2 Architecture 相关章节
- [ ] 已读 IMPLEMENTATION_STATUS
- [ ] 前置 Gate 已通过（Phase 00–16）
- [ ] 已重跑并记录 Phase 00 冻结的 benchmark（**冻结后不许为提高成绩偷偷改**）
- [ ] 已确认当前事件三元组填充率与实体归一填充率（可用 `tools/structured_artifact_coverage_audit.py` 复核），
      并据此判定哪些消融档可做、哪些必须 BLOCKED

## Must 子任务

### P17-01 E2E suite
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `E2E suite`；不得提前实现后续 Phase。
- **实现方法：**
  - 端到端场景（每个必须是**真实数据 + 真实接口**）：
    1. AI 助手对话问「某 `scope` 内某个实体最近有什么关键变化」→ 走完 `POST /api/chat/send` → 出带引用答案；
    2. 同上问题在 **A 机（无 RAGFlow 一级检索模式）** 与 **B 机（RAGFlow 网关 `192.168.0.64:8106`）** 各自跑通；
    3. 证据不足场景 → 走"证据不足输出"且 0 无引用结论；
    4. 跨 `scope`/跨用户越权场景 → 0 命中且有审计留痕（Phase 15）；
    5. 反爬拦截场景 → 信源被止损跳过，轨迹与看板可见（Phase 16 P16-05）。
  - 断言必须落在"事实"上：引用可回溯、终验记录存在、审计事件存在、指标可对账。
  - 复跑现役关键测试套件作为回归的一部分（`tests/test_qa_attribution.py`、
    `tests/test_retrieval_evidence_policy.py`、`tests/test_antibot_detector.py` 等）。
- **测试方式：** 本身即 E2E；另需失败路径（网络/反爬/超时下的行为被断言）。
- **验收标准：** 五个场景真实通过；每个场景有可复核的产物（截图/日志/接口响应）；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P17-02 ablation ladder
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `ablation ladder`；不得提前实现后续 Phase。
- **实现方法：** 逐级关闭模块，量化每级贡献（每级都要用同一 benchmark、同一数据快照）：
  | 档 | 关闭什么 | 现在可否做 | 说明 |
  |---|---|---|---|
  | L0 | 全关（基线问答） | 可做 | 现役链路 |
  | L1 | 开证据层 + Verifier | 可做 | Phase 02–03 |
  | L2 | 开并行检索舰队 | 可做 | Phase 04 |
  | L3 | 开 Gap 驱动多跳 + 上下文包 | 可做 | Phase 05–08（不含图） |
  | L4 | 开 Memory（含复验） | 可做 | Phase 09–10 |
  | L5 | 开 Skill Router | 可做 | Phase 11 |
  | L6 | 开统一采集动作 + 安全权重 | 可做 | Phase 14–15 |
  | L7 | 开 Evidence Graph / Context Graph 图结构 | **前置未完成 → BLOCKED** | 需事件三元组回填（阶段 7）+ 建图（阶段 8） |
  - **判定 `STRUCTURED_DATA`（内部数据源侧）动作的贡献时**必须同时报该 `scope` 的填充率，
    避免把"填充率 0"误读成"该动作没用"。
  - 每级记录：Recall@K、citation precision/recall、unsupported claim rate、
    latency、tokens、cost、cache hit（口径见 `reference/METRICS.md`）。
  - 若某级"无收益"，**如实报告无收益**，不得调参把结果做好看。
- **测试方式：** 消融脚本可复跑；每级有独立产物目录；失败路径：某级脚本失败时如实记 FAIL，不许跳过。
- **验收标准：** L0–L6 消融有真实数字；L7 明确标 BLOCKED 并写明解除条件；
  填充率随消融一起报告；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P17-03 metric report
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `metric report`；不得提前实现后续 Phase。
- **实现方法：**
  - 指标报告覆盖 `reference/METRICS.md` 全部分组：Retrieval / Evidence / Agent / Context / Memory /
    System / **结论安全与作用域隔离**；每项给 before/after 与样本量。
  - 必须含本项目特有的采集侧现实指标：发布时间精度分布（`exact/date/url/discovered` 覆盖）、
    内部证据库准入率、被反爬止损的信源数与影响面、结构化产物填充率
    （`intel_article_events`，并同时报出 B-01 的 **≥60%** 阈值进度）。
  - **两组趋势口径必须并排报告**：客观口径（`GET /api/intel/trends/objective`，由主线实现，
    基于 `intel_candidates` 全量标题算独立媒体数）与"我们收下的热度"（`/api/intel/trends`，
    `intel_topic_trends` 口径），用于量化信源偏差。
    客观接口未就绪时如实标"接口未就绪"，不得用我方口径冒充客观口径。
  - 报告不得只报好的：**无收益项、样本不足项、BLOCKED 项一并列出**。
  - 报告写入 tracking 与发布说明，不新建额外 `.md` 文档（与本项目"汇报在聊天/既有文档中完成"的规则一致）。
- **测试方式：** 指标可从原始数据复算（提供复算命令）；失败路径：样本量不足时必须标"样本不足"。
- **验收标准：** 全部指标组有数字或明确的"样本不足/待确认"；before/after 可复算；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P17-04 compatibility regression
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `compatibility regression`；不得提前实现后续 Phase。
- **实现方法：**
  - 冻结的原 API 契约（Phase 00）逐条回归：`POST /api/chat/send`、`/api/qa/v1/*`（含
    `/runs/<run_id>/attribution`、`/token-influence`、`/metrics`、`/audit`）、`/api/chat/history/*`、
    `/api/intel/*`（含 `/antibot/health-report`、`/rss-health`、`/buzzing-radar`、`/trends`），
    以及**主线新增的 `/api/intel/trends/objective`**（交付后纳入回归；**交付前该项记 BLOCKED，不得记 PASS**）。
  - 前端回归：`/`、`/panython/admin`、`/trends` 三页真实渲染 + 关键交互
    （【证据解析】【词元影响力】按钮、反爬看板、RSS 体检按钮、雷达、趋势曲线与客观曲线）。
  - SSE 协议兼容（`qa_contracts.QA_SSE_PROTOCOL_VERSION`）：事件类型与阶段名不得静默变更。
  - 需要变更契约时走 compatibility adapter，并在 DECISION_LOG 留记录。
- **测试方式：** 契约测试 + 前端 E2E；失败路径：任一原接口行为变化必须显式失败而不是"看起来还能用"。
- **验收标准：** 全部原接口与三页回归通过；SSE 协议未破坏；无未解释回归；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P17-05 migration rollback
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `migration rollback`；不得提前实现后续 Phase。
- **实现方法：**
  - 汇总 Phase 09–15 新增的表/字段/索引（记忆三表、链接、事件、图结构扩展等），
    每条 migration 都要有反向 migration，且已在真实库上演练过回滚。
  - **实验与 trace 不覆盖历史**（MASTER_RULES 9）：回滚不得删除既有 run/事件/审计数据。
  - 回滚后系统必须仍能提供服务（降级到 Phase 00 基线能力），而不是直接起不来。
  - 记录回滚演练的耗时与风险点（长事务、索引重建）。
- **测试方式：** 在副本库上真实执行"升级 → 回滚 → 再升级"；失败路径：任一步失败即标 FAIL。
- **验收标准：** 升级/回滚/再升级全程通过；回滚后服务可用；历史数据未丢失；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P17-06 final report/release
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `final report/release`；不得提前实现后续 Phase。
- **实现方法：**
  - 发布报告内容：实现了什么、关键指标 before/after、**已知限制**（明确含"事件三元组填充率 0.0%
    导致图相关收益无法度量"）、BLOCKED 项与解除条件、部署步骤（A 机 / B 机差异）、
    回滚步骤、监控要点（`/api/qa/v1/health`、`/api/qa/v1/metrics`、反爬看板）。
  - 发布前检查：结论安全与作用域隔离 blocker 归零（Phase 15 Gate）；
    反爬导致的信源缺失有可见解释；发布时间精度分布已知。
  - 报告写入 tracking 与既有文档位置；**不新建额外交付 `.md`**。
- **测试方式：** 发布检查清单逐项有证据；失败路径：任一 blocker 未归零则不得宣布发布完成。
- **验收标准：** 报告完整（含限制与阻塞）；部署与回滚步骤可照做；发布检查清单全绿；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

## Phase 禁止事项
- 不得硬编码 benchmark 答案或删除/skip 测试。
- 不得把 BLOCKED 标为 PASS。
- 跨 Phase contract 变更必须先写 DECISION_LOG，并运行受影响回归。
- 不得用桩数据/合成数据充当图收益的验收证据。
- 不得在 benchmark 冻结后为提高成绩偷偷修改 benchmark 或口径。
- 不得只报有收益的指标而隐去无收益/BLOCKED/样本不足项。
- 不得在结论安全或作用域隔离 blocker 未归零时宣布发布完成。

## Phase Gate
- [ ] 所有 Must 子任务 PASS（或经 DECISION_LOG 明确记录的 BLOCKED + 解除条件）
- [ ] Unit tests PASS
- [ ] Integration tests PASS
- [ ] Failure-path tests PASS
- [ ] Compatibility regression PASS
- [ ] 迁移与回滚演练 PASS
- [ ] IMPLEMENTATION_STATUS / ACCEPTANCE_MATRIX 已更新
- [ ] 无未处理高优先级 blocker（含结论安全/作用域隔离 blocker 必须为 0）

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
