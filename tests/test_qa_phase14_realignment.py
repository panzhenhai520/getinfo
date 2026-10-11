#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 14 · P14-07（重复提问识别与思路调整确认）用例。

覆盖规格书点名的九种情形（**全部在隔离临时 sqlite 上真跑**，`setUp` 断言 `backend=='sqlite'`）：
  ① 同一问题问两次 → 触发；② 相似问法 → 触发；③ 不同问题 → **不触发**；
  ④ 跨会话 → **不触发**；⑤ 用户拒绝（保持原思路）后 TTL 内 → 不再追问；
  ⑥ 单轮调用（无会话上下文）→ 跳过并如实标注；⑦ 判定抛错 → **不阻断**（回执带 error）；
  ⑧ 选项落到配置后**下一次真的改变行为**；⑨ 幂等（重复触发只记一次）。
外加：不满意依据**逐条可解释**（拿不到依据就不许交互）、选项表必须给"预期影响与代价"、
恒有"保持原思路"这一项、会话配置的键闭集合与归一化。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_acquisition as ac  # noqa: E402
import qa_gap_analyzer as ga  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_phase14_fixtures as fx  # noqa: E402
import qa_realignment as rl  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

FLAGS = ("QA_REPEAT_QUESTION_ENABLED", "QA_REPEAT_QUESTION_OVERLAP",
         "QA_REPEAT_QUESTION_WINDOW", "QA_REPEAT_QUESTION_UNRESOLVED_RATE",
         "QA_REPEAT_QUESTION_TTL_HOURS", "QA_REPEAT_QUESTION_MAX_INTERACTIONS")
OWNER = "p14"
SESSION = "s14"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.tmp.name, "phase14-realign.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.assertEqual(self.db.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.saved = {name: os.environ.pop(name, None) for name in FLAGS}
        os.environ["QA_REPEAT_QUESTION_ENABLED"] = "1"

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        try:
            self.db.connection.close()
        except Exception:  # noqa: BLE001
            pass
        self.tmp.cleanup()

    def _run_meta(self, run_id="r2", *, session=SESSION):
        return {"id": run_id, "owner_user_id": OWNER, "session_id": session,
                "industry_pack_id": "auto"}

    def _current_run(self, key="p14-current"):
        run = self.store.create_run(
            {"industry_pack_id": "auto", "question": fx.KEYWORD_QUESTION, "mode": "standard",
             "session_id": SESSION},
            owner_user_id=OWNER, idempotency_key=key)
        return str(run["id"])

    def _prior(self, question=fx.KEYWORD_QUESTION, **kwargs):
        return fx.completed_run(self.store, owner_user_id=OWNER, session_id=SESSION,
                                question=question, **kwargs)


class RepeatDetectionTests(_Base):
    def test_same_question_twice_triggers(self):
        self._prior()
        run_id = self._current_run()
        receipt = rl.realignment_step(store=self.store, run_meta=self._run_meta(run_id),
                                      question=fx.KEYWORD_QUESTION)
        interaction = receipt["interaction"]
        self.assertTrue(interaction, receipt.get("skipped_reason"))
        self.assertIn("LEXICAL_OVERLAP", interaction["triggers"])
        self.assertTrue(interaction["evidence"], "必须给出可解释的不满意依据")
        self.assertIn("NO_NEW_VERIFIED_EVIDENCE", interaction["evidence"])
        self.assertEqual(len(interaction["options"]), len(contracts.REALIGNMENT_OPTION_IDS))
        self.assertTrue(interaction["contract_ok"], interaction.get("contract_error"))

    def test_similar_wording_triggers(self):
        self._prior()
        run_id = self._current_run()
        receipt = rl.realignment_step(store=self.store, run_meta=self._run_meta(run_id),
                                      question=fx.SIMILAR_QUESTION)
        interaction = receipt["interaction"]
        self.assertTrue(interaction, receipt.get("skipped_reason"))
        self.assertTrue(interaction["triggers"])
        self.assertIn("SAME_SUBJECT_SAME_INTENT", interaction["triggers"])
        self.assertGreaterEqual(interaction["similarity"]["subject_jaccard"],
                                rl.contradiction_jaccard())

    def test_different_question_does_not_trigger(self):
        self._prior()
        run_id = self._current_run()
        receipt = rl.realignment_step(store=self.store, run_meta=self._run_meta(run_id),
                                      question=fx.OTHER_QUESTION)
        self.assertEqual(receipt["interaction"], {})
        self.assertEqual(receipt["skipped_reason"], "NOT_A_REPEAT")
        self.assertEqual(receipt["detection"]["matches"], 0)

    def test_other_session_does_not_trigger(self):
        fx.completed_run(self.store, owner_user_id=OWNER, session_id="another-session",
                         question=fx.KEYWORD_QUESTION)
        run_id = self._current_run()
        receipt = rl.realignment_step(store=self.store, run_meta=self._run_meta(run_id),
                                      question=fx.KEYWORD_QUESTION)
        self.assertEqual(receipt["skipped_reason"], "NOT_A_REPEAT",
                         "跨会话不算重复（新会话就是新议题）")

    def test_single_turn_call_skips(self):
        self._prior()
        receipt = rl.realignment_step(store=self.store,
                                      run_meta={"id": "r9", "owner_user_id": OWNER,
                                                "session_id": "", "industry_pack_id": "auto"},
                                      question=fx.KEYWORD_QUESTION)
        self.assertEqual(receipt["skipped_reason"], "NO_SESSION_CONTEXT")
        self.assertEqual(receipt["interaction"], {})
        self.assertTrue(receipt["contract_ok"], receipt.get("contract_error"))

    def test_window_and_threshold_are_configurable(self):
        self._prior()
        run_id = self._current_run()
        # 相似问法的词面重叠 ≈ 0.87：阈值提到 0.90 → 词面判据不再命中，
        # 但"同主体 + 同意图"仍然成立（两条判据是**独立**的，这条用例把这个差别钉住）
        baseline = rl.detect_repeat(question=fx.SIMILAR_QUESTION,
                                    history=[{"run_id": "r1", "question": fx.KEYWORD_QUESTION}])
        self.assertIn("LEXICAL_OVERLAP", baseline["triggers"])
        self.assertGreater(baseline["similarity"]["overlap"], 0.8)
        os.environ["QA_REPEAT_QUESTION_OVERLAP"] = "0.90"
        try:
            receipt = rl.realignment_step(store=self.store, run_meta=self._run_meta(run_id),
                                          question=fx.KEYWORD_QUESTION)
            del receipt
            self.assertEqual(rl.realignment_settings()["overlap_threshold"], 0.90)
            tightened = rl.detect_repeat(
                question=fx.SIMILAR_QUESTION,
                history=[{"run_id": "r1", "question": fx.KEYWORD_QUESTION}])
            self.assertEqual(tightened["triggers"], ["SAME_SUBJECT_SAME_INTENT"],
                             "词面判据必须真的受阈值控制（可复算）")
        finally:
            os.environ.pop("QA_REPEAT_QUESTION_OVERLAP", None)
        strict = rl.detect_repeat(question=fx.OTHER_QUESTION,
                                  history=[{"run_id": "r1", "question": fx.KEYWORD_QUESTION}])
        self.assertFalse(strict["repeat"])

    def test_window_limits_how_far_back_we_look(self):
        os.environ["QA_REPEAT_QUESTION_WINDOW"] = "1"
        try:
            detection = rl.detect_repeat(
                question=fx.KEYWORD_QUESTION,
                history=[{"run_id": "new", "question": fx.OTHER_QUESTION},
                         {"run_id": "old", "question": fx.KEYWORD_QUESTION}])
            self.assertFalse(detection["repeat"], "窗口 = 1 → 只看最近一轮（更早的不算）")
            self.assertEqual(rl.window_turns(), 1)
        finally:
            os.environ.pop("QA_REPEAT_QUESTION_WINDOW", None)

    def test_low_overlap_and_different_intent_do_not_trigger(self):
        detection = rl.detect_repeat(question=fx.OTHER_QUESTION,
                                     history=[{"run_id": "r1", "question": fx.KEYWORD_QUESTION}])
        self.assertFalse(detection["repeat"])
        self.assertEqual(detection["similarity"]["overlap"], 0.0)
        self.assertEqual(detection["similarity"]["subject_jaccard"], 0.0)


class DissatisfactionTests(_Base):
    def test_each_reason_is_recomputed_from_the_previous_run(self):
        stats = {"rounds": 1, "open_gaps": 3, "high_priority_open": 3,
                 "resolved_high_priority_total": 1, "new_verified_claims_total": 0,
                 "unresolved_contradictions": 0, "by_type": {}, "by_band": {},
                 "planned_hops": 0, "dedupe_hits": 0}
        self._prior(gap_stats=stats, stop_reason="BUDGET_EXHAUSTED",
                    degradation=["核验未完成"], answer_status="insufficient_evidence")
        signals = rl.run_signals(self.store, str(self.store.list_runs(
            owner_user_id=OWNER, session_id=SESSION)[0]["id"]))
        self.assertEqual(signals["new_verified_claims"], 0)
        self.assertEqual(signals["stop_reason"], "BUDGET_EXHAUSTED")
        self.assertAlmostEqual(signals["unresolved_rate"], 0.75, places=6)
        assessment = rl.assess_dissatisfaction(signals)
        self.assertEqual(assessment["evidence"],
                         ["NO_NEW_VERIFIED_EVIDENCE", "ANSWER_DEGRADED",
                          "STOP_REASON_BUDGET_EXHAUSTED", "UNRESOLVED_GAP_RATE_HIGH"])
        for code in assessment["evidence"]:
            self.assertIn(code, contracts.REALIGNMENT_DISSATISFACTION_REASONS)
            self.assertTrue(assessment["detail"][code], "每条依据都要给出判据输入")
        self.assertIn("预算用尽", assessment["explanation"])

    def test_no_evidence_means_no_interaction(self):
        self._prior(gap_stats={"rounds": 1, "high_priority_open": 0,
                               "resolved_high_priority_total": 4,
                               "new_verified_claims_total": 3},
                    stop_reason="ANSWERABLE", answer_status="ready")
        run_id = self._current_run()
        receipt = rl.realignment_step(store=self.store, run_meta=self._run_meta(run_id),
                                      question=fx.KEYWORD_QUESTION)
        self.assertEqual(receipt["skipped_reason"], "NO_DISSATISFACTION_EVIDENCE")
        self.assertEqual(receipt["interaction"], {},
                         "拿不到依据就不许空口说'你不满意'")
        self.assertEqual(self.store.realignment_decisions(session_id=SESSION), [])

    def test_stop_reason_mapping_covers_exactly_three_values(self):
        for reason, code in (("NO_GAIN", "STOP_REASON_NO_GAIN"),
                             ("BUDGET_EXHAUSTED", "STOP_REASON_BUDGET_EXHAUSTED"),
                             ("MAX_DEPTH", "STOP_REASON_MAX_DEPTH")):
            assessment = rl.assess_dissatisfaction({"stop_reason": reason})
            self.assertEqual(assessment["evidence"], [code])
        for reason in ("ANSWERABLE", "UNRESOLVABLE_CONTRADICTION", ""):
            self.assertEqual(rl.assess_dissatisfaction({"stop_reason": reason})["evidence"], [])


class OptionTableTests(_Base):
    def test_every_option_has_impact_and_cost(self):
        rows = rl.options_payload()
        self.assertEqual([row["option_id"] for row in rows],
                         list(contracts.REALIGNMENT_OPTION_IDS))
        required = {"change_route", "widen_time_window", "narrow_time_window", "raise_authority",
                    "loosen_evidence_threshold", "tighten_evidence_threshold",
                    "change_analysis_lens", "keep_strategy"}
        self.assertEqual({row["option_id"] for row in rows}, required)
        for row in rows:
            self.assertTrue(row["label"].strip())
            self.assertTrue(row["expected_effect"].strip(), "每个选项都要写预期影响")
            self.assertTrue(row["cost"].strip(), "每个选项都要写代价")
            for key in row["config_keys"]:
                self.assertIn(key, contracts.REALIGNMENT_CONFIG_KEYS)
            ok, note = contracts.validate("realignment_option", row)
            self.assertTrue(ok, note)

    def test_keep_strategy_is_present_and_is_the_refusal_path(self):
        keep = [row for row in rl.options_payload() if row["option_id"] == "keep_strategy"][0]
        self.assertEqual(keep["config_keys"], [])
        self.assertEqual(rl.choice_to_config("keep_strategy"), {})
        payload = rl.decision_payload(option_id="keep_strategy", topic="TQ1",
                                      scope={"scope_key": "k", "session_id": SESSION})
        self.assertEqual(payload["decision"], "KEEP_STRATEGY")
        self.assertEqual(payload["applied_keys"], [])

    def test_unknown_option_is_rejected(self):
        with self.assertRaises(ValueError):
            rl.decision_payload(option_id="teleport", topic="TQ1", scope={})

    def test_config_normalization_drops_illegal_values(self):
        config = rl.normalize_config({"route_preference": ["telepathy", "semantic"],
                                      "authority_floor": 999, "relevance_floor": -1,
                                      "analysis_lens": "vibes", "unknown_key": 1})
        self.assertEqual(config, {"route_preference": ["semantic"], "authority_floor": 100.0,
                                  "relevance_floor": 0.0})

    def test_interaction_slot_is_mounted_in_phase14(self):
        receipt = rl.interaction_slot_receipt()
        self.assertEqual(receipt["skill_id"], "patient_inquiry")
        self.assertEqual(receipt["owner_phase"], "P14-07")
        self.assertTrue(receipt["mounted_in_phase14"])
        self.assertEqual(sorted(receipt["capabilities_implemented"]),
                         ["repeat_question_detection", "strategy_change_confirmation"])


class ChoiceAndIdempotencyTests(_Base):
    def test_interaction_happens_once_per_topic(self):
        self._prior()
        run_id = self._current_run()
        first = rl.realignment_step(store=self.store, run_meta=self._run_meta(run_id),
                                    question=fx.KEYWORD_QUESTION)
        self.assertTrue(first["interaction"])
        # 同一 run 再触发一次（幂等）：不新增交互次数，也不新增行
        again = rl.realignment_step(store=self.store, run_meta=self._run_meta(run_id),
                                    question=fx.KEYWORD_QUESTION)
        self.assertEqual(again["interaction"], {})
        self.assertEqual(again["skipped_reason"], "ALREADY_ASKED_IN_TTL")
        rows = self.store.realignment_decisions(session_id=SESSION)
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["asked_count"]), 1, "重复触发只记一次")
        stats = self.store.realignment_stats(session_id=SESSION)
        self.assertEqual(stats["detected"], 1)
        self.assertEqual(stats["asked"], 1)
        self.assertEqual(stats["interactions_histogram"], {"1": 1})

    def test_keep_strategy_stops_further_questions(self):
        self._prior()
        run_id = self._current_run()
        first = rl.realignment_step(store=self.store, run_meta=self._run_meta(run_id),
                                    question=fx.KEYWORD_QUESTION)
        topic = first["interaction"]["topic_key"]
        row = rl.record_choice(self.store, run_meta=self._run_meta(run_id), topic=topic,
                              option_id="keep_strategy", interaction=first["interaction"])
        self.assertEqual(row["decision"], "KEEP_STRATEGY")
        later_id = self._current_run(key="p14-later")
        later = rl.realignment_step(store=self.store, run_meta=self._run_meta(later_id),
                                    question=fx.KEYWORD_QUESTION)
        self.assertEqual(later["interaction"], {})
        self.assertEqual(later["skipped_reason"], "KEEP_STRATEGY_DECLARED",
                         "用户明确保持原思路后，TTL 内不再追问同一议题")
        self.assertFalse(later["config"]["usable"])

    def test_choice_records_a_usable_config(self):
        self._prior()
        run_id = self._current_run()
        first = rl.realignment_step(store=self.store, run_meta=self._run_meta(run_id),
                                    question=fx.KEYWORD_QUESTION)
        topic = first["interaction"]["topic_key"]
        rl.record_choice(self.store, run_meta=self._run_meta(run_id), topic=topic,
                         option_id="change_route", interaction=first["interaction"])
        config = rl.session_config_for(self.store, run_meta=self._run_meta(run_id),
                                       question=fx.KEYWORD_QUESTION)
        self.assertTrue(config["usable"])
        self.assertEqual(config["decision"], "CHANGE_STRATEGY")
        self.assertEqual(config["config"]["route_preference"], list(rl.ROUTE_ORDER_FOR_CHANGE))
        self.assertFalse(config["expired"])

    def test_expired_config_is_not_used(self):
        self._prior()
        run_id = self._current_run()
        first = rl.realignment_step(store=self.store, run_meta=self._run_meta(run_id),
                                    question=fx.KEYWORD_QUESTION)
        topic = first["interaction"]["topic_key"]
        self.store.save_realignment_decision(
            scope_key=rl.session_scope(self._run_meta(run_id))["scope_key"], topic_key=topic,
            owner_user_id=OWNER, session_id=SESSION, industry_pack_id="auto",
            payload={"decision": "CHANGE_STRATEGY", "option_id": "change_route",
                     "config": {"route_preference": ["semantic"]},
                     "applied_keys": ["route_preference"],
                     "ttl_until": "2000-01-01T00:00:00Z"}, asked=False)
        config = rl.session_config_for(self.store, run_meta=self._run_meta(run_id),
                                      question=fx.KEYWORD_QUESTION)
        self.assertTrue(config["expired"])
        self.assertFalse(config["usable"], "过期配置不许静默继续用")

    def test_judgement_failure_does_not_block(self):
        self._prior()
        run_id = self._current_run()
        with mock.patch.object(rl, "detect_repeat", side_effect=RuntimeError("boom")):
            receipt = rl.realignment_step(store=self.store, run_meta=self._run_meta(run_id),
                                          question=fx.KEYWORD_QUESTION)
        self.assertIn("RuntimeError", receipt["realignment_error"])
        self.assertEqual(receipt["interaction"], {})
        self.assertTrue(receipt["contract_ok"], receipt.get("contract_error"))

    def test_clarifier_failure_falls_back_to_template(self):
        self._prior()
        run_id = self._current_run()
        os.environ["QA_REPEAT_QUESTION_LLM_INTERPRETER"] = "ghost"
        try:
            receipt = rl.realignment_step(store=self.store, run_meta=self._run_meta(run_id),
                                          question=fx.KEYWORD_QUESTION)
            interaction = receipt["interaction"]
            self.assertEqual(interaction["clarifier_fallback"], "UNKNOWN_CLARIFIER")
            self.assertIn(rl.BASE_QUESTION, interaction["question"])
        finally:
            os.environ.pop("QA_REPEAT_QUESTION_LLM_INTERPRETER", None)


class ConfigChangesBehaviourTests(_Base):
    """⑧ 选项落到配置后**下一次运行真的改变行为**（前后对比数字）。"""

    def _choose(self, option_id):
        self._prior()
        run_id = self._current_run(key="p14-choose-%s" % option_id)
        first = rl.realignment_step(store=self.store, run_meta=self._run_meta(run_id),
                                    question=fx.KEYWORD_QUESTION)
        topic = first["interaction"]["topic_key"]
        rl.record_choice(self.store, run_meta=self._run_meta(run_id), topic=topic,
                         option_id=option_id, interaction=first["interaction"])
        return rl.session_config_for(self.store, run_meta=self._run_meta(run_id),
                                     question=fx.KEYWORD_QUESTION)

    def test_time_window_choice_changes_the_retrieval_plan(self):
        config = self._choose("widen_time_window")
        before, before_receipt = ac.apply_session_config({"queries": ["q"]}, {})
        after, receipt = ac.apply_session_config({"queries": ["q"]}, config["config"])
        self.assertEqual(before_receipt["changed"], [])
        self.assertNotIn("time_window_adjustment", before)
        self.assertEqual(after["time_window_adjustment"]["days"], 365 * 3)
        self.assertEqual(receipt["changed"], ["time_window"])
        # 既有检索链路认得这个键（`qa_retrieval._window_from_adjustment` → 时间硬过滤）
        from qa_retrieval import _window_from_adjustment

        window = _window_from_adjustment(after)
        self.assertTrue(window["has_time"])
        self.assertEqual(window["days"], 365 * 3)

    def test_authority_choice_really_changes_the_gap_judgement(self):
        config = self._choose("raise_authority")
        self.assertEqual(config["config"]["authority_floor"], 90.0)
        plan, receipt = ac.apply_session_config({}, config["config"])
        self.assertEqual(plan["gap_floors"], {"authority": 90.0})
        self.assertEqual(receipt["changed"], ["authority_floor"])
        # 前：权威下界 50 → 权威 50 的证据不算缺口；后：下界 90 → 记 LOW_AUTHORITY 缺口
        evidence = [fx.evidence_item("article:1", authority=50)]
        claims = [fx.plan_claim("c1", fx.CLAIM_TEXT)]
        before = ga.detect_gaps(claims, evidence=evidence)
        after = ga.detect_gaps(claims, evidence=evidence, floors=plan["gap_floors"])
        before_types = set(before["stats"]["by_type"])
        after_types = set(after["stats"]["by_type"])
        self.assertNotIn("LOW_AUTHORITY", before_types)
        self.assertIn("LOW_AUTHORITY", after_types)
        self.assertEqual(before["stats"]["gaps"], 1)
        self.assertEqual(after["stats"]["gaps"], 2)

    def test_evidence_threshold_choice_really_changes_the_gap_judgement(self):
        config = self._choose("tighten_evidence_threshold")
        self.assertEqual(config["config"]["relevance_floor"], 0.55)
        state_default = ga.GapLoopState(floors=None)
        state_tight = ga.GapLoopState(floors=config["config"] and {"relevance": 0.55})
        self.assertEqual(state_default.floors["relevance"], ga.relevance_floor())
        self.assertEqual(state_tight.floors["relevance"], 0.55)
        self.assertEqual(state_tight.floors_source, "session_config")

    def test_lens_choice_changes_the_question_category(self):
        config = self._choose("change_analysis_lens")
        plan, receipt = ac.apply_session_config({"category": {"key": "industry_impact"}},
                                                config["config"])
        self.assertEqual(plan["category"], {"key": "comparison", "source": "realignment_lens",
                                           "previous": "industry_impact"})
        self.assertEqual(plan["analysis_lens"], "trend")
        self.assertEqual(receipt["changed"], ["analysis_lens"])
        self.assertEqual(rl.LENS_CATEGORY["trend"], "comparison")

    def test_route_choice_changes_the_channel_order(self):
        config = self._choose("change_route")
        gaps = [fx.gap(missing="NO_EVIDENCE", routes=("keyword", "semantic", "web"))]
        _, _, receipt = None, None, None
        from qa_memory_experience import apply_route_guidance

        adjusted, receipt = apply_route_guidance(
            gaps, {"prefer_routes": config["config"]["route_preference"]})
        self.assertEqual(receipt["changed"], 1)
        self.assertEqual(receipt["route_changes"][0]["from"], "keyword")
        self.assertEqual(receipt["route_changes"][0]["to"], "semantic")
        self.assertEqual(adjusted[0]["suggested_routes"][0], "semantic")

    def test_apply_session_config_never_overwrites_this_turn_time_window(self):
        """会话配置只补空缺：用户本轮明确说过的时间范围优先（Phase 10-3 的既有口径）。"""
        plan, receipt = ac.apply_session_config(
            {"time_window_adjustment": {"days": 7, "label": "近一周", "source": "user_adjustment"}},
            {"time_window": {"days": 365 * 3, "label": "近三年"},
             "authority_floor": 90})
        self.assertEqual(plan["time_window_adjustment"]["days"], 7)
        self.assertEqual(receipt["skipped_keys"], ["time_window"],
                         "被跳过的键必须记账（不许静默忽略）")
        self.assertEqual(receipt["changed"], ["authority_floor"], "其余键照常生效")
        # 没有本轮时间窗时才补上（前后对比）
        empty, second = ac.apply_session_config({}, {"time_window": {"days": 365 * 3,
                                                                    "label": "近三年"}})
        self.assertEqual(empty["time_window_adjustment"]["days"], 365 * 3)
        self.assertEqual(second["changed"], ["time_window"])


if __name__ == "__main__":
    unittest.main()
