# RESUME PROMPT
继续实施，不重新设计、不从 Phase 00 重来。
先读 STATUS、BLOCKERS、DECISION_LOG 和当前 Phase，从第一个非 PASS Must 继续。
复核因近期代码变化可能失效的关键 PASS 回归；失效则恢复为 IN_PROGRESS 并修复。
连续执行至新 blocker 或 Phase 17 完成。
