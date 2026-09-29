#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Run a bounded live contract check against a financial pack's RSS feeds."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industry_packs import industry_pack_loader
from intel_http import SafeHTTPClient, sanitize_external_error
from intel_light_scanner import USER_AGENT
from rss_feed_contract import validate_rss_feed_response


def _utc_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def check_pack_feeds(pack_id: str = "financial_markets") -> dict:
    pack = industry_pack_loader.load(pack_id)
    sources = [item for item in pack.get("default_sources") or [] if item.get("source_type") == "rss"]
    client = SafeHTTPClient()
    results = []
    for source in sources:
        started = time.monotonic()
        result = {
            "name": str(source.get("name") or ""),
            "url": str(source.get("url") or ""),
            "api_key_required": bool(source.get("api_key_required")),
            "access_cost": str(source.get("access_cost") or ""),
            "passed": False,
        }
        try:
            response = client.get(
                result["url"],
                headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"},
            )
            accepted = validate_rss_feed_response(response)
            result.update(
                {
                    "passed": True,
                    "status_code": response.status_code,
                    "content_type": response.content_type,
                    "response_bytes": len(response.content),
                    "final_url": response.url,
                    **accepted,
                }
            )
        except Exception as exc:
            result["error"] = sanitize_external_error(exc)
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        results.append(result)
    passed_count = sum(int(item["passed"]) for item in results)
    return {
        "check_version": "financial-rss-live-v1",
        "checked_at": _utc_text(),
        "industry_pack_id": pack_id,
        "source_count": len(results),
        "passed_count": passed_count,
        "failed_count": len(results) - passed_count,
        "all_passed": bool(results) and passed_count == len(results),
        "sources": results,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Validate configured official financial RSS feeds")
    parser.add_argument("--industry", default="financial_markets")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = check_pack_feeds(args.industry)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"{report['industry_pack_id']}: {report['passed_count']}/{report['source_count']} feeds passed"
        )
        for item in report["sources"]:
            detail = (
                f"HTTP {item.get('status_code')} · {item.get('content_type')} · "
                f"{item.get('entry_count')} entries · {item.get('response_bytes')} bytes"
                if item["passed"]
                else item.get("error", "unknown error")
            )
            print(f"- {'PASS' if item['passed'] else 'FAIL'} {item['name']}: {detail}")
    return 0 if report["all_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
