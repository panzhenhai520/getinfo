import json
import os
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from industry_packs import (
    IndustryPackError,
    IndustryPackLoader,
    validate_industry_pack,
)
from intel_database import IntelRepository
from sqlite_database import SQLiteDatabase


def _pack(pack_id, *, enabled=True, includes=None, source_url=None, version="1.0.0"):
    sources = []
    if source_url:
        sources.append(
            {
                "name": f"{pack_id} source",
                "url": source_url,
                "source_type": "rss",
                "authority_level": 3,
            }
        )
    return {
        "id": pack_id,
        "name": f"Pack {pack_id}",
        "schema_version": 2,
        "pack_version": version,
        "enabled": enabled,
        "default_market": "GLOBAL",
        "timezone": "Asia/Hong_Kong",
        "pack_kind": "primary",
        "includes": includes or [],
        "capabilities": [
            {
                "key": "shared_capability" if pack_id != "leaf" else "leaf_capability",
                "name": f"{pack_id} capability",
                "enabled": pack_id == "root",
            }
        ],
        "dashboard_categories": [
            {
                "key": "shared_dashboard" if pack_id != "leaf" else "leaf_dashboard",
                "name": f"{pack_id} dashboard",
                "enabled": pack_id == "root",
            }
        ],
        "core_keywords": [f"{pack_id} core"],
        "expanded_keywords": [],
        "trend_keywords": ["trend"],
        "event_keywords": ["event"],
        "negative_keywords": [],
        "classification": {
            "core_weight": 3,
            "expanded_weight": 1,
            "trend_weight": 2,
            "event_weight": 2,
            "negative_weight": -3,
            "minimum_relevance_score": 2,
            "llm_confidence_threshold": 0.65,
            "tie_break_order": ["trend", "event", "other"],
        },
        "serpapi_queries": [],
        "default_sources": sources,
        "fixed_topics": [],
    }


class IndustryPackV2Test(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_pack(self, pack):
        path = self.config_dir / f"{pack['id']}.json"
        path.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
        return path

    def test_production_family_office_adds_finance_without_overwriting_primary(self):
        # Verify the installation-owned production packs independently from
        # custom packs published in a developer/runtime database.
        loader = IndustryPackLoader(use_published_store=False)
        primary = loader.load("family_office")
        composed = loader.compose("family_office")

        self.assertEqual(composed["effective_pack_ids"], ["family_office", "financial_markets"])
        self.assertEqual(composed["primary_pack"]["name"], primary["name"])
        self.assertEqual(composed["primary_pack"]["core_keywords"], primary["core_keywords"])
        self.assertNotIn("上证指数", composed["primary_pack"]["core_keywords"])
        capabilities = {item["key"]: item for item in composed["capabilities"]}
        self.assertTrue(capabilities["family_office_content_intelligence"]["enabled"])
        self.assertFalse(capabilities["financial_market_data"]["enabled"])
        self.assertEqual(
            capabilities["financial_market_data"]["activation_flag"],
            "FINANCIAL_INTELLIGENCE_ENABLED",
        )
        dashboard = {item["key"]: item for item in composed["dashboard_categories"]}
        self.assertIn("financial_risk_management", dashboard)
        self.assertFalse(dashboard["financial_risk_management"]["enabled"])
        self.assertEqual(composed["primary_pack_id"], "family_office")
        self.assertEqual(composed["dependency_pack_ids"], ["financial_markets"])
        self.assertEqual(
            primary["ragflow_policy"],
            {
                "upload_crawled_articles": True,
                "qa_retrieval_enabled": True,
                "knowledge_base_key": "news",
            },
        )
        self.assertEqual(
            loader.load("ai_news")["ragflow_policy"],
            {
                "upload_crawled_articles": False,
                "qa_retrieval_enabled": False,
                "knowledge_base_key": "news",
            },
        )
        self.assertEqual(
            composed["dashboard_capabilities"],
            {
                "show_financial_news": True,
                "show_market_index_cards": True,
                "show_watched_stock_cards": True,
                # 时空地图首页已下线（5fc1f61/ab945b9）：family_office 首页改成主题驾驶舱，
                # 出厂包显式把 show_spatiotemporal_map 关掉，首页固定走资讯流 Dashboard。
                "show_spatiotemporal_map": False,
            },
        )

    def test_ai_healthcare_and_education_additions_are_declarations_only(self):
        loader = IndustryPackLoader()
        expected = {
            "ai_news": "ai_model_release_radar",
            "healthcare_news": "clinical_trial_monitor",
            "education_news": "education_policy_tracker",
        }
        for pack_id, capability_key in expected.items():
            composed = loader.compose(pack_id)
            capabilities = {item["key"]: item for item in composed["capabilities"]}
            self.assertEqual(
                composed["effective_pack_ids"], [pack_id, "financial_markets"]
            )
            self.assertIn(capability_key, capabilities)
            self.assertFalse(capabilities[capability_key]["enabled"])
            self.assertEqual(
                capabilities[capability_key]["implementation_status"], "schema_example"
            )
            self.assertTrue(
                composed["dashboard_capabilities"]["show_financial_news"]
            )
            self.assertFalse(
                composed["dashboard_capabilities"]["show_market_index_cards"]
            )
            self.assertFalse(
                composed["dashboard_capabilities"]["show_watched_stock_cards"]
            )

    def test_all_production_primary_packs_require_one_shared_financial_addon(self):
        # Runtime-created packs are covered by admin/activation tests; this
        # contract intentionally enumerates installation-owned seed packs.
        loader = IndustryPackLoader(use_published_store=False)
        primary_ids = []
        for pack in loader.list():
            if pack["pack_kind"] != "primary":
                continue
            primary_ids.append(pack["id"])
            financial_includes = [
                item
                for item in pack["includes"]
                if item["pack_id"] == "financial_markets"
            ]
            self.assertEqual(len(financial_includes), 1, pack["id"])
            self.assertTrue(financial_includes[0]["required"], pack["id"])
            composed = loader.compose(pack["id"])
            self.assertEqual(
                composed["effective_pack_ids"].count("financial_markets"), 1
            )
        self.assertEqual(
            set(primary_ids),
            {
                "ai_news",
                "automotive_industry",
                "bolean_security_compute",
                "education_news",
                "family_office",
                "healthcare_news",
                "short_video_news",
            },
        )

    def test_production_pack_files_validate_against_published_json_schema(self):
        config_dir = Path(__file__).resolve().parents[1] / "config" / "industry_packs"
        schema = json.loads((config_dir / "schema.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        for path in sorted(config_dir.glob("*.json")):
            if path.name == "schema.json":
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            errors = sorted(validator.iter_errors(payload), key=lambda item: list(item.path))
            self.assertEqual(errors, [], path.name)

    def test_schema_v3_primary_requires_financial_addon_and_strict_dashboard_flags(self):
        pack = _pack("strict")
        pack["schema_version"] = 3
        pack["includes"] = [{"pack_id": "financial_markets", "required": True}]
        pack["dashboard_capabilities"] = {
            "show_financial_news": True,
            "show_market_index_cards": False,
            "show_watched_stock_cards": False,
        }
        validated = validate_industry_pack(pack)
        self.assertTrue(validated["dashboard_capabilities"]["show_financial_news"])

        missing_finance = _pack("missing_finance")
        missing_finance["schema_version"] = 3
        missing_finance["dashboard_capabilities"] = dict(
            pack["dashboard_capabilities"]
        )
        with self.assertRaisesRegex(IndustryPackError, "must require financial_markets"):
            validate_industry_pack(missing_finance)

        optional_finance = _pack(
            "optional_finance",
            includes=[{"pack_id": "financial_markets", "required": False}],
        )
        optional_finance["schema_version"] = 3
        optional_finance["dashboard_capabilities"] = dict(
            pack["dashboard_capabilities"]
        )
        with self.assertRaisesRegex(IndustryPackError, "must require financial_markets"):
            validate_industry_pack(optional_finance)

        invalid_flag = json.loads(json.dumps(pack))
        invalid_flag["dashboard_capabilities"]["show_market_index_cards"] = "yes"
        with self.assertRaisesRegex(IndustryPackError, "must be boolean"):
            validate_industry_pack(invalid_flag)

        unknown_flag = json.loads(json.dumps(pack))
        unknown_flag["dashboard_capabilities"]["show_everything"] = True
        with self.assertRaisesRegex(IndustryPackError, "unsupported fields"):
            validate_industry_pack(unknown_flag)

    def test_validation_populates_and_accepts_classification_recency_windows(self):
        pack = _pack("windowed")
        validated = validate_industry_pack(pack)
        self.assertEqual(validated["classification"]["recent_today_window_days"], 5)
        self.assertEqual(validated["classification"]["recent_trend_window_days"], 21)

        custom = _pack("custom_windowed")
        custom["classification"]["recent_today_window_days"] = 1
        custom["classification"]["recent_trend_window_days"] = 6
        validated_custom = validate_industry_pack(custom)
        self.assertEqual(validated_custom["classification"]["recent_today_window_days"], 1)
        self.assertEqual(validated_custom["classification"]["recent_trend_window_days"], 6)

    def test_serpapi_query_gates_must_reference_configured_queries(self):
        pack = _pack("query_gates")
        query = '汽车 ("智能驾驶" OR "自动驾驶")'
        pack["serpapi_queries"] = [query]
        pack["serpapi_query_gates"] = {query: ["智能驾驶", "自动驾驶"]}

        validated = validate_industry_pack(pack)
        self.assertEqual(
            validated["serpapi_query_gates"],
            {query: ["智能驾驶", "自动驾驶"]},
        )

        unknown = _pack("unknown_query_gate")
        unknown["serpapi_query_gates"] = {"missing query": ["智能驾驶"]}
        with self.assertRaisesRegex(IndustryPackError, "unknown query"):
            validate_industry_pack(unknown)

        empty = _pack("empty_query_gate")
        empty["serpapi_queries"] = [query]
        empty["serpapi_query_gates"] = {query: []}
        with self.assertRaisesRegex(IndustryPackError, "non-empty string arrays"):
            validate_industry_pack(empty)

    def test_multilevel_dependency_deduplicates_packs_capabilities_and_sources(self):
        root = _pack(
            "root",
            includes=[{"pack_id": "child", "required": True}, "leaf"],
            source_url="https://Example.test/feed",
        )
        child = _pack(
            "child",
            includes=["leaf"],
            source_url="https://example.test/feed",
        )
        leaf = _pack("leaf", source_url="https://example.test/leaf")
        for pack in (root, child, leaf):
            self.write_pack(pack)
        loader = IndustryPackLoader(str(self.config_dir))

        composed = loader.compose("root")

        self.assertEqual(composed["effective_pack_ids"], ["root", "child", "leaf"])
        self.assertEqual(composed["primary_pack"]["name"], "Pack root")
        self.assertEqual(composed["primary_pack"]["core_keywords"], ["root core"])
        self.assertEqual(
            [item["key"] for item in composed["capabilities"]],
            ["shared_capability", "leaf_capability"],
        )
        self.assertTrue(composed["capabilities"][0]["enabled"])
        self.assertEqual(len(composed["default_sources"]), 2)
        self.assertEqual(
            composed["default_sources"][0]["declared_by_pack_ids"], ["root", "child"]
        )

    def test_cycle_and_required_missing_or_disabled_dependencies_fail_closed(self):
        self.write_pack(_pack("root", includes=["child"]))
        self.write_pack(_pack("child", includes=["root"]))
        loader = IndustryPackLoader(str(self.config_dir))
        with self.assertRaisesRegex(IndustryPackError, "dependency cycle"):
            loader.effective_pack_set("root")

        missing_root = _pack("missing_root", includes=[{"pack_id": "absent", "required": True}])
        self.write_pack(missing_root)
        with self.assertRaisesRegex(IndustryPackError, "not found"):
            loader.effective_pack_set("missing_root")

        disabled_root = _pack("disabled_root", includes=["disabled_child"])
        self.write_pack(disabled_root)
        self.write_pack(_pack("disabled_child", enabled=False))
        with self.assertRaisesRegex(IndustryPackError, "disabled"):
            loader.effective_pack_set("disabled_root")

    def test_optional_missing_and_disabled_dependencies_are_skipped(self):
        root = _pack(
            "root",
            includes=[
                {"pack_id": "missing", "required": False},
                {"pack_id": "disabled", "required": False},
            ],
        )
        self.write_pack(root)
        self.write_pack(_pack("disabled", enabled=False))
        loader = IndustryPackLoader(str(self.config_dir))
        self.assertEqual(
            [item["id"] for item in loader.effective_pack_set("root")],
            ["root"],
        )
        self.write_pack(_pack("malformed_root", includes=[{"pack_id": "broken", "required": False}]))
        (self.config_dir / "broken.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(IndustryPackError, "missing fields"):
            loader.effective_pack_set("malformed_root")

    def test_v1_remains_readable_and_dependency_version_change_invalidates_cache(self):
        legacy = _pack("legacy")
        legacy["schema_version"] = 1
        for field in ("pack_kind", "includes", "capabilities", "dashboard_categories"):
            legacy.pop(field)
        self.write_pack(legacy)
        child_path = self.write_pack(_pack("child", version="1.0.0"))
        self.write_pack(_pack("root", includes=["child"]))
        loader = IndustryPackLoader(str(self.config_dir))

        loaded_legacy = loader.load("legacy")
        self.assertEqual(loaded_legacy["schema_version"], 1)
        self.assertEqual(loaded_legacy["includes"], [])
        self.assertEqual(
            loaded_legacy["dashboard_capabilities"],
            {
                "show_financial_news": False,
                "show_market_index_cards": False,
                "show_watched_stock_cards": False,
                # 缺省不再开时空信息图：新包/老包未声明时首页走资讯流 Dashboard
                "show_spatiotemporal_map": False,
            },
        )
        self.assertEqual(loader.effective_pack_set("root")[1]["pack_version"], "1.0.0")

        changed = _pack("child", version="2.0.0")
        previous_mtime = child_path.stat().st_mtime_ns
        child_path.write_text(json.dumps(changed), encoding="utf-8")
        os.utime(child_path, ns=(previous_mtime + 2_000_000_000,) * 2)
        self.assertEqual(loader.effective_pack_set("root")[1]["pack_version"], "2.0.0")

    def test_switch_and_restore_preserve_shared_articles_and_classifications(self):
        database = SQLiteDatabase(str(self.config_dir / "pack-switch.sqlite3"))
        self.assertTrue(database.connect())
        self.assertTrue(database.create_tables())
        connection = database.connection
        connection.execute(
            "INSERT INTO articles(id, url, title, status) VALUES(1, 'https://example.test/1', 'one', 'active')"
        )
        connection.execute(
            "INSERT INTO articles(id, url, title, status) VALUES(2, 'https://example.test/2', 'two', 'archived')"
        )
        for pack_id in ("family_office", "financial_markets"):
            connection.execute(
                """
                INSERT INTO article_intel_classifications(
                    article_id, industry_pack_id, industry_pack_version,
                    classifier_version, article_content_hash, rule_category,
                    rule_confidence, rule_reason, final_category,
                    final_confidence, final_reason
                ) VALUES(1, ?, '1', '1', ?, 'trend', 1, 'test', 'trend', 1, 'test')
                """,
                (pack_id, pack_id),
            )
        repository = IntelRepository(database)
        statuses_before = list(connection.execute("SELECT id, status FROM articles ORDER BY id"))
        classifications_before = list(
            connection.execute(
                "SELECT article_id, industry_pack_id FROM article_intel_classifications ORDER BY industry_pack_id"
            )
        )

        switched = repository.switch_industry_pack("financial_markets", "v2 preservation test")
        restored = repository.restore_pack_backup(switched["backup_id"])

        self.assertTrue(switched["article_status_unchanged"])
        self.assertEqual(switched["archived_articles"], 0)
        self.assertTrue(restored["article_status_unchanged"])
        self.assertEqual(statuses_before, list(connection.execute("SELECT id, status FROM articles ORDER BY id")))
        self.assertEqual(
            classifications_before,
            list(
                connection.execute(
                    "SELECT article_id, industry_pack_id FROM article_intel_classifications ORDER BY industry_pack_id"
                )
            ),
        )
        self.assertEqual(repository.active_industry_pack_id(), "family_office")
        self.assertEqual(repository.list_pack_backups()[0]["article_count"], 1)
        database.disconnect()


if __name__ == "__main__":
    unittest.main()
