# IMPLEMENTATION STATUS

> 每次启动从第一个非 PASS 的 Must 项继续。

|ID|任务|状态|证据|最后更新|
|---|---|---|---|---|
|P00-01|环境/目录/依赖探查| PASS | tools/qa_baseline_inventory.py + tests/test_qa_baseline_inventory.py(8 例)；产物 baseline/qa-baseline-inventory.json（captured_at_utc=2026-10-09T15:58:47Z）：模块 20/20 存在、依赖边 129 条无自环、验收 acceptance.passed=true | 2026-10-09 |
|P00-02|冻结原 API contract| PASS | 七个契约 schema 指纹已冻结并落盘（EVIDENCE 370301331c02c738 / CLAIM 06fcdb02441248b2 / CONFLICT aabd3259b07f9a3e / LEVEL1 7e864764429db111 / LEVEL2 a0484f894e7bc9d6 / FINAL_ANSWER 4d1efa54ca1cbc1a / QA_EVENT 59358bfa88a6c6af）；版本锚点 unified-qa-v1 / qa-sse-v1 / unified-qa-schema-v6 / graph-contract-v1；守门用例断言指纹稳定；变更流程见 DECISION_LOG | 2026-10-09 |
|P00-03|建立固定 benchmark| PASS | config/qa_acceptance_questions.json 冻结为信封形态（benchmark_version=qa-acceptance-v1、corpus_snapshot_id=pending:first-run+真实快照写入历史、generated_at）；题目内容与 git HEAD 逐题相等（12 题 id 序列不变）；tools/qa_retrieval_acceptance.py 支持两形态解析；tests/test_qa_acceptance_benchmark_version.py 13 例 | 2026-10-09 |
|P00-04|记录 Recall/Citation/Quality/Latency/Cost baseline| PASS | Latency 已有（baseline/performance-baseline.json）；Recall@K=0.8333（k=12，词级 micro）/citation_precision=0.7719 / citation_recall=0.8333（真跑留档）；**Cost 链路已打通**：qa_level1/qa_synthesis 归一并累加 usage、qa_orchestrator 只写 details['token_usage']（不从阶段输出留键，避免 FINAL_ANSWER_SCHEMA additionalProperties:False 判失败），并新增 QA_STREAM_INCLUDE_USAGE（默认开，流式末块取 usage；400 点名 stream_options 时摘参重发一次）；真端点实测 tokens_in=18/tokens_out=1/tokens_total=19。**Quality（unsupported claim rate / entailment）按通用包规划属阶段 03**，本阶段只登记缺口 | 2026-10-09 |
|P01-01|Node/Edge contract| PASS | qa_graph_contracts.py（节点类型/边关系/Claim–Evidence 关系/检索通道/失败策略五值/停止原因五值/五个可校验 schema + validate()）；tests/test_qa_graph_contracts.py 12 例（含与 kg_builder 常量逐字相等的等价断言） | 2026-10-09 |
|P01-02|ResearchSession/SearchTrace| PASS | qa_reasoning_traces 补齐 gap_id/route/results/accepted/rejected/new_claims/resolved_gap；qa_storage.record_reasoning_trace 新增 7 个可选 kwarg（旧签名兼容）；qa_pipeline._record 回填 route/计数（回执与返回结构一字未变）；trace_id==run_id 的语义已在 qa_orchestrator 注释写明；tests/test_qa_phase01_schema.py 覆盖 | 2026-10-09 |
|P01-03|版本字段 corpus/retrieval/prompt/model| PASS | qa_runs 新增 corpus_version/model_version/prompt_version/config_hash（TEXT DEFAULT ''，ADD COLUMN 可回滚）；create_run 写入四元组；真跑值 corpus_version=ba768b331fd86cec803be04e、prompt_version=qa-research-notes-v3+qa-adjudication-v1、config_hash=c75da38ad7bbfa98；model_version 已由 qa_gateway 传入草稿角色 model_id（此前恒空的缺口已补） | 2026-10-09 |
|P01-04|idempotency/trace/round/node-run| PASS | 幂等保持（qa_storage.py:121 ON CONFLICT 回读既有 run）；node-run 载体落地：qa_stage_runs 新增 node_id/node_kind/parent_node_id + round_index，node_id 不传时自动等于 stage（现有调用点零改动）、显式 node_id 不会被状态更新打回；新建索引 idx_qa_stage_runs_node；tests/test_qa_phase01_schema.py 12 例 | 2026-10-09 |
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