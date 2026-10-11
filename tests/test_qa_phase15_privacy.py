#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 15 · P15-05（privacy-minimal logs）用例。

钉住 MASTER_RULES 第 18 条（日志不得泄露密钥或不必要的敏感信息）：
  1. **每条最小化规则单独可达**（密钥键 / 令牌 / key=value 密钥 / 邮箱 / 手机号 /
     证件号 / 正文只留指纹 / 截断），且逐条进 `rule_hits`；
  2. `leaks` 是**独立复算**的结果（拿最小化后的载荷再扫一遍原始敏感串），必须为空；
  3. 正文类字段只留 `{fingerprint, length}`：日志里查得到"是哪条"，查不到"写了什么"；
  4. 幂等（同一载荷两次最小化结果逐字相同）、非破坏（普通字段一个不改）；
  5. 关掉开关 → 载荷原样透传（但回执如实标 disabled），失败路径不抛错。
"""
import json
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_graph_contracts as contracts  # noqa: E402
import qa_memory as memory  # noqa: E402
import qa_memory_safety as ms  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

FLAGS = ("QA_PRIVACY_MINIMAL_LOG", "QA_PRIVACY_LOG_TEXT_LIMIT")


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = {name: os.environ.pop(name, None) for name in FLAGS}

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _minimize(self, payload, purpose="test"):
        return ms.minimize_log_payload(payload, purpose=purpose)


class RuleTests(Base):
    def test_every_rule_is_reachable(self):
        hits: set = set()
        cases = [
            ({"api_key": "sk-abcdefgh"}, "SECRET_KEY"),
            ({"note": "Authorization: Bearer abcdefghijklmn"}, "BEARER_TOKEN"),
            ({"note": "api_key=abcdef123456"}, "KEY_VALUE_SECRET"),
            ({"note": "联系 a.b@example.com 处理"}, "EMAIL"),
            ({"note": "联系电话 13800138000"}, "PHONE"),
            ({"note": "证件号 11010519491231002X"}, "IDENTIFIER_RUN"),
            ({"canonical_content": "一段不该进日志的正文"}, "CONTENT_FINGERPRINTED"),
            ({"note": "长" * (ms.log_text_limit() + 50)}, "TRUNCATED"),
        ]
        for payload, expected in cases:
            receipt = self._minimize(payload)["receipt"]
            hits.update(receipt["rule_hits"])
            self.assertIn(expected, receipt["rule_hits"],
                          "%s 这条规则没被命中：%s" % (expected, json.dumps(payload,
                                                                          ensure_ascii=False)[:60]))
            self.assertEqual(receipt["leaks"], [], payload)
        self.assertEqual(hits & set(contracts.PRIVACY_MINIMAL_RULES), hits)

    def test_nested_secret_keys_are_redacted_too(self):
        payload = {"outer": {"inner": {"token": "abcdef1234567"}, "list": [{"secret": "s3cr3t"}]}}
        minimized = self._minimize(payload)["payload"]
        self.assertEqual(minimized["outer"]["inner"]["token"], "[REDACTED]")
        self.assertEqual(minimized["outer"]["list"][0]["secret"], "[REDACTED]")

    def test_content_keys_become_fingerprint_and_length(self):
        text = "香港家族办公室税收优惠政策对合资格基金管理人给予利得税宽免"
        minimized = self._minimize({"canonical_content": text, "question_text": text})["payload"]
        for key in ("canonical_content", "question_text"):
            self.assertEqual(minimized[key]["fingerprint"], memory.content_fingerprint(text))
            self.assertEqual(minimized[key]["length"], len(text))
            self.assertNotIn(text[:8], json.dumps(minimized, ensure_ascii=False))

    def test_ordinary_fields_are_untouched(self):
        payload = {"stage": "level1_retrieval", "counts": {"evidence": 3}, "ok": True,
                   "priority": 0.75, "empty": None}
        minimized, receipt = ms.minimize_value(payload)
        self.assertEqual(minimized, payload)
        self.assertEqual(receipt, {})

    def test_long_text_is_truncated_with_a_marker(self):
        limit = ms.log_text_limit()
        minimized = self._minimize({"note": "长" * (limit + 20)})["payload"]
        self.assertTrue(minimized["note"].endswith("…"))
        self.assertEqual(len(minimized["note"]), limit + 1)

    def test_identifier_runs_are_replaced(self):
        minimized = self._minimize({"note": "手机 13800138000 与证件 11010519491231002X"})["payload"]
        self.assertEqual(minimized["note"], "手机 [PHONE] 与证件 [IDENTIFIER]")


class LeakTests(Base):
    def test_no_leak_across_a_rich_payload(self):
        payload = {
            "api_key": "sk-live-abcdefgh", "cookie": "session=abcdef",
            "canonical_content": "某主体的当前状态与用药记录全文",
            "evidence": [{"quote": "证据原文片段不该进日志", "evidence_ref": "article:1"}],
            "note": "联系人 a.b@example.com / 13800138000",
            "nested": {"final_answer": "最终答案全文不该进日志"},
        }
        result = self._minimize(payload)
        minimized, receipt = result["payload"], result["receipt"]
        self.assertEqual(receipt["leaks"], [])
        self.assertTrue(receipt["contract_ok"], receipt.get("contract_error"))
        rendered = json.dumps(minimized, ensure_ascii=False)
        for secret in ("sk-live-abcdefgh", "session=abcdef", "a.b@example.com", "13800138000",
                       "某主体的当前状态", "证据原文片段", "最终答案全文"):
            self.assertNotIn(secret, rendered, "%s 泄漏进日志了" % secret)

    def test_leak_detector_reports_a_real_leak(self):
        """反向验证：把原文直接当最小化结果 → 检测器必须报出来（否则它就是摆设）。"""
        payload = {"api_key": "sk-live-abcdefgh",
                   "canonical_content": "这段正文足够长以触发内容比对"}
        leaks = ms.verify_no_leak(payload, dict(payload))
        self.assertTrue([item for item in leaks if item.startswith("SECRET_VALUE")])
        self.assertTrue([item for item in leaks if item.startswith("CONTENT_VALUE")])

    def test_leak_detector_flags_identifier_runs(self):
        self.assertIn("IDENTIFIER_RUN", ms.verify_no_leak({}, {"note": "13800138000"}))

    def test_receipt_is_deterministic_and_self_describing(self):
        payload = {"canonical_content": "正文", "api_key": "sk-abcdefgh", "counts": {"a": 1}}
        first = self._minimize(payload)
        second = self._minimize(payload)
        self.assertEqual(first["payload"], second["payload"])
        self.assertEqual(first["receipt"]["rule_hits"], second["receipt"]["rule_hits"])
        self.assertIn("字段名判定", first["receipt"]["content_policy"])
        ok, note = validate("privacy_minimal_log", {key: first["receipt"][key] for key in (
            "policy_version", "rule_hits", "leaks", "content_policy")})
        self.assertTrue(ok, note)


class SwitchTests(Base):
    def test_switch_off_passes_the_payload_through(self):
        os.environ["QA_PRIVACY_MINIMAL_LOG"] = "0"
        payload = {"api_key": "sk-abcdefgh", "canonical_content": "正文"}
        result = self._minimize(payload)
        self.assertEqual(result["payload"], payload)
        self.assertEqual(result["receipt"]["content_policy"], "disabled")
        self.assertEqual(result["receipt"]["leaks"], [])

    def test_text_limit_is_configurable(self):
        os.environ["QA_PRIVACY_LOG_TEXT_LIMIT"] = "32"
        minimized = self._minimize({"note": "字" * 64})["payload"]
        self.assertEqual(len(minimized["note"]), 33)

    def test_audit_payload_helper_minimizes_before_returning(self):
        payload, receipt = ms.audit_payload({"canonical_content": "正文", "note": "13800138000"})
        self.assertNotIn("13800138000", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(receipt["leaks"], [])
        self.assertIn("CONTENT_FINGERPRINTED", receipt["rule_hits"])

    def test_odd_shapes_never_raise(self):
        for payload in ({}, [], {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}},
                        {"note": None}, {"note": 12345}):
            minimized, _receipt = ms.minimize_value(payload)
            self.assertIsNotNone(minimized)


class ContentKeyGovernanceTests(Base):
    def test_content_keys_are_a_closed_documented_set(self):
        self.assertIn("canonical_content", ms.CONTENT_LOG_KEYS)
        self.assertIn("final_answer", ms.CONTENT_LOG_KEYS)
        self.assertNotIn("evidence_ref", ms.CONTENT_LOG_KEYS,
                         "引用/标识类字段不是正文，删了它日志就没有可追溯性")

    def test_bom_and_control_chars_are_stripped(self):
        minimized = self._minimize({"note": "a\x00b\x07c"})["payload"]
        self.assertEqual(minimized["note"], "a b c")


if __name__ == "__main__":
    unittest.main()
