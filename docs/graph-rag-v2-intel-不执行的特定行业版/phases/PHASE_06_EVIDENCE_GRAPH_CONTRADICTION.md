# Phase 06 — Evidence Graph & Contradiction

## 目标
把"我们现在知道什么"显式地维护成一张图：哪些情报事实已被支持、哪些被反驳、
哪些互相冲突、哪些还缺证据。**结论层（研判）只能从这张图里长出来**，
而不是从某一段文本里直接长出来。

## 边界
显式维护当前情报事实状态。

> **术语约定（引擎层硬规则）**：本阶段属于**通用引擎层**，正文一律用中性类型
> （`Claim` / `Evidence` / `Source` / `Scope` / `Relation` / `Contradiction`），
> **不得出现任何具体行业词**；作用域差异（权威级别、时效策略、关系类型）一律由
> Phase 01 的 `ScopeConfig` 提供。作用域统一写 `scope`（**当前实现 = `industry_pack_id`**）。

## 开始前
- [ ] 已读 MASTER_RULES
- [ ] 已读 V2 Architecture 相关章节
- [ ] 已读 IMPLEMENTATION_STATUS
- [ ] 前置 Gate 已通过（Phase 00 除外）

---

## 本项目真实基础与前置（决定了本 Phase 现在能建哪一层图）

Evidence Graph 的节点分三层，**三层的就绪度完全不同**，必须分开验收：

| 层 | 节点 / 边 | 现役载体 | 就绪度 |
|---|---|---|---|
| 断言层 | `Claim` ↔ `Evidence`（`SUPPORTS` / `REFUTES` / `DERIVED_FROM`） | `qa_claims` / `qa_evidence` / `qa_claim_evidence` | **已就绪**（现役 run 即产出） |
| 冲突层 | `Contradiction`（`CONTRADICTS` / `RESOLVES`） | `qa_conflicts`（`conflict_key` / `conflict_type` / `resolution` / `rationale`）+ `qa_pipeline` 的 `conflict_review` 阶段 | **部分就绪**（有表有阶段，缺系统性裁决口径） |
| 实体 / 事件层 | `Entity`（`MENTIONS` / `DEPENDS_ON`） | `intel_subject_canonical`（**实测 0 行**）、`intel_article_events`（**实测 3 行 = 0.0%**）、`kg_nodes` / `kg_edges`（未建） | **未就绪** |

> **硬结论**：本 Phase 的 P06-01 / P06-02 / P06-03 **现在就能做**（断言层 + 冲突层）；
> 涉及实体 / 事件节点的部分**必须等** `docs/爬虫改进实施步骤.md`
> **阶段 7（事件抽取回填）** 与 **阶段 8（建图）** 达标。
> 在 0.0% 的填充率下把实体层当"图已建好"来验收，等于把空图当成果。

---

## 子任务
### P06-01 Evidence Graph repository/API
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Evidence Graph repository/API`；不得提前实现后续 Phase。
  给"读图"和"写图"各一个唯一入口，禁止各处直连表。
- **实现方法：**
  - 读：基于现役三张表建视图 / 查询层（建议 `qa_evidence_graph` 视图或
    repository 类，**命名待确认**）：
    - 节点：`qa_claims`（断言）、`qa_evidence`（证据）、`qa_conflicts`（冲突）、
      `qa_retrieval._rows` 命中的文章（来源）。
    - 边：`qa_claim_evidence.relationship` + `relevance_score`。
  - 写：**只允许 Verifier 通过的证据入图**（Phase 03 硬规则）；
    写入接口必须校验 `run_id` / `industry_pack_id` / `verification_status`。
  - 查询接口（供 Phase 07 的 Gap Analyzer 与 Phase 16 的 UI 消费）：
    `claim_evidence(claim_key)` / `conflicts(claim_key)` / `unresolved()` /
    `coverage(claim_key)` / `by_pack(industry_pack_id)`。
  - **多租户**：所有查询强制带 `industry_pack_id`；`by_pack` 之外的接口
    不接受空包参数（防"顺手查全库"）。
  - 回放：给定 run_id 能重建当时那张图（依赖 P01-03 的版本字段）。
- **测试方式：** 单测：未验证证据写不进去；跨作用域查询被拒；同 run 重复写入幂等；
  回放重建的图与线上图一致（用 `qa_consistency.business_fingerprint` 做结构比对）。
- **验收标准：** 一个真实 run 的 Evidence Graph 可导出、可回放、可逐条点开来源；
  跨作用域查询 100% 被拒。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P06-02 SUPPORTS/REFUTES/DEPENDS/CONTRADICTS
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `SUPPORTS/REFUTES/DEPENDS/CONTRADICTS`；不得提前实现后续 Phase。
- **实现方法：**
  - 现役取值来源：`qa_claim_evidence.relationship`（现役字段）与
    `qa_attribution_links.relation`（`support` / `refute` / `irrelevant`，现役【证据解析】产物）。
    两者要**归一为同一取值域**（映射表写死并单测）：
    `SUPPORTS` / `REFUTES` / `MENTIONS` / `DERIVED_FROM` / `DEPENDS_ON` / `CONTRADICTS` / `RESOLVES`。
  - `DERIVED_FROM`（二手转述）来自 P02-04 的转载归组；
    `DEPENDS_ON`（断言间依赖，如"结论 H3 依赖事实 H1"）来自 Phase 05 的
    `dependencies[]`；`CONTRADICTS` / `RESOLVES` 由 P06-04 产出。
  - 边上必须带：`weight`（强度）、`reason`（可读原因）、`verifier_status`（Phase 03）、
    `independent_source_count`（独立性，来自 P02-04）。
  - **禁止多数投票**：边的数量不代表对错（`01_V2_ARCHITECTURE.md` §15）。
- **测试方式：** 单测：映射表覆盖所有现役取值；未知取值报错而不是静默丢弃；
  `relationship` + `relevance_score` 组合的边界用例。
- **验收标准：** 四种边都有真实数据（断言层能跑通）；实体 / 事件层的边显式标注"待前置"，
  不得以空数组冒充"验证通过"。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P06-03 claim coverage
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `claim coverage`；不得提前实现后续 Phase。
  回答"要答这个问题，还差哪几条 claim 没有证据"。
- **实现方法：**
  - 输入：Phase 05 的 `required_claims[]`（要证什么）+ 本图的 claim / evidence（证到了什么）。
  - 输出：`coverage = {claim_key, required, support_count, independent_source_count,
    support_mass, refute_mass, status}`，其中
    `status ∈ {COVERED, PARTIAL, MISSING, CONTRADICTED}`。
  - 聚合字段口径（`01_V2_ARCHITECTURE.md` §11）：聚合结果落 `qa_claims.payload_json`，
    必须可 SQL 查询（Phase 16 的看板要用）。
  - 与 Phase 07 的接口：`MISSING` / `PARTIAL` / `CONTRADICTED` 直接就是 Gap 的候选，
    **不得在本 Phase 自行触发新检索**（触发权归 P07-03）。
  - 时效参与覆盖度：`freshness_required=true` 的 claim，
    证据若只是 `precision='date'` 或 `'discovered'` → 最多算 `PARTIAL`
    （沿用 Phase 03 的精度口径，不另立标准）。
- **测试方式：** 单测：四种 status 各有用例；时效不足降级为 PARTIAL 的用例；
  覆盖率数值与人工抽检一致（抽 20 条 claim 核对）。
- **验收标准：** "还差什么"能一条条列出来且可解释；覆盖率可 SQL 汇总。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P06-04 Contradiction detector/resolver
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Contradiction detector/resolver`；不得提前实现后续 Phase。
- **实现方法：**
  - 现役基础：`qa_pipeline` 的 `conflict_review` 阶段 + `qa_conflicts` 表
    （`conflict_key` / `conflict_type` / `resolution` / `rationale` / `payload_json`）+ 
    `qa_synthesis` 的冲突输出（`_filter_conflicts_to_refs` / `_public_conflict_summary`，
    终答里的 `conflicts[].evidence_refs`）。
  - 检测：`E1 SUPPORTS C` 与 `E2 REFUTES C` 同时存在 → 生成 Contradiction 节点。
    对时效类冲突还要考虑"新版优于旧版"（发文即最高价值）。
  - 裁决维度（`01_V2_ARCHITECTURE.md` §15 的行业化）：来源级别（`authority_level`）、
    发布时间（含 `published_precision`）、版本、适用主体与定义是否一致、
    证据独立性、是否存在更新版本 / 是否已被废止或替代。
  - 裁决手段（本项目可用）：`ArticleRetriever._policy_exact_evidence` /
    `_policy_registry_rows`（政策原文与生效日期可用来裁"以谁为准"），
    `qa_planner` 的 `conflict` 关系策略（"先列出不同口径，再按来源权威性、发布日期和
    适用范围裁决"）——**沿用，不另写一套口径**。
  - 无法消解时**保留** `UNRESOLVED_CONTRADICTION`，
    并在终答里显式呈现不确定性（**不得替用户做结论**）。
  - ⚠️ 注意区分：`qa_consistency.compare_origin_outputs` / `business_fingerprint` 是
    **双机输出一致性比对工具**（用于兼容回归），**不是**冲突裁决器，不要误用。
- **测试方式：** 单测：支持 / 反驳并存 → 生成冲突；可裁决（新版本覆盖旧版本）用例；
  不可裁决用例保留状态；裁决理由必填；冲突不得导致答案静默丢弃一方（必须显式呈现）。
- **验收标准：** 冲突可检测、可裁决、可留痕；未消解冲突在终答与 UI 上都可见；
  裁决理由能逐条对上来源权威性 / 时间 / 适用性。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

---

## Phase 禁止事项
- 不得硬编码 benchmark 答案或删除/skip 测试。
- 不得把 BLOCKED 标为 PASS。
- 跨 Phase contract 变更必须先写 DECISION_LOG，并运行受影响回归。
- （本项目追加）不得用多数投票裁决冲突。
- （本项目追加）不得让最新证据自动覆盖旧证据（覆盖必须走显式裁决 + 留痕）。
- （本项目追加）不得把实体 / 事件层空图报告为"Evidence Graph 已建成"。

## Phase Gate
- [ ] 所有 Must 子任务 PASS
- [ ] Unit tests PASS
- [ ] Integration tests PASS
- [ ] Failure-path tests PASS
- [ ] Compatibility regression PASS
- [ ] IMPLEMENTATION_STATUS / ACCEPTANCE_MATRIX 已更新
- [ ] 无未处理高优先级 blocker
- [ ] 断言层与冲突层有真实数据支撑的验收证据
- [ ] 实体 / 事件层显式标注"等 `docs/爬虫改进实施步骤.md` 阶段 7 / 8"

## 阶段结束报告
```text
Phase:
Status:
Changed files:
Migrations:
Tests run:
Metrics before/after:
Known limitations:
Blocked items:
Rollback method:
Next phase readiness:
```
