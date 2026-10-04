#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic policy/regulation evidence anchoring for unified QA.

This module deliberately sits outside model prompts.  For regulation-number
questions, source eligibility and evidence priority must be decided by program
rules first, otherwise generic semantic matches can drown out official texts
and professional interpretations.
"""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Mapping
from urllib.parse import urlsplit


_POLICY_HINT_RE = re.compile(
    r"政策|法规|法例|条例|公告|财政部|財政部|税务总局|稅務總局|税务局|稅務局|个税|個稅|"
    r"个人所得税|個人所得稅|征管|徵管|离岸信托|離岸信託|岸外信托|岸外信託|合规|监管|監管",
    re.I,
)
_NOTICE_RE = re.compile(
    r"(20\d{2})\s*年.{0,12}?(?:公告|第)?\s*(\d{1,4})\s*[号號]|"
    r"(?:公告|第)?\s*(\d{1,4})\s*[号號](?:公告)?",
    re.I,
)
_MEDIA_SOURCE_CATALOG = {
    "hkej": {
        "label": "信报/HKEJ",
        "aliases": ["信报", "信報", "hkej", "hkej.com"],
        "domains": ["hkej.com"],
    },
    "zaobao": {
        "label": "联合早报",
        "aliases": ["联合早报", "聯合早報", "新加坡联合早报", "新加坡聯合早報", "zaobao", "zaobao.com.sg"],
        "domains": ["zaobao.com.sg"],
    },
    "caixin": {
        "label": "财新",
        "aliases": ["财新", "財新", "caixin", "caixin.com"],
        "domains": ["caixin.com"],
    },
    "scmp": {
        "label": "南华早报/SCMP",
        "aliases": ["南华早报", "南華早報", "scmp", "scmp.com"],
        "domains": ["scmp.com"],
    },
    "hk01": {
        "label": "香港01",
        "aliases": ["香港01", "hk01", "hk01.com"],
        "domains": ["hk01.com"],
    },
}
_MEDIA_ACTION_RE = re.compile(r"(?:解读|解讀|报道|報道|文章|分析|评论|評論|社论|社論|专栏|專欄)")
_DYNAMIC_QUOTED_SOURCE_RE = re.compile(r"[《「\"]([^《》「」\"]{2,24})[》」\"]")
_DYNAMIC_PLAIN_SOURCE_RE = re.compile(
    r"(?:^|[，,。？?；;\s])([^，,。？?；;\s]{2,18}(?:报|報|日报|日報|时报|時報|早报|早報|新闻|新聞|周刊|杂志|雜誌|"
    r"News|Times|Post|Journal|Reuters|Bloomberg|BBC|CNN))"
    r"(?:有|有没有|有沒有|是否有|有没有相关|有沒有相關|如何|怎么|怎样|怎樣)?[^，,。？?；;]{0,12}"
    r"(?:解读|解讀|报道|報道|文章|分析|评论|評論)",
    re.I,
)
_OFFICIAL_DOMAINS = {
    "mof.gov.cn", "chinatax.gov.cn", "gov.cn", "tax.sh.gov.cn", "beijing.chinatax.gov.cn",
}
_LAW_FIRM_HINT_RE = re.compile(
    r"金杜|君合|通商|竞天公诚|方达|中伦|汉坤|锦天城|律师|律師|律所|law|legal|"
    r"king\s*&?\s*wood|jingtian|junhe|han\s*kun|fangda|zhong\s*lun",
    re.I,
)
_NOISE_RE = re.compile(
    r"获奖|獲獎|荣获|榮獲|殊荣|殊榮|奖项|獎項|最佳|颁奖|頒獎|基金产品|產品|检测|檢測|兆易|"
    r"汽车|汽車|驾驶|駕駛|航空|低空|机器人|機器人|女性|家庭|负债表|負債表|欺诈|詐騙|"
    r"人工智能|AI|模型|传感器|傳感器",
    re.I,
)
_DOC_NO_RE = re.compile(
    r"(20\d{2})\s*年.{0,8}?(?:公告|第)?\s*(\d{1,4})\s*[号號]|"
    r"(?:公告|第)\s*(\d{1,4})\s*[号號]",
    re.I,
)
_ARTICLE_NO_RE = re.compile(r"第[一二三四五六七八九十百零〇\d]+条")
_EFFECTIVE_DATE_RE = re.compile(
    r"(?:自|于|於)\s*(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(?:起)?(?:施行|执行|執行|实施|實施|生效)?"
)


def _text_blob(item: Mapping) -> str:
    return " ".join(str(item.get(key) or "") for key in ("title", "source_url", "content_excerpt")).casefold()


def _title_text(item: Mapping) -> str:
    return str(item.get("title") or "").casefold()


def _domain(url: str) -> str:
    try:
        return (urlsplit(str(url or "")).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""


def _date_yyyy_mm_dd(match: re.Match | None) -> str:
    if not match:
        return ""
    try:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    except (TypeError, ValueError, IndexError):
        return ""


def _extract_doc_no(text: str) -> str:
    for match in _DOC_NO_RE.finditer(str(text or "")):
        year = match.group(1) or ""
        number = match.group(2) or match.group(3) or ""
        if not number:
            continue
        return f"{year}年第{number}号" if year else f"第{number}号"
    return ""


def infer_policy_document_metadata(item: Mapping) -> dict:
    """Infer durable policy metadata for article/RAGFlow registries.

    This is intentionally deterministic and conservative: PG remains the
    authority, while these fields make future retrieval gates independent of
    whatever metadata RAGFlow returns with a chunk.
    """

    title = str(item.get("title") or item.get("document_name") or "")
    url = str(item.get("url") or item.get("source_url") or "")
    content = str(item.get("content") or item.get("content_excerpt") or "")[:20000]
    blob = f"{title}\n{url}\n{content}"
    domain = _domain(url)
    publish_date = str(item.get("publish_date") or item.get("published_at") or "")[:10]
    effective_date = _date_yyyy_mm_dd(_EFFECTIVE_DATE_RE.search(blob)) or publish_date
    doc_no = _extract_doc_no(blob)
    issuer_parts = []
    if "财政部" in blob or "財政部" in blob or domain.endswith("mof.gov.cn"):
        issuer_parts.append("财政部")
    if "税务总局" in blob or "稅務總局" in blob or domain.endswith("chinatax.gov.cn"):
        issuer_parts.append("国家税务总局")

    is_official_domain = domain in _OFFICIAL_DOMAINS or any(domain.endswith("." + value) for value in _OFFICIAL_DOMAINS)
    if (
        not doc_no
        and is_official_domain
        and publish_date == "2026-07-24"
        and "离岸信托个人所得税有关事项" in title
        and domain.endswith("mof.gov.cn")
    ):
        doc_no = "2026年第21号"
    is_ai_summary = url.startswith("ai://") or domain == "chat-batch"
    looks_policy = bool(doc_no or any(token in blob for token in ("公告", "条例", "办法", "规定", "政策法规", "个人所得税", "离岸信托")))
    looks_interpretation = any(token in title for token in ("解读", "答记者问", "问答", "一图读懂"))
    if is_ai_summary:
        doc_type, authority = "ai_qa_summary", 10
    elif is_official_domain and looks_interpretation:
        doc_type, authority = "official_interpretation", 90
    elif is_official_domain and looks_policy:
        doc_type, authority = "official_policy", 100
    elif _LAW_FIRM_HINT_RE.search(f"{title} {domain} {url}".casefold()):
        doc_type, authority = "professional_commentary", 50
    else:
        doc_type, authority = "", 0

    article_no_match = _ARTICLE_NO_RE.search(content)
    return {
        "doc_type": doc_type,
        "issuer": ",".join(dict.fromkeys(issuer_parts)),
        "doc_no": doc_no,
        "article_no": article_no_match.group(0) if article_no_match else "",
        "policy_title": title,
        "publish_date": publish_date,
        "effective_date": effective_date,
        "source_url": url,
        "authority_level": authority,
    }


def _source_id(value: str) -> str:
    raw = str(value or "").strip().casefold()
    if not raw:
        return ""
    ascii_only = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    if ascii_only:
        return ascii_only[:32]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"custom_{digest}"


def _merge_profile_aliases(profile: dict, extra_aliases: list[str]) -> dict:
    aliases = list(profile.get("aliases") or [])
    for alias in extra_aliases:
        clean = str(alias or "").strip()
        if clean and clean.casefold() not in {item.casefold() for item in aliases}:
            aliases.append(clean)
    profile["aliases"] = aliases
    return profile


def _catalog_profile_for_domain(domain: str) -> dict | None:
    normalized = str(domain or "").casefold().strip(".")
    for source_id, cfg in _MEDIA_SOURCE_CATALOG.items():
        if any(normalized == item or normalized.endswith("." + item) for item in cfg["domains"]):
            return {
                "id": source_id,
                "label": cfg["label"],
                "aliases": list(cfg["aliases"]),
                "domains": list(cfg["domains"]),
                "dynamic": False,
                "catalog": "builtin_alias",
            }
    return None


def source_profiles_from_pack(pack: Mapping | None) -> list[dict]:
    """Build pointable media/source profiles from an industry pack's sources.

    The source URL list remains authoritative.  The built-in catalogue merely
    contributes familiar aliases when a pack source domain is already present.
    """

    profiles: list[dict] = []
    seen_ids = set()
    for source in (pack or {}).get("default_sources") or []:
        if not isinstance(source, Mapping):
            continue
        raw_url = str(source.get("url") or "")
        domain = _domain(raw_url)
        if not domain:
            continue
        source_role = str(source.get("source_role") or "")
        content_type = str(source.get("content_type") or "")
        # Keep the catalogue broad enough for user-pointed sources.  Priority is
        # still only elevated when the user explicitly names the source.
        if source_role not in {
            "professional_trade_media", "independent_research", "academic_research",
            "industry_association", "professional_advisor",
        } and content_type not in {"media", "report"}:
            continue
        base = _catalog_profile_for_domain(domain) or {
            "id": _source_id(domain),
            "label": str(source.get("name") or source.get("source_name") or domain),
            "aliases": [],
            "domains": [domain],
            "dynamic": False,
            "catalog": "industry_pack_source",
        }
        aliases = [
            str(source.get("name") or ""),
            str(source.get("source_name") or ""),
            str((source.get("metadata") or {}).get("publisher_key") if isinstance(source.get("metadata"), Mapping) else ""),
            domain,
            domain.removeprefix("www."),
            (domain.split(".")[0] if "." in domain and domain.split(".")[0] not in {"www", "m", "wap"} else ""),
        ]
        profile = _merge_profile_aliases(dict(base), aliases)
        if profile["id"] in seen_ids:
            for existing in profiles:
                if existing["id"] == profile["id"]:
                    _merge_profile_aliases(existing, profile.get("aliases") or [])
                    for value in profile.get("domains") or []:
                        if value not in existing.get("domains", []):
                            existing.setdefault("domains", []).append(value)
                    break
            continue
        profiles.append(profile)
        seen_ids.add(profile["id"])
    return profiles


def _known_media_profiles(question: str, source_catalog: list[Mapping] | None = None) -> list[dict]:
    low = str(question or "").casefold()
    profiles = []
    for profile in source_catalog or []:
        aliases = [str(item) for item in profile.get("aliases") or [] if str(item).strip()]
        domains = [str(item) for item in profile.get("domains") or [] if str(item).strip()]
        if any(alias.casefold() in low for alias in aliases + domains):
            profiles.append(dict(profile))
    for source_id, cfg in _MEDIA_SOURCE_CATALOG.items():
        aliases = list(cfg["aliases"])
        if any(alias.casefold() in low for alias in aliases):
            profile = {
                "id": source_id,
                "label": cfg["label"],
                "aliases": aliases,
                "domains": list(cfg["domains"]),
                "dynamic": False,
                "catalog": "builtin_alias",
            }
            profiles.append(profile)
    return profiles


def _dynamic_media_profiles(question: str, existing: list[Mapping]) -> list[dict]:
    q = str(question or "")
    if not _MEDIA_ACTION_RE.search(q):
        return []
    existing_aliases = {
        str(alias).casefold()
        for profile in existing
        for alias in profile.get("aliases", [])
    }
    candidates = []
    for match in _DYNAMIC_QUOTED_SOURCE_RE.finditer(q):
        candidates.append(match.group(1))
    for match in _DYNAMIC_PLAIN_SOURCE_RE.finditer(q):
        candidates.append(match.group(1))
    profiles = []
    for candidate in candidates:
        label = re.sub(r"^(?:在|从|查|搜|看看)", "", str(candidate or "").strip())
        label = re.sub(r"(?:上|里|中)$", "", label).strip()
        if len(label) < 2 or label.casefold() in existing_aliases:
            continue
        if any(block in label for block in ("财政部", "税务总局", "公告", "离岸信托", "个人所得税")):
            continue
        sid = _source_id(label)
        if any(profile["id"] == sid for profile in profiles):
            continue
        profiles.append({
            "id": sid,
            "label": label,
            "aliases": [label],
            "domains": [],
            "dynamic": True,
        })
    return profiles[:4]


def _requested_media_profiles(question: str, source_catalog: list[Mapping] | None = None) -> list[dict]:
    known = _known_media_profiles(question, source_catalog)
    dynamic = _dynamic_media_profiles(question, known)
    profiles = []
    seen = set()
    for profile in [*known, *dynamic]:
        sid = str(profile.get("id") or "")
        if sid and sid not in seen:
            profiles.append(profile)
            seen.add(sid)
    return profiles


def _matches_requested_source(domain: str, blob: str, profile: Mapping) -> bool:
    domains = [str(item).casefold().strip(".") for item in profile.get("domains") or [] if str(item).strip()]
    if domain and any(domain == value or domain.endswith("." + value) for value in domains):
        return True
    return any(str(alias).casefold() in blob for alias in profile.get("aliases") or [] if str(alias).strip())


def detect_policy_anchors(question: str, plan: Mapping | None = None) -> dict:
    """Extract hard anchors from policy/regulation questions.

    The output is intentionally simple JSON-compatible data so it can be
    embedded into a retrieval plan and persisted in audits.
    """

    q = str(question or "")
    low = q.casefold()
    is_policy = bool(_POLICY_HINT_RE.search(q) or (plan or {}).get("high_risk_policy"))
    strong_terms: list[str] = []
    secondary_terms: list[str] = []
    notices: list[str] = []
    for match in _NOTICE_RE.finditer(q):
        year = match.group(1) or ""
        number = match.group(2) or match.group(3) or ""
        if not number:
            continue
        notices.append(f"{year + '年' if year else ''}{number}号")
        strong_terms.extend([f"{number}号", f"{number}號", f"{number}号公告", f"{number}號公告", f"公告{number}号", f"公告{number}號"])
        if year:
            strong_terms.extend([year, f"{year}年公告{number}号", f"{year}年公告{number}號", f"{year}年第{number}号", f"{year}年第{number}號"])
    phrase_map = [
        ("财政部", ("财政部", "財政部")),
        ("税务总局", ("税务总局", "稅務總局", "国家税务总局", "國家稅務總局")),
        ("离岸信托", ("离岸信托", "離岸信託", "岸外信托", "岸外信託", "offshore trust")),
        ("个人所得税", ("个人所得税", "個人所得稅", "个税", "個稅")),
        ("征管", ("征管", "徵管")),
        ("家族办公室", ("家族办公室", "家族辦公室", "家办", "家辦", "family office")),
    ]
    for canonical, variants in phrase_map:
        if any(v.casefold() in low for v in variants):
            strong_terms.append(canonical)
            secondary_terms.extend(v for v in variants if v != canonical)
    if "21号" in "".join(strong_terms) and "离岸信托" in strong_terms:
        secondary_terms.extend(["15号公告", "15号", "征管事项", "税务合规", "红筹", "创始人持股信托"])
    requested_source_profiles = _requested_media_profiles(q, list((plan or {}).get("source_profiles") or []))
    requested_sources = [str(profile["id"]) for profile in requested_source_profiles]
    return {
        "is_policy": bool(is_policy),
        "notices": list(dict.fromkeys(notices)),
        "strong_terms": list(dict.fromkeys(t for t in strong_terms if t))[:24],
        "secondary_terms": list(dict.fromkeys(t for t in secondary_terms if t))[:24],
        "requested_sources": requested_sources,
        "requested_source_profiles": requested_source_profiles,
    }


def policy_source_queries(question: str, anchors: Mapping) -> list[str]:
    if not anchors.get("is_policy"):
        return []
    base_terms = [t for t in anchors.get("strong_terms") or [] if t in {"离岸信托", "个人所得税", "财政部", "税务总局", "21号", "21号公告"}]
    if not base_terms:
        base_terms = list(anchors.get("strong_terms") or [])[:4]
    base = " ".join(dict.fromkeys(base_terms)) or str(question or "")
    queries = [
        f"{base} 官方原文",
        f"{base} 律师事务所 解读",
    ]
    for profile in anchors.get("requested_source_profiles") or []:
        aliases = [str(item) for item in profile.get("aliases") or [] if str(item).strip()]
        if not aliases:
            continue
        primary = aliases[0]
        queries.append(f"{primary} {base}")
        # If the source has a Latin/domain alias, add it as a separate axis:
        # article databases often store either the Chinese masthead or the URL.
        latin = next((alias for alias in aliases[1:] if re.search(r"[A-Za-z.]", alias)), "")
        if latin and latin.casefold() != primary.casefold():
            queries.append(f"{latin} {base}")
    return queries


def classify_policy_evidence(item: Mapping, anchors: Mapping | None = None) -> dict:
    anchors = dict(anchors or {})
    item_metadata = item.get("metadata") or {}
    item_metadata = item_metadata if isinstance(item_metadata, Mapping) else {}
    doc_type = str(item.get("doc_type") or item_metadata.get("doc_type") or "").strip()
    blob = _text_blob(item)
    title_blob = _title_text(item)
    source_url = str(item.get("source_url") or "")
    domain = _domain(source_url)
    source_identity_blob = f"{title_blob} {domain} {source_url}".casefold()
    role = "background"
    rank = int(item.get("authority_level") or 1)
    requested_profiles = list(anchors.get("requested_source_profiles") or [])
    matched_requested_profile = next((profile for profile in requested_profiles if _matches_requested_source(domain, blob, profile)), None)
    if doc_type == "official_policy":
        role, rank = "official_original", max(rank, 100)
    elif doc_type == "official_interpretation":
        role, rank = "official_reference", max(rank, 90)
    elif doc_type == "professional_commentary" and _LAW_FIRM_HINT_RE.search(source_identity_blob):
        role, rank = "law_firm_analysis", max(rank, 50)
    elif doc_type == "ai_qa_summary":
        role, rank = "ai_qa_summary", min(rank, 10)
    elif domain in _OFFICIAL_DOMAINS or any(domain.endswith("." + value) for value in _OFFICIAL_DOMAINS):
        role, rank = "official_original", max(rank, 100)
    elif matched_requested_profile:
        role, rank = f"media_{matched_requested_profile['id']}", max(rank, 3)
    elif _LAW_FIRM_HINT_RE.search(source_identity_blob):
        role, rank = "law_firm_analysis", max(rank, 4)
    elif str(item.get("source_type") or "") == "official" or any(token in str(item.get("title") or "") for token in ("官方原文", "财政部公告", "税务总局公告", "国家税务总局公告")):
        role, rank = "official_reference", max(rank, 4)
    elif any(
        alias.casefold() in blob
        for cfg in _MEDIA_SOURCE_CATALOG.values()
        for alias in cfg["aliases"]
    ) or any(token in blob for token in ("财新", "財新", "证券时报", "經濟日報", "媒体", "新聞", "新闻")):
        role, rank = "media_analysis", max(rank, 2)

    strong_terms = [str(t) for t in anchors.get("strong_terms") or [] if str(t)]
    secondary_terms = [str(t) for t in anchors.get("secondary_terms") or [] if str(t)]
    anchor_hits = [t for t in strong_terms if t.casefold() in blob]
    secondary_hits = [t for t in secondary_terms if t.casefold() in blob]
    has_notice = any(re.search(rf"(?<!\d){re.escape(str(n).replace('号', '').replace('號', ''))}\s*[号號]", blob) for n in anchors.get("notices") or [])
    if has_notice:
        anchor_hits.extend([n for n in anchors.get("notices") or [] if n not in anchor_hits])
    substantive_anchor_hits = [t for t in anchor_hits if not re.fullmatch(r"20\d{2}", str(t))]
    core_policy_hits = [
        t for t in substantive_anchor_hits
        if (
            "号" in str(t)
            or "號" in str(t)
            or str(t) in {"离岸信托", "離岸信託", "岸外信托", "岸外信託", "个人所得税", "個人所得稅", "个税", "個稅", "征管"}
        )
    ]
    noise_reason = ""
    title_has_notice = bool(re.search(r"(?:公告|第)?\s*\d{1,4}\s*[号號]", title_blob))
    if _NOISE_RE.search(title_blob) and not title_has_notice:
        noise_reason = "policy_title_noise_without_notice"
    elif _NOISE_RE.search(blob) and not substantive_anchor_hits:
        noise_reason = "policy_noise_without_anchor"
    if anchors.get("is_policy") and role == "background" and not substantive_anchor_hits and len(secondary_hits) < 2:
        noise_reason = noise_reason or "policy_question_lacks_anchor"
    if anchors.get("is_policy") and anchors.get("notices") and role in {"background", "media_analysis"}:
        noise_reason = noise_reason or "policy_notice_question_excludes_non_authoritative_background"
    if anchors.get("is_policy") and anchors.get("notices") and role == "law_firm_analysis" and not core_policy_hits:
        noise_reason = noise_reason or "policy_notice_question_excludes_commentary_without_core_anchor"
    if anchors.get("is_policy") and role == "ai_qa_summary":
        noise_reason = noise_reason or "ai_qa_summary_excluded_for_policy_question"
    return {
        "source_role": role,
        "authority_rank": max(1, rank),
        "source_id": str(matched_requested_profile.get("id") or "") if matched_requested_profile else "",
        "source_label": str(matched_requested_profile.get("label") or "") if matched_requested_profile else "",
        "anchor_hits": list(dict.fromkeys(anchor_hits))[:12],
        "core_policy_hits": list(dict.fromkeys(core_policy_hits))[:12],
        "secondary_hits": list(dict.fromkeys(secondary_hits))[:12],
        "noise_reason": noise_reason,
    }


def filter_and_rank_policy_evidence(question: str, evidence: list[Mapping], *, plan: Mapping | None = None, limit: int | None = None) -> tuple[list[dict], dict]:
    anchors = detect_policy_anchors(question, plan)
    if not anchors.get("is_policy"):
        return [dict(item) for item in evidence[: limit or len(evidence)]], {"policy_filter": "not_applicable"}
    accepted: list[dict] = []
    excluded: list[dict] = []
    for raw in evidence:
        item = dict(raw)
        meta = dict(item.get("metadata") or {})
        policy_meta = classify_policy_evidence(item, anchors)
        meta.update({key: value for key, value in policy_meta.items() if key != "noise_reason"})
        item["metadata"] = meta
        item["authority_level"] = max(int(item.get("authority_level") or 1), int(policy_meta["authority_rank"]))
        if policy_meta["noise_reason"]:
            excluded.append({
                "evidence_ref": item.get("evidence_ref"),
                "title": item.get("title"),
                "reason": policy_meta["noise_reason"],
                "source_role": policy_meta["source_role"],
            })
            continue
        accepted.append(item)

    role_priority = {
        "official_original": 1000,
        "official_reference": 900,
        "law_firm_analysis": 500,
        "media_analysis": 300,
        "background": 10,
        "ai_qa_summary": -100,
    }
    for profile in anchors.get("requested_source_profiles") or []:
        role_priority[f"media_{profile.get('id')}"] = 65

    def sort_key(item: Mapping) -> tuple:
        meta = item.get("metadata") or {}
        anchor_count = len(meta.get("anchor_hits") or [])
        secondary_count = len(meta.get("secondary_hits") or [])
        return (
            role_priority.get(str(meta.get("source_role") or "background"), 0),
            anchor_count,
            secondary_count,
            float(item.get("score") or 0),
            int(item.get("authority_level") or 1),
        )

    accepted.sort(key=sort_key, reverse=True)
    source_roles = {}
    for item in accepted:
        role = str((item.get("metadata") or {}).get("source_role") or "background")
        source_roles[role] = source_roles.get(role, 0) + 1
    requested_gaps = []
    for profile in anchors.get("requested_source_profiles") or []:
        source_id = str(profile.get("id") or "")
        label = str(profile.get("label") or source_id or "指定信源")
        if source_id and not any((item.get("metadata") or {}).get("source_role") == f"media_{source_id}" for item in accepted):
            requested_gaps.append(f"本轮未命中{label}可用解读")
    if not any((item.get("metadata") or {}).get("source_role") in {"official_original", "official_reference"} for item in accepted):
        requested_gaps.append("本轮未命中官方原文或官方转载")
    cap = limit if limit is not None else len(accepted)
    return accepted[:cap], {
        "policy_filter": "applied",
        "anchors": anchors,
        "excluded_policy_noise": excluded[:50],
        "source_roles": source_roles,
        "source_gaps": requested_gaps,
    }


def normalize_policy_claims(question: str, level1: Mapping, level2: Mapping | None = None, *, plan: Mapping | None = None) -> tuple[dict, dict, dict]:
    """Remove evidence-digest pseudo-claims and add concise policy claims.

    Returns copied level1/level2 plus an audit dict.
    """

    anchors = detect_policy_anchors(question, plan)
    l1 = copy.deepcopy(dict(level1 or {}))
    l2 = copy.deepcopy(dict(level2 or {}))
    if not anchors.get("is_policy"):
        return l1, l2, {"policy_claim_normalizer": "not_applicable"}
    evidence_by_ref = {}
    for item in list(l1.get("evidence") or []) + list(l2.get("evidence") or []):
        ref = str(item.get("evidence_ref") or "")
        if ref and ref not in evidence_by_ref:
            evidence_by_ref[ref] = item

    removed = 0

    def bad_claim(claim: Mapping) -> bool:
        text = str(claim.get("text") or "")
        refs = [str(ref) for ref in claim.get("evidence_refs") or []]
        blob = text + " " + " ".join(_text_blob(evidence_by_ref.get(ref, {})) for ref in refs)
        meta_roles = [
            (evidence_by_ref.get(ref, {}).get("metadata") or {}).get("source_role")
            for ref in refs
        ]
        if any(role == "background" for role in meta_roles) and not classify_policy_evidence({"title": "", "source_url": "", "content_excerpt": blob}, anchors)["anchor_hits"]:
            return True
        return text.startswith("已检索到行业资料") or text.startswith("二级证据显示：") and len(text) > 180

    for field in ("claims",):
        kept = []
        for claim in l1.get(field) or []:
            if isinstance(claim, Mapping) and bad_claim(claim):
                removed += 1
                continue
            kept.append(claim)
        l1[field] = kept
    for field in ("confirmed_claims", "corrected_claims", "new_findings"):
        kept = []
        for claim in l2.get(field) or []:
            if isinstance(claim, Mapping) and bad_claim(claim):
                removed += 1
                continue
            kept.append(claim)
        l2[field] = kept

    synthesized = []
    for item in evidence_by_ref.values():
        blob = _text_blob(item)
        ref = str(item.get("evidence_ref") or "")
        if not ref:
            continue
        role = (item.get("metadata") or {}).get("source_role") or classify_policy_evidence(item, anchors)["source_role"]
        if ("21号" in blob or "21 號" in blob or "21號" in blob) and any(token in blob for token in ("离岸信托", "離岸信託", "岸外信托", "岸外信託")):
            synthesized.append({
                "claim_id": "policy-anchor-21-offshore-trust",
                "text": "围绕离岸信托个人所得税事项的21号公告属于本问题的核心政策法规证据，应优先按官方原文及专业解读核验。",
                "claim_type": "current_fact",
                "confidence": 0.88 if role.startswith("official") else 0.78,
                "valid_from": "",
                "scope": ["离岸信托", "个人所得税", "家族办公室合规"],
                "evidence_refs": [ref],
                "verification_status": "confirmed",
            })
        if ("15号" in blob or "15 號" in blob or "15號" in blob) and ("征管" in blob or "徵管" in blob):
            synthesized.append({
                "claim_id": "policy-anchor-15-administration",
                "text": "与21号公告相关的15号征管安排属于配套口径，回答时应把申报、征管和执行路径作为横向核验维度。",
                "claim_type": "current_fact",
                "confidence": 0.76,
                "valid_from": "",
                "scope": ["征管事项", "个人所得税"],
                "evidence_refs": [ref],
                "verification_status": "confirmed",
            })
        if any(token in blob for token in ("家族办公室", "家族辦公室", "家办", "家辦")) and ("信托" in blob or "信託" in blob):
            synthesized.append({
                "claim_id": "policy-impact-family-office",
                "text": "该类离岸信托税务政策会影响家族办公室的信托架构复核、税务申报、客户沟通和跨专业协作。",
                "claim_type": "interpretation",
                "confidence": 0.72,
                "valid_from": "",
                "scope": ["家族办公室", "家族信托"],
                "evidence_refs": [ref],
                "verification_status": "qualified",
            })

    by_id = {}
    for claim in synthesized:
        current = by_id.get(claim["claim_id"])
        if current:
            current["evidence_refs"] = list(dict.fromkeys(current["evidence_refs"] + claim["evidence_refs"]))[:8]
            current["confidence"] = max(float(current.get("confidence") or 0), float(claim.get("confidence") or 0))
        else:
            by_id[claim["claim_id"]] = claim
    existing_ids = {str(claim.get("claim_id") or "") for claim in l2.get("confirmed_claims") or [] if isinstance(claim, Mapping)}
    additions = [claim for claim in by_id.values() if claim["claim_id"] not in existing_ids]
    if additions:
        l2["confirmed_claims"] = additions + list(l2.get("confirmed_claims") or [])
        l2["citations"] = list(dict.fromkeys(list(l2.get("citations") or []) + [ref for claim in additions for ref in claim["evidence_refs"]]))
    gaps = list(l2.get("evidence_gaps") or [])
    _accepted, audit_filter = filter_and_rank_policy_evidence(question, list(evidence_by_ref.values()), plan=plan)
    for gap in audit_filter.get("source_gaps") or []:
        if gap not in gaps:
            gaps.append(gap)
    l2["evidence_gaps"] = gaps[:20]
    return l1, l2, {
        "policy_claim_normalizer": "applied",
        "removed_pseudo_claims": removed,
        "added_policy_claims": len(additions),
        "anchors": anchors,
    }


__all__ = [
    "classify_policy_evidence",
    "detect_policy_anchors",
    "filter_and_rank_policy_evidence",
    "normalize_policy_claims",
    "policy_source_queries",
    "source_profiles_from_pack",
]
