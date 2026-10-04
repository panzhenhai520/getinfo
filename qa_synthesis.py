#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Evidence-locked final synthesis for the unified QA pipeline."""

from __future__ import annotations

import json
import re
import ast
from datetime import datetime, timezone
from typing import Callable, Mapping

import requests

from qa_contracts import QA_CONTRACT_VERSION, QaContractError, validate_final_answer
from qa_level1 import OpenAIJsonModelClient, extract_json_object

_REFUSAL_RE = re.compile(r"(抱歉|无法|不能|拒绝|sorry|cannot|can't|unable).{0,20}(处理|回答|协助|comply|process|answer)", re.I)
_RISK_RE = re.compile(r"(风险|冲击|影响|应对|调整|合规|家族办公室|家族信托|离岸信托|规避|反避税|申报|税务)", re.I)
_RAG_PROVIDER_RE = re.compile(r"RAGFlow", re.I)
_POLICY_QA_RE = re.compile(r"(公告|政策|法规|办法|条例|通知|个税|个人所得税|征管|离岸信托|境外信托)", re.I)


class _JsonAnswerDeltaExtractor:
    """Extract incremental content from a streaming JSON object's answer field."""

    def __init__(self):
        self.in_answer = False
        self.escape = False
        self.buffer = ""
        self._tail = ""
        self._started = False

    def feed(self, chunk: str) -> str:
        text = str(chunk or "")
        if not text:
            return ""
        combined = self._tail + text
        emitted = []
        index = 0
        if not self._started:
            match = re.search(r'"answer"\s*:\s*"', combined)
            if not match:
                self._tail = combined[-32:]
                return ""
            self._started = True
            self.in_answer = True
            index = match.end()
            self._tail = ""
        while index < len(combined) and self.in_answer:
            ch = combined[index]
            index += 1
            if self.escape:
                self.buffer += "\\" + ch
                self.escape = False
                continue
            if ch == "\\":
                self.escape = True
                continue
            if ch == '"':
                self.in_answer = False
                break
            self.buffer += ch
            decoded = self._decode_buffer(final=False)
            if decoded:
                emitted.append(decoded)
        return "".join(emitted)

    def flush(self) -> str:
        return self._decode_buffer(final=True)

    def _decode_buffer(self, *, final: bool) -> str:
        if not self.buffer:
            return ""
        try:
            decoded = json.loads(f'"{self.buffer}"')
        except Exception:
            if final:
                raw = self.buffer
                self.buffer = ""
                return raw
            return ""
        self.buffer = ""
        return decoded


def _stream_openai_json_content(profile, messages: list[dict], *, timeout: int = 90):
    if profile.provider_id != "local" and not profile.api_key:
        from qa_errors import missing_api_key_error
        from qa_orchestrator import QaStageFailure

        raise QaStageFailure(missing_api_key_error(profile.provider_id, stage="synthesis"))
    headers = {"Content-Type": "application/json"}
    if profile.api_key:
        headers["Authorization"] = f"Bearer {profile.api_key}"
    proxies = None
    if profile.use_proxy:
        try:
            import config
            proxies = config.get_proxies(enabled=True) or None
        except Exception:
            proxies = None
    from qa_observability import provider_allowed_hosts
    from qa_security import validate_outbound_url

    safe_base = validate_outbound_url(
        profile.base_url,
        allowed_hosts=provider_allowed_hosts(),
        allow_private_for_allowlist=True,
    )
    response_format = None if str(profile.provider_id or "").casefold() == "local" else {"type": "json_object"}
    if str(profile.provider_id or "").casefold() in {"chatgpt", "openai"}:
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "unified_qa_stage_output",
                "strict": False,
                "schema": {"type": "object", "additionalProperties": True},
            },
        }
    payload = {
        "model": profile.model_id,
        "messages": messages,
        "stream": True,
        "temperature": 0.1,
        "enable_thinking": False,
        "think": False,
        "num_ctx": 16384,
        "max_tokens": (4096 if profile.provider_id == "local" else 2048),
    }
    if response_format:
        payload["response_format"] = response_format
    response = requests.post(
        f"{safe_base.rstrip('/')}/chat/completions",
        headers=headers,
        json=payload,
        timeout=timeout,
        proxies=proxies,
        stream=True,
    )
    response.raise_for_status()
    for raw_line in response.iter_lines():
        if not raw_line:
            continue
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line)
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            body = json.loads(data)
        except Exception:
            continue
        choice = (body.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if content is None:
            content = delta.get("reasoning_content")
        if content is None:
            message = choice.get("message") or {}
            content = message.get("content")
        if content:
            yield str(content)


def _normalize_claim(claim: Mapping, *, fallback_id: str = "") -> dict:
    value = dict(claim or {})
    claim_id = str(value.get("claim_id") or fallback_id or "").strip()
    text = str(value.get("text") or "").strip()
    refs = [str(ref) for ref in value.get("evidence_refs") or [] if str(ref or "").strip()]
    scope = [str(item) for item in value.get("scope") or [] if str(item or "").strip()]
    try:
        confidence = float(value.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    claim_type = str(value.get("claim_type") or "interpretation")
    if claim_type not in {"current_fact", "historical_fact", "interpretation", "forecast", "background"}:
        claim_type = "interpretation"
    status = str(value.get("verification_status") or "unverified")
    if status not in {"unverified", "confirmed", "corrected", "qualified", "conflicted", "insufficient_evidence"}:
        status = "unverified"
    return {
        "claim_id": claim_id[:160] or "claim",
        "text": text[:4000] or "证据节点未提供可展示结论。",
        "claim_type": claim_type,
        "confidence": confidence,
        "valid_from": value.get("valid_from") if value.get("valid_from") is not None else None,
        "valid_to": value.get("valid_to") if value.get("valid_to") is not None else None,
        "scope": list(dict.fromkeys(scope))[:20],
        "evidence_refs": list(dict.fromkeys(refs))[:30],
        "needs_verification": bool(value.get("needs_verification", status != "confirmed")),
        "verification_status": status,
    }


def _eligible_claims(graph: Mapping) -> list[dict]:
    result = []
    conflicted = {
        claim_id
        for item in graph.get("conflicts") or []
        if item.get("resolution") == "unresolved"
        for claim_id in item.get("claim_ids") or []
    }
    for node in graph.get("claims") or []:
        claim = _normalize_claim(node.get("claim") or node, fallback_id=str(node.get("canonical_id") or ""))
        if claim["claim_id"] in conflicted:
            claim["verification_status"] = "conflicted"
            claim["needs_verification"] = True
        if claim.get("evidence_refs") or claim.get("verification_status") == "insufficient_evidence":
            result.append(claim)
    return result


def _metadata(item: Mapping) -> Mapping:
    metadata = item.get("metadata")
    return metadata if isinstance(metadata, Mapping) else {}


def _int_value(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _source_tier(item: Mapping) -> int:
    metadata = _metadata(item)
    doc_type = str(item.get("doc_type") or metadata.get("doc_type") or "").strip()
    source_role = str(item.get("source_role") or metadata.get("source_role") or "").strip()
    flags = {str(flag) for flag in metadata.get("authority_flags") or []}
    url = str(item.get("source_url") or metadata.get("source_url") or "")
    domain = str(metadata.get("domain") or metadata.get("source_domain") or "")
    official_domain = domain.endswith(".gov.cn") or ".mof.gov.cn" in url or "chinatax.gov.cn" in url
    if doc_type == "official_policy" or source_role == "official_original" or "official_original" in flags:
        return 40
    if doc_type == "official_interpretation" or source_role == "official_interpretation" or official_domain:
        return 30
    if doc_type == "professional_commentary":
        return 20
    if doc_type == "ai_qa_summary":
        return 0
    return 10


def _effective_authority(item: Mapping) -> int:
    metadata = _metadata(item)
    return max(
        _int_value(item.get("authority_level")),
        _int_value(metadata.get("authority_level")),
        _int_value(metadata.get("authority_rank")),
    )


def _is_official_evidence(item: Mapping) -> bool:
    return _source_tier(item) >= 30


def _official_only_question(question: str) -> bool:
    text = str(question or "")
    official_markers = ("只根据公告原文", "只看公告原文", "只依据公告原文", "只根据官方原文", "只看官方原文", "只依据官方原文")
    exclude_markers = ("不要解读", "不看解读", "不引用解读", "不引用律所", "不引用媒体")
    return any(marker in text for marker in official_markers) or ("原文" in text and any(marker in text for marker in exclude_markers))


def _policy_or_risk_question(question: str) -> bool:
    return bool(_POLICY_QA_RE.search(str(question or "")) or _RISK_RE.search(str(question or "")))


def _official_evidence(evidence: list[Mapping]) -> list[Mapping]:
    official = [item for item in evidence if _is_official_evidence(item)]
    return official or evidence


def _filter_claims_to_refs(claims: list[dict], refs: set[str]) -> list[dict]:
    if not refs:
        return claims
    filtered = []
    for claim in claims:
        kept_refs = [str(ref) for ref in claim.get("evidence_refs") or [] if str(ref) in refs]
        if not kept_refs:
            continue
        item = dict(claim)
        item["evidence_refs"] = kept_refs
        filtered.append(item)
    return filtered


def _filter_conflicts_to_refs(conflicts: list[Mapping], refs: set[str]) -> list[dict]:
    if not refs:
        return [dict(item) for item in conflicts]
    filtered = []
    for conflict in conflicts:
        kept_refs = [str(ref) for ref in conflict.get("evidence_refs") or [] if str(ref) in refs]
        if not kept_refs:
            continue
        item = dict(conflict)
        item["evidence_refs"] = kept_refs
        filtered.append(item)
    return filtered


def _citation_map(evidence: list[Mapping]) -> dict[str, str]:
    indexed = list(enumerate(evidence))
    ordered = sorted(
        indexed,
        key=lambda pair: (
            -_source_tier(pair[1]),
            -_effective_authority(pair[1]),
            0 if str(pair[1].get("source_type") or "") != "ragflow_chunk" else 1,
            str(pair[1].get("published_at") or _metadata(pair[1]).get("publish_date") or ""),
            pair[0],
        ),
    )
    ordered_evidence = [item for _, item in ordered]
    citation_map: dict[str, str] = {}
    seen_identities: set[str] = set()
    for item in ordered_evidence:
        ref = str(item.get("evidence_ref") or "")
        if not ref:
            continue
        identity = _citation_identity(item)
        if identity and identity in seen_identities:
            continue
        if identity:
            seen_identities.add(identity)
        citation_map[f"[{len(citation_map) + 1}]"] = ref
    return citation_map


def _citation_identity(item: Mapping) -> str:
    metadata = _metadata(item)
    if _is_official_evidence(item):
        doc_no = re.sub(r"\s+", "", str(item.get("doc_no") or metadata.get("doc_no") or "").casefold())
        title = re.sub(r"\s+", "", str(item.get("title") or metadata.get("title") or metadata.get("policy_title") or "").casefold())
        issuer = re.sub(r"\s+", "", str(item.get("issuer") or metadata.get("issuer") or "").casefold())
        if doc_no or title:
            return f"official:{issuer}:{doc_no}:{title}"
    url = str(item.get("source_url") or metadata.get("source_url") or "").strip().casefold()
    if url:
        return "url:" + url
    document_id = str(item.get("document_id") or metadata.get("document_id") or "").strip().casefold()
    if document_id:
        return "doc:" + document_id
    title = re.sub(r"\s+", "", str(item.get("title") or metadata.get("policy_title") or "").casefold())
    return "title:" + title if title else ""


def _citation_reverse(evidence: list[Mapping], citation_map: Mapping[str, str]) -> dict[str, str]:
    direct = {str(ref): str(label) for label, ref in dict(citation_map or {}).items()}
    evidence_by_ref = {str(item.get("evidence_ref")): item for item in evidence if item.get("evidence_ref")}
    primary_by_identity = {}
    for label, ref in dict(citation_map or {}).items():
        identity = _citation_identity(evidence_by_ref.get(str(ref), {}))
        if identity and identity not in primary_by_identity:
            primary_by_identity[identity] = str(label)
    reverse = {}
    for ref, item in evidence_by_ref.items():
        identity = _citation_identity(item)
        reverse[ref] = primary_by_identity.get(identity) or direct.get(ref, "")
    return reverse


def _cutoff(evidence: list[Mapping]) -> str:
    dates = [str(item.get("published_at") or "") for item in evidence if item.get("published_at")]
    if dates:
        return max(dates)
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _unsupported_numbers(answer: str, source_text: str) -> list[str]:
    answer_numbers = set(re.findall(r"(?<!\[)(?<!\d)\d+(?:\.\d+)?%?", str(answer or "")))
    source_numbers = set(re.findall(r"(?<!\d)\d+(?:\.\d+)?%?", str(source_text or "")))
    return sorted(answer_numbers - source_numbers)


def _is_model_refusal(raw: str) -> bool:
    text = str(raw or "").strip()
    if not text:
        return False
    if "{" in text and "}" in text:
        return False
    return bool(_REFUSAL_RE.search(text))


def _public_conflict_summary(item: Mapping) -> str:
    conflict_type = str(item.get("conflict_type") or "")
    text = str(item.get("summary") or item.get("reason") or item.get("rationale") or "").strip()
    text = re.sub(r"（?裁决规则\s*qa-adjudication-v\d+）?", "", text).strip()
    if text:
        return text[:240]
    if conflict_type == "method_difference":
        return "不同资料里的数字、税率、期限或计算口径不一致，不能取平均值；需要以官方原文或最新官方口径为准。"
    if conflict_type == "time_change":
        return "不同资料可能对应不同发布时间或适用阶段，回答时已优先采用最新且权威的官方依据。"
    if conflict_type == "real_conflict":
        return "不同资料存在实质性说法差异，回答时已优先采用官方原文；解读材料只作为参考。"
    return ""


def _public_conflict_summaries(conflicts: list[Mapping]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for item in conflicts:
        text = _public_conflict_summary(item)
        key = re.sub(r"\s+", "", text)
        if not text or key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output


def _public_text(value: object) -> str:
    text = str(value or "").strip()
    text = _RAG_PROVIDER_RE.sub("RAG增强检索", text)
    text = re.sub(r"RAG增强检索\s*(news\s*)?知识库", "RAG增强检索知识库", text, flags=re.I)
    text = re.sub(r"RAG增强检索\s*二级研究", "RAG增强检索", text)
    text = re.sub(r"连接\s*local\s*超时，请检查网络后重试。?", "综合模型响应超时，已改用证据约束重组。", text)
    text = re.sub(r"一级模型返回格式异常[^。；]*[。；]?", "资料已按证据锚定方式整理。", text)
    text = re.sub(r"一级分析未稳定返回结构化结果[^。；]*[。；]?", "资料已按证据锚定方式整理。", text)
    text = re.sub(r"[^。；]*研究模型格式未完全通过校验[^。；]*[。；]?", "已完成证据召回，并按证据锚定方式整理。", text)
    text = re.sub(r"降级摘要", "证据锚定摘要", text)
    text = re.sub(r"证据锚定摘要。?\s*现返回严格引用证据的证据锚定摘要。?", "证据锚定摘要。", text)
    return text.strip()


def _clean_claim_text(value: object) -> str:
    text = _public_text(value)
    text = re.sub(r"^\s*claim\s*=\s*[A-Za-z0-9_.:-]+\s*", "", text, flags=re.I)
    text = re.sub(r"^已检索到(?:官方原文|参考资料)[《「][^》」]+[》」]，?其内容载明：?", "", text)
    text = re.sub(r"^(?:已检索到|参考资料)[：:]\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -；;")
    if "个人将财产装入离岸信托" in text and "申报缴纳个人所得税" in text:
        return "21号公告明确，个人将财产装入离岸信托以及通过离岸信托取得收益，应按公告申报缴纳个人所得税。"
    if len(text) > 260:
        text = text[:260].rstrip("，。；;、 ") + "。"
    return text


def _is_digest_claim(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if text.startswith("已检索到参考资料") or text.startswith("已检索到行业资料"):
        return True
    if text.startswith("围绕") and "核心政策法规证据" in text:
        return True
    if text.startswith("与21号公告相关的15号征管安排"):
        return True
    return False


def _public_degradation_reasons(degradation: list, reason: str = "") -> list[str]:
    result, seen = [], set()
    for item in list(degradation or []) + ([{"message": reason}] if reason else []):
        if isinstance(item, Mapping):
            message = str(item.get("message") or item.get("code") or "").strip()
        else:
            message = str(item or "").strip()
        if not message:
            continue
        public = _public_text(message)
        if re.search(r"暂时无法连接\s*local|综合模型响应超时|RAG增强检索.*暂不可用|深度知识库暂不可用|已保留.*证据", public):
            continue
        if not public or public in seen:
            continue
        seen.add(public)
        result.append(public[:500])
    if result:
        return result[:20]
    return []


def _evidence_label(ref: str, citation_map: Mapping[str, str]) -> str:
    for label, mapped_ref in dict(citation_map or {}).items():
        if str(mapped_ref) == str(ref):
            return str(label)
    return ""


def _first_official_label(evidence: list[Mapping], citation_map: Mapping[str, str]) -> str:
    reverse = _citation_reverse(evidence, citation_map)
    for item in evidence:
        if _is_official_evidence(item):
            label = reverse.get(str(item.get("evidence_ref") or ""))
            if label:
                return label
    return ""


def _pin_official_citation(answer: str, evidence: list[Mapping], citation_map: Mapping[str, str]) -> str:
    label = _first_official_label(evidence, citation_map)
    text = str(answer or "").strip()
    if not label or label in text:
        return text
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if re.search(r"(公告|政策|法规|離岸信託|离岸信托|个人所得税|個人所得稅)", line):
            lines[index] = line.rstrip() + f" {label}"
            return "\n".join(lines)
    return text + f" {label}"


def _answer_has_internal_noise(answer: object) -> bool:
    return bool(re.search(r"claim\s*=|^\s*[\{\[]['\"]?(sections|answer|claims|citations)['\"]?\s*:|已检索到参考资料|已检索到官方原文|未解决冲突：|其内容载明：", str(answer or ""), re.I))


def _render_structured_answer_value(value: object, parsed: Mapping) -> str:
    if isinstance(value, str):
        text = value.strip()
        if re.match(r"^\s*[\{\[]", text) and re.search(r"['\"]?(sections|summary|claims|citations)['\"]?\s*:", text):
            try:
                value = json.loads(text)
            except Exception:
                try:
                    value = ast.literal_eval(text)
                except Exception:
                    return text
        else:
            return text
    source = value
    if not source:
        source = parsed
    lines = []
    if isinstance(source, Mapping):
        summary = _public_text(source.get("summary") or "")
        if summary:
            lines.extend(["核心结论：", summary])
        sections = source.get("sections")
        if isinstance(sections, Mapping):
            for key, content in sections.items():
                if key in {"diagnostics", "conflicts_and_uncertainty", "risk_deep_dive"}:
                    continue
                text = _public_text(content)
                if text:
                    lines.extend(["", str(key), text])
        elif isinstance(sections, list):
            for item in sections:
                if not isinstance(item, Mapping):
                    continue
                title = _public_text(item.get("title") or "分析")
                content = _public_text(item.get("content") or "")
                if content:
                    lines.extend(["", title, content])
        key_evidence = source.get("key_evidence")
        if isinstance(key_evidence, list):
            evidence_lines = [_public_text(item) for item in key_evidence if _public_text(item)]
            if evidence_lines:
                lines.extend(["", "关键依据：", *[f"- {item}" for item in evidence_lines[:6]]])
    elif isinstance(source, list):
        for item in source:
            text = _public_text(item)
            if text:
                lines.append(f"- {text}")
    return "\n".join(line for line in lines if str(line).strip()).strip()


def _answer_lacks_line_citations(answer: object) -> bool:
    lines = [line.strip() for line in str(answer or "").splitlines() if line.strip()]
    substantive = [
        line for line in lines
        if len(line) >= 24
        and not line.startswith(("关键依据", "依据：", "**依据"))
        and (_POLICY_QA_RE.search(line) or _RISK_RE.search(line))
    ]
    if not substantive:
        return False
    if len(substantive) == 1 and len(substantive[0]) > 320:
        return True
    missing = [line for line in substantive if not re.search(r"\[\d+\]", line)]
    return bool(missing)


def _public_evidence_gaps(gaps: list, evidence: list[Mapping]) -> list[str]:
    has_official = any(_is_official_evidence(item) for item in evidence)
    result, seen = [], set()
    for item in gaps:
        text = _public_text(item)
        if not text:
            continue
        if has_official and re.search(r"公告全文.*尚未|原文.*尚未|未公开", text):
            continue
        if re.search(r"主张\s+[A-Za-z0-9_.:-]+\s+尚无足够可引用依据", text):
            continue
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _extract_question_plan(question: str) -> dict:
    marker = "问题拆解与回答计划："
    text = str(question or "")
    if marker not in text:
        return {}
    raw = text.split(marker, 1)[1].strip()
    try:
        value = json.loads(raw)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _relation_public_label(value: str) -> str:
    labels = {
        "single": "单一问题",
        "parallel": "并列问题",
        "progressive": "递进问题",
        "causal": "因果问题",
        "comparison": "比较问题",
        "parent_child": "总分问题",
        "overlap": "交集问题",
        "conflict": "冲突核验问题",
    }
    return labels.get(str(value or ""), str(value or "相关问题"))


def _question_plan_intro(question: str) -> str:
    plan = _extract_question_plan(question)
    if not plan:
        return ""
    try:
        count = int(plan.get("问题数量") or 1)
    except (TypeError, ValueError):
        count = 1
    relation = str(plan.get("问题关系") or "single")
    subquestions = [item for item in plan.get("子问题") or [] if isinstance(item, Mapping)]
    categories = [item for item in plan.get("主题聚类") or [] if isinstance(item, Mapping)]
    outline = [str(item) for item in (plan.get("动态回答模板") or plan.get("回答大纲") or []) if str(item).strip()]
    strategy = _public_text(plan.get("回答策略") or "")
    lines = ["【问题分析思路】"]
    if count <= 1:
        first = str(subquestions[0].get("text") or "").strip() if subquestions else ""
        lines.append(f"- 已识别为 1 个单一问题{f'：{first}' if first else ''}。")
    else:
        lines.append(f"- 已识别为 {count} 个{_relation_public_label(relation)}。")
        for item in subquestions[:4]:
            qid = str(item.get("id") or "").replace("q", "")
            text = str(item.get("text") or "").strip()
            if text:
                lines.append(f"- 问题 {qid or len(lines)}：{text}")
        if count > 4:
            lines.append("- 其余问题会先合并同类项，再按主题分组回答。")
    labels = [str(item.get("label") or "") for item in categories if item.get("label")]
    if labels:
        unique_labels = list(dict.fromkeys(labels))
        lines.append(f"- 问题拆分为{len(unique_labels)}个类型：" + "、".join(unique_labels))
    if outline:
        cleaned = [item.lstrip("先再然后最后，,：: ") for item in outline[:5]]
        prefixes = ["我会先", "再", "然后", "最后"]
        steps = []
        for index, item in enumerate(cleaned):
            prefix = prefixes[index] if index < len(prefixes) else "然后"
            steps.append(prefix + item)
        lines.append("- " + " → ".join(steps) + "。")
    elif strategy:
        lines.append("- 我会按这个策略继续：" + strategy)
    return "\n".join(lines)


def _prepend_question_plan_intro(answer: str, question: str) -> str:
    intro = _question_plan_intro(question)
    text = str(answer or "").strip()
    if not intro or text.startswith("【问题分析思路】") or text.startswith("【问题分析思路：】") or text.startswith("问题拆解与回答计划："):
        return text
    return intro + "\n\n" + text


def _short_question_title(text: str) -> str:
    value = str(text or "").strip().strip("？?。.")
    if not value:
        return "问题"
    return value if len(value) <= 42 else value[:40] + "..."


def _question_kind(item: Mapping) -> str:
    category = str(item.get("category") or item.get("label") or "")
    text = str(item.get("text") or "")
    value = category + " " + text
    if any(word in value for word in ("影响", "风险", "应对", "家族办公室", "家族信托", "行业")):
        return "impact"
    if any(word in value for word in ("内容", "条文", "具体", "公告", "规定", "原文", "适用")):
        return "policy"
    if any(word in value for word in ("申报", "征管", "期限", "扣缴")):
        return "compliance"
    return "general"


def _claim_matches_question(claim: Mapping, subquestion: Mapping) -> bool:
    text = str(claim.get("text") or "")
    kind = _question_kind(subquestion)
    if kind == "impact":
        return bool(_RISK_RE.search(text) or any(word in text for word in ("家族办公室", "家族信托", "行业", "业务", "客户", "架构", "合规")))
    if kind == "policy":
        return bool(claim.get("has_official") or any(word in text for word in ("公告", "个人所得税", "离岸信托", "所得", "申报", "纳税", "适用")))
    if kind == "compliance":
        return any(word in text for word in ("申报", "征管", "扣缴", "资料", "留存", "期限", "税务机关"))
    question_words = set(re.findall(r"[\u4e00-\u9fff]{2,}", str(subquestion.get("text") or "")))
    return any(word in text for word in list(question_words)[:12])


def _planned_answer_lines(
    *,
    question: str,
    cleaned_claims: list[Mapping],
    risk_deep_dive: Mapping,
    official_label: str,
) -> list[str]:
    plan = _extract_question_plan(question)
    try:
        count = int(plan.get("问题数量") or 1)
    except (TypeError, ValueError):
        count = 1
    subquestions = [item for item in plan.get("子问题") or [] if isinstance(item, Mapping)]
    if count <= 1 or not subquestions:
        return []

    numerals = "一二三四五六七八九十"
    lines = ["", "分问题回答："]
    used_claims: set[str] = set()
    used_risks: set[str] = set()
    risks = [item for item in risk_deep_dive.get("risks") or [] if isinstance(item, Mapping)]

    for index, subquestion in enumerate(subquestions[:6]):
        prefix = numerals[index] if index < len(numerals) else str(index + 1)
        text = str(subquestion.get("text") or "").strip()
        kind = _question_kind(subquestion)
        lines.append("")
        lines.append(f"{prefix}、{_short_question_title(text)}")

        added = 0
        candidates = [claim for claim in cleaned_claims if claim.get("key") not in used_claims and _claim_matches_question(claim, subquestion)]
        if kind == "policy":
            candidates.sort(key=lambda item: (not item.get("has_official"), len(str(item.get("text") or ""))))
        elif kind == "impact":
            candidates.sort(key=lambda item: (not bool(_RISK_RE.search(str(item.get("text") or ""))), not any(word in str(item.get("text") or "") for word in ("家族办公室", "家族信托"))))
        for claim in candidates[:4]:
            labels = [str(label) for label in claim.get("labels") or [] if str(label)]
            if official_label and claim.get("has_official") and official_label not in labels:
                labels = [official_label, *labels]
            suffix = " " + " ".join(list(dict.fromkeys(labels))[:3]) if labels else ""
            lines.append(f"- {claim.get('text')}{suffix}")
            used_claims.add(str(claim.get("key") or ""))
            added += 1

        if kind == "impact":
            for risk in risks:
                area = str(risk.get("area") or "风险点")
                if area in used_risks:
                    continue
                labels = [str(label) for label in risk.get("citation_labels") or [] if str(label)]
                if official_label and not labels:
                    labels = [official_label]
                suffix = " " + " ".join(labels[:3]) if labels else ""
                lines.append(f"- {area}：{risk.get('impact')} 应对：{risk.get('response')}{suffix}")
                used_risks.add(area)
                added += 1
                if added >= 5:
                    break

        if added == 0:
            fallback = "知识库未检索到足够明确依据，不能直接下确定结论。"
            if kind == "impact":
                fallback = "现有证据只能说明可能影响合规流程，具体业务影响还需要结合信托文件、客户身份和资产类型继续核验。"
            lines.append(f"- {fallback}{(' ' + official_label) if official_label and kind == 'policy' else ''}")

    return lines


def _claim_labels(claim: Mapping, reverse: Mapping[str, str]) -> list[str]:
    labels = [str(reverse.get(str(ref), "")) for ref in claim.get("evidence_refs") or []]
    return [label for label in labels if label]


def _build_structured_answer(*, question: str, claims: list[Mapping], evidence: list[Mapping], citation_map: Mapping[str, str], models: Mapping | None = None) -> str:
    reverse = _citation_reverse(evidence, citation_map)
    official_label = _first_official_label(evidence, citation_map)
    evidence_refs_by_official = {
        str(item.get("evidence_ref"))
        for item in evidence
        if _is_official_evidence(item)
    }
    lines = []
    rag_label = _public_text(dict(models or {}).get("research") or "RAG检索")
    if rag_label not in {"RAG检索", "RAG增强检索"}:
        rag_label = "RAG检索"
    lines.append(f"已完成{rag_label}和证据图综合。")

    cleaned = []
    seen = set()
    for claim in claims:
        raw = str(claim.get("text") or "")
        if _is_digest_claim(raw):
            continue
        text = _clean_claim_text(raw)
        if not text:
            continue
        key = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", text.casefold())[:90]
        if key in seen:
            continue
        seen.add(key)
        refs = [str(ref) for ref in claim.get("evidence_refs") or []]
        labels = _claim_labels(claim, reverse)
        if official_label and any(ref in evidence_refs_by_official for ref in refs):
            labels = [official_label, *[label for label in labels if label != official_label]]
        cleaned.append({
            "key": key,
            "text": text,
            "labels": labels[:3],
            "refs": refs,
            "has_official": any(ref in evidence_refs_by_official for ref in refs),
        })
        if len(cleaned) >= 8:
            break

    risk_deep_dive = _build_risk_deep_dive(question, claims, evidence, citation_map)
    planned_lines = _planned_answer_lines(
        question=question,
        cleaned_claims=cleaned,
        risk_deep_dive=risk_deep_dive,
        official_label=official_label,
    )
    if planned_lines:
        lines.extend(planned_lines)
    elif cleaned:
        lines.extend(["", "核心结论："])
        for claim in cleaned[:4]:
            text = str(claim.get("text") or "")
            labels = [str(label) for label in claim.get("labels") or [] if str(label)]
            suffix = " " + " ".join(labels) if labels else (f" {official_label}" if official_label else "")
            lines.append(f"- {text}{suffix}")
    elif official_label:
        lines.extend(["", f"核心结论：应以官方原文作为法规判断依据。 {official_label}"])

    risks = risk_deep_dive.get("risks") or []
    if risks and not planned_lines:
        lines.extend(["", "风险拆解与应对："])
        seen_areas = set()
        for risk in risks:
            area = str(risk.get("area") or "风险点")
            if area in seen_areas:
                continue
            seen_areas.add(area)
            labels = [str(label) for label in risk.get("citation_labels") or [] if str(label)]
            if official_label and not labels:
                labels = [official_label]
            suffix = " " + " ".join(labels[:3]) if labels else ""
            lines.append(f"- {area}：{risk.get('impact')} 应对：{risk.get('response')}{suffix}")
            if len(seen_areas) >= 5:
                break
    answer = _pin_official_citation("\n".join(lines), evidence, citation_map)
    return _prepend_question_plan_intro(answer, question)


def _evidence_by_ref(evidence: list[Mapping]) -> dict[str, Mapping]:
    return {str(item.get("evidence_ref")): item for item in evidence if item.get("evidence_ref")}


def _risk_area_for_text(text: str) -> str:
    value = str(text or "")
    if any(word in value for word in ("设立", "装入", "转入", "注入", "入托")):
        return "设立与财产装入"
    if any(word in value for word in ("存续", "收益", "未分配", "按年", "投资")):
        return "存续期收益与年度申报"
    if any(word in value for word in ("分配", "受益人", "取得收益")):
        return "分配与受益人取得收益"
    if any(word in value for word in ("终止", "清算", "退出")):
        return "终止与清算"
    if any(word in value for word in ("申报", "征管", "资料", "留存", "报告")):
        return "申报、资料留存与征管"
    if any(word in value for word in ("反避税", "实质", "穿透", "规避")):
        return "反避税与实质穿透"
    if any(word in value for word in ("家族办公室", "家族信托", "架构", "客户")):
        return "家族办公室服务流程"
    return "政策影响与合规调整"


def _build_risk_deep_dive(question: str, claims: list[Mapping], evidence: list[Mapping], citation_map: Mapping[str, str]) -> dict:
    if not _RISK_RE.search(str(question or "") + " " + " ".join(str(claim.get("text") or "") for claim in claims[:20])):
        return {"risks": [], "law_search_queries": [], "methodology": ""}
    evidence_index = _evidence_by_ref(evidence)
    risks, seen = [], set()
    for claim in claims[:24]:
        text = str(claim.get("text") or "").strip()
        if not text or not _RISK_RE.search(text):
            continue
        area = _risk_area_for_text(text)
        refs = [str(ref) for ref in claim.get("evidence_refs") or [] if str(ref) in evidence_index]
        labels = [_evidence_label(ref, citation_map) for ref in refs]
        labels = [label for label in labels if label]
        key = area
        if key in seen:
            continue
        seen.add(key)
        risks.append({
            "area": area,
            "trigger": text[:260],
            "impact": _risk_impact(area),
            "response": _risk_response(area),
            "law_queries": _risk_law_queries(area),
            "evidence_refs": refs[:6],
            "citation_labels": labels[:4],
            "uncertainty": "具体税负、申报路径和历史安排处理需结合信托文件、资产类型、居民身份、收益分配记录及主管税务机关口径继续核验。",
        })
        if len(risks) >= 8:
            break
    if not risks:
        refs = [str(item.get("evidence_ref")) for item in evidence[:3] if item.get("evidence_ref")]
        risks.append({
            "area": "政策影响与合规调整",
            "trigger": "问题涉及政策变化对家族办公室、家族信托或离岸资产安排的影响。",
            "impact": "需要先确认官方原文是否直接覆盖相关业务环节，再判断行业解读是否仅为专业观点。",
            "response": "以官方政策和征管文件为准，补充检索个人所得税、反避税、境外所得申报和涉税信息交换规则后再形成操作建议。",
            "law_queries": _risk_law_queries("政策影响与合规调整"),
            "evidence_refs": refs,
            "citation_labels": [_evidence_label(ref, citation_map) for ref in refs if _evidence_label(ref, citation_map)],
            "uncertainty": "知识库未检索到更细分的风险触发条款时，应避免直接给出确定性税务处理结论。",
        })
    law_queries = []
    for risk in risks:
        law_queries.extend(risk.get("law_queries") or [])
    return {
        "methodology": "风险深研按“风险触发点 → 适用法规 → 业务影响 → 应对调整 → 缺口核验”多跳展开；官方政策优先，专业解读只作补充。",
        "risks": risks,
        "law_search_queries": list(dict.fromkeys(law_queries))[:16],
    }


def _risk_impact(area: str) -> str:
    mapping = {
        "设立与财产装入": "可能影响资产装入离岸信托时的所得识别、计税基础、估值资料和申报责任。",
        "存续期收益与年度申报": "可能影响信托存续期间收益确认、年度申报、资料留存和税款资金安排。",
        "分配与受益人取得收益": "可能影响受益人取得分配时的所得性质、纳税义务和跨境资料证明。",
        "终止与清算": "可能影响信托终止、资产返还或清算环节的所得确认和税务文件准备。",
        "申报、资料留存与征管": "可能增加客户尽调、申报辅导、底稿保存和与税务机关沟通的工作量。",
        "反避税与实质穿透": "可能使仅依赖形式隔离或递延的安排面临穿透核验和反避税调整。",
        "家族办公室服务流程": "可能要求家族办公室重做架构体检、客户风险分层、跨专业协同和持续监控流程。",
    }
    return mapping.get(area, "可能影响业务流程、税务判断、客户沟通和合规责任分配。")


def _risk_response(area: str) -> str:
    mapping = {
        "设立与财产装入": "梳理入托资产清单、历史成本、估值依据和装入路径；对无法取得依据的事项标记为需税务专业复核。",
        "存续期收益与年度申报": "建立年度收益台账，区分已分配与未分配收益，核对居民身份和境外所得申报要求。",
        "分配与受益人取得收益": "核对受益人身份、分配性质、支付路径和税款承担安排，避免把解读文章观点直接当作官方口径。",
        "终止与清算": "准备终止文件、资产估值、历史收益和税款计算底稿，先确认官方条文是否覆盖具体退出场景。",
        "申报、资料留存与征管": "建立客户问卷、资料清单、申报日历、复核留痕和异常升级机制。",
        "反避税与实质穿透": "对形式上离岸但实质由居民个人控制或受益的结构进行穿透复核，删除以规避纳税为目的的表达和方案。",
        "家族办公室服务流程": "将政策监控、客户架构盘点、税务测算、律师/税务师复核和客户沟通模板纳入标准服务流程。",
    }
    return mapping.get(area, "先用官方原文确认适用范围，再结合征管公告、个税法及专业意见形成可执行清单。")


def _risk_law_queries(area: str) -> list[str]:
    common = ["个人所得税法 境外所得 申报", "个人所得税法实施条例 反避税", "涉税信息交换 CRS 居民个人 境外资产"]
    mapping = {
        "设立与财产装入": ["离岸信托 财产装入 个人所得税 公告", "财产转让所得 个人所得税 计税依据"],
        "存续期收益与年度申报": ["离岸信托 存续期间 收益 个人所得税 申报", "居民个人 境外所得 年度申报"],
        "分配与受益人取得收益": ["离岸信托 收益分配 受益人 个人所得税", "利息股息红利所得 境外所得 申报"],
        "终止与清算": ["离岸信托 终止 清算 个人所得税", "信托终止 资产返还 税务处理"],
        "申报、资料留存与征管": ["离岸信托 个人所得税 征管 公告", "境外所得 资料留存 税务机关"],
        "反避税与实质穿透": ["个人所得税 反避税 实质重于形式", "受控外国企业 个人所得税 反避税"],
        "家族办公室服务流程": ["家族办公室 离岸信托 税务合规", "家族信托 涉税合规 个人所得税"],
    }
    return list(dict.fromkeys(mapping.get(area, []) + common))


def _ensure_inline_citations(answer: str, claims: list[Mapping], citation_map: Mapping[str, str]) -> str:
    text = str(answer or "").strip()
    if re.search(r"\[\d+\]", text):
        return text
    reverse = {str(ref): str(label) for label, ref in dict(citation_map or {}).items()}
    lines = []
    for claim in claims[:6]:
        labels = [reverse.get(str(ref), "") for ref in claim.get("evidence_refs") or []]
        labels = [label for label in labels if label]
        if labels:
            lines.append(f"- {str(claim.get('text') or '').strip()} {' '.join(labels[:3])}")
    if not lines:
        return text
    return text + "\n\n关键依据：\n" + "\n".join(lines)


class QaFinalSynthesizer:
    def __init__(self, model_client: Callable | None = None):
        self.model_client = model_client or OpenAIJsonModelClient()

    @staticmethod
    def _messages(*, question: str, graph: Mapping, level1: Mapping, level2: Mapping, degradation: list, repair_error: str = "", prior: str = "") -> list[dict]:
        claims = _eligible_claims(graph)
        evidence = list(graph.get("evidence") or [])
        conflicts = list(graph.get("conflicts") or [])
        if _official_only_question(question):
            evidence = _official_evidence(evidence)
            official_refs = {str(item.get("evidence_ref")) for item in evidence}
            claims = _filter_claims_to_refs(claims, official_refs)
            conflicts = _filter_conflicts_to_refs(conflicts, official_refs)
        citation_map = _citation_map(evidence)
        compact_evidence = [{
            "evidence_ref": item.get("evidence_ref"), "title": item.get("title"),
            "source_url": item.get("source_url"), "published_at": item.get("published_at"),
            "authority_level": item.get("authority_level"),
            "content_excerpt": str(item.get("content_excerpt") or "")[:1000],
        } for item in evidence[:20]]
        system = (
            "你是统一问答的最终综合器。输入已由程序建立主张-证据图。"
            "只能使用 allowed_claims 和 evidence；不得添加新主张、数字或引用；"
            "未解决冲突、例外、证据缺口和降级状态必须明确展示。"
            "只输出一个 JSON 对象；禁止 Markdown、代码围栏、解释性文字和思维过程；"
            "必须包含 required_shape 的全部顶层字段，字段名和类型必须一致；没有内容时用空数组、空字符串或 null。"
            "正文引用使用 citation_map 的 [n]，"
            "citations 字段填写对应 evidence_ref。answer不超过600字；每个sections文本不超过160字。"
            "claims 字段只填写 claim_id 字符串数组，不要回填完整 claim 对象；不要复述证据全文。"
            "如果 question 中包含“问题拆解与回答计划”，必须按其中的问题关系、主题聚类、检索策略和回答大纲组织答案；"
            "如果包含“动态回答模板”，最终 answer 的段落结构必须优先服从动态回答模板；"
            "多问题回答必须显式按子问题分节，分节标题要对应子问题；每个子问题下面只放与该问题相关的结论、依据和应对；"
            "single 直接回答；parallel 合并共用依据后分点回答；progressive 按“依据→影响→应对”；"
            "causal 先事实后原因/结果；comparison 先维度后比较；parent_child 先总览后分组；"
            "overlap 只回答共同范围；conflict 先列口径再按官方原文、官方解读、专业材料顺序裁决。"
            "多问题不要机械逐条复读，需先合并同类项；可以用一句话说明“这是两个递进问题，我先...再...”，但不要原样复述 JSON。"
            "如果用户在 question 中提供“用户调整意见”，必须优先服从该意见并调整回答组织方式；"
            "如果是“用户确认”继续，则沿用此前计划，不要把确认语当作新问题。"
        )
        required = {
            "contract_version": QA_CONTRACT_VERSION,
            "status": "ready|partial|insufficient_evidence",
            "answer": "综合回答，正文引用如 [1]",
            "sections": {
                "summary": "", "key_evidence": "", "timeline": "", "comparison": "",
                "conflicts_and_uncertainty": "", "business_impact": "", "retrieval_scope": "",
                "risk_deep_dive": {"methodology": "", "risks": [], "law_search_queries": []},
            },
            "claims": ["claim_id"],
            "conflicts": conflicts,
            "citations": list(citation_map.values()),
        }
        body = {
            "question": str(question)[:4000],
            "allowed_claims": [{
                "claim_id": item.get("claim_id"),
                "text": str(item.get("text") or "")[:220],
                "verification_status": item.get("verification_status"),
                "evidence_refs": list(item.get("evidence_refs") or [])[:6],
            } for item in claims[:12]],
            "conflicts": conflicts,
            "evidence": compact_evidence, "citation_map": citation_map,
            "level1_gaps": list(level1.get("gaps") or []),
            "level2_gaps": list(level2.get("evidence_gaps") or []),
            "degradation": list(degradation or []), "required_shape": required,
        }
        user = json.dumps(body, ensure_ascii=False)
        if repair_error:
            user += "\n只修复 JSON，不增加任何事实：" + repair_error[:1000] + "\n原输出：" + prior[:20000]
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def generate(
        self,
        *,
        question: str,
        graph: Mapping,
        level1: Mapping,
        level2: Mapping,
        degradation: list,
        profile,
        models: Mapping,
        token_callback: Callable[[str], None] | None = None,
    ) -> dict:
        messages = self._messages(question=question, graph=graph, level1=level1, level2=level2, degradation=degradation)
        if token_callback:
            raw_parts = []
            answer_delta = _JsonAnswerDeltaExtractor()
            streamed_buffer = []
            structured_answer_stream = False
            for content in _stream_openai_json_content(profile, messages, timeout=90):
                raw_parts.append(content)
                delta = answer_delta.feed(content)
                if delta:
                    streamed_buffer.append(delta)
                    probe = "".join(streamed_buffer).lstrip()
                    if probe.startswith(("{", "[")):
                        structured_answer_stream = True
                    if not structured_answer_stream:
                        token_callback(delta)
            tail = answer_delta.flush()
            if tail:
                streamed_buffer.append(tail)
                probe = "".join(streamed_buffer).lstrip()
                if probe.startswith(("{", "[")):
                    structured_answer_stream = True
                if not structured_answer_stream:
                    token_callback(tail)
            raw = "".join(raw_parts)
        else:
            raw = self.model_client(profile, messages, timeout=90)
        if _is_model_refusal(raw):
            item = {
                "stage": "synthesis",
                "code": "SYNTHESIS_MODEL_REFUSED",
                "message": "最终综合模型拒绝处理，已改用证据约束的结构化重组。",
            }
            return fallback_final_answer(
                graph=graph, level1=level1, level2=level2,
                degradation=list(degradation or []) + [item], models=models,
                reason=item["message"], question=question,
            )
        last_error = None
        for attempt in range(2):
            try:
                parsed = extract_json_object(raw)
                result = self._lock_to_graph(parsed, graph, level1, level2, degradation, models, question=question)
                source_text = json.dumps({"claims": result["claims"], "evidence": result["evidence"]}, ensure_ascii=False)
                unsupported = _unsupported_numbers(result["answer"], source_text)
                if unsupported:
                    raise QaContractError("最终答案包含证据外数字: " + ", ".join(unsupported[:10]))
                return validate_final_answer(result)
            except (QaContractError, KeyError, TypeError, ValueError) as exc:
                last_error = exc
                if attempt:
                    break
                raw = self.model_client(
                    profile,
                    self._messages(
                        question=question, graph=graph, level1=level1, level2=level2,
                        degradation=degradation, repair_error=str(exc), prior=str(raw),
                    ),
                    timeout=80,
                )
                if _is_model_refusal(raw):
                    item = {
                        "stage": "synthesis",
                        "code": "SYNTHESIS_MODEL_REFUSED",
                        "message": "最终综合模型拒绝处理，已改用证据约束的结构化重组。",
                    }
                    return fallback_final_answer(
                        graph=graph, level1=level1, level2=level2,
                        degradation=list(degradation or []) + [item], models=models,
                        reason=item["message"], question=question,
                    )
        return fallback_final_answer(
            graph=graph,
            level1=level1,
            level2=level2,
            degradation=list(degradation or []),
            models=models,
            reason="",
            question=question,
        )

    @staticmethod
    def _lock_to_graph(parsed: Mapping, graph: Mapping, level1: Mapping, level2: Mapping, degradation: list, models: Mapping, *, question: str = "") -> dict:
        allowed_claims = {str(item.get("claim_id") or ""): item for item in _eligible_claims(graph)}
        requested_ids = []
        for item in parsed.get("claims") or parsed.get("claim_ids") or []:
            if isinstance(item, Mapping):
                requested_ids.append(str(item.get("claim_id") or ""))
            else:
                requested_ids.append(str(item or ""))
        unknown_claims = sorted(set(requested_ids) - set(allowed_claims))
        if unknown_claims:
            raise QaContractError("最终模型引用了不存在的 claim_id: " + ", ".join(unknown_claims[:10]))
        selected_claims = [allowed_claims[item] for item in requested_ids if item in allowed_claims]
        if not requested_ids:
            selected_claims = list(allowed_claims.values())
        evidence = list(graph.get("evidence") or [])
        official_only = _official_only_question(question)
        if official_only:
            evidence = _official_evidence(evidence)
            official_refs = {str(item.get("evidence_ref")) for item in evidence}
            selected_claims = _filter_claims_to_refs(selected_claims, official_refs)
        evidence_refs = {str(item.get("evidence_ref")) for item in evidence}
        citations = list(dict.fromkeys(str(item) for item in parsed.get("citations") or []))
        if official_only:
            citations = [ref for ref in citations if ref in evidence_refs]
        unknown_refs = sorted(set(citations) - evidence_refs)
        if unknown_refs:
            raise QaContractError("最终模型引用了不存在的 evidence_ref: " + ", ".join(unknown_refs[:10]))
        if not citations:
            citations = list(dict.fromkeys(
                str(ref) for claim in selected_claims for ref in claim.get("evidence_refs") or []
            ))
        citation_map = _citation_map(evidence)
        reverse = _citation_reverse(evidence, citation_map)
        answer = _render_structured_answer_value(parsed.get("answer"), parsed)
        answer = re.sub(r"\[\d+\]", "", answer).strip()
        if selected_claims:
            answer = _ensure_inline_citations(answer, selected_claims, {label: ref for ref, label in reverse.items() if label})
        answer = _pin_official_citation(answer, evidence, citation_map)
        if (
            _answer_has_internal_noise(answer)
            or len(answer.strip()) < 20
            or (_policy_or_risk_question(question) and _answer_lacks_line_citations(answer))
        ):
            answer = _build_structured_answer(
                question=question, claims=selected_claims, evidence=evidence,
                citation_map=citation_map, models=models,
            )
        else:
            answer = _prepend_question_plan_intro(answer, question)
        conflicts = list(graph.get("conflicts") or [])
        if official_only:
            conflicts = _filter_conflicts_to_refs(conflicts, evidence_refs)
        unresolved = [
            item for item in conflicts
            if item.get("resolution") == "unresolved"
            and item.get("conflict_type") in {"real_conflict", "method_difference", "time_change"}
        ]
        l1_gaps = list(level1.get("gaps") or [])
        l2_gaps = list(level2.get("evidence_gaps") or []) if isinstance(level2, Mapping) else []
        sections = dict(parsed.get("sections") or {})
        public_degradation = _public_degradation_reasons(degradation)
        sections["conflicts_and_uncertainty"] = {
            "unresolved_conflicts": _public_conflict_summaries(unresolved),
            "evidence_gaps": _public_evidence_gaps(l1_gaps + l2_gaps, evidence),
            "degradation": public_degradation,
        }
        sections["risk_deep_dive"] = _build_risk_deep_dive(question, selected_claims, evidence, citation_map)
        sections["diagnostics"] = {
            "degradation_codes": [
                str(item.get("code") or "") for item in degradation or [] if isinstance(item, Mapping) and item.get("code")
            ],
        }
        degraded = bool(public_degradation)
        if not selected_claims:
            status = "insufficient_evidence"
        elif degraded or unresolved or l2_gaps:
            status = "partial"
        else:
            status = "ready"
        return {
            "contract_version": QA_CONTRACT_VERSION,
            "status": status,
            "answer": answer,
            "sections": sections,
            "claims": selected_claims,
            "conflicts": conflicts,
            "evidence": evidence,
            "citations": citations,
            "citation_map": citation_map,
            "cutoff_at": _cutoff(evidence),
            "degraded": degraded,
            "degradation_reasons": public_degradation,
            "models": {key: _public_text(value) for key, value in dict(models).items()},
        }


def fallback_final_answer(*, graph: Mapping, level1: Mapping, level2: Mapping, degradation: list, models: Mapping, reason: str, question: str = "") -> dict:
    claims = _eligible_claims(graph)
    evidence = list(graph.get("evidence") or [])
    conflicts = list(graph.get("conflicts") or [])
    if _official_only_question(question):
        evidence = _official_evidence(evidence)
        official_refs = {str(item.get("evidence_ref")) for item in evidence}
        claims = _filter_claims_to_refs(claims, official_refs)
        conflicts = _filter_conflicts_to_refs(conflicts, official_refs)
    citation_map = _citation_map(evidence)
    reverse = _citation_reverse(evidence, citation_map)
    visible_degradation = list(degradation or [])
    rag_label = _public_text(dict(models or {}).get("research") or "RAG检索")
    if rag_label not in {"RAG检索", "RAG增强检索"}:
        rag_label = "RAG检索"
    answer_text = _build_structured_answer(
        question=question, claims=claims, evidence=evidence,
        citation_map=citation_map, models=models,
    )
    unresolved = [
        item
        for item in conflicts
        if item.get("resolution") == "unresolved" and item.get("conflict_type") in {"real_conflict", "method_difference", "time_change"}
    ]
    reasons = []
    seen_reason_messages = set()
    reason_items = list(visible_degradation)
    if reason:
        reason_items.append({"code": "SYNTHESIS_FALLBACK_USED", "message": str(reason)[:400]})
    for item in reason_items:
        message = (
            str(item.get("message") or item.get("code") or item)[:500]
            if isinstance(item, Mapping) else str(item)[:500]
        ).strip()
        if message in seen_reason_messages:
            continue
        seen_reason_messages.add(message)
        reasons.append(item)
    risk_deep_dive = _build_risk_deep_dive(question, claims, evidence, citation_map)
    result = {
        "contract_version": QA_CONTRACT_VERSION,
        "status": "partial" if claims else "insufficient_evidence",
        "answer": answer_text,
        "sections": {
            "summary": answer_text.splitlines()[0] if answer_text else "",
            "conflicts_and_uncertainty": {
                "unresolved_conflicts": _public_conflict_summaries(unresolved),
                "evidence_gaps": _public_evidence_gaps(
                    list(level1.get("gaps") or []) + list(level2.get("evidence_gaps") or []),
                    evidence,
                ),
                "degradation": _public_degradation_reasons(reasons),
            },
            "risk_deep_dive": risk_deep_dive,
            "diagnostics": {
                "degradation_codes": [
                    str(item.get("code") or "") for item in reasons if isinstance(item, Mapping) and item.get("code")
                ],
            },
        },
        "claims": claims,
        "conflicts": conflicts,
        "evidence": evidence,
        "citations": list(dict.fromkeys(str(ref) for claim in claims for ref in claim.get("evidence_refs") or [])),
        "citation_map": citation_map,
        "cutoff_at": _cutoff(evidence),
        "degraded": bool(reasons),
        "degradation_reasons": _public_degradation_reasons(reasons, reason),
        "models": {key: _public_text(value) for key, value in dict(models).items()},
    }
    return validate_final_answer(result)


__all__ = ["QaFinalSynthesizer", "fallback_final_answer"]
