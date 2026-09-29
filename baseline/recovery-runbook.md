# 金融集成前基线恢复手册

## 基线身份

- Git 标签：`financial-tradingagents-baseline-v1`
- 范围：阶段 0 的五条金融 RSS、现有 Dashboard/问答/×÷、阶段 1 的数据库、配置、依赖、拓扑和性能验收。
- SQLite 备份不提交 Git。备份文件名、SHA-256、schema hash 和行数在 `baseline/database-backup-manifest.json`；实际文件必须保存在受控、加密且限制访问的备份位置。
- 非敏感配置状态在 `baseline/sanitized-config-manifest.json`；真实 `.env`、Cookie、API Key 和模型凭据不属于 Git 基线。

## 一键隔离演练

在仓库根目录执行：

```bash
python3 tools/verify_baseline_recovery.py \
  --revision financial-tradingagents-baseline-v1 \
  --database-manifest baseline/database-backup-manifest.json \
  --current-schema-compatibility \
  --container-smoke \
  --output baseline/recovery-acceptance.json
```

若备份已迁移到安全存储的恢复目录，额外传入 `--backup /受控恢复目录/crawler_articles.sqlite3`。工具会校验 manifest 内的 SHA-256，不接受错误或缺失的备份。

该命令只在临时目录中执行以下动作：

1. 用 `git archive` 解出标签代码，不执行 reset、clean 或覆盖式 checkout。
2. 用 SQLite online backup API 恢复基线库，比较完整性、schema、索引、所有表行数，并执行文章、历史会话和用户只读查询。
3. 使用仅含非敏感开关的最小环境启动 Flask test client，验证健康、金融 Dashboard 和历史会话读取。
4. 在基线库临时副本上应用当前 additive schema，插入一条合成的“基线后合法数据”，再由标签代码读取当前 schema；基线代码 smoke 后必须保持该行及 articles/chat_history/users 行数不变。
5. 基于标签代码无网络构建 smoke 镜像，并在 `--network=none`、只读根文件系统、临时数据库挂载下启动容器 smoke。
6. 删除临时恢复目录；不修改当前工作树、现网数据库，不重启现网服务。构建出的本地恢复镜像会保留，供故障恢复使用。

验收必须同时满足：标签解析成功、代码文件数量一致、运行时/密钥文件未进入归档、备份 checksum 一致、schema/行数一致、配置摘要安全、主机 smoke 通过、容器构建和启动通过。

## 阶段 6.6 批准目标

| 恢复路径 | RTO 目标 | RPO 目标 |
|---|---:|---|
| Provider/LLM 降级与 feature flag 回退 | 5 秒 | 0 行数据库数据丢失 |
| 损坏报告回退到上一 hash 有效版本 | 5 秒 | 0 行数据库数据丢失 |
| worker 停止、持久任务重试并由新 worker 接管 | 60 秒 | 0 个已完成任务丢失 |
| migration 中断后从完整数据库继续 additive migration | 120 秒 | 0 行受保护合法数据丢失 |
| 标签代码读取当前兼容 schema | 180 秒 | 0 行受保护合法数据丢失 |
| 经批准向隔离路径恢复数据库并完成 smoke | 300 秒 | `database-backup-manifest.json.captured_at_utc` |

这些是阶段 6.6 的自动化工程验收预算。真实生产变更仍须遵守维护窗口和审批；自动测试不能替代数据库恢复批准。

## 故障时的恢复顺序

优先做代码/开关回退，只有数据损坏时才恢复基线数据库：

1. 记录故障开始时间、当前 commit、容器 image ID、数据库路径和受影响功能；禁止在未取证前删除日志。
2. 若故障来自阶段 2 以后新增能力，先关闭对应金融、TradingAgents、模拟/回测开关，停止新增金融 job 的领取；保留普通聊天、RSS 和 Dashboard。
3. 在新的发布目录从 `financial-tradingagents-baseline-v1` 构建/部署，不在用户工作树执行破坏性 Git 命令。
4. 复用现有受控 `.env` 和密钥挂载，只按 `sanitized-config-manifest.json` 对照键名、布尔值与端点 host；不得从日志或 Git 恢复 secret。
5. 代码回退后先对当前数据库执行兼容性只读 smoke。阶段 2 以后的 schema 必须保持向后可读；若当前数据完整，禁止用旧备份覆盖它。
6. 只有 `PRAGMA integrity_check` 失败、schema/关键行不可恢复或负责人批准数据恢复时，才进入数据库恢复流程。

## 数据库灾难恢复

数据库恢复是可能丢失基线时间点之后合法数据的高风险操作，必须有明确批准和维护窗口：

1. 停止 Web、调度 worker 和 intel worker 的写入，确认没有运行中的写事务。
2. 即使当前数据库疑似损坏，也先用 SQLite online backup API 生成一份带时间戳的故障现场副本；不得直接删除。
3. 在新文件路径恢复受控备份，运行 `PRAGMA integrity_check`，并与 `database-backup-manifest.json` 比较 SHA-256、schema hash、表/索引和行数。
4. 对新文件运行本手册的隔离 smoke。通过前不得替换生产路径。
5. 在维护窗口内用明确的单文件原子切换替换数据库路径，保留原文件以便反向恢复；不要使用通配符、递归删除或覆盖整个 `data/` 目录。
6. 启动 Web，再依次启动普通 worker 和 intel worker；检查健康端点、Dashboard、历史会话、RSS source/run/candidate/article/classification 链路。
7. 记录实际 RTO、恢复点和基线之后未恢复的数据范围，交由负责人确认补录或放弃。

基线数据库的 RPO 是 `database-backup-manifest.json` 的 `captured_at_utc`。它不是持续备份方案，阶段 2 开始后仍需按运行策略创建增量/周期备份。

## 回滚判定

- 仅代码或新功能故障：标签代码＋当前完整数据库，禁止恢复旧库。
- Provider、LLM 或外网故障：关闭对应开关并使用缓存/降级，不回滚数据库。
- migration 中断但数据库完整：修复或前滚 migration；必须先做现场备份。
- 数据库物理损坏且无法修复：经批准恢复基线备份，并明确 RPO 数据损失。
- 密钥疑似泄漏：先吊销/轮换密钥，再恢复服务；基线摘要不能替代密钥轮换。

## 五类故障演练

- Provider 故障：立即把灰度回退到 RSS 或更低层，使用已核验缓存；不恢复数据库。
- LLM 故障：立即关闭 AI/研究层，保留只读快照、已核验事实和历史报告；不恢复数据库。
- schema migration 中断：先保留故障现场副本，确认事务/savepoint 未留下半迁移，再重跑幂等 additive migration；旧 reader 和基线后合法行都必须可读。
- 报告损坏：SHA-256 或长度不符时把该制品标为 `corrupt`，按版本倒序读取上一份 hash 有效制品；不删除报告元数据或覆盖 SQLite。
- worker 重启：停止新 claim，运行中任务通过 cancellation-aware context 进入 `retry_wait`；新 worker 复用同一 `intel_jobs` 完成任务，过期 owner 不得提交结果。

健康数据库即使误传“已批准”也禁止被旧备份覆盖。只有 integrity 失败且具有明确批准时，数据库恢复路径才可进入；恢复目的地先使用隔离新文件，禁止直接写现网路径。

## 恢复后检查

- `git rev-parse financial-tradingagents-baseline-v1^{commit}` 与验收报告的 commit 一致。
- 数据库 `integrity_check=ok`，schema SHA-256、表数、索引数、基线行数一致。
- 非敏感配置摘要 hash 一致，真实 secret 只来自受控位置。
- `/api/system/health`、金融 Dashboard、历史会话只读 smoke 通过。
- 五条官方金融 RSS 仍可追踪 source → scan run → candidate → article → financial classification。
- 不存在真实交易调用；后续新增功能开关均处于预期状态。

## 最终验收职责

- 产品门禁：回退后普通聊天、RSS、Dashboard 和历史只读边界符合计划，模拟始终不等于真实交易。
- 开发门禁：恢复策略、schema 兼容、制品 hash 回退、worker lease/retry 及架构静态检查通过。
- 测试门禁：Provider、LLM、migration、报告和 worker 五类注入、相关回归、全量回归与无网络镜像通过。
- 运维门禁：基线标签/备份 checksum、隔离恢复、当前 schema 兼容读取、RTO/RPO 及不触碰现网资源的证据通过。

验收 JSON 对以上四个职责只记录自动证据状态和复核范围，不伪造具名人员签名；生产发布或数据库恢复仍由部署流程收集真实审批。
