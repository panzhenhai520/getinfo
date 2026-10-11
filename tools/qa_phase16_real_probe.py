#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 16 真机**只读**探针（A 机：短命令 + timeout，一次连接多项探测，收尾不留进程）。

它做四件事，**零写操作、零模型调用、零 GPU**：

  1. 探**发布态**：`/app/qa_schema.py` 的 `QA_SCHEMA_VERSION`、Phase 16 的三个文件
     （`qa_observability_ui.py` / `qa_observability_api.py` / `templates/qa_observability.html`）
     是否在位、导航入口是否加上 —— 这是"线上跑到哪一阶段"的直接证据（没发布 ≠ 没数据）；
  2. 探**未登录的访问控制**：页面 `GET /qa-observability` 必须 302 跳登录、
     `GET /api/qa/v1/observability/views` 必须 401（既有约定）；
  3. 探**登录后的真实 JSON**：用容器内的 `DEFAULT_ADMIN_USERNAME` 与容器内 `.env` 的
     `DEFAULT_ADMIN_PASSWORD` 登录，拿到会话 cookie 后拉
     `/api/qa/v1/observability/views` 与 `/api/qa/v1/observability/dashboard`，
     再拉一次 run 级聚合（库里没有 run 时如实记 **0 个 run / 空态**，不猜、不造）；
  4. 探**库内现状**（只读 SELECT）：`qa_runs` / `qa_stage_runs` / `qa_reasoning_traces` /
     `memory_item` 的行数 —— 用来解释"为什么界面是空态"。

安全口径（硬约束）：
  · 凭据只从环境变量取（`QA_SSH_HOST` / `QA_SSH_USER` / `QA_SSH_PASSWORD`），脚本不写死口令；
  · 容器内只跑 `grep/ls/curl/python -c`，**不落文件、不改配置、不重启容器**；
  · 每条远端命令都 `timeout N`，全部带 LIMIT；
  · 登录失败（例如管理员口令已被用户改过）**如实记录"未取得登录态"**，不猜口令、不改口令。

用法：
    QA_SSH_PASSWORD=*** python tools/qa_phase16_real_probe.py --probe
    QA_SSH_PASSWORD=*** python tools/qa_phase16_real_probe.py --out baseline/qa-observability-ui-real-probe.json
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
PROBE_VERSION = "qa-observability-ui-real-probe-v1"
REMOTE_TIMEOUT_SECONDS = 25

PHASE16_FILES = (
    "/app/qa_observability_ui.py",
    "/app/qa_observability_api.py",
    "/app/templates/qa_observability.html",
)

COUNTS_SQL = _agg("SELECT "
                  "(SELECT count(*)::int FROM qa_runs) AS runs, "
                  "(SELECT count(*)::int FROM qa_stage_runs) AS stage_runs, "
                  "(SELECT count(*)::int FROM qa_reasoning_traces) AS reasoning_traces, "
                  "(SELECT count(*)::int FROM qa_events) AS events, "
                  "(SELECT count(*)::int FROM qa_retrieval_cache) AS cache_rows, "
                  "(SELECT count(*)::int FROM memory_item) AS memories, "
                  "(SELECT count(*)::int FROM memory_recall_log) AS recall_rows")


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


def _in_app(client, command: str) -> str:
    return _shell(client, "timeout %d docker exec %s sh -c %s"
                  % (REMOTE_TIMEOUT_SECONDS, APP_CONTAINER, _quote(command)))


def _quote(command: str) -> str:
    return "'" + str(command).replace("'", "'\"'\"'") + "'"


def _sql(client, sql: str) -> str:
    return _shell(client, "timeout %d %s" % (REMOTE_TIMEOUT_SECONDS,
                                             _remote(DEFAULT_CONTAINER, DEFAULT_DATABASE, sql)))


# 只读快照要导的表（**只 SELECT、只带 LIMIT**）。列名一律用 `SELECT *` +
# `row_to_json` 原样导出，绝不假设本机列集与线上一致（老库可能少列）。
SNAPSHOT_TABLES = (
    ("qa_runs", "SELECT * FROM qa_runs ORDER BY created_at DESC LIMIT 40"),
    ("qa_stage_runs", "SELECT * FROM qa_stage_runs WHERE run_id IN "
                      "(SELECT id FROM qa_runs ORDER BY created_at DESC LIMIT 3) LIMIT 400"),
    ("qa_reasoning_traces", "SELECT * FROM qa_reasoning_traces LIMIT 400"),
    ("qa_retrieval_cache", "SELECT * FROM qa_retrieval_cache LIMIT 300"),
    ("qa_claims", "SELECT * FROM qa_claims LIMIT 500"),
    ("qa_claim_evidence", "SELECT * FROM qa_claim_evidence LIMIT 1000"),
    ("qa_conflicts", "SELECT * FROM qa_conflicts LIMIT 200"),
    ("qa_evidence", "SELECT * FROM qa_evidence LIMIT 500"),
    ("memory_item", "SELECT * FROM memory_item LIMIT 500"),
    ("memory_recall_log", "SELECT * FROM memory_recall_log LIMIT 500"),
    ("memory_validation", "SELECT * FROM memory_validation LIMIT 500"),
    ("memory_write_decision", "SELECT * FROM memory_write_decision LIMIT 500"),
    ("memory_evidence_link", "SELECT * FROM memory_evidence_link LIMIT 500"),
    ("memory_entity_link", "SELECT * FROM memory_entity_link LIMIT 500"),
    ("memory_relation", "SELECT * FROM memory_relation LIMIT 500"),
)

SNAPSHOT_VERSION = "qa-observability-ui-real-snapshot-v1"


def export_snapshot(client) -> dict:
    """把六视图要读的那些表**只读**导出（每张表都是单条带 LIMIT 的 SELECT）。"""
    tables: dict = {}
    for name, sql in SNAPSHOT_TABLES:
        rows = _pick_json(_sql(client, _agg(sql)))
        tables[name] = rows if isinstance(rows, list) else []
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tables": tables,
        "row_counts": {name: len(rows) for name, rows in tables.items()},
    }


def _scalar_map(text: str) -> dict:
    """`_agg()` 产出的是**单行 JSON**（`[{...}]`），取第一条即为计数表。"""
    payload = _pick_json(text)
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return {str(key): value for key, value in payload[0].items()}
    if isinstance(payload, dict):
        return payload
    return {}


def probe_deployment(client) -> dict:
    """发布态：schema 版本、Phase 16 文件是否在位、导航入口。"""
    schema_line = _in_app(client, "grep -h 'QA_SCHEMA_VERSION = ' /app/qa_schema.py")
    files = {}
    for path in PHASE16_FILES:
        listing = _in_app(client, "ls -la %s 2>&1 | head -2" % path)
        files[path] = bool(listing) and "No such file" not in listing and "cannot access" not in listing
    nav = _in_app(client, "grep -c 'qa-observability' /app/templates/_dashboard_nav.html 2>/dev/null")
    blueprint = _in_app(client, "grep -c 'observability_bp' /app/firecrawl_app.py 2>/dev/null")
    try:
        nav_count = int(str(nav).strip().splitlines()[-1])
    except (TypeError, ValueError):
        nav_count = 0
    try:
        blueprint_count = int(str(blueprint).strip().splitlines()[-1])
    except (TypeError, ValueError):
        blueprint_count = 0
    return {
        "schema_version_line": schema_line.strip()[:120],
        "phase16_files": files,
        "phase16_files_present": all(files.values()),
        "nav_entry_count": nav_count,
        "blueprint_registered": blueprint_count > 0,
    }


def probe_unauthenticated(client) -> dict:
    """未登录的访问控制（既有约定：页面 302、API 401）。"""
    page = _in_app(client, "curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "
                           "-m 10 http://127.0.0.1:8003/qa-observability")
    api = _in_app(client, "curl -s -o /dev/null -w '%{http_code}' -m 10 "
                          "http://127.0.0.1:8003/api/qa/v1/observability/views")
    body = _in_app(client, "curl -s -m 10 http://127.0.0.1:8003/api/qa/v1/observability/views | head -c 300")
    parts = str(page or "").strip().split()
    return {
        "page_code": parts[0] if parts else "",
        "page_redirect": parts[1] if len(parts) > 1 else "",
        "api_code": str(api or "").strip().splitlines()[-1] if api else "",
        "api_body_head": str(body or "")[:300],
    }


def _admin_login(client) -> dict:
    """用容器内的默认管理员账号登录（口令取自容器内环境变量，不写进脚本/日志）。"""
    result = _in_app(
        client,
        "python - <<'PY'\n"
        "import json, os, urllib.request\n"
        "user = os.environ.get('DEFAULT_ADMIN_USERNAME', '')\n"
        "pwd = os.environ.get('DEFAULT_ADMIN_PASSWORD', '')\n"
        "print(json.dumps({'username': user, 'password_len': len(pwd)}))\n"
        "PY")
    info = {}
    try:
        info = json.loads(str(result).strip().splitlines()[-1]) if result else {}
    except (ValueError, TypeError, IndexError):
        info = {}
    if not info.get("username") or not info.get("password_len"):
        return {"login_attempted": False,
                "reason": "容器内没有 DEFAULT_ADMIN_USERNAME / DEFAULT_ADMIN_PASSWORD，未尝试登录",
                "session": False}
    login = _in_app(
        client,
        "python - <<'PY'\n"
        "import json, os, urllib.request\n"
        "payload = json.dumps({'username': os.environ.get('DEFAULT_ADMIN_USERNAME',''),\n"
        "                      'password': os.environ.get('DEFAULT_ADMIN_PASSWORD','')}).encode()\n"
        "req = urllib.request.Request('http://127.0.0.1:8003/api/user/login', data=payload,\n"
        "                             headers={'Content-Type': 'application/json'})\n"
        "try:\n"
        "    resp = urllib.request.urlopen(req, timeout=12)\n"
        "    body = json.loads(resp.read().decode('utf-8','replace') or '{}')\n"
        "    cookie = resp.headers.get('Set-Cookie') or ''\n"
        "    token = ''\n"
        "    for part in cookie.split(';'):\n"
        "        if part.strip().startswith('session_token='):\n"
        "            token = part.strip().split('=', 1)[1]\n"
        "    print(json.dumps({'code': resp.status, 'success': bool(body.get('success')),\n"
        "                      'message': str(body.get('error') or body.get('message') or '')[:160],\n"
        "                      'has_token': bool(token), 'token': token[:400]}))\n"
        "except Exception as exc:\n"
        "    detail = ''\n"
        "    try:\n"
        "        detail = exc.read().decode('utf-8','replace')[:160]\n"
        "    except Exception:\n"
        "        detail = ''\n"
        "    print(json.dumps({'code': 0, 'success': False,\n"
        "                      'error': str(exc)[:120], 'detail': detail}))\n"
        "PY")
    try:
        data = json.loads(str(login).strip().splitlines()[-1]) if login else {}
    except (ValueError, TypeError, IndexError):
        data = {}
    if not data.get("has_token"):
        return {"login_attempted": True,
                "reason": "登录未取得会话（口令可能已被用户改过）——如实记录未取得登录态",
                "session": False, "login_code": data.get("code"),
                "login_success": bool(data.get("success")),
                "login_message": str(data.get("message") or data.get("detail")
                                     or data.get("error") or "")[:160]}
    return {"login_attempted": True, "session": True, "login_code": data.get("code"),
            "login_success": bool(data.get("success")),
            "token": str(data.get("token") or "")}


def probe_authenticated(client, token: str) -> dict:
    """登录后的真实 JSON（只读接口；库里没有 run 时如实记空态）。"""
    def _get(path: str, limit: int = 4000) -> dict:
        raw = _in_app(client, "curl -s -m 15 -H 'Cookie: session_token=%s' "
                              "http://127.0.0.1:8003%s | head -c %d" % (token, path, limit))
        try:
            return json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            return {"_raw_head": str(raw or "")[:400]}

    catalog = _get("/api/qa/v1/observability/views")
    dashboard = _get("/api/qa/v1/observability/dashboard", 6000)
    runs = _get("/api/qa/v1/runs?limit=5")
    run_ids = [str(item.get("id") or "") for item in (runs.get("runs") or [])]
    run_view = {}
    if run_ids:
        run_view = _get("/api/qa/v1/runs/%s/observability/execution" % run_ids[0], 6000)
    return {
        "catalog_views": [item.get("view") for item in catalog.get("views") or []],
        "catalog_read_only": bool(catalog.get("read_only")),
        "dashboard_state": (dashboard.get("observability") or {}).get("state"),
        "dashboard_reason": (dashboard.get("observability") or {}).get("state_reason"),
        "dashboard_numbers": len((dashboard.get("observability") or {}).get("numbers") or {}),
        "runs_returned": len(run_ids),
        "run_scoped_probe": ({"run_id": run_ids[0],
                              "state": (run_view.get("observability") or {}).get("state"),
                              "reason": (run_view.get("observability") or {}).get("state_reason")}
                             if run_ids else {"note": "库里没有 run —— run 级视图在真机上是空态"}),
    }


def run_probe(*, host: str = "", user: str = "", password: str = "",
              snapshot: bool = False) -> dict:
    host = host or os.getenv("QA_SSH_HOST", DEFAULT_HOST)
    user = user or os.getenv("QA_SSH_USER", "root")
    password = password or os.getenv("QA_SSH_PASSWORD", "")
    if not password:
        raise SystemExit("缺少 QA_SSH_PASSWORD（只从环境变量取，脚本不写死口令）")
    client = _connect(host, user, password, REMOTE_TIMEOUT_SECONDS)
    report = {"probe_version": PROBE_VERSION, "host": host,
              "captured_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        report["deployment"] = probe_deployment(client)
        report["unauthenticated"] = probe_unauthenticated(client)
        report["counts"] = _scalar_map(_sql(client, COUNTS_SQL))
        login = _admin_login(client)
        token = str(login.pop("token", ""))
        report["login"] = login
        report["authenticated"] = (probe_authenticated(client, token) if token
                                   else {"note": "未取得登录态，登录后的接口未探测（如实记录）"})
        if snapshot:
            data = export_snapshot(client)
            report["snapshot"] = {"snapshot_version": data["snapshot_version"],
                                  "captured_at_utc": data["captured_at_utc"],
                                  "row_counts": data["row_counts"]}
            report["_snapshot_tables"] = data["tables"]
    finally:
        try:
            client.close()
        except Exception:      # noqa: BLE001
            pass
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--snapshot", action="store_true",
                        help="额外把六视图要读的表只读导出（供本地投影层跑真机数据）")
    parser.add_argument("--host", default="")
    parser.add_argument("--user", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--snapshot-out", default="",
                        help="快照落盘路径（只写本机文件，不写远端）")
    args = parser.parse_args(argv)
    report = run_probe(host=args.host, user=args.user, snapshot=args.snapshot)
    tables = report.pop("_snapshot_tables", None)
    if tables is not None and args.snapshot_out:
        snapshot = {"snapshot_version": SNAPSHOT_VERSION,
                    "captured_at_utc": report["captured_at_utc"],
                    "host": report["host"], "tables": tables,
                    "row_counts": (report.get("snapshot") or {}).get("row_counts") or {}}
        output = os.path.abspath(args.snapshot_out)
        os.makedirs(os.path.dirname(output), exist_ok=True)
        with open(output, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False, sort_keys=True)
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
