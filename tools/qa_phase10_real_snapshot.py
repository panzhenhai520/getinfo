#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 10 真机快照导出（**只读**：BEGIN READ ONLY + statement_timeout + LIMIT）。

与 Phase 09 快照的差别（本阶段验收的关键）：
  · 多探两张 Phase 10 的表（`memory_validation` / `memory_contradiction`）是否存在、各多少行；
  · 多探一次**部署态**（`/app/qa_schema.py` 的 QA_SCHEMA_VERSION 与 `/app/qa_memory_revalidation.py`
    是否存在）—— 这是"线上跑到哪一阶段"的直接证据，避免把"没发布"误读成"没有数据"；
  · 其余表与 Phase 09 完全一致（同一份真实数据、同一套只读口径）。

安全口径（沿用 Phase 06/07/08/09 + 本阶段硬约束）：
  · SSH 凭据只从环境变量取（`QA_SSH_HOST` / `QA_SSH_USER` / `QA_SSH_PASSWORD`），脚本不写死口令；
  · 每条远端命令都是**短命**的：一条 `docker exec ... psql -c "BEGIN READ ONLY; SET LOCAL
    statement_timeout='20s'; <一条 SELECT>; COMMIT;"`，带 LIMIT；
  · 只有 SELECT 与只读探针（`docker exec ... grep/ls`），没有写操作；不在容器里放文件；
  · 一次一个连接，读完即关；任何超时/失败都记账并继续（绝不挂在远端）。

用法：
    QA_SSH_PASSWORD=*** python tools/qa_phase10_real_snapshot.py --probe
    QA_SSH_PASSWORD=*** python tools/qa_phase10_real_snapshot.py --out baseline/qa-memory-revalidation-real-sample.json
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
from qa_phase09_real_snapshot import (  # noqa: E402
    JSON_TARGETS as P09_JSON_TARGETS, TABLES as P09_TABLES, TARGETS as P09_TARGETS,
    _counts_sql, _decode,
)

SNAPSHOT_VERSION = "qa-memory-revalidation-real-sample-v1"

PHASE10_TABLES = ("memory_validation", "memory_contradiction")
TABLES = tuple(P09_TABLES) + PHASE10_TABLES

TARGETS = dict(P09_TARGETS)
TARGETS.update({
    "memory_validation": {
        "validation_id": ["validation_id"], "memory_id": ["memory_id"], "run_id": ["run_id"],
        "outcome": ["outcome"], "reason": ["reason"], "gate_decision": ["gate_decision"],
        "gate_reason": ["gate_reason"], "status_before": ["status_before"],
        "status_after": ["status_after"], "verified": ["verified"], "promoted": ["promoted"],
        "high_stakes": ["high_stakes"], "judge": ["judge"],
        "revalidation_version": ["revalidation_version"], "created_at": ["created_at"],
    },
    "memory_contradiction": {
        "contradiction_id": ["contradiction_id"], "kind": ["kind"],
        "conflict_type": ["conflict_type"], "left_memory_id": ["left_memory_id"],
        "right_memory_id": ["right_memory_id"], "right_evidence_ref": ["right_evidence_ref"],
        "resolution": ["resolution"], "reason_code": ["reason_code"],
        "status_action": ["status_action"], "decider": ["decider"], "run_id": ["run_id"],
        "contradiction_version": ["contradiction_version"], "created_at": ["created_at"],
    },
})

INT_TARGETS = {"authority_level", "relevance_score", "latency_ms", "confidence", "seen_count",
               "rejected_count", "round_index", "reuse_count", "recall_count", "version",
               "decay_score", "weight", "evidence_score", "hits", "utility", "recalled",
               "used", "helped", "article_id", "verified", "promoted", "high_stakes"}
JSON_TARGETS = set(P09_JSON_TARGETS)
DEFAULT_LIMITS = {
    "qa_runs": 200, "qa_claims": 20000, "qa_claim_evidence": 40000, "qa_evidence": 20000,
    "qa_conflicts": 4000, "qa_evidence_seen": 20000, "qa_stage_runs": 2000,
    "memory_item": 20000, "memory_version": 40000, "memory_entity_link": 40000,
    "memory_evidence_link": 40000, "memory_relation": 40000, "memory_recall_log": 20000,
    "memory_write_decision": 40000, "memory_usage_stat": 20000,
    "memory_validation": 40000, "memory_contradiction": 40000,
}
PROBE = ("SELECT table_name, column_name, data_type FROM information_schema.columns "
         "WHERE table_schema='public' AND table_name IN (%s) ORDER BY table_name, ordinal_position"
         % ",".join("'%s'" % name for name in TABLES))


def _select_for(table: str, columns: set) -> str:
    select = []
    for target, candidates in TARGETS[table].items():
        found = next((name for name in candidates if name in columns), "")
        if not found:
            select.append(("NULL::text AS %s" if target not in INT_TARGETS
                           else "NULL::int AS %s") % target)
            continue
        select.append(("%s AS %s" if target in INT_TARGETS else "%s::text AS %s")
                      % (found, target))
    order = "created_at" if "created_at" in columns else (
        "run_id" if "run_id" in columns else "1")
    where = " WHERE stage='level1_retrieval'" if table == "qa_stage_runs" else ""
    return "SELECT %s FROM %s%s ORDER BY %s LIMIT %d" % (
        ", ".join(select), table, where, order, DEFAULT_LIMITS[table])


def _shell(client, command: str, timeout: int = 30) -> str:
    """短命只读探针（grep/ls 之类）：不进容器写文件、不改任何状态。"""
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = (stdout.read() + stderr.read()).decode("utf-8", "replace").strip()
    stdin.close()
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("QA_SSH_HOST", DEFAULT_HOST))
    parser.add_argument("--user", default=os.environ.get("QA_SSH_USER", "root"))
    parser.add_argument("--password", default=os.environ.get("QA_SSH_PASSWORD", ""))
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--web-container", default="collectinfo-web")
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--out", default=os.path.join(
        "baseline", "qa-memory-revalidation-real-sample.json"))
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--probe", action="store_true", help="只打印表结构与部署态，不导出")
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
            "query": ("qa_runs/qa_claims/qa_claim_evidence/qa_evidence/qa_conflicts/"
                      "qa_evidence_seen/qa_stage_runs + memory_*（含 Phase 10 的两张表）"
                      "各一条 SELECT，带 LIMIT；另加两条只读部署态探针"),
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
    deployment = {
        "schema_version": _shell(client, "timeout 20 docker exec %s grep -h 'QA_SCHEMA_VERSION =' "
                                         "/app/qa_schema.py" % args.web_container),
        "p09_module": _shell(client, "timeout 20 docker exec %s ls -l /app/qa_memory.py"
                             % args.web_container),
        "p10_module": _shell(client, "timeout 20 docker exec %s ls -l /app/qa_memory_revalidation.py"
                             % args.web_container),
        "containers": _shell(client, "timeout 15 docker ps --format '{{.Names}} {{.Status}}'"),
    }
    snapshot["deployment"] = deployment
    if args.probe:
        for name in TABLES:
            values = columns.get(name)
            print("[%s] %s" % (name, "缺表" if not values else "%d 列" % len(values)))
        print("部署态：%s" % json.dumps(deployment, ensure_ascii=False))
        client.close()
        return 0
    missing = [name for name in TABLES if name not in columns]
    snapshot["schema"] = {name: sorted(values) for name, values in sorted(columns.items())}
    snapshot["missing_tables"] = missing
    snapshot["memory_tables_present"] = [name for name in TABLES[:8] if name in columns]
    snapshot["phase10_tables_present"] = [name for name in PHASE10_TABLES if name in columns]
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
    print("Phase 09 记忆表存在：%s" % (snapshot["memory_tables_present"] or "无"))
    print("Phase 10 两张表存在：%s" % (snapshot["phase10_tables_present"] or "无"))
    print("部署态：schema=%s / p10 模块=%s"
          % (deployment["schema_version"] or "未知",
             "在位" if "No such file" not in deployment["p10_module"] else "不在位"))
    for table in TABLES:
        if table not in columns:
            snapshot[table] = []
            continue
        text = run(_agg(_select_for(table, columns[table])), table)
        rows = _pick_json(text)
        snapshot[table] = _decode(rows if isinstance(rows, list) else [])
        print("%-24s %5d 行" % (table, len(snapshot[table])))
    snapshot["memory_random_sample"] = {name: int(catalog.get(name) or 0)
                                       for name in TABLES if name.startswith("memory_")}
    article_ids = sorted({int(row.get("article_id")) for row in snapshot.get("qa_evidence") or []
                          if str(row.get("article_id") or "").strip().isdigit()})
    snapshot["embedding_article_ids"] = article_ids
    snapshot["intel_article_embeddings"] = []
    if article_ids:
        sql = ("SELECT coalesce(json_agg(row_to_json(t))::text,'[]') FROM (SELECT article_id, "
               "embedding_dim, replace(encode(embedding,'base64'), chr(10), '') AS embedding_b64 "
               "FROM intel_article_embeddings WHERE status='ready' AND article_id IN (%s) "
               "ORDER BY article_id LIMIT 500) t" % ",".join(str(value) for value in article_ids))
        rows = _pick_json(run(sql, "intel_article_embeddings"))
        snapshot["intel_article_embeddings"] = rows if isinstance(rows, list) else []
        print("库内已有向量 %5d 条（覆盖 %d 篇被引用文章）"
              % (len(snapshot["intel_article_embeddings"]), len(article_ids)))
    client.close()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False)
    print("快照已写入 %s（%.1f KB）" % (args.out, os.path.getsize(args.out) / 1024.0))
    if snapshot["errors"]:
        print("有 %d 条查询失败（见产物 errors）：%s"
              % (len(snapshot["errors"]), snapshot["errors"][:2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
