#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 10 追加用例：一条记忆出现在多对矛盾里时的**规范后继**挑选（真机数据形态）。

真机（A 机真实 claims 离线重建的 82 条记忆）上出现过这种形态：一条较老的记忆同时与
三条更新的同主体反义记忆冲突。`superseded_by` 只有一个字段，所以必须**确定性**地挑一个
规范后继（时间最晚；并列取 memory_id 最小），否则取代链会随配对顺序漂移、同钟重放还会
反复写版本行（这正是本用例集的由来）。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_memory_revalidation as mr  # noqa: E402

import qa_phase10_fixtures as fx  # noqa: E402


class CanonicalSuccessorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = fx.temp_store()
        self.database, self.store = self._tmp.__enter__()
        self.assertEqual(self.database.backend, "sqlite", "测试必须跑在隔离 sqlite 上")

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_single_successor_is_the_latest_winner(self):
        # 一条"最老"的记忆 + 三条比它新的同主体反义记忆（复现真机上的一对多形态）。
        # 三条反义说法各用一个**契约词表内**的否定短语（不予/不得/没有），
        # 保证 `check_negation` 判出"极性相反且指向同一话题"。
        fx.write_items(self.store, [
            fx.memory_item(fx.SUPPORTED_CLAIM, entity_ids=["hk"], valid_from="2026-01-01",
                           memory_id="MEM-oldest"),
            fx.memory_item(fx.NEGATED_CLAIM, entity_ids=["hk"], valid_from="2026-05-01",
                           memory_id="MEM-mid"),
            fx.memory_item(fx.NEGATED_CLAIM.replace("不予给予", "不得给予"), entity_ids=["hk"],
                           valid_from="2026-08-01", memory_id="MEM-new"),
            fx.memory_item(fx.NEGATED_CLAIM.replace("不予给予", "没有给予"), entity_ids=["hk"],
                           valid_from="2026-09-01", memory_id="MEM-newest"),
        ])
        report = mr.detect_memory_contradictions(self.store, run_id="run-1", now=fx.NOW)
        self.assertGreaterEqual(report["candidates"], 3, "最老的那条应当与三条新的都构成矛盾")
        versions = fx.version_rows(self.store, "MEM-oldest")
        self.assertEqual(len(versions), 2, "规范后继只有一个 → 只追加一行取代版本")
        self.assertEqual(versions[-1]["change"], "SUPERSEDE")
        self.assertEqual(fx.load(self.store, "MEM-oldest")["superseded_by"], "MEM-newest",
                         "并列规则：生效时间最晚的那个胜出")
        self.assertEqual(report["supersessions"], 1)

    def test_replay_writes_nothing_new(self):
        fx.write_items(self.store, [
            fx.memory_item(fx.SUPPORTED_CLAIM, entity_ids=["hk"], valid_from="2026-01-01",
                           memory_id="MEM-oldest"),
            fx.memory_item(fx.NEGATED_CLAIM, entity_ids=["hk"], valid_from="2026-05-01",
                           memory_id="MEM-mid"),
            fx.memory_item(fx.NEGATED_CLAIM.replace("不予给予", "不得给予"), entity_ids=["hk"],
                           valid_from="2026-08-01", memory_id="MEM-new"),
        ])
        first = mr.detect_memory_contradictions(self.store, run_id="run-1", now=fx.NOW)
        versions = len(fx.version_rows(self.store, "MEM-oldest"))
        second = mr.detect_memory_contradictions(self.store, run_id="run-1", now=fx.NOW)
        self.assertEqual(len(fx.version_rows(self.store, "MEM-oldest")), versions,
                         "同钟重放不许再写版本行")
        self.assertEqual(second["supersessions"], 0)
        self.assertEqual(first["by_reason_code"], second["by_reason_code"])
        self.assertEqual(first["candidates"], second["candidates"])

    def test_tie_breaks_on_the_smallest_memory_id(self):
        fx.write_items(self.store, [
            fx.memory_item(fx.SUPPORTED_CLAIM, entity_ids=["sg"], valid_from="2026-01-01",
                           memory_id="MEM-base"),
            fx.memory_item(fx.NEGATED_CLAIM, entity_ids=["sg"], valid_from="2026-07-01",
                           memory_id="MEM-bbb"),
            fx.memory_item(fx.NEGATED_CLAIM.replace("不予给予", "不得给予"), entity_ids=["sg"],
                           valid_from="2026-07-01", memory_id="MEM-aaa"),
        ])
        report = mr.detect_memory_contradictions(self.store, run_id="run-1", now=fx.NOW)
        self.assertGreaterEqual(report["candidates"], 2)
        self.assertEqual(fx.load(self.store, "MEM-base")["superseded_by"], "MEM-aaa",
                         "生效时间并列时取 memory_id 更小的后继（确定性）")

    def test_older_winner_does_not_supersede_the_newer_loser(self):
        """只有"较新的一方"才允许取代：败方比胜方新时不许走取代（§11 的时间裁决语义）。"""
        fx.write_items(self.store, [
            fx.memory_item(fx.NEGATED_CLAIM, entity_ids=["hk"], valid_from="2026-01-01",
                           memory_id="MEM-old-neg"),
            fx.memory_item(fx.SUPPORTED_CLAIM, entity_ids=["hk"], valid_from="2026-09-01",
                           memory_id="MEM-new-pos"),
        ])
        report = mr.detect_memory_contradictions(self.store, run_id="run-1", now=fx.NOW)
        self.assertEqual(report["candidates"], 1)
        row = report["contradictions"][0]
        self.assertEqual(row["reason_code"], "NEWER_VERSION_PRECEDES")
        self.assertEqual(row["loser_memory_id"], "MEM-old-neg")
        self.assertEqual(row["supersede_to"], "MEM-new-pos")
        self.assertEqual(fx.load(self.store, "MEM-old-neg")["status"], "SUPERSEDED")
        self.assertEqual(fx.load(self.store, "MEM-new-pos")["status"], "ACTIVE")

    def test_loser_on_the_right_side_is_also_superseded(self):
        """胜方在左、败方在右时同样要取代（方向对称，pair 顺序只按 id 排）。"""
        fx.write_items(self.store, [
            fx.memory_item(fx.NEGATED_CLAIM, entity_ids=["hk"], valid_from="2026-12-01",
                           memory_id="MEM-aaa-new"),
            fx.memory_item(fx.SUPPORTED_CLAIM, entity_ids=["hk"], valid_from="2026-01-01",
                           memory_id="MEM-zzz-old"),
        ])
        report = mr.detect_memory_contradictions(self.store, run_id="run-1", now=fx.NOW)
        row = report["contradictions"][0]
        self.assertEqual(row["winner"], "left")
        self.assertEqual(row["loser_memory_id"], "MEM-zzz-old")
        self.assertEqual(fx.load(self.store, "MEM-zzz-old")["status"], "SUPERSEDED")
        self.assertEqual(fx.load(self.store, "MEM-zzz-old")["superseded_by"], "MEM-aaa-new")

    def test_contradicted_and_superseded_never_fight_over_one_memory(self):
        """同一记忆既被时间裁决判负、又被未消解判矛盾时，取代优先（更强的结论）。"""
        fx.write_items(self.store, [
            fx.memory_item(fx.SUPPORTED_CLAIM, entity_ids=["hk"], valid_from="2026-01-01",
                           memory_id="MEM-oldest"),
            fx.memory_item(fx.NEGATED_CLAIM, entity_ids=["hk"], valid_from="2026-09-01",
                           memory_id="MEM-newest"),
            fx.memory_item(fx.NEGATED_CLAIM.replace("不予给予", "不得给予"), entity_ids=["hk"],
                           valid_from="2026-01-01", memory_id="MEM-oldest-2"),
        ])
        report = mr.detect_memory_contradictions(self.store, run_id="run-1", now=fx.NOW)
        statuses = {row["memory_id"]: row["status"] for row in
                    self.store.load_memory_items(include_all_scopes=True, limit=100)}
        self.assertEqual(statuses["MEM-oldest"], "SUPERSEDED")
        self.assertEqual(statuses["MEM-newest"], "ACTIVE")
        plans = {row["memory_id"]: row["status"] for row in
                 self.store.load_memory_items(include_all_scopes=True, limit=100)}
        for memory_id, plan in plans.items():
            self.assertNotEqual(plan, "SUPERSEDED_AND_CONTRADICTED")
        actions = {action.get("memory_id") for row in report["contradictions"]
                   for action in row["status_actions"]}
        self.assertIn("MEM-oldest", actions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
