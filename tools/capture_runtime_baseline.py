#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Capture dependency, Compose, image, container, and health baselines safely."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import shlex
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(root: Path, args: list[str], *, timeout: int = 60) -> str:
    result = subprocess.run(
        args,
        cwd=str(root),
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def summarize_compose(payload: dict) -> dict:
    services = {}
    raw_services = payload.get("services") or {}
    for name in sorted(raw_services):
        service = raw_services[name] or {}
        volumes = []
        for volume in service.get("volumes") or []:
            if isinstance(volume, str):
                parts = volume.split(":")
                volumes.append(
                    {
                        "type": "bind_or_volume",
                        "source": parts[0] if parts else "",
                        "target": parts[1] if len(parts) > 1 else "",
                        "read_only": len(parts) > 2 and parts[2] == "ro",
                    }
                )
            elif isinstance(volume, dict):
                volumes.append(
                    {
                        "type": str(volume.get("type") or ""),
                        "source": str(volume.get("source") or ""),
                        "target": str(volume.get("target") or ""),
                        "read_only": bool(volume.get("read_only", False)),
                    }
                )
        environment = service.get("environment") or {}
        environment_keys = (
            sorted(environment)
            if isinstance(environment, dict)
            else sorted(str(item).split("=", 1)[0] for item in environment)
        )
        depends_on = service.get("depends_on") or {}
        if isinstance(depends_on, dict):
            depends_on = sorted(depends_on)
        else:
            depends_on = sorted(str(item) for item in depends_on)
        env_files = service.get("env_file") or []
        if isinstance(env_files, (str, dict)):
            env_files = [env_files]
        services[name] = {
            "container_name": str(service.get("container_name") or ""),
            "image": str(service.get("image") or ""),
            "build": bool(service.get("build")),
            "command": service.get("command") or [],
            "ports": service.get("ports") or [],
            "volumes": volumes,
            "depends_on": depends_on,
            "environment_keys": environment_keys,
            "environment_values_recorded": False,
            "env_files": [
                str(item.get("path") or "") if isinstance(item, dict) else str(item)
                for item in env_files
            ],
            "healthcheck_configured": bool(service.get("healthcheck")),
            "restart": str(service.get("restart") or ""),
        }
    return {
        "services": services,
        "networks": sorted((payload.get("networks") or {}).keys()),
        "volumes": sorted((payload.get("volumes") or {}).keys()),
    }


def _compose_payload(root: Path, compose_file: Path) -> dict:
    output = _run(
        root,
        [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "config",
            "--no-env-resolution",
            "--no-interpolate",
            "--no-path-resolution",
            "--format",
            "json",
        ],
    )
    return json.loads(output)


def _image_metadata(root: Path, image: str) -> dict:
    output = _run(
        root,
        [
            "docker",
            "image",
            "inspect",
            image,
            "--format",
            "{{.Id}}|{{json .RepoDigests}}|{{.Created}}|{{.Os}}|{{.Architecture}}|{{.Size}}",
        ],
    )
    image_id, digests, created, os_name, architecture, size = output.split("|", 5)
    return {
        "image_id": image_id,
        "repo_digests": json.loads(digests),
        "created": created,
        "os": os_name,
        "architecture": architecture,
        "size": int(size),
    }


def _container_metadata(root: Path, container_name: str) -> dict:
    output = _run(
        root,
        [
            "docker",
            "inspect",
            container_name,
            "--format",
            "{{.Name}}|{{.Image}}|{{.Config.Image}}|{{.State.Status}}|"
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
        ],
    )
    name, image_id, configured_image, state, health = output.split("|", 4)
    return {
        "container_name": name.lstrip("/"),
        "runtime_image_id": image_id,
        "configured_image": configured_image,
        "state": state,
        "health": health,
        "environment_values_recorded": False,
    }


def _health_summary(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
            return {
                "http_status": int(response.status),
                "success": bool(payload.get("success")),
                "status": str(payload.get("status") or ""),
                "running_tasks": int(payload.get("running_tasks") or 0),
                "zombie_count": int(payload.get("zombie_count") or 0),
                "chrome_process_count": int(
                    payload.get("chrome_process_count") or 0
                ),
            }
    except Exception as exc:
        return {
            "http_status": 0,
            "success": False,
            "status": "unreachable",
            "error_type": type(exc).__name__,
        }


def _normalized_freeze(text: str) -> list[str]:
    return sorted(
        (line.strip() for line in text.splitlines() if line.strip()),
        key=str.casefold,
    )


def _host_packages() -> list[str]:
    packages = {
        f"{distribution.metadata.get('Name') or distribution.name}=={distribution.version}"
        for distribution in importlib.metadata.distributions()
    }
    return sorted(packages, key=str.casefold)


def _systemd_summary(root: Path, unit: str) -> dict:
    properties = (
        "Id,ActiveState,SubState,MainPID,WorkingDirectory,User,Group,"
        "FragmentPath,UnitFileState,Restart,ExecStart"
    )
    output = _run(
        root,
        ["systemctl", "show", unit, f"--property={properties}", "--no-pager"],
    )
    values = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    exec_start = values.pop("ExecStart", "")
    path_match = re.search(r"\bpath=([^ ;]+)", exec_start)
    argv_match = re.search(r"\bargv\[\]=([^;]+)", exec_start)
    argv = []
    if argv_match:
        try:
            raw_argv = shlex.split(argv_match.group(1).strip())
        except ValueError:
            raw_argv = []
        for index, value in enumerate(raw_argv):
            if index < 2 and not re.search(
                r"(?i)(password|secret|token|api[_-]?key|credential)", value
            ):
                argv.append(value)
            else:
                argv.append("REDACTED_ARGUMENT")
    fragment = Path(values.get("FragmentPath") or "")
    return {
        "unit": values.get("Id") or unit,
        "active_state": values.get("ActiveState") or "",
        "sub_state": values.get("SubState") or "",
        "main_pid": int(values.get("MainPID") or 0),
        "working_directory": values.get("WorkingDirectory") or "",
        "user": values.get("User") or "",
        "group": values.get("Group") or "",
        "unit_file_state": values.get("UnitFileState") or "",
        "restart": values.get("Restart") or "",
        "fragment_path": str(fragment) if str(fragment) != "." else "",
        "fragment_sha256": _sha256(fragment) if fragment.is_file() else "",
        "exec_path": path_match.group(1) if path_match else "",
        "exec_argv": argv,
        "environment_values_recorded": False,
    }


def parse_listeners(output: str, ports: tuple[int, ...]) -> list[dict]:
    listeners = []
    wanted = set(ports)
    for line in output.splitlines():
        address_match = re.search(r"\s([^\s]+):(\d+)\s+[^\s]+:\*", line)
        if not address_match:
            continue
        port = int(address_match.group(2))
        if port not in wanted:
            continue
        process_match = re.search(r'\(\("([^\"]+)",pid=(\d+)', line)
        listeners.append(
            {
                "address": address_match.group(1),
                "port": port,
                "process": process_match.group(1) if process_match else "",
                "pid": int(process_match.group(2)) if process_match else 0,
            }
        )
    return sorted(listeners, key=lambda item: (item["port"], item["address"]))


def capture_runtime_baseline(
    repository: str | Path,
    *,
    compose_path: str | Path = "docker-compose.crawler.yml",
    lock_path: str | Path = "baseline/runtime-pip-freeze.txt",
    package_container: str = "firecrawl-crawler",
    health_url: str = "http://127.0.0.1:8003/api/system/health",
    smoke_image: str = "firecrawlapp-crawler:baseline-smoke",
    systemd_unit: str = "firecrawl.service",
) -> dict:
    root = Path(repository).expanduser().resolve()
    compose_file = Path(compose_path)
    compose_file = compose_file if compose_file.is_absolute() else root / compose_file
    lock_file = Path(lock_path)
    lock_file = lock_file if lock_file.is_absolute() else root / lock_file
    compose = summarize_compose(_compose_payload(root, compose_file))
    configured_images = sorted(
        {
            service["image"]
            for service in compose["services"].values()
            if service["image"]
        }
    )
    images = {image: _image_metadata(root, image) for image in configured_images}
    smoke_metadata = _image_metadata(root, smoke_image)
    containers = {}
    for service_name, service in compose["services"].items():
        containers[service_name] = _container_metadata(
            root, service["container_name"]
        )
    installed_freeze = _run(
        root,
        ["docker", "exec", package_container, "python", "-m", "pip", "freeze", "--all"],
        timeout=120,
    )
    locked_freeze = lock_file.read_text(encoding="utf-8")
    package_lock_matches = _normalized_freeze(installed_freeze) == _normalized_freeze(
        locked_freeze
    )
    expected_services = set(compose["services"])
    runtime_services = set(containers)
    services_running = all(
        item["state"] == "running" for item in containers.values()
    )
    healthchecks_passed = all(
        not compose["services"][name]["healthcheck_configured"]
        or containers[name]["health"] == "healthy"
        for name in expected_services
    )
    health = _health_summary(health_url)
    systemd_service = _systemd_summary(root, systemd_unit)
    listeners = parse_listeners(_run(root, ["ss", "-ltnp"]), (8003, 6379))
    host_packages = _host_packages()
    acceptance = {
        "compose_service_set_matches_runtime": expected_services == runtime_services,
        "all_services_running": services_running,
        "configured_healthchecks_passed": healthchecks_passed,
        "runtime_packages_match_lock": package_lock_matches,
        "system_health_endpoint_passed": health.get("http_status") == 200
        and health.get("success") is True,
        "systemd_web_service_active": systemd_service["active_state"] == "active"
        and systemd_service["sub_state"] == "running",
        "web_port_listener_present": any(
            listener["port"] == 8003 for listener in listeners
        ),
        "host_python_dependencies_recorded": bool(host_packages),
        "all_configured_images_have_immutable_ids": all(
            item.get("image_id", "").startswith("sha256:")
            for item in images.values()
        ),
        "smoke_image_has_immutable_id": smoke_metadata.get("image_id", "").startswith(
            "sha256:"
        ),
        "environment_values_absent": True,
    }
    acceptance["passed"] = all(
        value is True for key, value in acceptance.items() if key != "passed"
    )
    tracked_files = (
        "requirements.txt",
        "Dockerfile",
        "Dockerfile.baseline-smoke",
        ".dockerignore",
        "docker-compose.crawler.yml",
        "baseline/docker-compose.image-lock.yml",
        "baseline/runtime-pip-freeze.txt",
    )
    return {
        "manifest_version": "runtime-topology-baseline-v1",
        "captured_at_utc": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "repository_root": str(root),
        "host_python": platform.python_version(),
        "host_python_executable": sys.executable,
        "host_python_packages": host_packages,
        "host_python_packages_sha256": hashlib.sha256(
            ("\n".join(host_packages) + "\n").encode("utf-8")
        ).hexdigest(),
        "tracked_file_sha256": {
            relative: _sha256(root / relative)
            for relative in tracked_files
            if (root / relative).is_file()
        },
        "package_lock_count": len(_normalized_freeze(locked_freeze)),
        "package_lock_values_recorded": True,
        "compose": compose,
        "configured_images": images,
        "smoke_image": {"name": smoke_image, **smoke_metadata},
        "runtime_containers": containers,
        "systemd_web_service": systemd_service,
        "listeners": listeners,
        "system_health": health,
        "acceptance": acceptance,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Capture runtime topology baseline")
    parser.add_argument("--repository", default=".")
    parser.add_argument("--compose", default="docker-compose.crawler.yml")
    parser.add_argument("--lock", default="baseline/runtime-pip-freeze.txt")
    parser.add_argument("--package-container", default="firecrawl-crawler")
    parser.add_argument(
        "--health-url", default="http://127.0.0.1:8003/api/system/health"
    )
    parser.add_argument("--smoke-image", default="firecrawlapp-crawler:baseline-smoke")
    parser.add_argument("--systemd-unit", default="firecrawl.service")
    parser.add_argument("--output", default="baseline/runtime-topology-manifest.json")
    args = parser.parse_args(argv)
    report = capture_runtime_baseline(
        args.repository,
        compose_path=args.compose,
        lock_path=args.lock,
        package_container=args.package_container,
        health_url=args.health_url,
        smoke_image=args.smoke_image,
        systemd_unit=args.systemd_unit,
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
                "services": sorted(report["compose"]["services"]),
                "package_lock_count": report["package_lock_count"],
                "health": report["system_health"],
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
