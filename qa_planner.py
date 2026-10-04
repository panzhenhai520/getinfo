#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic first-pass query understanding and bounded retrieval plans."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping

from industry_packs import industry_pack_loader
from qa_policy_evidence import detect_policy_anchors, policy_source_queries, source_profiles_from_pack


_POLICY_RE = re.compile(r"政策|法规|法例|条例|公告|税|监管|合规|征管|生效|适用主体|豁免|宽免", re.I)
_COMPARE_RE = re.compile(r"比较|对比|区别|横向|不同|vs\.?|versus", re.I)
_CONFLICT_RE = re.compile(r"冲突|矛盾|不一致|相反|口径|以谁为准|哪个为准|到底按|究竟按|裁决", re.I)
_OVERLAP_RE = re.compile(r"交集|共同|同时满足|既.*又|重叠|共同范围|哪些同时", re.I)
_TIME_RE = re.compile(r"(?<!\d)(20\d{2})(?:年|[-/.](\d{1,2})(?:[-/.](\d{1,2}))?)?")
_INJECTION_RE = re.compile(r"忽略.{0,12}(指令|规则|权限)|system\s*prompt|developer\s*message|绕过.{0,8}(权限|门禁)", re.I)
_DOC_NO_RE = re.compile(r"(20\d{2}\s*年\s*第?\s*\d{1,4}\s*[号號](?:公告)?|第?\s*\d{1,4}\s*[号號](?:公告)?)")
_TITLE_RE = re.compile(r"《([^》]{4,120})》")
_FOLLOWUP_RE = re.compile(r"^(那|那么|这个|这项|该|上述|前面|刚才|继续|进一步|展开|它|其|这些|对此|影响|风险|应对|怎么|如何)")
_QUESTION_SPLIT_RE = re.compile(r"(?<=[？?；;])\s*|(?:，|,)\s*(?=另外|还有|同时|并且|以及)")

_RELATION_LABELS = {
    "single": "单一问题",
    "parallel": "并列问题",
    "progressive": "递进问题",
    "causal": "因果问题",
    "comparison": "比较问题",
    "parent_child": "总分问题",
    "overlap": "交集问题",
    "conflict": "冲突问题",
}

_RELATION_STRATEGIES = {
    "single": "围绕当前问题直接检索并回答，结论必须绑定证据。",
    "parallel": "按子问题分别检索和回答，最后合并共同依据，避免重复结论。",
    "progressive": "先解析政策或事实本身，再基于该依据分析影响、风险和应对。",
    "causal": "先确认事实依据，再分析原因、结果和中间链条。",
    "comparison": "先建立比较维度，再逐项核验证据并给出差异。",
    "parent_child": "先总览问题，再按主题分组回答；问题过多时引导用户多轮追问。",
    "overlap": "先界定共同范围和排除范围，只回答同时满足条件的部分。",
    "conflict": "先列出不同口径，再按来源权威性、发布日期和适用范围裁决。",
}

_CATEGORY_RULES = [
    ("evidence_gap", "证据不足/待核验类", re.compile(r"有没有|是否明确|依据|核验|冲突|矛盾|不确定|待确认|以谁为准", re.I)),
    ("risk_response", "风险与应对类", re.compile(r"风险|应对|调整|合规|规避|方案|怎么做|如何处理|补救", re.I)),
    ("industry_impact", "行业影响类", re.compile(r"影响|行业|家族办公室|家族信托|业务|客户|市场|机构", re.I)),
    ("filing_collection", "申报征管类", re.compile(r"申报|征管|扣缴|期限|宽限|材料|留存|报送|缴纳|纳税", re.I)),
    ("subject_scope", "适用对象类", re.compile(r"适用|对象|主体|范围|哪些人|谁|例外|豁免|排除", re.I)),
    ("policy_content", "政策内容类", re.compile(r"内容|是什么|具体规定|条文|原文|公告|办法|条例|文件", re.I)),
]


def _needs_article_retrieval(question: str) -> bool:
    q = str(question or "").strip().casefold()
    if len(q) < 4:
        return False
    if any(marker in q for marker in (
        "你是什么", "你是谁", "什么模型", "你的名字", "介绍一下你自己",
        "你能做什么", "你会什么", "你叫什么",
    )):
        return False
    greetings = ("你好", "您好", "谢谢", "感谢", "再见", "拜拜", "在吗", "hello", "hi", "hey")
    return not (len(q) <= 12 and any(item in q for item in greetings))


def is_high_risk_policy_question(question: str) -> bool:
    return bool(_POLICY_RE.search(str(question or "")))


def _date_scope(question: str, now: datetime) -> dict:
    matches = list(_TIME_RE.finditer(question))
    if matches:
        years = [int(match.group(1)) for match in matches]
        return {"from": f"{min(years):04d}-01-01", "to": f"{max(years):04d}-12-31", "source": "explicit"}
    if any(token in question for token in ("最近", "近期", "最新", "近来")):
        return {"from": (now - timedelta(days=365)).date().isoformat(), "to": now.date().isoformat(), "source": "relative"}
    return {"from": None, "to": now.date().isoformat(), "source": "open"}


def _clean_query(value: str) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split())
    text = _INJECTION_RE.sub("", text).strip()
    return text[:1200]


def _field_block(text: str, label: str) -> str:
    pattern = rf"{re.escape(label)}：(.+?)(?=\s*(?:原始问题：|此前回答计划：|用户调整意见：|用户确认：|请沿用|请先理解|请按用户调整意见|$))"
    match = re.search(pattern, str(text or ""), re.S)
    return _clean_query(match.group(1)) if match else ""


def _unwrap_planning_question(question: str) -> tuple[str, dict]:
    text = str(question or "")
    if "原始问题：" not in text or ("用户调整意见：" not in text and "用户确认：" not in text):
        return question, {}
    original = _field_block(text, "原始问题")
    adjustment = _field_block(text, "用户调整意见")
    confirmation = _field_block(text, "用户确认")
    effective = original or question
    meta = {
        "original_question": original,
        "user_adjustment": adjustment,
        "user_confirmation": confirmation,
        "planning_source": "user_adjustment" if adjustment else "user_confirmation",
    }
    return effective, meta


def _adjustment_target_ids(adjustment: str, subquestions: list[dict]) -> list[str]:
    text = str(adjustment or "")
    ids = []
    for match in re.finditer(r"(?:第\s*)?([一二三四五六七八九十\d]+)\s*(?:个)?\s*(?:问题|问)", text):
        raw = match.group(1)
        if raw.isdigit():
            index = int(raw)
        else:
            index = "一二三四五六七八九十".find(raw) + 1
        if index > 0:
            ids.append(f"q{index}")
    if ids:
        available = {str(item.get("id") or "") for item in subquestions}
        return [qid for qid in ids if qid in available]
    category_hints = {
        "policy_content": ("政策内容", "公告内容", "原文", "条文", "具体内容"),
        "industry_impact": ("影响", "家族办公室", "家族信托", "行业"),
        "risk_response": ("风险", "应对", "方案", "怎么做"),
        "filing_collection": ("申报", "征管", "扣缴", "期限"),
        "subject_scope": ("适用", "对象", "范围", "主体"),
    }
    if any(word in text for word in ("只回答", "只看", "只要", "聚焦", "不要回答其他")):
        selected = []
        for item in subquestions:
            key = str((item.get("category") or {}).get("key") or "")
            if any(hint in text for hint in category_hints.get(key, ())):
                selected.append(str(item.get("id") or ""))
        return [qid for qid in selected if qid]
    return []


def _normalize_adjustment_patch(patch: object, subquestions: list[dict]) -> dict:
    if not isinstance(patch, Mapping):
        return {}
    available = {str(item.get("id") or "") for item in subquestions}
    operation = str(patch.get("operation") or "").strip().lower()
    if operation not in {"filter", "augment", "reorder", "format", "exclude", "replace", "confirm"}:
        operation = ""
    raw_targets = patch.get("target_subquestions") or patch.get("targets") or []
    if isinstance(raw_targets, str):
        raw_targets = [raw_targets]
    targets = []
    for item in raw_targets if isinstance(raw_targets, list) else []:
        value = str(item or "").strip().lower()
        match = re.search(r"q?\s*(\d{1,2})", value)
        if match:
            value = f"q{int(match.group(1))}"
        if value in available and value not in targets:
            targets.append(value)
    template = patch.get("answer_template") or patch.get("answer_outline") or []
    if isinstance(template, str):
        template = [template]
    template = [
        item for item in (_clean_query(str(item))[:80] for item in template if str(item or "").strip())
        if _valid_adjustment_template_item(item)
    ][:6] if isinstance(template, list) else []
    return {
        "operation": operation,
        "target_subquestions": targets,
        "answer_template": template,
        "answer_strategy": _clean_query(str(patch.get("answer_strategy") or patch.get("rationale") or ""))[:220],
        "format": _clean_query(str(patch.get("format") or ""))[:80],
        "exclude_sections": [
            _clean_query(str(item))[:80]
            for item in (patch.get("exclude_sections") or [])
            if str(item or "").strip()
        ][:8] if isinstance(patch.get("exclude_sections") or [], list) else [],
    }


def _valid_adjustment_template_item(value: str) -> bool:
    text = str(value or "").strip()
    if len(text) < 4:
        return False
    if re.search(r"(短句|不超过|不超過|可选|示例|example|optional|不超过\s*\d+\s*项|不超过\d+项)", text, re.I):
        return False
    if text in {"回答策略", "结构化回答", "短句，不超过6项", "短句，不超过 6 项"}:
        return False
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def _apply_plan_patch_to_plan(plan: dict, adjustment: str, patch: Mapping) -> dict:
    result = dict(plan)
    subquestions = [dict(item) for item in result.get("subquestions") or [] if isinstance(item, Mapping)]
    normalized = _normalize_adjustment_patch(patch, subquestions)
    operation = str(normalized.get("operation") or "")
    target_ids = list(normalized.get("target_subquestions") or [])
    if operation == "confirm":
        result["adjustment_operation"] = "confirm"
        result["user_adjustment"] = adjustment
        result["adjustment_patch"] = normalized
        return result
    if operation == "filter" and target_ids:
        result = _apply_adjustment_to_plan(result, adjustment, forced_target_ids=target_ids, patch=normalized)
    else:
        result = _apply_adjustment_to_plan(result, adjustment, patch=normalized if operation else None)
        if operation:
            result["adjustment_operation"] = operation
    result["adjustment_patch"] = normalized
    return result


def _apply_adjustment_to_plan(plan: dict, adjustment: str, *, forced_target_ids: list[str] | None = None, patch: Mapping | None = None) -> dict:
    text = str(adjustment or "").strip()
    if not text:
        return plan
    result = dict(plan)
    subquestions = [dict(item) for item in result.get("subquestions") or [] if isinstance(item, Mapping)]
    operation = "augment"
    target_ids = list(forced_target_ids or []) or _adjustment_target_ids(text, subquestions)
    if target_ids:
        operation = "filter"
        selected = [item for item in subquestions if str(item.get("id") or "") in set(target_ids)]
        for index, item in enumerate(selected, 1):
            item["original_id"] = item.get("id")
            item["id"] = f"q{index}"
        result["subquestions"] = selected
        result["question_count"] = len(selected) or 1
        result["relationship"] = "single" if len(selected) <= 1 else _question_relationship([str(item.get("text") or "") for item in selected])
        result["relationship_label"] = _RELATION_LABELS.get(str(result.get("relationship") or ""), str(result.get("relationship") or "相关问题"))
        categories = _cluster_subquestions([str(item.get("text") or "") for item in selected])
        result["categories"] = categories
        patch_template = list((patch or {}).get("answer_template") or [])
        if patch_template:
            result["answer_template"] = patch_template
        elif any(_question_category(str(item.get("text") or "")).get("key") in {"industry_impact", "risk_response"} for item in selected):
            result["answer_template"] = ["先聚焦选定问题说明影响", "再拆解风险点和业务变化", "最后给出可执行应对动作"]
        else:
            result["answer_template"] = _preferred_structure_from_adjustment(text, str(result.get("relationship") or "single"), categories)
        result["answer_outline"] = result["answer_template"]
        result["answer_strategy"] = str((patch or {}).get("answer_strategy") or "") or "用户调整为只回答选定子问题，已从原计划中筛选对应问题继续回答。"
    else:
        categories = list(result.get("categories") or [])
        patch_template = list((patch or {}).get("answer_template") or [])
        result["answer_template"] = patch_template or _preferred_structure_from_adjustment(text, str(result.get("relationship") or "single"), categories)
        result["answer_outline"] = result["answer_template"]
        patch_operation = str((patch or {}).get("operation") or "")
        if patch_operation in {"format", "augment", "exclude", "replace", "reorder"}:
            operation = patch_operation
            result["answer_strategy"] = str((patch or {}).get("answer_strategy") or "") or "用户已调整答题思路，回答时保留原问题上下文并按新思路组织。"
        elif any(word in text for word in ("不要", "不需要", "去掉", "删除")):
            operation = "exclude"
            result["answer_strategy"] = "用户调整为排除部分内容，回答时保留原问题但删除被排除的分析块。"
        elif any(word in text for word in ("表格", "对比", "比较", "差异", "格式")):
            operation = "format"
            result["answer_strategy"] = "用户调整了输出格式，回答时保留原问题并按新格式组织。"
        elif any(word in text for word in ("增加", "补充", "例子", "案例", "举例")):
            operation = "augment"
            template = list(result.get("answer_template") or _answer_outline(str(result.get("relationship") or "single"), categories))
            if not any("例" in item for item in template):
                template.append("补充一个简短例子说明")
            result["answer_template"] = template
            result["answer_outline"] = template
            result["answer_strategy"] = "用户调整为补充说明，回答时保留原问题并增加用户要求的内容。"
        else:
            result["answer_strategy"] = "用户已调整答题思路，回答时保留原问题上下文并按新思路组织。"
        result["adjustment_operation"] = operation
        result["user_adjustment"] = text
    result["retrieval_strategy"] = _retrieval_strategy(
        str(result.get("relationship") or "single"),
        list(result.get("categories") or []),
    )
    return result


def _preferred_structure_from_adjustment(adjustment: str, relationship: str, categories: list[dict]) -> list[str]:
    text = str(adjustment or "")
    if any(word in text for word in ("逐条", "按条", "条文", "原文顺序")):
        return ["按官方条文顺序解释", "每条后说明适用边界", "最后再分析影响和待核验事项"]
    if any(word in text for word in ("表格", "对比", "比较", "差异")):
        return ["先建立比较维度", "再用表格或分项比较", "最后给出适用场景和结论"]
    if any(word in text for word in ("风险", "应对", "方案", "怎么做")):
        return ["先列风险触发点", "再对应法规依据", "然后给出可执行应对动作"]
    if any(word in text for word in ("简洁", "简短", "只要结论")):
        return ["先给结论", "再列最关键依据", "最后说明证据不足处"]
    return _answer_outline(relationship, categories)


def _question_from_plan(plan: Mapping, fallback: str) -> str:
    subquestions = [item for item in plan.get("subquestions") or [] if isinstance(item, Mapping)]
    text = " ".join(str(item.get("text") or "") for item in subquestions if item.get("text"))
    if text:
        anchors = []
        anchors.extend(f"《{item}》" for item in _TITLE_RE.findall(str(fallback or ""))[:2])
        anchors.extend(_DOC_NO_RE.findall(str(fallback or ""))[:2])
        prefix = " ".join(dict.fromkeys(anchor for anchor in anchors if anchor and anchor not in text))
        return _clean_query(f"{prefix} {text}".strip())
    return _clean_query(fallback)


def _safe_messages(payload: Mapping, *, max_messages: int = 12, max_chars: int = 12000) -> list[dict]:
    candidates = []
    for field in ("messages", "history_context", "conversation_context"):
        value = payload.get(field)
        if isinstance(value, list):
            candidates.extend(value)
    selected = []
    remaining = max(1000, int(max_chars or 12000))
    for item in reversed(candidates):
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = _clean_query(str(item.get("content") or ""))
        if not content:
            continue
        if len(content) > remaining:
            content = content[-remaining:]
        selected.append({"role": role, "content": content})
        remaining -= len(content)
        if len(selected) >= max_messages or remaining <= 0:
            break
    selected.reverse()
    return selected


def _context_focus(messages: list[dict], current_question: str) -> str:
    snippets = []
    current = _clean_query(current_question)
    for item in reversed(messages[:-1] if messages and messages[-1].get("content") == current else messages):
        text = str(item.get("content") or "")
        if not text:
            continue
        titles = _TITLE_RE.findall(text)
        doc_nos = _DOC_NO_RE.findall(text)
        keywords = []
        for word in ("离岸信托", "个人所得税", "家族办公室", "家族信托", "征管", "财政部", "税务总局"):
            if word in text:
                keywords.append(word)
        focus = " ".join(dict.fromkeys([*titles[:2], *doc_nos[:2], *keywords[:6]]))
        if focus:
            snippets.append(focus)
        if len(snippets) >= 2:
            break
    merged = " ".join(dict.fromkeys(" ".join(snippets).split()))
    return merged[:180]


def _looks_followup(question: str) -> bool:
    text = str(question or "").strip()
    if not text:
        return False
    has_anchor = bool(_TITLE_RE.search(text) or _DOC_NO_RE.search(text))
    if has_anchor:
        return False
    return bool(_FOLLOWUP_RE.search(text)) or len(text) <= 24


def _standalone_question(question: str, messages: list[dict]) -> tuple[str, bool, str]:
    focus = _context_focus(messages, question)
    if focus and _looks_followup(question):
        return _clean_query(f"结合前文关于 {focus} 的讨论，{question}"), True, focus
    return question, False, focus


def _split_subquestions(question: str) -> list[str]:
    parts = []
    for raw in _QUESTION_SPLIT_RE.split(str(question or "")):
        clean = _clean_query(raw.strip(" ；;，,"))
        if clean and clean not in parts:
            parts.append(clean)
    if len(parts) <= 1 and "？" in question:
        parts = [_clean_query(item) for item in question.split("？") if _clean_query(item)]
    return parts[:12] or ([question] if question else [])


def _question_relationship(subquestions: list[str]) -> str:
    joined = " ".join(subquestions)
    if _CONFLICT_RE.search(joined):
        return "conflict"
    if _OVERLAP_RE.search(joined):
        return "overlap"
    if any(token in joined for token in ("比较", "对比", "区别", "不同")):
        return "comparison"
    if any(token in joined for token in ("为什么", "导致", "因此", "所以", "原因")):
        return "causal"
    if len(subquestions) <= 1:
        return "single"
    if len(subquestions) >= 4:
        return "parent_child"
    first, rest = subquestions[0], " ".join(subquestions[1:])
    if re.search(r"(内容|是什么|具体规定|条文|原文)", first) and re.search(r"(影响|风险|应对|怎么做|调整)", rest):
        return "progressive"
    return "parallel"


def _question_category(text: str) -> dict:
    for key, label, pattern in _CATEGORY_RULES:
        if pattern.search(str(text or "")):
            return {"key": key, "label": label}
    return {"key": "other", "label": "其他问题类"}


def _cluster_subquestions(subquestions: list[str]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for index, text in enumerate(subquestions, 1):
        category = _question_category(text)
        key = category["key"]
        if key not in grouped:
            grouped[key] = {"key": key, "label": category["label"], "question_ids": [], "questions": []}
        grouped[key]["question_ids"].append(f"q{index}")
        grouped[key]["questions"].append(text)
    order = ["policy_content", "subject_scope", "filing_collection", "industry_impact", "risk_response", "evidence_gap", "other"]
    return [grouped[key] for key in order if key in grouped]


def _retrieval_strategy(relationship: str, categories: list[dict]) -> list[dict]:
    category_keys = {str(item.get("key") or "") for item in categories}
    steps = [
        {
            "source": "official_policy",
            "purpose": "先按法规号、标题、发布机关精确查官方原文，作为结论和引用排序的基础。",
        }
    ]
    if category_keys & {"policy_content", "subject_scope", "filing_collection", "evidence_gap"}:
        steps.append({
            "source": "official_interpretation",
            "purpose": "当原文条款不足以解释征管或适用边界时，补充官方答问和政策解读。",
        })
    if category_keys & {"industry_impact", "risk_response"} or relationship in {"progressive", "causal", "comparison", "conflict"}:
        steps.append({
            "source": "professional_commentary",
            "purpose": "只在官方依据之后引用专业材料，用于影响、风险和实务应对分析。",
        })
    if relationship == "conflict":
        steps.append({
            "source": "adjudication",
            "purpose": "对冲突口径按官方原文优先、官方解读次之、专业材料辅助的顺序裁决。",
        })
    return steps


def _answer_outline(relationship: str, categories: list[dict]) -> list[str]:
    if relationship == "progressive":
        return ["先说明政策内容和适用边界", "再分析行业影响、风险点和应对动作"]
    if relationship == "conflict":
        return ["先列明不同材料的说法", "再给出以官方依据为准的裁决", "最后说明仍需核验的缺口"]
    if relationship == "overlap":
        return ["先定义共同范围", "再排除不满足共同条件的内容", "最后只回答交集内结论"]
    if relationship == "comparison":
        return ["先建立比较维度", "再逐项比较", "最后总结差异和适用场景"]
    if relationship == "causal":
        return ["先确认事实依据", "再说明原因", "最后说明影响和后续动作"]
    if relationship == "parent_child":
        return ["先给总览", "再按主题分组展开", "问题过多时提示可继续追问细项"]
    labels = [str(item.get("label") or "") for item in categories if item.get("label")]
    return labels[:6] or ["直接回答当前问题"]


def _question_plan(question: str, standalone: str, messages: list[dict], *, followup: bool, focus: str) -> dict:
    subquestions = _split_subquestions(question)
    relationship = _question_relationship(subquestions)
    categories = _cluster_subquestions(subquestions)
    strategy = _RELATION_STRATEGIES.get(relationship, _RELATION_STRATEGIES["single"])
    if followup:
        strategy = f"这是承接前文的追问，先把省略对象还原为“{focus}”，再检索回答。"
    return {
        "question_count": len(subquestions),
        "subquestions": [
            {"id": f"q{index + 1}", "text": item, "category": _question_category(item)}
            for index, item in enumerate(subquestions)
        ],
        "relationship": relationship,
        "relationship_label": _RELATION_LABELS.get(relationship, relationship),
        "is_followup": followup,
        "context_focus": focus,
        "standalone_question": standalone,
        "multi_turn_suggested": len(subquestions) >= 6,
        "categories": categories,
        "answer_strategy": strategy,
        "retrieval_strategy": _retrieval_strategy(relationship, categories),
        "answer_outline": _answer_outline(relationship, categories),
        "answer_template": _answer_outline(relationship, categories),
    }


class QaQueryPlanner:
    def __init__(self, *, pack_loader=None, query_expander: Callable | None = None, adjustment_parser: Callable | None = None, now: Callable | None = None):
        self.pack_loader = pack_loader or industry_pack_loader
        self.query_expander = query_expander
        self.adjustment_parser = adjustment_parser
        self.now = now or (lambda: datetime.now(timezone.utc))

    def plan(self, request_payload: Mapping) -> dict:
        raw_question = str(request_payload.get("question") or "")
        question, planning_meta = _unwrap_planning_question(raw_question)
        question = _clean_query(question)
        messages = _safe_messages(request_payload)
        standalone, is_followup, focus = _standalone_question(question, messages)
        planning_question = standalone or question
        question_plan = _question_plan(question, planning_question, messages, followup=is_followup, focus=focus)
        if planning_meta:
            question_plan["planning_source"] = planning_meta.get("planning_source")
            question_plan["original_question"] = planning_meta.get("original_question")
            question_plan["user_adjustment"] = planning_meta.get("user_adjustment")
            question_plan["user_confirmation"] = planning_meta.get("user_confirmation")
            if planning_meta.get("user_adjustment"):
                adjustment_text = str(planning_meta.get("user_adjustment") or "")
                patch = {}
                if self.adjustment_parser is not None:
                    try:
                        parsed_patch = self.adjustment_parser({
                            "original_question": planning_meta.get("original_question") or question,
                            "current_plan": question_plan,
                            "user_adjustment": adjustment_text,
                            "history": messages[-6:],
                            "request": dict(request_payload),
                        })
                        patch = _normalize_adjustment_patch(parsed_patch, list(question_plan.get("subquestions") or []))
                    except Exception:
                        patch = {}
                question_plan = _apply_plan_patch_to_plan(question_plan, adjustment_text, patch) if patch else _apply_adjustment_to_plan(question_plan, adjustment_text)
                question_plan["planning_source"] = planning_meta.get("planning_source")
                question_plan["original_question"] = planning_meta.get("original_question")
                question_plan["user_adjustment"] = planning_meta.get("user_adjustment")
                planning_question = _question_from_plan(question_plan, planning_question)
                question_plan["standalone_question"] = planning_question
            elif planning_meta.get("user_confirmation"):
                question_plan["answer_strategy"] = "用户确认沿用此前问题分析思路，继续按原计划检索和回答。"
        pack_id = str(request_payload.get("industry_pack_id") or "")
        pack = self.pack_loader.load(pack_id)
        ragflow_policy = dict(pack.get("ragflow_policy") or {})
        ragflow_qa_enabled = bool(ragflow_policy.get("qa_retrieval_enabled"))
        needs_retrieval = _needs_article_retrieval(planning_question)
        policy = is_high_risk_policy_question(planning_question)
        source_profiles = source_profiles_from_pack(pack)
        policy_anchors = detect_policy_anchors(planning_question, {"high_risk_policy": policy, "source_profiles": source_profiles})
        if policy_anchors.get("is_policy"):
            policy = True
        comparison = bool(_COMPARE_RE.search(planning_question))
        time_scope = _date_scope(planning_question, self.now())
        vocabulary = []
        for key in ("core_keywords", "expanded_keywords"):
            vocabulary.extend(str(item) for item in pack.get(key) or [])
        entities = [item for item in vocabulary if item.casefold() in planning_question.casefold()]
        topics = []
        for topic in pack.get("fixed_topics") or []:
            if isinstance(topic, Mapping):
                name = str(topic.get("name") or topic.get("key") or "")
                keywords = [name, *(topic.get("keywords") or [])]
                if any(str(keyword).casefold() in planning_question.casefold() for keyword in keywords if keyword):
                    topics.append(str(topic.get("key") or name))

        queries = []
        if needs_retrieval:
            queries.append(planning_question)
            if policy:
                queries.extend([
                    f"{planning_question} 官方原文 生效日期 适用主体",
                    f"{planning_question} 专业解读 例外 合规影响",
                ])
                queries.extend(policy_source_queries(planning_question, policy_anchors))
            if comparison:
                queries.append(f"{planning_question} 同类案例 横向比较")
            if self.query_expander is not None:
                try:
                    expanded = self.query_expander({
                        "question": planning_question,
                        "industry_pack_id": pack_id,
                        "entities": entities,
                        "topics": topics,
                        "time_scope": time_scope,
                    })
                    queries.extend(str(item) for item in expanded or [])
                except Exception:
                    pass
        normalized_queries = []
        query_cap = min(12, 5 + 2 * len(policy_anchors.get("requested_sources") or []))
        for item in queries:
            clean = _clean_query(item)
            if clean and clean not in normalized_queries:
                normalized_queries.append(clean)
            if len(normalized_queries) >= query_cap:
                break
        axes = []
        if policy:
            axes.extend(["official_text", "timeline", "scope", "exceptions"])
        if comparison:
            axes.append("peer_cases")
        if needs_retrieval:
            axes.append("conflict")
        return {
            "question": question,
            "standalone_question": planning_question,
            "question_plan": question_plan,
            "intent": "smalltalk" if not needs_retrieval else ("policy_impact_analysis" if policy else ("comparison" if comparison else "industry_research")),
            "requested_mode": str(request_payload.get("mode") or "standard"),
            "needs_local_articles": needs_retrieval,
            "needs_web": bool(request_payload.get("web_search")) and needs_retrieval,
            "needs_ragflow": needs_retrieval and ragflow_qa_enabled and str(request_payload.get("mode") or "standard") != "fast",
            "ragflow_policy": {
                "qa_retrieval_enabled": ragflow_qa_enabled,
                "knowledge_base_key": str(ragflow_policy.get("knowledge_base_key") or ""),
            },
            "needs_conflict_check": needs_retrieval,
            "high_risk_policy": policy,
            "time_scope": time_scope,
            "entities": entities[:30],
            "topics": topics[:20],
            "policy_anchors": policy_anchors,
            "requested_sources": list(policy_anchors.get("requested_sources") or []),
            "source_profiles": source_profiles[:120],
            "research_axes": list(dict.fromkeys(axes)),
            "queries": normalized_queries,
            "answer_language": "zh-CN",
        }


__all__ = ["QaQueryPlanner", "is_high_risk_policy_question"]
