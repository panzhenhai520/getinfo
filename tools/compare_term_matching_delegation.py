# -*- coding: utf-8 -*-
"""Prove the delegation in financial_evidence.py changed no test outcome.

Temporarily restores the original inline implementations, runs the affected
test selection, then puts the delegated version back. Touches only
financial_evidence.py, and restores it on every path.
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

# (delegated form, original form) applied in order to rebuild the old file.
REVERSALS = (
    (
        "import sqlite3\nfrom dataclasses import dataclass\n",
        "import sqlite3\nimport unicodedata\nfrom dataclasses import dataclass\n",
    ),
    (
        "\nfrom term_matching import normalize_text, term_occurs_in_normalized\n",
        "\n",
    ),
    (
        "def _normalize_text(value: object) -> str:\n"
        "    return normalize_text(value)\n",
        'def _normalize_text(value: object) -> str:\n'
        '    return unicodedata.normalize("NFKC", str(value or "")).casefold()\n',
    ),
    (
        'def _term_occurs(term: str, haystack: str) -> bool:\n'
        '    """Whole-word for Latin identifier terms, substring otherwise.\n'
        "\n"
        "    Delegates to ``term_matching`` so the project keyword gate and the article\n"
        "    keyword filter cannot drift apart; ``haystack`` is expected to be\n"
        "    normalised already by the caller.\n"
        '    """\n'
        "\n"
        "    return term_occurs_in_normalized(normalize_text(term), haystack)\n",
        "def _term_occurs(term: str, haystack: str) -> bool:\n"
        "    normalized = _normalize_text(term).strip()\n"
        "    if not normalized:\n"
        "        return False\n"
        '    if normalized.isascii() and re.fullmatch(r"[a-z0-9._^:-]+", normalized):\n'
        "        return bool(\n"
        "            re.search(\n"
        '                rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])",\n'
        "                haystack,\n"
        "            )\n"
        "        )\n"
        "    return normalized in haystack\n",
    ),
)


def selection() -> list[str]:
    pattern = re.compile(r"keyword|evidence|intel|feed|dashboard|industry")
    return [str(path) for path in sorted((ROOT / "tests").glob("*.py"))
            if pattern.search(path.name)]


def run(label: str) -> tuple[int, int]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *selection(), "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True,
    )
    passed = re.search(r"(\d+) passed", result.stdout)
    failed = re.search(r"(\d+) failed", result.stdout)
    counts = (int(passed.group(1)) if passed else 0,
              int(failed.group(1)) if failed else 0)
    print(f"{label}: {counts[0]} passed / {counts[1]} failed")
    return counts


def main() -> int:
    original = TARGET.read_text(encoding="utf-8")
    shutil.copy2(TARGET, BACKUP)
    try:
        pre = original
        for delegated, old in REVERSALS:
            if delegated not in pre:
                print(f"could not find the delegated form to revert:\n{delegated[:120]!r}")
                return 2
            pre = pre.replace(delegated, old, 1)

        TARGET.write_text(pre, encoding="utf-8")
        before = run("inline-original")
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
    print(f"DIFFERENT: inline={before} delegated={after}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
