import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from financial_instruments import InstrumentRegistry
from index_market_research_graph import (
    INDEX_CHECKPOINT_SCHEMA_VERSION,
    INDEX_GRAPH_VERSION,
    OUTPUT_CLASSIFICATION,
    IndexMarketResearchGraph,
    IndexMarketResearchGraphError,
    IndexMarketResearchGraphInterrupted,
)
from sqlite_database import SQLiteDatabase
from tradingagents_llm_adapter import TradingAgentsLLMAdapterError


NOW = datetime(2026, 7, 31, 2, 0, tzinfo=timezone.utc)


class _Broker:
    def runtime_identity(self):
        return {
            "model_id": "fixture-local-index-model",
            "provider_id": "local",
            "base_url": "http://fixture.invalid/v1",
            "runtime_source": "fixture",
        }


class _LLMFactory:
    def __init__(self, run_id):
        self.run_context = SimpleNamespace(research_run_id=run_id)
        self.broker = _Broker()


class _DataAdapter:
    def __init__(
        self,
        run_id,
        instrument,
        snapshot_id,
        *,
        now=NOW,
        market_available=True,
        composition_available=True,
    ):
        self.context = SimpleNamespace(
            research_run_id=run_id,
            instrument_id=instrument.instrument_id,
            server_now_utc=now,
            cutoff_at_utc=now,
        )
        self.instrument = instrument
        self.snapshot_id = snapshot_id
        self.market_available = market_available
        self.composition_available = composition_available
        self.calls = []

    def _value(self, tool, status, **extra):
        self.calls.append(tool)
        value = {
            "tool": tool,
            "status": status,
            "snapshot_id": self.snapshot_id,
            "observed_at": "2026-07-31T01:59:00Z",
        }
        value.update(extra)
        return json.dumps(value, ensure_ascii=False)

    def get_index_identity(self, symbol):
        del symbol
        return self._value(
            "get_index_identity",
            "complete",
            compiler=self.instrument.metadata["compiler"],
            official_url=self.instrument.metadata["official_url"],
            directly_tradeable=False,
        )

    def get_stock_data(self, symbol, start_date, end_date):
        del symbol, start_date, end_date
        return self._value(
            "get_stock_data", "fetched" if self.market_available else "unavailable"
        )

    def get_verified_market_snapshot(self, symbol, curr_date, look_back_days):
        del symbol, curr_date, look_back_days
        return self._value(
            "get_verified_market_snapshot",
            "completed" if self.market_available else "unavailable",
        )

    def get_market_breadth(self, symbol, curr_date):
        del symbol, curr_date
        return self._value("get_market_breadth", "complete")

    def get_index_constituents(self, symbol, curr_date):
        del symbol, curr_date
        return self._value(
            "get_index_constituents",
            "fetched" if self.composition_available else "unavailable",
            constituent_as_of=("2026-07-30" if self.composition_available else None),
            component_contribution={
                "status": "unavailable",
                "coverage": 0.0,
            },
        )

    def get_sector_rotation(self, symbol, curr_date, limit):
        del symbol, curr_date, limit
        return self._value(
            "get_sector_rotation",
            "fetched" if self.composition_available else "unavailable",
        )

    def get_market_liquidity(self, symbol, curr_date, look_back_days):
        del symbol, curr_date, look_back_days
        return self._value("get_market_liquidity", "completed")

    def get_news(self, symbol, start_date, end_date):
        del symbol, start_date, end_date
        return self._value("get_news", "complete")

    def get_macro_indicators(self, indicator, curr_date, look_back_days):
        del indicator, curr_date, look_back_days
        return self._value("get_macro_indicators", "fetched")


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
            argument = f"{speaker}: fixture index {role} argument with snapshot citation."
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
            argument = f"{speaker} Analyst: fixture index {role} argument."
            value["history"] += "\n" + argument
            value[history_key] += "\n" + argument
            value[current_key] = argument
            value["latest_speaker"] = speaker
            value["count"] += 1
            return {"risk_debate_state": value}

        return node

    return {
        "index_identity_analyst": record(
            "index_identity_analyst",
            {
                "index_identity_report": "Index identity and compiler report.",
                "fundamentals_report": "Index identity; no company statements.",
            },
        ),
        "index_technical_analyst": record(
            "index_technical_analyst",
            {
                "technical_report": "Index technical report cites snapshot.",
                "market_report": "Index technical report cites snapshot.",
            },
        ),
        "breadth_liquidity_analyst": record(
            "breadth_liquidity_analyst",
            {
                "breadth_liquidity_report": "Participation and liquidity proxy report.",
                "market_report": "Technical plus participation report.",
                "sentiment_report": "Participation is not social sentiment.",
            },
        ),
        "constituents_rotation_analyst": record(
            "constituents_rotation_analyst",
            {
                "constituents_rotation_report": "Composition coverage and sector rotation report.",
                "fundamentals_report": "Identity and composition; no company statements.",
                "sentiment_report": "Participation and sector rotation report.",
            },
        ),
        "macro_policy_analyst": record(
            "macro_policy_analyst",
            {
                "macro_policy_report": "Macro policy report with point-in-time boundary.",
                "news_report": "Macro policy report with point-in-time boundary.",
            },
        ),
        "bull_researcher": debate("bull_researcher", "Bull Analyst", "bull_history"),
        "bear_researcher": debate("bear_researcher", "Bear Analyst", "bear_history"),
        "research_manager": record(
            "research_manager",
            {
                "investment_plan": (
                    "**Recommendation**: Hold\n\n**Rationale**: balanced index evidence.\n\n"
                    "**Strategic Actions**: observe benchmark conditions."
                )
            },
        ),
        "market_strategy_analyst": record(
            "market_strategy_analyst",
            {
                "trader_investment_plan": (
                    "**Benchmark posture**: Neutral observation only; no index order."
                ),
                "sender": "Market Strategy Analyst",
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
                    "**Rating**: Hold\n\n**Executive Summary**: Maintain a neutral benchmark view; no order.\n\n"
                    "**Investment Thesis**: Evidence is bounded.\n\n**Time Horizon**: 1-3 months"
                )
            },
        ),
    }


class IndexMarketResearchGraphTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "index-graph.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.connection = self.database.connection
        self.registry = InstrumentRegistry(self.connection)
        self.registry.load_controlled_seed()

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _insert_run(self, run_id, instrument_id):
        self.connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, instrument_id, status, requested_at
            ) VALUES(?, 'test', 'instrument', ?, 'running', '2026-07-31T02:00:00.000Z')
            """,
            (run_id, instrument_id),
        )

    def _insert_evidence(self, run_id, instrument_id):
        self.connection.execute(
            """
            INSERT INTO financial_provider_profiles(
                provider_key, display_name, provider_type, access_tier,
                capabilities_json, is_enabled
            ) VALUES('index_graph_fixture', 'fixture', 'fixture', 'test', '["bar"]', 1)
            ON CONFLICT(provider_key) DO NOTHING
            """
        )
        provider_id = self.connection.execute(
            "SELECT id FROM financial_provider_profiles WHERE provider_key='index_graph_fixture'"
        ).fetchone()[0]
        payload = json.dumps({"normalized_payload": {"close": 3500.0}}, sort_keys=True)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        key = f"index-graph-fixture-{run_id}"
        self.connection.execute(
            """
            INSERT INTO financial_data_snapshots(
                snapshot_key, instrument_id, provider_profile_id, data_type,
                observed_at, fetched_at, market_status, payload_json, payload_sha256
            ) VALUES(?, ?, ?, 'quote', '2026-07-31T01:59:00Z',
                     '2026-07-31T02:00:00Z', 'open', ?, ?)
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
                     '2026-07-31T01:59:00Z', '{"tool_name":"get_verified_market_snapshot"}')
            """,
            (run_id, f"fixture:{run_id}", snapshot_id, instrument_id),
        )
        return int(snapshot_id)

    def _graph(
        self,
        symbol,
        nodes,
        *,
        run_id=None,
        now=NOW,
        market_available=True,
        composition_available=True,
        callback=None,
    ):
        instrument = self.registry.get_by_canonical_symbol(symbol)
        actual_run = run_id or f"index-{symbol.replace('.', '-')}"
        if self.connection.execute(
            "SELECT 1 FROM financial_research_runs WHERE id=?", (actual_run,)
        ).fetchone() is None:
            self._insert_run(actual_run, instrument.instrument_id)
            snapshot_id = self._insert_evidence(actual_run, instrument.instrument_id)
        else:
            snapshot_id = int(
                self.connection.execute(
                    "SELECT snapshot_id FROM financial_research_evidence WHERE research_run_id=? LIMIT 1",
                    (actual_run,),
                ).fetchone()[0]
            )
        adapter = _DataAdapter(
            actual_run,
            instrument,
            snapshot_id,
            now=now,
            market_available=market_available,
            composition_available=composition_available,
        )
        return IndexMarketResearchGraph(
            self.connection,
            actual_run,
            data_adapter=adapter,
            llm_factory=_LLMFactory(actual_run),
            nodes=nodes,
            checkpoint_callback=callback,
        )

    def test_sse_szse_and_hsi_use_index_graph_with_identity_session_latency_and_no_order(self):
        expected_compilers = {
            "000001.SH": "上海证券交易所",
            "399001.SZ": "深圳证券交易所",
            "HSI.HK": "恒生指数有限公司",
        }
        for symbol, compiler in expected_compilers.items():
            with self.subTest(symbol=symbol):
                calls = []
                result = self._graph(symbol, _role_nodes(calls)).run()
                self.assertEqual(result["status"], "generated_unverified")
                self.assertEqual(result["recommendation"], "Hold")
                self.assertEqual(result["output_classification"], OUTPUT_CLASSIFICATION)
                self.assertFalse(result["execution_allowed"])
                self.assertIsNone(result["order_target"])
                self.assertEqual(result["market_status"], "open")
                self.assertEqual(result["section_count"], 15)
                self.assertEqual(len(calls), 13)
                report = self.connection.execute(
                    "SELECT report_json, report_markdown FROM financial_final_reports WHERE id=?",
                    (result["report_id"],),
                ).fetchone()
                report_json = json.loads(report["report_json"])
                self.assertEqual(report_json["compiler"], compiler)
                self.assertEqual(report_json["constituent_as_of"], "2026-07-30")
                self.assertEqual(report_json["component_contribution_coverage"], 0.0)
                self.assertEqual(report_json["data_latency_seconds"], 60.0)
                self.assertFalse(report_json["execution_allowed"])
                self.assertIn("指数本身不生成订单", report["report_markdown"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0], 0)

    def test_intraday_lunch_close_and_holiday_market_states_are_server_clock_based(self):
        cases = (
            (datetime(2026, 7, 31, 2, 0, tzinfo=timezone.utc), "open"),
            (datetime(2026, 7, 31, 4, 0, tzinfo=timezone.utc), "lunch_break"),
            (datetime(2026, 7, 31, 8, 30, tzinfo=timezone.utc), "closed"),
            (datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc), "closed"),
        )
        for index, (now, expected) in enumerate(cases):
            graph = self._graph(
                "000001.SH",
                _role_nodes([]),
                run_id=f"clock-state-{index}",
                now=now,
            )
            preflight = graph._preflight()
            self.assertEqual(preflight["market_status"], expected)
            self.assertEqual(preflight["server_now_utc"], now.isoformat(timespec="milliseconds").replace("+00:00", "Z"))

    def test_missing_composition_degrades_coverage_but_does_not_substitute_company_data(self):
        calls = []
        result = self._graph(
            "HSI.HK",
            _role_nodes(calls),
            run_id="hsi-degraded-composition",
            composition_available=False,
        ).run()

        self.assertEqual(result["status"], "degraded_unverified")
        self.assertEqual(result["evidence_coverage"], 0.75)
        report = self.connection.execute(
            "SELECT report_json, report_markdown FROM financial_final_reports WHERE id=?",
            (result["report_id"],),
        ).fetchone()
        value = json.loads(report["report_json"])
        self.assertEqual(value["degraded_categories"], ["constituents", "sector_rotation"])
        self.assertNotIn("company cash flow", report["report_markdown"].lower())

    def test_missing_minimum_market_evidence_stops_before_llm(self):
        calls = []
        result = self._graph(
            "399001.SZ",
            _role_nodes(calls),
            run_id="szse-missing-market",
            market_available=False,
        ).run()
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["recommendation"], "insufficient_evidence")
        self.assertEqual(result["section_count"], 0)
        self.assertFalse(calls)

    def test_interruption_resume_and_checkpoint_scope_are_fail_closed(self):
        first_calls = []
        shared_failure = {"raised": False}
        graph = self._graph(
            "000001.SH",
            _role_nodes(first_calls, fail_role="bear_researcher", fail_once=shared_failure),
            run_id="index-resume",
        )
        with self.assertRaises(IndexMarketResearchGraphInterrupted) as caught:
            graph.run()
        interrupted = caught.exception
        self.assertEqual(interrupted.stage_key, "bear_researcher:1")
        self.assertEqual(interrupted.checkpoint["stage_index"], 6)
        self.assertEqual(interrupted.checkpoint["schema_version"], INDEX_CHECKPOINT_SCHEMA_VERSION)
        self.assertEqual(interrupted.checkpoint["graph_version"], INDEX_GRAPH_VERSION)

        resumed_calls = []
        resumed = self._graph(
            "000001.SH",
            _role_nodes(resumed_calls),
            run_id="index-resume",
        ).run(interrupted.checkpoint)
        self.assertEqual(resumed_calls[0], "bear_researcher")
        self.assertNotIn("index_identity_analyst", resumed_calls)

        invalid = resumed["checkpoint"]
        invalid["instrument_id"] += 1
        with self.assertRaises(IndexMarketResearchGraphError) as scope:
            self._graph(
                "000001.SH", _role_nodes([]), run_id="index-resume"
            ).run(invalid)
        self.assertEqual(scope.exception.error_code, "incompatible_checkpoint")

    def test_equity_is_rejected_by_index_graph(self):
        equity = self.registry.get_by_canonical_symbol("000001.SZ")
        self._insert_run("wrong-equity-index-graph", equity.instrument_id)
        snapshot_id = self._insert_evidence("wrong-equity-index-graph", equity.instrument_id)
        adapter = _DataAdapter("wrong-equity-index-graph", equity, snapshot_id)
        with self.assertRaises(IndexMarketResearchGraphError) as caught:
            IndexMarketResearchGraph(
                self.connection,
                "wrong-equity-index-graph",
                data_adapter=adapter,
                llm_factory=_LLMFactory("wrong-equity-index-graph"),
                nodes=_role_nodes([]),
            )
        self.assertEqual(caught.exception.error_code, "unsupported_index_graph_asset")


if __name__ == "__main__":
    unittest.main()
