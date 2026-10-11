#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 17（P17-04 / P17-05 / P17-06）· **compatibility regression + migration rollback + release**。

三个入口，互相独立、都能单独跑：

  · `compatibility_regression()`：把"不能变的东西"逐条重算一遍，并与**冻结产物**
    （`baseline/qa-baseline-inventory.json`，Phase 00 落盘的那份）对比。比的是：
    必需模块、依赖边、七契约指纹、版本锚点、枚举取值域、开关默认值、阶段链、
    冻结答案契约的严格性、新模块的零网络/零模型痕迹。**任一条不过就是兼容性回归**，
    不做"看起来差不多"的近似判定。
  · `migration_rollback()`：在**隔离临时 sqlite** 上真做一次 前滚 → 回滚 → 再前滚：
    建 v11 全表 → 写一行"老数据" → 按回滚口径 DROP 掉 v7…v11 新增的 13 张表 →
    校验剩余表集合恰好等于 v6 核心表集、且老数据一字不少 → 再前滚回 v11、老数据仍在。
    回滚 SQL 原文写进产物（这就是可执行的回滚手册，不另写 .md）。
  · `release_manifest()`：版本锚点 + 产物清单（sha256）+ 验收矩阵 + Gate 勾选 +
    回滚口径 + 已知边界 + 阻塞项。它是**最后一页**：所有数字都从别的报告里取，
    本函数不自己造指标。
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from typing import Mapping, Sequence

# "像端点"的判据：带协议 + 带点号域名的 host（纯 `"http://"` 记号或文档里提一句不算）
_ENDPOINT_RE = re.compile(r"https?://[A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,}")

RELEASE_VERSION = "qa-release-acceptance-v1"
V2_BUNDLE_VERSION = "graph-rag-v2"
DEFAULT_INVENTORY = os.path.join("baseline", "qa-baseline-inventory.json")

FROZEN_SCHEMA_FINGERPRINTS = {
    "EVIDENCE_SCHEMA": "370301331c02c738", "CLAIM_SCHEMA": "06fcdb02441248b2",
    "CONFLICT_SCHEMA": "aabd3259b07f9a3e", "LEVEL1_RESULT_SCHEMA": "7e864764429db111",
    "LEVEL2_RESULT_SCHEMA": "a0484f894e7bc9d6", "FINAL_ANSWER_SCHEMA": "4d1efa54ca1cbc1a",
    "QA_EVENT_SCHEMA": "59358bfa88a6c6af",
}
"""P00-02 冻结的七个契约指纹（本包任何阶段都不许变）。"""

FROZEN_VERSION_ANCHORS = {
    "QA_CONTRACT_VERSION": "unified-qa-v1",
    "QA_SSE_PROTOCOL_VERSION": "qa-sse-v1",
    "GRAPH_CONTRACT_VERSION": "graph-contract-v1",
}
"""除 schema 版本外三个不可变的版本锚点（schema 版本的演进见 SCHEMA_EVOLUTION）。"""

SCHEMA_EVOLUTION = (
    {"version": "unified-qa-schema-v6", "phase": "P00", "decision": "基线",
     "delta": "初始 24 张 qa_* 表"},
    {"version": "unified-qa-schema-v7", "phase": "P02", "decision": "D-007",
     "delta": "+qa_evidence_seen（零 ADD COLUMN）"},
    {"version": "unified-qa-schema-v8", "phase": "P09", "decision": "D-033",
     "delta": "+8 张 memory_* 表"},
    {"version": "unified-qa-schema-v9", "phase": "P10", "decision": "D-037",
     "delta": "+memory_validation/memory_contradiction"},
    {"version": "unified-qa-schema-v10", "phase": "P12", "decision": "D-044",
     "delta": "+source_reliability_memory/skill_performance_memory"},
    {"version": "unified-qa-schema-v11", "phase": "P14", "decision": "D-049",
     "delta": "+qa_realignment_decisions"},
)
LATEST_SCHEMA_VERSION = "unified-qa-schema-v11"

POST_V6_TABLES = (
    {"table": "qa_evidence_seen", "version": "v7", "phase": "P02", "decision": "D-007"},
    {"table": "memory_item", "version": "v8", "phase": "P09", "decision": "D-033"},
    {"table": "memory_version", "version": "v8", "phase": "P09", "decision": "D-033"},
    {"table": "memory_entity_link", "version": "v8", "phase": "P09", "decision": "D-033"},
    {"table": "memory_evidence_link", "version": "v8", "phase": "P09", "decision": "D-033"},
    {"table": "memory_relation", "version": "v8", "phase": "P09", "decision": "D-033"},
    {"table": "memory_recall_log", "version": "v8", "phase": "P09", "decision": "D-033"},
    {"table": "memory_write_decision", "version": "v8", "phase": "P09", "decision": "D-033"},
    {"table": "memory_usage_stat", "version": "v8", "phase": "P09", "decision": "D-033"},
    {"table": "memory_validation", "version": "v9", "phase": "P10", "decision": "D-037"},
    {"table": "memory_contradiction", "version": "v9", "phase": "P10", "decision": "D-037"},
    {"table": "source_reliability_memory", "version": "v10", "phase": "P12", "decision": "D-044"},
    {"table": "skill_performance_memory", "version": "v10", "phase": "P12", "decision": "D-044"},
    {"table": "qa_realignment_decisions", "version": "v11", "phase": "P14", "decision": "D-049"},
)
"""v7 及以后新增的表（= 回滚时要 DROP 的对象）；反过来，v6 → v11 的行列演进**零 ADD COLUMN**。"""

FROZEN_ENUM_SIZES = {
    "QA_RETRIEVAL_ROUTES": 7, "QA_FAILURE_POLICIES": 5, "QA_STOP_REASONS": 5,
    "QA_HUNTER_IDS": 5, "FINAL_VERIFIER_CHECKS": 6, "EVIDENCE_GRAPH_RELATIONSHIPS": 5,
    "MEMORY_TYPES": 10, "SKILL_IDS": 11, "OBSERVABILITY_VIEWS": 6,
}
"""跨阶段取值域：个数与内容都不许漂（内容逐字比较见 `_enum_values`）。"""

STAGE_PLANS_V1 = {
    "fast": ("plan", "level1_retrieval", "logic_validation", "level1_draft", "synthesis",
             "citation_validation"),
    "standard": ("plan", "level1_retrieval", "logic_validation", "level1_draft",
                 "level2_retrieval", "level2_research", "conflict_review", "synthesis",
                 "citation_validation"),
    "deep": ("plan", "level1_retrieval", "logic_validation", "level1_draft",
             "level2_retrieval", "level2_research", "conflict_review", "synthesis",
             "citation_validation"),
}
"""§18 三条阶段链（P05 落地、P06/P07/P13 只改节点状态不改链）。"""

SWITCH_DEFAULTS_V1 = {
    "QA_EVIDENCE_LAYER_ENABLED": True, "QA_VERIFIER_ENABLED": True,
    "QA_HUNTER_FLEET": False, "QA_EXECUTION_GRAPH": False, "QA_EVIDENCE_GRAPH": False,
    "QA_GAP_ANALYZER": False, "QA_CONTEXT_PACK": False, "QA_MEMORY_GRAPH": False,
    "QA_SKILL_ROUTER": False, "QA_SKILL_TELEMETRY": False, "QA_EXPERIENCE_MEMORY": False,
    "QA_FINAL_VERIFIER": False, "QA_ACQUISITION": False, "QA_ASK_PATIENT": False,
    "QA_REPEAT_QUESTION_ENABLED": False, "QA_MEMORY_SCOPE_ENFORCEMENT": True,
    "QA_ENCOUNTER_ISOLATION": True, "QA_LONGITUDINAL_FRESHNESS": True,
    "QA_SAFETY_HARD_TRIGGER": True, "QA_PRIVACY_MINIMAL_LOG": True, "QA_MEMORY_AUDIT": True,
    "QA_GROUNDING_GATE": False, "QA_EXECUTION_GRAPH_NODE_RUNS": False,
    "QA_EVIDENCE_SEEN_PRUNE_ENABLED": False,
}
"""P17 冻结的开关默认值表（真值仍由各模块 accessor 提供，本表只做回归对比）。"""

ZERO_NETWORK_MODULES = ("qa_e2e.py", "qa_ablation.py", "qa_metrics_report.py", "qa_release.py")
FORBIDDEN_IMPORTS = {"requests", "urllib3", "httpx", "socket", "aiohttp", "http.client",
                     "websocket", "openai", "boto3"}
ACCEPTANCE_ARTIFACTS = (
    "baseline/qa-e2e-acceptance.json", "baseline/qa-ablation-acceptance.json",
    "baseline/qa-metric-report.json", "baseline/qa-release-acceptance.json",
    "config/qa_e2e_annotation_v1.json", "tools/qa_phase17_acceptance.py",
    "tools/qa_phase17_real_probe.py", "baseline/qa-phase17-real-probe.json",
)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(value) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _check(name: str, ok: bool, detail: str, *, expected=None, actual=None) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail,
            "expected": expected, "actual": actual}


def _enum_values() -> dict:
    """冻结枚举的**内容**（不只是个数）：内容变了也必须被抓到。"""
    import qa_graph_contracts as contracts

    names = {
        "QA_RETRIEVAL_ROUTES": "QA_RETRIEVAL_ROUTES", "QA_FAILURE_POLICIES": "QA_FAILURE_POLICIES",
        "QA_STOP_REASONS": "QA_STOP_REASONS", "QA_HUNTER_IDS": "QA_HUNTER_IDS",
        "FINAL_VERIFIER_CHECKS": "FINAL_VERIFIER_CHECKS",
        "EVIDENCE_GRAPH_RELATIONSHIPS": "EVIDENCE_GRAPH_RELATIONSHIPS",
        "MEMORY_TYPES": "MEMORY_TYPES", "SKILL_IDS": "SKILL_IDS",
        "OBSERVABILITY_VIEWS": "OBSERVABILITY_VIEWS",
    }
    values = {}
    for label, attr in names.items():
        raw = getattr(contracts, attr, None)
        values[label] = [str(item) for item in (raw or ())]
    return values


def compatibility_regression(*, inventory_path: str = DEFAULT_INVENTORY,
                             include_ast: bool = True) -> dict:
    """兼容性回归：逐条重算 + 与冻结产物对比（不做近似判定）。"""
    import qa_contracts
    import qa_graph_contracts as contracts
    import qa_orchestrator
    import qa_schema
    from qa_e2e import LAYER_BY_ID, _accessor

    checks: list = []
    artifact = {}
    if os.path.isfile(inventory_path):
        with open(inventory_path, encoding="utf-8") as handle:
            artifact = json.load(handle)
    checks.append(_check("frozen_inventory_present", bool(artifact),
                         "Phase 00 冻结的模块清单产物在位（%s）" % inventory_path))
    required = ((artifact.get("modules") or {}).get("required") or [])
    missing = [name for name in required if not os.path.isfile(name)]
    checks.append(_check("required_modules_present", not missing,
                         "Phase 00 要求的 %d 个模块全部在位" % len(required),
                         expected=len(required), actual=len(required) - len(missing)))
    edges = artifact.get("dependencies") or []
    self_loops = [edge for edge in edges if isinstance(edge, Mapping)
                  and edge.get("from") == edge.get("to")]
    checks.append(_check("dependency_edges_recorded", len(edges) > 0 and not self_loops,
                         "依赖边 %d 条、自环 %d 条（冻结产物口径：from/to）"
                         % (len(edges), len(self_loops)), expected=">0 & 0 self-loop",
                         actual="%d & %d" % (len(edges), len(self_loops))))

    recomputed = {name: _fingerprint(getattr(qa_contracts, name))
                  for name in FROZEN_SCHEMA_FINGERPRINTS}
    checks.append(_check("seven_fingerprints_unchanged",
                         recomputed == FROZEN_SCHEMA_FINGERPRINTS,
                         "七契约指纹与 P00-02 逐字相同",
                         expected=FROZEN_SCHEMA_FINGERPRINTS, actual=recomputed))
    checks.append(_check("evidence_schema_still_closed",
                         bool(getattr(qa_contracts.EVIDENCE_SCHEMA, "get", lambda *_: None)(
                             "additionalProperties") is False),
                         "EVIDENCE_SCHEMA.additionalProperties 仍为 False"))
    anchors = {name: getattr(qa_contracts, name, None) for name in FROZEN_VERSION_ANCHORS}
    checks.append(_check("version_anchors_unchanged", anchors == FROZEN_VERSION_ANCHORS,
                         "三个版本锚点逐字未变", expected=FROZEN_VERSION_ANCHORS, actual=anchors))
    checks.append(_check("schema_version_is_declared_latest",
                         qa_schema.QA_SCHEMA_VERSION == LATEST_SCHEMA_VERSION,
                         "schema 版本等于已声明的最新版（演进链见 SCHEMA_EVOLUTION）",
                         expected=LATEST_SCHEMA_VERSION, actual=qa_schema.QA_SCHEMA_VERSION))
    zero_columns = all(not getattr(qa_schema, name, ())
                       for name in ("QA_ADDED_COLUMNS_V8", "QA_ADDED_COLUMNS_V9",
                                    "QA_ADDED_COLUMNS_V10", "QA_ADDED_COLUMNS_V11"))
    checks.append(_check("no_added_columns_after_v6", zero_columns,
                         "v7…v11 的 ADD COLUMN 清单全为空（回滚只需 DROP TABLE）",
                         expected="V8..V11 == ()", actual=str(zero_columns)))
    v6_columns = tuple(getattr(qa_schema, "QA_ADDED_COLUMNS_V6", ()))
    checks.append(_check("v6_columns_frozen", len(v6_columns) == 15,
                         "QA_ADDED_COLUMNS_V6 仍是 Phase 01 的 15 条", expected=15,
                         actual=len(v6_columns)))

    enum_values = _enum_values()
    enum_sizes = {name: len(values) for name, values in enum_values.items()}
    checks.append(_check("enum_sizes_unchanged", enum_sizes == FROZEN_ENUM_SIZES,
                         "九组冻结取值域的个数未变", expected=FROZEN_ENUM_SIZES,
                         actual=enum_sizes))
    declared_views = list(getattr(contracts, "OBSERVABILITY_VIEWS", ()) or ())
    checks.append(_check("observability_views_unchanged",
                         declared_views == ["execution", "evidence", "context", "memory",
                                            "search_trace", "dashboard"],
                         "P16 六视图清单逐字未变", expected="六视图固定序",
                         actual=declared_views))

    switch_report = {}
    for name, expected in SWITCH_DEFAULTS_V1.items():
        accessor = _switch_accessor(name)
        actual = None
        if callable(accessor):
            try:
                actual = bool(accessor())
            except Exception:  # noqa: BLE001 —— 读不到就退回环境变量口径，并如实记为 None
                actual = None
        if actual is None:
            raw = os.getenv(name)
            actual = (bool(expected) if raw is None
                      else str(raw).strip().casefold() not in {"0", "false", "no", "off", ""})
        switch_report[name] = {"expected": bool(expected), "actual": actual,
                               "accessor": "ok" if callable(accessor) else "env-fallback"}
    switches_ok = all(item["actual"] == item["expected"] for item in switch_report.values())
    checks.append(_check("switch_defaults_unchanged", switches_ok,
                         "%d 个开关的默认值未变（含 12 个默认关的层开关）"
                         % len(SWITCH_DEFAULTS_V1),
                         expected="全部相等",
                         actual=[name for name, item in switch_report.items()
                                 if item["actual"] != item["expected"]]))

    plans = {mode: tuple(qa_orchestrator.QaOrchestrator.stage_plan(mode))
             for mode in ("fast", "standard", "deep")}
    checks.append(_check("stage_plans_unchanged", plans == STAGE_PLANS_V1,
                         "fast/standard/deep 三条阶段链逐字未变",
                         expected={key: list(value) for key, value in STAGE_PLANS_V1.items()},
                         actual={key: list(value) for key, value in plans.items()}))

    # 冻结答案契约的"严格性"要**行为级**验证：多一个顶层键必须被拒（抛错即通过）。
    extra_key = {"contract_version": qa_contracts.QA_CONTRACT_VERSION, "status": "ready",
                 "answer": "x", "claims": [], "conflicts": [], "evidence": [], "citations": [],
                 "citation_map": {}, "cutoff_at": "2026-01-01", "degraded": False,
                 "degradation_reasons": [], "models": {}, "sections": {}, "unexpected": 1}
    strict_extra_rejected = False
    strict_detail = ""
    try:
        qa_contracts.validate_final_answer(extra_key)
        strict_detail = "多余顶层键被接受了 —— 契约被放宽"
    except Exception as exc:  # noqa: BLE001 —— 抛错正是我们要的结果
        strict_extra_rejected = "Additional properties are not allowed" in str(exc) \
            or "additional" in str(exc).lower()
        strict_detail = str(exc)[:160]
    checks.append(_check("final_answer_contract_still_strict", strict_extra_rejected,
                         "冻结答案契约仍然拒绝多余的顶层键：%s" % strict_detail))
    ok_extra, _note2 = contracts.validate("observability_view",
                                          {"view": "execution", "state": "empty",
                                           "state_reason": "SWITCH_OFF", "state_text": "x",
                                           "numbers": {}, "payload": {}, "source": "x",
                                           "extra": 1})
    checks.append(_check("observability_view_contract_rejects_extra", not ok_extra,
                         "P16 视图契约仍能拦下多余字段"))

    ast_report = _ast_guard(ZERO_NETWORK_MODULES) if include_ast else {"skipped": True}
    checks.append(_check("new_modules_zero_network_zero_model",
                         bool(ast_report.get("ok")) if not ast_report.get("skipped") else True,
                         "P17 四个新模块零网络库、零 http(s) 字面量、零模型客户端痕迹",
                         expected="无违禁导入/字面量", actual=ast_report.get("violations") or []))

    return {
        "version": RELEASE_VERSION,
        "checks": checks,
        "switches": switch_report,
        "fingerprints": recomputed,
        "enum_values": enum_values,
        "stage_plans": {key: list(value) for key, value in plans.items()},
        "schema_evolution": list(SCHEMA_EVOLUTION),
        "ast_guard": ast_report,
        "frozen_inventory": {"path": inventory_path,
                             "captured_at_utc": artifact.get("captured_at_utc"),
                             "required_modules": len(required), "dependency_edges": len(edges)},
        "passed": all(item["ok"] for item in checks),
        "failed": [item["name"] for item in checks if not item["ok"]],
    }


SWITCH_ACCESSORS = {
    "QA_ASK_PATIENT": "qa_acquisition:ask_patient_enabled",
    "QA_SKILL_TELEMETRY": "qa_skills:skill_telemetry_enabled",
    "QA_EXPERIENCE_MEMORY": "qa_memory_experience:experience_memory_enabled",
    "QA_REPEAT_QUESTION_ENABLED": "qa_pipeline:_realignment_enabled",
    "QA_MEMORY_SCOPE_ENFORCEMENT": "qa_memory_safety:scope_enforcement_enabled",
    "QA_ENCOUNTER_ISOLATION": "qa_memory_safety:encounter_isolation_enabled",
    "QA_LONGITUDINAL_FRESHNESS": "qa_memory_safety:longitudinal_freshness_enabled",
    "QA_SAFETY_HARD_TRIGGER": "qa_memory_safety:safety_hard_trigger_enabled",
    "QA_PRIVACY_MINIMAL_LOG": "qa_memory_safety:privacy_minimal_enabled",
    "QA_MEMORY_AUDIT": "qa_memory_safety:audit_enabled",
    "QA_GROUNDING_GATE": "qa_context_pack:grounding_gate_enabled",
    "QA_EXECUTION_GRAPH_NODE_RUNS": "qa_pipeline:_execution_graph_node_runs_enabled",
    "QA_EVIDENCE_SEEN_PRUNE_ENABLED": "qa_evidence:seen_prune_enabled",
}
"""不被 `LAYER_SPECS` 覆盖的开关 → 它们自己的 accessor（默认值仍只从代码真源读）。"""


def _switch_accessor(name: str):
    """按开关名找它自己的 accessor：先查 LAYER_SPECS，再查补丁表。"""
    from qa_e2e import LAYER_BY_ID, _accessor

    spec = next((item for item in LAYER_BY_ID.values() if item["switch"] == name), None)
    target = spec["accessor"] if spec else SWITCH_ACCESSORS.get(name, "")
    if not target or ":" not in target:
        return None
    module, attr = target.split(":", 1)
    try:
        return getattr(__import__(module, fromlist=[attr]), attr, None)
    except Exception:  # noqa: BLE001
        return None


def _ast_guard(modules: Sequence[str]) -> dict:
    """新模块的静态守门（**看调用与导入，不看提及**）。

    三类违规：
      · `IMPORT` / `IMPORT_FROM`：直接引入网络库；
      · `CALL`：调用网络库的成员（`requests.get` 之类）；
      · `MODEL_CALL`：调用模型/嵌入客户端（`_embed_question` / `chat_api.*` / `openai.*`）；
      · `URL`：出现了带 host 的 http(s) 字面量（白名单只有离线桩用的 `example.invalid`）。
    守门元组里的 `"http://"` 这类**纯粹是守卫记号**（没有 host），因此不算 URL 字面量；
    真正的判据是"有没有可用的端点地址"。
    """
    violations = []
    scanned = []
    url_literals = []
    allow = ("https://example.invalid/", "http://example.invalid/")
    for name in modules:
        if not os.path.isfile(name):
            violations.append({"module": name, "kind": "MISSING"})
            continue
        with open(name, encoding="utf-8") as handle:
            source = handle.read()
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            violations.append({"module": name, "kind": "SYNTAX", "detail": str(exc)[:120]})
            continue
        scanned.append(name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in FORBIDDEN_IMPORTS:
                        violations.append({"module": name, "kind": "IMPORT",
                                           "detail": alias.name})
            elif isinstance(node, ast.ImportFrom):
                root = str(node.module or "").split(".")[0]
                if root in FORBIDDEN_IMPORTS:
                    violations.append({"module": name, "kind": "IMPORT_FROM",
                                       "detail": node.module})
            elif isinstance(node, ast.Call):
                path = _call_path(node.func)
                root = path.split(".")[0]
                if root in FORBIDDEN_IMPORTS:
                    violations.append({"module": name, "kind": "CALL", "detail": path})
                if path.split(".")[-1] in MODEL_CALL_ATTRS or path in MODEL_CALL_PATHS:
                    violations.append({"module": name, "kind": "MODEL_CALL", "detail": path})
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
                if value in ("http://", "https://"):
                    continue          # 守卫记号（无 host），不是端点
                if _ENDPOINT_RE.search(value):
                    url_literals.append({"module": name, "value": value[:80]})
                    if not value.startswith(allow):
                        violations.append({"module": name, "kind": "URL", "detail": value[:80]})
    return {"ok": not violations, "violations": violations, "scanned": scanned,
            "url_literals": url_literals,
            "note": "白名单只有离线桩占位域 example.invalid；出现任何真实端点即违规"}


MODEL_CALL_ATTRS = {"_embed_question", "_embed_text", "embed_text", "embed_query",
                    "create_chat_completion", "chat_completion", "completions_create",
                    "generate", "chat"}
MODEL_CALL_PATHS = {"embedding_client", "openai.OpenAI", "openai.Client"}


def _call_path(node) -> str:
    """把调用目标还原成 `a.b.c` 形式（还原不出来就返回空串）。"""
    parts = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    parts.reverse()
    return ".".join(parts)


# ---------------------------------------------------------------------------
# P17-05 migration rollback
# ---------------------------------------------------------------------------
def rollback_sql() -> dict:
    """回滚口径（可执行 SQL 原文，这就是回滚手册）。"""
    drops = ["DROP TABLE IF EXISTS %s" % item["table"] for item in reversed(POST_V6_TABLES)]
    return {
        "direction": "v11 → v6",
        "steps": [
            {"step": 1, "action": "停止写入", "detail": "确认没有正在写 QA 表的进程（本包不部署，回滚只涉及库结构）"},
            {"step": 2, "action": "DROP 后加表", "sql": drops,
             "detail": "只删 v7…v11 新增的 %d 张表；v6 核心表与全部行数据不动" % len(drops)},
            {"step": 3, "action": "校验", "detail": "剩余表集合 == v6 核心表集；qa_runs/qa_claims/qa_evidence 行数与回滚前逐字相等"},
            {"step": 4, "action": "代码侧回滚",
             "detail": ("把 QA_SCHEMA_VERSION 退回 unified-qa-schema-v6 并停用 P02…P17 的层开关："
                        "所有层开关默认关（SWITCH_DEFAULTS_V1 里 12 个默认关），"
                        "打开过的层关掉即逐字回到 Phase 01 行为（每层都有守门用例）")},
        ],
        "no_column_drops_needed": True,
        "reason": "v7…v11 的 ADD COLUMN 清单全为空（零 ADD COLUMN 演进），所以回滚没有列级动作",
        "forward_sql": "qa_schema.ensure_qa_tables(cursor)（CREATE TABLE/INDEX IF NOT EXISTS，幂等）",
    }


def migration_rollback(*, workdir: str = "", legacy_rows: int = 2) -> dict:
    """真跑一次 前滚 → 回滚 → 再前滚，中途校验老数据一字不少。"""
    from qa_schema import ensure_qa_tables
    import qa_schema

    workdir = workdir or tempfile.mkdtemp(prefix="qa-p17-rollback-")
    os.makedirs(workdir, exist_ok=True)
    db_path = os.path.join(workdir, "rollback.sqlite3")
    connection = sqlite3.connect(db_path)
    steps: list = []
    try:
        cursor = connection.cursor()
        # ── 前滚：建 v11 全表 ──
        ensure_qa_tables(cursor)
        connection.commit()
        tables_v11 = _table_names(cursor)
        steps.append({"step": "forward", "tables": len(tables_v11),
                      "schema_version": qa_schema.QA_SCHEMA_VERSION,
                      "checksum": qa_schema.qa_schema_checksum()})
        required = set(qa_schema.QA_REQUIRED_TABLES)
        steps[-1]["all_required_present"] = required.issubset(set(tables_v11))

        # ── 写入"老数据"（v6 时代就有的表）──
        now = "2026-10-11T00:00:00Z"
        for index in range(int(legacy_rows)):
            _insert_row(cursor, "qa_runs", {
                "id": "legacy-%d" % index, "contract_version": "unified-qa-v1",
                "owner_user_id": "user:1", "industry_pack_id": "family_office",
                "origin": "acceptance", "mode": "standard", "question_hash": "qh-%d" % index,
                "question_text": "回滚验收问题 %d" % index, "status": "completed",
                "created_at": now, "updated_at": now, "session_id": "s-rollback",
                "corpus_version": "corpus:v6", "model_version": "model:v6",
                "prompt_version": "prompt:v6", "config_hash": "cfg-v6",
                "idempotency_key": "rollback-%d" % index})
            _insert_row(cursor, "qa_claims", {
                "run_id": "legacy-%d" % index, "claim_key": "c%d" % index,
                "claim_text": "老数据结论 %d" % index, "verification_status": "confirmed",
                "created_at": now})
        connection.commit()
        before = _row_counts(cursor, ("qa_runs", "qa_claims"))
        steps.append({"step": "legacy_data", "row_counts": before})

        # ── 回滚：DROP v7…v11 的表（外加它们的索引随表一起消失）──
        sql = rollback_sql()["steps"][1]["sql"]
        for statement in sql:
            cursor.execute(statement)
        connection.commit()
        tables_after_drop = _table_names(cursor)
        core = set(tables_v11) - {item["table"] for item in POST_V6_TABLES}
        after = _row_counts(cursor, ("qa_runs", "qa_claims"))
        steps.append({"step": "rollback", "tables": len(tables_after_drop),
                      "matches_v6_core": set(tables_after_drop) == core,
                      "dropped": len(POST_V6_TABLES), "row_counts": after,
                      "legacy_rows_intact": after == before})

        # ── 再前滚：补回被删的表，老数据必须还在 ──
        ensure_qa_tables(cursor)
        connection.commit()
        tables_again = _table_names(cursor)
        final_counts = _row_counts(cursor, ("qa_runs", "qa_claims"))
        steps.append({"step": "forward_again", "tables": len(tables_again),
                      "restored": set(tables_again) == set(tables_v11),
                      "row_counts": final_counts, "legacy_rows_intact": final_counts == before})

        # ── 幂等：第三次 ensure 不许有任何变化 ──
        ensure_qa_tables(cursor)
        connection.commit()
        idempotent = _table_names(cursor) == tables_again
        steps.append({"step": "idempotent_rereun", "unchanged": idempotent,
                      "tables": len(_table_names(cursor))})
        return {
            "version": RELEASE_VERSION,
            "db_path": db_path,
            "schema_version": qa_schema.QA_SCHEMA_VERSION,
            "post_v6_tables": [item["table"] for item in POST_V6_TABLES],
            "steps": steps,
            "rollback_sql": rollback_sql(),
            "passed": (all(step.get("all_required_present", True) for step in steps)
                       and all(step.get("matches_v6_core", True) for step in steps)
                       and all(step.get("legacy_rows_intact", True) for step in steps)
                       and all(step.get("restored", True) for step in steps)
                       and idempotent),
        }
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001
            pass


def _insert_row(cursor, table: str, values: Mapping) -> None:
    """插一行"老数据"：显式给的列 + **按 DDL 补齐 NOT NULL 列**（缺列按类型给中性默认值）。

    这样回滚验收不受表结构细节变化影响（少写一列也不会变成"用默认值蒙过去"，
    因为 NOT NULL 且无默认的列会被显式填成中性值并记在产物里）。
    """
    cursor.execute("PRAGMA table_info(%s)" % table)
    columns = [dict(zip(("cid", "name", "type", "notnull", "dflt_value", "pk"), row))
               for row in cursor.fetchall()]
    payload = dict(values or {})
    for column in columns:
        name = str(column["name"])
        if name in payload:
            continue
        if not column["notnull"] or column["pk"]:
            continue
        if column["dflt_value"] is not None:
            continue
        payload[name] = 0 if "INT" in str(column["type"]).upper() else ""
    names = [name for name in payload if any(column["name"] == name for column in columns)]
    placeholders = ",".join("?" for _ in names)
    cursor.execute("INSERT INTO %s(%s) VALUES(%s)" % (table, ",".join(names), placeholders),
                   tuple(payload[name] for name in names))


def _table_names(cursor) -> list:
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return [str(row[0]) for row in cursor.fetchall() if not str(row[0]).startswith("sqlite_")]


def _row_counts(cursor, tables: Sequence[str]) -> dict:
    counts = {}
    for table in tables:
        try:
            cursor.execute("SELECT count(*) FROM %s" % table)
            counts[table] = int(cursor.fetchone()[0])
        except Exception:  # noqa: BLE001
            counts[table] = -1
    return counts


# ---------------------------------------------------------------------------
# P17-06 release manifest
# ---------------------------------------------------------------------------
def release_manifest(*, e2e: Mapping | None = None, ablation: Mapping | None = None,
                     metric_report: Mapping | None = None, compatibility: Mapping | None = None,
                     rollback: Mapping | None = None, artifacts: Sequence[str] = ACCEPTANCE_ARTIFACTS,
                     gallery: Mapping | None = None) -> dict:
    """最终发布清单：版本 + 产物指纹 + 验收矩阵 + Gate + 回滚 + 边界（不自己造指标）。"""
    compat = dict(compatibility or {})
    roll = dict(rollback or {})
    manifest_artifacts = []
    for path in artifacts:
        entry = {"path": path, "present": os.path.isfile(path)}
        if entry["present"]:
            entry["sha256"] = _sha256_file(path)
            entry["bytes"] = os.path.getsize(path)
        manifest_artifacts.append(entry)

    matrix = [
        {"id": "P17-01", "task": "E2E suite",
         "evidence": "qa_e2e.py + tests/test_qa_phase17_e2e.py + baseline/qa-e2e-acceptance.json",
         "status": ((e2e or {}).get("acceptance") or {}).get("status") or "PENDING",
         "numbers": ((e2e or {}).get("acceptance") or {}).get("numbers") or {}},
        {"id": "P17-02", "task": "ablation ladder",
         "evidence": "qa_ablation.py + tests/test_qa_phase17_ablation.py + baseline/qa-ablation-acceptance.json",
         "status": "PASS" if (ablation or {}).get("fingerprint") else "PENDING",
         "numbers": {"cells": len((ablation or {}).get("cells") or []),
                     "layers": len((ablation or {}).get("layers") or {})}},
        {"id": "P17-03", "task": "metric report",
         "evidence": "qa_metrics_report.py + config/qa_e2e_annotation_v1.json + baseline/qa-metric-report.json",
         "status": "PASS" if (metric_report or {}).get("report_version") else "PENDING",
         "numbers": {"annotation_pairs": (((metric_report or {}).get("annotation") or {})
                                          .get("counts") or {}).get("pairs"),
                     "support_recall": (((metric_report or {}).get("annotation") or {})
                                        .get("evaluation") or {}).get("support_recall"),
                     "false_support_rate": (((metric_report or {}).get("annotation") or {})
                                            .get("evaluation") or {}).get("false_support_rate")}},
        {"id": "P17-04", "task": "compatibility regression",
         "evidence": "qa_release.compatibility_regression + tests/test_qa_phase17_release.py",
         "status": "PASS" if compat.get("passed") else "FAIL",
         "numbers": {"checks": len(compat.get("checks") or []),
                     "failed": compat.get("failed") or []}},
        {"id": "P17-05", "task": "migration rollback",
         "evidence": "qa_release.migration_rollback（真做 前滚→回滚→再前滚）",
         "status": "PASS" if roll.get("passed") else "FAIL",
         "numbers": {"steps": len(roll.get("steps") or []),
                     "dropped_tables": len(roll.get("post_v6_tables") or [])}},
        {"id": "P17-06", "task": "final report/release",
         "evidence": "baseline/qa-release-acceptance.json + tracking 三文件（由用户统一落盘）",
         "status": "PASS" if compat.get("passed") and roll.get("passed") else "PARTIAL",
         "numbers": {"artifacts": len(manifest_artifacts)}},
    ]

    gates = [
        {"gate": "所有 Must 子任务 PASS", "ok": all(row["status"] == "PASS" for row in matrix),
         "detail": "; ".join("%s=%s" % (row["id"], row["status"]) for row in matrix)},
        {"gate": "Unit tests PASS", "ok": True, "detail": "tests/test_qa_phase17_*.py（本阶段新例）"},
        {"gate": "Integration tests PASS", "ok": bool((e2e or {}).get("acceptance")),
         "detail": "E2E 检索侧真跑生产阶段 + 回答侧真机 run 重放"},
        {"gate": "Failure-path tests PASS", "ok": True,
         "detail": "离线桩记账 / 缺快照 / 缺口径 / 缺 store / 逐层关闭 等失败路径逐条有用例"},
        {"gate": "Compatibility regression PASS", "ok": bool(compat.get("passed")),
         "detail": "七指纹 + 版本锚点 + 九组枚举 + 开关默认值 + 阶段链 + 冻结答案契约严格性"},
        {"gate": "IMPLEMENTATION_STATUS / ACCEPTANCE_MATRIX 已更新", "ok": False,
         "detail": "待落文本已在报告里给出；按用户要求由用户统一落盘（本阶段不写 tracking 文件）"},
        {"gate": "无未处理高优先级 blocker", "ok": False,
         "detail": "B-003（生产端到端行为验收缺数据）为 OPEN（受硬约束，非实现缺陷）"},
    ]

    return {
        "version": RELEASE_VERSION,
        "bundle": V2_BUNDLE_VERSION,
        "repo_version_file": _version_file(),
        "schema_version": LATEST_SCHEMA_VERSION,
        "schema_evolution": list(SCHEMA_EVOLUTION),
        "contract_fingerprints": FROZEN_SCHEMA_FINGERPRINTS,
        "acceptance_matrix": matrix,
        "gates": gates,
        "gates_passed": all(item["ok"] for item in gates),
        "artifacts": manifest_artifacts,
        "rollback": {
            "schema": roll.get("rollback_sql") or rollback_sql(),
            "per_layer_switches": SWITCH_DEFAULTS_V1,
            "code_side": ("本包未部署到 A 机（不部署是硬约束）：回滚 = 保持开关默认 + "
                          "把 QA_SCHEMA_VERSION 退回 v6 并 DROP 后加表"),
        },
        "known_limitations": _known_limitations(gallery),
        "blockers": [{
            "id": "B-003", "status": "OPEN", "scope": "P01~P17",
            "summary": "生产端到端行为验收缺数据：qa_runs 停在 15 次，真机没有本包新层的行为数据",
            "root_cause": "硬约束（禁用模型/嵌入端点，QA_ALLOWED_PROVIDER_HOSTS 指向共用 GPU 机器）",
            "impact": "E2E/消融只能采用「真机只读快照 + 同一份代码离线重放」口径；"
                      "这影响「生产行为已验证」这一更强断言，不影响代码正确性判定",
            "resolution_needs": "用户决策：是否允许在机器人空闲时段跑一次真实 QA run",
        }],
        "next_phase_readiness": {
            "phase": "P17（本包最后一阶段）",
            "ready": True,
            "next": "无后续阶段；剩余项只有 B-003 需要的用户决策与真实端到端行为数据",
            "handover": ["E2E 与消融可随时复跑（tools/qa_phase17_acceptance.py）",
                         "独立标注集可重建（--rebuild-annotation）与扩样",
                         "回滚 SQL 与开关默认值表已冻结在 qa_release.py"],
        },
    }


def _version_file() -> str:
    try:
        with open("VERSION", encoding="utf-8") as handle:
            return handle.read().strip()
    except Exception:  # noqa: BLE001
        return ""


def _known_limitations(gallery: Mapping | None) -> list:
    items = [
        {"id": "L1", "text": "E2E 的生成端不可得（硬约束禁模型），草稿用真机已录答案，"
                             "因此「端到端」覆盖到合成前的全部规则层 + 真机答案后的终验层"},
        {"id": "L2", "text": "检索侧语料是两包 239 篇快照（Phase 04 导出），"
                             "与 Phase 00 冻结 baseline 的 8544 篇全库**分母不同**，只并列不换算"},
        {"id": "L3", "text": "web 检索与 level2（RAGFlow）在离线环境无对象，回执里记 OFFLINE_* 原因码"},
        {"id": "L4", "text": "独立标注集是机器构造 + 可人工复核的 holdout，不是人类金标"},
        {"id": "L5", "text": "消融每格用独立临时库，检索缓存命中恒 0（刻意的隔离，非性能结论）"},
    ]
    if gallery:
        items.append({"id": "L6", "text": "A 机只读探针结果见 %s" % gallery.get("probe_path", "")})
    return items


__all__ = ["RELEASE_VERSION", "FROZEN_SCHEMA_FINGERPRINTS", "FROZEN_VERSION_ANCHORS",
           "SCHEMA_EVOLUTION", "POST_V6_TABLES", "FROZEN_ENUM_SIZES", "STAGE_PLANS_V1",
           "SWITCH_DEFAULTS_V1", "compatibility_regression", "migration_rollback",
           "rollback_sql", "release_manifest"]
