#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 10 · 推理留痕 + 递归重规划 + 会话约束 + 排序权重表。

钉住四件事：
  1. 每跳都有留痕，且**同 run + 轮次 + 跳序号**幂等（重复写覆盖，不叠加）；
  2. 缺失链接能触发**一轮**补检；预算用尽时**停止递归**并如实标注（不无限循环）；
  3. 会话约束按 (用户, 会话, 包) 隔离，只补空缺、不覆盖本轮明确表达，空会话不固化；
  4. 排序权重表默认档与原硬编码**逐项相等**（等价替换），切档/单项覆盖可用。
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from qa_pipeline import _persist_session_constraints, _run_multi_hop  # noqa: E402
from qa_ranking_weights import PROFILES, ranking_weights  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402


def _evidence(ref, text="证据正文"):
    return {"evidence_ref": ref, "source_type": "article", "title": text[:40],
            "content_excerpt": text, "source_url": "https://example.com/x",
            "authority_level": 50, "metadata": {}}


class _FakeRetriever:
    def __init__(self, mapping, delay=0.0):
        import time

        self.mapping = mapping
        self.delay = delay
        self.calls = []
        self._time = time

    def retrieve(self, plan, **kwargs):
        self.calls.append(dict(plan))
        if self.delay:
            self._time.sleep(self.delay)
        question = str(plan.get("question") or "")
        hits = [key for key in self.mapping if key in question]
        if hits:
            best = max(hits, key=lambda key: (len(key), question.rfind(key)))
            return {"evidence": self.mapping[best], "stats": {}, "queries": [question]}
        return {"evidence": [], "stats": {}, "queries": [question]}


def _plan():
    return {
        "question": "2026年医保新规对民营医院有什么影响？",
        "entities": ["医保"],
        "decomposition": {
            "is_multi_hop": True, "pattern": "impact_chain",
            "hops": [
                {"id": "h1", "question": "2026年医保新规", "depends_on": []},
                {"id": "h2", "question": "民营医院压力", "depends_on": ["h1"]},
            ],
        },
    }


def _plan3():
    plan = _plan()
    plan["decomposition"]["hops"].append(
        {"id": "h3", "question": "器械企业影响", "depends_on": ["h2"]})
    return plan


class ReasoningTraceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "traces.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self._saved_rounds = getattr(config, "QA_RECURSION_MAX_ROUNDS", 1)
        self._saved_budget = getattr(config, "QA_MULTI_HOP_BUDGET_SECONDS", 25)
        config.QA_RECURSION_MAX_ROUNDS = 1
        config.QA_MULTI_HOP_BUDGET_SECONDS = 25

    def tearDown(self):
        config.QA_RECURSION_MAX_ROUNDS = self._saved_rounds
        # 预算必须还原：否则后一个用例会继承前一个的极小预算（实测踩到用例间串扰）
        config.QA_MULTI_HOP_BUDGET_SECONDS = self._saved_budget
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def test_trace_round_trip_and_idempotency(self):
        self.store.record_reasoning_trace("run-1", hop_index=0, sub_query_id="h1",
                                          sub_query="A 的事实", status="ok",
                                          used_evidence_refs=["article:1", "article:2"])
        self.store.record_reasoning_trace("run-1", hop_index=1, sub_query_id="h2",
                                          sub_query="A 对 B", status="empty",
                                          missing_links=[{"type": "hop_missing"}],
                                          next_queries=["A 对 B 影响"])
        traces = self.store.reasoning_traces("run-1")
        self.assertEqual([t["hop_index"] for t in traces], [0, 1])
        self.assertEqual(traces[0]["used_evidence_refs_json"], '["article:1","article:2"]')
        self.assertIn("hop_missing", traces[1]["missing_links_json"])
        # 幂等：同 (run, round, hop) 再写一次是覆盖，不是追加
        self.store.record_reasoning_trace("run-1", hop_index=1, sub_query_id="h2",
                                          sub_query="A 对 B（改）", status="ok")
        self.assertEqual(len(self.store.reasoning_traces("run-1")), 2)
        self.assertEqual(self.store.reasoning_traces("run-1")[1]["sub_query"], "A 对 B（改）")

    def test_trace_write_failure_does_not_raise(self):
        """留痕绝不能拖累主流程：坏 run_id / 断连接都必须静默。"""
        self.store.record_reasoning_trace("", hop_index=0)  # 空 run_id 也不许抛
        self.store.record_reasoning_trace("run-x", hop_index=0, used_evidence_refs=None)

    def test_recorder_called_per_hop_with_rounds(self):
        entries = []
        retriever = _FakeRetriever({
            "医保新规": [_evidence("article:1", "医保新规落地")],
            "民营医院压力": [_evidence("article:2", "民营医院压力上升")],
        })
        merged, receipts = _run_multi_hop(
            retriever, _plan(),
            {"evidence": [_evidence("article:1", "医保新规落地")]}, {}, pack_id="health",
            limit=8, trace_recorder=lambda entry: entries.append(dict(entry)),
        )
        self.assertEqual(len(receipts), 2)
        self.assertEqual([e["hop_index"] for e in entries], [0, 1])
        self.assertTrue(all(e["round_index"] == 0 for e in entries))
        self.assertEqual(entries[0]["status"], "ok")
        self.assertTrue(entries[0]["used_evidence_refs"], "第 1 跳的留痕要带证据引用")
        self.assertIn("latency_ms", entries[0])

    def test_missing_hop_triggers_one_repair_round(self):
        events = []
        retriever = _FakeRetriever({"医保新规": [_evidence("article:1", "医保新规落地")],
                                    "民营医院压力": []})
        merged, receipts = _run_multi_hop(
            retriever, _plan(),
            {"evidence": [_evidence("article:1", "医保新规落地")]}, {}, pack_id="health",
            limit=8,
            emit_stage_event=lambda kind, payload=None: events.append((kind, payload or {})),
        )
        # h2 没命中 → 触发一轮补检（r1），而不是直接认输
        self.assertIn("r1", [item["hop_id"] for item in receipts])
        repair = next(item for item in receipts if item["hop_id"] == "r1")
        self.assertIn(repair["status"], ("ok", "empty"))
        self.assertEqual(merged["multi_hop"]["recursion_rounds"], 1)
        joined = " ".join(str(payload.get("message") or "") for _kind, payload in events)
        self.assertIn("补检", joined)

    def test_budget_exhausted_stops_recursion(self):
        config.QA_MULTI_HOP_BUDGET_SECONDS = 0.05
        events = []
        retriever = _FakeRetriever({"医保新规": [_evidence("article:1", "医保新规落地")],
                                    "民营医院压力": [_evidence("article:2", "第二跳证据")]},
                                   delay=0.12)
        merged, receipts = _run_multi_hop(
            retriever, _plan3(),
            {"evidence": [_evidence("article:1", "医保新规落地")]}, {}, pack_id="health",
            limit=8,
            emit_stage_event=lambda kind, payload=None: events.append((kind, payload or {})),
        )
        statuses = [item["status"] for item in receipts]
        self.assertIn("skipped_budget", statuses, "预算用尽必须停跳")
        joined = " ".join(str(payload.get("message") or "") for _kind, payload in events)
        self.assertIn("预算", joined)
        self.assertTrue(merged["multi_hop"]["degraded"])


class SessionConstraintTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "session.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.store = QaStore(self.db)
        self.store.ensure_schema()

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def test_round_trip_and_scope_isolation(self):
        self.store.save_session_constraints(
            owner_user_id="u1", session_id="s1", industry_pack_id="family_office",
            constraints={"output_form": "table", "time_window": {"label": "近 90 天"},
                         "entities": ["家族信托"], "must_fetch_fulltext": False},
        )
        loaded = self.store.session_constraints(owner_user_id="u1", session_id="s1",
                                                industry_pack_id="family_office")
        self.assertEqual(loaded["output_form"], "table")
        self.assertEqual(loaded["time_window"]["label"], "近 90 天")
        self.assertNotIn("must_fetch_fulltext", loaded, "空值不该被固化")
        # 作用域隔离：换用户 / 换会话 / 换包都读不到
        self.assertEqual(self.store.session_constraints(owner_user_id="u2", session_id="s1"), {})
        self.assertEqual(self.store.session_constraints(owner_user_id="u1", session_id="s2"), {})
        self.assertEqual(self.store.session_constraints(owner_user_id="u1", session_id="s1",
                                                        industry_pack_id="healthcare_news"), {})

    def test_empty_session_not_persisted(self):
        self.assertEqual(self.store.save_session_constraints(
            owner_user_id="u1", session_id="", industry_pack_id="p",
            constraints={"output_form": "table"}), 0)
        self.assertEqual(self.store.session_constraints(owner_user_id="u1", session_id=""), {})

    def test_persist_helper_extracts_confirmed_constraints(self):
        run_meta = {"id": "run-9", "owner_user_id": "u1", "session_id": "s1",
                    "industry_pack_id": "family_office"}
        plan_result = {
            "time_window_adjustment": {"label": "近 90 天", "days": 90},
            "output_form": "paragraph_by_paragraph",
            "must_fetch_fulltext": True,
            "entities": ["家族信托", "税务宽免"],
        }
        written = _persist_session_constraints(self.store, run_meta, plan_result)
        self.assertGreaterEqual(written, 3)
        loaded = self.store.session_constraints(owner_user_id="u1", session_id="s1",
                                                industry_pack_id="family_office")
        self.assertEqual(loaded["output_form"], "paragraph_by_paragraph")
        self.assertTrue(loaded["must_fetch_fulltext"])
        # 没有会话 id 时不固化
        self.assertEqual(_persist_session_constraints(
            self.store, {"owner_user_id": "u1", "session_id": ""}, plan_result), 0)


class RankingWeightTests(unittest.TestCase):
    def test_balanced_matches_historical_constants(self):
        weights = ranking_weights()
        historical = {"title_phrase": 32, "title_amount": 36, "title": 8, "keyword": 5,
                      "body_phrase": 10, "body_amount": 16, "body": 1.5,
                      "anchor_coverage": 12, "semantic": 10, "in_window_bonus": 8}
        for key, value in historical.items():
            self.assertEqual(float(weights[key]), float(value),
                             "默认档 %s 必须与原硬编码相等（等价替换）" % key)
        self.assertEqual(float(weights["freshness"]), 1.0)

    def test_profile_switch(self):
        self.assertGreater(PROFILES["freshness"]["in_window_bonus"],
                           PROFILES["balanced"]["in_window_bonus"])
        self.assertGreater(PROFILES["authority"]["authority"],
                           PROFILES["balanced"]["authority"])
        self.assertGreater(PROFILES["coverage"]["coverage"],
                           PROFILES["balanced"]["coverage"])
        self.assertEqual(ranking_weights("freshness")["in_window_bonus"], 16.0)
        self.assertEqual(ranking_weights("不存在").get("title_phrase"), 32.0, "非法档位退回默认")

    def test_env_override_single_key(self):
        os.environ["QA_RANKING_WEIGHT_IN_WINDOW_BONUS"] = "20"
        try:
            self.assertEqual(ranking_weights()["in_window_bonus"], 20.0)
        finally:
            os.environ.pop("QA_RANKING_WEIGHT_IN_WINDOW_BONUS", None)
        os.environ["QA_RANKING_PROFILE"] = "coverage"
        try:
            self.assertEqual(ranking_weights()["anchor_coverage"], 16.0)
        finally:
            os.environ.pop("QA_RANKING_PROFILE", None)

    def test_retrieval_uses_weight_table(self):
        """检索侧必须真的走权重表（改单项权重就会改打分）。"""
        import qa_retrieval

        os.environ["QA_RANKING_WEIGHT_IN_WINDOW_BONUS"] = "40"
        try:
            self.assertEqual(qa_retrieval._ranking_weights()["in_window_bonus"], 40.0)
        finally:
            os.environ.pop("QA_RANKING_WEIGHT_IN_WINDOW_BONUS", None)


class AttributionHopTests(unittest.TestCase):
    """阶段 10-5：【证据解析】要能展开"每一跳的依据 + 缺失链接"。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "attr.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.store = QaStore(self.db)
        self.store.ensure_schema()

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def test_public_hops_exposes_evidence_and_missing_links(self):
        from qa_attribution import QaAttributionService

        self.store.record_reasoning_trace("run-h", hop_index=0, sub_query_id="h1",
                                          sub_query="A 的事实", status="ok",
                                          used_evidence_refs=["article:1", "edge:x"])
        self.store.record_reasoning_trace("run-h", hop_index=1, sub_query_id="h2",
                                          sub_query="A 对 B", status="empty",
                                          missing_links=[{"type": "hop_missing", "detail": "没证据"}],
                                          next_queries=["A 对 B 影响"])
        service = QaAttributionService(self.db, store=self.store)
        hops = service._public_hops("run-h")
        self.assertEqual(len(hops), 2)
        self.assertEqual(hops[0]["evidence_refs"], ["article:1", "edge:x"])
        self.assertEqual(hops[1]["status"], "empty")
        self.assertEqual(hops[1]["missing_links"][0]["type"], "hop_missing")
        self.assertEqual(hops[1]["next_queries"], ["A 对 B 影响"])
        self.assertEqual(service._public_hops("不存在"), [])


if __name__ == "__main__":
    unittest.main()
