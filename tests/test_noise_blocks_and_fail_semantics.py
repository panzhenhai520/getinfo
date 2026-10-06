import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

config.DATABASE_TYPE = "sqlite"

from intel_worker import IntelWorker  # noqa: E402
from sqlite_database import clean_article_markdown, _dedup_title_lines  # noqa: E402
from content_handlers import _drop_short_line_blocks, build_article_markdown  # noqa: E402


class _FakeRepo:
    """最小仓库替身：只服务 run_once 的 claim/complete/fail/get 路径。"""

    def __init__(self, claim_result):
        self._claim = list(claim_result)
        self.completed = []
        self.failed = []
        self.db = None  # 构造 IntelWorker 时其他服务只引用，不真正访问

    def claim_jobs(self, worker_id, *, job_types=None, limit=None, lease_seconds=60,
                   starved_before=None):
        if starved_before:
            return []
        return self._claim

    def complete_job(self, job_id, result, *, lease_owner=""):
        self.completed.append((job_id, result))
        return True

    def fail_job(self, job_id, error, *, retry_delay_seconds=None, lease_owner="", retryable=True):
        self.failed.append((job_id, error, retryable))
        return "retry_wait"

    def get_job(self, job_id):
        return {"id": job_id, "status": "running"}


class WorkerFailSemanticsTests(unittest.TestCase):
    """worker 主循环：handler 返回 success=False 必须走失败重试，而不是误标 completed"""

    def _run_job(self, handler_result):
        fake = _FakeRepo([{"id": 1, "job_type": "test", "payload": {}, "dedupe_key": "k"}])
        worker = IntelWorker(repository=fake, active_composition_service=object())
        worker.handlers["test"] = lambda payload: handler_result
        stats = worker.run_once(job_types=["test"], limit=1, schedule_periodic=False)
        return fake, stats

    def test_success_false_goes_to_fail_job_not_completed(self):
        fake, _stats = self._run_job({"success": False, "error": "VPN 精炼失败"})
        self.assertEqual(fake.completed, [])
        self.assertEqual(len(fake.failed), 1)
        self.assertEqual(fake.failed[0][0], 1)
        self.assertIn("VPN 精炼失败", fake.failed[0][1])

    def test_success_false_retryable_flag_respected(self):
        fake, _stats = self._run_job({"success": False, "error": "x", "retryable": False})
        self.assertEqual(fake.failed[0][2], False)

    def test_success_true_still_completes(self):
        fake, stats = self._run_job({"success": True, "article_id": 1})
        self.assertEqual(len(fake.completed), 1)
        self.assertEqual(stats["completed"], 1)
        self.assertEqual(fake.failed, [])

    def test_result_without_success_key_completes(self):
        fake, stats = self._run_job({"skipped": True, "reason": "classification disabled"})
        self.assertEqual(len(fake.completed), 1)
        self.assertEqual(stats["completed"], 1)


class ShortLineBlockTests(unittest.TestCase):
    """连续超短行块（导航菜单/行情碎片）整块删除"""

    NAV = "\n".join(["新浪首页", "新闻", "体育", "财经", "娱乐", "科技", "博客", "图片", "专栏"]) + "\n\n正文第一段内容足够长。"

    def test_nav_block_dropped_keeps_body(self):
        out = _drop_short_line_blocks(self.NAV)
        self.assertNotIn("新浪首页", out)
        self.assertNotIn("博客", out)
        self.assertIn("正文第一段内容足够长", out)

    def test_short_block_below_min_run_kept(self):
        text = "一\n二\n三\n\n正文段落内容足够长不会被删除。"
        out = _drop_short_line_blocks(text)
        self.assertIn("一", out)

    def test_stock_quote_fragment_dropped(self):
        text = "聚焦人形\n机器人\n(\n514.980\n,\n11.72\n,\n2.33%\n)\n的核心本质——工业生产力落地。"
        out = _drop_short_line_blocks(text)
        self.assertNotIn("514.980", out)
        self.assertIn("核心本质——工业生产力落地", out)

    def test_blank_line_separated_short_run_dropped(self):
        text = "新浪首页\n\n新闻\n\n体育\n\n财经\n\n娱乐\n\n科技\n\n博客\n\n图片\n\n正文段落内容足够长不会被删除。"
        out = _drop_short_line_blocks(text)
        self.assertNotIn("新浪首页", out)
        self.assertIn("正文段落内容足够长", out)

    def test_clean_article_markdown_applies_short_block(self):
        out = clean_article_markdown(self.NAV)
        self.assertNotIn("新浪首页", out)
        self.assertIn("正文第一段内容足够长", out)

    def test_build_markdown_applies_short_block(self):
        raw = "新浪首页\n新闻\n体育\n财经\n娱乐\n科技\n博客\n图片\n\n宇树科技发布新一代人形机器人，强调工业场景落地。"
        out = build_article_markdown(raw, raw)
        self.assertNotIn("新浪首页", out)
        self.assertIn("人形机器人", out)


class PlainTitleDedupTests(unittest.TestCase):
    """正文中与标题完全相同的纯文本行（非 heading）删除"""

    TITLE = "宇树科技产业突围：告别参数内卷，人形机器人从展演走向真实生产力"

    def test_plain_duplicate_lines_removed(self):
        md = f"{self.TITLE}\n{self.TITLE}\n\n正文第一段。"
        out = _dedup_title_lines(md, self.TITLE)
        self.assertNotIn(self.TITLE, out)
        self.assertIn("正文第一段", out)

    def test_heading_and_plain_mixed_removed(self):
        md = f"# {self.TITLE}\n{self.TITLE}\n\n正文。"
        out = _dedup_title_lines(md, self.TITLE)
        self.assertNotIn(self.TITLE, out)
        self.assertIn("正文。", out)

    def test_short_identical_line_kept(self):
        title = "融资完成"
        md = "融资完成\n\n公司完成新一轮融资。"
        out = _dedup_title_lines(md, title)
        self.assertIn("融资完成", out)


if __name__ == "__main__":
    unittest.main()
