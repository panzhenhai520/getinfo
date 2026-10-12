# IMPLEMENTATION STATUS

> 每次启动从第一个非 PASS 的 Must 项继续。
> **当前全局状态：所有阶段均为「未开始」（NOT_STARTED）** —— 本文件为改写后的初始台账。
> 状态只允许：NOT_STARTED / IN_PROGRESS / PASS / FAIL / BLOCKED / DEFERRED。

|ID|任务|状态|证据|最后更新|
|---|---|---|---|---|
|P00-01|环境/目录/依赖探查|NOT_STARTED|||
|P00-02|冻结原 API contract|NOT_STARTED|||
|P00-03|建立固定 benchmark|NOT_STARTED|||
|P00-04|记录 Recall/Citation/Quality/Latency/Cost baseline|NOT_STARTED|||
|P01-01|Node/Edge contract|NOT_STARTED|||
|P01-02|ResearchSession/SearchTrace|NOT_STARTED|||
|P01-03|版本字段 corpus/retrieval/prompt/model|NOT_STARTED|||
|P01-04|idempotency/trace/round/node-run|NOT_STARTED|||
|P02-01|Claim/Entity/Evidence/Source/Span schema|NOT_STARTED|||
|P02-02|provenance|NOT_STARTED|||
|P02-03|seen 与 confirmed|NOT_STARTED|||
|P02-04|fingerprint/去重|NOT_STARTED|||
|P03-01|relevance/reranker|NOT_STARTED|||
|P03-02|entailment/NLI|NOT_STARTED|||
|P03-03|entity/time/negation/source verifier|NOT_STARTED|||
|P03-04|EvidenceScore/reason/cache|NOT_STARTED|||
|P04-01|BM25 Hunter|NOT_STARTED|||
|P04-02|Semantic Hunter|NOT_STARTED|||
|P04-03|Graph Hunter adapter|NOT_STARTED|||
|P04-04|Structured/DB/内部结构化数据 adapter|NOT_STARTED|||
|P04-05|Query Expansion|NOT_STARTED|||
|P04-06|fan-out/timeout/fallback|NOT_STARTED|||
|P05-01|Query Interpreter|NOT_STARTED|||
|P05-02|Subquestion/Claim decomposition|NOT_STARTED|||
|P05-03|dependency/parallel groups|NOT_STARTED|||
|P05-04|Fast/Standard/Deep|NOT_STARTED|||
|P05-05|budget/Execution Graph|NOT_STARTED|||
|P06-01|Evidence Graph repository/API|NOT_STARTED|||
|P06-02|SUPPORTS/REFUTES/DEPENDS/CONTRADICTS|NOT_STARTED|||
|P06-03|claim coverage|NOT_STARTED|||
|P06-04|Contradiction detector/resolver|NOT_STARTED|||
|P07-01|Gap taxonomy/priority|NOT_STARTED|||
|P07-02|suggested route/evidence requirement|NOT_STARTED|||
|P07-03|Next-hop Planner|NOT_STARTED|||
|P07-04|seen dedupe|NOT_STARTED|||
|P07-05|no-gain convergence|NOT_STARTED|||
|P07-06|stop reasons|NOT_STARTED|||
|P08-01|ContextItem/Graph|NOT_STARTED|||
|P08-02|ContextUtility/token budget|NOT_STARTED|||
|P08-03|Context Pack Builder|NOT_STARTED|||
|P08-04|counter-evidence reservation|NOT_STARTED|||
|P08-05|Context Gap|NOT_STARTED|||
|P08-06|selection trace|NOT_STARTED|||
|P09-01|memory schema/version/relations|NOT_STARTED|||
|P09-02|memory types|NOT_STARTED|||
|P09-03|Write Gate|NOT_STARTED|||
|P09-04|Recall API|NOT_STARTED|||
|P09-05|lifecycle|NOT_STARTED|||
|P09-06|provenance/vector+graph+relational|NOT_STARTED|||
|P10-01|freshness/TTL|NOT_STARTED|||
|P10-02|source-version detection|NOT_STARTED|||
|P10-03|MEMORY_HINT revalidation|NOT_STARTED|||
|P10-04|MemoryContradiction|NOT_STARTED|||
|P10-05|SUPERSEDED_BY|NOT_STARTED|||
|P10-06|revoke/high-risk hook|NOT_STARTED|||
|P11-01|Skill schema/version|NOT_STARTED|||
|P11-02|Registry|NOT_STARTED|||
|P11-03|Router|NOT_STARTED|||
|P11-04|permission/cost/latency|NOT_STARTED|||
|P11-05|on-demand instruction|NOT_STARTED|||
|P11-06|performance telemetry|NOT_STARTED|||
|P12-01|episode summarizer from trace|NOT_STARTED|||
|P12-02|Failure Memory|NOT_STARTED|||
|P12-03|Strategy Memory|NOT_STARTED|||
|P12-04|Source reliability|NOT_STARTED|||
|P12-05|Skill performance memory|NOT_STARTED|||
|P12-06|version-aware retry|NOT_STARTED|||
|P13-01|claim-aware composer|NOT_STARTED|||
|P13-02|citation binding（复用 citation_validation）|NOT_STARTED|||
|P13-03|counter-evidence/unresolved gaps|NOT_STARTED|||
|P13-04|Final Verifier|NOT_STARTED|||
|P13-05|local retry|NOT_STARTED|||
|P13-06|insufficient-evidence output|NOT_STARTED|||
|P14-01|Action schema（五类领域无关动作）|NOT_STARTED|||
|P14-02|ExpectedGapReduction utility|NOT_STARTED|||
|P14-03|内部证据库/结构化数据/公开网络/趋势信号 routes|NOT_STARTED|||
|P14-04|ask-user route（向用户澄清）|NOT_STARTED|||
|P14-05|Context Gap no-repeat|NOT_STARTED|||
|P14-06|safety/scope weights（结论安全与作用域权重）|NOT_STARTED|||
|P15-01|记忆作用域强制（scope enforcement）|NOT_STARTED|||
|P15-02|隔离强制（isolation enforcement）|NOT_STARTED|||
|P15-03|长期时效与敏感源标注（longitudinal freshness）|NOT_STARTED|||
|P15-04|结论安全 override（SafetyCritical override）|NOT_STARTED|||
|P15-05|最小化日志（privacy-minimal logs）|NOT_STARTED|||
|P15-06|可撤销与可审计（audit/replay）|NOT_STARTED|||
|P16-01|Execution View|NOT_STARTED|||
|P16-02|Evidence View（含已交付的证据解析/词元影响力）|NOT_STARTED|||
|P16-03|Context Inspector|NOT_STARTED|||
|P16-04|Memory View|NOT_STARTED|||
|P16-05|Search Trace|NOT_STARTED|||
|P16-06|cost/latency/token/cache/skill dashboard|NOT_STARTED|||
|P17-01|E2E suite|NOT_STARTED|||
|P17-02|ablation ladder|NOT_STARTED|||
|P17-03|metric report|NOT_STARTED|||
|P17-04|compatibility regression|NOT_STARTED|||
|P17-05|migration rollback|NOT_STARTED|||
|P17-06|final report/release|NOT_STARTED|||

## 预置 BLOCKED 提示（不是 PASS 的替代，是必须正视的现实）
- **P06-01～P06-04、P07-03、P17-02（L7 图收益消融）** 等依赖图结构的 Must，
  在事件三元组填充率仍为 0.0%（7022 篇仅 3 行）、实体归一 0 行时应保持 BLOCKED。
  **解除阈值：`intel_article_events` 填充率 ≥60%**（低于 60% 时图上大量实体缺边，多跳会系统性断在半路，
  测出的"建图收益"会是负的）；复核工具 `tools/structured_artifact_coverage_audit.py`；
  前置任务见 `docs/爬虫改进实施步骤.md` 阶段 7/8。详见 `tracking/BLOCKERS.md` B-01。
- 其余不依赖图的能力（检索、证据解析、词元影响力、客观趋势、发布时间采集、反爬止损、记忆底座与作用域隔离）
  现在即可推进。
