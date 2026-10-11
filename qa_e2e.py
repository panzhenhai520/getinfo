#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 17（P17-01）· **E2E suite**：离线端到端重放（零模型 / 零网络 / 零嵌入端点 / 可复算）。

## 为什么是"离线重放"而不是"真机跑一次问答"

本包的硬约束（DECISION_LOG D-018/D-035/B-003）禁止调用任何模型或嵌入端点：容器内
`QA_ALLOWED_PROVIDER_HOSTS` 指向的就是与语音机器人共用的那台 GPU 机器。因此"真实一次问答"
在本约束下**不可得**——缺的不是代码而是生成端算力。本模块沿用 Phase 04/09/10/11/12/13/16 已确立的
同一条口径：**A 机只读快照 + 同一份代码离线重放**。具体是：

  · **检索侧**（`run_retrieval_suite`）：对冻结 benchmark 逐题真跑**生产阶段处理器**
    `qa_pipeline.build_qa_stage_handlers()` 的 `plan` + `level1_retrieval`。这两个 handler 就是
    生产用的那两段代码，唯一替换掉的是它的三个外呼口（web 检索 / provider 注册表 / 模型），
    替换件是"离线桩"且**每次外呼都会记账**（见 `OFFLINE_SUBSTITUTIONS`）。
  · **回答侧**（`run_answer_suite`）：对 A 机导出的真实 run（真实 claim / 证据 / 最终答案）跑
    `conflict_review` 之后的全部**规则层**（核验 → 证据图 → 缺口复核 → 记忆 → 技能 → 上下文包 →
    获取计划 → 终验）。生成端草稿用**真机已录答案**，因此"生成"这一步不是本模块造的，是生产录下来的。

## 指标口径（唯一真源，绝不另立一套）

  · 检索侧：直接调 `tools/qa_retrieval_acceptance.summarize`（Phase 00 冻结的 Recall@K /
    citation P·R / hit / traceable / grounded / cost 口径），逐题行字段与它逐字对齐；
  · 回答侧：直接调 `tools/qa_phase13_final_acceptance` 的 `_composition_facts` / `_verification_facts`
    / `_replay_verification`（Phase 13 的六问与引用可回溯口径）与
    `tools/qa_phase08_context_acceptance` 的 `_graph_for` / `_traceability`（Phase 08 的图重建与
    span 复算口径）。
  · 拿不到这两个工具模块时**不做第二套计算**，直接把指标写成 null + reason 码（`caliber_missing`）。

## 层开关（消融的抓手）

`LAYER_SPECS` 逐层写明：开关名、**生产默认值**（由各模块自己的 accessor 读出，不抄字面量）、
作用侧、以及**生产接线点**（file:line）。消融时只翻这些环境变量，代码路径不变。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from typing import Iterable, Mapping, Sequence

E2E_SUITE_VERSION = "qa-e2e-suite-v1"
RETRIEVAL_ROW_KEYS = (
    "id", "question", "kind", "industry_pack_id", "evidence", "hit", "traceable",
    "traceable_ratio", "grounded", "terms", "graph", "graph_attribute", "graph_event",
    "graph_receipt", "time_window", "ms", "expect_terms_count", "expect_terms_hit",
    "cited_total", "cited_matched", "citation_hit",
)
"""逐题明细键：与 `tools/qa_retrieval_acceptance.evaluate()` 的输出逐字一致（同一口径）。"""

E2E_METRIC_KEYS = (
    "hit_rate", "traceable_rate", "grounded_rate", "recall_at_k", "citation_precision",
    "citation_recall", "avg_evidence", "avg_ms", "errors",
)
"""检索侧参与 before/after 与消融增量的指标键（Phase 00 冻结口径里的同一批键）。"""

OFFLINE_SUBSTITUTIONS = (
    {"port": "web_search", "real": "QaWebSearchService（外网检索）", "offline": "_OfflineWebSearch",
     "why": "离线验收不许出网；每次都记账 calls，回执里如实写该通道无对象"},
    {"port": "provider_registry", "real": "QaProviderRegistry（模型提供方）", "offline": "_OfflineProviderRegistry",
     "why": "硬约束禁止调模型端点；解析一律抛错 → plan 阶段的预热线程直接返回，不产生任何外呼"},
    {"port": "level1_generator / final_synthesizer", "real": "QaLevel1Generator / QaFinalSynthesizer（模型）",
     "offline": "不调用（草稿取真机已录答案）", "why": "生成端必须调模型，本约束下不可得（B-003）"},
    {"port": "ragflow_client（level2）", "real": "QaRagflowResearchClient", "offline": "不调用（needs_ragflow 关闭）",
     "why": "离线环境没有 RAGFlow；level2 在回执里记 no_object"},
)

LAYER_SPECS = (
    {
        "layer": "evidence_layer", "phase": "P02", "sides": ("retrieval",),
        "switch": "QA_EVIDENCE_LAYER_ENABLED", "default": True,
        "accessor": "qa_evidence:evidence_layer_enabled",
        "wired_at": "qa_pipeline._apply_evidence_layer（qa_pipeline.py:610 的开关判断）",
        "measures": ("recall_at_k", "citation_precision", "citation_recall", "avg_ms"),
        "note": "标注 + seen 登记 + 跨轮去重；关掉即回到 Phase 01 的证据条元数据形状",
    },
    {
        "layer": "verifier", "phase": "P03", "sides": ("retrieval", "answer"),
        "switch": "QA_VERIFIER_ENABLED", "default": True,
        "accessor": "qa_verifier:verifier_enabled",
        "wired_at": "qa_pipeline._apply_evidence_layer（qa_pipeline.py:681 只在此开关打开时写核验回执）",
        "measures": ("recall_at_k", "citation_precision", "unsupported_claim_rate", "checks_pass_rate"),
        "note": "证据级 + claim 级核验；关掉后 claim 状态回到模型自评（MASTER_RULES 11 的守卫对象）",
    },
    {
        "layer": "hunter_fleet", "phase": "P04", "sides": ("retrieval",),
        "switch": "QA_HUNTER_FLEET", "default": False,
        "accessor": "qa_pipeline:_hunter_fleet_enabled",
        "wired_at": "qa_pipeline.level1_retrieval（qa_pipeline.py:2944）",
        "measures": ("hit_rate", "recall_at_k", "citation_precision", "avg_ms"),
        "note": "并行舰队（BM25/语义/图/结构化/扩展词）；关掉即既有 ArticleRetriever 顺序多通道",
    },
    {
        "layer": "execution_graph", "phase": "P05", "sides": ("retrieval",),
        "switch": "QA_EXECUTION_GRAPH", "default": False,
        "accessor": "qa_pipeline:_execution_graph_enabled",
        "wired_at": "qa_pipeline.level1_retrieval（qa_pipeline.py:2958）",
        "measures": ("recall_at_k", "avg_ms"),
        "note": "计划期建图 + 账本；只读既有配置，本身不改检索行为（增量应≈0，这本身就是结论）",
    },
    {
        "layer": "evidence_graph", "phase": "P06", "sides": ("answer",),
        "switch": "QA_EVIDENCE_GRAPH", "default": False,
        "accessor": "qa_evidence_graph:evidence_graph_enabled",
        "wired_at": "qa_pipeline.conflict_review（qa_pipeline.py:3715）",
        "measures": ("claim_coverage", "contradictions", "checks_pass_rate"),
        "note": "显式证据图 + 矛盾裁决；真机 qa_conflicts 全库 0 行 → 矛盾裁决无对象",
    },
    {
        "layer": "gap_analyzer", "phase": "P07", "sides": ("retrieval", "answer"),
        "switch": "QA_GAP_ANALYZER", "default": False,
        "accessor": "qa_gap_analyzer:gap_analyzer_enabled",
        "wired_at": "qa_pipeline._run_multi_hop 缺口环 + qa_pipeline._attach_gap_review（qa_pipeline.py:3720）",
        "measures": ("gaps", "stop_reason", "recall_at_k"),
        "note": "缺口驱动补充跳 + 证据图复核（UNRESOLVABLE_CONTRADICTION 的唯一出口）",
    },
    {
        "layer": "context_pack", "phase": "P08", "sides": ("answer",),
        "switch": "QA_CONTEXT_PACK", "default": False,
        "accessor": "qa_context_pack:context_pack_enabled",
        "wired_at": "qa_pipeline.conflict_review（qa_pipeline.py:3773）",
        "measures": ("context_tokens", "context_items", "citation_traceable_rate"),
        "note": "九段最小有效上下文包 + 引用索引 + Context Gap",
    },
    {
        "layer": "memory_graph", "phase": "P09-P10-P15", "sides": ("answer",),
        "switch": "QA_MEMORY_GRAPH", "default": False,
        "accessor": "qa_memory:memory_graph_enabled",
        "wired_at": "qa_pipeline.conflict_review（qa_pipeline.py:3731 召回 / _write_memories_after_answer）",
        "measures": ("memory_recalled", "memory_written", "context_tokens"),
        "note": "写门 + 召回（恒为 MEMORY_HINT）+ 复验 + 作用域/轮次隔离；线上 memory_item 0 行 → 真机无对象",
    },
    {
        "layer": "skill_router", "phase": "P11", "sides": ("answer",),
        "switch": "QA_SKILL_ROUTER", "default": False,
        "accessor": "qa_skills:skill_router_enabled",
        "wired_at": "qa_pipeline.conflict_review（qa_pipeline.py:3743）",
        "measures": ("skills_selected", "context_tokens"),
        "note": "最小必要集合 + 权限/成本/延迟预算；指令恒不进 citation_map",
    },
    {
        "layer": "final_verifier", "phase": "P13", "sides": ("answer",),
        "switch": "QA_FINAL_VERIFIER", "default": False,
        "accessor": "qa_final_verifier:final_verifier_enabled",
        "wired_at": "qa_pipeline.synthesis / citation_validation（qa_pipeline.py:1266 _final_verification_step）",
        "measures": ("checks_pass_rate", "blocking", "citation_traceable_rate", "insufficient_runs"),
        "note": "claim 感知组装 + 六问终验 + 局部重试（retrieval_calls 恒 0）+ 证据不足输出",
    },
    {
        "layer": "acquisition", "phase": "P14", "sides": ("answer",),
        "switch": "QA_ACQUISITION", "default": False,
        "accessor": "qa_pipeline:_acquisition_enabled",
        "wired_at": "qa_pipeline._build_context_pack_layer（qa_pipeline.py:794）",
        "measures": ("acquisition_actions", "context_gap_suppressed"),
        "note": "SAGE-RAG 统一获取；与 Phase 08 Context Gap 共用同一份缺口产物",
    },
)

LAYER_BY_ID = {spec["layer"]: spec for spec in LAYER_SPECS}
LAYER_IDS = tuple(spec["layer"] for spec in LAYER_SPECS)


# ---------------------------------------------------------------------------
# 离线替换件（每个外呼口都记账：回执里能查到"这里没出网"）
# ---------------------------------------------------------------------------
class OfflineWebSearch:
    """外部检索的离线替换件：永远返回空证据，并计数（回执里如实写该通道无对象）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.enabled_calls = 0

    def search(self, queries, *, enabled: bool = False, limit: int = 8) -> dict:
        self.calls += 1
        if enabled:
            self.enabled_calls += 1
        return {"evidence": [], "status": "offline_no_network",
                "queries": [str(item) for item in (queries or [])][:8],
                "enabled": bool(enabled), "limit": int(limit)}

    def receipt(self) -> dict:
        return {"substitution": "OfflineWebSearch", "calls": self.calls,
                "enabled_calls": self.enabled_calls, "evidence": 0,
                "reason_code": "OFFLINE_NO_NETWORK"}


class OfflineProviderRegistry:
    """模型提供方注册表的离线替换件：任何解析都抛错（禁止任何模型端点调用）。"""

    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError("offline-acceptance: model provider resolution is disabled")

    def __getattr__(self, name):
        raise RuntimeError("offline-acceptance: provider registry disabled (%s)" % name)

    def receipt(self) -> dict:
        return {"substitution": "OfflineProviderRegistry", "calls": self.calls,
                "reason_code": "OFFLINE_NO_MODEL_ENDPOINT"}


# ---------------------------------------------------------------------------
# 层开关：读真实 accessor（不抄默认值）+ 临时翻转（消融抓手）
# ---------------------------------------------------------------------------
def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().casefold() not in {"0", "false", "no", "off", ""}


def _accessor(spec: Mapping) -> tuple:
    """把 `"module:attr"` 解析成 `(module, callable|None)`；解析不了返回 (None, None)。"""
    text = str(spec.get("accessor") or "")
    if ":" not in text:
        return None, None
    module_name, attr = text.split(":", 1)
    try:
        module = __import__(module_name, fromlist=[attr])
        return module, getattr(module, attr, None)
    except Exception:  # noqa: BLE001 —— 模块不可用时退回环境变量口径（默认值仍取 spec 里那份）
        return None, None


def layer_states() -> dict:
    """当前每一层的真实开关状态（默认值由各模块自己的 accessor 读出）。"""
    states = {}
    for spec in LAYER_SPECS:
        module, accessor = _accessor(spec)
        default = bool(spec.get("default"))
        enabled = None
        if callable(accessor):
            try:
                enabled = bool(accessor())
            except Exception:  # noqa: BLE001
                enabled = None
        states[spec["layer"]] = {
            "layer": spec["layer"], "phase": spec["phase"], "sides": list(spec["sides"]),
            "switch": spec["switch"], "documented_default": default,
            "runtime_default": (bool(enabled) if enabled is not None
                                else _env_flag(str(spec["switch"]), default)),
            "enabled": (bool(enabled) if enabled is not None
                        else _env_flag(str(spec["switch"]), default)),
            "accessor_available": callable(accessor),
            "wired_at": spec["wired_at"], "measures": list(spec["measures"]),
            "note": spec["note"],
        }
    return states


@contextmanager
def layer_switches(states: Mapping):
    """临时把层开关设成给定值（消融用），退出时逐字恢复原环境。"""
    saved = {}
    try:
        for layer, value in dict(states or {}).items():
            spec = LAYER_BY_ID.get(str(layer))
            if not spec:
                continue
            name = str(spec["switch"])
            saved[name] = os.environ.get(name)
            os.environ[name] = "1" if value else "0"
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def reset_process_caches() -> dict:
    """清掉**跨格会串味**的进程内缓存（核验缓存是进程级的，消融必须先清）。

    只清进程内对象（不动任何库表）；清不掉的如实记账，让"缓存串味"这件事有据可查。
    """
    report = {}
    try:
        from qa_verifier import _DEFAULT_CACHE  # type: ignore

        cleared = 0
        for attr in ("_data", "_entries", "_order", "_lru", "_store"):
            store = getattr(_DEFAULT_CACHE, attr, None)
            if hasattr(store, "clear"):
                try:
                    cleared += len(store)
                except TypeError:      # 没有 __len__ 就只清
                    pass
                store.clear()
        report["verifier_cache"] = {"cleared": cleared, "ok": True,
                                    "attrs": sorted(name for name in
                                                    ("_data", "_entries", "_order", "_lru", "_store")
                                                    if hasattr(_DEFAULT_CACHE, name))}
    except Exception as exc:  # noqa: BLE001
        report["verifier_cache"] = {"ok": False, "error": "%s: %s" % (type(exc).__name__,
                                                                    str(exc)[:120])}
    return report


# ---------------------------------------------------------------------------
# 语料：只读快照 → 隔离临时 sqlite（同 Phase 04 的构建器；模板 + 文件级复制）
# ---------------------------------------------------------------------------
def build_corpus_template(snapshot_path: str, *, workdir: str = "") -> dict:
    """把 A 机只读语料快照建成**模板库**（后续每一格用文件级复制得到独立库）。"""
    from tools.qa_hunter_fleet_acceptance import build_db_from_snapshot

    path = os.path.abspath(snapshot_path)
    if not os.path.isfile(path):
        raise FileNotFoundError("语料快照不存在：%s" % snapshot_path)
    with open(path, encoding="utf-8") as handle:
        snapshot = json.load(handle)
    workdir = workdir or tempfile.mkdtemp(prefix="qa-p17-corpus-")
    os.makedirs(workdir, exist_ok=True)
    db_path = os.path.join(workdir, "corpus-template.sqlite3")
    _saved = os.environ.get("DATABASE_PATH")
    try:
        os.environ["DATABASE_PATH"] = db_path
        database = build_db_from_snapshot(snapshot, db_path)
        packs = sorted(str(key) for key in (snapshot.get("packs") or {}))
        counts = {
            "articles": len(snapshot.get("articles") or []),
            "events": len(snapshot.get("events") or []),
            "attributes": len(snapshot.get("attributes") or []),
            "embeddings": len(snapshot.get("embeddings") or []),
            "classifications": len(snapshot.get("classifications") or []),
        }
        created_at = str(snapshot.get("captured_at_utc")
                         or (snapshot.get("source") or {}).get("captured_at_utc") or "")
        source = dict(snapshot.get("source") or {})
        try:
            database.connection.close()
        except Exception:  # noqa: BLE001
            pass
    finally:
        if _saved is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = _saved
    return {"template_path": db_path, "workdir": workdir, "snapshot_path": path,
            "snapshot_version": str(snapshot.get("snapshot_version") or ""),
            "captured_at_utc": created_at, "packs": packs, "counts": counts,
            "source": {"host": str(source.get("host") or ""),
                       "access": str(source.get("access") or ""),
                       "queries": str(source.get("queries") or "")[:400]}}


def clone_database(template_path: str, target_path: str) -> str:
    """文件级复制模板库 → 一份**互不串味**的独立库（消融每格一份）。"""
    target = os.path.abspath(target_path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copyfile(os.path.abspath(template_path), target)
    return target


# ---------------------------------------------------------------------------
# 检索侧 E2E：真实 plan + level1_retrieval 阶段处理器
# ---------------------------------------------------------------------------
def build_offline_handlers(database, store, *, article_retriever=None):
    """装配生产阶段处理器，三个外呼口换成离线桩（返回 `(handlers, substitutions)`）。"""
    import qa_pipeline
    from qa_retrieval import ArticleRetriever

    web = OfflineWebSearch()
    registry = OfflineProviderRegistry()
    retriever = article_retriever or ArticleRetriever(database)
    handlers = qa_pipeline.build_qa_stage_handlers(
        database=database, store=store, article_retriever=retriever,
        web_search=web, provider_registry=registry,
    )
    return handlers, {"web_search": web, "provider_registry": registry,
                      "article_retriever": type(retriever).__name__}


def _acceptance_module():
    """指标口径的唯一真源（拿不到就返回 None，绝不另算一套）。"""
    try:
        import tools.qa_retrieval_acceptance as acceptance

        return acceptance
    except Exception:  # noqa: BLE001
        return None


def run_retrieval_suite(database, questions: Sequence[Mapping], *, limit: int = 12,
                        mode: str = "standard", store=None, owner: str = "user:1",
                        session_id: str = "p17-e2e", id_prefix: str = "p17",
                        session_per_question: bool = True) -> dict:
    """逐题真跑生产检索阶段，产出与 Phase 00 **同口径**的逐题行 + 阶段回执。

    只读快照、零模型、零外网：外呼口全部换成离线桩并记账。

    `session_per_question=True`（默认）时每题一个会话轴：**跨轮去重仍然在题内生效**
    （多跳逐跳、level1 与后续跳之间），但不会让 A 题的 seen 影响 B 题的指标——
    消融对比要求每一格、每一题都被独立测量。
    """
    acceptance = _acceptance_module()
    if acceptance is None:
        raise RuntimeError("拿不到 tools.qa_retrieval_acceptance（指标口径的唯一真源）")
    from qa_storage import QaStore

    store = store or QaStore(database)
    store.ensure_schema()
    handlers, substitutions = build_offline_handlers(database, store)
    rows: list = []
    receipts: list = []
    for index, item in enumerate(questions, 1):
        question = str(item.get("question") or "").strip()
        if not question:
            continue
        pack_id = str(item.get("industry_pack_id") or "")
        session = ("%s-%s" % (session_id, item.get("id") or index)) if session_per_question \
            else session_id
        run = store.create_run(
            {"industry_pack_id": pack_id, "question": question, "mode": mode,
             "session_id": session},
            owner_user_id=owner,
            idempotency_key="%s-%s-%s" % (id_prefix, pack_id, index))
        run_id = str(run["id"])
        run_meta = {"id": run_id, "owner_user_id": owner, "session_id": session,
                    "industry_pack_id": pack_id}
        context = {"run": run_meta,
                   "request": {"question": question, "industry_pack_id": pack_id, "mode": mode},
                   "outputs": {}, "store": store}
        import time as _time

        started = _time.monotonic()
        error = ""
        evidence: list = []
        stats: dict = {}
        plan: dict = {}
        try:
            plan = handlers["plan"](context)
            context["outputs"]["plan"] = plan
            level1 = handlers["level1_retrieval"](context)
            context["outputs"]["level1_retrieval"] = level1
            evidence = list(level1.get("evidence") or [])
            stats = dict(level1.get("stats") or {})
        except Exception as exc:  # noqa: BLE001 —— 逐题失败如实记账，不伪装成空结果
            error = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        elapsed_ms = int((_time.monotonic() - started) * 1000)
        labeled_terms = acceptance._weak_label_terms(item)
        cursor = database.connection.cursor()
        try:
            checks = [acceptance._resolve_evidence(cursor, one) for one in evidence]
        finally:
            cursor.close()
        terms = acceptance._terms_of(question, item.get("expect_terms"))
        cited_total, cited_matched, matched_terms = acceptance._citation_counts(
            evidence, labeled_terms)
        # 排序类指标（P17-03 新登记的口径）：第一条命中期待词的证据排在第几位、前 k 条命中几条。
        first_hit_rank = 0
        relevant_at_k = 0
        relevant_ranks: list = []
        if labeled_terms:
            for position, one in enumerate(evidence, 1):
                haystack = acceptance._evidence_haystack(one)
                if any(term in haystack for term in labeled_terms):
                    relevant_at_k += 1
                    relevant_ranks.append(position)
                    if not first_hit_rank:
                        first_hit_rank = position
        graph_items = [one for one in evidence if str(one.get("source_type")) == "graph"]
        row = {
            "id": item.get("id") or index,
            "question": question,
            "kind": item.get("kind") or "",
            "industry_pack_id": pack_id,
            "evidence": len(evidence),
            "hit": bool(evidence),
            "traceable": bool(checks) and all(one["ok"] for one in checks),
            "traceable_ratio": (sum(1 for one in checks if one["ok"]) / len(checks)) if checks else 0.0,
            "grounded": acceptance._grounded(question, evidence, terms),
            "terms": terms,
            "graph": len(graph_items),
            "graph_attribute": sum(1 for one in graph_items
                                   if (one.get("metadata") or {}).get("relation_kind") == "attribute"),
            "graph_event": sum(1 for one in graph_items
                               if (one.get("metadata") or {}).get("relation_kind") == "event"),
            "graph_receipt": {},
            "time_window": {},
            "ms": elapsed_ms,
            "expect_terms_count": len(labeled_terms),
            "expect_terms_hit": len(matched_terms),
            "cited_total": cited_total,
            "cited_matched": cited_matched,
            "citation_hit": bool(cited_matched),
            # —— P17-03 新登记的排序口径（同一批弱标注 expect_terms，不新造标注）——
            "first_hit_rank": first_hit_rank,
            "relevant_at_k": relevant_at_k,
            "relevant_ranks": relevant_ranks,
        }
        if error:
            row["error"] = error
        rows.append(row)
        receipts.append({
            "id": row["id"], "run_id": run_id, "question": question,
            "industry_pack_id": pack_id, "error": error, "ms": elapsed_ms,
            "plan_category": str((plan.get("category") if isinstance(plan, Mapping) else "") or ""),
            "plan_needs_web": bool(plan.get("needs_web")) if isinstance(plan, Mapping) else False,
            "plan_needs_ragflow": bool(plan.get("needs_ragflow")) if isinstance(plan, Mapping) else False,
            "plan_needs_multi_hop": bool(plan.get("needs_multi_hop")) if isinstance(plan, Mapping) else False,
            "evidence": len(evidence),
            "evidence_layer": dict(stats.get("evidence_layer") or {}),
            "verification": dict(stats.get("verification") or {}),
            "execution_graph": {key: value for key, value in (stats.get("execution_graph") or {}).items()
                                if key in ("path", "stop_reason", "node_counts", "budget", "error")},
            "gap_loop": {key: value for key, value in (stats.get("gap_loop") or {}).items()
                         if key in ("stop_reason", "rounds", "gaps", "new_verified_claims",
                                    "resolved_high_priority_gaps", "new_verified_claims_total")},
            "hunter_fleet": dict(stats.get("hunter_fleet") or {}),
            "stats_keys": sorted(stats),
        })
    return {"rows": rows, "receipts": receipts,
            "substitutions": {name: (value.receipt() if hasattr(value, "receipt") else str(value))
                              for name, value in substitutions.items()},
            "questions": len(rows), "limit": int(limit), "mode": mode}


# ---------------------------------------------------------------------------
# 回答侧 E2E：真机真实 run 的规则层重放（生成端草稿 = 真机已录答案）
# ---------------------------------------------------------------------------
def _answer_tools():
    try:
        import tools.qa_phase08_context_acceptance as acc08
        import tools.qa_phase13_final_acceptance as acc13

        return acc08, acc13
    except Exception:  # noqa: BLE001
        return None, None


SNAPSHOT_KEY_ALIASES = (
    ("runs", "qa_runs"), ("claims", "qa_claims"), ("edges", "qa_claim_evidence"),
    ("evidence", "qa_evidence"), ("conflicts", "qa_conflicts"),
    ("stages", "qa_stage_runs"), ("traces", "qa_reasoning_traces"),
)


def normalize_answer_snapshot(snapshot: Mapping) -> dict:
    """把 A 机导出快照的两种键名形态统一成 Phase 08/13 工具认的形状。

    真机导出用过两套键名：`runs/claims/edges/...`（Phase 08/13）与
    `qa_runs/qa_claims/qa_claim_evidence/...`（Phase 12）。**只换键名，不动任何一行数据**：
    两套键同时存在时以原名优先，缺哪个补哪个。
    """
    out = dict(snapshot or {})
    for canonical, alias in SNAPSHOT_KEY_ALIASES:
        if not out.get(canonical) and out.get(alias):
            out[canonical] = out[alias]
    return out


def _recall_reason(memory_recall: Mapping | None, run_meta: Mapping) -> str:
    """召回为空时**说清是哪种空**：开关关 / 真机 run 没有作用域轴 / 作用域内确实没有记忆。"""
    if not memory_recall:
        return "SWITCH_OFF:QA_MEMORY_GRAPH"
    hits = (memory_recall or {}).get("hits") or []
    if hits:
        return ""
    if not str((run_meta or {}).get("owner_user_id") or "") and \
            not str((run_meta or {}).get("session_id") or ""):
        # 真机 run 行没有归属轴：写门会跳过登记、召回没有作用域可查（不是"没有记忆"）
        return "NO_SCOPE_AXIS_IN_REAL_DATA"
    return "NO_MEMORY_IN_SCOPE"


def _empty_answer_totals() -> dict:
    return {
        "runs_total": 0, "runs_scored": 0, "runs_skipped": 0, "claims": 0, "evidence": 0,
        "claims_verified_supported": 0, "unsupported_claim_rate": None,
        "bindings": 0, "bound_in_answer": 0, "citation_labels": 0,
        "citation_checked": 0, "citation_traceable": 0, "citation_traceable_rate": None,
        "counter_evidence": 0, "unresolved_gaps": 0, "contradictions": 0, "claim_coverage": None,
        "gaps": 0, "high_priority_gaps": 0, "stop_reason": "",
        "memory_recalled": 0, "memory_written": 0, "memory_candidates": 0,
        "skills_selected": 0, "skills_considered": 0,
        "context_items": 0, "context_tokens": 0, "context_trimmed": 0,
        "acquisition_actions": 0, "context_gap_suppressed": 0,
        "checks_passed": 0, "checks_total": 0, "checks_pass_rate": None,
        "blocking_before": 0, "blocking_after": 0, "retrieval_calls": 0,
        "insufficient_runs": 0, "no_evidence_runs": 0,
    }


def run_answer_suite(snapshot: Mapping, *, store=None, write_memories: bool = True,
                     max_runs: int = 0) -> dict:
    """对真机 run 逐条重放规则层（核验→证据图→缺口→记忆→技能→上下文包→终验）。

    逐层的接线点与生产 `conflict_review` / `citation_validation` **完全同序**（见 LAYER_SPECS
    的 `wired_at`）；所有开关都是生产那批环境变量，因此消融时改的是真行为。
    """
    acc08, acc13 = _answer_tools()
    if acc08 is None or acc13 is None:
        raise RuntimeError("拿不到 Phase 08/13 验收工具（回答侧指标口径的唯一真源）")
    import qa_pipeline
    from qa_evidence_graph import evidence_graph_enabled
    from qa_final_verifier import final_verifier_enabled, final_verification_layer
    from qa_gap_analyzer import gap_analyzer_enabled
    from qa_skills import skill_router_enabled
    from qa_verifier import verifier_enabled as verification_enabled, verify_evidence_batch

    buckets = acc08._rows_by_run(normalize_answer_snapshot(snapshot))
    rows: list = []
    totals = _empty_answer_totals()
    for run_id, bucket in sorted(buckets.items()):
        if max_runs and len(rows) >= int(max_runs):
            break
        run = bucket.get("run") if isinstance(bucket.get("run"), Mapping) else {}
        question = str(run.get("question_text") or "").strip()
        answer = dict(run.get("final_answer") or {}) if isinstance(run.get("final_answer"), Mapping) else {}
        row = {"run_id": run_id, "mode": str(run.get("mode") or ""), "question": question[:120],
               "layers": {}, "skipped": "", "error": ""}
        if not question:
            row["skipped"] = "缺少问题"
            rows.append(row)
            totals["runs_total"] += 1
            continue
        if not str(answer.get("answer") or ""):
            row["skipped"] = "没有最终答案（生成端草稿缺失）"
            rows.append(row)
            totals["runs_total"] += 1
            continue
        graph, claims, evidence = acc08._graph_for(bucket)
        run_meta = {"id": run_id, "owner_user_id": str(run.get("owner_user_id") or ""),
                    "session_id": str(run.get("session_id") or ""),
                    "industry_pack_id": str(run.get("industry_pack_id") or "")}
        request = {"question": question, "mode": row["mode"],
                   "industry_pack_id": run_meta["industry_pack_id"]}
        plan = {"question": question, "standalone_question": question, "queries": [question],
                "entities": [], "mode": row["mode"]}
        try:
            from qa_query_interpreter import interpret_query

            plan["interpretation"] = interpret_query(question, plan=plan)
        except Exception as exc:  # noqa: BLE001 —— 解释层失败只记账（不猜）
            row["layers"]["interpretation"] = {"error": "%s: %s" % (type(exc).__name__,
                                                                   str(exc)[:120])}
        context = {"run": run_meta, "request": request, "outputs": {}, "store": store}

        # ── P03（证据级）：真机 run 早于核验层接线，证据 payload 上没有核验结论；
        # 生产链路上这一步在 `_apply_evidence_layer` 里做（qa_pipeline.py:636）。这里用**同一个
        # 函数**补跑，把结论挂回证据对象——否则下游（证据图关系、记忆写门）拿不到"已验证证据"，
        # 会一律按 MASTER_RULES 11 拒收，那是"没接线"而不是"层没价值"。
        if verification_enabled():
            try:
                annotated, audit = verify_evidence_batch(
                    [dict(item) for item in graph.get("evidence") or []],
                    claim_text=question, required_entities=qa_pipeline._plan_entities(plan),
                    store=store, pack_id=run_meta["industry_pack_id"])
                by_ref = {str(item.get("evidence_ref") or ""): item for item in annotated}
                graph["evidence"] = [by_ref.get(str(item.get("evidence_ref") or ""), item)
                                     for item in graph.get("evidence") or []]
                row["layers"]["evidence_verification"] = {
                    "state": "on", "verdicts": dict(audit.get("verdicts") or {}),
                    "checked": int(audit.get("checked") or 0),
                    "dropped": int(audit.get("dropped") or 0),
                    "reordered": int(audit.get("reordered") or 0),
                    "error": ""}
            except Exception as exc:  # noqa: BLE001 —— 证据级核验失败只记账
                row["layers"]["evidence_verification"] = {
                    "state": "error", "error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}
        else:
            row["layers"]["evidence_verification"] = {
                "state": "skipped", "reason": "SWITCH_OFF:QA_VERIFIER_ENABLED"}

        # ── P03：claim 级核验（生产接线点 qa_pipeline.py:3710）──
        verification = qa_pipeline._verify_claim_graph_in_place(
            graph, store=store, question=question, run_meta=run_meta)
        row["layers"]["claim_verification"] = {
            "stats": dict(verification.get("stats") or {}),
            "error": str(verification.get("error") or "")}

        # ── P06：证据图 + 矛盾裁决（生产接线点 qa_pipeline.py:3715）──
        evidence_graph = {}
        gap_review = {}
        if evidence_graph_enabled():
            evidence_graph = qa_pipeline._build_evidence_graph_layer(
                graph, plan=plan, run_meta=run_meta)
            if gap_analyzer_enabled() and not evidence_graph.get("error"):
                gap_review = qa_pipeline._attach_gap_review(
                    graph, evidence_graph, plan=plan, previous_stop_reason="")
            graph["evidence_graph"] = evidence_graph
        row["layers"]["evidence_graph"] = {
            "state": "on" if evidence_graph else "skipped",
            "reason": "" if evidence_graph else "SWITCH_OFF:QA_EVIDENCE_GRAPH",
            "coverage": dict((evidence_graph or {}).get("coverage") or {}),
            "stats": dict((evidence_graph or {}).get("stats") or {}),
            "relations": dict((evidence_graph or {}).get("relation_distribution") or {}),
            "error": str((evidence_graph or {}).get("error") or "")}
        row["layers"]["gap_review"] = {
            "state": "on" if gap_review else "skipped",
            "reason": "" if gap_review else ("SWITCH_OFF:QA_GAP_ANALYZER"
                                             if not gap_analyzer_enabled() else "NO_EVIDENCE_GRAPH"),
            "stop_reason": str((gap_review or {}).get("stop_reason") or ""),
            "gaps": len((gap_review or {}).get("gaps") or []),
            "error": str((gap_review or {}).get("error") or "")}

        # ── P09/P10/P15：记忆召回（生产接线点 qa_pipeline.py:3731）──
        memory_recall = qa_pipeline._attach_memory_recall(
            graph, plan=plan, request=request, run_meta=run_meta, store=store)
        memory_revalidation = qa_pipeline._attach_memory_revalidation(
            graph, recall=memory_recall, run_meta=run_meta, store=store, trace_id=run_id)
        if memory_revalidation:
            graph["memory_revalidation"] = memory_revalidation
        memory_items = qa_pipeline._memory_items_from(
            memory_recall, revalidated_hints=memory_revalidation.get("hints") or ())
        row["layers"]["memory_recall"] = {
            "state": "on" if memory_recall else "skipped",
            "reason": _recall_reason(memory_recall, run_meta),
            "hits": len((memory_recall or {}).get("hits") or []),
            "counts": dict((memory_recall or {}).get("counts") or {}),
            "items": len(memory_items),
            "scope_axis": {"owner_user_id": bool(str(run_meta.get("owner_user_id") or "")),
                           "session_id": bool(str(run_meta.get("session_id") or ""))},
            "error": str((memory_recall or {}).get("error") or "")}
        row["layers"]["memory_revalidation"] = {
            "state": "on" if memory_revalidation else "skipped",
            "reason": "" if memory_revalidation else "SWITCH_OFF_OR_NO_HITS",
            "checked": int((memory_revalidation or {}).get("checked") or 0),
            "revalidated": int((memory_revalidation or {}).get("revalidated") or 0)}

        # ── P11：技能路由（生产接线点 qa_pipeline.py:3743）──
        skill_routing = {}
        if skill_router_enabled():
            skill_routing = qa_pipeline._attach_skill_routing(
                graph, plan=plan, request=request, run_meta=run_meta, context=context)
            if skill_routing:
                graph["skill_routing"] = skill_routing
        skill_items = qa_pipeline._skill_items_from(skill_routing)
        row["layers"]["skill_routing"] = {
            "state": "on" if skill_routing else "skipped",
            "reason": "" if skill_routing else "SWITCH_OFF:QA_SKILL_ROUTER",
            "selected": list((skill_routing or {}).get("selected") or []),
            "items": len(skill_items),
            "error": str((skill_routing or {}).get("error") or "")}

        # ── P08 + P14：上下文包与获取计划（生产接线点 qa_pipeline.py:3773）──
        from qa_context_pack import context_pack_enabled

        pack: dict = {}
        if context_pack_enabled():
            gaps = qa_pipeline._skill_gaps_from(context, graph, plan)
            pack = qa_pipeline._build_context_pack_layer(
                graph, plan=plan, request=request, run_meta=run_meta,
                working_memory={}, memory_items=memory_items, skill_items=skill_items, gaps=gaps)
            graph["context_pack"] = pack
        _budget = dict(pack.get("budget") or {})
        row["layers"]["context_pack"] = {
            "state": "on" if pack else "skipped",
            "reason": "" if pack else "SWITCH_OFF:QA_CONTEXT_PACK",
            "items": len(pack.get("items") or []),
            "tokens": int(_budget.get("used") or 0),
            "total_budget": int(_budget.get("total") or 0),
            "trimmed": int(_budget.get("trimmed_items") or 0),
            "context_gaps": len(pack.get("context_gaps") or []),
            "acquisition": dict(pack.get("acquisition") or {}),
            "error": str(pack.get("error") or "")}

        # ── P09：回答后写记忆（生产接线点 qa_pipeline._write_memories_after_answer）──
        memory_write: dict = {}
        if write_memories and store is not None and qa_pipeline.memory_graph_enabled():
            try:
                from qa_memory import write_memories_from_graph

                memory_write = write_memories_from_graph(
                    store, graph=graph, run_meta=run_meta, trace_id=run_id)
            except Exception as exc:  # noqa: BLE001 —— 写记忆失败只记账
                memory_write = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}
        row["layers"]["memory_write"] = {
            "state": "on" if memory_write else "skipped",
            "reason": "" if memory_write else "SWITCH_OFF:QA_MEMORY_GRAPH",
            "candidates": int((memory_write or {}).get("candidates") or 0),
            "persisted": int((memory_write or {}).get("persisted") or 0),
            "decisions": dict((memory_write or {}).get("decision_counts") or {}),
            "reasons": dict((memory_write or {}).get("reason_counts") or {}),
            "error": str((memory_write or {}).get("error") or "")}

        # ── P13：组装 + 六问终验（生产接线点 qa_pipeline.py:1266）──
        # 组装是纯规则（零模型），因此**无论终验开关如何都先算一份**：
        # 它是"before"侧的 grounding 输入，也是终验关掉时的对照口径。
        from qa_final_verifier import _grounding_for, compose_answer

        composition_before = compose_answer(graph=graph, answer=answer, pack=(pack or None),
                                            question=question)
        grounding = _grounding_for(answer, pack=(pack or None), graph=graph,
                                   composition=composition_before)
        layer_out: dict = {}
        if final_verifier_enabled():
            # stop_reason 口径与生产 `_final_verification_step`（qa_pipeline.py:1282）**逐字一致**：
            # 缺口复核的停止原因优先，没有就用多跳缺口环的；都没有就是空串（不编 ANSWERABLE）。
            layer_out = final_verification_layer(
                graph=graph, answer=answer, pack=(pack or None), question=question,
                stop_reason=str((gap_review or {}).get("stop_reason") or ""),
                routes=qa_pipeline._final_verify_routes(graph), store=store, run_id=run_id)
        row["layers"]["final_verifier"] = {
            "state": "on" if layer_out else "skipped",
            "reason": "" if layer_out else "SWITCH_OFF:QA_FINAL_VERIFIER",
            "receipt": dict((layer_out or {}).get("receipt") or {}),
            "stats": dict(((layer_out or {}).get("verification") or {}).get("stats") or {}),
            "stop_reason": str((gap_review or {}).get("stop_reason") or ""),
            "error": str((layer_out or {}).get("error") or "")}

        composition = layer_out.get("composition") if layer_out else None
        if not isinstance(composition, Mapping):
            composition = composition_before
        # claim 级核验统计**只认 `verify_claim_graph` 那一份**（MASTER_RULES 11 的唯一判据）；
        # 终验自己的 stats 是另一套语义（六问），单独放在 final_verifier 图层里，绝不混用。
        verify_stats = dict(row["layers"]["claim_verification"].get("stats") or {})
        row["composition"] = acc13._composition_facts(composition)
        row["before"] = {
            "blocking": bool(grounding.get("blocking")),
            "blocking_codes": list(grounding.get("blocking_codes") or []),
            "status": str(answer.get("status") or ""),
            "verified_claims": row["composition"]["verified_claims"]}
        row["after"] = (acc13._verification_facts(layer_out.get("verification") or {},
                                                  dict(layer_out.get("receipt") or {}),
                                                  retries=layer_out.get("retries"))
                        if layer_out else {"checks_passed": 0, "checks_total": 0,
                                           "failed_checks": [], "blocking_codes": [],
                                           "outcome": "skipped", "insufficient": False,
                                           "insufficient_reasons": [], "retries": []})
        row["answer_status"] = str((layer_out.get("answer") or {}).get("status") or "") if layer_out \
            else str(answer.get("status") or "")
        row["no_evidence"] = not evidence
        row["claims"] = len(claims)
        row["evidence"] = len(evidence)
        row["verify_stats"] = verify_stats
        # 引用可回溯率：复用 Phase 08 的同一条 span 复算（唯一口径）
        pack_for_trace = {"citation_index": {
            label: {"evidence_ref": entry.get("evidence_ref"),
                    "evidence_id": entry.get("evidence_id"),
                    "quote": entry.get("quote"), "span": entry.get("span"),
                    "grounded": entry.get("grounded")}
            for label, entry in (composition.get("citation_index") or {}).items()
            if isinstance(entry, Mapping)}}
        row["traceability"] = acc08._traceability(pack_for_trace, graph)
        rows.append(row)

        # ── 汇总 ──
        totals["runs_total"] += 1
        totals["runs_scored"] += 1
        totals["claims"] += len(claims)
        totals["evidence"] += len(evidence)
        totals["claims_verified_supported"] += int(verify_stats.get("confirmed") or 0)
        totals["bindings"] += int(row["composition"]["claims"] or 0)
        totals["bound_in_answer"] += int(row["composition"]["bound_in_answer"] or 0)
        totals["citation_labels"] += int(row["composition"]["citation_labels"] or 0)
        totals["citation_checked"] += int(row["traceability"]["checked"] or 0)
        totals["citation_traceable"] += int(row["traceability"]["traceable"] or 0)
        totals["counter_evidence"] += int(row["composition"]["counter_evidence"] or 0)
        totals["unresolved_gaps"] += int(row["composition"]["unresolved_gaps"] or 0)
        totals["contradictions"] += int(row["composition"]["contradictions"] or 0)
        totals["gaps"] += int(row["layers"]["gap_review"]["gaps"] or 0)
        totals["memory_recalled"] += int(row["layers"]["memory_recall"]["hits"] or 0)
        totals["memory_written"] += int(row["layers"]["memory_write"]["persisted"] or 0)
        totals["memory_candidates"] += int(row["layers"]["memory_write"]["candidates"] or 0)
        totals["skills_selected"] += len(row["layers"]["skill_routing"]["selected"] or [])
        totals["context_items"] += int(row["layers"]["context_pack"]["items"] or 0)
        totals["context_tokens"] += int(row["layers"]["context_pack"]["tokens"] or 0)
        totals["context_trimmed"] += int(row["layers"]["context_pack"]["trimmed"] or 0)
        totals["acquisition_actions"] += len(
            (row["layers"]["context_pack"]["acquisition"] or {}).get("actions") or [])
        totals["context_gap_suppressed"] += int(
            ((row["layers"]["context_pack"]["acquisition"] or {}).get("stats") or {}
             ).get("suppressed") or 0)
        totals["checks_passed"] += int(row["after"].get("checks_passed") or 0)
        totals["checks_total"] += int(row["after"].get("checks_total") or 0)
        totals["blocking_before"] += 1 if row["before"]["blocking"] else 0
        totals["blocking_after"] += 1 if row["after"].get("blocking_codes") else 0
        totals["retrieval_calls"] += sum(int(item.get("retrieval_calls") or 0)
                                         for item in (row["after"].get("retries") or []))
        totals["insufficient_runs"] += 1 if row["after"].get("insufficient") else 0
        totals["no_evidence_runs"] += 1 if row["no_evidence"] else 0
        coverage = (row["layers"]["evidence_graph"]["coverage"] or {}).get("claim_coverage")
        if isinstance(coverage, (int, float)):
            totals.setdefault("_coverage_values", []).append(float(coverage))
        stop = str(row["layers"]["gap_review"]["stop_reason"] or "")
        if stop:
            totals["stop_reason"] = stop

    coverage_values = totals.pop("_coverage_values", [])
    totals["claim_coverage"] = (round(sum(coverage_values) / len(coverage_values), 6)
                               if coverage_values else None)
    totals["citation_traceable_rate"] = (round(totals["citation_traceable"]
                                               / float(totals["citation_checked"]), 6)
                                         if totals["citation_checked"] else None)
    totals["checks_pass_rate"] = (round(totals["checks_passed"] / float(totals["checks_total"]), 6)
                                  if totals["checks_total"] else None)
    totals["unsupported_claim_rate"] = (
        round(1.0 - (totals["claims_verified_supported"] / float(totals["claims"])), 6)
        if totals["claims"] else None)
    return {"rows": rows, "totals": totals}


def qa_pipeline_mod():
    """小工具：拿 qa_pipeline 模块（避免在模块顶部引入它的重依赖）。"""
    import qa_pipeline

    return qa_pipeline


def retrieval_metrics(rows: Sequence[Mapping], *, limit: int, benchmark_meta=None,
                      cost=None) -> dict:
    """检索侧指标：**直接调** Phase 00 的 summarize（唯一口径）。"""
    acceptance = _acceptance_module()
    if acceptance is None:
        return {"caliber_missing": True,
                "reason": "tools.qa_retrieval_acceptance 不可用；不另算一套指标"}
    return acceptance.summarize(list(rows), limit=int(limit), benchmark_meta=benchmark_meta,
                                cost=cost)


def run_suite(database, questions: Sequence[Mapping], snapshot: Mapping | None = None, *,
              store=None, limit: int = 12, mode: str = "standard",
              benchmark_meta=None, answer_store=None, write_memories: bool = True) -> dict:
    """一段式入口：检索侧 + 回答侧（可选）一起跑，返回指标与逐层回执。"""
    caches = reset_process_caches()
    retrieval = run_retrieval_suite(database, questions, limit=limit, mode=mode, store=store)
    metrics = retrieval_metrics(retrieval["rows"], limit=limit, benchmark_meta=benchmark_meta)
    answer = {"rows": [], "totals": _empty_answer_totals()}
    if snapshot:
        answer = run_answer_suite(snapshot, store=answer_store, write_memories=write_memories)
    return {
        "suite_version": E2E_SUITE_VERSION,
        "layers": layer_states(),
        "cache_reset": caches,
        "retrieval": retrieval,
        "retrieval_metrics": metrics,
        "answer": answer,
        "answer_totals": answer["totals"],
        "substitutions": retrieval["substitutions"],
    }


def no_object_report(retrieval_receipts: Sequence[Mapping],
                     answer_totals: Mapping) -> dict:
    """如实标注"哪些层在本轮数据上**无对象**"（不是零分，是没有可判的东西）。"""
    report = {}
    layer_hits = {}
    for receipt in retrieval_receipts:
        for layer in ("evidence_layer", "verification", "execution_graph", "gap_loop", "hunter_fleet"):
            payload = receipt.get(layer) or {}
            if payload:
                layer_hits[layer] = layer_hits.get(layer, 0) + 1
    report["retrieval"] = {
        "hunter_fleet": {"objects": layer_hits.get("hunter_fleet", 0),
                         "reason_code": "" if layer_hits.get("hunter_fleet") else "SWITCH_OFF"},
        "execution_graph": {"objects": layer_hits.get("execution_graph", 0),
                            "reason_code": "" if layer_hits.get("execution_graph") else "SWITCH_OFF"},
        "gap_loop": {"objects": layer_hits.get("gap_loop", 0),
                     "reason_code": "" if layer_hits.get("gap_loop") else "SWITCH_OFF_OR_NO_GAP"},
        "web_channel": {"objects": 0, "reason_code": "OFFLINE_NO_NETWORK"},
        "level2_ragflow": {"objects": 0, "reason_code": "OFFLINE_NO_RAGFLOW"},
    }
    report["answer"] = {
        "contradiction_resolution": {
            "objects": int(answer_totals.get("contradictions") or 0),
            "reason_code": "" if int(answer_totals.get("contradictions") or 0)
                           else "NO_CONFLICT_ROW_IN_REAL_DATA"},
        "counter_evidence": {
            "objects": int(answer_totals.get("counter_evidence") or 0),
            "reason_code": "" if int(answer_totals.get("counter_evidence") or 0)
                           else "NO_REFUTES_EDGE_IN_REAL_DATA"},
        "memory_online": {"objects": 0,
                          "reason_code": "ONLINE_MEMORY_ITEM_ZERO_ROWS（快照口径下由同代码重建）"},
        "memory_recall_scope": {
            "objects": int(answer_totals.get("memory_recalled") or 0),
            "reason_code": "" if int(answer_totals.get("memory_recalled") or 0)
                           else "NO_SCOPE_AXIS_IN_REAL_RUNS（真机 run 行没有 owner/session 轴，"
                                "召回没有作用域可查 —— 不是「零命中」）"},
        "memory_write": {
            "objects": int(answer_totals.get("memory_written") or 0),
            "candidates": int(answer_totals.get("memory_candidates") or 0),
            "reason_code": "" if int(answer_totals.get("memory_written") or 0)
                           else "ALL_CANDIDATES_DROPPED_BY_WRITE_GATE"},
    }
    return report


def fingerprint(value) -> str:
    """内容寻址指纹（报告里任何"同一份东西"都用它对齐）。"""
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def phase00_baseline(history_path: str = os.path.join("data", "qa_acceptance_history.jsonl")) -> dict:
    """Phase 00 冻结的 baseline（**同口径的原始留档行**，不做任何换算）。"""
    out = {"history_path": history_path, "rows": [], "frozen": None, "legacy": None}
    if not os.path.isfile(history_path):
        out["reason_code"] = "NO_HISTORY_FILE"
        return out
    with open(history_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                out["rows"].append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
    for row in out["rows"]:
        summary = row.get("summary") or {}
        if not summary.get("benchmark_version"):
            out["legacy"] = out["legacy"] or row
        elif str(summary.get("benchmark_version")).startswith("qa-acceptance"):
            out["frozen"] = out["frozen"] or row
    out["reason_code"] = "" if out["frozen"] else "NO_FROZEN_BENCHMARK_ROW"
    return out


def metric_delta(before: Mapping, after: Mapping, keys: Iterable = E2E_METRIC_KEYS) -> dict:
    """before/after 差值：任一侧缺失（null）就写 null，**不用 0 充数**。"""
    delta = {}
    for key in keys:
        left = (before or {}).get(key)
        right = (after or {}).get(key)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            delta[key] = round(float(right) - float(left), 6)
        else:
            delta[key] = None
    return delta


def corpus_disjointness(questions: Sequence[Mapping], packs: Sequence[str]) -> dict:
    """benchmark 题目与快照语料的交集：**快照里没有的包**要如实标"无对象"。"""
    in_scope, out_of_scope = [], []
    pack_set = {str(item) for item in packs}
    for item in questions:
        pack = str(item.get("industry_pack_id") or "")
        (in_scope if pack in pack_set else out_of_scope).append(
            {"id": item.get("id"), "industry_pack_id": pack,
             "reason_code": "" if pack in pack_set else "NO_CORPUS_IN_SNAPSHOT"})
    return {"in_scope": in_scope, "out_of_scope": out_of_scope,
            "in_scope_count": len(in_scope), "out_of_scope_count": len(out_of_scope)}


__all__ = [
    "E2E_SUITE_VERSION", "LAYER_SPECS", "LAYER_IDS", "LAYER_BY_ID", "E2E_METRIC_KEYS",
    "RETRIEVAL_ROW_KEYS", "OFFLINE_SUBSTITUTIONS", "OfflineWebSearch", "OfflineProviderRegistry",
    "layer_states", "layer_switches", "reset_process_caches", "build_corpus_template",
    "clone_database", "build_offline_handlers", "run_retrieval_suite", "run_answer_suite",
    "run_suite", "retrieval_metrics", "no_object_report", "fingerprint", "phase00_baseline",
    "metric_delta", "corpus_disjointness", "qa_pipeline_mod",
]
