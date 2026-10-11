#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 14 · P14-04（`ask-patient route`）用例。

钉住：
  1. 问诊候选是**规则模板**拼装的（零模型调用）；文案只说"缺什么、请给什么"，不诱导答案；
  2. 注入点（`register_clarifier` / 本模块的 `register_inquiry_writer`）**三条失败路径**
     全部回落模板并记账：未注册名 / 生成器抛错 / 返回非法载荷；
  3. **MASTER_RULES 11**：问诊候选恒 `is_evidence=False`、`in_citation_map=False`、
     `requires_revalidation=True` —— 患者陈述不是已验证证据；
  4. 权限位是 Phase 11 的 `patient_contact`，**默认被拒**（PERMISSION_DENIED）；
  5. 用户负担按缺口类型取档，超过上限时记 `OVER_USER_BURDEN`；开关关着记 `SOURCE_UNAVAILABLE`；
  6. 问诊 id 内容寻址（同缺口同问题 → 同 id），重复问同一问题会被拦（P14-05 的 no-repeat）。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_acquisition as ac  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_phase14_fixtures as fx  # noqa: E402

ALL_SOURCES = {"kb": True, "emr": True, "db": True, "graph": True, "web": True, "patient": True}
BURDEN_FLAGS = ("QA_ACQ_MAX_USER_BURDEN",)


class _Base(unittest.TestCase):
    def setUp(self):
        self.saved = {name: os.environ.pop(name, None) for name in BURDEN_FLAGS}

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class TemplateTests(_Base):
    def test_question_is_template_based_and_specific(self):
        for missing in ("MISSING_TIME_LINK", "MISSING_ENTITY_LINK", "AMBIGUOUS_ENTITY",
                        "NO_EVIDENCE", "MISSING_COUNTEREVIDENCE", "CONTRADICTION"):
            question = ac.patient_question_for(fx.gap(missing=missing))
            self.assertEqual(question["writer"], "template")
            self.assertEqual(question["fallback"], "")
            self.assertTrue(question["question_text"].strip())
            self.assertIn(question["question_text"], ac.PATIENT_QUESTION_TEMPLATES[missing])

    def test_unknown_gap_type_falls_back_to_claim_text(self):
        question = ac.patient_question_for(fx.gap(missing="MYSTERY", claim_text="某条结论正文"))
        self.assertIn("某条结论正文", question["question_text"])

    def test_inquiry_is_never_evidence(self):
        inquiry = ac.patient_inquiry_candidate(fx.gap(missing="MISSING_TIME_LINK"))
        self.assertIs(inquiry["is_evidence"], False)
        self.assertIs(inquiry["in_citation_map"], False)
        self.assertIs(inquiry["requires_revalidation"], True)
        self.assertEqual(inquiry["requires_permission"], "patient_contact")
        self.assertEqual(inquiry["slot_skill_id"], "patient_inquiry")
        self.assertEqual(inquiry["skill_owner_phase"], "P14-07")
        ok, note = contracts.validate("patient_inquiry", inquiry)
        self.assertTrue(ok, note)

    def test_inquiry_id_is_content_addressed(self):
        gap = fx.gap(missing="MISSING_TIME_LINK")
        first = ac.patient_inquiry_candidate(gap)
        second = ac.patient_inquiry_candidate(gap)
        self.assertEqual(first["inquiry_id"], second["inquiry_id"])
        other = ac.patient_inquiry_candidate(fx.gap(gap_id="GAP-9", missing="MISSING_TIME_LINK"))
        self.assertNotEqual(first["inquiry_id"], other["inquiry_id"],
                            "不同缺口不能共用一个问诊 id（否则幂等会误拦）")

    def test_asked_ids_mark_the_question_as_asked(self):
        gap = fx.gap(missing="MISSING_TIME_LINK")
        first = ac.patient_inquiry_candidate(gap)
        again = ac.patient_inquiry_candidate(gap, asked_ids=[first["inquiry_id"]])
        self.assertIs(again["asked_before"], True)


class InjectionPointTests(_Base):
    """可插拔"澄清问题生成器"：**默认不注册**，失败一律回落模板。"""

    def test_no_writer_registered_by_default(self):
        self.assertEqual(ac.inquiry_writers(), ())

    def test_registered_writer_is_used(self):
        ac.register_inquiry_writer("toy", lambda gap: {"question_text": "自定义问法"})
        try:
            question = ac.patient_question_for(fx.gap(), writer="toy")
            self.assertEqual(question["question_text"], "自定义问法")
            self.assertEqual(question["writer"], "toy")
        finally:
            ac._INQUIRY_WRITERS.pop("toy", None)

    def test_unknown_writer_falls_back(self):
        question = ac.patient_question_for(fx.gap(), writer="ghost")
        self.assertEqual(question["fallback"], "UNKNOWN_INQUIRY_WRITER")
        self.assertEqual(question["writer"], "template")
        self.assertIn(question["question_text"], ac.PATIENT_QUESTION_TEMPLATES.values())

    def test_raising_writer_falls_back(self):
        ac.register_inquiry_writer("boom", lambda gap: (_ for _ in ()).throw(RuntimeError("x")))
        try:
            question = ac.patient_question_for(fx.gap(), writer="boom")
            self.assertIn("RuntimeError", question["fallback"])
            self.assertEqual(question["writer"], "template")
        finally:
            ac._INQUIRY_WRITERS.pop("boom", None)

    def test_invalid_payload_falls_back(self):
        ac.register_inquiry_writer("empty", lambda gap: {"question_text": "   "})
        try:
            question = ac.patient_question_for(fx.gap(), writer="empty")
            self.assertEqual(question["fallback"], "INVALID_INQUIRY_PAYLOAD")
            self.assertEqual(question["writer"], "template")
        finally:
            ac._INQUIRY_WRITERS.pop("empty", None)


class GateTests(_Base):
    def test_permission_denied_is_recorded(self):
        plan = ac.plan_acquisition([fx.gap(missing="AMBIGUOUS_ENTITY")], sources=ALL_SOURCES,
                                   permissions={"patient_contact": False})
        row = [item for item in plan["candidates"] if item["action"] == "ask_patient"][0]
        self.assertEqual(row["decision"], "skipped")
        self.assertEqual(row["skip_reason"], "PERMISSION_DENIED")
        self.assertIs(row["inquiry"]["permission_granted"], False)
        self.assertNotIn("ask_patient", plan["selected"])

    def test_patient_source_switch_off_is_recorded(self):
        plan = ac.plan_acquisition([fx.gap(missing="AMBIGUOUS_ENTITY")],
                                   sources={"kb": True, "emr": False, "db": True,
                                            "graph": True, "web": True, "patient": False},
                                   permissions={"patient_contact": True})
        row = [item for item in plan["candidates"] if item["action"] == "ask_patient"][0]
        self.assertEqual(row["skip_reason"], "SOURCE_UNAVAILABLE")

    def test_over_user_burden_is_recorded(self):
        os.environ["QA_ACQ_MAX_USER_BURDEN"] = "low"
        plan = ac.plan_acquisition([fx.gap(missing="NO_EVIDENCE")], sources=ALL_SOURCES,
                                   permissions={"patient_contact": True})
        row = [item for item in plan["candidates"] if item["action"] == "ask_patient"][0]
        self.assertEqual(row["inquiry"]["user_burden"], "medium")
        self.assertEqual(row["skip_reason"], "OVER_USER_BURDEN")

    def test_burden_follows_the_gap_type(self):
        self.assertEqual(ac.action_user_burden("ask_patient",
                                               fx.gap(missing="MISSING_TIME_LINK")), "low")
        self.assertEqual(ac.action_user_burden("ask_patient",
                                               fx.gap(missing="MISSING_COUNTEREVIDENCE")),
                         "medium")
        self.assertEqual(ac.action_user_burden("retrieve_document", fx.gap()), "none")

    def test_knowledge_gaps_are_not_asked(self):
        for missing in ("LOW_AUTHORITY", "SINGLE_SOURCE", "MISSING_CAUSAL_BRIDGE"):
            plan = ac.plan_acquisition([fx.gap(missing=missing)], sources=ALL_SOURCES,
                                       permissions={"patient_contact": True})
            row = [item for item in plan["candidates"] if item["action"] == "ask_patient"][0]
            self.assertEqual(row["skip_reason"], "NOT_APPLICABLE_TO_GAP",
                             "%s 是知识型缺口：问人也拿不到（不该骚扰用户）" % missing)

    def test_duplicate_patient_question_is_not_asked_twice(self):
        gap = fx.gap(missing="MISSING_TIME_LINK")
        first = ac.patient_inquiry_candidate(gap)
        plan = ac.plan_acquisition([gap], sources=ALL_SOURCES,
                                   permissions={"patient_contact": True},
                                   asked_ids=[first["inquiry_id"]])
        row = [item for item in plan["candidates"] if item["action"] == "ask_patient"][0]
        self.assertEqual(row["decision"], "skipped")
        self.assertEqual(row["skip_reason"], "PATIENT_QUESTION_ALREADY_ASKED")


if __name__ == "__main__":
    unittest.main()
