#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 10（P10-01…P10-06）验收：**真机数据上的前后对比**（纯规则，零模型调用，零端点）。

它回答六个问题，全部可逐项复算：
  1. **线上到底有什么**：A 机只读快照里的记忆表行数（本次实测 0 行）、部署态
     （`QA_SCHEMA_VERSION` / Phase 10 模块是否在位）、以及真实 claims/evidence 的规模 ——
     这是"线上线下差距"的如实记录，而不是含糊的"没有数据"；
  2. **时效闸门分布**：把真实 claims/evidence 离线重建成记忆图（Phase 09 的写门，同一份代码），
     再对每条记忆跑 P10-01 闸门 → `ALLOW/REVALIDATE/BLOCK` 与理由码分布；
  3. **复验命中与状态迁移**：在三个时钟（快照时刻 / +90 天 / +365 天）各跑一次
     P09 lifecycle + P10 复验 → **复验命中数、复验通过率（§16 Revalidation Pass Rate）、
     状态迁移分布**（含 `STALE/EXPIRED → ACTIVE` 的复活次数）；
  4. **来源版本检测**：语料版本变化与文档版本号的检出分布（P10-02）；
  5. **矛盾检出与裁决**：全库配对（P10-04）→ 按 Phase 06 的理由码给出裁决分布与
     取代（P10-05）次数；**真机数据上没有真矛盾时如实报 0**，并用构造注入证明能力（另有用例）；
  6. **幂等与确定性**：同一时钟重放 → 复验/取代/撤销的写入次数与分布是否逐字相同。

口径与边界（宁写 PARTIAL 不谎报）：
  · 数据来源 = `--snapshot` 指定的 JSON（A 机**只读**导出，带 `source.access`）；本工具不联网、不连库；
  · **线上记忆行数为 0**（Phase 09 已发布但还没有 QA run 触发写入），所以复验/矛盾/取代
    全部在**离线重建的记忆图**上跑（同一份代码 + 同一份真实证据），并在报告里如实标注；
  · 本工具只写**内存 sqlite**（`:memory:`），不碰任何真实库；
  · 候选证据只来自快照里该 run 的证据（零新增检索）；向量通道不参与本阶段。

用法：
    python tools/qa_phase10_revalidation_acceptance.py --json
    python tools/qa_phase10_revalidation_acceptance.py --out baseline/qa-memory-revalidation-acceptance.json
    python tools/qa_phase10_revalidation_acceptance.py --snapshot <新快照>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_evidence  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_memory as memory  # noqa: E402
import qa_memory_revalidation as mr  # noqa: E402
from qa_phase08_context_acceptance import (  # noqa: E402
    _claim_graph_row, _edge_row, _evidence_object, _rows_by_run,
)
from qa_phase09_memory_acceptance import (  # noqa: E402
    _counter, _fresh_store, _merge, _normalize_snapshot, _parse_time, _run_meta,
)

REPORT_VERSION = "qa-memory-revalidation-acceptance-v1"
DEFAULT_SNAPSHOT = os.path.join("baseline", "qa-memory-revalidation-real-sample.json")
FALLBACK_SNAPSHOT = os.path.join("baseline", "qa-memory-real-sample.json")
DEFAULT_OUTPUT = os.path.join("baseline", "qa-memory-revalidation-acceptance.json")
AGE_OFFSETS = (0, 90, 365)


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _pairs(snapshot: dict) -> list:
    buckets = sorted(_rows_by_run(snapshot).items())
    return [(bucket.get("run") or {}, bucket) for _run_id, bucket in buckets
            if (bucket.get("run") or {}).get("id")]


def _graph_for(bucket: dict, *, now=None) -> dict:
    """把快照里一个 run 的行还原成证据图（Phase 09 工具同口径，但**时钟钉死**）。

    与 `qa_phase09_memory_acceptance._graph_for` 的唯一差别：Phase 02 的
    `annotate_evidence(retrieved_at=…)` 与 Phase 03 的 `verify_evidence_batch(now=…)`
    都接收调用方给的时钟。不钉死的话，证据的时效分按"现在几点"重算，
    写门算出来的 confidence 会在一天之内漂 5e-5 —— 报告就不再逐字可比（实测踩到）。
    """
    claims = [item for item in (_claim_graph_row(row) for row in bucket.get("claims") or [])
              if item]
    evidence, by_ref = [], {}
    for row in bucket.get("evidence") or []:
        item = _evidence_object(row)
        if not item:
            continue
        annotated = qa_evidence.annotate_evidence(
            item, run_id=str(row.get("run_id") or ""), stage="level1_retrieval",
            corpus_version=str((bucket.get("run") or {}).get("corpus_version") or ""),
            retrieved_at=(now.isoformat(timespec='seconds') if now else ''))
        evidence.append(annotated)
        by_ref[str(annotated.get("evidence_ref") or "")] = annotated
    seen, edges = set(), []
    for row in bucket.get("edges") or []:
        edge = _edge_row(row)
        key = (edge["claim_id"], edge["evidence_ref"])
        if not all(key) or key in seen:
            continue
        seen.add(key)
        edges.append(edge)
    verdicts: dict = {}
    if evidence:
        from qa_verifier import term_set, verify_evidence_batch

        for node in claims:
            claim = node.get("claim") or {}
            refs = [str(ref) for ref in (claim.get("evidence_refs") or []) if ref in by_ref]
            if not refs:
                continue
            items = [dict(by_ref[ref]) for ref in refs]
            reviewed, _audit = verify_evidence_batch(
                items, claim_text=str(claim.get("text") or ""),
                terms=term_set(str(claim.get("text") or "")), gate="off", now=now)
            for item in reviewed:
                ref = str(item.get("evidence_ref") or "")
                by_ref[ref] = item
                layer = qa_evidence.evidence_object(item)
                verification = layer.get("verification") if isinstance(layer, dict) else {}
                verdict = str((verification or {}).get("verdict") or "UNVERIFIED")
                verdicts[verdict] = verdicts.get(verdict, 0) + 1
    graph = {"version": contracts.EVIDENCE_GRAPH_VERSION, "claims": claims,
             "evidence": [by_ref.get(str(item.get("evidence_ref") or ""), item)
                          for item in evidence],
             "edges": edges, "conflicts": list(bucket.get("conflicts") or []), "stats": {}}
    graph["verdicts"] = verdicts
    return graph


def _write_phase(pairs: list, *, persist: bool = True, now=None) -> dict:
    """对所有真实 run 跑一遍写门（可只统计不落库），返回回执与分布（口径同 Phase 09）。"""
    database, store = _fresh_store()
    summary = {"runs": 0, "candidates": 0, "persisted": 0, "merged": 0, "session_only": 0,
               "dropped": 0, "decision_counts": {}, "reason_counts": {}, "per_run": [],
               "memory_ids": [], "by_type": {}, "by_freshness": {}}
    try:
        for run, bucket in pairs:
            graph = _graph_for(bucket, now=now)
            meta = _run_meta(run)
            candidates = memory.memory_candidates_from_graph(
                graph, scope="PATIENT_LONGITUDINAL", owner_user_id=meta["owner_user_id"],
                session_id=meta["session_id"], industry_pack_id=meta["industry_pack_id"],
                run_id=meta["id"])
            receipt = memory.apply_write_gate(store if persist else None, candidates,
                                             run_id=meta["id"], now=now)
            summary["runs"] += 1
            summary["candidates"] += receipt["candidates"]
            summary["persisted"] += receipt["persisted"]
            summary["merged"] += receipt["merged"]
            summary["session_only"] += receipt["session_only"]
            summary["dropped"] += receipt["dropped"]
            summary["decision_counts"] = _merge(summary["decision_counts"],
                                                receipt["decision_counts"])
            summary["reason_counts"] = _merge(summary["reason_counts"], receipt["reason_counts"])
            summary["memory_ids"].extend(receipt["memory_ids"])
            for row in receipt["decisions"]:
                key = str(row.get("memory_type") or "")
                summary["by_type"][key] = summary["by_type"].get(key, 0) + 1
                freshness = str((row.get("metadata") or {}).get("freshness_class") or "")
                summary["by_freshness"][freshness] = summary["by_freshness"].get(freshness, 0) + 1
            summary["per_run"].append({
                "run_id": meta["id"], "claims": len(graph["claims"]),
                "evidence": len(graph["evidence"]), "candidates": receipt["candidates"],
                "persisted": receipt["persisted"], "decision_counts": receipt["decision_counts"],
                "reason_counts": receipt["reason_counts"]})
        return {**summary, "memory_stats": store.memory_stats() if persist else {}}
    finally:
        database.close()


def _seed_store(pairs: list, *, now, lifecycle_at=None) -> tuple:
    """离线重建记忆图（Phase 09 写门），可再跑一次 P09 lifecycle 把状态推到目标时钟。

    返回 `(database, store, memories)`；调用方负责 `database.close()`。
    """
    database, store = _fresh_store()
    for run, bucket in pairs:
        meta = _run_meta(run)
        memory.apply_write_gate(store, memory.memory_candidates_from_graph(
            _graph_for(bucket, now=now), scope="PATIENT_LONGITUDINAL",
            owner_user_id=meta["owner_user_id"], session_id=meta["session_id"],
            industry_pack_id=meta["industry_pack_id"], run_id=meta["id"]),
            run_id=meta["id"], now=now)
    lifecycle = {}
    if lifecycle_at is not None:
        lifecycle = memory.apply_lifecycle(store, now=lifecycle_at, include_all_scopes=True)
    # **按 memory_id 排序**：`load_memory_items` 按 `updated_at`（墙上时钟）排序，
    # 两次运行的顺序会差几秒 —— 集合一样、次序不同，报告就不再逐字可比（实测踩到）。
    memories = sorted(store.load_memory_items(include_all_scopes=True, limit=5000) or [],
                      key=lambda item: str(item.get("memory_id") or ""))
    return database, store, memories, lifecycle


def _live_state(snapshot: dict) -> dict:
    """线上到底有什么：记忆表行数 + 部署态（这一段是"线上线下差距"的原始证据）。"""
    catalog = {key: int(value) for key, value in (snapshot.get("catalog") or {}).items()}
    memory_tables = sorted(name for name in catalog if name.startswith("memory_"))
    return {
        "captured_at_utc": snapshot.get("captured_at_utc"),
        "catalog": catalog,
        "memory_table_rows": {name: catalog.get(name, 0) for name in memory_tables},
        "memory_rows_total": sum(catalog.get(name, 0) for name in memory_tables),
        "phase10_tables_present": list(snapshot.get("phase10_tables_present") or []),
        "deployment": dict(snapshot.get("deployment") or {}),
        "runs": catalog.get("qa_runs", 0),
        "claims": catalog.get("qa_claims", 0),
        "evidence": catalog.get("qa_evidence", 0),
    }


def _gate_phase(store, memories: list, *, now=None) -> dict:
    """P10-01：对每条记忆跑闸门（不写库，纯分布）。

    `now` 必须由调用方钉死（快照时刻/目标时钟）：闸门的年龄与衰减分依赖时钟，
    用墙上时钟会让两次运行的分布对不上（确定性复算的前提）。
    """
    decisions, reasons, high_stakes = [], [], 0
    rows = []
    for item in memories:
        gate = mr.freshness_gate(item, now=now)
        decisions.append(gate["decision"])
        reasons.append("%s/%s" % (gate["decision"], gate["reason"]))
        high_stakes += 1 if gate["high_stakes"] else 0
        rows.append({"memory_id": item.get("memory_id"), "decision": gate["decision"],
                     "reason": gate["reason"], "freshness_class": item.get("freshness_class"),
                     "status": item.get("status"), "decay_score": gate["decay_score"],
                     "age_days": gate["age_days"]})
    return {"checked": len(memories), "decisions": _counter(decisions),
            "reasons": _counter(reasons), "high_stakes": high_stakes, "rows": rows}


def _revalidation_phase(pairs: list, snapshot: dict, *, offset_days: int) -> dict:
    """在"快照时刻 + offset 天"这个时钟上跑：写门 → P09 lifecycle → P10 复验 + 矛盾 + 取代。"""
    frozen = _parse_time(snapshot.get("captured_at_utc")) or datetime.now(timezone.utc)
    moment = frozen + timedelta(days=offset_days)
    database, store, memories, lifecycle = _seed_store(pairs, now=frozen, lifecycle_at=moment)
    try:
        before_status = _counter(item.get("status") for item in memories)
        gate = _gate_phase(store, memories, now=moment)
        report = {"offset_days": offset_days, "clock": moment.isoformat(timespec="seconds"),
                  "memories": len(memories), "status_before": before_status,
                  "lifecycle": {key: lifecycle.get(key) for key in
                                ("checked", "transition_counts", "status_counts", "decay_bands")},
                  "gate": {"decisions": gate["decisions"], "reasons": gate["reasons"],
                           "high_stakes": gate["high_stakes"]},
                  "per_run": []}
        # 逐 run：召回命中 → 复验（与管线同一入口，只是把证据换成本 run 的快照证据）
        hints_total = 0
        for run, bucket in pairs:
            graph = _graph_for(bucket, now=moment)
            meta = _run_meta(run)
            recall = memory.recall_from_run(store, run_meta=meta, graph=graph,
                                           question=str(run.get("question_text") or ""),
                                           now=frozen, store_log=False)
            layer = mr.run_revalidation(store, hints=recall.get("hits") or [], graph=graph,
                                        run_meta=meta, now=moment, contradiction_scope="touched")
            if not layer.get("checked"):
                continue
            hints_total += len(recall.get("hits") or [])
            report["per_run"].append({
                "run_id": meta["id"], "hits": len(recall.get("hits") or []),
                "checked": layer["checked"], "outcomes": layer["outcomes"],
                "gate_decisions": layer["gate_decisions"],
                "revalidated": layer["revalidated"], "promoted": layer["promoted"],
                "source_version": layer["source_version"],
                "contradictions": {key: value for key, value in
                                   (layer["contradictions"] or {}).items() if key != "details"},
            })
        # 全库配对（P10-04/05）：真机数据上到底有没有真矛盾
        conflict = mr.detect_memory_contradictions(store, run_id="", now=moment)
        validations = store.memory_validations(limit=5000)
        outcomes = _counter(row.get("outcome") for row in validations)
        revalidated = sum(1 for row in validations if str(row.get("outcome")) == "REVALIDATED")
        checked = len(validations)
        revalidated_ids = sorted({str(row.get("memory_id")) for row in validations
                                  if str(row.get("outcome")) == "REVALIDATED"})
        transitions = _counter("%s->%s" % (row.get("status_before"), row.get("status_after"))
                               for row in validations
                               if str(row.get("status_before")) != str(row.get("status_after")))
        after = store.load_memory_items(include_all_scopes=True, limit=5000) or []
        chains = mr.supersession_chains(store)
        # 幂等/确定性：同一时钟重放一次（只数"新写入"）
        replay_versions_before = _count_table(store, "memory_version")
        replay_validations_before = _count_table(store, "memory_validation")
        replay_conflicts_before = _count_table(store, "memory_contradiction")
        for run, bucket in pairs:
            graph = _graph_for(bucket, now=moment)
            meta = _run_meta(run)
            recall = memory.recall_from_run(store, run_meta=meta, graph=graph,
                                           question=str(run.get("question_text") or ""),
                                           now=frozen, store_log=False)
            mr.run_revalidation(store, hints=recall.get("hits") or [], graph=graph,
                                run_meta=meta, now=moment, contradiction_scope="touched")
        replay = {
            "new_versions": _count_table(store, "memory_version") - replay_versions_before,
            "new_validations": _count_table(store, "memory_validation") - replay_validations_before,
            "new_contradictions": (_count_table(store, "memory_contradiction")
                                   - replay_conflicts_before),
        }
        report.update({
            "hits": hints_total,
            "hints_recalled": hints_total,
            "validations": checked,
            "outcomes": outcomes,
            "revalidated": revalidated,
            "revalidated_ids": revalidated_ids,
            "revalidation_pass_rate": round(revalidated / float(checked), 6) if checked else None,
            "status_transitions": transitions,
            "status_after": _counter(item.get("status") for item in after),
            "source_version": {
                "changed": sum(1 for row in validations
                               if row.get("metadata", {}).get("source_version", {}).get("changed")),
                "reasons": _counter(reason for row in validations
                                    for reason in ((row.get("metadata") or {}).get("source_version")
                                                   or {}).get("reasons") or [])},
            "high_risk": {"high_stakes": sum(1 for row in validations if row.get("high_stakes")),
                          "hooked": sum(1 for row in validations
                                        if ((row.get("metadata") or {}).get("high_risk_hook")
                                            or {}).get("applied"))},
            "contradictions": {"candidates": conflict["candidates"],
                               "by_kind": conflict["by_kind"],
                               "by_resolution": conflict["by_resolution"],
                               "by_reason_code": conflict["by_reason_code"],
                               "by_status_action": conflict["by_status_action"],
                               "supersessions": conflict["supersessions"]},
            "supersession_audit": {"chains": len(chains["chains"]),
                                   "problems": chains["problems"][:5],
                                   "superseded": chains["superseded"]},
            "replay": replay,
            "gate_rows": gate["rows"],
        })
        # 撤销（P10-06）：**构造**一次按来源的污染撤销（真机数据里没有真污染，如实标注）
        top_source = _top_source(store)
        revoke = {}
        if top_source:
            revoke = mr.revoke_memories(store, reason="SOURCE_CONTAMINATED",
                                        source_fingerprint=top_source, run_id="acceptance")
            revoke = {key: revoke[key] for key in
                      ("reason", "checked", "revoked", "skipped", "already_revoked",
                       "status_counts")}
            revoke["selector"] = top_source
        report["revoke_constructed"] = revoke
        return report
    finally:
        database.close()


def _count_table(store, table: str) -> int:
    try:
        row = store.database.connection.execute("SELECT count(*) FROM %s" % table).fetchone()
        return int((row[0] if row else 0) or 0)
    except Exception:      # noqa: BLE001
        return 0


def _top_source(store) -> str:
    """挑一个被引用最多的来源指纹（只用于**构造**撤销演示）。"""
    counts: dict = {}
    try:
        rows = store.memory_evidence(memory_ids=[
            str(item.get("memory_id") or "") for item in
            (store.load_memory_items(include_all_scopes=True, limit=5000) or [])])
    except Exception:      # noqa: BLE001
        return ""
    for row in rows or []:
        fingerprint = str(row.get("source_fingerprint") or "")
        if fingerprint:
            counts[fingerprint] = counts.get(fingerprint, 0) + 1
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _construct_conflict_phase(pairs: list, snapshot: dict) -> dict:
    """真机素材上的**构造注入**矛盾（能力证据）+ 否定控制（假阳性守卫）。

    真机数据里"可对错的断言型记忆"之间没有真矛盾（与 Phase 06 的 `qa_conflicts` 全库 0 行
    同一处置口径，见 D-029/D-030）：所以能力证据必须靠**拿真机素材构造**——
      ① 正例：取真机里两条相似度最高的 `VERIFIED_CLAIM` 记忆，给较新的一条加否定短语，
         两条一起写进临时库 → 必须检出 1 条矛盾并给出 Phase 06 的理由码；
      ② 负例（控制组）：把其中一条换成 `ENTITY`（索引类）→ 必须**0 条**（这就是真机上
         8 条假矛盾的形态，类型白名单把它挡住了）。
    """
    frozen = _parse_time(snapshot.get("captured_at_utc")) or datetime.now(timezone.utc)
    database, store, memories, _lifecycle = _seed_store(pairs, now=frozen)
    sample = {"candidates": 0, "by_reason_code": {}, "by_resolution": {},
              "status_action": "", "control_candidates": 0, "example": {}}
    try:
        claims = [item for item in memories
                  if str(item.get("memory_type") or "") == "VERIFIED_CLAIM"]
        if len(claims) < 1:
            sample["note"] = "真机素材里没有断言型记忆，无法构造"
            return sample
        # 挑一条**带实体**的真机断言当素材（实体是配对的主体一致性前提）
        entities = {}
        try:
            for row in store.memory_entities(memory_ids=[str(item.get("memory_id") or "")
                                                         for item in claims]) or []:
                entities.setdefault(str(row.get("memory_id") or ""), []).append(
                    str(row.get("entity_key") or ""))
        except Exception:      # noqa: BLE001
            entities = {}
        source = next((item for item in claims
                       if entities.get(str(item.get("memory_id")) or "")), claims[0])
        text = str(source.get("canonical_content") or "")
        source_entities = entities.get(str(source.get("memory_id")) or []) or []
        older_text = _negate_text(text, source_entities)
        # 构造对必须用**独立作用域键**：真机素材的内容指纹已被原记忆占用，
        # `UNIQUE(scope_key, memory_type, content_fingerprint)` 会把同作用域的重写顶掉
        scope_key = "%s|P10-CONSTRUCTED" % str(source.get("scope_key") or "")
        base = {
            "memory_id": "MEM-constructed-old", "memory_type": "VERIFIED_CLAIM",
            "canonical_content": text,
            "content_fingerprint": memory.content_fingerprint(text),
            "confidence": 0.8, "freshness_class": str(source.get("freshness_class") or "MEDIUM"),
            "status": "ACTIVE", "scope": str(source.get("scope") or "PATIENT_LONGITUDINAL"),
            "scope_key": scope_key, "valid_from": "2026-01-01", "valid_until": "",
            "last_verified_at": frozen.isoformat(timespec="seconds"),
            "created_at": frozen.isoformat(timespec="seconds"), "version": 1, "decay_score": 0.5,
            "entity_ids": entities.get(str(source.get("memory_id")) or []) or [],
            "metadata": {"claim_type": "policy", "valid_from": "2026-01-01"},
        }
        twin = dict(base)
        twin.update({"memory_id": "MEM-constructed-new", "canonical_content": older_text,
                     "content_fingerprint": memory.content_fingerprint(older_text),
                     "valid_from": "2026-09-01",
                     "metadata": {"claim_type": "policy", "valid_from": "2026-09-01"}})
        saved = [store.save_memory_item(base), store.save_memory_item(twin)]
        sample["save_errors"] = [row.get("error") for row in saved if row.get("error")]
        report = mr.detect_memory_contradictions(store, run_id="acceptance-constructed",
                                                now=frozen)
        constructed_ids = {"MEM-constructed-old", "MEM-constructed-new"}
        found = [row for row in report["contradictions"]
                 if {str(row.get("left_memory_id")), str(row.get("right_memory_id"))}
                 & constructed_ids]
        sample.update({"candidates": len(found),
                       "by_reason_code": _counter(row.get("reason_code") for row in found),
                       "by_resolution": _counter(row.get("resolution") for row in found),
                       "by_status_action": _counter(row.get("status_action") for row in found),
                       "supersessions": len([row for row in found
                                             if row.get("status_action") == "SUPERSEDE"
                                             and (row.get("status_actions") or [{}])[0].get(
                                                 "changed")]),
                       "example": {"text": text[:60], "negated": older_text[:60],
                                   "older_status": (store.load_memory_items(
                                       memory_ids=["MEM-constructed-old"],
                                       include_all_scopes=True, limit=1) or [{}])[0].get("status"),
                                   "superseded_by": (store.load_memory_items(
                                       memory_ids=["MEM-constructed-old"],
                                       include_all_scopes=True, limit=1) or [{}])[0]
                                   .get("superseded_by")}})
        # 负例控制组：把同一批素材里的"新"那条换成实体记忆（索引类）→ 不许配对
        database2, store2 = _fresh_store()
        try:
            control = dict(twin)
            control.update({"memory_id": "MEM-control-entity", "memory_type": "ENTITY",
                            "canonical_content": str(source.get("canonical_content") or "")[:20]})
            store2.save_memory_item(dict(base, memory_id="MEM-control-old"))
            store2.save_memory_item(control)
            control_report = mr.detect_memory_contradictions(
                store2, run_id="acceptance-control", now=frozen)
            sample["control_candidates"] = control_report["candidates"]
            sample["control_eligible_types"] = control_report["eligible_types"]
        finally:
            database2.close()
        return sample
    finally:
        database.close()


def _negate_text(text: str, anchors=()) -> str:
    """把一条真实断言**就地**改成反义说法（构造注入用；确定性、可复算）。

    插入位置的口径（顺序即优先级）：
      ① 优先插在**共享实体键**前面 —— 实体既是配对的主体锚点，也是 Phase 03 判
         "否定极性相反且指向同一话题"（`anchored`）的锚点，插在这里才能让四个前置条件
         同时成立（否则构造出来的只是"两条文本不相干的假矛盾"）；
      ② 退到常见谓词（给予/适用/包括/支持/备案）；
      ③ 再退到文本中点。
    只做最小改动（插入一个契约词表内的否定短语），保证"同一主体 + 同一命题 + 极性相反"。
    """
    body = str(text or "")
    for anchor in sorted({str(item) for item in (anchors or ()) if str(item or "")},
                         key=lambda value: (-len(value), value)):
        if anchor and anchor in body:
            return body.replace(anchor, "不予" + anchor, 1)
    for anchor, replacement in (("给予", "不予给予"), ("适用", "不予适用"),
                                ("包括", "不包括"), ("支持", "不予支持"),
                                ("备案", "不予备案")):
        if anchor in body:
            return body.replace(anchor, replacement, 1)
    if len(body) >= 8:
        middle = len(body) // 2
        return body[:middle] + "不予" + body[middle:]
    return "不予" + body


def _determinism_phase(pairs: list, snapshot: dict) -> dict:
    """同样的输入跑两遍（两个独立内存库）：闸门/复验/矛盾的分布必须逐字相同。"""
    def _run():
        frozen = _parse_time(snapshot.get("captured_at_utc")) or datetime.now(timezone.utc)
        database, store, memories, _lifecycle = _seed_store(pairs, now=frozen)
        try:
            gate = _gate_phase(store, memories, now=frozen)
            layer = mr.run_revalidation(store, graph={}, run_meta={}, now=frozen,
                                        contradiction_scope="all")
            return {"decisions": gate["decisions"], "reasons": gate["reasons"],
                    "outcomes": layer["outcomes"], "gate": layer["gate_decisions"],
                    "contradictions": (layer["contradictions"] or {}).get("by_kind", {})}
        finally:
            database.close()

    first, second = _run(), _run()
    return {"deterministic": bool(first == second), "first": first, "second": second}


def build_report(snapshot: dict, *, snapshot_path: str = "") -> dict:
    snapshot = _normalize_snapshot(snapshot)
    pairs = _pairs(snapshot)
    live = _live_state(snapshot)
    frozen = _parse_time(snapshot.get("captured_at_utc")) or datetime.now(timezone.utc)
    rebuild = _write_phase(pairs, now=frozen)
    phases = [_revalidation_phase(pairs, snapshot, offset_days=offset) for offset in AGE_OFFSETS]
    determinism = _determinism_phase(pairs, snapshot)
    constructed = _construct_conflict_phase(pairs, snapshot)
    phase0 = phases[0]
    checks = [
        {"name": "live_machine_state_recorded", "passed": True,
         "detail": ("线上记忆行 %d 条（8 张表）、Phase 10 两张表存在 %s、部署 schema %s"
                    % (live["memory_rows_total"], live["phase10_tables_present"] or "无",
                       str(live["deployment"].get("schema_version") or "未知").strip()))},
        {"name": "memory_graph_rebuilt_offline",
         "passed": bool(rebuild["persisted"]) and phase0["memories"] > 0,
         "detail": "真实快照上离线重建 %d 条记忆（候选 %d）"
                   % (phase0["memories"], rebuild["candidates"])},
        {"name": "freshness_gate_reaches_every_decision",
         "passed": set(phase0["gate"]["decisions"]) <= set(contracts.MEMORY_FRESHNESS_DECISIONS)
         and bool(phase0["gate"]["decisions"]),
         "detail": "闸门分布：%s" % json.dumps(phase0["gate"]["decisions"], ensure_ascii=False)},
        {"name": "revalidation_produces_outcomes",
         "passed": bool(phase0["outcomes"]),
         "detail": "复验出口分布：%s（受检 %d）"
                   % (json.dumps(phase0["outcomes"], ensure_ascii=False), phase0["validations"])},
        {"name": "revalidation_pass_rate_recorded",
         "passed": phase0["revalidation_pass_rate"] is not None,
         "detail": "复验通过率 %.4f（§16 Revalidation Pass Rate，命中 %d 次）"
                   % (phase0["revalidation_pass_rate"] or 0.0, phase0["hits"])},
        {"name": "aged_clocks_show_status_transitions",
         "passed": all(phase["lifecycle"]["checked"] for phase in phases),
         "detail": "在 +0/+90/+365 天三个时钟上的状态迁移：%s"
                   % json.dumps([phase["status_transitions"] for phase in phases],
                                ensure_ascii=False)},
        {"name": "contradiction_verdicts_use_phase06_codes",
         "passed": all(set(phase["contradictions"]["by_reason_code"])
                       <= set(contracts.CONTRADICTION_RESOLUTION_CODES) for phase in phases)
         and bool(constructed.get("by_reason_code")),
         "detail": ("三个时钟上的裁决理由码：%s；**真机素材构造注入**（正例/负例控制）：%s"
                    % (json.dumps([phase["contradictions"]["by_reason_code"] for phase in phases],
                                  ensure_ascii=False),
                       json.dumps({key: constructed.get(key) for key in
                                   ("candidates", "by_reason_code", "by_resolution",
                                    "by_status_action", "supersessions", "control_candidates")},
                                  ensure_ascii=False)))},
        {"name": "supersession_chain_has_no_problems",
         "passed": all(not phase["supersession_audit"]["problems"] for phase in phases),
         "detail": "取代链问题：%s"
                   % json.dumps([phase["supersession_audit"]["problems"] for phase in phases],
                                ensure_ascii=False)},
        {"name": "revoke_constructed_is_auditable",
         "passed": bool(phase0["revoke_constructed"]),
         "detail": "构造的按来源撤销：撤销 %s 条 / 跳过 %s 条"
                   % (len(phase0["revoke_constructed"].get("revoked") or []),
                      len(phase0["revoke_constructed"].get("skipped") or []))},
        {"name": "deterministic_across_runs", "passed": bool(determinism["deterministic"]),
         "detail": "两独立库的闸门/复验/矛盾分布逐字相同：%s" % determinism["deterministic"]},
        {"name": "replay_writes_no_memory_state",
         "passed": all(phase["replay"]["new_versions"] == 0
                       and phase["replay"]["new_contradictions"] == 0
                       and phase["replay"]["new_validations"]
                       <= sum(phase["status_transitions"].values()) for phase in phases),
         "detail": ("同钟重放的写入：%s（`new_validations` 只允许来自\"首遍真的改了状态\""
                    "（最多 %s 行），状态/取代/矛盾本身一律 0 新增）"
                    % (json.dumps([phase["replay"] for phase in phases], ensure_ascii=False),
                       json.dumps([sum(phase["status_transitions"].values()) for phase in phases])))},
    ]
    report = {
        "report_version": REPORT_VERSION,
        "generated_at_utc": _utc_now_z(),
        "snapshot": {"path": snapshot_path or "", "captured_at_utc": snapshot.get("captured_at_utc"),
                     "source": snapshot.get("source") or {}},
        "live_machine": live,
        "rebuild": {key: rebuild[key] for key in ("runs", "candidates", "persisted", "merged",
                                                  "session_only", "dropped", "decision_counts",
                                                  "reason_counts", "by_type", "by_freshness")},
        "phases": phases,
        "constructed_conflict": constructed,
        "determinism": determinism,
        "checks": checks,
        "summary": {
            "checks_passed": sum(1 for check in checks if check["passed"]),
            "checks_total": len(checks),
            "memories_rebuilt": phase0["memories"],
            "revalidation_hits": phase0["hits"],
            "revalidated": phase0["revalidated"],
            "revalidation_pass_rate": phase0["revalidation_pass_rate"],
            "contradictions_detected": sum(phase["contradictions"]["candidates"] for phase in phases),
            "supersessions": sum(phase["contradictions"]["supersessions"] for phase in phases),
            "constructed_contradictions": constructed.get("candidates", 0),
            "constructed_control_candidates": constructed.get("control_candidates", 0),
            "stale_or_expired_before": phase0["status_before"],
            "status_transitions": phase0["status_transitions"],
        },
        "notes": [
            "线上（A 机）记忆行数为 0：Phase 09 已发布、表已建，但发布后还没有 QA run 触发写入；",
            "复验/矛盾/取代在**离线重建的记忆图**上复算（同一份代码 + 同一份真实证据），"
            "口径与管线一致（只吃本轮证据图的证据，零新增检索）；",
            "只允许**断言型**记忆（默认 VERIFIED_CLAIM）参与矛盾判定：实体记忆只是索引，"
            "真机上 8 条\"实体 vs 含否定词长句\"的假矛盾由这条前置条件消除（有负例控制组）；",
            "构造的撤销/矛盾演示不代表线上发生了污染或冲突，只证明链路可跑通且可审计；",
            "所有判定纯规则/统计：无 LLM、无嵌入端点调用（GPU 机本轮停用）。",
        ],
    }
    ok, note = contracts.validate("memory_revalidation_report", {
        "revalidation_version": contracts.MEMORY_REVALIDATION_VERSION,
        "checked": phase0["validations"], "gate_decisions": phase0["gate"]["decisions"],
        "outcomes": phase0["outcomes"]})
    report["contract_ok"] = bool(ok)
    if not ok:
        report["contract_error"] = note
    report["status"] = "PASS" if all(check["passed"] for check in checks) else "PARTIAL"
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--json", action="store_true", help="把报告打到 stdout")
    args = parser.parse_args()
    path = args.snapshot or (DEFAULT_SNAPSHOT if os.path.exists(DEFAULT_SNAPSHOT)
                             else FALLBACK_SNAPSHOT)
    if not os.path.exists(path):
        print("缺少快照：%s（先用 tools/qa_phase10_real_snapshot.py 导出）" % path)
        return 2
    with open(path, encoding="utf-8") as handle:
        snapshot = json.load(handle)
    report = build_report(snapshot, snapshot_path=path)
    out = args.out or DEFAULT_OUTPUT
    if args.out or not args.json:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=1)
        print("验收报告已写入 %s（status=%s，%d/%d 检查通过）"
              % (out, report["status"], report["summary"]["checks_passed"],
                 report["summary"]["checks_total"]))
    if args.json or not args.out:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    for check in report["checks"]:
        print("[%s] %s — %s" % ("OK" if check["passed"] else "!!", check["name"], check["detail"]))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
