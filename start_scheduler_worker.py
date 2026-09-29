#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Dedicated scheduler worker process.

The web container imports firecrawl_app through gunicorn and must not run
crawler jobs. This worker owns the long-running scheduler and Playwright-heavy
crawl execution so failures stay isolated from HTTP serving.
"""

import os
import signal
import sys
import threading
import time


def _load_dotenv_file(path='.env'):
    if not os.path.exists(path):
        return
    try:
        with open(path, 'r', encoding='utf-8') as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                if line.startswith('export '):
                    line = line[7:].strip()
                key, value = line.split('=', 1)
                key = key.strip()
                if key and key not in os.environ:
                    os.environ[key] = value.strip().strip('"').strip("'")
    except Exception as exc:
        print(f"Warning: failed to load .env: {exc}", flush=True)


def _configure_stdio():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass


_load_dotenv_file()
_configure_stdio()

_running = True


def _handle_signal(signum, _frame):
    global _running
    print(f"收到信号 {signum}，正在停止调度 Worker...", flush=True)
    _running = False


def _read_int_file(path: str):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw = f.read().strip()
        if raw == 'max':
            return None
        return int(raw)
    except Exception:
        return None


def _scan_worker_processes() -> dict:
    zombie_count = 0
    chrome_process_count = 0
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            with open(f'/proc/{pid}/status', 'r', encoding='utf-8', errors='replace') as f:
                status_text = f.read()
            if '\nState:\tZ' in status_text or '\nState: Z' in status_text:
                zombie_count += 1
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                cmdline = f.read().replace(b'\x00', b' ').decode('utf-8', errors='replace').lower()
            if 'chrome' in cmdline or 'chromium' in cmdline:
                chrome_process_count += 1
        except Exception:
            continue
    return {'zombie_count': zombie_count, 'chrome_process_count': chrome_process_count}


def _watchdog_loop():
    pid_unhealthy = float(os.getenv('WORKER_WATCHDOG_PID_UNHEALTHY', '0.85'))
    zombie_unhealthy = int(os.getenv('WORKER_WATCHDOG_ZOMBIE_UNHEALTHY', '100'))
    chrome_unhealthy = int(os.getenv('WORKER_WATCHDOG_CHROME_UNHEALTHY', '80'))
    interval = int(os.getenv('WORKER_WATCHDOG_INTERVAL_SECONDS', '30') or 30)
    while _running:
        current = _read_int_file('/sys/fs/cgroup/pids.current')
        maximum = _read_int_file('/sys/fs/cgroup/pids.max')
        pid_usage = (current / maximum) if current is not None and maximum else 0
        proc = _scan_worker_processes()
        if (
            pid_usage >= pid_unhealthy
            or proc['zombie_count'] > zombie_unhealthy
            or proc['chrome_process_count'] > chrome_unhealthy
        ):
            print(
                "Worker watchdog unhealthy: "
                f"pid_usage={pid_usage:.3f} pids={current}/{maximum} "
                f"zombies={proc['zombie_count']} chrome={proc['chrome_process_count']}. exiting for restart.",
                flush=True,
            )
            os._exit(75)
        time.sleep(max(5, interval))


def main():
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    print("启动独立调度 Worker", flush=True)
    print(f"DATABASE_PATH={os.getenv('DATABASE_PATH')}", flush=True)
    print(f"CRAWL_SCHEDULER_MAX_CONCURRENT={os.getenv('CRAWL_SCHEDULER_MAX_CONCURRENT')}", flush=True)

    from scheduler import scheduler

    watchdog_thread = threading.Thread(target=_watchdog_loop, name='WorkerWatchdog', daemon=True)
    watchdog_thread.start()

    scheduler.start()
    try:
        while _running:
            time.sleep(2)
    finally:
        scheduler.stop()
        print("调度 Worker 已停止", flush=True)


if __name__ == '__main__':
    main()
