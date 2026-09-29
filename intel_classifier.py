#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Deterministic rule and fixed-topic classifier for intelligence articles."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List

import re

import config
from industry_packs import (
    IndustryPackLoader,
    industry_anchor_keywords,
    industry_pack_loader,
    normalize_intel_text,
)
from intel_contracts import DEFAULT_INDUSTRY_PACK_ID
from intel_database import IntelRepository, intel_repository
from intel_http import sanitize_external_error
from intel_llm_client import IntelLLMClient, intel_llm_client
from ragflow_llm_client import (
    FUSION_VERSION,
    LLM_PROMPT_VERSION,
)


CLASSIFIER_VERSION = "rule-v1"
TOPIC_TAGGING_VERSION = "fixed-topic-tag-v1"
MAX_TOPIC_TAGS = 8
DEFAULT_RECENT_TODAY_WINDOW_DAYS = 5
DEFAULT_RECENT_TREND_WINDOW_DAYS = 21


def _matches(text: str, keywords: List[str]) -> List[str]:
    found = []
    for keyword in keywords or []:
        normalized = normalize_intel_text(keyword)
        if normalized and normalized in text:
            found.append(keyword)
    return found


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, float(value)))


def _parse_reference_time(article: Dict) -> datetime | None:
    for field in ("publish_date", "first_crawled", "created_at"):
        raw = str((article or {}).get(field) or "").strip()
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _recent_fallback_category(
    article: Dict,
    *,
    hits: Dict,
    weights: Dict,
    relevance_score: float,
) -> Dict | None:
    """Return a narrow recency fallback for new but high-relevance articles.

    This keeps fresh industry articles from collapsing into ``other`` when the
    title/content are clearly on-topic but do not repeat an explicit
    trend/event keyword.  The fallback stays conservative by requiring a
    stronger-than-minimum relevance score and at least two evidence points
    across anchor/core/expanded matches.
    """

    reference_time = _parse_reference_time(article)
    if reference_time is None:
        return None
    today_window_days = max(
        1,
        int(weights.get("recent_today_window_days", DEFAULT_RECENT_TODAY_WINDOW_DAYS)),
    )
    trend_window_days = max(
        today_window_days,
        int(weights.get("recent_trend_window_days", DEFAULT_RECENT_TREND_WINDOW_DAYS)),
    )
    age_days = max(
        0.0,
        (datetime.now(timezone.utc) - reference_time).total_seconds() / 86400.0,
    )
    strong_evidence = (
        len(hits["anchor"]) >= 2
        or (
            len(hits["anchor"]) >= 1
            and (
                len(hits["expanded"]) + len(hits["trend"]) + len(hits["event"]) >= 1
            )
        )
    )
    required_relevance = float(weights["minimum_relevance_score"])
    if not strong_evidence or relevance_score < required_relevance:
        return None
    if age_days <= today_window_days:
        return {
            "category": "event",
            "confidence": _clamp(0.62 + relevance_score / 45.0),
            "reason": "行业相关性达到阈值，且属于近期新资讯，按今天分类兜底",
            "age_days": round(age_days, 2),
            "window_days": today_window_days,
        }
    if age_days <= trend_window_days:
        return {
            "category": "trend",
            "confidence": _clamp(0.60 + relevance_score / 45.0),
            "reason": "行业相关性达到阈值，且属于近期延续性资讯，按趋势分类兜底",
            "age_days": round(age_days, 2),
            "window_days": trend_window_days,
        }
    return None


# 泛化的主题关键词：单次命中不足以把一篇文章归入某主题（需正文≥2次或标题命中）。
_GENERIC_TOPIC_KEYWORDS = {"合作", "发布", "产品", "上线", "项目", "服务", "方案", "动态", "支持", "申请"}

# 排除词中"仅标题命中即剔除"的词：这些词在合法行业正文里也可能出现（如"解决方案/简介"），
# 只在标题出现才视为厂商产品/解决方案/硬广页，避免误伤正文提到的行业案例或行业术语。
_TITLE_ONLY_EXCLUDE = {
    "解决方案", "简介", "为什么选择我们",
    "企业云", "云服务器", "裸金属", "池化软件", "全栈云平台", "智能诊断", "服务解析",
}

# 「客户与中标」主题只收真正的招标/中标/采购类文章：无以下强标讯信号则跳过。
_BID_TENDER_RE = re.compile(
    r"招标公告|招标文件|招标代理|招标中心|中标公告|中标结果|中标候选人|中标公示|中标人|中标单位|中标价|"
    r"开标|评标|评标结果|投标|公开招标|邀请招标|竞争性磋商|竞争性谈判|询价公告|采购公告|采购项目|"
    r"招投标|标段|资格预审|单一来源|供应商征集|成交公告|成交结果|中选|中标通知书|合同签约|项目采购",
    re.I,
)


_ASCII_ONLY_KEYWORD = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 .+\-_/]*$')


def _keyword_occurrences(text: str, keyword: str) -> int:
    """关键词出现次数。纯 ASCII 关键词按**单词边界**匹配，避免短缩写误命中
    （如 ANC 命中 ADVanCED / BALANCE 这类子串，会把不相干的文章拉进技术主题）；
    中文按子串匹配（中文没有词边界）。"""
    if not text or not keyword:
        return 0
    if _ASCII_ONLY_KEYWORD.match(keyword):
        pattern = r'(?<![A-Za-z0-9])' + re.escape(keyword) + r'(?![A-Za-z0-9])'
        return len(re.findall(pattern, text, re.I))
    return text.count(keyword)


def _keyword_present(text: str, keyword: str) -> bool:
    return _keyword_occurrences(text, keyword) > 0


# 归属门槛：单个弱命中（正文出现 +0.35 / 匹配关键词 +0.45）不足以归类主题，
# 至少要标题命中（+0.6）或两处以上命中；否则车型文章会因为正文里提了一句
# "支持主动降噪"就被归进 ANC 技术主题。
MIN_TOPIC_ASSIGN_SCORE = 0.6
# 正文级命中的出现次数门槛：原来只要求 ≥2 次，结果"车型参数表里顺带写到风阻/空气动力学"
# 这类文章也会被归进技术主题（例：一篇讲汽车出海的文章进了「风洞测试技术」✗）。
# 提到 3 次以上才说明正文真的在讲这个技术 ✓
MIN_CONTENT_OCCURRENCES = 3


def match_fixed_topics(article: Dict, industry_pack: Dict) -> List[Dict]:
    """Return deterministic multi-label topic assignments for one article.

    Industry admission is deliberately handled by ``classify_article`` before
    this function is called. Topic keywords may therefore be narrower and may
    overlap between topics without becoming a second industry admission gate.
    """
    title_text = normalize_intel_text(str((article or {}).get("title") or ""))
    crawler_text = normalize_intel_text(
        str((article or {}).get("matched_keywords") or "")
    )
    content_text = normalize_intel_text(
        str((article or {}).get("content") or "")[:200000]
    )
    assignments = []
    # 判别力门限：某关键词若同时出现在**半数以上主题**的关键词表里，它就无法区分主题
    # （例如 8 个主题里有 7 个都写了同一家机构名），这类词不参与主题归属；否则任何提到
    # 该词的文章会被同时挂到多个互不相干的主题上。机构名应放在"备案机构"（门禁维度），
    # 不属于主题维度。没有任何共用词时此逻辑不生效，行为与原来完全一致。
    _topics_all = list(industry_pack.get("fixed_topics") or [])
    _shared_cutoff = max(2, (len(_topics_all) + 1) // 2)
    _kw_topic_count = {}
    for _topic in _topics_all:
        for _kw in {normalize_intel_text(str(k)) for k in (_topic.get("keywords") or []) if str(k).strip()}:
            if _kw:
                _kw_topic_count[_kw] = _kw_topic_count.get(_kw, 0) + 1
    shared_keywords = {k for k, n in _kw_topic_count.items() if n >= _shared_cutoff}
    if shared_keywords:
        print(
            "🧭 主题判别力门限：忽略 %d 个被 ≥%d 个主题共用的关键词（例：%s）"
            % (len(shared_keywords), _shared_cutoff, "、".join(list(shared_keywords)[:3]))
        )
    for order, topic in enumerate(industry_pack.get("fixed_topics") or []):
        # 「客户与中标」只收招标/中标/采购类文章：无强标讯信号则跳过，避免普通资讯沾"客户/合作"词进入
        if (topic.get("key") == "customers" or topic.get("name") == "客户与中标") and not _BID_TENDER_RE.search(
            title_text + " " + content_text
        ):
            continue
        # 排除词：命中则跳过该主题（剔除厂商产品页/导航页等噪声，让主题只留权威/行业正文类文章，提高权威占比）。
        # 仅标题命中即剔除的词见 _TITLE_ONLY_EXCLUDE；其余词标题或正文命中即剔除。
        exclude_keywords = topic.get("exclude_keywords") or []
        if exclude_keywords:
            excluded = False
            for ek in exclude_keywords:
                nk = normalize_intel_text(str(ek))
                if not nk:
                    continue
                if nk in title_text:
                    excluded = True
                    break
                if nk not in _TITLE_ONLY_EXCLUDE and nk in content_text:
                    excluded = True
                    break
            if excluded:
                continue
        hits = []
        score = 0.0
        seen = set()
        strong_hit = False          # A 方案：是否存在"强命中"（标题命中，或某关键词正文 ≥3 次）
        for keyword in topic.get("keywords") or []:
            normalized = normalize_intel_text(keyword)
            if not normalized or normalized in seen or normalized in shared_keywords:
                continue
            locations = []
            content_occ = _keyword_occurrences(content_text, normalized)
            is_generic = normalized in _GENERIC_TOPIC_KEYWORDS
            if _keyword_present(title_text, normalized):
                locations.append("title")
                score += 0.6
                strong_hit = True            # 标题命中 = 强命中 ✓
            if _keyword_present(crawler_text, normalized) and not is_generic:
                locations.append("matched_keywords")
                score += 0.45
            if content_occ > 0:
                # 泛词需正文出现多次，或标题也命中，才算内容级强匹配；
                # 非泛词也要求出现 ≥MIN_CONTENT_OCCURRENCES 次才算"强命中"，
                # 只为"正文里顺带提过"的老弱命中计权（不足以单独决定归属）。
                if content_occ >= MIN_CONTENT_OCCURRENCES or (not is_generic) or _keyword_present(title_text, normalized):
                    locations.append("content")
                    score += 0.35
                if content_occ >= MIN_CONTENT_OCCURRENCES:
                    strong_hit = True        # 同一关键词反复出现 = 真的在讲它 ✓
            if not locations:
                continue
            seen.add(normalized)
            hits.append({"keyword": keyword, "locations": locations})
        # A 方案：必须有【至少一个强命中】（标题命中，或某关键词正文出现 ≥3 次）才归属。
        # 否则"多个不同关键词各在正文出现 1 次"（车型参数表顺带提到风阻/空气动力学…）
        # 累加分数也会过线，导致不相干文章进入技术主题 ✗
        if hits and score >= MIN_TOPIC_ASSIGN_SCORE and strong_hit:
            assignments.append(
                {
                    "key": str(topic.get("key") or ""),
                    "name": str(topic.get("name") or ""),
                    "matched_keywords": [hit["keyword"] for hit in hits],
                    "evidence": hits,
                    "score": round(min(1.0, score), 4),
                    "assignment_method": "rule_keyword",
                    "_order": order,
                }
            )
    assignments.sort(key=lambda item: (-item["score"], item["_order"]))
    for item in assignments[:MAX_TOPIC_TAGS]:
        item.pop("_order", None)
    return assignments[:MAX_TOPIC_TAGS]


# 行业强信号词（工控安全与算力包）：命中其一即视为"具备行业信号"。泛词（基础设施/中标）不算。
_INDUSTRY_CORE_TERMS = [
    "工控", "工业控制", "工控安全", "工业信息安全", "工业互联网", "网络安全", "等保", "等级保护",
    "电力", "电网", "变电站", "发电", "电厂", "电力工程", "能源", "新能源", "储能",
    "算力", "智算", "GPU", "芯片", "半导体", "超算", "数据中心", "服务器",
    "信创", "国产化", "网络攻击", "漏洞", "安全设备", "工控系统", "SCADA", "DCS", "PLC",
]
# 企业/官网噪声标题：命中即视为非行业正文（企业简介/成果摘要/汇总/导航/联系方式/产品页等），一律拒绝
_NOISE_TITLE_TERMS = [
    "简介", "概况", "公司简介", "企业简介", "集团简介", "联系方式", "领导班子", "班子成员", "微博",
    "信息公开", "公告汇总", "动态概览", "关于我们", "产品中心", "产品介绍", "产品页", "用户手册",
    "文档中心", "免费试用", "试用中心", "网站维护", "维护通知", "成果摘要", "重要活动", "活动与成果",
    "大事记", "年鉴", "目录", "联系我们", "登录", "注册", "招聘", "人才招聘", "企业概况", "重要成果",
]


def _industry_signal(text: str, industry_pack: Dict, title: str = '') -> bool:
    """通用行业限定：命中行业强信号词 或 行业包特定实体名 → 属于行业包。
    排除企业/官网噪声标题（简介/成果摘要/汇总/导航页等）；不因话题标注词（政策/监管/中标/AI）误纳。"""
    title = normalize_intel_text(str(title or ""))
    if title:
        for nt in _NOISE_TITLE_TERMS:
            if nt in title:
                return False
    t = normalize_intel_text(str(text or ""))
    # 行业包自身强信号词（核心词/扩展词，或配置了锚点/机构词时用锚点+机构）——
    # 多租户下必须按包判定，不能只认全局硬编码的工控/电力/算力词表，
    # 否则汽车/风洞等其它行业文章会因不含那些词而被误拒。
    for kw in industry_anchor_keywords(industry_pack):
        nm = normalize_intel_text(kw)
        if nm and nm in t:
            return True
    for kw in _INDUSTRY_CORE_TERMS:
        if kw in t:
            return True
    # 行业包特定实体：来源厂商/客户实体名（名词，非话题词）
    for src in (industry_pack or {}).get("default_sources") or []:
        nm = normalize_intel_text(str(src.get("name") or ""))
        if nm and len(nm) >= 2 and nm in t:
            return True
    return False


def _industry_gate_passed(score_details: Dict, article: Dict = None, industry_pack: Dict = None) -> bool:
    hits = (score_details.get("hits") or {}).get("anchor") or []
    if not (bool(hits) and float(score_details.get("relevance_score") or 0) >= float(
            score_details.get("minimum_relevance_score") or 0)):
        return False
    # 通用行业限定：命中行业核心词或包实体才放行（防止仅泛词命中）
    if article is not None:
        title = str((article or {}).get("title") or "")
        content = str((article or {}).get("content") or "")
        kw = str((article or {}).get("matched_keywords") or "")
        if not _industry_signal(title + " " + content + " " + kw, industry_pack):
            return False
    return True


def _resolve_llm_topics(tags: List[str], industry_pack: Dict) -> List[Dict]:
    """Map untrusted LLM tags onto configured topics and discard inventions."""
    resolved = []
    seen = set()
    for raw_tag in tags or []:
        normalized_tag = normalize_intel_text(raw_tag)
        if not normalized_tag:
            continue
        for topic in industry_pack.get("fixed_topics") or []:
            topic_terms = {
                normalize_intel_text(topic.get("key")),
                normalize_intel_text(topic.get("name")),
                *(
                    normalize_intel_text(keyword)
                    for keyword in topic.get("keywords") or []
                ),
            }
            key = str(topic.get("key") or "")
            if normalized_tag not in topic_terms or not key or key in seen:
                continue
            seen.add(key)
            resolved.append(
                {
                    "key": key,
                    "name": str(topic.get("name") or ""),
                    "matched_keywords": [],
                    "evidence": [{"llm_tag": str(raw_tag)}],
                    "score": 0.5,
                    "assignment_method": "llm_tag",
                }
            )
            break
        if len(resolved) >= MAX_TOPIC_TAGS:
            break
    return resolved


def classify_article(article: Dict, industry_pack: Dict) -> Dict:
    title = str((article or {}).get("title") or "")
    content = str((article or {}).get("content") or "")
    matched_from_crawler = str((article or {}).get("matched_keywords") or "")
    # 通用行业过滤器（前置）：不属于行业包（未命中行业核心词/包实体）→ 不予准入
    if not _industry_signal(f"{title} {content} {matched_from_crawler}", industry_pack, title=title):
        return {
            "rule_category": "other", "rule_confidence": 0.0,
            "rule_reason": "通用行业过滤器：未命中行业核心词或行业包实体，不予准入",
            "score_details": {"relevance_score": 0.0, "minimum_relevance_score": 0.0,
                              "hits": {}, "admitted": False},
            "matched_keywords": [], "topic_tags": [], "topic_keys": [],
            "final_category": "other", "final_confidence": 0.0,
            "final_reason": "通用行业过滤器：未命中行业核心词或行业包实体，不予准入",
            "result_source": "industry_filter", "admitted": False,
        }
    normalized_text = normalize_intel_text(f"{title}\n{title}\n{matched_from_crawler}\n{content[:200000]}")

    hits = {
        "anchor": _matches(normalized_text, industry_anchor_keywords(industry_pack)),
        "core": _matches(normalized_text, industry_pack.get("core_keywords") or []),
        "expanded": _matches(normalized_text, industry_pack.get("expanded_keywords") or []),
        "trend": _matches(normalized_text, industry_pack.get("trend_keywords") or []),
        "event": _matches(normalized_text, industry_pack.get("event_keywords") or []),
        "negative": _matches(normalized_text, industry_pack.get("negative_keywords") or []),
        "brand": _matches(normalized_text, industry_pack.get("brands") or []),
    }
    weights = industry_pack["classification"]
    component_scores = {
        "core": len(hits["core"]) * float(weights["core_weight"]),
        "expanded": len(hits["expanded"]) * float(weights["expanded_weight"]),
        "trend": len(hits["trend"]) * float(weights["trend_weight"]),
        "event": len(hits["event"]) * float(weights["event_weight"]),
        "negative": len(hits["negative"]) * float(weights["negative_weight"]),
    }
    # An expanded keyword can be useful for display, but only configured
    # anchors are allowed to prove industry relevance.
    relevance_score = (len(hits["anchor"]) * float(weights["core_weight"])) + component_scores["negative"]
    minimum = float(weights["minimum_relevance_score"])
    trend_score = component_scores["trend"]
    event_score = component_scores["event"]
    score_details_fallback = {}

    if not hits["anchor"] or relevance_score < minimum:
        category = "other"
        confidence = _clamp(0.45 + max(0.0, relevance_score) / max(10.0, minimum * 5.0))
        reason = "核心相关性低于行业包阈值"
    elif trend_score <= 0 and event_score <= 0:
        recent_fallback = _recent_fallback_category(
            article,
            hits=hits,
            weights=weights,
            relevance_score=relevance_score,
        )
        if recent_fallback:
            category = str(recent_fallback["category"])
            confidence = float(recent_fallback["confidence"])
            reason = str(recent_fallback["reason"])
            score_details_fallback = {
                "category": category,
                "age_days": recent_fallback["age_days"],
                "window_days": recent_fallback["window_days"],
            }
        else:
            category = "other"
            confidence = _clamp(0.55 + relevance_score / 30.0)
            reason = "文章与行业相关，但未命中明确趋势或事件信号"
            score_details_fallback = {}
    else:
        category_scores = {"trend": trend_score, "event": event_score, "other": 0.0}
        tie_break = list(weights.get("tie_break_order") or ["trend", "event", "other"])
        category = sorted(
            category_scores,
            key=lambda item: (-category_scores[item], tie_break.index(item)),
        )[0]
        winner = category_scores[category]
        runner_up = max(score for key, score in category_scores.items() if key != category)
        confidence = _clamp(0.60 + relevance_score / 40.0 + (winner - runner_up) / 20.0)
        signal_name = "趋势" if category == "trend" else "事件"
        reason = f"行业相关性达到阈值，并命中{signal_name}信号"
        score_details_fallback = {}

    ordered_matches = []
    for group in ("core", "expanded", "trend", "event", "negative"):
        for keyword in hits[group]:
            if keyword not in ordered_matches:
                ordered_matches.append(keyword)
    score_details = {
        "relevance_score": relevance_score,
        "minimum_relevance_score": minimum,
        "trend_score": trend_score,
        "event_score": event_score,
        "components": component_scores,
        "hits": hits,
        "tie_break_order": list(weights.get("tie_break_order") or []),
        "topic_tagging_version": TOPIC_TAGGING_VERSION,
    }
    if score_details_fallback:
        score_details["temporal_fallback"] = score_details_fallback
    topic_assignments = (
        match_fixed_topics(article, industry_pack)
        if _industry_gate_passed(score_details, article, industry_pack)
        else []
    )
    score_details["topic_assignments"] = topic_assignments
    return {
        "rule_category": category,
        "rule_confidence": round(confidence, 4),
        "rule_reason": reason,
        "score_details": score_details,
        "matched_keywords": ordered_matches,
        "topic_tags": [item["name"] for item in topic_assignments],
        "topic_keys": [item["key"] for item in topic_assignments],
        "topic_tagging_version": TOPIC_TAGGING_VERSION,
        "final_category": category,
        "final_confidence": round(confidence, 4),
        "final_reason": reason,
        "result_source": "rule",
        "admitted": True,
    }


def _rule_importance(article: Dict, result: Dict) -> str:
    title = " ".join(str(article.get("title") or "").split())[:100]
    if result["rule_category"] == "event":
        return f"事件判断：{title or '该事件'}；建议关注其后续进展与短期行业影响。"
    if result["rule_category"] == "trend":
        return f"趋势判断：{title or '该变化'}可能影响行业政策、市场结构或长期决策。"
    return ""


def fuse_rule_and_llm(rule_result: Dict, llm_result: Dict, industry_pack: Dict) -> Dict:
    threshold = float(industry_pack["classification"]["llm_confidence_threshold"])
    fused = dict(rule_result)
    fused.update(
        {
            "llm_category": llm_result["category"],
            "llm_confidence": llm_result["confidence"],
            "llm_reason": llm_result["reason"],
            "why_important": llm_result["why_important"],
            "trend_summary": llm_result["trend_summary"],
            "fusion_version": FUSION_VERSION,
        }
    )
    if llm_result["confidence"] >= threshold:
        fused["final_category"] = llm_result["category"]
        fused["final_confidence"] = round(
            max(float(rule_result["rule_confidence"]), float(llm_result["confidence"])),
            4,
        )
        fused["final_reason"] = llm_result["reason"]
        fused["result_source"] = (
            "rule_llm_agree"
            if llm_result["category"] == rule_result["rule_category"]
            else "llm_override"
        )
        if _industry_gate_passed(fused.get("score_details") or {}):
            assignments = list(
                (fused.get("score_details") or {}).get("topic_assignments") or []
            )
            existing = {str(item.get("key") or "") for item in assignments}
            for assignment in _resolve_llm_topics(
                llm_result.get("topic_tags") or [], industry_pack
            ):
                if assignment["key"] not in existing:
                    assignments.append(assignment)
                    existing.add(assignment["key"])
            assignments = assignments[:MAX_TOPIC_TAGS]
            fused["score_details"]["topic_assignments"] = assignments
            fused["topic_tags"] = [item["name"] for item in assignments]
            fused["topic_keys"] = [item["key"] for item in assignments]
    else:
        fused["result_source"] = "rule_llm_low_confidence"
        fused["final_reason"] = (
            f"{rule_result['rule_reason']}；LLM 置信度未达到行业包阈值"
        )
    return fused


class IntelClassificationService:
    def __init__(
        self,
        repository: IntelRepository = None,
        pack_loader: IndustryPackLoader = None,
        llm_client: IntelLLMClient = None,
    ):
        self.repository = repository or intel_repository
        self.pack_loader = pack_loader or industry_pack_loader
        self.llm_client = llm_client or intel_llm_client

    def classify_article_id(
        self,
        article_id: int,
        industry_pack_id: str = DEFAULT_INDUSTRY_PACK_ID,
        *,
        activation_id: str = "",
    ) -> Dict:
        article = self.repository.get_article(article_id)
        if not article:
            raise ValueError(f"article not found: {article_id}")
        pack = self.pack_loader.load(industry_pack_id)
        result = classify_article(article, pack)
        if not result.get("admitted"):
            return result  # 通用行业过滤器：不属于行业包，跳过 LLM 融合与准入
        result["why_important"] = _rule_importance(article, result)
        result["trend_summary"] = ""
        result["fusion_version"] = FUSION_VERSION
        result["llm_model_id"] = ""
        result["llm_prompt_version"] = ""
        result["llm_error"] = ""
        llm_threshold = float(pack["classification"]["llm_confidence_threshold"])
        # 分级字数标准：短行业动态（30~149 字）跳过 LLM 分类，直接采用规则结果（省 LLM 消耗）
        from intel_content_quality_gate import MIN_ARTICLE_CHARS, MIN_SHORT_DYNAMIC_CHARS
        _content_len = len(str(article.get("content") or ""))
        _short_dynamic = MIN_SHORT_DYNAMIC_CHARS <= _content_len < MIN_ARTICLE_CHARS
        if config.INTEL_LLM_ENABLED and not _short_dynamic and float(result["rule_confidence"]) < llm_threshold:
            result["llm_model_id"] = self.llm_client.model_id
            result["llm_prompt_version"] = LLM_PROMPT_VERSION
            try:
                llm_result = self.llm_client.classify(article, pack)
                # Tier2 行业门禁：LLM 一句话判定"是否属于当前行业包"，不属于 → 舍弃（不落库/不总结）
                if not llm_result.get("in_pack_industry", True):
                    result = classify_article(article, pack)
                    if isinstance(result, dict):
                        result["admitted"] = False
                        result["rule_reason"] = "Tier2行业门禁：LLM 判定该文不属于当前行业包，舍弃"
                        result["final_reason"] = "Tier2行业门禁：LLM 判定该文不属于当前行业包，舍弃"
                        result["rule_category"] = "other"
                        result["final_category"] = "other"
                    return result
                result = fuse_rule_and_llm(result, llm_result, pack)
            except Exception as exc:
                result["llm_error"] = sanitize_external_error(
                    exc,
                    secrets=(
                        config.RAGFLOW_API_KEY,
                        getattr(self.llm_client, "api_key", ""),
                    ),
                )
                result["result_source"] = "rule_fallback"
                result["final_reason"] = (
                    f"{result['rule_reason']}；LLM 不可用，已使用规则结果"
                )
        result.update(
            {
                "article_id": int(article_id),
                "industry_pack_id": pack["id"],
                "activation_id": str(activation_id or ""),
                "industry_pack_version": pack["pack_version"],
                "classifier_version": CLASSIFIER_VERSION,
                "article_content_hash": self.repository.article_content_hash(article),
            }
        )
        # 禁止兜底归包：未命中行业包锚点词（或相关性低于阈值）的文章不得落分类行。
        # 历史行为会把这类"other"兜底结果也写入一行（典型：默认包混入全库噪声），
        # 现改为跳过写入；若存在历史兜底行则一并清除，供批量重分类收敛存量数据。
        _score_details = result.get("score_details") or {}
        _hits = _score_details.get("hits") or {}
        _anchors = _hits.get("anchor") or []
        _relevance = float(_score_details.get("relevance_score") or 0.0)
        _minimum = float(_score_details.get("minimum_relevance_score") or 0.0)
        if not _anchors or _relevance < _minimum:
            try:
                self.repository.delete_article_classification(
                    int(article_id), pack["id"]
                )
            except Exception as _del_exc:
                print(
                    f"⚠️ 清理历史兜底分类行失败 article={article_id} pack={pack['id']}: {_del_exc}"
                )
            result["_not_classified"] = True
            result["final_category"] = "other"
            result["final_reason"] = "未命中行业包锚点词，不归包（禁止兜底）"
            return result
        classification_id = self.repository.upsert_classification(result)
        result["classification_id"] = classification_id
        if pack["id"] == "financial_markets" and str(activation_id or ""):
            from industry_pack_runtime import ActiveIndustryCompositionService
            from project_keyword_gate import matched_project_keywords

            active = ActiveIndustryCompositionService(
                self.repository.db, pack_loader=self.pack_loader
            ).snapshot()
            if active["active_industry_activation_id"] == str(activation_id):
                project_matches = matched_project_keywords(
                    active["project_keywords"],
                    article.get("title"),
                    article.get("content"),
                    article.get("matched_keywords"),
                )
                result["financial_addon_match"] = (
                    self.repository.record_financial_addon_match(
                        int(article_id),
                        activation_id=str(activation_id),
                        primary_industry_pack_id=active["active_industry_pack_id"],
                        matched_keywords=project_matches,
                    )
                )
        return result


classification_service = IntelClassificationService()


def classify_article_by_id(article_id: int, industry_pack_id: str = None) -> Dict:
    return classification_service.classify_article_id(
        article_id,
        industry_pack_id or config.INTEL_DEFAULT_INDUSTRY_PACK,
    )
