#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Versioned industry-pack loading and text normalization."""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import unicodedata
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlparse, urlsplit, urlunsplit

import config
from intel_contracts import DEFAULT_INDUSTRY_PACK_ID, INTERNAL_CATEGORIES
from source_authority import SourceAuthorityError, normalize_manifest_source

try:
    from opencc import OpenCC
except Exception:  # pragma: no cover - optional runtime dependency fallback
    OpenCC = None


PACK_ID_PATTERN = re.compile(r"^[a-z0-9_]+$")
KEYWORD_FIELDS = (
    "core_keywords",
    "expanded_keywords",
    "trend_keywords",
    "event_keywords",
    "negative_keywords",
)
REQUIRED_FIELDS = (
    "id",
    "name",
    "schema_version",
    "pack_version",
    "enabled",
    "default_market",
    "timezone",
    *KEYWORD_FIELDS,
    "classification",
    "serpapi_queries",
    "default_sources",
    "fixed_topics",
)
CLASSIFICATION_FIELDS = (
    "core_weight",
    "expanded_weight",
    "trend_weight",
    "event_weight",
    "negative_weight",
    "minimum_relevance_score",
    "llm_confidence_threshold",
    "tie_break_order",
)
CLASSIFICATION_WINDOW_FIELDS = (
    "recent_today_window_days",
    "recent_trend_window_days",
)
DEFAULT_CLASSIFICATION_WINDOWS = {
    "recent_today_window_days": 5,
    "recent_trend_window_days": 21,
}
SUPPORTED_SCHEMA_VERSIONS = {1, 2, 3}
PACK_KINDS = {"primary", "capability", "hybrid"}
DASHBOARD_CAPABILITY_DEFAULTS = {
    "show_financial_news": False,
    "show_market_index_cards": False,
    "show_watched_stock_cards": False,
    # 新建行业包默认走资讯流首页；时空信息图需要显式写 true 才开启。
    # （旧默认 True 会让所有新包一建好就进地图模式，与现有资讯类包的形态不一致）
    "show_spatiotemporal_map": False,
}
DASHBOARD_CAPABILITY_FIELDS = tuple(DASHBOARD_CAPABILITY_DEFAULTS)
SHARED_FINANCIAL_PACK_ID = "financial_markets"
RAGFLOW_KNOWLEDGE_BASE_KEYS = {"news"}


class IndustryPackError(ValueError):
    pass


_opencc = OpenCC("t2s") if OpenCC else None


def normalize_intel_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    if _opencc:
        try:
            text = _opencc.convert(text)
        except Exception:
            pass
    return " ".join(text.casefold().split())


def unique_normalized_keywords(values: Iterable[str]) -> List[str]:
    result = []
    seen = set()
    for value in values or []:
        clean = str(value or "").strip()
        normalized = normalize_intel_text(clean)
        if clean and normalized and normalized not in seen:
            result.append(clean)
            seen.add(normalized)
    return result


def industry_anchor_keywords(pack: Dict) -> List[str]:
    """Return the terms that establish the article belongs to an industry.

    Trend/event terms describe *what happened*; they must never be enough to
    establish *which industry* an article belongs to.
    """
    gate = pack.get("candidate_gate") or {}
    anchors = gate.get("anchor_keywords") if isinstance(gate, dict) else None
    entity_keywords = gate.get("entity_keywords") if isinstance(gate, dict) else None
    return unique_normalized_keywords(
        (list(anchors or []) + list(entity_keywords or [])) if isinstance(anchors, list) and anchors else (
            list(pack.get("core_keywords") or []) + list(pack.get("expanded_keywords") or [])
        )
    )


def _validate_http_url(value: str, label: str) -> None:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise IndustryPackError(f"{label} must be an absolute HTTP(S) URL")


def _validate_composable_fields(pack: Dict) -> None:
    schema_version = int(pack["schema_version"])
    if schema_version == 1:
        # Frozen v1 files remain readable.  These defaults are additive and do
        # not change the historical classification fields.
        pack.setdefault("pack_kind", "primary")
        pack.setdefault("includes", [])
        pack.setdefault("capabilities", [])
        pack.setdefault("dashboard_categories", [])
        pack.setdefault(
            "dashboard_capabilities",
            dict(DASHBOARD_CAPABILITY_DEFAULTS),
        )
        return

    required = ("pack_kind", "includes", "capabilities", "dashboard_categories")
    if schema_version >= 3:
        required += ("dashboard_capabilities",)
    missing = [field for field in required if field not in pack]
    if missing:
        raise IndustryPackError(
            f"schema v{schema_version} missing fields: {', '.join(missing)}"
        )

    if pack.get("pack_kind") not in PACK_KINDS:
        raise IndustryPackError("pack_kind must be primary, capability or hybrid")

    includes = pack.get("includes")
    if not isinstance(includes, list):
        raise IndustryPackError("includes must be an array")
    normalized_includes = []
    included_ids = set()
    for index, include in enumerate(includes):
        spec = {"pack_id": include, "required": True} if isinstance(include, str) else include
        if not isinstance(spec, dict):
            raise IndustryPackError(f"includes[{index}] must be a pack id or object")
        included_id = str(spec.get("pack_id") or "")
        if not PACK_ID_PATTERN.fullmatch(included_id):
            raise IndustryPackError(f"includes[{index}].pack_id is invalid")
        if included_id in included_ids:
            raise IndustryPackError(f"includes contains duplicated pack: {included_id}")
        if not isinstance(spec.get("required", True), bool):
            raise IndustryPackError(f"includes[{index}].required must be boolean")
        included_ids.add(included_id)
        normalized = copy.deepcopy(spec)
        normalized["pack_id"] = included_id
        normalized["required"] = spec.get("required", True)
        normalized_includes.append(normalized)
    pack["includes"] = normalized_includes

    if schema_version >= 3 and pack.get("pack_kind") == "primary":
        financial_dependency = next(
            (
                item
                for item in normalized_includes
                if item["pack_id"] == SHARED_FINANCIAL_PACK_ID
            ),
            None,
        )
        if not financial_dependency or not financial_dependency.get("required", True):
            raise IndustryPackError(
                "schema v3 primary pack must require financial_markets"
            )

    for field in ("capabilities", "dashboard_categories"):
        values = pack.get(field)
        if not isinstance(values, list):
            raise IndustryPackError(f"{field} must be an array")
        keys = set()
        normalized_values = []
        for index, value in enumerate(values):
            if not isinstance(value, dict):
                raise IndustryPackError(f"{field}[{index}] must be an object")
            key = str(value.get("key") or "")
            if not PACK_ID_PATTERN.fullmatch(key) or key in keys:
                raise IndustryPackError(f"{field}[{index}].key is invalid or duplicated")
            if not str(value.get("name") or "").strip():
                raise IndustryPackError(f"{field}[{index}].name is required")
            if not isinstance(value.get("enabled"), bool):
                raise IndustryPackError(f"{field}[{index}].enabled must be boolean")
            activation_flag = value.get("activation_flag")
            if activation_flag is not None and not isinstance(activation_flag, str):
                raise IndustryPackError(f"{field}[{index}].activation_flag must be a string")
            keys.add(key)
            normalized_values.append(copy.deepcopy(value))
        pack[field] = normalized_values

    if schema_version < 3:
        pack.setdefault(
            "dashboard_capabilities",
            dict(DASHBOARD_CAPABILITY_DEFAULTS),
        )
    dashboard_capabilities = pack.get("dashboard_capabilities")
    if not isinstance(dashboard_capabilities, dict):
        raise IndustryPackError("dashboard_capabilities must be an object")
    unknown_dashboard_capabilities = sorted(
        set(dashboard_capabilities) - set(DASHBOARD_CAPABILITY_FIELDS)
    )
    if unknown_dashboard_capabilities:
        raise IndustryPackError(
            "dashboard_capabilities contains unsupported fields: "
            + ", ".join(unknown_dashboard_capabilities)
        )
    for field in DASHBOARD_CAPABILITY_FIELDS:
        value = dashboard_capabilities.get(field, DASHBOARD_CAPABILITY_DEFAULTS[field])
        if not isinstance(value, bool):
            raise IndustryPackError(
                f"dashboard_capabilities.{field} must be boolean"
            )
        dashboard_capabilities[field] = value
    pack["dashboard_capabilities"] = dashboard_capabilities


def _validate_ragflow_policy(pack: Dict) -> None:
    """Normalize the per-industry knowledge upload policy.

    Published manifests created before this setting existed remain readable.
    Family office preserves the historical News upload behavior; every other
    industry fails closed until an administrator explicitly enables it.
    """

    default_enabled = str(pack.get("id") or "") == DEFAULT_INDUSTRY_PACK_ID
    policy = pack.get("ragflow_policy")
    if policy is None:
        policy = {}
    if not isinstance(policy, dict):
        raise IndustryPackError("ragflow_policy must be an object")
    unknown = sorted(set(policy) - {"upload_crawled_articles", "knowledge_base_key"})
    if unknown:
        raise IndustryPackError(
            "ragflow_policy contains unsupported fields: " + ", ".join(unknown)
        )
    enabled = policy.get("upload_crawled_articles", default_enabled)
    if not isinstance(enabled, bool):
        raise IndustryPackError(
            "ragflow_policy.upload_crawled_articles must be boolean"
        )
    knowledge_base_key = str(policy.get("knowledge_base_key") or "news").strip()
    if knowledge_base_key not in RAGFLOW_KNOWLEDGE_BASE_KEYS:
        raise IndustryPackError(
            "ragflow_policy.knowledge_base_key currently only supports news"
        )
    pack["ragflow_policy"] = {
        "upload_crawled_articles": enabled,
        "knowledge_base_key": knowledge_base_key,
    }


def validate_industry_pack(pack: Dict, *, expected_id: Optional[str] = None) -> Dict:
    if not isinstance(pack, dict):
        raise IndustryPackError("industry pack must be a JSON object")
    missing = [name for name in REQUIRED_FIELDS if name not in pack]
    if missing:
        raise IndustryPackError(f"missing fields: {', '.join(missing)}")

    pack_id = str(pack.get("id") or "")
    if not PACK_ID_PATTERN.fullmatch(pack_id):
        raise IndustryPackError("id must contain only lowercase letters, digits and underscores")
    if expected_id and pack_id != expected_id:
        raise IndustryPackError(f"pack id {pack_id} does not match filename {expected_id}")
    if not str(pack.get("name") or "").strip():
        raise IndustryPackError("name is required")
    if pack.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
        raise IndustryPackError("schema_version must be a supported version (1, 2 or 3)")
    if not str(pack.get("pack_version") or "").strip():
        raise IndustryPackError("pack_version is required")
    if not isinstance(pack.get("enabled"), bool):
        raise IndustryPackError("enabled must be boolean")
    _validate_composable_fields(pack)
    _validate_ragflow_policy(pack)

    for field in KEYWORD_FIELDS + ("serpapi_queries",):
        values = pack.get(field)
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise IndustryPackError(f"{field} must be an array of strings")
        pack[field] = unique_normalized_keywords(values)

    query_gates = pack.get("serpapi_query_gates") or {}
    if not isinstance(query_gates, dict):
        raise IndustryPackError("serpapi_query_gates must be an object")
    unknown_query_gates = sorted(set(query_gates) - set(pack["serpapi_queries"]))
    if unknown_query_gates:
        raise IndustryPackError(
            "serpapi_query_gates contains an unknown query: "
            + unknown_query_gates[0]
        )
    normalized_query_gates = {}
    for query, terms in query_gates.items():
        if (
            not isinstance(terms, list)
            or not terms
            or any(not isinstance(term, str) for term in terms)
        ):
            raise IndustryPackError(
                "serpapi_query_gates values must be non-empty string arrays"
            )
        normalized_query_gates[str(query)] = unique_normalized_keywords(terms)
    pack["serpapi_query_gates"] = normalized_query_gates

    classification = pack.get("classification")
    if not isinstance(classification, dict):
        raise IndustryPackError("classification must be an object")
    missing_classification = [name for name in CLASSIFICATION_FIELDS if name not in classification]
    if missing_classification:
        raise IndustryPackError(
            f"classification missing fields: {', '.join(missing_classification)}"
        )
    for field in CLASSIFICATION_FIELDS[:-1]:
        if not isinstance(classification.get(field), (int, float)):
            raise IndustryPackError(f"classification.{field} must be numeric")
    threshold = float(classification["llm_confidence_threshold"])
    if threshold < 0 or threshold > 1:
        raise IndustryPackError("classification.llm_confidence_threshold must be within 0..1")
    tie_break = classification.get("tie_break_order")
    if (
        not isinstance(tie_break, list)
        or set(tie_break) != set(INTERNAL_CATEGORIES)
        or len(tie_break) != len(INTERNAL_CATEGORIES)
    ):
        raise IndustryPackError("classification.tie_break_order must list trend/event/other once")
    for field, default_value in DEFAULT_CLASSIFICATION_WINDOWS.items():
        value = classification.get(field, default_value)
        if not isinstance(value, int):
            raise IndustryPackError(f"classification.{field} must be an integer")
        if value < 1:
            raise IndustryPackError(f"classification.{field} must be at least 1")
        classification[field] = value
    if classification["recent_trend_window_days"] < classification["recent_today_window_days"]:
        raise IndustryPackError(
            "classification.recent_trend_window_days must be greater than or equal to recent_today_window_days"
        )

    sources = pack.get("default_sources")
    if not isinstance(sources, list):
        raise IndustryPackError("default_sources must be an array")
    for index, source in enumerate(list(sources)):
        if not isinstance(source, dict) or not str(source.get("name") or "").strip():
            raise IndustryPackError(f"default_sources[{index}] requires name")
        try:
            source = normalize_manifest_source(source, strict=True)
        except SourceAuthorityError as exc:
            raise IndustryPackError(f"default_sources[{index}]: {exc}") from exc
        _validate_http_url(source.get("url"), f"default_sources[{index}].url")
        sources[index] = source

    topics = pack.get("fixed_topics")
    if not isinstance(topics, list):
        raise IndustryPackError("fixed_topics must be an array")
    if len(topics) > 100:
        raise IndustryPackError("fixed_topics must contain at most 100 topics")
    topic_keys = set()
    topic_names = set()
    for index, topic in enumerate(topics):
        if not isinstance(topic, dict):
            raise IndustryPackError(f"fixed_topics[{index}] must be an object")
        topic_key = str(topic.get("key") or "")
        if not PACK_ID_PATTERN.fullmatch(topic_key) or topic_key in topic_keys:
            raise IndustryPackError(f"fixed_topics[{index}].key is invalid or duplicated")
        topic_keys.add(topic_key)
        topic_name = str(topic.get("name") or "").strip()
        normalized_name = normalize_intel_text(topic_name)
        if not topic_name:
            raise IndustryPackError(f"fixed_topics[{index}].name is required")
        if normalized_name in topic_names:
            raise IndustryPackError(
                f"fixed_topics[{index}].name is duplicated"
            )
        topic_names.add(normalized_name)
        topic["name"] = topic_name
        keywords = topic.get("keywords")
        if not isinstance(keywords, list) or any(
            not isinstance(keyword, str) for keyword in keywords
        ):
            raise IndustryPackError(f"fixed_topics[{index}].keywords must be an array")
        topic["keywords"] = unique_normalized_keywords(keywords)
        if not topic["keywords"]:
            raise IndustryPackError(
                f"fixed_topics[{index}].keywords requires at least one keyword"
            )
        if len(topic["keywords"]) > 200:
            raise IndustryPackError(
                f"fixed_topics[{index}].keywords must contain at most 200 keywords"
            )

    return pack


class IndustryPackLoader:
    def __init__(
        self,
        config_dir: Optional[str] = None,
        *,
        use_published_store: Optional[bool] = None,
        published_manifest_provider=None,
    ):
        uses_default_directory = config_dir is None
        self.config_dir = os.path.abspath(
            config_dir or os.path.join(config.APP_BASE_DIR, "config", "industry_packs")
        )
        self.use_published_store = (
            uses_default_directory
            if use_published_store is None
            else bool(use_published_store)
        )
        self.published_manifest_provider = published_manifest_provider
        self._cache: Dict[str, tuple] = {}
        self._lock = threading.RLock()

    def clear_cache(self, pack_id: str = "") -> None:
        with self._lock:
            if str(pack_id or "").strip():
                self._cache.pop(str(pack_id).strip(), None)
            else:
                self._cache.clear()

    def _published_manifest(self, pack_id: str):
        if not self.use_published_store:
            return None
        if self.published_manifest_provider is not None:
            return self.published_manifest_provider(pack_id)
        try:
            # Lazy import prevents the version store from becoming a hard
            # dependency for isolated file-loader tests and startup tooling.
            from industry_pack_admin import industry_pack_version_store

            return industry_pack_version_store.published_manifest_for_loader(pack_id)
        except Exception:
            # The file manifest is the installation seed.  Before startup
            # migrations create the version tables, falling back is required.
            return None

    def _path_for_id(self, pack_id: str) -> str:
        normalized = str(pack_id or "").strip()
        if not PACK_ID_PATTERN.fullmatch(normalized):
            raise IndustryPackError("invalid industry pack id")
        path = os.path.abspath(os.path.join(self.config_dir, f"{normalized}.json"))
        if os.path.commonpath((self.config_dir, path)) != self.config_dir:
            raise IndustryPackError("industry pack path escapes config directory")
        return path

    def has_seed_pack(self, pack_id: str) -> bool:
        """Return whether an installation-owned manifest exists for this ID."""

        return os.path.isfile(self._path_for_id(pack_id))

    def _database_pack_ids(self) -> List[str]:
        if not self.use_published_store:
            return []
        try:
            provider_owner = getattr(self.published_manifest_provider, "__self__", None)
            if provider_owner is not None and hasattr(
                provider_owner, "list_runtime_pack_ids"
            ):
                return provider_owner.list_runtime_pack_ids()
            from industry_pack_admin import industry_pack_version_store

            return industry_pack_version_store.list_runtime_pack_ids()
        except Exception:
            return []

    def load(
        self,
        pack_id: str = DEFAULT_INDUSTRY_PACK_ID,
        *,
        enabled_only: bool = True,
        use_published: bool = True,
    ) -> Dict:
        path = self._path_for_id(pack_id)
        published = self._published_manifest(pack_id) if use_published else None
        if published:
            raw, digest = published
            cache_token = ("published", str(digest))
            with self._lock:
                cached = self._cache.get(pack_id)
                if cached and cached[0] == cache_token:
                    pack = copy.deepcopy(cached[1])
                else:
                    pack = validate_industry_pack(raw, expected_id=pack_id)
                    self._cache[pack_id] = (cache_token, copy.deepcopy(pack))
            if enabled_only and not pack.get("enabled"):
                raise IndustryPackError(f"industry pack is disabled: {pack_id}")
            return pack
        try:
            modified = os.path.getmtime(path)
        except OSError as exc:
            raise IndustryPackError(f"industry pack not found: {pack_id}") from exc

        with self._lock:
            cached = self._cache.get(pack_id)
            cache_token = ("file", modified)
            if cached and cached[0] == cache_token:
                pack = copy.deepcopy(cached[1])
            else:
                try:
                    with open(path, "r", encoding="utf-8") as handle:
                        raw = json.load(handle)
                except (OSError, json.JSONDecodeError) as exc:
                    raise IndustryPackError(f"failed to read industry pack {pack_id}: {exc}") from exc
                pack = validate_industry_pack(raw, expected_id=pack_id)
                self._cache[pack_id] = (cache_token, copy.deepcopy(pack))

        if enabled_only and not pack.get("enabled"):
            raise IndustryPackError(f"industry pack is disabled: {pack_id}")
        return pack

    def list(self, *, enabled_only: bool = True) -> List[Dict]:
        try:
            filenames = sorted(os.listdir(self.config_dir))
        except OSError:
            filenames = []
        result = []
        pack_ids = {
            filename[:-5]
            for filename in filenames
            if filename != "schema.json" and filename.endswith(".json")
        }
        pack_ids.update(self._database_pack_ids())
        for pack_id in sorted(pack_ids):
            try:
                pack = self.load(pack_id, enabled_only=enabled_only)
                result.append(pack)
            except IndustryPackError:
                continue
        return result

    def effective_pack_set(
        self,
        pack_id: str = DEFAULT_INDUSTRY_PACK_ID,
        *,
        enabled_only: bool = True,
    ) -> List[Dict]:
        """Resolve one pack and all enabled dependencies in stable DFS order."""

        resolved: List[Dict] = []
        visited = set()
        visiting: List[str] = []

        def visit(current_id: str, *, required: bool, root: bool = False) -> None:
            if current_id in visiting:
                cycle = " -> ".join(visiting + [current_id])
                raise IndustryPackError(f"industry pack dependency cycle: {cycle}")
            if current_id in visited:
                return
            try:
                current = self.load(current_id, enabled_only=False)
            except IndustryPackError:
                # Optional means absent is acceptable; a present but malformed
                # dependency is still a configuration error and must fail closed.
                if not required and not root and not os.path.exists(self._path_for_id(current_id)):
                    return
                raise
            if enabled_only and not current.get("enabled"):
                if not required and not root:
                    return
                raise IndustryPackError(f"industry pack dependency is disabled: {current_id}")

            visiting.append(current_id)
            resolved.append(current)
            visited.add(current_id)
            try:
                for include in current.get("includes") or []:
                    visit(
                        str(include["pack_id"]),
                        required=bool(include.get("required", True)),
                    )
            finally:
                visiting.pop()

        visit(str(pack_id or DEFAULT_INDUSTRY_PACK_ID), required=True, root=True)
        return copy.deepcopy(resolved)

    @staticmethod
    def _source_identity(source: Dict) -> str:
        raw = str(source.get("url") or "").strip()
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").casefold().rstrip(".")
        netloc = host
        if parsed.port and not (
            (parsed.scheme.casefold() == "http" and parsed.port == 80)
            or (parsed.scheme.casefold() == "https" and parsed.port == 443)
        ):
            netloc = f"{host}:{parsed.port}"
        path = parsed.path or "/"
        return urlunsplit((parsed.scheme.casefold(), netloc, path, parsed.query, ""))

    def compose(
        self,
        pack_id: str = DEFAULT_INDUSTRY_PACK_ID,
        *,
        enabled_only: bool = True,
    ) -> Dict:
        """Return additive capabilities without mutating the primary pack semantics."""

        packs = self.effective_pack_set(pack_id, enabled_only=enabled_only)
        primary = copy.deepcopy(packs[0])
        capabilities = []
        dashboard_categories = []
        default_sources = []
        seen_capabilities = set()
        seen_categories = set()
        source_by_identity = {}

        for pack in packs:
            declared_by = pack["id"]
            for capability in pack.get("capabilities") or []:
                if capability["key"] in seen_capabilities:
                    continue
                item = copy.deepcopy(capability)
                item["declared_by_pack_id"] = declared_by
                capabilities.append(item)
                seen_capabilities.add(item["key"])
            for category in pack.get("dashboard_categories") or []:
                if category["key"] in seen_categories:
                    continue
                item = copy.deepcopy(category)
                item["declared_by_pack_id"] = declared_by
                dashboard_categories.append(item)
                seen_categories.add(item["key"])
            for source in pack.get("default_sources") or []:
                identity = self._source_identity(source)
                existing = source_by_identity.get(identity)
                if existing:
                    if declared_by not in existing["declared_by_pack_ids"]:
                        existing["declared_by_pack_ids"].append(declared_by)
                    continue
                item = copy.deepcopy(source)
                item["declared_by_pack_ids"] = [declared_by]
                source_by_identity[identity] = item
                default_sources.append(item)

        return {
            "primary_pack_id": primary["id"],
            "primary_pack": primary,
            "effective_pack_ids": [pack["id"] for pack in packs],
            "dependency_pack_ids": [pack["id"] for pack in packs[1:]],
            "packs": packs,
            "capabilities": capabilities,
            "dashboard_categories": dashboard_categories,
            "dashboard_capabilities": copy.deepcopy(
                primary["dashboard_capabilities"]
            ),
            "default_sources": default_sources,
        }

    def metadata(self, *, enabled_only: bool = True) -> List[Dict]:
        return [
            {
                "id": pack["id"],
                "name": pack["name"],
                "schema_version": pack["schema_version"],
                "pack_version": pack["pack_version"],
                "enabled": pack["enabled"],
                "default_market": pack["default_market"],
                "timezone": pack["timezone"],
                "pack_kind": pack["pack_kind"],
                "includes": [item["pack_id"] for item in pack["includes"]],
                "dashboard_capabilities": copy.deepcopy(
                    pack["dashboard_capabilities"]
                ),
                "ragflow_policy": copy.deepcopy(pack["ragflow_policy"]),
            }
            for pack in self.list(enabled_only=enabled_only)
        ]


industry_pack_loader = IndustryPackLoader()


def effective_pack_set(
    pack_id: str = DEFAULT_INDUSTRY_PACK_ID,
    *,
    enabled_only: bool = True,
) -> List[Dict]:
    return industry_pack_loader.effective_pack_set(pack_id, enabled_only=enabled_only)
