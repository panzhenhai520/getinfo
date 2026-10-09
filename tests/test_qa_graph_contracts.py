#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 01 · F-6/F-9 守门测试。

钉住三件事：
  1. **等价替换**：新契约模块里的取值必须与既有实现里的字面量**逐个相等**
     （`kg_builder.RELATION_*`、节点类型、`qa_reasoning` 的缺省关系）；
  2. 契约层的再导出与单一事实源一致（`qa_contracts` 的值 == `qa_graph_contracts` 的值）；
  3. `validate()` 真的会拦下缺字段/越界枚举，而不是永远返回 True。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_graph_contracts as contracts  # noqa: E402


class EquivalenceTests(unittest.TestCase):
    """契约值必须与既有实现一字不差（等价替换，不改行为）。"""

    def test_relation_kinds_match_kg_builder(self):
        import kg_builder

        self.assertEqual(contracts.KG_RELATION_EVENT, kg_builder.RELATION_EVENT)
        self.assertEqual(contracts.KG_RELATION_ATTRIBUTE, kg_builder.RELATION_ATTRIBUTE)
        self.assertEqual(contracts.KG_RELATION_COOCCURRENCE, kg_builder.RELATION_COOCCURRENCE)
        self.assertEqual(
            set(contracts.KG_RELATION_KINDS),
            {kg_builder.RELATION_EVENT, kg_builder.RELATION_ATTRIBUTE,
             kg_builder.RELATION_COOCCURRENCE},
        )

    def test_node_types_match_kg_builder_literals(self):
        # kg_builder 里以字面量出现：add_node(key, "topic", ...) / "entity" / "value"
        self.assertEqual(set(contracts.KG_NODE_TYPES), {"entity", "topic", "value"})

    def test_claim_relationship_default_matches_reasoning(self):
        # qa_reasoning.py:220 缺省 relationship = "supports"
        self.assertIn("supports", contracts.CLAIM_EVIDENCE_RELATIONSHIPS)
        self.assertEqual(contracts.CLAIM_EVIDENCE_RELATIONSHIPS[0], "supports")

    def test_evidence_relationship_enum_is_subset_of_contract(self):
        """证据契约里的 relationship 枚举必须被图契约覆盖（否则边建不出来）。"""
        from qa_contracts import EVIDENCE_SCHEMA

        enum = EVIDENCE_SCHEMA["properties"]["relationship"]["enum"]
        allowed = set(contracts.CLAIM_EVIDENCE_RELATIONSHIPS) | {"", None}
        unknown = [item for item in enum if item not in allowed]
        self.assertFalse(unknown, "证据契约出现图契约未登记的关系取值：%s" % unknown)


class ReExportTests(unittest.TestCase):
    """F-9：契约层再导出的值必须与单一事实源完全一致。"""

    def test_qa_contracts_reexports_match(self):
        import qa_contracts

        self.assertEqual(qa_contracts.QA_RETRIEVAL_ROUTES, contracts.QA_RETRIEVAL_ROUTES)
        self.assertEqual(qa_contracts.QA_FAILURE_POLICIES, contracts.QA_FAILURE_POLICIES)
        self.assertEqual(qa_contracts.QA_STOP_REASONS, contracts.QA_STOP_REASONS)
        self.assertEqual(qa_contracts.QA_AUDIT_EVENT_TYPES, contracts.QA_AUDIT_EVENT_TYPES)
        self.assertEqual(qa_contracts.GRAPH_CONTRACT_VERSION, contracts.GRAPH_CONTRACT_VERSION)

    def test_stage_roles_cover_all_stages(self):
        import qa_contracts

        missing = [stage for stage in qa_contracts.QA_STAGES
                   if stage not in qa_contracts.QA_STAGE_ROLES]
        self.assertFalse(missing, "阶段未登记四图角色：%s" % missing)

    def test_retrieval_routes_cover_actual_channels(self):
        """实际用到的通道名必须都在契约里（防止新增通道不登记）。"""
        import qa_retrieval

        source = open(qa_retrieval.__file__, encoding="utf-8").read()
        for route in ("page_context", "policy_exact", "keyword", "semantic", "graph", "web"):
            self.assertIn(route, contracts.QA_RETRIEVAL_ROUTES)
            self.assertIn(route, source, "契约登记了 %s，但检索实现里找不到它" % route)


class ValidateTests(unittest.TestCase):
    def test_validate_catches_missing_required(self):
        ok, note = contracts.validate("kg_edge", {"src_key": "a", "dst_key": "b"})
        self.assertFalse(ok)
        self.assertIn("relation_kind", note)

    def test_validate_catches_bad_enum(self):
        ok, note = contracts.validate("kg_node", {"node_key": "k", "node_type": "组织"})
        self.assertFalse(ok)
        self.assertIn("node_type", note)

    def test_validate_passes_valid_payloads(self):
        ok, _note = contracts.validate(
            "kg_edge", {"src_key": "a", "dst_key": "b", "relation_kind": "attribute"})
        self.assertTrue(ok)
        ok, _note = contracts.validate(
            "search_trace", {"hop_index": 0, "route": "keyword", "accepted": 3})
        self.assertTrue(ok)
        ok, _note = contracts.validate("execution_node", {"node_id": "plan"})
        self.assertTrue(ok)

    def test_validate_rejects_unknown_schema(self):
        ok, note = contracts.validate("不存在", {})
        self.assertFalse(ok)
        self.assertIn("未知 schema", note)

    def test_describe_mentions_version(self):
        self.assertIn(contracts.GRAPH_CONTRACT_VERSION, contracts.describe())


if __name__ == "__main__":
    unittest.main()
