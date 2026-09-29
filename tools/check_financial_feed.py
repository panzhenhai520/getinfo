#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Read-only checks for the independent financial Dashboard feed."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from flask import Flask

import config
import intel_api
from intel_api import intel_bp
from intel_database import IntelRepository
from sqlite_database import SQLiteDatabase


FRONTEND_MARKERS = (
    'id="financialFeedSection"',
    'id="financialFeedCards"',
    "function renderFinancialFeed(data)",
    "document.createElement('article')",
    "title.textContent = String(item.title || '无标题')",
    "summary.textContent = String(item.summary || '暂无摘要')",
    "badge.textContent = String(item.kind_label || '')",
    "card.className = 'article-card financial-feed-card'",
    "translateFinancialFeedCard(item, event)",
    "/api/intel/financial/feed/translate",
    "viewFinancialMarketDetail(String(item.item_id || ''))",
    "/api/intel/financial/feed/items/${encodeURIComponent(itemId)}/detail",
    'id="financialMarketDetailModal" class="article-modal"',
    "openItem = () => viewArticle(Number(item.article_id))",
    "section.hidden = true",
    "loadFinancialFeed(1)",
)


def inspect_financial_feed_frontend(template_path: str | Path = "") -> dict:
    path = Path(template_path or PROJECT_ROOT / "templates" / "mapindex.html").resolve()
    template = path.read_text(encoding="utf-8")
    missing = [marker for marker in FRONTEND_MARKERS if marker not in template]
    start = template.find("function renderFinancialFeed(data)")
    end = template.find("async function loadFinancialFeed", start)
    renderer = template[start:end] if start >= 0 and end > start else ""
    unsafe = [
        marker
        for marker in (
            "${item.title}", "${item.summary}", "${item.kind_label}",
            "cards.innerHTML", "categories.innerHTML",
            "window.open(sourceHref, '_blank', 'noopener,noreferrer')",
            "'查看信源'",
        )
        if marker in renderer
    ]
    return {
        "template": str(path),
        "safe": not missing and not unsafe,
        "missing_markers": missing,
        "unsafe_markers": unsafe,
    }


def check_financial_feed(database_path: str, *, industry_pack_id: str, time_range: str) -> dict:
    path = Path(database_path).expanduser().resolve()
    database = SQLiteDatabase(str(path))
    if not database.connect():
        raise RuntimeError(f"无法打开数据库：{path}")
    database.connection.execute("PRAGMA query_only=ON")
    repository = IntelRepository(database)
    app = Flask("financial-feed-acceptance")
    app.config.update(TESTING=True)
    app.register_blueprint(intel_bp)
    client = app.test_client()
    unauthenticated = client.get("/api/intel/financial/feed")
    started = time.perf_counter()
    try:
        with patch.object(intel_api, "intel_repository", repository), patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": 0, "role": "admin"},
        ):
            response = client.get(
                "/api/intel/financial/feed",
                query_string={
                    "industry_pack_id": industry_pack_id,
                    "time_range": time_range,
                    "per_page": 15,
                },
                headers={"Authorization": "Bearer local-acceptance"},
            )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        payload = response.get_json(silent=True) or {}
        frontend = inspect_financial_feed_frontend()
        kinds = {str(item.get("content_kind") or "") for item in payload.get("items") or []}
        labels = {str(item.get("kind_label") or "") for item in payload.get("items") or []}
        gate_expected = bool(config.FINANCIAL_INTELLIGENCE_ENABLED)
        passed = bool(
            unauthenticated.status_code == 401
            and response.status_code == 200
            and payload.get("success")
            and payload.get("visible") == gate_expected
            and kinds.issubset({"market_fact", "source_document", "research_opinion"})
            and labels.issubset({"事实", "原文", "研究观点"})
            and elapsed_ms < 1000
            and frontend["safe"]
        )
        return {
            "check_version": "financial-dashboard-feed-v1",
            "database": str(path),
            "passed": passed,
            "authentication": {"missing_session_status": unauthenticated.status_code},
            "http_status": response.status_code,
            "visible": payload.get("visible"),
            "visibility_reason": payload.get("visibility_reason"),
            "effective_pack_ids": payload.get("effective_pack_ids") or [],
            "counts": payload.get("counts") or {},
            "item_kinds": sorted(kinds),
            "kind_labels": sorted(labels),
            "elapsed_ms": elapsed_ms,
            "frontend": frontend,
        }
    finally:
        database.disconnect()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check the independent financial Dashboard feed")
    parser.add_argument("--database", default=os.getenv("DATABASE_PATH", "data/crawler_articles.db"))
    parser.add_argument("--industry-pack-id", default="family_office")
    parser.add_argument("--time-range", default="7d")
    args = parser.parse_args(argv)
    report = check_financial_feed(
        args.database,
        industry_pack_id=args.industry_pack_id,
        time_range=args.time_range,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
