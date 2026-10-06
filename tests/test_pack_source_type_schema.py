#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行业包种子与发布校验一致性回归测试。

背景（生产实测）：
  * 代码里已实现 GgzyTradingApiScanner（scanner_type='ggzy_api'），行业包
    bolean_security_compute 的 default_sources 里也带了一个 ggzy_api 信源；
  * 但 config/industry_packs/schema.json 的 default_sources.items.source_type
    只允许 website/list_page/rss → 该包从种子发布时校验失败，永远发布不出去；
  * 同时 intel_scan_runs.scanner_type 的 CHECK 约束也不含 ggzy_api
    （实测该类型扫描记录停在 2026-09-07，之后写不进去）。
这里钉住"种子用到的 source_type 必须都在 schema 允许范围内"这条性质。
"""
import json
import unittest
from pathlib import Path

SEED_DIR = Path(__file__).resolve().parent.parent / "config" / "industry_packs"


class PackSourceTypeSchemaTests(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads((SEED_DIR / "schema.json").read_text(encoding="utf-8"))
        item = self.schema["properties"]["default_sources"]["items"]["properties"]["source_type"]
        self.allowed = set(item["enum"])

    def test_every_seed_source_type_is_allowed_by_schema(self):
        offenders = {}
        for path in sorted(SEED_DIR.glob("*.json")):
            if path.name == "schema.json":
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            bad = sorted({
                str(src.get("source_type") or "")
                for src in data.get("default_sources") or []
                if str(src.get("source_type") or "") not in self.allowed
            })
            if bad:
                offenders[path.name] = bad
        self.assertEqual({}, offenders,
                         "这些种子用了 schema 不允许的 source_type，会发布失败：%s" % offenders)

    def test_ggzy_api_is_allowed_because_scanner_exists(self):
        """代码支持 ggzy_api 扫描器，schema 必须放行，否则该包永远发布不出去。"""
        scanner = (SEED_DIR.parent.parent / "intel_light_scanner.py").read_text(encoding="utf-8")
        self.assertIn('scanner_type = "ggzy_api"', scanner,
                      "扫描器实现里若不再有 ggzy_api，本用例与 schema 的例外应一起删掉")
        self.assertIn("ggzy_api", self.allowed)


if __name__ == "__main__":
    unittest.main()
