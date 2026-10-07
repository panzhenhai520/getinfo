#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tools.check_tradingagents_architecture import (
    check_repository,
    parse_compose_surface,
    parse_exposed_ports,
    parse_requirement_packages,
    validate_registry,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TradingAgentsArchitectureTests(unittest.TestCase):
    def _registry_invariants(self) -> dict:
        registry = json.loads(
            (PROJECT_ROOT / "architecture" / "tradingagents-component-registry.json").read_text(
                encoding="utf-8"
            )
        )
        return registry["architecture_invariants"]

    def test_current_registry_compose_dependencies_ports_and_tables_pass(self):
        report = check_repository(PROJECT_ROOT)
        self.assertTrue(report["acceptance"]["passed"], report)
        # 断言对象取"已批准的基线"，而不是把服务/端口再抄一份写死在用例里：
        # 抄死会在每次正式批准新服务（如 postgres / qa-worker）时又制造一次假失败。
        invariants = self._registry_invariants()
        self.assertEqual(
            report["compose"]["services"],
            sorted(invariants["compose_services_after_stage2"]),
        )
        self.assertEqual(
            report["ports"]["compose_published_container_ports"],
            sorted(invariants["compose_published_container_ports"]),
        )
        self.assertEqual(
            report["ports"]["dockerfile_exposed_ports"],
            sorted(invariants["published_container_ports"]),
        )
        self.assertEqual(report["dependencies"]["prohibited_dependencies_found"], [])

    def test_static_parsers_detect_new_service_port_and_duplicate_dependencies(self):
        compose = """services:
  crawler:
    ports:
      - \"8003:8003\"
  tradingagents-api:
    ports:
      - \"9000:9000\"
volumes:
  data:
"""
        self.assertEqual(
            parse_compose_surface(compose),
            {
                "services": ["crawler", "tradingagents-api"],
                "published_container_ports": [8003, 9000],
            },
        )
        self.assertEqual(parse_exposed_ports("EXPOSE 8003 9000/tcp\n"), [8003, 9000])
        self.assertEqual(
            parse_requirement_packages(
                "requests==2.0\nchromadb>=1.0 # prohibited\n-r base.txt\n"
            ),
            {"requests", "chromadb"},
        )

    def test_shared_broker_and_orchestrator_are_explicit_embedded_additions(self):
        registry = json.loads(
            (PROJECT_ROOT / "architecture/tradingagents-component-registry.json")
            .read_text(encoding="utf-8")
        )
        decisions = {item["id"]: item for item in registry["components"]}
        broker = decisions["shared_llm_broker"]
        orchestrator = decisions["multi_role_orchestration"]
        self.assertEqual(broker["disposition"], "add_embedded")
        self.assertEqual(broker["current_state"], "available_embedded_v1")
        self.assertIn("shared_llm_broker.py", broker["existing"]["files"])
        self.assertEqual(broker["planned"]["services"], [])
        self.assertEqual(orchestrator["disposition"], "add_embedded")
        self.assertEqual(orchestrator["current_state"], "not_available")
        self.assertEqual(orchestrator["planned"]["ports"], [])

    def test_registry_validator_rejects_unmapped_new_service(self):
        registry = {
            "components": [
                {
                    "id": "bad",
                    "capability": "bad",
                    "disposition": "add_embedded",
                    "current_state": "not_available",
                    "existing": {"files": [], "processes": [], "tables": [], "apis": []},
                    "planned": {
                        "files": ["future.py"],
                        "services": ["new-service"],
                        "ports": [9000],
                    },
                    "boundary": "none",
                }
            ],
            "explicitly_forbidden": [],
        }
        result = validate_registry(PROJECT_ROOT, registry)
        self.assertFalse(result["passed"])
        self.assertIn("bad: new service is prohibited", result["errors"])
        self.assertIn("bad: new port is prohibited", result["errors"])


if __name__ == "__main__":
    unittest.main()
