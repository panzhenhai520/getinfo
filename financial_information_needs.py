#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""把金融意图细分为可并行执行的行情、新闻和研究需求。"""

from __future__ import annotations

import re
import unicodedata
from typing import Mapping

from jsonschema import Draft202012Validator

import config


FINANCIAL_INFORMATION_NEEDS_SCHEMA_VERSION = "financial-information-needs-v1"
CHANNELS = ("quote", "news", "research")
FRESHNESS_MODES = ("latest_available", "historical", "unspecified")
FINANCIAL_INFORMATION_NEEDS_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "schema_version",
        "status",
        "channels",
        "freshness_mode",
        "information_scope",
        "needs_clarification",
        "reason_codes",
        "llm_used",
    ],
    "properties": {
        "schema_version": {
            "const": FINANCIAL_INFORMATION_NEEDS_SCHEMA_VERSION
        },
        "status": {"enum": ["skipped", "planned", "degraded"]},
        "channels": {
            "type": "array",
            "items": {"enum": list(CHANNELS)},
            "uniqueItems": True,
        },
        "freshness_mode": {"enum": list(FRESHNESS_MODES)},
        "information_scope": {
            "enum": [
                "none",
                "explicit_quote",
                "explicit_news",
                "explicit_both",
                "implicit_both",
                "research",
            ]
        },
        "needs_clarification": {"type": "boolean"},
        "reason_codes": {"type": "array", "items": {"type": "string"}},
        "llm_used": {"type": "boolean"},
    },
    "additionalProperties": False,
}
_VALIDATOR = Draft202012Validator(FINANCIAL_INFORMATION_NEEDS_SCHEMA)

_QUOTE = re.compile(
    r"股价|价格|现价|行情|涨跌|涨幅|跌幅|成交|成交量|市值|开盘|收盘|最高|最低|"
    r"多少钱|\bquote\b|\bprice\b|\bmarket cap\b",
    re.I,
)
_NEWS = re.compile(
    r"新闻|消息|公告|事件|动态|资讯|\bnews\b|\bannouncement(?:s)?\b",
    re.I,
)
_GENERIC_INFORMATION = re.compile(
    r"最新(?:的)?(?:信息|情况)|现在(?:有什么)?(?:新消息|动态)|"
    r"\blatest (?:information|updates?)\b|\bwhat(?:'s| is) new\b",
    re.I,
)
_LATEST = re.compile(
    r"今天|今日|现在|当前|实时|最新|刚刚|盘中|\btoday\b|\bnow\b|"
    r"\breal[ -]?time\b|\blatest\b",
    re.I,
)


def _setting(settings: object, name: str, default: object) -> object:
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


def validate_financial_information_needs(payload: Mapping[str, object]) -> dict:
    result = dict(payload)
    _VALIDATOR.validate(result)
    return result


def skipped_financial_information_needs(reason: str) -> dict:
    return validate_financial_information_needs(
        {
            "schema_version": FINANCIAL_INFORMATION_NEEDS_SCHEMA_VERSION,
            "status": "skipped",
            "channels": [],
            "freshness_mode": "unspecified",
            "information_scope": "none",
            "needs_clarification": False,
            "reason_codes": [str(reason)],
            "llm_used": False,
        }
    )


class FinancialInformationNeedsPlanner:
    """只规划需要查询的渠道，不选择证券代码或生成金融事实。"""

    def __init__(self, *, settings=None):
        self.settings = config if settings is None else settings

    def _enabled(self) -> bool:
        global_enabled = bool(
            _setting(self.settings, "FINANCIAL_INTELLIGENCE_ENABLED", False)
        )
        return global_enabled and bool(
            _setting(
                self.settings,
                "FINANCIAL_INFORMATION_NEEDS_ENABLED",
                global_enabled,
            )
        )

    def plan(
        self,
        question: str,
        financial_intent: Mapping[str, object],
    ) -> dict:
        if not self._enabled():
            return skipped_financial_information_needs("information_needs_disabled")
        if not bool(financial_intent.get("is_financial")):
            return skipped_financial_information_needs("not_financial")

        text = unicodedata.normalize("NFKC", str(question or "")).strip()
        intent = str(financial_intent.get("intent") or "")
        quote = bool(_QUOTE.search(text))
        news = bool(_NEWS.search(text))
        generic_information = bool(_GENERIC_INFORMATION.search(text))
        latest = bool(_LATEST.search(text)) or str(
            financial_intent.get("freshness") or ""
        ) in {"realtime", "latest"}
        reasons = []

        if intent in {"research", "comparison", "fund_research"}:
            channels = ["research"]
            scope = "research"
            reasons.append("research_intent")
        elif quote and news:
            channels = ["quote", "news"]
            scope = "explicit_both"
            reasons.extend(["explicit_quote_expression", "explicit_news_expression"])
        elif quote:
            channels = ["quote"]
            scope = "explicit_quote"
            reasons.append("explicit_quote_expression")
        elif news:
            channels = ["news"]
            scope = "explicit_news"
            reasons.append("explicit_news_expression")
        elif generic_information and latest:
            # “股票最新信息”按产品约定同时获取行情和新闻，避免单一
            # intent 让两个真实需求互相覆盖。
            channels = ["quote", "news"]
            scope = "implicit_both"
            reasons.extend(
                ["latest_expression", "generic_information_expression"]
            )
        else:
            return skipped_financial_information_needs(
                "no_actionable_information_channel"
            )

        freshness = "latest_available" if latest and "research" not in channels else (
            "historical"
            if str(financial_intent.get("freshness") or "") == "historical"
            else "unspecified"
        )
        return validate_financial_information_needs(
            {
                "schema_version": FINANCIAL_INFORMATION_NEEDS_SCHEMA_VERSION,
                "status": "planned",
                "channels": channels,
                "freshness_mode": freshness,
                "information_scope": scope,
                "needs_clarification": False,
                "reason_codes": reasons,
                "llm_used": False,
            }
        )


__all__ = [
    "FINANCIAL_INFORMATION_NEEDS_SCHEMA",
    "FINANCIAL_INFORMATION_NEEDS_SCHEMA_VERSION",
    "FinancialInformationNeedsPlanner",
    "skipped_financial_information_needs",
    "validate_financial_information_needs",
]
