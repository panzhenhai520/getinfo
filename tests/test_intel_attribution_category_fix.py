#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""钉住「兜底归属不再硬编码 other，且只升级不破坏」这条修复。

背景（A 机 invest_mgmt 实测）：intel_attribution.ensure_pack_attribution 原先在 INSERT 时
把 rule_category/final_category 硬编码成 'other'，并且用 ON CONFLICT DO NOTHING 写入。
后果：该包 860 行兜底里 448 行永久停在「其他」（classifier_version='fallback-attribution-v1'），
其中 160 篇按当前已发布配置重跑规则会判 event/trend —— 库内 other 占比 93.37%，
而"用当前配置重跑"只有 74.77%，差 18.6 个百分点全由这条链路造成。

本测试全部跑在隔离的临时 SQLite 上：
  * 导入 config 之前就把 DATABASE_TYPE / DATABASE_PATH / SQLITE_BACKUP_PATH 指向临时库；
  * setUp 里再次强制 config.DATABASE_TYPE='sqlite' 并断言 db.backend == 'sqlite'；
  * 分类走**桩行业包**驱动的纯规则分类器，不调 LLM、不连任何远程机器。
"""
import ast
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# 必须在 import config 之前：config._load_dotenv_file() 只在键"不在环境里"时才写 .env 的值。
_TEMP_MAIN = tempfile.mkdtemp(prefix="collectinfo-attribution-")
os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["DATABASE_PATH"] = os.path.join(_TEMP_MAIN, "main.sqlite3")
os.environ["SQLITE_BACKUP_PATH"] = os.path.join(_TEMP_MAIN, "main.sqlite3")

import config  # noqa: E402

config.DATABASE_TYPE = "sqlite"
config.INTEL_LLM_ENABLED = False

import industry_packs  # noqa: E402
import intel_attribution  # noqa: E402
import sqlite_database  # noqa: E402
from intel_attribution import (  # noqa: E402
    ensure_pack_attribution,
    repair_fallback_attributions,
)
from intel_classifier import classify_article  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

PACK_ID = "test_pack_attribution"
ANCHOR = "智算中心"
SECOND_ANCHOR = "算力网络"

EVENT_TITLE = "智算中心项目签约落地"
EVENT_BODY = "智算中心项目正式签约，由地方国资与运营商共同推进，总投资额约 30 亿元。"
TREND_TITLE = "智算中心并购整合加速"
TREND_BODY = "智算中心行业并购持续升温，头部厂商通过并购补齐算力供给能力。"
# 既没有行业锚点、也没有趋势/事件词的普通资讯：规则只能说 other。
PLAIN_TITLE = "某机构发布年度合规操作指引"
PLAIN_BODY = "该机构发布了年度合规操作指引，" + "操作细则" * 40 + "。"


def _pack() -> dict:
    """桩行业包：锚点=智算中心/算力网络，趋势词=并购，事件词=签约。"""
    return {
        "id": PACK_ID,
        "pack_version": "test-pack-v1",
        "classification": {
            "core_weight": 3,
            "expanded_weight": 1,
            "trend_weight": 2,
            "event_weight": 3,
            "negative_weight": -3,
            "minimum_relevance_score": 2,
            "llm_confidence_threshold": 0.9,
            "tie_break_order": ["trend", "event", "other"],
            "recent_today_window_days": 5,
            "recent_trend_window_days": 21,
        },
        "candidate_gate": {"anchor_keywords": [ANCHOR, SECOND_ANCHOR], "entity_keywords": []},
        "core_keywords": [ANCHOR],
        "expanded_keywords": [],
        "trend_keywords": ["并购"],
        "event_keywords": ["签约"],
        "negative_keywords": [],
        "brands": [],
        "fixed_topics": [],
        "default_sources": [],
    }


class _StubPackLoader:
    """只回桩包的加载器：把 industry_pack_loader 换掉，测试不读真实包配置。"""

    def __init__(self, pack: dict):
        self.pack = pack

    def load(self, pack_id=PACK_ID, **_kwargs):
        if str(pack_id) != PACK_ID:
            raise ValueError("unknown pack: %s" % pack_id)
        return copy.deepcopy(self.pack)

    def list(self, **_kwargs):
        return [copy.deepcopy(self.pack)]


class AttributionCategoryFixTest(unittest.TestCase):
    """兜底归属落库口径 + 存量修复（全部在隔离临时 SQLite 上跑）。"""

    def setUp(self):
        self._orig_db_type = getattr(config, "DATABASE_TYPE", None)
        self._orig_llm_enabled = getattr(config, "INTEL_LLM_ENABLED", None)
        config.DATABASE_TYPE = "sqlite"
        config.INTEL_LLM_ENABLED = False

        self.temp_dir = tempfile.TemporaryDirectory(prefix="attribution-case-")
        self.db_path = os.path.join(self.temp_dir.name, "attribution.sqlite3")
        self.db = SQLiteDatabase(self.db_path)
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        self.assertEqual(
            "sqlite",
            self.db.backend,
            "测试必须跑在隔离的临时 SQLite 上：连到主库会写入夹具、污染真实数据",
        )
        self.db.analyze_article_spacetime_profile = lambda _article_id: None

        self.pack = _pack()
        self.loader = _StubPackLoader(self.pack)
        self._patches = [
            patch.object(industry_packs, "industry_pack_loader", self.loader),
            # repair_fallback_attributions 通过模块单例取库；把它换成临时库。
            patch.object(sqlite_database, "sqlite_db", self.db),
        ]
        for item in self._patches:
            item.start()
        self._url_seq = 0

    def tearDown(self):
        for item in self._patches:
            item.stop()
        try:
            self.db.disconnect()
        except Exception:
            pass
        self.temp_dir.cleanup()
        config.DATABASE_TYPE = self._orig_db_type
        config.INTEL_LLM_ENABLED = self._orig_llm_enabled

    # ---------------- 夹具与查询助手 ----------------

    def _insert_article(self, title: str, content: str, *, publish_date: str = "") -> int:
        self._url_seq += 1
        url = "https://example.test/attribution/%d" % self._url_seq
        with self.db.lock:
            cursor = self.db.connection.cursor()
            cursor.execute(
                "INSERT INTO articles (url, title, content, matched_keywords, publish_date, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (url, title, content, "", publish_date, hashlib.sha256(content.encode("utf-8")).hexdigest()),
            )
            article_id = int(cursor.lastrowid)
            self.db.connection.commit()
            cursor.close()
        return article_id

    def _insert_classification(
        self,
        article_id: int,
        *,
        final_category: str,
        result_source: str,
        classifier_version: str,
        rule_category: str = "",
        rule_reason: str = "夹具预置",
        final_reason: str = "夹具预置",
        industry_pack_id: str = PACK_ID,
    ) -> None:
        with self.db.lock:
            cursor = self.db.connection.cursor()
            cursor.execute(
                "INSERT INTO article_intel_classifications ("
                "  article_id, industry_pack_id, activation_id, industry_pack_version,"
                "  classifier_version, article_content_hash, rule_category, rule_confidence,"
                "  rule_reason, score_details_json, matched_keywords_json, topic_tags_json,"
                "  final_category, final_confidence, final_reason, result_source,"
                "  classified_at, created_at, updated_at"
                ") VALUES (?, ?, '', 'test-pack-v1', ?, 'hash-fixture', ?, 0.1, ?, '{}', '[]', '[]',"
                "          ?, 0.1, ?, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z',"
                "          '2026-01-01T00:00:00Z')",
                (
                    int(article_id), industry_pack_id, classifier_version,
                    rule_category or final_category, rule_reason,
                    final_category, final_reason, result_source,
                ),
            )
            self.db.connection.commit()
            cursor.close()

    def _classification_row(self, article_id: int, pack_id: str = PACK_ID) -> dict:
        with self.db.lock:
            cursor = self.db.connection.cursor()
            cursor.execute(
                "SELECT * FROM article_intel_classifications "
                "WHERE article_id=? AND industry_pack_id=?",
                (int(article_id), pack_id),
            )
            row = cursor.fetchone()
            cursor.close()
        return dict(row) if row is not None else {}

    def _classification_snapshot(self) -> list:
        with self.db.lock:
            cursor = self.db.connection.cursor()
            cursor.execute("SELECT * FROM article_intel_classifications ORDER BY id")
            rows = [dict(row) for row in cursor.fetchall()]
            cursor.close()
        return rows

    def _articles_snapshot(self) -> list:
        with self.db.lock:
            cursor = self.db.connection.cursor()
            cursor.execute("SELECT * FROM articles ORDER BY id")
            rows = [dict(row) for row in cursor.fetchall()]
            cursor.close()
        return rows

    def _rule_verdict(self, title: str, content: str, *, publish_date: str = "") -> dict:
        payload = {"title": title, "content": content, "matched_keywords": ""}
        if publish_date:
            payload["publish_date"] = publish_date
        return classify_article(payload, self.pack)

    def _restore_from_backup(self, backup_path: str) -> int:
        """按备份 JSON 里的 columns/rows 逐行还原（证明备份真的能回滚）。"""
        with open(backup_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        columns = list(payload["columns"])
        assignments = ", ".join("%s = ?" % column for column in columns)
        with self.db.lock:
            cursor = self.db.connection.cursor()
            for row in payload["rows"]:
                cursor.execute(
                    "UPDATE %s SET %s WHERE %s = ?"
                    % (payload["table"], assignments, payload["key_column"]),
                    tuple(row[column] for column in columns)
                    + (row[payload["key_column"]],),
                )
            self.db.connection.commit()
            cursor.close()
        return len(payload["rows"])

    # ---------------- 1. 兜底路径落库等于规则结论 ----------------

    def test_fallback_insert_stores_rule_category_not_other(self):
        article_id = self._insert_article(EVENT_TITLE, EVENT_BODY)
        expected = self._rule_verdict(EVENT_TITLE, EVENT_BODY)
        self.assertEqual("event", expected["final_category"], "夹具必须被规则判为 event")

        outcome = ensure_pack_attribution(
            self.db, article_id, {"title": EVENT_TITLE, "content": EVENT_BODY, "matched_keywords": ""}
        )

        self.assertEqual([PACK_ID], outcome["attributed"])
        self.assertEqual("keyword", outcome["source"])
        row = self._classification_row(article_id)
        self.assertTrue(row, "兜底归属必须落一行")
        self.assertEqual(expected["final_category"], row["final_category"])
        self.assertNotEqual("other", row["final_category"], "兜底行不得再硬编码「其他」")
        self.assertEqual(expected["rule_category"], row["rule_category"])
        self.assertEqual("rule", row["result_source"])
        self.assertEqual("rule-v1", row["classifier_version"])
        self.assertIn("签约", row["matched_keywords_json"])
        score_details = json.loads(row["score_details_json"])
        self.assertTrue(
            (score_details.get("hits") or {}).get("anchor"),
            "score_details 必须是真实命中证据（检索质量门要用）",
        )
        self.assertEqual(expected["final_confidence"], row["final_confidence"])

    def test_recent_anchor_article_uses_temporal_rule_category(self):
        """锚点达标但没有趋势/事件词的近期文章：规则判 event，兜底行也必须落 event。"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        title = "智算中心与算力网络建设进展"
        body = "智算中心与算力网络的建设进度按计划推进，两地机房已完成主体施工。"
        expected = self._rule_verdict(title, body, publish_date=today)
        self.assertEqual("event", expected["final_category"],
                         "夹具必须命中 temporal_fallback（依赖发布时间）")
        self.assertEqual(
            "other",
            self._rule_verdict(title, body)["final_category"],
            "不传发布时间时规则只能判 other —— 这正是兜底路径必须透传 publish_date 的原因",
        )

        article_id = self._insert_article(title, body, publish_date=today)
        ensure_pack_attribution(
            self.db, article_id, {"title": title, "content": body, "publish_date": today}
        )
        row = self._classification_row(article_id)
        self.assertEqual("event", row["final_category"])

    # ---------------- 2. 真分类行绝不被覆盖 ----------------

    def test_real_classification_row_is_never_overwritten(self):
        article_id = self._insert_article(EVENT_TITLE, EVENT_BODY)
        self._insert_classification(
            article_id,
            final_category="trend",
            result_source="llm_override",
            classifier_version="rule-v1",
            final_reason="LLM 判定为趋势",
        )
        before = self._classification_row(article_id)

        outcome = ensure_pack_attribution(
            self.db, article_id, {"title": EVENT_TITLE, "content": EVENT_BODY}
        )

        self.assertEqual("existing", outcome["source"])
        self.assertEqual([PACK_ID], outcome["attributed"])
        self.assertEqual([], outcome["upgraded"])
        after = self._classification_row(article_id)
        self.assertEqual(before, after, "非兜底来源的行必须一个字段都不变")
        self.assertEqual("trend", after["final_category"])
        self.assertEqual("llm_override", after["result_source"])

    # ---------------- 3. 兜底行被升级 ----------------

    def test_fallback_row_is_upgraded_by_rule_verdict(self):
        article_id = self._insert_article(TREND_TITLE, TREND_BODY)
        self._insert_classification(
            article_id,
            final_category="other",
            rule_category="other",
            result_source="fallback_attribution",
            classifier_version="fallback-attribution-v1",
            final_reason="关键词命中兜底归属（分类任务未产出归属，先归入「其他」分类）",
        )
        expected = self._rule_verdict(TREND_TITLE, TREND_BODY)
        self.assertEqual("trend", expected["final_category"])

        outcome = ensure_pack_attribution(
            self.db, article_id, {"title": TREND_TITLE, "content": TREND_BODY}
        )

        self.assertEqual("existing_fallback", outcome["source"])
        self.assertEqual([PACK_ID], outcome["upgraded"])
        row = self._classification_row(article_id)
        self.assertEqual("trend", row["final_category"], "兜底 other 行必须被规则结论升级")
        self.assertEqual("rule", row["result_source"], "result_source 必须换成规则来源")
        self.assertEqual("rule-v1", row["classifier_version"])

    def test_ensure_upgrade_is_idempotent(self):
        article_id = self._insert_article(TREND_TITLE, TREND_BODY)
        self._insert_classification(
            article_id,
            final_category="other",
            rule_category="other",
            result_source="fallback_attribution",
            classifier_version="fallback-attribution-v1",
        )
        ensure_pack_attribution(self.db, article_id, {"title": TREND_TITLE, "content": TREND_BODY})
        upgraded_row = self._classification_row(article_id)

        second = ensure_pack_attribution(
            self.db, article_id, {"title": TREND_TITLE, "content": TREND_BODY}
        )

        self.assertEqual([], second["upgraded"])
        self.assertEqual("existing", second["source"], "升级后已是非兜底来源 → 不再触碰")
        self.assertEqual(upgraded_row, self._classification_row(article_id))

    # ---------------- 4. dry_run 不写库 / 非 dry_run 写库并备份 ----------------

    def test_repair_dry_run_writes_nothing_and_apply_writes_with_backup(self):
        upgrade_id = self._insert_article(TREND_TITLE, TREND_BODY)
        keep_id = self._insert_article(PLAIN_TITLE, PLAIN_BODY)
        for article_id in (upgrade_id, keep_id):
            self._insert_classification(
                article_id,
                final_category="other",
                rule_category="other",
                result_source="fallback_attribution",
                classifier_version="fallback-attribution-v1",
            )
        self.assertEqual("other", self._rule_verdict(PLAIN_TITLE, PLAIN_BODY)["final_category"],
                         "夹具二必须规则判 other（无可升级空间）")
        before = self._classification_snapshot()
        backup_dir = os.path.join(self.temp_dir.name, "backups")

        dry = repair_fallback_attributions(dry_run=True, backup_dir=backup_dir)

        self.assertEqual(2, dry["scanned"])
        self.assertEqual(1, dry["upgraded"])
        self.assertEqual(1, dry["unchanged"])
        self.assertTrue(dry["dry_run"])
        self.assertEqual(1, len(dry["samples"]))
        self.assertEqual("other", dry["samples"][0]["from_category"])
        self.assertEqual("trend", dry["samples"][0]["to_category"])
        self.assertNotIn("backup_path", dry)
        self.assertEqual(before, self._classification_snapshot(), "dry_run 必须一个字节都不写")
        self.assertFalse(os.path.exists(backup_dir), "dry_run 不得创建备份文件/目录")

        applied = repair_fallback_attributions(dry_run=False, backup_dir=backup_dir)

        self.assertEqual(2, applied["scanned"])
        self.assertEqual(1, applied["upgraded"])
        self.assertEqual(1, applied["unchanged"])
        self.assertFalse(applied["dry_run"])
        backup_path = applied["backup_path"]
        self.assertTrue(os.path.isfile(backup_path), "非 dry_run 必须先落备份文件")
        self.assertTrue(
            os.path.abspath(backup_path).startswith(os.path.abspath(backup_dir)),
            "备份必须写在调用方给的 backup_dir 里",
        )
        self.assertEqual(1, applied["backup_rows"])
        self.assertIn("UPDATE article_intel_classifications", applied["rollback"])

        upgraded_row = self._classification_row(upgrade_id)
        self.assertEqual("trend", upgraded_row["final_category"])
        self.assertEqual("rule", upgraded_row["result_source"])
        self.assertEqual("rule-v1", upgraded_row["classifier_version"])
        kept_row = self._classification_row(keep_id)
        self.assertEqual("other", kept_row["final_category"])
        self.assertEqual("fallback_attribution", kept_row["result_source"],
                         "规则结论与现值一致的兜底行不该被改动")

    def test_default_backup_dir_is_repo_data(self):
        self.assertEqual(
            os.path.join(str(REPO_ROOT), "data"),
            os.path.normpath(intel_attribution._default_backup_dir()),
        )

    # ---------------- 5. 幂等 ----------------

    def test_repair_is_row_level_idempotent(self):
        upgrade_id = self._insert_article(TREND_TITLE, TREND_BODY)
        self._insert_classification(
            upgrade_id,
            final_category="other",
            rule_category="other",
            result_source="fallback_attribution",
            classifier_version="fallback-attribution-v1",
        )
        backup_dir = os.path.join(self.temp_dir.name, "backups")

        first = repair_fallback_attributions(dry_run=False, backup_dir=backup_dir)
        self.assertEqual(1, first["upgraded"])
        snapshot_after_first = self._classification_snapshot()

        second = repair_fallback_attributions(dry_run=False, backup_dir=backup_dir)
        third = repair_fallback_attributions(dry_run=True, backup_dir=backup_dir)

        self.assertEqual(0, second["upgraded"], "第二次运行必须 upgraded=0")
        self.assertEqual(0, second["scanned"], "升级后的行不再算兜底行")
        self.assertEqual(0, third["upgraded"])
        self.assertEqual(snapshot_after_first, self._classification_snapshot())
        self.assertNotIn("backup_path", second)

    # ---------------- 6. 回滚 ----------------

    def test_backup_restores_pre_repair_state(self):
        upgrade_id = self._insert_article(TREND_TITLE, TREND_BODY)
        keep_id = self._insert_article(PLAIN_TITLE, PLAIN_BODY)
        for article_id in (upgrade_id, keep_id):
            self._insert_classification(
                article_id,
                final_category="other",
                rule_category="other",
                result_source="fallback_attribution",
                classifier_version="fallback-attribution-v1",
            )
        before = self._classification_snapshot()
        articles_before = self._articles_snapshot()
        backup_dir = os.path.join(self.temp_dir.name, "backups")

        applied = repair_fallback_attributions(dry_run=False, backup_dir=backup_dir)
        self.assertEqual(1, applied["upgraded"])
        self.assertNotEqual(before, self._classification_snapshot(), "修复必须真的改了库")

        restored = self._restore_from_backup(applied["backup_path"])

        self.assertEqual(applied["backup_rows"], restored)
        self.assertEqual(before, self._classification_snapshot(), "按备份回滚后必须与修复前完全一致")
        rolled_back = self._classification_row(upgrade_id)
        self.assertEqual("other", rolled_back["final_category"])
        self.assertEqual("fallback_attribution", rolled_back["result_source"])
        self.assertEqual("fallback-attribution-v1", rolled_back["classifier_version"])
        self.assertEqual(articles_before, self._articles_snapshot(), "articles 表必须原样不动")

    # ---------------- 链路守卫 ----------------

    def test_attribution_module_has_no_llm_code_path(self):
        """兜底/修复链路必须是纯规则：代码里不许引用 LLM 客户端、融合函数或网络库。

        用 AST 取「被引用的名字」，不用字符串扫描——注释/文档串里提到 fuse_rule_and_llm
        （说明"不走它"）是正常的，只有真的引用才算违规。
        """
        source = (REPO_ROOT / "intel_attribution.py").read_text(encoding="utf-8")
        referenced = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced.add(node.attr)
            elif isinstance(node, ast.Import):
                referenced.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                referenced.add(str(node.module or "").split(".")[0])
                referenced.update(alias.name for alias in node.names)
        for forbidden in (
            "fuse_rule_and_llm", "intel_llm_client", "IntelLLMClient", "llm_client",
            "requests", "urllib", "httpx", "ragflow_llm_client",
        ):
            self.assertNotIn(forbidden, referenced,
                             "兜底归属链路不得引入 LLM/网络调用：%s" % forbidden)
        self.assertIn("classify_article", referenced, "兜底分类必须走纯规则分类器")
        self.assertFalse(config.INTEL_LLM_ENABLED, "本测试全程关闭 LLM")

    def test_repair_can_scope_to_one_pack(self):
        other_pack = "other_pack_no_match"
        upgrade_id = self._insert_article(TREND_TITLE, TREND_BODY)
        self._insert_classification(
            upgrade_id,
            final_category="other",
            rule_category="other",
            result_source="fallback_attribution",
            classifier_version="fallback-attribution-v1",
        )
        untouched_id = self._insert_article(TREND_TITLE, TREND_BODY)
        self._insert_classification(
            untouched_id,
            final_category="other",
            rule_category="other",
            result_source="fallback_attribution",
            classifier_version="fallback-attribution-v1",
            industry_pack_id=other_pack,
        )

        scoped = repair_fallback_attributions(pack_id=PACK_ID, dry_run=True)

        self.assertEqual(1, scoped["scanned"], "pack_id 过滤后只应扫到本包的行")
        self.assertEqual(1, scoped["upgraded"])
        self.assertEqual(other_pack, self._classification_row(untouched_id, other_pack)["industry_pack_id"])


if __name__ == "__main__":
    unittest.main()
