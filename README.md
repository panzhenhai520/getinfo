# 智能文章爬取系统

用于管理网站栏目链接、定时爬取文章、抽取正文、去重入库，并可按任务上传到 RAGFlow。

## 启动

```bash
pip install -r requirements.txt
python -m playwright install chromium
python init_sqlite_database.py init
python start_with_schedule.py
```

默认访问地址：

```text
http://127.0.0.1:8003
```

## 主要入口

- `start_with_schedule.py`：生产启动入口，包含 Web 服务和定时任务。
- `firecrawl_app.py`：Flask 主应用。
- `scheduler.py`：定时任务调度。
- `article_link_extractor.py`：栏目链接发现和文章处理主链路。
- `playwright_link_extractor.py`：动态页面链接发现。
- `smart_article_extractor.py`：正文抽取增强。
- `ragflow_client.py`：RAGFlow 上传客户端。

## 运行说明

- 配置项优先放在 `.env`，示例见 `.env.example`。
- 数据库默认使用 `data/crawler_articles.db`，避免根目录和容器挂载目录出现两份数据库。
- 登录态保存在 `auth_storage/`，交付或迁移时如需保留认证爬取能力，不要删除该目录。
- 日志和爬取结果目录可由程序运行时自动生成。

## 交付注意

当前目录已清理测试脚本、旧修复脚本、IDE 配置、`node_modules`、临时日志、截图和历史说明文档。保留下来的文件为运行主链路、基础运维说明、静态资源、模板和现有数据库。
