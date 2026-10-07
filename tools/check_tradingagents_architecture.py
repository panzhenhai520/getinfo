#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Static guard for the TradingAgents reuse/add/forbid architecture decisions."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VALID_DISPOSITIONS = {"reuse", "extend", "add_embedded"}


def normalize_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", str(value or "").strip().casefold())


def parse_requirement_packages(text: str) -> set[str]:
    packages = set()
    for raw_line in str(text or "").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith(("-r", "--requirement", "--constraint")):
            continue
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9_.-]*)", line)
        if match:
            packages.add(normalize_package_name(match.group(1)))
    return packages


def parse_compose_surface(text: str) -> dict:
    services = []
    published_container_ports = []
    in_services = False
    current_service = ""
    in_ports = False
    for raw_line in str(text or "").splitlines():
        if raw_line == "services:":
            in_services = True
            current_service = ""
            in_ports = False
            continue
        if in_services and raw_line and not raw_line.startswith(" "):
            break
        service_match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", raw_line)
        if in_services and service_match:
            current_service = service_match.group(1)
            services.append(current_service)
            in_ports = False
            continue
        if current_service and re.match(r"^    ports:\s*$", raw_line):
            in_ports = True
            continue
        if in_ports:
            if re.match(r"^      -\s+", raw_line):
                value = raw_line.split("-", 1)[1].strip().strip("\"'")
                match = re.search(r":(\d+)(?:/(?:tcp|udp))?\s*$", value)
                if match:
                    published_container_ports.append(int(match.group(1)))
                continue
            if raw_line.strip() and not raw_line.startswith("      "):
                in_ports = False
    return {
        "services": sorted(set(services)),
        "published_container_ports": sorted(set(published_container_ports)),
    }


def parse_exposed_ports(text: str) -> list[int]:
    ports = set()
    for match in re.finditer(r"(?im)^\s*EXPOSE\s+(.+?)\s*$", str(text or "")):
        for token in match.group(1).split():
            port = token.split("/", 1)[0]
            if port.isdigit():
                ports.add(int(port))
    return sorted(ports)


def validate_registry(root: Path, registry: dict) -> dict:
    errors = []
    components = registry.get("components") or []
    forbidden = registry.get("explicitly_forbidden") or []
    identifiers = [str(item.get("id") or "") for item in components]
    if not components:
        errors.append("components is empty")
    if len(identifiers) != len(set(identifiers)) or any(not value for value in identifiers):
        errors.append("component ids must be non-empty and unique")
    for component in components:
        component_id = str(component.get("id") or "<missing>")
        disposition = component.get("disposition")
        if disposition not in VALID_DISPOSITIONS:
            errors.append(f"{component_id}: invalid disposition {disposition}")
        if not str(component.get("current_state") or ""):
            errors.append(f"{component_id}: current_state is required")
        existing = component.get("existing")
        planned = component.get("planned")
        if not isinstance(existing, dict) or not isinstance(planned, dict):
            errors.append(f"{component_id}: existing/planned mappings are required")
            continue
        for relative in existing.get("files") or []:
            if not (root / relative).is_file():
                errors.append(f"{component_id}: mapped file missing: {relative}")
        if planned.get("services"):
            errors.append(f"{component_id}: new service is prohibited")
        if planned.get("ports"):
            errors.append(f"{component_id}: new port is prohibited")
        if disposition == "reuse" and "not_available" in str(
            component.get("current_state")
        ):
            errors.append(f"{component_id}: unavailable component cannot be reuse")
        embedded_asset_mapped = bool(
            planned.get("files")
            or planned.get("tables")
            or (
                "available" in str(component.get("current_state") or "")
                and (existing.get("files") or existing.get("tables"))
            )
        )
        if disposition == "add_embedded" and not embedded_asset_mapped:
            errors.append(f"{component_id}: embedded addition has no asset mapping")
        if not str(component.get("boundary") or ""):
            errors.append(f"{component_id}: boundary is required")
    for item in forbidden:
        if item.get("disposition") != "forbid" or not item.get("reason"):
            errors.append(f"forbidden decision invalid: {item.get('id')}")
    return {
        "component_count": len(components),
        "forbidden_decision_count": len(forbidden),
        "errors": errors,
        "passed": not errors,
    }


def check_repository(
    repository: str | Path,
    *,
    registry_path: str | Path = "architecture/tradingagents-component-registry.json",
    compose_path: str | Path = "docker-compose.crawler.yml",
    requirements_path: str | Path = "requirements.txt",
    dockerfile_path: str | Path = "Dockerfile",
    database_manifest_path: str | Path = "baseline/database-backup-manifest.json",
) -> dict:
    root = Path(repository).expanduser().resolve()

    def resolve(value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else root / path

    registry_file = resolve(registry_path)
    registry = json.loads(registry_file.read_text(encoding="utf-8"))
    registry_check = validate_registry(root, registry)
    invariants = registry.get("architecture_invariants") or {}
    compose_surface = parse_compose_surface(
        resolve(compose_path).read_text(encoding="utf-8")
    )
    requirements = parse_requirement_packages(
        resolve(requirements_path).read_text(encoding="utf-8")
    )
    exposed_ports = parse_exposed_ports(
        resolve(dockerfile_path).read_text(encoding="utf-8")
    )
    prohibited_packages = {
        normalize_package_name(value)
        for value in registry.get("prohibited_packages") or []
    }
    prohibited_dependencies_found = sorted(requirements & prohibited_packages)
    service_terms = tuple(
        str(value).casefold()
        for value in registry.get("prohibited_service_terms") or []
    )
    prohibited_services_found = sorted(
        service
        for service in compose_surface["services"]
        if any(term in service.casefold() for term in service_terms)
    )
    expected_services = sorted(invariants.get("compose_services_before") or [])
    stage2_services = sorted(invariants.get("compose_services_after_stage2") or [])
    allowed_ports = sorted(invariants.get("published_container_ports") or [])
    # compose 的发布端口与本镜像 Dockerfile 的 EXPOSE 不是同一件事：compose 里还包含
    # postgres 等基础服务的容器端口（如 5432），而应用镜像只暴露自己的 8003。
    # 两者分开登记，避免"给 compose 放行 5432"顺带把 Dockerfile 的红线也放宽。
    allowed_compose_ports = sorted(
        invariants.get("compose_published_container_ports") or allowed_ports
    )

    database_manifest = json.loads(
        resolve(database_manifest_path).read_text(encoding="utf-8")
    )
    baseline_tables = set(
        database_manifest.get("backup_snapshot", {}).get("tables") or []
    )
    mapped_existing_tables = {
        table
        for component in registry.get("components") or []
        for table in (component.get("existing") or {}).get("tables") or []
    }
    missing_mapped_tables = sorted(mapped_existing_tables - baseline_tables)
    mapped_processes = {
        process
        for component in registry.get("components") or []
        for process in (component.get("existing") or {}).get("processes") or []
    }
    unknown_mapped_processes = sorted(
        mapped_processes - set(compose_surface["services"])
    )

    acceptance = {
        "registry_schema_and_mappings_valid": registry_check["passed"],
        "compose_service_set_matches_baseline": compose_surface["services"]
        == expected_services,
        "stage2_service_set_unchanged": stage2_services == expected_services,
        "no_prohibited_service_present": not prohibited_services_found,
        "published_ports_unchanged": compose_surface["published_container_ports"]
        == allowed_compose_ports,
        "dockerfile_exposed_ports_unchanged": exposed_ports == allowed_ports,
        "no_prohibited_dependency_present": not prohibited_dependencies_found,
        "mapped_existing_tables_exist": not missing_mapped_tables,
        "mapped_processes_exist": not unknown_mapped_processes,
        "new_service_disabled_by_policy": invariants.get("new_service_allowed")
        is False,
        "new_port_disabled_by_policy": invariants.get("new_published_port_allowed")
        is False,
        "runtime_install_disabled_by_policy": invariants.get(
            "runtime_git_clone_or_install_allowed"
        )
        is False,
        "real_trading_disabled_by_policy": invariants.get(
            "real_broker_or_order_gateway_allowed"
        )
        is False,
    }
    acceptance["passed"] = all(acceptance.values())
    return {
        "check_version": "tradingagents-architecture-static-v1",
        "checked_at_utc": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "registry": {
            "path": str(registry_file.relative_to(root)),
            "version": registry.get("registry_version"),
            **registry_check,
        },
        "compose": {
            **compose_surface,
            "expected_services": expected_services,
            "stage2_services": stage2_services,
            "prohibited_services_found": prohibited_services_found,
        },
        "dependencies": {
            "requirement_package_count": len(requirements),
            "prohibited_dependencies_found": prohibited_dependencies_found,
        },
        "ports": {
            "allowed": allowed_ports,
            "compose_published_container_ports": compose_surface[
                "published_container_ports"
            ],
            "dockerfile_exposed_ports": exposed_ports,
        },
        "database_mapping": {
            "baseline_table_count": len(baseline_tables),
            "mapped_existing_table_count": len(mapped_existing_tables),
            "missing_mapped_tables": missing_mapped_tables,
        },
        "process_mapping": {
            "mapped_processes": sorted(mapped_processes),
            "unknown_mapped_processes": unknown_mapped_processes,
        },
        "acceptance": acceptance,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Check TradingAgents architecture reuse boundaries"
    )
    parser.add_argument("--repository", default=".")
    parser.add_argument(
        "--registry", default="architecture/tradingagents-component-registry.json"
    )
    parser.add_argument(
        "--output", default="architecture/tradingagents-component-acceptance.json"
    )
    args = parser.parse_args(argv)
    report = check_repository(args.repository, registry_path=args.registry)
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
                "component_count": report["registry"]["component_count"],
                "services": report["compose"]["services"],
                "published_ports": report["ports"][
                    "compose_published_container_ports"
                ],
                "prohibited_dependencies_found": report["dependencies"][
                    "prohibited_dependencies_found"
                ],
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
