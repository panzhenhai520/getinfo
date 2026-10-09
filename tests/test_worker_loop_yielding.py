#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""worker 主循环的让出语义（阶段 12 吞吐根因修复）。

A 机实测（2026-10-09 12:00 CST）：只有 `core` 一条车道、3 个 worker 进程全部显示空闲
（inflight 0/0/2）、库里积着 **6494 条完全可领**的作业，而吞吐只有 157/h；
同时 intel-worker **CPU 长期 100%**。

原循环：
    if claimed == 0 and in_flight == 0: sleep(interval)   # 真空闲才睡
    elif cooldown: sleep(2)
→ 「**领不到、但槽位上有活**」这一情形**两个分支都不进**：主循环变成紧循环忙转，
既烧 CPU 又抢 GIL，把正在跑作业的线程拖慢（这正是吞吐塌下来的直接机制）。

新语义（本测试钉住）：
  1. 领到活 → 立刻继续（填满空槽，不等整批）；
  2. 没领到但**有在飞作业** → 短暂让出（必须让出 GIL）；
  3. 冷却中 → 让出 2 秒；
  4. 真的没活（无在飞、无冷却）→ 才按轮询间隔睡。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intel_worker import IntelWorker  # noqa: E402


class _LoopProbe(IntelWorker):
    """把 _pump_once / time.sleep / 停止条件换成可控桩，观察循环的让出行为。"""

    def __init__(self, sequence):
        # 不走 IntelWorker.__init__（要 DB 等依赖），只挂本测试需要的属性
        self._sequence = list(sequence)
        self._sleeps = []
        self.stop_requested = False
        self.lane_name = "probe"
        self._heartbeat_thread = None
        self._job_pool = None

    def _start_heartbeat_thread(self):
        return None

    def _record_lane_heartbeat(self, **kwargs):
        return None

    def _pump_once(self, **_kwargs):
        stats = self._sequence.pop(0) if self._sequence else {"claimed": 0, "in_flight": 0}
        # 跑完预定轮次就请求停止，避免死循环
        if not self._sequence:
            self.stop_requested = True
        return dict(stats)


class RunForeverYieldingTests(unittest.TestCase):
    def setUp(self):
        import intel_worker as module

        self.module = module
        self._real_sleep = module.time.sleep
        self.sleeps = []

        def fake_sleep(seconds):
            self.sleeps.append(seconds)

        module.time.sleep = fake_sleep
        self.addCleanup(lambda: setattr(module.time, "sleep", self._real_sleep))

    def _run(self, sequence):
        probe = _LoopProbe(sequence)
        probe.run_forever(poll_seconds=3600)
        return probe, self.sleeps

    def test_claimed_jobs_do_not_sleep(self):
        """领到活必须立刻继续领（把空槽填满），不能睡轮询间隔。"""
        _probe, sleeps = self._run([{"claimed": 5, "in_flight": 5},
                                    {"claimed": 0, "in_flight": 0}])
        self.assertEqual(sleeps[0], 3600, "只有最后一轮真空闲才按轮询间隔睡")
        self.assertNotIn(3600, sleeps[:-1])

    def test_in_flight_without_claim_yields_gil(self):
        """关键回归：没领到但有在飞作业时，必须短暂让出（原来会紧循环忙转 → CPU 100%）。"""
        probe, sleeps = self._run([{"claimed": 0, "in_flight": 8},
                                   {"claimed": 0, "in_flight": 0}])
        self.assertTrue(sleeps, "必须让出，否则主循环忙转抢 GIL")
        self.assertLess(sleeps[0], 1.0, "让出时间要短（0.2s 级），不能睡满轮询间隔")
        self.assertEqual(sleeps[-1], 3600)

    def test_cooldown_yields_two_seconds(self):
        _probe, sleeps = self._run([{"claimed": 0, "in_flight": 0, "cooldown": True},
                                    {"claimed": 0, "in_flight": 0}])
        self.assertEqual(sleeps[0], 2)
        self.assertEqual(sleeps[-1], 3600)

    def test_idle_sleeps_poll_interval(self):
        _probe, sleeps = self._run([{"claimed": 0, "in_flight": 0}])
        self.assertEqual(sleeps, [3600])

    def test_no_tight_spin_across_mixed_turns(self):
        """混合轮次不得出现"一步都不睡"的忙转（除了领到活那一轮）。"""
        _probe, sleeps = self._run([
            {"claimed": 3, "in_flight": 3},
            {"claimed": 0, "in_flight": 8},
            {"claimed": 0, "in_flight": 8},
            {"claimed": 0, "in_flight": 0},
        ])
        # 4 轮里：第 1 轮领到活不睡，第 2/3 轮让出，第 4 轮真空闲睡轮询间隔
        self.assertEqual(len(sleeps), 3)
        self.assertLess(sleeps[0], 1.0, "在飞让出必须是短睡")
        self.assertLess(sleeps[1], 1.0, "在飞让出必须是短睡")
        self.assertEqual(sleeps[2], 3600, "真空闲才按轮询间隔睡")


if __name__ == "__main__":
    unittest.main()
