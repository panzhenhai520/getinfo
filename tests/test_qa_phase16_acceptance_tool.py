#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 16 验收工具用例（`tools/qa_phase16_acceptance.py`）。

它跑的是**完整验收链路**（在隔离临时 sqlite 上真造一次 run 的全部阶段产物，再逐项验收），
因此这一组用例本身就是本阶段的集成证据：
  · 六个视图全部 `ok` 且过契约；
  · 147 条口径数字 100% 命中来源白名单、100% 有口径说明；
  · 只读：跑一遍六视图 + 三个 HTTP 端点，行数逐字不变；
  · 空态：另一个全新空库上六个视图的状态与理由码；
  · 冻结面：七指纹复算不变、`QA_SCHEMA_VERSION` 仍 v11、无 v12。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, os.path.join(_ROOT, "tools"))

import qa_phase16_acceptance as acceptance  # noqa: E402


class AcceptanceToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = acceptance.run_acceptance()

    def test_acceptance_passes(self):
        self.assertEqual(self.report["acceptance"], "passed",
                         {key: value for key, value in self.report["checks"].items()
                          if not value})
        self.assertTrue(all(self.report["checks"].values()), self.report["checks"])

    def test_six_views_all_ok_on_real_artifacts(self):
        views = self.report["views"]
        self.assertEqual(set(views), set(acceptance.contracts.OBSERVABILITY_VIEWS))
        for name, item in views.items():
            self.assertEqual(item["state"], "ok", "%s 没有出数：%s" % (name, item))
            self.assertGreater(item["numbers"], 0, "%s 没有口径数字" % name)
        self.assertGreater(views["context"]["payload_items"], 0)
        self.assertGreater(views["execution"]["payload_items"], 0)
        self.assertGreater(views["search_trace"]["payload_items"], 0)

    def test_every_number_is_traceable_and_documented(self):
        trace = self.report["traceability"]
        self.assertGreater(trace["numbers"], 100, "口径数字太少，验收没有说服力")
        self.assertEqual(trace["ratio"], 1.0, trace["problems"])
        self.assertEqual(trace["definition_ratio"], 1.0, trace["problems"])
        self.assertEqual(trace["problems"], [])

    def test_read_only_row_counts_are_identical(self):
        self.assertEqual(self.report["row_counts_before"], self.report["row_counts_after"])
        self.assertTrue(self.report["checks"]["read_only_row_counts_unchanged"])

    def test_api_codes(self):
        api = self.report["api"]
        self.assertTrue(api["unauthenticated"], api)
        self.assertEqual(set(api["unauthenticated"].values()), {401})
        self.assertEqual(set(api["authenticated"].values()), {200}, api["authenticated"])
        self.assertEqual(api["unknown_view"], 400)
        self.assertEqual(api["methods"], ["GET"])

    def test_empty_states_are_explicit_on_a_fresh_database(self):
        empty = self.report["empty_states"]
        self.assertEqual(set(empty), set(acceptance.contracts.OBSERVABILITY_VIEWS))
        for name, item in empty.items():
            self.assertIn(item["state"], {"empty", "unavailable"}, name)
            self.assertTrue(item["state_reason"], name)
        self.assertEqual(empty["execution"]["state_reason"], "NO_STAGE_ARTIFACT")
        self.assertEqual(empty["context"]["state_reason"], "NO_STAGE_ARTIFACT")
        self.assertEqual(empty["dashboard"]["state_reason"], "NO_RUN_IN_SCOPE")
        self.assertEqual(empty["memory"]["state_reason"], "NO_MEMORY_IN_SCOPE")
        self.assertGreater(empty["dashboard"]["null_numbers"], 0,
                           "空库的看板必须有 null（不许把没记录说成 0）")

    def test_frozen_surface_is_untouched(self):
        frozen = self.report["frozen"]
        self.assertTrue(frozen["unchanged"], frozen["fingerprints"])
        self.assertEqual(frozen["schema_version"], "unified-qa-schema-v11")
        self.assertFalse(frozen["has_v12"])
        self.assertEqual(frozen["added_columns_v11"], [])

    def test_frontend_checks_are_all_true(self):
        checks = self.report["frontend_checks"]
        self.assertTrue(checks, "前端契约检查没有跑")
        self.assertTrue(all(checks.values()), checks)


if __name__ == "__main__":
    unittest.main()
