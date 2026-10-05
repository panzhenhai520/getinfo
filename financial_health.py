#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Read-only financial health metrics and administrator alerts.

The service derives its view from existing SQLite audit/state tables and the
in-process resource controllers.  It stores no prompt, question, answer,
session, credential or token value and introduces no monitoring service.
"""

from __future__ import annotations

import math
from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Mapping

import config
from financial_config import financial_capabilities
from financial_latest_observability import FinancialLatestObservabilityService
from financial_resource_isolation import (
    artifact_io_controller,
    provider_admission_controller,
)
from financial_security import redact_public_payload


UTC = timezone.utc
FINANCIAL_HEALTH_VERSION = "financial-health-v1"
FINANCIAL_JOB_TYPES = (
    "financial_snapshot",
    "financial_research",
    "financial_verify",
    "market_overview",
    "paper_backtest",
)
UNRESOLVED_VERDICTS = frozenset(
    {"unresolved_conflict", "insufficient_evidence", "requires_human_review"}
)


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("financial health clock must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _age_seconds(value, now: datetime) -> int | None:
    parsed = _parse_time(value)
    return max(0, int((now - parsed).total_seconds())) if parsed else None


def _p95(values) -> int | None:
    samples = sorted(max(0, int(value)) for value in values if value is not None)
    if not samples:
        return None
    return samples[max(0, math.ceil(len(samples) * 0.95) - 1)]


def _setting(settings, key: str, default):
    if isinstance(settings, Mapping):
        return settings.get(key, default)
    return getattr(settings, key, default)


class FinancialHealthService:
    def __init__(self, database, *, settings=None, clock=None):
        self.database = database
        self.settings = config if settings is None else settings
        self.clock = clock or (lambda: datetime.now(UTC))

    @property
    def connection(self):
        ensure = getattr(self.database, "_ensure_connection", None)
        if callable(ensure):
            ensure()
        return getattr(self.database, "connection", self.database)

    def _lock(self):
        lock = getattr(self.database, "lock", None)
        return lock if lock is not None else nullcontext()

    @staticmethod
    def _alert(
        code: str,
        severity: str,
        component: str,
        message: str,
        *,
        observed=None,
        threshold=None,
        target_ids=None,
    ) -> dict:
        return {
            "code": str(code),
            "severity": str(severity),
            "component": str(component),
            "message": str(message),
            "observed": observed,
            "threshold": threshold,
            "target_ids": list(target_ids or [])[:50],
        }

    def build(self) -> dict:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("financial health clock must be timezone-aware")
        now = now.astimezone(UTC)
        now_text = _utc_text(now)
        day_start = _utc_text(now - timedelta(hours=24))
        alerts = []
        capability_state = financial_capabilities(self.settings)
        rollout = capability_state["rollout"]
        rss_enabled = bool(rollout["capabilities"]["rss"])
        financial_enabled = bool(
            capability_state["effective"]["financial_intelligence"]
        )
        research_enabled = bool(rollout["capabilities"]["stock_research"])
        history_review_enabled = bool(
            rollout["capabilities"]["history_review"]
        )
        placeholders = ",".join("?" for _ in FINANCIAL_JOB_TYPES)

        with self._lock():
            providers = [
                dict(row)
                for row in self.connection.execute(
                    """SELECT provider_key,is_enabled,health_status,last_health_check_at
                       FROM financial_provider_profiles ORDER BY provider_key"""
                ).fetchall()
            ]
            sources = [
                dict(row)
                for row in self.connection.execute(
                    """SELECT source.id,source.source_type,source.is_enabled,
                              source.polling_interval_minutes,source.last_scan_at,
                              source.last_successful_scan_at,source.last_scan_status,
                              source.consecutive_scan_failures
                       FROM intel_sources source
                       JOIN intel_source_industries industry ON industry.source_id=source.id
                       WHERE industry.industry_pack_id='financial_markets'
                       ORDER BY source.id"""
                ).fetchall()
            ]
            snapshot_row = self.connection.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN stale_after IS NOT NULL AND datetime(stale_after)<=datetime(?) THEN 1 ELSE 0 END) AS stale,
                          MAX(fetched_at) AS latest_fetched_at
                   FROM financial_data_snapshots""",
                (now_text,),
            ).fetchone()
            market_scheduler_row = self.connection.execute(
                """SELECT COUNT(*) AS total, MAX(created_at) AS latest_created_at,
                          (SELECT id FROM intel_jobs
                           WHERE created_by='financial_market_scheduler'
                           ORDER BY datetime(created_at) DESC, id DESC LIMIT 1) AS latest_id
                   FROM intel_jobs
                   WHERE created_by='financial_market_scheduler'"""
            ).fetchone()
            quality_rows = self.connection.execute(
                "SELECT quality_status,COUNT(*) FROM financial_data_snapshots GROUP BY quality_status"
            ).fetchall()
            stale_snapshot_ids = [
                int(row[0]) for row in self.connection.execute(
                    """SELECT id FROM financial_data_snapshots
                       WHERE stale_after IS NOT NULL AND datetime(stale_after)<=datetime(?)
                       ORDER BY stale_after LIMIT 50""",
                    (now_text,),
                ).fetchall()
            ]
            market_rows = self.connection.execute(
                """SELECT COALESCE(NULLIF(instrument.market,''),NULLIF(universe.market,''),'unknown') AS market,
                          COUNT(*)
                   FROM financial_data_snapshots snapshot
                   LEFT JOIN financial_instruments instrument ON instrument.id=snapshot.instrument_id
                   LEFT JOIN financial_universes universe ON universe.id=snapshot.universe_id
                   GROUP BY 1"""
            ).fetchall()
            job_rows = [
                dict(row)
                for row in self.connection.execute(
                    f"""SELECT id,job_type,status,created_at,updated_at,lease_expires_at
                         FROM intel_jobs WHERE job_type IN ({placeholders})
                           AND (status IN ('queued','running','retry_wait')
                                OR datetime(updated_at)>=datetime(?))""",
                    (*FINANCIAL_JOB_TYPES, day_start),
                ).fetchall()
            ]
            llm_rows = [
                dict(row)
                for row in self.connection.execute(
                    """SELECT id,status,error_code,latency_ms,input_tokens,output_tokens
                       FROM llm_call_audit WHERE datetime(started_at)>=datetime(?)""",
                    (day_start,),
                ).fetchall()
            ]
            report_row = self.connection.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN lower(report_status) NOT IN ('','draft','failed','cancelled') THEN 1 ELSE 0 END) AS completed,
                          MAX(CASE WHEN lower(report_status) NOT IN ('','draft','failed','cancelled')
                              THEN COALESCE(fetched_at,updated_at,created_at) END) AS latest_success
                   FROM financial_final_reports"""
            ).fetchone()
            report_scope_rows = self.connection.execute(
                """SELECT CASE
                              WHEN run.scope_type IN ('universe','market') THEN 'universe'
                              ELSE COALESCE(NULLIF(instrument.asset_type,''),run.scope_type,'unknown')
                           END AS report_scope,
                           COUNT(*)
                   FROM financial_final_reports report
                   JOIN financial_research_runs run ON run.id=report.research_run_id
                   LEFT JOIN financial_instruments instrument ON instrument.id=run.instrument_id
                   WHERE lower(report.report_status) NOT IN ('','draft','failed','cancelled')
                   GROUP BY 1"""
            ).fetchall()
            latest_report_row = self.connection.execute(
                """SELECT id,COALESCE(fetched_at,updated_at,created_at) AS success_at
                   FROM financial_final_reports
                   WHERE lower(report_status) NOT IN ('','draft','failed','cancelled')
                   ORDER BY datetime(success_at) DESC LIMIT 1"""
            ).fetchone()
            failed_run_ids = [
                str(row[0]) for row in self.connection.execute(
                    """SELECT id FROM financial_research_runs
                       WHERE status='failed' AND datetime(updated_at)>=datetime(?)
                       ORDER BY updated_at DESC LIMIT 50""",
                    (day_start,),
                ).fetchall()
            ]
            completed_without_report_ids = [
                str(row[0]) for row in self.connection.execute(
                    """SELECT run.id FROM financial_research_runs run
                       WHERE run.status='completed' AND NOT EXISTS(
                           SELECT 1 FROM financial_final_reports report
                           WHERE report.research_run_id=run.id
                       ) ORDER BY run.updated_at DESC LIMIT 50"""
                ).fetchall()
            ]
            verdict_rows = self.connection.execute(
                """SELECT verdict,COUNT(*) FROM financial_verdicts verdict
                   WHERE verdict.id IN (
                       SELECT MAX(id) FROM financial_verdicts GROUP BY claim_id
                   ) GROUP BY verdict"""
            ).fetchall()
            unresolved_claim_ids = [
                int(row[0]) for row in self.connection.execute(
                    """SELECT verdict.claim_id FROM financial_verdicts verdict
                       WHERE verdict.id IN (
                           SELECT MAX(id) FROM financial_verdicts GROUP BY claim_id
                       ) AND verdict.verdict IN (?,?,?)
                       ORDER BY verdict.decided_at DESC LIMIT 50""",
                    tuple(sorted(UNRESOLVED_VERDICTS)),
                ).fetchall()
            ]
            usage_rows = self.connection.execute(
                # usage_date 在 SQLite 与 PG 里都是 TEXT（'YYYY-MM-DD'）。
                # 原先写 date(?)：SQLite 下 date() 返回字符串，能比；
                # PG 下 date() 返回 DATE，与 text 列比较直接报
                # operator does not exist: text = date ——
                # 这就是 /api/intel/financial/feed 在生产 PG 上 500 的真因。
                # 改成截取日期字符串做纯文本比较，两种后端都成立。
                "SELECT service,usage_count FROM intel_api_usage WHERE usage_date=?",
                (str(now_text or "")[:10],),
            ).fetchall()
            research_budget_row = self.connection.execute(
                """SELECT COUNT(*) AS runs,
                          COALESCE(SUM(llm_call_budget),0) AS calls,
                          COALESCE(SUM(token_budget),0) AS tokens
                   FROM financial_research_runs WHERE datetime(requested_at)>=datetime(?)""",
                (day_start,),
            ).fetchone()

        enabled_providers = [item for item in providers if int(item["is_enabled"] or 0)]
        provider_items = []
        for item in providers:
            status = str(item["health_status"] or "unknown").casefold()
            age = _age_seconds(item["last_health_check_at"], now)
            provider_items.append(
                {
                    "provider_id": str(item["provider_key"]),
                    "enabled": bool(item["is_enabled"]),
                    "health_status": status,
                    "health_check_age_seconds": age,
                }
            )
            if not item["is_enabled"] or not financial_enabled:
                continue
            if status in {"permission_denied", "unauthorized", "forbidden"}:
                alerts.append(self._alert(
                    "provider_permission_denied", "critical", "provider",
                    "已启用 Provider 缺少调用权限。", observed=status,
                    target_ids=[str(item["provider_key"])],
                ))
            elif status in {"failed", "unhealthy", "rate_limited", "unavailable"}:
                alerts.append(self._alert(
                    "provider_unhealthy", "warning", "provider",
                    "已启用 Provider 当前不可健康使用。", observed=status,
                    target_ids=[str(item["provider_key"])],
                ))
            max_age = int(_setting(
                self.settings, "FINANCIAL_HEALTH_PROVIDER_MAX_AGE_SECONDS", 86400
            ))
            if age is None or age > max_age:
                alerts.append(self._alert(
                    "provider_health_check_stale", "warning", "provider",
                    "已启用 Provider 的健康检查已过期或缺失。",
                    observed=age, threshold=max_age,
                    target_ids=[str(item["provider_key"])],
                ))

        enabled_sources = [item for item in sources if int(item["is_enabled"] or 0)]
        source_items = []
        source_base_age = int(_setting(
            self.settings, "FINANCIAL_HEALTH_SOURCE_MAX_AGE_SECONDS", 86400
        ))
        for item in sources:
            status = str(item["last_scan_status"] or "unknown").casefold()
            age = _age_seconds(item["last_successful_scan_at"], now)
            scan_age = _age_seconds(item["last_scan_at"], now)
            threshold = max(
                source_base_age,
                max(5, int(item["polling_interval_minutes"] or 1440)) * 120,
            )
            source_items.append({
                "source_id": int(item["id"]),
                "source_type": str(item["source_type"] or "unknown"),
                "enabled": bool(item["is_enabled"]),
                "last_scan_status": status,
                "last_scan_age_seconds": scan_age,
                "last_success_age_seconds": age,
                "consecutive_failures": int(item["consecutive_scan_failures"] or 0),
            })
            if not item["is_enabled"] or not rss_enabled:
                continue
            if status in {"failed", "rate_limited"} or int(item["consecutive_scan_failures"] or 0):
                alerts.append(self._alert(
                    "financial_source_scan_failed", "warning", "source",
                    "金融资讯源最近扫描失败或受限。",
                    observed=max(1, int(item["consecutive_scan_failures"] or 0)),
                    threshold=0,
                    target_ids=[int(item["id"])],
                ))
            if age is None or age > threshold:
                alerts.append(self._alert(
                    "financial_source_stale", "warning", "source",
                    "金融资讯源缺少近期成功扫描。", observed=age, threshold=threshold,
                    target_ids=[int(item["id"])],
                ))

        snapshot_total = int(snapshot_row[0] or 0)
        snapshot_stale = int(snapshot_row[1] or 0)
        latest_snapshot_age = _age_seconds(snapshot_row[2], now)
        market_scheduler_jobs = int(market_scheduler_row[0] or 0)
        latest_market_scheduler_age = _age_seconds(market_scheduler_row[1], now)
        market_scheduler_max_age = int(_setting(
            self.settings,
            "FINANCIAL_HEALTH_MARKET_SCHEDULER_MAX_AGE_SECONDS",
            345600,
        ))
        if financial_enabled and not enabled_providers:
            alerts.append(self._alert(
                "no_enabled_financial_provider", "warning", "provider",
                "金融能力已开启，但没有已启用 Provider。", observed=0, threshold=1,
            ))
        if financial_enabled and snapshot_total == 0:
            alerts.append(self._alert(
                "financial_snapshot_missing", "warning", "snapshot",
                "金融能力已开启，但尚无结构化快照。", observed=0, threshold=1,
            ))
        elif financial_enabled and snapshot_stale:
            alerts.append(self._alert(
                "financial_snapshot_stale", "warning", "snapshot",
                "存在超过 stale_after 的金融快照。",
                observed=snapshot_stale, threshold=0, target_ids=stale_snapshot_ids,
            ))
        if (
            financial_enabled
            and (snapshot_total == 0 or snapshot_stale > 0)
            and (
                market_scheduler_jobs == 0
                or latest_market_scheduler_age is None
                or latest_market_scheduler_age > market_scheduler_max_age
            )
        ):
            alerts.append(self._alert(
                "financial_market_scheduler_inactive",
                "warning",
                "scheduler",
                "行情快照已缺失或过期，但自动行情调度近期没有创建任务。",
                observed=latest_market_scheduler_age,
                threshold=market_scheduler_max_age,
                target_ids=[int(market_scheduler_row[2])]
                if market_scheduler_row[2] is not None else [],
            ))

        allowed_job_types = set()
        if financial_enabled:
            allowed_job_types.update({"financial_snapshot", "market_overview"})
        if research_enabled:
            allowed_job_types.update({"financial_research", "financial_verify"})
        if rollout["capabilities"]["simulation_backtest"]:
            allowed_job_types.add("paper_backtest")
        job_rows = [
            item for item in job_rows if str(item["job_type"]) in allowed_job_types
        ]

        status_counts = Counter(str(item["status"] or "unknown") for item in job_rows)
        queued_ages = [
            _age_seconds(item["created_at"], now)
            for item in job_rows if item["status"] in {"queued", "retry_wait"}
        ]
        running_ages = [
            _age_seconds(item["updated_at"], now)
            for item in job_rows if item["status"] == "running"
        ]
        oldest_queued = max((value for value in queued_ages if value is not None), default=None)
        oldest_running = max((value for value in running_ages if value is not None), default=None)
        expired_leases = sum(
            1 for item in job_rows
            if item["status"] == "running"
            and _parse_time(item["lease_expires_at"])
            and _parse_time(item["lease_expires_at"]) <= now
        )
        expired_job_ids = [
            int(item["id"]) for item in job_rows
            if item["status"] == "running"
            and _parse_time(item["lease_expires_at"])
            and _parse_time(item["lease_expires_at"]) <= now
        ][:50]
        job_max_age = int(_setting(
            self.settings, "FINANCIAL_HEALTH_JOB_MAX_AGE_SECONDS", 900
        ))
        stale_queued_job_ids = [
            int(item["id"]) for item in job_rows
            if item["status"] in {"queued", "retry_wait"}
            and (_age_seconds(item["created_at"], now) or 0) > job_max_age
        ][:50]
        if expired_leases:
            alerts.append(self._alert(
                "financial_worker_lease_expired", "critical", "worker",
                "存在租约已过期的运行中金融任务，worker 可能停止。",
                observed=expired_leases, threshold=0, target_ids=expired_job_ids,
            ))
        if oldest_queued is not None and oldest_queued > job_max_age:
            alerts.append(self._alert(
                "financial_job_backlog_stale", "warning", "worker",
                "金融任务排队时间超过健康阈值。",
                observed=oldest_queued, threshold=job_max_age,
                target_ids=stale_queued_job_ids,
            ))

        llm_failed = [item for item in llm_rows if item["status"] != "completed"]
        llm_timeouts = sum(
            1 for item in llm_rows if "timeout" in str(item["error_code"] or "").casefold()
        )
        timeout_audit_ids = [
            int(item["id"]) for item in llm_rows
            if "timeout" in str(item["error_code"] or "").casefold()
        ][:50]
        llm_p95 = _p95(item["latency_ms"] for item in llm_rows)
        llm_limit = int(_setting(self.settings, "FINANCIAL_HEALTH_LLM_P95_MS", 90000))
        if research_enabled and llm_timeouts:
            alerts.append(self._alert(
                "llm_timeout_failures", "warning", "llm",
                "最近 24 小时存在 LLM 超时。", observed=llm_timeouts, threshold=0,
                target_ids=timeout_audit_ids,
            ))
        if research_enabled and llm_p95 is not None and llm_p95 > llm_limit:
            alerts.append(self._alert(
                "llm_latency_p95_high", "warning", "llm",
                "LLM p95 延迟超过阈值。", observed=llm_p95, threshold=llm_limit,
                target_ids=[
                    int(item["id"]) for item in llm_rows
                    if int(item["latency_ms"] or 0) > llm_limit
                ][:50],
            ))

        latest_report_age = _age_seconds(report_row[2], now)
        failed_runs = len(failed_run_ids)
        completed_without_report = len(completed_without_report_ids)
        if research_enabled and failed_runs:
            alerts.append(self._alert(
                "financial_research_failed", "warning", "report",
                "最近 24 小时存在失败的金融研究。", observed=failed_runs, threshold=0,
                target_ids=failed_run_ids,
            ))
        if research_enabled and completed_without_report:
            alerts.append(self._alert(
                "completed_research_missing_report", "critical", "report",
                "存在已完成研究但缺少终极报告。",
                observed=completed_without_report, threshold=0,
                target_ids=completed_without_report_ids,
            ))
        report_max_age = int(_setting(
            self.settings, "FINANCIAL_HEALTH_REPORT_MAX_AGE_SECONDS", 86400
        ))
        if (
            research_enabled
            and latest_report_age is not None
            and latest_report_age > report_max_age
        ):
            alerts.append(self._alert(
                "financial_report_stale", "warning", "report",
                "最近成功报告已超过新鲜度阈值。",
                observed=latest_report_age, threshold=report_max_age,
                target_ids=[int(latest_report_row[0])] if latest_report_row else [],
            ))

        verdict_counts = {str(row[0]): int(row[1]) for row in verdict_rows}
        unresolved = sum(verdict_counts.get(value, 0) for value in UNRESOLVED_VERDICTS)
        if history_review_enabled and unresolved:
            alerts.append(self._alert(
                "verification_conflicts_pending", "warning", "verification",
                "存在尚未解决的金融事实冲突。", observed=unresolved, threshold=0,
                target_ids=unresolved_claim_ids,
            ))

        usage = {str(row[0]): int(row[1]) for row in usage_rows}
        provider_usage = sum(
            count for service, count in usage.items()
            if service.casefold() in {
                str(item["provider_id"]).casefold() for item in provider_items
            }
        )
        provider_usage_items = {
            service: count for service, count in sorted(usage.items())
            if service.casefold() in {
                str(item["provider_id"]).casefold() for item in provider_items
            }
        }
        provider_budget = int(_setting(
            self.settings, "FINANCIAL_PROVIDER_DAILY_CALL_BUDGET", 1000
        ))
        if (
            financial_enabled
            and provider_budget
            and provider_usage >= math.ceil(provider_budget * 0.9)
        ):
            alerts.append(self._alert(
                "provider_daily_budget_near_limit", "warning", "budget",
                "Provider 每日调用预算已接近上限。",
                observed=provider_usage, threshold=provider_budget,
                target_ids=list(provider_usage_items),
            ))

        alerts.sort(key=lambda item: (
            {"critical": 0, "warning": 1, "info": 2}.get(item["severity"], 9),
            item["code"],
        ))
        status = (
            "critical" if any(item["severity"] == "critical" for item in alerts)
            else "degraded" if alerts
            else "healthy"
        )
        metrics = {
            "providers": {
                "configured": len(providers),
                "enabled": len(enabled_providers),
                "items": provider_items,
            },
            "sources": {
                "configured": len(sources),
                "enabled": len(enabled_sources),
                "items": source_items,
            },
            "snapshots": {
                "total": snapshot_total,
                "stale": snapshot_stale,
                "latest_age_seconds": latest_snapshot_age,
                "quality_counts": {str(row[0]): int(row[1]) for row in quality_rows},
                "market_coverage": {str(row[0]): int(row[1]) for row in market_rows},
            },
            "jobs": {
                "status_counts": dict(sorted(status_counts.items())),
                "market_scheduler_jobs": market_scheduler_jobs,
                "latest_market_scheduler_job_age_seconds": latest_market_scheduler_age,
                "oldest_queued_age_seconds": oldest_queued,
                "oldest_running_age_seconds": oldest_running,
                "expired_running_leases": expired_leases,
                "expired_job_ids": expired_job_ids,
                "stale_queued_job_ids": stale_queued_job_ids,
            },
            "llm": {
                "calls_24h": len(llm_rows),
                "failures_24h": len(llm_failed),
                "timeouts_24h": llm_timeouts,
                "timeout_audit_ids": timeout_audit_ids,
                "latency_p95_ms": llm_p95,
                "input_tokens_24h": sum(int(item["input_tokens"] or 0) for item in llm_rows),
                "output_tokens_24h": sum(int(item["output_tokens"] or 0) for item in llm_rows),
            },
            "reports": {
                "total": int(report_row[0] or 0),
                "successful": int(report_row[1] or 0),
                "latest_success_age_seconds": latest_report_age,
                "failed_research_24h": failed_runs,
                "failed_research_run_ids": failed_run_ids,
                "completed_without_report": completed_without_report,
                "completed_without_report_run_ids": completed_without_report_ids,
                "scope_coverage": {
                    str(row[0]): int(row[1]) for row in report_scope_rows
                },
            },
            "verification": {
                "verdict_counts": dict(sorted(verdict_counts.items())),
                "pending_conflicts": unresolved,
                "pending_conflict_claim_ids": unresolved_claim_ids,
            },
            "budgets": {
                "provider_calls_today": provider_usage,
                "provider_daily_limit": provider_budget,
                "provider_usage_today": provider_usage_items,
                "research_runs_24h": int(research_budget_row[0] or 0),
                "configured_llm_call_budget_24h": int(research_budget_row[1] or 0),
                "configured_token_budget_24h": int(research_budget_row[2] or 0),
            },
            "resource_isolation": {
                "provider_process": provider_admission_controller.snapshot(),
                "artifact_io_process": artifact_io_controller.snapshot(),
            },
            "rollout": rollout,
            "latest_information": FinancialLatestObservabilityService(
                self.database, clock=lambda: now
            ).snapshot(),
        }
        return redact_public_payload({
            "health_version": FINANCIAL_HEALTH_VERSION,
            "status": status,
            "checked_at": now_text,
            "metrics": metrics,
            "alerts": alerts,
            "alert_count": len(alerts),
        })

    @staticmethod
    def public_availability(health: Mapping[str, object]) -> dict:
        alerts = health.get("alerts") if isinstance(health, Mapping) else []
        codes = {
            str(item.get("code") or "")
            for item in (alerts or []) if isinstance(item, Mapping)
        }
        priority = (
            ("financial_worker_lease_expired", "金融后台任务暂不可用，正在等待 worker 恢复。"),
            ("provider_permission_denied", "金融数据源权限不足，请联系管理员检查授权。"),
            ("no_enabled_financial_provider", "尚未启用可用的金融数据源。"),
            ("provider_unhealthy", "金融行情 Provider 当前不可用。"),
            ("completed_research_missing_report", "金融研究已完成，但终极报告尚不可用。"),
            ("financial_snapshot_missing", "尚无可展示的结构化行情快照。"),
            ("financial_market_scheduler_inactive", "自动行情调度近期没有产生刷新任务。"),
            ("financial_snapshot_stale", "现有行情快照已过期，后台正在等待刷新。"),
            ("financial_source_scan_failed", "金融资讯源最近刷新失败。"),
            ("financial_source_stale", "金融资讯源较长时间未成功刷新。"),
            ("financial_job_backlog_stale", "金融刷新任务排队时间较长。"),
            ("llm_timeout_failures", "金融研究模型最近发生超时。"),
            ("financial_research_failed", "最近一次金融研究未成功完成。"),
            ("financial_report_stale", "已有金融研究报告超过新鲜度阈值。"),
            ("verification_conflicts_pending", "部分金融事实存在待复核冲突。"),
        )
        status = str(health.get("status") or "degraded")
        fallback = "金融数据链路正常。" if status == "healthy" else "金融数据链路部分降级，请稍后重试。"
        message = next((text for code, text in priority if code in codes), fallback)
        return {
            "status": "unavailable" if status == "critical" else status,
            "reason_codes": sorted(codes),
            "message": message,
            "checked_at": str(health.get("checked_at") or ""),
        }


__all__ = ["FINANCIAL_HEALTH_VERSION", "FinancialHealthService"]
