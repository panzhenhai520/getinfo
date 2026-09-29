# -*- coding: utf-8 -*-
"""告警防抖单测：VPN 连续 N 次才告警、堆积连续 M 次才告警、worker 单次即告警、恢复复位。"""
import unittest
from unittest.mock import patch

import intel_alerting


def _snapshot(active=True, remote_ok=True, candidates=0):
    return (
        {"active": active, "pipeline": {"candidate_queued": candidates}},
        {"ok": remote_ok, "error": "" if remote_ok else "timeout"},
        candidates,
        0,
    )


class AlertDebounceTest(unittest.TestCase):
    def setUp(self):
        intel_alerting._state.update({'worker_alerted': False, 'remote_fails': 0,
                                      'remote_alerted': False, 'backlog_fails': 0,
                                      'backlog_alerted': False})
        self.mails = []
        patcher = patch.object(intel_alerting, '_send_mail',
                               side_effect=lambda to, subject, body: self.mails.append(body))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, times, snapshot):
        for _ in range(times):
            with patch.object(intel_alerting, '_collect_status', return_value=snapshot()):
                intel_alerting.check_and_alert('a@b.c', backlog_threshold=60)

    def test_remote_requires_consecutive_failures(self):
        intel_alerting._env_int = lambda name, default: {'SYSTEM_ALERT_REMOTE_FAILS': 3,
                                                         'SYSTEM_ALERT_BACKLOG_CONFIRM': 2}.get(name, default)
        self._run(2, lambda: _snapshot(remote_ok=False))
        self.assertEqual(self.mails, [])  # 2 次失败不告警
        self._run(1, lambda: _snapshot(remote_ok=False))
        self.assertEqual(len(self.mails), 1)
        self.assertIn("连续 3 次", self.mails[0])

    def test_remote_recovery_resets_counter(self):
        intel_alerting._env_int = lambda name, default: {'SYSTEM_ALERT_REMOTE_FAILS': 3,
                                                         'SYSTEM_ALERT_BACKLOG_CONFIRM': 2}.get(name, default)
        self._run(2, lambda: _snapshot(remote_ok=False))
        self._run(1, lambda: _snapshot(remote_ok=True))   # 恢复 → 复位
        self._run(2, lambda: _snapshot(remote_ok=False))
        self.assertEqual(self.mails, [])  # 重新计满 3 次前不再告警

    def test_backlog_requires_confirmation(self):
        intel_alerting._env_int = lambda name, default: {'SYSTEM_ALERT_REMOTE_FAILS': 3,
                                                         'SYSTEM_ALERT_BACKLOG_CONFIRM': 2}.get(name, default)
        self._run(1, lambda: _snapshot(candidates=100))
        self.assertEqual(self.mails, [])  # 单次峰值不告警
        self._run(1, lambda: _snapshot(candidates=100))
        self.assertEqual(len(self.mails), 1)
        self.assertIn("候选堆积 100", self.mails[0])

    def test_worker_offline_alerts_immediately(self):
        self._run(1, lambda: _snapshot(active=False))
        self.assertEqual(len(self.mails), 1)
        self.assertIn("Worker 未运行", self.mails[0])

    def test_env_overrides(self):
        intel_alerting._env_int = lambda name, default: {'SYSTEM_ALERT_REMOTE_FAILS': 2,
                                                         'SYSTEM_ALERT_BACKLOG_CONFIRM': 1}.get(name, default)
        self._run(2, lambda: _snapshot(remote_ok=False))
        self.assertEqual(len(self.mails), 1)  # REMOTE_FAILS=2 → 第 2 次告警
        self._run(1, lambda: _snapshot(candidates=80))
        self.assertEqual(len(self.mails), 2)  # CONFIRM=1 → 单次即告警


if __name__ == "__main__":
    unittest.main()
