import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

config.DATABASE_TYPE = "sqlite"

from industry_packs import IndustryPackLoader  # noqa: E402
from intel_candidates import IntelCandidateRepository  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CandidateClaimPackScopeTests(unittest.TestCase):
    """派发按行业包限定范围：异包激活上下文不得拦截本包候选（汽车包断流回归）"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(str(Path(self.temp_dir.name) / "claim-scope.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        self.loader = IndustryPackLoader(
            str(PROJECT_ROOT / "config" / "industry_packs"), use_published_store=False
        )
        self.candidates = IntelCandidateRepository(self.db)

    def tearDown(self):
        self.db.disconnect()
        self.temp_dir.cleanup()

    def _discover(self, pack_id, url, title, published_at=None, observation_type="website", activation_id=""):
        return self.candidates.discover(
            {
                "url": url,
                "title": title,
                "summary": f"{title} 相关动态。",
                "published_at": published_at,
            },
            industry_pack_id=pack_id,
            activation_id=activation_id,
            observation_type=observation_type,
        )

    def test_pack_scoped_claim_ignores_foreign_activation(self):
        """回归：候选无激活会话，而派发任务被异包 activation 污染时，仍能派发到本包候选。"""
        fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        found = self._discover(
            "automotive_industry", "https://auto.example/a1", "智能驾驶新动态", published_at=fresh
        )
        self.assertTrue(found["should_queue"])
        claimed = self.candidates.claim_candidates(
            "worker-1",
            active_activation_id="32e8e33a7bac44aeae5eab459091a233",  # 异包激活
            industry_pack_id="automotive_industry",
        )
        self.assertEqual([item["id"] for item in claimed], [found["candidate_id"]])

    def test_pack_scoped_claim_excludes_other_pack(self):
        fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self._discover("healthcare_news", "https://health.example/h1", "医疗行业新政策", published_at=fresh)
        claimed = self.candidates.claim_candidates(
            "worker-2",
            active_activation_id="",
            industry_pack_id="automotive_industry",
        )
        self.assertEqual(claimed, [])

    def test_claim_without_pack_keeps_legacy_activation_filter(self):
        fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        found = self._discover(
            "healthcare_news", "https://health.example/h2", "医疗健康产业新事件",
            published_at=fresh, activation_id="activation-education",
        )
        self.assertTrue(found["should_queue"])
        # 不带 pack 参数：维持旧的纯 activation 过滤
        self.assertEqual(
            self.candidates.claim_candidates("w", active_activation_id="activation-other"),
            [],
        )
        self.assertEqual(
            len(self.candidates.claim_candidates("w", active_activation_id="activation-education")),
            1,
        )

    def test_website_candidate_without_date_is_queued(self):
        """回归：网站直采条目无日期不再被 freshness 一票否决（水位线已确认其为新链接）。"""
        found = self._discover(
            "automotive_industry", "https://auto.example/nodate", "智能驾驶新车型发布",
            published_at=None, observation_type="website",
        )
        self.assertTrue(found["should_queue"])
        self.assertFalse(found.get("freshness_rejected"))

    def test_website_candidate_with_stale_date_rejected(self):
        stale = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        found = self._discover(
            "automotive_industry", "https://auto.example/stale", "旧闻一篇",
            published_at=stale, observation_type="website",
        )
        self.assertFalse(found["should_queue"])
        self.assertTrue(found.get("freshness_rejected"))

    def test_deny_domain_candidates_are_skipped(self):
        with patch.object(config, "CRAWL_DENY_DOMAINS", ("cn.investing.com", "www.investing.com")):
            found = self._discover(
                "automotive_industry",
                "https://cn.investing.com/news/stock-market-news/article-123",
                "股市新闻头条",
                published_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                observation_type="website",
            )
        self.assertFalse(found["should_queue"])
        self.assertTrue(found.get("denied_domain"))
        self.assertIsNone(found["candidate_id"])

    def test_deny_domain_subdomains_matched_via_suffix(self):
        # 黑名单配根域 investing.com 时，子域 cn.investing.com 一并拒绝
        with patch.object(config, "CRAWL_DENY_DOMAINS", ("investing.com",)):
            found = self._discover(
                "automotive_industry",
                "https://cn.investing.com/news/1",
                "智能驾驶新车型发布",
                published_at=None,
                observation_type="website",
            )
            self.assertFalse(found["should_queue"])
            self.assertTrue(found.get("denied_domain"))

    def test_runtime_deny_domains_from_settings_are_enforced(self):
        """运行时黑名单（信源管理页维护）存在 intel_runtime_settings，worker 现读现用。"""
        self.db.connection.execute(
            "INSERT INTO intel_runtime_settings(setting_key, setting_value) "
            "VALUES('crawl_deny_domains', 'blocked.example, root.example')"
        )
        self.db.connection.commit()
        found = self._discover(
            "automotive_industry",
            "https://sub.blocked.example/news/1",
            "智能驾驶新车型发布",
            published_at=None,
            observation_type="website",
        )
        self.assertFalse(found["should_queue"])
        self.assertTrue(found.get("denied_domain"))

    def test_deny_domains_snapshot_merges_env_and_runtime(self):
        from intel_candidates import deny_domains_snapshot
        self.db.connection.execute(
            "INSERT INTO intel_runtime_settings(setting_key, setting_value) "
            "VALUES('crawl_deny_domains', 'runtime.example, cn.investing.com')"
        )
        self.db.connection.commit()
        with patch.object(config, "CRAWL_DENY_DOMAINS", ("env.example", "cn.investing.com")):
            snap = deny_domains_snapshot(self.db)
        self.assertEqual(snap["env_domains"], ["env.example", "cn.investing.com"])
        self.assertEqual(snap["runtime_domains"], ["runtime.example", "cn.investing.com"])
        self.assertEqual(
            snap["domains"], ["env.example", "cn.investing.com", "runtime.example"]
        )


if __name__ == "__main__":
    unittest.main()
