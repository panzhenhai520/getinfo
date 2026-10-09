# 阶段 1.2：既有测试失败裁决记录

- 裁决时间：2026-08-01（Asia/Hong_Kong）
- 冻结基线：`b17a17cc1459998158714b8c03f585fa2e0b2b8c`
- 原则：不删除测试、不弱化安全转义、调度幂等或包版本校验。

## 四项失败裁决

| 测试 | 分类 | 裁决与修复 | 保留的产品约束 |
| --- | --- | --- | --- |
| `tests.test_intel_stage1.IntelStageOneTests.test_authenticated_api_contract_and_dashboard_home` | 过时断言 | 页面已从直接渲染 `escapeHtml(article.title)` 改为先取得 `rawTitle`、执行 `escapeHtml`，再在已转义文本上做关键词高亮；断言更新为实际安全链路 `highlightKeywords(escapeHtml(rawTitle)`。 | 标题必须先转义，关键词高亮不能重新引入 HTML。 |
| `tests.test_intel_stage4.IntelStageFourTests.test_mapindex_dashboard_cards_and_llm_text_are_escaped` | 过时断言，兼有安全覆盖不足 | 更新标题断言并补齐预览、信源、日期、三类关键词的转义断言；聊天抽屉层级的产品值已由 `9` 调整为 `200`，测试同步当前可用布局。 | 所有外部卡片字段都必须经过转义；聊天抽屉仍须可访问且不被首页卡片遮挡。 |
| `tests.test_intel_stage3.IntelStageThreeTests.test_worker_periodic_scan_and_dispatch_jobs_are_deduped` | 过时调度模型断言 | 原断言固定期待 5 个包级扫描任务；当前实现按“到期信源”创建带日期和 source id 的任务。测试改为验证一个到期信源只生成一个任务，且同一 worker 重复调度和 worker 重启均不重复。 | 只扫描到期信源；同一信源同一交易日/自然日任务幂等；候选分发任务幂等。 |
| `tests.test_intel_stage3.IntelStageThreeTests.test_dispatcher_reuses_existing_article_then_crawls_new_article` | 测试环境隔离缺陷 | 单独运行通过，全套运行时会因其他模块先导入 `config`、并从开发机 `.env` 取得 `INTEL_LLM_ENABLED=true` 而调用生产准入 LLM。`setUp` 现显式关闭该开关，`tearDown` 恢复原值。生产配置和调度代码均未修改。 | 已有文章必须复用且不重抓；新文章必须沿现有正文提取器入库；测试不能访问生产 LLM。 |

## 验证结果

定向命令：

```text
python3 -m unittest -v \
  tests.test_intel_stage1.IntelStageOneTests.test_authenticated_api_contract_and_dashboard_home \
  tests.test_intel_stage3.IntelStageThreeTests.test_dispatcher_reuses_existing_article_then_crawls_new_article \
  tests.test_intel_stage3.IntelStageThreeTests.test_worker_periodic_scan_and_dispatch_jobs_are_deduped \
  tests.test_intel_stage4.IntelStageFourTests.test_mapindex_dashboard_cards_and_llm_text_are_escaped
```

结果：4 项全部通过，0 失败。

全套命令：

```text
python3 -m unittest discover -s tests -v
```

结果：62 项全部通过，0 失败、0 错误、0 跳过；没有保留 `known-failure`。

## 验收结论

- 四项失败均有明确归因；仅一项需要测试隔离修复，未修改生产行为。
- 安全转义与调度幂等断言得到加强，没有改成恒真断言。
- 新增金融 RSS、Dashboard、复扫调度和基线清单测试全部进入全套回归。
- 阶段 1.2 达到“全套零失败”的出口标准。

---

## Phase 00 F-4：全量测试失败判定基线（graph-rag-v2 通用包，2026-10-09）

- 采集命令（**全量**，未加 `-x`、未裁剪 `-k` 子集）：
  `python -m pytest tests -q --no-header -p no:cacheprovider 2>&1 | Tee-Object _tmp_full_pytest.txt`
- 采集时间（本机时区 UTC-05:00；括号内为 UTC）：
  - 第 1 轮：2026-10-09 10:38:45 → 10:49:43（15:38:45Z → 15:49:43Z）
  - 第 2 轮：2026-10-09 10:50:35 → 10:57:28（15:50:35Z → 15:57:28Z）
- 源码状态：HEAD `e187ee2bc8f10a6d3ba911781242964809250351`（2026-10-09 10:38:44 -0500，“Phase01 F-6/F-9：图与执行链契约集中化（qa_graph_contracts.py）+ 验收矩阵”）。工作区**非干净**（17 项未提交改动，含 `qa_pipeline.py` / `qa_orchestrator.py` / `qa_schema.py` / `qa_storage.py` / `config/qa_acceptance_questions.json` 等），且采集期间存在并发编辑——因此本基线与「该 revision + 该脏树状态」绑定，不绑定到某个更早的干净 revision。
- 模块与契约指纹同批留档：`baseline/qa-baseline-inventory.json`（`captured_at_utc` 2026-10-09T15:58:47Z，20 个模块 sha256、129 条仓库内依赖边、七个契约 schema 指纹）。

### 统计

| 轮次 | 收集用例数 | passed | failed | error | skipped | 用时 | 退出码 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1574 | 1573 | 0 | 0 | 1 | 647.73s | 0 |
| 2 | 1606 | 1605 | 0 | 0 | 1 | 425.57s | 0 |

第 2 轮比第 1 轮多 32 个用例：本次新增的守门测试 8 个（`tests/test_qa_baseline_inventory.py`）+ 并发加入的其它测试文件。两轮相差的是**用例集合**，不是失败集合。

### 失败用例清单

**无（0 条）**：两轮全量运行的日志里没有任何 `FAILED` / `ERROR` 行，退出码均为 0。

> 与任务前提的差异：任务书假设“仓库实测基线有失败用例”，但本轮在 HEAD `e187ee2`（含上述脏树）上两轮全量运行均为 **0 失败**。若确有更早 revision 上的失败名单，需按该 revision 单独采集，不能与本小节数字混用。

### 跳过与环境类问题分类（非失败，逐条标注）

| 类别 | 条目 | 说明与处置 |
| --- | --- | --- |
| 环境/数据态缺配置（跳过） | `tests/test_agent_reach_search.py::...::test_brand_hit_passes`（pytest 报告位置 `:116`，`skipTest` 在 `:118`）→ 1 skip，`SKIPPED: 该行业包未配置品牌词` | 当前行业包未配置品牌词导致的数据态跳过，非失败；不删、不改断言。 |
| 测试自带本地 HTTP 服务线程退出竞态（环境/偶发，仅告警） | `tests/test_qa_level1_stream.py::StreamingFirstByteTests::test_no_callback_keeps_non_streaming_path` 抛 `PytestUnhandledThreadExceptionWarning`：`socketserver.serve_forever` → `OSError: [WinError 10038] 在一个非套接字上尝试了一个操作` | 该用例本身 PASS，是 Windows 下 teardown 关 socket 的时序竞态；只作告警留档，暂不列为 Gate 失败项。后续若升级为失败，须先按此类归因再决定处置。 |
| 第三方库弃用噪音（非失败） | `autoscraper` 的 `find(text=...)` `DeprecationWarning`，集中在 `tests/test_site_scraper_models.py`（1040 条）与 `tests/test_listpage_template.py`（387 条） | 第 1 轮共 1456 warnings、第 2 轮 1455 warnings；等上游修复，本轮不处理。 |

### 判定口径

后续 Gate 一律按**同批用例前后对比**判定：以本小节两轮全量结果为判定基线（失败集合 = 空集），只认“相对基线新增的失败”；**禁止删测试、禁止用 skip/xfail 掩盖、禁止把断言改成恒真来制造 PASS**。用例数增加不算回归，失败集合变化才算。

