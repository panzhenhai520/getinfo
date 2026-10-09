# PHASE OVERVIEW

|Phase|名称|边界|前置|
|---|---|---|---|
|00|Project Discovery & Baseline|只读探查、冻结原 RAG 与 benchmark|无|
|01|Contracts & Research State|建立四图共享的类型契约和可回放 session|00|
|02|Evidence Layer|Chunk 升级为可验证 Evidence|01|
|03|Verifier Layer|候选证据验证后才能入图|02|
|04|Parallel Retrieval Fleet|多路检索并行且可降级|03|
|05|Research Planner & Execution Graph|问题拆成真实依赖 DAG|04|
|06|Evidence Graph & Contradiction|显式维护当前事实状态|05|
|07|Gap Analyzer & Dynamic Multi-hop|Evidence Gap 驱动下一跳和收敛|06|
|08|Context Graph & Context Planner|按任务组装最小有效 Context|07|
|09|Memory Graph Core|跨会话长期记忆及 Write Gate|08|
|10|Revalidation & Memory Conflict|时效、冲突、失效和污染撤销|09|
|11|Skill Registry & Router|按任务加载最小必要能力|10|
|12|Episodic Failure Strategy Memory|学习成功和失败检索经验|11|
|13|Answer Composer & Final Verifier|只用当前已验证证据回答|12|
|14|SAGE-RAG Unified Acquisition|统一 KB/EMR/DB/Web/患者追问|13|
|15|Clinical Memory Safety|医疗 Memory scope 与 encounter 边界|14|
|16|Observability UI|四图和决策全过程可解释|15|
|17|E2E Ablation Release|证明收益、兼容、可回滚发布|16|


**Gate：** 当前 Phase 的 Must 子任务未全部 PASS，不进入依赖它的下一阶段。