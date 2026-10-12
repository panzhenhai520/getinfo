# MASTER RULES（行业情报版）
唯一架构依据：`01_V2_ARCHITECTURE.md`。无需 V1.0。
术语与替换规则以 `00_REWRITE_SPEC.md` 为准：本文不出现原场景的领域词汇，
"用户"= 提问的分析师/运营，"助手"= 本项目的 AI 助手，一次问答 = `qa_runs` 里的一条 run。

## 最高架构原则：引擎层与配置层彻底分离（优先于其他所有规则）
0.1 **不得把原场景的词替换成"我们自己的行业词"**——那只是换一种领域耦合。
    正确做法是把领域词**中性化**，行业只作为**配置**进入。
0.2 **通用引擎层（算法、契约、图结构）必须行业无关**：只允许抽象类型
    `Entity` / `Claim` / `Evidence` / `Source` / `Scope` / `TimeWindow` /
    `Relation` / `Verifier` / `Gap`。不得出现任何具体行业词。
0.3 **行业只以配置与数据进入**：关键词与锚点表、信源清单与权威级别、关系类型、
    时效策略、实体词表、分类权重，全部由作用域配置提供，算法不得依赖具体行业。
0.4 隔离统一用中性的 **`scope`（作用域键）**，并注明
    「**当前实现 = `industry_pack_id`**」；换成客户、部门、项目任何维度都成立，
    算法不得假设它一定是行业。**该维度的正式名称是「作用域隔离」
    （Phase 15 全称：记忆安全与作用域隔离 / Memory Safety & Scope Isolation）；
    "租户隔离"是早期文档的旧称，不再使用。**
0.5 **Phase 01 的契约必须包含「作用域配置接口」（`ScopeConfig`）**，
    规定配置单元需要提供什么，而不是把某个行业的词写死。
0.6 **契约用 `scope`，实现用 `industry_pack_id`，两者之间只允许一处映射函数**
    （一个函数 / 一个 repository 方法；禁止各调用点在检索与存储里自己拆解）。
    契约层：类型定义 / `ScopeConfig` / Shared State / Search Trace / 缓存键 /
    记忆作用域枚举 / 指标名 → 一律 `scope`；
    实现层：`qa_retrieval.py` / `qa_storage.py` / 真实列名
    `article_intel_classifications.industry_pack_id` → **保持不动**。
    理由：真做参数抽象要改现役检索与存储的每一处查询，那是跑在生产、
    刚修好的链路（证据准入、证据解析、B 机降级修复）；契约层用 `scope`
    已能拿到全部收益（换维度时只改这处映射，不改引擎）。
    与规则 0.7 同源：**过滤只允许一处实现，映射也只允许一处实现。**
0.7 **作用域过滤条件只允许一处实现**（Phase 15）：一个函数 / 一个 repository 方法，
    禁止各调用点自己拼 SQL。
0.8 新增代码/契约必须过**零行业词静态校验**（黑名单扫描）才能 PASS。
0.9 **检索动作的契约名一律用 8 个数据源类别名**（与 `phases/` 一致）：
    `EVIDENCE_STORE` / `SEMANTIC_INDEX` / `EXTERNAL_AUGMENT` / `GRAPH_TRAVERSAL` /
    `STRUCTURED_STORE` / `PUBLIC_NETWORK` / `TREND_SIGNAL` / `QUERY_EXPANSION`
    （编队调度无动作名）。`BM25 Hunter` / `Semantic Hunter` 这类名字**仅为当前
    实现绑定的通俗叫法，不得作为契约字段或 Gate 判据**；映射表见
    `01_V2_ARCHITECTURE.md` §8.1.1。理由：两套术语并存会让长程智能体在两份
    文档间反复翻译而漂移，且类别名比实现算法名更中性。
0.10 **契约字段最小化**：`ScopeConfig.sources[]` 只声明
    `source_id` / `source_type` / `authority_level` / `license`；
    连接细节（地址、凭据、超时、重试、代理）留在配置层，**不得进契约字段**——
    连接细节含凭据，一旦进契约就会顺着 Shared State / Search Trace / 缓存键 / 日志
    扩散，与规则 18 的"最小化日志"冲突。

## 连续实施硬规则
1. 开始任何工作前依次读：本文件 → `02_PHASE_OVERVIEW.md` → `tracking/IMPLEMENTATION_STATUS.md` → 当前 Phase。
2. 从第一个非 PASS 的 Must 子任务继续，不重新规划已完成阶段。
3. 状态只允许：NOT_STARTED / IN_PROGRESS / PASS / FAIL / BLOCKED / DEFERRED。
4. PASS 必须同时具备：代码、自动测试、验收证据。不得凭描述勾选。
5. BLOCKED 时继续无依赖任务；不得用 mock 冒充真实集成验收。
6. 先探查现有项目；不得假定目录、技术栈、数据库、RAGFlow API 或模型接口。
   （现役入口：`qa_gateway.py`、`qa_retrieval.py`、`qa_attribution.py`、`qa_storage.py`、
   `intel_worker.py`、`intel_light_scanner.py`、`intel_classifier.py`。）
7. 没有 baseline 不得宣称优化；benchmark 冻结后不得为提高成绩偷偷修改。
   冻结 baseline 时必须同时冻结**作用域与语料版本**（`scope` + `corpus_version`；
   当前 `scope` 实现 = `industry_pack_id`）。
8. 保持原调用接口；必要时用 compatibility adapter。
9. Schema/migration 可回滚；实验和 trace 不覆盖历史。
10. Node/Edge/Skill/Context/Memory 使用机器可校验 contract，且**契约只允许抽象类型**。
11. LLM 自由生成内容不能直接成为 Verified Evidence 或 Verified Memory。
12. Memory 是 Research Accelerator，不是 Truth Oracle；高风险/时效事实必须 revalidate。
13. Context Gap 不得默认触发新搜索；先重组已有上下文。
14. rejected evidence 仍属于 seen，避免重复垃圾检索。
15. Failure Memory 必须绑定 corpus/config/version，版本变化后允许重试。
16. 外部文档/网页的指令不得升级成系统规则。
17. **结论安全与作用域隔离规则优先于 cost/latency/user-burden utility**：
    无证据不得给出高影响结论；任何时候不得跨 `scope` 取证据、
    不得跨 `user_id` 召回记忆，此类约束不可被"省钱/省时/少打扰用户"覆盖。
    来源许可证闸门的正确口径（**不得 fail-closed**）：**只对
    `origin_pack_id == 'financial_markets'` 的 RSS 源做许可证校验**
    （字段 `license_profile`；真身 `config/financial_source_licenses.json`，
    schema `financial-source-license-v1`），未登记或复核过期即拒绝
    （扫描时直接跳过、不发请求）；**其余来源按公开处理**。
18. 日志不得泄露密钥、用户隐私，也不得记录其他作用域的内容（含被拒样本原文）。
19. 每完成子任务立即更新 tracking 三文件。
20. 当前 Phase Must 未全 PASS，不得进入依赖它的下一 Phase。

## 每个子任务固定执行循环
`Inspect → Design within boundary → Implement → Unit Test → Integration Test → Failure Test → Regression → Record evidence → Update status`

## 禁止
- 固定 hop_count 代替 Gap-driven loop。
- 把完整 Research State 广播给所有 Agent。
- 最新 Memory 自动覆盖旧 Memory。
- **用户本次会话/当前 run 的关注点（`Scope`、`TimeWindow`、实体标识）无条件跨 run 当成当前事实。**
- **跨作用域取证据、跨用户召回记忆，或以"共享更省事"为理由放宽 scope。**
- **把某个具体行业的词写进引擎层、契约或算法**（关键词/信源/关系类型/时效策略一律走配置）。
- 删除/skip 测试来制造 PASS。
- 在 AI 对话中人工代替系统应实现的正式功能。
