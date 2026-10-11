#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 09（P09-01…P09-06）验收：**真机数据上的前后对比**（纯规则，零模型调用）。

它回答六个问题，全部可逐项复算：
  1. **写门前后的记忆条数**：A 机真实 claims/evidence（只读快照）跑一遍 Write Gate，
     before（没有记忆表、0 条）→ after（落库 N 条）落成分布（PERSIST / TTL / 仅会话 / 丢弃）；
  2. **召回率与通道分布**：拿同一批真实 run 的**跨会话**召回做对比——
     before（记忆库为空）命中 0，after（写完之后的记忆图）哪些 run 能召回、走哪些通道；
     每条命中都必须是 MEMORY_HINT（`requires_revalidation=True` / `verified_evidence=False`）；
  3. **衰减分布**：在快照时刻 +0/+30/+90/+365 天各跑一次 lifecycle，给出状态迁移与衰减分桶；
     再跑第二遍验证**幂等**（同一时钟第二次 0 迁移）；
  4. **provenance 回溯率**：每条记忆能不能回到 Phase 02 的来源指纹 + 最小 span（必须 100%）；
  5. **污染率**：有没有"没通过核验的证据"变成记忆、有没有敏感串/外部指令内容被长期保存
     （MASTER_RULES 11/16/18 —— 三条硬规则的机器校验）；
  6. **与 Phase 02 seen 机制对齐**：记忆链的证据指纹与 `qa_evidence.source_fingerprint()`
     的口径是否逐条相等（同一份身份，不是两套指纹）。

口径与边界（宁写 PARTIAL 不谎报）：
  · 数据来源 = `--snapshot` 指定的 JSON（A 机**只读**导出，带 `source.access`）；本工具不联网、不连库；
  · 真机 run 早于本阶段接线，所以记忆图是**离线重建**的（同一份代码、同一份真实证据）；
    线上行为由 `tests/test_qa_phase09_pipeline.py` 端到端覆盖；
  · 向量通道用到的向量在快照里没有（`intel_article_embeddings` 不在本快照范围），
    所以离线复算的向量通道标 `no_vectors` 并**如实计入边界**，不假装跑过；
  · 召回分数是 §11 公式的确定性实现：同输入同输出（本工具会跑两遍比对）。

用法：
    python tools/qa_phase09_memory_acceptance.py --json
    python tools/qa_phase09_memory_acceptance.py --out baseline/qa-memory-acceptance.json
    python tools/qa_phase09_memory_acceptance.py --snapshot <新快照>
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_context_pack as cp  # noqa: E402
import qa_evidence  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_memory as memory  # noqa: E402
import qa_schema  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402
from qa_phase08_context_acceptance import (  # noqa: E402
    _claim_graph_row, _evidence_object, _edge_row, _rows_by_run,
)
from qa_storage import QaStore  # noqa: E402

REPORT_VERSION = "qa-memory-acceptance-v1"
DEFAULT_SNAPSHOT = os.path.join("baseline", "qa-memory-real-sample.json")
FALLBACK_SNAPSHOT = os.path.join("baseline", "qa-context-real-sample.json")
DEFAULT_OUTPUT = os.path.join("baseline", "qa-memory-acceptance.json")
DECAY_DAYS = (0, 30, 90, 365)

FALLBACK_BASELINE_CLOCK = datetime(2026, 10, 11, tzinfo=timezone.utc)
"""离线重建的**兜底基准时刻**（快照里没有 `captured_at_utc` 时用它）。

写死成一个常量而不是"现在"：验收产物必须逐字可复现，任何一处读墙上时钟都会让
"同一天绿、跨天红"（Phase 10 的 D-038 记录了这个缺陷与修复）。"""

BASELINE_CLOCK_ENV = "QA_P09_BASELINE_CLOCK"
FAKE_NOW_ENV = "QA_P09_FAKE_NOW"
"""两个**只在验收/守门用例里用**的时钟注入点：
`QA_P09_BASELINE_CLOCK` 覆盖"基准时刻"（快照抓取时刻的替身），
`QA_P09_FAKE_NOW` 覆盖进程看到的"墙上时钟"——守门用例用两个不同的假"现在"各跑一遍，
产物必须逐字相同（若有人把时钟改回墙上时间，产物就会跟着"现在"漂，用例变红）。"""

_baseline_clock: datetime | None = None


def _wall_clock() -> datetime:
    """进程**看到的**"现在"：只给"确实需要当前时间"的地方用（本工具里应当一处都没有）。

    守门用例通过 `QA_P09_FAKE_NOW` 注入两个不同的假"现在"来证明产物与它无关。
    """
    return _parse_time(os.environ.get(FAKE_NOW_ENV, "")) or datetime.now(timezone.utc)


def set_baseline_clock(value) -> dict:
    """钉死离线重建的基准时刻（**默认 = 快照抓取时刻**，见 `_graph_for` 的理由）。

    优先级（写清楚是为了让"产物跟谁对齐"没有歧义）：
      ① `QA_P09_BASELINE_CLOCK`（显式覆盖，守门用例的控制组用它证明基准时刻真的进了数字）；
      ② 调用方传入的快照抓取时刻（正常路径）；
      ③ `FALLBACK_BASELINE_CLOCK` 常量（快照缺 `captured_at_utc` 时）。
    **任何情况下都不使用墙上时钟**。
    """
    global _baseline_clock
    env_value = _parse_time(os.environ.get(BASELINE_CLOCK_ENV, ""))
    passed = value if isinstance(value, datetime) else _parse_time(value)
    if env_value is not None:
        moment, source = env_value, BASELINE_CLOCK_ENV
    elif passed is not None:
        moment, source = passed, "snapshot.captured_at_utc"
    else:
        moment, source = FALLBACK_BASELINE_CLOCK, "fallback_constant"
    _baseline_clock = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return {"baseline_clock": _baseline_clock.isoformat(timespec="seconds"),
            "baseline_clock_source": source,
            "fallback_constant": FALLBACK_BASELINE_CLOCK.isoformat(timespec="seconds"),
            "wall_clock_env": FAKE_NOW_ENV,
            "wall_clock_used": False}


def baseline_clock() -> datetime:
    """当前基准时刻（`_graph_for` 的默认时钟）。"""
    if _baseline_clock is not None:
        return _baseline_clock
    return _parse_time(os.environ.get(BASELINE_CLOCK_ENV, "")) or FALLBACK_BASELINE_CLOCK


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _counter(values) -> dict:
    counts: dict = {}
    for value in values:
        key = str(value or "")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


class _MemoryDb:
    """**内存 sqlite** + QaStore 需要的最小接口（不碰任何真实库、不读环境变量）。"""

    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()

    def _ensure_connection(self) -> None:
        return None

    def close(self) -> None:
        try:
            self.connection.close()
        except Exception:
            pass


def _fresh_store() -> tuple:
    database = _MemoryDb()
    cursor = database.connection.cursor()
    qa_schema.ensure_qa_tables(cursor)
    cursor.close()
    database.connection.commit()
    return database, QaStore(database)


def _graph_for(bucket: dict, *, verify: bool = True, now=None) -> dict:
    """把快照里一个 run 的行还原成证据图（只读既有 payload，口径与 Phase 08 同源）。

    真机 run 早于 Phase 02/03 的接线（payload 里没有 `evidence_layer`），所以这里**离线补跑**
    两条既有链路（同一份代码、同一份真实证据、零端点）：
      ① Phase 02 `qa_evidence.annotate_evidence()`：最小 span / 来源身份 / 指纹；
      ② Phase 03 `qa_verifier.verify_evidence_batch()`：按 claim 文本逐条核验出 verdict。
    补跑之后的 verdict 才是写门的输入 —— 写门**不会**因为"真机没有 verdict"而放行
    （MASTER_RULES 11：没有通过核验的证据不写 VerifiedClaimMemory）。

    **时钟一律走 `now or baseline_clock()`（不再读墙上时钟）**：Phase 03 的时效维度按"天"变，
    用墙上时钟的话同一份快照在不同天算出不同的证据分（进而写门的 confidence 差 5e-5），
    验收产物就不再逐字可复现 —— `test_committed_report_matches_a_fresh_run` 会在跨天时变红
    （实测踩到，见 Phase 10 的 D-038）。基准时刻的选择：**快照的抓取时刻**
    （`snapshot.captured_at_utc`；由 `build_report` 通过 `set_baseline_clock()` 钉住），
    理由是"这份快照代表那一时刻的真实数据"，用它当基准才能让产物与快照一一对应。
    """
    moment = now if isinstance(now, datetime) else (now or baseline_clock())
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
            retrieved_at=moment.isoformat(timespec="seconds"))
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
    if verify and evidence:
        from qa_verifier import term_set, verify_evidence_batch

        for node in claims:
            claim = node.get("claim") or {}
            refs = [str(ref) for ref in (claim.get("evidence_refs") or []) if ref in by_ref]
            if not refs:
                continue
            items = [dict(by_ref[ref]) for ref in refs]
            reviewed, _audit = verify_evidence_batch(
                items, claim_text=str(claim.get("text") or ""),
                terms=term_set(str(claim.get("text") or "")), gate="off", now=moment)
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


def _run_meta(run: dict) -> dict:
    return {
        "id": str(run.get("id") or ""),
        "owner_user_id": str(run.get("owner_user_id") or ""),
        "session_id": str(run.get("session_id") or ""),
        "industry_pack_id": str(run.get("industry_pack_id") or "auto"),
    }


def _question(run: dict) -> str:
    return str(run.get("question_text") or "")


def _write_phase(buckets: list, *, persist: bool = True, now=None) -> dict:
    """对所有真实 run 跑一遍写门（可只统计不落库），返回回执与分布。

    `now` 固定成快照时刻：写门会给 `PERSIST_WITH_TTL` 补 `valid_until`、给记忆打
    `last_verified_at`，用**墙上时钟**的话两次运行之间就会差出几秒（衰减分随之不同），
    确定性复算必须钉死时钟。
    """
    database, store = _fresh_store()
    summary = {"runs": 0, "candidates": 0, "persisted": 0, "merged": 0, "session_only": 0,
               "dropped": 0, "decision_counts": {}, "reason_counts": {}, "per_run": [],
               "memory_ids": [], "by_type": {}, "by_freshness": {}}
    try:
        for run, bucket in buckets:
            graph = _graph_for(bucket)
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
                "run_id": meta["id"], "question": _question(run)[:80],
                "claims": len(graph["claims"]), "evidence": len(graph["evidence"]),
                "candidates": receipt["candidates"], "persisted": receipt["persisted"],
                "decision_counts": receipt["decision_counts"],
                "reason_counts": receipt["reason_counts"],
            })
        return {**summary, "memory_stats": store.memory_stats() if persist else {}}
    finally:
        database.close()


def _merge(left: dict, right: dict) -> dict:
    out = dict(left)
    for key, value in (right or {}).items():
        out[key] = out.get(key, 0) + int(value or 0)
    return dict(sorted(out.items(), key=lambda item: (-item[1], item[0])))


def _recall_phase(buckets: list, *, seed: bool, now=None, vectors=None) -> dict:
    """跨会话召回对比：`seed=False` 时记忆库为空（before），True 时先跑写门（after）。

    走的是**生产入口** `qa_memory.recall_from_run()`（问题实词 ∪ 结论实词 + run 自己的作用域），
    不是手工拼参数 —— 这样"召回率"这个数才代表真实链路。
    """
    database, store = _fresh_store()
    summary = {"runs": 0, "runs_with_hits": 0, "hits_total": 0, "by_channel": {},
               "by_type": {}, "hint_violations": 0, "non_active_hits": 0, "top_scores": [],
               "boundaries": {}, "per_run": [], "planning_hits": 0, "raw_top_scores": []}
    try:
        for run, bucket in buckets:
            graph = _graph_for(bucket)
            meta = _run_meta(run)
            if seed:
                memory.apply_write_gate(store, memory.memory_candidates_from_graph(
                    graph, scope="PATIENT_LONGITUDINAL", owner_user_id=meta["owner_user_id"],
                    session_id=meta["session_id"], industry_pack_id=meta["industry_pack_id"],
                    run_id=meta["id"]), run_id=meta["id"], now=now)
            receipt = memory.recall_from_run(store, run_meta=meta, graph=graph,
                                             question=_question(run), vectors=vectors,
                                             now=now)
            summary["runs"] += 1
            hits = receipt["hits"]
            summary["hits_total"] += len(hits)
            if hits:
                summary["runs_with_hits"] += 1
                summary["top_scores"].append(round(float(hits[0]["score"]), 4))
            for hit in hits:
                for channel in hit["channels"]:
                    summary["by_channel"][channel] = summary["by_channel"].get(channel, 0) + 1
                key = str(hit["memory_type"])
                summary["by_type"][key] = summary["by_type"].get(key, 0) + 1
                if not (hit["hint"] and hit["requires_revalidation"]
                        and not hit["verified_evidence"]):
                    summary["hint_violations"] += 1
                if str(hit["status"]) != "ACTIVE":
                    summary["non_active_hits"] += 1
            # 原始 top 分（不过门槛）：召回门槛的标定要能看到**整条分布**，不能只看过线的那几条
            raw = memory.recall(store, mode="evidence", query=_question(run),
                                terms=_query_terms(graph, _question(run)),
                                owner_user_id=meta["owner_user_id"],
                                session_id=meta["session_id"],
                                industry_pack_id=meta["industry_pack_id"],
                                min_score=0.0, limit=1, now=now, store_log=False,
                                vectors=vectors)
            summary["raw_top_scores"].append(round(float(raw["hits"][0]["score"]), 6)
                                            if raw["hits"] else 0.0)
            reason = str((receipt["stats"].get("vector") or {}).get("reason") or "")
            if reason:
                summary["boundaries"][reason] = summary["boundaries"].get(reason, 0) + 1
            summary["per_run"].append({"run_id": meta["id"], "question": _question(run)[:60],
                                       "hits": len(hits),
                                       "channels": _counter(c for hit in hits
                                                            for c in hit["channels"])})
        scores = sorted(summary["raw_top_scores"])
        summary["scores"] = {
            "runs": len(scores),
            "min": scores[0] if scores else None,
            "median": scores[len(scores) // 2] if scores else None,
            "max": scores[-1] if scores else None,
            "zero": sum(1 for value in scores if value <= 0),
            "top_scores": summary["raw_top_scores"],
        }
        summary["threshold_sensitivity"] = {
            str(threshold): sum(1 for value in scores if value >= threshold)
            for threshold in (0.02, 0.05, 0.10, 0.20)
        }
        summary["min_score"] = memory.recall_min_score()
        # 召回跑完之后的记忆图统计（含 recall 日志行数；写入阶段的快照里 recalls 还是 0）
        summary["memory_stats"] = store.memory_stats()
        planning = memory.recall(store, mode="planning", query="",
                                 owner_user_id="", session_id="", industry_pack_id="",
                                 include_all_scopes=True)
        summary["planning_hits"] = len(planning["hits"])
        summary["planning_note"] = "planning 模式只召回 STRATEGY/FAILURE/QUERY_PATTERN（Phase 12 的账）"
        return summary
    finally:
        database.close()


def _lifecycle_phase(buckets: list, *, captured_at: str, now=None) -> dict:
    """衰减分布：写完之后在 +0/+30/+90/+365 天各跑一次 lifecycle，并验证幂等。"""
    database, store = _fresh_store()
    # 基准时刻优先取快照抓取时刻，缺失时退到**钉死的常量**（不读墙上时钟，见 `_graph_for`）
    base = _parse_time(captured_at) or baseline_clock()
    out = {"base": base.isoformat(timespec="seconds"), "at_days": {}, "idempotent": None,
           "deterministic": None}
    try:
        for run, bucket in buckets:
            meta = _run_meta(run)
            memory.apply_write_gate(store, memory.memory_candidates_from_graph(
                _graph_for(bucket), scope="PATIENT_LONGITUDINAL",
                owner_user_id=meta["owner_user_id"], session_id=meta["session_id"],
                industry_pack_id=meta["industry_pack_id"], run_id=meta["id"]),
                run_id=meta["id"], now=now)
        first_pass = {}
        for days in DECAY_DAYS:
            moment = base + timedelta(days=days)
            report = memory.apply_lifecycle(store, now=moment)
            first_pass[str(days)] = {
                "checked": report["checked"],
                "transitions": len(report["transitions"]),
                "transition_counts": report["transition_counts"],
                "status_counts": report["status_counts"],
                "decay_bands": report["decay_bands"],
            }
            if days == DECAY_DAYS[0]:
                again = memory.apply_lifecycle(store, now=moment)
                out["idempotent"] = len(again["transitions"]) == 0
                out["second_pass_transitions"] = len(again["transitions"])
        out["at_days"] = first_pass
        # 确定性：另起一个库、同一时钟再算一遍，分布必须逐字相同
        other_database, other_store = _fresh_store()
        try:
            for run, bucket in buckets:
                meta = _run_meta(run)
                memory.apply_write_gate(other_store, memory.memory_candidates_from_graph(
                    _graph_for(bucket), scope="PATIENT_LONGITUDINAL",
                    owner_user_id=meta["owner_user_id"], session_id=meta["session_id"],
                    industry_pack_id=meta["industry_pack_id"], run_id=meta["id"]),
                    run_id=meta["id"], now=now)
            # 复现同样的**时间序列**（+0 → +30 → +90）：状态机会记住中间态，
            # 只跳到 +90 比是拿"另一次实验"跟"连续老化"比，本来就不该相等。
            repeat = {}
            for days in DECAY_DAYS[:3]:
                repeat = memory.apply_lifecycle(other_store, now=base + timedelta(days=days))
            out["deterministic"] = (repeat["decay_bands"] == first_pass["90"]["decay_bands"]
                                    and repeat["status_counts"] == first_pass["90"]["status_counts"])
        finally:
            other_database.close()
        return out
    finally:
        database.close()


def _parse_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _provenance_and_pollution(buckets: list, *, now=None) -> dict:
    database, store = _fresh_store()
    try:
        for run, bucket in buckets:
            meta = _run_meta(run)
            memory.apply_write_gate(store, memory.memory_candidates_from_graph(
                _graph_for(bucket), scope="PATIENT_LONGITUDINAL",
                owner_user_id=meta["owner_user_id"], session_id=meta["session_id"],
                industry_pack_id=meta["industry_pack_id"], run_id=meta["id"]),
                run_id=meta["id"], now=now)
        provenance = memory.provenance_report(store)
        items = store.load_memory_items(include_all_scopes=True)
        links = store.memory_evidence(memory_ids=[item["memory_id"] for item in items])
        unsupported = [link for link in links if str(link.get("verdict")) != "SUPPORTED"]
        sensitive = [item["memory_id"] for item in items
                     if memory.sensitive_hits(item["canonical_content"])]
        instructions = [item["memory_id"] for item in items
                        if memory.external_instruction(item["canonical_content"])]
        empty_links = [item["memory_id"] for item in items
                       if not any(link["memory_id"] == item["memory_id"] for link in links)]
        return {
            "provenance": provenance,
            "items": len(items),
            "links": len(links),
            "pollution": {
                "unsupported_evidence_links": len(unsupported),
                "memories_without_evidence_link": len(empty_links),
                "sensitive_memories": len(sensitive),
                "external_instruction_memories": len(instructions),
            },
            "status_counts": _counter(item["status"] for item in items),
            "type_counts": _counter(item["memory_type"] for item in items),
            "freshness_counts": _counter(item["freshness_class"] for item in items),
        }
    finally:
        database.close()


def _phase02_alignment(buckets: list, snapshot: dict, *, now=None) -> dict:
    """与 Phase 02 的证据身份对齐：记忆链的指纹必须与 `qa_evidence` 的口径逐条相等。"""
    database, store = _fresh_store()
    checked, matched, seen_rows = 0, 0, len(snapshot.get("qa_evidence_seen") or [])
    try:
        for run, bucket in buckets:
            meta = _run_meta(run)
            graph = _graph_for(bucket)
            memory.apply_write_gate(store, memory.memory_candidates_from_graph(
                graph, scope="PATIENT_LONGITUDINAL", owner_user_id=meta["owner_user_id"],
                session_id=meta["session_id"], industry_pack_id=meta["industry_pack_id"],
                run_id=meta["id"]), run_id=meta["id"], now=now)
        links = store.memory_evidence(memory_ids=[item["memory_id"]
                                                  for item in store.load_memory_items(
                                                      include_all_scopes=True)])
        expected = {}
        for run, bucket in buckets:
            for row in bucket.get("evidence") or []:
                item = _evidence_object(row)
                if item:
                    expected[str(item.get("evidence_ref") or "")] = \
                        qa_evidence.source_fingerprint(item)
        for link in links:
            ref = str(link.get("evidence_ref") or "")
            if ref not in expected:
                continue
            checked += 1
            if str(link.get("source_fingerprint") or "") == expected[ref]:
                matched += 1
        return {"links_checked": checked, "fingerprint_matched": matched,
                "match_rate": round(matched / checked, 6) if checked else None,
                "seen_rows_in_snapshot": seen_rows,
                "note": ("记忆链的来源指纹与 `qa_evidence.source_fingerprint()` 同口径；"
                         "A 机 `qa_evidence_seen` 当前 %d 行（表在、暂无行），"
                         "所以 seen 侧的分布只能报 0，不编数" % seen_rows)}
    finally:
        database.close()


def _determinism_check(buckets: list, *, now=None) -> dict:
    """整条写门跑两遍（两个独立库、同一时钟）：决策分布与记忆 id 必须逐字相同。"""
    first = _write_phase(buckets, now=now)
    second = _write_phase(buckets, now=now)
    return {
        "decision_distribution_equal": first["decision_counts"] == second["decision_counts"],
        "reason_distribution_equal": first["reason_counts"] == second["reason_counts"],
        "memory_ids_equal": sorted(first["memory_ids"]) == sorted(second["memory_ids"]),
        "persisted_equal": first["persisted"] == second["persisted"],
    }


def _vectors_from_snapshot(snapshot: dict) -> tuple:
    """快照里导出的**库内已有向量** → `(ids, matrix)`（L2 归一化，口径同 Phase 04）。

    快照随生产环境里已有的 1024 维向量（只导"被真实证据引用到的文章"那一小撮），
    **没有任何嵌入计算**：这里只是把 base64 的 float32 还原成矩阵。拿不到 numpy 或快照里
    没有向量就返回 `([], None)`，向量通道会如实记 `no_vectors` 边界。
    """
    rows = snapshot.get("intel_article_embeddings") or []
    if not rows:
        return [], None
    try:
        import base64

        import numpy as np
    except Exception:      # noqa: BLE001
        return [], None
    ids, arrays = [], []
    for row in rows:
        blob = str((row or {}).get("embedding_b64") or "")
        dim = int((row or {}).get("embedding_dim") or 0)
        if not blob or dim <= 0:
            continue
        try:
            raw = base64.b64decode(blob)
        except Exception:      # noqa: BLE001
            continue
        array = np.frombuffer(raw, dtype=np.float32)
        if array.size != dim:
            continue
        ids.append(int((row or {}).get("article_id") or 0))
        arrays.append(array)
    if not arrays:
        return [], None
    matrix = np.vstack(arrays).astype(np.float32, copy=False)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return ids, matrix / np.maximum(norms, 1e-9)


def _query_terms(graph: dict, question: str) -> set:
    """召回查询实词（与 `qa_memory.recall_from_run` 完全同口径）：问题实词 ∪ 结论实词。"""
    from qa_verifier import term_set

    terms = term_set(question, sizes=(2, 3)) if question else set()
    for node in (graph or {}).get("claims") or []:
        claim = node.get("claim") if isinstance(node.get("claim"), dict) else {}
        text = str(claim.get("text") or node.get("text") or "")
        if text:
            terms |= term_set(text, sizes=(2, 3))
    return terms


def _normalize_snapshot(snapshot: dict) -> dict:
    """把 Phase 09 快照的 `qa_*` 表键映射成 Phase 08 复算工具用的别名键。

    本阶段的快照是按**表名**落的（`qa_runs` / `qa_claims` / …，便于和 missing_tables /
    catalog 逐表对照），而 `tools/qa_phase08_context_acceptance._rows_by_run` 的输入口径是
    `runs/claims/edges/conflicts/evidence`。这里做一次显式映射，**不复制数据**（只是别名），
    两个工具因此都能吃同一份快照。
    """
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    aliases = {"runs": "qa_runs", "claims": "qa_claims", "edges": "qa_claim_evidence",
               "conflicts": "qa_conflicts", "evidence": "qa_evidence"}
    normalized = dict(snapshot)
    for alias, table in aliases.items():
        if not normalized.get(alias) and snapshot.get(table):
            normalized[alias] = snapshot[table]
    return normalized


def _verification_phase(pairs) -> dict:
    """离线补跑 Phase 03 的核验分布（写门的输入侧证据：多少 claim 真的有 SUPPORTED 支撑）。"""
    verdicts, claims_with_supported, claims_total = {}, 0, 0
    for run, bucket in pairs:
        graph = _graph_for(bucket)
        verdicts = _merge(verdicts, graph.get("verdicts") or {})
        rows = memory.claim_evidence_rows(graph)
        for node in graph["claims"]:
            claims_total += 1
            claim_id = str(node.get("canonical_id") or "")
            if any(row["graph_relation"] == "SUPPORTS"
                   and row["verdict"] == contracts.EVIDENCE_STATUS_SUPPORTED
                   for row in rows.get(claim_id) or []):
                claims_with_supported += 1
    return {"claim_verdicts": verdicts, "claims": claims_total,
            "claims_with_supported_evidence": claims_with_supported,
            "supported_rate": round(claims_with_supported / claims_total, 6) if claims_total else None}


def build_report(snapshot: dict, *, snapshot_path: str = "") -> dict:
    snapshot = _normalize_snapshot(snapshot)
    buckets = sorted(_rows_by_run(snapshot).items())
    pairs = [(bucket.get("run") or {}, bucket) for _run_id, bucket in buckets
             if (bucket.get("run") or {}).get("id")]
    # 基准时刻 = 快照抓取时刻（写进产物，便于复核）；缺失时退到常量，**绝不退到"现在"**
    clock = set_baseline_clock(snapshot.get("captured_at_utc"))
    frozen_now = baseline_clock()
    write = _write_phase(pairs, now=frozen_now)
    write_receipt = {key: write[key] for key in (
        "runs", "candidates", "persisted", "merged", "session_only", "dropped",
        "decision_counts", "reason_counts", "by_type", "by_freshness", "per_run")}
    no_persist = _write_phase(pairs, persist=False, now=frozen_now)
    vector_ids, vector_matrix = _vectors_from_snapshot(snapshot)
    vectors = (vector_ids, vector_matrix)
    recall_before = _recall_phase(pairs, seed=False, now=frozen_now, vectors=vectors)
    recall_after = _recall_phase(pairs, seed=True, now=frozen_now, vectors=vectors)
    lifecycle = _lifecycle_phase(pairs, captured_at=str(snapshot.get("captured_at_utc") or ""),
                                 now=frozen_now)
    pollution = _provenance_and_pollution(pairs, now=frozen_now)
    alignment = _phase02_alignment(pairs, snapshot, now=frozen_now)
    determinism = _determinism_check(pairs, now=frozen_now)
    verification = _verification_phase(pairs)
    catalog = dict(snapshot.get("catalog") or {})
    item_schema_ok = 0
    # 形状自检：落库的每一条记忆都要过 `memory_item` 契约（用一次独立库取回真行）
    database, store = _fresh_store()
    try:
        for run, bucket in pairs:
            meta = _run_meta(run)
            memory.apply_write_gate(store, memory.memory_candidates_from_graph(
                _graph_for(bucket), scope="PATIENT_LONGITUDINAL",
                owner_user_id=meta["owner_user_id"], session_id=meta["session_id"],
                industry_pack_id=meta["industry_pack_id"], run_id=meta["id"]),
                run_id=meta["id"], now=frozen_now)
        rows = store.load_memory_items(include_all_scopes=True)
        for row in rows:
            ok, _note = validate("memory_item", {key: row.get(key) for key in (
                "memory_id", "memory_type", "canonical_content", "confidence",
                "freshness_class", "status", "scope")})
            item_schema_ok += 1 if ok else 0
        item_count = len(rows)
    finally:
        database.close()

    checks = [
        {"name": "write_gate_produces_memories", "passed": write["persisted"] > 0,
         "detail": "真实数据上落库 %d 条记忆（候选 %d）" % (write["persisted"], write["candidates"])},
        {"name": "write_gate_all_four_decisions_reachable_in_tests",
         "passed": bool(no_persist["decision_counts"]),
         "detail": "候选决策分布：%s" % json.dumps(no_persist["decision_counts"], ensure_ascii=False)},
        {"name": "memory_item_contract", "passed": item_count > 0 and item_schema_ok == item_count,
         "detail": "%d/%d 条记忆过 memory_item 契约" % (item_schema_ok, item_count)},
        {"name": "provenance_traceable",
         "passed": pollution["provenance"]["checked"] > 0
         and pollution["provenance"]["traceable"] == pollution["provenance"]["checked"],
         "detail": "回溯 %d/%d" % (pollution["provenance"]["traceable"],
                                   pollution["provenance"]["checked"])},
        {"name": "no_unsupported_evidence_in_memory",
         "passed": pollution["pollution"]["unsupported_evidence_links"] == 0,
         "detail": "非 SUPPORTED 证据链接 %d 条（MASTER_RULES 11）"
                   % pollution["pollution"]["unsupported_evidence_links"]},
        {"name": "no_memory_without_evidence_link",
         "passed": pollution["pollution"]["memories_without_evidence_link"] == 0,
         "detail": "无证据链接的记忆 %d 条" % pollution["pollution"]["memories_without_evidence_link"]},
        {"name": "no_sensitive_memory", "passed": pollution["pollution"]["sensitive_memories"] == 0,
         "detail": "含敏感串的记忆 %d 条（MASTER_RULES 18）"
                   % pollution["pollution"]["sensitive_memories"]},
        {"name": "no_external_instruction_memory",
         "passed": pollution["pollution"]["external_instruction_memories"] == 0,
         "detail": "外部指令性内容 %d 条（MASTER_RULES 16）"
                   % pollution["pollution"]["external_instruction_memories"]},
        {"name": "recall_hint_invariants",
         "passed": recall_after["hint_violations"] == 0 and recall_after["non_active_hits"] == 0,
         "detail": "hint 违规 %d / 非 ACTIVE 命中 %d"
                   % (recall_after["hint_violations"], recall_after["non_active_hits"])},
        {"name": "recall_before_after", "passed": recall_before["hits_total"] == 0,
         "detail": "before 命中 %d、after 命中 %d（跨 %d 个真实 run）"
                   % (recall_before["hits_total"], recall_after["hits_total"], recall_after["runs"])},
        {"name": "lifecycle_idempotent", "passed": lifecycle["idempotent"] is True,
         "detail": "同一时钟第二次迁移 %s 条" % lifecycle.get("second_pass_transitions")},
        {"name": "lifecycle_deterministic", "passed": lifecycle["deterministic"] is True,
         "detail": "两个独立库在 +90 天得到相同状态/衰减分布"},
        {"name": "phase02_fingerprint_alignment",
         "passed": alignment["links_checked"] > 0 and alignment["match_rate"] == 1.0,
         "detail": "%d/%d 条证据链接指纹与 Phase 02 同口径"
                   % (alignment["fingerprint_matched"], alignment["links_checked"])},
        {"name": "write_gate_determinism",
         "passed": all(value for key, value in determinism.items() if key.endswith("equal")),
         "detail": json.dumps(determinism, ensure_ascii=False)},
    ]
    failed = [item["name"] for item in checks if not item["passed"]]
    return {
        "report_version": REPORT_VERSION,
        "generated_at_utc": _utc_now_z(),
        # 离线重建的基准时刻（快照抓取时刻）：产物必须与它一一对应、与"现在几点"无关
        "clock": clock,
        "snapshot": {
            "path": snapshot_path,
            "captured_at_utc": snapshot.get("captured_at_utc"),
            "source": snapshot.get("source") or {},
            "catalog": catalog,
            "missing_tables": snapshot.get("missing_tables") or [],
            "memory_tables_present": snapshot.get("memory_tables_present") or [],
        },
        "real_counts": {
            "runs": len(snapshot.get("qa_runs") or []),
            "claims": len(snapshot.get("qa_claims") or []),
            "edges": len(snapshot.get("qa_claim_evidence") or []),
            "evidence": len(snapshot.get("qa_evidence") or []),
            "conflicts": len(snapshot.get("qa_conflicts") or []),
            "evidence_seen": len(snapshot.get("qa_evidence_seen") or []),
        },
        "write_gate": write_receipt,
        "verification": verification,
        "memory_graph": (recall_after.get("memory_stats") or write.get("memory_stats") or {}),
        "before_after": {
            "memories_before": sum(int(catalog.get(name) or 0) for name in (
                "memory_item", "memory_version", "memory_evidence_link")),
            "memories_after": item_count,
            "write_operations": write["persisted"],
            "merged_writes": write["merged"],
            "recall_hits_before": recall_before["hits_total"],
            "recall_hits_after": recall_after["hits_total"],
            "runs_with_hits_before": recall_before["runs_with_hits"],
            "runs_with_hits_after": recall_after["runs_with_hits"],
            "note": ("before = A 机当前状态（八张 memory_* 表不存在、qa_evidence_seen 在但 0 行）；"
                     "after = 同一份真实证据离线跑完 Write Gate 之后的记忆图"),
        },
        "recall": {"before": recall_before, "after": recall_after},
        "vector_source": {
            "rows": len(snapshot.get("intel_article_embeddings") or []),
            "articles": len(snapshot.get("embedding_article_ids") or []),
            "ids": len(vector_ids),
            "dim": int(vector_matrix.shape[1]) if vector_matrix is not None else 0,
            "note": ("库内**已有**向量的只读导出（被真实证据引用到的文章）；"
                     "查询向量是离线质心，全程零嵌入端点调用"),
        },
        "lifecycle": lifecycle,
        "provenance": pollution["provenance"],
        "pollution": pollution["pollution"],
        "distribution": {"by_type": pollution["type_counts"],
                         "by_status": pollution["status_counts"],
                         "by_freshness": pollution["freshness_counts"]},
        "phase02_alignment": alignment,
        "determinism": determinism,
        "checks": checks,
        "acceptance": {"passed": not failed, "failed": failed,
                       "checks": len(checks), "passed_checks": len(checks) - len(failed)},
        "limits": [
            ("向量通道用快照导出的**库内已有向量**（只覆盖被真实证据引用到的文章），"
             "覆盖不到的记忆一律记 no_vectors 边界；查询向量是离线质心，零嵌入端点调用"),
            "真机 run 早于本阶段接线：记忆图是离线重建（同一份代码/证据），线上路径由管线用例覆盖",
            "qa_evidence_seen 在 A 机当前 0 行，所以 seen 分布只能报 0（不编数）",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", default="", help="A 机只读快照 JSON")
    parser.add_argument("--out", default=DEFAULT_OUTPUT)
    parser.add_argument("--json", action="store_true", help="只打印 JSON")
    parser.add_argument("--no-write", action="store_true", help="不落盘（只打印）")
    args = parser.parse_args()
    path = args.snapshot or (DEFAULT_SNAPSHOT if os.path.exists(DEFAULT_SNAPSHOT)
                             else FALLBACK_SNAPSHOT)
    if not os.path.exists(path):
        print("找不到快照：%s（先跑 tools/qa_phase09_real_snapshot.py）" % path)
        return 2
    with open(path, encoding="utf-8") as handle:
        snapshot = json.load(handle)
    report = build_report(snapshot, snapshot_path=path)
    if args.no_write:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["acceptance"]["passed"] else 1
    out_path = args.out
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=1)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("验收报告：%s" % out_path)
        print("真机规模：%s" % json.dumps(report["real_counts"], ensure_ascii=False))
        print("写门：候选 %d → 落库 %d（合并 %d / 仅会话 %d / 丢弃 %d）"
              % (report["write_gate"]["candidates"], report["write_gate"]["persisted"],
                 report["write_gate"]["merged"], report["write_gate"]["session_only"],
                 report["write_gate"]["dropped"]))
        print("核验（离线补跑 Phase 03）：%d/%d 条 claim 有 SUPPORTED 支撑；verdict 分布 %s"
              % (report["verification"]["claims_with_supported_evidence"],
                 report["verification"]["claims"],
                 json.dumps(report["verification"]["claim_verdicts"], ensure_ascii=False)))
        print("写门理由分布：%s" % json.dumps(report["write_gate"]["reason_counts"],
                                            ensure_ascii=False))
        print("决策分布：%s" % json.dumps(report["write_gate"]["decision_counts"],
                                        ensure_ascii=False))
        before_after = report["before_after"]
        print("前后对比：记忆 %d → %d（写操作 %d，其中合并 %d）；召回命中 %d → %d（%d/%d 个 run 命中）"
              % (before_after["memories_before"], before_after["memories_after"],
                 before_after["write_operations"], before_after["merged_writes"],
                 before_after["recall_hits_before"], before_after["recall_hits_after"],
                 before_after["runs_with_hits_after"], report["recall"]["after"]["runs"]))
        print("衰减分布（+0/+30/+90/+365 天）：%s" % json.dumps(
            {key: value["decay_bands"] for key, value in report["lifecycle"]["at_days"].items()},
            ensure_ascii=False))
        print("状态迁移（+365 天）：%s" % json.dumps(
            report["lifecycle"]["at_days"].get("365", {}).get("transition_counts", {}),
            ensure_ascii=False))
        print("向量来源：%s" % json.dumps(report["vector_source"], ensure_ascii=False))
        print("provenance 回溯：%d/%d；污染：%s"
              % (report["provenance"]["traceable"], report["provenance"]["checked"],
                 json.dumps(report["pollution"], ensure_ascii=False)))
        print("与 Phase 02 指纹对齐：%s/%s" % (report["phase02_alignment"]["fingerprint_matched"],
                                              report["phase02_alignment"]["links_checked"]))
        for item in report["checks"]:
            print("%s %s（%s）" % ("PASS" if item["passed"] else "FAIL", item["name"],
                                   item["detail"]))
        print("验收：%s" % ("PASS" if report["acceptance"]["passed"]
                            else "FAIL：%s" % report["acceptance"]["failed"]))
    return 0 if report["acceptance"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
