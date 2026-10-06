#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务模板与能力声明（问题分析全面度）的回归测试。

对照用户给的案例：问"帮我检查当前网络环境（出口 IP / v2rayN 代理 / TUN / WebRTC / 时区撕裂）"时，
好的助手会先给"我按哪几组检查项做 + 只读不改 + 涉及密钥脱敏 + 哪些项需要本机探测"，
而不是只把问句拆成一个子问题。这里钉住这套结构，避免以后又被简化掉。
"""
import unittest

from qa_task_templates import DEFAULT_TEMPLATE, match_task_template, render_task_brief

ENV_QUESTION = (
    "我需要你帮我检查当前的网络环境。1.出口ip，2.代理服务v2rayn的设置是否合理，"
    "之前启用tun模式会不正常，虚拟网卡是不是设置有问题？3.可能出现webrtc泄露，"
    "终端代理残留导致时区撕裂"
)


class TaskTemplateMatchTests(unittest.TestCase):
    def test_environment_audit_question_matches_audit_template(self):
        template = match_task_template(ENV_QUESTION)
        self.assertEqual("system_environment_audit", template["key"])
        groups = [group["group"] for group in template["checklist"]]
        self.assertEqual(["出口与链路", "代理配置", "泄露与一致性"], groups)

    def test_audit_template_declares_boundary_and_capability(self):
        template = match_task_template(ENV_QUESTION)
        self.assertIn("只读", template["boundary"])
        self.assertIn("脱敏", template["boundary"])
        self.assertEqual("mixed", template["capability"])
        self.assertIn("本机环境", template["capability_note"])
        self.assertIn("不能替你在本机执行探测", template["capability_note"])

    def test_policy_full_text_question_matches_deep_read(self):
        template = match_task_template("这个政策全文里的第 12 条怎么规定的？逐段解释一下")
        self.assertEqual("document_deep_read", template["key"])
        self.assertIn("原文", template["boundary"])

    def test_comparison_question_matches_comparison(self):
        template = match_task_template("A 方案和 B 方案的优缺点对比")
        self.assertEqual("comparison", template["key"])

    def test_troubleshooting_question_matches_troubleshooting(self):
        template = match_task_template("这个采集任务为什么总是失败，帮我排查一下")
        self.assertEqual("troubleshooting", template["key"])

    def test_plain_industry_question_falls_back_to_default(self):
        template = match_task_template("香港家族办公室税务宽免政策的具体内容是什么？")
        self.assertEqual(DEFAULT_TEMPLATE["key"], template["key"])
        self.assertEqual("document_research", template["capability"])

    def test_empty_question_is_safe(self):
        template = match_task_template("")
        self.assertEqual(DEFAULT_TEMPLATE["key"], template["key"])
        self.assertTrue(template["checklist"])


class TaskBriefRenderTests(unittest.TestCase):
    def test_brief_contains_type_checklist_boundary_capability(self):
        lines = render_task_brief(match_task_template(ENV_QUESTION))
        text = "\n".join(lines)
        self.assertIn("【本次任务类型】环境/系统体检", text)
        self.assertIn("出口与链路", text)
        self.assertIn("【边界】", text)
        self.assertIn("【能力说明】", text)

    def test_brief_is_empty_for_empty_template(self):
        self.assertEqual([], render_task_brief({}))


class PlannerIntegrationTests(unittest.TestCase):
    def test_plan_carries_task_template_and_checklist(self):
        from qa_planner import QaQueryPlanner

        plan = QaQueryPlanner().plan({"question": ENV_QUESTION, "industry_pack_id": "family_office",
                                      "mode": "standard"})
        self.assertEqual("system_environment_audit", (plan.get("task_template") or {}).get("key"))
        question_plan = plan.get("question_plan") or {}
        self.assertTrue(question_plan.get("plan_checklist"), "计划里必须带检查清单")
        self.assertTrue(question_plan.get("plan_boundary"), "计划里必须带边界声明")
        self.assertEqual("mixed", question_plan.get("capability"))
        self.assertTrue(question_plan.get("capability_note"))

    def test_plan_rendering_includes_task_brief(self):
        from qa_planner import QaQueryPlanner
        from qa_question_templates import render_question_plan_answer

        plan = QaQueryPlanner().plan({"question": ENV_QUESTION, "industry_pack_id": "family_office",
                                      "mode": "standard"})
        rendered = render_question_plan_answer(plan.get("question_plan") or {})
        self.assertIn("【本次任务类型】", rendered)
        self.assertIn("【边界】", rendered)
        self.assertIn("【能力说明】", rendered)


if __name__ == "__main__":
    unittest.main()
