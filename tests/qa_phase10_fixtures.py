#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 10 测试公用素材（**不是测试模块**：文件名不以 test_ 开头，pytest 不收集）。

全部走**真实链路**构造：Phase 02 的证据标注、Phase 03 的核验结论、Phase 09 的写门与仓储 ——
测试里不手写 `metadata.evidence_layer.verification`，否则测的就不是真正的接线。
Phase 10 只额外提供"记忆条目 / 记忆对 / 选择器"的构造器。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_memory as memory  # noqa: E402
import qa_memory_revalidation as mr  # noqa: E402

import qa_phase09_fixtures as fx09  # noqa: E402

NOW = "2026-10-11T00:00:00Z"
LATER = "2026-10-12T00:00:00Z"
SCOPE = {"owner_user_id": "p10", "session_id": "s10", "industry_pack_id": "auto"}

SUPPORTED_CLAIM = fx09.CLAIM_TEXT
SUPPORTED_CLAIM_2 = "香港家族办公室税收优惠政策明确给予合资格基金管理人利得税宽免"
NEGATED_CLAIM = "香港家族办公室税收优惠政策不予给予合资格基金管理人利得税宽免"
OTHER_CLAIM = "香港家族办公室的合资格交易需要满足实质经营要求"

temp_store = fx09.temp_store
store_for = fx09.store_for
verified_evidence = fx09.verified_evidence
annotated_evidence = fx09.annotated_evidence
evidence_item = fx09.evidence_item
graph = fx09.graph


def memory_item(content=SUPPORTED_CLAIM, *, memory_type="VERIFIED_CLAIM", freshness="LONG",
                status="ACTIVE", confidence=0.8, scope="PATIENT_LONGITUDINAL",
                valid_from="2026-04-01", valid_until="", last_verified_at="2026-10-05T00:00:00Z",
                created_at="2026-10-05T00:00:00Z", evidence_refs=("article:1",),
                entity_ids=(), claim_type="background", memory_id="", **overrides):
    """一条**完整形态**的记忆条目（字段口径与 `qa_memory.build_memory_item()` 一致）。

    默认 `freshness="LONG"` + `claim_type="background"`：高危与时效档两条规则**都不命中**，
    闸门走的是"年龄 + 证据绑定"规则 —— 单测里要的就是"只改一个变量看一个规则"。
    """
    scope_ids = dict(SCOPE)
    identifier = memory_id or memory.memory_id_for(
        scope=scope, memory_type=memory_type, canonical_content=content, **scope_ids)
    item = {
        "memory_id": identifier, "memory_type": memory_type, "canonical_content": content,
        "content_fingerprint": memory.content_fingerprint(content), "confidence": confidence,
        "freshness_class": freshness, "valid_from": valid_from, "valid_until": valid_until,
        "last_verified_at": last_verified_at, "status": status, "scope": scope,
        "scope_key": memory.scope_key(scope, **scope_ids), "owner_user_id": SCOPE["owner_user_id"],
        "session_id": SCOPE["session_id"], "industry_pack_id": SCOPE["industry_pack_id"],
        "entity_ids": list(entity_ids), "source_evidence_ids": list(evidence_refs),
        "evidence_refs": list(evidence_refs), "superseded_by": "", "reuse_count": 0,
        "recall_count": 0, "version": 1, "decay_score": 0.5, "created_at": created_at,
        "metadata": {"claim_type": claim_type, "valid_from": valid_from},
    }
    item.update(overrides)
    return item


def write_items(store, items):
    """把素材落进隔离 sqlite（`save_memory_item` 会连实体链接一起写）。"""
    written = []
    for item in items:
        result = store.save_memory_item(item)
        assert not result.get("error"), result
        written.append(result)
    return written


def seeded_store(*, graph_payload=None, content=SUPPORTED_CLAIM, **overrides):
    """一条已落库的记忆（默认绑 article:1 的已验证证据）。"""
    item = memory_item(content, **overrides)
    return item


def memory_with_evidence(store, content=SUPPORTED_CLAIM, *, evidence_ref="article:1", **overrides):
    """落一条记忆 + 一条**真实核验过的**证据绑定行（走 Phase 02/03 的链路）。"""
    item = memory_item(content, evidence_refs=(evidence_ref,), **overrides)
    store.save_memory_item(item)
    evidence = verified_evidence(evidence_ref, claim_text=content)
    link = memory.evidence_link_row(evidence, {"verdict": "SUPPORTED", "evidence_score": 0.8},
                                    run_id="run-p10", stage="level1_retrieval",
                                    corpus_version="corpus-p10")
    store.link_memory_evidence(item["memory_id"], [link])
    return item


def contradiction_pair(store, *, freshness="LONG", status="ACTIVE", valid_from="2026-04-01",
                       newer_valid_from="2026-09-01", entity_key="hk_family_office"):
    """一对"同一主体、否定极性相反"的记忆（§11 的 M1 支持 C / M2 反驳 C）。

    两条都挂同一实体键（走 `save_memory_item` 的 `entity_ids`），所以配对是**实体共享**成立；
    M2 的 `valid_from` 更晚 → 时间裁决会判 NEWER_VERSION_PRECEDES（取代路径）。
    """
    left = memory_item(SUPPORTED_CLAIM, freshness=freshness, status=status,
                       valid_from=valid_from, entity_ids=[entity_key], memory_id="MEM-left")
    right = memory_item(NEGATED_CLAIM, freshness=freshness, status=status,
                        valid_from=newer_valid_from, entity_ids=[entity_key],
                        memory_id="MEM-right")
    write_items(store, [left, right])
    return left, right


def load(store, memory_id):
    rows = store.load_memory_items(memory_ids=[memory_id], include_all_scopes=True, limit=1)
    return rows[0] if rows else {}


def links_of(store, memory_id):
    return store.memory_evidence(memory_ids=[memory_id])


def version_rows(store, memory_id):
    rows = store.database.connection.execute(
        "SELECT version, change, status, reason FROM memory_version WHERE memory_id=? "
        "ORDER BY version", (memory_id,)).fetchall()
    return [dict(row) if hasattr(row, "keys") else {"version": row[0]} for row in rows]


def revalidation_ids(store):
    rows = store.database.connection.execute(
        "SELECT validation_id, outcome, reason FROM memory_validation ORDER BY id").fetchall()
    return [dict(row) if hasattr(row, "keys") else {} for row in rows]


def contradiction_ids(store):
    rows = store.database.connection.execute(
        "SELECT contradiction_id, resolution, reason_code, status_action "
        "FROM memory_contradiction ORDER BY id").fetchall()
    return [dict(row) if hasattr(row, "keys") else {} for row in rows]


def relation_rows(store, memory_id, *, relation=""):
    return store.memory_relations([memory_id], relations=[relation] if relation else ())
