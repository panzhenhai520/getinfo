#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 10 · P10-06 `revoke/high-risk hook` 用例。

钉住：
  1. 四个选择器都能撤销：来源指纹 / 实体键 / 会话 / 显式 id（§11 "支持按 source/entity/session
     撤销污染 Memory"），且**只撤销命中的**；
  2. 撤销必须可追溯：`memory_version` 追加 REVOKE 行 + 一条 `EXPIRED_BY` 关系边
     （`target_kind` = source/entity/session/manual，`target_ref` = 选择器取值）；
  3. `REVOKED` 是终态：复验不复活、取代也拒绝（`supersession` 回 error）；
  4. **幂等**：第二遍 `revoked=0 / already_revoked=N`，版本行不再增长；
  5. 高危钩子给出口到动作的确定性映射（`DOWNGRADE_TO_STALE` / `KEEP` /
     `CONTRADICTION_HANDLES`），并且**不调用任何模型**（默认判定器就是 `rule`）；
  6. 失败路径：空选择器、未知 reason、未知记忆 id 都不抛异常，且回执可校验。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests"))

import qa_graph_contracts as contracts  # noqa: E402
import qa_memory_revalidation as mr  # noqa: E402

import qa_phase10_fixtures as fx  # noqa: E402


def _link(memory_id, *, source_fingerprint="SF-1", evidence_ref="article:1",
          corpus_version="corpus-1"):
    return {"memory_id": memory_id, "evidence_ref": evidence_ref,
            "source_fingerprint": source_fingerprint, "span_fingerprint": "SPAN-1",
            "corpus_version": corpus_version, "verdict": "SUPPORTED", "evidence_score": 0.8,
            "metadata": {}}


class RevokeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = fx.temp_store()
        self.database, self.store = self._tmp.__enter__()
        self.assertEqual(self.database.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        fx.write_items(self.store, [
            fx.memory_item(fx.SUPPORTED_CLAIM, memory_id="MEM-a", entity_ids=["hk"],
                           session_id="s10"),
            fx.memory_item(fx.SUPPORTED_CLAIM_2, memory_id="MEM-b", entity_ids=["sg"],
                           session_id="s11"),
            fx.memory_item(fx.NEGATED_CLAIM, memory_id="MEM-c", entity_ids=["hk"],
                           session_id="s10"),
        ])
        self.store.link_memory_evidence("MEM-a", [_link("MEM-a", source_fingerprint="SF-1")])
        self.store.link_memory_evidence("MEM-b", [_link("MEM-b", source_fingerprint="SF-2")])

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_revoke_by_source_fingerprint(self):
        receipt = mr.revoke_memories(self.store, reason="SOURCE_CONTAMINATED",
                                     source_fingerprint="SF-1", run_id="run-1")
        self.assertTrue(receipt["contract_ok"], receipt.get("contract_error"))
        self.assertEqual(receipt["reason"], "SOURCE_CONTAMINATED")
        self.assertEqual([row["memory_id"] for row in receipt["revoked"]], ["MEM-a"])
        self.assertEqual(fx.load(self.store, "MEM-a")["status"], "REVOKED")
        self.assertEqual(fx.load(self.store, "MEM-b")["status"], "ACTIVE")
        edges = self.store.memory_relations(["MEM-a"], relations=["EXPIRED_BY"])
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["target_kind"], "source")
        self.assertEqual(edges[0]["target_ref"], "SF-1")
        versions = fx.version_rows(self.store, "MEM-a")
        self.assertEqual(versions[-1]["change"], "REVOKE")

    def test_revoke_by_entity_key(self):
        receipt = mr.revoke_memories(self.store, reason="ENTITY_CONTAMINATED",
                                     entity_key="hk", run_id="run-1")
        self.assertEqual([row["memory_id"] for row in receipt["revoked"]], ["MEM-a", "MEM-c"])
        self.assertEqual(fx.load(self.store, "MEM-b")["status"], "ACTIVE")
        edges = self.store.memory_relations(["MEM-c"], relations=["EXPIRED_BY"])
        self.assertEqual(edges[0]["target_kind"], "entity")
        self.assertEqual(edges[0]["target_ref"], "hk")

    def test_revoke_by_session(self):
        receipt = mr.revoke_memories(self.store, reason="SESSION_CONTAMINATED",
                                     session_id="s11", run_id="run-1")
        self.assertEqual([row["memory_id"] for row in receipt["revoked"]], ["MEM-b"])
        edges = self.store.memory_relations(["MEM-b"], relations=["EXPIRED_BY"])
        self.assertEqual(edges[0]["target_kind"], "session")

    def test_revoke_by_explicit_ids(self):
        receipt = mr.revoke_memories(self.store, reason="MANUAL_REVOKE",
                                     memory_ids=["MEM-b", "MEM-c"], run_id="run-1")
        self.assertEqual(sorted(row["memory_id"] for row in receipt["revoked"]),
                         ["MEM-b", "MEM-c"])
        self.assertEqual(fx.load(self.store, "MEM-a")["status"], "ACTIVE")
        edges = self.store.memory_relations(["MEM-b"], relations=["EXPIRED_BY"])
        self.assertEqual(edges[0]["target_kind"], "manual")

    def test_selectors_are_unioned_and_deduplicated(self):
        receipt = mr.revoke_memories(self.store, reason="MANUAL_REVOKE", entity_key="hk",
                                     memory_ids=["MEM-a"], run_id="run-1")
        self.assertEqual(receipt["checked"], 2, "并集去重：MEM-a 只出现一次")
        self.assertEqual(sorted(row["memory_id"] for row in receipt["revoked"]),
                         ["MEM-a", "MEM-c"])

    def test_revoke_is_idempotent(self):
        first = mr.revoke_memories(self.store, reason="SOURCE_CONTAMINATED",
                                   source_fingerprint="SF-1", run_id="run-1")
        versions = len(fx.version_rows(self.store, "MEM-a"))
        edges = len(self.store.memory_relations(["MEM-a"], relations=["EXPIRED_BY"]))
        second = mr.revoke_memories(self.store, reason="SOURCE_CONTAMINATED",
                                    source_fingerprint="SF-1", run_id="run-1")
        self.assertEqual(len(first["revoked"]), 1)
        self.assertEqual(len(second["revoked"]), 0)
        self.assertEqual(second["already_revoked"], 1)
        self.assertEqual([row["reason"] for row in second["skipped"]], ["ALREADY_REVOKED"])
        self.assertEqual(len(fx.version_rows(self.store, "MEM-a")), versions)
        self.assertEqual(len(self.store.memory_relations(["MEM-a"], relations=["EXPIRED_BY"])),
                         edges)

    def test_revoked_is_terminal(self):
        mr.revoke_memories(self.store, reason="MANUAL_REVOKE", memory_ids=["MEM-a"])
        revalidation = mr.revalidate_item(
            self.store, fx.load(self.store, "MEM-a"),
            current_evidence=[fx.verified_evidence("article:1")], run_id="run-1", now=fx.NOW)
        self.assertEqual(revalidation["outcome"], "SKIPPED_NOT_ACTIVE")
        self.assertEqual(fx.load(self.store, "MEM-a")["status"], "REVOKED")
        supersession = mr.supersede_memory(self.store, "MEM-a", "MEM-b")
        self.assertFalse(supersession["changed"])
        self.assertIn("REVOKED", supersession["error"])

    def test_receipt_reports_status_distribution(self):
        mr.revoke_memories(self.store, reason="MANUAL_REVOKE", memory_ids=["MEM-a"])
        receipt = mr.revoke_memories(self.store, reason="MANUAL_REVOKE", memory_ids=["MEM-b"])
        self.assertEqual(receipt["status_counts"], {"ACTIVE": 1, "REVOKED": 2})

    def test_no_match_and_unknown_ids_are_reported(self):
        receipt = mr.revoke_memories(self.store, reason="MANUAL_REVOKE",
                                     source_fingerprint="SF-nope")
        self.assertEqual((receipt["checked"], receipt["revoked"]), (0, []))
        receipt = mr.revoke_memories(self.store, reason="MANUAL_REVOKE",
                                     memory_ids=["MEM-ghost"])
        self.assertEqual(receipt["skipped"][0]["reason"], "MEMORY_NOT_FOUND")
        receipt = mr.revoke_memories(self.store, reason="MADE_UP", memory_ids=["MEM-a"])
        self.assertEqual(receipt["reason"], "MANUAL_REVOKE", "未知理由码回落人工撤销")

    def test_contract_and_reason_enum(self):
        for reason in contracts.MEMORY_REVOKE_REASONS:
            receipt = mr.revoke_memories(self.store, reason=reason, memory_ids=[],
                                         write=False)
            self.assertIn(receipt["reason"], contracts.MEMORY_REVOKE_REASONS)
            self.assertTrue(receipt["contract_ok"], receipt.get("contract_error"))
            self.assertEqual(receipt["revoke_version"], contracts.MEMORY_REVOKE_VERSION)


class HighRiskHookTests(unittest.TestCase):
    def test_hook_action_mapping(self):
        item = fx.memory_item(claim_type="policy", freshness="VERSION_SENSITIVE")
        cases = {"REVALIDATED": "KEEP", "UNVERIFIED": "DOWNGRADE_TO_STALE",
                 "NO_CANDIDATE_EVIDENCE": "DOWNGRADE_TO_STALE",
                 "REFUTED": "CONTRADICTION_HANDLES", "": "KEEP"}
        for outcome, action in cases.items():
            hook = mr.run_high_risk_hook(item, outcome=outcome)
            self.assertEqual(hook["action"], action, outcome)
            self.assertTrue(hook["high_stakes"])
        plain = fx.memory_item(claim_type="background", freshness="LONG")
        self.assertEqual(mr.run_high_risk_hook(plain, outcome="UNVERIFIED")["action"], "KEEP")

    def test_hook_records_reasons_and_judge(self):
        hook = mr.run_high_risk_hook(fx.memory_item(claim_type="policy"), outcome="UNVERIFIED",
                                     context={"selector": {"entity_key": "hk"}})
        self.assertIn("CLAIM_TYPE:policy", hook["reasons"])
        self.assertEqual(hook["judge"], "rule")
        self.assertEqual(hook["context"]["selector"]["entity_key"], "hk")

    def test_hook_does_not_call_any_model(self):
        source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "qa_memory_revalidation.py"), encoding="utf-8").read()
        for forbidden in ("openai", "requests.", "httpx", "urllib", "http.client", "anthropic",
                          "dashscope", "bge-m3", "sentence_transformers"):
            self.assertNotIn(forbidden, source, "复验链路不许出现模型/网络调用：%s" % forbidden)


if __name__ == "__main__":
    unittest.main(verbosity=2)
