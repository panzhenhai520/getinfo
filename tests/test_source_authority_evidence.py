#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import tempfile
import unittest

_BOOTSTRAP = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(_BOOTSTRAP.name, "bootstrap.sqlite3")
os.environ["INTEL_LLM_ENABLED"] = "false"
os.environ["CRAWL_REQUIRE_KEYWORD_MATCH"] = "false"

import config
from industry_packs import IndustryPackError, IndustryPackLoader, validate_industry_pack
from intel_classifier import IntelClassificationService, classify_article
from intel_database import IntelRepository
from intel_evidence import IntelEvidenceService, extract_comparable_claims
from intel_sources import IntelSourceRegistry
from source_authority import (
    SourceAuthorityError,
    resolve_source_authority,
    source_authority_profiles,
)
from sqlite_database import SQLiteDatabase


class SourceAuthorityEvidenceTests(unittest.TestCase):
    def setUp(self):
        self._keyword_guard = config.CRAWL_REQUIRE_KEYWORD_MATCH
        config.CRAWL_REQUIRE_KEYWORD_MATCH = False
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "evidence.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        self.db.analyze_article_spacetime_profile = lambda _article_id: None
        self.repo = IntelRepository(self.db)
        self.loader = IndustryPackLoader()
        self.classifier = IntelClassificationService(self.repo, self.loader)
        self.evidence = IntelEvidenceService(self.db)

    def tearDown(self):
        config.CRAWL_REQUIRE_KEYWORD_MATCH = self._keyword_guard
        self.db.disconnect()
        self.temp_dir.cleanup()

    def _source(self, name, url, role, level, publisher_key=""):
        profile = resolve_source_authority(
            {
                "url": url,
                "source_role": role,
                "authority_level": level,
                "publisher_key": publisher_key,
            },
            strict=True,
        )
        metadata = {
            "source_role": profile["source_role"],
            "authority_scope": profile["authority_scope"],
            "publisher_key": profile["publisher_key"],
        }
        cursor = self.db.connection.execute(
            """
            INSERT INTO intel_sources(
                canonical_source_url,source_url,source_name,source_type,
                content_type,authority_level,metadata_json
            ) VALUES(?,?,?,'list_page','official',?,?)
            """,
            (url, url, name, level, json.dumps(metadata, ensure_ascii=False)),
        )
        self.db.connection.commit()
        return int(cursor.lastrowid)

    def _article(self, title, url, source_id):
        article_id = self.db.insert_article(
            {
                "url": url,
                "title": title,
                "content": (
                    f"{title}。香港家族办公室政策与行业统计信息，"
                    "用于来源权威与证据冲突判定的回归验证，正文长度需满足入库闸门要求。"
                ),
                "publish_date": "2026-08-05",
                "matched_keywords": ["家族办公室", "政策", "统计"],
            }
        )
        candidate = self.db.connection.execute(
            """
            INSERT INTO intel_candidates(
                canonical_url,original_url,title,normalized_title,status,article_id
            ) VALUES(?,?,?,?,'crawled',?)
            """,
            (url, url, title, title.casefold(), article_id),
        )
        candidate_id = int(candidate.lastrowid)
        self.db.connection.execute(
            """
            INSERT INTO intel_candidate_observations(
                candidate_id,source_id,observation_type,observation_key,raw_url,title
            ) VALUES(?,?,'list_page',?,?,?)
            """,
            (candidate_id, source_id, f"obs-{candidate_id}", url, title),
        )
        self.db.connection.commit()
        self.classifier.classify_article_id(article_id, "family_office")
        return int(article_id)

    def test_global_profiles_and_manifest_validation_are_generic(self):
        profiles = source_authority_profiles()
        self.assertEqual(profiles["government_regulator"]["weight"], 5)
        self.assertFalse(profiles["industry_association"]["can_resolve_conflicts"])
        self.assertEqual(profiles["professional_trade_media"]["weight"], 2)
        with self.assertRaises(SourceAuthorityError):
            resolve_source_authority(
                {"url": "https://example.com", "source_role": "invented"},
                strict=True,
            )

        pack = self.loader.load("family_office")
        custom = dict(pack)
        custom["default_sources"] = [
            {
                "name": "监管来源",
                "url": "https://regulator.example/news",
                "source_type": "list_page",
                "source_role": "government_regulator",
            }
        ]
        validated = validate_industry_pack(custom, expected_id="family_office")
        source = validated["default_sources"][0]
        self.assertEqual(source["authority_level"], 5)
        self.assertEqual(source["publisher_key"], "regulator.example")
        custom["default_sources"][0]["source_role"] = "invented"
        with self.assertRaises(IndustryPackError):
            validate_industry_pack(custom, expected_id="family_office")

        source_id = self._source(
            "待调整来源", "https://role-change.example/news", "unclassified", 1
        )
        updated = IntelSourceRegistry(self.db).update_source(
            source_id, source_role="industry_association"
        )
        self.assertEqual(updated["authority_level"], 4)
        self.assertEqual(updated["metadata"]["source_role"], "industry_association")
        self.assertFalse(
            resolve_source_authority(
                {
                    "url": updated["source_url"],
                    "source_role": updated["metadata"]["source_role"],
                    "authority_level": updated["authority_level"],
                }
            )["can_resolve_conflicts"]
        )

    def test_authority_never_changes_industry_admission(self):
        result = classify_article(
            {
                "title": "普通餐厅推出夏季菜单",
                "content": "来源为政府网站，但没有任何目标行业锚点。",
                "authority_level": 5,
                "source_role": "government_regulator",
            },
            self.loader.load("family_office"),
        )
        self.assertEqual(result["final_category"], "other")
        # "通用行业过滤器"短路分支固定返回 hits={}（产品自身消费者一律按
        # (score_details.get("hits") or {}).get("anchor") or [] 读取），
        # 这里沿用同一口径，只断言"没有任何锚点命中"。
        self.assertEqual(
            (result["score_details"].get("hits") or {}).get("anchor") or [], []
        )
        # 同上：短路分支的 score_details 没有 components 键，按"没有该评分维度"读取。
        self.assertNotIn("authority", (result["score_details"].get("components") or {}))

    def test_official_scope_can_prefer_but_never_hide_conflict(self):
        official = self._source(
            "监管机构", "https://regulator.example/news", "government_regulator", 5
        )
        association = self._source(
            "行业协会", "https://association.example/news", "industry_association", 4
        )
        official_article = self._article(
            "香港家族办公室数量增长20%",
            "https://regulator.example/news/20",
            official,
        )
        association_article = self._article(
            "香港家族办公室数量增长30%",
            "https://association.example/news/30",
            association,
        )
        stats = self.evidence.rebuild("family_office")
        self.assertEqual(stats["group_count"], 1)
        self.assertEqual(stats["conflict_count"], 1)
        mapped = self.evidence.evidence_for_articles(
            [official_article, association_article], industry_pack_id="family_office"
        )
        group = mapped[official_article]
        self.assertEqual(group["conflict_status"], "authoritative_preferred")
        self.assertEqual(group["representative_article_id"], official_article)
        self.assertEqual(group["independent_source_count"], 2)
        self.assertEqual(group["evidence_grade"], "A")
        self.assertEqual(group["citations"][0]["source_role"], "government_regulator")
        self.assertTrue(group["conflict_details"])

        visible, total, _window = self.repo.list_classified_articles(
            industry_pack_id="family_office", time_range="730d"
        )
        self.assertEqual(total, 1)
        self.assertEqual(visible[0]["article_id"], official_article)
        self.assertEqual(len(visible[0]["source_evidence"]["citations"]), 2)

    def test_association_cannot_turn_its_position_into_regulatory_fact(self):
        association = self._source(
            "行业协会", "https://association.example/a", "industry_association", 4
        )
        media = self._source(
            "专业媒体",
            "https://trade.example/a",
            "professional_trade_media",
            2,
        )
        first = self._article(
            "香港家族办公室数量增长20%",
            "https://association.example/a/20",
            association,
        )
        second = self._article(
            "香港家族办公室数量增长30%",
            "https://trade.example/a/30",
            media,
        )
        self.evidence.rebuild("family_office")
        group = self.evidence.evidence_for_articles(
            [first, second], industry_pack_id="family_office"
        )[first]
        self.assertEqual(group["conflict_status"], "unresolved")
        self.assertEqual(group["evidence_grade"], "CONFLICT")
        self.assertEqual(group["base_evidence_grade"], "A")

    def test_same_publisher_is_counted_once(self):
        first_source = self._source(
            "研究机构主站",
            "https://research.example/news",
            "independent_research",
            4,
            publisher_key="research-group",
        )
        second_source = self._source(
            "研究机构镜像",
            "https://mirror.example/news",
            "independent_research",
            4,
            publisher_key="research-group",
        )
        first = self._article(
            "香港家族办公室数量增长20%",
            "https://research.example/news/20",
            first_source,
        )
        second = self._article(
            "香港家族办公室数量增长20%",
            "https://mirror.example/news/20",
            second_source,
        )
        self.evidence.rebuild("family_office")
        group = self.evidence.evidence_for_articles(
            [first, second], industry_pack_id="family_office"
        )[first]
        self.assertEqual(group["independent_source_count"], 1)
        self.assertEqual(len(group["citations"]), 1)

    def test_direction_claim_extraction_avoids_substring_double_count(self):
        approved = extract_comparable_claims("香港家族办公室政策获批准")
        rejected = extract_comparable_claims("香港家族办公室政策未通过")
        self.assertEqual(
            [item["value"] for item in approved if item["kind"] == "direction"],
            ["approved"],
        )
        self.assertEqual(
            [item["value"] for item in rejected if item["kind"] == "direction"],
            ["rejected"],
        )
        self.assertEqual(approved[-1]["key"], rejected[-1]["key"])

    def test_distinct_numeric_metrics_in_one_title_are_not_a_conflict(self):
        claims = extract_comparable_claims(
            "拓普集团涨1.23%，成交额12.33亿元，后市是否有机会？"
        )
        numeric = [item for item in claims if item["kind"] == "numeric"]
        self.assertEqual([item["value"] for item in numeric], ["1.23%", "12.33亿元"])
        self.assertEqual(len({item["key"] for item in numeric}), 2)


if __name__ == "__main__":
    unittest.main()
