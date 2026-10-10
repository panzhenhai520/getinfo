#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 08 真机快照导出（**只读**：BEGIN READ ONLY + statement_timeout + LIMIT）。

它只做一件事：把 A 机 Postgres 上组装 Context Pack / 校验生成端 grounding 需要的几张表
**只读**导成一份 JSON，供 `tools/qa_phase08_context_acceptance.py` 离线复算（那个工具不联网、不连库）。

与 Phase 07 快照的差别（本阶段新增的两个字段是 P08 验收的关键）：
  · `qa_runs.final_answer_json` —— **真机真实最终答案**，用来量生成端 grounding 的真拦截率；
  · `qa_stage_runs.token_usage_json` —— 真机真实 token 用量，用来给确定性估算器做**粗校准对照**；
  · payload 解析容错：A 机的 `payload_json` 是 text，可能是 JSON 也可能是 Python repr
    （旧版本用 str(dict) 落库），两种都能读回来。

安全口径（沿用 Phase 06/07 快照 + 本阶段硬约束）：
  · SSH 凭据只从环境变量取（`QA_SSH_HOST` / `QA_SSH_USER` / `QA_SSH_PASSWORD`），脚本里不写死口令；
  · 每条远端命令都是**短命**的：一条 `docker exec ... psql -c "BEGIN READ ONLY; SET LOCAL
    statement_timeout='20s'; <一条 SELECT>; COMMIT;"`，带 `LIMIT`；
  · 只有 SELECT，没有写操作；不在容器里放任何文件（结果直接走 stdout）；
  · 一次一个连接，读完即关；任何超时/失败都记账并继续（绝不挂在远端）。

用法：
    QA_SSH_PASSWORD=*** python tools/qa_phase08_real_snapshot.py --out baseline/qa-context-real-sample.json
    python tools/qa_phase08_real_snapshot.py --probe
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qa_phase07_real_snapshot import (  # noqa: E402
    DEFAULT_CONTAINER, DEFAULT_DATABASE, DEFAULT_HOST, STATEMENT_TIMEOUT,
    _agg, _connect, _pick_json, _remote,
)

SNAPSHOT_VERSION = "qa-context-real-sample-v1"

TABLES = ("qa_runs", "qa_claims", "qa_claim_evidence", "qa_conflicts", "qa_evidence",
          "qa_stage_runs")

# 目标字段 → 候选列名（按实际库结构自适应；缺列写 NULL 并如实反映）
TARGETS = {
    "qa_runs": {
        "id": ["id"], "question_text": ["question_text"], "mode": ["mode"],
        "status": ["status"], "industry_pack_id": ["industry_pack_id"],
        "final_answer": ["final_answer_json", "final_answer"],
        "degradation": ["degradation_json", "degradation"],
        "created_at": ["created_at"], "completed_at": ["completed_at"],
    },
    "qa_claims": {
        "run_id": ["run_id"], "claim_key": ["claim_key"], "stage": ["stage"],
        "claim_text": ["claim_text"], "verification_status": ["verification_status"],
        "payload": ["payload", "payload_json"],
    },
    "qa_claim_evidence": {
        "run_id": ["run_id"], "claim_key": ["claim_key"], "evidence_ref": ["evidence_ref"],
        "relationship": ["relationship"], "relevance_score": ["relevance_score"],
        "published_at": ["published_at"],
    },
    "qa_conflicts": {
        "run_id": ["run_id"], "conflict_id": ["conflict_id", "conflict_key"],
        "subject": ["subject"], "conflict_type": ["conflict_type"],
        "resolution": ["resolution"], "payload": ["payload", "payload_json"],
    },
    "qa_evidence": {
        "run_id": ["run_id"], "evidence_ref": ["evidence_ref"], "source_type": ["source_type"],
        "source_url": ["source_url"], "source_title": ["source_title"],
        "published_at": ["published_at"], "authority_level": ["authority_level"],
        "payload": ["payload", "payload_json"],
    },
    "qa_stage_runs": {
        "run_id": ["run_id"], "stage": ["stage"], "status": ["status"],
        "latency_ms": ["latency_ms"], "token_usage": ["token_usage_json", "token_usage"],
    },
}
INT_TARGETS = {"relevance_score", "latency_ms", "authority_level"}
JSON_TARGETS = {"payload", "final_answer", "degradation", "token_usage"}
# 阶段输出要整体落库，答案 JSON 可能很长：真机侧按 run 取最近 40 条即可
LIMITS = {"qa_runs": 40, "qa_claims": 2000, "qa_claim_evidence": 4000, "qa_conflicts": 1000,
          "qa_evidence": 4000, "qa_stage_runs": 600}
PROBE = ("SELECT table_name, column_name, data_type FROM information_schema.columns "
         "WHERE table_schema='public' AND table_name IN (%s) ORDER BY table_name, ordinal_position"
         % ",".join("'%s'" % name for name in TABLES))


def _select_for(table: str, columns: set) -> str:
    select = []
    for target, candidates in TARGETS[table].items():
        found = next((name for name in candidates if name in columns), "")
        if not found:
            select.append(("NULL::int AS %s" if target in INT_TARGETS
                           else "NULL::text AS %s") % target)
            continue
        select.append(("%s AS %s" if target in INT_TARGETS else "%s::text AS %s")
                      % (found, target))
    order = "run_id" if "run_id" in columns else (
        "created_at" if "created_at" in columns else "1")
    return "SELECT %s FROM %s ORDER BY %s LIMIT %d" % (
        ", ".join(select), table, order, LIMITS[table])


def _decode_value(text: str):
    """A 机的 text 列可能是 JSON，也可能是旧版本落下的 Python repr（`str(dict)`）。"""
    body = str(text or "").strip()
    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        pass
    try:
        value = ast.literal_eval(body)
    except (ValueError, SyntaxError):
        return None
    return value if isinstance(value, (dict, list)) else None


def _decode(rows: list) -> list:
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        for key in JSON_TARGETS:
            item[key] = _decode_value(item.get(key))
        out.append(item)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("QA_SSH_HOST", DEFAULT_HOST))
    parser.add_argument("--user", default=os.environ.get("QA_SSH_USER", "root"))
    parser.add_argument("--password", default=os.environ.get("QA_SSH_PASSWORD", ""))
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--out", default=os.path.join("baseline", "qa-context-real-sample.json"))
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--probe", action="store_true", help="只打印表结构，不导出")
    args = parser.parse_args()
    if not args.password:
        print("缺少 QA_SSH_PASSWORD（脚本里不写口令）")
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
            "query": "qa_runs(含 final_answer_json)/qa_claims/qa_claim_evidence/qa_conflicts/"
                     "qa_evidence/qa_stage_runs(含 token_usage_json)（各一条 SELECT，带 LIMIT）",
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

    columns: dict = {}
    text = run(PROBE, "probe")
    # --probe 打印过一遍，这里再取一次列集（同一条只读 SELECT，幂等）
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) == 3:
            columns.setdefault(parts[0], set()).add(parts[1])
    missing_tables = [name for name in TABLES if name not in columns]
    if missing_tables:
        snapshot["errors"].append({"key": "probe", "error": "远端缺表：%s" % missing_tables})
    snapshot["schema"] = {name: sorted(values) for name, values in sorted(columns.items())}
    catalog = {}
    counts_sql = " UNION ALL ".join(
        "SELECT '%s' AS t, count(*) AS n FROM %s" % (name, name)
        for name in TABLES if name in columns)
    if counts_sql:
        for line in run(counts_sql, "counts").splitlines():
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
                       ("evidence", "qa_evidence"), ("stages", "qa_stage_runs")):
        if table not in columns:
            snapshot[key] = []
            print("%-10s %5d 行（远端没有这张表）" % (key, 0))
            continue
        rows = _pick_json(run(_agg(_select_for(table, columns[table])), key))
        snapshot[key] = _decode(rows if isinstance(rows, list) else [])
        print("%-10s %5d 行" % (key, len(snapshot[key])))
    client.close()

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False)
    print("快照已写入 %s（%.1f KB）" % (args.out, os.path.getsize(args.out) / 1024.0))
    answers = [row for row in snapshot["runs"] if row.get("final_answer")]
    print("带真实最终答案的 run：%d / %d" % (len(answers), len(snapshot["runs"])))
    tokens = [row for row in snapshot["stages"] if row.get("token_usage")]
    print("带真实 token 用量的阶段行：%d / %d" % (len(tokens), len(snapshot["stages"])))
    if snapshot["errors"]:
        print("有 %d 条查询失败（见产物 errors）：%s"
              % (len(snapshot["errors"]), snapshot["errors"][:2]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
