#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Crawler resource guards.

This module centralizes process-heavy crawler limits. It is intentionally small
so high-risk Playwright paths can share the same concurrency gate before the
larger resource-scope refactor lands.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from contextlib import asynccontextmanager, contextmanager


def _read_int_env(name: str, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(value, max_value))


PLAYWRIGHT_MAX_CONCURRENT = _read_int_env('PLAYWRIGHT_MAX_CONCURRENT', 1, 1, 8)
_playwright_semaphore = threading.BoundedSemaphore(PLAYWRIGHT_MAX_CONCURRENT)
_state_lock = threading.Lock()
_active_playwright = 0


def _acquire_playwright(label: str = ''):
    global _active_playwright
    started = time.time()
    _playwright_semaphore.acquire()
    waited = time.time() - started
    with _state_lock:
        _active_playwright += 1
        active = _active_playwright
    if waited > 1:
        print(f"Playwright资源等待 {waited:.1f}s 后进入: {label} active={active}/{PLAYWRIGHT_MAX_CONCURRENT}", flush=True)


def _release_playwright():
    global _active_playwright
    with _state_lock:
        _active_playwright = max(0, _active_playwright - 1)
    _playwright_semaphore.release()


@asynccontextmanager
async def playwright_slot(label: str = ''):
    await asyncio.to_thread(_acquire_playwright, label)
    try:
        yield
    finally:
        _release_playwright()


@contextmanager
def sync_playwright_slot(label: str = ''):
    _acquire_playwright(label)
    try:
        yield
    finally:
        _release_playwright()


def get_resource_guard_stats() -> dict:
    with _state_lock:
        active = _active_playwright
    return {
        'playwright_max_concurrent': PLAYWRIGHT_MAX_CONCURRENT,
        'playwright_active': active,
        'playwright_available': max(0, PLAYWRIGHT_MAX_CONCURRENT - active),
    }
