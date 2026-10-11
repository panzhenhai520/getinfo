#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 15 真机**只读**探针（A 机 Postgres：一次连接、多条短命查询、带 LIMIT）。

它做三件事（口径与 Phase 06~14 的快照工具一致，**零写操作、零模型调用**）：
  1. 探发布态：`/app/qa_schema.py` 的 `QA_SCHEMA_VERSION`、`/app/qa_memory_safety.py`
     是否在位、容器列表 —— 这是"线上跑到哪一阶段"的直接证据（没发布 ≠ 没数据）；
  2. 探**记忆与作用域的真机现状**：八张 `memory_*` 表的行数、`memory_item` 的作用域分布
     （有行时）、`qa_audit_events` 的事件类型分布 —— 这就是 P15-01/P15-02 在真机上的
     "before 侧"数字；
  3. 导真实问题流与证据（`qa_runs` / `qa_claims` / `qa_claim_evidence` / `qa_evidence`，
     各一条带 LIMIT 的 SELECT），供 `tools/qa_phase15_acceptance.py` 做
     **"快照 + 同代码离线重建"** 口径的前后对比（真机上记忆表为空时，这是唯一能给出
     记忆级统计的诚实口径，验收报告里必须如实标注）。

安全口径（硬约束）：
  · 凭据只从环境变量取（`QA_SSH_HOST` / `QA_SSH_USER` / `QA_SSH_PASSWORD`），脚本不写死口令；
  · 每条远端命令都是**短命**的：`timeout N docker exec ... psql -c "BEGIN READ ONLY;
    SET LOCAL statement_timeout='Ns'; <一条 SELECT>; COMMIT;"`，全部带 LIMIT；
  · 只有 SELECT 与只读探针（`docker exec ... grep/ls`）；不在容器里放任何文件；
  · 失败退避重试（默认 2 次，间隔递增）；收尾断开连接，不留长事务。

用法：
    QA_SSH_PASSWORD=*** python tools/qa_phase15_real_probe.py --probe
    QA_SSH_PASSWORD=*** python tools/qa_phase15_real_probe.py --out baseline/qa-memory-safety-real-sample.json
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
from qa_phase09_real_snapshot import MEMORY_TABLES  # noqa: E402
from qa_phase10_real_snapshot import _select_for  # noqa: E402

EXPORT_TABLES = ("qa_runs", "qa_claims", "qa_claim_evidence", "qa_evidence")
COLUMNS_SQL = ("SELECT table_name, column_name FROM information_schema.columns "
               "WHERE table_schema='public' AND table_name IN (%s) "
               "ORDER BY table_name, ordinal_position"
               % ",".join("'%s'" % name for name in EXPORT_TABLES))

SNAPSHOT_VERSION = "qa-memory-safety-real-sample-v1"
REMOTE_TIMEOUT_SECONDS = 25
MAX_RUNS = 40
MAX_ROWS = 4000

MEMORY_COUNTS_SQL = _agg("SELECT %s" % ", ".join(
    "(SELECT count(*) FROM %s) AS %s" % (name, name) for name in MEMORY_TABLES))
SCOPE_DISTRIBUTION_SQL = _agg(
    "SELECT coalesce(scope,'') AS scope, count(*)::int AS rows FROM memory_item "
    "GROUP BY scope ORDER BY rows DESC LIMIT 20")
SCOPE_KEY_SHAPE_SQL = _agg(
    "SELECT CASE WHEN scope_key LIKE 'encounter:%' THEN 'encounter_bucket' "
    "WHEN scope_key LIKE 'session:%' THEN 'session' WHEN scope_key LIKE 'subject:%' THEN 'subject' "
    "WHEN scope_key LIKE 'org:%' THEN 'organization' WHEN scope_key = 'global' THEN 'global' "
    "ELSE 'other' END AS shape, count(*)::int AS rows FROM memory_item "
    "GROUP BY 1 ORDER BY rows DESC LIMIT 20")
AUDIT_EVENTS_SQL = _agg("SELECT event_type, count(*)::int AS rows FROM qa_audit_events "
                        "GROUP BY event_type ORDER BY rows DESC LIMIT 30")
COUNTS_SQL = _agg("SELECT "
                  "(SELECT count(*)::int FROM qa_runs) AS runs, "
                  "(SELECT count(*)::int FROM qa_runs WHERE session_id <> '') AS runs_with_session, "
                  "(SELECT count(*)::int FROM qa_runs WHERE industry_pack_id <> '') AS runs_with_pack, "
                  "(SELECT count(*)::int FROM qa_claims) AS claims, "
                  "(SELECT count(*)::int FROM qa_evidence) AS evidence, "
                  "(SELECT count(*)::int FROM qa_claim_evidence) AS edges")
RUNS_SQL = _agg("""
    SELECT id, question_text, mode, status, industry_pack_id, owner_user_id, session_id,
           corpus_version, created_at, completed_at
    FROM qa_runs WHERE question_text <> '' ORDER BY created_at DESC LIMIT %d
""" % MAX_RUNS)


def _shell(client, command: str, *, retries: int = 2) -> str:
    """一条短命远端命令（失败退避重试；绝不挂住）。"""
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


def _sql(client, sql: str, container: str, database: str) -> str:
    return _shell(client, "timeout %d %s" % (REMOTE_TIMEOUT_SECONDS,
                                             _remote(container, database, sql)))


def _scalar_map(text: str) -> dict:
    """把 `psql` 的 `name|value` 行解析成字典（只认两列；保留给按行输出的探针用）。"""
    out = {}
    for line in (text or "").splitlines():
        parts = line.strip().split("|")
        if len(parts) == 2 and parts[0]:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                out[parts[0]] = parts[1]
    return out


def _rows(text: str) -> list:
    payload = _pick_json(text)
    return payload if isinstance(payload, list) else []


def _row_map(text: str) -> dict:
    """单行 `json_agg(row_to_json(...))` → 字典（空/出错时返回空字典，绝不编数字）。"""
    payload = _pick_json(text)
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    return {}


def _normalize_payloads(rows: list) -> list:
    """把 `payload` 的 JSON **文本**还原成对象（与 Phase 09 快照的行形态一致）。

    `psql` 里 `payload::text` 拿到的是字符串，而 Phase 08/09 的复算工具按"嵌套对象"读它
    （`_decode` 只解一层）。不还原就会静默退化成"没有 payload" → 离线重建出 0 条记忆
    （实测踩到：129 条候选全部被写门按'证据不足'拒收）。这是一处**导出侧的形态对齐**，
    不改任何真机数据。
    """
    out = []
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        value = dict(row)
        raw = value.get("payload")
        if isinstance(raw, str) and raw.strip().startswith(("{", "[")):
            try:
                value["payload"] = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        out.append(value)
    return out


def probe(client, container: str, web_container: str, database: str) -> dict:
    """发布态 + 记忆/作用域现状（全部只读、带 LIMIT）。"""
    return {
        "schema_version": _shell(client, "timeout 20 docker exec %s grep -h 'QA_SCHEMA_VERSION =' "
                                         "/app/qa_schema.py" % web_container),
        "memory_safety_module": _shell(client, "timeout 20 docker exec %s sh -c "
                                               "'ls /app/qa_memory_safety.py 2>&1'" % web_container),
        "memory_module": _shell(client, "timeout 20 docker exec %s sh -c "
                                        "'ls /app/qa_memory.py 2>&1'" % web_container),
        "containers": _shell(client, "timeout 15 docker ps --format '{{.Names}} {{.Status}}'"),
        "workload": _row_map(_sql(client, COUNTS_SQL, container, database)),
        "memory_counts": _row_map(_sql(client, MEMORY_COUNTS_SQL, container, database)),
        "memory_scope_distribution": _rows(_sql(client, SCOPE_DISTRIBUTION_SQL, container,
                                                database)),
        "memory_scope_key_shapes": _rows(_sql(client, SCOPE_KEY_SHAPE_SQL, container, database)),
        "audit_event_types": _rows(_sql(client, AUDIT_EVENTS_SQL, container, database)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("QA_SSH_HOST", DEFAULT_HOST))
    parser.add_argument("--user", default=os.environ.get("QA_SSH_USER", "root"))
    parser.add_argument("--password", default=os.environ.get("QA_SSH_PASSWORD", ""))
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--web-container", default="collectinfo-web")
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--out", default=os.path.join("baseline",
                                                      "qa-memory-safety-real-sample.json"))
    parser.add_argument("--probe", action="store_true", help="只探发布态与记忆现状，不导数据")
    args = parser.parse_args()

    if not args.password:
        print("缺少凭据：请设置 QA_SSH_PASSWORD（脚本不写死口令）")
        return 2

    snapshot = {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")
                           .replace("+00:00", "Z"),
        "source": {
            "host": args.host, "container": args.container, "database": args.database,
            "access": ("readonly (BEGIN READ ONLY + statement_timeout=%ds + LIMIT)"
                       % REMOTE_TIMEOUT_SECONDS),
            "snapshot_version": SNAPSHOT_VERSION,
            "note": ("Phase 15 真机只读探针：发布态 + 记忆作用域现状 + 真实问题流/证据；"
                     "零写操作、零模型调用。记忆表为空时，记忆级统计只能走"
                     "『快照 + 同代码离线重建』口径，验收报告必须如实标注。"),
        },
        "probe": {}, "qa_runs": [], "qa_claims": [], "qa_claim_evidence": [], "qa_evidence": [],
        "errors": [],
    }
    client = _connect(args.host, args.user, args.password, timeout=15)
    try:
        snapshot["probe"] = probe(client, args.container, args.web_container, args.database)
        if args.probe:
            print(json.dumps(snapshot, ensure_ascii=False, indent=1))
            return 0
        # 列名先探一次（`payload` 与 `payload_json` 在不同版本里都出现过），
        # 再用 Phase 10 的字段契约生成 SELECT —— 不手写列名，避免"查询报错被当成没数据"。
        columns: dict = {}
        for line in _sql(client, COLUMNS_SQL, args.container, args.database).splitlines():
            parts = line.strip().split("|")
            if len(parts) == 2 and parts[0]:
                columns.setdefault(parts[0], set()).add(parts[1])
        snapshot["schema"] = {name: sorted(values) for name, values in sorted(columns.items())}
        for key in EXPORT_TABLES:
            if key not in columns:
                snapshot["errors"].append({"section": key, "error": "表不存在或无法读取列名"})
                continue
            try:
                sql = _select_for(key, columns[key])
                text = _sql(client, _agg(sql), args.container, args.database)
                if text.strip().upper().startswith("ERROR"):
                    raise RuntimeError(text.strip()[:200])
                snapshot[key] = _normalize_payloads(_rows(text))
            except Exception as exc:      # noqa: BLE001 —— 一段失败只记一段
                snapshot["errors"].append({"section": key,
                                           "error": "%s: %s" % (type(exc).__name__,
                                                                str(exc)[:160])})
    finally:
        try:
            client.close()
        except Exception:      # noqa: BLE001
            pass

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=1)
    probe_info = snapshot["probe"]
    print("快照已写入 %s（runs %d / claims %d / evidence %d / edges %d / 错误 %d）"
          % (args.out, len(snapshot["qa_runs"]), len(snapshot["qa_claims"]),
             len(snapshot["qa_evidence"]), len(snapshot["qa_claim_evidence"]),
             len(snapshot["errors"])))
    print("发布态：schema=%s / qa_memory_safety %s"
          % (probe_info.get("schema_version") or "未知",
             "在位" if "No such file" not in str(probe_info.get("memory_safety_module")) else "不在位"))
    print("真实工作量：%s" % json.dumps(probe_info.get("workload"), ensure_ascii=False))
    print("记忆表行数：%s" % json.dumps(probe_info.get("memory_counts"), ensure_ascii=False))
    print("记忆作用域分布：%s" % json.dumps(probe_info.get("memory_scope_distribution"),
                                            ensure_ascii=False))
    print("作用域键形态：%s" % json.dumps(probe_info.get("memory_scope_key_shapes"),
                                          ensure_ascii=False))
    print("审计事件类型：%s" % json.dumps(probe_info.get("audit_event_types"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
