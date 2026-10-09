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


def _env_flag(name: str, default: bool = False) -> bool:
    """环境开关（与其它模块同一口径：#0/false/off/no 视为关闭）。"""
    import os

    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return bool(default)
    return str(raw).strip().lower() not in ("0", "false", "off", "no")


def _env_int(name: str, default: int, low: int, high: int) -> int:
    import os

    try:
        value = int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return int(default)
    return max(int(low), min(int(high), value))


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
    # ── 阶段 9 新增四类：逻辑驱动的问题类型，放在最前（越具体越优先）──
    # 多跳传导：问的是"A 怎么影响到 B"，需要分跳检索
    ("multi_hop", "多跳传导类", re.compile(
        r"(对|向|给)[^。？！]{1,24}(有什么|有何|会有|带来|产生)[^。？！]{0,8}影响"
        r"|传导(路径|链条|机制|效应)|上下游|供应链[^。？！]{0,8}(影响|传导)"
        r"|间接影响|如何影响|会影响到|波及|连锁反应|外溢效应|传导到", re.I)),
    # 因果：问"为什么"，需要先事实后原因
    ("causal", "因果类", re.compile(
        r"为什么|为何|导致|引起|造成|原因|成因|因为|由于|根源|驱动因素|背后(的)?(逻辑|原因)", re.I)),
    # 时序关系：问先后/时间线（"先…再…" 与 "先…还是…" 两种问法都要认）
    ("temporal_relation", "时序关系类", re.compile(
        r"先后顺序|时间线|时间轴|早于|晚于|在此之前|在此之后|同期|随后|紧接着|"
        r"先[^。？！]{1,24}(?:再|还是|或)", re.I)),
    # 条件约束：问"在什么条件下成立/是否满足"
    ("conditional_constraint", "条件约束类", re.compile(
        r"在[^。？！]{1,24}(条件|前提|情形|情况)下|如果[^。？！]{1,24}(是否|能否|会)|除非|"
        r"只有[^。？！]{1,16}才|是否满足|满足[^。？！]{1,12}条件|前提是|限于[^。？！]{1,12}(情形|情况)", re.I)),
    ("evidence_gap", "证据不足/待核验类", re.compile(r"有没有|是否明确|依据|核验|冲突|矛盾|不确定|待确认|以谁为准", re.I)),
    ("risk_response", "风险与应对类", re.compile(r"风险|应对|调整|合规|规避|方案|怎么做|如何处理|补救", re.I)),
    ("industry_impact", "行业影响类", re.compile(r"影响|行业|家族办公室|家族信托|业务|客户|市场|机构", re.I)),
    ("filing_collection", "申报征管类", re.compile(r"申报|征管|扣缴|期限|宽限|材料|留存|报送|缴纳|纳税", re.I)),
    ("subject_scope", "适用对象类", re.compile(r"适用|对象|主体|范围|哪些人|谁|例外|豁免|排除", re.I)),
    ("policy_content", "政策内容类", re.compile(r"内容|是什么|具体规定|条文|原文|公告|办法|条例|文件", re.I)),
    ("fact_check", "事实核验类", re.compile(r"已经|是否|有没有|了吗|运行|上线|发布|推出|开始|最新|目前|现在", re.I)),
]

# 需要分跳检索的问题类型（阶段 9）：这些类别才会触发多跳分解
_MULTI_HOP_CATEGORIES = {"multi_hop", "causal", "conditional_constraint", "temporal_relation"}


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


_OUTPUT_FORMS = {
    "paragraph_by_paragraph", "structured_text", "table", "brief", "timeline",
}
# 用户说"逐段/逐条/全文/原文"这类词 → 要的是**原文全文逐段过**，不是换个模板
_FULLTEXT_WORDS = ("逐段", "逐条", "逐句", "全文", "原文", "条文", "整篇", "完整内容")
_OUTPUT_FORM_HINTS = (
    ("paragraph_by_paragraph", ("逐段", "逐条", "逐句", "按段落", "按条文", "原文顺序")),
    ("table", ("表格", "列表对比", "横向比较", "对比表")),
    ("timeline", ("时间轴", "时间线", "按时间顺序")),
    ("brief", ("简洁", "简短", "只要结论", "一句话", "精简")),
)
_REL_MONTHS = re.compile(r"(近|最近|过去|前)\s*(\d{1,2}|[一二两三四五六七八九十]+)\s*个?月")
_REL_DAYS = re.compile(r"(近|最近|过去|前)\s*(\d{1,3}|[一二两三四五六七八九十]+)\s*(天|日|周)")
_ABS_MONTH = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月")


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
    output_form = str(patch.get("output_form") or "").strip().lower()
    if output_form not in _OUTPUT_FORMS:
        # 兼容旧字段 format（structured_text/table/brief）与中文写法
        legacy = str(patch.get("format") or "").strip().lower()
        output_form = legacy if legacy in _OUTPUT_FORMS else ""
    must_fulltext = bool(patch.get("must_fetch_fulltext")) or output_form == "paragraph_by_paragraph"
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
        # ── 阶段 5 新增：调整必须真的改变**检索与生成**，而不只是换模板 ──
        "retrieval_queries": [
            clean for clean in (
                _clean_query(str(item))[:120]
                for item in (patch.get("retrieval_queries") or patch.get("queries") or [])
                if str(item or "").strip()
            ) if clean
        ][:4] if isinstance(patch.get("retrieval_queries") or patch.get("queries") or [], list) else [],
        "time_window": _normalize_adjustment_time_window(patch.get("time_window")),
        "must_fetch_fulltext": must_fulltext,
        "output_form": output_form,
    }


def _normalize_adjustment_time_window(value: object) -> dict:
    """调整里的时间范围 → {label, days, start, end, source}；解析不出来返回 {}。

    start/end 一律存 ISO 字符串：plan 会被塞进 SSE 事件与阶段结果，放 datetime 会序列化失败
    （实测 level1_retrieval 曾因此 INTERNAL_ERROR）。
    """
    if isinstance(value, str):
        return _time_window_from_phrase(value)
    if not isinstance(value, Mapping):
        return {}
    label = _clean_query(str(value.get("label") or value.get("text") or ""))[:60]
    raw_days = value.get("days")
    days = 0
    try:
        days = int(raw_days)
    except (TypeError, ValueError):
        days = 0
    start = str(value.get("start") or "")
    end = str(value.get("end") or "")
    if not (label or days > 0 or start):
        return {}
    if not (start or end) and label:
        # 模型只给了自然语言 → 用同一个解析器补出区间
        derived = _time_window_from_phrase(label)
        start, end = derived.get("start", ""), derived.get("end", "")
        days = days or int(derived.get("days") or 0)
        label = str(derived.get("label") or label)
    if days <= 0 and label:
        days = _days_from_label(label)
    if days:
        days = max(1, min(3650, days))
    return {"label": label or (f"近 {days} 天" if days else ""), "days": days,
            "start": start, "end": end, "source": "user_adjustment"}


def _window_phrase(time_window: Mapping) -> str:
    """时间范围 → 能塞进检索式的**简短**说法（parse_time_window 认得出来）。

    相对范围优先用"近 N 天"；绝对范围（模型只给了 label 或 start/end）用 label 里的年月。
    完整 label 里常带"（2026-07-10 起）"这类给人看的注释，塞进检索式只会变成关键词噪声。
    """
    if not isinstance(time_window, Mapping):
        return ""
    try:
        days = int(time_window.get("days") or 0)
    except (TypeError, ValueError):
        days = 0
    if days > 0:
        return f"近 {days} 天" if days < 3650 else "近 1 年"
    label = str(time_window.get("label") or "").strip()
    match = _ABS_MONTH.search(label)
    if match:
        return match.group(0)
    match = re.search(r"(20\d{2})\s*年", label)
    return match.group(0) if match else label[:20]


def _time_phrase_in(text: str) -> str:
    """从调整文本里摘出**时间短语本身**（而不是整句话）——回执要给人看，检索式要能用。"""
    raw = str(text or "")
    for pattern, group in ((_REL_MONTHS, 0), (_REL_DAYS, 0), (_ABS_MONTH, 0)):
        match = pattern.search(raw)
        if match:
            return match.group(group).strip()
    match = re.search(r"(20\d{2})\s*年", raw)
    if match:
        return match.group(0).strip()
    return ""


def _time_window_from_phrase(phrase: str) -> dict:
    """时间短语 → 计划里的时间范围（**存 ISO 字符串**，因为 plan 会被塞进 SSE 事件）。

    复用既有的 parse_time_window，不另造一套时间解析：
    相对说法（最近 3 个月）给 days；绝对说法（2026 年 1 月）给 start/end。
    """
    text = str(phrase or "").strip()
    if not text:
        return {}
    result = {"label": text, "days": _days_from_label(text), "start": "", "end": "",
              "source": "user_adjustment"}
    try:
        from qa_query_normalize import parse_time_window

        parsed = parse_time_window(text)
    except Exception:
        parsed = {}
    if parsed.get("has_time"):
        if parsed.get("start") is not None:
            result["start"] = parsed["start"].isoformat()
        if parsed.get("end") is not None:
            result["end"] = parsed["end"].isoformat()
        result["label"] = str(parsed.get("label") or text)
        try:
            result["days"] = int(parsed.get("days") or result["days"] or 0)
        except (TypeError, ValueError):
            pass
    return result


def _days_from_label(label: str) -> int:
    text = str(label or "")
    match = _REL_MONTHS.search(text)
    if match:
        return _cn_number(match.group(2)) * 30
    match = _REL_DAYS.search(text)
    if match:
        amount = _cn_number(match.group(2))
        unit = match.group(3)
        return amount * {"天": 1, "日": 1, "周": 7}.get(unit, 1)
    if _ABS_MONTH.search(text):
        return 30
    if re.search(r"(20\d{2})\s*年", text):
        return 365
    return 0


def _cn_number(raw: str) -> int:
    text = str(raw or "").strip()
    if text.isdigit():
        return int(text)
    table = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
             "七": 7, "八": 8, "九": 9, "十": 10}
    if text in table:
        return table[text]
    match = re.fullmatch(r"十([一二三四五六七八九])", text)
    if match:
        return 10 + table.get(match.group(1), 0)
    return 0


def _rule_based_adjustment_patch(adjustment: str, subquestions: list[dict]) -> dict:
    target_ids = _adjustment_target_ids(adjustment, subquestions)
    if target_ids:
        return {
            "operation": "filter",
            "target_subquestions": target_ids,
            "answer_template": [],
            "answer_strategy": "用户明确要求只回答选定子问题，已直接筛选对应问题继续回答。",
            "format": "",
            "exclude_sections": [],
        }
    text = str(adjustment or "")
    if any(word in text for word in ("同意", "继续", "按此思路", "按这个思路", "听你的", "你看着办")):
        return {
            "operation": "confirm",
            "target_subquestions": [],
            "answer_template": [],
            "answer_strategy": "",
            "format": "",
            "exclude_sections": [],
        }
    # ── 阶段 5：规则路也要能改**检索条件**，不能只认 confirm/filter ──
    # 实测问题：用户说「查看政策全文，逐段解释」，plan 里只有 answer_template 变了，
    # queries 与送模证据原封不动 → "听话了但没做到"。这里把这类话落成
    # output_form / must_fetch_fulltext / retrieval_queries / time_window。
    retrieval_queries = []
    output_form = ""
    for form, words in _OUTPUT_FORM_HINTS:
        if any(word in text for word in words):
            output_form = form
            break
    must_fulltext = any(word in text for word in _FULLTEXT_WORDS)
    if must_fulltext and not output_form:
        output_form = "paragraph_by_paragraph"
    if must_fulltext:
        retrieval_queries.append("官方原文 全文 逐条")
    if any(word in text for word in ("官方原文", "原文", "文件全称", "政策全称", "全文")):
        retrieval_queries.append("政策全称 发文机关 发文字号 原文")
    time_window = {}
    phrase = _time_phrase_in(text)
    if phrase:
        time_window = _time_window_from_phrase(phrase)
    if not (retrieval_queries or output_form or time_window):
        return {}
    return {
        "operation": "augment",
        "target_subquestions": [],
        "answer_template": [],
        "answer_strategy": "用户调整了检索与输出要求，已把调整落到检索式与输出形式上。",
        "format": "",
        "exclude_sections": [],
        "retrieval_queries": retrieval_queries,
        "time_window": time_window,
        "must_fetch_fulltext": must_fulltext,
        "output_form": output_form,
    }


def _material_cleaning_from_adjustment(adjustment: str) -> dict:
    text = str(adjustment or "")
    if not any(word in text for word in ("清洗材料", "清理材料", "剔除", "去掉", "过滤", "合并重复", "重复片段", "社媒", "泛主题", "泛行业", "泛家办", "背景")):
        return {}
    exclude_social = any(word in text.casefold() for word in ("社媒", "社交媒体", "instagram", "facebook", "linkedin", "小红书", "微博"))
    exclude_generic = any(word in text for word in ("泛主题", "泛行业", "泛家办", "泛泛", "背景", "无关家办", "通用家办"))
    dedupe = any(word in text for word in ("合并重复", "重复片段", "去重", "重复"))
    return {
        "enabled": True,
        "dedupe_repeated_fragments": bool(dedupe or "清洗材料" in text),
        "exclude_social_media": bool(exclude_social or "清洗材料" in text),
        "exclude_generic_background": bool(exclude_generic or "清洗材料" in text),
        "instruction": _clean_query(text)[:240],
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
    # ── 阶段 5：把调整落到**检索条件与输出形式**上（不只是回答模板）──
    # 专项检索式插在列表最前，保证在 query_cap 截断前一定被保留。
    existing_queries = [str(item) for item in result.get("retrieval_queries") or []]
    for item in normalized.get("retrieval_queries") or []:
        if item and item not in existing_queries:
            existing_queries.append(item)
    if existing_queries:
        result["retrieval_queries"] = existing_queries[:4]
    if normalized.get("output_form"):
        result["output_form"] = normalized["output_form"]
    if normalized.get("must_fetch_fulltext"):
        result["must_fetch_fulltext"] = True
    if normalized.get("time_window"):
        result["time_window"] = normalized["time_window"]
    result["adjustment_receipt"] = _adjustment_receipt(result, adjustment, normalized)
    return result


def _adjustment_receipt(plan: Mapping, adjustment: str, normalized: Mapping) -> dict:
    """把"解析成了什么"写成给用户看的回执（前端直接展示，不另外调模型）。"""
    items = []
    output_form = str(plan.get("output_form") or "")
    form_labels = {
        "paragraph_by_paragraph": "逐段解释",
        "table": "表格对比",
        "timeline": "时间轴",
        "brief": "只给结论",
        "structured_text": "结构化文本",
    }
    if output_form:
        items.append("已按「%s」组织回答" % form_labels.get(output_form, output_form))
    if plan.get("must_fetch_fulltext"):
        items.append("会先把原文全文取出来再逐段生成")
    queries = list(plan.get("retrieval_queries") or [])
    if queries:
        items.append("检索式已加入专项查询：%s" % "、".join(queries[:3]))
    time_window = plan.get("time_window") or {}
    if time_window.get("label"):
        items.append("时间范围已改为：%s" % time_window["label"])
    target_ids = [str(item) for item in normalized.get("target_subquestions") or []]
    if target_ids:
        items.append("只回答指定的子问题：%s" % "、".join(target_ids))
    if normalized.get("exclude_sections"):
        items.append("已排除：%s" % "、".join(str(item) for item in normalized["exclude_sections"][:4]))
    operation = str(plan.get("adjustment_operation") or "")
    return {
        "adjustment": str(adjustment or "")[:200],
        "operation": operation,
        "output_form": output_form,
        "must_fetch_fulltext": bool(plan.get("must_fetch_fulltext")),
        "retrieval_queries": queries,
        "time_window": dict(time_window) if isinstance(time_window, Mapping) else {},
        "items": items,
        "summary": ("已按你的调整执行：" + "；".join(items)) if items
                   else "已记录你的调整（未改变检索条件与输出形式）",
    }


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
        result["adjustment_operation"] = operation
        cleaning = _material_cleaning_from_adjustment(text)
        if cleaning:
            result["material_cleaning"] = cleaning
            result["answer_strategy"] = "用户调整为只回答选定子问题，并要求先清洗材料：合并重复片段，剔除泛背景和社媒噪声。"
            template = list(result.get("answer_template") or [])
            if not any("清洗" in item or "筛选" in item for item in template):
                template.insert(0, "先清洗证据，只保留与选定问题直接相关的材料")
            result["answer_template"] = template[:6]
            result["answer_outline"] = result["answer_template"]
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
        cleaning = _material_cleaning_from_adjustment(text)
        if cleaning:
            result["material_cleaning"] = cleaning
            if operation == "augment":
                operation = "exclude"
            result["answer_strategy"] = "用户要求先清洗材料，回答时合并重复片段并剔除泛背景、社媒噪声。"
            template = list(result.get("answer_template") or [])
            if not any("清洗" in item or "筛选" in item for item in template):
                template.insert(0, "先清洗证据，只保留直接相关材料")
            result["answer_template"] = template[:6]
            result["answer_outline"] = result["answer_template"]
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
    return {"key": "fact_check", "label": "事实核验类"}


def _cluster_subquestions(subquestions: list[str]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for index, text in enumerate(subquestions, 1):
        category = _question_category(text)
        key = category["key"]
        if key not in grouped:
            grouped[key] = {"key": key, "label": category["label"], "question_ids": [], "questions": []}
        grouped[key]["question_ids"].append(f"q{index}")
        grouped[key]["questions"].append(text)
    order = ["policy_content", "subject_scope", "filing_collection", "industry_impact",
             "multi_hop", "causal", "conditional_constraint", "temporal_relation",
             "risk_response", "evidence_gap", "fact_check", "other"]
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
    if category_keys & _MULTI_HOP_CATEGORIES:
        steps.append({
            "source": "hop_chain",
            "purpose": "按依赖顺序分跳检索：上一跳定位到的实体作为下一跳的过滤条件，每一跳都要有证据。",
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
    if any(str(item.get("key") or "") == "fact_check" for item in categories):
        return ["核验问题是否属于当前行业包范围", "检索本行业包内的直接证据", "证据不足时明确说明缺口"]
    return labels[:6] or ["核验事实并基于证据回答"]


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
                subquestions = list(question_plan.get("subquestions") or [])
                patch = _rule_based_adjustment_patch(adjustment_text, subquestions)
                if not patch and self.adjustment_parser is not None:
                    try:
                        parsed_patch = self.adjustment_parser({
                            "original_question": planning_meta.get("original_question") or question,
                            "current_plan": question_plan,
                            "user_adjustment": adjustment_text,
                            "history": messages[-6:],
                            "request": dict(request_payload),
                        })
                        patch = _normalize_adjustment_patch(parsed_patch, subquestions)
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

        # 行业规则引擎（阶段 9）：条件 → 动作。命中才补检索式/加权词，没命中与旧行为逐字一致。
        category = _question_category(planning_question)
        rule_patch = {"matched": [], "retrieval_queries": [], "boost_terms": [],
                      "require_fulltext": False, "note": ""}
        if _env_flag("QA_BUSINESS_RULES_ENABLED", True):
            try:
                from business_rules import business_rule_engine

                rule_patch = business_rule_engine.match(
                    planning_question, pack_id=pack_id, category=str(category.get("key") or ""))
            except Exception:
                rule_patch = {"matched": [], "retrieval_queries": [], "boost_terms": [],
                              "require_fulltext": False, "note": ""}
        for term in rule_patch.get("boost_terms") or []:
            text = str(term or "").strip()
            if text and text not in entities:
                entities.append(text)
        if rule_patch.get("require_fulltext"):
            question_plan["must_fetch_fulltext"] = True

        # 子查询分解（阶段 9）：产出带 depends_on 的有向无环图；分不出来就保持单跳。
        decomposition = {"is_multi_hop": False, "hops": [], "reason": "未分解"}
        if _env_flag("QA_QUERY_DECOMPOSE_ENABLED", True):
            try:
                from qa_query_decompose import decompose

                decomposition = decompose(
                    planning_question,
                    category=str(category.get("key") or ""),
                    relationship=str(question_plan.get("relationship") or ""),
                    pack_id=pack_id, entities=entities, topics=topics,
                    max_hops=_env_int("QA_MAX_HOPS", 3, 1, 5),
                )
            except Exception as exc:
                decomposition = {"is_multi_hop": False, "hops": [],
                                 "reason": "分解器不可用：%s" % str(exc)[:60]}

        # 用户调整产生的专项检索式：插在最前，确保在 query_cap 截断前一定保留
        # （阶段 5：调整要真的改变检索，不能只换回答模板）。
        adjustment_queries = [
            _clean_query(str(item))[:120]
            for item in question_plan.get("retrieval_queries") or []
            if str(item or "").strip()
        ]
        queries = []
        if needs_retrieval:
            queries.append(planning_question)
            time_window_adjust = question_plan.get("time_window") or {}
            for item in adjustment_queries:
                if not item:
                    continue
                # 时间范围调整：把"最近 N 天/某年某月"并进检索式，
                # 让既有 parse_time_window 直接认出来（不另造一套时间解析）。
                # 用简短说法而不是完整 label："近 90 天（2026-07-10 起）"里的括号注释
                # 对关键词匹配只是噪声。
                phrase = _window_phrase(time_window_adjust)
                if phrase and phrase not in item:
                    item = f"{item} {phrase}"
                queries.append(item)
            if policy:
                queries.extend([
                    f"{planning_question} 官方原文 生效日期 适用主体",
                    f"{planning_question} 专业解读 例外 合规影响",
                ])
                queries.extend(policy_source_queries(planning_question, policy_anchors))
            if comparison:
                queries.append(f"{planning_question} 同类案例 横向比较")
            # 规则动作补的检索式：紧跟用户调整之后、扩展器之前，确保在 query_cap 内保留
            for item in rule_patch.get("retrieval_queries") or []:
                text = _clean_query(str(item))
                if text:
                    queries.append(text)
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
        # 任务模板：先判定"这是哪一类需求"，把检查清单、边界、能力声明写进计划。
        # 参考用户给的正例：好的计划会先说清"我按哪几组检查项做、只读不改、做不到的明说"，
        # 而不是只把问句拆成子问题。
        try:
            from qa_task_templates import match_task_template

            task_template = match_task_template(planning_question or question)
        except Exception:
            task_template = {}
        question_plan["task_template"] = task_template
        question_plan["plan_checklist"] = task_template.get("checklist") or []
        question_plan["plan_boundary"] = str(task_template.get("boundary") or "")
        question_plan["capability"] = str(task_template.get("capability") or "document_research")
        if task_template.get("capability_note"):
            question_plan["capability_note"] = str(task_template["capability_note"])
        return {
            "question": question,
            "standalone_question": planning_question,
            "question_plan": question_plan,
            "task_template": task_template,
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
            "output_form": str(question_plan.get("output_form") or ""),
            "must_fetch_fulltext": bool(question_plan.get("must_fetch_fulltext")),
            "time_window_adjustment": dict(question_plan.get("time_window") or {}),
            "adjustment_receipt": dict(question_plan.get("adjustment_receipt") or {}),
            "category": dict(category),
            "business_rules": {
                "matched": list(rule_patch.get("matched") or []),
                "retrieval_queries": list(rule_patch.get("retrieval_queries") or []),
                "boost_terms": list(rule_patch.get("boost_terms") or []),
                "note": str(rule_patch.get("note") or ""),
            },
            "decomposition": dict(decomposition),
            "needs_multi_hop": bool(decomposition.get("is_multi_hop")),
            "answer_language": "zh-CN",
        }


__all__ = ["QaQueryPlanner", "is_high_risk_policy_question"]
