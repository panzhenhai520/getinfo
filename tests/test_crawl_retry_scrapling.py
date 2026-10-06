import contextlib
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
        self._session_guard = os.environ.get("CRAWL_SCRAPLING_SESSION_ENABLED")
        # 本类用例锁定的是"每次新建 StealthyFetcher"的旧路径（patch 的是 StealthyFetcher），
        # 关掉会话复用才能保证确定性、且不去真启浏览器
        os.environ["CRAWL_SCRAPLING_SESSION_ENABLED"] = "0"

    def tearDown(self):
        if self._env_guard is None:
            os.environ.pop("CRAWL_SCRAPLING_TIER_ENABLED", None)
        else:
            os.environ["CRAWL_SCRAPLING_TIER_ENABLED"] = self._env_guard
        if self._session_guard is None:
            os.environ.pop("CRAWL_SCRAPLING_SESSION_ENABLED", None)
        else:
            os.environ["CRAWL_SCRAPLING_SESSION_ENABLED"] = self._session_guard

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


class ScraplingSessionReuseTests(unittest.TestCase):
    """阶段5 优化：Scrapling 会话复用（同线程复用浏览器）+ 会话异常时的降级回退"""

    ANCHOR_HTML = (
        "<html><body><p>网络安全行业年度报告正文，包含大量内容。" + "内容" * 300 + "</p></body></html>"
    )
    ANCHOR_MD = "## 网络安全行业年度报告\n\n" + "清洗后的 Markdown 正文。" * 30

    def setUp(self):
        import candidate_crawler_adapter as cca
        self.cca = cca
        self.adapter = cca.CandidateCrawlerAdapter()
        self.pack = {"candidate_gate": {"anchor_keywords": ["网络安全"], "entity_keywords": []}}
        self._env_guard = {
            name: os.environ.get(name)
            for name in ("CRAWL_SCRAPLING_TIER_ENABLED", "CRAWL_SCRAPLING_SESSION_ENABLED")
        }
        os.environ["CRAWL_SCRAPLING_TIER_ENABLED"] = "1"
        os.environ["CRAWL_SCRAPLING_SESSION_ENABLED"] = "1"
        # 用例之间不能共享会话（会话按线程缓存，pytest 主线程复用同一个）
        cca._drop_thread_scrapling_session()
        self._stats_guard = dict(cca._SCRAPLING_SESSION_STATS)

    def tearDown(self):
        self.cca._drop_thread_scrapling_session()
        self.cca._SCRAPLING_SESSION_STATS.clear()
        self.cca._SCRAPLING_SESSION_STATS.update(self._stats_guard)
        for name, value in self._env_guard.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    @staticmethod
    def _fake_page(html, markdown="", status=200, with_markdown=True):
        """构造 Scrapling Response 的最小替身（只用到 html_content/status/markdown）。"""
        class _Page:
            pass

        page = _Page()
        page.html_content = html
        page.status = status
        if with_markdown:
            page.markdown = lambda css_selector=None, main_content_only=False: markdown
        return page

    @staticmethod
    def _fake_session(page=None, error=None):
        """构造 StealthySession 的最小替身。"""
        class _Session:
            def __init__(self):
                self._is_alive = True
                self.closed = False
                self.fetch_calls = 0
                self.page = page
                self.error = error

            def start(self):
                return None

            def fetch(self, url, **kwargs):
                self.fetch_calls += 1
                if self.error is not None:
                    raise self.error
                return self.page

            def close(self):
                self.closed = True
                self._is_alive = False

        return _Session()

    def _patch_common(self, stack):
        stack.enter_context(patch("candidate_crawler_adapter.industry_pack_loader.load", return_value=self.pack))
        stack.enter_context(patch("candidate_crawler_adapter.quick_score_candidate",
                                  return_value={"anchor_hits": ["网络安全"], "score": 10}))
        stack.enter_context(patch("candidate_crawler_adapter.sqlite_db.record_crawl_attempt", return_value=None))

    def test_session_is_created_once_and_reused_across_calls(self):
        """同线程多次兜底抓取只建一个会话（浏览器只冷启动一次）。"""
        session = self._fake_session(page=self._fake_page(self.ANCHOR_HTML, self.ANCHOR_MD))
        created = []

        def _factory(**kwargs):
            created.append(kwargs)
            return session

        with contextlib.ExitStack() as stack:
            self._patch_common(stack)
            stack.enter_context(patch("scrapling.fetchers.StealthySession", _factory))
            first = self.adapter._scrapling_fallback(
                "https://x.example/a", title="t", pack_id="family_office", candidate_id=1)
            second = self.adapter._scrapling_fallback(
                "https://x.example/b", title="t", pack_id="family_office", candidate_id=2)
        self.assertTrue(first.get("success"))
        self.assertTrue(second.get("success"))
        self.assertEqual(len(created), 1, "同一线程应复用同一个会话")
        self.assertEqual(session.fetch_calls, 2)
        self.assertEqual(self.cca._SCRAPLING_SESSION_STATS["created"], 1)
        self.assertEqual(self.cca._SCRAPLING_SESSION_STATS["reused"], 1)

    def test_markdown_main_content_used_as_article_content(self):
        """Scrapling 路径优先用 markdown(main_content_only=True) 的结果当正文。"""
        session = self._fake_session(page=self._fake_page(self.ANCHOR_HTML, self.ANCHOR_MD))
        with contextlib.ExitStack() as stack:
            self._patch_common(stack)
            stack.enter_context(patch("scrapling.fetchers.StealthySession", lambda **kw: session))
            result = self.adapter._scrapling_fallback(
                "https://x.example/a", title="t", pack_id="family_office", candidate_id=1)
        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("content"), self.ANCHOR_MD.strip())
        self.assertEqual(result.get("source_method"), "scrapling_fetch")
        self.assertEqual(result.get("extraction_method"), "scrapling_fetch")

    def test_falls_back_to_plain_text_when_markdown_unavailable(self):
        """取不到 markdown（无该方法/返回空）时退回原有纯文本抽取。"""
        session = self._fake_session(
            page=self._fake_page(self.ANCHOR_HTML, "", with_markdown=False))
        with contextlib.ExitStack() as stack:
            self._patch_common(stack)
            stack.enter_context(patch("scrapling.fetchers.StealthySession", lambda **kw: session))
            result = self.adapter._scrapling_fallback(
                "https://x.example/a", title="t", pack_id="family_office", candidate_id=1)
        self.assertTrue(result.get("success"))
        self.assertIn("网络安全", result.get("content") or "")
        self.assertNotIn("##", result.get("content") or "")

    def test_markdown_exception_falls_back_to_plain_text(self):
        """markdown() 抛异常也不能让兜底失效，退回纯文本。"""
        session = self._fake_session(page=self._fake_page(self.ANCHOR_HTML, self.ANCHOR_MD))

        def _boom(css_selector=None, main_content_only=False):
            raise RuntimeError("markdownify 崩了")

        session.page.markdown = _boom
        with contextlib.ExitStack() as stack:
            self._patch_common(stack)
            stack.enter_context(patch("scrapling.fetchers.StealthySession", lambda **kw: session))
            result = self.adapter._scrapling_fallback(
                "https://x.example/a", title="t", pack_id="family_office", candidate_id=1)
        self.assertTrue(result.get("success"))
        self.assertIn("网络安全", result.get("content") or "")

    def test_session_create_failure_degrades_to_one_shot_fetcher(self):
        """会话建不起来 → 回退到每次新建 StealthyFetcher，梯队不能整体失效。"""
        page = self._fake_page(self.ANCHOR_HTML, self.ANCHOR_MD)
        fetcher_calls = []

        class FakeFetcher:
            def fetch(self, url, **kwargs):
                fetcher_calls.append(url)
                return page

        with contextlib.ExitStack() as stack:
            self._patch_common(stack)
            stack.enter_context(patch("scrapling.fetchers.StealthySession",
                                      side_effect=RuntimeError("patchright 启动失败")))
            stack.enter_context(patch("scrapling.fetchers.StealthyFetcher", return_value=FakeFetcher()))
            result = self.adapter._scrapling_fallback(
                "https://x.example/a", title="t", pack_id="family_office", candidate_id=1)
        self.assertTrue(result.get("success"))
        self.assertEqual(fetcher_calls, ["https://x.example/a"])
        self.assertEqual(self.cca._SCRAPLING_SESSION_STATS["create_failed"], 1)

    def test_broken_session_is_dropped_and_falls_back_to_one_shot(self):
        """会话判定损坏（浏览器被关闭）→ 先释放会话，再回退每次新建实例并成功。"""
        page = self._fake_page(self.ANCHOR_HTML, self.ANCHOR_MD)
        session = self._fake_session(
            error=RuntimeError("Target page, context or browser has been closed"))

        class FakeFetcher:
            def fetch(self, url, **kwargs):
                return page

        with contextlib.ExitStack() as stack:
            self._patch_common(stack)
            stack.enter_context(patch("scrapling.fetchers.StealthySession", lambda **kw: session))
            stack.enter_context(patch("scrapling.fetchers.StealthyFetcher", return_value=FakeFetcher()))
            result = self.adapter._scrapling_fallback(
                "https://x.example/a", title="t", pack_id="family_office", candidate_id=1)
        self.assertTrue(result.get("success"))
        self.assertTrue(session.closed, "坏会话必须先释放，否则新建实例会与它冲突")
        self.assertEqual(self.cca._SCRAPLING_SESSION_STATS["session_broken"], 1)
        self.assertIsNone(getattr(self.cca._SCRAPLING_SESSION_LOCAL, "session", None))

    def test_page_level_failure_keeps_session_and_skips_duplicate_fetch(self):
        """坏域名（DNS 失败）→ 会话被保留，且不重复新建实例。

        依据实测：同一线程里会话还活着时再 StealthyFetcher.fetch() 必然抛
        "It looks like you are using Playwright Sync API inside the asyncio loop"，
        重复抓取只会拿到误导性错误并白付一次浏览器冷启动。
        """
        session = self._fake_session(error=RuntimeError("Page.goto: net::ERR_NAME_NOT_RESOLVED"))
        fetcher_calls = []

        class FakeFetcher:
            def fetch(self, url, **kwargs):
                fetcher_calls.append(url)
                return None

        with contextlib.ExitStack() as stack:
            self._patch_common(stack)
            stack.enter_context(patch("scrapling.fetchers.StealthySession", lambda **kw: session))
            stack.enter_context(patch("scrapling.fetchers.StealthyFetcher", return_value=FakeFetcher()))
            result = self.adapter._scrapling_fallback(
                "https://bad.invalid/x", title="t", pack_id="family_office", candidate_id=1)
        self.assertFalse(result.get("success"))
        self.assertIn("Scrapling 抓取失败", result.get("error") or "")
        self.assertEqual(fetcher_calls, [], "页面级失败不该重复新建实例")
        self.assertFalse(session.closed, "页面级失败不该丢弃仍然健康的会话")
        self.assertEqual(session.fetch_calls, 1)

    def test_repeated_page_level_failures_eventually_rebuild_session(self):
        """连续失败的兜底网：达到阈值后丢弃会话，下次重建，避免卡死在未知损坏的会话上。"""
        session = self._fake_session(error=RuntimeError("未知异常 XYZ"))

        with contextlib.ExitStack() as stack:
            self._patch_common(stack)
            stack.enter_context(patch("scrapling.fetchers.StealthySession", lambda **kw: session))
            for cid in range(1, 4):
                result = self.adapter._scrapling_fallback(
                    "https://x.example/bad", title="t", pack_id="family_office", candidate_id=cid)
                self.assertFalse(result.get("success"))
        self.assertTrue(session.closed, "连续失败到阈值应丢弃会话")
        self.assertIsNone(getattr(self.cca._SCRAPLING_SESSION_LOCAL, "session", None))

    def test_noisy_markdown_is_rejected_in_favor_of_plain_text(self):
        """markdown 明显比纯文本臃肿（导航残留）时改用纯文本，避免把噪声带进 RAG。"""
        noisy_md = "## 导航\n\n" + "* [栏目](https://x.example/a)\n" * 200
        session = self._fake_session(page=self._fake_page(self.ANCHOR_HTML, noisy_md))
        with contextlib.ExitStack() as stack:
            self._patch_common(stack)
            stack.enter_context(patch("scrapling.fetchers.StealthySession", lambda **kw: session))
            result = self.adapter._scrapling_fallback(
                "https://x.example/a", title="t", pack_id="family_office", candidate_id=1)
        self.assertTrue(result.get("success"))
        self.assertNotIn("[栏目]", result.get("content") or "")
        self.assertIn("网络安全", result.get("content") or "")

    def test_ratio_env_can_force_markdown_priority(self):
        """CRAWL_SCRAPLING_MARKDOWN_MAX_RATIO 调大 → 回到"永远优先 markdown"。"""
        noisy_md = "## 导航\n\n" + "网络安全栏目正文。" * 200
        self._env_guard["CRAWL_SCRAPLING_MARKDOWN_MAX_RATIO"] = os.environ.get(
            "CRAWL_SCRAPLING_MARKDOWN_MAX_RATIO")
        os.environ["CRAWL_SCRAPLING_MARKDOWN_MAX_RATIO"] = "99"
        try:
            session = self._fake_session(page=self._fake_page(self.ANCHOR_HTML, noisy_md))
            with contextlib.ExitStack() as stack:
                self._patch_common(stack)
                stack.enter_context(patch("scrapling.fetchers.StealthySession", lambda **kw: session))
                result = self.adapter._scrapling_fallback(
                    "https://x.example/a", title="t", pack_id="family_office", candidate_id=1)
        finally:
            if self._env_guard["CRAWL_SCRAPLING_MARKDOWN_MAX_RATIO"] is None:
                os.environ.pop("CRAWL_SCRAPLING_MARKDOWN_MAX_RATIO", None)
            else:
                os.environ["CRAWL_SCRAPLING_MARKDOWN_MAX_RATIO"] = \
                    self._env_guard["CRAWL_SCRAPLING_MARKDOWN_MAX_RATIO"]
        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("content"), noisy_md.strip())

    def test_session_reuse_disabled_by_env_uses_legacy_path(self):
        """CRAWL_SCRAPLING_SESSION_ENABLED=0 → 完全回到旧的每次新建路径。"""
        os.environ["CRAWL_SCRAPLING_SESSION_ENABLED"] = "0"
        page = self._fake_page(self.ANCHOR_HTML, self.ANCHOR_MD)

        class FakeFetcher:
            def fetch(self, url, **kwargs):
                return page

        created = []
        with contextlib.ExitStack() as stack:
            self._patch_common(stack)
            stack.enter_context(patch("scrapling.fetchers.StealthySession",
                                      lambda **kw: created.append(kw) or self._fake_session(page)))
            stack.enter_context(patch("scrapling.fetchers.StealthyFetcher", return_value=FakeFetcher()))
            result = self.adapter._scrapling_fallback(
                "https://x.example/a", title="t", pack_id="family_office", candidate_id=1)
        self.assertTrue(result.get("success"))
        self.assertEqual(created, [], "开关关闭时不应创建会话")


if __name__ == "__main__":
    unittest.main()
