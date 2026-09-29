#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""One canonical rule for deciding whether a term occurs in a text.

The rule has to differ by script, and getting that wrong is how a short Latin
keyword starts matching inside unrelated words: ``anc`` is a substring of both
``Finance`` and ``guidance``, so plain ``in`` matching admits an entire English
finance article for a keyword that never appears in it.

* **Latin-script terms** are matched on word boundaries, so ``anc`` matches
  ``anc``, ``ANC`` or ``(anc)`` but not ``Finance``. Boundaries only consider
  ASCII letters and digits, which keeps ``AI`` matching ``AI芯片`` while still
  refusing ``AIGC``.
* **Every other term** (CJK, mixed, multi-word phrases) is matched as a plain
  substring, because Chinese text has no word delimiters and a phrase such as
  ``family office`` should still match ``family offices``.

Text and terms are compared after NFKC normalisation and case folding, so
full-width punctuation and letter case do not decide a match.

This mirrors the rule ``financial_evidence`` already applied to the project
keyword gate; that module now delegates here so the two paths cannot diverge.
"""

from __future__ import annotations

import re
import unicodedata

#: ASCII terms shaped like an identifier get word-boundary matching. Anything
#: with a space or non-ASCII character falls through to substring matching.
_IDENTIFIER_TERM_RE = re.compile(r"[a-z0-9._^:-]+")

#: A term may not be preceded or followed by an ASCII letter or digit. CJK
#: neighbours are allowed on purpose: ``AI`` must still match ``AI芯片``.
_LEFT_GUARD = r"(?<![a-z0-9])"
_RIGHT_GUARD = r"(?![a-z0-9])"


def normalize_text(value: object) -> str:
    """NFKC-normalise and case-fold ``value`` for comparison."""

    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def is_identifier_term(term: str) -> bool:
    """Whether ``term`` is an ASCII identifier-like term needing boundaries."""

    return term.isascii() and bool(_IDENTIFIER_TERM_RE.fullmatch(term))


def term_pattern(term: str) -> re.Pattern[str] | None:
    """Compiled whole-word pattern for an identifier term, else ``None``."""

    if not is_identifier_term(term):
        return None
    return re.compile(_LEFT_GUARD + re.escape(term) + _RIGHT_GUARD)


def term_occurs(term: object, haystack: object) -> bool:
    """Whether ``term`` occurs in ``haystack`` under the canonical rule.

    ``haystack`` is normalised here, so callers may pass raw text; normalising
    a large document once per term is wasteful, so prefer
    :func:`term_occurs_in_normalized` in a loop.
    """

    return term_occurs_in_normalized(normalize_text(term), normalize_text(haystack))


def term_occurs_in_normalized(normalized_term: str, normalized_haystack: str) -> bool:
    """Same as :func:`term_occurs` but for an already-normalised haystack."""

    term = str(normalized_term or "").strip()
    if not term:
        return False
    pattern = term_pattern(term)
    if pattern is None:
        return term in normalized_haystack
    return bool(pattern.search(normalized_haystack))


__all__ = [
    "is_identifier_term",
    "normalize_text",
    "term_occurs",
    "term_occurs_in_normalized",
    "term_pattern",
]
