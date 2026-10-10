#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 03（Verifier Layer）验收：**核验口径的可复算指标**（纯 CPU 规则，不调任何模型/端点）。

为什么要有这个工具：Phase 00 的 P00-04 把 Quality（unsupported claim rate / entailment）
登记为缺口并注明"属阶段 03"。这个工具就是那个缺口的交付物：它把核验层的判定质量算成
可复算的数字，before/after 直接比大小即可。

三组指标（全部离线、无网络、无模型）：
  1. **金标小样本**（`config/qa_verifier_golden.json`，人工标注 16 对）：
       · support_recall     证据确实充分时判 SUPPORTED 的比例
       · support_precision  判 SUPPORTED 的里面证据确实充分的比例
       · false_support_rate **证据不充分却被判 SUPPORTED** 的比例（MASTER_RULES 第 11 条的硬指标，必须 0）
       · contradiction_recall / false_refute_rate  反证召回与误判反证率
     金标只被本工具读取；核验实现（qa_verifier.py）**不读它**，所以不构成"硬编码答案"。
  2. **真机样本回放**（`baseline/qa-verifier-real-sample.json`，A 机只读导出的一个真实 run）：
     把真实 claim 与真实证据按 `conflict_review` 的口径跑一遍 `verify_claim_graph`，
     输出 claim 状态分布、unsupported_claim_rate、引用悬空数、耗时。
  3. **缓存与性能**：同批证据跑两遍的 cache 命中率、单条核验平均耗时（毫秒）。

用法
    python tools/qa_verifier_acceptance.py                     # 打印摘要
    python tools/qa_verifier_acceptance.py --json              # 机器可读
    python tools/qa_verifier_acceptance.py --json --out baseline/qa-verifier-acceptance.json
    python tools/qa_verifier_acceptance.py --history data/qa_verifier_history.jsonl

退出码：0 = 硬指标达标（false_support_rate/ false_refute_rate 为 0）；
        1 = 有硬指标不达标（真实运行结果，不隐藏）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("INTEL_LLM_ENABLED", "false")
os.environ.setdefault("INTEL_EMBEDDING_ENABLED", "false")

import qa_verifier as verifier  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN = os.path.join(ROOT, "config", "qa_verifier_golden.json")
REAL_SAMPLE = os.path.join(ROOT, "baseline", "qa-verifier-real-sample.json")
HISTORY_DEFAULT = os.path.join(ROOT, "data", "qa_verifier_history.jsonl")

# 硬门槛（不达标即视为验收失败，绝不静默调低）
HARD_ZERO_METRICS = ("false_support_rate", "false_refute_rate")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _rate(hit: int, total: int):
    """分母为 0 时返回 None（**绝不用 0 冒充**：没有样本不等于指标为 0）。"""
    return None if not total else round(hit / total, 4)


def _item(pair: dict) -> dict:
    return {
        "evidence_ref": "golden:%s" % pair["id"],
        "source_type": "article",
        "title": pair.get("title") or "",
        "source_url": "https://example.invalid/%s" % pair["id"],
        "article_id": 1,
        "content_excerpt": pair.get("evidence_text") or "",
        "published_at": pair.get("published_at") or "",
        "authority_level": pair.get("authority_level"),
        "score": 30.0,
        "retrieval_method": "keyword",
        "relationship": "supports",
        "metadata": {"doc_type": pair["doc_type"]} if pair.get("doc_type") else {},
    }


def _loader(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def golden_metrics(*, cache: verifier.VerificationCache | None = None) -> dict:
    """金标小样本：把每对 (claim, 证据) 跑一遍核验，算支持/反证的精确率与召回率。"""
    payload = _loader(GOLDEN)
    pairs = payload.get("pairs") or []
    cache = cache or verifier.VerificationCache()
    rows, started = [], time.perf_counter()
    for pair in pairs:
        result = verifier.verify_evidence_item(
            _item(pair), claim_text=str(pair.get("claim") or ""),
            required_entities=pair.get("required_entities") or (),
            valid_from=str(pair.get("valid_from") or ""), cache=cache)
        rows.append({
            "id": pair["id"], "axis": pair.get("axis"),
            "sufficient": bool(pair.get("sufficient")),
            "contradiction": bool(pair.get("contradiction")),
            "verdict": result["verdict"], "score": result["score"],
            "entailment": (result.get("dimensions") or {}).get("entailment"),
            "reasons": result["reasons"],
        })
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    sufficient = [row for row in rows if row["sufficient"]]
    insufficient = [row for row in rows if not row["sufficient"]]
    contradictions = [row for row in rows if row["contradiction"]]
    non_contradictions = [row for row in rows if not row["contradiction"]]
    predicted_supported = [row for row in rows if row["verdict"] == "SUPPORTED"]
    predicted_refuted = [row for row in rows if row["verdict"] == "REFUTED"]
    qualified = [row for row in rows if row["verdict"] == "QUALIFIED"]

    metrics = {
        "pairs": len(rows),
        "support_recall": _rate(sum(1 for row in sufficient if row["verdict"] == "SUPPORTED"),
                                len(sufficient)),
        "support_precision": _rate(sum(1 for row in predicted_supported if row["sufficient"]),
                                   len(predicted_supported)),
        "partial_support_rate": _rate(sum(1 for row in sufficient if row["verdict"] == "QUALIFIED"),
                                      len(sufficient)),
        "false_support_rate": _rate(sum(1 for row in insufficient if row["verdict"] == "SUPPORTED"),
                                    len(insufficient)),
        "contradiction_recall": _rate(sum(1 for row in contradictions if row["verdict"] == "REFUTED"),
                                      len(contradictions)),
        "false_refute_rate": _rate(sum(1 for row in predicted_refuted if not row["contradiction"]),
                                   len(non_contradictions)),
        "unresolved_rate": _rate(sum(1 for row in rows if row["verdict"] == "UNVERIFIED"), len(rows)),
        "qualified_share": _rate(len(qualified), len(rows)),
        "avg_ms_per_pair": round(elapsed_ms / max(1, len(rows)), 3),
    }
    return {"benchmark_version": payload.get("benchmark_version"), "metrics": metrics,
            "verdicts": {verdict: sum(1 for row in rows if row["verdict"] == verdict)
                         for verdict in verifier.VERDICTS},
            "rows": rows}


def real_sample_metrics(*, cache: verifier.VerificationCache | None = None) -> dict:
    """真机样本回放：A 机只读导出的一个真实 run，按 conflict_review 的口径跑 claim 级核验。"""
    if not os.path.exists(REAL_SAMPLE):
        return {"available": False, "reason": "缺少 %s（A 机只读导出的样本）" % REAL_SAMPLE}
    payload = _loader(REAL_SAMPLE)
    cache = cache or verifier.VerificationCache()
    evidence = []
    for index, row in enumerate(payload.get("evidence") or []):
        evidence.append({
            "evidence_ref": row.get("evidence_ref"),
            "source_type": row.get("source_type") or "article",
            "title": row.get("title") or "",
            "source_url": row.get("source_url") or "",
            "article_id": None,
            "content_excerpt": row.get("content_excerpt") or "",
            "published_at": row.get("published_at") or "",
            "authority_level": row.get("authority_level"),
            "score": 30.0,
            "retrieval_method": row.get("retrieval_method") or "keyword",
            "relationship": row.get("relationship") or "",
            "metadata": {},
        })
    claims = []
    for index, row in enumerate(payload.get("claims") or []):
        claims.append({
            "canonical_id": row.get("claim_key") or "c%d" % index,
            "claim": {
                "claim_id": row.get("claim_key") or "c%d" % index,
                "text": row.get("text") or "",
                "claim_type": row.get("claim_type") or "current_fact",
                "confidence": float(row.get("confidence") or 0),
                "valid_from": None, "valid_to": None, "scope": [],
                "evidence_refs": list(row.get("evidence_refs") or []),
                "needs_verification": True,
                # 真机数据里这个字段是**模型自己填的**（实测全是 qualified）：回放的目的
                # 就是看规则核验会把它改成什么。
                "verification_status": row.get("verification_status") or "unverified",
            },
        })
    graph = {"version": "qa-adjudication-v1", "claims": claims, "evidence": evidence,
             "edges": [], "conflicts": []}
    started = time.perf_counter()
    summary = verifier.verify_claim_graph(graph, cache=cache)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    model_statuses = {}
    for node in claims:
        # 回放前模型自评（原样保留，便于对比"规则核验把自评改成了什么"）
        original = str(node["claim"].get("verification_status") or "")
        model_statuses[original] = model_statuses.get(original, 0) + 1
    return {
        "available": True,
        "fixture": payload.get("fixture_version"),
        "captured_at_utc": payload.get("captured_at_utc"),
        "run": payload.get("run"),
        "evidence_count": len(evidence),
        "claim_count": len(claims),
        "model_self_assessment": model_statuses,
        "stats": summary.get("stats"),
        "elapsed_ms": round(elapsed_ms, 2),
        "claims": summary.get("claims"),
    }


def cache_metrics() -> dict:
    """缓存：同一批证据跑两遍，第二遍必须命中；顺带记单条耗时。"""
    if not os.path.exists(REAL_SAMPLE):
        return {"available": False}
    payload = _loader(REAL_SAMPLE)
    evidence = []
    for row in payload.get("evidence") or []:
        evidence.append({
            "evidence_ref": row.get("evidence_ref"), "source_type": "article",
            "title": row.get("title") or "", "source_url": row.get("source_url") or "",
            "content_excerpt": row.get("content_excerpt") or "",
            "published_at": row.get("published_at") or "",
            "authority_level": row.get("authority_level"), "score": 30.0,
            "retrieval_method": "keyword", "relationship": "supports", "metadata": {},
        })
    claim = str((payload.get("run") or {}).get("question_text") or "")
    cache = verifier.VerificationCache()
    started = time.perf_counter()
    verifier.verify_evidence_batch(evidence, claim_text=claim, cache=cache)
    first_ms = (time.perf_counter() - started) * 1000.0
    started = time.perf_counter()
    verifier.verify_evidence_batch(evidence, claim_text=claim, cache=cache)
    second_ms = (time.perf_counter() - started) * 1000.0
    stats = cache.stats()
    return {
        "available": True,
        "items": len(evidence),
        "first_pass_ms": round(first_ms, 2),
        "second_pass_ms": round(second_ms, 2),
        "speedup": round(first_ms / second_ms, 2) if second_ms else None,
        "cache": stats,
        "hit_rate": _rate(stats.get("hits", 0), stats.get("hits", 0) + stats.get("misses", 0)),
    }


def _thresholds(metrics: dict) -> dict:
    verdict = {}
    for name in HARD_ZERO_METRICS:
        value = metrics.get(name)
        verdict[name] = {"value": value, "required": 0.0,
                         "passed": value == 0.0 if value is not None else None}
    recall = metrics.get("support_recall")
    verdict["support_recall_floor"] = {"value": recall, "required": 0.6,
                                       "passed": recall is not None and recall >= 0.6}
    return verdict


def build_report() -> dict:
    cache = verifier.VerificationCache()
    golden = golden_metrics(cache=cache)
    thresholds = _thresholds(golden["metrics"])
    hard_ok = all(item["passed"] is not False for item in thresholds.values())
    return {
        "tool": "qa_verifier_acceptance",
        "report_version": "qa-verifier-acceptance-v1",
        "generated_at_utc": _utc_now(),
        "verifier_version": verifier.VERIFIER_VERSION,
        "config_hash": verifier.config_hash(),
        "weights": verifier.score_weights(),
        "thresholds": {"support_min": verifier.support_min(),
                       "partial_min": verifier.partial_min(),
                       "relevance_floor": verifier.relevance_floor(),
                       "gate": verifier.gate_mode()},
        "golden": golden,
        "thresholds_verdict": thresholds,
        "real_sample": real_sample_metrics(cache=cache),
        "cache": cache_metrics(),
        "acceptance": {"passed": bool(hard_ok),
                       "note": "硬指标（误判支持率 / 误判反证率）必须为 0；support_recall 是软下限"},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 03 核验层验收（纯 CPU，不调模型）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--out", default="", help="把 JSON 报告写到文件")
    parser.add_argument("--history", default="", help="追加一行到历史留档（默认不写）")
    args = parser.parse_args()

    report = build_report()
    text = json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    if args.history:
        os.makedirs(os.path.dirname(os.path.abspath(args.history)), exist_ok=True)
        with open(args.history, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "generated_at_utc": report["generated_at_utc"],
                "verifier_version": report["verifier_version"],
                "config_hash": report["config_hash"],
                "golden": report["golden"]["metrics"],
                "real_sample": {key: report["real_sample"].get(key)
                                for key in ("claim_count", "evidence_count", "stats", "elapsed_ms")},
                "cache": {key: report["cache"].get(key)
                          for key in ("first_pass_ms", "second_pass_ms", "hit_rate")},
                "passed": report["acceptance"]["passed"],
            }, ensure_ascii=False) + "\n")

    if args.json:
        print(text)
    else:
        metrics = report["golden"]["metrics"]
        print("核验层验收（%s / 配置 %s）" % (report["verifier_version"], report["config_hash"]))
        print("  金标 %s 对：support_recall=%s support_precision=%s false_support_rate=%s"
              % (metrics["pairs"], metrics["support_recall"], metrics["support_precision"],
                 metrics["false_support_rate"]))
        print("  反证：contradiction_recall=%s false_refute_rate=%s；未核验占比=%s；单条 %.3f ms"
              % (metrics["contradiction_recall"], metrics["false_refute_rate"],
                 metrics["unresolved_rate"], metrics["avg_ms_per_pair"]))
        print("  判定分布：%s" % report["golden"]["verdicts"])
        real = report["real_sample"]
        if real.get("available"):
            print("  真机样本（run %s / %s）：证据 %s 条、结论 %s 条 → %s"
                  % ((real.get("run") or {}).get("id"), (real.get("run") or {}).get("industry_pack_id"),
                     real["evidence_count"], real["claim_count"], real["stats"]))
            print("  模型自评（回放前）：%s" % real["model_self_assessment"])
        else:
            print("  真机样本：不可用（%s）" % real.get("reason"))
        cache = report["cache"]
        if cache.get("available"):
            print("  缓存：%s 条第一遍 %.1f ms → 第二遍 %.1f ms（%sx，命中率 %s）"
                  % (cache["items"], cache["first_pass_ms"], cache["second_pass_ms"],
                     cache["speedup"], cache["hit_rate"]))
        print("  硬指标：%s" % json.dumps(report["thresholds_verdict"], ensure_ascii=False))
        print("  验收结论：%s" % ("PASS" if report["acceptance"]["passed"] else "FAIL"))
    return 0 if report["acceptance"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
