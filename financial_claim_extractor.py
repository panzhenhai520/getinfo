#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Evidence-bound atomic claim extraction for financial reports and answers.

Deterministic templates own structured and common numeric facts.  The existing
SharedLLMBroker may only fill uncovered spans; its output is accepted only when
the quoted source text and every numeric value are present in the input.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from jsonschema import Draft202012Validator

from financial_instruments import stable_instrument_key


FINANCIAL_CLAIM_EXTRACTION_VERSION = "financial-claim-extraction-v1"
FINANCIAL_CLAIM_EXTRACTOR_VERSION = "financial-claim-extractor-v1"
CLAIM_TYPES = ("fact", "opinion", "conditional_prediction")
FACT_VERIFICATION_STATUS = "pending"
NON_FACT_STATUS = {
    "opinion": "opinion_not_fact",
    "conditional_prediction": "prediction_not_fact",
}
MAX_SOURCE_CHARS = 120_000
MAX_CLAIMS = 200
NUMERIC_COVERAGE_THRESHOLD = 0.95

_NUMBER = r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?"
_UNIT = (
    r"%|％|个基点|基点|bps|倍|点|港元|港币|人民币|美元|亿元|万元|万港元|"
    r"亿港元|亿美元|万股|亿股|股|元|HKD|CNY|RMB|USD"
)
_KEY_NUMBER_RE = re.compile(rf"(?P<number>{_NUMBER})\s*(?P<unit>{_UNIT})", re.I)
_RANGE_RE = re.compile(
    rf"(?P<label>股价|价格|目标价|目标价格|价格区间|估值区间|点位|指数点位)"
    rf"\s*(?:为|在|约为|介于|：|:)?\s*(?P<low>{_NUMBER})\s*"
    rf"(?:至|到|—|–|-|~|～)\s*(?P<high>{_NUMBER})\s*(?P<unit>{_UNIT})?",
    re.I,
)
_METRIC_RE = re.compile(
    rf"(?P<label>股价|现价|收盘价|开盘价|最高价|最低价|目标价|点位|指数点位|"
    rf"市值|市盈率|市净率|营收|营业收入|收入|净利润|归母净利润|每股收益|EPS|"
    rf"净资产收益率|ROE|股息率|分红|派息|基金净值|单位净值|CPI|PPI|GDP|"
    rf"失业率|利率|收益率|成交量|成交额)\s*"
    rf"(?:为|是|达到|达|报|收于|升至|降至|约为|：|:)?\s*"
    rf"(?P<number>{_NUMBER})\s*(?P<unit>{_UNIT})?",
    re.I,
)
_COMPARISON_RE = re.compile(
    rf"(?P<label>同比|环比)\s*(?P<direction>增长|上升|增加|下降|下跌|减少)?\s*"
    rf"(?P<number>{_NUMBER})\s*(?P<unit>%|％|个基点|基点|bps)",
    re.I,
)
_CHANGE_RE = re.compile(
    rf"(?P<direction>上涨|上升|增长|涨|下跌|下降|减少|跌)\s*"
    rf"(?P<number>{_NUMBER})\s*(?P<unit>%|％|点|个基点|基点|bps)",
    re.I,
)
_MEMBERSHIP_RE = re.compile(
    r"(?P<text>(?:是|仍是|仍为|成为|被纳入|获纳入|被剔除|不再是).{0,36}(?:指数|名单).{0,12}(?:成分股|成份股|成分券))"
)
_ANNOUNCEMENT_RE = re.compile(
    r"(?P<text>(?:公司|发行人|董事会|交易所|监管机构|央行|政府)?\s*"
    r"(?:公告|通告|声明|文件)?\s*(?:宣布|披露|发布|确认|批准|否认|未宣布|尚未宣布|尚未披露|没有宣布)"
    r"[^。！？!?\n]{1,180})"
)
_SENTENCE_RE = re.compile(r"[^。！？!?\n]+[。！？!?]?|[^。！？!?\n]+$")
_PREDICTION_RE = re.compile(r"如果|若|预计|预期|可能|或将|有望|目标价|情景下|假设", re.I)
_OPINION_RE = re.compile(
    r"建议|评级|买入|卖出|持有|增持|减持|估值(?:偏高|偏低|合理)|值得|"
    r"风险较[高低]|看多|看空|观点|倾向|认为",
    re.I,
)
_NEGATIVE_RE = re.compile(r"未|没有|无|尚未|不再|否认|下降|下跌|减少|被剔除")

METRIC_ALIASES = {
    "股价": "last_price",
    "现价": "last_price",
    "价格": "last_price",
    "收盘价": "close",
    "开盘价": "open",
    "最高价": "high",
    "最低价": "low",
    "目标价": "target_price",
    "目标价格": "target_price",
    "价格区间": "price_range",
    "估值区间": "valuation_range",
    "点位": "index_level",
    "指数点位": "index_level",
    "市值": "market_cap",
    "市盈率": "pe_ratio",
    "市净率": "pb_ratio",
    "营收": "revenue",
    "营业收入": "revenue",
    "收入": "revenue",
    "净利润": "net_income",
    "归母净利润": "net_income_attributable",
    "每股收益": "eps",
    "EPS": "eps",
    "净资产收益率": "roe",
    "ROE": "roe",
    "股息率": "dividend_yield",
    "分红": "dividend",
    "派息": "dividend",
    "基金净值": "nav",
    "单位净值": "nav",
    "CPI": "cpi",
    "PPI": "ppi",
    "GDP": "gdp",
    "失业率": "unemployment_rate",
    "利率": "interest_rate",
    "收益率": "yield",
    "成交量": "volume",
    "成交额": "turnover",
}


CLAIM_EXTRACTION_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "schema_version", "extractor_version", "status", "source", "claims",
        "coverage", "errors", "llm_used", "boundaries",
    ],
    "properties": {
        "schema_version": {"const": FINANCIAL_CLAIM_EXTRACTION_VERSION},
        "extractor_version": {"const": FINANCIAL_CLAIM_EXTRACTOR_VERSION},
        "status": {"enum": ["complete", "partial", "failed"]},
        "source": {"type": "object"},
        "claims": {
            "type": "array",
            "maxItems": MAX_CLAIMS,
            "items": {
                "type": "object",
                "required": [
                    "claim_key", "claim_type", "subject", "statement", "metric",
                    "value", "unit", "currency", "period", "as_of", "polarity",
                    "source_span", "extraction_method", "verification_status",
                    "reason_codes",
                ],
                "properties": {
                    "claim_key": {"type": "string", "minLength": 16},
                    "claim_type": {"enum": list(CLAIM_TYPES)},
                    "subject": {"type": "object"},
                    "statement": {"type": "string", "minLength": 1},
                    "metric": {"type": "string", "minLength": 1},
                    "value": {"type": "object"},
                    "unit": {"type": "string"},
                    "currency": {"type": "string"},
                    "period": {"type": "object"},
                    "as_of": {"type": "string"},
                    "polarity": {"enum": ["affirmative", "negative"]},
                    "source_span": {"type": "object"},
                    "extraction_method": {
                        "enum": ["structured_template", "deterministic_rule", "shared_llm_grounded"],
                    },
                    "verification_status": {"type": "string"},
                    "reason_codes": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
            },
        },
        "coverage": {"type": "object"},
        "errors": {"type": "array", "items": {"type": "string"}},
        "llm_used": {"type": "boolean"},
        "boundaries": {"type": "object"},
    },
    "additionalProperties": False,
}
_VALIDATOR = Draft202012Validator(CLAIM_EXTRACTION_SCHEMA)


def _finite(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _utc(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except ValueError:
        return text[:80]


def _unit_currency(unit: str, currency: str = "") -> tuple[str, str]:
    raw = unicodedata.normalize("NFKC", str(unit or "")).strip()
    upper = raw.upper()
    explicit_currency = str(currency or "").strip().upper()
    if raw in {"港元", "港币", "万港元", "亿港元"} or upper == "HKD":
        return raw or "HKD", explicit_currency or "HKD"
    if raw in {"人民币", "元", "万元", "亿元"} or upper in {"CNY", "RMB"}:
        return raw or "CNY", explicit_currency or "CNY"
    if raw in {"美元", "亿美元"} or upper == "USD":
        return raw or "USD", explicit_currency or "USD"
    if raw in {"%", "％"}:
        return "percent", explicit_currency
    if upper == "BPS" or raw in {"基点", "个基点"}:
        return "basis_point", explicit_currency
    return raw, explicit_currency


def _subject(value: Mapping[str, object] | None) -> dict:
    source = dict(value or {})
    result = {
        key: source.get(key)
        for key in (
            "instrument_id", "instrument_key", "canonical_symbol", "display_name",
            "asset_type", "market", "exchange", "country_code",
        )
        if source.get(key) not in (None, "")
    }
    if not result.get("instrument_key") and all(
        result.get(key) for key in ("canonical_symbol", "asset_type")
    ) and (result.get("exchange") or result.get("market")):
        try:
            result["instrument_key"] = stable_instrument_key(
                canonical_symbol=str(result["canonical_symbol"]),
                asset_type=str(result["asset_type"]),
                market=str(result.get("market") or ""),
                exchange=str(result.get("exchange") or ""),
                country_code=str(result.get("country_code") or ""),
            )
        except ValueError:
            pass
    return result


def _claim_type(statement: str, metric: str) -> str:
    if _PREDICTION_RE.search(statement) or metric == "target_price":
        return "conditional_prediction"
    if _OPINION_RE.search(statement):
        return "opinion"
    return "fact"


def _span(text: str, start: int, end: int, source_ref: str) -> dict:
    return {
        "kind": "text",
        "source_ref": source_ref,
        "start": int(start),
        "end": int(end),
        "text": text[start:end],
    }


def _claim_key(payload: Mapping[str, object]) -> str:
    identity = {
        "version": FINANCIAL_CLAIM_EXTRACTOR_VERSION,
        "subject": payload.get("subject"),
        "claim_type": payload.get("claim_type"),
        "metric": payload.get("metric"),
        "value": payload.get("value"),
        "unit": payload.get("unit"),
        "currency": payload.get("currency"),
        "period": payload.get("period"),
        "as_of": payload.get("as_of"),
        "source_span": payload.get("source_span"),
    }
    return "claim-" + hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()


def _build_claim(
    *,
    statement: str,
    metric: str,
    value: Mapping[str, object],
    unit: str,
    currency: str,
    subject: Mapping[str, object],
    period: Mapping[str, object],
    as_of: str,
    source_span: Mapping[str, object],
    extraction_method: str,
    claim_type: str | None = None,
    reason_codes: Sequence[str] = (),
) -> dict:
    normalized_type = claim_type or _claim_type(statement, metric)
    payload = {
        "claim_type": normalized_type,
        "subject": dict(subject),
        "statement": str(statement).strip()[:4000],
        "metric": str(metric).strip().casefold(),
        "value": dict(value),
        "unit": str(unit or ""),
        "currency": str(currency or "").upper(),
        "period": dict(period or {}),
        "as_of": _utc(as_of),
        "polarity": "negative" if _NEGATIVE_RE.search(statement) else "affirmative",
        "source_span": dict(source_span),
        "extraction_method": extraction_method,
        "verification_status": (
            FACT_VERIFICATION_STATUS
            if normalized_type == "fact"
            else NON_FACT_STATUS[normalized_type]
        ),
        "reason_codes": list(dict.fromkeys(reason_codes)),
    }
    payload["claim_key"] = _claim_key(payload)
    return payload


def _metric_for_label(label: str, *, comparison: str = "") -> str:
    base = METRIC_ALIASES.get(label, str(label or "").strip().casefold())
    if comparison:
        return f"{base}_{comparison}"
    return base


def _period(value: Mapping[str, object] | None, as_of: str) -> dict:
    result = {
        key: str(item)
        for key, item in dict(value or {}).items()
        if key in {"kind", "start", "end", "label", "comparison"} and item not in (None, "")
    }
    if not result and as_of:
        result = {"kind": "instant", "start": _utc(as_of), "end": _utc(as_of)}
    if not result:
        result = {"kind": "unspecified"}
    return result


class SharedLLMClaimExtractor:
    """Narrow structured adapter over the project's existing SharedLLMBroker."""

    RESPONSE_SCHEMA = {
        "type": "object",
        "required": ["claims"],
        "properties": {
            "claims": {
                "type": "array",
                "maxItems": 50,
                "items": {
                    "type": "object",
                    "required": [
                        "source_text", "claim_type", "metric", "value", "unit",
                        "currency", "period", "as_of",
                    ],
                    "properties": {
                        "source_text": {"type": "string", "minLength": 1, "maxLength": 1000},
                        "claim_type": {"enum": list(CLAIM_TYPES)},
                        "metric": {"type": "string", "minLength": 1, "maxLength": 100},
                        "value": {"type": "object"},
                        "unit": {"type": "string", "maxLength": 50},
                        "currency": {"type": "string", "maxLength": 10},
                        "period": {"type": "object"},
                        "as_of": {"type": "string", "maxLength": 80},
                    },
                    "additionalProperties": False,
                },
            }
        },
        "additionalProperties": False,
    }

    def __init__(self, broker):
        self.broker = broker

    def __call__(self, text: str, *, request_id: str = "") -> list[dict]:
        result = self.broker.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Extract atomic financial claims only. Quote source_text exactly from input. "
                        "Separate fact, opinion, and conditional_prediction. Never infer a value, "
                        "date, subject, or unit that is not explicit. Return JSON only."
                    ),
                },
                {"role": "user", "content": json.dumps({"text": text}, ensure_ascii=False)},
            ],
            profile="fast",
            priority="batch_reflection",
            role_key="financial_claim_extractor",
            request_id=request_id,
            response_schema=self.RESPONSE_SCHEMA,
            max_tokens=2048,
            temperature=0,
            timeout_seconds=60,
        )
        if not isinstance(result.parsed, Mapping):
            raise ValueError("claim extractor returned no JSON object")
        return [dict(item) for item in result.parsed.get("claims") or [] if isinstance(item, Mapping)]


class FinancialClaimExtractor:
    def __init__(self, *, llm_extractor=None):
        self.llm_extractor = llm_extractor

    def _structured(
        self,
        text: str,
        source_ref: str,
        subject: Mapping[str, object],
        as_of: str,
        period: Mapping[str, object],
        values: Sequence[Mapping[str, object]],
    ) -> list[dict]:
        claims = []
        for index, item in enumerate(values[:MAX_CLAIMS]):
            metric = str(item.get("metric") or "").strip()
            statement = str(item.get("statement") or "").strip()
            if not metric or not statement:
                continue
            source_span = item.get("source_span")
            if isinstance(source_span, Mapping) and source_span.get("kind") == "structured_field":
                span = {**source_span, "source_ref": str(source_span.get("source_ref") or source_ref)}
            else:
                start = text.find(statement)
                if start < 0:
                    continue
                span = _span(text, start, start + len(statement), source_ref)
            raw_value = item.get("value")
            if isinstance(raw_value, Mapping):
                value = dict(raw_value)
            elif isinstance(raw_value, bool):
                value = {"kind": "boolean", "boolean": raw_value}
            elif (number := _finite(raw_value)) is not None:
                value = {"kind": "scalar", "number": number}
            else:
                value = {"kind": "text", "text": str(raw_value or "")[:1000]}
            unit, currency = _unit_currency(
                str(item.get("unit") or ""), str(item.get("currency") or "")
            )
            claims.append(
                _build_claim(
                    statement=statement,
                    metric=metric,
                    value=value,
                    unit=unit,
                    currency=currency,
                    subject=_subject(item.get("subject") if isinstance(item.get("subject"), Mapping) else subject),
                    period=_period(item.get("period") if isinstance(item.get("period"), Mapping) else period, str(item.get("as_of") or as_of)),
                    as_of=str(item.get("as_of") or as_of),
                    source_span=span,
                    extraction_method="structured_template",
                    claim_type=str(item.get("claim_type") or "") or None,
                    reason_codes=["structured_source", f"structured_index:{index}"],
                )
            )
        return claims

    def _rules(
        self,
        text: str,
        source_ref: str,
        subject: Mapping[str, object],
        as_of: str,
        period: Mapping[str, object],
    ) -> list[dict]:
        claims = []
        occupied = []

        def add(
            match,
            metric,
            value,
            unit="",
            currency="",
            reason="numeric_template",
            claim_type=None,
        ):
            start, end = match.span()
            statement = text[start:end]
            classification_context = text[max(0, start - 24):end]
            normalized_unit, normalized_currency = _unit_currency(unit, currency)
            claims.append(
                _build_claim(
                    statement=statement,
                    metric=metric,
                    value=value,
                    unit=normalized_unit,
                    currency=normalized_currency,
                    subject=subject,
                    period=_period(period, as_of),
                    as_of=as_of,
                    source_span=_span(text, start, end, source_ref),
                    extraction_method="deterministic_rule",
                    claim_type=claim_type or _claim_type(classification_context, metric),
                    reason_codes=[reason],
                )
            )
            occupied.append((start, end))

        for match in _RANGE_RE.finditer(text):
            low, high = _finite(match.group("low")), _finite(match.group("high"))
            if low is None or high is None:
                continue
            add(
                match,
                _metric_for_label(match.group("label")),
                {"kind": "range", "min": min(low, high), "max": max(low, high)},
                match.group("unit") or "",
                reason="range_template",
            )

        for match in _METRIC_RE.finditer(text):
            if any(match.start() >= start and match.end() <= end for start, end in occupied):
                continue
            number = _finite(match.group("number"))
            if number is None:
                continue
            add(
                match,
                _metric_for_label(match.group("label")),
                {"kind": "scalar", "number": number},
                match.group("unit") or "",
            )

        for match in _COMPARISON_RE.finditer(text):
            number = _finite(match.group("number"))
            if number is None:
                continue
            direction = match.group("direction") or ""
            if direction in {"下降", "下跌", "减少"}:
                number = -abs(number)
            prefix = text[max(0, match.start() - 60):match.start()]
            prior_metrics = list(re.finditer("|".join(map(re.escape, METRIC_ALIASES)), prefix, re.I))
            base_metric = (
                _metric_for_label(prior_metrics[-1].group(0))
                if prior_metrics
                else "unspecified_metric"
            )
            add(
                match,
                f"{base_metric}_{'yoy' if match.group('label') == '同比' else 'qoq'}",
                {"kind": "scalar", "number": number},
                match.group("unit"),
                reason="comparison_template",
            )

        for match in _CHANGE_RE.finditer(text):
            if any(match.start() >= start and match.end() <= end for start, end in occupied):
                continue
            number = _finite(match.group("number"))
            if number is None:
                continue
            if match.group("direction") in {"下跌", "下降", "减少", "跌"}:
                number = -abs(number)
            add(
                match,
                "change_percent" if match.group("unit") in {"%", "％"} else "change",
                {"kind": "scalar", "number": number},
                match.group("unit"),
                reason="change_template",
            )

        for match in _MEMBERSHIP_RE.finditer(text):
            statement = match.group("text")
            negative = bool(re.search(r"剔除|不再", statement))
            add(
                match,
                "index_membership",
                {"kind": "boolean", "boolean": not negative},
                reason="index_membership_template",
                claim_type="fact",
            )

        for match in _ANNOUNCEMENT_RE.finditer(text):
            statement = match.group("text")
            if len(statement.strip()) < 4:
                continue
            add(
                match,
                "announced_event",
                {"kind": "text", "text": statement[:1000]},
                reason="announcement_template",
                claim_type="fact",
            )
        for match in _SENTENCE_RE.finditer(text):
            statement = match.group(0).strip()
            if not statement:
                continue
            start = match.start() + (len(match.group(0)) - len(match.group(0).lstrip()))
            end = start + len(statement)
            if _PREDICTION_RE.search(statement):
                claims.append(
                    _build_claim(
                        statement=statement,
                        metric="forecast_statement",
                        value={"kind": "text", "text": statement[:1000]},
                        unit="",
                        currency="",
                        subject=subject,
                        period=_period(period, as_of),
                        as_of=as_of,
                        source_span=_span(text, start, end, source_ref),
                        extraction_method="deterministic_rule",
                        claim_type="conditional_prediction",
                        reason_codes=["prediction_language"],
                    )
                )
            elif _OPINION_RE.search(statement):
                claims.append(
                    _build_claim(
                        statement=statement,
                        metric="investment_opinion",
                        value={"kind": "text", "text": statement[:1000]},
                        unit="",
                        currency="",
                        subject=subject,
                        period=_period(period, as_of),
                        as_of=as_of,
                        source_span=_span(text, start, end, source_ref),
                        extraction_method="deterministic_rule",
                        claim_type="opinion",
                        reason_codes=["opinion_language"],
                    )
                )
        return claims

    @staticmethod
    def _numbers_grounded(source_text: str, value: Mapping[str, object]) -> bool:
        expected = []
        for key in ("number", "min", "max"):
            if key in value:
                number = _finite(value.get(key))
                if number is None:
                    return False
                expected.append(number)
        if not expected:
            return True
        observed = [
            number
            for raw in re.findall(_NUMBER, source_text)
            if (number := _finite(raw)) is not None
        ]
        return all(any(abs(item - candidate) <= 1e-9 for candidate in observed) for item in expected)

    @staticmethod
    def _unit_grounded(source_text: str, unit: str, currency: str) -> bool:
        normalized_source = unicodedata.normalize("NFKC", source_text).casefold()
        raw_unit = unicodedata.normalize("NFKC", str(unit or "")).strip()
        raw_currency = str(currency or "").strip().upper()
        if raw_unit and raw_unit.casefold() not in normalized_source:
            return False
        if not raw_currency:
            return True
        _unit_value, derived_currency = _unit_currency(raw_unit)
        if derived_currency == raw_currency:
            return True
        currency_markers = {
            "HKD": ("hkd", "港元", "港币"),
            "CNY": ("cny", "rmb", "人民币", "元", "万元", "亿元"),
            "USD": ("usd", "美元"),
        }
        return any(marker.casefold() in normalized_source for marker in currency_markers.get(raw_currency, (raw_currency,)))

    def _llm(
        self,
        text: str,
        source_ref: str,
        subject: Mapping[str, object],
        as_of: str,
        period: Mapping[str, object],
        *,
        request_id: str,
    ) -> tuple[list[dict], bool, list[str]]:
        if self.llm_extractor is None:
            return [], False, []
        try:
            proposed = self.llm_extractor(text, request_id=request_id)
        except Exception:
            return [], True, ["llm_extraction_unavailable"]
        claims = []
        errors = []
        search_from = 0
        for item in proposed[:50]:
            source_text = str(item.get("source_text") or "")
            start = text.find(source_text, search_from)
            if start < 0:
                start = text.find(source_text)
            if start < 0:
                errors.append("llm_source_span_not_grounded")
                continue
            value = item.get("value")
            if not isinstance(value, Mapping) or not self._numbers_grounded(source_text, value):
                errors.append("llm_value_not_grounded")
                continue
            if not self._unit_grounded(
                source_text,
                str(item.get("unit") or ""),
                str(item.get("currency") or ""),
            ):
                errors.append("llm_unit_or_currency_not_grounded")
                continue
            claim_type = str(item.get("claim_type") or "")
            if claim_type not in CLAIM_TYPES:
                errors.append("llm_claim_type_invalid")
                continue
            unit, currency = _unit_currency(
                str(item.get("unit") or ""), str(item.get("currency") or "")
            )
            claims.append(
                _build_claim(
                    statement=source_text,
                    metric=str(item.get("metric") or "unspecified_metric"),
                    value=value,
                    unit=unit,
                    currency=currency,
                    subject=subject,
                    period=_period(period, as_of),
                    as_of=as_of,
                    source_span=_span(text, start, start + len(source_text), source_ref),
                    extraction_method="shared_llm_grounded",
                    claim_type=claim_type,
                    reason_codes=[
                        "exact_source_span_grounded",
                        "numeric_values_grounded",
                        "server_time_context_preserved",
                    ],
                )
            )
            search_from = start + len(source_text)
        return claims, True, list(dict.fromkeys(errors))

    @staticmethod
    def _dedupe(claims: Sequence[Mapping[str, object]]) -> list[dict]:
        result = []
        seen_keys = set()
        seen_semantics = set()
        order = {"structured_template": 0, "deterministic_rule": 1, "shared_llm_grounded": 2}
        for item in sorted((dict(value) for value in claims), key=lambda value: order[value["extraction_method"]]):
            semantic = _json({
                "subject": item["subject"], "claim_type": item["claim_type"],
                "metric": item["metric"], "value": item["value"], "unit": item["unit"],
                "currency": item["currency"], "period": item["period"], "as_of": item["as_of"],
            })
            if item["claim_key"] in seen_keys or semantic in seen_semantics:
                continue
            seen_keys.add(item["claim_key"])
            seen_semantics.add(semantic)
            result.append(item)
            if len(result) >= MAX_CLAIMS:
                break
        return result

    def extract(
        self,
        text: str,
        *,
        source_kind: str,
        source_ref: str,
        subject: Mapping[str, object] | None = None,
        as_of: str = "",
        period: Mapping[str, object] | None = None,
        structured_claims: Sequence[Mapping[str, object]] = (),
        request_id: str = "",
    ) -> dict:
        # Offsets are part of the audit contract, so never normalize or rewrite
        # the source buffer before measuring spans. Individual parsers may
        # normalize captured units, while span text remains byte-for-byte exact.
        normalized = str(text or "")[:MAX_SOURCE_CHARS]
        normalized_subject = _subject(subject)
        normalized_period = _period(period, as_of)
        claims = self._structured(
            normalized, source_ref, normalized_subject, as_of, normalized_period,
            structured_claims,
        )
        claims.extend(self._rules(normalized, source_ref, normalized_subject, as_of, normalized_period))
        covered_ranges = [
            (int(item["source_span"]["start"]), int(item["source_span"]["end"]))
            for item in claims
            if item["source_span"].get("kind") == "text"
        ]
        key_numbers = list(_KEY_NUMBER_RE.finditer(normalized))
        uncovered = [
            match for match in key_numbers
            if not any(match.start() >= start and match.end() <= end for start, end in covered_ranges)
        ]
        llm_used = False
        errors = []
        if uncovered or (not claims and normalized.strip()):
            llm_claims, llm_used, llm_errors = self._llm(
                normalized, source_ref, normalized_subject, as_of, normalized_period,
                request_id=request_id,
            )
            claims.extend(llm_claims)
            errors.extend(llm_errors)
        claims = self._dedupe(claims)
        covered_ranges = [
            (int(item["source_span"]["start"]), int(item["source_span"]["end"]))
            for item in claims
            if item["source_span"].get("kind") == "text"
        ]
        covered_count = sum(
            any(match.start() >= start and match.end() <= end for start, end in covered_ranges)
            for match in key_numbers
        )
        coverage_ratio = covered_count / len(key_numbers) if key_numbers else 1.0
        unresolved = [
            {
                "start": match.start(),
                "end": match.end(),
                "text": match.group(0),
            }
            for match in key_numbers
            if not any(match.start() >= start and match.end() <= end for start, end in covered_ranges)
        ]
        if not normalized.strip() and not claims:
            status = "failed"
            errors.append("empty_source")
        elif coverage_ratio >= NUMERIC_COVERAGE_THRESHOLD:
            status = "complete"
        else:
            status = "partial"
            errors.append("numeric_claim_coverage_below_threshold")
        report = {
            "schema_version": FINANCIAL_CLAIM_EXTRACTION_VERSION,
            "extractor_version": FINANCIAL_CLAIM_EXTRACTOR_VERSION,
            "status": status,
            "source": {
                "kind": str(source_kind),
                "ref": str(source_ref),
                "character_count": len(normalized),
                "sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            },
            "claims": claims,
            "coverage": {
                "key_numeric_span_count": len(key_numbers),
                "covered_key_numeric_span_count": covered_count,
                "key_numeric_coverage": round(coverage_ratio, 6),
                "threshold": NUMERIC_COVERAGE_THRESHOLD,
                "unresolved_spans": unresolved,
            },
            "errors": list(dict.fromkeys(errors)),
            "llm_used": llm_used,
            "boundaries": {
                "facts_default_to_verified": False,
                "opinions_are_facts": False,
                "predictions_are_facts": False,
                "llm_may_create_source_span": False,
                "llm_ungrounded_numbers_allowed": False,
            },
        }
        _VALIDATOR.validate(report)
        return report


class FinancialClaimService:
    """Load immutable report sources and persist extracted claims in existing SQLite."""

    def __init__(self, database, *, extractor: FinancialClaimExtractor | None = None):
        self.database = database
        self.extractor = extractor or FinancialClaimExtractor()

    @property
    def connection(self):
        self.database._ensure_connection()
        return self.database.connection

    def _report_sources(self, report_id: int):
        with self.database.lock:
            report = self.connection.execute(
                """
                SELECT report.id, report.research_run_id, report.report_version,
                       report.report_status, report.recommendation, report.confidence,
                       report.executive_summary, report.report_markdown,
                       report.observed_at, run.instrument_id,
                       instrument.canonical_symbol, instrument.display_name,
                       instrument.asset_type, instrument.market, instrument.exchange,
                       instrument.country_code
                FROM financial_final_reports report
                JOIN financial_research_runs run ON run.id=report.research_run_id
                LEFT JOIN financial_instruments instrument ON instrument.id=run.instrument_id
                WHERE report.id=?
                """,
                (int(report_id),),
            ).fetchone()
            if report is None:
                return None, []
            sections = self.connection.execute(
                """
                SELECT id, role_key, section_type, content_markdown
                FROM financial_report_sections
                WHERE research_run_id=? AND status='completed' AND TRIM(content_markdown)<>''
                ORDER BY sequence_no, id
                """,
                (str(report[1]),),
            ).fetchall()
        return report, sections

    def extract_report(self, report_id: int, *, request_id: str = "") -> dict:
        report, sections = self._report_sources(report_id)
        if report is None:
            return {
                "schema_version": FINANCIAL_CLAIM_EXTRACTION_VERSION,
                "status": "failed",
                "report_id": int(report_id),
                "errors": ["report_not_found"],
                "claims": [],
                "sources": [],
            }
        if str(report[3] or "").casefold() in {"", "draft", "failed", "cancelled"}:
            return {
                "schema_version": FINANCIAL_CLAIM_EXTRACTION_VERSION,
                "status": "failed",
                "report_id": int(report[0]),
                "report_version": int(report[2]),
                "report_status": str(report[3] or ""),
                "research_run_id": str(report[1]),
                "errors": ["report_not_terminal"],
                "claims": [],
                "sources": [],
            }
        subject = _subject({
            "instrument_id": report[9], "canonical_symbol": report[10],
            "display_name": report[11], "asset_type": report[12],
            "market": report[13], "exchange": report[14], "country_code": report[15],
        })
        as_of = str(report[8] or "")
        sources = []
        summary_text = str(report[6] or report[7] or "")
        structured = [
            {
                "statement": f"TradingAgents recommendation: {report[4] or 'insufficient_evidence'}",
                "metric": "investment_recommendation",
                "value": {"kind": "text", "text": str(report[4] or "insufficient_evidence")},
                "claim_type": "opinion",
                "source_span": {
                    "kind": "structured_field",
                    "field": "financial_final_reports.recommendation",
                },
            }
        ]
        if report[5] is not None:
            structured.append({
                "statement": f"TradingAgents confidence: {float(report[5]):.6g}",
                "metric": "recommendation_confidence",
                "value": {"kind": "scalar", "number": float(report[5])},
                "claim_type": "opinion",
                "unit": "ratio",
                "source_span": {
                    "kind": "structured_field",
                    "field": "financial_final_reports.confidence",
                },
            })
        sources.append(self.extractor.extract(
            summary_text,
            source_kind="final_report_summary",
            source_ref=f"report:{int(report[0])}:v{int(report[2])}:summary",
            subject=subject,
            as_of=as_of,
            structured_claims=structured,
            request_id=request_id,
        ))
        for section in sections:
            sources.append(self.extractor.extract(
                str(section[3] or ""),
                source_kind="final_report_section",
                source_ref=f"report:{int(report[0])}:section:{int(section[0])}",
                subject=subject,
                as_of=as_of,
                request_id=request_id,
            ))
        claims = FinancialClaimExtractor._dedupe(
            [claim for source in sources for claim in source.get("claims") or []]
        )
        statuses = {str(source.get("status")) for source in sources}
        return {
            "schema_version": FINANCIAL_CLAIM_EXTRACTION_VERSION,
            "status": "failed" if statuses == {"failed"} else ("partial" if "partial" in statuses else "complete"),
            "report_id": int(report[0]),
            "report_version": int(report[2]),
            "report_status": str(report[3]),
            "research_run_id": str(report[1]),
            "subject": subject,
            "claims": claims,
            "sources": sources,
            "errors": list(dict.fromkeys(error for source in sources for error in source.get("errors") or [])),
        }

    def extract_and_persist_report(self, report_id: int, *, request_id: str = "") -> dict:
        result = self.extract_report(report_id, request_id=request_id)
        if result.get("status") == "failed" and not result.get("claims"):
            return {**result, "persisted_claim_count": 0}
        run_id = str(result["research_run_id"])
        with self.database.lock:
            self.connection.execute("SAVEPOINT financial_claim_extract")
            try:
                for claim in result["claims"]:
                    normalized = {
                        "extractor_version": FINANCIAL_CLAIM_EXTRACTOR_VERSION,
                        "metric": claim["metric"],
                        "value": claim["value"],
                        "period": claim["period"],
                        "as_of": claim["as_of"],
                        "polarity": claim["polarity"],
                        "source_span": claim["source_span"],
                        "extraction_method": claim["extraction_method"],
                        "reason_codes": claim["reason_codes"],
                        "subject": claim["subject"],
                    }
                    self.connection.execute(
                        """
                        INSERT INTO financial_claims(
                            research_run_id, final_report_id, claim_key, claim_type,
                            subject, statement, normalized_value_json, unit, currency,
                            effective_at, observed_at, verification_status
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(research_run_id, claim_key) DO UPDATE SET
                            updated_at=financial_claims.updated_at
                        """,
                        (
                            run_id, int(result["report_id"]), claim["claim_key"],
                            claim["claim_type"], str(
                                claim["subject"].get("instrument_key")
                                or claim["subject"].get("canonical_symbol")
                                or claim["subject"].get("display_name")
                                or ""
                            ), claim["statement"],
                            _json(normalized), claim["unit"], claim["currency"],
                            claim["period"].get("end") or claim["period"].get("start") or None,
                            claim["as_of"] or None, claim["verification_status"],
                        ),
                    )
                self.connection.execute("RELEASE SAVEPOINT financial_claim_extract")
            except Exception:
                self.connection.execute("ROLLBACK TO SAVEPOINT financial_claim_extract")
                self.connection.execute("RELEASE SAVEPOINT financial_claim_extract")
                raise
            count = self.connection.execute(
                "SELECT COUNT(*) FROM financial_claims WHERE research_run_id=? AND final_report_id=?",
                (run_id, int(result["report_id"])),
            ).fetchone()[0]
        return {**result, "persisted_claim_count": int(count)}


def validate_claim_extraction(payload: Mapping[str, object]) -> dict:
    result = dict(payload)
    _VALIDATOR.validate(result)
    return result


__all__ = [
    "CLAIM_EXTRACTION_SCHEMA",
    "FINANCIAL_CLAIM_EXTRACTION_VERSION",
    "FINANCIAL_CLAIM_EXTRACTOR_VERSION",
    "FinancialClaimExtractor",
    "FinancialClaimService",
    "NUMERIC_COVERAGE_THRESHOLD",
    "SharedLLMClaimExtractor",
    "validate_claim_extraction",
]
