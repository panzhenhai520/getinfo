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
