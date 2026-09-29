#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Capture configuration state without serializing credentials or URL secrets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


EXPLICIT_SECRET_KEYS = {
    "SECRET_KEY",
    "DEFAULT_ADMIN_PASSWORD",
    "REDIS_PASSWORD",
    "RAGFLOW_API_KEY",
    "SERPAPI_API_KEY",
}
SECRET_MARKERS = (
    "API_KEY",
    "ACCESS_KEY",
    "PRIVATE_KEY",
    "PASSWORD",
    "SECRET",
    "TOKEN",
    "CREDENTIAL",
    "COOKIE",
)
ENDPOINT_MARKERS = ("BASE_URL", "ENDPOINT", "_HOST", "_URL", "_URI")
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_env_file(path: Path) -> dict[str, str]:
    values = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    return values


def _is_secret_key(key: str) -> bool:
    upper = key.upper()
    return upper in EXPLICIT_SECRET_KEYS or any(
        marker in upper for marker in SECRET_MARKERS
    )


def _is_endpoint_key(key: str) -> bool:
    upper = key.upper()
    proxy_endpoint = "PROXY" in upper and not upper.endswith(
        ("_ENABLED", "_DEFAULT")
    )
    return (
        upper == "FLASK_HOST"
        or proxy_endpoint
        or any(marker in upper for marker in ENDPOINT_MARKERS)
    )


def _is_boolean_key(key: str, value: str) -> bool:
    normalized = str(value or "").strip().casefold()
    if normalized not in TRUE_VALUES | FALSE_VALUES:
        return False
    upper = key.upper()
    return (
        upper.endswith(("_ENABLED", "_DEBUG"))
        or upper.startswith("ENABLE_")
        or "_REQUIRE_" in upper
        or upper.startswith("REQUIRE_")
        or upper.endswith(
            (
                "_AUTO_PARSE",
                "_REUPLOAD_EXISTING",
                "_DATE_RANGE_PRIORITY",
                "_PREFILTER_CANDIDATE_DATES",
                "_USE_PROXY_DEFAULT",
            )
        )
    )


def _boolean_value(value: str) -> bool:
    return str(value).strip().casefold() in TRUE_VALUES


def _endpoint_summary(value: str) -> dict:
    raw = str(value or "").strip()
    if not raw:
        return {"configured": False, "scheme": "", "host": "", "port": None}
    candidate = raw if "://" in raw else f"//{raw}"
    parsed = urlsplit(candidate)
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    return {
        "configured": bool(host),
        "scheme": parsed.scheme.casefold(),
        "host": host.casefold(),
        "port": port,
    }


def _effective_value(
    key: str,
    dotenv_values: dict[str, str],
    environment: dict[str, str],
) -> tuple[str, str]:
    if key in environment:
        return str(environment[key]), "process_environment"
    if key in dotenv_values:
        return dotenv_values[key], "dotenv"
    return "", "unconfigured"


def _declared_config_keys(root: Path) -> set[str]:
    keys = set()
    pattern = re.compile(
        r"(?:os\.getenv|os\.environ\.get|_env_bool|_env_int|_env_float|_env_str)"
        r"\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]"
    )
    for relative in ("config.py", "config_management_api.py", "chat_api.py"):
        path = root / relative
        if path.is_file():
            keys.update(pattern.findall(path.read_text(encoding="utf-8")))
    return keys


def _chat_config_summary(path: Path) -> tuple[dict, list[str]]:
    if not path.is_file():
        return {"exists": False, "models": []}, []
    payload = json.loads(path.read_text(encoding="utf-8"))
    models = payload.get("models") if isinstance(payload, dict) else {}
    models = models if isinstance(models, dict) else {}
    secrets = []
    summaries = []
    for provider in sorted(models):
        item = models.get(provider)
        item = item if isinstance(item, dict) else {}
        api_key = str(item.get("api_key") or "").strip()
        if api_key:
            secrets.append(api_key)
        summaries.append(
            {
                "provider": str(provider),
                "model_id": str(item.get("model_id") or ""),
                "api_key_configured": bool(api_key),
                "use_proxy": bool(item.get("use_proxy", False)),
                "endpoint": _endpoint_summary(item.get("base_url") or ""),
            }
        )
    return (
        {
            "exists": True,
            "active_model": str(payload.get("active_model") or ""),
            "ragflow_kb_configured": bool(str(payload.get("ragflow_kb_id") or "").strip()),
            "models": summaries,
        },
        secrets,
    )


def _manifest_structure_safe(manifest: dict) -> bool:
    allowed_endpoint_fields = {"configured", "scheme", "host", "port"}
    for endpoint in manifest.get("endpoint_hosts", []):
        if set(endpoint.get("endpoint", {})) != allowed_endpoint_fields:
            return False
    for model in manifest.get("chat_model_configuration", {}).get("models", []):
        if set(model.get("endpoint", {})) != allowed_endpoint_fields:
            return False
        if "api_key" in model:
            return False
    return all("value" not in item for item in manifest.get("configuration_keys", []))


def capture_sanitized_config(
    repository: str | Path,
    *,
    env_path: str | Path = ".env",
    example_path: str | Path = ".env.example",
    chat_config_path: str | Path = "data/chat_config.json",
    environment: dict[str, str] | None = None,
) -> dict:
    root = Path(repository).expanduser().resolve()
    actual_env = Path(env_path)
    actual_env = actual_env if actual_env.is_absolute() else root / actual_env
    example = Path(example_path)
    example = example if example.is_absolute() else root / example
    chat_path = Path(chat_config_path)
    chat_path = chat_path if chat_path.is_absolute() else root / chat_path
    dotenv_values = _parse_env_file(actual_env)
    example_values = _parse_env_file(example)
    current_environment = dict(os.environ if environment is None else environment)
    keys = sorted(
        set(example_values) | set(dotenv_values) | _declared_config_keys(root)
    )

    configuration_keys = []
    boolean_settings = []
    endpoint_hosts = []
    raw_secrets = []
    for key in keys:
        value, source = _effective_value(key, dotenv_values, current_environment)
        secret = _is_secret_key(key)
        if source == "unconfigured" and not secret and key in example_values:
            value = example_values[key]
            source = "example_default"
        endpoint = _is_endpoint_key(key) and not secret
        configured = bool(str(value).strip())
        kind = "secret" if secret else "endpoint" if endpoint else "setting"
        configuration_keys.append(
            {
                "key": key,
                "kind": kind,
                "in_example": key in example_values,
                "configured": configured,
                "source": source,
            }
        )
        if secret and configured:
            raw_secrets.append(str(value))
        if not secret and _is_boolean_key(key, value):
            boolean_settings.append(
                {"key": key, "value": _boolean_value(value), "source": source}
            )
        if endpoint:
            endpoint_hosts.append(
                {"key": key, "source": source, "endpoint": _endpoint_summary(value)}
            )

    chat_summary, chat_secrets = _chat_config_summary(chat_path)
    raw_secrets.extend(chat_secrets)
    tracked_config_files = {}
    for relative in (".env.example", "config.py", "config_management_api.py", "chat_api.py"):
        path = root / relative
        if path.is_file():
            tracked_config_files[relative] = _sha256(path)
    actual_key_set_sha256 = hashlib.sha256(
        "\n".join(sorted(dotenv_values)).encode("utf-8")
    ).hexdigest()
    manifest = {
        "manifest_version": "sanitized-config-baseline-v1",
        "captured_at_utc": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "repository_root": str(root),
        "dotenv_present": actual_env.is_file(),
        "dotenv_values_committed": False,
        "dotenv_key_set_sha256": actual_key_set_sha256,
        "tracked_config_file_sha256": tracked_config_files,
        "configuration_keys": configuration_keys,
        "boolean_settings": boolean_settings,
        "endpoint_hosts": endpoint_hosts,
        "chat_model_configuration": chat_summary,
    }
    serialized = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    leaked = sorted(
        {secret for secret in raw_secrets if len(secret) >= 6 and secret in serialized}
    )
    manifest["acceptance"] = {
        "manifest_structure_safe": _manifest_structure_safe(manifest),
        "raw_secret_values_found": len(leaked),
        "raw_secret_values_absent": not leaked,
        "dotenv_values_committed": False,
    }
    manifest["acceptance"]["passed"] = all(
        (
            manifest["acceptance"]["manifest_structure_safe"],
            manifest["acceptance"]["raw_secret_values_absent"],
            not manifest["acceptance"]["dotenv_values_committed"],
        )
    )
    fingerprint_payload = dict(manifest)
    fingerprint_payload.pop("captured_at_utc", None)
    manifest["sanitized_configuration_sha256"] = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Capture secret-safe configuration state")
    parser.add_argument("--repository", default=".")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--example", default=".env.example")
    parser.add_argument("--chat-config", default="data/chat_config.json")
    parser.add_argument("--output", default="baseline/sanitized-config-manifest.json")
    args = parser.parse_args(argv)
    report = capture_sanitized_config(
        args.repository,
        env_path=args.env,
        example_path=args.example,
        chat_config_path=args.chat_config,
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
                "key_count": len(report["configuration_keys"]),
                "boolean_count": len(report["boolean_settings"]),
                "endpoint_count": len(report["endpoint_hosts"]),
                "chat_model_count": len(report["chat_model_configuration"].get("models", [])),
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
