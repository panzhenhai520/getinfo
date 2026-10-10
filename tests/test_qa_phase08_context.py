#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 08 · P08-01（ContextItem / Context Graph）用例。

钉住：
  1. 候选池的每一层都能追到上游真实产出物（`source_stage`）——没有"凭空造的上下文"；
  2. **引用可回溯到 Phase 02 的最小 span**：`evidence_id` + `span`，且
     `content_excerpt[start:end] == span.quote` 在**原始证据正文**上真的成立
     （不是拼出来的字符串），这正是 MASTER_RULES 第 11 条要的可校验引用；
  3. 回溯不上的条目**必须显式 `grounded=false` + 原因**，不许伪装成有据；
  4. 反证身份来自 Phase 06 的图级关系（REFUTES）/矛盾裁决，不是这里另判的；
  5. 图上没有的东西（memory / skill）本阶段是空段 + `deferred_to`，绝不编内容；
  6. `item_id` 内容寻址：同输入同 id，正文不同则 id 不同。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_context_pack as cp  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402
from qa_phase08_fixtures import (  # noqa: E402
    LONG_ARTICLE, QUESTION, conflict, evidence_item, graph, graph_claim, graph_edge, hop,
    plan, verification,
)


def _graph(*, with_counter=True, conflicts=()):
    evidence = [
        evidence_item("article:1", verification=verification("SUPPORTED"), doc_type="official_policy"),
        evidence_item("article:3", text="第三方研究认为该政策对中小客户影响有限。" * 4,
                      verification=verification("QUALIFIED")),
    ]
    edges = [graph_edge("c1", "article:1", relation="SUPPORTS", verified=True)]
    if with_counter:
        evidence.append(evidence_item("article:2", text="反驳观点：宽免范围很有限，门槛很高。" * 4,
                                      verification=verification("REFUTED")))
        edges.append(graph_edge("c1", "article:2", relation="REFUTES", status="REFUTED"))
    claims = [graph_claim("c1", text="香港家族办公室税收优惠政策对内地高净值客户有影响",
                          refs=["article:1"])]
    return graph(claims=claims, evidence=evidence, edges=edges, conflicts=list(conflicts))


class ContextGraphTests(unittest.TestCase):
    def setUp(self):
        self.graph = _graph(conflicts=[conflict("k1", ["c1"], evidence_refs=["article:2"])])
        self.cg = cp.build_context_graph(graph=self.graph, plan=plan(hops=[hop("h1", "政策事实")]),
                                         working_memory={"已取到证据": 3}, run_id="r1")

    def test_nodes_are_context_items_with_contract_ids(self):
        self.assertTrue(self.cg["nodes"])
        for node in self.cg["nodes"]:
            self.assertTrue(node["node_id"].startswith("context:"))
            self.assertIn(node["kind"], contracts.CONTEXT_ITEM_KINDS)
            self.assertIn(node["section"], contracts.CONTEXT_SECTIONS)
            self.assertGreaterEqual(node["tokens"], 0)

    def test_every_item_declares_where_it_came_from(self):
        for item in self.cg["items"]:
            self.assertIn(str(item.get("source_stage") or ""), contracts.CONTEXT_ITEM_SOURCES,
                          "%s 没有可追溯的来源" % item["item_id"])

    def test_evidence_span_is_traceable_to_phase02_minimal_span(self):
        """`evidence_id` + `span` 必须能在**原始证据正文**上校验通过。"""
        by_ref = {str(item.get("evidence_ref")): item for item in self.graph["evidence"]}
        checked = 0
        for item in self.cg["items"]:
            if item["kind"] not in ("evidence", "counter_evidence"):
                continue
            ground = item["grounding"]
            self.assertTrue(ground["evidence_ref"])
            self.assertTrue(ground["evidence_id"], "缺少 Phase 02 的证据指纹")
            span = ground["span"]
            self.assertEqual(span["quote"], item["text"],
                             "包内文本必须就是 span.quote（不许另写摘要冒充引用）")
            source = str(by_ref[ground["evidence_ref"]]["content_excerpt"])
            self.assertEqual(source[span["start"]:span["end"]], span["quote"],
                             "span 偏移在原始正文上对不上（引用不可回溯）")
            self.assertTrue(span["source"], "span 必须带切分来源标注")
            checked += 1
        self.assertGreaterEqual(checked, 3)

    def test_ungrounded_entries_are_labelled_not_faked(self):
        for item in self.cg["items"]:
            ground = item["grounding"]
            if item["kind"] in ("claim", "gap", "task", "system", "constraint", "working_memory"):
                self.assertFalse(ground["grounded"])
                self.assertTrue(ground.get("reason"), "无据条目必须写明为什么无据")
            if not ground["grounded"]:
                self.assertNotIn("evidence_id", ground)

    def test_counter_evidence_comes_from_graph_relations(self):
        counter = [item for item in self.cg["items"] if item["kind"] == "counter_evidence"]
        self.assertEqual([item["evidence_ref"] for item in counter], ["article:2"],
                         "反证由 Phase 06 的 REFUTES 关系判定，不是这里另判")
        self.assertEqual(counter[0]["section"], "counter_evidence")
        self.assertEqual(counter[0]["claim_id"], "c1")

    def test_conflict_and_task_and_working_memory_are_present(self):
        kinds = {item["kind"] for item in self.cg["items"]}
        self.assertIn("task", kinds)
        self.assertIn("gap", kinds, "未消解矛盾要进候选池")
        self.assertIn("working_memory", kinds)
        gap = [item for item in self.cg["items"] if item["kind"] == "gap"][0]
        self.assertEqual(gap["metadata"]["role"], "unresolved_conflict")
        self.assertEqual(gap["section"], "counter_evidence")

    def test_edges_only_point_at_real_nodes(self):
        node_ids = {item["item_id"] for item in self.cg["items"]}
        self.assertTrue(self.cg["edges"])
        for edge in self.cg["edges"]:
            self.assertTrue(edge["src"].startswith("context:"))
            self.assertTrue(edge["dst"].startswith("claim:"))
            self.assertIn(edge["src"].split(":", 1)[1], node_ids)
            ok, note = validate("context_edge", edge)
            self.assertTrue(ok, note)
        self.assertEqual(self.cg["stats"]["edges"], len(self.cg["edges"]))

    def test_stats_are_consistent(self):
        stats = self.cg["stats"]
        self.assertEqual(stats["items"], len(self.cg["items"]))
        self.assertEqual(stats["grounded_items"] + stats["ungrounded_items"], stats["items"])
        self.assertEqual(stats["claim_items"], 1)
        self.assertEqual(stats["counter_evidence_items"], 1)


class ItemIdentityTests(unittest.TestCase):
    def test_item_ids_are_content_addressed(self):
        first = cp.make_context_item(kind="evidence", section="evidence_context", text="同一段正文",
                                     grounding={"grounded": False})
        second = cp.make_context_item(kind="evidence", section="evidence_context", text="同一段正文",
                                      grounding={"grounded": False})
        third = cp.make_context_item(kind="evidence", section="evidence_context", text="另一段正文",
                                     grounding={"grounded": False})
        self.assertEqual(first["item_id"], second["item_id"])
        self.assertNotEqual(first["item_id"], third["item_id"])

    def test_bad_kind_and_section_fall_back_safely(self):
        item = cp.make_context_item(kind="vibes", section="nowhere", text="x")
        self.assertIn(item["kind"], contracts.CONTEXT_ITEM_KINDS)
        self.assertIn(item["section"], contracts.CONTEXT_SECTIONS)


class SectionTests(unittest.TestCase):
    def test_all_nine_sections_exist_and_deferred_ones_are_marked(self):
        graph_obj = _graph()
        pack = cp.build_context_pack(graph=graph_obj, plan=plan(), request={"question": QUESTION},
                                     run_id="r1")
        self.assertEqual(tuple(pack["sections"]), contracts.CONTEXT_SECTIONS)
        for name in ("memory_context", "skill_context"):
            self.assertEqual(pack["sections"][name]["count"], 0)
            self.assertIn("Phase", pack["sections"][name]["deferred_to"])
        for name in ("system_context", "task_context", "evidence_context", "counter_evidence",
                     "constraints", "budget"):
            self.assertGreater(pack["sections"][name]["count"], 0, "%s 段不该是空的" % name)

    def test_system_rules_are_in_the_pack(self):
        pack = cp.build_context_pack(graph=_graph(), plan=plan(), request={"question": QUESTION},
                                     run_id="r1")
        rules = [item["text"] for item in pack["items"] if item["kind"] == "system"]
        self.assertTrue(any(cp.UNGROUNDED_MARK in rule for rule in rules),
                        "系统规则必须写明'没有证据要标注【无证据】'")
        self.assertEqual(len(rules), 4)

    def test_pack_contract_and_citation_index(self):
        pack = cp.build_context_pack(graph=_graph(), plan=plan(), request={"question": QUESTION},
                                     run_id="r1")
        ok, note = validate("context_pack", pack)
        self.assertTrue(ok, note)
        self.assertTrue(pack["citation_map"])
        for label, ref in pack["citation_map"].items():
            entry = pack["citation_index"][label]
            self.assertEqual(entry["evidence_ref"], ref)
            self.assertTrue(entry["grounded"])
            self.assertEqual(entry["quote"], entry["span"]["quote"])
        self.assertEqual(pack["grounding"]["untraceable_citations"], 0)


class LongTextSpanTests(unittest.TestCase):
    def test_minimal_span_really_narrows_a_long_chunk(self):
        evidence = [evidence_item("article:9", text=LONG_ARTICLE,
                                  verification=verification("SUPPORTED"))]
        claims = [graph_claim("c1", text="香港家族办公室税收优惠政策对内地高净值客户有影响",
                              refs=["article:9"])]
        pack = cp.build_context_pack(
            graph=graph(claims=claims, evidence=evidence,
                        edges=[graph_edge("c1", "article:9")]),
            plan=plan(), request={"question": QUESTION, "mode": "standard"}, run_id="r1")
        entry = list(pack["citation_index"].values())[0]
        self.assertLess(len(entry["quote"]), len(LONG_ARTICLE),
                        "长 chunk 必须被切成最小 span（§2.2/§9），不能整篇进上下文")
        self.assertGreater(len(entry["quote"]), 10)
        self.assertEqual(entry["quote"], LONG_ARTICLE[entry["span"]["start"]:entry["span"]["end"]])


if __name__ == "__main__":
    unittest.main()
