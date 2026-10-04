#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lazy runtime wiring used by the durable QA worker lane."""

from __future__ import annotations

from qa_orchestrator import QaOrchestrator
from qa_storage import QaStore


_orchestrator_override = None


def set_qa_orchestrator(orchestrator) -> None:
    global _orchestrator_override
    _orchestrator_override = orchestrator


def get_qa_orchestrator() -> QaOrchestrator:
    if _orchestrator_override is not None:
        return _orchestrator_override
    from qa_pipeline import build_qa_stage_handlers
    from sqlite_database import sqlite_db

    return QaOrchestrator(QaStore(sqlite_db), build_qa_stage_handlers())


__all__ = ["get_qa_orchestrator", "set_qa_orchestrator"]
