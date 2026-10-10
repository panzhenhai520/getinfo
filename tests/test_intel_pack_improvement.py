#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""intel_pack_improvement 单元测试。

隔离性：每个用例自建临时 sqlite（``SQLiteDatabase(temp)``），把模块的数据库/行业包加载器
都换成测试夹具，**不连真库、不联网、不调模型、不碰 GPU**；抓文章探针一律打桩。
"""

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

# 必须在 import config 之前把主库切到临时 SQLite（与 tests/conftest.py 同款做法，但这里也写一遍：
# 本机 .env 是 DATABASE_TYPE=postgres，若直接 python tests/xxx.py 跑（没有 conftest），
# 用例会连**共享 PostgreSQL 主库**并真的写进去。SQLITE_BACKUP_PATH 的优先级高于 DATABASE_PATH，
# 两个键都要指向临时库才算真正隔离。
_BOOTSTRAP_DIR = tempfile.mkdtemp(prefix="intel-improvement-tests-")
_BOOTSTRAP_DB = os.path.join(_BOOTSTRAP_DIR, "bootstrap.sqlite3")
os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["DATABASE_PATH"] = _BOOTSTRAP_DB
os.environ["SQLITE_BACKUP_PATH"] = _BOOTSTRAP_DB
os.environ["INTEL_LLM_ENABLED"] = "false"

import intel_pack_improvement as m
from intel_database import IntelRepository
from sqlite_database import SQLiteDatabase

ORIGINAL_LOADER = m._pack_loader
ORIGINAL_REPOSITORY = m._repository
SEED_PACK_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "industry_packs",
    "family_office.json",
)


def _date(days_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).date().isoformat()


def _classification_tail(**overrides) -> dict:
    payload = {
        "pack_version": "1.0.0",
        "classifier_version": "rule-v1",
        "content_hash": "hash",
        "rule_category": "other",
        "final_category": "other",
        "rule_reason": "核心相关性低于行业包阈值",
        "score_details": {},
        "matched_keywords": [],
    }
    payload.update(overrides)
    return payload


def _demo_pack() -> dict:
    """演示包：有 candidate_gate.anchor_keywords（走"锚点词表"分支）。"""
    return {
        "id": "demo_pack",
        "name": "演示包",
        "schema_version": 1,
        "pack_version": "1.0.0",
        "enabled": True,
        "default_market": "CN",
        "timezone": "Asia/Hong_Kong",
        "core_keywords": ["演示核心词"],
        "expanded_keywords": [],
        "trend_keywords": ["演示趋势词"],
        "event_keywords": ["演示事件词"],
        "negative_keywords": ["演示负向词"],
        "classification": {
            "core_weight": 3,
            "expanded_weight": 1,
            "trend_weight": 2,
            "event_weight": 2,
            "negative_weight": -3,
            "minimum_relevance_score": 2,
            "llm_confidence_threshold": 0.65,
            "tie_break_order": ["trend", "event", "other"],
            "recent_today_window_days": 5,
            "recent_trend_window_days": 21,
        },
        "serpapi_queries": [],
        "default_sources": [],
        "fixed_topics": [
            {"key": "pilot", "name": "中试线", "keywords": ["中试线"]},
        ],
        "candidate_gate": {"anchor_keywords": ["演示锚点词"], "entity_keywords": []},
    }


def _gate_free_pack() -> dict:
    """无 candidate_gate 锚点的包：门禁回退 core_keywords + expanded_keywords。"""
    pack = _demo_pack()
    pack["id"] = "gate_free_pack"
    pack["name"] = "无门禁锚点包"
    pack["core_keywords"] = ["演示核心词"]
    pack.pop("candidate_gate", None)
    return pack


def _peer_pack() -> dict:
    pack = _demo_pack()
    pack["id"] = "peer_pack"
    pack["name"] = "对照包"
    pack["candidate_gate"] = {"anchor_keywords": ["对照锚点词"], "entity_keywords": []}
    pack["fixed_topics"] = []
    return pack


class _FakeLoader:
    """行业包加载器替身：返回深拷贝（生产配置一个字节都不会被改到）。

    另外补齐 admin 服务需要的 ``has_seed_pack`` / ``clear_cache``，
    这样 ``prepare_pack_version`` 能走真实的"草稿→保存→发布"链路（写临时库）。

    关键保真点：给了 ``database`` 时，``load()`` **优先读已发布存储**（与生产
    ``IndustryPackLoader`` 的行为一致）——否则"发布后候选词是否真的进了生效配置"
    这条最关键的断言就测不出来。
    """

    def __init__(self, packs, database=None):
        self.packs = copy.deepcopy(packs)
        self.database = database

    def _published_manifest(self, pack_id):
        if self.database is None:
            return None
        try:
            from industry_pack_admin import IndustryPackVersionStore

            record = IndustryPackVersionStore(self.database).latest_published(pack_id)
        except Exception:
            return None
        if not record:
            return None
        return copy.deepcopy(record.get("manifest") or {})

    def load(self, pack_id, *, enabled_only=True, use_published=True, **_kwargs):
        key = str(pack_id or "")
        if key not in self.packs:
            raise ValueError(f"industry pack not found: {key}")
        if use_published:
            published = self._published_manifest(key)
            if published:
                return published
        return copy.deepcopy(self.packs[key])

    def effective_pack_set(self, pack_id, **_kwargs):
        return [self.load(pack_id)]

    def has_seed_pack(self, pack_id) -> bool:
        return str(pack_id or "") in self.packs

    def clear_cache(self, pack_id: str = "") -> None:
        return None

    def snapshot(self) -> str:
        return json.dumps(self.packs, sort_keys=True, ensure_ascii=False)


# 演示语料：P1~P3 含候选词"固态电池"（3 篇 ≥ 判别力文档数门槛）与"并购"（对照语料里也有）；
# P4 刻意与它们几乎不共享任何实词，改后必须仍然是 other。
_DEMO_ARTICLES = [
    {
        "title": "固态电池中试线投产",
        "content": (
            "固态电池中试线在东部园区投产，首批样品已交付两家整车客户，量产节拍稳步爬坡，"
            "并带动上下游并购热度。演示趋势词同步观察。2026年该产线计划扩产。"
        ),
        "category": "other",
    },
    {
        "title": "固态电池获海外批量订单",
        "content": (
            "该企业拿下海外批量订单，履约周期覆盖明年全年，产线良率较上季度提升明显，"
            "并购团队亦在接洽。演示趋势词相关进展持续跟踪。2026年交付节奏不变。"
        ),
        "category": "other",
    },
    {
        "title": "固态电池技术评审通过",
        "content": (
            "行业协会组织专家评审，认为该路线工程化可行，建议加快落地验证节奏，"
            "并购与合资安排同步推进。演示趋势词维持一致口径。2026年完成评审收口。"
        ),
        "category": "other",
    },
    {
        "title": "产线良率提升明显",
        "content": "车间良率提升源于刀具更换流程优化，与前述材料路线并无关联。",
        "category": "other",
    },
]

_PEER_ARTICLES = [
    {
        "title": "精细化工资产并购完成交割",
        "content": "并购标的为一家精细化工企业，交割流程已全部走完。",
        "category": "trend",
    },
    {
        "title": "并购基金完成新一轮募集",
        "content": "并购基金募集规模超出预期，投资人结构保持稳定。",
        "category": "trend",
    },
    {
        "title": "并购重组审核口径更新",
        "content": "并购重组审核口径有所更新，申报材料要求同步细化。",
        "category": "trend",
    },
]


class IntelPackImprovementTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "improvement.sqlite3"))
        self.assertTrue(self.db.connect())
        # 隔离哨兵：万一环境把它指向了 PostgreSQL 主库，立刻失败而不是往真库写测试数据
        self.assertEqual(
            getattr(self.db, "backend", "sqlite"),
            "sqlite",
            "测试必须跑在隔离的临时 SQLite 上（检查 DATABASE_TYPE/SQLITE_BACKUP_PATH）",
        )
        self.assertTrue(self.db.create_tables())
        self.repo = IntelRepository(self.db)
        m.use_database(self.db)
        self.loader = _FakeLoader(
            {
                "demo_pack": _demo_pack(),
                "gate_free_pack": _gate_free_pack(),
                "peer_pack": _peer_pack(),
            },
            database=self.db,
        )
        m.use_pack_loader(self.loader)

    def tearDown(self):
        m.use_pack_loader(ORIGINAL_LOADER)
        m.use_database(ORIGINAL_REPOSITORY)
        self.db.disconnect()
        self.temp_dir.cleanup()

    # ── 夹具 ──
    def _insert_article(self, title, content, url, publish_date=None):
        cursor = self.db.connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO articles (url, title, content, domain, publish_date, status,
                                      matched_keywords, first_crawled, created_at)
                VALUES (?, ?, ?, 'example.com', ?, 'active', '', ?, ?)
                """,
                (
                    url,
                    title,
                    content,
                    publish_date or _date(0),
                    _date(0) + " 00:00:00",
                    _date(0) + " 00:00:00",
                ),
            )
            return int(cursor.lastrowid)
        finally:
            cursor.close()

    def _insert_classification(self, article_id, pack_id, **overrides):
        payload = _classification_tail(**overrides)
        details = payload["score_details"]
        cursor = self.db.connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO article_intel_classifications
                    (article_id, industry_pack_id, industry_pack_version, classifier_version,
                     article_content_hash, rule_category, final_category, rule_reason,
                     final_reason, score_details_json, matched_keywords_json, classified_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    article_id,
                    pack_id,
                    payload["pack_version"],
                    payload["classifier_version"],
                    payload["content_hash"],
                    payload["rule_category"],
                    payload["final_category"],
                    payload["rule_reason"],
                    payload["rule_reason"],
                    json.dumps(details, ensure_ascii=False),
                    json.dumps(payload["matched_keywords"], ensure_ascii=False),
                    _date(0) + "T00:00:00Z",
                ),
            )
        finally:
            cursor.close()

    def _seed_demo_scenario(self):
        ids = {}
        for index, item in enumerate(_DEMO_ARTICLES, start=1):
            article_id = self._insert_article(
                item["title"],
                item["content"],
                f"https://demo.example.com/{index}",
                publish_date=_date(index),
            )
            self._insert_classification(
                article_id, "demo_pack", final_category=item["category"]
            )
            ids[item["title"]] = article_id
        peer_ids = []
        for index, item in enumerate(_PEER_ARTICLES, start=1):
            article_id = self._insert_article(
                item["title"],
                item["content"],
                f"https://peer.example.com/{index}",
                publish_date=_date(index),
            )
            self._insert_classification(
                article_id, "peer_pack", final_category=item["category"]
            )
            peer_ids.append(article_id)
        return ids, peer_ids

    def _insert_source(self, pack_id, name, url, metadata=None, authority=3):
        cursor = self.db.connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO intel_sources
                    (canonical_source_url, source_url, source_name, source_type,
                     authority_level, metadata_json)
                VALUES (?, ?, ?, 'rss', ?, ?)
                """,
                (url, url, name, authority, json.dumps(metadata or {}, ensure_ascii=False)),
            )
            source_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO intel_source_industries
                    (source_id, industry_pack_id, is_active, ownership_type)
                VALUES (?, ?, 1, 'pack_owned')
                """,
                (source_id, pack_id),
            )
            return source_id
        finally:
            cursor.close()

    def _link_article_to_source(self, article_id, source_id):
        cursor = self.db.connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO intel_candidates (canonical_url, original_url, title, article_id)
                VALUES (?, ?, '', ?)
                """,
                (f"https://linked.example.com/{article_id}", f"https://linked.example.com/{article_id}", article_id),
            )
            candidate_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO intel_candidate_observations
                    (candidate_id, source_id, observation_type, observation_key, raw_url)
                VALUES (?, ?, 'rss', ?, ?)
                """,
                (
                    candidate_id,
                    source_id,
                    f"obs-{candidate_id}",
                    f"https://linked.example.com/{article_id}",
                ),
            )
        finally:
            cursor.close()

    # ── 表结构 ──
    def test_schema_table_exists_and_only_adds_new_objects(self):
        tables = {
            row[0]
            for row in self.db.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("intel_pack_improvements", tables)
        indexes = {
            row[0]
            for row in self.db.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        self.assertIn("idx_intel_pack_improvements_pack", indexes)
        self.assertIn("idx_intel_pack_improvements_status", indexes)
        columns = [
            row[1]
            for row in self.db.connection.execute(
                "PRAGMA table_info(intel_pack_improvements)"
            )
        ]
        for expected in (
            "id",
            "industry_pack_id",
            "kind",
            "payload_json",
            "metrics_json",
            "evidence_json",
            "status",
            "reason",
            "created_at",
            "applied_at",
            "activation_id",
            "after_apply_json",
        ):
            self.assertIn(expected, columns)
        # 可回滚：只新增表，DROP 掉即可（不影响既有表）
        self.db.connection.execute("DROP TABLE intel_pack_improvements")
        self.db.connection.execute(
            "SELECT COUNT(*) FROM article_intel_classifications"
        )

    # ── 评估 ──
    def test_assess_pack_reports_other_pct_gate_failures_and_peer_median(self):
        reason_map = {
            "industry_filter": (
                "通用行业过滤器：未命中行业核心词或行业包实体，不予准入",
                {"admitted": False, "hits": {}},
            ),
            "negative_keyword": (
                "核心相关性低于行业包阈值",
                {
                    "hits": {"anchor": ["演示锚点词"], "negative": ["演示负向词"]},
                    "relevance_score": -3.0,
                    "minimum_relevance_score": 2.0,
                    "rule_signal": "below_threshold",
                },
            ),
            "no_anchor": (
                "核心相关性低于行业包阈值",
                {"hits": {"anchor": []}, "relevance_score": 0.0,
                 "minimum_relevance_score": 2.0, "rule_signal": "below_threshold"},
            ),
            "below_min_score": (
                "核心相关性低于行业包阈值",
                {"hits": {"anchor": ["演示锚点词"]}, "relevance_score": 1.0,
                 "minimum_relevance_score": 2.0, "rule_signal": "below_threshold"},
            ),
            "no_signal": (
                "文章与行业相关，但未命中明确趋势或事件信号",
                {"hits": {"anchor": ["演示锚点词"]}, "relevance_score": 3.0,
                 "minimum_relevance_score": 2.0, "rule_signal": "no_signal"},
            ),
        }
        for index, (key, (reason, details)) in enumerate(reason_map.items(), start=1):
            article_id = self._insert_article(
                f"其余文章{index}",
                f"该段文字用于验证失败归类{index}，不含任何行业词。",
                f"https://bucket.example.com/{index}",
            )
            self._insert_classification(
                article_id,
                "demo_pack",
                final_category="other",
                rule_reason=reason,
                score_details=details,
            )
        trend_id = self._insert_article(
            "演示趋势词相关综述", "本条命中演示趋势词与演示锚点词。", "https://bucket.example.com/t"
        )
        self._insert_classification(trend_id, "demo_pack", final_category="trend")
        event_id = self._insert_article(
            "演示事件词相关通报", "本条命中演示事件词与演示锚点词。", "https://bucket.example.com/e"
        )
        self._insert_classification(event_id, "demo_pack", final_category="event")

        # 对照包：demo_pack 之外的两个包，other 占比 50% 与 25% → 中位数 37.5%
        for index in range(4):
            article_id = self._insert_article(
                f"对照文章{index}", f"对照正文{index}", f"https://peer.example.com/a{index}"
            )
            self._insert_classification(
                article_id,
                "peer_pack",
                final_category="other" if index < 2 else "trend",
            )
        for index in range(4):
            article_id = self._insert_article(
                f"第三包文章{index}", f"第三包正文{index}", f"https://third.example.com/a{index}"
            )
            self._insert_classification(
                article_id,
                "gate_free_pack",
                final_category="other" if index < 1 else "trend",
            )

        result = m.assess_pack("demo_pack", sample_limit=100)

        self.assertEqual(result["pack_id"], "demo_pack")
        self.assertEqual(result["articles"], 7)
        self.assertEqual(result["categories"], {"trend": 1, "event": 1, "other": 5})
        self.assertAlmostEqual(result["other_pct"], round(100.0 * 5 / 7, 4))
        self.assertEqual(result["peer_pack_count"], 2)
        self.assertAlmostEqual(result["peer_other_pct"], 37.5)
        counts = result["gate_failures"]["counts"]
        self.assertEqual(counts["industry_filter"], 1)
        self.assertEqual(counts["negative_keyword"], 1)
        self.assertEqual(counts["no_anchor"], 1)
        self.assertEqual(counts["below_min_score"], 1)
        self.assertEqual(counts["no_signal"], 1)
        self.assertEqual(counts["unknown"], 0)
        self.assertEqual(result["gate_failures"]["total"], 5)
        self.assertEqual(len(result["sample"]["other_article_ids"]), 5)
        # 候选词里不该出现包内已有词
        self.assertNotIn("演示核心词", result["candidate_keywords"])
        self.assertNotIn("演示趋势词", result["candidate_keywords"])

    def test_assess_pack_verdicts_cover_all_four_source_states(self):
        low_source = self._insert_source("demo_pack", "低准入信源", "https://low.example.com/rss")
        zero_source = self._insert_source("demo_pack", "零产出信源", "https://zero.example.com/rss")
        idle_source = self._insert_source("demo_pack", "停更信源", "https://idle.example.com/rss")
        good_source = self._insert_source("demo_pack", "有效信源", "https://good.example.com/rss")

        for index in range(2):
            article_id = self._insert_article(
                f"低准入文章{index}", f"低准入正文{index}", f"https://low.example.com/a{index}"
            )
            self._insert_classification(article_id, "demo_pack", final_category="other")
            self._link_article_to_source(article_id, low_source)

        idle_id = self._insert_article(
            "停更文章", "停更正文", "https://idle.example.com/a1", publish_date=_date(90)
        )
        self._insert_classification(idle_id, "demo_pack", final_category="trend")
        self._link_article_to_source(idle_id, idle_source)

        good_ids = []
        for index in range(2):
            article_id = self._insert_article(
                f"有效文章{index}", f"有效正文{index}", f"https://good.example.com/a{index}"
            )
            self._insert_classification(
                article_id, "demo_pack", final_category="trend" if index == 0 else "other"
            )
            self._link_article_to_source(article_id, good_source)
            good_ids.append(article_id)

        result = m.assess_pack("demo_pack", sample_limit=100)
        by_id = {row["source_id"]: row for row in result["sources"]}

        self.assertEqual(by_id[low_source]["verdict"], "准入率低")
        self.assertEqual(by_id[low_source]["article_count"], 2)
        self.assertEqual(by_id[low_source]["admitted_count"], 0)
        self.assertEqual(by_id[low_source]["admitted_pct"], 0.0)
        self.assertEqual(by_id[zero_source]["verdict"], "零产出")
        self.assertEqual(by_id[zero_source]["article_count"], 0)
        self.assertEqual(by_id[idle_source]["verdict"], "长期无新文")
        self.assertEqual(by_id[idle_source]["admitted_pct"], 100.0)
        self.assertEqual(by_id[good_source]["verdict"], "有效")
        self.assertEqual(by_id[good_source]["article_count"], 2)
        self.assertEqual(by_id[good_source]["admitted_pct"], 50.0)
        self.assertEqual(
            result["source_verdict_counts"],
            {"准入率低": 1, "零产出": 1, "长期无新文": 1, "有效": 1},
        )

    # ── 候选挖掘 ──
    def test_mine_keyword_candidates_applies_discrimination_filter(self):
        self._seed_demo_scenario()

        result = m.mine_keyword_candidates("demo_pack", top_n=20, sample_limit=100)
        keywords = result["keywords"]
        excluded = {item["term"]: item["reason"] for item in result["excluded"]}

        # 只在本包 other 语料出现的词 → 选中
        self.assertIn("固态电池", keywords)
        self.assertEqual(
            result["candidates"]["core_keywords"][0]["hits"] >= 3,
            True,
        )
        selected = {item["term"]: item for item in result["candidates"]["anchors"]}
        self.assertIn("固态电池", selected)
        self.assertEqual(selected["固态电池"]["hits"], 3)
        self.assertTrue(selected["固态电池"]["examples"])
        self.assertEqual(selected["固态电池"]["peer_rate"], 0.0)
        # 在其它包语料里同样高频的词 → 被判别力过滤剔除
        self.assertNotIn("并购", keywords)
        self.assertIn("并购", excluded)
        self.assertTrue(
            "对照出现率" in excluded["并购"] or "判别力" in excluded["并购"],
            excluded["并购"],
        )
        # 纯数字/年份类噪声不进候选（也不进排除清单）
        self.assertNotIn("2026年", keywords)
        self.assertNotIn("2026年", excluded)
        self.assertEqual(result["pack_other_docs"], 4)
        self.assertEqual(result["peer_admitted_docs"], 3)
        self.assertTrue(result["recomputable"]["pack_other_article_ids"])

    def test_merge_candidates_targets_gate_and_keeps_original_pack_untouched(self):
        pack = _demo_pack()
        pristine = copy.deepcopy(pack)
        buckets = m._normalize_candidate_input(
            {"core_keywords": ["新核心词"], "entity_keywords": ["某某协会"], "anchors": ["新锚点词"]}
        )
        merged = m._merge_candidates_into_pack(pack, buckets)

        self.assertEqual(pack, pristine)  # 原包一字未改
        self.assertIn("新锚点词", merged["candidate_gate"]["anchor_keywords"])
        self.assertIn("演示锚点词", merged["candidate_gate"]["anchor_keywords"])
        self.assertIn("某某协会", merged["candidate_gate"]["entity_keywords"])
        self.assertIn("新核心词", merged["core_keywords"])
        self.assertIn("演示核心词", merged["core_keywords"])

        # 无 candidate_gate 的包：门禁回退 core_keywords，锚点候选必须并进 core 才生效
        gate_free = _gate_free_pack()
        merged_free = m._merge_candidates_into_pack(gate_free, buckets)
        self.assertIn("新锚点词", merged_free["core_keywords"])
        self.assertNotIn("anchor_keywords", merged_free.get("candidate_gate") or {})
        self.assertIn("演示核心词", merged_free["core_keywords"])

    # ── 影子自测 ──
    def test_shadow_test_metrics_are_exact_on_fixed_samples(self):
        ids, peer_ids = self._seed_demo_scenario()
        candidates = {"core_keywords": [], "entity_keywords": [], "anchors": ["固态电池"]}

        shadow = m.shadow_test("demo_pack", candidates, sample_limit=100, negative_sample=100)

        self.assertEqual(shadow["positive_sample"], 4)
        self.assertEqual(shadow["negative_sample"], 3)
        # 改前：门禁不认"固态电池" → 4 篇全落 other；改后：3 篇拿到 trend（第 4 篇不变）
        self.assertEqual(shadow["admit_rate_before"], 0.0)
        self.assertEqual(shadow["admit_rate_after"], 75.0)
        self.assertEqual(shadow["delta_admit_rate"], 75.0)
        self.assertEqual(shadow["delta_other_pct"], -75.0)
        self.assertEqual(shadow["other_pct_before"], 100.0)
        self.assertEqual(shadow["other_pct_after"], 25.0)
        # 主题命中率（match_fixed_topics 口径）：只有"固态电池中试线投产"命中"中试线"，
        # 改前改后都用同一份 fixed_topics，所以数值相同——这是正确行为
        self.assertEqual(shadow["topic_assoc_before"], 25.0)
        self.assertEqual(shadow["topic_assoc_after"], 25.0)
        # 经门禁后真正挂上主题标签的比例：改前 0%（门禁没过），改后 25%
        self.assertEqual(shadow["topic_tagged_before"], 0.0)
        self.assertEqual(shadow["topic_tagged_after"], 25.0)
        # 负样本（其它包已准入）不含候选词 → 误准入 0
        self.assertEqual(shadow["false_positive_before"], 0.0)
        self.assertEqual(shadow["false_positive_after"], 0.0)
        self.assertEqual(len(shadow["examples"]["newly_admitted"]), 3)
        self.assertEqual(shadow["examples"]["new_false_positives"], [])
        self.assertEqual(
            sorted(shadow["recomputable"]["negative_article_ids"]), sorted(peer_ids)
        )
        self.assertEqual(len(shadow["recomputable"]["positive_article_ids"]), 4)
        self.assertIn(ids["产线良率提升明显"], shadow["recomputable"]["positive_article_ids"])
        self.assertEqual(
            shadow["before"],
            {"admit_rate": 0.0, "false_positive": 0.0, "topic_assoc": 25.0, "other_pct": 100.0},
        )
        self.assertEqual(
            shadow["after"],
            {"admit_rate": 75.0, "false_positive": 0.0, "topic_assoc": 25.0, "other_pct": 25.0},
        )

    def test_shadow_test_counts_false_positive_when_negative_holds_candidate(self):
        _ids, peer_ids = self._seed_demo_scenario()
        # 把候选词 + 趋势词塞进一条对照包文章：改后它会被误拉进本包 → 误准入 1/3
        # （只塞候选词不够：门禁过了但没有趋势/事件信号，仍会落 other）
        cursor = self.db.connection.cursor()
        cursor.execute(
            "UPDATE articles SET content = content || '固态电池与演示趋势词相关纪要。' WHERE id = ?",
            (peer_ids[0],),
        )
        cursor.close()

        shadow = m.shadow_test(
            "demo_pack",
            {"core_keywords": [], "entity_keywords": [], "anchors": ["固态电池"]},
            sample_limit=100,
            negative_sample=100,
        )

        self.assertEqual(shadow["false_positive_before"], 0.0)
        self.assertEqual(shadow["false_positive_after"], round(100.0 / 3, 4))
        self.assertEqual(shadow["delta_false_positive"], round(100.0 / 3, 4))
        self.assertEqual(len(shadow["examples"]["new_false_positives"]), 1)
        self.assertEqual(
            shadow["examples"]["new_false_positives"][0]["title"],
            "精细化工资产并购完成交割",
        )

    # ── 抓文章探针 ──
    def _good_probe(self, pack_id, candidates, *, per_source, timeout_seconds, max_sources):
        return {
            "pack_id": pack_id,
            "probed_at": "2026-01-01T00:00:00Z",
            "sources_selected": [
                {"source_id": 1, "source_name": "低准入信源", "source_url": "https://x/rss", "verdict": "准入率低"}
            ],
            "sources": [
                {
                    "source_id": 1,
                    "source_name": "低准入信源",
                    "source_url": "https://x/rss",
                    "verdict": "准入率低",
                    "listing_status": "ok",
                    "listing_error": "",
                    "fetched": 3,
                    "parsed": 3,
                    "parse_failed": 0,
                    "articles": [],
                    "admit_rate_before": 0.0,
                    "admit_rate_after": 66.67,
                }
            ],
            "sources_probed": 1,
            "sources_succeeded": 1,
            "fetched_total": 3,
            "parsed_total": 3,
            "parse_failed_total": 0,
            "new_article_admit_rate_before": 0.0,
            "new_article_admit_rate_after": 66.67,
            "new_articles": [],
            "sample_available": True,
            "errors": [],
        }

    def _dead_probe(self, pack_id, candidates, *, per_source, timeout_seconds, max_sources):
        return {
            "pack_id": pack_id,
            "probed_at": "2026-01-01T00:00:00Z",
            "sources_selected": [
                {"source_id": 1, "source_name": "超时信源", "source_url": "https://x/rss", "verdict": "零产出"}
            ],
            "sources": [
                {
                    "source_id": 1,
                    "source_name": "超时信源",
                    "source_url": "https://x/rss",
                    "verdict": "零产出",
                    "listing_status": "failed",
                    "listing_error": "ReadTimeout",
                    "fetched": 0,
                    "parsed": 0,
                    "parse_failed": 0,
                    "articles": [],
                    "admit_rate_before": None,
                    "admit_rate_after": None,
                }
            ],
            "sources_probed": 1,
            "sources_succeeded": 0,
            "fetched_total": 0,
            "parsed_total": 0,
            "parse_failed_total": 0,
            "new_article_admit_rate_before": None,
            "new_article_admit_rate_after": None,
            "new_articles": [],
            "sample_available": False,
            "errors": [{"source_id": 1, "stage": "listing", "error": "ReadTimeout"}],
        }

    def test_crawl_probe_marks_fetch_failures_as_errors_not_zero_admission(self):
        self._seed_demo_scenario()
        source = {
            "source_id": 7,
            "source_name": "超时信源",
            "source_url": "https://timeout.example.com/rss",
            "verdict": "零产出",
            "source_type": "rss",
            "metadata": {},
        }

        probe = m.crawl_probe(
            "demo_pack",
            per_source=2,
            timeout_seconds=30,
            source_picker=lambda pack_id, limit: [source],
            listing_fetcher=lambda src, *, limit, timeout_seconds: (_ for _ in ()).throw(
                TimeoutError("ReadTimeout")
            ),
        )

        self.assertEqual(probe["sources_probed"], 1)
        self.assertEqual(probe["parsed_total"], 0)
        self.assertEqual(probe["parse_failed_total"], 0)
        self.assertIsNone(probe["new_article_admit_rate_before"])
        self.assertIsNone(probe["new_article_admit_rate_after"])
        self.assertFalse(probe["sample_available"])
        self.assertEqual(probe["sources"][0]["listing_status"], "failed")
        self.assertEqual(len(probe["errors"]), 1)
        self.assertEqual(probe["errors"][0]["stage"], "listing")
        # 抓取失败绝不能被当成"准入率 0%"：_crawl_probe_verdict 必须判不达标
        verdict = m._crawl_probe_verdict(probe)
        self.assertFalse(verdict["passed"])
        self.assertIn("未取得样本", verdict["reason"])

    def test_crawl_probe_classifies_new_articles_under_before_and_after(self):
        source = {
            "source_id": 3,
            "source_name": "低准入信源",
            "source_url": "https://list.example.com/news",
            "verdict": "准入率低",
            "source_type": "list_page",
            "metadata": {},
        }
        listing = [
            {"url": "https://list.example.com/1", "title": "固态电池产线动态", "published_at": _date(1)},
            {"url": "https://list.example.com/2", "title": "无关条目", "published_at": _date(1)},
            {"url": "https://list.example.com/3", "title": "抓取失败条目", "published_at": _date(1)},
        ]

        def _content_fetcher(entry, *, timeout_seconds):
            if entry["url"].endswith("/3"):
                return None, "正文过短（0 字 < 80）"
            body = "演示趋势词与固态电池相关纪要。" if entry["url"].endswith("/1") else "与主题无关的简述。"
            return {"title": entry["title"], "content": body, "matched_keywords": "", "url": entry["url"]}, ""

        probe = m.crawl_probe(
            "demo_pack",
            per_source=3,
            timeout_seconds=30,
            candidates={"core_keywords": [], "entity_keywords": [], "anchors": ["固态电池"]},
            source_picker=lambda pack_id, limit: [source],
            listing_fetcher=lambda src, *, limit, timeout_seconds: (listing, ""),
            content_fetcher=_content_fetcher,
        )

        self.assertEqual(probe["fetched_total"], 3)
        self.assertEqual(probe["parsed_total"], 2)
        self.assertEqual(probe["parse_failed_total"], 1)
        self.assertEqual(probe["sources_succeeded"], 1)
        self.assertEqual(probe["new_article_admit_rate_before"], 0.0)
        self.assertEqual(probe["new_article_admit_rate_after"], 50.0)
        first = probe["sources"][0]["articles"][0]
        self.assertEqual(first["category_before"], "other")
        self.assertEqual(first["category_after"], "trend")
        self.assertTrue(first["admit_after"])
        # 过门禁的真正原因是锚点（matched_keywords 只收 core/expanded/trend/event/negative）
        self.assertEqual(first["anchors_after"], ["固态电池"])
        self.assertEqual(first["anchors_before"], [])
        self.assertEqual(first["matched_keywords_after"], ["演示趋势词"])
        self.assertEqual(len(probe["errors"]), 1)
        self.assertEqual(probe["errors"][0]["stage"], "content")
        self.assertIn("正文过短", probe["errors"][0]["error"])

    # ── 串起来跑 ──
    def test_run_self_test_and_stage_verifies_when_both_sets_pass(self):
        self._seed_demo_scenario()
        before_snapshot = self.loader.snapshot()
        with open(SEED_PACK_FILE, "rb") as handle:
            seed_before = handle.read()
        with patch(
            "industry_pack_activation.IndustryPackActivationService.preview",
            side_effect=AssertionError("自测阶段不允许调用激活服务"),
        ) as preview_mock, patch(
            "industry_pack_activation.IndustryPackActivationService.activate",
            side_effect=AssertionError("自测阶段不允许调用激活服务"),
        ) as activate_mock:
            result = m.run_self_test_and_stage(
                "demo_pack", sample_limit=100, probe_runner=self._good_probe
            )
            preview_mock.assert_not_called()
            activate_mock.assert_not_called()

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["shadow_test"]["admit_rate_before"], 0.0)
        self.assertEqual(result["shadow_test"]["admit_rate_after"], 75.0)
        self.assertEqual(result["crawl_probe"]["new_article_admit_rate_after"], 66.67)
        suggestion = result["suggestion"]
        self.assertEqual(suggestion["status"], "verified")
        self.assertEqual(suggestion["kind"], "keyword")
        self.assertEqual(
            set(suggestion["payload"]["candidates"]),
            {"core_keywords", "entity_keywords", "anchors"},
        )
        for bucket in suggestion["payload"]["candidates"].values():
            for item in bucket:
                self.assertEqual(set(item), {"term", "hits", "examples"})
        self.assertIn("固态电池", [item["term"] for item in suggestion["payload"]["candidates"]["anchors"]])
        metrics = suggestion["metrics"]
        for key in ("admit_rate", "false_positive", "topic_assoc", "other_pct"):
            self.assertIn(key, metrics["before"])
            self.assertIn(key, metrics["after"])
        self.assertIsNotNone(metrics["crawl_probe"])
        self.assertEqual(metrics["crawl_probe"]["sources"][0]["source_id"], 1)
        self.assertEqual(metrics["crawl_probe"]["sources"][0]["fetched"], 3)
        self.assertEqual(
            metrics["crawl_probe"]["new_article_admit_rate_before"], 0.0
        )
        self.assertIn("影子自测达标", suggestion["reason"])
        self.assertIn("抓文章实测达标", suggestion["reason"])
        # 生产配置零改动
        self.assertEqual(self.loader.snapshot(), before_snapshot)
        with open(SEED_PACK_FILE, "rb") as handle:
            self.assertEqual(handle.read(), seed_before)
        # 建议确实落库了
        self.assertEqual(len(m.list_suggestions("demo_pack", status="verified")), 1)

    def test_run_self_test_and_stage_rejects_when_threshold_raised(self):
        self._seed_demo_scenario()
        with patch.object(m, "MIN_ADMIT_RATE_GAIN_PCT", 99.0):
            result = m.run_self_test_and_stage(
                "demo_pack", sample_limit=100, probe_runner=self._good_probe
            )
        self.assertEqual(result["status"], "rejected")
        self.assertIn("影子自测未达标", result["reason"])
        self.assertEqual(result["suggestion"]["status"], "rejected")
        self.assertEqual(m.list_suggestions("demo_pack", status="verified"), [])

    def test_run_self_test_and_stage_rejects_when_crawl_probe_has_no_sample(self):
        self._seed_demo_scenario()
        result = m.run_self_test_and_stage(
            "demo_pack", sample_limit=100, probe_runner=self._dead_probe
        )
        self.assertEqual(result["status"], "rejected")
        self.assertIn("抓文章实测未达标", result["reason"])
        self.assertIn("未取得样本", result["reason"])
        # 影子自测本身是达标的，被否决的唯一原因是抓文章测试没有样本
        self.assertEqual(result["shadow_test"]["admit_rate_after"], 75.0)
        self.assertIsNone(result["crawl_probe"]["new_article_admit_rate_after"])
        self.assertEqual(result["suggestion"]["status"], "rejected")

    def _passing_metrics(self):
        """一份"影子 + 抓文章双达标"的指标（供 verified 相关用例复用）。"""
        return {
            "before": {
                "admit_rate": 0.0,
                "false_positive": 0.0,
                "topic_assoc": 0.0,
                "other_pct": 100.0,
            },
            "after": {
                "admit_rate": 75.0,
                "false_positive": 0.0,
                "topic_assoc": 0.0,
                "other_pct": 25.0,
            },
            "crawl_probe": {
                "parsed_total": 3,
                "sample_available": True,
                "new_article_admit_rate_before": 0.0,
                "new_article_admit_rate_after": 50.0,
                "sources": [],
                "errors": [],
            },
            # 自测来源标记：只有 run_self_test_and_stage 会写，外部不得伪造 verified
            "self_test": {
                "source": m.SELF_TEST_SOURCE,
                "pack_id": "demo_pack",
                "passed": True,
                "reason": "影子自测达标；抓文章实测达标",
            },
        }

    def _passing_source_metrics(self):
        """信源类建议的达标指标（口径：有探测证明不可用的可执行项 + 自测来源标记）。

        2026-10 起信源类建议不再看"零产出"这类机械规则，只认 probe_sources 的实测证据，
        所以 fixture 里也带上一份"证明 #9 坏、#99 正常"的逐源探测记录。
        """
        return {
            "before": {"admit_rate": 0.0, "false_positive": None, "topic_assoc": None, "other_pct": 100.0},
            "after": {"admit_rate": None, "false_positive": None, "topic_assoc": None, "other_pct": None},
            "crawl_probe": {
                "parsed_total": 0,
                "sample_available": False,
                "new_article_admit_rate_before": None,
                "new_article_admit_rate_after": None,
                "sources": [],
                "errors": [],
            },
            "source_evidence": {
                "actionable_count": 1,
                "unverified_count": 0,
                "disable_source_ids": [9],
                "source_probe": self._source_probe_fixture(
                    [{"source_id": 9, "verdict": m.PROBE_VERDICT_DEAD, "http_status": 404,
                      "reachable": False, "parsed_items": 0, "error": "HTTP 404"}]
                ),
            },
            "self_test": {
                "source": m.SELF_TEST_SOURCE,
                "kind": "source",
                "passed": True,
                "reason": "信源体检：建议停用 1 个源（探测证明不可用）",
            },
        }

    def test_stage_suggestion_refuses_external_verified_when_metrics_fail(self):
        passing = self._passing_metrics()
        record = m.stage_suggestion(
            "demo_pack",
            "keyword",
            {"candidates": {"core_keywords": ["固态电池"], "entity_keywords": [], "anchors": ["固态电池"]}},
            passing,
            status="verified",
        )
        self.assertEqual(record["status"], "verified")

        # 没有自测来源标记（外部直接塞一份"看起来达标"的指标）→ 一律降级 rejected
        no_marker = copy.deepcopy(passing)
        no_marker.pop("self_test")
        record = m.stage_suggestion(
            "demo_pack",
            "keyword",
            {"candidates": {"core_keywords": ["无标记词"], "entity_keywords": [], "anchors": ["无标记词"]}},
            no_marker,
            status="verified",
        )
        self.assertEqual(record["status"], "rejected")
        self.assertIn("缺少自测来源标记", record["reason"])

        failing = copy.deepcopy(passing)
        failing["after"]["admit_rate"] = 10.0  # 提升只有 10 个百分点
        record = m.stage_suggestion(
            "demo_pack",
            "keyword",
            {"candidates": {"core_keywords": ["假词"], "entity_keywords": [], "anchors": ["假词"]}},
            failing,
            status="verified",
        )
        self.assertEqual(record["status"], "rejected")
        self.assertIn("复算未达标", record["reason"])
        self.assertIn("准入率提升", record["reason"])

        # 抓文章测试缺失时，哪怕影子指标很好也不允许 verified
        no_probe = copy.deepcopy(passing)
        no_probe["crawl_probe"] = None
        record = m.stage_suggestion(
            "demo_pack",
            "keyword",
            {"candidates": {"core_keywords": ["词"], "entity_keywords": [], "anchors": ["词"]}},
            no_probe,
            status="verified",
        )
        self.assertEqual(record["status"], "rejected")
        self.assertIn("抓文章实测未达标", record["reason"])

    def test_suggestion_round_trip_and_status_filter(self):
        first = m.stage_suggestion(
            "demo_pack",
            "keyword",
            {"candidates": {"core_keywords": ["固态电池"], "entity_keywords": [], "anchors": ["固态电池"]}},
            {"before": {"admit_rate": 0.0}, "after": {"admit_rate": 50.0}},
            status="rejected",
            reason="样本不足",
            evidence={"note": "证据"},
        )
        second = m.stage_suggestion(
            "demo_pack",
            "source",
            {"source_id": 9, "action": "enable"},
            self._passing_source_metrics(),
            status="verified",
            reason="信源体检：建议停用 1 个零产出源",
        )
        self.assertNotEqual(first["suggestion_id"], second["suggestion_id"])
        # 扁平键兼容：before/after 只有 admit_rate 时，缺的指标写 None（键必须存在）
        self.assertEqual(first["metrics"]["before"]["admit_rate"], 0.0)
        self.assertIsNone(first["metrics"]["before"]["false_positive"])
        self.assertIsNone(first["metrics"]["crawl_probe"])
        self.assertEqual(first["evidence"], {"note": "证据"})
        # source 类型的 payload 原样保留
        self.assertEqual(second["payload"], {"source_id": 9, "action": "enable"})

        self.assertEqual(len(m.list_suggestions("demo_pack")), 2)
        self.assertEqual(len(m.list_suggestions("demo_pack", status="rejected")), 1)
        self.assertEqual(len(m.list_suggestions("demo_pack", status="verified")), 1)
        self.assertEqual(m.list_suggestions("demo_pack", status="applied"), [])
        with self.assertRaises(ValueError):
            m.list_suggestions("demo_pack", status="不存在的状态")

        fetched = m.get_suggestion(first["suggestion_id"])
        self.assertEqual(fetched["suggestion_id"], first["suggestion_id"])
        self.assertEqual(fetched["kind"], "keyword")
        self.assertIsNone(m.get_suggestion(999999))

        applied = m.mark_applied(
            second["suggestion_id"],
            activation_result={"activation_id": "act-123", "target_version_id": 7},
        )
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(applied["activation_id"], "act-123")
        self.assertIsNotNone(applied["applied_at"])
        self.assertEqual(
            applied["after_apply"]["activation_result"]["target_version_id"], 7
        )

        retested = m.record_after_apply(
            second["suggestion_id"],
            {"before": {"admit_rate": 0.0}, "after": {"admit_rate": 61.5}},
        )
        self.assertEqual(retested["status"], "applied")  # 复测回填不改状态
        self.assertEqual(retested["after_apply"]["after_apply"]["after"]["admit_rate"], 61.5)
        self.assertEqual(
            retested["after_apply"]["activation_result"]["activation_id"], "act-123"
        )

        rejected = m.mark_rejected(first["suggestion_id"], reason="人工判断噪声偏多")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["reason"], "人工判断噪声偏多")
        self.assertEqual(len(m.list_suggestions("demo_pack", status="rejected")), 1)
        self.assertEqual(len(m.list_suggestions("demo_pack", status="applied")), 1)
        with self.assertRaises(ValueError):
            m.mark_rejected(999999)

    def test_stage_suggestion_validates_kind_and_status(self):
        with self.assertRaises(ValueError):
            m.stage_suggestion("demo_pack", "unknown", {}, {}, status="verified")
        with self.assertRaises(ValueError):
            m.stage_suggestion("demo_pack", "keyword", {}, {}, status="pending")
        with self.assertRaises(ValueError):
            m.stage_suggestion("missing_pack", "keyword", {}, {}, status="rejected")

    # ── 生成新版本（发布链路） ──
    def _admin_service(self):
        from industry_pack_admin import IndustryPackAdminService, IndustryPackVersionStore

        return IndustryPackAdminService(IndustryPackVersionStore(self.db), self.loader)

    def test_prepare_pack_version_publishes_new_version_with_merged_terms(self):
        self._seed_demo_scenario()
        result = m.run_self_test_and_stage(
            "demo_pack", sample_limit=100, probe_runner=self._good_probe
        )
        suggestion_id = result["suggestion_id"]
        self.assertEqual(result["status"], "verified")

        prepared = m.prepare_pack_version(
            "demo_pack", suggestion_id, admin_service=self._admin_service()
        )

        self.assertIsNone(prepared["error"])
        self.assertIsInstance(prepared["target_version_id"], int)
        self.assertEqual(prepared["pack_version"], "1.0.1")  # 版本号补丁位 +1
        self.assertFalse(prepared["already_prepared"])
        self.assertIn("固态电池", prepared["merged_terms"]["anchor_keywords"]["added"])
        self.assertIn("固态电池", prepared["merged_terms"]["core_keywords"]["added"])
        self.assertIn("plan_sha256", prepared)
        self.assertIn("preview_error", prepared)
        self.assertIn("source_summary", prepared)

        # 真的是一个新版本（版本表里有行，且 manifest 里带上了候选词）
        rows = list(
            self.db.connection.execute(
                "SELECT id, pack_version, manifest_json FROM industry_pack_versions "
                "WHERE industry_pack_id='demo_pack' ORDER BY version_number"
            )
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0][0]), prepared["target_version_id"])
        self.assertEqual(rows[0][1], "1.0.1")
        manifest = json.loads(rows[0][2])
        self.assertIn("固态电池", manifest["candidate_gate"]["anchor_keywords"])
        self.assertIn("演示锚点词", manifest["candidate_gate"]["anchor_keywords"])  # 原有词保留
        # 草稿已消费（发布即删除草稿）
        drafts = list(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM industry_pack_drafts WHERE industry_pack_id='demo_pack'"
            )
        )
        self.assertEqual(int(drafts[0][0]), 0)
        # 重复调用走幂等分支：不再产生第二个版本
        again = m.prepare_pack_version(
            "demo_pack", suggestion_id, admin_service=self._admin_service()
        )
        self.assertTrue(again["already_prepared"])
        self.assertEqual(again["target_version_id"], prepared["target_version_id"])
        self.assertEqual(
            int(
                list(
                    self.db.connection.execute(
                        "SELECT COUNT(*) FROM industry_pack_versions "
                        "WHERE industry_pack_id='demo_pack'"
                    )
                )[0][0]
            ),
            1,
        )

    def test_prepare_pack_version_refuses_unverified_or_non_keyword(self):
        rejected = m.stage_suggestion(
            "demo_pack",
            "keyword",
            {"candidates": {"core_keywords": ["词"], "entity_keywords": [], "anchors": ["词"]}},
            {"before": {"admit_rate": 0.0}, "after": {"admit_rate": 10.0}},
            status="rejected",
            reason="未达标",
        )
        prepared = m.prepare_pack_version(
            "demo_pack", rejected["suggestion_id"], admin_service=self._admin_service()
        )
        self.assertIn("只接受 status=verified", prepared["error"])
        self.assertIsNone(prepared["target_version_id"])
        self.assertEqual(
            int(
                list(
                    self.db.connection.execute(
                        "SELECT COUNT(*) FROM industry_pack_versions"
                    )
                )[0][0]
            ),
            0,
        )

        source_suggestion = m.stage_suggestion(
            "demo_pack",
            "source",
            {"disable_sources": [{"source_id": 1}], "replace_sources": [], "add_sources": [], "keep_sources": []},
            {
                "before": {}, "after": {},
                "source_evidence": {
                    "actionable_count": 1,
                    "disable_source_ids": [1],
                    # 信源类建议要 verified 必须带 probe_sources 的真实探测证据（#1 实测不可用）
                    "source_probe": self._source_probe_fixture(
                        [
                            {"source_id": 1, "verdict": m.PROBE_VERDICT_DEAD, "http_status": 404,
                             "reachable": False, "parsed_items": 0, "error": "HTTP 404"}
                        ]
                    ),
                },
                "self_test": {"source": m.SELF_TEST_SOURCE, "kind": "source", "passed": True},
            },
            status="verified",
            reason="信源体检",
        )
        self.assertEqual(source_suggestion["status"], "verified")
        prepared = m.prepare_pack_version(
            "demo_pack", source_suggestion["suggestion_id"], admin_service=self._admin_service()
        )
        self.assertIn("只支持 keyword 类建议", prepared["error"])

    # ── 信源类建议 ──
    def _probe_stub(self, source_rows, errors=None, parsed_total=0):
        def _runner(pack_id, candidates, *, per_source, timeout_seconds, max_sources):
            fetched = sum(int(row.get("fetched") or 0) for row in source_rows)
            return {
                "pack_id": pack_id,
                "probed_at": "2026-01-01T00:00:00Z",
                "sources_selected": [],
                "sources": source_rows,
                "sources_probed": len(source_rows),
                "sources_succeeded": sum(1 for row in source_rows if int(row.get("parsed") or 0) > 0),
                "fetched_total": fetched,
                "parsed_total": parsed_total,
                "parse_failed_total": 0,
                "new_article_admit_rate_before": None,
                "new_article_admit_rate_after": None,
                "new_articles": [],
                "sample_available": parsed_total > 0,
                "errors": list(errors or []),
            }

        return _runner

    # ── 信源实测探针（probe_sources）夹具 ──
    def _source_probe_fixture(self, rows, *, healthy_id=99):
        """造一份 probe_sources 形态的探测记录（默认再补一个"可达"样本，便于过样本闸门）。"""
        records = [
            {
                "source_id": int(row["source_id"]),
                "source_name": row.get("source_name", f"源{row['source_id']}"),
                "source_url": row.get("source_url", f"https://s{row['source_id']}.example.com/rss"),
                "probe_url": row.get("source_url", f"https://s{row['source_id']}.example.com/rss"),
                "source_type": "rss",
                "is_enabled": True,
                "probed": row.get("probed", True),
                "reachable": bool(row.get("reachable")),
                "http_status": row.get("http_status"),
                "content_type": row.get("content_type", ""),
                "parsed_items": int(row.get("parsed_items") or 0),
                "error": row.get("error", ""),
                "verdict": row["verdict"],
                "probed_at": "2026-01-01T00:00:00Z",
            }
            for row in rows
        ]
        if healthy_id is not None and not any(r["reachable"] for r in records):
            records.append(
                {
                    "source_id": int(healthy_id),
                    "source_name": "正常源",
                    "source_url": "https://healthy.example.com/rss",
                    "probe_url": "https://healthy.example.com/rss",
                    "source_type": "rss",
                    "is_enabled": True,
                    "probed": True,
                    "reachable": True,
                    "http_status": 200,
                    "content_type": "application/rss+xml",
                    "parsed_items": 12,
                    "error": "",
                    "verdict": m.PROBE_VERDICT_OK,
                    "probed_at": "2026-01-01T00:00:00Z",
                }
            )
        verdict_counts = {}
        for record in records:
            verdict_counts[record["verdict"]] = verdict_counts.get(record["verdict"], 0) + 1
        return {
            "pack_id": "demo_pack",
            "generated_by": m.SOURCE_PROBE_GENERATOR,
            "probed_at": "2026-01-01T00:00:00Z",
            "limit": 20,
            "timeout_seconds": 12,
            "sources": records,
            "sources_probed": sum(1 for record in records if record["probed"]),
            "sources_reachable": sum(1 for record in records if record["reachable"]),
            "sample_available": any(record["reachable"] for record in records),
            "verdict_counts": verdict_counts,
            "errors": [],
        }

    def _source_probe_stub(self, rows, **kwargs):
        """probe_sources 的桩（完全离线）：回放给定逐源记录。"""
        fixture = self._source_probe_fixture(rows, **kwargs)

        def _runner(pack_id, *, limit, timeout_seconds):
            return dict(fixture, pack_id=pack_id, limit=limit, timeout_seconds=timeout_seconds)

        return _runner

    def _seed_source_mix(self):
        """一个零产出源 + 一个准入率低源 + 一个停更源（信源建议的三种输入）。"""
        zero = self._insert_source("demo_pack", "零产出源", "https://zero.example.com/rss")
        low = self._insert_source("demo_pack", "低准入源", "https://low.example.com/rss")
        idle = self._insert_source("demo_pack", "停更源", "https://idle.example.com/rss")
        for index in range(2):
            article_id = self._insert_article(
                f"低准入文章{index}", f"低准入正文{index}", f"https://low.example.com/a{index}"
            )
            self._insert_classification(article_id, "demo_pack", final_category="other")
            self._link_article_to_source(article_id, low)
        idle_id = self._insert_article(
            "停更文章", "停更正文", "https://idle.example.com/a1", publish_date=_date(120)
        )
        self._insert_classification(idle_id, "demo_pack", final_category="trend")
        self._link_article_to_source(idle_id, idle)
        return zero, low, idle

    def test_run_self_test_and_stage_emits_actionable_source_suggestion(self):
        zero, low, idle = self._seed_source_mix()
        probe_rows = [
            {"source_id": zero, "source_name": "零产出源", "listing_status": "ok",
             "listing_error": "", "fetched": 3, "parsed": 0, "parse_failed": 0,
             "articles": [], "verdict": "零产出"},
            {"source_id": low, "source_name": "低准入源", "listing_status": "ok",
             "listing_error": "", "fetched": 2, "parsed": 0, "parse_failed": 2,
             "articles": [], "verdict": "准入率低"},
            {"source_id": idle, "source_name": "停更源", "listing_status": "ok",
             "listing_error": "", "fetched": 3, "parsed": 0, "parse_failed": 0,
             "articles": [], "verdict": "长期无新文"},
        ]

        result = m.run_self_test_and_stage(
            "demo_pack",
            sample_limit=50,
            probe_runner=self._probe_stub(probe_rows),
            # 信源实测探针：只有探测证明坏的源才允许进停用/替换（零产出不再作依据）
            source_probe_runner=self._source_probe_stub(
                [
                    {"source_id": zero, "verdict": m.PROBE_VERDICT_DEAD, "http_status": 404,
                     "reachable": False, "parsed_items": 0, "error": "HTTP 404"},
                    {"source_id": low, "verdict": m.PROBE_VERDICT_OK, "http_status": 200,
                     "reachable": True, "parsed_items": 8},
                    {"source_id": idle, "verdict": m.PROBE_VERDICT_EMPTY_FEED, "http_status": 200,
                     "reachable": True, "parsed_items": 0},
                ]
            ),
        )

        self.assertIn("source_suggestion", result)
        suggestion = result["source_suggestion"]
        self.assertEqual(suggestion["kind"], "source")
        self.assertEqual(suggestion["status"], "verified")
        self.assertEqual(result["source_suggestion_id"], suggestion["suggestion_id"])
        payload = suggestion["payload"]
        self.assertEqual(
            set(payload),
            {"disable_sources", "replace_sources", "add_sources", "keep_sources",
             "unverified_sources", "notes"},
        )
        self.assertEqual([item["source_id"] for item in payload["disable_sources"]], [zero])
        self.assertEqual([item["source_id"] for item in payload["replace_sources"]], [idle])
        self.assertEqual([item["source_id"] for item in payload["keep_sources"]], [low])
        self.assertEqual(payload["unverified_sources"], [])
        self.assertEqual(payload["add_sources"], [])
        self.assertIn("历史入库产出 0 篇", payload["disable_sources"][0]["reason"])
        self.assertIn("准入率仅", payload["keep_sources"][0]["reason"])
        self.assertIn("随关键词改进观察", payload["keep_sources"][0]["reason"])
        metrics = suggestion["metrics"]
        # 整包口径：3 篇里 2 篇 other → other 66.6667%、准入率 33.3333%
        self.assertEqual(metrics["before"]["other_pct"], 66.6667)
        self.assertEqual(metrics["before"]["admit_rate"], 33.3333)
        self.assertIsNone(metrics["after"]["admit_rate"])
        self.assertEqual(
            [(row["source_id"], row["fetched"], row["parse_failed"])
             for row in metrics["crawl_probe"]["sources"]],
            [(zero, 3, 0), (low, 2, 2), (idle, 3, 0)],
        )
        self.assertIn("建议停用 1 个零产出源", suggestion["reason"])
        # 关键词侧没有候选词 → 关键词建议是 rejected，与信源建议互不影响
        self.assertEqual(result["status"], "rejected")
        self.assertIn("未挖到可用候选词", result["reason"])

    def test_source_suggestion_marks_fetch_failure_as_unverified_not_disable(self):
        zero, low, idle = self._seed_source_mix()
        probe_rows = [
            {"source_id": zero, "source_name": "零产出源", "listing_status": "failed",
             "listing_error": "ReadTimeout", "fetched": 0, "parsed": 0, "parse_failed": 0,
             "articles": [], "verdict": "零产出"},
            {"source_id": low, "source_name": "低准入源", "listing_status": "ok",
             "listing_error": "", "fetched": 1, "parsed": 0, "parse_failed": 1,
             "articles": [], "verdict": "准入率低"},
            {"source_id": idle, "source_name": "停更源", "listing_status": "ok",
             "listing_error": "", "fetched": 1, "parsed": 0, "parse_failed": 0,
             "articles": [], "verdict": "长期无新文"},
        ]
        errors = [{"source_id": zero, "stage": "listing", "error": "ReadTimeout"}]

        result = m.run_self_test_and_stage(
            "demo_pack",
            sample_limit=50,
            probe_runner=self._probe_stub(probe_rows, errors=errors),
            # 信源探测显示零产出源可用（#zero 可达）→ 不许停用，只能进 unverified
            source_probe_runner=self._source_probe_stub(
                [
                    {"source_id": zero, "verdict": m.PROBE_VERDICT_OK, "http_status": 200,
                     "reachable": True, "parsed_items": 5},
                    {"source_id": low, "verdict": m.PROBE_VERDICT_OK, "http_status": 200,
                     "reachable": True, "parsed_items": 5},
                    {"source_id": idle, "verdict": m.PROBE_VERDICT_EMPTY_FEED, "http_status": 200,
                     "reachable": True, "parsed_items": 0},
                ]
            ),
        )

        suggestion = result["source_suggestion"]
        payload = suggestion["payload"]
        # 抓取失败的零产出源只能进 unverified，不能进停用建议
        self.assertEqual(payload["disable_sources"], [])
        self.assertEqual(
            [item["source_id"] for item in payload["unverified_sources"]], [zero]
        )
        self.assertIn("无法确认该源状态", payload["unverified_sources"][0]["reason"])
        self.assertIn(
            "抓取失败", payload["unverified_sources"][0]["reason"]
        )
        self.assertEqual(suggestion["metrics"]["crawl_probe"]["errors"], errors)
        # 可执行项只剩"替换停更源"→ 仍达标；若连它都没有，就必须 rejected
        self.assertEqual([item["source_id"] for item in payload["replace_sources"]], [idle])
        self.assertEqual(suggestion["status"], "verified")

    def test_source_suggestion_without_actionable_items_is_rejected(self):
        _zero, low, _idle = self._seed_source_mix()
        # 只暴露"准入率低"这个正常源（没有停更、没有零产出）→ 无可执行项
        cursor = self.db.connection.cursor()
        cursor.execute(
            "UPDATE intel_sources SET source_name='停更源' WHERE source_name='停更源'"
        )
        cursor.close()
        probe_rows = [
            {"source_id": low, "source_name": "低准入源", "listing_status": "ok",
             "listing_error": "", "fetched": 1, "parsed": 0, "parse_failed": 1,
             "articles": [], "verdict": "准入率低"},
        ]
        # 把停更源的最近文章改成"今天"，让它判为有效；零产出源保持零产出 → 仍有可执行项，
        # 因此这里直接构造一个"只有有效源"的评估结果来验证门槛。
        assessment = {
            "pack_id": "demo_pack",
            "other_pct": 0.0,
            "sources": [
                {"source_id": low, "source_name": "低准入源", "source_url": "https://low",
                 "verdict": "有效", "article_count": 2, "admitted_count": 1,
                 "admitted_pct": 50.0, "last_article_at": "2026-10-01 00:00:00"},
            ],
            "source_verdict_counts": {"有效": 1},
        }
        built = m._build_source_suggestion(assessment, {"sources": probe_rows, "errors": []})
        self.assertFalse(built["passed"])
        self.assertIn("没有可执行的信源调整项", built["reason"])
        record = m.stage_suggestion(
            "demo_pack",
            "source",
            built["payload"],
            built["metrics"],
            status="verified" if built["passed"] else "rejected",
            evidence=built["evidence"],
            reason=built["reason"],
        )
        self.assertEqual(record["status"], "rejected")

        # 即使外部硬塞 verified，也因为没有可执行项被降级
        forced = copy.deepcopy(built["metrics"])
        record = m.stage_suggestion(
            "demo_pack", "source", built["payload"], forced, status="verified"
        )
        self.assertEqual(record["status"], "rejected")
        self.assertIn("没有可执行项", record["reason"])

    # ── 信源实测探针 probe_sources（注入假 fetcher，全程离线） ──
    def test_probe_sources_verdicts_cover_ok_dead_dns_blocked_and_empty(self):
        """五种情形（可用/404/DNS 失败/403/200 但 0 条）+ 200 HTML 空页面，verdict 与条目数都要对。"""
        ok = self._insert_source("demo_pack", "可用 RSS", "https://ok.example.com/rss")
        dead = self._insert_source("demo_pack", "404 源", "https://dead.example.com/rss")
        dns = self._insert_source("demo_pack", "DNS 失败源", "https://dns.example.com/rss")
        blocked = self._insert_source("demo_pack", "403 源", "https://blocked.example.com/rss")
        empty = self._insert_source("demo_pack", "空 feed 源", "https://empty.example.com/rss")
        shell = self._insert_source("demo_pack", "空壳首页", "https://shell.example.com/news")
        feed_ok = (
            "<?xml version='1.0' encoding='UTF-8'?><rss version='2.0'><channel>"
            + "".join(
                f"<item><title>条目{i}</title><link>https://ok.example.com/a{i}</link>"
                f"<pubDate>Mon, 05 Oct 2026 0{i}:00:00 GMT</pubDate></item>"
                for i in range(3)
            )
            + "</channel></rss>"
        )
        feed_empty = (
            "<?xml version='1.0' encoding='UTF-8'?><rss version='2.0'><channel>"
            "<title>空</title></channel></rss>"
        )
        calls = []

        def fake_fetcher(source, url, timeout_seconds):
            calls.append((source["source_id"], url, timeout_seconds))
            source_id = source["source_id"]
            if source_id == ok:
                return {"status_code": 200, "content": feed_ok,
                        "content_type": "application/rss+xml", "url": url}
            if source_id == dead:
                return {"status_code": 404, "content": b"<html>gone</html>",
                        "content_type": "text/html", "url": url}
            if source_id == dns:
                raise OSError("Name or service not known")
            if source_id == blocked:
                return {"status_code": 403, "content": b"<html>forbidden</html>",
                        "content_type": "text/html", "url": url}
            if source_id == empty:
                return {"status_code": 200, "content": feed_empty,
                        "content_type": "application/rss+xml", "url": url}
            return {"status_code": 200,
                    "content": b"<html><body><p>nothing here</p></body></html>",
                    "content_type": "text/html; charset=utf-8", "url": url}

        probe = m.probe_sources(
            "demo_pack",
            source_ids=[ok, dead, dns, blocked, empty, shell],
            limit=20,
            timeout_seconds=12,
            fetcher=fake_fetcher,
        )

        by_id = {row["source_id"]: row for row in probe["sources"]}
        self.assertEqual(probe["generated_by"], "probe_sources")
        self.assertEqual(len(probe["sources"]), 6)
        # 每源最多 1 次请求（串行、不重试）
        self.assertEqual(len(calls), 6)
        self.assertEqual(by_id[ok]["verdict"], m.PROBE_VERDICT_OK)
        self.assertEqual(by_id[ok]["parsed_items"], 3)
        self.assertTrue(by_id[ok]["reachable"])
        self.assertEqual(by_id[dead]["verdict"], m.PROBE_VERDICT_DEAD)
        self.assertEqual(by_id[dead]["http_status"], 404)
        self.assertEqual(by_id[dead]["parsed_items"], 0)
        self.assertFalse(by_id[dead]["reachable"])
        self.assertEqual(by_id[dns]["verdict"], m.PROBE_VERDICT_DEAD)
        self.assertIsNone(by_id[dns]["http_status"])
        self.assertIn("Name or service not known", by_id[dns]["error"])
        self.assertEqual(by_id[blocked]["verdict"], m.PROBE_VERDICT_BLOCKED)
        self.assertEqual(by_id[blocked]["http_status"], 403)
        self.assertEqual(by_id[empty]["verdict"], m.PROBE_VERDICT_EMPTY_FEED)
        self.assertEqual(by_id[empty]["parsed_items"], 0)
        # 200 + HTML + 0 条 = 页面不可解析（不是"空 feed"，也**不是**停用依据）
        self.assertEqual(by_id[shell]["verdict"], m.PROBE_VERDICT_UNPARSABLE)
        self.assertTrue(by_id[shell]["reachable"])
        self.assertEqual(probe["sources_probed"], 6)
        self.assertEqual(probe["sources_reachable"], 3)
        self.assertTrue(probe["sample_available"])
        self.assertEqual(probe["verdict_counts"][m.PROBE_VERDICT_DEAD], 2)
        # 口径固定：只有这三种 verdict（或 reachable=False）才算"探测证明坏"
        self.assertTrue(m._source_probe_proves_bad(by_id[dead]))
        self.assertTrue(m._source_probe_proves_bad(by_id[blocked]))
        self.assertTrue(m._source_probe_proves_bad(by_id[empty]))
        self.assertFalse(m._source_probe_proves_bad(by_id[shell]))
        self.assertFalse(m._source_probe_proves_bad(by_id[ok]))

    def test_probe_sources_default_selection_respects_limit(self):
        """缺省选源走"零产出/准入率低优先"，且遵守 limit（默认口径最多 20 个源）。"""
        for index in range(4):
            self._insert_source("demo_pack", f"零产出源{index}", f"https://z{index}.example.com/rss")
        calls = []

        def fake_fetcher(source, url, timeout_seconds):
            calls.append(source["source_id"])
            return {"status_code": 200, "content": b"<html><body>x</body></html>",
                    "content_type": "text/html", "url": url}

        probe = m.probe_sources("demo_pack", limit=2, timeout_seconds=12, fetcher=fake_fetcher)
        self.assertEqual(len(probe["sources"]), 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(probe["sources"][0]["verdict"], m.PROBE_VERDICT_UNPARSABLE)

    # ── 信源类建议的 verified 闸门（先探测、后 verified） ──
    def _stage_source_probe_suggestion(self, payload, source_probe, *, reason="信源体检"):
        """按引擎口径落一条信源建议（带自测来源标记），返回落库后的记录。"""
        metrics = {
            "before": {}, "after": {},
            "source_evidence": {
                "actionable_count": len(payload.get("disable_sources") or [])
                + len(payload.get("replace_sources") or []),
                "source_probe": source_probe,
            },
            "self_test": {
                "source": m.SELF_TEST_SOURCE, "kind": "source",
                "pack_id": "demo_pack", "passed": True, "reason": reason,
            },
        }
        return m.stage_suggestion(
            "demo_pack", "source", payload, metrics,
            status="verified", evidence={"source_probe": source_probe}, reason=reason,
        )

    def test_source_gate_rejects_disabling_healthy_zero_output_source(self):
        """上一轮真实事故回归：零产出但 URL 正常的源被建议停用 → 必须 rejected。

        A 机 invest_mgmt 就是按"零产出 → 停用"机械规则产出了"停用全部 65 个源"的 verified 建议，
        实测其中 46 个 URL 完全正常（只是调度没跑）。这条口径必须永远拦住它。
        """
        probe = self._source_probe_fixture(
            [{"source_id": 501, "verdict": m.PROBE_VERDICT_OK, "http_status": 200,
              "reachable": True, "parsed_items": 18}]
        )
        payload = {
            "disable_sources": [
                {"source_id": 501, "source_name": "零产出正常源", "reason": "零产出 → 建议停用"}
            ],
            "replace_sources": [],
            "add_sources": [],
            "keep_sources": [],
        }
        record = self._stage_source_probe_suggestion(payload, probe)
        self.assertEqual(record["status"], "rejected")
        self.assertIn("判定为 verified 但按当前阈值复算未达标", record["reason"])
        self.assertIn("#501", record["reason"])
        self.assertIn("不构成停用/替换依据", record["reason"])

    def test_source_gate_rejects_when_probe_evidence_missing(self):
        """探测记录缺失 → rejected（信源类建议不能只凭机械规则判 verified）。"""
        payload = {
            "disable_sources": [{"source_id": 501, "reason": "零产出"}],
            "replace_sources": [], "add_sources": [], "keep_sources": [],
        }
        metrics = {
            "before": {}, "after": {},
            "source_evidence": {"actionable_count": 1},
            "self_test": {
                "source": m.SELF_TEST_SOURCE, "kind": "source",
                "pack_id": "demo_pack", "passed": True,
            },
        }
        record = m.stage_suggestion(
            "demo_pack", "source", payload, metrics, status="verified"
        )
        self.assertEqual(record["status"], "rejected")
        self.assertIn("缺少信源探测记录", record["reason"])

    def test_source_gate_rejects_forged_probe_generated_by(self):
        """探测记录来源标记被伪造（generated_by 不是 probe_sources）→ rejected。"""
        forged = self._source_probe_fixture(
            [{"source_id": 501, "verdict": m.PROBE_VERDICT_DEAD, "http_status": 404,
              "reachable": False, "parsed_items": 0, "error": "HTTP 404"}]
        )
        forged["generated_by"] = "manual_probe_20261010"
        payload = {
            "disable_sources": [{"source_id": 501, "reason": "URL 失效"}],
            "replace_sources": [], "add_sources": [], "keep_sources": [],
        }
        record = self._stage_source_probe_suggestion(payload, forged)
        self.assertEqual(record["status"], "rejected")
        self.assertIn("来源不可信", record["reason"])

    def test_source_gate_rejects_when_probe_reached_nothing(self):
        """抓取全失败（一个源都没探到）→ rejected，理由含"未取得探测样本"。"""
        all_failed = self._source_probe_fixture(
            [
                {"source_id": 501, "verdict": m.PROBE_VERDICT_DEAD, "http_status": None,
                 "reachable": False, "parsed_items": 0, "error": "ConnectTimeout"},
                {"source_id": 502, "verdict": m.PROBE_VERDICT_DEAD, "http_status": None,
                 "reachable": False, "parsed_items": 0, "error": "Name or service not known"},
            ],
            healthy_id=None,
        )
        payload = {
            "disable_sources": [
                {"source_id": 501, "reason": "URL 失效"},
                {"source_id": 502, "reason": "URL 失效"},
            ],
            "replace_sources": [], "add_sources": [], "keep_sources": [],
        }
        record = self._stage_source_probe_suggestion(payload, all_failed)
        self.assertEqual(record["status"], "rejected")
        self.assertIn("未取得探测样本，无法验证", record["reason"])

        # 一条记录都没有（探针整个没跑成）同样必须被拦住
        empty = self._source_probe_fixture([], healthy_id=None)
        empty["sources"] = []
        record = self._stage_source_probe_suggestion(payload, empty)
        self.assertEqual(record["status"], "rejected")
        self.assertIn("未取得探测样本，无法验证", record["reason"])

        # 探测覆盖不全（#502 没有探测记录）也必须 rejected
        partial = self._source_probe_fixture(
            [{"source_id": 501, "verdict": m.PROBE_VERDICT_DEAD, "http_status": 404,
              "reachable": False, "parsed_items": 0, "error": "HTTP 404"}]
        )
        record = self._stage_source_probe_suggestion(payload, partial)
        self.assertEqual(record["status"], "rejected")
        self.assertIn("#502", record["reason"])
        self.assertIn("探测覆盖不全", record["reason"])

    def test_source_gate_allows_verified_when_probe_proves_dead(self):
        """探测证明坏 → 允许 verified；同时"页面不可解析"的源不许进停用桶。"""
        probe = self._source_probe_fixture(
            [
                {"source_id": 501, "verdict": m.PROBE_VERDICT_DEAD, "http_status": 404,
                 "reachable": False, "parsed_items": 0, "error": "HTTP 404"},
                {"source_id": 502, "verdict": m.PROBE_VERDICT_BLOCKED, "http_status": 403,
                 "reachable": False, "parsed_items": 0, "error": "HTTP 403"},
                {"source_id": 503, "verdict": m.PROBE_VERDICT_EMPTY_FEED, "http_status": 200,
                 "reachable": True, "parsed_items": 0},
                {"source_id": 504, "verdict": m.PROBE_VERDICT_UNPARSABLE, "http_status": 200,
                 "reachable": True, "parsed_items": 0},
            ]
        )
        payload = {
            "disable_sources": [
                {"source_id": 501, "reason": "URL 失效"},
                {"source_id": 502, "reason": "需登录或反爬"},
                {"source_id": 503, "reason": "空 feed"},
            ],
            "replace_sources": [], "add_sources": [],
            "keep_sources": [{"source_id": 504, "reason": "页面不可解析，需人工确认"}],
        }
        record = self._stage_source_probe_suggestion(payload, probe)
        self.assertEqual(record["status"], "verified")

        # 把"页面不可解析"的源也塞进停用桶 → 立刻 reject（口径：它不算坏源证据）
        bad_payload = copy.deepcopy(payload)
        bad_payload["disable_sources"].append({"source_id": 504, "reason": "解析不出条目"})
        record = self._stage_source_probe_suggestion(bad_payload, probe)
        self.assertEqual(record["status"], "rejected")
        self.assertIn("页面不可解析", record["reason"])

    def test_source_suggestion_keeps_zero_output_source_when_probe_is_healthy(self):
        """引擎侧回归：零产出 + 探测可用 → 进 keep_sources + notes，不进 disable_sources。"""
        zero, low, idle = self._seed_source_mix()
        probe_rows = [
            {"source_id": zero, "source_name": "零产出源", "listing_status": "ok",
             "listing_error": "", "fetched": 3, "parsed": 0, "parse_failed": 0,
             "articles": [], "verdict": "零产出"},
            {"source_id": low, "source_name": "低准入源", "listing_status": "ok",
             "listing_error": "", "fetched": 1, "parsed": 0, "parse_failed": 1,
             "articles": [], "verdict": "准入率低"},
            {"source_id": idle, "source_name": "停更源", "listing_status": "ok",
             "listing_error": "", "fetched": 1, "parsed": 0, "parse_failed": 0,
             "articles": [], "verdict": "长期无新文"},
        ]

        result = m.run_self_test_and_stage(
            "demo_pack",
            sample_limit=50,
            probe_runner=self._probe_stub(probe_rows),
            # 三个源都探测得到内容：谁都不该被停用/替换
            source_probe_runner=self._source_probe_stub(
                [
                    {"source_id": zero, "verdict": m.PROBE_VERDICT_OK, "http_status": 200,
                     "reachable": True, "parsed_items": 21},
                    {"source_id": low, "verdict": m.PROBE_VERDICT_OK, "http_status": 200,
                     "reachable": True, "parsed_items": 9},
                    {"source_id": idle, "verdict": m.PROBE_VERDICT_OK, "http_status": 200,
                     "reachable": True, "parsed_items": 4},
                ]
            ),
        )

        suggestion = result["source_suggestion"]
        payload = suggestion["payload"]
        self.assertEqual(payload["disable_sources"], [])
        self.assertEqual(payload["replace_sources"], [])
        self.assertEqual(payload["unverified_sources"], [])
        self.assertEqual(
            sorted(item["source_id"] for item in payload["keep_sources"]),
            sorted([zero, low, idle]),
        )
        zero_item = next(
            item for item in payload["keep_sources"] if item["source_id"] == zero
        )
        self.assertIn("零产出不能作为停用理由", zero_item["reason"])
        self.assertIn("需先确认调度/映射是否正常", zero_item["reason"])
        self.assertIn("历史入库产出 0 篇", zero_item["reason"])
        self.assertTrue(
            any("零产出，需先确认调度/映射是否正常" in note for note in payload["notes"])
        )
        # 没有可执行项 → 不可 verified；探测记录必须随证据落库，供闸门复算
        self.assertEqual(suggestion["status"], "rejected")
        self.assertIn("没有可执行的信源调整项", suggestion["reason"])
        self.assertEqual(
            suggestion["evidence"]["source_probe"]["generated_by"], "probe_sources"
        )

    def test_source_suggestion_marks_unprobed_source_as_unverified(self):
        """没有探测记录的源（探针没覆盖）→ unverified，不许进停用桶。"""
        zero, low, idle = self._seed_source_mix()
        probe_rows = [
            {"source_id": zero, "source_name": "零产出源", "listing_status": "ok",
             "listing_error": "", "fetched": 3, "parsed": 0, "parse_failed": 0,
             "articles": [], "verdict": "零产出"},
            {"source_id": low, "source_name": "低准入源", "listing_status": "ok",
             "listing_error": "", "fetched": 1, "parsed": 0, "parse_failed": 1,
             "articles": [], "verdict": "准入率低"},
            {"source_id": idle, "source_name": "停更源", "listing_status": "ok",
             "listing_error": "", "fetched": 1, "parsed": 0, "parse_failed": 0,
             "articles": [], "verdict": "长期无新文"},
        ]
        result = m.run_self_test_and_stage(
            "demo_pack",
            sample_limit=50,
            probe_runner=self._probe_stub(probe_rows),
            # 探针只覆盖 low：zero/idle 没有探测记录 → 一律 unverified
            source_probe_runner=self._source_probe_stub(
                [
                    {"source_id": low, "verdict": m.PROBE_VERDICT_OK, "http_status": 200,
                     "reachable": True, "parsed_items": 9},
                ]
            ),
        )
        payload = result["source_suggestion"]["payload"]
        self.assertEqual(payload["disable_sources"], [])
        self.assertEqual(payload["replace_sources"], [])
        self.assertEqual(
            sorted(item["source_id"] for item in payload["unverified_sources"]),
            sorted([zero, idle]),
        )
        for item in payload["unverified_sources"]:
            self.assertIn("缺少探测记录", item["reason"])
            self.assertIn("无法确认该源状态", item["reason"])
        self.assertEqual(result["source_suggestion_status"], "rejected")
        self.assertIn("没有可执行", result["source_suggestion"]["reason"])


# ─────────────────── 挖词质量：去样板 / 跨域名多样性 / 导航短语（真机夹具回归） ───────────────────
# 真机（A 机 invest_mgmt，sample_limit=100、5 个域名）实测：top 候选词全是 wallstreetcn.com 页脚
# 「本文来自华尔街见闻，欢迎下载APP查看更多」的碎片——华尔街(33) 华尔街见闻(25) 见闻(25)
# app查看(24) 来自华尔街(24) 欢迎app(24)。这六个词必须**全部被剔除**（硬剔除，不是降权）。
_WALLSTREET_FOOTER = "本文来自华尔街见闻，欢迎下载APP查看更多"
_NOISE_TERMS = ("华尔街", "华尔街见闻", "见闻", "app查看", "来自华尔街", "欢迎app")
# 真行业词（跨域名复现）——回归用：过滤不能把好词一起杀掉。
_INDUSTRY_TERMS = ("私募股权", "基金备案")


def _quality_pack_docs(footer_on_own_line: bool = False):
    """两域名夹具语料（各 10 篇）：A 站（wallstreetcn.com）每篇都带同一句真机页脚。

    * 页脚里的噪声词在 A 站 100% 复现、在 B 站 0% 出现 → 必须剔除；
    * ``私募股权`` / ``基金备案`` 在 A、B 两站各 5 篇出现（跨域名复现）→ 必须保留；
    * ``独角兽`` 只在 A 站 4 篇出现（跨域名多样性不足，但没到站点复现率门槛）→ 必须剔除。

    ``footer_on_own_line=True`` 时页脚独占一行（10/10 复现 → 走"去样板"整行删除）；
    False 时把页脚拼在正文句尾（每篇的行都不一样 → 只能靠站点级复现率判据剔除）。
    """
    docs = []
    for index in range(10):
        industry = "私募股权与基金备案的安排在本期均有进展，" if index < 5 else ""
        extra = "独角兽相关内容见前述观察记录。" if index < 4 else ""
        body = f"{industry}第 {index} 期观察：某机构在项目流转环节的操作路径出现变化。{extra}"
        content = f"{body}\n{_WALLSTREET_FOOTER}" if footer_on_own_line else f"{body}{_WALLSTREET_FOOTER}"
        docs.append(
            {
                "article_id": 500 + index,
                "title": f"A 站观察第 {index} 期",
                "domain": "wallstreetcn.com",
                "content": content,
            }
        )
    for index in range(10):
        industry = "私募股权与基金备案的制度安排同步细化，" if index < 5 else ""
        docs.append(
            {
                "article_id": 600 + index,
                "title": f"B 站记录第 {index} 期",
                "domain": "www.tmtpost.com",
                "content": f"{industry}B 站第 {index} 期记录：机构投资者结构保持稳定，未见异常。",
            }
        )
    return docs


def _quality_peer_docs():
    """对照语料：其它包已准入文章，刻意不含任何夹具词（保证判别力比值够高）。"""
    return [
        {
            "article_id": 700 + index,
            "title": f"对照文章 {index}",
            "domain": "peer.example.com",
            "content": "精细化工资产并购完成交割，交割流程已全部走完，投资人结构保持稳定。",
        }
        for index in range(4)
    ]


class IntelPackKeywordQualityTests(unittest.TestCase):
    """挖词质量三件套（① 去样板 ② 跨域名多样性 ③ 导航短语）的真机噪声词回归。

    隔离性与主用例一致：临时 sqlite + 假包加载器，**不连真库、不联网、不调模型、不碰 GPU**；
    语料直接用 ``_pack_docs`` / ``_peer_docs`` 注入，不写任何生产配置。
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "keyword_quality.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertEqual(
            getattr(self.db, "backend", "sqlite"),
            "sqlite",
            "测试必须跑在隔离的临时 SQLite 上（检查 DATABASE_TYPE/SQLITE_BACKUP_PATH）",
        )
        self.assertTrue(self.db.create_tables())
        m.use_database(self.db)
        m.use_pack_loader(
            _FakeLoader(
                {
                    "demo_pack": _demo_pack(),
                    "gate_free_pack": _gate_free_pack(),
                    "peer_pack": _peer_pack(),
                },
                database=self.db,
            )
        )

    def tearDown(self):
        m.use_pack_loader(ORIGINAL_LOADER)
        m.use_database(ORIGINAL_REPOSITORY)
        self.db.disconnect()
        self.temp_dir.cleanup()

    def _mine(self, pack_docs, peer_docs=None, top_n: int = 20):
        return m.mine_keyword_candidates(
            "demo_pack",
            top_n=top_n,
            sample_limit=100,
            _pack_docs=pack_docs,
            _peer_docs=_quality_peer_docs() if peer_docs is None else peer_docs,
        )

    @staticmethod
    def _excluded_reasons(result) -> dict:
        return {str(item.get("term")): str(item.get("reason") or "") for item in result["excluded"]}

    @staticmethod
    def _tokens_of(docs) -> set:
        """按**挖词链路同一口径**取 token（含行级去样板），否则会拿未清洗的文本做断言。"""
        templates = m._domain_template_lines(docs)
        tokens = set()
        for row in docs:
            text = m._mine_doc_text(
                row, templates.get(str(row.get("domain") or "")) or frozenset()
            )
            tokens.update(m._tokenize(text))
        return tokens

    # ── ① 真机噪声词夹具：这批词必须全部被剔除 ──
    def test_machine_noise_terms_are_all_dropped(self):
        docs = _quality_pack_docs()
        result = self._mine(docs)
        keywords = set(result["keywords"])
        reasons = self._excluded_reasons(result)
        tokens = self._tokens_of(docs)

        # 夹具自身必须真的能切出噪声词（否则这条用例是空转）
        produced = {term for term in _NOISE_TERMS if term in tokens}
        self.assertTrue(produced, f"夹具没切出任何噪声词：{sorted(tokens)[:20]}")

        for term in _NOISE_TERMS:
            self.assertNotIn(term, keywords, f"真机站点样板词仍被推荐进候选：{term}")
            self.assertNotIn(term, self._all_candidate_terms(result), term)
        # 切出来的噪声词必须留下"被剔除"的痕迹与理由（硬剔除，不是降权）
        for term in sorted(produced):
            self.assertIn(term, reasons, f"{term} 没有出现在 excluded 里")
            self.assertTrue(
                "样板" in reasons[term] or "导航" in reasons[term],
                f"{term} 的剔除理由不对：{reasons[term]}",
            )
        self.assertGreaterEqual(result["keyword_filter"]["site_boilerplate_dropped"], 1)
        self.assertGreaterEqual(result["keyword_filter"]["nav_phrase_dropped"], 1)
        self.assertTrue(any("站点样板词" in warn for warn in result["warnings"]))
        # 判别力三条门槛仍在（回归：没被新规则挤掉）
        self.assertEqual(result["discrimination"]["min_ratio"], m.DISCRIMINATION_MIN_RATIO)
        self.assertEqual(result["discrimination"]["max_peer_rate"], m.DISCRIMINATION_MAX_PEER_RATE)
        self.assertEqual(result["discrimination"]["min_pack_docs"], m.DISCRIMINATION_MIN_DOCS)

    def _all_candidate_terms(self, result) -> set:
        candidates = result.get("candidates") or {}
        terms = set()
        for bucket in ("core_keywords", "entity_keywords", "anchors"):
            terms.update(str(item.get("term")) for item in (candidates.get(bucket) or []))
        return terms

    # ── ② 去样板：同一段页脚混进多篇文章 → 切词前就被删掉 ──
    def test_repeated_footer_lines_are_stripped_before_counting(self):
        docs = _quality_pack_docs(footer_on_own_line=True)
        result = self._mine(docs)
        reasons = self._excluded_reasons(result)

        self.assertGreaterEqual(result["keyword_filter"]["template_line_count"], 1)
        self.assertIn("wallstreetcn.com", result["keyword_filter"]["template_line_domains"])
        self.assertTrue(any("去样板" in warn for warn in result["warnings"]))
        for term in ("华尔街", "见闻", "华尔街见闻"):
            self.assertNotIn(term, self._all_candidate_terms(result), term)
            # 整行在切词**之前**就被删掉 → 连"被剔除"的痕迹都不该有（这些词根本没进过词频统计）
            self.assertNotIn(term, reasons, f"{term} 是切词后才剔除的，去样板没生效")
        # 去样板不能把正文里的真行业词一起删掉
        self.assertIn("私募股权", self._keywords_for_tokenizer(result), result["keywords"])

    def _keywords_for_tokenizer(self, result):
        """按当前分词器给出回归断言用的关键词集合。

        jieba 在 requirements.txt 里（生产就是它）；万一环境缺 jieba，_tokenize 会退化成
        "中文 2/3-gram"，切不出「私募股权」这种四字短语，只能断言它的词片段。
        """
        if m._tokenizer_name() == "jieba":
            return set(result["keywords"])
        return set(result["keywords"]) | {"私募", "股权", "基金", "备案"}

    # ── ③ 跨域名多样性：只在 A 站出现的词剔除；A、B 都出现的词保留 ──
    def test_cross_domain_diversity_drops_single_domain_terms(self):
        docs = _quality_pack_docs()
        result = self._mine(docs)
        keywords = set(result["keywords"])
        reasons = self._excluded_reasons(result)
        tokens = self._tokens_of(docs)

        self.assertTrue(result["discrimination"]["cross_domain_enabled"])
        self.assertGreaterEqual(result["keyword_filter"]["cross_domain_dropped"], 1)
        # 「独角兽」只在 A 站 4 篇出现（4/10=40% 够不到站点复现率 60%）→ 只能靠跨域名判据剔除
        self.assertIn("独角兽", tokens)
        self.assertNotIn("独角兽", keywords)
        self.assertIn("独角兽", reasons)
        self.assertIn("跨域名多样性", reasons["独角兽"])
        # 保留下来的一定跨域名（不变量）
        for item in result["candidates"]["anchors"]:
            self.assertGreaterEqual(item["domains"], m.CROSS_DOMAIN_MIN_DOMAINS, item["term"])

    def test_real_industry_terms_across_domains_survive(self):
        result = self._mine(_quality_pack_docs())
        keywords = set(result["keywords"])
        self.assertTrue(keywords, "过滤过猛：一个候选词都没剩下")
        if m._tokenizer_name() == "jieba":
            for term in _INDUSTRY_TERMS:
                self.assertIn(term, keywords, f"真行业词被误杀：{term}")
        else:  # pragma: no cover - 无 jieba 环境只切词片段
            for term in ("私募", "基金", "备案"):
                self.assertIn(term, keywords, f"真行业词被误杀：{term}")
        # 真行业词是跨域名复现的
        selected = {str(item["term"]): item for item in result["candidates"]["anchors"]}
        for term in _INDUSTRY_TERMS:
            if term in selected:
                self.assertGreaterEqual(selected[term]["domains"], 2)
                self.assertEqual(selected[term]["peer_rate"], 0.0)

    # ── ④ 词形/导航短语过滤（与分词器无关的直接断言） ──
    def test_nav_phrase_filter_drops_navigation_and_keeps_industry_words(self):
        for term in (
            "app查看", "欢迎app", "点击查看", "阅读原文", "扫码关注", "微信扫码", "来自华尔街",
            "免责声明", "版权所有", "钛媒体app", "下载app", "点击下载", "查看更多", "关注我们",
            "用户协议", "来源华尔街见闻", "某某公众号",
        ):
            self.assertTrue(m._nav_phrase_reason(term), f"导航式短语未被识别：{term}")
        for term in _INDUSTRY_TERMS + (
            "AI芯片", "Pre-IPO", "REITs基金", "点击率", "下载量", "独角兽", "固态电池", "中试线",
        ):
            self.assertEqual(m._nav_phrase_reason(term), "", f"真行业词被误判成导航短语：{term}")

    # ── ⑤ 站点级复现率判据（用 A 机真实域名分布做夹具） ──
    def test_site_boilerplate_reason_matches_real_machine_distribution(self):
        # A 机 invest_mgmt 实测分布（100 篇 other）：wallstreetcn 31 / tmtpost 40 / cyzone 25 /
        # qbitai 3 / zhidx 1
        domain_docs = {
            "wallstreetcn.com": 31, "www.tmtpost.com": 40, "www.cyzone.cn": 25,
            "www.qbitai.com": 3, "zhidx.com": 1,
        }
        for term, per_domain in (
            ("华尔街见闻", {"wallstreetcn.com": 25}),
            ("见闻", {"wallstreetcn.com": 25}),
            ("app查看", {"wallstreetcn.com": 24}),
            ("来自华尔街", {"wallstreetcn.com": 24}),
            ("华尔街", {"wallstreetcn.com": 25, "www.tmtpost.com": 4, "www.cyzone.cn": 4}),
        ):
            reason = m._site_boilerplate_reason(term, per_domain, domain_docs, 100)
            self.assertIn("站点样板词", reason, f"{term} 没被判成站点样板：{reason}")
        for term, per_domain in (
            ("私募股权", {"wallstreetcn.com": 12, "www.tmtpost.com": 14, "www.cyzone.cn": 9}),
            ("基金备案", {"wallstreetcn.com": 4, "www.tmtpost.com": 5}),
        ):
            self.assertEqual(
                m._site_boilerplate_reason(term, per_domain, domain_docs, 100), "",
                f"真行业词被误判成站点样板：{term}",
            )
        # 域名文档数 < SITE_BOILERPLATE_MIN_DOCS（样本太小）不参与判定
        self.assertEqual(
            m._site_boilerplate_reason("某冷门词", {"www.qbitai.com": 3}, domain_docs, 100), ""
        )
        # 单一域名语料：门槛收紧到 SOLO_RATE，60%~80% 之间不算样板
        solo = {"only.example.com": 10}
        self.assertEqual(m._site_boilerplate_reason("某词", {"only.example.com": 7}, solo, 10), "")
        self.assertIn(
            "站点样板词",
            m._site_boilerplate_reason("某词", {"only.example.com": 8}, solo, 10),
        )

    # ── ⑥ 机构名分桶：core_keywords 与 entity_keywords 互斥 ──
    def test_candidate_buckets_are_disjoint_entity_names_go_to_entity_bucket(self):
        result = self._mine(_quality_pack_docs())
        candidates = result["candidates"]
        core = {str(item["term"]) for item in candidates["core_keywords"]}
        entity = {str(item["term"]) for item in candidates["entity_keywords"]}
        self.assertEqual(core & entity, set(), "同一个词不能同时进 core_keywords 与 entity_keywords")
        self.assertEqual(
            {str(item["term"]) for item in candidates["anchors"]}, core | entity,
            "anchors 必须是 core ∪ entity",
        )
        for item in candidates["entity_keywords"]:
            self.assertEqual(item["bucket"], "entity_keywords")
            self.assertTrue(m._is_entity_term(item["term"]))
        for item in candidates["core_keywords"]:
            self.assertEqual(item["bucket"], "core_keywords")
            self.assertFalse(m._is_entity_term(item["term"]))
        # 机构名启发式本身（机构/组织后缀 + ≥4 字）
        self.assertTrue(m._is_entity_term("中国证券投资基金业协会"))
        self.assertTrue(m._is_entity_term("上海证券交易所"))
        self.assertFalse(m._is_entity_term("交易所"))  # 命中后缀但只有 3 字 → 按概念词走 core
        self.assertFalse(m._is_entity_term("私募股权"))


if __name__ == "__main__":
    unittest.main()
