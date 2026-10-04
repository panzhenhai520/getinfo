# QA Worker 独立泳道 · 生产发布清单

适用范围：把统一 QA（AI 助手）发布到生产（`117.50.211.93:8003` / 宿主机 `10.88.0.3`，
部署目录 `/www/CollectInfo_latest_new`）。

> 一句话前提：**`qa.run` 已经从 long 泳道移出，只有 `qa-worker` 容器会领取它。**
> 代码与 `qa-worker` 服务必须同一批上线，否则用户提问会一直停在 `queued`。

---

## 0. 这次发布包含什么

| 文件 / 位置 | 作用 | 缺失后果 |
|---|---|---|
| `qa_*.py`（qa_gateway / qa_pipeline / qa_planner / qa_synthesis / qa_level1 / qa_ragflow_client / qa_research / qa_retrieval / qa_storage / qa_flags / qa_relevance …）+ `qa_templates/` | 统一 QA 全链路 | 助手回落旧链路或直接不可用 |
| `financial_resource_isolation.py` | `qa.run` 从 `LONG_FINANCIAL_JOB_TYPES` 拆到新的 `QA_WORKER_JOB_TYPES` | 与下一条组合出"双消费者"或"无人消费" |
| `intel_worker.py` | 注册 `qa.run` handler | QA 作业无人处理 |
| `templates/mapindex.html` | QA 前端（问题分析思路卡片、进度、证据抽屉、引用高亮、调整思路） | 只有后端能力，界面看不到 |
| `docker-compose.prod.yml` | 新增 `qa-worker` 服务 | **必配**，见下 |
| `.env`（生产机） | `RAGFLOW_BASE_URL` / `RAGFLOW_API_KEY` / KB 注册表 / QA 开关 | 二级检索降级为只用一级 |
| `deploy/systemd/info-aggregator-qa-worker.service` | 仅非 Docker 部署用 | Docker 部署忽略此项 |

生产现状（发布前基线，2026-10-05 实测）：8 核 / 15G，容器
`collectinfo-web`、`collectinfo-worker`、`collectinfo-intel-worker`、`collectinfo-postgres`、
`collectinfo-redis`；`collectinfo-intel-worker` 内部跑 **core + long 两条泳道**（supervisor），
`.env` 里 `RAGFLOW_BASE_URL=` 为空、没有任何 `UNIFIED_QA_*`/`QA_*`。

---

## 1. 发布前检查

在宿主机 `10.88.0.3` 上：

```bash
cd /www/CollectInfo_latest_new
docker ps --format 'table {{.Names}}\t{{.Status}}'          # 五个容器都应 healthy/Up
grep -nE '^(UNIFIED_QA|QA_|RAGFLOW_BASE_URL|RAGFLOW_API_KEY|RAGFLOW_KB_ID)' .env
nproc; free -g                                              # 核对 CPU/内存预算
docker exec collectinfo-intel-worker sh -c "ps -eo args | grep '[i]ntel_worker.py'"
#   ↑ 记下当前两条泳道的参数，发布后要对比：long 泳道里不应再出现 qa.run
```

备份：

```bash
docker tag collectinfo-web:latest collectinfo-web:rollback-$(date +%Y%m%d%H%M)
cp .env .env.bak-qa-$(date +%Y%m%d%H%M%S)
```

`.env` 里需要补齐（示例，按实际环境填）：

```ini
RAGFLOW_BASE_URL=http://<ragflow-host>:9222
RAGFLOW_API_KEY=ragflow-...
RAGFLOW_KB_ID=<kb id>              # 或用 config/ragflow_kb_registry.json 统一注册
# 私网模型端点必须显式加白名单，否则 qa_security 会 fail-closed
QA_ALLOWED_PROVIDER_HOSTS=<内网 LLM 主机>
QA_ALLOWED_OUTBOUND_HOSTS=<内网 LLM 主机>
# 灰度期先只走一级（纯 PG 检索），稳定后再打开二级
UNIFIED_QA_LEVEL2_ENABLED=false
# QA 容器限核（8 核机器：intel-worker 已占 5，这里给 3）
QA_WORKER_CPUS=3
```

---

## 2. 发布（在本机 `F:\CollectInfo` 执行，不要登生产机改文件）

```powershell
pwsh -File F:\CollectInfo\deploy-to-prod.ps1
```

脚本行为：打包源码 → 生产机 overlay `docker build` 出新镜像 →
同步 `docker-compose.prod.yml` → `docker compose -f docker-compose.prod.yml up -d`。
由于 compose 里已有 `qa-worker` 服务，`up -d` 会一并创建 `collectinfo-qa-worker`。

> 若用 `-SkipComposeSync` 单独跑，务必先确认生产机上的 compose 已含 `qa-worker` 段，
> 否则会变成"代码更新了但没有消费者"。

---

## 3. 发布后验证（按顺序做，任何一步不过就回滚）

```bash
# 1) 容器起来了
docker ps --format 'table {{.Names}}\t{{.Status}}' | grep -E 'qa-worker|intel-worker'
#    期望：collectinfo-qa-worker   Up ... (healthy)

# 2) qa-worker 只跑 qa.run 一条泳道
docker exec collectinfo-qa-worker sh -c "ps -eo args | grep '[i]ntel_worker.py'"
#    期望：python /app/intel_worker.py --no-periodic-scheduler --job-type qa.run

# 3) intel-worker 的 long 泳道不再含 qa.run（关键！）
docker exec collectinfo-intel-worker sh -c "ps -eo args | grep '[i]ntel_worker.py'"
#    期望：long 泳道只有 financial_research / financial_verify / paper_backtest

# 4) 提问一次，看作业是否被 qa-worker 领取
docker exec collectinfo-postgres psql -U postgres -d collectinfo -c \
 "select id,job_type,status,lease_owner from intel_jobs where job_type='qa.run' order by id desc limit 3;"
#    期望：status=running/completed，lease_owner 形如 <容器ID>:<pid>:xxxx
docker exec collectinfo-postgres psql -U postgres -d collectinfo -c \
 "select status,current_stage,degraded from qa_runs order by created_at desc limit 3;"

# 5) 页面侧：打开 http://117.50.211.93:8003/ 的 AI 助手，能出「问题分析思路」卡片、进度、
#    答案与引用；点「调整思路」后旧计划折叠、不重复输出。

# 6) 资源：docker stats --no-stream，qa-worker 不应长期打满
```

---

## 4. 灰度与观察

- 观察 24h：`qa_runs` 失败率、`intel_jobs` 里 qa.run 的排队时长、`qa_circuit_states` 里
  `provider:*` / `ragflow` 是否长期 open、`docker stats` 的 CPU。
- 稳定后把 `.env` 的 `UNIFIED_QA_LEVEL2_ENABLED` 改成 `true`（或直接改热开关：
  `PUT /api/qa/v1/feature-flags` 的 `level2_enabled`），再观察一轮。
- 想临时停用助手但保留部署：`UNIFIED_QA_ENABLED=false`（问答自动回落旧链路）。

---

## 5. 回滚

```bash
# 整版本回滚
docker tag collectinfo-web:rollback-YYYYMMDDHHMM collectinfo-web:latest
cd /www/CollectInfo_latest_new && docker compose -f docker-compose.prod.yml up -d
# 只停 QA 泳道（保留新版本其它能力）
docker compose -f docker-compose.prod.yml stop qa-worker
```

---

## 6. 常见故障对照

| 现象 | 原因 | 处理 |
|---|---|---|
| 提问后一直"正在研究"，`intel_jobs` 里 `qa.run` 长期 `queued` | 没有 `qa-worker` 容器，或它挂了 | `docker compose up -d qa-worker`；`docker logs collectinfo-qa-worker` |
| `qa.run` 被 intel-worker 领走 | 旧容器/旧进程仍带着 `--job-type qa.run`（**泳道命令行是进程启动时确定的，改代码不会自动生效**） | 重启 intel-worker 容器，再按第 3 步第 3 条核对 |
| 问答很慢或频繁降级 | QA 与批处理抢 CPU；或本地/内网模型太慢 | 调 `QA_WORKER_CPUS`；确认 intel-worker 的 `cpus` 限制仍在；必要时换更快的模型端点 |
| 二级检索总是降级 | RAGFlow 地址/KB 未配、私网未加白名单、或熔断打开 | 查 `/api/qa/v1/health`、`qa_circuit_states`、`.env` 的白名单 |
| 前端没有新卡片 | 页面缓存或前端未随镜像更新 | 强刷页面；确认镜像里 `templates/mapindex.html` 是新版 |

---

## 7. 非 Docker 部署（备用）

像 8005 那台（`/home/timebot/app`）那样直接跑进程的节点：

```bash
# intel-worker 单元跑 supervisor（只含 core/long），QA 用单独单元
install -m 644 deploy/systemd/info-aggregator-qa-worker.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now info-aggregator-qa-worker
systemctl status info-aggregator-qa-worker --no-pager
```

---

## 8. 本机预演结论（2026-10-05）

在本机按上述形态实测过：

1. 不启动 qa worker 时建 run → `intel_jobs` 里 `qa.run` 一直 `queued`（30s 无变化，lease 为空），
   证明"只发代码不起容器"会把助手卡死。
2. 用生产同款命令 `python intel_worker.py --no-periodic-scheduler --job-type qa.run` 启动后，
   同一个 queued 作业立刻被领取并跑完。
3. 还现场复现了"旧容器抢 QA 作业"：本机一个 6 小时前起的 `firecrawl-intel-worker` 容器，
   其 long 泳道命令行里仍带 `--job-type qa.run`（那是旧代码算出来的泳道），
   重启容器后 long 泳道变回 3 个财务作业，`qa.run` 只归 QA 容器。
   → 生产发布时同样要**重启** `collectinfo-intel-worker`，不能只换镜像。
