#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Keyword normalization and blocklist rules for crawl governance."""

from __future__ import annotations

import json
import re
from typing import Dict, Iterable, List, Optional


_LOCATION_PREFIX_RE = re.compile(r'^\s*\[(?:标|標|文)\]\s*')


def clean_keyword(keyword) -> str:
    """Normalize whitespace and remove storage-only location markers."""
    text = str(keyword or '').strip()
    text = _LOCATION_PREFIX_RE.sub('', text).strip()
    text = re.sub(r'\s+', ' ', text)
    return text


def keyword_key(keyword) -> str:
    """Case-insensitive lookup key for governance rules."""
    return clean_keyword(keyword).casefold()


def parse_keyword_text(value) -> List[str]:
    """Parse keyword text/list/json into ordered unique keywords."""
    if not value:
        return []

    parsed: List[str] = []
    seen = set()

    def add(item) -> None:
        keyword = clean_keyword(item)
        key = keyword_key(keyword)
        if keyword and key not in seen:
            seen.add(key)
            parsed.append(keyword)

    if isinstance(value, (list, tuple, set)):
        for item in value:
            add(item)
        return parsed

    if isinstance(value, dict):
        for key in ('title_keywords', 'content_keywords', 'all_keywords', 'keywords'):
            for item in value.get(key) or []:
                add(item)
        return parsed

    raw = str(value or '').strip()
    if not raw:
        return []

    if raw.startswith('{') or raw.startswith('['):
        try:
            return parse_keyword_text(json.loads(raw))
        except Exception:
            pass

    for item in raw.replace('，', ',').replace('；', ';').split(','):
        for token in item.replace(';', '\n').splitlines():
            add(token)

    return parsed


def format_keyword_text(keywords: Iterable[str]) -> str:
    """Serialize keywords in the storage format used by task fields."""
    return ','.join(normalize_keyword_list_static(keywords))


def normalize_keyword_list_static(keywords: Iterable[str]) -> List[str]:
    """Clean and de-duplicate keywords without consulting database rules."""
    output: List[str] = []
    seen = set()
    for keyword in keywords or []:
        clean = clean_keyword(keyword)
        key = keyword_key(clean)
        if clean and key not in seen:
            seen.add(key)
            output.append(clean)
    return output


class KeywordGovernance:
    """Apply persisted keyword canonicalization and blocklist rules."""

    def __init__(self, connection=None):
        self.connection = connection
        self._rules: Optional[Dict[str, str]] = None
        self._blocklist: Optional[set] = None

    def _get_connection(self):
        if self.connection is not None:
            return self.connection
        from sqlite_database import sqlite_db
        sqlite_db._ensure_connection()
        return sqlite_db.connection

    def reload(self) -> None:
        self._rules = None
        self._blocklist = None

    def _load_rules(self) -> Dict[str, str]:
        if self._rules is not None:
            return self._rules

        rules: Dict[str, str] = {}
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT source_keyword, canonical_keyword
                FROM keyword_canonical_rules
                WHERE status = 'active'
            """)
            for row in cursor.fetchall():
                source = clean_keyword(row['source_keyword'] if hasattr(row, 'keys') else row[0])
                canonical = clean_keyword(row['canonical_keyword'] if hasattr(row, 'keys') else row[1])
                if source and canonical:
                    rules[keyword_key(source)] = canonical
        except Exception:
            rules = {}
        finally:
            cursor.close()

        self._rules = rules
        return rules

    def _load_blocklist(self) -> set:
        if self._blocklist is not None:
            return self._blocklist

        blocked = set()
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT keyword
                FROM keyword_blocklist
                WHERE status = 'active'
            """)
            for row in cursor.fetchall():
                value = row['keyword'] if hasattr(row, 'keys') else row[0]
                key = keyword_key(value)
                if key:
                    blocked.add(key)
        except Exception:
            blocked = set()
        finally:
            cursor.close()

        self._blocklist = blocked
        return blocked

    def normalize_keyword(self, keyword) -> str:
        """Return canonical keyword, or empty string when blocked/blank."""
        clean = clean_keyword(keyword)
        if not clean:
            return ''

        rules = self._load_rules()
        blocked = self._load_blocklist()

        current = clean
        visited = set()
        for _ in range(10):
            key = keyword_key(current)
            if key in blocked:
                return ''
            if key in visited or key not in rules:
                break
            visited.add(key)
            current = rules[key]

        if keyword_key(current) in blocked:
            return ''
        return current

    def is_keyword_blocked(self, keyword) -> bool:
        clean = clean_keyword(keyword)
        if not clean:
            return False
        return not self.normalize_keyword(clean)

    def normalize_keyword_list(self, keywords) -> List[str]:
        output: List[str] = []
        seen = set()
        for keyword in parse_keyword_text(keywords):
            normalized = self.normalize_keyword(keyword)
            key = keyword_key(normalized)
            if normalized and key not in seen:
                seen.add(key)
                output.append(normalized)
        return output

    def apply_keyword_rules_to_task_keywords(self, keywords) -> List[str]:
        return self.normalize_keyword_list(keywords)

    def apply_keyword_rules_to_task_keyword_text(self, keywords) -> str:
        return ','.join(self.apply_keyword_rules_to_task_keywords(keywords))


_default_governance: Optional[KeywordGovernance] = None


def get_keyword_governance() -> KeywordGovernance:
    global _default_governance
    if _default_governance is None:
        _default_governance = KeywordGovernance()
    return _default_governance


def normalize_keyword(keyword) -> str:
    return get_keyword_governance().normalize_keyword(keyword)


def normalize_keyword_list(keywords) -> List[str]:
    return get_keyword_governance().normalize_keyword_list(keywords)


def is_keyword_blocked(keyword) -> bool:
    return get_keyword_governance().is_keyword_blocked(keyword)


def apply_keyword_rules_to_task_keywords(keywords) -> List[str]:
    return get_keyword_governance().apply_keyword_rules_to_task_keywords(keywords)


def apply_keyword_rules_to_task_keyword_text(keywords) -> str:
    return get_keyword_governance().apply_keyword_rules_to_task_keyword_text(keywords)
