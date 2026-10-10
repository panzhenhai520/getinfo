#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""【主题文章入库情况评估】评估引擎：评估 + 候选挖掘 + 影子自测 + 抓取探针 + 建议暂存。

流程（DECISION_LOG D-009，硬规矩）：
    评估 assess → 挖候选 mine → 影子重跑自测 shadow → 真抓一遍 crawl_probe
    → 双达标才 stage(status="verified")，否则 stage(status="rejected"，并写清原因)
    → 人工同意 → 调用方走 industry_pack_activation 发布激活 → record_after_apply 回填复测

信源类建议另有一道**独立闸门**（2026-10 真机事故后补）：必须先用 ``probe_sources`` 对信源 URL
做真实探测，拿到"这个源真的坏了"的第一手证据（reachable=False 或 verdict ∈
{URL 失效, 需登录或反爬, 空 feed}）才允许 verified；"零产出"这类机械规则**不是**停用理由。

本模块自身的硬约束（请勿放宽）：
* **纯 CPU、纯规则**：判定只调用 ``intel_classifier.classify_article``（规则链路），
  绝不调用 LLM / 向量模型 / 任何模型端点，也不碰 GPU。
* **不改生产配置**：影子自测只在 ``copy.deepcopy`` 出来的内存副本上合并候选词；
  ``config/industry_packs/*.json`` 与「已发布存储」都不落笔。发布激活必须由调用方
  显式走 ``industry_pack_activation.IndustryPackActivationService``（本模块不代劳）。
* **返回只含 JSON 可序列化类型**（dict/list/str/int/float/bool/None）。
* 达标阈值是模块常量，**不接受调用方覆盖**（防止为了好看把门槛放宽）。
"""

from __future__ import annotations

import contextlib
import copy
import json
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from industry_packs import (
    IndustryPackLoader,
    industry_anchor_keywords,
    industry_pack_loader,
    normalize_intel_text,
    unique_normalized_keywords,
)
from intel_classifier import classify_article, match_fixed_topics
from intel_contracts import DEFAULT_INDUSTRY_PACK_ID, utc_text
from intel_database import ARTICLE_TIME_SQL, IntelRepository, intel_repository
# 去样板复用仓库既有模块：intel_boilerplate.FRAME_MARKERS 是"分享/评论/热文推荐/扫码关注/
# 版权声明"这类页面框架特征词，已用于入库前判废，这里直接拿来做"挖词前去样板"（不另造一套）。
from intel_boilerplate import FRAME_MARKERS as BOILERPLATE_FRAME_MARKERS

MODULE_VERSION = "1.0.0"

# ─────────────────────────── 达标阈值（硬规矩，勿放宽） ───────────────────────────
# 影子重跑自测（正样本 = 本包被判 other 的文章；负样本 = 其它包已准入的文章）：
#   * 准入率提升 ≥ 15 个百分点：低于这个量级说明候选词对真实语料几乎没起作用，
#     放上【改进】页只会浪费人工审核（实测投资包 89.7% 落 other，十几点的提升才叫"改进"）。
#   * 误准入率上升 ≤ 2 个百分点：放宽门禁必然带进噪声，2 个点是"可接受的代价上限"，
#     超过就说明候选词判别力不够（宁可拒绝）。
#   * 改后准入率 ≥ 30%：只看增量会被"0% → 16%"这种小基数提升骗过；30% 是"这个包真的能用"
#     的最低线（否则大部分文章仍落 other，评审没有意义）。
MIN_ADMIT_RATE_GAIN_PCT = 15.0
MAX_FALSE_POSITIVE_GAIN_PCT = 2.0
MIN_ADMIT_RATE_AFTER_PCT = 30.0

# 抓文章实测（crawl_probe）阈值：
#   * 至少 3 篇成功解析的新文章才算"取得样本"；0 篇一律 rejected（抓取失败 ≠ 准入率 0）。
#   * 新文章准入率提升 ≥ 5 个百分点：样本量小，阈值比影子自测低，但只要没提升就不放行。
MIN_CRAWL_PROBE_ARTICLES = 3
MIN_CRAWL_PROBE_GAIN_PCT = 5.0
CRAWL_PROBE_MAX_SOURCES = 5
CRAWL_PROBE_PER_SOURCE = 3
CRAWL_PROBE_TIMEOUT_SECONDS = 90
# 单篇文章正文少于该字数视为"解析失败"（计入 parse_failed，绝不当作"准入率 0"）。
PROBE_MIN_CONTENT_CHARS = 80

# ─────────────────────────── 候选词挖掘口径 ───────────────────────────
DEFAULT_TOP_N = 20
DEFAULT_SAMPLE_LIMIT = 500
# 判别力过滤（候选词质量的关键）：
#   本包出现率 = 命中该词的「本包 other 文章」数 / 本包 other 文章总数
#   对照出现率 = 命中该词的「其它包已准入文章」数 / 对照文章总数
#   比值 = (本包出现率 + ε) / (对照出现率 + ε)，ε=0.01 防止除零、也防止"只出现 1 次的
#   噪声词"因分母为 0 被捧成无穷大。
#   保留条件（三条全满足）：比值 ≥ 3、对照出现率 ≤ 10%、本包命中文章数 ≥ 3。
#   含义：该词必须"显著属于本包、几乎不属于别人"，否则加进关键词表只会把别行业文章拉进来。
DISCRIMINATION_MIN_RATIO = 3.0
DISCRIMINATION_MAX_PEER_RATE = 0.10
DISCRIMINATION_MIN_DOCS = 3
RATIO_EPS = 0.01
# 词形门槛：中文词 ≥2 字、纯 ASCII 词 ≥3 字母（剔除单字、纯数字、年份、标点）。
MIN_TOKEN_LEN = 2
MIN_ASCII_TOKEN_LEN = 3
# 标题命中说明该词是文章主题（而非正文顺带提及）：排序时给标题命中加权。
TITLE_HIT_BONUS = 2.0
MAX_RATIO_FOR_SCORE = 20.0
# 模板噪声降权（**不剔除**，只降排序）：实测本机语料里同一信源的榜单/申报类文章会重复同一句
# 标题模板（例："… | 申报2026第八届金辑奖最佳技术实践应用奖"），于是"申报第八届""金辑"这类
# 模板片段能同时满足判别力与文档数门槛，却完全不是行业词。判定口径：命中该词的文档**只来自
# 1 个域名**且**≥80% 的命中文档标题里都有它**（真行业词很少在单一域名下被当标题模板反复套用）。
# 处理方式：排序分 ×0.5（rank_score），并在候选对象上打 template_suspect 标记；
# 判别力过滤与达标判定都不受影响（最终仍由影子自测 + 抓文章实测 + 人工审核把关）。
# 注意：页脚/导航这类**必须拦住**的噪声不能只降权——见下面 SITE_BOILERPLATE_* 硬剔除口径。
TEMPLATE_TITLE_RATIO = 0.8
TEMPLATE_SUSPECT_PENALTY = 0.5
# 站点级模板噪声降权（同样只降排序、不剔除）：页脚/导航/免责声明常被正文抽取带进来
# （实测本机语料出现"产业使命/产品工程/价值全球/万家"这类同一站点的页脚词组）。判定口径：
# 命中该词的文档**只来自 1 个域名**且**在其它包判 other 的本包语料里出现率 ≥50%**——
# 本包 other 恰恰是"没进本行"的文章，一个词覆盖其中一半又只来自单一站点，多半是站点模板而非行业词。
BOILERPLATE_PACK_RATE = 0.5
BOILERPLATE_SUSPECT_PENALTY = 0.5

# ── 去样板 + 词形过滤（**硬剔除**，2026-10 真机实测后补的口径）──────────────────
# 为什么必须硬剔除：A 机投资包（invest_mgmt，sample_limit=100，语料 5 个域名）挖出来的 top 候选词是
#   华尔街(33，3 个域名) 华尔街见闻(25) 见闻(25) app查看(24) 来自华尔街(24) 欢迎app(24)
# —— 全是 wallstreetcn.com 页脚「本文来自华尔街见闻，欢迎下载APP查看更多」的碎片。上面那两条
# template_suspect / boilerplate_suspect 只降权不剔除，且要求「单域名 + 本包 other 出现率 ≥50%」，
# 这批词的出现率只有 24%~33%，够不到门槛，于是照样被推荐。硬剔除靠下面三条独立判据：
#   ① 站点级复现率：某词在同一域名 ≥60% 的文档里都出现（该域名文档数 ≥8），且在**其它域名**
#      几乎不出现（出现率 ≤15%）→ 判为站点样板词。取 60% 而不是 80%：页脚/导航在整站几乎每页
#      都出现，60% 已是强信号；真正防误杀的是后半句「其它域名几乎不出现」——真行业词会跨来源复现。
#      语料只有单一域名时（没有跨站证据）门槛收紧到 80%（SITE_BOILERPLATE_SOLO_RATE）并出 warning。
#   ② 跨域名多样性：命中必须来自 ≥2 个域名。理由同①的后半句：页脚/导航只属于某一个站，而真正的
#      行业词会在多个来源反复出现。（任务给的「单一域名占命中 ≤80%」与「≥2 个域名」其实等价：
#      只有一个域名时占比必然 100%，所以取更强的「≥2 个域名」口径。语料本身只有 1 个域名时这条
#      无从判别，直接不启用——否则会把整份语料全杀掉。）
#   ③ 导航式短语：中英混排的按钮/客户端短语（app查看/欢迎app）、导航词拼成的短语
#      （点击查看/阅读原文/扫码关注）、出处声明（来自华尔街）——见 _nav_phrase_reason()。
SITE_BOILERPLATE_MIN_DOCS = 8
SITE_BOILERPLATE_RATE = 0.6
SITE_BOILERPLATE_SOLO_RATE = 0.8
SITE_BOILERPLATE_OTHER_RATE = 0.15
# 语料级「站点复现行」：同一域名下 ≥60% 的文档都出现的短行（≤200 字）视为页脚/导航行，切词前删掉
# （整行去样板；跨行/跨字段的碎片由上面①的「站点级复现率」兜住）。
SITE_TEMPLATE_LINE_RATE = 0.6
SITE_TEMPLATE_LINE_MAX_CHARS = 200
CROSS_DOMAIN_MIN_DOMAINS = 2
MAX_EXAMPLES = 3
TITLE_LIMIT = 120
# 语料截断：分类链路本身按 20 万字符截断，本模块为控制内存/带宽按 2 万字符截断
# （候选词几乎都出现在标题与前 2 万字符里；该上限同时写进返回值的 recomputable 里）。
SAMPLE_CONTENT_CHARS = 20000
PEER_SAMPLE_MIN = 200

# ─────────────────────────── 信源 verdict 阈值 ───────────────────────────
# 零产出：该信源为本包贡献 0 篇入库文章；
# 准入率低：入库 ≥2 篇且准入率 < 20%（<2 篇样本太小，不轻易判"低"）；
# 长期无新文：最近一篇文章距今 > 30 天（比默认 1440 分钟轮询周期宽 30 倍，排除偶发停更）；
# 有效：其余。
SOURCE_VERDICT_LOW_ADMIT_PCT = 20.0
SOURCE_VERDICT_MIN_ARTICLES = 2
SOURCE_VERDICT_IDLE_DAYS = 30

# 其它包对照样本量下限：判别力过滤需要足够大的"别人家语料"做分母。
NEGATIVE_SAMPLE_DEFAULT = 300

SUGGESTION_KINDS = ("keyword", "source")
# pending 只是表级默认值；写接口只允许下面四种。
SUGGESTION_STATUSES = ("pending", "verified", "rejected", "applied", "failed")
# 外部（API/UI）可设置的终态：**不允许外部把建议置为 verified**。
# verified 只能由自测判定产生（stage_suggestion 收到 verified 时会按当前阈值复算，
# 且要求 metrics 带有下面这个"自测来源标记"——只有 run_self_test_and_stage 会写它）。
EXTERNAL_STATUSES = ("rejected", "applied", "failed")
SELF_TEST_SOURCE = "run_self_test_and_stage"
# 生成新版本时写进审计的 actor（便于在行业包生命周期事件里认出"这是改进建议落地的版本"）。
PREPARE_ACTOR = "intel_pack_improvement"

# 信源类建议规则（2026-10 真机事故后改写，为什么这么定）：
#   **零产出不再是停用理由**。上一轮 A 机 invest_mgmt 就是按"零产出 → 建议停用"这条机械规则
#   产出了"停用全部 65 个零产出源"的 verified 建议，事后逐源探测证明 46 个（71%）URL 完全正常，
#   URL 只是没被调度扫到。真正该停用的只有 9 个。
#   现在：停用/替换**只认 probe_sources 的实测证据**（reachable=False 或 verdict ∈
#   {URL 失效, 需登录或反爬, 空 feed}）；零产出源只能进 keep_sources + notes
#   （"零产出，需先确认调度/映射是否正常"）。
#   长期无新文 + 探测证明不可用 → 建议替换（历史有产出但源已坏，需要人工找替代源）；
#   准入率低   → 保留观察：产出正常，落 other 是**关键词覆盖**问题，应随关键词改进解决；
#   有效       → 保留。
# 安全边界：抓文章探针（crawl_probe）失败的源只标记 unverified_sources（"无法确认该源状态"），
# 绝不进停用建议——抓不到不等于坏。
SOURCE_VERDICT_DISABLE = "零产出"
SOURCE_VERDICT_REPLACE = "长期无新文"
SOURCE_VERDICT_KEEP = "准入率低"

# ─────────────── 信源实测探针（probe_sources）口径 ───────────────
# 为什么需要它：信源类建议过去没有任何"这个源真的坏了"的第一手证据，判 verified 只看机械规则。
# 本探针是**唯一合法证据来源**（``generated_by="probe_sources"``，闸门会校验），
# 纯 HTTP + 仓库既有解析（rss_feed_contract / intel_light_scanner），不启浏览器、不调模型。
SOURCE_PROBE_TIMEOUT_SECONDS = 12      # 单源请求预算（连接 min(10,预算)、读取 min(20,预算)）
SOURCE_PROBE_MAX_SOURCES = 20          # 默认最多探测 20 个源（串行、每源最多 1 次请求）
SOURCE_PROBE_ITEM_LIMIT = 20           # 单源最多数多少条条目（数够判断"有没有内容"即可）
SOURCE_PROBE_GENERATOR = "probe_sources"   # evidence.source_probe.generated_by 必须是它

# 逐源 verdict（六选一，口径见 _probe_source_verdict）：
#   可用          200/2xx 且能解析出 ≥1 条条目；
#   空 feed       URL 是 feed（XML/JSON 等非 HTML）但解析出 0 条；
#   页面不可解析   200 且是 HTML，但静态解析出 0 条（改版/JS 渲染都可能这样，**不构成停用依据**）；
#   URL 失效      404/410、DNS 失败、超时、SSL/连接被重置、其它非 2xx（拿不到内容）；
#   需登录或反爬   401/403（不做任何绕过）；
#   未启用        is_enabled=0：不发起请求，也不是坏源证据。
PROBE_VERDICT_OK = "可用"
PROBE_VERDICT_EMPTY_FEED = "空 feed"
PROBE_VERDICT_UNPARSABLE = "页面不可解析"
PROBE_VERDICT_DEAD = "URL 失效"
PROBE_VERDICT_BLOCKED = "需登录或反爬"
PROBE_VERDICT_DISABLED = "未启用"
SOURCE_PROBE_VERDICTS = (
    PROBE_VERDICT_OK,
    PROBE_VERDICT_EMPTY_FEED,
    PROBE_VERDICT_UNPARSABLE,
    PROBE_VERDICT_DEAD,
    PROBE_VERDICT_BLOCKED,
    PROBE_VERDICT_DISABLED,
)
# 可作"停用/替换"依据的 verdict（用户给定口径，勿放宽）。
# 「页面不可解析」刻意不在里面：200 但静态解析不出条目，多半是改版/JS 渲染/列表页 URL 配错，
# 当坏源停用会误杀（A 机 65 源里 11 个属此类，人工复核后 9 个保留）。要放行只需把它加到这里。
SOURCE_PROBE_BAD_VERDICTS = (
    PROBE_VERDICT_DEAD,
    PROBE_VERDICT_BLOCKED,
    PROBE_VERDICT_EMPTY_FEED,
)

# 版本号递增（发布改进版本时用）：形如 X.Y.Z 则补丁位 +1，否则追加 ".1"。
_PACK_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

# 机构名启发式（决定候选词进 core_keywords 还是 entity_keywords）：
# 命中机构/组织后缀的词按"包实体"合并进 candidate_gate.entity_keywords（那里放机构名，
# 不进 fixed_topics）；其余按"行业概念词"合并进 core_keywords。分类不影响门禁效果
# （两者都会被 industry_anchor_keywords() 当成证明行业归属的词），只影响可读性。
_ENTITY_SUFFIX_RE = re.compile(
    r"(协会|学会|委员会|基金管理|基金|集团|公司|银行|交易所|研究院|研究所|大学|学院|"
    r"总局|监管局|管理局|监督管理|证券|保险|信托|事务所|联盟|商会|基金会|理事会|"
    r"管委会|办公室|中心|总局|部|局|厅|署)"
)

# 停用词：中文虚词/新闻套话 + 英文功能词。领域泛词（投资/市场/公司…）不在此列——
# 它们靠"判别力过滤"被对照语料自然淘汰，比硬编码停用词表更可靠。
_STOPWORDS = frozenset(
    """
    的 了 和 与 及 或 在 是 为 对 从 到 由 等 中 上 下 前 后 内 外 之 其 该 此 这 那 这些 那些 这个 那个
    我们 你们 他们 她们 它们 自己 本报 记者 报道 消息 编辑 来源 声明 版权 转载 点击 更多 阅读 原文
    详情 时间 日期 附件 下载 打印 关闭 返回 首页 上一页 下一页 网站 页面 浏览器 客户端 微信 微博
    公众号 扫码 关注 分享 评论 邮箱 电话 地址 关于我们 联系我们 免责声明 版权所有
    本文 本站 本网 详见 链接 网址 责任编辑 责编 摘要 导语 编者按
    表示 认为 指出 介绍 称 说 显示 发布 进行 实现 相关 有关 方面 情况 问题 工作 主要 重要 包括 以及
    但是 因为 所以 如果 可以 需要 已经 正在 同时 此外 另外 目前 今年 去年 明年 今日 昨日 明天 本
    日报 每日 最新 全部 更多 一 二 三 四 五 六 七 八 九 十 个 条 项 万 亿 千 百 元 年 月 日 时 分 秒
    记者 通讯 员 综合 报道 网 站 新闻 资讯 动态 观点 分析 解读 观察 独家 专题 直播 视频 图片 图集
    the and for with from that this these those are was were has have had not but you your our their its
    news report reports said says will would can could should about after before more most other some such
    just only also than then them there here when where which while who whom why how all any both each few
    www com cn net org http https htm html shtml php aspx jsp jpg jpeg png gif svg pdf amp nbsp href src
    utm div span script style json xml api
    """.split()
)

# 正文里常残留的采集痕迹：URL（含 www.）与域名/文件名片段。不止会影响可读性——
# 实测本机语料里"本文https://auto.gasgoo.com/xxx.shtml"这类残句会切出 auto/gasgoo/shtml/本文https
# 这些"本包独有"的假候选词（其它包的对照语料里当然没有），所以必须在切词前先剥掉 URL。
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
# 词形：中文/ASCII 混合的"词"，允许中间的连字符/加号（如 "Pre-IPO"、"C++"），
# **不允许点号**——点号基本只出现在域名/文件名/版本号里。
_TOKEN_SHAPE_RE = re.compile(r"^[0-9A-Za-z\u4e00-\u9fff][0-9A-Za-z\u4e00-\u9fff\-\+_%]*$")
# 年份/日期/数量类噪声（"2026年""3月""12日""5亿"）。
_NOISE_TOKEN_RE = re.compile(r"^[0-9]+(\.[0-9]+)?[年月日时分秒个条项万元亿千百%]*$")

# ── 导航式短语（词形/组合过滤，硬剔除）───────────────────────────────
# 维护位置就是下面四行常量 + _nav_phrase_reason()：新增一类导航/交互短语时，
# 能写成模式就加到 _NAV_PHRASE_PATTERNS，只是"构件词"（如新的"订阅号"）就加到 _NAV_CUE_WORDS。
_NAV_PHRASE_PATTERNS = (
    # ① 显式清单：动作按钮/入口短语（新增导航短语优先加这里）
    re.compile(
        r"^(点击查看|点击下载|查看全文|查看更多|查看详情|阅读原文|阅读全文|扫码关注|扫一扫|"
        r"关注我们|联系我们|关于我们|返回首页|欢迎下载|欢迎关注|欢迎投稿|投稿邮箱|商务合作)"
    ),
    # ② 页脚法律/服务条款声明
    re.compile(r"(免责声明|版权声明|版权所有|未经授权|转载请注明|如有侵权|用户协议|隐私政策)$"),
    # ③ 出处/转载声明前缀（"来自华尔街""来源华尔街见闻"这类是署名，不是主题词）
    re.compile(r"^(来自|来源|转自|摘自|原载|本文来自|以上内容)"),
    # ④ 站点自指后缀（"钛媒体app""某某公众号"）
    re.compile(r"(app|客户端|公众号|微博|头条号|官网)$"),
    # ⑤ 纯英文导航词
    re.compile(r"^(app|menu|login|share|download|click|more|read|wap|home)$"),
)
# 导航/交互构件词：中英混排时命中其一即判为按钮/客户端短语。
_NAV_CUE_WORDS = (
    "查看", "点击", "下载", "关注", "欢迎", "阅读", "原文", "扫码", "扫一扫", "打开", "返回",
    "分享", "收藏", "点赞", "评论", "登录", "注册", "订阅", "转发", "更多", "详情", "首页",
    "客服", "客户端", "二维码", "微信", "微博", "公众号",
)
# 纯 ASCII 的导航/客户端词（"app" 之类单字母词条被 _is_valid_token 放行，需要单独挡掉）。
_NAV_ASCII_HINTS = ("app", "wap", "menu", "login", "share", "download", "click", "more", "read")
_ASCII_LETTER_RE = re.compile(r"[a-z]")


# ─────────────────────────── 依赖注入（测试/多租户隔离用） ───────────────────────────
_repository: IntelRepository = intel_repository
_pack_loader: IndustryPackLoader = industry_pack_loader


def use_database(database) -> None:
    """把模块绑定的数据库换成指定实例（测试隔离用）。

    ``database`` 可以是 ``SQLiteDatabase`` 实例，也可以是 ``IntelRepository``。
    """
    global _repository
    _repository = database if isinstance(database, IntelRepository) else IntelRepository(database)


def use_pack_loader(loader) -> None:
    """把模块绑定的行业包加载器换成指定实例（测试隔离用；需提供 ``load(pack_id)``）。"""
    global _pack_loader
    _pack_loader = loader


# ─────────────────────────── 基础小工具 ───────────────────────────
def _lock_of(db):
    return getattr(db, "lock", None) or contextlib.nullcontext()


def _as_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pct(numerator: int, denominator: int) -> Optional[float]:
    """百分比（0~100，保留 4 位）。分母为 0 时返回 None（让 UI 渲染成 null，而不是假的 0）。"""
    if not denominator:
        return None
    return round(100.0 * float(numerator) / float(denominator), 4)


def _median(values: Sequence[float]) -> Optional[float]:
    ordered = sorted(float(item) for item in values if item is not None)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 4)
    return round((ordered[middle - 1] + ordered[middle]) / 2.0, 4)


def _truncate(text, limit: int = TITLE_LIMIT) -> str:
    value = " ".join(str(text or "").split())
    return value[:limit]


def _json_dumps(value) -> str:
    """JSON 序列化兜底：任何意外类型都降级成字符串，绝不因序列化失败丢记录。"""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=False, default=str)
    except (TypeError, ValueError):
        return json.dumps({"serialization_error": str(value)[:200]}, ensure_ascii=False)


def _json_loads(value, default):
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _parse_dt(value) -> Optional[datetime]:
    """解析 DB 里的时间文本（SQLite 存字符串、PG 可能给 datetime）。"""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raw = str(value or "").strip()
    if not raw or raw.lower() in ("none", "null"):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        for shape in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(raw[:19] if "%H" in shape else raw[:10], shape)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ─────────────────────────── 分词 ───────────────────────────
_JIEBA_STATE: Dict[str, object] = {"tried": False, "module": None}


def _jieba():
    """惰性加载 jieba（仓库既有依赖，bertopic_topic_service / qa_retrieval 都在用）。

    不可用时返回 None，由 ``_tokenize`` 退化为"中文 n-gram + ASCII 单词"，
    保证不新增依赖也能跑（只是候选词质量略差）。
    """
    if not _JIEBA_STATE["tried"]:
        _JIEBA_STATE["tried"] = True
        try:
            import jieba  # type: ignore

            try:
                jieba.setLogLevel(60)  # 关掉 "Building prefix dict" 之类的日志
            except Exception:
                pass
            _JIEBA_STATE["module"] = jieba
        except Exception:
            _JIEBA_STATE["module"] = None
    return _JIEBA_STATE["module"]


def _tokenizer_name() -> str:
    """实际生效的分词器（jieba 装了但没有 lcut/cut 时，实际走的是 n-gram 兜底）。"""
    jieba = _jieba()
    if jieba is not None and (
        getattr(jieba, "lcut", None) or getattr(jieba, "cut", None)
    ):
        return "jieba"
    return "ngram_fallback"


def _is_entity_term(term: str) -> bool:
    """机构/品牌名启发式：命中机构后缀且 ≥4 字 → 进 entity_keywords 桶。

    两桶互斥（同一个词只会落一个桶）：机构/品牌名走 entity_keywords（包实体词，不进
    fixed_topics），其余走 core_keywords；分类只影响可读性，不影响门禁效果
    （industry_anchor_keywords() 把两者都当"证明行业归属"的词）。
    """
    token = str(term or "")
    return bool(_ENTITY_SUFFIX_RE.search(token)) and len(token) >= 4


def _is_valid_token(token: str) -> bool:
    if not token or token in _STOPWORDS:
        return False
    if _NOISE_TOKEN_RE.match(token):
        return False
    if not _TOKEN_SHAPE_RE.match(token):
        return False
    cjk = sum(1 for char in token if "\u4e00" <= char <= "\u9fff")
    if cjk:
        return token not in _STOPWORDS and len(token) >= MIN_TOKEN_LEN
    return len(token) >= MIN_ASCII_TOKEN_LEN


def _tokenize(text: str) -> List[str]:
    """切词：jieba 优先；无 jieba 时用"中文 2/3-gram + ASCII 单词"兜底。

    额外产出"相邻词二元短语"（如 私募+股权 → 私募股权），
    领域短语往往不在通用词典里，靠这一步补回来。
    """
    normalized = _URL_RE.sub(" ", normalize_intel_text(text))
    if not normalized:
        return []
    jieba = _jieba()
    tokens: List[str] = []
    if jieba is not None:
        # 部分环境里的 jieba 版本没有 lcut（本机 0.34 就只有 cut），逐个探测，
        # 拿不到分词器才退化为 n-gram。
        cutter = getattr(jieba, "lcut", None) or getattr(jieba, "cut", None)
        if cutter is not None:
            try:
                tokens = [str(item).strip() for item in cutter(normalized)]
            except Exception:
                tokens = []
    if not tokens:
        tokens = []
        for run in re.findall(r"[\u4e00-\u9fff]+|[0-9A-Za-z][0-9A-Za-z\.\-\+_]*", normalized):
            if re.match(r"^[0-9A-Za-z]", run):
                tokens.append(run)
                continue
            if len(run) <= 3:
                tokens.append(run)
            for size in (2, 3):
                tokens.extend(run[index:index + size] for index in range(len(run) - size + 1))
    tokens = [item for item in tokens if _is_valid_token(item)]
    phrases = []
    for index in range(len(tokens) - 1):
        left, right = tokens[index], tokens[index + 1]
        if len(left) >= 2 and len(right) >= 2:
            merged = f"{left}{right}"
            if _is_valid_token(merged):
                phrases.append(merged)
    return tokens + phrases


# ─────────────────────────── 去样板 / 导航短语过滤（硬剔除） ───────────────────────────
def _nav_phrase_reason(term: str) -> str:
    """导航式短语判定：命中返回原因文本，否则返回空串（词形级判据，不依赖语料）。

    三类通用规则 + 一张可维护清单（_NAV_PHRASE_PATTERNS）：
      ① 中英混排且含导航构件词（app查看 / 欢迎app / 下载app / 钛媒体app）——中英混排本身也可能是
         真行业词（AI芯片 / Pre-IPO / REITs基金），所以**必须同时**含导航构件词才剔除；
      ② 纯中文且由 ≥2 个导航构件词拼成（点击查看 / 阅读原文 / 扫码关注 / 微信扫码）；
      ③ 出处前缀开头、站点自指后缀结尾、页脚法律声明（来自华尔街 / 某某公众号 / 免责声明）。
    """
    token = str(term or "").strip()
    if not token:
        return ""
    low = token.casefold()
    for pattern in _NAV_PHRASE_PATTERNS:
        if pattern.search(low):
            return f"导航式短语（命中模式 {pattern.pattern[:28]}）"
    if _ASCII_LETTER_RE.search(low):
        cue = next((item for item in _NAV_CUE_WORDS if item in token), "")
        hint = next((item for item in _NAV_ASCII_HINTS if item in low), "")
        if cue or hint:
            return f"中英混排的导航短语（含「{cue or hint}」）"
        return ""
    cues = [item for item in _NAV_CUE_WORDS if item in token]
    if len(cues) >= 2:
        return "导航式短语（由「%s」等导航构件词拼成）" % "」「".join(cues[:3])
    return ""


def _normalized_line(line: str) -> str:
    """行归一化：去掉所有空白，用于"同一行是否重复出现"的比对。"""
    return re.sub(r"\s+", "", str(line or ""))


def _doc_lines(text: str) -> List[str]:
    return [line.strip() for line in str(text or "").replace("\r", "\n").split("\n")]


def _domain_template_lines(rows: Sequence[Dict]) -> Dict[str, frozenset]:
    """语料级「站点复现行」：同一域名下 ≥SITE_TEMPLATE_LINE_RATE 的文档都出现的短行。

    页脚/导航被正文抽取带进来时通常整行一字不差地重复，按站点统计复现率即可识别。
    只统计短行（≤SITE_TEMPLATE_LINE_MAX_CHARS 字），避免把"整篇正文"当成模板行；
    域名文档数不足 SITE_BOILERPLATE_MIN_DOCS 的站点不参与判定（样本太小，复现率不可信）。

    注意：按**原始正文**分行统计，不能用 _doc_text——它内部的 normalize_intel_text 会把
    整篇文本折叠成一行，行结构就没了（页脚会粘在最后一段正文尾部）。
    """
    counters: Dict[str, Counter] = {}
    docs_per_domain: Counter = Counter()
    for row in rows or []:
        domain = str((row or {}).get("domain") or "")
        docs_per_domain[domain] += 1
        counter = counters.setdefault(domain, Counter())
        for line in _raw_doc_lines(row):
            normalized = _normalized_line(line)
            if not normalized or len(normalized) > SITE_TEMPLATE_LINE_MAX_CHARS:
                continue
            counter[normalized] += 1
    templates: Dict[str, frozenset] = {}
    for domain, counter in counters.items():
        total = docs_per_domain[domain]
        if total < SITE_BOILERPLATE_MIN_DOCS:
            continue
        lines = frozenset(
            line
            for line, count in counter.items()
            if count / float(total) >= SITE_TEMPLATE_LINE_RATE
        )
        if lines:  # 只记有模板行的域名（没识别出模板行的站点不必出现在过程数据里）
            templates[domain] = lines
    return templates


def _raw_doc_lines(row: Dict) -> List[str]:
    """原文行（标题 + 正文，按原始换行切）——用于统计站点复现行。"""
    title = str((row or {}).get("title") or "")
    content = str((row or {}).get("content") or "")[:SAMPLE_CONTENT_CHARS]
    return _doc_lines(title) + _doc_lines(content)


def _strip_boilerplate_lines(text: str, template_lines: frozenset = frozenset()) -> str:
    """切词前去样板（两级）：

      ① 整行删除：语料级站点复现行（同一域名 ≥60% 文档一字不差重复的短行）；
      ② 局部删除：命中 ``intel_boilerplate.FRAME_MARKERS`` 的框架短语——**只删短语本身**，
         保留同一行的其它文字。为什么不像该模块那样整行删：我们在统计词频，而正文抽取器常把
         页脚粘在最后一段正文尾部（真机语料就是"…踏板也要撑得住。本文来自华尔街见闻，欢迎…"），
         整行删掉会把整段正文的词频一起抹掉；删短语既挡住"下载APP/点击查看"这类模板词，
         又不动正文。特征词表本身直接复用仓库既有模块，不另造一套。
    """
    kept: List[str] = []
    for line in _doc_lines(text):
        if not line:
            continue
        if _normalized_line(line) in template_lines:
            continue
        cleaned = line
        for marker in BOILERPLATE_FRAME_MARKERS:
            if marker in cleaned:
                cleaned = cleaned.replace(marker, " ")
        cleaned = " ".join(cleaned.split())
        if cleaned:
            kept.append(cleaned)
    return "\n".join(kept)


def _mine_doc_text(row: Dict, template_lines: frozenset = frozenset()) -> str:
    """与 ``_doc_text`` 同口径，但正文先做行级去样板再拼判定文本。

    必须先按**原始正文**分行删样板行、再 normalize：反过来（先 _doc_text 再按行删）不行——
    normalize_intel_text 会把整篇折叠成一行，页脚就粘在正文尾部删不掉了。
    """
    content = str((row or {}).get("content") or "")[:SAMPLE_CONTENT_CHARS]
    return _doc_text(row, content=_strip_boilerplate_lines(content, template_lines))


def _site_boilerplate_reason(
    term: str,
    term_domains: Dict[str, int],
    domain_docs: Dict[str, int],
    total_docs: int,
) -> str:
    """站点级复现率判据：命中返回原因文本，否则空串（硬剔除用）。

    ``term_domains`` = 该词在各域名的命中文档数；``domain_docs`` = 各域名的文档总数。
    判定：存在某个域名 D（文档数 ≥ SITE_BOILERPLATE_MIN_DOCS），该词在 D 的复现率
    ≥ SITE_BOILERPLATE_RATE（≥60%），且在**其它域名**的复现率 ≤ SITE_BOILERPLATE_OTHER_RATE
    （≤15%）→ 站点样板词。语料只有单一域名时没有跨站证据，门槛收紧到 SITE_BOILERPLATE_SOLO_RATE。
    """
    in_term_domains = term_domains or {}
    for domain, docs_in_domain in (domain_docs or {}).items():
        docs_in_domain = _as_int(docs_in_domain)
        if docs_in_domain < SITE_BOILERPLATE_MIN_DOCS:
            continue
        hits = _as_int(in_term_domains.get(domain))
        if hits <= 0:
            continue
        rate = hits / float(docs_in_domain)
        other_docs = max(0, _as_int(total_docs) - docs_in_domain)
        if other_docs <= 0:
            if rate >= SITE_BOILERPLATE_SOLO_RATE:
                return (
                    f"站点样板词：语料只有单一域名 {domain}，该词在 {round(rate * 100)}% 的文档里复现"
                )
            continue
        if rate < SITE_BOILERPLATE_RATE:
            continue
        other_hits = sum(
            _as_int(value) for key, value in in_term_domains.items() if key != domain
        )
        other_rate = other_hits / float(other_docs)
        if other_rate > SITE_BOILERPLATE_OTHER_RATE:
            continue
        return (
            f"站点样板词：域名 {domain} 下 {round(rate * 100)}% 的文档都出现，"
            f"其它域名仅 {round(other_rate * 100, 1)}%"
        )
    return ""


# ─────────────────────────── 命中口径 ───────────────────────────
try:  # 复用分类器的命中口径（ASCII 词走单词边界、中文走子串），保证统计与判定链路一致
    from intel_classifier import _keyword_occurrences as _classifier_occurrences
except Exception:  # pragma: no cover - 分类器改名时的兜底
    def _classifier_occurrences(text: str, keyword: str) -> int:
        return str(text or "").count(str(keyword or ""))


def _keyword_hits(text: str, keyword: str) -> int:
    return _classifier_occurrences(text or "", keyword or "")


def _keyword_present(text: str, keyword: str) -> bool:
    return _keyword_hits(text, keyword) > 0


def _doc_text(row: Dict, content: Optional[str] = None) -> str:
    """把一篇文章拼成与 classify_article 一致的判定文本（标题×2 + 采集关键词 + 正文）。

    ``content`` 可传入"已去样板的正文"（挖词链路的 ``_mine_doc_text`` 就是这么用的）；
    不传则取原始正文。
    """
    title = str((row or {}).get("title") or "")
    matched = str((row or {}).get("matched_keywords") or "")
    if content is None:
        content = str((row or {}).get("content") or "")[:SAMPLE_CONTENT_CHARS]
    return normalize_intel_text(f"{title}\n{title}\n{matched}\n{content}")


def _article_from_row(row: Dict) -> Dict:
    """DB 行 → classify_article 需要的文章 dict（字段名与生产 get_article 对齐）。"""
    article_id = _as_int((row or {}).get("article_id"))
    return {
        "id": article_id,
        "article_id": article_id,
        "title": str((row or {}).get("title") or ""),
        "content": str((row or {}).get("content") or ""),
        "matched_keywords": (row or {}).get("matched_keywords") or "",
        "url": str((row or {}).get("url") or ""),
        "domain": str((row or {}).get("domain") or ""),
        "publish_date": (row or {}).get("publish_date"),
        "first_crawled": (row or {}).get("first_crawled"),
        "created_at": (row or {}).get("created_at"),
    }


def _is_admitted(result: Dict) -> bool:
    """「准入」口径：规则链路给出真实分类（trend/event）。

    为什么不用 ``result["admitted"]``：``classify_article`` 只在"通用行业过滤器前置拦截"
    分支把 admitted 置 False，其它分支恒为 True——它表示"这条结论可用"，不表示"文章入库"。
    而「主题文章入库情况评估」关心的正是"落没落 other"，所以用 final_category 判定。
    """
    return str((result or {}).get("final_category") or "") in ("trend", "event")


# ─────────────────────────── 数据库访问 ───────────────────────────
def _ensure_schema() -> None:
    """幂等建表：老仓库走 IntelRepository._ensure()（内含 ensure_intel_core_tables），
    它失败时再单独跑一次本模块的建表，避免"主库迁移没跑到"导致整个功能不可用。"""
    db = _repository.db
    try:
        db._ensure_connection()
    except Exception:
        pass
    try:
        _repository._ensure()
    except Exception as exc:  # pragma: no cover - 取决于部署环境
        print(f"⚠️ intel 仓储 schema 检查失败（继续尝试单表建表）: {str(exc)[:200]}")
    try:
        from intel_schema import ensure_intel_pack_improvement_tables

        with _lock_of(db):
            cursor = db.connection.cursor()
            try:
                ensure_intel_pack_improvement_tables(cursor)
                db.connection.commit()
            finally:
                cursor.close()
    except Exception as exc:  # pragma: no cover
        print(f"⚠️ intel_pack_improvements 建表失败: {str(exc)[:200]}")


def _query(sql: str, params: Sequence = ()) -> List[Dict]:
    db = _repository.db
    try:
        db._ensure_connection()
    except Exception:
        pass
    with _lock_of(db):
        cursor = db.connection.cursor()
        try:
            cursor.execute(sql, tuple(params))
            return [dict(row) for row in cursor.fetchall()]
        finally:
            cursor.close()


def _execute(sql: str, params: Sequence = ()) -> int:
    db = _repository.db
    try:
        db._ensure_connection()
    except Exception:
        pass
    with _lock_of(db):
        cursor = db.connection.cursor()
        try:
            cursor.execute(sql, tuple(params))
            try:
                db.connection.commit()
            except Exception:
                pass
            return _as_int(getattr(cursor, "lastrowid", 0))
        finally:
            cursor.close()


def _load_pack(pack_id: str) -> Dict:
    value = str(pack_id or "").strip() or DEFAULT_INDUSTRY_PACK_ID
    pack = _pack_loader.load(value)
    if not isinstance(pack, dict) or not pack.get("id"):
        raise ValueError(f"industry pack not found: {value}")
    return pack


def _effective_pack_ids(pack_id: str) -> List[str]:
    """本包 + 依赖包（信源/语料归属口径与扫描链路一致）。"""
    try:
        return [pack["id"] for pack in _pack_loader.effective_pack_set(pack_id)]
    except Exception:
        return [str(pack_id)]


_ARTICLE_COLUMNS = """
    a.id AS article_id, a.url, a.domain, a.title, a.publish_date, a.first_crawled, a.created_at,
    a.matched_keywords, substr(COALESCE(a.content, ''), 1, {cap}) AS content
""".strip()


def _classification_select(cap: int = SAMPLE_CONTENT_CHARS) -> str:
    return f"""
        SELECT c.article_id AS article_id, c.industry_pack_id, c.final_category, c.rule_category,
               c.rule_reason, c.final_reason, c.score_details_json, c.matched_keywords_json,
               {_ARTICLE_COLUMNS.format(cap=int(cap))}
        FROM article_intel_classifications c
        JOIN articles a ON a.id = c.article_id
    """.strip()


def _load_pack_articles(
    pack_id: str,
    *,
    category: str = "",
    limit: int = DEFAULT_SAMPLE_LIMIT,
) -> List[Dict]:
    """按"最近优先"取本包的已分类文章（可只取 other）。"""
    conditions = ["a.status = 'active'", "c.industry_pack_id = ?"]
    params: List = [pack_id]
    if category:
        conditions.append("c.final_category = ?")
        params.append(category)
    params.append(max(1, _as_int(limit, DEFAULT_SAMPLE_LIMIT)))
    sql = f"""
        {_classification_select()}
        WHERE {' AND '.join(conditions)}
        ORDER BY datetime({ARTICLE_TIME_SQL}) DESC, c.article_id DESC
        LIMIT ?
    """
    return _query(sql, params)


def _load_peer_admitted_articles(pack_id: str, *, limit: int) -> List[Dict]:
    sql = f"""
        {_classification_select()}
        WHERE a.status = 'active'
          AND c.industry_pack_id <> ?
          AND c.final_category IN ('trend', 'event')
        ORDER BY c.article_id DESC
        LIMIT ?
    """
    return _query(sql, [str(pack_id), max(1, _as_int(limit, NEGATIVE_SAMPLE_DEFAULT))])


def _pack_category_counts() -> Dict[str, Dict[str, int]]:
    sql = """
        SELECT c.industry_pack_id AS pack_id,
               COUNT(*) AS total,
               SUM(CASE WHEN c.final_category = 'trend' THEN 1 ELSE 0 END) AS trend_count,
               SUM(CASE WHEN c.final_category = 'event' THEN 1 ELSE 0 END) AS event_count,
               SUM(CASE WHEN c.final_category = 'other' THEN 1 ELSE 0 END) AS other_count
        FROM article_intel_classifications c
        JOIN articles a ON a.id = c.article_id
        WHERE a.status = 'active'
        GROUP BY c.industry_pack_id
    """
    result: Dict[str, Dict[str, int]] = {}
    for row in _query(sql):
        result[str(row["pack_id"])] = {
            "trend": _as_int(row.get("trend_count")),
            "event": _as_int(row.get("event_count")),
            "other": _as_int(row.get("other_count")),
            "total": _as_int(row.get("total")),
        }
    return result


def _source_verdict(article_count: int, admitted_count: int, last_article_at) -> str:
    if article_count <= 0:
        return "零产出"
    if article_count >= SOURCE_VERDICT_MIN_ARTICLES and (
        (admitted_count / float(article_count)) * 100.0 < SOURCE_VERDICT_LOW_ADMIT_PCT
    ):
        return "准入率低"
    reference = _parse_dt(last_article_at)
    if reference is not None and reference < datetime.now(timezone.utc) - timedelta(
        days=SOURCE_VERDICT_IDLE_DAYS
    ):
        return "长期无新文"
    return "有效"


def _source_stats(pack_id: str, *, limit: int = 0) -> List[Dict]:
    """本包（含依赖包）已启用信源的产出统计。

    文章↔信源的关联沿用仓库既有口径：articles ← intel_candidates.article_id，
    信源 ← intel_candidate_observations.source_id（与 list_classified_articles 一致）。
    """
    pack_ids = _effective_pack_ids(pack_id)
    placeholders = ",".join("?" for _ in pack_ids)
    limit_sql = ""
    params: List = [str(pack_id), *pack_ids]
    if limit:
        limit_sql = "LIMIT ?"
        params.append(max(1, _as_int(limit)))
    sql = f"""
        SELECT s.id AS source_id, s.source_name, s.source_url, s.source_type,
               s.authority_level, s.metadata_json,
               COUNT(DISTINCT a.id) AS article_count,
               COUNT(DISTINCT CASE WHEN c.final_category IN ('trend', 'event') THEN a.id END)
                   AS admitted_count,
               MAX(CASE WHEN a.id IS NOT NULL THEN datetime({ARTICLE_TIME_SQL}) END)
                   AS last_article_at
        FROM intel_sources s
        JOIN intel_source_industries si ON si.source_id = s.id AND si.is_active = 1
        LEFT JOIN intel_candidate_observations o ON o.source_id = s.id
        LEFT JOIN intel_candidates ic ON ic.id = o.candidate_id
        LEFT JOIN articles a ON a.id = ic.article_id AND a.status = 'active'
        LEFT JOIN article_intel_classifications c
               ON c.article_id = a.id AND c.industry_pack_id = ?
        WHERE s.is_enabled = 1 AND si.industry_pack_id IN ({placeholders})
        GROUP BY s.id, s.source_name, s.source_url, s.source_type, s.authority_level,
                 s.metadata_json
        ORDER BY article_count DESC, s.id
        {limit_sql}
    """
    rows = []
    for row in _query(sql, params):
        article_count = _as_int(row.get("article_count"))
        admitted_count = _as_int(row.get("admitted_count"))
        rows.append(
            {
                "source_id": _as_int(row.get("source_id")),
                "source_name": str(row.get("source_name") or ""),
                "source_url": str(row.get("source_url") or ""),
                "source_type": str(row.get("source_type") or "website"),
                "authority_level": _as_int(row.get("authority_level")),
                "metadata": _json_loads(row.get("metadata_json"), {}),
                "article_count": article_count,
                "admitted_count": admitted_count,
                "admitted_pct": _pct(admitted_count, article_count) or 0.0,
                "last_article_at": str(row.get("last_article_at") or ""),
                "verdict": _source_verdict(
                    article_count, admitted_count, row.get("last_article_at")
                ),
            }
        )
    return rows


# ─────────────────────────── 失败原因归类 ───────────────────────────
# 口径：只看 rule_reason / final_reason + score_details（都是 classify_article 的原生产出），
# 不引入任何新判定。顺序即优先级：先判"被前置过滤器拦下"，再判负向词/锚点/分数/信号。
GATE_FAILURE_LABELS = {
    "industry_filter": "未命中行业核心词/包实体（通用行业过滤器前置拦截）",
    "no_anchor": "未命中行业包锚点词（anchor/core/expanded 全未命中）",
    "negative_keyword": "命中负向词，相关性被扣至阈值以下",
    "below_min_score": "相关性分数低于 minimum_relevance_score",
    "no_signal": "命中锚点但未命中趋势/事件信号",
    "unknown": "其它/无判定依据",
}
# 哪些失败原因"靠补关键词候选就能解决"（供 UI 提示；不参与达标判定）。
GATE_FAILURE_FIXABLE = {
    "industry_filter": True,
    "no_anchor": True,
    "negative_keyword": True,
    "below_min_score": True,
    "no_signal": False,
    "unknown": False,
}


def _gate_failure_key(row: Dict) -> str:
    reason = str(row.get("rule_reason") or row.get("final_reason") or "")
    details = _json_loads(row.get("score_details_json"), {})
    hits = details.get("hits") if isinstance(details.get("hits"), dict) else {}
    anchors = hits.get("anchor") or []
    negative = hits.get("negative") or []
    score = _as_float(details.get("relevance_score"), 0.0) or 0.0
    minimum = _as_float(details.get("minimum_relevance_score"), 0.0) or 0.0
    signal = str(details.get("rule_signal") or "")
    if "通用行业过滤器" in reason or details.get("admitted") is False:
        return "industry_filter"
    if negative:
        return "negative_keyword"
    if not anchors:
        return "no_anchor"
    if score < minimum:
        return "below_min_score"
    if signal in ("no_signal", "temporal_fallback", ""):
        return "no_signal"
    return "unknown"


def _gate_failure_buckets(rows: Sequence[Dict]) -> Dict:
    counts: Dict[str, int] = {key: 0 for key in GATE_FAILURE_LABELS}
    examples: Dict[str, List[Dict]] = {key: [] for key in GATE_FAILURE_LABELS}
    for row in rows:
        key = _gate_failure_key(row)
        counts[key] = counts.get(key, 0) + 1
        if len(examples.get(key, [])) < MAX_EXAMPLES:
            examples.setdefault(key, []).append(
                {
                    "article_id": _as_int(row.get("article_id")),
                    "title": _truncate(row.get("title")),
                    "reason": _truncate(row.get("rule_reason") or row.get("final_reason"), 160),
                }
            )
    others = len(rows)
    buckets = []
    for key, label in GATE_FAILURE_LABELS.items():
        buckets.append(
            {
                "key": key,
                "label": label,
                "count": counts.get(key, 0),
                "pct": _pct(counts.get(key, 0), others),
                "fixable_by_keywords": GATE_FAILURE_FIXABLE.get(key, False),
                "examples": examples.get(key, []),
            }
        )
    buckets.sort(key=lambda item: (-item["count"], item["key"]))
    return {
        "total": others,
        "counts": {key: counts.get(key, 0) for key in GATE_FAILURE_LABELS},
        "buckets": buckets,
    }


# ─────────────────────────── 候选词（合并进内存副本） ───────────────────────────
def _normalize_candidate_input(candidates) -> Dict[str, List[str]]:
    """把各种入参形态归一成 ``{"core_keywords": [...], "entity_keywords": [...], "anchors": [...]}``。

    接受：None / 字符串列表 / 候选对象列表 ``{"term":...}`` / assess·mine 的返回 dict
    （带 ``candidates`` 子对象或不带）/ 只给 ``{"keywords": [...]}``（视为锚点+核心词都加）。
    """
    buckets = {"core_keywords": [], "entity_keywords": [], "anchors": []}

    def _collect(values) -> List[str]:
        terms = []
        if isinstance(values, (str, dict)):
            values = [values]
        for item in values or []:
            if isinstance(item, dict):
                term = item.get("term") or item.get("keyword") or item.get("value")
            else:
                term = item
            term = str(term or "").strip()
            if term:
                terms.append(term)
        return terms

    source = candidates
    if isinstance(source, dict):
        if isinstance(source.get("candidates"), dict):
            source = source["candidates"]
        if isinstance(source, dict):
            for key in buckets:
                buckets[key] = _collect(source.get(key))
            if not any(buckets.values()) and source.get("keywords"):
                shared = _collect(source.get("keywords"))
                buckets["core_keywords"] = list(shared)
                buckets["anchors"] = list(shared)
    else:
        buckets["core_keywords"] = _collect(source)
        buckets["anchors"] = _collect(source)
    return buckets


def _has_candidates(buckets: Dict[str, List[str]]) -> bool:
    return any(buckets.get(key) for key in ("core_keywords", "entity_keywords", "anchors"))


def _merge_candidates_into_pack(pack: Dict, buckets: Dict[str, List[str]]) -> Dict:
    """在**内存副本**上合并候选词（原配置一字不动）。

    合并规则（与 ``industry_anchor_keywords`` 的真实取值逻辑对齐，否则"改前/改后"会一模一样）：
      * ``candidate_gate.anchor_keywords`` 非空 → 门禁只认 anchors + entity_keywords，
        因此候选锚点必须合并进 ``anchor_keywords``（合并进 core 对门禁毫无作用）；
      * 该字段为空/不存在 → 门禁回退 ``core_keywords + expanded_keywords``，
        候选锚点改并进 ``core_keywords``；
      * ``entity_keywords`` 一律合并进 ``candidate_gate.entity_keywords``。
    全部走 ``unique_normalized_keywords``（按归一化去重、保留原有词与原顺序）。
    """
    merged = copy.deepcopy(pack)
    core_new = list(buckets.get("core_keywords") or [])
    entity_new = list(buckets.get("entity_keywords") or [])
    anchor_new = list(buckets.get("anchors") or [])
    gate = merged.get("candidate_gate")
    gate = dict(gate) if isinstance(gate, dict) else {}
    if gate.get("anchor_keywords"):
        gate["anchor_keywords"] = unique_normalized_keywords(
            list(gate.get("anchor_keywords") or []) + anchor_new
        )
    else:
        core_new = core_new + anchor_new
    if entity_new:
        gate["entity_keywords"] = unique_normalized_keywords(
            list(gate.get("entity_keywords") or []) + entity_new
        )
    if gate:
        merged["candidate_gate"] = gate
    merged["core_keywords"] = unique_normalized_keywords(
        list(merged.get("core_keywords") or []) + core_new
    )
    return merged


def _existing_terms(pack: Dict) -> set:
    """包内已有词表（归一化），用于剔除"不是新候选"的词。"""
    values: List[str] = []
    for field in (
        "core_keywords",
        "expanded_keywords",
        "trend_keywords",
        "event_keywords",
        "negative_keywords",
        "brands",
    ):
        values.extend(str(item) for item in (pack.get(field) or []))
    gate = pack.get("candidate_gate") if isinstance(pack.get("candidate_gate"), dict) else {}
    values.extend(str(item) for item in (gate.get("anchor_keywords") or []))
    values.extend(str(item) for item in (gate.get("entity_keywords") or []))
    for topic in pack.get("fixed_topics") or []:
        values.extend(str(item) for item in (topic.get("keywords") or []))
    return {normalize_intel_text(item) for item in values if str(item or "").strip()}


def _candidate_object(term: str, stat: Dict, *, entity: bool) -> Dict:
    return {
        "term": term,
        "hits": int(stat.get("hits") or 0),
        "examples": list(stat.get("examples") or [])[:MAX_EXAMPLES],
        "score": float(stat.get("score") or 0.0),
        "rank_score": float(stat.get("rank_score") or stat.get("score") or 0.0),
        "pack_rate": float(stat.get("pack_rate") or 0.0),
        "peer_rate": float(stat.get("peer_rate") or 0.0),
        "ratio": float(stat.get("ratio") or 0.0),
        "title_hits": int(stat.get("title_hits") or 0),
        "domains": int(stat.get("domains") or 0),
        "template_suspect": bool(stat.get("template_suspect")),
        "boilerplate_suspect": bool(stat.get("boilerplate_suspect")),
        "bucket": "entity_keywords" if entity else "core_keywords",
    }


# ─────────────────────────── 交付物 1.2：候选词挖掘 ───────────────────────────
def mine_keyword_candidates(
    pack_id: str,
    *,
    top_n: int = DEFAULT_TOP_N,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
    _pack_docs: Optional[List[Dict]] = None,
    _peer_docs: Optional[List[Dict]] = None,
) -> Dict:
    """从**本包被判 other 的文章**里挖候选词（这些是"本该属于本包却没进来"的语料）。

    判别力过滤（质量关键，口径见文件头常量注释）：
        比值 = (本包出现率 + ε) / (其它包已准入语料出现率 + ε) ≥ 3
        且 对照出现率 ≤ 10%、且本包命中文档数 ≥ 3。

    挖词前先**去样板**（见 _strip_boilerplate_lines）：删掉语料级站点复现行与
    intel_boilerplate.FRAME_MARKERS 命中的框架行，再统计词频；统计后再按
    「站点级复现率」「跨域名多样性」「导航式短语」三条硬剔除规则过筛。

    ``_pack_docs`` / ``_peer_docs`` 是内部复用参数（assess_pack 已经查过一次语料，
    避免重复拉库）；外部调用不用传。
    """
    pack = _load_pack(pack_id)
    limit = max(1, _as_int(sample_limit, DEFAULT_SAMPLE_LIMIT))
    top_n = max(1, _as_int(top_n, DEFAULT_TOP_N))
    warnings: List[str] = []

    pack_docs = _pack_docs
    if pack_docs is None:
        pack_docs = _load_pack_articles(pack_id, category="other", limit=limit)
    peer_docs = _peer_docs
    if peer_docs is None:
        peer_docs = _load_peer_admitted_articles(
            pack_id, limit=max(PEER_SAMPLE_MIN, limit)
        )
    if not peer_docs:
        warnings.append(
            "库内没有其它包的已准入文章作为对照语料，判别力过滤已退化（只看词频与文档数）"
        )

    # 去样板（切词前）：站点复现行只在**本包语料**里统计（本包的站点模板才是我们要挡的），
    # 但两份语料都按它清洗，避免"同一句页脚在本包被删、在对照语料里还算命中"的不对称统计。
    template_lines = _domain_template_lines(pack_docs)
    template_line_count = sum(len(lines) for lines in template_lines.values())

    def _index(rows: Sequence[Dict]) -> Tuple[List[set], List[set], List[str]]:
        body_sets: List[set] = []
        title_sets: List[set] = []
        titles: List[str] = []
        for row in rows:
            clean = _mine_doc_text(
                row, template_lines.get(str(row.get("domain") or "")) or frozenset()
            )
            body_sets.append(set(_tokenize(clean)))
            title_sets.append(set(_tokenize(str(row.get("title") or ""))))
            titles.append(_truncate(row.get("title")))
        return body_sets, title_sets, titles

    pack_token_sets, pack_title_sets, pack_titles = _index(pack_docs)
    peer_token_sets, _peer_title_sets, _peer_titles = _index(peer_docs)

    stats: Dict[str, Dict] = {}
    for index, tokens in enumerate(pack_token_sets):
        title_tokens = pack_title_sets[index]
        for token in tokens:
            item = stats.setdefault(token, {"hits": 0, "title_hits": 0, "docs": []})
            item["hits"] += 1
            item["docs"].append(index)
            if token in title_tokens:
                item["title_hits"] += 1

    peer_doc_freq = Counter()
    for tokens in peer_token_sets:
        for token in tokens:
            peer_doc_freq[token] += 1

    existing = _existing_terms(pack)
    selected: List[Tuple[str, Dict]] = []
    excluded: List[Dict] = []
    pack_total = len(pack_docs)
    peer_total = len(peer_docs)
    # 域名分布：判断"站点样板词"与"跨域名多样性"要用。空域名（抓取没带 domain）也算一个桶，
    # 但整个语料只有 1 个域名时跨域名判据无从判别，按"不启用"处理（否则会把语料全杀掉）。
    domain_docs: Dict[str, int] = {}
    for row in pack_docs:
        key = str((row or {}).get("domain") or "")
        domain_docs[key] = domain_docs.get(key, 0) + 1
    cross_domain_enabled = len(domain_docs) >= CROSS_DOMAIN_MIN_DOMAINS
    if not cross_domain_enabled and pack_total >= SITE_BOILERPLATE_MIN_DOCS:
        warnings.append(
            f"本包 other 语料只有 {len(domain_docs)} 个域名，跨域名多样性判据未启用；"
            f"站点样板判定已收紧到 {SITE_BOILERPLATE_SOLO_RATE:.0%}"
        )
    if template_line_count:
        warnings.append(
            f"去样板：在 {len(template_lines)} 个域名下识别出 {template_line_count} 条"
            f"「≥{SITE_TEMPLATE_LINE_RATE:.0%} 文档复现」的模板行，已在统计词频前删除"
        )
    nav_phrase_dropped = site_boilerplate_dropped = cross_domain_dropped = 0

    for token, item in stats.items():
        if not _is_valid_token(token):
            continue
        if token in existing:
            excluded.append({"term": token, "hits": item["hits"], "reason": "已存在于该包现有词表"})
            continue
        if item["hits"] < DISCRIMINATION_MIN_DOCS:
            excluded.append(
                {
                    "term": token,
                    "hits": item["hits"],
                    "reason": f"本包 other 语料命中文档数 < {DISCRIMINATION_MIN_DOCS}",
                }
            )
            continue
        # ① 导航式短语（词形级）：app查看 / 欢迎app / 点击查看 / 阅读原文 / 来自华尔街 …
        nav_reason = _nav_phrase_reason(token)
        if nav_reason:
            nav_phrase_dropped += 1
            excluded.append({"term": token, "hits": item["hits"], "reason": nav_reason})
            continue
        hit_domain_counts: Dict[str, int] = {}
        for index in item["docs"]:
            key = str(pack_docs[index].get("domain") or "")
            hit_domain_counts[key] = hit_domain_counts.get(key, 0) + 1
        hit_domains = set(hit_domain_counts)
        # ② 站点级复现率：同一域名下 ≥60% 文档复现 + 其它域名几乎不出现 → 站点样板词，直接剔除
        site_reason = _site_boilerplate_reason(token, hit_domain_counts, domain_docs, pack_total)
        if site_reason:
            site_boilerplate_dropped += 1
            excluded.append({"term": token, "hits": item["hits"], "reason": site_reason})
            continue
        # ③ 跨域名多样性：命中只来自单一域名 → 是某个站的页脚/导航，不是行业词，直接剔除
        if cross_domain_enabled and len(hit_domains) < CROSS_DOMAIN_MIN_DOMAINS:
            cross_domain_dropped += 1
            excluded.append(
                {
                    "term": token,
                    "hits": item["hits"],
                    "reason": (
                        f"跨域名多样性不足：命中全部来自单一域名 "
                        f"{sorted(hit_domains)[0]}（真行业词会跨来源复现）"
                    ),
                }
            )
            continue
        pack_rate = item["hits"] / float(pack_total) if pack_total else 0.0
        peer_hits = peer_doc_freq.get(token, 0)
        peer_rate = peer_hits / float(peer_total) if peer_total else 0.0
        ratio = (pack_rate + RATIO_EPS) / (peer_rate + RATIO_EPS)
        if peer_rate > DISCRIMINATION_MAX_PEER_RATE:
            excluded.append(
                {
                    "term": token,
                    "hits": item["hits"],
                    "reason": (
                        f"对照出现率 {round(peer_rate * 100, 2)}% > "
                        f"{DISCRIMINATION_MAX_PEER_RATE * 100:.0f}%（在其它包语料里也常见）"
                    ),
                }
            )
            continue
        if ratio < DISCRIMINATION_MIN_RATIO:
            excluded.append(
                {
                    "term": token,
                    "hits": item["hits"],
                    "reason": (
                        f"判别力不足：比值 {round(ratio, 2)} < {DISCRIMINATION_MIN_RATIO}"
                    ),
                }
            )
            continue
        title_docs = [
            index for index in item["docs"] if token in pack_title_sets[index]
        ]
        examples: List[str] = [pack_titles[index] for index in title_docs[:MAX_EXAMPLES]]
        for index in item["docs"]:
            if len(examples) >= MAX_EXAMPLES:
                break
            if pack_titles[index] not in examples:
                examples.append(pack_titles[index])
        score = round(
            (item["hits"] + TITLE_HIT_BONUS * item["title_hits"])
            * min(ratio, MAX_RATIO_FOR_SCORE),
            4,
        )
        title_ratio = item["title_hits"] / float(item["hits"]) if item["hits"] else 0.0
        template_suspect = len(hit_domains) <= 1 and title_ratio >= TEMPLATE_TITLE_RATIO
        boilerplate_suspect = len(hit_domains) <= 1 and pack_rate >= BOILERPLATE_PACK_RATE
        rank_factor = 1.0
        if template_suspect:
            rank_factor *= TEMPLATE_SUSPECT_PENALTY
        if boilerplate_suspect:
            rank_factor *= BOILERPLATE_SUSPECT_PENALTY
        selected.append(
            (
                token,
                {
                    "hits": item["hits"],
                    "title_hits": item["title_hits"],
                    "pack_rate": round(pack_rate, 6),
                    "peer_rate": round(peer_rate, 6),
                    "peer_hits": peer_hits,
                    "ratio": round(ratio, 4),
                    "score": score,
                    "rank_score": round(score * rank_factor, 4),
                    "domains": len(hit_domains),
                    "template_suspect": template_suspect,
                    "boilerplate_suspect": boilerplate_suspect,
                    "examples": examples[:MAX_EXAMPLES],
                    "title_docs": len(title_docs),
                },
            )
        )

    # 排序按 rank_score（= score × 模板/站点模板噪声降权），避免"金辑/申报第八届/产业使命"
    # 这类模板片段把真正的行业词挤出 top_n。
    selected.sort(key=lambda item: (-item[1]["rank_score"], -item[1]["hits"], item[0]))
    selected = selected[:top_n]
    excluded.sort(key=lambda item: (-item["hits"], item["term"]))
    suspect_count = sum(1 for _token, stat in selected if stat["template_suspect"])
    boilerplate_count = sum(1 for _token, stat in selected if stat["boilerplate_suspect"])
    if suspect_count:
        warnings.append(
            f"{suspect_count} 个候选词命中「单域名 + 标题模板」特征（疑似榜单/申报类模板词），"
            f"已按 {TEMPLATE_SUSPECT_PENALTY} 系数降权并标记 template_suspect，请人工复核后再采纳"
        )
    if boilerplate_count:
        warnings.append(
            f"{boilerplate_count} 个候选词命中「单域名 + 本包 other 出现率 ≥{BOILERPLATE_PACK_RATE:.0%}」"
            f"特征（疑似站点页脚/导航模板词），已按 {BOILERPLATE_SUSPECT_PENALTY} 系数降权并标记"
            "boilerplate_suspect，请人工复核后再采纳"
        )
    if site_boilerplate_dropped:
        warnings.append(
            f"已剔除 {site_boilerplate_dropped} 个站点样板词（同一域名 ≥{SITE_BOILERPLATE_RATE:.0%} "
            f"文档复现、其它域名 ≤{SITE_BOILERPLATE_OTHER_RATE:.0%}）"
        )
    if cross_domain_dropped:
        warnings.append(
            f"已剔除 {cross_domain_dropped} 个「只来自单一域名」的词"
            f"（跨域名多样性要求 ≥{CROSS_DOMAIN_MIN_DOMAINS} 个域名）"
        )
    if nav_phrase_dropped:
        warnings.append(
            f"已剔除 {nav_phrase_dropped} 个导航式短语（查看/下载/关注/扫码/出处声明 …）"
        )

    core_objects: List[Dict] = []
    entity_objects: List[Dict] = []
    anchor_objects: List[Dict] = []
    for token, stat in selected:
        # 同一个词只进一个桶：命中机构/组织后缀的走 entity_keywords（包实体词），其余走
        # core_keywords。两桶**互斥**——同一词同在两处会让合并进包后在 candidate_gate 与
        # core_keywords 各写一份、人工复核也看到重复项；取舍是"机构/品牌名一律归实体桶"。
        entity = _is_entity_term(token)
        obj = _candidate_object(token, stat, entity=entity)
        anchor_objects.append(obj)
        (entity_objects if entity else core_objects).append(obj)

    return {
        "pack_id": str(pack_id),
        "generated_at": utc_text(),
        "top_n": top_n,
        "sample_limit": limit,
        "pack_other_docs": pack_total,
        "peer_admitted_docs": peer_total,
        "discrimination": {
            "min_ratio": DISCRIMINATION_MIN_RATIO,
            "max_peer_rate": DISCRIMINATION_MAX_PEER_RATE,
            "min_pack_docs": DISCRIMINATION_MIN_DOCS,
            "epsilon": RATIO_EPS,
            "peer_sampled": bool(peer_docs),
            "template_title_ratio": TEMPLATE_TITLE_RATIO,
            "template_penalty": TEMPLATE_SUSPECT_PENALTY,
            "boilerplate_pack_rate": BOILERPLATE_PACK_RATE,
            "boilerplate_penalty": BOILERPLATE_SUSPECT_PENALTY,
            # 硬剔除口径（去样板 / 跨域名 / 导航短语），供 UI 展示与复算
            "site_boilerplate_min_docs": SITE_BOILERPLATE_MIN_DOCS,
            "site_boilerplate_rate": SITE_BOILERPLATE_RATE,
            "site_boilerplate_other_rate": SITE_BOILERPLATE_OTHER_RATE,
            "site_boilerplate_solo_rate": SITE_BOILERPLATE_SOLO_RATE,
            "cross_domain_min_domains": CROSS_DOMAIN_MIN_DOMAINS,
            "cross_domain_enabled": cross_domain_enabled,
        },
        "template_suspect_count": suspect_count,
        "boilerplate_suspect_count": boilerplate_count,
        # 去样板 / 硬剔除的过程数据（UI 可展示"为什么这批词没被推荐"）
        "keyword_filter": {
            "site_boilerplate_dropped": site_boilerplate_dropped,
            "cross_domain_dropped": cross_domain_dropped,
            "nav_phrase_dropped": nav_phrase_dropped,
            "template_line_domains": sorted(template_lines),
            "template_line_count": template_line_count,
            "domain_doc_counts": dict(
                sorted(domain_docs.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
            ),
        },
        # 面向 UI 的候选对象（term/hits/examples）；anchors 是"可当门禁锚点"的全集
        # （core ∪ entity，按分值排序），合并时按 _merge_candidates_into_pack 的规则落位。
        "candidates": {
            "core_keywords": core_objects,
            "entity_keywords": entity_objects,
            "anchors": anchor_objects,
        },
        "keywords": [item[0] for item in selected],
        "excluded": excluded[: max(20, top_n * 5)],
        "excluded_count": len(excluded),
        "warnings": warnings,
        "recomputable": {
            "pack_other_article_ids": [_as_int(row.get("article_id")) for row in pack_docs],
            "peer_article_ids": [_as_int(row.get("article_id")) for row in peer_docs],
            "content_chars_cap": SAMPLE_CONTENT_CHARS,
            "tokenizer": _tokenizer_name(),
        },
    }


# ─────────────────────────── 交付物 1.1：评估 ───────────────────────────
def assess_pack(pack_id: str, *, sample_limit: int = DEFAULT_SAMPLE_LIMIT) -> Dict:
    """评估一个行业包的主题文章入库情况（纯读，不改任何配置）。

    返回关键字段：
        ``articles``        本包 active 已分类文章数（全量口径，不受 sample 影响）
        ``categories``      trend/event/other 全量计数
        ``other_pct``       other 占比（%，无文章时为 0.0）
        ``peer_other_pct``  同库其它包 other 占比的中位数（无对照包时为 None）
        ``gate_failures``   样本内 other 文章的失败原因归类
        ``sources``         每个信源的产出与 verdict
        ``candidates``      从真实语料挖出的候选词（含命中数与示例标题）
        ``sample``          本次采样的口径（供复算）
    """
    pack = _load_pack(pack_id)
    limit = max(1, _as_int(sample_limit, DEFAULT_SAMPLE_LIMIT))
    counts = _pack_category_counts()
    mine = counts.get(str(pack_id)) or {"trend": 0, "event": 0, "other": 0, "total": 0}
    total = int(mine["total"])
    other = int(mine["other"])
    other_pct = round(100.0 * other / total, 4) if total else 0.0

    peer_values = []
    for pid, item in counts.items():
        if pid == str(pack_id):
            continue
        if int(item["total"]) <= 0:
            continue
        peer_values.append(100.0 * int(item["other"]) / int(item["total"]))

    sample_rows = _load_pack_articles(pack_id, limit=limit)
    other_rows = [row for row in sample_rows if str(row.get("final_category")) == "other"]
    gate_failures = _gate_failure_buckets(other_rows)

    peer_docs = _load_peer_admitted_articles(pack_id, limit=max(PEER_SAMPLE_MIN, limit))
    mining = mine_keyword_candidates(
        pack_id,
        top_n=DEFAULT_TOP_N,
        sample_limit=limit,
        _pack_docs=other_rows,
        _peer_docs=peer_docs,
    )

    sources = [
        {
            "source_id": row["source_id"],
            "source_name": row["source_name"],
            "source_url": row["source_url"],
            "article_count": row["article_count"],
            "admitted_count": row["admitted_count"],
            "admitted_pct": row["admitted_pct"],
            "last_article_at": row["last_article_at"],
            "verdict": row["verdict"],
        }
        for row in _source_stats(pack_id)
    ]
    verdict_counts = Counter(row["verdict"] for row in sources)

    return {
        "pack_id": str(pack_id),
        "pack_version": str(pack.get("pack_version") or ""),
        "pack_name": str(pack.get("name") or ""),
        "assessed_at": utc_text(),
        "articles": total,
        "categories": {"trend": int(mine["trend"]), "event": int(mine["event"]), "other": other},
        "other_pct": other_pct,
        "peer_other_pct": _median(peer_values),
        "peer_pack_count": len(peer_values),
        "peer_other_pct_detail": {
            pid: round(100.0 * int(item["other"]) / int(item["total"]), 4)
            for pid, item in counts.items()
            if pid != str(pack_id) and int(item["total"]) > 0
        },
        "gate_failures": gate_failures,
        "sources": sources,
        "source_verdict_counts": dict(verdict_counts),
        "candidates": mining["candidates"],
        "candidate_keywords": mining["keywords"],
        "candidate_evidence": {
            "discrimination": mining["discrimination"],
            "keyword_filter": mining.get("keyword_filter") or {},
            "excluded": mining["excluded"][:20],
            "excluded_count": mining["excluded_count"],
            "template_suspect_count": mining.get("template_suspect_count", 0),
            "boilerplate_suspect_count": mining.get("boilerplate_suspect_count", 0),
            "warnings": mining["warnings"],
        },
        "sample": {
            "sample_limit": limit,
            "sampled_articles": len(sample_rows),
            "sampled_other_articles": len(other_rows),
            "other_article_ids": [_as_int(row.get("article_id")) for row in other_rows],
            "peer_article_ids": [_as_int(row.get("article_id")) for row in peer_docs],
            "content_chars_cap": SAMPLE_CONTENT_CHARS,
        },
        "thresholds": {
            "min_admit_rate_gain_pct": MIN_ADMIT_RATE_GAIN_PCT,
            "max_false_positive_gain_pct": MAX_FALSE_POSITIVE_GAIN_PCT,
            "min_admit_rate_after_pct": MIN_ADMIT_RATE_AFTER_PCT,
            "min_crawl_probe_gain_pct": MIN_CRAWL_PROBE_GAIN_PCT,
            "min_crawl_probe_articles": MIN_CRAWL_PROBE_ARTICLES,
        },
    }


# ─────────────────────────── 交付物 1.3：影子自测 ───────────────────────────
def shadow_test(
    pack_id: str,
    candidates,
    *,
    sample_limit: int = 400,
    negative_sample: int = NEGATIVE_SAMPLE_DEFAULT,
) -> Dict:
    """影子重跑：把候选词合并进**内存副本**后，对正/负样本各跑一遍改前 vs 改后。

    正样本 = 本包被判 other 的文章；负样本 = 其它包已准入（trend/event）的文章。
    "改前"= 当前**已发布配置**重跑（不是 DB 里的历史结论），这是影子自测的正确基线。
    所有比率都是百分比（0~100）；样本为空时返回 None，绝不返回假的 0。

    返回的四个核心指标：
        ``admit_rate_before/after``   正样本准入率
        ``topic_assoc_before/after``  正样本"能命中固定主题关键词"的比例（match_fixed_topics 口径）
        ``false_positive_before/after`` 负样本被误准入的比例
        ``delta_other_pct``          正样本 other 占比的下降幅度（百分点）
    另有 ``topic_tagged_before/after``：分类结果里真正挂上主题标签的比例（经门禁，反映真实效果）。
    注意 ``topic_assoc`` 用 match_fixed_topics 直接算，而候选词只改 core/锚点/实体词，
    **不动 fixed_topics**，所以改前改后通常相同——这是正确行为，不是"没生效"。
    """
    pack = _load_pack(pack_id)
    buckets = _normalize_candidate_input(candidates)
    merged = _merge_candidates_into_pack(pack, buckets)
    positive_rows = _load_pack_articles(pack_id, category="other", limit=sample_limit)
    negative_rows = _load_peer_admitted_articles(
        pack_id, limit=max(1, _as_int(negative_sample, NEGATIVE_SAMPLE_DEFAULT))
    )

    pos_before_admit = pos_after_admit = 0
    pos_before_topic = pos_after_topic = 0
    pos_before_tagged = pos_after_tagged = 0
    newly_admitted: List[Dict] = []
    lost_admitted: List[Dict] = []
    for row in positive_rows:
        article = _article_from_row(row)
        before = classify_article(article, pack)
        after = classify_article(article, merged)
        admit_before, admit_after = _is_admitted(before), _is_admitted(after)
        pos_before_admit += 1 if admit_before else 0
        pos_after_admit += 1 if admit_after else 0
        pos_before_topic += 1 if match_fixed_topics(article, pack) else 0
        pos_after_topic += 1 if match_fixed_topics(article, merged) else 0
        pos_before_tagged += 1 if (before.get("topic_keys") or []) else 0
        pos_after_tagged += 1 if (after.get("topic_keys") or []) else 0
        if admit_after and not admit_before:
            newly_admitted.append(_example(article, after))
        elif admit_before and not admit_after:
            lost_admitted.append(_example(article, after))

    neg_before_admit = neg_after_admit = 0
    new_false_positives: List[Dict] = []
    for row in negative_rows:
        article = _article_from_row(row)
        before = classify_article(article, pack)
        after = classify_article(article, merged)
        admit_before, admit_after = _is_admitted(before), _is_admitted(after)
        neg_before_admit += 1 if admit_before else 0
        neg_after_admit += 1 if admit_after else 0
        if admit_after and not admit_before:
            new_false_positives.append(_example(article, after))

    positive_total = len(positive_rows)
    negative_total = len(negative_rows)
    admit_before = _pct(pos_before_admit, positive_total)
    admit_after = _pct(pos_after_admit, positive_total)
    fp_before = _pct(neg_before_admit, negative_total)
    fp_after = _pct(neg_after_admit, negative_total)
    other_before = None if admit_before is None else round(100.0 - admit_before, 4)
    other_after = None if admit_after is None else round(100.0 - admit_after, 4)

    def _delta(after_value, before_value):
        if after_value is None or before_value is None:
            return None
        return round(after_value - before_value, 4)

    return {
        "pack_id": str(pack_id),
        "pack_version": str(pack.get("pack_version") or ""),
        "tested_at": utc_text(),
        "positive_sample": positive_total,
        "negative_sample": negative_total,
        "admit_rate_before": admit_before,
        "admit_rate_after": admit_after,
        "delta_admit_rate": _delta(admit_after, admit_before),
        "topic_assoc_before": _pct(pos_before_topic, positive_total),
        "topic_assoc_after": _pct(pos_after_topic, positive_total),
        "topic_tagged_before": _pct(pos_before_tagged, positive_total),
        "topic_tagged_after": _pct(pos_after_tagged, positive_total),
        "false_positive_before": fp_before,
        "false_positive_after": fp_after,
        "delta_false_positive": _delta(fp_after, fp_before),
        "other_pct_before": other_before,
        "other_pct_after": other_after,
        "delta_other_pct": _delta(other_after, other_before),
        # UI 契约：before / after 两个子对象（缺值一律 None，键必须存在）
        "before": {
            "admit_rate": admit_before,
            "false_positive": fp_before,
            "topic_assoc": _pct(pos_before_topic, positive_total),
            "other_pct": other_before,
        },
        "after": {
            "admit_rate": admit_after,
            "false_positive": fp_after,
            "topic_assoc": _pct(pos_after_topic, positive_total),
            "other_pct": other_after,
        },
        "delta": {
            "admit_rate": _delta(admit_after, admit_before),
            "false_positive": _delta(fp_after, fp_before),
            "topic_assoc": _delta(
                _pct(pos_after_topic, positive_total), _pct(pos_before_topic, positive_total)
            ),
            "other_pct": _delta(other_after, other_before),
        },
        "candidates_applied": {key: list(value) for key, value in buckets.items()},
        "examples": {
            "newly_admitted": newly_admitted[:10],
            "new_false_positives": new_false_positives[:10],
            "lost_admitted": lost_admitted[:10],
        },
        "recomputable": {
            "positive_article_ids": [_as_int(row.get("article_id")) for row in positive_rows],
            "negative_article_ids": [_as_int(row.get("article_id")) for row in negative_rows],
            "pack_version": str(pack.get("pack_version") or ""),
            "content_chars_cap": SAMPLE_CONTENT_CHARS,
            "merged_keyword_counts": {
                "core_keywords": len(merged.get("core_keywords") or []),
                "anchor_keywords": len(
                    (merged.get("candidate_gate") or {}).get("anchor_keywords") or []
                ),
                "entity_keywords": len(
                    (merged.get("candidate_gate") or {}).get("entity_keywords") or []
                ),
            },
        },
    }


def _example(article: Dict, result: Dict) -> Dict:
    return {
        "article_id": _as_int(article.get("article_id") or article.get("id")),
        "title": _truncate(article.get("title")),
        "category": str(result.get("final_category") or ""),
        "matched_keywords": [
            _truncate(item, 40) for item in (result.get("matched_keywords") or [])[:8]
        ],
    }


# ─────────────────────────── 达标判定 ───────────────────────────
def _shadow_verdict(shadow: Dict) -> Dict:
    """影子自测是否达标（阈值全部来自模块常量）。"""
    before = shadow.get("before") or {}
    after = shadow.get("after") or {}
    checks: List[Dict] = []
    reasons: List[str] = []
    admit_before = _as_float(before.get("admit_rate"))
    admit_after = _as_float(after.get("admit_rate"))
    fp_before = _as_float(before.get("false_positive"))
    fp_after = _as_float(after.get("false_positive"))

    if admit_before is None or admit_after is None:
        checks.append(
            {
                "key": "shadow_sample",
                "passed": False,
                "value": None,
                "threshold": MIN_ADMIT_RATE_GAIN_PCT,
                "note": "本包没有 other 正样本，影子自测无数据",
            }
        )
        reasons.append("影子自测无正样本（本包没有 other 文章），无法验证")
    else:
        gain = round(admit_after - admit_before, 4)
        checks.append(
            {
                "key": "admit_rate_gain",
                "passed": gain >= MIN_ADMIT_RATE_GAIN_PCT,
                "value": gain,
                "threshold": MIN_ADMIT_RATE_GAIN_PCT,
                "note": f"准入率 {admit_before}% → {admit_after}%",
            }
        )
        if gain < MIN_ADMIT_RATE_GAIN_PCT:
            reasons.append(
                f"准入率仅提升 {gain} 个百分点 < 阈值 {MIN_ADMIT_RATE_GAIN_PCT}"
            )
        checks.append(
            {
                "key": "admit_rate_after",
                "passed": admit_after >= MIN_ADMIT_RATE_AFTER_PCT,
                "value": admit_after,
                "threshold": MIN_ADMIT_RATE_AFTER_PCT,
                "note": "改后准入率下限",
            }
        )
        if admit_after < MIN_ADMIT_RATE_AFTER_PCT:
            reasons.append(
                f"改后准入率 {admit_after}% < 下限 {MIN_ADMIT_RATE_AFTER_PCT}%"
            )
    if fp_before is None or fp_after is None:
        checks.append(
            {
                "key": "false_positive",
                "passed": False,
                "value": None,
                "threshold": MAX_FALSE_POSITIVE_GAIN_PCT,
                "note": "没有其它包的已准入文章作负样本，误准入无法验证",
            }
        )
        reasons.append("影子自测无负样本（其它包没有已准入文章），误准入无法验证")
    else:
        fp_gain = round(fp_after - fp_before, 4)
        checks.append(
            {
                "key": "false_positive",
                "passed": fp_gain <= MAX_FALSE_POSITIVE_GAIN_PCT,
                "value": fp_gain,
                "threshold": MAX_FALSE_POSITIVE_GAIN_PCT,
                "note": f"误准入 {fp_before}% → {fp_after}%",
            }
        )
        if fp_gain > MAX_FALSE_POSITIVE_GAIN_PCT:
            reasons.append(
                f"误准入率上升 {fp_gain} 个百分点 > 上限 {MAX_FALSE_POSITIVE_GAIN_PCT}"
            )
    return {"passed": not reasons, "reason": "；".join(reasons), "checks": checks}


def _crawl_probe_verdict(probe) -> Dict:
    """抓文章实测是否达标。抓不到样本（0 篇解析成功）一律不达标。"""
    if not isinstance(probe, dict):
        return {
            "passed": False,
            "reason": "缺少抓文章测试结果",
            "checks": [],
            "sample_available": False,
        }
    parsed = _as_int(probe.get("parsed_total"))
    before = _as_float(probe.get("new_article_admit_rate_before"))
    after = _as_float(probe.get("new_article_admit_rate_after"))
    checks: List[Dict] = []
    if parsed <= 0:
        return {
            "passed": False,
            "reason": "抓文章测试未取得样本，无法验证（抓取失败不计为 0% 准入率）",
            "checks": [
                {
                    "key": "crawl_probe_sample",
                    "passed": False,
                    "value": 0,
                    "threshold": MIN_CRAWL_PROBE_ARTICLES,
                    "note": "成功解析的新文章数",
                }
            ],
            "sample_available": False,
        }
    checks.append(
        {
            "key": "crawl_probe_sample",
            "passed": parsed >= MIN_CRAWL_PROBE_ARTICLES,
            "value": parsed,
            "threshold": MIN_CRAWL_PROBE_ARTICLES,
            "note": "成功解析的新文章数",
        }
    )
    if before is None or after is None:
        return {
            "passed": False,
            "reason": "抓文章测试缺少准入率指标",
            "checks": checks,
            "sample_available": True,
        }
    gain = round(after - before, 4)
    # 已经 100% 准入的源没有改进空间：这种边界只需"不变差"。
    if before >= 99.999:
        passed = after >= before - 1e-9
        note = "改前已全部准入，只需不变差"
    else:
        passed = gain >= MIN_CRAWL_PROBE_GAIN_PCT
        note = f"新文章准入率 {before}% → {after}%"
    checks.append(
        {
            "key": "crawl_probe_gain",
            "passed": passed,
            "value": gain,
            "threshold": MIN_CRAWL_PROBE_GAIN_PCT,
            "note": note,
        }
    )
    reason = ""
    if not passed:
        reason = (
            f"抓文章实测准入率仅提升 {gain} 个百分点 < 阈值 {MIN_CRAWL_PROBE_GAIN_PCT}"
            if gain < MIN_CRAWL_PROBE_GAIN_PCT
            else "抓文章实测准入率下降"
        )
    if parsed < MIN_CRAWL_PROBE_ARTICLES:
        reason = (
            f"抓文章测试样本不足（成功解析 {parsed} 篇 < {MIN_CRAWL_PROBE_ARTICLES} 篇）"
        )
    return {
        "passed": passed and parsed >= MIN_CRAWL_PROBE_ARTICLES,
        "reason": reason,
        "checks": checks,
        "sample_available": True,
    }


def _evaluate_self_test(shadow: Dict, probe) -> Dict:
    """双达标：影子重跑指标 + 抓文章实测指标**都要过**才算 verified。"""
    shadow_verdict = _shadow_verdict(shadow)
    probe_verdict = _crawl_probe_verdict(probe)
    reasons = []
    if not shadow_verdict["passed"]:
        reasons.append(f"影子自测未达标：{shadow_verdict['reason']}")
    if not probe_verdict["passed"]:
        reasons.append(f"抓文章实测未达标：{probe_verdict['reason']}")
    passed = not reasons
    if passed:
        passed_text = (
            f"影子自测达标（准入率 {shadow.get('admit_rate_before')}% → "
            f"{shadow.get('admit_rate_after')}%，误准入 {shadow.get('false_positive_before')}% → "
            f"{shadow.get('false_positive_after')}%）"
        )
        probe_text = (
            f"抓文章实测达标（成功解析 {_as_int((probe or {}).get('parsed_total'))} 篇，"
            f"准入率 {(probe or {}).get('new_article_admit_rate_before')}% → "
            f"{(probe or {}).get('new_article_admit_rate_after')}%）"
            if isinstance(probe, dict)
            else "抓文章实测达标"
        )
        reason = f"{passed_text}；{probe_text}"
    else:
        reason = "；".join(reasons)
    return {
        "passed": passed,
        "reason": reason,
        "shadow": shadow_verdict,
        "crawl_probe": probe_verdict,
    }


# ─────────────────────────── 交付物 1.7：抓文章实测探针 ───────────────────────────
def _probe_rank(row: Dict) -> int:
    """探针选源优先级：准入率低(0) → 零产出(1) → 长期无新文(2) → 有效(3)。"""
    return {"准入率低": 0, "零产出": 1, "长期无新文": 2}.get(str(row.get("verdict")), 3)


def _probe_select_sources(pack_id: str, limit: int) -> List[Dict]:
    """挑最多 limit 个已启用信源：优先"准入率低/零产出"（改进要解决的就是它们）。

    偏好非浏览器信源：探针只走 HTTP，不启 Playwright（缺 chromium 也不会整体失败）。
    """
    rows = _source_stats(pack_id)
    rows.sort(
        key=lambda row: (
            _probe_rank(row),
            1 if (row.get("metadata") or {}).get("browser_fetch_enabled") else 0,
            -_as_int(row.get("authority_level")),
            _as_int(row.get("source_id")),
        )
    )
    return rows[: max(1, _as_int(limit, CRAWL_PROBE_MAX_SOURCES))]


def _probe_request_timeout(timeout_seconds: float) -> Tuple[float, float]:
    budget = max(3.0, float(timeout_seconds or CRAWL_PROBE_TIMEOUT_SECONDS))
    return (min(10.0, budget), min(20.0, budget))


def _probe_fetch_listing(
    source: Dict, *, limit: int, timeout_seconds: float
) -> Tuple[List[Dict], str]:
    """真实抓一次信源列表（RSS 或列表页），返回 (items, error_text)。

    复用仓库既有解析：``rss_feed_contract.parse_rss_feed`` 与
    ``intel_light_scanner.ListPageScanner.scan_html``；只走 HTTP，不启浏览器。
    """
    from intel_http import SafeHTTPClient
    from intel_light_scanner import USER_AGENT, ListPageScanner

    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    rss_url = str(metadata.get("rss_url") or "").strip()
    is_rss = str(source.get("source_type") or "") == "rss" or bool(rss_url)
    url = rss_url or str(source.get("source_url") or "").strip()
    if not url:
        return [], "信源没有可抓取的 URL"
    client = SafeHTTPClient()
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "application/rss+xml, application/xml, text/xml"
            if is_rss
            else "text/html,application/xhtml+xml"
        ),
    }
    response = client.get(url, headers=headers, timeout=_probe_request_timeout(timeout_seconds))
    if is_rss:
        from rss_feed_contract import parse_rss_feed

        return parse_rss_feed(response, limit=max(1, _as_int(limit, 1))), ""
    include_pattern = str(metadata.get("link_include_pattern") or "").strip() or None
    scanner = ListPageScanner(http_client=client)
    items = scanner.scan_html(
        response.url,
        response.content,
        limit=max(1, _as_int(limit, 1)),
        include_pattern=include_pattern,
    )
    return items, ""


def _probe_extract_text(html: str, url: str) -> str:
    """正文抽取：trafilatura（若装了）优先，否则用 BeautifulSoup 取最长候选块。"""
    text = ""
    try:
        import trafilatura  # type: ignore

        text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    except Exception:
        text = ""
    text = " ".join(str(text or "").split())
    if len(text) >= PROBE_MIN_CONTENT_CHARS:
        return text[:SAMPLE_CONTENT_CHARS]
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "form"]):
            tag.decompose()
        best = ""
        for selector in (
            "article",
            "main",
            "[role=main]",
            ".article-content",
            ".post-content",
            ".content",
            "#content",
        ):
            for node in soup.select(selector):
                candidate = " ".join(node.get_text(" ", strip=True).split())
                if len(candidate) > len(best):
                    best = candidate
        if len(best) < len(text):
            best = text
        if not best:
            body = soup.body or soup
            best = " ".join(body.get_text(" ", strip=True).split())
        return best[:SAMPLE_CONTENT_CHARS]
    except Exception:
        return text[:SAMPLE_CONTENT_CHARS]


def _probe_extract_title(html: str) -> str:
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        node = soup.find("meta", property="og:title") or soup.find("title")
        if node is None:
            return ""
        return _truncate(node.get("content") if node.name == "meta" else node.get_text(" ", strip=True))
    except Exception:
        return ""


def _probe_fetch_content(item: Dict, *, timeout_seconds: float) -> Tuple[Optional[Dict], str]:
    """抓一篇新文章的正文，返回 (article, error_text)。正文太短算解析失败。"""
    from intel_http import SafeHTTPClient
    from intel_light_scanner import USER_AGENT

    url = str((item or {}).get("url") or "").strip()
    if not url:
        return None, "条目缺少 URL"
    client = SafeHTTPClient()
    response = client.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        timeout=_probe_request_timeout(timeout_seconds),
    )
    html = response.text
    content = _probe_extract_text(html, url)
    title = _truncate(item.get("title")) or _probe_extract_title(html)
    if len(content) < PROBE_MIN_CONTENT_CHARS:
        return None, f"正文过短（{len(content)} 字 < {PROBE_MIN_CONTENT_CHARS}）"
    return {
        "title": title,
        "content": content,
        "matched_keywords": "",
        "url": url,
        "publish_date": str(item.get("published_at") or ""),
    }, ""


def crawl_probe(
    pack_id: str,
    *,
    per_source: int = CRAWL_PROBE_PER_SOURCE,
    timeout_seconds: float = CRAWL_PROBE_TIMEOUT_SECONDS,
    max_sources: int = CRAWL_PROBE_MAX_SOURCES,
    candidates=None,
    source_picker=None,
    listing_fetcher=None,
    content_fetcher=None,
) -> Dict:
    """真跑一遍抓文章测试：抓新文章 → 改前/改后各分类一次（纯网络 + CPU，不调模型）。

    * ``timeout_seconds`` 是**总预算**：每步开始前检查剩余预算，单次请求超时取
      ``min(剩余预算, 20s)``。阻塞中的 socket 无法中断，实际耗时可能略超预算。
    * 抓取/解析失败一律写进 ``errors`` 与 ``parse_failed``，**绝不记成"准入率 0"**。
    * 一个源都没解析出文章时 ``parsed_total == 0``、比率为 None，由调用方判为"无样本"。
    * 默认选源 = 本包已启用信源里"准入率低/零产出"优先（见 ``_probe_select_sources``）。
    """
    started = time.monotonic()
    budget = max(3.0, float(timeout_seconds or CRAWL_PROBE_TIMEOUT_SECONDS))
    deadline = started + budget
    pack = _load_pack(pack_id)
    buckets = _normalize_candidate_input(candidates)
    merged = _merge_candidates_into_pack(pack, buckets) if _has_candidates(buckets) else pack

    picker = source_picker or _probe_select_sources
    try:
        selected = list(picker(pack_id, max(1, _as_int(max_sources, CRAWL_PROBE_MAX_SOURCES))) or [])
    except Exception as exc:
        selected = []
        select_error = str(exc)[:200]
    else:
        select_error = ""

    errors: List[Dict] = []
    if select_error:
        errors.append({"source_id": None, "stage": "select_sources", "error": select_error})
    sources_out: List[Dict] = []
    fetched_total = parsed_total = parse_failed_total = 0
    for source in selected:
        source_id = _as_int(source.get("source_id"))
        source_out = {
            "source_id": source_id,
            "source_name": str(source.get("source_name") or ""),
            "source_url": str(source.get("source_url") or ""),
            "verdict": str(source.get("verdict") or ""),
            "listing_status": "ok",
            "listing_error": "",
            "fetched": 0,
            "parsed": 0,
            "parse_failed": 0,
            "articles": [],
            "admit_rate_before": None,
            "admit_rate_after": None,
        }
        sources_out.append(source_out)
        if time.monotonic() >= deadline:
            source_out["listing_status"] = "skipped"
            source_out["listing_error"] = "探针总预算已用尽"
            errors.append(
                {"source_id": source_id, "stage": "budget", "error": "探针总预算已用尽"}
            )
            continue
        try:
            listing, listing_error = (listing_fetcher or _probe_fetch_listing)(
                source,
                limit=max(1, _as_int(per_source, CRAWL_PROBE_PER_SOURCE)),
                timeout_seconds=deadline - time.monotonic(),
            )
        except Exception as exc:
            listing, listing_error = [], _probe_error_text(exc)
        if listing_error:
            source_out["listing_status"] = "failed"
            source_out["listing_error"] = listing_error
            errors.append(
                {"source_id": source_id, "stage": "listing", "error": listing_error}
            )
            continue
        entries = list(listing or [])[: max(1, _as_int(per_source, CRAWL_PROBE_PER_SOURCE))]
        source_out["fetched"] = len(entries)
        fetched_total += len(entries)
        before_admit = after_admit = 0
        for entry in entries:
            if time.monotonic() >= deadline:
                errors.append(
                    {
                        "source_id": source_id,
                        "stage": "budget",
                        "error": "正文抓取阶段总预算已用尽，剩余条目未抓",
                    }
                )
                break
            try:
                article, content_error = (content_fetcher or _probe_fetch_content)(
                    entry, timeout_seconds=deadline - time.monotonic()
                )
            except Exception as exc:
                article, content_error = None, _probe_error_text(exc)
            if content_error or not article:
                source_out["parse_failed"] += 1
                parse_failed_total += 1
                errors.append(
                    {
                        "source_id": source_id,
                        "stage": "content",
                        "url": str(entry.get("url") or ""),
                        "error": content_error or "正文抓取失败",
                    }
                )
                continue
            source_out["parsed"] += 1
            parsed_total += 1
            result_before = classify_article(article, pack)
            result_after = classify_article(article, merged)
            admit_before = _is_admitted(result_before)
            admit_after = _is_admitted(result_after)
            before_admit += 1 if admit_before else 0
            after_admit += 1 if admit_after else 0
            source_out["articles"].append(
                {
                    "url": str(article.get("url") or ""),
                    "title": _truncate(article.get("title")),
                    "category_before": str(result_before.get("final_category") or ""),
                    "category_after": str(result_after.get("final_category") or ""),
                    "admit_before": admit_before,
                    "admit_after": admit_after,
                    # anchors_* 才是"让文章过门禁的词"：classify_article 的 matched_keywords
                    # 只收 core/expanded/trend/event/negative，不含锚点，直接看它会漏掉真正原因。
                    "anchors_before": list(
                        ((result_before.get("score_details") or {}).get("hits") or {}).get("anchor") or []
                    ),
                    "anchors_after": list(
                        ((result_after.get("score_details") or {}).get("hits") or {}).get("anchor") or []
                    ),
                    "matched_keywords_before": [
                        _truncate(term, 40)
                        for term in (result_before.get("matched_keywords") or [])[:8]
                    ],
                    "matched_keywords_after": [
                        _truncate(term, 40)
                        for term in (result_after.get("matched_keywords") or [])[:8]
                    ],
                }
            )
        if source_out["parsed"]:
            source_out["admit_rate_before"] = _pct(before_admit, source_out["parsed"])
            source_out["admit_rate_after"] = _pct(after_admit, source_out["parsed"])

    admit_before_total = sum(
        1 for source in sources_out for item in source["articles"] if item["admit_before"]
    )
    admit_after_total = sum(
        1 for source in sources_out for item in source["articles"] if item["admit_after"]
    )
    return {
        "pack_id": str(pack_id),
        "pack_version": str(pack.get("pack_version") or ""),
        "probed_at": utc_text(),
        "per_source": _as_int(per_source, CRAWL_PROBE_PER_SOURCE),
        "timeout_seconds": budget,
        "max_sources": _as_int(max_sources, CRAWL_PROBE_MAX_SOURCES),
        "duration_seconds": round(time.monotonic() - started, 3),
        "sources_selected": [
            {
                "source_id": _as_int(source.get("source_id")),
                "source_name": str(source.get("source_name") or ""),
                "source_url": str(source.get("source_url") or ""),
                "verdict": str(source.get("verdict") or ""),
            }
            for source in selected
        ],
        "sources": sources_out,
        "sources_probed": len(sources_out),
        "sources_succeeded": sum(1 for source in sources_out if source["parsed"] > 0),
        "fetched_total": fetched_total,
        "parsed_total": parsed_total,
        "parse_failed_total": parse_failed_total,
        "new_article_admit_rate_before": _pct(admit_before_total, parsed_total),
        "new_article_admit_rate_after": _pct(admit_after_total, parsed_total),
        "new_articles": [
            item for source in sources_out for item in source["articles"]
        ],
        "sample_available": parsed_total > 0,
        "errors": errors,
        "candidates_applied": {key: list(value) for key, value in buckets.items()},
    }


def _probe_error_text(exc: Exception) -> str:
    try:
        from intel_http import sanitize_external_error

        return _truncate(sanitize_external_error(exc), 300)
    except Exception:
        return _truncate(f"{type(exc).__name__}: {exc}", 300)


# ─────────────────────────── 交付物 1.7b：信源实测探针 probe_sources ───────────────────────────
class _ProbeResponse:
    """把探测结果伪装成 ``intel_http.HTTPFetchResult``（``parse_rss_feed`` 只读这 4 个属性）。"""

    __slots__ = ("url", "status_code", "content", "content_type")

    def __init__(self, url: str, status_code, content: bytes, content_type: str):
        self.url = str(url or "")
        self.status_code = _as_int(status_code)
        self.content = bytes(content or b"")
        self.content_type = str(content_type or "")


def _load_source_rows(source_ids: Sequence) -> Dict[int, Dict]:
    """按 id 直接取信源（含未启用的）：显式点名探测时用，补 ``_source_stats`` 覆盖不到的源。"""
    ids = []
    for item in source_ids or []:
        source_id = _as_int(item)
        if source_id > 0 and source_id not in ids:
            ids.append(source_id)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    out: Dict[int, Dict] = {}
    for row in _query(
        f"SELECT id, source_name, source_url, source_type, metadata_json, is_enabled "
        f"FROM intel_sources WHERE id IN ({placeholders})",
        ids,
    ):
        source_id = _as_int(row.get("id"))
        out[source_id] = {
            "source_id": source_id,
            "source_name": str(row.get("source_name") or ""),
            "source_url": str(row.get("source_url") or ""),
            "source_type": str(row.get("source_type") or "website"),
            "metadata": _json_loads(row.get("metadata_json"), {}),
            "is_enabled": bool(_as_int(row.get("is_enabled"))),
        }
    return out


def _probe_source_select(pack_id: str, source_ids, limit: int) -> List[Dict]:
    """挑要探测的信源：显式 source_ids 优先（按给定顺序），否则取"零产出/准入率低"优先的前 limit 个。"""
    cap = max(1, _as_int(limit, SOURCE_PROBE_MAX_SOURCES))
    wanted = []
    for item in source_ids or []:
        source_id = _as_int(item)
        if source_id > 0 and source_id not in wanted:
            wanted.append(source_id)
    if wanted:
        found = _load_source_rows(wanted)
        selected = []
        for source_id in wanted[:cap]:
            row = found.get(source_id)
            if row is None:
                # 库里没有这个 id：记成"未启用"（不构成坏源证据），而不是当成坏源
                row = {
                    "source_id": source_id,
                    "source_name": "",
                    "source_url": "",
                    "source_type": "",
                    "metadata": {},
                    "is_enabled": False,
                    "missing": True,
                }
            selected.append(row)
        return selected
    rows = _source_stats(pack_id)
    rows.sort(key=lambda row: (_probe_rank(row), _as_int(row.get("source_id"))))
    selected = []
    for row in rows[:cap]:
        item = dict(row)
        item["is_enabled"] = True  # _source_stats 只返回 is_enabled=1 的源
        selected.append(item)
    return selected


def _probe_source_url(source: Dict) -> Tuple[str, bool]:
    """探针要抓的 URL + 是否按 feed 口径解析（rss 类型或 metadata.rss_url 命中即按 feed）。"""
    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    rss_url = str(metadata.get("rss_url") or "").strip()
    is_rss = str(source.get("source_type") or "") == "rss" or bool(rss_url)
    return (rss_url or str(source.get("source_url") or "").strip(), is_rss)


def _default_source_probe_fetcher(source: Dict, url: str, timeout_seconds: float) -> Dict:
    """默认抓取器：真发一次 HTTP GET（复用 ``intel_http.SafeHTTPClient``，无浏览器、无模型）。

    只在连接层抛异常时返回 ``status_code=None``；HTTP 4xx/5xx 也在这里落成状态码，交给
    ``_probe_source_verdict`` 判"URL 失效/需登录或反爬"。
    """
    from intel_http import SafeHTTPClient
    from intel_light_scanner import USER_AGENT

    try:
        response = SafeHTTPClient().get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/xml, text/xml, text/html, application/xhtml+xml",
            },
            timeout=_probe_request_timeout(timeout_seconds),
        )
    except Exception as exc:
        status = _as_int(getattr(getattr(exc, "response", None), "status_code", 0))
        return {
            "status_code": status or None,
            "content": b"",
            "content_type": "",
            "url": url,
            "error": _probe_error_text(exc),
        }
    return {
        "status_code": response.status_code,
        "content": response.content,
        "content_type": response.content_type,
        "url": response.url,
        "error": "",
    }


def _normalize_probe_response(raw) -> Dict:
    """归一 fetcher 返回值：dict 或带同名属性的对象都接受；content 统一成 bytes。"""
    if isinstance(raw, dict):
        status = raw.get("status_code")
        content = raw.get("content")
        if content is None:
            content = raw.get("text")
        content_type = raw.get("content_type")
        url = raw.get("url")
        error = raw.get("error")
    else:
        status = getattr(raw, "status_code", None)
        content = getattr(raw, "content", None)
        content_type = getattr(raw, "content_type", "")
        url = getattr(raw, "url", "")
        error = getattr(raw, "error", "")
    if isinstance(content, str):
        content = content.encode("utf-8", "replace")
    return {
        "status_code": _as_int(status) or None,
        "content": bytes(content or b""),
        "content_type": str(content_type or ""),
        "url": str(url or ""),
        "error": str(error or ""),
    }


def _probe_source_parse(
    source: Dict, response: Dict, *, is_rss: bool, limit: int
) -> Tuple[int, str]:
    """解析条目数：feed 走 ``rss_feed_contract.parse_rss_feed``，列表页走 ``ListPageScanner.scan_html``。

    两者都是仓库既有能力（纯解析）。**刻意不走 ``ListPageScanner.scan``**：那条路会调
    ``site_scraper_models.extract_with_model``（模型提取），违反"不调任何模型端点"的硬约束。
    解析异常不抛出去，返回 ``(0, 错误原文)``，由 verdict 判"空 feed / 页面不可解析"。
    """
    content = response.get("content") or b""
    url = str(response.get("url") or "")
    if is_rss:
        from rss_feed_contract import parse_rss_feed

        try:
            items = parse_rss_feed(
                _ProbeResponse(url, response.get("status_code"), content, response.get("content_type")),
                limit=max(1, _as_int(limit, SOURCE_PROBE_ITEM_LIMIT)),
            )
        except Exception as exc:
            return 0, _probe_error_text(exc)
        return len(items or []), ""
    from intel_light_scanner import ListPageScanner

    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    include_pattern = str(metadata.get("link_include_pattern") or "").strip() or None
    try:
        items = ListPageScanner().scan_html(
            url or str(source.get("source_url") or ""),
            content,
            limit=max(1, _as_int(limit, SOURCE_PROBE_ITEM_LIMIT)),
            include_pattern=include_pattern,
        )
    except Exception as exc:
        return 0, _probe_error_text(exc)
    return len(items or []), ""


def _probe_source_verdict(
    status_code, content_type: str, parsed_items: int, *, is_rss: bool
) -> str:
    """逐源 verdict（口径写在模块常量那一节，改口径就改这里）。"""
    if status_code is None:
        # DNS 失败/超时/SSL/连接被重置：拿不到任何响应，等同 URL 不可用（原文留在 error 里）
        return PROBE_VERDICT_DEAD
    if int(status_code) in (401, 403):
        return PROBE_VERDICT_BLOCKED
    if int(status_code) in (404, 410):
        return PROBE_VERDICT_DEAD
    if not 200 <= int(status_code) < 300:
        # 其它非 2xx（含 5xx/502）：拿不到内容就等同该 URL 不可用，状态码留在 http_status 供人工区分
        return PROBE_VERDICT_DEAD
    if parsed_items > 0:
        return PROBE_VERDICT_OK
    media = str(content_type or "").split(";")[0].strip().casefold()
    if media:
        return PROBE_VERDICT_UNPARSABLE if "html" in media else PROBE_VERDICT_EMPTY_FEED
    # 没有 Content-Type（部分老旧站点）：按源类型兜底
    return PROBE_VERDICT_EMPTY_FEED if is_rss else PROBE_VERDICT_UNPARSABLE


def probe_sources(
    pack_id: str,
    *,
    source_ids=None,
    limit: int = SOURCE_PROBE_MAX_SOURCES,
    timeout_seconds: float = SOURCE_PROBE_TIMEOUT_SECONDS,
    fetcher=None,
) -> Dict:
    """对信源 URL 做一次真实探测，产出"这个源到底还能不能用"的逐源第一手证据。

    **为什么必须有它**：信源类建议过去只按"零产出 → 建议停用"的机械规则就判 verified，
    实测证明会大规模误杀（A 机 invest_mgmt 65 个零产出源里 46 个 URL 完全正常，只是调度从未
    执行）。所以停用/替换前必须先探测，本函数是这条证据的唯一合法来源
    （输出带 ``generated_by="probe_sources"``，闸门会校验，手工伪造的一律不认）。

    参数：
      * ``source_ids``：显式指定要探测的源（按给定顺序，最多 ``limit`` 个）；缺省时按
        "准入率低/零产出优先"取本包已启用信源的前 ``limit`` 个。
      * ``limit``：最多探测多少个源，默认 20。
      * ``timeout_seconds``：单源请求预算，默认 12 秒；总预算 = ``timeout_seconds`` ×
        实际探测源数，每次请求的超时取 ``min(剩余总预算, timeout_seconds)``。
      * ``fetcher``：抓取器注入点（测试打桩）。签名 ``fetcher(source, url, timeout_seconds)``，
        返回 dict（``status_code``/``content``/``content_type``/``url``/``error``）或任何带这
        几个属性的对象；抛异常视为连接层失败（判 URL 失效）。默认实现走 ``SafeHTTPClient``。

    硬约束：**串行、每源最多 1 次请求、不启浏览器、不调模型**；返回只含 JSON 可序列化类型。
    """
    started = time.monotonic()
    cap = max(1, _as_int(limit, SOURCE_PROBE_MAX_SOURCES))
    per_source_timeout = max(
        1.0, _as_float(timeout_seconds, float(SOURCE_PROBE_TIMEOUT_SECONDS)) or float(SOURCE_PROBE_TIMEOUT_SECONDS)
    )
    selected = _probe_source_select(pack_id, source_ids, cap)
    deadline = started + per_source_timeout * max(1, len(selected))
    rows: List[Dict] = []
    errors: List[Dict] = []
    for source in selected:
        source_id = _as_int(source.get("source_id"))
        url, is_rss = _probe_source_url(source)
        record = {
            "source_id": source_id,
            "source_name": str(source.get("source_name") or ""),
            "source_url": str(source.get("source_url") or ""),
            "probe_url": url,
            "source_type": str(source.get("source_type") or ""),
            "is_enabled": bool(source.get("is_enabled")),
            "probed": False,
            "reachable": False,
            "http_status": None,
            "content_type": "",
            "parsed_items": 0,
            "error": "",
            "verdict": PROBE_VERDICT_DISABLED,
            "probed_at": utc_text(),
        }
        rows.append(record)
        if not record["is_enabled"]:
            record["error"] = "信源未启用（is_enabled=0），未发起探测"
            continue
        if not url:
            record["probed"] = True
            record["error"] = "信源没有可探测的 URL"
            record["verdict"] = PROBE_VERDICT_DEAD
            errors.append({"source_id": source_id, "error": record["error"]})
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # 没发起请求就不算证据（闸门只认 probed=True 的记录），仅记录跳过原因
            record["error"] = "探测总预算已用尽，未发起请求（不构成停用依据）"
            continue
        record["probed"] = True
        try:
            raw = (fetcher or _default_source_probe_fetcher)(
                source, url, min(per_source_timeout, remaining)
            )
        except Exception as exc:
            response = {
                "status_code": None,
                "content": b"",
                "content_type": "",
                "url": url,
                "error": _probe_error_text(exc),
            }
        else:
            response = _normalize_probe_response(raw)
        record["http_status"] = response["status_code"]
        record["content_type"] = response["content_type"][:120]
        record["reachable"] = bool(
            response["status_code"] and 200 <= int(response["status_code"]) < 300
        )
        record["error"] = response["error"][:300]
        if record["reachable"]:
            parsed, parse_error = _probe_source_parse(
                source, response, is_rss=is_rss, limit=SOURCE_PROBE_ITEM_LIMIT
            )
            record["parsed_items"] = parsed
            if parse_error and not record["error"]:
                record["error"] = parse_error[:300]
        record["verdict"] = _probe_source_verdict(
            response["status_code"], response["content_type"], record["parsed_items"], is_rss=is_rss
        )
        if record["verdict"] in (PROBE_VERDICT_DEAD, PROBE_VERDICT_BLOCKED):
            errors.append(
                {
                    "source_id": source_id,
                    "error": record["error"] or f"HTTP {record['http_status']}",
                }
            )
    verdict_counts: Dict[str, int] = {}
    for record in rows:
        verdict_counts[record["verdict"]] = verdict_counts.get(record["verdict"], 0) + 1
    probed_count = sum(1 for record in rows if record["probed"])
    reachable_count = sum(1 for record in rows if record["reachable"])
    return {
        "pack_id": str(pack_id),
        "generated_by": SOURCE_PROBE_GENERATOR,
        "probed_at": utc_text(),
        "limit": cap,
        "timeout_seconds": per_source_timeout,
        "budget_seconds": round(per_source_timeout * max(1, len(selected)), 3),
        "duration_seconds": round(time.monotonic() - started, 3),
        "sources": rows,
        "sources_selected": [
            {
                "source_id": _as_int(source.get("source_id")),
                "source_name": str(source.get("source_name") or ""),
                "source_url": str(source.get("source_url") or ""),
            }
            for source in selected
        ],
        "sources_probed": probed_count,
        "sources_reachable": reachable_count,
        "sample_available": reachable_count > 0,
        "verdict_counts": verdict_counts,
        "errors": errors,
    }


def _source_probe_index(source_probe) -> Dict[int, Dict]:
    """逐源探测记录按 source_id 建索引（非 dict/缺 id 的记录直接丢）。"""
    if not isinstance(source_probe, dict):
        return {}
    out: Dict[int, Dict] = {}
    for row in source_probe.get("sources") or []:
        if isinstance(row, dict):
            out[_as_int(row.get("source_id"))] = row
    return out


def _source_probe_proves_bad(row) -> bool:
    """这条探测记录能不能当"该源不可用"的证据（停用/替换的**唯一**合法依据）。

    * 必须真的探测过（``probed=True``）——「未启用」「探针总预算用尽」都不是证据；
    * ``reachable=False``（连不上 / 非 2xx）或 verdict ∈ {URL 失效, 需登录或反爬, 空 feed}；
    * 「页面不可解析」不算（200 但静态解析不出条目：改版/JS 渲染/列表页 URL 配错都会这样），
      只进 keep_sources + notes 等人工确认。
    """
    if not isinstance(row, dict) or not row.get("probed"):
        return False
    verdict = str(row.get("verdict") or "")
    if verdict == PROBE_VERDICT_DISABLED:
        return False
    if verdict in SOURCE_PROBE_BAD_VERDICTS:
        return True
    return row.get("reachable") is False and bool(str(row.get("error") or "").strip())


def _slim_source_probe(source_probe) -> Optional[Dict]:
    """信源探测的精简版（供 metrics/接口回传，不缩水判定所需字段）。"""
    if not isinstance(source_probe, dict):
        return None
    return {
        "pack_id": str(source_probe.get("pack_id") or ""),
        "generated_by": str(source_probe.get("generated_by") or ""),
        "probed_at": str(source_probe.get("probed_at") or ""),
        "timeout_seconds": _as_float(source_probe.get("timeout_seconds")),
        "sources_probed": _as_int(source_probe.get("sources_probed")),
        "sources_reachable": _as_int(source_probe.get("sources_reachable")),
        "sample_available": bool(source_probe.get("sample_available")),
        "verdict_counts": dict(source_probe.get("verdict_counts") or {}),
        "sources": [
            {
                "source_id": _as_int(row.get("source_id")),
                "source_name": str(row.get("source_name") or ""),
                "source_url": str(row.get("source_url") or ""),
                "probed": bool(row.get("probed")),
                "reachable": bool(row.get("reachable")),
                "http_status": row.get("http_status"),
                "parsed_items": _as_int(row.get("parsed_items")),
                "verdict": str(row.get("verdict") or ""),
                "error": str(row.get("error") or "")[:200],
            }
            for row in (source_probe.get("sources") or [])
            if isinstance(row, dict)
        ],
        "errors": list(source_probe.get("errors") or []),
    }


# ─────────────────────────── 交付物 1.4~1.6：建议暂存与闭环 ───────────────────────────
_METRIC_KEYS = ("admit_rate", "false_positive", "topic_assoc", "other_pct")


def _normalize_metrics(metrics) -> Dict:
    """把指标归一成 UI 契约：``before``/``after`` 两个子对象 + ``crawl_probe``，键必须存在。

    拿不到的值写 None（不写 0，避免把"没测"伪装成"测出来是 0"）。
    """
    source = dict(metrics) if isinstance(metrics, dict) else {}
    result = dict(source)

    def _block(name: str) -> Dict:
        raw = source.get(name) if isinstance(source.get(name), dict) else {}
        block = {}
        for key in _METRIC_KEYS:
            flat = source.get(f"{key}_{name}")
            value = _as_float(raw.get(key))
            if value is None:
                value = _as_float(flat)
            block[key] = value
        return block

    before, after = _block("before"), _block("after")
    result["before"] = before
    result["after"] = after
    delta = source.get("delta") if isinstance(source.get("delta"), dict) else {}
    result["delta"] = {
        key: (
            _as_float(delta.get(key))
            if delta.get(key) is not None
            else (
                round(after[key] - before[key], 4)
                if after.get(key) is not None and before.get(key) is not None
                else None
            )
        )
        for key in _METRIC_KEYS
    }
    result["crawl_probe"] = (
        source.get("crawl_probe") if isinstance(source.get("crawl_probe"), dict) else None
    )
    result.setdefault("thresholds", {
        "min_admit_rate_gain_pct": MIN_ADMIT_RATE_GAIN_PCT,
        "max_false_positive_gain_pct": MAX_FALSE_POSITIVE_GAIN_PCT,
        "min_admit_rate_after_pct": MIN_ADMIT_RATE_AFTER_PCT,
        "min_crawl_probe_gain_pct": MIN_CRAWL_PROBE_GAIN_PCT,
        "min_crawl_probe_articles": MIN_CRAWL_PROBE_ARTICLES,
    })
    return result


def _normalize_payload(kind: str, payload) -> Dict:
    """payload 归一：keyword 建议的 ``candidates`` 三个桶必须都是候选对象列表。"""
    result = dict(payload) if isinstance(payload, dict) else {"value": payload}
    if str(kind) == "keyword":
        raw = result.get("candidates") if isinstance(result.get("candidates"), dict) else result
        buckets = {}
        for key in ("core_keywords", "entity_keywords", "anchors"):
            values = raw.get(key) if isinstance(raw, dict) else None
            if isinstance(values, (str, dict)):
                values = [values]
            items = []
            for item in values or []:
                if isinstance(item, dict):
                    term = item.get("term") or item.get("keyword") or item.get("value")
                    items.append(
                        {
                            "term": str(term or "").strip(),
                            "hits": _as_int(item.get("hits")) if item.get("hits") is not None else None,
                            "examples": [
                                _truncate(example)
                                for example in (item.get("examples") or [])[:MAX_EXAMPLES]
                            ],
                        }
                    )
                else:
                    items.append(
                        {"term": str(item or "").strip(), "hits": None, "examples": []}
                    )
            buckets[key] = [item for item in items if item["term"]]
        result["candidates"] = buckets
    return result


def _collect_source_ids(container, key: str) -> List[int]:
    """从 ``container[key]``（source_id 列表或 {source_id:...} 列表）取 source_id。"""
    ids: List[int] = []
    if not isinstance(container, dict):
        return ids
    for item in container.get(key) or []:
        source_id = _as_int(item.get("source_id")) if isinstance(item, dict) else _as_int(item)
        if source_id > 0 and source_id not in ids:
            ids.append(source_id)
    return ids


def _payload_actionable_source_ids(payload, *containers) -> List[int]:
    """被建议停用/替换的 source_id：payload 两个桶 + 证据里登记的 disable/replace ids 的**并集**。

    取并集是为了"任何一处提到的源都必须有探测证据"——只看 payload 的话，把源写进 evidence 的
    disable_source_ids 而 payload 里另写一个，就能绕过；只看 evidence 同理。
    """
    ids = _collect_source_ids(payload, "disable_sources") + _collect_source_ids(
        payload, "replace_sources"
    )
    for container in containers:
        ids.extend(_collect_source_ids(container, "disable_source_ids"))
        ids.extend(_collect_source_ids(container, "replace_source_ids"))
    return ids


def _source_probe_evidence(metrics: Dict, evidence) -> Optional[Dict]:
    """取信源探测记录：优先 ``evidence.source_probe``，其次 ``metrics.source_evidence.source_probe``。"""
    for container in (evidence, (metrics or {}).get("source_evidence")):
        if isinstance(container, dict) and isinstance(container.get("source_probe"), dict):
            return container["source_probe"]
    return None


def _source_probe_gaps(payload, probe, *extra_containers) -> List[str]:
    """信源类建议的 verified 闸门（按当前口径复算，不看调用方的"通过"标志）。

    三条硬口径（2026-10 真机事故后定）：
      ① 每条被建议停用/替换的源，都要有 ``probe_sources`` 的真实探测记录，且该记录**证明它
         不可用**（reachable=False 或 verdict ∈ {URL 失效, 需登录或反爬, 空 feed}）；记录必须带
         ``generated_by="probe_sources"``——手工伪造的探测结果一律不认；
      ② 探测显示可用（或只是"页面不可解析"）的源，绝不许出现在停用/替换里——"零产出"不能
         当停用理由；
      ③ 探测样本必须 ≥1：一个源都没探到，或探测**全部失败**（无法区分信源故障与探针网络故障）
         → 一律 rejected，理由写"未取得探测样本，无法验证"。
    """
    actionable = _payload_actionable_source_ids(payload, *extra_containers)
    if not actionable:
        # 先判"有没有可执行项"：没有停用/替换项时，连探测记录都不必看
        return ["信源类建议没有可执行项（没有探测证明不可用的停用/替换项）"]
    if probe is None:
        return [
            "缺少信源探测记录（evidence.source_probe）：信源类建议必须先跑 probe_sources 真实探测"
        ]
    gaps: List[str] = []
    generated_by = str(probe.get("generated_by") or "")
    if generated_by != SOURCE_PROBE_GENERATOR:
        gaps.append(
            f"信源探测记录来源不可信（generated_by={generated_by!r}，只认 "
            f"{SOURCE_PROBE_GENERATOR!r}）：拒绝手工伪造的探测结果"
        )
    index = _source_probe_index(probe)
    if not index:
        gaps.append("未取得探测样本，无法验证（探测记录里没有任何信源）")
    elif not any(row.get("reachable") for row in index.values()):
        gaps.append(
            "未取得探测样本，无法验证（本次探测全部失败，无法区分信源故障与探针网络故障）"
        )
    for source_id in actionable:
        row = index.get(source_id)
        if row is None:
            gaps.append(f"信源 #{source_id} 缺少真实探测记录，无法验证（探测覆盖不全）")
            continue
        if _source_probe_proves_bad(row):
            continue
        gaps.append(
            f"信源 #{source_id} 的探测结论为「{row.get('verdict') or '未知'}」"
            f"（HTTP {row.get('http_status')}，解析出 {_as_int(row.get('parsed_items'))} 条），"
            "不构成停用/替换依据（只认 reachable=false 或 verdict ∈ "
            "{URL 失效, 需登录或反爬, 空 feed}）"
        )
    return gaps


def _verified_justified(kind: str, metrics: Dict, evidence=None, payload=None) -> Tuple[bool, str]:
    """复算"自测达标"结论：不接受外部传入的"通过"标志，一律按当前阈值重算。

    两道闸门：
      1. metrics 必须带 ``self_test`` 来源标记（只有 ``run_self_test_and_stage`` 会写）；
      2. 关键词建议：影子重跑 + 抓文章实测都按**当前**阈值复算通过；
         信源建议：**先探测、后 verified**——每条停用/替换项都要有 probe_sources 的实测证据，
         零产出等机械规则不算理由（见 ``_source_probe_gaps``）。
    """
    gaps: List[str] = []
    marker = metrics.get("self_test") if isinstance(metrics.get("self_test"), dict) else {}
    if marker.get("source") != SELF_TEST_SOURCE or not marker.get("passed"):
        gaps.append("缺少自测来源标记（verified 只能由自测判定产生，不接受外部传入的通过标志）")
    if str(kind) == "source":
        source_evidence = (
            metrics.get("source_evidence")
            if isinstance(metrics.get("source_evidence"), dict)
            else {}
        )
        gaps.extend(
            _source_probe_gaps(
                payload,
                _source_probe_evidence(metrics, evidence),
                source_evidence,
                evidence if isinstance(evidence, dict) else {},
            )
        )
        return (not gaps), "；".join(gaps)

    before, after = metrics.get("before") or {}, metrics.get("after") or {}
    admit_before = _as_float(before.get("admit_rate"))
    admit_after = _as_float(after.get("admit_rate"))
    if admit_before is None or admit_after is None:
        gaps.append("缺少影子自测准入率指标")
    else:
        gain = round(admit_after - admit_before, 4)
        if gain < MIN_ADMIT_RATE_GAIN_PCT:
            gaps.append(f"准入率提升 {gain} 个百分点 < {MIN_ADMIT_RATE_GAIN_PCT}")
        if admit_after < MIN_ADMIT_RATE_AFTER_PCT:
            gaps.append(f"改后准入率 {admit_after}% < {MIN_ADMIT_RATE_AFTER_PCT}%")
    fp_before = _as_float(before.get("false_positive"))
    fp_after = _as_float(after.get("false_positive"))
    if fp_before is None or fp_after is None:
        gaps.append("缺少误准入指标")
    elif round(fp_after - fp_before, 4) > MAX_FALSE_POSITIVE_GAIN_PCT:
        gaps.append(
            f"误准入率上升 {round(fp_after - fp_before, 4)} 个百分点 > {MAX_FALSE_POSITIVE_GAIN_PCT}"
        )
    probe_verdict = _crawl_probe_verdict(metrics.get("crawl_probe"))
    if not probe_verdict["passed"]:
        gaps.append(f"抓文章实测未达标：{probe_verdict['reason']}")
    if gaps:
        return False, "；".join(gaps)
    return True, ""


def _serialize_suggestion(row: Dict) -> Dict:
    suggestion_id = _as_int(row.get("id"))
    pack_id = str(row.get("industry_pack_id") or "")
    return {
        "suggestion_id": suggestion_id,
        "id": suggestion_id,
        "industry_pack_id": pack_id,
        "pack_id": pack_id,
        "kind": str(row.get("kind") or ""),
        "status": str(row.get("status") or ""),
        "reason": str(row.get("reason") or ""),
        "payload": _json_loads(row.get("payload_json"), {}),
        "metrics": _json_loads(row.get("metrics_json"), {}),
        "evidence": _json_loads(row.get("evidence_json"), {}),
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
        "applied_at": str(row.get("applied_at") or "") or None,
        "activation_id": str(row.get("activation_id") or ""),
        "after_apply": _json_loads(row.get("after_apply_json"), {}),
    }


_METRIC_SLIM_KEYS = (
    "source_id",
    "source_name",
    "fetched",
    "parse_failed",
    "parsed",
    "listing_status",
    "listing_error",
    "admit_rate_before",
    "admit_rate_after",
)


def _slim_probe(probe) -> Optional[Dict]:
    """抓取明细留给 metrics 里一份精简版（正文/条目明细放 evidence，避免 metrics 过大）。"""
    if not isinstance(probe, dict):
        return None
    return {
        "pack_id": str(probe.get("pack_id") or ""),
        "probed_at": str(probe.get("probed_at") or ""),
        "duration_seconds": _as_float(probe.get("duration_seconds")),
        "parsed_total": _as_int(probe.get("parsed_total")),
        "fetched_total": _as_int(probe.get("fetched_total")),
        "parse_failed_total": _as_int(probe.get("parse_failed_total")),
        "sources_succeeded": _as_int(probe.get("sources_succeeded")),
        "sample_available": bool(probe.get("sample_available")),
        "new_article_admit_rate_before": _as_float(probe.get("new_article_admit_rate_before")),
        "new_article_admit_rate_after": _as_float(probe.get("new_article_admit_rate_after")),
        "sources": [
            {key: source.get(key) for key in _METRIC_SLIM_KEYS}
            for source in (probe.get("sources") or [])
        ],
        "errors": list(probe.get("errors") or []),
    }


def stage_suggestion(
    pack_id: str,
    kind: str,
    payload,
    metrics,
    *,
    status: str,
    evidence=None,
    reason: str = "",
) -> Dict:
    """把一条改进建议写进 ``intel_pack_improvements``（自测达标的才允许 verified）。

    ``status`` 只接受 ``verified/rejected/applied/failed``；收到 ``verified`` 时**不接受**
    调用方的"通过"标志——按当前阈值复算 ``metrics``，复算不过一律降级为 ``rejected``
    并把差距写进 ``reason``（防止把没自测的建议放上【改进】页）。
    """
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in ("verified", "rejected", "applied", "failed"):
        raise ValueError(
            f"status 只允许 verified/rejected/applied/failed，收到: {status!r}"
        )
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind not in SUGGESTION_KINDS:
        raise ValueError(f"kind 只允许 {SUGGESTION_KINDS}，收到: {kind!r}")
    pack_id = str(pack_id or "").strip()
    if not pack_id:
        raise ValueError("pack_id 不能为空")
    _load_pack(pack_id)  # 包不存在直接失败，避免留下无法处理的孤儿建议

    normalized_metrics = _normalize_metrics(metrics)
    normalized_payload = _normalize_payload(normalized_kind, payload)
    normalized_evidence = evidence if isinstance(evidence, dict) else {}
    final_reason = str(reason or "").strip()
    if normalized_status == "verified":
        justified, gaps = _verified_justified(
            normalized_kind, normalized_metrics, normalized_evidence, normalized_payload
        )
        if not justified:
            normalized_status = "rejected"
            final_reason = (
                f"{final_reason}；判定为 verified 但按当前阈值复算未达标：{gaps}"
            ).lstrip("；")

    _ensure_schema()
    suggestion_id = _execute(
        """
        INSERT INTO intel_pack_improvements
            (industry_pack_id, kind, payload_json, metrics_json, evidence_json,
             status, reason, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pack_id,
            normalized_kind,
            _json_dumps(normalized_payload),
            _json_dumps(normalized_metrics),
            _json_dumps(normalized_evidence),
            normalized_status,
            final_reason,
            utc_text(),
            utc_text(),
        ),
    )
    return get_suggestion(suggestion_id) or {
        "suggestion_id": suggestion_id,
        "industry_pack_id": pack_id,
        "kind": normalized_kind,
        "payload": normalized_payload,
        "metrics": normalized_metrics,
        "evidence": normalized_evidence,
        "status": normalized_status,
        "reason": final_reason,
    }


def list_suggestions(pack_id: str, *, status: str = "") -> List[Dict]:
    """列出某包的建议（默认全部；``status`` 可过滤，未知状态报错而不是静默返回空）。"""
    pack_id = str(pack_id or "").strip()
    if not pack_id:
        raise ValueError("pack_id 不能为空")
    normalized_status = str(status or "").strip().lower()
    if normalized_status in ("all", "*"):
        normalized_status = ""
    if normalized_status and normalized_status not in SUGGESTION_STATUSES:
        raise ValueError(
            f"status 只允许 {SUGGESTION_STATUSES} 之一（或空），收到: {status!r}"
        )
    _ensure_schema()
    conditions = ["industry_pack_id = ?"]
    params: List = [pack_id]
    if normalized_status:
        conditions.append("status = ?")
        params.append(normalized_status)
    rows = _query(
        f"""
        SELECT * FROM intel_pack_improvements
        WHERE {' AND '.join(conditions)}
        ORDER BY created_at DESC, id DESC
        """,
        params,
    )
    return [_serialize_suggestion(row) for row in rows]


def get_suggestion(suggestion_id) -> Optional[Dict]:
    """取一条建议（不存在返回 None）。"""
    _ensure_schema()
    rows = _query(
        "SELECT * FROM intel_pack_improvements WHERE id = ?",
        (_as_int(suggestion_id),),
    )
    return _serialize_suggestion(rows[0]) if rows else None


def _touch_suggestion(suggestion_id, sql: str, params: Sequence) -> Dict:
    _execute(sql, (*params, _as_int(suggestion_id)))
    record = get_suggestion(suggestion_id)
    if record is None:
        raise ValueError(f"suggestion not found: {suggestion_id}")
    return record


def mark_applied(suggestion_id, *, activation_result=None) -> Dict:
    """人工同意并已发布激活 → 置 ``applied``，记录 activation_id 与激活结果。

    注意：本模块**不**执行激活。调用方必须先跑
    ``industry_pack_activation.IndustryPackActivationService.activate(...)``，
    再把它的返回传进来。
    """
    result = activation_result if isinstance(activation_result, dict) else {}
    activation_id = str(
        result.get("activation_id") or result.get("id") or result.get("activationId") or ""
    )
    record = get_suggestion(suggestion_id)
    if record is None:
        raise ValueError(f"suggestion not found: {suggestion_id}")
    after_apply = dict(record.get("after_apply") or {})
    after_apply["activation_result"] = result
    now = utc_text()
    return _touch_suggestion(
        suggestion_id,
        """
        UPDATE intel_pack_improvements
        SET status = 'applied', applied_at = ?, updated_at = ?, activation_id = ?,
            after_apply_json = ?
        WHERE id = ?
        """,
        (now, now, activation_id, _json_dumps(after_apply)),
    )


def mark_rejected(suggestion_id, *, reason: str = "") -> Dict:
    """人工拒绝（或不采纳）→ 置 ``rejected`` 并写清原因。

    只允许写 rejected/applied/failed 三种终态里的 rejected；
    **不允许**通过本接口把建议置为 verified（verified 只能由自测判定产生）。
    """
    record = get_suggestion(suggestion_id)
    if record is None:
        raise ValueError(f"suggestion not found: {suggestion_id}")
    reason_text = str(reason or "").strip() or "人工拒绝"
    if record.get("status") == "applied":
        reason_text = f"{reason_text}（该建议已 applied，拒绝状态仅作留痕）"
    return _touch_suggestion(
        suggestion_id,
        """
        UPDATE intel_pack_improvements
        SET status = 'rejected', reason = ?, updated_at = ?
        WHERE id = ?
        """,
        (reason_text, utc_text()),
    )


def record_after_apply(suggestion_id, metrics) -> Dict:
    """回填"发布激活后再跑一次真实抓取+分类复测"的结果（不改 status）。"""
    record = get_suggestion(suggestion_id)
    if record is None:
        raise ValueError(f"suggestion not found: {suggestion_id}")
    after_apply = dict(record.get("after_apply") or {})
    after_apply["after_apply"] = _normalize_metrics(metrics)
    after_apply["recorded_at"] = utc_text()
    return _touch_suggestion(
        suggestion_id,
        """
        UPDATE intel_pack_improvements
        SET after_apply_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (_json_dumps(after_apply), utc_text()),
    )


# ─────────────────────────── 生成新版本（发布链路） ───────────────────────────
def _bump_pack_version(value) -> str:
    raw = str(value or "").strip()
    match = _PACK_VERSION_RE.match(raw)
    if match:
        return f"{match.group(1)}.{match.group(2)}.{int(match.group(3)) + 1}"
    return f"{raw}.1" if raw else "1.0.1"


def _manifest_signature(manifest) -> str:
    """草稿/已发布 manifest 的比较指纹（用于识别"有没有人工未发布的改动"）。"""
    if not isinstance(manifest, dict):
        return ""
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, default=str)


def _merged_terms_summary(before: Dict, after: Dict) -> Dict:
    """候选词合并前后各词表的增量与总量（returned 给 API/UI 做变更预览）。"""
    before_gate = before.get("candidate_gate") if isinstance(before.get("candidate_gate"), dict) else {}
    after_gate = after.get("candidate_gate") if isinstance(after.get("candidate_gate"), dict) else {}
    summary = {}
    for field, old_values, new_values in (
        ("core_keywords", before.get("core_keywords"), after.get("core_keywords")),
        ("anchor_keywords", before_gate.get("anchor_keywords"), after_gate.get("anchor_keywords")),
        ("entity_keywords", before_gate.get("entity_keywords"), after_gate.get("entity_keywords")),
    ):
        old_list = [str(item) for item in (old_values or [])]
        new_list = [str(item) for item in (new_values or [])]
        summary[field] = {
            "added": [item for item in new_list if item not in old_list],
            "total_before": len(old_list),
            "total_after": len(new_list),
        }
    return summary


def _default_admin_service():
    """复用仓库既有的行业包 admin 服务（草稿 → 保存 → 发布），不自己写版本表 SQL。

    优先用当前模块绑定的数据库/加载器装配（保证与 ``use_database`` 注入的库一致），
    装不起来时才退到 ``intel_api`` 里已装配好的单例。
    """
    try:
        from industry_pack_admin import IndustryPackAdminService, IndustryPackVersionStore

        return IndustryPackAdminService(
            IndustryPackVersionStore(_repository.db), _pack_loader
        )
    except Exception:
        from intel_api import industry_pack_admin_service

        return industry_pack_admin_service


def _default_activation_service():
    """激活服务（这里只用它的只读 ``preview`` 取 plan_sha256，绝不调用 activate）。"""
    from industry_pack_activation import IndustryPackActivationService
    from industry_pack_admin import IndustryPackVersionStore

    return IndustryPackActivationService(
        _repository.db,
        version_store=IndustryPackVersionStore(_repository.db),
        repository=_repository,
    )


def _attach_preview(result: Dict, pack_id: str, preview_service=None, *, target_version_id=None) -> Dict:
    """把激活服务的**只读** preview 结果附到返回值上（拿 plan_sha256 供两阶段确认）。

    preview 不可用时写 ``preview_error``（键一定存在），绝不静默假装成功。
    """
    result.setdefault("preview_error", None)
    try:
        service = preview_service or _default_activation_service()
        preview = service.preview(
            pack_id,
            target_version_id=target_version_id or result.get("target_version_id"),
        )
        result["plan_sha256"] = str(preview.get("plan_sha256") or "") or None
        result["source_summary"] = {
            "changed": False,
            "source_counts": preview.get("source_counts"),
            "source_plan_sha256": str(preview.get("source_plan_sha256") or ""),
            "note": "关键词类建议不改 default_sources；信源对账计划取自激活服务 preview",
        }
    except Exception as exc:
        result["preview_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        result.setdefault(
            "source_summary",
            {
                "changed": False,
                "source_counts": None,
                "source_plan_sha256": "",
                "note": "preview 不可用，未能取得信源对账计划",
            },
        )
    return result


def prepare_pack_version(
    pack_id: str,
    suggestion_id,
    *,
    admin_service=None,
    preview_service=None,
    actor: str = PREPARE_ACTOR,
) -> Dict:
    """把一条 **verified** 的关键词建议落成一个**新版本**（草稿→保存→发布），返回目标版本 id。

    为什么需要它：【同意并发布】如果只调 activation，激活的是**线上那一版旧配置**——
    界面显示"发布成功"但候选词根本没生效（等于空操作）。所以人工同意后必须先由本函数
    生成包含候选词的新版本，API 层再拿 ``target_version_id`` 去
    ``activation.preview(pack_id, target_version_id=...)`` → ``activate(...)``。

    安全边界（硬）：
      * 只接受 ``status="verified"`` 的建议——未通过自测的候选**绝不**写进版本；
      * 只处理 ``kind="keyword"``；信源类建议涉及 default_sources 增删，必须人工在行业包
        管理页调整（本函数不会替人改信源）；
      * 若该包存在**未发布的草稿**（可能是人工编辑），拒绝覆盖并报错；
      * 已发布版本里已包含这些候选词时走幂等分支（复用原版本，不重复发布）；
      * admin 服务不可用时返回 ``{"error": ...}``，绝不静默降级成"激活旧版本"。

    只读副作用说明：函数内部会调用 activation 的 ``preview``（纯读，不写库）取 plan_sha256。
    """
    normalized_id = str(pack_id or "").strip()
    result: Dict = {
        "pack_id": normalized_id,
        "suggestion_id": _as_int(suggestion_id),
        "target_version_id": None,
        "pack_version": "",
        "plan_sha256": None,
        "merged_terms": {},
        "source_summary": {},
        "already_prepared": False,
        "preview_error": None,
        "error": None,
    }
    record = get_suggestion(suggestion_id)
    if record is None:
        result["error"] = f"改进建议不存在：{suggestion_id}"
        return result
    result["suggestion_id"] = int(record["suggestion_id"])
    if str(record.get("status")) != "verified":
        result["error"] = (
            f"只接受 status=verified 的建议（当前 {record.get('status')}）："
            "未通过自测的候选词不允许写进行业包版本"
        )
        return result
    if str(record.get("kind")) != "keyword":
        result["error"] = (
            f"只支持 keyword 类建议（当前 {record.get('kind')}）：信源类建议需要在行业包管理页"
            "人工调整 default_sources 后自行发布"
        )
        return result
    buckets = _normalize_candidate_input((record.get("payload") or {}).get("candidates"))
    if not _has_candidates(buckets):
        result["error"] = "建议 payload 里没有候选词，无法生成新版本"
        return result

    published_pack = _load_pack(normalized_id)
    merged = _merge_candidates_into_pack(published_pack, buckets)
    merged_terms = _merged_terms_summary(published_pack, merged)
    result["merged_terms"] = merged_terms
    added_total = sum(len(item["added"]) for item in merged_terms.values())

    try:
        service = admin_service or _default_admin_service()
    except Exception as exc:
        result["error"] = (
            f"行业包 admin 服务不可用（{type(exc).__name__}: {str(exc)[:160]}），"
            "无法生成新版本；请检查 industry_pack_admin 装配"
        )
        return result

    try:
        latest = service.store.latest_published(normalized_id)
    except Exception as exc:
        result["error"] = f"读取已发布版本失败：{type(exc).__name__}: {str(exc)[:160]}"
        return result

    if added_total == 0 and latest:
        # 幂等：已发布版本里已经有这些候选词（重复点击【同意并发布】）→ 复用原版本
        result["target_version_id"] = int(latest["id"])
        result["pack_version"] = str((latest.get("manifest") or {}).get("pack_version") or "")
        result["already_prepared"] = True
        return _attach_preview(
            result, normalized_id, preview_service, target_version_id=int(latest["id"])
        )

    try:
        draft = service.get_or_create_draft(normalized_id, actor=actor)
    except Exception as exc:
        result["error"] = f"创建/读取行业包草稿失败：{type(exc).__name__}: {str(exc)[:160]}"
        return result
    if latest and _manifest_signature(draft.get("manifest")) != _manifest_signature(
        latest.get("manifest")
    ):
        result["error"] = (
            "该行业包已有未发布的草稿内容（可能来自人工编辑），为避免覆盖已拒绝本次生成；"
            "请先在行业包管理页发布或丢弃草稿后重试"
        )
        return result

    base_version_text = str(
        ((latest or {}).get("manifest") or {}).get("pack_version")
        or published_pack.get("pack_version")
        or ""
    )
    merged["pack_version"] = _bump_pack_version(base_version_text)
    try:
        saved = service.save_draft(
            normalized_id, merged, expected_revision=int(draft["revision"]), actor=actor
        )
        version = service.publish_draft(
            normalized_id, expected_revision=int(saved["revision"]), actor=actor
        )
    except Exception as exc:
        result["error"] = f"生成新版本失败：{type(exc).__name__}: {str(exc)[:200]}"
        return result

    result["target_version_id"] = int(version["id"])
    result["pack_version"] = str(
        version.get("pack_version") or merged.get("pack_version") or ""
    )
    return _attach_preview(
        result, normalized_id, preview_service, target_version_id=int(version["id"])
    )


# ─────────────────────────── 信源类建议 ───────────────────────────
def _source_probe_text(row: Dict) -> str:
    """把一条探测记录写成人话（进 disable/replace/keep 的 reason，人工审核就看它）。"""
    verdict = str(row.get("verdict") or "未知")
    status = row.get("http_status")
    parsed = _as_int(row.get("parsed_items"))
    error = str(row.get("error") or "").strip()
    text = f"本次真实探测：{verdict}"
    text += f"（HTTP {status}）" if status is not None else "（未取得 HTTP 响应）"
    text += f"，解析出 {parsed} 条"
    if error:
        text += f"；错误原文：{error[:160]}"
    return text


def _build_source_suggestion(assessment: Dict, probe, source_probe=None) -> Dict:
    """按信源 verdict + 真实探测（``probe_sources``）证据，生成信源类改进建议。

    硬口径（2026-10 真机事故后改写，勿放宽）：
      * **"零产出 → 建议停用"这条机械规则已删除**：零产出本身不是停用理由，探测显示可用的
        零产出源只进 ``keep_sources`` + ``notes``（写明"零产出，需先确认调度/映射是否正常"）；
      * 只有 ``probe_sources`` 的逐源探测**证明该源不可用**（reachable=False 或 verdict ∈
        {URL 失效, 需登录或反爬, 空 feed}）才进 disable/replace；探测记录随 evidence 一起落库，
        闸门（``_source_probe_gaps``）会逐条复核；
      * 抓文章探针（crawl_probe）失败、或该源没有探测记录的，一律进 ``unverified_sources``
        （"无法确认该源状态"），绝不进停用建议——抓不到不等于坏。
    """
    sources = list(assessment.get("sources") or [])
    crawl_rows = {}
    for row in (probe or {}).get("sources") or []:
        crawl_rows[_as_int(row.get("source_id"))] = row
    crawl_errors = {}
    for item in (probe or {}).get("errors") or []:
        crawl_errors.setdefault(_as_int(item.get("source_id")), []).append(
            {
                "stage": str(item.get("stage") or ""),
                "error": str(item.get("error") or "")[:300],
            }
        )
    probe_index = _source_probe_index(source_probe)

    disable: List[Dict] = []
    replace: List[Dict] = []
    keep: List[Dict] = []
    unverified: List[Dict] = []
    zero_output_ok: List[Dict] = []
    for row in sources:
        source_id = _as_int(row.get("source_id"))
        crawl_row = crawl_rows.get(source_id) or {}
        probe_row = probe_index.get(source_id) or {}
        listing_status = str(
            crawl_row.get("listing_status") or ("not_probed" if not crawl_row else "")
        )
        item = {
            "source_id": source_id,
            "source_name": str(row.get("source_name") or ""),
            "source_url": str(row.get("source_url") or ""),
            "verdict": str(row.get("verdict") or ""),
            "article_count": _as_int(row.get("article_count")),
            "admitted_count": _as_int(row.get("admitted_count")),
            "admitted_pct": _as_float(row.get("admitted_pct")),
            "last_article_at": str(row.get("last_article_at") or ""),
            # 抓文章探针（crawl_probe）的口径
            "probe_status": listing_status or "not_probed",
            "fetched": _as_int(crawl_row.get("fetched")),
            "parse_failed": _as_int(crawl_row.get("parse_failed")),
            "errors": crawl_errors.get(source_id, []),
            # 信源实测探针（probe_sources）的口径：停用/替换的唯一依据
            "source_probe": dict(probe_row) if probe_row else None,
            "reason": "",
        }
        proves_bad = _source_probe_proves_bad(probe_row)
        if proves_bad:
            probe_text = _source_probe_text(probe_row)
            if item["article_count"] > 0:
                item["reason"] = (
                    f"历史入库产出 {item['article_count']} 篇，最近一篇 "
                    f"{item['last_article_at'] or '未知'}；{probe_text} → "
                    "建议替换为可用信源，或人工确认站点是否改版/换域名"
                )
                replace.append(item)
            else:
                item["reason"] = (
                    f"启用中但历史入库产出 0 篇；{probe_text} → 建议停用或替换为可用信源"
                )
                disable.append(item)
        elif not probe_row:
            item["reason"] = (
                "本次未做信源探测（缺少探测记录），无法确认该源状态，"
                "不纳入停用建议，需人工复核"
            )
            unverified.append(item)
        elif str(listing_status) == "failed":
            item["reason"] = (
                f"{_source_probe_text(probe_row)}；但本次抓文章探针抓取失败"
                f"（{item['probe_status']}），仍无法确认该源状态，不纳入停用建议，需人工复核"
            )
            unverified.append(item)
        elif item["verdict"] == SOURCE_VERDICT_DISABLE:
            # 零产出但探测未证明不可用：不许停用，只能保留 + 提示先查调度/映射
            item["reason"] = (
                f"启用中但历史入库产出 0 篇；{_source_probe_text(probe_row)}，"
                "探测未证明该源不可用，零产出不能作为停用理由——零产出，需先确认调度/映射是否正常"
            )
            zero_output_ok.append(item)
            keep.append(item)
        elif item["verdict"] == SOURCE_VERDICT_KEEP:
            item["reason"] = (
                f"产出正常（{item['article_count']} 篇）但准入率仅 {item['admitted_pct']}%；"
                "属关键词覆盖不足，建议随关键词改进观察，不要当坏源停用"
            )
            keep.append(item)
        elif item["verdict"] == SOURCE_VERDICT_REPLACE:
            # 停更但探测可用：站点没坏，属停更/调度问题，不能拿"长期无新文"当坏源停用或替换
            item["reason"] = (
                f"最近一篇 {item['last_article_at'] or '未知'}，已超过 {SOURCE_VERDICT_IDLE_DAYS} "
                f"天无新文；{_source_probe_text(probe_row)} → 站点本身可用，"
                "建议人工确认改版/停更后再决定是否替换"
            )
            keep.append(item)
        else:
            item["reason"] = "产出与准入率正常，保持启用"
            keep.append(item)

    actionable = len(disable) + len(replace)
    passed = actionable >= 1
    disabled_zero = [item for item in disable if item["verdict"] == SOURCE_VERDICT_DISABLE]
    unverified_note = (
        f"{len(unverified)} 个源本次抓取失败或缺探测记录，无法确认该源状态"
    )
    if passed:
        parts = []
        if disabled_zero:
            parts.append(
                f"建议停用 {len(disabled_zero)} 个零产出源（均已由真实探测证明不可用）"
            )
        if len(disable) - len(disabled_zero):
            parts.append(f"另有 {len(disable) - len(disabled_zero)} 个探测证明不可用的源建议停用")
        parts.append(f"替换 {len(replace)} 个探测证明不可用但有历史产出的源")
        # 不说"正常源"：keep 里既有正常的，也有"探测未证明不可用但结论存疑"的（如页面不可解析）
        parts.append(f"保留 {len(keep)} 个源（探测未证明不可用）")
        reason = "信源体检：" + "、".join(parts)
        if zero_output_ok:
            reason += (
                f"；其中 {len(zero_output_ok)} 个零产出源本次探测可用，"
                "零产出不作为停用理由（需先确认调度/映射是否正常）"
            )
        if unverified:
            reason += f"；另有 {unverified_note}（已单列 unverified_sources，未纳入停用建议）"
    else:
        reason = (
            "信源体检：没有可执行的信源调整项（没有探测证明不可用的信源），"
            "不生成可发布的信源建议"
        )
        if unverified:
            reason += f"；{unverified_note}"

    probe_parsed = _as_int((probe or {}).get("parsed_total"))
    probe_failed = _as_int((probe or {}).get("parse_failed_total"))
    source_probe = source_probe if isinstance(source_probe, dict) else None
    probed_ids = _source_probe_index(source_probe)
    notes = [
        "add_sources 需要人工提供具体 URL（引擎不臆造信源地址）",
        f"抓文章探针：解析成功 {probe_parsed} 篇、解析失败 {probe_failed} 篇",
        "信源停用/替换只认 probe_sources 的真实探测证据"
        "（reachable=false 或 verdict ∈ {URL 失效, 需登录或反爬, 空 feed}）；"
        "「零产出」不是停用理由。",
    ]
    if zero_output_ok:
        notes.append(
            f"{len(zero_output_ok)} 个零产出源本次探测可用（HTTP 200 且能解析出条目）："
            "零产出，需先确认调度/映射是否正常，不要停用"
        )
    if not probed_ids:
        notes.append("本次没有信源探测记录（probe_sources 未执行或执行失败）：所有源都不构成停用依据")
    else:
        notes.append(
            f"信源实测探针：探测 {len(probed_ids)} 个源，"
            f"结论分布 {dict((source_probe or {}).get('verdict_counts') or {})}"
        )
    payload = {
        "disable_sources": disable,
        "replace_sources": replace,
        "add_sources": [],
        "keep_sources": keep,
        "unverified_sources": unverified,
        "notes": notes,
    }
    other_pct = _as_float(assessment.get("other_pct"))
    before_admit = None if other_pct is None else round(100.0 - other_pct, 4)
    evidence = {
        "source_verdict_counts": assessment.get("source_verdict_counts"),
        "actionable_count": actionable,
        "unverified_count": len(unverified),
        "disable_source_ids": [item["source_id"] for item in disable],
        "replace_source_ids": [item["source_id"] for item in replace],
        "keep_source_ids": [item["source_id"] for item in keep],
        "probe_errors": (probe or {}).get("errors") or [],
        # 闸门的唯一依据：逐源真实探测记录（带 generated_by="probe_sources"）
        "source_probe": source_probe,
        "source_probe_verdict_counts": dict((source_probe or {}).get("verdict_counts") or {}),
    }
    metrics = _normalize_metrics(
        {
            # 整包口径：改前 = 当前准入率（100 - other%）；改后只能等真实复测回填 → null
            "before": {
                "admit_rate": before_admit,
                "other_pct": other_pct,
                "false_positive": None,
                "topic_assoc": None,
            },
            "after": {
                "admit_rate": None,
                "other_pct": None,
                "false_positive": None,
                "topic_assoc": None,
            },
            "crawl_probe": _slim_probe(probe),
            "source_probe": _slim_source_probe(source_probe),
            "source_evidence": evidence,
            # 信源类建议不跑影子/抓文章双达标，用自己的证据口径 + 来源标记
            "self_test": {
                "source": SELF_TEST_SOURCE,
                "kind": "source",
                "pack_id": str(assessment.get("pack_id") or ""),
                "ran_at": utc_text(),
                "passed": bool(passed),
                "reason": reason,
            },
        }
    )
    return {
        "payload": payload,
        "metrics": metrics,
        "evidence": evidence,
        "passed": passed,
        "reason": reason,
    }


# ─────────────────────────── 交付物 1.8：串起来跑 ───────────────────────────
def run_self_test_and_stage(
    pack_id: str,
    *,
    sample_limit: int = 400,
    top_n: int = DEFAULT_TOP_N,
    probe_runner=None,
    per_source: int = CRAWL_PROBE_PER_SOURCE,
    probe_timeout_seconds: float = CRAWL_PROBE_TIMEOUT_SECONDS,
    probe_max_sources: int = CRAWL_PROBE_MAX_SOURCES,
    source_probe_runner=None,
    source_probe_limit: int = SOURCE_PROBE_MAX_SOURCES,
    source_probe_timeout_seconds: float = SOURCE_PROBE_TIMEOUT_SECONDS,
) -> Dict:
    """评估 → 挖候选 → 影子自测 → 抓文章实测 → **双达标才 verified**，否则 rejected。

    ``probe_runner`` 需满足
    ``(pack_id, candidates, *, per_source, timeout_seconds, max_sources) -> dict`` 签名，
    默认用 ``crawl_probe``（真联网）；测试传入桩函数即可完全离线。

    信源类建议另跑一道**信源实测探针**：``source_probe_runner`` 需满足
    ``(pack_id, *, limit, timeout_seconds) -> dict`` 签名，默认用 ``probe_sources``（真联网）；
    测试同样可以打桩（信源建议的 verified 依据就是它，缺了必然 rejected）。
    """
    pack = _load_pack(pack_id)
    assessment = assess_pack(pack_id, sample_limit=sample_limit)
    candidates = assessment.get("candidates") or {
        "core_keywords": [],
        "entity_keywords": [],
        "anchors": [],
    }
    candidate_terms = list(assessment.get("candidate_keywords") or [])

    # 抓文章实测：即使没有关键词候选也要跑——信源类建议需要它的逐源 fetched/parse_failed/errors。
    runner = probe_runner or crawl_probe
    shadow: Optional[Dict] = None
    try:
        probe = runner(
            pack_id,
            candidates,
            per_source=per_source,
            timeout_seconds=probe_timeout_seconds,
            max_sources=probe_max_sources,
        )
    except Exception as exc:
        probe = {
            "pack_id": str(pack_id),
            "probed_at": utc_text(),
            "sources": [],
            "parsed_total": 0,
            "fetched_total": 0,
            "parse_failed_total": 0,
            "new_article_admit_rate_before": None,
            "new_article_admit_rate_after": None,
            "sample_available": False,
            "errors": [{"stage": "probe_runner", "error": _probe_error_text(exc)}],
        }
    if not isinstance(probe, dict):
        probe = {"pack_id": str(pack_id), "sources": [], "parsed_total": 0, "errors": []}

    if not candidate_terms:
        metrics = _normalize_metrics(
            {
                "before": {},
                "after": {},
                "crawl_probe": _slim_probe(probe),
                "shadow_test": None,
            }
        )
        reason = (
            "未挖到可用候选词（判别力过滤后为空）：本包 other 语料不足或候选词与其它包语料无区分度，"
            "无法生成关键词改进建议"
        )
        record = stage_suggestion(
            pack_id,
            "keyword",
            {"candidates": candidates, "pack_version": assessment.get("pack_version")},
            metrics,
            status="rejected",
            evidence={"assessment": _assessment_summary(assessment), "crawl_probe": probe},
            reason=reason,
        )
        result = _self_test_result(record, assessment, None, probe, reason)
        return _stage_source_suggestion(
            result,
            pack_id,
            assessment,
            probe,
            source_probe_runner=source_probe_runner,
            source_probe_limit=source_probe_limit,
            source_probe_timeout_seconds=source_probe_timeout_seconds,
        )

    shadow = shadow_test(pack_id, candidates, sample_limit=sample_limit)
    verdict = _evaluate_self_test(shadow, probe)
    metrics = _normalize_metrics(
        {
            "before": shadow.get("before"),
            "after": shadow.get("after"),
            "delta": shadow.get("delta"),
            "shadow_test": shadow,
            "crawl_probe": _slim_probe(probe),
            "checks": {
                "shadow": verdict["shadow"]["checks"],
                "crawl_probe": verdict["crawl_probe"]["checks"],
            },
            # 自测来源标记：stage_suggestion 只在"复算通过 + 带这个标记"时才接受 verified。
            "self_test": {
                "source": SELF_TEST_SOURCE,
                "kind": "keyword",
                "pack_id": str(pack_id),
                "ran_at": utc_text(),
                "passed": bool(verdict["passed"]),
                "reason": verdict["reason"],
            },
        }
    )
    evidence = {
        "assessment": _assessment_summary(assessment),
        "candidates": candidates,
        "gate_failures": assessment.get("gate_failures"),
        "sources": (assessment.get("sources") or [])[:20],
        "shadow_examples": (shadow or {}).get("examples"),
        "recomputable": (shadow or {}).get("recomputable"),
        "crawl_probe": probe,
    }
    record = stage_suggestion(
        pack_id,
        "keyword",
        {
            "candidates": candidates,
            "pack_version": assessment.get("pack_version"),
            "merge_targets": ["core_keywords", "candidate_gate.entity_keywords", "candidate_gate.anchor_keywords"],
            "candidate_terms": candidate_terms,
        },
        metrics,
        status="verified" if verdict["passed"] else "rejected",
        evidence=evidence,
        reason=verdict["reason"],
    )
    result = _self_test_result(record, assessment, shadow, probe, verdict["reason"], verdict)
    return _stage_source_suggestion(
        result,
        pack_id,
        assessment,
        probe,
        source_probe_runner=source_probe_runner,
        source_probe_limit=source_probe_limit,
        source_probe_timeout_seconds=source_probe_timeout_seconds,
    )


def _stage_source_suggestion(
    result: Dict,
    pack_id: str,
    assessment: Dict,
    probe,
    *,
    source_probe_runner=None,
    source_probe_limit: int = SOURCE_PROBE_MAX_SOURCES,
    source_probe_timeout_seconds: float = SOURCE_PROBE_TIMEOUT_SECONDS,
) -> Dict:
    """同时落一条信源类建议（【改进】页的"信源类建议"区块靠它出数据）。

    信源建议的 verified 依据是 ``probe_sources`` 的**真实探测**（不是"零产出"规则），所以这里
    先跑一次信源实测探针，把逐源结论随 evidence 一起落库；探针整体失败也不影响关键词建议的
    结论，只是信源建议必然被闸门降级（没有探测证据 → rejected）。
    """
    runner = source_probe_runner or probe_sources
    try:
        source_probe = runner(
            pack_id, limit=source_probe_limit, timeout_seconds=source_probe_timeout_seconds
        )
    except Exception as exc:
        source_probe = {
            "pack_id": str(pack_id),
            "generated_by": SOURCE_PROBE_GENERATOR,
            "probed_at": utc_text(),
            "sources": [],
            "sources_probed": 0,
            "sources_reachable": 0,
            "sample_available": False,
            "verdict_counts": {},
            "errors": [{"source_id": None, "error": _probe_error_text(exc)}],
        }
    if not isinstance(source_probe, dict):
        source_probe = {
            "pack_id": str(pack_id),
            "generated_by": SOURCE_PROBE_GENERATOR,
            "sources": [],
            "errors": [{"source_id": None, "error": "信源探针返回结构异常（不是对象）"}],
        }
    built = _build_source_suggestion(assessment, probe, source_probe)
    result["source_probe"] = _slim_source_probe(source_probe)
    try:
        record = stage_suggestion(
            pack_id,
            "source",
            built["payload"],
            built["metrics"],
            status="verified" if built["passed"] else "rejected",
            evidence=built["evidence"],
            reason=built["reason"],
        )
    except Exception as exc:  # 信源建议失败不能拖垮关键词建议的结论
        result["source_suggestion_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        result["source_suggestion"] = None
        result["source_suggestion_id"] = None
        return result
    result["source_suggestion"] = record
    result["source_suggestion_id"] = record.get("suggestion_id")
    result["source_suggestion_status"] = record.get("status")
    return result


def _assessment_summary(assessment: Dict) -> Dict:
    return {
        "pack_id": assessment.get("pack_id"),
        "pack_version": assessment.get("pack_version"),
        "assessed_at": assessment.get("assessed_at"),
        "articles": assessment.get("articles"),
        "categories": assessment.get("categories"),
        "other_pct": assessment.get("other_pct"),
        "peer_other_pct": assessment.get("peer_other_pct"),
        "peer_pack_count": assessment.get("peer_pack_count"),
        "source_verdict_counts": assessment.get("source_verdict_counts"),
        "gate_failures": {
            "total": (assessment.get("gate_failures") or {}).get("total"),
            "counts": (assessment.get("gate_failures") or {}).get("counts"),
        },
        "sample": assessment.get("sample"),
    }


def _self_test_result(
    record: Dict,
    assessment: Dict,
    shadow: Optional[Dict],
    probe: Optional[Dict],
    reason: str,
    verdict: Optional[Dict] = None,
) -> Dict:
    return {
        "pack_id": record.get("industry_pack_id"),
        "ran_at": utc_text(),
        "status": record.get("status"),
        "suggestion_id": record.get("suggestion_id"),
        "reason": reason,
        "checks": {
            "shadow": (verdict or {}).get("shadow"),
            "crawl_probe": (verdict or {}).get("crawl_probe"),
        },
        "assessment": _assessment_summary(assessment),
        "shadow_test": {
            "admit_rate_before": (shadow or {}).get("admit_rate_before"),
            "admit_rate_after": (shadow or {}).get("admit_rate_after"),
            "false_positive_before": (shadow or {}).get("false_positive_before"),
            "false_positive_after": (shadow or {}).get("false_positive_after"),
            "topic_assoc_before": (shadow or {}).get("topic_assoc_before"),
            "topic_assoc_after": (shadow or {}).get("topic_assoc_after"),
            "delta_other_pct": (shadow or {}).get("delta_other_pct"),
            "positive_sample": (shadow or {}).get("positive_sample"),
            "negative_sample": (shadow or {}).get("negative_sample"),
        }
        if shadow
        else None,
        "crawl_probe": _slim_probe(probe) if probe else None,
        "metrics": record.get("metrics"),
        "suggestion": record,
    }


# ─────────────────────────── 只读聚合（供 API/UI 直接展示） ───────────────────────────
def improvement_overview(pack_id: str, *, sample_limit: int = DEFAULT_SAMPLE_LIMIT) -> Dict:
    """评估卡 + 建议列表一次取回（不改任何配置，纯读）。"""
    return {
        "assessment": assess_pack(pack_id, sample_limit=sample_limit),
        "suggestions": list_suggestions(pack_id),
    }


__all__ = [
    "assess_pack",
    "mine_keyword_candidates",
    "shadow_test",
    "crawl_probe",
    "probe_sources",
    "stage_suggestion",
    "list_suggestions",
    "get_suggestion",
    "mark_applied",
    "mark_rejected",
    "record_after_apply",
    "prepare_pack_version",
    "run_self_test_and_stage",
    "improvement_overview",
    "use_database",
    "use_pack_loader",
    "MIN_ADMIT_RATE_GAIN_PCT",
    "MAX_FALSE_POSITIVE_GAIN_PCT",
    "MIN_ADMIT_RATE_AFTER_PCT",
    "MIN_CRAWL_PROBE_GAIN_PCT",
    "MIN_CRAWL_PROBE_ARTICLES",
    "DISCRIMINATION_MIN_RATIO",
    "SUGGESTION_KINDS",
    "SUGGESTION_STATUSES",
    "SELF_TEST_SOURCE",
    "SOURCE_PROBE_GENERATOR",
    "SOURCE_PROBE_MAX_SOURCES",
    "SOURCE_PROBE_TIMEOUT_SECONDS",
    "SOURCE_PROBE_BAD_VERDICTS",
    "PROBE_VERDICT_OK",
    "PROBE_VERDICT_EMPTY_FEED",
    "PROBE_VERDICT_UNPARSABLE",
    "PROBE_VERDICT_DEAD",
    "PROBE_VERDICT_BLOCKED",
    "PROBE_VERDICT_DISABLED",
]
