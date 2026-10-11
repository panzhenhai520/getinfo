#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 15 · P15-06（audit/replay）用例。

钉住 §17 追加验收项（"可 replay 任意回答：执行 → 证据 → 上下文 → 记忆"）与
MASTER_RULES 第 9/18 条（实验与 trace 不覆盖历史、日志不泄露敏感信息）：
  1. 审计事件落**既有** `qa_audit_events` 表（零迁移），事件种类只在契约闭集合内；
  2. 审计载荷**先过 P15-05 最小化**：正文只留指纹，标识符/密钥一律不带；
  3. 回放链是四层（执行/证据/上下文/记忆），层间引用**逐跳校验**，缺口如实列出；
  4. `replay_matches` 是"拿审计事件里的输入重跑同一批纯规则"的结果比对 ——
     篡改过的事件必须被检出（反例用例）；
  5. 失败路径：没有库、没有 run、没有审计事件都不抛错，如实给空结构。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_graph_contracts as contracts  # noqa: E402
import qa_memory as memory  # noqa: E402
import qa_memory_safety as ms  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

import qa_phase09_fixtures as fx  # noqa: E402

FLAGS = ("QA_MEMORY_AUDIT",)


def _candidate(text=fx.CLAIM_TEXT, *, scope="PATIENT_LONGITUDINAL", **overrides):
    row = {
        "memory_type": "VERIFIED_CLAIM", "canonical_content": text, "claim_id": "c1",
        "claim_type": "policy", "claim_confidence": 0.82, "freshness_class": "LONG",
        "valid_from": "", "valid_until": "", "entity_ids": ["家族办公室"], "supporting": 1,
        "evidence": [{"evidence_ref": "article:1", "source_fingerprint": "SF-1",
                      "span_fingerprint": "SP-1", "verdict": "SUPPORTED",
                      "evidence_score": 0.9, "relationship": "supports",
                      "run_id": "run-p15", "stage": "level1_retrieval", "route": "keyword",
                      "corpus_version": "corpus-p15", "metadata": {"article_id": 1}}],
        "scope": scope, "owner_user_id": "p15", "session_id": "s1",
        "industry_pack_id": "auto", "run_id": "run-p15", "metadata": {},
    }
    row.update(overrides)
    return row


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = {name: os.environ.pop(name, None) for name in FLAGS}

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class AuditEventTests(Base):
    def test_event_kind_is_clamped_to_the_contract(self):
        with fx.temp_store("phase15-audit.sqlite3") as (_database, store):
            result = ms.record_memory_audit(store, "NOT_A_KIND", {"a": 1}, run_id="run-p15")
            self.assertEqual(result["event_type"], "LOG_MINIMIZED")
            rows = store.list_audit_events(trace_id="run-p15")
            self.assertEqual(rows[0]["event_type"], "LOG_MINIMIZED")
            self.assertIn(rows[0]["event_type"], contracts.MEMORY_AUDIT_EVENT_KINDS)

    def test_payload_is_minimized_before_it_is_stored(self):
        with fx.temp_store("phase15-audit2.sqlite3") as (_database, store):
            ms.record_memory_audit(store, "SCOPE_ENFORCED",
                                   {"inputs": {"canonical_content": "正文不该进审计表",
                                               "scope": "GLOBAL_KNOWLEDGE"},
                                    "api_key": "sk-abcdefgh", "note": "13800138000"},
                                   run_id="run-p15")
            row = store.list_audit_events(trace_id="run-p15")[0]
            payload = row["payload"]
            self.assertEqual(payload["api_key"], "[REDACTED]")
            self.assertNotIn("13800138000", str(payload))
            self.assertEqual(payload["inputs"]["canonical_content"]["length"], 8)
            self.assertNotIn("正文不该进审计表", str(payload))
            self.assertEqual(payload["privacy_policy"], contracts.PRIVACY_MINIMAL_LOG_VERSION)

    def test_switch_off_records_nothing_but_says_why(self):
        os.environ["QA_MEMORY_AUDIT"] = "0"
        with fx.temp_store("phase15-audit3.sqlite3") as (_database, store):
            result = ms.record_memory_audit(store, "LOG_MINIMIZED", {"a": 1}, run_id="r")
            self.assertFalse(result["recorded"])
            self.assertEqual(result["error"], "audit_disabled")
            self.assertEqual(store.list_audit_events(trace_id="r"), [])

    def test_store_without_a_database_is_tolerated(self):
        result = ms.record_memory_audit(object(), "LOG_MINIMIZED", {"a": 1})
        self.assertFalse(result["recorded"])
        self.assertEqual(result["error"], "no_audit_sink")


class RecallAuditTests(Base):
    def test_recall_safety_writes_replayable_events(self):
        with fx.temp_store("phase15-audit4.sqlite3") as (_database, store):
            memory.apply_write_gate(store, [_candidate(encounter_id="e-other",
                                                       scope="ENCOUNTER")], run_id="run-p15")
            stored = store.load_memory_items(include_all_scopes=True)[0]
            receipt = {"recall_version": "qa-memory-recall-v1", "mode": "evidence",
                       "channels": ["relational"], "counts": {}, "hits": [{
                           "memory_id": stored["memory_id"], "memory_type": "VERIFIED_CLAIM",
                           "canonical_content": stored["canonical_content"], "status": "ACTIVE",
                           "scope": "ENCOUNTER", "freshness_class": "LONG", "score": 0.4,
                           "factors": {}, "channels": ["relational"], "evidence_refs": [],
                           "hint": True, "requires_revalidation": True,
                           "verified_evidence": False, "explain": ""}]}
            safety = ms.apply_recall_safety(
                store, receipt, run_meta={"id": "run-p15", "owner_user_id": "p15",
                                          "session_id": "s1", "industry_pack_id": "auto"})
            self.assertGreaterEqual(safety["memory_safety"]["audit"]["events"], 1)
            self.assertEqual(safety["memory_safety"]["audit"]["recorded"],
                             safety["memory_safety"]["audit"]["events"])
            rows = store.list_audit_events(trace_id="run-p15")
            kinds = {row["event_type"] for row in rows}
            self.assertTrue(kinds <= set(contracts.MEMORY_AUDIT_EVENT_KINDS))
            self.assertIn("ENCOUNTER_ISOLATED", kinds)
            for row in rows:
                self.assertIn("inputs", row["payload"])
                self.assertTrue(row["payload"]["decision"])

    def test_replay_reproduces_the_recorded_decisions(self):
        with fx.temp_store("phase15-audit5.sqlite3") as (_database, store):
            memory.apply_write_gate(store, [_candidate(encounter_id="e-other",
                                                       scope="ENCOUNTER")], run_id="run-p15")
            stored = store.load_memory_items(include_all_scopes=True)[0]
            ms.apply_recall_safety(
                store, {"recall_version": "qa-memory-recall-v1", "mode": "evidence",
                        "channels": ["relational"], "counts": {}, "hits": [
                            {"memory_id": stored["memory_id"], "memory_type": "VERIFIED_CLAIM",
                             "canonical_content": stored["canonical_content"], "status": "ACTIVE",
                             "scope": "ENCOUNTER", "freshness_class": "LONG", "score": 0.4,
                             "factors": {}, "channels": ["relational"], "evidence_refs": [],
                             "hint": True, "requires_revalidation": True,
                             "verified_evidence": False, "explain": ""}]},
                run_meta={"id": "run-p15", "owner_user_id": "p15", "session_id": "s1",
                          "industry_pack_id": "auto"})
            report = ms.verify_replay(store, "run-p15")
            self.assertTrue(report["replay_matches"], report["mismatches"])
            self.assertGreaterEqual(report["counts"]["audited_decisions"], 1)
            self.assertTrue(report["contract_ok"], report.get("contract_error"))

    def test_tampered_audit_event_is_detected(self):
        """被改写过的审计判定必须被重放检出（否则 `replay_matches` 就是摆设）。"""
        with fx.temp_store("phase15-audit6.sqlite3") as (_database, store):
            memory.apply_write_gate(store, [_candidate(encounter_id="e-other",
                                                       scope="ENCOUNTER")], run_id="run-p15")
            stored = store.load_memory_items(include_all_scopes=True)[0]
            ms.record_memory_audit(store, "SCOPE_ENFORCED",
                                   {"inputs": {"memory_id": stored["memory_id"],
                                               "memory_type": "VERIFIED_CLAIM",
                                               "scope": "ENCOUNTER",
                                               "scope_key": "encounter:auto:s1:e-other",
                                               "content_kind": "SUBJECT_FACT"},
                                    "decision": "ALLOW", "reason": "SCOPE_AND_CONTENT_ALIGNED"},
                                   run_id="run-p15")
            report = ms.verify_replay(store, "run-p15")
            self.assertFalse(report["replay_matches"], "被篡改的判定必须被重放检出")
            self.assertEqual(report["counts"]["audited_decisions"], 1)
            self.assertEqual(report["mismatches"][0]["expected"]["decision"], "ALLOW")
            self.assertEqual(report["mismatches"][0]["recomputed"]["reason"],
                             "SCOPE_KEY_MISMATCH")


class BundleTests(Base):
    def _seed_run(self, store, run_id="run-p15"):
        run = store.create_run({"industry_pack_id": "auto", "question": fx.QUESTION,
                                "mode": "standard"}, owner_user_id="p15",
                               idempotency_key="phase15-%s" % run_id)
        return str(run["id"])

    def test_bundle_has_four_layers_and_is_contract_valid(self):
        with fx.temp_store("phase15-bundle.sqlite3") as (_database, store):
            run_id = self._seed_run(store)
            bundle = ms.replay_bundle(store, run_id)
            self.assertEqual(bundle["layers"], list(contracts.AUDIT_REPLAY_LAYERS))
            for layer in contracts.AUDIT_REPLAY_LAYERS:
                self.assertIn(layer, bundle)
            report = ms.verify_replay(store, run_id, bundle=bundle)
            self.assertEqual(report["counts"]["stages"],
                             bundle["execution"]["count"])
            self.assertTrue(report["redacted"])
            self.assertTrue(report["idempotent"])
            ok, note = validate("audit_replay", {key: report[key] for key in (
                "replay_version", "run_id", "layers", "counts", "replay_matches", "redacted")})
            self.assertTrue(ok, note)

    def test_replay_is_deterministic_between_runs(self):
        with fx.temp_store("phase15-bundle2.sqlite3") as (_database, store):
            run_id = self._seed_run(store)
            memory.apply_write_gate(store, [_candidate()], run_id=run_id)
            first = ms.verify_replay(store, run_id)
            second = ms.verify_replay(store, run_id)
            self.assertEqual(first["counts"], second["counts"])
            self.assertEqual(first["hops"], second["hops"])
            self.assertEqual(first["replay_matches"], second["replay_matches"])

    def test_missing_run_gives_empty_structures_without_raising(self):
        with fx.temp_store("phase15-bundle3.sqlite3") as (_database, store):
            report = ms.verify_replay(store, "no-such-run")
            self.assertEqual(report["counts"]["stages"], 0)
            self.assertEqual(report["counts"]["claims"], 0)
            self.assertEqual(report["missing"], [])
            self.assertTrue(report["replay_matches"])
            self.assertTrue(report["contract_ok"], report.get("contract_error"))
        empty = ms.replay_bundle(None, "x")
        self.assertEqual(empty["run_id"], "x")
        self.assertEqual(empty["memory"]["counts"]["decisions"], 0)

    def test_hop_checks_report_missing_references(self):
        """构造"引用对不上"的上下文：`evidence->context` 这一跳必须报缺。"""
        with fx.temp_store("phase15-bundle4.sqlite3") as (_database, store):
            run_id = self._seed_run(store)
            bundle = ms.replay_bundle(store, run_id)
            bundle["context"] = {"present": True, "pack_id": "P1", "citation_count": 1,
                                 "citations": [{"evidence_ref": "article:does-not-exist"}],
                                 "sections": []}
            report = ms.verify_replay(store, run_id, bundle=bundle)
            hop = [row for row in report["hops"] if row["hop"] == "evidence->context"][0]
            self.assertFalse(hop["ok"])
            self.assertIn("article:does-not-exist", hop["missing"])
            self.assertIn("evidence->context", report["missing"])


class CensusTests(Base):
    def test_census_reports_scope_distribution_and_violations(self):
        with fx.temp_store("phase15-census.sqlite3") as (_database, store):
            memory.apply_write_gate(store, [_candidate(),
                                            _candidate("另一个主体的长期登记信息",
                                                       scope="PATIENT_LONGITUDINAL")],
                                    run_id="run-p15")
            census = ms.scope_census(store)
            self.assertEqual(census["items"], len(store.load_memory_items(
                include_all_scopes=True)))
            self.assertEqual(sum(census["by_scope"].values()), census["items"])
            self.assertIn("longitudinal", census)
            self.assertEqual(census["scope_violations"]["count"], 0)
            self.assertTrue(census["settings"]["memory_safety_version"].startswith("qa-"))

    def test_census_on_empty_store(self):
        with fx.temp_store("phase15-census2.sqlite3") as (_database, store):
            census = ms.scope_census(store)
            self.assertEqual(census["items"], 0)
            self.assertIsNone(census["longitudinal"]["stale_rate"])
        self.assertEqual(ms.scope_census(None)["items"], 0)


if __name__ == "__main__":
    unittest.main()
