#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""One authoritative capability gate for financial features."""

from __future__ import annotations

from collections.abc import Mapping

import config
from financial_rollout import financial_rollout_state, rollout_capability_reason


CAPABILITY_FLAGS = {
    "financial_intelligence": "FINANCIAL_INTELLIGENCE_ENABLED",
    "trading_agents": "TRADING_AGENTS_ENABLED",
    "auto_research": "FINANCIAL_AUTO_RESEARCH_ENABLED",
    "simulation": "TRADING_SIMULATION_ENABLED",
    "akshare_cn": "AKSHARE_CN_ENABLED",
    "tushare_cn": "TUSHARE_CN_ENABLED",
    "yahoo": "YAHOO_FINANCE_ENABLED",
    "alpha_vantage": "ALPHA_VANTAGE_ENABLED",
    "fred": "FRED_ENABLED",
    "polymarket": "POLYMARKET_ENABLED",
    "easyquotation": "EASYQUOTATION_ENABLED",
    "official_evidence": "OFFICIAL_FINANCIAL_EVIDENCE_ENABLED",
}

CAPABILITY_SECRET_KEYS = {
    "tushare_cn": "TUSHARE_TOKEN",
    "alpha_vantage": "ALPHA_VANTAGE_API_KEY",
    "fred": "FRED_API_KEY",
}

PRODUCT_CAPABILITY_SCHEMA_VERSION = "financial-product-capabilities-v1"
PRODUCT_CAPABILITY_KEYS = (
    "financial_zone",
    "tradingagents_reports",
    "simulation",
    "backtesting",
)


class FinancialCapabilityDisabled(PermissionError):
    def __init__(self, capability: str, reason: str):
        self.capability = str(capability)
        self.reason = str(reason)
        super().__init__(f"金融能力未启用: {self.capability} ({self.reason})")


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _value(settings, name: str, default=None):
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


def financial_capabilities(settings=None) -> dict:
    source = config if settings is None else settings
    rollout = financial_rollout_state(source)
    configured = {
        name: _bool(_value(source, flag, False))
        for name, flag in CAPABILITY_FLAGS.items()
    }
    secret_configured = {
        capability: bool(str(_value(source, secret_key, "") or "").strip())
        for capability, secret_key in CAPABILITY_SECRET_KEYS.items()
    }
    financial_flag = configured["financial_intelligence"]
    financial = financial_flag and rollout["capabilities"]["snapshot_readonly"]
    trading_agents_flag = financial_flag and configured["trading_agents"]
    trading_agents = (
        trading_agents_flag and rollout["capabilities"]["stock_research"]
    )
    effective = {
        "financial_intelligence": financial,
        "trading_agents": trading_agents,
        "auto_research": (
            trading_agents
            and configured["auto_research"]
            and rollout["capabilities"]["auto_research"]
        ),
        "simulation": (
            financial
            and configured["simulation"]
            and rollout["capabilities"]["simulation_backtest"]
        ),
        "akshare_cn": financial and configured["akshare_cn"],
        "tushare_cn": (
            financial and configured["tushare_cn"] and secret_configured["tushare_cn"]
        ),
        "yahoo": financial and configured["yahoo"],
        "alpha_vantage": (
            financial
            and configured["alpha_vantage"]
            and secret_configured["alpha_vantage"]
        ),
        "fred": financial and configured["fred"] and secret_configured["fred"],
        "polymarket": financial and configured["polymarket"],
        "easyquotation": financial and configured["easyquotation"],
        "official_evidence": financial and configured["official_evidence"],
    }
    reasons = {}
    for capability, is_effective in effective.items():
        if is_effective:
            reasons[capability] = "enabled"
        elif not financial_flag:
            reasons[capability] = "financial_intelligence_disabled"
        elif capability == "financial_intelligence":
            reasons[capability] = rollout_capability_reason(
                "snapshot_readonly", source
            )
        elif capability in CAPABILITY_SECRET_KEYS and not configured[capability]:
            reasons[capability] = f"{CAPABILITY_FLAGS[capability].casefold()}_disabled"
        elif capability in CAPABILITY_SECRET_KEYS and not secret_configured[capability]:
            reasons[capability] = f"{CAPABILITY_SECRET_KEYS[capability].casefold()}_missing"
        elif capability in {
            "akshare_cn", "yahoo", "polymarket", "easyquotation",
            "official_evidence",
        } and not configured[capability]:
            reasons[capability] = f"{CAPABILITY_FLAGS[capability].casefold()}_disabled"
        elif capability in {
            "akshare_cn", "tushare_cn", "yahoo", "alpha_vantage", "fred",
            "polymarket", "easyquotation", "official_evidence",
        } and not rollout["capabilities"]["snapshot_readonly"]:
            reasons[capability] = rollout_capability_reason(
                "snapshot_readonly", source
            )
        elif capability in {"trading_agents", "auto_research"} and not configured["trading_agents"]:
            reasons[capability] = "trading_agents_disabled"
        elif capability == "auto_research" and not configured["auto_research"]:
            reasons[capability] = f"{CAPABILITY_FLAGS[capability].casefold()}_disabled"
        elif capability == "trading_agents" and not rollout["capabilities"]["stock_research"]:
            reasons[capability] = rollout_capability_reason("stock_research", source)
        elif capability == "auto_research" and not rollout["capabilities"]["auto_research"]:
            reasons[capability] = rollout_capability_reason("auto_research", source)
        elif capability == "simulation" and not configured["simulation"]:
            reasons[capability] = f"{CAPABILITY_FLAGS[capability].casefold()}_disabled"
        elif capability == "simulation" and not rollout["capabilities"]["simulation_backtest"]:
            reasons[capability] = rollout_capability_reason(
                "simulation_backtest", source
            )
        else:
            reasons[capability] = f"{CAPABILITY_FLAGS[capability].casefold()}_disabled"
    return {
        "configured": configured,
        "effective": effective,
        "reasons": reasons,
        "rollout": rollout,
        "tushare_token_configured": secret_configured["tushare_cn"],
        "alpha_vantage_api_key_configured": secret_configured["alpha_vantage"],
        "fred_api_key_configured": secret_configured["fred"],
    }


def financial_product_capabilities(
    industry_pack_id: str = "",
    *,
    settings=None,
    pack_loader=None,
) -> dict:
    """Project configured flags through the effective industry-pack graph.

    This is the product/UI/API/worker gate. ``financial_capabilities`` remains
    the low-level flag/secret parent-chain calculation used by providers.
    """

    if pack_loader is None:
        from industry_packs import industry_pack_loader

        pack_loader = industry_pack_loader
    source = config if settings is None else settings
    normalized_pack = str(
        industry_pack_id
        or _value(source, "INTEL_DEFAULT_INDUSTRY_PACK", "")
        or getattr(config, "INTEL_DEFAULT_INDUSTRY_PACK", "family_office")
        or "family_office"
    ).strip()
    state = financial_capabilities(source)
    rollout = state["rollout"]
    effective_pack_ids = []
    pack_capability_keys = set()
    dashboard_capabilities = {
        "show_financial_news": False,
        "show_market_index_cards": False,
        "show_watched_stock_cards": False,
        "show_spatiotemporal_map": True,
    }
    pack_reason = "enabled"
    try:
        composed = pack_loader.compose(normalized_pack)
        effective_pack_ids = [str(item) for item in composed.get("effective_pack_ids") or []]
        pack_capability_keys = {
            str(item.get("key") or "")
            for item in composed.get("capabilities") or []
            if isinstance(item, Mapping)
        }
        declared_dashboard_capabilities = composed.get("dashboard_capabilities") or {}
        if isinstance(declared_dashboard_capabilities, Mapping):
            dashboard_capabilities = {
                key: bool(
                    declared_dashboard_capabilities.get(
                        key,
                        key == "show_spatiotemporal_map",
                    )
                )
                for key in dashboard_capabilities
            }
    except Exception:
        pack_reason = "invalid_or_disabled_industry_pack"
    pack_has_finance = "financial_market_data" in pack_capability_keys
    if pack_reason == "enabled" and not pack_has_finance:
        pack_reason = "financial_capability_not_in_effective_pack"
    pack_financial_products_enabled = bool(
        pack_has_finance
        and (
            normalized_pack == "financial_markets"
            or dashboard_capabilities["show_market_index_cards"]
            or dashboard_capabilities["show_watched_stock_cards"]
        )
    )
    product_pack_reason = pack_reason
    if pack_reason == "enabled" and not pack_financial_products_enabled:
        product_pack_reason = "financial_products_hidden_for_primary_pack"

    effective = {
        key: bool(value and pack_financial_products_enabled)
        for key, value in state["effective"].items()
    }
    reasons = dict(state["reasons"])
    if not pack_financial_products_enabled:
        for key in reasons:
            reasons[key] = product_pack_reason

    product = {
        "financial_zone": bool(
            state["configured"]["financial_intelligence"]
            and rollout["capabilities"]["dashboard"]
            and pack_financial_products_enabled
        ),
        "tradingagents_reports": effective["trading_agents"],
        "simulation": effective["simulation"],
        "backtesting": effective["simulation"],
    }
    product_reasons = {
        "financial_zone": (
            "enabled"
            if product["financial_zone"]
            else product_pack_reason
            if not pack_financial_products_enabled
            else "financial_intelligence_disabled"
            if not state["configured"]["financial_intelligence"]
            else rollout_capability_reason("dashboard", source)
        ),
        "tradingagents_reports": reasons["trading_agents"],
        "simulation": reasons["simulation"],
        "backtesting": reasons["simulation"],
    }
    return {
        **state,
        "schema_version": PRODUCT_CAPABILITY_SCHEMA_VERSION,
        "industry_pack_id": normalized_pack,
        "effective_pack_ids": effective_pack_ids,
        "pack_has_financial_markets": pack_has_finance,
        "pack_financial_products_enabled": pack_financial_products_enabled,
        "pack_capability_keys": sorted(pack_capability_keys),
        "dashboard_capabilities": dashboard_capabilities,
        "effective": effective,
        "reasons": reasons,
        "product": product,
        "product_reasons": product_reasons,
        "running_task_policy": "finish_claimed_skip_queued_and_new_preserve_history",
    }


def require_financial_product_capability(
    capability: str,
    industry_pack_id: str = "",
    *,
    settings=None,
    pack_loader=None,
) -> dict:
    normalized = str(capability or "").strip()
    if normalized not in PRODUCT_CAPABILITY_KEYS:
        raise ValueError(f"未知金融产品能力: {normalized}")
    state = financial_product_capabilities(
        industry_pack_id,
        settings=settings,
        pack_loader=pack_loader,
    )
    if not state["product"][normalized]:
        raise FinancialCapabilityDisabled(
            normalized,
            state["product_reasons"][normalized],
        )
    return state


def require_financial_capability(capability: str, settings=None) -> dict:
    normalized = str(capability or "").strip()
    if normalized not in CAPABILITY_FLAGS:
        raise ValueError(f"未知金融能力: {normalized}")
    state = financial_capabilities(settings)
    if not state["effective"][normalized]:
        raise FinancialCapabilityDisabled(normalized, state["reasons"][normalized])
    return state
