#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 12 真机快照导出（**只读**：BEGIN READ ONLY + statement_timeout + LIMIT）。

它做一件事：把 A 机 Postgres 上"做经验归纳 + 前后对比"需要的几张表**只读**导成一份 JSON，
供 `tools/qa_phase12_experience_acceptance.py` 离线复算（那个工具不联网、不连库）。

与 Phase 09/11 快照的差别（本阶段验收的关键）：
  · `qa_stage_runs` **全阶段**（不再只取 `level1_retrieval`）：经验归纳要的是"这一轮
    检索/核验/缺口各段到底发生了什么"，stage 行本身就是 trace 的骨架；
    每行只投影出**归纳用得到的字段**（performance + result 里的 stats/queries/excluded/
    search_status/cache/search_errors/evidence 计数），SQL 里写死并随快照一起留档；
  · 探针额外报告 `memory_*` 与两张 Phase 12 投影表是否存在（A 机未发布 Phase 12 时
    应当是"不存在"，这份快照就是"前后对比"里的 before 侧证据）；
  · 库内已有向量照旧按"被引用文章"小批导出（零嵌入计算）。

安全口径（沿用 Phase 06~11 + 本阶段硬约束）：
  · SSH 凭据只从环境变量取（`QA_SSH_HOST` / `QA_SSH_USER` / `QA_SSH_PASSWORD`），脚本不写死口令；
  · 每条远端命令都是**短命**的：一条 `docker exec ... psql -c "BEGIN READ ONLY; SET LOCAL
    statement_timeout='40s'; <一条 SELECT>; COMMIT;"`，带 LIMIT；
  · 只有 SELECT，没有写操作；不在容器里放任何文件（结果直接走 stdout）；
  · 一次一个连接，读完即关；任何超时/失败都记账并继续（绝不挂在远端）。

用法：
    QA_SSH_PASSWORD=*** python tools/qa_phase12_real_snapshot.py --out baseline/qa-experience-real-sample.json
    python tools/qa_phase12_real_snapshot.py --probe
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

SNAPSHOT_VERSION = "qa-experience-real-sample-v1"

MEMORY_TABLES = ("memory_item", "memory_version", "memory_entity_link", "memory_evidence_link",
                 "memory_relation", "memory_recall_log", "memory_write_decision",
                 "memory_usage_stat", "memory_validation", "memory_contradiction")
PHASE12_TABLES = ("source_reliability_memory", "skill_performance_memory")

TABLES = ("qa_runs", "qa_claims", "qa_claim_evidence", "qa_evidence", "qa_conflicts",
          "qa_reasoning_traces", "qa_stage_runs") + MEMORY_TABLES + PHASE12_TABLES

TARGETS = {
    "qa_runs": {
        "id": ["id"], "question_text": ["question_text"], "mode": ["mode"],
        "status": ["status"], "industry_pack_id": ["industry_pack_id"],
        "owner_user_id": ["owner_user_id"], "session_id": ["session_id"],
        "corpus_version": ["corpus_version"], "model_version": ["model_version"],
        "config_hash": ["config_hash"], "created_at": ["created_at"],
        "completed_at": ["completed_at"],
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
    "qa_reasoning_traces": {
        "run_id": ["run_id"], "hop_index": ["hop_index"], "round_index": ["round_index"],
        "sub_query_id": ["sub_query_id"], "sub_query": ["sub_query"], "route": ["route"],
        "results": ["results"], "accepted": ["accepted"], "rejected": ["rejected"],
        "new_claims": ["new_claims"], "resolved_gap": ["resolved_gap"], "gap_id": ["gap_id"],
        "status": ["status"], "latency_ms": ["latency_ms"],
    },
    # 记忆表：A 机尚未发布 Phase 09/10 时不存在（探针如实报告），存在就照常导出
    "memory_item": {
        "memory_id": ["memory_id"], "memory_type": ["memory_type"],
        "canonical_content": ["canonical_content"], "confidence": ["confidence"],
        "freshness_class": ["freshness_class"], "status": ["status"], "scope": ["scope"],
        "scope_key": ["scope_key"], "reuse_count": ["reuse_count"], "recall_count": ["recall_count"],
        "version": ["version"], "decay_score": ["decay_score"], "valid_until": ["valid_until"],
        "last_verified_at": ["last_verified_at"], "created_at": ["created_at"],
        "payload": ["payload_json", "payload"],
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
    "memory_validation": {
        "validation_id": ["validation_id"], "memory_id": ["memory_id"], "run_id": ["run_id"],
        "outcome": ["outcome"], "reason": ["reason"], "status_after": ["status_after"],
        "verified": ["verified"], "promoted": ["promoted"],
    },
    "memory_contradiction": {
        "contradiction_id": ["contradiction_id"], "kind": ["kind"],
        "left_memory_id": ["left_memory_id"], "right_memory_id": ["right_memory_id"],
        "resolution": ["resolution"], "reason_code": ["reason_code"],
    },
    "source_reliability_memory": {
        "domain": ["domain"], "scope_key": ["scope_key"], "authority_level": ["authority_level"],
        "reliability": ["reliability"], "band": ["band"], "observations": ["observations"],
        "verified": ["verified"], "rejected": ["rejected"], "update_frequency": ["update_frequency"],
        "memory_id": ["memory_id"], "source_version": ["source_version"],
    },
    "skill_performance_memory": {
        "skill_id": ["skill_id"], "task_type": ["task_type"], "scope_key": ["scope_key"],
        "attempts": ["attempts"], "successes": ["successes"], "success_rate": ["success_rate"],
        "cost_units": ["cost_units"], "evidence_yield": ["evidence_yield"],
        "verified_yield": ["verified_yield"], "min_samples": ["min_samples"],
        "success_definition": ["success_definition"], "telemetry_version": ["telemetry_version"],
        "memory_id": ["memory_id"],
    },
}
INT_TARGETS = {"authority_level", "relevance_score", "latency_ms", "confidence", "results",
               "accepted", "rejected", "new_claims", "resolved_gap", "hop_index", "round_index",
               "reuse_count", "recall_count", "version", "decay_score", "hits", "utility",
               "recalled", "used", "helped", "verified", "promoted", "observations",
               "attempts", "successes", "evidence_yield", "verified_yield", "min_samples",
               "article_id"}
FLOAT_TARGETS = {"reliability", "success_rate", "cost_units"}
JSON_TARGETS = {"payload", "counts"}
DEFAULT_LIMITS = {
    "qa_runs": 200, "qa_claims": 20000, "qa_claim_evidence": 40000, "qa_evidence": 20000,
    "qa_conflicts": 4000, "qa_reasoning_traces": 20000, "qa_stage_runs": 2000,
    "memory_item": 20000, "memory_version": 40000, "memory_entity_link": 40000,
    "memory_evidence_link": 40000, "memory_relation": 40000, "memory_recall_log": 20000,
    "memory_write_decision": 40000, "memory_usage_stat": 20000, "memory_validation": 20000,
    "memory_contradiction": 20000, "source_reliability_memory": 20000,
    "skill_performance_memory": 20000,
}
# 归纳只需要的 result 子键（**这一条 SQL 就是投影口径**，随快照一起留档）：
#   performance / stats / queries / excluded / search_status / cache / search_errors / 证据条数
STAGE_DETAIL_KEYS = ("performance", "stats", "queries", "excluded", "search_status", "cache",
                     "search_errors")
PROBE = ("SELECT table_name, column_name, data_type FROM information_schema.columns "
         "WHERE table_schema='public' AND table_name IN (%s) ORDER BY table_name, ordinal_position"
         % ",".join("'%s'" % name for name in TABLES))


def _counts_sql(tables) -> str:
    return " UNION ALL ".join(
        "SELECT '%s' AS t, count(*) AS n FROM %s" % (name, name) for name in tables)


def _select_for(table: str, columns: set) -> str:
    select = []
    for target, candidates in TARGETS[table].items():
        found = next((name for name in candidates if name in columns), "")
        if not found:
            cast = "int" if target in INT_TARGETS else (
                "float" if target in FLOAT_TARGETS else "text")
            select.append(("NULL::%s AS %s" % (cast, target)))
            continue
        cast = "int" if target in INT_TARGETS else ("float" if target in FLOAT_TARGETS else "text")
        select.append("%s::%s AS %s" % (found, cast, target))
    order = "created_at" if "created_at" in columns else (
        "run_id" if "run_id" in columns else ("memory_id" if "memory_id" in columns else "1"))
    where = ""
    if table == "qa_stage_runs":
        where = ""
    return "SELECT %s FROM %s%s ORDER BY %s LIMIT %d" % (
        ", ".join(select), table, where, order, DEFAULT_LIMITS[table])


def _stage_runs_sql(columns: set) -> str:
    """`qa_stage_runs` 的**投影** SQL：只取归纳要用的子键（不是整份 details）。

    为什么投影而不是整份 `details_json`：真机一行 details 有 50KB+（里面是完整证据正文），
    全导会让快照膨胀到几十 MB；而经验归纳只需要"这一轮发生了什么"的骨架。
    投影形状与**运行期一致**（`{"result": {...}, "performance": {...}}`），
    这样离线侧读法与管线里读法一模一样（`qa_memory_experience.build_trace` 直接吃）。
    投影口径写死在这里并随快照留档（`stage_projection_keys`），可复核。
    """
    parts = []
    for name in ("run_id", "stage", "attempt", "status", "latency_ms", "node_id", "node_kind",
                 "round_index", "error_code"):
        parts.append("%s::text AS %s" % (name, name) if name in columns
                     else "NULL::text AS %s" % name)
    result_parts = []
    for key in STAGE_DETAIL_KEYS:
        result_parts.append("'%s', coalesce(details_json::jsonb->'result'->'%s', 'null'::jsonb)"
                            % (key, key))
    result_parts.append("'evidence_count', coalesce(jsonb_array_length("
                        "details_json::jsonb->'result'->'evidence'), 0)")
    parts.append("jsonb_build_object('result', jsonb_build_object(%s), "
                 "'performance', coalesce(details_json::jsonb->'performance', '{}'::jsonb))::text "
                 "AS details_projection" % ", ".join(result_parts))
    return ("SELECT %s FROM qa_stage_runs ORDER BY started_at, stage, attempt LIMIT %d"
            % (", ".join(parts), DEFAULT_LIMITS["qa_stage_runs"]))


def _decode(rows: list, *, stage_rows: bool = False) -> list:
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        if stage_rows:
            raw = item.pop("details_projection", None)
            try:
                item["details"] = json.loads(raw) if raw else {}
            except ValueError:
                item["details"] = {}
        for key in JSON_TARGETS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                try:
                    item[key] = json.loads(value)
                except ValueError:
                    item[key] = None
        if stage_rows:
            for key in ("latency_ms", "round_index", "attempt"):
                try:
                    item[key] = int(item.get(key) or 0)
                except (TypeError, ValueError):
                    item[key] = 0
        out.append(item)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 12 经验记忆只读快照导出")
    parser.add_argument("--host", default=os.environ.get("QA_SSH_HOST", DEFAULT_HOST))
    parser.add_argument("--user", default=os.environ.get("QA_SSH_USER", "root"))
    parser.add_argument("--password", default=os.environ.get("QA_SSH_PASSWORD", ""))
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--out", default=os.path.join("baseline", "qa-experience-real-sample.json"))
    parser.add_argument("--timeout", type=int, default=40)
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
            "query": ("qa_runs/qa_claims/qa_claim_evidence/qa_evidence/qa_conflicts/"
                      "qa_reasoning_traces/qa_stage_runs（仅投影 result 子键） + memory_* + "
                      "source_reliability_memory/skill_performance_memory（各一条 SELECT，带 LIMIT）"),
            "snapshot_version": SNAPSHOT_VERSION,
            "stage_projection_keys": list(STAGE_DETAIL_KEYS),
        },
        "errors": [],
    }

    def run(sql: str, key: str) -> str:
        stdin, stdout, stderr = client.exec_command(_remote(args.container, args.database, sql),
                                                    timeout=args.timeout + 30)
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
    snapshot["phase12_tables_present"] = [name for name in PHASE12_TABLES if name in columns]
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
    print("记忆表存在：%s" % (snapshot["memory_tables_present"] or "无（Phase 09 未发布）"))
    print("Phase 12 投影表存在：%s" % (snapshot["phase12_tables_present"] or "无（Phase 12 未发布）"))
    for table in TABLES:
        if table not in columns:
            snapshot[table] = []
            print("%-28s %5d 行（远端没有这张表）" % (table, 0))
            continue
        sql = _stage_runs_sql(columns[table]) if table == "qa_stage_runs" \
            else _select_for(table, columns[table])
        text = run(_agg(sql), table)
        rows = _pick_json(text)
        snapshot[table] = _decode(rows if isinstance(rows, list) else [],
                                  stage_rows=(table == "qa_stage_runs"))
        print("%-28s %5d 行" % (table, len(snapshot[table])))
    # ── 库内**已有**向量（P09/P12 的向量通道用）：只导"真实证据引用到的文章"那一小撮 ──
    article_ids = sorted({int(row.get("article_id")) for row in snapshot.get("qa_evidence") or []
                          if str(row.get("article_id") or "").strip().isdigit()})
    snapshot["embedding_article_ids"] = article_ids
    snapshot["intel_article_embeddings"] = []
    if article_ids and "intel_article_embeddings" in _remote_tables(run, client, args):
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
    if snapshot["errors"]:
        print("有 %d 条查询失败（见产物 errors）：%s"
              % (len(snapshot["errors"]), snapshot["errors"][:2]))
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False)
    print("快照已写入 %s（%.1f KB）" % (args.out, os.path.getsize(args.out) / 1024.0))
    return 0


def _remote_tables(run, client, args) -> set:
    """远端真实存在的表名（用于可选表的探测；探测失败就当作不存在）。"""
    sql = ("SELECT coalesce(json_agg(t.table_name)::text,'[]') FROM (SELECT table_name FROM "
           "information_schema.tables WHERE table_schema='public' AND table_name IN "
           "('intel_article_embeddings','articles') LIMIT 20) t")
    text = run(sql, "table_probe")
    rows = _pick_json(text)
    return set(rows or []) if isinstance(rows, list) else set()


if __name__ == "__main__":
    sys.exit(main())
