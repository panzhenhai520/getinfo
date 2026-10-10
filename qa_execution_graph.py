#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 05（P05-02…P05-05）· Research Planner 与 Execution Graph。

通用包 01_V2_ARCHITECTURE：
  · §7 Research Planner："Planner 不负责回答；把问题拆为 SubQuestion / Claim /
    Evidence Requirement / Dependency"，输出 `sub_questions/dependencies/parallel_groups/
    required_evidence_types`；
  · §2.1 Node 是任务、Edge 是真实依赖（"B 不读 A 的结果就不许写成 A→B"）；§2.2 每个 Node 必须有
    Contract；§2.3 Edge 也是数据 Contract；§2.4 独立任务必须 Fan-out；§2.5 Barrier 只在真正需要
    全局结果时出现；§28 每个节点声明失败策略（五值）；
  · §18 Fast / Standard / Deep 三条路径；§3.1 Execution Graph（谁执行/能否并行/何时汇合/
    失败怎么办/何时停）。

**本模块一行检索/验证逻辑都不重写**，只做"把既有零件接成执行图"：
  复用 ① `qa_query_decompose.decompose()` + `validate_dag()` —— 子问题 DAG（含环/自环/顺序校验）；
  复用 ② `qa_planner._retrieval_strategy()` —— "需要哪类证据"（官方原文/官方解读/专业材料/跳链/裁决）；
  复用 ③ `qa_planner._MULTI_HOP_CATEGORIES` / `qa_query_interpreter` —— 意图与复杂度；
  复用 ④ `qa_policy.QaPolicy` —— 三档跳数与运行时长上限（`research_timeout_seconds`）；
  复用 ⑤ `qa_resilience.STAGE_BUDGET_SECONDS` —— 既有阶段预算（节点预算的来源）；
  复用 ⑥ `qa_hunter_fleet` —— 舰队的并发度/单 Hunter 超时/重试/总预算（Hunter 节点的预算就是它）；
  复用 ⑦ `qa_graph_contracts` —— 失败策略五值、停止原因五值、Node/图 schema；
  复用 ⑧ `qa_storage.record_stage()` 的 node_id/node_kind/parent_node_id/round_index 列（P01-04）。
新增：§18 三条路径的节点编排、每节点契约与失败策略声明、依赖层级→并行组、关键路径预算估算、
超预算可观测裁剪与停止原因、运行期账本（ExecutionLedger）、节点行落库。

诚实边界（宁写 PARTIAL 不谎报）
--------------------------------
  · **零模型调用**：所有真正执行的节点 `model_tier` 都是 `none`/`rule`；`small/medium/strong`
    只用于**声明**（§29），本轮不接任何模型（GPU 与语音机器人共用、已停用）。
  · §18 的 deep 链里 `evidence_graph`(P06) / `gap_loop`(P07) / `final_verify`(P13) 属后续阶段：
    本模块把它们建成 `status="deferred"`、`implemented=False`、`deferred_to="P0x"` 的**占位节点**，
    不参与预算、也不假装跑过。既有的 `conflict_review` 阶段是**真节点**（今天就在跑）。
  · `timeout` 是"分配预算"，不等于"一定会被强杀"：每个节点用 `budget_enforced` +
    `budget_source` 标明这笔预算今天到底由谁执行（舰队超时/多跳预算/阶段预算/仅记账）。
"""
from __future__ import annotations

import os
import time
from typing import Callable, Iterable, Mapping, Sequence

from qa_graph_contracts import (
    EXECUTION_GRAPH_VERSION,
    EXECUTION_NODE_KINDS,
    HUNTER_BM25,
    HUNTER_GRAPH,
    HUNTER_QUERY_EXPANSION,
    HUNTER_SEMANTIC,
    HUNTER_STRUCTURED,
    MODEL_TIERS,
    NODE_STATUSES,
    QA_ANSWER_TYPES,          # noqa: F401  （对外再导出，便于验收脚本一处 import）
    QA_FAILURE_DEGRADE,
    QA_FAILURE_FAIL_FAST,
    QA_FAILURE_FALLBACK,
    QA_FAILURE_RETRY,
    QA_FAILURE_SKIP,
    QA_PATH_DEEP,
    QA_PATH_FAST,
    QA_PATH_STANDARD,
    QA_PATHS,
    QA_STOP_ANSWERABLE,
    QA_STOP_BUDGET_EXHAUSTED,
    QA_STOP_MAX_DEPTH,
    QA_STOP_REASONS,
    SIGNAL_SCHEMA,            # noqa: F401
    describe as _contracts_describe,
    validate as validate_contract,
)
from qa_query_decompose import MAX_HOPS_HARD, decompose, validate_dag
from qa_query_interpreter import (
    REUSE_MAP as INTERPRETER_REUSE_MAP,
    interpret_query,
)
from qa_retrieval import _env_flag

# 路径链上的"预算来源"阶段（用于现算总预算；与 FULL_STAGES/FAST_STAGES 同口径）。
# 三档总预算一律**现算**（`path_budget()`）：`sum(STAGE_BUDGET_SECONDS[stage])`，
# 其中 level2 深研那一段优先用 `qa_policy.research_timeout_seconds`（那才是客户端真超时）；
# 环境变量 `QA_GRAPH_BUDGET_<PATH>_SECONDS` 或入参 `total_seconds` 可覆盖。
PATH_STAGES = {
    QA_PATH_FAST: (("plan", 3.0), ("level1_retrieval", 10.0), ("multi_hop", 25.0),
                   ("level1_draft", 60.0), ("synthesis", 45.0), ("citation_validation", 5.0)),
    QA_PATH_STANDARD: (("plan", 3.0), ("level1_retrieval", 10.0), ("multi_hop", 25.0),
                       ("logic_validation", 3.0), ("level1_draft", 60.0),
                       ("level2_retrieval", 60.0), ("level2_research", 90.0),
                       ("conflict_review", 12.0), ("synthesis", 45.0),
                       ("citation_validation", 5.0)),
    QA_PATH_DEEP: (("plan", 3.0), ("level1_retrieval", 10.0), ("multi_hop", 25.0),
                   ("logic_validation", 3.0), ("level1_draft", 60.0),
                   ("level2_retrieval", 60.0), ("level2_research_deep", 180.0),
                   ("conflict_review", 12.0), ("synthesis", 45.0),
                   ("citation_validation", 5.0)),
}
"""路径链 → (阶段, 兜底秒)。阶段预算一律**复用** `qa_resilience.STAGE_BUDGET_SECONDS`，
取不到时用这里的兜底（citation_validation / conflict_review 在既有表里没有独立条目）。"""

# 数据契约名（Edge 也是契约，§2.3）：节点用名字引用，不复制契约实体
SCHEMA_RESEARCH_PLAN = "qa.research_plan"
SCHEMA_SEARCH_TRACE = "qa.search_trace"
SCHEMA_EVIDENCE_OBJECT = "qa.evidence_object"
SCHEMA_EVIDENCE_VERIFICATION = "qa.evidence_verification"
SCHEMA_CLAIM_GRAPH = "qa.claim_graph"
SCHEMA_RETRIEVAL_RESULT = "qa.level1_result"
SCHEMA_LEVEL2_RESULT = "qa.level2_result"
SCHEMA_FINAL_ANSWER = "qa.final_answer"
SCHEMA_HUNTER_RESULT = "qa.hunter_result"
SCHEMA_QUERY_INTERPRETATION = "qa.query_interpretation"


# ── 开关（都默认关：不打开即逐字回到既有行为）───────────────────────────────
def graph_enabled() -> bool:
    """执行图接线开关（`QA_EXECUTION_GRAPH`，默认关）。"""
    return _env_flag("QA_EXECUTION_GRAPH", False)


def node_runs_enabled() -> bool:
    """节点行落库开关（`QA_EXECUTION_GRAPH_NODE_RUNS`，默认关）。

    单独一个开关的原因：写 `qa_stage_runs` 是**副作用**（会多出 `node:*` 行），
    与"只算一张图给回执看"是两件事；默认关保证既有行为零变化。
    """
    return _env_flag("QA_EXECUTION_GRAPH_NODE_RUNS", False)


def _config():
    import config

    return config


def _env_seconds(name: str) -> float:
    try:
        raw = float(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return 0.0
    return raw if raw > 0 else 0.0


def path_budget(path: str, *, total_seconds: float | None = None, policy=None) -> dict:
    """该路径的总预算 + 它的来源（可复算）。

    优先级：入参 `total_seconds` > `QA_GRAPH_BUDGET_<PATH>_SECONDS` > 阶段预算之和。
    `level2_research` 那一段优先用 `qa_policy.QaPolicy.research_timeout_seconds`
    （它就是这个客户端调用的真超时，见 `qa_pipeline` 构造 RAGFlow 客户端处）。
    """
    from qa_resilience import STAGE_BUDGET_SECONDS

    stages = list(PATH_STAGES.get(str(path), PATH_STAGES[QA_PATH_STANDARD]))
    sources = []
    total = 0.0
    for stage, fallback in stages:
        value = float(STAGE_BUDGET_SECONDS.get(stage, fallback) or fallback)
        if stage.startswith("level2_research") and policy is not None:
            ceiling = float(getattr(policy, "research_timeout_seconds", 0) or 0)
            if ceiling > 0:
                value = ceiling
                sources.append("%s=%s（qa_policy.research_timeout_seconds）" % (stage, value))
            else:
                sources.append("%s=%s（STAGE_BUDGET_SECONDS）" % (stage, value))
        else:
            sources.append("%s=%s" % (stage, value))
        total += value
    override = ""
    if total_seconds is not None and float(total_seconds) > 0:
        total = float(total_seconds)
        override = "explicit:%.3f" % total
    else:
        env_value = _env_seconds("QA_GRAPH_BUDGET_%s_SECONDS" % str(path or "").upper())
        if env_value > 0:
            total = env_value
            override = "env:QA_GRAPH_BUDGET_%s_SECONDS" % str(path or "").upper()
    return {
        "path": str(path),
        "total_seconds": round(total, 3),
        "override": override,
        "stages": [{"stage": stage, "fallback_seconds": fallback} for stage, fallback in stages],
        "sources": sources,
    }


def choose_path(interpretation: Mapping, mode: str = "") -> tuple:
    """选路径 → `(path, path_source)`。

    **路径 = 实际会跑的阶段链**：本仓库的阶段链由 `mode`（与 level2 开关）决定
    （`qa_orchestrator.stage_plan`），Phase 05 不改编排行为，所以这里不替调用方改档：
      · `mode=fast`  → fast（既有 `FAST_STAGES`）；
      · `mode=deep`  → deep（`FULL_STAGES` + 舰队 + 后续阶段的占位节点）；
      · 其余（standard/空）→ standard。
    §6 的 "complexity = simple 直接进入 Fast Path" 落地成**建议**（`suggest_path`），
    进回执供调用方/前端决定是否改档——隐式改档会让执行图与实际阶段链不一致（图会撒谎）。
    """
    requested = str(mode or "").strip().casefold()
    if requested == QA_PATH_FAST:
        return QA_PATH_FAST, "mode=fast（既有 FAST_STAGES）"
    if requested == QA_PATH_DEEP:
        return QA_PATH_DEEP, "mode=deep（既有 FULL_STAGES + 舰队）"
    complexity = str((interpretation or {}).get("complexity") or "").strip()
    if complexity == "simple":
        return QA_PATH_STANDARD, ("mode=standard；complexity=simple → 建议走 Fast Path（§6，"
                                  "见 suggested_path）")
    return QA_PATH_STANDARD, "mode=standard"


def suggest_path(interpretation: Mapping, mode: str = "") -> str:
    """§6 的降档/升档建议（只进回执，**不改变**实际路径）。"""
    requested = str(mode or "").strip().casefold()
    complexity = str((interpretation or {}).get("complexity") or "").strip()
    if complexity == "simple":
        return QA_PATH_FAST
    if requested == QA_PATH_DEEP or complexity == "deep":
        return QA_PATH_DEEP
    return QA_PATH_STANDARD


# ── P05-02 Subquestion / Claim decomposition ────────────────────────────────
def _sub_questions_from_hops(hops: Sequence[Mapping]) -> list:
    items = []
    for index, hop in enumerate(hops or []):
        hop_id = str(hop.get("id") or ("h%d" % (index + 1)))
        items.append({
            "sub_question_id": "sq:%s" % hop_id,
            "plan_node_kind": "sub_question",
            "question": str(hop.get("question") or ""),
            "purpose": str(hop.get("purpose") or ""),
            "depends_on": ["sq:%s" % str(dep) for dep in (hop.get("depends_on") or [])],
            "carry": [str(item) for item in (hop.get("carry") or [])],
            "hop_index": index,
            "parallel_group": "",
            "required_evidence_types": [],
        })
    return items


def _evidence_requirements(sub_questions: Sequence[Mapping], *, relationship: str,
                           categories: Sequence[Mapping]) -> list:
    """§7 的 Evidence Requirement：**复用** `qa_planner._retrieval_strategy` 的 source 口径。"""
    from qa_planner import _retrieval_strategy

    steps = _retrieval_strategy(str(relationship or "single"), list(categories or []))
    requirements = []
    for index, step in enumerate(steps, 1):
        requirements.append({
            "requirement_id": "er:%d" % index,
            "plan_node_kind": "evidence_requirement",
            "sub_question_id": "",
            "evidence_type": str(step.get("source") or ""),
            "rationale": str(step.get("purpose") or ""),
            "satisfied_by": "",       # Phase 05 只声明需求，不放"已满足"（那是 P06/P07 的判定）
        })
    for item in sub_questions or []:
        if requirements:
            break
        requirements.append({
            "requirement_id": "er:default",
            "plan_node_kind": "evidence_requirement",
            "sub_question_id": str(item.get("sub_question_id") or ""),
            "evidence_type": "official_policy",
            "rationale": "默认要求：可回溯的官方/一手来源",
            "satisfied_by": "",
        })
    return requirements


def _claims_from_sub_questions(sub_questions: Sequence[Mapping], *, role: str) -> list:
    claims = []
    for item in sub_questions or []:
        claims.append({
            "claim_id": "c:%s" % str(item.get("sub_question_id") or "").replace("sq:", ""),
            "plan_node_kind": "claim",
            "statement": "需要证实或证伪：%s" % str(item.get("question") or ""),
            "role": role,
            "sub_question_id": str(item.get("sub_question_id") or ""),
            "required_evidence_types": list(item.get("required_evidence_types") or []),
            "parallel_group": str(item.get("parallel_group") or ""),
        })
    claims.append({
        "claim_id": "c:counter", "plan_node_kind": "claim",
        "statement": "是否存在反证或替代解释（§7 的 H5：必须独立来源，不能只靠支持性证据）",
        "role": "counter", "sub_question_id": "", "plan_only": True,
        "required_evidence_types": ["professional_commentary", "adjudication"],
    })
    return claims


def _levels(node_ids: Sequence[str], deps_by_id: Mapping) -> dict:
    """按依赖算层级（level 0 = 无依赖）。环由 `validate_dag` 负责拦，这里只做拓扑分层。"""
    levels: dict = {}
    for node_id in node_ids:
        levels[node_id] = 0
    for _round in range(len(node_ids) + 1):
        changed = False
        for node_id in node_ids:
            deps = [str(dep) for dep in (deps_by_id.get(node_id) or []) if str(dep) in levels]
            if not deps:
                continue
            want = 1 + max(levels[dep] for dep in deps)
            if want > levels[node_id]:
                levels[node_id] = want
                changed = True
        if not changed:
            break
    return levels


def parallel_groups(nodes: Sequence[Mapping]) -> list:
    """依赖层级 → 并行组（§2.1/§2.4/§2.5）。

    · 同一层里的节点**互不依赖** → 可以并行（`parallel=True` 当且仅当组内 ≥2 个节点）；
    · 入度 ≥2 的节点是汇合点（barrier，§2.5："Barrier 只在真正需要全局结果时出现"）；
    · 组内被静默串行化（明明没有依赖却写成 A→B）会因为"层级不同"自然暴露出来。
    """
    items = list(nodes or [])
    ids = [str(item.get("node_id") or item.get("sub_question_id") or "") for item in items]
    deps_by_id = {str(item.get("node_id") or item.get("sub_question_id") or ""):
                  list(item.get("depends_on") or []) for item in items}
    levels = _levels(ids, deps_by_id)
    grouped: dict = {}
    for item in items:
        key = str(item.get("node_id") or item.get("sub_question_id") or "")
        grouped.setdefault(levels.get(key, 0), []).append(item)
    groups = []
    for level in sorted(grouped):
        members = grouped[level]
        member_ids = [str(item.get("node_id") or item.get("sub_question_id") or "") for item in members]
        barrier = any(len([d for d in (deps_by_id.get(mid) or []) if str(d) in ids]) >= 2
                      for mid in member_ids)
        group_id = "g%d" % (level + 1)
        for item in members:
            item["parallel_group"] = group_id
        groups.append({
            "group_id": group_id,
            "level": int(level),
            "nodes": member_ids,
            "size": len(member_ids),
            "parallel": len(member_ids) > 1,
            "barrier": bool(barrier),
        })
    return groups


def build_research_plan(question: str, *, plan: Mapping | None = None, mode: str = "standard",
                        interpretation: Mapping | None = None, max_hops: int | None = None,
                        now=None) -> dict:
    """§7 的研究计划：SubQuestion / Claim / Evidence Requirement / Dependency / parallel_groups。

    **复用 `plan["decomposition"]`**（`QaQueryPlanner.plan()` 已经算过的 DAG）：
    有就直接用，绝不重新分解一遍（否则执行图与实际多跳用的 hops 会分叉）。
    """
    plan = plan or {}
    text = str(question or plan.get("standalone_question") or plan.get("question") or "")
    category = plan.get("category") or {}
    question_plan = plan.get("question_plan") if isinstance(plan.get("question_plan"), Mapping) else {}
    relationship = str(question_plan.get("relationship") or "")
    categories = list(question_plan.get("categories") or [])
    if not categories and category:
        categories = [dict(category) if isinstance(category, Mapping) else {"key": str(category)}]

    decomposed_from = "reused:plan.decomposition"
    declared = plan.get("decomposition") if isinstance(plan.get("decomposition"), Mapping) else None
    decomposition = dict(declared or {})
    hops = list(decomposition.get("hops") or [])
    cap = int(max_hops if max_hops is not None else getattr(_config(), "QA_MAX_HOPS", 3) or 3)
    cap = max(1, min(cap, MAX_HOPS_HARD))
    if declared is None:
        # 规划器**根本没给** decomposition 才现算；给了就一律沿用（哪怕它只有一跳）——
        # 否则执行图会与实际多跳用的 hops 分叉（图上有 3 跳、运行期只跑 1 跳）。
        decomposition = decompose(
            text, category=str(category.get("key") if isinstance(category, Mapping) else category or ""),
            relationship=relationship, entities=list(plan.get("entities") or []),
            topics=list(plan.get("topics") or []), max_hops=cap)
        hops = list(decomposition.get("hops") or [])
        decomposed_from = "qa_query_decompose.decompose（计划里没有 decomposition）"

    # 跳数上限是否**真的**截断了计划：只有"放宽上限会长出更多跳"才算 MAX_DEPTH，
    # 不能因为"跳数正好等于上限"就宣布到了深度上限（那会把正常的多跳计划误报成截断）。
    hop_cap = {"max_hops": cap, "hop_count": len(hops), "probe_hop_count": len(hops),
               "truncated": False}
    if len(hops) >= cap and cap < MAX_HOPS_HARD:
        try:
            probe = decompose(
                text,
                category=str(category.get("key") if isinstance(category, Mapping) else category or ""),
                relationship=relationship, entities=list(plan.get("entities") or []),
                topics=list(plan.get("topics") or []), max_hops=MAX_HOPS_HARD)
            probe_hops = list(probe.get("hops") or [])
            hop_cap["probe_hop_count"] = len(probe_hops)
            hop_cap["truncated"] = len(probe_hops) > len(hops)
        except Exception as exc:
            hop_cap["probe_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:80])

    # 复用的 DAG 校验：环/自环/顺序/跳数上限不过 → 记下来（不掩盖）
    dag = validate_dag(hops, max_hops=MAX_HOPS_HARD)
    sub_questions = _sub_questions_from_hops(hops)
    requirements = _evidence_requirements(sub_questions, relationship=relationship,
                                          categories=categories)
    evidence_types = [item["evidence_type"] for item in requirements if item.get("evidence_type")]
    for item in sub_questions:
        item["required_evidence_types"] = list(evidence_types)
    role = "link" if bool(decomposition.get("is_multi_hop")) else "answer"
    claims = _claims_from_sub_questions(sub_questions, role=role)

    dependencies = []
    by_id = {item["sub_question_id"]: item for item in sub_questions}
    for item in sub_questions:
        for dep in item.get("depends_on") or []:
            dependencies.append({
                "from": str(dep), "to": item["sub_question_id"],
                "carries": list(item.get("carry") or []),
                "schema": SCHEMA_EVIDENCE_OBJECT,
                "why": "%s 的输出（实体/证据）是 %s 的输入" % (dep, item["sub_question_id"]),
            })
    groups = parallel_groups(sub_questions)
    return {
        "plan_version": EXECUTION_GRAPH_VERSION,
        "question": text,
        "mode": str(mode or "standard"),
        "interpretation": dict(interpretation or {}),
        "sub_questions": sub_questions,
        "claims": claims,
        "evidence_requirements": requirements,
        "required_evidence_types": list(evidence_types),
        "dependencies": [edge for edge in dependencies if edge["from"] in by_id],
        "parallel_groups": groups,
        "dag": {"ok": bool(dag.get("ok")), "problems": list(dag.get("problems") or [])},
        "hop_cap": hop_cap,
        "decomposition": {
            "is_multi_hop": bool(decomposition.get("is_multi_hop")),
            "pattern": str(decomposition.get("pattern") or ""),
            "reason": str(decomposition.get("reason") or ""),
            "hop_count": len(hops),
            "source": decomposed_from,
        },
        "reuse": dict(INTERPRETER_REUSE_MAP,
                      **{"decomposition": decomposed_from,
                         "dag_validation": "qa_query_decompose.validate_dag",
                         "evidence_requirements": "qa_planner._retrieval_strategy"}),
    }


# ── P05-03/P05-04：节点编排与三条路径 ──────────────────────────────────────
def _signal(name: str, fields: Iterable[str]) -> dict:
    return {"name": str(name), "fields": [str(item) for item in fields]}


def _node(path: str, name: str, *, node_kind: str, stage: str, purpose: str,
          depends_on=None, timeout: float = 0.0, retry: int = 0, model_tier: str = "none",
          allowed_tools=None, validation=None, failure_policy: str = QA_FAILURE_SKIP,
          input_schema: Mapping | None = None, output_schema: Mapping | None = None,
          budget_source: str = "", budget_enforced: bool = False, parent_node_id: str = "",
          optional: bool = False, implemented: bool = True, deferred_to: str = "",
          status: str = "pending", hop_index: int = -1, notes: str = "") -> dict:
    tier = str(model_tier) if str(model_tier) in MODEL_TIERS else "none"
    policy = str(failure_policy) if str(failure_policy) in (QA_FAILURE_FAIL_FAST, QA_FAILURE_RETRY,
                                                            QA_FAILURE_SKIP, QA_FAILURE_FALLBACK,
                                                            QA_FAILURE_DEGRADE) else QA_FAILURE_SKIP
    return {
        "node_id": "%s.%s" % (path, name),
        "node_kind": str(node_kind),
        "stage": str(stage),
        "purpose": str(purpose),
        "path": str(path),
        "parent_node_id": str(parent_node_id or ""),
        "depends_on": [str(item) for item in (depends_on or [])],
        "parallel_group": "",
        "barrier": False,
        "input_schema": dict(input_schema or _signal(SCHEMA_RESEARCH_PLAN, ["question", "sub_questions"])),
        "output_schema": dict(output_schema or _signal(SCHEMA_EVIDENCE_OBJECT, ["evidence_ref", "status"])),
        "timeout": float(max(0.0, timeout)),
        "retry": int(max(0, retry)),
        "model_tier": tier,
        "allowed_tools": [str(item) for item in (allowed_tools or [])],
        "validation": [str(item) for item in (validation or [])],
        "failure_policy": policy,
        "budget_source": str(budget_source or ""),
        "budget_enforced": bool(budget_enforced),
        "optional": bool(optional),
        "implemented": bool(implemented),
        "deferred_to": str(deferred_to or ""),
        "status": str(status),
        "hop_index": int(hop_index),
        "notes": str(notes or ""),
    }


def _stage_budget(stage: str, default: float) -> float:
    from qa_resilience import STAGE_BUDGET_SECONDS

    return float(STAGE_BUDGET_SECONDS.get(str(stage), default) or default)


def _policy_for_stage(stage: str, default: str) -> str:
    """失败策略**复用**既有实现：`qa_orchestrator.DEGRADABLE_STAGES` 里的阶段今天就是 DEGRADE。"""
    try:
        from qa_orchestrator import DEGRADABLE_STAGES

        if str(stage) in DEGRADABLE_STAGES:
            return QA_FAILURE_DEGRADE
    except Exception:
        pass
    return str(default)


def stage_chain_for(mode: str, *, level2_enabled: bool = True) -> tuple:
    """本轮真正的阶段链：**直接复用** `QaOrchestrator.stage_plan`（不另立一份阶段表）。"""
    try:
        from qa_orchestrator import QaOrchestrator

        return tuple(QaOrchestrator.stage_plan(mode, level2_enabled=level2_enabled))
    except Exception:
        from qa_orchestrator import FAST_STAGES, FULL_STAGES

        if str(mode or "").casefold() == "fast":
            return tuple(FAST_STAGES)
        if level2_enabled is False:
            return tuple(stage for stage in FULL_STAGES
                         if stage not in ("level2_retrieval", "level2_research", "conflict_review"))
        return tuple(FULL_STAGES)


def _apply_stage_chain(nodes: list, stage_chain: Sequence[str]) -> list:
    """把"不在本轮阶段链上"的节点标成 skipped（图必须与实际会跑的阶段一致）。"""
    chain = {str(item) for item in (stage_chain or [])}
    off = []
    for item in nodes:
        stage = str(item.get("stage") or "")
        if item.get("status") == "deferred" or not stage:
            continue
        if stage not in chain and item.get("status") not in ("skipped",):
            item["status"] = "skipped"
            item["notes"] = (str(item.get("notes") or "")
                             + "；阶段 %s 不在本轮链上（qa_orchestrator.stage_plan）" % stage).strip("；")
            off.append(str(item.get("node_id")))
    return off


def _retrieval_nodes(path: str, ctx: Mapping) -> list:
    """检索段：多跳 → 每跳一个节点；舰队打开 → 首跳扇出 + 汇合（§2.4/§2.5）。

    首跳的入口节点都**依赖规划节点**（`<path>.interpret`）：检索用的 queries/entities 来自
    规划结果，所以"计划 → 检索"是真实依赖，不许写成并行（§2.1）。
    """
    nodes: list = []
    hops = list(ctx.get("hops") or [])
    hop_ids = [str(hop.get("id") or ("h%d" % (i + 1))) for i, hop in enumerate(hops)]
    hop_cap = float(ctx.get("hop_budget_seconds") or 25.0)
    per_hop = max(1.0, hop_cap / max(1, len(hop_ids)))
    # 首跳同时受"多跳预算按跳分摊"与"level1_retrieval 阶段预算"两个既有上限约束，取小的那个
    first_hop_cap = min(per_hop, _stage_budget("level1_retrieval", 10.0))
    tools = [str(item) for item in (ctx.get("channels") or [])]
    plan_node = "%s.interpret" % path
    if ctx.get("fleet_on"):
        hunter_timeout = float(ctx.get("hunter_timeout") or 8.0)
        hunter_retry = int(ctx.get("hunter_retries") or 0)
        fallbacks = dict(ctx.get("hunter_fallbacks") or {})
        parent = ""
        for index, hunter_id in enumerate(ctx.get("hunters") or []):
            nodes.append(_node(
                path, "hunter.%s" % hunter_id, node_kind="retrieve",
                stage="level1_retrieval", hop_index=0, optional=index >= 1,
                purpose="首跳并行检索：%s 通道（§8.x %s Hunter）" % (hunter_id, hunter_id),
                depends_on=[plan_node], parent_node_id=parent,
                timeout=hunter_timeout, retry=hunter_retry,
                allowed_tools=[str(hunter_id)],
                validation=["hunter_result"], failure_policy=QA_FAILURE_RETRY,
                input_schema=_signal(SCHEMA_SEARCH_TRACE, ["sub_query", "route"]),
                output_schema=_signal(SCHEMA_HUNTER_RESULT, ["hunter_id", "status", "evidence"]),
                budget_source="QA_HUNTER_FLEET_TIMEOUT_SECONDS / QA_HUNTER_FLEET_RETRIES",
                budget_enforced=True,
                notes=("失败回退 → %s" % fallbacks.get(hunter_id)) if fallbacks.get(hunter_id) else ""))
        nodes.append(_node(
            path, "merge", node_kind="merge", stage="level1_retrieval",
            purpose="首跳扇入：全局去重 + 时间窗 + 权威性排序（§2.5 Barrier）",
            depends_on=["%s.hunter.%s" % (path, item) for item in (ctx.get("hunters") or [])],
            timeout=float(ctx.get("merge_seconds") or 1.0), model_tier="rule",
            allowed_tools=["dedupe", "time_window", "authority_rank"],
            validation=["level1_result"], failure_policy=QA_FAILURE_DEGRADE,
            input_schema=_signal(SCHEMA_HUNTER_RESULT, ["evidence"]),
            output_schema=_signal(SCHEMA_RETRIEVAL_RESULT, ["evidence", "stats"]),
            budget_source="内存合并（无外部调用）", budget_enforced=False,
            notes="§2.5：多 Hunter 之后才需要 fan-in；第 1 跳 = 主检索（复用，不重复打）"))
        entry = "%s.merge" % path
    else:
        entry = "%s.retrieve" % path
        nodes.append(_node(
            path, "retrieve", node_kind="retrieve", stage="level1_retrieval", hop_index=0,
            purpose="首跳检索（既有 ArticleRetriever 通道：%s）" % "、".join(tools or ["keyword"]),
            depends_on=[plan_node], timeout=first_hop_cap, model_tier="none",
            allowed_tools=tools or [HUNTER_BM25], validation=["level1_result"],
            failure_policy=QA_FAILURE_RETRY,
            input_schema=_signal(SCHEMA_SEARCH_TRACE, ["sub_query", "route"]),
            output_schema=_signal(SCHEMA_RETRIEVAL_RESULT, ["evidence", "stats", "time_window"]),
            budget_source=("min(STAGE_BUDGET_SECONDS['level1_retrieval']=%.0fs, "
                           "QA_MULTI_HOP_BUDGET_SECONDS/%d 跳=%.2fs)" % (
                               _stage_budget("level1_retrieval", 10.0), max(1, len(hop_ids)), per_hop)),
            budget_enforced=False))
    for index, hop_id in enumerate(hop_ids[1:], 1):
        nodes.append(_node(
            path, "hop.%s" % hop_id, node_kind="retrieve", stage="level1_retrieval",
            hop_index=index, optional=True,
            purpose="第 %d 跳：%s" % (index + 1, str(hops[index].get("purpose") or "")[:80]),
            depends_on=[entry] if index == 1 else ["%s.hop.%s" % (path, hop_ids[index - 1])],
            timeout=per_hop,
            allowed_tools=tools or [HUNTER_BM25], validation=["search_trace", "level1_result"],
            failure_policy=QA_FAILURE_SKIP,
            input_schema=_signal(SCHEMA_EVIDENCE_OBJECT, ["entities", "evidence_ref"]),
            output_schema=_signal(SCHEMA_RETRIEVAL_RESULT, ["evidence", "stats"]),
            budget_source="QA_MULTI_HOP_BUDGET_SECONDS 按跳分摊（QA_MULTI_HOP_BUDGET_SECONDS=%.1fs）"
                          % hop_cap,
            budget_enforced=True,
            notes="超预算时既有 _run_multi_hop 会写 status=skipped_budget（停止原因 BUDGET_EXHAUSTED）"))
    return nodes


def _nodes_for_path(path: str, ctx: Mapping) -> list:
    plan_stage = "plan"
    nodes: list = [_node(
        path, "interpret", node_kind="plan", stage=plan_stage,
        purpose="Query Interpreter + Research Planner（§6/§7）：意图、子问题、Claim、证据要求",
        depends_on=[], timeout=_stage_budget(plan_stage, 3.0), model_tier="rule",
        allowed_tools=["qa_query_interpreter", "qa_query_decompose"],
        validation=["query_interpretation", "sub_question", "plan_claim"],
        failure_policy=QA_FAILURE_FAIL_FAST,
        input_schema=_signal("qa.question", ["question", "industry_pack_id"]),
        output_schema=_signal(SCHEMA_RESEARCH_PLAN, ["sub_questions", "claims",
                                                     "evidence_requirements", "parallel_groups"]),
        budget_source="STAGE_BUDGET_SECONDS['plan']", budget_enforced=False,
        notes="§2.2 禁止节点一边搜索一边回答：本节点只规划")]

    retrieval = _retrieval_nodes(path, ctx)
    nodes.extend(retrieval)
    retrieval_ids = [item["node_id"] for item in retrieval]

    nodes.append(_node(
        path, "verify", node_kind="verify", stage="level1_retrieval",
        purpose="§2.6 Verifier 放在 Edge 上：证据层标注 + 核验判定（既有 qa_evidence/qa_verifier）",
        depends_on=retrieval_ids, timeout=float(ctx.get("verify_seconds") or 3.0),
        model_tier="rule", allowed_tools=["qa_evidence", "qa_verifier"],
        validation=["evidence_object", "evidence_verification"],
        failure_policy=_policy_for_stage("level1_retrieval", QA_FAILURE_DEGRADE),
        input_schema=_signal(SCHEMA_EVIDENCE_OBJECT, ["evidence_ref", "content_excerpt"]),
        output_schema=_signal(SCHEMA_EVIDENCE_VERIFICATION, ["verdict", "score", "reasons"]),
        budget_source="进程内规则核验（缓存命中 2–4ms，见 P03-04）", budget_enforced=False,
        notes="失败降级：核验不可用时证据照常进包，只是不许当「已验证」"))
    nodes.append(_node(
        path, "rerank", node_kind="rerank", stage="level1_retrieval",
        purpose="按核验分稳定重排（§18 fast 链的 rerank；既有 qa_verifier.rerank_evidence）",
        depends_on=["%s.verify" % path], timeout=float(ctx.get("rerank_seconds") or 1.0),
        model_tier="rule", allowed_tools=["qa_verifier.rerank_evidence"],
        validation=["evidence_object"], failure_policy=QA_FAILURE_DEGRADE,
        input_schema=_signal(SCHEMA_EVIDENCE_VERIFICATION, ["score"]),
        output_schema=_signal(SCHEMA_EVIDENCE_OBJECT, ["evidence_ref", "status"]),
        budget_source="QA_VERIFIER_RERANK（默认开，进程内）", budget_enforced=False))
    nodes.append(_node(
        path, "logic_check", node_kind="verify", stage="logic_validation",
        purpose="逻辑校验（既有 logic_validation：因果链/条件满足/缺失链接，有缺口必须明说）",
        depends_on=["%s.rerank" % path], timeout=_stage_budget("logic_validation", 3.0),
        model_tier="rule", allowed_tools=["qa_pipeline._logic_validation"],
        validation=["level1_result"],
        failure_policy=_policy_for_stage("logic_validation", QA_FAILURE_DEGRADE),
        input_schema=_signal(SCHEMA_EVIDENCE_OBJECT, ["evidence"]),
        output_schema=_signal("qa.logic_validation", ["status", "missing_links"]),
        budget_source="STAGE_BUDGET_SECONDS['logic_validation']", budget_enforced=False))

    nodes.append(_node(
        path, "draft", node_kind="answer", stage="level1_draft",
        purpose="一级草稿（既有 level1_draft，证据约束生成）",
        depends_on=["%s.logic_check" % path], timeout=_stage_budget("level1_draft", 60.0),
        model_tier="strong", allowed_tools=["qa_level1"], validation=["level1_result"],
        failure_policy=QA_FAILURE_FALLBACK,
        input_schema=_signal(SCHEMA_EVIDENCE_OBJECT, ["evidence"]),
        output_schema=_signal("qa.level1_draft", ["answer", "claims"]),
        budget_source="STAGE_BUDGET_SECONDS['level1_draft']", budget_enforced=True,
        notes="§28：草稿失败回退到证据约束结果（FALLBACK）；本轮不许调模型 → 实际走规则兜底"))

    if path != QA_PATH_FAST:
        level2_status = "pending" if ctx.get("level2_enabled", True) else "skipped"
        level2_note = ("" if ctx.get("level2_enabled", True)
                       else "level2 关闭 → qa_orchestrator.stage_plan 会把这三个阶段从链上摘掉")
        nodes.append(_node(
            path, "level2_retrieval", node_kind="retrieve", stage="level2_retrieval",
            purpose="二级检索（RAGFlow 知识库）：按跳数上限取补充证据",
            depends_on=["%s.draft" % path], timeout=_stage_budget("level2_retrieval", 60.0),
            model_tier="none", allowed_tools=["ragflow_retrieval"], optional=True,
            validation=["level2_result"], failure_policy=_policy_for_stage("level2_retrieval",
                                                                          QA_FAILURE_DEGRADE),
            input_schema=_signal(SCHEMA_RESEARCH_PLAN, ["queries", "entities"]),
            output_schema=_signal(SCHEMA_LEVEL2_RESULT, ["evidence", "stats"]),
            budget_source="STAGE_BUDGET_SECONDS['level2_retrieval']", status=level2_status,
            notes=level2_note or ("跳数上限 %s（qa_policy）" % ctx.get("level2_max_hops"))))
        research_stage = "level2_research_deep" if path == QA_PATH_DEEP else "level2_research_standard"
        research_seconds = _stage_budget(research_stage, 90.0)
        if ctx.get("policy_research_timeout"):
            research_seconds = float(ctx["policy_research_timeout"])
        nodes.append(_node(
            path, "level2_research", node_kind="retrieve", stage="level2_research",
            purpose="深研阶段（§18 deep 的检索深挖）：受跳数/检索式上限与客户端超时约束",
            depends_on=["%s.level2_retrieval" % path],
            timeout=research_seconds, model_tier="strong", optional=True,
            allowed_tools=["ragflow_research"], validation=["level2_result"],
            failure_policy=_policy_for_stage("level2_research", QA_FAILURE_DEGRADE),
            input_schema=_signal(SCHEMA_LEVEL2_RESULT, ["evidence"]),
            output_schema=_signal(SCHEMA_LEVEL2_RESULT, ["evidence", "claims"]),
            budget_source=("qa_policy.research_timeout_seconds（就是这个客户端调用的超时）"
                           if ctx.get("policy_research_timeout")
                           else "STAGE_BUDGET_SECONDS['%s']（记账）" % research_stage),
            # 只有拿到 policy 时这笔超时才真的传给了 RAGFlow 客户端（见 qa_pipeline 构造处）；
            # 用阶段预算兜底时它只是记账，不许写成"已被执行"。
            budget_enforced=bool(ctx.get("policy_research_timeout")), status=level2_status,
            notes=level2_note or ("跳数上限 %s / 每跳检索式上限 %s（qa_policy）"
                                  % (ctx.get("level2_max_hops"), ctx.get("max_queries_per_hop")))))
        nodes.append(_node(
            path, "conflict_review", node_kind="contradiction", stage="conflict_review",
            purpose="冲突复核（既有 conflict_review 阶段：claim 级核验 + 口径裁决）",
            depends_on=["%s.level2_research" % path],
            timeout=_stage_budget("level2_query", 12.0), model_tier="strong", optional=True,
            allowed_tools=["qa_verifier.verify_claim_graph"], validation=["claim_evidence_edge"],
            failure_policy=_policy_for_stage("conflict_review", QA_FAILURE_DEGRADE),
            input_schema=_signal(SCHEMA_CLAIM_GRAPH, ["claims"]),
            output_schema=_signal(SCHEMA_CLAIM_GRAPH, ["claims", "conflicts"]),
            budget_source="STAGE_BUDGET_SECONDS['level2_query']（记账）", budget_enforced=False,
            status=level2_status,
            notes=level2_note or "P06 会把它升级为 Evidence Graph 级矛盾检测；本阶段复用既有阶段"))

    nodes.append(_node(
        path, "answer", node_kind="answer", stage="synthesis",
        purpose="综合成稿（既有 synthesis：绑定引用、标注缺口与降级）",
        depends_on=(["%s.conflict_review" % path, "%s.draft" % path] if path != QA_PATH_FAST
                    else ["%s.draft" % path]),
        timeout=_stage_budget("synthesis", 45.0),
        model_tier="strong", allowed_tools=["qa_synthesis"], validation=["final_answer"],
        failure_policy=QA_FAILURE_FALLBACK,
        input_schema=_signal("qa.level1_draft", ["answer", "claims"]),
        output_schema=_signal(SCHEMA_FINAL_ANSWER, ["answer", "citations", "conflicts"]),
        budget_source="STAGE_BUDGET_SECONDS['synthesis']", budget_enforced=True,
        notes="§28：Answer Composer 要求 Evidence State 有效，否则 Fail Fast（上游无证据时降级）"))
    nodes.append(_node(
        path, "finalize", node_kind="verify", stage="citation_validation",
        purpose="引用与结构校验（既有 citation_validation：引用必须可回溯）",
        depends_on=["%s.answer" % path], timeout=float(ctx.get("finalize_seconds") or 5.0),
        model_tier="rule", allowed_tools=["qa_contracts.validate_final_answer"],
        validation=["final_answer"], failure_policy=QA_FAILURE_FAIL_FAST,
        input_schema=_signal(SCHEMA_FINAL_ANSWER, ["answer", "citations"]),
        output_schema=_signal(SCHEMA_FINAL_ANSWER, ["answer", "citations", "status"]),
        budget_source="进程内契约校验（无外部调用）", budget_enforced=False))

    if path == QA_PATH_DEEP:
        # ── §18 deep 链里的后续阶段：**占位节点**，本阶段不执行、不假装跑过 ──
        nodes.append(_node(
            path, "evidence_graph", node_kind="evidence_graph", stage="evidence_graph",
            purpose="证据图构建与矛盾检测（§18 deep 的第 3 段）",
            depends_on=["%s.verify" % path], timeout=0.0, model_tier="rule",
            allowed_tools=["qa_evidence_graph"], validation=["evidence_object"],
            failure_policy=QA_FAILURE_DEGRADE,
            output_schema=_signal(SCHEMA_EVIDENCE_OBJECT, ["relations"]),
            implemented=False, deferred_to="P06（Evidence Graph）", status="deferred",
            notes="本阶段未实现：Phase 06 交付；占位只为让 deep 链与 §18 对得上"))
        nodes.append(_node(
            path, "gap_loop", node_kind="gap_loop", stage="gap_loop",
            purpose="缺口驱动的再检索循环（§14 收敛与停止）",
            depends_on=["%s.evidence_graph" % path], timeout=0.0, model_tier="strong",
            allowed_tools=["qa_gap_analyzer"], validation=["search_trace"],
            failure_policy=QA_FAILURE_DEGRADE,
            output_schema=_signal(SCHEMA_SEARCH_TRACE, ["gap_id", "resolved_gap"]),
            implemented=False, deferred_to="P07（Gap Analyzer / Dynamic Multi-hop）",
            status="deferred",
            notes="本阶段未实现：NO_GAIN/UNRESOLVABLE_CONTRADICTION 两个停止原因也归 P07"))
        nodes.append(_node(
            path, "final_verify", node_kind="final_verify", stage="final_verify",
            purpose="最终核验（§18 deep 的最后一段）",
            depends_on=["%s.answer" % path], timeout=0.0, model_tier="strong",
            allowed_tools=["qa_final_verifier"], validation=["final_answer"],
            failure_policy=QA_FAILURE_FAIL_FAST,
            output_schema=_signal(SCHEMA_FINAL_ANSWER, ["status"]),
            implemented=False, deferred_to="P13（Answer Composer / Final Verifier）",
            status="deferred",
            notes="本阶段未实现：Phase 13 交付"))
    return nodes


def _mark_barriers(nodes: list) -> None:
    """入度 ≥2 的节点是汇合点（barrier）：它的下游必须等所有上游。"""
    ids = {str(item.get("node_id") or "") for item in nodes}
    for item in nodes:
        deps = [str(dep) for dep in (item.get("depends_on") or []) if str(dep) in ids]
        item["barrier"] = bool(len(deps) >= 2)
        item["depends_on"] = deps


def _edges(nodes: Sequence[Mapping]) -> list:
    """Edge 也是数据契约（§2.3）：上游输出什么结构、下游需要什么结构。"""
    by_id = {str(item.get("node_id")): item for item in nodes}
    edges = []
    for item in nodes:
        for dep in item.get("depends_on") or []:
            source = by_id.get(str(dep)) or {}
            edges.append({
                "src": str(dep), "dst": str(item.get("node_id")),
                "schema": str((source.get("output_schema") or {}).get("name") or ""),
                "expected_by": str((item.get("input_schema") or {}).get("name") or ""),
            })
    return edges


def _execution_order(nodes: Sequence[Mapping]) -> list:
    """按层级排序（同层保持声明顺序）——声明顺序就是既有阶段链的顺序。"""
    items = list(nodes)
    ids = [str(item.get("node_id") or "") for item in items]
    levels = _levels(ids, {str(item.get("node_id") or ""): item.get("depends_on") or []
                           for item in items})
    return sorted(items, key=lambda item: levels.get(str(item.get("node_id") or ""), 0))


def _critical_path(nodes: Sequence[Mapping]) -> tuple:
    """关键路径（§2.4/§2.5）：并行分支取"最慢的一支"，不是把并行时间相加。"""
    by_id = {str(item.get("node_id") or ""): item for item in nodes}
    best: dict = {}

    def walk(node_id: str):
        if node_id in best:
            return best[node_id]
        node = by_id.get(node_id) or {}
        deps = [str(dep) for dep in (node.get("depends_on") or []) if str(dep) in by_id]
        if not deps:
            best[node_id] = (float(node.get("timeout") or 0.0), [node_id])
            return best[node_id]
        candidates = [walk(dep) for dep in deps]
        cost, path = max(candidates, key=lambda item: item[0])
        best[node_id] = (cost + float(node.get("timeout") or 0.0), list(path) + [node_id])
        return best[node_id]

    for node_id in by_id:
        walk(node_id)
    if not best:
        return 0.0, []
    return max(best.values(), key=lambda item: item[0])


def apply_budget(nodes: list, total_seconds: float, *, off_chain=None) -> dict:
    """P05-05：把节点预算摊到总预算里；装不下就**从链尾往前裁**并写停止原因。

    两件事必须分清（否则预算表会自相矛盾）：
      · 每个节点的 `timeout` 是**上限**（谁执行写在 `budget_source`）；
      · `total_seconds` 是这条路径的**墙钟预算**。
    上限之和 > 墙钟预算是常态（最坏情况相加），所以裁剪只针对 `optional=True` 的节点，
    并且**只在关键路径的上限之和真的装不下时**才裁——裁完仍装不下就老实标
    `infeasible=True`（宁写"装不下"，也不假装裁两下就合适了）。

    裁剪口径与既有运行期语义一致：`_run_multi_hop` 在预算用尽时就是"停在做完的跳上"
    （`status=skipped_budget`），这里把同一件事在计划期先算出来，让它**可见**。
    """
    executed = [item for item in nodes if str(item.get("status")) != "deferred"]
    order = _execution_order(executed)
    cut_ids: set = set()
    off_chain = {str(item) for item in (off_chain or [])}
    # 阶段链已经判定"不在本轮链上"的节点（status=skipped）不参与预算估算——
    # 它们根本不会跑，算进去会把估计值撑大（level2 关闭时尤其明显）。
    cut_ids.update(str(item.get("node_id")) for item in executed
                   if str(item.get("status")) == "skipped")

    def _remaining():
        return [item for item in executed if str(item.get("node_id")) not in cut_ids]

    for item in reversed(order):
        if not item.get("optional"):
            continue
        if _critical_path(_remaining())[0] <= float(total_seconds):
            break
        item["status"] = "budget_exhausted"
        cut_ids.add(str(item.get("node_id")))

    # 上游被裁 → 下游不可能跑；但**可选**上游被裁不算硬依赖（跳 2 没跑，第 3 步照常出答案，
    # 与 _run_multi_hop 的既有语义一致：停在做完的跳上，不把整条链判死）
    optional_ids = {str(item.get("node_id")) for item in executed if item.get("optional")}
    hard_cut = {node_id for node_id in cut_ids if node_id not in optional_ids}
    skipped: list = []
    changed = True
    while changed:
        changed = False
        for item in executed:
            node_id = str(item.get("node_id"))
            if node_id in cut_ids:
                continue
            deps = [str(dep) for dep in (item.get("depends_on") or [])]
            if any(dep in hard_cut for dep in deps):
                item["status"] = "skipped"
                item["notes"] = (str(item.get("notes") or "") + "；上游被预算裁掉").strip("；")
                cut_ids.add(node_id)
                hard_cut.add(node_id)
                skipped.append(node_id)
                changed = True

    remaining = _remaining()
    estimate, path = _critical_path(remaining)
    cut_nodes = [str(item.get("node_id")) for item in executed
                 if item.get("status") == "budget_exhausted" and str(item.get("node_id")) not in off_chain]
    return {
        "total_seconds": round(float(total_seconds), 3),
        "allocated_seconds": round(sum(float(n.get("timeout") or 0.0) for n in executed), 3),
        "estimated_wall_clock_seconds": round(float(estimate), 3),
        "critical_path": [str(item) for item in path],
        "cut_nodes": cut_nodes,
        "dependency_skipped_nodes": list(skipped),
        "infeasible": bool(estimate > float(total_seconds)),
        "infeasible_detail": ("关键路径上限之和 %.1fs > 墙钟预算 %.1fs（上限是最坏情况，"
                              "不是期望耗时：运行期由阶段预算/模型超时/多跳预算各自兜底）"
                              % (estimate, float(total_seconds)))
        if estimate > float(total_seconds) else "",
        "per_node": {str(n.get("node_id")): {"timeout": float(n.get("timeout") or 0.0),
                                             "enforced": bool(n.get("budget_enforced")),
                                             "source": str(n.get("budget_source") or ""),
                                             "status": str(n.get("status") or "")}
                     for n in executed},
    }


def build_execution_graph(question: str, *, plan: Mapping | None = None, mode: str = "standard",
                          policy=None, interpretation: Mapping | None = None,
                          research_plan: Mapping | None = None, hunters=None,
                          level2_enabled: bool = True, run_id: str = "",
                          max_hops: int | None = None, total_seconds: float | None = None,
                          now=None) -> dict:
    """建执行图（§3.1）：谁执行 / 能否并行 / 何时汇合 / 失败怎么办 / 何时停。

    参数只用既有零件：`plan` 是 `QaQueryPlanner.plan()` 的输出（含 decomposition/policy 输入），
    `policy` 是 `qa_policy.QaPolicy`（三档跳数与运行时长上限），`hunters` 为 None 时
    按 `QA_HUNTER_FLEET` 开关决定是否展开并行 Hunter（图必须反映**实际会跑什么**）。
    """
    plan = plan or {}
    text = str(question or plan.get("standalone_question") or plan.get("question") or "")
    interp = dict(interpretation or interpret_query(
        text, plan=plan, category=plan.get("category"), entities=plan.get("entities"), now=now))
    path, path_source = choose_path(interp, mode)
    research = dict(research_plan or build_research_plan(
        text, plan=plan, mode=mode, interpretation=interp, max_hops=max_hops, now=now))
    hops = list((plan.get("decomposition") or {}).get("hops") or [])
    if not hops and research.get("sub_questions"):
        # 外部直接传进来的 research_plan（没有原始 decomposition）→ 从子问题还原顺序骨架
        hops = [{"id": str(item.get("sub_question_id") or "").replace("sq:", ""),
                 "question": str(item.get("question") or ""),
                 "depends_on": [str(dep).replace("sq:", "") for dep in (item.get("depends_on") or [])],
                 "purpose": str(item.get("purpose") or "")}
                for item in (research.get("sub_questions") or [])]

    config = _config()
    fleet_on = bool(hunters) if hunters is not None else False
    if hunters is None:
        try:
            import qa_hunter_fleet as fleet_module

            fleet_on = bool(fleet_module.fleet_enabled())
            hunter_ids = list(fleet_module.DEFAULT_HUNTER_ORDER)
            hunter_timeout = float(fleet_module.hunter_timeout_seconds())
            hunter_retries = int(fleet_module.retries())
            fleet_budget = float(fleet_module.total_budget_seconds())
        except Exception:
            hunter_ids, hunter_timeout, hunter_retries, fleet_budget = [], 8.0, 0, 20.0
    else:
        hunter_ids = [str(item) for item in hunters]
        try:
            import qa_hunter_fleet as fleet_module

            hunter_timeout = float(fleet_module.hunter_timeout_seconds())
            hunter_retries = int(fleet_module.retries())
            fleet_budget = float(fleet_module.total_budget_seconds())
        except Exception:
            hunter_timeout, hunter_retries, fleet_budget = 8.0, 0, 20.0

    hop_budget = float(getattr(config, "QA_MULTI_HOP_BUDGET_SECONDS", 25) or 25)
    max_hop_count = int(max_hops if max_hops is not None
                        else getattr(config, "QA_MAX_HOPS", 3) or 3)
    max_hop_count = max(1, min(max_hop_count, MAX_HOPS_HARD))
    level2_max_hops = int(getattr(policy, "deep_max_hops" if path == QA_PATH_DEEP
                                  else "standard_max_hops", 1) or 1) if policy is not None else 1
    ctx = {
        "hops": hops, "hop_budget_seconds": hop_budget, "fleet_on": fleet_on,
        "hunters": hunter_ids if fleet_on else [], "hunter_timeout": hunter_timeout,
        "hunter_retries": hunter_retries,
        "hunter_fallbacks": {HUNTER_SEMANTIC: HUNTER_BM25, HUNTER_GRAPH: QA_FAILURE_DEGRADE},
        "channels": ["policy_exact", "keyword", "graph", "page_context"],
        "level2_enabled": bool(level2_enabled), "level2_max_hops": level2_max_hops,
        "max_queries_per_hop": int(getattr(policy, "max_queries_per_hop", 5) or 5),
        "policy_research_timeout": (float(getattr(policy, "research_timeout_seconds", 0) or 0)
                                    if policy is not None else 0.0),
        "merge_seconds": 1.0, "verify_seconds": 3.0, "rerank_seconds": 1.0, "finalize_seconds": 5.0,
    }
    nodes = _nodes_for_path(path, ctx)
    stage_chain = stage_chain_for(mode, level2_enabled=level2_enabled)
    off_chain = _apply_stage_chain(nodes, stage_chain)
    _mark_barriers(nodes)
    groups = parallel_groups(nodes)

    path_default = path_budget(path, policy=policy)
    total = float(total_seconds) if (total_seconds is not None and float(total_seconds) > 0) else (
        _env_seconds("QA_GRAPH_BUDGET_%s_SECONDS" % path.upper()) or path_default["total_seconds"])
    budget = apply_budget(nodes, total, off_chain=off_chain)
    budget.update({
        "path": path,
        "path_default_seconds": path_default["total_seconds"],
        "path_budget_sources": list(path_default["sources"]),
        "path_budget_override": str(path_default["override"] or ""),
        "policy_ceiling_seconds": (round(float(getattr(policy, "research_timeout_seconds", 0) or 0), 3)
                                   if policy is not None else 0.0),
        "hop_budget_seconds": round(hop_budget, 3),
        "hunter_budget_seconds": round(fleet_budget, 3),
        "max_hops": max_hop_count,
        "level2_max_hops": level2_max_hops,
        "fleet_on": bool(fleet_on),
        "hunter_count": len(hunter_ids) if fleet_on else 0,
        "note": ("每节点 timeout 是上限（谁执行见 budget_source），total_seconds 是这条路径的"
                 "墙钟预算；上限之和大于预算是常态，只有关键路径上限之和装不下时才裁 optional 节点"),
    })

    executed_nodes = [n for n in nodes if n.get("status") != "deferred"]
    stop_reasons: list = []
    if budget["cut_nodes"] or budget["dependency_skipped_nodes"]:
        stop_reasons.append({
            "reason": QA_STOP_BUDGET_EXHAUSTED,
            "detail": "墙钟预算 %.1fs 装不下关键路径上限：裁掉 %s；上游被裁而不可跑 %s"
                      % (total, "、".join(budget["cut_nodes"]) or "无",
                         "、".join(budget["dependency_skipped_nodes"]) or "无"),
        })
    if budget.get("infeasible"):
        stop_reasons.append({
            "reason": QA_STOP_BUDGET_EXHAUSTED,
            "detail": "裁完可选节点后仍装不下：%s" % str(budget.get("infeasible_detail") or ""),
        })
    if bool((research.get("hop_cap") or {}).get("truncated")):
        stop_reasons.append({
            "reason": QA_STOP_MAX_DEPTH,
            "detail": "跳数被上限 %d（QA_MAX_HOPS）截断：放宽到 %d 会产出 %d 跳 > 当前 %d 跳"
                      % (max_hop_count, MAX_HOPS_HARD,
                         int((research.get("hop_cap") or {}).get("probe_hop_count") or 0),
                         len(hops)),
        })
    if not stop_reasons:
        stop_reasons.append({
            "reason": QA_STOP_ANSWERABLE,
            "detail": "计划完整、关键路径上限 %.1fs 装得进墙钟预算 %.1fs"
                      % (budget["estimated_wall_clock_seconds"], total),
        })
    primary = QA_STOP_BUDGET_EXHAUSTED if any(
        item["reason"] == QA_STOP_BUDGET_EXHAUSTED for item in stop_reasons) else (
        QA_STOP_MAX_DEPTH if any(item["reason"] == QA_STOP_MAX_DEPTH for item in stop_reasons)
        else QA_STOP_ANSWERABLE)
    budget["stop_reason"] = primary
    budget["stop_reasons"] = list(stop_reasons)
    # 本阶段绝不可能产出 NO_GAIN / UNRESOLVABLE_CONTRADICTION（那属 Phase 07 的缺口闭环）
    budget["stop_reasons_supported"] = [QA_STOP_ANSWERABLE, QA_STOP_BUDGET_EXHAUSTED,
                                        QA_STOP_MAX_DEPTH]

    graph = {
        "contract_version": EXECUTION_GRAPH_VERSION,
        "graph_version": EXECUTION_GRAPH_VERSION,
        "run_id": str(run_id or ""),
        "question": text,
        "mode": str(mode or "standard"),
        "path": path,
        "path_source": path_source,
        "suggested_path": suggest_path(interp, mode),
        "spec_chain": {
            QA_PATH_FAST: ["retrieve", "rerank", "answer"],
            QA_PATH_STANDARD: ["plan", "2~3 hunters", "verify", "answer"],
            QA_PATH_DEEP: ["plan", "retrieval fleet", "evidence graph", "gap loop",
                           "contradiction resolution", "answer", "final verifier"],
        }.get(path, []),
        "intent": str(interp.get("intent") or ""),
        "complexity": str(interp.get("complexity") or ""),
        "answer_type": str(interp.get("answer_type") or ""),
        "stage_chain": [str(item) for item in stage_chain],
        "off_chain_nodes": list(off_chain),
        "nodes": nodes,
        "edges": _edges(nodes),
        "parallel_groups": groups,
        "budget": budget,
        "stop_reason": primary,
        "stop_reasons": stop_reasons,
        "node_counts": {
            "total": len(nodes),
            "runnable": len(executed_nodes),
            "deferred": len(nodes) - len(executed_nodes),
            "parallel": sum(group["size"] for group in groups if group["parallel"]),
            "barriers": sum(1 for node in nodes if node.get("barrier")),
            "budget_exhausted": sum(1 for node in nodes if node.get("status") == "budget_exhausted"),
            "skipped": sum(1 for node in nodes if node.get("status") == "skipped"),
            "enforced": sum(1 for node in nodes if node.get("budget_enforced")),
        },
        "research_plan": research,
        "interpretation": interp,
        "reuse": dict(INTERPRETER_REUSE_MAP, **{
            "sub_question_dag": "qa_query_decompose.decompose/validate_dag",
            "hop_budget": "config.QA_MULTI_HOP_BUDGET_SECONDS / config.QA_MAX_HOPS",
            "stage_budget": "qa_resilience.STAGE_BUDGET_SECONDS",
            "policy": "qa_policy.QaPolicy（research_timeout_seconds / *_max_hops / max_queries_per_hop）",
            "fleet": "qa_hunter_fleet（并发/超时/重试/回退/总预算）",
            "enums": "qa_graph_contracts（失败策略五值、停止原因五值）",
            "node_run_columns": "qa_storage.record_stage(node_id/node_kind/parent_node_id/round_index)",
        }),
    }
    ok, note = validate_contract("execution_graph", graph)
    graph["contract_ok"] = bool(ok)
    graph["contract_note"] = "" if ok else str(note)
    return graph


# ── P05-05：运行期账本（超预算可观测地停止）─────────────────────────────────
class ExecutionLedger:
    """节点级运行账本：谁跑了、跑了多久、预算还剩多少、为什么停。

    · `should_run(node_id)` 在预算耗尽后返回 False —— 调用方据此**真的停**（不是只打标签）；
    · `stop_reason` 只取既有五值枚举：预算耗尽 `BUDGET_EXHAUSTED`，跑完 `ANSWERABLE`；
    · 时钟可注入（`clock`），测试里用假时钟就能复算。
    """

    def __init__(self, graph: Mapping | None = None, *, total_seconds: float | None = None,
                 clock: Callable | None = None, stop_reason_on_budget: str = QA_STOP_BUDGET_EXHAUSTED):
        budget = dict((graph or {}).get("budget") or {})
        self.total_seconds = float(total_seconds if total_seconds is not None
                                   else budget.get("total_seconds") or 0.0)
        self.clock = clock or time.monotonic
        self.started = float(self.clock())
        self.entries: dict = {}
        self.order: list = []
        self.stop_reason_on_budget = str(stop_reason_on_budget)

    # —— 记账 ——
    def begin(self, node_id: str) -> float:
        self._entry(node_id)["started"] = float(self.clock())
        return float(self.clock())

    def finish(self, node_id: str, *, status: str = "ok", latency_ms: int | None = None,
               detail: str = "", evidence: int = 0) -> dict:
        entry = self._entry(node_id)
        if latency_ms is None:
            started = entry.get("started")
            if started is None:
                started = self.clock()
            latency_ms = int(max(0.0, (float(self.clock()) - float(started))) * 1000)
        entry.update({"status": str(status), "latency_ms": int(latency_ms),
                      "detail": str(detail or ""), "evidence": int(evidence or 0)})
        return dict(entry)

    def record(self, node_id: str, *, status: str = "ok", latency_ms: int = 0,
               detail: str = "", evidence: int = 0) -> dict:
        """直接记一条（用于"别人已经跑完、这里补记"的节点，如 hop 回执）。"""
        return self.finish(node_id, status=status, latency_ms=latency_ms, detail=detail,
                           evidence=evidence)

    def _entry(self, node_id: str) -> dict:
        key = str(node_id)
        if key not in self.entries:
            self.entries[key] = {"node_id": key, "status": "pending", "latency_ms": 0,
                                 "detail": "", "evidence": 0, "started": None}
            self.order.append(key)
        return self.entries[key]

    # —— 预算 ——
    def used_seconds(self) -> float:
        return max(0.0, float(self.clock()) - self.started)

    def remaining_seconds(self) -> float:
        return self.total_seconds - self.used_seconds()

    def exceeded(self) -> bool:
        return self.total_seconds > 0 and self.used_seconds() >= self.total_seconds

    def should_run(self, node_id: str) -> bool:
        """预算用尽 → False（调用方据此跳过剩余节点）。已跑过的节点不受影响。"""
        return not self.exceeded()

    def stop_reason(self, *, default: str = "") -> str:
        if self.exceeded():
            return self.stop_reason_on_budget
        for entry in self.entries.values():
            if entry.get("status") == "budget_exhausted":
                return self.stop_reason_on_budget
        return str(default or QA_STOP_ANSWERABLE)

    def receipt(self, *, default_stop_reason: str = "") -> dict:
        nodes = [dict(self.entries[key]) for key in self.order]
        reason = self.stop_reason(default=default_stop_reason)
        return {
            "total_seconds": round(self.total_seconds, 3),
            "used_seconds": round(self.used_seconds(), 3),
            "remaining_seconds": round(self.remaining_seconds(), 3),
            "over_budget": bool(self.exceeded()),
            "stop_reason": reason,
            "nodes": nodes,
            "executed_count": sum(1 for item in nodes if item["status"] != "pending"),
        }


def hop_node_id(graph: Mapping, hop_id: str) -> str:
    """给调用方一个稳定映射：第 N 跳的证据实际由哪个节点产出（舰队打开时首跳是 merge）。"""
    path = str((graph or {}).get("path") or "standard")
    nodes = list((graph or {}).get("nodes") or [])
    if str(hop_id) in ("h1", ""):
        for item in nodes:
            if item.get("node_id") == "%s.merge" % path or (
                    item.get("node_id") == "%s.retrieve" % path):
                return str(item.get("node_id"))
        return "%s.retrieve" % path
    candidate = "%s.hop.%s" % (path, hop_id)
    return candidate if any(item.get("node_id") == candidate for item in nodes) else candidate


def record_node_runs(store, run_id: str, graph: Mapping, *, ledger: ExecutionLedger | None = None,
                     round_index: int = 0, attempt: int = 1) -> dict:
    """把执行图节点落进 Phase 01 的 node-run 列（`qa_stage_runs`）。

    `stage` 用 `node:<node_id>`：既有阶段行的唯一键是 (run_id, stage, attempt)，
    用真节点 id 当 stage 才能"一节点一行"，而不会把既有阶段行互相打回默认值
    （这正是 `record_stage` 那段 CASE 保护的语义）。`node_id`/`node_kind`/`parent_node_id`
    才是执行图要落的列。**失败不影响主流程**（落库问题不许把检索拖死）。
    """
    written, failed, skipped = [], [], []
    nodes = list((graph or {}).get("nodes") or [])
    if ledger is None:
        ledger = ExecutionLedger(graph)
        for item in nodes:
            if item.get("status") == "deferred":
                continue
            ledger.record(str(item.get("node_id")), status=str(item.get("status") or "pending"))
    for item in nodes:
        node_id = str(item.get("node_id") or "")
        if not node_id:
            continue
        entry = dict(ledger.entries.get(node_id) or {})
        status = str(entry.get("status") or item.get("status") or "pending")
        if status == "pending":
            skipped.append(node_id)
            continue
        if status not in ("ok", "empty", "skipped", "degraded", "error", "timeout",
                          "budget_exhausted", "deferred"):
            status = "error"
        try:
            store.record_stage(
                str(run_id), "node:%s" % node_id, status=status, attempt=int(attempt),
                node_id=node_id, node_kind=str(item.get("node_kind") or "execution"),
                parent_node_id=str(item.get("parent_node_id") or ""),
                round_index=int(round_index),
                details={"execution_graph": {
                    "path": str((graph or {}).get("path") or ""),
                    "purpose": str(item.get("purpose") or ""),
                    "failure_policy": str(item.get("failure_policy") or ""),
                    "budget_source": str(item.get("budget_source") or ""),
                    "latency_ms": int(entry.get("latency_ms") or 0),
                    "evidence": int(entry.get("evidence") or 0),
                    "detail": str(entry.get("detail") or ""),
                    "implemented": bool(item.get("implemented", True)),
                    "deferred_to": str(item.get("deferred_to") or ""),
                }},
            )
            written.append(node_id)
        except Exception as exc:      # 落库失败只记录，不影响检索
            failed.append({"node_id": node_id, "error": "%s: %s" % (type(exc).__name__, str(exc)[:120])})
    return {
        "written": written, "written_count": len(written),
        "failed": failed, "skipped_pending": skipped,
        "stage_prefix": "node:", "contract": "qa_stage_runs.node_id/node_kind/parent_node_id/round_index",
    }


def graph_receipt(graph: Mapping | None, *, ledger: ExecutionLedger | None = None,
                  node_runs: Mapping | None = None) -> dict:
    """给管线用的短回执（放 stats 的**兄弟键**，不动任何既有键集）。"""
    value = dict(graph or {})
    if not value:
        return {}
    budget = dict(value.get("budget") or {})
    receipt = {
        "graph_version": str(value.get("graph_version") or ""),
        "path": str(value.get("path") or ""),
        "path_source": str(value.get("path_source") or ""),
        "suggested_path": str(value.get("suggested_path") or ""),
        "intent": str(value.get("intent") or ""),
        "complexity": str(value.get("complexity") or ""),
        "node_counts": dict(value.get("node_counts") or {}),
        "parallel_groups": [{"group_id": group.get("group_id"), "size": group.get("size"),
                             "parallel": group.get("parallel"), "barrier": group.get("barrier")}
                            for group in (value.get("parallel_groups") or [])],
        "budget": {
            "total_seconds": budget.get("total_seconds"),
            "path_default_seconds": budget.get("path_default_seconds"),
            "estimated_wall_clock_seconds": budget.get("estimated_wall_clock_seconds"),
            "critical_path": list(budget.get("critical_path") or []),
            "cut_nodes": list(budget.get("cut_nodes") or []),
            "infeasible": bool(budget.get("infeasible")),
            "hop_budget_seconds": budget.get("hop_budget_seconds"),
            "policy_ceiling_seconds": budget.get("policy_ceiling_seconds"),
            "max_hops": budget.get("max_hops"),
            "fleet_on": bool(budget.get("fleet_on")),
            "hunter_count": budget.get("hunter_count"),
            "per_node": dict(budget.get("per_node") or {}),
        },
        "stop_reason": str(value.get("stop_reason") or ""),
        "stop_reasons": list(value.get("stop_reasons") or []),
        "hop_count": int(((value.get("research_plan") or {}).get("decomposition") or {})
                         .get("hop_count") or 0),
        "dag_ok": bool(((value.get("research_plan") or {}).get("dag") or {}).get("ok")),
        "contract_ok": bool(value.get("contract_ok")),
    }
    if ledger is not None:
        receipt["runtime"] = ledger.receipt(default_stop_reason=str(value.get("stop_reason") or ""))
    if node_runs:
        receipt["node_runs"] = dict(node_runs)
    return receipt


def describe() -> str:
    budgets = "、".join("%s=%.0fs" % (path, path_budget(path)["total_seconds"])
                       for path in QA_PATHS)
    return ("Research Planner / Execution Graph %s：路径 %s / 节点种类 %d / 现算总预算 %s"
            % (EXECUTION_GRAPH_VERSION, "/".join(QA_PATHS), len(EXECUTION_NODE_KINDS), budgets))


__all__ = [
    "ExecutionLedger",
    "PATH_STAGES",
    "apply_budget",
    "build_execution_graph",
    "build_research_plan",
    "choose_path",
    "describe",
    "graph_enabled",
    "graph_receipt",
    "hop_node_id",
    "node_runs_enabled",
    "parallel_groups",
    "path_budget",
    "record_node_runs",
    "stage_chain_for",
    "suggest_path",
]
