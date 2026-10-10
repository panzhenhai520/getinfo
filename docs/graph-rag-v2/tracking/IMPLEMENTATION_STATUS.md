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
|P02-01|Claim/Entity/Evidence/Source/Span schema| PASS | 证据对象契约 + 最小 span：qa_evidence.py（minimal_quote_span 保证 span.quote 与正文一致）、qa_graph_contracts 五个新 schema + 递归校验；EVIDENCE_SCHEMA 未动、七指纹复算未变；43 例 | 2026-10-10 |
|P02-02|provenance| PASS | provenance：source_identity（edge>doc+chunk>doc>article>web>url>ref）+ evidence_provenance（run/stage/route/round/corpus_version）+ annotate 落 metadata.evidence_layer；多跳每跳与 level2 均已接线（真跑 annotated=25 / recorded=34） | 2026-10-10 |
|P02-03|seen 与 confirmed| PASS | seen 与 confirmed：新表 qa_evidence_seen（v7），三轴作用域隔离；被拒证据留身份；TTL 清理 prune_seen_evidence（QA_EVIDENCE_SEEN_TTL_DAYS 默认 30、开关默认关，挂在 task_cleanup）；真跑本 run 作用域 9 行（confirmed 8 / rejected 1）、其它作用域 0 行；20+7 例 | 2026-10-10 |
|P02-04|fingerprint/去重| PASS | fingerprint/去重：三种指纹 + 跨轮去重（默认只丢上轮被拒来源）；整批与逐跳两条路径都保持 rejected 身份（不降级为 seen，已有源码级守门用例）；回执键集与接线前逐字相同；11+12 例 | 2026-10-10 |
|P03-01|relevance/reranker| PASS | qa_verifier（P03-01 段）：`relevance_score`（标题覆盖 ×2.5 / 正文前段 ×1.25，0.55/0.45 加权，知识库相似度仅 0–1 区间才当相似度用）+ `rerank_evidence`（按核验分稳定重排，同分保序）；接在 `_apply_evidence_layer` 核验之后（`QA_VERIFIER_RERANK` 默认开，审计记 `reordered`）。tests/test_qa_phase03_verifier.py 5 例 + 接线 2 例；真机 12 条证据重排实测重排 2 条 | 2026-10-10 |
|P03-02|entailment/NLI| PASS | qa_verifier（P03-02 段）：**纯规则**方向性蕴含（claim 实词被证据覆盖比例：2 元组 ×0.65 + 3 元组 ×0.35）；可插拔后端 `register_entailment_backend` + `QA_NLI_BACKEND`，未注册名/后端抛错/未注册一律保守回落规则后端并记 `nli_backend_fallback`（SUPPORTED 降 QUALIFIED）；**证据不足永远给 UNVERIFIED/QUALIFIED，绝不给 SUPPORTED**。AST 级守门用例断言核验层不 import requests/urllib3/httpx/socket、源码内零 http(s) 字面量（GPU 端点禁用约束） | 2026-10-10 |
|P03-03|entity/time/negation/source verifier| PASS | qa_verifier（P03-03 段）：实体一致性（required_entities 必须出现在证据实体表/文本里，不做新 NER）、时间适用性（早于 claim.valid_from / 晚于 valid_to 判不适用，时效半衰期 180 天）、否定一致性（多字否定词 + **仅在与结论最相关的分句上判** + 否定词前后 8 字必须出现对方实词才敢判 REFUTED）、来源核验（权威性、二手转述、因果夸大、relationship=contradicts 降级）。真机回放纠正了两处误判：整篇 900 字搜否定词导致 8 条 claim 里 6 条被误判 conflicted → 修正后 0 条；"逾期不予受理"程序性子句误判反证 → 修正后判 SUPPORTED | 2026-10-10 |
|P03-04|EvidenceScore/reason/cache| PASS | qa_verifier（P03-04 段）：`evidence_score`（§11 加权口径，五项和为 1.0 + contradiction_risk 减项，钳制 [0,1]，逐项 `QA_VERIFIER_W_*` 可配）、21 个原因码 + 中文解释（`reason_text`）、`VerificationCache`（进程内 TTL+LRU + 可选落库**复用既有 `qa_retrieval_cache` 表**，namespace=qa_verification，**零迁移**）；`QaStore.get/put_verification_cache` 两个新方法。缓存键含权威性/发布时间/relationship/doc_type/要求实体/有效期/独立性/配置指纹（早期只用来源+正文，实测把 authority 从 60 改成 None 仍命中旧结论）。真机回放 12 条证据：第一遍 73–219ms → 第二遍 1.8–4.2ms（40–68x，命中率 0.5）；金标单条 0.9–2.4ms（调优前 3.6ms：OpenCC 归一化缓存 + 分句先整段归一化，判定结果逐条不变） | 2026-10-10 |
|P04-01|BM25 Hunter| PASS | `qa_hunters.BM25Hunter`（P04-01 段）。**复用**：`ArticleRetriever._rows()`（候选池 + 全部既有闸门）、`apply_time_gate`（时间梯子）、`_article_evidence`（证据成型，键集与既有通道逐字一致）、`_semantic_anchor_terms`（既有"什么算实词"口径）。**新增**：Okapi BM25（k1=1.5/b=0.75 可配）+ **全池 df 的 idf**（只在剪枝候选上算 idf 会反过来奖励泛词——实测把「限时权益」促销文顶到「蔚来」前面）+ 候选剪枝（不含任何查询词的文档 BM25 恒为 0，剪枝等价）+ 文档词表缓存。真跑（A 机只读快照 239 篇 / 7 题）：BM25 通道 6/7 有命中，比亚迪·蔚来两题 citation matched 12/12 与基线持平。测试 30 例（含手算 BM25 复算、时间硬过滤、空查询失败路径、泛词不得盖过实体） | 2026-10-10 |
|P04-02|Semantic Hunter| PASS | `qa_hunters.SemanticHunter`（P04-02 段）。**复用库内已有向量** `intel_article_embeddings`（同一条 SQL、同样 L2 归一化；`database=None` 时委托既有 `chat_api._load_vector_matrix`）。**零端点调用**（GPU 与语音机器人共用、已停用）：查询向量用**离线质心**（词面种子文章向量加权平均），能力边界写在 D-018。真跑快照 240 条 1024 维向量：语义通道 **7/7 都取到证据**，`semantic_only`（词面不命中、靠向量找回）在题内有 5 条；无向量/无种子/loader 抛错三条失败路径都有用例。守卫：AST 断言本模块不 import requests/urllib3/httpx/socket、源码零 http(s) 字面量与 embedding 客户端调用点 | 2026-10-10 |
|P04-03|Graph Hunter adapter| PASS | `qa_hunters.GraphHunter`：**纯适配器**（新增约 30 行），图逻辑一行没重写——直接调既有 `qa_retrieval.graph_evidence()`（事件边/属性边 + 意图权重 + 属性边有效期过滤），只加统一契约、`builder` 注入点与 DEGRADE 失败语义（§28「Graph DB unavailable → DEGRADE」）。真跑：快照导出的 238 条事件 + 240 条属性在回放库按既有 `KnowledgeGraphBuilder` 重建图，比亚迪/蔚来两题取到 4 / 3 条图证据且可回溯 100%；图库抛错走 `safe_run` → `status=error` 不冒泡（有用例） | 2026-10-10 |
|P04-04|Structured/DB/EMR adapter| PASS | `qa_hunters.StructuredHunter`：① 政策登记表走**既有** `ArticleRetriever._policy_exact_evidence()`（通道名沿用 `policy_exact`，政策行命中用例）；② 元数据闸门（时间/权威度/文档类型/域名/分类，白名单键，**没给条件就不产证据**，避免变成第二遍关键词检索）；③ `structured_providers` 注入点接外部业务库（通用包 §8.6 的 SQL/API/EMR/HIS/LIS/PACS 按 D-002 抽象为"内部证据库"），单源失败只记 `stats.providers[].error`、不影响其它源（Failure Isolation 用例）。**本轮不新增任何网络依赖**；快照内 0 条政策登记行 → 7/7 `empty`（如实的无命中，不是错误） | 2026-10-10 |
|P04-05|Query Expansion| PASS | `qa_hunters.QueryExpansionHunter`：**只产词、不产证据**（§8.5 硬约束，用例断言 `evidence == []`）。词源逐条可追溯（`original`/`normalize`/`entity_alias`/`graph_neighbor`/`anchor`/`token`）：复用 `qa_query_normalize.expand_terms/expand_query`（繁简 + 别名词表）、`_semantic_anchor_terms`（jieba 实词）、`KnowledgeGraphBuilder.neighborhood()`（图谱邻居 = 相关实体）；输入信息量排序后再截断，稀缺来源（别名/图谱邻居）不会被 2 字碎片挤掉。真跑 7/7 都产出扩展词表与扩展检索式（含 4 个首题图谱邻居来源计数） | 2026-10-10 |
|P04-06|fan-out/timeout/fallback| PASS | `qa_hunter_fleet.HunterFleet`（P04-06 段）：**有界**线程池（`QA_HUNTER_FLEET_MAX_WORKERS` 默认 4）、单 Hunter 超时（默认 8s，**按 Hunter 真正开始执行起算**，排队不计入）、`RETRY 1`（§28，只读天然幂等，超时/抛错都重试一次）、`FALLBACK`（语义通道降级 → 兜底 BM25，并标记 `stats.fallback.satisfied`）、**总预算**（默认 20s）+ **部分结果回退**（耗尽预算 → `partial=true`/`stop_reason=BUDGET_EXHAUSTED`，已完成通道的证据照常返回，未完成的记 `budget_exhausted`）、失败隔离（任何通道抛错都收敛成契约对象，不影响其它通道）、候选池一次加载多通道共用（`pool_loads` 可审计）、池按 pack+TTL(180s) 失效。真跑快照 7 题：0 降级、0 部分结果，墙钟 1357ms vs 通道累计 3285ms（**并行增益 1928ms**）；22 例测试覆盖并发/上限/超时重试/回退/预算/隔离 | 2026-10-10 |
|P05-01|Query Interpreter| PASS | 新模块 `qa_query_interpreter.py`（**规则实现 + 可插拔注入点**，零模型调用）：§6 的 9 值意图**全部可命中**（复用 `qa_planner._CATEGORY_RULES/_COMPARE_RE/_CONFLICT_RE/_MULTI_HOP_CATEGORIES/_date_scope/_split_subquestions/_needs_article_retrieval`，只新增"机制/通路"与"谁更/哪个更"两条最小正则）、复杂度三档（`complexity_reasons` 可复算：多跳类别/比较冲突/≥3 实体或问句→deep；单问句+简单类别+≤60 字→simple）、答案形态、`freshness_required`、`constraints` 投影（question_plan/axes/requested_sources）、`required_claims`（每条子问题一条 + 一条反证 `plan_only=True`）；`register_query_interpreter` + `QA_QUERY_INTERPRETER` 三条失败路径（未注册名/后端抛错/返回非法载荷）**全部保守回落 rules** 并写明原因。tests/test_qa_phase05_interpreter.py **23 例** | 2026-10-10 |
|P05-02|Subquestion/Claim decomposition| PASS | `qa_execution_graph.build_research_plan`：hop → `sub_question`（`sq:<hop.id>` 一对一，**不新建分解器**）、Claim（每条子问题一条 + 反证那条 `plan_only`）、Evidence Requirement（**复用** `qa_planner._retrieval_strategy` 的 source 口径：official_policy/official_interpretation/professional_commentary/hop_chain/adjudication）、dependency 边（带 `carries` 与数据契约名）；**计划里已有 `decomposition` 就一次 `decompose()` 都不再调**（mock 断言守门，避免图与运行期 hops 分叉），没有才现算并写明来源；DAG 有效性复用 `validate_dag`（环/自环/指向更晚的跳都拦）。tests/test_qa_phase05_plan.py **15 例** | 2026-10-10 |
|P05-03|dependency/parallel groups| PASS | 依赖层级 → `parallel_groups`（同层=互不依赖才并行，§2.1/§2.4；入度 ≥2 标 `barrier`，§2.5）；"计划→检索"是真实依赖（有用例钉着：`retrieve.depends_on == [interpret]`）；无依赖就**不产生边**（§2.1 禁止无意义串行）；舰队打开时首跳 5 个 Hunter 同组并行 + `merge` 汇合（barrier=True）。用例逐组断言"组内任意两节点之间没有依赖" | 2026-10-10 |
|P05-04|Fast/Standard/Deep| PASS | §18 三条链落成节点编排，`stage_chain` **直接取** `QaOrchestrator.stage_plan(mode, level2_enabled=...)`：图上"会跑"的节点其阶段必须在链上（有守门用例）；fast 无 level2/冲突节点、level2 关闭时那三段标 `skipped`；路径 = `mode`（§6 的 simple→Fast 落地为 `suggested_path` 建议，**不隐式改档**，见 D-019）；每节点带 §2.2 契约全字段（purpose/input_schema/output_schema/timeout/retry/model_tier/allowed_tools/validation/failure_policy）且过 `validate("execution_node")`，失败策略只用既有五值并与 `DEGRADABLE_STAGES` 对齐；deep 的 P06/P07/P13 三段是 `deferred` 占位（`implemented=False`、timeout=0、不占预算）。真跑（A 机 13 条真实问题）节点均值 fast **8.31** / standard **11.31** / deep **14.31**（含 3 个占位）| 2026-10-10 |
|P05-05|budget/Execution Graph| PASS | `path_budget`（= 该路径阶段预算之和，level2 深研那一段优先用 `qa_policy.research_timeout_seconds`；`QA_GRAPH_BUDGET_<PATH>_SECONDS` 可覆盖）+ `apply_budget`（只裁 `optional` 节点、从链尾往前、裁完仍装不下就标 `infeasible=True`）+ `ExecutionLedger`（可注入时钟，假时钟复算：超预算 `should_run()`→False 即"真的停"、停止原因 `BUDGET_EXHAUSTED`）+ `record_node_runs`（落 Phase 01 的 `node_id/node_kind/parent_node_id/round_index`，`stage="node:<node_id>"`，开关 `QA_EXECUTION_GRAPH_NODE_RUNS` 默认关）+ 管线回执走**兄弟键** `stats["execution_graph"]`（Phase 02 六键/Phase 03 回执不动）；本阶段只可能产出 {ANSWERABLE, BUDGET_EXHAUSTED, MAX_DEPTH}（守门用例断言 NO_GAIN/UNRESOLVABLE_CONTRADICTION 不出现），且 MAX_DEPTH 需"放宽跳数上限会长出更多跳"的真截断证明。真跑（13 条真实问题）：预算均值 fast 148s / standard=deep 313s，关键路径估算 132s/294s；`QA_GRAPH_BUDGET_{STANDARD,DEEP}_SECONDS=120` 时 13/13 题 `BUDGET_EXHAUSTED`、裁 43 个可选节点。tests/test_qa_phase05_budget.py **23 例** | 2026-10-10 |
|P05-R1|Phase 02/03/04 不回归| PASS | `python -m pytest tests -q -k "phase05"` → **107 passed, 1985 deselected**；`tests/test_qa_phase04_pipeline.py tests/test_qa_phase02_pipeline.py tests/test_qa_stage_contract.py` → 24 passed；`QA_EXECUTION_GRAPH` 不设时 `level1_retrieval` 的阶段返回键集/`stats` 键集与接线前逐字一致（有专门用例），`stats["evidence_layer"]` 六键与 `stats["verification"]` 兄弟键未被触碰；`_run_multi_hop` 新增 `budget_seconds` 缺省 None → 逐字回到 `config.QA_MULTI_HOP_BUDGET_SECONDS`。全量：`python -m pytest tests -q` → **2091 passed, 1 skipped, 0 failed**（Phase 04 基线 1984+107，零回归、零删除/跳过用例）| 2026-10-10 |
|P05-R2|冻结契约指纹复算 + 枚举不动| PASS | tests/test_qa_phase05_contracts.py **17 例**：七指纹与 P00-02 逐字相同（EVIDENCE 370301331c02c738 / CLAIM 06fcdb02441248b2 / CONFLICT aabd3259b07f9a3e / LEVEL1 7e864764429db111 / LEVEL2 a0484f894e7bc9d6 / FINAL_ANSWER 4d1efa54ca1cbc1a / QA_EVENT 59358bfa88a6c6af）；`EVIDENCE_SCHEMA.additionalProperties` 仍 False；`QA_RETRIEVAL_ROUTES` 仍 7 值、失败策略/停止原因仍 5 值；`EXECUTION_NODE_SCHEMA.required` 仍只有 `node_id`；`QA_SCHEMA_VERSION` 仍 v7、无新表/新列；两个新开关默认关；两个新模块零网络库导入、零 http(s) 与模型客户端痕迹 | 2026-10-10 |
|P06-01|Evidence Graph repository/API| PASS | 新模块 qa_evidence_graph.py（save 复用 persist_reasoning_graph、load 用 graph_from_rows 从 payload_json 重建含 Phase 03 pairs、load_persisted 读 qa_stage_runs.details_json 即免费持久化）+ qa_storage.load_reasoning_graph()（4 张既有表、零新表零迁移）+ 只读 HTTP API GET /api/qa/v1/runs/<id>/evidence-graph；19 例 | 2026-10-10 |
|P06-02|SUPPORTS/REFUTES/DEPENDS/CONTRADICTS| PASS | 新增图级五值枚举 EVIDENCE_GRAPH_RELATIONSHIPS（四 Must + MENTIONS，后者给冻结证据契约的 context 与未判定证据落脚，否则只能错记 SUPPORTS 或丢边）+ 端点乘关系矩阵；不动 CLAIM_EVIDENCE_RELATIONSHIPS（它是冻结证据契约的镜像，守门用例断言二者机器对齐）；关系由 Phase 03 verdict 派生（claim pairs 优先于 evidence_layer.verification 优先于 UNVERIFIED），claim_status_from_edges 与 qa_verifier._claim_status 等价、真机 43 claim 复算 0 不一致；31 例 | 2026-10-10 |
|P06-03|claim coverage| PASS | 口径写死 COVERAGE_DEFINITION：主口径为有已核验 SUPPORTS 边的非计划 claim 占比，带权口径为已核验支持强度饱和求和比，另有 claimed 对照口径与 evidence_coverage、refuted_claim_rate、支持数直方图、覆盖分桶、逐 claim 明细；空图比率写 null；真机 13 run 主口径 0.6977、带权 0.1531、证据覆盖 1.0；关掉核验复放则主口径 0.0（没有核验就没有已验证证据）；18 例 | 2026-10-10 |
|P06-04|Contradiction detector/resolver| PASS | 两族检测（同一结论既有已核验支持又有已核验反驳；结论互斥）+ 九理由码规则裁决（范围、时间、权威、证据质量、独立性、关系强度 + 三种保留不确定性），每条记 winner 与 inputs、同输入同输出；零模型，默认规则裁决器加注入点，未注册或抛错或非法载荷或非法理由码四条失败路径全回落并记账；单调性守例（接线前 resolved 接线后仍 resolved）；回写只动冻结 CONFLICT_SCHEMA 的三个字段且 validate_final_answer 端到端通过；27 例 + 管线 9 例；真机 qa_conflicts 全库 0 行，故真机无对象可检、能力证据来自构造用例 | 2026-10-10 |
|P07-01|Gap taxonomy/priority| PASS | 新模块 qa_gap_analyzer.py（纯规则零模型）：§12 十种 Gap 类型逐字入契约（五件套齐全，缺一个就有守例挂）；类型与严重度**由 Phase 03 核验理由码派生**（映射表可追溯 derived_from_reasons），两处口径不可能打架；优先级 = clamp(0.55×严重度 + 0.25×claim 重要性 + 0.20×证据亏空)，三分量与权重全进 priority_factors **可复算**；§21 安全关键缺口走 Priority Override（抬到 ≥0.95）；gap_id 内容寻址、同条件恒定；25 例 | 2026-10-11 |
|P07-02|suggested route/evidence requirement| PASS | 缺口到建议通道只吃 Phase 01 冻结的 7 个通道值（bm25 落 Phase 04 的 Hunter 身份层，不新增通道取值）；通道到 Hunter 复用 QA_HUNTER_IDS；证据要求走既有契约且 satisfied_by 恒空（满足度由下一轮缺口重算证明、不自证）；建议查询是 claim 实词加模板的确定性拼装（LLM 重写只留注入点）；真机 13 run 复算 126 条缺口 / 7 种类型 / 79 高优 / 15 安全关键 | 2026-10-11 |
|P07-03|Next-hop Planner| PASS | 缺口到一跳：route 取建议第一条且 plan_overrides 让 route **真的改变检索计划**（结构化与图通道带实体、语义通道带词表扩展），逐字复用同一条 ArticleRetriever 链路；四条失败路径全有用例（未注册或抛错或非法载荷保守回落并记账，后端合法返回空列表则尊重它并据此走向 NO_GAIN）；管线里补充跳真的发出且留进 SearchTrace；21 例 | 2026-10-11 |
|P07-04|seen dedupe| PASS | 复用 Phase 02 既有 seen 机制：Query Fingerprint = 归一查询 + route + 约束 + 语料版本 + 检索配置；三种去重全留痕（同批重复 / 已搜指纹 / 已见且被拒来源，MASTER_RULES 第 14 条）；dropped_by_basis 把命中已搜查询与同一缺口重复规划分开记账；真机 13 次命中全部是 already_planned（already_searched=0，边界已记） | 2026-10-11 |
|P07-05|no-gain convergence| PASS | §14 逐字：连续 QA_GAP_NO_GAIN_ROUNDS（默认 2）轮 new_verified_claims==0 且 resolved_high_priority_gaps==0 才收敛；第 0 轮是基线不计入连续数；resolved 口径严格（gap_id 内容哈希，上轮有本轮没了才算）；收敛后不再发补充跳；20 例；端到端同证据 retriever 得 NO_GAIN，真机复算 13/13 | 2026-10-11 |
|P07-06|stop reasons| PASS | 五值**真的都能被产出**（Phase 05 留的账本阶段结清）：decide_stop_reason 唯一决策点（UNRESOLVABLE_CONTRADICTION 优先于 ANSWERABLE 优先于 BUDGET_EXHAUSTED 优先于 NO_GAIN 优先于 MAX_DEPTH，每条带 factors）；UNRESOLVABLE_CONTRADICTION 唯一权威出口是证据图缺口复核（引 Phase 06 的 unresolved 与理由码）；真机 0 条矛盾故用真机素材构造用例复现；MAX_DEPTH 只在高优缺口仍在且补充跳上限用完时给；五值齐备有专门用例 | 2026-10-11 |
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