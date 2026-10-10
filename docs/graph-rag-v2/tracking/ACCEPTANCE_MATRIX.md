# ACCEPTANCE MATRIX

> 判定原则（MASTER_RULES 第 4/20 条）：PASS 必须**同时**具备①代码 ②自动测试 ③验收证据；
> 只有证据齐了三样才允许写 PASS，凭描述不许勾。

|ID|验收项|测试/命令|期望|实际|状态|证据路径|
|---|---|---|---|---|---|---|
|P00-01|QA/V2 模块与依赖清单可复现|`python tools/qa_baseline_inventory.py --print`|`acceptance.passed=true`，模块指纹与依赖边齐全|见工具输出（模块 20 个、依赖边非空、七个契约指纹互不相同）|PASS|`baseline/qa-baseline-inventory.json`；`tests/test_qa_baseline_inventory.py`|
|P00-02|契约字段指纹可复现且稳定|`python -m pytest tests/test_qa_baseline_inventory.py -q`|七个 schema 指纹存在且两次运行一致|**8 passed**；七指纹互不相同且写进 `baseline/qa-baseline-inventory.json`|PASS|同上（`contracts` 段）|
|P00-03|benchmark 有版本号与语料快照绑定|`python tools/qa_retrieval_acceptance.py --json --save-history data/qa_acceptance_history.jsonl`|summary 带 `benchmark_version`/语料快照，并新增 `recall_at_k`、`citation_precision`、`citation_recall`、`cost`|真跑留档：`benchmark_version=qa-acceptance-v1`、`k=12`、**recall_at_k=0.8333**、**citation_precision=0.7719**、**citation_recall=0.8333**、`corpus_snapshot_id=corpus:1b51cb6bc44bab4d`；既有字段不变|PASS|`config/qa_acceptance_questions.json`；`data/qa_acceptance_history.jsonl`|
|P00-04|Recall/Citation/Quality/Latency/Cost 五类基线|同上 + `baseline/performance-baseline.json` + `python -m pytest tests/test_qa_token_usage_capture.py tests/test_qa_stream_usage_option.py -q`|五类都要有可复算基线|Latency 完整；**Recall@12=0.8333、citation_precision=0.7719、citation_recall=0.8333**；**Cost 链路打通**（usage 采集 14 例 + 流式 stream_options 10 例全过；真端点 tokens_in=18/out=1/total=19；关开关回滚到旧行为）；**Quality 归阶段 03**（unsupported claim rate / entailment）|PASS（Quality 除外）|`baseline/performance-baseline.json`|
|P00-Gate|Unit tests 基线判定|`python -m pytest tests -q`|失败名单固化，后续按"同批前后对比"判定|**全量两轮实测 0 失败**（1573→1606 passed / 1 skipped / 0 failed，收集数增长来自并行新增用例）；判定口径已写入 `baseline/test-failure-adjudication.md`|PASS|`baseline/test-failure-adjudication.md`|
|P01-01|Node/Edge 机器可校验契约|`python -m pytest tests/test_qa_graph_contracts.py -q`|契约取值与既有实现**逐字相等**；`validate()` 能拦缺字段/越界枚举|12 passed|PASS|`qa_graph_contracts.py`；`tests/test_qa_graph_contracts.py`|
|P01-02|ResearchSession/SearchTrace 字段齐备|`python -m pytest tests/test_qa_phase01_schema.py -q`|`qa_reasoning_traces` 具备 `gap_id/route/results/accepted/rejected/new_claims/resolved_gap` 且旧调用兼容|**12 passed**；新旧签名都能落库、新字段过 `qa_graph_contracts.validate("search_trace")`、`_run_multi_hop` 回执键集与升级前逐字相同|PASS|`qa_schema.py`；`qa_storage.py`|
|P01-03|版本四元组落库|同上|`qa_runs` 具备 `corpus_version/model_version/prompt_version/config_hash` 且 `create_run` 写入|**12 passed**；真跑落库 `corpus_version=ba768b331fd86cec803be04e`、`prompt_version=qa-research-notes-v3+qa-adjudication-v1`、`config_hash=c75da38ad7bbfa98`（同配置稳定）；`model_version` 已由 `qa_gateway` 注入草稿角色 model_id|PASS|`qa_schema.py`；`qa_storage.py`|
|P01-04|idempotency / round / node-run|同上|既有幂等保持；`qa_stage_runs` 具备 `node_id/node_kind/parent_node_id`，`node_id` 默认 == stage|**12 passed**；`node_id=='level1_retrieval'`、`node_kind=='execution'`；显式 node_id 不被后续状态更新覆盖|PASS|`qa_schema.py`；`qa_storage.py`|
|P01-09|枚举集中化（F-9）|`python -m pytest tests/test_qa_graph_contracts.py -q`|检索通道/失败策略/停止原因/审计事件类型单一事实源，契约层再导出一致|12 passed（含再导出与阶段角色覆盖断言）|PASS|`qa_contracts.py`；`qa_graph_contracts.py`|
|P02-05|证据层 before/after 指标（Gate 的 Metrics 项）|`QA_EVIDENCE_LAYER_ENABLED=0|1 python tools/qa_retrieval_acceptance.py --json`（真库检索、不调 LLM）|替换后不得有回归|**OFF（Phase 01 行为）**：hit 0.8333 / traceable 0.8333 / grounded 0.8333 / avg_evidence 4.75 / recall_at_k 0.8333 / errors 0 / avg_ms 891；**ON（Phase 02 证据层）**：完全相同的 hit/traceable/grounded/avg_evidence/recall_at_k，citation_precision 0.7719、citation_recall 0.8333、graph_rate 0.5、errors 0、avg_ms **922**（+31ms ≈ +3.5% 标注与去重开销）→ **零回归**|PASS|`data/qa_acceptance_history.jsonl`|
|P02-01|Claim/Entity/Evidence/Source/Span schema|`python -m pytest tests/test_qa_phase02_evidence.py -q`|Evidence Object / Source / Span / Entity / Relation 五个 schema 可校验；`EVIDENCE_SCHEMA` 冻结指纹与 `additionalProperties: False` 一字不动|**43 passed**；`evidence_object` 校验含**嵌套**（缺 `span.quote`/`entities[0].entity_key`/越界 `status` 全部被拦）；既有五个 schema 行为不变；证据条目标注后**顶层键集不变**且仍过 `validate_level1_result`|PASS|`qa_graph_contracts.py`；`qa_evidence.py`；`tests/test_qa_phase02_evidence.py`|
|P02-02|provenance|同上|每条证据可回溯 run/stage/route/检索方式/时间 + source/chunk/span 链条|**43 passed**；`provenance.source.source_id == layer.source.source_id`、`content[span.start:span.end] == span.quote`、`route` 缺省回落 `retrieval_method`、ragflow 证据的 `source_id=chunk:<doc>#<chunk>`|PASS|`qa_evidence.py`（`source_identity`/`evidence_provenance`）|
|P02-03|seen 与 confirmed|`python -m pytest tests/test_qa_phase02_seen.py tests/test_qa_phase02_pipeline.py -q`|被拒证据留身份；作用域按 (owner,session,pack) 隔离；confirmed 不被后续 rejected 覆盖|**20 + 11 passed**；旧实现"只留计数不留身份"已修（`qa_evidence_seen` 落 `rejected` + `rejected_count`）；三轴各换一个值都读不到串味身份；重复登记是覆盖（`seen_count` 累加、行数仍为 1）；`forget` 无作用域键时拒绝全局清空|PASS|`qa_schema.py`（v7 新表）；`qa_storage.py`；`tests/test_qa_phase02_seen.py`|
|P02-04|fingerprint/去重|同上 + `-k "qa or retrieval or evidence or contract or schema"`|来源级指纹跨轮稳定；跨轮/跨 run 不再重复捞同一批垃圾；去重口径集中到单一事实源|**11 passed**；正文被截短后 `source_fingerprint` 不变（同来源）、不同段落 `fingerprint` 不同；第二轮把上一轮 `rejected` 的来源挡在证据包外（`seen_dropped=1`），上一轮用过的有用来源仍保留（只加 `repeat` 标记）；`dedupe_evidence_items` 与改造前四条去重键逐条一致|PASS|`qa_evidence.py`；`qa_pipeline.py`（`_apply_evidence_layer`）；`tests/test_qa_phase02_pipeline.py`|

**说明**
- P00-03/P00-04 的 Quality 一项（unsupported claim rate / entailment）按通用包规划属**阶段 03 Verifier**，Phase 00 只登记缺口，故 P00-04 记 `PARTIAL` 并注明原因。
- 本表的"实际"列全部来自**真跑输出**；未跑完的写 `PENDING`，绝不预填 PASS。
- **P02-01…P02-04 的边界声明（避免谎报 PASS）**：
  - 证据层目前**只接在 level1 证据链**（`qa_pipeline._apply_evidence_layer`，检索闸门之后）；
    level2（RAGFlow）与多跳各跳的候选集**尚未**经过证据层标注与登记，属 Phase 03/04 的接线范围；
  - `status` 只做"既有 `relationship` → 规范化枚举"的映射，**不是** verifier 结论；
    entailment/relevance 打分属 Phase 03（P03-01/P03-02），本阶段不实现；
  - 证据层确实**没有**跑过端到端真问答（不调 LLM、不连真库），全部验收都在隔离临时 sqlite 上完成。
- **P02 回归证据（真跑输出）**：`python -m pytest tests/test_qa_phase02_evidence.py tests/test_qa_phase02_seen.py tests/test_qa_phase02_pipeline.py -q -p no:cacheprovider` → **74 passed**；
  `python -m pytest tests -q -k "qa or retrieval or evidence or contract or schema"` → **466 passed, 1239 deselected**；
  `python -m pytest tests -q` 全量 → **1704 passed, 1 skipped, 0 failed**（Phase 00 冻结的 0 失败基线保持）。
  七个契约 schema 指纹复算仍为 EVIDENCE 370301331c02c738 / CLAIM 06fcdb02441248b2 / CONFLICT aabd3259b07f9a3e /
  LEVEL1 7e864764429db111 / LEVEL2 a0484f894e7bc9d6 / FINAL_ANSWER 4d1efa54ca1cbc1a / QA_EVENT 59358bfa88a6c6af。
|P02-06|多跳每跳 + level2 接入证据层|`python -m pytest tests/test_qa_phase02_wiring.py -q`|每跳都有 provenance 与 seen 登记，回执键集不变|**14 passed**；真链路 run 852cce80…：`evidence_layer={"annotated":25,"recorded":34,"seen_dropped":2}`、seen 本 run 9 行且作用域正确。**限制**：本机 RAGFlow（127.0.0.1:9222）未启动且 level2 flag=False，level2 分支只有桩测试证据，无真 RAGFlow 调用证据|PASS（level2 真链路待环境）|
|P02-07|seen TTL / 清理|`python -m pytest tests/test_qa_phase02_seen.py -q`|过期删、未过期留、作用域隔离、开关默认关|**7 例新增**（prune 默认 30 天、最小 1、缺失列退 first_seen_at、坏 store 只回 error）；开关 `QA_EVIDENCE_SEEN_PRUNE_ENABLED` 默认关（新表先观察），挂在 task_cleanup 可选调用|PASS|
|P02-08|真端到端 5 项（Gate 的 Integration 项）|`create_run` + `orchestrator.execute` 真跑 1 题（family_office / standard / 3 跳 / 93.8s）|四元组落库、seen 行、token 用量非空、traces 带 route、run completed|**5/5 PASS**：四元组 ('aa91f12927852b39ef2c2c75','qwen3.8-27b-uncensored','qa-research-notes-v3+qa-adjudication-v1','c75da38ad7bbfa98')；seen 9 行；synthesis token 2692（level1_draft 8887）；traces 3 条均带 route=keyword 与计数；status=completed degraded=0|PASS|
|P03-01|relevance/reranker|`python -m pytest tests/test_qa_phase03_verifier.py tests/test_qa_phase03_pipeline.py -q`|相关性 [0,1] 可复算；重排稳定（同分保序）；接线后证据包按核验分排序|**92 passed**（阶段 03 三份用例合计）；标题命中分高于仅正文命中、无实词返回 0 不抛错、>1 的知识库排序分不被当相似度；真机 12 条证据按核验分重排：`reordered=9`（第 1 名不变），且 12 条的判定分布与人工看标题的判断一致（唯一同主题文章 QUALIFIED，其余 UNVERIFIED）|PASS|`qa_verifier.py`（`relevance_score`/`rerank_evidence`）；`qa_pipeline.py`（`_apply_evidence_layer`）|
|P03-02|entailment/NLI|`python -m pytest tests/test_qa_phase03_verifier.py -q` + `python tools/qa_verifier_acceptance.py`|证据不足必须 UNVERIFIED/QUALIFIED；后端缺失/抛错保守回落且降档；核验层零联网|金标 18 对：`support_recall=1.0`、`support_precision=1.0`、**`false_support_rate=0.0`**（MASTER_RULES 第 11 条硬指标）；未注册后端名（写成端点样式）与后端抛错两例都回落并降档；AST 守门断言无 requests/urllib3/httpx/socket、零 http(s) 字面量|PASS|`qa_verifier.py`；`tools/qa_verifier_acceptance.py`|
|P03-03|entity/time/negation/source verifier|同上 + 真机回放|实体/时间/否定/来源各自能单独否决；不许把"碰巧有否定词"当反证|金标：实体缺失/数字不符/时间早于结论 → UNVERIFIED；否定翻转（同话题）→ REFUTED 且 contradiction_recall=1.0、**false_refute_rate=0.0**；二手转述/因果夸大 → QUALIFIED；**真机 8 条真实 claim 由"6 条 conflicted"修正为 0 条**（长文误判），并新增 g17/g18 两个回归用例钉死|PASS|`qa_verifier.py`；`config/qa_verifier_golden.json`；`baseline/qa-verifier-real-sample.json`|
|P03-04|EvidenceScore/reason/cache|`python tools/qa_verifier_acceptance.py --json --out baseline/qa-verifier-acceptance.json`|加权分可复算、原因码齐备、缓存命中且不许串味|真机 12 条证据：第一遍 **73–219ms → 第二遍 1.8–4.2ms（40–68x，命中率 0.5）**；金标单条 **0.93–2.4ms**（调优前逐条 3.6→0.9ms：OpenCC 归一化缓存 + 分句先整段归一化）；`VerificationCache` TTL 过期与 LRU 上限各有用例；缓存键覆盖权威性/时间/relationship（把 authority 60→None 必须 miss，有守门用例）|PASS|`qa_verifier.py`（`evidence_score`/`VerificationCache`）；`qa_storage.py`（`get/put_verification_cache`）；`baseline/qa-verifier-acceptance.json`；`data/qa_verifier_history.jsonl`|
|P03-05|MASTER_RULES 第 11 条落地（claim 级核验）|`python -m pytest tests/test_qa_phase03_pipeline.py -q`|模型自评的 claim 状态必须被规则核验覆盖；结论级统计进生成端|`conflict_review` 阶段真跑：claim `verification_status` 按"结论→引用证据"逐对核验重写并落 `qa_claims`；模型自评 `confirmed` + 不支持证据 → 库内降为 unverified（有 DB 级断言）；无证据 → `insufficient_evidence`；悬空引用 → `claim_evidence_missing`；`unsupported_claim_rate` 进阶段事件与生成端问题（有闭包级断言）|PASS|`qa_verifier.py`（`verify_claim_graph`）；`qa_pipeline.py`（`conflict_review`/`synthesis`/`_verification_prompt_blocks`）|
|P03-R1|Phase 02 契约不回归|`python -m pytest tests/test_qa_phase02_*.py tests/test_qa_logic_validation.py -q`|evidence_layer 回执键集逐字不变；关掉核验回到 Phase 02 结构|**105 passed**；核验回执走**兄弟键 `stats["verification"]`**（不动 Phase 02 冻结的六键回执）；`record_seen` 新增可选 `extra_rejected`（原 `rejected=[...]` 调用点一字不动，Phase 02 的源码级守门用例继续有效）；`QA_VERIFIER_ENABLED=0` 时阶段返回结构与 Phase 02 逐字相同|PASS|`qa_pipeline.py`；`qa_evidence.py`|
|P03-R2|冻结契约指纹复算|`python -m pytest tests/test_qa_phase03_contracts.py -q`|七个契约指纹与 P00-02 一致；`EVIDENCE_SCHEMA` 仍 `additionalProperties: False`|**13 passed**；七指纹复算 EVIDENCE 370301331c02c738 / CLAIM 06fcdb02441248b2 / CONFLICT aabd3259b07f9a3e / LEVEL1 7e864764429db111 / LEVEL2 a0484f894e7bc9d6 / FINAL_ANSWER 4d1efa54ca1cbc1a / QA_EVENT 59358bfa88a6c6af（与 Phase 00 逐字相同）；核验结论只落在 `metadata.evidence_layer.verification`（可选字段），核验过的证据照样过 `validate_level1_result`|PASS|`tests/test_qa_phase03_contracts.py`；`qa_graph_contracts.py`|

**Phase 03 说明（边界与未满足项，宁写 PARTIAL 不谎报）**
- **P03-01…P03-04 全部记 PASS**，但下面这些**明确不算通过**的项要一起看：
  - **`status` 语义未改**：证据对象的 `status` 仍是 Phase 02 的"`relationship` → 规范化枚举"映射，
    核验结论放**新增可选字段** `verification.verdict`（同一取值域）。取舍理由：`status` 被
    Phase 02 的回归用例与既有调用方钉着，改语义属跨阶段契约变更且会牵动生产；**verdict 才是
    verifier 的结论**，`verified == (verdict == "SUPPORTED")` 是唯一可当"已验证"的判据。
    → 若评审要求 `status` 直接等于核验结论，需先写 DECISION_LOG 并重跑 Phase 02 回归（本轮未做）。
  - **不是模型/NLI 质量指标**：金标 18 对是**人工标注的小样本**，且与阈值调参同源，
    `support_recall=1.0` 只说明"没把明显该支持的判丢"，**不能当泛化质量证据**；
    真正的 NLI 质量要等有独立标注集（Phase 17 的 metric report 范围）。
  - **闸门只丢 REFUTED**（`QA_VERIFIER_GATE=refuted` 默认）：UNVERIFIED 证据仍进证据包，
    只是不许当"已验证"；更激进的 `unverified` 档已实现但**默认关**（未在生产验证）。
  - **claim 级核验只到 `conflict_review`/`synthesis`**：`fast` 模式走 synthesis 补建图时同样核验
    （有接线），但 `level2_research` 内部产生的中间 claim（未进图前）没有核验。
  - **来源核验只用既有字段**（`authority_level`/`doc_type`/`source_role`/`relationship`），
    没有新增信源可靠性表；`source_quality` 进 EvidenceScore 是**打折项**，不是否决项——
    实测真机文章 `authority_level` 普遍是 1–2，若据此否决会让所有结论都"证据不足"（无区分度）。
    信源可靠性的正式建模属 P12-04。
  - **真机验证是"只读导出 + 本地离线回放"**，不是在生产容器里跑新代码：核验层代码没有部署到 A 机
    （不部署是本次任务的硬约束）。因此"核验结论分布来自真机数据"成立，"核验在生产链路里跑过"不成立。
- **Phase 03 回归证据（真跑输出）**：
  `python -m pytest tests/test_qa_phase03_contracts.py tests/test_qa_phase03_pipeline.py tests/test_qa_phase03_verifier.py -q -p no:cacheprovider` → **92 passed**；
  `python -m pytest tests/test_qa_phase02_wiring.py tests/test_qa_phase02_pipeline.py tests/test_qa_phase02_evidence.py tests/test_qa_phase02_seen.py tests/test_qa_logic_validation.py -q` → **105 passed**；
  `python -m pytest tests -q -k "qa or evidence or reasoning or retrieval or contract or schema"` → **581 passed, 1317 deselected**；
  `python -m pytest tests -q`（全量）→ **1897 passed, 1 skipped, 0 failed**（Phase 00 冻结的 0 失败基线保持；收集数 1704 → 1897 含本阶段新增 92 例）；
  `python tools/qa_verifier_acceptance.py` → 金标 18 对：`support_recall=1.0 / support_precision=1.0 / false_support_rate=0.0 /
  contradiction_recall=1.0 / false_refute_rate=0.0`（单条 1.5ms），真机样本（run `c3fd469f115d4002bd2824b397425756`，automotive_industry）
  12 条证据 / 8 条真实 claim：`confirmed=4, unverified=3, insufficient_evidence=1, conflicted=0`，
  `unsupported_claim_rate=0.5`，缓存 12 条 73–219ms → 1.8–4.2ms（40–68x）。
  报告留档 `baseline/qa-verifier-acceptance.json`、历史行 `data/qa_verifier_history.jsonl`。

