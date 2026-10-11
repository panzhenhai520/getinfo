#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 10 · P10-01 `freshness/TTL` + P10-02 `source-version detection` 用例。

钉住：
  1. 时效闸门是**先判先返回**的确定性函数：十一条规则的顺序逐条用单变量改法验证
     （只改一个变量，看出口与理由码是否按契约变）；
  2. 四条 §10 硬规则各有独立用例：`status!=ACTIVE`、`valid_until` 过期、
     `source_version_changed`、`freshness_required`、`high_stakes`；
  3. 高危判定、年龄阈值、必须复验档位都是**纯规则 + 可配**（环境变量改了就换口径）；
  4. 来源版本检测只认显式版本标记（**不把普通日期当版本**），语料版本变化必须被检出；
  5. 失败路径：缺字段/脏数据不抛异常；`contract_ok` 必须为真。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_graph_contracts as contracts  # noqa: E402
import qa_memory_revalidation as mr  # noqa: E402

import qa_phase10_fixtures as fx  # noqa: E402


class GateDecisionTests(unittest.TestCase):
    def test_fresh_long_memory_with_binding_is_allowed(self):
        gate = mr.freshness_gate(fx.memory_item(), now=fx.NOW)
        self.assertEqual((gate["decision"], gate["reason"]), ("ALLOW", "FRESH_AND_VERIFIED"))
        self.assertTrue(gate["contract_ok"])
        self.assertEqual(gate["gate_version"], contracts.MEMORY_FRESHNESS_GATE_VERSION)

    def test_gate_is_deterministic(self):
        item = fx.memory_item(freshness="SHORT")
        first = mr.freshness_gate(item, now=fx.NOW)
        second = mr.freshness_gate(fx.memory_item(freshness="SHORT"), now=fx.NOW)
        self.assertEqual(first, second, "同输入必须同输出")

    def test_terminal_statuses_are_blocked(self):
        for status in ("SUPERSEDED", "CONTRADICTED", "REVOKED"):
            gate = mr.freshness_gate(fx.memory_item(status=status), now=fx.NOW)
            self.assertEqual((gate["decision"], gate["reason"]), ("BLOCK", "NOT_ACTIVE"),
                             "%s 必须被闸门拦下" % status)

    def test_expired_valid_until_requires_revalidation(self):
        gate = mr.freshness_gate(fx.memory_item(valid_until="2026-10-01T00:00:00Z"), now=fx.NOW)
        self.assertEqual((gate["decision"], gate["reason"]),
                         ("REVALIDATE", "VALID_UNTIL_PASSED"))

    def test_expired_status_requires_revalidation(self):
        gate = mr.freshness_gate(fx.memory_item(status="EXPIRED"), now=fx.NOW)
        self.assertEqual((gate["decision"], gate["reason"]), ("REVALIDATE", "STATUS_EXPIRED"))

    def test_stale_status_requires_revalidation(self):
        gate = mr.freshness_gate(fx.memory_item(status="STALE"), now=fx.NOW)
        self.assertEqual((gate["decision"], gate["reason"]), ("REVALIDATE", "STATUS_STALE"))

    def test_decay_below_expire_floor_blocks_even_active(self):
        # LONG 半衰期 3650 天：把 last_verified_at 推到很早，衰减分必然低于 0.02
        gate = mr.freshness_gate(
            fx.memory_item(freshness="SHORT", last_verified_at="2015-01-01T00:00:00Z"),
            now=fx.NOW)
        self.assertEqual(gate["decision"], "BLOCK")
        self.assertEqual(gate["reason"], "DECAY_BELOW_EXPIRE_FLOOR")

    def test_source_version_change_forces_revalidation(self):
        gate = mr.freshness_gate(fx.memory_item(), now=fx.NOW, source_version_changed=True)
        self.assertEqual((gate["decision"], gate["reason"]),
                         ("REVALIDATE", "SOURCE_VERSION_CHANGED"))

    def test_high_stakes_forces_revalidation(self):
        gate = mr.freshness_gate(fx.memory_item(claim_type="policy"), now=fx.NOW)
        self.assertEqual((gate["decision"], gate["reason"]), ("REVALIDATE", "HIGH_STAKES_DEFAULT"))
        self.assertTrue(gate["high_stakes"])
        self.assertIn("CLAIM_TYPE:policy", gate["detail"]["high_stakes_reasons"])

    def test_freshness_required_classes_force_revalidation(self):
        for freshness in mr.REVALIDATE_FRESHNESS_CLASSES:
            # 刚核验过（age≈0）：这样衰减不会先一步把它们压到过期线以下（那是另一条规则）
            gate = mr.freshness_gate(
                fx.memory_item(freshness=freshness, claim_type="background",
                               last_verified_at="2026-10-10T23:00:00Z"), now=fx.NOW)
            self.assertEqual(gate["decision"], "REVALIDATE", freshness)
            self.assertIn(gate["reason"], ("FRESHNESS_REQUIRED", "HIGH_STAKES_DEFAULT"))

    def test_age_threshold_applies_to_long_and_medium(self):
        # MEDIUM 半衰期 365 天、阈值 91 天：选 132 天前核验 —— 过阈值但衰减分仍远高于过期线
        old = fx.memory_item(freshness="MEDIUM", last_verified_at="2026-06-01T00:00:00Z",
                             created_at="2026-06-01T00:00:00Z", claim_type="background")
        gate = mr.freshness_gate(old, now=fx.NOW)
        self.assertEqual((gate["decision"], gate["reason"]),
                         ("REVALIDATE", "AGE_OVER_REVALIDATE_THRESHOLD"))
        fresh = mr.freshness_gate(fx.memory_item(freshness="MEDIUM", claim_type="background"),
                                  now=fx.NOW)
        self.assertEqual(fresh["decision"], "ALLOW")

    def test_missing_evidence_binding_requires_revalidation(self):
        gate = mr.freshness_gate(fx.memory_item(evidence_refs=(), claim_type="background"),
                                 now=fx.NOW)
        self.assertEqual((gate["decision"], gate["reason"]), ("REVALIDATE", "NO_EVIDENCE_BINDING"))

    def test_reason_is_always_in_the_contract_enum(self):
        samples = [fx.memory_item(), fx.memory_item(status="REVOKED"),
                   fx.memory_item(freshness="VERY_SHORT", claim_type="background"),
                   fx.memory_item(valid_until="2020-01-01T00:00:00Z"),
                   fx.memory_item(evidence_refs=(), claim_type="background"),
                   fx.memory_item(status="EXPIRED"), fx.memory_item(status="STALE"),
                   fx.memory_item(last_verified_at="2015-01-01T00:00:00Z")]
        for item in samples:
            gate = mr.freshness_gate(item, now=fx.NOW)
            self.assertIn(gate["reason"], contracts.MEMORY_FRESHNESS_REASONS)
            self.assertIn(gate["decision"], contracts.MEMORY_FRESHNESS_DECISIONS)

    def test_failure_path_on_dirty_item_does_not_raise(self):
        gate = mr.freshness_gate({"memory_id": "x", "freshness_class": "NOPE", "status": "JUNK"},
                                 now=fx.NOW)
        self.assertIn(gate["decision"], contracts.MEMORY_FRESHNESS_DECISIONS)
        self.assertTrue(gate["contract_ok"], "脏数据也必须产出可校验回执（归一化 + 留原值）")
        self.assertEqual(gate["freshness_class"], "MEDIUM")
        self.assertEqual(gate["detail"]["freshness_class_raw"], "NOPE")
        self.assertEqual(gate["detail"]["status_raw"], "JUNK")
        gate = mr.freshness_gate(None, now="not-a-time")
        self.assertIn(gate["decision"], contracts.MEMORY_FRESHNESS_DECISIONS)

    def test_age_ratio_is_configurable(self):
        os.environ["QA_MEMORY_REVALIDATE_AGE_RATIO"] = "5"
        try:
            required = mr.freshness_required(
                fx.memory_item(freshness="MEDIUM", last_verified_at="2026-01-01T00:00:00Z"),
                now=fx.NOW)
            self.assertEqual(required["age_ratio"], 5.0)
            self.assertGreater(required["threshold_days"], 1000)
        finally:
            os.environ.pop("QA_MEMORY_REVALIDATE_AGE_RATIO", None)

    def test_high_stakes_types_are_configurable(self):
        os.environ["QA_MEMORY_HIGH_STAKES_TYPES"] = "custom_kind"
        try:
            self.assertEqual(mr.high_stakes_types(), ("custom_kind",))
            stakes = mr.high_stakes_of(fx.memory_item(freshness="LONG", claim_type="policy"))
            self.assertNotIn("CLAIM_TYPE:policy", stakes["reasons"],
                             "改了口径表后 policy 不再是高危")
            custom = mr.high_stakes_of(fx.memory_item(freshness="LONG", claim_type="custom_kind"))
            self.assertIn("CLAIM_TYPE:custom_kind", custom["reasons"])
        finally:
            os.environ.pop("QA_MEMORY_HIGH_STAKES_TYPES", None)


class HighStakesTests(unittest.TestCase):
    def test_numbered_content_is_high_stakes(self):
        stakes = mr.high_stakes_of(fx.memory_item(
            content="该政策的门槛为 200 万港元，税率为 8.25%。", freshness="MEDIUM",
            claim_type="background"))
        self.assertTrue(stakes["high_stakes"])
        self.assertTrue(any(reason.startswith("NUMBERED:") for reason in stakes["reasons"]))

    def test_explicit_false_overrides_rules(self):
        stakes = mr.high_stakes_of(fx.memory_item(claim_type="policy"), stakes=False)
        self.assertFalse(stakes["high_stakes"])
        self.assertEqual(stakes["reasons"], ["EXPLICIT_NOT_HIGH_STAKES"])

    def test_plain_background_text_is_not_high_stakes(self):
        stakes = mr.high_stakes_of(fx.memory_item(
            content="香港家族办公室的历史沿革可以追溯到十九世纪。", freshness="LONG",
            claim_type="background"))
        self.assertFalse(stakes["high_stakes"])


class VersionTokenTests(unittest.TestCase):
    def test_explicit_markers_are_extracted(self):
        tokens = mr.version_tokens("《指引》第 3 版（v2.1，2026 年版，2025 年修订）")
        for expected in ("2.1", "3", "2026", "2025"):
            self.assertIn(expected, tokens)

    def test_plain_dates_are_not_versions(self):
        self.assertEqual(mr.version_tokens("政策自 2026 年 4 月 1 日起生效"), [])

    def test_latest_token_orders_numerically(self):
        self.assertEqual(mr.latest_version_token(["1.9", "1.10", "1.2"]), "1.10")

    def test_no_tokens_returns_empty(self):
        self.assertEqual(mr.latest_version_token([]), "")
        self.assertEqual(mr.version_tokens(""), [])


class SourceVersionTests(unittest.TestCase):
    def _links(self, corpus_version="corpus-a", evidence_ref="article:1",
               source_fingerprint="SF1"):
        return [{"memory_id": "MEM1", "evidence_ref": evidence_ref,
                 "source_fingerprint": source_fingerprint, "corpus_version": corpus_version,
                 "verdict": "SUPPORTED", "evidence_score": 0.8}]

    def test_corpus_version_change_is_detected(self):
        state = mr.source_version_state(fx.memory_item(), self._links("corpus-a"),
                                        corpus_version="corpus-b")
        self.assertTrue(state["changed"])
        self.assertIn("CORPUS_VERSION_CHANGED", state["reasons"])
        self.assertTrue(state["contract_ok"])

    def test_same_corpus_version_is_stable(self):
        state = mr.source_version_state(fx.memory_item(), self._links("corpus-a"),
                                        corpus_version="corpus-a")
        self.assertFalse(state["changed"])
        self.assertIn("SOURCE_VERSION_STABLE", state["reasons"])

    def test_missing_corpus_version_is_reported_not_guessed(self):
        state = mr.source_version_state(fx.memory_item(), [], corpus_version="corpus-a")
        self.assertFalse(state["changed"])
        self.assertIn("NO_CORPUS_VERSION", state["reasons"])

    def test_newer_document_version_is_detected(self):
        item = fx.memory_item(content="《家族办公室税务指引》第 2 版明确了利得税宽免口径")
        evidence = fx.evidence_item("article:1", text="《家族办公室税务指引》第 3 版明确了利得税宽免口径")
        state = mr.source_version_state(item, self._links("corpus-a"), current_evidence=[evidence],
                                        corpus_version="corpus-a")
        self.assertTrue(state["changed"])
        self.assertIn("DOCUMENT_VERSION_CHANGED", state["reasons"])
        self.assertEqual(state["newest_token_current"], "3")

    def test_same_document_version_is_stable(self):
        item = fx.memory_item(content="《家族办公室税务指引》第 3 版明确了利得税宽免口径")
        evidence = fx.evidence_item("article:1", text="《家族办公室税务指引》第 3 版明确了利得税宽免口径")
        state = mr.source_version_state(item, self._links("corpus-a"), current_evidence=[evidence],
                                        corpus_version="corpus-a")
        self.assertFalse(state["changed"])

    def test_unrelated_evidence_is_not_used_for_version_comparison(self):
        item = fx.memory_item(content="《家族办公室税务指引》第 2 版明确了利得税宽免口径")
        other = fx.evidence_item("article:77", text="另一份完全无关的材料 第 9 版")
        state = mr.source_version_state(item, self._links("corpus-a"), current_evidence=[other],
                                        corpus_version="corpus-a")
        self.assertFalse(state["changed"])
        self.assertEqual(state["version_tokens_current"], [])

    def test_reasons_are_always_in_the_contract_enum(self):
        samples = [
            mr.source_version_state(fx.memory_item(), self._links("a"), corpus_version="b"),
            mr.source_version_state(fx.memory_item(), self._links("a"), corpus_version="a"),
            mr.source_version_state(fx.memory_item(), [], corpus_version=""),
        ]
        for state in samples:
            for reason in state["reasons"]:
                self.assertIn(reason, contracts.MEMORY_SOURCE_VERSION_REASONS)

    def test_failure_path_on_dirty_links(self):
        state = mr.source_version_state(fx.memory_item(), [None, "junk", {}], corpus_version="x")
        self.assertFalse(state["changed"])
        self.assertIn("NO_CORPUS_VERSION", state["reasons"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
