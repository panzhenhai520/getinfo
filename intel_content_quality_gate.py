"""Deterministic pre-admission quality checks for intelligence articles."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Dict

MIN_ARTICLE_CHARS = 150
# 分级字数标准：短行业动态下限。30~149 字的短讯允许入库（跳过 LLM），
# 低于 30 字或无实质句子的仍拒绝——字数不能挡住真正的行业动态，也不放行壳页。
MIN_SHORT_DYNAMIC_CHARS = 30
_MARKDOWN_LINK = re.compile(r"!?\[[^\]]{0,500}\]\((https?://[^)\s]+)\)", re.I)
_RAW_URL = re.compile(r"https?://[^\s)]+", re.I)

# ============================================================
# 栏目/首页/列表页 URL 形态与"刊头式聚合索引"标题
# ============================================================
# 为什么加这两条：A 机 invest_mgmt 976 行实测，有 98 行（10.0%）根本不是文章详情页，
# 但正文长度（88~3815 字）足以穿过"最小字数"与"段落/句子"两道检查，于是被当文章入库：
#   1) 导航样板页/栏目页/首页（22 行）：形如 `.../index.html`、目录页（path 以 / 结尾）、
#      首页（path 为空）、`/album/`（专题策划）、`/zt/`（专题）、`?tag=` / `?keyword=`（标签/列表）、
#      `A200102index_1.htm`（CMS 栏目模板）、`/news/c2`（移动频道页）。它们有正文，
#      但 URL 形态在结构上永远不可能是单篇文章详情页。
#   2) 机构公告/定期披露（22 行）：中基协"注销失联私募基金管理人登记公告"栏目、
#      中国银行"企业年金集合计划季度管理情况"栏目、公共资源交易平台"中标结果公示"栏目。
#      这三类只能用「域名 + 栏目路径」分开：同一个域名下，
#      `news.pedaily.cn/202609/569704.shtml`（郑州战新母基金遴选结果公示，真行业事件）
#      与 `news.pedaily.cn/202609/568760.shtml`（徐州母基金遴选结果公示）URL 模板完全相同，
#      按"公告/公示/遴选"关键词拦必然连真事件一起压掉。因此排除表只按栏目路径配置。
#   3) 媒体聚合快讯（54 行）：`投资界24h | A；B；C`、`投资界AI周报| …`、`投资界LP周报 | …`、
#      `投资界募资月报 | …`——标题本身就是"多条并列快讯的总目录"，正文只落到其中一条。
#
# 判据全部是纯函数、可复算、不联网；开关见 config.INTEL_REJECT_URL_SHAPE /
# INTEL_REJECT_PERIOD_DIGEST，排除表见 config.INTEL_URL_DENY_PATH_PATTERNS。
_DEFAULT_DENY_PATH_PATTERNS = (
    # 中基协：自律管理→私募基金→注销公告 栏目（"注销第 N 批失联私募基金管理人登记公告"）
    "amac.org.cn/zlgl/sljg",
    # 中基协：新闻发布→通知公告 栏目（培训通知 / 信用信息报告发布通知）
    "amac.org.cn/xwfb/tzgg",
    # 中国银行：企业年金集合计划季度管理情况（定期披露，非行业事件）
    "boc.cn/cbservice/bi2",
    # 国家公共资源交易平台：交易公开（水利/周转房/饲草等中标结果公示，与行业包无关）
    "ggzy.gov.cn/information/deal",
)

# CMS 栏目首页模板：末段以 index 结尾（index.html / index.shtml / A200102index_1.htm）
_INDEX_TAIL_RE = re.compile(r"(?:^|[^A-Za-z])index(?:_\d+)?\.(?:s?html?|php|jspx?|aspx?)$", re.I)
# 专题/栏目聚合路径。**只收实测确证是栏目页的词**，且逐词说明为什么不会误伤：
#   - album：整站"专题策划专辑"（cyzone /album/848459，5 行）；
#   - zt："专题"的拼音缩写（pedaily /zt/558050.shtml，1 行）；
#   - lists?：栏目列表（stcn /article/list/djsj.html、jiemian /lists/118.html）。
# 刻意**不含** topic(s)/special(s)/subject(s)：`/topic/<slug>`、`/special/<slug>`
# 在大量 CMS 里就是文章详情路径，实测把 tests/test_intel_stage4.py 的
# `https://example.com/topic/one` 这类正常文章 URL 判成了栏目页——收益（976 行里 1 行、
# 且该行分类本就是 other）远小于误伤代价，故一律不放行。
_SECTION_PATH_RE = re.compile(r"/(?:album|zt|lists?)(?:/|$)", re.I)
# 短频道页：m.pedaily.cn/news/c2（字母+数字的频道代号）。
# 尾段必须是"字母紧跟数字"这一种形态，两侧都收紧了实测误伤：
#   - 纯数字尾段（/news/20、/news/2026）在多数站点是"新闻 + 文章ID/年份"的文章路径；
#   - 纯字母尾段（/news/detail、/news/article）是详情页动作词，不是频道。
# 两例都是 976 行 / 全量单测里真实出现过的误杀，故一律不放行。
_SHORT_CHANNEL_RE = re.compile(
    r"^/(?:news|c|col|column|channel|categories|cat)/[A-Za-z]{1,6}\d{1,4}/?$")
# 列表类查询参数：只收实测确证是"列表筛选"的参数名。
#   - tags?：secrss /articles?tag=APT（标签列表页，2 行）；
#   - keywords?：cdb-capital /news/dynamic?keyword=（列表页空查询串，1 行）；
#   - cat*/column*：CMS 通用栏目参数。
# 刻意**不含** p/page/pageno（多页文章的 `?page=2` 是同一篇的后半段，不是列表页；
# 该形态在候选派发层已由 looks_like_listing_url 覆盖）、也不含 kw/class 这类
# 与文章参数同形的短名。
_LISTING_QUERY_RE = re.compile(
    r"(?:^|&)(?:tags?|keywords?|cat|categories?|columns?|channelid)=", re.I)
# 刊头式聚合索引："<品牌><刊期词>" 紧跟 |/｜/：/" "。
# 刻意不含 日报/早报/晚报/午报/快报：`人民日报 | …`、`经济日报：…` 是报纸转载的长文标题，
# 不是聚合刊头（A 机 976 行里 `【人民日报 | …】` 一条 9650 字长文，加进来就是误伤）。
_DIGEST_HEAD_RE = re.compile(
    r"^(?P<brand>[\u4e00-\u9fffA-Za-z0-9]{2,10}?)"
    r"(?P<period>24h|24小时|双周报|周报|月报|周刊|月刊|盘点|速览|要闻|简报|合集|一览)"
    r"\s*[|｜:：]")
# 以引号/书名号/序号括号开头的标题是"转载/引用"体，不是聚合刊头
_QUOTED_HEAD_RE = re.compile(r"^[\s【\[（(“\"'《]")


def deny_path_patterns():
    """信源栏目路径排除表：优先取 config 配置，缺省用生产实测清单。"""
    try:
        import config as _config  # 延迟导入：本模块被多处轻量引用，避免抬高 import 成本

        configured = getattr(_config, "INTEL_URL_DENY_PATH_PATTERNS", None)
        if configured is not None:
            return tuple(str(item).strip().casefold() for item in configured if str(item).strip())
    except Exception:
        pass
    return _DEFAULT_DENY_PATH_PATTERNS


def _gate_switch(name: str, default: bool) -> bool:
    """读取 config 上的闸门开关；config 不可用（单测直跑本模块）时用默认值。"""
    try:
        import config as _config

        return bool(getattr(_config, name, default))
    except Exception:
        return default


def url_shape_denied_reason(url: str, deny_patterns=None) -> str:
    """URL 是否为栏目/首页/列表页形态。命中返回原因字符串，未命中返回 ""。

    命中即代表该 URL 结构上不是单篇文章详情页（站点首页、栏目索引、专题页、
    标签/搜索/翻页列表、以及信源配置的机构公告栏目）。
    """
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(text)
    except Exception:
        return ""
    path = parts.path or ""
    # 只判网页 URL：`manual://1`、`internal://x` 这类内部伪 URL 与相对路径没有"页面形态"
    # 可言（urlsplit 把 `manual://1` 解析成 netloc=1/path=''，会被误判成站点首页），
    # 一律放行交给上游自己的判定。
    if str(parts.scheme or "").casefold() not in ("http", "https"):
        return ""
    host_path = f"{parts.netloc or ''}{path}".casefold()
    patterns = deny_path_patterns() if deny_patterns is None else tuple(
        str(item).strip().casefold() for item in deny_patterns if str(item).strip())
    for fragment in patterns:
        if fragment and fragment in host_path:
            return "deny_path"
    if path in ("", "/"):
        return "home"
    if _INDEX_TAIL_RE.search(path):
        return "index"
    if path.endswith("/"):
        return "directory"
    if _SECTION_PATH_RE.search(path):
        return "section"
    if _SHORT_CHANNEL_RE.match(path):
        return "channel"
    query = parts.query or ""
    if _LISTING_QUERY_RE.search(query):
        return "listing_query"
    return ""


def looks_like_channel_or_index_url(url: str, deny_patterns=None) -> bool:
    """URL 是否是栏目/首页/列表页（结构上不是文章详情页）。"""
    return bool(url_shape_denied_reason(url, deny_patterns))


def looks_like_digest_index_title(title: str) -> bool:
    """标题是否是"刊头式聚合索引"——标题本身是多条并列快讯的总目录。

    例：`投资界24h | 姚顺雨，坐在马化腾旁边；黄仁勋女婿火速晋升；紫燕食品又做LP了`。
    这类页面的正文只会落到其中一条快讯，与标题不构成一篇稿件。
    """
    text = str(title or "").strip()
    if not text or _QUOTED_HEAD_RE.match(text):
        return False
    return bool(_DIGEST_HEAD_RE.match(text))
# 字段标签：发文机关：/标　　题：/发文字号：/成文日期：/发布日期：/发布机构： 等
_LABEL_FIELD = re.compile(r"(?:^|[\n|｜])\s*[\u4e00-\u9fff][\u4e00-\u9fff \u3000]{0,9}[:：]")


def looks_like_metadata_shell(content: str) -> bool:
    """是否只是"公告头/字段表壳"页——只有若干「标签：值」字段行，没有正文。

    政务站点的公告详情常把 发文机关/发文字号/成文日期/发布日期/发布机构 单独渲染成一个字段区，
    正文却没随页面返回。这类页面字数常在 150~300 之间，能穿过"最小字数"和"段落数"两道检查，
    实际没有任何可读内容。判据：标签字段 ≥4 个，且去掉标签后剩余内容很薄、几乎没有句子。
    """
    text = str(content or "")
    if not text.strip():
        return False
    fields = _LABEL_FIELD.findall(text)
    if len(fields) < 4:
        return False
    residual = _LABEL_FIELD.sub(" ", text)
    residual = re.sub(r"[\s|｜]+", " ", residual).strip()
    sentences = len(re.findall(r"[。！？；]", residual))
    return len(residual) < 200 and sentences <= 1


# 会议/活动专用字段：用于识别"会议通知页"，不是文章正文
_MEETING_FIELDS = (
    "会议时间", "举办时间", "召开时间", "活动时间", "会议日期", "报到时间", "报名时间",
    "参会回执", "组委会", "会议地点", "会议日程", "会议议程", "议程安排", "拟邀请", "拟邀嘉宾",
    "主办单位", "承办单位", "协办单位", "支持单位", "会议注册", "报名方式", "参会费用", "会务费",
)


def looks_like_meeting_notice(content: str, title: str = "") -> bool:
    """是否只是会议/活动通知页——只有会议时间/主办单位/议程/报名回执等结构化字段，没有文章正文。

    学会/协会的会议通知会被日期抽取误当成"新文章"入库（页面上唯一日期是未来的会议时间），
    实际正文只是通知条款。判据：命中 ≥3 个会议专用字段；
    或命中 ≥2 个且全文很短、几乎没有句子（典型通知体的紧凑字段块）。
    只认会议专用字段，避免把"报道某峰会的新闻"误伤。
    """
    text = str(content or "")
    if not text.strip():
        return False
    hits = sum(1 for field in _MEETING_FIELDS if field in text)
    if hits >= 3:
        return True
    if hits >= 2:
        sentences = len(re.findall(r"[。！？；]", text))
        return len(text) < 1200 and sentences <= 6
    return False


def _content_sanity(content: str, title: str = "") -> list:
    """内容合理性：只在内容较长时，用"真实段落数"区分正文 vs 导航/列表。
    短到中等内容（<800 字）不强制多段落，避免误拒"单块紧凑结构化记录"，
    例如招标/中标公告头部（标题+编号+信息来源，无分段的单块文本）。
    同时容忍"结构化表格正文"（政务/公告/财务页）：即使没有 \\n\\n 空行分段，
    只要存在多条实质内容行也算正文，避免把含中标人/金额的表格正文误判为列表。
    真正的导航/列表页由 content_is_link_directory 与 listing_page 检查拦截。
    """
    issues = []
    text = (content or "").strip()
    if not text:
        return ["empty_content"]
    if len(text) >= 800:
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        real_paras = [p for p in paras if len(p) >= 40]
        substantive_lines = [ln.strip() for ln in text.split("\n") if len(ln.strip()) >= 40]
        if len(real_paras) < 2 and len(substantive_lines) < 2:
            issues.append("no_real_paragraphs")
    return issues


def _year(value: str):
    match = re.search(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", str(value or ""))
    return int(match.group(1)) if match else None


def assess_article_quality(extracted: Dict, candidate: Dict = None) -> Dict:
    """Return a strict, explainable quality decision before article persistence."""
    candidate = candidate or {}
    content = str(extracted.get("content") or "").strip()
    title = str(extracted.get("title") or candidate.get("title") or "").strip()
    published = str(extracted.get("publish_date") or candidate.get("published_at") or "")
    issues = []
    content_length = len(content)
    if content_length < MIN_SHORT_DYNAMIC_CHARS:
        # L3：低于短动态下限，硬拒绝
        issues.append("content_too_short")
    elif content_length < MIN_ARTICLE_CHARS:
        # L2 短行业动态：低于 LLM 处理标准字数（150），但满足「有效标题 + 发布日期
        # + 至少一个完整句子」即允许进入（tier=short_dynamic，调用方跳过 LLM）。
        if not title:
            issues.append("short_dynamic_missing_title")
        if not published:
            issues.append("short_dynamic_missing_publish")
        if not re.search(r"[。！？；!?;]", content):
            issues.append("short_dynamic_no_sentence")
    # 公告头/字段表壳页：只有「标签：值」字段行、没有正文 → 直接拒绝
    if looks_like_metadata_shell(content):
        issues.append("content_is_metadata_shell")
    # 会议/活动通知页：会议时间/主办单位/议程/报名回执 等结构化字段 → 不是文章，直接拒绝
    if looks_like_meeting_notice(content, title):
        issues.append("content_is_meeting_notice")
    # 内容健全性：区分真实正文 vs 导航/列表/占位
    issues.extend(_content_sanity(content, title))
    # Navigation pages can be very long while containing almost no readable
    # article body.  Count URL-bearing Markdown separately from residual text
    # so a corporate sitemap cannot pass merely because it has 10k characters.
    markdown_urls = _MARKDOWN_LINK.findall(content)
    raw_urls = _RAW_URL.findall(content)
    residual_text = _MARKDOWN_LINK.sub("", content)
    residual_text = _RAW_URL.sub("", residual_text)
    url_char_ratio = sum(len(url) for url in raw_urls) / max(1, len(content))
    if len(raw_urls) >= 25 and url_char_ratio >= 0.45 and len(residual_text.strip()) < 2500:
        issues.append("content_is_link_directory")
    # 作者主页/聚合列表页：标题列表（≥5 条日期行）或作者卡片特征（TA的文章/认证作者/文章 N 篇…）
    # → 不是单篇文章，拒绝入库（列表中的条目应由爬虫作为候选单独抓取）
    try:
        from content_handlers import looks_like_title_list as _looks_like_title_list
        from content_handlers import _AUTHOR_CARD_RE as _author_card_re
        if _looks_like_title_list(content) or _author_card_re.search(content[:3000]):
            issues.append("content_is_author_profile_or_title_list")
    except Exception:
        pass
    if re.search(r"(?:\.\.\.|…|\.\.。)\s*$", content):
        issues.append("content_looks_truncated")
    integrity = extracted.get("integrity") or {}
    issues.extend(str(item) for item in integrity.get("issues", []) if item)
    # 栏目/首页/列表页 URL 形态：有正文但结构上不是单篇文章详情页。
    # 例：`/index.html`、目录页、`/album/`、`/zt/`、`?tag=`、`?keyword=`、站点首页、机构公告栏目。
    url_shape_reason = ""
    article_url = str(extracted.get("url") or candidate.get("original_url") or "").strip()
    if _gate_switch("INTEL_REJECT_URL_SHAPE", True) and article_url:
        url_shape_reason = url_shape_denied_reason(article_url)
        if url_shape_reason:
            issues.append("url_is_channel_or_index_page")
    # 刊头式聚合索引标题（投资界24h | …、投资界LP周报 | …）：标题是多条快讯的总目录。
    if _gate_switch("INTEL_REJECT_PERIOD_DIGEST", True) and looks_like_digest_index_title(title):
        issues.append("title_is_period_digest")
    quality_score = extracted.get("quality_score", extracted.get("score"))
    try:
        quality_score = float(quality_score)
    except (TypeError, ValueError):
        quality_score = None
    # 统一到 [0,100]：缺省给 60 基础分，避免 out_of_range 误拒
    if quality_score is None:
        quality_score = 60.0
    else:
        quality_score = min(100.0, max(0.0, quality_score))
    title_year, published_year = _year(title), _year(published)
    if title_year and published_year and abs(title_year - published_year) > 1:
        historical = re.search(r"回顾|回顧|历史|歷史|存档|存檔|转载|轉載", f"{title}\n{content[:600]}", re.I)
        if not historical:
            issues.append("title_publish_date_conflict")
    metadata_issues = []
    if not extracted.get("site_name"):
        metadata_issues.append("site_name_missing")
    if not published:
        metadata_issues.append("publish_date_missing")
    hard_issues = [issue for issue in issues if issue != "title_publish_date_conflict"]
    # 分级字数标准：30~149 字且通过 L2 前置条件 → 短行业动态（调用方跳过 LLM）；
    # <30 字 → too_short；≥150 字 → full
    if content_length < MIN_SHORT_DYNAMIC_CHARS:
        tier = "too_short"
    elif content_length < MIN_ARTICLE_CHARS:
        tier = "short_dynamic"
    else:
        tier = "full"
    skip_llm = tier == "short_dynamic"
    return {
        "passed": not issues,
        "issues": list(dict.fromkeys(issues)),
        "metadata_issues": metadata_issues,
        "content_length": content_length,
        "tier": tier,
        "skip_llm": skip_llm,
        "url_count": len(raw_urls),
        "markdown_link_count": len(markdown_urls),
        "url_char_ratio": round(url_char_ratio, 4),
        "residual_text_length": len(residual_text.strip()),
        "url_shape_reason": url_shape_reason,
        "quality_score": quality_score,
        "reason": ",".join(dict.fromkeys(issues)),
    }
