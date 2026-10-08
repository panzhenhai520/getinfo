#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 5：用户【调整思路】必须真正改变**检索与生成**，而不只是换回答模板。

实测问题（本轮之前）：调整确实到后端并进了 plan（user_adjustment / answer_strategy /
answer_template），但 `queries`、`subquestions`、送模证据完全没变
——"查看政策全文，逐段解释"只是"听话了但没做到"。

这里钉住四条底线：
  1. "全文/逐段解释" → output_form=paragraph_by_paragraph + must_fetch_fulltext
     + plan.queries 里出现专项检索式；
  2. 时间调整 → plan.time_window_adjustment 有值，且检索侧真的把窗口换掉；
  3. 用户没调整时，行为与以前**完全一致**（不新增检索式、不改输出形式）；
  4. "同意/继续"仍走确认分支，不触发额外的 LLM 解析。
"""
import json
import unittest
from datetime import datetime, timedelta, timezone

import qa_planner as qp
import qa_retrieval as qr

SUBQUESTIONS = [
    {"id": "q1", "text": "政策内容和适用边界是什么", "category": {"key": "policy_content"}},
    {"id": "q2", "text": "对行业有什么影响", "category": {"key": "industry_impact"}},
]


class _Pack:
    """最小行业包替身：只要 planner 用到的几个字段。"""

    def __init__(self):
        self.pack = {
            "id": "family_office",
            "core_keywords": ["家族办公室", "离岸信托"],
            "expanded_keywords": ["税务"],
            "fixed_topics": [],
            "ragflow_policy": {"qa_retrieval_enabled": False},
        }

    def load(self, pack_id, **_kwargs):
        return dict(self.pack)


class _Loader:
    def __init__(self):
        self._pack = _Pack()

    def load(self, pack_id, **_kwargs):
        return self._pack.load(pack_id)


def _adjustment_question(original: str, adjustment: str) -> str:
    return "\n\n".join([
        "原始问题：%s" % original,
        "此前回答计划：先核验政策原文",
        "用户调整意见：%s" % adjustment,
        "请先理解用户调整意见，重新生成问题分析思路，再按新思路继续回答。",
    ])


class FulltextAdjustmentTests(unittest.TestCase):
    def test_rule_path_detects_paragraph_by_paragraph(self):
        patch = qp._rule_based_adjustment_patch("查看政策全文，逐段解释", SUBQUESTIONS)
        normalized = qp._normalize_adjustment_patch(patch, SUBQUESTIONS)
        self.assertEqual(normalized["output_form"], "paragraph_by_paragraph")
        self.assertTrue(normalized["must_fetch_fulltext"])
        self.assertTrue(normalized["retrieval_queries"], "必须给出专项检索式")

    def test_output_form_hints(self):
        cases = {
            "用表格对比一下两个口径": "table",
            "给我一句话结论就行": "brief",
            "按时间轴梳理": "timeline",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                patch = qp._rule_based_adjustment_patch(text, SUBQUESTIONS)
                self.assertEqual(qp._normalize_adjustment_patch(patch, SUBQUESTIONS)["output_form"], expected)

    def test_plan_queries_gain_the_special_query(self):
        planner = qp.QaQueryPlanner(pack_loader=_Loader())
        base = planner.plan({"question": "离岸信托新规怎么规定的", "industry_pack_id": "family_office"})
        adjusted = planner.plan({
            "question": _adjustment_question("离岸信托新规怎么规定的", "查看政策全文，逐段解释"),
            "industry_pack_id": "family_office",
        })
        self.assertEqual(adjusted["output_form"], "paragraph_by_paragraph")
        self.assertTrue(adjusted["must_fetch_fulltext"])
        # 政策类问题本身就会加"官方原文"检索式，所以这里比对"调整新增的那几条"
        added = [q for q in adjusted["queries"] if q not in base["queries"]]
        self.assertTrue(added, "调整必须给检索式列表带来新增项")
        self.assertTrue(any("全文" in q for q in added),
                        "调整要求看全文时，检索式里要出现全文专项查询：%r" % added)
        for item in adjusted["question_plan"]["retrieval_queries"]:
            self.assertIn(item, adjusted["queries"], "专项检索式必须真的进 plan.queries")

    def test_receipt_is_user_readable(self):
        planner = qp.QaQueryPlanner(pack_loader=_Loader())
        result = planner.plan({
            "question": _adjustment_question("离岸信托新规怎么规定的", "查看政策全文，逐段解释"),
            "industry_pack_id": "family_office",
        })
        receipt = result["adjustment_receipt"]
        self.assertTrue(receipt["items"])
        self.assertIn("逐段解释", receipt["summary"])
        # 回执要能安全进 SSE 事件负载
        json.dumps(receipt, ensure_ascii=False)


class TimeAdjustmentTests(unittest.TestCase):
    def test_relative_time_phrase_becomes_window(self):
        patch = qp._rule_based_adjustment_patch("只看最近3个月的资料", SUBQUESTIONS)
        window = qp._normalize_adjustment_patch(patch, SUBQUESTIONS)["time_window"]
        self.assertEqual(window["days"], 90)
        self.assertTrue(window["start"] and window["end"])

    def test_absolute_month_becomes_window(self):
        window = qp._time_window_from_phrase("2026年1月")
        self.assertEqual(window["label"], "2026 年 1 月")
        self.assertTrue(window["start"].startswith("2026-01-01"))

    def test_plain_sentence_is_not_mistaken_for_a_time_range(self):
        """整句话不能被当成时间标签（曾把"查看政策全文"写成时间范围）。"""
        patch = qp._rule_based_adjustment_patch("查看政策全文，逐段解释", SUBQUESTIONS)
        self.assertFalse(qp._normalize_adjustment_patch(patch, SUBQUESTIONS)["time_window"])

    def test_adjustment_window_replaces_parsed_window_in_retrieval(self):
        plan = {"time_window_adjustment": {"label": "2026 年 1 月", "days": 31,
                                           "start": "2026-01-01T00:00:00+00:00",
                                           "end": "2026-02-01T00:00:00+00:00"}}
        window = qr._window_from_adjustment(plan)
        self.assertIsNotNone(window)
        self.assertEqual(window["start"].date().isoformat(), "2026-01-01")
        self.assertEqual(window["source"], "user_adjustment")

    def test_days_only_adjustment_window(self):
        window = qr._window_from_adjustment({"time_window_adjustment": {"label": "近 90 天", "days": 90}})
        self.assertIsNotNone(window)
        self.assertGreaterEqual((window["end"] - window["start"]).days, 89)

    def test_no_adjustment_means_no_window(self):
        self.assertIsNone(qr._window_from_adjustment({}))
        self.assertIsNone(qr._window_from_adjustment({"time_window_adjustment": {}}))


class NoAdjustmentRegressionTests(unittest.TestCase):
    """用户没调整时，路径必须与改动前一致。"""

    def test_plain_question_has_no_adjustment_artifacts(self):
        planner = qp.QaQueryPlanner(pack_loader=_Loader())
        result = planner.plan({"question": "离岸信托新规怎么规定的", "industry_pack_id": "family_office"})
        self.assertEqual(result["output_form"], "")
        self.assertFalse(result["must_fetch_fulltext"])
        self.assertEqual(result["time_window_adjustment"], {})
        self.assertEqual(result["adjustment_receipt"], {})
        # 政策类问题自带"官方原文"检索式，但不得出现调整才会加的"全文逐条"专项式
        self.assertTrue(all("全文" not in q for q in result["queries"]),
                        "没调整就不该出现全文专项检索式：%r" % result["queries"])

    def test_agreement_uses_confirm_branch_and_skips_llm_parser(self):
        calls = []

        def parser(payload):
            calls.append(payload)
            return {"operation": "augment", "retrieval_queries": ["不应被调用"]}

        planner = qp.QaQueryPlanner(pack_loader=_Loader(), adjustment_parser=parser)
        result = planner.plan({
            "question": "\n\n".join([
                "原始问题：离岸信托新规怎么规定的",
                "此前回答计划：先核验政策原文",
                "用户确认：同意，继续",
                "请沿用此前问题分析思路继续生成答案。",
            ]),
            "industry_pack_id": "family_office",
        })
        self.assertEqual(calls, [], "确认分支不得调用 LLM 解析器")
        self.assertEqual(result["output_form"], "")
        self.assertIn("确认", result["question_plan"]["answer_strategy"])


class PatchNormalisationTests(unittest.TestCase):
    """LLM 解析器给出的新字段也必须被归一化（不能原样信）。"""

    def test_llm_patch_fields_are_used(self):
        patch = {
            "operation": "augment",
            "retrieval_queries": ["银保监 信托新规 原文", "信托新规 政策全称 发文字号"],
            "output_form": "paragraph_by_paragraph",
            "must_fetch_fulltext": True,
            "time_window": {"label": "近 6 个月"},
        }
        normalized = qp._normalize_adjustment_patch(patch, SUBQUESTIONS)
        self.assertEqual(normalized["retrieval_queries"],
                         ["银保监 信托新规 原文", "信托新规 政策全称 发文字号"])
        self.assertEqual(normalized["output_form"], "paragraph_by_paragraph")
        self.assertTrue(normalized["must_fetch_fulltext"])
        self.assertEqual(normalized["time_window"]["days"], 180)

    def test_unknown_output_form_is_dropped(self):
        normalized = qp._normalize_adjustment_patch({"output_form": "powerpoint"}, SUBQUESTIONS)
        self.assertEqual(normalized["output_form"], "")

    def test_retrieval_queries_are_capped(self):
        normalized = qp._normalize_adjustment_patch(
            {"retrieval_queries": ["a 原文", "b 原文", "c 原文", "d 原文", "e 原文", "f 原文"]},
            SUBQUESTIONS)
        self.assertEqual(len(normalized["retrieval_queries"]), 4)

    def test_mismatched_patch_types_do_not_crash(self):
        for bad in (None, "text", 42, {"retrieval_queries": "不是数组"}, {"time_window": []}):
            with self.subTest(bad=repr(bad)[:20]):
                normalized = qp._normalize_adjustment_patch(bad, SUBQUESTIONS)
                self.assertIsInstance(normalized, dict)


if __name__ == "__main__":
    unittest.main()
