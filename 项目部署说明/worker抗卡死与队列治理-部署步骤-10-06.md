# worker 抗卡死与队列治理 · 部署步骤（10-06）

> 适用：把"子任务级看门狗 / 流式调度 / 卡死清理 / 调度巡检 / worker 心跳 / lane 熔断"这一批改动
> 发布到 A 机（117.50.211.93，VPN 内 10.88.0.3）与 B 机（122.10.99.195）。
> 关联文档：`DOCKER_DEPLOY.md`（镜像与数据卷约定）、`deploy/QA_WORKER_ROLLOUT.md`（QA 独立泳道发布清单，格式参照）。
> 相关提交：`f6d2624`（看门狗首版）、`fb08220`（流式调度 + 巡检 + 心跳 + 熔断）、`cbb16e5`/`9758784`（embedding/enrich/优先级）。

---

## 0. 先分清：哪些是"改代码"，哪些是"部署/配置"

| # | 事项 | 类型 | 落点 |
|---|---|---|---|
| 1 | 子任务级超时（看门狗）、流式调度、任务解耦 | **改代码** | `intel_worker.py`（`_pump_once` / `_settle_in_flight` / `_on_job_timeout` / `run_forever`） |
| 2 | 超时后的资源清理（浏览器会话/僵尸子进程/gc） | **改代码** | `intel_worker._cleanup_thread_resources`、`candidate_crawler_adapter._drop_thread_scrapling_session` |
| 3 | straggler 重试上限与退避 | **改代码**（复用既有 `fail_job`） | `intel_database.fail_job`（30/60/120…秒，上限 1 小时；`attempt_count >= max_attempts` 落 failed） |
| 4 | `intel_jobs.started_at` 列 | **改代码 + 自动迁移** | `intel_schema.py`（`CREATE TABLE`/`_ensure_column`，应用启动时自动补列） |
| 5 | `intel_worker_heartbeats` 表 + 心跳上报 | **改代码 + 自动建表** | `intel_schema.py` / `IntelRepository.record_worker_heartbeat` |
| 6 | 调度层主动巡检回收卡死作业 | **改代码** | `IntelRepository.reap_stuck_jobs` + `IntelWorker._maybe_reap_stuck_jobs` |
| 7 | lane 熔断（连续超时→限流冷却） | **改代码** | `IntelWorker._lane_*` / `_effective_concurrency` |
| 8 | 环境变量（超时/心跳/巡检/熔断/并发） | **部署配置** | A：`/www/CollectInfo_latest_new/.env`；B：`/home/timebot/getinfo/.env` |
| 9 | embedding 服务 `-ub` 重启（恢复长文本不截断） | **部署运维（需人工/凭据）** | `10.88.0.1:8082`（A 用）、`192.168.0.18:8082`（开发机用） |
| 10 | 备用 worker 池 | **部署方案（可选）** | 见 §6 |

> 结论：**代码部分必须走一次完整发布**（A、B 各一次）；**配置部分**（第 8 项）跟着发布改 `.env` 并重建容器；
> **第 9 项**与第 10 项属于运维动作，可独立执行、不影响本批代码上线。

---

## 1. 发布前检查（在两台生产机上各做一次，只读）

```bash
# A 机（VPN 不通时用公网 IP）
ssh root@117.50.211.93     # 公网；VPN 内为 10.88.0.3（本轮实测该地址曾整体不可达）
# B 机
ssh -p 8001 timebot@122.10.99.195

# 1) 记录当前队列与卡死作业（发布后要对比）
cd /www/CollectInfo_latest_new      # B 机为 /home/timebot/getinfo
docker exec collectinfo-web python -c "
from sqlite_database import sqlite_db as db; db._ensure_connection()
with db.lock:
    for r in db.connection.execute(\"select status, count(*) from intel_jobs group by status\").fetchall(): print(tuple(r))
    for r in db.connection.execute(\"select id, job_type, created_at, updated_at from intel_jobs where status='running'\").fetchall(): print('running:', tuple(r))
"
# 2) 记录当前并发/优先级参数
grep -nE '^INTEL_(WORKER_JOB_CONCURRENCY|JOB_PRIORITY_AGING|JOB_HARD_TIMEOUT|JOB_REAP|WORKER_HEARTBEAT|LANE_TIMEOUT)' .env
# 3) 备份 .env 与镜像（回滚要用）
cp -n .env .env.bak-$(date +%Y%m%d%H%M%S)
docker images | grep collectinfo-web | head -3   # 发布脚本会自动打 collectinfo-web:rollback
```

**预期现状（本轮实测基线，供对照）**：A 机 `classification` 排队 5300+、最老等待 30 小时；
存在运行时长超过 10 小时仍未结束的 `running` 作业；core lane 总吞吐约 53 作业/小时。

---

## 2. 发布代码（在本机 `F:\CollectInfo` 执行，**不要登生产机改源码**）

```powershell
# 0) 确认本地就是标准且已提交
git status --short          # 应为空
git log --oneline -3

# 1) A 机（VPN 地址不通时用公网地址）
pwsh -NoProfile -File F:\CollectInfo\deploy-to-prod.ps1 -ProdHost root@117.50.211.93
#   VPN 正常时：pwsh -NoProfile -File F:\CollectInfo\deploy-to-prod.ps1

# 2) B 机
python F:\CollectInfo\_b_deploy.py upload build up
```

脚本会自动：打包工作区 → 上传校验（分片 + sha256）→ 构建镜像 → 重建容器 → 健康检查 → 打印回滚命令。
**A 机的发布脚本用工作区文件**（不需要 push）；**B 机用 `git archive HEAD`**（需要本地已 commit）。

---

## 3. 配置改动（第 8 项，两台都要做）

在各自 `.env` 里补齐/确认下列项（等号后为本轮生产采用值）：

```ini
# ── 并发与调度 ──────────────────────────────────────────────
INTEL_WORKER_JOB_CONCURRENCY=8          # 批次内并发；默认 1（历史串行）。实测 4 时总吞吐 +27%、分类 20 倍
INTEL_JOB_HARD_TIMEOUT_SECONDS=1800     # 单个子任务硬超时（看门狗），默认 1800
INTEL_JOB_REAP_INTERVAL_SECONDS=120     # 调度巡检间隔，默认 120
INTEL_JOB_REAP_GRACE_SECONDS=300        # 巡检阈值 = 硬超时 + 该宽限，默认 300
INTEL_WORKER_HEARTBEAT_SECONDS=30       # worker 心跳上报间隔，默认 30
INTEL_LANE_TIMEOUT_BREAKER_THRESHOLD=3  # 连续几次子任务超时进入限流冷却，默认 3
INTEL_LANE_TIMEOUT_BREAKER_COOLDOWN_SECONDS=300  # 冷却时长，默认 300

# ── 优先级语义（本轮修复的核心）───────────────────────────
INTEL_JOB_PRIORITY_AGING_SECONDS=600    # 原来 30：任何等待超 1 分钟的作业都盖过优先级，优先级形同虚设
INTEL_JOB_PRIORITY_AGING_CAP=90         # 等待积分上限：不设上限则"等得够久"照样盖过一切
# 分类作业的优先级由代码常量给出（intel_database._CLASSIFICATION_JOB_PRIORITY=100，
# 即 enqueue_job 允许的上限；维护类最高 +5，折算后 95 < 100 → 分类排队时优先）

# ── 防饿死保留名额（10-06 二次修复，两台都要做）─────────────
INTEL_WORKER_STARVATION_DEADLINE_SECONDS=1800   # 等超过该秒数视为"饥饿"，走 FIFO 保留通道，默认 1800
INTEL_WORKER_STARVATION_RESERVED_SLOTS=2        # 饥饿轮次一次最多用几个空槽，默认 2；0=关闭
INTEL_WORKER_STARVATION_TURN_EVERY=4            # 每 4 次领活机会让 1 次走饥饿通道，默认 4
# 为什么必须要有：优先级+等待积分是**有封顶**的（CAP=90），而优先级带宽是 -45~+100，
# 所以"高优先级类型只要持续到货，低优先级类型就永远赢不了"。A 机 10-06 实测：
# 按真实排序键取出的下 20 条全是 15 小时前的 candidate_dispatch(prio=5→eff=95)；
# topic_cluster(-40→eff=50) 积压 1564 条、trend_aggregate(-45→eff=45) 积压 434 条，
# 最久 32 小时且 attempt_count=0（从未被领取）。
# 为什么是"每 N 次轮一次"而不是"每次先发保留名额"：lane 通常已满，一次领活往往只有 1 个
# 空槽，保留名额会把唯一空槽全吃掉——实测那样改完分类作业 15 分钟一个都领不到（反向饿死）。
# 轮转后维护类稳定拿到约 1/N 的吞吐，分类等要紧的活仍占 (N-1)/N。

# ── 可选：按类型单独设超时（默认继承全局硬超时）────────────
# INTEL_JOB_TIMEOUT_LIGHT_SCAN=900
# INTEL_JOB_TIMEOUT_CANDIDATE_DISPATCH=900
```

改完必须**重建读取这些变量的容器**（compose 服务名是 `web` / `worker` / `intel-worker` / `qa-worker`）：

```bash
cd /www/CollectInfo_latest_new        # B 机为 /home/timebot/getinfo
docker compose -f docker-compose.prod.yml up -d --force-recreate intel-worker worker web
sleep 10
docker exec collectinfo-intel-worker env | grep -E 'INTEL_WORKER_JOB_CONCURRENCY|INTEL_JOB_PRIORITY|INTEL_JOB_HARD|INTEL_LANE|INTEL_WORKER_STARVATION'
```

> B 机的这批变量也可以直接写进 `_b_deploy.py` 的 `LLM_ENV`，再执行 `python _b_deploy.py env` 由脚本 upsert 并重建容器（避免手改）。

---

## 4. 数据库自动迁移（无需手工 SQL，但要验一下）

**背景（本轮实测踩到）**：PostgreSQL 主库模式下 `create_tables()` 会直接跳过所有 DDL
（日志："PostgreSQL主库模式：跳过 SQLite DDL，使用已迁移 schema"），因此**代码新增的表/列不会自动落到生产库** ——
第一次发布后 A/B 上 `intel_jobs.started_at` 与 `intel_worker_heartbeats` 都不存在，心跳与巡检报 `UndefinedTable`。
本批已在 `IntelRepository._ensure()` 里加了"每进程一次"的 `ensure_intel_core_tables`（幂等），
**只要完成了 §2 的代码发布，重启容器即自动补列建表**。

若你的发布包早于该修复（或想手工确认），在两台机各跑一次一次性迁移：

```bash
docker exec -w /app collectinfo-web python -c "
from sqlite_database import sqlite_db as db
from intel_schema import ensure_intel_core_tables
db._ensure_connection()
with db.lock:
    cur = db.connection.cursor(); ensure_intel_core_tables(cur); db.connection.commit(); cur.close()
print('迁移完成')
"
```

验证（任一容器内均可）：

```bash
docker exec -w /app collectinfo-web python -c "
from sqlite_database import sqlite_db as db
db._ensure_connection()
with db.lock:
    cur = db.connection.cursor()
    cols = [r['column_name'] for r in cur.execute(\"select column_name from information_schema.columns where table_name='intel_jobs'\").fetchall()]
    tabs = [r['table_name'] for r in cur.execute(\"select table_name from information_schema.tables where table_schema='public'\").fetchall()]
print('started_at 存在:', 'started_at' in cols)
print('心跳表存在:', 'intel_worker_heartbeats' in tabs)
"
```

> 若这一步报 `DuplicateColumn`，说明用的是旧版 `intel_schema._ensure_column`（PG 上 PRAGMA 返回空集导致误判）；
> 本批已修复（取 PRAGMA 与 information_schema 并集），**先完成 §2 代码发布再验**。

---

## 5. 发布后验证（按顺序做，任一不过就按 §7 回滚）

```bash
# 1) 容器健康
docker ps --format '{{.Names}} {{.Status}}' | head -6         # web/intel-worker/qa-worker 应为 healthy

# 2) 看门狗与流式调度已生效：日志里应能看到超时/巡检/清理字样
docker logs --tail 200 collectinfo-intel-worker 2>&1 | grep -E '看门狗|lane 进入|巡检回收|清理超时作业'

# 3) worker 心跳已上报（30 秒内应有记录）
docker exec -w /app collectinfo-web python -c "
from intel_database import intel_repository as r
for b in r.list_worker_heartbeats(limit=5):
    print(b['worker_id'][:40], '| lane=', b['lane'], '| inflight=', b['inflight_count'],
          '| 连续超时=', b['timeout_streak'], '| 心跳距今=', b['since_seen_s'], 's')
"

# 4) 卡死作业被回收（发布前后对比：running 里不应再有运行 >10 小时的作业）
docker exec -w /app collectinfo-web python -c "
from sqlite_database import sqlite_db as db; db._ensure_connection()
with db.lock:
    rows = db.connection.execute(\"\"\"select id, job_type, round(EXTRACT(EPOCH FROM (now()-started_at::timestamptz))/3600.0,1) as h
                                     from intel_jobs where status='running' order by h desc nulls last limit 10\"\"\").fetchall()
for r in rows: print(tuple(r))
"

# 5) 吞吐与分类排空（观察 15 分钟）
docker exec -w /app collectinfo-web python -c "
from sqlite_database import sqlite_db as db; db._ensure_connection()
with db.lock:
    done = db.connection.execute(\"select job_type, count(*) from intel_jobs where status='completed' and completed_at::timestamptz > now() - interval '15 minutes' group by 1 order by 2 desc\").fetchall()
    cls = db.connection.execute(\"select count(*) from intel_jobs where job_type='classification' and status='queued'\").fetchone()
for r in done: print('完成:', tuple(r))
print('分类排队:', tuple(cls))
"
```

**验收标准**

| 指标 | 发布前（实测基线） | 目标 | 本轮 A 机实测（10-06 发布后） |
|---|---|---|---|
| 单条作业最长运行时长 | 19.3 小时 | < 硬超时 + 巡检宽限（默认 ≤ 35 分钟） | **0.05 小时**（3 分钟），无超长作业 |
| core lane 总吞吐 | 53.4 作业/小时 | ≥ 200 作业/小时 | **129 作业/小时**（20 分钟窗口）；积压高峰小时完成 **5206 个** |
| classification 完成 | 0.8 作业/小时 | 稳态到达可被完全消化 | **42 个/30 分钟**；积压从 **5343 → 96** |
| classification 排队深度 | 5343 且单调增长 | 不再单调增长 | **96**（已排空），随后转入维护类作业 |
| `embed_articles` 成功率 | 0%（超 512 token 全 500） | ≥ 95% | 客户端按 token 截断 + 缩小重试后不再整批失败（服务端 `-ub` 重启后可不截断） |
| worker 心跳 | 无该能力 | 30 秒内有上报 | 3 条泳道心跳，`inflight=8`、`连续超时=0` |
| 容器 | 全部 healthy | 全部 healthy，无重启循环 | web / intel-worker / qa-worker 全部 healthy |

> B 机（122.10.99.195）同样验证通过：schema 自动迁移 ✓、心跳 3 条 ✓、最长运行 0.25 小时 ✓、
> `classification` 排队 = 0（该机本就没有积压，发布后无需排空）。

---

## 6. 运维动作（与代码发布解耦，可独立执行）

### 6.1 embedding 服务重启（恢复长文本不截断）

现状：A 机用的 `http://10.88.0.1:8082` 与开发机用的 `http://192.168.0.18:8082` 都是 **llama.cpp 起的 bge-m3**，
物理批大小 512（`n_ctx=8192`、`n_embd=1024`），输入超 512 token 一律返回
`input (N tokens) is too large to process. increase the physical batch size (current batch size: 512)`。
B 机用的 `http://192.168.0.64:9997` 是 **Xinference**，实测 3000 字符输入正常，**无需处理**。

处理（在 `10.88.0.1` 或 `192.168.0.18` 上执行，需要有该机凭据——本轮实测两台都无可用 SSH 凭据）：

```bash
# 裸进程方式：找到启动命令后补上 -ub/-b（ubatch 必须 ≥ 单条输入 token 数）
ps -ef | grep -i 'llama' | grep -v grep
# 例如原来是：
#   llama-server -m /models/bge-m3.gguf --embedding --pooling cls --port 8082
# 改成：
llama-server -m /models/bge-m3.gguf --embedding --pooling cls \
             -b 2048 -ub 2048 --host 0.0.0.0 --port 8082

# 容器方式：
docker ps | grep -i llama
docker rm -f <容器名>
docker run -d --name bge-m3 --restart unless-stopped -p 8082:8080 \
  -v /models:/models ghcr.io/ggml-org/llama.cpp:server-cuda \
  -m /models/bge-m3.gguf --embedding --pooling cls -b 2048 -ub 2048 --host 0.0.0.0 --port 8080
```

重启后验证（在 A 机容器里执行）：

```bash
docker exec -w /app collectinfo-web python -c "
from embedding_client import EmbeddingClient
c = EmbeddingClient('http://10.88.0.1:8082', 'bge-m3', timeout=60)
t = '香港家族办公室税务宽免政策。' * 200          # 约 2800 字符
c.max_input_tokens = 0                            # 临时关掉客户端截断，验证服务端能吃长文本
print('长文本维度:', len(c.embed(t)))
"
```

服务端能吃长文本后，把 A 机 `.env` 的 `INTEL_EMBEDDING_MAX_INPUT_TOKENS` 调大（如 4000）或设为 0（不截断），
重建 `intel-worker`。**在此之前保持默认 480**：客户端会按 token 估算截断并按"缩小重试"策略自愈，
不会像以前那样 100% 失败重试（这是本批代码修复的效果）。

### 6.2 备用 worker 池（防雪崩的"第二池"）

当前只有一条 core lane（compose 服务 `intel-worker` 跑 `intel_worker_supervisor.py`，内含 core / long-financial 两条泳道）。
需要真正"备用池"时（例如连续超时熔断频繁触发、或要隔离重任务），按下面做法加一个**只跑指定类型**的 worker：

```bash
# 在 .env 里给备用池单独一份变量（示例：只跑分类与精炼，并发 2）
# 然后以覆盖 env 的方式再起一个 worker 容器（不影响主 lane）
docker run -d --name collectinfo-intel-worker-backup --restart unless-stopped \
  --env-file .env \
  -e INTEL_WORKER_JOB_CONCURRENCY=2 \
  -e INTEL_JOB_HARD_TIMEOUT_SECONDS=900 \
  --network <compose 网络> \
  collectinfo-web:latest \
  python intel_worker.py --no-periodic-scheduler --job-type classification --job-type enrich
```

主 lane 侧对应的做法：把熔断阈值调到 1~2，让它更快让位；备用池只跑关键类型，避免两边抢同一批作业
（`claim_jobs` 是带状态条件的原子更新，不会重复领取）。

---

## 7. 回滚

```bash
# 代码回滚（镜像级）
docker tag collectinfo-web:rollback collectinfo-web:latest
cd /www/CollectInfo_latest_new && docker compose -f docker-compose.prod.yml up -d

# 配置回滚
cp .env.bak-<时间戳> .env && docker compose -f docker-compose.prod.yml up -d --force-recreate intel-worker worker web
```

数据层**不需要回滚**：新增的 `intel_jobs.started_at` 列与 `intel_worker_heartbeats` 表对旧代码无副作用
（旧代码不读它们）；被看门狗/巡检置为 `retry_wait` 的作业会照常按新代码或旧代码重试。

**回滚判据**：出现以下任一情况立即回滚——容器重启循环、`intel-worker` 健康检查持续 unhealthy、
问答（qa-worker）受影响、或队列深度在 30 分钟内仍单调增长且日志出现大量同类型超时。

---

## 8. 日常运维速查

```bash
# 卡死作业快照
docker exec -w /app collectinfo-web python -c "
from sqlite_database import sqlite_db as db; db._ensure_connection()
with db.lock:
    for r in db.connection.execute(\"\"\"select id, job_type, lease_owner,
        round(EXTRACT(EPOCH FROM (now()-started_at::timestamptz))/60.0,1) as min
        from intel_jobs where status='running' order by min desc nulls last limit 10\"\"\").fetchall(): print(tuple(r))
"

# worker 心跳（谁在跑、跑了多久、连续超时几次）
docker exec -w /app collectinfo-web python -c "
from intel_database import intel_repository as r
import json
for b in r.list_worker_heartbeats(limit=10): print(b['worker_id'], b['status'], b['inflight_count'], b['timeout_streak'], json.loads(b['detail_json'] if 'detail_json' in b else '{}'))
"

# 被看门狗/巡检处理的作业台账
docker exec collectinfo-postgres psql -U postgres -d collectinfo -c \
  "select id,job_type,attempt_count,max_attempts,substr(last_error,1,60) from intel_jobs where last_error like '%看门狗%' or last_error like '%巡检回收%' order by id desc limit 20;"

# 领取顺序（确认分类优先）
docker exec -w /app collectinfo-web python -c "
from sqlite_database import sqlite_db as db; db._ensure_connection()
with db.lock:
    for r in db.connection.execute(\"select job_type, priority, count(*) from intel_jobs where status in ('queued','retry_wait') group by 1,2 order by 3 desc limit 8\").fetchall(): print(tuple(r))
"

# 防饿死保留名额是否生效：饥饿通道里有货 + 低优先级类型开始减少
docker exec -w /app collectinfo-web python -c "
from datetime import timedelta
from sqlite_database import sqlite_db as db
from intel_contracts import utc_now, utc_text
import config
db._ensure_connection()
cutoff = utc_text(utc_now() - timedelta(seconds=int(config.INTEL_WORKER_STARVATION_DEADLINE_SECONDS)))
with db.lock:
    row = db.connection.execute(
        \"select count(*) as n from intel_jobs where status in ('queued','retry_wait') and created_at <= ?\",
        (cutoff,)).fetchone()
    print('饥饿作业（等待超过阈值）:', dict(row)['n'])
    for r in db.connection.execute(
        \"select job_type, count(*) as n, min(created_at) as oldest from intel_jobs where status in ('queued','retry_wait') group by 1 order by 2 desc limit 8\").fetchall():
        print(tuple(r))
"
# 判定：发布后低优先级类型（topic_cluster / trend_aggregate / embed_articles / light_scan）
# 的 n 必须持续下降、oldest 必须变新；若 30 分钟后 oldest 完全不动，说明保留名额没生效。
```

**巡检建议**：上线后 24 小时内每 2 小时看一次"完成速率 / 分类排队 / 最长运行时长 / 连续超时次数"；
若 `timeout_streak` 反复触顶（≥3）说明下游（LLM 或目标站点）不稳，此时应先查下游再调
`INTEL_LANE_TIMEOUT_BREAKER_*`，不要盲目调大超时。

---

## 9. 开启行业包的 RAGFlow 增强检索（qa_retrieval_enabled）

**背景**：`needs_ragflow = needs_retrieval and ragflow_qa_enabled and mode != "fast"`，
而**所有已发布包的 `ragflow_policy.qa_retrieval_enabled` 都是 False** → 任何机器、任何问题都不走
RAGFlow（`level2_retrieval` 直接 `skipped=true`）。种子文件里 family_office 本来是 true，
但已发布版本漂移成了 false（已发布优先）。

**做法（工具已入库）**：

```bash
# A 机（无 RAGFlow 也可以执行：开关照常发布，运行期走 kb_not_configured 兜底）
docker cp tools/enable_pack_qa_retrieval.py collectinfo-web:/app/tools/
docker exec -w /app collectinfo-web python tools/enable_pack_qa_retrieval.py           # 预演
docker exec -w /app collectinfo-web python tools/enable_pack_qa_retrieval.py --apply   # 发布
# B 机同理
```

要点与实测：

| 事项 | 说明 |
|---|---|
| 只发布、不激活 | 工具走正规 `save_draft → publish_draft`，**不调用 activation**；实测发布期间新增作业 0 个重扫作业（A 机 37 个新增全是正常爬取流量） |
| 激活包必须切版本 | `published_manifest_for_loader` 对**当前激活包**返回 `active_industry_pack_version_id` 指向的版本，只发布不切版本读到的还是旧值（实测 family_office 已发布 v11=True 但读到 False）。工具会做一次轻量切换（等价 `/activate-version`），只改一个 runtime setting、不触发重扫 |
| 若确实触发了重扫作业 | 用户口径：全部删除。判定特征 = `created_by`/`dedupe_key` 命中 `pack_activation` / `industry_revalidate` / `activation` / `rescan` / `reclassify`；用 §8 的速查命令先看清单再删 |
| 知识库解析 | B 机 9 个包全部解析到同一个 News 知识库 `acb412ec4dab…`；A 机没有 RAGFlow，解析为空但仍发布开关（以后配好即生效） |
| 已知残留问题 | `bolean_security_compute` 的**种子 manifest 校验不通过**（`default_sources[95].source_type is invalid`），该包在 B 机无法发布新版本；与开关无关，需先修该包 manifest |
| 验证 | B 机真实问答：`level2_retrieval enhanced=True`、知识库证据 **0 → 14 条**（耗时 6.1s） |

**报告阶段（`level2_research`）仍会降级**，原因已定位：RAGFlow 侧
`POST /v1/unified_qa/research` 返回 `400 RESEARCH_REQUEST_INVALID: research assistant is not allowlisted`
—— 需要在 **RAGFlow 服务器（192.168.0.64）** 把本项目的 assistant_id
（`QaPolicyResolver().resolve(pack).ragflow_app_id`，当前 `7a6e0c9cc1b411…`）加入 research 白名单。
检索本身不受影响（那才是"增强检索"的主体），研究报告只是多一轮归纳。
