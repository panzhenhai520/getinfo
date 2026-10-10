#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 09 真机快照导出（**只读**：BEGIN READ ONLY + statement_timeout + LIMIT）。

它只做一件事：把 A 机 Postgres 上"重建记忆图候选 / 复算召回与衰减"需要的几张表
**只读**导成一份 JSON，供 `tools/qa_phase09_memory_acceptance.py` 离线复算
（那个工具不联网、不连库）。

与 Phase 07/08 快照的差别（本阶段验收的关键）：
  · 多了 `qa_runs.session_id` / `owner_user_id` —— 记忆的作用域（§14）与"跨会话召回对比"要用；
  · 多了 `qa_evidence_seen` 全字段 —— 与 Phase 02 的 seen 机制对齐（三层集合的第一层）；
  · 探针里额外**列出 memory_* 表是否存在**（A 机尚未发布 Phase 09 时应当是"不存在"，
    这份快照就是"前后对比"里的 before 侧证据）。

安全口径（沿用 Phase 06/07/08 + 本阶段硬约束）：
  · SSH 凭据只从环境变量取（`QA_SSH_HOST` / `QA_SSH_USER` / `QA_SSH_PASSWORD`），脚本不写死口令；
  · 每条远端命令都是**短命**的：一条 `docker exec ... psql -c "BEGIN READ ONLY; SET LOCAL
    statement_timeout='20s'; <一条 SELECT>; COMMIT;"`，带 LIMIT；
  · 只有 SELECT，没有写操作；不在容器里放任何文件（结果直接走 stdout）；
  · 一次一个连接，读完即关；任何超时/失败都记账并继续（绝不挂在远端）。

用法：
    QA_SSH_PASSWORD=*** python tools/qa_phase09_real_snapshot.py --out baseline/qa-memory-real-sample.json
    python tools/qa_phase09_real_snapshot.py --probe
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qa_phase07_real_snapshot import (  # noqa: E402
    DEFAULT_CONTAINER, DEFAULT_DATABASE, DEFAULT_HOST, STATEMENT_TIMEOUT,
    _agg, _connect, _pick_json, _remote,
)

SNAPSHOT_VERSION = "qa-memory-real-sample-v1"

MEMORY_TABLES = ("memory_item", "memory_version", "memory_entity_link", "memory_evidence_link",
                 "memory_relation", "memory_recall_log", "memory_write_decision",
                 "memory_usage_stat")

TABLES = ("qa_runs", "qa_claims", "qa_claim_evidence", "qa_evidence", "qa_conflicts",
          "qa_evidence_seen", "qa_stage_runs") + MEMORY_TABLES

TARGETS = {
    "qa_runs": {
        "id": ["id"], "question_text": ["question_text"], "mode": ["mode"],
        "status": ["status"], "industry_pack_id": ["industry_pack_id"],
        "owner_user_id": ["owner_user_id"], "session_id": ["session_id"],
        "corpus_version": ["corpus_version"], "model_version": ["model_version"],
        "created_at": ["created_at"], "completed_at": ["completed_at"],
    },
    "qa_claims": {
        "run_id": ["run_id"], "claim_key": ["claim_key"], "stage": ["stage"],
        "claim_text": ["claim_text"], "claim_type": ["claim_type"],
        "confidence": ["confidence"], "verification_status": ["verification_status"],
        "payload": ["payload", "payload_json"],
    },
    "qa_claim_evidence": {
        "run_id": ["run_id"], "claim_key": ["claim_key"], "evidence_ref": ["evidence_ref"],
        "relationship": ["relationship"], "relevance_score": ["relevance_score"],
    },
    "qa_evidence": {
        "run_id": ["run_id"], "evidence_ref": ["evidence_ref"], "source_type": ["source_type"],
        "article_id": ["article_id"], "source_url": ["source_url"],
        "source_title": ["source_title"], "published_at": ["published_at"],
        "authority_level": ["authority_level"], "payload": ["payload", "payload_json"],
    },
    "qa_conflicts": {
        "run_id": ["run_id"], "conflict_id": ["conflict_id", "conflict_key"],
        "conflict_type": ["conflict_type"], "resolution": ["resolution"],
        "rationale": ["rationale"], "payload": ["payload", "payload_json"],
    },
    "qa_evidence_seen": {
        "owner_user_id": ["owner_user_id"], "session_id": ["session_id"],
        "industry_pack_id": ["industry_pack_id"], "source_fingerprint": ["source_fingerprint"],
        "span_fingerprint": ["span_fingerprint"], "evidence_ref": ["evidence_ref"],
        "source_type": ["source_type"], "status": ["status"], "seen_count": ["seen_count"],
        "rejected_count": ["rejected_count"], "first_run_id": ["first_run_id"],
        "last_run_id": ["last_run_id"], "round_index": ["round_index"],
        "first_seen_at": ["first_seen_at"], "last_seen_at": ["last_seen_at"],
    },
    "qa_stage_runs": {
        "run_id": ["run_id"], "stage": ["stage"], "status": ["status"],
        "latency_ms": ["latency_ms"], "token_usage": ["token_usage_json", "token_usage"],
    },
    # 记忆表：A 机尚未发布 Phase 09 时不存在（探针会如实报告），存在就照常导出
    "memory_item": {
        "memory_id": ["memory_id"], "memory_type": ["memory_type"],
        "canonical_content": ["canonical_content"], "confidence": ["confidence"],
        "freshness_class": ["freshness_class"], "status": ["status"], "scope": ["scope"],
        "scope_key": ["scope_key"], "reuse_count": ["reuse_count"],
        "recall_count": ["recall_count"], "version": ["version"],
        "decay_score": ["decay_score"], "last_verified_at": ["last_verified_at"],
        "valid_until": ["valid_until"], "created_at": ["created_at"],
    },
    "memory_version": {
        "memory_id": ["memory_id"], "version": ["version"], "change": ["change"],
        "status": ["status"], "decay_score": ["decay_score"], "reason": ["reason"],
        "created_at": ["created_at"],
    },
    "memory_entity_link": {
        "memory_id": ["memory_id"], "entity_key": ["entity_key"], "role": ["role"],
    },
    "memory_evidence_link": {
        "memory_id": ["memory_id"], "evidence_ref": ["evidence_ref"],
        "source_fingerprint": ["source_fingerprint"], "span_fingerprint": ["span_fingerprint"],
        "run_id": ["run_id"], "verdict": ["verdict"], "evidence_score": ["evidence_score"],
    },
    "memory_relation": {
        "memory_id": ["memory_id"], "relation": ["relation"],
        "target_memory_id": ["target_memory_id"], "weight": ["weight"],
    },
    "memory_recall_log": {
        "recall_id": ["recall_id"], "run_id": ["run_id"], "mode": ["mode"],
        "query_fingerprint": ["query_fingerprint"], "hits": ["hits"],
        "top_score": ["top_score"], "counts": ["counts_json", "counts"],
    },
    "memory_write_decision": {
        "decision_id": ["decision_id"], "run_id": ["run_id"], "memory_type": ["memory_type"],
        "decision": ["decision"], "reason": ["reason"], "utility": ["utility"],
        "memory_id": ["memory_id"], "scope": ["scope"],
    },
    "memory_usage_stat": {
        "memory_id": ["memory_id"], "day": ["day"], "recalled": ["recalled"],
        "used": ["used"], "helped": ["helped"],
    },
}
INT_TARGETS = {"authority_level", "relevance_score", "latency_ms", "confidence", "seen_count",
               "rejected_count", "round_index", "reuse_count", "recall_count", "version",
               "decay_score", "weight", "evidence_score", "hits", "utility", "recalled",
               "used", "helped", "article_id"}
JSON_TARGETS = {"payload", "counts"}
DEFAULT_LIMITS = {
    "qa_runs": 200, "qa_claims": 20000, "qa_claim_evidence": 40000, "qa_evidence": 20000,
    "qa_conflicts": 4000, "qa_evidence_seen": 20000, "qa_stage_runs": 2000,
    "memory_item": 20000, "memory_version": 40000, "memory_entity_link": 40000,
    "memory_evidence_link": 40000, "memory_relation": 40000, "memory_recall_log": 20000,
    "memory_write_decision": 40000, "memory_usage_stat": 20000,
}
PROBE = ("SELECT table_name, column_name, data_type FROM information_schema.columns "
         "WHERE table_schema='public' AND table_name IN (%s) ORDER BY table_name, ordinal_position"
         % ",".join("'%s'" % name for name in TABLES))


def _counts_sql(tables) -> str:
    """只统计**远端真的存在**的表（缺表不该让整条统计语句报错）。"""
    return " UNION ALL ".join(
        "SELECT '%s' AS t, count(*) AS n FROM %s" % (name, name) for name in tables)


def _select_for(table: str, columns: set) -> str:
    """按实际列集拼 SELECT（缺的目标字段写 NULL，且如实反映这台机器上没有那一列）。"""
    select = []
    for target, candidates in TARGETS[table].items():
        found = next((name for name in candidates if name in columns), "")
        if not found:
            select.append(("NULL::text AS %s" if target not in INT_TARGETS
                           else "NULL::int AS %s") % target)
            continue
        if target in INT_TARGETS:
            select.append("%s AS %s" % (found, target))
        else:
            select.append("%s::text AS %s" % (found, target))
    order = "created_at" if "created_at" in columns else (
        "run_id" if "run_id" in columns else ("memory_id" if "memory_id" in columns else "1"))
    where = " WHERE stage='level1_retrieval'" if table == "qa_stage_runs" else ""
    return "SELECT %s FROM %s%s ORDER BY %s LIMIT %d" % (
        ", ".join(select), table, where, order, DEFAULT_LIMITS[table])


def _decode(rows: list) -> list:
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        for key in JSON_TARGETS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                try:
                    item[key] = json.loads(value)
                except ValueError:
                    item[key] = None
        out.append(item)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("QA_SSH_HOST", DEFAULT_HOST))
    parser.add_argument("--user", default=os.environ.get("QA_SSH_USER", "root"))
    parser.add_argument("--password", default=os.environ.get("QA_SSH_PASSWORD", ""))
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--out", default=os.path.join("baseline", "qa-memory-real-sample.json"))
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--probe", action="store_true", help="只打印表结构，不导出")
    args = parser.parse_args()
    if not args.password:
        print("缺少凭据：请设置 QA_SSH_PASSWORD（脚本不写死口令）")
        return 2
    try:
        client = _connect(args.host, args.user, args.password, args.timeout)
    except ImportError:
        print("缺少 paramiko：pip install paramiko")
        return 2
    snapshot = {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")
                           .replace("+00:00", "Z"),
        "source": {
            "host": args.host, "container": args.container, "database": args.database,
            "access": ("readonly (BEGIN READ ONLY + statement_timeout=%s + LIMIT)"
                       % STATEMENT_TIMEOUT),
            "query": "qa_runs/qa_claims/qa_claim_evidence/qa_evidence/qa_conflicts/"
                     "qa_evidence_seen/qa_stage_runs + memory_*（各一条 SELECT，带 LIMIT）",
            "snapshot_version": SNAPSHOT_VERSION,
        },
        "errors": [],
    }

    def run(sql: str, key: str) -> str:
        stdin, stdout, stderr = client.exec_command(_remote(args.container, args.database, sql),
                                                    timeout=args.timeout + 20)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        stdin.close()
        if err.strip():
            snapshot["errors"].append({"key": key, "error": err.strip()[:300]})
        return out

    columns = {}
    for line in run(PROBE, "probe").splitlines():
        parts = line.strip().split("|")
        if len(parts) == 3:
            columns.setdefault(parts[0], set()).add(parts[1])
    if args.probe:
        for name in TABLES:
            values = columns.get(name)
            print("[%s] %s" % (name, "缺表" if not values else "%d 列" % len(values)))
        client.close()
        return 0
    missing = [name for name in TABLES if name not in columns]
    snapshot["schema"] = {name: sorted(values) for name, values in sorted(columns.items())}
    snapshot["missing_tables"] = missing
    snapshot["memory_tables_present"] = [name for name in MEMORY_TABLES if name in columns]
    catalog = {}
    for line in run(_counts_sql([name for name in TABLES if name in columns]), "counts").splitlines():
        parts = line.strip().split("|")
        if len(parts) == 2 and parts[0]:
            try:
                catalog[parts[0]] = int(parts[1])
            except ValueError:
                continue
    snapshot["catalog"] = catalog
    print("表行数：%s" % json.dumps(catalog, ensure_ascii=False))
    print("缺失表（本机声明、A 机没有）：%s" % missing)
    print("记忆表存在：%s" % (snapshot["memory_tables_present"] or "无（Phase 09 尚未发布）"))
    for table in ("qa_runs", "qa_claims", "qa_claim_evidence", "qa_evidence", "qa_conflicts",
                  "qa_evidence_seen", "qa_stage_runs"):
        if table not in columns:
            snapshot[table] = []
            print("%-20s %5d 行（远端没有这张表）" % (table, 0))
            continue
        text = run(_agg(_select_for(table, columns[table])), table)
        rows = _pick_json(text)
        snapshot[table] = _decode(rows if isinstance(rows, list) else [])
        print("%-20s %5d 行" % (table, len(snapshot[table])))
    snapshot["memory_random_sample"] = {}
    for table in MEMORY_TABLES:
        if table not in columns:
            snapshot["memory_random_sample"][table] = 0
            continue
        snapshot["memory_random_sample"][table] = int(catalog.get(table) or 0)
    # ── 库内**已有**向量（P09-06 向量通道用）：只导"真实证据引用到的文章"那一小撮 ──
    # 全表 10644 行 / 43.6MB 太大，而向量通道只需要"记忆链到的文章"的向量；
    # 这条查询同样是只读、短命、带 LIMIT 的，**不触发任何嵌入计算**（只是把已存向量读出来）。
    article_ids = sorted({int(row.get("article_id")) for row in snapshot.get("qa_evidence") or []
                          if str(row.get("article_id") or "").strip().isdigit()})
    snapshot["embedding_article_ids"] = article_ids
    snapshot["intel_article_embeddings"] = []
    if article_ids:
        sql = ("SELECT coalesce(json_agg(row_to_json(t))::text,'[]') FROM (SELECT article_id, "
               "embedding_dim, replace(encode(embedding,'base64'), chr(10), '') AS embedding_b64 "
               "FROM intel_article_embeddings WHERE status='ready' AND article_id IN (%s) "
               "ORDER BY article_id LIMIT 500) t" % ",".join(str(value) for value in article_ids))
        text = run(sql, "intel_article_embeddings")
        rows = _pick_json(text)
        snapshot["intel_article_embeddings"] = rows if isinstance(rows, list) else []
        print("库内已有向量 %5d 条（覆盖 %d 篇被引用文章）"
              % (len(snapshot["intel_article_embeddings"]), len(article_ids)))
    client.close()
    out_path = args.out
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False)
    print("快照已写入 %s（%.1f KB）" % (out_path, os.path.getsize(out_path) / 1024.0))
    if snapshot["errors"]:
        print("有 %d 条查询失败（见产物 errors）：%s"
              % (len(snapshot["errors"]), snapshot["errors"][:2]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
