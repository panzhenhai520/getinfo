#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Bounded target refresh with recent-first and latest-available discovery."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional
from urllib.parse import urlsplit

import config
from candidate_dispatcher import IntelCandidateDispatcher
from financial_evidence import _normalize_text, _term_occurs
from financial_official_news import (
    FinancialOfficialNewsRepository,
    HKEXNewsTitleSearchClient,
)
from intel_candidates import IntelCandidateRepository
from intel_http import sanitize_external_error
from intel_light_scanner import IntelLightScanner, source_scan_window_key
from intel_sources import IntelSourceRegistry
from serpapi_client import SerpAPIClient


UTC = timezone.utc
FINANCIAL_NEWS_REFRESH_VERSION = "financial-news-refresh-v1"


def _setting(settings: object, name: str, default: object) -> object:
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


def _target_market(target: Mapping[str, object]) -> str:
    exchange = str(target.get("exchange") or "").strip().upper()
    market = str(target.get("market") or "").strip().upper()
    if exchange in {"XSHG", "XSHE"} or market in {"CN", "CN_FUND"}:
        return "CN"
    if exchange == "XHKG" or market in {"HK", "XHKG"}:
        return "HK"
    if exchange in {"US", "XNAS", "XNYS", "ARCX"} or market == "US":
        return "US"
    return market


class FinancialNewsRefreshCoordinator:
    """Scan only approved RSS feeds and dispatch only target-matched candidates."""

    def __init__(
        self,
        database,
        *,
        settings=None,
        source_registry: Optional[IntelSourceRegistry] = None,
        candidate_repository: Optional[IntelCandidateRepository] = None,
        scanner: Optional[IntelLightScanner] = None,
        dispatcher: Optional[IntelCandidateDispatcher] = None,
        search_client=None,
        official_news_client=None,
        official_news_repository=None,
        clock=None,
    ):
        self.database = database
        self.settings = config if settings is None else settings
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sources = source_registry or IntelSourceRegistry(database)
        self.candidates = candidate_repository or IntelCandidateRepository(database)
        self.scanner = scanner or IntelLightScanner(
            candidate_repository=self.candidates,
            source_registry=self.sources,
        )
        self.dispatcher = dispatcher or IntelCandidateDispatcher(
            repository=self.candidates,
        )
        self.search_client = search_client or SerpAPIClient()
        self.official_news_client = official_news_client or HKEXNewsTitleSearchClient()
        self.official_news_repository = (
            official_news_repository or FinancialOfficialNewsRepository(database)
        )

    def _terms(self, target: Mapping[str, object]) -> tuple[str, ...]:
        instrument_id = int(target.get("instrument_id") or 0)
        terms = {
            str(target.get("canonical_symbol") or "").strip(),
            str(target.get("display_name") or "").strip(),
        }
        if instrument_id:
            with self.database.lock:
                row = self.database.connection.execute(
                    """
                    SELECT canonical_symbol, display_name, provider_mappings_json
                    FROM financial_instruments WHERE id=?
                    """,
                    (instrument_id,),
                ).fetchone()
                aliases = self.database.connection.execute(
                    """
                    SELECT alias FROM financial_instrument_aliases
                    WHERE instrument_id=?
                    """,
                    (instrument_id,),
                ).fetchall()
            if row is not None:
                terms.update((str(row[0] or ""), str(row[1] or "")))
                try:
                    mappings = json.loads(str(row[2] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    mappings = {}
                if isinstance(mappings, Mapping):
                    terms.update(str(value or "") for value in mappings.values())
            terms.update(str(item[0] or "") for item in aliases)
        canonical = str(target.get("canonical_symbol") or "").strip()
        if canonical:
            terms.add(canonical.rsplit(".", 1)[0])
        return tuple(
            sorted(
                {item.strip() for item in terms if item and item.strip()},
                key=lambda item: (-len(item), item),
            )
        )

    def _source_ids(self, target: Mapping[str, object]) -> tuple[int, ...]:
        self.sources.ensure_pack_default_sources("financial_markets")
        sources, _ = self.sources.list_sources(
            industry_pack_id="financial_markets",
            source_type="rss",
            is_enabled=True,
            page=1,
            per_page=100,
        )
        target_market = _target_market(target)
        allowed_markets = {
            "CN": {"CN", "CN_HK", "GLOBAL", ""},
            "HK": {"HK", "XHKG", "CN_HK", "GLOBAL", ""},
            "US": {"US", "GLOBAL", ""},
        }.get(target_market, {target_market, "GLOBAL", ""})
        matching = [
            source
            for source in sources
            if str(source.get("market") or "").strip().upper() in allowed_markets
        ]
        matching.sort(
            key=lambda source: (
                str(source.get("market") or "").strip().upper() != target_market,
                -int(source.get("authority_level") or 0),
                int(source.get("id") or 0),
            )
        )
        max_sources = max(
            1,
            min(
                int(
                    _setting(
                        self.settings,
                        "FINANCIAL_NEWS_REFRESH_MAX_SOURCES",
                        5,
                    )
                ),
                8,
            ),
        )
        return tuple(int(item["id"]) for item in matching[:max_sources])

    def _official_discovery_sources(
        self, target: Mapping[str, object]
    ) -> tuple[dict, ...]:
        target_market = _target_market(target)
        canonical = str(target.get("canonical_symbol") or "").strip().upper()
        allowed_markets = {
            "CN": {"CN", "GLOBAL", ""},
            "HK": {"HK", "XHKG", "GLOBAL", ""},
            "US": {"US", "GLOBAL", ""},
        }.get(target_market, {target_market, "GLOBAL", ""})
        collected = []
        for source_type in ("list_page", "website"):
            sources, _ = self.sources.list_sources(
                industry_pack_id="financial_markets",
                source_type=source_type,
                is_enabled=True,
                page=1,
                per_page=100,
            )
            for source in sources:
                metadata = source.get("metadata") or {}
                market = str(source.get("market") or "").strip().upper()
                role = str(metadata.get("source_role") or "")
                targets = {
                    str(item or "").strip().upper()
                    for item in metadata.get("target_symbols") or []
                }
                domains = [
                    str(item or "").strip().casefold().lstrip(".")
                    for item in metadata.get("approved_domains") or []
                    if str(item or "").strip()
                ]
                if (
                    market in allowed_markets
                    and role in {"exchange_official", "issuer_official"}
                    and str(metadata.get("approval_status") or "") == "approved"
                    and domains
                    and (not targets or canonical in targets)
                ):
                    collected.append(dict(source))
        collected.sort(
            key=lambda item: (
                str((item.get("metadata") or {}).get("source_role"))
                != "issuer_official",
                -int(item.get("authority_level") or 0),
                int(item.get("id") or 0),
            )
        )
        limit = max(
            1,
            min(
                int(
                    _setting(
                        self.settings,
                        "FINANCIAL_NEWS_DISCOVERY_MAX_SOURCES",
                        4,
                    )
                ),
                8,
            ),
        )
        return tuple(collected[:limit])

    @staticmethod
    def _url_allowed(url: object, source: Mapping[str, object]) -> bool:
        try:
            parsed = urlsplit(str(url or "").strip())
            host = (parsed.hostname or "").casefold().rstrip(".")
        except ValueError:
            return False
        if parsed.scheme.casefold() != "https" or not host:
            return False
        domains = (source.get("metadata") or {}).get("approved_domains") or []
        return any(
            host == str(domain).casefold().lstrip(".")
            or host.endswith("." + str(domain).casefold().lstrip("."))
            for domain in domains
            if str(domain).strip()
        )

    @staticmethod
    def _within_window(
        item: Mapping[str, object],
        start: Optional[datetime],
        cutoff: datetime,
    ) -> bool:
        raw = str(item.get("published_at") or "").strip()
        if not raw:
            # Unknown candidate time may be fetched, but the final article query
            # still requires a verifiable publication interval inside the window.
            return True
        try:
            if len(raw) == 10 and "T" not in raw:
                day = datetime.fromisoformat(raw).date()
                return (start is None or start.date() <= day) and day <= cutoff.date()
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return True
            observed = parsed.astimezone(UTC)
            return (start is None or start <= observed) and observed <= cutoff
        except ValueError:
            return True

    @staticmethod
    def _merge_discovery_stats(first: Mapping[str, object], second: Mapping[str, object]) -> dict:
        keys = {
            "searched_source_count",
            "failed_source_count",
            "matched_candidate_count",
            "reused_source_count",
            "persisted_count",
        }
        merged = {
            key: int(first.get(key) or 0) + int(second.get(key) or 0)
            for key in keys
        }
        reasons = []
        for item in list(first.get("reason_codes") or []) + list(
            second.get("reason_codes") or []
        ):
            value = str(item or "")
            if value and value not in reasons:
                reasons.append(value)
        merged["reason_codes"] = reasons
        return merged

    def _discover_hkex_official_items(
        self,
        *,
        target: Mapping[str, object],
        cutoff: datetime,
        lookback_days: int,
        latest_available: bool,
    ) -> dict:
        empty = {
            "searched_source_count": 0,
            "failed_source_count": 0,
            "matched_candidate_count": 0,
            "reused_source_count": 0,
            "persisted_count": 0,
            "reason_codes": [],
        }
        if _target_market(target) != "HK":
            return empty
        source = next(
            (
                item
                for item in self._official_discovery_sources(target)
                if str((item.get("metadata") or {}).get("source_role") or "")
                == "exchange_official"
                and any(
                    str(domain or "").casefold().lstrip(".") == "hkexnews.hk"
                    for domain in (item.get("metadata") or {}).get("approved_domains") or []
                )
            ),
            None,
        )
        if source is None:
            return {
                **empty,
                "reason_codes": ["no_approved_hkexnews_title_source"],
            }
        mode = "latest-available" if latest_available else "recent"
        window_key = (
            f"financial-hkexnews-title:{mode}:{int(source['id'])}:"
            f"{int(target.get('instrument_id') or 0)}:{cutoff.date().isoformat()}"
        )
        run_id, acquired = self.candidates.claim_scan_run(
            source_id=int(source["id"]),
            industry_pack_id="financial_markets",
            scanner_type="list_page",
            scan_window_key=window_key,
            requested_pack_ids=["financial_markets"],
            metadata={
                "refresh_version": FINANCIAL_NEWS_REFRESH_VERSION,
                "target_instrument_id": int(target.get("instrument_id") or 0),
                "selection_mode": mode,
                "official_metadata_only": True,
            },
        )
        if not acquired:
            return {
                **empty,
                "reused_source_count": 1,
                "reason_codes": ["hkexnews_title_search_window_reused"],
            }
        stats = {"status": "completed", "discovered_count": 0, "queued_count": 0}
        result = dict(empty)
        try:
            start = None
            if not latest_available:
                start = cutoff - timedelta(days=max(1, min(int(lookback_days), 30)))
            items = self.official_news_client.search(
                target.get("canonical_symbol"),
                cutoff=cutoff,
                start=start,
                max_items=1 if latest_available else 20,
            )
            result["searched_source_count"] = 1
            for item in items:
                if not self._url_allowed(item.get("url"), source):
                    continue
                if not self._within_window(item, start, cutoff):
                    continue
                observation = self.candidates.discover(
                    dict(item),
                    industry_pack_id="financial_markets",
                    source_id=int(source["id"]),
                    scan_run_id=run_id,
                    observation_type="list_page",
                    query_text=str(target.get("canonical_symbol") or ""),
                    force_unqueued=True,
                )
                stats["discovered_count"] += 1
                _item_id, created = self.official_news_repository.upsert(
                    instrument_id=int(target.get("instrument_id") or 0),
                    source_id=int(source["id"]),
                    source_key=str(source.get("name") or source.get("source_name") or "hkexnews"),
                    item=item,
                    fetched_at=self.clock().astimezone(UTC),
                )
                result["matched_candidate_count"] += 1
                result["persisted_count"] += int(created)
                if observation.get("candidate_id"):
                    stats["official_metadata_candidate_id"] = int(
                        observation["candidate_id"]
                    )
            result["reason_codes"] = [
                "controlled_hkexnews_latest_available_completed"
                if latest_available
                else "controlled_hkexnews_recent_search_completed"
            ]
            if not result["matched_candidate_count"]:
                result["reason_codes"].append("no_hkexnews_item_in_selected_window")
        except Exception as exc:
            result["failed_source_count"] = 1
            result["reason_codes"] = ["hkexnews_title_search_failed"]
            stats.update(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__[:100],
                    "error_message": sanitize_external_error(exc),
                }
            )
        finally:
            self.candidates.finish_scan_run(run_id, stats)
        return result

    def _discover_official_candidates(
        self,
        *,
        target: Mapping[str, object],
        terms: tuple[str, ...],
        cutoff: datetime,
        lookback_days: int,
        started: float,
        timeout_seconds: float,
        latest_available: bool = False,
    ) -> tuple[list[int], dict]:
        sources = self._official_discovery_sources(target)
        if not sources:
            return [], {
                "searched_source_count": 0,
                "failed_source_count": 0,
                "matched_candidate_count": 0,
                "reused_source_count": 0,
                "reason_codes": [
                    "no_approved_official_discovery_source",
                    (
                        "controlled_latest_available_search_unavailable"
                        if latest_available
                        else "controlled_official_search_unavailable"
                    ),
                ],
            }
        candidate_ids = []
        searched = failed = reused = matched = 0
        max_candidates = max(
            1,
            min(
                int(
                    _setting(
                        self.settings,
                        "FINANCIAL_NEWS_DISCOVERY_MAX_CANDIDATES",
                        12,
                    )
                ),
                25,
            ),
        )
        window_days = max(1, min(int(lookback_days), 7))
        window_start = None if latest_available else cutoff - timedelta(days=window_days)
        primary_term = str(target.get("display_name") or "").strip() or terms[0]
        canonical = str(target.get("canonical_symbol") or "").strip()
        for source in sources:
            if len(candidate_ids) >= max_candidates or time.monotonic() - started >= timeout_seconds:
                break
            metadata = source.get("metadata") or {}
            template = str(metadata.get("search_query_template") or "").strip()
            query = (template or "{target}").replace("{target}", primary_term)
            if canonical and canonical.casefold() not in query.casefold():
                query = f"{query} {canonical}"
            search_mode = "latest-available" if latest_available else "recent"
            window_key = (
                f"financial-news-search:{search_mode}:{int(source['id'])}:"
                f"{int(target.get('instrument_id') or 0)}:{cutoff.date().isoformat()}"
            )
            run_id, acquired = self.candidates.claim_scan_run(
                source_id=int(source["id"]),
                industry_pack_id="financial_markets",
                scanner_type="serpapi",
                scan_window_key=window_key,
                requested_pack_ids=["financial_markets"],
                metadata={
                    "refresh_version": FINANCIAL_NEWS_REFRESH_VERSION,
                    "target_instrument_id": int(target.get("instrument_id") or 0),
                    "controlled_official_discovery": True,
                    "selection_mode": search_mode,
                    "lookback_days": 0 if latest_available else window_days,
                },
            )
            if not acquired:
                reused += 1
                continue
            stats = {"status": "completed", "discovered_count": 0, "queued_count": 0}
            try:
                results = self.search_client.search(
                    query,
                    recency_days=0 if latest_available else window_days,
                )
                searched += 1
                for item in results:
                    if len(candidate_ids) >= max_candidates:
                        break
                    if not self._url_allowed(item.get("url"), source):
                        continue
                    if not self._within_window(item, window_start, cutoff):
                        continue
                    targets = {
                        str(value or "").strip().upper()
                        for value in metadata.get("target_symbols") or []
                    }
                    target_scoped = canonical.upper() in targets
                    if not target_scoped and not self._matches_target(item, terms):
                        continue
                    matched += 1
                    discovered = self.candidates.discover(
                        dict(item),
                        industry_pack_id="financial_markets",
                        source_id=int(source["id"]),
                        scan_run_id=run_id,
                        observation_type="serpapi",
                        query_text=query,
                        bypass_industry_gate=True,
                    )
                    stats["discovered_count"] += 1
                    if discovered.get("should_queue") and discovered.get("candidate_id"):
                        candidate_ids.append(int(discovered["candidate_id"]))
                        stats["queued_count"] += 1
            except Exception as exc:
                failed += 1
                stats.update(
                    {
                        "status": "failed",
                        "error_type": type(exc).__name__[:100],
                        "error_message": sanitize_external_error(exc),
                    }
                )
            finally:
                self.candidates.finish_scan_run(run_id, stats)
        reasons = [
            "controlled_latest_available_search_completed"
            if latest_available
            else "controlled_official_search_completed"
        ]
        if reused:
            reasons.append("official_search_window_reused")
        if failed:
            reasons.append("one_or_more_official_searches_failed")
        if not candidate_ids:
            reasons.append("no_target_matched_official_search_candidates")
        return candidate_ids, {
            "searched_source_count": searched,
            "failed_source_count": failed,
            "matched_candidate_count": matched,
            "reused_source_count": reused,
            "reason_codes": reasons,
        }

    @staticmethod
    def _matches_target(item: Mapping[str, object], terms: tuple[str, ...]) -> bool:
        haystack = _normalize_text(
            " ".join(
                (
                    str(item.get("title") or ""),
                    str(item.get("summary") or item.get("snippet") or ""),
                )
            )
        )
        return any(_term_occurs(term, haystack) for term in terms)

    def refresh(
        self,
        *,
        target: Mapping[str, object],
        cutoff_at_utc: str,
        lookback_days: int,
        latest_available_only: bool = False,
    ) -> dict:
        try:
            cutoff = datetime.fromisoformat(str(cutoff_at_utc).replace("Z", "+00:00"))
            if cutoff.tzinfo is None or cutoff.utcoffset() is None:
                raise ValueError
            cutoff = cutoff.astimezone(UTC)
        except ValueError:
            return {
                "status": "failed",
                "inserted_count": 0,
                "scanned_source_count": 0,
                "matched_candidate_count": 0,
                "reason_codes": ["invalid_news_cutoff"],
            }
        started = time.monotonic()
        timeout_seconds = max(
            1.0,
            min(
                float(
                    _setting(
                        self.settings,
                        "FINANCIAL_NEWS_REFRESH_TIMEOUT_SECONDS",
                        10,
                    )
                ),
                30.0,
            ),
        )
        max_items = max(
            1,
            min(
                int(
                    _setting(
                        self.settings,
                        "FINANCIAL_NEWS_REFRESH_ITEMS_PER_SOURCE",
                        20,
                    )
                ),
                50,
            ),
        )
        terms = self._terms(target)
        self.sources.ensure_pack_default_sources("financial_markets")
        source_ids = () if latest_available_only else self._source_ids(target)
        if not terms:
            return {
                "status": "skipped",
                "inserted_count": 0,
                "scanned_source_count": 0,
                "matched_candidate_count": 0,
                "reason_codes": ["target_terms_missing"],
            }
        authorized = (
            self.scanner._enabled_sources(
                "financial_markets",
                source_ids,
                len(source_ids),
            )
            if source_ids
            else []
        )
        candidate_ids = []
        scanned = reused = failed = 0
        timed_out = False
        for source in authorized:
            if time.monotonic() - started >= timeout_seconds:
                timed_out = True
                break
            window_key = source_scan_window_key(source)
            run_id, acquired = self.candidates.claim_scan_run(
                source_id=int(source["id"]),
                industry_pack_id="financial_markets",
                scanner_type="rss",
                scan_window_key=window_key,
                requested_pack_ids=["financial_markets"],
                metadata={
                    "refresh_version": FINANCIAL_NEWS_REFRESH_VERSION,
                    "target_instrument_id": int(target.get("instrument_id") or 0),
                },
            )
            if not acquired:
                reused += 1
                continue
            stats = {
                "status": "completed",
                "discovered_count": 0,
                "queued_count": 0,
                "target_matched_count": 0,
            }
            try:
                items = self.scanner.rss_scanner.scan(source, limit=max_items)
                scanned += 1
                for item in items:
                    if not self._within_window(
                        item,
                        cutoff - timedelta(
                            days=max(1, min(int(lookback_days), 7))
                        ),
                        cutoff,
                    ):
                        continue
                    if not self._matches_target(item, terms):
                        continue
                    stats["target_matched_count"] += 1
                    result = self.candidates.discover(
                        dict(item),
                        industry_pack_id="financial_markets",
                        source_id=int(source["id"]),
                        scan_run_id=run_id,
                        observation_type="rss",
                        query_text=str(target.get("canonical_symbol") or ""),
                        bypass_industry_gate=True,
                    )
                    stats["discovered_count"] += 1
                    if result.get("should_queue") and result.get("candidate_id"):
                        stats["queued_count"] += 1
                        candidate_ids.append(int(result["candidate_id"]))
            except Exception as exc:
                failed += 1
                stats.update(
                    {
                        "status": "failed",
                        "error_type": type(exc).__name__[:100],
                        "error_message": sanitize_external_error(exc),
                    }
                )
            finally:
                self.candidates.finish_scan_run(run_id, stats)

        search_stats = {
            "searched_source_count": 0,
            "failed_source_count": 0,
            "matched_candidate_count": 0,
            "reused_source_count": 0,
            "persisted_count": 0,
            "reason_codes": [],
        }
        if not candidate_ids and not timed_out:
            direct_stats = self._discover_hkex_official_items(
                target=target,
                cutoff=cutoff,
                lookback_days=lookback_days,
                latest_available=latest_available_only,
            )
            search_stats = self._merge_discovery_stats(search_stats, direct_stats)
            if not direct_stats["matched_candidate_count"]:
                discovered, generic_stats = self._discover_official_candidates(
                    target=target,
                    terms=terms,
                    cutoff=cutoff,
                    lookback_days=lookback_days,
                    started=started,
                    timeout_seconds=timeout_seconds,
                    latest_available=latest_available_only,
                )
                candidate_ids.extend(discovered)
                search_stats = self._merge_discovery_stats(
                    search_stats, generic_stats
                )
        if (
            not latest_available_only
            and not candidate_ids
            and not search_stats["matched_candidate_count"]
            and not timed_out
            and time.monotonic() - started < timeout_seconds
        ):
            latest_direct = self._discover_hkex_official_items(
                target=target,
                cutoff=cutoff,
                lookback_days=lookback_days,
                latest_available=True,
            )
            search_stats = self._merge_discovery_stats(
                search_stats, latest_direct
            )
            if not latest_direct["matched_candidate_count"]:
                latest_ids, latest_stats = self._discover_official_candidates(
                    target=target,
                    terms=terms,
                    cutoff=cutoff,
                    lookback_days=lookback_days,
                    started=started,
                    timeout_seconds=timeout_seconds,
                    latest_available=True,
                )
                candidate_ids.extend(latest_ids)
                search_stats = self._merge_discovery_stats(
                    search_stats, latest_stats
                )
        candidate_ids = list(dict.fromkeys(candidate_ids))
        dispatch = (
            self.dispatcher.dispatch_once(
                limit=len(candidate_ids),
                manual=True,
                candidate_ids=candidate_ids,
            )
            if candidate_ids and not timed_out
            else {"crawled": 0, "failed": 0}
        )
        reasons = [
            "controlled_latest_available_refresh_completed"
            if latest_available_only
            else "authorized_target_filtered_rss_refresh_completed"
        ]
        if not source_ids and not latest_available_only:
            reasons.append("no_market_matched_authorized_rss_source")
        if reused:
            reasons.append("rss_scan_window_reused")
        if failed:
            reasons.append("one_or_more_rss_sources_failed")
        if not candidate_ids and not search_stats["matched_candidate_count"]:
            reasons.append("no_target_matched_rss_candidates")
        reasons.extend(search_stats["reason_codes"])
        if timed_out:
            reasons.append("news_refresh_timeout_budget_reached")
        return {
            "status": "timed_out" if timed_out else "completed",
            "inserted_count": max(0, int(dispatch.get("crawled") or 0))
            + max(0, int(search_stats.get("persisted_count") or 0)),
            "scanned_source_count": scanned,
            "reused_source_count": reused,
            "failed_source_count": failed,
            "matched_candidate_count": len(candidate_ids)
            + int(search_stats.get("matched_candidate_count") or 0),
            "official_search_source_count": search_stats["searched_source_count"],
            "official_search_failed_source_count": search_stats["failed_source_count"],
            "official_search_candidate_count": search_stats["matched_candidate_count"],
            "official_search_reused_source_count": search_stats["reused_source_count"],
            "dispatched_candidate_count": int(dispatch.get("claimed") or 0),
            "reason_codes": reasons,
        }


__all__ = [
    "FINANCIAL_NEWS_REFRESH_VERSION",
    "FinancialNewsRefreshCoordinator",
]
