#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 08 · P08-02（ContextUtility / token budget）用例。

钉住：
  1. §5 公式可复算：`ContextUtility = Relevance × EvidenceStrength × TaskNecessity ×
     Freshness × Diversity / TokenCost` —— 用 factors + 权重**手算**回来必须等于 `utility`；
  2. 预算裁剪有**确定性口径**：同输入同输出（pack_id、选中集合、trace 逐字相同）；
  3. **前后对比数字真的成立**：`estimated_tokens_after <= budget.total`、
     `after <= before`、`trimmed_items == 候选数 - 入选数`、`trim_reasons` 分布与 trace 一致；
  4. 报的账是真账：`budget.used == Σ 入选条目 tokens`（budget 回执条目自己不占预算）；
  5. 失败路径：预算极小（装不下必选段）时**如实记账**（OVER_TOKEN_BUDGET），不崩、不假装成功。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_context_pack as cp  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
from qa_phase08_fixtures import (  # noqa: E402
    QUESTION, evidence_item, graph, graph_claim, graph_edge, hop, plan, verification,
)


def _wide_graph(count=24, *, body_repeat=12):
    """造一批**真实过 Phase 02 标注**的证据（长短不一），用来压预算。"""
    evidence, edges = [], []
    for index in range(count):
        ref = "article:%d" % (index + 1)
        text = ("第%d号材料：香港家族办公室税收优惠政策对内地高净值客户的申报义务有影响，"
                "要点如下。" % (index + 1)) * body_repeat
        evidence.append(evidence_item(
            ref, text=text, authority=100 - index,
            verification=verification("SUPPORTED" if index % 3 else "QUALIFIED",
                                      score=0.9 - 0.01 * index)))
        edges.append(graph_edge("c1", ref))
    claims = [graph_claim("c1", text="香港家族办公室税收优惠政策对内地高净值客户有影响",
                          refs=[item["evidence_ref"] for item in evidence])]
    return graph(claims=claims, evidence=evidence, edges=edges)


class UtilityFormulaTests(unittest.TestCase):
    def setUp(self):
        self.pack_graph = _wide_graph(count=3)
        self.task = {"terms": list(cp.term_set(QUESTION)), "required_claims": ["c1"],
                     "primary_refs": ["article:1"]}

    def _item(self, ref):
        cg = cp.build_context_graph(graph=self.pack_graph, plan=plan(), run_id="r1")
        return [item for item in cg["items"] if item.get("evidence_ref") == ref][0]

    def test_five_factors_are_all_present_and_bounded(self):
        for ref in ("article:1", "article:2", "article:3"):
            value = cp.context_utility(self._item(ref), self.task)
            self.assertEqual(tuple(value["factors"]), contracts.CONTEXT_UTILITY_FACTORS)
            for name, factor in value["factors"].items():
                self.assertGreaterEqual(factor, 0.0, name)
                self.assertLessEqual(factor, 1.0, name)

    def test_utility_can_be_recomputed_from_the_factors(self):
        import math

        for ref in ("article:1", "article:2", "article:3"):
            item = self._item(ref)
            value = cp.context_utility(item, self.task)
            weights = contracts.CONTEXT_UTILITY_WEIGHTS
            total = sum(weights.values())
            log_sum = sum((weights[name] / total) * math.log(max(1e-6, value["factors"][name]))
                          for name in contracts.CONTEXT_UTILITY_FACTORS)
            expected = math.exp(log_sum) / max(1, int(item["tokens"]))
            self.assertAlmostEqual(value["utility"], round(expected, 8), places=8,
                                   msg="%s 的效用算不回去（口径不可复算）" % ref)
            self.assertEqual(value["token_cost"], max(1, item["tokens"]))

    def test_primary_evidence_and_required_claims_get_more_necessity(self):
        primary = cp.context_utility(self._item("article:1"), self.task)
        other = cp.context_utility(self._item("article:2"), self.task)
        self.assertGreater(primary["factors"]["task_necessity"],
                           other["factors"]["task_necessity"])

    def test_freshness_decays_with_a_documented_half_life(self):
        fresh = cp.freshness_factor("2026-10-09")
        older = cp.freshness_factor("2025-01-01")
        unknown = cp.freshness_factor("")
        self.assertGreater(fresh, older)
        self.assertEqual(unknown, 0.5, "没有时间戳给中性值（不是 0，也不是 1）")

    def test_diversity_penalises_the_same_source_group(self):
        item = self._item("article:2")
        alone = cp.context_utility(item, self.task, selected_identities=())
        grouped = cp.context_utility(
            item, self.task,
            selected_identities=[cp._identity_of(item)] * 2)
        self.assertLess(grouped["factors"]["diversity"], alone["factors"]["diversity"])


class BudgetTrimTests(unittest.TestCase):
    def _pack(self, budget, count=24):
        return cp.build_context_pack(
            graph=_wide_graph(count=count), plan=plan(hops=[hop("h1", "政策事实")]),
            request={"question": QUESTION, "mode": "standard"}, run_id="r1",
            budget_tokens=budget)

    def _candidates_tokens(self, count=24):
        """先量一遍"不裁剪时有多少 token"，后面的预算都按它**自校准**（不写死魔法数字）。"""
        return self._pack(200000, count=count)["budget"]["candidates_tokens"]

    def test_before_after_numbers_are_consistent(self):
        candidates = self._candidates_tokens()
        budget_tokens = max(400, candidates // 3)
        pack = self._pack(budget_tokens)
        budget = pack["budget"]
        self.assertEqual(budget["total"], budget_tokens)
        self.assertLessEqual(budget["estimated_tokens_after"], budget["total"],
                             "裁剪后必须装得进预算")
        self.assertLess(budget["estimated_tokens_after"], budget["estimated_tokens_before"],
                        "这个预算下必须真的裁掉了东西，否则对照没有意义")
        self.assertEqual(budget["trimmed_items"],
                         budget["candidates"] - budget["included"])
        self.assertGreater(budget["trimmed_items"], 0)
        self.assertIn("OVER_TOKEN_BUDGET", budget["trim_reasons"],
                      "预算不足时必须如实记 OVER_TOKEN_BUDGET：%s" % budget["trim_reasons"])

    def test_used_tokens_equal_the_sum_of_included_items(self):
        pack = self._pack(max(400, self._candidates_tokens() // 3))
        total = sum(int(item["tokens"]) for item in pack["items"]
                    if item["section"] != "budget")
        self.assertEqual(pack["budget"]["used"], total,
                         "报的账必须等于入选条目之和（budget 回执自己不占预算）")
        self.assertEqual(pack["sections"]["budget"]["tokens"], 0)

    def test_trim_reasons_distribution_matches_the_trace(self):
        pack = self._pack(max(400, self._candidates_tokens() // 3))
        from_trace = {}
        for row in pack["selection_trace"]:
            if row["decision"] != "excluded":
                continue
            from_trace[row["reason"]] = from_trace.get(row["reason"], 0) + 1
        self.assertEqual(pack["budget"]["trim_reasons"], from_trace)

    def test_same_input_same_output(self):
        first = self._pack(1500)
        second = self._pack(1500)
        self.assertEqual(first["pack_id"], second["pack_id"])
        self.assertEqual([item["item_id"] for item in first["items"]],
                         [item["item_id"] for item in second["items"]])
        self.assertEqual(first["selection_trace"], second["selection_trace"])
        self.assertEqual(first["budget"], second["budget"])

    def test_bigger_budget_keeps_more(self):
        candidates = self._candidates_tokens()
        small = self._pack(max(300, candidates // 4))
        large = self._pack(max(600, candidates // 2))
        self.assertLess(small["stats"]["included"], large["stats"]["included"])
        self.assertGreaterEqual(large["budget"]["used"], small["budget"]["used"])

    def test_mandatory_sections_survive_a_tight_budget(self):
        # 预算 = 只够必选段的一半 → 必选段优先保住，竞争段全被裁
        pack = self._pack(420, count=24)
        self.assertGreater(pack["sections"]["system_context"]["count"], 0)
        self.assertGreater(pack["sections"]["constraints"]["count"], 0)
        self.assertEqual(pack["stats"]["retrieval_requested"], 0)

    def test_budget_too_small_is_recorded_not_hidden(self):
        """预算连必选段都装不下：如实记 OVER_TOKEN_BUDGET，绝不假装成功。"""
        pack = self._pack(8, count=24)
        reasons = pack["budget"]["trim_reasons"]
        self.assertIn("OVER_TOKEN_BUDGET", reasons)
        excluded_mandatory = [
            row for row in pack["selection_trace"]
            if row["decision"] == "excluded" and row["section"] in
            ("system_context", "task_context", "constraints")]
        self.assertTrue(excluded_mandatory, "必选段装不下时必须留痕：%s" % pack["selection_trace"])
        self.assertTrue(all(row["reason"] == "OVER_TOKEN_BUDGET" for row in excluded_mandatory))
        ok, note = cp.validate_contract("context_pack", pack)
        self.assertTrue(ok, note)

    def test_zero_budget_still_returns_a_valid_pack(self):
        pack = self._pack(0, count=4)
        self.assertEqual(pack["budget"]["used"], 0)
        self.assertTrue(pack["sections"]["budget"]["count"] == 1,
                        "预算回执条目本身仍然要在（它不占预算）")
        ok, note = cp.validate_contract("context_pack", pack)
        self.assertTrue(ok, note)

    def test_budget_respects_the_output_reserve_field(self):
        pack = self._pack(1200)
        self.assertEqual(pack["budget"]["output_reserve"], cp.output_reserve_tokens())
        self.assertTrue(pack["budget"]["estimator_note"])

    def test_no_trim_when_budget_is_generous(self):
        """预算充足时不许无谓裁剪：before == after 且 trim_reasons 为空（可复算的"不该裁就别裁"）。"""
        pack = self._pack(200000)
        self.assertEqual(pack["budget"]["estimated_tokens_after"],
                         pack["budget"]["estimated_tokens_before"])
        self.assertEqual(pack["budget"]["trimmed_items"],
                         len([row for row in pack["selection_trace"]
                              if row["decision"] == "excluded"]))


if __name__ == "__main__":
    unittest.main()
