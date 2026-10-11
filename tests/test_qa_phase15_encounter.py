#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 15 · P15-02（encounter isolation）用例。

钉住 §14 的最后一段（"当前状态不得无条件跨轮次/跨会话当成当前事实"）：
  1. 作用域键的**轮次维**是可选的：不给就逐字保持 Phase 09 的键（老数据照样可见）；
  2. 九条可见/隔离理由码**全部可达**，逐条给出理由；
  3. 同一会话的**两次不同轮次**互相隔离（这正是本阶段新增的那一维）；
  4. 主体长期记忆跨轮次**允许**但标 `REVALIDATE_ON_USE`（交给 P15-03 的时效窗口）；
  5. 隔离回执**只留引用与指纹、不留正文**（MASTER_RULES 18），且 `leaks` 必须为空；
  6. 隔离在**真实召回链路**上生效：上一轮的当前状态不进这一轮的命中（集成用例）。
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

CONTEXT = ms.encounter_context(owner_user_id="p15", session_id="s1",
                               industry_pack_id="auto", encounter_id="e2", run_id="run-2")
FLAGS = ("QA_ENCOUNTER_ISOLATION", "QA_MEMORY_GRAPH")


def _item(scope, scope_key, **overrides):
    row = {"memory_id": "M-%s" % scope[:3], "memory_type": "VERIFIED_CLAIM",
           "canonical_content": "正文不进回执", "content_fingerprint": "F-%s" % scope[:3],
           "scope": scope, "scope_key": scope_key, "owner_user_id": "p15",
           "session_id": "s1", "industry_pack_id": "auto", "created_from_run_id": "run-1",
           "freshness_class": "LONG", "status": "ACTIVE"}
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


class VerdictTests(Base):
    def test_every_reason_is_reachable(self):
        cases = {
            "SAME_ENCOUNTER": _item("ENCOUNTER", CONTEXT["encounter_key"]),
            "SAME_SESSION": _item("SESSION", CONTEXT["session_key"]),
            "SAME_SUBJECT": _item("PATIENT_LONGITUDINAL", CONTEXT["subject_key"]),
            "SAME_ORGANIZATION": _item("ORGANIZATION", CONTEXT["organization_key"]),
            "GLOBAL": _item("GLOBAL_KNOWLEDGE", "global"),
            "OTHER_ENCOUNTER": _item("ENCOUNTER", "encounter:auto:s1:e9"),
            "OTHER_SESSION": _item("SESSION", "session:p15:s9:auto"),
            "OTHER_SUBJECT": _item("PATIENT_LONGITUDINAL", "subject:other",
                                   industry_pack_id="other"),
            "OTHER_ORGANIZATION": _item("ORGANIZATION", "org:other", owner_user_id="other"),
        }
        for expected, item in cases.items():
            row = ms.isolation_verdict(item, CONTEXT)
            self.assertEqual(row["reason"], expected, item["scope_key"])
            self.assertIn(expected, contracts.ENCOUNTER_ISOLATION_REASONS)

    def test_unknown_scope_is_isolated_not_guessed(self):
        row = ms.isolation_verdict({"memory_id": "M", "scope": "NOPE", "scope_key": "nope"}, CONTEXT)
        self.assertFalse(row["visible"])
        self.assertEqual(row["policy"], "BLOCK")

    def test_scope_is_recovered_from_the_key_when_the_field_is_dirty(self):
        dirty = {"memory_id": "M", "scope": "PATIENT", "scope_key": CONTEXT["subject_key"],
                 "industry_pack_id": "auto"}
        self.assertEqual(ms.scope_of_item(dirty), "PATIENT_LONGITUDINAL")
        self.assertEqual(ms.isolation_verdict(dirty, CONTEXT)["reason"], "SAME_SUBJECT")

    def test_subject_memory_crosses_encounters_but_requires_revalidation(self):
        item = _item("PATIENT_LONGITUDINAL", CONTEXT["subject_key"], session_id="s0",
                     created_from_run_id="run-0")
        row = ms.isolation_verdict(item, CONTEXT)
        self.assertTrue(row["visible"])
        self.assertTrue(row["cross_encounter"])
        self.assertEqual(row["policy"], "REVALIDATE_ON_USE")

    def test_encounter_axis_separates_two_runs_in_the_same_session(self):
        older = _item("ENCOUNTER", memory.scope_key("ENCOUNTER", owner_user_id="p15",
                                                    session_id="s1", industry_pack_id="auto",
                                                    encounter_id="e1"))
        newer = _item("ENCOUNTER", CONTEXT["encounter_key"])
        self.assertFalse(ms.isolation_verdict(older, CONTEXT)["visible"])
        self.assertEqual(ms.isolation_verdict(older, CONTEXT)["reason"], "OTHER_ENCOUNTER")
        self.assertTrue(ms.isolation_verdict(newer, CONTEXT)["visible"])

    def test_legacy_encounter_bucket_is_visible_but_requires_revalidation(self):
        legacy = _item("ENCOUNTER", CONTEXT["legacy_encounter_key"])
        row = ms.isolation_verdict(legacy, CONTEXT)
        self.assertTrue(row["visible"], "本阶段之前写入的轮次桶记忆必须照旧可见")
        self.assertTrue(row["legacy_bucket"])
        self.assertEqual(row["policy"], "REVALIDATE_ON_USE",
                         "键上没写轮次的老记忆可见，但不许当作当前事实直接用")


class ReceiptTests(Base):
    def test_receipt_counts_add_up_and_carries_no_content(self):
        rows = [_item("GLOBAL_KNOWLEDGE", "global"),
                _item("ENCOUNTER", "encounter:auto:s1:e9"),
                _item("SESSION", "session:p15:s9:auto"),
                _item("PATIENT_LONGITUDINAL", CONTEXT["subject_key"])]
        receipt = ms.isolate(rows, CONTEXT)
        self.assertEqual(receipt["checked"], 4)
        self.assertEqual(receipt["visible"] + receipt["isolated"], 4)
        self.assertEqual(receipt["isolated"], 2)
        self.assertEqual(receipt["isolated_by_reason"],
                         {"OTHER_ENCOUNTER": 1, "OTHER_SESSION": 1})
        self.assertEqual(receipt["leaks"], [])
        self.assertTrue(receipt["contract_ok"], receipt.get("contract_error"))
        rendered = repr(receipt)
        for row in rows:
            self.assertNotIn(str(row["canonical_content"]), rendered,
                             "隔离回执不许搬运正文（MASTER_RULES 18）")
        for ref in receipt["isolated_refs"]:
            self.assertTrue(ref["memory_id"])
            self.assertIn(ref["reason"], contracts.ENCOUNTER_ISOLATION_REASONS)

    def test_leak_check_catches_key_and_axis_disagreement(self):
        """键在可见集合里、但行的绑定列与上下文对不上 → 必须报 leak（不许静默放进命中）。"""
        forged = _item("PATIENT_LONGITUDINAL", CONTEXT["subject_key"], industry_pack_id="other")
        receipt = ms.isolate([forged], CONTEXT)
        self.assertEqual(receipt["leaks"], [forged["memory_id"]])
        clean = ms.isolate([_item("PATIENT_LONGITUDINAL", CONTEXT["subject_key"])], CONTEXT)
        self.assertEqual(clean["leaks"], [])
        self.assertTrue(clean["visible"])

    def test_switch_off_keeps_everything_visible_but_records_the_fact(self):
        os.environ["QA_ENCOUNTER_ISOLATION"] = "0"
        receipt = ms.isolate([_item("ENCOUNTER", "encounter:auto:s1:e9")], CONTEXT)
        self.assertEqual(receipt["isolated"], 0)
        self.assertFalse(receipt["settings"]["encounter_isolation"])

    def test_empty_input_is_contract_valid(self):
        receipt = ms.isolate([], CONTEXT)
        self.assertEqual((receipt["checked"], receipt["visible"], receipt["isolated"]), (0, 0, 0))
        self.assertTrue(receipt["contract_ok"], receipt.get("contract_error"))
        ok, note = validate("encounter_isolation", {key: receipt[key] for key in (
            "isolation_version", "checked", "visible", "isolated", "isolated_by_reason")})
        self.assertTrue(ok, note)


class RecallIntegrationTests(Base):
    def _write(self, store, *, scope, encounter_id, content=fx.CLAIM_TEXT, run_id="run-1"):
        candidate = {
            "memory_type": "VERIFIED_CLAIM", "canonical_content": content,
            "claim_id": "c1", "claim_type": "policy", "claim_confidence": 0.82,
            "freshness_class": "LONG", "valid_from": "", "valid_until": "",
            "entity_ids": ["家族办公室"], "supporting": 1,
            "evidence": [{"evidence_ref": "article:1", "source_fingerprint": "SF-1",
                          "span_fingerprint": "SP-1", "verdict": "SUPPORTED",
                          "evidence_score": 0.9, "relationship": "supports",
                          "run_id": run_id, "stage": "level1_retrieval", "route": "keyword",
                          "corpus_version": "corpus-p15", "metadata": {"article_id": 1}}],
            "scope": scope, "owner_user_id": "p15", "session_id": "s1",
            "industry_pack_id": "auto", "run_id": run_id, "metadata": {},
        }
        if encounter_id:
            candidate["encounter_id"] = encounter_id
        receipt = memory.apply_write_gate(store, [candidate], run_id=run_id)
        self.assertEqual(receipt["persisted"], 1, receipt["decisions"])
        return store.load_memory_items(include_all_scopes=True)[0]

    def test_previous_encounter_state_does_not_enter_the_next_recall(self):
        with fx.temp_store("phase15-encounter.sqlite3") as (_database, store):
            stored = self._write(store, scope="ENCOUNTER", encounter_id="run-1")
            self.assertEqual(stored["scope_key"], "encounter:auto:s1:run-1")
            # 同一会话的**下一轮**（run-2 → 轮次维 e2）：这条轮次记忆连召回都进不来
            receipt = memory.recall(store, query=fx.CLAIM_TEXT, owner_user_id="p15",
                                    session_id="s1", industry_pack_id="auto", run_id="run-2",
                                    encounter_id="run-2", now="2026-10-11T00:00:00Z")
            safety = ms.apply_recall_safety(
                store, receipt, run_meta={"id": "run-2", "owner_user_id": "p15",
                                          "session_id": "s1", "industry_pack_id": "auto"},
                record_audit=False)
            self.assertEqual(safety["hits"], [], "上一轮的轮次记忆不许进这一轮的命中")
            # 同一轮次（run-1）里它仍然可见 —— 隔离的是"跨轮次"，不是"这条记忆"
            same = ms.apply_recall_safety(
                store, memory.recall(store, query=fx.CLAIM_TEXT, owner_user_id="p15",
                                     session_id="s1", industry_pack_id="auto", run_id="run-1",
                                     encounter_id="run-1", now="2026-10-11T00:00:00Z"),
                run_meta={"id": "run-1", "owner_user_id": "p15", "session_id": "s1",
                          "industry_pack_id": "auto"}, record_audit=False)
            self.assertTrue(same["hits"], "同一轮次里它必须还能被召回")

    def test_legacy_rows_are_kept_but_flagged_for_revalidation(self):
        with fx.temp_store("phase15-encounter2.sqlite3") as (_database, store):
            stored = self._write(store, scope="ENCOUNTER", encounter_id="")
            self.assertEqual(stored["scope_key"], "encounter:auto:s1")
            safety = ms.apply_recall_safety(
                store, memory.recall(store, query=fx.CLAIM_TEXT, owner_user_id="p15",
                                     session_id="s1", industry_pack_id="auto", run_id="run-2",
                                     now="2026-10-11T00:00:00Z"),
                run_meta={"id": "run-2", "owner_user_id": "p15", "session_id": "s1",
                          "industry_pack_id": "auto"}, record_audit=False)
            self.assertTrue(safety["hits"], "老口径写入的轮次桶记忆不许被静默丢掉")
            self.assertEqual(safety["memory_safety"]["isolation"]["cross_encounter"], [],
                             "轮次桶不算'跨轮次放行的主体长期记忆'")

    def test_cross_encounter_state_is_isolated_at_the_safety_layer(self):
        """把一条**键属于别的轮次**的行直接喂给安全层：必须被拦下并给出理由码。"""
        with fx.temp_store("phase15-encounter3.sqlite3") as (_database, store):
            stored = self._write(store, scope="ENCOUNTER", encounter_id="e9")
            receipt = {"recall_version": "qa-memory-recall-v1", "mode": "evidence",
                       "channels": ["relational"], "counts": {}, "hits": [{
                           "memory_id": stored["memory_id"], "memory_type": "VERIFIED_CLAIM",
                           "canonical_content": stored["canonical_content"], "status": "ACTIVE",
                           "scope": "ENCOUNTER", "freshness_class": "LONG", "score": 0.5,
                           "factors": {}, "channels": ["relational"], "evidence_refs": [],
                           "hint": True, "requires_revalidation": True,
                           "verified_evidence": False, "explain": ""}]}
            safety = ms.apply_recall_safety(
                store, receipt, run_meta={"id": "run-2", "owner_user_id": "p15",
                                          "session_id": "s1", "industry_pack_id": "auto"},
                record_audit=False)
            self.assertEqual(safety["hits"], [], "别的轮次的行不许进命中")
            self.assertEqual(safety["memory_safety"]["excluded"]["total"], 1,
                             "同一行只算一次（三条闸门取并集）")
            self.assertEqual(safety["memory_safety"]["excluded"]["reasons"],
                             {"SCOPE_KEY_MISMATCH": 1, "OTHER_ENCOUNTER": 1},
                             "键与轴不一致的行同时被'作用域完整性复核'与'轮次隔离'指出")

    def test_legacy_rows_survive_the_scope_audit(self):
        """兼容口径：会话级轮次桶的行**不许**被作用域复核误判成越界。"""
        with fx.temp_store("phase15-encounter4.sqlite3") as (_database, store):
            stored = self._write(store, scope="ENCOUNTER", encounter_id="")
            audit = ms.audit_items_scope(
                [stored], owner_user_id="p15", session_id="s1", industry_pack_id="auto",
                encounter_id="run-2")
            self.assertEqual(audit["violations"], [])
            self.assertEqual(audit["quarantined"], [])


if __name__ == "__main__":
    unittest.main()
