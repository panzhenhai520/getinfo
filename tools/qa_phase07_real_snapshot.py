#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 07 真机快照导出（**只读**：BEGIN READ ONLY + statement_timeout + LIMIT）。

它只做一件事：把 A 机 Postgres 上缺口分析需要的几张表**只读**导成一份 JSON，
供 `tools/qa_phase07_gap_acceptance.py` 离线复算（那个工具不联网、不连库）。

安全口径（沿用 Phase 06 快照 + 本阶段硬约束）：
  · SSH 凭据只从环境变量取（`QA_SSH_HOST` / `QA_SSH_USER` / `QA_SSH_PASSWORD`），
    脚本里**不写死任何口令**；
  · 每条远端命令都是**短命**的：一条 `docker exec ... psql -c "BEGIN READ ONLY; SET LOCAL
    statement_timeout='20s'; <一条 SELECT>; COMMIT;"`，带 `LIMIT`；
  · 只有 SELECT，没有写操作；不在容器里放任何文件（结果直接走 stdout）；
  · 一次一个连接，读完即关；任何超时/失败都记账并继续（绝不挂在远端）。

用法：
    QA_SSH_PASSWORD=*** python tools/qa_phase07_real_snapshot.py --out baseline/qa-gap-real-sample.json
    python tools/qa_phase07_real_snapshot.py --probe          # 只看表结构（不落盘）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

DEFAULT_HOST = "117.50.211.93"
DEFAULT_CONTAINER = "collectinfo-postgres"
DEFAULT_DATABASE = "collectinfo"
STATEMENT_TIMEOUT = "20s"
TABLES = ("qa_runs", "qa_claims", "qa_claim_evidence", "qa_conflicts", "qa_evidence",
          "qa_reasoning_traces", "qa_evidence_seen", "qa_stage_runs")

# 每个目标字段的候选列名（**按实际库结构自适应**：A 机的库版本比本机旧，
# 列名是 payload_json / scope_json / conflict_key，且没有 v7 的缺口三列与 route 等）。
# 目标字段缺失时导出 NULL —— 快照里如实反映"这台机器上这一列不存在"，而不是编一个值。
TARGETS = {
    "qa_runs": {
        "id": ["id"], "question_text": ["question_text"], "mode": ["mode"],
        "industry_pack_id": ["industry_pack_id"], "status": ["status"],
        "corpus_version": ["corpus_version"], "created_at": ["created_at"],
        "completed_at": ["completed_at"],
    },
    "qa_claims": {
        "run_id": ["run_id"], "claim_key": ["claim_key"], "stage": ["stage"],
        "claim_text": ["claim_text"], "verification_status": ["verification_status"],
        "payload": ["payload", "payload_json"],
    },
    "qa_claim_evidence": {
        "run_id": ["run_id"], "claim_key": ["claim_key"], "evidence_ref": ["evidence_ref"],
        "relationship": ["relationship"], "relevance_score": ["relevance_score"],
        "published_at": ["published_at"], "scope": ["scope", "scope_json"],
    },
    "qa_conflicts": {
        "run_id": ["run_id"], "conflict_id": ["conflict_id", "conflict_key"],
        "subject": ["subject"], "conflict_type": ["conflict_type"],
        "claim_ids": ["claim_ids", "claim_ids_json"], "evidence_refs": ["evidence_refs"],
        "resolution": ["resolution"], "rationale": ["rationale"],
        "rule_version": ["rule_version"],
    },
    "qa_evidence": {
        "run_id": ["run_id"], "evidence_ref": ["evidence_ref"], "source_type": ["source_type"],
        "source_url": ["source_url"], "source_title": ["source_title"],
        "published_at": ["published_at"], "authority_level": ["authority_level"],
        "payload": ["payload", "payload_json"],
    },
    "qa_reasoning_traces": {
        "run_id": ["run_id"], "hop_index": ["hop_index"], "round_index": ["round_index"],
        "sub_query_id": ["sub_query_id"], "sub_query": ["sub_query"], "route": ["route"],
        "results": ["results"], "accepted": ["accepted"], "rejected": ["rejected"],
        "new_claims": ["new_claims"], "resolved_gap": ["resolved_gap"], "gap_id": ["gap_id"],
        "status": ["status"], "latency_ms": ["latency_ms"],
    },
    "qa_evidence_seen": {
        "owner_user_id": ["owner_user_id"], "session_id": ["session_id"],
        "industry_pack_id": ["industry_pack_id"], "source_fingerprint": ["source_fingerprint"],
        "status": ["status"],
    },
    # 只取检索阶段的行：跳数与耗时的**真机**基线（缺口循环的耗时对比要有个真数）
    "qa_stage_runs": {
        "run_id": ["run_id"], "stage": ["stage"], "status": ["status"],
        "latency_ms": ["latency_ms"],
    },
}
INT_TARGETS = {"hop_index", "round_index", "results", "accepted", "rejected", "new_claims",
               "resolved_gap", "latency_ms", "authority_level", "relevance_score"}
JSON_TARGETS = {"payload", "claim_ids", "evidence_refs", "scope"}
DEFAULT_LIMITS = {
    "qa_runs": 60, "qa_claims": 4000, "qa_claim_evidence": 8000, "qa_conflicts": 2000,
    "qa_evidence": 6000, "qa_reasoning_traces": 4000, "qa_evidence_seen": 4000,
    "qa_stage_runs": 200,
}
COUNTS = ""   # 由 _counts_sql(现有表) 现拼
PROBE = ("SELECT table_name, column_name, data_type FROM information_schema.columns "
         "WHERE table_schema='public' AND table_name IN (%s) ORDER BY table_name, ordinal_position"
         % ",".join("'%s'" % name for name in TABLES))


def _counts_sql(tables) -> str:
    """只统计**远端真的存在**的表（缺表不该让整条统计语句报错）。"""
    return " UNION ALL ".join(
        "SELECT '%s' AS t, count(*) AS n FROM %s" % (name, name) for name in tables)


def _pick_json(text: str):
    """从 psql 输出里挑出那一行 JSON（多语句会带 BEGIN/SET/COMMIT 行）。"""
    for line in reversed([item.strip() for item in (text or "").splitlines()]):
        if not line or line[0] not in "[{":
            continue
        try:
            return json.loads(line)
        except ValueError:
            continue
    return []


def _select_for(table: str, columns: set) -> str:
    """按实际列集拼 SELECT（缺的目标字段写 NULL，且注明缺哪几列）。"""
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
    order = "run_id" if "run_id" in columns else ("created_at" if "created_at" in columns else "1")
    where = " WHERE stage='level1_retrieval'" if table == "qa_stage_runs" else ""
    return "SELECT %s FROM %s%s ORDER BY %s LIMIT %d" % (", ".join(select), table, where, order,
                                                         DEFAULT_LIMITS[table])


def _wrap(sql: str) -> str:
    """把一条 SELECT 包进只读事务 + 语句超时（远端只跑这一条）。"""
    return ("BEGIN READ ONLY; SET LOCAL statement_timeout='%s'; %s; COMMIT;"
            % (STATEMENT_TIMEOUT, str(sql).strip().rstrip(";")))


def _remote(container: str, database: str, sql: str) -> str:
    escaped = _wrap(sql).replace('"', '\\"')
    return ('docker exec %s psql -U postgres -d %s -v ON_ERROR_STOP=1 -t -A -c "%s"'
            % (container, database, escaped))


def _agg(inner: str) -> str:
    """一条 SELECT 的**整表 JSON**（单行返回）：`SELECT coalesce(json_agg(row_to_json(t))::text,'[]') FROM (<inner>) t`。"""
    return ("SELECT coalesce(json_agg(row_to_json(t))::text,'[]') FROM (%s) t"
            % str(inner).strip().rstrip(";"))


QUERIES = {}   # 由 _select_for 按实际列集现拼（见 main）


def _connect(host: str, user: str, password: str, timeout: int):
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, password=password, timeout=timeout,
                   allow_agent=False, look_for_keys=False)
    return client


def _decode(rows: list) -> list:
    """把 text 形态的 JSON 列解回对象（A 机的 payload_json 是 text，本机是 jsonb）。"""
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
    parser.add_argument("--out", default=os.path.join("baseline", "qa-gap-real-sample.json"))
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
            "query": "qa_runs/qa_claims/qa_claim_evidence/qa_conflicts/qa_evidence/"
                     "qa_reasoning_traces/qa_evidence_seen（各一条 SELECT，带 LIMIT）",
            "snapshot_version": "qa-gap-real-sample-v1",
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

    if args.probe:
        text = run(PROBE, "probe")
        current = ""
        for line in text.splitlines():
            parts = line.strip().split("|")
            if len(parts) != 3:
                continue
            if parts[0] != current:
                current = parts[0]
                print("\n[%s]" % current)
            print("   %-28s %s" % (parts[1], parts[2]))
        client.close()
        return 0

    columns = {}
    for line in run(PROBE, "probe").splitlines():
        parts = line.strip().split("|")
        if len(parts) == 3:
            columns.setdefault(parts[0], set()).add(parts[1])
    missing_tables = [name for name in TABLES if name not in columns]
    if missing_tables:
        snapshot["errors"].append({"key": "probe", "error": "远端缺表：%s" % missing_tables})
    snapshot["schema"] = {name: sorted(values) for name, values in sorted(columns.items())}
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
    if missing_tables:
        print("远端缺表（本机有、A 机没有）：%s" % missing_tables)
    for key, table in (("runs", "qa_runs"), ("claims", "qa_claims"),
                       ("edges", "qa_claim_evidence"), ("conflicts", "qa_conflicts"),
                       ("evidence", "qa_evidence"), ("traces", "qa_reasoning_traces"),
                       ("seen", "qa_evidence_seen"), ("stages", "qa_stage_runs")):
        if table not in columns:
            snapshot[key] = []
            print("%-10s %5d 行（远端没有这张表）" % (key, 0))
            continue
        text = run(_agg(_select_for(table, columns[table])), key)
        rows = _pick_json(text)
        if not rows and "[]" not in [item.strip() for item in text.splitlines()]:
            snapshot["errors"].append({"key": key, "error": "结果不是合法 JSON（前 200 字）：%s"
                                       % text[:200]})
        snapshot[key] = _decode(rows if isinstance(rows, list) else [])
        print("%-10s %5d 行" % (key, len(snapshot[key])))
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
