#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Read-only source-to-classification trace for financial RSS acceptance."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def trace_pipeline(database_path: str, pack_id: str = "financial_markets") -> dict:
    path = Path(database_path).expanduser().resolve()
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        sources = connection.execute(
            """
            SELECT s.id, s.source_name, s.source_url, s.last_scan_status
            FROM intel_sources s
            JOIN intel_source_industries si ON si.source_id=s.id
            WHERE si.industry_pack_id=? AND s.source_type='rss'
            ORDER BY s.id
            """,
            (pack_id,),
        ).fetchall()
        results = []
        for source in sources:
            counts = connection.execute(
                """
                SELECT
                    COUNT(DISTINCT r.id) AS run_count,
                    COUNT(DISTINCT o.id) AS observation_count,
                    COUNT(DISTINCT c.id) AS candidate_count,
                    COUNT(DISTINCT CASE WHEN c.status='crawled' THEN c.id END) AS crawled_count,
                    COUNT(DISTINCT c.article_id) AS article_count,
                    COUNT(DISTINCT ic.id) AS classification_count
                FROM intel_scan_runs r
                LEFT JOIN intel_candidate_observations o ON o.scan_run_id=r.id
                LEFT JOIN intel_candidates c ON c.id=o.candidate_id
                LEFT JOIN article_intel_classifications ic
                  ON ic.article_id=c.article_id AND ic.industry_pack_id=?
                WHERE r.source_id=? AND r.industry_pack_id=?
                """,
                (pack_id, int(source["id"]), pack_id),
            ).fetchone()
            sample = connection.execute(
                """
                SELECT r.id AS run_id, o.id AS observation_id,
                       c.id AS candidate_id, c.status AS candidate_status,
                       c.article_id, a.title, a.url AS article_url,
                       ic.id AS classification_id,
                       ic.final_category, ic.final_confidence
                FROM intel_scan_runs r
                JOIN intel_candidate_observations o ON o.scan_run_id=r.id
                JOIN intel_candidates c ON c.id=o.candidate_id
                JOIN articles a ON a.id=c.article_id
                JOIN article_intel_classifications ic
                  ON ic.article_id=a.id AND ic.industry_pack_id=?
                WHERE r.source_id=? AND r.industry_pack_id=?
                ORDER BY ic.classified_at DESC, ic.id DESC
                LIMIT 1
                """,
                (pack_id, int(source["id"]), pack_id),
            ).fetchone()
            item = {
                "source_id": int(source["id"]),
                "source_name": str(source["source_name"] or ""),
                "source_url": str(source["source_url"] or ""),
                "last_scan_status": str(source["last_scan_status"] or ""),
                **{key: int(counts[key] or 0) for key in counts.keys()},
                "sample_chain": dict(sample) if sample else None,
            }
            item["complete"] = bool(
                item["run_count"]
                and item["observation_count"]
                and item["candidate_count"]
                and item["article_count"]
                and item["classification_count"]
            )
            results.append(item)
        return {
            "trace_version": "financial-rss-pipeline-v1",
            "database": str(path),
            "industry_pack_id": pack_id,
            "source_count": len(results),
            "complete_source_count": sum(int(item["complete"]) for item in results),
            "all_sources_complete": bool(results) and all(item["complete"] for item in results),
            "sources": results,
        }
    finally:
        connection.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Trace financial RSS to classifications")
    parser.add_argument("--database", default="data/crawler_articles.db")
    parser.add_argument("--industry", default="financial_markets")
    args = parser.parse_args(argv)
    report = trace_pipeline(args.database, args.industry)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["all_sources_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
