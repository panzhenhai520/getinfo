from datetime import datetime
import unittest

from resource_aware_scheduler import is_peak_time, next_offpeak_time, should_defer_task


class ResourceAwareSchedulerTest(unittest.TestCase):
    def test_peak_boundaries_and_next_offpeak(self):
        self.assertTrue(is_peak_time(datetime(2026, 8, 7, 8, 0)))
        self.assertFalse(is_peak_time(datetime(2026, 8, 7, 12, 0)))
        self.assertTrue(is_peak_time(datetime(2026, 8, 7, 13, 30)))
        self.assertFalse(is_peak_time(datetime(2026, 8, 7, 18, 0)))
        self.assertEqual(next_offpeak_time(datetime(2026, 8, 7, 9, 0)).hour, 12)
        self.assertEqual(next_offpeak_time(datetime(2026, 8, 7, 14, 0)).hour, 18)

    def test_realtime_finance_is_not_deferred(self):
        peak = datetime(2026, 8, 7, 10, 0)
        self.assertTrue(should_defer_task({'config': {}}, peak))
        self.assertFalse(should_defer_task({'config': {'resource_class': 'financial_realtime'}}, peak))

