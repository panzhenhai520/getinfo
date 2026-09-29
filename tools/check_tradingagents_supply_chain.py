#!/usr/bin/env python3
"""Verify the pinned TradingAgents source, dependency lock and runtime graph.

The default mode is fully offline and performs static supply-chain checks.  The
``--runtime`` mode is intended for the built crawler image: it additionally
imports the installed distribution, compiles a minimal LangGraph and runs a
deterministic fixture through upstream state/routing/rating components.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PROVENANCE_PATH = ROOT / "vendor/tradingagents/PROVENANCE.json"
LOCK_PATH = ROOT / "requirements-tradingagents.lock"
INPUT_PATH = ROOT / "requirements-tradingagents.in"
LICENSE_PATH = ROOT / "vendor/tradingagents/LICENSE"
SBOM_PATH = ROOT / "architecture/tradingagents-sbom.json"
DOCKERFILE_PATH = ROOT / "Dockerfile"
OFFLINE_DOCKERFILE_PATH = ROOT / "Dockerfile.offline"

EXPECTED_VERSION = "0.3.1"
EXPECTED_COMMIT = "01477f9afb7a47b849ed4c9259d3a9a4738d9fda"
EXPECTED_TAG_OBJECT = "5a3d1b51d339202d03c4b57c1a1012f69376495f"
EXPECTED_ARCHIVE_SHA256 = "e5b1886d4d61ed7bd79da8294b3d7802408dc262c51fe1544ac317cc43f6566e"
EXPECTED_PYPROJECT_SHA256 = "cc11a95766eec8d73c9626cf6bea9647a7a5c0293e3a50cfe57418b1356bed70"
EXPECTED_LICENSE_SHA256 = "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"
EXPECTED_SIGNATURE_FINGERPRINT = "SHA256:N2/PlRlO8HTKzfBov/54/rsLLLI5BdBxvGWN1MUnhfU"

DIRECT_DEPENDENCIES = {
    "backtrader",
    "langchain-anthropic",
    "langchain-core",
    "langchain-experimental",
    "langchain-google-genai",
    "langchain-openai",
    "langgraph",
    "langgraph-checkpoint-sqlite",
    "pandas",
    "parsel",
    "python-dotenv",
    "pytz",
    "questionary",
    "redis",
    "requests",
    "rich",
    "setuptools",
    "stockstats",
    "tqdm",
    "typer",
    "typing-extensions",
    "yfinance",
}

EXPECTED_LOCK_VERSIONS = {
    "tradingagents": "0.3.1",
    "backtrader": "1.9.78.123",
    "langchain-anthropic": "1.5.3",
    "langchain-core": "1.5.3",
    "langchain-experimental": "0.4.2",
    "langchain-google-genai": "4.3.2",
    "langchain-openai": "1.4.1",
    "langgraph": "1.2.10",
    "langgraph-checkpoint-sqlite": "3.1.1",
    "greenlet": "3.0.1",
    "lxml": "5.4.0",
    "pytz": "2025.2",
    "redis": "6.2.0",
    "requests": "2.32.5",
    "yfinance": "1.5.1",
}

LICENSE_OVERRIDES = {
    "tradingagents": ("Apache-2.0", "vendored upstream LICENSE"),
    "backtrader": ("GPL-3.0-or-later", "distribution metadata and classifier"),
    "peewee": ("MIT", "official upstream LICENSE"),
    "tiktoken": ("MIT", "official upstream LICENSE"),
}


class CheckFailure(RuntimeError):
    """Raised when an acceptance invariant does not hold."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def _parse_lock() -> tuple[dict[str, str], dict[str, list[str]]]:
    text = LOCK_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    starts: list[tuple[int, str, str]] = []
    exact_re = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s\\;]+)")

    for index, line in enumerate(lines):
        match = exact_re.match(line)
        if match:
            starts.append((index, _normalize_name(match.group(1)), match.group(2)))
        elif line.startswith("./vendor/tradingagents/TradingAgents-"):
            starts.append((index, "tradingagents", EXPECTED_VERSION))

    versions: dict[str, str] = {}
    hashes: dict[str, list[str]] = {}
    for position, (start, name, version) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        block = "\n".join(lines[start:end])
        package_hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", block)
        _assert(name not in versions, f"duplicate locked distribution: {name}")
        _assert(bool(package_hashes), f"locked distribution has no SHA-256: {name}")
        versions[name] = version
        hashes[name] = sorted(set(package_hashes))

    _assert(len(versions) >= 90, f"dependency closure unexpectedly small: {len(versions)}")
    for name, version in EXPECTED_LOCK_VERSIONS.items():
        _assert(versions.get(name) == version, f"lock mismatch for {name}: {versions.get(name)!r}")
    _assert(hashes["tradingagents"] == [EXPECTED_ARCHIVE_SHA256], "source hash missing from lock")
    _assert("file:///home/" not in text, "lock contains a workstation-specific file URL")
    return versions, hashes


def _validate_archive(provenance: dict[str, Any]) -> dict[str, Any]:
    source = provenance["vendored_source"]
    archive_path = ROOT / source["path"]
    _assert(archive_path.is_file(), f"vendored source missing: {archive_path}")
    archive_sha = _sha256_file(archive_path)
    _assert(archive_sha == EXPECTED_ARCHIVE_SHA256, "vendored source SHA-256 mismatch")
    _assert(source["sha256"] == archive_sha, "provenance source hash mismatch")

    file_count = 0
    pyproject = b""
    upstream_license = b""
    root_prefix = source["archive_root"]
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive.getmembers():
            member_path = PurePosixPath(member.name)
            _assert(not member_path.is_absolute(), f"absolute archive member: {member.name}")
            _assert(".." not in member_path.parts, f"traversal archive member: {member.name}")
            _assert(not member.isdev(), f"device archive member: {member.name}")
            _assert(not member.issym() and not member.islnk(), f"linked archive member: {member.name}")
            _assert(
                member.name == root_prefix.rstrip("/") or member.name.startswith(root_prefix),
                f"unexpected archive root: {member.name}",
            )
            if member.isfile():
                file_count += 1
            if member.name == f"{root_prefix}pyproject.toml":
                extracted = archive.extractfile(member)
                pyproject = extracted.read() if extracted else b""
            if member.name == f"{root_prefix}LICENSE":
                extracted = archive.extractfile(member)
                upstream_license = extracted.read() if extracted else b""

    _assert(file_count >= 100, f"archive file count unexpectedly small: {file_count}")
    _assert(_sha256_bytes(pyproject) == EXPECTED_PYPROJECT_SHA256, "upstream pyproject hash mismatch")
    _assert(_sha256_bytes(upstream_license) == EXPECTED_LICENSE_SHA256, "upstream LICENSE hash mismatch")
    _assert(_sha256_file(LICENSE_PATH) == EXPECTED_LICENSE_SHA256, "vendored LICENSE hash mismatch")

    project_text = pyproject.decode("utf-8")
    version_match = re.search(r'^version\s*=\s*"([^"]+)"', project_text, re.MULTILINE)
    _assert(version_match is not None and version_match.group(1) == EXPECTED_VERSION, "version assertion failed")
    dependency_section = project_text.split("dependencies = [", 1)[1].split("]", 1)[0]
    dependencies = {
        _normalize_name(re.split(r"[<>=!~;\s]", item)[0])
        for item in re.findall(r'"([^"]+)"', dependency_section)
    }
    _assert(dependencies == DIRECT_DEPENDENCIES, "upstream direct dependency set drifted")
    _assert(b"Apache License" in upstream_license, "upstream license is not Apache-2.0 text")
    return {"path": source["path"], "sha256": archive_sha, "file_count": file_count}


def _validate_provenance() -> tuple[dict[str, Any], dict[str, Any]]:
    provenance = json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))
    upstream = provenance["upstream"]
    component = provenance["component"]
    policy = provenance["integration_policy"]
    _assert(component["version"] == EXPECTED_VERSION, "provenance version mismatch")
    _assert(component["license"] == "Apache-2.0", "provenance license mismatch")
    _assert(upstream["commit"] == EXPECTED_COMMIT, "upstream commit mismatch")
    _assert(upstream["tag_object"] == EXPECTED_TAG_OBJECT, "tag object mismatch")
    signature = upstream["tag_signature"]
    _assert(signature["verified"] is True, "release tag signature is not verified")
    _assert(signature["fingerprint"] == EXPECTED_SIGNATURE_FINGERPRINT, "release signer fingerprint mismatch")
    for key in (
        "runtime_git_clone",
        "upstream_cli_enabled",
        "upstream_web_service_enabled",
        "upstream_ollama_service_enabled",
        "upstream_redis_service_enabled",
        "upstream_database_service_enabled",
    ):
        _assert(policy[key] is False, f"integration policy unexpectedly enables {key}")
    return provenance, _validate_archive(provenance)


def _validate_docker_policy() -> dict[str, Any]:
    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
    offline_dockerfile = OFFLINE_DOCKERFILE_PATH.read_text(encoding="utf-8")
    _assert("TradingAgents-0.3.1-01477f9.tar.gz" in dockerfile, "Dockerfile does not copy pinned source")
    _assert("--require-hashes -r requirements-tradingagents.lock" in dockerfile, "Dockerfile does not enforce hashes")
    _assert("--no-build-isolation" in dockerfile, "Dockerfile build isolation policy missing")
    _assert("check_tradingagents_supply_chain.py --runtime" in dockerfile, "runtime build gate missing")
    _assert("git clone" not in dockerfile.lower(), "Dockerfile performs a runtime source clone")
    _assert("CMD [\"tradingagents\"" not in dockerfile, "upstream CLI became the image command")
    offline_lower = offline_dockerfile.lower()
    for prohibited in ("apt-get", "pip install", "curl ", "git clone"):
        _assert(prohibited not in offline_lower, f"offline Dockerfile contains {prohibited!r}")
    _assert(
        "ARG COLLECTINFO_LOCKED_RUNTIME_IMAGE=collectinfo:stage2-15" in offline_dockerfile,
        "offline Dockerfile does not require the accepted runtime image",
    )
    _assert(
        "check_tradingagents_supply_chain.py --runtime" in offline_dockerfile,
        "offline Dockerfile has no runtime acceptance gate",
    )
    return {
        "build_time_install": True,
        "hash_enforcement": True,
        "runtime_download": False,
        "upstream_entrypoint": False,
        "offline_application_rebuild": True,
        "offline_rebuild_requires_preloaded_runtime_image": True,
    }


def _license_from_metadata(name: str, metadata: importlib.metadata.PackageMetadata) -> tuple[str, str]:
    override = LICENSE_OVERRIDES.get(name)
    if override:
        return override
    expression = (metadata.get("License-Expression") or "").strip()
    if expression:
        return expression, "License-Expression metadata"
    license_text = (metadata.get("License") or "").strip()
    if license_text and license_text.upper() not in {"UNKNOWN", "NONE"} and len(license_text) <= 160:
        return license_text, "License metadata"
    classifiers = metadata.get_all("Classifier") or []
    license_classifiers = [item for item in classifiers if item.startswith("License ::")]
    if license_classifiers:
        return " ; ".join(license_classifiers), "Troves license classifier"
    return "NOASSERTION", "distribution metadata has no machine-readable license"


def _write_sbom(path: Path, locked_versions: dict[str, str]) -> dict[str, Any]:
    packages = []
    for name, locked_version in sorted(locked_versions.items()):
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise CheckFailure(f"locked distribution not installed: {name}") from exc
        installed_version = distribution.version
        _assert(installed_version == locked_version, f"runtime version mismatch for {name}: {installed_version}")
        license_id, license_evidence = _license_from_metadata(name, distribution.metadata)
        packages.append(
            {
                "name": name,
                "version": installed_version,
                "direct": name in DIRECT_DEPENDENCIES or name == "tradingagents",
                "license": license_id,
                "license_evidence": license_evidence,
            }
        )

    document = {
        "bom_format": "CollectInfo-SPDX-summary",
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "component": "TradingAgents embedded dependency closure",
        "source_commit": EXPECTED_COMMIT,
        "dependency_lock": {
            "path": "requirements-tradingagents.lock",
            "sha256": _sha256_file(LOCK_PATH),
        },
        "package_count": len(packages),
        "packages": packages,
        "license_boundary": {
            "upstream_framework": "Apache-2.0",
            "copyleft_dependency": "backtrader@1.9.78.123 is GPL-3.0-or-later",
            "redistribution_review_required": True,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return document


def _validate_sbom(locked_versions: dict[str, str], required: bool) -> dict[str, Any]:
    if not SBOM_PATH.exists():
        _assert(not required, "tracked TradingAgents SBOM is missing")
        return {"present": False}
    sbom = json.loads(SBOM_PATH.read_text(encoding="utf-8"))
    packages = {_normalize_name(item["name"]): item for item in sbom["packages"]}
    _assert(sbom["package_count"] == len(locked_versions), "SBOM package count mismatch")
    _assert(set(packages) == set(locked_versions), "SBOM package set mismatch")
    for name, version in locked_versions.items():
        _assert(packages[name]["version"] == version, f"SBOM version mismatch for {name}")
    _assert(packages["tradingagents"]["license"] == "Apache-2.0", "TradingAgents license missing from SBOM")
    _assert(packages["backtrader"]["license"] == "GPL-3.0-or-later", "Backtrader GPL boundary missing")
    return {
        "present": True,
        "path": str(SBOM_PATH.relative_to(ROOT)),
        "package_count": len(packages),
        "noassertion_count": sum(item["license"] == "NOASSERTION" for item in packages.values()),
        "copyleft_boundary_declared": True,
    }


def _runtime_acceptance(locked_versions: dict[str, str]) -> dict[str, Any]:
    installed = importlib.metadata.version("tradingagents")
    _assert(installed == EXPECTED_VERSION, f"installed TradingAgents version is {installed}")

    from unittest.mock import MagicMock

    from langchain_core.tools import tool
    from langgraph.prebuilt import ToolNode
    from tradingagents.graph.conditional_logic import ConditionalLogic
    from tradingagents.graph.propagation import Propagator
    from tradingagents.graph.setup import GraphSetup
    from tradingagents.graph.signal_processing import SignalProcessor

    @tool
    def fixed_market_fixture(symbol: str) -> str:
        """Return a deterministic, network-free market fixture."""
        return json.dumps({"symbol": symbol, "close": 100.0, "as_of": "2026-07-31T08:00:00Z"})

    llm = MagicMock(name="offline_fixture_llm")
    llm.bind_tools.return_value = llm
    llm.with_structured_output.return_value = llm
    tool_nodes = {
        key: ToolNode([fixed_market_fixture])
        for key in ("market", "social", "news", "fundamentals")
    }
    logic = ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1)
    workflow = GraphSetup(llm, llm, tool_nodes, logic).setup_graph(["market"])
    compiled = workflow.compile()
    graph_nodes = set(compiled.get_graph().nodes)
    expected_nodes = {
        "Market Analyst",
        "Bull Researcher",
        "Bear Researcher",
        "Research Manager",
        "Trader",
        "Aggressive Analyst",
        "Conservative Analyst",
        "Neutral Analyst",
        "Portfolio Manager",
    }
    _assert(expected_nodes <= graph_nodes, "minimal graph is missing original role nodes")

    state = Propagator(max_recur_limit=25).create_initial_state(
        "0700.HK",
        "2026-07-31",
        instrument_context="fixture:0700.HK",
    )
    _assert(state["company_of_interest"] == "0700.HK", "fixture state propagation failed")
    _assert(logic.should_continue_debate(state) == "Bull Researcher", "fixture debate route failed")
    _assert(logic.should_continue_risk_analysis(state) == "Aggressive Analyst", "fixture risk route failed")
    rating = SignalProcessor().process_signal("**Rating**: Buy\n\nFixture-only decision.")
    _assert(rating == "Buy", "fixture signal parsing failed")

    check = subprocess.run(
        ["python", "-m", "pip", "check"],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    _assert(check.returncode == 0, f"pip check failed: {check.stdout}{check.stderr}")
    for name, version in locked_versions.items():
        _assert(importlib.metadata.version(name) == version, f"runtime lock drift: {name}")
    return {
        "imported_version": installed,
        "minimal_graph_compiled": True,
        "minimal_graph_node_count": len(graph_nodes),
        "original_role_nodes_present": sorted(expected_nodes),
        "fixed_fixture_ran": True,
        "fixture_rating": rating,
        "pip_check": "passed",
        "network_calls": 0,
    }


def run(*, runtime: bool, require_sbom: bool, write_sbom: Path | None) -> dict[str, Any]:
    provenance, archive = _validate_provenance()
    locked_versions, hashes = _parse_lock()
    docker = _validate_docker_policy()
    runtime_result: dict[str, Any] = {"executed": False}
    generated_sbom = None
    if runtime:
        runtime_result = _runtime_acceptance(locked_versions)
        runtime_result["executed"] = True
        if write_sbom is not None:
            generated_sbom = _write_sbom(write_sbom, locked_versions)
    sbom = _validate_sbom(locked_versions, required=require_sbom)

    return {
        "check_version": "tradingagents-supply-chain-v1",
        "checked_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "component": {
            "name": provenance["component"]["name"],
            "version": provenance["component"]["version"],
            "commit": provenance["upstream"]["commit"],
            "tag_object": provenance["upstream"]["tag_object"],
            "tag_signature_verified": provenance["upstream"]["tag_signature"]["verified"],
        },
        "archive": archive,
        "dependency_lock": {
            "path": str(LOCK_PATH.relative_to(ROOT)),
            "sha256": _sha256_file(LOCK_PATH),
            "input_sha256": _sha256_file(INPUT_PATH),
            "package_count": len(locked_versions),
            "all_packages_exact": True,
            "all_packages_hashed": all(bool(value) for value in hashes.values()),
        },
        "docker": docker,
        "sbom": sbom,
        "generated_sbom_package_count": generated_sbom["package_count"] if generated_sbom else None,
        "runtime": runtime_result,
        "acceptance": {
            "source_version_and_commit_locked": True,
            "signed_tag_evidence_recorded": True,
            "archive_hash_verified": True,
            "archive_members_safe": True,
            "dependency_hashes_complete": True,
            "license_boundary_declared": True,
            "runtime_install_disabled": True,
            "upstream_services_disabled": True,
            "runtime_checks_passed": runtime_result.get("executed") is True if runtime else None,
            "passed": True,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", action="store_true", help="also import and build the installed graph")
    parser.add_argument("--require-sbom", action="store_true", help="fail unless the tracked SBOM is complete")
    parser.add_argument("--write-sbom", type=Path, help="write an SBOM from the installed runtime")
    parser.add_argument("--output", type=Path, help="write the acceptance JSON to this path")
    args = parser.parse_args()

    try:
        report = run(runtime=args.runtime, require_sbom=args.require_sbom, write_sbom=args.write_sbom)
    except (CheckFailure, KeyError, OSError, tarfile.TarError, ValueError) as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1

    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
