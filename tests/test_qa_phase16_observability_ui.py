#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 16 · 只读投影层用例（P16-01…P16-06）。

覆盖四件事（每个 Must 子任务都有正例 + 边界/失败路径）：
  1. **六个视图在真实产物上真的出数**（数据全部来自真链路构造的阶段产物）；
  2. **每个数字都能追到某个阶段的产物**（`numbers[*].source` 必须命中来源白名单，
     且与 owning phase 自己的数字逐字相等 —— 不许在 UI 层另算一套）；
  3. **空态 / 降级态 / 不可得态**都如实呈现并点名理由码（绝不用 0 冒充）；
  4. **只读**：跑一遍视图不改任何一行（阶段行数、记忆条数、审计行数逐字不变）。
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_graph_contracts as contracts  # noqa: E402
import qa_observability_ui as ui  # noqa: E402
import qa_phase16_fixtures as fx  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

RUN_VIEWS = ("execution", "evidence", "context", "memory", "search_trace")


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db, self.store = fx.store_for(self.temp_dir.name)
        self.assertEqual(self.db.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        self.run, self.receipt = fx.seed_standard_run(self.db, self.store)
        self.run_id = str(self.run["id"])

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _all_views(self):
        out = {name: ui.build_view(self.store, self.run_id, name) for name in RUN_VIEWS}
        out["dashboard"] = ui.dashboard(self.store)
        return out

    def _counts(self):
        def _row(sql):
            return int(self.store.database.connection.execute(sql).fetchone()[0] or 0)

        return {
            "stage_runs": _row("SELECT count(*) FROM qa_stage_runs"),
            "memory_item": _row("SELECT count(*) FROM memory_item"),
            "memory_recall_log": _row("SELECT count(*) FROM memory_recall_log"),
            "traces": _row("SELECT count(*) FROM qa_reasoning_traces"),
            "audit": _row("SELECT count(*) FROM qa_audit_events"),
            "cache": _row("SELECT count(*) FROM qa_retrieval_cache"),
        }


class ContractShapeTests(_Base):
    def test_every_view_satisfies_the_contract(self):
        for name, view in self._all_views().items():
            ok, note = validate("observability_view", view)
            self.assertTrue(ok, "%s 视图不符契约：%s" % (name, note))
            self.assertEqual(view["view"], name)
            self.assertIs(view["read_only"], True)
            self.assertTrue(view["state_text"], "%s 必须有给人看的状态说明" % name)
            self.assertTrue(view["source"], "%s 必须写明数据来源" % name)

    def test_every_number_carries_a_whitelisted_source_and_a_definition(self):
        for name, view in self._all_views().items():
            self.assertTrue(view["numbers"], "%s 必须至少给一条口径数字" % name)
            for field, item in view["numbers"].items():
                ok, note = validate("observability_number", item)
                self.assertTrue(ok, "%s.%s 口径数字不合契约：%s" % (name, field, note))
                self.assertEqual(item["field"], field)
                self.assertTrue(item["definition"].strip(),
                                "%s.%s 缺口径说明" % (name, field))
                self.assertTrue(
                    any(str(item["source"]).startswith(prefix)
                        for prefix in contracts.OBSERVABILITY_SOURCES),
                    "%s.%s 的来源 %r 不在白名单里（说明 UI 层另算了一套）"
                    % (name, field, item["source"]))

    def test_states_and_reasons_are_in_the_closed_sets(self):
        for name, view in self._all_views().items():
            self.assertIn(view["state"], contracts.OBSERVABILITY_STATES, name)
            self.assertIn(view["state_reason"], contracts.OBSERVABILITY_STATE_REASONS, name)


class CrossPhaseConsistencyTests(_Base):
    """界面上每个数字都能追到某个阶段的产物 —— 与 owning phase 的数字逐字相等。"""

    def test_execution_view_matches_the_persisted_receipt(self):
        view = ui.execution_view(self.store, self.run_id)
        self.assertEqual(view["state"], "ok")
        receipt = view["payload"]["receipt"]
        self.assertEqual(view["numbers"]["path"]["value"], receipt["path"])
        self.assertEqual(view["numbers"]["nodes_total"]["value"], receipt["node_counts"]["total"])
        self.assertEqual(view["numbers"]["budget_total_seconds"]["value"],
                         receipt["budget"]["total_seconds"])
        self.assertEqual(view["numbers"]["stop_reason"]["value"], receipt["stop_reason"])
        self.assertEqual(receipt, self.receipt["receipt"], "回执必须逐字来自阶段产物")

    def test_execution_node_detail_uses_phase05_builder(self):
        from qa_execution_graph import build_execution_graph

        view = ui.execution_view(self.store, self.run_id)
        plan = self.store.stage_runs(self.run_id)
        plan_result = [row for row in plan if row["stage"] == "plan"][0]["details"]["result"]
        rebuilt = build_execution_graph(fx.QUESTION, plan=plan_result, mode="standard",
                                        run_id=self.run_id)
        self.assertEqual(view["payload"]["detail_source"], "rebuilt")
        self.assertEqual([n["node_id"] for n in view["payload"]["nodes"]],
                         [n["node_id"] for n in rebuilt["nodes"]],
                         "节点明细必须由 Phase 05 的构建器复算，不许自己画")
        self.assertEqual(len(view["payload"]["edges"]), len(rebuilt["edges"]))

    def test_evidence_view_is_the_same_code_path_as_the_gateway(self):
        from qa_gateway import QaGatewayService
        from qa_storage import QaStore

        service = QaGatewayService(QaStore(self.db), None)
        api_payload = service.evidence_graph(self.run_id, owner_user_id="p16", limit=20)
        view = ui.evidence_view(self.store, self.run_id, limit=20)
        self.assertEqual(view["payload"]["coverage"], api_payload["coverage"])
        self.assertEqual(view["payload"]["stats"], api_payload["stats"])
        self.assertEqual([item["claim_id"] for item in view["payload"]["claims"]],
                         [item["claim_id"] for item in api_payload["claims"]],
                         "页面数字必须与 /evidence-graph 接口逐字一致（同一读侧）")
        self.assertEqual(view["numbers"]["claim_coverage"]["value"],
                         api_payload["coverage"]["claim_coverage"])

    def test_context_view_numbers_come_from_the_pack(self):
        view = ui.context_inspector(self.store, self.run_id)
        self.assertEqual(view["state"], "ok")
        pack = self.receipt["graph"]["context_pack"]
        self.assertEqual(view["numbers"]["items_selected"]["value"], len(pack["items"]))
        self.assertEqual(view["numbers"]["tokens_used"]["value"], pack["budget"]["used"])
        self.assertEqual(view["numbers"]["items_dropped"]["value"],
                         pack["budget"]["trimmed_items"])
        self.assertEqual(view["numbers"]["citations"]["value"], len(pack["citation_map"]))
        # §15 的三个问题逐项可答：为何加入 / 来自哪 / 是否复验
        for item in view["payload"]["items"]:
            self.assertIn(item["origin"], {"evidence", "memory", "skill", "task", "context"})
            self.assertIn("why_included_known", item)
        with_trace = [item for item in view["payload"]["items"] if item["why_included_known"]]
        self.assertTrue(with_trace, "至少有条目应当带 Phase 08 的入选理由")
        for item in with_trace:
            self.assertTrue(item["why_included"], "有决策行的条目必须有入选理由")
        without_trace = [item for item in view["payload"]["items"]
                         if not item["why_included_known"]]
        self.assertEqual(len(without_trace),
                         view["numbers"]["items_without_trace"]["value"],
                         "没有决策行的条目数必须与口径数字一致（不许把空白说成没理由）")
        self.assertTrue(view["numbers"]["from_memory"]["value"] >= 1,
                        "记忆条目应当进 memory_context 段（fixture 里真跑了召回）")
        self.assertTrue(view["numbers"]["from_skill"]["value"] >= 1,
                        "技能条目应当进 skill_context 段（fixture 里真跑了路由）")

    def test_memory_view_numbers_come_from_the_owning_tables(self):
        view = ui.memory_view(self.store, self.run_id)
        stats = self.store.memory_stats(include_all_scopes=True)
        self.assertEqual(view["numbers"]["items_total"]["value"], stats["items"])
        self.assertEqual(view["numbers"]["by_type"]["value"],
                         dict(sorted(stats["by_type"].items(),
                                     key=lambda kv: (-kv[1], kv[0]))))
        decisions = self.store.memory_write_decisions(run_id=self.run_id, limit=200)
        expected_ids = {str(row.get("memory_id") or "") for row in decisions
                        if str(row.get("memory_id") or "")}
        self.assertEqual(view["numbers"]["items_created_by_this_run"]["value"], len(expected_ids))
        for item in view["payload"]["items"]:
            for key in ("memory_type", "confidence", "freshness_class", "last_verified_at",
                        "reuse_count", "status", "superseded_by"):
                self.assertIn(key, item, "Memory View 必须给出 §15 要求的字段：%s" % key)

    def test_search_trace_additions_come_from_each_owning_phase(self):
        """§15 的六个追加字段必须齐备，且各自等于 owning phase 的数字。"""
        view = ui.search_trace(self.store, self.run_id)
        additions = view["payload"]["additions"]
        self.assertEqual(set(additions), set(ui.SEARCH_TRACE_ADDITIONS))
        self.assertEqual(additions["memory_recalled"], view["numbers"]["memory_recalled"]["value"])
        recalls = self.store.observability_memory_recalls(run_id=self.run_id)
        self.assertEqual(additions["memory_recalled"],
                         sum(int(row.get("hits") or 0) for row in recalls))
        pack = self.receipt["graph"]["context_pack"]
        self.assertEqual(additions["context_items_selected"], len(pack["items"]))
        self.assertEqual(additions["context_tokens"], pack["budget"]["used"])
        self.assertEqual(additions["context_items_dropped"], pack["budget"]["trimmed_items"])
        loaded = (pack["sections"]["skill_context"] or {}).get("loaded_skills") or []
        self.assertEqual(additions["skills_loaded"], len(loaded))
        self.assertEqual(view["payload"]["skills_loaded"], list(loaded))
        # §23 要求 UI 能回答的九个问题必须逐条有字段
        keys = {item["key"] for item in view["payload"]["questions"]}
        self.assertEqual(keys, {key for key, _text in ui.SEARCH_TRACE_QUESTIONS})
        for action in view["payload"]["actions"]:
            for key in ("why_search", "query", "route", "results", "accepted", "rejected",
                        "resolved_gap_text", "continue_reason", "stop_reason"):
                self.assertIn(key, action)

    def test_dashboard_latency_comes_from_the_existing_metrics_service(self):
        from qa_observability import QaMetricsService

        view = ui.dashboard(self.store)
        snapshot = QaMetricsService(self.db).snapshot()
        self.assertEqual(view["numbers"]["runs"]["value"], snapshot["runs"])
        self.assertEqual(view["numbers"]["by_status"]["value"],
                         dict(sorted(snapshot["by_status"].items(),
                                     key=lambda kv: (-kv[1], kv[0]))))
        self.assertEqual(view["numbers"]["stage_p95_ms"]["value"],
                         {name: row["p95_ms"] for name, row in snapshot["stages"].items()})

    def test_dashboard_token_totals_are_sums_of_recorded_keys(self):
        view = ui.dashboard(self.store)
        rows = self.store.database.connection.execute(
            "SELECT token_usage_json FROM qa_stage_runs WHERE token_usage_json NOT IN ('', '{}')"
        ).fetchall()
        expected = 0
        for row in rows:
            import json as _json

            expected += int(_json.loads(row[0]).get("tokens_total") or 0)
        self.assertEqual(view["numbers"]["tokens_total"]["value"], expected)
        self.assertGreater(expected, 0)

    def test_dashboard_cache_namespaces_are_read_not_hardcoded(self):
        view = ui.dashboard(self.store)
        namespaces = view["numbers"]["cache_by_namespace"]["value"]
        self.assertEqual(set(namespaces), {"level1_retrieval", "level2_retrieval",
                                          "qa_verification"})
        self.assertTrue(all(count == 1 for count in namespaces.values()))

    def test_dashboard_skill_rate_uses_phase11_definition(self):
        view = ui.dashboard(self.store)
        # fixture 只写了 1 条遥测 → 低于 Phase 11 的样本门限 → 成功率必须是空值而不是 0
        self.assertEqual(view["numbers"]["skill_success_rate"]["value"],
                         {fx.SKILL_ID: None})
        self.assertEqual(view["numbers"]["skill_sample_reason"]["value"],
                         {fx.SKILL_ID: "INSUFFICIENT_SAMPLES"})
        written = fx.seed_skill_rows(self.store, self.run_id, attempts=3)
        self.assertEqual(written, 3)
        view2 = ui.dashboard(self.store)
        self.assertEqual(view2["numbers"]["skill_success_rate"]["value"], {fx.SKILL_ID: 1.0})
        self.assertEqual(view2["numbers"]["skill_sample_reason"]["value"], {fx.SKILL_ID: ""})


class EmptyStateTests(_Base):
    """空态是**真实状态**（A 机当前就没有 QA run 数据），必须做对。"""

    def test_bare_run_gives_empty_states_with_reasons(self):
        bare = fx.bare_run(self.store, question="全新部署里只有 run、没有任何阶段产物")
        bare_id = str(bare["id"])
        # 四个 run 级视图必须如实空态（记忆视图是全局的，同库里已有别的 run 写的记忆）
        for name in ("execution", "evidence", "context", "search_trace"):
            view = ui.build_view(self.store, bare_id, name)
            ok, note = validate("observability_view", view)
            self.assertTrue(ok, note)
            self.assertIn(view["state"], {"empty", "degraded", "unavailable"}, name)
            self.assertIn(view["state_reason"],
                          {"SWITCH_OFF", "NO_STAGE_ARTIFACT", "PARTIAL_READ", "STORE_UNAVAILABLE"},
                          name)
            self.assertTrue(view["state_text"], "%s 空态也要说明为什么空" % name)
        # 没有任何阶段行 → 分不清"开关关着"和"根本没跑"，因此只能给 NO_STAGE_ARTIFACT
        self.assertEqual(ui.execution_view(self.store, bare_id)["state_reason"],
                         "NO_STAGE_ARTIFACT")
        # 记忆视图此时仍能读全局记忆，但**本 run 的**数字必须是 0/空，不能蹭别人的
        memory = ui.memory_view(self.store, bare_id)
        self.assertEqual(memory["numbers"]["items_created_by_this_run"]["value"], 0)
        self.assertEqual(memory["numbers"]["run_recall_rows"]["value"], 0)
        self.assertEqual(memory["numbers"]["run_validations"]["value"], 0)
        self.assertEqual(memory["payload"]["run_items"], [])

    def test_switch_off_is_proven_from_the_read_side_only_with_stage_rows(self):
        """`SWITCH_OFF` 只在"确实跑过阶段 + 开关当前关"时才敢给（否则一律 NO_STAGE_ARTIFACT）。"""
        saved = {name: os.environ.pop(name, None)
                 for name in ("QA_EXECUTION_GRAPH", "QA_CONTEXT_PACK")}
        try:
            store = self.store
            run = fx.new_run(store, question="开关关着、只留阶段行的 run")
            run_id = str(run["id"])
            store.record_stage(run_id, "plan", status="completed", details={"result": {}})
            execution = ui.execution_view(store, run_id)
            self.assertEqual(execution["state_reason"], "SWITCH_OFF")
            self.assertIn("读侧探测", execution["state_text"])
            self.assertEqual(ui.context_inspector(store, run_id)["state_reason"], "SWITCH_OFF")
            # 打开开关后同一条 run 必须改口（理由跟着事实走，不是写死的）
            os.environ["QA_EXECUTION_GRAPH"] = "1"
            os.environ["QA_CONTEXT_PACK"] = "1"
            self.assertEqual(ui.execution_view(store, run_id)["state_reason"],
                             "NO_STAGE_ARTIFACT")
            self.assertEqual(ui.context_inspector(store, run_id)["state_reason"],
                             "NO_STAGE_ARTIFACT")
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_no_run_at_all_gives_the_global_empty_state(self):
        self.temp_dir2 = tempfile.TemporaryDirectory()
        db, store = fx.store_for(self.temp_dir2.name, "empty16.sqlite3")
        try:
            for name in RUN_VIEWS:
                view = ui.build_view(store, "no-such-run", name)
                self.assertEqual(view["state"], "empty")
                self.assertIn(view["state_reason"],
                              {"SWITCH_OFF", "NO_STAGE_ARTIFACT", "NO_MEMORY_IN_SCOPE"}, name)
            # 一条阶段行都没有 → 不许说"开关关着"（分不清），只能说"没有产物"
            self.assertEqual(ui.execution_view(store, "no-such-run")["state_reason"],
                             "NO_STAGE_ARTIFACT")
            dashboard = ui.dashboard(store)
            self.assertEqual(dashboard["state"], "empty")
            self.assertEqual(dashboard["state_reason"], "NO_RUN_IN_SCOPE")
            self.assertEqual(dashboard["numbers"]["runs"]["value"], 0)
            self.assertIsNone(dashboard["numbers"]["tokens_total"]["value"],
                              "空库的 token 总数必须是 null，不许冒充 0（没有记录 ≠ 记为 0）")
            self.assertEqual(dashboard["numbers"]["cache_entries"]["value"], 0,
                             "缓存表存在且确实一行都没有 → 0 是真实计数")
            self.assertEqual(ui.memory_view(store, "no-such-run")["state_reason"],
                             "NO_MEMORY_IN_SCOPE")
        finally:
            try:
                db.connection.close()
            except Exception:
                pass
            self.temp_dir2.cleanup()

    def test_empty_state_numbers_are_null_not_zero(self):
        view = ui.execution_view(self.store, str(fx.bare_run(self.store)["id"]))
        self.assertIsNone(view["numbers"]["path"]["value"])
        self.assertIsNone(view["numbers"]["nodes_total"]["value"])
        self.assertIsNone(view["numbers"]["stop_reason"]["value"])


class DegradedStateTests(_Base):
    def test_evidence_view_flags_a_phase_error(self):
        store = self.store
        run = fx.new_run(store, question="证据图带错误标记的 run")
        run_id = str(run["id"])
        store.record_stage(run_id, "conflict_review", status="degraded",
                           error_code="EVIDENCE_GRAPH_FAILED",
                           details={"result": {"evidence_graph": {
                               "graph_version": "qa-evidence-graph-v1", "error": "boom",
                               "run_id": run_id, "claims": [], "edges": [], "nodes": [],
                               "coverage": {}, "stats": {}, "contradictions": []}}})
        view = ui.evidence_view(store, run_id)
        self.assertEqual(view["state"], "degraded")
        self.assertEqual(view["state_reason"], "PHASE_DEGRADED")
        self.assertIn("boom", view["state_text"])

    def test_context_inspector_degrades_on_receipt_only(self):
        """快速路径只在 SSE 事件里留下计数回执 → 必须降级呈现，不能装作有明细。"""
        from qa_sse import qa_event

        store = self.store
        run = fx.new_run(store, question="只有上下文包计数回执的 run", mode="fast")
        run_id = str(run["id"])
        store.append_event(qa_event(event_type="stage_progress", run_id=run_id, event_id=1,
                                    stage="synthesis",
                                    payload={"context_pack": {"pack_id": "CP-x", "items": 7,
                                                              "citations": 3,
                                                              "context_gaps": {"TRUNCATED_SPAN": 2},
                                                              "retrieval_requested": 0,
                                                              "budget": {"used": 900,
                                                                         "estimated_tokens_before": 1500,
                                                                         "trimmed_items": 4}}}))
        view = ui.context_inspector(store, run_id)
        self.assertEqual(view["state"], "degraded")
        self.assertEqual(view["state_reason"], "PARTIAL_READ")
        self.assertFalse(view["payload"]["detail_available"])
        # 回执口径下的字段名与明细口径不同（回执只有计数）—— 页面必须按状态取
        self.assertEqual(view["numbers"]["items"]["value"], 7)
        self.assertEqual(view["numbers"]["context_tokens"]["value"], 900)
        self.assertEqual(view["numbers"]["trimmed_items"]["value"], 4)
        self.assertEqual(view["numbers"]["context_gaps"]["value"], 2)
        self.assertEqual(view["payload"]["items"], [], "回执态不许伪造逐项明细")
        self.assertIn("qa_events", view["source"])
        ok, note = validate("observability_view", view)
        self.assertTrue(ok, note)

    def test_memory_view_degrades_when_one_reader_fails(self):
        def _boom(**_kwargs):
            raise RuntimeError("stats unavailable")

        self.store.memory_stats = _boom
        view = ui.memory_view(self.store, self.run_id)
        self.assertEqual(view["state"], "degraded")
        self.assertEqual(view["state_reason"], "PARTIAL_READ")
        self.assertIn("memory_stats", view["payload"]["read_errors"])

    def test_memory_view_is_unavailable_when_everything_fails(self):
        def _boom(**_kwargs):
            raise RuntimeError("no table")

        self.store.memory_stats = _boom
        self.store.memory_validations = _boom
        original = self.store.load_memory_items

        def _no_items(**_kwargs):
            return []

        self.store.load_memory_items = _no_items
        try:
            view = ui.memory_view(self.store, self.run_id)
        finally:
            self.store.load_memory_items = original
        self.assertEqual(view["state"], "unavailable")
        self.assertEqual(view["state_reason"], "STORE_UNAVAILABLE")

    def test_search_trace_degrades_when_the_trace_reader_fails(self):
        def _boom(_run_id):
            raise RuntimeError("trace table missing")

        self.store.reasoning_traces = _boom
        view = ui.search_trace(self.store, self.run_id)
        self.assertEqual(view["state"], "degraded")
        self.assertEqual(view["state_reason"], "PARTIAL_READ")
        self.assertTrue(view["payload"]["read_error"])
        # 六个追加字段仍然要给（它们来自别的阶段，不该被一路读失败带走）
        self.assertEqual(set(view["payload"]["additions"]), set(ui.SEARCH_TRACE_ADDITIONS))

    def test_dashboard_is_unavailable_when_metrics_fail(self):
        class _BadMetrics:
            def snapshot(self, **_kwargs):
                raise RuntimeError("metrics down")

        view = ui.dashboard(self.store, metrics=_BadMetrics())
        self.assertEqual(view["state"], "unavailable")
        self.assertEqual(view["state_reason"], "STORE_UNAVAILABLE")
        self.assertIn("metrics", view["payload"]["read_errors"])

    def test_execution_view_degrades_when_no_node_is_runnable(self):
        store = self.store
        run = fx.new_run(store, question="执行图回执里没有可运行节点的 run")
        run_id = str(run["id"])
        store.record_stage(run_id, "level1_retrieval", status="completed",
                           details={"result": {"stats": {"execution_graph": {
                               "graph_version": "qa-execution-graph-v1", "path": "fast",
                               "node_counts": {"total": 3, "runnable": 0}}}}})
        view = ui.execution_view(store, run_id)
        self.assertEqual(view["state"], "degraded")
        self.assertEqual(view["state_reason"], "PARTIAL_READ")
        self.assertEqual(view["numbers"]["nodes_total"]["value"], 3)


class ReadOnlyTests(_Base):
    """P16 硬约束：界面只读 —— 跑一遍视图，一行都不许变。"""

    def test_running_every_view_changes_nothing(self):
        before = self._counts()
        first = self._all_views()
        middle = self._counts()
        second = self._all_views()
        after = self._counts()
        self.assertEqual(before, middle, "跑视图改动了数据库（before/middle 不一致）")
        self.assertEqual(before, after, "跑视图改动了数据库（before/after 不一致）")
        for name in list(first):
            left, right = first[name], second[name]
            self.assertEqual(left["state"], right["state"], name)
            self.assertEqual(left["numbers"], right["numbers"], "%s 两次结果不同（不确定）" % name)

    def test_view_builder_rejects_unknown_names(self):
        with self.assertRaises(KeyError):
            ui.build_view(self.store, self.run_id, "not-a-view")

    def test_run_views_returns_all_five_run_scoped_views(self):
        payload = ui.run_views(self.store, self.run_id)
        self.assertEqual(set(payload["views"]), set(RUN_VIEWS))
        self.assertIs(payload["read_only"], True)
        self.assertEqual(payload["run_id"], self.run_id)
        for name, view in payload["views"].items():
            ok, note = validate("observability_view", view)
            self.assertTrue(ok, "%s: %s" % (name, note))

    def test_switches_off_gives_empty_instead_of_fake_zero(self):
        """开关关着（没有 context_pack / 没有 execution_graph）时必空态，且数字是 null。"""
        store = self.store
        run = fx.seed_standard_run(self.db, store, with_context_pack=False,
                                   with_evidence_graph=False, with_memory=False,
                                   with_skill=False, traces=(),
                                   question="只跑检索、没有 P06/P08/P09/P11 产物的 run")[0]
        run_id = str(run["id"])
        context = ui.context_inspector(store, run_id)
        self.assertEqual(context["state"], "empty")
        self.assertEqual(len(context["numbers"]), 1)
        self.assertIsNone(context["numbers"]["items"]["value"])
        trace = ui.search_trace(store, run_id)
        self.assertEqual(trace["state"], "empty")
        self.assertIsNone(trace["payload"]["additions"]["context_items_selected"],
                          "没有上下文包产物时必须是 null 而不是 0")
        self.assertIsNone(trace["payload"]["additions"]["context_tokens"])
        self.assertIsNone(trace["payload"]["additions"]["context_items_dropped"])
        # 召回/复验行数分列，用来区分"召回 0 条"与"根本没跑召回"
        self.assertEqual(trace["numbers"]["run_recall_rows"]["value"], 0)
        self.assertEqual(trace["payload"]["additions"]["memory_recalled"], 0)
        evidence = ui.evidence_view(store, run_id)
        self.assertIn(evidence["state"], {"empty", "degraded"})


if __name__ == "__main__":
    unittest.main()
