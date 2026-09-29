#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Check the provider-neutral market-intelligence LLM gate without leaking secrets."""

from __future__ import annotations

import argparse
import json
import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from industry_packs import industry_pack_loader
from intel_http import sanitize_external_error
from intel_llm_client import intel_llm_client


def main() -> int:
    parser = argparse.ArgumentParser(description="Check market-intelligence LLM readiness")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Send one minimal strict-JSON classification request",
    )
    args = parser.parse_args()

    result = intel_llm_client.health_check()
    if args.probe and result.get("ready"):
        try:
            classified = intel_llm_client.classify(
                {
                    "title": "香港家族办公室政策更新",
                    "content": "香港公布家族办公室税务宽免与落户政策调整。",
                },
                industry_pack_loader.load("family_office"),
            )
            result["probe"] = {
                "success": True,
                "category": classified.get("category"),
                "confidence": classified.get("confidence"),
                "topic_tag_count": len(classified.get("topic_tags") or []),
            }
        except Exception as exc:
            result["probe"] = {
                "success": False,
                "error": sanitize_external_error(
                    exc,
                    secrets=(intel_llm_client.api_key,),
                ),
            }

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if not result.get("ready"):
        return 2
    if args.probe and not result.get("probe", {}).get("success"):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
