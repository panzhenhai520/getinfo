#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 9 · 业务规则引擎测试：包配置迁移、幂等、命中补丁、禁用生效。

口径：规则只做**检索侧增强**（补检索式/加权词），不改证据闸门；
没命中规则时补丁必须是空的（保证单跳问题行为与以前逐字一致）。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from business_rules import BusinessRuleEngine  # noqa: E402
from intel_database import IntelRepository  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

PACK = {
    "id": "family_office",
    "name": "家族办公室",
    "version": "9.9.9",
    "core_keywords": ["家族办公室", "家族信托"],
    "fixed_topics": [
        {"name": "税务宽免", "search_queries": ["税务宽免 政策原文", "税务宽免 适用条件"]},
        "家办监管",
    ],
}


class BusinessRuleEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "rules.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.repo = IntelRepository(self.db)
        self.repo._ensure()
        self.engine = BusinessRuleEngine(repository=self.repo)

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def test_sync_from_pack_config(self):
        with mock.patch("pack_attention.list_directions", return_value=[]):
            result = self.engine.sync_from_pack("family_office", PACK)
        self.assertEqual(result["rules"], 4, "2 个核心关键词 + 2 个固定主题")
        self.assertEqual(result["written"], 4)
        rules = self.engine.list_rules("family_office")
        self.assertEqual(len(rules), 4)
        keys = {rule["rule_key"] for rule in rules}
        self.assertIn("keyword:家族信托", keys)
        self.assertIn("topic:税务宽免", keys)
        topic = next(rule for rule in rules if rule["rule_key"] == "topic:税务宽免")
        self.assertEqual(topic["source"], "pack_config")
        self.assertEqual(topic["priority"], 30)
        action = json.loads(topic["action_json"])
        self.assertIn("税务宽免 政策原文", action["retrieval_queries"])

    def test_sync_is_idempotent(self):
        with mock.patch("pack_attention.list_directions", return_value=[]):
            self.engine.sync_from_pack("family_office", PACK)
            self.engine.sync_from_pack("family_office", PACK)
        self.assertEqual(len(self.engine.list_rules("family_office")), 4, "重复同步不得产生重复规则")

    def test_attention_directions_become_rules(self):
        direction = [{"direction": "AI 芯片出口管制", "keywords_json": json.dumps(["出口管制", "芯片"]),
                      "status": "active", "week_key": "2026-W41", "reason": "本周关注"}]
        with mock.patch("pack_attention.list_directions", return_value=direction):
            self.engine.sync_from_pack("family_office", PACK)
        rules = {rule["rule_key"]: rule for rule in self.engine.list_rules("family_office")}
        attention_key = [key for key in rules if key.startswith("attention:")]
        self.assertTrue(attention_key, "追踪方向应成为规则")
        rule = rules[attention_key[0]]
        self.assertEqual(rule["source"], "attention")
        self.assertEqual(rule["priority"], 40, "追踪方向优先级高于固定主题")

    def test_match_builds_patch(self):
        with mock.patch("pack_attention.list_directions", return_value=[]):
            self.engine.sync_from_pack("family_office", PACK)
        patch = self.engine.match("家族信托的税务宽免有哪些适用条件？", pack_id="family_office")
        self.assertTrue(patch["matched"])
        self.assertIn("家族信托", patch["boost_terms"])
        self.assertTrue(patch["retrieval_queries"], "命中规则要补检索式")
        self.assertIn("税务宽免 政策原文", patch["retrieval_queries"])

    def test_no_match_returns_empty_patch(self):
        with mock.patch("pack_attention.list_directions", return_value=[]):
            self.engine.sync_from_pack("family_office", PACK)
        patch = self.engine.match("今天天气怎么样？", pack_id="family_office")
        self.assertEqual(patch["matched"], [])
        self.assertEqual(patch["retrieval_queries"], [])
        self.assertEqual(patch["boost_terms"], [])

    def test_disabled_rule_not_matched(self):
        with mock.patch("pack_attention.list_directions", return_value=[]):
            self.engine.sync_from_pack("family_office", PACK)
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("UPDATE intel_business_rules SET is_enabled=0")
                self.db.connection.commit()
            finally:
                cursor.close()
        patch = self.engine.match("家族信托的税务宽免有哪些适用条件？", pack_id="family_office")
        self.assertEqual(patch["matched"], [])
        self.assertEqual(len(self.engine.list_rules("family_office", enabled_only=False)), 4)


if __name__ == "__main__":
    unittest.main()
