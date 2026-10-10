#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 09 · P09-05 `lifecycle` 用例。

钉住：
  1. 衰减分是**纯函数**：同输入同输出；越旧越小；`valid_until` 一过就归零；
  2. 状态机**只自动产出** ACTIVE/STALE/EXPIRED；`SUPERSEDED`/`CONTRADICTED`/`REVOKED`
     一律**尊重**（不复活、不覆盖）——它们属 Phase 10/15；
  3. 迁移理由码只能在契约的迁移表里取（`MEMORY_LIFECYCLE_TRANSITIONS`）；
  4. 维护任务**幂等**：同一时钟连跑两次，第二次 transitions == 0（有守例）；
  5. 每次迁移都追加 `memory_version`（§2.3 不物理覆盖），`dry_run` 一行都不写；
  6. 分布（状态/衰减分桶）加总必须等于 checked。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_graph_contracts as contracts  # noqa: E402
import qa_memory as memory  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

import qa_phase09_fixtures as fx  # noqa: E402

NOW = "2026-10-11T00:00:00.000Z"


def _item(*, content="香港家族办公室税收优惠结论", freshness="LONG", confidence=0.8,
          status="ACTIVE", last_verified_at="2026-10-10T00:00:00.000Z", valid_until="",
          reuse_count=0, memory_id="MEM1", evidence=("article:1",)):
    return {
        "memory_id": memory_id, "memory_type": "VERIFIED_CLAIM", "canonical_content": content,
        "content_fingerprint": memory.content_fingerprint(content), "confidence": confidence,
        "freshness_class": freshness, "status": status, "scope": "PATIENT_LONGITUDINAL",
        "scope_key": memory.scope_key("PATIENT_LONGITUDINAL", industry_pack_id="auto"),
        "industry_pack_id": "auto", "reuse_count": reuse_count,
        "last_verified_at": last_verified_at, "valid_until": valid_until,
        "source_evidence_ids": list(evidence), "version": 1, "decay_score": 0.5,
        "created_at": last_verified_at, "metadata": {},
    }


class DecayTests(unittest.TestCase):
    def test_decay_is_deterministic_and_monotone_in_age(self):
        fresh = memory.decay_score(_item(last_verified_at="2026-10-10T00:00:00.000Z"), now=NOW)
        older = memory.decay_score(_item(last_verified_at="2026-01-10T00:00:00.000Z"), now=NOW)
        again = memory.decay_score(_item(last_verified_at="2026-01-10T00:00:00.000Z"), now=NOW)
        self.assertEqual(older, again, "同输入必须同输出")
        self.assertLess(older["score"], fresh["score"])
        self.assertGreater(older["age_days"], fresh["age_days"])

    def test_half_life_comes_from_the_freshness_class(self):
        short = memory.decay_score(_item(freshness="VERY_SHORT",
                                         last_verified_at="2026-10-01T00:00:00.000Z"), now=NOW)
        long_value = memory.decay_score(_item(freshness="LONG",
                                              last_verified_at="2026-10-01T00:00:00.000Z"), now=NOW)
        self.assertLess(short["score"], long_value["score"])
        self.assertEqual(short["half_life_days"], memory.HALF_LIFE_DAYS["VERY_SHORT"])

    def test_expired_valid_until_zeroes_the_score(self):
        decay = memory.decay_score(_item(valid_until="2026-10-01T00:00:00.000Z"), now=NOW)
        self.assertTrue(decay["expired"])
        self.assertEqual(decay["score"], 0.0)
        future = memory.decay_score(_item(valid_until="2027-10-01T00:00:00.000Z"), now=NOW)
        self.assertFalse(future["expired"])

    def test_reuse_slows_the_decay(self):
        plain = memory.decay_score(_item(last_verified_at="2026-06-10T00:00:00.000Z"), now=NOW)
        reused = memory.decay_score(_item(last_verified_at="2026-06-10T00:00:00.000Z",
                                          reuse_count=4), now=NOW)
        self.assertGreaterEqual(reused["score"], plain["score"])

    def test_missing_evidence_link_reduces_the_score(self):
        linked = memory.decay_score(_item(evidence=("article:1",)), now=NOW)
        unlinked = memory.decay_score(_item(evidence=()), now=NOW)
        self.assertLess(unlinked["score"], linked["score"])


class TransitionTests(unittest.TestCase):
    def test_active_becomes_stale_then_expired(self):
        # SHORT 的半衰期 90 天，距今 180 天 → 0.25；乘 0.8 置信 = 0.2 → STALE 档
        stale = memory.plan_transition(
            _item(freshness="SHORT", last_verified_at="2026-04-14T00:00:00.000Z"), now=NOW)
        self.assertEqual((stale["from"], stale["to"]), ("ACTIVE", "STALE"))
        self.assertEqual(stale["reason"], "DECAY_BELOW_STALE_FLOOR")
        expired = memory.plan_transition(
            _item(freshness="VERY_SHORT", last_verified_at="2026-01-01T00:00:00.000Z"), now=NOW)
        self.assertEqual(expired["to"], "EXPIRED")

    def test_stale_can_recover_to_active(self):
        plan = memory.plan_transition(_item(status="STALE", freshness="LONG", confidence=0.9,
                                            last_verified_at="2026-10-10T00:00:00.000Z"), now=NOW)
        self.assertEqual((plan["from"], plan["to"]), ("STALE", "ACTIVE"))
        self.assertEqual(plan["reason"], "DECAY_ABOVE_STALE_FLOOR")

    def test_valid_until_expiry_is_its_own_reason(self):
        plan = memory.plan_transition(_item(valid_until="2026-10-01T00:00:00.000Z"), now=NOW)
        self.assertEqual((plan["to"], plan["reason"]), ("EXPIRED", "VALID_UNTIL_PASSED"))

    def test_terminal_statuses_are_respected_never_revived(self):
        for status in ("SUPERSEDED", "CONTRADICTED", "REVOKED", "EXPIRED"):
            plan = memory.plan_transition(
                _item(status=status, last_verified_at="2020-01-01T00:00:00.000Z"), now=NOW)
            self.assertEqual(plan["to"], status)
            self.assertEqual(plan["reason"], "NO_CHANGE")

    def test_transition_table_is_the_single_source_of_truth(self):
        for before, after in (("ACTIVE", "STALE"), ("ACTIVE", "EXPIRED"), ("STALE", "EXPIRED"),
                              ("STALE", "ACTIVE"), ("EXPIRED", "EXPIRED")):
            self.assertTrue(memory.lifecycle_allowed_transition(before, after))
            self.assertIn((before, after), contracts.MEMORY_LIFECYCLE_TRANSITIONS)
        for before, after in (("REVOKED", "ACTIVE"), ("EXPIRED", "ACTIVE"),
                              ("SUPERSEDED", "ACTIVE"), ("CONTRADICTED", "ACTIVE")):
            self.assertFalse(memory.lifecycle_allowed_transition(before, after))


class ApplyLifecycleTests(unittest.TestCase):
    def _seed(self, store, **overrides):
        item = _item(**overrides)
        item["memory_id"] = memory.memory_id_for(
            scope="PATIENT_LONGITUDINAL", memory_type="VERIFIED_CLAIM",
            canonical_content=item["canonical_content"], industry_pack_id="auto")
        store.save_memory_item(item)
        return item["memory_id"]

    def test_lifecycle_writes_status_and_version_rows(self):
        with fx.temp_store() as (database, store):
            memory_id = self._seed(store, freshness="SHORT",
                                   last_verified_at="2026-04-14T00:00:00.000Z")
            report = memory.apply_lifecycle(store, now=NOW)
            self.assertEqual(report["checked"], 1)
            self.assertEqual(report["transitions"][0]["to"], "STALE")
            row = store.load_memory_items(memory_ids=[memory_id])[0]
            self.assertEqual(row["status"], "STALE")
            self.assertEqual(int(row["version"]), 2, "状态迁移必须追加版本")
            versions = database.connection.execute(
                "SELECT version, change FROM memory_version WHERE memory_id=? ORDER BY version",
                (memory_id,)).fetchall()
            self.assertEqual([str(item[1]) for item in versions], ["CREATE", "STATUS"])
            ok, note = validate("memory_lifecycle_report", {key: report[key] for key in (
                "lifecycle_version", "checked", "transitions")})
            self.assertTrue(ok, note)

    def test_second_run_with_the_same_clock_is_idempotent(self):
        with fx.temp_store() as (_database, store):
            self._seed(store, freshness="SHORT", last_verified_at="2026-04-14T00:00:00.000Z")
            first = memory.apply_lifecycle(store, now=NOW)
            second = memory.apply_lifecycle(store, now=NOW)
            self.assertEqual(len(first["transitions"]), 1)
            self.assertEqual(second["transitions"], [], "同一时钟连跑两次不该再动任何记忆")

    def test_expiry_transitions_use_the_expire_change_code(self):
        with fx.temp_store() as (database, store):
            memory_id = self._seed(store, valid_until="2026-10-01T00:00:00.000Z")
            report = memory.apply_lifecycle(store, now=NOW)
            self.assertEqual(report["transitions"][0]["reason"], "VALID_UNTIL_PASSED")
            changes = [str(row[0]) for row in database.connection.execute(
                "SELECT change FROM memory_version WHERE memory_id=? ORDER BY version",
                (memory_id,)).fetchall()]
            self.assertEqual(changes, ["CREATE", "EXPIRE"])

    def test_terminal_statuses_are_not_touched(self):
        with fx.temp_store() as (_database, store):
            memory_id = self._seed(
                store, content="被撤销的结论", status="REVOKED",
                last_verified_at="2020-01-01T00:00:00.000Z")
            report = memory.apply_lifecycle(store, now=NOW)
            self.assertEqual([row for row in report["transitions"]
                              if row["memory_id"] == memory_id], [])
            self.assertEqual(store.load_memory_items(memory_ids=[memory_id])[0]["status"], "REVOKED")

    def test_dry_run_writes_nothing(self):
        with fx.temp_store() as (_database, store):
            memory_id = self._seed(store, freshness="SHORT",
                                   last_verified_at="2026-04-14T00:00:00.000Z")
            report = memory.apply_lifecycle(store, now=NOW, dry_run=True)
            self.assertTrue(report["transitions"])
            self.assertTrue(report["dry_run"])
            self.assertEqual(store.load_memory_items(memory_ids=[memory_id])[0]["status"], "ACTIVE")

    def test_distributions_add_up(self):
        with fx.temp_store() as (_database, store):
            self._seed(store, content="结论甲", last_verified_at="2026-10-10T00:00:00.000Z")
            self._seed(store, content="结论乙", freshness="VERY_SHORT",
                       last_verified_at="2026-01-01T00:00:00.000Z")
            report = memory.apply_lifecycle(store, now=NOW)
            self.assertEqual(report["checked"], 2)
            self.assertEqual(sum(report["status_counts"].values()), 2)
            self.assertEqual(sum(report["decay_bands"].values()), 2)
            self.assertEqual(sum(report["transition_counts"].values()), len(report["transitions"]))

    def test_no_store_is_a_noop_report(self):
        report = memory.apply_lifecycle(None, now=NOW)
        self.assertEqual(report["checked"], 0)
        self.assertEqual(report["transitions"], [])
        self.assertTrue(report["contract_ok"])


if __name__ == "__main__":
    unittest.main()
