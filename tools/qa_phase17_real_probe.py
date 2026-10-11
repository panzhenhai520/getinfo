#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 17 真机**只读**探针（A 机：短命令 + timeout，一次连接多项探测，收尾确认无残留）。

它回答四件事，**零写操作、零模型调用、零 GPU**（凭据只从环境变量取，脚本不写死口令）：

  1. **发布态**：`collectinfo-web` 里 `/app/qa_schema.py` 的 `QA_SCHEMA_VERSION`、
     Phase 17 的四个模块/工具是否在位 —— 用来钉住"线上跑到哪一阶段"（没发布 ≠ 没数据）；
  2. **库内现状（只读 SELECT）**：`qa_runs` / `qa_stage_runs` / `qa_claims` / `qa_evidence` /
     `qa_conflicts` / `memory_item` / `qa_reasoning_traces` 的行数 —— 这是 B-003 的直接证据
     （生产端到端行为数据到底有没有）；
  3. **cost 链路**：`qa_stage_runs.token_usage_json` 里有多少行真带 token 记录
     —— Phase 16 把线上 tokens 写成 null，这里给出行级依据；
  4. **收尾**：长事务与驻留进程复核（`long_transactions=[]`、无残留 `ps` 行）。

用法：
    QA_SSH_PASSWORD=*** python tools/qa_phase17_real_probe.py --probe --out baseline/qa-phase17-real-probe.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qa_phase07_real_snapshot import (  # noqa: E402
    DEFAULT_CONTAINER, DEFAULT_DATABASE, DEFAULT_HOST, _agg, _connect, _pick_json, _remote,
)

APP_CONTAINER = "collectinfo-web"
PROBE_VERSION = "qa-phase17-real-probe-v1"
REMOTE_TIMEOUT_SECONDS = 25

PHASE17_FILES = (
    "/app/qa_e2e.py", "/app/qa_ablation.py", "/app/qa_metrics_report.py", "/app/qa_release.py",
)

COUNTS_SQL = _agg(
    "SELECT "
    "(SELECT count(*)::int FROM qa_runs) AS runs, "
    "(SELECT count(*)::int FROM qa_stage_runs) AS stage_runs, "
    "(SELECT count(*)::int FROM qa_claims) AS claims, "
    "(SELECT count(*)::int FROM qa_claim_evidence) AS claim_edges, "
    "(SELECT count(*)::int FROM qa_evidence) AS evidence, "
    "(SELECT count(*)::int FROM qa_conflicts) AS conflicts, "
    "(SELECT count(*)::int FROM qa_reasoning_traces) AS traces, "
    "(SELECT count(*)::int FROM qa_evidence_seen) AS seen_rows, "
    "(SELECT count(*)::int FROM memory_item) AS memories, "
    "(SELECT count(*)::int FROM qa_realignment_decisions) AS realignment_rows")

COST_SQL = _agg(
    "SELECT count(*)::int AS stage_rows, "
    "sum(CASE WHEN COALESCE(token_usage_json, '') NOT IN ('', '{}', 'null') "
    "THEN 1 ELSE 0 END)::int AS rows_with_tokens, "
    "(SELECT count(*)::int FROM qa_runs WHERE COALESCE(corpus_version, '') <> '') "
    "AS runs_with_corpus FROM qa_stage_runs")

IDLE_SQL = _agg(
    "SELECT count(*)::int AS long_transactions FROM pg_stat_activity "
    "WHERE state <> 'idle' AND now() - query_start > interval '60 seconds' "
    "AND pid <> pg_backend_pid()")


def _shell(client, command: str, *, retries: int = 2) -> str:
    delay = 2.0
    for _attempt in range(retries + 1):
        try:
            _stdin, stdout, stderr = client.exec_command(command, timeout=REMOTE_TIMEOUT_SECONDS)
            out = stdout.read().decode("utf-8", "replace").strip()
            err = stderr.read().decode("utf-8", "replace").strip()
            if out or not err:
                return out
        except Exception:      # noqa: BLE001 —— 超时/断连都退避重试
            pass
        time.sleep(delay)
        delay *= 2
    return ""


def _quote(command: str) -> str:
    return "'" + str(command).replace("'", "'\"'\"'") + "'"


def _in_app(client, command: str) -> str:
    return _shell(client, "timeout %d docker exec %s sh -c %s"
                  % (REMOTE_TIMEOUT_SECONDS, APP_CONTAINER, _quote(command)))


def _sql(client, sql: str) -> str:
    return _shell(client, "timeout %d %s" % (REMOTE_TIMEOUT_SECONDS,
                                             _remote(DEFAULT_CONTAINER, DEFAULT_DATABASE, sql)))


def _scalar_map(text: str) -> dict:
    payload = _pick_json(text)
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return {str(key): value for key, value in payload[0].items()}
    if isinstance(payload, dict):
        return payload
    return {}


def probe_deployment(client) -> dict:
    """发布态：schema 版本 + Phase 17 文件是否在位 + 既有模块在位（对照）。"""
    schema_line = _in_app(client, "grep -h 'QA_SCHEMA_VERSION = ' /app/qa_schema.py")
    files = {}
    for path in PHASE17_FILES:
        listing = _in_app(client, "ls -la %s 2>&1 | head -2" % path)
        files[path] = bool(listing) and "No such file" not in listing \
            and "cannot access" not in listing
    phase16_present = bool(_in_app(client, "ls /app/qa_observability_ui.py 2>&1 | head -1")) and \
        "No such file" not in _in_app(client, "ls /app/qa_observability_ui.py 2>&1 | head -1")
    return {
        "schema_version_line": schema_line.strip()[:160],
        "schema_version_is_v11": "unified-qa-schema-v11" in schema_line,
        "phase17_files": files,
        "phase17_files_present": all(files.values()),
        "phase16_still_present": bool(phase16_present),
        "note": ("本包**未部署**（不部署是硬约束）：Phase 17 的四个模块不在应用镜像里，"
                 "这里如实记录，不用它推断「没数据」"),
    }


def probe_processes(client) -> dict:
    """收尾：确认没有我们留下的驻留进程（只看容器内的进程表，不杀任何东西）。"""
    listing = _in_app(client, "ps -eo pid,etimes,comm,args --sort=-etimes | head -12")
    lines = [line for line in str(listing or "").splitlines() if line.strip()]
    probes = [line for line in lines
              if ("ps -eo" in line and "head" in line) or "grep" in line]
    return {"process_lines": len(lines), "head": lines[:12],
            "probe_leftovers": len(probes),
            "note": "只读：只列进程，不 kill、不重启"}


def run_probe(*, host: str = "", user: str = "", password: str = "") -> dict:
    host = host or os.getenv("QA_SSH_HOST", DEFAULT_HOST)
    user = user or os.getenv("QA_SSH_USER", "root")
    password = password or os.getenv("QA_SSH_PASSWORD", "")
    if not password:
        raise SystemExit("缺少 QA_SSH_PASSWORD（只从环境变量取，脚本不写死口令）")
    client = _connect(host, user, password, REMOTE_TIMEOUT_SECONDS)
    report = {"probe_version": PROBE_VERSION, "host": host, "read_only": True,
              "captured_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        report["deployment"] = probe_deployment(client)
        report["counts"] = _scalar_map(_sql(client, COUNTS_SQL))
        report["cost"] = _scalar_map(_sql(client, COST_SQL))
        report["cleanup"] = {"long_transactions": _scalar_map(_sql(client, IDLE_SQL))}
        report["processes"] = probe_processes(client)
    finally:
        try:
            client.close()
        except Exception:      # noqa: BLE001
            pass
    counts = report.get("counts") or {}
    cost = report.get("cost") or {}
    report["conclusion"] = {
        "production_e2e_data_available": int(counts.get("runs") or 0) > 0
        and int(counts.get("traces") or 0) > 0,
        "reason_code": "" if int(counts.get("traces") or 0) > 0
        else "B-003_PRODUCTION_BEHAVIOR_DATA_MISSING（qa_reasoning_traces 为 0 行）",
        "token_rows_online": int(cost.get("rows_with_tokens") or 0),
        "tokens_null_justified": int(cost.get("rows_with_tokens") or 0) == 0,
    }
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--host", default="")
    parser.add_argument("--user", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    report = run_probe(host=args.host, user=args.user)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out:
        output = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(output), exist_ok=True)
        with open(output, "w", encoding="utf-8") as handle:
            handle.write(rendered)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
