#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import unittest

from tools.capture_runtime_baseline import parse_listeners, summarize_compose


class RuntimeBaselineTests(unittest.TestCase):
    def test_compose_summary_preserves_topology_without_environment_values(self):
        payload = {
            "services": {
                "api": {
                    "container_name": "example-api",
                    "image": "example/api:1",
                    "environment": {
                        "API_KEY": "must-not-leak",
                        "FEATURE_ENABLED": "true",
                    },
                    "env_file": [{"path": ".env", "required": True}],
                    "ports": [{"published": "8003", "target": 8003}],
                    "volumes": [
                        {"type": "bind", "source": ".", "target": "/app"}
                    ],
                    "depends_on": {"redis": {"condition": "service_healthy"}},
                    "healthcheck": {"test": ["CMD", "health"]},
                    "restart": "unless-stopped",
                },
                "redis": {"image": "redis:7-alpine"},
            },
            "networks": {"default": {}},
            "volumes": {"redis_data": {}},
        }
        report = summarize_compose(payload)
        serialized = json.dumps(report)
        self.assertNotIn("must-not-leak", serialized)
        self.assertFalse(report["services"]["api"]["environment_values_recorded"])
        self.assertEqual(
            report["services"]["api"]["environment_keys"],
            ["API_KEY", "FEATURE_ENABLED"],
        )
        self.assertEqual(report["services"]["api"]["depends_on"], ["redis"])
        self.assertEqual(report["volumes"], ["redis_data"])

    def test_listener_parser_keeps_only_requested_ports_and_process_identity(self):
        output = (
            'LISTEN 0 128 0.0.0.0:8003 0.0.0.0:* users:(("python3",pid=42,fd=13))\n'
            'LISTEN 0 128 127.0.0.1:9000 0.0.0.0:* users:(("other",pid=7,fd=3))\n'
        )
        self.assertEqual(
            parse_listeners(output, (8003, 6379)),
            [{"address": "0.0.0.0", "port": 8003, "process": "python3", "pid": 42}],
        )


if __name__ == "__main__":
    unittest.main()
