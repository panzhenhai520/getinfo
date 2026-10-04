#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unified multi-LLM, proxy and research-policy settings service."""

from __future__ import annotations

from typing import Mapping
from urllib.parse import urlsplit

from qa_observability import QaAuditLogger, probe_provider, probe_proxy
from qa_policy import QaPolicyResolver
from qa_provider_registry import QaProviderRegistry
from qa_security import QaSecurityError, validate_proxy_url


class QaSettingsError(ValueError):
    pass


def pack_user_id(owner_user_id: str) -> int:
    owner = str(owner_user_id or "")
    if not owner.startswith("pack:"):
        return 0
    try:
        return max(0, int(owner.split(":", 1)[1]))
    except (TypeError, ValueError):
        return 0


class QaSettingsService:
    def __init__(self, database, *, provider_registry=None, policy_resolver=None):
        self.database = database
        self.provider_registry = provider_registry or QaProviderRegistry()
        self.policy_resolver = policy_resolver or QaPolicyResolver()
        self.audit = QaAuditLogger(database)

    @staticmethod
    def _model_ids() -> list[str]:
        from chat_api import MODEL_META

        return list(MODEL_META)

    def get(self, *, owner_user_id: str, industry_pack_id: str, is_admin: bool = False) -> dict:
        from pack_tenant import get_user_llm_keys, get_user_settings

        user_id = pack_user_id(owner_user_id)
        keys = get_user_llm_keys(user_id) if user_id else {}
        saved = get_user_settings(user_id) if user_id else {}
        providers = []
        for provider_id in self._model_ids():
            try:
                profile = self.provider_registry.resolve(
                    "draft", provider_id, owner_user_id=owner_user_id,
                    industry_pack_id=industry_pack_id,
                )
                providers.append({
                    "provider_id": provider_id, "display_name": profile.name,
                    "model_id": profile.model_id, "configured": bool(
                        profile.base_url and profile.model_id and (profile.api_key or provider_id == "local")
                    ),
                    "has_key": bool(keys.get(provider_id) or (provider_id == "local" and profile.api_key)),
                    "use_proxy": bool(profile.use_proxy),
                })
            except Exception:
                providers.append({
                    "provider_id": provider_id, "display_name": provider_id,
                    "model_id": "", "configured": False, "has_key": False, "use_proxy": False,
                })
        proxy_raw = str(saved.get("proxy_http") or "")
        parts = urlsplit(proxy_raw) if proxy_raw else None
        policy = self.policy_resolver.resolve(industry_pack_id)
        response = {
            "success": True, "owner_user_id": owner_user_id,
            "industry_pack_id": industry_pack_id, "can_edit_user_settings": bool(user_id),
            "can_manage_ragflow": bool(is_admin), "providers": providers,
            "network": {
                "proxy_configured": bool(proxy_raw),
                "proxy_scheme": parts.scheme if parts else "",
                "proxy_host": parts.hostname if parts else "",
                "proxy_port": parts.port if parts else None,
            },
            "local_llm": {
                "provider": str(saved.get("llm_provider") or ""),
                "model": str(saved.get("llm_model") or ""),
                "timeout": int(saved.get("llm_timeout") or 0),
                "endpoint_configured": bool(saved.get("llm_base_url")),
            },
            "policy": {
                "default_mode": policy.default_mode,
                "standard_max_hops": policy.standard_max_hops,
                "deep_max_hops": policy.deep_max_hops,
                "research_timeout_seconds": policy.research_timeout_seconds,
            },
            "ragflow": {
                "configured": bool(policy.ragflow_app_id and policy.ragflow_kb_id),
                "assistant_id": policy.ragflow_app_id if is_admin else "",
                "kb_id": policy.ragflow_kb_id if is_admin else "",
            },
        }
        return response

    def save(self, payload: Mapping, *, owner_user_id: str, industry_pack_id: str) -> dict:
        from pack_tenant import (
            clear_user_llm_key, get_user_settings, set_user_llm_key, set_user_settings,
        )

        user_id = pack_user_id(owner_user_id)
        if not user_id:
            raise QaSettingsError("管理员不能保存用户级模型密钥；请使用行业用户或管理员全局配置。")
        allowed = set(self._model_ids())
        keys = payload.get("keys") if isinstance(payload.get("keys"), Mapping) else {}
        clear_keys = payload.get("clear_keys") if isinstance(payload.get("clear_keys"), list) else []
        for provider_id, raw in keys.items():
            provider = str(provider_id).casefold()
            if provider not in allowed:
                raise QaSettingsError("未知模型提供商。")
            value = str(raw or "").strip()
            if value:
                if len(value) > 4096:
                    raise QaSettingsError("API Key 长度异常。")
                set_user_llm_key(user_id, provider, value)
        for provider_id in clear_keys:
            provider = str(provider_id).casefold()
            if provider in allowed:
                clear_user_llm_key(user_id, provider)

        prior = get_user_settings(user_id)
        proxy_present = "proxy_http" in payload
        proxy = str(payload.get("proxy_http") or "").strip() if proxy_present else str(prior.get("proxy_http") or "")
        if proxy:
            proxy = validate_proxy_url(proxy)
        provider = str(payload.get("llm_provider", prior.get("llm_provider") or "")).strip().casefold()
        if provider not in {"", "ollama", "openai", "openrouter", "vpn"}:
            raise QaSettingsError("本地模型提供商无效。")
        model = str(payload.get("llm_model", prior.get("llm_model") or "")).strip()[:200]
        try:
            timeout = int(payload.get("llm_timeout", prior.get("llm_timeout") or 0) or 0)
        except (TypeError, ValueError) as exc:
            raise QaSettingsError("模型超时必须为整数秒。") from exc
        timeout = max(0, min(timeout, 600))
        # Arbitrary endpoints are deliberately not accepted from user payloads.
        base_url = str(prior.get("llm_base_url") or "")
        set_user_settings(
            user_id, proxy_http=proxy, llm_provider=provider,
            llm_base_url=base_url, llm_model=model, llm_timeout=timeout,
        )
        self.audit.record(
            "settings_changed", actor_id=owner_user_id, origin="settings",
            industry_pack_id=industry_pack_id,
            payload={
                "providers_updated": sorted(str(item) for item in keys),
                "providers_cleared": sorted(str(item) for item in clear_keys),
                "proxy_configured": bool(proxy), "local_model": model,
            },
        )
        return self.get(owner_user_id=owner_user_id, industry_pack_id=industry_pack_id)

    def probe_provider(self, provider_id: str, *, owner_user_id: str, industry_pack_id: str) -> dict:
        provider = str(provider_id or "").casefold()
        if provider not in set(self._model_ids()):
            raise QaSettingsError("未知模型提供商。")
        profile = self.provider_registry.resolve(
            "draft", provider, owner_user_id=owner_user_id, industry_pack_id=industry_pack_id,
        )
        if provider != "local" and not profile.api_key:
            return {"ready": False, "provider_id": provider, "code": "API_KEY_MISSING", "message": "请先保存 API Key。"}
        return probe_provider(profile)

    def probe_network(self, *, owner_user_id: str) -> dict:
        from pack_tenant import get_user_settings

        user_id = pack_user_id(owner_user_id)
        proxy = str((get_user_settings(user_id) if user_id else {}).get("proxy_http") or "")
        return probe_proxy(proxy)


__all__ = ["QaSettingsError", "QaSettingsService", "pack_user_id"]
