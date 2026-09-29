# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目规则（最高优先级）

- **使用中文**回复、注释代码、写入 Plan 和 To-do
- 任务汇报在聊天中完成，**禁止**未经用户要求创建 `.md` 总结/说明文件
- **最小修改原则**：不允许推倒现有代码重构，只在源代码上找最优解；如有必要重构需询问用户
- 代码注释用中文，要清晰简洁，方便 review 逻辑流程
- 修改代码后主动检查修改部分的完整性和语法正确性
- 删除逻辑时把相关代码一并清理干净，减少复杂度

## 启动与运行

```bash
# 安装依赖（首次）
pip install -r requirements.txt
python -m playwright install chromium

# 初始化数据库（首次）
python init_sqlite_database.py init

# 本地开发启动
python start_with_schedule.py

# Docker 部署
docker compose -f docker-compose.crawler.yml up -d
```

默认访问地址：`http://127.0.0.1:8003`

## 架构概览

### 主入口链路

```
start_with_schedule.py        # 生产启动入口：启动监控线程后加载 Flask
  └── firecrawl_app.py        # Flask 主应用：注册蓝图、初始化 DB/Redis、定义核心路由
        ├── scheduler.py      # 定时任务调度器（ThreadPoolExecutor，按域名并发限制）
        ├── article_link_extractor.py   # 栏目发现 → 文章链接收集 → 正文抽取主链路
        ├── playwright_link_extractor.py # Playwright 动态页面链接发现
        ├── smart_article_extractor.py  # 多库正文抽取（trafilatura/readability/newspaper3k）
        └── ragflow_client.py           # RAGFlow 知识库上传客户端
```

### Flask 蓝图分工

| 蓝图 | 文件 | 功能 |
|------|------|------|
| `smart_bp` | `smart_extraction_api.py` | 单 URL 智能抽取 |
| `incremental_bp` | `incremental_crawl_api.py` | 增量爬取 API |
| `sqlite_bp` | `sqlite_api.py` | 文章数据库查询 |
| `article_management_bp` | `article_management_api.py` | 文章管理 |
| `url_management_bp` | `url_management_api.py` | 管理目标 URL 列表 |
| `schedule_management_bp` | `schedule_management_api.py` | 定时任务 CRUD |
| `schedule_execution_bp` | `schedule_execution_api.py` | 执行记录查询 |
| `crawl_task_bp` | `crawl_task_api.py` | 任务状态/日志查询 |
| `category_bp` | `category_management_api.py` | 分类管理 |
| `config_management_bp` | `config_management_api.py` | 运行时配置 |
| `auth_bp` | `auth_management_api.py` | Playwright 认证存储 |
| `user_bp` | `user_management_api.py` | 用户管理 |

### 公共工具模块

- `config.py` — 从 `.env` 加载所有配置，对外暴露常量和 `get_proxies()`、`get_playwright_proxy()` 等方法
- `sqlite_database.py` — SQLite 单例 `sqlite_db`，管理 articles / managed_urls / crawl_tasks / scheduled_tasks 等表
- `user_database.py` — 用户 + 会话管理（`UserDatabase`）
- `utils.py` — `get_china_time()`（UTC+8）、`coerce_int()`
- `decorators.py` — `@login_required`（检查 cookie `session_token`）
- `content_handlers.py` — `extract_with_newspaper3k`、`extract_article_links_from_list_page`、`is_valid_article_content`
- `crawl_options.py` — `normalize_crawl_options()`、`public_runtime_config()`
- `url_validation_helper.py` — `normalize_task_url()`、`validate_http_url()`
- `crawl_logger.py` — `get_crawl_logger(task_id)` 按任务写文件日志
- `keyword_filter.py` — 关键词过滤
- `cloudflare_bypass.py` — Cloudflare 绕过（curl_cffi / browserforge）
- `hybrid_crawler.py` — 多策略混合爬取（requests + Playwright 自动切换）
- `supplemental_link_discovery.py` — sitemap / RSS feed / 静态翻页等补充链接发现

### 数据存储

- **SQLite** `crawler_articles.db`（或 `DATABASE_PATH` 环境变量）— 主数据库
- **Redis**（可选）— 运行时辅助状态，连接失败时降级继续运行
- `crawl_results/` — 爬取任务 JSON 结果文件（`{task_id}_detail.json`）
- `auth_storage/` — Playwright 认证 Cookie 存储，迁移时需保留
- `crawl_logs/` — 按任务 ID 的文件日志

### 认证机制

- Cookie `session_token` 验证，通过 `user_db.verify_session(token)` 校验
- 免认证路径：`/login`、`/api/user/login`、`/static/`
- API 路由未认证时返回 `401 JSON`，页面路由重定向到 `/login`

## 配置

所有配置通过 `.env` 文件或环境变量注入，参考 `.env.example`。关键变量：

- `FLASK_PORT`（默认 8003）、`SECRET_KEY`（生产必须设置）
- `DATABASE_PATH`、`REDIS_HOST`/`REDIS_PORT`
- `RAGFLOW_BASE_URL`、`RAGFLOW_API_KEY`、`RAGFLOW_UPLOAD_ENABLED`
- `PROXY_ENABLED`、`PROXY_HTTP`/`PLAYWRIGHT_PROXY`（公网代理，默认关闭）
- `CRAWL_SCHEDULER_MAX_CONCURRENT`（并发爬取数，默认 4）
