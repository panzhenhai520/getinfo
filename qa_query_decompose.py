#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 9 · 子查询分解器：把逻辑类问题拆成带 `depends_on` 的**有向无环图**。

设计口径：
  · 纯规则、可解释、可单测；**不调用 LLM**（LLM 只在执行侧兜底，见 qa_pipeline）。
  · 拆不出来就老实返回单跳（`is_multi_hop=False`，并写明 `reason`），
    绝不为了"看起来像多跳"硬凑跳数。
  · 图的有效性硬校验：`depends_on` 必须指向已存在的更早跳、不能自环、不能成环、
    跳数不超过上限——校验不过直接降级为单跳。

每一跳的形状：
    {"id": "h1", "question": "...", "depends_on": [], "carry": ["entities"],
     "purpose": "为什么要有这一跳"}
执行侧会把上一跳定位到的实体作为下一跳的过滤条件（`carry` 声明要带什么）。
"""
from __future__ import annotations

import re
from typing import Dict, List, Mapping

MAX_HOPS_HARD = 5

# "A 对 B 有什么影响" → 拆成 A 的事实 + B 受到的影响
# 先用「影响」切前缀，再从「对」切 A/B：比一条大正则稳（实测大正则会把
# "2026年医保新规对民营医院有什么影响" 的 B 截成"民营"）。
_QUESTION_TAIL_RE = re.compile(r"(有何|有什么|会有什么|会有|会|能|可能|带来|产生|的|哪些|什么|怎么)+$")
_SPLIT_AT_RE = re.compile(r"^(?P<a>.{2,30}?)(?:对|向|给)(?P<b>.{2,30})$")
# "为什么 X" / "X 为什么 Y"
_WHY_RE = re.compile(r"^(?:为什么|为何)(?P<subject>[^？?。]{2,40})|(?P<subject2>[^？?。]{2,40}?)(?:为什么|为何)(?P<rest>[^？?。]{0,40})")
# "在……条件下" / "如果……是否"
_CONDITION_RE = re.compile(r"(?:在(?P<cond1>[^，。？！]{2,60}?)(?:条件|前提|情形|情况)下)|(?:如果(?P<cond2>[^，。？！]{2,60}?)(?:是否|能否|会))")
# 时序：先……再/后…… 或 先……还是……
_TEMPORAL_RE = re.compile(r"先(?P<first>[^，。？！]{2,30}?)(?:再|后|然后)(?P<second>[^，。？！]{2,30})")
_TEMPORAL_OR_RE = re.compile(r"先(?P<first>[^，。？！]{2,30}?)(?:还是|或)(?:先)?(?P<second>[^，。？！]{2,30})")


def split_impact_subjects(text: str) -> Dict[str, str]:
    """从"A 对 B 有什么影响"里抽出起点 A 与受影响的 B（抽不出就返回空串）。"""
    body = _clean(text)
    index = body.find("影响")
    if index <= 0:
        return {"a": "", "b": ""}
    prefix = body[:index].rstrip("的")
    match = _SPLIT_AT_RE.match(prefix)
    if not match:
        return {"a": "", "b": ""}
    subject_a = _clean(match.group("a"))
    subject_b = _QUESTION_TAIL_RE.sub("", _clean(match.group("b"))).strip()
    subject_b = re.sub(r"^(会|能|可能|要|将)", "", subject_b).strip()
    return {"a": subject_a, "b": subject_b}


def _clean(text) -> str:
    return " ".join(str(text or "").split())[:120]


def _hop(hop_id: str, question: str, *, depends_on=None, carry=None, purpose: str = "") -> Dict:
    return {
        "id": hop_id,
        "question": _clean(question),
        "depends_on": list(depends_on or []),
        "carry": list(carry or ["entities"]),
        "purpose": str(purpose or "")[:120],
    }


def _entity_terms(entities: List[str], topics: List[str]) -> List[str]:
    terms: List[str] = []
    for item in list(entities or []) + list(topics or []):
        text = _clean(item)
        if text and text not in terms:
            terms.append(text)
    return terms[:8]


def validate_dag(hops: List[Mapping], *, max_hops: int = MAX_HOPS_HARD) -> Dict:
    """DAG 校验：id 唯一、依赖存在且只能指向更早的跳、无自环、跳数受限。"""
    problems: List[str] = []
    if not hops:
        return {"ok": False, "problems": ["没有任何跳"]}
    if len(hops) > max_hops:
        problems.append("跳数 %d 超过上限 %d" % (len(hops), max_hops))
    seen: List[str] = []
    for index, hop in enumerate(hops):
        hop_id = str(hop.get("id") or "")
        if not hop_id:
            problems.append("第 %d 跳缺少 id" % (index + 1))
            continue
        if hop_id in seen:
            problems.append("跳 id 重复：%s" % hop_id)
        for dependency in hop.get("depends_on") or []:
            dependency = str(dependency)
            if dependency == hop_id:
                problems.append("%s 依赖了自己" % hop_id)
            elif dependency not in seen:
                problems.append("%s 依赖了不存在的或更晚的跳 %s" % (hop_id, dependency))
        seen.append(hop_id)
    return {"ok": not problems, "problems": problems}


def _single(question: str, reason: str) -> Dict:
    return {
        "is_multi_hop": False,
        "hops": [_hop("h1", question, purpose="单跳直接检索")],
        "reason": reason,
        "pattern": "single",
    }


def _two_hop_fact_then_reason(question: str, subject: str, reason_label: str, purpose: str) -> Dict:
    subject = _clean(subject) or _clean(question)
    return {
        "is_multi_hop": True,
        "hops": [
            _hop("h1", subject, purpose="先定位事实与时间线（谁、什么时候、发生了什么）"),
            _hop("h2", "%s %s" % (subject, reason_label), depends_on=["h1"],
                 purpose=purpose),
        ],
        "reason": "先事实后%s：第 2 跳带第 1 跳定位到的实体" % reason_label,
        "pattern": "fact_then_reason",
    }


def decompose(question: str, *, category: str = "", relationship: str = "",
              pack_id: str = "", entities=None, topics=None,
              max_hops: int = 3) -> Dict:
    """按问题类型产出子查询图；识别不出逻辑结构时返回单跳。"""
    text = _clean(question)
    max_hops = max(1, min(int(max_hops or 3), MAX_HOPS_HARD))
    if not text:
        return _single(question, "空问题")
    if max_hops < 2:
        return _single(question, "多跳上限为 1（QA_MAX_HOPS=1），保持单跳")

    terms = _entity_terms(list(entities or []), list(topics or []))
    graph: Dict

    # ① 多跳传导："A 对 B 有什么影响"
    subjects = split_impact_subjects(text)
    if category == "multi_hop" or subjects["a"]:
        if subjects["a"] and subjects["b"] and subjects["a"] != subjects["b"]:
            subject_a, subject_b = subjects["a"], subjects["b"]
            graph = {
                "is_multi_hop": True,
                "hops": [
                    _hop("h1", subject_a,
                         purpose="第 1 跳：把 A 的事实（政策/动作/时间）取全，作为传导起点"),
                    _hop("h2", "%s %s" % (subject_a, subject_b), depends_on=["h1"],
                         purpose="第 2 跳：找 A 与 B 之间的直接证据（同一篇或同一条链条）"),
                    _hop("h3", "%s 影响" % subject_b, depends_on=["h2"],
                         purpose="第 3 跳：补 B 侧受到的具体影响与量化结果"),
                ][:max_hops],
                "reason": "识别到「A 对 B 的影响」结构，按 起点 → 连接 → 结果 分跳",
                "pattern": "impact_chain",
                "subjects": {"a": subject_a, "b": subject_b},
                "carry_terms": terms,
            }
            return _verified(graph, max_hops)
        # 命中 multi_hop 词但没有 A/B 结构 → 用最后手段：事实 + 影响两跳
        graph = {
            "is_multi_hop": True,
            "hops": [
                _hop("h1", text, purpose="第 1 跳：先取问题本身的事实与背景"),
                _hop("h2", "%s 影响 传导" % text, depends_on=["h1"],
                     purpose="第 2 跳：带上第 1 跳实体，找影响与传导路径"),
            ][:max_hops],
            "reason": "识别到传导类问法（未解析出 A/B 主体），退化为事实 + 影响两跳",
            "pattern": "impact_generic",
            "carry_terms": terms,
        }
        return _verified(graph, max_hops)

    # ② 因果："为什么 X"
    if category == "causal":
        why = _WHY_RE.search(text)
        subject = ""
        if why:
            subject = _clean(why.group("subject") or why.group("subject2"))
        graph = _two_hop_fact_then_reason(
            text, subject or text, "原因 驱动因素",
            "第 2 跳：找原因与驱动因素（`因为/由于/导致` 类证据）")
        graph["carry_terms"] = terms
        return _verified(graph, max_hops)

    # ③ 条件约束："在……条件下，……是否……"
    if category == "conditional_constraint":
        condition = _CONDITION_RE.search(text)
        condition_text = ""
        if condition:
            condition_text = _clean(condition.group("cond1") or condition.group("cond2"))
        graph = {
            "is_multi_hop": True,
            "hops": [
                _hop("h1", condition_text or text,
                     purpose="第 1 跳：先把条件本身的规定取全（谁、什么条件、什么口径）"),
                _hop("h2", "%s 是否满足 适用" % (condition_text or text), depends_on=["h1"],
                     purpose="第 2 跳：在条件成立的前提下核对是否满足、有无例外"),
            ][:max_hops],
            "reason": "识别到条件式问法：先取条件，再判定是否满足",
            "pattern": "condition_check",
            "carry_terms": terms,
        }
        return _verified(graph, max_hops)

    # ④ 时序关系："先……后……" / "先……还是……" / 时间线
    if category == "temporal_relation":
        temporal = _TEMPORAL_RE.search(text) or _TEMPORAL_OR_RE.search(text)
        if temporal:
            graph = {
                "is_multi_hop": True,
                "hops": [
                    _hop("h1", _clean(temporal.group("first")),
                         purpose="第 1 跳：取前一件事的事实与时间"),
                    _hop("h2", _clean(temporal.group("second")), depends_on=["h1"],
                         purpose="第 2 跳：取后一件事的事实与时间，用于判定先后"),
                ][:max_hops],
                "reason": "识别到「先……后……/还是」结构，分别取两件事的时间",
                "pattern": "temporal_pair",
                "carry_terms": terms,
            }
            return _verified(graph, max_hops)
        graph = _two_hop_fact_then_reason(
            text, text, "时间线 先后顺序",
            "第 2 跳：补时间线，确认先后关系")
        graph["pattern"] = "temporal_timeline"
        graph["carry_terms"] = terms
        return _verified(graph, max_hops)

    # ⑤ 其它情况一律保持单跳（不改变既有路径）
    return _single(question, "保持单跳：问题不含可分解的逻辑结构（类别=%s）" % (category or "未知"))


def _verified(graph: Dict, max_hops: int) -> Dict:
    """统一走一次 DAG 校验；不通过就降级为单跳并写明原因。"""
    check = validate_dag(graph.get("hops") or [], max_hops=max_hops)
    if check["ok"]:
        graph["dag_ok"] = True
        return graph
    question = (graph.get("hops") or [{}])[0].get("question") or ""
    fallback = _single(question, "子查询图校验失败（%s），已降级为单跳" % "；".join(check["problems"])[:120])
    fallback["pattern"] = "dag_invalid"
    return fallback


__all__ = ["decompose", "validate_dag"]
