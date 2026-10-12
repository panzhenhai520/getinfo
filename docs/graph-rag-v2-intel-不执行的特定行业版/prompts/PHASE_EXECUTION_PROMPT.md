# PHASE EXECUTION PROMPT
只执行指定 Phase：读取 MASTER_RULES、V2 Architecture、STATUS、本 Phase；确认前置 Gate；按子任务执行实现/Unit/Integration/Failure/Regression；有证据才 PASS；完成 Gate 并更新 tracking；不得提前实现下一 Phase。

本项目的执行口径：
- 术语按 `00_REWRITE_SPEC.md` 的对照表；**引擎层必须行业无关**，出现他方项目残留词或把本项目行业词写进算法，
  都算改写缺陷（行业只允许以「作用域 / 配置」进入，契约里统一用中性字段 `scope`，当前实现 = `industry_pack_id`）。
- 引用本项目文件时用真实路径（例如 `qa_gateway.py`、`qa_attribution.py`、`intel_api.py`、
  `templates/mapindex.html`、`templates/industry_pack_management.html`）；不确定写"待确认"，不要编造。
- 触及证据检索、记忆召回、动作路由时，先确认 `scope` 收口是否存在（当前实现 = `industry_pack_id`）。
- 触及【词元影响力】时，确认返回值带 `method` 且文案没有把代理法说成梯度。
