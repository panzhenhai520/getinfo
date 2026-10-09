#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""各 handler 并发幂等性复核（阶段 12 待办 4）。

背景（A 机生产只读实测，2026-10-09 03:40Z）
  * core lane 已从"批次串行"改成 `INTEL_WORKER_JOB_CONCURRENCY=8` 的批次并发，
    作业级隔离由 `tests/test_worker_job_concurrency.py` 覆盖；
  * 但"**同一条作业被两个槽位/两个 worker 同时处理**"这一场景没有用例。生产上这条路径真实存在：
    看门狗超时先把作业置为可重试（原线程还在跑）、租约过期被别的 worker 重新领取、人工重跑，
    都会让同一条作业的 handler 被执行两次；
  * 队列里最老等待 60.4 小时的 `topic_cluster`、以及刚排空的 `classification` 都走这条路径，
    一旦不幂等就会重复入队 / 重复写副作用 / 计数翻倍，把积压重新滚起来。

本文件用**隔离的临时 SQLite 库**（连到共享 PG 主库会直接失败）验证三件事：
  1. 作业级结果一致：两次执行产出同一份结果（含同一个 `classification_id`）；
  2. 计数不重复累加：终态只写一次、worker 统计只加一次、维护类作业第二次计数为 0；
  3. 不产生重复副作用：唯一约束生效（分类行不翻倍）、入队去重键生效（不重复入队）、
     终态清理不重复删。

覆盖 handler：`classification`（真实分类链路）、`candidate_rescore`（无副作用维护类）、
`task_cleanup`（终态记录清理维护类）。
"""
import os
import tempfile
import threading
import time
import unittest

import config

# 必须在导入数据库层之前指定本地临时库（config 在导入时就把 DATABASE_PATH 读成常量）
_BOOTSTRAP_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(_BOOTSTRAP_TEMP_DIR.name, "idempotency.sqlite3")

from industry_packs import IndustryPackLoader  # noqa: E402
from intel_classifier import IntelClassificationService  # noqa: E402
from intel_database import IntelRepository  # noqa: E402
from intel_worker import IntelWorker  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

PACK_ID = "family_office"
# 入库闸门会判废"有效正文 < 40 字"的近空内容，夹具正文刻意写足并带上真实行业词
ARTICLE_TITLE = "香港家族办公室税务宽免新规落地"
ARTICLE_BODY = (
    "香港特区政府公布家族办公室税务宽免新规，跨境财富管理与家族信托架构安排同步放宽。"
    "政策明确家族办公室在港投资可享利得税豁免，信托架构与财富传承规划纳入适用范围。"
    "业内认为该政策将推动更多联合家族办公室落户香港，家族治理与传承规划需求随之上升。"
)


class _FakeHeartbeat:
    """`_process_claimed_job` 收尾会调用 heartbeat.join，用空实现顶替真实心跳线程。"""

    def join(self, timeout=None):
        return None


def _bare_worker(repository, handlers):
    """用 `__new__` 造一个只带作业执行所需属性的 worker（沿用并发用例的写法）。"""
    worker = IntelWorker.__new__(IntelWorker)
    worker.repository = repository
    worker.worker_id = "idempotency-bare"
    worker.job_lease_seconds = 60
    worker.heartbeat_seconds = 0.05
    worker.job_hard_timeout_seconds = 60
    worker.stop_requested = False
    worker.handlers = dict(handlers)
    worker._context_handler_types = frozenset()
    worker._active_cancel_events = {}
    worker._active_job_lock = threading.Lock()
    worker._job_context = lambda job: type(
        "Ctx", (), {"cancel_event": threading.Event(), "job_type": job["job_type"]}
    )()
    worker._release_job_context = lambda job_id: None
    worker._heartbeat_job = lambda job_id, cancel_event, stop_event: None
    worker.enqueue_due_periodic_jobs = lambda: None
    return worker


def _run_concurrently(callables):
    """同时起跑，最大化两次处理的重叠窗口；返回 (结果列表, 异常列表)。"""
    results, errors = [], []
    barrier = threading.Barrier(len(callables))
    lock = threading.Lock()

    def wrap(fn):
        def runner():
            try:
                barrier.wait(timeout=15)          # 两条线程尽量同一瞬间进入 handler
                value = fn()
                with lock:
                    results.append(value)
            except Exception as exc:              # noqa: BLE001 - 用例要如实暴露异常
                with lock:
                    errors.append(exc)
        return runner

    threads = [threading.Thread(target=wrap(fn)) for fn in callables]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results, errors


class WorkerJobIdempotencyTest(unittest.TestCase):
    """所有用例都在临时 SQLite 库里跑，绝不碰到真实作业与文章。"""

    def setUp(self):
        self._original_database_type = getattr(config, "DATABASE_TYPE", "sqlite")
        self._original_keyword_guard = getattr(config, "CRAWL_REQUIRE_KEYWORD_MATCH", True)
        # 临时切 sqlite（与 tests/test_job_starvation_fairness.py 同一套隔离手法）
        config.DATABASE_TYPE = "sqlite"
        config.CRAWL_REQUIRE_KEYWORD_MATCH = False
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "idempotency.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertEqual("sqlite", self.db.backend,
                         "必须使用临时 SQLite 库；连到共享主库会领取/污染真实作业")
        self.assertTrue(self.db.create_tables())
        self.db.analyze_article_spacetime_profile = lambda _article_id: None
        self.repo = IntelRepository(self.db)
        self.worker = IntelWorker(
            repository=self.repo,
            worker_id="idempotency-test",
            classification_service=IntelClassificationService(self.repo, IndustryPackLoader()),
        )

    def tearDown(self):
        try:
            self.db.disconnect()
        except Exception:
            pass
        config.DATABASE_TYPE = self._original_database_type
        config.CRAWL_REQUIRE_KEYWORD_MATCH = self._original_keyword_guard
        self.temp_dir.cleanup()

    # ---------------- 夹具 ----------------

    def _insert_article(self, url="https://example.com/idempotency/fo-1"):
        article_id = self.db.insert_article({
            "url": url,
            "title": ARTICLE_TITLE,
            "content": ARTICLE_BODY,
            "publish_date": "2026-10-09",
            "matched_keywords": [],
        })
        self.assertTrue(article_id, "夹具文章必须成功入库")
        return int(article_id)

    def _scalar(self, sql, params=()):
        with self.db.lock:
            row = self.db.connection.execute(sql, params).fetchone()
        return row[0] if row else None

    def _count(self, sql, params=()):
        return int(self._scalar(sql, params) or 0)

    def _classification_rows(self, article_id):
        return self._count(
            "SELECT COUNT(*) FROM article_intel_classifications "
            "WHERE article_id=? AND industry_pack_id=?", (int(article_id), PACK_ID))

    # ---------------- 1) classification：同一作业跑两次 ----------------

    def test_classification_double_run_returns_same_result_and_single_row(self):
        """顺序双跑：作业级结果逐项一致，分类行不翻倍。"""
        article_id = self._insert_article()
        payload = {"article_id": article_id, "industry_pack_id": PACK_ID}

        first = self.worker._handle_classification(dict(payload))
        second = self.worker._handle_classification(dict(payload))

        self.assertEqual(first, second, "同一作业两次执行必须产出同一份结果")
        self.assertEqual(1, self._classification_rows(article_id),
                         "唯一约束 (article_id, industry_pack_id) 必须让分类行保持 1 行")
        self.assertEqual(first.get("classification_id"), second.get("classification_id"),
                         "第二次执行必须复用同一行，而不是新插入一行")

    def test_classification_concurrent_double_processing_is_idempotent(self):
        """并发双跑：结果一致、分类行 1 行、且不产生重复的 dedupe_key 作业。"""
        article_id = self._insert_article()
        payload = {"article_id": article_id, "industry_pack_id": PACK_ID}

        results, errors = _run_concurrently([
            lambda: self.worker._handle_classification(dict(payload)),
            lambda: self.worker._handle_classification(dict(payload)),
        ])

        self.assertEqual([], errors, "并发执行不允许抛异常")
        self.assertEqual(2, len(results))
        self.assertEqual(results[0], results[1], "并发下两次执行的结果必须一致")
        self.assertEqual(1, self._classification_rows(article_id))
        self.assertEqual(0, self._count(
            "SELECT COUNT(*) FROM (SELECT dedupe_key FROM intel_jobs "
            "GROUP BY dedupe_key HAVING COUNT(*) > 1) AS dup"),
            "dedupe_key 必须全局唯一：并发双跑不得重复入队同一件事")

    def test_classification_concurrent_double_processing_enqueues_topic_cluster_once(self):
        """副作用的去重键：并发双跑只允许产生 1 条 topic_cluster 作业。"""
        original = config.INTEL_TOPIC_CLUSTER_ENABLED
        config.INTEL_TOPIC_CLUSTER_ENABLED = True
        try:
            article_id = self._insert_article()
            payload = {"article_id": article_id, "industry_pack_id": PACK_ID}
            # 前提校验：这篇文章真的会命中主题标签（否则本用例会退化成空断言）
            probe = self.worker._handle_classification(dict(payload))
            self.assertTrue(probe.get("topic_tags"),
                            "夹具文章必须命中行业包主题，否则验证不到入队去重")
            before = self._count("SELECT COUNT(*) FROM intel_jobs WHERE job_type='topic_cluster'")

            results, errors = _run_concurrently([
                lambda: self.worker._handle_classification(dict(payload)),
                lambda: self.worker._handle_classification(dict(payload)),
            ])

            self.assertEqual([], errors)
            after = self._count("SELECT COUNT(*) FROM intel_jobs WHERE job_type='topic_cluster'")
            self.assertEqual(before, after,
                             "并发双跑不得重复入队 topic_cluster（dedupe_key 必须生效）")
            expected_key = "topic-cluster:%s:%s:%s" % (
                PACK_ID, article_id, probe.get("article_content_hash") or "")
            self.assertEqual(1, self._count(
                "SELECT COUNT(*) FROM intel_jobs WHERE job_type='topic_cluster' AND dedupe_key=?",
                (expected_key,)),
                "同一 (包, 文章, content_hash) 只允许存在 1 条 topic_cluster 作业")
            self.assertEqual(1, self._classification_rows(article_id))
        finally:
            config.INTEL_TOPIC_CLUSTER_ENABLED = original

    def test_run_once_does_not_reprocess_a_completed_job(self):
        """批次级：同一条作业只能被领取一次，第二轮 run_once 不得重复累加。"""
        article_id = self._insert_article()

        first = self.worker.run_once(job_types=["classification"], limit=50,
                                     schedule_periodic=False)
        self.assertGreaterEqual(first["completed"], 1)
        rows_after_first = self._classification_rows(article_id)
        self.assertEqual(1, rows_after_first, "入库后自动入队的分类作业也只允许落 1 行")

        second = self.worker.run_once(job_types=["classification"], limit=50,
                                      schedule_periodic=False)
        self.assertEqual(0, second["claimed"], "已完成的作业不得被再次领取")
        self.assertEqual(0, second["completed"])
        self.assertEqual(rows_after_first, self._classification_rows(article_id),
                         "第二次执行不得重复累加分类行")
        self.assertEqual(0, self._count(
            "SELECT COUNT(*) FROM intel_jobs WHERE job_type='classification' "
            "AND status <> 'completed'"))
        self.assertEqual(0, self._count(
            "SELECT COUNT(*) FROM (SELECT dedupe_key FROM intel_jobs "
            "GROUP BY dedupe_key HAVING COUNT(*) > 1) AS dup"))

    # ---------------- 2) 作业级终态：只写一次、只计一次 ----------------

    def test_same_claimed_job_terminal_write_and_counter_happen_once(self):
        """同一条 running 作业被两个槽位执行：两个都跑了 handler，但终态只写一次、统计只加一次。"""
        job_id, _created = self.repo.enqueue_job(
            "classification", "idempotency-lease-race-1", {"article_id": 1}, priority=100)
        claimed = self.repo.claim_jobs("idempotency-bare", job_types=["classification"],
                                       limit=1, lease_seconds=300)
        self.assertEqual([int(job_id)], [int(job["id"]) for job in claimed])
        job = claimed[0]

        handled, lock = [], threading.Lock()

        def handler(payload):
            with lock:
                handled.append(payload.get("article_id"))
            time.sleep(0.2)          # 让两次处理的重叠窗口足够大
            return {"status": "completed", "handler_ran": True}

        worker = _bare_worker(self.repo, {"classification": handler})
        runtimes = {int(job_id): (
            type("Ctx", (), {"cancel_event": threading.Event(), "job_type": "classification"})(),
            threading.Event(),
            _FakeHeartbeat(),
        )}
        stats = {"claimed": 1, "completed": 0, "retry_wait": 0,
                 "failed": 0, "cancelled": 0, "lease_lost": 0}
        stats_lock = threading.Lock()

        def process():
            worker._process_claimed_job(job, runtimes, stats, stats_lock)

        _results, errors = _run_concurrently([process, process])
        self.assertEqual([], errors)

        self.assertEqual(2, len(handled), "前提：两个槽位都真的执行了 handler")
        self.assertEqual(1, stats["completed"], "终态只允许一次写成功")
        self.assertEqual(1, stats["lease_lost"], "抢输的那次必须记成 lease_lost，而不是再记一次完成")
        self.assertEqual(1, self._count(
            "SELECT COUNT(*) FROM intel_jobs WHERE id=? AND status='completed'", (job_id,)))
        self.assertEqual(1, self._count(
            "SELECT COUNT(*) FROM intel_jobs WHERE id=? AND result_json LIKE '%handler_ran%'",
            (job_id,)), "结果只允许被写入一次")

    # ---------------- 3) 维护类 handler ----------------

    def test_candidate_rescore_promotes_only_once(self):
        """无副作用维护类作业：第二次执行扫描 0 条、提升 0 条（计数不重复累加）。"""
        now = "2026-10-09T00:00:00Z"
        with self.db.lock:
            cursor = self.db.connection.cursor()
            cursor.execute(
                "INSERT INTO intel_candidates (canonical_url, original_url, title, summary, "
                "status, created_at, updated_at) VALUES (?,?,?,?, 'discovered', ?, ?)",
                ("https://example.com/cand/1", "https://example.com/cand/1",
                 "香港家族办公室税务宽免新规落地", "跨境财富管理与家族信托架构同步放宽。",
                 now, now))
            candidate_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO intel_candidate_industries (candidate_id, industry_pack_id, "
                "created_at, updated_at) VALUES (?,?,?,?)", (candidate_id, PACK_ID, now, now))
            self.db.connection.commit()
            cursor.close()

        first = self.worker._handle_candidate_rescore({"industry_pack_id": PACK_ID})
        self.assertEqual(1, int(first.get("promoted") or 0),
                         "命中行业锚点的 discovered 候选必须被提升为 queued")
        self.assertEqual("queued", self._scalar(
            "SELECT status FROM intel_candidates WHERE id=?", (candidate_id,)))

        second = self.worker._handle_candidate_rescore({"industry_pack_id": PACK_ID})
        self.assertEqual(0, int(second.get("promoted") or 0),
                         "第二次执行不得重复提升（计数不重复累加）")
        self.assertEqual(1, self._count("SELECT COUNT(*) FROM intel_candidates"),
                         "候选行不得被复制")
        self.assertEqual(1, self._count("SELECT COUNT(*) FROM intel_candidate_industries"))

    def test_task_cleanup_is_idempotent_and_spares_active_jobs(self):
        """终态清理：同一批记录只被删一次，且不碰排队/运行中的作业。"""
        terminal_ids = []
        for index in range(3):
            job_id, _created = self.repo.enqueue_job(
                "classification", "idempotency-cleanup-done-%d" % index, {"index": index})
            terminal_ids.append(int(job_id))
        queued_id, _created = self.repo.enqueue_job(
            "classification", "idempotency-cleanup-queued", {"index": 99})

        with self.db.lock:
            cursor = self.db.connection.cursor()
            cursor.execute(
                "UPDATE intel_jobs SET status='completed', updated_at='2020-01-01T00:00:00Z' "
                "WHERE id IN (%s)" % ",".join("?" for _ in terminal_ids), tuple(terminal_ids))
            self.db.connection.commit()
            cursor.close()

        first = self.worker._handle_task_cleanup({})
        self.assertTrue(first.get("success"))
        self.assertEqual(3, int(first.get("jobs_deleted") or 0))

        second = self.worker._handle_task_cleanup({})
        self.assertEqual(0, int(second.get("jobs_deleted") or 0),
                         "同一批记录第二次必须是 0 删除（幂等）")
        self.assertEqual(0, self._count(
            "SELECT COUNT(*) FROM intel_jobs WHERE id IN (%s)"
            % ",".join("?" for _ in terminal_ids), tuple(terminal_ids)))
        self.assertEqual(1, self._count(
            "SELECT COUNT(*) FROM intel_jobs WHERE id=?", (queued_id,)),
            "排队中的作业不得被清理")


if __name__ == "__main__":
    unittest.main()
