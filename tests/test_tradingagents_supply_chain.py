import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROVENANCE = ROOT / "vendor/tradingagents/PROVENANCE.json"
ARCHIVE = ROOT / "vendor/tradingagents/TradingAgents-0.3.1-01477f9.tar.gz"
SBOM = ROOT / "architecture/tradingagents-sbom.json"
ACCEPTANCE = ROOT / "architecture/tradingagents-supply-chain-acceptance.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_offline_static_supply_chain_gate_passes():
    completed = subprocess.run(
        [
            sys.executable,
            "tools/check_tradingagents_supply_chain.py",
            "--require-sbom",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["acceptance"]["passed"] is True
    assert report["dependency_lock"]["all_packages_hashed"] is True


def test_provenance_pins_signed_release_and_archive_hash():
    provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    assert provenance["component"]["version"] == "0.3.1"
    assert provenance["upstream"]["commit"] == "01477f9afb7a47b849ed4c9259d3a9a4738d9fda"
    assert provenance["upstream"]["tag_object"] == "5a3d1b51d339202d03c4b57c1a1012f69376495f"
    assert provenance["upstream"]["tag_signature"]["verified"] is True
    assert _sha256(ARCHIVE) == provenance["vendored_source"]["sha256"]


def test_runtime_acceptance_imports_and_builds_original_role_graph():
    acceptance = json.loads(ACCEPTANCE.read_text(encoding="utf-8"))
    runtime = acceptance["runtime"]
    assert acceptance["acceptance"]["passed"] is True
    assert runtime["imported_version"] == "0.3.1"
    assert runtime["minimal_graph_compiled"] is True
    assert runtime["fixed_fixture_ran"] is True
    assert runtime["pip_check"] == "passed"
    assert runtime["network_calls"] == 0
    assert {
        "Bull Researcher",
        "Bear Researcher",
        "Trader",
        "Aggressive Analyst",
        "Conservative Analyst",
        "Neutral Analyst",
        "Portfolio Manager",
    } <= set(runtime["original_role_nodes_present"])


def test_sbom_covers_entire_lock_and_declares_copyleft_boundary():
    sbom = json.loads(SBOM.read_text(encoding="utf-8"))
    packages = {item["name"]: item for item in sbom["packages"]}
    assert sbom["package_count"] == 109 == len(packages)
    assert all(item["license"] != "NOASSERTION" for item in packages.values())
    assert packages["tradingagents"]["license"] == "Apache-2.0"
    assert packages["backtrader"]["license"] == "GPL-3.0-or-later"
    assert sbom["license_boundary"]["redistribution_review_required"] is True


def test_runtime_never_fetches_or_starts_upstream_stack():
    provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    policy = provenance["integration_policy"]
    assert policy["runtime_git_clone"] is False
    assert policy["upstream_cli_enabled"] is False
    assert policy["upstream_web_service_enabled"] is False
    assert policy["upstream_ollama_service_enabled"] is False
    assert policy["upstream_redis_service_enabled"] is False
    assert policy["upstream_database_service_enabled"] is False


def test_offline_rebuild_file_has_no_network_or_package_install_step():
    dockerfile = (ROOT / "Dockerfile.offline").read_text(encoding="utf-8").lower()
    assert "apt-get" not in dockerfile
    assert "pip install" not in dockerfile
    assert "curl " not in dockerfile
    assert "git clone" not in dockerfile
    assert "check_tradingagents_supply_chain.py --runtime" in dockerfile


def test_normal_image_keeps_existing_application_entrypoint():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'EXPOSE 8003' in dockerfile
    assert 'ENTRYPOINT ["/app/docker-entrypoint.sh"]' in dockerfile
    assert 'CMD ["python", "start_with_schedule.py"]' in dockerfile
    assert 'CMD ["tradingagents"' not in dockerfile
