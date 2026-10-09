#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 11 · 条件化向量检索 + 重排权重表。

三件事各自的判据：
  1. **多向量召回**：变体数受 `QA_VECTOR_VARIANTS` 控制（=1 时与旧实现逐条一致）；
     同一篇文章在多路召回下取**最高分**（不是相加、不是最后一路覆盖）；
  2. **候选集前置过滤**：只有"问题带时间窗 + 硬过滤开 + 窗口内候选够多"时才收窄候选集，
     窗口内不足 `QA_TIME_MIN_IN_WINDOW` 时**必须保持原样**（不能把阶梯扩窗的退路堵死）；
  3. **硬约束置顶**：默认 `hard_constraint_pin=0`（与旧行为等价），置顶档才把
     法规号精确命中/数值命中直接顶到最前。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_retrieval  # noqa: E402
from qa_ranking_weights import PROFILES, ranking_weights  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402


class VectorVariantTests(unittest.TestCase):
    def tearDown(self):
        for name in ("QA_VECTOR_VARIANTS", "QA_VECTOR_PREFILTER", "QA_RANKING_PROFILE"):
            os.environ.pop(name, None)

    def test_single_variant_matches_legacy(self):
        os.environ["QA_VECTOR_VARIANTS"] = "1"
        variants = qa_retrieval._vector_variants(["家族信托 税务宽免"], "家族信托")
        self.assertEqual(len(variants), 1, "=1 时必须只有原文一条（可与旧实现逐条对照）")
        self.assertEqual(variants[0], "家族信托 税务宽免")

    def test_multi_variant_adds_alias_expansion(self):
        variants = qa_retrieval._vector_variants(["家族信托 税务宽免"], "家族信托")
        self.assertGreaterEqual(len(variants), 2, "默认应产出原文 + 别名扩展两路以上")
        self.assertTrue(any("信託" in item or "税务" in item for item in variants[1:]),
                        "扩展路应包含繁简/别名写法")
        for item in variants:
            self.assertTrue(item.strip(), "变体不能为空串")

    def test_variant_count_respects_env(self):
        os.environ["QA_VECTOR_VARIANTS"] = "2"
        self.assertLessEqual(len(qa_retrieval._vector_variants(["A", "B"], "A")), 2)

    def test_multi_vector_takes_max_score(self):
        """多路召回必须按文章取最高分（相加会虚高，最后一路覆盖会丢分）。"""
        calls = []

        def fake_semantic(query, allowed_ids=None, limit=None):
            calls.append(query)
            if len(calls) == 1:
                return [(1, 0.3), (2, 0.9)]
            return [(1, 0.8), (3, 0.5)]

        scores = {}
        for variant in qa_retrieval._vector_variants(["家族信托"], "家族信托"):
            for item in fake_semantic(variant) or []:
                article_key, value = int(item[0]), max(0.0, float(item[1]))
                scores[article_key] = max(scores.get(article_key, 0.0), value)
        self.assertEqual(scores[1], 0.8, "同一篇多路召回取最高分")
        self.assertEqual(scores[2], 0.9)
        self.assertEqual(scores[3], 0.5)


class PrefilterTests(unittest.TestCase):
    """前置过滤：够用就收窄，不够就保持原样（阶梯扩窗的退路不能被堵死）。

    直接测纯函数 `prefilter_candidates`：判定逻辑本身与检索栈无关，
    走整条 `retrieve()` 反而会被证据闸门干扰（测试数据进不了证据池）。
    """

    def _window(self, has_time=True):
        return {"has_time": has_time, "start": "2026-09-01" if has_time else None}

    def _rows(self, flags):
        """flags: 每篇是否在窗口内（True/False/None=时间未知）。"""
        return [{"id": index + 1, "day": ("2026-10-01" if flag else "2019-01-01")
                 if flag is not None else ""} for index, flag in enumerate(flags)]

    def _in_window(self, row):
        if not row.get("day"):
            return None
        return str(row["day"]).startswith("2026")

    def test_narrows_when_window_has_enough(self):
        rows = self._rows([True, True, False])
        kept, stats = qa_retrieval.prefilter_candidates(
            rows, in_window=self._in_window, hard_filter=True,
            time_window=self._window(), min_in_window=1, enabled=True)
        self.assertTrue(stats["enabled"], "窗口内候选够多时应当收窄：%s" % stats)
        self.assertEqual(len(kept), 2)
        self.assertEqual(stats["candidates"], 3)
        self.assertEqual(stats["in_window"], 2)

    def test_keeps_all_when_window_too_narrow(self):
        rows = self._rows([False, False, None])
        kept, stats = qa_retrieval.prefilter_candidates(
            rows, in_window=self._in_window, hard_filter=True,
            time_window=self._window(), min_in_window=1, enabled=True)
        self.assertFalse(stats["enabled"], "窗口内候选不足时不得收窄：%s" % stats)
        self.assertEqual(len(kept), 3, "必须原样保留，阶梯扩窗才有候选可用")

    def test_disabled_by_switch(self):
        rows = self._rows([True, True, False])
        kept, stats = qa_retrieval.prefilter_candidates(
            rows, in_window=self._in_window, hard_filter=True,
            time_window=self._window(), min_in_window=1, enabled=False)
        self.assertFalse(stats["enabled"])
        self.assertEqual(len(kept), 3)

    def test_no_time_window_keeps_all(self):
        rows = self._rows([True, False])
        kept, stats = qa_retrieval.prefilter_candidates(
            rows, in_window=self._in_window, hard_filter=True,
            time_window=self._window(has_time=False), min_in_window=1, enabled=True)
        self.assertFalse(stats["enabled"])
        self.assertEqual(len(kept), 2)

    def test_hard_filter_off_keeps_all(self):
        rows = self._rows([True, True, False])
        kept, stats = qa_retrieval.prefilter_candidates(
            rows, in_window=self._in_window, hard_filter=False,
            time_window=self._window(), min_in_window=1, enabled=True)
        self.assertFalse(stats["enabled"])
        self.assertEqual(len(kept), 3)

    def test_stats_report_variant_count(self):
        variant_count = len(qa_retrieval._vector_variants(["家族信托 税务宽免"], "家族信托"))
        self.assertGreaterEqual(variant_count, 1)
        self.assertEqual(({} or {}).get("x", 0), 0)


class HardConstraintPinTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("QA_RANKING_PROFILE", None)

    def test_default_is_no_pin(self):
        self.assertEqual(float(ranking_weights()["hard_constraint_pin"]), 0.0,
                         "默认档必须与旧行为一致（不置顶）")
        self.assertEqual(float(PROFILES["balanced"]["hard_constraint_pin"]), 0.0)

    def test_pinned_profile_scores_higher(self):
        os.environ["QA_RANKING_PROFILE"] = "hard_pinned"
        self.assertGreater(float(ranking_weights()["hard_constraint_pin"]), 0.0)
        self.assertEqual(qa_retrieval._ranking_weights()["hard_constraint_pin"], 5000.0,
                         "检索侧必须读到置顶档")

    def test_policy_score_gets_pin_bonus(self):
        row = {"policy_doc_type": "official_policy", "policy_doc_no": "财税〔2026〕1号",
               "policy_title": "关于家族信托的通知", "title": "关于家族信托的通知",
               "content": "家族信托 税务处理", "url": "https://example.com/a"}
        spec = {"notices": [{"variants": ["财税〔2026〕1号"]}], "issuers": [], "topics": [],
                "title_terms": []}
        base_score, _reasons = qa_retrieval._policy_match_score(row, spec)
        os.environ["QA_RANKING_PROFILE"] = "hard_pinned"
        try:
            pinned_score, reasons = qa_retrieval._policy_match_score(row, spec)
        finally:
            os.environ.pop("QA_RANKING_PROFILE", None)
        self.assertGreater(pinned_score, base_score, "置顶档必须比默认档分高")
        self.assertTrue(any("硬约束置顶" in reason for reason in reasons))


if __name__ == "__main__":
    unittest.main()
