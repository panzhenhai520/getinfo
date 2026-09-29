#!/usr/bin/env python3
"""Validate and import the documented automotive RSS source catalogue."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable

import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from industry_pack_admin import IndustryPackAdminService
from industry_packs import IndustryPackLoader, unique_normalized_keywords
from intel_api import industry_pack_admin_service
from intel_http import SafeHTTPClient, sanitize_external_error
from rss_feed_contract import validate_rss_feed_response


PACK_ID = "automotive"
TARGET_PACK_VERSION = "1.1.0"
CATALOGUE_PATH = ROOT / "docs" / "汽车行业_RSS信源_AI导入清单.md"
USER_AGENT = "Automotive-Industry-RSS-Collector/1.0 (+site-admin)"
INACTIVE_DAYS = 180

ROLE_MAP = {
    "government_regulator": "government_regulator",
    "official_investigation": "official_investigation",
    "independent_research": "independent_research",
    "independent_safety_research": "independent_research",
    "industry_association": "industry_association",
    "engineering_media": "engineering_media",
    "professional_trade_media": "professional_trade_media",
    "automotive_media": "consumer_media",
    "consumer_automotive_media": "consumer_media",
}

TOPIC_ROUTE_MAP = {
    "intelligent_driving": "intelligent_driving",
    "intelligent_connected_and_sdv": "intelligent_connected_vehicle",
    "vehicle_risk_and_repairability": "vehicle_risk_insurability",
    "new_energy_battery_thermal": "new_energy_battery_thermal",
    "offroad_and_all_terrain": "offroad_performance",
    "embodied_intelligent_driving": "embodied_intelligent_driving",
    "evtol_and_low_altitude_aircraft": "low_altitude_aircraft_testing",
    "automotive_policy_regulation_standards": (
        "automotive_policy_regulation_standards"
    ),
}

COMBINED_ENGINEERING_TOPIC_KEYS = (
    "nvh_acoustics",
    "active_noise_control",
    "wind_tunnel_aerodynamics",
    "electronic_control_simulation",
)

AUTOMOTIVE_SERPAPI_QUERIES = [
    '汽车 ("NVH技术" OR "汽车NVH" OR "声振粗糙度" OR "ANC主动降噪技术" OR "ANC主动降噪" OR "汽车主动降噪" OR "主动噪声控制")',
    '汽车 ("风洞测试技术" OR "汽车风洞" OR "空气动力学测试" OR "电控仿真开发技术" OR "汽车电控仿真" OR "电子控制系统仿真" OR "硬件在环")',
    '汽车 ("车辆风险等级开发技术" OR "车辆风险等级" OR "智能驾驶风险评估" OR "科技越野属性开发技术" OR "越野智能化技术" OR "越野性能开发" OR "全地形控制")',
    '汽车 ("智能驾驶" OR "自动驾驶" OR "智能网联汽车" OR "车联网") (技术 OR 测试 OR 标准 OR 政策)',
    '汽车 ("具身智能驾驶" OR "汽车具身智能" OR "车载智能体技术" OR "汽车人形机器人" OR "驾驶世界模型")',
    '(eVTOL OR "eVTOL测试" OR "低空飞行器" OR "低空飞行器测试技术" OR "新型飞行器测试" OR "电动垂直起降航空器") (测试 OR 适航 OR 试飞 OR 标准)',
]

AUTOMOTIVE_SERPAPI_QUERY_GATES = {
    AUTOMOTIVE_SERPAPI_QUERIES[0]: [
        "NVH", "声振粗糙度", "ANC主动降噪", "汽车主动降噪", "主动噪声控制",
    ],
    AUTOMOTIVE_SERPAPI_QUERIES[1]: [
        "风洞", "空气动力学", "电控仿真", "电子控制系统仿真",
        "硬件在环", "HIL", "XiL", "虚拟ECU",
    ],
    AUTOMOTIVE_SERPAPI_QUERIES[2]: [
        "车辆风险等级", "智能驾驶风险评估", "科技越野", "越野智能化",
        "越野性能", "全地形控制", "地形可通行性",
    ],
    AUTOMOTIVE_SERPAPI_QUERIES[3]: [
        "智能驾驶", "自动驾驶", "智能网联", "车联网", "车路协同", "C-V2X",
    ],
    AUTOMOTIVE_SERPAPI_QUERIES[4]: [
        "具身智能驾驶", "汽车具身智能", "车载智能体技术",
        "汽车人形机器人", "驾驶世界模型",
    ],
    AUTOMOTIVE_SERPAPI_QUERIES[5]: [
        "eVTOL", "低空飞行器", "新型飞行器", "电动垂直起降航空器",
    ],
}

AUTOMOTIVE_EXACT_TOPIC_KEYWORDS = {
    "nvh_acoustics": ["NVH技术", "噪声、振动与声振粗糙度", "声振粗糙度"],
    "active_noise_control": ["ANC主动降噪技术", "主动噪声控制"],
    "wind_tunnel_aerodynamics": ["风洞测试技术", "空气动力学测试"],
    "low_altitude_aircraft_testing": [
        "低空飞行器测试技术", "新型飞行器测试",
    ],
    "electronic_control_simulation": [
        "电控仿真开发技术", "电子控制系统仿真",
    ],
    "vehicle_risk_insurability": [
        "车辆风险等级开发技术", "智能驾驶风险评估",
    ],
    "offroad_performance": [
        "科技越野属性开发技术", "越野智能化技术",
    ],
    "intelligent_driving": ["智能驾驶", "自动驾驶"],
    "intelligent_connected_vehicle": ["智能网联", "车联网"],
    "embodied_intelligent_driving": [
        "具身智能", "汽车机器人", "车载智能体技术",
    ],
}


def load_catalogue(path: Path = CATALOGUE_PATH) -> dict:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"```yaml\s*(.*?)\s*```", text, flags=re.DOTALL)
    if not match:
        raise ValueError("汽车 RSS 清单缺少 YAML 配置块")
    payload = yaml.safe_load(match.group(1))
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), list):
        raise ValueError("汽车 RSS 清单 YAML 结构无效")
    return payload


def _utc_text(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")


def _validation_failure(source: dict, error: Exception) -> dict:
    message = sanitize_external_error(error)
    status_match = re.search(r"\b([45]\d\d)\b", message)
    http_status = int(status_match.group(1)) if status_match else None
    if http_status == 403:
        status = "blocked_403"
    elif any(term in message.casefold() for term in ("captcha", "验证码", "login")):
        status = "manual_review_required"
    else:
        status = "validation_failed"
    return {
        "source_id": source["id"],
        "validation_status": status,
        "validation_checked_at": _utc_text(),
        "http_status": http_status,
        "final_url": str(source.get("feed_url") or ""),
        "entry_count": 0,
        "latest_published_at": "",
        "redirected": False,
        "error": message,
    }


def validate_feed(source: dict, *, client_factory=SafeHTTPClient) -> dict:
    feed_url = str(source.get("feed_url") or "").strip()
    if not feed_url:
        return {
            "source_id": source["id"],
            "validation_status": "directory_only",
            "validation_checked_at": _utc_text(),
            "http_status": None,
            "final_url": str(source.get("directory_url") or ""),
            "entry_count": 0,
            "latest_published_at": "",
            "redirected": False,
            "error": "RSS目录网页，不作为 XML Feed 导入",
        }
    try:
        response = client_factory().get(
            feed_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": (
                    "application/rss+xml, application/atom+xml, application/xml, "
                    "text/xml;q=0.9, text/html;q=0.5"
                ),
            },
        )
        accepted = validate_rss_feed_response(response)
        latest = datetime.fromisoformat(
            accepted["latest_published_at"].replace("Z", "+00:00")
        )
        inactive = latest < datetime.now(timezone.utc) - timedelta(days=INACTIVE_DAYS)
        return {
            "source_id": source["id"],
            "validation_status": "inactive_suspected" if inactive else "active",
            "validation_checked_at": _utc_text(),
            "http_status": int(response.status_code),
            "validation_content_type": str(response.content_type or ""),
            "final_url": response.url,
            "entry_count": int(accepted["entry_count"]),
            "latest_published_at": accepted["latest_published_at"],
            "sample_title": accepted["sample_title"],
            "sample_url": accepted["sample_url"],
            "redirected": response.url != feed_url,
            "error": "",
        }
    except Exception as exc:
        return _validation_failure(source, exc)


def validate_catalogue_sources(
    sources: Iterable[dict], *, client_factory=SafeHTTPClient, max_workers: int = 5
) -> Dict[str, dict]:
    source_list = list(sources)
    results: Dict[str, dict] = {}
    directories = [item for item in source_list if not item.get("feed_url")]
    for source in directories:
        result = validate_feed(source, client_factory=client_factory)
        results[result["source_id"]] = result
    feeds = [item for item in source_list if item.get("feed_url")]
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 5))) as executor:
        futures = {
            executor.submit(validate_feed, source, client_factory=client_factory): source
            for source in feeds
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # defensive boundary around worker failures
                result = _validation_failure(source, exc)
            results[result["source_id"]] = result
    return results


def _source_url(source: dict, validation: dict) -> tuple[str, str]:
    mode = str(source.get("import_mode") or "")
    feed_url = str(source.get("feed_url") or "")
    status = validation["validation_status"]
    if mode == "rss_directory":
        return str(source.get("directory_url") or source.get("homepage_url")), "list_page"
    if mode == "conditional_rss" and status != "active":
        return str(source.get("homepage_url") or feed_url), "list_page"
    return feed_url, "rss"


def build_manifest_source(source: dict, validation: dict) -> dict:
    role = ROLE_MAP.get(str(source.get("source_type") or ""), "unclassified")
    url, source_type = _source_url(source, validation)
    status = validation["validation_status"]
    enabled = bool(source.get("enabled_by_default")) and status == "active"
    item = {
        "name": str(source.get("name") or source["id"]),
        "url": url,
        "source_type": source_type,
        "content_type": "official" if role in {
            "government_regulator", "official_investigation"
        } else ("media" if role in {
            "engineering_media", "professional_trade_media", "consumer_media"
        } else "report"),
        "source_role": role,
        "authority_level": max(1, min(5, int(source.get("evidence_weight") or 1))),
        "polling_interval_minutes": 360 if source_type == "rss" else 1440,
        "is_enabled": enabled,
        "language": ",".join(source.get("language") or []),
        "source_import_id": source["id"],
        "organization": str(source.get("organization") or ""),
        "country_or_region": str(source.get("country_or_region") or ""),
        "import_mode": str(source.get("import_mode") or ""),
        "feed_url": str(source.get("feed_url") or ""),
        "directory_url": str(source.get("directory_url") or ""),
        "homepage_url": str(source.get("homepage_url") or ""),
        "topics": list(source.get("topics") or []),
        "required_filter_any": list(source.get("required_filter_any") or []),
        "deduplication_group": str(source.get("deduplication_group") or ""),
        "viewpoint_label": str(source.get("viewpoint_label") or ""),
        **copy.deepcopy(validation),
    }
    item.pop("source_id", None)
    return item


def _merge_unique(existing: Iterable[str], additional: Iterable[str]) -> list[str]:
    return unique_normalized_keywords(list(existing) + list(additional))


def merge_topic_routes(manifest: dict, catalogue: dict) -> None:
    topics = {
        str(item["key"]): copy.deepcopy(item)
        for item in manifest.get("fixed_topics") or []
    }
    for route in catalogue.get("topic_routes") or []:
        route_id = str(route.get("topic_id") or "")
        if route_id == "nvh_anc_wind_tunnel_controls":
            for key in COMBINED_ENGINEERING_TOPIC_KEYS:
                topic = topics[key]
                topic["preferred_source_ids"] = _merge_unique(
                    topic.get("preferred_source_ids") or [],
                    route.get("preferred_source_ids") or [],
                )
            continue
        target_key = TOPIC_ROUTE_MAP.get(route_id)
        if not target_key:
            continue
        topic = topics.get(target_key) or {
            "key": target_key,
            "name": str(route.get("name") or target_key),
            "keywords": [],
        }
        topic["keywords"] = _merge_unique(
            topic.get("keywords") or [], route.get("required_topic_terms_any") or []
        )
        topic["preferred_source_ids"] = _merge_unique(
            topic.get("preferred_source_ids") or [],
            route.get("preferred_source_ids") or [],
        )
        if route.get("required_vehicle_context_any"):
            topic["required_vehicle_context_any"] = _merge_unique(
                topic.get("required_vehicle_context_any") or [],
                route["required_vehicle_context_any"],
            )
        topics[target_key] = topic
    manifest["fixed_topics"] = list(topics.values())


def build_imported_manifest(
    current_manifest: dict, catalogue: dict, validations: Dict[str, dict]
) -> dict:
    manifest = copy.deepcopy(current_manifest)
    manifest["pack_version"] = TARGET_PACK_VERSION
    source_by_import_id = {
        str(item.get("source_import_id") or ""): item
        for item in manifest.get("default_sources") or []
        if item.get("source_import_id")
    }
    retained = [
        item
        for item in manifest.get("default_sources") or []
        if not item.get("source_import_id")
    ]
    imported = []
    for source in catalogue["sources"]:
        built = build_manifest_source(source, validations[source["id"]])
        previous = source_by_import_id.get(source["id"])
        if previous:
            preserved_manual = {
                key: previous[key]
                for key in ("is_enabled",)
                if key in previous and previous.get("enabled_is_manual")
            }
            built.update(preserved_manual)
        imported.append(built)
    manifest["default_sources"] = retained + imported

    context = catalogue["industry_package"]["global_required_context_any"]
    strong_anchors = [
        value for value in context
        if value.casefold() not in {"车辆", "vehicle"}
    ]
    weak_anchors = [value for value in context if value.casefold() in {"车辆", "vehicle"}]
    manifest["core_keywords"] = _merge_unique(
        manifest.get("core_keywords") or [], strong_anchors
    )
    manifest["expanded_keywords"] = _merge_unique(
        manifest.get("expanded_keywords") or [], weak_anchors
    )
    manifest["negative_keywords"] = _merge_unique(
        manifest.get("negative_keywords") or [],
        catalogue["industry_package"]["global_exclude_terms"],
    )
    manifest["serpapi_queries"] = list(AUTOMOTIVE_SERPAPI_QUERIES)
    manifest["serpapi_query_gates"] = copy.deepcopy(
        AUTOMOTIVE_SERPAPI_QUERY_GATES
    )
    merge_topic_routes(manifest, catalogue)
    topics = {
        str(topic.get("key") or ""): topic
        for topic in manifest.get("fixed_topics") or []
    }
    exact_keywords = []
    for topic_key, keywords in AUTOMOTIVE_EXACT_TOPIC_KEYWORDS.items():
        topic = topics.get(topic_key)
        if not topic:
            continue
        topic["keywords"] = _merge_unique(topic.get("keywords") or [], keywords)
        exact_keywords.extend(keywords)
    manifest["expanded_keywords"] = _merge_unique(
        manifest.get("expanded_keywords") or [], exact_keywords
    )
    manifest["rss_import_policy"] = {
        "catalogue": str(CATALOGUE_PATH.relative_to(ROOT)),
        "catalogue_version": str(
            catalogue.get("industry_package", {}).get("version") or ""
        ),
        "source_authority_does_not_bypass_industry_gate": True,
        "do_not_bypass_403_or_captcha": True,
        "inactive_check_days": INACTIVE_DAYS,
        "deduplication": copy.deepcopy(catalogue.get("deduplication") or {}),
        "web_search_supplements": copy.deepcopy(
            catalogue.get("web_search_supplements") or []
        ),
    }
    return manifest


def summarize(validations: Dict[str, dict], manifest: dict) -> dict:
    status_counts: Dict[str, int] = {}
    for result in validations.values():
        status = result["validation_status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    imported = [
        item for item in manifest["default_sources"] if item.get("source_import_id")
    ]
    return {
        "pack_id": PACK_ID,
        "pack_version": manifest["pack_version"],
        "catalogue_source_count": len(validations),
        "total_source_count": len(manifest["default_sources"]),
        "imported_source_count": len(imported),
        "enabled_imported_source_count": sum(
            1 for item in imported if item.get("is_enabled")
        ),
        "topic_count": len(manifest["fixed_topics"]),
        "status_counts": dict(sorted(status_counts.items())),
        "validations": [validations[key] for key in sorted(validations)],
        "access_controls_bypassed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    if args.publish and not args.apply:
        parser.error("--publish requires --apply")

    catalogue = load_catalogue()
    validations = validate_catalogue_sources(catalogue["sources"])
    draft = industry_pack_admin_service.get_or_create_draft(PACK_ID, actor="codex-rss-import")
    manifest = build_imported_manifest(draft["manifest"], catalogue, validations)
    # Live feed validation has already happened. The admin validator still
    # enforces the manifest/source contract; this scoped validator avoids a
    # second DNS pass over all retained legacy sources.
    admin = IndustryPackAdminService(
        industry_pack_admin_service.store,
        industry_pack_admin_service.loader,
        url_validator=lambda value: str(value),
    )
    normalized = admin.validate_manifest(PACK_ID, manifest)
    result = summarize(validations, normalized)
    result["applied"] = bool(args.apply)
    result["published"] = False
    result["previous_revision"] = int(draft["revision"])
    if args.apply:
        saved = admin.save_draft(
            PACK_ID,
            normalized,
            expected_revision=int(draft["revision"]),
            actor="codex-rss-import",
        )
        result["draft_revision"] = int(saved["revision"])
        if args.publish:
            published = admin.publish_draft(
                PACK_ID,
                expected_revision=int(saved["revision"]),
                actor="codex-rss-import",
            )
            result["published"] = True
            result["published_version_id"] = int(published["id"])
            result["published_version_number"] = int(published["version_number"])
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
