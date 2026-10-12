# ACCEPTANCE MATRIX

> 字段结构固定为：`ID | 验收项 | 测试/命令 | 期望 | 实际 | 状态 | 证据路径`。
> 本表按 **Phase 级 Gate** 逐条登记；每个 Phase 内 Must 子任务的逐项证据写入该 Phase 文件的「完成证据」与
> `IMPLEMENTATION_STATUS`。
> **当前状态：全部为「未开始」，实际列与证据列留空待填。**
> 填表纪律：`期望` 必须是可判定的（数字或明确是/否）；`实际` 必须来自真实运行输出；
> 无证据不得填 PASS；样本不足写「样本不足」；拿不准写「待确认」。

|ID|验收项|测试/命令|期望|实际|状态|证据路径|
|---|---|---|---|---|---|---|
|A00|Phase 00 Gate：环境/契约冻结/benchmark/baseline|待确认|benchmark 冻结且 baseline 记录完整||NOT_STARTED||
|A01|Phase 01 Gate：四图契约与可回放 session|待确认|契约可序列化且可回放||NOT_STARTED||
|A02|Phase 02 Gate：证据层（provenance/去重/seen）|待确认|证据可回溯到 article_id + span||NOT_STARTED||
|A03|Phase 03 Gate：Verifier 才能入图|待确认|未验证证据入图数 = 0||NOT_STARTED||
|A04|Phase 04 Gate：并行检索舰队|待确认|fan-out/timeout/fallback 全部有测试||NOT_STARTED||
|A05|Phase 05 Gate：Research Planner 与 Execution Graph|待确认|预算裁剪可见且可回放||NOT_STARTED||
|A06|Phase 06 Gate：证据图与冲突消解|待确认|未消解冲突 0 次进入结论||NOT_STARTED||
|A07|Phase 07 Gate：Gap 驱动多跳与收敛|待确认|同一 gap 不重复触发同一动作||NOT_STARTED||
|A08|Phase 08 Gate：上下文计划与 Context Gap|待确认|Context-gap 误检索 = 0||NOT_STARTED||
|A09|Phase 09 Gate：记忆底座与 Write Gate|待确认|跨用户/跨 `scope` 召回 = 0；无绕过 Write Gate 的写入路径||NOT_STARTED||
|A10|Phase 10 Gate：时效/复验/冲突/撤销|待确认|高风险记忆未复验不得进结论||NOT_STARTED||
|A11|Phase 11 Gate：Skill Registry 与 Router|待确认|未注册 skill 调用数 = 0；A 机不路由 RAGFlow skill||NOT_STARTED||
|A12|Phase 12 Gate：经验记忆|待确认|失败记忆带版本四元组；版本变化可重试||NOT_STARTED||
|A13|Phase 13 Gate：回答合成与终验|待确认|终验不通过时输出 0 字答案；悬空引用 = 0；【词元影响力】method 已标注||NOT_STARTED||
|A14|Phase 14 Gate：统一采集动作（内部证据库/结构化数据/公开网络/趋势信号/向用户澄清）|待确认|跨 `scope` 动作候选 = 0；五类动作契约齐备且领域无关||NOT_STARTED||
|A15|Phase 15 Gate：记忆安全与作用域隔离|待确认|跨用户/跨 `scope` 泄露 = 0；许可校验范围严格等于配置声明的作用域（不多不少）；审计覆盖率 100%||NOT_STARTED||
|A16|Phase 16 Gate：可观测性 UI|待确认|`/`、`/panython/admin`、`/trends` 三页真实渲染且四视图数据一致；客观曲线（`GET /api/intel/trends/objective`，主线交付）与我方口径并排且不混淆||NOT_STARTED||
|A17|Phase 17 Gate：E2E/消融/发布|待确认|L0–L6 消融有真实数字；L7 明确 BLOCKED；回滚演练通过||NOT_STARTED||

## 验收时必须同时声明的前提（否则数字不可解释）
- 所有指标按中性的 **`scope`** 收口（当前实现 = `industry_pack_id`）。
- 事件三元组、实体归一的填充率（当前实测 0.0% / 0 行）：图相关验收项在填充率接近 0 时必须标 BLOCKED。
- 发布时间精度分布（`exact/date/url/discovered`）：影响时效类验收项的可信度。
- 被反爬止损的信源数：影响检索类验收项的可及性，不能把采集限制读成"世界没这事"。
- 引擎层**领域无关性**自检：算法与契约里不得出现任何具体行业词（两向都算缺陷）。
