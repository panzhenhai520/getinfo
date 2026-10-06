#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作业优先级语义回归测试。

生产实测背景：`INTEL_JOB_PRIORITY_AGING_SECONDS=30` 时，任何等待超过约 1 分钟的作业
折算出的积分都会盖过任意优先级设置 → 优先级形同虚设、退化成纯 FIFO，
分类作业被 31 小时未处理的 trend_aggregate/topic_cluster 长期插队
（14 小时只完成 11 个分类作业，按此速率排空要 6700+ 小时）。

这里钉住"优先级必须真的起作用"这条性质：
  1. 等待积分有上限，不能无限盖过优先级；
  2. 分类的优先级让它在**排队时一定先于维护类作业**被领取；
  3. 队列里没有分类作业时，维护类作业照常能被领取。

⚠️ 后续修正（2026-10-06，A 机实测）：第 1 条的上限一旦封顶，"高优先级类型持续到货"就会让
低优先级类型**永远赢不了**——topic_cluster/trend_aggregate/embed_articles 积压最久 32 小时
且 attempt_count=0。因此防饿死不能靠调这个上限，而是靠 worker 的饥饿保留名额
（见 tests/test_job_starvation_fairness.py）。这里补充第 4 条性质：优先级通道的排序键
本身确实会让低优先级类型输给新鲜高优先级作业（这正是需要保留名额的原因）。
"""
import os
import unittest

import config
from intel_database import _CLASSIFICATION_JOB_PRIORITY

# 各维护类作业的优先级（取自生产实测的分布）
MAINTENANCE_PRIORITIES = {
    "trend_aggregate": -45,
    "topic_cluster": -40,
    "candidate_dispatch": -30,
    "embed_articles": -30,
    "light_scan": -20,
    "enrich": -10,
    "candidate_rescore": 5,
    "candidate_dispatch_high": 5,
}


def effective_priority(priority: int, waited_seconds: float,
                       *, aging_seconds: int, cap: int) -> float:
    """复刻 claim SQL 的排序键：priority + MIN(等待秒数/aging_seconds, cap)。"""
    points = 0.0 if aging_seconds <= 0 else waited_seconds / float(aging_seconds)
    return float(priority) + min(points, float(cap))


class PrioritySemanticsTests(unittest.TestCase):
    def setUp(self):
        self.aging = int(config.INTEL_JOB_PRIORITY_AGING_SECONDS)
        self.cap = int(config.INTEL_JOB_PRIORITY_AGING_CAP)

    def test_aging_cap_is_bounded(self):
        self.assertGreater(self.cap, 0, "等待积分必须有上限，否则优先级会被无限盖过")
        self.assertLessEqual(self.cap, 3600)

    def test_classification_priority_is_the_highest_allowed(self):
        self.assertEqual(100, _CLASSIFICATION_JOB_PRIORITY,
                         "分类优先级取 enqueue_job 允许的上限（coerce 到 [-100,100]）")

    def test_classification_beats_every_maintenance_type_even_when_they_are_ancient(self):
        for name, priority in MAINTENANCE_PRIORITIES.items():
            oldest_maintenance = effective_priority(
                priority, 7 * 24 * 3600, aging_seconds=self.aging, cap=self.cap)
            fresh_classification = effective_priority(
                _CLASSIFICATION_JOB_PRIORITY, 0, aging_seconds=self.aging, cap=self.cap)
            self.assertGreater(
                fresh_classification, oldest_maintenance,
                "分类即使刚入队也应优先于等待一周的 %s（%.1f vs %.1f）"
                % (name, fresh_classification, oldest_maintenance))

    def test_maintenance_still_runs_when_no_classification_pending(self):
        # 分类不在队列时，最老的维护类作业的排序键必须为正、且同类之间仍按等待时长排序
        older = effective_priority(-45, 30 * 3600, aging_seconds=self.aging, cap=self.cap)
        newer = effective_priority(-45, 60, aging_seconds=self.aging, cap=self.cap)
        self.assertGreater(older, newer, "同类作业之间仍应是老的先做（防饿死）")

    def test_no_aging_cap_would_reintroduce_the_bug(self):
        """反例守护：若把上限放开，分类就会重新被极老作业插队——这正是修复前的现象。"""
        unbounded_oldest_maintenance = effective_priority(
            5, 30 * 3600, aging_seconds=30, cap=10 ** 9)
        self.assertGreater(unbounded_oldest_maintenance, _CLASSIFICATION_JOB_PRIORITY,
                           "旧参数（aging=30s、无上限）下维护类确实会盖过分类")

    def test_aging_cap_alone_cannot_prevent_starvation(self):
        """反例守护：上限封顶 + 高优先级持续到货 = 低优先级永久饿死，必须靠保留名额兜底。"""
        starved_maintenance = effective_priority(
            -45, 7 * 24 * 3600, aging_seconds=self.aging, cap=self.cap)
        fresh_classification = effective_priority(
            _CLASSIFICATION_JOB_PRIORITY, 0, aging_seconds=self.aging, cap=self.cap)
        self.assertLess(starved_maintenance, fresh_classification,
                        "排序键上维护类赢不了新鲜分类——所以必须有饥饿保留名额兜底")

    def test_starvation_reserved_slots_are_configured(self):
        """保留名额必须存在且不占多数，否则低优先级类型会再次长期饿死。"""
        deadline = int(config.INTEL_WORKER_STARVATION_DEADLINE_SECONDS)
        reserved = int(config.INTEL_WORKER_STARVATION_RESERVED_SLOTS)
        self.assertGreater(deadline, 0, "饥饿阈值必须为正")
        self.assertLessEqual(deadline, 86400, "饥饿阈值不该超过一天，否则等于没有兜底")
        self.assertGreater(reserved, 0, "保留名额为 0 时低优先级类型会饿死")
        concurrency = int(os.environ.get("INTEL_WORKER_JOB_CONCURRENCY", "1") or 1)
        if concurrency > 1:
            self.assertLess(reserved, concurrency,
                            "保留名额必须是少数派，否则高优先级吞吐被拖垮")


if __name__ == "__main__":
    unittest.main()
