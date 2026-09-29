import os
import unittest
from unittest.mock import patch

from scheduler import TaskScheduler


class CrawlRetryClassificationTests(unittest.TestCase):
    """阶段5：采集层分类重试（Crawlee 策略）—— 403/404/410 不重试、429/5xx/超时退避重试"""

    def setUp(self):
        self.scheduler = TaskScheduler()
        self.scheduler.retry_backoff_seconds = 20
        self.scheduler.retry_jitter_seconds = 10
        self.scheduler.retry_backoff_max_seconds = 600

    def _classify(self, message):
        return self.scheduler._classify_retry(Exception(message))

    def test_permanent_http_statuses_do_not_retry(self):
        for status in (400, 401, 403, 404, 410, 422):
            should_retry, kind = self._classify(f"聚合失败 HTTP {status} Forbidden")
            self.assertFalse(should_retry, f"HTTP {status} 不应重试")
            self.assertIn(str(status), kind)

    def test_backoff_http_statuses_retry_with_classification(self):
        for status in (408, 425, 429, 500, 502, 503, 504):
            should_retry, kind = self._classify(f"HTTP {status} Service Unavailable")
            self.assertTrue(should_retry, f"HTTP {status} 应重试")
            self.assertTrue(kind.endswith("_backoff"), kind)

    def test_timeout_and_connection_errors_retry_as_backoff(self):
        for message in ("读取超时", "Read timed out", "Connection reset by peer", "连接被重置"):
            should_retry, kind = self._classify(message)
            self.assertTrue(should_retry, message)
            self.assertEqual(kind, "network_backoff")

    def test_config_and_stop_errors_never_retry(self):
        for message in ("任务被用户停止", "任务执行超时", "无法获取目标URL", "不支持的任务类型"):
            should_retry, kind = self._classify(message)
            self.assertFalse(should_retry, message)
            self.assertEqual(kind, "config")

    def test_unknown_errors_keep_legacy_retry_semantics(self):
        should_retry, kind = self._classify("未知的聚合异常 XYZ")
        self.assertTrue(should_retry)
        self.assertEqual(kind, "unknown")

    def test_exponential_backoff_with_jitter(self):
        with patch("random.uniform", return_value=0.0):
            self.assertAlmostEqual(self.scheduler._retry_sleep_seconds(1, "http_429_backoff"), 20)
            self.assertAlmostEqual(self.scheduler._retry_sleep_seconds(2, "http_500_backoff"), 40)
            self.assertAlmostEqual(self.scheduler._retry_sleep_seconds(3, "network_backoff"), 80)
            # 抖动范围：[base, base+jitter]
            with patch("random.uniform", return_value=10.0):
                self.assertAlmostEqual(self.scheduler._retry_sleep_seconds(2, "http_503_backoff"), 50)

    def test_backoff_capped_at_max(self):
        self.scheduler.retry_backoff_max_seconds = 50
        with patch("random.uniform", return_value=0.0):
            self.assertEqual(self.scheduler._retry_sleep_seconds(4, "http_429_backoff"), 50)

    def test_non_backoff_kinds_keep_linear_schedule(self):
        with patch("random.uniform", return_value=0.0):
            self.assertEqual(self.scheduler._retry_sleep_seconds(2, "unknown"), 40)
            self.assertEqual(self.scheduler._retry_sleep_seconds(3, "unknown"), 60)


class CrawlCheckpointResumeTests(unittest.TestCase):
    """阶段5：任务级断点续跑 —— 同一运行内已成功的 URL 重试时跳过"""

    def setUp(self):
        self.scheduler = TaskScheduler()

    def test_succeeded_urls_skipped_on_same_run_retry(self):
        calls = []

        def fake_exec(task, target_url, execution_id, get_stop_flag, crawl_task_id):
            calls.append(target_url)
            if target_url == "http://fail.example/":
                return {"success": False, "error": "x", "message": "x", "articles_found": 0}
            return {"success": True, "articles_found": 1, "message": "ok"}

        self.scheduler._execute_crawl_task = fake_exec
        self.scheduler._execute_crawl4ai_fallback_if_needed = lambda *a, **k: None
        targets = ["http://ok.example/", "http://fail.example/"]
        first = self.scheduler._execute_crawl_target_queue({"id": 1}, targets, "exec-1", None, None)
        second = self.scheduler._execute_crawl_target_queue({"id": 1}, targets, "exec-1", None, None)
        # 第一次执行两个 URL；第二次 ok 被跳过、只重爬 fail
        self.assertEqual(calls, ["http://ok.example/", "http://fail.example/", "http://fail.example/"])
        ok_result = [t for t in second.get("target_results", []) if t["target_url"] == "http://ok.example/"]
        self.assertTrue(ok_result and ok_result[0].get("skipped") is True)
        self.assertEqual(first.get("articles_found"), 1)

    def test_different_run_keys_do_not_share_progress(self):
        calls = []

        def fake_exec(task, target_url, execution_id, get_stop_flag, crawl_task_id):
            calls.append((execution_id, target_url))
            return {"success": True, "articles_found": 1, "message": "ok"}

        self.scheduler._execute_crawl_task = fake_exec
        self.scheduler._execute_crawl4ai_fallback_if_needed = lambda *a, **k: None
        targets = ["http://ok.example/"]
        self.scheduler._execute_crawl_target_queue({"id": 1}, targets, "exec-1", None, None)
        self.scheduler._execute_crawl_target_queue({"id": 1}, targets, "exec-2", None, None)
        self.assertEqual(len(calls), 2)


class ScraplingTierTests(unittest.TestCase):
    """阶段5：Scrapling 隐身抓取本地第一梯队（VPN 之前、失败静默降级）"""

    def setUp(self):
        from candidate_crawler_adapter import CandidateCrawlerAdapter
        self.adapter = CandidateCrawlerAdapter()
        self.pack = {"candidate_gate": {"anchor_keywords": ["网络安全"], "entity_keywords": []}}
        self._env_guard = os.environ.get("CRAWL_SCRAPLING_TIER_ENABLED")

    def tearDown(self):
        if self._env_guard is None:
            os.environ.pop("CRAWL_SCRAPLING_TIER_ENABLED", None)
        else:
            os.environ["CRAWL_SCRAPLING_TIER_ENABLED"] = self._env_guard

    def test_html_to_validated_content_requires_anchor_hits(self):
        html = "<html><body><p>网络安全行业年度报告正文，包含大量内容。" + "内容" * 300 + "</p></body></html>"
        with patch("candidate_crawler_adapter.quick_score_candidate",
                   return_value={"anchor_hits": ["网络安全"], "score": 10}):
            result = self.adapter._html_to_validated_content(
                "https://x.example/report", title="安全报告", html=html, pack=self.pack, label="test"
            )
        self.assertTrue(result.get("success"))
        self.assertIn("网络安全", result.get("content") or "")

    def test_html_without_anchor_is_rejected(self):
        html = "<html><body><p>完全无关的内容。" + "无关" * 300 + "</p></body></html>"
        with patch("candidate_crawler_adapter.quick_score_candidate",
                   return_value={"anchor_hits": [], "score": 0}):
            result = self.adapter._html_to_validated_content(
                "https://x.example/report", title="无关报告", html=html, pack=self.pack, label="test"
            )
        self.assertFalse(result.get("success"))
        self.assertIn("锚点", result.get("error") or "")

    def test_scrapling_tier_disabled_by_env(self):
        os.environ["CRAWL_SCRAPLING_TIER_ENABLED"] = "0"
        result = self.adapter._scrapling_fallback(
            "https://x.example/report", title="t", pack_id="family_office", candidate_id=1
        )
        self.assertFalse(result.get("success"))
        self.assertIn("配置关闭", result.get("error") or "")

    def test_scrapling_success_path_uses_shared_validation(self):
        os.environ["CRAWL_SCRAPLING_TIER_ENABLED"] = "1"
        html = "<html><body><p>网络安全行业年度报告正文。" + "内容" * 300 + "</p></body></html>"

        class FakePage:
            html_content = html
            status = 200

        class FakeFetcher:
            def fetch(self, url, **kwargs):
                return FakePage()

        with patch("candidate_crawler_adapter.industry_pack_loader.load", return_value=self.pack), \
             patch("candidate_crawler_adapter.quick_score_candidate", return_value={"anchor_hits": ["网络安全"], "score": 10}), \
             patch("candidate_crawler_adapter.sqlite_db.record_crawl_attempt", return_value=None), \
             patch("scrapling.fetchers.StealthyFetcher", return_value=FakeFetcher()):
            result = self.adapter._scrapling_fallback(
                "https://x.example/report", title="t", pack_id="family_office", candidate_id=1, task_id="t1"
            )
        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("source_method"), "scrapling_fetch")
        self.assertIn("网络安全", result.get("content") or "")

    def test_scrapling_failure_degrades_silently(self):
        os.environ["CRAWL_SCRAPLING_TIER_ENABLED"] = "1"

        class BadFetcher:
            def fetch(self, url, **kwargs):
                raise RuntimeError("TLS 指纹被拒")

        with patch("candidate_crawler_adapter.industry_pack_loader.load", return_value=self.pack), \
             patch("candidate_crawler_adapter.sqlite_db.record_crawl_attempt", return_value=None), \
             patch("scrapling.fetchers.StealthyFetcher", return_value=BadFetcher()):
            result = self.adapter._scrapling_fallback(
                "https://x.example/report", title="t", pack_id="family_office", candidate_id=1, task_id="t1"
            )
        self.assertFalse(result.get("success"))
        self.assertIn("Scrapling 抓取失败", result.get("error") or "")


if __name__ == "__main__":
    unittest.main()
