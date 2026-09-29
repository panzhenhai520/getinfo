#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""未知证券的受控发现、交叉核验和正式注册表准入。"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

import config
from financial_instruments import InstrumentRegistry, normalize_alias


UTC = timezone.utc
DISCOVERY_SCHEMA_VERSION = "financial-instrument-discovery-v1"
DEFAULT_CATALOG_PATH = (
    Path(__file__).resolve().parent
    / "config"
    / "financial_instrument_discovery.catalog.json"
)
_FINANCE_MARKER = re.compile(
    r"股票|股价|行情|证券|代码|基金|ETF|净值|走势|最新|新闻|消息|信息|动态|公告|"
    r"(?:的)?(?:基本面|技术面|风险|估值|前景|财报|盈利|现金流|资产负债|投资价值)|"
    r"\bstock\b|\bshare price\b|\bquote\b|\bnews\b|\blatest\b",
    re.I,
)
_LEADING_MARKET_SCOPE = re.compile(
    r"^(?:(?:美股|港股|A股|沪深|股票|基金|ETF)\s*)+", re.I
)
_TRAILING_MARKET_SCOPE = re.compile(
    r"\s*(?:(?:美股|港股|A股|沪深|股票|基金|ETF)\s*)+$", re.I
)
_LEADING_REQUEST = re.compile(
    r"^(?:(?:请问|请|麻烦|帮我|查询|查一下|查找|看看|告诉我|分析)\s*)+",
    re.I,
)
_IDENTITY_FIELDS = (
    "canonical_symbol",
    "display_name",
    "asset_type",
    "market",
    "exchange",
    "currency",
    "country_code",
    "listing_status",
)
_CORE_IDENTITY_FIELDS = tuple(
    field for field in _IDENTITY_FIELDS if field != "display_name"
)
_AUTHORITATIVE_TYPES = frozenset({"exchange", "regulator", "issuer"})
_DISCOVERY_WRITE_LOCK = threading.RLock()
PROVIDER_MAPPING_DERIVATION_VERSION = "canonical-provider-mapping-v2"


def _setting(settings: object, name: str, default: object) -> object:
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_utc(value: object) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    return _utc(value, "datetime").isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def derive_provider_mappings(
    identity: Mapping[str, object],
    existing: Mapping[str, str],
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Derive query symbols from an already admitted canonical identity.

    These deterministic aliases are transport metadata only.  They never add
    an identity assertion and therefore cannot satisfy discovery corroboration.
    """

    canonical = str(identity.get("canonical_symbol") or "").strip().upper()
    exchange = str(identity.get("exchange") or "").strip().upper()
    market = str(identity.get("market") or "").strip().upper()
    mappings = {
        str(key).strip(): str(value).strip()
        for key, value in dict(existing).items()
        if str(key).strip() and str(value).strip()
    }
    derived = {}
    if exchange in {"XSHG", "XSHE"} or market in {"CN", "CN_FUND"}:
        suffix = canonical.rsplit(".", 1)[-1] if "." in canonical else ""
        code = canonical.rsplit(".", 1)[0]
        if code.isdigit() and len(code) == 6 and suffix in {"SH", "SZ"}:
            derived = {
                "akshare_cn": code,
                "tushare_cn": f"{code}.{suffix}",
                "yahoo": f"{code}.{'SS' if suffix == 'SH' else 'SZ'}",
                "easyquotation": f"{suffix.casefold()}{code}",
            }
    elif exchange == "XHKG" or market in {"HK", "XHKG"}:
        code = canonical.rsplit(".", 1)[0]
        if code.isdigit():
            canonical_code = code.lstrip("0") or "0"
            canonical_code = canonical_code.zfill(4) if len(canonical_code) < 4 else canonical_code
            five_digit = canonical_code.zfill(5)
            derived = {
                "akshare_cn": five_digit,
                "tushare_cn": f"{five_digit}.HK",
                "yahoo": f"{canonical_code}.HK",
                "easyquotation": five_digit,
            }
    elif market == "US" or exchange in {"US", "XNAS", "XNYS", "ARCX"}:
        symbol = canonical[:-3] if canonical.endswith(".US") else canonical
        if symbol:
            derived = {
                "alpha_vantage": symbol,
                "yahoo": symbol,
            }

    added = []
    for provider_id, provider_symbol in derived.items():
        if provider_id not in mappings:
            mappings[provider_id] = provider_symbol
            added.append(provider_id)
    return mappings, tuple(sorted(added))


def extract_instrument_entity(question: str) -> str:
    """只从用户原文抽取实体，不接受模型生成的代码。"""

    text = unicodedata.normalize("NFKC", str(question or "")).strip()
    if not text:
        return ""
    text = _LEADING_REQUEST.sub("", text)
    text = _LEADING_MARKET_SCOPE.sub("", text)
    marker = _FINANCE_MARKER.search(text)
    candidate = text[: marker.start()] if marker else text
    candidate = _TRAILING_MARKET_SCOPE.sub("", candidate)
    candidate = candidate.strip(" ，,：:？?。.!！")
    if len(candidate) > 96 or not re.search(r"[A-Za-z0-9\u3400-\u9fff]", candidate):
        return ""
    return candidate


class CatalogInstrumentDiscoverySource:
    """读取经审查的本地官方来源快照；测试和离线回退使用相同接口。"""

    def __init__(self, assertions: Sequence[Mapping[str, object]]):
        self.assertions = tuple(dict(item) for item in assertions)

    @classmethod
    def from_path(cls, path: Path | str = DEFAULT_CATALOG_PATH):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if int(payload.get("schema_version") or 0) != 1:
            raise ValueError("unsupported discovery catalog schema")
        return cls(payload.get("assertions") or [])

    def search(self, query: str, *, requested_at: datetime, request_id: str) -> list[dict]:
        del requested_at, request_id
        try:
            normalized = normalize_alias(query)
        except ValueError:
            return []
        matches = []
        for item in self.assertions:
            aliases = list(item.get("aliases") or [])
            aliases.extend(
                [item.get("canonical_symbol"), item.get("display_name")]
            )
            normalized_aliases = set()
            for alias in aliases:
                try:
                    normalized_aliases.add(normalize_alias(alias))
                except ValueError:
                    continue
            if normalized in normalized_aliases:
                matches.append(dict(item))
        return matches


def skipped_instrument_discovery(reason: str) -> dict:
    return {
        "schema_version": DISCOVERY_SCHEMA_VERSION,
        "status": "skipped",
        "query": "",
        "query_normalized": "",
        "candidates": [],
        "promoted_target": None,
        "attempted_sources": [],
        "failed_sources": [],
        "search_hints": [],
        "reason_codes": [str(reason)],
    }


def planned_instrument_discovery(question: str) -> dict:
    query = extract_instrument_entity(question)
    result = skipped_instrument_discovery("instrument_discovery_planned")
    result.update(
        {
            "status": "planned",
            "query": query,
            "query_normalized": normalize_alias(query) if query else "",
        }
    )
    return result


class FinancialInstrumentDiscoveryService:
    """把多来源断言聚合成候选；硬门槛未通过时绝不写正式标的。"""

    def __init__(
        self,
        connection,
        *,
        sources: Optional[Iterable[object]] = None,
        settings=None,
    ):
        self.connection = connection
        self.instruments = InstrumentRegistry(connection)
        self.settings = config if settings is None else settings
        self._uses_default_sources = sources is None
        self.sources = tuple(sources) if sources is not None else (
            CatalogInstrumentDiscoverySource.from_path(),
        )
        self._external_sources = None
        self._lock = threading.RLock()

    def _enabled(self) -> bool:
        return bool(_setting(self.settings, "FINANCIAL_INTELLIGENCE_ENABLED", False)) and bool(
            _setting(self.settings, "FINANCIAL_INSTRUMENT_DISCOVERY_ENABLED", True)
        )

    def _auto_promotion_enabled(self) -> bool:
        return bool(
            _setting(
                self.settings,
                "FINANCIAL_INSTRUMENT_AUTO_PROMOTION_ENABLED",
                True,
            )
        )

    def _external_discovery_enabled(self) -> bool:
        value = _setting(
            self.settings,
            "FINANCIAL_EXTERNAL_INSTRUMENT_DISCOVERY_ENABLED",
            False,
        )
        if isinstance(value, bool):
            return value
        return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}

    def _active_sources(self) -> tuple[object, ...]:
        if not self._uses_default_sources or not self._external_discovery_enabled():
            return self.sources
        from financial_instrument_sources import default_external_instrument_sources

        with self._lock:
            if self._external_sources is None:
                self._external_sources = default_external_instrument_sources(
                    settings=self.settings
                )
            return (*self.sources, *self._external_sources)

    @staticmethod
    def _active_assertion(item: Mapping[str, object], cutoff: datetime) -> bool:
        observed = _parse_utc(item.get("observed_at"))
        expires = _parse_utc(item.get("expires_at"))
        return observed is not None and observed <= cutoff and (
            expires is None or expires >= cutoff
        )

    @staticmethod
    def _identity(item: Mapping[str, object]) -> dict:
        return {
            field: str(item.get(field) or "").strip()
            for field in _IDENTITY_FIELDS
        }

    @staticmethod
    def _candidate_key(query_normalized: str, canonical_symbol: str) -> str:
        material = f"{query_normalized}|{canonical_symbol.upper()}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _alias_collision(self, payload: Mapping[str, object], cutoff: datetime) -> bool:
        canonical = str(payload["canonical_symbol"]).upper()
        for alias in payload.get("aliases") or []:
            alias_text = (
                str(alias.get("alias") or "")
                if isinstance(alias, Mapping)
                else str(alias)
            )
            resolution = self.instruments.resolve(alias_text, as_of=cutoff.date())
            for candidate in resolution.candidates:
                if candidate.instrument.canonical_symbol != canonical:
                    return True
        return False

    def _aggregate(
        self,
        query: str,
        assertions: Sequence[Mapping[str, object]],
        cutoff: datetime,
    ) -> tuple[Optional[dict], list[str], bool]:
        if not assertions:
            return None, ["no_verified_candidate"], False
        groups: dict[str, list[Mapping[str, object]]] = {}
        for item in assertions:
            if not self._active_assertion(item, cutoff):
                continue
            symbol = str(item.get("canonical_symbol") or "").strip().upper()
            if symbol:
                groups.setdefault(symbol, []).append(item)
        if not groups:
            return None, ["no_current_candidate_evidence"], False
        if len(groups) != 1:
            return None, ["multiple_current_candidates"], False

        symbol, evidence = next(iter(groups.items()))
        authorities = [
            dict(item)
            for item in evidence
            if str(item.get("source_role")) == "authoritative"
            and str(item.get("source_type")) in _AUTHORITATIVE_TYPES
        ]
        ordered = [*authorities, *[dict(item) for item in evidence if item not in authorities]]
        identities = [self._identity(item) for item in ordered]
        merged = {}
        for field in _CORE_IDENTITY_FIELDS:
            values = [identity[field] for identity in identities if identity[field]]
            normalized = {value.casefold() for value in values}
            if len(normalized) > 1:
                return None, ["source_identity_conflict"], True
            merged[field] = values[0] if values else ""
        display_names = [
            identity["display_name"] for identity in identities
            if identity["display_name"]
        ]
        merged["display_name"] = display_names[0] if display_names else ""
        if any(not merged[field] for field in _IDENTITY_FIELDS):
            return None, ["incomplete_instrument_identity"], True

        corroborating = [
            dict(item)
            for item in evidence
            if str(item.get("source_role")) == "corroborating"
        ]
        providers = [
            dict(item)
            for item in evidence
            if str(item.get("source_role")) == "provider"
            and bool(item.get("approved"))
            and item.get("provider_key")
            and item.get("provider_symbol")
        ]
        reasons = []
        if not authorities:
            reasons.append("authoritative_source_required")
        if not corroborating:
            reasons.append("independent_corroboration_required")
        if not providers:
            reasons.append("approved_provider_mapping_required")
        authority_keys = {str(item.get("source_key") or "") for item in authorities}
        corroborating_keys = {
            str(item.get("source_key") or "") for item in corroborating
        }
        if authority_keys & corroborating_keys:
            reasons.append("corroboration_must_be_independent")

        provider_mappings: dict[str, str] = {}
        for item in providers:
            key = str(item["provider_key"]).strip()
            value = str(item["provider_symbol"]).strip()
            if key in provider_mappings and provider_mappings[key].casefold() != value.casefold():
                reasons.append("provider_mapping_conflict")
            provider_mappings[key] = value

        provider_mappings, derived_provider_ids = derive_provider_mappings(
            {**merged, "canonical_symbol": symbol},
            provider_mappings,
        )

        aliases = []
        seen_aliases = set()

        def add_alias(value, *, evidence_item=None):
            attributes = dict(value) if isinstance(value, Mapping) else {"alias": value}
            clean = str(attributes.get("alias") or "").strip()
            normalized = normalize_alias(clean)
            if not normalized or normalized in seen_aliases:
                return
            source = evidence_item or {}
            authoritative = (
                str(source.get("source_role") or "") == "authoritative"
                and str(source.get("source_type") or "") in _AUTHORITATIVE_TYPES
            )
            if authoritative:
                metadata = source.get("metadata") or {}
                official_names = metadata.get("official_names") or {}
                former_names = {
                    normalize_alias(item)
                    for item in metadata.get("former_names") or []
                    if str(item or "").strip()
                }
                name_type = next(
                    (
                        f"official_{key}"
                        for key, name in official_names.items()
                        if normalize_alias(name) == normalized
                    ),
                    "former_name" if normalized in former_names else "official_alias",
                )
                attributes.update(
                    {
                        "alias_type": attributes.get("alias_type") or name_type,
                        "source_key": str(source.get("source_key") or ""),
                        "source_url": str(source.get("source_url") or ""),
                        "is_official": True,
                    }
                )
            attributes["alias"] = clean
            aliases.append(attributes if len(attributes) > 1 else clean)
            seen_aliases.add(normalized)

        for item in evidence:
            display_name = str(item.get("display_name") or "").strip()
            if display_name:
                add_alias(display_name, evidence_item=item)
            for alias in item.get("aliases") or []:
                add_alias(alias, evidence_item=item)
        for alias in (symbol.split(".", 1)[0], merged["display_name"], query):
            if alias:
                add_alias(alias)
        observed_values = [_parse_utc(item.get("observed_at")) for item in evidence]
        expires_values = [_parse_utc(item.get("expires_at")) for item in evidence]
        payload = {
            **merged,
            "canonical_symbol": symbol,
            "listed_at": str(evidence[0].get("listed_at") or ""),
            "provider_mappings": provider_mappings,
            "aliases": aliases,
            "metadata": {
                "identity_source": "verified_discovery",
                "discovery_source_keys": sorted(
                    {str(item.get("source_key") or "") for item in evidence}
                ),
                "provider_mapping_derivation": {
                    "version": PROVIDER_MAPPING_DERIVATION_VERSION,
                    "origin": "admitted_canonical_identity",
                    "derived_provider_ids": list(derived_provider_ids),
                    "counts_as_identity_corroboration": False,
                },
            },
            "authoritative_sources": authorities,
            "corroborating_sources": corroborating + providers,
            "observed_at": _utc_text(max(value for value in observed_values if value)),
            "expires_at": (
                _utc_text(min(value for value in expires_values if value))
                if any(expires_values)
                else None
            ),
        }
        if self._alias_collision(payload, cutoff):
            reasons.append("alias_collision")
        return payload, sorted(set(reasons)), True

    def _persist_candidate(
        self,
        query_normalized: str,
        payload: Mapping[str, object],
        *,
        status: str,
        confidence: float,
        reasons: Sequence[str],
        request_id: str,
        promoted_instrument_id: Optional[int] = None,
    ) -> None:
        canonical = str(payload["canonical_symbol"]).upper()
        candidate_key = self._candidate_key(query_normalized, canonical)
        source_payload = {
            "instrument": {
                key: payload.get(key)
                for key in (
                    *_IDENTITY_FIELDS,
                    "listed_at",
                    "provider_mappings",
                    "aliases",
                )
            },
            "authoritative_sources": payload.get("authoritative_sources") or [],
            "corroborating_sources": payload.get("corroborating_sources") or [],
        }
        payload_json = _canonical_json(source_payload)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        now = _utc_text(datetime.now(UTC))
        self.connection.execute(
            """
            INSERT INTO financial_instrument_candidates(
                candidate_key, query_normalized, canonical_symbol, display_name,
                asset_type, market, exchange, currency, country_code, listing_status,
                listed_at, provider_mappings_json, aliases_json,
                authoritative_sources_json, corroborating_sources_json,
                status, confidence, reason_codes_json, payload_sha256,
                observed_at, expires_at, promoted_instrument_id,
                first_request_id, last_request_id, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULLIF(?, ''), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_key) DO UPDATE SET
                display_name=excluded.display_name,
                provider_mappings_json=excluded.provider_mappings_json,
                aliases_json=excluded.aliases_json,
                authoritative_sources_json=excluded.authoritative_sources_json,
                corroborating_sources_json=excluded.corroborating_sources_json,
                status=excluded.status,
                confidence=excluded.confidence,
                reason_codes_json=excluded.reason_codes_json,
                payload_sha256=excluded.payload_sha256,
                observed_at=excluded.observed_at,
                expires_at=excluded.expires_at,
                promoted_instrument_id=COALESCE(excluded.promoted_instrument_id, financial_instrument_candidates.promoted_instrument_id),
                last_request_id=excluded.last_request_id,
                updated_at=excluded.updated_at
            """,
            (
                candidate_key,
                query_normalized,
                canonical,
                payload["display_name"],
                payload["asset_type"],
                payload["market"],
                payload["exchange"],
                payload["currency"],
                payload["country_code"],
                payload["listing_status"],
                payload.get("listed_at") or "",
                _canonical_json(payload.get("provider_mappings") or {}),
                _canonical_json(payload.get("aliases") or []),
                _canonical_json(payload.get("authoritative_sources") or []),
                _canonical_json(payload.get("corroborating_sources") or []),
                status,
                float(confidence),
                _canonical_json(list(reasons)),
                payload_sha256,
                payload["observed_at"],
                payload.get("expires_at"),
                promoted_instrument_id,
                request_id,
                request_id,
                now,
            ),
        )

    def discover_and_promote(
        self,
        question: str,
        *,
        requested_at: datetime,
        request_id: str,
    ) -> dict:
        if not self._enabled():
            return skipped_instrument_discovery("instrument_discovery_disabled")
        cutoff = _utc(requested_at, "requested_at")
        query = extract_instrument_entity(question)
        if not query:
            return skipped_instrument_discovery("instrument_entity_not_found")
        query_normalized = normalize_alias(query)
        assertions = []
        attempted_sources = []
        failed_sources = []

        sources = self._active_sources()
        primary_sources = [
            source for source in sources if not bool(getattr(source, "fallback_only", False))
        ]
        fallback_sources = [
            source for source in sources if bool(getattr(source, "fallback_only", False))
        ]

        def search_source(source, source_query: str, *, suffix: str = ""):
            index = sources.index(source)
            source_name = str(getattr(source, "source_key", "") or f"source-{index + 1}")
            attempt_name = f"{source_name}{suffix}"
            attempted_sources.append(attempt_name)
            try:
                records = source.search(
                    source_query,
                    requested_at=cutoff,
                    request_id=str(request_id or ""),
                )
                assertions.extend(dict(item) for item in records or [])
            except Exception:
                failed_sources.append(attempt_name)

        def verify_provider_mappings(current_payload):
            canonical_symbol = str(
                (current_payload or {}).get("canonical_symbol") or ""
            ).strip()
            for source in primary_sources:
                if not bool(getattr(source, "provider_mapping_verifier", False)):
                    continue
                source_name = str(getattr(source, "source_key", "") or "")
                if (
                    f"{source_name}:provider-mapping" in attempted_sources
                    or not canonical_symbol
                ):
                    continue
                search_source(source, canonical_symbol, suffix=":provider-mapping")
            return self._aggregate(query, assertions, cutoff)

        for source in primary_sources:
            search_source(source, query)
            if self._uses_default_sources:
                current_payload, current_reasons, _ = self._aggregate(
                    query, assertions, cutoff
                )
                if current_payload is not None and not current_reasons:
                    break

        payload, reasons, persistable = self._aggregate(query, assertions, cutoff)
        if payload is not None and not reasons:
            payload, reasons, persistable = verify_provider_mappings(payload)
        if payload is None or reasons:
            authoritative_suggestions = []
            for item in assertions:
                if str(item.get("source_role") or "") != "authoritative":
                    continue
                suggestion = str(item.get("canonical_symbol") or "").strip()
                if (
                    suggestion
                    and normalize_alias(suggestion) != query_normalized
                    and suggestion not in authoritative_suggestions
                ):
                    authoritative_suggestions.append(suggestion)
            canonical_sources = [
                source
                for source in primary_sources
                if bool(getattr(source, "canonical_requery", False))
            ]
            resolved = False
            for suggestion_index, suggestion in enumerate(
                authoritative_suggestions[:3], start=1
            ):
                for source in canonical_sources:
                    search_source(
                        source,
                        suggestion,
                        suffix=f":canonical-{suggestion_index}",
                    )
                    current_payload, current_reasons, _ = self._aggregate(
                        query, assertions, cutoff
                    )
                    if current_payload is not None and not current_reasons:
                        resolved = True
                        break
                if resolved:
                    break
            payload, reasons, persistable = self._aggregate(
                query, assertions, cutoff
            )
        if payload is not None and not reasons:
            payload, reasons, persistable = verify_provider_mappings(payload)
        search_hints = []
        if (payload is None or reasons) and fallback_sources:
            for source in fallback_sources:
                search_source(source, query)
            search_hints = [
                {
                    "source_url": str(item.get("source_url") or ""),
                    "title": str(item.get("title") or "")[:300],
                    "suggested_queries": list(item.get("suggested_queries") or [])[:3],
                }
                for item in assertions
                if str(item.get("source_role") or "") == "web_hint"
            ][:10]
            suggestions = []
            for hint in search_hints:
                for suggestion in hint["suggested_queries"]:
                    if suggestion and suggestion not in suggestions:
                        suggestions.append(str(suggestion))
            structured_sources = [
                source
                for source in primary_sources
                if not isinstance(source, CatalogInstrumentDiscoverySource)
            ]
            for suggestion_index, suggestion in enumerate(suggestions[:3], start=1):
                for source in structured_sources:
                    search_source(
                        source,
                        suggestion,
                        suffix=f":suggestion-{suggestion_index}",
                    )
            payload, reasons, persistable = self._aggregate(
                query, assertions, cutoff
            )
        if payload is None:
            has_identity_candidate = any(
                str(item.get("canonical_symbol") or "").strip()
                for item in assertions
                if str(item.get("source_role") or "") != "web_hint"
            )
            return {
                "schema_version": DISCOVERY_SCHEMA_VERSION,
                "status": (
                    "verification_required" if has_identity_candidate else "not_found"
                ),
                "query": query,
                "query_normalized": query_normalized,
                "candidates": [],
                "promoted_target": None,
                "attempted_sources": attempted_sources,
                "failed_sources": failed_sources,
                "search_hints": search_hints,
                "reason_codes": reasons + (["source_query_failed"] if failed_sources else []),
            }

        auto_promote = self._auto_promotion_enabled()
        status = "rejected" if reasons else ("verified" if not auto_promote else "promoted")
        confidence = 0.0 if reasons else 1.0
        promoted = None
        with self._lock, _DISCOVERY_WRITE_LOCK:
            savepoint = f"financial_instrument_discovery_{uuid.uuid4().hex}"
            self.connection.execute(f"SAVEPOINT {savepoint}")
            try:
                if not reasons and auto_promote:
                    promoted = self.instruments.upsert_instrument(payload)
                self._persist_candidate(
                    query_normalized,
                    payload,
                    status=status,
                    confidence=confidence,
                    reasons=reasons or (["auto_promotion_disabled"] if not auto_promote else []),
                    request_id=str(request_id or ""),
                    promoted_instrument_id=(promoted.instrument_id if promoted else None),
                )
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            except Exception:
                self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise

        candidate = {
            key: payload.get(key)
            for key in (
                "canonical_symbol", "display_name", "asset_type", "market",
                "exchange", "currency", "country_code", "listing_status",
                "listed_at", "provider_mappings", "aliases", "observed_at", "expires_at",
            )
        }
        return {
            "schema_version": DISCOVERY_SCHEMA_VERSION,
            "status": status,
            "query": query,
            "query_normalized": query_normalized,
            "candidates": [candidate],
            "promoted_target": promoted.to_dict() if promoted else None,
            "attempted_sources": attempted_sources,
            "failed_sources": failed_sources,
            "search_hints": search_hints,
            "reason_codes": reasons or (["auto_promotion_disabled"] if not auto_promote else ["verified_candidate_promoted"]),
        }
