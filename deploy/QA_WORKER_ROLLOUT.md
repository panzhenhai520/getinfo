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

## 2.5 一次性数据迁移：`article_ragflow_documents` 政策列（**PG 部署必做**）

生产是 PostgreSQL 主库，而 `sqlite_database.create_tables()` 在 PG 模式下会直接
`跳过 SQLite DDL，使用已迁移 schema` —— 所以 `article_ragflow_documents` 的 10 个政策列
（`doc_type/issuer/doc_no/article_no/policy_title/publish_date/effective_date/source_url/
authority_level/metadata_json`）与 3 个索引**不会自动补**。缺列会让 QA 的
`level1_retrieval` 直接 `UndefinedColumn` 失败。

发布后在生产机上执行一次（幂等，已存在就跳过）：

```bash
docker exec -i collectinfo-web python - <<'PY'
import sys; sys.path.insert(0, "/app")
from sqlite_database import sqlite_db
sqlite_db._ensure_connection()
cur = sqlite_db.connection.cursor()
sqlite_db._ensure_article_ragflow_policy_columns(cur)
sqlite_db.connection.commit(); cur.close()
print("done")
PY

# 核对：应列出 19 列（9 基础 + 10 政策）与 3 个 idx_article_ragflow_* 索引
docker exec collectinfo-postgres psql -U postgres -d collectinfo -tAc \
 "select string_agg(column_name, ',' order by ordinal_position) from information_schema.columns where table_name='article_ragflow_documents'"
```

`qa_*` 那 17 张表不用管：它们是 `ensure_qa_tables()` 懒创建的（第一次问答或第一次读
feature flags 时自动建），但也可以顺手确认：

```bash
docker exec collectinfo-postgres psql -U postgres -d collectinfo -tAc \
 "select count(*) from information_schema.tables where table_name like 'qa%'"   # 期望 17
```

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

## 9. 生产首次发布实测记录（2026-10-05 已执行）

发布结果：`collectinfo-qa-worker` 随 compose 一起建起（healthy）；intel-worker 两条泳道
均不含 `qa.run`；QA 的 17 张表懒创建成功；政策列迁移执行后 19 列 + 3 索引齐全；
容器内跑 `chat_api._legacy_request_via_unified_qa` 得到 `HTTP 200 + text/event-stream`
且带 `X-QA-Run-ID`，事件序列 `status/searching/chunk/retrieval/search_done/done` 与旧前端契约一致。
灰度取值：`UNIFIED_QA_ENABLED=true`、`UNIFIED_QA_LEVEL2_ENABLED=false`（生产暂无可用 RAGFlow）。

**模型侧是本轮真正的瓶颈，与发布无关**：

| 实测 | 结果 |
|---|---|
| 生产助手用的 `gemma431b-32k`（10.88.0.1:11434） | 冷加载 `load_duration≈50s`；一次问答总时长可达 200s+ |
| `qwen3.8-27b-uncensored`（同机） | 约 2k 字上下文热态 11.5s，可用 |
| 服务器默认给 `provider=local` 的首包超时 8s | 必然先超时一次再重试，表现为降级标记 |
| `_prewarm_synthesis_provider` 打的是 `GET /v1/models` | 对 Ollama **不会**把模型加载进显存，起不到预热作用 |

因此生产 `.env` 显式放宽了本地模型超时（这几个变量只对 `provider=local` 生效）：

```ini
QA_LEVEL1_LOCAL_TIMEOUT_SECONDS=120
QA_LEVEL1_LOCAL_REPAIR_TIMEOUT_SECONDS=60
QA_SYNTHESIS_LOCAL_FIRST_TOKEN_TIMEOUT_SECONDS=120
QA_SYNTHESIS_LOCAL_TIMEOUT_SECONDS=180
QA_SYNTHESIS_LOCAL_REPAIR_TIMEOUT_SECONDS=60
```

两类待办（都在模型宿主侧，不在本仓库）：

1. 让模型常驻：给 Ollama 设长 `OLLAMA_KEEP_ALIVE`（或把预热改成一次 1 token 的
   `generate` 带 `keep_alive`），避免每个问题都付 ~50s 冷加载。
2. 若仍太慢：把助手使用的本地模型换成热态更快的那一个（改
   `/www/CollectInfo_latest_new/data/chat_config.json` 的 `models.local.model_id` 后重启 web）。

### 本地模型端点（2026-10-05 更正：Ollama 已弃用）

生产助手/QA 走的是 **GPU2 上固定运行的 llama.cpp**，不是 Ollama：

| 端点 | 是什么 | 用途 |
|---|---|---|
| `http://10.88.0.1:8081/v1` | **llama.cpp**（`/props` 可自证：`model_alias=qwen3.8-27b-uncensored`、`n_ctx=32768`、`total_slots=2`） | 助手与 QA 的对话模型 |
| `http://10.88.0.1:8082/v1` | **llama.cpp + `--embeddings`**（`--alias bge-m3`，GPU2，4 slots × 8192 ctx） | 文章向量与语义检索（`INTEL_EMBEDDING_BASE_URL`） |
| `http://10.88.0.1:11434` | Ollama 0.30.6（`/api/version` 可自证） | **待下线**：仅 RAGFlow 的知识库 embedding（`bge-m3:latest@Ollama`）还在用 |
| `http://10.88.0.1:11435` | ollama_llama_shim（把 Ollama API 转成 llama.cpp） | voice-project 的 `OLLAMA_BASE_URL` 可指向它，用于彻底摆脱 Ollama |

`10.88.0.0/24` 是走 wg0 的隧道网段（`ip route` 可见），所以这些端口都不在宿主机本地监听，排查时别用 `ss` 找。
bge-m3 的向量与 Ollama 版本**余弦相似度 0.99997~1.000000**、维度同为 1024，因此换引擎后**存量向量无需重算**。

### Ollama 下线（进行中，尚差 RAGFlow）

`ollama` 还是 systemd 自启服务，而 llama.cpp 两个实例是手工 `setsid nohup` 起的（重启会丢）。
已在 GPU 机暂存两个单元（**未安装未启用**）：`/root/llama-systemd/llama-chat.service`、`llama-embed.service`。

真正挡住下线的不是 CollectInfo，而是 **RAGFlow**：`rag_flow.tenant_llm` 里仍有 3 条指向 11434，其中
`bge-m3:latest@Ollama` 是各知识库的 `embd_id`（embedding 模型）。迁移步骤：

1. 在 RAGFlow 增加一条 **OpenAI-API-Compatible** 的 embedding 模型：`bge-m3` → `http://127.0.0.1:8082/v1`；
2. 先拿 1 个知识库把 `embd_id` 换过去，验证检索命中正常（向量几乎一致，理论无需重新解析）；
3. 两个 Ollama chat 条目（`gemma431b:latest` / `gemma431b-32k:latest`）删除或改用 8081 的 OpenAI 条目，
   同时检查 `dialog.llm_id` 是否指向它们；
4. 全部切换并观察后再 `systemctl stop ollama && systemctl disable ollama`；
   同时把 `voice-project/.env` 的 `OLLAMA_BASE_URL` 指向 `http://127.0.0.1:11435`（shim），
   `tools/gpu_status_server.py` 里硬编码的 `/api/ps` 也改成 11435。

回滚：`systemctl enable --now ollama`，并把 `INTEL_EMBEDDING_BASE_URL` 改回 `http://10.88.0.1:11434`、`INTEL_EMBEDDING_MODEL` 改回 `bge-m3:latest`。


`10.88.0.0/24` 是走 wg0 的隧道网段（`ip route` 可见），所以这两个端口都不在宿主机本地监听，排查时别用 `ss` 找。

llama.cpp 的实测表现（对比同机 Ollama）：短上下文 **3.1s**、约 2k 字上下文 **2.3s**；切过去之前用
Ollama + gemma431b-32k 时一次一级草稿要 159s、综合 172s，切到 llama.cpp 后同一问题整体
**81s**（草稿 36s、综合 19s），模型侧降级项消失。

配置位置：`data/chat_config.json` 的 `models.local.base_url` / `model_id`
（改完不用重建容器，文件是挂载进去的）。备份：`data/chat_config.json.bak-llamacpp-<ts>`。

**因此这几条旧建议作废**：不需要 `OLLAMA_KEEP_ALIVE`、不需要靠预热避免"每次冷加载"——
llama.cpp 常驻模型，慢只可能来自 prompt 体积与模型本身的推理速度。

### 本地模型自适应（local_model_router）

生产推理机显存只够常驻一个模型（gemma431b-32k / qwen3.8-27b-uncensored 二选一），
配置里写死的 `model_id` 会和"当前实际加载的"错位。`local_model_router.py` 在解析本地
provider 时先探测端点，跟随当前可用的那个：

* Ollama `GET /api/ps` → 已加载进显存的模型（判定"当前可用"的依据）
* 通用 `GET /v1/models` → 已安装列表（拿不到 /api/ps 时的兜底）

规则：配置的模型已在"已加载"里 → 用配置的；已加载里只有一个别的模型 → 跟随它；
探测失败 / 多模型 / 拿不准 → 原样用配置值。结果按 base_url 缓存 20s（跟着切换走），
单次探测超时 0.8s。可用 `LOCAL_MODEL_AUTOSELECT=0` 关闭，`LOCAL_MODEL_PROBE_TTL_SECONDS`
调缓存时长。

排障时先看这三者的关系：

```bash
curl -s http://10.88.0.1:11434/api/ps | head -c 300          # 端点当前常驻谁
docker exec collectinfo-web python -c "import json;print(json.load(open('/app/data/chat_config.json'))['models']['local'])"
docker exec collectinfo-web python -c "import sys;sys.path.insert(0,'/app');from chat_api import get_chat_model_runtime_config as f;print(f('local')['model_id'])"
```

第三行就是应用实际会用的模型——若与第一行不一致，说明探测没生效（检查容器能否访问该端点）。

未配 RAGFlow 时，`create_run` 会写入 `RAG_ENHANCEMENT_NOT_CONFIGURED` 降级项、二级整段跳过，
问答仍能完成（只用平台文章库证据）——生产当前就是这个形态；等 RAGFlow 端点与 API Key 就绪后，
把 `UNIFIED_QA_LEVEL2_ENABLED` 打开即可。

