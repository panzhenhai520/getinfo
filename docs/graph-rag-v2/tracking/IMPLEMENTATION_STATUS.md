# IMPLEMENTATION STATUS

> 每次启动从第一个非 PASS 的 Must 项继续。

|ID|任务|状态|证据|最后更新|
|---|---|---|---|---|
|P00-01|环境/目录/依赖探查| IN_PROGRESS | 部分：运行拓扑/工作树/依赖/配置/DB/镜像锁 6 份 manifest 已在（tools/capture_*.py + tests/test_*baseline*.py）；缺 QA/V2 模块与依赖清单（按 D-005 补） | 2026-10-09 |
|P00-02|冻结原 API contract| IN_PROGRESS | 部分：版本锚点在（qa_contracts.py:19 unified-qa-v1、qa_schema.py:10 unified-qa-schema-v5、7 个 schema + 守门用例）；缺冻结快照与变更流程 | 2026-10-09 |
|P00-03|建立固定 benchmark| IN_PROGRESS | 部分：config/qa_acceptance_questions.json(12 题) + tools/qa_retrieval_acceptance.py + 历史 1 条；缺 benchmark_version、语料快照绑定、golden 答案 | 2026-10-09 |
|P00-04|记录 Recall/Citation/Quality/Latency/Cost baseline| IN_PROGRESS | 部分：Latency 完整（baseline/performance-baseline.json + tools/benchmark_stage1.py）；检索侧 hit/grounded/graph_rate 有；缺 Recall@K、citation precision/recall、Quality、Cost | 2026-10-09 |
|P01-01|Node/Edge contract| IN_PROGRESS | 部分：两套事实上的 node/edge（qa_reasoning.py:206 Claim–Evidence 图；kg_builder.py:51-53 事件/属性/共现）都不在契约层；缺 Execution Graph Node 契约 | 2026-10-09 |
|P01-02|ResearchSession/SearchTrace| IN_PROGRESS | 部分：qa_runs / qa_reasoning_traces / qa_stage_runs / qa_events / qa_audit_events 在；缺 gap_id/route/results/accepted/rejected/new_claims/resolved_gap，且 trace_id 当前 == run_id（qa_orchestrator.py:112） | 2026-10-09 |
|P01-03|版本字段 corpus/retrieval/prompt/model| IN_PROGRESS | 部分：检索侧 qa_retrieval_cache.kb_version 在（qa_schema.py:243）；qa_runs/qa_contracts 缺 corpus_version/model_version/prompt_version/config_hash | 2026-10-09 |
|P01-04|idempotency/trace/round/node-run| IN_PROGRESS | 部分：idempotency 已达标（qa_schema.py:66/74 + qa_storage.py:121 ON CONFLICT）；round/hop 有；node-run 完全缺失（无 node_id/node_kind/parent_node_id） | 2026-10-09 |
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
|P04-04|Structured/DB/EMR adapter|NOT_STARTED|||
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
|P13-02|citation binding|NOT_STARTED|||
|P13-03|counter-evidence/unresolved gaps|NOT_STARTED|||
|P13-04|Final Verifier|NOT_STARTED|||
|P13-05|local retry|NOT_STARTED|||
|P13-06|insufficient-evidence output|NOT_STARTED|||
|P14-01|Action schema|NOT_STARTED|||
|P14-02|ExpectedGapReduction utility|NOT_STARTED|||
|P14-03|KB/EMR/DB/Web routes|NOT_STARTED|||
|P14-04|ask-patient route|NOT_STARTED|||
|P14-05|Context Gap no-repeat|NOT_STARTED|||
|P14-06|clinical/safety weights|NOT_STARTED|||
|P15-01|scope enforcement|NOT_STARTED|||
|P15-02|encounter isolation|NOT_STARTED|||
|P15-03|longitudinal freshness|NOT_STARTED|||
|P15-04|SafetyCritical override|NOT_STARTED|||
|P15-05|privacy-minimal logs|NOT_STARTED|||
|P15-06|audit/replay|NOT_STARTED|||
|P16-01|Execution View|NOT_STARTED|||
|P16-02|Evidence View|NOT_STARTED|||
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