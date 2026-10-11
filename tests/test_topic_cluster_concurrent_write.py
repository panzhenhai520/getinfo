#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""topic_cluster 回写并发丢数据的回归用例（事实 A）。

背景：`IntelTopicService.cluster` 是「作业开头读一遍 score_details 快照 → 逐行整块回写」
的读-改-写。生产库是 PostgreSQL READ COMMITTED（postgres_compat 把 SQLite 的
BEGIN IMMEDIATE 翻译成 BEGIN，连接又是 autocommit），同一事务里每条语句各取一次新快照，
事务保护不了这份读-改-写：另一路 classification 作业在作业窗口内写入的字段会被作业开头
的陈旧副本整块覆盖（生产实测：invest_mgmt 激活包 948 行里 338 行 score_details 缺
rule_signal，其中 300 行用同一份代码重跑能算出 rule_signal）。

隔离 sqlite 的并发注入说明：SQLite 的 BEGIN IMMEDIATE 会独占写锁，第二个连接在事务期间
根本写不进去（会拿 SQLITE_BUSY），所以用例在「作业快照读之后、回写之前」用**同一连接**
注入并发写入者，精确复现丢数据的数据形态（PG READ COMMITTED 下语句级新快照产生同一形态）。
断言的是修复要保证的性质：回写必须基于该行**最新副本**合并，绝不整块覆盖。
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_BOOTSTRAP_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(
    _BOOTSTRAP_TEMP_DIR.name, "bootstrap.sqlite3"
)
# 内联闸门与环境：本机 .env 把 DATABASE_TYPE 指向共享 PostgreSQL 主库，
# 本用例必须在隔离 sqlite 上跑（pytest 走 tests/conftest.py 已强制 sqlite；
# 这里再兜一层，并关掉关键词入库闸门，避免夹具文章被"无关键词命中"拦截）。
os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ["INTEL_LLM_ENABLED"] = "false"
os.environ["CRAWL_REQUIRE_KEYWORD_MATCH"] = "false"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import intel_topics  # noqa: E402
from intel_topics import IntelTopicService  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

PACK_ID = "test_pack"
TOPIC_KEY = "fund_raise"

# 入库闸门要求"去框架后有效正文"足够长（≥40 字），夹具正文按需补齐。
GATE_FILLER = (
    "本条正文为单元测试夹具生成的占位内容，用于满足入库闸门与分级字数标准要求的正文长度，"
    "不包含额外主题关键词，避免影响本用例对回写行为的判定。"
)


def _fixture_content() -> str:
    return "该母基金由省级引导基金出资设立，用于支持先进制造方向的子基金。" + GATE_FILLER

# 作业启动读到的旧副本：rule_signal=no_signal（规则没给信号）
SNAPSHOT_DETAILS = {
    "relevance_score": 3.0,
    "minimum_relevance_score": 2.0,
    "trend_score": 0,
    "event_score": 0,
    "components": {"core": 3, "expanded": 0, "trend": 0, "event": 0, "negative": 0},
    "hits": {"anchor": ["母基金"], "core": [], "expanded": [], "trend": [], "event": []},
    "rule_signal": "no_signal",
    "topic_tagging_version": "fixed-topic-tag-v1",
}


def _pack():
    return {
        "id": PACK_ID,
        "name": "测试行业包",
        "fixed_topics": [
            {
                "key": TOPIC_KEY,
                "name": "募资与基金",
                "keywords": ["母基金", "引导基金"],
            }
        ],
    }


class _FixedPackLoader:
    def __init__(self, pack):
        self._pack = pack

    def load(self, pack_id, **_kwargs):
        return copy.deepcopy(self._pack)


class TopicClusterConcurrentWriteTests(unittest.TestCase):
    def setUp(self):
        self._original_keyword_guard = config.CRAWL_REQUIRE_KEYWORD_MATCH
        config.CRAWL_REQUIRE_KEYWORD_MATCH = False
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "topics.sqlite3"))
        # 硬护栏：绝不能在共享 PostgreSQL 主库上跑这个用例
        self.assertEqual(
            self.db.backend, "sqlite", "本用例必须在隔离 sqlite 上跑（见 tests/conftest.py）"
        )
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        self.db.analyze_article_spacetime_profile = lambda _article_id: None
        self.service = IntelTopicService(
            database=self.db, pack_loader=_FixedPackLoader(_pack())
        )
        self.article_id = self.db.insert_article(
            {
                "url": "https://example.com/topic/concurrent",
                "title": "某省母基金完成设立，规模 30 亿元",
                "content": _fixture_content(),
                "publish_date": "2026-10-10",
                "matched_keywords": ["母基金"],
            }
        )
        self.assertIsNotNone(self.article_id, "夹具文章必须入库成功")
        self._seed_classification()

    def tearDown(self):
        self.db.disconnect()
        self.temp_dir.cleanup()
        config.CRAWL_REQUIRE_KEYWORD_MATCH = self._original_keyword_guard

    # ---------- 夹具 ----------

    def _seed_classification(self):
        self.db.connection.execute(
            """
            INSERT INTO article_intel_classifications (
                article_id, industry_pack_id, activation_id, industry_pack_version,
                classifier_version, article_content_hash, rule_category,
                rule_confidence, rule_reason, score_details_json,
                matched_keywords_json, final_category, final_confidence, final_reason,
                topic_tags_json, classified_at, updated_at
            ) VALUES (?, ?, '', '1.0.0', 'rule-v1', 'hash-1', 'event', 0.7, '事件判断',
                      ?, '[]', 'event', 0.7, '事件判断', '[]',
                      '2026-10-11T18:15:33Z', '2026-10-11T18:15:33Z')
            """,
            (
                int(self.article_id),
                PACK_ID,
                json.dumps(SNAPSHOT_DETAILS, ensure_ascii=False, sort_keys=True),
            ),
        )
        self.db.connection.commit()

    def _row(self):
        row = self.db.connection.execute(
            """
            SELECT final_category, rule_category, score_details_json,
                   topic_tags_json, updated_at
            FROM article_intel_classifications
            WHERE article_id=? AND industry_pack_id=?
            """,
            (int(self.article_id), PACK_ID),
        ).fetchone()
        return dict(row) if row else None

    def _details(self):
        row = self._row()
        return json.loads(row["score_details_json"]) if row else None

    def _concurrent_writer(self, cursor, marker="signal"):
        """模拟另一路写入者（classification 作业）在同一行写入新证据。

        与 intel_database.save_classification 的整块 upsert 等价：把 score_details 换成
        带新字段的最新副本，并刷 updated_at。
        """
        fresh = self._details() or dict(SNAPSHOT_DETAILS)
        fresh["rule_signal"] = marker
        fresh["hits"] = {"anchor": ["母基金"], "core": [], "expanded": [], "trend": [],
                         "event": ["完成设立"]}
        fresh["event_score"] = 2
        cursor.execute(
            """
            UPDATE article_intel_classifications
            SET score_details_json=?, updated_at=?
            WHERE article_id=? AND industry_pack_id=?
            """,
            (
                json.dumps(fresh, ensure_ascii=False, sort_keys=True),
                "2026-10-11T18:16:00Z",
                int(self.article_id),
                PACK_ID,
            ),
        )

    # ---------- 用例 ----------

    def test_concurrent_score_details_write_survives_writeback(self):
        """并发写入落在作业快照读之后 → 回写后对方字段仍在，且主题归属已加上。"""
        real = intel_topics._latest_classification_copy
        fired = {"n": 0}

        def hook(cursor, article_id, industry_pack_id):
            # 第一次重读前注入并发写入者：等价于"对方在作业读快照之后提交"
            if fired["n"] == 0:
                self._concurrent_writer(cursor)
            fired["n"] += 1
            return real(cursor, article_id, industry_pack_id)

        with patch("intel_topics._latest_classification_copy", side_effect=hook):
            stats = self.service.cluster(PACK_ID, manual=True)

        self.assertEqual(stats["write_conflicts"], 0)
        details = self._details()
        # 对方刚写入的证据必须还在（旧实现会被作业开头的陈旧副本抹成 no_signal）
        self.assertEqual(details["rule_signal"], "signal")
        self.assertEqual(details["hits"]["event"], ["完成设立"])
        self.assertEqual(details["event_score"], 2)
        # 本作业的主题归属也加上了
        self.assertEqual([item["key"] for item in details["topic_assignments"]], [TOPIC_KEY])
        self.assertEqual(details["topic_tagging_version"], intel_topics.TOPIC_TAGGING_VERSION)
        row = self._row()
        self.assertEqual(json.loads(row["topic_tags_json"]), ["募资与基金"])
        # 绝不触碰分类结论
        self.assertEqual(row["final_category"], "event")
        self.assertEqual(row["rule_category"], "event")

    def test_conflict_between_reread_and_update_is_retried(self):
        """并发写入落在"重读之后、UPDATE 之前" → 乐观并发校验拦下，重读重试后两者共存。"""
        real = intel_topics._latest_classification_copy
        fired = {"n": 0}

        def hook(cursor, article_id, industry_pack_id):
            row = real(cursor, article_id, industry_pack_id)
            if fired["n"] == 0:
                self._concurrent_writer(cursor, marker="signal")
            fired["n"] += 1
            return row

        with patch("intel_topics._latest_classification_copy", side_effect=hook):
            stats = self.service.cluster(PACK_ID, manual=True)

        self.assertGreaterEqual(fired["n"], 2, "校验失败必须重读重试")
        self.assertEqual(stats["write_conflicts"], 0)
        details = self._details()
        self.assertEqual(details["rule_signal"], "signal")
        self.assertEqual([item["key"] for item in details["topic_assignments"]], [TOPIC_KEY])

    def test_repeated_conflict_skips_write_instead_of_clobbering(self):
        """持续冲突 → 重试用尽后放弃本次 score_details 回写，绝不落陈旧副本。"""
        real = intel_topics._latest_classification_copy
        fired = {"n": 0}

        def hook(cursor, article_id, industry_pack_id):
            row = real(cursor, article_id, industry_pack_id)
            # 每次重读后都被改写：模拟"这一行一直在被人刷"
            self._concurrent_writer(cursor, marker="signal-%d" % fired["n"])
            fired["n"] += 1
            return row

        with patch("intel_topics._latest_classification_copy", side_effect=hook):
            stats = self.service.cluster(PACK_ID, manual=True)

        self.assertEqual(stats["write_conflicts"], 1)
        self.assertEqual(
            fired["n"], intel_topics._TOPIC_WRITE_CONFLICT_RETRIES,
            "重试次数必须受常量约束，不能无限重试",
        )
        details = self._details()
        # 对方最后一次写入原样保留，本作业一个字都没覆盖
        self.assertEqual(details["rule_signal"], "signal-2")
        self.assertNotIn("topic_assignments", details)

    def test_deleted_row_is_not_resurrected(self):
        """该行被并发删除（重跑/换激活）→ 不写回、不复活，计入 write_conflicts。"""
        real = intel_topics._latest_classification_copy
        fired = {"n": 0}

        def hook(cursor, article_id, industry_pack_id):
            if fired["n"] == 0:
                cursor.execute(
                    "DELETE FROM article_intel_classifications "
                    "WHERE article_id=? AND industry_pack_id=?",
                    (int(self.article_id), PACK_ID),
                )
            fired["n"] += 1
            return real(cursor, article_id, industry_pack_id)

        with patch("intel_topics._latest_classification_copy", side_effect=hook):
            stats = self.service.cluster(PACK_ID, manual=True)

        self.assertEqual(stats["write_conflicts"], 1)
        self.assertIsNone(self._row())
        # 主题关联仍然建立在 intel_topic_articles（与本回写无关，不受影响）
        count = self.db.connection.execute(
            "SELECT COUNT(*) FROM intel_topic_articles WHERE article_id=?",
            (int(self.article_id),),
        ).fetchone()[0]
        self.assertGreaterEqual(int(count), 1)

    def test_updated_at_uses_write_time_not_job_start(self):
        """updated_at 必须是回写当下的时间，不是作业启动时缓存的 now。"""
        import intel_contracts

        real_utc_text = intel_contracts.utc_text
        seen = {"n": 0}

        def fake_utc_text(value=None):
            if value is not None:
                return real_utc_text(value)
            seen["n"] += 1
            # 第 1 次 = cluster() 开头缓存 now；之后 = 回写当下的时间
            return "2026-10-11T18:14:38Z" if seen["n"] == 1 else "2026-10-11T18:19:25Z"

        with patch("intel_topics.utc_text", side_effect=fake_utc_text):
            stats = self.service.cluster(PACK_ID, manual=True)

        self.assertEqual(stats["write_conflicts"], 0)
        self.assertGreaterEqual(seen["n"], 2, "回写必须重新取当前时间，不能复用作业启动的 now")
        self.assertEqual(self._row()["updated_at"], "2026-10-11T18:19:25Z")

    def test_legacy_blob_writeback_would_lose_the_concurrent_write(self):
        """负向对照：旧实现（整块回写作业快照）在同一夹具下确实会丢数据。

        这条用例固定"夹具能测出丢数据"这件事：如果它开始失败，说明上面的断言
        已经无法区分新旧实现，回归用例就失去意义了。
        """
        snapshot = self._details()
        cursor = self.db.connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        self._concurrent_writer(cursor)
        # 旧实现：把作业开头读到的快照整块写回去
        stale = dict(snapshot)
        stale["topic_tagging_version"] = intel_topics.TOPIC_TAGGING_VERSION
        stale["topic_assignments"] = []
        cursor.execute(
            """
            UPDATE article_intel_classifications
            SET topic_tags_json=?, score_details_json=?, updated_at=?
            WHERE article_id=? AND industry_pack_id=?
            """,
            (
                "[]",
                json.dumps(stale, ensure_ascii=False, sort_keys=True),
                "2026-10-11T18:14:38Z",
                int(self.article_id),
                PACK_ID,
            ),
        )
        self.db.connection.commit()
        cursor.close()
        self.assertEqual(self._details()["rule_signal"], "no_signal")
        self.assertEqual(self._row()["updated_at"], "2026-10-11T18:14:38Z")


if __name__ == "__main__":
    unittest.main()
