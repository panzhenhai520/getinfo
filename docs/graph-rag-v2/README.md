# Graph-RAG Agent V2 — AI 编程智能体连续实施开发包
**无需另附 V1.0。** `01_V2_ARCHITECTURE.md` 是唯一架构依据。

首次使用：把整个目录放入目标仓库（建议 `docs/graph-rag-v2/`），将 `prompts/START_HERE.md` 内容交给 DeepSeek/Claude/Codex。
中断恢复：使用 `prompts/RESUME_PROMPT.md`，智能体从 tracking 的第一个非 PASS 项继续。

目录：
- MASTER_RULES：最高工程规则
- V2_ARCHITECTURE：完整架构
- PHASE_OVERVIEW：18 阶段依赖
- phases：逐阶段可勾选执行卡
- prompts：首次/长程/恢复/单阶段提示词
- tracking：状态/决策/验收/blocker
- schemas/reference：契约检查和指标

最终 DoD：Phase 00–17 Must 全 PASS；E2E/ablation/兼容/回滚通过；有真实指标报告；无未解释安全 blocker。
