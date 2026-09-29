import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from financial_instruments import InstrumentRegistry
from sqlite_database import SQLiteDatabase
from stock_research_graph import (
    CHECKPOINT_SCHEMA_VERSION,
    OUTPUT_CLASSIFICATION,
    STOCK_GRAPH_VERSION,
    StockResearchGraph,
    StockResearchGraphError,
    StockResearchGraphInterrupted,
)
from tradingagents_llm_adapter import TradingAgentsLLMAdapterError


NOW = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)


class _Broker:
    def runtime_identity(self):
        return {
            "model_id": "fixture-local-model",
            "provider_id": "local",
            "base_url": "http://fixture.invalid/v1",
            "runtime_source": "fixture",
        }


class _LLMFactory:
    def __init__(self, run_id):
        self.run_context = SimpleNamespace(research_run_id=run_id)
        self.broker = _Broker()


class _DataAdapter:
    def __init__(self, run_id, instrument_id, snapshot_id, *, market_available=True):
        self.context = SimpleNamespace(
            research_run_id=run_id,
            instrument_id=instrument_id,
            server_now_utc=NOW,
            cutoff_at_utc=NOW,
        )
        self.snapshot_id = snapshot_id
        self.market_available = market_available
        self.calls = []

    def _value(self, tool, status):
        self.calls.append(tool)
        return json.dumps(
            {
                "tool": tool,
                "status": status,
                "snapshot_id": self.snapshot_id,
                "observed_at": "2026-07-31T07:59:00Z",
            }
        )

    def get_stock_data(self, symbol, start_date, end_date):
        del symbol, start_date, end_date
        return self._value("get_stock_data", "fetched" if self.market_available else "unavailable")

    def get_verified_market_snapshot(self, symbol, curr_date, look_back_days):
        del symbol, curr_date, look_back_days
        return self._value(
            "get_verified_market_snapshot",
            "completed" if self.market_available else "unavailable",
        )

    def get_sentiment_inputs(self, symbol, start_date, end_date):
        del symbol, start_date, end_date
        return self._value("get_sentiment_inputs", "complete")

    def get_news(self, symbol, start_date, end_date):
        del symbol, start_date, end_date
        return self._value("get_news", "complete")

    def get_fundamentals(self, symbol, curr_date):
        del symbol, curr_date
        return self._value("get_fundamentals", "complete")


def _role_nodes(call_log, *, fail_role="", fail_once=None):
    fail_state = fail_once if fail_once is not None else {"raised": False}

    def record(role, update):
        def node(state):
            del state
            call_log.append(role)
            if role == fail_role and not fail_state["raised"]:
                fail_state["raised"] = True
                raise TradingAgentsLLMAdapterError(
                    "fixture endpoint detail",
                    error_code="llm_timeout",
                    role_key=role,
                )
            return update

        return node

    def debate(role, speaker, history_key):
        def node(state):
            call_log.append(role)
            if role == fail_role and not fail_state["raised"]:
                fail_state["raised"] = True
                raise TradingAgentsLLMAdapterError(
                    "fixture endpoint detail",
                    error_code="llm_timeout",
                    role_key=role,
                )
            value = dict(state["investment_debate_state"])
            argument = f"{speaker}: fixture {role} argument with snapshot citation."
            value["history"] += "\n" + argument
            value[history_key] += "\n" + argument
            value["current_response"] = argument
            value["count"] += 1
            return {"investment_debate_state": value}

        return node

    def risk(role, speaker, history_key, current_key):
        def node(state):
            call_log.append(role)
            value = dict(state["risk_debate_state"])
            argument = f"{speaker} Analyst: fixture {role} argument."
            value["history"] += "\n" + argument
            value[history_key] += "\n" + argument
            value[current_key] = argument
            value["latest_speaker"] = speaker
            value["count"] += 1
            return {"risk_debate_state": value}

        return node

    return {
        "market_analyst": record("market_analyst", {"market_report": "Market report cites snapshot 1."}),
        "sentiment_analyst": record("sentiment_analyst", {"sentiment_report": "Sentiment report; confidence bounded."}),
        "news_analyst": record("news_analyst", {"news_report": "News report marks external text untrusted."}),
        "fundamentals_analyst": record("fundamentals_analyst", {"fundamentals_report": "Fundamentals report uses announcement cutoff."}),
        "bull_researcher": debate("bull_researcher", "Bull Analyst", "bull_history"),
        "bear_researcher": debate("bear_researcher", "Bear Analyst", "bear_history"),
        "research_manager": record(
            "research_manager",
            {
                "investment_plan": (
                    "**Recommendation**: Hold\n\n**Rationale**: balanced fixture.\n\n"
                    "**Strategic Actions**: wait for verified evidence."
                )
            },
        ),
        "trader": record(
            "trader",
            {
                "messages": [{"role": "assistant", "content": "fixture trader"}],
                "trader_investment_plan": (
                    "**Action**: Hold\n\n**Reasoning**: fixture.\n\n"
                    "FINAL TRANSACTION PROPOSAL: **HOLD**"
                ),
                "sender": "Trader",
            },
        ),
        "aggressive_risk_analyst": risk(
            "aggressive_risk_analyst", "Aggressive", "aggressive_history", "current_aggressive_response"
        ),
        "conservative_risk_analyst": risk(
            "conservative_risk_analyst", "Conservative", "conservative_history", "current_conservative_response"
        ),
        "neutral_risk_analyst": risk(
            "neutral_risk_analyst", "Neutral", "neutral_history", "current_neutral_response"
        ),
        "portfolio_manager": record(
            "portfolio_manager",
            {
                "final_trade_decision": (
                    "**Rating**: Hold\n\n**Executive Summary**: Maintain observation only; no order.\n\n"
                    "**Investment Thesis**: Evidence is balanced and bounded.\n\n"
                    "**Time Horizon**: 1-3 months"
                )
            },
        ),
    }


class StockResearchGraphTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "stock-graph.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.connection = self.database.connection
        self.registry = InstrumentRegistry(self.connection)
        self.registry.load_controlled_seed()
        self.instrument = self.registry.get_by_canonical_symbol("000001.SZ")
        self.run_id = "stock-graph-run"
        self._insert_run(self.run_id, self.instrument.instrument_id)
        self.snapshot_id = self._insert_evidence(self.run_id, self.instrument.instrument_id)

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _insert_run(self, run_id, instrument_id):
        self.connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, instrument_id, status, requested_at
            ) VALUES(?, 'test', 'instrument', ?, 'running', '2026-07-31T08:00:00.000Z')
            """,
            (run_id, instrument_id),
        )

    def _insert_evidence(self, run_id, instrument_id):
        self.connection.execute(
            """
            INSERT INTO financial_provider_profiles(
                provider_key, display_name, provider_type, access_tier,
                capabilities_json, is_enabled
            ) VALUES('stock_graph_fixture', 'fixture', 'fixture', 'test', '["bar"]', 1)
            ON CONFLICT(provider_key) DO NOTHING
            """
        )
        provider_id = self.connection.execute(
            "SELECT id FROM financial_provider_profiles WHERE provider_key='stock_graph_fixture'"
        ).fetchone()[0]
        payload = json.dumps({"normalized_payload": {"close": 10.0}}, sort_keys=True)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        key = f"stock-graph-fixture-{run_id}"
        self.connection.execute(
            """
            INSERT INTO financial_data_snapshots(
                snapshot_key, instrument_id, provider_profile_id, data_type,
                observed_at, fetched_at, payload_json, payload_sha256
            ) VALUES(?, ?, ?, 'quote', '2026-07-31T07:59:00Z',
                     '2026-07-31T08:00:00Z', ?, ?)
            """,
            (key, instrument_id, provider_id, payload, digest),
        )
        snapshot_id = self.connection.execute(
            "SELECT id FROM financial_data_snapshots WHERE snapshot_key=?", (key,)
        ).fetchone()[0]
        self.connection.execute(
            """
            INSERT INTO financial_research_evidence(
                research_run_id, evidence_key, evidence_kind, snapshot_id,
                instrument_id, evidence_role, match_method, match_score,
                observed_at, metadata_json
            ) VALUES(?, ?, 'structured_snapshot', ?, ?, 'fixture', 'direct', 1,
                     '2026-07-31T07:59:00Z', '{"tool_name":"get_verified_market_snapshot"}')
            """,
            (run_id, f"fixture:{run_id}", snapshot_id, instrument_id),
        )
        return int(snapshot_id)

    def _graph(self, nodes, *, adapter=None, callback=None, run_id=None, instrument=None):
        actual_run = run_id or self.run_id
        actual_instrument = instrument or self.instrument
        data = adapter or _DataAdapter(
            actual_run, actual_instrument.instrument_id, self.snapshot_id
        )
        return StockResearchGraph(
            self.connection,
            actual_run,
            data_adapter=data,
            llm_factory=_LLMFactory(actual_run),
            nodes=nodes,
            checkpoint_callback=callback,
        )

    def test_complete_role_graph_persists_discussions_and_final_research_opinion(self):
        calls = []
        checkpoints = []
        graph = self._graph(_role_nodes(calls), callback=checkpoints.append)

        result = graph.run()

        self.assertEqual(result["status"], "generated_unverified")
        self.assertEqual(result["recommendation"], "Hold")
        self.assertEqual(result["output_classification"], OUTPUT_CLASSIFICATION)
        self.assertFalse(result["execution_allowed"])
        self.assertEqual(result["evidence_coverage"], 1.0)
        self.assertEqual(result["section_count"], 14)
        self.assertEqual(len(calls), 12)
        self.assertEqual(result["checkpoint"]["state"]["investment_debate_state"]["count"], 2)
        self.assertEqual(result["checkpoint"]["state"]["risk_debate_state"]["count"], 3)
        self.assertEqual(len(result["checkpoint"]["node_trace"]), 12)
        self.assertEqual(result["checkpoint"]["schema_version"], CHECKPOINT_SCHEMA_VERSION)
        self.assertEqual(result["checkpoint"]["graph_version"], STOCK_GRAPH_VERSION)
        self.assertGreaterEqual(len(checkpoints), 14)

        report = self.connection.execute(
            """
            SELECT report_status, recommendation, report_json, report_markdown,
                   verified_at FROM financial_final_reports WHERE id=?
            """,
            (result["report_id"],),
        ).fetchone()
        self.assertEqual(report["report_status"], "generated_unverified")
        self.assertEqual(report["recommendation"], "Hold")
        self.assertIsNone(report["verified_at"])
        self.assertFalse(json.loads(report["report_json"])["execution_allowed"])
        self.assertIn("As of:", report["report_markdown"])
        sections = self.connection.execute(
            "SELECT role_key, citations_json, model_id FROM financial_report_sections WHERE research_run_id=?",
            (self.run_id,),
        ).fetchall()
        self.assertEqual(len(sections), 14)
        self.assertTrue(all(json.loads(row["citations_json"]) for row in sections))
        self.assertTrue(all(row["model_id"] == "fixture-local-model" for row in sections))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0], 0)

    def test_llm_interruption_returns_last_checkpoint_and_resume_does_not_repeat_roles(self):
        first_calls = []
        shared_failure = {"raised": False}
        graph = self._graph(
            _role_nodes(first_calls, fail_role="bear_researcher", fail_once=shared_failure)
        )

        with self.assertRaises(StockResearchGraphInterrupted) as caught:
            graph.run()

        interrupted = caught.exception
        self.assertEqual(interrupted.error_code, "llm_timeout")
        self.assertEqual(interrupted.stage_key, "bear_researcher:1")
        self.assertEqual(interrupted.checkpoint["stage_index"], 5)
        self.assertEqual(
            [item["role_key"] for item in interrupted.checkpoint["node_trace"]],
            [
                "market_analyst",
                "sentiment_analyst",
                "news_analyst",
                "fundamentals_analyst",
                "bull_researcher",
            ],
        )

        resumed_calls = []
        resumed = self._graph(_role_nodes(resumed_calls)).run(interrupted.checkpoint)
        self.assertEqual(resumed["status"], "generated_unverified")
        self.assertEqual(resumed_calls[0], "bear_researcher")
        self.assertNotIn("market_analyst", resumed_calls)
        self.assertEqual(len(resumed["checkpoint"]["node_trace"]), 12)

    def test_missing_market_evidence_finishes_without_llm_or_direction(self):
        calls = []
        adapter = _DataAdapter(
            self.run_id,
            self.instrument.instrument_id,
            self.snapshot_id,
            market_available=False,
        )
        result = self._graph(_role_nodes(calls), adapter=adapter).run()

        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["recommendation"], "insufficient_evidence")
        self.assertEqual(result["section_count"], 0)
        self.assertFalse(calls)
        self.assertEqual(result["checkpoint"]["node_trace"], [])
        self.assertIn("未调用 LLM", self.connection.execute(
            "SELECT report_markdown FROM financial_final_reports WHERE id=?", (result["report_id"],)
        ).fetchone()[0])

    def test_checkpoint_scope_and_time_are_fail_closed(self):
        calls = []
        graph = self._graph(_role_nodes(calls))
        checkpoint = graph.initial_checkpoint()
        checkpoint["instrument_id"] += 1
        with self.assertRaises(StockResearchGraphError) as scope:
            graph.run(checkpoint)
        self.assertEqual(scope.exception.error_code, "incompatible_checkpoint")

        checkpoint = graph.initial_checkpoint()
        checkpoint["preflight"] = {"as_of": "2026-08-01T00:00:00.000Z"}
        with self.assertRaises(StockResearchGraphError) as time_error:
            graph.run(checkpoint)
        self.assertEqual(time_error.exception.error_code, "incompatible_checkpoint")

    def test_index_is_rejected_and_ss_alias_resolves_to_new_sh_equity(self):
        resolution = self.registry.resolve("600000.SS")
        self.assertEqual(resolution.status, "resolved")
        sh_equity = self.registry.get(resolution.instrument_id)
        self.assertEqual(sh_equity.canonical_symbol, "600000.SH")

        index = self.registry.get_by_canonical_symbol("000001.SH")
        run_id = "stock-index-wrong-graph"
        self._insert_run(run_id, index.instrument_id)
        snapshot_id = self._insert_evidence(run_id, index.instrument_id)
        adapter = _DataAdapter(run_id, index.instrument_id, snapshot_id)
        with self.assertRaises(StockResearchGraphError) as caught:
            self._graph(
                _role_nodes([]),
                adapter=adapter,
                run_id=run_id,
                instrument=index,
            )
        self.assertEqual(caught.exception.error_code, "unsupported_stock_graph_asset")


if __name__ == "__main__":
    unittest.main()
