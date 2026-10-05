#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pack-isolated article and web retrieval for unified QA level one."""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import re
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from intel_topics import _classification_admitted
from qa_policy_evidence import detect_policy_anchors, infer_policy_document_metadata


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{1,48}|[\u3400-\u9fff]{2,24}")
_STOP = {
    "什么", "哪些", "如何", "怎么", "是否", "请问", "介绍", "分析", "一下", "有关",
    "关于", "目前", "当前", "最近", "这个", "那个", "以及", "and", "the", "what",
    "已经", "了吗", "运行", "开始", "现在", "最新", "有没有", "已经在", "运行了",
}
_BROAD_INDUSTRY_TERMS = {
    "行业", "产业", "市场", "政策", "公告", "法规", "影响", "风险", "应对",
    "内容", "具体", "资料", "材料", "证据", "检索", "新闻", "资讯", "研究",
}
_ANCHOR_NOISE_FRAGMENTS = (
    "已经", "了吗", "运行", "目前", "现在", "最新", "是否", "什么", "哪些",
    "如何", "怎么", "有关", "关于",
)
_QUERY_TARGET_TERMS = {
    "哪个", "哪家", "哪个公司", "公司", "企业", "机构", "主体", "对象", "是谁", "什么",
}
_AMOUNT_UNIT_TERMS = {"亿元", "亿", "万元", "万", "千元", "人民币", "美元", "港元", "元"}
_AMOUNT_RE = re.compile(
    r"(?:(?:近|约|超|逾|超过|累计|合计|达到|获得|完成|融了|融资|募资|投资)\s*)?"
    r"\d+(?:\.\d+)?\s*(?:亿元|亿|万元|万|千元|人民币|美元|港元|元)",
    re.I,
)
_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid"}
_MOJIBAKE_MARKERS = ("Ã", "Â", "å", "ä", "ç", "æ", "è", "é", "ï¼", "ã")
_POLICY_NOTICE_RE = re.compile(
    r"(20\d{2})\s*年.{0,12}?(?:公告|第)?\s*(\d{1,4})\s*[号號]|"
    r"(?:公告|第)?\s*(\d{1,4})\s*[号號](?:公告)?",
    re.I,
)
_POLICY_ISSUER_TERMS = (
    "财政部", "財政部", "税务总局", "稅務總局", "国家税务总局", "國家稅務總局",
    "税务局", "稅務局", "国务院", "國務院", "证监会", "證監會", "银保监", "銀保監",
)
_POLICY_TOPIC_TERMS = (
    "离岸信托", "離岸信託", "岸外信托", "岸外信託", "个人所得税", "個人所得稅",
    "个税", "個稅", "征管", "徵管", "家族办公室", "家族辦公室", "家办", "家辦",
    "家族企业", "家族企業", "离岸资产", "離岸資產", "境外资产", "境外資產",
    "信托", "信託", "税收", "稅收", "税务", "稅務", "合规", "合規",
)


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_http_url(value: str) -> str:
    raw = html.unescape(str(value or "").strip())
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        return ""
    if parts.username or parts.password:
        return ""
    host = parts.hostname.casefold().rstrip(".")
    try:
        # Public links may resolve to a private address later, but literal
        # private/loopback IPs are never exposed as external-search results.
        address = ipaddress.ip_address(host)
        if not address.is_global:
            return ""
    except ValueError:
        pass
    try:
        port = parts.port
    except ValueError:
        return ""
    default_port = (parts.scheme.casefold() == "http" and port == 80) or (parts.scheme.casefold() == "https" and port == 443)
    netloc = host if not port or default_port else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.casefold() not in _TRACKING])
    return urlunsplit((parts.scheme.casefold(), netloc, path, query, ""))


def _json(value, default):
    if isinstance(value, (dict, list)):
        return value
    try:
        decoded = json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return decoded if isinstance(decoded, type(default)) else default


def repair_mojibake(value: str) -> str:
    """Repair UTF-8 bytes that were previously decoded as Latin-1."""
    text = str(value or "")
    marker_count = sum(text.count(marker) for marker in _MOJIBAKE_MARKERS)
    if marker_count < 1:
        return text

    def try_repair(part: str) -> str:
        try:
            candidate = part.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return part
        before_cjk = len(re.findall(r"[\u3400-\u9fff]", part))
        after_cjk = len(re.findall(r"[\u3400-\u9fff]", candidate))
        before_markers = sum(part.count(marker) for marker in _MOJIBAKE_MARKERS)
        after_markers = sum(candidate.count(marker) for marker in _MOJIBAKE_MARKERS)
        # Pure punctuation fragments such as ``ï¼`` contain no CJK ideograph,
        # so CJK growth alone would leave them corrupted.  A strictly lower
        # mojibake-marker count is sufficient as long as decoding succeeds and
        # it does not remove existing CJK text.
        return candidate if (
            after_markers < before_markers
            and after_cjk >= before_cjk
        ) else part

    def repair_part(part: str) -> str:
        repaired = try_repair(part)
        if repaired != part:
            return repaired
        # A line may contain a valid Chinese label followed by a corrupted
        # Latin-1-decoded fragment.  Repair each byte-preserving span instead
        # of abandoning the whole line when one valid CJK character cannot be
        # encoded as Latin-1.
        return re.sub(r"[\x00-\xff]+", lambda match: try_repair(match.group(0)), part)

    # Long pages can contain a valid Chinese footer next to a corrupted body;
    # repairing line-by-line preserves the valid portion instead of making the
    # whole conversion fail on one non-Latin-1 character.
    repaired_lines = [repair_part(part) for part in text.split("\n")]

    # HTML extraction can insert a newline in the middle of a UTF-8 byte
    # sequence after the bytes were mis-decoded.  Such individual lines cannot
    # decode, but the consecutive marker-only fragments can be safely joined
    # and repaired as one byte-preserving span.
    joined_lines = []
    index = 0
    while index < len(repaired_lines):
        line = repaired_lines[index]
        has_markers = any(marker in line for marker in _MOJIBAKE_MARKERS)
        has_cjk = bool(re.search(r"[\u3400-\u9fff]", line))
        if not has_markers or has_cjk:
            joined_lines.append(line)
            index += 1
            continue
        fragments = [line]
        cursor = index + 1
        while cursor < len(repaired_lines):
            candidate_line = repaired_lines[cursor]
            if not any(marker in candidate_line for marker in _MOJIBAKE_MARKERS):
                break
            if re.search(r"[\u3400-\u9fff]", candidate_line):
                break
            fragments.append(candidate_line)
            cursor += 1
        combined = "".join(fragments)
        fixed = try_repair(combined)
        if fixed != combined:
            joined_lines.append(fixed)
        else:
            joined_lines.extend(fragments)
        index = cursor
    return "\n".join(joined_lines)


def _terms(values: Iterable[str]) -> list[str]:
    result = []
    for value in values:
        for token in _WORD_RE.findall(str(value or "")):
            token = token.casefold().strip("._-")
            if token and token not in _STOP and token not in result:
                result.append(token)
            if re.fullmatch(r"[\u3400-\u9fff]{4,}", token):
                for width in (2, 3, 4):
                    for index in range(len(token) - width + 1):
                        part = token[index:index + width]
                        if part not in _STOP and part not in result:
                            result.append(part)
    return result[:120]


def _anchor_terms(terms: Iterable[str]) -> list[str]:
    anchors = []
    for raw in terms:
        term = str(raw or "").strip().casefold()
        if not term or term in _STOP or term in _BROAD_INDUSTRY_TERMS or term in _QUERY_TARGET_TERMS:
            continue
        if any(fragment in term for fragment in _ANCHOR_NOISE_FRAGMENTS):
            continue
        if re.fullmatch(r"\d+", term):
            continue
        if re.fullmatch(r"[\u3400-\u9fff]{2}", term):
            continue
        if len(term) < 3:
            continue
        if term not in anchors:
            anchors.append(term)
    return anchors[:12]


def _semantic_anchor_terms(values: Iterable[str]) -> list[str]:
    anchors = []
    try:
        import jieba  # type: ignore
    except Exception:
        return anchors
    for value in values:
        cutter = getattr(jieba, "lcut", None)
        raw_tokens = cutter(str(value or "")) if callable(cutter) else list(jieba.cut(str(value or "")))
        for raw in raw_tokens:
            term = str(raw or "").strip().casefold()
            if not term or term in _STOP or term in _BROAD_INDUSTRY_TERMS or term in _QUERY_TARGET_TERMS:
                continue
            if any(fragment in term for fragment in _ANCHOR_NOISE_FRAGMENTS):
                continue
            if re.fullmatch(r"\d+|[，。！？；、,.!?;:：]+", term):
                continue
            if len(term) < 2:
                continue
            if term not in anchors:
                anchors.append(term)
    return anchors[:12]


def _amount_to_yi(value: str) -> float | None:
    raw = re.sub(r"\s+", "", str(value or "").casefold())
    match = re.search(r"(\d+(?:\.\d+)?)(亿元|亿|万元|万|千元|人民币|美元|港元|元)", raw)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2)
    if unit in {"亿元", "亿"}:
        return number
    if unit in {"万元", "万"}:
        return number / 10000
    if unit == "千元":
        return number / 100000
    return number / 100000000


def _amount_constraints(value: str) -> list[dict]:
    compact = re.sub(r"\s+", "", str(value or "").casefold())
    constraints = []
    seen = set()
    for match in _AMOUNT_RE.finditer(compact):
        raw = match.group(0)
        amount = _amount_to_yi(raw)
        if amount is None:
            continue
        approximate = bool(re.match(r"^(近|约|超|逾|超过)", raw))
        key = (round(amount, 6), approximate)
        if key in seen:
            continue
        seen.add(key)
        constraints.append({"raw": raw, "value_yi": amount, "approximate": approximate})
    return constraints[:8]


def _amount_context_terms(values: Iterable[str]) -> list[str]:
    terms = []
    for value in values:
        for token in _tokenize_query(str(value or "")):
            if token in _AMOUNT_UNIT_TERMS or token in _QUERY_TARGET_TERMS:
                continue
            if token not in terms:
                terms.append(token)
            if re.fullmatch(r"[\u4e00-\u9fff]{2,4}", token):
                root = token[0]
                if root not in terms:
                    terms.append(root)
    return terms[:12]


def _amount_constraint_hits(constraints: list[dict], text: str, *, context_terms: Iterable[str] | None = None) -> list[str]:
    if not constraints:
        return []
    hits = []
    compact = re.sub(r"\s+", "", str(text or "").casefold())
    context = [str(term or "").casefold() for term in (context_terms or []) if str(term or "").strip()]
    doc_amounts = []
    for match in _AMOUNT_RE.finditer(compact):
        raw = match.group(0)
        amount = _amount_to_yi(raw)
        if amount is not None:
            doc_amounts.append((raw, amount, match.start(), match.end()))
    for constraint in constraints:
        target = float(constraint.get("value_yi") or 0)
        if target <= 0:
            continue
        tolerance = 0.25 if constraint.get("approximate") else 0.08
        for raw, amount, start, end in doc_amounts:
            if abs(amount - target) / max(target, 1e-9) <= tolerance:
                if context:
                    window = compact[max(0, start - 30): min(len(compact), end + 30)]
                    if not any(term and term in window for term in context):
                        continue
                label = str(constraint.get("raw") or raw)
                if label not in hits:
                    hits.append(label)
                break
    return hits[:8]


def _tokenize_query(value: str) -> list[str]:
    try:
        import jieba  # type: ignore
        cutter = getattr(jieba, "lcut", None)
        raw_tokens = cutter(str(value or "")) if callable(cutter) else list(jieba.cut(str(value or "")))
    except Exception:
        raw_tokens = _WORD_RE.findall(str(value or ""))
    tokens: list[str] = []
    for raw in raw_tokens:
        term = str(raw or "").strip().casefold()
        if not term or term in _STOP or term in _QUERY_TARGET_TERMS:
            continue
        if re.fullmatch(r"\d+|[，。！？；、,.!?;:：]+", term):
            continue
        if len(term) < 2:
            continue
        if term not in tokens:
            tokens.append(term)
    return tokens[:32]


def _amount_variants(value: str) -> list[str]:
    compact = re.sub(r"\s+", "", str(value or "").casefold())
    variants: list[str] = []
    for match in _AMOUNT_RE.finditer(compact):
        raw = match.group(0)
        items = {raw}
        bare = re.sub(r"^(近|约|超|逾|超过|累计|合计|达到|获得|完成|融了|融资|募资|投资)", "", raw)
        if bare:
            items.add(bare)
        if bare.endswith("亿元"):
            items.add(bare[:-2] + "亿")
        if raw.endswith("亿元"):
            items.add(raw[:-2] + "亿")
        for item in items:
            if item and item not in variants:
                variants.append(item)
    return variants[:16]


def _query_phrases(values: Iterable[str]) -> list[str]:
    phrases: list[str] = []
    for value in values:
        text = re.sub(r"\s+", "", str(value or "").casefold())
        tokens = _tokenize_query(text)
        for width in (4, 3, 2):
            for index in range(0, max(0, len(tokens) - width + 1)):
                phrase = "".join(tokens[index:index + width])
                if len(phrase) >= 4 and phrase not in phrases:
                    phrases.append(phrase)
        for token in tokens:
            if len(token) >= 2 and token not in phrases:
                phrases.append(token)
    return phrases[:40]


def _notice_variants(year: str, number: str) -> list[str]:
    num = str(number or "").lstrip("0") or str(number or "")
    variants = [f"第{num}号", f"{num}号", f"公告{num}号", f"{num}号公告"]
    if year:
        variants.extend([
            f"{year}年第{num}号",
            f"{year}年{num}号",
            f"{year}年公告{num}号",
            f"{year}年第{num}號",
            f"{year}年{num}號",
        ])
    return list(dict.fromkeys(variants))


def _policy_query_context(plan: Mapping) -> str:
    parts = [str(plan.get("question") or "")]
    parts.extend(str(item) for item in plan.get("queries") or [])
    parts.extend(str(item) for item in plan.get("source_queries") or [])
    return " ".join(part for part in parts if part.strip())


def _policy_anchor_spec(plan: Mapping) -> dict:
    text = _policy_query_context(plan)
    anchors = detect_policy_anchors(str(plan.get("question") or text), plan)
    notices = []
    for match in _POLICY_NOTICE_RE.finditer(text):
        year = match.group(1) or ""
        number = match.group(2) or match.group(3) or ""
        if not number:
            continue
        notices.append({"year": year, "number": number.lstrip("0") or number, "variants": _notice_variants(year, number)})
    for notice in anchors.get("notices") or []:
        match = re.search(r"(?:(20\d{2})年)?\s*(\d{1,4})\s*[号號]", str(notice))
        if match:
            year, number = match.group(1) or "", match.group(2) or ""
            notices.append({"year": year, "number": number.lstrip("0") or number, "variants": _notice_variants(year, number)})
    deduped_notices = []
    seen = set()
    for notice in notices:
        key = (notice["year"], notice["number"])
        if key in seen:
            continue
        seen.add(key)
        deduped_notices.append(notice)
    issuers = [term for term in _POLICY_ISSUER_TERMS if term.casefold() in text.casefold()]
    topics = [term for term in _POLICY_TOPIC_TERMS if term.casefold() in text.casefold()]
    title_terms = [
        term for term in _terms([text])
        if len(term) >= 2 and term not in {"公告", "政策", "法规", "法規", "如何", "影响", "影響", "风险", "風險", "应对", "應對"}
    ][:24]
    return {
        "is_policy": bool(anchors.get("is_policy") or notices or issuers),
        "notices": deduped_notices[:8],
        "issuers": list(dict.fromkeys(issuers))[:8],
        "topics": list(dict.fromkeys(topics))[:12],
        "title_terms": title_terms,
    }


def _policy_match_score(row: Mapping, spec: Mapping) -> tuple[float, list[str]]:
    metadata_blob = " ".join(
        str(row.get(key) or "")
        for key in ("policy_doc_no", "policy_title", "policy_issuer", "policy_source_url", "title", "url")
    )
    content_blob = str(row.get("content") or "")[:5000]
    blob = f"{metadata_blob} {content_blob}".casefold()
    score = 0.0
    reasons = []
    doc_type = str(row.get("policy_doc_type") or "")
    if doc_type == "official_policy":
        score += 1000
        reasons.append("官方原文")
    elif doc_type == "official_interpretation":
        score += 700
        reasons.append("官方解读")
    for notice in spec.get("notices") or []:
        variants = [str(item) for item in notice.get("variants") or [] if str(item)]
        exact_doc_no = str(row.get("policy_doc_no") or "")
        if exact_doc_no and any(variant == exact_doc_no for variant in variants):
            score += 900
            reasons.append(f"法规号精确命中：{exact_doc_no}")
        elif any(variant.casefold() in blob for variant in variants):
            score += 520
            reasons.append("法规号命中")
    issuer_hits = [term for term in spec.get("issuers") or [] if str(term).casefold() in blob]
    topic_hits = [term for term in spec.get("topics") or [] if str(term).casefold() in blob]
    title_hits = [term for term in spec.get("title_terms") or [] if str(term).casefold() in metadata_blob.casefold()]
    if issuer_hits:
        score += 70 * min(3, len(issuer_hits))
        reasons.append("发布机关命中：" + "、".join(issuer_hits[:3]))
    if topic_hits:
        score += 80 * min(4, len(topic_hits))
        reasons.append("主题词命中：" + "、".join(topic_hits[:4]))
    if title_hits:
        score += 35 * min(4, len(title_hits))
        reasons.append("标题命中：" + "、".join(title_hits[:4]))
    score += min(100, int(row.get("authority_level") or 1))
    return score, list(dict.fromkeys(reasons))


def _article_evidence(row: Mapping, *, score: float, method: str, reason: str, source_type: str = "article") -> dict:
    article_id = int(row.get("id") or 0)
    content = " ".join(repair_mojibake(row.get("content") or row.get("preview") or "").split())
    policy_meta = infer_policy_document_metadata(row)
    policy_meta.update({
        "doc_type": str(row.get("policy_doc_type") or policy_meta.get("doc_type") or ""),
        "issuer": str(row.get("policy_issuer") or policy_meta.get("issuer") or ""),
        "doc_no": str(row.get("policy_doc_no") or policy_meta.get("doc_no") or ""),
        "article_no": str(row.get("policy_article_no") or policy_meta.get("article_no") or ""),
        "policy_title": str(row.get("policy_title") or policy_meta.get("policy_title") or ""),
        "effective_date": str(row.get("policy_effective_date") or policy_meta.get("effective_date") or ""),
        "source_url": str(row.get("policy_source_url") or policy_meta.get("source_url") or ""),
    })
    authority_level = int(row.get("authority_level") or 1)
    if policy_meta.get("doc_type") == "official_policy":
        authority_level = max(authority_level, 100)
    elif policy_meta.get("doc_type") == "official_interpretation":
        authority_level = max(authority_level, 90)
    elif policy_meta.get("doc_type") == "professional_commentary":
        authority_level = max(authority_level, 50)
    elif policy_meta.get("doc_type") == "ai_qa_summary":
        authority_level = min(authority_level, 10)
    article_url = canonical_http_url(str(row.get("url") or ""))
    policy_url = canonical_http_url(str(row.get("policy_source_url") or policy_meta.get("source_url") or ""))
    return {
        "evidence_ref": f"page:{article_id}" if source_type == "page_context" else f"article:{article_id}",
        "source_type": source_type,
        "title": repair_mojibake(row.get("title") or "未命名文章")[:1000],
        "source_url": policy_url or article_url,
        "content_excerpt": content[:5000],
        "published_at": str(row.get("publish_date") or "") or None,
        "fetched_at": str(row.get("first_crawled") or "") or None,
        "article_id": article_id,
        "ragflow_kb_id": None,
        "document_id": None,
        "chunk_id": None,
        "score": round(max(0.0, float(score)), 6),
        "authority_level": authority_level,
        "retrieval_method": method,
        "match_reason": str(reason)[:1000],
        "relationship": "context" if source_type == "page_context" else "supports",
        "metadata": {
            "domain": str(row.get("domain") or ""),
            "category": str(row.get("final_category") or ""),
            "article_url": article_url,
            "article_detail": f"/article-management/api/article/{article_id}" if article_id else "",
            "matched_keywords": _json(row.get("matched_keywords_json"), []),
            "topic_tags": _json(row.get("topic_tags_json"), []),
            **{key: value for key, value in policy_meta.items() if value not in ("", 0, None)},
        },
    }


class ArticleRetriever:
    def __init__(self, database, *, semantic_search: Callable | None = None):
        self.database = database
        self.semantic_search = semantic_search

    def _active_activation(self, pack_id: str) -> str:
        try:
            rows = self.database.connection.execute(
                "SELECT setting_key,setting_value FROM intel_runtime_settings "
                "WHERE setting_key IN ('active_industry_pack_id','active_industry_activation_id')"
            ).fetchall()
            values = {str(row[0]): str(row[1] or "") for row in rows}
            return values.get("active_industry_activation_id", "") if values.get("active_industry_pack_id") == pack_id else ""
        except Exception:
            return ""

    def _rows(self, pack_id: str) -> tuple[list[dict], dict[str, int]]:
        self.database._ensure_connection()
        with self.database.lock:
            active_activation = self._active_activation(pack_id)
            rows = self.database.connection.execute(
                """
                SELECT a.id,a.title,a.url,a.domain,a.content,a.publish_date,a.first_crawled,
                       a.status,a.quality_score,a.content_length,
                       c.industry_pack_id,c.activation_id,c.score_details_json,
                       c.matched_keywords_json,c.topic_tags_json,c.final_category,
                       ard.doc_type AS policy_doc_type, ard.doc_no AS policy_doc_no,
                       ard.issuer AS policy_issuer, ard.article_no AS policy_article_no,
                       ard.policy_title, ard.effective_date AS policy_effective_date,
                       ard.source_url AS policy_source_url,
                       COALESCE((SELECT MAX(ega.authority_level)
                         FROM intel_evidence_group_articles ega
                         JOIN intel_evidence_groups eg ON eg.id=ega.evidence_group_id
                         WHERE ega.article_id=a.id AND eg.industry_pack_id=?
                       ), ard.authority_level, 1) authority_level
                FROM articles a
                LEFT JOIN article_intel_classifications c
                  ON c.article_id=a.id AND c.industry_pack_id=?
                LEFT JOIN article_ragflow_documents ard
                  ON ard.article_id=a.id
                 AND COALESCE(ard.sync_status,'') IN ('parsed','uploaded')
                 AND COALESCE(ard.doc_type,'') IN ('official_policy','official_interpretation')
                WHERE (
                    c.industry_pack_id IS NOT NULL
                    OR EXISTS (
                        SELECT 1 FROM content_industry_packs cip
                        WHERE cip.content_type='article'
                          AND cip.content_id=CAST(a.id AS TEXT)
                          AND cip.industry_pack_id=?
                          AND cip.is_active=1
                    )
                    OR EXISTS (
                        SELECT 1 FROM intel_evidence_group_articles ega2
                        JOIN intel_evidence_groups eg2 ON eg2.id=ega2.evidence_group_id
                        WHERE ega2.article_id=a.id AND eg2.industry_pack_id=?
                    )
                )
                ORDER BY COALESCE(a.publish_date,a.first_crawled,a.created_at,'') DESC,a.id DESC
                LIMIT 1000
                """,
                (str(pack_id), str(pack_id), str(pack_id), str(pack_id)),
            ).fetchall()
        accepted, excluded = [], {"inactive": 0, "stale_activation": 0, "quality_gate": 0, "unsafe_url": 0, "policy_registry": 0}
        seen = set()
        for raw in rows:
            row = dict(raw)
            article_id = int(row.get("id") or 0)
            if article_id in seen:
                continue
            seen.add(article_id)
            is_policy_registry = str(row.get("policy_doc_type") or "") in {"official_policy", "official_interpretation"}
            if row.get("status") != "active":
                excluded["inactive"] += 1
                continue
            if active_activation and row.get("activation_id") and str(row.get("activation_id") or "") != active_activation:
                excluded["stale_activation"] += 1
                continue
            if not is_policy_registry and not _classification_admitted(_json(row.get("score_details_json"), {})):
                excluded["quality_gate"] += 1
                continue
            if not canonical_http_url(row.get("url")):
                excluded["unsafe_url"] += 1
                continue
            if is_policy_registry:
                excluded["policy_registry"] += 1
            accepted.append(row)
        return accepted, excluded

    def _policy_registry_rows(self, spec: Mapping, *, pack_id: str, limit: int = 20) -> list[dict]:
        if not spec.get("is_policy"):
            return []
        notices = spec.get("notices") or []
        if not notices and not (spec.get("issuers") and (spec.get("topics") or spec.get("title_terms"))):
            return []
        self.database._ensure_connection()
        notice_terms = []
        for notice in notices:
            notice_terms.extend(str(item) for item in notice.get("variants") or [] if str(item).strip())
        filter_terms = list(dict.fromkeys([
            *notice_terms,
            *[str(item) for item in spec.get("issuers") or []],
            *[str(item) for item in spec.get("topics") or []],
            *[str(item) for item in spec.get("title_terms") or []],
        ]))[:32]
        where = [
            "COALESCE(ard.sync_status,'') IN ('parsed','uploaded','')",
            "COALESCE(ard.doc_type,'') IN ('official_policy','official_interpretation')",
            "COALESCE(a.status,'active')='active'",
            """(
                EXISTS (
                    SELECT 1 FROM article_intel_classifications c
                    WHERE c.article_id=a.id AND c.industry_pack_id=?
                )
                OR EXISTS (
                    SELECT 1 FROM content_industry_packs cip
                    WHERE cip.content_type='article'
                      AND cip.content_id=CAST(a.id AS TEXT)
                      AND cip.industry_pack_id=?
                      AND cip.is_active=1
                )
                OR EXISTS (
                    SELECT 1 FROM intel_evidence_group_articles ega
                    JOIN intel_evidence_groups eg ON eg.id=ega.evidence_group_id
                    WHERE ega.article_id=a.id AND eg.industry_pack_id=?
                )
            )""",
        ]
        params: list[str | int] = [str(pack_id), str(pack_id), str(pack_id)]
        if filter_terms:
            like_parts = []
            for term in filter_terms[:24]:
                like_parts.append(
                    "(COALESCE(ard.doc_no,'') LIKE ? OR COALESCE(ard.policy_title,'') LIKE ? "
                    "OR COALESCE(ard.issuer,'') LIKE ? OR COALESCE(ard.source_url,'') LIKE ? "
                    "OR COALESCE(a.title,'') LIKE ? OR COALESCE(a.content,'') LIKE ?)"
                )
                params.extend([f"%{term}%"] * 6)
            where.append("(" + " OR ".join(like_parts) + ")")
        sql = f"""
            SELECT a.id,a.title,COALESCE(NULLIF(ard.source_url,''),a.url) AS url,
                   a.domain,a.content,a.publish_date,a.first_crawled,a.status,
                   a.quality_score,a.content_length,
                   NULL AS industry_pack_id,NULL AS activation_id,NULL AS score_details_json,
                   NULL AS matched_keywords_json,NULL AS topic_tags_json,NULL AS final_category,
                   ard.doc_type AS policy_doc_type, ard.doc_no AS policy_doc_no,
                   ard.issuer AS policy_issuer, ard.article_no AS policy_article_no,
                   ard.policy_title, ard.publish_date AS policy_publish_date,
                   ard.effective_date AS policy_effective_date,
                   ard.source_url AS policy_source_url,
                   COALESCE(ard.authority_level,1) authority_level
            FROM article_ragflow_documents ard
            JOIN articles a ON a.id=ard.article_id
            WHERE {" AND ".join(where)}
            ORDER BY COALESCE(ard.authority_level,1) DESC,
                     CASE COALESCE(ard.doc_type,'') WHEN 'official_policy' THEN 0 ELSE 1 END,
                     COALESCE(ard.publish_date,a.publish_date,a.first_crawled,'') DESC,
                     a.id DESC
            LIMIT ?
        """
        params.append(max(1, min(int(limit or 20), 50)))
        with self.database.lock:
            return [dict(row) for row in self.database.connection.execute(sql, tuple(params)).fetchall()]

    def _policy_exact_evidence(self, plan: Mapping, *, industry_pack_id: str, limit: int = 6) -> tuple[list[dict], dict]:
        spec = _policy_anchor_spec(plan)
        audit = {
            "policy_exact_gate": "not_applicable" if not spec.get("is_policy") else "applied",
            "notices": spec.get("notices") or [],
            "issuers": spec.get("issuers") or [],
            "topics": spec.get("topics") or [],
            "candidates": 0,
            "adopted": 0,
        }
        if not spec.get("is_policy"):
            return [], audit
        ranked = []
        for row in self._policy_registry_rows(spec, pack_id=str(industry_pack_id), limit=30):
            score, reasons = _policy_match_score(row, spec)
            if score < 1050:
                continue
            ranked.append((score, int(row.get("id") or 0), row, reasons))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        audit["candidates"] = len(ranked)
        evidence = []
        seen = set()
        for score, _article_id, row, reasons in ranked:
            article_id = int(row.get("id") or 0)
            if not article_id or article_id in seen:
                continue
            seen.add(article_id)
            item = _article_evidence(
                row,
                score=score,
                method="policy_metadata_exact",
                reason="PG政策元数据优先命中：" + "；".join(reasons[:6]),
            )
            meta = dict(item.get("metadata") or {})
            if meta.get("doc_type") == "official_policy":
                meta["source_role"] = "official_original"
                meta["authority_flags"] = list(dict.fromkeys([*(meta.get("authority_flags") or []), "official_original", "policy_metadata_exact"]))
            elif meta.get("doc_type") == "official_interpretation":
                meta["source_role"] = "official_reference"
                meta["authority_flags"] = list(dict.fromkeys([*(meta.get("authority_flags") or []), "official_reference", "policy_metadata_exact"]))
            item["metadata"] = meta
            evidence.append(item)
            if len(evidence) >= max(1, min(int(limit or 6), 12)):
                break
        audit["adopted"] = len(evidence)
        audit["official_original_adopted"] = sum(
            1 for item in evidence if (item.get("metadata") or {}).get("source_role") == "official_original"
        )
        return evidence, audit

    def retrieve(
        self,
        plan: Mapping,
        *,
        industry_pack_id: str,
        page_context: Mapping | None = None,
        limit: int = 12,
    ) -> dict:
        rows, excluded = self._rows(str(industry_pack_id))
        by_id = {int(row["id"]): row for row in rows}
        page_context = dict(page_context or {})
        requested_ids = []
        for raw in [page_context.get("article_id"), *(page_context.get("article_ids") or [])]:
            try:
                article_id = int(raw)
            except (TypeError, ValueError):
                continue
            if article_id > 0 and article_id not in requested_ids:
                requested_ids.append(article_id)

        selected = []
        seen_articles = set()
        page_denied = []
        for article_id in requested_ids:
            row = by_id.get(article_id)
            if not row:
                page_denied.append({"article_id": article_id, "reason": "not_found_or_not_authorized"})
                continue
            selected.append(_article_evidence(row, score=1000, method="page_context", reason="用户当前页面或显式引用", source_type="page_context"))
            seen_articles.add(article_id)

        policy_exact, policy_exact_audit = self._policy_exact_evidence(plan, industry_pack_id=industry_pack_id, limit=6)
        for item in policy_exact:
            article_id = int(item.get("article_id") or 0)
            if article_id and article_id in seen_articles:
                continue
            selected.append(item)
            if article_id:
                seen_articles.add(article_id)

        queries = [str(item) for item in plan.get("queries") or [] if str(item).strip()]
        query_terms = _terms(queries or [str(plan.get("question") or "")])
        anchor_terms = _semantic_anchor_terms(queries or [str(plan.get("question") or "")])
        phrase_terms = _query_phrases(queries or [str(plan.get("question") or "")])
        amount_constraints = []
        for query in queries or [str(plan.get("question") or "")]:
            for item in _amount_constraints(query):
                key = (round(float(item.get("value_yi") or 0), 6), bool(item.get("approximate")))
                if not any((round(float(existing.get("value_yi") or 0), 6), bool(existing.get("approximate"))) == key for existing in amount_constraints):
                    amount_constraints.append(item)
        amount_context_terms = _amount_context_terms(queries or [str(plan.get("question") or "")])
        semantic_scores = {}
        if self.semantic_search and rows:
            try:
                raw_semantic = self.semantic_search(" ".join(queries), allowed_ids=set(by_id), limit=limit)
                for item in raw_semantic or []:
                    if isinstance(item, (tuple, list)) and len(item) >= 2:
                        semantic_scores[int(item[0])] = max(0.0, float(item[1]))
                    else:
                        semantic_scores[int(item)] = 0.1
            except Exception:
                excluded["semantic_error"] = excluded.get("semantic_error", 0) + 1

        ranked = []
        for row in rows:
            article_id = int(row["id"])
            if article_id in seen_articles:
                continue
            title = str(row.get("title") or "").casefold()
            keywords = " ".join(str(item) for item in _json(row.get("matched_keywords_json"), []))
            topics = " ".join(str(item) for item in _json(row.get("topic_tags_json"), []))
            body = str(row.get("content") or "")[:4000].casefold()
            searchable_blob = f"{title} {keywords.casefold()} {topics.casefold()} {body}"
            anchor_hits = [term for term in anchor_terms if term in searchable_blob]
            title_hits = [term for term in query_terms if term in title]
            keyword_hits = [term for term in query_terms if term in keywords.casefold() or term in topics.casefold()]
            body_hits = [term for term in query_terms if term in body]
            title_phrase_hits = [term for term in phrase_terms if len(term) >= 3 and term in title]
            body_phrase_hits = [term for term in phrase_terms if len(term) >= 3 and term in body]
            title_amount_hits = _amount_constraint_hits(amount_constraints, title, context_terms=amount_context_terms)
            body_amount_hits = _amount_constraint_hits(amount_constraints, body, context_terms=amount_context_terms)
            if amount_constraints and not title_amount_hits and not body_amount_hits:
                excluded["amount_miss"] = excluded.get("amount_miss", 0) + 1
                continue
            semantic = semantic_scores.get(article_id, 0.0)
            anchor_coverage = (len(anchor_hits) / max(1, len(anchor_terms))) if anchor_terms else 0.0
            score = (
                len(title_phrase_hits) * 32
                + len(title_amount_hits) * 36
                + len(title_hits) * 8
                + len(keyword_hits) * 5
                + min(5, len(body_phrase_hits)) * 10
                + min(4, len(body_amount_hits)) * 16
                + min(5, len(body_hits)) * 1.5
                + anchor_coverage * 12
                + semantic * 10
            )
            if score <= 0:
                continue
            reasons = []
            if title_phrase_hits:
                reasons.append("标题短语命中：" + "、".join(title_phrase_hits[:6]))
            if title_amount_hits:
                reasons.append("标题数值命中：" + "、".join(title_amount_hits[:4]))
            if title_hits:
                reasons.append("标题命中：" + "、".join(title_hits[:6]))
            if keyword_hits:
                reasons.append("分类词命中：" + "、".join(keyword_hits[:6]))
            if body_phrase_hits:
                reasons.append("正文短语命中：" + "、".join(body_phrase_hits[:6]))
            if body_amount_hits:
                reasons.append("正文数值命中：" + "、".join(body_amount_hits[:4]))
            if anchor_hits:
                reasons.append("核心词命中：" + "、".join(anchor_hits[:4]))
            if semantic:
                reasons.append(f"语义相似度 {semantic:.2f}")
            ranked.append((score, str(row.get("publish_date") or row.get("first_crawled") or ""), article_id, row, "；".join(reasons) or "正文相关"))
        ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        cap = max(1, min(int(limit or 12), 30))
        for score, _date, article_id, row, reason in ranked:
            if len(selected) >= cap:
                break
            selected.append(_article_evidence(row, score=score, method="hybrid" if article_id in semantic_scores else "keyword", reason=reason))
            seen_articles.add(article_id)
        return {
            "queries": queries,
            "evidence": selected,
            "excluded": {**excluded, "page_context": page_denied, "policy_exact": policy_exact_audit},
            "stats": {"eligible": len(rows), "adopted": len(selected), "keyword_candidates": len(ranked)},
        }


class WebSearchService:
    """Normalize multiple providers without persisting their results."""

    def __init__(self, providers: Iterable[tuple[str, Callable]] | None = None):
        self.providers = list(providers or [])

    def search(self, queries: Iterable[str], *, enabled: bool, limit: int = 8) -> dict:
        if not enabled:
            return {"status": "disabled", "evidence": [], "providers": [], "errors": []}
        cap = max(1, min(int(limit or 8), 20))
        evidence, seen, provider_states, errors = [], set(), [], []
        for provider_name, provider in self.providers:
            count = 0
            try:
                for query in list(queries)[:3]:
                    for item in provider(query, cap) or []:
                        url = canonical_http_url(item.get("url") or item.get("href"))
                        if not url or url in seen:
                            continue
                        seen.add(url)
                        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
                        evidence.append({
                            "evidence_ref": f"web:{digest}", "source_type": "web",
                            "title": str(item.get("title") or url)[:1000], "source_url": url,
                            "content_excerpt": " ".join(str(item.get("snippet") or item.get("summary") or item.get("body") or "").split())[:3000],
                            "published_at": str(item.get("published_at") or item.get("date") or "") or None,
                            "fetched_at": utc_now_text(), "article_id": None, "ragflow_kb_id": None,
                            "document_id": None, "chunk_id": None, "score": float(item.get("score") or 0),
                            "authority_level": int(item.get("authority_level") or 1),
                            "retrieval_method": provider_name, "match_reason": f"联网搜索：{query[:120]}",
                            "relationship": "context", "metadata": {"provider": provider_name},
                        })
                        count += 1
                        if len(evidence) >= cap:
                            break
                    if len(evidence) >= cap:
                        break
                provider_states.append({"provider": provider_name, "status": "completed", "count": count})
            except Exception as exc:
                provider_states.append({"provider": provider_name, "status": "failed", "count": count})
                errors.append({"provider": provider_name, "code": "SEARCH_PROVIDER_FAILED", "message": "联网搜索供应商暂不可用"})
            if len(evidence) >= cap:
                break
        status = "completed" if evidence else ("failed" if errors else "empty")
        return {"status": status, "evidence": evidence, "providers": provider_states, "errors": errors}


def default_web_search_service() -> WebSearchService:
    providers = []
    try:
        import config
        if config.TAVILY_ENABLED and config.TAVILY_API_KEY:
            def tavily(query, limit):
                from tavily_client import TavilyClient
                return TavilyClient().search(query, max_results=limit)
            providers.append(("tavily", tavily))
        if config.SERPAPI_ENABLED and config.SERPAPI_API_KEY:
            def serpapi(query, limit):
                from serpapi_client import SerpAPIClient
                return SerpAPIClient().search(query, recency_days=0)[:limit]
            providers.append(("serpapi", serpapi))
    except Exception:
        pass
    return WebSearchService(providers)


__all__ = ["ArticleRetriever", "WebSearchService", "canonical_http_url", "default_web_search_service", "repair_mojibake"]
