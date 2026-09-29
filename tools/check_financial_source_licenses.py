#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Stage 6.1 provider/RSS authorization, cost and endpoint reconciliation gate."""

from __future__ import annotations

import argparse
import io
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from financial_provider_router import EMBEDDED_PROVIDER_IDS
from financial_source_license import (
    load_license_catalog,
    provider_authorization_decision,
    provider_license_profile,
    rss_authorization_decision,
    rss_license_profile,
)
from intel_sources import canonicalize_source_url


PROVIDER_IMPLEMENTATIONS = {
    provider_id: ROOT / "financial_providers" / f"{provider_id}.py"
    for provider_id in EMBEDDED_PROVIDER_IDS
}
PROVIDER_IMPLEMENTATIONS["yahoo"] = ROOT / "financial_providers" / "yahoo.py"


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def _provider_runtime_profiles() -> dict[str, dict]:
    return {
        provider_id: json.loads(
            (ROOT / "config" / "financial_providers" / f"{provider_id}.json").read_text(
                encoding="utf-8"
            )
        )
        for provider_id in EMBEDDED_PROVIDER_IDS
    }


def _financial_rss_specs() -> list[dict]:
    pack = json.loads(
        (ROOT / "config" / "industry_packs" / "financial_markets.json").read_text(
            encoding="utf-8"
        )
    )
    return [
        dict(item)
        for item in pack.get("default_sources") or []
        if item.get("source_type") == "rss"
    ]


def endpoint_reconciliation() -> dict:
    runtime_profiles = _provider_runtime_profiles()
    providers = {}
    missing_markers = {}
    for provider_id, runtime in runtime_profiles.items():
        source = PROVIDER_IMPLEMENTATIONS[provider_id].read_text(encoding="utf-8")
        endpoints = sorted({
            str(endpoint)
            for values in (runtime.get("logical_endpoints") or {}).values()
            for endpoint in values
        })
        missing = [endpoint for endpoint in endpoints if endpoint not in source]
        if missing:
            missing_markers[provider_id] = missing
        providers[provider_id] = {
            "implementation": str(PROVIDER_IMPLEMENTATIONS[provider_id].relative_to(ROOT)),
            "configured_endpoints": endpoints,
            "missing_implementation_markers": missing,
        }

    catalog = load_license_catalog()
    rss_specs = _financial_rss_specs()
    rss = {}
    for spec in rss_specs:
        canonical = canonicalize_source_url(spec["url"])
        profile = rss_license_profile(
            spec["url"], str(spec.get("license_profile") or ""), catalog=catalog
        )
        rss[profile["source_id"]] = {
            "profile_id": profile["profile_id"],
            "configured_endpoint": canonical,
            "profile_endpoint": profile["canonical_source_url"],
            "matched": canonical == profile["canonical_source_url"],
        }
    _assert(not missing_markers, {"missing_endpoint_markers": missing_markers})
    _assert(all(item["matched"] for item in rss.values()), rss)
    return {"providers": providers, "rss": rss}


def static_acceptance(*, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    catalog = load_license_catalog()
    profiles = catalog["profiles"]
    runtime_profiles = _provider_runtime_profiles()
    rss_specs = _financial_rss_specs()
    provider_profiles = {
        provider_id: provider_license_profile(
            provider_id,
            provider_profile=runtime_profiles[provider_id],
            catalog=catalog,
        )
        for provider_id in EMBEDDED_PROVIDER_IDS
    }
    rss_profiles = {
        str(spec["license_profile"]): rss_license_profile(
            spec["url"], spec["license_profile"], catalog=catalog
        )
        for spec in rss_specs
    }
    production_without_approval = {
        provider_id: provider_authorization_decision(
            provider_id,
            {
                "FINANCIAL_LICENSE_ENVIRONMENT": "production",
                "FINANCIAL_SOURCE_LICENSE_APPROVALS": "",
            },
            at=now,
            provider_profile=runtime_profiles[provider_id],
            catalog=catalog,
        )
        for provider_id in EMBEDDED_PROVIDER_IDS
    }
    expired = provider_authorization_decision(
        "tushare_cn",
        {
            "FINANCIAL_LICENSE_ENVIRONMENT": "production",
            "FINANCIAL_SOURCE_LICENSE_APPROVALS": "tushare_cn",
        },
        at=datetime(2027, 2, 2, tzinfo=timezone.utc),
        provider_profile=runtime_profiles["tushare_cn"],
        catalog=catalog,
    )
    missing_rss = rss_authorization_decision(
        rss_specs[0]["url"], "", {"FINANCIAL_LICENSE_ENVIRONMENT": "production"}, at=now
    )
    paid = {
        profile["source_id"]: {
            "cost_type": profile["cost_type"],
            "billing_owner_role": profile["billing_owner_role"],
            "credential_owner_role": profile["credential"]["owner_role"],
            "credential_acquisition_url": profile["credential"]["acquisition_url"],
        }
        for profile in provider_profiles.values()
        if "paid" in profile["cost_type"]
    }
    checks = {
        "all_embedded_providers_profiled": set(provider_profiles)
            == set(EMBEDDED_PROVIDER_IDS),
        "all_financial_rss_profiled": len(rss_profiles) == len(rss_specs) == 5,
        "provider_and_rss_profile_ids_unique": len(profiles)
            == len(provider_profiles) + len(rss_profiles),
        "all_profiles_have_owners_cost_permissions_quota_review": all(
            profile["owner_role"]
            and profile["legal_review_owner_role"]
            and profile["cost_type"]
            and profile["permissions"]
            and profile["quota"]
            and profile["review_due_on"]
            for profile in profiles.values()
        ),
        "redistribution_never_implicitly_allowed": all(
            profile["permissions"]["redistribute"] is False
            for profile in profiles.values()
        ),
        "production_requires_explicit_approval": all(
            not decision["authorized"]
            and decision["reason"] in {
                "production_approval_missing", "production_use_forbidden"
            }
            for decision in production_without_approval.values()
        ),
        "expired_authorization_disabled": not expired["authorized"]
            and expired["reason"] == "license_review_expired",
        "missing_rss_profile_disabled_in_production": not missing_rss["authorized"]
            and missing_rss["reason"] == "license_profile_missing",
        "paid_sources_have_billing_and_token_responsibility": bool(paid)
            and all(
                item["billing_owner_role"] not in {"", "not_applicable"}
                and item["credential_owner_role"] not in {"", "not_applicable"}
                and item["credential_acquisition_url"].startswith("https://")
                for item in paid.values()
            ),
    }
    endpoints = endpoint_reconciliation()
    checks["configured_endpoints_match_implementations"] = True
    _assert(all(checks.values()), checks)
    return {
        "checks": checks,
        "profile_count": len(profiles),
        "provider_count": len(provider_profiles),
        "rss_count": len(rss_profiles),
        "production_without_approval": production_without_approval,
        "expired_authorization": expired,
        "missing_rss_profile": missing_rss,
        "paid_source_responsibility": paid,
        "endpoint_reconciliation": endpoints,
        "profiles": {
            profile_id: {
                key: profile[key]
                for key in (
                    "source_type", "source_id", "owner_role",
                    "legal_review_owner_role", "cost_type", "billing_owner_role",
                    "credential", "permissions", "quota", "reviewed_on",
                    "review_due_on", "authorization_status",
                    "production_allowed", "production_approval_required",
                )
            }
            for profile_id, profile in profiles.items()
        },
    }


def runtime_acceptance() -> dict:
    suites = (
        "tests.test_financial_source_licenses",
        "tests.test_financial_provider_router",
        "tests.test_financial_phase0_rss_sources",
        "tests.test_financial_shared_sources",
    )
    suite = unittest.TestSuite(
        unittest.defaultTestLoader.loadTestsFromName(name) for name in suites
    )
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    _assert(result.wasSuccessful(), stream.getvalue())
    return {
        "executed": True,
        "suites": list(suites),
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "network_calls": 0,
        "secrets_included": False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    report = {
        "acceptance": "passed",
        "task": "6.1",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "boundaries": {
            "operational_profile_not_legal_opinion": True,
            "sdk_license_not_data_license": True,
            "test_approval_not_production_approval": True,
            "new_database": False,
            "new_table": False,
            "new_service": False,
            "new_port": False,
        },
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
