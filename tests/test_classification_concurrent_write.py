#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""upsert_classification 反向缺口（分类覆盖主题归属）的回归用例。

背景（与 tests/test_topic_cluster_concurrent_write.py 互为反向）：
`IntelRepository.upsert_classification` 原来是「作业开头把结果算好 → INSERT … ON CONFLICT
DO UPDATE 整块替换」的读-改-写。生产库是 PostgreSQL READ COMMITTED（postgres_compat 把
SQLite 的 BEGIN IMMEDIATE 翻译成 BEGIN，连接又是 autocommit），同一事务里每条语句各取一次
新快照，事务保护不了这份读-改-写：分类作业会用「本作业算出的 score_details」整块覆盖
主题聚类作业在作业窗口内刚写入的 `topic_assignments`（不会丢 rule_signal —— 那个方向已被
intel_topics._write_topic_assignments 的修复挡住）。

修复后必须保证的性质：
  1) 回写基于该行**最新副本**合并，只改本作业拥有的键；
  2) 主题聚类作业拥有的两个键（topic_assignments / topic_tagging_version）原样保留；
  3) 带乐观并发校验，重读后被改写则重试（次数受常量约束）；
  4) updated_at 是回写当下的时间，不是作业启动时缓存的 now。

隔离 sqlite 的并发注入说明：本用例把「并发写入者」直接落在 `upsert_classification` 内部
「重读之后、UPDATE 之前」的窗口里（以 intel_database.utc_text 的调用时机作钩子），
精确复现 PG READ COMMITTED 下语句级新快照产生的丢数据形态。
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
os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ["INTEL_LLM_ENABLED"] = "false"
os.environ["CRAWL_REQUIRE_KEYWORD_MATCH"] = "false"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import intel_database  # noqa: E402
from intel_database import (  # noqa: E402
    _CLASSIFICATION_WRITE_CONFLICT_RETRIES,
    IntelRepository,
)
from sqlite_database import SQLiteDatabase  # noqa: E402

PACK_ID = "test_pack"

GATE_FILLER = (
    "本条正文为单元测试夹具生成的占位内容，用于满足入库闸门与分级字数标准要求的正文长度，"
    "不包含额外主题关键词，避免影响本用例对回写行为的判定。"
)

# 作业启动时算出的分类结果快照（分类作业自己的 topic_assignments 是空 —— 这篇文章当时
# 没通过 _industry_gate_passed；真正的归属由更晚运行的主题聚类作业写入）。
CLASSIFIER_DETAILS = {
    "relevance_score": 6.0,
    "minimum_relevance_score": 2.0,
    "trend_score": 0.0,
    "event_score": 2.0,
    "components": {"core": 3, "expanded": 0, "trend": 0, "event": 2, "negative": 0},
    "hits": {
        "anchor": ["母基金"],
        "core": ["引导基金"],
        "expanded": [],
        "trend": [],
        "event": ["完成设立"],
    },
    "rule_signal": "signal",
    "topic_assignments": [],
}

# 主题聚类作业在作业窗口内写入的归属（分类作业必须原样保留它）。
TOPIC_ASSIGNMENTS = [
    {
        "key": "fund_raise",
        "name": "募资与基金",
        "matched_keywords": ["母基金"],
        "evidence": [{"hits": ["母基金"]}],
        "score": 3.0,
        "assignment_method": "rule",
    }
]

# 该行在被分类作业覆盖前的既有状态（上一轮分类留下的 no_signal）。
SEED_DETAILS = {
    "relevance_score": 3.0,
    "minimum_relevance_score": 2.0,
    "trend_score": 0,
    "event_score": 0,
    "components": {"core": 3, "expanded": 0, "trend": 0, "event": 0, "negative": 0},
    "hits": {"anchor": ["母基金"], "core": [], "expanded": [], "trend": [], "event": []},
    "rule_signal": "no_signal",
}

SEED_UPDATED_AT = "2026-10-11T18:15:33Z"
WRITE_TIME = "2026-10-12T09:31:07Z"
JOB_START_TIME = "2026-10-11T18:14:38Z"


def _fixture_content() -> str:
    return "该母基金由省级引导基金出资设立，用于支持先进制造方向的子基金。" + GATE_FILLER


def _classification_result(article_id: int) -> dict:
    """分类作业产出的结果字典（等价 intel_classifier.classify_article_id 的返回）。"""
    return {
        "article_id": int(article_id),
        "industry_pack_id": PACK_ID,
        "activation_id": "",
        "industry_pack_version": "1.1.0",
        "classifier_version": "rule-v2",
        "article_content_hash": "hash-2",
        "rule_category": "event",
        "rule_confidence": 0.82,
        "rule_reason": "行业相关性达到阈值，并命中事件信号",
        "score_details": copy.deepcopy(CLASSIFIER_DETAILS),
        "matched_keywords": ["母基金", "引导基金", "完成设立"],
        "topic_tags": [],
        "final_category": "event",
        "final_confidence": 0.82,
        "final_reason": "行业相关性达到阈值，并命中事件信号",
        "result_source": "rule",
    }


class ClassificationConcurrentWriteTests(unittest.TestCase):
    def setUp(self):
        self._original_keyword_guard = config.CRAWL_REQUIRE_KEYWORD_MATCH
        config.CRAWL_REQUIRE_KEYWORD_MATCH = False
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "classification.sqlite3"))
        # 硬护栏：绝不能在共享 PostgreSQL 主库上跑这个用例
        self.assertEqual(
            self.db.backend, "sqlite", "本用例必须在隔离 sqlite 上跑（见 tests/conftest.py）"
        )
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        self.db.analyze_article_spacetime_profile = lambda _article_id: None
        self.repo = IntelRepository(database=self.db)
        self.repo._ensure()  # 预热：把建表补列的一次性开销挪出注入窗口
        self.article_id = self.db.insert_article(
            {
                "url": "https://example.com/classification/concurrent",
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
            ) VALUES (?, ?, '', '1.0.0', 'rule-v1', 'hash-1', 'other', 0.3, '兜底归属',
                      ?, '[]', 'other', 0.3, '兜底归属', '[]', ?, ?)
            """,
            (
                int(self.article_id),
                PACK_ID,
                json.dumps(SEED_DETAILS, ensure_ascii=False, sort_keys=True),
                SEED_UPDATED_AT,
                SEED_UPDATED_AT,
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

    def _topic_writer(self, marker: str = "topic-write"):
        """模拟主题聚类作业写入归属（等价 intel_topics._write_topic_assignments 的产物）。"""
        assignments = copy.deepcopy(TOPIC_ASSIGNMENTS)
        assignments[0]["evidence"] = [{"hits": ["母基金"], "marker": marker}]
        fresh = self._details() or dict(SEED_DETAILS)
        fresh["topic_assignments"] = assignments
        fresh["topic_tagging_version"] = "fixed-topic-tag-v1"
        self.db.connection.execute(
            """
            UPDATE article_intel_classifications
            SET score_details_json=?, topic_tags_json=?, updated_at=?
            WHERE article_id=? AND industry_pack_id=?
            """,
            (
                json.dumps(fresh, ensure_ascii=False, sort_keys=True),
                json.dumps([item["name"] for item in assignments], ensure_ascii=False),
                "2026-10-11T18:16:00Z",
                int(self.article_id),
                PACK_ID,
            ),
        )
        self.db.connection.commit()

    def _write_with_hook(self, result, hook):
        """在 utc_text 的第 2 次调用（= 重读之后、写入之前）注入并发写入者。

        upsert_classification 的 utc_text 调用次序：
          第 1 次 = 方法入口缓存 now（classified_at）；
          第 2 次起 = 每一轮「重读之后、写入之前」取回写当下时间。
        因此 hook 在 write_time 取时间的那一刻落库，正好是乐观并发校验要拦下的窗口。
        """
        real_utc_text = intel_database.utc_text
        state = {"n": 0}

        def fake_utc_text(value=None):
            if value is not None:
                return real_utc_text(value)
            state["n"] += 1
            hook(state["n"])
            return WRITE_TIME

        with patch("intel_database.utc_text", side_effect=fake_utc_text):
            row_id = self.repo.upsert_classification(result)
        return row_id, state["n"]

    # ---------- 用例 ----------

    def test_topic_assignments_written_after_job_start_survive(self):
        """并发写入落在「作业算完结果、写库之前」→ 归属不丢，分类字段正常更新。"""
        result = _classification_result(self.article_id)  # 分类作业先把结果算完
        self._topic_writer()  # 聚类作业在它写库之前抢先进去
        row_id = self.repo.upsert_classification(result)

        self.assertIsNotNone(row_id)
        details = self._details()
        # 对方刚写入的归属必须还在（旧实现会被分类结果整块抹成 []）
        self.assertEqual(
            [item["key"] for item in details["topic_assignments"]], ["fund_raise"]
        )
        self.assertEqual(details["topic_tagging_version"], "fixed-topic-tag-v1")
        # 分类作业自己的字段正常更新
        self.assertEqual(details["rule_signal"], "signal")
        self.assertEqual(details["event_score"], 2.0)
        self.assertEqual(details["hits"]["event"], ["完成设立"])
        self.assertEqual(details["hits"]["core"], ["引导基金"])
        self.assertEqual(details["relevance_score"], 6.0)
        self.assertEqual(details["components"]["event"], 2)
        row = self._row()
        self.assertEqual(row["final_category"], "event")
        self.assertEqual(row["rule_category"], "event")
        self.assertNotEqual(row["updated_at"], SEED_UPDATED_AT, "updated_at 必须刷新")
        # topic_tags_json 必须与 details 里保留的归属成对，不能自相矛盾
        self.assertEqual(json.loads(row["topic_tags_json"]), ["募资与基金"])

    def test_conflict_between_reread_and_update_is_retried(self):
        """并发写入落在「重读之后、UPDATE 之前」→ 乐观并发校验拦下，重读重试后两者共存。"""
        fired = {"n": 0, "at": None}

        def hook(call_no):
            # call_no >= 2 = write_time，即每轮 UPDATE 之前
            if call_no >= 2 and fired["n"] == 0:
                self._topic_writer(marker="late")
                fired["n"] += 1
                fired["at"] = call_no

        _, calls = self._write_with_hook(_classification_result(self.article_id), hook)

        self.assertEqual(fired["n"], 1, "夹具必须真的注入了一次并发写入")
        self.assertGreaterEqual(calls, fired["at"] + 1, "校验失败必须重读重试")
        details = self._details()
        self.assertEqual(
            [item["key"] for item in details["topic_assignments"]], ["fund_raise"]
        )
        self.assertEqual(
            details["topic_assignments"][0]["evidence"][0]["marker"], "late"
        )
        self.assertEqual(details["rule_signal"], "signal")
        self.assertEqual(self._row()["updated_at"], WRITE_TIME)

    def test_repeated_conflict_skips_write_instead_of_clobbering(self):
        """持续冲突 → 重试用尽后放弃本次回写，绝不落陈旧副本。"""
        fired = {"n": 0}

        def hook(call_no):
            if call_no >= 2:
                self._topic_writer(marker="late-%d" % call_no)
            fired["n"] += 1

        row_id, calls = self._write_with_hook(_classification_result(self.article_id), hook)

        self.assertIsNotNone(row_id, "放弃回写时仍要返回该行 id，不能抛异常打断作业")
        self.assertEqual(
            calls,
            1 + _CLASSIFICATION_WRITE_CONFLICT_RETRIES,
            "重试次数必须受常量约束，不能无限重试",
        )
        details = self._details()
        # 对方最后一次写入原样保留；分类作业一个字都没覆盖（rule_signal 仍是种子值）
        self.assertEqual(details["rule_signal"], "no_signal")
        self.assertEqual(
            [item["key"] for item in details["topic_assignments"]], ["fund_raise"]
        )
        self.assertEqual(self._row()["updated_at"], "2026-10-11T18:16:00Z")

    def test_updated_at_uses_write_time_not_job_start(self):
        """updated_at 必须是回写当下的时间，不是作业启动时缓存的 now。"""
        seen = {"n": 0}

        def hook(call_no):
            seen["n"] = call_no

        _, calls = self._write_with_hook(_classification_result(self.article_id), hook)

        self.assertGreaterEqual(calls, 2, "回写必须重新取当前时间，不能复用作业启动的 now")
        self.assertEqual(self._row()["updated_at"], WRITE_TIME)

    def test_legacy_blob_upsert_would_lose_the_concurrent_topic_write(self):
        """负向对照（变异验证）：旧的整块 upsert 在同一夹具下确实会把归属抹掉。

        这条用例固定「夹具能测出丢数据」这件事：如果它开始失败，说明上面的断言
        已经无法区分新旧实现，回归用例就失去意义了。
        """
        result = _classification_result(self.article_id)
        self._topic_writer()
        # 旧实现：INSERT … ON CONFLICT DO UPDATE，score_details_json = excluded.score_details_json
        self.db.connection.execute(
            """
            INSERT INTO article_intel_classifications (
                article_id, industry_pack_id, activation_id, industry_pack_version,
                classifier_version, article_content_hash,
                rule_category, rule_confidence, rule_reason,
                score_details_json, matched_keywords_json,
                final_category, final_confidence, final_reason,
                result_source, classified_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(article_id, industry_pack_id) DO UPDATE SET
                score_details_json = excluded.score_details_json,
                topic_tags_json = excluded.topic_tags_json,
                final_category = excluded.final_category,
                updated_at = excluded.updated_at
            """,
            (
                int(self.article_id),
                PACK_ID,
                result["activation_id"],
                result["industry_pack_version"],
                result["classifier_version"],
                result["article_content_hash"],
                result["rule_category"],
                result["rule_confidence"],
                result["rule_reason"],
                json.dumps(result["score_details"], ensure_ascii=False, sort_keys=True),
                "[]",
                result["final_category"],
                result["final_confidence"],
                result["final_reason"],
                result["result_source"],
                JOB_START_TIME,
                JOB_START_TIME,
            ),
        )
        self.db.connection.commit()
        details = self._details()
        self.assertEqual(details["topic_assignments"], [], "旧实现会把对方的归属整块抹掉")
        self.assertNotIn("topic_tagging_version", details)
        self.assertEqual(self._row()["updated_at"], JOB_START_TIME)

    def test_first_write_keeps_classifier_topic_assignments(self):
        """该行此前不存在 → 分类作业自己的归属照常落库，不会被"保护对方"误伤成空。"""
        self.db.connection.execute(
            "DELETE FROM article_intel_classifications WHERE article_id=? AND industry_pack_id=?",
            (int(self.article_id), PACK_ID),
        )
        self.db.connection.commit()
        result = _classification_result(self.article_id)
        result["score_details"]["topic_assignments"] = copy.deepcopy(TOPIC_ASSIGNMENTS)
        result["score_details"]["topic_tagging_version"] = "fixed-topic-tag-v1"
        result["topic_tags"] = ["募资与基金"]

        self.repo.upsert_classification(result)

        details = self._details()
        self.assertEqual(
            [item["key"] for item in details["topic_assignments"]], ["fund_raise"]
        )
        self.assertEqual(self._row()["topic_tags_json"], '["募资与基金"]')

    def test_reclassification_drops_stale_classifier_only_keys(self):
        """分类作业拥有「除归属之外的全部 score_details 键」：旧键必须被新的结果替换掉。"""
        self.db.connection.execute(
            """
            UPDATE article_intel_classifications
            SET score_details_json=?
            WHERE article_id=? AND industry_pack_id=?
            """,
            (
                json.dumps(
                    {**SEED_DETAILS, "admission_source": "llm_semantic"},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                int(self.article_id),
                PACK_ID,
            ),
        )
        self.db.connection.commit()

        self.repo.upsert_classification(_classification_result(self.article_id))

        details = self._details()
        self.assertNotIn(
            "admission_source",
            details,
            "上一轮分类留下的 admission_source 必须被本轮结果清掉，不能残留成假准入",
        )
        self.assertEqual(details["rule_signal"], "signal")


if __name__ == "__main__":
    unittest.main()
