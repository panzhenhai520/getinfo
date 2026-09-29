#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Smoke-check long-running crawler service stability signals."""

from __future__ import annotations

import json
import sys
import urllib.request


BASE_URL = 'http://127.0.0.1:8003'


def fetch_json(path: str) -> dict:
    with urllib.request.urlopen(f'{BASE_URL}{path}', timeout=10) as response:
        return json.loads(response.read().decode('utf-8'))


def fetch_status(path: str) -> int:
    with urllib.request.urlopen(f'{BASE_URL}{path}', timeout=10) as response:
        return response.status


def main() -> int:
    checks = []

    login_status = fetch_status('/login')
    checks.append(('login_http_200', login_status == 200, login_status))

    health = fetch_json('/api/system/health')
    checks.append(('health_ok', health.get('status') == 'ok', health.get('status')))
    checks.append(('zombie_count_ok', int(health.get('zombie_count') or 0) <= 100, health.get('zombie_count')))
    checks.append(('chrome_count_ok', int(health.get('chrome_process_count') or 0) <= 80, health.get('chrome_process_count')))

    pid_usage = health.get('pid_usage')
    checks.append(('pid_usage_ok', pid_usage is None or float(pid_usage) < 0.70, pid_usage))

    guards = health.get('resource_guards') or {}
    checks.append(('playwright_guard_present', guards.get('playwright_max_concurrent') == 1, guards))

    failed = False
    for name, ok, value in checks:
        print(f'{name}: {"PASS" if ok else "FAIL"} ({value})')
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
