import os
import tempfile
import unittest
from unittest.mock import patch

import intel_worker as iw
from intel_database import IntelRepository
from intel_worker import IntelWorker
from sqlite_database import SQLiteDatabase


class EnrichMarkdownRefreshTests(unittest.TestCase):
    """阶段1×回填联动：vpn_ocr 文章展示 Markdown 优先用摘要；enrich 落库同步刷新 content_markdown。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.tmp.name, "enrich.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        self.db.analyze_article_spacetime_profile = lambda _article_id: None

    def tearDown(self):
        self.db.disconnect()
        self.tmp.cleanup()

    def _insert_vpn_ocr(self):
        ocr_raw = "首页 登录 注册 导航\n\n" + "导航噪声行一\n" * 8
        summary = "该厂商发布新一代工业控制系统，覆盖网络安全防护能力，已落地多个行业。"
        aid = self.db.insert_article({
            "url": "https://x.example/ocr-article",
            "title": "工控安全新品发布",
            "content": summary,
            "raw_content": ocr_raw,
            "extraction_method": "vpn_ocr",
            "matched_keywords": ["工控安全"],
            "publish_date": "2026-09-18",
        })
        return aid, ocr_raw, summary

    def test_vpn_ocr_insert_prefers_summary_for_markdown(self):
        aid, ocr_raw, summary = self._insert_vpn_ocr()
        row = self.db.get_article_by_id(aid)
        self.assertIn("工业控制系统", str(row.get("content_markdown") or ""))
        self.assertNotIn("导航噪声", str(row.get("content_markdown") or ""))
        # 原文快照仍保留 OCR 全文，供原文比对
        self.assertIn("导航噪声", str(row.get("raw_content") or ""))

    def test_normal_article_still_uses_raw_for_markdown(self):
        aid = self.db.insert_article({
            "url": "https://x.example/normal",
            "title": "行业观察",
            "content": "这是清洗后的摘要正文内容。",
            "raw_content": "<h1>行业观察</h1><p>这是清洗后的摘要正文内容，原始页面包含更多细节。</p>",
            "extraction_method": "newspaper3k",
            "matched_keywords": ["行业观察"],
            "publish_date": "2026-09-18",
        })
        row = self.db.get_article_by_id(aid)
        self.assertIn("原始页面包含更多细节", str(row.get("content_markdown") or ""))

    def test_enrich_refreshes_content_markdown(self):
        aid, _ocr_raw, _summary = self._insert_vpn_ocr()
        # enrich 前的 content_markdown 来自摘要
        before = self.db.get_article_by_id(aid)
        self.assertIn("工业控制系统", str(before.get("content_markdown") or ""))

        refined = "精炼结论：该产品已通过等保三级测评，重点覆盖电力与制造行业。"
        repo = IntelRepository(self.db)
        worker = IntelWorker(repository=repo, worker_id="enrich-md-test")
        with patch.object(iw.config, "REMOTE_PIPELINE_URL", "http://vpn.example/"), \
             patch.object(iw.config, "REMOTE_PIPELINE_TOKEN", "t"), \
             patch.object(iw.config, "REMOTE_PIPELINE_ENRICH", True), \
             patch.object(iw.config, "REMOTE_PIPELINE_TTS", False), \
             patch.object(iw.config, "REMOTE_PIPELINE_TTS_VOICE", ""), \
             patch.object(iw.config, "REMOTE_PIPELINE_JOB_TIMEOUT_SECONDS", 5), \
             patch("remote_pipeline_client.remote_pipeline_client.enrich",
                   return_value={"refined_content": refined, "job_id": "j1"}), \
             patch("remote_result_ingestor._write_article_derivative", return_value=None):
            result = worker._handle_enrich({
                "article_id": aid,
                "url": "https://x.example/ocr-article",
                "title": "工控安全新品发布",
                "content": "",
            })
        self.assertTrue(result.get("success"))
        after = self.db.get_article_by_id(aid)
        self.assertEqual(str(after.get("content") or ""), refined)
        # 联动：content_markdown 同步刷新为精炼结论，而不是精炼前的旧摘要
        self.assertIn("精炼结论", str(after.get("content_markdown") or ""))
        self.assertNotIn("该厂商发布新一代", str(after.get("content_markdown") or ""))


if __name__ == "__main__":
    unittest.main()
