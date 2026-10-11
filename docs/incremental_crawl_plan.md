# 智能增量爬取四期改进计划（子任务拆解文档）

> 版本：v1（2026-09-20）
> 状态：一期已完成并验证；本文档是二期~四期 + 排序改造 + 文件拆分的完整子任务拆解。
> 宪法：**任何代码文件不超过 6000 行**，超标的必须拆成蓝图/子模块；反爬一律交 VPN OCR，**不新增任何绕开反爬的手段**，简单朴实合规。

---

## 0. 背景与最终目标

### 0.1 最终目标（按用户要求逐条对应）

| # | 要求 | 实现方式（对应任务） |
|---|---|---|
| 1 | 已知 URL 自动探查文章列表页 URL | T2.1 列表页探测器 |
| 2 | 自动对比新旧、爬过的不重复爬、只爬新的 | 一期已完成 + T3.1/T3.3 零请求信号 |
| 3 | 减少 LLM 消耗 | 水位线拦截（已做）+ T2.2 模板持久化 + T4.1 LLM 识别缓存 |
| 4 | 反爬交 VPN 服务器 OCR，避免触发反爬、不绕开 | T4.3 礼貌抓取宪法 + 降级链收口 |
| 5 | 生产机实时动态按实时间排序；保持预告时间，预告时间也参与排序 | T0.1/T0.2 |
| 6 | 代码文件 ≤6000 行 | T5.1/T5.2/T5.3 拆分任务 |
| 7 | 所有 URL 必须关注站点「动态/新闻」字样的栏目；即使没有标准文章列表页/详情页结构也要拿到 | T2.5 |
| 8 | 建立分级字数标准：简短行业动态低于 LLM 标准字数也允许进入，不因字数限制挡住真正的行业动态 | T2.6 |

### 0.2 借鉴的开源工具优势映射（已随依赖装好）

| 工具 | 优势 | 用在哪 |
|---|---|---|
| autoscraper | 少量样例学习列表项模板（标题/链接/日期三字段） | T2.1/T2.2 列表页模板学习 |
| scrapling | 自适应抓取/解析，无浏览器请求层 | T2.1 静态列表页探测 |
| patchright | 正常浏览器渲染 JS 列表页（**只当普通浏览器用，不启用隐身/指纹对抗**） | T2.1 动态列表页兜底 |
| curl_cffi | 标准 HTTP 客户端（仅用于正常请求兼容，不做 TLS 伪装绕过） | 现有 hybrid_crawler 请求层 |
| trafilatura / readability / newspaper3k | 正文 + 日期抽取（JSON-LD→meta→time 分层） | T2.3 日期分层提取 |
| feedparser / 现有 sitemap 解析 | RSS `pubDate`、sitemap `lastmod` 增量信号 | T3.1 |
| 参考方案 | feder-cr/invisible_playwright 的 high-water mark（重叠窗口+内容签名）；SparkProxy 三层信号（发现/校验/指纹） | 一期已落地；三期扩展 |

### 0.3 本地已有能力盘点（本计划的地基）

| 模块/函数 | 现状 | 本计划中的作用 |
|---|---|---|
| `crawl_waterline.py`（一期已完成） | 两表 `crawl_waterline`/`crawl_item_seen`；`classify_new_items`（新→旧遍历、连续 3 条已保存停止、乱序容错、日期短路径丢旧文、全局“爬过不再爬”）；`mark_items_seen/saved`、`record_waterline_visit`；只有成功入库才推进水位线 | 所有后续任务的共用底座 |
| `hybrid_crawler.py`（一期已接入） | Step3 后水位线过滤；无新文正常结束 `waterline_no_new`；Step4 后推进水位线；异常降级全量 | T3 信号层挂接点 |
| `supplemental_link_discovery.py` | `_parse_sitemap` 已取 `lastmod`/`publication_date`（L1213）；feed 已取 `pubDate`（L1068）；robots/sitemap 探测（L1160-1192） | T3.1 增量信号改造点 |
| `smart_site_discovery.py` / `smart_target_resolver` / `/api/schedule-management/smart-url-suggestions` | 栏目 URL 建议（基于行业锚点） | T2.1 列表页探测复用 |
| `article_link_extractor.py` | `_is_likely_article_url`（L2470）、`_extract_publish_date`（L866）、`_crawl_article_details` 日期窗口（`_date_out_of_range_reason` L1817） | T2.3 日期分层增强 |
| `articles` 表 | 已有 `publish_date`/`published_at_utc`/`published_timezone`/`published_precision`/`published_time_source` 五字段分离 | T2.3 不需要新列 |
| `/api/intel/timeline`（`intel_api.py` L4324） | 当前排序：未来日期按入库时间、其余按发布时间；已有「预告日期」标记 `date_future`（L4383-4415） | T0.1 排序改造点 |
| `remote_pipeline_client.ocr()`（L246）/ `remote_pipeline/app.py` L971 独立 OCR 管线 | 截图→OCR→LLM 总结 | T4.3 反爬降级链终点 |
| `candidate_crawler_adapter.py` L365、`intel_worker.py` L436 | 已存在 `vpn_ocr` 提取路径 | T4.3 收口统一 |
| `intel_content_quality_gate.py` | `MIN_ARTICLE_CHARS=150` 硬门槛（L8/L94，`content_too_short` 直接拒绝）；`metadata_shell`/`meeting_notice` 专项拦截 | T2.6 分级字数标准的改造点（150 是目前“LLM 处理标准线”） |
| `smart_site_discovery.py` | 栏目词库已含「新闻/动态」（L46），但目前只是相关性排序词，**没有“强制纳入”语义** | T2.5 的改造点 |
| `scheduler.py` 域名并发/限速、`hybrid_crawler` 双通道（远程 Crawl4AI / 静态降级） | 已有礼貌抓取基础 | T4.3 宪法落点 |

---

## 1. 任务总览表

| 编号 | 名称 | 期 | 依赖 | 预计新增/改动行数 |
|---|---|---|---|---|
| T0.1 | 实时动态按实时间排序（预告时间参与排序） | 独立（先行） | 无 | ~30 行改 |
| T0.2 | 预告日期显示与排序的一致性回归 | 独立 | T0.1 | 测试为主 |
| T2.1 | 已知 URL 自动探查文章列表页（列表页探测器） | 二期 | 一期 | 新模块 ~700 行 |
| T2.2 | 列表页模板学习与持久化（autoscraper） | 二期 | T2.1 | 新模块 ~400 行 |
| T2.3 | 日期分层提取 + 置信度入库 | 二期 | 无 | ~300 行改/增 |
| T2.4 | 页面类型四级分类器（列表/详情/动态/首页） | 二期 | T2.1 | ~250 行 |
| T2.5 | 「动态/新闻」栏目强制纳入 + 非标准结构兜底抽取 | 二期 | T2.1 | 新模块 ~400 行 |
| T2.6 | 分级字数标准（短行业动态准入） | 二期（可先行） | 无 | ~150 行改 |
| T3.1 | sitemap/RSS 增量信号接入水位线 | 三期 | 一期 | ~200 行改 |
| T3.2 | ETag/Last-Modified 条件请求（304 跳过） | 三期 | 一期 | ~150 行改 |
| T3.3 | 调度层“零请求跳过” | 三期 | T3.1 | ~100 行改 |
| T4.1 | LLM 页面识别兜底（低频 + 按域名缓存） | 四期 | T2.4 | ~200 行 |
| T4.2 | 信源管理水位线可视 + 手动检查 | 四期 | 一期 | ~250 行 |
| T4.3 | 反爬→VPN OCR 降级链收口 + 礼貌抓取宪法 | 四期 | 无（部分可提前） | ~300 行改 |
| T5.1 | 拆分 `sqlite_database.py`（6771 行，已超宪法） | 独立（尽早） | 无 | 平移 ~800 行 |
| T5.2 | `firecrawl_app.py` 接近 6000 行的预案 | 独立 | T5.1 | 预案为主 |
| T5.3 | `mapindex.html`（8012 行）JS 外置 | 独立（低优先级） | 无 | 平移 ~2500 行 |

---

## 2. T0：实时动态按实时间排序（含预告时间）

### T0.1 排序键统一为“实时间”，预告时间参与排序

- **目标**：`/api/intel/timeline` 的排序不再分叉。统一按
  `COALESCE(published_at_utc, publish_date, first_crawled, created_at) DESC, id DESC` 排序；
  未来日期（预告日期）不再回落到按入库时间排序，而是**按其预告时间参与排序**（自然排在最前，前端已有「预告日期」标注）。
- **边界**：
  - 只改 `intel_api.py` L4366-4376 的 `ORDER BY`（去掉 CASE）；
  - 显示逻辑（L4380-4415：时间格式、`date_future` 标记、`date_inferred`）**保持原样不动**；
  - 不改前端；不新增字段；不动分页。
- **改动文件**：`intel_api.py`（~15 行改 + 注释）。
- **测试方法**：新增 `tests/test_timeline_sort.py`（temp SQLite + `config.DATABASE_TYPE='sqlite'` 模式）：
  1. 常规文章按发布日期倒序；
  2. 发布日期缺失的回落到 first_crawled 参与排序；
  3. 未来日期文章按其未来日期排在最前，且 `date_future=true`；
  4. 两篇未来日期文章按未来日期倒序（预告时间也参与排序）。
- **验收标准**：
  - 单测 4 项全绿；
  - 本地 `/api/intel/timeline` 页面时间轴从上到下递减可读、未来日期条目带「预告日期」标记且排最前；
  - 部署说明：此项上线生产需「同步生产机」。

### T0.2 预告时间显示与排序一致性回归

- **目标**：确保改排序后「显示时间 = 排序键」仍同源，杜绝“标签显示 A、位置按 B 排”的错乱。
- **边界**：不改业务，只做回归测试与代码注释澄清。
- **测试方法**：`tests/test_timeline_sort.py` 增加断言：每个事件 `time` 字段与 SQL 排序键同源（未来日期显示为 `MM-DD`，排序按完整日期）。
- **验收标准**：回归全绿；人工抽查 5 篇含预告日期的文章时间轴。

---

## 3. 二期：结构识别（找到“哪里是新的”）

### T2.1 已知 URL 自动探查文章列表页（列表页探测器）

- **目标**：给一个已知 URL（首页/栏目页/任意文章页），自动产出该站的文章列表页 URL 清单 + 置信度，落库供调度使用。对 `https://www.chinaaeri.com/news/category/aeri/` 这类入口自动定位其新闻列表结构。
- **方案**（探测顺序，全部只读、不暴力遍历）：
  1. robots.txt 明示的 sitemap（复用 `supplemental_link_discovery._discover_sitemap_urls` L1160）；
  2. 导航链接过滤（复用 `smart_site_discovery` 栏目词库 + `content_handlers` 导航词黑名单），候选 URL 要求与已知页同域；
  3. 对候选页做**列表页判定**（借鉴 boilerplate 检测浅层特征，WSDM'10 论文思路）：重复卡片结构检测（同层 DOM 结构相似兄弟节点 ≥3）、链接密度、分页控件、`<time>`/日期节点聚集；
  4. 动态页兜底：静态探测失败或页面为 JS 渲染时，交给远程 pipeline（`remote_pipeline_client.run(mode="list")`，VPN 已有能力）——**不用本地 patchright 硬扛**；
  5. 结果写 `crawl_list_pages(site_domain, list_url, confidence, page_type, discovered_at)` 新表（新模块建表，行数预算内）。
- **边界**：只探测导航/sitemap 明示入口，**不做全站枚举**；遵守 robots.txt；不登录、不绕过；每次探测限候选数（默认 ≤30）与频率（每域名每日 ≤1 次）。
- **改动文件**：新增 `crawl_listpage.py`（≤800 行，含建表与 API）；`intel_api.py` 增加一个查询接口（复用 `/api/schedule-management/smart-url-suggestions` 的响应风格，不新建页面）。
- **测试方法**：
  - 单测：用本地 fixture HTML（模拟列表页/详情页/首页）验证判定函数；对 chinaaeri 用录制好的静态样本测探测器输出；
  - 集成：`tools/` 下新增脚本对 2 个真实站点（含 chinaaeri）跑一次探测，人工核对输出清单。
- **验收标准**：对给定入口 URL，输出 ≥1 个列表页且置信度字段存在；列表页判定对 3 类 fixture 页的准确率 100%；不产生对未明示页面的额外抓取请求。

### T2.2 列表页模板学习与持久化（autoscraper）

- **目标**：对每个列表页学习“标题/链接/日期”三字段模板并持久化，扫描时直接套模板取条目，**不再每次重学、不调 LLM**。
- **方案**：`crawl_listpage_template(domain, list_url, template_json, learned_at, hit_count, miss_count)` 新表；模板命中率下降（连续 2 次提取 0 条）→ 自动重学一次，仍失败则回退结构聚类。
- **边界**：只学三字段；不学正文模板（正文抽取继续用 trafilatura/readability）；模板按（域名+列表页 URL 模式）隔离。
- **改动文件**：`crawl_listpage.py` 扩展（+~400 行）。
- **测试方法**：单测用 fixture HTML 学一次模板→二次套用→断言三字段提取一致；模板失效重学路径 mock 验证。
- **验收标准**：同一样本学一次后，套用阶段 0 次 LLM 调用；命中率统计字段正确累加。

### T2.3 日期分层提取 + 置信度入库

- **目标**：列表页条目与详情页都能按可信度分层拿到发布时间，写入已有五字段，**不再为日期调 LLM**。
- **分层顺序**：JSON-LD `datePublished/dateModified` → meta `article:published_time`/`og:published_time`/`DC.date.issued` → 微数据 `itemprop=datePublished` → `<time datetime>` → URL 内日期 → 相对时间（“3 小时前”，按抓取时刻折算）→ 无日期则 `publish_date` 留空（排序回落 first_crawled）。
- **边界**：写已有字段 `published_at_utc/published_timezone/published_precision/published_time_source`（来源层级字符串），不新增列；不改 `smart_article_extractor` 主流程，先以函数形式提供并被 `_crawl_article_details` 与列表条目解析调用。
- **改动文件**：新增 `crawl_date_extract.py`（≤400 行）；`article_link_extractor.py` 调用点 ~20 行改。
- **测试方法**：单测覆盖 6 个层级的 fixture；相对时间折算用固定“抓取时刻”断言；冲突时（JSON-LD 与 meta 不一致）取高层级并记录来源。
- **验收标准**：6 层 fixture 全部命中正确日期；无日期条目不伪造；0 次 LLM 调用。

### T2.4 页面类型四级分类器（列表/详情/动态/首页）

- **目标**：`classify_page_type(url, html_or_dom_features) → (page_type, confidence)`，供 T2.1 判定与调度侧“值不值得扫”决策。
- **特征**：URL 模式（复用 `_is_likely_article_url`）、链接密度、重复卡片、正文块连续度、分页控件、导航占比（浅层文本特征，参考 boilerplate 检测论文）。
- **边界**：只做四级分类；LLM 兜底在本任务中**不启用**（留给 T4.1）；不下载任何页面（输入由调用方提供）。
- **改动文件**：`crawl_listpage.py` 内实现（+~250 行）。
- **测试方法**：四类 fixture 各 ≥3 份断言；对真实站点样本做离线批量校验脚本。
- **验收标准**：fixture 100% 正确；真实样本 ≥90%（人工抽样 20 个）。

### T2.5 「动态/新闻」栏目强制纳入 + 非标准结构兜底抽取

- **目标**：对每个已注册 URL 的站点，凡栏目名/导航文案命中「动态、新闻、快讯、资讯、要闻」等词的入口，**无条件纳入扫描清单**——即使页面不是标准文章列表页（没有重复卡片）、文章也没有标准详情页结构，也必须把其中的条目拿到。例：滚动快讯流、公告栏、表格型列表、JS 渲染的动态页。
- **方案**：
  1. 探测阶段硬规则：`smart_site_discovery` 的栏目词命中「动态/新闻」族时，跳过相关性排序，直接标记 `force_include=true` 进入 `crawl_list_pages`；
  2. 非标准列表页兜底抽取链（都不调 LLM）：JSON-LD/微数据 → JS 数据对象（嵌入 JSON）→ DOM 文本行聚类（标题行 + 相邻时间行成对）→ 仍拿不到 → VPN OCR（T4.3 链内）；
  3. 无标准详情页的条目：直接以列表页携带的摘要/标题/日期入库（`content_hint_source` 已有概念），正文缺失时 `content` 用条目摘要，`extraction_method='list_inline'`。
- **边界**：只对「动态/新闻」语义栏目强制；普通栏目维持相关性判定；仍遵守 robots.txt 与频控；不为此开发任何绕过手段。
- **改动文件**：`smart_site_discovery.py`（+~80 行：force_include 语义）、新增 `crawl_dynamic_column.py`（≤500 行：兜底抽取链）。
- **测试方法**：单测用四类 fixture（滚动快讯流/表格列表/JS 数据流/无结构公告栏）断言条目均被抽出；集成脚本对 1~2 个真实「动态」栏目页验证。
- **验收标准**：命中「动态/新闻」词的所有栏目都出现在扫描清单（`force_include=true`）；四类非标准 fixture 均产出条目；0 次 LLM 调用。

### T2.6 分级字数标准（简短行业动态准入）

- **目标**：把「字数」从单一硬门槛改为**三级标准**，行业动态/简短新闻即使低于 LLM 处理标准也允许进入，同时继续挡住导航页/占位页：
  - **L1 完整文章**：正文 ≥150 字 → 全流程（LLM 分类/摘要/趋势），即现有 `MIN_ARTICLE_CHARS` 作为“LLM 处理标准线”保持不变；
  - **L2 短行业动态**：30 ≤ 正文 < 150 字，且满足「有效标题 + 来源域名 + 日期（任一来源层级）+ 至少 1 个完整句子 + 命中行业词或来自 force_include 栏目」→ **允许入库**，打 `content_tier='short_dynamic'`，走轻量规则分类，跳过 LLM（或仅摘要级），正常进入「行业动态」时间轴；
  - **L3 丢弃**：<30 字、无实质句子、`metadata_shell`/`meeting_notice`/链接目录 → 维持拒绝（这些专项检查全部保留，不因分级放开）。
- **边界**：
  - 只改 `intel_content_quality_gate.assess_article_quality` 的判定：`content_too_short` 从硬拒绝改为分档（L2 返回 `tier='short_dynamic'` + `passed=True` + `skip_llm=True`）；
  - 调用方（`candidate_crawler_adapter`/`_crawl_article_details`）按 `skip_llm` 决定是否跳过 LLM 分类（省 LLM 消耗）；
  - 不放开 L3 的专项拦截；阈值常量集中在 gate 文件顶部（`MIN_ARTICLE_CHARS=150`、新增 `MIN_SHORT_DYNAMIC_CHARS=30`），便于后续调参。
- **改动文件**：`intel_content_quality_gate.py`（+~80 行改）、`candidate_crawler_adapter.py`（+~40 行）、`article_link_extractor.py` 调用点（+~30 行）。
- **测试方法**：单测覆盖 4 档边界（149/150、29/30、空内容、metadata_shell、meeting_notice、链接目录）；断言 L2 入库且 `skip_llm=true`、L3 全拒；集成测试确认短讯出现在时间轴且排序按实时间。
- **验收标准**：真实短行业动态（如 60 字快讯）不再被 `content_too_short` 拦截；导航/占位/会议壳仍全部拒绝；L2 路径 LLM 调用为 0。

---

## 4. 三期：零请求信号（没有新的，连正文都别发）

### T3.1 sitemap/RSS 增量信号接入水位线

- **目标**：sitemap 只取 `lastmod/publication_date` 新于水位线的 URL；RSS 只取 `pubDate` 新于水位线的条目；两者结果与列表页水位线共用 `crawl_waterline.last_max_publish_time` 做过滤。
- **边界**：`lastmod` 是站点自报值，只当**候选提示**不当证据（CMS 可能写构建时间）；只改 `supplemental_link_discovery._parse_sitemap` 与 feed 解析的输出过滤，不改其探测逻辑。
- **改动文件**：`supplemental_link_discovery.py`（+~150 行改）、`crawl_waterline.py`（+~50 行：`filter_by_external_signal`）。
- **测试方法**：fixture sitemap（含新旧 lastmod 混合）与 fixture RSS；水位线时间前后各设一档断言过滤结果。
- **验收标准**：旧 URL 0 条进入候选；`lastmod` 缺省条目不误杀（保留，交给详情页日期窗口）。

### T3.2 ETag/Last-Modified 条件请求（304 跳过）

- **目标**：详情页请求带 `If-None-Match/If-Modified-Since`；304 → 判定未变化，跳过正文解析与 LLM 分类；200 → 正常处理并把新校验头存水位线。
- **边界**：只对**已有水位线且上次保存过 ETag/Last-Modified** 的 URL 生效；服务器不理会条件头的（返回 200 且内容指纹相同）交给内容哈希比对（`crawl_item_seen` 指纹已有）；不新增请求库。
- **改动文件**：`hybrid_crawler.py` 请求层（+~100 行）、`crawl_waterline.py`（etag/last_modified 两列 +~50 行）。
- **测试方法**：mock 响应 304/200/无校验头三态；断言 304 路径 0 次解析、0 次 LLM。
- **验收标准**：304 路径不产生文章候选与任务；水位线两列正确读写。

### T3.3 调度层“零请求跳过”

- **目标**：每轮扫描前先查该列表页的 sitemap/RSS 快照信号：信号时间 ≤ 水位线且本轮未到 RSS 新条目 → 整站跳过（连列表页都不请求），只写 `record_waterline_visit(0)`。
- **边界**：仅当 T3.1 信号可用时生效；信号不可用的站点保持现状（一期列表页停止条件兜底）。
- **改动文件**：`scheduler.py` 扫描前检查（+~60 行）、`intel_sources.py` 周期扫描入口（+~40 行）。
- **测试方法**：mock 信号层返回“无变化”→ 断言 0 网络请求、任务状态 `skipped_no_change`；返回“有变化”→ 正常派发。
- **验收标准**：无变化站点整轮 0 请求；有变化站点行为与现状一致。

---

## 5. 四期：智能兜底、可视与反爬收口

### T4.1 LLM 页面识别兜底（低频 + 按域名缓存）

- **目标**：T2.4 规则分类低置信（<0.6）的页面才调一次本地 LLM 判定，结果按域名+URL 模式缓存 30 天，**重复遇到不重复调用**（省 LLM 消耗）。
- **边界**：只判定页面类型（四级），不做其他；每域名每日 LLM 判定 ≤5 次；失败降级“未知”，不影响爬取。
- **改动文件**：`crawl_listpage.py`（+~150 行）、`intel_llm_client` 复用现有调用路径。
- **测试方法**：单测 mock LLM 断言缓存命中后 0 次调用；限额超限路径。
- **验收标准**：同域名同模式第二次识别 0 次 LLM 调用；超限时报错可观测且不阻塞。

### T4.2 信源管理水位线可视 + 手动检查

- **目标**：信源管理（`intel_management.html` 扫描状态或新 tab）展示每个列表页水位线：上次爬取时间、已见条目数、上次新增数、水位线时间；提供「立即检查」（触发一轮列表页探测并应用水位线）。
- **边界**：只读展示 + 手动触发；不改调度策略本身。
- **改动文件**：`crawl_waterline.py` 汇总查询（+~80 行）、`intel_api.py` 接口（+~60 行）、`templates/intel_management.html`（+~120 行）。
- **测试方法**：Playwright 验收（仿 `tools/next_items_acceptance.py`）：登录→信源管理→断言水位线字段展示；点「立即检查」→ 任务派发成功提示。
- **验收标准**：字段与库内一致；按钮派发成功且不重复入队（dedupe key）。

### T4.3 反爬→VPN OCR 降级链收口 + 礼貌抓取宪法

- **目标**：把「遇到反爬怎么办」收敛成一条明确、合规的降级链，全程**不绕开反爬**：
  1. 常规解析（限速、退避、robots 尊重、条件请求）；
  2. 列表页动态渲染 → VPN `run(mode="list")`（已有）；
  3. 详情页解析失败/403/验证码 → VPN `ocr(url)`（`remote_pipeline_client.ocr` L246，截图→OCR→LLM，`extraction_method='vpn_ocr'` 落库路径已存在于 `candidate_crawler_adapter.py` L365）；
  4. 仍失败 → 记录 `last_error`，**不再重试硬攻**，等下一轮。
- **边界**：
  - 不启用/不开发新的隐身、指纹伪装、验证码破解、代理轮换绕过手段（现有 `cloudflare_bypass` 维持现状，默认不启用）；
  - 新代码中 patchright 只作普通浏览器渲染，不加载对抗类插件；
  - 频控加严：对返回 403/429 的域名自动降频（退避系数），避免“以量压反爬”。
- **改动文件**：`hybrid_crawler.py`/`candidate_crawler_adapter.py` 降级链统一函数（+~200 行）、`scheduler.py` 403/429 退避（+~60 行）。
- **测试方法**：mock 403 → 断言走 `ocr` 且 `extraction_method='vpn_ocr'`；mock OCR 失败 → 断言错误落库、任务标记可重试；退避系数单测。
- **验收标准**：整条链无任何“绕过”代码路径；403/429 站点 24h 内请求量按退避递减；VPN OCR 结果正确落库并进分类流程。

---

## 6. T5：宪法拆分（任何代码文件 ≤6000 行）

### T5.1 拆分 `sqlite_database.py`（当前 6771 行，已超）

- **目标**：降至 ≤5600 行。把聊天存储部分（`chat_history`/`chat_operations`/`_chat_scope`/`save_chat_qa` 等，约 800 行）平移为新子模块 `chat_storage.py`（类 `ChatStorageMixin` 或独立函数），`sqlite_database.py` 保留主表与公共工具。
- **边界**：**纯平移，零行为变化**；`mapindex_api.py`/`chat_api.py` 的调用点改为从 `chat_storage` 导入或经 `sqlite_db` 代理属性转发（`__getattr__` 代理最稳，先代理后逐步迁移）。
- **测试方法**：现有全部单测（含 chat 相关 10 项）+ `py_compile`；跑一遍 `tools/history_ops_acceptance.py` 与 `tools/next_items_acceptance.py`。
- **验收标准**：拆分后两文件均 <6000 行；全部既有单测/验收绿；0 功能变化。

### T5.2 `firecrawl_app.py`（5550 行，接近上限）预案

- **目标**：立规矩——所有新页面/接口一律进蓝图（参照 `pack_report_bp` 模式），不再往 `firecrawl_app.py` 加行；若后续超 5800 行，把 报告页/分享页/静态页路由抽 `pages_bp.py`。
- **验收标准**：本计划所有新代码合入后 `firecrawl_app.py` 行数净增 ≤30。

### T5.3 `mapindex.html`（8012 行）JS 外置

- **目标**：把主脚本抽到 `static/js/mapindex/*.js`（按功能：chat/report/trend/article 分文件，每个 <6000 行），模板只留 HTML 骨架与加载标签。
- **边界**：低优先级、独立排期；先做 `</script>` 大块平移 + 浏览器验收，再逐步拆；**不与其他任务并行改动同一区域**。
- **测试方法**：Playwright 全量回归（首页/聊天/趋势/报告/手机端）。
- **验收标准**：功能无回归；`mapindex.html` ≤6000 行。

---

## 7. 实施顺序与里程碑

1. **M0（立即可做）**：T0.1+T0.2 排序改造（用户已点名，改动小、影响明确）→ 本地验收 → 等用户「同步生产机」。
2. **M1（宪法先行）**：T5.1 sqlite_database 拆分（避免后续任务继续把文件撑大）。
3. **M2（二期）**：T2.6 分级字数标准（独立、收益直接）→ T2.3 日期分层 → T2.4 页面分类 → T2.1 列表页探测 → T2.2 模板学习 → T2.5 动态/新闻栏目强制纳入。
4. **M3（三期）**：T3.1 → T3.2 → T3.3。
5. **M4（四期）**：T4.3 降级链（可提前到 M2 后）→ T4.1 → T4.2。
6. **M5（收尾）**：T5.2/T5.3 + 全局回归 + 生产发布清单。

## 8. 全局验收清单（最终验收）

- [ ] 对 chinaaeri 实测：首爬建水位线 → 次日无新文 0 详情页请求（或整站跳过）→ 有新文只抓新增 → 重复运行入库数不增
- [ ] 已知 URL 自动产出列表页清单，模板复用 0 次 LLM
- [ ] 站点内所有「动态/新闻」栏目都被强制纳入扫描，非标准结构（快讯流/表格列表/公告栏）也能拿到条目
- [ ] 30~149 字短行业动态正常入库并进入时间轴，LLM 调用为 0；导航/占位/会议壳仍全拒
- [ ] 时间轴按实时间倒序、预告日期带标记且按预告时间参与排序（本地 + 生产）
- [ ] 403/验证码站点全部走 VPN OCR，无绕过手段，退避生效
- [ ] 所有代码文件 ≤6000 行
- [ ] 既有单测 + Playwright 验收全绿
