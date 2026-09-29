#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Verify the RAGFlow upload API and optional LLM assistant gate."""

from __future__ import annotations

import argparse
import json
import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config
from ragflow_client import RagflowClient
from ragflow_llm_client import RagflowLLMClient


def main() -> int:
    parser = argparse.ArgumentParser(description="Check RAGFlow LLM delivery gate")
    parser.add_argument(
        "--allow-disabled",
        action="store_true",
        help="accept a healthy upload API with a correctly disabled, unconfigured LLM",
    )
    args = parser.parse_args()
    result = {
        "base_url_configured": bool(config.RAGFLOW_BASE_URL),
        "api_key_configured": bool(config.RAGFLOW_API_KEY),
        "upload_api_healthy": False,
        "llm_enabled": bool(config.RAGFLOW_LLM_ENABLED),
        "llm_app_id_configured": bool(config.RAGFLOW_LLM_APP_ID),
        "llm_model_id_configured": bool(config.RAGFLOW_LLM_MODEL_ID),
        "llm_ready": False,
        "safe_disabled": False,
    }
    try:
        datasets = RagflowClient().list_datasets(page=1, page_size=1)
        result["upload_api_healthy"] = isinstance(datasets, list)
    except Exception:
        result["upload_api_healthy"] = False

    health = RagflowLLMClient().health_check()
    result["llm_ready"] = bool(health.get("ready"))
    result["safe_disabled"] = bool(
        not config.RAGFLOW_LLM_ENABLED
        and not result["llm_ready"]
        and (
            not config.RAGFLOW_LLM_APP_ID
            or not config.RAGFLOW_LLM_MODEL_ID
        )
    )
    result["gate"] = (
        "ready"
        if result["upload_api_healthy"] and result["llm_ready"]
        else (
            "safe_disabled"
            if result["upload_api_healthy"] and result["safe_disabled"]
            else "blocked"
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if result["gate"] == "ready":
        return 0
    if args.allow_disabled and result["gate"] == "safe_disabled":
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
