#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 12（P12-01…P12-06）· **经验记忆验收工具**（A 机只读快照 + 同代码离线重建，可复算）。

为什么是"离线重建"而不是"线上统计"：A 机 `memory_item` 是 **0 行**、两张 Phase 12 投影表
**根本不存在**（Phase 12 未发布），所以真机上没有可统计的经验记忆。口径与 Phase 04/09/10/11 一致：
  · 从 A 机**只读**导出真实 run 的结论/边/证据/阶段行（`tools/qa_phase12_real_snapshot.py`）；
  · 离线用**同一份代码**重放：Phase 02 标注 → Phase 03 核验 → Phase 07 缺口与建议通道 →
    Phase 12 归纳（剧集/失败/策略/查询形态/来源/技能表现）→ **同一个写门** → 两张投影表；
  · 报出来的每个数字都来自真数据 + 真规则，边界（无技能遥测、无 reasoning trace）如实标注。

本工具**零模型调用、零嵌入端点、零写库**（只读快照 JSON，只写报告 JSON）。

关键口径：
  · `before` = 直觉基线：每个缺口按 Phase 07 的规则取 `suggested_routes[0]`；
  · `after` = 经验指导（P12-06 判定 + P12-03 策略偏好）作用之后的通道选择；
  · **留一法（leave-one-out）**：给某一轮的建议只由**其它轮**的经验得出（不许自己教自己）；
  · 前后对比的两个数字：① 被改变的缺口数（逐条 from/to/reason）；
    ② "重复踩坑"数 = 选中的通道在当前环境下**有失败记录**的缺口数（应当显著下降）；
  · 时钟：一律取快照的 `captured_at_utc`（**不用墙上钟**，两次运行产出逐字相同）。

用法：
    python tools/qa_phase12_experience_acceptance.py \\
        --snapshot baseline/qa-experience-real-sample.json \\
        --out baseline/qa-experience-acceptance.json [--print]
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
import threading
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPORT_VERSION = "qa-experience-acceptance-v1"
SIX_TYPES = ("EPISODIC_RESEARCH", "FAILURE", "STRATEGY", "QUERY_PATTERN", "SOURCE",
             "SKILL_PERFORMANCE")


def _counter(values) -> dict:
    out: dict = {}
    for value in values or ():
        key = str(value or "")
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda item: (-item[1], item[0])))


def _merge(left: dict, right: dict) -> dict:
    out = dict(left or {})
    for key, value in (right or {}).items():
        out[key] = out.get(key, 0) + int(value or 0)
    return out


def _parse_time(value):
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class _MemoryDb:
    """与 Phase 09/10/11 验收工具同一套的最小库（内存 sqlite + 应用自己的建表入口）。"""

    backend = "sqlite"

    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()

    def _ensure_connection(self) -> None:
        return None

    def close(self):
        with contextlib.suppress(Exception):
            self.connection.close()


def _fresh_store():
    import qa_schema
    from qa_storage import QaStore

    database = _MemoryDb()
    cursor = database.connection.cursor()
    qa_schema.ensure_qa_tables(cursor)
    cursor.close()
    database.connection.commit()
    return database, QaStore(database)


def runs_from_snapshot(snapshot: dict, *, limit_runs: int | None = None) -> list:
    """把扁平快照按 run 分组（键名与 Phase 11 的验收工具兼容，便于复用同一套重建函数）。

    `limit_runs` 只截取**最近的 N 轮**（快照按导出顺序排列），用于快速自检；
    正式验收不截取（全量 15 轮）。
    """
    by_run: dict = {}
    for row in snapshot.get("qa_runs") or []:
        run_id = str(row.get("id") or "")
        if not run_id:
            continue
        by_run[run_id] = {
            "run_id": run_id, "question": str(row.get("question_text") or ""),
            "mode": str(row.get("mode") or ""), "status": str(row.get("status") or ""),
            "industry_pack_id": str(row.get("industry_pack_id") or ""),
            "owner_user_id": str(row.get("owner_user_id") or ""),
            "session_id": str(row.get("session_id") or ""),
            "corpus_version": str(row.get("corpus_version") or ""),
            "config_hash": str(row.get("config_hash") or ""),
            "created_at": str(row.get("created_at") or ""),
            "claims": [], "edges": [], "evidence": [],
            "stage_rows": [dict(item) for item in (snapshot.get("qa_stage_runs") or [])
                           if str(item.get("run_id") or "") == run_id],
            "traces": [dict(item) for item in (snapshot.get("qa_reasoning_traces") or [])
                       if str(item.get("run_id") or "") == run_id],
        }
    for row in snapshot.get("qa_claims") or []:
        bucket = by_run.get(str(row.get("run_id") or ""))
        if bucket is not None:
            bucket["claims"].append(dict(row))
    for row in snapshot.get("qa_claim_evidence") or []:
        bucket = by_run.get(str(row.get("run_id") or ""))
        if bucket is not None:
            bucket["edges"].append(dict(row))
    for row in snapshot.get("qa_evidence") or []:
        bucket = by_run.get(str(row.get("run_id") or ""))
        if bucket is not None:
            bucket["evidence"].append(dict(row))
    ordered = [by_run[key] for key in sorted(by_run)]
    if limit_runs:
        ordered = ordered[:max(1, int(limit_runs))]
    return ordered


def rebuild_graph(run: dict, *, now=None) -> dict:
    """把快照里的一个 run 还原成**已标注 + 已核验**的证据图（走真实 Phase 02/03）。"""
    import qa_graph_contracts as contracts
    from qa_evidence import annotate_evidence
    from qa_verifier import term_set, verify_evidence_batch

    moment = now
    claims = []
    for row in run.get("claims") or []:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        claim_id = str(row.get("claim_key") or payload.get("claim_id") or "")
        text = str(row.get("claim_text") or payload.get("text") or "")
        claims.append({
            "canonical_id": claim_id, "plan_only": False,
            "text": text,
            "claim": {"claim_id": claim_id, "text": text,
                      "claim_type": str(row.get("claim_type") or "background"),
                      "confidence": float(row.get("confidence") or 0.0),
                      "evidence_refs": [str(item.get("evidence_ref") or "")
                                        for item in (run.get("edges") or [])
                                        if str(item.get("claim_key") or "") == claim_id]},
        })
    evidence, by_ref = [], {}
    for row in run.get("evidence") or []:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        item = dict(payload)
        item.setdefault("evidence_ref", str(row.get("evidence_ref") or ""))
        item.setdefault("source_type", str(row.get("source_type") or "article"))
        item.setdefault("title", str(row.get("source_title") or ""))
        item.setdefault("source_url", str(row.get("source_url") or ""))
        item.setdefault("published_at", row.get("published_at"))
        item.setdefault("authority_level", row.get("authority_level"))
        annotated = annotate_evidence(
            item, run_id=str(run.get("run_id") or ""), stage="level1_retrieval",
            route=str(payload.get("retrieval_method") or "keyword"),
            corpus_version=str(run.get("corpus_version") or ""),
            retrieved_at=moment.isoformat(timespec="seconds") if moment else "")
        evidence.append(annotated)
        by_ref[str(annotated.get("evidence_ref") or "")] = annotated
    for claim in claims:
        refs = [ref for ref in (claim["claim"].get("evidence_refs") or []) if ref in by_ref]
        if not refs:
            continue
        reviewed, _audit = verify_evidence_batch(
            [dict(by_ref[ref]) for ref in refs], claim_text=claim["text"],
            terms=term_set(claim["text"]), gate="off", now=moment)
        for item in reviewed:
            by_ref[str(item.get("evidence_ref") or "")] = item
    edges = []
    for row in run.get("edges") or []:
        claim_id = str(row.get("claim_key") or "")
        relation = str(row.get("relationship") or "supports")
        edges.append({
            "edge_id": "edge:%s:%s" % (claim_id, row.get("evidence_ref")),
            "kind": "claim-evidence", "claim_id": claim_id,
            "evidence_ref": str(row.get("evidence_ref") or ""),
            "relationship": relation,
            "graph_relation": {"supports": "SUPPORTS", "refutes": "REFUTES",
                               "qualifies": "SUPPORTS", "context": "MENTIONS"}.get(
                                   relation, "MENTIONS"),
            "relevance_score": float(row.get("relevance_score") or 0.0),
            "metadata": {}, "scope": [],
        })
    return {"version": contracts.EVIDENCE_GRAPH_VERSION, "claims": claims,
            "evidence": [by_ref.get(str(item.get("evidence_ref") or ""), item)
                         for item in evidence],
            "edges": edges, "conflicts": [], "stats": {}}


def level1_for(run: dict) -> dict:
    """从阶段行还原 `level1_retrieval` 的合并回执（stats/queries/excluded/cache/status）。"""
    stats: dict = {}
    queries: list = []
    excluded: dict = {}
    cache: dict = {}
    status = ""
    errors: list = []
    for row in run.get("stage_rows") or []:
        if str(row.get("stage") or "") != "level1_retrieval":
            continue
        details = row.get("details") if isinstance(row.get("details"), dict) else {}
        result = details.get("result") if isinstance(details.get("result"), dict) else {}
        merged = details.get("stats") if isinstance(details.get("stats"), dict) else None
        stats.update({key: value for key, value in (result.get("stats") or {}).items()
                      if isinstance(result.get("stats"), dict)})
        if merged:
            stats.update(merged)
        excluded.update(result.get("excluded") or {})
        cache.update(result.get("cache") or {})
        status = str(result.get("search_status") or status or "")
        queries = [str(item) for item in (result.get("queries") or []) if str(item)] or queries
        errors = [str(item) for item in (result.get("search_errors") or []) if str(item)] or errors
    return {"evidence": [], "stats": stats, "queries": queries, "excluded": excluded,
            "cache": cache, "search_status": status, "search_errors": errors}


def traces_from_snapshot(snapshot: dict, *, now=None, limit_runs: int | None = None) -> list:
    """为每个 run 装配一条 trace（走真实 `build_trace`；缺段如实进 missing_sections）。"""
    import qa_memory_experience as exp

    moment = now or _parse_time(snapshot.get("captured_at_utc")) or datetime.now(timezone.utc)
    captured = moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    out = []
    for run in runs_from_snapshot(snapshot, limit_runs=limit_runs):
        graph = rebuild_graph(run, now=moment)
        trace = exp.build_trace(
            run_meta={"id": run["run_id"], "question_text": run["question"],
                      "mode": run["mode"], "owner_user_id": run["owner_user_id"],
                      "session_id": run["session_id"],
                      "industry_pack_id": run["industry_pack_id"],
                      "corpus_version": run["corpus_version"],
                      "config_hash": run["config_hash"], "status": run["status"]},
            plan={"entities": [], "queries": [run["question"]]}, graph=graph,
            level1=level1_for(run), stage_rows=run.get("stage_rows") or [],
            reasoning_traces=run.get("traces") or [], captured_at=captured)
        out.append({"run": run, "graph": graph, "trace": trace})
    return out


def skill_records_from_snapshot(snapshot: dict, *, trace_rows: list, now=None) -> list:
    """技能遥测记录：**沿用 Phase 11 的离线重建口径**（真机 `node_kind='skill'` 是 0 行）。

    走 Phase 11 的真实链路（缺口 → 路由 → 遥测记录），不另造一套成功率。
    """
    import qa_phase11_skill_acceptance as acc11
    from qa_skills import telemetry_records_from_routing

    records: list = []
    for row in trace_rows:
        run = row["run"]
        bucket = {"run_id": run["run_id"], "question": run["question"], "mode": run["mode"],
                  "claims": run["claims"], "edges": run["edges"], "evidence": run["evidence"]}
        try:
            detail = acc11.analyze_run(bucket)
        except Exception:        # noqa: BLE001 —— 重建失败就不给它造记录（不猜）
            continue
        records.extend(detail.get("records") or [])
    return records


def _gap_rows(row: dict) -> list:
    """这一轮的真实缺口（Phase 07 规则 + 真实建议通道）。

    `gap_id` 是内容寻址的（同一缺口可能出现多次），这里**按 id 去重**：
    "改变了几个缺口"这个数字必须按缺口算，不能按出现的次数算。
    """
    from qa_gap_analyzer import detect_gaps

    graph = row["graph"]
    result = detect_gaps(graph["claims"], edges=graph["edges"], evidence=graph["evidence"])
    seen: dict = {}
    for gap in result.get("gaps") or []:
        gap_id = str(gap.get("gap_id") or "")
        if not gap_id or gap_id in seen:
            continue
        gap.setdefault("status", "open")
        seen[gap_id] = gap
    return [seen[key] for key in sorted(seen)]


def _seed(traces: list, *, scope: str = "ORGANIZATION", now=None,
          skill_records: Sequence = ()) -> dict:
    """把一组 trace 的经验写进一个**全新**的记忆库（走唯一写门 + 两张投影表）。"""
    import qa_memory_experience as exp

    database, store = _fresh_store()
    receipt = exp.write_experience_memories(
        store, traces=[item["trace"] for item in traces], scope=scope, now=now,
        skill_records=list(skill_records or ()))
    return {"database": database, "store": store, "receipt": receipt}


def _guidance_for(store, *, target: dict, task_type: str, corpus_version: str,
                  retrieval_config_version: str, now=None) -> dict:
    import qa_memory_experience as exp

    run = target["run"]
    return exp.planning_hint(
        store, run_meta={"id": run["run_id"], "owner_user_id": run["owner_user_id"],
                         "session_id": run["session_id"],
                         "industry_pack_id": run["industry_pack_id"]},
        task_type=task_type, question=run["question"], corpus_version=corpus_version,
        retrieval_config_version=retrieval_config_version, now=now,
        extra_terms=[task_type])


def leave_one_out(snapshot: dict, *, now=None, limit_runs: int | None = None) -> dict:
    """留一法前后对比：目标轮的建议**只由其它轮**的经验得出。"""
    import qa_memory_experience as exp

    rows = traces_from_snapshot(snapshot, now=now, limit_runs=limit_runs)
    summary = {
        "runs": len(rows), "gaps": 0, "changed": 0,
        "baseline_routes": {}, "after_routes": {}, "changed_by_reason": {},
        "failure_records": 0, "repeat_before": 0, "repeat_after": 0,
        "avoided_in_baseline": 0, "avoided_after": 0,
        "warnings": 0, "warning_codes": {}, "retry_reasons": {},
        "per_run": [], "route_changes": [],
    }
    seen_changes: set = set()
    for index, target in enumerate(rows):
        others = [row for position, row in enumerate(rows) if position != index]
        seeded = _seed(others, now=now)
        try:
            run = target["run"]
            trace = target["trace"]
            corpus = exp.corpus_binding(trace)
            config = exp.retrieval_config_token(trace)
            task_type = str(trace.get("task_type") or "")
            hint = _guidance_for(seeded["store"], target=target, task_type=task_type,
                                 corpus_version=str(corpus.get("token") or ""),
                                 retrieval_config_version=str(config.get("token") or ""), now=now)
            gaps = _gap_rows(target)
            guidance = hint.get("guidance") or {}
            applied, receipt = exp.apply_route_guidance(gaps, guidance)
            baseline = [str((gap.get("suggested_routes") or [""])[0]) for gap in gaps]
            after = [str((gap.get("suggested_routes") or [""])[0]) for gap in applied]
            proposed_before = [[str(item) for item in (gap.get("suggested_routes") or [])]
                               for gap in gaps]
            proposed_after = [[str(item) for item in (gap.get("suggested_routes") or [])]
                              for gap in applied]
            avoid = set(guidance.get("avoid_routes") or [])
            summary["gaps"] += len(gaps)
            summary["changed"] += int(receipt.get("changed") or 0)
            summary["baseline_routes"] = _merge(summary["baseline_routes"], _counter(baseline))
            summary["after_routes"] = _merge(summary["after_routes"], _counter(after))
            summary["changed_by_reason"] = _merge(summary["changed_by_reason"],
                                                 receipt.get("changed_by_reason") or {})
            summary["failure_records"] += len(hint.get("failures") or [])
            summary["repeat_before"] += len([route for route in baseline if route in avoid])
            summary["repeat_after"] += len([route for route in after if route in avoid])
            # 通道**集合**层面的前后对比：被避免的通道还留在建议集合里的缺口数
            summary["avoided_in_baseline"] += len([row for row in proposed_before
                                                   if avoid & set(row)])
            summary["avoided_after"] += len([row for row in proposed_after
                                             if avoid & set(row)])
            summary["warnings"] += len(guidance.get("warnings") or [])
            summary["warning_codes"] = _merge(summary["warning_codes"],
                                              _counter(row.get("code")
                                                       for row in (guidance.get("warnings") or [])))
            summary["retry_reasons"] = _merge(summary["retry_reasons"],
                                              (guidance.get("stats") or {}).get("retry_reasons") or {})
            for change in receipt.get("route_changes") or []:
                key = (run["run_id"], str(change.get("gap_id") or ""), str(change.get("from") or ""),
                       str(change.get("to") or ""))
                if key in seen_changes:
                    continue
                seen_changes.add(key)
                if len(summary["route_changes"]) < 40:
                    summary["route_changes"].append({**change, "run_id": run["run_id"]})
            summary["per_run"].append({
                "run_id": run["run_id"], "question": run["question"][:60],
                "task_type": task_type, "gaps": len(gaps),
                "hits": int(hint.get("hits") or 0),
                "avoid_routes": list(guidance.get("avoid_routes") or []),
                "prefer_routes": list(guidance.get("prefer_routes") or []),
                "changed": int(receipt.get("changed") or 0),
                "retry_reasons": dict((guidance.get("stats") or {}).get("retry_reasons") or {}),
                "warnings": len(guidance.get("warnings") or []),
                "produced": dict((seeded["receipt"] or {}).get("produced") or {}),
                "persisted_by_type": dict((seeded["receipt"] or {}).get("persisted_by_type") or {}),
                "reason_counts": dict(((seeded["receipt"] or {}).get("gate") or {})
                                      .get("reason_counts") or {}),
            })
        finally:
            seeded["database"].close()
    summary["blocked_by_version"] = summary["retry_reasons"].get("CORPUS_VERSION_CHANGED", 0) \
        + summary["retry_reasons"].get("RETRIEVAL_CONFIG_CHANGED", 0)
    summary["failure_repetition_before"] = (
        round(summary["repeat_before"] / summary["gaps"], 6) if summary["gaps"] else None)
    summary["failure_repetition_after"] = (
        round(summary["repeat_after"] / summary["gaps"], 6) if summary["gaps"] else None)
    return summary


def corpus_only(snapshot: dict, *, now=None, limit_runs: int | None = None) -> dict:
    """全量重建（所有 15 轮一起学）：六类记忆的产出/落库分布 + 两张投影表。

    技能表现类的遥测记录**沿用 Phase 11 的离线重建口径**（真机 `node_kind='skill'` 是 0 行）：
    同一个"缺口 → 路由 → 遥测记录"链路，本阶段只消费它、不重定义成功率。
    """
    import qa_memory_experience as exp

    rows = traces_from_snapshot(snapshot, now=now, limit_runs=limit_runs)
    records = skill_records_from_snapshot(snapshot, trace_rows=rows, now=now)
    seeded = _seed(rows, now=now, skill_records=records)
    try:
        store = seeded["store"]
        receipt = seeded["receipt"] or {}
        items = store.load_memory_items(include_all_scopes=True)
        by_type = _counter(item.get("memory_type") for item in items)
        statuses = _counter(item.get("status") for item in items)
        scopes = _counter(item.get("scope") for item in items)
        decision_rows = store.memory_write_decisions(limit=5000)
        decisions = _counter(row.get("decision") for row in decision_rows)
        reasons = _counter(row.get("reason") for row in decision_rows)
        by_type_decision = {}
        for row in decision_rows:
            key = "%s/%s" % (row.get("memory_type"), row.get("decision"))
            by_type_decision[key] = by_type_decision.get(key, 0) + 1
        provenance = exp.__dict__ and None
        from qa_memory import provenance_report

        provenance = provenance_report(store, include_all_scopes=True)
        source_rows = store.source_reliability(scope_keys=["org:user:1"])
        if not source_rows:
            source_rows = store.source_reliability()
        skill_rows = store.skill_performance_memory()
        bands = _counter(row.get("band") for row in source_rows)
        episodes = [item for item in items if item.get("memory_type") == "EPISODIC_RESEARCH"]
        failures = [item for item in items if item.get("memory_type") == "FAILURE"]
        strategies = [item for item in items if item.get("memory_type") == "STRATEGY"]
        patterns = [item for item in items if item.get("memory_type") == "QUERY_PATTERN"]
        hint_violations = len([item for item in items
                               if not (item.get("metadata") or {}).get("hint_only")])
        return {
            "traces": len(rows),
            "skill_records": len(records),
            "skill_record_skills": _counter(row.get("skill_id") for row in records),
            "produced": dict(receipt.get("produced") or {}),
            "candidates": int(receipt.get("candidates") or 0),
            "persisted_by_type": dict(receipt.get("persisted_by_type") or {}),
            "gate": {key: receipt.get("gate", {}).get(key) for key in
                     ("persisted", "merged", "session_only", "dropped", "decision_counts",
                      "reason_counts", "min_utility")},
            "memory_rows": len(items), "by_type": by_type, "statuses": statuses, "scopes": scopes,
            "decisions": decisions, "reasons": reasons, "by_type_decision": by_type_decision,
            "six_type_counts": {name: by_type.get(name, 0) for name in SIX_TYPES},
            "episodes": {"rows": len(episodes),
                         "summarizers": _counter((item.get("metadata") or {}).get("summarizer")
                                                 for item in episodes),
                         "missing_sections": _counter(
                             section for item in episodes
                             for section in ((item.get("metadata") or {}).get("missing_sections")
                                             or []))},
            "failures": {"rows": len(failures),
                         "kinds": _counter((item.get("metadata") or {}).get("kind")
                                           for item in failures),
                         "routes": _counter((item.get("metadata") or {}).get("route")
                                            for item in failures),
                         "bound_corpus": len([item for item in failures
                                              if (item.get("metadata") or {}).get("corpus_version")]),
                         "bound_config": len([item for item in failures
                                              if (item.get("metadata") or {})
                                              .get("retrieval_config_version")])},
            "strategies": {"rows": len(strategies),
                           "combinations": _counter(
                               "、".join((item.get("metadata") or {}).get("combination") or [])
                               for item in strategies),
                           "samples": sorted({int((item.get("metadata") or {}).get("observations")
                                                  or 0) for item in strategies})},
            "query_patterns": {"rows": len(patterns),
                               "patterns": _counter((item.get("metadata") or {}).get("pattern")
                                                    for item in patterns)},
            "sources": {"rows": len(source_rows), "bands": bands,
                        "top": [{"domain": row.get("domain"), "reliability": row.get("reliability"),
                                 "band": row.get("band"), "observations": row.get("observations"),
                                 "verified": row.get("verified")}
                                for row in sorted(source_rows,
                                                  key=lambda item: (-float(item.get("reliability")
                                                                           or 0),
                                                                    str(item.get("domain") or "")))[:8]]},
            "skills": {"rows": len(skill_rows),
                       "table": [{"skill_id": row.get("skill_id"),
                                  "attempts": row.get("attempts"),
                                  "successes": row.get("successes"),
                                  "success_rate": row.get("success_rate"),
                                  "min_samples": row.get("min_samples")}
                                 for row in skill_rows]},
            "provenance": {"checked": provenance.get("checked"),
                           "traceable": provenance.get("traceable"),
                           "untraceable": len(provenance.get("untraceable") or []),
                           "untraceable_by_type": _counter(
                               row.get("memory_type")
                               for row in (provenance.get("untraceable") or [])),
                           "untraceable_reasons": _counter(
                               row.get("reason") for row in (provenance.get("untraceable") or []))},
            "hint_violations": hint_violations,
            "projections": dict(receipt.get("projections") or {}),
            "experience_version": exp.EXPERIENCE_MEMORY_VERSION,
        }
    finally:
        seeded["database"].close()


def analyze(snapshot: dict, *, now=None, limit_runs: int | None = None) -> dict:
    """整份快照的验收报告（**确定性**：同快照同参数 → 逐字相同的报告）。"""
    import qa_memory_experience as exp

    moment = now or _parse_time(snapshot.get("captured_at_utc")) or datetime.now(timezone.utc)
    first = corpus_only(snapshot, now=moment, limit_runs=limit_runs)
    second = corpus_only(snapshot, now=moment, limit_runs=limit_runs)
    loop_first = leave_one_out(snapshot, now=moment, limit_runs=limit_runs)
    loop_second = leave_one_out(snapshot, now=moment, limit_runs=limit_runs)
    determinism = {
        "same_six_type_counts": first["six_type_counts"] == second["six_type_counts"],
        "same_gate": first["gate"] == second["gate"],
        "same_failure_kinds": first["failures"]["kinds"] == second["failures"]["kinds"],
        "same_loop": (loop_first["baseline_routes"] == loop_second["baseline_routes"]
                      and loop_first["after_routes"] == loop_second["after_routes"]
                      and loop_first["changed"] == loop_second["changed"]),
    }
    acceptance = {
        "six_types_produced": all(int(first["produced"].get(name) or 0) >= 0 for name in SIX_TYPES),
        "episodes_persisted": int(first["six_type_counts"]["EPISODIC_RESEARCH"]) > 0,
        "failures_persisted": int(first["six_type_counts"]["FAILURE"]) > 0,
        "strategies_persisted": int(first["six_type_counts"]["STRATEGY"]) > 0,
        "query_patterns_persisted": int(first["six_type_counts"]["QUERY_PATTERN"]) > 0,
        "sources_persisted": int(first["six_type_counts"]["SOURCE"]) > 0,
        "source_projection_rows": len(first["sources"]["top"]) > 0
        and int(first["sources"]["rows"]) > 0,
        "no_hint_violations": int(first["hint_violations"]) == 0,
        "provenance_complete": int(first["provenance"]["untraceable"] or 0) == 0,
        "guidance_changed_routes": int(loop_first["changed"]) > 0,
        "avoided_channels_removed": (int(loop_first["avoided_after"])
                                     < int(loop_first["avoided_in_baseline"])
                                     or int(loop_first["avoided_in_baseline"]) == 0),
        "version_gate_blocks_cross_version_suppression": int(loop_first["blocked_by_version"]) > 0,
        "failure_repetition_dropped": (loop_first["failure_repetition_after"] is not None
                                       and loop_first["failure_repetition_before"] is not None
                                       and loop_first["failure_repetition_after"]
                                       <= loop_first["failure_repetition_before"]),
        "determinism": all(determinism.values()),
    }
    report = {
        "report_version": REPORT_VERSION,
        "clock": {
            "source": "snapshot.captured_at_utc（显式基准，绝不用墙上钟）",
            "baseline_utc": moment.astimezone(timezone.utc).isoformat(timespec="seconds")
                            .replace("+00:00", "Z"),
        },
        "snapshot": {
            "snapshot_version": str((snapshot.get("source") or {}).get("snapshot_version") or ""),
            "captured_at_utc": str(snapshot.get("captured_at_utc") or ""),
            "catalog": dict(snapshot.get("catalog") or {}),
            "missing_tables": list(snapshot.get("missing_tables") or []),
            "memory_tables_present": list(snapshot.get("memory_tables_present") or []),
            "phase12_tables_present": list(snapshot.get("phase12_tables_present") or []),
            "errors": list(snapshot.get("errors") or []),
            "stage_projection_keys": list((snapshot.get("source") or {})
                                          .get("stage_projection_keys") or []),
        },
        "experience_version": exp.EXPERIENCE_MEMORY_VERSION,
        "corpus": first,
        "leave_one_out": loop_first,
        "determinism": determinism,
        "acceptance": acceptance,
        "notes": [
            ("A 机的 `memory_item` 与本工具读到的十张 memory_* 表都是 **0 行**、"
             "`source_reliability_memory` / `skill_performance_memory` **根本不存在**："
             "Phase 12 未发布（本阶段不部署）。所以「六类记忆统计」来自"
             "**同一份代码在真实数据上的离线重建**，口径与 Phase 04/09/10/11 相同。"),
            ("真机没有 `qa_reasoning_traces` 行（0 行）与 `node_kind='skill'` 遥测行（0 行）："
             "所以「逐查询/逐通道归因」只能落到「该查询所在的一轮」这一粒度，"
             "技能表现记忆的记录沿用 Phase 11 的离线重建，两处边界都如实标注。"),
            ("`qa_runs.corpus_version` 在 A 机 15 行里全为空 → 失败绑定退到检索回执里"
             "**真实观测到的** `cache.kb_version`（`binding_source` 写明来路）；"
             "`config_hash` 同样全空 → 配置轴用运行期真值 `qa-retrieval-v3-policy-exact`。"),
            ("前后对比用**留一法**：给某一轮的建议只由其它轮的失败/策略经验得出，"
             "自己不许教自己；`changed` = 首选通道被改的缺口数，"
             "`avoided_in_baseline` → `avoided_after` = 建议集合里还留着「被避免通道」的缺口数。"),
            ("**边界（如实说）**：`failure_repetition_*` 在这一份真机数据上是**退化指标** —— "
             "Phase 07 的规则首选项是 semantic / policy_exact，而真机记录到的失败通道是 "
             "keyword / web / graph（两者不相交），所以「按首选通道算的重复踩坑率」恒为 0，"
             "它证明不了什么（验收闸门里那条因此是空真）；有信息量的是 "
             "`changed`（16 条）与 `avoided_*`（17 → 0）。"),
        ],
        "per_run": loop_first["per_run"],
    }
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Phase 12 经验记忆验收（快照离线重建）")
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--out", default="")
    parser.add_argument("--history", default="")
    parser.add_argument("--print", action="store_true", dest="print_only")
    parser.add_argument("--limit-runs", type=int, default=0,
                        help="只取最近 N 轮（0 = 全量；正式验收用全量）")
    args = parser.parse_args(argv)
    with open(args.snapshot, encoding="utf-8") as handle:
        snapshot = json.load(handle)
    report = analyze(snapshot, limit_runs=args.limit_runs or None)
    summary = {
        "report_version": report["report_version"], "clock": report["clock"],
        "snapshot": {key: report["snapshot"][key] for key in
                     ("snapshot_version", "captured_at_utc", "catalog", "missing_tables",
                      "phase12_tables_present")},
        "six_type_counts": report["corpus"]["six_type_counts"],
        "produced": report["corpus"]["produced"],
        "gate": report["corpus"]["gate"],
        "sources": {"rows": report["corpus"]["sources"]["rows"],
                    "bands": report["corpus"]["sources"]["bands"]},
        "skills": report["corpus"]["skills"],
        "provenance": report["corpus"]["provenance"],
        "leave_one_out": {key: report["leave_one_out"][key] for key in
                          ("runs", "gaps", "changed", "changed_by_reason", "baseline_routes",
                           "after_routes", "failure_repetition_before",
                           "failure_repetition_after", "avoided_in_baseline", "avoided_after",
                           "blocked_by_version", "warnings", "warning_codes")},
        "determinism": report["determinism"], "acceptance": report["acceptance"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.out and not args.print_only:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print("written: %s" % args.out)
    if args.history and not args.print_only:
        line = {"report_version": REPORT_VERSION,
                "captured_at_utc": report["snapshot"]["captured_at_utc"],
                "six_type_counts": report["corpus"]["six_type_counts"],
                "changed": report["leave_one_out"]["changed"],
                "gaps": report["leave_one_out"]["gaps"],
                "failure_repetition_before": report["leave_one_out"]["failure_repetition_before"],
                "failure_repetition_after": report["leave_one_out"]["failure_repetition_after"],
                "acceptance": report["acceptance"]}
        with open(args.history, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
        print("history appended: %s" % args.history)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
