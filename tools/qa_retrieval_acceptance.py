#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 7/8 使用侧验收：**检索命中率 / 引用可回溯率 / 词面接地率**（不调用 LLM）。

为什么要有这个工具：事件覆盖率是"过程指标"，抽得多不等于答得好。
用户真正关心的是"问一句，能不能给出有出处的答案"。所以验收改成看：

  · 命中率        问题能不能取到证据（evidence 非空）
  · 可回溯率      每条证据能不能点回原文（article:<id> / edge:<key> 都能在库里解析）
  · 词面接地率    取到的证据里是否真的出现了问题里的关键词（防止"答非所问"）
  · 图证据利用率  有多少问题用上了图谱事实（事件边/属性边）与有效期过滤
  · 平均耗时      单题检索耗时（不含 LLM 生成）
  · Recall@K      显式 K 口径（K = 本次实际使用的 --limit）：命中相关项 / 弱标注相关项
  · citation P/R  引用精确率（含期待词的采纳证据 / 采纳证据）与引用召回率（含期待词的题 / 有期待词的题）
  · cost          库内 qa_stage_runs.token_usage_json 按 run 累计的 tokens_in/out/total

指标口径（重点：不许用 0 冒充 null）：
  · 弱标注一律用题目里现成的 expect_terms；**分母为 0 时输出 null 并写 note，绝不输出 0 充数**。
  · cost 表/列不存在或读库失败时降级为 null + note（degraded=true）。
  · 验收本身不调用 LLM，所以 cost 是**库内累计值**（历史 run），不是本次检索的花费；
    做 before/after 就用两次留档的 tokens_total 相减。

benchmark 冻结（Phase 00 · F-3）：
  问题文件两种形态都能读：
    · 冻结形态：{"benchmark_version": "qa-acceptance-v1",
                "corpus_snapshot_id": "pending:first-run",
                "generated_at": "2026-10-09T00:00:00Z",
                "questions": [ ...题目... ]}
    · 旧 list 形态：[ ...题目... ] → benchmark_version 记 "legacy-unversioned"（如实记录"未冻结"，
      不假装有版本），并在 summary 的 benchmark_format 里保留这个事实。
  冻结的含义：questions 内容一字不改；要改题必须同时升 benchmark_version。
  corpus_snapshot_id 由首次 --save-history 留档时写入真实快照 id（见 _snapshot_id），
  在此之前写 "pending:first-run" 占位（占位不是真实绑定，别据此宣称语料已冻结）。

纯读库、纯检索，不占用模型槽位，可以和抽取回填并行跑。

用法
    # 1) 先按语料生成一套真实问题（离线，不用 LLM；写出的是冻结形态）
    python tools/qa_retrieval_acceptance.py --generate 24 --out config/qa_acceptance_questions.json

    # 2) 跑验收（可指定行业包 / 上限 / 输出 JSON / 留档）
    python tools/qa_retrieval_acceptance.py                              # 默认跑冻结 benchmark
    python tools/qa_retrieval_acceptance.py --questions config/qa_acceptance_questions.json
    python tools/qa_retrieval_acceptance.py --questions config/qa_acceptance_questions.json --json
    python tools/qa_retrieval_acceptance.py --json --save-history data/qa_acceptance_history.jsonl

  不带 --questions 时默认读 config/qa_acceptance_questions.json（冻结 benchmark，留档里的版本号才有对比意义）；
  该文件不存在才临时生成 24 题并记生成态（generated-adhoc，不可用于趋势对比）。

题目字段（可手改，改题必须同时升 benchmark_version）：
    {"id": "q1", "question": "某公司最近有什么动态？", "kind": "event",
     "industry_pack_id": "ai_news", "expect_terms": ["某公司"]}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlite_database import sqlite_db  # noqa: E402

# ---------------------------------------------------------------------------
# benchmark 冻结口径（Phase 00 · F-3）
# ---------------------------------------------------------------------------
LEGACY_BENCHMARK_VERSION = "legacy-unversioned"  # 旧 list 形态：没有版本号，如实记录
GENERATED_BENCHMARK_VERSION = "generated-adhoc"  # 命令行临时生成：等同未冻结
INLINE_BENCHMARK_FORMAT = "inline"               # evaluate() 被直接调用（根本没有文件）
PENDING_SNAPSHOT_PREFIX = "pending:"             # 语料快照占位前缀（尚未绑定真实快照）

# 指标 note：口径写在字段旁，避免以后有人把口径猜错
RECALL_AT_K_NOTE = (
    "弱标注口径：K=本次评估实际使用的 --limit（每题最多采纳证据条数上限）；"
    "分子=采纳证据里命中的期待词数（expect_terms 去重），分母=全部弱标注期待词数"
    "（只统计带 expect_terms 的题目）；分母为 0 → null（不输出 0 充数）"
)
CITATION_PRECISION_NOTE = (
    "弱标注口径：分子=含 ≥1 个期待词的采纳证据条数，分母=采纳证据总条数"
    "（只统计带 expect_terms 的题目，未被标注的题目不计入分母）；分母为 0 → null"
)
CITATION_RECALL_NOTE = (
    "弱标注口径：分子=至少一条采纳证据含期待词的题目数，分母=带 expect_terms 的题目数"
    "（题级粗细口径）；分母为 0 → null"
)
COST_NOTE = (
    "按 run 聚合 qa_stage_runs.token_usage_json 的库内累计值（验收本身不调用 LLM，"
    "故这不是本次检索的花费）；runs_counted=有 token 记录的 run 数，stage_rows=扫到的 stage 行数；"
    "before/after 用两次留档的 tokens_total 相减"
)
# 兼容常见 token 字段命名（写入方尚未统一，见 qa_storage.py 的 token_usage_json）
TOKEN_IN_KEYS = ("tokens_in", "input_tokens", "prompt_tokens", "prompt")
TOKEN_OUT_KEYS = ("tokens_out", "output_tokens", "completion_tokens", "completion")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 不带 --questions 时优先跑这份冻结 benchmark（否则留档里的版本号没有对比意义）
DEFAULT_QUESTIONS_FILE = "config/qa_acceptance_questions.json"


def _resolve_questions_path(path: str) -> str:
    """问题文件路径：默认路径按**仓库根**解析，避免在别的目录跑时找不到冻结 benchmark。"""
    if not path or os.path.isabs(path) or os.path.isfile(path):
        return path
    candidate = os.path.join(REPO_ROOT, path)
    return candidate if os.path.isfile(candidate) else path


def _default_meta(**overrides) -> dict:
    """benchmark 元数据的默认值（任何入口都要能在 summary 里查到版本，不许缺字段）。"""
    meta = {
        "benchmark_version": LEGACY_BENCHMARK_VERSION,
        "benchmark_format": INLINE_BENCHMARK_FORMAT,
        "benchmark_questions_file": "",
        "benchmark_generated_at": None,
        "corpus_snapshot_id": None,
        "pack_filter": "",
    }
    meta.update(overrides)
    return meta


def parse_questions(payload, *, source: str = "") -> tuple:
    """问题集 → (questions, benchmark_meta)，两种形态都认。

    · 对象形态（冻结形态）：顶层 dict，题目在 "questions" 里，带 benchmark_version 等元数据；
    · 旧 list 形态：顶层就是题目列表 → benchmark_version 记 LEGACY_BENCHMARK_VERSION，
      并在 benchmark_format 里留下 "legacy-list"，让"这份 benchmark 未冻结"这件事可被查到。
    """
    if isinstance(payload, dict):
        raw = payload.get("questions")
        questions = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
        declared = str(payload.get("benchmark_version") or "").strip()
        snapshot = payload.get("corpus_snapshot_id")
        generated = payload.get("generated_at")
        meta = _default_meta(
            benchmark_version=declared or LEGACY_BENCHMARK_VERSION,
            benchmark_format="object",
            benchmark_questions_file=str(source or ""),
            benchmark_generated_at=str(generated) if generated else None,
            corpus_snapshot_id=str(snapshot) if snapshot else None,
        )
        return questions, meta
    if isinstance(payload, list):
        questions = [item for item in payload if isinstance(item, dict)]
        meta = _default_meta(
            benchmark_version=LEGACY_BENCHMARK_VERSION,
            benchmark_format="legacy-list",
            benchmark_questions_file=str(source or ""),
        )
        return questions, meta
    raise ValueError("问题文件必须是题目列表或含 questions 的对象，实际为 %s" % type(payload).__name__)


def load_questions(path: str) -> tuple:
    """读问题文件（两种形态都支持），返回 (questions, benchmark_meta)。"""
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return parse_questions(payload, source=path)


def _weak_label_terms(item: dict) -> list:
    """题目的弱标注相关项：**只取显式 expect_terms**（去空去重）。

    这里刻意不回退到语义锚点词：回退词是检索自己挑的，用它当"相关项"会让 recall 失真。
    """
    terms = []
    for raw in (item.get("expect_terms") or []):
        text = str(raw).strip()
        if text and text not in terms:
            terms.append(text)
    return terms


def _terms_of(question: str, expect_terms=None) -> list:
    """问题里的检索关键词（用于"词面接地"判定）：显式 expect_terms 优先。"""
    terms = [str(item).strip() for item in (expect_terms or []) if str(item).strip()]
    if terms:
        return terms
    try:
        from qa_retrieval import _semantic_anchor_terms, _terms

        terms = list(_semantic_anchor_terms([question])) + list(_terms([question]))
    except Exception:
        terms = []
    cleaned = []
    for item in terms:
        text = str(item).strip()
        if len(text) >= 2 and text not in cleaned:
            cleaned.append(text)
    return cleaned[:6]


def _fetch_ref(cursor, ref: str) -> dict:
    """解析一条证据引用：article:<id> 或 edge:<edge_key>。"""
    text = str(ref or "")
    if text.startswith("article:"):
        try:
            article_id = int(text.split(":", 1)[1])
        except ValueError:
            return {}
        cursor.execute("SELECT id, url, title FROM articles WHERE id=?", (article_id,))
        row = cursor.fetchone()
        return dict(row) if row else {}
    if text.startswith("edge:"):
        edge_key = text.split(":", 1)[1]
        cursor.execute(
            "SELECT edge_key, article_id, relation_kind, src_key, dst_key, valid_from, valid_to"
            " FROM kg_edges WHERE edge_key=?", (edge_key,))
        row = cursor.fetchone()
        if not row:
            return {}
        edge = dict(row)
        if edge.get("article_id"):
            cursor.execute("SELECT id, url, title FROM articles WHERE id=?",
                           (int(edge["article_id"]),))
            article = cursor.fetchone()
            edge["article"] = dict(article) if article else {}
        return edge
    return {}


def _resolve_evidence(cursor, item: dict) -> dict:
    """判定一条证据可不可回溯（要点：必须有可打开的原文链接）。"""
    ref = str(item.get("evidence_ref") or "")
    resolved = _fetch_ref(cursor, ref)
    if resolved:
        url = str(resolved.get("url") or "")
        if not url and isinstance(resolved.get("article"), dict):
            url = str(resolved["article"].get("url") or "")
        return {"ref": ref, "ok": bool(url), "url": url, "kind": ref.split(":", 1)[0]}
    # 没有可解析的库内引用时，退一步看是否自带 URL（联网/官方来源）
    url = str(item.get("source_url") or "")
    return {"ref": ref, "ok": bool(url), "url": url, "kind": "external"}


def _evidence_haystack(item: dict) -> str:
    """一条证据的文本面（标题 + 摘录 + 来源 URL）：接地/引用判定共用同一份口径。"""
    return " ".join([
        str(item.get("title") or ""),
        str(item.get("content_excerpt") or ""),
        str(item.get("source_url") or ""),
    ])


def _grounded(question: str, evidence: list, terms: list) -> bool:
    """词面接地：证据文本里是否出现问题的关键词（至少命中一个）。"""
    if not terms:
        return bool(evidence)
    for item in evidence:
        haystack = _evidence_haystack(item)
        if any(term in haystack for term in terms):
            return True
    return False


def _citation_counts(evidence: list, terms: list) -> tuple:
    """弱标注口径下的引用计数 → (采纳证据总条数, 含期待词的采纳证据条数, 命中的期待词集合)。

    · 第 1 项是 citation_precision 的分母，第 2 项是它的分子；
    · 第 3 项（去重后的期待词）是 recall@K 的分子来源。
    """
    matched_terms = set()
    cited_matched = 0
    for item in evidence:
        haystack = _evidence_haystack(item)
        hits = [term for term in terms if term in haystack]
        if hits:
            cited_matched += 1
            matched_terms.update(hits)
    return len(evidence), cited_matched, matched_terms


def _token_int(value):
    """token 数转 int；转不了（None / 字符串脏值 / bool）返回 None，不拿 0 顶替。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pick_int(payload: dict, keys) -> object:
    """按候选键名取第一个能转成 int 的值。"""
    for key in keys:
        if key in payload:
            value = _token_int(payload.get(key))
            if value is not None:
                return value
    return None


def _token_pair(raw) -> tuple:
    """一条 token_usage_json → (输入 tokens, 输出 tokens)；认不出来就 (None, None)。"""
    payload = raw
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload or "{}")
        except (TypeError, ValueError):
            return None, None
    if not isinstance(payload, dict):
        return None, None
    if isinstance(payload.get("usage"), dict):
        # 有些写入方会把 OpenAI 风格的 {"usage": {...}} 整体塞进来
        merged = dict(payload)
        merged.update(payload["usage"])
        payload = merged
    return _pick_int(payload, TOKEN_IN_KEYS), _pick_int(payload, TOKEN_OUT_KEYS)


def _cost_summary() -> dict:
    """cost：按 run 聚合 qa_stage_runs.token_usage_json。

    表/列不存在或读库失败 → **降级为 null + note**（不抛异常、不用 0 冒充）；
    表存在但没有行 → 如实输出 0 并把"表存在、确无记录"写进 note。
    """
    cost = {
        "tokens_in": None,
        "tokens_out": None,
        "tokens_total": None,
        "runs_counted": 0,
        "stage_rows": 0,
        "stage_rows_with_tokens": 0,
        "source": "qa_stage_runs.token_usage_json",
        "degraded": False,
        "note": COST_NOTE,
    }
    try:
        sqlite_db._ensure_connection()
        with sqlite_db.lock:
            cursor = sqlite_db.connection.cursor()
            try:
                cursor.execute("SELECT run_id, token_usage_json FROM qa_stage_runs")
                rows = list(cursor.fetchall())
            finally:
                cursor.close()
    except Exception as exc:  # 表/列不存在、库不可读 → 降级
        cost["degraded"] = True
        cost["note"] = "读 qa_stage_runs 失败，cost 降级为 null：%s" % str(exc)[:160]
        return cost

    tokens_in = 0
    tokens_out = 0
    runs_with_tokens = set()
    for row in rows:
        run_id = str(row[0])
        token_in, token_out = _token_pair(row[1])
        cost["stage_rows"] += 1
        if token_in is None and token_out is None:
            continue
        cost["stage_rows_with_tokens"] += 1
        runs_with_tokens.add(run_id)
        tokens_in += int(token_in or 0)
        tokens_out += int(token_out or 0)
    cost["tokens_in"] = tokens_in
    cost["tokens_out"] = tokens_out
    cost["tokens_total"] = tokens_in + tokens_out
    cost["runs_counted"] = len(runs_with_tokens)
    if not rows:
        cost["note"] = COST_NOTE + "；本次库内 qa_stage_runs 有 0 行 → 累计值为 0（表存在，不是降级）"
    elif not runs_with_tokens:
        cost["note"] = COST_NOTE + "；库内有 %d 行但都没带 token 记录 → 累计值为 0" % cost["stage_rows"]
    return cost


def evaluate(questions: list, *, limit: int = 12, verbose: bool = True,
             benchmark_meta=None, cost=None) -> dict:
    """逐题跑真实检索，统计命中 / 可回溯 / 接地 / 图证据 / 耗时 + Recall@K / 引用 / cost。

    benchmark_meta：来自 parse_questions()/load_questions() 的冻结元数据；
    不传就按"内联未冻结"记录（benchmark_version=legacy-unversioned），保证 summary 里永不缺版本号。
    cost：注入用（测试时避免连库）；不传则现场从 qa_stage_runs 汇总。
    """
    from qa_retrieval import ArticleRetriever

    meta = _default_meta(**(benchmark_meta or {}))
    retriever = ArticleRetriever(sqlite_db)
    sqlite_db._ensure_connection()
    results = []
    for index, item in enumerate(questions, 1):
        question = str(item.get("question") or "").strip()
        if not question:
            continue
        pack_id = str(item.get("industry_pack_id") or "")
        plan = {
            "question": question,
            "queries": [question],
            "entities": [str(t) for t in (item.get("expect_terms") or [])],
            "needs_local_articles": True,
        }
        labeled_terms = _weak_label_terms(item)
        started = time.monotonic()
        try:
            outcome = retriever.retrieve(plan, industry_pack_id=pack_id, limit=limit)
        except Exception as exc:
            results.append({"id": item.get("id") or index, "question": question,
                            "kind": item.get("kind") or "", "error": str(exc)[:160],
                            "evidence": 0, "hit": False, "traceable": False,
                            "grounded": False, "graph": 0, "ms": 0,
                            "expect_terms_count": len(labeled_terms), "expect_terms_hit": 0,
                            "cited_total": 0, "cited_matched": 0, "citation_hit": False})
            continue
        elapsed_ms = int((time.monotonic() - started) * 1000)
        evidence = list(outcome.get("evidence") or [])
        cursor = sqlite_db.connection.cursor()
        try:
            checks = [_resolve_evidence(cursor, one) for one in evidence]
        finally:
            cursor.close()
        traceable = bool(checks) and all(one["ok"] for one in checks)
        terms = _terms_of(question, item.get("expect_terms"))
        # 弱标注口径的引用计数（分子分母都只算"有 expect_terms 的题"，见 _citation_counts）
        cited_total, cited_matched, matched_terms = _citation_counts(evidence, labeled_terms)
        graph_items = [one for one in evidence if str(one.get("source_type")) == "graph"]
        row = {
            "id": item.get("id") or index,
            "question": question,
            "kind": item.get("kind") or "",
            "industry_pack_id": pack_id,
            "evidence": len(evidence),
            "hit": bool(evidence),
            "traceable": traceable,
            "traceable_ratio": (sum(1 for one in checks if one["ok"]) / len(checks)) if checks else 0.0,
            "grounded": _grounded(question, evidence, terms),
            "terms": terms,
            "graph": len(graph_items),
            "graph_attribute": sum(1 for one in graph_items
                                   if (one.get("metadata") or {}).get("relation_kind") == "attribute"),
            "graph_event": sum(1 for one in graph_items
                               if (one.get("metadata") or {}).get("relation_kind") == "event"),
            "graph_receipt": outcome.get("graph") or {},
            "time_window": {k: v for k, v in (outcome.get("time_window") or {}).items()
                            if k in ("has_time", "label", "expanded", "in_window_adopted")},
            "ms": elapsed_ms,
            # —— 以下为 F-3 新增：弱标注口径逐题计数（汇总见 summarize） ——
            "expect_terms_count": len(labeled_terms),
            "expect_terms_hit": len(matched_terms),
            "cited_total": cited_total,
            "cited_matched": cited_matched,
            "citation_hit": bool(cited_matched),
        }
        results.append(row)
        if verbose:
            flag = "命中" if row["hit"] else "空"
            print("  [%-4s] %-4s 证据 %2d 条（图 %d）| 可回溯 %.0f%% | 接地 %s | %5d ms | %s"
                  % (row["id"], flag, row["evidence"], row["graph"],
                     100.0 * row["traceable_ratio"], "是" if row["grounded"] else "否",
                     row["ms"], question[:40]))

    summary = summarize(results, limit=limit, benchmark_meta=meta, cost=cost)
    return {"summary": summary, "results": results}


def _fmt_ratio(value) -> str:
    """比率打印：None（分母为 0）如实打 null，绝不拿 0.0% 冒充。"""
    return "null（分母为 0）" if value is None else "%.1f%%" % (100.0 * float(value))


def _fmt_count(value) -> str:
    """token 计数打印：None（降级/未知）如实打 null，不打 0。"""
    return "null" if value is None else str(value)


def _utc_now_z() -> str:
    """UTC ISO8601（带 Z 后缀，秒级）：冻结时间戳不留本地时区歧义。"""
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def summarize(results: list, *, limit: int = 12, benchmark_meta=None, cost=None) -> dict:
    """逐题明细 → 汇总指标（纯汇总，不连库；cost 由调用方注入，便于不连真库的测试）。

    既有字段一个不删不改；新增字段一律带口径 note。
    """
    meta = _default_meta(**(benchmark_meta or {}))
    # 弱标注只认带 expect_terms 的题目：没有标注就不进 recall / citation 的分母
    labeled = [r for r in results if int(r.get("expect_terms_count") or 0) > 0]
    terms_total = sum(int(r.get("expect_terms_count") or 0) for r in labeled)
    terms_hit = sum(int(r.get("expect_terms_hit") or 0) for r in labeled)
    cited_total = sum(int(r.get("cited_total") or 0) for r in labeled)
    cited_matched = sum(int(r.get("cited_matched") or 0) for r in labeled)
    cited_questions = sum(1 for r in labeled if r.get("citation_hit"))

    total = len(results)
    # 分母为 0 时把"本次确实是 0 分母"写进 note：避免以后有人把 null 读成"没实现"
    recall_note = RECALL_AT_K_NOTE
    if not terms_total:
        recall_note += "；本次分母为 0（没有带 expect_terms 的题目）→ null"
    precision_note = CITATION_PRECISION_NOTE
    if not cited_total:
        precision_note += "；本次分母为 0（没有带 expect_terms 的题目或没取到证据）→ null"
    citation_recall_note = CITATION_RECALL_NOTE
    if not labeled:
        citation_recall_note += "；本次分母为 0（没有带 expect_terms 的题目）→ null"
    summary = {
        "questions": total,
        "hit_rate": round(sum(1 for r in results if r["hit"]) / total, 4) if total else 0.0,
        "traceable_rate": round(sum(1 for r in results if r["traceable"]) / total, 4) if total else 0.0,
        "grounded_rate": round(sum(1 for r in results if r["grounded"]) / total, 4) if total else 0.0,
        "avg_evidence": round(sum(r["evidence"] for r in results) / total, 2) if total else 0.0,
        "graph_rate": round(sum(1 for r in results if r["graph"]) / total, 4) if total else 0.0,
        "errors": sum(1 for r in results if r.get("error")),
        "avg_ms": int(sum(r["ms"] for r in results) / total) if total else 0,
        # —— 新增：benchmark 冻结口径（版本永远有值，未冻结就写 legacy-unversioned）——
        "benchmark_version": meta["benchmark_version"],
        "benchmark_format": meta["benchmark_format"],
        "benchmark_questions_file": meta["benchmark_questions_file"],
        "benchmark_generated_at": meta["benchmark_generated_at"],
        "corpus_snapshot_id": meta["corpus_snapshot_id"],
        "pack_filter": meta["pack_filter"],
        # —— 新增：Recall@K（显式 K 口径 + 弱标注，分母 0 → null）——
        "k": int(limit),
        "recall_at_k": round(terms_hit / terms_total, 4) if terms_total else None,
        "recall_at_k_note": recall_note,
        "recall_at_k_terms_hit": terms_hit,
        "recall_at_k_terms": terms_total,
        "recall_at_k_labeled_questions": len(labeled),
        # —— 新增：citation precision / recall（弱标注，分母 0 → null）——
        "citation_precision": round(cited_matched / cited_total, 4) if cited_total else None,
        "citation_precision_note": precision_note,
        "citation_recall": round(cited_questions / len(labeled), 4) if labeled else None,
        "citation_recall_note": citation_recall_note,
        "citation_evidence_matched": cited_matched,
        "citation_evidence_total": cited_total,
        "citation_questions_matched": cited_questions,
        "citation_questions_labeled": len(labeled),
        # —— 新增：cost（qa_stage_runs.token_usage_json 按 run 累计；缺表降级 null）——
        "cost": cost if cost is not None else _cost_summary(),
        "by_kind": {},
    }
    kinds = {}
    for row in results:
        bucket = kinds.setdefault(row["kind"] or "other",
                                  {"questions": 0, "hit": 0, "traceable": 0, "grounded": 0, "graph": 0})
        bucket["questions"] += 1
        bucket["hit"] += 1 if row["hit"] else 0
        bucket["traceable"] += 1 if row["traceable"] else 0
        bucket["grounded"] += 1 if row["grounded"] else 0
        bucket["graph"] += 1 if row["graph"] else 0
    summary["by_kind"] = kinds
    return summary


def generate_questions(count: int = 24, *, limit_packs: int = 6) -> list:
    """按语料现况生成问题（不调用 LLM）：事件问法 / 属性问法 / 时间窗问法三类。"""
    sqlite_db._ensure_connection()
    questions = []
    with sqlite_db.lock:
        cursor = sqlite_db.connection.cursor()
        try:
            # 1) 有事件的主体 → "最近有什么动态"（考事件证据）
            cursor.execute(
                """
                SELECT subject, industry_pack_id, COUNT(DISTINCT article_id) AS articles
                FROM intel_article_events
                WHERE subject NOT IN ('__no_event__', '__error__') AND subject != ''
                GROUP BY subject, industry_pack_id
                HAVING COUNT(DISTINCT article_id) >= 2
                ORDER BY articles DESC
                LIMIT 12
                """)
            subjects = [dict(row) for row in cursor.fetchall()]
            # 2) 有属性的主体 → "某属性是什么"（考属性边与有效期）
            cursor.execute(
                """
                SELECT subject, attribute, industry_pack_id, COUNT(*) AS rows_count
                FROM intel_article_attributes
                WHERE subject != '' AND attribute != ''
                GROUP BY subject, attribute, industry_pack_id
                ORDER BY rows_count DESC, subject
                LIMIT 12
                """)
            attributes = [dict(row) for row in cursor.fetchall()]
            # 3) 近期热点词 → 时间窗问法（考时间硬过滤与扩窗）
            cursor.execute(
                """
                SELECT matched_keywords_json, industry_pack_id FROM article_intel_classifications
                WHERE COALESCE(matched_keywords_json, '[]') NOT IN ('', '[]')
                ORDER BY id DESC LIMIT 60
                """)
            keyword_rows = [dict(row) for row in cursor.fetchall()]
        finally:
            cursor.close()

    pack_seen = {}
    for row in subjects:
        pack = str(row.get("industry_pack_id") or "")
        if pack_seen.get(pack, 0) >= 2 or len(pack_seen) > limit_packs:
            continue
        pack_seen[pack] = pack_seen.get(pack, 0) + 1
        questions.append({
            "id": "e%d" % (len(questions) + 1),
            "question": "%s最近有什么动态？" % str(row["subject"])[:24],
            "kind": "event",
            "industry_pack_id": pack,
            "expect_terms": [str(row["subject"])[:24]],
        })
    for row in attributes:
        pack = str(row.get("industry_pack_id") or "")
        questions.append({
            "id": "a%d" % (len(questions) + 1),
            "question": "%s的%s是什么？" % (str(row["subject"])[:20], str(row["attribute"])[:12]),
            "kind": "attribute",
            "industry_pack_id": pack,
            "expect_terms": [str(row["subject"])[:20]],
        })
    hot_words = []
    for row in keyword_rows:
        try:
            words = json.loads(row.get("matched_keywords_json") or "[]")
        except Exception:
            continue
        for word in words:
            text = str(word).strip()
            if len(text) >= 2 and text not in hot_words:
                hot_words.append((text, str(row.get("industry_pack_id") or "")))
    for word, pack in hot_words[:6]:
        questions.append({
            "id": "t%d" % (len(questions) + 1),
            "question": "%s最近 90 天有什么新动态？" % word[:16],
            "kind": "time_window",
            "industry_pack_id": pack,
            "expect_terms": [word[:16]],
        })
    return questions[: max(1, int(count))]


def _corpus_snapshot() -> dict:
    """留档用的语料/图谱快照：覆盖率双口径 + 图规模（每次验收都记一份，便于看趋势）。"""
    snapshot = {}
    try:
        from intel_database import intel_repository

        intel_repository._ensure()
        snapshot["coverage"] = intel_repository.coverage_report()
        with sqlite_db.lock:
            cursor = sqlite_db.connection.cursor()
            try:
                for label, sql in (
                    ("nodes", "SELECT COUNT(*) AS n FROM kg_nodes"),
                    ("edges", "SELECT COUNT(*) AS n FROM kg_edges"),
                    ("event_edges",
                     "SELECT COUNT(*) AS n FROM kg_edges WHERE relation_kind='event'"),
                    ("attribute_edges",
                     "SELECT COUNT(*) AS n FROM kg_edges WHERE relation_kind='attribute'"),
                    ("attributes",
                     "SELECT COUNT(*) AS n FROM intel_article_attributes"),
                    ("attributes_with_validity",
                     "SELECT COUNT(*) AS n FROM intel_article_attributes"
                     " WHERE COALESCE(valid_from,'')<>'' OR COALESCE(valid_to,'')<>''"),
                    ("event_rows",
                     "SELECT COUNT(*) AS n FROM intel_article_events"
                     " WHERE subject NOT IN ('__no_event__','__error__')"),
                    ("placeholders",
                     "SELECT COUNT(*) AS n FROM intel_article_events WHERE subject='__no_event__'"),
                ):
                    try:
                        cursor.execute(sql)
                        row = cursor.fetchone()
                        snapshot[label] = int((row["n"] if hasattr(row, "keys") else row[0]) or 0)
                    except Exception:
                        snapshot[label] = None
            finally:
                cursor.close()
    except Exception as exc:
        snapshot["error"] = str(exc)[:160]
    return snapshot


def _snapshot_id(snapshot: dict) -> str:
    """语料快照 id：快照内容哈希（内容相同 → id 相同，便于核查 benchmark 是否绑在同一语料上）。"""
    blob = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    return "corpus:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _append_history(path: str, outcome: dict) -> dict:
    """把一次验收结果追加到 JSONL（一行一次，便于按天对比与出趋势报告）。

    Phase 00 · F-3：留档行必带 benchmark_version + corpus_snapshot_id，
    这样"benchmark 有没有被偷偷改过""语料换没换"都能在历史里查出来：
      · benchmark_version            本次跑的是哪一版问题集（未冻结就写 legacy-unversioned）；
      · corpus_snapshot_id           本次语料快照的真实内容哈希（这个才是真实快照绑定）；
      · benchmark_corpus_snapshot_id 问题文件里声明的语料快照 id（占位时是 pending:first-run）；
      · benchmark_corpus_snapshot_state 声明状态：pending / declared / unset。
    """
    import datetime as _dt

    summary = outcome.get("summary") or {}
    corpus = _corpus_snapshot()
    declared = summary.get("corpus_snapshot_id")
    declared_text = str(declared or "")
    record = {
        # recorded_at 保持原格式（本地时间），兼容已有历史行；UTC 口径另给一列
        "recorded_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "recorded_at_utc": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "benchmark_version": summary.get("benchmark_version") or LEGACY_BENCHMARK_VERSION,
        "corpus_snapshot_id": _snapshot_id(corpus),
        "benchmark_corpus_snapshot_id": declared_text or None,
        "benchmark_corpus_snapshot_state": (
            "pending" if declared_text.startswith(PENDING_SNAPSHOT_PREFIX)
            else ("declared" if declared_text else "unset")),
        "summary": summary,
        "corpus": corpus,
    }
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="阶段 7/8 使用侧检索验收（不调用 LLM）")
    parser.add_argument("--questions", default=DEFAULT_QUESTIONS_FILE,
                        help="问题 JSON 文件（对象形态或旧 list 形态都认；默认用冻结的 %s，"
                             "该文件不存在才临时生成）" % DEFAULT_QUESTIONS_FILE)
    parser.add_argument("--generate", type=int, default=0,
                        help="按语料生成 N 个问题并写文件（配合 --out，写成冻结形态）")
    parser.add_argument("--out", default="", help="生成的问题写到哪（默认打印）")
    parser.add_argument("--limit", type=int, default=12, help="每题最多取多少条证据（也是 Recall@K 的 K 口径）")
    parser.add_argument("--pack", default="", help="只看某个行业包的问题")
    parser.add_argument("--save-history", default="",
                        help="把本次结果追加到 JSONL（含 benchmark_version / corpus_snapshot_id / 覆盖率与图规模快照）")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args(argv)

    if args.generate:
        questions = generate_questions(args.generate)
        # 生成的也是"冻结形态"：带 benchmark_version / 语料快照占位 / 生成时间，
        # 避免再写回旧 list 形态（旧 list 会被当成 legacy-unversioned）。
        payload = {
            "benchmark_version": "qa-acceptance-v1",
            "corpus_snapshot_id": PENDING_SNAPSHOT_PREFIX + "first-run",
            "corpus_snapshot_id_note": (
                "占位：首次 --save-history 留档时写入真实语料快照 id（见 _snapshot_id 的内容哈希）；"
                "在此之前它是 pending 状态，不代表语料已绑定。"),
            "generated_at": _utc_now_z(),
            "questions": questions,
        }
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=1)
            print("已生成 %d 个问题 → %s（benchmark_version=%s）"
                  % (len(questions), args.out, payload["benchmark_version"]))
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 0

    questions_path = _resolve_questions_path(args.questions)
    if questions_path and os.path.isfile(questions_path):
        questions, benchmark_meta = load_questions(questions_path)
    elif args.questions and args.questions != DEFAULT_QUESTIONS_FILE:
        print("问题文件不存在：%s" % args.questions, file=sys.stderr)
        return 2
    else:
        # 连默认的冻结 benchmark 都没有 → 临时生成（版本记 generated-adhoc，别拿它做趋势对比）
        print("提示：未找到冻结 benchmark %s，改为按语料临时生成（benchmark_version=%s）"
              % (DEFAULT_QUESTIONS_FILE, GENERATED_BENCHMARK_VERSION))
        questions = generate_questions(24)
        benchmark_meta = _default_meta(benchmark_version=GENERATED_BENCHMARK_VERSION,
                                       benchmark_format="generated")
    if args.pack:
        questions = [q for q in questions if str(q.get("industry_pack_id") or "") == args.pack]
    benchmark_meta = _default_meta(**dict(benchmark_meta, pack_filter=args.pack or ""))

    print("=" * 96)
    print("检索验收（阶段 7/8 使用侧）：%d 个问题" % len(questions))
    print("=" * 96)
    outcome = evaluate(questions, limit=args.limit, benchmark_meta=benchmark_meta)
    summary = outcome["summary"]
    print("\n" + "=" * 96)
    print("汇总")
    print("=" * 96)
    print("  命中率（取到证据）      %.1f%%" % (100.0 * summary["hit_rate"]))
    print("  可回溯率（能点回原文）  %.1f%%" % (100.0 * summary["traceable_rate"]))
    print("  词面接地率（答对题）    %.1f%%" % (100.0 * summary["grounded_rate"]))
    print("  图证据利用率            %.1f%%" % (100.0 * summary["graph_rate"]))
    print("  平均证据条数            %.2f" % summary["avg_evidence"])
    print("  平均检索耗时            %d ms" % summary["avg_ms"])
    print("  报错题数                %d" % summary["errors"])
    print("\n  benchmark（冻结口径）：%s | 形态 %s | 文件 %s"
          % (summary["benchmark_version"], summary["benchmark_format"],
             summary["benchmark_questions_file"] or "(命令行临时生成)"))
    print("    生成时间 %s | 声明语料快照 %s%s"
          % (summary["benchmark_generated_at"] or "-",
             summary["corpus_snapshot_id"] or "-",
             "（占位：尚未绑定真实快照）"
             if str(summary["corpus_snapshot_id"] or "").startswith(PENDING_SNAPSHOT_PREFIX) else ""))
    print("  Recall@%d（弱标注 expect_terms）  %s  = 命中相关项 %d / 相关项 %d（有标注题 %d）"
          % (summary["k"], _fmt_ratio(summary["recall_at_k"]),
             summary["recall_at_k_terms_hit"], summary["recall_at_k_terms"],
             summary["recall_at_k_labeled_questions"]))
    print("  citation precision / recall      %s / %s  （采纳证据 %d 条，含期待词 %d 条；题级 %d/%d）"
          % (_fmt_ratio(summary["citation_precision"]), _fmt_ratio(summary["citation_recall"]),
             summary["citation_evidence_total"], summary["citation_evidence_matched"],
             summary["citation_questions_matched"], summary["citation_questions_labeled"]))
    cost = summary["cost"]
    print("  cost（qa_stage_runs 库内累计）    tokens_in %s | tokens_out %s | total %s | runs %s%s"
          % (_fmt_count(cost["tokens_in"]), _fmt_count(cost["tokens_out"]),
             _fmt_count(cost["tokens_total"]), cost["runs_counted"],
             "  [降级：见 note]" if cost.get("degraded") else ""))
    print("\n  分类：")
    for kind, bucket in sorted(summary["by_kind"].items()):
        print("    %-12s 题 %2d | 命中 %2d | 可回溯 %2d | 接地 %2d | 用图 %2d"
              % (kind, bucket["questions"], bucket["hit"], bucket["traceable"],
                 bucket["grounded"], bucket["graph"]))
    if args.save_history:
        record = _append_history(args.save_history, outcome)
        print("\n  已留档 → %s" % args.save_history)
        print("    recorded_at=%s（UTC %s）| benchmark_version=%s | corpus_snapshot_id=%s"
              % (record["recorded_at"], record["recorded_at_utc"],
                 record["benchmark_version"], record["corpus_snapshot_id"]))
        print("    声明语料快照 %s（状态 %s）"
              % (record["benchmark_corpus_snapshot_id"] or "-",
                 record["benchmark_corpus_snapshot_state"]))
    if args.json:
        print("\n" + json.dumps(outcome, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
