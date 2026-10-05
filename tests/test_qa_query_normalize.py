# -*- coding: utf-8 -*-
"""多语言/繁简归一 + 时间窗口解析的回归测试。"""
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, r"F:\CollectInfo")

import qa_query_normalize as qn  # noqa: E402

# 用户实测漏检的那篇文章标题（繁体）
TRAD_TITLE = "工信部成立人形機器人與具身智能標準化技術委員會"
TRAD_BODY = "工信部於2025年12月26日在北京成立人形機器人與具身智能標準化技術委員會，秘書處設在中國電子學會。"


class SimplifyTest(unittest.TestCase):
    def test_traditional_title_becomes_simplified(self):
        self.assertEqual(
            qn.to_simplified("人形機器人與具身智能標準化技術委員會"),
            "人形机器人与具身智能标准化技术委员会",
        )

    def test_simplified_query_matches_traditional_article(self):
        # 核心回归：简体问题词必须能命中繁体正文
        query = qn.normalize_text("最近工信部在人形机器人领域有什么动态")
        article = qn.normalize_text(TRAD_TITLE + TRAD_BODY)
        self.assertIn("人形机器人", query)
        self.assertIn("人形机器人", article)
        self.assertIn("工信部", article)


class EntityTest(unittest.TestCase):
    def test_english_question_expands_to_chinese_entities(self):
        # 英文提问也要能展开出中文实体
        terms = qn.expand_entity_terms("What did MIIT say about humanoid robots recently?")
        joined = " ".join(terms)
        self.assertIn("工信部", joined)
        self.assertIn("人形机器人", joined)

    def test_chinese_question_expands_to_english_entities(self):
        terms = qn.expand_entity_terms("工信部在人形机器人方面有什么动态")
        joined = " ".join(terms)
        self.assertIn("MIIT", joined)
        self.assertIn("humanoid robot", joined)

    def test_search_texts_contain_original_normalized_and_aliases(self):
        pack = qn.expand_query("工信部 人形机器人 最新动态")
        self.assertIn("工信部 人形机器人 最新动态", pack["search_texts"])
        self.assertTrue(any("MIIT" == t for t in pack["search_texts"]))
        self.assertTrue(any("humanoid" in t.lower() for t in pack["search_texts"]))


class TimeWindowTest(unittest.TestCase):
    NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)

    def test_recent_defaults_to_configurable_window(self):
        w = qn.parse_time_window("最近工信部在人形机器人领域有什么动态", now=self.NOW)
        self.assertTrue(w["has_time"])
        self.assertEqual(w["days"], qn.default_window_days())
        self.assertEqual(w["source"], "default_recent")
        self.assertIn("最近 = 近", w["label"])          # 窗口要能回写给用户
        self.assertEqual(w["start"].strftime("%Y-%m-%d"), "2026-07-07")

    def test_explicit_relative_window(self):
        w = qn.parse_time_window("近一个月工信部的动态", now=self.NOW)
        self.assertEqual(w["days"], 30)
        self.assertEqual(w["source"], "relative")

    def test_absolute_month_and_year(self):
        self.assertEqual(qn.parse_time_window("2026年1月21日工信部说了什么", now=self.NOW)["label"], "2026 年 1 月")
        self.assertEqual(qn.parse_time_window("2025年的政策", now=self.NOW)["label"], "2025 年")

    def test_this_week_and_month(self):
        self.assertEqual(qn.parse_time_window("本周动态", now=self.NOW)["source"], "week")
        self.assertEqual(qn.parse_time_window("本月有什么", now=self.NOW)["source"], "month")

    def test_no_time_marker(self):
        w = qn.parse_time_window("工信部的职责是什么", now=self.NOW)
        self.assertFalse(w["has_time"])
        self.assertIsNone(w["days"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
