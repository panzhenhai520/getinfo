#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Jinja2-backed dynamic status text for the unified QA planner."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

try:
    from jinja2 import StrictUndefined
    from jinja2.sandbox import SandboxedEnvironment
except Exception:  # pragma: no cover - import guard for minimal runtimes
    StrictUndefined = None
    SandboxedEnvironment = None


_TEMPLATE_DIR = Path(__file__).resolve().parent / "qa_templates"
_TEMPLATE_NAME = "question_plan_status.zh.j2"


def _compact(value: str) -> str:
    return " ".join(str(value or "").split())


def _fallback(plan: Mapping) -> str:
    count = int(plan.get("question_count") or 1)
    relation_label = str(plan.get("relationship_label") or "相关问题")
    strategy = str(plan.get("answer_strategy") or "先规划检索路径，再生成证据约束结论。")
    if count <= 1:
        return _compact(f"已识别为 1 个单一问题。我会先查权威资料，再回答。{strategy}")
    return _compact(f"已识别为 {count} 个{relation_label}。{strategy}")


def render_question_plan_answer(plan: Mapping) -> str:
    """Render a concise answer-body prelude from a structured question plan."""
    if not isinstance(plan, Mapping):
        return ""
    try:
        count = int(plan.get("question_count") or 1)
    except (TypeError, ValueError):
        count = 1
    relation_label = str(plan.get("relationship_label") or "")
    if not relation_label:
        relation_label = {
            "single": "单一问题",
            "parallel": "并列问题",
            "progressive": "递进问题",
            "causal": "因果问题",
            "comparison": "比较问题",
            "parent_child": "总分问题",
            "overlap": "交集问题",
            "conflict": "冲突核验问题",
        }.get(str(plan.get("relationship") or ""), "相关问题")
    lines = ["【问题分析思路：】"]
    if count <= 1:
        subquestions = [item for item in plan.get("subquestions") or [] if isinstance(item, Mapping)]
        first = str(subquestions[0].get("text") or "").strip() if subquestions else ""
        lines.append(f"- 已识别为 1 个单一问题{f'：{first}' if first else ''}。")
    else:
        lines.append(f"- 已识别为 {count} 个{relation_label}。")
        for item in [item for item in plan.get("subquestions") or [] if isinstance(item, Mapping)][:4]:
            qid = str(item.get("id") or "").replace("q", "")
            text = str(item.get("text") or "").strip()
            if text:
                lines.append(f"- 问题 {qid or len(lines)}：{text}")
        if count > 4:
            lines.append("- 其余问题会先合并同类项，再按主题分组回答。")
    categories = [
        str(item.get("label") or "")
        for item in plan.get("categories") or []
        if isinstance(item, Mapping) and item.get("label")
    ]
    if categories:
        unique_categories = list(dict.fromkeys(categories))
        lines.append(f"- 问题拆分为{len(unique_categories)}个类型：" + "、".join(unique_categories))
    outline = [str(item) for item in plan.get("answer_outline") or [] if str(item).strip()]
    if outline:
        cleaned = [item.lstrip("先再然后最后，,：: ") for item in outline[:5]]
        prefixes = ["我会先", "再", "然后", "最后"]
        steps = []
        for index, item in enumerate(cleaned):
            prefix = prefixes[index] if index < len(prefixes) else "然后"
            steps.append(prefix + item)
        lines.append("- " + " → ".join(steps) + "。")
    elif plan.get("answer_strategy"):
        lines.append("- 我会按这个策略继续：" + str(plan.get("answer_strategy")))
    return "\n".join(lines)


def render_question_plan_status(plan: Mapping) -> str:
    """Render the first streaming progress sentence from a structured plan."""
    if not isinstance(plan, Mapping):
        return "正在拆解问题并规划检索路径。"
    if SandboxedEnvironment is None:
        return _fallback(plan)
    try:
        env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, trim_blocks=True, lstrip_blocks=True)
        template = env.get_template(_TEMPLATE_NAME) if False else env.from_string((_TEMPLATE_DIR / _TEMPLATE_NAME).read_text("utf-8"))
        return _compact(template.render(plan=plan))
    except Exception:
        return _fallback(plan)


__all__ = ["render_question_plan_answer", "render_question_plan_status"]
