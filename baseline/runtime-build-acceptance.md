# 阶段 1.5：运行环境构建与拓扑验收

- 验收时间：2026-08-01（Asia/Hong_Kong）
- 构建方式：使用不可变应用镜像摘要作为基础，Docker 构建网络设为 `none`。
- 构建上下文：2.56 MB；`.env`、数据库、备份、认证状态、抓取结果和日志均由 `.dockerignore` 排除。
- Python 锁：`baseline/runtime-pip-freeze.txt` 共 106 个发行包，构建时与镜像内 `pip freeze --all` 逐行比对通过。
- Smoke 镜像：`firecrawlapp-crawler:baseline-smoke`，不可变 ID 已写入运行拓扑清单和 Compose 镜像锁。
- 健康检查：镜像构建阶段通过 Flask test client 调用 `/api/system/health`，HTTP 200 且 `success=true`；实际 8003 端点亦为 HTTP 200。

## 实际服务拓扑

- 对外 8003 由宿主机 `firecrawl.service` 运行 `start_with_schedule.py` 提供，服务 active/running，实际监听进程和 unit 文件 hash 已记录。
- Docker 同时运行 crawler、worker、intel-worker 和 Redis 四个服务；配置了 healthcheck 的三个服务均为 healthy，普通 worker 为 running。
- 当前普通 worker 在验收时仍承载活动任务，运行镜像 ID 早于当前 `latest`。为避免中断用户任务，本阶段没有重启它；真实运行 ID已保留，回滚目标则统一使用已通过 smoke 的不可变镜像锁。
- Compose 清单只保存环境变量键名；任何环境变量值和 `env_file` 内容均未保存。

## 已观察但不阻断本阶段的问题

1. 容器依赖中 `requests` 会报告 `chardet`/`charset-normalizer` 版本兼容警告。当前接口与全套测试通过，后续依赖升级必须单独验证，不能在基线阶段静默改包。
2. 使用全新空库导入整个应用时，旧 URL 迁移逻辑会打印缺表/旧字段警告；`/api/system/health` 仍通过。阶段 1.7 回滚演练必须使用阶段 1.3 的一致性数据库备份，不能把空库启动等同于完整恢复。
3. 宿主 Python 环境存在系统包与用户包的多版本并存；运行拓扑清单已保存全部可见发行包及集合 hash。后续 TradingAgents 部署应优先复用已锁定容器环境，避免继续扩大宿主环境漂移。

## 验收结论

- 固定文件 hash、容器依赖锁、镜像 ID、服务/端口/卷、宿主 systemd 服务和健康状态均可追溯。
- `baseline/docker-compose.image-lock.yml` 可与主 Compose 叠加，并配合 `--no-build` 在本机选择已验收的不可变镜像。
- 阶段 1.5 的构建与健康出口标准通过；上述三项观察进入阶段 1.7 恢复演练和后续依赖治理。
