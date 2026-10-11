#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 14 真机**只读**探针（A 机 Postgres：一次连接、多条短命查询、带 LIMIT）。

它做三件事（口径与 Phase 06~13 的快照工具一致，**零写操作**）：
  1. 探发布态：`/app/qa_schema.py` 的 `QA_SCHEMA_VERSION`、Phase 14 两个新模块是否在位、
     `qa_realignment_decisions` 表是否存在（A 机未发布 Phase 14 时应为"不存在"）；
  2. 导**真实问题流**：`qa_runs`（含 `session_id`）—— 这正是 P14-07 的"重复提问检出数与
     交互次数分布"在真机上要的口径（比不带 session_id 的旧快照更完整）；
  3. 导 `qa_stage_runs(stage='level1_retrieval')` 的**缺口环投影**（stop_reason / 新增已核验
     证据 / 高优缺口），让验收工具能走完整口径而不是口径替代。

安全口径（硬约束）：
  · 凭据只从环境变量取（`QA_SSH_HOST` / `QA_SSH_USER` / `QA_SSH_PASSWORD`），脚本不写死口令；
  · 每条远端命令都是**短命**的：`timeout N docker exec ... psql -c "BEGIN READ ONLY; SET LOCAL
    statement_timeout='Ns'; <一条 SELECT>; COMMIT;"`，全部带 LIMIT；
  · 只有 SELECT；不在容器里放任何文件（结果直接走 stdout）；失败退避重试（默认 2 次，间隔递增）；
  · 收尾断开连接，不留长事务（BEGIN READ ONLY + COMMIT 由同一条 -c 完成）。

用法：
    QA_SSH_PASSWORD=*** python tools/qa_phase14_real_probe.py --out baseline/qa-realignment-real-sample.json
    python tools/qa_phase14_real_probe.py --probe
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
    DEFAULT_CONTAINER, DEFAULT_DATABASE, DEFAULT_HOST, _agg, _connect, _remote,
)

SNAPSHOT_VERSION = "qa-realignment-real-sample-v1"
REMOTE_TIMEOUT_SECONDS = 25

# 文件类检查必须落在**应用容器**上：默认容器是数据库容器（只跑 psql，没有 /app 代码目录），
# 早期版本把两者用同一个容器名，导致 "ls /app/qa_realignment.py" 恒报 No such file（实测踩到）。
WEB_CONTAINER = os.environ.get("QA_WEB_CONTAINER", "collectinfo-web")
MAX_RUNS = 60
MAX_GAP_ROWS = 120

RUNS_SQL = _agg("""
    SELECT id, question_text, session_id, owner_user_id, industry_pack_id, mode, status,
           created_at, completed_at, degradation_json, final_answer_json
    FROM qa_runs
    WHERE question_text <> ''
    ORDER BY created_at DESC
    LIMIT %d
""" % MAX_RUNS)

GAPS_SQL = _agg("""
    SELECT run_id, stage, status,
           coalesce((details_json::jsonb #>> '{result,stats,gap_loop,stop_reason}'), '') AS stop_reason,
           coalesce((details_json::jsonb #>> '{result,stats,gap_loop,stats,new_verified_claims_total}'), '') AS new_verified,
           coalesce((details_json::jsonb #>> '{result,stats,gap_loop,stats,resolved_high_priority_total}'), '') AS resolved_high,
           coalesce((details_json::jsonb #>> '{result,stats,gap_loop,stats,high_priority_open}'), '') AS high_open,
           coalesce((details_json::jsonb #>> '{result,stats,gap_loop,stats,open_gaps}'), '') AS open_gaps
    FROM qa_stage_runs
    WHERE stage = 'level1_retrieval' AND details_json LIKE '%%gap_loop%%'
    ORDER BY started_at DESC
    LIMIT %d
""" % MAX_GAP_ROWS)

PROBE_SQL = ("SELECT "
             "(SELECT count(*) FROM information_schema.tables "
             " WHERE table_name = 'qa_realignment_decisions') AS realignment_table, "
             "(SELECT count(*) FROM qa_runs) AS runs, "
             "(SELECT count(*) FROM qa_runs WHERE session_id <> '') AS runs_with_session")


def _shell(client, command: str, *, retries: int = 2) -> str:
    """一条短命远端命令（失败退避重试；绝不挂住）。"""
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            _stdin, stdout, stderr = client.exec_command(command, timeout=REMOTE_TIMEOUT_SECONDS)
            out = stdout.read().decode("utf-8", "replace").strip()
            err = stderr.read().decode("utf-8", "replace").strip()
            if out or not err:
                return out
        except Exception:  # noqa: BLE001 —— 超时/断连都退避重试，绝不挂住
            pass
        time.sleep(delay)
        delay *= 2
    return ""


def _sql(client, sql: str, container: str, database: str) -> str:
    return _shell(client, "timeout %d %s" % (REMOTE_TIMEOUT_SECONDS,
                                             _remote(container, database, sql)))


def probe(client, container: str, database: str) -> dict:
    """发布态探针：schema 版本 / 新模块 / 新表 / 真机问题流规模。

    注意：`container` 是**数据库容器**（跑 psql），而三个文件类检查必须落在**应用容器**
    （`WEB_CONTAINER`，代码目录 /app 在它里面）——两者混用会让文件检查恒报不存在。
    """
    web = WEB_CONTAINER
    out = {
        "web_container": web,
        "schema_version": _shell(client, "timeout 20 docker exec %s grep -h 'QA_SCHEMA_VERSION =' "
                                         "/app/qa_schema.py" % web),
        "phase14_modules": _shell(client, "timeout 20 docker exec %s sh -c "
                                          "'ls /app/qa_realignment.py /app/qa_acquisition.py 2>&1'"
                                          % web),
        "gap_analyzer_version": _shell(client, "timeout 20 docker exec %s grep -h 'GAP_ANALYZER_VERSION =' "
                                               "/app/qa_gap_analyzer.py" % web),
        "counts": _sql(client, PROBE_SQL, container, database),
    }
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("QA_SSH_HOST", DEFAULT_HOST))
    parser.add_argument("--user", default=os.environ.get("QA_SSH_USER", "root"))
    parser.add_argument("--password", default=os.environ.get("QA_SSH_PASSWORD", ""))
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--out", default=os.path.join("baseline", "qa-realignment-real-sample.json"))
    parser.add_argument("--probe", action="store_true", help="只探发布态，不导数据")
    args = parser.parse_args()

    if not args.password:
        print("缺少凭据：请设置 QA_SSH_PASSWORD（脚本不写死口令）")
        return 2

    snapshot = {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": {"host": args.host, "container": args.container, "database": args.database,
                   "access": ("readonly (BEGIN READ ONLY + statement_timeout=%ds + LIMIT)"
                              % REMOTE_TIMEOUT_SECONDS),
                   "snapshot_version": SNAPSHOT_VERSION,
                   "note": "Phase 14 真机只读探针：问题流 + 缺口环投影；零写操作、零模型调用。"},
        "probe": {}, "runs": [], "gap_loop": [], "errors": [],
    }
    client = _connect(args.host, args.user, args.password, timeout=15)
    try:
        snapshot["probe"] = probe(client, args.container, args.database)
        if args.probe:
            print(json.dumps(snapshot, ensure_ascii=False, indent=1))
            return 0
        for key, sql in (("runs", RUNS_SQL), ("gap_loop", GAPS_SQL)):
            try:
                payload = _sql(client, sql, args.container, args.database)
                snapshot[key] = json.loads(payload) if payload else []
            except Exception as exc:  # noqa: BLE001 —— 一段失败只记一段
                snapshot["errors"].append({"section": key, "error": "%s: %s"
                                           % (type(exc).__name__, str(exc)[:160])})
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=1)
    print("快照已写入 %s（runs %d 行 / 缺口环 %d 行 / 错误 %d 条）"
          % (args.out, len(snapshot["runs"]), len(snapshot["gap_loop"]),
             len(snapshot["errors"])))
    print("发布态：%s" % json.dumps(snapshot["probe"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
