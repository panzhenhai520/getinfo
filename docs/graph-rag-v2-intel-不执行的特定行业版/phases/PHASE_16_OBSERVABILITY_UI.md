# Phase 16 — Observability UI

## 目标
把**四图与决策全过程**变成**真实页面上看得见的东西**：不是新造一个"可观测性页面"，
而是在本项目已有的页面里把链路摊开——用户能看见助手**为什么这么答**、**依据是哪几句**、
**这一跳为什么选了它**、**哪个 `scope` 的信源被反爬拦住了**。

- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **定位：** 依赖 Phase 01（trace）、Phase 06（证据图）、Phase 08（上下文）、Phase 09–10（记忆）、
  Phase 11–12（skill 与失败记忆）、Phase 13（合成与终验）；被 Phase 17 验收依赖。
- **承接关系：** 【证据解析】与【词元影响力】的前端**已经交付**（`mapindex.html` 里的
  `.qa-attribution-button` / `.qa-influence-button`），本阶段在其上补齐四图与过程视图，**不推倒重画**。

## 边界
**四图和决策全过程可解释。**

允许做：Execution / Evidence / Context / Memory 四视图、检索轨迹、指标看板，以及把已有页面接上四图数据。
不允许做：新造业务页面与后端能力（本阶段只做"看得见"）；不得虚构页面上不存在的功能入口。

## 本项目真实的页面与接口（硬约束：只允许引用这些）
| 页面 | 路由 | 模板 | 本阶段挂什么 |
|---|---|---|---|
| AI 助手对话（首页驾驶舱） | `GET /`（`firecrawl_app.index`） | `templates/mapindex.html`；另有 `templates/_chat_mini.html` | 执行/证据/上下文/记忆四视图入口；【证据解析】【词元影响力】按钮所在「依据、分析与核验」抽屉 |
| 行业包管理页 | `GET /panython/admin`（`firecrawl_app.industry_pack_management`） | `templates/industry_pack_management.html` | 反爬拦截看板、RSS 订阅体检、海外标题雷达区块（均已存在） |
| 趋势页 | `GET /trends`（`firecrawl_app.intel_trends_page`） | `templates/intel_trends.html` | 客观曲线（接 `GET /api/intel/trends/objective`，**由主线实现**，见 P16-06） |

| 接口 | 路径 | 用途 |
|---|---|---|
| 对话发送 | `POST /api/chat/send`（`chat_api`） | AI 助手主入口 |
| 会话历史 | `GET /api/chat/history/sessions`、`GET /api/chat/history/session/<session_id>` | 会话恢复 |
| run 详情 / 事件 | `GET /api/qa/v1/runs/<run_id>`、`GET /api/qa/v1/runs/<run_id>/events` | Execution View |
| 【证据解析】 | `GET|POST /api/qa/v1/runs/<run_id>/attribution` | Evidence View（已交付） |
| 【词元影响力】 | `GET|POST /api/qa/v1/runs/<run_id>/token-influence` | Evidence View（已交付，须显示 method） |
| 指标 | `GET /api/qa/v1/metrics` | 看板 |
| 审计 | `GET /api/qa/v1/audit` | 审计视图（Phase 15 依赖） |
| 反爬健康报告 | `GET /api/intel/antibot/health-report` | 反爬看板（已存在） |
| 反爬恢复派发 | `POST /api/intel/antibot/sources/<id>/recover` | 反爬看板（已存在） |
| 反爬规则热加载 | `POST /api/intel/antibot/reload-rules` | 反爬看板（已存在） |
| RSS 订阅体检 | `GET /api/intel/rss-health` | RSS 体检按钮（已存在，只读，最多等 60 秒） |
| 海外标题雷达 | `GET /api/intel/buzzing-radar` | 雷达区块（已存在） |
| 趋势曲线（我方口径） | `GET /api/intel/trends` | 趋势页当前真实数据源 |
| 趋势曲线（客观口径） | `GET /api/intel/trends/objective` | **由主线实现，当前尚不存在**；交付后趋势页改用它出客观曲线（Phase 16 P16-06） |

## 开始前
- [ ] 已读 MASTER_RULES
- [ ] 已读 V2 Architecture 相关章节
- [ ] 已读 IMPLEMENTATION_STATUS
- [ ] 前置 Gate 已通过（Phase 00 除外）
- [ ] 已在浏览器里实地打开 `/`、`/panython/admin`、`/trends` 三个页面，确认上表的入口与接口真实存在
      （**不得凭想象画图**；对不上的地方以实测为准并更新本文件）

## Must 子任务

### P16-01 Execution View
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Execution View`；不得提前实现后续 Phase。
- **实现方法：**
  - 在 AI 助手对话页（`/`，`mapindex.html`）新增「执行过程」抽屉/面板，数据来自
    `GET /api/qa/v1/runs/<run_id>` 与 `/events`，按 `qa_contracts.QA_STAGES` 展示阶段推进。
  - 展示：阶段顺序与耗时、每阶段产出的节点数、走了哪些 skill（Phase 11 遥测）、
    每一跳的 stop reason 与降级事件（`degraded`）。
  - 必须能回答"这一跳为什么选它"：显示动作与排除理由（Phase 14 P14-02 的可分解效用分量）。
  - 该视图对用户只读；不得把内部密钥/凭据渲染到前端（复用 `qa_contracts._FORBIDDEN_CLIENT_FIELDS` 的口径）。
  - 复用现有页面的样式体系与深蓝皮肤规则（`mapindex.html` 已有抽屉与皮肤约定），不引入新视觉体系。
- **测试方式：** 前端集成测试（Playwright，参考现役 `_tmp_attribution_ui_verify.py` 的写法）+
  接口契约测试（`/runs/<id>` 与 `/events` 字段齐全）；失败路径（run 不存在 / 已取消时页面给出明确状态而非空白）。
- **验收标准：** 真实 run 的执行过程在页面上完整可见；阶段、耗时、stop reason、降级均可见；
  无敏感字段泄漏；截图证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P16-02 Evidence View
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Evidence View`；不得提前实现后续 Phase。
- **实现方法：**
  - **在已交付的「依据、分析与核验」抽屉上扩展**，不新建入口：
    - 【证据解析】按钮已有：展示信息单元 + 归因链路（支持/反驳/无关）+ 置信度；
    - 【词元影响力】按钮已有：展示信息单元内的词元影响力；
      **必须在 UI 上明确显示 `method`**，且 `occlusion_overlap_proxy` 必须显示为
      "词汇重叠代理（近似）"，**不得**写成"模型关注度/梯度归因"（Phase 13 硬要求）。
  - 新增/补齐：证据图关系（`SUPPORTS` / `REFUTES` / `DEPENDS` / `CONTRADICTS`，Phase 06）、
    引用校验结果（`citation_validation` 通过/失败与失败原因）、每条引用的可回溯跳转
    （`evidence_ref → article_id → 原文`）。
  - 反证与未决缺口（Phase 13 P13-03）必须在这一视图里能直接看到，不能只藏在答案文字里。
- **测试方式：** 前端集成测试（按钮点击 → 数据渲染；method 文案断言）+ 接口契约测试；
  失败路径（证据解析未计算 / 失败时给出可读提示，不是空面板）。
- **验收标准：** 信息单元与归因可见；词元影响力可见且 method 文案正确；引用校验结果与反证可见；
  与后端数据一致（抽查对账）；截图证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P16-03 Context Inspector
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Context Inspector`；不得提前实现后续 Phase。
- **实现方法：**
  - 展示 Phase 08 上下文包的选择结果：选入/淘汰了哪些 `ContextItem`、每个的选择理由与效用分、
    token 账（token budget 用了多少）、反证预留位是否被占用、Context Gap 是否发生了**误检索**。
  - 必须能看出"这个上下文项来自哪个 `scope`"，跨 `scope` 项出现即为缺陷（Phase 15 隔离）。
  - 数据来源为 run 的 stage 记录与 `selection trace`（Phase 08 P08-06），
    若无 trace 则本子任务 BLOCKED（**不得用事后重算冒充 trace**）。
- **测试方式：** 前端集成测试 + trace 契约测试；失败路径：trace 缺失时页面显示"该 run 无上下文轨迹"。
- **验收标准：** 上下文选择过程可复看；token 账可见；跨 `scope` 项 0；截图证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P16-04 Memory View
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Memory View`；不得提前实现后续 Phase。
- **实现方法：**
  - 展示记忆条目：`memory_type`、`scope`、`status`（`ACTIVE/STALE/SUPERSEDED/REVOKED/EXPIRED`）、
    `confidence`、`provenance`、生效条件。
  - **必须显示归属**：`owner_user_id` 与 `industry_pack_id` 以可读形式呈现（例如用户名 + 包名），
    让"这条记忆属于谁、属于哪个 `scope`"一眼可见——这是 Phase 15 作用域隔离的**可视化底线**。
  - 视图**只能看自己有权看的**：复用 Phase 15 的作用域过滤接口，前端不得自行过滤（前端过滤不等于隔离）。
  - 与【证据解析】一致地展示"曾被复验/被取代/被撤销"的历史（读 `qa_memory_events` + `GET /api/qa/v1/audit`）。
- **测试方式：** 前端集成测试 + 越权测试（用 A 用户会话打开 B 用户的记忆视图，必须查不到）；
  失败路径：跨 `scope` 记忆出现在列表里 → 测试必须失败。
- **验收标准：** 记忆状态与归属可见；越权 0 命中；撤销/取代历史可看；截图证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P16-05 Search Trace
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `Search Trace`；不得提前实现后续 Phase。
- **实现方法：**
  - 展示一次 run 的检索轨迹：查询改写、动作类型（Phase 14 七类）、命中的信源与文章数、
    被 `seen` 去重跳过的动作及理由（Phase 14 P14-05）、失败重试与版本感知重试（Phase 12 P12-06）。
  - **与反爬现实打通**：被 `antibot_detector` 判为"放弃派发"的信源要在轨迹里标出，
    并指向行业包管理页的反爬拦截看板（`GET /api/intel/antibot/health-report`），
    让"为什么这个包最近情报变少"能被解释，而不是当成"世界上没这事"。
  - 与客观趋势区分清楚：轨迹里的"文章命中数"是**我们收下的**，趋势页的客观曲线是**世界在说的**，
    UI 文案不得混为一谈（本项目趋势口径的根源就在这里，见 `trend_objective.py` 的模块说明）。
- **测试方式：** 前端集成测试 + 接口契约测试；失败路径：被反爬跳过的源必须可见（造一个被拦信源验证）。
- **验收标准：** 检索轨迹完整可见；被拦信源可见且可跳转看板；两种口径文案不混淆；截图证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

### P16-06 cost/latency/token/cache/skill dashboard
- **状态：** [ ] NOT_STARTED  [ ] IN_PROGRESS  [ ] PASS  [ ] FAIL  [ ] BLOCKED
- **边界/目标：** 只完成 `cost/latency/token/cache/skill dashboard`；不得提前实现后续 Phase。
- **实现方法：**
  - 看板数据统一来自 `GET /api/qa/v1/metrics` 与 `qa_events`（Phase 11 P11-06 的遥测），
    **不得**另建一套统计口径。
  - 指标口径对齐 `reference/METRICS.md`：latency / tokens / cost / cache hit / skill 成功率与版本维度。
  - **趋势页客观曲线**：趋势页 `/trends` 现有曲线数据来自 `GET /api/intel/trends`
    （口径是 `intel_topic_trends`，即"我们收下并过闸门的文章命中热度"）。
    它的**客观口径**由 `trend_objective.py`（`objective_series` / `top_objective_terms`，
    基于 `intel_candidates` 全量标题、按 domain 去重算独立媒体数）提供。
    **决策：新增接口 `GET /api/intel/trends/objective` 并接上趋势页，由主线负责实现。**
    - 该接口**此刻尚不存在**，本子任务不得假设它已可用；实施顺序是"主线先交付接口 → 本视图再接"。
    - 接上后的页面形态（主线已定）：**选一个关键词或事件 → 只出这一条客观曲线 + 我方覆盖副线 + 缺口 +
      国内/国外分色，默认选中客观热度第一名**。
    - 本子任务负责：在视图里并排呈现"客观热度（世界在说什么）"与"我们收下的热度"两条口径，
      **文案不得混为一谈**；接口未就绪时显示明确的"客观曲线接口未就绪"提示，不得用我方口径冒充客观口径。
  - 看板必须能按 `scope` 切换（指标也按 `scope` 收口），并显示 A/B 机差异（A 机无 RAGFlow 一级检索模式）。
- **测试方式：** 前端集成测试 + 指标一致性测试（页面数值 vs 接口数值）；
  失败路径：接口不可用时看板显示明确降级提示而不是显示 0 冒充"没有消耗"。
- **验收标准：** 指标可按 `scope` 查看且与接口一致；接口不可用时不误导；
  客观曲线与"我们收下的热度"两条口径并排且文案不混淆；客观曲线接口未就绪时有明确提示；截图证据写入 tracking。
- **完成证据：** `<文件/commit/测试命令/结果/指标/截图路径>`

## Phase 禁止事项
- 不得硬编码 benchmark 答案或删除/skip 测试。
- 不得把 BLOCKED 标为 PASS。
- 跨 Phase contract 变更必须先写 DECISION_LOG，并运行受影响回归。
- 不得虚构页面上不存在的入口或接口（本文件的页面/接口表是白名单）。
- 不得在前端渲染密钥/凭据，也不得把"前端过滤"当作作用域隔离。
- 不得把 `occlusion_overlap_proxy` 呈现为梯度或"模型真实关注度"。
- 不得把"我们收下的文章热度"与"世界客观热度"在文案上混为一谈。

## Phase Gate
- [ ] 所有 Must 子任务 PASS
- [ ] Unit tests PASS
- [ ] Integration tests PASS
- [ ] Failure-path tests PASS
- [ ] 前端端到端测试 PASS（`/`、`/panython/admin`、`/trends` 三页真实渲染 + 关键交互）
- [ ] Compatibility regression PASS（现役证据解析/词元影响力按钮行为不回退）
- [ ] IMPLEMENTATION_STATUS / ACCEPTANCE_MATRIX 已更新
- [ ] 无未处理高优先级 blocker

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
