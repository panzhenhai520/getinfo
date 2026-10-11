#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性排查脚本（**只读**）：看清 A 机上那个 `idle in transaction` 连接是谁。

判断依据（全部来自 `pg_stat_activity`，不做任何写操作、不 kill 任何会话）：
  · `application_name` / `usename`：是 web 应用（gunicorn/psycopg）还是外部工具；
  · `state_change` 距今多久；
  · 最近一条 `query` 的文本（本阶段的探针查询都以 `SELECT coalesce(json_agg…` 或
    `SELECT CASE WHEN scope_key…` 开头，一眼可辨）。

用法：QA_SSH_PASSWORD=*** python tools/qa_phase15_idle_tx_inspect.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qa_phase07_real_snapshot import (  # noqa: E402
    DEFAULT_CONTAINER, DEFAULT_DATABASE, DEFAULT_HOST, _agg, _connect, _pick_json, _remote,
)

SQL = _agg("""
    SELECT pid, usename, application_name, client_addr::text AS client_addr, state,
           (now() - state_change)::text AS idle_for, (now() - query_start)::text AS query_age,
           left(query, 120) AS query
    FROM pg_stat_activity
    WHERE datname = '%s' AND state = 'idle in transaction'
    ORDER BY state_change LIMIT 5
""" % DEFAULT_DATABASE)


def main() -> int:
    password = os.environ.get("QA_SSH_PASSWORD", "")
    if not password:
        print("缺少凭据：请设置 QA_SSH_PASSWORD")
        return 2
    client = _connect(os.environ.get("QA_SSH_HOST", DEFAULT_HOST),
                      os.environ.get("QA_SSH_USER", "root"), password, timeout=15)
    try:
        _stdin, stdout, _stderr = client.exec_command(
            "timeout 25 " + _remote(DEFAULT_CONTAINER, DEFAULT_DATABASE, SQL), timeout=40)
        text = stdout.read().decode("utf-8", "replace").strip()
    finally:
        try:
            client.close()
        except Exception:      # noqa: BLE001
            pass
    rows = _pick_json(text)
    rows = rows if isinstance(rows, list) else []
    print(json.dumps(rows, ensure_ascii=False, indent=1))
    ours = [row for row in rows if "json_agg" in str(row.get("query") or "")
            or "scope_key" in str(row.get("query") or "")]
    print("判定：%s" % ("其中 %d 条属于本阶段探针（需处理）" % len(ours) if ours
                       else "没有一条属于本阶段探针（探针全是短命令，未留事务）"))
    return 1 if ours else 0


if __name__ == "__main__":
    sys.exit(main())
