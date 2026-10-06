import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
from financial_market_scheduler import (
    DEFAULT_PULSE_UNIVERSE,
    FIXED_HOME_INDEX_SCOPES,
    FinancialMarketJobService,
    FinancialMarketScheduler,
    STANDARD_MARKET_SCOPES,
)
from financial_worker_jobs import FINANCIAL_JOB_TYPES, FinancialJobDispatcher
from financial_worker_jobs import FinancialJobExecutionError
from financial_provider_contract import TemporarilyUnavailableError
from intel_database import IntelRepository
from intel_worker import IntelWorker
from sqlite_database import SQLiteDatabase


UTC = timezone.utc
ENABLED_SETTINGS = {
    "FINANCIAL_INTELLIGENCE_ENABLED": True,
    "TRADING_AGENTS_ENABLED": True,
    "FINANCIAL_AUTO_RESEARCH_ENABLED": True,
    "TRADING_SIMULATION_ENABLED": False,
    "FINANCIAL_QUOTE_FRESHNESS_SECONDS": 300,
}
SNAPSHOT_ONLY_SETTINGS = {
    **ENABLED_SETTINGS,
    "TRADING_AGENTS_ENABLED": False,
    "FINANCIAL_AUTO_RESEARCH_ENABLED": False,
}


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class _FixtureRouter:
    """Persist normalized index snapshots without network or provider SDKs."""

    def __init__(self, connection):
        self.connection = connection
        self.calls = []

    def fetch_and_persist(
        self,
        request,
        *,
        candidate_provider_ids,
        allow_fallback,
    ):
        self.calls.append(
            {
                "request": request,
                "candidate_provider_ids": tuple(candidate_provider_ids),
                "allow_fallback": bool(allow_fallback),
            }
        )
        provider_id = "market_scheduler_fixture"
        self.connection.execute(
            """
            INSERT INTO financial_provider_profiles(
                provider_key, display_name, provider_type, access_tier,
                capabilities_json, is_enabled
            ) VALUES(?, 'Market scheduler fixture', 'fixture', 'test', '[]', 1)
            ON CONFLICT(provider_key) DO UPDATE SET is_enabled=1
            """,
            (provider_id,),
        )
        profile_id = int(
            self.connection.execute(
                "SELECT id FROM financial_provider_profiles WHERE provider_key=?",
                (provider_id,),
            ).fetchone()[0]
        )
        payload = {
            "provider_id": provider_id,
            "endpoint": request.endpoint,
            "data_kind": request.data_kind.value,
            "metric": request.metric,
            "value": 100.0 + int(request.instrument_id),
            "normalized_payload": {
                "fixture": True,
                "instrument_id": request.instrument_id,
                "interval": request.parameters.get("interval", ""),
            },
        }
        payload_text = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
        snapshot_key = hashlib.sha256(
            f"{provider_id}|{request.request_id}|{request.data_kind.value}".encode(
                "utf-8"
            )
        ).hexdigest()
        observed_at = request.requested_as_of.astimezone(UTC).isoformat().replace(
            "+00:00", "Z"
        )
        self.connection.execute(
            """
            INSERT INTO financial_data_snapshots(
                snapshot_key, instrument_id, provider_profile_id, data_type,
                interval_code, observed_at, fetched_at, market_status,
                currency, timezone, quality_status, payload_json,
                payload_sha256, source_url, request_id
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 'open', '', 'UTC',
                     'normalized_fixture', ?, ?, 'fixture://market', ?)
            ON CONFLICT(snapshot_key) DO UPDATE SET
                fetched_at=excluded.fetched_at,
                payload_json=excluded.payload_json,
                payload_sha256=excluded.payload_sha256
            """,
            (
                snapshot_key,
                int(request.instrument_id),
                profile_id,
                request.data_kind.value,
                str(request.parameters.get("interval") or ""),
                observed_at,
                observed_at,
                payload_text,
                payload_hash,
                request.request_id,
            ),
        )
        snapshot_id = int(
            self.connection.execute(
                "SELECT id FROM financial_data_snapshots WHERE snapshot_key=?",
                (snapshot_key,),
            ).fetchone()[0]
        )
        response = SimpleNamespace(
            provider_id=provider_id,
            degradation=SimpleNamespace(degraded=False, reason=""),
        )
        return response, (snapshot_id,)


class _RetryableFixtureRouter:
    def fetch_and_persist(self, request, **_kwargs):
        raise TemporarilyUnavailableError(
            "fixture provider is temporarily unavailable",
            provider_id="yahoo",
            endpoint=request.endpoint,
            request_id=request.request_id,
        )


class FinancialMarketSchedulerTest(unittest.TestCase):
    def setUp(self):
        # IntelWorker.__init__ 在 config 的金融开关全关时会直接丢弃显式传入的
        # financial_dispatcher（生产启动优化）；本机 .env 默认就是全关，
        # 于是 worker 里没有任何金融 handler，作业全部停在排队状态。
        # 这里只打开"是否初始化金融模块"，具体开关仍由各用例的 settings 决定。
        for item in (
            patch.object(config, "FINANCIAL_INTELLIGENCE_ENABLED", True),
            patch.object(config, "TRADING_AGENTS_ENABLED", True),
            patch.object(config, "TRADING_SIMULATION_ENABLED", True),
            # conftest 的 DATABASE_TYPE=sqlite 会被 .env 覆盖（config 里仍是
            # postgres），SQLiteDatabase(path) 只改路径不改后端，本文件的
            # 快照/报告会真的写进共享主库。
            patch.object(config, "DATABASE_TYPE", "sqlite"),
            patch("db_connection.database_type", lambda: "sqlite"),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(
            str(Path(self.temp_dir.name) / "financial-market-scheduler.sqlite3")
        )
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.repository = IntelRepository(self.database)
        self.scheduler = FinancialMarketScheduler(
            self.repository,
            settings=ENABLED_SETTINGS,
        )

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _jobs(self, result, job_type=None):
        jobs = [self.repository.get_job(item["job_id"]) for item in result["jobs"]]
        return [job for job in jobs if not job_type or job["job_type"] == job_type]

    def _overview_payload(self, result, universe_key):
        for job in self._jobs(result, "market_overview"):
            if job["payload"]["universe_key"] == universe_key:
                return job["payload"]
        return None

    def test_exchange_specific_holidays_do_not_share_one_market_state(self):
        mainland_holiday = self.scheduler.enqueue_due_jobs(
            now=_at("2026-02-20T02:00:00Z"), trigger="startup"
        )
        cn = self._overview_payload(mainland_holiday, "CN_XSHG_MARKET")
        hk = self._overview_payload(mainland_holiday, "HK_MARKET")
        self.assertEqual(cn["phase"], "closed_latest")
        self.assertEqual(
            cn["market_sessions"]["XSHG"]["reason"], "exchange_holiday"
        )
        self.assertEqual(hk["phase"], "intraday")
        self.assertEqual(
            hk["market_sessions"]["XHKG"]["market_session_state"], "open"
        )

        hong_kong_holiday = self.scheduler.enqueue_due_jobs(
            now=_at("2026-04-07T02:00:00Z"), trigger="periodic"
        )
        cn = self._overview_payload(hong_kong_holiday, "CN_XSHG_MARKET")
        hk = self._overview_payload(hong_kong_holiday, "HK_MARKET")
        self.assertEqual(cn["phase"], "intraday")
        self.assertEqual(hk["phase"], "closed_latest")
        self.assertEqual(
            hk["market_sessions"]["XHKG"]["reason"], "exchange_holiday"
        )

    def test_lunch_weekend_and_post_close_delay_have_explicit_windows(self):
        lunch = self.scheduler.enqueue_due_jobs(
            now=_at("2026-07-31T03:45:00Z"), trigger="periodic"
        )
        self.assertEqual(
            self._overview_payload(lunch, "CN_XSHG_MARKET")["phase"], "lunch"
        )
        self.assertEqual(
            self._overview_payload(lunch, "HK_MARKET")["phase"], "intraday"
        )

        weekend = self.scheduler.enqueue_due_jobs(
            now=_at("2026-08-01T02:00:00Z"), trigger="periodic"
        )
        for scope in STANDARD_MARKET_SCOPES:
            self.assertEqual(
                self._overview_payload(weekend, scope.universe_key)["phase"],
                "closed_latest",
            )

        before_delay = self.scheduler.enqueue_due_jobs(
            now=_at("2026-07-31T07:03:00Z"), trigger="periodic"
        )
        self.assertIsNone(self._overview_payload(before_delay, "CN_XSHG_MARKET"))
        after_delay = self.scheduler.enqueue_due_jobs(
            now=_at("2026-07-31T07:06:00Z"), trigger="periodic"
        )
        self.assertEqual(
            self._overview_payload(after_delay, "CN_XSHG_MARKET")["phase"],
            "post_close",
        )

    def test_restart_catches_up_once_and_schedule_windows_are_permanent(self):
        now = _at("2026-07-31T02:00:00Z")
        first = self.scheduler.enqueue_due_jobs(now=now, trigger="startup")
        restarted = FinancialMarketScheduler(
            self.repository,
            settings=ENABLED_SETTINGS,
        ).enqueue_due_jobs(now=now, trigger="startup")
        self.assertGreater(first["created"], 0)
        self.assertEqual(restarted["created"], 0)
        self.assertEqual(restarted["existing"], len(restarted["jobs"]))
        self.assertEqual(first["full_research_jobs_created"], 0)
        rows = self.database.connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT dedupe_key) FROM intel_jobs"
        ).fetchone()
        self.assertEqual(int(rows[0]), int(rows[1]))
        self.assertEqual(
            int(
                self.database.connection.execute(
                    "SELECT COUNT(*) FROM intel_jobs WHERE job_type='financial_research'"
                ).fetchone()[0]
            ),
            0,
        )

    def test_event_refresh_requires_safe_id_and_dedupes_same_event(self):
        now = _at("2026-07-31T02:00:00Z")
        first = self.scheduler.enqueue_due_jobs(
            now=now, trigger="event", event_key="exchange-notice-42"
        )
        duplicate = self.scheduler.enqueue_due_jobs(
            now=now, trigger="event", event_key="exchange-notice-42"
        )
        another = self.scheduler.enqueue_due_jobs(
            now=now, trigger="event", event_key="exchange-notice-43"
        )
        self.assertGreater(first["created"], 0)
        self.assertEqual(duplicate["created"], 0)
        self.assertEqual(another["created"], first["created"])
        with self.assertRaises(ValueError):
            self.scheduler.enqueue_due_jobs(
                now=now, trigger="event", event_key="../unsafe event"
            )

    def test_existing_worker_persists_all_standard_overviews(self):
        now = _at("2026-07-31T02:00:00Z")
        scheduled = self.scheduler.enqueue_due_jobs(now=now, trigger="startup")
        router = _FixtureRouter(self.database.connection)
        service = FinancialMarketJobService(
            self.repository,
            settings=ENABLED_SETTINGS,
            router=router,
        )
        dispatcher = FinancialJobDispatcher(
            service.runners(), settings=ENABLED_SETTINGS
        )
        worker = IntelWorker(
            repository=self.repository,
            worker_id="financial-market-fixture-worker",
            financial_dispatcher=dispatcher,
            financial_market_scheduler=self.scheduler,
            heartbeat_seconds=0.01,
            job_lease_seconds=30,
        )
        worker.enqueue_due_periodic_jobs = lambda: None
        stats = worker.run_once(job_types=FINANCIAL_JOB_TYPES, limit=100)
        self.assertEqual(stats["claimed"], len(scheduled["jobs"]))
        self.assertEqual(stats["completed"], len(scheduled["jobs"]))
        self.assertEqual(len(router.calls), 10)

        snapshots = int(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM financial_data_snapshots"
            ).fetchone()[0]
        )
        self.assertEqual(snapshots, 10)
        report_rows = self.database.connection.execute(
            """
            SELECT u.universe_key, r.report_status, r.report_json
            FROM financial_final_reports r
            JOIN financial_research_runs run ON run.id=r.research_run_id
            JOIN financial_universes u ON u.id=run.universe_id
            ORDER BY u.universe_key
            """
        ).fetchall()
        self.assertEqual(len(report_rows), 5)
        self.assertEqual(
            {str(row[0]) for row in report_rows},
            {scope.universe_key for scope in STANDARD_MARKET_SCOPES}
            | {DEFAULT_PULSE_UNIVERSE},
        )
        coverage_by_universe = {}
        for row in report_rows:
            report = json.loads(row[2])
            self.assertEqual(report["report_type"], "lightweight_market_overview")
            coverage_by_universe[str(row[0])] = report["coverage"]
            self.assertIn("not an order instruction", report["boundary"])
        for universe_key in {"CN_XSHG_MARKET", "CN_XSHE_MARKET", "CN_A_MARKET"}:
            self.assertEqual(coverage_by_universe[universe_key], 1.0)
        self.assertAlmostEqual(coverage_by_universe["HK_MARKET"], 2 / 3)
        self.assertAlmostEqual(coverage_by_universe[DEFAULT_PULSE_UNIVERSE], 8 / 9)
        statuses = {str(row[0]): str(row[1]) for row in report_rows}
        self.assertEqual(statuses["CN_A_MARKET"], "market_overview")
        self.assertEqual(statuses["HK_MARKET"], "degraded_unverified")

    def test_five_home_indices_are_concrete_seeded_and_automatically_scheduled(self):
        self.assertEqual(
            [scope.canonical_symbol for scope in FIXED_HOME_INDEX_SCOPES],
            ["000001.SH", "399001.SZ", "HSI.HK", "IXIC.US", "N225.JP"],
        )
        for scope in FIXED_HOME_INDEX_SCOPES:
            instrument = self.scheduler.instruments.get_by_canonical_symbol(
                scope.canonical_symbol
            )
            self.assertIsNotNone(instrument, scope.canonical_symbol)
            self.assertEqual(instrument.asset_type, "index")
        result = self.scheduler.enqueue_due_jobs(
            now=_at("2026-07-31T02:00:00Z"), trigger="startup"
        )
        symbols = {
            job["payload"]["canonical_symbol"]
            for job in self._jobs(result, "financial_snapshot")
        }
        self.assertTrue(
            {scope.canonical_symbol for scope in FIXED_HOME_INDEX_SCOPES}.issubset(symbols)
        )
        fixed_jobs = {
            job["payload"]["canonical_symbol"]: job["payload"]
            for job in self._jobs(result, "financial_snapshot")
            if job["payload"]["canonical_symbol"]
            in {scope.canonical_symbol for scope in FIXED_HOME_INDEX_SCOPES}
            and job["payload"].get("snapshot_kind", "benchmark") == "benchmark"
        }
        self.assertEqual(len(fixed_jobs), 5)
        self.assertTrue(
            all(payload["provider_chain"] == ["yahoo"] for payload in fixed_jobs.values())
        )

    def test_snapshot_schedule_does_not_require_tradingagents_or_auto_research(self):
        scheduler = FinancialMarketScheduler(
            self.repository,
            settings=SNAPSHOT_ONLY_SETTINGS,
        )
        result = scheduler.enqueue_due_jobs(
            now=_at("2026-08-06T10:30:00Z"),
            trigger="periodic",
        )
        self.assertEqual(result["status"], "scheduled")
        self.assertGreater(result["created"], 0)
        jobs = self._jobs(result)
        self.assertTrue(any(job["job_type"] == "financial_snapshot" for job in jobs))
        self.assertTrue(any(job["job_type"] == "market_overview" for job in jobs))
        self.assertFalse(any(job["job_type"] == "financial_research" for job in jobs))

    def test_retryable_provider_failure_is_returned_to_worker_retry_queue(self):
        scheduled = self.scheduler.enqueue_due_jobs(
            now=_at("2026-08-06T10:30:00Z"),
            trigger="periodic",
        )
        job = next(
            job for job in self._jobs(scheduled, "financial_snapshot")
            if job["payload"].get("canonical_symbol") == "000001.SH"
            and not job["payload"].get("market_metric")
        )
        service = FinancialMarketJobService(
            self.repository,
            settings=ENABLED_SETTINGS,
            router=_RetryableFixtureRouter(),
        )
        context = SimpleNamespace(raise_if_cancelled=lambda: None)
        with self.assertRaises(FinancialJobExecutionError) as captured:
            service.run_snapshot(job["payload"], context)
        self.assertTrue(captured.exception.retryable)
        self.assertEqual(captured.exception.error_code, "temporarily_unavailable")

    def test_disabled_gate_creates_no_jobs(self):
        result = FinancialMarketScheduler(
            self.repository,
            settings={},
        ).enqueue_due_jobs(now=_at("2026-07-31T02:00:00Z"), trigger="startup")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["required_capability"], "financial_intelligence")
        self.assertEqual(result["created"], 0)


if __name__ == "__main__":
    unittest.main()
