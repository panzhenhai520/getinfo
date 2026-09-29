#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Lease-based candidate dispatcher using the existing crawler adapter."""

from __future__ import annotations

import argparse
import json
import os
import socket
import uuid
from typing import Dict, Iterable, Optional

import config
from candidate_crawler_adapter import CandidateCrawlerAdapter
from intel_candidates import IntelCandidateRepository, intel_candidate_repository


class IntelCandidateDispatcher:
    def __init__(
        self,
        *,
        repository: IntelCandidateRepository = None,
        adapter: CandidateCrawlerAdapter = None,
        worker_id: str = "",
    ):
        self.repository = repository or intel_candidate_repository
        self.adapter = adapter or CandidateCrawlerAdapter(
            database=self.repository.db,
            candidate_repository=self.repository,
        )
        self.worker_id = worker_id or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )

    def dispatch_once(
        self,
        *,
        limit: Optional[int] = None,
        manual: bool = False,
        candidate_ids: Optional[Iterable[int]] = None,
        activation_id: str = "",
        industry_pack_id: str = "",
    ) -> Dict:
        if not config.INTEL_CANDIDATE_DISPATCH_ENABLED and not manual:
            return {
                "skipped": True,
                "reason": "automatic candidate dispatch disabled",
                "claimed": 0,
                "crawled": 0,
                "retry_wait": 0,
                "failed": 0,
            }
        # 不再定期删除临时库原文：保留抓取到的原文供"对比原文"查看。
        candidates = self.repository.claim_candidates(
            self.worker_id,
            limit=limit,
            candidate_ids=candidate_ids,
            active_activation_id=activation_id,
            industry_pack_id=industry_pack_id if candidate_ids is None else "",
        )
        stats = {
            "claimed": len(candidates),
            "crawled": 0,
            "existing_article": 0,
            "partial_success": 0,
            "retry_wait": 0,
            "failed": 0,
        }
        for candidate in candidates:
            try:
                result = self.adapter.dispatch(candidate)
                if result.get("success") and result.get("article_id"):
                    self.repository.complete_candidate(
                        int(candidate["id"]),
                        int(result["article_id"]),
                        str(result.get("crawler_task_id") or ""),
                    )
                    stats["crawled"] += 1
                    if result.get("outcome") == "existing_article":
                        stats["existing_article"] += 1
                    continue
                outcome = result.get("outcome") or "crawler_failed"
                if outcome == "partial_success":
                    stats["partial_success"] += 1
                status = self.repository.fail_candidate(
                    int(candidate["id"]),
                    str(result.get("error") or outcome),
                    permanent=bool(result.get("permanent")),
                )
                if outcome == "quality_failed" and status == "failed":
                    self.repository.set_candidate_decision(
                        int(candidate["id"]), quality_status="quality_failed",
                    )
                stats[status] = stats.get(status, 0) + 1
            except Exception as exc:
                status = self.repository.fail_candidate(int(candidate["id"]), str(exc))
                stats[status] = stats.get(status, 0) + 1
        return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Dispatch one candidate batch")
    parser.add_argument("--limit", type=int, default=config.INTEL_CANDIDATE_BATCH_SIZE)
    args = parser.parse_args()
    result = IntelCandidateDispatcher().dispatch_once(limit=args.limit, manual=True)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("failed", 0) == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
