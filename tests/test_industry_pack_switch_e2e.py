import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from industry_pack_activation import verify_sqlite_backup
from intel_api import intel_bp
from tools.check_industry_pack_switch_e2e import run


class IndustryPackSwitchEndToEndTest(unittest.TestCase):
    def test_isolated_family_education_rollback_acceptance(self):
        result = run()
        self.assertTrue(result["passed"], result)
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual(result["activations"]["count"], 3)
        self.assertEqual(result["source_counts"]["shared_financial"], 7)

    def test_backup_verifier_is_read_only_and_rejects_wrong_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "backup.sqlite3"
            # sqlite3.Connection 的上下文管理器只提交/回滚，**不会关闭连接**；
            # 未关闭的句柄会让 TemporaryDirectory 清理在 Windows 上失败，必须显式 close。
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE fixture(id INTEGER PRIMARY KEY)")
                connection.commit()
            finally:
                connection.close()
            before = path.read_bytes()
            result = verify_sqlite_backup(str(path), expected_sha256="wrong")
            self.assertFalse(result["passed"])
            self.assertFalse(result["checks"]["sha256"])
            self.assertTrue(result["checks"]["integrity"])
            self.assertTrue(result["read_only"])
            self.assertEqual(path.read_bytes(), before)

    def test_legacy_restore_api_is_a_non_restorable_audit_surface(self):
        app = Flask(__name__)
        app.register_blueprint(intel_bp)
        client = app.test_client()
        administrator = {"user_id": 1, "username": "admin", "role": "admin"}
        with patch(
            "decorators.user_db.verify_session", return_value=administrator
        ), patch(
            "intel_api.intel_repository.restore_pack_backup"
        ) as legacy_restore, patch(
            "intel_api.intel_repository.list_pack_backups", return_value=[]
        ):
            listed = client.get(
                "/api/intel/industry-packs/backups",
                headers={"Authorization": "Bearer fixture"},
            )
            self.assertEqual(listed.status_code, 200)
            self.assertTrue(listed.get_json()["deprecated"])
            self.assertFalse(listed.get_json()["restorable"])
            restored = client.post(
                "/api/intel/industry-packs/backups/1/restore",
                headers={"Authorization": "Bearer fixture"},
            )
            self.assertEqual(restored.status_code, 410)
            self.assertFalse(restored.get_json()["success"])
            legacy_restore.assert_not_called()


if __name__ == "__main__":
    unittest.main()
