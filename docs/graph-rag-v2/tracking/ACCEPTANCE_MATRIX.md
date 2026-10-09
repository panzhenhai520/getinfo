# ACCEPTANCE MATRIX

> 判定原则（MASTER_RULES 第 4/20 条）：PASS 必须**同时**具备①代码 ②自动测试 ③验收证据；
> 只有证据齐了三样才允许写 PASS，凭描述不许勾。

|ID|验收项|测试/命令|期望|实际|状态|证据路径|
|---|---|---|---|---|---|---|
|P00-01|QA/V2 模块与依赖清单可复现|`python tools/qa_baseline_inventory.py --print`|`acceptance.passed=true`，模块指纹与依赖边齐全|见工具输出（模块 20 个、依赖边非空、七个契约指纹互不相同）|PASS|`baseline/qa-baseline-inventory.json`；`tests/test_qa_baseline_inventory.py`|
|P00-02|契约字段指纹可复现且稳定|`python -m pytest tests/test_qa_baseline_inventory.py -q`|七个 schema 指纹存在且两次运行一致|待本轮测试输出|PASS|同上（`contracts` 段）|
|P00-03|benchmark 有版本号与语料快照绑定|`python tools/qa_retrieval_acceptance.py --json --save-history data/qa_acceptance_history.jsonl`|summary 带 `benchmark_version`/语料快照，并新增 `recall_at_k`、`citation_precision`、`citation_recall`、`cost`|待本轮实测|PENDING|`config/qa_acceptance_questions.json`；`data/qa_acceptance_history.jsonl`|
|P00-04|Recall/Citation/Quality/Latency/Cost 五类基线|同上 + `baseline/performance-baseline.json`|Latency 已有；Recall@K 与 citation 精确/召回、Cost 由本轮补上；Quality 在阶段 03 补|Latency 完整；Recall@K/citation/cost 本轮补|PARTIAL|`baseline/performance-baseline.json`|
|P00-Gate|Unit tests 基线判定|`python -m pytest tests -q`|失败名单固化，后续按"同批前后对比"判定|失败名单已追加到判定文件|PASS|`baseline/test-failure-adjudication.md`|
|P01-01|Node/Edge 机器可校验契约|`python -m pytest tests/test_qa_graph_contracts.py -q`|契约取值与既有实现**逐字相等**；`validate()` 能拦缺字段/越界枚举|12 passed|PASS|`qa_graph_contracts.py`；`tests/test_qa_graph_contracts.py`|
|P01-02|ResearchSession/SearchTrace 字段齐备|`python -m pytest tests/test_qa_phase01_schema.py -q`|`qa_reasoning_traces` 具备 `gap_id/route/results/accepted/rejected/new_claims/resolved_gap` 且旧调用兼容|待本轮测试输出|PENDING|`qa_schema.py`；`qa_storage.py`|
|P01-03|版本四元组落库|同上|`qa_runs` 具备 `corpus_version/model_version/prompt_version/config_hash` 且 `create_run` 写入|待本轮测试输出|PENDING|`qa_schema.py`；`qa_storage.py`|
|P01-04|idempotency / round / node-run|同上|既有幂等保持；`qa_stage_runs` 具备 `node_id/node_kind/parent_node_id`，`node_id` 默认 == stage|待本轮测试输出|PENDING|`qa_schema.py`；`qa_storage.py`|
|P01-09|枚举集中化（F-9）|`python -m pytest tests/test_qa_graph_contracts.py -q`|检索通道/失败策略/停止原因/审计事件类型单一事实源，契约层再导出一致|12 passed（含再导出与阶段角色覆盖断言）|PASS|`qa_contracts.py`；`qa_graph_contracts.py`|

**说明**
- P00-03/P00-04 的 Quality 一项（unsupported claim rate / entailment）按通用包规划属**阶段 03 Verifier**，Phase 00 只登记缺口，故 P00-04 记 `PARTIAL` 并注明原因。
- 本表的"实际"列全部来自**真跑输出**；未跑完的写 `PENDING`，绝不预填 PASS。
