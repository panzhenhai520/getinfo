# -*- coding: utf-8 -*-
"""Agent-Reach 聚焦检索单测（不发真实网络请求，全部用替身）。

覆盖：
  1. 平台就绪判定：开关/凭据/上游工具三层门禁，以及缺一层的可读原因
  2. URL 归一化：追踪参数剥离、会话页过滤、无效链接丢弃
  3. 严格闸门：命中行业锚点才放行；只有搜索词命中/无摘要短标题一律不放行
  4. 预算硬边界：平台数、关键词数、总耗时都会真的截断
  5. 失败隔离：平台抛异常不能影响其它平台，也不能让扫描中断
  6. 扫描器接线：分支只在开关打开时才跑，且用严格闸门
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_TYPE", "sqlite")

import agent_reach_search as ar  # noqa: E402


class PlatformGateTests(unittest.TestCase):
    def setUp(self):
        ar._AVAILABILITY_CACHE.clear()

    def test_platform_off_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLATFORM_SOURCE_V2EX_ENABLED", None)
            rows = ar.enabled_platforms()
        self.assertTrue(rows)
        self.assertTrue(all(not row["usable"] for row in rows))
        self.assertTrue(all("未在设置页开启" in row["reason"] for row in rows))

    def test_credentialed_platform_needs_auth(self):
        with patch.dict(os.environ, {"PLATFORM_SOURCE_XUEQIU_ENABLED": "true"}, clear=False):
            os.environ.pop("PLATFORM_SOURCE_XUEQIU_AUTH", None)
            with patch.object(ar, "_configured_platforms", return_value=["xueqiu"]):
                rows = ar.enabled_platforms()
        self.assertEqual(rows[0]["platform"], "xueqiu")
        self.assertFalse(rows[0]["usable"])
        self.assertIn("缺少凭据", rows[0]["reason"])

    def test_zero_config_platform_usable_when_tool_ready(self):
        with patch.dict(os.environ, {"PLATFORM_SOURCE_V2EX_ENABLED": "true"}, clear=False):
            with patch.object(ar, "_configured_platforms", return_value=["v2ex"]), \
                    patch.object(ar, "availability",
                                 return_value={"ok": True, "backend": "V2EX API (public)"}):
                rows = ar.enabled_platforms()
        self.assertTrue(rows[0]["usable"])
        self.assertEqual(rows[0]["backend"], "V2EX API (public)")

    def test_unready_tool_is_reported_with_reason(self):
        with patch.dict(os.environ, {"PLATFORM_SOURCE_V2EX_ENABLED": "true"}, clear=False):
            with patch.object(ar, "_configured_platforms", return_value=["v2ex"]), \
                    patch.object(ar, "availability",
                                 return_value={"ok": False, "status": "off",
                                               "message": "V2EX API 不可用"}):
                rows = ar.enabled_platforms()
        self.assertFalse(rows[0]["usable"])
        self.assertIn("上游工具未就绪", rows[0]["reason"])


class UrlNormalizationTests(unittest.TestCase):
    def test_tracking_params_and_fragment_are_stripped(self):
        self.assertEqual(
            ar.clean_url("https://www.bilibili.com/video/BV1xx?spm_id_from=333&vd_source=a#t=1"),
            "https://www.bilibili.com/video/BV1xx",
        )
        self.assertEqual(
            ar.clean_url("https://example.com/a?utm_source=x&id=7"),
            "https://example.com/a?id=7",
        )

    def test_invalid_urls_are_dropped(self):
        for bad in ("", "javascript:void(0)", "ftp://x/y", "not-a-url"):
            self.assertEqual(ar.clean_url(bad), "", bad)

    def test_normalize_drops_non_article_pages_and_duplicates(self):
        raw = [
            {"url": "https://www.v2ex.com/member/someone", "title": "某人的主页"},
            {"url": "https://www.v2ex.com/t/123456", "title": "正常主题"},
            {"url": "https://www.v2ex.com/t/123456?utm_source=x", "title": "重复"},
            {"url": "https://www.v2ex.com/t/999", "title": ""},
            {"url": "https://example.com/a", "title": "正常文章", "content": "摘要内容"},
        ]
        items = ar.normalize_items(raw, "v2ex")
        self.assertEqual([i["url"] for i in items],
                         ["https://www.v2ex.com/t/123456", "https://example.com/a"])
        self.assertEqual(items[1]["snippet"], "摘要内容")
        self.assertEqual(items[0]["platform"], "v2ex")

    def test_non_dict_items_are_ignored(self):
        self.assertEqual(ar.normalize_items([None, "x", 3], "v2ex"), [])


class PreviewGateTests(unittest.TestCase):
    """闸门用真实行业包验证（打分依赖 classification/anchor 等真实结构）。"""

    @classmethod
    def setUpClass(cls):
        from industry_packs import industry_pack_loader

        cls.PACK = industry_pack_loader.load("family_office")
        from intel_candidates import industry_anchor_keywords

        cls.anchors = [str(v) for v in industry_anchor_keywords(cls.PACK) if str(v).strip()]
        cls.brands = [str(v) for v in (cls.PACK.get("brands") or []) if str(v).strip()]

    def test_anchor_hit_passes(self):
        self.assertTrue(self.anchors, "家庭办公室包应有行业锚点词")
        anchor = self.anchors[0]
        self.assertTrue(
            ar.preview_gate({"title": f"{anchor} 最新政策解读", "snippet": "监管发布新规"}, self.PACK)
        )

    def test_brand_hit_passes(self):
        if not self.brands:
            self.skipTest("该行业包未配置品牌词")
        brand = self.brands[0]
        self.assertTrue(
            ar.preview_gate({"title": f"{brand} 发布年度报告", "snippet": "面向高净值客户"}, self.PACK)
        )

    def test_query_only_hit_does_not_pass(self):
        """只有搜索词命中、正文没有任何行业锚点：不许它绕过打分占全文抓取槽。"""
        item = {"title": "今天随便聊聊天气和交通", "snippet": "与行业无关的闲聊内容"}
        self.assertFalse(ar.preview_gate(item, self.PACK, "家族办公室"))

    def test_short_title_without_snippet_is_rejected(self):
        anchor = self.anchors[0] if self.anchors else "家族办公室"
        self.assertFalse(ar.preview_gate({"title": anchor, "snippet": ""}, self.PACK))

    def test_broken_pack_structure_never_raises(self):
        self.assertFalse(ar.preview_gate({"title": "x" * 30, "snippet": "y"}, {}, ""))


class BudgetTests(unittest.TestCase):
    def setUp(self):
        ar._AVAILABILITY_CACHE.clear()

    def test_platform_and_query_caps_are_enforced(self):
        calls = []

        def fake_search(platform_id, query, limit=5):
            calls.append((platform_id, query))
            return [{"url": f"https://example.com/{platform_id}/{len(calls)}",
                     "title": f"{platform_id}-{query}"}]

        with patch.dict(os.environ, {}, clear=False), \
                patch.object(ar, "search_platform", side_effect=fake_search), \
                patch.object(ar.config, "AGENT_REACH_MAX_PLATFORMS_PER_RUN", 2), \
                patch.object(ar.config, "AGENT_REACH_MAX_QUERIES_PER_PLATFORM", 2):
            result = ar.search_pack(["q1", "q2", "q3"], platforms=["a", "b", "c"])
        self.assertEqual(result["platforms"], ["a", "b"])
        self.assertEqual(result["queries"], ["q1", "q2"])
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(result["items"]), 4)
        # 每条结果都要带上产生它的检索词，门禁才能按对应关键词判定
        self.assertTrue(all(item.get("query") for item in result["items"]))

    def test_platform_failure_is_isolated(self):
        def flaky(platform_id, query, limit=5):
            if platform_id == "bad":
                raise RuntimeError("boom")
            return [{"url": "https://example.com/ok", "title": "好结果"}]

        with patch.object(ar, "search_platform", side_effect=flaky):
            result = ar.search_pack(["q"], platforms=["bad", "good"])
        self.assertTrue(any(i["url"].endswith("/ok") for i in result["items"]))

    def test_deadline_stops_further_calls(self):
        calls = []

        def slow(platform_id, query, limit=5):
            calls.append(platform_id)
            return []

        with patch.object(ar, "search_platform", side_effect=slow):
            result = ar.search_pack(["q1"], platforms=["a", "b", "c"], deadline_seconds=0)
        self.assertTrue(result["timeout"])
        self.assertEqual(calls, [])

    def test_empty_queries_short_circuit(self):
        with patch.object(ar, "search_platform") as mocked:
            result = ar.search_pack([], platforms=["a"])
        mocked.assert_not_called()
        self.assertEqual(result["items"], [])


class ScannerWiringTests(unittest.TestCase):
    def test_scanner_uses_the_real_gate_and_branch(self):
        import intel_light_scanner

        self.assertIs(intel_light_scanner._agent_reach_preview_gate, ar.preview_gate)
        self.assertIs(intel_light_scanner._agent_reach_search_pack, ar.search_pack)
        import inspect

        signature = inspect.signature(intel_light_scanner.IntelLightScanner.scan)
        self.assertIn("include_agent_reach", signature.parameters)
        self.assertTrue(signature.parameters["include_agent_reach"].default)

    def test_branch_is_off_by_default(self):
        import config

        self.assertFalse(getattr(config, "AGENT_REACH_ENABLED"))


class ScannerTypeConstraintTests(unittest.TestCase):
    """来源类型约束必须处处同步。

    踩过两次的坑：代码新增一种扫描/观测来源，但 CHECK 约束没同步，
    结果是"跑得好好的，数据一行都写不进去"（ggzy_api 停写一个月，
    tavily / agent_reach 的观测行会被直接拒绝）。约束文本出现在多处，
    任何一处漏改都会被另一处后执行的 ALTER 覆盖，所以这里逐处钉住。
    """

    REQUIRED = ("rss", "list_page", "website", "serpapi", "tavily", "ggzy_api", "agent_reach")

    def _sources(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return {
            "intel_schema.py": open(os.path.join(root, "intel_schema.py"), encoding="utf-8").read(),
            "postgres_shims.py": open(os.path.join(root, "postgres_shims.py"), encoding="utf-8").read(),
        }

    def test_every_constraint_list_contains_all_types(self):
        import re

        found = 0
        for name, source in self._sources().items():
            for match in re.finditer(
                r"(scanner_type|observation_type) IN \(([^)]*)\)", source
            ):
                found += 1
                column, values = match.group(1), match.group(2)
                for required in self.REQUIRED:
                    self.assertIn(
                        f"'{required}'", values,
                        f"{name} 的 {column} 约束缺少 '{required}'：{values.strip()}",
                    )
        self.assertGreaterEqual(found, 5, "约束文本明显变少了，检查是否被误删")

    def test_scanner_emits_only_declared_types(self):
        """扫描器实际会写的 scanner_type / observation_type 必须都在上面那份清单里。"""
        import re

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        source = open(os.path.join(root, "intel_light_scanner.py"), encoding="utf-8").read()
        emitted = set(re.findall(r'scanner_type\s*=\s*"([a-z_]+)"', source))
        emitted |= set(re.findall(r"scanner_type\s*=\s*'([a-z_]+)'", source))
        emitted |= set(re.findall(r'observation_type\s*=\s*"([a-z_]+)"', source))
        self.assertTrue(emitted, "没有从扫描器里解析出任何来源类型，检查正则")
        for value in emitted:
            self.assertIn(value, self.REQUIRED, f"扫描器会写 {value}，但约束清单里没有")


if __name__ == "__main__":
    unittest.main()
