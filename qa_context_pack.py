#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 08（P08-01…P08-06）· Context Graph / Context Planner + 生成端 grounding 约束
（**纯规则/统计，零模型调用，零嵌入端点**）。

通用包 01_V2_ARCHITECTURE 依据：
  · §4 Context Engineering：`Persistent State → Context Planner → Context Graph →
    Context Pack → Agent`；统一 Context Pack 的九段 `system_context / task_context /
    evidence_context / counter_evidence / memory_context / skill_context / working_memory /
    constraints / budget` **逐字**入契约；并且"BM25 Hunter 不应看到完整 Evidence Graph；
    Answer Composer 主要看到 Verified Claims、Primary/Counter Evidence、Unresolved Gaps
    和 Citation Map"——本模块就是那个**按角色裁剪**的执行者；
  · §5 Context Utility 与预算：`ContextUtility(item, task) = Relevance × EvidenceStrength ×
    TaskNecessity × Freshness × Diversity / TokenCost`；"Context Planner 在 token budget 下
    选择最大价值集合，并保留输出/工具预算。**禁止再把固定 top_k 当成 Context 策略**"；
  · §6 三类缺口：Context Gap = "证据已存在，但当前 Agent 没拿到、摘要不足或缺反证"，
    动作 `REPACK_CONTEXT / EXPAND_EVIDENCE_SPAN / LOAD_COUNTEREVIDENCE / LOAD_SKILL`，
    且 **"Context Gap 默认不得触发昂贵新检索"**（MASTER_RULES 第 13 条）；
  · §7 三层 Planner：本模块只做 **Context Planner** 那一层（Research Planner 在 Phase 05，
    Memory Planner 在 Phase 09），职责不混合；
  · §16 Answer Composer 输入清单 + §17 Final Verifier 的六问（"每个事实是否有证据？引用是否
    正确？有没有把推测写成事实？"）→ 落成 `check_grounding()`（**校验**，不改冻结 schema）；
  · MASTER_RULES 第 11 条：LLM 自由生成内容不能直接成为 Verified Evidence —— 所以引用必须
    能回溯到 Phase 02 的 `evidence_ref` + 最小 span，回溯不上的内容必须显式标注为无证据。

本模块**一行检索/核验逻辑都不重写**，只做"把既有结论组装成最小有效上下文"：
  复用 ① Phase 02 `qa_evidence`：`evidence_object()`（最小 span / 来源身份 / 指纹）、
        `minimal_quote_span()`（没标注过的证据现算 span，口径完全一样）、`evidence_terms()`；
  复用 ② Phase 03 `qa_verifier`：`relevance_score()`（相关性）、`verification_of()`
        （核验结论/分数）——ContextUtility 的 Relevance 与 EvidenceStrength 直接取它们，
        不另造一套质量判断；
  复用 ③ Phase 06 `qa_evidence_graph` 的图级关系（`graph_relation` 五值）与矛盾裁决
        （`resolution` / `reason_code`）——反证身份与"漏反证"判定的唯一来源；
  复用 ④ Phase 07 `qa_gap_analyzer` 的缺口清单（`MISSING_COUNTEREVIDENCE`）；
  复用 ⑤ Phase 05 执行图的计划（子问题/计划 claim）当作 §4 的 `task_context`；
  复用 ⑥ 冻结契约 `qa_graph_contracts.CONTEXT_*`（本阶段新增，七指纹不受影响）。

边界与取舍（诚实声明，宁写 PARTIAL 不谎报）
--------------------------------------------
  1. **零模型调用**：§5 的五个乘子全部是**词面统计 + 时间衰减**；token 预算用的
     `estimate_tokens()` 是**确定性估算器**（不是真 tokenizer——本机硬约束禁止调任何模型/嵌入
     端点，拿不到 tokenizer）。它是一个**相对预算单位**：同一份输入永远同一个数，因此"裁剪
     前后对比/预算是否超"是可复算的；但它**不等于**任何真实模型的 token 计数（能力边界见
     `ESTIMATOR_NOTE` 与 D-031）。
  2. **memory_context / skill_context 本阶段是空段**：§4 九段里这两段是 Phase 09 / Phase 11
     的产物，本阶段只留**空段 + `deferred_to` 标注**，绝不提前实现后续 Phase，也绝不编内容。
  3. **Context Gap 不触发检索**：`detect_context_gaps()` 产出的每一条 `requires_retrieval`
     恒为 `False`（契约里就是这么写的），模块内零检索器/下一跳规划器引用（有 AST 守门用例）；
     它只产出**重组建议**，要不要真的再召回由上层决定。
  4. **grounding 只校验、不改 schema**：`FINAL_ANSWER_SCHEMA` 是冻结的（`additionalProperties:
     False`），所以生成端约束走"**出包前把约束写进提示 + 出包后校验并按既有可选字段
     `degraded`/`degradation_reasons` 标注 + 正文显式【无证据】标注**"三件事，
     **一个字段都不往冻结 schema 里加**。
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from datetime import datetime, timezone
from typing import Iterable, Mapping, Sequence

from qa_evidence import (
    evidence_fingerprint,
    evidence_object,
    evidence_status,
    evidence_terms,
    minimal_quote_span,
    now_utc,
    source_identity,
    span_max_chars,
)
from qa_graph_contracts import (
    CONTEXT_DECISIONS,
    CONTEXT_GAP_ACTIONS,
    CONTEXT_GAP_TYPES,
    CONTEXT_GAP_VERSION,
    CONTEXT_GROUNDING_VIOLATIONS,
    CONTEXT_ITEM_KINDS,
    CONTEXT_PACK_VERSION,
    CONTEXT_SECTIONS,
    CONTEXT_SELECTION_REASONS,
    CONTEXT_SELECTION_VERSION,
    CONTEXT_UTILITY_FACTORS,
    CONTEXT_UTILITY_VERSION,
    CONTEXT_UTILITY_WEIGHTS,
    EVIDENCE_STATUS_CONTEXT,
    EVIDENCE_STATUS_QUALIFIED,
    EVIDENCE_STATUS_REFUTED,
    EVIDENCE_STATUS_SUPPORTED,
    EVIDENCE_STATUS_UNVERIFIED,
    GROUNDING_VERSION,
    validate as validate_contract,
)
from qa_verifier import relevance_score, term_set, verification_of

ESTIMATOR_VERSION = "qa-token-estimate-v1"
"""token 估算器身份。换估算口径 = 换版本号（预算数字必须能追到口径）。"""

ESTIMATOR_NOTE = ("中文/全角 1 token/字、ASCII 词 1 token/4 字符（每段向上取整）、"
                  "其它符号 0.5、空白 0.25；这是**相对预算单位**，不等于任何真实模型的 tokenizer。")

GROUNDING_BLOCKING_VIOLATIONS = (
    "CLAIM_WITHOUT_EVIDENCE", "CITATION_NOT_IN_MAP", "CITATION_WITHOUT_SPAN",
)
"""阻断级违规：出现就必须走"修复重试 / 降级标注"，不许悄悄放行。"""

GROUNDING_WARNING_VIOLATIONS = (
    "CLAIM_WITH_UNVERIFIED_EVIDENCE", "NUMBER_WITHOUT_SPAN", "COUNTER_EVIDENCE_OMITTED",
    "UNMARKED_UNGROUNDED_TEXT",
)
"""告警级违规：只记账 + 在答案里显式披露（§17 的"漏掉重要反证""过度推理"两类）。"""

UNGROUNDED_MARK = "【无证据】"
"""正文里标注"这条没有可回溯证据"的统一记号（生成端与校验端共用同一个字符串）。"""

# ── 可配置旋钮（全部可回滚；默认值写在常量里便于复算）───────────────────────
DEFAULT_TOTAL_TOKEN_BUDGET = 6000        # §5：整包 token 预算
DEFAULT_OUTPUT_RESERVE = 900             # §5："保留输出/工具预算"
DEFAULT_COUNTER_RESERVE_RATIO = 0.20     # P08-04：反证预留比例（占证据预算）
DEFAULT_MIN_ITEM_TOKENS = 1              # 低于这个 token 数的条目不值得占位
DEFAULT_UTILITY_FLOOR = 0.0              # 效用下限（0 = 不设门槛，只按预算裁）
DEFAULT_FRESHNESS_HALF_LIFE_DAYS = 180.0  # 与 Phase 03 时间适用性的半衰期同口径
DEFAULT_MAX_ITEMS = 400                  # 病态输入保护：候选条目上限
DEFAULT_SPAN_MIN_CHARS = 40              # 短于这个长度的 span 判"摘要不足"（Context Gap）
DEFAULT_MAX_GAPS = 40                    # Context Gap 清单上限
DEFAULT_MAX_TRACE = 600                  # selection trace 上限（可复算的决策留痕）

_SECTION_ORDER = tuple(CONTEXT_SECTIONS)


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


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def context_pack_enabled() -> bool:
    """P08 总开关（默认**关**：与 Phase 06/07 同样手法，保证既有链路的键集与提示逐字不变）。"""
    return _env_flag("QA_CONTEXT_PACK", False)


def grounding_gate_enabled() -> bool:
    """生成端 grounding 闸门开关（默认关；打开后校验 + 修复重试 + 显式标注三件事都生效）。"""
    return _env_flag("QA_GROUNDING_GATE", False)


def total_token_budget() -> int:
    return _env_int("QA_CONTEXT_TOKEN_BUDGET", DEFAULT_TOTAL_TOKEN_BUDGET, 200, 200000)


def output_reserve_tokens() -> int:
    return _env_int("QA_CONTEXT_OUTPUT_RESERVE", DEFAULT_OUTPUT_RESERVE, 0, 100000)


def counter_reserve_ratio() -> float:
    return _env_float("QA_CONTEXT_COUNTER_RESERVE", DEFAULT_COUNTER_RESERVE_RATIO, 0.0, 0.9)


def utility_floor() -> float:
    return _env_float("QA_CONTEXT_UTILITY_FLOOR", DEFAULT_UTILITY_FLOOR, 0.0, 1.0)


def span_min_chars() -> int:
    return _env_int("QA_CONTEXT_SPAN_MIN_CHARS", DEFAULT_SPAN_MIN_CHARS, 8, 1000)


# ── P08-02：确定性 token 估算（相对预算单位）────────────────────────────────

_CJK_RANGES = (
    (0x3000, 0x303F),   # CJK 标点
    (0x3040, 0x30FF),   # 日文假名
    (0x3400, 0x4DBF),   # CJK 扩展 A
    (0x4E00, 0x9FFF),   # CJK 基本区
    (0xAC00, 0xD7AF),   # 谚文
    (0xF900, 0xFAFF),   # CJK 兼容
    (0xFF00, 0xFFEF),   # 全角形式
    (0x20000, 0x2FA1F),  # CJK 扩展 B~
)

_ASCII_TOKEN_CHARS = 4.0
_CJK_TOKEN_CHARS = 1.0
_OTHER_TOKEN_COST = 0.5
_SPACE_TOKEN_COST = 0.25


def estimate_tokens(text) -> int:
    """确定性 token 估算（**纯函数**：同一输入永远同一输出，无随机、无外部状态）。

    口径（见 `ESTIMATOR_NOTE`）：
      · CJK / 全角等"一个字一个 token"的字符 → 1.0；
      · 连续的 ASCII 字母/数字视为一个词 → `len/4` 且**每段至少 1**（向上取整在末尾统一做）；
      · 其它符号（标点/emoji 等）→ 0.5；
      · 空白 → 0.25。
    为什么不做"按词切分"：预算裁剪要在**任何**文本上稳定（含 JSON、URL、乱码），
    字符级规则不会因为分词器差异而漂移。
    """
    total = 0.0
    ascii_run = 0
    for char in str(text if text is not None else ""):
        if char.isascii() and char.isalnum():
            ascii_run += 1
            continue
        if ascii_run:
            total += max(1.0, ascii_run / _ASCII_TOKEN_CHARS)
            ascii_run = 0
        if char.isspace():
            total += _SPACE_TOKEN_COST
            continue
        code = ord(char)
        if any(low <= code <= high for low, high in _CJK_RANGES):
            total += _CJK_TOKEN_CHARS
        else:
            total += _OTHER_TOKEN_COST
    if ascii_run:
        total += max(1.0, ascii_run / _ASCII_TOKEN_CHARS)
    if total <= 0:
        return 0
    return int(math.ceil(total - 1e-9))


def _clamp(value, low: float = 0.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    if math.isnan(number):
        return low
    return max(low, min(high, number))


def _digest(value, length: int = 16) -> str:
    body = str(value if value is not None else "").encode("utf-8")
    return hashlib.sha256(body).hexdigest()[:length]


def _counter(values: Iterable) -> dict:
    counts: dict = {}
    for value in values:
        key = str(value or "")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _metadata(item: Mapping) -> dict:
    value = item.get("metadata") if isinstance(item, Mapping) else None
    return dict(value) if isinstance(value, Mapping) else {}


def _date(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ── P08-01：ContextItem / Context Graph ─────────────────────────────────────

def normalize_evidence_layer(item: Mapping) -> dict:
    """取证据的 Phase 02 证据层对象；**没标注过就现算**（口径一字不改地复用 Phase 02）。

    返回 `{evidence_ref, evidence_id, span, source, status, verification, grounded, reason}`。
    `grounded=False` 表示"这条证据拿不出可回溯的最小 span"——调用方必须显式标注，
    不许伪装成有据（MASTER_RULES 第 11 条）。
    """
    payload = dict(item) if isinstance(item, Mapping) else {}
    layer = evidence_object(payload)
    ref = str(payload.get("evidence_ref") or layer.get("evidence_ref") or "")
    content = str(payload.get("content_excerpt") or "")
    span = layer.get("span") if isinstance(layer.get("span"), Mapping) else None
    if not isinstance(span, Mapping) or not str(span.get("quote") or ""):
        anchors = evidence_terms(payload)
        span = minimal_quote_span(content, anchors, max_chars=span_max_chars())
    else:
        span = dict(span)
    quote = str(span.get("quote") or "")
    identity = layer.get("fingerprint") or evidence_fingerprint(payload, span=span)
    source = layer.get("source") if isinstance(layer.get("source"), Mapping) else source_identity(payload)
    verification = verification_of(payload) or (
        layer.get("verification") if isinstance(layer.get("verification"), Mapping) else {})
    span_ok = bool(quote)
    if not ref:
        reason = "证据缺少 evidence_ref，无法回溯到来源"
    elif not span_ok:
        reason = "证据正文为空或切不出 span，无法给出可校验引用"
    elif not str(span.get("source") or ""):
        reason = "span 缺少切分来源标注"
    else:
        reason = ""
    return {
        "evidence_ref": ref,
        "evidence_id": str(identity or ""),
        "span": {
            "start": int(span.get("start") or 0),
            "end": int(span.get("end") or 0),
            "quote": quote,
            "source": str(span.get("source") or ""),
            "chars": int(span.get("chars") or len(quote)),
        },
        "source": dict(source) if isinstance(source, Mapping) else {},
        "status": str(layer.get("status") or evidence_status(payload)),
        "verification": dict(verification) if isinstance(verification, Mapping) else {},
        "grounded": bool(ref and span_ok),
        "reason": reason,
    }


def make_context_item(*, kind: str, section: str, text: str, source_stage: str = "",
                      grounding: Mapping | None = None, claim_id: str = "",
                      evidence_ref: str = "", metadata: Mapping | None = None,
                      item_id: str = "") -> dict:
    """构造一条 ContextItem（P08-01）。`item_id` 内容寻址：同输入同 id，可跨轮比对。"""
    clean_kind = str(kind) if str(kind) in CONTEXT_ITEM_KINDS else "evidence"
    clean_section = str(section) if str(section) in CONTEXT_SECTIONS else "evidence_context"
    body = str(text or "")
    ground = dict(grounding) if isinstance(grounding, Mapping) else {"grounded": False}
    ground.setdefault("grounded", False)
    if not ground.get("grounded") and not ground.get("reason"):
        ground["reason"] = "该条目不是证据（没有可回溯 span）"
    # item_id 内容寻址，但**必须带上来源身份**：不同证据可能引用同一句话（同一段引文被
    # 多篇转载就是常态），只用正文做 id 会把它们误合并成一条上下文（实测踩到）。
    identity = item_id or ("C" + _digest("%s|%s|%s|%s|%s" % (
        clean_kind, clean_section, claim_id, evidence_ref, body))[:20])
    return {
        "item_id": identity,
        "kind": clean_kind,
        "section": clean_section,
        "text": body,
        "tokens": estimate_tokens(body),
        "source_stage": str(source_stage or ""),
        "grounding": ground,
        "claim_id": str(claim_id or ""),
        "evidence_ref": str(evidence_ref or ""),
        "metadata": dict(metadata) if isinstance(metadata, Mapping) else {},
    }


def _edge_rows(graph: Mapping) -> list:
    """把图上的 claim-evidence 边归一成 `{claim_id, evidence_ref, relation, ...}`。

    优先读 Phase 06 图层（`graph["evidence_graph"]["edges"]`，带图级大写关系），
    没有就退回 `graph["edges"]`（Phase 06 之前的形状，`relationship` 是小写）。
    """
    rows = []
    containers = []
    layer = graph.get("evidence_graph") if isinstance(graph, Mapping) else None
    if isinstance(layer, Mapping) and isinstance(layer.get("edges"), Sequence):
        containers.append(layer.get("edges"))
    if isinstance(graph, Mapping) and isinstance(graph.get("edges"), Sequence):
        containers.append(graph.get("edges"))
    seen = set()
    for edges in containers:
        for edge in edges or []:
            if not isinstance(edge, Mapping):
                continue
            claim_id = str(edge.get("claim_id") or "")
            ref = str(edge.get("evidence_ref") or "")
            if not claim_id or not ref:
                src, dst = str(edge.get("src") or ""), str(edge.get("dst") or "")
                if not claim_id and src.startswith("claim:"):
                    claim_id = src.split(":", 1)[1]
                if not ref and dst.startswith("evidence:"):
                    ref = dst.split(":", 1)[1]
            if not claim_id or not ref:
                continue
            key = (claim_id, ref)
            if key in seen:
                continue
            seen.add(key)
            relation = str(edge.get("graph_relation") or "").upper()
            if not relation:
                relation = str(edge.get("relationship") or "").upper()
            rows.append({
                "claim_id": claim_id, "evidence_ref": ref, "relation": relation,
                "status": str(edge.get("status") or ""),
                "strength": _clamp(edge.get("strength")),
                "verified": bool((edge.get("metadata") or {}).get("verified")),
                "reasons": [str(item) for item in
                            ((edge.get("metadata") or {}).get("reasons") or [])],
                "source_id": str((edge.get("metadata") or {}).get("source_id") or ""),
            })
    return rows


def build_context_graph(*, graph: Mapping, plan: Mapping | None = None,
                        working_memory: Mapping | None = None,
                        run_id: str = "") -> dict:
    """P08-01：把既有结论组装成 Context Graph（节点 = 候选 ContextItem，边 = 真实关系）。

    **只加不改**：不触碰 `graph` 的任何既有键；输出的图是"候选池"，还没做预算裁剪
    （裁剪在 `select_context_items`，组装在 `build_context_pack`）。
    """
    graph = graph if isinstance(graph, Mapping) else {}
    plan = plan if isinstance(plan, Mapping) else {}
    claims = [node for node in (graph.get("claims") or []) if isinstance(node, Mapping)]
    evidence = [item for item in (graph.get("evidence") or []) if isinstance(item, Mapping)]
    edges = _edge_rows(graph)

    claim_text = {}
    claim_plan_only = {}
    for node in claims:
        payload = node.get("claim") if isinstance(node.get("claim"), Mapping) else node
        cid = str(node.get("canonical_id") or payload.get("claim_id") or "")
        if not cid:
            continue
        claim_text[cid] = str(payload.get("text") or "")
        claim_plan_only[cid] = bool(node.get("plan_only"))

    refutes_by_claim: dict = {}
    supports_by_claim: dict = {}
    relations_by_ref: dict = {}
    for row in edges:
        if row["relation"] == "REFUTES":
            refutes_by_claim.setdefault(row["claim_id"], []).append(row["evidence_ref"])
        elif row["relation"] == "SUPPORTS":
            supports_by_claim.setdefault(row["claim_id"], []).append(row["evidence_ref"])
        relations_by_ref.setdefault(row["evidence_ref"], []).append(row)

    unresolved_refs = {
        str(ref)
        for conflict in (graph.get("conflicts") or [])
        if isinstance(conflict, Mapping) and str(conflict.get("resolution") or "") == "unresolved"
        for ref in (conflict.get("evidence_refs") or [])
    }

    items: list = []
    for node in claims:
        payload = node.get("claim") if isinstance(node.get("claim"), Mapping) else node
        cid = str(node.get("canonical_id") or payload.get("claim_id") or "")
        if not cid:
            continue
        text = str(payload.get("text") or "")
        status = str(node.get("verification_status") or payload.get("verification_status") or "unverified")
        items.append(make_context_item(
            kind="claim", section="evidence_context", text=text, source_stage="evidence_graph",
            claim_id=cid, grounding={"grounded": False, "reason": "claim 本身是结论、不是证据（证据见同 claim 的证据条目）"},
            metadata={"verification_status": status, "plan_only": bool(node.get("plan_only")),
                      "refutes": sorted(set(refutes_by_claim.get(cid) or [])),
                      "supports": sorted(set(supports_by_claim.get(cid) or []))}))

    for item in evidence:
        layer = normalize_evidence_layer(item)
        ref = layer["evidence_ref"] or str(item.get("evidence_ref") or "")
        if not ref:
            continue
        rows = relations_by_ref.get(ref) or []
        relations = {row["relation"] for row in rows}
        verdict = str((layer.get("verification") or {}).get("verdict") or "")
        counter = bool(
            rows and all(row["relation"] == "REFUTES" for row in rows)) or \
            "REFUTES" in relations or "CONTRADICTS" in relations or \
            verdict == "REFUTED" or ref in unresolved_refs or \
            str(item.get("relationship") or "") == "contradicts"
        section = "counter_evidence" if counter else "evidence_context"
        kind = "counter_evidence" if counter else "evidence"
        items.append(make_context_item(
            kind=kind, section=section, text=layer["span"]["quote"],
            source_stage="evidence",
            grounding={"grounded": bool(layer["grounded"]), "evidence_ref": ref,
                       "evidence_id": layer["evidence_id"], "span": layer["span"],
                       "source_url": str(item.get("source_url") or ""),
                       "reason": layer["reason"]},
            evidence_ref=ref,
            claim_id=str(rows[0]["claim_id"]) if rows else "",
            metadata={"title": str(item.get("title") or ""),
                      "published_at": str(item.get("published_at") or ""),
                      "authority_level": item.get("authority_level"),
                      "source_type": str(item.get("source_type") or ""),
                      "verdict": verdict,
                      "verification": dict(layer.get("verification") or {}),
                      # 多样性分组键 = Phase 02 的来源身份（同一篇文章/同一 chunk 算一组）
                      "source_group": str((layer.get("source") or {}).get("source_id")
                                          or layer["evidence_id"]),
                      "relations": sorted(relations),
                      "full_chars": len(str(item.get("content_excerpt") or ""))}))

    # 任务段：问题 + 子问题（Phase 05 的计划，没计划就不造）
    question = str((plan or {}).get("question") or (plan or {}).get("standalone_question") or "")
    if question:
        items.append(make_context_item(kind="task", section="task_context", text=question,
                                       source_stage="plan",
                                       metadata={"role": "question"}))
    decomposition = plan.get("decomposition") if isinstance(plan.get("decomposition"), Mapping) else {}
    for hop in (decomposition.get("hops") or []):
        if not isinstance(hop, Mapping):
            continue
        text = str(hop.get("question") or "").strip()
        if not text:
            continue
        items.append(make_context_item(kind="task", section="task_context", text=text,
                                       source_stage="plan",
                                       metadata={"role": "sub_question", "hop_id": str(hop.get("id") or ""),
                                                 "depends_on": list(hop.get("depends_on") or [])}))
    # 未消解矛盾（§16 的 Unresolved Gaps 一栏）
    for conflict in (graph.get("conflicts") or []):
        if not isinstance(conflict, Mapping):
            continue
        if str(conflict.get("resolution") or "") != "unresolved":
            continue
        text = str(conflict.get("subject") or "")
        if not text:
            continue
        items.append(make_context_item(
            kind="gap", section="counter_evidence", text=text, source_stage="evidence_graph",
            grounding={"grounded": False, "reason": "矛盾条目本身不是证据；其 refs 指向具体证据条目"},
            metadata={"role": "unresolved_conflict", "conflict_id": str(conflict.get("conflict_id") or ""),
                      "claim_ids": list(conflict.get("claim_ids") or []),
                      "evidence_refs": list(conflict.get("evidence_refs") or []),
                      "conflict_type": str(conflict.get("conflict_type") or "")}))

    working = working_memory if isinstance(working_memory, Mapping) else {}
    for key, value in sorted(working.items()):
        text = _working_memory_text(key, value)
        if not text:
            continue
        items.append(make_context_item(kind="working_memory", section="working_memory", text=text,
                                       source_stage="config",
                                       metadata={"role": str(key)}))

    nodes = []
    for item in items:
        spec = dict(item)
        spec["node_id"] = "context:%s" % item["item_id"]
        nodes.append(spec)
    edges_out = []
    for row in edges:
        for item in items:
            if item["kind"] in ("evidence", "counter_evidence") and str(item["evidence_ref"]) == row["evidence_ref"]:
                edges_out.append({
                    "edge_id": "ctx-edge:%s" % _digest("%s|%s|%s" % (item["item_id"], row["claim_id"], row["relation"]), 20),
                    "src": "context:%s" % item["item_id"],
                    "dst": "claim:%s" % row["claim_id"],
                    "relation": row["relation"] if row["relation"] in
                                ("SUPPORTS", "REFUTES", "DERIVED_FROM", "REQUIRES", "CONSTRAINS")
                                else "DERIVED_FROM",
                    "section": item["section"],
                })
    sections: dict = {}
    for item in items:
        bucket = sections.setdefault(item["section"], {"items": [], "tokens": 0})
        bucket["items"].append(item["item_id"])
        bucket["tokens"] += int(item["tokens"])
    return {
        "graph_version": CONTEXT_PACK_VERSION,
        "run_id": str(run_id or ""),
        "nodes": nodes,
        "edges": edges_out,
        "items": items,
        "claims": sorted(claim_text),
        "plan_only_claims": sorted(cid for cid, flag in claim_plan_only.items() if flag),
        "refutes_by_claim": {key: sorted(set(value)) for key, value in sorted(refutes_by_claim.items())},
        "supports_by_claim": {key: sorted(set(value)) for key, value in sorted(supports_by_claim.items())},
        "sections": sections,
        "stats": {
            "items": len(items),
            "evidence_items": len([i for i in items if i["kind"] == "evidence"]),
            "counter_evidence_items": len([i for i in items if i["kind"] == "counter_evidence"]),
            "claim_items": len([i for i in items if i["kind"] == "claim"]),
            "grounded_items": len([i for i in items if i["grounding"].get("grounded")]),
            "ungrounded_items": len([i for i in items if not i["grounding"].get("grounded")]),
            "edges": len(edges_out),
        },
    }


def _working_memory_text(key, value) -> str:
    """working_memory 段的一行文本（只把既有回执读成人话，不新增事实）。"""
    if isinstance(value, Mapping):
        parts = ["%s=%s" % (name, str(item)[:120]) for name, item in sorted(value.items())[:8]]
        return "%s: %s" % (key, "; ".join(parts)) if parts else ""
    if isinstance(value, (list, tuple)):
        return "%s: %s" % (key, ", ".join(str(item)[:60] for item in list(value)[:8])) if value else ""
    text = str(value or "").strip()
    return "%s: %s" % (key, text[:240]) if text else ""


# ── P08-02：ContextUtility（§5）─────────────────────────────────────────────

_NECESSITY_BY_KIND = {
    "system": 1.0, "task": 1.0, "claim": 0.95, "counter_evidence": 0.90,
    "constraint": 0.85, "evidence": 0.80, "gap": 0.75, "working_memory": 0.60,
    "memory": 0.5, "skill": 0.5,
}

_STRENGTH_BY_STATUS = {
    EVIDENCE_STATUS_SUPPORTED: 1.0,
    EVIDENCE_STATUS_REFUTED: 0.85,      # 反证同样是"强信息"，但语义相反
    EVIDENCE_STATUS_QUALIFIED: 0.65,
    EVIDENCE_STATUS_CONTEXT: 0.45,
    EVIDENCE_STATUS_UNVERIFIED: 0.30,
}


def evidence_strength(item: Mapping) -> float:
    """证据强度：Phase 03 有核验分就用分，没有就按证据状态映射（**不新造质量判断**）。"""
    verification = verification_of(item) if isinstance(item, Mapping) else {}
    score = verification.get("score") if isinstance(verification, Mapping) else None
    verdict = str((verification or {}).get("verdict") or "")
    base = _STRENGTH_BY_STATUS.get(verdict)
    if base is None:
        base = _STRENGTH_BY_STATUS.get(evidence_status(item or {}), 0.30)
    if score is None:
        return base
    try:
        number = float(score)
    except (TypeError, ValueError):
        return base
    # 核验分与本模块的量纲不同（§11 EvidenceScore 是加权多项式），按 0.5 混合是**明确的取舍**：
    # 有分时既保留状态语义，又让分的差异体现出来。
    return _clamp(0.5 * base + 0.5 * _clamp(number))


def freshness_factor(published_at, *, now: datetime | None = None,
                     half_life_days: float = DEFAULT_FRESHNESS_HALF_LIFE_DAYS) -> float:
    """时间新鲜度：半衰期 180 天（与 Phase 03 的时间适用性同口径）。

    没有时间戳 → 0.5（**中性值**，不是 0：没有发布时间不等于过期，也不等于新鲜；
    这个取舍写进 D-032，任何"没有时间就当过期"的做法都会把大量政策原文误杀）。
    """
    moment = _date(published_at)
    if moment is None:
        return 0.5
    reference = now or datetime.now(timezone.utc)
    age_days = abs((reference - moment).total_seconds()) / 86400.0
    return _clamp(math.pow(0.5, age_days / max(1.0, float(half_life_days))))


def _identity_of(item: Mapping) -> str:
    """**去重**身份：证据按 Phase 02 的**来源指纹**分组（同一篇文章算一组），
    其余条目按自己的内容寻址 id —— 于是"同一来源被塞两次"会被判重复，
    而同一段落里的不同条目（比如两条工作记忆）不会被互相吃掉。"""
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    if str(item.get("kind")) in ("evidence", "counter_evidence"):
        group = str(metadata.get("source_group") or item.get("evidence_ref") or item.get("item_id") or "")
        return "evidence:" + group
    return "item:" + str(item.get("item_id") or "")


def _diversity_group(item: Mapping) -> str:
    """**多样性**分组：证据按来源，其余按段落 —— 同一段落堆太多条也会被多样性压分。"""
    return _identity_of(item) if str(item.get("kind")) in ("evidence", "counter_evidence") \
        else "section:" + str(item.get("section") or "")


def context_utility(item: Mapping, task: Mapping, *, selected_identities: Iterable = ()) -> dict:
    """§5：`ContextUtility = Relevance × EvidenceStrength × TaskNecessity × Freshness × Diversity / TokenCost`。

    五个乘子全部 ∈ [0,1]，取**加权几何平均**（权重见 `CONTEXT_UTILITY_WEIGHTS`），
    再除以 token 成本（`/TokenCost`，token 为 0 时按 1 计）。为什么几何平均而不是直接相乘：
    直接相乘时任何一个小分量都会把整条压到 0（实测：一条权威但没有发布时间的证据会被
    Freshness 直接清零），几何平均保留"某维度确实没贡献"的语义但不会一击致命。
    返回 `{utility, utility_raw, factors, token_cost, estimator}`，**每项可复算**。
    """
    item = item if isinstance(item, Mapping) else {}
    task = task if isinstance(task, Mapping) else {}
    terms = set(task.get("terms") or ())
    kind = str(item.get("kind") or "")
    text = str(item.get("text") or "")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}

    if kind in ("evidence", "counter_evidence"):
        relevance = _clamp(relevance_score(terms, {
            "title": metadata.get("title") or "",
            "content_excerpt": text,
        }).get("relevance"))
        strength = evidence_strength({
            "metadata": {"evidence_layer": {"verification": metadata.get("verification") or {}}},
            "content_excerpt": text,
        })
        if metadata.get("verdict"):
            strength = max(strength, _STRENGTH_BY_STATUS.get(str(metadata.get("verdict")), 0.0))
    elif kind == "claim":
        overlap = _term_overlap(terms, text)
        relevance = _clamp(0.4 + 0.6 * overlap)
        status = str(metadata.get("verification_status") or "")
        strength = _STRENGTH_BY_STATUS.get(
            {"confirmed": EVIDENCE_STATUS_SUPPORTED, "qualified": EVIDENCE_STATUS_QUALIFIED,
             "conflicted": EVIDENCE_STATUS_REFUTED, "insufficient_evidence": EVIDENCE_STATUS_UNVERIFIED}
            .get(status, ""), 0.45)
    else:
        # 非证据/结论类条目（系统规则 / 约束 / 任务 / 工作记忆）没有"相关性"这个概念，
        # 统一给 0.5 中性值：它们要么是必选段，要么由 task_necessity 决定去留。
        relevance = _clamp(_term_overlap(terms, text) or 0.5)
        strength = 0.9 if kind in ("system", "constraint") else 0.7

    base_necessity = _NECESSITY_BY_KIND.get(kind, 0.5)
    necessity = base_necessity
    if kind in ("claim", "counter_evidence"):
        claim_id = str(item.get("claim_id") or metadata.get("claim_id") or "")
        if claim_id and claim_id not in set(task.get("required_claims") or ()):
            necessity = base_necessity * 0.6
    if kind in ("evidence", "counter_evidence"):
        ref = str(item.get("evidence_ref") or "")
        if ref and ref in set(task.get("primary_refs") or ()):
            necessity = min(1.0, base_necessity + 0.15)

    freshness = freshness_factor(metadata.get("published_at")) if kind in (
        "evidence", "counter_evidence") else 1.0

    selected = set(selected_identities or ())
    identity = _diversity_group(item)
    # 没有任何已选条目时多样性无意义 → 1.0；否则按"同分组重复度"递减。
    # 注意**不能**写 `identity in selected → 1.0`：那会让"同一来源的第二条"拿满分，
    # 多样性就永远不会惩罚同源堆叠（实测踩到，用例钉住）。
    diversity = 1.0 if not selected else _clamp(1.0 - _identity_overlap(identity, selected))

    factors = {
        "relevance": round(relevance, 6),
        "evidence_strength": round(_clamp(strength), 6),
        "task_necessity": round(_clamp(necessity), 6),
        "freshness": round(_clamp(freshness), 6),
        "diversity": round(_clamp(diversity), 6),
    }
    weights = {name: float(CONTEXT_UTILITY_WEIGHTS.get(name, 0.0)) for name in CONTEXT_UTILITY_FACTORS}
    total_weight = sum(weights.values()) or 1.0
    log_sum = 0.0
    for name, weight in weights.items():
        value = max(1e-6, factors[name])
        log_sum += (weight / total_weight) * math.log(value)
    raw = math.exp(log_sum)
    token_cost = max(1, int(item.get("tokens") or estimate_tokens(text)))
    return {
        "utility": round(raw / token_cost, 8),
        "utility_raw": round(raw, 6),
        "factors": factors,
        "token_cost": token_cost,
        "weights": weights,
        "estimator": ESTIMATOR_VERSION,
    }


def _term_overlap(terms: set, text: str) -> float:
    if not terms or not text:
        return 0.0
    hits = sum(1 for term in terms if term and term in text)
    return _clamp(hits / float(len(terms)))


def _identity_overlap(identity: str, selected: Iterable) -> float:
    """多样性：与已选集合的"同组重复度"（0 = 全新分组，越大越冗余）。

    `diversity = 1 / (1 + 0.5 × 同组已选条数)`：
      · 该分组第一次出现 → 1.0；
      · 同一来源第二条 → 0.667；第三条 → 0.5 ……
    只用**分组计数**，不做语义相似度（那需要模型）；所以它衡量的是"来源多样性"
    而不是"观点多样性"——能力边界写进报告（D-032）。
    """
    same = sum(1 for other in selected if str(other) == str(identity))
    if not same:
        return 0.0
    return _clamp(1.0 - 1.0 / (1.0 + 0.5 * same))


# ── P08-02 / P08-04 / P08-06：预算裁剪 + 反证预留 + selection trace ──────────

def _trace_row(*, item: Mapping, decision: str, reason: str, utility: Mapping,
               reserved: bool, budget_after: int, rank: int, detail: str = "") -> dict:
    return {
        "trace_version": CONTEXT_SELECTION_VERSION,
        "item_id": str(item.get("item_id") or ""),
        "kind": str(item.get("kind") or ""),
        "section": str(item.get("section") or ""),
        "decision": decision if decision in CONTEXT_DECISIONS else "excluded",
        "reason": reason if reason in CONTEXT_SELECTION_REASONS else "LOW_UTILITY",
        "utility": float(utility.get("utility") or 0.0),
        "utility_factors": dict(utility.get("factors") or {}),
        "tokens": int(item.get("tokens") or 0),
        "reserved": bool(reserved),
        "budget_after": int(max(0, budget_after)),
        "rank": int(rank),
        "detail": str(detail or ""),
    }


def select_context_items(items: Sequence[Mapping], *, task: Mapping, budget_tokens: int,
                         reserve_ratio: float) -> dict:
    """P08-02 + P08-04 + P08-06：在 token 预算下选出**最大价值集合**，并留全量决策痕迹。

    规则（确定性，同输入同输出）：
      ① 必选段（`task_context` / `system_context` / `constraints` / `budget` 的承载条目）
         先入包，`reason=MANDATORY_SECTION`；它们不参与竞争，也不吃反证预留；
      ② 反证预留（P08-04）：`counter_evidence` 候选先占 `reserve = floor(证据预算 × 比例)`，
         **支持性证据无论效用多高都不能挤掉它**（这是本阶段最容易做错的地方：
         反证通常相关性低、又常来自二手来源，纯按效用排序必然被裁掉）；
      ③ 其余候选按 `utiltiy` 降序填满剩余预算，同分按原始下标稳定排序；
      ④ 预留没被反证填满时，剩余额度**归还**给一般候选（记 `counter_reserve_unfilled`）；
      ⑤ 每一条候选都产出一条 selection trace（included / excluded + 原因码）。

    返回 `{selected, trace, budget, stats}`。
    """
    candidates = [dict(item) for item in (items or []) if isinstance(item, Mapping)]
    # 同一 item_id 只算一个候选（内容寻址 id 相同 = 同一个上下文条目）：
    # 否则同一段引文被多篇转载时会重复占预算、trace 里也会出现两条一样的决策。
    deduped: list = []
    seen_ids: set = set()
    collapsed = 0
    for item in candidates:
        key = str(item.get("item_id") or "")
        if key and key in seen_ids:
            collapsed += 1
            continue
        if key:
            seen_ids.add(key)
        deduped.append(item)
    candidates = deduped
    # 稳定序：内容寻址 id 排序保证"同输入同顺序"，不依赖上游 list 顺序
    candidates.sort(key=lambda item: str(item.get("item_id") or ""))
    candidates = candidates[:DEFAULT_MAX_ITEMS]
    total_budget = max(0, int(budget_tokens))
    reserve = int(math.floor(total_budget * _clamp(reserve_ratio, 0.0, 0.9)))

    utilities: dict = {}
    identity_of = {str(item.get("item_id")): _identity_of(item) for item in candidates}
    mandatory = [item for item in candidates if str(item.get("section")) in
                 ("task_context", "system_context", "constraints")]
    counter = [item for item in candidates if str(item.get("kind")) == "counter_evidence"]
    mandatory_ids = {str(item.get("item_id")) for item in mandatory}
    counter_ids = {str(item.get("item_id")) for item in counter}
    others = [item for item in candidates
              if str(item.get("item_id")) not in mandatory_ids | counter_ids]

    selected: list = []
    trace: list = []
    used = 0
    selected_identities: list = []
    reserve_used = 0
    floor = utility_floor()

    def _score(item, selected_ids):
        value = context_utility(item, task, selected_identities=selected_ids)
        utilities[str(item.get("item_id"))] = value
        return value

    rank = 0
    for item in mandatory:
        value = _score(item, selected_identities)
        tokens = int(item.get("tokens") or 0)
        if tokens < DEFAULT_MIN_ITEM_TOKENS:
            trace.append(_trace_row(item=item, decision="excluded", reason="LOW_UTILITY",
                                    utility=value, reserved=False, budget_after=total_budget - used,
                                    rank=rank, detail="空条目（0 token）"))
            rank += 1
            continue
        if used + tokens > total_budget:
            trace.append(_trace_row(item=item, decision="excluded", reason="OVER_TOKEN_BUDGET",
                                    utility=value, reserved=False, budget_after=total_budget - used,
                                    rank=rank, detail="必选段也装不下（预算过小）"))
            rank += 1
            continue
        selected.append(item)
        selected_identities.append(identity_of[str(item.get("item_id"))])
        used += tokens
        trace.append(_trace_row(item=item, decision="included", reason="MANDATORY_SECTION",
                                utility=value, reserved=False, budget_after=total_budget - used,
                                rank=rank))
        rank += 1

    # 反证预留：先按效用排序，再在预留额度内填
    counter_scored = sorted(
        ((_score(item, selected_identities), index, item) for index, item in enumerate(counter)),
        key=lambda row: (-row[0]["utility"], row[1]))
    counter_overflow: list = []
    unfilled_reason = ""
    for value, index, item in counter_scored:
        tokens = int(item.get("tokens") or 0)
        if not item.get("grounding", {}).get("grounded"):
            # 先判"拿不出 span"再判 token：空正文的 0-token 不是"便宜"，而是**不可引用**
            trace.append(_trace_row(item=item, decision="excluded", reason="NO_GROUNDING_SPAN",
                                    utility=value, reserved=True, budget_after=reserve - reserve_used,
                                    rank=rank, detail=str(item.get("grounding", {}).get("reason") or "")))
            rank += 1
            continue
        if tokens < DEFAULT_MIN_ITEM_TOKENS:
            trace.append(_trace_row(item=item, decision="excluded", reason="LOW_UTILITY",
                                    utility=value, reserved=True, budget_after=reserve - reserve_used,
                                    rank=rank, detail="空条目（0 token）"))
            rank += 1
            continue
        if value["utility"] <= floor:
            trace.append(_trace_row(item=item, decision="excluded", reason="LOW_UTILITY",
                                    utility=value, reserved=True, budget_after=reserve - reserve_used,
                                    rank=rank, detail="效用低于下限"))
            rank += 1
            continue
        if reserve_used + tokens > reserve:
            # 预留额度装不下的反证**回到一般池竞争**（不是被丢掉）——否则多出来的反证
            # 既没进预留、又没机会用剩余预算，等于被静默吞掉。
            counter_overflow.append(item)
            continue
        selected.append(item)
        selected_identities.append(identity_of[str(item.get("item_id"))])
        used += tokens
        reserve_used += tokens
        trace.append(_trace_row(item=item, decision="included", reason="COUNTER_EVIDENCE_RESERVED",
                                utility=value, reserved=True,
                                budget_after=total_budget - used, rank=rank))
        rank += 1
    if counter and reserve_used < reserve:
        unfilled_reason = "reserve_wider_than_supply" if counter_scored else "no_counter_evidence"

    others_scored = sorted(
        ((_score(item, selected_identities), index, item)
         for index, item in enumerate(list(others) + counter_overflow)),
        key=lambda row: (-row[0]["utility"], row[1]))
    for value, index, item in others_scored:
        tokens = int(item.get("tokens") or 0)
        if str(item.get("kind")) in ("evidence", "counter_evidence") and \
                not item.get("grounding", {}).get("grounded"):
            trace.append(_trace_row(item=item, decision="excluded", reason="NO_GROUNDING_SPAN",
                                    utility=value, reserved=False, budget_after=total_budget - used,
                                    rank=rank, detail=str(item.get("grounding", {}).get("reason") or "")))
            rank += 1
            continue
        if tokens < DEFAULT_MIN_ITEM_TOKENS:
            trace.append(_trace_row(item=item, decision="excluded", reason="LOW_UTILITY",
                                    utility=value, reserved=False, budget_after=total_budget - used,
                                    rank=rank, detail="空条目（0 token）"))
            rank += 1
            continue
        if identity_of[str(item.get("item_id"))] in set(selected_identities):
            trace.append(_trace_row(item=item, decision="excluded", reason="DUPLICATE_IDENTITY",
                                    utility=value, reserved=False, budget_after=total_budget - used,
                                    rank=rank))
            rank += 1
            continue
        if value["utility"] <= floor:
            trace.append(_trace_row(item=item, decision="excluded", reason="LOW_UTILITY",
                                    utility=value, reserved=False, budget_after=total_budget - used,
                                    rank=rank, detail="效用低于下限"))
            rank += 1
            continue
        if used + tokens > total_budget:
            trace.append(_trace_row(item=item, decision="excluded", reason="OVER_TOKEN_BUDGET",
                                    utility=value, reserved=False, budget_after=total_budget - used,
                                    rank=rank))
            rank += 1
            continue
        selected.append(item)
        selected_identities.append(identity_of[str(item.get("item_id"))])
        used += tokens
        trace.append(_trace_row(item=item, decision="included",
                                reason="DIVERSITY_BONUS" if value["factors"].get("diversity", 1.0) < 1.0
                                else "TOP_UTILITY",
                                utility=value, reserved=False, budget_after=total_budget - used,
                                rank=rank))
        rank += 1

    selected.sort(key=lambda item: (_SECTION_ORDER.index(str(item.get("section")))
                                    if str(item.get("section")) in _SECTION_ORDER else 99,
                                    -float(utilities.get(str(item.get("item_id")), {}).get("utility") or 0.0),
                                    str(item.get("item_id"))))
    for item in selected:
        value = utilities.get(str(item.get("item_id"))) or context_utility(item, task)
        item["utility"] = float(value.get("utility") or 0.0)
        item["utility_factors"] = dict(value.get("factors") or {})

    included_tokens = sum(int(item.get("tokens") or 0) for item in selected)
    all_tokens = sum(int(item.get("tokens") or 0) for item in candidates)
    excluded = [row for row in trace if str(row.get("decision")) == "excluded"]
    budget = {
        "total": total_budget,
        "used": included_tokens,
        "remaining": max(0, total_budget - included_tokens),
        "candidates_tokens": all_tokens,
        "candidates": len(candidates),
        "included": len(selected),
        "duplicates_collapsed": collapsed,
        "reserved_counter_evidence": reserve,
        "counter_evidence_used": reserve_used,
        "counter_evidence_unfilled": max(0, reserve - reserve_used),
        "counter_evidence_overflow": len(counter_overflow),
        "counter_reserve_unfilled_reason": unfilled_reason if reserve_used < reserve and counter else "",
        "output_reserve": output_reserve_tokens(),
        "estimator": ESTIMATOR_VERSION,
        "trimmed_items": len(excluded),
        "trim_reasons": _counter(row.get("reason") for row in excluded),
    }
    stats = {
        "candidates": len(candidates),
        "included": len(selected),
        "excluded": len(candidates) - len(selected),
        "estimator": ESTIMATOR_VERSION,
    }
    return {"selected": selected, "trace": trace[:DEFAULT_MAX_TRACE], "budget": budget,
            "stats": stats}


# ── P08-03：Context Pack Builder ────────────────────────────────────────────

_SYSTEM_RULES = (
    "只使用本包中的证据作答；包内没有的内容一律视为无证据。",
    "引用只能使用 citation_index 里的 [n]，并且要能指回 evidence_ref 与 span。",
    "没有证据支持的句子必须显式标注" + UNGROUNDED_MARK + "，不得写成已确证事实。",
    "结论与反证都要展示；未消解的矛盾必须显式说明。",
)


def _citation_identity(item: Mapping) -> str:
    """引用的"同一份来源"身份（与 `qa_synthesis._citation_identity` 同口径，有守门用例钉等值）。"""
    metadata = _metadata(item)
    doc_type = str(item.get("doc_type") or metadata.get("doc_type") or "")
    source_role = str(item.get("source_role") or metadata.get("source_role") or "")
    flags = {str(flag) for flag in metadata.get("authority_flags") or []}
    url = str(item.get("source_url") or metadata.get("source_url") or "")
    domain = str(metadata.get("domain") or metadata.get("source_domain") or "")
    official = (doc_type == "official_policy" or source_role == "official_original"
                or "official_original" in flags or domain.endswith(".gov.cn")
                or ".mof.gov.cn" in url or "chinatax.gov.cn" in url)
    if official:
        doc_no = re.sub(r"\s+", "", str(item.get("doc_no") or metadata.get("doc_no") or "").casefold())
        title = re.sub(r"\s+", "", str(item.get("title") or metadata.get("title")
                                       or metadata.get("policy_title") or "").casefold())
        issuer = re.sub(r"\s+", "", str(item.get("issuer") or metadata.get("issuer") or "").casefold())
        if doc_no or title:
            return "official:%s:%s:%s" % (issuer, doc_no, title)
    if url:
        return "url:" + url.strip().casefold()
    document_id = str(item.get("document_id") or metadata.get("document_id") or "").strip().casefold()
    if document_id:
        return "doc:" + document_id
    title = re.sub(r"\s+", "", str(item.get("title") or metadata.get("policy_title") or "").casefold())
    return "title:" + title if title else ""


def _source_tier(item: Mapping) -> int:
    metadata = _metadata(item)
    doc_type = str(item.get("doc_type") or metadata.get("doc_type") or "")
    source_role = str(item.get("source_role") or metadata.get("source_role") or "")
    flags = {str(flag) for flag in metadata.get("authority_flags") or []}
    if doc_type == "official_policy" or source_role == "official_original" or "official_original" in flags:
        return 40
    if doc_type == "official_interpretation" or source_role == "official_interpretation":
        return 30
    if doc_type == "professional_commentary":
        return 20
    return 10


def citation_labels(evidence: Sequence[Mapping]) -> dict:
    """`[n] → evidence_ref` 的确定性编号（与 `qa_synthesis._citation_map` **同口径**）。

    排序键：来源级别降序 → 权威度降序 → ragflow_chunk 靠后 → 发布时间降序 → 原始下标。
    同一份来源只给一个编号（后面的同源条目复用第一个编号）；没有 `evidence_ref` 的条目跳过。
    本函数与 `qa_synthesis._citation_map` 的等值由 `tests/test_qa_phase08_grounding.py`
    钉死——两处一旦分叉（生成端编号 ≠ 包内编号），引用就会指错人。
    """
    indexed = list(enumerate(evidence or []))
    ordered = sorted(indexed, key=lambda pair: (
        -_source_tier(pair[1]),
        -_int_or_zero(pair[1].get("authority_level")),
        0 if str(pair[1].get("source_type") or "") != "ragflow_chunk" else 1,
        str(pair[1].get("published_at") or _metadata(pair[1]).get("publish_date") or ""),
        pair[0],
    ))
    labels: dict = {}
    seen: set = set()
    for _, item in ordered:
        if not isinstance(item, Mapping):
            continue
        ref = str(item.get("evidence_ref") or "")
        if not ref:
            continue
        identity = _citation_identity(item)
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        labels["[%d]" % (len(labels) + 1)] = ref
    return labels


def _int_or_zero(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def build_prompt_blocks(pack: Mapping) -> dict:
    """把 Context Pack 压成生成端看得懂的块（§4：Answer Composer 只该看到这些）。

    三件事必须进去，否则"生成端约束"就是空话：
      ① `引用索引`：`[n] → {evidence_ref, span.quote, 可回溯}`；
      ② `无证据内容`：**明确列出**哪些结论/条目没有可回溯证据（MASTER_RULES 第 11 条）；
      ③ `约束`：只能引用索引内的 [n]、无证据必须标注、反证必须展示。
    """
    pack = pack if isinstance(pack, Mapping) else {}
    sections = pack.get("sections") if isinstance(pack.get("sections"), Mapping) else {}
    citation_index = pack.get("citation_index") if isinstance(pack.get("citation_index"), Mapping) else {}
    ungrounded = [
        {"item_id": item.get("item_id"), "kind": item.get("kind"),
         "text": str(item.get("text") or "")[:160],
         "reason": str((item.get("grounding") or {}).get("reason") or "")[:160]}
        for item in (pack.get("items") or []) if not (item.get("grounding") or {}).get("grounded")
    ]
    index_block = {}
    for label, entry in sorted(citation_index.items(), key=lambda pair: _label_order(pair[0])):
        entry = entry if isinstance(entry, Mapping) else {}
        index_block[label] = {
            "evidence_ref": entry.get("evidence_ref"),
            "可回溯": bool(entry.get("grounded")),
            "片段": str(entry.get("quote") or "")[:240],
            "来源": str(entry.get("source_url") or "")[:200],
        }
    gaps = [
        {"类型": gap.get("context_gap_type"), "动作": gap.get("action"),
         "是否触发检索": bool(gap.get("requires_retrieval")),
         "说明": str(gap.get("detail") or "")[:160]}
        for gap in (pack.get("context_gaps") or [])[:12]
    ]
    return {
        "上下文包版本": pack.get("pack_version"),
        "任务": dict(pack.get("task") or {}),
        "段落": {name: (sections.get(name) or {}).get("items", []) for name in sections},
        "引用索引": index_block,
        "无证据内容": ungrounded[:20],
        "反证": [
            {"item_id": item.get("item_id"), "evidence_ref": item.get("evidence_ref"),
             "片段": str(item.get("text") or "")[:200],
             "claim_id": item.get("claim_id")}
            for item in (pack.get("items") or []) if str(item.get("kind")) == "counter_evidence"
        ][:12],
        "上下文缺口": gaps,
        "约束": list(_SYSTEM_RULES),
        "预算": dict(pack.get("budget") or {}),
    }


def _label_order(label) -> int:
    match = re.match(r"^\[(\d+)\]$", str(label or ""))
    return int(match.group(1)) if match else 10 ** 6


def build_context_pack(*, graph: Mapping, plan: Mapping | None = None, request: Mapping | None = None,
                       working_memory: Mapping | None = None, run_id: str = "",
                       budget_tokens: int | None = None,
                       reserve_ratio: float | None = None) -> dict:
    """P08-03：组装 Context Pack（九段 + 引用索引 + 预算 + Context Gap + selection trace）。

    **不改任何既有结构**：只读 `graph` / `plan` / `request`，返回一个新字典；
    组装失败一律由调用方兜底（本函数自身不吞异常，方便用例钉住失败路径）。
    """
    request = request if isinstance(request, Mapping) else {}
    plan = plan if isinstance(plan, Mapping) else {}
    context_graph = build_context_graph(graph=graph, plan=plan, working_memory=working_memory,
                                        run_id=run_id)
    items = context_graph["items"]

    question = str(request.get("question") or plan.get("question") or plan.get("standalone_question") or "")
    terms = set(term_set(question)) if question else set()
    required_claims = sorted(
        node for node in {str(item.get("claim_id")) for item in items if item.get("kind") == "claim"}
        if node and node not in set(context_graph.get("plan_only_claims") or ()))
    primary_refs = sorted({ref for claim in required_claims
                           for ref in (context_graph["supports_by_claim"].get(claim) or [])})
    task = {
        "question": question[:600],
        "terms": sorted(terms)[:80],
        "required_claims": required_claims[:40],
        "primary_refs": primary_refs[:60],
        "sub_questions": len([item for item in items
                              if (item.get("metadata") or {}).get("role") == "sub_question"]),
        "answer_form": str((plan.get("question_plan") or {}).get("output_form")
                           or request.get("output_form") or ""),
        "mode": str(request.get("mode") or ""),
    }

    system_items = [make_context_item(kind="system", section="system_context", text=rule,
                                      source_stage="config") for rule in _SYSTEM_RULES]
    constraints = [
        make_context_item(kind="constraint", section="constraints",
                          text="token 预算 %d（估算器 %s），其中输出/工具预算 %d"
                               % (int(budget_tokens or total_token_budget()), ESTIMATOR_VERSION,
                                  output_reserve_tokens()),
                          source_stage="config"),
        make_context_item(kind="constraint", section="constraints",
                          text="反证预留比例 %.2f（支持性证据不得挤占）" % (reserve_ratio
                                                                        if reserve_ratio is not None
                                                                        else counter_reserve_ratio()),
                          source_stage="config"),
    ]
    for name, value in sorted((request.get("session_constraints") or {}).items()):
        text = _working_memory_text(name, value)
        if text:
            constraints.append(make_context_item(kind="constraint", section="constraints",
                                                 text=text, source_stage="request"))
    candidates = list(items) + system_items + constraints

    selection = select_context_items(
        candidates, task=task,
        budget_tokens=int(budget_tokens if budget_tokens is not None else total_token_budget()),
        reserve_ratio=float(reserve_ratio if reserve_ratio is not None else counter_reserve_ratio()))
    selected = selection["selected"]
    budget = dict(selection["budget"])

    sections: dict = {}
    for name in CONTEXT_SECTIONS:
        rows = [item for item in selected if str(item.get("section")) == name]
        sections[name] = {
            "items": [item["item_id"] for item in rows],
            "tokens": sum(int(item.get("tokens") or 0) for item in rows),
            "count": len(rows),
        }
    if not sections["memory_context"]["count"]:
        sections["memory_context"]["deferred_to"] = "Phase 09（Memory Graph Core）"
        sections["memory_context"]["note"] = "本阶段不实现 Memory：空段而不是编内容"
    if not sections["skill_context"]["count"]:
        sections["skill_context"]["deferred_to"] = "Phase 11（Skill Registry & Router）"
        sections["skill_context"]["note"] = "本阶段不实现 Skill 注册表：空段"

    # `budget` 段是**预算回执本身**（§4 的九段之一）：它是裁剪之后才算出来的，
    # 所以不参与竞争、也不占预算——只把"花了多少、裁了多少、为什么"写进包里给生成端看。
    budget_item = make_context_item(
        kind="constraint", section="budget",
        text=("token 预算 %d（估算器 %s）：裁剪前 %d → 裁剪后 %d，被裁 %d 条，"
              "反证预留 %d 用了 %d" % (
                  int(budget.get("total") or 0), ESTIMATOR_VERSION,
                  int(budget.get("candidates_tokens") or 0), int(budget.get("used") or 0),
                  int(budget.get("candidates") or 0) - int(budget.get("included") or 0),
                  int(budget.get("reserved_counter_evidence") or 0),
                  int(budget.get("counter_evidence_used") or 0))),
        source_stage="config", metadata={"role": "budget_receipt"})
    budget_item["utility"] = 0.0
    budget_item["utility_factors"] = {}
    selected = list(selected) + [budget_item]
    sections["budget"] = {
        "items": [budget_item["item_id"]], "tokens": 0, "count": 1,
        "note": "预算回执条目不占预算（它是裁剪结果本身）",
    }

    evidence_selected = [item for item in selected
                         if str(item.get("kind")) in ("evidence", "counter_evidence")
                         and item.get("grounding", {}).get("grounded")]
    pack_evidence = []
    for item in evidence_selected:
        pack_evidence.append({
            "evidence_ref": item["evidence_ref"],
            "title": str((item.get("metadata") or {}).get("title") or ""),
            "source_url": str(item["grounding"].get("source_url") or ""),
            "published_at": str((item.get("metadata") or {}).get("published_at") or ""),
            "authority_level": (item.get("metadata") or {}).get("authority_level"),
            "content_excerpt": item["text"],
        })
    citation_map = citation_labels(pack_evidence)
    index: dict = {}
    for label, ref in citation_map.items():
        item = next((row for row in evidence_selected if row["evidence_ref"] == ref), None)
        index[label] = {
            "evidence_ref": ref,
            "evidence_id": str((item or {}).get("grounding", {}).get("evidence_id") or ""),
            "span": dict((item or {}).get("grounding", {}).get("span") or {}),
            "quote": str((item or {}).get("text") or ""),
            "source_url": str((item or {}).get("grounding", {}).get("source_url") or ""),
            "grounded": bool((item or {}).get("grounding", {}).get("grounded")),
            "section": str((item or {}).get("section") or ""),
        }
    grounding = {
        "grounding_version": GROUNDING_VERSION,
        "grounded_items": len([item for item in selected if item.get("grounding", {}).get("grounded")]),
        "ungrounded_items": len([item for item in selected if not item.get("grounding", {}).get("grounded")]),
        "traceable_citations": len([entry for entry in index.values() if entry.get("grounded")]),
        "untraceable_citations": len([entry for entry in index.values() if not entry.get("grounded")]),
    }
    budget.update({
        "estimated_tokens_before": int(budget.get("candidates_tokens") or 0),
        "estimated_tokens_after": int(budget.get("used") or 0),
        "trimmed_items": int(budget.get("candidates") or 0) - int(budget.get("included") or 0),
        "trim_reasons": dict(budget.get("trim_reasons") or {}),
        "estimator_note": ESTIMATOR_NOTE,
    })

    pack = {
        "pack_version": CONTEXT_PACK_VERSION,
        "pack_id": "CP" + _digest("%s|%s|%s" % (run_id, CONTEXT_PACK_VERSION,
                                                "|".join(item["item_id"] for item in selected)))[:20],
        "built_at": now_utc(),
        "run_id": str(run_id or ""),
        "task": task,
        "sections": sections,
        "items": selected,
        "citation_map": citation_map,
        "citation_index": index,
        "grounding": grounding,
        "budget": budget,
        "utility_version": CONTEXT_UTILITY_VERSION,
        "stats": dict(selection["stats"]),
    }
    pack["context_gaps"] = detect_context_gaps(pack=pack, graph=graph, task=task,
                                               candidates=candidates, trace=selection["trace"])
    pack["selection_trace"] = selection["trace"]
    pack["stats"].update({
        "context_gaps": len(pack["context_gaps"]),
        "retrieval_requested": len([gap for gap in pack["context_gaps"]
                                    if gap.get("requires_retrieval")]),
        "sections_filled": len([name for name in CONTEXT_SECTIONS
                                if sections[name]["count"] and name not in
                                ("memory_context", "skill_context")]),
    })
    return pack


# ── P08-05：Context Gap（§6，**默认不得触发新检索**）────────────────────────

def _gap_id(*parts) -> str:
    return "CG" + _digest("|".join(str(part) for part in parts), 20)


def detect_context_gaps(*, pack: Mapping, graph: Mapping, task: Mapping | None = None,
                        candidates: Sequence[Mapping] = (), trace: Sequence[Mapping] = ()) -> list:
    """P08-05：检测"证据已存在但没进包 / span 不足 / 缺反证"三类 Context Gap（§6）。

    硬规则（MASTER_RULES 第 13 条 + §6 原话）：**每一条 `requires_retrieval` 恒为 False**，
    动作只允许在冻结的四个 Context Gap 动作里取值。Context Gap 默认不触发昂贵新检索。
    """
    pack = pack if isinstance(pack, Mapping) else {}
    graph = graph if isinstance(graph, Mapping) else {}
    task = task if isinstance(task, Mapping) else {}
    included_refs = {str(item.get("evidence_ref")) for item in (pack.get("items") or [])}
    included_claims = {str(item.get("claim_id")) for item in (pack.get("items") or [])
                       if str(item.get("kind")) == "claim"}
    excluded = [row for row in (trace or []) if str(row.get("decision")) == "excluded"]
    gaps: list = []

    # ① OMITTED_EVIDENCE：图上属于"已进包 claim"的证据因预算/去重被裁掉 → 先重组上下文
    by_item_id = {str(item.get("item_id")): item for item in (candidates or [])
                  if isinstance(item, Mapping)}
    for row in excluded:
        item = by_item_id.get(str(row.get("item_id")))
        if item is None:
            continue
        claim_id = str(item.get("claim_id") or "")
        if not claim_id or claim_id not in included_claims:
            continue
        if str(row.get("reason")) not in ("OVER_TOKEN_BUDGET", "DUPLICATE_IDENTITY"):
            continue
        gaps.append({
            "gap_id": _gap_id("OMITTED", row.get("item_id"), claim_id),
            "context_gap_type": "OMITTED_EVIDENCE",
            "claim_id": claim_id,
            "evidence_ref": str(row.get("item_id") or ""),
            "section": "evidence_context",
            "action": "REPACK_CONTEXT",
            "requires_retrieval": False,
            "detail": "证据在图上、claim 也在包里，只是这条证据被 %s 裁掉：优先重组上下文而不是再检索"
                      % row.get("reason"),
            "tokens_recoverable": int(row.get("tokens") or 0),
        })
        if len(gaps) >= DEFAULT_MAX_GAPS:
            break

    # ② TRUNCATED_SPAN：进了包但 span 太短（摘要不足）→ 扩 span（不换检索）
    for item in (pack.get("items") or []):
        if str(item.get("kind")) not in ("evidence", "counter_evidence"):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        full_chars = int(metadata.get("full_chars") or 0)
        span_chars = int((item.get("grounding") or {}).get("span", {}).get("chars") or 0)
        if full_chars <= span_chars or span_chars >= span_min_chars():
            continue
        gaps.append({
            "gap_id": _gap_id("SPAN", item.get("item_id")),
            "context_gap_type": "TRUNCATED_SPAN",
            "claim_id": str(item.get("claim_id") or ""),
            "evidence_ref": str(item.get("evidence_ref") or ""),
            "section": str(item.get("section") or ""),
            "action": "EXPAND_EVIDENCE_SPAN",
            "requires_retrieval": False,
            "detail": "span 只有 %d 字（阈值 %d），原文 %d 字：只扩 span，不重新检索"
                      % (span_chars, span_min_chars(), full_chars),
            "tokens_recoverable": 0,
        })
        if len(gaps) >= DEFAULT_MAX_GAPS:
            break

    # ③ MISSING_COUNTEREVIDENCE：结论的反证在图上但没进包 → 载入反证
    by_ref = {str(item.get("evidence_ref")): item for item in (candidates or [])
              if isinstance(item, Mapping) and item.get("evidence_ref")}
    refutes_by_claim = graph.get("evidence_graph", {}).get("refutes_by_claim") if isinstance(
        graph.get("evidence_graph"), Mapping) else None
    known_refutes: dict = {}
    for row in _edge_rows(graph):
        if row["relation"] == "REFUTES":
            known_refutes.setdefault(row["claim_id"], set()).add(row["evidence_ref"])
    if isinstance(refutes_by_claim, Mapping):
        for claim, refs in refutes_by_claim.items():
            known_refutes.setdefault(str(claim), set()).update(str(ref) for ref in refs or [])
    for claim_id, refs in sorted(known_refutes.items()):
        if claim_id not in included_claims:
            continue
        missing = sorted(ref for ref in refs if ref not in included_refs)
        if not missing:
            continue
        gaps.append({
            "gap_id": _gap_id("COUNTER", claim_id, ",".join(missing)),
            "context_gap_type": "MISSING_COUNTEREVIDENCE",
            "claim_id": claim_id,
            "evidence_ref": missing[0],
            "section": "counter_evidence",
            "action": "LOAD_COUNTEREVIDENCE",
            "requires_retrieval": False,
            "detail": "该结论有 %d 条反证没进包（%s）：从已有结果里装入，不新检索"
                      % (len(missing), ", ".join(missing[:3])),
            "tokens_recoverable": sum(int(by_ref.get(ref, {}).get("tokens") or 0) for ref in missing),
        })
        if len(gaps) >= DEFAULT_MAX_GAPS:
            break

    # ④ UNGROUNDED_CLAIM：进包的结论一条可回溯证据都没有 → 重组上下文
    grounded_claims = {str(item.get("claim_id")) for item in (pack.get("items") or [])
                       if str(item.get("kind")) in ("evidence", "counter_evidence")
                       and item.get("grounding", {}).get("grounded")}
    for item in (pack.get("items") or []):
        if str(item.get("kind")) != "claim":
            continue
        claim_id = str(item.get("claim_id") or "")
        if not claim_id or claim_id in grounded_claims:
            continue
        gaps.append({
            "gap_id": _gap_id("UNGROUNDED", claim_id),
            "context_gap_type": "UNGROUNDED_CLAIM",
            "claim_id": claim_id,
            "evidence_ref": "",
            "section": "evidence_context",
            "action": "REPACK_CONTEXT",
            "requires_retrieval": False,
            "detail": "结论进了包但没有可回溯到 span 的证据：要么重组上下文，要么标注为无证据",
            "tokens_recoverable": 0,
        })
        if len(gaps) >= DEFAULT_MAX_GAPS:
            break

    # ⑤ DUPLICATE_SECTION：同一份来源在多个段落重复占预算 → 重组上下文
    identity_sections: dict = {}
    for item in (pack.get("items") or []):
        if str(item.get("kind")) not in ("evidence", "counter_evidence"):
            continue
        identity = "evidence:" + str(item.get("evidence_ref") or "")
        identity_sections.setdefault(identity, set()).add(str(item.get("section") or ""))
    for identity, names in sorted(identity_sections.items()):
        if len(names) < 2:
            continue
        gaps.append({
            "gap_id": _gap_id("DUP", identity, ",".join(sorted(names))),
            "context_gap_type": "DUPLICATE_SECTION",
            "claim_id": "",
            "evidence_ref": identity.split(":", 1)[1],
            "section": sorted(names)[0],
            "action": "REPACK_CONTEXT",
            "requires_retrieval": False,
            "detail": "同一份来源同时占了 %s 两段预算：重组上下文可以省下重复 token"
                      % "、".join(sorted(names)),
            "tokens_recoverable": 0,
        })
        if len(gaps) >= DEFAULT_MAX_GAPS:
            break

    # ⑥ LOAD_SKILL：§8 的能力（contradiction_resolution / citation_verification）属 Phase 11，
    #     本阶段 skill_context 是空段 —— 需要时只**标注**这个动作，绝不假装已加载。
    need_skill = ""
    if any(str(item.get("kind")) == "counter_evidence" for item in (pack.get("items") or [])):
        need_skill = "contradiction_resolution"
    elif len([item for item in (pack.get("items") or []) if str(item.get("kind")) == "claim"]) >= 3:
        need_skill = "citation_verification"
    if need_skill:
        gaps.append({
            "gap_id": _gap_id("SKILL", need_skill, pack.get("pack_id")),
            "context_gap_type": "SKILL_NOT_AVAILABLE",
            "claim_id": "",
            "evidence_ref": "",
            "section": "skill_context",
            "action": "LOAD_SKILL",
            "requires_retrieval": False,
            "detail": "本任务需要 %s 能力（§8），skill_context 由 Phase 11 提供、本阶段为空段"
                      % need_skill,
            "tokens_recoverable": 0,
        })

    for gap in gaps:
        gap["requires_retrieval"] = False   # 硬规则：Context Gap 不许触发新检索
    return gaps[:DEFAULT_MAX_GAPS]


# ── 生成端 grounding：校验（不改冻结 schema）+ 拦截 ────────────────────────

_LABEL_RE = re.compile(r"\[(\d{1,3})\]")
_NUMBER_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?")
_FULLWIDTH = str.maketrans("０１２３４５６７８９，．", "0123456789,.")


def _numbers(text: str) -> set:
    clean = _LABEL_RE.sub(" ", str(text or "")).translate(_FULLWIDTH)
    out = set()
    for match in _NUMBER_RE.finditer(clean):
        out.add(match.group(0).replace(",", ""))
    return out


def _claim_rows(payload: Mapping) -> list:
    rows = []
    for index, claim in enumerate(payload.get("claims") or []):
        if isinstance(claim, Mapping):
            rows.append(claim)
        elif isinstance(claim, str):
            rows.append({"claim_id": str(claim), "text": "", "evidence_refs": []})
    return rows


def check_grounding(answer: Mapping, *, pack: Mapping | None = None,
                    extra_evidence: Sequence[Mapping] | None = None) -> dict:
    """生成端 grounding 校验（**只校验，不改冻结 schema**）。

    七条规则（`CONTEXT_GROUNDING_VIOLATIONS`）：
      1. `CLAIM_WITHOUT_EVIDENCE`（阻断）：答案里的 claim 没有任何 evidence_ref；
      2. `CLAIM_WITH_UNVERIFIED_EVIDENCE`（告警）：claim 引用的证据在包内没有已验证的支持；
      3. `CITATION_NOT_IN_MAP`（阻断）：正文用了不在引用索引里的 [n]、或 claim 引用了包外证据；
      4. `CITATION_WITHOUT_SPAN`（阻断）：claim 引用的证据在包里是不可回溯的（没有 span）；
      5. `NUMBER_WITHOUT_SPAN`（告警）：正文里的数字在被引用的 span 里找不到；
      6. `COUNTER_EVIDENCE_OMITTED`（告警）：包里该 claim 有反证，答案一条都没引；
      7. `UNMARKED_UNGROUNDED_TEXT`（告警）：正文出现"包内没有证据支持"的断言却没有
         `【无证据】` 标注。
    `extra_evidence` 是"包外但本轮合法可见"的证据（例如官方原文优先过滤路径产生的条目），
    它们同样按 Phase 02 的最小 span 参与校验，避免把合法引用误判成引用错误。
    返回 `GROUNDING_REPORT_SCHEMA` 形态的回执；`blocking=True` 表示必须走修复/标注。
    """
    answer = answer if isinstance(answer, Mapping) else {}
    pack = pack if isinstance(pack, Mapping) else {}
    index = pack.get("citation_index") if isinstance(pack.get("citation_index"), Mapping) else {}
    citation_map = pack.get("citation_map") if isinstance(pack.get("citation_map"), Mapping) else {}
    known_labels = set(index) | set(citation_map)
    extra_index = {}
    for item in (extra_evidence or []):
        if not isinstance(item, Mapping):
            continue
        layer = item if item.get("span") is not None and item.get("evidence_ref") else \
            normalize_evidence_layer(item)
        ref = str(layer.get("evidence_ref") or "")
        if not ref:
            continue
        verification = layer.get("verification") if isinstance(layer.get("verification"), Mapping) else {}
        extra_index["extra:%s" % ref] = {
            "evidence_ref": ref, "quote": str((layer.get("span") or {}).get("quote") or ""),
            "grounded": bool(layer.get("grounded")), "evidence_id": str(layer.get("evidence_id") or ""),
            "section": "extra", "verdict": str(verification.get("verdict") or ""),
        }
    merged = dict(extra_index)
    merged.update(index)
    known_refs = {str(entry.get("evidence_ref")) for entry in merged.values()
                  if isinstance(entry, Mapping)}
    grounded_refs = {str(entry.get("evidence_ref")) for entry in merged.values()
                     if isinstance(entry, Mapping) and entry.get("grounded")}
    verified_refs = {str(item.get("evidence_ref")) for item in (pack.get("items") or [])
                     if str(item.get("kind")) in ("evidence", "counter_evidence")
                     and str((item.get("metadata") or {}).get("verdict") or "") == EVIDENCE_STATUS_SUPPORTED}
    for entry in extra_index.values():
        # 包外证据同样要**按核验结论**计入：不能因为"在包里没有"就默认它已验证
        if entry.get("grounded") and entry.get("verdict") == EVIDENCE_STATUS_SUPPORTED:
            verified_refs.add(str(entry.get("evidence_ref")))
    counter_by_claim: dict = {}
    for item in (pack.get("items") or []):
        if str(item.get("kind")) == "counter_evidence":
            counter_by_claim.setdefault(str(item.get("claim_id") or ""), set()).add(
                str(item.get("evidence_ref") or ""))

    text = str(answer.get("answer") or "")
    sections_text = " ".join(str(value) for value in (answer.get("sections") or {}).values()) \
        if isinstance(answer.get("sections"), Mapping) else ""
    body = text + " " + sections_text
    violations: list = []
    claims = _claim_rows(answer)
    grounded_claims = 0

    labels_used = set(_LABEL_RE.findall(body))
    for label in sorted(labels_used, key=int):
        if ("[%s]" % label) not in known_labels:
            violations.append({
                "code": "CITATION_NOT_IN_MAP", "detail": "[%s] 不在上下文包的引用索引里" % label,
                "label": label,
            })

    cited_refs: set = set()
    for claim in claims:
        claim_id = str(claim.get("claim_id") or "")
        refs = [str(ref) for ref in claim.get("evidence_refs") or []]
        cited_refs.update(refs)
        if not refs:
            violations.append({
                "code": "CLAIM_WITHOUT_EVIDENCE", "claim_id": claim_id,
                "detail": "claim %s 没有绑定任何 evidence_ref" % claim_id,
                "text": str(claim.get("text") or "")[:120],
            })
            continue
        traceable = [ref for ref in refs if ref in grounded_refs]
        if refs and not traceable:
            violations.append({
                "code": "CITATION_WITHOUT_SPAN", "claim_id": claim_id,
                "detail": "claim %s 引用的证据在包里都不可回溯（没有 span）" % claim_id,
                "refs": refs[:4],
            })
            continue
        if not any(ref in verified_refs for ref in refs):
            violations.append({
                "code": "CLAIM_WITH_UNVERIFIED_EVIDENCE", "claim_id": claim_id,
                "detail": "claim %s 引用的证据没有已验证支持（不得当作已确证事实）" % claim_id,
                "refs": refs[:4],
            })
        else:
            grounded_claims += 1
        missing_counter = counter_by_claim.get(claim_id, set()) - cited_refs
        if missing_counter:
            violations.append({
                "code": "COUNTER_EVIDENCE_OMITTED", "claim_id": claim_id,
                "detail": "claim %s 的反证没被引用：%s" % (claim_id, ", ".join(sorted(missing_counter)[:3])),
                "refs": sorted(missing_counter)[:4],
            })

    span_text = " ".join(str(entry.get("quote") or "") for entry in merged.values()
                         if isinstance(entry, Mapping))
    for number in sorted(_numbers(body) - _numbers(span_text)):
        violations.append({
            "code": "NUMBER_WITHOUT_SPAN", "detail": "正文数字 %s 在任何被引用的 span 里都找不到" % number,
            "number": number,
        })

    unknown_refs = sorted(ref for ref in cited_refs if ref and ref not in known_refs)
    for ref in unknown_refs:
        violations.append({
            "code": "CITATION_NOT_IN_MAP", "detail": "claim 引用了不在上下文包里的证据 %s" % ref,
            "evidence_ref": ref,
        })

    if UNGROUNDED_MARK not in body and str(answer.get("status") or "") == "ready":
        unmarked = [item for item in (pack.get("items") or [])
                    if str(item.get("kind")) == "claim"
                    and not (item.get("grounding") or {}).get("grounded")
                    and str(item.get("text") or "")[:24] in body]
        if unmarked:
            violations.append({
                "code": "UNMARKED_UNGROUNDED_TEXT",
                "detail": "有 %d 条包内结论没有可回溯证据、正文也没标注 %s"
                          % (len(unmarked), UNGROUNDED_MARK),
                "claim_ids": [str(item.get("claim_id") or "") for item in unmarked[:6]],
            })

    counts = _counter(item.get("code") for item in violations)
    blocking_codes = sorted({str(item.get("code")) for item in violations
                             if str(item.get("code")) in GROUNDING_BLOCKING_VIOLATIONS})
    report = {
        "grounding_version": GROUNDING_VERSION,
        "checked_claims": len(claims),
        "grounded_claims": grounded_claims,
        "ungrounded_claims": len(claims) - grounded_claims,
        "violations": violations[:200],
        "violation_counts": counts,
        "blocking": bool(blocking_codes),
        "blocking_codes": blocking_codes,
        "warning_codes": sorted({str(item.get("code")) for item in violations
                                 if str(item.get("code")) in GROUNDING_WARNING_VIOLATIONS}),
        "known_codes": list(CONTEXT_GROUNDING_VIOLATIONS),
        "checked_labels": len(labels_used),
        "known_labels": len(known_labels),
        "note": ("本回执只做校验：FINAL_ANSWER_SCHEMA 冻结，一个字都不加；"
                 "阻断级违规必须走修复重试或显式标注，告警级违规必须写进 degradation_reasons。"),
    }
    ok, _note = validate_contract("grounding_report", report)
    report["schema_ok"] = bool(ok)
    return report


def mark_ungrounded_answer(answer: Mapping, report: Mapping, *, pack: Mapping | None = None) -> dict:
    """把 grounding 回执**落到答案上**（拦截动作，不改 schema）：

      · 阻断级违规 → `status` 从 `ready` 降为 `partial`；
      · 有"无证据 claim" → 正文追加 `【无证据】` 标注块（MASTER_RULES 第 11 条：不许伪装成有据）；
      · 一律把违规摘要追加进 `degradation_reasons`（**既有可选字段**，不新增键）。
    返回新的答案字典（原对象不动）。
    """
    result = dict(answer) if isinstance(answer, Mapping) else {}
    report = report if isinstance(report, Mapping) else {}
    violations = [item for item in (report.get("violations") or []) if isinstance(item, Mapping)]
    if not violations:
        return result
    counts = dict(report.get("violation_counts") or {})
    reason = "生成端 grounding 校验：%s" % "，".join(
        "%s×%d" % (code, number) for code, number in sorted(counts.items())[:6])
    reasons = list(result.get("degradation_reasons") or [])
    if reason not in reasons and len(reasons) < 20:
        reasons.append(reason[:500])
    result["degradation_reasons"] = reasons
    if report.get("blocking"):
        result["degraded"] = True
        if str(result.get("status") or "") == "ready":
            result["status"] = "partial"
    ungrounded = [item for item in violations if str(item.get("code")) == "CLAIM_WITHOUT_EVIDENCE"]
    if ungrounded:
        lines = [UNGROUNDED_MARK + "以下内容没有可回溯证据，不得视为已确证事实："]
        for item in ungrounded[:10]:
            label = str(item.get("claim_id") or "").strip() or "未命名结论"
            snippet = str(item.get("text") or "").strip()
            lines.append("- %s%s" % (label, ("：" + snippet) if snippet else ""))
        block = "\n".join(lines)
        answer_text = str(result.get("answer") or "")
        if UNGROUNDED_MARK not in answer_text:
            result["answer"] = (answer_text.rstrip() + "\n\n" + block)[:80000]
    return result


def context_pack_receipt(pack: Mapping) -> dict:
    """给 stats / SSE / 运维看的 Context Pack 回执（不含正文，只有计数与口径）。"""
    pack = pack if isinstance(pack, Mapping) else {}
    budget = pack.get("budget") if isinstance(pack.get("budget"), Mapping) else {}
    return {
        "pack_version": pack.get("pack_version"),
        "pack_id": pack.get("pack_id"),
        "utility_version": pack.get("utility_version"),
        "grounding_version": (pack.get("grounding") or {}).get("grounding_version"),
        "gap_version": CONTEXT_GAP_VERSION,
        "gap_types": list(CONTEXT_GAP_TYPES),
        "gap_actions": list(CONTEXT_GAP_ACTIONS),
        "sections": {name: (pack.get("sections") or {}).get(name, {}).get("count", 0)
                     for name in CONTEXT_SECTIONS},
        "items": len(pack.get("items") or []),
        "citations": len(pack.get("citation_map") or {}),
        "context_gaps": _counter(gap.get("context_gap_type") for gap in (pack.get("context_gaps") or [])),
        "context_gap_actions": _counter(gap.get("action") for gap in (pack.get("context_gaps") or [])),
        "retrieval_requested": len([gap for gap in (pack.get("context_gaps") or [])
                                    if gap.get("requires_retrieval")]),
        "budget": {
            "total": budget.get("total"),
            "used": budget.get("used"),
            "estimated_tokens_before": budget.get("estimated_tokens_before"),
            "estimated_tokens_after": budget.get("estimated_tokens_after"),
            "trimmed_items": budget.get("trimmed_items"),
            "duplicates_collapsed": budget.get("duplicates_collapsed"),
            "trim_reasons": dict(budget.get("trim_reasons") or {}),
            "counter_evidence_used": budget.get("counter_evidence_used"),
            "counter_evidence_reserved": budget.get("reserved_counter_evidence"),
            "estimator": budget.get("estimator"),
        },
        "stats": dict(pack.get("stats") or {}),
    }


def selection_summary(trace: Sequence[Mapping]) -> dict:
    """selection trace 的聚合口径（验收/报表用；口径变了要同时改 `CONTEXT_SELECTION_VERSION`）。"""
    rows = [row for row in (trace or []) if isinstance(row, Mapping)]
    included = [row for row in rows if row.get("decision") == "included"]
    return {
        "trace_version": CONTEXT_SELECTION_VERSION,
        "rows": len(rows),
        "included": len(included),
        "excluded": len(rows) - len(included),
        "reason_distribution": _counter(row.get("reason") for row in rows),
        "included_reasons": _counter(row.get("reason") for row in included),
        "reserved_included": len([row for row in included if row.get("reserved")]),
        "tokens_included": sum(int(row.get("tokens") or 0) for row in included),
        "avg_utility_included": round(
            sum(float(row.get("utility") or 0.0) for row in included) / len(included), 8)
        if included else None,
    }


__all__ = [
    "ESTIMATOR_NOTE", "ESTIMATOR_VERSION", "GROUNDING_BLOCKING_VIOLATIONS",
    "GROUNDING_WARNING_VIOLATIONS", "UNGROUNDED_MARK",
    "build_context_graph", "build_context_pack", "build_prompt_blocks", "check_grounding",
    "citation_labels", "context_pack_enabled", "context_pack_receipt", "context_utility",
    "counter_reserve_ratio", "detect_context_gaps", "estimate_tokens", "evidence_strength",
    "freshness_factor", "grounding_gate_enabled", "make_context_item",
    "mark_ungrounded_answer", "normalize_evidence_layer", "output_reserve_tokens",
    "select_context_items", "selection_summary", "span_min_chars", "total_token_budget",
]
