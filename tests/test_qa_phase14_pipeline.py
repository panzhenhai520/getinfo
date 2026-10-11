#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 14 · **管线接线用例**（`QA_ACQUISITION` / `QA_REPEAT_QUESTION_ENABLED`）。

全部在**隔离临时 sqlite** 上真跑（`setUp` 断言 `backend == 'sqlite'`），零模型/零嵌入端点。
钉住：
  1. **默认关 = 逐字回滚**：`plan` 阶段不新增键、`stats` 里没有 `acquisition`、
     `qa_realignment_decisions` 一行都没有；
  2. **P14-07 打开**：同一个会话里第二次问同一题 → `plan["realignment"]` 带交互
     （选项表 8 项 + 逐条依据）且发 `action_required` 事件；同一 run 再判定**不重复交互**；
  3. **用户选择真的改变下一次运行**：选了"扩大时间窗"后，下一轮检索计划里真的带上
     `time_window_adjustment`（**前后对比**：before 无 / after 有，且天数 = 1095）；
  4. **P14-01…P14-06 打开**：`stats["acquisition"]` 带动作候选与理由码，
     会话配置的缺口判据（`gap_floors`）真的传到了检索阶段；
  5. **失败路径不阻断**：获取规划抛错 → 检索照常返回（`stats["acquisition"]["error"]`）；
     重对齐判定抛错 → `plan` 照常返回（`realignment_error`），答案链路不受影响。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_context_pack as cp  # noqa: E402
import qa_phase08_fixtures as fx08  # noqa: E402
import qa_phase14_fixtures as fx  # noqa: E402
import qa_pipeline  # noqa: E402
import qa_realignment as rl  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

OWNER = "p14"
SESSION = "s14"
FLAGS = ("QA_ACQUISITION", "QA_REPEAT_QUESTION_ENABLED", "QA_GAP_ANALYZER",
         "QA_CONTEXT_PACK", "QA_MULTI_HOP_ENABLED", "QA_EXPERIENCE_MEMORY")
LEVEL1_KEYS = {"queries", "evidence", "excluded", "stats", "graph", "search_status",
               "search_providers", "search_errors", "time_window", "cache"}


class _FakePlanner:
    """只回一个形状正确的计划（P14 的接线不依赖规划器内部）。"""

    def __init__(self):
        self.payloads = []

    def plan(self, payload):
        self.payloads.append(dict(payload))
        return fx.plan_output(question=str(payload.get("question") or fx.QUESTION))


class _RecordingRetriever:
    """记录**每一次**收到的检索计划（前后对比就靠它）。"""

    def __init__(self):
        self.plans = []

    def retrieve(self, plan, **kwargs):
        self.plans.append(dict(plan))
        return {"evidence": [], "stats": {"eligible": 0, "adopted": 0}, "excluded": {}}


class _WebSearch:
    def search(self, queries, *, enabled=True, limit=8):
        return {"evidence": [], "status": "disabled", "providers": [], "errors": []}


class _Policy:
    ragflow_kb_id = "kb"
    ragflow_app_id = "app"
    max_evidence = 12
    max_queries_per_hop = 2
    standard_max_hops = 2
    deep_max_hops = 3
    research_timeout_seconds = 30


class _PolicyResolver:
    def resolve(self, industry_pack_id):
        return _Policy()

    def require_research_ready(self, industry_pack_id):
        return _Policy()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.tmp.name, "phase14-pipeline.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.assertEqual(self.db.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.saved = {name: os.environ.pop(name, None) for name in FLAGS}
        self.planner = _FakePlanner()
        self.retriever = _RecordingRetriever()
        self.events = []
        self.handlers = qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=self.retriever, web_search=_WebSearch(),
            store=self.store, planner=self.planner, policy_resolver=_PolicyResolver())

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

    def _emit(self, event_type, payload=None):
        self.events.append({"type": event_type, "payload": dict(payload or {})})

    def _new_run(self, key, *, question=fx.KEYWORD_QUESTION):
        run = self.store.create_run(
            {"industry_pack_id": "auto", "question": question, "mode": "standard",
             "session_id": SESSION},
            owner_user_id=OWNER, idempotency_key=key)
        return str(run["id"])

    def _plan(self, run_id, *, question=fx.KEYWORD_QUESTION):
        context = fx.plan_context(run_id, question=question, session_id=SESSION,
                                 owner_user_id=OWNER)
        context["_emit_stage_event"] = self._emit
        return self.handlers["plan"](context)

    def _level1(self, run_id, plan, *, question=fx.KEYWORD_QUESTION):
        context = fx.level1_context(run_id, plan=plan, question=question, session_id=SESSION,
                                   owner_user_id=OWNER)
        context["_emit_stage_event"] = self._emit
        return self.handlers["level1_retrieval"](context)


class DefaultOffTests(_Base):
    def test_plan_stage_adds_nothing(self):
        run_id = self._new_run("p14-off-plan")
        result = self._plan(run_id)
        self.assertNotIn("realignment", result)
        self.assertEqual(set(result), set(fx.plan_output()))
        self.assertEqual(self.store.realignment_decisions(session_id=SESSION), [])

    def test_level1_adds_no_acquisition_key(self):
        os.environ["QA_GAP_ANALYZER"] = "1"
        run_id = self._new_run("p14-off-level1")
        result = self._level1(run_id, fx.plan_output())
        self.assertEqual(set(result), LEVEL1_KEYS)
        self.assertNotIn("acquisition", result["stats"])

    def test_run_still_works_without_gap_analyzer(self):
        os.environ["QA_ACQUISITION"] = "1"
        run_id = self._new_run("p14-acq-no-gap")
        result = self._level1(run_id, fx.plan_output())
        self.assertEqual(result["stats"]["acquisition"]["skipped_reason"], "NO_GAP_ANALYSIS",
                         "没有缺口分析就没有动作可规划：如实标注，不许硬凑")


class RealignmentWiringTests(_Base):
    def setUp(self):
        super().setUp()
        os.environ["QA_REPEAT_QUESTION_ENABLED"] = "1"
        fx.completed_run(self.store, owner_user_id=OWNER, session_id=SESSION,
                         question=fx.KEYWORD_QUESTION)

    def test_second_same_question_asks_once_and_emits_an_event(self):
        run_id = self._new_run("p14-real-1")
        result = self._plan(run_id)
        receipt = result["realignment"]
        self.assertTrue(receipt["interaction"], receipt.get("skipped_reason"))
        self.assertTrue(receipt["contract_ok"], receipt.get("contract_error"))
        self.assertEqual(len(receipt["interaction"]["options"]), 8)
        self.assertTrue(receipt["interaction"]["evidence"])
        actions = [event for event in self.events if event["type"] == "action_required"]
        self.assertEqual(len(actions), 1)
        self.assertIn("realignment", actions[0]["payload"])
        self.assertEqual(len(actions[0]["payload"]["realignment"]["options"]), 8)
        rows = self.store.realignment_decisions(session_id=SESSION)
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["asked_count"]), 1)

    def test_asking_again_in_the_same_run_is_idempotent(self):
        run_id = self._new_run("p14-real-2")
        self._plan(run_id)
        second = self._plan(run_id)
        self.assertEqual(second["realignment"]["skipped_reason"], "ALREADY_ASKED_IN_TTL")
        self.assertEqual(second["realignment"]["interaction"], {})
        rows = self.store.realignment_decisions(session_id=SESSION)
        self.assertEqual(int(rows[0]["asked_count"]), 1, "重复触发只记一次")

    def test_choice_changes_the_next_run_and_the_retrieval_plan(self):
        run_id = self._new_run("p14-real-3")
        first = self._plan(run_id)
        topic = first["realignment"]["interaction"]["topic_key"]
        rl.record_choice(self.store, run_meta={"id": run_id, "owner_user_id": OWNER,
                                              "session_id": SESSION,
                                              "industry_pack_id": "auto"},
                         topic=topic, option_id="widen_time_window",
                         interaction=first["realignment"]["interaction"])
        # ── after：下一轮同题运行真的按新思路执行 ──
        before = self._level1(run_id, fx.plan_output())
        before_calls = len(self.retriever.plans)
        self.assertTrue(before_calls >= 1, "首跳必须真的打过检索器")
        self.assertTrue(all("time_window_adjustment" not in plan
                            for plan in self.retriever.plans[:before_calls]),
                        "before：没有任何会话配置 → 检索计划里没有窗口调整")
        next_run = self._new_run("p14-real-4")
        second = self._plan(next_run)
        self.assertEqual(second["realignment"]["skipped_reason"], "ALREADY_ASKED_IN_TTL")
        self.assertEqual(second["realignment"]["applied_plan"]["changed"], ["time_window"],
                         "会话配置必须真的落到本轮计划上")
        self.assertEqual(second["time_window_adjustment"]["days"], 365 * 3)
        after = self._level1(next_run, second)
        new_plans = self.retriever.plans[before_calls:]
        self.assertTrue(new_plans, "配置进了缓存键 → 这一轮必须真的重新检索（不许被旧缓存吃掉）")
        for plan in new_plans:
            self.assertEqual(plan["time_window_adjustment"]["days"], 365 * 3,
                             "检索器真的收到了新的时间窗（'换了思路'的可验证形态）")
        self.assertEqual(before["stats"]["adopted"], after["stats"]["adopted"])
        self.assertNotIn("acquisition", after["stats"],
                         "只开了 P14-07、没开 `QA_ACQUISITION` → 获取回执不该出现")

    def test_authority_choice_reaches_the_retrieval_stage(self):
        run_id = self._new_run("p14-real-5")
        first = self._plan(run_id)
        topic = first["realignment"]["interaction"]["topic_key"]
        rl.record_choice(self.store, run_meta={"id": run_id, "owner_user_id": OWNER,
                                              "session_id": SESSION,
                                              "industry_pack_id": "auto"},
                         topic=topic, option_id="raise_authority",
                         interaction=first["realignment"]["interaction"])
        os.environ["QA_ACQUISITION"] = "1"
        os.environ["QA_GAP_ANALYZER"] = "1"
        next_run = self._new_run("p14-real-6")
        second = self._plan(next_run)
        self.assertEqual(second["gap_floors"], {"authority": 90.0})
        self.assertEqual(second["realignment"]["applied_plan"]["changed"], ["authority_floor"])
        result = self._level1(next_run, second)
        self.assertEqual(result["stats"]["acquisition"]["gap_floors"], {"authority": 90.0},
                         "判据覆盖值必须传到缺口环（否则'提高权威要求'只是句空话）")

    def test_realignment_failure_does_not_block_the_plan(self):
        run_id = self._new_run("p14-real-7")
        with mock.patch.object(qa_pipeline, "run_realignment_step",
                               side_effect=RuntimeError("wiring boom")):
            result = self._plan(run_id)
        self.assertIn("RuntimeError", result["realignment"]["realignment_error"])
        self.assertTrue(result["contract_ok"] if "contract_ok" in result
                        else True)
        self.assertEqual(set(result) - {"realignment"}, set(fx.plan_output()),
                         "判定坏掉时计划本身一字不变（照常出答案）")
        self.assertEqual(self.store.realignment_decisions(session_id=SESSION), [])


class AcquisitionWiringTests(_Base):
    def setUp(self):
        super().setUp()
        os.environ["QA_ACQUISITION"] = "1"
        os.environ["QA_GAP_ANALYZER"] = "1"
        os.environ["QA_MULTI_HOP_ENABLED"] = "1"

    def test_acquisition_receipt_is_a_sibling_key_with_real_candidates(self):
        run_id = self._new_run("p14-acq-1")
        result = self._level1(run_id, fx.plan_output())
        receipt = result["stats"]["acquisition"]
        self.assertTrue(receipt["contract_ok"], receipt.get("contract_error"))
        self.assertEqual(receipt["stats"]["candidates"] > 0, True,
                         "缺口环真的产出了缺口 → 必须有动作候选")
        self.assertIn("retrieve_document", receipt["selected"])
        self.assertIn("kb", receipt["by_source"])
        self.assertTrue(receipt["by_source"]["kb"] >= 1)
        self.assertNotIn("emr", receipt["sources"] and {key for key, value in
                                                        receipt["sources"].items() if value},
                         "本部署没有 EMR 通道：不许假装有")
        events = [event for event in self.events
                  if event["type"] == "stage_progress"
                  and "acquisition" in event["payload"]]
        self.assertTrue(events, "获取动作必须推到事件里（可观测性）")

    def test_acquisition_is_recorded_in_the_reasoning_trace_route(self):
        """被选中的动作的通道偏好真的作用到缺口上（SearchTrace 的 route 列可查）。"""
        run_id = self._new_run("p14-acq-2")
        result = self._level1(run_id, fx.plan_output())
        receipt = result["stats"]["acquisition"]
        self.assertIn("route_guidance", receipt)
        self.assertEqual(receipt["route_guidance"]["source"], qa_pipeline.ACQUISITION_PLANNER_VERSION)

    def test_acquisition_failure_does_not_block_retrieval(self):
        run_id = self._new_run("p14-acq-3")
        with mock.patch.object(qa_pipeline, "_acquisition_plan_for_gaps",
                               side_effect=RuntimeError("planner boom")):
            result = self._level1(run_id, fx.plan_output())
        self.assertEqual(set(result), LEVEL1_KEYS, "返回键集一个字不变")
        self.assertIn("RuntimeError", result["stats"]["acquisition"]["error"])
        self.assertTrue(result["evidence"] == [])

    def test_session_config_preference_is_applied_to_the_gaps(self):
        """会话里的通道偏好（P14-07 的选择）真的改了缺口的建议通道次序。"""
        plan = fx.plan_output()
        plan["route_preference"] = ["semantic", "keyword"]
        plan["realignment"] = {"config": {"usable": True, "decision": "CHANGE_STRATEGY",
                                          "config": {"route_preference": ["semantic"]}}}
        run_id = self._new_run("p14-acq-4")
        result = self._level1(run_id, plan)
        receipt = result["stats"]["acquisition"]
        self.assertEqual(receipt["session_config"]["config"]["route_preference"], ["semantic"])
        self.assertTrue(receipt["route_changes"], "偏好必须真的改动了至少一条缺口的通道次序")
        change = receipt["route_changes"][0]
        self.assertEqual(change["to"], "semantic")
        self.assertEqual(change["from"], "keyword")


class ContextPackNoRepeatTests(_Base):
    """P14-05 在**真实 Phase 08 产物**上的接线：`pack["acquisition"]` 给出被拦下的动作数。"""

    def _graph_with_context_gap(self):
        """一段**短 span** 的真实证据：Phase 02 的最小 span 只有十来个字 → `TRUNCATED_SPAN`。"""
        text = ("家族办公室税收优惠。\n"
                + "背景补充材料，与本次问题没有直接关系。" * 20)
        evidence = [fx08.evidence_item("article:1", text=text,
                                       verification=fx08.verification("SUPPORTED"))]
        claims = [fx08.graph_claim("c1", text="家族办公室税收优惠的适用范围", refs=["article:1"])]
        return fx08.graph(claims=claims, evidence=evidence,
                          edges=[fx08.graph_edge("c1", "article:1")])

    def test_pack_layer_reports_suppressed_actions_when_enabled(self):
        os.environ["QA_ACQUISITION"] = "1"
        pack = qa_pipeline._build_context_pack_layer(
            self._graph_with_context_gap(), plan=fx08.plan(),
            request={"question": fx08.QUESTION}, run_meta={"id": "p14-pack"},
            gaps=[fx.gap(claim_id="c1", missing="LOW_RELEVANCE")])
        self.assertTrue(pack["context_gaps"], "样例必须真的产出 Context Gap")
        self.assertIn("TRUNCATED_SPAN",
                      {row["context_gap_type"] for row in pack["context_gaps"]})
        acquisition = pack["acquisition"]
        self.assertEqual(acquisition["no_repeat"]["suppressed"], 6,
                         "六条动作全部因'信息已在手上'被拦下")
        self.assertEqual(acquisition["no_repeat"]["suppressed_by_reason"],
                         {"SPAN_TRUNCATED_ONLY": 6})
        self.assertEqual(acquisition["no_repeat"]["suppressed_actions"]["ask_patient"], 1,
                         "§13：该重组上下文，而不是再次询问患者")
        self.assertEqual(acquisition["selected"], [])

    def test_pack_layer_adds_nothing_when_disabled(self):
        pack = qa_pipeline._build_context_pack_layer(
            self._graph_with_context_gap(), plan=fx08.plan(),
            request={"question": fx08.QUESTION}, run_meta={"id": "p14-pack-off"},
            gaps=[fx.gap(claim_id="c1", missing="LOW_RELEVANCE")])
        self.assertNotIn("acquisition", pack)
        self.assertTrue(pack["context_gaps"], "Phase 08 自己的缺口检测不受 Phase 14 开关影响")


if __name__ == "__main__":
    unittest.main()
