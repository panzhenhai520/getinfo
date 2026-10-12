# Phase 12 — Episodic Failure Strategy Memory

## 目标
让 **AI 助手**从自己的历史 run 里学经验：哪些检索式对哪类问题有效、哪些信源长期抓不到、
被反爬拦掉的源不要反复重试、哪次失败是因为模型/prompt/语料版本变了而不是方法错了。

- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **定位：** 依赖 Phase 09（记忆底座与 Write Gate）、Phase 11（skill 遥测）；被 Phase 14（动作效用估计）依赖。
- **本阶段不产出"结论"**：episode 与策略记忆只影响**检索与调查怎么走**，绝不进入答案的依据集合。

## 边界
**学习成功和失败检索经验。**

允许做：从 trace 生成 episode、失败记忆、策略记忆、信源可靠性、skill 性能记忆、版本感知重试。
不允许做：答案合成（→ Phase 13）、时效与冲突（→ Phase 10）、作用域隔离强制（→ Phase 15）。

## 开始前
- [ ] 已读 MASTER_RULES
- [ ] 已读 V2 Architecture 相关章节
- [ ] 已读 IMPLEMENTATION_STATUS
- [ ] 前置 Gate 已通过（Phase 00 除外）
- [ ] 已确认 `qa_stage_runs` / `qa_events` 里有可用的分阶段记录与事件

## Must 子任务

### P12-01 episode summarizer from trace
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `episode summarizer from trace`；不得提前实现后续 Phase。
- **实现方法：**
  - 从现役 stage 序列（`qa_contracts.QA_STAGES`：`plan / level1_retrieval / level1_draft / level2_retrieval /
    level2_research / conflict_review / synthesis / citation_validation / completed`）与 `qa_events` 生成 episode：
    问题类型、`scope`、走了哪些 skill、命中哪些信源、缺口怎么收敛、**为什么失败**（stop reason）。
  - episode 是**结构化记录**（不是一段散文）：字段固定，便于后续统计与消解。
  - 摘要生成不得凭空补充 trace 里没有的信息；缺字段写"未知"。
- **测试方式：** Unit（字段完整性；trace 缺阶段时的降级）+ Integration（真实 run 生成 episode 并与 trace 对账）。
- **验收标准：** episode 字段与 trace 一致（可对账）；缺失信息标"未知"而非编造；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P12-02 Failure Memory
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Failure Memory`；不得提前实现后续 Phase。
- **实现方法：**
  - 记录失败模式：检索式无命中、信源长期 404/改版、被反爬拦截、正文质量不合格、证据不足、
    引用校验不通过、模型超时。
  - **必须绑定 `corpus/config/version`**（对应 MASTER_RULES 15）：`corpus_version`、`model_version`、
    `prompt_version`、`config_hash`。任一项变化后该失败记忆**允许重试**，不得永久封杀。
  - 与现役反爬止损能力衔接：被反爬拦到阈值的信源由 `antibot_detector.should_skip_source()` 决定是否跳过派发；
    Phase 12 只**读**该状态并记录为失败模式，不得另造一套封禁名单（避免两处口径打架）。
- **测试方式：** Unit（版本变化后失败记忆失效并可重试）+ Integration（构造一次被反爬拦截的失败 → 记录 → 版本变化 → 允许重试）；
  失败路径：同一失败模式在同一版本下不得被无限重试（要有次数上限）。
- **验收标准：** 失败记忆带齐版本字段；版本变化后可重试有测试证明；不产生与反爬止损冲突的第二套名单；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P12-03 Strategy Memory
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Strategy Memory`；不得提前实现后续 Phase。
- **实现方法：**
  - 记录成功策略：某类问题（例如"某作用域最近的权威来源变化"）用哪种查询改写、哪组信源、是否加语义兜底
    （`intel_topics._classification_admitted` 的 `admission_source='llm_semantic'` 语义兜底路径）更有效。
  - 策略只作为**建议**输入给 Phase 05/07 的规划器与 Phase 14 的动作效用估计；不得强行覆盖规划。
  - 策略记忆必须带生效条件（`scope`、问题类型、`TimeWindow`）与失效标记。
- **测试方式：** Unit（生效条件匹配/不匹配）+ Integration（真实 run 是否采纳策略，采纳后的效果可对比）。
- **验收标准：** 策略可被规划器读到且只在生效条件内生效；采纳与否在 trace 里可见；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P12-04 Source reliability
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Source reliability`；不得提前实现后续 Phase。
- **实现方法：**
  - 按信源（source_id / domain）统计：可抓取率、被反爬率与被拦厂商（`antibot_detector.source_health_report()`）、
    正文质量合格率（`intel_content_quality_gate.py`）、发布时间获取率与精度分布（`publish_time.py`）、
    关键词闸门命中率、证据被引用率（来自 `qa_evidence`）。
  - **按 `scope` 收口**：统计必须带 `industry_pack_id`（`scope` 的当前实现），不得做全库混合口径（现役
    `/api/intel/antibot/health-report` 与 `/api/intel/rss-health` 已是按 `scope` 收口的写法，复用同一口径）。
  - 可靠性分数供 Phase 14 的动作效用估计与 Phase 16 的看板复用，只出一份口径。
- **测试方式：** Unit（分数计算与边界值）+ Integration（按 `scope` 统计与现役反爬健康报告数据对得上）；
  失败路径：样本量不足时返回"数据不足"而不是给出误导分数。
- **验收标准：** 按 `scope` 统计与现役看板口径一致；样本不足有明确标注；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P12-05 Skill performance memory
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Skill performance memory`；不得提前实现后续 Phase。
- **实现方法：** 与 Phase 11 P11-06 的遥测**同一数据源**，按 skill 聚合成功率/延迟/成本/缓存命中，
  并保留版本维度（skill 版本升级后旧统计不得直接沿用）。禁止新造一份统计表。
- **测试方式：** Unit（版本隔离的聚合）+ Integration（遥测写入后聚合结果可复现）。
- **验收标准：** 聚合与遥测一致；升级 skill 后旧统计不误导 Router；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P12-06 version-aware retry
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `version-aware retry`；不得提前实现后续 Phase。
- **实现方法：**
  - 重试判定输入：失败记忆的版本四元组 vs 当前 `corpus/model/prompt/config` 版本。
  - 任一版本变化 → 允许重试并把旧失败记忆标记为"已过期（版本变更）"；全部相同 → 按次数上限抑制重试。
  - 重试次数、退避与幂等键沿用现役 run 幂等约定（`qa_runs` 的 `UNIQUE(owner_user_id, idempotency_key)`），
    不得因为重试而产生重复 run 脏数据。
- **测试方式：** Unit（版本四元组比较矩阵）+ Integration（真实重试路径幂等）；
  失败路径：同一 idempotency_key 重试不得产生第二条 run。
- **验收标准：** 版本变化触发重试有测试；同键重试幂等；旧失败记忆被正确标过期；验收证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

## Phase 禁止事项
- 不得硬编码 benchmark 答案或删除/skip 测试。
- 不得把 BLOCKED 标为 PASS。
- 跨 Phase contract 变更必须先写 DECISION_LOG，并运行受影响回归。
- 不得让 episode/策略记忆进入答案的依据集合。
- 不得在版本未变化时无限重试同一失败模式。
- 不得新建与 `antibot_detector` 冲突的第二套信源封禁名单。

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
