# -*- coding: utf-8 -*-
"""Prove the delegation in financial_evidence.py changed no test outcome.

Temporarily restores the original inline implementations, runs the affected
test selection, then puts the delegated version back. Read-only with respect to
everything except financial_evidence.py, which is restored either way.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "financial_evidence.py"
BACKUP = ROOT / ".financial_evidence.pre_compare.bak"

DELEGATED_IMPORT = "from intel_sources import canonicalize_source_url\nfrom term_matching import normalize_text, term_occurs_in_normalized\n"
ORIGINAL_IMPORT = "import unicodedata\nfrom dataclasses import dataclass\n"

DELEGATED_NORMALIZE = '''def _normalize_text(value: object) -> str:
    return normalize_text(value)
'''
ORIGINAL_NORMALIZE = '''def _normalize_text(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold()
'''

DELEGATED_TERM = '''def _term_occurs(term: str, haystack: str) -> bool:
    """Whole-word for Latin identifier terms, substring otherwise.

    Delegates to ``term_matching`` so the project keyword gate and the article
    keyword filter cannot drift apart; ``haystack`` is expected to be
    normalised already by the caller.
    """

    return term_occurs_in_normalized(normalize_text(term), haystack)
'''
ORIGINAL_TERM = '''def _term_occurs(term: str, haystack: str) -> bool:
    normalized = _normalize_text(term).strip()
    if not normalized:
        return False
    if normalized.isascii() and re.fullmatch(r"[a-z0-9._^:-]+", normalized):
        return bool(
            re.search(
                rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])",
                haystack,
            )
        )
    return normalized in haystack
'''


def selection() -> list[str]:
    pattern = re.compile(r"keyword|evidence|intel|feed|dashboard|industry")
    return [str(path) for path in sorted((ROOT / "tests").glob("*.py"))
            if pattern.search(path.name)]


def run(label: str) -> tuple[int, int]:
    files = selection()
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *files, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True,
    )
    tail = [line for line in result.stdout.splitlines()
            if line.startswith(("Tests:", "Test Suites:")) or " passed" in line or " failed" in line]
    summary = tail[-1] if tail else result.stdout.strip().splitlines()[-1:]
    print(f"{label}: {summary}")
    match = re.search(r"(\d+) failed", result.stdout)
    failed = int(match.group(1)) if match else 0
    match = re.search(r"(\d+) passed", result.stdout)
    passed = int(match.group(1)) if match else 0
    return passed, failed


def main() -> int:
    original = TARGET.read_text(encoding="utf-8")
    shutil.copy2(TARGET, BACKUP)
    try:
        pre = original
        for new, old in ((DELEGATED_IMPORT, ORIGINAL_IMPORT),
                         (DELEGATED_NORMALIZE, ORIGINAL_NORMALIZE),
                         (DELEGATED_TERM, ORIGINAL_TERM)):
            if new not in pre:
                print(f"could not find delegated form to revert:\n{new[:80]}")
                return 2
            pre = pre.replace(new, old, 1)
        pre = pre.replace("import sqlite3\n", "import sqlite3\n", 1)

        TARGET.write_text(pre, encoding="utf-8")
        before = run("inline-original ")
        TARGET.write_text(original, encoding="utf-8")
        after = run("delegated      ")
    finally:
        TARGET.write_text(original, encoding="utf-8")
        BACKUP.unlink(missing_ok=True)

    print()
    if before == after:
        print(f"IDENTICAL: {before[0]} passed / {before[1]} failed either way")
        print("=> the delegation changed no test outcome.")
        return 0
    print(f"DIFFERENT: {before} -> {after}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
