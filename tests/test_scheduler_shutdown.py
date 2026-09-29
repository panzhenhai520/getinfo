import signal
import unittest
from unittest.mock import patch

import start_with_schedule
from scheduler import TaskScheduler


class SchedulerShutdownTest(unittest.TestCase):
    def test_request_stop_is_non_blocking_and_marks_only_active_tasks(self):
        scheduler = TaskScheduler()
        scheduler.running = True
        scheduler._running_tasks = {
            "active": {"completed": False, "stop_flag": False},
            "done": {"completed": True, "stop_flag": False},
        }

        scheduler.request_stop(reason="process_shutdown")

        self.assertFalse(scheduler.running)
        self.assertTrue(scheduler._running_tasks["active"]["stop_flag"])
        self.assertEqual(
            scheduler._running_tasks["active"]["stop_reason"],
            "process_shutdown",
        )
        self.assertFalse(scheduler._running_tasks["done"]["stop_flag"])

    def test_signal_handler_starts_watchdog_and_requests_non_blocking_stop(self):
        start_with_schedule._shutdown_requested.clear()
        with patch.object(
            start_with_schedule, "_start_force_exit_watchdog"
        ) as watchdog, patch(
            "scheduler.scheduler.request_stop"
        ) as request_stop:
            with self.assertRaises(SystemExit) as raised:
                start_with_schedule.signal_handler(signal.SIGTERM, None)

        self.assertEqual(raised.exception.code, 0)
        watchdog.assert_called_once_with(signal.SIGTERM)
        request_stop.assert_called_once_with(reason="process_shutdown")
        start_with_schedule._shutdown_requested.clear()


if __name__ == "__main__":
    unittest.main()
