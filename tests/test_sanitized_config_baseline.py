#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.capture_sanitized_config import capture_sanitized_config


class SanitizedConfigBaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "data").mkdir()
        (self.root / ".env.example").write_text(
            "FEATURE_ENABLED=false\n"
            "PROXY_ENABLED=false\n"
            "CRAWL_USE_PROXY_DEFAULT=false\n"
            "RAGFLOW_BASE_URL=\n"
            "RAGFLOW_API_KEY=\n"
            "FLASK_PORT=8003\n",
            encoding="utf-8",
        )
        (self.root / ".env").write_text(
            "FEATURE_ENABLED=true\n"
            "RAGFLOW_BASE_URL=https://user:pass@ragflow.internal:8443/v1?token=url-secret\n"
            "RAGFLOW_API_KEY=ragflow-private-123\n"
            "ACCESS_TOKEN=access-private-456\n",
            encoding="utf-8",
        )
        (self.root / "data" / "chat_config.json").write_text(
            json.dumps(
                {
                    "active_model": "local",
                    "ragflow_kb_id": "kb-private-id",
                    "models": {
                        "local": {
                            "api_key": "local-private-789",
                            "model_id": "local-model",
                            "base_url": "http://llm-user:llm-pass@local-llm.internal:8106/v1?key=nope",
                            "use_proxy": False,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_secrets_and_url_credentials_are_absent_but_state_is_preserved(self):
        report = capture_sanitized_config(self.root, environment={})
        serialized = json.dumps(report, ensure_ascii=False)
        for forbidden in (
            "ragflow-private-123",
            "access-private-456",
            "local-private-789",
            "url-secret",
            "llm-pass",
            "kb-private-id",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertTrue(report["acceptance"]["passed"])
        secret_states = {
            item["key"]: item["configured"]
            for item in report["configuration_keys"]
            if item["kind"] == "secret"
        }
        self.assertTrue(secret_states["RAGFLOW_API_KEY"])
        self.assertTrue(secret_states["ACCESS_TOKEN"])
        endpoints = {
            item["key"]: item["endpoint"] for item in report["endpoint_hosts"]
        }
        self.assertNotIn("PROXY_ENABLED", endpoints)
        self.assertNotIn("CRAWL_USE_PROXY_DEFAULT", endpoints)
        self.assertEqual(endpoints["RAGFLOW_BASE_URL"]["host"], "ragflow.internal")
        self.assertEqual(endpoints["RAGFLOW_BASE_URL"]["port"], 8443)
        local_model = report["chat_model_configuration"]["models"][0]
        self.assertTrue(local_model["api_key_configured"])
        self.assertEqual(local_model["endpoint"]["host"], "local-llm.internal")
        booleans = {item["key"]: item["value"] for item in report["boolean_settings"]}
        self.assertEqual(booleans["FEATURE_ENABLED"], True)

    def test_process_environment_has_precedence_without_exposing_value(self):
        report = capture_sanitized_config(
            self.root,
            environment={"RAGFLOW_API_KEY": "process-private-000"},
        )
        item = next(
            row
            for row in report["configuration_keys"]
            if row["key"] == "RAGFLOW_API_KEY"
        )
        self.assertEqual(item["source"], "process_environment")
        self.assertNotIn("process-private-000", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
