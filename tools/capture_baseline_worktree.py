#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Capture and verify a secret-safe Git worktree baseline manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


RUNTIME_EXCLUSIONS = [
    ".env (secret configuration)",
    "data/ and crawler_articles.db (mutable runtime database/artifacts)",
    "auth_storage/ (sessions and authentication state)",
    "crawl_results/ and crawl_logs/ (runtime outputs)",
    "backups/ (separate recovery lifecycle)",
    "virtual environments, caches, editor and OS files from .gitignore",
]


def _git(root: Path, *args: str, binary: bool = False):
    result = subprocess.run(
        ["git", *args],
        cwd=str(root),
        check=True,
        capture_output=True,
        text=not binary,
    )
    return result.stdout


def _repository_root(path: str | Path) -> Path:
    candidate = Path(path).expanduser().resolve()
    root = _git(candidate, "rev-parse", "--show-toplevel").strip()
    return Path(root).resolve()


def _status_entries(root: Path, ignored_paths: set[str]) -> list[dict]:
    raw = _git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        binary=True,
    )
    records = [item for item in raw.split(b"\0") if item]
    result = []
    index = 0
    while index < len(records):
        record = records[index].decode("utf-8", "surrogateescape")
        code = record[:2]
        path = record[3:]
        old_path = ""
        if "R" in code or "C" in code:
            index += 1
            if index < len(records):
                old_path = records[index].decode("utf-8", "surrogateescape")
        normalized = path.replace(os.sep, "/")
        if normalized in ignored_paths:
            index += 1
            continue
        kind = (
            "untracked"
            if code == "??"
            else "deleted"
            if "D" in code
            else "renamed"
            if "R" in code
            else "modified"
        )
        result.append(
            {
                "code": code,
                "path": normalized,
                "old_path": old_path.replace(os.sep, "/"),
                "kind": kind,
                "disposition": "requires_review_before_baseline",
            }
        )
        index += 1
    return result


def _tracked_paths(root: Path) -> list[str]:
    raw = _git(root, "ls-files", "-z", binary=True)
    return sorted(
        item.decode("utf-8", "surrogateescape").replace(os.sep, "/")
        for item in raw.split(b"\0")
        if item
    )


def _file_hash(root: Path, relative_path: str) -> str:
    path = root / relative_path
    digest = hashlib.sha256()
    if path.is_symlink():
        digest.update(b"symlink\0")
        digest.update(os.readlink(path).encode("utf-8", "surrogateescape"))
    else:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def capture_manifest(
    repository: str | Path,
    *,
    output_path: str | Path = "",
    sample_size: int = 12,
) -> dict:
    root = _repository_root(repository)
    output = Path(output_path).expanduser().resolve() if output_path else None
    ignored_paths = set()
    if output:
        try:
            ignored_paths.add(output.relative_to(root).as_posix())
        except ValueError:
            pass
    head = _git(root, "rev-parse", "HEAD").strip()
    branch = _git(root, "branch", "--show-current").strip()
    statuses = _status_entries(root, ignored_paths)
    tracked = [path for path in _tracked_paths(root) if path not in ignored_paths]
    ranked = sorted(
        tracked,
        key=lambda path: hashlib.sha256(f"{head}\0{path}".encode("utf-8")).hexdigest(),
    )
    samples = [
        {"path": path, "sha256": _file_hash(root, path)}
        for path in ranked[: max(1, min(int(sample_size), 100))]
    ]
    changed_counts = {
        kind: sum(int(item["kind"] == kind) for item in statuses)
        for kind in ("modified", "deleted", "renamed", "untracked")
    }
    return {
        "manifest_version": "baseline-worktree-v1",
        "captured_at_utc": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "repository_root": str(root),
        "branch": branch,
        "head": head,
        "worktree_clean": not statuses,
        "status_entries": statuses,
        "status_counts": {**changed_counts, "total": len(statuses)},
        "diff_stat": _git(root, "diff", "--stat").strip(),
        "cached_diff_stat": _git(root, "diff", "--cached", "--stat").strip(),
        "tracked_file_count": len(tracked),
        "tracked_scope": "all Git-tracked files at the recorded HEAD",
        "sample_strategy": "lowest SHA-256 rank of HEAD + NUL + tracked path",
        "sample_hashes": samples,
        "runtime_exclusions": RUNTIME_EXCLUSIONS,
        "output_artifact_excluded_from_status": sorted(ignored_paths),
        "acceptance": {
            "all_current_changes_have_disposition": all(
                bool(item.get("disposition")) for item in statuses
            ),
            "no_unknown_worktree_files": not statuses,
            "sample_hashes_readable": len(samples) == min(len(tracked), max(1, min(int(sample_size), 100))),
        },
    }


def verify_manifest(manifest_path: str | Path) -> dict:
    path = Path(manifest_path).expanduser().resolve()
    saved = json.loads(path.read_text(encoding="utf-8"))
    current = capture_manifest(
        saved["repository_root"],
        output_path=path,
        sample_size=len(saved.get("sample_hashes") or []),
    )
    fields = (
        "branch",
        "head",
        "worktree_clean",
        "status_entries",
        "status_counts",
        "diff_stat",
        "cached_diff_stat",
        "tracked_file_count",
        "sample_hashes",
    )
    differences = {
        field: {"saved": saved.get(field), "current": current.get(field)}
        for field in fields
        if saved.get(field) != current.get(field)
    }
    return {
        "manifest": str(path),
        "valid": not differences,
        "differences": differences,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Capture or verify a Git worktree baseline")
    parser.add_argument("--repository", default=".")
    parser.add_argument("--output", default="baseline/worktree-manifest.json")
    parser.add_argument("--sample-size", type=int, default=12)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    if args.verify:
        report = verify_manifest(args.output)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report["valid"] else 2
    report = capture_manifest(
        args.repository,
        output_path=args.output,
        sample_size=args.sample_size,
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
                "head": report["head"],
                "worktree_clean": report["worktree_clean"],
                "tracked_file_count": report["tracked_file_count"],
                "status_counts": report["status_counts"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["worktree_clean"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
