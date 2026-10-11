#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 11（P11-06 验收素材）· 技能路由的**只读真机快照导出**。

为什么要它：线上**没有任何 Phase 11 的技能遥测行**（`qa_stage_runs` 里 `node_kind='skill'`
是 0 行 —— 本阶段还没发布）。所以"真机上的技能统计"只能用
**快照 + 同代码离线重建**：把真实 run 的结论/边/证据原样导出，离线用**同一份代码**
（Phase 07 的缺口规则 + Phase 11 的路由规则）重放，再把分布报出来。
这与 Phase 04/09/10 的处置口径一致（见各阶段 DECISION_LOG）。

只读口径：
  · SQL 只有 SELECT，且每条都带 LIMIT；
  · 不建表、不写库、不调任何模型或嵌入端点；
  · 导出的是**原始回执字段**（question / mode / claim / edge / evidence 的 payload），
    不做任何再加工 —— 离线侧才能证明"统计是从真数据算出来的"。

用法：
    python tools/qa_phase11_real_snapshot.py --out baseline/qa-skill-snapshot.json --limit 20
    python tools/qa_phase11_real_snapshot.py --print        # 只看计数，不写文件
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SNAPSHOT_VERSION = "qa-skill-snapshot-v1"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _rows(cursor, sql: str, params=()) -> list:
    cursor.execute(sql, tuple(params))
    return [dict(row) for row in cursor.fetchall()]


def _json(raw, fallback):
    try:
        value = json.loads(raw) if raw not in (None, "") else fallback
    except (TypeError, ValueError):
        return fallback
    return value


def build_snapshot(connection, *, limit_runs: int = 20, limit_rows: int = 4000) -> dict:
    """从（只读）连接导出快照：run 元信息 + 结论/边/证据的原始 payload。

    每条 SQL 都带 LIMIT，且只读；任何一张表读不到就如实记 `errors`（不猜、不补）。
    """
    cursor = connection.cursor()
    snapshot = {"snapshot_version": SNAPSHOT_VERSION, "captured_at_utc": _utc(),
                "limit_runs": int(limit_runs), "runs": [], "counts": {}, "errors": []}
    try:
        runs = _rows(cursor, "SELECT id, question_text, mode, status, corpus_version, "
                             "created_at FROM qa_runs ORDER BY created_at DESC LIMIT ?",
                     (int(limit_runs),))
    except Exception as exc:      # noqa: BLE001
        snapshot["errors"].append("qa_runs: %s: %s" % (type(exc).__name__, str(exc)[:160]))
        return snapshot
    run_ids = [str(row.get("id") or "") for row in runs if row.get("id")]
    for run in runs:
        run_id = str(run.get("id") or "")
        row = {"run_id": run_id, "question": str(run.get("question_text") or ""),
               "mode": str(run.get("mode") or ""), "status": str(run.get("status") or ""),
               "corpus_version": str(run.get("corpus_version") or ""),
               "created_at": str(run.get("created_at") or ""),
               "claims": [], "edges": [], "evidence": []}
        for table, target, sql in (
            ("qa_claims", "claims",
             "SELECT claim_key, claim_text, claim_type, confidence, verification_status, "
             "payload_json FROM qa_claims WHERE run_id=? LIMIT ?"),
            ("qa_claim_evidence", "edges",
             "SELECT claim_key, evidence_ref, relationship, relevance_score "
             "FROM qa_claim_evidence WHERE run_id=? LIMIT ?"),
            ("qa_evidence", "evidence",
             "SELECT evidence_ref, source_type, source_title, source_url, published_at, "
             "authority_level, payload_json FROM qa_evidence WHERE run_id=? LIMIT ?"),
        ):
            try:
                rows = _rows(cursor, sql, (run_id, int(limit_rows)))
            except Exception as exc:      # noqa: BLE001
                snapshot["errors"].append("%s(%s): %s: %s"
                                          % (table, run_id, type(exc).__name__, str(exc)[:120]))
                continue
            if target == "claims":
                row["claims"] = [{"claim_key": str(item.get("claim_key") or ""),
                                  "text": str(item.get("claim_text") or ""),
                                  "claim_type": str(item.get("claim_type") or ""),
                                  "confidence": float(item.get("confidence") or 0.0),
                                  "verification_status": str(item.get("verification_status") or ""),
                                  "payload": _json(item.get("payload_json"), {})}
                                 for item in rows]
            elif target == "edges":
                row["edges"] = rows
            else:
                row["evidence"] = [{"evidence_ref": str(item.get("evidence_ref") or ""),
                                    "source_type": str(item.get("source_type") or ""),
                                    "title": str(item.get("source_title") or ""),
                                    "source_url": str(item.get("source_url") or ""),
                                    "published_at": item.get("published_at"),
                                    "authority_level": item.get("authority_level"),
                                    "payload": _json(item.get("payload_json"), {})}
                                   for item in rows]
        snapshot["runs"].append(row)
    cursor.close()
    snapshot["counts"] = {
        "runs": len(snapshot["runs"]),
        "runs_with_claims": len([row for row in snapshot["runs"] if row["claims"]]),
        "runs_with_evidence": len([row for row in snapshot["runs"] if row["evidence"]]),
        "claims": sum(len(row["claims"]) for row in snapshot["runs"]),
        "edges": sum(len(row["edges"]) for row in snapshot["runs"]),
        "evidence": sum(len(row["evidence"]) for row in snapshot["runs"]),
    }
    return snapshot


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Phase 11 技能路由只读快照导出")
    parser.add_argument("--out", default="", help="写出的 JSON 路径（空=只打印）")
    parser.add_argument("--limit", type=int, default=20, help="最多导出多少个 run")
    parser.add_argument("--print", action="store_true", dest="print_only", help="只打印计数")
    args = parser.parse_args(argv)

    from db_connection import connect_database

    connection = connect_database(read_only=True)
    snapshot = build_snapshot(connection, limit_runs=args.limit)
    summary = {"snapshot_version": snapshot["snapshot_version"],
               "captured_at_utc": snapshot["captured_at_utc"],
               "counts": snapshot["counts"], "errors": snapshot["errors"]}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.out and not args.print_only:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False, indent=2)
        print("written: %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
