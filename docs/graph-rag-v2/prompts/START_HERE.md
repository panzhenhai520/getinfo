# START HERE
请连续实施本开发包，不要只给建议。
1. 读 `00_MASTER_RULES.md`。
2. 读 `01_V2_ARCHITECTURE.md`；这是唯一架构依据，不寻找 V1.0。
3. 读 `02_PHASE_OVERVIEW.md` 与 `tracking/IMPLEMENTATION_STATUS.md`。
4. 探查真实代码仓库，从第一个非 PASS 的 Must 子任务开始。
5. 严格执行对应 Phase 文件；每项实现后立即测试并更新 tracking。
6. Phase Gate 通过后自动进入下一阶段，不等待逐阶段确认。
7. 只有必须由用户提供凭据/业务选择/外部资源时标 BLOCKED；同时继续无依赖任务。
8. 不得在聊天中人工代替系统应实现的功能。
9. 一直执行至 Phase 17，最终输出实现报告、指标对比、限制、部署和回滚说明。
现有项目与文档假设冲突时，优先保持兼容并依据真实探查结果实施；所有偏离写 DECISION_LOG。
