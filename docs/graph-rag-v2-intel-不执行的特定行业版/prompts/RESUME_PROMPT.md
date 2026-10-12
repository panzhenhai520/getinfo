# RESUME PROMPT
继续实施，不重新设计、不从 Phase 00 重来。
先读 STATUS、BLOCKERS、DECISION_LOG 和当前 Phase，从第一个非 PASS Must 继续。
复核因近期代码变化可能失效的关键 PASS 回归；失效则恢复为 IN_PROGRESS 并修复。
连续执行至新 blocker 或 Phase 17 完成。

恢复时特别注意（本项目现状）：
- 两层彻底分离仍是最高优先级：**引擎层必须行业无关**，只认中性的 `scope`（当前实现 = `industry_pack_id`）；
  发现哪一步把具体行业词写进了算法/契约，就把它退回配置层。
- 事件三元组填充率 0.0%（7022 篇仅 3 行）、实体归一 0 行：**图相关 Must 仍应保持 BLOCKED**，
  直到 `docs/爬虫改进实施步骤.md` 阶段 7/8 完成，不要因为"重新跑一遍"就把它标成 PASS。
- 发布时间精度分布可能被新采集批次改变（`publish_time.py`），会影响 Phase 10 的时效判定回归，需复核。
- 反爬止损名单来自 `antibot_detector`（`config/antibot_rules.json` 可热加载），
  信源被跳过会改变检索可及性，需复核受影响的检索类 PASS。
- 汇报在聊天中完成并写进既有 tracking 文档，**不新建额外交付 `.md`**。
