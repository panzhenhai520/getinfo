import unittest
from unittest.mock import patch

import chat_api


def _fake_rows():
    return [
        {
            "id": 1,
            "title": "网络安全漏洞年度报告发布",
            "domain": "example.com",
            "publish_date": "2026-09-01",
            "first_crawled": "2026-09-02 10:00:00",
            "url": "https://example.com/a",
            "preview": "本报告聚焦网络安全与工控安全年度态势。",
            "matched_keywords_json": '["网络安全"]',
            "topic_tags_json": '["网络安全"]',
            "final_category": "trend",
            "trend_summary": "",
            "_packs": ["bolean_security_compute"],
        }
    ]


class ChatRetrievalGateTests(unittest.TestCase):
    """问题①级门禁：身份/寒暄类问题不检索文章库、不下发召回"""

    def test_identity_questions_skip_retrieval(self):
        for question in ("你是什么模型？", "你是谁？", "你叫什么名字", "介绍一下你自己",
                         "你能做什么", "你会什么", "你是什么模型，介绍一下自己"):
            self.assertFalse(chat_api._needs_article_retrieval(question), question)

    def test_greeting_and_short_questions_skip(self):
        for question in ("你好", "谢谢", "在吗", "再见", "hi", ""):
            self.assertFalse(chat_api._needs_article_retrieval(question), question)

    def test_substantive_questions_retrieve(self):
        for question in ("最近网络安全领域有什么重要动态？", "新能源汽车销量如何",
                         "帮我总结一下工业互联网大会的主要内容"):
            self.assertTrue(chat_api._needs_article_retrieval(question), question)

    def test_gate_can_be_disabled_by_config(self):
        with patch.object(chat_api._cfg, "INTEL_CHAT_RETRIEVAL_GATE_ENABLED", False):
            self.assertTrue(chat_api._needs_article_retrieval("你是什么模型？"))

    def test_gate_skip_returns_empty_context(self):
        with patch.object(chat_api, "_load_aggregated_articles", return_value=_fake_rows()):
            text, rows = chat_api._format_aggregated_articles_context("你是什么模型？")
        self.assertEqual(text, "")
        self.assertEqual(rows, [])

    def test_no_precise_hits_no_fake_fallback_range(self):
        # 无关键词命中、语义不触发 → 不注入任何文章（不再用最新文章罗列假召回）
        with patch.object(chat_api, "_load_aggregated_articles", return_value=_fake_rows()), \
             patch.object(chat_api, "_semantic_would_run", return_value=False):
            text, rows = chat_api._format_aggregated_articles_context("完全无关的主题词")
        self.assertEqual(rows, [])
        self.assertIn("未精确命中", text or "")

    def test_precise_keyword_hit_recalls_exact_article(self):
        with patch.object(chat_api, "_load_aggregated_articles", return_value=_fake_rows()), \
             patch.object(chat_api, "_semantic_would_run", return_value=False):
            text, rows = chat_api._format_aggregated_articles_context("网络安全漏洞年度报告")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 1)
        self.assertIn("网络安全漏洞年度报告发布", text or "")


if __name__ == "__main__":
    unittest.main()
