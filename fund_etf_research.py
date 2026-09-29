#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Distinct ETF and open-end fund research templates on project-owned data.

This component is deliberately not a company-stock graph.  It orchestrates the
fund tools exposed by ``TradingAgentsCNDataAdapter`` and produces a JSON-safe
report that later report/verification stages can consume.  It creates no model,
database, vector-store, queue, service or port.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Mapping, Optional

from financial_instruments import InstrumentRegistry
from stock_research_graph import _snapshot_ids
from tradingagents_cn_data_adapter import TradingAgentsCNDataAdapter


FUND_ETF_RESEARCH_VERSION = "fund-etf-research-v1"
AVAILABLE_STATUSES = frozenset({"complete", "completed", "fetched", "cached", "limited"})


class FundETFResearchError(RuntimeError):
    """Stable routing/template failure without provider or credential details."""

    def __init__(self, message: str, *, error_code: str):
        super().__init__(message)
        self.error_code = str(error_code)


def resolve_fund_target(
    registry: InstrumentRegistry,
    query: str,
    *,
    as_of: date | str,
    asset_type: str = "",
    share_class: str = "",
    currency: str = "",
) -> Mapping[str, Any]:
    """Resolve a fund target without guessing an asset, share class or currency."""
    if asset_type and asset_type not in {"etf", "fund"}:
        raise FundETFResearchError(
            "基金研究标的类型必须为 etf 或 fund", error_code="unsupported_asset"
        )
    resolution = registry.resolve(
        query,
        asset_type=asset_type,
        share_class=share_class,
        currency=currency,
        as_of=as_of,
    )
    payload = resolution.to_dict()
    candidates = [
        item
        for item in payload["candidates"]
        if item.get("asset_type") in {"etf", "fund"}
    ]
    if not candidates:
        return {
            **payload,
            "status": "unsupported_asset" if payload["candidates"] else "not_found",
            "instrument_id": None,
            "candidates": candidates,
            "required_clarifications": [],
        }
    if len(candidates) > 1:
        clarifications = list(payload.get("required_clarifications") or [])
        if len({item.get("asset_type") for item in candidates}) > 1 and "asset_type" not in clarifications:
            clarifications.append("asset_type")
        if len({item.get("metadata", {}).get("share_class") for item in candidates}) > 1 and "share_class" not in clarifications:
            clarifications.append("share_class")
        if len({item.get("currency") for item in candidates}) > 1 and "currency" not in clarifications:
            clarifications.append("currency")
        return {
            **payload,
            "status": "clarification_required",
            "instrument_id": None,
            "candidates": candidates,
            "required_clarifications": clarifications or ["instrument"],
        }
    return {
        **payload,
        "status": "resolved",
        "instrument_id": int(candidates[0]["instrument_id"]),
        "candidates": candidates,
        "required_clarifications": [],
    }


def _json_tool(value: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FundETFResearchError(
            f"{label} 返回无效 JSON", error_code="invalid_tool_output"
        ) from exc
    if not isinstance(parsed, dict):
        raise FundETFResearchError(
            f"{label} 返回值必须为对象", error_code="invalid_tool_output"
        )
    return parsed


def _available(section: Mapping[str, Any]) -> bool:
    return str(section.get("status") or "").casefold() in AVAILABLE_STATUSES


class FundETFResearch:
    """Build one of two explicit, non-company fund research reports."""

    def __init__(
        self,
        connection,
        research_run_id: str,
        *,
        data_adapter: TradingAgentsCNDataAdapter,
    ):
        self.connection = connection
        self.research_run_id = str(research_run_id or "")
        self.data_adapter = data_adapter
        self.instruments = InstrumentRegistry(connection)
        row = connection.execute(
            "SELECT instrument_id, scope_type FROM financial_research_runs WHERE id=?",
            (self.research_run_id,),
        ).fetchone()
        if row is None:
            raise FundETFResearchError(
                "基金研究任务不存在", error_code="research_run_not_found"
            )
        if row[0] is None:
            raise FundETFResearchError(
                "基金研究要求单一标的", error_code="instrument_scope_required"
            )
        self.instrument = self.instruments.get(int(row[0]))
        if self.instrument is None:
            raise FundETFResearchError(
                "基金研究标的不存在", error_code="instrument_not_found"
            )
        if self.instrument.asset_type not in {"etf", "fund"}:
            raise FundETFResearchError(
                "该标的不支持基金研究模板", error_code="unsupported_asset"
            )
        context = getattr(data_adapter, "context", None)
        if str(getattr(context, "research_run_id", "")) != self.research_run_id:
            raise FundETFResearchError(
                "data adapter 研究任务不一致", error_code="research_run_scope_mismatch"
            )
        if int(getattr(context, "instrument_id", 0)) != self.instrument.instrument_id:
            raise FundETFResearchError(
                "data adapter 标的不一致", error_code="research_run_scope_mismatch"
            )

    @staticmethod
    def _call(function, label: str, *args) -> dict[str, Any]:
        return _json_tool(function(*args), label)

    def _etf_sections(self, symbol: str, curr_date: str) -> dict[str, Mapping[str, Any]]:
        return {
            "identity": self._call(self.data_adapter.get_fund_identity, "ETF identity", symbol),
            "market_history": self._call(
                self.data_adapter.get_stock_data,
                "ETF market history",
                symbol,
                (date.fromisoformat(curr_date) - timedelta(days=370)).isoformat(),
                curr_date,
            ),
            "market_snapshot": self._call(
                self.data_adapter.get_verified_market_snapshot,
                "ETF market snapshot",
                symbol,
                curr_date,
                30,
            ),
            "disclosed_nav": self._call(
                self.data_adapter.get_fund_nav, "ETF NAV", symbol, curr_date, 30
            ),
            "tracking": self._call(
                self.data_adapter.get_etf_tracking, "ETF tracking", symbol, curr_date, 60
            ),
            "tracked_index_constituents": self._call(
                self.data_adapter.get_etf_constituents,
                "ETF tracked constituents",
                symbol,
                curr_date,
            ),
            "fees": self._call(self.data_adapter.get_fund_fees, "ETF fees", symbol, curr_date),
            "liquidity": self._call(
                self.data_adapter.get_etf_liquidity,
                "ETF liquidity",
                symbol,
                curr_date,
                20,
            ),
        }

    def _fund_sections(self, symbol: str, curr_date: str) -> dict[str, Mapping[str, Any]]:
        return {
            "identity": self._call(self.data_adapter.get_fund_identity, "fund identity", symbol),
            "profile_and_benchmark": self._call(
                self.data_adapter.get_fund_profile, "fund profile", symbol, curr_date
            ),
            "nav": self._call(self.data_adapter.get_fund_nav, "fund NAV", symbol, curr_date, 30),
            "share_records": self._call(
                self.data_adapter.get_fund_share, "fund share", symbol, curr_date, 365
            ),
            "holdings_disclosure": self._call(
                self.data_adapter.get_fund_holdings, "fund holdings", symbol, curr_date
            ),
            "manager": self._call(
                self.data_adapter.get_fund_manager, "fund manager", symbol, curr_date
            ),
            "fees": self._call(self.data_adapter.get_fund_fees, "fund fees", symbol, curr_date),
            "subscription_redemption": self._call(
                self.data_adapter.get_fund_subscription_redemption,
                "fund subscription/redemption",
                symbol,
                curr_date,
            ),
        }

    def build_report(self, curr_date: str) -> Mapping[str, Any]:
        try:
            requested_day = date.fromisoformat(str(curr_date or ""))
        except ValueError as exc:
            raise FundETFResearchError(
                "curr_date 必须为 YYYY-MM-DD", error_code="invalid_research_date"
            ) from exc
        symbol = self.instrument.canonical_symbol
        is_etf = self.instrument.asset_type == "etf"
        sections = (
            self._etf_sections(symbol, requested_day.isoformat())
            if is_etf
            else self._fund_sections(symbol, requested_day.isoformat())
        )
        external_sections = [value for key, value in sections.items() if key != "identity"]
        available_count = sum(_available(item) for item in external_sections)
        unavailable = [key for key, value in sections.items() if not _available(value)]
        required_key = "market_snapshot" if is_etf else "nav"
        if available_count == 0 or not _available(sections[required_key]):
            status = "insufficient_data"
        elif unavailable:
            status = "limited"
        else:
            status = "complete"
        latest_nav_date: Optional[str] = None
        nav_section = sections.get("disclosed_nav") or sections.get("nav") or {}
        if isinstance(nav_section, Mapping):
            latest_nav_date = nav_section.get("latest_disclosed_nav_date")
        coverage = available_count / len(external_sections) if external_sections else 0.0
        report = {
            "schema_version": 1,
            "component_version": FUND_ETF_RESEARCH_VERSION,
            "research_run_id": self.research_run_id,
            "report_type": "etf_research" if is_etf else "open_end_fund_research",
            "template": "etf" if is_etf else "open_end_fund",
            "status": status,
            "error_code": "insufficient_data" if status == "insufficient_data" else None,
            "target": self.instrument.to_dict(),
            "requested_date": requested_day.isoformat(),
            "share_class": self.instrument.share_class or None,
            "currency": self.instrument.currency,
            "sections": sections,
            "section_order": list(sections),
            "missing_sections": unavailable,
            "evidence_coverage": round(coverage, 6),
            "snapshot_ids": _snapshot_ids(sections),
            "latest_disclosed_nav_date": latest_nav_date,
            "valuation_semantics": (
                "exchange_price plus disclosed NAV; tracking and liquidity are derived research metrics"
                if is_etf
                else "last disclosed NAV; no intraday price or transaction assumption"
            ),
            "company_fundamentals_used": False,
            "company_statement_sections": [],
            "intraday_trade_assumption_used": False,
            "execution_target_created": False,
            "disclosure_lag_applies": True,
            "boundary": (
                "ETF constituents mean controlled benchmark constituents, not a live creation basket."
                if is_etf
                else "Open-end fund holdings, manager and share data are disclosures, not live positions."
            ),
        }
        return json.loads(json.dumps(report, ensure_ascii=False, allow_nan=False))
