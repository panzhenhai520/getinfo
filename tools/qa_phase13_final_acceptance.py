#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 13（P13-01…P13-06）· **Answer Composer & Final Verifier 验收工具**
（A 机只读快照 + 同代码离线重建，**可复算**；零模型调用、零嵌入端点、零写库）。

为什么是"离线重建"而不是"线上统计"：A 机尚未发布 Phase 13（`qa_stage_runs` 里没有
`final_verify` 行），所以线上没有可统计的终验结论。口径与 Phase 04/08/09~12 一致：
  · 从 A 机**只读**导出真实 run 的结论/边/证据/最终答案（`baseline/qa-context-real-sample.json`）；
  · 离线用**同一份代码**重放：Phase 06 的读侧重建结论图 → Phase 03 的**规则核验**
    （`qa_verifier.verify_claim_graph`，与管线里跑的是同一个函数）→ Phase 13 的组装/六问/
    局部重试/证据不足输出；
  · 报出来的每个数字都来自**真数据 + 真规则**，边界（终验尚未上线、真机无矛盾/无反证边）
    如实标注。

本工具回答六个可复算的问题：
  1. **绑定覆盖率**：真机最终答案里的每条结论，能不能绑到"Phase 02 最小 span + Phase 03 核验结论"；
  2. **§17 六问的真实失败分布**（before = 只跑 Phase 08 的 grounding；after = 六问终验）；
  3. **阻断级违规"局部重试修掉了多少"**（before/after 两个数字）；
  4. **局部重试的作用域分布与改动条数**，以及 `retrieval_calls` 恒 0（不重跑 Research Graph）；
  5. **答案形态变化**：`status` 从 before 到 after 的迁移计数（这会**真的改变用户看到的答案**）；
  6. **六问都能被触发**（对真实答案形状做六种构造注入，逐一验证检出），补上真机上"某几问没有分母"的缺口。

时钟：一律取快照的 `captured_at_utc`（**不用墙上钟**），所以同一份快照跑两次产出逐字相同。

用法：
    python tools/qa_phase13_final_acceptance.py --json
    python tools/qa_phase13_final_acceptance.py --out baseline/qa-final-acceptance.json
    python tools/qa_phase13_final_acceptance.py --snapshot <新快照> --print
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_final_verifier as fv  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_verifier  # noqa: E402

import qa_phase08_context_acceptance as acc08  # noqa: E402  （复用同一套"快照→结论图"重建口径）

REPORT_VERSION = "qa-final-acceptance-v1"
DEFAULT_SNAPSHOT = os.path.join("baseline", "qa-context-real-sample.json")
FALLBACK_SNAPSHOT = os.path.join("baseline", "qa-gap-real-sample.json")
DEFAULT_OUTPUT = os.path.join("baseline", "qa-final-acceptance.json")


def _counter(values, *, top: int = 0) -> dict:
    counts: dict = {}
    for value in values or ():
        key = str(value or "")
        counts[key] = counts.get(key, 0) + 1
    ordered = dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
    if top:
        return dict(list(ordered.items())[:top])
    return ordered


def _clock(snapshot: dict) -> str:
    """快照自带的时间戳（**唯一的"时钟"**；没有就退到一个固定值，绝不用墙上钟）。"""
    raw = str(snapshot.get("captured_at_utc") or "").strip()
    if raw:
        return raw
    return "1970-01-01T00:00:00Z"


def _now(snapshot: dict) -> datetime:
    raw = _clock(snapshot).replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        moment = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _replay_verification(graph: dict, *, now: datetime, pack_id: str) -> dict:
    """同一份代码重放 Phase 03 的 claim 级核验（真机 run 早于核验层接线时也要能核）。

    时钟显式传快照时间：否则"今天"一变，时效分就会变，报告不可复现。
    """
    try:
        summary = qa_verifier.verify_claim_graph(graph, pack_id=pack_id, now=now)
        return {"stats": dict((summary or {}).get("stats") or {}), "error": ""}
    except Exception as exc:  # noqa: BLE001 —— 核验重放失败要如实记账，不许静默
        return {"stats": {}, "error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}


def _composition_facts(composition: dict) -> dict:
    bindings = [row for row in (composition.get("bindings") or []) if isinstance(row, dict)]
    in_answer = [row for row in bindings if row.get("in_answer")]
    return {
        "claims": len(bindings),
        "claims_in_answer": len(in_answer),
        "bound": len([row for row in bindings if row.get("bound")]),
        "bound_in_answer": len([row for row in in_answer if row.get("bound")]),
        "unbound_in_answer": len([row for row in in_answer if not row.get("bound")]),
        "verified_claims": len(composition.get("verified_claims") or []),
        "binding_reasons": _counter(row.get("binding_reason") for row in bindings),
        "citation_labels": len(composition.get("citation_index") or {}),
        "counter_evidence": len(composition.get("counter_evidence") or []),
        "unresolved_gaps": len(composition.get("unresolved_gaps") or []),
        "contradictions": len(composition.get("contradictions") or []),
    }


def _retry_facts(retries) -> list:
    """重试记录：层的 `retries` 带完整 `changes` 列表，回执里的同名键是**计数**（两者都认）。"""
    out = []
    for row in retries or []:
        if not isinstance(row, dict):
            continue
        changes = row.get("changes")
        out.append({
            "scope": list(row.get("scope") or []),
            "reason": str(row.get("reason") or ""),
            "changes": len(changes) if isinstance(changes, (list, tuple)) else int(changes or 0),
            "retrieval_calls": int(row.get("retrieval_calls") or 0),
        })
    return out


def _verification_facts(verification: dict, receipt: dict, retries=None) -> dict:
    checks = [row for row in (verification.get("checks") or []) if isinstance(row, dict)]
    retry_rows = _retry_facts(retries if retries is not None else receipt.get("retries"))
    return {
        "outcome": str(verification.get("outcome") or ""),
        "checks_passed": len([row for row in checks if row.get("passed")]),
        "checks_total": len(checks),
        "failed_checks": list(verification.get("failed_checks") or []),
        "blocking_codes": list(verification.get("blocking_codes") or []),
        "warning_codes": list(verification.get("warning_codes") or []),
        "check_severity": {row["check_id"]: row.get("severity") for row in checks},
        "retries": retry_rows,
        "retrieval_calls": sum(int(row["retrieval_calls"]) for row in retry_rows),
        "insufficient": bool(receipt.get("insufficient")),
        "insufficient_reasons": list(receipt.get("insufficient_reasons") or []),
        "status": str(receipt.get("status") or ""),
    }


def _injections(run_id: str, graph: dict, answer: dict, composition: dict) -> list:
    """六种构造注入（**明确标注：不是真机数据，是对真实答案形状的变异**），一问一种。

    真机上很可能有几问没有分母（例如真机根本没有 REFUTES 边），所以"六问都能真的失败"
    这件事必须用真实答案形状的变异来证明；每条都记录"该问是否被检出 + 分级"。
    """
    out: list = []
    base_graph = graph
    base_answer = answer

    def _run(name: str, target: str, mutate) -> None:
        payload_graph = copy.deepcopy(base_graph)
        payload_answer = copy.deepcopy(base_answer)
        try:
            applied = mutate(payload_graph, payload_answer, composition)
        except Exception as exc:  # noqa: BLE001
            out.append({"run_id": run_id, "variant": name, "target_check": target,
                        "applicable": False, "detected": False, "severity": "", "failures": 0,
                        "outcome": "", "error": "%s: %s" % (type(exc).__name__, str(exc)[:120])})
            return
        if applied is False:
            # 这条 run 的形状让该变异无从下手（例如答案里一条 claim 都没有）：
            # 如实记"不适用"，**不进分母**（分母里混进不适用样本等于放水）。
            out.append({"run_id": run_id, "variant": name, "target_check": target,
                        "applicable": False, "detected": False, "severity": "", "failures": 0,
                        "outcome": "", "error": ""})
            return
        try:
            verification = fv.verify_answer(answer=payload_answer,
                                            composition=fv.compose_answer(graph=payload_graph,
                                                                          answer=payload_answer),
                                            graph=payload_graph)
            checks = {row["check_id"]: row for row in verification["checks"]}
            row = checks.get(target) or {}
            out.append({
                "run_id": run_id, "variant": name, "target_check": target,
                "applicable": True,
                "detected": not bool(row.get("passed", True)),
                "severity": str(row.get("severity") or ""),
                "failures": len(row.get("failures") or []),
                "outcome": str(verification.get("outcome") or ""),
                "error": "",
            })
        except Exception as exc:  # noqa: BLE001
            out.append({"run_id": run_id, "variant": name, "target_check": target,
                        "applicable": True, "detected": False, "severity": "", "failures": 0,
                        "outcome": "", "error": "%s: %s" % (type(exc).__name__, str(exc)[:120])})

    def _strip_bindings(_graph, payload, _composition) -> bool:
        claims = [row for row in payload.get("claims") or [] if isinstance(row, dict)]
        if not claims:
            return False
        for claim in claims:
            claim["evidence_refs"] = []
        return True

    def _first_claim_id(payload) -> str:
        for claim in payload.get("claims") or []:
            if isinstance(claim, dict) and claim.get("claim_id"):
                return str(claim["claim_id"])
        return ""

    def _node_for(graph_payload, cid: str) -> dict:
        for node in graph_payload.get("claims") or []:
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("claim_id") or node.get("canonical_id")
                          or (node.get("claim") or {}).get("claim_id") or "")
            if node_id and node_id == cid:
                return node
        return {}

    def _refute_first(graph_payload, payload, _composition) -> bool:
        cid = _first_claim_id(payload)
        node = _node_for(graph_payload, cid)
        if not cid or not node:
            return False
        refs = [str(ref) for ref in
                ((payload.get("claims") or [{}])[0].get("evidence_refs") or [])]
        ref = refs[0] if refs else "inj:evidence"
        node["verification"] = {"status": "conflicted",
                                "pairs": [{"evidence_ref": ref, "verdict": "REFUTED",
                                           "score": 0.6}]}
        for claim in payload.get("claims") or []:
            if isinstance(claim, dict) and str(claim.get("claim_id") or "") == cid:
                claim["verification_status"] = "conflicted"
        return True

    def _numbers(_graph, payload, _composition) -> bool:
        payload["answer"] = str(payload.get("answer") or "") + " 另有 987654 万港元的缺口。"
        return True

    def _counter(graph_payload, payload, _composition) -> bool:
        """给答案里的第一条结论挂一条**答案没引用**的反证边（用合成 ref，保证条件必然成立）。"""
        cid = _first_claim_id(payload)
        node = _node_for(graph_payload, cid)
        if not cid or not node:
            return False
        cited = {str(ref) for ref in (payload.get("citations") or [])}
        ref = "inj:counter-evidence"
        while ref in cited:
            ref += "x"
        node["edges"] = list(node.get("edges") or []) + [
            {"edge_id": "inj", "kind": "claim-evidence", "graph_relation": "REFUTES",
             "status": "REFUTED", "claim_id": cid, "evidence_ref": ref}]
        claim = node.setdefault("claim", {})
        claim["evidence_refs"] = list(claim.get("evidence_refs") or []) + [ref]
        return True

    def _label_key(label: str):
        text = str(label).strip("[]")
        return (int(text) if text.isdigit() else 10 ** 6, str(label))

    def _citation(graph_payload, payload, composition_payload) -> bool:
        """把**当前与引用索引一致**的某个编号改成另一条证据（保证"新出现一处不一致"）。

        为什么不无脑交换前两个编号：离线复算时（`index_source=derived`）编号本来就可能与
        答案自带的 `citation_map` 不一致，交换两个"本来就都不一致"的编号等于没改。
        """
        citation_map = payload.get("citation_map")
        if not isinstance(citation_map, dict) or not citation_map:
            return False
        index = (composition_payload or {}).get("citation_index") or {}
        refs = [str(entry.get("evidence_ref")) for entry in index.values()
                if isinstance(entry, dict) and str(entry.get("evidence_ref") or "")]
        for label in sorted(citation_map, key=_label_key):
            current = str(citation_map[label])
            mapped = str((index.get(str(label)) or {}).get("evidence_ref") or "")
            if mapped != current:
                continue
            alternative = next((ref for ref in refs if ref != current), "inj:other-evidence")
            citation_map[label] = alternative
            return True
        # 一个"一致"的编号都没有：直接把第一个编号指到另一条证据
        label = sorted(citation_map, key=_label_key)[0]
        current = str(citation_map[label])
        citation_map[label] = next((ref for ref in refs if ref != current), "inj:other-evidence")
        return True

    def _plan_only(graph_payload, payload, _composition) -> bool:
        cid = _first_claim_id(payload)
        node = _node_for(graph_payload, cid)
        if not cid or not node:
            return False
        node["plan_only"] = True
        return True

    _run("A_fact_without_evidence", "FACT_WITHOUT_EVIDENCE", _strip_bindings)
    _run("B_evidence_refutes_claim", "EVIDENCE_NOT_SUPPORTING", _refute_first)
    _run("C_number_beyond_span", "OVER_INFERENCE", _numbers)
    _run("D_counter_evidence_omitted", "COUNTER_EVIDENCE_MISSING", _counter)
    _run("E_citation_swapped", "CITATION_MISBOUND", _citation)
    _run("F_plan_claim_as_fact", "SPECULATION_AS_FACT", _plan_only)
    return out


def analyze(snapshot: dict, *, injections: bool = True) -> dict:
    """跑完整份快照（**纯函数**：同一份快照永远同一份报告）。"""
    clock = _clock(snapshot)
    moment = _now(snapshot)
    buckets = acc08._rows_by_run(snapshot)
    runs_out: list = []
    injection_rows: list = []
    for run_id, bucket in sorted(buckets.items()):
        run = bucket["run"] if isinstance(bucket.get("run"), dict) else {}
        question = str(run.get("question_text") or "").strip()
        answer = run.get("final_answer") if isinstance(run.get("final_answer"), dict) else {}
        graph, claims, evidence = acc08._graph_for(bucket)
        row = {"run_id": run_id, "mode": str(run.get("mode") or ""), "question": question[:120],
               "claims": len(claims), "evidence": len(evidence),
               "preexisting_status": str(answer.get("status") or "")}
        if not question:
            row["skipped"] = "缺少问题"
            runs_out.append(row)
            continue
        if not str(answer.get("answer") or ""):
            row["skipped"] = "没有最终答案"
            runs_out.append(row)
            continue
        # 证据/结论为空**不是**跳过理由：那正是 P13-06 最该说话的场景（真机上就有一条
        # status=insufficient_evidence 的 run 是零证据的），必须照样跑终验。
        row["no_evidence"] = not evidence
        replayed = _replay_verification(graph, now=moment, pack_id=run_id)
        row["verify_stats"] = replayed["stats"]
        row["verify_error"] = replayed["error"]
        composition = fv.compose_answer(graph=graph, answer=answer, question=question)
        row["composition"] = _composition_facts(composition)
        # before：只跑 Phase 08 的 grounding（本阶段的单一真源）
        grounding = fv._grounding_for(answer, pack=None, graph=graph, composition=composition)
        row["before"] = {
            "blocking": bool(grounding.get("blocking")),
            "blocking_codes": list(grounding.get("blocking_codes") or []),
            "warning_codes": list(grounding.get("warning_codes") or []),
            "violation_counts": dict(grounding.get("violation_counts") or {}),
            "status": str(answer.get("status") or ""),
            "verified_claims": row["composition"]["verified_claims"],
        }
        layer = fv.final_verification_layer(graph=graph, answer=answer, pack=None,
                                            question=question, stop_reason="ANSWERABLE")
        receipt = dict(layer.get("receipt") or {})
        row["after"] = _verification_facts(layer.get("verification") or {}, receipt,
                                           retries=layer.get("retries"))
        row["rounds"] = len(layer.get("rounds") or [])
        row["answer_status"] = str((layer.get("answer") or {}).get("status") or "")
        row["degradation_reasons"] = [str(item)[:120]
                                      for item in ((layer.get("answer") or {})
                                                   .get("degradation_reasons") or [])][:4]
        # 引用可回溯率（复用 Phase 08 的同一条复算：content_excerpt[start:end] == span.quote）
        pack = {"citation_index": {
            label: {"evidence_ref": entry.get("evidence_ref"),
                    "evidence_id": entry.get("evidence_id"),
                    "quote": entry.get("quote"), "span": entry.get("span"),
                    "grounded": entry.get("grounded")}
            for label, entry in (composition.get("citation_index") or {}).items()
            if isinstance(entry, dict)}}
        row["traceability"] = acc08._traceability(pack, graph)
        if injections:
            injection_rows.extend(_injections(run_id, graph, answer, composition))
        runs_out.append(row)

    scored = [row for row in runs_out if row.get("composition")]
    check_failures = _counter(code for row in scored for code in (row["after"]["failed_checks"] or []))
    check_passed = sum(int(row["after"]["checks_passed"]) for row in scored)
    check_total = sum(int(row["after"]["checks_total"]) for row in scored)
    blocking_before = [row for row in scored if row["before"]["blocking"]]
    blocking_after = [row for row in scored if row["after"]["blocking_codes"]]
    retry_rows = [retry for row in scored for retry in (row["after"]["retries"] or [])]
    changed_rows = [retry for retry in retry_rows if retry["changes"]]
    status_moves = _counter("%s→%s" % (row["before"]["status"], row["answer_status"])
                            for row in scored)
    citation_checked = sum(int(row["traceability"]["checked"]) for row in scored)
    citation_traceable = sum(int(row["traceability"]["traceable"]) for row in scored)
    injection_summary = {}
    for name in sorted({row["variant"] for row in injection_rows}):
        rows = [row for row in injection_rows if row["variant"] == name and row.get("applicable")]
        skipped = len([row for row in injection_rows
                       if row["variant"] == name and not row.get("applicable")])
        injection_summary[name] = {
            "cases": len(rows),
            "inapplicable": skipped,
            "detected": len([row for row in rows if row["detected"]]),
            "blocking": len([row for row in rows if row["severity"] == "blocking"]),
            "warning": len([row for row in rows if row["severity"] == "warning"]),
            "errors": len([row for row in rows if row.get("error")]),
            "detect_rate": (round(len([row for row in rows if row["detected"]]) / float(len(rows)), 6)
                            if rows else None),
        }
    report = {
        "report_version": REPORT_VERSION,
        "clock": clock,
        "source": {
            "snapshot": str(snapshot.get("__path__") or ""),
            "snapshot_version": str((snapshot.get("source") or {}).get("snapshot_version") or ""),
            "captured_at_utc": snapshot.get("captured_at_utc"),
            "access": str((snapshot.get("source") or {}).get("access") or ""),
            "host": str((snapshot.get("source") or {}).get("host") or ""),
            "note": ("离线复算 + 同代码重建：A 机尚未发布 Phase 13（qa_stage_runs 里没有 "
                     "final_verify 行），所以终验结论是拿**真机真实答案**在同一份代码、"
                     "同一份真实证据上现跑的；时钟取快照 captured_at_utc，两次运行逐字相同。"),
        },
        "params": {
            "composer_version": contracts.ANSWER_COMPOSER_VERSION,
            "verifier_version": contracts.FINAL_VERIFIER_VERSION,
            "retry_version": contracts.FINAL_VERIFY_RETRY_VERSION,
            "insufficient_version": contracts.INSUFFICIENT_EVIDENCE_VERSION,
            "verifier_core": qa_verifier.VERIFIER_VERSION,
            "retry_max": fv.retry_max(),
            "checks": list(contracts.FINAL_VERIFIER_CHECKS),
        },
        "runs": runs_out,
        "totals": {
            "runs_total": len(runs_out),
            "runs_scored": len(scored),
            "runs_skipped": len([row for row in runs_out if row.get("skipped")]),
            "claims": sum(int(row["claims"]) for row in scored),
            "evidence": sum(int(row["evidence"]) for row in scored),
            "claims_verified_supported": sum(int(row["verify_stats"].get("confirmed") or 0)
                                             for row in scored),
            "bindings": sum(int(row["composition"]["claims"]) for row in scored),
            "bound_in_answer": sum(int(row["composition"]["bound_in_answer"]) for row in scored),
            "unbound_in_answer": sum(int(row["composition"]["unbound_in_answer"]) for row in scored),
            "verified_claims": sum(int(row["composition"]["verified_claims"]) for row in scored),
            "binding_reasons": _counter(reason for row in scored
                                        for reason, count in row["composition"]["binding_reasons"].items()
                                        for _ in range(int(count))),
            "citation_labels": sum(int(row["composition"]["citation_labels"]) for row in scored),
            "citation_checked": citation_checked,
            "citation_traceable": citation_traceable,
            "citation_traceable_rate": (round(citation_traceable / float(citation_checked), 6)
                                        if citation_checked else None),
            "counter_evidence": sum(int(row["composition"]["counter_evidence"]) for row in scored),
            "unresolved_gaps": sum(int(row["composition"]["unresolved_gaps"]) for row in scored),
            "contradictions": sum(int(row["composition"]["contradictions"]) for row in scored),
            "checks_passed": check_passed,
            "checks_total": check_total,
            "checks_pass_rate": (round(check_passed / float(check_total), 6) if check_total else None),
            "check_failures": check_failures,
            "outcomes": _counter(row["after"]["outcome"] for row in scored),
            "blocking_before": len(blocking_before),
            "blocking_after": len(blocking_after),
            "blocking_fixed_by_retry": len([row for row in blocking_before
                                            if not row["after"]["blocking_codes"]]),
            "retry_rows": len(retry_rows),
            "retry_rows_with_changes": len(changed_rows),
            "retry_scopes": _counter("+".join(retry["scope"]) for retry in retry_rows),
            "retry_reasons": _counter(retry["reason"] for retry in retry_rows),
            "retrieval_calls": sum(int(retry["retrieval_calls"]) for retry in retry_rows),
            "insufficient_runs": len([row for row in scored if row["after"]["insufficient"]]),
            "insufficient_reasons": _counter(reason for row in scored
                                             for reason in (row["after"]["insufficient_reasons"] or [])),
            "no_evidence_runs": len([row for row in scored if row.get("no_evidence")]),
            "no_evidence_marked_insufficient": len([row for row in scored
                                                    if row.get("no_evidence")
                                                    and row["after"]["insufficient"]]),
            "preexisting_status": _counter(row["preexisting_status"] for row in scored),
            "status_moves": status_moves,
            "status_before": _counter(row["preexisting_status"] for row in scored),
            "status_after": _counter(row["answer_status"] for row in scored),
            "verify_errors": len([row for row in scored if row.get("verify_error")]),
        },
        "injection": injection_summary,
        "injection_rows": injection_rows,
        "acceptance": {},
    }
    totals = report["totals"]
    injection_ok = (all(row["cases"] > 0 and row["detect_rate"] == 1.0
                        for row in injection_summary.values())
                    if injection_summary else False)
    report["acceptance"] = {
        "real_answers_scored": totals["runs_scored"] > 0,
        "bindings_are_traceable": bool(totals["citation_checked"])
                                  and totals["citation_traceable_rate"] == 1.0,
        "six_checks_all_reachable": len(injection_summary) == len(contracts.FINAL_VERIFIER_CHECKS)
                                    and injection_ok,
        "retry_never_retrieves": totals["retrieval_calls"] == 0,
        "no_verification_errors": totals["verify_errors"] == 0,
        "insufficient_has_a_basis": all(row["after"]["insufficient_reasons"]
                                        for row in scored if row["after"]["insufficient"]),
        "no_evidence_is_marked_insufficient": all(row["after"]["insufficient"]
                                                  for row in scored if row.get("no_evidence")),
        "passed": (totals["runs_scored"] > 0
                   and bool(totals["citation_checked"])
                   and totals["citation_traceable_rate"] == 1.0
                   and totals["retrieval_calls"] == 0
                   and injection_ok),
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", default="")
    parser.add_argument("--out", default=DEFAULT_OUTPUT)
    parser.add_argument("--json", action="store_true", help="只把结果打到 stdout")
    parser.add_argument("--print", dest="show", action="store_true", help="写完报告再打一份摘要")
    parser.add_argument("--no-injections", action="store_true")
    args = parser.parse_args()

    path = args.snapshot or DEFAULT_SNAPSHOT
    if not os.path.exists(path):
        path = FALLBACK_SNAPSHOT
    if not os.path.exists(path):
        print("缺少快照文件：%s（先跑 tools/qa_phase08_real_snapshot.py）" % path)
        return 2
    with open(path, encoding="utf-8") as handle:
        snapshot = json.load(handle)
    snapshot["__path__"] = path

    report = analyze(snapshot, injections=not args.no_injections)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=1)
    totals = report["totals"]
    print("报告已写入 %s" % args.out)
    print("run 数 %d（评分 %d；跳过 %d）｜结论 %d 条 / 证据 %d 条｜Phase 03 判 confirmed %d 条"
          % (totals["runs_total"], totals["runs_scored"], totals["runs_skipped"],
             totals["claims"], totals["evidence"], totals["claims_verified_supported"]))
    print("绑定：%d 条结论 / 答案内 %d 条绑上 %d 条（绑不上 %d 条）｜已验证 %d 条｜引用编号 %d 个"
          % (totals["bindings"], totals["bound_in_answer"] + totals["unbound_in_answer"],
             totals["bound_in_answer"], totals["unbound_in_answer"],
             totals["verified_claims"], totals["citation_labels"]))
    print("  绑定理由分布：%s" % json.dumps(totals["binding_reasons"], ensure_ascii=False))
    print("引用可回溯：%d/%d（%s）" % (totals["citation_traceable"], totals["citation_checked"],
                                      totals["citation_traceable_rate"]))
    print("六问：通过 %d/%d（%s）｜失败分布 %s"
          % (totals["checks_passed"], totals["checks_total"], totals["checks_pass_rate"],
             json.dumps(totals["check_failures"], ensure_ascii=False)))
    print("阻断级：before %d 条 → after %d 条（局部重试修掉 %d 条）"
          % (totals["blocking_before"], totals["blocking_after"], totals["blocking_fixed_by_retry"]))
    print("局部重试：%d 轮（有改动 %d 轮）｜作用域 %s｜去向 %s｜**检索调用 %d**"
          % (totals["retry_rows"], totals["retry_rows_with_changes"],
             json.dumps(totals["retry_scopes"], ensure_ascii=False),
             json.dumps(totals["retry_reasons"], ensure_ascii=False), totals["retrieval_calls"]))
    print("证据不足：%d 条 run（%s）" % (totals["insufficient_runs"],
                                       json.dumps(totals["insufficient_reasons"], ensure_ascii=False)))
    print("答案形态迁移（before→after）：%s" % json.dumps(totals["status_moves"], ensure_ascii=False))
    print("构造注入（明确非真机数据，一问一种）：%s"
          % json.dumps({key: [value["cases"], value["detected"], value["detect_rate"]]
                        for key, value in report["injection"].items()}, ensure_ascii=False))
    print("验收：%s" % json.dumps(report["acceptance"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
