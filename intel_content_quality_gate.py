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
        "quality_score": quality_score,
        "reason": ",".join(dict.fromkeys(issues)),
    }
