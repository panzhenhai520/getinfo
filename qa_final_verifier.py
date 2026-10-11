#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 13（P13-01…P13-06）· Answer Composer & Final Verifier（**纯规则/统计，零模型调用、零嵌入端点**）。

通用包 01_V2_ARCHITECTURE 依据：
  · §16 Answer Composer：**不读大量原始 chunk**，只读 `Question / Verified Claims / Evidence Graph /
    Unresolved Gaps / Contradictions / Citation Map`；输出每个事实声明时绑定
    `claim_id → evidence_id[] → source/chunk/span`（→ `compose_answer()`）；
  · §17 Final Verifier 的**六问**逐字入契约 `FINAL_VERIFIER_QUESTIONS`，六个检查码入
    `FINAL_VERIFIER_CHECKS`（→ `verify_answer()`）；
  · §17 "失败时**只重跑 Composer 或只重跑相关 Verifier**，不重跑整个 Research Graph"
    → `plan_retry()` + `run_local_retry()`（作用域只有两个，检索调用数恒 0）；
  · §18 deep 链最后一段 `final_verify`（Phase 05 执行图里的占位节点）由本阶段落成真节点。

边界与取舍（诚实声明，宁写 PARTIAL 不谎报）
--------------------------------------------
  1. **本模块不写正文**：本机硬约束禁止调用任何模型/嵌入端点，而 §16 的"成稿"由既有
     生成端链路（`qa_synthesis.QaFinalSynthesizer`）负责。本模块只做**组装（绑定结构）、
     约束与校验**三件事，绝不新造第二套生成链路。
  2. **终验不另造质量判断（单一真源）**：六问的判据全部由 ① Phase 03 的核验结论
     （`qa_verifier` 的 claim 级 pairs / verdict / `check_numbers`）与 ② Phase 08 的生成端
     grounding 回执（`qa_context_pack.check_grounding` 的七个违规码）派生；每个 check 的
     `derived_from` 写明来源标识，`inputs` 写明判据输入数字。守门用例钉住
     "改 Phase 08 的回执 → 终验结论跟着变"（证明不是两套判断）。
  3. **冻结 schema 一个字不动**：`FINAL_ANSWER_SCHEMA` 仍 `additionalProperties=False`，
     本模块只写它**已有**的字段（`status` / `degraded` / `degradation_reasons` /
     `citations` / `citation_map` / `claims` / `sections`）。
  4. **局部重试的边界**：只重跑 Composer（在同一批证据上重新绑定 + 丢掉绑不上的结论 +
     把反证与未解决缺口写进答案）或只重跑相关 Verifier（把 Phase 03 的规则核验**只**重跑
     受影响 claim 的子图）。**零检索、零新证据、零模型**：`retrieval_calls` 恒 0，
     `verifier.scope_claims` 只含受影响 claim（有用例断言"没被碰到的 claim 一行没变"）。
  5. **幂等与有界**：重试上限 `QA_FINAL_VERIFY_RETRY_MAX`（默认 1）；重跑后结论一字没变
     记 `NO_CHANGE` 并停止（不许无限重试）；同一输入两次运行产出逐字相同。
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Iterable, Mapping, Sequence

from qa_context_pack import (
    GROUNDING_BLOCKING_VIOLATIONS,
    GROUNDING_WARNING_VIOLATIONS,
    UNGROUNDED_MARK,
    check_grounding,
    citation_labels,
    normalize_evidence_layer,
)
from qa_graph_contracts import (
    ANSWER_COMPOSER_VERSION,
    CLAIM_VERIFICATION_STATUSES,
    CONTEXT_PACK_VERSION,
    EVIDENCE_GRAPH_RELATIONSHIPS,
    EVIDENCE_STATUS_CONTEXT,
    EVIDENCE_STATUS_QUALIFIED,
    EVIDENCE_STATUS_REFUTED,
    EVIDENCE_STATUS_SUPPORTED,
    EVIDENCE_STATUS_UNVERIFIED,
    FINAL_RETRY_SCOPES,
    FINAL_VERIFY_RETRY_VERSION,
    FINAL_VERIFIER_QUESTIONS,
    FINAL_VERIFIER_VERSION,
    GAP_PRIORITY_BANDS,
    INSUFFICIENT_EVIDENCE_TRIGGERS,
    INSUFFICIENT_EVIDENCE_VERSION,
    QA_GAP_TYPES,
    QA_RETRIEVAL_ROUTES,
    QA_STOP_REASONS,
    validate as validate_contract,
)
from qa_verifier import VERIFIER_VERSION, check_numbers, verify_claim_graph

_MISSING = object()

# ── 可配置旋钮（全部可回滚；默认值写在常量里便于复算）───────────────────────
DEFAULT_RETRY_MAX = 1                 # §17 局部重试上限（默认只给一次机会）
DEFAULT_UNCERTAINTY_MARKS = (
    "可能", "或许", "尚不确定", "不确定", "待确认", "未能确认", "据推测", "推测",
    "存疑", "尚需", "预计", "或将", UNGROUNDED_MARK,
)
"""不确定性表达记号（P13-03/P13-04 的"过度推理/把推测写成事实"判据之一）。

口径写死：一条**未获已核验支持**的结论出现在正文里时，它的实词附近必须有这些记号之一，
否则判"把推测写成事实"。这是**词面判据**、完全可复算；它不试图理解语义（本机禁止调模型）。"""

DEFAULT_INSIGHT_MIN_SPAN_CHARS = 8    # 短于这个长度的 span 不当可回溯（与 Phase 08 的 40 口径分列）

_UNCERTAINTY_WINDOW = 24
"""不确定性记号必须出现在结论实词的前后多少字之内（窗口太大就变成"整篇扫一遍"，
Phase 03 的否定词判定踩过同一个坑，所以这里也用**局部窗口**）。"""


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().casefold() not in {"0", "false", "no", "off", ""}


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(float(os.getenv(name, "") or default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def final_verifier_enabled() -> bool:
    """P13 总开关（`QA_FINAL_VERIFIER`，**默认关**：与 Phase 06/07/08/12 同样手法，
    关掉时 `citation_validation` 阶段的返回逐字回到接线前，一个键都不新增）。"""
    return _env_flag("QA_FINAL_VERIFIER", False)


def retry_max() -> int:
    """局部重试上限（§17 的"只重跑 Composer / 只重跑相关 Verifier"次数上限）。"""
    return _env_int("QA_FINAL_VERIFY_RETRY_MAX", DEFAULT_RETRY_MAX, 0, 3)


def uncertainty_marks() -> tuple:
    raw = str(os.getenv("QA_FINAL_VERIFY_UNCERTAINTY_MARKS") or "").strip()
    if not raw:
        return DEFAULT_UNCERTAINTY_MARKS
    return tuple(item.strip() for item in raw.split(",") if item.strip()) or DEFAULT_UNCERTAINTY_MARKS


def _counter(values: Iterable) -> dict:
    out: dict = {}
    for value in values or ():
        key = str(value or "")
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda item: (-item[1], item[0])))


def _digest(value, length: int = 16) -> str:
    body = repr(value) if not isinstance(value, str) else value
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:length]


def _mapping(value) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ── 基础抽取：答案 / 图 / 引用索引 ──────────────────────────────────────────

def _answer_body(answer: Mapping) -> str:
    """答案正文 = `answer` 字段 + `sections` 里的全部文本（生成端把内容分放在两处）。"""
    answer = answer if isinstance(answer, Mapping) else {}
    text = str(answer.get("answer") or "")
    sections = answer.get("sections") if isinstance(answer.get("sections"), Mapping) else {}
    parts = [text]
    for value in sections.values():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, Mapping):
            for inner in value.values():
                if isinstance(inner, str):
                    parts.append(inner)
                elif isinstance(inner, (list, tuple)):
                    parts.extend(str(item) for item in inner if isinstance(item, (str, int, float)))
    return " ".join(part for part in parts if part)


def _claim_id_of(row) -> str:
    if isinstance(row, Mapping):
        node = row.get("claim") if isinstance(row.get("claim"), Mapping) else {}
        return str(row.get("claim_id") or node.get("claim_id") or row.get("canonical_id") or "")
    return str(row or "")


def _answer_claims(answer: Mapping) -> list:
    """答案里的 claim 行（生成端写的是**归一化后的 claim 字典**，也可能是纯字符串 id）。"""
    answer = answer if isinstance(answer, Mapping) else {}
    rows = []
    for item in answer.get("claims") or []:
        if isinstance(item, Mapping):
            rows.append(item)
        elif isinstance(item, str):
            rows.append({"claim_id": item, "text": "", "evidence_refs": []})
    return rows


def _answer_claim_ids(answer: Mapping) -> list:
    return [cid for cid in (_claim_id_of(row) for row in _answer_claims(answer)) if cid]


def _graph_claim_nodes(graph: Mapping) -> list:
    graph = graph if isinstance(graph, Mapping) else {}
    return [node for node in (graph.get("claims") or []) if isinstance(node, Mapping)]


def _claim_text(node: Mapping) -> str:
    claim = node.get("claim") if isinstance(node.get("claim"), Mapping) else {}
    return str(claim.get("text") or node.get("text") or node.get("statement") or "")


def _claim_refs(node: Mapping) -> list:
    claim = node.get("claim") if isinstance(node.get("claim"), Mapping) else {}
    refs = list(claim.get("evidence_refs") or node.get("evidence_refs") or [])
    if not refs:
        refs = [str(edge.get("evidence_ref") or "") for edge in (node.get("edges") or [])
                if isinstance(edge, Mapping)]
    return [str(ref) for ref in refs if str(ref or "").strip()]


def _claim_pairs(node: Mapping) -> dict:
    """Phase 03 的 claim 级核验 pairs（`evidence_ref → {verdict, score, reasons}`）。

    两处都认：`verify_claim_graph()` 把结论写在**节点**的 `verification` 上，而
    `graph_from_rows()` 重建 / 离线快照里可能挂在 `claim.verification` 上（同一份结论的
    两个落脚点，取值同一个来源，不新造判定）。
    """
    claim = node.get("claim") if isinstance(node.get("claim"), Mapping) else {}
    for holder in (node, claim):
        verification = holder.get("verification") if isinstance(holder.get("verification"),
                                                               Mapping) else {}
        pairs = {}
        for pair in verification.get("pairs") or []:
            if isinstance(pair, Mapping) and pair.get("evidence_ref"):
                pairs[str(pair.get("evidence_ref"))] = dict(pair)
        if pairs:
            return pairs
    return {}


def _claim_status(node: Mapping) -> str:
    claim = node.get("claim") if isinstance(node.get("claim"), Mapping) else {}
    for holder in (node, claim):
        verification = holder.get("verification") if isinstance(holder.get("verification"),
                                                               Mapping) else {}
        status = str(verification.get("status") or "")
        if status:
            return status
    return str(claim.get("verification_status") or node.get("verification_status") or "")


def _evidence_pool(answer: Mapping, graph: Mapping) -> dict:
    """证据池：**答案自带证据优先**（它才是这份答案真正可以引用的东西），图里补齐其余。

    两边的同一条证据以"带 Phase 02 证据层 / Phase 03 核验"的那份为准（合并而不是覆盖）。
    """
    pool: dict = {}
    for source in (graph if isinstance(graph, Mapping) else {}, answer if isinstance(answer, Mapping) else {}):
        for item in (source.get("evidence") or []):
            if not isinstance(item, Mapping):
                continue
            ref = str(item.get("evidence_ref") or "")
            if not ref:
                continue
            prior = pool.get(ref)
            if prior is None:
                pool[ref] = dict(item)
                continue
            merged = dict(item)
            for key, value in prior.items():
                if merged.get(key) in (None, "", {}, []):
                    merged[key] = value
            pool[ref] = merged
    return pool


def _citation_index(pack: Mapping | None, pool: Mapping) -> tuple:
    """引用索引：**包里的优先**（单一真源）；没有包就现算（口径与 Phase 08 逐字相同）。

    返回 `(index, labels_by_ref, source)`；`source` 只有两个值：
      · `pack` —— 索引来自上下文包（Phase 08 的产物，与生成端编号**同口径**，有守门用例钉等值）；
      · `derived` —— 没有包，按同一套排序规则**现算**。两条路径的**判定权限不同**：
        有包时"编号↔证据"的对应关系是权威的，答案与它不一致就是引用错误；
        没有包时（离线复算 / 未开 `QA_CONTEXT_PACK`）编号本来就可以重算，
        此时**答案自带的 `citation_map` 才是那一轮的权威**，现算的编号只用来判断
        "被引用的证据在不在证据域内"（详见 `verify_answer` 第⑤问）。
    """
    pack = pack if isinstance(pack, Mapping) else {}
    index = pack.get("citation_index") if isinstance(pack.get("citation_index"), Mapping) else {}
    labels: dict = {}
    clean_index: dict = {}
    for label, entry in index.items():
        entry = _mapping(entry)
        ref = str(entry.get("evidence_ref") or "")
        if not ref:
            continue
        clean_index[str(label)] = {
            "evidence_ref": ref,
            "evidence_id": str(entry.get("evidence_id") or ""),
            "quote": str(entry.get("quote") or ""),
            "span": _mapping(entry.get("span")),
            "source_url": str(entry.get("source_url") or ""),
            "grounded": bool(entry.get("grounded")),
            "verdict": str(entry.get("verdict") or ""),
            "section": str(entry.get("section") or ""),
        }
        labels.setdefault(ref, str(label))
    if clean_index:
        return clean_index, labels, "pack"
    labels = citation_labels(list(pool.values()))
    for label, ref in labels.items():
        item = pool.get(str(ref)) or {}
        layer = normalize_evidence_layer(item)
        clean_index[str(label)] = {
            "evidence_ref": str(ref),
            "evidence_id": str(layer.get("evidence_id") or ""),
            "quote": str((layer.get("span") or {}).get("quote") or ""),
            "span": _mapping(layer.get("span")),
            "source_url": str((layer.get("source") or {}).get("source_url")
                              or item.get("source_url") or ""),
            "grounded": bool(layer.get("grounded")),
            "verdict": str((layer.get("verification") or {}).get("verdict") or ""),
            "section": "evidence_context",
        }
    return clean_index, labels, "derived"


def _closed(value, allowed: Iterable, *, fallback: str = "") -> str:
    """把上游取值收进闭集合：不认识就记空串（**不许悄悄放行契约外的值**）。

    为什么要这一步：上游（Phase 06/07 的图与缺口）理论上只产闭集合内的值，但真机上
    "老 run / 外部注入"确实可能带来陌生取值。组装层宁可把它记空并在 `inputs.unrecognized`
    里计数，也不能让一个陌生值把整份组装的契约校验打红——更不许把它当成合法值往下传。
    """
    text = str(value or "")
    return text if text in set(allowed) else fallback


def _mentions(text: str, needle: str, *, min_chars: int = 6) -> bool:
    """正文有没有提到某个结论：长文本用**滑动片段**判（避免整句逐字相等这种过严口径）。"""
    body = str(text or "")
    target = re.sub(r"\s+", "", str(needle or ""))
    if not body or len(target) < min_chars:
        return False
    compact = re.sub(r"\s+", "", body)
    if target[:min_chars] in compact:
        return True
    step = max(min_chars, len(target) // 3)
    for index in range(0, max(1, len(target) - min_chars + 1), step):
        if target[index:index + min_chars] in compact:
            return True
    return False


def _has_uncertainty_mark(body: str, text: str) -> bool:
    """结论的实词附近有没有不确定性表达（局部窗口，可复算）。"""
    compact = re.sub(r"\s+", "", str(body or ""))
    target = re.sub(r"\s+", "", str(text or ""))
    if not compact or not target:
        return False
    marks = uncertainty_marks()
    start = compact.find(target[:8]) if len(target) >= 8 else compact.find(target)
    if start < 0:
        return any(mark in compact for mark in marks)
    window = compact[max(0, start - _UNCERTAINTY_WINDOW):start + len(target) + _UNCERTAINTY_WINDOW]
    return any(mark in window for mark in marks)


# ── P13-01 / P13-02 / P13-03：claim-aware composer + 引用绑定 + 反证与未解决缺口 ──

def compose_answer(*, graph: Mapping | None = None, answer: Mapping | None = None,
                   pack: Mapping | None = None, question: str = "") -> dict:
    """§16 Answer Composer：把"回答问题要用到的已验证证据"组装成**可校验的绑定结构**。

    输出（`ANSWER_COMPOSITION_SCHEMA`）：
      · `bindings`：每条 claim 的 `claim_id → evidence_id[] → source/chunk/span`
        （P13-01 + P13-02），每条带 `bound`/`binding_reason`（为什么绑上/为什么绑不上）；
      · `counter_evidence`：该 claim 的反证条目 + **答案有没有引用它**（P13-03）；
      · `unresolved_gaps`：Phase 07 的未解决缺口（含优先级与建议通道）+ 答案有没有提到；
      · `contradictions`：Phase 06 的矛盾与裁决（含裁决理由码）+ 答案有没有提到；
      · `citation_map` / `citation_index`：引用编号与可回溯索引（与 Phase 08 同口径）；
      · `inputs`：本次组装的**输入计数**（可复算"我到底看到了多少东西"）。

    零模型、零检索：只读入参。
    """
    graph = graph if isinstance(graph, Mapping) else {}
    answer = answer if isinstance(answer, Mapping) else {}
    body = _answer_body(answer)
    pool = _evidence_pool(answer, graph)
    index, labels_by_ref, index_source = _citation_index(pack, pool)
    answer_ids = _answer_claim_ids(answer)
    answer_id_set = set(answer_ids)

    graph_nodes = _graph_claim_nodes(graph)
    by_id = {}
    for node in graph_nodes:
        cid = _claim_id_of(node)
        if cid and cid not in by_id:
            by_id[cid] = node
    # 答案里有、图里没有的结论同样要绑定（生成端可能保留了一级草稿里的结论）
    ordered_ids = list(dict.fromkeys(list(answer_id_set) + list(by_id)))

    bindings: list = []
    counter_rows: list = []
    for cid in ordered_ids:
        node = by_id.get(cid) or {}
        answer_row = next((row for row in _answer_claims(answer) if _claim_id_of(row) == cid), {})
        claim = node.get("claim") if isinstance(node.get("claim"), Mapping) else {}
        text = str(claim.get("text") or answer_row.get("text") or node.get("text") or "")
        refs = list(dict.fromkeys(
            list(answer_row.get("evidence_refs") or []) + list(_claim_refs(node))))
        pairs = _claim_pairs(node)
        plan_only = bool(node.get("plan_only") or answer_row.get("plan_only"))
        in_answer = bool(cid in answer_id_set) or _mentions(body, text)
        entries = []
        for ref in refs:
            item = pool.get(str(ref))
            layer = normalize_evidence_layer(item) if isinstance(item, Mapping) else {}
            pair = pairs.get(str(ref))
            verdict = ""
            verdict_source = "none"
            score = 0.0
            if isinstance(pair, Mapping):
                verdict = str(pair.get("verdict") or "")
                verdict_source = "claim_pair"
                score = _as_float(pair.get("score"))
            elif isinstance(layer, Mapping) and (layer.get("verification") or {}).get("verdict"):
                verdict = str((layer.get("verification") or {}).get("verdict") or "")
                verdict_source = "pack_evidence"
                score = _as_float((layer.get("verification") or {}).get("score"))
            entries.append({
                "evidence_ref": str(ref),
                "evidence_id": str((layer or {}).get("evidence_id") or ""),
                "verdict": verdict,
                "verdict_source": verdict_source,
                "evidence_score": round(score, 4),
                "grounded": bool((layer or {}).get("grounded")),
                "span": _mapping((layer or {}).get("span")),
                "source_url": str(((layer or {}).get("source") or {}).get("source_url")
                                  or (item or {}).get("source_url") or ""),
                "source_type": str((item or {}).get("source_type") or ""),
                "chunk_id": str((item or {}).get("chunk_id")
                                or ((item or {}).get("metadata") or {}).get("chunk_id") or ""),
                "document_id": str((item or {}).get("document_id")
                                   or ((item or {}).get("metadata") or {}).get("document_id") or ""),
                "label": str(labels_by_ref.get(str(ref)) or ""),
            })
        traceable = [entry for entry in entries if entry["grounded"]]
        supported = [entry for entry in traceable if entry["verdict"] == EVIDENCE_STATUS_SUPPORTED]
        if not refs:
            reason = "NO_EVIDENCE_REF"
        elif not traceable:
            reason = "NO_TRACEABLE_SPAN"
        elif supported:
            reason = "BOUND_SUPPORTED"
        else:
            reason = "BOUND_QUALIFIED"
        if plan_only and reason not in ("NO_EVIDENCE_REF", "NO_TRACEABLE_SPAN"):
            reason = "PLAN_ONLY"
        if not in_answer and reason in ("BOUND_SUPPORTED", "BOUND_QUALIFIED"):
            reason = "NOT_IN_ANSWER"
        bound = reason in ("BOUND_SUPPORTED", "BOUND_QUALIFIED")
        bindings.append({
            "composer_version": ANSWER_COMPOSER_VERSION,
            "claim_id": cid, "text": text[:600],
            "claim_status": _closed(_claim_status(node)
                                    or str(answer_row.get("verification_status") or ""),
                                    CLAIM_VERIFICATION_STATUSES),
            "plan_only": plan_only, "in_answer": in_answer,
            "bound": bound, "binding_reason": reason,
            "evidence": entries,
        })
        # P13-03：反证（REFUTES 关系 / 已核验的反驳）
        for entry in entries:
            relation = ""
            for edge in (node.get("edges") or []):
                if isinstance(edge, Mapping) and str(edge.get("evidence_ref")) == entry["evidence_ref"]:
                    relation = str(edge.get("graph_relation") or edge.get("status") or "").upper()
            if not relation:
                relation = "REFUTES" if entry["verdict"] == EVIDENCE_STATUS_REFUTED else (
                    "SUPPORTS" if entry["verdict"] == EVIDENCE_STATUS_SUPPORTED else "")
            if relation != "REFUTES" or not _closed(relation, EVIDENCE_GRAPH_RELATIONSHIPS):
                continue
            counter_rows.append({
                "claim_id": cid, "evidence_ref": entry["evidence_ref"], "relation": relation,
                "label": entry["label"],
                "quote": str((entry.get("span") or {}).get("quote") or "")[:240],
                "cited_in_answer": (entry["evidence_ref"] in set(
                    str(ref) for ref in (answer.get("citations") or []))
                    or (bool(entry["label"]) and str(entry["label"]) in body)),
            })

    unresolved_gaps, contradictions, unrecognized = _gap_and_conflict_rows(graph, answer, body)
    verified = [row["claim_id"] for row in bindings if row["binding_reason"] == "BOUND_SUPPORTED"]
    unbound = [row["claim_id"] for row in bindings if not row["bound"] and row["in_answer"]]
    reason_codes = sorted({row["binding_reason"] for row in bindings})
    composition = {
        "composer_version": ANSWER_COMPOSER_VERSION,
        "question": str(question or "")[:4000],
        "status": str(answer.get("status") or ""),
        "bindings": bindings,
        "verified_claims": verified,
        "unbound_claims": unbound,
        "counter_evidence": counter_rows[:60],
        "unresolved_gaps": unresolved_gaps[:60],
        "contradictions": contradictions[:60],
        "citation_map": dict(pack.get("citation_map") or {}) if isinstance(pack, Mapping) else
                        {label: entry["evidence_ref"] for label, entry in index.items()},
        "citation_index_source": index_source,
        "citation_index": {label: {"evidence_ref": entry["evidence_ref"],
                                   "evidence_id": entry["evidence_id"],
                                   "quote": entry["quote"][:240],
                                   # span 与 source_url 与 Phase 08 的 `citation_index` 同形：
                                   # 引用能不能**逐字对上原文**要靠这两个字段复算
                                   "span": dict(entry.get("span") or {}),
                                   "source_url": str(entry.get("source_url") or ""),
                                   "grounded": entry["grounded"],
                                   "verdict": entry["verdict"]}
                           for label, entry in index.items()},
        "inputs": {
            "claims": len(bindings),
            "claims_in_answer": len([row for row in bindings if row["in_answer"]]),
            "evidence": len(pool),
            "citation_labels": len(index),
            "counter_evidence": len(counter_rows),
            "unresolved_gaps": len(unresolved_gaps),
            "contradictions": len(contradictions),
            "unrecognized": dict(unrecognized),
        },
        "reason_codes": reason_codes,
        "note": ("本结构只做组装与绑定：正文由既有生成端链路负责（本机禁止调模型）；"
                 "每条绑定都能回到 Phase 02 的最小 span 与 Phase 03 的核验结论。"),
    }
    ok, note = validate_contract("answer_composition", composition)
    composition["schema_ok"] = bool(ok)
    if not ok:
        composition["schema_note"] = note
    return composition


def _gap_and_conflict_rows(graph: Mapping, answer: Mapping, body: str) -> tuple:
    """未解决缺口与矛盾（**只读 Phase 06/07 的产物**，不新造缺口）。

    返回 `(gaps, contradictions, unrecognized)`；`unrecognized` 是"上游给了闭集合外的取值、
    被组装层记空"的计数（可复算，绝不静默吞掉）。
    """
    gaps: list = []
    unrecognized: dict = {}
    layer = graph.get("evidence_graph") if isinstance(graph.get("evidence_graph"), Mapping) else {}
    review = layer.get("gap_review") if isinstance(layer.get("gap_review"), Mapping) else {}
    candidates = list(review.get("gaps") or [])
    if not candidates:
        loop = graph.get("gap_loop") if isinstance(graph.get("gap_loop"), Mapping) else {}
        candidates = list(loop.get("open_gaps") or [])
    for gap in candidates:
        if not isinstance(gap, Mapping):
            continue
        if str(gap.get("status") or "open") == "resolved":
            continue
        raw_type = str(gap.get("gap_type") or gap.get("type") or "")
        raw_band = str(gap.get("band") or "")
        gap_type = _closed(raw_type, QA_GAP_TYPES)
        band = _closed(raw_band, GAP_PRIORITY_BANDS)
        if raw_type and not gap_type:
            unrecognized["gap_type"] = unrecognized.get("gap_type", 0) + 1
        if raw_band and not band:
            unrecognized["band"] = unrecognized.get("band", 0) + 1
        routes = []
        for route in (gap.get("suggested_routes") or []):
            clean = _closed(route, QA_RETRIEVAL_ROUTES)
            if clean:
                routes.append(clean)
            elif str(route or ""):
                unrecognized["route"] = unrecognized.get("route", 0) + 1
        gaps.append({
            "gap_id": str(gap.get("gap_id") or ""),
            "gap_type": gap_type,
            "claim_id": str(gap.get("claim_id") or ""),
            "priority": round(_as_float(gap.get("priority")), 4),
            "band": band,
            "severity": str(gap.get("severity") or ""),
            "suggested_routes": routes,
            "evidence_requirement": _mapping(gap.get("evidence_requirement")),
            "mentioned_in_answer": _mentions(body, str(gap.get("detail") or gap.get("gap_type") or "")),
        })
    contradictions: list = []
    for item in graph.get("conflicts") or []:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("resolution") or "") != "unresolved":
            continue
        contradictions.append({
            "conflict_id": str(item.get("conflict_id") or ""),
            "claim_id": str((item.get("claim_ids") or [""])[0] or ""),
            "conflict_type": str(item.get("conflict_type") or ""),
            "resolution": str(item.get("resolution") or ""),
            "reason_code": str(item.get("rule_version") or item.get("reason_code") or ""),
            "winner": str(item.get("winner") or ""),
            "mentioned_in_answer": _mentions(body, str(item.get("rationale") or "")),
        })
    return gaps, contradictions, unrecognized


# ── P13-04：Final Verifier 六问 ────────────────────────────────────────────

CHECK_SEVERITY = {
    "FACT_WITHOUT_EVIDENCE": "blocking",
    "EVIDENCE_NOT_SUPPORTING": "blocking",
    "OVER_INFERENCE": "warning",
    "COUNTER_EVIDENCE_MISSING": "warning",
    "CITATION_MISBOUND": "blocking",
    "SPECULATION_AS_FACT": "blocking",
}
"""每一问**最坏情况下**的分级（这一问可能坏到"拦下答案"的程度就是 `blocking`）。

三条硬规则：
  1. 运行时分级 = "该问的失败项里有没有 blocking"，每一条 failure 的 `severity` 要么抄自
     Phase 08 的违规码分级（`GROUNDING_BLOCKING_VIOLATIONS` / `GROUNDING_WARNING_VIOLATIONS`），
     要么是**结构性问题**（"结论在答案里却完全没有证据" / "证据与结论相悖" / "计划态 claim
     被当成结论"——这三条在 Phase 08 里对应的正是阻断级的 `CLAIM_WITHOUT_EVIDENCE`）；
  2. **不许比上游更严**：`NUMBER_WITHOUT_SPAN` / `COUNTER_EVIDENCE_OMITTED` /
     `UNMARKED_UNGROUNDED_TEXT` / `CLAIM_WITH_UNVERIFIED_EVIDENCE` 在 Phase 08 里都是**告警级**，
     于是第③④⑥问在这些情形上也只能是 `warning`——否则等于绕过 Phase 08 私抬门槛；
  3. 检测可以更细（例如逐 claim 的数字一致性），**分级不许更狠**。"""

DERIVED_FROM = {
    "FACT_WITHOUT_EVIDENCE": ("qa-grounding-v1:CLAIM_WITHOUT_EVIDENCE",
                              "qa-grounding-v1:CITATION_WITHOUT_SPAN",
                              "qa-answer-composer-v1:binding"),
    "EVIDENCE_NOT_SUPPORTING": ("qa-verifier-v1:claim_pairs.verdict",
                               "qa-verifier-v1:claim_pairs.score"),
    "OVER_INFERENCE": ("qa-verifier-v1:check_numbers", "qa-grounding-v1:NUMBER_WITHOUT_SPAN",
                       "qa-verifier-v1:claim_pairs.verdict"),
    "COUNTER_EVIDENCE_MISSING": ("qa-grounding-v1:COUNTER_EVIDENCE_OMITTED",
                                 "qa-evidence-graph-v1:REFUTES",
                                 "qa-answer-composer-v1:counter_evidence"),
    "CITATION_MISBOUND": ("qa-grounding-v1:CITATION_NOT_IN_MAP",
                          "qa-context-pack-v1:citation_index"),
    "SPECULATION_AS_FACT": ("qa-grounding-v1:UNMARKED_UNGROUNDED_TEXT",
                            "qa-execution-graph-v1:plan_only",
                            "qa-verifier-v1:claim_pairs.verdict"),
}
"""每一问的**派生来源**（机器可校验：守门用例断言这些标识真的是上游模块里存在的东西）。"""


def _violations(grounding: Mapping, codes: Iterable) -> list:
    wanted = {str(code) for code in codes}
    return [dict(item) for item in (grounding.get("violations") or [])
            if isinstance(item, Mapping) and str(item.get("code")) in wanted]


def _violation_severity(code: str) -> str:
    if code in GROUNDING_BLOCKING_VIOLATIONS:
        return "blocking"
    if code in GROUNDING_WARNING_VIOLATIONS:
        return "warning"
    return "warning"


def _check(check_id: str, failures: Sequence[Mapping], *, inputs: Mapping) -> dict:
    """一问答一判：`passed` = 没有失败项；`severity` = 失败项里有没有阻断级的。

    分级**一律**由失败项自己的 `severity` 决定（上游违规码抄 Phase 08，结构性问题在生成
    失败项时就写明），所以这一问永远不可能比它的真源更严。
    """
    rows = [dict(item) for item in failures if isinstance(item, Mapping)]
    blocking = [row for row in rows if row.get("severity") == "blocking"]
    severity = "blocking" if blocking else ("warning" if rows else "none")
    return {
        "check_id": check_id,
        "question": FINAL_VERIFIER_QUESTIONS.get(check_id, ""),
        "passed": not rows,
        "severity": severity,
        "declared_max_severity": CHECK_SEVERITY.get(check_id, "warning"),
        "derived_from": list(DERIVED_FROM.get(check_id) or ()),
        "inputs": dict(inputs),
        "failures": rows[:40],
    }


def verify_answer(*, answer: Mapping, composition: Mapping, graph: Mapping | None = None,
                  pack: Mapping | None = None, grounding: Mapping | None = None) -> dict:
    """§17 Final Verifier：**六问**逐项判定（`FINAL_VERIFIER_CHECKS` 顺序）。

    **单一真源**：`grounding` 不传就现算（`qa_context_pack.check_grounding`，Phase 08 的口径），
    传了就用传进来的（守门用例正是靠这一点证明"改 Phase 08 的回执 → 终验跟着变"）；
    "证据是否真正支持"直接读 Phase 03 的 claim 级 pairs（`qa_verifier`），
    "过度推理"里的数字一致性直接调 Phase 03 的 `check_numbers()`。
    """
    answer = answer if isinstance(answer, Mapping) else {}
    graph = graph if isinstance(graph, Mapping) else {}
    composition = composition if isinstance(composition, Mapping) else {}
    if grounding is None:
        grounding = _grounding_for(answer, pack=pack, graph=graph, composition=composition)
    grounding = grounding if isinstance(grounding, Mapping) else {}
    body = _answer_body(answer)
    bindings = [row for row in (composition.get("bindings") or []) if isinstance(row, Mapping)]
    in_answer = [row for row in bindings if row.get("in_answer")]

    # ── ① 每个事实是否有证据？─────────────────────────────────────────────
    failures = []
    unbound = [row for row in in_answer if not row.get("bound")]
    for row in unbound:
        failures.append({
            "claim_id": str(row.get("claim_id") or ""),
            "binding_reason": str(row.get("binding_reason") or ""),
            "severity": "blocking",
            "detail": "结论 %s 在答案里，但没有任何可回溯证据（%s）"
                      % (row.get("claim_id"), row.get("binding_reason")),
            "source": "qa-answer-composer-v1:binding",
        })
    for item in _violations(grounding, ("CLAIM_WITHOUT_EVIDENCE", "CITATION_WITHOUT_SPAN")):
        failures.append({
            "claim_id": str(item.get("claim_id") or ""),
            "code": str(item.get("code")),
            "severity": _violation_severity(str(item.get("code"))),
            "detail": str(item.get("detail") or ""),
            "source": "qa-grounding-v1:%s" % item.get("code"),
        })
    fact_check = _check("FACT_WITHOUT_EVIDENCE", failures, inputs={
        "claims_in_answer": len(in_answer), "unbound": len(unbound),
        "grounding_codes": sorted({str(item.get("code")) for item in failures if item.get("code")}),
    })

    # ── ② 证据是否真正支持？──────────────────────────────────────────────
    # 分级口径**照抄 Phase 08**：只有"证据与结论相悖"是阻断级
    # （Phase 08 对"引用的证据没通过核验"给的是告警级 `CLAIM_WITH_UNVERIFIED_EVIDENCE`），
    # 其余（限定支持 / 没有核验结论）都是告警级——终验不许比它的真源更严。
    failures = []
    states = {"SUPPORTED": 0, "QUALIFIED": 0, "UNVERIFIED": 0, "REFUTED": 0, "NO_VERDICT": 0}
    for row in in_answer:
        entries = [entry for entry in (row.get("evidence") or []) if isinstance(entry, Mapping)]
        traceable = [entry for entry in entries if entry.get("grounded")]
        verdicts = {str(entry.get("verdict") or "") for entry in traceable}
        status = str(row.get("claim_status") or "")
        if EVIDENCE_STATUS_SUPPORTED in verdicts or status == "confirmed":
            states["SUPPORTED"] += 1
            continue
        if EVIDENCE_STATUS_REFUTED in verdicts or status == "conflicted":
            states["REFUTED"] += 1
            failures.append({"claim_id": str(row.get("claim_id") or ""), "severity": "blocking",
                             "verdicts": sorted(verdicts) or [status],
                             "detail": "证据与结论相悖（Phase 03 判 conflicted/refuted）",
                             "source": "qa-verifier-v1:claim_pairs.verdict"})
            continue
        if not traceable:
            states["UNVERIFIED"] += 1
            failures.append({"claim_id": str(row.get("claim_id") or ""), "severity": "warning",
                             "detail": "结论没有任何可回溯证据参与核验（Phase 03 无从判支持）",
                             "source": "qa-verifier-v1:claim_pairs.verdict"})
            continue
        if EVIDENCE_STATUS_QUALIFIED in verdicts or EVIDENCE_STATUS_CONTEXT in verdicts:
            states["QUALIFIED"] += 1
            failures.append({"claim_id": str(row.get("claim_id") or ""), "severity": "warning",
                             "verdicts": sorted(verdicts),
                             "detail": "证据只能限定支持（Phase 03 判 qualified），不得当作已确证",
                             "source": "qa-verifier-v1:claim_pairs.verdict"})
            continue
        states["NO_VERDICT"] += 1
        failures.append({"claim_id": str(row.get("claim_id") or ""), "severity": "warning",
                         "verdicts": sorted(verdicts) or ["<无核验结论>"],
                         "detail": "证据上没有 Phase 03 的核验结论（没有核验就不算支持）",
                         "source": "qa-verifier-v1:claim_pairs.verdict"})
    support_check = _check("EVIDENCE_NOT_SUPPORTING", failures, inputs=states)

    # ── ③ 有没有过度推理？────────────────────────────────────────────────
    failures = []
    for item in _violations(grounding, ("NUMBER_WITHOUT_SPAN",)):
        failures.append({
            "number": item.get("number"), "severity": _violation_severity("NUMBER_WITHOUT_SPAN"),
            "detail": str(item.get("detail") or ""),
            "source": "qa-grounding-v1:NUMBER_WITHOUT_SPAN",
        })
    number_checked = number_failed = 0
    unentailed = []
    for row in in_answer:
        text = str(row.get("text") or "")
        grounded_entries = [entry for entry in (row.get("evidence") or [])
                            if isinstance(entry, Mapping) and entry.get("grounded")]
        spans = " ".join(str((entry.get("span") or {}).get("quote") or "") for entry in grounded_entries)
        if text and spans:
            number_checked += 1
            verdict = check_numbers(text, spans)
            if not verdict.get("passed", True):
                number_failed += 1
                failures.append({
                    "claim_id": str(row.get("claim_id") or ""), "severity": "warning",
                    "missing": list(verdict.get("missing") or [])[:6],
                    "detail": "结论里的数字在它自己的证据 span 里找不到（超出证据的推断）",
                    "source": "qa-verifier-v1:check_numbers",
                })
        # "证据在、但推不出这个结论"：Phase 03 的规则蕴含判 UNVERIFIED —— 这正是过度推理
        verdicts = {str(entry.get("verdict") or "") for entry in grounded_entries}
        if EVIDENCE_STATUS_UNVERIFIED in verdicts and EVIDENCE_STATUS_SUPPORTED not in verdicts:
            unentailed.append({"claim_id": str(row.get("claim_id") or ""), "severity": "warning",
                               "verdicts": sorted(verdicts),
                               "detail": "证据在，但不能蕴含这条结论（Phase 03 判 unverified）",
                               "source": "qa-verifier-v1:claim_pairs.verdict"})
    failures.extend(unentailed)
    over_check = _check("OVER_INFERENCE", failures, inputs={
        "numbers_checked": number_checked, "numbers_over_reach": number_failed,
        "claims_not_entailed": len(unentailed),
    })

    # ── ④ 是否漏掉重要反证？──────────────────────────────────────────────
    failures = []
    counters = [row for row in (composition.get("counter_evidence") or []) if isinstance(row, Mapping)]
    omitted = [row for row in counters if not row.get("cited_in_answer")]
    for row in omitted:
        failures.append({
            "claim_id": str(row.get("claim_id") or ""), "evidence_ref": str(row.get("evidence_ref") or ""),
            "severity": "warning", "detail": "该结论的反证没有出现在答案里",
            "source": "qa-answer-composer-v1:counter_evidence",
        })
    for item in _violations(grounding, ("COUNTER_EVIDENCE_OMITTED",)):
        failures.append({
            "claim_id": str(item.get("claim_id") or ""), "severity": _violation_severity(
                "COUNTER_EVIDENCE_OMITTED"),
            "detail": str(item.get("detail") or ""), "source": "qa-grounding-v1:COUNTER_EVIDENCE_OMITTED",
        })
    counter_check = _check("COUNTER_EVIDENCE_MISSING", failures, inputs={
        "counter_evidence": len(counters), "omitted": len(omitted),
    })

    # ── ⑤ citation 是否正确？────────────────────────────────────────────
    failures = []
    for item in _violations(grounding, ("CITATION_NOT_IN_MAP",)):
        failures.append({"label": str(item.get("label") or ""),
                         "evidence_ref": str(item.get("evidence_ref") or ""),
                         "severity": _violation_severity("CITATION_NOT_IN_MAP"),
                         "detail": str(item.get("detail") or ""),
                         "source": "qa-grounding-v1:CITATION_NOT_IN_MAP"})
    index = composition.get("citation_index") if isinstance(composition.get("citation_index"),
                                                            Mapping) else {}
    index_source = str(composition.get("citation_index_source") or "derived")
    answer_map = answer.get("citation_map") if isinstance(answer.get("citation_map"), Mapping) else {}
    # 有包（`pack`）= 编号↔证据的对应关系是**权威**的（Phase 08 与生成端同口径），
    # 答案与它不一致就是引用错误（阻断级）；没有包（`derived`）= 编号本来就可以重算，
    # 此时**答案自带的 citation_map 才是那一轮的权威**，现算编号只用来判断
    # "被引用的证据在不在证据域内"，对应关系不一致只记告警（检测更细、分级不许更狠）。
    assignment_severity = "blocking" if index_source == "pack" else "warning"
    mismatched = [label for label, ref in answer_map.items()
                  if str(label) in index
                  and str(index[str(label)].get("evidence_ref")) != str(ref)]
    for label in sorted(mismatched)[:20]:
        failures.append({"label": str(label), "severity": assignment_severity,
                         "index_source": index_source,
                         "detail": "答案的引用编号 %s 与引用索引指向不同证据" % label,
                         "source": "qa-context-pack-v1:citation_index"})
    unknown_labels = [str(label) for label in answer_map if str(label) not in index]
    for label in sorted(unknown_labels)[:20]:
        failures.append({"label": label, "severity": assignment_severity,
                         "index_source": index_source,
                         "detail": "答案的引用编号 %s 不在引用索引里" % label,
                         "source": "qa-context-pack-v1:citation_index"})
    citations = [str(ref) for ref in (answer.get("citations") or [])]
    known_refs = {str(entry.get("evidence_ref")) for entry in index.values()
                  if isinstance(entry, Mapping)}
    dangling = [ref for ref in citations if known_refs and ref not in known_refs]
    for ref in dangling[:20]:
        # 分级与检测范围都写清楚：Phase 08 只查 **claim 级引用**，这里连 `citations`（含冲突
        # 条目带出的引用）一起查——检测更细，但分级保持**告警**，绝不比上游更严。
        failures.append({"evidence_ref": ref, "severity": "warning",
                         "detail": "答案的 citations 里有引用索引之外的证据 %s" % ref,
                         "source": "qa-context-pack-v1:citation_index"})
    citation_check = _check("CITATION_MISBOUND", failures, inputs={
        "labels": len(index), "index_source": index_source,
        "answer_map": len(answer_map), "mismatched": len(mismatched),
        "unknown_labels": len(unknown_labels),
        "citations": len(citations), "dangling": len(dangling),
    })

    # ── ⑥ 有没有把推测写成事实？───────────────────────────────────────────
    failures = []
    for item in _violations(grounding, ("UNMARKED_UNGROUNDED_TEXT",)):
        failures.append({"severity": _violation_severity("UNMARKED_UNGROUNDED_TEXT"),
                         "claim_ids": list(item.get("claim_ids") or [])[:6],
                         "detail": str(item.get("detail") or ""),
                         "source": "qa-grounding-v1:UNMARKED_UNGROUNDED_TEXT"})
    speculated = []
    plan_only_asserted = []
    unsupported_asserted = []
    for row in in_answer:
        status = str(row.get("claim_status") or "")
        text = str(row.get("text") or "")
        if row.get("plan_only"):
            plan_only_asserted.append({"claim_id": str(row.get("claim_id") or ""),
                                       "severity": "blocking",
                                       "detail": "计划态 claim（§5 的\"要证实什么\"）被当成已下结论陈述",
                                       "source": "qa-execution-graph-v1:plan_only"})
            continue
        if status in ("confirmed",):
            continue
        if not text or _has_uncertainty_mark(body, text):
            continue
        entry = {"claim_id": str(row.get("claim_id") or ""),
                 "claim_status": status or "unverified",
                 "severity": "warning",
                 "detail": "未获已核验支持的结论被当成事实陈述（正文没有不确定性表达）",
                 "source": "qa-verifier-v1:claim_pairs.verdict"}
        speculated.append(entry)
        if status in ("unverified", "insufficient_evidence", "conflicted", ""):
            unsupported_asserted.append(entry)
    failures.extend(plan_only_asserted)
    failures.extend(speculated)
    speculation_check = _check("SPECULATION_AS_FACT", failures, inputs={
        "plan_only_in_answer": len(plan_only_asserted),
        "unhedged_unsupported": len(unsupported_asserted),
        "unhedged_total": len(speculated),
        "unmarked_text": len([item for item in failures if item.get("claim_ids")]),
    })

    checks = [fact_check, support_check, over_check, counter_check, citation_check, speculation_check]
    failed = [check["check_id"] for check in checks if not check["passed"]]
    blocking_codes = [check["check_id"] for check in checks if check["severity"] == "blocking"]
    warning_codes = [check["check_id"] for check in checks if check["severity"] == "warning"]
    report = {
        "verifier_version": FINAL_VERIFIER_VERSION,
        "composer_version": str(composition.get("composer_version") or ANSWER_COMPOSER_VERSION),
        "verifier_core": VERIFIER_VERSION,
        "grounding_version": str(grounding.get("grounding_version") or ""),
        "outcome": "pass" if not failed else "fail",
        "checks": checks,
        "failed_checks": failed,
        "blocking_codes": blocking_codes,
        "warning_codes": warning_codes,
        "grounding": {
            "blocking": bool(grounding.get("blocking")),
            "blocking_codes": list(grounding.get("blocking_codes") or []),
            "warning_codes": list(grounding.get("warning_codes") or []),
            "violation_counts": dict(grounding.get("violation_counts") or {}),
            "checked_claims": int(grounding.get("checked_claims") or 0),
            "grounded_claims": int(grounding.get("grounded_claims") or 0),
        },
        "stats": {
            "claims_checked": len(bindings),
            "claims_in_answer": len(in_answer),
            "passed_checks": len(checks) - len(failed),
            "failed_checks": len(failed),
        },
        "note": ("六问全部由 Phase 03 的核验结论与 Phase 08 的 grounding 回执派生（derived_from 可查）；"
                 "本模块只判定与记账，不重写正文、不调模型。"),
    }
    ok, note = validate_contract("final_verification", report)
    report["schema_ok"] = bool(ok)
    if not ok:
        report["schema_note"] = note
    return report


def _pack_for(composition: Mapping, answer: Mapping, pack: Mapping | None) -> dict:
    """校验用的"证据域"：**有包用包**（Phase 08 单一真源）；没有包就从绑定结构现造一个同形包。

    为什么必须现造：没有包的链路（快速路径 / 未开 `QA_CONTEXT_PACK`）里，Phase 08 的
    `check_grounding(answer, pack={})` 会把**所有** `[n]` 与 evidence_ref 判成"引用越界"
    ——那是"包是空的"造成的假阳性，不是答案的问题。现造的包只装**这份答案真正能用的证据**
    （绑定结构里的 `citation_index` 与每条证据的 verdict/grounded），于是 Phase 08 的七条规则
    在同一个真源上继续生效，一个字都没改。
    """
    pack = pack if isinstance(pack, Mapping) else {}
    if pack.get("citation_index") or pack.get("citation_map"):
        return dict(pack)
    index = composition.get("citation_index") if isinstance(composition.get("citation_index"),
                                                            Mapping) else {}
    items: list = []
    for row in (composition.get("bindings") or []):
        if not isinstance(row, Mapping):
            continue
        for entry in (row.get("evidence") or []):
            if not isinstance(entry, Mapping):
                continue
            span = _mapping(entry.get("span"))
            items.append({
                "kind": "evidence", "claim_id": str(row.get("claim_id") or ""),
                "evidence_ref": str(entry.get("evidence_ref") or ""),
                "text": str(span.get("quote") or "")[:400],
                "metadata": {"verdict": str(entry.get("verdict") or ""),
                             "verdict_source": str(entry.get("verdict_source") or "")},
                "grounding": {"grounded": bool(entry.get("grounded")),
                              "evidence_ref": str(entry.get("evidence_ref") or ""),
                              "evidence_id": str(entry.get("evidence_id") or ""),
                              "span": span,
                              "source_url": str(entry.get("source_url") or "")},
            })
    for row in (composition.get("counter_evidence") or []):
        if isinstance(row, Mapping):
            items.append({"kind": "counter_evidence", "claim_id": str(row.get("claim_id") or ""),
                          "evidence_ref": str(row.get("evidence_ref") or ""),
                          "text": str(row.get("quote") or ""),
                          "metadata": {"verdict": EVIDENCE_STATUS_REFUTED},
                          "grounding": {"grounded": bool(row.get("evidence_ref"))}})
    return {
        "pack_version": CONTEXT_PACK_VERSION,
        "sections": {},
        "items": items,
        "citation_map": {label: entry.get("evidence_ref")
                         for label, entry in index.items() if isinstance(entry, Mapping)},
        "citation_index": {label: {"evidence_ref": entry.get("evidence_ref"),
                                   "evidence_id": entry.get("evidence_id"),
                                   "quote": entry.get("quote"),
                                   "span": _mapping(entry.get("span")),
                                   "grounded": bool(entry.get("grounded")),
                                   "verdict": entry.get("verdict")}
                           for label, entry in index.items() if isinstance(entry, Mapping)},
        "budget": {}, "stats": {"derived": True, "reason": "没有上下文包：按绑定结构现造证据域"},
    }


def _grounding_for(answer: Mapping, *, pack: Mapping | None, graph: Mapping | None,
                   composition: Mapping | None = None) -> dict:
    """Phase 08 的生成端 grounding 回执（**唯一真源**）：包外但本轮合法的证据也参与校验。"""
    pack = _pack_for(composition or {}, answer, pack)
    pack_refs = {str(ref) for ref in (pack.get("citation_map") or {}).values() if str(ref)}
    extra = []
    for item in ((graph or {}).get("evidence") or []):
        if not isinstance(item, Mapping):
            continue
        ref = str(item.get("evidence_ref") or "")
        if ref and ref not in pack_refs:
            extra.append(normalize_evidence_layer(item))
    return check_grounding(answer, pack=pack, extra_evidence=extra)


# ── P13-05：局部重试（只重跑 Composer / 只重跑相关 Verifier）─────────────────

RETRY_SCOPE_BY_CODE = {
    "FACT_WITHOUT_EVIDENCE": "composer",
    "CITATION_MISBOUND": "composer",
    "COUNTER_EVIDENCE_MISSING": "composer",
    "SPECULATION_AS_FACT": "composer",
    "EVIDENCE_NOT_SUPPORTING": "verifier",
    "OVER_INFERENCE": "verifier",
}
"""失败码 → 重试作用域（§17 的两个出口，**没有第三个**）。

为什么这样分：绑定/引用/标注/反证展示这四类问题**重跑 Composer 就能修**
（在同一批证据上重新绑定、把反证与缺口写进答案）；"证据是否真正支持"与"是否过度推理"
是**核验结论**的问题，重跑 Composer 改不动它，只能**只重跑受影响 claim 的核验**。"""


def plan_retry(verification: Mapping) -> dict:
    """按失败码决定重试作用域（可复算：`scope` 完全由 `failed_checks` 与映射表决定）。"""
    verification = verification if isinstance(verification, Mapping) else {}
    failed = [str(code) for code in (verification.get("failed_checks") or [])]
    by_code = {code: RETRY_SCOPE_BY_CODE[code] for code in failed if code in RETRY_SCOPE_BY_CODE}
    scope = [name for name in FINAL_RETRY_SCOPES if name in set(by_code.values())]
    return {"scope": scope, "scope_by_code": by_code, "failed_checks": failed}


def retry_composer(*, graph: Mapping | None = None, answer: Mapping | None = None,
                   composition: Mapping | None = None, pack: Mapping | None = None,
                   question: str = "") -> dict:
    """§17 的"只重跑 Composer"：**在同一批证据上**重新绑定，并把绑不上的结论摘出去。

    具体动作（每一步都留痕在 `changes` 里）：
      1. 重新组装绑定（`compose_answer`，输入一字未变——所以这是"重跑"而不是"重新检索"）；
      2. 从 `answer.claims` 里**摘掉绑不上的结论**（它们不能在答案里冒充已核验结论）；
      3. `citations` 只保留可回溯的证据（避免引用指向没有 span 的证据）；
      4. 把**没被引用的反证**与**没被提到的未解决缺口**写进答案（§16 要求"漏掉重要反证"
         必须显式呈现）；
      5. 重跑 Phase 08 的 grounding 校验；仍有违规就用 Phase 08 的 `mark_ungrounded_answer`
         口径显式标注（单一真源，不另造标注）。
    """
    answer = answer if isinstance(answer, Mapping) else {}
    fresh = compose_answer(graph=graph, answer=answer, pack=pack, question=question)
    changes: list = []

    dropped = [str(row.get("claim_id") or "") for row in fresh["bindings"]
               if not row.get("bound") and row.get("in_answer")]
    dropped_set = set(dropped)
    kept_claims = [row for row in _answer_claims(answer) if _claim_id_of(row) not in dropped_set]
    if len(kept_claims) != len(_answer_claims(answer)):
        changes.append({"action": "drop_unbound_claims", "claim_ids": dropped[:20],
                        "before": len(_answer_claims(answer)), "after": len(kept_claims),
                        "reason": "绑不上可回溯证据的结论不得留在 claims 里冒充已核验结论"})

    traceable = {str(entry.get("evidence_ref")) for row in fresh["bindings"]
                 for entry in (row.get("evidence") or []) if entry.get("grounded")}
    citations = [str(ref) for ref in (answer.get("citations") or [])]
    kept_citations = [ref for ref in citations if not traceable or ref in traceable]
    if len(kept_citations) != len(citations):
        changes.append({"action": "rebind_citations", "before": len(citations),
                        "after": len(kept_citations),
                        "removed": sorted(set(citations) - set(kept_citations))[:20],
                        "reason": "引用必须指向可回溯的证据"})

    result = dict(answer)
    result["claims"] = kept_claims
    result["citations"] = list(dict.fromkeys(kept_citations))

    block_lines: list = []
    added_counter = []
    for row in fresh.get("counter_evidence") or []:
        if row.get("cited_in_answer"):
            continue
        added_counter.append({"claim_id": row.get("claim_id"), "evidence_ref": row.get("evidence_ref")})
        block_lines.append("- 反证（结论 %s）：%s %s"
                           % (row.get("claim_id") or "-", row.get("label") or "",
                              str(row.get("quote") or "")[:160]))
    added_gaps = []
    for row in fresh.get("unresolved_gaps") or []:
        if row.get("mentioned_in_answer"):
            continue
        added_gaps.append({"gap_id": row.get("gap_id"), "gap_type": row.get("gap_type")})
        block_lines.append("- 未解决缺口：%s（%s，优先级 %s）"
                           % (row.get("gap_type") or row.get("gap_id") or "-",
                              row.get("band") or "-", row.get("priority")))
    if block_lines:
        header = "以下内容为反证与未解决缺口，必须与正面结论一并阅读："
        text = str(result.get("answer") or "").rstrip()
        result["answer"] = (text + "\n\n" + header + "\n" + "\n".join(block_lines[:20]))[:79000]
        sections = dict(result.get("sections") or {}) if isinstance(result.get("sections"), Mapping) else {}
        uncertainty = sections.get("conflicts_and_uncertainty")
        if not isinstance(uncertainty, Mapping):
            uncertainty = {}
        uncertainty = dict(uncertainty)
        uncertainty["counter_evidence"] = [row.get("evidence_ref") for row in added_counter]
        uncertainty["unresolved_gaps"] = [row.get("gap_id") for row in added_gaps]
        sections["conflicts_and_uncertainty"] = uncertainty
        result["sections"] = sections
        changes.append({"action": "present_counter_and_gaps", "counter_evidence": len(added_counter),
                        "unresolved_gaps": len(added_gaps),
                        "reason": "§16 要求反证与未解决缺口必须在答案里显式呈现"})

    # 重跑 Phase 08 的 grounding（单一真源）→ 仍有违规就用 Phase 08 的口径显式标注
    report = _grounding_for(result, pack=pack, graph=graph, composition=fresh)
    if report.get("violations"):
        from qa_context_pack import mark_ungrounded_answer

        marked = mark_ungrounded_answer(result, report, pack=pack)
        if marked != result:
            changes.append({"action": "mark_ungrounded",
                            "codes": sorted(set(report.get("violation_counts") or {})),
                            "status_before": str(result.get("status") or ""),
                            "status_after": str(marked.get("status") or ""),
                            "reason": "Phase 08 的显式标注口径（阻断级 → partial + 正文标注）"})
            result = marked
    return {"answer": result, "composition": fresh, "grounding": report, "changes": changes,
            "dropped_claims": dropped[:40],
            "added_counter_evidence": added_counter[:40], "added_gaps": added_gaps[:40]}


def retry_verifier(*, graph: Mapping | None = None, composition: Mapping | None = None,
                   verification: Mapping | None = None, store=None, now=None,
                   verify_fn=None) -> dict:
    """§17 的"只重跑相关 Verifier"：把 Phase 03 的规则核验**只**重跑受影响 claim 的子图。

    边界（可机器校验）：`scope_claims` 只含失败检查点名的 claim；`evidence` 只含这些 claim
    引用的证据；**零检索**（不碰任何 Hunter/Retriever）。返回"哪些 claim 的结论变了"。
    """
    graph = graph if isinstance(graph, Mapping) else {}
    verification = verification if isinstance(verification, Mapping) else {}
    plan = plan_retry(verification)
    wanted = [code for code, scope in plan["scope_by_code"].items() if scope == "verifier"]
    targets: list = []
    for check in verification.get("checks") or []:
        if not isinstance(check, Mapping) or str(check.get("check_id")) not in wanted:
            continue
        for failure in check.get("failures") or []:
            if isinstance(failure, Mapping) and failure.get("claim_id"):
                targets.append(str(failure["claim_id"]))
    targets = list(dict.fromkeys(targets))
    nodes = []
    by_id = {}
    for node in _graph_claim_nodes(graph):
        cid = _claim_id_of(node)
        if cid:
            by_id[cid] = node
    for cid in targets:
        node = by_id.get(cid)
        if node is not None:
            nodes.append(node)
    if not nodes:
        return {"scope_claims": [], "changed": [], "before": {}, "after": {},
                "reason": "SCOPE_UNAVAILABLE",
                "note": "要重跑核验的 claim 不在当前结论图里（例如快速路径没有 canonical claim 行）"}

    keep_evidence = {str(ref) for node in nodes for ref in _claim_refs(node)}
    pool = _evidence_pool({}, graph)
    scoped = {
        "version": str(graph.get("version") or ""),
        "claims": nodes,       # 故意传**同一批节点对象**：核验结论就地回写（不重跑检索）
        "evidence": [pool[ref] for ref in keep_evidence if ref in pool],
        "conflicts": [], "stats": {},
    }
    before = {_claim_id_of(node): _claim_status(node) for node in nodes}
    runner = verify_fn or verify_claim_graph
    summary = runner(scoped, store=store, now=now)
    after = {_claim_id_of(node): _claim_status(node) for node in nodes}
    changed = [cid for cid in after if before.get(cid) != after.get(cid)]
    return {"scope_claims": list(after), "changed": changed, "before": before, "after": after,
            "verifier_stats": dict((summary or {}).get("stats") or {}),
            "evidence": len(scoped["evidence"]), "reason": "FIXED" if changed else "NO_CHANGE"}


def run_local_retry(*, graph: Mapping | None = None, answer: Mapping | None = None,
                    composition: Mapping | None = None, verification: Mapping | None = None,
                    pack: Mapping | None = None, question: str = "", attempt: int = 1,
                    max_attempts: int | None = None, store=None, now=None,
                    verify_fn=None) -> dict:
    """执行一次局部重试（先 Verifier 再 Composer —— 核验结论是组装的输入）。

    `retrieval_calls` 恒 0：这是"不重跑整个 Research Graph"的**机器可校验形态**。
    """
    max_attempts = retry_max() if max_attempts is None else int(max_attempts)
    plan = plan_retry(verification)
    record = {
        "retry_version": FINAL_VERIFY_RETRY_VERSION,
        "attempt": int(attempt), "max_attempts": int(max_attempts),
        "scope": list(plan["scope"]), "scope_by_code": dict(plan["scope_by_code"]),
        "reason": "", "composer": {}, "verifier": {}, "changes": [],
        "retrieval_calls": 0, "stats": {},
    }
    if not plan["scope"]:
        record["reason"] = "NO_SCOPE"
        return record
    if attempt > max_attempts:
        record["reason"] = "RETRY_LIMIT_REACHED"
        return record

    result_answer = answer if isinstance(answer, Mapping) else {}
    result_composition = composition if isinstance(composition, Mapping) else {}
    reason = "NO_CHANGE"

    if "verifier" in plan["scope"]:
        outcome = retry_verifier(graph=graph, composition=result_composition,
                                 verification=verification, store=store, now=now,
                                 verify_fn=verify_fn)
        record["verifier"] = {key: value for key, value in outcome.items() if key != "evidence"}
        if outcome.get("changed"):
            reason = "FIXED"
            for cid in outcome["changed"]:
                record["changes"].append({
                    "scope": "verifier", "claim_id": cid,
                    "before": outcome["before"].get(cid), "after": outcome["after"].get(cid),
                    "reason": "只重跑了这条 claim 的规则核验（零检索、零新证据）",
                })
        elif outcome.get("reason") == "SCOPE_UNAVAILABLE":
            reason = "SCOPE_UNAVAILABLE"

    if "composer" in plan["scope"]:
        outcome = retry_composer(graph=graph, answer=result_answer, composition=result_composition,
                                 pack=pack, question=question)
        record["composer"] = {
            "changes": outcome["changes"], "dropped_claims": outcome["dropped_claims"],
            "added_counter_evidence": outcome["added_counter_evidence"],
            "added_gaps": outcome["added_gaps"],
            "grounding_violations": dict((outcome.get("grounding") or {}).get("violation_counts") or {}),
        }
        if outcome["changes"]:
            reason = "FIXED"
            for change in outcome["changes"]:
                record["changes"].append({"scope": "composer", **change})
        result_answer = outcome["answer"]
        result_composition = outcome["composition"]

    record["reason"] = reason
    record["stats"] = {
        "change_rows": len(record["changes"]),
        "verifier_scope_claims": len((record.get("verifier") or {}).get("scope_claims") or []),
        "verifier_changed": len((record.get("verifier") or {}).get("changed") or []),
        "retrieval_calls": 0,
    }
    ok, note = validate_contract("final_retry", record)
    record["schema_ok"] = bool(ok)
    if not ok:
        record["schema_note"] = note
    record["_answer"] = result_answer
    record["_composition"] = result_composition
    return record


# ── P13-06：insufficient-evidence output ────────────────────────────────────

INSUFFICIENT_HEADER = "【证据不足】"
"""证据不足时正文的显式开头（生成端与校验端共读同一个字符串常量）。"""


def insufficient_evidence_report(*, composition: Mapping | None = None,
                                 verification: Mapping | None = None,
                                 answer: Mapping | None = None, graph: Mapping | None = None,
                                 stop_reason: str = "", routes: Iterable = ()) -> dict:
    """P13-06：四条判据（`INSUFFICIENT_EVIDENCE_REASONS`）逐条可复算。

    ① `NO_EVIDENCE`：本轮一条证据都没有；
    ② `NO_VERIFIED_CLAIM`：没有任何**被 Phase 03 判 SUPPORTED 且绑得上可回溯 span** 的 claim；
    ③ `RESEARCH_NOT_ANSWERABLE`：停止原因不是 `ANSWERABLE`（Phase 07 的单一真源）；
    ④ `GROUNDING_BLOCKED`：终验仍有阻断级违规（局部重试也修不掉）。
    """
    graph = graph if isinstance(graph, Mapping) else {}
    composition = composition if isinstance(composition, Mapping) else {}
    verification = verification if isinstance(verification, Mapping) else {}
    answer = answer if isinstance(answer, Mapping) else {}
    pool_size = len(_evidence_pool(answer, graph))
    bindings = [row for row in (composition.get("bindings") or []) if isinstance(row, Mapping)]
    verified = [str(cid) for cid in (composition.get("verified_claims") or [])]
    blocking = [str(code) for code in (verification.get("blocking_codes") or [])]
    stop = str(stop_reason or "")

    codes: list = []
    if pool_size == 0:
        codes.append("NO_EVIDENCE")
    if not verified:
        codes.append("NO_VERIFIED_CLAIM")
    if stop and stop in QA_STOP_REASONS and stop != "ANSWERABLE":
        codes.append("RESEARCH_NOT_ANSWERABLE")
    if blocking:
        codes.append("GROUNDING_BLOCKED")
    triggers = [code for code in codes if code in INSUFFICIENT_EVIDENCE_TRIGGERS]
    insufficient = bool(triggers)
    missing = []
    for row in (composition.get("unresolved_gaps") or [])[:20]:
        if isinstance(row, Mapping):
            missing.append({"gap_id": row.get("gap_id"), "gap_type": row.get("gap_type"),
                            "claim_id": row.get("claim_id"), "band": row.get("band"),
                            "priority": row.get("priority"),
                            "suggested_routes": list(row.get("suggested_routes") or []),
                            "evidence_requirement": dict(row.get("evidence_requirement") or {})})
    report = {
        "insufficient_version": INSUFFICIENT_EVIDENCE_VERSION,
        "insufficient": insufficient,
        "status": "insufficient_evidence" if insufficient else (str(answer.get("status") or "ready")),
        "reason_codes": codes,
        "trigger_codes": triggers,
        "inputs": {
            "evidence": pool_size,
            "claims": len(bindings),
            "claims_in_answer": len([row for row in bindings if row.get("in_answer")]),
            "verified_claims": len(verified),
            "unresolved_gaps": len(composition.get("unresolved_gaps") or []),
            "blocking_codes": blocking,
            "stop_reason": stop,
            "trigger_codes": triggers,
        },
        "missing": missing,
        "searched": {"stop_reason": stop,
                     "routes": sorted({str(route) for route in (routes or []) if str(route)})},
    }
    ok, note = validate_contract("insufficient_evidence", report)
    report["schema_ok"] = bool(ok)
    if not ok:
        report["schema_note"] = note
    return report


def apply_insufficient_evidence(answer: Mapping, report: Mapping) -> dict:
    """把"证据不足"落到答案上（**只用冻结 schema 的已有字段**）。

    会改变用户看到的东西的三处（可复算）：
      · `status`：`ready`/`partial` → `insufficient_evidence`；
      · `answer`：正文顶部插入显式说明块（判据码 + 停止原因 + 仍缺什么 + 需要什么证据）；
      · `degraded` / `degradation_reasons`：置 True 并追加一条含判据码的原因。
    幂等：同一份回执跑两次结果逐字相同（正文里的 `INSUFFICIENT_HEADER` 已存在就不再插）。
    """
    result = dict(answer) if isinstance(answer, Mapping) else {}
    report = report if isinstance(report, Mapping) else {}
    if not report.get("insufficient"):
        return result
    reasons = [str(code) for code in (report.get("reason_codes") or [])]
    inputs = _mapping(report.get("inputs"))
    lines = [INSUFFICIENT_HEADER + "本轮没有取到足以支撑结论的已验证证据，以下只说明已知情况，不构成结论。"]
    lines.append("判定依据：" + "、".join(reasons) +
                 "（证据 %s 条 / 结论 %s 条 / 通过核验 %s 条 / 停止原因 %s）"
                 % (inputs.get("evidence"), inputs.get("claims"),
                    inputs.get("verified_claims"), inputs.get("stop_reason") or "-"))
    missing = [row for row in (report.get("missing") or []) if isinstance(row, Mapping)]
    if missing:
        lines.append("仍缺：" + "；".join(
            "%s（%s，优先级 %s）" % (row.get("gap_type") or "-", row.get("claim_id") or "-",
                                     row.get("band") or "-") for row in missing[:6]))
        routes = sorted({str(route) for row in missing for route in (row.get("suggested_routes") or [])
                         if str(route)})
        if routes:
            lines.append("建议补齐渠道：" + "、".join(routes))
    block = "\n".join(lines)
    text = str(result.get("answer") or "")
    if INSUFFICIENT_HEADER not in text:
        result["answer"] = (block + "\n\n" + text.lstrip())[:79000]
    result["status"] = "insufficient_evidence"
    result["degraded"] = True
    reasons_list = list(result.get("degradation_reasons") or [])
    entry = ("证据不足输出（%s）：%s" % (INSUFFICIENT_EVIDENCE_VERSION,
                                        "、".join(reasons)))[:500]
    if entry not in reasons_list and len(reasons_list) < 20:
        reasons_list.append(entry)
    result["degradation_reasons"] = reasons_list
    return result


# ── 入口：一次完整的最终核验（组装 → 六问 → 局部重试 → 证据不足输出）──────────

def final_verification_layer(*, graph: Mapping | None = None, answer: Mapping | None = None,
                             pack: Mapping | None = None, question: str = "",
                             stop_reason: str = "", routes: Iterable = (),
                             store=None, run_id: str = "", attempts: int | None = None,
                             now=None, verify_fn=None) -> dict:
    """P13 的一步到位入口（管线只调这一个函数）。

    返回 `{composition, verification, retries, insufficient, answer, receipt, stats}`：
      · `answer` 是**最终答案**（仍是冻结 schema 的字段集合，可直接过 `validate_final_answer`）；
      · `retries` 是局部重试记录（每条的 `retrieval_calls` 恒 0）；
      · `insufficient` 是 P13-06 的回执（`insufficient=False` 时答案不动）。
    失败路径：任何内部异常都收敛成 `error` 字段并**原样返回输入答案**（终验绝不能把答案弄丢）。
    """
    answer = dict(answer) if isinstance(answer, Mapping) else {}
    graph = graph if isinstance(graph, Mapping) else {}
    rounds: list = []
    try:
        composition = compose_answer(graph=graph, answer=answer, pack=pack, question=question)
        verification = verify_answer(answer=answer, composition=composition, graph=graph, pack=pack)
        retries: list = []
        limit = retry_max() if attempts is None else int(attempts)
        attempt = 0

        def _snapshot() -> dict:
            """本轮的收据（每轮一份 → 落库后能逐轮复算，而不是只看最后一眼）。"""
            return final_verification_receipt({
                "verification": verification, "composition": composition,
                "insufficient": _round_insufficient(composition, verification, answer, graph,
                                                    stop_reason, routes),
                "retries": list(retries), "stats": {}, "error": "",
                "round": len(rounds) + 1,
            })

        rounds.append(_snapshot())
        while verification.get("outcome") == "fail" and attempt < limit:
            attempt += 1
            record = run_local_retry(graph=graph, answer=answer, composition=composition,
                                     verification=verification, pack=pack, question=question,
                                     attempt=attempt, max_attempts=limit, store=store, now=now,
                                     verify_fn=verify_fn)
            answer = record.pop("_answer", answer)
            composition = record.pop("_composition", composition)
            retries.append(record)
            if record.get("reason") in ("NO_SCOPE", "RETRY_LIMIT_REACHED", "SCOPE_UNAVAILABLE",
                                        "NO_CHANGE"):
                rounds.append(_snapshot())
                break
            verification = verify_answer(answer=answer, composition=composition, graph=graph,
                                         pack=pack)
            rounds.append(_snapshot())
        insufficient = insufficient_evidence_report(
            composition=composition, verification=verification, answer=answer, graph=graph,
            stop_reason=stop_reason, routes=routes)
        if insufficient.get("insufficient"):
            answer = apply_insufficient_evidence(answer, insufficient)
        else:
            codes = [str(code) for code in (insufficient.get("reason_codes") or [])]
            if verification.get("outcome") == "fail":
                # 只是告警级没修好：按 Phase 08 的口径显式披露（告警不降档、不置 degraded，
                # 与 `qa_context_pack.mark_ungrounded_answer` 的行为逐字一致）
                grounding = _grounding_for(answer, pack=pack, graph=graph, composition=composition)
                if grounding.get("violations"):
                    from qa_context_pack import mark_ungrounded_answer

                    answer = mark_ungrounded_answer(answer, grounding, pack=pack)
                codes = codes + list(verification.get("warning_codes") or [])
            if codes:
                reasons = list(answer.get("degradation_reasons") or [])
                entry = ("最终核验告警（%s）：%s" % (FINAL_VERIFIER_VERSION,
                                                  "、".join(sorted(set(codes))[:6])))[:500]
                if entry not in reasons and len(reasons) < 20:
                    reasons.append(entry)
                answer["degradation_reasons"] = reasons
        layer = {
            "layer_version": FINAL_VERIFIER_VERSION,
            "answer": answer,
            "composition": composition,
            "verification": verification,
            "retries": retries,
            "rounds": rounds,
            "insufficient": insufficient,
            "stats": {
                "claims": len(composition.get("bindings") or []),
                "verified_claims": len(composition.get("verified_claims") or []),
                "failed_checks": list(verification.get("failed_checks") or []),
                "blocking_codes": list(verification.get("blocking_codes") or []),
                "retries": len(retries),
                "retrieval_calls": sum(int(row.get("retrieval_calls") or 0) for row in retries),
                "insufficient_reasons": list(insufficient.get("reason_codes") or []),
                "status": str(answer.get("status") or ""),
            },
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001 —— 终验绝不能把答案弄丢
        layer = {
            "layer_version": FINAL_VERIFIER_VERSION,
            "answer": answer, "composition": {}, "verification": {}, "retries": [],
            "rounds": rounds, "insufficient": {},
            "stats": {"status": str(answer.get("status") or "")},
            "error": "%s: %s" % (type(exc).__name__, str(exc)[:200]),
        }
    layer["receipt"] = final_verification_receipt(layer)
    return layer


def _round_insufficient(composition: Mapping, verification: Mapping, answer: Mapping,
                        graph: Mapping, stop_reason: str, routes) -> dict:
    """本轮的证据不足判定（收据用；正式输出再用一次，保证"看到的"与"判过的"一致）。"""
    try:
        return insufficient_evidence_report(composition=composition, verification=verification,
                                           answer=answer, graph=graph, stop_reason=stop_reason,
                                           routes=routes)
    except Exception as exc:  # noqa: BLE001
        return {"insufficient": False, "reason_codes": [], "status": "",
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}


def final_verification_receipt(layer: Mapping) -> dict:
    """给 stats / SSE / 运维看的回执（**不含正文**，只有口径与计数）。"""
    layer = layer if isinstance(layer, Mapping) else {}
    verification = layer.get("verification") if isinstance(layer.get("verification"), Mapping) else {}
    composition = layer.get("composition") if isinstance(layer.get("composition"), Mapping) else {}
    insufficient = layer.get("insufficient") if isinstance(layer.get("insufficient"), Mapping) else {}
    stats = layer.get("stats") if isinstance(layer.get("stats"), Mapping) else {}
    return {
        "layer_version": FINAL_VERIFIER_VERSION,
        "round": int(layer.get("round") or 0),
        "composer_version": ANSWER_COMPOSER_VERSION,
        "retry_version": FINAL_VERIFY_RETRY_VERSION,
        "insufficient_version": INSUFFICIENT_EVIDENCE_VERSION,
        "verifier_core": VERIFIER_VERSION,
        "context_pack_version": CONTEXT_PACK_VERSION,
        "grounding_version": str((verification.get("grounding") or {}).get("grounding_version") or ""),
        "outcome": str(verification.get("outcome") or ""),
        "failed_checks": list(verification.get("failed_checks") or []),
        "blocking_codes": list(verification.get("blocking_codes") or []),
        "checks": [{"check_id": check.get("check_id"), "passed": bool(check.get("passed")),
                    "severity": check.get("severity"),
                    "failures": len(check.get("failures") or [])}
                   for check in (verification.get("checks") or []) if isinstance(check, Mapping)],
        "claims": int(composition.get("inputs", {}).get("claims") or 0),
        "verified_claims": len(composition.get("verified_claims") or []),
        "citation_labels": int(composition.get("inputs", {}).get("citation_labels") or 0),
        "counter_evidence": int(composition.get("inputs", {}).get("counter_evidence") or 0),
        "unresolved_gaps": int(composition.get("inputs", {}).get("unresolved_gaps") or 0),
        "retries": [
            {"attempt": row.get("attempt"), "scope": list(row.get("scope") or []),
             "reason": row.get("reason"), "changes": len(row.get("changes") or []),
             "retrieval_calls": int(row.get("retrieval_calls") or 0)}
            for row in (layer.get("retries") or []) if isinstance(row, Mapping)],
        "insufficient": bool(insufficient.get("insufficient")),
        "insufficient_reasons": list(insufficient.get("reason_codes") or []),
        "status": str(insufficient.get("status") or stats.get("status") or ""),
        "stats": dict(stats),
        "error": str(layer.get("error") or ""),
    }


__all__ = [
    "CHECK_SEVERITY", "DERIVED_FROM", "INSUFFICIENT_HEADER", "RETRY_SCOPE_BY_CODE",
    "apply_insufficient_evidence", "compose_answer", "final_verification_layer",
    "final_verification_receipt", "final_verifier_enabled", "insufficient_evidence_report",
    "plan_retry", "retry_composer", "retry_max", "retry_verifier", "run_local_retry",
    "uncertainty_marks", "verify_answer",
]
