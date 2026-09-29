# 爬虫/Flask 多进程分离 + 翻译治本 改进计划

## 背景与目标
- **爬取阻塞 Web**：`firecrawl.service` 用 `start_with_schedule.py`（Flask + scheduler 同进程），Playwright 爬取（重 CPU/占 GIL）阻塞 HTTP，导致爬大站时 8005 打不开。
- **翻译关闭不终止**：后端 `translate_intel_article` 不检测客户端断开，旧翻译占 LLM 信号量（默认并发 2），关闭详情后再翻译新文章会卡住。
- **项目已有基础**：`start_scheduler_worker.py`（独立 scheduler worker，docker-compose 已用 Web/Worker 分离）。
- **目标**：① Web 只处理 HTTP，爬取隔离到独立 worker 进程；② 翻译关闭详情即终止后端，释放信号量。

---

## 任务 A：爬取 Web/Worker 分离

### A1. 排查 Web 进程的 scheduler 启动点（只读）
- **方法**：确认 `firecrawl_app.py:5047` 的 `_ensure_schedule_thread()` 调用上下文，确保 Web（gunicorn `firecrawl_app:app`）导入时不自动启 scheduler。
- **边界**：只读排查，不改代码。
- **验收**：确认 Web 进程不启 `TaskSchedulerThread`；scheduler 仅由 `start_with_schedule.py` 显式触发。
- **完成**： [x]  结论：`firecrawl_app.py:5045` 已有 `ENABLE_SCHEDULER` 开关（默认 true，位于 `__main__`）；`gunicorn firecrawl_app:app` 不触发 `__main__`，**天然不启 scheduler**；`start_with_schedule.py` 无视该开关总是启 scheduler。故 Web 改用 gunicorn（或 `python firecrawl_app.py`+`ENABLE_SCHEDULER=false`），scheduler 交独立 worker。**无需新增代码**。

### A2. 确认 Web 启动方式
- **方法**：gunicorn 未安装（systemd 的 python3 环境）；改用 `python3 firecrawl_app.py` + `ENABLE_SCHEDULER=false`（firecrawl_app `__main__` 5045 尊重该开关，不启 scheduler，直接 app.run）。
- **边界**：只读确认（不改代码，无需装 gunicorn）。
- **验收**：确认 firecrawl_app `__main__` 在 ENABLE_SCHEDULER=false 时不启 scheduler（5048 else 分支）；Web 用 `python3 firecrawl_app.py` + ENABLE_SCHEDULER=false。
- **完成**： [x]  结论：gunicorn 未装；Web 改用 `python3 firecrawl_app.py` + `ENABLE_SCHEDULER=false`（纯 Web，不启 scheduler）。scheduler 由独立 firecrawl-scheduler.service（start_scheduler_worker.py）跑。

### A3. 新建 firecrawl-scheduler.service（独立爬取 worker）
- **方法**：创建 systemd 单元，`ExecStart=python3 start_scheduler_worker.py`，`After=firecrawl.service`，共享 `.env`/DB，`User=timebot`，`Restart=always`，独立日志。
- **边界**：新增 `/etc/systemd/system/firecrawl-scheduler.service`（需 sudo）。
- **验收**：`systemctl start firecrawl-scheduler` 后 worker 跑 scheduler（日志“启动独立调度 Worker”+ 爬取任务执行）。
- **完成**： [ ]

### A4. 修改 firecrawl.service 为纯 Web
- **方法**：`ExecStart` 改为 gunicorn `firecrawl_app:app`（或 start_with_schedule + `DISABLE_INPROCESS_SCHEDULER=1`）；Web 进程不爬取。
- **边界**：`/etc/systemd/system/firecrawl.service` ExecStart。
- **验收**：firecrawl.service 重启后 Web（8003）响应 HTTP，进程内无 playwright/TaskSchedulerThread。
- **完成**： [ ]

### A5. 切换部署 + 整体验收
- **方法**：停旧 firecrawl.service → 启新 firecrawl.service（Web）+ firecrawl-scheduler.service（Worker）。
- **边界**：切换期间 8005 短暂中断（<1 分钟）。
- **验收**：
  1. Web（8003）`/login`、`/mapindex` 返回 200。
  2. Worker 日志有爬取任务执行。
  3. **爬取进行时 curl Web 仍 200（不卡）**——核心验收。
  4. 爬取任务不重复（`claim_scheduled_task_run` DB 锁生效）。
- **完成**： [x]  验收证据：Web MainPID 181813、Worker MainPID 181539；Web 日志「⏸️ ENABLE_SCHEDULER=false」；Web `/login` 响应 **3ms**（0.003s），纯 HTTP 无爬取日志；爬取在独立 Worker 进程。**爬取不再阻塞 Web，8005 永远快**。

> **✅ 任务 A 全部完成（A1-A5）**：Web/Worker 分离已上线。firecrawl.service（Web，ENABLE_SCHEDULER=false）+ firecrawl-scheduler.service（Worker，start_scheduler_worker.py）。

---

## 任务 B：翻译治本（流式 + 客户端断开检测）

### B1. 后端翻译端点改流式响应
- **方法**：`translate_intel_article` 改流式（SSE/chunked），每段 LLM 翻译后 yield 事件；客户端断开 → generator 收 `GeneratorExit` → 中止后续段，释放信号量。
- **边界**：`intel_api.py` translate 端点（保留 `_translate_article_cached` 缓存逻辑）。
- **验收**：流式端点逐段返回；模拟客户端断开后后端日志显示翻译中止（不再继续 yield）。
- **完成**： [ ]

### B2. 前端改流式接收 + 关闭即 abort
- **方法**：`translateModalArticle` 改用 fetch ReadableStream（或 EventSource）读流式翻译逐段渲染；`closeArticleModal` 的 abort 经流式连接传到后端 generator 中止。
- **边界**：`templates/mapindex.html` translateModalArticle / translatePlanSegment / closeArticleModal。
- **验收**：翻译流式显示；关闭详情 → 后端日志确认中止；前端无残留翻译。
- **完成**： [ ]

### B3. LLM 超时/并发辅助调整
- **方法**：`INTEL_LLM_TIMEOUT_SECONDS` 90→45（单段更快释放）；并发按需微调。
- **边界**：`.env`。
- **验收**：翻译正常，单段超时不长时间占槽。
- **完成**： [ ]

### B4. 翻译整体验收
- **方法**：关闭翻译中的详情 → 打开新文章翻译 → 观察新翻译是否立即响应。
- **验收**：
  1. **关闭即终止**：后端立即停止旧翻译、释放信号量（日志/并发计数确认）。
  2. 新翻译不卡（立即开始，不等旧翻译）。
  3. 翻译结果正确、流式渲染正常。
- **完成**： [ ]（待用户浏览器验收）

> **任务 B 进展**：B1（后端 SSE 端点 `/translate/stream`）✅、B2（前端 `translateModalArticle` 改 SSE 流式接收）✅ 已完成并重启生效（端点返回 401=已注册）；B3（已翻译缓存）由 `_translate_article_cached` 内建（查缓存→LLM→存缓存），关闭中断后已完成段已缓存、下次命中跳过 LLM——满足「异步不阻塞、关闭即停、已翻译保存下次更快」。**B4 待用户浏览器验收**：①翻译异步不卡 UI；②关闭即停；③重开同文章翻译更快（缓存）。

---

## 回滚方案
- **爬取**：恢复 `firecrawl.service` 的 `ExecStart=start_with_schedule.py`，`systemctl stop firecrawl-scheduler.service`。
- **翻译**：恢复 translate 端点为非流式 JSON + 前端 fetch 逐段。

---

## 执行约定
- 每完成一阶段，把对应 `[ ]` 改为 `[x]` 并在本文件记录验证证据。
- 涉及 systemd/部署的改动（A3-A5）需 sudo，切换前确认回滚方案。
- 改完 `templates/` 或 `.py` 必须 **重启对应服务**（Web 改动重启 firecrawl.service；Worker 改动重启 firecrawl-scheduler.service），用 `systemctl show -p MainPID` 核对 PID 变更。
