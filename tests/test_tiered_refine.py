import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

config.DATABASE_TYPE = "sqlite"

from content_handlers import (  # noqa: E402
    _drop_browser_and_author_noise,
    looks_like_title_list,
)
from intel_content_quality_gate import assess_article_quality  # noqa: E402


class BrowserAndAuthorNoiseTests(unittest.TestCase):
    """浏览器兼容提示条与作者卡/互动区删除"""

    def test_browser_notice_dropped(self):
        lines = [
            "正文第一段内容。",
            "您正在使用IE低版浏览器，为了您的雷峰网账号安全和更好的产品体验，强烈建议使用更快更安全的浏览器",
            "正文第二段内容。",
        ]
        out = _drop_browser_and_author_noise(lines)
        self.assertNotIn("IE低版浏览器", "\n".join(out))
        self.assertEqual(out, ["正文第一段内容。", "正文第二段内容。"])

    def test_author_card_rows_dropped(self):
        lines = [
            "正文第一段。",
            "TA的文章(289)",
            "评论TA的(0)",
            "TA的评论(0)",
            "点赞 0 / 文章 443",
            "於 雷峰网认证作者",
            "正文第二段。",
        ]
        out = _drop_browser_and_author_noise(lines)
        joined = "\n".join(out)
        for noise in ("TA的文章", "评论TA的", "TA的评论", "点赞 0", "认证作者"):
            self.assertNotIn(noise, joined)
        self.assertIn("正文第一段", joined)
        self.assertIn("正文第二段", joined)

    def test_long_body_sentence_kept(self):
        text = "本文介绍了华为发布的全新计算架构 Peerium，旨在满足 AI 算力需求，并为作者的文章列表提供索引。"
        out = _drop_browser_and_author_noise(text.split("\n"))
        self.assertEqual("\n".join(out).strip(), text)


class TitleListDetectionTests(unittest.TestCase):
    """标题列表/作者主页聚合页识别"""

    def test_author_profile_like_text_detected(self):
        text = (
            "您正在使用IE低版浏览器，为了您的雷峰网账号安全和更好的产品体验，强烈建议使用更快更安全的浏览器\n"
            "赛力斯集团董事、副总裁康波，首次回应了用户及市场关心的话题。\n"
            "2026/09/18 13:51\n"
            "近日，腾势汽车宣布，腾势 Z9S 展车现已抵达全国 166 城 334 家门店。\n"
            "2026/09/18 12:26\n"
            "9月17日，华为发布AI时代的全新计算架构 – Peerium计算架构。\n"
            "2026/09/17 22:25\n"
            "一家做无人车的公司，为什么需要一万多张GPU？\n"
            "2026/09/17 18:14\n"
            "华为常务董事宣布，华为坤灵全面升级一站式场景化方案\n"
            "2026/09/16 17:27\n"
        )
        self.assertTrue(looks_like_title_list(text))

    def test_normal_article_not_detected(self):
        text = (
            "9月17日，华为发布了面向AI时代的全新计算架构——Peerium计算架构。\n\n"
            "该架构旨在实现百万级处理器集成一台计算机，以满足日益增长的AI算力需求。\n\n"
            "此举标志着华为在AI计算领域的重要突破。"
        )
        self.assertFalse(looks_like_title_list(text))

    def test_quality_gate_flags_author_profile(self):
        content = (
            "TA的文章(289)\n评论TA的(0)\n"
            "赛力斯集团董事、副总裁康波，首次回应了用户及市场关心的话题。\n"
            "2026/09/18 13:51\n"
            "近日，腾势汽车宣布，腾势 Z9S 展车现已抵达全国 166 城。\n"
            "2026/09/18 12:26\n"
            "9月17日，华为发布AI时代的全新计算架构 – Peerium计算架构。\n"
            "2026/09/17 22:25\n"
            "一家做无人车的公司，为什么需要一万多张GPU？\n"
            "2026/09/17 18:14\n"
            "华为常务董事宣布，华为坤灵全面升级一站式场景化方案\n"
            "2026/09/16 17:27\n"
        )
        result = assess_article_quality({
            "title": "华为发布Peerium计算架构，推动AI时代算力发展",
            "content": content,
            "url": "https://www.leiphone.com/author/shi512",
            "publish_date": "2026-09-17",
        }, {})
        self.assertIn("content_is_author_profile_or_title_list", result["issues"])


class TieredRefinePromptTests(unittest.TestCase):
    """VPN 分层提炼：提示词注入行业包 + 元数据返回"""

    def test_refine_v3_returns_tier_metadata(self):
        import remote_pipeline.app as app
        fake = {
            "content_type": "B",
            "relevance": "low",
            "refined_title": "华为发布 Peerium 计算架构",
            "refined_content": "华为发布 Peerium 计算架构，实现百万级处理器集成。",
            "core_facts": ["Peerium 计算架构", "百万级处理器"],
        }
        with patch.object(app, "_ollama_json", return_value=fake) as ollama_mock, \
             patch.object(app, "_language", return_value="zh"):
            result = app._refine_article(
                {"title": "华为发布Peerium计算架构", "content": "正文内容"},
                industry_pack_name="具身智能",
                industry_topics=[{"key": "embodied_ai", "name": "具身智能"}],
            )
        self.assertEqual(result["content_type"], "B")
        self.assertEqual(result["relevance"], "low")
        self.assertEqual(result["core_facts"], ["Peerium 计算架构", "百万级处理器"])
        self.assertEqual(result["prompt_version"], "collectinfo-refine-v3")
        # 提示词包含行业包与主题锚定
        sys_text = ollama_mock.call_args[0][0][0]["content"]
        user_text = ollama_mock.call_args[0][0][1]["content"]
        self.assertIn("行业包", user_text)
        self.assertIn("具身智能", user_text)
        self.assertIn("内容判定", sys_text)
        self.assertIn("content_type", sys_text)

    def test_refine_v3_without_pack_still_works(self):
        import remote_pipeline.app as app
        fake = {
            "content_type": "A",
            "relevance": "high",
            "refined_title": "标题",
            "refined_content": "精炼正文内容。",
            "core_facts": [],
        }
        with patch.object(app, "_ollama_json", return_value=fake), \
             patch.object(app, "_language", return_value="zh"):
            result = app._refine_article({"title": "t", "content": "c"})
        self.assertEqual(result["prompt_version"], "collectinfo-refine-v3")
        self.assertEqual(result["content_type"], "A")


class EnrichClientPackContextTests(unittest.TestCase):
    """client.enrich 透传行业包上下文"""

    def test_payload_carries_pack_context(self):
        from remote_pipeline_client import remote_pipeline_client
        with patch.object(remote_pipeline_client.session, "post") as post_mock, \
             patch.object(remote_pipeline_client, "base_url", "http://vpn.example/"), \
             patch.object(remote_pipeline_client, "token", "t" * 40):
            post_mock.return_value.status_code = 200
            post_mock.return_value.json.return_value = {"success": True, "refined_content": "x"}
            post_mock.return_value.raise_for_status.return_value = None
            remote_pipeline_client.enrich(
                url="https://x.example/a", title="t", content="c",
                industry_pack_id="embodied_ai", industry_pack_name="具身智能",
                industry_topics=[{"key": "embodied_ai", "name": "具身智能"}],
            )
            payload = post_mock.call_args.kwargs["json"]
            self.assertEqual(payload["industry_pack_id"], "embodied_ai")
            self.assertEqual(payload["industry_pack_name"], "具身智能")
            self.assertEqual(payload["industry_topics"], [{"key": "embodied_ai", "name": "具身智能"}])


class RefineMetaPersistenceTests(unittest.TestCase):
    """分层元数据写入 article_derivatives.refine_meta_json"""

    def test_refine_meta_written(self):
        from remote_result_ingestor import _write_article_derivative, ensure_remote_pipeline_schema
        from sqlite_database import SQLiteDatabase
        tmp = tempfile.TemporaryDirectory()
        db = None
        try:
            db = SQLiteDatabase(os.path.join(tmp.name, "meta.sqlite3"))
            self.assertTrue(db.connect())
            self.assertTrue(db.create_tables())
            ensure_remote_pipeline_schema(db)
            article_id = db.insert_article({
                "url": "https://x.example/meta",
                "title": "分层元数据测试",
                # 入库闸门（intel_boilerplate）会判废"去框架后有效正文 < 40 字"的近空内容，
                # 这里补足真实长度（占位文本不含框架特征词）。
                "content": (
                    "正文内容，长度足够。"
                    "本条正文为单元测试夹具生成的占位内容，用于满足入库闸门对有效正文字数的要求。"
                ),
                "matched_keywords": ["元数据"],
                "publish_date": "2026-09-21",
            })
            _write_article_derivative(db, article_id, {
                "url": "https://x.example/meta",
                "raw_content": "",
                "refined_content": "精炼正文",
                "refined_title": "标题",
                "translated_content": "",
                "audio_manifest": {},
                "source_language": "zh",
                "target_language": "en",
                "refine_meta": {"content_type": "B", "relevance": "low", "core_facts": ["f1"]},
            }, "job-meta")
            import json
            cursor = db.connection.cursor()
            cursor.execute(
                "SELECT refine_meta_json FROM article_derivatives WHERE article_id=?", (article_id,))
            row = cursor.fetchone()
            cursor.close()
            meta = json.loads(row["refine_meta_json"] or "{}")
            self.assertEqual(meta.get("content_type"), "B")
            self.assertEqual(meta.get("relevance"), "low")
        finally:
            if db is not None:
                db.disconnect()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
