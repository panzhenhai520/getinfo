#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Measure repeatable local and live performance baselines without storing secrets."""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import statistics
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * float(percent)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def summarize_samples(samples: list[dict], *, scope: str) -> dict:
    elapsed = [float(item["elapsed_ms"]) for item in samples]
    return {
        "scope": scope,
        "sample_count": len(samples),
        "success_count": sum(bool(item.get("success")) for item in samples),
        "samples_ms": [round(value, 3) for value in elapsed],
        "median_ms": round(statistics.median(elapsed), 3) if elapsed else 0.0,
        "p95_ms": round(percentile(elapsed, 0.95), 3) if elapsed else 0.0,
        "min_ms": round(min(elapsed), 3) if elapsed else 0.0,
        "max_ms": round(max(elapsed), 3) if elapsed else 0.0,
        "error_types": [
            str(item.get("error_type"))
            for item in samples
            if not item.get("success") and item.get("error_type")
        ],
    }


def parse_sse_event_line(line: str) -> dict | None:
    if not str(line).startswith("data:"):
        return None
    payload = str(line)[5:].strip()
    if not payload:
        return None
    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def _timed(callable_) -> dict:
    started = time.perf_counter()
    try:
        result = callable_()
        return {
            "elapsed_ms": (time.perf_counter() - started) * 1000,
            "success": result is not False,
        }
    except Exception as exc:
        return {
            "elapsed_ms": (time.perf_counter() - started) * 1000,
            "success": False,
            "error_type": type(exc).__name__,
        }


class _StaticHTTP:
    def __init__(self, result):
        self.result = result

    def get(self, *_args, **_kwargs):
        return self.result


def _local_rss_metric(repeats: int) -> dict:
    from intel_http import HTTPFetchResult
    from intel_light_scanner import RSSScanner

    items = "".join(
        f"<item><title>SFC market notice {index}</title>"
        f"<link>https://example.invalid/notice/{index}</link>"
        f"<description>Capital market regulatory notice {index}</description>"
        f"<pubDate>Fri, 31 Jul 2026 08:{index:02d}:00 +0800</pubDate></item>"
        for index in range(20)
    )
    content = f"<?xml version='1.0'?><rss><channel>{items}</channel></rss>".encode()
    result = HTTPFetchResult(
        "https://example.invalid/feed.xml",
        200,
        content,
        "application/rss+xml",
        "utf-8",
    )
    scanner = RSSScanner(_StaticHTTP(result))
    samples = []
    for _index in range(repeats):
        samples.append(
            _timed(
                lambda: len(
                    scanner.scan(
                        {"source_url": "https://example.invalid/feed.xml"},
                        limit=20,
                    )
                )
                == 20
            )
        )
    return summarize_samples(samples, scope="local_deterministic")


def _local_application_metrics(repeats: int) -> dict:
    import config
    import intel_api
    from flask import Flask
    from industry_packs import IndustryPackLoader
    from intel_api import intel_bp
    from intel_classifier import IntelClassificationService
    from intel_database import IntelRepository
    from intel_worker import IntelWorker
    from mapindex_api import _clean_chat_history_rows
    from sqlite_database import SQLiteDatabase

    original_llm = config.INTEL_LLM_ENABLED
    original_keyword_guard = config.CRAWL_REQUIRE_KEYWORD_MATCH
    config.INTEL_LLM_ENABLED = False
    config.CRAWL_REQUIRE_KEYWORD_MATCH = False
    metrics = {}
    with tempfile.TemporaryDirectory(prefix="stage1-performance-") as temp_dir:
        database = SQLiteDatabase(str(Path(temp_dir) / "performance.sqlite3"))
        if not database.connect() or not database.create_tables():
            raise RuntimeError("temporary database setup failed")
        database.analyze_article_spacetime_profile = lambda _article_id: None
        repository = IntelRepository(database)
        loader = IndustryPackLoader()
        service = IntelClassificationService(repository, loader)
        article_ids = []
        for index in range(max(repeats, 12)):
            article_ids.append(
                database.insert_article(
                    {
                        "url": f"https://example.invalid/performance/{index}",
                        "title": f"香港证监会发布资本市场监管公告 {index}",
                        "content": (
                            "SFC capital market regulatory framework and market trading update. "
                            * 20
                        )
                        + f" Unique benchmark sequence {index}.",
                        "publish_date": "2026-07-31",
                        "matched_keywords": ["SFC", "capital market"],
                    }
                )
            )
        worker = IntelWorker(
            repository=repository,
            classification_service=service,
            worker_id="performance-baseline",
        )
        worker_samples = [
            _timed(
                lambda: worker.run_once(job_types=["classification"], limit=1).get(
                    "completed"
                )
                == 1
            )
            for _index in range(repeats)
        ]
        metrics["classification_worker_job"] = summarize_samples(
            worker_samples, scope="local_deterministic"
        )

        app = Flask("stage1-performance-dashboard")
        app.register_blueprint(intel_bp)
        with patch.object(intel_api, "intel_repository", repository), patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": 1, "role": "admin"},
        ):
            client = app.test_client()
            dashboard_samples = []
            for _index in range(repeats):
                dashboard_samples.append(
                    _timed(
                        lambda: client.get(
                            "/api/intel/dashboard?industry_pack_id=financial_markets&time_range=7d",
                            headers={"Authorization": "Bearer benchmark-placeholder"},
                        ).status_code
                        == 200
                    )
                )
        metrics["dashboard_api"] = summarize_samples(
            dashboard_samples, scope="local_deterministic"
        )

        left = "performance-left"
        right = "performance-right"
        for session_id, suffix in ((left, "A"), (right, "B")):
            database.save_chat_qa(
                session_id,
                "local",
                "基线",
                "如何理解资本市场监管政策？",
                f"监管政策需要从信息披露、市场稳定和投资者保护三个方面评估。版本 {suffix}。",
            )
        source_rows = []
        for session_id in (left, right):
            source_rows.extend(
                {**dict(row), "_source_session_id": session_id}
                for row in database.get_chat_session_messages(session_id)
            )

        divide_samples = []
        for _index in range(repeats):
            def divide_once():
                cleaned, _removed = _clean_chat_history_rows(source_rows)
                output_id = uuid.uuid4().hex
                return all(
                    database.save_chat_qa(
                        output_id,
                        item.get("model_id") or "cleanup",
                        "历史会话最大公约简化",
                        item["question"],
                        item["answer"],
                    )
                    for item in cleaned
                )

            divide_samples.append(_timed(divide_once))
        metrics["history_divide_gcd"] = summarize_samples(
            divide_samples, scope="local_deterministic"
        )

        write_samples = []
        for index in range(repeats):
            write_samples.append(
                _timed(
                    lambda index=index: bool(
                        database.save_chat_qa(
                            f"write-{index}",
                            "benchmark",
                            "performance",
                            f"write question {index}",
                            "write answer with sufficient complete content",
                        )
                    )
                )
            )
        metrics["sqlite_committed_write"] = summarize_samples(
            write_samples, scope="local_deterministic"
        )
        database.disconnect()
    config.INTEL_LLM_ENABLED = original_llm
    config.CRAWL_REQUIRE_KEYWORD_MATCH = original_keyword_guard
    return metrics


def _local_chat_sse_metric(repeats: int) -> dict:
    import chat_api
    from flask import Flask
    from chat_api import chat_bp

    config_payload = {
        "active_model": "local",
        "models": {
            "local": {
                "api_key": "benchmark-placeholder",
                "model_id": "benchmark-model",
                "base_url": "http://example.invalid/v1",
                "use_proxy": False,
            }
        },
    }

    def fake_stream(*_args, **_kwargs):
        yield "benchmark token"

    app = Flask("stage1-performance-chat")
    app.register_blueprint(chat_bp)
    event_samples = []
    token_samples = []
    with patch.object(chat_api, "_load_config", return_value=config_payload), patch.object(
        chat_api, "_stream_openai", side_effect=fake_stream
    ), patch.object(chat_api, "_save_chat_metric", return_value=None):
        client = app.test_client()
        for _index in range(repeats):
            started = time.perf_counter()
            response = client.post(
                "/api/chat/send",
                json={
                    "model": "local",
                    "messages": [{"role": "user", "content": "performance"}],
                    "web_search": False,
                },
                buffered=False,
            )
            first_event = None
            first_token = None
            for chunk in response.response:
                for line in chunk.decode("utf-8").splitlines():
                    event = parse_sse_event_line(line)
                    if not event:
                        continue
                    elapsed = (time.perf_counter() - started) * 1000
                    first_event = elapsed if first_event is None else first_event
                    if event.get("type") == "chunk" and event.get("content"):
                        first_token = elapsed
            event_samples.append(
                {
                    "elapsed_ms": first_event or 0,
                    "success": first_event is not None,
                }
            )
            token_samples.append(
                {
                    "elapsed_ms": first_token or 0,
                    "success": first_token is not None,
                }
            )
    return {
        "chat_first_sse_event": summarize_samples(
            event_samples, scope="local_deterministic"
        ),
        "chat_first_content_token": summarize_samples(
            token_samples, scope="local_deterministic"
        ),
    }


def _active_session_token(database_path: Path) -> str:
    connection = sqlite3.connect(
        f"{database_path.resolve().as_uri()}?mode=ro", uri=True, timeout=10
    )
    try:
        row = connection.execute(
            """
            SELECT s.session_token
            FROM user_sessions s JOIN users u ON u.id=s.user_id
            WHERE u.is_active=1 AND s.expires_at > datetime('now','localtime')
            ORDER BY s.id DESC LIMIT 1
            """
        ).fetchone()
        if not row:
            raise RuntimeError("no active authenticated session available")
        return str(row[0])
    finally:
        connection.close()


def _live_http_metrics(
    repeats: int,
    *,
    base_url: str,
    database_path: Path,
) -> dict:
    token = _active_session_token(database_path)
    session = requests.Session()
    # firecrawl_app.before_request authenticates API calls from the browser-style
    # session cookie before route decorators inspect Authorization headers.  Use
    # the same path as the production UI, while never serializing the token.
    session.cookies.set("session_token", token)
    dashboard_samples = []
    for _index in range(repeats):
        started = time.perf_counter()
        try:
            response = session.get(
                f"{base_url.rstrip('/')}/api/intel/dashboard",
                params={
                    "industry_pack_id": "financial_markets",
                    "time_range": "7d",
                },
                timeout=30,
            )
            dashboard_samples.append(
                {
                    "elapsed_ms": (time.perf_counter() - started) * 1000,
                    "success": response.status_code == 200,
                    "error_type": (
                        "" if response.status_code == 200
                        else f"HTTPStatus{response.status_code}"
                    ),
                }
            )
        except Exception as exc:
            dashboard_samples.append(
                {
                    "elapsed_ms": (time.perf_counter() - started) * 1000,
                    "success": False,
                    "error_type": type(exc).__name__,
                }
            )

    event_samples = []
    token_samples = []
    for index in range(repeats):
        started = time.perf_counter()
        first_event = None
        first_token = None
        success = False
        error_type = ""
        try:
            with session.post(
                f"{base_url.rstrip('/')}/api/chat/send",
                json={
                    "model": "local",
                    "messages": [
                        {
                            "role": "user",
                            "content": f"只回复：性能基线 {index + 1}",
                        }
                    ],
                    "web_search": False,
                },
                stream=True,
                timeout=(10, 180),
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines(decode_unicode=True):
                    event = parse_sse_event_line(line or "")
                    if not event:
                        continue
                    elapsed = (time.perf_counter() - started) * 1000
                    first_event = elapsed if first_event is None else first_event
                    if event.get("type") == "chunk" and event.get("content"):
                        first_token = first_token or elapsed
                    if event.get("type") == "done":
                        success = first_token is not None
                    if event.get("type") == "error":
                        error_type = "SSEError"
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            error_type = (
                f"HTTPStatus{status_code}" if status_code is not None else "HTTPError"
            )
        except Exception as exc:
            error_type = type(exc).__name__
        event_samples.append(
            {
                "elapsed_ms": first_event or (time.perf_counter() - started) * 1000,
                "success": first_event is not None and not error_type,
                "error_type": error_type,
            }
        )
        token_samples.append(
            {
                "elapsed_ms": first_token or (time.perf_counter() - started) * 1000,
                "success": success,
                "error_type": error_type,
            }
        )
    return {
        "dashboard_api": summarize_samples(dashboard_samples, scope="live_local_http"),
        "chat_first_sse_event": summarize_samples(
            event_samples, scope="live_local_llm"
        ),
        "chat_first_content_token": summarize_samples(
            token_samples, scope="live_local_llm"
        ),
    }


def _live_rss_metric(repeats: int) -> dict:
    from intel_light_scanner import RSSScanner

    feed_url = "https://www.hkma.gov.hk/eng/other-information/rss/rss_press-release.xml"
    scanner = RSSScanner()
    samples = []
    for _index in range(repeats):
        samples.append(
            _timed(
                lambda: len(scanner.scan({"source_url": feed_url}, limit=20)) > 0
            )
        )
    return summarize_samples(samples, scope="live_external_network")


def _live_multiply_metric(repeats: int) -> dict:
    from mapindex_api import _clean_chat_history_rows, _synthesize_chat_history

    rows = [
        {
            "question": "如何评估一项资本市场监管政策？",
            "answer": "应检查信息披露质量、投资者保护和市场稳定机制，并明确证据时点。",
            "model_id": "local",
            "topic": "performance",
            "_source_session_id": "one",
        },
        {
            "question": "如何形成监管政策的综合评估？",
            "answer": "在事实核验后比较政策目标、实施成本、流动性影响和风险边界。",
            "model_id": "local",
            "topic": "performance",
            "_source_session_id": "two",
        },
    ]
    cleaned, _removed = _clean_chat_history_rows(rows)
    samples = [
        _timed(lambda: isinstance(_synthesize_chat_history(cleaned), dict))
        for _index in range(repeats)
    ]
    return summarize_samples(samples, scope="live_local_llm")


def build_performance_baseline(
    *,
    repeats: int = 3,
    base_url: str = "http://127.0.0.1:8003",
    database_path: str | Path = "data/crawler_articles.db",
    include_live: bool = True,
) -> dict:
    if repeats < 3:
        raise ValueError("at least three repetitions are required")
    original_database_path = os.environ.get("DATABASE_PATH")
    try:
        with tempfile.TemporaryDirectory(prefix="stage1-bootstrap-") as bootstrap_dir:
            os.environ["DATABASE_PATH"] = str(Path(bootstrap_dir) / "bootstrap.sqlite3")
            local_metrics = {"rss_parse_20_items": _local_rss_metric(repeats)}
            local_metrics.update(_local_application_metrics(repeats))
            local_metrics.update(_local_chat_sse_metric(repeats))
            live_metrics = {}
            if include_live:
                live_metrics.update(
                    _live_http_metrics(
                        repeats,
                        base_url=base_url,
                        database_path=Path(database_path),
                    )
                )
                live_metrics["rss_fetch_parse_20_items"] = _live_rss_metric(repeats)
                live_metrics["history_multiply_synthesis"] = _live_multiply_metric(repeats)
    finally:
        if original_database_path is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = original_database_path
    all_metrics = list(local_metrics.values()) + list(live_metrics.values())
    acceptance = {
        "minimum_three_samples_each": all(
            metric["sample_count"] >= 3 for metric in all_metrics
        ),
        "all_samples_successful": all(
            metric["success_count"] == metric["sample_count"]
            for metric in all_metrics
        ),
        "local_and_live_separated": bool(local_metrics) and (
            bool(live_metrics) if include_live else True
        ),
        "raw_prompts_responses_and_tokens_recorded": False,
    }
    acceptance["passed"] = all(
        (
            acceptance["minimum_three_samples_each"],
            acceptance["all_samples_successful"],
            acceptance["local_and_live_separated"],
            not acceptance["raw_prompts_responses_and_tokens_recorded"],
        )
    )
    return {
        "manifest_version": "stage1-performance-baseline-v1",
        "captured_at_utc": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "server_clock_source": "datetime.now(timezone.utc) on the project host",
        "repetitions": repeats,
        "local_metrics": local_metrics,
        "live_metrics": live_metrics,
        "scope_notes": {
            "local_deterministic": "temporary SQLite, synthetic RSS, mocked LLM stream; excludes external network",
            "live_local_http": "actual authenticated localhost API and production read path",
            "live_local_llm": "actual configured local LLM; no web search and no response text retained",
            "live_external_network": "one official HKMA RSS; network latency is not a regression threshold",
        },
        "acceptance": acceptance,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Capture stage-one performance baseline")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--base-url", default="http://127.0.0.1:8003")
    parser.add_argument("--database", default="data/crawler_articles.db")
    parser.add_argument("--skip-live", action="store_true")
    parser.add_argument("--output", default="baseline/performance-baseline.json")
    args = parser.parse_args(argv)
    report = build_performance_baseline(
        repeats=args.repeats,
        base_url=args.base_url,
        database_path=args.database,
        include_live=not args.skip_live,
    )
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "local_metrics": {
                    name: {"median_ms": value["median_ms"], "p95_ms": value["p95_ms"]}
                    for name, value in report["local_metrics"].items()
                },
                "live_metrics": {
                    name: {"median_ms": value["median_ms"], "p95_ms": value["p95_ms"]}
                    for name, value in report["live_metrics"].items()
                },
                "acceptance": report["acceptance"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["acceptance"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
