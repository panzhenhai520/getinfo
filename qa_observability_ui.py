#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 Phase 16（P16-01…P16-06）· **可观测性 UI 的只读投影层**。

这个模块只做一件事：把前序阶段**已经落盘**的产物，按 §15/§23/§32 要求的六个视图
重新组织成"可解释、可追溯"的只读载荷。它自己**不产生任何新业务语义**：

  · P16-01 Execution View   ← Phase 05 `stats["execution_graph"]` 回执 + 账本运行期 + 节点运行行
  · P16-02 Evidence View    ← Phase 06 `EvidenceGraphRepository`（**与既有 HTTP API 同一条代码路径**）
  · P16-03 Context Inspector← Phase 08 `graph["context_pack"]`（九段包 + selection trace + 预算）
  · P16-04 Memory View      ← Phase 09/10/12/15 的 `memory_*` 只读仓储
  · P16-05 Search Trace     ← Phase 01/07 的 `qa_reasoning_traces` + §15 追加的六个计数字段
  · P16-06 Dashboard        ← Phase 00/03/04/11 的 latency/token/cache/skill 落点 + 既有 `QaMetricsService`

三条硬规矩（都是可机器校验的）：

  1. **零写入**：本模块不 import 任何写路径，也不自带 SQL；读一律走 `QaStore` 的只读方法
     与既有只读服务。守门用例用 AST 断言本文件里没有 INSERT/UPDATE/DELETE 与写方法调用。
  2. **每个数字都带出处与口径**：`numbers[<字段>] = {field, value, source, definition}`，
     `source` 必须命中 `qa_graph_contracts.OBSERVABILITY_SOURCES` 白名单的某一项前缀 ——
     "UI 层另算一套"因此在结构上不可能发生。
  3. **空态/降级态是合法结果**：读不到产物就给 `empty`/`degraded`/`unavailable` 并点名理由码，
     **绝不用 0 冒充**。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Mapping, Sequence

from qa_graph_contracts import (
    OBSERVABILITY_SOURCES,
    OBSERVABILITY_STATES,
    OBSERVABILITY_STATE_REASONS,
    OBSERVABILITY_UI_VERSION,
)

# 来源白名单的按阶段索引（**只从契约里取**，本模块不自己写来源字面量）。
# 同一个阶段可能有多条载体（例如 Phase 08 既有阶段输出、又有 SSE 回执），
# 因此这里**首个优先**建立 `{阶段: 主载体}`，次要用 `_source()` 按子串挑。
def _source(phase: str, hint: str = "") -> str:
    candidates = [item for item in OBSERVABILITY_SOURCES
                  if item.split("·", 1)[0] == str(phase)]
    for item in candidates:
        if hint and hint in item:
            return item
    return candidates[0] if candidates else ""


SRC: dict = {}
for _entry in OBSERVABILITY_SOURCES:
    SRC.setdefault(_entry.split("·", 1)[0], _entry)
SRC_CONTEXT_EVENTS = _source("P08", "qa_events")
"""§15 的快速路径只在 SSE 事件里留上下文包回执 —— 那是 Phase 08 的**另一条载体**。"""

# §15 追加到 Search Trace 的六个字段（架构原文逐字）：这六项在视图里必须齐备，
# 每项的出处由 `numbers[*].source` 指明（它们本来就是各阶段自己记的数，UI 只是读出来）。
SEARCH_TRACE_ADDITIONS = (
    "memory_recalled", "memory_revalidated", "context_items_selected",
    "context_items_dropped", "context_tokens", "skills_loaded",
)

# §23 要求 UI 能回答的九个问题（key 是字段名，值是给用户看的中文问句）
SEARCH_TRACE_QUESTIONS = (
    ("why_search", "为什么搜？"), ("query", "搜了什么？"), ("route", "为什么走这个 route？"),
    ("results", "找到了什么？"), ("accepted", "为什么接受？"), ("rejected", "为什么拒绝？"),
    ("resolved_gap", "解决了哪个 Gap？"), ("continue_reason", "为什么继续？"),
    ("stop_reason", "为什么停止？"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _num(field: str, value, source: str, definition: str) -> dict:
    """一条"可追溯的数字"：字段名 + 值 + **来源** + **口径定义**（四项都不许空）。"""
    return {"field": str(field), "value": value, "source": str(source),
            "definition": str(definition)}


def _numbers(rows: Sequence[tuple]) -> dict:
    """`[(字段, 值, 来源, 口径), ...]` → `{字段: 数字对象}`。"""
    out: dict = {}
    for field, value, source, definition in rows:
        out[str(field)] = _num(field, value, source, definition)
    return out


def _view(view: str, *, state: str, reason: str, state_text: str, source: str,
          numbers: Mapping | None = None, payload: Mapping | None = None,
          run_id: str = "", notes: Sequence[str] = ()) -> dict:
    """组装一个视图载荷（形状由 `OBSERVABILITY_VIEW_SCHEMA` 钉死）。"""
    return {
        "view_version": OBSERVABILITY_UI_VERSION,
        "view": str(view),
        "run_id": str(run_id or ""),
        "state": state if state in OBSERVABILITY_STATES else "unavailable",
        "state_reason": (reason if reason in OBSERVABILITY_STATE_REASONS
                         else "STORE_UNAVAILABLE"),
        "state_text": str(state_text or ""),
        "source": str(source or ""),
        "notes": [str(item) for item in (notes or ()) if str(item or "")],
        "numbers": dict(numbers or {}),
        "payload": dict(payload or {}),
        "generated_at": _now(),
        "read_only": True,
    }


def _mapping(value) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _bucket(counts: Mapping | None) -> dict:
    """把计数映射按"次数降序、键升序"排好（同输入同顺序，便于断言与肉眼比对）。"""
    rows = {str(key or ""): int(value or 0)
            for key, value in _mapping(counts).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)}
    return dict(sorted(rows.items(), key=lambda item: (-item[1], item[0])))


def _json_list(raw) -> list:
    """`qa_reasoning_traces` 的 `*_json` 文本列 → 列表（坏值退化成空列表，绝不抛）。"""
    if isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        value = json.loads(raw) if raw not in (None, "") else []
    except (TypeError, ValueError):
        return []
    return list(value) if isinstance(value, (list, tuple)) else []


# ── 读取公共设施：阶段产物（`qa_stage_runs.details_json`）──────────────────────
def stage_outputs(store, run_id: str) -> dict:
    """`{stage: {'status': ..., 'result': {...}, 'attempt': ...}}`（只读一次，多视图共用）。

    读不出任何阶段行时返回 `{}`，调用方据此给 `empty`（老 run / 空库的真实状态）。
    """
    out: dict = {}
    try:
        rows = store.stage_runs(str(run_id))
    except Exception:
        return out
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        stage = str(row.get("stage") or "")
        if not stage:
            continue
        details = row.get("details") if isinstance(row.get("details"), Mapping) else {}
        result = details.get("result") if isinstance(details.get("result"), Mapping) else {}
        item = {"status": str(row.get("status") or ""), "attempt": int(row.get("attempt") or 1),
                "latency_ms": row.get("latency_ms"), "error_code": str(row.get("error_code") or ""),
                "node_id": str(row.get("node_id") or ""), "node_kind": str(row.get("node_kind") or ""),
                "parent_node_id": str(row.get("parent_node_id") or ""),
                "round_index": int(row.get("round_index") or 0),
                "token_usage": _mapping(details.get("token_usage")),
                "result": dict(result)}
        # 同一 stage 可能有多 attempt：保留**已完成/降级**的那次，否则保留最后一次
        previous = out.get(stage)
        if previous is None or item["status"] in {"completed", "degraded"}:
            out[stage] = item
    return out


def _stats_of(stages: Mapping, stage: str) -> dict:
    return _mapping((_mapping(_mapping(stages.get(stage)).get("result"))).get("stats"))


def _missing_reason(stages: Mapping, switch_reader=None) -> str:
    """产物缺失时的**诚实理由码**（P16 的边界：不许把"没跑过"和"开关关着"混为一谈）。

    · 该 run 一条阶段行都没有 → `NO_STAGE_ARTIFACT`（分不清"老 run"与"根本没跑"，
      这说明不了任何事，就如实说"没有产物"）；
    · 有阶段行（证明它确实跑过）**且**该阶段的开关当前是关的 → `SWITCH_OFF`
      （读侧探测出来的最可能解释，`state_text` 里必须写明这是推断而不是当时的事实）。
    """
    if not stages:
        return "NO_STAGE_ARTIFACT"
    if callable(switch_reader):
        try:
            if not switch_reader():
                return "SWITCH_OFF"
        except Exception:  # noqa: BLE001 —— 开关读不出来就退回"没有产物"
            pass
    return "NO_STAGE_ARTIFACT"


def context_pack_of(store, run_id: str, stages: Mapping | None = None) -> tuple:
    """取该 run 的上下文包正文 `(pack, source_text)`（Phase 08·P08-01…P08-06）。

    优先 `conflict_review` 的阶段输出（standard/deep：`graph["context_pack"]` 整体落在
    `details["result"]` 里，**免费持久化**）；快速路径没有这个阶段输出，于是退到
    `qa_events` 里 Phase 08 自己发出的 `context_pack` 回执（只有计数、没有 selection trace，
    这一点必须在 `source_text` 里如实写出来）。
    """
    stages = stages if isinstance(stages, Mapping) else stage_outputs(store, run_id)
    pack = _mapping(_mapping(stages.get("conflict_review")).get("result")).get("context_pack")
    if isinstance(pack, Mapping) and pack:
        return dict(pack), SRC["P08"]
    try:
        # 注意 `QaStore.events_after()` 返回的是**整条事件**字典（含 `payload` 键，
        # 因为 `append_event` 把整个事件序列化进 `qa_events.payload_json`）。
        # 两种形状都容错：带 `payload` 包装的优先，裸 payload 也认。
        for event in store.events_after(str(run_id), 0) or ():
            row = _mapping(event)
            payload = _mapping(row.get("payload")) or row
            receipt = payload.get("context_pack")
            if isinstance(receipt, Mapping) and receipt:
                return {"receipt_only": dict(receipt)}, SRC_CONTEXT_EVENTS
    except Exception:
        pass
    return {}, ""


# ── P16-01：Execution View（执行图）───────────────────────────────────────────
def execution_view(store, run_id: str, *, stages: Mapping | None = None) -> dict:
    """§15 的 Execution Graph View：节点/边/并行组/预算/停止原因/**运行期真的跑到哪**。

    数字**全部取自已持久化的回执**（Phase 05·P05-05 的 `stats["execution_graph"]`）；
    节点明细由 Phase 05 的**同一个构建器**从已持久化的计划复算（`detail_source=rebuilt`），
    两者口径同源 —— 回执是回放、明细是可复算（Phase 06 读侧同样的两手）。
    """
    stages = stages if isinstance(stages, Mapping) else stage_outputs(store, run_id)
    receipt = {}
    for stage in ("level1_retrieval", "level2_retrieval", "synthesis"):
        candidate = _mapping(_stats_of(stages, stage).get("execution_graph"))
        if candidate:
            receipt = candidate
            break
    if not receipt:
        try:
            from qa_execution_graph import graph_enabled
        except Exception:  # noqa: BLE001
            graph_enabled = None
        reason = _missing_reason(stages, graph_enabled)
        return _view(
            "execution", run_id=run_id, state="empty", reason=reason,
            state_text=(
                ("本 run 跑过阶段，但执行图开关当前是关的（`QA_EXECUTION_GRAPH` 默认关；"
                 "关着时行为逐字回到接线前）—— 这是**读侧探测的推断**，不是当时的事实记录。"
                 if reason == "SWITCH_OFF" else
                 "本 run 没有执行图产物：`QA_EXECUTION_GRAPH` 未开启，或该阶段没有留下产物"
                 "（老 run / 快速路径）。这里如实给空态，不用 0 冒充。")),
            source=SRC["P05"], notes=[
                "来源：Phase 05·P05-05 把 `graph_receipt()` 挂在 `stats[\"execution_graph\"]` 兄弟键上。",
                "空态口径：开关 `QA_EXECUTION_GRAPH` 默认关；关着时该键根本不存在。",
            ],
            numbers=_numbers([
                ("path", None, SRC["P05"] + "#path",
                 "空态下不给 0：没有回执就是没有回执，页面必须显示“无产物”。"),
                ("nodes_total", None, SRC["P05"] + "#node_counts.total", "同上（缺产物 → null）。"),
                ("nodes_runnable", None, SRC["P05"] + "#node_counts.runnable", "同上（缺产物 → null）。"),
                ("budget_total_seconds", None, SRC["P05"] + "#budget.total_seconds",
                 "同上（缺产物 → null）。"),
                ("stop_reason", None, SRC["P05"] + "#stop_reason", "同上（缺产物 → null）。"),
                ("node_run_rows", len([item for item in stages.values()
                                       if str(item.get("node_id") or "")]),
                 SRC["P05"] + "#node_runs",
                 "没有回执时仍然如实报“有没有节点运行行”（两个开关是两件事）。"),
            ]),
            payload={"receipt": {}, "nodes": [], "edges": [], "node_runs": [],
                     "stage_present": sorted(stages)})

    budget = _mapping(receipt.get("budget"))
    runtime = _mapping(receipt.get("runtime"))
    node_counts = _mapping(receipt.get("node_counts"))
    graph_version = str(receipt.get("graph_version") or "")

    # 节点明细：用 Phase 05 的构建器从**已持久化的计划**复算（不重写任何图逻辑）
    nodes: list = []
    edges: list = []
    detail_source = "unavailable"
    detail_note = ""
    plan_output = _mapping(_mapping(stages.get("plan")).get("result"))
    question = str(plan_output.get("standalone_question") or "")
    try:
        from qa_execution_graph import build_execution_graph

        run_row = {}
        try:
            run_row = store.get_run(str(run_id)) or {}
        except Exception:
            run_row = {}
        question = question or str(_mapping(run_row.get("request")).get("question") or "")
        rebuilt = build_execution_graph(
            question, plan=plan_output, mode=str(run_row.get("mode") or "standard"),
            run_id=str(run_id))
        if str(rebuilt.get("graph_version") or "") == graph_version:
            nodes = [dict(item) for item in (rebuilt.get("nodes") or []) if isinstance(item, Mapping)]
            edges = [dict(item) for item in (rebuilt.get("edges") or []) if isinstance(item, Mapping)]
            detail_source = "rebuilt"
            detail_note = ("节点/边明细由 Phase 05 的 `build_execution_graph()` 从**已持久化的计划**"
                           "复算，graph_version 与回执逐字相同；数字一律取回执，不复算。")
        else:
            detail_note = ("复算出的 graph_version(%s) 与回执(%s) 不一致，故不展示节点明细"
                           "（宁可少一层，也不混两个版本的口径）。"
                           % (rebuilt.get("graph_version"), graph_version))
    except Exception as exc:  # noqa: BLE001 —— 明细拿不到只降级，回执照常呈现
        detail_note = "节点明细复算失败：%s: %s" % (type(exc).__name__, str(exc)[:120])

    node_runs = []
    for stage, item in (stages or {}).items():
        if str(item.get("node_id") or "") and not str(stage).startswith("skill:"):
            node_runs.append({"stage": stage, "node_id": item.get("node_id"),
                              "node_kind": item.get("node_kind"),
                              "parent_node_id": item.get("parent_node_id"),
                              "round_index": item.get("round_index"),
                              "status": item.get("status"), "latency_ms": item.get("latency_ms"),
                              "token_usage": item.get("token_usage")})

    state = "ok"
    reason = "PRODUCT_PRESENT"
    state_text = "执行图回执来自 Phase 05（P05-05）落盘的 `stats[\"execution_graph\"]` 兄弟键。"
    if not node_counts.get("runnable"):
        state, reason = "degraded", "PARTIAL_READ"
        state_text = "执行图回执存在，但可运行节点数为 0（计划不可用或阶段未真正展开）。"

    numbers = _numbers([
        ("path", receipt.get("path"), SRC["P05"] + "#path",
         "Phase 05·P05-04 判定的执行路径（fast/standard/deep），`choose_path()` 的唯一输出。"),
        ("path_source", receipt.get("path_source"), SRC["P05"] + "#path_source",
         "路径是怎么定的（显式 / 由 mode / 由建议路径），用于回答“为什么跑这条链”。"),
        ("suggested_path", receipt.get("suggested_path"), SRC["P05"] + "#suggested_path",
         "Phase 05 对问题复杂度的**建议**路径；D-019 规定它不隐式改档，只作建议展示。"),
        ("intent", receipt.get("intent"), SRC["P05"] + "#intent",
         "Phase 05·P05-01 Query Interpreter 的问题意图（9 值闭集合）。"),
        ("complexity", receipt.get("complexity"), SRC["P05"] + "#complexity",
         "Phase 05·P05-01 的复杂度三档（simple/medium/deep）。"),
        ("nodes_total", node_counts.get("total"), SRC["P05"] + "#node_counts.total",
         "执行图节点总数（回执原值，不重算）。"),
        ("nodes_runnable", node_counts.get("runnable"), SRC["P05"] + "#node_counts.runnable",
         "真的会跑的节点数（`deferred` 占位节点不计入）。"),
        ("nodes_deferred", node_counts.get("deferred"), SRC["P05"] + "#node_counts.deferred",
         "被标为 deferred 的占位节点数（未实现/不在链上）。"),
        ("nodes_parallel", node_counts.get("parallel"), SRC["P05"] + "#node_counts.parallel",
         "落在可并行组里的节点数（§2.1/§2.4「能并行就别串行」）。"),
        ("barriers", node_counts.get("barriers"), SRC["P05"] + "#node_counts.barriers",
         "汇合点（barrier）节点数（§2.5 多入度节点）。"),
        ("budget_total_seconds", budget.get("total_seconds"), SRC["P05"] + "#budget.total_seconds",
         "该路径的阶段预算之和（Phase 05·P05-05 `path_budget`）。"),
        ("estimated_wall_clock_seconds", budget.get("estimated_wall_clock_seconds"),
         SRC["P05"] + "#budget.estimated_wall_clock_seconds",
         "关键路径的墙钟估算（按并行组折算，不是节点耗时简单相加）。"),
        ("budget_infeasible", budget.get("infeasible"), SRC["P05"] + "#budget.infeasible",
         "裁掉所有可选节点后仍然装不下时为 True（`apply_budget` 的结论）。"),
        ("cut_nodes", len(budget.get("cut_nodes") or []), SRC["P05"] + "#budget.cut_nodes",
         "因预算被裁掉的**可选**节点个数（从链尾往前裁）。"),
        ("critical_path", len(budget.get("critical_path") or []),
         SRC["P05"] + "#budget.critical_path", "关键路径上的节点个数。"),
        ("hop_count", receipt.get("hop_count"), SRC["P05"] + "#hop_count",
         "研究计划里的跳数（`research_plan.decomposition.hop_count`）。"),
        ("dag_ok", receipt.get("dag_ok"), SRC["P05"] + "#dag_ok",
         "子问题 DAG 的合法性（复用 `qa_query_decompose.validate_dag`，环/自环/逆序都拦）。"),
        ("contract_ok", receipt.get("contract_ok"), SRC["P05"] + "#contract_ok",
         "Phase 05 的 `validate_contract('execution_graph', ...)` 结论。"),
        ("stop_reason", receipt.get("stop_reason"), SRC["P05"] + "#stop_reason",
         "Phase 05 阶段可能给出的停止原因（本阶段只可能 ANSWERABLE/BUDGET_EXHAUSTED/MAX_DEPTH）。"),
        ("runtime_used_seconds", runtime.get("used_seconds"), SRC["P05"] + "#runtime.used_seconds",
         "`ExecutionLedger` 记的运行期实际耗时（秒，用注入时钟，可复算）。"),
        ("runtime_total_seconds", runtime.get("total_seconds"),
         SRC["P05"] + "#runtime.total_seconds", "账本的总预算（秒）。"),
        ("runtime_remaining_seconds", runtime.get("remaining_seconds"),
         SRC["P05"] + "#runtime.remaining_seconds", "账本剩余预算（秒）。"),
        ("runtime_over_budget", runtime.get("over_budget"), SRC["P05"] + "#runtime.over_budget",
         "运行期是否超出该路径预算（账本的唯一停止判据之一）。"),
        ("runtime_executed_count", runtime.get("executed_count"),
         SRC["P05"] + "#runtime.executed_count", "账本里真的记到运行过的节点个数。"),
        ("runtime_nodes_run", len(runtime.get("nodes") or []), SRC["P05"] + "#runtime.nodes",
         "账本节点明细行数（与 `executed_count` 同源，分列便于发现不一致）。"),
        ("node_run_rows", len(node_runs), SRC["P05"] + "#node_runs",
         "`qa_stage_runs` 里带 node_id 的真实节点运行行数（开关 `QA_EXECUTION_GRAPH_NODE_RUNS` "
         "默认关，关着时为 0 —— 这是“没记账”而不是“没运行”）。"),
        ("node_detail_source", detail_source, SRC["P05"] + "#detail_source",
         "节点/边明细的取得方式：rebuilt=用 Phase 05 构建器从已持久化计划复算。"),
    ])
    notes = [detail_note] if detail_note else []
    notes.append("回执里的数字**一律不回算**；明细层只补节点/边结构，且版本号必须与回执逐字相同。")
    return _view(
        "execution", run_id=run_id, state=state, reason=reason, state_text=state_text,
        source=SRC["P05"], numbers=numbers, notes=notes,
        payload={
            "graph_version": graph_version,
            "receipt": dict(receipt),
            "detail_source": detail_source,
            "stage_chain": list(receipt.get("stage_chain") or []) or [
                str(item.get("stage") or "") for item in nodes if item.get("stage")],
            "nodes": nodes, "edges": edges, "parallel_groups": list(receipt.get("parallel_groups") or []),
            "budget": budget, "runtime": runtime, "node_runs": node_runs,
            "stop_reasons": list(receipt.get("stop_reasons") or []),
        })


# ── P16-02：Evidence View（证据图）────────────────────────────────────────────
def evidence_view(store, run_id: str, *, limit: int = 20) -> dict:
    """§15 的 Evidence Graph View：结论-证据关系、覆盖度、矛盾裁决。

    **刻意复用网关同一条代码路径**（`EvidenceGraphRepository.load_persisted` 优先、
    否则 `build()` 从四张既有表重建），因此页面上的数字与
    `GET /api/qa/v1/runs/<id>/evidence-graph` 逐字一致 —— 不存在第二套口径。
    """
    cap = max(1, min(int(limit or 20), 200))
    try:
        from qa_evidence_graph import EvidenceGraphRepository

        repository = EvidenceGraphRepository(store)
        persisted = repository.load_persisted(run_id)
        layer = dict(persisted) if persisted else repository.build(run_id)
        origin = "persisted" if persisted else "rebuilt"
    except Exception as exc:  # noqa: BLE001
        return _view(
            "evidence", run_id=run_id, state="unavailable", reason="STORE_UNAVAILABLE",
            state_text="证据图读侧不可用：%s" % str(exc)[:160], source=SRC["P06"],
            payload={"claims": [], "contradictions": []})

    coverage = _mapping(layer.get("coverage"))
    stats = _mapping(layer.get("stats"))
    claims = list(layer.get("claims") or [])
    contradictions = list(layer.get("contradictions") or [])
    error = str(layer.get("error") or "")
    unresolved = [item for item in contradictions
                  if isinstance(item, Mapping) and str(item.get("resolution") or "") in
                  {"unresolved", "UNRESOLVED"}]
    if error:
        state, reason = "degraded", "PHASE_DEGRADED"
        state_text = "证据图带错误标记（%s），其余字段仍是真实产物。" % error[:120]
    elif not claims:
        state, reason = "empty", "NO_STAGE_ARTIFACT"
        state_text = ("该 run 没有可用的证据图对象（老 run / 该阶段没跑过）。"
                      "空图不等于“证据为零”，只表示本次没有已持久化的结论-证据关系。")
    else:
        state, reason = "ok", "PRODUCT_PRESENT"
        state_text = ("证据图来自 Phase 06；%s。与 `/api/qa/v1/runs/<id>/evidence-graph` "
                      "共用同一读侧代码。" % ("当时真的跑过的那份" if origin == "persisted"
                                              else "按四张既有表重建的同口径图"))
    definition = str(coverage.get("coverage_definition") or "")
    numbers = _numbers([
        ("graph_version", layer.get("graph_version"), SRC["P06"] + "#graph_version",
         "Phase 06 证据图的契约版本（`EVIDENCE_GRAPH_VERSION`）。"),
        ("graph_source", origin, SRC["P06"] + "#source",
         "persisted=读当时跑过的阶段产物；rebuilt=按既有表重建（两者同口径）。"),
        ("claims", len(claims), SRC["P06"] + "#claims", "图上的结论节点数（截断前）。"),
        ("coverage_claims", coverage.get("total_claims"), SRC["P06"] + "#coverage.total_claims",
         "覆盖度分母：非计划 claim 总数（`claim_coverage` 的 `total`）。"),
        ("edges", stats.get("edges"), SRC["P06"] + "#stats.edges",
         "结论-证据边数（关系分布的分母）。"),
        ("evidence_nodes", stats.get("evidence_nodes"), SRC["P06"] + "#stats.evidence_nodes",
         "图上的证据节点数。"),
        ("claim_coverage", coverage.get("claim_coverage"), SRC["P06"] + "#coverage.claim_coverage",
         "主口径覆盖度：%s" % (definition or "见 Phase 06 的 `COVERAGE_DEFINITION`。")),
        ("weighted_claim_coverage", coverage.get("weighted_claim_coverage"),
         SRC["P06"] + "#coverage.weighted_claim_coverage",
         "带权口径：已核验支持强度饱和求和 / claim 数（与主口径并列，不可互相替代）。"),
        ("claimed_claim_coverage", coverage.get("claimed_claim_coverage"),
         SRC["P06"] + "#coverage.claimed_claim_coverage",
         "对照口径：不看核验、只看有没有被 claim 引用（用来看\"核验前\"的样子）。"),
        ("evidence_coverage", coverage.get("evidence_coverage"),
         SRC["P06"] + "#coverage.evidence_coverage",
         "证据侧覆盖：被至少一条 claim 引用的证据占比。"),
        ("refuted_claim_rate", coverage.get("refuted_claim_rate"),
         SRC["P06"] + "#coverage.refuted_claim_rate",
         "被已核验反驳边否定的 claim 占比（反证强度，不是缺陷率）。"),
        ("supported_claims", coverage.get("supported_claims"),
         SRC["P06"] + "#coverage.supported_claims", "已核验 SUPPORTED 的 claim 数。"),
        ("refuted_claims", coverage.get("refuted_claims"), SRC["P06"] + "#coverage.refuted_claims",
         "已核验 REFUTED 的 claim 数。"),
        ("verified_support_mass", coverage.get("verified_support_mass"),
         SRC["P06"] + "#coverage.verified_support_mass",
         "已核验支持强度的饱和求和（带权口径的分子）。"),
        ("relation_supports", _mapping(stats.get("relation_distribution")).get("SUPPORTS"),
         SRC["P06"] + "#stats.relation_distribution",
         "SUPPORTS 边数（另有 REFUTES/DEPENDS/CONTRADICTS/MENTIONS 四类，见 payload）。"),
        ("relation_distribution", _bucket(stats.get("relation_distribution")),
         SRC["P06"] + "#stats.relation_distribution",
         "图级五种关系的完整分布（闭集合 EVIDENCE_GRAPH_RELATIONSHIPS）。"),
        ("contradictions", stats.get("contradictions", len(contradictions)),
         SRC["P06"] + "#stats.contradictions",
         "检测到的矛盾数（两族检测：同结论正反两边 / 结论互斥）。"),
        ("unresolved_contradictions", len(unresolved), SRC["P06"] + "#contradictions.resolution",
         "裁决结论为 unresolved 的矛盾数（UNRESOLVABLE_CONTRADICTION 的唯一权威出口）。"),
        ("status_consistency_checked", _mapping(stats.get("status_consistency")).get("checked"),
         SRC["P06"] + "#stats.status_consistency.checked",
         "被核对过\"边能否复算出核验状态\"的 claim 数。"),
        ("status_consistency_mismatches", _mapping(stats.get("status_consistency")).get("mismatches"),
         SRC["P06"] + "#stats.status_consistency.mismatches",
         "不一致数（应当为 0；不为 0 说明图与核验层口径打架，页面必须显式告警）。"),
        ("truncated_claims", max(0, len(claims) - cap), SRC["P06"] + "#limit",
         "因展示上限被截断的结论数（页面上的 limit 参数，不影响数字口径）。"),
    ])
    return _view(
        "evidence", run_id=run_id, state=state, reason=reason, state_text=state_text,
        source=SRC["P06"], numbers=numbers,
        notes=["覆盖度口径写死在 Phase 06 的 `COVERAGE_DEFINITION`，本视图只读不算。"],
        payload={
            "graph_version": str(layer.get("graph_version") or ""),
            "source": origin, "coverage": coverage, "stats": stats,
            "claims": claims[:cap], "contradictions": contradictions[:cap],
            "truncated": {"claims": max(0, len(claims) - cap),
                          "contradictions": max(0, len(contradictions) - cap)},
        })


# ── P16-03：Context Inspector（上下文检查器）─────────────────────────────────
def _item_origin(section: str) -> str:
    """§15 要求回答"来自 Evidence 还是 Memory"：按 Phase 08 的九段**段名**判定。"""
    name = str(section or "")
    if name == "memory_context":
        return "memory"
    if name == "skill_context":
        return "skill"
    if name in {"task_context", "system_context", "constraints", "budget", "working_memory",
                "gap_context"}:
        return "task" if name in {"task_context", "system_context"} else "context"
    return "evidence"


def context_inspector(store, run_id: str, *, stages: Mapping | None = None,
                      trace_limit: int = 600) -> dict:
    """§15 的 Context Inspector：为何加入 / token 数 / 来自 Evidence 还是 Memory /
    哪些 item 被预算挤出 / 是否 revalidated。

    正文与决策痕迹**全部来自 Phase 08 的 Context Pack**（含 `selection_trace`），
    本视图只做"逐项标注"（origin / why / dropped_reason / revalidated），不重新选包。
    """
    stages = stages if isinstance(stages, Mapping) else stage_outputs(store, run_id)
    pack, pack_source = context_pack_of(store, run_id, stages)
    if not pack:
        try:
            from qa_context_pack import context_pack_enabled
        except Exception:  # noqa: BLE001
            context_pack_enabled = None
        reason = _missing_reason(stages, context_pack_enabled)
        return _view(
            "context", run_id=run_id, state="empty", reason=reason,
            state_text=(
                ("本 run 跑过阶段，但上下文包开关当前是关的（`QA_CONTEXT_PACK`）——"
                 "这是**读侧探测的推断**，不是当时的事实记录。"
                 if reason == "SWITCH_OFF" else
                 "本 run 没有上下文包产物：`QA_CONTEXT_PACK` 未开启，或该路径没有留下阶段输出"
                 "（快速路径只在 SSE 事件里留下计数回执，若事件也读不到就如实给空态）。")),
            source=SRC["P08"], numbers=_numbers([
                ("items", None, SRC["P08"] + "#items",
                 "没有产物时不报 0：空态本身就是“该开关关着/老 run 没跑过”的证据。"),
            ]),
            notes=["来源：Phase 08·P08-01…P08-06 的 `graph[\"context_pack\"]`（conflict_review "
                   "阶段输出免费持久化；快速路径退到 `qa_events` 的计数回执）。"],
            payload={"pack": {}, "items": [], "dropped": [], "sections": {}, "budget": {},
                     "context_gaps": [], "grounding": {}, "selection_trace": []})

    receipt_only = bool(pack.get("receipt_only"))
    receipt = _mapping(pack.get("receipt_only")) or _mapping(pack.get("receipt"))
    if receipt_only:
        # 快速路径：只有计数回执，没有逐项明细 —— 必须如实说明，不能装作有明细
        budget = _mapping(receipt.get("budget"))
        numbers = _numbers([
            ("pack_id", receipt.get("pack_id"), pack_source + "#pack_id", "上下文包内容寻址 id。"),
            ("items", receipt.get("items"), pack_source + "#items",
             "包内条目数（来自 Phase 08 自己发的 SSE 回执，不是本视图数的）。"),
            ("context_tokens", budget.get("used"), pack_source + "#budget.used",
             "整包已用 token（Phase 08 的确定性估算器口径，见其 estimator_note）。"),
            ("tokens_before_trim", budget.get("estimated_tokens_before"),
             pack_source + "#budget.estimated_tokens_before", "裁剪前候选总量（token 估算）。"),
            ("trimmed_items", budget.get("trimmed_items"),
             pack_source + "#budget.trimmed_items", "被预算挤出的条目数。"),
            ("citations", receipt.get("citations"), pack_source + "#citations",
             "引用条目数（记忆与技能条目**不进** citation_map）。"),
            ("context_gaps", sum(int(v or 0) for v in _mapping(receipt.get("context_gaps")).values()),
             pack_source + "#context_gaps", "上下文缺口总数（按类型求和）。"),
            ("retrieval_requested", receipt.get("retrieval_requested"),
             pack_source + "#retrieval_requested",
             "其中会触发新检索的缺口数（§6 + MASTER_RULES 13：默认先重组上下文）。"),
        ])
        return _view(
            "context", run_id=run_id, state="degraded", reason="PARTIAL_READ",
            state_text=("只有 Phase 08 的**计数回执**（快速路径把回执发在 SSE 事件里），"
                        "逐项明细与 selection trace 不可得 —— 因此给 degraded 而不是 ok。"),
            source=pack_source, numbers=numbers,
            notes=["降级口径：计数真实、明细缺失，两者必须区分，不能把回执当明细渲染。"],
            payload={"pack": {}, "receipt": receipt, "items": [], "dropped": [],
                     "sections": _mapping(receipt.get("sections")), "budget": budget,
                     "context_gaps": [], "grounding": {}, "selection_trace": [],
                     "detail_available": False})

    items = [dict(item) for item in (pack.get("items") or []) if isinstance(item, Mapping)]
    trace = [dict(row) for row in (pack.get("selection_trace") or [])
             if isinstance(row, Mapping)][:max(1, min(int(trace_limit), 5000))]
    trace_by_item = {str(row.get("item_id") or ""): row for row in trace}
    sections = _mapping(pack.get("sections"))
    budget = _mapping(pack.get("budget"))
    grounding = _mapping(pack.get("grounding"))
    gaps = [dict(gap) for gap in (pack.get("context_gaps") or []) if isinstance(gap, Mapping)]

    enriched = []
    for item in items:
        row = trace_by_item.get(str(item.get("item_id") or "")) or {}
        item_grounding = _mapping(item.get("grounding"))
        enriched.append({
            "item_id": str(item.get("item_id") or ""),
            "kind": str(item.get("kind") or ""),
            "section": str(item.get("section") or ""),
            "tokens": int(item.get("tokens") or 0),
            "source_stage": str(item.get("source_stage") or ""),
            "evidence_ref": str(item.get("evidence_ref") or ""),
            "claim_id": str(item.get("claim_id") or ""),
            "origin": _item_origin(str(item.get("section") or "")),
            "why_included": str(row.get("reason") or ""),
            # 包内**追加**的条目（例如 `budget` 段的额度承载项）在 Phase 08 的
            # selection trace 里没有决策行 —— 这个事实必须能被页面区分出来，
            # 否则"空白"会被误读成"没有理由"。
            "why_included_known": bool(row),
            "decision": str(row.get("decision") or ""),
            "reserved": bool(row.get("reserved")),
            "utility": row.get("utility"),
            "text": str(item.get("text") or ""),
            "grounded": bool(item_grounding.get("grounded")),
            "revalidated": bool(item_grounding.get("revalidated")),
            "requires_revalidation": bool(item_grounding.get("requires_revalidation")),
            "grounding_reason": str(item_grounding.get("reason") or ""),
        })
    dropped = []
    for row in trace:
        if str(row.get("decision") or "") == "included":
            continue
        dropped.append({
            "item_id": str(row.get("item_id") or ""), "section": str(row.get("section") or ""),
            "kind": str(row.get("kind") or ""), "tokens": int(row.get("tokens") or 0),
            "reason": str(row.get("reason") or ""), "utility": row.get("utility"),
            "budget_after": row.get("budget_after"), "detail": str(row.get("detail") or ""),
            "origin": _item_origin(str(row.get("section") or "")),
        })
    memory_items = [item for item in enriched if item["origin"] == "memory"]
    skill_items = [item for item in enriched if item["origin"] == "skill"]

    numbers = _numbers([
        ("pack_version", pack.get("pack_version"), SRC["P08"] + "#pack_version",
         "Phase 08 上下文包契约版本（`CONTEXT_PACK_VERSION`）。"),
        ("pack_id", pack.get("pack_id"), SRC["P08"] + "#pack_id",
         "内容寻址的包 id（同输入同 id，可跨轮比对）。"),
        ("items_selected", len(enriched), SRC["P08"] + "#items",
         "真的进了包的条目数（即 `items` 数组长度，不是候选数）。"),
        ("items_dropped", int(budget.get("trimmed_items") or 0), SRC["P08"] + "#budget.trimmed_items",
         "被预算挤出的条目数（Phase 08 的 `trim_reasons` 给出逐条原因）。"),
        ("tokens_used", budget.get("used"), SRC["P08"] + "#budget.used",
         "整包已用 token（估算器：%s）" % str(budget.get("estimator") or "见 estimator_note")),
        ("tokens_total_budget", budget.get("total"), SRC["P08"] + "#budget.total",
         "整包 token 预算（`QA_CONTEXT_TOKEN_BUDGET`，默认 6000）。"),
        ("tokens_before_trim", budget.get("estimated_tokens_before"),
         SRC["P08"] + "#budget.estimated_tokens_before", "裁剪前候选 token 总量。"),
        ("tokens_after_trim", budget.get("estimated_tokens_after"),
         SRC["P08"] + "#budget.estimated_tokens_after", "裁剪后整包 token 总量。"),
        ("duplicates_collapsed", budget.get("duplicates_collapsed"),
         SRC["P08"] + "#budget.duplicates_collapsed", "被折叠掉的重复条目数。"),
        ("counter_evidence_reserved", budget.get("reserved_counter_evidence"),
         SRC["P08"] + "#budget.reserved_counter_evidence",
         "为反证预留的 token 额度（P08-04：支持性证据不得挤掉它）。"),
        ("counter_evidence_used", budget.get("counter_evidence_used"),
         SRC["P08"] + "#budget.counter_evidence_used", "反证预留里真被用掉的额度。"),
        ("trim_reasons", _bucket(budget.get("trim_reasons")), SRC["P08"] + "#budget.trim_reasons",
         "裁剪原因的完整分布（每条被裁条目都有理由码）。"),
        ("from_evidence", len([item for item in enriched if item["origin"] == "evidence"]),
         SRC["P08"] + "#items[].section", "来自证据的条目数（§15「来自 Evidence 还是 Memory」）。"),
        ("from_memory", len(memory_items), SRC["P09"] + "#context_pack.sections.memory_context",
         "来自记忆的条目数；记忆条目恒 `is_evidence=false`（MASTER_RULES 11）。"),
        ("from_skill", len(skill_items), SRC["P11"] + "#context_pack.sections.skill_context",
         "来自技能指令的条目数（同样不是证据、不进 citation_map）。"),
        ("memory_requires_revalidation", len([item for item in memory_items
                                              if item["requires_revalidation"]]),
         SRC["P10"] + "#context_pack.items[].grounding.requires_revalidation",
         "需要复验的记忆条目数（Phase 10 的复验结论写回条目 grounding）。"),
        ("memory_revalidated", len([item for item in memory_items if item["revalidated"]]),
         SRC["P10"] + "#context_pack.items[].grounding.revalidated",
         "已通过复验的记忆条目数（只有 Phase 03 判 SUPPORTED 才会被置位）。"),
        ("grounded_items", grounding.get("grounded_items"), SRC["P02"] + "#context_pack.grounding",
         "有可回溯 span 的条目数（Phase 02 的证据对象口径）。"),
        ("ungrounded_items", grounding.get("ungrounded_items"),
         SRC["P02"] + "#context_pack.grounding", "没有可回溯 span 的条目数（不得当证据用）。"),
        ("citations", len(_mapping(pack.get("citation_map"))), SRC["P08"] + "#citation_map",
         "引用标签数（记忆/技能条目**不进**这张表）。"),
        ("traceable_citations", grounding.get("traceable_citations"),
         SRC["P08"] + "#grounding.traceable_citations", "可回溯到证据的引用数。"),
        ("untraceable_citations", grounding.get("untraceable_citations"),
         SRC["P08"] + "#grounding.untraceable_citations", "不可回溯的引用数（应当为 0）。"),
        ("context_gaps", len(gaps), SRC["P08"] + "#context_gaps", "上下文缺口总数。"),
        ("context_gaps_requiring_retrieval", len([gap for gap in gaps if gap.get("requires_retrieval")]),
         SRC["P08"] + "#context_gaps[].requires_retrieval",
         "会触发新检索的缺口数（默认 0：先重组已有上下文）。"),
        ("gap_actions", _bucket(gap.get("action") for gap in gaps),
         SRC["P08"] + "#context_gaps[].action", "缺口对应动作的分布（闭集合四值）。"),
        ("selection_trace_rows", len(trace), SRC["P08"] + "#selection_trace",
         "入选/被裁的逐条决策痕迹行数（上限 %d，超出部分不展示但计数仍可复算）。" % trace_limit),
        ("items_without_trace", len([item for item in enriched if not item["why_included_known"]]),
         SRC["P08"] + "#items vs #selection_trace",
         "包内**追加**（不经选择器）因而没有决策行的条目数 —— 空白的 `why_included` 就是这一批，"
         "页面必须据此区分“没有理由”和“选了但没记账”。"),
    ])
    return _view(
        "context", run_id=run_id, state="ok", reason="PRODUCT_PRESENT",
        state_text="上下文包正文与 selection trace 都来自 Phase 08 的阶段输出（免费持久化）。",
        source=pack_source, numbers=numbers,
        notes=["`why_included` 直接取 Phase 08 selection trace 的 `reason` 字段，本视图不另判；"
               "包内追加的条目没有决策行，靠 `why_included_known=false` 与原样空白区分。",
               "`origin` 由九段**段名**派生（memory_context→memory / skill_context→skill），"
               "不是二次判断。"],
        payload={
            "pack": {"pack_version": pack.get("pack_version"), "pack_id": pack.get("pack_id"),
                     "built_at": pack.get("built_at"), "utility_version": pack.get("utility_version"),
                     "task": _mapping(pack.get("task")), "gap_version": _mapping(pack.get("stats")).get("gap_version")},
            "sections": sections, "items": enriched, "dropped": dropped,
            "budget": budget, "grounding": grounding, "context_gaps": gaps,
            "selection_trace": trace, "citation_map": _mapping(pack.get("citation_map")),
            "detail_available": True,
        })


# ── P16-04：Memory View（记忆视图）───────────────────────────────────────────
def memory_view(store, run_id: str, *, limit: int = 200) -> dict:
    """§15 的 Memory View：类型 / 来源 / confidence / freshness / last_verified /
    reuse_count / status / superseded chain（+ Phase 10/15 的复验与作用域事实）。

    行一律来自 `memory_*` 表的**只读**仓储方法；本视图不写、不改、不重算衰减分。
    """
    cap = max(1, min(int(limit or 200), 2000))
    errors: list = []
    stats: dict = {}
    try:
        stats = _mapping(store.memory_stats(include_all_scopes=True))
    except Exception as exc:  # noqa: BLE001
        stats = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}
    if stats.get("error"):
        errors.append("memory_stats")

    items: list = []
    try:
        items = store.load_memory_items(include_all_scopes=True, limit=cap)
    except Exception:  # noqa: BLE001
        errors.append("load_memory_items")
    recalls = store.observability_memory_recalls(run_id=run_id, limit=200) \
        if hasattr(store, "observability_memory_recalls") else []
    validations: list = []
    try:
        validations = store.memory_validations(run_id=run_id, limit=200)
    except Exception:  # noqa: BLE001
        errors.append("memory_validations")
    contradictions: list = []
    try:
        contradictions = store.memory_contradictions(run_id=run_id, limit=200)
    except Exception:  # noqa: BLE001
        errors.append("memory_contradictions")
    decisions: list = []
    try:
        decisions = store.memory_write_decisions(run_id=run_id, limit=200)
    except Exception:  # noqa: BLE001
        errors.append("memory_write_decisions")

    # 本 run 到底创建了哪些记忆：**以写门决策行为准**（`memory_write_decision` 记了
    # `run_id` + `memory_id`）。刻意不只信 `memory_item.created_from_run_id` —— 该列由
    # 写门从**候选对象**上取，Phase 09 的管线入口并不保证候选带 `run_id`，于是它可能
    # 为空串；两个口径分开报，谁都不冒充谁。
    decisions = [row for row in decisions if isinstance(row, Mapping)]
    run_created_ids = {str(row.get("memory_id") or "") for row in decisions
                       if str(row.get("memory_id") or "")}
    tagged_ids = {str(item.get("memory_id") or "") for item in items
                  if str(item.get("created_from_run_id") or "") == str(run_id)}
    run_items = [item for item in items if str(item.get("memory_id") or "") in run_created_ids]
    links = []
    if items:
        try:
            links = store.memory_evidence([str(item.get("memory_id") or "") for item in items][:200])
        except Exception:  # noqa: BLE001
            errors.append("memory_evidence")
    link_by_memory: dict = {}
    for row in links or ():
        if not isinstance(row, Mapping):
            continue
        link_by_memory.setdefault(str(row.get("memory_id") or ""), []).append({
            "evidence_ref": str(row.get("evidence_ref") or ""),
            "source_fingerprint": str(row.get("source_fingerprint") or ""),
            "span_fingerprint": str(row.get("span_fingerprint") or ""),
            "run_id": str(row.get("run_id") or ""), "stage": str(row.get("stage") or ""),
            "route": str(row.get("route") or ""), "verdict": str(row.get("verdict") or ""),
        })
    relations = []
    if items:
        try:
            relations = store.memory_relations([str(item.get("memory_id") or "") for item in items][:200])
        except Exception:  # noqa: BLE001
            errors.append("memory_relations")

    if stats.get("error") and not items:
        state, reason = "unavailable", "STORE_UNAVAILABLE"
        state_text = "记忆读侧不可用：%s" % str(stats.get("error"))
    elif stats.get("error") or errors:
        state, reason = "degraded", "PARTIAL_READ"
        state_text = ("记忆读侧有 %d 路失败（%s），其余字段仍是真实读数。"
                      % (len(set(errors)), "、".join(sorted(set(errors)))))
    elif not items:
        state, reason = "empty", "NO_MEMORY_IN_SCOPE"
        state_text = ("库里一条记忆都没有（或全部被作用域隔离）。记忆是 Research Accelerator "
                      "不是 Truth Oracle（MASTER_RULES 12），没有记忆是完全正常的状态。")
    else:
        state, reason = "ok", "PRODUCT_PRESENT"
        state_text = "记忆行来自 Phase 09~15 落盘的 `memory_*` 表（只读仓储读出，不重算衰减）。"

    chain = []
    if run_id and run_items:
        try:
            chain = store.observability_supersession_chain(str(run_items[0].get("memory_id") or ""))
        except Exception:  # noqa: BLE001
            errors.append("supersession_chain")

    numbers = _numbers([
        ("items_total", stats.get("items"), SRC["P09"] + "#memory_stats.items",
         "记忆总条数（`memory_stats` 的只读统计，跨作用域）。"),
        ("items_created_by_this_run", len(run_created_ids),
         SRC["P09"] + "#memory_write_decision.run_id/memory_id",
         "本 run 写进门（Write Gate）并落库的记忆条数 —— 以**写门决策行**为准"
         "（`memory_write_decision` 同时记了 run 与 memory）。"),
        ("items_tagged_with_run_id", len(tagged_ids), SRC["P09"] + "#memory_item.created_from_run_id",
         "`memory_item.created_from_run_id` 等于本 run 的条数（该列由写门从候选对象上取，"
         "管线入口不保证候选带 run_id，所以它可能少于上一项 —— 两个口径分列，不互相顶替）。"),
        ("run_memory_ids", sorted(run_created_ids), SRC["P09"] + "#memory_write_decision.memory_id",
         "本 run 创建的记忆 id 清单（取代链与逐条溯源都从这里进）。"),
        ("by_type", _bucket(stats.get("by_type")), SRC["P09"] + "#memory_stats.by_type",
         "十类记忆的分布（类型闭集合见 `MEMORY_TYPES`）。"),
        ("by_status", _bucket(stats.get("by_status")), SRC["P09"] + "#memory_stats.by_status",
         "状态分布（ACTIVE/STALE/EXPIRED/... 六值）。"),
        ("by_freshness", _bucket(stats.get("by_freshness")), SRC["P09"] + "#memory_stats.by_freshness",
         "七档时效分布（判定规则来自 `qa_memory` 的时效档表）。"),
        ("by_scope", _bucket(stats.get("by_scope")), SRC["P15"] + "#memory_stats.by_scope",
         "五个作用域的分布（P15-01 强制后的真实落点）。"),
        ("evidence_links", _mapping(stats.get("links")).get("evidence"),
         SRC["P09"] + "#memory_stats.links.evidence", "记忆↔证据绑定行数（provenance 的载体）。"),
        ("entity_links", _mapping(stats.get("links")).get("entity"),
         SRC["P09"] + "#memory_stats.links.entity", "记忆↔实体绑定行数（图通道的载体）。"),
        ("relation_rows", _mapping(stats.get("links")).get("relation"),
         SRC["P09"] + "#memory_stats.links.relation", "记忆图关系行数（含 SUPERSEDES/CONTRADICTS）。"),
        ("write_decisions", _bucket(stats.get("decisions")), SRC["P09"] + "#memory_stats.decisions",
         "写门四出口的分布（落库/仅会话/合并/丢弃）。"),
        ("recall_rows", _mapping(stats.get("recalls")).get("rows"),
         SRC["P09"] + "#memory_recall_log", "召回留痕行数（每次召回一行）。"),
        ("recall_hits", _mapping(stats.get("recalls")).get("hits"),
         SRC["P09"] + "#memory_recall_log.hits", "召回到的命中总数。"),
        ("run_recall_rows", len(recalls), SRC["P09"] + "#memory_recall_log.run_id",
         "**本 run** 的召回留痕行数。"),
        ("run_recall_hits", sum(int(row.get("hits") or 0) for row in recalls),
         SRC["P09"] + "#memory_recall_log.hits", "本 run 召回到的命中条数（回执原值求和）。"),
        ("run_validations", len(validations), SRC["P10"] + "#memory_validation.run_id",
         "本 run 的记忆复验行数（Phase 10·P10-03）。"),
        ("run_revalidated", len([row for row in validations
                                 if bool(row.get("verified")) or bool(row.get("promoted"))]),
         SRC["P10"] + "#memory_validation.verified/promoted",
         "本 run 复验成功（判 SUPPORTED）并被提升的记忆条数；**不是**记忆正文自证。"),
        ("run_contradictions", len(contradictions), SRC["P10"] + "#memory_contradiction.run_id",
         "本 run 检测到的记忆矛盾数（裁决复用 Phase 06 的规则裁决器）。"),
        ("run_write_decisions", len(decisions), SRC["P09"] + "#memory_write_decision.run_id",
         "本 run 的写门决策行数（逐条留痕，可审计）。"),
        ("superseded_items", len([item for item in items if str(item.get("superseded_by") or "")]),
         SRC["P10"] + "#memory_item.superseded_by",
         "已有后继的记忆条数（存在 `superseded_by` 即被取代，历史不物理覆盖）。"),
        ("supersession_chain_hops", len(chain), SRC["P10"] + "#memory_relation(SUPERSEDES)",
         "取代链跳数（沿 SUPERSEDES 边走 + `superseded_by` 兜底，带去环）。"),
        ("read_errors", len(errors), SRC["P09"] + "#read_errors",
         "本次读侧失败的路数（>0 时状态判 degraded，读不到的字段留空而**不补 0**）。"),
    ])
    return _view(
        "memory", run_id=run_id, state=state, reason=reason, state_text=state_text,
        source=SRC["P09"], numbers=numbers,
        notes=["`confidence` / `decay_score` / `freshness_class` 都是**当时写入/维护的值**，"
               "本视图不重算衰减（重算属 Phase 09 的生命周期维护）。",
               "记忆正文只作为 `canonical_content` 展示；MASTER_RULES 11 规定它永远不是证据。"],
        payload={
            "items": [{
                "memory_id": str(item.get("memory_id") or ""),
                "memory_type": str(item.get("memory_type") or ""),
                "canonical_content": str(item.get("canonical_content") or "")[:500],
                "confidence": item.get("confidence"),
                "freshness_class": str(item.get("freshness_class") or ""),
                "decay_score": item.get("decay_score"),
                "status": str(item.get("status") or ""),
                "scope": str(item.get("scope") or ""),
                "scope_key": str(item.get("scope_key") or ""),
                "last_verified_at": item.get("last_verified_at"),
                "valid_from": item.get("valid_from"), "valid_until": item.get("valid_until"),
                "reuse_count": int(item.get("reuse_count") or 0),
                "recall_count": int(item.get("recall_count") or 0),
                "version": int(item.get("version") or 1),
                "superseded_by": str(item.get("superseded_by") or ""),
                "created_from_run_id": str(item.get("created_from_run_id") or ""),
                "created_from_session_id": str(item.get("created_from_session_id") or ""),
                "sources": link_by_memory.get(str(item.get("memory_id") or ""), [])[:10],
                "origin": "memory_persisted",
            } for item in items],
            "run_items": [str(item.get("memory_id") or "") for item in run_items],
            "run_created_ids": sorted(run_created_ids),
            "run_tagged_ids": sorted(tagged_ids),
            "recalls": recalls[:50], "validations": validations[:50],
            "contradictions": contradictions[:50], "decisions": decisions[:50],
            "relations": [{
                "memory_id": str(row.get("memory_id") or ""),
                "relation": str(row.get("relation") or ""),
                "target_memory_id": str(row.get("target_memory_id") or ""),
                "target_ref": str(row.get("target_ref") or ""),
                "rationale": str(row.get("rationale") or ""),
            } for row in relations[:200]],
            "supersession_chain": chain,
            "stats": stats,
            "read_errors": sorted(set(errors)),
        })


# ── P16-05：Search Trace（检索轨迹 + §15 追加的六个字段）──────────────────────
def search_trace(store, run_id: str, *, stages: Mapping | None = None) -> dict:
    """§23 的 Search Trace：逐动作可解释 + §15 追加的六个计数字段。

    轨迹行取 `qa_reasoning_traces`（Phase 01 建、Phase 07 填 `gap_id`/`new_claims`/`resolved_gap`）。
    §15 追加的六个字段**本来就由各阶段自己记**（记忆召回/复验、上下文包、技能加载），
    本视图把它们归到轨迹上并逐个标出来源 —— **不在 UI 层另算一套**。
    """
    stages = stages if isinstance(stages, Mapping) else stage_outputs(store, run_id)
    rows: list = []
    trace_error = ""
    try:
        rows = store.reasoning_traces(str(run_id)) or []
    except Exception as exc:  # noqa: BLE001
        trace_error = "%s: %s" % (type(exc).__name__, str(exc)[:120])
    gap_loop = {}
    for stage in ("level1_retrieval", "level2_retrieval"):
        candidate = _mapping(_stats_of(stages, stage).get("gap_loop"))
        if candidate:
            gap_loop = candidate
            break
    stop_reason = str(gap_loop.get("stop_reason") or "")

    pack, pack_source = context_pack_of(store, run_id, stages)
    receipt = (_mapping(pack.get("receipt_only")) or _mapping(pack.get("receipt"))) \
        if pack.get("receipt_only") else {}
    budget = _mapping(receipt.get("budget")) if receipt else _mapping(pack.get("budget"))
    stats = _mapping(pack.get("stats"))
    sections = _mapping(pack.get("sections"))
    skill_section = _mapping(sections.get("skill_context"))
    memory_section = _mapping(sections.get("memory_context"))

    recalls = store.observability_memory_recalls(run_id=run_id, limit=500) \
        if hasattr(store, "observability_memory_recalls") else []
    validations: list = []
    try:
        validations = store.memory_validations(run_id=run_id, limit=500)
    except Exception:  # noqa: BLE001
        validations = []
    skills_loaded = [str(name) for name in (skill_section.get("loaded_skills") or []) if str(name or "")]

    has_pack = bool(pack)
    context_items_selected = (len(list(pack.get("items") or [])) if has_pack else None)
    if receipt:
        context_items_selected = receipt.get("items")
    context_items_dropped = budget.get("trimmed_items") if has_pack else None
    context_tokens = budget.get("used") if has_pack else None
    skill_rows = store.skill_load_rows(run_id=run_id, limit=200) if hasattr(store, "skill_load_rows") else []

    additions = _numbers([
        ("memory_recalled", sum(int(row.get("hits") or 0) for row in recalls),
         SRC["P09"] + "#memory_recall_log.hits",
         "§15 追加字段：本 run 召回到的记忆条数（Phase 09·P09-04 的召回留痕，原值求和；"
         "没有召回留痕时是 0 而不是 null —— “召回了 0 条”与“没跑召回”在 memory_recalled 上"
         "由 `memory_recall_log` 的行数区分，页面同时展示 `run_recall_rows`）。"),
        ("memory_revalidated", len([row for row in validations
                                    if bool(row.get("verified")) or bool(row.get("promoted"))]),
         SRC["P10"] + "#memory_validation.verified/promoted",
         "§15 追加字段：本 run 复验成功的记忆条数（判定只有 Phase 03 的核验，零新增检索）。"),
        ("context_items_selected", context_items_selected,
         pack_source + "#items" if pack_source else SRC["P08"] + "#items",
         "§15 追加字段：进包的上下文条目数（Phase 08 的口径）；没有上下文包产物时是 null。"),
        ("context_items_dropped", context_items_dropped, pack_source + "#budget.trimmed_items"
         if pack_source else SRC["P08"] + "#budget.trimmed_items",
         "§15 追加字段：被预算挤出的上下文条目数（Phase 08 的 `trim_reasons` 给逐条原因）；"
         "没有上下文包产物时是 null。"),
        ("context_tokens", context_tokens, pack_source + "#budget.used"
         if pack_source else SRC["P08"] + "#budget.used",
         "§15 追加字段：整包已用 token（Phase 08 的确定性估算器口径）；无产物时是 null。"),
        ("skills_loaded", len(skills_loaded), SRC["P11"] + "#context_pack.sections.skill_context",
         "§15 追加字段：真正被加载进上下文的技能个数（Phase 11·P11-05 的按需指令）；"
         "没有上下文包时技能指令无处可加载，因此是 0 而不是推测值。"),
    ])
    numbers = dict(additions)
    numbers.update(_numbers([
        ("trace_rows", len(rows), SRC["P01"] + "#qa_reasoning_traces",
         "本 run 的检索轨迹行数（每跳/每动作一行，Phase 01 建表、Phase 07 补三列）。"),
        ("rounds", len({int(row.get("round_index") or 0) for row in rows if isinstance(row, Mapping)}),
         SRC["P01"] + "#round_index", "轨迹里出现过的轮次数（去重后的真实轮次）。"),
        ("stop_reason", stop_reason, SRC["P07"] + "#stats.gap_loop.stop_reason",
         "Phase 07·P07-06 的唯一停止决策点给出的停止原因（五值闭集合）。"),
        ("gap_loop_rounds", len(list(gap_loop.get("rounds") or [])),
         SRC["P07"] + "#stats.gap_loop.rounds", "缺口闭环真的跑过的轮数。"),
        ("new_verified_claims", gap_loop.get("new_verified_claims"),
         SRC["P07"] + "#stats.gap_loop.new_verified_claims",
         "本轮新增的**已核验**结论数（收敛判据的分子之一）。"),
        ("resolved_high_priority_gaps", gap_loop.get("resolved_high_priority_gaps"),
         SRC["P07"] + "#stats.gap_loop.resolved_high_priority_gaps",
         "本轮被消解的高优缺口数（收敛判据的另一半）。"),
        ("accepted_total", sum(int(row.get("accepted") or 0) for row in rows
                               if isinstance(row, Mapping)),
         SRC["P01"] + "#qa_reasoning_traces.accepted", "全部轨迹行的采纳证据数之和。"),
        ("rejected_total", sum(int(row.get("rejected") or 0) for row in rows
                               if isinstance(row, Mapping)),
         SRC["P01"] + "#qa_reasoning_traces.rejected",
         "全部轨迹行的拒绝证据数之和（被拒证据仍属 seen，不会被重复检索）。"),
        ("results_total", sum(int(row.get("results") or 0) for row in rows
                              if isinstance(row, Mapping)),
         SRC["P01"] + "#qa_reasoning_traces.results", "全部轨迹行的候选命中数之和。"),
        ("skill_telemetry_rows", len(skill_rows), SRC["P11"] + "#qa_stage_runs(stage=skill:<id>)",
         "Phase 11·P11-06 落在既有 `qa_stage_runs` 上的技能遥测行数。"),
        ("run_recall_rows", len(recalls), SRC["P09"] + "#memory_recall_log.run_id",
         "本 run 的记忆召回留痕行数 —— 用来把“召回了 0 条”与“根本没跑召回”分开"
         "（`memory_recalled` 是命中数之和，行数为 0 时它必然是 0）。"),
        ("run_validations", len(validations), SRC["P10"] + "#memory_validation.run_id",
         "本 run 的记忆复验行数（同上：区分“复验 0 条”与“没跑复验”）。"),
        ("memory_context_supplied", stats.get("memory_supplied") or memory_section.get("supplied"),
         SRC["P09"] + "#context_pack.stats.memory_supplied",
         "交给上下文包的记忆候选数（其中被裁掉多少见 memory_context.trimmed）。"),
        ("skill_context_supplied", stats.get("skill_supplied") or skill_section.get("supplied_skills"),
         SRC["P11"] + "#context_pack.stats.skill_supplied", "交给上下文包的技能候选数。"),
    ]))

    if trace_error:
        state, reason = "degraded", "PARTIAL_READ"
        state_text = "轨迹表读侧失败（%s）；§15 追加的六个字段仍来自各自阶段的产物。" % trace_error
    elif not rows:
        state, reason = "empty", "NO_STAGE_ARTIFACT"
        state_text = ("本 run 没有检索轨迹行：多跳/缺口闭环没有跑到（单跳快速路径），或该 run 是"
                      "老数据。§15 追加的六个字段照常给出，出处逐个标明。")
    else:
        state, reason = "ok", "PRODUCT_PRESENT"
        state_text = "轨迹行来自 Phase 01 的 `qa_reasoning_traces`，缺口字段由 Phase 07 回填。"

    actions = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        resolved = bool(row.get("resolved_gap"))
        actions.append({
            "round": int(row.get("round_index") or 0),
            "hop_index": int(row.get("hop_index") or 0),
            "why_search": str(row.get("sub_query") or row.get("partial_answer") or "")[:300],
            "gap_id": str(row.get("gap_id") or ""),
            "query": str(row.get("sub_query") or ""),
            "route": str(row.get("route") or ""),
            "results": int(row.get("results") or 0),
            "accepted": int(row.get("accepted") or 0),
            "rejected": int(row.get("rejected") or 0),
            "new_claims": int(row.get("new_claims") or 0),
            "resolved_gap": resolved,
            "resolved_gap_text": ("消解了缺口 %s" % row.get("gap_id")) if resolved else "未消解缺口",
            "continue_reason": ("" if resolved else "缺口仍在：本轮没有把它消解掉，因此继续下一跳。"),
            "stop_reason": stop_reason,
            "latency_ms": int(row.get("latency_ms") or 0),
            "status": str(row.get("status") or ""),
            "sub_query_id": str(row.get("sub_query_id") or ""),
            # JSON 列在 `qa_reasoning_traces` 里是 `*_json` 文本列（该读方法不解码），
            # 这里只做 JSON 解码，不改语义。
            "depends_on": _json_list(row.get("depends_on_json")),
            "used_evidence_refs": _json_list(row.get("used_evidence_refs_json"))[:20],
            "missing_links": _json_list(row.get("missing_links_json"))[:20],
            "next_queries": _json_list(row.get("next_queries_json"))[:10],
            "created_at": str(row.get("created_at") or ""),
        })
    return _view(
        "search_trace", run_id=run_id, state=state, reason=reason, state_text=state_text,
        source=SRC["P01"], numbers=numbers,
        notes=["§15 追加的六个字段是**各阶段自己记的数**，本视图只做归集与出处标注。",
               "九个 UI 问题（为什么搜/搜了什么/走哪个 route/找到了什么/为什么接受/为什么拒绝/"
               "解决了哪个 Gap/为什么继续/为什么停止）逐行给出对应字段。"],
        payload={
            "actions": actions, "questions": [{"key": key, "text": text}
                                               for key, text in SEARCH_TRACE_QUESTIONS],
            "additions": {key: additions[key]["value"] for key in SEARCH_TRACE_ADDITIONS},
            "additions_meta": {key: additions[key] for key in SEARCH_TRACE_ADDITIONS},
            "gap_loop": gap_loop, "stop_reason": stop_reason,
            "skill_telemetry": [{
                "stage": str(row.get("stage") or ""), "skill_id": str(row.get("skill_id") or ""),
                "stage_reached": str(row.get("stage_reached") or ""),
                "outcome": str(row.get("outcome") or ""), "status": str(row.get("status") or ""),
                "latency_ms": row.get("latency_ms"), "cost_units": row.get("cost_units"),
                "evidence_yield": row.get("evidence_yield"),
                "verified_yield": row.get("verified_yield"),
                "task_type": str(row.get("task_type") or ""),
                "run_id": str(row.get("run_id") or ""),
            } for row in skill_rows[:50]],
            "skills_loaded": skills_loaded,
            "read_error": trace_error,
        })


# ── P16-06：cost / latency / token / cache / skill dashboard ─────────────────
def dashboard(store, *, industry_pack_id: str = "", metrics=None, limit: int = 2000) -> dict:
    """§32 的看板：成本 / 延迟 / token / 缓存 / 技能。

    聚合口径**一律复用**：run 与阶段延迟取既有 `QaMetricsService.snapshot`（不重算分位数）、
    token 取 `qa_stage_runs.token_usage_json` 的加总、缓存取 `qa_retrieval_cache` 的分组、
    技能取 `qa_skills` 的既有 `performance_table`（**成功率口径零重定义**）。
    """
    errors: list = []
    snapshot = {}
    try:
        if metrics is None:
            from qa_observability import QaMetricsService

            metrics = QaMetricsService(store.database)
        snapshot = _mapping(metrics.snapshot(industry_pack_id=industry_pack_id, limit=limit))
    except Exception as exc:  # noqa: BLE001
        errors.append("metrics")
        snapshot = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}

    tokens = store.observability_token_usage(industry_pack_id=industry_pack_id, limit=limit) \
        if hasattr(store, "observability_token_usage") else {"error": "no_reader"}
    tokens = _mapping(tokens)
    if tokens.get("error"):
        errors.append("tokens")
    cache = store.observability_cache_stats(limit=limit) if hasattr(store, "observability_cache_stats") \
        else {"error": "no_reader"}
    cache = _mapping(cache)
    if cache.get("error"):
        errors.append("cache")
    skill_rows = store.skill_load_rows(limit=limit) if hasattr(store, "skill_load_rows") else []
    if not isinstance(skill_rows, list):
        skill_rows = []

    performance = {}
    try:
        records = [row for row in skill_rows if isinstance(row, Mapping) and row.get("skill_id")]
        if records:
            from qa_skills import performance_table

            performance = _mapping(performance_table(records))
    except Exception:  # noqa: BLE001
        errors.append("skill_performance")

    stages = _mapping(snapshot.get("stages"))
    latency_samples = [int(_mapping(row).get("p95_ms") or 0) for row in stages.values()]
    runs = int(snapshot.get("runs") or 0)
    if snapshot.get("error") and not stages:
        state, reason = "unavailable", "STORE_UNAVAILABLE"
        state_text = "看板读侧不可用：%s" % str(snapshot.get("error"))
    elif not runs:
        state, reason = "empty", "NO_RUN_IN_SCOPE"
        state_text = ("作用域内一条 QA run 都没有 —— 这是全新部署的真实状态。看板的每一格都"
                      "应当显示“暂无数据”而不是 0。")
    elif errors:
        state, reason = "degraded", "PARTIAL_READ"
        state_text = "有 %d 路读侧失败（%s），其余数字仍是真实读数。" % (len(errors), "、".join(sorted(set(errors))))
    else:
        state, reason = "ok", "PRODUCT_PRESENT"
        state_text = "各项数字分别来自既有指标服务与各阶段自己的落点，口径逐个标注。"

    token_by_key = _mapping(tokens.get("by_key"))
    numbers = _numbers([
        ("runs", snapshot.get("runs"), SRC["P00"] + "#QaMetricsService.snapshot.runs",
         "作用域内最近 N 次 run 的条数（既有 `QaMetricsService` 的口径，不重算）。"),
        ("by_status", _bucket(snapshot.get("by_status")), SRC["P00"] + "#snapshot.by_status",
         "run 状态分布（queued/running/completed/failed/cancelled/retry_wait）。"),
        ("by_origin", _bucket(snapshot.get("by_origin")), SRC["P00"] + "#snapshot.by_origin",
         "入口来源分布（getinfo_ui / ragflow_ui / api）。"),
        ("degraded_runs", snapshot.get("degraded"), SRC["P00"] + "#snapshot.degraded",
         "带降级标记的 run 数（降级原因逐条存在 `qa_runs.degradation_json`）。"),
        ("stage_p50_ms", {name: _mapping(row).get("p50_ms") for name, row in stages.items()},
         SRC["P00"] + "#snapshot.stages.p50_ms", "各阶段延迟 p50（既有分位数实现，不重写）。"),
        ("stage_p95_ms", {name: _mapping(row).get("p95_ms") for name, row in stages.items()},
         SRC["P00"] + "#snapshot.stages.p95_ms", "各阶段延迟 p95（同上）。"),
        ("stage_failures", {name: _mapping(row).get("failures") for name, row in stages.items()},
         SRC["P00"] + "#snapshot.stages.failures", "各阶段失败次数。"),
        ("worst_stage_p95_ms", max(latency_samples) if latency_samples else None,
         SRC["P00"] + "#snapshot.stages.p95_ms", "最慢阶段的 p95（用来一眼看瓶颈）。"),
        ("error_codes", _bucket(snapshot.get("error_codes")), SRC["P00"] + "#snapshot.error_codes",
         "阶段错误码分布（闭集合来自 `qa_errors`）。"),
        ("retrieval_evidence_adopted", snapshot.get("retrieval_evidence_adopted"),
         SRC["P02"] + "#snapshot.retrieval_evidence_adopted",
         "检索阶段采纳的证据数（既有指标服务从阶段输出里取的 `stats.adopted`）。"),
        ("token_rows", tokens.get("rows"), SRC["P00"] + "#qa_stage_runs.token_usage_json",
         "带 token 用量的阶段行数（Phase 00·P00-04 的落点）。"),
        ("tokens_in", tokens.get("tokens_in"), SRC["P00"] + "#token_usage_json.tokens_in",
         "输入 token 加总（**记录里真的有这个键**才报数，没有就是 None）。"),
        ("tokens_out", tokens.get("tokens_out"), SRC["P00"] + "#token_usage_json.tokens_out",
         "输出 token 加总（同上的诚实口径）。"),
        ("tokens_total", tokens.get("tokens_total"), SRC["P00"] + "#token_usage_json.tokens_total",
         "total 取记录里的键值；**不用 in+out 计算**（不同端点的 total 口径可能含缓存命中）。"),
        ("tokens_by_key", _bucket(token_by_key), SRC["P00"] + "#token_usage_json",
         "所见 token 键的原样求和（新端点新增键会自动出现，不需要改代码）。"),
        ("tokens_by_stage", {name: _bucket(value) for name, value in _mapping(tokens.get("by_stage")).items()},
         SRC["P00"] + "#qa_stage_runs.token_usage_json", "按阶段分组的 token 用量。"),
        ("cache_entries", cache.get("entries"), SRC["P04"] + "#qa_retrieval_cache",
         "缓存条目数（检索/核验共用一张表的既有设计）。"),
        ("cache_by_namespace", _bucket({name: _mapping(row).get("entries")
                                        for name, row in _mapping(cache.get("by_namespace")).items()}),
         SRC["P04"] + "#qa_retrieval_cache.namespace",
         "按 namespace 分组（既有代码用到 level1_retrieval / level2_retrieval / qa_verification）。"),
        ("cache_expired", cache.get("expired"), SRC["P04"] + "#qa_retrieval_cache.expires_at",
         "已过期条目数（字符串比较，与 `cache_get` 的 `expires_at>?` 判据同一口径）。"),
        ("cache_live", cache.get("live"), SRC["P04"] + "#qa_retrieval_cache.expires_at",
         "未过期条目数（entries - expired）。"),
        ("skill_telemetry_rows", len(skill_rows), SRC["P11"] + "#qa_stage_runs(stage=skill:<id>)",
         "技能遥测行数（Phase 11·P11-06 落在既有表上，零迁移）。"),
        ("skills_measured", len(performance), SRC["P12"] + "#skill_performance_memory",
         "有遥测样本的技能个数（成功率表来自 `qa_skills.performance_table`，口径零重定义）。"),
        ("skill_success_rate", {name: _mapping(row).get("success_rate")
                                for name, row in performance.items()},
         SRC["P11"] + "#qa_skills.performance_summary.success_rate",
         "各技能成功率（样本不足时为空值 + 不足样本理由码，绝不冒充 0）。"),
        ("skill_sample_reason", {name: _mapping(row).get("reason")
                                 for name, row in performance.items()},
         SRC["P11"] + "#qa_skills.performance_summary.reason",
         "成功率为什么是空值：`INSUFFICIENT_SAMPLES`（样本 < 门限）/ `EMPTY_RECORDS`；"
         "非空表示这次真的算出了比率。"),
        ("skill_attempts", {name: _mapping(row).get("attempts") for name, row in performance.items()},
         SRC["P11"] + "#qa_skills.performance_summary.attempts",
         "各技能的遥测样本条数（失败与跳过都算，不许只统计成功的）。"),
        ("skill_latency_ms", {name: _mapping(_mapping(row).get("latency_ms")).get("p90")
                              for name, row in performance.items()},
         SRC["P11"] + "#qa_skills.performance_summary.latency_ms.p90",
         "各技能申报延迟 p90（最近秩法，不插值；只统计真实观测到的值）。"),
        ("skill_cost_units", {name: _mapping(row).get("cost_units")
                              for name, row in performance.items()},
         SRC["P11"] + "#qa_skills.performance_summary.cost_units",
         "各技能累计申报成本（成本档口径来自技能注册表）。"),
        ("skill_success_definition", {name: _mapping(row).get("success_definition")
                                      for name, row in performance.items()},
         SRC["P11"] + "#qa_skills.performance_summary.success_definition",
         "成功口径（逐字来自 Phase 11，本看板不重定义）。"),
        ("stage_count", len(stages), SRC["P00"] + "#snapshot.stages",
         "有观测的阶段种类数。"),
    ])
    return _view(
        "dashboard", run_id="*", state=state, reason=reason, state_text=state_text,
        source=SRC["P00"], numbers=numbers,
        notes=["本视图是**全局**看板（不带 run_id，`run_id=\"*\"` 表示“跨 run 的作用域”），"
               "run 级视图请用其余五个。",
               "延迟与 run 维度复用既有 `QaMetricsService.snapshot`，不重算分位数。"],
        payload={"scope": {"industry_pack_id": str(industry_pack_id or ""), "limit": int(limit)},
                 "metrics": snapshot, "tokens": tokens, "cache": cache,
                 "skill_performance": performance,
                 "skill_rows": [{"stage": str(row.get("stage") or ""),
                                 "skill_id": str(row.get("skill_id") or ""),
                                 "run_id": str(row.get("run_id") or ""),
                                 "status": str(row.get("status") or ""),
                                 "outcome": str(row.get("outcome") or ""),
                                 "stage_reached": str(row.get("stage_reached") or ""),
                                 "latency_ms": row.get("latency_ms"),
                                 "wall_latency_ms": row.get("wall_latency_ms")}
                                for row in skill_rows[:100]],
                 "read_errors": sorted(set(errors))})


# ── 汇总入口 ────────────────────────────────────────────────────────────────
RUN_VIEWS = ("execution", "evidence", "context", "memory", "search_trace")


def run_views(store, run_id: str, *, evidence_limit: int = 20, memory_limit: int = 200) -> dict:
    """五个 run 级视图一次给全（页面一次请求即可渲染；每个视图仍是同一份只读投影）。"""
    stages = stage_outputs(store, run_id)
    return {
        "view_version": OBSERVABILITY_UI_VERSION,
        "run_id": str(run_id or ""),
        "read_only": True,
        "generated_at": _now(),
        "views": {
            "execution": execution_view(store, run_id, stages=stages),
            "evidence": evidence_view(store, run_id, limit=evidence_limit),
            "context": context_inspector(store, run_id, stages=stages),
            "memory": memory_view(store, run_id, limit=memory_limit),
            "search_trace": search_trace(store, run_id, stages=stages),
        },
    }


VIEW_BUILDERS = {
    "execution": execution_view,
    "evidence": evidence_view,
    "context": context_inspector,
    "memory": memory_view,
    "search_trace": search_trace,
}


def build_view(store, run_id: str, view: str, **kwargs) -> dict:
    """按视图名分派（未知视图名抛 `KeyError`，由 API 层翻译成 400）。"""
    name = str(view or "")
    if name == "dashboard":
        return dashboard(store, **kwargs)
    return VIEW_BUILDERS[name](store, run_id, **kwargs)


__all__ = [
    "RUN_VIEWS", "SEARCH_TRACE_ADDITIONS", "SEARCH_TRACE_QUESTIONS", "SRC", "VIEW_BUILDERS",
    "build_view", "context_inspector", "context_pack_of", "dashboard", "evidence_view",
    "execution_view", "memory_view", "run_views", "search_trace", "stage_outputs",
]
