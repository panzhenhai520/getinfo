#!/usr/bin/env python3
"""Read-only acceptance check for the automotive fixed-topic draft."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from intel_api import industry_pack_admin_service
from intel_classifier import classify_article
from intel_database import intel_repository


EXPECTED_TOPICS = {
    "nvh_acoustics",
    "active_noise_control",
    "wind_tunnel_aerodynamics",
    "low_altitude_aircraft_testing",
    "electronic_control_simulation",
    "vehicle_risk_insurability",
    "offroad_performance",
    "intelligent_driving",
    "intelligent_connected_vehicle",
    "embodied_intelligent_driving",
    "functional_safety",
    "automotive_cybersecurity",
    "new_energy_battery_thermal",
    "automotive_policy_regulation_standards",
}

EXPECTED_SOURCES = {
    "https://www.sae.org/news/": ("industry_association", 4),
    "https://www.asam.net/standards/": ("industry_association", 4),
    "http://www.caam.org.cn/": ("industry_association", 4),
    "https://www.sae-china.org/": ("industry_association", 4),
    "https://www.catarc.org.cn/xwdt/gzdt/": ("independent_research", 4),
    "https://unece.org/transport/vehicle-regulations": (
        "government_regulator",
        5,
    ),
    "https://www.nhtsa.gov/press-releases?page=0": (
        "government_regulator",
        5,
    ),
}

EXPECTED_RSS_IMPORT_IDS = {
    "miit_rss_directory",
    "federal_register_nhtsa",
    "ntsb_press_releases",
    "ntsb_investigations",
    "ntsb_reports",
    "acea_main",
    "icct_main",
    "iihs_hldi",
    "ieee_transportation",
    "electrive_main",
    "green_car_reports_main",
    "green_car_reports_ev",
    "edmunds_car_news",
    "edmunds_articles",
    "motor_authority_main",
}


def run() -> dict:
    draft = industry_pack_admin_service.store.draft("automotive")
    published = industry_pack_admin_service.store.latest_published("automotive")
    record = draft or published
    if not record:
        return {"passed": False, "error": "automotive draft/version is missing"}
    pack = industry_pack_admin_service.validate_manifest(
        "automotive", record["manifest"]
    )
    topic_keys = {topic["key"] for topic in pack.get("fixed_topics") or []}
    sources = {
        source["url"]: (
            source.get("source_role"),
            int(source.get("authority_level") or 0),
        )
        for source in pack.get("default_sources") or []
    }
    imported = {
        str(source.get("source_import_id") or ""): source
        for source in pack.get("default_sources") or []
        if source.get("source_import_id")
    }
    example = classify_article(
        {
            "title": "某车型主动道路噪声控制系统正式量产",
            "content": "该汽车技术已经完成整车验证并进入量产阶段。",
        },
        pack,
    )
    topic_samples = {
        "wind_tunnel_aerodynamics": "汽车风洞测试标准发布",
        "low_altitude_aircraft_testing": "eVTOL测试完成首次试飞",
        "electronic_control_simulation": "汽车电控仿真平台正式发布",
        "vehicle_risk_insurability": "车辆风险等级评估标准发布",
        "offroad_performance": "越野性能开发技术路线发布",
        "intelligent_driving": "自动驾驶系统正式量产",
        "intelligent_connected_vehicle": "智能网联汽车标准发布",
        "embodied_intelligent_driving": "具身智能驾驶技术路线发布",
        "functional_safety": "汽车功能安全测试标准发布",
        "automotive_cybersecurity": "汽车网络安全标准发布",
    }
    sample_results = {}
    for expected_key, title in topic_samples.items():
        classified = classify_article({"title": title, "content": ""}, pack)
        sample_results[expected_key] = {
            "content_type": classified["final_category"],
            "topic_keys": classified["topic_keys"],
            "matched": expected_key in classified["topic_keys"],
        }
    rejected = classify_article(
        {"title": "新型无人机完成首次首飞", "content": "通用航空活动。"},
        pack,
    )
    active = intel_repository.active_industry_pack_id()
    imported_enabled = {
        source_id
        for source_id, source in imported.items()
        if bool(source.get("is_enabled"))
    }
    validation_statuses = {
        source_id: str(source.get("validation_status") or "")
        for source_id, source in imported.items()
    }
    passed = bool(
        pack["pack_version"] == "1.1.0"
        and topic_keys == EXPECTED_TOPICS
        and all(sources.get(url) == profile for url, profile in EXPECTED_SOURCES.items())
        and len(sources) == 22
        and set(imported) == EXPECTED_RSS_IMPORT_IDS
        and imported_enabled == {"ieee_transportation", "electrive_main"}
        and all(
            validation_statuses[source_id] == "active"
            for source_id in imported_enabled
        )
        and validation_statuses["miit_rss_directory"] == "directory_only"
        and imported["miit_rss_directory"]["source_type"] == "list_page"
        and not imported["miit_rss_directory"]["is_enabled"]
        and validation_statuses["icct_main"] == "active"
        and not imported["icct_main"]["is_enabled"]
        and not any(
            source["is_enabled"]
            for source in imported.values()
            if source.get("validation_status") != "active"
        )
        and bool(published)
        and published["pack_version"] == "1.1.0"
        and example["final_category"] == "event"
        and set(example["topic_tags"]) == {"NVH与声学", "主动降噪"}
        and example["score_details"]["components"]["core"] >= 3
        and example["score_details"]["components"]["expanded"] >= 1
        and example["score_details"]["components"]["event"] >= 2
        and all(item["matched"] for item in sample_results.values())
        and rejected["topic_tags"] == []
        and active == "family_office"
    )
    return {
        "passed": passed,
        "draft_revision": int(draft["revision"]) if draft else None,
        "topic_count": len(topic_keys),
        "source_count": len(sources),
        "imported_source_count": len(imported),
        "enabled_imported_sources": sorted(imported_enabled),
        "validation_statuses": validation_statuses,
        "published_version_id": int(published["id"]) if published else None,
        "published_version_number": (
            int(published["version_number"]) if published else None
        ),
        "sources": sources,
        "example": {
            "content_type": example["final_category"],
            "topic_tags": example["topic_tags"],
            "components": example["score_details"]["components"],
        },
        "sample_results": sample_results,
        "rejected_topic_tags": rejected["topic_tags"],
        "active_industry_pack_id": active,
    }


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
