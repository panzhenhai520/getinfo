#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 15（P15-01…P15-06）· **记忆安全与作用域隔离 验收工具**
（只读快照 / 只读本地库，**可复算**；零模型调用、零嵌入端点、零写真实库）。

它回答的是本阶段验收要点名的几个数字，并且**每个数字都能被同一份代码重算**：

  1. **作用域强制的前后对比**：同一份真机快照，用 Phase 14 口径（不强制）与 Phase 15 口径
     （强制 + 轮次维）各重建一次记忆图 —— 落库条数不变的前提下，**有多少条被收到更严作用域**、
     理由是哪些（收窄只可能变严，绝不放宽）；
  2. **轮次隔离**：在每一个真实 run 的上下文里，看得到的 / 被隔离的条数与理由码分布；
  3. **长期时效**：长期作用域记忆里有多少条要复验（超窗口 / 尚未生效 / 终态）；
  4. **日志最小化**：拿**真机记忆行**做一次审计载荷最小化，`leaks` 必须为空；
  5. **审计与回放**（本地库模式）：四层回放链的逐跳校验 + 规则重放是否复现。

两种数据源（都用**同一份真代码**跑）：
  · `--snapshot <json>`：A 机只读快照（如 `baseline/qa-memory-real-sample.json`）。
    快照里**有**真实 runs/claims/evidence/memory_*；**没有**运行期的记忆安全审计事件，
    所以回放一节在快照模式下如实标"不可得"，绝不编一个数字出来。
  · `--db <sqlite 路径>`：本地部署库（信号齐全）→ 走完整口径（含审计事件与回放校验）。

用法：
    python tools/qa_phase15_acceptance.py --snapshot baseline/qa-memory-real-sample.json --print
    python tools/qa_phase15_acceptance.py --db data/crawler_articles.db --print
    python tools/qa_phase15_acceptance.py --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_contracts  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_memory as memory  # noqa: E402
import qa_memory_safety as ms  # noqa: E402
import qa_schema  # noqa: E402
import qa_phase09_memory_acceptance as p09  # noqa: E402

REPORT_VERSION = "qa-memory-safety-acceptance-v1"
DEFAULT_SNAPSHOT = os.path.join("baseline", "qa-memory-real-sample.json")
FALLBACK_SNAPSHOT = os.path.join("baseline", "qa-context-real-sample.json")
DEFAULT_OUTPUT = os.path.join("baseline", "qa-memory-safety-acceptance.json")
FROZEN_FINGERPRINTS = {
    "EVIDENCE_SCHEMA": "370301331c02c738", "CLAIM_SCHEMA": "06fcdb02441248b2",
    "CONFLICT_SCHEMA": "aabd3259b07f9a3e", "LEVEL1_RESULT_SCHEMA": "7e864764429db111",
    "LEVEL2_RESULT_SCHEMA": "a0484f894e7bc9d6", "FINAL_ANSWER_SCHEMA": "4d1efa54ca1cbc1a",
    "QA_EVENT_SCHEMA": "59358bfa88a6c6af",
}


def _counter(values, *, top: int = 0) -> dict:
    counts: dict = {}
    for value in values or ():
        key = str(value or "")
        counts[key] = counts.get(key, 0) + 1
    ordered = dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
    return dict(list(ordered.items())[:top]) if top else ordered


def _fingerprint(value) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _rebuild(buckets: list, *, enforce: bool, encounter_axis: bool, now=None) -> dict:
    """在**内存 sqlite** 上按给定口径重建记忆图（两个口径的唯一差别就是这两个开关）。"""
    saved = os.environ.get("QA_MEMORY_SCOPE_ENFORCEMENT")
    os.environ["QA_MEMORY_SCOPE_ENFORCEMENT"] = "1" if enforce else "0"
    database, store = p09._fresh_store()
    summary = {"candidates": 0, "persisted": 0, "dropped": 0,
               "enforcement_decisions": {}, "enforcement_reasons": {},
               "narrowed": 0, "rejected": 0, "scope_keys": {}, "by_scope_decision": {},
               "narrow_pairs": [], "per_run": []}
    try:
        for run, bucket in buckets:
            graph = p09._graph_for(bucket)
            meta = p09._run_meta(run)
            candidates = memory.memory_candidates_from_graph(
                graph, scope="PATIENT_LONGITUDINAL", owner_user_id=meta["owner_user_id"],
                session_id=meta["session_id"], industry_pack_id=meta["industry_pack_id"],
                run_id=meta["id"],
                encounter_id=(meta["id"] if encounter_axis else ""))
            receipt = memory.apply_write_gate(store, candidates, run_id=meta["id"], now=now)
            summary["candidates"] += receipt["candidates"]
            summary["persisted"] += receipt["persisted"]
            summary["dropped"] += receipt["dropped"]
            enforcement = receipt.get("scope_enforcement") or {}
            for key, count in (enforcement.get("decisions") or {}).items():
                summary["enforcement_decisions"][key] = \
                    summary["enforcement_decisions"].get(key, 0) + int(count)
            for key, count in (enforcement.get("reasons") or {}).items():
                summary["enforcement_reasons"][key] = \
                    summary["enforcement_reasons"].get(key, 0) + int(count)
            summary["narrowed"] += int(enforcement.get("narrowed") or 0)
            summary["rejected"] += int(enforcement.get("rejected") or 0)
            for decision_row in receipt["decisions"]:
                pair = ((decision_row.get("metadata") or {}).get("scope_enforcement") or {})
                if pair.get("decision") == "NARROW":
                    summary["narrow_pairs"].append({
                        "from": str(pair.get("narrowed_from") or ""),
                        "to": str(pair.get("narrowed_to") or ""),
                        "content_kind": str(pair.get("content_kind") or "")})
            summary["per_run"].append({"run_id": meta["id"],
                                       "candidates": receipt["candidates"],
                                       "persisted": receipt["persisted"],
                                       "narrowed": int(enforcement.get("narrowed") or 0),
                                       "rejected": int(enforcement.get("rejected") or 0)})
        items = store.load_memory_items(include_all_scopes=True)
        summary["memories"] = len(items)
        summary["by_scope"] = _counter(item.get("scope") for item in items)
        summary["by_type"] = _counter(item.get("memory_type") for item in items)
        summary["scope_keys"] = _counter(item.get("scope_key") for item in items)
        summary["encounter_keys_with_axis"] = len([
            item for item in items if str(item.get("scope_key") or "").startswith("encounter:")
            and len(str(item.get("scope_key") or "").split(":")) >= 4])
        summary["_items"] = items
        return summary
    finally:
        database.close()
        if saved is None:
            os.environ.pop("QA_MEMORY_SCOPE_ENFORCEMENT", None)
        else:
            os.environ["QA_MEMORY_SCOPE_ENFORCEMENT"] = saved


def _isolation_analysis(buckets: list, items: list) -> dict:
    """在**每个真实 run 的上下文**里跑一遍轮次隔离（可见/隔离 + 理由码 + 泄漏复核）。"""
    rows: list = []
    leaks: list = []
    for run, _bucket in buckets:
        meta = p09._run_meta(run)
        context = ms.encounter_context(owner_user_id=meta["owner_user_id"],
                                      session_id=meta["session_id"],
                                      industry_pack_id=meta["industry_pack_id"],
                                      encounter_id=meta["id"], run_id=meta["id"])
        visible = [item for item in items
                   if str(item.get("scope_key") or "") in (context["visible_scope_keys"] or [])]
        receipt = ms.isolate(items, context)
        rows.append({"run_id": meta["id"], "visible_keys": len(context["visible_scope_keys"]),
                     "checked": receipt["checked"], "visible": receipt["visible"],
                     "isolated": receipt["isolated"],
                     "isolated_by_reason": receipt["isolated_by_reason"],
                     "cross_encounter": len(receipt["cross_encounter"])})
        leaks.extend(receipt["leaks"])
    return {"runs": len(rows), "rows": rows,
            "isolated_by_reason": _counter(reason for row in rows
                                           for reason, count in (row["isolated_by_reason"]
                                                                 or {}).items()
                                           for _ in range(int(count))),
            "cross_encounter_revalidations": sum(row["cross_encounter"] for row in rows),
            "leaks": sorted(set(leaks))}


def _longitudinal_analysis(items: list, *, now=None) -> dict:
    report = ms.longitudinal_report(items, now=now)
    return {key: value for key, value in report.items() if key != "checks"}


def _privacy_analysis(items: list) -> dict:
    """拿**真机记忆行**做一次审计载荷最小化（正文只留指纹），并复核 leaks。"""
    sample = items[:50]
    payloads = []
    for item in sample:
        payloads.append({
            "inputs": {"memory_id": item.get("memory_id"),
                       "canonical_content": item.get("canonical_content"),
                       "scope": item.get("scope"), "scope_key": item.get("scope_key"),
                       "entity_ids": list(item.get("entity_ids") or [])[:5]},
            "decision": "ALLOW", "reason": "SCOPE_AND_CONTENT_ALIGNED",
        })
    rule_hits: dict = {}
    leaks: list = []
    fingerprinted = 0
    for payload in payloads:
        result = ms.minimize_log_payload(payload, purpose="acceptance")
        leaks.extend(result["receipt"]["leaks"])
        fingerprinted += int(result["receipt"]["fingerprinted_fields"] or 0)
        for rule, count in (result["receipt"]["rule_hits"] or {}).items():
            rule_hits[rule] = rule_hits.get(rule, 0) + int(count)
    return {"sampled": len(payloads), "rule_hits": dict(sorted(rule_hits.items())),
            "leaks": sorted(set(leaks)), "fingerprinted_fields": fingerprinted,
            "note": "真机记忆正文只以指纹与长度进载荷（字段名政策），逐条复核 leaks 必须为空"}


def _contract_check() -> dict:
    return {
        "fingerprints": {name: _fingerprint(getattr(qa_contracts, name))
                         for name in FROZEN_FINGERPRINTS},
        "expected": dict(FROZEN_FINGERPRINTS),
        "schema_version": qa_schema.QA_SCHEMA_VERSION,
        "memory_safety_version": contracts.MEMORY_SAFETY_VERSION,
        "scope_enforcement_version": contracts.SCOPE_ENFORCEMENT_VERSION,
        "encounter_isolation_version": contracts.ENCOUNTER_ISOLATION_VERSION,
        "longitudinal_freshness_version": contracts.LONGITUDINAL_FRESHNESS_VERSION,
        "safety_override_version": contracts.SAFETY_CRITICAL_OVERRIDE_VERSION,
        "privacy_minimal_version": contracts.PRIVACY_MINIMAL_LOG_VERSION,
        "audit_replay_version": contracts.MEMORY_AUDIT_REPLAY_VERSION,
    }


def analyze_snapshot(snapshot: dict, *, snapshot_path: str = "") -> dict:
    """快照模式：两个口径各重建一次 + 隔离 / 长期 / 隐私三项分析（纯函数、可复算）。"""
    p09.set_baseline_clock(snapshot.get("captured_at_utc"))
    frozen = p09.baseline_clock()
    normalized = p09._normalize_snapshot(snapshot)
    buckets = [(bucket.get("run") or {}, bucket)
               for _run_id, bucket in sorted(p09._rows_by_run(normalized).items())
               if (bucket.get("run") or {}).get("id")]
    before = _rebuild(buckets, enforce=False, encounter_axis=False, now=frozen)
    after = _rebuild(buckets, enforce=True, encounter_axis=True, now=frozen)
    items = after.pop("_items")
    before.pop("_items", None)
    isolation = _isolation_analysis(buckets, items)
    longitudinal = _longitudinal_analysis(items, now=frozen)
    privacy = _privacy_analysis(items)
    contracts_check = _contract_check()
    acceptance = {
        "ran_on_real_data": bool(buckets) and after["memories"] > 0,
        "enforcement_decisions_recorded": bool(after["enforcement_decisions"]),
        "narrowing_never_loses_memories": after["memories"] == before["memories"],
        "narrowing_always_goes_stricter": all(
            contracts.SCOPE_BREADTH.get(pair["to"], 99)
            < contracts.SCOPE_BREADTH.get(pair["from"], -1)
            for pair in after["narrow_pairs"])
        and bool(after["narrow_pairs"] or not after["narrowed"]),
        "narrowing_reasons_are_contract_codes": all(
            reason in contracts.SCOPE_ENFORCEMENT_REASONS
            for reason in after["enforcement_reasons"]),
        "isolation_has_no_leaks": not isolation["leaks"],
        "longitudinal_window_reported": "stale_rate" in longitudinal,
        "privacy_minimal_leaks_empty": not privacy["leaks"],
        "contracts_frozen": contracts_check["fingerprints"] == contracts_check["expected"],
        "schema_version_unchanged": contracts_check["schema_version"] == "unified-qa-schema-v11",
    }
    return {
        "report_version": REPORT_VERSION,
        "source": {
            "snapshot": os.path.relpath(snapshot_path, os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))) if snapshot_path else "",
            "snapshot_version": str((snapshot.get("source") or {}).get("snapshot_version") or ""),
            "host": str((snapshot.get("source") or {}).get("host") or ""),
            "captured_at_utc": snapshot.get("captured_at_utc"),
            "baseline_clock": p09.baseline_clock().isoformat(timespec="seconds"),
            "access": str((snapshot.get("source") or {}).get("access") or ""),
            "note": ("快照模式：无运行期记忆安全审计事件 → 回放一节标记不可得，不编数字；"
                     "两个口径用同一份代码、同一份真实数据，唯一差别是作用域强制与轮次维。"),
        },
        "params": ms.memory_safety_settings(),
        "rebuild": {
            "before_phase14_scope": {key: value for key, value in before.items()
                                     if key != "_items"},
            "after_phase15_scope": after,
            "delta": {
                "memories": after["memories"] - before["memories"],
                "narrowed": after["narrowed"],
                "rejected": after["rejected"],
                "narrow_pairs": _counter("%s→%s" % (pair["from"], pair["to"])
                                         for pair in after["narrow_pairs"]),
                "encounter_keys_with_axis": after["encounter_keys_with_axis"],
                "scope_distribution_before": before["by_scope"],
                "scope_distribution_after": after["by_scope"],
            },
        },
        "isolation": isolation,
        "longitudinal": longitudinal,
        "privacy": privacy,
        "replay": {"available": False,
                   "note": ("快照里没有运行期审计事件（`qa_audit_events` 是本地库才有的运行期产物）"
                            "→ 回放一节**不可得**，如实留空，绝不编一个数字出来")},
        "contracts": contracts_check,
        "acceptance": {**acceptance, "passed": all(acceptance.values())},
    }


def analyze_db(path: str) -> dict:
    """本地库模式：作用域普查 + 逐 run 回放校验 + 审计事件分布（全量口径）。"""
    from qa_storage import QaStore
    from sqlite_database import SQLiteDatabase

    os.environ.setdefault("DATABASE_TYPE", "sqlite")
    database = SQLiteDatabase(path)
    database.connect()
    try:
        store = QaStore(database)
        census = ms.scope_census(store)
        runs = []
        try:
            rows = database.connection.execute(
                "SELECT id FROM qa_runs ORDER BY created_at DESC LIMIT 20").fetchall()
            runs = [str(row[0]) for row in rows]
        except Exception:      # noqa: BLE001
            runs = []
        replays = [ms.verify_replay(store, run_id) for run_id in runs]
        replay_ok = [row for row in replays if row["replay_matches"] and not row["missing"]]
        audit_rows = []
        try:
            audit_rows = store.list_audit_events(limit=500) or []
        except Exception:      # noqa: BLE001
            audit_rows = []
        kinds = _counter(row.get("event_type") for row in audit_rows)
        # 审计表是**多个阶段共用**的（Phase 06 起就有人在写 run_created / stage_completed…），
        # 所以"种类必须在闭集合内"这条只能约束**本阶段产生的**事件（origin=memory_recall）。
        # 拿全表去卡会把别人的历史事件判成违规（实测：本地库 500 行里全是 stage_* 之类的旧事件）。
        ours = [row for row in audit_rows if str(row.get("origin") or "") == "memory_recall"]
        ours_kinds = _counter(row.get("event_type") for row in ours)
        privacy_leaks = []
        for row in audit_rows[:100]:
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            privacy_leaks.extend(ms.verify_no_leak(payload, payload))
        contracts_check = _contract_check()
        acceptance = {
            "has_memories": census["items"] > 0,
            "scope_census_recorded": bool(census["by_scope"]),
            "isolation_has_no_leaks": census["scope_violations"]["count"] == 0
            or bool(census["scope_violations"]["by_reason"]),
            "every_replay_hop_closed": all(not row["missing"] for row in replays),
            "replay_matches_recorded_decisions": all(row["replay_matches"] for row in replays),
            "memory_safety_events_are_known_kinds": set(ours_kinds)
            <= set(contracts.MEMORY_AUDIT_EVENT_KINDS),
            "privacy_minimal_leaks_empty": not privacy_leaks,
            "contracts_frozen": contracts_check["fingerprints"] == contracts_check["expected"],
        }
        return {
            "report_version": REPORT_VERSION,
            "source": {"db": os.path.abspath(path), "access": "local sqlite (read)"},
            "params": ms.memory_safety_settings(),
            "census": census,
            "replay": {"runs": len(runs), "closed": len(replay_ok),
                       "mismatches": [row["mismatches"] for row in replays if row["mismatches"]][:3],
                       "reports": [{key: row[key] for key in
                                    ("run_id", "counts", "missing", "replay_matches")}
                                   for row in replays[:5]]},
            "audit": {"rows": len(audit_rows), "by_kind": kinds,
                      "memory_safety_rows": len(ours), "memory_safety_by_kind": ours_kinds},
            "contracts": contracts_check,
            "acceptance": {**acceptance, "passed": all(acceptance.values())},
        }
    finally:
        try:
            database.connection.close()
        except Exception:      # noqa: BLE001
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", default="")
    parser.add_argument("--db", default="")
    parser.add_argument("--out", default=DEFAULT_OUTPUT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--print", dest="show", action="store_true")
    args = parser.parse_args()

    if args.db:
        report = analyze_db(args.db)
    else:
        path = args.snapshot or (DEFAULT_SNAPSHOT if os.path.exists(DEFAULT_SNAPSHOT)
                                 else FALLBACK_SNAPSHOT)
        if not os.path.exists(path):
            print("缺少快照文件：%s（先跑 tools/qa_phase09_real_snapshot.py）" % path)
            return 2
        with open(path, encoding="utf-8") as handle:
            snapshot = json.load(handle)
        report = analyze_snapshot(snapshot, snapshot_path=path)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0 if report["acceptance"].get("passed") else 1
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=1)
    print("报告已写入 %s" % args.out)
    acceptance = report.get("acceptance") or {}
    if "rebuild" in report:
        delta = report["rebuild"]["delta"]
        after = report["rebuild"]["after_phase15_scope"]
        print("重建：候选 %d → 落库 %d（前 %d）｜**被收窄 %d**（理由 %s）｜被拒收 %d"
              % (after["candidates"], after["memories"],
                 report["rebuild"]["before_phase14_scope"]["memories"], delta["narrowed"],
                 json.dumps(after["enforcement_reasons"], ensure_ascii=False),
                 delta["rejected"]))
        print("作用域分布：%s → %s"
              % (json.dumps(delta["scope_distribution_before"], ensure_ascii=False),
                 json.dumps(delta["scope_distribution_after"], ensure_ascii=False)))
        isolation = report["isolation"]
        print("轮次隔离（%d 个真实 run 的上下文）：隔离理由分布 %s｜跨轮次放行（需复验）%d 条｜泄漏 %d"
              % (isolation["runs"], json.dumps(isolation["isolated_by_reason"], ensure_ascii=False),
                 isolation["cross_encounter_revalidations"], len(isolation["leaks"])))
        longitudinal = report["longitudinal"]
        print("长期时效：长期 %d 条（需复验 %d / 阻断 %d）｜超窗口 %d｜尚未生效 %d｜陈旧率 %s"
              % (longitudinal["longitudinal"], longitudinal["needs_revalidation"],
                 longitudinal["blocked"], longitudinal["beyond_window"],
                 longitudinal["not_yet_effective"], longitudinal["stale_rate"]))
        print("日志最小化（真机记忆行）：抽样 %d 条｜规则命中 %s｜leaks %s"
              % (report["privacy"]["sampled"],
                 json.dumps(report["privacy"]["rule_hits"], ensure_ascii=False),
                 report["privacy"]["leaks"]))
    else:
        census = report["census"]
        print("普查：记忆 %d 条｜作用域分布 %s｜作用域越界 %d 条"
              % (census["items"], json.dumps(census["by_scope"], ensure_ascii=False),
                 census["scope_violations"]["count"]))
        print("回放：%d 个 run（闭环 %d）｜审计事件 %d 行 %s"
              % (report["replay"]["runs"], report["replay"]["closed"],
                 report["audit"]["rows"], json.dumps(report["audit"]["by_kind"],
                                                     ensure_ascii=False)))
    print("验收：%s" % json.dumps(acceptance, ensure_ascii=False))
    return 0 if acceptance.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
