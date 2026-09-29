#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Read-only, separately sourced Dashboard feed for financial intelligence.

Market snapshots are facts, classified articles are source documents, and
TradingAgents reports are research opinions.  They intentionally remain in
their own tables and are only combined in memory for display pagination.
"""

from __future__ import annotations

import json
import hashlib
import math
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

import config
from financial_config import financial_product_capabilities
from industry_packs import industry_pack_loader
from financial_intent_classifier import financial_classification_gate
from financial_security import redact_public_payload, safe_public_url
from financial_health import FinancialHealthService
from financial_evidence import (
    _article_time_interval,
    _normalize_text,
    _parse_stored_datetime,
    _term_occurs,
)
from financial_news_query import FinancialNewsQueryService
from intel_contracts import parse_time_range, utc_text
from intel_database import ARTICLE_TIME_SQL, INDUSTRY_KEYWORD_GATE_SQL
from project_keyword_gate import (
    configured_project_keyword_snapshot,
    matched_project_keywords,
)
from utils import coerce_int


FINANCIAL_FEED_VERSION = "financial-dashboard-feed-v1"
FINANCIAL_FEED_DETAIL_VERSION = "financial-dashboard-card-detail-v1"
CONTENT_KINDS = ("market_fact", "source_document", "research_opinion")
KIND_LABELS = {
    "market_fact": "事实",
    "source_document": "原文",
    "research_opinion": "研究观点",
}
CORE_CATEGORIES = (
    {"key": "market_index", "name": "市场与指数"},
    {"key": "stock_issuer", "name": "股票与发行人"},
    {"key": "fund_etf", "name": "ETF 与基金"},
    {"key": "macro_policy", "name": "宏观与政策"},
    {"key": "financial_news", "name": "金融资讯与公告"},
)
TRADINGAGENTS_CATEGORIES = (
    {"key": "tradingagents_report", "name": "TradingAgents 终极报告"},
    {"key": "risk_watch", "name": "风险观察"},
)
SIMULATION_CATEGORY = {
    "key": "paper_backtest",
    "name": "模拟组合与回测",
}
_TERMINAL_REPORT_DENYLIST = frozenset({"", "draft", "failed", "cancelled"})
# These terms identify a financial/business facet on an article that has
# already passed the active primary-industry gate.  Matching is deliberately
# limited to the title and stored extraction keywords; body-only hits often
# come from unrelated links embedded in archive pages.
FINANCIAL_NEWS_SIGNAL_KEYWORDS = (
    "融资", "投资", "上市", "股票", "股价", "股权", "股份", "证券",
    "基金", "债券", "资本市场", "金融市场", "保险", "银行", "贷款", "信贷",
    "估值", "市值", "营收", "利润", "亏损", "财报", "业绩", "分红", "回购",
    "并购", "收购", "IPO", "关税",
    "funding", "financing", "investment", "investor", "stock", "shares",
    "equity", "bond", "insurance", "loan", "valuation", "market capitalization",
    "revenue", "profit", "earnings", "financial results", "acquisition", "merger",
    "tariff",
)
_SNAPSHOT_FIELDS = (
    "last_price",
    "close",
    "change_percent",
    "previous_close",
    "open",
    "high",
    "low",
    "volume",
    "amount",
    "nav",
    "metric",
    "value",
)
MARKET_OVERVIEW_DEFINITIONS = (
    {
        "key": "sse",
        "title": "上证指数",
        "canonical_symbol": "000001.SH",
        "market": "CN",
        "symbols": {"000001.SH", "000001.SS", "^SSEC", "SH000001"},
        "news_terms": {"A股", "上证指数", "上证综指", "上海证券交易所"},
    },
    {
        "key": "szse",
        "title": "深证成指",
        "canonical_symbol": "399001.SZ",
        "market": "CN",
        "symbols": {"399001.SZ", "^SZSC", "SZ399001"},
        "news_terms": {"A股", "深证成指", "深成指", "深圳证券交易所"},
    },
    {
        "key": "hsi",
        "title": "恒生指数",
        "canonical_symbol": "HSI.HK",
        "market": "HK",
        "symbols": {"HSI.HK", "^HSI", "HSI"},
        "news_terms": {"港股", "恒生指数", "恒指", "香港股市"},
    },
    {
        "key": "nasdaq",
        "title": "纳斯达克综合指数",
        "canonical_symbol": "IXIC.US",
        "market": "US",
        "symbols": {"IXIC.US", "^IXIC", "IXIC"},
        "news_terms": {"美股", "纳斯达克综合指数", "纳斯达克", "Nasdaq Composite"},
    },
    {
        "key": "nikkei",
        "title": "日经225",
        "canonical_symbol": "N225.JP",
        "market": "JP",
        "symbols": {"N225.JP", "^N225", "N225", "NI225"},
        "news_terms": {"日本股市", "日经225", "日经指数", "Nikkei 225"},
    },
)
FIXED_OVERVIEW_SYMBOLS = tuple(sorted({
    str(symbol).strip().upper()
    for definition in MARKET_OVERVIEW_DEFINITIONS
    for symbol in definition["symbols"]
}))


def _market_overview_title(definition: Mapping[str, object]) -> str:
    return f"{definition['title']}（{definition['canonical_symbol']}）"


def _json_object(value) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _json_value(value, default):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _finite_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _market_movement(change_percent, *, market: str, basis: str) -> dict:
    """Return a bounded direction token using the target market's color convention."""

    normalized_market = str(market or "").strip().upper()
    if normalized_market in {"CN", "XSHG", "XSHE", "HK", "XHKG"}:
        convention = "red_up_green_down"
        up_color, down_color = "red", "green"
    elif normalized_market in {"JP", "XTKS", "XJPX"}:
        convention = "red_up_blue_down"
        up_color, down_color = "red", "blue"
    else:
        convention = "green_up_red_down"
        up_color, down_color = "green", "red"
    normalized_change = _finite_number(change_percent)
    if normalized_change is None:
        return {
            "status": "unavailable",
            "direction": "unknown",
            "symbol": "—",
            "change_percent": None,
            "basis": "",
            "color": "neutral",
            "market_convention": convention,
        }
    rounded_change = round(normalized_change, 4)
    if rounded_change > 0:
        direction, symbol, color = "up", "↑", up_color
    elif rounded_change < 0:
        direction, symbol, color = "down", "↓", down_color
    else:
        direction, symbol, color = "flat", "→", "neutral"
    return {
        "status": "ready",
        "direction": direction,
        "symbol": symbol,
        "change_percent": rounded_change,
        "basis": str(basis or ""),
        "color": color,
        "market_convention": convention,
    }


def _safe_source_url(value) -> str:
    return safe_public_url(value)


def _project_keyword_hits(item: Mapping[str, object], keywords: list[str]) -> list[str]:
    """Match an aggregate financial card against the active primary industry."""

    scope = item.get("scope") if isinstance(item.get("scope"), Mapping) else {}
    return matched_project_keywords(
        keywords,
        item.get("title"),
        item.get("summary"),
        json.dumps(item.get("values") or {}, ensure_ascii=False, sort_keys=True),
        json.dumps(item.get("overview") or {}, ensure_ascii=False, sort_keys=True),
        json.dumps(item.get("risk_summary") or {}, ensure_ascii=False, sort_keys=True),
        scope.get("symbol"),
        scope.get("display_name"),
        scope.get("asset_type"),
        scope.get("market"),
    )


def _financial_news_signal_hits(*texts: object) -> list[str]:
    """Return explicit finance signals from trusted title/keyword fields."""

    return matched_project_keywords(FINANCIAL_NEWS_SIGNAL_KEYWORDS, *texts)


def _snapshot_values(payload: Mapping[str, object]) -> dict:
    values = {}
    normalized = payload.get("normalized_payload")
    candidates = [payload]
    if isinstance(normalized, Mapping):
        candidates.insert(0, normalized)
        bars = normalized.get("bars")
        if isinstance(bars, list) and bars and isinstance(bars[-1], Mapping):
            candidates.insert(0, bars[-1])
    for field in _SNAPSHOT_FIELDS:
        for source in candidates:
            value = source.get(field)
            if field == "metric" and isinstance(value, str):
                values[field] = value[:80]
                break
            number = _finite_number(value)
            if number is not None:
                values[field] = number
                break
    return values


def _qualified_snapshot_payload(payload_text: object, payload_sha256: object, quality_status: object) -> dict:
    quality = str(quality_status or "").casefold()
    if quality not in {"verified", "available", "normalized"} and not quality.startswith(
        "normalized_"
    ):
        return {}
    encoded = str(payload_text or "")
    if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != str(payload_sha256 or ""):
        return {}
    return _json_object(encoded)


def _history_field(payload: Mapping[str, object]) -> str:
    values = _snapshot_values(payload)
    for field in ("last_price", "close", "nav", "value"):
        if _finite_number(values.get(field)) is not None:
            return field
    return ""


def _snapshot_category(asset_type: str, scope_type: str, data_type: str) -> str:
    normalized = str(asset_type or "").casefold()
    if normalized in {"etf", "fund", "mutual_fund"}:
        return "fund_etf"
    if normalized in {"stock", "equity"}:
        return "stock_issuer"
    if normalized in {"macro", "economic_indicator", "bond", "forex"}:
        return "macro_policy"
    if str(scope_type or "").casefold() == "universe" or normalized == "index":
        return "market_index"
    if str(data_type or "").casefold() in {"macro", "fundamental"}:
        return "macro_policy"
    return "market_index"


def _article_category(final_category: str, topic_tags_json: str) -> str:
    tags = _json_value(topic_tags_json, [])
    tag_text = " ".join(str(item) for item in tags).casefold() if isinstance(tags, list) else str(tags).casefold()
    if str(final_category or "").casefold() == "trend" or any(
        marker in tag_text for marker in ("policy", "regulat", "monetary", "macro")
    ):
        return "macro_policy"
    return "financial_news"


def _risk_summary(value) -> dict:
    source = _json_object(value)
    allowed = {}
    for key in (
        "risk_level", "summary", "assessment", "key_risks", "risk_factors",
        "constraints", "aggressive", "conservative", "neutral",
    ):
        item = source.get(key)
        if isinstance(item, (str, int, float, bool)) or item is None:
            allowed[key] = str(item or "")[:1000]
        elif isinstance(item, list):
            allowed[key] = [str(entry)[:500] for entry in item[:20] if isinstance(entry, (str, int, float))]
    return allowed


def _report_overview(report_json_value, risk_summary: Mapping[str, object]) -> dict:
    report_json = _json_object(report_json_value)
    coverage = _finite_number(report_json.get("evidence_coverage"))
    gaps = report_json.get("degraded_categories") or report_json.get("missing_sections") or []
    if not isinstance(gaps, list):
        gaps = []
    return {
        "as_of": str(report_json.get("as_of") or "")[:80],
        "market_status": str(report_json.get("market_status") or "")[:80],
        "evidence_coverage": coverage,
        "component_contribution_coverage": _finite_number(
            report_json.get("component_contribution_coverage")
        ),
        "data_latency_seconds": _finite_number(report_json.get("data_latency_seconds")),
        "constituent_as_of": str(report_json.get("constituent_as_of") or "")[:80],
        "data_gaps": [str(item)[:200] for item in gaps[:30]],
        "counter_evidence": str(risk_summary.get("conservative") or "")[:1000],
        "risk_excerpt": str(
            risk_summary.get("summary")
            or risk_summary.get("neutral")
            or risk_summary.get("risk_level")
            or ""
        )[:1000],
    }


class FinancialFeedService:
    """Build a bounded, read-only financial feed on the existing SQLite DB."""

    def __init__(self, database, *, settings=None, pack_loader=None, clock=None):
        self.database = database
        self.settings = settings
        self.pack_loader = pack_loader
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def connection(self):
        self.database._ensure_connection()
        return self.database.connection

    def _gate(self, industry_pack_id: str) -> tuple[dict, dict]:
        kwargs = {"settings": self.settings}
        if self.pack_loader is not None:
            kwargs["pack_loader"] = self.pack_loader
        product_gate = financial_classification_gate(industry_pack_id, **kwargs)
        capabilities = financial_product_capabilities(
            industry_pack_id,
            settings=self.settings,
            pack_loader=self.pack_loader,
        ) if self.pack_loader is not None else financial_product_capabilities(
            industry_pack_id,
            settings=self.settings,
        )
        dashboard = capabilities.get("dashboard_capabilities") or {}
        news_enabled = bool(
            dashboard.get("show_financial_news")
            and capabilities.get("pack_has_financial_markets")
            and (capabilities.get("configured") or {}).get("financial_intelligence")
            and ((capabilities.get("rollout") or {}).get("capabilities") or {}).get(
                "dashboard"
            )
        )
        gate = {
            **product_gate,
            "enabled": bool(product_gate["enabled"] or news_enabled),
            "reason": (
                "enabled"
                if product_gate["enabled"] or news_enabled
                else product_gate["reason"]
            ),
            "financial_news_enabled": news_enabled,
            "financial_products_enabled": bool(product_gate["enabled"]),
        }
        return gate, capabilities

    def _snapshot_movement(
        self,
        *,
        payload: Mapping[str, object],
        market: str,
        instrument_id,
        universe_id,
        provider_profile_id,
        data_type,
        currency,
        observed_at,
    ) -> dict:
        values = _snapshot_values(payload)
        reported_change = _finite_number(values.get("change_percent"))
        if reported_change is not None:
            return _market_movement(
                reported_change,
                market=market,
                basis="reported_change_percent",
            )
        value_field = _history_field(payload)
        current_value = _finite_number(values.get(value_field)) if value_field else None
        previous_close = _finite_number(values.get("previous_close"))
        if current_value is not None and previous_close not in {None, 0}:
            return _market_movement(
                (current_value - previous_close) / previous_close * 100,
                market=market,
                basis="reported_previous_close",
            )
        if current_value is None or provider_profile_id is None or not value_field:
            return _market_movement(None, market=market, basis="")
        if instrument_id is not None:
            scope_clause, scope_id = "snapshot.instrument_id=?", int(instrument_id)
        elif universe_id is not None:
            scope_clause, scope_id = "snapshot.universe_id=?", int(universe_id)
        else:
            return _market_movement(None, market=market, basis="")
        candidates = self.connection.execute(
            f"""
            SELECT snapshot.payload_json, snapshot.payload_sha256,
                   snapshot.quality_status
            FROM financial_data_snapshots snapshot
            WHERE {scope_clause}
              AND snapshot.provider_profile_id=?
              AND snapshot.data_type=?
              AND COALESCE(snapshot.currency,'')=?
              AND datetime(snapshot.observed_at) < datetime(?)
            ORDER BY datetime(snapshot.observed_at) DESC,
                     datetime(snapshot.fetched_at) DESC, snapshot.id DESC
            LIMIT 20
            """,
            (
                scope_id,
                int(provider_profile_id),
                str(data_type or ""),
                str(currency or ""),
                str(observed_at or ""),
            ),
        ).fetchall()
        for candidate in candidates:
            previous_payload = _qualified_snapshot_payload(
                candidate[0], candidate[1], candidate[2]
            )
            previous_value = _finite_number(
                _snapshot_values(previous_payload).get(value_field)
            )
            if previous_value in {None, 0}:
                continue
            return _market_movement(
                (current_value - previous_value) / previous_value * 100,
                market=market,
                basis="previous_snapshot",
            )
        return _market_movement(None, market=market, basis="")

    def _snapshot_rows(
        self,
        start_text: str,
        end_text: str,
        limit: int,
        *,
        viewer_user_id: str = "",
        project_keywords: list[str] | None = None,
    ) -> tuple[list[dict], int]:
        fixed_placeholders = ", ".join("?" for _ in FIXED_OVERVIEW_SYMBOLS)
        hidden_clause = ""
        params = [start_text, end_text]
        normalized_user_id = str(viewer_user_id or "").strip()
        if normalized_user_id:
            hidden_clause = """
                  AND NOT EXISTS (
                      SELECT 1
                      FROM financial_dashboard_hidden_instruments hidden
                      WHERE hidden.owner_user_id=?
                        AND hidden.instrument_id=snapshot.instrument_id
                  )
            """
            params.append(normalized_user_id)
        params.extend(FIXED_OVERVIEW_SYMBOLS)
        ranked_snapshot_sql = f"""
            WITH ranked_snapshots AS (
                SELECT snapshot.id,
                       ROW_NUMBER() OVER (
                           PARTITION BY CASE
                               WHEN TRIM(COALESCE(instrument.canonical_symbol, '')) <> ''
                                   THEN 'instrument:' || UPPER(TRIM(instrument.canonical_symbol))
                               WHEN TRIM(COALESCE(universe.universe_key, '')) <> ''
                                   THEN 'universe:' || UPPER(TRIM(universe.universe_key))
                               WHEN snapshot.instrument_id IS NOT NULL
                                   THEN 'instrument_id:' || CAST(snapshot.instrument_id AS TEXT)
                               WHEN snapshot.universe_id IS NOT NULL
                                   THEN 'universe_id:' || CAST(snapshot.universe_id AS TEXT)
                               ELSE 'snapshot:' || CAST(snapshot.id AS TEXT)
                           END
                           ORDER BY datetime(snapshot.observed_at) DESC,
                                    datetime(snapshot.fetched_at) DESC,
                                    snapshot.id DESC
                       ) AS scope_rank
                FROM financial_data_snapshots snapshot
                LEFT JOIN financial_instruments instrument
                  ON instrument.id=snapshot.instrument_id
                LEFT JOIN financial_universes universe
                  ON universe.id=snapshot.universe_id
                WHERE datetime(snapshot.observed_at) >= datetime(?)
                  AND datetime(snapshot.observed_at) <= datetime(?)
                  {hidden_clause}
                  AND UPPER(TRIM(COALESCE(
                      instrument.canonical_symbol,
                      universe.universe_key,
                      ''
                  ))) NOT IN ({fixed_placeholders})
            )
        """
        count_row = self.connection.execute(
            ranked_snapshot_sql + "SELECT COUNT(*) FROM ranked_snapshots WHERE scope_rank=1",
            tuple(params),
        ).fetchone()
        rows = self.connection.execute(
            ranked_snapshot_sql + """
            SELECT snapshot.id, snapshot.data_type, snapshot.observed_at,
                   snapshot.fetched_at, snapshot.market_status, snapshot.currency,
                   snapshot.stale_after, snapshot.quality_status,
                   snapshot.payload_json, snapshot.source_url,
                   provider.provider_key, provider.display_name,
                   instrument.id, instrument.canonical_symbol,
                   instrument.display_name, instrument.asset_type, instrument.market,
                   universe.id, universe.universe_key, universe.display_name,
                   universe.universe_type, universe.market,
                   snapshot.provider_profile_id
            FROM ranked_snapshots ranked
            JOIN financial_data_snapshots snapshot
              ON snapshot.id=ranked.id
            JOIN financial_provider_profiles provider
              ON provider.id=snapshot.provider_profile_id
            LEFT JOIN financial_instruments instrument
              ON instrument.id=snapshot.instrument_id
            LEFT JOIN financial_universes universe
              ON universe.id=snapshot.universe_id
            WHERE ranked.scope_rank=1
            ORDER BY datetime(snapshot.observed_at) DESC,
                     datetime(snapshot.fetched_at) DESC, snapshot.id DESC
            LIMIT ?
            """,
            (*params, 2000 if project_keywords is not None else int(limit)),
        ).fetchall()
        now_text = utc_text(self.clock())
        items = []
        for row in rows:
            payload = _json_object(row[8])
            scope_type = "instrument" if row[12] is not None else "universe"
            asset_type = str(row[15] or row[20] or "")
            display_name = str(row[14] or row[19] or row[13] or row[18] or "金融市场")
            symbol = str(row[13] or row[18] or "")
            market = str(row[16] or row[21] or "")
            movement = self._snapshot_movement(
                payload=payload,
                market=market,
                instrument_id=row[12],
                universe_id=row[17],
                provider_profile_id=row[22],
                data_type=row[1],
                currency=row[5],
                observed_at=row[2],
            )
            item = {
                    "content_kind": "market_fact",
                    "kind_label": KIND_LABELS["market_fact"],
                    "item_id": f"snapshot:{int(row[0])}",
                    "snapshot_id": int(row[0]),
                    "title": f"{display_name}{'（' + symbol + '）' if symbol and symbol != display_name else ''}",
                    "summary": "结构化行情快照；请结合质量、时点和市场状态判断实时性。",
                    "subcategory": _snapshot_category(asset_type, scope_type, str(row[1] or "")),
                    "observed_at": str(row[2] or ""),
                    "fetched_at": str(row[3] or ""),
                    "sort_time": str(row[2] or row[3] or ""),
                    "market_status": str(row[4] or "unknown"),
                    "currency": str(row[5] or ""),
                    "stale_after": str(row[6] or ""),
                    "is_stale": bool(row[6] and str(row[6]) < now_text),
                    "quality_status": str(row[7] or "unverified"),
                    "values": _snapshot_values(payload),
                    "movement": movement,
                    "source": {
                        "provider_key": str(row[10] or ""),
                        "provider_name": str(row[11] or row[10] or ""),
                        "url": _safe_source_url(row[9]),
                    },
                    "scope": {
                        "scope_type": scope_type,
                        "instrument_id": int(row[12]) if row[12] is not None else None,
                        "universe_id": int(row[17]) if row[17] is not None else None,
                        "symbol": symbol,
                        "display_name": display_name,
                        "asset_type": asset_type,
                        "market": market,
                    },
                }
            if project_keywords is not None:
                hits = _project_keyword_hits(item, project_keywords)
                if not hits:
                    continue
                item["matched_keywords"] = hits
                item["keyword_gate_source"] = "active_published_industry_pack"
            items.append(item)
        if project_keywords is not None:
            return items[: int(limit)], len(items)
        return items, int(count_row[0] or 0)

    def _market_overview_cards(
        self,
        *,
        project_keywords: list[str] | None = None,
    ) -> list[dict]:
        """Return five fixed index cards without consuming feed pagination."""

        rows = self.connection.execute(
            """
            SELECT snapshot.id, snapshot.data_type, snapshot.observed_at,
                   snapshot.fetched_at, snapshot.market_status, snapshot.currency,
                   snapshot.stale_after, snapshot.quality_status,
                   snapshot.payload_json, snapshot.payload_sha256,
                   snapshot.source_url, provider.provider_key, provider.display_name,
                   instrument.id, instrument.canonical_symbol,
                   instrument.display_name, instrument.asset_type,
                   instrument.market, instrument.exchange,
                   universe.id, universe.universe_key, universe.display_name,
                   universe.universe_type, universe.market,
                   snapshot.provider_profile_id
            FROM financial_data_snapshots snapshot
            JOIN financial_provider_profiles provider
              ON provider.id=snapshot.provider_profile_id
            LEFT JOIN financial_instruments instrument
              ON instrument.id=snapshot.instrument_id
            LEFT JOIN financial_universes universe
              ON universe.id=snapshot.universe_id
            WHERE lower(COALESCE(instrument.asset_type,''))='index'
               OR universe.id IS NOT NULL
            ORDER BY datetime(snapshot.observed_at) DESC,
                     datetime(snapshot.fetched_at) DESC, snapshot.id DESC
            LIMIT 500
            """
        ).fetchall()
        qualified = []
        for row in rows:
            quality = str(row[7] or "").casefold()
            if quality not in {"verified", "available", "normalized"} and not quality.startswith(
                "normalized_"
            ):
                continue
            payload_text = str(row[8] or "")
            if hashlib.sha256(payload_text.encode("utf-8")).hexdigest() != str(row[9] or ""):
                continue
            qualified.append(row)

        now_text = utc_text(self.clock())
        cards = []
        for definition in MARKET_OVERVIEW_DEFINITIONS:
            selected = None
            for row in qualified:
                symbol = str(row[14] or row[20] or "").strip().upper()
                if symbol in definition["symbols"]:
                    selected = row
                    break
            if selected is None:
                cards.append(
                    {
                        "content_kind": "market_fact",
                        "kind_label": "大盘",
                        "item_id": f"overview:{definition['key']}",
                        "overview_key": definition["key"],
                        "fixed": True,
                        "title": _market_overview_title(definition),
                        "summary": "暂无通过完整性与质量校验的指数快照；卡片固定保留，不生成或推测数值。",
                        "subcategory": "market_index",
                        "observed_at": "",
                        "fetched_at": "",
                        "market_status": "unknown",
                        "currency": "",
                        "is_stale": True,
                        "quality_status": "unavailable",
                        "values": {},
                        "movement": _market_movement(
                            None, market=definition["market"], basis=""
                        ),
                        "source": {"provider_key": "", "provider_name": "结构化指数源", "url": ""},
                        "scope": {
                            "scope_type": "market_overview",
                            "symbol": definition["canonical_symbol"],
                            "display_name": definition["title"],
                            "asset_type": "index",
                            "market": definition["market"],
                        },
                        "availability": {
                            "status": "unavailable",
                            "reason": "no_qualified_index_snapshot",
                        },
                    }
                )
                continue
            row = selected
            payload = _json_object(row[8])
            symbol = str(row[14] or row[20] or "")
            display_name = str(row[15] or row[21] or symbol or definition["title"])
            market = str(row[17] or row[23] or definition["market"])
            movement = self._snapshot_movement(
                payload=payload,
                market=market,
                instrument_id=row[13],
                universe_id=row[19],
                provider_profile_id=row[24],
                data_type=row[1],
                currency=row[5],
                observed_at=row[2],
            )
            cards.append(
                {
                    "content_kind": "market_fact",
                    "kind_label": "大盘",
                    "item_id": f"overview:{definition['key']}",
                    "overview_key": definition["key"],
                    "fixed": True,
                    "snapshot_id": int(row[0]),
                    "title": _market_overview_title(definition),
                    "summary": "结构化指数快照；展示最新通过完整性与质量校验的观察，并保留来源与绝对时点。",
                    "subcategory": "market_index",
                    "observed_at": str(row[2] or ""),
                    "fetched_at": str(row[3] or ""),
                    "market_status": str(row[4] or "unknown"),
                    "currency": str(row[5] or ""),
                    "stale_after": str(row[6] or ""),
                    "is_stale": bool(not row[6] or str(row[6]) < now_text),
                    "quality_status": str(row[7] or ""),
                    "values": _snapshot_values(payload),
                    "movement": movement,
                    "source": {
                        "provider_key": str(row[11] or ""),
                        "provider_name": str(row[12] or row[11] or ""),
                        "url": _safe_source_url(row[10]),
                    },
                    "scope": {
                        "scope_type": "instrument" if row[13] is not None else "universe",
                        "instrument_id": int(row[13]) if row[13] is not None else None,
                        "universe_id": int(row[19]) if row[19] is not None else None,
                        "symbol": definition["canonical_symbol"],
                        "display_name": display_name,
                        "asset_type": str(row[16] or row[22] or "index"),
                        "market": market,
                    },
                    "availability": {"status": "ready", "reason": "qualified_snapshot"},
                }
            )
        if project_keywords is None:
            return cards
        gated = []
        for card in cards:
            hits = _project_keyword_hits(card, project_keywords)
            if not hits:
                continue
            card["matched_keywords"] = hits
            card["keyword_gate_source"] = "active_published_industry_pack"
            gated.append(card)
        return gated

    def _article_rows(
        self,
        start_text: str,
        end_text: str,
        limit: int,
        *,
        project_pack_id: str,
        project_keywords: list[str],
        activation_id: str = "",
    ) -> tuple[list[dict], int]:
        if not project_keywords:
            return [], 0
        main_category_exclusion = ""
        if project_pack_id in {"family_office", "financial_markets"}:
            main_category_exclusion = f"""
            AND NOT EXISTS (
                SELECT 1
                FROM article_intel_classifications project_classification
                WHERE project_classification.article_id=a.id
                  AND project_classification.industry_pack_id=?
                  AND (?='' OR project_classification.activation_id=?)
                  AND project_classification.final_category IN ('event','trend')
                  AND {INDUSTRY_KEYWORD_GATE_SQL.replace('c.', 'project_classification.')}
            )
            """
        where = f"""
            a.status='active'
            AND a.url NOT LIKE 'ai://chat%'
            AND c.industry_pack_id='financial_markets'
            AND (?='' OR c.activation_id=?)
            AND (
                ?=''
                OR EXISTS (
                    SELECT 1 FROM financial_addon_article_matches addon_match
                    WHERE addon_match.article_id=a.id
                      AND addon_match.activation_id=?
                      AND addon_match.primary_industry_pack_id=?
                      AND addon_match.is_visible=1
                )
            )
            AND {INDUSTRY_KEYWORD_GATE_SQL}
            AND datetime({ARTICLE_TIME_SQL}) >= datetime(?)
            AND datetime({ARTICLE_TIME_SQL}) <= datetime(?)
            {main_category_exclusion}
        """
        params = [
            str(activation_id or ""),
            str(activation_id or ""),
            str(activation_id or ""),
            str(activation_id or ""),
            str(project_pack_id or ""),
            start_text,
            end_text,
        ]
        if main_category_exclusion:
            params.extend(
                [
                    str(project_pack_id or ""),
                    str(activation_id or ""),
                    str(activation_id or ""),
                ]
            )
        financial_rows = self.connection.execute(
            f"""
            SELECT a.id, a.title, a.url, a.domain, a.publish_date,
                   COALESCE(a.content,''), a.matched_keywords,
                   c.final_category, c.final_confidence, c.final_reason,
                   c.why_important, c.trend_summary, c.topic_tags_json,
                   {ARTICLE_TIME_SQL} AS effective_time
            FROM article_intel_classifications c
            JOIN articles a ON a.id=c.article_id
            WHERE {where}
            ORDER BY datetime(effective_time) DESC, a.id DESC
            """,
            params,
        ).fetchall()
        candidate_rows = [(row, False) for row in financial_rows]
        if project_pack_id not in {"family_office", "financial_markets"}:
            project_rows = self.connection.execute(
                f"""
                SELECT a.id, a.title, a.url, a.domain, a.publish_date,
                       COALESCE(a.content,''), a.matched_keywords,
                       c.final_category, c.final_confidence, c.final_reason,
                       c.why_important, c.trend_summary, c.topic_tags_json,
                       {ARTICLE_TIME_SQL} AS effective_time
                FROM article_intel_classifications c
                JOIN articles a ON a.id=c.article_id
                WHERE a.status='active'
                  AND a.url NOT LIKE 'ai://chat%'
                  AND c.industry_pack_id=?
                  AND (?='' OR c.activation_id=?)
                  AND {INDUSTRY_KEYWORD_GATE_SQL}
                  AND datetime({ARTICLE_TIME_SQL}) >= datetime(?)
                  AND datetime({ARTICLE_TIME_SQL}) <= datetime(?)
                ORDER BY datetime(effective_time) DESC, a.id DESC
                """,
                (
                    str(project_pack_id or ""),
                    str(activation_id or ""),
                    str(activation_id or ""),
                    start_text,
                    end_text,
                ),
            ).fetchall()
            candidate_rows.extend((row, True) for row in project_rows)
        items = []
        seen_article_ids = set()
        for row, require_financial_signal in candidate_rows:
            article_id = int(row[0])
            if article_id in seen_article_ids:
                continue
            matched_keywords = matched_project_keywords(
                project_keywords,
                row[1],
                row[5],
                row[6],
            )
            if not matched_keywords:
                continue
            if require_financial_signal and not _financial_news_signal_hits(
                row[1], row[6]
            ):
                continue
            seen_article_ids.add(article_id)
            items.append(
                {
                    "content_kind": "source_document",
                    "kind_label": KIND_LABELS["source_document"],
                    "item_id": f"article:{article_id}",
                    "article_id": article_id,
                    "title": str(row[1] or "无标题"),
                    "summary": str(row[11] or row[10] or row[9] or row[5] or "")[:500],
                    "subcategory": _article_category(str(row[7] or ""), str(row[12] or "")),
                    "observed_at": str(row[13] or ""),
                    "fetched_at": "",
                    "sort_time": str(row[13] or ""),
                    "publish_date": str(row[4] or ""),
                    "confidence": _finite_number(row[8]),
                    "matched_keywords": matched_keywords,
                    "keyword_gate_source": "active_published_industry_pack",
                    "source": {
                        "provider_key": str(row[3] or ""),
                        "provider_name": str(row[3] or ""),
                        "url": _safe_source_url(row[2]),
                    },
                }
            )
        items.sort(
            key=lambda item: (
                str(item.get("sort_time") or ""),
                int(item.get("article_id") or 0),
            ),
            reverse=True,
        )
        return items[: int(limit)], len(items)

    def _report_rows(
        self,
        start_text: str,
        end_text: str,
        limit: int,
        *,
        project_keywords: list[str] | None = None,
    ) -> tuple[list[dict], int]:
        denied = tuple(sorted(_TERMINAL_REPORT_DENYLIST))
        placeholders = ",".join("?" for _ in denied)
        where = f"""
            run.status='completed'
            AND lower(COALESCE(report.report_status,'')) NOT IN ({placeholders})
            AND NOT EXISTS(
                SELECT 1 FROM financial_final_reports newer
                WHERE newer.research_run_id=report.research_run_id
                  AND newer.report_version>report.report_version
            )
            AND datetime(COALESCE(report.fetched_at, report.updated_at, report.created_at)) >= datetime(?)
            AND datetime(COALESCE(report.fetched_at, report.updated_at, report.created_at)) <= datetime(?)
        """
        params = (*denied, start_text, end_text)
        count_row = self.connection.execute(
            f"""SELECT COUNT(*) FROM financial_final_reports report
                JOIN financial_research_runs run ON run.id=report.research_run_id
                WHERE {where}""",
            params,
        ).fetchone()
        rows = self.connection.execute(
            f"""
            SELECT report.id, report.research_run_id, report.report_version,
                   report.report_status, report.recommendation, report.confidence,
                   report.title, report.executive_summary,
                   report.risk_summary_json, report.observed_at, report.fetched_at,
                   report.verified_at, report.updated_at, run.scope_type,
                   instrument.id, instrument.canonical_symbol,
                   instrument.display_name, instrument.asset_type, instrument.market,
                   universe.id, universe.universe_key, universe.display_name,
                   universe.universe_type, universe.market, report.report_json
            FROM financial_final_reports report
            JOIN financial_research_runs run ON run.id=report.research_run_id
            LEFT JOIN financial_instruments instrument ON instrument.id=run.instrument_id
            LEFT JOIN financial_universes universe ON universe.id=run.universe_id
            WHERE {where}
            ORDER BY datetime(COALESCE(report.fetched_at, report.updated_at, report.created_at)) DESC,
                     report.id DESC
            LIMIT ?
            """,
            (*params, 1000 if project_keywords is not None else int(limit)),
        ).fetchall()
        items = []
        for row in rows:
            risk = _risk_summary(row[8])
            overview = _report_overview(row[24], risk)
            display_name = str(row[16] or row[21] or row[15] or row[20] or "市场")
            symbol = str(row[15] or row[20] or "")
            item = {
                    "content_kind": "research_opinion",
                    "kind_label": KIND_LABELS["research_opinion"],
                    "item_id": f"report:{int(row[0])}:v{int(row[2])}",
                    "report_id": int(row[0]),
                    "research_run_id": str(row[1]),
                    "report_version": int(row[2]),
                    "report_status": str(row[3]),
                    "recommendation": str(row[4] or "insufficient_evidence"),
                    "confidence": _finite_number(row[5]),
                    "title": str(row[6] or f"{display_name} TradingAgents 终极报告"),
                    "summary": str(row[7] or "")[:1000],
                    "subcategory": "tradingagents_report",
                    "observed_at": str(row[9] or ""),
                    "fetched_at": str(row[10] or ""),
                    "verified_at": str(row[11] or ""),
                    "sort_time": str(row[10] or row[12] or row[9] or ""),
                    "risk_summary": risk,
                    "overview": overview,
                    "report_url": f"/api/financial/reports/{int(row[0])}",
                    "scope": {
                        "scope_type": str(row[13] or ""),
                        "instrument_id": int(row[14]) if row[14] is not None else None,
                        "universe_id": int(row[19]) if row[19] is not None else None,
                        "symbol": symbol,
                        "display_name": display_name,
                        "asset_type": str(row[17] or row[22] or ""),
                        "market": str(row[18] or row[23] or ""),
                    },
                }
            if project_keywords is not None:
                hits = _project_keyword_hits(item, project_keywords)
                if not hits:
                    continue
                item["matched_keywords"] = hits
                item["keyword_gate_source"] = "active_published_industry_pack"
            items.append(item)
        if project_keywords is not None:
            return items[: int(limit)], len(items)
        return items, int(count_row[0] or 0)

    def _snapshot_detail_row(self, snapshot_id: int) -> dict | None:
        row = self.connection.execute(
            """
            SELECT snapshot.id AS snapshot_id,
                   snapshot.provider_profile_id AS provider_profile_id,
                   snapshot.data_type AS data_type,
                   snapshot.observed_at AS observed_at,
                   snapshot.fetched_at AS fetched_at,
                   snapshot.market_status AS market_status,
                   snapshot.currency AS currency,
                   snapshot.stale_after AS stale_after,
                   snapshot.quality_status AS quality_status,
                   snapshot.payload_json AS payload_json,
                   snapshot.payload_sha256 AS payload_sha256,
                   provider.provider_key AS provider_key,
                   provider.display_name AS provider_name,
                   instrument.id AS instrument_id,
                   instrument.canonical_symbol AS instrument_symbol,
                   instrument.display_name AS instrument_name,
                   instrument.asset_type AS instrument_asset_type,
                   instrument.market AS instrument_market,
                   universe.id AS universe_id,
                   universe.universe_key AS universe_key,
                   universe.display_name AS universe_name,
                   universe.universe_type AS universe_type,
                   universe.market AS universe_market
            FROM financial_data_snapshots snapshot
            JOIN financial_provider_profiles provider
              ON provider.id=snapshot.provider_profile_id
            LEFT JOIN financial_instruments instrument
              ON instrument.id=snapshot.instrument_id
            LEFT JOIN financial_universes universe
              ON universe.id=snapshot.universe_id
            WHERE snapshot.id=?
            """,
            (int(snapshot_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _scope_from_snapshot(row: Mapping[str, object]) -> dict:
        instrument_id = row.get("instrument_id")
        if instrument_id is not None:
            return {
                "scope_type": "instrument",
                "instrument_id": int(instrument_id),
                "universe_id": None,
                "symbol": str(row.get("instrument_symbol") or ""),
                "display_name": str(row.get("instrument_name") or row.get("instrument_symbol") or "金融标的"),
                "asset_type": str(row.get("instrument_asset_type") or ""),
                "market": str(row.get("instrument_market") or ""),
            }
        return {
            "scope_type": "universe",
            "instrument_id": None,
            "universe_id": int(row["universe_id"]) if row.get("universe_id") is not None else None,
            "symbol": str(row.get("universe_key") or ""),
            "display_name": str(row.get("universe_name") or row.get("universe_key") or "金融市场"),
            "asset_type": str(row.get("universe_type") or ""),
            "market": str(row.get("universe_market") or ""),
        }

    def _resolve_detail_target(self, item_id: str) -> tuple[dict, dict | None]:
        snapshot_match = re.fullmatch(r"snapshot:(\d+)", item_id)
        if snapshot_match:
            row = self._snapshot_detail_row(int(snapshot_match.group(1)))
            if row is None:
                raise LookupError("金融行情卡片不存在")
            scope = self._scope_from_snapshot(row)
            title = f"{scope['display_name']}{'（' + scope['symbol'] + '）' if scope['symbol'] and scope['symbol'] != scope['display_name'] else ''}"
            return {
                "item_id": item_id,
                "title": title,
                "overview_key": "",
                "scope": scope,
            }, row

        overview_match = re.fullmatch(
            r"overview:(sse|szse|hsi|nasdaq|nikkei)", item_id
        )
        if not overview_match:
            raise ValueError("只有股票、指数或大盘事实卡支持行情详情")
        overview_key = overview_match.group(1)
        card = next(
            (entry for entry in self._market_overview_cards() if entry.get("overview_key") == overview_key),
            None,
        )
        if card is None:
            raise LookupError("大盘卡片不存在")
        row = self._snapshot_detail_row(int(card["snapshot_id"])) if card.get("snapshot_id") else None
        return {
            "item_id": item_id,
            "title": str(card.get("title") or "大盘行情"),
            "overview_key": overview_key,
            "scope": dict(card.get("scope") or {}),
        }, row

    def _history(self, row: Mapping[str, object] | None, *, limit: int) -> dict:
        if row is None:
            return {
                "status": "unavailable",
                "reason": "no_qualified_history",
                "value_field": "",
                "currency": "",
                "provider": {"provider_key": "", "provider_name": ""},
                "points": [],
                "point_count": 0,
                "from": "",
                "to": "",
            }
        scope_clause = "snapshot.instrument_id=?" if row.get("instrument_id") is not None else "snapshot.universe_id=?"
        scope_id = row.get("instrument_id") if row.get("instrument_id") is not None else row.get("universe_id")
        now_text = utc_text(self.clock())
        candidates = [
            dict(item)
            for item in self.connection.execute(
                f"""
                SELECT snapshot.id AS snapshot_id,
                       snapshot.observed_at AS observed_at,
                       snapshot.fetched_at AS fetched_at,
                       snapshot.market_status AS market_status,
                       snapshot.quality_status AS quality_status,
                       snapshot.payload_json AS payload_json,
                       snapshot.payload_sha256 AS payload_sha256
                FROM financial_data_snapshots snapshot
                WHERE {scope_clause}
                  AND snapshot.provider_profile_id=?
                  AND snapshot.data_type=?
                  AND COALESCE(snapshot.currency,'')=?
                  AND datetime(snapshot.observed_at) <= datetime(?)
                ORDER BY datetime(snapshot.observed_at) DESC, snapshot.id DESC
                LIMIT ?
                """,
                (
                    int(scope_id),
                    int(row["provider_profile_id"]),
                    str(row.get("data_type") or ""),
                    str(row.get("currency") or ""),
                    now_text,
                    int(limit),
                ),
            ).fetchall()
        ]
        qualified = []
        for candidate in candidates:
            payload = _qualified_snapshot_payload(
                candidate.get("payload_json"),
                candidate.get("payload_sha256"),
                candidate.get("quality_status"),
            )
            if payload:
                qualified.append((candidate, payload))
        current_payload = _qualified_snapshot_payload(
            row.get("payload_json"), row.get("payload_sha256"), row.get("quality_status")
        )
        value_field = _history_field(current_payload)
        if not value_field:
            value_field = next((_history_field(payload) for _, payload in qualified if _history_field(payload)), "")
        points_by_time = {}
        for candidate, payload in qualified:
            value = _finite_number(_snapshot_values(payload).get(value_field)) if value_field else None
            observed_at = str(candidate.get("observed_at") or "")
            if value is None or not observed_at or observed_at in points_by_time:
                continue
            points_by_time[observed_at] = {
                "snapshot_id": int(candidate["snapshot_id"]),
                "observed_at": observed_at,
                "fetched_at": str(candidate.get("fetched_at") or ""),
                "market_status": str(candidate.get("market_status") or "unknown"),
                "value": value,
            }
        points = sorted(points_by_time.values(), key=lambda item: item["observed_at"])
        status = "ready" if points else "unavailable"
        return {
            "status": status,
            "reason": "qualified_history" if points else "no_qualified_history",
            "value_field": value_field,
            "currency": str(row.get("currency") or ""),
            "provider": {
                "provider_key": str(row.get("provider_key") or ""),
                "provider_name": str(row.get("provider_name") or row.get("provider_key") or ""),
            },
            "points": points,
            "point_count": len(points),
            "from": points[0]["observed_at"] if points else "",
            "to": points[-1]["observed_at"] if points else "",
        }

    def _instrument_related_news(self, scope: Mapping[str, object], *, limit: int) -> tuple[list[dict], list[str]]:
        now = self.clock()
        service = FinancialNewsQueryService(
            self.database,
            settings=self.settings,
            clock=self.clock,
            max_items=limit,
            refresher=None,
        )
        query = service.plan(
            {
                "status": "resolved",
                "targets": [{
                    "instrument_id": int(scope["instrument_id"]),
                    "canonical_symbol": str(scope.get("symbol") or ""),
                    "display_name": str(scope.get("display_name") or ""),
                    "asset_type": str(scope.get("asset_type") or ""),
                    "market": str(scope.get("market") or ""),
                }],
            },
            {"server_now_utc": utc_text(now)},
            {"channels": ["news"]},
        )
        result = service.execute(query)
        news = []
        for evidence in result.get("evidence") or []:
            article_id = evidence.get("article_id")
            official_news_id = evidence.get("official_news_id")
            item = {
                "title": str(evidence.get("title") or "无标题"),
                "summary": str(evidence.get("content_excerpt") or "")[:600],
                "published_at": str(evidence.get("published_at") or evidence.get("observed_at") or ""),
                "observed_at": str(evidence.get("observed_at") or ""),
                "recency_status": str(evidence.get("recency_status") or ""),
                "age_days": int(evidence.get("age_days") or 0),
                "matched_terms": [
                    str(term) for term in evidence.get("match_terms") or [] if str(term).strip()
                ][:20],
                "source": {
                    "provider_name": str(evidence.get("domain") or "官方公告"),
                    "url": _safe_source_url(evidence.get("source_url")),
                },
                "detail_mode": "article_modal" if article_id else "inline",
            }
            if article_id:
                item["article_id"] = int(article_id)
            if official_news_id:
                item["official_news_id"] = int(official_news_id)
            news.append(item)
        return news, [str(reason) for reason in result.get("reason_codes") or []][:10]

    def _market_related_news(self, target: Mapping[str, object], *, limit: int) -> tuple[list[dict], list[str]]:
        definition = next(
            (item for item in MARKET_OVERVIEW_DEFINITIONS if item["key"] == target.get("overview_key")),
            None,
        )
        terms = set(definition.get("news_terms") or ()) if definition else set()
        scope = target.get("scope") or {}
        terms.update(
            str(value)
            for value in (scope.get("symbol"), scope.get("display_name"))
            if str(value or "").strip()
        )
        terms = {term for term in terms if len(term.strip()) >= 2}
        patterns = []
        for term in sorted(terms, key=lambda value: (-len(value), value))[:20]:
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            patterns.append(f"%{escaped}%")
        if not patterns:
            return [], ["no_market_news_terms"]
        predicates = " OR ".join(
            "(a.title LIKE ? ESCAPE '\\' OR a.content LIKE ? ESCAPE '\\' OR a.matched_keywords LIKE ? ESCAPE '\\')"
            for _ in patterns
        )
        parameters = []
        for pattern in patterns:
            parameters.extend((pattern, pattern, pattern))
        rows = [
            dict(row)
            for row in self.connection.execute(
                f"""
                SELECT a.id, a.url, a.canonical_url, a.title, a.content, a.domain,
                       a.publish_date, a.published_at_utc, a.published_timezone,
                       a.published_precision, a.published_time_source,
                       a.first_crawled, a.created_at, a.updated_at, a.matched_keywords,
                       a.source_task_name
                FROM article_intel_classifications c
                JOIN articles a ON a.id=c.article_id
                WHERE a.status='active'
                  AND c.industry_pack_id='financial_markets'
                  AND {INDUSTRY_KEYWORD_GATE_SQL}
                  AND ({predicates})
                ORDER BY COALESCE(a.published_at_utc,a.publish_date,a.first_crawled,a.created_at) DESC,
                         a.id DESC
                LIMIT 500
                """,
                tuple(parameters),
            ).fetchall()
        ]
        cutoff = self.clock()
        selected = []
        seen = set()
        normalized_terms = tuple(sorted(terms, key=lambda value: (-len(value), value)))
        for row in rows:
            interval = _article_time_interval(row)
            if interval is None:
                continue
            observed_start, _, _ = interval
            if observed_start > cutoff:
                continue
            matched = [
                term for term in normalized_terms
                if _term_occurs(term, _normalize_text(row.get("title")))
                or _term_occurs(term, _normalize_text(row.get("matched_keywords")))
                or _term_occurs(term, _normalize_text(row.get("content")))
            ]
            if not matched:
                continue
            article_id = int(row["id"])
            if article_id in seen:
                continue
            seen.add(article_id)
            fetched = _parse_stored_datetime(row.get("first_crawled")) or observed_start
            selected.append({
                "article_id": article_id,
                "title": str(row.get("title") or "无标题"),
                "summary": str(row.get("content") or "")[:600],
                "published_at": str(row.get("publish_date") or row.get("published_at_utc") or utc_text(observed_start)),
                "observed_at": utc_text(observed_start),
                "fetched_at": utc_text(fetched),
                "recency_status": "recent" if observed_start >= cutoff - timedelta(days=7) else "latest_available",
                "age_days": max(0, int((cutoff - observed_start).total_seconds() // 86400)),
                "matched_terms": matched[:20],
                "source": {
                    "provider_name": str(row.get("domain") or row.get("source_task_name") or ""),
                    "url": _safe_source_url(row.get("url")),
                },
                "detail_mode": "article_modal",
            })
            if len(selected) >= limit:
                break
        return selected, ["matched_market_news"] if selected else ["no_related_market_news"]

    def detail(
        self,
        *,
        industry_pack_id: str,
        item_id: str,
        history_limit: int = 240,
        news_limit: int = 8,
    ) -> dict:
        gate, _ = self._gate(industry_pack_id)
        if not gate["enabled"]:
            raise ValueError("金融能力当前未启用")
        item_id = str(item_id or "").strip()
        history_limit = coerce_int(history_limit, 240, 20, 500)
        news_limit = coerce_int(news_limit, 8, 1, 10)
        with self.database.lock:
            target, row = self._resolve_detail_target(item_id)
            history = self._history(row, limit=history_limit)
        scope = dict(target.get("scope") or {})
        if scope.get("instrument_id") is not None:
            related_news, news_reasons = self._instrument_related_news(scope, limit=news_limit)
        else:
            with self.database.lock:
                related_news, news_reasons = self._market_related_news(target, limit=news_limit)
        current_payload = _qualified_snapshot_payload(
            row.get("payload_json"), row.get("payload_sha256"), row.get("quality_status")
        ) if row else {}
        return redact_public_payload({
            "detail_version": FINANCIAL_FEED_DETAIL_VERSION,
            "item_id": item_id,
            "content_kind": "market_fact",
            "title": target["title"],
            "overview_key": target.get("overview_key") or "",
            "scope": scope,
            "current": {
                "snapshot_id": int(row["snapshot_id"]) if row else None,
                "observed_at": str(row.get("observed_at") or "") if row else "",
                "market_status": str(row.get("market_status") or "unknown") if row else "unknown",
                "quality_status": str(row.get("quality_status") or "unavailable") if current_payload else "unavailable",
                "values": _snapshot_values(current_payload),
            },
            "history": history,
            "related_news": related_news,
            "news_status": {
                "status": "ready" if related_news else "unavailable",
                "reason_codes": news_reasons,
                "relatedness_policy": {
                    "version": "financial-related-news-policy-v1",
                    "project_keyword_gate_applied": False,
                    "target_type": "instrument" if scope.get("instrument_id") is not None else "fixed_market_index",
                    "identity_terms": (
                        "canonical_symbol_display_name_provider_mappings_and_aliases"
                        if scope.get("instrument_id") is not None
                        else "fixed_index_symbol_name_and_market_terms"
                    ),
                    "match_fields": ["title", "matched_keywords", "content", "official_stock_code"],
                    "publication_boundary": "not_after_request_time",
                    "recency_strategy": "configured_recent_window_then_latest_available",
                    "deduplication": "canonical_document_or_article_identity",
                    "document_boundary": "active_ingested_article_or_verified_official_metadata",
                },
            },
            "generated_at": utc_text(self.clock()),
        })

    def hide_dashboard_instrument(self, *, owner_user_id: object, instrument_id: int) -> dict:
        """Persist a per-user homepage projection exclusion for one non-index instrument."""

        owner = str(owner_user_id or "").strip()
        if not owner:
            raise ValueError("无法确认当前用户")
        normalized_instrument_id = coerce_int(instrument_id, 0, 1)
        with self.database.lock:
            row = self.connection.execute(
                """
                SELECT id, canonical_symbol, display_name, asset_type
                FROM financial_instruments
                WHERE id=?
                """,
                (normalized_instrument_id,),
            ).fetchone()
            if row is None:
                raise LookupError("证券标的不存在")
            symbol = str(row[1] or "").strip().upper()
            if str(row[3] or "").strip().casefold() == "index" or symbol in FIXED_OVERVIEW_SYMBOLS:
                raise ValueError("固定指数卡不能通过个股删除操作隐藏")
            self.connection.execute(
                """
                INSERT INTO financial_dashboard_hidden_instruments(
                    owner_user_id, instrument_id, canonical_symbol
                ) VALUES(?,?,?)
                ON CONFLICT(owner_user_id, instrument_id) DO UPDATE SET
                    canonical_symbol=excluded.canonical_symbol
                """,
                (owner, normalized_instrument_id, symbol),
            )
            self.connection.commit()
        return {
            "hidden": True,
            "instrument_id": normalized_instrument_id,
            "canonical_symbol": symbol,
            "display_name": str(row[2] or symbol),
        }

    def build(
        self,
        *,
        industry_pack_id: str,
        time_range: str = "7d",
        page: int = 1,
        per_page: int = 15,
        content_kind: str = "",
        viewer_user_id: object = "",
    ) -> dict:
        gate, capabilities = self._gate(industry_pack_id)
        page = coerce_int(page, 1, 1, 20)
        per_page = coerce_int(per_page, 15, 1, 50)
        selected_kind = str(content_kind or "").strip().casefold()
        if selected_kind and selected_kind not in CONTENT_KINDS:
            raise ValueError("content_kind 只支持 market_fact、source_document 或 research_opinion")
        now = self.clock()
        start, end = parse_time_range(time_range, now=now)
        window = {
            "time_range": str(time_range or "7d"),
            "from": utc_text(start),
            "to": utc_text(end),
            "timezone": "Asia/Hong_Kong",
            "clock_source": "application_server",
        }
        categories = list(CORE_CATEGORIES)
        if capabilities["effective"]["trading_agents"]:
            categories.extend(TRADINGAGENTS_CATEGORIES)
        if capabilities["effective"]["simulation"]:
            categories.append(dict(SIMULATION_CATEGORY))
        if not gate["enabled"]:
            return redact_public_payload({
                "feed_version": FINANCIAL_FEED_VERSION,
                "visible": False,
                "visibility_reason": gate["reason"],
                "industry_pack_id": gate["pack_id"],
                "effective_pack_ids": gate["effective_pack_ids"],
                "items": [],
                "market_overview": [],
                "counts": {kind: 0 for kind in CONTENT_KINDS},
                "total": 0,
                "page": page,
                "per_page": per_page,
                "total_pages": 0,
                "time_window": window,
                "categories": [],
                "effective_capabilities": capabilities["product"],
                "simulation_enabled": False,
                "availability": {
                    "status": "disabled",
                    "reason_codes": [gate["reason"]],
                    "message": "金融能力当前未启用。",
                    "checked_at": utc_text(now),
                },
            })

        offset = (page - 1) * per_page
        fetch_limit = offset + per_page
        start_text, end_text = utc_text(start), utc_text(end)
        items = []
        counts = {kind: 0 for kind in CONTENT_KINDS}
        with self.database.lock:
            keyword_snapshot = configured_project_keyword_snapshot(
                self.connection,
                pack_loader=self.pack_loader,
            )
            project_keywords = keyword_snapshot["keywords"]
            requested_pack_kind = str(
                (self.pack_loader or industry_pack_loader)
                .load(gate["pack_id"])
                .get("pack_kind")
                or "primary"
            )
            strict_financial_aggregation = (
                gate["pack_id"] not in {"family_office", "financial_markets"}
                and requested_pack_kind != "capability"
            )
            strict_project_keywords = (
                project_keywords if strict_financial_aggregation else None
            )
            dashboard = capabilities.get("dashboard_capabilities") or {}
            direct_financial_pack = gate["pack_id"] == "financial_markets"
            show_market_indices = bool(
                direct_financial_pack or dashboard.get("show_market_index_cards")
            )
            show_watched_stocks = bool(
                direct_financial_pack or dashboard.get("show_watched_stock_cards")
            )
            market_overview = (
                self._market_overview_cards(
                    project_keywords=strict_project_keywords
                ) if show_market_indices else []
            )
            if selected_kind in {"", "market_fact"} and show_watched_stocks:
                current, counts["market_fact"] = self._snapshot_rows(
                    start_text,
                    end_text,
                    fetch_limit,
                    viewer_user_id=str(viewer_user_id or ""),
                    project_keywords=strict_project_keywords,
                )
                items.extend(current)
            if (
                selected_kind in {"", "source_document"}
                and gate["financial_news_enabled"]
            ):
                current, counts["source_document"] = self._article_rows(
                    start_text,
                    end_text,
                    fetch_limit,
                    project_pack_id=gate["pack_id"],
                    project_keywords=project_keywords,
                    activation_id=keyword_snapshot["activation_id"],
                )
                items.extend(current)
            if selected_kind in {"", "research_opinion"} and capabilities["effective"]["trading_agents"]:
                current, counts["research_opinion"] = self._report_rows(
                    start_text,
                    end_text,
                    fetch_limit,
                    project_keywords=strict_project_keywords,
                )
                items.extend(current)
        items.sort(key=lambda item: (str(item.get("sort_time") or ""), str(item.get("item_id") or "")), reverse=True)
        total = sum(counts.values())
        page_items = items[offset : offset + per_page]
        for item in page_items:
            item.pop("sort_time", None)
        health = FinancialHealthService(
            self.database,
            settings=self.settings or config,
            clock=self.clock,
        ).build()
        availability = FinancialHealthService.public_availability(health)
        if total == 0 and health.get("status") == "healthy":
            availability = {
                **availability,
                "status": "empty",
                "reason_codes": ["empty_time_window"],
                "message": "当前时间窗暂无金融行情、资讯或已完成研究报告；数据链路正常。",
            }
        return redact_public_payload({
            "feed_version": FINANCIAL_FEED_VERSION,
            "visible": True,
            "visibility_reason": "enabled",
            "industry_pack_id": gate["pack_id"],
            "effective_pack_ids": gate["effective_pack_ids"],
            "items": page_items,
            "market_overview": market_overview,
            "counts": counts,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page,
            "time_window": window,
            "project_keyword_gate": {
                "source": "active_published_industry_pack",
                "industry_pack_id": keyword_snapshot["industry_pack_id"],
                "industry_pack_version_id": keyword_snapshot["industry_pack_version_id"],
                "activation_id": keyword_snapshot["activation_id"],
                "enabled": bool(project_keywords),
                "strict_for_financial_aggregation": strict_financial_aggregation,
                "family_office_exception": gate["pack_id"] == "family_office",
                "keywords": project_keywords,
                "classification_precedence": ["event", "trend", "financial_fallback"],
            },
            "dashboard_card_visibility": {
                "show_financial_news": bool(gate["financial_news_enabled"]),
                "show_market_index_cards": show_market_indices,
                "show_watched_stock_cards": show_watched_stocks,
            },
            "categories": categories,
            "effective_capabilities": capabilities["product"],
            "simulation_enabled": bool(capabilities["effective"]["simulation"]),
            "availability": availability,
        })
