from __future__ import annotations

import config


def test_local_browser_is_a_hard_invariant(monkeypatch):
    monkeypatch.setenv('LOCAL_BROWSER_ENABLED', 'true')
    assert config.LOCAL_BROWSER_ENABLED is False
    assert config.ZYTE_ENABLED is False


def test_hybrid_crawler_has_no_playwright_import():
    source = open('hybrid_crawler.py', encoding='utf-8').read().lower()
    assert 'from playwright' not in source
    assert 'import playwright' not in source

