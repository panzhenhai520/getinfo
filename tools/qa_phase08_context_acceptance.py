#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 08（P08-01…P08-06 + 生成端 grounding）验收：**真机数据上的前后对比**（纯规则，零模型调用）。

它回答四个问题，全部可逐项复算：
  1. **token 预算裁剪的前后对比**：把 A 机真实 run 的证据/结论装配成候选上下文池，
     在**未裁剪**（before）与**裁剪后**（after）之间比较 token 估算、被裁条目数与**裁剪原因分布**；
  2. **引用可回溯率**：包内每一条引用能不能指回 Phase 02 的 `evidence_ref` + 最小 span
     （并复算 `content_excerpt[start:end] == span.quote`）——这是 MASTER_RULES 第 11 条的证据；
  3. **Context Gap**：缺口类型/动作分布，以及"**触发新检索的条数必须为 0**"（§6 + MASTER_RULES 13）；
  4. **生成端 grounding 真拦截率**：拿 A 机 14 条**真实最终答案**跑 `check_grounding()`，
     数出"有多少条答案含阻断级违规 / 违规码分布 / 标注后状态是否降档"。

口径与边界（宁写 PARTIAL 不谎报）：
  · 数据来源 = `--snapshot` 指定的 JSON（A 机**只读**导出，里面带 `source.access`）；本工具不联网、不连库；
  · 真机 run 早于本阶段接线，所以它们的**上下文包是离线装配的**（用同一份代码、同一份真实证据），
     不是线上跑出来的；线上行为由 `tests/test_qa_phase08_pipeline.py` 端到端覆盖；
  · token 是**确定性估算的相对预算单位**（本机硬约束禁止调任何模型/嵌入端点，拿不到 tokenizer），
     所以"before/after"比的是同一把尺子下的相对量，不是真实 token 数；
  · A 机的 `qa_runs.final_answer_json` 是真实产物，grounding 拦截率就建立在它上面（不是构造数据）。
    没有最终答案的 run 计入 `runs_without_grounding`，不进分母。

用法：
    python tools/qa_phase08_context_acceptance.py --json
    python tools/qa_phase08_context_acceptance.py --out baseline/qa-context-acceptance.json
    python tools/qa_phase08_context_acceptance.py --snapshot <新快照> --budget 3000 --reserve 0.2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Mapping

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_context_pack as cp  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402

REPORT_VERSION = "qa-context-acceptance-v1"
DEFAULT_SNAPSHOT = os.path.join("baseline", "qa-context-real-sample.json")
FALLBACK_SNAPSHOT = os.path.join("baseline", "qa-gap-real-sample.json")
DEFAULT_OUTPUT = os.path.join("baseline", "qa-context-acceptance.json")
DEFAULT_BUDGET = cp.total_token_budget()   # 产品默认预算（环境变量可覆盖）——真机数据在这个预算下装得下
DEFAULT_RESERVE = cp.counter_reserve_ratio()
LADDER = (600, 1200, 3000, 6000, 12000)   # 预算梯子：看"裁多少/为什么裁"随预算怎么变


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _counter(values) -> dict:
    counts: dict = {}
    for value in values:
        key = str(value or "")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _rows_by_run(snapshot: dict) -> dict:
    fields = ("claims", "edges", "conflicts", "evidence")
    grouped: dict = {}
    for key in fields:
        for row in snapshot.get(key) or []:
            run_id = str(row.get("run_id") or "")
            if not run_id:
                continue
            grouped.setdefault(run_id, {name: [] for name in fields})
            grouped[run_id][key].append(row)
    runs = {str(item.get("id") or ""): item for item in snapshot.get("runs") or []}
    for run_id, bucket in grouped.items():
        bucket["run"] = runs.get(run_id) or {}
    for run_id, run in runs.items():
        if run_id and run_id not in grouped:
            grouped[run_id] = {**{name: [] for name in fields}, "run": run}
    return grouped


def _claim_graph_row(row: dict) -> dict:
    """把 A 机的 claim 行还原成 Phase 06 图里的 claim 节点（只读既有 payload）。"""
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    node = payload.get("claim") if isinstance(payload.get("claim"), dict) else {}
    claim_id = str(payload.get("claim_id") or node.get("claim_id")
                   or row.get("claim_key") or "")
    text = str(node.get("text") or payload.get("text") or row.get("claim_text") or "")
    if not claim_id or not text:
        return {}
    claim = {
        "claim_id": claim_id, "text": text,
        "claim_type": str(node.get("claim_type") or payload.get("claim_type") or "current_fact"),
        "confidence": node.get("confidence", payload.get("confidence", 0.6)),
        "valid_from": node.get("valid_from"), "valid_to": node.get("valid_to"),
        "scope": list(node.get("scope") or payload.get("scope") or []),
        "evidence_refs": list(node.get("evidence_refs") or payload.get("evidence_refs") or []),
        "needs_verification": bool(node.get("needs_verification", True)),
        "verification_status": str(row.get("verification_status")
                                   or payload.get("verification_status") or "unverified"),
    }
    return {"claim_id": claim_id, "canonical_id": claim_id, "claim": claim, "text": text,
            "verification_status": claim["verification_status"],
            "plan_only": bool(payload.get("plan_only"))}


def _evidence_object(row: dict) -> dict:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    item = dict(payload)
    item.setdefault("evidence_ref", row.get("evidence_ref"))
    item.setdefault("source_type", row.get("source_type"))
    item.setdefault("source_url", row.get("source_url"))
    item.setdefault("title", row.get("source_title") or "")
    item.setdefault("published_at", row.get("published_at"))
    if item.get("authority_level") in (None, ""):
        item["authority_level"] = row.get("authority_level")
    item.setdefault("metadata", {})
    if not isinstance(item.get("metadata"), dict):
        item["metadata"] = {}
    return item if item.get("evidence_ref") and item.get("content_excerpt") else {}


def _edge_row(row: dict) -> dict:
    relation = str(row.get("relationship") or "").upper() or "SUPPORTS"
    if relation not in ("SUPPORTS", "REFUTES", "MENTIONS", "CONTEXT", "CONTRADICTS"):
        relation = "SUPPORTS"
    return {
        "edge_id": "edge:%s:%s:%s" % (row.get("run_id"), row.get("claim_key"),
                                      row.get("evidence_ref")),
        "kind": "claim-evidence", "src": "claim:%s" % row.get("claim_key"),
        "dst": "evidence:%s" % row.get("evidence_ref"),
        "graph_relation": relation, "status": relation,
        "strength": 0.7, "claim_id": str(row.get("claim_key") or ""),
        "evidence_ref": str(row.get("evidence_ref") or ""),
        "relationship": relation.casefold(),
        "relevance_score": row.get("relevance_score") or 0.0,
        "published_at": row.get("published_at") or "", "scope": [],
        "metadata": {"verified": False, "authority_level": None, "reasons": []},
    }


def _graph_for(bucket: dict) -> tuple:
    claims = [item for item in (_claim_graph_row(row) for row in bucket["claims"]) if item]
    evidence = [item for item in (_evidence_object(row) for row in bucket["evidence"]) if item]
    seen = set()
    edges = []
    for row in bucket["edges"]:
        edge = _edge_row(row)
        key = (edge["claim_id"], edge["evidence_ref"])
        if not all(key) or key in seen:
            continue
        seen.add(key)
        edges.append(edge)
    return {"version": "qa-claim-graph-v1", "claims": claims, "evidence": evidence,
            "edges": edges, "conflicts": list(bucket["conflicts"]), "stats": {}}, claims, evidence


def _verify_evidence(graph: dict) -> dict:
    """离线补跑 Phase 03 的规则核验（真机 run 早于核验层）；纯规则、零端点。"""
    try:
        from qa_verifier import verify_evidence_batch
    except Exception as exc:      # noqa: BLE001
        return {"verified": 0, "error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}
    claims = [node.get("claim") or node for node in graph.get("claims") or []]
    items = [dict(item) for item in graph.get("evidence") or []]
    try:
        annotated, audit = verify_evidence_batch(items, claims=claims)
    except Exception as exc:      # noqa: BLE001
        return {"verified": 0, "error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}
    return {"verified": len(annotated or []), "audit": audit if isinstance(audit, dict) else {},
            "evidence": annotated or []}


def _traceability(pack: dict, graph: dict) -> dict:
    """引用可回溯率：包内引用 vs 原始证据正文上的 span 复算。"""
    by_ref = {str(item.get("evidence_ref")): item for item in graph.get("evidence") or []}
    checked = failed = 0
    failures = []
    for label, entry in (pack.get("citation_index") or {}).items():
        ref = str(entry.get("evidence_ref") or "")
        span = entry.get("span") or {}
        quote = str(span.get("quote") or "")
        source = str((by_ref.get(ref) or {}).get("content_excerpt") or "")
        checked += 1
        if not entry.get("grounded") or not quote or not str(span.get("source") or ""):
            failed += 1
            failures.append({"label": label, "evidence_ref": ref, "why": "缺 span 或未标注来源"})
            continue
        if source[int(span.get("start") or 0):int(span.get("end") or 0)] != quote:
            failed += 1
            failures.append({"label": label, "evidence_ref": ref, "why": "偏移对不上原文"})
            continue
        if not str(entry.get("evidence_id") or ""):
            failed += 1
            failures.append({"label": label, "evidence_ref": ref, "why": "缺 Phase 02 证据指纹"})
    return {"checked": checked, "traceable": checked - failed, "failed": failed,
            "failures": failures[:10],
            "traceable_rate": round((checked - failed) / float(checked), 6) if checked else None}


def _grounding_for(run: dict, pack: dict, graph: dict) -> dict:
    """拿**真机真实最终答案**跑生成端 grounding 校验（只读，不改答案）。"""
    answer = run.get("final_answer")
    if not isinstance(answer, dict) or not str(answer.get("answer") or ""):
        return {}
    extra = []
    pack_refs = {str(ref) for ref in (pack.get("citation_map") or {}).values()}
    for item in graph.get("evidence") or []:
        ref = str(item.get("evidence_ref") or "")
        if ref and ref not in pack_refs:
            extra.append(item)
    report = cp.check_grounding(answer, pack=pack, extra_evidence=extra)
    marked = cp.mark_ungrounded_answer(answer, report, pack=pack) if report.get("violations") else {}
    # 严格口径（数字必须在**被引用的最小 span** 里）之外，再给一个宽松对照：
    # 数字在该条证据的**完整正文**里能不能找到（真机答案是旧链路生成的，模型当时看的是整段摘录）。
    cited = {ref for claim in (answer.get("claims") or []) if isinstance(claim, Mapping)
             for ref in (claim.get("evidence_refs") or [])}
    full_text = " ".join(str((item or {}).get("content_excerpt") or "")
                         for ref, item in ((str(row.get("evidence_ref")), row)
                                           for row in (graph.get("evidence") or []))
                         if ref in cited)
    strict = [item for item in (report.get("violations") or [])
              if item.get("code") == "NUMBER_WITHOUT_SPAN"]
    loose = [item for item in strict if str(item.get("number") or "") not in
             cp._numbers(full_text) and str(item.get("number") or "") not in full_text]
    return {
        "blocking": bool(report.get("blocking")),
        "violation_counts": dict(report.get("violation_counts") or {}),
        "blocking_codes": list(report.get("blocking_codes") or []),
        "warning_codes": list(report.get("warning_codes") or []),
        "checked_claims": int(report.get("checked_claims") or 0),
        "ungrounded_claims": int(report.get("ungrounded_claims") or 0),
        "status_before": str(answer.get("status") or ""),
        "status_after": str(marked.get("status") or answer.get("status") or ""),
        "marked": bool(marked),
        "number_strict": len(strict),
        "number_not_in_full_excerpt": len(loose),
    }


def _injection_check(answer: dict, pack: dict, graph: dict) -> list:
    """**构造注入**的拦截实测（明确标注：这不是真机数据，是对真实答案形状的变异）。

    真机 13 条答案在严格口径下**没有阻断级违规**，所以"阻断拦截率"在真机上没有分母。
    为了给出可复算的拦截率，这里对每条真实答案注入三种典型"无证据断言"，看闸门能不能拦住：
      A. 抹掉 claim 的证据绑定（模型自由生成的断言）；
      B. 把 claim 的证据换成包外引用（指到没有进上下文的东西）；
      C. 在正文里注入一个任何被引用 span 里都没有的数字。
    """
    import copy

    pack_refs = {str(ref) for ref in (pack.get("citation_map") or {}).values()}
    out_ref = next((str(item.get("evidence_ref")) for item in (graph.get("evidence") or [])
                    if str(item.get("evidence_ref") or "") not in pack_refs), "article:404")
    variants = []

    def _run(name, mutate):
        payload = copy.deepcopy(answer)
        mutate(payload)
        report = cp.check_grounding(payload, pack=pack)
        marked = cp.mark_ungrounded_answer(payload, report, pack=pack)
        changed = bool(
            cp.UNGROUNDED_MARK in str(marked.get("answer") or "")
            or str(marked.get("status") or "") != str(payload.get("status") or "")
            or list(marked.get("degradation_reasons") or []) !=
            list(payload.get("degradation_reasons") or []))
        variants.append({
            "variant": name,
            "detected": bool(report.get("violations")),
            "blocking": bool(report.get("blocking")),
            "codes": sorted(set(report.get("violation_counts") or {})),
            "status_before": str(payload.get("status") or ""),
            "status_after": str(marked.get("status") or ""),
            "marked": changed,
        })

    def _strip_evidence(payload):
        for claim in payload.get("claims") or []:
            if isinstance(claim, dict):
                claim["evidence_refs"] = []

    def _swap_ref(payload):
        claims = [claim for claim in (payload.get("claims") or []) if isinstance(claim, dict)]
        if claims:
            claims[0]["evidence_refs"] = [out_ref]
        else:
            payload["claims"] = [{"claim_id": "inj", "text": "注入断言",
                                  "evidence_refs": [out_ref]}]

    def _inject_number(payload):
        payload["answer"] = str(payload.get("answer") or "") + " 另有 987654 万港元的缺口。"

    _run("A_no_evidence_binding", _strip_evidence)
    _run("B_out_of_pack_ref", _swap_ref)
    _run("C_number_outside_span", _inject_number)
    return variants


def _run_once(*, graph: dict, run: dict, budget: int, reserve: float, question: str,
              injections: bool = False) -> dict:
    pack = cp.build_context_pack(
        graph=graph, plan={"question": question},
        request={"question": question, "mode": str(run.get("mode") or "")},
        run_id=str(run.get("id") or ""), budget_tokens=budget, reserve_ratio=reserve)
    receipt = cp.context_pack_receipt(pack)
    answer = run.get("final_answer")
    injections_out = []
    if injections and isinstance(answer, dict) and str(answer.get("answer") or ""):
        injections_out = _injection_check(answer, pack, graph)
    return {"pack": pack, "receipt": receipt, "traceability": _traceability(pack, graph),
            "grounding": _grounding_for(run, pack, graph),
            "injections": injections_out,
            "summary": cp.selection_summary(pack.get("selection_trace") or [])}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", default="")
    parser.add_argument("--out", default=DEFAULT_OUTPUT)
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    parser.add_argument("--reserve", type=float, default=DEFAULT_RESERVE)
    parser.add_argument("--json", action="store_true", help="只把结果打到 stdout")
    args = parser.parse_args()

    path = args.snapshot or DEFAULT_SNAPSHOT
    if not os.path.exists(path):
        path = FALLBACK_SNAPSHOT
    if not os.path.exists(path):
        print("缺少快照文件：%s（先跑 tools/qa_phase08_real_snapshot.py）" % path)
        return 2
    with open(path, encoding="utf-8") as handle:
        snapshot = json.load(handle)

    buckets = _rows_by_run(snapshot)
    runs_out = []
    ladder_out: dict = {}
    for run_id, bucket in sorted(buckets.items()):
        run = bucket["run"] if isinstance(bucket.get("run"), dict) else {}
        question = str(run.get("question_text") or "").strip()
        graph, claims, evidence = _graph_for(bucket)
        if not question or not evidence:
            runs_out.append({"run_id": run_id, "skipped": "缺少问题或可用证据",
                             "claims": len(claims), "evidence": len(evidence)})
            continue
        verified = _verify_evidence(graph)
        if isinstance(verified.get("evidence"), list) and verified["evidence"]:
            graph["evidence"] = verified["evidence"]
        result = _run_once(graph=graph, run=run, budget=args.budget, reserve=args.reserve,
                           question=question, injections=True)
        receipt = result["receipt"]
        runs_out.append({
            "run_id": run_id, "mode": str(run.get("mode") or ""),
            "question": question[:120],
            "claims": len(claims), "evidence": len(evidence),
            "verified": verified.get("verified"),
            "verify_error": verified.get("error", ""),
            "tokens_before": receipt["budget"]["estimated_tokens_before"],
            "tokens_after": receipt["budget"]["estimated_tokens_after"],
            "trimmed_items": receipt["budget"]["trimmed_items"],
            "trim_reasons": receipt["budget"]["trim_reasons"],
            "counter_evidence": receipt["sections"]["counter_evidence"],
            "counter_evidence_used": receipt["budget"]["counter_evidence_used"],
            "citations": receipt["citations"],
            "traceable_rate": result["traceability"]["traceable_rate"],
            "context_gaps": receipt["context_gaps"],
            "context_gap_actions": receipt["context_gap_actions"],
            "retrieval_requested": receipt["retrieval_requested"],
            "selection": result["summary"],
            "grounding": result["grounding"],
            "injections": result["injections"],
        })

    # 预算梯子：同一批 run 在不同预算下的裁剪/缺口/预留行为（可复算的前后对比）
    for budget in LADDER:
        totals = {"runs": 0, "tokens_before": 0, "tokens_after": 0, "trimmed": 0,
                  "counter_used": 0, "citations": 0, "gaps": 0, "retrieval_requested": 0,
                  "duplicates_collapsed": 0}
        reasons: dict = {}
        for run_id, bucket in sorted(buckets.items()):
            run = bucket["run"] if isinstance(bucket.get("run"), dict) else {}
            question = str(run.get("question_text") or "").strip()
            graph, claims, evidence = _graph_for(bucket)
            if not question or not evidence:
                continue
            result = _run_once(graph=graph, run=run, budget=budget, reserve=args.reserve,
                               question=question)
            receipt = result["receipt"]
            totals["runs"] += 1
            totals["tokens_before"] += int(receipt["budget"]["estimated_tokens_before"] or 0)
            totals["tokens_after"] += int(receipt["budget"]["estimated_tokens_after"] or 0)
            totals["trimmed"] += int(receipt["budget"]["trimmed_items"] or 0)
            totals["counter_used"] += int(receipt["budget"]["counter_evidence_used"] or 0)
            totals["citations"] += int(receipt["citations"] or 0)
            totals["gaps"] += sum((receipt["context_gaps"] or {}).values())
            totals["retrieval_requested"] += int(receipt["retrieval_requested"] or 0)
            totals["duplicates_collapsed"] += int(
                receipt["budget"].get("duplicates_collapsed") or 0)
            for name, value in (receipt["budget"]["trim_reasons"] or {}).items():
                reasons[name] = reasons.get(name, 0) + int(value)
        totals["trim_reasons"] = dict(sorted(reasons.items(), key=lambda item: (-item[1], item[0])))
        totals["duplicates_collapsed"] = totals.get("duplicates_collapsed", 0)
        totals["kept_rate"] = (round(totals["tokens_after"] / float(totals["tokens_before"]), 6)
                               if totals["tokens_before"] else None)
        ladder_out[str(budget)] = totals

    answered = [row for row in runs_out if row.get("grounding")]
    blocking = [row for row in answered if row["grounding"]["blocking"]]
    marked = [row for row in answered if row["grounding"]["marked"]]
    downgraded = [row for row in answered
                  if row["grounding"]["status_after"] != row["grounding"]["status_before"]]
    violation_counts: dict = {}
    for row in answered:
        for name, value in row["grounding"]["violation_counts"].items():
            violation_counts[name] = violation_counts.get(name, 0) + int(value)

    injection_rows = [item for row in runs_out for item in (row.get("injections") or [])]
    injections = {}
    for name in sorted({item["variant"] for item in injection_rows}):
        rows = [item for item in injection_rows if item["variant"] == name]
        injections[name] = {
            "cases": len(rows),
            "detected": len([item for item in rows if item["detected"]]),
            "blocking": len([item for item in rows if item["blocking"]]),
            "marked": len([item for item in rows if item["marked"]]),
            "codes": _counter(code for item in rows for code in item["codes"]),
            "blocked_rate": round(len([item for item in rows if item["detected"]])
                                  / float(len(rows)), 6) if rows else None,
        }

    report = {
        "report_version": REPORT_VERSION,
        "captured_at_utc": _utc_now_z(),
        "source": {
            "snapshot": path, "snapshot_version": str((snapshot.get("source") or {})
                                                      .get("snapshot_version") or ""),
            "captured_at_utc": snapshot.get("captured_at_utc"),
            "access": str((snapshot.get("source") or {}).get("access") or ""),
            "host": str((snapshot.get("source") or {}).get("host") or ""),
            "note": ("离线复算：真机 run 早于本阶段接线，上下文包是按同一份代码在同一份真实证据上"
                     "现装的；线上行为见 tests/test_qa_phase08_pipeline.py"),
        },
        "params": {"budget": args.budget, "reserve": args.reserve,
                   "estimator": cp.ESTIMATOR_VERSION,
                   "pack_version": contracts.CONTEXT_PACK_VERSION,
                   "grounding_version": contracts.GROUNDING_VERSION,
                   "utility_version": contracts.CONTEXT_UTILITY_VERSION},
        "runs": runs_out,
        "budget_ladder": ladder_out,
        "totals": {
            "runs_scored": len(answered),
            "runs_skipped": len([row for row in runs_out if row.get("skipped")]),
            "runs_without_grounding": len([row for row in runs_out
                                           if not row.get("grounding")]),
            "tokens_before": sum(int(row.get("tokens_before") or 0) for row in runs_out),
            "tokens_after": sum(int(row.get("tokens_after") or 0) for row in runs_out),
            "trimmed_items": sum(int(row.get("trimmed_items") or 0) for row in runs_out),
            "trim_reasons": _counter(reason for row in runs_out
                                     for reason, count in (row.get("trim_reasons") or {}).items()
                                     for _ in range(int(count))),
            "citations": sum(int(row.get("citations") or 0) for row in runs_out),
        "refutes_edges": sum(1 for row in snapshot.get("edges") or []
                             if str(row.get("relationship") or "").casefold() == "refutes"),
        "counter_evidence_items": sum(int(row.get("counter_evidence") or 0) for row in runs_out),
            "traceable_rate": (round(sum(int(row.get("traceable_rate") or 0) * int(row["citations"])
                                         for row in runs_out if row.get("citations"))
                                     / float(sum(int(row.get("citations") or 0)
                                                 for row in runs_out)), 6)
                              if sum(int(row.get("citations") or 0) for row in runs_out) else None),
            "context_gap_types": _counter(gap for row in runs_out
                                          for gap, count in (row.get("context_gaps") or {}).items()
                                          for _ in range(int(count))),
            "context_gap_actions": _counter(action for row in runs_out
                                            for action, count in
                                            (row.get("context_gap_actions") or {}).items()
                                            for _ in range(int(count))),
            "retrieval_requested": sum(int(row.get("retrieval_requested") or 0)
                                       for row in runs_out),
            "grounding_checked": len(answered),
            "grounding_blocking": len(blocking),
            "grounding_blocking_rate": (round(len(blocking) / float(len(answered)), 6)
                                        if answered else None),
            "grounding_marked": len(marked),
            "grounding_status_downgraded": len(downgraded),
            "grounding_interception_rate": (round(len(marked) / float(len(blocking)), 6)
                                            if blocking else None),
            "grounding_violations": dict(sorted(violation_counts.items(),
                                                key=lambda item: (-item[1], item[0]))),
            "grounding_number_strict": sum(int(row["grounding"].get("number_strict") or 0)
                                           for row in answered),
            "grounding_number_not_in_full_excerpt": sum(
                int(row["grounding"].get("number_not_in_full_excerpt") or 0) for row in answered),
        },
        "injection": injections,
        "acceptance": {},
    }
    totals = report["totals"]
    report["acceptance"] = {
        "traceability_ok": bool(totals["citations"]) and totals["traceable_rate"] == 1.0,
        "context_gap_never_triggers_retrieval": totals["retrieval_requested"] == 0,
        "budget_respected": all(int(row.get("tokens_after") or 0) <= args.budget
                                for row in runs_out),
        "trim_is_deterministic": True,   # 由 tests/test_qa_phase08_budget.py 的双跑等值钉死
        "grounding_rate_measured": totals["grounding_checked"] > 0,
        "injection_blocked_rate": (round(sum(item["detected"] for item in injection_rows)
                                         / float(len(injection_rows)), 6)
                                   if injection_rows else None),
        "passed": bool(totals["citations"]) and totals["traceable_rate"] == 1.0
                  and totals["retrieval_requested"] == 0,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=1)
    print("报告已写入 %s" % args.out)
    print("run 数 %d（有最终答案 %d；跳过 %d）｜引用 %d 条，可回溯率 %s"
          % (totals["runs_scored"], totals["grounding_checked"], totals["runs_skipped"],
             totals["citations"], totals["traceable_rate"]))
    print("token 估算：裁剪前 %d → 裁剪后 %d（被裁 %d 条）｜原因分布 %s"
          % (totals["tokens_before"], totals["tokens_after"], totals["trimmed_items"],
             json.dumps(totals["trim_reasons"], ensure_ascii=False)))
    print("Context Gap：%s ｜动作 %s ｜触发检索 %d 条"
          % (json.dumps(totals["context_gap_types"], ensure_ascii=False),
             json.dumps(totals["context_gap_actions"], ensure_ascii=False),
             totals["retrieval_requested"]))
    print("反证：真机 refutes 边 %d 条，进包反证条目 %d 条（真机无冲突/无反证边时预留无对象，"
          "该能力的证据来自构造用例）" % (totals["refutes_edges"], totals["counter_evidence_items"]))
    print("生成端 grounding：检查 %d 条真实答案，含阻断级违规 %d 条（%s），显式标注 %d 条，"
          "状态降档 %d 条｜真机拦截率 %s"
          % (totals["grounding_checked"], totals["grounding_blocking"],
             totals["grounding_blocking_rate"], totals["grounding_marked"],
             totals["grounding_status_downgraded"], totals["grounding_interception_rate"]))
    print("  违规码分布：%s" % json.dumps(totals["grounding_violations"], ensure_ascii=False))
    print("  数字口径对照：不在被引用 span 内 %d 处，其中在完整正文里也找不到 %d 处"
          % (totals["grounding_number_strict"], totals["grounding_number_not_in_full_excerpt"]))
    print("  构造注入拦截（明确非真机数据）：%s"
          % json.dumps({key: [value["cases"], value["detected"], value["blocked_rate"]]
                        for key, value in injections.items()}, ensure_ascii=False))
    print("预算梯子（同一批真机 run，只换预算）：")
    for key, value in ladder_out.items():
        print("  预算 %-6s token %6d → %6d（保留 %s）｜被裁 %3d 条 %s｜同 id 折叠 %d 条"
              % (key, value["tokens_before"], value["tokens_after"], value["kept_rate"],
                 value["trimmed"], json.dumps(value["trim_reasons"], ensure_ascii=False),
                 value["duplicates_collapsed"]))
    print("验收：%s" % json.dumps(report["acceptance"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
