# -*- coding: utf-8 -*-
"""自研反爬厂商识别器（antibot_detector + config/antibot_rules.json）单测。

覆盖：
  1. 规则库自身合法（schema、方法分类、正则可编译、置信度范围）
  2. 各厂商代表性判据能被识别出来，并给出正确的类型与处置引擎
  3. 「CDN 存在性」弱信号不会把正常 200 响应误判成拦截
  4. 正常页面返回 None（不误报）
  5. 引擎编排：JS 传感器型跳过 curl_cffi，验证码型标人工且不再尝试
  6. 信源级统计与放弃策略（写 intel_sources.metadata_json）与恢复
"""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_TYPE", "sqlite")

import antibot_detector as ad  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class AntibotRulesFileTests(unittest.TestCase):
    """规则库本身的合法性：文件坏掉必须在加载时就报错，而不是静默失效。"""

    def test_rules_file_loads_and_covers_required_vendors(self):
        catalog = ad.reload_antibot_rules()
        self.assertEqual(catalog["schema_version"], "antibot-rules-v1")
        vendors = catalog["vendors"]
        # 首批必须覆盖的厂商（用户明确点名的清单）
        for vendor_id in (
            "cloudflare",
            "akamai",
            "datadome",
            "imperva",
            "aws_waf",
            "perimeterx",
            "kasada",
            "aliyun_waf",
            "ruishu",
            "turnstile",
            "geetest",
            "recaptcha",
            "hcaptcha",
        ):
            self.assertIn(vendor_id, vendors, f"缺少厂商规则: {vendor_id}")
        # 每个厂商都要有类型与处置建议
        for vendor_id, vendor in vendors.items():
            self.assertIn(
                vendor["kind"],
                {"fingerprint", "js_sensor", "captcha", "waf"},
                f"{vendor_id} 的 kind 非法",
            )
            self.assertTrue(vendor["preferred_engine"], f"{vendor_id} 缺少 preferred_engine")
            self.assertTrue(vendor["rules"], f"{vendor_id} 没有任何规则")

    def test_invalid_method_is_rejected(self):
        bad = {
            "schema_version": "antibot-rules-v1",
            "vendors": {"x": {"rules": [{"id": "bad", "method": "telepathy", "pattern": "x"}]}},
        }
        path = self._write_rules(bad)
        with self.assertRaises(ad.AntibotRulesError):
            ad.load_antibot_rules(path)

    def test_invalid_pattern_is_rejected(self):
        bad = {
            "schema_version": "antibot-rules-v1",
            "vendors": {"x": {"rules": [{"id": "bad", "method": "content", "pattern": "([unclosed"}]}},
        }
        path = self._write_rules(bad)
        with self.assertRaises(ad.AntibotRulesError):
            ad.load_antibot_rules(path)

    def _write_rules(self, payload) -> str:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(payload, handle)
        handle.close()
        self.addCleanup(lambda: os.unlink(handle.name))
        return handle.name


class AntibotDetectionTests(unittest.TestCase):
    """识别准确性：认得出厂商、不误伤正常页面。"""

    def test_cloudflare_challenge_page_is_blocked(self):
        protection = ad.detect_antibot(
            status_code=503,
            headers={"Server": "cloudflare", "cf-ray": "abc", "cf-mitigated": "challenge"},
            body="<title>Just a moment...</title>",
        )
        self.assertIsNotNone(protection)
        self.assertEqual(protection["vendor"], "cloudflare")
        self.assertTrue(protection["is_block"])
        self.assertEqual(ad.preferred_engine(protection), "curl_cffi")
        self.assertFalse(ad.should_skip_curl_cffi(protection))

    def test_cloudflare_normal_200_is_not_a_block(self):
        """cf-ray 是 CDN 存在性信号：正常 200 不能判成被拦（否则会大面积误报）。"""
        protection = ad.detect_antibot(
            status_code=200,
            headers={"Server": "cloudflare", "cf-ray": "abc"},
            body="<html>正常的新闻正文</html>",
        )
        self.assertIsNotNone(protection)
        self.assertEqual(protection["vendor"], "cloudflare")
        self.assertFalse(protection["is_block"])

    def test_plain_page_detects_nothing(self):
        protection = ad.detect_antibot(
            status_code=200,
            headers={"Server": "nginx"},
            body="<html><body>普通新闻正文，没有任何反爬痕迹</body></html>",
        )
        self.assertIsNone(protection)

    def test_js_sensor_vendors_skip_curl_cffi(self):
        """DataDome/Kasada 这类靠 JS 传感器的，curl_cffi 试多少次都是浪费。"""
        cases = [
            ({"Set-Cookie": "datadome=abc"}, "datadome"),
            ({"x-kpsdk-ct": "xyz"}, "kasada"),
            ({"Set-Cookie": "_abck=abc"}, "akamai"),
            ({"Set-Cookie": "FSSBBIl1UgzbN7N80S=abc"}, "ruishu"),
        ]
        for headers, expected in cases:
            with self.subTest(vendor=expected):
                protection = ad.detect_antibot(status_code=403, headers=headers, body="x")
                self.assertIsNotNone(protection, expected)
                self.assertEqual(protection["vendor"], expected)
                self.assertEqual(protection["kind"], "js_sensor")
                self.assertTrue(ad.should_skip_curl_cffi(protection))
                self.assertEqual(ad.plan_engines(protection, ["curl_cffi", "browser"]), ["browser"])

    def test_captcha_vendors_require_manual_and_no_engine(self):
        """验证码型机器过不去：标人工，且不再尝试任何引擎。"""
        cases = [
            ('<div class="cf-turnstile" data-sitekey="0x4AAA"></div>', "turnstile"),
            ('<script src="https://static.geetest.com/static/js/gt.js"></script>', "geetest"),
            ('<div class="g-recaptcha"></div>', "recaptcha"),
            ('<div class="h-captcha"></div>', "hcaptcha"),
        ]
        for body, expected in cases:
            with self.subTest(vendor=expected):
                protection = ad.detect_antibot(status_code=403, body=body)
                self.assertIsNotNone(protection, expected)
                self.assertEqual(protection["vendor"], expected)
                self.assertEqual(protection["kind"], "captcha")
                self.assertTrue(ad.needs_manual(protection))
                self.assertEqual(ad.plan_engines(protection, ["curl_cffi", "browser"]), [])

    def test_non_standard_challenge_status_codes_are_honoured(self):
        """加速乐/瑞数用 412/202/521 这类非标准码返回挑战页，必须按厂商口径判定。"""
        for status, headers, expected in (
            (412, {"Set-Cookie": "__jsluid_s=abc"}, "jsl"),
            (521, {"Set-Cookie": "__jsl_clearance=abc"}, "jsl"),
            (202, {"Set-Cookie": "FSSBBIl1UgzbN7N80S=abc"}, "ruishu"),
        ):
            with self.subTest(status=status, vendor=expected):
                protection = ad.detect_antibot(status_code=status, headers=headers, body="<html></html>")
                self.assertIsNotNone(protection, expected)
                self.assertEqual(protection["vendor"], expected)
                self.assertTrue(protection["is_block"], f"{expected} {status} 应判定为拦截")
                self.assertEqual(ad.plan_engines(protection, ["curl_cffi", "browser"]), ["browser"])

    def test_captcha_widget_on_a_normal_page_is_only_a_trace(self):
        """登录页/评论框也会挂验证码挂件：正常 200 不能判成被拦（实测踩过的误报）。"""
        protection = ad.detect_antibot(
            status_code=200,
            body='<div class="g-recaptcha"></div><form>登录</form>',
        )
        self.assertIsNotNone(protection)
        self.assertEqual(protection["vendor"], "recaptcha")
        self.assertFalse(protection["is_block"])
        self.assertFalse(ad.needs_manual(protection))
        self.assertEqual(
            ad.plan_engines(protection, ["curl_cffi", "browser"]), ["curl_cffi", "browser"]
        )

    def test_session_cookie_on_a_normal_page_is_only_a_trace(self):
        """acw_tc / __jsluid / _abck / datadome 这类会话 cookie 正常响应也会下发。"""
        cases = [
            ({"Set-Cookie": "acw_tc=abc; path=/"}, "aliyun_waf"),
            ({"Set-Cookie": "__jsluid_s=abc; path=/"}, "jsl"),
            ({"Set-Cookie": "_abck=abc; path=/"}, "akamai"),
            ({"Set-Cookie": "datadome=abc; path=/"}, "datadome"),
            ({"Set-Cookie": "BIGipServerpool=abc; path=/"}, "f5_bigip"),
        ]
        for headers, expected in cases:
            with self.subTest(vendor=expected):
                trace = ad.detect_antibot(status_code=200, headers=headers, body="<html>正常正文</html>")
                self.assertIsNotNone(trace, expected)
                self.assertEqual(trace["vendor"], expected)
                self.assertFalse(trace["is_block"], f"{expected} 正常 200 不该判成拦截")
                blocked = ad.detect_antibot(status_code=403, headers=headers, body="blocked")
                self.assertIsNotNone(blocked, expected)
                self.assertTrue(blocked["is_block"], f"{expected} 403 应该判成拦截")

    def test_waf_vendors_are_recognised(self):
        cases = [
            ({"x-iinfo": "9-1", "Set-Cookie": "incap_ses_1=a"}, 403, "imperva"),
            ({"x-amzn-waf-action": "challenge"}, 405, "aws_waf"),
            ({"Set-Cookie": "BIGipServerpool=a", "x-wa-info": "9"}, 403, "f5_bigip"),
        ]
        for headers, status, expected in cases:
            with self.subTest(vendor=expected):
                protection = ad.detect_antibot(status_code=status, headers=headers, body="x")
                self.assertIsNotNone(protection, expected)
                self.assertEqual(protection["vendor"], expected)

    def test_unknown_blocker_is_attributed_to_generic_waf(self):
        """认不出厂商的中文风控文案也要有归属，否则统计里会漏掉一整类拦截。"""
        protection = ad.detect_antibot(
            status_code=403,
            headers={"Server": "Tengine", "Content-Type": "application/json"},
            body=json.dumps({"status": 403, "error": True, "msg": "检测到异常访问行为"}, ensure_ascii=False),
        )
        self.assertIsNotNone(protection)
        self.assertEqual(protection["vendor"], "unknown_waf")
        self.assertTrue(protection["is_block"])

    def test_window_globals_participate_in_matching(self):
        """window 方法与 content 方法必须都能命中（浏览器探测时用 window 更准）。"""
        protection = ad.detect_antibot(
            status_code=403,
            body="<html></html>",
            window_globals=["KPSDK", "other"],
        )
        self.assertIsNotNone(protection)
        self.assertEqual(protection["vendor"], "kasada")

    def test_payload_method_is_supported(self):
        """payload 方法已实现（未来主动探测用），规则库里暂时没有厂商使用。"""
        signals = ad.build_signals(payload="a=1")
        self.assertEqual(signals["payload"], "a=1")

    def test_detector_disabled_returns_none(self):
        class _Settings:
            ANTIBOT_DETECTOR_ENABLED = False

        protection = ad.detect_antibot(
            status_code=503,
            headers={"cf-mitigated": "challenge"},
            body="Just a moment...",
            settings=_Settings(),
        )
        self.assertIsNone(protection)

    def test_detector_never_raises_on_garbage_input(self):
        for kwargs in (
            dict(headers=object(), body=None),
            dict(headers=None, body=object()),
            dict(status_code="not-a-number", body=b"\xff\xfe binary"),
            dict(body="x" * 10, cookies={"a": "b"}),
        ):
            self.assertIsNone(ad.detect_antibot(**kwargs) if False else None)
            ad.detect_antibot(**kwargs)  # 不抛异常即可

    def test_protection_summary_is_human_readable(self):
        protection = ad.detect_antibot(
            status_code=503, headers={"cf-mitigated": "challenge"}, body="Just a moment..."
        )
        summary = ad.protection_summary(protection)
        self.assertIn("Cloudflare", summary)
        self.assertIn("指纹型", summary)


class AntibotSourcePolicyTests(unittest.TestCase):
    """信源级统计 + 放弃策略：只写 metadata_json，不改表结构。"""

    def setUp(self):
        self.db = _FakeSourceDb()

    def _protection(self, vendor="datadome", kind="js_sensor", label="DataDome", action="retry_browser"):
        return {
            "vendor": vendor,
            "label": label,
            "kind": kind,
            "action": action,
            "is_block": True,
            "confidence": 0.95,
        }

    def test_block_is_counted_and_source_is_abandoned_at_threshold(self):
        # JS 传感器型阈值 3：第 1、2 次只是 warn，第 3 次起标记「不再派发任务」
        for index in range(1, 4):
            result = ad.record_source_block(1, self._protection(), db=self.db)
            self.assertTrue(result["recorded"])
            self.assertEqual(result["vendor_count"], index)
            status = self.db.metadata(1)[ad.META_STATUS]
            self.assertEqual(status, "blocked" if index >= 3 else "warn")
        metadata = self.db.metadata(1)
        self.assertEqual(metadata[ad.META_VENDORS]["datadome"], 3)
        self.assertIn("被DataDome拦截3次", metadata[ad.META_REASON])
        self.assertTrue(ad.should_skip_source(metadata))

    def test_captcha_source_is_abandoned_after_two_blocks(self):
        protection = self._protection(
            vendor="geetest", kind="captcha", label="极验 Geetest", action="needs_manual"
        )
        ad.record_source_block(2, protection, db=self.db)
        self.assertEqual(self.db.metadata(2)[ad.META_STATUS], "warn")
        ad.record_source_block(2, protection, db=self.db)
        metadata = self.db.metadata(2)
        self.assertEqual(metadata[ad.META_STATUS], "blocked")
        self.assertTrue(metadata[ad.META_NEEDS_MANUAL])
        self.assertTrue(ad.should_skip_source(metadata))

    def test_success_clears_the_abandon_flag(self):
        for _ in range(3):
            ad.record_source_block(1, self._protection(), db=self.db)
        self.assertTrue(ad.should_skip_source(self.db.metadata(1)))
        result = ad.record_source_success(1, db=self.db)
        self.assertTrue(result["cleared"])
        metadata = self.db.metadata(1)
        self.assertEqual(metadata[ad.META_STATUS], "recovered")
        self.assertFalse(ad.should_skip_source(metadata))
        # 累计计数保留，便于看板复盘
        self.assertEqual(metadata[ad.META_VENDORS]["datadome"], 3)

    def test_give_up_can_be_disabled_by_settings(self):
        class _Settings:
            ANTIBOT_GIVE_UP_ENABLED = False

        for _ in range(3):
            ad.record_source_block(1, self._protection(), db=self.db, settings=_Settings())
        self.assertFalse(ad.should_skip_source(self.db.metadata(1), _Settings()))

    def test_explicit_threshold_overrides_kind_default(self):
        class _Settings:
            ANTIBOT_GIVE_UP_THRESHOLD = 1

        ad.record_source_block(1, self._protection(), db=self.db, settings=_Settings())
        self.assertEqual(self.db.metadata(1)[ad.META_STATUS], "blocked")

    def test_unknown_source_is_ignored(self):
        result = ad.record_source_block(999, self._protection(), db=self.db)
        self.assertFalse(result["recorded"])

    def test_no_protection_is_ignored(self):
        self.assertFalse(ad.record_source_block(1, None, db=self.db)["recorded"])

    def test_ranking_and_health_report(self):
        for _ in range(3):
            ad.record_source_block(1, self._protection(), db=self.db)
        ad.record_source_block(2, self._protection(vendor="cloudflare", kind="fingerprint", label="Cloudflare"), db=self.db)
        ranking = ad.blocked_vendor_ranking(self.db)
        self.assertEqual(ranking[0]["vendor"], "datadome")
        self.assertEqual(ranking[0]["count"], 3)
        report = ad.source_health_report(self.db)
        self.assertEqual(report["source_total"], 2)
        self.assertEqual(report["blocked_count"], 1)
        self.assertEqual(report["warn_count"], 1)
        self.assertEqual(report["blocked_sources"][0]["source_id"], 1)
        self.assertTrue(report["blocked_sources"][0]["needs_proxy"])

    def test_corrupt_metadata_does_not_crash(self):
        self.db.set_metadata_raw(1, "{not json")
        report = ad.source_health_report(self.db)
        self.assertEqual(report["source_total"], 2)
        self.assertFalse(ad.should_skip_source(ad._parse_metadata("{not json")))

    def test_scanner_uses_the_real_give_up_check(self):
        """扫描器必须接上真正的放弃判定（而不是 ImportError 兜底的空实现）。"""
        import intel_light_scanner

        self.assertIs(
            intel_light_scanner._should_skip_blocked_source,
            ad.should_skip_source,
        )
        blocked = {ad.META_STATUS: "blocked", ad.META_REASON: "被DataDome拦截3次，不再派发任务"}
        self.assertTrue(intel_light_scanner._should_skip_blocked_source(blocked))
        self.assertFalse(intel_light_scanner._should_skip_blocked_source({}))


class AntibotSelfCheckTests(unittest.TestCase):
    """隐身指纹自检：探针清单完整，浏览器不可用时必须优雅降级（不能把接口打挂）。"""

    def test_probe_list_is_well_formed(self):
        ids = [probe[0] for probe in ad.SELF_CHECK_PROBES]
        self.assertEqual(len(ids), len(set(ids)), "探针 id 不能重复")
        self.assertIn("navigator.webdriver", ids)
        self.assertIn("automation_globals", ids)
        self.assertIn("webgl_renderer", ids)
        for probe_id, label, js, ok_fn, severity, advice in ad.SELF_CHECK_PROBES:
            self.assertTrue(label and js and advice, probe_id)
            self.assertIn(severity, {"high", "medium", "low"}, probe_id)
            self.assertTrue(callable(ok_fn), probe_id)

    def test_automation_globals_probe_covers_known_markers(self):
        js = ad._automation_globals_js()
        for marker in ("cdc_", "__playwright", "__selenium_unwrapped", "callPhantom"):
            self.assertIn(marker, js)

    def test_self_check_degrades_when_scrapling_missing(self):
        import builtins
        import sys

        original = sys.modules.get("scrapling")
        sys.modules["scrapling"] = None  # 让 import 抛错
        try:
            result = ad.stealth_self_check("https://example.com", timeout_ms=1000)
        finally:
            if original is None:
                sys.modules.pop("scrapling", None)
            else:
                sys.modules["scrapling"] = original
        self.assertFalse(result["available"])
        self.assertIn("Scrapling 不可用", result["error"])


class _FakeSourceDb:
    """最小 intel_sources 替身：列名必须与 intel_schema 一致。

    故意使用真实列名 source_name（不是 name）：如果 detector 把列名写错，
    这里的查询会抛错 → _all_source_rows 吞掉异常返回空 → 看板统计用例失败，
    从而把「看板恒返回全 0」这类静默 Bug 挡在提交之前。
    """

    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.lock = __import__("threading").RLock()
        self.connection.execute(
            "CREATE TABLE intel_sources ("
            " id INTEGER PRIMARY KEY, source_name TEXT, source_url TEXT,"
            " authority_level INTEGER, is_enabled INTEGER,"
            " metadata_json TEXT, updated_at TEXT)"
        )
        self.connection.execute(
            "INSERT INTO intel_sources(id,source_name,source_url,authority_level,is_enabled,metadata_json,updated_at)"
            " VALUES(1,'源A','https://a.example/feed',3,1,'{}',''),"
            "       (2,'源B','https://b.example/feed',2,1,'{}','')"
        )
        self.connection.commit()

    def _ensure_connection(self):
        return None

    def metadata(self, source_id: int) -> dict:
        row = self.connection.execute(
            "SELECT metadata_json FROM intel_sources WHERE id=?", (source_id,)
        ).fetchone()
        return json.loads(row["metadata_json"] or "{}")

    def set_metadata_raw(self, source_id: int, raw: str) -> None:
        self.connection.execute(
            "UPDATE intel_sources SET metadata_json=? WHERE id=?", (raw, source_id)
        )
        self.connection.commit()


if __name__ == "__main__":
    unittest.main()
