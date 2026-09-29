import tempfile
import unittest
from pathlib import Path

from sqlite_database import SQLiteDatabase
from tools.bootstrap_industry_pack_versions import bootstrap


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class IndustryPackSeedBootstrapTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.database = SQLiteDatabase(str(root / "bootstrap.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.backup_dir = root / "backups"

    def tearDown(self):
        self.database.disconnect()
        self.temporary.cleanup()

    def _run(self, apply):
        return bootstrap(
            self.database,
            pack_ids=["family_office", "education_news"],
            apply=apply,
            config_dir=str(PROJECT_ROOT / "config" / "industry_packs"),
            url_validator=lambda value: str(value),
            backup_dir=str(self.backup_dir),
        )

    def test_dry_run_is_read_only_then_apply_is_atomic_and_idempotent(self):
        preview = self._run(False)
        self.assertTrue(preview["dry_run"])
        self.assertFalse(preview["writes_performed"])
        self.assertEqual(preview["create_count"], 2)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM industry_pack_versions"
            ).fetchone()[0],
            0,
        )

        applied = self._run(True)
        self.assertEqual(applied["created_count"], 2)
        self.assertTrue(applied["backup"]["integrity"] == "ok")
        rows = self.database.connection.execute(
            """
            SELECT industry_pack_id, version_number FROM industry_pack_versions
            ORDER BY industry_pack_id
            """
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("education_news", 1), ("family_office", 1)],
        )

        repeated = self._run(True)
        self.assertEqual(repeated["created_count"], 0)
        self.assertFalse(repeated["writes_performed"])
        self.assertIsNone(repeated["backup"])


if __name__ == "__main__":
    unittest.main()
