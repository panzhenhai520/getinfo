#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Authenticated, allow-listed projection of saved TradingAgents reports."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping

from financial_instruments import stable_instrument_key
from financial_security import redact_sensitive_text


FINANCIAL_REPORT_VIEW_VERSION = "financial-report-view-v1"
TERMINAL_REPORT_DENYLIST = frozenset({"", "draft", "failed", "cancelled"})

ROLE_PRESENTATION = {
    "market_analyst": ("市场技术分析", "analysis"),
    "sentiment_analyst": ("情绪分析", "analysis"),
    "news_analyst": ("新闻与宏观分析", "analysis"),
    "fundamentals_analyst": ("基本面分析", "analysis"),
    "index_identity_analyst": ("指数身份与口径", "analysis"),
    "index_technical_analyst": ("指数技术分析", "analysis"),
    "breadth_liquidity_analyst": ("市场宽度与流动性", "analysis"),
    "constituents_rotation_analyst": ("成分与行业轮动", "analysis"),
    "macro_policy_analyst": ("宏观与政策", "analysis"),
    "bull_researcher": ("多方研究员", "bull_bear"),
    "bear_researcher": ("空方研究员", "bull_bear"),
    "investment_debate": ("多空讨论记录", "bull_bear"),
    "research_manager": ("研究经理评估", "decision"),
    "trader": ("Trader 方案", "decision"),
    "market_strategy_analyst": ("市场策略方案", "decision"),
    "aggressive_risk_analyst": ("进取风险观点", "risk"),
    "conservative_risk_analyst": ("保守风险观点", "risk"),
    "neutral_risk_analyst": ("中性风险观点", "risk"),
    "risk_debate": ("风险讨论记录", "risk"),
    "portfolio_manager": ("Portfolio Manager 终极结论", "final"),
}
GROUPS = (
    {"key": "analysis", "name": "分析师报告"},
    {"key": "bull_bear", "name": "多空研究动态讨论"},
    {"key": "decision", "name": "研究经理与 Trader"},
    {"key": "risk", "name": "风险管理讨论"},
    {"key": "final", "name": "协同终极评估"},
)
STOCK_EXPECTED_ROLES = (
    "market_analyst", "sentiment_analyst", "news_analyst", "fundamentals_analyst",
    "bull_researcher", "bear_researcher", "investment_debate", "research_manager",
    "trader", "aggressive_risk_analyst", "conservative_risk_analyst",
    "neutral_risk_analyst", "risk_debate", "portfolio_manager",
)
INDEX_EXPECTED_ROLES = (
    "index_identity_analyst", "index_technical_analyst", "breadth_liquidity_analyst",
    "constituents_rotation_analyst", "macro_policy_analyst", "bull_researcher",
    "bear_researcher", "investment_debate", "research_manager",
    "market_strategy_analyst", "aggressive_risk_analyst",
    "conservative_risk_analyst", "neutral_risk_analyst", "risk_debate",
    "portfolio_manager",
)


def _json_value(value, default):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _text(value, maximum: int = 20000) -> str:
    return redact_sensitive_text(value, maximum=maximum).strip()


def _string_list(value, maximum: int = 50) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item, 300) for item in value[:maximum] if _text(item, 300)]


def _safe_risk_summary(value) -> dict:
    source = _json_value(value, {})
    if not isinstance(source, Mapping):
        return {}
    result = {}
    for key in (
        "risk_level", "summary", "assessment", "key_risks", "risk_factors",
        "constraints", "aggressive", "conservative", "neutral",
    ):
        item = source.get(key)
        if isinstance(item, list):
            result[key] = [_text(entry, 2000) for entry in item[:20] if _text(entry, 2000)]
        elif isinstance(item, (str, int, float, bool)):
            result[key] = _text(item, 12000)
    return result


def _safe_citations(value) -> list[dict]:
    source = _json_value(value, [])
    if not isinstance(source, list):
        return []
    result = []
    for item in source[:200]:
        if not isinstance(item, Mapping):
            continue
        citation = {
            "evidence_id": int(item["evidence_id"]) if str(item.get("evidence_id") or "").isdigit() else None,
            "kind": _text(item.get("kind"), 80),
            "snapshot_id": int(item["snapshot_id"]) if str(item.get("snapshot_id") or "").isdigit() else None,
            "article_id": int(item["article_id"]) if str(item.get("article_id") or "").isdigit() else None,
            "role": _text(item.get("role"), 100),
            "observed_at": _text(item.get("observed_at"), 80),
        }
        if any(value not in {None, ""} for value in citation.values()):
            result.append(citation)
    return result


def _expected_roles(report_json: Mapping[str, object]) -> tuple[str, ...]:
    graph_version = str(report_json.get("graph_version") or "").casefold()
    if "index" in graph_version:
        return INDEX_EXPECTED_ROLES
    if "stock" in graph_version:
        return STOCK_EXPECTED_ROLES
    return ()


class FinancialReportView:
    """Read report and public role outputs without exposing prompts or model internals."""

    def __init__(self, database):
        self.database = database

    @property
    def connection(self):
        self.database._ensure_connection()
        return self.database.connection

    def get(self, report_id: int) -> dict | None:
        with self.database.lock:
            row = self.connection.execute(
                """
                SELECT report.id, report.research_run_id, report.report_version,
                       report.report_status, report.recommendation, report.confidence,
                       report.title, report.executive_summary, report.report_markdown,
                       report.report_json, report.risk_summary_json,
                       report.suitability_notice, report.disclaimer,
                       report.observed_at, report.fetched_at, report.verified_at,
                       report.created_at, report.updated_at,
                       run.scope_type, run.instrument_id, run.universe_id,
                       instrument.canonical_symbol, instrument.display_name,
                       instrument.asset_type, instrument.market, instrument.exchange,
                       instrument.currency, instrument.country_code, universe.universe_key,
                       universe.display_name, universe.universe_type, universe.market
                FROM financial_final_reports report
                JOIN financial_research_runs run ON run.id=report.research_run_id
                LEFT JOIN financial_instruments instrument ON instrument.id=run.instrument_id
                LEFT JOIN financial_universes universe ON universe.id=run.universe_id
                WHERE report.id=?
                """,
                (int(report_id),),
            ).fetchone()
            if row is None or str(row[3] or "").casefold() in TERMINAL_REPORT_DENYLIST:
                return None
            section_rows = self.connection.execute(
                """
                SELECT id, role_key, section_type, sequence_no, status,
                       content_markdown, citations_json, created_at, updated_at
                FROM financial_report_sections
                WHERE research_run_id=?
                ORDER BY sequence_no ASC, id ASC
                """,
                (str(row[1]),),
            ).fetchall()

        report_json = _json_value(row[9], {})
        if not isinstance(report_json, Mapping):
            report_json = {}
        risk_summary = _safe_risk_summary(row[10])
        sections = []
        role_status = {}
        citation_identities = set()
        for section_row in section_rows:
            role_key = _text(section_row[1], 100)
            role_label, group = ROLE_PRESENTATION.get(role_key, (role_key or "其他角色", "analysis"))
            citations = _safe_citations(section_row[6])
            for citation in citations:
                citation_identities.add((citation["evidence_id"], citation["snapshot_id"], citation["article_id"]))
            # Role reports are intentionally lossless at this projection boundary;
            # redact secrets without imposing a new content truncation limit.
            content = _text(section_row[5], max(len(str(section_row[5] or "")) + 1, 20000))
            status = _text(section_row[4], 50) or ("completed" if content.strip() else "unavailable")
            role_status[role_key] = status if content.strip() else "unavailable"
            sections.append(
                {
                    "section_id": int(section_row[0]),
                    "role_key": role_key,
                    "role_label": role_label,
                    "group": group,
                    "section_type": _text(section_row[2], 100),
                    "sequence_no": int(section_row[3]),
                    "status": role_status[role_key],
                    "content_markdown": content,
                    "citations": citations,
                    "created_at": _text(section_row[7], 80),
                    "updated_at": _text(section_row[8], 80),
                }
            )

        expected_roles = _expected_roles(report_json)
        missing_roles = [role for role in expected_roles if role_status.get(role) != "completed"]
        degraded_categories = _string_list(report_json.get("degraded_categories"))
        missing_sections = _string_list(report_json.get("missing_sections"))
        data_gaps = list(dict.fromkeys(degraded_categories + missing_sections + missing_roles))
        category_scores = report_json.get("category_scores")
        if not isinstance(category_scores, Mapping):
            category_scores = {}
        public_scores = {
            _text(key, 100): number
            for key, value in category_scores.items()
            if _text(key, 100) and (number := _number(value)) is not None
        }
        coverage = _number(report_json.get("evidence_coverage"))
        if coverage is None:
            coverage = _number(row[5])
        bear_section = next((item for item in sections if item["role_key"] == "bear_researcher"), None)
        counter_evidence = _text((bear_section or {}).get("content_markdown"), 2000)
        if not counter_evidence:
            counter_evidence = _text(risk_summary.get("conservative"), 2000)

        symbol = _text(row[21] or row[28], 100)
        display_name = _text(row[22] or row[29] or symbol or "市场", 300)
        instrument_key = None
        if row[19] is not None:
            instrument_key = stable_instrument_key(
                canonical_symbol=_text(row[21], 100),
                asset_type=_text(row[23], 80),
                market=_text(row[24], 80),
                exchange=_text(row[25], 80),
                country_code=_text(row[27], 20),
            )
        target = {
            "scope_type": _text(row[18], 50),
            "instrument_id": int(row[19]) if row[19] is not None else None,
            "instrument_key": instrument_key,
            "universe_id": int(row[20]) if row[20] is not None else None,
            "canonical_symbol": symbol,
            "display_name": display_name,
            "asset_type": _text(row[23] or row[30], 80),
            "market": _text(row[24] or row[31], 80),
            "exchange": _text(row[25], 80),
            "currency": _text(row[26], 30),
        }
        return {
            "view_version": FINANCIAL_REPORT_VIEW_VERSION,
            "report_id": int(row[0]),
            "research_run_id": _text(row[1], 160),
            "report_version": int(row[2]),
            "report_status": _text(row[3], 80),
            "output_classification": "research_opinion",
            "recommendation": _text(row[4] or "insufficient_evidence", 100),
            "confidence": _number(row[5]),
            "title": _text(row[6], 2000),
            "executive_summary": _text(row[7], 20000),
            "report_markdown": _text(row[8], 200000),
            "risk_summary": risk_summary,
            "suitability_notice": _text(row[11], 10000),
            "disclaimer": _text(row[12], 10000),
            "observed_at": _text(row[13], 100),
            "fetched_at": _text(row[14], 100),
            "verified_at": _text(row[15], 100),
            "created_at": _text(row[16], 100),
            "updated_at": _text(row[17], 100),
            "scope_type": _text(row[18], 80),
            "instrument_id": row[19],
            "universe_id": row[20],
            "target": target,
            "overview": {
                "as_of": _text(report_json.get("as_of") or row[13], 80),
                "market_status": _text(report_json.get("market_status"), 80),
                "evidence_coverage": coverage,
                "component_contribution_coverage": _number(report_json.get("component_contribution_coverage")),
                "data_latency_seconds": _number(report_json.get("data_latency_seconds")),
                "constituent_as_of": _text(report_json.get("constituent_as_of"), 80),
                "category_scores": public_scores,
                "data_gaps": data_gaps,
                "core_reason": _text(row[7], 4000),
                "counter_evidence": counter_evidence,
                "citation_count": len(citation_identities),
            },
            "section_groups": [dict(group) for group in GROUPS],
            "sections": sections,
            "section_count": len(sections),
            "missing_roles": missing_roles,
        }


__all__ = [
    "FINANCIAL_REPORT_VIEW_VERSION",
    "FinancialReportView",
    "GROUPS",
    "INDEX_EXPECTED_ROLES",
    "ROLE_PRESENTATION",
    "STOCK_EXPECTED_ROLES",
]
