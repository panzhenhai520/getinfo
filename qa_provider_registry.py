#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Role-aware provider resolution for unified QA."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from qa_policy import QaPolicy


QA_MODEL_ROLES = ("draft", "synthesis")


@dataclass(frozen=True)
class QaProviderProfile:
    role: str
    provider_id: str
    name: str
    provider_type: str
    base_url: str
    model_id: str
    api_key: str
    use_proxy: bool

    def public_dict(self) -> dict:
        return {
            "role": self.role,
            "provider_id": self.provider_id,
            "name": self.name,
            "provider_type": self.provider_type,
            "base_url": self.base_url if self.provider_id == "local" else "",
            "model_id": self.model_id,
            "has_key": bool(self.api_key),
            "use_proxy": self.use_proxy,
        }


class QaProviderRegistry:
    def __init__(self, runtime_loader: Callable[[str], Mapping] | None = None):
        self.runtime_loader = runtime_loader

    def _load(self, provider_id: str, *, owner_user_id: str = "", industry_pack_id: str = "") -> dict:
        if self.runtime_loader is not None:
            try:
                return dict(self.runtime_loader(provider_id, owner_user_id, industry_pack_id) or {})
            except TypeError:
                return dict(self.runtime_loader(provider_id) or {})
        from chat_api import get_chat_model_runtime_config

        runtime = dict(get_chat_model_runtime_config(provider_id))
        owner = str(owner_user_id or "")
        if owner.startswith("pack:"):
            try:
                from pack_tenant import get_pack_remote_config, get_user_llm_keys, get_user_settings

                user_id = int(owner.split(":", 1)[1])
                keys = get_user_llm_keys(user_id)
                if keys.get(provider_id):
                    runtime["api_key"] = str(keys[provider_id]).strip()
                if provider_id == "local":
                    settings = get_user_settings(user_id)
                    remote = get_pack_remote_config(industry_pack_id) if industry_pack_id else {}
                    runtime["base_url"] = str(settings.get("llm_base_url") or remote.get("llm_base_url") or runtime.get("base_url") or "").rstrip("/")
                    runtime["model_id"] = str(settings.get("llm_model") or remote.get("llm_model") or runtime.get("model_id") or "")
            except Exception:
                pass
        return runtime

    def resolve(self, role: str, provider_id: str, *, owner_user_id: str = "", industry_pack_id: str = "") -> QaProviderProfile:
        normalized_role = str(role or "").strip().casefold()
        if normalized_role not in QA_MODEL_ROLES:
            raise ValueError(f"unknown QA model role: {normalized_role}")
        normalized_provider = str(provider_id or "").strip().casefold()
        runtime = self._load(normalized_provider, owner_user_id=owner_user_id, industry_pack_id=industry_pack_id)
        actual = str(runtime.get("provider_id") or normalized_provider).strip().casefold()
        if actual != normalized_provider:
            raise ValueError("provider runtime identity mismatch")
        return QaProviderProfile(
            role=normalized_role,
            provider_id=actual,
            name=str(runtime.get("name") or actual),
            provider_type=str(runtime.get("type") or "openai"),
            base_url=str(runtime.get("base_url") or "").rstrip("/"),
            model_id=str(runtime.get("model_id") or ""),
            api_key=str(runtime.get("api_key") or ""),
            use_proxy=bool(runtime.get("use_proxy", False)),
        )

    def resolve_run_roles(self, draft_provider_id: str, policy: QaPolicy, *, owner_user_id: str = "") -> dict[str, QaProviderProfile]:
        return {
            "draft": self.resolve("draft", draft_provider_id, owner_user_id=owner_user_id, industry_pack_id=policy.industry_pack_id),
            "synthesis": self.resolve("synthesis", policy.synthesis_provider_id, owner_user_id=owner_user_id, industry_pack_id=policy.industry_pack_id),
        }


__all__ = ["QA_MODEL_ROLES", "QaProviderProfile", "QaProviderRegistry"]
