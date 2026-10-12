# START HERE
请连续实施本开发包，不要只给建议。
本开发包的目标是**让本项目的 AI 助手真正聪明**（行业情报采集与分析系统：多作用域内部证据库 + 带引用的问答）。

## 最高优先级原则：两层彻底分离（`00_REWRITE_SPEC.md` 第二节）
**不要把"他方项目的领域词"替换成"我们自己的行业词"** ——那只是把别人的领域耦合换成我们的领域耦合。正确做法是把领域词**中性化**：
- **通用引擎层**（所有 Phase 的算法、契约、图结构）**必须行业无关**：只允许抽象类型
  `Entity / Claim / Evidence / Source / Scope / TimeWindow / Relation / Verifier / Gap`；
  **不得出现任何具体行业词**——既不许保留他方项目的领域词，也**不许**把本项目自身任何行业的词
  （行业名、机构类型、产品名、专有俗称等，反例清单见 `00_REWRITE_SPEC.md` 第二节表格）
  写进算法、契约或 Gate。
- **行业只以配置进入**：关键词与锚点表、来源清单与权威级别、关系类型定义、时效策略、实体词表、分类权重。
- **隔离统一用中性 `scope`**（注明"当前实现 = `industry_pack_id`"），算法不得假设它一定是"行业"。
- 唯一例外：**本项目的真实实现名**可以写（`qa_attribution`、`qa_pipeline` 的 `citation_validation` 阶段、
  真实表名 `articles`、真实页面 `/panython/admin` 等）——那是我们的实现，不是领域词。

1. 读 `00_MASTER_RULES.md`。
2. 读 `01_V2_ARCHITECTURE.md`；这是唯一架构依据，不寻找 V1.0。
3. 读 `02_PHASE_OVERVIEW.md` 与 `tracking/IMPLEMENTATION_STATUS.md`。
4. 探查真实代码仓库，从第一个非 PASS 的 Must 子任务开始。
5. 严格执行对应 Phase 文件；每项实现后立即测试并更新 tracking。
6. Phase Gate 通过后自动进入下一阶段，不等待逐阶段确认。
7. 只有必须由用户提供凭据/业务选择/外部资源时标 BLOCKED；同时继续无依赖任务。
8. 不得在聊天中人工代替系统应实现的功能。
9. 一直执行至 Phase 17，最终输出实现报告、指标对比、限制、部署和回滚说明。

## 开始前必须先承认的三个现实前提
- **结构化产物填充率极低**：实测 A 机 7022 篇里事件三元组仅 3 行（0.0%）、实体归一 0 行。
  此时**建图＝建空图**；图相关阶段的前置是 `docs/爬虫改进实施步骤.md` 阶段 7（事件抽取回填）与阶段 8（建图）。
- **发布时间曾经全表为空**（0/6105），现已做到 RSS 与列表页在采集当刻写入（`publish_time.py`），
  并按精度标注 `exact/date/url/discovered`——**精度决定时效判定的可信度**。
- **内部证据库有准入闸门**：内容要进 AI 证据池必须过关键词/锚点闸门与正文质量闸门
  （`intel_topics._classification_admitted`），并支持 `admission_source='llm_semantic'` 的语义兜底。

## 已交付能力（要承接，不要重造）
- 【证据解析】`qa_attribution.QaAttributionService`（信息单元 + 归因链路）
- 【词元影响力】遮挡法近似（**必须标明 method**，代理法不得冒充梯度）
- 引用校验 = `qa_pipeline.py` 的 `citation_validation` 阶段（**不是独立模块**）
- 客观趋势 `trend_objective.py` / 海外标题雷达 `buzzing_radar.py` / 反爬止损 `antibot_detector.py`

现有项目与文档假设冲突时，优先保持兼容并依据真实探查结果实施；所有偏离写 DECISION_LOG。
