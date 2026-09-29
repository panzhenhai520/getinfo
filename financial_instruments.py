#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Stable instrument identities, temporal aliases, and ambiguity resolution.

The registry is embedded in the existing SQLite database.  It does not fetch
market data and it never guesses which candidate a user intended.
"""

from __future__ import annotations

import json
import re
import threading
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple


DEFAULT_SEED_PATH = (
    Path(__file__).resolve().parent / "config" / "financial_instruments.seed.json"
)
ALLOWED_LISTING_STATUSES = frozenset({"active", "suspended", "delisted"})
ALLOWED_ASSET_TYPES = frozenset(
    {
        "equity", "index", "etf", "fund", "bond", "future", "forex",
        "crypto", "macro", "prediction",
    }
)
MARKET_ALIASES = {
    "sh": "XSHG",
    "sse": "XSHG",
    "xshg": "XSHG",
    "上海": "XSHG",
    "上交所": "XSHG",
    "sz": "XSHE",
    "szse": "XSHE",
    "xshe": "XSHE",
    "深圳": "XSHE",
    "深交所": "XSHE",
    "hk": "XHKG",
    "hkex": "XHKG",
    "xhkg": "XHKG",
    "香港": "XHKG",
    "港股": "XHKG",
    "cn": "CN",
    "a股": "CN",
    "cn_fund": "CN_FUND",
    "us": "US",
    "美股": "US",
    "jp": "JP",
    "日本": "JP",
    "日股": "JP",
    "macro": "MACRO",
    "宏观": "MACRO",
    "prediction": "PREDICTION",
    "预期": "PREDICTION",
}

# 同一进程内可能有多个 InstrumentRegistry 实例共享主 SQLite 连接。
# SQLite 连接不允许多个线程同时操作嵌套 savepoint，因此注册表写入必须
# 使用一个进程级短锁；标的写入频率很低，不会影响行情和新闻读取并行度。
_REGISTRY_WRITE_LOCK = threading.RLock()


def normalize_alias(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    normalized = re.sub(r"\s+", "", normalized)
    if not normalized:
        raise ValueError("instrument alias cannot be empty")
    return normalized


def normalize_market(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text:
        return ""
    return MARKET_ALIASES.get(text.casefold(), text.upper())


def stable_instrument_key(
    *,
    canonical_symbol: str,
    asset_type: str,
    exchange: str = "",
    country_code: str = "",
    market: str = "",
) -> str:
    """Build the provider-independent identity defined by the financial contract."""

    canonical = str(canonical_symbol or "").strip().upper()
    kind = str(asset_type or "").strip().upper()
    venue = normalize_market(exchange or market)
    country = str(country_code or "").strip().upper()
    if not country:
        country = {
            "XSHG": "CN",
            "XSHE": "CN",
            "CN_FUND": "CN",
            "XHKG": "HK",
        }.get(venue, normalize_market(market))
    if not canonical or not kind or not venue or not country:
        raise ValueError(
            "canonical_symbol, asset_type, venue, and country_code are required "
            "for a stable instrument key"
        )
    code = canonical.split(".", 1)[0]
    if country == "HK" and code.isdigit():
        code = code.zfill(5)
    return f"{country}:{venue}:{kind}:{code}"


def _json_object(value, label: str) -> Dict[str, object]:
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _iso_date(value, label: str, *, allow_empty: bool = True) -> str:
    if value in (None, "") and allow_empty:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{label} datetime must be timezone-aware")
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO date") from exc


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True)
class InstrumentRecord:
    instrument_id: int
    canonical_symbol: str
    display_name: str
    asset_type: str
    market: str
    exchange: str
    currency: str
    country_code: str
    listing_status: str
    listed_at: str
    delisted_at: str
    provider_mappings: Mapping[str, str]
    metadata: Mapping[str, object]

    @property
    def share_class(self) -> str:
        return str(self.metadata.get("share_class") or "")

    @property
    def instrument_key(self) -> str:
        return stable_instrument_key(
            canonical_symbol=self.canonical_symbol,
            asset_type=self.asset_type,
            market=self.market,
            exchange=self.exchange,
            country_code=self.country_code,
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "instrument_key": self.instrument_key,
            "canonical_symbol": self.canonical_symbol,
            "display_name": self.display_name,
            "asset_type": self.asset_type,
            "market": self.market,
            "exchange": self.exchange,
            "currency": self.currency,
            "country_code": self.country_code,
            "listing_status": self.listing_status,
            "listed_at": self.listed_at or None,
            "delisted_at": self.delisted_at or None,
            "provider_mappings": dict(self.provider_mappings),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class InstrumentCandidate:
    instrument: InstrumentRecord
    matched_aliases: Tuple[str, ...]
    matched_provider_keys: Tuple[str, ...]
    reasons: Tuple[str, ...]

    def to_dict(self) -> Dict[str, object]:
        payload = self.instrument.to_dict()
        payload.update(
            {
                "matched_aliases": list(self.matched_aliases),
                "matched_provider_keys": list(self.matched_provider_keys),
                "reasons": list(self.reasons),
            }
        )
        return payload


@dataclass(frozen=True)
class InstrumentResolution:
    query: str
    normalized_query: str
    status: str
    reason: str
    candidates: Tuple[InstrumentCandidate, ...]
    required_clarifications: Tuple[str, ...] = ()

    @property
    def instrument_id(self) -> Optional[int]:
        if self.status == "resolved" and len(self.candidates) == 1:
            return self.candidates[0].instrument.instrument_id
        return None

    def to_dict(self) -> Dict[str, object]:
        return {
            "query": self.query,
            "normalized_query": self.normalized_query,
            "status": self.status,
            "reason": self.reason,
            "instrument_id": self.instrument_id,
            "required_clarifications": list(self.required_clarifications),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


class InstrumentNotFoundError(LookupError):
    pass


class AmbiguousInstrumentError(LookupError):
    def __init__(self, resolution: InstrumentResolution):
        super().__init__(resolution.reason)
        self.resolution = resolution


class InstrumentRegistry:
    """Repository and resolver over the existing financial schema."""

    def __init__(self, connection):
        self.connection = connection
        self._alias_provenance_supported = False
        self._assert_schema()

    def _assert_schema(self) -> None:
        required = {"financial_instruments", "financial_instrument_aliases"}
        # Multiple registries can be constructed concurrently over the same
        # check_same_thread=False SQLite connection.  Serialize this short
        # connection-level read with the existing registry transaction lock;
        # sqlite connections are not safe for overlapping execute calls even
        # when the statements themselves are read-only.
        with _REGISTRY_WRITE_LOCK:
            rows = self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        present = {row[0] for row in rows}
        missing = sorted(required - present)
        if missing:
            raise RuntimeError(f"financial schema is not initialized: {', '.join(missing)}")
        with _REGISTRY_WRITE_LOCK:
            from db_connection import is_postgres_connection
            if is_postgres_connection(self.connection):
                # postgres 兼容层不支持 PRAGMA table_info；用 information_schema
                alias_columns = {
                    str(row[0])
                    for row in self.connection.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema=current_schema() "
                        "AND table_name='financial_instrument_aliases'"
                    ).fetchall()
                }
            else:
                alias_columns = {
                    str(row[1])
                    for row in self.connection.execute(
                        "PRAGMA table_info(financial_instrument_aliases)"
                    ).fetchall()
                }
        self._alias_provenance_supported = {
            "alias_type", "source_key", "source_url", "is_official"
        }.issubset(alias_columns)

    @contextmanager
    def _atomic(self):
        savepoint = f"instrument_registry_{uuid.uuid4().hex}"
        with _REGISTRY_WRITE_LOCK:
            self.connection.execute(f"SAVEPOINT {savepoint}")
            try:
                yield
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            except Exception:
                self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise

    @staticmethod
    def _record_from_values(values: Sequence[object]) -> InstrumentRecord:
        return InstrumentRecord(
            instrument_id=int(values[0]),
            canonical_symbol=str(values[1]),
            display_name=str(values[2]),
            asset_type=str(values[3]),
            market=str(values[4]),
            exchange=str(values[5]),
            currency=str(values[6]),
            country_code=str(values[7]),
            listing_status=str(values[8]),
            listed_at=str(values[9] or ""),
            delisted_at=str(values[10] or ""),
            provider_mappings=_json_object(values[11], "provider_mappings_json"),
            metadata=_json_object(values[12], "metadata_json"),
        )

    def get(self, instrument_id: int) -> Optional[InstrumentRecord]:
        row = self.connection.execute(
            """
            SELECT id, canonical_symbol, display_name, asset_type, market, exchange,
                   currency, country_code, listing_status, listed_at, delisted_at,
                   provider_mappings_json, metadata_json
            FROM financial_instruments WHERE id=?
            """,
            (int(instrument_id),),
        ).fetchone()
        return self._record_from_values(row) if row else None

    def get_by_canonical_symbol(self, canonical_symbol: str) -> Optional[InstrumentRecord]:
        row = self.connection.execute(
            """
            SELECT id, canonical_symbol, display_name, asset_type, market, exchange,
                   currency, country_code, listing_status, listed_at, delisted_at,
                   provider_mappings_json, metadata_json
            FROM financial_instruments WHERE canonical_symbol=?
            """,
            (str(canonical_symbol or "").strip().upper(),),
        ).fetchone()
        return self._record_from_values(row) if row else None

    def _add_alias(
        self,
        instrument_id: int,
        alias: str,
        *,
        market: str,
        provider_key: str = "",
        alias_type: str = "other",
        source_key: str = "",
        source_url: str = "",
        is_official: bool = False,
        valid_from: str = "",
        valid_to: str = "",
        is_primary: bool = False,
    ) -> None:
        alias_text = unicodedata.normalize("NFKC", str(alias or "")).strip()
        alias_normalized = normalize_alias(alias_text)
        start = _iso_date(valid_from, "valid_from")
        end = _iso_date(valid_to, "valid_to")
        if start and end and end <= start:
            raise ValueError("valid_to must be later than valid_from")
        if not self._alias_provenance_supported:
            # Rolling deployments can briefly serve requests against a v6
            # database before the startup migrator has added v7 provenance
            # columns. Keep identity writes available without performing DDL
            # from the request path; the next normal startup backfills schema.
            self.connection.execute(
                """
                INSERT INTO financial_instrument_aliases(
                    instrument_id, alias, alias_normalized, market, provider_key,
                    valid_from, valid_to, is_primary
                ) VALUES(?, ?, ?, ?, ?, ?, NULLIF(?, ''), ?)
                ON CONFLICT(
                    instrument_id, alias_normalized, market, provider_key, valid_from
                ) DO UPDATE SET
                    alias=excluded.alias,
                    valid_to=excluded.valid_to,
                    is_primary=MAX(financial_instrument_aliases.is_primary, excluded.is_primary)
                """,
                (
                    int(instrument_id), alias_text, alias_normalized,
                    normalize_market(market), str(provider_key or "").strip(),
                    start, end, int(bool(is_primary)),
                ),
            )
            return
        self.connection.execute(
            """
            INSERT INTO financial_instrument_aliases(
                instrument_id, alias, alias_normalized, market, provider_key,
                alias_type, source_key, source_url, is_official,
                valid_from, valid_to, is_primary
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULLIF(?, ''), ?)
            ON CONFLICT(
                instrument_id, alias_normalized, market, provider_key, valid_from
            ) DO UPDATE SET
                alias=excluded.alias,
                alias_type=CASE
                    WHEN excluded.is_official=1 THEN excluded.alias_type
                    ELSE financial_instrument_aliases.alias_type END,
                source_key=CASE
                    WHEN excluded.is_official=1 THEN excluded.source_key
                    ELSE financial_instrument_aliases.source_key END,
                source_url=CASE
                    WHEN excluded.is_official=1 THEN excluded.source_url
                    ELSE financial_instrument_aliases.source_url END,
                is_official=MAX(financial_instrument_aliases.is_official, excluded.is_official),
                valid_to=excluded.valid_to,
                is_primary=MAX(financial_instrument_aliases.is_primary, excluded.is_primary)
            """,
            (
                int(instrument_id),
                alias_text,
                alias_normalized,
                normalize_market(market),
                str(provider_key or "").strip(),
                str(alias_type or "other").strip()[:80] or "other",
                str(source_key or "").strip()[:160],
                str(source_url or "").strip()[:1000],
                int(bool(is_official)),
                start,
                end,
                int(bool(is_primary)),
            ),
        )

    def add_alias(self, instrument_id: int, alias: str, **attributes) -> None:
        instrument = self.get(instrument_id)
        if instrument is None:
            raise InstrumentNotFoundError(f"unknown instrument_id: {instrument_id}")
        attributes.setdefault("market", instrument.exchange or instrument.market)
        with self._atomic():
            self._add_alias(instrument_id, alias, **attributes)

    def _close_alias(
        self,
        instrument_id: int,
        alias: str,
        *,
        provider_key: str,
        valid_to: str,
    ) -> None:
        if not valid_to:
            raise ValueError("effective_from is required when replacing an alias")
        self.connection.execute(
            """
            UPDATE financial_instrument_aliases
            SET valid_to=?
            WHERE instrument_id=? AND alias_normalized=? AND provider_key=?
              AND valid_to IS NULL AND (valid_from='' OR valid_from < ?)
            """,
            (valid_to, int(instrument_id), normalize_alias(alias), provider_key, valid_to),
        )

    def upsert_instrument(self, payload: Mapping[str, object]) -> InstrumentRecord:
        if not isinstance(payload, Mapping):
            raise ValueError("instrument payload must be an object")
        canonical = str(payload.get("canonical_symbol") or "").strip().upper()
        display_name = str(payload.get("display_name") or "").strip()
        asset_type = str(payload.get("asset_type") or "").strip().lower()
        market = normalize_market(payload.get("market") or "")
        exchange = normalize_market(payload.get("exchange") or "")
        listing_status = str(payload.get("listing_status") or "active").strip().lower()
        if not canonical or not display_name or not market:
            raise ValueError("canonical_symbol, display_name, and market are required")
        if asset_type not in ALLOWED_ASSET_TYPES:
            raise ValueError(f"unsupported asset_type: {asset_type}")
        if listing_status not in ALLOWED_LISTING_STATUSES:
            raise ValueError(f"unsupported listing_status: {listing_status}")
        listed_at = _iso_date(payload.get("listed_at"), "listed_at")
        delisted_at = _iso_date(payload.get("delisted_at"), "delisted_at")
        effective_from = _iso_date(
            payload.get("effective_from") or listed_at, "effective_from"
        )
        if listing_status == "delisted" and not delisted_at:
            raise ValueError("delisted_at is required for a delisted instrument")
        if delisted_at and listed_at and delisted_at <= listed_at:
            raise ValueError("delisted_at must be later than listed_at")
        metadata = _json_object(payload.get("metadata"), "metadata")
        incoming_mappings = {
            str(key).strip(): str(value).strip()
            for key, value in _json_object(
                payload.get("provider_mappings"), "provider_mappings"
            ).items()
        }
        if any(not key or not value for key, value in incoming_mappings.items()):
            raise ValueError("provider mapping keys and symbols cannot be empty")
        aliases = payload.get("aliases") or []
        if not isinstance(aliases, (list, tuple)):
            raise ValueError("aliases must be a list")

        with self._atomic():
            existing = self.get_by_canonical_symbol(canonical)
            if existing:
                stable_before = (existing.asset_type, existing.market, existing.exchange)
                stable_after = (asset_type, market, exchange)
                if stable_before != stable_after:
                    raise ValueError(
                        "asset_type, market, and exchange are stable identity fields"
                    )
                if existing.listing_status == "delisted" and listing_status != "delisted":
                    raise ValueError("a delisted identity cannot be reactivated; create a new identity")
                listed_at = listed_at or existing.listed_at
                delisted_at = delisted_at or existing.delisted_at
                merged_mappings = dict(existing.provider_mappings)
                for provider_key, symbol in incoming_mappings.items():
                    old_symbol = str(merged_mappings.get(provider_key) or "")
                    if old_symbol and normalize_alias(old_symbol) != normalize_alias(symbol):
                        self._close_alias(
                            existing.instrument_id,
                            old_symbol,
                            provider_key=provider_key,
                            valid_to=effective_from,
                        )
                    merged_mappings[provider_key] = symbol
                merged_metadata = dict(existing.metadata)
                merged_metadata.update(metadata)
                if existing.display_name != display_name:
                    self._close_alias(
                        existing.instrument_id,
                        existing.display_name,
                        provider_key="",
                        valid_to=effective_from,
                    )
                self.connection.execute(
                    """
                    UPDATE financial_instruments
                    SET display_name=?, currency=?, country_code=?, listing_status=?,
                        listed_at=NULLIF(?, ''), delisted_at=NULLIF(?, ''),
                        provider_mappings_json=?, metadata_json=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        display_name,
                        str(payload.get("currency") or existing.currency).upper(),
                        str(payload.get("country_code") or existing.country_code).upper(),
                        listing_status,
                        listed_at,
                        delisted_at,
                        json.dumps(merged_mappings, ensure_ascii=False, sort_keys=True),
                        json.dumps(merged_metadata, ensure_ascii=False, sort_keys=True),
                        _utc_now_text(),
                        existing.instrument_id,
                    ),
                )
                instrument_id = existing.instrument_id
            else:
                self.connection.execute(
                    """
                    INSERT INTO financial_instruments(
                        canonical_symbol, display_name, asset_type, market, exchange,
                        currency, country_code, listing_status, listed_at, delisted_at,
                        provider_mappings_json, metadata_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULLIF(?, ''), NULLIF(?, ''), ?, ?)
                    """,
                    (
                        canonical,
                        display_name,
                        asset_type,
                        market,
                        exchange,
                        str(payload.get("currency") or "").upper(),
                        str(payload.get("country_code") or "").upper(),
                        listing_status,
                        listed_at,
                        delisted_at,
                        json.dumps(incoming_mappings, ensure_ascii=False, sort_keys=True),
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    ),
                )
                instrument_id = int(self.connection.execute("SELECT last_insert_rowid()").fetchone()[0])

            alias_market = exchange or market
            self._add_alias(
                instrument_id,
                canonical,
                market=alias_market,
                valid_from=listed_at,
                is_primary=True,
                alias_type="canonical_symbol",
                is_official=True,
            )
            self._add_alias(
                instrument_id,
                display_name,
                market=alias_market,
                valid_from=effective_from,
                is_primary=True,
                alias_type="display_name",
            )
            for provider_key, symbol in incoming_mappings.items():
                self._add_alias(
                    instrument_id,
                    symbol,
                    market=alias_market,
                    provider_key=provider_key,
                    valid_from=effective_from,
                    alias_type="provider_symbol",
                )
            for item in aliases:
                attributes = dict(item) if isinstance(item, Mapping) else {"alias": item}
                alias_text = attributes.pop("alias", "")
                self._add_alias(
                    instrument_id,
                    alias_text,
                    market=attributes.pop("market", alias_market),
                    provider_key=attributes.pop("provider_key", ""),
                    alias_type=attributes.pop("alias_type", "other"),
                    source_key=attributes.pop("source_key", ""),
                    source_url=attributes.pop("source_url", ""),
                    is_official=bool(attributes.pop("is_official", False)),
                    valid_from=attributes.pop("valid_from", effective_from),
                    valid_to=attributes.pop("valid_to", ""),
                    is_primary=bool(attributes.pop("is_primary", False)),
                )
                if attributes:
                    raise ValueError(
                        f"unsupported alias attributes: {', '.join(sorted(attributes))}"
                    )
        result = self.get(instrument_id)
        if result is None:  # pragma: no cover - protected by the transaction
            raise RuntimeError("instrument write did not persist")
        return result

    def import_master_records(
        self,
        records: Iterable[Mapping[str, object]],
        *,
        provider_key: str,
        observed_on,
    ) -> Tuple[InstrumentRecord, ...]:
        source = str(provider_key or "").strip()
        if not source:
            raise ValueError("provider_key is required")
        effective = _iso_date(observed_on, "observed_on", allow_empty=False)
        imported = []
        for original in records:
            item = dict(original)
            provider_symbol = str(item.pop("provider_symbol", "")).strip()
            if not provider_symbol:
                raise ValueError("provider_symbol is required for provider master data")
            mappings = _json_object(item.get("provider_mappings"), "provider_mappings")
            mappings[source] = provider_symbol
            item["provider_mappings"] = mappings
            item.setdefault("effective_from", effective)
            metadata = _json_object(item.get("metadata"), "metadata")
            metadata.update(
                {"master_source": source, "master_observed_on": effective}
            )
            item["metadata"] = metadata
            imported.append(self.upsert_instrument(item))
        return tuple(imported)

    def load_controlled_seed(self, path: Path | str = DEFAULT_SEED_PATH) -> Tuple[InstrumentRecord, ...]:
        seed = json.loads(Path(path).read_text(encoding="utf-8"))
        if seed.get("schema_version") != 1 or not seed.get("seed_version"):
            raise ValueError("unsupported instrument seed schema")
        source = str(seed.get("source") or "controlled_seed").strip()
        observed_on = _iso_date(seed.get("effective_on"), "effective_on", allow_empty=False)
        records = []
        for original in seed.get("instruments") or []:
            item = dict(original)
            metadata = _json_object(item.get("metadata"), "metadata")
            metadata.update(
                {"master_source": source, "seed_version": str(seed["seed_version"])}
            )
            item["metadata"] = metadata
            item.setdefault("effective_from", item.get("listed_at") or observed_on)
            records.append(self.upsert_instrument(item))
        return tuple(records)

    def resolve(
        self,
        query: str,
        *,
        market: str = "",
        asset_type: str = "",
        share_class: str = "",
        currency: str = "",
        as_of=None,
        provider_key: str = "",
    ) -> InstrumentResolution:
        query_text = unicodedata.normalize("NFKC", str(query or "")).strip()
        normalized = normalize_alias(query_text)
        requested_market = normalize_market(market)
        requested_asset_type = str(asset_type or "").strip().lower()
        requested_share_class = str(share_class or "").strip().casefold()
        requested_currency = str(currency or "").strip().upper()
        if requested_asset_type and requested_asset_type not in ALLOWED_ASSET_TYPES:
            raise ValueError(f"unsupported asset_type: {requested_asset_type}")
        as_of_text = _iso_date(as_of, "as_of") if as_of not in (None, "") else ""
        source = str(provider_key or "").strip()
        rows = self.connection.execute(
            """
            SELECT i.id, i.canonical_symbol, i.display_name, i.asset_type, i.market,
                   i.exchange, i.currency, i.country_code, i.listing_status,
                   i.listed_at, i.delisted_at, i.provider_mappings_json, i.metadata_json,
                   a.alias, a.provider_key
            FROM financial_instrument_aliases a
            JOIN financial_instruments i ON i.id=a.instrument_id
            WHERE a.alias_normalized=?
              AND (?='' OR a.provider_key='' OR a.provider_key=?)
              AND (
                    (?='' AND a.valid_to IS NULL)
                    OR
                    (?<>'' AND (a.valid_from='' OR a.valid_from<=?)
                           AND (a.valid_to IS NULL OR a.valid_to>?))
                  )
            ORDER BY i.market, i.exchange, i.asset_type, i.canonical_symbol
            """,
            (normalized, source, source, as_of_text, as_of_text, as_of_text, as_of_text),
        ).fetchall()

        grouped: Dict[int, Dict[str, object]] = {}
        for row in rows:
            record = self._record_from_values(row[:13])
            if requested_market and requested_market not in {
                normalize_market(record.market),
                normalize_market(record.exchange),
            }:
                continue
            if requested_asset_type and record.asset_type != requested_asset_type:
                continue
            if requested_share_class and record.share_class.casefold() != requested_share_class:
                continue
            if requested_currency and record.currency.upper() != requested_currency:
                continue
            if as_of_text:
                if record.listed_at and record.listed_at > as_of_text:
                    continue
                if record.delisted_at and record.delisted_at <= as_of_text:
                    continue
            entry = grouped.setdefault(
                record.instrument_id,
                {"record": record, "aliases": set(), "providers": set()},
            )
            entry["aliases"].add(str(row[13]))
            if row[14]:
                entry["providers"].add(str(row[14]))

        candidates = []
        for entry in grouped.values():
            record = entry["record"]
            reasons = ["exact_alias_match"]
            if record.listing_status != "active":
                reasons.append(f"listing_status:{record.listing_status}")
            if record.share_class:
                reasons.append(f"share_class:{record.share_class}")
            candidates.append(
                InstrumentCandidate(
                    instrument=record,
                    matched_aliases=tuple(sorted(entry["aliases"])),
                    matched_provider_keys=tuple(sorted(entry["providers"])),
                    reasons=tuple(reasons),
                )
            )
        candidates.sort(
            key=lambda candidate: (
                candidate.instrument.market,
                candidate.instrument.exchange,
                candidate.instrument.asset_type,
                candidate.instrument.canonical_symbol,
            )
        )
        candidate_tuple = tuple(candidates)
        if not candidate_tuple:
            return InstrumentResolution(
                query_text,
                normalized,
                "not_found",
                "no_instrument_matches_alias_and_filters",
                (),
            )
        if len(candidate_tuple) == 1:
            return InstrumentResolution(
                query_text,
                normalized,
                "resolved",
                "unique_exact_alias_match",
                candidate_tuple,
            )

        clarifications = []
        if len({item.instrument.exchange for item in candidate_tuple}) > 1:
            clarifications.append("exchange")
        if len({item.instrument.asset_type for item in candidate_tuple}) > 1:
            clarifications.append("asset_type")
        share_classes = {
            item.instrument.share_class for item in candidate_tuple if item.instrument.share_class
        }
        if len(share_classes) > 1:
            clarifications.append("share_class")
        currencies = {item.instrument.currency for item in candidate_tuple if item.instrument.currency}
        if len(currencies) > 1:
            clarifications.append("currency")
        if not clarifications:
            clarifications.append("instrument")
        return InstrumentResolution(
            query_text,
            normalized,
            "ambiguous",
            "multiple_instruments_share_the_exact_alias",
            candidate_tuple,
            tuple(clarifications),
        )

    def find_mentions(
        self,
        text: str,
        *,
        max_mentions: int = 8,
        as_of=None,
    ) -> Tuple[InstrumentResolution, ...]:
        """Resolve known aliases that occur in free text without fuzzy guessing.

        Longest aliases win, then overlapping shorter aliases are ignored.  The
        returned resolution remains ambiguous when the master registry says it
        is ambiguous (for example ``000001``); callers must not select one.
        """

        limit = int(max_mentions)
        if limit < 1 or limit > 32:
            raise ValueError("max_mentions must be within 1..32")
        normalized_text = normalize_alias(text)
        rows = self.connection.execute(
            """
            SELECT DISTINCT alias, alias_normalized
            FROM financial_instrument_aliases
            WHERE alias_normalized<>''
            ORDER BY length(alias_normalized) DESC, alias_normalized
            """
        ).fetchall()
        occupied = set()
        matches = []
        seen_queries = set()
        for alias, alias_normalized in rows:
            needle = str(alias_normalized)
            if not needle or needle in seen_queries:
                continue
            start = normalized_text.find(needle)
            if start < 0:
                continue
            end = start + len(needle)
            if any(position in occupied for position in range(start, end)):
                continue
            # ASCII aliases such as HSI/Apple must be token-like.  This avoids
            # matching an alias inside an unrelated identifier.
            if needle.isascii():
                before = normalized_text[start - 1] if start else ""
                after = normalized_text[end] if end < len(normalized_text) else ""
                if (
                    (before and before.isascii() and before.isalnum())
                    or (after and after.isascii() and after.isalnum())
                ):
                    continue
            resolution = self.resolve(str(alias), as_of=as_of)
            if resolution.status == "not_found":
                continue
            matches.append(resolution)
            seen_queries.add(needle)
            occupied.update(range(start, end))
            if len(matches) >= limit:
                break
        return tuple(matches)

    def require_instrument_id(self, query: str, **filters) -> int:
        resolution = self.resolve(query, **filters)
        if resolution.status == "not_found":
            raise InstrumentNotFoundError(resolution.reason)
        if resolution.status == "ambiguous":
            raise AmbiguousInstrumentError(resolution)
        return int(resolution.instrument_id)
