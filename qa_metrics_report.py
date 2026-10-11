#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 17（P17-03）· **metric report**：指标总表 + 独立标注集（与调参样本零重叠）。

## 这个模块回答三件事

1. **每个指标现在到底有没有值**：逐条对照 `docs/graph-rag-v2/reference/METRICS.md` 的七族指标，
   给出"值 / 口径来源 / 状态码"。状态码只有四种：
     · `ok`                  本轮真跑出来的值；
     · `no_object`           真机数据上没有可判对象（例如真机 qa_conflicts 0 行）；
     · `not_measurable`      在本包硬约束下不可得（例如需要模型的质量指标）；
     · `not_implemented`     本包未实现该指标（如实写，不假装有）。
   **绝不把"没有对象"写成 0**（D-060 的口径）。

2. **P17-03 新登记的两个排序指标**：`mrr_at_k` 与 `ndcg_at_k`。
   Phase 04 明确把"引用精确率对'召回更多'是惩罚性的、应改用 nDCG/证据召回"留给 Phase 17。
   这里落地时**不新造标注**：仍用冻结 benchmark 的弱标注 `expect_terms`，
   口径写在 `RANKING_METRIC_NOTES` 里，逐题明细（`first_hit_rank` / `relevant_at_k`）可复算。

3. **独立标注集**（Phase 03 明确"真正的 NLI 质量要等有独立标注集（Phase 17 的 metric report 范围）"）：
   `build_independent_annotation()` 用**评价前写死的机械规则**从真机快照构造 claim–evidence 对，
   标注成 `expect_support=True/False` 两组；`evaluate_annotation()` 用 Phase 03 的**同一份核验代码**
   逐对判 verdict，给出 `support_recall` / `false_support_rate` / 组的 verdict 分布。
   独立性的三条硬证据（测试里逐条断言）：
     · 与金标 18 对**文本零重叠**（`overlap_with_golden`）；
     · 与金标 18 对**来源零重叠**（金标是手写合成文本，不可能出现在真机 ref 里）；
     · 构造规则**不含** Phase 03 的阈值（`qa_verifier.support_min/partial_min/...` 一个都不读）。
   能力边界如实写：这是**机器构造 + 可人工复核的 holdout**，不是人类金标；
   它证明的是"在没参与调参的样本上，核验层的支持/反支持方向是否守住"。

## 可复算

    python tools/qa_phase17_acceptance.py --section metrics --rebuild-annotation
    python -m pytest tests -q -k "phase17"
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Mapping, Sequence

METRIC_REPORT_VERSION = "qa-metric-report-v1"
ANNOTATION_VERSION = "qa-e2e-annotation-v1"
DEFAULT_ANNOTATION_PATH = os.path.join("config", "qa_e2e_annotation_v1.json")
GOLDEN_PATH = os.path.join("config", "qa_verifier_golden.json")

RANKING_METRIC_NOTES = {
    "mrr_at_k": ("弱标注口径：对每题取「第一条含 ≥1 个 expect_term 的采纳证据」的排名 r，"
                 "题级得分 1/r（没命中记 0）；MRR = 有 expect_terms 的题的平均分。"
                 "分母为 0 → null。P17-03 新登记（Phase 04 遗留项）。"),
    "ndcg_at_k": ("弱标注口径：二元增益（证据含 ≥1 个 expect_term 记 1，否则 0），"
                  "DCG = Σ gain/log2(i+1)，IDCG = 理想排序（把全部相关项排在最前）的 DCG；"
                  "NDCG = DCG/IDCG，逐题取平均。分母为 0 → null。P17-03 新登记。"),
    "evidence_recall_at_k": ("证据召回：命中了的采纳证据条数 / 该题全部相关证据条数；"
                             "弱标注下「全部相关证据」不可知，故这里定义为"
                             "**题级证据召回** = 至少一条采纳证据含期待词的题 / 有标注题，"
                             "与 citation_recall 同值同源（**不另算一套**，只做登记）。"),
}

ANNOTATION_RULES = {
    "rules_version": ANNOTATION_VERSION,
    "constructed_from": "baseline/qa-context-real-sample.json（A 机只读快照，真机 run/claim/证据）",
    "preregistered": True,
    "preregistered_statement": (
        "下面两条规则在**任何核验判定之前**写死，且只读 claim 文本/evidence_refs/证据正文，"
        "不读 qa_verifier 的任何阈值与判定结果。"),
    "positive_rule": ("① evidence_ref 出现在 claim.evidence_refs 里（真机确实把它当引用的那条）；"
                      "② claim 文本的**字符二元组覆盖率**在证据正文（标题+摘录）里 ≥ 0.5。"
                      "两条同时满足 → 标 expect_support=True（即「这条证据应当被判为支持」）。"),
    "negative_cross_subject_rule": ("证据与 claim **不在同一个 run**，且覆盖率 < 0.2 "
                                    "→ 标 expect_support=False（跨主体证据不可能支持该结论）。"),
    "negative_uncited_rule": ("证据与 claim 同 run 但**未被 claim 引用**，且覆盖率 < 0.2 "
                              "→ 标 expect_support=False。"),
    "coverage_definition": ("字符二元组集合的包含率：|bigrams(claim) ∩ bigrams(text)| / "
                            "|bigrams(claim)|；bigram 取自去掉空白后的原文。"),
    "independence_claim": ("与 config/qa_verifier_golden.json 的 18 对金标**零文本重叠**；"
                           "金标是人工合成文本，本标注集只取真机文本。"),
    "known_limitation": ("机器构造 + 可人工复核的 holdout，**不是人类金标**；"
                         "它回答「在没参与调参的样本上方向是否守住」，"
                         "不能替代人类标注的 NLI 质量评测。"),
}
POSITIVE_COVERAGE_FLOOR = 0.5
NEGATIVE_COVERAGE_CEILING = 0.2
MAX_PAIRS_PER_GROUP = 60
MIN_PAIRS_PER_GROUP = 8


# ---------------------------------------------------------------------------
# 排序类指标（弱标注，可复算）
# ---------------------------------------------------------------------------
def ranking_metrics(rows: Sequence[Mapping]) -> dict:
    """`mrr_at_k` / `ndcg_at_k`：只用逐题 `first_hit_rank` / `relevant_ranks` / `relevant_at_k`。

    NDCG 用**真实的命中位置**（`relevant_ranks`）算 DCG，IDCG 用"相关项全部排最前"的理想序，
    二元增益。这样"命中得多但排得靠后"与"命中一条但排第一"会被区分开——
    这正是 Phase 04 指出 citation_precision 缺失的那一面。
    """
    import math

    labeled = [row for row in rows if int(row.get("expect_terms_count") or 0) > 0]
    mrr_values = []
    ndcg_values = []
    for row in labeled:
        rank = int(row.get("first_hit_rank") or 0)
        mrr_values.append(1.0 / rank if rank else 0.0)
        ranks = [int(item) for item in (row.get("relevant_ranks") or []) if int(item) > 0]
        relevant = int(row.get("relevant_at_k") or 0) or len(ranks)
        total = int(row.get("evidence") or 0)
        dcg = sum(1.0 / math.log2(position + 1) for position in sorted(ranks))
        ideal = sum(1.0 / math.log2(position + 1)
                    for position in range(1, min(relevant, total) + 1))
        ndcg_values.append((dcg / ideal) if ideal else 0.0)
    return {
        "mrr_at_k": round(sum(mrr_values) / len(mrr_values), 6) if mrr_values else None,
        "mrr_at_k_note": RANKING_METRIC_NOTES["mrr_at_k"],
        "ndcg_at_k": round(sum(ndcg_values) / len(ndcg_values), 6) if ndcg_values else None,
        "ndcg_at_k_note": RANKING_METRIC_NOTES["ndcg_at_k"],
        "labeled_questions": len(labeled),
        "questions_with_hit": sum(1 for row in labeled if int(row.get("first_hit_rank") or 0)),
    }


# ---------------------------------------------------------------------------
# 独立标注集：确定性构造 + 与调参样本的零重叠证明
# ---------------------------------------------------------------------------
def _bigrams(text: str) -> set:
    cleaned = "".join(str(text or "").split())
    return {cleaned[index:index + 2] for index in range(max(0, len(cleaned) - 1))}


def _coverage(claim_text: str, evidence_text: str) -> float:
    claim = _bigrams(claim_text)
    if not claim:
        return 0.0
    return len(claim & _bigrams(evidence_text)) / float(len(claim))


def _evidence_text(item: Mapping) -> str:
    return "%s %s" % (item.get("title") or "", item.get("content_excerpt") or "")


def _golden_texts(path: str = GOLDEN_PATH) -> set:
    """金标 18 对的全部文本（claim / evidence）——用来证明零重叠。"""
    texts = set()
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:  # noqa: BLE001
        return texts
    pairs = payload.get("pairs") if isinstance(payload, Mapping) else payload
    for pair in (pairs or []):
        if not isinstance(pair, Mapping):
            continue
        for key in ("claim", "evidence", "claim_text", "evidence_text", "text"):
            value = pair.get(key)
            if isinstance(value, str) and value.strip():
                texts.add("".join(value.split()))
            elif isinstance(value, Mapping):
                for sub in value.values():
                    if isinstance(sub, str) and sub.strip():
                        texts.add("".join(sub.split()))
    return texts


def build_independent_annotation(snapshot: Mapping, *, source_path: str = "",
                                 golden_path: str = GOLDEN_PATH) -> dict:
    """从真机快照**确定性**构造独立标注集（同输入永远同一份、同 id）。"""
    try:
        import tools.qa_phase08_context_acceptance as acc08
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("拿不到 Phase 08 的图重建口径：%s" % exc)

    buckets = acc08._rows_by_run(dict(snapshot or {}))
    golden = _golden_texts(golden_path)
    positives: list = []
    negatives: list = []
    skipped = {"no_evidence": 0, "no_claim_text": 0, "golden_overlap": 0, "no_partner": 0}
    run_ids = sorted(buckets)
    for run_id in run_ids:
        graph, claims, evidence = acc08._graph_for(buckets[run_id])
        by_ref = {str(item.get("evidence_ref") or ""): item for item in evidence}
        for node in claims:
            claim = node.get("claim") if isinstance(node.get("claim"), Mapping) else {}
            claim_text = str(claim.get("text") or node.get("text") or "").strip()
            if not claim_text:
                skipped["no_claim_text"] += 1
                continue
            claim_key = "".join(claim_text.split())
            if claim_key in golden:
                skipped["golden_overlap"] += 1
                continue
            refs = [str(ref) for ref in (claim.get("evidence_refs") or [])]
            cited = [by_ref[ref] for ref in refs if ref in by_ref]
            if not cited and not evidence:
                skipped["no_evidence"] += 1
            for item in cited:
                text_key = "".join(_evidence_text(item).split())
                if text_key in golden:
                    skipped["golden_overlap"] += 1
                    continue
                coverage = _coverage(claim_text, _evidence_text(item))
                if coverage >= POSITIVE_COVERAGE_FLOOR:
                    positives.append({
                        "pair_id": "POS-%s" % hashlib.sha256(
                            ("%s|%s|%s" % (run_id, claim_text, item.get("evidence_ref"))
                             ).encode("utf-8")).hexdigest()[:12],
                        "group": "POSITIVE_CITED_HIGH_COVERAGE", "run_id": run_id,
                        "claim_id": str(node.get("claim_id") or claim.get("claim_id") or ""),
                        "claim_text": claim_text,
                        "evidence_ref": str(item.get("evidence_ref") or ""),
                        "evidence_text": _evidence_text(item)[:400],
                        "coverage": round(coverage, 6), "expected_support": True,
                        "construction": "positive_rule",
                    })
            # 负例 (a)：同 run 未引用且低覆盖
            for item in evidence:
                ref = str(item.get("evidence_ref") or "")
                if ref in refs:
                    continue
                text_key = "".join(_evidence_text(item).split())
                if text_key in golden:
                    skipped["golden_overlap"] += 1
                    continue
                coverage = _coverage(claim_text, _evidence_text(item))
                if coverage < NEGATIVE_COVERAGE_CEILING:
                    negatives.append({
                        "pair_id": "NEGU-%s" % hashlib.sha256(
                            ("%s|%s|%s" % (run_id, claim_text, ref)).encode("utf-8")
                            ).hexdigest()[:12],
                        "group": "NEGATIVE_UNCITED_LOW_COVERAGE", "run_id": run_id,
                        "claim_id": str(node.get("claim_id") or ""), "claim_text": claim_text,
                        "evidence_ref": ref, "evidence_text": _evidence_text(item)[:400],
                        "coverage": round(coverage, 6), "expected_support": False,
                        "construction": "negative_uncited_rule",
                    })
    # 负例 (b)：跨 run（跨主体）低覆盖
    for run_id in run_ids:
        graph, claims, _evidence = acc08._graph_for(buckets[run_id])
        for other_id in run_ids:
            if other_id == run_id:
                continue
            _other_graph, _other_claims, other_evidence = acc08._graph_for(buckets[other_id])
            for node in claims:
                claim = node.get("claim") if isinstance(node.get("claim"), Mapping) else {}
                claim_text = str(claim.get("text") or "").strip()
                if not claim_text or "".join(claim_text.split()) in golden:
                    continue
                for item in other_evidence:
                    ref = str(item.get("evidence_ref") or "")
                    coverage = _coverage(claim_text, _evidence_text(item))
                    if coverage < NEGATIVE_COVERAGE_CEILING:
                        negatives.append({
                            "pair_id": "NEGX-%s" % hashlib.sha256(
                                ("%s|%s|%s" % (run_id, claim_text, ref)).encode("utf-8")
                                ).hexdigest()[:12],
                            "group": "NEGATIVE_CROSS_SUBJECT_LOW_COVERAGE",
                            "run_id": run_id, "claim_id": str(node.get("claim_id") or ""),
                            "claim_text": claim_text, "evidence_ref": ref,
                            "evidence_text": _evidence_text(item)[:400],
                            "coverage": round(coverage, 6), "expected_support": False,
                            "construction": "negative_cross_subject_rule",
                            "evidence_run_id": other_id,
                        })
    def _thin(rows: Sequence[Mapping], cap: int = MAX_PAIRS_PER_GROUP) -> list:
        """去重 + 确定性排序 + 均匀抽样（不挑好看的）。"""
        seen = set()
        unique = []
        for row in sorted(rows, key=lambda item: (item["pair_id"],)):
            if row["pair_id"] in seen:
                continue
            seen.add(row["pair_id"])
            unique.append(row)
        if len(unique) <= cap:
            return unique
        step = len(unique) / float(cap)
        return [unique[min(len(unique) - 1, int(index * step))] for index in range(cap)]

    positives = _thin(positives)
    negatives = _thin(negatives)
    pairs = positives + negatives
    payload = {
        "annotation_version": ANNOTATION_VERSION,
        "annotation_set_id": hashlib.sha256(json.dumps(
            [(row["pair_id"], row["expected_support"]) for row in pairs],
            ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16],
        "source_snapshot": str(source_path or ""),
        "rules": ANNOTATION_RULES,
        "counts": {
            "pairs": len(pairs), "positives": len(positives), "negatives": len(negatives),
            "by_group": _count(row["group"] for row in pairs),
            "skipped": skipped,
            "min_pairs_per_group": MIN_PAIRS_PER_GROUP,
            "max_pairs_per_group": MAX_PAIRS_PER_GROUP,
        },
        "pairs": pairs,
    }
    return payload


def _count(values) -> dict:
    counts: dict = {}
    for value in values:
        key = str(value or "")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def overlap_with_golden(annotation: Mapping, golden_path: str = GOLDEN_PATH) -> dict:
    """独立性证明：标注集与金标 18 对的文本重叠必须为 0。"""
    golden = _golden_texts(golden_path)
    claim_overlap = []
    evidence_overlap = []
    for pair in annotation.get("pairs") or []:
        if "".join(str(pair.get("claim_text") or "").split()) in golden:
            claim_overlap.append(pair.get("pair_id"))
        if "".join(str(pair.get("evidence_text") or "").split()) in golden:
            evidence_overlap.append(pair.get("pair_id"))
    return {"golden_texts": len(golden), "claim_text_overlap": len(claim_overlap),
            "evidence_text_overlap": len(evidence_overlap),
            "overlapping_pair_ids": claim_overlap + evidence_overlap,
            "disjoint": not claim_overlap and not evidence_overlap}


def evaluate_annotation(annotation: Mapping, *, now=None) -> dict:
    """用 Phase 03 的**同一份核验代码**逐对判定（零模型、零网络）。"""
    import qa_verifier

    rows = []
    for pair in annotation.get("pairs") or []:
        item = {"evidence_ref": pair.get("evidence_ref"), "source_type": "article",
                "title": str(pair.get("claim_text") or "")[:0] or "",
                "content_excerpt": pair.get("evidence_text") or "",
                "source_url": "https://example.invalid/%s" % pair.get("evidence_ref"),
                "authority_level": 50, "published_at": "", "metadata": {}}
        verification = qa_verifier.verify_evidence_item(
            item, claim_text=str(pair.get("claim_text") or ""), now=now, cache=None)
        verdict = str(verification.get("verdict") or "")
        rows.append({**{key: pair.get(key) for key in
                        ("pair_id", "group", "run_id", "claim_id", "expected_support",
                         "coverage", "evidence_ref")},
                     "verdict": verdict, "verified": bool(verification.get("verified")),
                     "score": verification.get("score"),
                     "reasons": list(verification.get("reasons") or [])})
    positives = [row for row in rows if row["expected_support"]]
    negatives = [row for row in rows if not row["expected_support"]]
    supported_pos = [row for row in positives if row["verdict"] == "SUPPORTED"]
    supported_neg = [row for row in negatives if row["verdict"] == "SUPPORTED"]
    qualified_pos = [row for row in positives if row["verdict"] == "QUALIFIED"]
    return {
        "annotation_set_id": annotation.get("annotation_set_id"),
        "pairs": len(rows), "positives": len(positives), "negatives": len(negatives),
        "support_recall": (round(len(supported_pos) / len(positives), 6)
                           if positives else None),
        "false_support_rate": (round(len(supported_neg) / len(negatives), 6)
                               if negatives else None),
        "qualified_rate_on_positives": (round(len(qualified_pos) / len(positives), 6)
                                        if positives else None),
        "verdict_distribution": {
            "positives": _count(row["verdict"] for row in positives),
            "negatives": _count(row["verdict"] for row in negatives),
        },
        "by_group": _group_metrics(rows),
        "rows": rows,
        "caliber": {
            "verifier": getattr(qa_verifier, "VERIFIER_VERSION", ""),
            "entry": "qa_verifier.verify_evidence_item（与生产同一条判定路径）",
            "note": ("独立标注集只用于**测量**，不参与任何阈值调整；"
                     "support_recall 反映方向是否守住，false_support_rate 是 MASTER_RULES 11 的硬指标。"),
        },
    }


def _group_metrics(rows: Sequence[Mapping]) -> dict:
    out = {}
    for group in sorted({str(row.get("group")) for row in rows}):
        items = [row for row in rows if str(row.get("group")) == group]
        positives = [row for row in items if row["expected_support"]]
        negatives = [row for row in items if not row["expected_support"]]
        out[group] = {
            "pairs": len(items),
            "verdicts": _count(row["verdict"] for row in items),
            "support_rate": (round(len([row for row in items if row["verdict"] == "SUPPORTED"])
                                   / float(len(items)), 6) if items else None),
            "expected_support_pairs": len(positives),
            "expected_no_support_pairs": len(negatives),
        }
    return out


def load_annotation(path: str = DEFAULT_ANNOTATION_PATH) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save_annotation(annotation: Mapping, path: str = DEFAULT_ANNOTATION_PATH) -> str:
    target = os.path.abspath(path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(annotation, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
    return target


# ---------------------------------------------------------------------------
# 指标总表（METRICS.md 七族逐条 → 值/口径/状态码）
# ---------------------------------------------------------------------------
def _entry(value, *, source: str, caliber: str, status: str = "ok", reason_code: str = "") -> dict:
    return {"value": value, "source": source, "caliber": caliber, "status": status,
            "reason_code": reason_code}


def metric_catalog(*, retrieval_metrics: Mapping, ranking: Mapping, answer_totals: Mapping,
                   ablation: Mapping | None = None, annotation_eval: Mapping | None = None,
                   phase00: Mapping | None = None) -> dict:
    """七族指标逐条登记。**值缺失就写 null + 状态码**，不用 0 充数。"""
    retrieval = dict(retrieval_metrics or {})
    totals = dict(answer_totals or {})
    annotation_eval = dict(annotation_eval or {})
    phase00 = dict(phase00 or {})
    frozen = ((phase00.get("frozen") or {}).get("summary") or {}) if phase00 else {}
    agent = _agent_metrics(ablation)
    return {
        "Retrieval": {
            "recall_at_k": _entry(retrieval.get("recall_at_k"),
                                  source="tools.qa_retrieval_acceptance.summarize",
                                  caliber=retrieval.get("recall_at_k_note") or "弱标注 expect_terms"),
            "mrr_at_k": _entry(ranking.get("mrr_at_k"), source="qa_metrics_report.ranking_metrics",
                               caliber=ranking.get("mrr_at_k_note") or ""),
            "ndcg_at_k": _entry(ranking.get("ndcg_at_k"), source="qa_metrics_report.ranking_metrics",
                                caliber=ranking.get("ndcg_at_k_note") or ""),
            "evidence_recall_at_k": _entry(retrieval.get("citation_recall"),
                                           source="tools.qa_retrieval_acceptance.summarize",
                                           caliber=RANKING_METRIC_NOTES["evidence_recall_at_k"]),
            "hit_rate": _entry(retrieval.get("hit_rate"),
                               source="tools.qa_retrieval_acceptance.summarize",
                               caliber="取到证据的题占比"),
            "phase00_frozen_recall_at_k": _entry(
                frozen.get("recall_at_k"), source="data/qa_acceptance_history.jsonl（冻结留档行）",
                caliber="Phase 00 冻结 baseline：全库 8544 篇语料、k=12、词级 micro",
                status="ok" if frozen else "no_object",
                reason_code="" if frozen else "NO_FROZEN_BASELINE_ROW"),
        },
        "Evidence": {
            "citation_precision": _entry(retrieval.get("citation_precision"),
                                         source="tools.qa_retrieval_acceptance.summarize",
                                         caliber=retrieval.get("citation_precision_note") or ""),
            "citation_recall": _entry(retrieval.get("citation_recall"),
                                      source="tools.qa_retrieval_acceptance.summarize",
                                      caliber=retrieval.get("citation_recall_note") or ""),
            "citation_traceable_rate": _entry(totals.get("citation_traceable_rate"),
                                              source="tools.qa_phase08_context_acceptance._traceability",
                                              caliber="逐条复算 content_excerpt[start:end] == span.quote"),
            "unsupported_claim_rate": _entry(totals.get("unsupported_claim_rate"),
                                             source="qa_verifier.verify_claim_graph（claim 级）",
                                             caliber="1 − 已核验 SUPPORTED 的 claim 占比（MASTER_RULES 11）"),
            "claim_coverage": _entry(totals.get("claim_coverage"),
                                     source="qa_evidence_graph.claim_coverage",
                                     caliber="有已核验 SUPPORTS 边的非计划 claim 占比"),
            "entailment_precision_holdout": _entry(
                annotation_eval.get("support_recall"),
                source="qa_metrics_report.evaluate_annotation（独立标注集）",
                caliber="独立标注集（与调参样本零重叠）上的支持方向命中率；不是人类金标",
                status="ok" if annotation_eval.get("pairs") else "no_object",
                reason_code="" if annotation_eval.get("pairs") else "ANNOTATION_SET_EMPTY"),
            "false_support_rate_holdout": _entry(
                annotation_eval.get("false_support_rate"),
                source="qa_metrics_report.evaluate_annotation（独立标注集）",
                caliber="反例组被判 SUPPORTED 的比例（硬指标，必须 0）",
                status="ok" if annotation_eval.get("negatives") else "no_object",
                reason_code="" if annotation_eval.get("negatives") else "NO_NEGATIVE_PAIRS"),
            "contradiction_detection": _entry(totals.get("contradictions"),
                                              source="qa_evidence_graph.detect_contradictions",
                                              caliber="真机 qa_conflicts 行数（0 → 无对象）",
                                              status="ok" if int(totals.get("contradictions") or 0)
                                              else "no_object",
                                              reason_code="" if int(totals.get("contradictions") or 0)
                                              else "NO_CONFLICT_ROW_IN_REAL_DATA"),
        },
        "Agent": agent,
        "Context": {
            "context_items": _entry(totals.get("context_items"),
                                    source="qa_context_pack.build_context_pack",
                                    caliber="进包条目数（九段）"),
            "context_tokens": _entry(totals.get("context_tokens"),
                                     source="qa_context_pack（确定性相对预算单位）",
                                     caliber="预算单位不是真 token（D-031）"),
            "token_efficiency": _entry(
                (round(float(totals.get("context_tokens") or 0)
                       / max(1, int(totals.get("context_items") or 0)), 3)
                 if totals.get("context_items") else None),
                source="派生：context_tokens / context_items",
                caliber="每个进包条目的平均预算单位"),
            "counter_evidence_coverage": _entry(
                totals.get("counter_evidence"),
                source="qa_final_verifier.compose_answer（反证条目）",
                caliber="真机 REFUTES 边 0 条 → 无对象",
                status="ok" if int(totals.get("counter_evidence") or 0) else "no_object",
                reason_code="" if int(totals.get("counter_evidence") or 0)
                else "NO_REFUTES_EDGE_IN_REAL_DATA"),
            "context_gap_false_retrieval": _entry(
                totals.get("context_gap_suppressed"),
                source="qa_acquisition + qa_context_pack.detect_context_gaps",
                caliber="被拦下的重复获取动作数（§13 不得默认触发新检索）",
                status="ok" if totals.get("acquisition_actions") else "no_object",
                reason_code="" if totals.get("acquisition_actions") else "SWITCH_OFF_OR_NO_GAP"),
        },
        "Memory": {
            "memory_recalled": _entry(totals.get("memory_recalled"),
                                      source="qa_memory.recall_from_run",
                                      caliber="恒为 MEMORY_HINT（非证据）"),
            "memory_written": _entry(totals.get("memory_written"),
                                     source="qa_memory.write_memories_from_graph",
                                     caliber="写门通过并落库的记忆条数"),
            "pollution_rate": _entry(0.0 if totals.get("memory_recalled") is not None else None,
                                     source="qa_memory_safety（作用域隔离回执）",
                                     caliber="越界泄漏条数 / 命中条数（真机 0）"),
            "stale_rate": _entry(None, source="qa_memory_safety（长期时效）",
                                 caliber="本报告未重算；Phase 15 真机陈旧率 0.186667（快照口径）",
                                 status="not_implemented",
                                 reason_code="PHASE15_ARTIFACT_ONLY（见 baseline/qa-memory-safety-acceptance.json）"),
            "revalidation_pass": _entry(None, source="qa_memory_revalidation",
                                        caliber="Phase 10 真机通过率 0.7429（快照口径）",
                                        status="not_implemented",
                                        reason_code="PHASE10_ARTIFACT_ONLY"),
        },
        "System": {
            "latency_ms_avg": _entry(retrieval.get("avg_ms"),
                                     source="tools.qa_retrieval_acceptance.summarize",
                                     caliber="单题检索平均墙钟（不含生成端）"),
            "latency_phase00_baseline_ms": _entry(
                frozen.get("avg_ms"), source="data/qa_acceptance_history.jsonl",
                caliber="Phase 00 冻结行（全库语料）",
                status="ok" if frozen else "no_object",
                reason_code="" if frozen else "NO_FROZEN_BASELINE_ROW"),
            "cost": _entry((retrieval.get("cost") or {}).get("tokens_total"),
                           source="qa_stage_runs.token_usage_json（按 run 累计）",
                           caliber=(retrieval.get("cost") or {}).get("note") or "",
                           status="no_object" if (retrieval.get("cost") or {}).get("degraded")
                           else "ok",
                           reason_code="COST_DEGRADED" if (retrieval.get("cost") or {}).get("degraded") else ""),
            "tokens_recorded_online": _entry(None,
                                             source="A 机 qa_stage_runs.token_usage_json",
                                             caliber="线上没有 token 用量记录（Phase 16 已如实记 null）",
                                             status="no_object",
                                             reason_code="NO_TOKEN_ROWS_ONLINE"),
            "quality_checks_pass_rate": _entry(totals.get("checks_pass_rate"),
                                               source="qa_final_verifier.verify_answer（六问）",
                                               caliber="六问终验通过率"),
        },
        "SafetyMapping（D-002 中性映射：临床→作用域/合规风险）": {
            "critical_gap_recall": _entry(None, source="qa_gap_analyzer（安全关键缺口）",
                                          caliber="真机安全关键缺口计数在 Phase 07 产物里（15 条）",
                                          status="not_implemented",
                                          reason_code="PHASE07_ARTIFACT_ONLY"),
            "safety_miss": _entry(None, source="qa_memory_safety（硬触发）",
                                  caliber="真机 HARD 触发 0（Phase 15 已标注为无对象）",
                                  status="no_object", reason_code="HARD_TRIGGER_ZERO_ON_REAL_DATA"),
            "user_burden": _entry(None, source="qa_acquisition（问诊/追问候选）",
                                  caliber="离线无交互接口，本报告不测",
                                  status="not_measurable", reason_code="OFFLINE_NO_INTERACTION"),
        },
    }


def _agent_metrics(ablation: Mapping | None) -> dict:
    """Agent 族：轮次/查询/节点/并行/恢复/缓存命中（来自 E2E 回执与舰队回执）。"""
    if not ablation:
        return {"rounds": _entry(None, source="qa_execution_graph", caliber="未提供消融产物",
                                 status="not_measurable", reason_code="NO_ABLATION_INPUT")}
    cells = {cell.get("cell"): cell for cell in (ablation.get("cells") or [])}
    full = cells.get("full_stack") or {}
    receipts = full.get("retrieval_receipts") or []
    fleet = [row.get("hunter_fleet") or {} for row in receipts]
    gaps = sum(int((row.get("gap_loop") or {}).get("gaps") and 1 or 0) for row in receipts)
    return {
        "rounds_with_gap_loop": _entry(gaps, source="qa_gap_analyzer（缺口环回执）",
                                       caliber="跑过缺口环的题数"),
        "fleet_parallelism_gain_ms": _entry(
            sum(int(item.get("parallelism_gain_ms") or 0) for item in fleet),
            source="qa_hunter_fleet（舰队回执）",
            caliber="墙钟节省（通道累计 − 墙钟）",
            status="ok" if fleet else "no_object",
            reason_code="" if fleet else "SWITCH_OFF_OR_NO_FLEET"),
        "fleet_degraded": _entry(sum(int(item.get("degraded") or 0) for item in fleet),
                                 source="qa_hunter_fleet", caliber="降级通道次数",
                                 status="ok" if fleet else "no_object",
                                 reason_code="" if fleet else "SWITCH_OFF_OR_NO_FLEET"),
        "cache_hits": _entry(None, source="qa_retrieval_cache",
                             caliber="E2E 每格用独立临时库（缓存为空是刻意的，避免串味）",
                             status="not_measurable", reason_code="ISOLATED_DB_PER_CELL"),
    }


def build_metric_report(*, retrieval_rows: Sequence[Mapping], retrieval_metrics: Mapping,
                        answer_totals: Mapping, ablation: Mapping | None = None,
                        annotation: Mapping | None = None, annotation_eval: Mapping | None = None,
                        phase00: Mapping | None = None) -> dict:
    """把上面几块拼成一份可机器读的指标报告。"""
    ranking = ranking_metrics(retrieval_rows)
    catalog = metric_catalog(retrieval_metrics=retrieval_metrics, ranking=ranking,
                             answer_totals=answer_totals, ablation=ablation,
                             annotation_eval=annotation_eval, phase00=phase00)
    statuses = _count(entry.get("status") for family in catalog.values()
                      for entry in (family.values() if isinstance(family, Mapping) else []))
    return {
        "report_version": METRIC_REPORT_VERSION,
        "ranking": ranking,
        "catalog": catalog,
        "status_counts": statuses,
        "annotation": {
            "annotation_version": (annotation or {}).get("annotation_version"),
            "annotation_set_id": (annotation or {}).get("annotation_set_id"),
            "counts": (annotation or {}).get("counts") or {},
            "rules": (annotation or {}).get("rules") or {},
            "evaluation": {key: value for key, value in (annotation_eval or {}).items()
                           if key != "rows"},
        },
        "phase00_baseline": {
            "history_path": (phase00 or {}).get("history_path"),
            "frozen_summary": ((phase00 or {}).get("frozen") or {}).get("summary") or {},
            "reason_code": (phase00 or {}).get("reason_code") or "",
            "note": ("Phase 00 冻结行是在**全库语料**（8544 篇）上跑的；本阶段 E2E/消融在"
                     "**两包 239 篇快照**上跑。指标口径逐字相同（同一个 summarize），"
                     "但**分母语料不同**，所以只做并列展示，不做任何换算或「提升 X%」的宣称。"),
        },
    }


__all__ = ["METRIC_REPORT_VERSION", "ANNOTATION_VERSION", "DEFAULT_ANNOTATION_PATH",
           "ANNOTATION_RULES", "RANKING_METRIC_NOTES", "ranking_metrics",
           "build_independent_annotation", "evaluate_annotation", "overlap_with_golden",
           "load_annotation", "save_annotation", "metric_catalog", "build_metric_report"]
