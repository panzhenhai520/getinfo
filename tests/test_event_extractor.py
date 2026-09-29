#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""事件抽取解析逻辑单测：validate_event_output / event_hash / _normalize_event_text（不依赖 LLM 网络）。"""

import unittest

from intel_llm_client import (
    IntelLLMError,
    _normalize_event_text,
    event_hash,
    validate_event_output,
)


class ValidateEventOutputTest(unittest.TestCase):
    def test_plain_json_array(self):
        content = ('[{"subject":"保监局","action":"表态","object":"境外保单征税",'
                   '"event_type":"regulation","entities":["保单"],"event_time":"2026-08-06"}]')
        events = validate_event_output(content)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["subject"], "保监局")
        self.assertEqual(events[0]["event_type"], "regulation")
        self.assertEqual(len(events[0]["event_hash"]), 16)

    def test_events_wrapper(self):
        content = '{"events":[{"subject":"A","action":"B","object":"C","event_type":"other","entities":[],"event_time":""}]}'
        self.assertEqual(len(validate_event_output(content)), 1)

    def test_markdown_fence(self):
        content = '```json\n[{"subject":"A","action":"B","object":"","event_type":"release"}]\n```'
        self.assertEqual(len(validate_event_output(content)), 1)

    def test_empty(self):
        self.assertEqual(validate_event_output('{"events":[]}'), [])
        self.assertEqual(validate_event_output('[]'), [])

    def test_missing_subject_or_action_dropped(self):
        content = ('[{"subject":"","action":"B","object":"C"},'
                   '{"subject":"A","action":"B","object":"C","event_type":"market"}]')
        events = validate_event_output(content)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["subject"], "A")

    def test_invalid_event_type_falls_to_other(self):
        content = '[{"subject":"A","action":"B","object":"C","event_type":"猜猜"}]'
        events = validate_event_output(content)
        self.assertEqual(events[0]["event_type"], "other")

    def test_invalid_json_raises(self):
        with self.assertRaises(IntelLLMError):
            validate_event_output("不是json")

    def test_cap_8_events(self):
        items = [{"subject": "S%d" % i, "action": "A", "object": "", "event_type": "other"}
                 for i in range(20)]
        events = validate_event_output(items)
        self.assertEqual(len(events), 8)


class EventHashTest(unittest.TestCase):
    def test_stable(self):
        h1 = event_hash("保监局", "表态", "境外保单征税", "regulation")
        h2 = event_hash("保监局", "表态", "境外保单征税", "regulation")
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 16)

    def test_different_type_different_hash(self):
        h1 = event_hash("A", "B", "C", "regulation")
        h2 = event_hash("A", "B", "C", "enforcement")
        self.assertNotEqual(h1, h2)

    def test_normalize_whitespace(self):
        # 空格归一（繁简归一留 2B 聚类用 OpenCC 处理，此处用同繁简测空格）
        h1 = event_hash("保 监 局", "表 态", "境外", "regulation")
        h2 = event_hash("保监局", "表态", "境外", "regulation")
        self.assertEqual(h1, h2)

    def test_normalize_brackets(self):
        h1 = event_hash("保监局（HKMA）", "表态", "X", "regulation")
        h2 = event_hash("保监局", "表态", "X", "regulation")
        self.assertEqual(h1, h2)


class NormalizeTextTest(unittest.TestCase):
    def test_strips_punctuation_and_whitespace(self):
        self.assertEqual(_normalize_event_text("  A,B.C！"), "abc")
        self.assertEqual(_normalize_event_text("保监局"), "保监局")


if __name__ == "__main__":
    unittest.main()
