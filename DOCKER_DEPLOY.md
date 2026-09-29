# 爬虫程序 Docker 部署说明

这个 Docker 文件是给当前爬虫程序用的，不是 RAGFlow。根目录里的 `docker-compose-base.yml` 是之前 RAGFlow 相关文件，不要用它启动爬虫。

## 需要保留的数据

- `data/crawler_articles.db`：SQLite 数据库，保存用户、任务、文章等数据。
- `crawl_results/`：爬取结果文件。
- `auth_storage/`：登录态、认证抓取相关文件。
- `crawl_logs/`：运行日志。

如果你现在根目录已经有 `crawler_articles.db`，第一次 Docker 部署前建议这样放：

```bash
mkdir -p data crawl_results auth_storage crawl_logs
cp crawler_articles.db data/crawler_articles.db
```

如果不拷贝，容器第一次启动会自动初始化一个空数据库。

## 在线环境部署

1. 准备配置：

```bash
cp .env.example .env
```

然后编辑 `.env`，至少把下面两个改掉：

```env
SECRET_KEY=换成随机字符串
DEFAULT_ADMIN_PASSWORD=换成自己的管理员密码
```

如果爬虫结果要上传 RAGFlow，再配置：

```env
RAGFLOW_BASE_URL=http://你的RAGFlow地址
RAGFLOW_API_KEY=你的API_KEY
RAGFLOW_UPLOAD_ENABLED=true
```

2. 启动：

```bash
docker compose -f docker-compose.crawler.yml up -d --build
```

3. 查看状态和日志：

```bash
docker compose -f docker-compose.crawler.yml ps
docker compose -f docker-compose.crawler.yml logs -f crawler
```

4. 浏览器访问：

```text
http://服务器IP:8003
```

如果宿主机 8003 被占用，可以在 `.env` 增加：

```env
CRAWLER_HOST_PORT=8010
```

然后访问 `http://服务器IP:8010`。

## 内网/离线环境部署

先在有网络的机器上构建并导出镜像：

```bash
docker compose -f docker-compose.crawler.yml build
docker pull redis:7-alpine
docker save firecrawlapp-crawler:latest redis:7-alpine -o firecrawl_crawler_images.tar
```

把项目目录和 `firecrawl_crawler_images.tar` 拷到客户 Ubuntu 机器后执行：

```bash
docker load -i firecrawl_crawler_images.tar
cp .env.example .env
mkdir -p data crawl_results auth_storage crawl_logs
docker compose -f docker-compose.crawler.yml up -d
```

## 常用命令

停止：

```bash
docker compose -f docker-compose.crawler.yml down
```

重启：

```bash
docker compose -f docker-compose.crawler.yml restart crawler
```

进入容器：

```bash
docker exec -it firecrawl-crawler bash
```

手动初始化数据库：

```bash
docker compose -f docker-compose.crawler.yml run --rm crawler python init_sqlite_database.py init
```
