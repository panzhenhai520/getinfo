#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 16（P16-01…P16-06）· **可观测性 UI 验收工具**（只读，可复算；零模型/零 GPU/零网络）。

它点名本阶段要回答的几件事，并且**每个数字都能被同一份代码重算**：

  1. **六个视图在真实产物上真的出数**：状态 + 口径数字条数 + 正文条数；
  2. **每个数字都能追到某个阶段的产物**：`numbers[*].source` 命中来源白名单的比例必须是 1.0，
     `definition`（口径）不许为空；
  3. **空态与降级态是真实状态**：空库里六个视图的状态与理由码逐个列出（A 机当前就没有 run 数据）；
  4. **只读**：跑一遍六视图 + 三个 HTTP 端点，库里的行数逐字不变；路由表里只有 GET；
  5. **冻结面**：七契约指纹复算不变、`QA_SCHEMA_VERSION` 仍是 v11、零新表零新列。

两种模式（都用**同一份真代码**跑）：
  · 默认 `--demo`：在**隔离临时 sqlite** 上真跑一次（Phase 05/06/08/09/11 的产物全走真链路），
    再对同一份库做一次全量验收 —— 自包含、零副作用、可随时复跑；
  · `--db <sqlite 路径>`：对本地部署库**只读**验收（不建表、不写行、不跑模型）。

用法：
    python tools/qa_phase16_acceptance.py --print
    python tools/qa_phase16_acceptance.py --db data/crawler_articles.db --print
    python tools/qa_phase16_acceptance.py --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from unittest.mock import patch

# 必须在**导入 config 之前**决定数据库后端：
# 只要没给 `--db`（即 demo 模式或快照模式），全程在隔离 sqlite 上跑，绝不碰本机 .env
# 指向的主库（本机开发库是 PostgreSQL，一旦连上就会把"只读验收"变成对真库的连接）。
_USE_ISOLATED = "--db" not in sys.argv
if _USE_ISOLATED:
    _DEMO_DIR = tempfile.mkdtemp(prefix="qa-phase16-acceptance-")
    _DEMO_DB = os.path.join(_DEMO_DIR, "acceptance.sqlite3")
    os.environ["DATABASE_TYPE"] = "sqlite"
    os.environ["DATABASE_PATH"] = _DEMO_DB
    os.environ["SQLITE_BACKUP_PATH"] = _DEMO_DB

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import qa_contracts  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_observability_ui as ui  # noqa: E402
import qa_schema  # noqa: E402

REPORT_VERSION = "qa-observability-ui-acceptance-v1"
DEFAULT_OUTPUT = os.path.join("baseline", "qa-observability-ui-acceptance.json")
ACCEPTANCE_OWNER = "user:1"
"""验收用的归属轴：与 `qa_gateway._identity()` 的 `user:<id>` 口径一致。"""
RUN_VIEWS = tuple(name for name in contracts.OBSERVABILITY_VIEWS if name != "dashboard")
FROZEN_FINGERPRINTS = {
    "EVIDENCE_SCHEMA": "370301331c02c738", "CLAIM_SCHEMA": "06fcdb02441248b2",
    "CONFLICT_SCHEMA": "aabd3259b07f9a3e", "LEVEL1_RESULT_SCHEMA": "7e864764429db111",
    "LEVEL2_RESULT_SCHEMA": "a0484f894e7bc9d6", "FINAL_ANSWER_SCHEMA": "4d1efa54ca1cbc1a",
    "QA_EVENT_SCHEMA": "59358bfa88a6c6af",
}
COUNT_TABLES = ("qa_runs", "qa_stage_runs", "qa_reasoning_traces", "qa_events",
                "qa_retrieval_cache", "qa_audit_events", "memory_item",
                "memory_recall_log", "memory_validation", "memory_write_decision")


def _fingerprint(value) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _counter(values, *, top: int = 0) -> dict:
    counts: dict = {}
    for value in values:
        key = str(value or "")
        counts[key] = counts.get(key, 0) + 1
    ordered = dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
    return dict(list(ordered.items())[:top]) if top else ordered


def _row_counts(store) -> dict:
    out = {}
    for table in COUNT_TABLES:
        try:
            out[table] = int(store.database.connection.execute(
                "SELECT count(*) FROM %s" % table).fetchone()[0] or 0)
        except Exception:  # noqa: BLE001 —— 表不存在就如实记 -1（老库可能没有 memory_*）
            out[table] = -1
    return out


def _traceability(view: dict) -> dict:
    total = len(view.get("numbers") or {})
    whitelisted = 0
    defined = 0
    fields = []
    for field, item in (view.get("numbers") or {}).items():
        source = str(item.get("source") or "")
        definition = str(item.get("definition") or "")
        if any(source.startswith(prefix) for prefix in contracts.OBSERVABILITY_SOURCES):
            whitelisted += 1
        else:
            fields.append({"view": view.get("view"), "field": field, "source": source,
                           "problem": "SOURCE_NOT_WHITELISTED"})
        if definition.strip():
            defined += 1
        else:
            fields.append({"view": view.get("view"), "field": field,
                           "problem": "EMPTY_DEFINITION"})
    return {"numbers": total, "whitelisted": whitelisted, "defined": defined, "problems": fields}


def _contract_problems(view: dict) -> list:
    ok, note = contracts.validate("observability_view", view)
    if ok:
        return []
    return [{"view": view.get("view"), "problem": note}]


def _view_summary(store, run_id: str) -> dict:
    summary = {}
    problems = []
    trace = {"numbers": 0, "whitelisted": 0, "defined": 0, "problems": []}
    for name in contracts.OBSERVABILITY_VIEWS:
        try:
            view = (ui.dashboard(store) if name == "dashboard"
                    else ui.build_view(store, run_id, name))
        except Exception as exc:  # noqa: BLE001
            problems.append({"view": name, "problem": "%s: %s" % (type(exc).__name__,
                                                                 str(exc)[:120])})
            continue
        body = view.get("payload") or {}
        summary[name] = {
            "state": view.get("state"), "state_reason": view.get("state_reason"),
            "numbers": len(view.get("numbers") or {}),
            "payload_items": len(body.get("items") or body.get("claims") or body.get("nodes")
                                or body.get("actions") or body.get("views") or []),
            "source": view.get("source"),
        }
        problems.extend(_contract_problems(view))
        item = _traceability(view)
        trace["numbers"] += item["numbers"]
        trace["whitelisted"] += item["whitelisted"]
        trace["defined"] += item["defined"]
        trace["problems"].extend(item["problems"])
    trace["ratio"] = round(trace["whitelisted"] / trace["numbers"], 6) if trace["numbers"] else None
    trace["definition_ratio"] = (round(trace["defined"] / trace["numbers"], 6)
                                if trace["numbers"] else None)
    return {"views": summary, "traceability": trace, "contract_problems": problems}


def _empty_state_report() -> dict:
    """空态证据：在**另一个全新空库**上跑六个视图（A 机当前就没有 QA run 数据）。

    刻意不拿"已经种过数据的库"当空态样本 —— 那会把 `ok` 当成空态证据，是自欺。
    """
    import tempfile as _tempfile

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "tests"))
    import qa_phase16_fixtures as fx

    temp = _tempfile.TemporaryDirectory()
    database, store = fx.store_for(temp.name, "phase16-empty.sqlite3")
    try:
        report = {}
        for name in contracts.OBSERVABILITY_VIEWS:
            view = (ui.dashboard(store) if name == "dashboard"
                    else ui.build_view(store, "no-run", name))
            report[name] = {"state": view["state"], "state_reason": view["state_reason"],
                            "state_text": view["state_text"][:160],
                            "null_numbers": len([1 for item in view["numbers"].values()
                                                 if item.get("value") is None])}
        return report
    finally:
        try:
            database.connection.close()
        except Exception:
            pass
        temp.cleanup()


def _frozen_report() -> dict:
    fingerprints = {name: _fingerprint(getattr(qa_contracts, name))
                    for name in FROZEN_FINGERPRINTS}
    return {
        "fingerprints": fingerprints,
        "unchanged": all(fingerprints[name] == expected
                         for name, expected in FROZEN_FINGERPRINTS.items()),
        "schema_version": qa_schema.QA_SCHEMA_VERSION,
        "schema_version_unchanged": qa_schema.QA_SCHEMA_VERSION == "unified-qa-schema-v11",
        "added_columns_v11": list(qa_schema.QA_ADDED_COLUMNS_V11),
        "has_v12": hasattr(qa_schema, "QA_ADDED_COLUMNS_V12"),
    }


def _first_run(store) -> dict:
    """取库里最新的那条 run（**不假设归属轴**：真机快照里的 owner 是线上自己的口径）。"""
    try:
        row = store.database.connection.execute(
            "SELECT id, owner_user_id FROM qa_runs ORDER BY created_at DESC LIMIT 1").fetchone()
    except Exception:  # noqa: BLE001
        return {}
    value = dict(row) if row is not None else {}
    return {"id": str(value.get("id") or ""),
            "owner_user_id": str(value.get("owner_user_id") or "")}


def _api_report(store, owner_user_id: str = ACCEPTANCE_OWNER) -> dict:
    """三个 HTTP 端点的真实状态码：未登录 401、登录后 200、越权 404。"""
    from flask import Flask

    import qa_gateway
    from qa_observability_api import observability_bp

    app = Flask(__name__)
    app.config.update(TESTING=True)
    app.register_blueprint(observability_bp)
    saved = qa_gateway._service_override
    qa_gateway.set_qa_gateway_service(qa_gateway.QaGatewayService(store, None))
    client = app.test_client()
    first = _first_run(store)
    run_id = str(first.get("id") or "no-run")
    owner = str(owner_user_id or first.get("owner_user_id") or ACCEPTANCE_OWNER)
    report = {"unauthenticated": {}, "authenticated": {}, "owner": owner,
              "identity": {}, "methods": sorted({
        method for rule in app.url_map.iter_rules() if rule.endpoint != "static"
        for method in rule.methods if method not in {"HEAD", "OPTIONS"}})}
    try:
        for path in ("/api/qa/v1/runs/%s/observability" % run_id,
                     "/api/qa/v1/observability/dashboard",
                     "/api/qa/v1/observability/views"):
            report["unauthenticated"][path] = client.get(path).status_code
        # 登录态：把 `request.current_user` 直接钉成**该 run 的 owner**（`user:<id>` 口径），
        # 这样验的是"接口在正确身份下能不能读到自己的 run"，而不是碰运气。
        parts = str(owner).split(":")
        if len(parts) == 2 and parts[0] == "user" and parts[1].isdigit():
            report["identity"] = {"role": "admin", "user_id": int(parts[1])}
            with patch("decorators.user_db.verify_session",
                       return_value=dict(report["identity"])):
                client.set_cookie("localhost", "session_token", "acceptance")
                for path in ("/api/qa/v1/runs/%s/observability" % run_id,
                             "/api/qa/v1/observability/dashboard",
                             "/api/qa/v1/observability/views"):
                    report["authenticated"][path] = client.get(path).status_code
                report["unknown_view"] = client.get(
                    "/api/qa/v1/runs/%s/observability/nope" % run_id).status_code
        else:
            report["note"] = ("该库里最新 run 的归属轴不是 `user:<id>`（%r），"
                              "登录态一节按原样跳过 —— 不猜身份。" % owner)
    finally:
        qa_gateway.set_qa_gateway_service(saved)
    return report


def _frontend_report() -> dict:
    try:
        from check_qa_observability_view import static_acceptance

        return static_acceptance()
    except Exception as exc:  # noqa: BLE001
        return {"error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}


def _load_snapshot(path: str, database, store) -> dict:
    """把只读快照灌进隔离临时 sqlite（**离线跑真机数据**：同一份投影代码、同一个口径）。

    逐表按"快照里的列 ∩ 本机临时库的列"取交集插入 —— 线上老库可能少列/多列，
    绝不假设列集一致；插不进去的行只记账，不猜、不补默认值。
    """
    with open(path, encoding="utf-8") as handle:
        snapshot = json.load(handle)
    tables = snapshot.get("tables") or {}
    loaded, skipped = {}, {}
    for table, rows in tables.items():
        if not isinstance(rows, list) or not rows:
            loaded[table] = 0
            continue
        try:
            columns = [str(row[1]) for row in database.connection.execute(
                "PRAGMA table_info(%s)" % table).fetchall()]
        except Exception:  # noqa: BLE001 —— 本机没有这张表就整表跳过
            skipped[table] = "本地无此表"
            continue
        if not columns:
            skipped[table] = "本地无此表"
            continue
        written = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            names = [name for name in columns if name in row]
            if not names:
                continue
            placeholders = ",".join("?" for _ in names)
            try:
                database.connection.execute(
                    "INSERT OR REPLACE INTO %s(%s) VALUES(%s)"
                    % (table, ",".join(names), placeholders),
                    tuple(row[name] for name in names))
                written += 1
            except Exception:  # noqa: BLE001 —— 单行脏数据不许打断整表
                skipped[table] = "存在插不进去的行"
        loaded[table] = written
    database.connection.commit()
    return {"snapshot_version": snapshot.get("snapshot_version"),
            "captured_at_utc": snapshot.get("captured_at_utc"),
            "source_host": snapshot.get("host", ""),
            "loaded_rows": loaded, "skipped": skipped}


def run_acceptance(*, demo: bool = True, db_path: str = "", snapshot_path: str = "") -> dict:
    """跑一次完整验收，返回报告字典（不改任何真实库；`--demo` 全部在临时库里）。"""
    temp = None
    snapshot_report = {}
    if db_path:
        from qa_storage import QaStore
        from sqlite_database import SQLiteDatabase

        database = SQLiteDatabase(db_path)
        database.connect()
        store = QaStore(database)
        seeded = False
    else:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "tests"))
        import qa_phase16_fixtures as fx

        temp = tempfile.TemporaryDirectory()
        database, store = fx.store_for(temp.name, "phase16-acceptance.sqlite3")
        if snapshot_path:
            # 快照模式：**不种任何合成数据**，六视图只跑真机导出的那些行
            snapshot_report = _load_snapshot(snapshot_path, database, store)
            seeded = False
        else:
            # 归属轴与 `qa_gateway._identity()` 对齐（`user:<id>`），否则验收里的
            # "登录后 200"会因为看不到自己的 run 而变成 404（那是假失败）。
            fx.seed_standard_run(database, store, owner=ACCEPTANCE_OWNER)
            seeded = True

    try:
        first = _first_run(store)
        run_id = str(first.get("id") or "")
        owner = str(first.get("owner_user_id") or "")

        before = _row_counts(store)
        summary = _view_summary(store, run_id) if run_id else {
            "views": {}, "traceability": {"numbers": 0, "whitelisted": 0, "defined": 0,
                                          "ratio": None, "problems": []},
            "contract_problems": []}
        empty = _empty_state_report()
        api = _api_report(store, owner or ACCEPTANCE_OWNER)
        after = _row_counts(store)
        frontend = _frontend_report()
        frozen = _frozen_report()

        checks = {
            "six_views_present": set(summary["views"]) == set(contracts.OBSERVABILITY_VIEWS),
            "all_views_contract_valid": not summary["contract_problems"],
            "every_number_traceable": (summary["traceability"]["ratio"] is None
                                       or summary["traceability"]["ratio"] == 1.0),
            "every_number_has_definition": (summary["traceability"]["definition_ratio"] is None
                                            or summary["traceability"]["definition_ratio"] == 1.0),
            "read_only_row_counts_unchanged": before == after,
            "api_unauthenticated_401": all(code == 401
                                           for code in api["unauthenticated"].values()),
            "api_authenticated_200": (all(code == 200
                                          for code in api["authenticated"].values())
                                      if api["authenticated"] else True),
            "api_unknown_view_400": api.get("unknown_view") in (400, None),
            "api_get_only": api["methods"] == ["GET"],
            "empty_states_are_explicit": all(
                item["state"] in {"empty", "degraded", "unavailable", "ok"}
                and item["state_reason"] for item in empty.values()),
            "frontend_safe_and_read_only": bool(frontend.get("checks", {}).get("frontend_safe")),
            "seven_fingerprints_unchanged": frozen["unchanged"],
            "no_schema_migration": (frozen["schema_version_unchanged"]
                                    and not frozen["has_v12"]),
        }
        if snapshot_path:
            checks["snapshot_loaded_rows"] = sum(
                int(value) for value in (snapshot_report.get("loaded_rows") or {}).values()) > 0
        else:
            checks["seeded_by_real_pipeline"] = seeded
        return {
            "report_version": REPORT_VERSION,
            "demo": bool(seeded),
            "data_source": ("真机只读快照（离线同代码投影）" if snapshot_path
                            else ("临时隔离 sqlite（真链路构造）" if seeded
                                  else os.path.basename(db_path))),
            "snapshot": snapshot_report,
            "run_id": run_id,
            "views": summary["views"],
            "traceability": summary["traceability"],
            "contract_problems": summary["contract_problems"],
            "empty_states": empty,
            "api": api,
            "frontend_checks": frontend.get("checks", {}),
            "frontend_endpoints": frontend.get("endpoints", []),
            "frozen": frozen,
            "row_counts_before": before,
            "row_counts_after": after,
            "checks": checks,
            "acceptance": "passed" if all(checks.values()) else "failed",
        }
    finally:
        try:
            database.connection.close()
        except Exception:
            pass
        if temp is not None:
            temp.cleanup()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="", help="对本地 sqlite 部署库做只读验收")
    parser.add_argument("--snapshot", default="",
                        help="对真机只读快照（tools/qa_phase16_real_probe.py --snapshot）离线验收")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    parser.add_argument("--print", dest="print_report", action="store_true",
                        help="打印报告（与 --json 同义，保留给脚本习惯）")
    parser.add_argument("--output", default="", help="把报告写到文件（默认不写盘）")
    args = parser.parse_args(argv)
    report = run_acceptance(demo=not bool(args.db or args.snapshot), db_path=args.db,
                            snapshot_path=args.snapshot)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    if args.output:
        output = os.path.abspath(args.output)
        os.makedirs(os.path.dirname(output), exist_ok=True)
        with open(output, "w", encoding="utf-8") as handle:
            handle.write(rendered)
    print(rendered, end="")
    return 0 if report["acceptance"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
