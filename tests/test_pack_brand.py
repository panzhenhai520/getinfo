import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

config.DATABASE_TYPE = "sqlite"

from sqlite_database import SQLiteDatabase  # noqa: E402


class PackBrandTests(unittest.TestCase):
    """多租户白标：pack_users 的品牌开关与 Logo"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(str(Path(self.temp_dir.name) / "brand.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        import pack_tenant
        self._orig_db = pack_tenant.sqlite_db
        pack_tenant.sqlite_db = self.db
        pack_tenant._ensure()

    def tearDown(self):
        import pack_tenant
        pack_tenant.sqlite_db = self._orig_db
        self.db.disconnect()
        self.temp_dir.cleanup()

    def _create_user(self, **kw):
        import pack_tenant
        uid = pack_tenant.create_pack_user(
            industry_pack_id=kw.get("industry_pack_id", "auto_test"),
            username=kw.get("username", "branduser"),
            password="Pass1234",
            email=kw.get("email", "brand@example.com"),
            company_name=kw.get("company_name", "木链科技"),
            auth_days=kw.get("auth_days", 30),
        )
        return uid

    def test_columns_and_flag_update(self):
        import pack_tenant
        uid = self._create_user()
        profile = pack_tenant.update_pack_user(uid, show_user_brand=True)
        self.assertEqual(profile["show_user_brand"], 1)
        profile = pack_tenant.update_pack_user(uid, show_user_brand=False)
        self.assertEqual(profile["show_user_brand"], 0)

    def test_brand_override_requires_flag(self):
        import pack_tenant
        uid = self._create_user(company_name="木链科技")
        fake_user = {
            "id": uid, "industry_pack_id": "auto_test", "company_name": "木链科技",
            "logo_url": "", "show_user_brand": 0, "status": "active", "expire_at": "2099-12-31",
        }
        with patch.object(pack_tenant, "current_pack_user", return_value=fake_user):
            self.assertIsNone(pack_tenant.brand_override())
        fake_user["show_user_brand"] = 1
        with patch.object(pack_tenant, "current_pack_user", return_value=fake_user):
            override = pack_tenant.brand_override()
            self.assertEqual(override["name"], "木链科技")
            self.assertEqual(override["logo_url"], "")

    def test_brand_override_with_logo(self):
        import pack_tenant
        uid = self._create_user(company_name="木链科技")
        fake_user = {
            "id": uid, "industry_pack_id": "auto_test", "company_name": "木链科技",
            "logo_url": "/static/uploads/pack_logo_1.png", "show_user_brand": 1,
            "status": "active", "expire_at": "2099-12-31",
        }
        with patch.object(pack_tenant, "current_pack_user", return_value=fake_user):
            override = pack_tenant.brand_override()
            self.assertEqual(override["logo_url"], "/static/uploads/pack_logo_1.png")

    def test_brand_override_none_without_name_and_logo(self):
        import pack_tenant
        uid = self._create_user(company_name="")
        fake_user = {
            "id": uid, "industry_pack_id": "auto_test", "company_name": "", "logo_url": "",
            "show_user_brand": 1, "status": "active", "expire_at": "2099-12-31",
        }
        with patch.object(pack_tenant, "current_pack_user", return_value=fake_user):
            self.assertIsNone(pack_tenant.brand_override())

    def test_set_user_logo_writes_url(self):
        import pack_tenant
        uid = self._create_user()

        class FakeFile:
            filename = "logo.png"

            def save(self, path):
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_bytes(b"\x89PNG\r\n")

        tmp_upload = Path(self.temp_dir.name) / "uploads"
        with patch.object(pack_tenant, "_LOGO_UPLOAD_DIR", str(tmp_upload)):
            rel = pack_tenant.set_user_logo(uid, FakeFile())
        self.assertTrue(rel.startswith("/static/uploads/pack_logo_"))
        saved = Path(tmp_upload) / f"pack_logo_{uid}.png"
        self.assertTrue(saved.exists())
        profile = pack_tenant.get_profile(uid)
        self.assertEqual(profile["logo_url"], rel)


if __name__ == "__main__":
    unittest.main()
