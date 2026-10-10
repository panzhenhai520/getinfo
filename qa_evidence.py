#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""证据层（graph-rag-v2 通用包 Phase 02 · P02-01…P02-04）。

为什么要这个文件：通用包 01_V2_ARCHITECTURE §9 明确"不要把 Chunk 当成最终证据"，
真正进入 Evidence Graph 的应该是**支持某个 Claim 的最小证据 Span**，并且每条证据都要能
回溯到 Source / Chunk / Span，还要有跨轮去重的身份。本仓库现状（Phase 01 对齐盘点结论）
恰好缺这四样：没有最小 span（quote_span）、证据的 entities/relations 未表达、`seen`
只在单次 run 的内存里（被拒证据只留计数不留身份）、没有 fingerprint。

本模块只做四件事，且**只加不改**：
  · P02-01：把一条既有证据标注成 `metadata.evidence_layer`（status / span / source /
            entities / relations），全部取值来自 `qa_graph_contracts` 的契约；
  · P02-02：provenance —— run/stage/route/检索方式/时间 + source/chunk/span 链条；
  · P02-03：给"见过的证据身份"（含被拒的）提供构造与作用域化登记/查询的适配层；
  · P02-04：三种 fingerprint（span 级 / 来源级 / 内容级）与两级去重。

硬约束（不许违反）：
  1. `qa_contracts.EVIDENCE_SCHEMA` 是 `additionalProperties: False` 的历史冻结指纹
     （P00-02：EVIDENCE 370301331c02c738），**一个字都不改**；本模块新增的一切都放在它
     放行的 `metadata` 对象里，证据条目的顶层键集保持不变。
  2. 不做新 NER、不编关系：entities 只把既有 metadata/图谱边里已有的东西显式化，
     relations 只由图谱边证据给出；拿不到就空着（宁可空，也不把共现说成因果）。
  3. 本模块不 import 存储层（duck-typed store），避免 qa_storage ↔ qa_evidence 循环依赖。

配置化参数（都有默认值；关掉即回到 Phase 01 行为，可随时回滚）：
  · QA_EVIDENCE_LAYER_ENABLED   默认 1：关掉则 `annotate_evidence` 原样返回证据；
  · QA_EVIDENCE_SPAN_MAX_CHARS  默认 320：最小 span 的字符上限（钳制 80…2000）；
  · QA_EVIDENCE_SEEN_DEDUPE     默认 rejected：跨轮/跨 run 去重强度 off / rejected / all。
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Iterable, Mapping

from qa_graph_contracts import (
    EVIDENCE_LAYER_VERSION,
    EVIDENCE_STATUS_BY_RELATIONSHIP,
    EVIDENCE_STATUS_UNVERIFIED,
    EVIDENCE_STATUSES,
    KG_NODE_TYPES,
    KG_RELATION_KINDS,
    validate as validate_contract,
)

# 内容指纹只看标题+正文的前 600 个规范化字符（与 qa_pipeline._dedupe_evidence 的历史口径
# 逐字一致：改这个数会让"同一篇文章换个长度就当成两条"，反而更差）。
_FINGERPRINT_CHARS = 600
# 句子边界：最小 span 尽量落在完整句子上，但绝不为了"好看的边界"突破字符上限。
_BOUNDARY_CHARS = "。！？；!?;…\n\r"
# 边界回退搜索的窗口：太大会把 span 拉长到超上限，太小则切在半句上。
_SNAP_LOOKBACK = 80
# 单个实体元素的保留上限（证据包会进 SSE 事件与最终答案 JSON，不能无界增长；
# 12 与 qa_level1 提示词里"entities 不超过 12 项"的口径一致）
_MAX_ENTITIES = 12

_SEEN_MODES = ("off", "rejected", "all")

# 与 kg_builder._node_key / qa_retrieval._graph_node_key 同口径（去空白/括号/标点、小写）。
# 这里刻意自带一份而不是 import qa_retrieval：证据层不该拖着整个检索模块；
# 等价性由 tests/test_qa_phase02_evidence.py 的守门用例钉死。
_NODE_KEY_STRIP = re.compile(r"[^一-鿿a-z0-9]")


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().casefold() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(str(os.getenv(name, str(default))).strip())
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def evidence_layer_enabled() -> bool:
    """证据层总开关（QA_EVIDENCE_LAYER_ENABLED，默认开）。关掉 = 完全回到 Phase 01 行为。"""
    return _env_flag("QA_EVIDENCE_LAYER_ENABLED", True)


def span_max_chars() -> int:
    """最小 span 的字符上限（QA_EVIDENCE_SPAN_MAX_CHARS，默认 320，钳制 80…2000）。"""
    return _env_int("QA_EVIDENCE_SPAN_MAX_CHARS", 320, 80, 2000)


def seen_dedupe_mode() -> str:
    """跨轮/跨 run 去重强度（QA_EVIDENCE_SEEN_DEDUPE）：

    · `rejected`（默认）：只丢"上一轮被闸门拒掉的东西"——它们本来就不该进证据包，
      丢掉不可能让答案变差，纯粹省一次重复检索（MASTER_RULES 第 14 条）；
    · `all`：任何见过的来源都不再重复进入证据包（更省，但同一会话追问时可能少给证据）；
    · `off`：只登记、不过滤（登记照旧，便于观测）。
    """
    raw = str(os.getenv("QA_EVIDENCE_SEEN_DEDUPE", "rejected") or "").strip().casefold()
    return raw if raw in _SEEN_MODES else "rejected"


def seen_prune_enabled() -> bool:
    """seen 表的过期清理开关（QA_EVIDENCE_SEEN_PRUNE_ENABLED）——**默认关**。

    为什么默认关：`qa_evidence_seen` 是 Phase 02 才建的新表（schema v7），还没有真实写入量
    与去重命中率的数据。先让维护入口空转、观察一段时间（多少行、多少天会被判过期、
    清理后跨轮去重是否仍然有效），确认不会把"长时间没被复用但依然合法"的来源记忆误删，
    再按运维节奏打开——不默认开是为了不影响生产节奏（清理本身不阻塞问答，但删错记忆
    会让重复垃圾重新进证据包）。
    """
    return _env_flag("QA_EVIDENCE_SEEN_PRUNE_ENABLED", False)


def seen_ttl_days() -> int:
    """seen 身份的保留天数（QA_EVIDENCE_SEEN_TTL_DAYS，默认 30，钳制 1…3650）。"""
    return _env_int("QA_EVIDENCE_SEEN_TTL_DAYS", 30, 1, 3650)


def now_utc() -> str:
    """与 qa_storage._now 同格式的 UTC ISO8601（毫秒 + Z）。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _norm_text(value) -> str:
    """归一化：折叠空白 + casefold（指纹与实体键共用，保证跨运行稳定）。"""
    return " ".join(str(value or "").split()).casefold()


def _digest(text: str, length: int = 24) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:length]


def node_key(value) -> str:
    """实体键：与 kg_builder._node_key / qa_retrieval._graph_node_key 同口径。"""
    text = str(value or "").lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[（(].*?[)）]", "", text)
    return _NODE_KEY_STRIP.sub("", text)[:120]


def _metadata(item: Mapping) -> dict:
    value = item.get("metadata") if isinstance(item, Mapping) else None
    return dict(value) if isinstance(value, Mapping) else {}


# ── P02-01 / P02-02：Source / status / span / entities / relations ────────────

def source_identity(item: Mapping) -> dict:
    """Evidence → Source：稳定来源标识（同一篇文章/同一 chunk 每次都得到同一个 source_id）。

    优先级：图谱边 > 文档+chunk > 文档 > 文章 > 联网摘要 > URL 摘要 > evidence_ref。
    """
    md = _metadata(item)
    article_id = item.get("article_id")
    document_id = str(item.get("document_id") or "")
    chunk_id = str(item.get("chunk_id") or "")
    graph_edge_key = str(md.get("graph_edge_key") or "")
    url = str(item.get("source_url") or "")
    ref = str(item.get("evidence_ref") or "")
    try:
        article_id = int(article_id) if article_id not in (None, "") else None
    except (TypeError, ValueError):
        article_id = None
    if graph_edge_key:
        source_id = "edge:%s" % graph_edge_key
    elif document_id and chunk_id:
        source_id = "chunk:%s#%s" % (document_id, chunk_id)
    elif document_id:
        source_id = "doc:%s" % document_id
    elif article_id:
        source_id = "article:%s" % article_id
    elif ref.startswith("web:"):
        source_id = ref
    elif url:
        source_id = "url:%s" % _digest(_norm_text(url), 16)
    else:
        source_id = ref or "unknown"
    return {
        "source_id": source_id,
        "source_type": str(item.get("source_type") or ""),
        # 只留一个短副本：完整 URL 在证据条目的顶层字段里已经有了，证据层不再重复搬运
        "source_url": url[:500],
        "article_id": article_id,
        "document_id": document_id,
        "chunk_id": chunk_id,
        "authority_level": item.get("authority_level"),
        "published_at": str(item.get("published_at") or ""),
    }


def evidence_status(item: Mapping) -> str:
    """既有 `relationship` → 规范化状态（不改变 `relationship` 本身）。

    注意：`UNVERIFIED` 只表示"还没判定"，它**不是** Phase 03 verifier 的结论。
    """
    relationship = str(item.get("relationship") or "").strip().casefold()
    return EVIDENCE_STATUS_BY_RELATIONSHIP.get(relationship, EVIDENCE_STATUS_UNVERIFIED)


def source_fingerprint(item: Mapping) -> str:
    """来源级指纹（去重主键）：同一篇文章/同一 chunk/同一条边 → 同一个值。

    刻意**不含正文**：正文长度会随检索参数变化，含进去就会把同一条来源当成两条，
    跨轮去重立刻失效。
    """
    source = source_identity(item)
    return _digest("v1|%s|%s" % (source["source_id"], source["source_type"]))


def content_fingerprint(item: Mapping) -> str:
    """内容级指纹：标题+正文（前 600 个规范化字符）——与既有 `_dedupe_evidence` 同口径。"""
    text = " ".join(str(item.get(key) or "")
                    for key in ("title", "content_excerpt", "excerpt", "content"))
    if not text.strip():
        return ""
    return _digest(" ".join(text.casefold().split())[:_FINGERPRINT_CHARS])


def evidence_fingerprint(item: Mapping, *, span: Mapping | None = None) -> str:
    """span 级指纹（P02-04）：来源 + 标题 + 最小 span 引文 → 24 位十六进制。

    与 `source_fingerprint` 的分工：来源级回答"这条来源见过没有"（跨轮去重用它），
    span 级回答"这条**证据**（同一来源的同一段话）见过没有"（Evidence Graph 的节点身份）。
    """
    source = source_identity(item)
    quote = ""
    if isinstance(span, Mapping):
        quote = str(span.get("quote") or "")
    if not quote:
        quote = str(item.get("content_excerpt") or "")[:span_max_chars()]
    return _digest("v1|%s|%s|%s|%s" % (
        source["source_id"], source["source_type"],
        _norm_text(item.get("title"))[:200], _norm_text(quote)[:_FINGERPRINT_CHARS],
    ))


def evidence_terms(item: Mapping, extra: Iterable | None = None) -> list:
    """可用于定位最小 span 的实词：命中关键词 / 主题标签 / 问题实词（调用方传入）。

    口径与 qa_relevance 一致：单字太泛不参与；去重且保持顺序。
    """
    md = _metadata(item)
    values = []
    for key in ("matched_keywords", "topic_tags"):
        for value in md.get(key) or []:
            values.append(value)
    for value in item.get("relevance_hits") or []:
        values.append(value)
    for value in extra or []:
        values.append(value)
    terms, seen = [], set()
    for value in values:
        clean = _norm_text(value)
        if not (2 <= len(clean) <= 16) or clean in seen:
            continue
        seen.add(clean)
        terms.append(clean)
    return terms


def _best_term_hit(text: str, terms: Iterable) -> tuple | None:
    """最长优先、同长取最早出现的实词命中，返回 (起始下标, 词长)。"""
    folded = text.casefold()
    if len(folded) != len(text):  # casefold 可能改长度（如 ß→ss），退化到 lower/原串
        folded = text.lower()
        if len(folded) != len(text):
            folded = text
    best = None
    for term in sorted({str(item or "") for item in terms or ()}, key=len, reverse=True):
        clean = term.strip().casefold()
        if len(clean) < 2:
            continue
        index = folded.find(clean)
        if index < 0:
            continue
        if best is None or len(clean) > best[1] or (len(clean) == best[1] and index < best[0]):
            best = (index, len(clean))
    return best


def _sentence_bounds(text: str, start: int, end: int) -> tuple:
    """包含 [start, end) 的完整句子边界（找不到边界就用文本两端）。"""
    left = 0
    for offset in range(start, -1, -1):
        if offset == 0 or text[offset - 1] in _BOUNDARY_CHARS:
            left = offset
            break
    right = len(text)
    for offset in range(end, len(text)):
        if text[offset] in _BOUNDARY_CHARS:
            right = offset + 1
            break
    return left, right


def _snap_to_boundaries(text: str, start: int, end: int, *, cap: int) -> tuple:
    """把窗口吸附到句子边界；吸附后超过上限则放弃吸附（上限优先）。"""
    snapped_start, snapped_end = start, end
    for offset in range(start, max(-1, start - _SNAP_LOOKBACK - 1), -1):
        if offset == 0 or text[offset - 1] in _BOUNDARY_CHARS:
            snapped_start = offset
            break
    for offset in range(end, min(len(text), end + _SNAP_LOOKBACK)):
        if text[offset] in _BOUNDARY_CHARS:
            snapped_end = offset + 1
            break
    if snapped_end - snapped_start > cap or snapped_end <= snapped_start:
        return start, end
    return snapped_start, snapped_end


def minimal_quote_span(content, terms: Iterable | None = None, *,
                       max_chars: int | None = None, source_hint: str = "query_terms") -> dict:
    """最小证据 span：从长 chunk 里切出**支持性最强的一小段**，并给可校验的字符偏移。

    规则（先精准、再退化，绝不假装精准）：
      ① 命中实词所在的**完整句子**（句末标点为准）；句子本身超过上限时，
      ② 退化为"以命中词为中心"的窗口，并尽力吸附到最近的句边界（上限于边界）；
      ③ 一个实词都定位不到时取开头一段，`source="lead"`；整条本来就短则 `"whole"`。

    返回 `{"start","end","quote","source","chars"}`，保证
    `content[start:end] == quote`（偏移相对这条证据自己的 content_excerpt）。
    """
    text = str(content or "")
    cap = max(80, int(max_chars or span_max_chars()))
    if not text:
        return {"start": 0, "end": 0, "quote": "", "source": "whole", "chars": 0}
    if len(text) <= cap:
        return {"start": 0, "end": len(text), "quote": text, "source": "whole", "chars": len(text)}

    hit = _best_term_hit(text, terms)
    if hit is None:
        start, end, label = 0, cap, "lead"
    else:
        position, length = hit
        label = source_hint if source_hint in ("query_terms", "matched_keywords", "anchor") else "query_terms"
        sentence_start, sentence_end = _sentence_bounds(text, position, position + length)
        if sentence_end - sentence_start <= cap:
            start, end = sentence_start, sentence_end
        else:
            start = max(0, position - max(0, (cap - length) // 2))
            end = min(len(text), start + cap)
            start = max(0, end - cap)
            start, end = _snap_to_boundaries(text, start, end, cap=cap)

    raw = text[start:end]
    lead_ws = len(raw) - len(raw.lstrip())
    trail_ws = len(raw) - len(raw.rstrip())
    start, end = start + lead_ws, end - trail_ws
    quote = text[start:end]
    if not quote:  # 极端情况（整窗都是空白）：退回窗口本身，绝不返回空 quote
        start, end = (0, min(len(text), cap)) if hit is None else (hit[0], min(len(text), hit[0] + cap))
        quote, label = text[start:end], label
    return {"start": start, "end": end, "quote": quote, "source": label, "chars": len(quote)}


def evidence_entities(item: Mapping) -> list:
    """证据里提到的实体（只把既有信息显式化，不做新 NER）。

    来源按可信度排列：图谱边的两端与属性值 > 检索命中的关键词 > 主题标签 > 问题实词命中。
    """
    md = _metadata(item)
    out, seen = [], set()

    def add(label, entity_type, origin) -> None:
        key = node_key(label)
        if not key or key in seen or len(out) >= _MAX_ENTITIES:
            return
        seen.add(key)
        out.append({"entity_key": key, "label": str(label)[:200],
                    "entity_type": entity_type, "origin": origin})

    if str(item.get("source_type") or "") == "graph" or md.get("graph_edge_key"):
        add(md.get("src_key"), "entity", "graph_src")
        if md.get("attr_value"):
            add(md.get("attr_value"), "value", "graph_attr")
        else:
            add(md.get("dst_key"), "entity", "graph_dst")
    for value in md.get("matched_keywords") or []:
        add(value, "keyword", "matched_keyword")
    for value in md.get("topic_tags") or []:
        add(value, "topic", "topic_tag")
    for value in item.get("relevance_hits") or []:
        add(value, "keyword", "question_term")
    return out


def evidence_relations(item: Mapping) -> list:
    """证据里表达的关系：**只有图谱边证据**能给出，文章证据一律为空列表。

    宁可空着，也不把"同一篇文章里出现过"包装成因果（01_V2_ARCHITECTURE §10 第 8 条）。
    校验不过的关系（枚举越界/缺字段）直接丢弃，不把脏数据带进证据图。
    """
    md = _metadata(item)
    if not md.get("graph_edge_key"):
        return []
    row = {
        "src_key": str(md.get("src_key") or ""),
        "dst_key": str(md.get("dst_key") or md.get("attr_value") or ""),
        "relation_kind": str(md.get("relation_kind") or ""),
        "attr_key": str(md.get("attr_key") or ""),
        "attr_value": str(md.get("attr_value") or ""),
        "valid_from": str(md.get("valid_from") or ""),
        "valid_to": str(md.get("valid_to") or ""),
        "evidence_ref": str(item.get("evidence_ref") or ""),
    }
    if row["relation_kind"] not in KG_RELATION_KINDS or not row["src_key"] or not row["dst_key"]:
        return []
    ok, _note = validate_contract("evidence_relation", row)
    return [row] if ok else []


def evidence_provenance(item: Mapping, *, run_id: str = "", stage: str = "", route: str = "",
                        round_index: int = 0, corpus_version: str = "",
                        retrieved_at: str = "") -> dict:
    """P02-02 provenance：从证据能一路回到 Source / Chunk / Span 与产生它的那次检索。

    刻意**不复述** `match_reason`/`source_url` 这类顶层已有的长文本：证据层对象是嵌在
    证据条目里的，重复搬运只会让 SSE 事件与最终答案 JSON 白白变大。
    """
    source = source_identity(item)
    return {
        "run_id": str(run_id or ""),
        "stage": str(stage or ""),
        "route": str(route or item.get("retrieval_method") or ""),
        "retrieval_method": str(item.get("retrieval_method") or ""),
        "round_index": int(round_index or 0),
        "retrieved_at": str(retrieved_at or now_utc()),
        "corpus_version": str(corpus_version or ""),
        "source": source,
        "chunk_id": source["chunk_id"],
    }


def annotate_evidence(item: Mapping, *, terms: Iterable | None = None, run_id: str = "",
                      stage: str = "", route: str = "", round_index: int = 0,
                      corpus_version: str = "", retrieved_at: str = "") -> dict:
    """把一条证据标注成"证据层对象"，写入 `metadata.evidence_layer`（**顶层键集不变**）。

    关掉 QA_EVIDENCE_LAYER_ENABLED 时原样返回（只做了浅拷贝），便于一键回滚。
    """
    result = dict(item) if isinstance(item, Mapping) else {}
    if not evidence_layer_enabled():
        return result
    metadata = _metadata(item)
    anchors = evidence_terms(item, extra=terms)
    origin = "matched_keywords" if (metadata.get("matched_keywords") or metadata.get("topic_tags")) \
        else "query_terms"
    span = minimal_quote_span(str(item.get("content_excerpt") or ""), anchors,
                              max_chars=span_max_chars(), source_hint=origin)
    fingerprint = evidence_fingerprint(item, span=span)
    source = source_identity(item)
    layer = {
        "layer_version": EVIDENCE_LAYER_VERSION,
        "evidence_ref": str(item.get("evidence_ref") or ""),
        "relationship": str(item.get("relationship") or ""),
        "status": evidence_status(item),
        "span": span,
        "source": source,
        "entities": evidence_entities(item),
        "relations": evidence_relations(item),
        "fingerprint": fingerprint,
        "source_fingerprint": source_fingerprint(item),
        "provenance": evidence_provenance(
            item, run_id=run_id, stage=stage, route=route,
            round_index=round_index, corpus_version=corpus_version, retrieved_at=retrieved_at,
        ),
    }
    metadata["evidence_layer"] = layer
    result["metadata"] = metadata
    return result


def annotate_evidence_batch(items: Iterable, **kwargs) -> list:
    """批量标注（保持顺序；非 Mapping 的元素原样丢弃以避免污染证据包）。"""
    return [annotate_evidence(item, **kwargs) for item in (items or []) if isinstance(item, Mapping)]


def evidence_object(item: Mapping) -> dict:
    """取出证据层对象（没有标注过就返回空字典，调用方自己决定兜底）。"""
    value = _metadata(item).get("evidence_layer")
    return dict(value) if isinstance(value, Mapping) else {}


# ── P02-03 / P02-04：seen 登记、作用域查询与去重 ─────────────────────────────

def seen_records(items: Iterable, *, status: str) -> list:
    """把证据（或只有身份的被拒候选）转成 `qa_evidence_seen` 的行。

    被拒候选往往已经没有正文了，但 `evidence_ref/title` 还在——来源级指纹不依赖正文，
    所以照样能算出稳定身份（这正是"被拒证据只留计数不留身份"缺口的修法）。
    """
    if status not in EVIDENCE_STATUSES and status not in ("seen", "confirmed", "rejected"):
        status = "seen"
    rows, seen = [], set()
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        layer = evidence_object(item)
        key = str(layer.get("source_fingerprint") or source_fingerprint(item))
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append({
            "source_fingerprint": key,
            "span_fingerprint": str(layer.get("fingerprint") or ""),
            "evidence_ref": str(item.get("evidence_ref") or ""),
            "source_type": str(item.get("source_type") or ""),
            "status": status,
        })
    return rows


def record_seen(store, *, scope: Mapping, accepted: Iterable = (), rejected: Iterable = (),
                witnessed: Iterable = (), run_id: str = "", round_index: int = 0) -> dict:
    """P02-03：把本轮的 seen / confirmed / rejected 身份登记到 store（失败绝不影响主流程）。

    `scope` 必须显式给 (owner_user_id, session_id, industry_pack_id) 三元组——跨用户/
    跨会话/跨行业包串味是这一层最危险的错。
    `witnessed` 是"见过但本轮不作表态"的（例如因重复被跳过的），登记成中性 `seen`，
    既不冒充 confirmed，也不冤枉成 rejected。
    """
    summary = {"confirmed": 0, "rejected": 0, "seen": 0, "recorded": 0, "error": ""}
    scope = scope if isinstance(scope, Mapping) else {}
    records = (seen_records(accepted, status="confirmed")
               + seen_records(rejected, status="rejected")
               + seen_records(witnessed, status="seen"))
    if not records:
        return summary
    try:
        result = store.record_seen_evidence(
            owner_user_id=str(scope.get("owner_user_id") or ""),
            session_id=str(scope.get("session_id") or ""),
            industry_pack_id=str(scope.get("industry_pack_id") or ""),
            records=records, run_id=str(run_id or ""), round_index=int(round_index or 0),
        )
    except Exception as exc:  # noqa: BLE001 —— 留痕/去重绝不拖累问答
        summary["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
        return summary
    summary.update({key: result.get(key, summary.get(key, 0))
                    for key in ("recorded", "confirmed", "rejected", "error")})
    summary["seen"] = summary["recorded"] - summary["confirmed"] - summary["rejected"]
    return summary


def load_seen(store, *, scope: Mapping, items: Iterable) -> dict:
    """按作用域查这批证据里"之前见过"的身份 → {source_fingerprint: status}（异常返回空）。"""
    scope = scope if isinstance(scope, Mapping) else {}
    keys = [row["source_fingerprint"] for row in
            seen_records(items, status="seen") if row.get("source_fingerprint")]
    if not keys:
        return {}
    try:
        return dict(store.seen_evidence(
            owner_user_id=str(scope.get("owner_user_id") or ""),
            session_id=str(scope.get("session_id") or ""),
            industry_pack_id=str(scope.get("industry_pack_id") or ""),
            source_fingerprints=keys,
        ) or {})
    except Exception:  # noqa: BLE001
        return {}


def filter_seen(items: Iterable, seen: Mapping, *, mode: str | None = None) -> tuple:
    """跨轮/跨 run 去重（P02-03 + P02-04），返回 (保留的证据, 审计)。

    `seen` 是 {source_fingerprint: status}（由 `load_seen` 取）。被丢掉的、以及"见过但
    本轮仍保留"的，都在审计里留身份，便于排查"为什么这次少了几条证据"。
    """
    mode = (mode or seen_dedupe_mode())
    if mode not in _SEEN_MODES:
        mode = "rejected"
    seen = seen if isinstance(seen, Mapping) else {}
    kept, dropped, repeated = [], [], []
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        layer = evidence_object(item)
        key = str(layer.get("source_fingerprint") or source_fingerprint(item))
        previous = str(seen.get(key) or "") if key else ""
        if mode != "off" and previous and (mode == "all" or previous == "rejected"):
            dropped.append({"source_fingerprint": key, "evidence_ref": str(item.get("evidence_ref") or ""),
                            "previous_status": previous, "reason": "seen_dedupe_%s" % mode})
            continue
        if previous:
            result = dict(item)
            metadata = _metadata(item)
            previous_layer = dict(metadata.get("evidence_layer") or {})
            previous_layer["repeat"] = True
            previous_layer["previous_status"] = previous
            metadata["evidence_layer"] = previous_layer
            result["metadata"] = metadata
            repeated.append({"source_fingerprint": key,
                             "evidence_ref": str(item.get("evidence_ref") or ""),
                             "previous_status": previous})
            kept.append(result)
            continue
        kept.append(dict(item))
    return kept, {
        "mode": mode, "checked": len(kept) + len(dropped), "kept": len(kept),
        "dropped": dropped[:50], "dropped_count": len(dropped),
        "repeated": repeated[:50], "repeated_count": len(repeated),
    }


def prune_seen_evidence(store, *, older_than_days: int | None = None) -> dict:
    """维护入口（Phase 02 缺口 2）：按开关与保留期清理 seen 身份，返回审计。

    **默认关**（`QA_EVIDENCE_SEEN_PRUNE_ENABLED`，见 `seen_prune_enabled` 的说明）：
    关掉时一条都不删，只回 `{"enabled": False, "deleted": 0}`——维护入口照样能安全调用。
    开关打开时委托 store 的 `prune_seen_evidence`（按 `last_seen_at` 删过期行）；
    任何异常都吞掉并写进 `error`：清理绝不能拖累维护作业。
    """
    if older_than_days is None:
        days = seen_ttl_days()
    else:
        try:
            days = max(1, int(older_than_days))
        except (TypeError, ValueError):
            days = seen_ttl_days()
    if not seen_prune_enabled():
        return {"enabled": False, "deleted": 0, "ttl_days": days}
    try:
        deleted = int(store.prune_seen_evidence(older_than_days=days) or 0)
    except Exception as exc:  # noqa: BLE001
        return {"enabled": True, "deleted": 0, "ttl_days": days,
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}
    return {"enabled": True, "deleted": deleted, "ttl_days": days}


def dedupe_evidence_items(items: Iterable, limit: int) -> list:
    """既有 `_dedupe_evidence` 的口径（集中到证据层单一事实源，行为不变）。

    去重键：evidence_ref / article_id / source_url / 内容指纹，四者任一撞上即视为重复；
    第一条胜出，按 limit 截断。原实现散落在 `qa_pipeline._dedupe_evidence`，
    这里集中后 `qa_pipeline` 只做委托，避免同一套规则两处漂移。
    """
    result, refs, urls, article_ids, fingerprints = [], set(), set(), set(), set()
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        ref = str(item.get("evidence_ref") or "")
        url = str(item.get("source_url") or "")
        article_id = item.get("article_id")
        fingerprint = content_fingerprint(item)
        if (
            ref in refs
            or (article_id and article_id in article_ids)
            or (url and url in urls)
            or (fingerprint and fingerprint in fingerprints)
        ):
            continue
        refs.add(ref)
        if url:
            urls.add(url)
        if article_id:
            article_ids.add(article_id)
        if fingerprint:
            fingerprints.add(fingerprint)
        result.append(dict(item))
        if len(result) >= limit:
            break
    return result


def dedupe_by_fingerprint(items: Iterable) -> tuple:
    """span 级去重（P02-04）：同一来源的同一段话只留一条，返回 (保留, 审计)。

    与 `dedupe_evidence_items` 的分工：那个按"来源"去重（既有行为），这个按"证据对象"
    （来源 + 最小 span）去重——两条证据指纹相同 = 同来源同段落，是真重复。
    """
    kept, dropped, seen = [], [], set()
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        layer = evidence_object(item)
        key = str(layer.get("fingerprint") or evidence_fingerprint(item))
        if key in seen:
            dropped.append({"fingerprint": key, "evidence_ref": str(item.get("evidence_ref") or "")})
            continue
        seen.add(key)
        kept.append(dict(item))
    return kept, {"checked": len(kept) + len(dropped), "kept": len(kept),
                  "dropped": dropped[:50], "dropped_count": len(dropped)}


__all__ = [
    "annotate_evidence", "annotate_evidence_batch", "content_fingerprint",
    "dedupe_by_fingerprint", "dedupe_evidence_items", "evidence_entities",
    "evidence_fingerprint", "evidence_layer_enabled", "evidence_object",
    "evidence_provenance", "evidence_relations", "evidence_status", "evidence_terms",
    "filter_seen", "load_seen", "minimal_quote_span", "node_key", "now_utc",
    "prune_seen_evidence", "record_seen", "seen_dedupe_mode", "seen_prune_enabled",
    "seen_records", "seen_ttl_days", "source_fingerprint",
    "source_identity", "span_max_chars",
]
