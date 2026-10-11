#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""收尾核查（**只读**）：确认 A 机上没有本阶段留下的长事务与遗留进程。

它只查三件事，全部是短命只读查询：
  1. `pg_stat_activity` 里超过 5 秒的活动事务（应当为空 —— 本阶段的探针全是
     一条 `BEGIN READ ONLY … COMMIT` 的短命令）；
  2. `idle in transaction` 的连接数（长事务的典型形态）；
  3. web 容器里的 `/tmp` 残留与前三个最老进程（本阶段**不在容器里放任何文件**）。

用法：QA_SSH_PASSWORD=*** python tools/qa_phase15_cleanup_check.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qa_phase07_real_snapshot import (  # noqa: E402
    DEFAULT_CONTAINER, DEFAULT_DATABASE, DEFAULT_HOST, _agg, _connect, _pick_json, _remote,
)

LONG_TX_SQL = _agg("""
    SELECT pid, state, (now() - xact_start)::text AS age, left(query, 60) AS query
    FROM pg_stat_activity
    WHERE datname = '%s' AND state <> 'idle' AND xact_start IS NOT NULL
      AND now() - xact_start > interval '5 seconds'
    ORDER BY xact_start LIMIT 5
""" % DEFAULT_DATABASE)
COUNTS_SQL = _agg("""
    SELECT (SELECT count(*)::int FROM pg_stat_activity WHERE datname = '%s'
              AND state = 'idle in transaction') AS idle_in_transaction,
           (SELECT count(*)::int FROM pg_stat_activity WHERE datname = '%s'
              AND state = 'active') AS active,
           (SELECT count(*)::int FROM pg_stat_activity WHERE datname = '%s') AS connections
""" % (DEFAULT_DATABASE, DEFAULT_DATABASE, DEFAULT_DATABASE))
PROCESS_SQL = ("timeout 15 docker exec collectinfo-web sh -c "
               "'ls -1 /tmp 2>/dev/null | head -5; echo ---; "
               "ps -eo pid,etime,cmd --sort=-etime | head -3'")


def main() -> int:
    password = os.environ.get("QA_SSH_PASSWORD", "")
    if not password:
        print("缺少凭据：请设置 QA_SSH_PASSWORD（脚本不写死口令）")
        return 2
    host = os.environ.get("QA_SSH_HOST", DEFAULT_HOST)
    client = _connect(host, os.environ.get("QA_SSH_USER", "root"), password, timeout=15)
    try:
        def _sql(sql: str) -> str:
            _stdin, stdout, _stderr = client.exec_command(
                "timeout 25 " + _remote(DEFAULT_CONTAINER, DEFAULT_DATABASE, sql), timeout=40)
            return stdout.read().decode("utf-8", "replace").strip()

        def _shell(command: str) -> str:
            _stdin, stdout, _stderr = client.exec_command(command, timeout=30)
            return stdout.read().decode("utf-8", "replace").strip()

        long_tx = _pick_json(_sql(LONG_TX_SQL))
        counts = _pick_json(_sql(COUNTS_SQL))
        counts = counts[0] if isinstance(counts, list) and counts else {}
        processes = _shell(PROCESS_SQL).replace("\n", " | ")
    finally:
        try:
            client.close()
        except Exception:      # noqa: BLE001
            pass
    report = {"long_transactions": long_tx if isinstance(long_tx, list) else [],
              "connections": counts, "container_tmp_and_oldest": processes}
    print(json.dumps(report, ensure_ascii=False, indent=1))
    clean = (not report["long_transactions"]) and not int(
        counts.get("idle_in_transaction") or 0)
    print("收尾判定：%s" % ("干净（无长事务、无 idle in transaction）" if clean else "存在长事务，需处理"))
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
