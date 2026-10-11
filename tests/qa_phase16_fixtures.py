#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 16（P16-01…P16-06）用例的公共构造器（**不是测试模块**：文件名不以 test_ 开头）。

素材全部走**真实链路**构造，测试里不手写任何阶段产物：

  · run / 阶段行   → `QaStore.create_run()` + `QaStore.record_stage()`（编排器的同一写入口，
    `details["result"]` 就是 `qa_stage_runs.details_json` 里那份形状）；
  · 证据图         → `qa_evidence_graph.build_layer()`（Phase 06 的构建器）；
  · 上下文包       → `qa_context_pack.build_context_pack()`（Phase 08 的构建器，含 selection trace）；
  · 执行图         → `qa_execution_graph.build_execution_graph()` + `ExecutionLedger` + `graph_receipt()`；
  · 记忆           → `qa_memory.write_memories_from_graph()` / `recall_from_run()`（Phase 09 的入口）；
  · 技能遥测       → `qa_skills.load_record()` + `QaStore.record_skill_load()`（Phase 11 的入口）；
  · 缓存           → `QaPersistentResilience.cache_put()`（Phase 03/04 的同一张表）；
  · 检索轨迹       → `QaStore.record_reasoning_trace()`（Phase 01 的同一写入口）。

零模型调用、零网络、零嵌入端点。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_phase08_fixtures as fx08  # noqa: E402
import qa_phase09_fixtures as fx09  # noqa: E402

QUESTION = fx08.QUESTION
SCOPE = {"owner_user_id": "p16", "session_id": "s16", "industry_pack_id": "auto"}
SKILL_ID = "semantic_search"
"""默认预算下真的会被 Phase 11 选中的检索型技能（`web_search` 会被延迟预算拦下）。"""
TRACE_HOP_ONE = {"hop_index": 1, "round_index": 0, "gap_id": "G1", "route": "keyword",
                 "results": 12, "accepted": 2, "rejected": 10, "new_claims": 1,
                 "resolved_gap": 1, "latency_ms": 480, "status": "completed"}
TRACE_HOP_TWO = {"hop_index": 2, "round_index": 1, "gap_id": "G2", "route": "semantic",
                 "results": 8, "accepted": 0, "rejected": 8, "new_claims": 0,
                 "resolved_gap": 0, "latency_ms": 510, "status": "completed"}


def store_for(temp_dir, name="phase16.sqlite3"):
    """隔离临时 sqlite 上的 QaStore（setUp 里断言 backend 必须是 sqlite）。"""
    from qa_storage import QaStore
    from sqlite_database import SQLiteDatabase

    database = SQLiteDatabase(os.path.join(temp_dir, name))
    database.connect()
    database.create_tables()
    assert database.backend == "sqlite", "测试必须跑在隔离 sqlite 上"
    store = QaStore(database)
    store.ensure_schema()
    return database, store


def new_run(store, *, question=QUESTION, mode="standard", pack="auto", owner="p16",
            session_id=SCOPE["session_id"]):
    return store.create_run({"industry_pack_id": pack, "question": question, "mode": mode,
                             "session_id": session_id},
                            owner_user_id=owner,
                            idempotency_key="phase16-%s" % abs(hash((question, owner))))


def run_meta(run, **overrides):
    meta = {"id": str(run["id"]), **SCOPE}
    meta.update(overrides)
    return meta


def evidence_graph_fixture(*, refs=("article:1", "article:2"), claim_id="c1"):
    """Phase 06 形状的证据图：真证据（过 Phase 02/03）+ 真边。"""
    return fx08.graph(
        claims=[fx08.graph_claim(claim_id, refs=refs, text=fx09.CLAIM_TEXT)],
        evidence=[fx09.verified_evidence(ref, claim_text=fx09.CLAIM_TEXT) for ref in refs],
        edges=[fx08.graph_edge(claim_id, ref, status="SUPPORTED") for ref in refs],
        conflicts=[])


def seed_standard_run(database, store, *, mode="standard", with_context_pack=True,
                      with_evidence_graph=True, with_memory=True, with_skill=True,
                      traces=(TRACE_HOP_ONE, TRACE_HOP_TWO), question=QUESTION, owner="p16"):
    """造一次"跑过 P05/P06/P08/P09/P11"的 standard run，返回 `(run, receipt)`。

    阶段行的写入口与编排器**完全一致**（`record_stage` + `details={"result": ...}`），
    所以读侧看到的就是真实持久化形状。
    """
    run = new_run(store, question=question, mode=mode, owner=owner)
    run_id = str(run["id"])
    meta = run_meta(run)

    plan = fx08.plan(hops=[fx08.hop("h1", question)], question=question, mode=mode)
    graph = evidence_graph_fixture()
    graph["version"] = "qa-claim-graph-v1"

    # ── Phase 06 图层 ──
    if with_evidence_graph:
        from qa_evidence_graph import build_layer

        graph["evidence_graph"] = build_layer(graph, plan=plan, run_id=run_id)

    # ── Phase 05 执行图回执（回执挂 stats 兄弟键）──
    from qa_execution_graph import (ExecutionLedger, build_execution_graph,
                                    graph_receipt, hop_node_id)

    execution_graph = build_execution_graph(question, plan=plan, mode=mode, run_id=run_id)
    # 账本用**注入的假时钟**：耗时是确定值，验收可复算（不用墙上钟）。
    ticks = iter([1000.0, 1001.5])

    def _clock():
        try:
            return next(ticks)
        except StopIteration:
            return 1001.5

    ledger = ExecutionLedger(execution_graph, clock=_clock)
    node_id = hop_node_id(execution_graph, "h1")
    try:
        ledger.begin(node_id)
        ledger.finish(node_id, status="completed", latency_ms=1500, evidence=2)
    except Exception:      # 节点 id 形状变化只影响运行期回执，不影响其它断言
        pass
    receipt = graph_receipt(execution_graph, ledger=ledger)

    # ── Phase 09 记忆（写门 → 召回 → 复验）──
    memory_receipt = {}
    memory_items: list = []
    if with_memory:
        from qa_memory import memory_context_items, recall_from_run, write_memories_from_graph

        memory_receipt = write_memories_from_graph(store, graph=graph, run_meta=meta)
        recall = recall_from_run(store, run_meta=meta, graph=graph, question=question)
        graph["memory"] = recall
        # 记忆条目交给上下文包的 `memory_context` 段（Phase 08 的九段之一）：
        # 口径由 Phase 09 自己的 `memory_context_items()` 决定，测试不手搓条目。
        memory_items = memory_context_items(recall)

    # ── Phase 11 技能路由 + 遥测（落既有 qa_stage_runs）──
    skill_items: list = []
    if with_skill:
        from qa_skills import load_record, route_skills, skill_context_items

        # `semantic_search` 是默认预算下**真的会被选中**的检索型技能
        # （`web_search` 会被 OVER_LATENCY_BUDGET 拦下 —— 那是 P11-04 的真实口径，
        #   不能为了让用例好看而放宽预算）。
        routing = route_skills(mandatory=(SKILL_ID,), task_type="research")
        graph["skill_routing"] = routing
        skill_items = skill_context_items(routing)
        store.record_skill_load(run_id, load_record(
            skill_id=SKILL_ID, task_type="research", stage_reached="included_in_pack",
            outcome="ok", reason="ROUTE_MATCH", latency_ms=120.0, cost_units=1.0,
            evidence_yield=2, verified_yield=1, run_id=run_id))

    # ── Phase 08 上下文包（要放在记忆/技能之后：记忆与技能条目是九段里的两段）──
    if with_context_pack:
        from qa_context_pack import build_context_pack

        request = {"question": question, "industry_pack_id": meta["industry_pack_id"],
                   "mode": mode}
        graph["context_pack"] = build_context_pack(
            graph=graph, plan=plan, request=request, run_id=run_id,
            memory_items=memory_items, skill_items=skill_items)

    # ── 落阶段行（与编排器同形状）──
    store.record_stage(run_id, "plan", status="completed",
                       details={"result": plan})
    level1_stats = {"adopted": len(graph.get("evidence") or []),
                    "execution_graph": receipt,
                    "gap_loop": {"stop_reason": "ANSWERABLE", "rounds": [{"round": 0}],
                                 "new_verified_claims": 1, "resolved_high_priority_gaps": 1}}
    store.record_stage(run_id, "level1_retrieval", status="completed",
                       details={"result": {"evidence": list(graph.get("evidence") or []),
                                           "queries": list(plan.get("queries") or []),
                                           "stats": level1_stats,
                                           "cache": {"hit": False, "kb_version": "kb-p16"}},
                                 "token_usage": {"tokens_in": 120, "tokens_out": 40,
                                                 "tokens_total": 160},
                                 "performance": {"elapsed_ms": 1234, "budget_ms": 60000}})
    store.record_stage(run_id, "synthesis", status="completed",
                       details={"result": {"status": "ready", "answer": "测试答案"},
                                 "token_usage": {"tokens_in": 300, "tokens_out": 220,
                                                 "tokens_total": 520}})
    store.record_stage(run_id, "conflict_review", status="completed",
                       details={"result": graph})

    # ── Phase 01 检索轨迹 ──
    for trace in traces or ():
        store.record_reasoning_trace(
            run_id, sub_query=str(trace.get("sub_query") or question),
            depends_on=["sq:h1"], used_evidence_refs=["article:1"],
            missing_links=["外汇合规"], next_queries=["家族办公室 利得税宽免"], **trace)

    # ── Phase 03/04 检索缓存（同一张表，三个既有 namespace）──
    from qa_resilience import QaPersistentResilience

    resilience = QaPersistentResilience(database)
    for namespace in ("level1_retrieval", "level2_retrieval", "qa_verification"):
        resilience.cache_put("key-%s" % namespace, {"ok": True}, namespace=namespace,
                             pack_id="auto", kb_version="kb-p16", ttl_seconds=180)

    return run, {"plan": plan, "graph": graph, "receipt": receipt,
                 "execution_graph": execution_graph, "memory": memory_receipt}


def bare_run(store, *, question="只有 run、没有任何阶段产物的问题", mode="fast"):
    """只建 run、不写任何阶段产物：用来钉住"空态"（老 run / 快速路径的真实状态）。"""
    return new_run(store, question=question, mode=mode)


def seed_skill_rows(store, run_id, *, attempts=3, skill_id=SKILL_ID, outcome="ok"):
    """再补若干技能遥测行（同一技能不同 attempt），用来把成功率样本抬到门限之上。"""
    from qa_skills import load_record

    written = 0
    for index in range(max(1, int(attempts))):
        result = store.record_skill_load(str(run_id), load_record(
            skill_id=skill_id, task_type="research", stage_reached="executed",
            outcome=outcome, reason="ROUTE_MATCH", latency_ms=100.0 + index,
            cost_units=1.0, evidence_yield=2, verified_yield=1,
            run_id=str(run_id), attempt=index + 10))
        written += 1 if result.get("ok") else 0
    return written
