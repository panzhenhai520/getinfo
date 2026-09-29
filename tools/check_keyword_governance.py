#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Acceptance checks for keyword governance normalization."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from keyword_governance import (
    KeywordGovernance,
    format_keyword_text,
    parse_keyword_text,
)


def build_connection():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE keyword_canonical_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_keyword TEXT NOT NULL,
            canonical_keyword TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
        )
    """)
    cur.execute("""
        CREATE TABLE keyword_blocklist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
        )
    """)
    cur.executemany(
        "INSERT INTO keyword_canonical_rules(source_keyword, canonical_keyword, status) VALUES (?, ?, ?)",
        [
            ('family offices', 'Family Office', 'active'),
            ('Family office', 'Family Office', 'active'),
            ('legacy disabled', 'Disabled Target', 'disabled'),
        ],
    )
    cur.executemany(
        "INSERT INTO keyword_blocklist(keyword, status) VALUES (?, ?)",
        [
            ('C', 'active'),
            ('blocked target', 'active'),
            ('inactive blocked', 'disabled'),
        ],
    )
    conn.commit()
    return conn


def main():
    governance = KeywordGovernance(build_connection())

    assert governance.normalize_keyword(' family offices ') == 'Family Office'
    assert governance.normalize_keyword('Family Offices') == 'Family Office'
    assert governance.normalize_keyword_list(['family offices', 'Family Office']) == ['Family Office']
    assert governance.apply_keyword_rules_to_task_keywords('A,C,B') == ['A', 'B']
    assert governance.apply_keyword_rules_to_task_keyword_text('A,C,B') == 'A,B'
    assert governance.normalize_keyword('legacy disabled') == 'legacy disabled'
    assert governance.normalize_keyword('inactive blocked') == 'inactive blocked'
    assert governance.normalize_keyword('blocked target') == ''
    assert governance.normalize_keyword_list('') == []
    assert parse_keyword_text('[文]family offices, [标]A；B\nC') == ['family offices', 'A', 'B', 'C']
    assert format_keyword_text([' A ', 'a', '[文]B']) == 'A,B'

    print('keyword_governance checks passed')


if __name__ == '__main__':
    main()
