import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import intel_api
from industry_pack_activation import IndustryPackActivationService
from industry_pack_admin import IndustryPackAdminService, IndustryPackVersionStore
from industry_packs import IndustryPackError, IndustryPackLoader
from intel_api import intel_bp
from intel_database import IntelRepository
from sqlite_database import SQLiteDatabase


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class IndustryPackAdminServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(
            str(Path(self.temp_dir.name) / "industry-pack-admin.sqlite3")
        )
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.store = IndustryPackVersionStore(self.database)
        self.loader = IndustryPackLoader(
            str(PROJECT_ROOT / "config" / "industry_packs"),
            use_published_store=True,
            published_manifest_provider=self.store.published_manifest_for_loader,
        )
        self.service = IndustryPackAdminService(
            self.store,
            self.loader,
            url_validator=lambda value: str(value).strip(),
            settings={"APP_ENV": "development"},
        )

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def test_schema_creates_version_and_draft_tables(self):
        tables = {
            row[0]
            for row in self.database.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("industry_pack_versions", tables)
        self.assertIn("industry_pack_drafts", tables)
        self.assertIn("industry_pack_registry", tables)
        self.assertIn("industry_pack_lifecycle_events", tables)

    def test_create_publish_edit_and_logically_delete_custom_pack(self):
        created = self.service.create_pack(
            "robotics_news",
            "机器人行业",
            default_market="global",
            actor="admin-1",
        )
        self.assertEqual(created["revision"], 1)
        self.assertEqual(created["manifest"]["schema_version"], 3)
        self.assertEqual(
            created["manifest"]["includes"],
            [{"pack_id": "financial_markets", "required": True}],
        )
        self.assertTrue(
            created["manifest"]["dashboard_capabilities"]["show_financial_news"]
        )
        self.assertEqual(
            created["manifest"]["ragflow_policy"],
            {
                "upload_crawled_articles": False,
                "knowledge_base_key": "news",
            },
        )
        self.assertFalse(
            created["manifest"]["dashboard_capabilities"][
                "show_market_index_cards"
            ]
        )
        with self.assertRaisesRegex(IndustryPackError, "not found"):
            self.loader.load("robotics_news")
        managed = {
            item["id"]: item for item in self.service.list_managed_packs()
        }
        self.assertTrue(managed["robotics_news"]["draft_only"])
        self.assertEqual(managed["robotics_news"]["origin"], "custom")

        manifest = created["manifest"]
        manifest["name"] = "机器人与自动化"
        manifest["core_keywords"] = ["机器人", "自动化"]
        saved = self.service.save_draft(
            "robotics_news", manifest, expected_revision=1, actor="admin-1"
        )
        published = self.service.publish_draft(
            "robotics_news", expected_revision=saved["revision"], actor="admin-1"
        )
        self.assertEqual(published["version_number"], 1)
        self.assertEqual(self.loader.load("robotics_news")["name"], "机器人与自动化")
        self.assertIn(
            "robotics_news", [item["id"] for item in self.loader.list()]
        )

        preview = self.service.deletion_preview("robotics_news")
        self.assertTrue(preview["deletable"])
        self.assertEqual(preview["counts"]["published_versions"], 1)
        with self.assertRaisesRegex(ValueError, "确认文本"):
            self.service.delete_pack(
                "robotics_news",
                expected_plan_sha256=preview["plan_sha256"],
                confirmation_text="wrong",
                actor="admin-1",
            )
        deleted = self.service.delete_pack(
            "robotics_news",
            expected_plan_sha256=preview["plan_sha256"],
            confirmation_text="robotics_news",
            actor="admin-1",
        )
        self.assertTrue(deleted["logical_delete"])
        self.assertEqual(len(self.store.list_versions("robotics_news")), 1)
        with self.assertRaisesRegex(IndustryPackError, "not found"):
            self.loader.load("robotics_news")
        self.assertNotIn(
            "robotics_news",
            [item["id"] for item in self.service.list_managed_packs()],
        )
        events = self.service.list_lifecycle_events()
        self.assertEqual([item["event_type"] for item in events], ["deleted", "created"])
        self.assertTrue(events[0]["details"]["retention_policy"]["versions_retained"])
        with self.assertRaisesRegex(ValueError, "保留用于审计"):
            self.service.create_pack("robotics_news", "重复机器人行业")

    def test_delete_preview_blocks_system_active_dependent_and_running_pack(self):
        system_preview = self.service.deletion_preview("education_news")
        self.assertFalse(system_preview["deletable"])
        self.assertIn("系统内置行业包不能删除", system_preview["blockers"])

        draft = self.service.create_pack("energy_news", "能源行业")
        self.service.publish_draft("energy_news", expected_revision=draft["revision"])
        self.database.connection.execute(
            """
            INSERT INTO intel_runtime_settings(setting_key, setting_value)
            VALUES('active_industry_pack_id', 'energy_news')
            ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value
            """
        )
        active_preview = self.service.deletion_preview("energy_news")
        self.assertFalse(active_preview["deletable"])
        self.assertTrue(any("当前激活" in item for item in active_preview["blockers"]))
        self.database.connection.execute(
            "UPDATE intel_runtime_settings SET setting_value='family_office' WHERE setting_key='active_industry_pack_id'"
        )

        dependent = self.service.create_pack("climate_news", "气候行业")
        dependent_manifest = dependent["manifest"]
        dependent_manifest["includes"].append(
            {"pack_id": "energy_news", "required": True}
        )
        dependent = self.service.save_draft(
            "climate_news",
            dependent_manifest,
            expected_revision=dependent["revision"],
        )
        self.service.publish_draft(
            "climate_news", expected_revision=dependent["revision"]
        )
        dependency_preview = self.service.deletion_preview("energy_news")
        self.assertFalse(dependency_preview["deletable"])
        self.assertEqual(dependency_preview["dependent_pack_ids"], ["climate_news"])

    def test_draft_optimistic_lock_publish_immutability_and_runtime_override(self):
        draft = self.service.get_or_create_draft("education_news", actor="admin-1")
        self.assertEqual(draft["revision"], 1)
        manifest = draft["manifest"]
        manifest["pack_version"] = "2.0.1"
        manifest["core_keywords"].append("教育金融")

        saved = self.service.save_draft(
            "education_news",
            manifest,
            expected_revision=1,
            actor="admin-1",
        )
        self.assertEqual(saved["revision"], 2)
        with self.assertRaisesRegex(ValueError, "其他操作更新"):
            self.service.save_draft(
                "education_news",
                manifest,
                expected_revision=1,
                actor="stale-admin",
            )

        published = self.service.publish_draft(
            "education_news",
            expected_revision=2,
            actor="admin-1",
        )
        self.assertEqual(published["version_number"], 1)
        self.assertEqual(published["parent_version_id"], None)
        self.assertIn(
            "教育金融", self.loader.load("education_news")["core_keywords"]
        )

        next_draft = self.service.get_or_create_draft(
            "education_news", actor="admin-2"
        )
        self.assertEqual(next_draft["base_version_id"], published["id"])
        with self.assertRaisesRegex(ValueError, "已经发布"):
            self.service.publish_draft(
                "education_news",
                expected_revision=next_draft["revision"],
                actor="admin-2",
            )
        versions = self.store.list_versions("education_news")
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["manifest"]["pack_version"], "2.0.1")

    def test_validation_rejects_duplicate_sources_and_bad_source_contract(self):
        manifest = self.loader.load(
            "ai_news", enabled_only=False, use_published=False
        )
        manifest["default_sources"].append(
            {
                **manifest["default_sources"][0],
                "name": "duplicate",
            }
        )
        with self.assertRaisesRegex(IndustryPackError, "duplicates"):
            self.service.validate_manifest("ai_news", manifest)

        manifest = self.loader.load(
            "ai_news", enabled_only=False, use_published=False
        )
        manifest["default_sources"][0]["source_type"] = "database"
        with self.assertRaisesRegex(IndustryPackError, "source_type"):
            self.service.validate_manifest("ai_news", manifest)

        manifest = self.loader.load(
            "ai_news", enabled_only=False, use_published=False
        )
        manifest["default_sources"][0]["polling_interval_minutes"] = 1
        with self.assertRaisesRegex(IndustryPackError, "within 5..10080"):
            self.service.validate_manifest("ai_news", manifest)

    def test_diff_reports_keywords_sources_and_dashboard_flags(self):
        draft = self.service.get_or_create_draft("healthcare_news")
        changed = json.loads(json.dumps(draft["manifest"]))
        changed["core_keywords"].append("医疗融资")
        removed = changed["default_sources"].pop()
        changed["dashboard_capabilities"]["show_market_index_cards"] = True
        diff = self.service.diff("healthcare_news", changed)
        self.assertEqual(diff["keywords"]["core_keywords"]["added"], ["医疗融资"])
        self.assertEqual(diff["sources"]["removed"][0]["url"], removed["url"])
        self.assertFalse(
            diff["dashboard_capabilities"]["before"]["show_market_index_cards"]
        )
        self.assertTrue(
            diff["dashboard_capabilities"]["after"]["show_market_index_cards"]
        )


class IndustryPackAdminAPITest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(
            str(Path(self.temp_dir.name) / "industry-pack-api.sqlite3")
        )
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.store = IndustryPackVersionStore(self.database)
        self.loader = IndustryPackLoader(
            str(PROJECT_ROOT / "config" / "industry_packs"),
            use_published_store=True,
            published_manifest_provider=self.store.published_manifest_for_loader,
        )
        self.service = IndustryPackAdminService(
            self.store,
            self.loader,
            url_validator=lambda value: str(value).strip(),
            settings={"APP_ENV": "development"},
        )
        app = Flask(__name__)
        app.config.update(TESTING=True)
        app.register_blueprint(intel_bp)
        self.client = app.test_client()
        self.auth = patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": "admin-1", "role": "admin"},
        )
        self.auth.start()
        self.service_patch = patch.object(
            intel_api, "industry_pack_admin_service", self.service
        )
        self.store_patch = patch.object(
            intel_api, "industry_pack_version_store", self.store
        )
        self.loader_patch = patch.object(intel_api, "industry_pack_loader", self.loader)
        self.repository_patch = patch.object(
            intel_api,
            "intel_repository",
            IntelRepository(self.database),
        )
        self.activation_patch = patch.object(
            intel_api,
            "industry_pack_activation_service",
            IndustryPackActivationService(
                self.database,
                version_store=self.store,
                repository=IntelRepository(self.database),
            ),
        )
        self.service_patch.start()
        self.store_patch.start()
        self.loader_patch.start()
        self.repository_patch.start()
        self.activation_patch.start()
        self.headers = {"Authorization": "Bearer fixture"}

    def tearDown(self):
        self.activation_patch.stop()
        self.repository_patch.stop()
        self.loader_patch.stop()
        self.store_patch.stop()
        self.service_patch.stop()
        self.auth.stop()
        self.database.disconnect()
        self.temp_dir.cleanup()

    def test_admin_draft_validate_save_publish_and_list_versions(self):
        draft_response = self.client.get(
            "/api/intel/industry-packs/ai_news/draft", headers=self.headers
        )
        self.assertEqual(draft_response.status_code, 200)
        draft = draft_response.get_json()["draft"]
        manifest = draft["manifest"]
        manifest["pack_version"] = "2.0.1"
        manifest["core_keywords"].append("AI 金融")
        manifest["fixed_topics"].append(
            {
                "key": "ai_finance",
                "name": "AI金融",
                "keywords": ["AI 金融", "金融大模型"],
            }
        )

        duplicate_name = copy.deepcopy(manifest)
        duplicate_name["fixed_topics"].append(
            {
                "key": "duplicate_topic_name",
                "name": "AI金融",
                "keywords": ["重复主题"],
            }
        )
        invalid_topics = self.client.post(
            "/api/intel/industry-packs/ai_news/draft/validate",
            headers=self.headers,
            json={"manifest": duplicate_name},
        )
        self.assertEqual(invalid_topics.status_code, 400)

        validation = self.client.post(
            "/api/intel/industry-packs/ai_news/draft/validate",
            headers=self.headers,
            json={"manifest": manifest},
        )
        self.assertEqual(validation.status_code, 200)
        self.assertTrue(validation.get_json()["valid"])

        saved = self.client.put(
            "/api/intel/industry-packs/ai_news/draft",
            headers=self.headers,
            json={"manifest": manifest, "expected_revision": draft["revision"]},
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(
            saved.get_json()["draft"]["manifest"]["fixed_topics"][-1]["key"],
            "ai_finance",
        )
        revision = saved.get_json()["draft"]["revision"]

        stale = self.client.put(
            "/api/intel/industry-packs/ai_news/draft",
            headers=self.headers,
            json={"manifest": manifest, "expected_revision": draft["revision"]},
        )
        self.assertEqual(stale.status_code, 400)

        published = self.client.post(
            "/api/intel/industry-packs/ai_news/draft/publish",
            headers=self.headers,
            json={"expected_revision": revision},
        )
        self.assertEqual(published.status_code, 201)
        versions = self.client.get(
            "/api/intel/industry-packs/ai_news/versions", headers=self.headers
        )
        self.assertEqual(versions.status_code, 200)
        self.assertEqual(len(versions.get_json()["versions"]), 1)

    def test_custom_pack_create_edit_publish_delete_and_lifecycle_api(self):
        created = self.client.post(
            "/api/intel/industry-packs/custom",
            headers=self.headers,
            json={
                "id": "robotics_news",
                "name": "机器人行业",
                "default_market": "GLOBAL",
                "timezone": "Asia/Hong_Kong",
            },
        )
        self.assertEqual(created.status_code, 201)
        created_payload = created.get_json()
        revision = created_payload["draft"]["revision"]
        managed = self.client.get(
            "/api/intel/industry-packs/admin", headers=self.headers
        )
        self.assertEqual(managed.status_code, 200)
        custom = next(
            item
            for item in managed.get_json()["industry_packs"]
            if item["id"] == "robotics_news"
        )
        self.assertTrue(custom["draft_only"])
        public_before = self.client.get(
            "/api/intel/industry-packs", headers=self.headers
        ).get_json()["industry_packs"]
        self.assertNotIn("robotics_news", [item["id"] for item in public_before])

        published = self.client.post(
            "/api/intel/industry-packs/robotics_news/draft/publish",
            headers=self.headers,
            json={"expected_revision": revision},
        )
        self.assertEqual(published.status_code, 201)
        public_after = self.client.get(
            "/api/intel/industry-packs", headers=self.headers
        ).get_json()["industry_packs"]
        self.assertIn("robotics_news", [item["id"] for item in public_after])

        dry_run = self.client.delete(
            "/api/intel/industry-packs/robotics_news", headers=self.headers
        )
        self.assertEqual(dry_run.status_code, 200)
        preview = dry_run.get_json()["preview"]
        self.assertTrue(preview["deletable"])
        deleted = self.client.delete(
            "/api/intel/industry-packs/robotics_news",
            headers=self.headers,
            json={
                "confirm": True,
                "plan_sha256": preview["plan_sha256"],
                "confirmation_text": "robotics_news",
            },
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.get_json()["result"]["history_retained"])
        lifecycle = self.client.get(
            "/api/intel/industry-packs/activations", headers=self.headers
        )
        self.assertEqual(lifecycle.status_code, 200)
        self.assertEqual(
            lifecycle.get_json()["lifecycle_events"][0]["event_type"], "deleted"
        )

    def test_admin_boundary_and_management_page_contract(self):
        with patch("decorators.user_db.verify_session", return_value=None):
            response = self.client.get(
                "/api/intel/industry-packs/ai_news/draft",
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 401)
        template = (
            PROJECT_ROOT / "templates" / "industry_pack_management.html"
        ).read_text(encoding="utf-8")
        for marker in (
            'id="coreKeywords"',
            'id="sources"',
            'id="topics"',
            'id="showFinancialNews"',
            'data-keyword-guidance="core"',
            'data-keyword-guidance="expanded"',
            'data-keyword-guidance="trend"',
            'data-keyword-guidance="event"',
            'data-keyword-guidance="search"',
            "能否单独证明属于该行业：可以，但准入权重较低",
            "能否单独证明属于该行业：不参与最终分类",
            "默认权重：3",
            "默认权重：1",
            "默认权重：2",
            "默认权重：—",
            "验证全部配置",
            "内部主题标签",
            "function topicRow(topic={})",
            "next.fixed_topics=",
            "发布新版本",
            "切换聚合视图会保留历史文章",
        ):
            self.assertIn(marker, template)
        self.assertNotIn("清空当前聚合视图、启用", template)


if __name__ == "__main__":
    unittest.main()
