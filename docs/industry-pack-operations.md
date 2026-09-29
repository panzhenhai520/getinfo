# 行业包切换与恢复运行手册

## 适用边界

行业包切换只改变当前主行业的已发布配置、有效信源关联、任务身份和首页投影。文章、分类审计记录及结构化金融事实不会被物理清空。所有主行业固定组合共享的 `financial_markets` 附包；金融资讯仍须命中当前主行业的 `core_keywords + expanded_keywords`，非家族办公室默认不显示固定大盘和关注个股卡片。

本流程不包含 RAGFlow 金融实时行情同步，不替换 TradingAgents 的行情与研究逻辑，也不采用“一行业一数据库”的运行时热切换。

## 首次启用检查

1. 在 `/panython/admin` 依次打开当前主行业和准备切入的目标行业。
2. 检查关键词、网站、RSS、抓取频率和首页能力；先验证，再保存草稿，最后发布不可变版本。
3. 初次使用版本化切换时，应先发布当前行业作为可回滚基线；系统会自动采用其最新发布版本，无需先做一次同包激活。若当前行业没有已发布基线，切换预览会拒绝继续。
4. 家族办公室安装种子 `2.1.0` 已规范化承载当前 103 个行业来源；7 个通用金融来源只保留在共享附包中。

全新安装或从旧版本升级时，可先 dry-run，再一次性建立所有种子包的首发不可变版本。正式应用会先创建一致性数据库备份；已有自定义发布版本会保留，不会被种子覆盖：

```bash
python3 tools/bootstrap_industry_pack_versions.py --database data/crawler_articles.db
python3 tools/bootstrap_industry_pack_versions.py --database data/crawler_articles.db --apply
```

生产切换前可执行以下只读或隔离检查：

```bash
python3 tools/export_family_office_industry_pack.py --database data/crawler_articles.db
python3 tools/check_industry_pack_switch_e2e.py
python3 -m pytest -q tests/test_industry_pack_activation.py tests/test_industry_pack_switch_e2e.py
```

只有导出错误数、受保护人工来源数、缺失来源和意外来源均为 0，且回放和端到端检查均为 `passed: true` 时才继续。

## 普通行业切换

1. 管理员在 `/panython/admin` 选择已发布的目标行业包。
2. 点击“切换到此行业包”。系统先执行不写数据库的 dry-run，显示目标发布版本、来源新增/更新/关联/停用计数及计划哈希。
3. 确认后，系统在同一事务中对账信源、写入新激活身份、取消旧激活的排队/重试任务，并提交目标行业的初始扫描任务。
4. 正式写入前会通过 SQLite Online Backup API 生成独立备份，并记录 SHA-256、文件大小、schema version 和 `integrity_check` 结果。
5. 运行中的旧任务允许完成，但结果保留旧激活身份，不能进入新行业视图。旧文章保持 `active`，不会被归档或删除。

切换后应确认：

- `/batch-schedule` 显示目标行业、发布版本、激活 ID 及目标包的完整门控词。
- 新排队任务只属于目标主行业和 `financial_markets`。
- 首页普通行业内容只显示目标行业分类；金融资讯必须保存实际命中的目标关键词。
- 非家族办公室首页不返回固定大盘或关注个股卡片。

## 普通配置回滚

在 `/panython/admin` 的“激活与回滚记录”中，只能回滚当前生效且具有上一发布版本的激活记录。回滚同样先 dry-run，再确认计划哈希，并创建一条新的激活记录与新的灾难恢复备份。

普通回滚只反向应用版本化配置和来源关联，不复制、不覆盖、不重命名当前数据库文件。切回某个行业时，系统会用该行业当前发布版本的 `core_keywords + expanded_keywords` 重新核对旧聚合文章证据：仍命中的分类及金融命中记录会安全绑定到新激活并立即恢复卡片；不再命中的旧记录继续保留审计，但不会进入当前视图。晚到旧任务也不能覆盖已经恢复到新激活的分类。

旧 `/api/intel/industry-packs/backups` 仅保留为历史“文章 ID 索引”审计接口，返回 `restorable: false`。其 `/restore` 路由固定返回 HTTP 410，不能绕过版本化激活流程。

## SQLite 灾难恢复

灾难恢复是离线运维操作，不属于网页/API 的普通回滚。只有当前数据库损坏且配置回滚无法解决时才执行。

1. 从激活记录取得 `backup_path`、`backup_sha256`、`backup_size` 和 `backup_schema_version`。
2. 在任何停止服务或替换动作前，只读验证候选备份：

```bash
python3 tools/verify_industry_pack_backup.py /absolute/path/to/backup.sqlite3 \
  --sha256 EXPECTED_SHA256 --size EXPECTED_SIZE --schema-version EXPECTED_SCHEMA_VERSION
```

3. 仅当输出中所有 `checks` 和 `passed` 均为 `true` 时继续。
4. 停止 Web 服务、行业情报 worker 及所有可能写入 SQLite 的调度进程；确认没有运行中写事务。
5. 把当前数据库及可能存在的 `-wal`、`-shm` 文件移动到独立的故障保全目录，不要删除。
6. 将已验证备份复制到同目录的临时文件，设置与原数据库一致的属主和权限，再以同文件系统原子重命名为正式数据库路径。
7. 对恢复后的正式文件再次运行同一校验命令；随后启动 Web 服务和 worker，检查健康接口、当前行业/版本、任务队列和首页。
8. 保留恢复前故障文件、两次校验输出和激活记录，直到业务验收结束。

严禁在服务仍写库时直接覆盖数据库，严禁只复制主文件却遗留不匹配的 WAL/SHM，严禁通过网页接口上传或恢复任意 SQLite 文件。

## 验收与故障处置

- dry-run 与确认阶段的目标版本或计划哈希不一致：停止操作，刷新并重新预览。
- 备份完整性、哈希、大小或 schema 任一不匹配：禁止激活或恢复，保留文件用于审计。
- 切换后发现旧关键词、旧任务或旧首页内容泄漏：停止 worker，保存激活 ID 和任务 payload，执行当前激活的配置回滚。
- 目标行业内容暂时为空但任务身份正确：检查目标来源扫描和分类进度，不要恢复数据库；历史内容按设计不会跨激活直接显示。
