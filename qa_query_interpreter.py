#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 05（P05-01）· Query Interpreter：把问题读成**结构化意图**，不负责回答。

通用包 01_V2_ARCHITECTURE §6 原文："第一步不是 embedding，而是理解问题结构"，
输出 intent / entities / time_scope / constraints / required_claims / freshness_required /
answer_type / complexity，并规定"若 complexity = simple 直接进入 Fast Path"。

本仓库的落地口径（**复用优先，规则实现，零模型调用**）
--------------------------------------------------------
复用（一行新分类规则都不写）：
  · `qa_planner._CATEGORY_RULES` / `_question_category` —— 既有的 12 类问题分类器；
  · `qa_planner._COMPARE_RE` / `_CONFLICT_RE` / `_MULTI_HOP_CATEGORIES` —— 既有比较/冲突/多跳口径；
  · `qa_planner._date_scope` —— 既有的时间范围解析（显式年份 / 相对时间 / 开放区间）；
  · `qa_planner._split_subquestions` / `_question_relationship` —— 既有的子问题切分与关系判定；
  · `qa_planner._needs_article_retrieval` —— 既有的"要不要检索"判定（闲聊不检索）；
  · `qa_planner._clean_query` / `is_high_risk_policy_question` —— 既有的注入清洗与政策风险识别；
  · `QaQueryPlanner.plan()` 的输出（可选入参 `plan`）—— 实体/主题/检索轴/时间窗/输出形式
    已经是**行业包词表驱动**的现成结果，解释器直接消费，不重新做 NER。
新增：
  · §6 的 9 值意图枚举映射（`intent_of`）、复杂度三档（`complexity_of`）、答案形态（`answer_type_of`）；
  · `required_claims`：把"要证实/证伪什么"显式声明出来（role=answer/cause/link/counter）；
  · **可插拔接口**（`register_query_interpreter` + `QA_QUERY_INTERPRETER`），
    默认后端 `rules`。

能力边界（必须如实说明，见 DECISION_LOG D-019）
------------------------------------------------
本轮硬约束禁止调用任何模型/嵌入端点（GPU 机与语音机器人共用、已停用）。因此：
  1. 实体抽取**不做新 NER**：只用规划器从行业包词表里匹配到的实体；没有 `plan` 入参时
     `entities` 就是空数组（宁可空着，也不编）。
  2. `required_claims.statement` 是**待证命题声明**（role + 证据类型绑定是真的，可机器消费），
     但"把自然语言问题改写成规范命题"这件事规则做不到——需要 LLM 版本解释器
     （注册 `register_query_interpreter("llm", fn)` 即可接入），本轮**只留注入点、不调用**。
  3. 复杂度的判断是**规则可解释**的，不是模型打分；边界问题（例如 60 字的阈值）
     在 `complexity_of` 里写明依据，便于复算。
"""
from __future__ import annotations

import os
import re
from typing import Callable, Mapping

from qa_graph_contracts import (
    PLAN_CLAIM_ROLES,
    QA_ANSWER_TYPES,
    QUERY_CAUSAL,
    QUERY_COMPARISON,
    QUERY_COMPLEXITIES,
    QUERY_COMPLEXITY_DEEP,
    QUERY_COMPLEXITY_SIMPLE,
    QUERY_COMPLEXITY_STANDARD,
    QUERY_DIAGNOSTIC,
    QUERY_INTENTS,
    QUERY_INTERPRETER_VERSION,
    QUERY_MECHANISM,
    QUERY_MULTI_ENTITY,
    QUERY_MULTI_HOP,
    QUERY_SIMPLE_FACT,
    QUERY_SYNTHESIS,
    QUERY_TEMPORAL,
)

# ── 复用既有规则模块（不复制规则、不新建分类器）─────────────────────────────
from qa_planner import (  # noqa: E402  （复用既有实现，见模块 docstring 的"复用"清单）
    _COMPARE_RE,
    _CONFLICT_RE,
    _MULTI_HOP_CATEGORIES,
    _clean_query,
    _date_scope,
    _needs_article_retrieval,
    _question_category,
    _question_relationship,
    _split_subquestions,
    is_high_risk_policy_question,
)

# "机制/原理"类问法：与 causal 同类但答案形态不同（要的是通路而不是原因列表）
_MECHANISM_RE = re.compile(
    r"机制|机理|原理|路径|通路|pathway|怎么(会|能)?(起|发生|产生)|如何(起|产生)作用|传导过程|作用过程",
    re.I)
# 比较类问法的补充识别（既有 `_COMPARE_RE` 只认"比较/对比/区别/横向/不同/vs"，
# 认不出"谁更高/哪个更"这类比较级提问）——**P05-01 新增的最小规则**，只用于意图判定，
# 不改既有分类器与检索行为。
_COMPARATIVE_ASK_RE = re.compile(r"谁(更|最|高|低|好|强)|哪个(更|最)|哪家(更|最)|孰(高|低|优)|差距|差额", re.I)
# 时效敏感：答案会随时间变化，必须走 revalidate（§10 硬规则）
_FRESHNESS_RE = re.compile(r"最新|最近|近期|现行|目前|当前|今年|本年度|本季度|本月|现在|截至")
# 简单事实类：单问句 + 这些类别 + 没有比较/冲突/多跳结构 → fast path
_SIMPLE_CATEGORIES = frozenset({
    "fact_check", "policy_content", "subject_scope", "filing_collection",
})
# 需要综合多来源的类别
_SYNTHESIS_CATEGORIES = frozenset({"industry_impact", "risk_response", "evidence_gap"})

# 意图 → 角色（决定 required_claims 的 role）
_ROLE_BY_INTENT = {
    QUERY_SIMPLE_FACT: "answer",
    QUERY_MULTI_ENTITY: "answer",
    QUERY_COMPARISON: "answer",
    QUERY_TEMPORAL: "answer",
    QUERY_CAUSAL: "cause",
    QUERY_MECHANISM: "mechanism",
    QUERY_DIAGNOSTIC: "answer",
    QUERY_MULTI_HOP: "link",
    QUERY_SYNTHESIS: "answer",
}
# 意图 → 答案形态
_ANSWER_TYPE_BY_INTENT = {
    QUERY_SIMPLE_FACT: "fact",
    QUERY_MULTI_ENTITY: "fact",
    QUERY_COMPARISON: "comparison",
    QUERY_TEMPORAL: "timeline",
    QUERY_CAUSAL: "causal_explanation",
    QUERY_MECHANISM: "causal_explanation",
    QUERY_DIAGNOSTIC: "diagnostic",
    QUERY_MULTI_HOP: "evidence_synthesis",
    QUERY_SYNTHESIS: "evidence_synthesis",
}

# 复用清单（写进解释结果，便于验收时逐条核对"复用了什么"）
REUSE_MAP = {
    "category_rules": "qa_planner._CATEGORY_RULES/_question_category",
    "compare_conflict": "qa_planner._COMPARE_RE/_CONFLICT_RE",
    "multi_hop_categories": "qa_planner._MULTI_HOP_CATEGORIES",
    "time_scope": "qa_planner._date_scope",
    "sub_question_split": "qa_planner._split_subquestions/_question_relationship",
    "needs_retrieval": "qa_planner._needs_article_retrieval",
    "entities_topics": "QaQueryPlanner.plan()（行业包词表驱动，不另做 NER）",
    "policy_risk": "qa_planner.is_high_risk_policy_question",
}

# ── 可插拔后端（LLM 版本只留注入点，本轮不实现、不调用）─────────────────────
_INTERPRETER_BACKENDS: dict = {}


def register_query_interpreter(name: str, backend: Callable | None) -> None:
    """注册一个解释器后端；`backend=None` 取消注册。

    后端签名：`backend(question, context: dict) -> dict`，返回字段需满足
    `qa_graph_contracts.validate("query_interpretation", payload)`。
    **本轮不提供任何内置模型后端**：默认后端是规则实现 `rules`；
    想接 LLM 的调用方自己注册（GPU/端点禁用约束见 DECISION_LOG D-018/D-019）。
    """
    key = str(name or "").strip()
    if not key or key == "rules":
        raise ValueError("解释器名不能为空，也不能覆盖内置的 rules 后端")
    if backend is None:
        _INTERPRETER_BACKENDS.pop(key, None)
        return
    if not callable(backend):
        raise TypeError("解释器后端必须可调用")
    _INTERPRETER_BACKENDS[key] = backend


def registered_interpreters() -> list:
    return sorted(_INTERPRETER_BACKENDS)


def selected_interpreter() -> str:
    """环境变量选择后端（默认 `rules`；未注册名会保守回落到 rules）。"""
    return str(os.getenv("QA_QUERY_INTERPRETER", "") or "rules").strip() or "rules"


def intent_of(category: Mapping | str, *, relationship: str = "", question: str = "",
              entities=None) -> str:
    """把既有分类类别映射到 §6 的 9 值意图（纯规则、可解释、可复算）。

    规则顺序（越具体越优先）：
      1. 无关检索（闲聊/自指） → SIMPLE_FACT（下游走 fake-answer，不检索）；
      2. 比较/对比（既有正则或 relationship=comparison） → COMPARISON；
      3. 口径冲突（既有正则或 relationship=conflict） → SYNTHESIS（多口径综合裁决）；
      4. 既有 `_MULTI_HOP_CATEGORIES` 四类：multi_hop→MULTI_HOP / causal→CAUSAL 或 MECHANISM /
         conditional_constraint→DIAGNOSTIC / temporal_relation→TEMPORAL；
      5. 行业影响/风险/证据缺口 → SYNTHESIS；
      6. 命中 ≥2 个行业包实体 → MULTI_ENTITY；
      7. 其余 → SIMPLE_FACT（单问题）或 SYNTHESIS（多问句）。
    """
    key = str(category.get("key") if isinstance(category, Mapping) else category or "")
    text = str(question or "")
    if not _needs_article_retrieval(text):
        return QUERY_SIMPLE_FACT
    if relationship == "comparison" or _COMPARE_RE.search(text) or _COMPARATIVE_ASK_RE.search(text):
        return QUERY_COMPARISON
    if relationship == "conflict" or _CONFLICT_RE.search(text):
        return QUERY_SYNTHESIS
    if key in _MULTI_HOP_CATEGORIES:
        if key == "multi_hop":
            return QUERY_MULTI_HOP
        if key == "causal":
            return QUERY_MECHANISM if _MECHANISM_RE.search(text) else QUERY_CAUSAL
        if key == "conditional_constraint":
            return QUERY_DIAGNOSTIC
        if key == "temporal_relation":
            return QUERY_TEMPORAL
    # 机制/原理是"问题结构"，与内部分类无关：问"机制/通路"的一律按 MECHANISM 处理
    if _MECHANISM_RE.search(text):
        return QUERY_MECHANISM
    if key in _SYNTHESIS_CATEGORIES:
        return QUERY_SYNTHESIS
    if len([item for item in (entities or []) if str(item).strip()]) >= 2:
        return QUERY_MULTI_ENTITY
    return QUERY_SIMPLE_FACT


def complexity_of(intent: str, *, category: Mapping | str = "", relationship: str = "",
                  question: str = "", question_count: int = 1,
                  needs_retrieval: bool = True) -> tuple:
    """复杂度三档 + 判断依据（返回 `(complexity, reasons)`，reasons 便于复算）。

    口径（§6 "complexity = simple 直接进入 Fast Path"）：
      · `simple`：不需要检索（闲聊）；或"单问句 + 简单事实类类别 + 无比较/冲突/多跳结构
        + 问句 ≤ 60 字"；
      · `deep`：命中多跳四类之一，或比较/冲突，或 ≥3 个实体，或 ≥3 个问句；
      · 其余 `standard`。
    """
    key = str(category.get("key") if isinstance(category, Mapping) else category or "")
    text = str(question or "")
    reasons: list = []
    if not needs_retrieval:
        return QUERY_COMPLEXITY_SIMPLE, ["不需要检索（闲聊/自指类问题）"]
    if key in _MULTI_HOP_CATEGORIES:
        reasons.append("命中多跳类别 %s" % key)
    if intent in (QUERY_COMPARISON, QUERY_SYNTHESIS) or relationship in ("comparison", "conflict"):
        reasons.append("比较/冲突/综合类")
    if intent in (QUERY_MULTI_HOP, QUERY_CAUSAL, QUERY_MECHANISM, QUERY_DIAGNOSTIC):
        reasons.append("意图=%s 需要多步推理" % intent)
    if int(question_count or 1) >= 3:
        reasons.append("问句数 %d ≥ 3" % int(question_count))
    if reasons:
        return QUERY_COMPLEXITY_DEEP, reasons
    if (int(question_count or 1) <= 1 and key in _SIMPLE_CATEGORIES
            and not _COMPARE_RE.search(text) and not _CONFLICT_RE.search(text)
            and len(text) <= 60):
        return QUERY_COMPLEXITY_SIMPLE, ["单问句 + 简单事实类 %s + 无逻辑结构 + ≤60 字" % key]
    return QUERY_COMPLEXITY_STANDARD, ["非简单事实、也非多跳结构 → standard"]


def answer_type_of(intent: str, *, needs_retrieval: bool = True) -> str:
    if not needs_retrieval:
        return "no_answer"
    value = _ANSWER_TYPE_BY_INTENT.get(str(intent), "evidence_synthesis")
    return value if value in QA_ANSWER_TYPES else "evidence_synthesis"


def _constraints(plan: Mapping | None, time_scope: Mapping) -> list:
    """把规划器已经解析出来的约束**投影**成显式清单（不新解析一遍）。"""
    items: list = []
    plan = plan or {}
    question_plan = plan.get("question_plan") if isinstance(plan.get("question_plan"), Mapping) else {}
    if question_plan.get("output_form"):
        items.append({"key": "output_form", "value": str(question_plan["output_form"]),
                      "source": "question_plan"})
    if question_plan.get("must_fetch_fulltext") or plan.get("must_fetch_fulltext"):
        items.append({"key": "must_fetch_fulltext", "value": True, "source": "question_plan"})
    window = plan.get("time_window_adjustment") or question_plan.get("time_window") or {}
    if isinstance(window, Mapping) and window:
        items.append({"key": "time_window", "value": dict(window), "source": "adjustment"})
    if time_scope:
        items.append({"key": "time_scope", "value": dict(time_scope), "source": "date_scope"})
    for axis in plan.get("research_axes") or []:
        items.append({"key": "research_axis", "value": str(axis), "source": "planner"})
    for source in plan.get("requested_sources") or []:
        items.append({"key": "requested_source", "value": str(source), "source": "policy_anchors"})
    return items


def required_claims(question: str, *, sub_questions=None, intent: str = QUERY_SIMPLE_FACT,
                    role: str = "answer") -> list:
    """§7 的 Claim 声明：每条子问题一个待证命题 + 一条"反证/替代解释"命题。

    诚实边界（能力边界第 2 条）：规则只能给出**待证命题声明**（role/证据要求绑定是真的），
    不能把自然语言问题改写成规范命题——那需要 LLM 版本解释器（注入点已留，本轮不调用）。
    反证那条标 `plan_only=True`：它的检索与判定属 Phase 06/07/13，本轮不执行、不假装跑过。
    """
    text = str(question or "")
    claims: list = []
    items = list(sub_questions or [])
    role = role if role in PLAN_CLAIM_ROLES else _ROLE_BY_INTENT.get(str(intent), "answer")
    if not items:
        claims.append({
            "claim_id": "c:q", "plan_node_kind": "claim",
            "statement": "需要证实或证伪：%s" % text,
            "role": role,
            "sub_question_id": "",
        })
    else:
        for index, item in enumerate(items, 1):
            sid = str(item.get("id") or ("q%d" % index)) if isinstance(item, Mapping) else str(item)
            sub_text = str(item.get("text") or item.get("statement") or "") if isinstance(item, Mapping) else str(item)
            claims.append({
                "claim_id": "c:%s" % sid, "plan_node_kind": "claim",
                "statement": "需要证实或证伪：%s" % (sub_text or text),
                "role": role,
                "sub_question_id": sid,
            })
    claims.append({
        "claim_id": "c:counter", "plan_node_kind": "claim",
        "statement": "是否存在反证或替代解释（需要独立来源，不能只靠支持性证据）",
        "role": "counter", "sub_question_id": "", "plan_only": True,
    })
    return claims


def interpret_with_rules(question: str, *, plan: Mapping | None = None,
                         category: Mapping | str = "", relationship: str = "",
                         entities=None, now=None) -> dict:
    """规则解释器（默认后端）：零模型调用、零网络、可复算。"""
    text = _clean_query(question)
    plan = plan or {}
    question_plan = plan.get("question_plan") if isinstance(plan.get("question_plan"), Mapping) else {}
    category = category or plan.get("category") or _question_category(text)
    relationship = str(relationship or question_plan.get("relationship") or "")
    sub_questions = list(question_plan.get("subquestions") or [])
    if not sub_questions:
        sub_questions = [{"id": "q%d" % (i + 1), "text": item}
                         for i, item in enumerate(_split_subquestions(text))]
    if not relationship:
        relationship = _question_relationship([str(item.get("text") or "") for item in sub_questions])
    entity_list = [str(item) for item in (entities if entities is not None else plan.get("entities") or [])
                   if str(item).strip()]
    needs_retrieval = _needs_article_retrieval(text)
    time_scope = dict(plan.get("time_scope") or _date_scope(text, now or _now()))
    intent = intent_of(category, relationship=relationship, question=text, entities=entity_list)
    complexity, reasons = complexity_of(
        intent, category=category, relationship=relationship, question=text,
        question_count=len(sub_questions) or 1, needs_retrieval=needs_retrieval)
    role = _ROLE_BY_INTENT.get(intent, "answer")
    return {
        "interpreter_version": QUERY_INTERPRETER_VERSION,
        "backend": "rules",
        "backend_source": "rules",
        "fallback": {"used": False, "from": "", "reason": ""},
        "question": text,
        "intent": intent,
        "entities": entity_list[:30],
        "time_scope": time_scope,
        "constraints": _constraints(plan, time_scope),
        "required_claims": required_claims(text, sub_questions=sub_questions, intent=intent, role=role),
        "freshness_required": bool(_FRESHNESS_RE.search(text))
        or str(time_scope.get("source") or "") == "relative",
        "answer_type": answer_type_of(intent, needs_retrieval=needs_retrieval),
        "complexity": complexity,
        "complexity_reasons": reasons,
        "category": dict(category) if isinstance(category, Mapping) else {"key": str(category)},
        "relationship": relationship,
        "question_count": len(sub_questions) or 1,
        "sub_questions": sub_questions,
        "needs_retrieval": needs_retrieval,
        "high_risk_policy": bool(is_high_risk_policy_question(text) or plan.get("high_risk_policy")),
        "axes": [str(item) for item in (plan.get("research_axes") or [])],
        "reuse": dict(REUSE_MAP),
    }


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def interpret_query(question: str, *, plan: Mapping | None = None, category: Mapping | str = "",
                    relationship: str = "", entities=None, now=None,
                    backend: str = "") -> dict:
    """解释一个问题 → §6 的结构化意图（可插拔：默认 rules，未注册/失败一律保守回落）。

    失败路径（有用例钉死）：
      · 环境变量/入参指定的后端**未注册** → 回落 rules，`fallback.used=True`；
      · 注册的后端抛错 → 回落 rules，错误写进 `fallback.reason`；
      · 注册的后端返回非法载荷（缺 required 或枚举越界）→ 回落 rules 并写明校验失败原因。
    任何情况下都**不会**因为没有后端就不给规划结果。
    """
    name = str(backend or selected_interpreter() or "rules").strip() or "rules"
    if name == "rules" or name not in _INTERPRETER_BACKENDS:
        result = interpret_with_rules(question, plan=plan, category=category,
                                      relationship=relationship, entities=entities, now=now)
        if name != "rules":
            result["fallback"] = {"used": True, "from": name,
                                  "reason": "未注册的解释器后端，已保守回落到规则实现"}
        return result
    try:
        payload = _INTERPRETER_BACKENDS[name](str(question or ""), {
            "plan": dict(plan or {}), "category": category, "relationship": relationship,
            "entities": list(entities or []) if entities is not None else None,
        })
        candidate = dict(payload or {})
        candidate.setdefault("interpreter_version", QUERY_INTERPRETER_VERSION)
        candidate["backend"] = name
        candidate["backend_source"] = "registered:%s" % name
        candidate.setdefault("fallback", {"used": False, "from": "", "reason": ""})
        if not candidate.get("fallback", {}).get("used") and not _valid_interpretation(candidate):
            raise ValueError("返回载荷不满足 query_interpretation 契约")
        return candidate
    except Exception as exc:  # 后端坏掉不许把规划拖死
        fallback_result = interpret_with_rules(question, plan=plan, category=category,
                                               relationship=relationship, entities=entities, now=now)
        fallback_result["fallback"] = {"used": True, "from": name,
                                       "reason": "%s: %s" % (type(exc).__name__, str(exc)[:180])}
        return fallback_result


def _valid_interpretation(payload: Mapping) -> bool:
    """用契约层做自校验（不引入 jsonschema 依赖）。"""
    try:
        from qa_graph_contracts import validate

        ok, _note = validate("query_interpretation", dict(payload))
        return bool(ok)
    except Exception:
        return False


def interpretation_receipt(interpretation: Mapping | None) -> dict:
    """给管线用的短回执（stats 的**兄弟键**内容，不动任何既有键集）。"""
    value = dict(interpretation or {})
    if not value:
        return {}
    claims = list(value.get("required_claims") or [])
    return {
        "interpreter_version": str(value.get("interpreter_version") or ""),
        "backend": str(value.get("backend_source") or value.get("backend") or ""),
        "fallback": dict(value.get("fallback") or {}),
        "intent": str(value.get("intent") or ""),
        "complexity": str(value.get("complexity") or ""),
        "complexity_reasons": list(value.get("complexity_reasons") or []),
        "answer_type": str(value.get("answer_type") or ""),
        "freshness_required": bool(value.get("freshness_required")),
        "entity_count": len(value.get("entities") or []),
        "constraint_count": len(value.get("constraints") or []),
        "claim_count": len(claims),
        "counter_claim_planned": any(str(item.get("role")) == "counter" for item in claims),
    }


def describe() -> str:
    return ("Query Interpreter %s（规则实现，零模型调用）：意图 %d 值 / 复杂度 %s / "
            "已注册后端 %s"
            % (QUERY_INTERPRETER_VERSION, len(QUERY_INTENTS), "/".join(QUERY_COMPLEXITIES),
               "、".join(registered_interpreters()) or "无（只有内置 rules）"))


__all__ = [
    "REUSE_MAP",
    "answer_type_of",
    "complexity_of",
    "describe",
    "intent_of",
    "interpret_query",
    "interpret_with_rules",
    "interpretation_receipt",
    "register_query_interpreter",
    "registered_interpreters",
    "required_claims",
    "selected_interpreter",
]
