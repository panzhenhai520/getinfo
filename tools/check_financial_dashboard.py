#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Read-only production acceptance check for the financial Dashboard API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from flask import Flask

import intel_api
from intel_api import intel_bp
from intel_database import IntelRepository
from sqlite_database import SQLiteDatabase


SAFE_FRONTEND_MARKERS = (
    "function renderIntelCard(article)",
    "const articleId = Number(article.article_id || 0)",
    "highlightKeywords(escapeHtml(rawTitle)",
    "highlightKeywords(escapeHtml(rawPreview)",
    "escapeHtml(sourceName)",
    "escapeHtml(article.domain || '-')",
    "escapeHtml(article.publish_date || article.effective_time || '未知')",
    "titleKeywords.map(escapeHtml)",
    "contentKeywords.map(escapeHtml)",
    "unknownKeywords.map(escapeHtml)",
    ".map(article => renderIntelCard({...article, dashboard_category: key}))",
)


def inspect_frontend_contract(template_path: str | Path = "") -> dict:
    path = Path(template_path or PROJECT_ROOT / "templates" / "mapindex.html").resolve()
    template = path.read_text(encoding="utf-8")
    missing = [marker for marker in SAFE_FRONTEND_MARKERS if marker not in template]
    start = template.find("function renderIntelCard(article)")
    end = template.find("function bindIntelArticleButtons()", start)
    renderer = template[start:end] if start >= 0 and end > start else ""
    unsafe_interpolations = [
        token
        for token in ("${rawTitle}", "${rawPreview}", "${sourceName}")
        if token in renderer
    ]
    return {
        "template": str(path),
        "safe": not missing and not unsafe_interpolations,
        "missing_markers": missing,
        "unsafe_interpolations": unsafe_interpolations,
    }


def _section_articles(payload: dict) -> list[dict]:
    unique = {}
    sections = payload.get("sections") or {}
    # ``recent`` is a deliberately shared stream of followed and AI-generated
    # material.  It is not evidence that the requested pack was classified.
    for key in ("policy", "trend", "today", "other"):
        section = sections.get(key) or {}
        for article in section.get("articles") or []:
            article_id = int(article.get("article_id") or 0)
            if article_id:
                unique.setdefault(article_id, article)
    return list(unique.values())


def check_dashboard(database_path: str, *, time_range: str = "730d") -> dict:
    path = Path(database_path).expanduser().resolve()
    database = SQLiteDatabase(str(path))
    if not database.connect():
        raise RuntimeError(f"无法打开数据库：{path}")
    # All API calls in this acceptance tool are GETs.  Enforce that boundary at
    # SQLite level so the checker cannot mutate the production database.
    database.connection.execute("PRAGMA query_only=ON")
    repository = IntelRepository(database)
    app = Flask("financial-dashboard-acceptance")
    app.config.update(TESTING=True)
    app.register_blueprint(intel_bp)
    client = app.test_client()
    unauthenticated = client.get(
        f"/api/intel/dashboard?industry_pack_id=financial_markets&time_range={time_range}"
    )
    headers = {"Authorization": "Bearer local-acceptance-session"}
    try:
        with patch.object(intel_api, "intel_repository", repository), patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": 0, "role": "admin"},
        ):
            financial_response = client.get(
                "/api/intel/dashboard",
                query_string={
                    "industry_pack_id": "financial_markets",
                    "time_range": time_range,
                    "per_category": 40,
                },
                headers=headers,
            )
            family_response = client.get(
                "/api/intel/dashboard",
                query_string={
                    "industry_pack_id": "family_office",
                    "time_range": time_range,
                    "per_category": 1,
                },
                headers=headers,
            )
        financial = financial_response.get_json(silent=True) or {}
        family = family_response.get_json(silent=True) or {}
        articles = _section_articles(financial)
        frontend = inspect_frontend_contract()
        sources_visible = [
            str(item.get("source_display_name") or "").strip()
            for item in articles
            if str(item.get("source_display_name") or "").strip()
        ]
        publication_visible = [
            str(item.get("publish_date") or item.get("effective_time") or "").strip()
            for item in articles
            if str(item.get("publish_date") or item.get("effective_time") or "").strip()
        ]
        passed = bool(
            unauthenticated.status_code == 401
            and financial_response.status_code == 200
            and financial.get("success")
            and int(financial.get("total") or 0) > 0
            and articles
            and sources_visible
            and publication_visible
            and family_response.status_code == 200
            and family.get("success")
            and int(family.get("total") or 0) > 0
            and frontend["safe"]
        )
        return {
            "check_version": "financial-dashboard-v1",
            "database": str(path),
            "passed": passed,
            "authentication": {
                "missing_session_status": unauthenticated.status_code,
                "authenticated_status": financial_response.status_code,
            },
            "financial": {
                "industry_pack": financial.get("industry_pack") or {},
                "total": int(financial.get("total") or 0),
                "counts": financial.get("counts") or {},
                "unique_articles_in_response": len(articles),
                "source_visible_count": len(sources_visible),
                "publication_visible_count": len(publication_visible),
                "samples": [
                    {
                        "article_id": item.get("article_id"),
                        "category": item.get("category"),
                        "title": item.get("title"),
                        "source_display_name": item.get("source_display_name"),
                        "publish_date": item.get("publish_date"),
                        "effective_time": item.get("effective_time"),
                    }
                    for item in articles[:5]
                ],
            },
            "family_office": {
                "status": family_response.status_code,
                "total": int(family.get("total") or 0),
            },
            "frontend": frontend,
        }
    finally:
        database.disconnect()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check the financial Dashboard read path")
    parser.add_argument("--database", default=os.getenv("DATABASE_PATH", "data/crawler_articles.db"))
    parser.add_argument("--time-range", default="730d")
    args = parser.parse_args(argv)
    report = check_dashboard(args.database, time_range=args.time_range)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
