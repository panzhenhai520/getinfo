#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Evidence-closed financial answer composition.

The composer is deliberately deterministic.  It projects already adjudicated
facts and already persisted TradingAgents reports into one public answer; it
does not ask a model to repair missing values or reinterpret a saved rating.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from jsonschema import Draft202012Validator

from financial_conflict_judge import (
    FINANCIAL_CONFLICT_JUDGE_VERSION,
    FinancialConflictJudge,
)
from financial_full_research import format_full_research_answer
from financial_chat_market_scope import format_market_scope_answer
from financial_realtime_query import format_realtime_query_answer
from financial_report_view import FinancialReportView
from financial_security import redact_sensitive_text, safe_public_url


FINANCIAL_ANSWER_COMPOSER_VERSION = "financial-answer-composer-v1"
FINANCIAL_ANSWER_SCHEMA_VERSION = "financial-answer-v1"
FINANCIAL_ANSWER_DISCLAIMER = (
    "模拟研究参考，非投资建议；不构成适当性判断、收益承诺或真实交易指令。"
)
ANSWER_STATUSES = ("ready", "partial", "insufficient_evidence", "pending")
VERIFIED_FACT_STATUSES = frozenset({"verified_current", "verified_historical"})
VERIFIED_CONFLICT_VERDICTS = frozenset(
    {"verified_consensus", "verified_authoritative"}
)
TERMINAL_REPORT_STATUSES = frozenset(
    {"verified", "generated_unverified", "degraded_unverified", "insufficient_evidence"}
)

_TARGET_FIELDS = (
    "instrument_id", "instrument_key", "universe_id", "universe_key",
    "canonical_symbol", "display_name", "asset_type", "market", "exchange",
    "currency", "country_code", "scope_type",
)

FINANCIAL_ANSWER_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "schema_version", "composer_version", "status", "request_time",
        "targets", "current_facts", "historical_facts", "research_reports",
        "risks_and_counter_evidence", "conflicts_and_gaps", "citations",
        "answer_markdown", "disclaimer", "boundaries",
    ],
    "properties": {
        "schema_version": {"const": FINANCIAL_ANSWER_SCHEMA_VERSION},
        "composer_version": {"const": FINANCIAL_ANSWER_COMPOSER_VERSION},
        "status": {"enum": list(ANSWER_STATUSES)},
        "request_time": {"type": "object"},
        "targets": {"type": "array", "items": {"type": "object"}},
        "current_facts": {"type": "array", "items": {"type": "object"}},
        "historical_facts": {"type": "array", "items": {"type": "object"}},
        "research_reports": {"type": "array", "items": {"type": "object"}},
        "risks_and_counter_evidence": {
            "type": "array", "items": {"type": "object"}
        },
        "conflicts_and_gaps": {"type": "array", "items": {"type": "object"}},
        "citations": {"type": "array", "items": {"type": "object"}},
        "answer_markdown": {"type": "string"},
        "disclaimer": {"const": FINANCIAL_ANSWER_DISCLAIMER},
        "boundaries": {"type": "object"},
    },
    "additionalProperties": False,
}
_VALIDATOR = Draft202012Validator(FINANCIAL_ANSWER_SCHEMA)


def _text(value: object, maximum: int = 4000) -> str:
    return redact_sensitive_text(value, maximum=maximum).strip()


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _json_object(value: object) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _json_array(value: object) -> list:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _parse_utc(value: object) -> datetime | None:
    raw = _text(value, 100)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _safe_url(value: object, *, local: bool = True) -> str:
    prefixes = ("/api/financial/",) if local else ()
    return safe_public_url(value, local_prefixes=prefixes)


def _safe_target(value: object) -> dict:
    if not isinstance(value, Mapping):
        return {}
    result = {}
    for key in _TARGET_FIELDS:
        item = value.get(key)
        if item in (None, ""):
            continue
        if key in {"instrument_id", "universe_id"}:
            try:
                result[key] = int(item)
            except (TypeError, ValueError):
                continue
        else:
            result[key] = _text(item, 300)
    return result


def _safe_value(value: object) -> object:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        number = _finite(value)
        return number
    if isinstance(value, str):
        return _text(value, 2000)
    if not isinstance(value, Mapping):
        return None
    result = {}
    for key in ("kind", "number", "min", "max", "boolean", "text"):
        if key not in value:
            continue
        item = value[key]
        if key in {"number", "min", "max"}:
            item = _finite(item)
            if item is None:
                continue
        elif key == "boolean":
            if not isinstance(item, bool):
                continue
        else:
            item = _text(item, 2000)
        result[key] = item
    return result


def _target_label(target: Mapping[str, object]) -> str:
    name = _text(target.get("display_name") or target.get("universe_key") or "金融标的", 300)
    symbol = _text(target.get("canonical_symbol"), 100)
    return f"{name}（{symbol}）" if symbol else name


def _value_text(value: object) -> str:
    if isinstance(value, Mapping):
        kind = _text(value.get("kind"), 40)
        if kind == "scalar" or "number" in value:
            number = _finite(value.get("number"))
            return f"{number:g}" if number is not None else ""
        if kind == "range" or "min" in value or "max" in value:
            low, high = _finite(value.get("min")), _finite(value.get("max"))
            if low is not None and high is not None:
                return f"{low:g}–{high:g}"
        if "boolean" in value and isinstance(value.get("boolean"), bool):
            return "是" if value["boolean"] else "否"
        return _text(value.get("text"), 1000)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = _finite(value)
        return f"{number:g}" if number is not None else ""
    return _text(value, 1000)


def _citation(item: object, *, fallback_index: int = 0) -> dict | None:
    if not isinstance(item, Mapping):
        return None
    snapshot_id = None
    evidence_id = None
    article_id = None
    for key, name in (
        ("snapshot_id", "snapshot_id"),
        ("evidence_id", "evidence_id"),
        ("article_id", "article_id"),
    ):
        try:
            number = int(item.get(key))
        except (TypeError, ValueError):
            number = None
        if name == "snapshot_id":
            snapshot_id = number
        elif name == "evidence_id":
            evidence_id = number
        else:
            article_id = number
    url = _safe_url(item.get("url") or item.get("source_url"))
    if not url and snapshot_id is not None:
        url = f"/api/financial/snapshots/{snapshot_id}"
    if not url:
        return None
    return {
        "citation_id": _text(item.get("citation_id") or f"source-{fallback_index}", 120),
        "label": _text(
            item.get("label") or item.get("title") or item.get("provider")
            or item.get("provider_id") or "证据来源",
            300,
        ),
        "url": url,
        "evidence_id": evidence_id,
        "snapshot_id": snapshot_id,
        "article_id": article_id,
        "provider": _text(item.get("provider") or item.get("provider_id"), 120),
        "observed_at": _text(item.get("observed_at"), 100),
        "fetched_at": _text(item.get("fetched_at"), 100),
    }


def _gap(code: str, message: str, *, severity: str = "warning", target=None, details=()) -> dict:
    return {
        "code": _text(code, 120),
        "message": _text(message, 2000),
        "severity": severity if severity in {"info", "warning", "blocking"} else "warning",
        "target": _safe_target(target),
        "details": [_text(item, 1000) for item in list(details)[:30] if _text(item, 1000)],
    }


class FinancialAnswerComposer:
    """Build the public answer without changing upstream facts or opinions."""

    def compose(
        self,
        *,
        server_time_context: Mapping[str, object],
        targets: Sequence[Mapping[str, object]] = (),
        facts: Sequence[Mapping[str, object]] = (),
        reports: Sequence[Mapping[str, object]] = (),
        gaps: Sequence[Mapping[str, object]] = (),
        scope_summary: str = "",
        pending: bool = False,
    ) -> dict:
        server_now = _parse_utc(server_time_context.get("server_now_utc"))
        if server_now is None:
            raise ValueError("absolute application server time is required")
        requested_as_of = _parse_utc(
            server_time_context.get("requested_as_of")
            or server_time_context.get("resolved_as_of")
            or server_time_context.get("server_now_utc")
        )
        if requested_as_of is None:
            raise ValueError("absolute requested_as_of is required")
        request_time = {
            "server_now_utc": _utc_text(server_now),
            "requested_as_of": _utc_text(requested_as_of),
            "server_timezone": _text(server_time_context.get("server_timezone"), 100),
            "user_timezone": _text(server_time_context.get("user_timezone"), 100),
            "clock_source": "application_server",
            "resolved_time_expression": _text(
                server_time_context.get("resolved_time_expression"), 200
            ),
        }
        safe_targets = []
        target_seen = set()
        for item in targets:
            target = _safe_target(item)
            if not target:
                continue
            identity = json.dumps(target, ensure_ascii=False, sort_keys=True)
            if identity not in target_seen:
                target_seen.add(identity)
                safe_targets.append(target)

        current_facts, historical_facts = [], []
        all_citations = []
        safe_gaps = [
            _gap(
                str(item.get("code") or "evidence_gap"),
                str(item.get("message") or "存在未满足的金融证据条件"),
                severity=str(item.get("severity") or "warning"),
                target=item.get("target"),
                details=item.get("details") or (),
            )
            for item in gaps
            if isinstance(item, Mapping)
        ]
        fact_seen = set()
        for index, raw in enumerate(facts, start=1):
            if not isinstance(raw, Mapping):
                continue
            verification_status = _text(raw.get("verification_status"), 80)
            conflict_verdict = _text(raw.get("conflict_verdict"), 80)
            if verification_status not in VERIFIED_FACT_STATUSES:
                safe_gaps.append(
                    _gap(
                        "fact_not_verified",
                        f"指标 {_text(raw.get('metric') or 'unspecified_metric', 120)} 的主张未通过事实门禁"
                        f"（status={verification_status or 'unknown'}）。",
                        severity="blocking",
                        target=raw.get("target"),
                    )
                )
                continue
            if conflict_verdict not in VERIFIED_CONFLICT_VERDICTS:
                safe_gaps.append(
                    _gap(
                        "fact_conflict_not_resolved",
                        f"主张的多源裁决为 {conflict_verdict or 'missing'}，未进入事实集合。",
                        severity="blocking",
                        target=raw.get("target"),
                    )
                )
                continue
            citations = []
            for citation_index, item in enumerate(raw.get("citations") or (), start=1):
                safe = _citation(item, fallback_index=index * 100 + citation_index)
                if safe is not None:
                    citations.append(safe)
            if not citations:
                safe_gaps.append(
                    _gap(
                        "fact_missing_clickable_citation",
                        "主张虽有状态，但缺少可点击证据，已从答案事实中移除。",
                        severity="blocking",
                        target=raw.get("target"),
                    )
                )
                continue
            target = _safe_target(raw.get("target"))
            value = _safe_value(raw.get("value"))
            fact = {
                "claim_id": int(raw["claim_id"]) if str(raw.get("claim_id") or "").isdigit() else None,
                "metric": _text(raw.get("metric") or "unspecified_metric", 120),
                "value": value,
                "unit": _text(raw.get("unit"), 80),
                "currency": _text(raw.get("currency"), 40),
                "statement": _text(raw.get("statement"), 2000),
                "as_of": _text(raw.get("as_of") or raw.get("observed_at"), 100),
                "verification_status": verification_status,
                "conflict_verdict": conflict_verdict,
                "target": target,
                "citations": citations,
            }
            identity = json.dumps(
                {
                    "claim_id": fact["claim_id"], "metric": fact["metric"],
                    "value": fact["value"], "target": target, "as_of": fact["as_of"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            if identity in fact_seen:
                continue
            fact_seen.add(identity)
            (current_facts if verification_status == "verified_current" else historical_facts).append(fact)
            all_citations.extend(citations)

        newest_current = max(
            (_parse_utc(item.get("as_of")) for item in current_facts),
            default=None,
            key=lambda value: value or datetime.min.replace(tzinfo=timezone.utc),
        )
        safe_reports = []
        risks = []
        for raw in reports:
            if not isinstance(raw, Mapping):
                continue
            try:
                report_id = int(raw.get("report_id"))
            except (TypeError, ValueError):
                report_id = 0
            report_status = _text(raw.get("report_status"), 80)
            if (
                report_id < 1
                or report_status.casefold() not in TERMINAL_REPORT_STATUSES
                or not bool(raw.get("saved"))
            ):
                safe_gaps.append(
                    _gap(
                        "report_not_saved_or_terminal",
                        "TradingAgents 产物尚未形成可读取的终态报告。",
                        severity="blocking",
                        target=raw.get("target"),
                    )
                )
                continue
            report_as_of = _text(raw.get("as_of") or raw.get("observed_at"), 100)
            parsed_report_as_of = _parse_utc(report_as_of)
            freshness = "compatible"
            if newest_current and parsed_report_as_of and newest_current > parsed_report_as_of:
                freshness = "older_than_current_facts"
                safe_gaps.append(
                    _gap(
                        "report_older_than_current_fact",
                        f"终极报告时点 {report_as_of} 早于最新事实 {_utc_text(newest_current)}；当前事实使用新快照，研究结论保留原报告时点。",
                        severity="warning",
                        target=raw.get("target"),
                    )
                )
            report = {
                "report_id": report_id,
                "research_run_id": _text(raw.get("research_run_id"), 160),
                "report_status": report_status,
                "output_classification": "research_opinion",
                "recommendation": _text(raw.get("recommendation") or "insufficient_evidence", 200),
                "confidence": _finite(raw.get("confidence")),
                "as_of": report_as_of,
                "observed_at": _text(raw.get("observed_at"), 100),
                "verified_at": _text(raw.get("verified_at"), 100),
                "title": _text(raw.get("title") or "TradingAgents 终极报告", 500),
                "executive_summary": _text(raw.get("executive_summary"), 4000),
                "report_url": f"/api/financial/reports/{report_id}",
                "target": _safe_target(raw.get("target")),
                "freshness": freshness,
            }
            safe_reports.append(report)
            all_citations.append(
                {
                    "citation_id": f"report-{report_id}",
                    "label": report["title"],
                    "url": report["report_url"],
                    "evidence_id": None,
                    "snapshot_id": None,
                    "article_id": None,
                    "provider": "TradingAgents",
                    "observed_at": report["observed_at"],
                    "fetched_at": "",
                }
            )
            risk_summary = raw.get("risk_summary")
            if isinstance(risk_summary, Mapping):
                risk_text = _text(
                    risk_summary.get("summary") or risk_summary.get("assessment")
                    or risk_summary.get("risk_level"),
                    3000,
                )
            else:
                risk_text = _text(risk_summary, 3000)
            counter = _text(raw.get("counter_evidence"), 3000)
            if risk_text or counter:
                risks.append(
                    {
                        "report_id": report_id,
                        "target": report["target"],
                        "risk_summary": risk_text,
                        "counter_evidence": counter,
                    }
                )

        citations = []
        citation_seen = set()
        for item in all_citations:
            identity = (item["url"], item.get("evidence_id"), item.get("snapshot_id"))
            if identity in citation_seen:
                continue
            citation_seen.add(identity)
            citations.append(item)

        if pending:
            status = "pending"
        elif not current_facts and not historical_facts and not safe_reports and not _text(scope_summary):
            status = "insufficient_evidence"
        elif safe_gaps:
            status = "partial"
        else:
            status = "ready"
        result = {
            "schema_version": FINANCIAL_ANSWER_SCHEMA_VERSION,
            "composer_version": FINANCIAL_ANSWER_COMPOSER_VERSION,
            "status": status,
            "request_time": request_time,
            "targets": safe_targets,
            "current_facts": current_facts,
            "historical_facts": historical_facts,
            "research_reports": safe_reports,
            "risks_and_counter_evidence": risks,
            "conflicts_and_gaps": safe_gaps,
            "citations": citations,
            "answer_markdown": "",
            "disclaimer": FINANCIAL_ANSWER_DISCLAIMER,
            "boundaries": {
                "model_calls": 0,
                "network_calls": 0,
                "missing_values_fabricated": False,
                "report_recommendation_rewritten": False,
                "report_confidence_rewritten": False,
                "report_as_of_rewritten": False,
                "unverified_fact_output_as_current": False,
                "server_time_authoritative": True,
                "real_order_execution": False,
            },
        }
        result["answer_markdown"] = self._render(result, scope_summary=_text(scope_summary, 8000))
        _VALIDATOR.validate(result)
        return result

    @staticmethod
    def _render(answer: Mapping[str, object], *, scope_summary: str) -> str:
        targets = answer["targets"]
        target_text = "、".join(_target_label(item) for item in targets) or "已解析金融范围"
        request_time = answer["request_time"]
        blocks = [
            "## 对象与时点\n"
            f"{target_text}；服务器时点 {request_time['server_now_utc']}，"
            f"请求时点 {request_time['requested_as_of']}（{request_time.get('user_timezone') or 'UTC'}）。"
        ]
        if scope_summary:
            blocks.append("## 当前回答\n" + scope_summary)

        fact_lines = []
        for fact in answer["current_facts"]:
            value = _value_text(fact["value"])
            unit = " ".join(item for item in (fact["currency"], fact["unit"]) if item)
            statement = fact["statement"] or f"{fact['metric']}={value} {unit}".strip()
            citation = fact["citations"][0]
            fact_lines.append(
                f"- {_target_label(fact['target'])}：{statement}"
                f"（as_of={fact['as_of']}，[{citation['label']}]({citation['url']})）"
            )
        blocks.append(
            "## 已核验当前事实\n"
            + ("\n".join(fact_lines) if fact_lines else "无满足当前事实门禁且可点击追溯的数值主张。")
        )
        if answer["historical_facts"]:
            historical_lines = []
            for fact in answer["historical_facts"]:
                citation = fact["citations"][0]
                statement = fact["statement"] or (
                    f"{fact['metric']}={_value_text(fact['value'])} {fact['currency']} {fact['unit']}"
                ).strip()
                historical_lines.append(
                    f"- {_target_label(fact['target'])}：{statement}"
                    f"（历史有效，as_of={fact['as_of']}，[{citation['label']}]({citation['url']})）"
                )
            blocks.append("## 历史/非实时事实\n" + "\n".join(historical_lines))

        report_lines = []
        for report in answer["research_reports"]:
            confidence = (
                json.dumps(report["confidence"], ensure_ascii=False, allow_nan=False)
                if report["confidence"] is not None else "unknown"
            )
            age_note = "；报告早于当前事实" if report["freshness"] == "older_than_current_facts" else ""
            report_lines.append(
                f"- {_target_label(report['target'])}：评级={report['recommendation']}，"
                f"置信度={confidence}，as_of={report['as_of']}，"
                f"report_status={report['report_status']}{age_note}。"
                f"[{report['title']}]({report['report_url']})"
                + (f"\n  {report['executive_summary']}" if report["executive_summary"] else "")
            )
        blocks.append(
            "## TradingAgents 研究结论\n"
            + ("\n".join(report_lines) if report_lines else "没有已保存且可公开读取的终态研究报告。")
        )

        risk_lines = []
        for item in answer["risks_and_counter_evidence"]:
            if item["risk_summary"]:
                risk_lines.append(f"- 风险：{item['risk_summary']}")
            if item["counter_evidence"]:
                risk_lines.append(f"- 反证：{item['counter_evidence']}")
        blocks.append(
            "## 风险与反证\n"
            + ("\n".join(risk_lines) if risk_lines else "当前公开产物未提供可投影的风险或反证摘要。")
        )

        gap_lines = [
            f"- [{item['severity']}] {item['message']}"
            + (" 详情：" + "；".join(item["details"]) if item["details"] else "")
            for item in answer["conflicts_and_gaps"]
        ]
        blocks.append(
            "## 冲突与证据缺口\n"
            + ("\n".join(gap_lines) if gap_lines else "未发现阻止本次投影的已知冲突或证据缺口。")
        )
        blocks.append(FINANCIAL_ANSWER_DISCLAIMER)
        return "\n\n".join(blocks)


class FinancialAnswerComposerService:
    """Load adjudicated claims and public report projections from shared SQLite."""

    def __init__(self, database, *, composer: FinancialAnswerComposer | None = None):
        self.database = database
        self.composer = composer or FinancialAnswerComposer()

    @property
    def connection(self):
        self.database._ensure_connection()
        return self.database.connection

    def _citations(self, evidence_ids: Sequence[int]) -> list[dict]:
        ids = sorted({int(item) for item in evidence_ids if int(item) > 0})
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self.database.lock:
            rows = self.connection.execute(
                f"""
                SELECT evidence.id, evidence.snapshot_id, evidence.article_id,
                       evidence.source_url, evidence.source_title,
                       evidence.observed_at, evidence.fetched_at,
                       snapshot.source_url, profile.provider_key,
                       article.url, article.title
                FROM financial_claim_evidence evidence
                LEFT JOIN financial_data_snapshots snapshot ON snapshot.id=evidence.snapshot_id
                LEFT JOIN financial_provider_profiles profile
                  ON profile.id=COALESCE(evidence.provider_profile_id, snapshot.provider_profile_id)
                LEFT JOIN articles article ON article.id=evidence.article_id
                WHERE evidence.id IN ({placeholders})
                ORDER BY evidence.id
                """,
                ids,
            ).fetchall()
        result = []
        for row in rows:
            snapshot_id = int(row[1]) if row[1] is not None else None
            source_url = str(row[3] or row[7] or row[9] or "")
            if not _safe_url(source_url) and snapshot_id is None:
                continue
            result.append(
                {
                    "citation_id": f"claim-evidence-{int(row[0])}",
                    "evidence_id": int(row[0]),
                    "snapshot_id": snapshot_id,
                    "article_id": int(row[2]) if row[2] is not None else None,
                    "source_url": source_url,
                    "title": str(row[4] or row[10] or row[8] or "金融事实证据"),
                    "provider_id": str(row[8] or ""),
                    "observed_at": str(row[5] or ""),
                    "fetched_at": str(row[6] or ""),
                }
            )
        return result

    def load_verified_facts(self, report_ids: Sequence[int]) -> list[dict]:
        ids = sorted({int(item) for item in report_ids if int(item) > 0})
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self.database.lock:
            rows = self.connection.execute(
                f"""
                WITH latest_conflict AS (
                    SELECT verdict.claim_id, verdict.verdict, verdict.rationale,
                           verdict.selected_evidence_ids_json
                    FROM financial_verdicts verdict
                    JOIN (
                        SELECT claim_id, MAX(adjudication_version) AS version
                        FROM financial_verdicts
                        WHERE adjudicator=?
                        GROUP BY claim_id
                    ) latest ON latest.claim_id=verdict.claim_id
                            AND latest.version=verdict.adjudication_version
                    WHERE verdict.adjudicator=?
                )
                SELECT claim.id, claim.final_report_id, claim.statement,
                       claim.normalized_value_json, claim.unit, claim.currency,
                       claim.observed_at, claim.effective_at,
                       claim.verification_status, conflict.verdict,
                       conflict.rationale, conflict.selected_evidence_ids_json,
                       instrument.id, instrument.canonical_symbol,
                       instrument.display_name, instrument.asset_type,
                       instrument.market, instrument.exchange,
                       instrument.currency, instrument.country_code
                FROM financial_claims claim
                JOIN latest_conflict conflict ON conflict.claim_id=claim.id
                JOIN financial_research_runs run ON run.id=claim.research_run_id
                LEFT JOIN financial_instruments instrument ON instrument.id=run.instrument_id
                WHERE claim.final_report_id IN ({placeholders})
                  AND claim.claim_type='fact'
                  AND claim.verification_status IN ('verified_current','verified_historical')
                  AND conflict.verdict IN ('verified_consensus','verified_authoritative')
                ORDER BY claim.id
                """,
                (FINANCIAL_CONFLICT_JUDGE_VERSION, FINANCIAL_CONFLICT_JUDGE_VERSION, *ids),
            ).fetchall()
        result = []
        for row in rows:
            normalized = _json_object(row[3])
            rationale = _json_object(row[10])
            selected = rationale.get("selected_evidence_ids") or _json_array(row[11])
            citations = self._citations(selected)
            target = {
                "instrument_id": int(row[12]) if row[12] is not None else None,
                "canonical_symbol": str(row[13] or ""),
                "display_name": str(row[14] or ""),
                "asset_type": str(row[15] or ""),
                "market": str(row[16] or ""),
                "exchange": str(row[17] or ""),
                "currency": str(row[18] or ""),
                "country_code": str(row[19] or ""),
            }
            subject = normalized.get("subject")
            if isinstance(subject, Mapping):
                target.update({key: value for key, value in subject.items() if value not in (None, "")})
            decision_value = rationale.get("decision_value")
            if isinstance(decision_value, Mapping) and _finite(decision_value.get("number")) is not None:
                value = {"kind": "scalar", "number": _finite(decision_value.get("number"))}
            elif isinstance(decision_value, Mapping) and decision_value.get("value") is not None:
                value = decision_value.get("value")
            else:
                value = normalized.get("value")
            result.append(
                {
                    "claim_id": int(row[0]),
                    "metric": str(normalized.get("metric") or "unspecified_metric"),
                    "value": value,
                    "unit": str(row[4] or ""),
                    "currency": str(row[5] or ""),
                    "statement": str(row[2] or ""),
                    "as_of": str(normalized.get("as_of") or row[6] or row[7] or ""),
                    "verification_status": str(row[8]),
                    "conflict_verdict": str(row[9]),
                    "target": target,
                    "citations": citations,
                }
            )
        return result

    def load_reports(self, report_ids: Sequence[int]) -> list[dict]:
        view = FinancialReportView(self.database)
        result = []
        for report_id in sorted({int(item) for item in report_ids if int(item) > 0}):
            report = view.get(report_id)
            if report is None:
                continue
            overview = report.get("overview") or {}
            result.append(
                {
                    "report_id": int(report["report_id"]),
                    "research_run_id": str(report.get("research_run_id") or ""),
                    "report_status": str(report.get("report_status") or ""),
                    "recommendation": str(report.get("recommendation") or "insufficient_evidence"),
                    "confidence": report.get("confidence"),
                    "as_of": str(overview.get("as_of") or report.get("observed_at") or ""),
                    "observed_at": str(report.get("observed_at") or ""),
                    "verified_at": str(report.get("verified_at") or ""),
                    "title": str(report.get("title") or ""),
                    "executive_summary": str(report.get("executive_summary") or ""),
                    "risk_summary": report.get("risk_summary") or {},
                    "counter_evidence": str(overview.get("counter_evidence") or ""),
                    "target": report.get("target") or {},
                    "saved": True,
                }
            )
        return result

    def compose_reports(
        self,
        report_ids: Sequence[int],
        *,
        server_time_context: Mapping[str, object],
        targets: Sequence[Mapping[str, object]] = (),
        extra_facts: Sequence[Mapping[str, object]] = (),
        gaps: Sequence[Mapping[str, object]] = (),
        scope_summary: str = "",
    ) -> dict:
        reports = self.load_reports(report_ids)
        facts = [*self.load_verified_facts(report_ids), *list(extra_facts)]
        report_targets = [item.get("target") or {} for item in reports]
        return self.composer.compose(
            server_time_context=server_time_context,
            targets=[*targets, *report_targets],
            facts=facts,
            reports=reports,
            gaps=gaps,
            scope_summary=scope_summary,
        )

    def public_snapshot(self, snapshot_id: int) -> dict | None:
        with self.database.lock:
            row = self.connection.execute(
                """
                SELECT snapshot.id, snapshot.observed_at, snapshot.fetched_at,
                       snapshot.market_status, snapshot.currency, snapshot.timezone,
                       snapshot.quality_status, snapshot.payload_json,
                       snapshot.payload_sha256, snapshot.source_url,
                       profile.provider_key, profile.display_name,
                       instrument.canonical_symbol, instrument.display_name,
                       instrument.asset_type, instrument.market, instrument.exchange,
                       instrument.country_code
                FROM financial_data_snapshots snapshot
                JOIN financial_provider_profiles profile ON profile.id=snapshot.provider_profile_id
                LEFT JOIN financial_instruments instrument ON instrument.id=snapshot.instrument_id
                WHERE snapshot.id=?
                """,
                (int(snapshot_id),),
            ).fetchone()
        if row is None:
            return None
        payload_text = str(row[7] or "")
        if hashlib.sha256(payload_text.encode("utf-8")).hexdigest() != str(row[8] or ""):
            return None
        payload = _json_object(payload_text)
        normalized = payload.get("normalized_payload")
        normalized = normalized if isinstance(normalized, Mapping) else {}
        values = {}
        for key in (
            "last_price", "price", "close", "previous_close", "pre_close",
            "change", "change_percent", "pct_change", "open", "high", "low",
            "volume", "turnover", "index_level", "nav",
        ):
            raw = normalized.get(key, payload.get(key))
            number = _finite(raw)
            if number is not None:
                values[key] = number
        if not values and not isinstance(payload.get("value"), Mapping):
            number = _finite(payload.get("value"))
            if number is not None:
                values["value"] = number
        return {
            "snapshot_id": int(row[0]),
            "observed_at": str(row[1] or ""),
            "fetched_at": str(row[2] or ""),
            "market_status": str(row[3] or ""),
            "currency": str(row[4] or ""),
            "timezone": str(row[5] or ""),
            "quality_status": str(row[6] or ""),
            "payload_sha256": str(row[8] or ""),
            "source_url": _safe_url(row[9]),
            "provider": {
                "provider_id": str(row[10] or ""),
                "display_name": str(row[11] or ""),
            },
            "target": _safe_target(
                {
                    "canonical_symbol": row[12], "display_name": row[13],
                    "asset_type": row[14], "market": row[15], "exchange": row[16],
                    "country_code": row[17], "currency": row[4],
                }
            ),
            "values": values,
        }


def _server_context(value: Mapping[str, object] | None, fallback: object) -> dict:
    source = dict(value or {})
    fallback_text = _text(fallback, 100)
    source.setdefault("server_now_utc", fallback_text)
    source.setdefault("requested_as_of", fallback_text)
    source.setdefault("server_timezone", "Asia/Hong_Kong")
    source.setdefault("user_timezone", "Asia/Hong_Kong")
    return source


def compose_realtime_query_answer(
    query: Mapping[str, object],
    server_time_context: Mapping[str, object] | None = None,
) -> dict:
    target = _safe_target(query.get("target"))
    evidence = [item for item in query.get("evidence") or [] if isinstance(item, Mapping)]
    status = _text(query.get("status"), 80)
    facts = []
    gaps = []
    if evidence and status in {"ready", "stale"} and bool(query.get("numeric_claims_allowed")):
        instrument_key = _text(
            target.get("instrument_key")
            or f"{target.get('country_code') or target.get('market') or 'UNKNOWN'}:"
               f"{target.get('exchange') or target.get('market') or 'UNKNOWN'}:"
               f"{target.get('asset_type') or 'asset'}:{target.get('canonical_symbol') or 'unknown'}",
            300,
        )
        metric = str(query.get("metric") or "last_price")
        temporal_status = "verified_current" if status == "ready" else "verified_historical"
        judge_evidence = []
        for item in evidence:
            try:
                snapshot_id = int(item["snapshot_id"])
            except (KeyError, TypeError, ValueError):
                continue
            judge_evidence.append(
                {
                    "evidence_id": snapshot_id,
                    "snapshot_id": snapshot_id,
                    "evidence_type": "structured_snapshot",
                    "provider_id": str(item.get("provider_id") or ""),
                    "instrument_key": instrument_key,
                    "metric": metric,
                    "value": {"kind": "scalar", "number": item.get("price")},
                    "unit": "",
                    "currency": str(item.get("currency") or target.get("currency") or ""),
                    "adjustment": "raw",
                    "observed_at": str(item.get("observed_at") or ""),
                    "fetched_at": str(item.get("fetched_at") or ""),
                    "source_url": str(item.get("source_url") or ""),
                    "temporal_status": temporal_status,
                    "availability_status": "available",
                    "integrity_valid": True,
                }
            )
        claim_value = evidence[0].get("price")
        decision = FinancialConflictJudge().judge(
            {
                "claim_key": f"chat:{instrument_key}:{metric}:{query.get('requested_at_utc') or ''}",
                "claim_type": "fact",
                "subject": instrument_key,
                "metric": metric,
                "value": {"kind": "scalar", "number": claim_value},
                "unit": "",
                "currency": str(evidence[0].get("currency") or target.get("currency") or ""),
                "adjustment": "raw",
                "period": {},
            },
            judge_evidence,
        )
        if decision["verdict"] in VERIFIED_CONFLICT_VERDICTS:
            selected_id = next(iter(decision.get("selected_evidence_ids") or []), None)
            item = next(
                (value for value in evidence if int(value.get("snapshot_id") or 0) == selected_id),
                evidence[0],
            )
            decision_number = _finite((decision.get("decision_value") or {}).get("number"))
            fact_price = decision_number if decision_number is not None else item.get("price")
            snapshot_id = int(item["snapshot_id"])
            facts.append(
                {
                    "metric": metric,
                    "value": {"kind": "scalar", "number": fact_price},
                    "unit": "",
                    "currency": str(item.get("currency") or target.get("currency") or ""),
                    "statement": (
                        f"{_target_label(target)}{'当前' if status == 'ready' else '最近一次（非实时）'}"
                        f"价格为 {_value_text(fact_price)} {item.get('currency') or ''}；"
                        f"observed_at={item.get('observed_at') or ''}，"
                        f"fetched_at={item.get('fetched_at') or ''}，"
                        f"market_status={item.get('market_status') or 'unknown'}，"
                        f"来源={item.get('provider_display_name') or item.get('provider_id') or ''}，"
                        f"snapshot #{snapshot_id}。"
                        + ("该快照已过实时阈值，不能称为实时行情。" if status == "stale" else "")
                    ),
                    "as_of": str(item.get("observed_at") or ""),
                    "verification_status": temporal_status,
                    "conflict_verdict": str(decision["verdict"]),
                    "target": target,
                    "citations": [
                        {
                            "snapshot_id": int(value["snapshot_id"]),
                            "provider_id": value.get("provider_id"),
                            "title": value.get("provider_display_name") or value.get("provider_id"),
                            "source_url": value.get("source_url"),
                            "observed_at": value.get("observed_at"),
                            "fetched_at": value.get("fetched_at"),
                        }
                        for value in evidence
                        if int(value.get("snapshot_id") or 0) in set(decision.get("selected_evidence_ids") or [])
                    ],
                }
            )
        else:
            details = [
                f"provider={item.get('provider_id') or 'unknown'}，snapshot #{item.get('snapshot_id')}，"
                f"observed_at={item.get('observed_at') or ''}，fetched_at={item.get('fetched_at') or ''}，"
                f"market_status={item.get('market_status') or 'unknown'}，"
                f"来源={item.get('provider_display_name') or item.get('provider_id') or 'unknown'}"
                for item in evidence
            ]
            gaps.append(
                _gap(
                    "realtime_fact_not_multisource_verified",
                    f"{_target_label(target)}只有未达到共识/官方门槛的快照，"
                    "本轮不把其中数值输出为当前事实。",
                    severity="blocking",
                    target=target,
                    details=[f"conflict_verdict={decision['verdict']}", *details],
                )
            )
    elif status == "conflict":
        details = [
            f"{item.get('provider_id')}={_value_text(item.get('price'))} {item.get('currency') or ''}，"
            f"snapshot #{item.get('snapshot_id')}，observed_at={item.get('observed_at')}，"
            f"fetched_at={item.get('fetched_at')}，market_status={item.get('market_status') or 'unknown'}，"
            f"来源={item.get('provider_display_name') or item.get('provider_id')}"
            for item in evidence
        ]
        gaps.append(
            _gap(
                "realtime_provider_conflict",
                f"{_target_label(target)}的当前来源存在实质冲突；本轮不选择单一实时价格。",
                severity="blocking",
                target=target,
                details=details,
            )
        )
    elif not evidence:
        error_code = str((query.get("refresh") or {}).get("error_code") or "no_data")
        gaps.append(
            _gap(
                "realtime_snapshot_unavailable",
                f"{_target_label(target)}暂时没有可核验的价格快照；本轮不会由通用模型补造行情数字。",
                severity="blocking",
                target=target,
                details=[f"error_code={error_code}"],
            )
        )
    fallback = query.get("completed_at_utc") or query.get("requested_at_utc")
    if facts:
        summary = "实时快照已通过时效与多源/官方来源门禁。"
    elif status == "conflict":
        summary = format_realtime_query_answer(query)
    elif evidence:
        summary = (
            f"{_target_label(target)}已取得 {len(evidence)} 个可追溯快照，"
            "但未达到当前事实发布门槛；本轮不输出价格。"
        )
    else:
        summary = format_realtime_query_answer(query)
    return FinancialAnswerComposer().compose(
        server_time_context=_server_context(server_time_context, fallback),
        targets=[target],
        facts=facts,
        gaps=gaps,
        scope_summary=summary,
    )


def compose_market_scope_answer(
    scope: Mapping[str, object],
    server_time_context: Mapping[str, object] | None = None,
) -> dict:
    universe = dict(scope.get("universe") or {})
    report = dict(scope.get("report") or {})
    gaps = []
    if not bool(scope.get("answer_allowed")):
        gaps.append(_gap("market_snapshot_report_not_ready", "市场快照报告尚未满足时效门禁。", severity="blocking", target=universe))
    else:
        missing = [*list(report.get("missing_symbols") or []), *list(report.get("missing_market_metrics") or [])]
        if missing:
            gaps.append(_gap("partial_market_coverage", "市场概览存在明确的数据覆盖缺口。", target=universe, details=missing))
    refresh = scope.get("refresh") or {}
    fallback = refresh.get("server_now_utc") or report.get("requested_at_utc")
    return FinancialAnswerComposer().compose(
        server_time_context=_server_context(server_time_context, fallback),
        targets=[universe],
        gaps=gaps,
        scope_summary=format_market_scope_answer(scope),
        pending=not bool(scope.get("answer_allowed")),
    )


def compose_full_research_answer(
    route: Mapping[str, object],
    server_time_context: Mapping[str, object] | None = None,
    *,
    database=None,
) -> dict:
    route_reports = [item for item in route.get("reports") or [] if isinstance(item, Mapping)]
    targets = [dict(item.get("target") or {}) for item in route_reports]
    reports = [
        {
            **dict(item),
            "as_of": item.get("observed_at"),
            "saved": True,
        }
        for item in route_reports
    ]
    report_ids = [int(item["report_id"]) for item in route_reports if str(item.get("report_id") or "").isdigit()]
    facts = []
    if database is not None and report_ids:
        try:
            service = FinancialAnswerComposerService(database)
            persisted_reports = service.load_reports(report_ids)
            persisted_by_id = {item["report_id"]: item for item in persisted_reports}
            reports = [persisted_by_id.get(int(item["report_id"]), item) for item in reports]
            facts = service.load_verified_facts(report_ids)
        except Exception:
            facts = []
    status = str(route.get("status") or "")
    gaps = []
    pending = status in {"queued", "running", "mixed"}
    if pending:
        gaps.append(_gap("tradingagents_report_pending", "TradingAgents 多角色研究仍在现有 worker 中处理。", severity="info"))
    elif status != "cache_hit":
        gaps.append(_gap("tradingagents_report_unavailable", "TradingAgents 完整研究当前不可用，且不会回退为无来源观点。", severity="blocking"))
    fallback = route.get("completed_at_utc") or route.get("requested_at_utc")
    return FinancialAnswerComposer().compose(
        server_time_context=_server_context(server_time_context, fallback),
        targets=targets,
        facts=facts,
        reports=reports,
        gaps=gaps,
        scope_summary=format_full_research_answer(route),
        pending=pending,
    )


def format_composed_realtime_answer(query, server_time_context=None) -> str:
    return compose_realtime_query_answer(query, server_time_context)["answer_markdown"]


def format_composed_market_answer(scope, server_time_context=None) -> str:
    return compose_market_scope_answer(scope, server_time_context)["answer_markdown"]


def format_composed_research_answer(route, server_time_context=None, *, database=None) -> str:
    return compose_full_research_answer(route, server_time_context, database=database)["answer_markdown"]


__all__ = [
    "ANSWER_STATUSES",
    "FINANCIAL_ANSWER_COMPOSER_VERSION",
    "FINANCIAL_ANSWER_DISCLAIMER",
    "FINANCIAL_ANSWER_SCHEMA",
    "FINANCIAL_ANSWER_SCHEMA_VERSION",
    "FinancialAnswerComposer",
    "FinancialAnswerComposerService",
    "compose_full_research_answer",
    "compose_market_scope_answer",
    "compose_realtime_query_answer",
    "format_composed_market_answer",
    "format_composed_realtime_answer",
    "format_composed_research_answer",
]
