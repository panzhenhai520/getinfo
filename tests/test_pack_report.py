import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import pack_report
from sqlite_database import SQLiteDatabase


NOW = datetime(2026, 9, 18, 10, 0, 0)


def _day(day: int) -> str:
    return f"2026-09-{day:02d}"


class PackReportTest(unittest.TestCase):
    def setUp(self):
        # 强制 SQLite 后端：本机 .env 配了 DATABASE_TYPE=postgres，测试不得写入本地 PG
        import config as _config
        self._orig_db_type = getattr(_config, 'DATABASE_TYPE', None)
        _config.DATABASE_TYPE = 'sqlite'
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "pack-report.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.connection = self.database.connection
        self._orig_db = pack_report.sqlite_db
        pack_report.sqlite_db = self.database
        self._seed_articles()

    def tearDown(self):
        pack_report.sqlite_db = self._orig_db
        import config as _config
        if self._orig_db_type is not None:
            _config.DATABASE_TYPE = self._orig_db_type
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _seed_articles(self):
        rows = [
            (1, _day(16), "趋势", "测试文章一：行业热点内容", "example-a.com", "trend"),
            (2, _day(17), "事件", "测试文章二：行业动态内容", "example-b.com", "event"),
            (3, _day(17), "其他", "测试文章三：其他资讯内容", "example-c.com", "other"),
        ]
        for aid, day, kw, content, domain, category in rows:
            self.connection.execute(
                "INSERT INTO articles(id, url, title, content, domain, publish_date, first_crawled, status)"
                " VALUES(?,?,?,?,?,?,?, 'active')",
                (aid, f"https://{domain}/{aid}", f"文章{aid}", content, domain, day, day),
            )
            self.connection.execute(
                "INSERT INTO article_intel_classifications(article_id, industry_pack_id,"
                " industry_pack_version, classifier_version, article_content_hash,"
                " rule_category, matched_keywords_json, topic_tags_json, final_category)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (aid, "auto_test", "v1", "test", f"hash{aid}", "other",
                 json.dumps([kw], ensure_ascii=False), "[]", category),
            )
        self.connection.commit()

    # ---- 提示词设置 ----
    def test_settings_default_then_save_then_clear(self):
        settings = pack_report.get_pack_report_settings("auto_test")
        self.assertTrue(settings["is_default"])
        self.assertEqual(settings["prompt"], pack_report.DEFAULT_PACK_REPORT_PROMPT)
        saved = pack_report.save_pack_report_settings("auto_test", "自定义提示词：{行业名称} {时间窗}")
        self.assertFalse(saved["is_default"])
        settings = pack_report.get_pack_report_settings("auto_test")
        self.assertFalse(settings["is_default"])
        self.assertIn("自定义提示词", settings["prompt"])
        cleared = pack_report.save_pack_report_settings("auto_test", "")
        self.assertTrue(cleared["is_default"])
        settings = pack_report.get_pack_report_settings("auto_test")
        self.assertEqual(settings["prompt"], pack_report.DEFAULT_PACK_REPORT_PROMPT)

    # ---- 生成 + 落库（幂等 upsert）----
    def test_generate_report_saves_markdown_and_upserts(self):
        from industry_packs import IndustryPackLoader

        stub_loader = IndustryPackLoader.__new__(IndustryPackLoader)
        stub_loader.load = lambda pack_id, enabled_only=True, use_published=True: {
            "name": "测试行业",
            "candidate_gate": {"anchor_keywords": ["测试"]},
            "trend_topics": [],
            "fixed_topics": [],
        }
        with patch("industry_packs.industry_pack_loader", stub_loader), \
                patch("pack_report._semantic_rank", return_value=[]), \
                patch("pack_report._llm_markdown", return_value="# 测试行业周报（09-11 ~ 09-18）\n\n## 一、本周概览\n共 3 篇。\n"):
            report = pack_report.generate_pack_report("auto_test", _day(11), _day(18), source="test")
        self.assertEqual(report["title"], "测试行业周报（09-11 ~ 09-18）")
        self.assertEqual(report["article_count"], 3)
        self.assertEqual(report["pack_id"], "auto_test")
        reports = pack_report.list_pack_reports("auto_test")
        self.assertEqual(len(reports), 1)
        markdown = pack_report.get_pack_report_markdown(reports[0]["id"], "auto_test")
        self.assertIn("本周概览", markdown)
        # 同 pack+时间窗+来源 再次生成 → 更新而非新增
        with patch("industry_packs.industry_pack_loader", stub_loader), \
                patch("pack_report._semantic_rank", return_value=[]), \
                patch("pack_report._llm_markdown", return_value="# 测试行业周报（新版）\n\n## 一、本周概览\n更新版。\n"):
            again = pack_report.generate_pack_report("auto_test", _day(11), _day(18), source="test")
        reports = pack_report.list_pack_reports("auto_test")
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["id"], again["id"])
        self.assertEqual(reports[0]["title"], "测试行业周报（新版）")
        last = pack_report.last_report_info("auto_test")
        self.assertEqual(last["title"], "测试行业周报（新版）")

    def test_generate_rejects_empty_window(self):
        with patch("pack_report._load_window_articles", return_value=[]):
            with self.assertRaises(ValueError) as ctx:
                pack_report.generate_pack_report("auto_test", _day(11), _day(18), source="test")
        self.assertIn("没有该行业的文章", str(ctx.exception))

    def test_prompt_placeholders_filled(self):
        data = {
            "pack_name": "测试行业", "anchors_text": "测试", "topics_text": "主题A",
            "window_text": "2026-09-11 ~ 2026-09-18", "total": 3,
            "daily_lines": ["2026-09-17 → 2 篇"], "topic_lines": ["趋势观察 | 17:1 | 合计 1 篇 | 爆发日 无"],
            "article_lines": ["1. 文章1 | 2026-09-16 | example-a.com | 趋势观察 | 摘要"],
        }
        filled = pack_report._fill_prompt(pack_report.DEFAULT_PACK_REPORT_PROMPT, data)
        self.assertIn("测试行业", filled)
        self.assertIn("2026-09-11 ~ 2026-09-18", filled)
        self.assertIn("共 3 篇", filled)
        self.assertIn("趋势观察 | 17:1", filled)
        self.assertNotIn("{行业名称}", filled)
        self.assertNotIn("{文章总数}", filled)

    def test_latex_normalized_for_readability(self):
        raw = ("# 标题\n\n## 趋势\n爆发$\\rightarrow$回落$\\rightarrow$微生；平稳$\\rightarrow$短期爆发；"
               "增长 $x$ 倍，占比$\\geq$50$\\%$。")
        cleaned = pack_report._normalize_report_markdown(raw)
        self.assertNotIn("$", cleaned)
        self.assertIn("爆发→回落→微生", cleaned)
        self.assertIn("平稳→短期爆发", cleaned)
        self.assertIn("≥", cleaned)
        self.assertIn("x 倍", cleaned)

    def test_generate_saves_latex_free_markdown(self):
        from industry_packs import IndustryPackLoader
        stub_loader = IndustryPackLoader.__new__(IndustryPackLoader)
        stub_loader.load = lambda pack_id, enabled_only=True, use_published=True: {
            "name": "测试行业", "candidate_gate": {"anchor_keywords": ["测试"]},
            "trend_topics": [], "fixed_topics": [],
        }
        with patch("industry_packs.industry_pack_loader", stub_loader), \
                patch("pack_report._semantic_rank", return_value=[]), \
                patch("pack_report._llm_markdown",
                      return_value="# 测试行业周报\n\n趋势：爆发$\\rightarrow$回落$\\rightarrow$微生\n"):
            pack_report.generate_pack_report("auto_test", _day(11), _day(18), source="test")
        markdown = pack_report.get_pack_report_markdown(
            pack_report.list_pack_reports("auto_test")[0]["id"], "auto_test"
        )
        self.assertNotIn("$", markdown)
        self.assertIn("爆发→回落→微生", markdown)


class PackReportApiTest(unittest.TestCase):
    def setUp(self):
        # 强制 SQLite 后端，测试不碰本地 PG
        import config as _config
        self._orig_db_type = getattr(_config, 'DATABASE_TYPE', None)
        _config.DATABASE_TYPE = 'sqlite'
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "pack-report-api.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self._orig_db = pack_report.sqlite_db
        pack_report.sqlite_db = self.database
        self.app = Flask(__name__)
        self.app.register_blueprint(pack_report.pack_report_bp)
        self.client = self.app.test_client()
        # 蓝图路由在定义时已绑定 decorators.login_required，这里用假会话放行
        import decorators
        stub_db = type('StubUserDb', (), {
            'verify_session': staticmethod(lambda token: {
                'user_id': 1, 'username': 'tester', 'role': 'admin',
            }),
        })()
        patcher = patch.object(decorators, 'user_db', stub_db)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 带 Authorization 头 → 装饰器走 verify_session 假会话分支
        self.headers = {'Authorization': 'Bearer test-token'}

    def tearDown(self):
        pack_report.sqlite_db = self._orig_db
        import config as _config
        if self._orig_db_type is not None:
            _config.DATABASE_TYPE = self._orig_db_type
        self.database.disconnect()
        self.temp_dir.cleanup()

    def test_settings_get_put_and_list(self):
        resp = self.client.get("/api/pack-reports/settings?industry_pack_id=auto_test", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertTrue(data["is_default"])
        self.assertEqual(data["prompt"], pack_report.DEFAULT_PACK_REPORT_PROMPT)
        self.assertIsNone(data["last_report"])
        resp = self.client.put(
            "/api/pack-reports/settings",
            json={"industry_pack_id": "auto_test", "prompt": "自定义 {时间窗}"},
            headers=self.headers,
        )
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertFalse(data["is_default"])
        resp = self.client.get("/api/pack-reports?industry_pack_id=auto_test", headers=self.headers)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["reports"], [])

    def test_generate_enqueues_job(self):
        class StubRepository:
            def enqueue_job(self, job_type, dedupe_key, payload, *, priority=0,
                            max_attempts=None, request_id="", created_by=""):
                return 123, True
        with patch("intel_database.IntelRepository", return_value=StubRepository()):
            resp = self.client.post("/api/pack-reports/generate", json={"industry_pack_id": "auto_test"},
                                    headers=self.headers)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["job_id"], 123)

    def test_markdown_404_for_missing_report(self):
        resp = self.client.get("/api/pack-reports/9999/markdown?industry_pack_id=auto_test",
                               headers=self.headers)
        self.assertEqual(resp.status_code, 404)


class PackTenantDuplicateEmailTest(unittest.TestCase):
    """邮箱重复注册给中文提示，不暴露数据库英文错误。"""

    def setUp(self):
        # 强制 SQLite 后端，测试不碰本地 PG
        import config as _config
        self._orig_db_type = getattr(_config, 'DATABASE_TYPE', None)
        _config.DATABASE_TYPE = 'sqlite'
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "pack-tenant.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        import pack_tenant
        self._orig_db = pack_tenant.sqlite_db
        pack_tenant.sqlite_db = self.database
        pack_tenant._ensure()

    def tearDown(self):
        import pack_tenant
        pack_tenant.sqlite_db = self._orig_db
        import config as _config
        if self._orig_db_type is not None:
            _config.DATABASE_TYPE = self._orig_db_type
        self.database.disconnect()
        self.temp_dir.cleanup()

    def test_duplicate_email_raises_chinese_error(self):
        from pack_tenant import create_pack_user
        create_pack_user(industry_pack_id="auto_test", username="u1", password="Pass1234",
                         email="dup@example.com")
        with self.assertRaises(ValueError) as ctx:
            create_pack_user(industry_pack_id="auto_test", username="u2", password="Pass1234",
                             email="dup@example.com")
        self.assertEqual(str(ctx.exception), "邮箱已注册，请勿重复注册")

    def test_duplicate_username_raises_chinese_error(self):
        from pack_tenant import create_pack_user
        create_pack_user(industry_pack_id="auto_test", username="same", password="Pass1234",
                         email="a@example.com")
        with self.assertRaises(ValueError) as ctx:
            create_pack_user(industry_pack_id="auto_test", username="same", password="Pass1234",
                             email="b@example.com")
        self.assertEqual(str(ctx.exception), "用户名已存在，请更换用户名")

    def test_begin_login_binding_duplicate_email_chinese(self):
        from pack_tenant import begin_login, create_pack_user
        create_pack_user(industry_pack_id="auto_test", username="bound", password="Pass1234",
                         email="taken@example.com")
        uid = create_pack_user(industry_pack_id="auto_test", username="newbie", password="Pass1234",
                               email="")
        self.assertGreater(uid, 0)
        with self.assertRaises(ValueError) as ctx:
            begin_login("newbie", "Pass1234", email="taken@example.com")
        self.assertEqual(str(ctx.exception), "邮箱已注册，请勿重复注册")


if __name__ == "__main__":
    unittest.main()
