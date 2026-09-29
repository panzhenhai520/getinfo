import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from industry_pack_activation import (
    IndustryPackActivationService,
    SQLiteActivationBackupService,
)
from industry_pack_admin import IndustryPackAdminService, IndustryPackVersionStore
from industry_packs import IndustryPackLoader
from intel_database import IntelRepository
from intel_sources import IntelSourceRegistry, canonicalize_source_url
from sqlite_database import SQLiteDatabase


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class IndustryPackActivationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.database = SQLiteDatabase(str(root / "activation.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.store = IndustryPackVersionStore(self.database)
        self.loader = IndustryPackLoader(
            str(PROJECT_ROOT / "config" / "industry_packs"),
            use_published_store=True,
            published_manifest_provider=self.store.published_manifest_for_loader,
        )
        self.registry = IntelSourceRegistry(self.database, pack_loader=self.loader)
        self.repository = IntelRepository(self.database)
        self.backup_service = SQLiteActivationBackupService(
            self.database, backup_dir=str(root / "backups")
        )
        self.service = IndustryPackActivationService(
            self.database,
            version_store=self.store,
            source_registry=self.registry,
            repository=self.repository,
            backup_service=self.backup_service,
        )
        self.family_baseline = self._publish("family_office")

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _publish(self, pack_id="education_news", mutate=None):
        latest = self.store.latest_published(pack_id)
        seed = (
            latest["manifest"]
            if latest
            else self.loader.load(pack_id, enabled_only=False, use_published=False)
        )
        draft = self.store.get_or_create_draft(pack_id, seed, actor="test")
        manifest = draft["manifest"]
        if mutate:
            mutate(manifest)
            draft = self.store.save_draft(
                pack_id,
                manifest,
                expected_revision=draft["revision"],
                actor="test",
            )
        published = self.store.publish_draft(
            pack_id, expected_revision=draft["revision"], actor="test"
        )
        self.loader.clear_cache(pack_id)
        return published

    def _setting(self, key):
        row = self.database.connection.execute(
            "SELECT setting_value FROM intel_runtime_settings WHERE setting_key=?",
            (key,),
        ).fetchone()
        return str(row[0]) if row else ""

    def test_unpublished_target_is_rejected_and_preview_performs_no_writes(self):
        with self.assertRaisesRegex(ValueError, "尚无已发布版本"):
            self.service.preview("education_news")

        version = self._publish()
        before = {
            table: self.database.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in (
                "intel_sources",
                "intel_source_industries",
                "industry_pack_activations",
                "industry_pack_activation_events",
                "intel_runtime_settings",
            )
        }
        preview = self.service.preview("education_news")
        after = {
            table: self.database.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in before
        }
        self.assertEqual(before, after)
        self.assertEqual(preview["target_version_id"], version["id"])
        self.assertEqual(
            preview["previous_version_id"], self.family_baseline["id"]
        )
        self.assertFalse(preview["writes_performed"])
        self.assertTrue(preview["requires_confirmation"])

    def test_deleted_custom_pack_cannot_be_activated_or_rollback_target(self):
        admin = IndustryPackAdminService(
            self.store,
            self.loader,
            url_validator=lambda value: str(value).strip(),
        )
        draft = admin.create_pack("robotics_news", "机器人行业", actor="admin")
        published = admin.publish_draft(
            "robotics_news", expected_revision=draft["revision"], actor="admin"
        )
        preview = admin.deletion_preview("robotics_news")
        self.assertTrue(preview["deletable"])
        admin.delete_pack(
            "robotics_news",
            expected_plan_sha256=preview["plan_sha256"],
            confirmation_text="robotics_news",
            actor="admin",
        )
        with self.assertRaisesRegex(ValueError, "已删除"):
            self.service.preview(
                "robotics_news", target_version_id=int(published["id"])
            )

    def test_switch_rejects_missing_current_published_baseline(self):
        self.database.connection.execute(
            "DELETE FROM industry_pack_versions WHERE industry_pack_id='family_office'"
        )
        self.database.connection.commit()
        version = self._publish("education_news")
        with self.assertRaisesRegex(ValueError, "尚无已发布基线"):
            self.service.preview(
                "education_news", target_version_id=int(version["id"])
            )

    def test_activation_requires_exact_version_and_plan_then_creates_valid_backup(self):
        version = self._publish()
        preview = self.service.preview("education_news")
        with self.assertRaisesRegex(ValueError, "计划已经变化"):
            self.service.activate(
                "education_news",
                target_version_id=version["id"],
                expected_plan_sha256="stale-plan",
            )
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM industry_pack_activations"
            ).fetchone()[0],
            0,
        )

        result = self.service.activate(
            "education_news",
            target_version_id=version["id"],
            expected_plan_sha256=preview["plan_sha256"],
            actor="admin",
        )
        backup_path = Path(result["backup"]["path"])
        self.assertTrue(backup_path.is_file())
        self.assertEqual(
            hashlib.sha256(backup_path.read_bytes()).hexdigest(),
            result["backup"]["sha256"],
        )
        with sqlite3.connect(backup_path) as backup:
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            # Backup happens before the activation transaction by design.
            self.assertEqual(
                backup.execute(
                    "SELECT COUNT(*) FROM industry_pack_activations"
                ).fetchone()[0],
                0,
            )
        self.assertEqual(self._setting("active_industry_pack_id"), "education_news")
        self.assertEqual(
            self._setting("active_industry_pack_version_id"), str(version["id"])
        )
        record = self.database.connection.execute(
            "SELECT status, is_current, backup_integrity FROM industry_pack_activations"
        ).fetchone()
        self.assertEqual(tuple(record), ("active", 1, "ok"))
        stages = [
            row[0]
            for row in self.database.connection.execute(
                "SELECT stage FROM industry_pack_activation_events ORDER BY id"
            ).fetchall()
        ]
        self.assertEqual(
            stages,
            [
                "applying",
                "verifying",
                "legacy_url_projection_synced",
                "collection_tasks_projected",
                "restoring_projection",
                "content_preservation_verified",
                "active",
            ],
        )
        self.assertTrue(result["backup"]["table_counts_verified"])
        self.assertEqual(
            result["content_preservation"]["tables"],
            {"articles": 0, "article_spacetime_profiles": 0},
        )

    def test_legacy_projection_is_revalidated_and_restored_to_new_activation(self):
        article_ids = []
        for slug, title, content in (
            (
                "matching",
                "家族办公室恢复验收",
                "家族办公室与财富传承信息仍命中当前项目关键词。",
            ),
            ("unrelated", "普通办公家具", "这是一条与目标行业无关的旧内容。"),
        ):
            article_id = int(
                self.database.connection.execute(
                    """
                    INSERT INTO articles(
                        url,title,content,domain,publish_date,status,
                        content_hash,content_length
                    ) VALUES(?,?,?,?,date('now'),'active',?,?)
                    """,
                    (
                        f"https://projection.example.test/{slug}",
                        title,
                        content,
                        "projection.example.test",
                        f"projection-{slug}",
                        len(content),
                    ),
                ).lastrowid
            )
            article_ids.append(article_id)
            self.repository.upsert_classification(
                {
                    "article_id": article_id,
                    "industry_pack_id": "family_office",
                    "activation_id": "",
                    "industry_pack_version": "1.0.0",
                    "classifier_version": "legacy-fixture",
                    "article_content_hash": f"projection-{slug}",
                    "rule_category": "event",
                    "rule_confidence": 1,
                    "rule_reason": "legacy",
                    "score_details": {
                        "hits": {"anchor": [], "trend": ["政策"]},
                    },
                    "matched_keywords": ["旧关键词", "政策"],
                    "topic_tags": ["政策与税务"],
                    "final_category": "event",
                    "final_confidence": 1,
                    "final_reason": "legacy",
                }
            )
        self.database.connection.commit()

        preview = self.service.preview(
            "family_office", target_version_id=self.family_baseline["id"]
        )
        result = self.service.activate(
            "family_office",
            target_version_id=self.family_baseline["id"],
            expected_plan_sha256=preview["plan_sha256"],
        )
        restored = result["restored_projection"]
        self.assertEqual(restored["classification_rows_evaluated"], 2)
        self.assertEqual(restored["classification_rows_restored"], 1)
        self.assertEqual(restored["classification_rows_left_hidden"], 1)
        self.assertEqual(
            restored["classification_rows_reclassification_required"], 1
        )
        matching = self.database.connection.execute(
            """
            SELECT activation_id,industry_pack_version,
                   json_extract(score_details_json,'$.hits.anchor[0]'),
                   matched_keywords_json,topic_tags_json
            FROM article_intel_classifications WHERE article_id=?
            """,
            (article_ids[0],),
        ).fetchone()
        self.assertEqual(matching[0], result["activation_id"])
        # Projection restoration is not reclassification. Keep the producing
        # version and category metadata so the normal sweep can upgrade it.
        self.assertEqual(matching[1], "1.0.0")
        self.assertEqual(matching[2], "家族办公室")
        self.assertEqual(json.loads(matching[3]), ["旧关键词", "政策"])
        self.assertEqual(json.loads(matching[4]), ["政策与税务"])
        unrelated = self.database.connection.execute(
            "SELECT activation_id FROM article_intel_classifications WHERE article_id=?",
            (article_ids[1],),
        ).fetchone()
        self.assertEqual(str(unrelated[0] or ""), "")
        _rows, total, _window = self.repository.list_classified_articles(
            industry_pack_id="family_office", time_range="7d"
        )
        self.assertEqual(total, 1)
        pending_ids = {
            int(row["id"])
            for row in self.repository.list_unclassified_articles(
                "family_office", limit=1000
            )
        }
        self.assertIn(article_ids[0], pending_ids)

        # Compatibility with rows damaged by the old activation path: the
        # structured trend evidence still identifies policy content even when
        # the denormalized keyword/topic lists contain anchors only.
        self.database.connection.execute(
            """
            UPDATE article_intel_classifications
            SET matched_keywords_json='["家族办公室"]', topic_tags_json='[]'
            WHERE article_id=?
            """,
            (article_ids[0],),
        )
        self.database.connection.commit()
        policy_rows, policy_total, _window = (
            self.repository.list_classified_articles(
                industry_pack_id="family_office",
                time_range="7d",
                policy_only=True,
            )
        )
        self.assertEqual(policy_total, 1)
        self.assertEqual(policy_rows[0]["article_id"], article_ids[0])

    def test_failure_during_apply_rolls_back_settings_sources_and_activation(self):
        first = self._publish()
        first_preview = self.service.preview("education_news")
        first_result = self.service.activate(
            "education_news",
            target_version_id=first["id"],
            expected_plan_sha256=first_preview["plan_sha256"],
        )
        source_count = self.database.connection.execute(
            "SELECT COUNT(*) FROM intel_sources"
        ).fetchone()[0]

        def mutate(manifest):
            manifest["pack_version"] = "2.0.1"
            manifest["default_sources"].append(
                {
                    "name": "Atomic failure fixture",
                    "url": "https://failure.example.test/news",
                    "source_type": "website",
                    "authority_level": 2,
                    "polling_interval_minutes": 60,
                }
            )

        second = self._publish(mutate=mutate)
        second_preview = self.service.preview("education_news")

        def fail_inside_transaction(*args, **kwargs):
            cursor = kwargs["transaction_cursor"]
            cursor.execute(
                """
                INSERT INTO intel_runtime_settings(setting_key, setting_value)
                VALUES('partial-write-fixture', 'must-rollback')
                """
            )
            cursor.execute(
                """
                INSERT INTO intel_sources(
                    canonical_source_url, source_url, source_name,
                    source_type, content_type, authority_level,
                    polling_interval_minutes, is_enabled, metadata_json
                ) VALUES(?, ?, 'partial', 'website', 'other', 1, 60, 1, '{}')
                """,
                (
                    canonicalize_source_url("https://partial.example.test"),
                    "https://partial.example.test",
                ),
            )
            raise RuntimeError("forced apply failure")

        with patch.object(
            self.registry, "apply_source_reconciliation", side_effect=fail_inside_transaction
        ):
            with self.assertRaisesRegex(RuntimeError, "forced apply failure"):
                self.service.activate(
                    "education_news",
                    target_version_id=second["id"],
                    expected_plan_sha256=second_preview["plan_sha256"],
                )

        self.assertEqual(
            self._setting("active_industry_pack_version_id"), str(first["id"])
        )
        self.assertEqual(self._setting("partial-write-fixture"), "")
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM intel_sources"
            ).fetchone()[0],
            source_count,
        )
        activations = self.database.connection.execute(
            "SELECT id, status, is_current FROM industry_pack_activations"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in activations],
            [(first_result["activation_id"], "active", 1)],
        )

    def test_configuration_rollback_restores_exact_old_version_and_source_set(self):
        first = self._publish()
        first_preview = self.service.preview("education_news")
        self.service.activate(
            "education_news",
            target_version_id=first["id"],
            expected_plan_sha256=first_preview["plan_sha256"],
        )
        first_keywords = list(first["manifest"]["core_keywords"])

        def mutate(manifest):
            manifest["pack_version"] = "2.0.2"
            manifest["core_keywords"] = ["回滚隔离测试关键词"]
            manifest["default_sources"].append(
                {
                    "name": "Rollback fixture",
                    "url": "https://rollback.example.test/news",
                    "source_type": "website",
                    "authority_level": 2,
                    "polling_interval_minutes": 60,
                }
            )

        second = self._publish(mutate=mutate)
        second_preview = self.service.preview("education_news")
        second_result = self.service.activate(
            "education_news",
            target_version_id=second["id"],
            expected_plan_sha256=second_preview["plan_sha256"],
        )
        self.assertEqual(
            self.loader.load("education_news")["core_keywords"],
            ["回滚隔离测试关键词"],
        )

        rollback_preview = self.service.preview_rollback(
            second_result["activation_id"]
        )
        rollback = self.service.rollback(
            second_result["activation_id"],
            target_version_id=rollback_preview["target_version_id"],
            expected_plan_sha256=rollback_preview["plan_sha256"],
            actor="admin",
        )
        self.assertEqual(rollback["active_version_id"], first["id"])
        self.assertEqual(self.loader.load("education_news")["core_keywords"], first_keywords)
        association = self.database.connection.execute(
            """
            SELECT si.is_active
            FROM intel_source_industries si
            JOIN intel_sources s ON s.id=si.source_id
            WHERE s.canonical_source_url=? AND si.industry_pack_id='education_news'
            """,
            (canonicalize_source_url("https://rollback.example.test/news"),),
        ).fetchone()
        self.assertEqual(int(association[0]), 0)
        rolled_back = self.database.connection.execute(
            "SELECT status, is_current FROM industry_pack_activations WHERE id=?",
            (second_result["activation_id"],),
        ).fetchone()
        self.assertEqual(tuple(rolled_back), ("rolled_back", 0))
        current = self.database.connection.execute(
            "SELECT target_version_id, status, is_current FROM industry_pack_activations WHERE id=?",
            (rollback["activation_id"],),
        ).fetchone()
        self.assertEqual(tuple(current), (first["id"], "active", 1))


if __name__ == "__main__":
    unittest.main()
