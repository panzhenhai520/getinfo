"""Pre-persistence admission decision for extracted intelligence content."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

import config
from intel_llm_client import IntelLLMError, intel_llm_client

ADMISSION_VALUES = {"article", "directory", "service_page", "irrelevant", "review", "boilerplate"}


def _rule_decision(extracted, candidate):
    url = str(extracted.get("url") or candidate.get("original_url") or "")
    path = urlsplit(url).path.rstrip("/")
    content = str(extracted.get("content") or "")
    title = str(extracted.get("title") or candidate.get("title") or "")
    # 整页都是模板框架（分享/评论/热文推荐/公众号…、几乎没有有效段落）→ 判 boilerplate。
    # 这类页面标题看着像正经文章，只靠 URL 规则和 LLM 都容易放行，
    # 放进来会污染知识库（实例：2016亚太财富论坛…风云榜，410 字全是框架）。
    # 调用方 candidate_crawler_adapter 对 admission != "article" 一律不持久化，
    # 判定值仍会写进 admission_status 留审计。
    try:
        from intel_boilerplate import assess as _assess_boilerplate

        verdict = _assess_boilerplate(content, title)
        if verdict.get("is_boilerplate"):
            return {
                "admission": "boilerplate",
                "confidence": 0.9,
                "reason": verdict.get("reason") or "正文为页面框架",
                "link_expansion_recommended": False,
            }
    except Exception:
        pass
    # A root page with many navigation-like words is not an independent article.
    nav_markers = ("服務", "服务", "公司註冊", "公司注册", "聯絡", "联系我们")
    if path in {"", "/", "/zh-hant", "/zh-hans"} and sum(marker in content for marker in nav_markers) >= 2:
        return {"admission": "directory", "confidence": 0.95, "reason": "根页面包含导航/服务目录", "link_expansion_recommended": True}
    if any(token in url.lower() for token in ("/services/", "/service/", "/private-clients/")) and len(content) < 1200:
        return {"admission": "service_page", "confidence": 0.8, "reason": "URL 与正文呈现服务页特征", "link_expansion_recommended": True}
    return None


def assess_admission(extracted, candidate, pack):
    """Return fail-closed decision. LLM ambiguity never permits persistence."""
    rule = _rule_decision(extracted, candidate)
    if rule:
        return rule
    if not config.INTEL_LLM_ENABLED or not intel_llm_client.configured:
        # Offline/test deployments retain a conservative deterministic path
        # after the hard quality gate; production enables the LLM path.
        return {"admission": "article", "confidence": 0.65, "reason": "LLM 未启用，已通过硬性质量准入", "link_expansion_recommended": False}
    try:
        return intel_llm_client.assess_admission(extracted, pack)
    except IntelLLMError as exc:
        return {"admission": "review", "confidence": 0.0, "reason": f"LLM 准入失败：{exc}", "link_expansion_recommended": False}
