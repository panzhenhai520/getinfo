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
|P04-01|BM25 Hunter 可单跑/可降级/可复算|`python -m pytest tests/test_qa_phase04_hunters.py -q`|排序可手算复算；时间闸门生效；空查询与异常都有干净失败路径|**30 passed**；手算对照 `idf=ln(1+(N-df+0.5)/(df+0.5))` 逐项相等；不含查询词的文档被等价剪枝；`2026年1月` 问题把 10 月文章挡在窗外；空查询 → `empty/no_query_terms`；**泛词回归**：只含「限时权益/最近」的促销文不得盖过含「蔚来」的文章（idf 必须用全池 df，实测这是踩过的坑）|PASS|`qa_hunters.py`（`BM25Hunter`/`bm25_scores`/`bm25_query_tokens`）；`tests/test_qa_phase04_hunters.py`|
|P04-02|Semantic Hunter 只吃库内向量、零端点|`python -m pytest tests/test_qa_phase04_hunters.py -q` + 快照真跑|无向量时降级；语义近似（词面不命中）文章能被召回并计数；源码零端点调用|**30 passed**（含 5 例语义专测）；快照 240 条 1024 维向量 → 语义通道 **7/7 取到证据**，`semantic_only` 在题内最高 5 条；`no_vectors`/`no_lexical_seed`/loader 抛错三条降级路径各有用例；AST 断言不 import 网络库、源码零 `http(s)://` 与 `embedding_client`/`_embed_question` 痕迹|PASS|`qa_hunters.py`（`SemanticHunter`/`load_article_vectors`）；`tests/test_qa_phase04_hunters.py`；DECISION_LOG D-018|
|P04-03|Graph Hunter adapter 复用既有图通道|同上（真链路用例）|适配器只转发不重写；图库不可用 → DEGRADE|**30 passed**；注入 `graph_runner` 断言 `plan/pack/limit=min(4,limit//3)/builder` 原样转发；真链路（事件/属性 → `KnowledgeGraphBuilder` 建图 → 取回图证据）1 例；快照回放里比亚迪/蔚来两题取到 **4 / 3 条**图证据、可回溯 100%|PASS|`qa_hunters.py`（`GraphHunter`）；`tests/test_qa_phase04_hunters.py`|
|P04-04|Structured/DB adapter（政策表 + 元数据闸门 + 外部源注入点）|同上|政策登记表命中走既有通道；没给过滤条件不产证据；单源失败不影响其它源|**30 passed**；政策行真数据用例命中 `article:<id>`；`min_authority=60` 时闸门挡住全部行（有反例）；注入一个抛错的 provider + 一个正常 provider → 正常源照常出货、失败只记 `stats.providers[].error`；快照里 0 条政策登记行 → 7/7 `empty`（如实无命中）|PASS|`qa_hunters.py`（`StructuredHunter`/`normalize_structured_filters`）；`tests/test_qa_phase04_hunters.py`|
|P04-05|Query Expansion 只产词不产证据|同上|`evidence` 恒为空；词源可追溯；别名/繁体/图谱邻居都要用上|**30 passed**；`evidence == []` 有专门断言；`MIIT`/`人形機器人` 出现在词表（既有词表复用）；图谱邻居标 `source=graph_neighbor`；图查询抛错仍能出词（只少一个来源）|PASS|`qa_hunters.py`（`QueryExpansionHunter`）；`tests/test_qa_phase04_hunters.py`|
|P04-06|fan-out/timeout/retry/fallback/预算/部分结果|`python -m pytest tests/test_qa_phase04_fleet.py -q`|并发有界；超时先重试再降级；兜底标记；预算耗尽返回部分结果；失败隔离|**22 passed**；3×0.25s 并发墙钟 < 串行和（`parallelism_gain_ms` 可测）；`max_workers=1` 时确实串行；超时 `attempts=2` 且真的重跑（`calls==2`）；首次失败第二次成功；预算 0.25s → `partial=true`/`BUDGET_EXHAUSTED`/快通道证据保留、慢通道记 `budget_exhausted`；一个通道抛错不影响其它通道|PASS|`qa_hunter_fleet.py`；`tests/test_qa_phase04_fleet.py`|
|P04-07|管线接线（默认关，打开即走舰队）|`python -m pytest tests/test_qa_phase04_pipeline.py -q`|默认关时阶段返回键集/stats 键集逐字不变；打开时走舰队且回执走兄弟键；舰队抛错回落既有检索|**8 passed**；默认关：阶段 10 个返回键与 Phase 02 口径逐字一致、`stats` 无 `hunter_fleet`；`QA_HUNTER_FLEET=1`：5 个 Hunter 全部出现在回执里、`stats["evidence_layer"]` 六键不变；注入抛错舰队 → `stats["hunter_fleet"]["fallback"]=="ArticleRetriever.retrieve"` 且证据包照常；多跳后续跳仍由既有 retriever 执行（有断言）|PASS|`qa_pipeline.py`（`_local_retrieval`/`_build_hunter_fleet`/`_hunter_fleet_enabled`、`level1_retrieval`）；`tests/test_qa_phase04_pipeline.py`|
|P04-08|单通道 vs 舰队 真跑对比（A 机只读快照回放）|`python tools/qa_phase04_corpus_export.py ... --out baseline/qa-hunter-corpus-snapshot.json` → `python tools/qa_hunter_fleet_acceptance.py --snapshot ... --out baseline/qa-hunter-fleet-acceptance.json --history data/qa_hunter_history.jsonl`|命中/可回溯/接地/Recall@K/引用 P·R/耗时/降级次数两侧可比|真跑（A 机只读快照：2 个行业包 239 篇 / 262 条分类 / 238 事件 / 240 属性 / **240 条 1024 维真向量**；冻结 benchmark 中命中该语料的 7 题，limit=12）：hit/traceable/grounded/recall@12/citation_recall **两侧完全相同**（100%/100%/57.1%/57.1%/57.1%）；**avg_ms 548 → 303（−245ms）**；证据合计 48 → 52；舰队 0 降级、0 部分结果、并行增益 1928ms、候选池 7 次加载；**citation_precision 75.0% → 67.3%（−7.7pp，见下方"已知取舍"第 1 条）**|PASS（引用精确率差异已解释）|`baseline/qa-hunter-corpus-snapshot.json`；`baseline/qa-hunter-fleet-acceptance.json`；`data/qa_hunter_history.jsonl`；`tools/qa_hunter_fleet_acceptance.py`；`tools/qa_phase04_corpus_export.py`|
|P04-09|验收工具自测（快照回放可复算）|`python -m pytest tests/test_qa_phase04_acceptance_tool.py -q`|快照→临时 sqlite 往返、逐题明细键集与既有口径一致、compare 出两侧与 Δ|**5 passed**；向量 base64 解码后原样入库（2 条）；逐题明细含 `qa_retrieval_acceptance.evaluate` 的全部键（复用同一 `summarize` 口径）；`compare` 报出 baseline/fleet/Δ/fleet_metrics；`build_db_from_snapshot` 断言 `backend=='sqlite'` 且库路径=临时文件（拒绝写主库）|PASS|`tools/qa_hunter_fleet_acceptance.py`；`tests/test_qa_phase04_acceptance_tool.py`|
|P04-R1|Phase 02/03 契约与行为不回归|`python -m pytest tests/test_qa_phase02_*.py tests/test_qa_phase03_*.py tests/test_qa_logic_validation.py -q`|冻结回执键集不变；关掉舰队回到旧路径|**全部通过**（随全量 1984 passed）；`stats["evidence_layer"]` 六键与 `stats["verification"]` 兄弟键均未被舰队触碰（有用例）；`QA_HUNTER_FLEET` 不设时 `level1_retrieval` 与接线前逐字相同|PASS|`tests/test_qa_phase04_pipeline.py`；`tests/test_qa_phase02_*.py`|
|P04-R2|冻结契约指纹复算 + 通道枚举不动|`python -m pytest tests/test_qa_phase04_contracts.py -q`|七指纹与 P00-02 一致；`QA_RETRIEVAL_ROUTES` 仍 7 个取值；库表零迁移|**18 passed**；七指纹复算与 Phase 00 逐字相同、`EVIDENCE_SCHEMA.additionalProperties` 仍 False；`QA_RETRIEVAL_ROUTES` 与 `SEARCH_TRACE_SCHEMA.route` 取值域逐字未变（Hunter 身份走新命名空间 `QA_HUNTER_IDS`）；`QA_SCHEMA_VERSION` 仍 v7、无新表/新列；`QA_HUNTER_FLEET` 默认 false|PASS|`tests/test_qa_phase04_contracts.py`；`qa_graph_contracts.py`；DECISION_LOG D-017|
|P05-01|Query Interpreter（规则实现 + 可插拔）|`python -m pytest tests/test_qa_phase05_interpreter.py -q`|§6 的 9 值意图全部可命中；复杂度依据可复算；未注册/抛错/非法载荷三条失败路径都回落 rules|**23 passed**；9 个意图逐条有真实命中样例（`MULTI_ENTITY`/`DIAGNOSTIC` 用显式入参钉口径）；`complexity_reasons` 非空且对得上（多跳类别/比较冲突/≥3 实体问句→deep；单问句+简单类别+≤60 字→simple；超 60 字降回 standard 有反例）；反证 claim 恒 `plan_only=True`；三条失败路径都得到 `backend_source=rules` + `fallback.used=True` + 中文原因；AST 断言不 import 网络库、源码零 `http(s)`/模型客户端痕迹；内置后端只有 rules（LLM 版本只留注入点）|PASS|`qa_query_interpreter.py`；`tests/test_qa_phase05_interpreter.py`；DECISION_LOG D-019|
|P05-02|Subquestion/Claim decomposition|`python -m pytest tests/test_qa_phase05_plan.py -q`|复用既有分解器；四件产物齐全；有计划里的 decomposition 不许重新分解|**15 passed**；`mock.patch(eg.decompose, side_effect=AssertionError)` 证明"有计划 hops 时一次都不再调 decompose"；无 decomposition 时才现算并写 `qa_query_decompose.decompose`；`sub_question`/`plan_claim`/`evidence_requirement` 三个 schema 逐个校验通过；证据要求与 `qa_planner._retrieval_strategy` 的 source 逐项相等；依赖边带 `carries`+`schema`；环/自环/指向更晚的跳由 `validate_dag` 拦（有用例）|PASS|`qa_execution_graph.py`（`build_research_plan`）；`tests/test_qa_phase05_plan.py`|
|P05-03|dependency/parallel groups|`python -m pytest tests/test_qa_phase05_plan.py tests/test_qa_phase05_paths.py -q`|组内两两无依赖；无依赖不产生边；只有真正汇合才是 barrier|**15 + 19 passed**；`fan` 用例：3 个独立子问题同组且 `parallel=True`、第 4 个汇合点 `barrier=True`/`size=1`；`dependencies == []`（无依赖就不写边，§2.1）；"计划→检索"是真实依赖（`retrieve.depends_on == ["…interpret"]`）；舰队场景 5 个 Hunter 同组并行、`merge` 为 barrier 且依赖全部 Hunter|PASS|`qa_execution_graph.py`（`parallel_groups`/`_levels`/`_mark_barriers`）；`tests/test_qa_phase05_paths.py`|
|P05-04|Fast/Standard/Deep 三路径|`python -m pytest tests/test_qa_phase05_paths.py -q`|§18 三条链；图上有用节点必须在 `stage_plan` 链上；每节点带契约全字段与失败策略五值|**19 passed**；`spec_chain` 与 §18 逐字相同；`stage_chain` 与 `QaOrchestrator.stage_plan` 相等（fast=FAST_STAGES/standard=FULL_STAGES）；level2 关闭时三段 `skipped`；deep 的 3 个占位节点 `implemented=False`+`deferred_to=P06/P07/P13`+`timeout=0`；五路径失败策略取值 {FAIL_FAST,RETRY,SKIP,FALLBACK,DEGRADE} 有真实落点且 `DEGRADABLE_STAGES`→DEGRADE；Hunter 节点的超时/重试取自舰队既有旋钮（改成 2.5s/3 次会跟着变）|PASS|`qa_execution_graph.py`；`tests/test_qa_phase05_paths.py`；DECISION_LOG D-019|
|P05-05|budget / Execution Graph|`python -m pytest tests/test_qa_phase05_budget.py -q`|三档总预算 + 每节点预算可复算；超预算可观测地停并写既有五值停止原因；节点行落 Phase 01 的列|**23 passed**；`path_budget` 与阶段预算之和相等（deep 的深研段用 `qa_policy.research_timeout_seconds=90`）；`QA_GRAPH_BUDGET_FAST_SECONDS=12` 覆盖生效；紧预算（deep 240s / 100s）分别得到"裁可选节点"与"`infeasible=True`"两条真实路径，停止原因全部落在既有五值内且**永不产出 NO_GAIN/UNRESOLVABLE_CONTRADICTION**；假时钟账本：超预算后 `should_run()`→False、`over_budget=True`、`stop_reason=BUDGET_EXHAUSTED`；`record_node_runs` 在隔离临时 sqlite（断言 `backend=='sqlite'`）写出 `node:<id>` 行、`node_kind` 分别 plan/retrieve/merge、`round_index` 落库、坏 store 只记 failed 不抛|PASS|`qa_execution_graph.py`（`path_budget`/`apply_budget`/`ExecutionLedger`/`record_node_runs`）；`qa_pipeline.py`；`tests/test_qa_phase05_budget.py`；DECISION_LOG D-020/D-021|
|P05-06|管线接线（默认关，打开即出图 + 节点落库）|`python -m pytest tests/test_qa_phase05_pipeline.py -q`|默认关时阶段返回键集/stats 键集逐字不变；打开时回执走兄弟键；建图失败不影响证据|**10 passed**；默认关：10 个返回键与 Phase 02 口径逐字一致、`stats` 无 `execution_graph`；打开：`stats["execution_graph"]` 带 path/node_counts/budget/stop_reason/runtime（检索/核验/重排/每跳都记上），`stats["evidence_layer"]` 六键不变；`QA_EXECUTION_GRAPH_NODE_RUNS=1` 时 `qa_stage_runs` 出现 `node:standard.retrieve`（`node_kind=retrieve`）等行；图的 `hop_budget_seconds` 真的传进 `_run_multi_hop`（mock 断言）；建图抛错 → 回执带 error+fallback 且证据包照常；policy 读不到也照样出图|PASS|`qa_pipeline.py`（`_build_run_graph`/`_ledger_*`/`level1_retrieval`/`_run_multi_hop(budget_seconds=)`）；`tests/test_qa_phase05_pipeline.py`|
|P05-07|真机只读验收：真实问题三档规划输出|`python _tmp_phase05_real_questions.py --limit 15`（A 机只读）+ `python tools/qa_phase05_planner_acceptance.py --questions _tmp_phase05_real_questions.json --out baseline/qa-planner-acceptance.json --history data/qa_planner_history.jsonl`|给出真实问题在 fast/standard/deep 下的节点数/跳数/预算/停止原因|**13 条真实问题**（去重后；来自 A 机 `qa_runs.question_text`，只读 + LIMIT 15）：节点均值 fast **8.31** / standard **11.31** / deep **14.31**（deep 含 3 个占位）；跳合计 17（"DRG对医院的影响和对患者的影响分别有哪些？"与"DRG对医院行业的影响有哪些？对患者的影响有哪些？"各 3 跳）；Claim 30、依赖边 4、barrier 15；预算均值 fast 148s / standard=deep 313s（关键路径估算 132s / 294s）；停止原因三档全 `ANSWERABLE`；**对照**：舰队打开时（benchmark 12 题）节点均值 fast 13.0 / standard 16.0 / deep 19.0、多节点并行组 12/12/48；**把 `QA_GRAPH_BUDGET_{STANDARD,DEEP}_SECONDS` 压到 120s** → 13/13 题变 `BUDGET_EXHAUSTED`、裁掉 43 个可选节点、估算 130s > 120s 且如实标 `infeasible`。报告留档 `baseline/qa-planner-acceptance.json`（另有 `-benchmark.json` 含舰队对照），历史行 `data/qa_planner_history.jsonl`|PASS|`baseline/qa-planner-acceptance.json`；`baseline/qa-planner-acceptance-benchmark.json`；`data/qa_planner_history.jsonl`；`tools/qa_phase05_planner_acceptance.py`；`_tmp_phase05_real_questions.json`|
|P05-R1|Phase 02/03/04 不回归|`python -m pytest tests -q -k "phase05"` + `python -m pytest tests/test_qa_phase04_pipeline.py tests/test_qa_phase02_pipeline.py tests/test_qa_stage_contract.py -q`|关掉开关回到旧路径；冻结回执键集不变|**107 + 24 passed**；`QA_EXECUTION_GRAPH` 不设时 `level1_retrieval` 返回键集/`stats` 键集与接线前逐字一致（有专门用例）；`stats["evidence_layer"]` 六键与 `stats["verification"]` 未被触碰；`_run_multi_hop` 新增的 `budget_seconds` 缺省 None → 逐字回到 `config.QA_MULTI_HOP_BUDGET_SECONDS`；**全量 2091 passed / 1 skipped / 0 failed**（1984+107，零回归、零删除/跳过用例）|PASS|`tests/test_qa_phase05_pipeline.py`；`tests/test_qa_phase04_pipeline.py`|
|P05-R2|冻结契约指纹复算 + 枚举不动|`python -m pytest tests/test_qa_phase05_contracts.py -q`|七指纹与 P00-02 一致；Phase 01 的三个枚举与 `EXECUTION_NODE_SCHEMA.required` 不动；库表零迁移|**17 passed**；七指纹复算与 Phase 00 逐字相同、`EVIDENCE_SCHEMA.additionalProperties` 仍 False；`QA_RETRIEVAL_ROUTES`（7）/`QA_FAILURE_POLICIES`（5）/`QA_STOP_REASONS`（5）逐字未变；`EXECUTION_NODE_SCHEMA.required == ["node_id"]`（Phase 01 调用方零改动）；`QA_SCHEMA_VERSION` 仍 v7、`QA_ADDED_COLUMNS_V6` 仍 15 条、无新表；两个新开关默认关；新 schema 能拦缺字段/越界枚举（含嵌套 `input_schema.name`）|PASS|`tests/test_qa_phase05_contracts.py`；`qa_graph_contracts.py`；DECISION_LOG D-020/D-021|

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


**Phase 04 说明（边界、取舍与未满足项，宁写 PARTIAL 不谎报）**
- **P04-01…P04-09 记 PASS**，但下面这些**明确不算通过**的项要一起看：
  1. **引用精确率（citation_precision）在真跑对比里下降 7.7pp（75.0% → 67.3%）**，必须一起读：
     · 逐题看，**7 题里 6 题的"含期待词条数"两侧完全相同**（t8 少 1 条）；差额几乎全部来自
       t10/t11/t12 三题——那三题的 `expect_terms` 是「政策/报告/治理」这类**泛词**，
       两侧都**一条都匹配不上**（grounded=False），而舰队在这三题各返 4 条、基线各返 3/3/2 条，
       分母变大 → 精确率被摊薄。**这不是"舰队找错了证据"，而是"舰队在无可匹配标注的题上召回更多"**。
     · 反过来说，这条指标本身是"含期待词的采纳证据 / 采纳证据"，对"召回更多"是惩罚性的；
       要更公平的对比应改用 nDCG/证据召回这类同时看分子分母的指标（Phase 17 的范围）。
     · 报告里两侧的原始分子/分母都在（`rows.baseline` / `rows.fleet` 的 `cited_total`/`cited_matched`），
       可直接复算，不做任何粉饰。
  2. **语义通道不是"真语义"**：本轮不许调 embedding 端点（GPU 推理机与语音机器人共用、已停用），
     查询向量是**离线质心**（词面种子文章的向量加权平均）。它的能力边界是"召回与命中文章语义相近、
     用词不同的文章"，**召回不了与查询毫无词面交集的文章**——早期"只取最新 60 篇"的快照上就出现过
     `semantic:no_lexical_seed`（4/7 题）。真语义要等嵌入服务解禁或换离线编码器（DECISION_LOG D-018）。
  3. **快照是抽样 + 正文截断**：A 机只读导出的快照只含 2 个行业包各 120 篇（共 239 篇）、
     正文截到 1000 字（`--content-chars`），向量是完整 1024 维。因此：
     · 真跑指标只代表"这两个包、这批题、这段语料"，**不能外推成全库水平**；
     · 正文截断会低估"引用精确率"（期待词可能出现在被截掉的后半段），两侧同受影响。
  4. **"舰队在管线里跑了"只到 `level1_retrieval` 首跳**：多跳的后续跳、level2（RAGFlow）仍走既有
     retriever（有用例钉着）。舰队要接管全链路得等 Phase 05 的 Planner/Execution Graph（D-017 的边界）。
  5. **`QA_HUNTER_FLEET` 默认关**：生产默认行为**逐字不变**（零回归有用例），
     舰队的真跑证据来自验收工具与快照回放，**不等于"生产已经在跑舰队"**。
  6. **结构化通道的元数据闸门是"显式条件才生效"**：`plan["structured_filters"]` 没给条件时它不产证据
     （刻意的，避免变成第二遍关键词检索）。本仓库目前没有别处填这个字段，所以它现在的实际贡献主要来自
     "政策登记表精确命中"；外部业务库（EMR/HIS/LIS/PACS 类）只有注入点与失败语义，**没有接任何真实外部库**。
  7. **没有做的事**（越界检查）：不写 SearchTrace 表（Phase 05）、不做 Gap-driven 循环（Phase 07）、
     不做 Context Pack（Phase 08）、不动库表结构、不动任何模型/嵌入端点、不部署。
- **Phase 04 回归证据（真跑输出）**：
  `python -m pytest tests/test_qa_phase04_hunters.py -q -p no:cacheprovider` → **30 passed**；
  `python -m pytest tests/test_qa_phase04_fleet.py -q` → **22 passed**；
  `python -m pytest tests/test_qa_phase04_pipeline.py -q` → **8 passed**；
  `python -m pytest tests/test_qa_phase04_contracts.py -q` → **18 passed**；
  `python -m pytest tests/test_qa_phase04_acceptance_tool.py -q` → **5 passed**（五份合计 **86 passed**，
  且以 `-W error::pytest.PytestUnhandledThreadExceptionWarning` 复跑仍 86 passed → 舰队线程没有留下未处理异常）；
  `python -m pytest tests -q -k "qa or retrieval or evidence or contract or schema"` → **667 passed, 1318 deselected**；
  `python -m pytest tests -q`（全量）→ **1984 passed, 1 skipped, 0 failed**（Phase 00 冻结的 0 失败基线保持；
  收集数 1897 → 1984，含本阶段新增 87 例；另：`-k` 过滤集从 581 → 667）。
- **A 机只读真机验证（本次实际执行的命令与结果）**：
  · 只读探测（`timeout 20 docker exec collectinfo-postgres psql -U postgres -d collectinfo -A -t -c
    "SET statement_timeout='8s'; SET default_transaction_read_only=on; ..."`）：
    `articles=8544 / active=7652 / intel_article_embeddings=10644 / classifications=12803 / kg_edges=1321`；
  · 语料快照（`python tools/qa_phase04_corpus_export.py --pack family_office --pack automotive_industry
    --per-pack 120 --content-chars 1000`，每条远端命令都用 `timeout` 包住、查询都带 LIMIT）：
    family_office 120 篇 + automotive_industry 120 篇 → 239 篇文章 / 262 分类 / 238 事件 / 240 属性 /
    240 条 1024 维真向量，落盘 `baseline/qa-hunter-corpus-snapshot.json`（2.40MB）；
  · 本地离线回放（`python tools/qa_hunter_fleet_acceptance.py --snapshot baseline/qa-hunter-corpus-snapshot.json
    --out baseline/qa-hunter-fleet-acceptance.json --history data/qa_hunter_history.jsonl`）：见 P04-08 那一行。
    **所有远端命令都是短命只读**（无写语句、无部署、无驻留进程）。

**Phase 05 说明（边界、取舍与未满足项，宁写 PARTIAL 不谎报）**
- **P05-01…P05-07 记 PASS**，但下面这些**明确不算通过**的项要一起看：
  1. **Query Interpreter 是规则实现，不是"问题理解"**：没有 LLM 就没有"把自然语言问题改写成规范命题"
     这一步（硬约束禁止调模型/嵌入端点）。因此：
     · `entities` **不做新 NER**，只透传规划器用行业包词表匹配到的实体（没有 `plan` 入参时就是空数组）；
     · `required_claims.statement` 是**待证命题声明**（role/证据要求绑定是真的、可机器消费），
       不是"重写后的命题"；`register_query_interpreter("llm", fn)` 是给后续留的注入点，
       **本轮没有实现、也没有调用**（`registered_interpreters() == []` 有守门用例）。
  2. **§6 的 "complexity=simple → 直接进入 Fast Path" 落地为建议而不是隐式改档**（D-019）：
     `path` 严格等于 `mode`（本仓库实际跑哪条阶段链由 `mode` + level2 开关决定），
     §6 的结论进 `suggested_path`。理由：若 Planner 隐式改档，执行图会与实际阶段链不一致
     （图上写 fast、运行期跑 FULL_STAGES）——**图不许撒谎**。代价：调用方要自己决定是否采纳建议。
  3. **`timeout` 是"上限"，不是"一定会被强杀"**：只有 `budget_enforced=True` 的节点今天有真闸门
     （舰队单 Hunter 超时/重试、多跳墙钟预算、`level1_draft` 首 token 与 synthesis 的阶段预算）；
     其余节点标 `budget_enforced=False` 并写明"仅记账"。**没有**给每个节点装硬性 kill（会改变既有行为）。
  4. **预算裁剪只发生在计划期**：运行期真正的停止仍由既有机制执行（`_run_multi_hop` 超预算写
     `status=skipped_budget`、舰队预算耗尽返回部分结果）。Phase 05 做的是"把这件事提前算出来并写
     `stop_reason`"，它**没有**把 `QA_MULTI_HOP_BUDGET_SECONDS` / 阶段预算换成自己的数
     （缺省 `budget_seconds=None` → 逐字回到既有 config）。
  5. **`NO_GAIN` / `UNRESOLVABLE_CONTRADICTION` 两个停止原因本阶段产不出来**（属 Phase 07 的缺口闭环），
     有守门用例断言三档只会产出 {ANSWERABLE, BUDGET_EXHAUSTED, MAX_DEPTH}；`MAX_DEPTH` 还需
     "放宽跳数上限会长出更多跳"的真截断证明（否则不报），避免把正常多跳误报成截断。
  6. **deep 链里的 P06/P07/P13 三段是占位节点**（`status=deferred`/`implemented=False`/`timeout=0`）：
     它们只让 deep 链与 §18 对得上，**不参与预算、不执行、也不假装跑过**；既有的 `conflict_review`
     是真节点（今天就在跑），P06 会把它升级为 Evidence Graph 级矛盾检测。
  7. **节点行落库是可选副作用**：`QA_EXECUTION_GRAPH_NODE_RUNS` 默认关；打开后节点行以
     `stage="node:<node_id>"` 出现（既有阶段行唯一键是 `(run_id, stage, attempt)`，用真阶段名会让
     多个节点互相覆盖——见 D-021）。这些行会进 `QaMetricsService` 的 stage 分桶统计，属预期副作用。
  8. **真机验证仍是"只读取数 + 本地离线规划"**：Phase 05 代码**没有部署到 A 机**（不部署是硬约束）。
     成立的是"13 条真实生产问题的规划输出由本代码算出"；**不成立**的是"规划器在生产链路里跑过"。
     另外真实问题的三档停止原因默认全是 ANSWERABLE（默认预算装得下），紧预算是**人为压出来**的
     对照实验，不代表生产配置。
- **Phase 05 回归证据（真跑输出）**：
  `python -m pytest tests -q -k "phase05" -p no:cacheprovider` → **107 passed, 1985 deselected**
  （interpreter 23 / plan 15 / paths 19 / budget 23 / pipeline 10 / contracts 17；
  注意 Windows 下 `tests/test_qa_phase05_*.py` 这个通配由 shell 展开，PowerShell 里请用
  `-k "phase05"` 或显式列出六个文件）；
  `python -m pytest tests/test_qa_phase05_pipeline.py tests/test_qa_phase05_budget.py -q -W
  error::pytest.PytestUnhandledThreadExceptionWarning` → **33 passed**（没有留下未处理线程异常）；
  `python -m pytest tests -q -k "qa or plan or retrieval or evidence or contract or schema"` →
  **793 passed, 1299 deselected**（Phase 04 同口径为 667 passed / 1318 deselected）；
  `python -m pytest tests -q`（全量）→ **2091 passed, 1 skipped, 0 failed**（Phase 00 冻结的
  "0 failed" 基线保持；收集数 1984 → 2091，正是本阶段新增的 107 例，**没有改动任何既有用例**）。
- **A 机只读真机验证（本次实际执行的命令与结果）**：
  · 只读探活与计数（`timeout 15 docker ps --format '{{.Names}}'`、`timeout 20 docker exec
    collectinfo-postgres psql -U postgres -d collectinfo -A -t -c "SET statement_timeout='8s';
    SET default_transaction_read_only=on; …"`）：容器 `collectinfo-web/-worker/-qa-worker/-intel-worker/
    -postgres/-redis` 在线；`qa_runs=15 / articles=8544 / active=7652`；
  · 真实问题（`python _tmp_phase05_real_questions.py --limit 15 --out _tmp_phase05_real_questions.json`）：
    取 `qa_runs.question_text`（近 15 条去重后 13 条，`ORDER BY created_at DESC LIMIT 15`）→
    落盘含 `source.access=readonly` 说明；
  · 本地离线规划（`python tools/qa_phase05_planner_acceptance.py --questions
    _tmp_phase05_real_questions.json --out baseline/qa-planner-acceptance.json --history
    data/qa_planner_history.jsonl`）：见 P05-07 那一行；
  · 复核无驻留进程（`timeout 15 docker exec collectinfo-web ps -eo pid,etimes,comm,args`）：
    只有容器自身的 gunicorn（已运行 4692s）与我那一条 `ps`，**无残留**。
    **所有远端命令都是短命只读**（`SET default_transaction_read_only=on` + `timeout` 包住 + 显式 LIMIT；
    无写语句、无部署、无驻留进程）。
|P06-01|证据图仓储与只读 API|`python -m pytest tests/test_qa_phase06_repository.py -q`|读写复用既有存储、零新表零迁移|**19 passed**；真机 13 run 全量重建成功；写库行数与回读 pairs 一致、写读重算关系分布逐字相同|PASS|`qa_evidence_graph.py`；`baseline/qa-evidence-graph-real-sample.json`|
|P06-02|四类关系 + MENTIONS|`python -m pytest tests/test_qa_phase06_relations.py -q`|关系与核验结论一致、边过契约与端点矩阵|**31 passed**；真机关系分布 `{SUPPORTS:30, MENTIONS:16, REFUTES:1}` + DEPENDS 4；43 claim 复算核验状态 **0 不一致**；51 条边 0 违规|PASS|同上|
|P06-03|claim coverage|`python -m pytest tests/test_qa_phase06_relations.py -q`|口径写死且可复算|真机 13 run：主口径 **0.6977**（30/43）、带权 0.1531、evidence_coverage 1.0；关核验复放 → 0.0|PASS|`baseline/qa-evidence-graph-acceptance.json`|
|P06-04|矛盾检测与裁决|`python -m pytest tests/test_qa_phase06_contradiction.py -q`|两族检测 + 九理由码规则裁决、零模型、同输入同输出|**27 passed + 管线 9 例**；单调性守例通过；**真机 qa_conflicts 全库 0 行 ⇒ 真机无可检对象**，能力证据来自构造用例|PASS|同上|
|P06-R1|Phase 05 占位升级|`python -m pytest tests -q -k phase05`|deep 的 evidence_graph 由占位改已实现且不回归|节点 `implemented=True`、stage=conflict_review、model_tier=rule；开关关时标 skipped；Phase 05 六份 **107 passed**；全量 2203 passed / 1 skipped / 0 failed|PASS|`qa_execution_graph.py`|
|P06-R2|冻结契约不动|`python -m pytest tests/test_qa_phase06_contracts.py -q`|七指纹复算不变、枚举不扩|**18 passed**；七指纹与 P00-02 逐字相同；EVIDENCE_SCHEMA 仍 additionalProperties=False；CONFLICT_SCHEMA 字段集与取值域未动；仍 v7、无新表|PASS|同上|
|P07-01|Gap 分类与优先级|`python -m pytest tests/test_qa_phase07_taxonomy.py -q`|十种类型逐字、判定由核验理由码派生、优先级可复算|**25 passed**；五件套齐全；优先级三分量按权重重算逐条相等；gap_id 内容寻址|PASS|`qa_gap_analyzer.py`|
|P07-02|建议通道与证据要求|`python -m pytest tests/test_qa_phase07_taxonomy.py tests/test_qa_phase07_next_hop.py -q`|通道只在冻结枚举内、证据要求可校验不自证|**46 passed**；`satisfied_by` 恒空；真机 13 run 复算 126 条缺口 / 7 类 / 79 高优|PASS|`baseline/qa-gap-acceptance.json`|
|P07-03|下一跳规划与去重|`python -m pytest tests/test_qa_phase07_next_hop.py -q`|缺口到一跳、route 真改计划、失败回落、去重留痕|**21 passed**；四条失败路径；管线里补充跳真的发出并留 SearchTrace|PASS|`qa_gap_analyzer.py`|
|P07-04|无增益收敛与五停止原因|`python -m pytest tests/test_qa_phase07_convergence.py tests/test_qa_phase07_pipeline.py -q`|连续两轮无增益得 NO_GAIN；unresolved 得 UNRESOLVABLE_CONTRADICTION；五值可产出|**20 + 16 passed**；五值齐备用例；真机复算 NO_GAIN 13/13、构造用例复现 UNRESOLVABLE|PASS|`baseline/qa-gap-acceptance.json`|
|P07-R1|执行图占位升级（P07）|`python -m pytest tests -q -k phase05`|gap_loop 由 deferred 改已实现、三路径都有、开关关标 skipped|节点 `implemented=True`、stage=level1_retrieval、model_tier=rule；开关关时 skipped 且不进关键路径估算；Phase 05 用例同步通过|PASS|`qa_execution_graph.py`|
|P07-R2|冻结契约与零迁移|`python -m pytest tests/test_qa_phase07_contracts.py -q`|七指纹不变、枚举不扩、仍 v7、无网络与模型痕迹|**15 passed**；七指纹与 P00-02 逐字相同；QA_STOP_REASONS 仍 5 值（只补产出路径）；无新表新列；AST 守门通过|PASS|同上|
