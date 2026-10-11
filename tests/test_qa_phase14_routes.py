#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 14 · P14-03（KB/EMR/DB/Web 路由）用例。

钉住 §33 的"四路统一获取"在本仓库的真实落点：
  1. 动作 → 通道只吃 **Phase 01 冻结的七个通道**（或空串），动作 → 来源只吃六个来源取值；
  2. 本部署**真的有什么**：KB 与 DB（政策登记表）在、图谱/Web 由本轮计划决定、
     EMR 需要部署方注入内部证据库（默认没有）、患者通道默认关；
  3. 缺口类型不同 → 选中的动作不同（同一批候选里"谁来拿"由 §13 的通道规则决定）；
  4. **通道偏好真的改变缺口的建议通道次序**（`route_guidance_from` + Phase 12 的同一条
     `apply_route_guidance`）：前后对比给出 `from → to`；
  5. 来源/权限闸门：不可用与未授权的动作**如实记理由码**，绝不硬凑一个动作。
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


class RoutingTableTests(unittest.TestCase):
    def test_action_to_source_and_route(self):
        self.assertEqual(ac.action_source("retrieve_document"), "kb")
        self.assertEqual(ac.action_route("retrieve_document"), contracts.QA_ROUTE_KEYWORD)
        self.assertEqual(ac.action_source("retrieve_emr"), "emr")
        self.assertEqual(ac.action_route("retrieve_emr"), "",
                         "EMR 不是 §2 冻结的七个检索通道之一 → 如实给空串")
        self.assertEqual(ac.action_source("query_database"), "db")
        self.assertEqual(ac.action_route("query_database"), contracts.QA_ROUTE_POLICY_EXACT)
        self.assertEqual(ac.action_source("graph_traverse"), "graph")
        self.assertEqual(ac.action_route("graph_traverse"), contracts.QA_ROUTE_GRAPH)
        self.assertEqual(ac.action_source("web_search"), "web")
        self.assertEqual(ac.action_route("web_search"), contracts.QA_ROUTE_WEB)
        self.assertEqual(ac.action_source("ask_patient"), "patient")
        self.assertEqual(ac.action_hunters("retrieve_document"), ["bm25"])
        self.assertEqual(ac.action_hunters("ask_patient"), [], "问诊不是检索，没有 Hunter")

    def test_hunters_match_phase07_mapping(self):
        import qa_gap_analyzer as ga

        for action, route in contracts.ACQUISITION_ACTION_ROUTES.items():
            if not route:
                continue
            expected = list(ga.HUNTER_BY_ROUTE.get(route) or ())
            self.assertEqual(ac.action_hunters(action), expected,
                             "%s 的 Hunter 身份必须与 Phase 07 的 HUNTER_BY_ROUTE 同源" % action)


class DeploymentSourceTests(unittest.TestCase):
    def test_defaults_reflect_this_deployment(self):
        saved = os.environ.pop("QA_ASK_PATIENT", None)
        try:
            sources = ac.deployment_sources(needs_web=False, has_graph=False)
            self.assertEqual(sources, {"kb": True, "emr": False, "db": True,
                                       "graph": False, "web": False, "patient": False})
            self.assertTrue(ac.deployment_sources(needs_web=True)["web"])
            self.assertTrue(ac.deployment_sources(has_graph=True)["graph"])
            self.assertTrue(ac.deployment_sources(has_emr=True)["emr"])
            self.assertFalse(ac.deployment_sources()["patient"], "患者通道默认关")
            os.environ["QA_ASK_PATIENT"] = "1"
            self.assertTrue(ac.deployment_sources()["patient"])
        finally:
            os.environ.pop("QA_ASK_PATIENT", None)
            if saved is not None:
                os.environ["QA_ASK_PATIENT"] = saved


class ActionSelectionTests(unittest.TestCase):
    def _selected(self, missing, **kwargs):
        plan = ac.plan_acquisition([fx.gap(missing=missing, **kwargs)],
                                   sources=ALL_SOURCES,
                                   permissions={"patient_contact": True, "emr_read": True})
        return plan["selected"], plan

    def test_document_gap_goes_to_kb(self):
        selected, plan = self._selected("NO_EVIDENCE")
        self.assertEqual(selected, ["retrieve_document"])
        self.assertEqual(plan["by_source"], {"kb": 1})

    def test_entity_link_gap_prefers_the_graph(self):
        selected, _ = self._selected("MISSING_ENTITY_LINK")
        self.assertEqual(selected, ["graph_traverse"],
                         "缺实体链路的建议通道是 graph/graph_attribute → 图遍历")

    def test_low_authority_gap_goes_to_the_structured_registry(self):
        selected, _ = self._selected("LOW_AUTHORITY")
        self.assertEqual(selected, ["query_database"],
                         "缺权威口径的建议通道是 policy_exact → 结构化登记表")

    def test_web_only_gap_falls_back_to_web(self):
        plan = ac.plan_acquisition([fx.gap(missing="NO_EVIDENCE")],
                                   sources={"kb": False, "emr": False, "db": False,
                                            "graph": False, "web": True, "patient": False})
        self.assertEqual(plan["selected"], ["web_search"])
        self.assertEqual(plan["by_source"], {"web": 1})

    def test_ambiguous_entity_can_be_asked(self):
        selected, plan = self._selected("AMBIGUOUS_ENTITY")
        self.assertEqual(selected, ["ask_patient"],
                         "实体歧义时问当事者比再检索更划算（§13 的 Patient Inquiry Candidate）")
        self.assertEqual(plan["stats"]["inquiries"], 1)

    def test_emr_is_unavailable_by_default(self):
        plan = ac.plan_acquisition([fx.gap(missing="MISSING_TIME_LINK")])
        row = [item for item in plan["candidates"] if item["action"] == "retrieve_emr"][0]
        self.assertEqual(row["decision"], "skipped")
        self.assertEqual(row["skip_reason"], "SOURCE_UNAVAILABLE")

    def test_every_source_can_be_reached(self):
        """六个来源**都有一条真实的选取路径**（本部署没有 EMR/患者通道 → 用注入的来源配置证明）。

        每条配方都写明"为什么它该赢"：可用来源集合不同，赢家也会变 —— 这正是 §33 的
        "统一进入 Action Candidate 之后由效用挑一个"的可验证形态。
        """
        recipes = [
            ("kb", "NO_EVIDENCE", {"kb": True, "emr": True, "db": True,
                                   "graph": True, "web": True, "patient": True}),
            ("graph", "MISSING_ENTITY_LINK", {"kb": True, "emr": True, "db": True,
                                              "graph": True, "web": True, "patient": True}),
            ("db", "LOW_AUTHORITY", {"kb": True, "emr": True, "db": True,
                                     "graph": True, "web": True, "patient": True}),
            ("web", "NO_EVIDENCE", {"kb": False, "emr": False, "db": False,
                                    "graph": False, "web": True, "patient": False}),
            ("emr", "MISSING_TIME_LINK", {"kb": False, "emr": True, "db": False,
                                          "graph": False, "web": False, "patient": False}),
            ("patient", "AMBIGUOUS_ENTITY", {"kb": True, "emr": False, "db": True,
                                             "graph": False, "web": True, "patient": True}),
        ]
        reached = set()
        for source, missing, sources in recipes:
            plan = ac.plan_acquisition([fx.gap(missing=missing)], sources=sources,
                                       permissions={"patient_contact": True, "emr_read": True})
            winner = [row for row in plan["candidates"] if row["decision"] == "selected"][0]
            self.assertEqual(winner["source"], source,
                             "%s 场景应从 %s 取证据，实际走了 %s"
                             % (missing, source, winner["source"]))
            reached.add(winner["source"])
        self.assertEqual(reached, set(contracts.ACQUISITION_SOURCES),
                         "六个来源必须都有真实可选的路径（少一个就说明那条腿是假的）")


class RouteGuidanceTests(unittest.TestCase):
    def test_guidance_reorders_the_gap_routes(self):
        """前后对比：同一批缺口，加了偏好之后 `suggested_routes[0]` 真的变了。"""
        from qa_memory_experience import apply_route_guidance

        gaps = [fx.gap(gap_id="G1", missing="NO_EVIDENCE",
                       routes=("keyword", "semantic", "web"))]
        plan = ac.plan_acquisition(gaps, sources=ALL_SOURCES)
        guidance = ac.route_guidance_from(plan)
        self.assertEqual(guidance["prefer_routes"], ["keyword"],
                         "被选中的是 retrieve_document → keyword（Phase 07 的首选通道）")
        adjusted, receipt = apply_route_guidance(gaps, guidance)
        self.assertEqual(receipt["changed"], 0, "首位本来就是它 → 不算改动（不许假报变化）")
        self.assertEqual(adjusted[0]["suggested_routes"], ["keyword", "semantic", "web"])

    def test_a_preference_that_really_changes_the_first_route(self):
        from qa_memory_experience import apply_route_guidance

        gaps = [fx.gap(gap_id="G1", missing="NO_EVIDENCE",
                       routes=("keyword", "semantic", "web"))]
        plan = ac.plan_acquisition(gaps, sources=ALL_SOURCES,
                                   preferred_routes=["semantic", "keyword"])
        adjusted, receipt = apply_route_guidance(gaps, {"prefer_routes": ["semantic"]})
        self.assertEqual(receipt["changed"], 1)
        self.assertEqual(receipt["route_changes"][0]["from"], "keyword")
        self.assertEqual(receipt["route_changes"][0]["to"], "semantic")
        self.assertEqual(adjusted[0]["suggested_routes"][0], "semantic")
        self.assertEqual([row["action"] for row in plan["candidates"]
                          if row["decision"] == "selected"], ["retrieve_document"],
                         "偏好只在效用并列时改排序，不许把低效用动作硬抬成赢家")

    def test_preference_never_invents_a_channel(self):
        plan = ac.plan_acquisition([fx.gap()], preferred_routes=["telepathy", "keyword"])
        self.assertEqual(plan["preferred_routes"], ["keyword"], "非通道取值一律丢弃")


class SkipReasonCoverageTests(unittest.TestCase):
    """十个淘汰理由码**全部可达**（闭集合不是摆设：每个码都要有一条真的产出路径）。"""

    def test_all_skip_reasons_are_reachable(self):
        import qa_graph_contracts as contracts

        seen: set = set()

        def _record(plan):
            seen.update(str(row.get("skip_reason") or "") for row in plan["candidates"]
                        if row.get("decision") == "skipped")

        # SOURCE_UNAVAILABLE / PERMISSION_DENIED / NOT_APPLICABLE_TO_GAP / OVER_USER_BURDEN
        _record(ac.plan_acquisition([fx.gap(missing="LOW_AUTHORITY")],
                                    sources={"kb": True, "emr": True, "db": True, "graph": True,
                                             "web": True, "patient": False}))
        _record(ac.plan_acquisition([fx.gap(missing="AMBIGUOUS_ENTITY")], sources=ALL_SOURCES,
                                    permissions={"patient_contact": False}))
        os.environ["QA_ACQ_MAX_USER_BURDEN"] = "low"
        try:
            _record(ac.plan_acquisition([fx.gap(missing="NO_EVIDENCE")], sources=ALL_SOURCES,
                                        permissions={"patient_contact": True}))
        finally:
            os.environ.pop("QA_ACQ_MAX_USER_BURDEN", None)
        # LOWER_UTILITY / SAFETY_OVERRIDE
        _record(ac.plan_acquisition([fx.gap(missing="NO_EVIDENCE")], sources=ALL_SOURCES))
        _record(ac.plan_acquisition([fx.gap(missing="NO_EVIDENCE", safety_critical=True)],
                                    sources={"kb": True, "emr": False, "db": False,
                                             "graph": False, "web": False, "patient": True},
                                    permissions={"patient_contact": True}))
        # DUPLICATE_ACTION（同一缺口重复提出同一动作）
        _record(ac.plan_acquisition([fx.gap()], actions=["retrieve_document", "retrieve_document"]))
        # UTILITY_FLOOR（把下限设得比任何动作都高）
        os.environ["QA_ACQ_UTILITY_FLOOR"] = "50"
        try:
            _record(ac.plan_acquisition([fx.gap()], sources=ALL_SOURCES))
        finally:
            os.environ.pop("QA_ACQ_UTILITY_FLOOR", None)
        # CONTEXT_GAP_NO_REPEAT / PATIENT_QUESTION_ALREADY_ASKED（P14-05，另有用例细验）
        gap = fx.gap(claim_id="c1", missing="LOW_RELEVANCE")
        _record(ac.plan_acquisition(
            [gap], sources=ALL_SOURCES,
            context_gaps=[{"gap_id": "CG1", "claim_id": "c1",
                           "context_gap_type": "OMITTED_EVIDENCE", "action": "REPACK_CONTEXT"}]))
        time_gap = fx.gap(missing="MISSING_TIME_LINK")
        asked = ac.patient_inquiry_candidate(time_gap)
        _record(ac.plan_acquisition([time_gap], sources=ALL_SOURCES,
                                    permissions={"patient_contact": True},
                                    asked_ids=[asked["inquiry_id"]]))
        self.assertEqual(seen, set(contracts.ACQUISITION_SKIP_REASONS),
                         "少一个可达的淘汰理由码就说明有一条判定路径是死的")


if __name__ == "__main__":
    unittest.main()
