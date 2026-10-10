#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 09 测试公用素材（**不是测试模块**：文件名不以 test_ 开头，pytest 不收集）。

素材全部走**真实链路**构造：Phase 02 的 `annotate_evidence()` 出证据层对象，
Phase 03 的 `verify_evidence_batch()` 出核验结论（verdict）——测试里不手写
`metadata.evidence_layer.verification`，否则测的就不是真正的接线。
"""
from __future__ import annotations

import contextlib
import os
import tempfile

from qa_evidence import annotate_evidence
from qa_verifier import verify_evidence_batch

QUESTION = "香港家族办公室税收优惠政策对内地高净值客户有什么影响？"
CLAIM_TEXT = "香港家族办公室税收优惠政策对合资格基金管理人给予利得税宽免"
SCOPE = {"owner_user_id": "p09", "session_id": "s1", "industry_pack_id": "auto"}
EVIDENCE_TEXT = ("香港家族办公室税收优惠政策明确：对符合条件的管理人，源自合资格交易的"
                 "应评税利润可获利得税宽免，政策自 2026 年 4 月 1 日起生效。")
UNVERIFIED_EVIDENCE_TEXT = "某论坛帖子讨论过家族办公室的税务安排，但没有给出任何正式依据。"


def evidence_item(evidence_ref="article:1", *, text=EVIDENCE_TEXT,
                  title="香港家族办公室税收优惠政策解读", relation="supports",
                  authority=60, article_id=None, source_url="", published_at="2026-10-09"):
    return {
        "evidence_ref": evidence_ref, "source_type": "article", "title": title,
        "source_url": source_url or ("https://example.com/%s" % evidence_ref.replace(":", "-")),
        "article_id": int(article_id if article_id is not None
                          else str(evidence_ref).split(":")[-1] or 1),
        "content_excerpt": text, "published_at": published_at, "authority_level": authority,
        "score": 30.0, "retrieval_method": "keyword", "match_reason": "标题命中：家族办公室",
        "relationship": relation, "metadata": {"matched_keywords": ["家族办公室", "税收优惠"]},
    }


def annotated_evidence(evidence_ref="article:1", **kwargs):
    """走 Phase 02 的标注（最小 span / 来源指纹 / 指纹都在 metadata.evidence_layer 里）。"""
    return annotate_evidence(evidence_item(evidence_ref, **kwargs),
                             terms=["家族办公室", "税收优惠", "利得税宽免"],
                             run_id="run-p09", stage="level1_retrieval", route="keyword",
                             corpus_version="corpus-p09")


def verified_evidence(evidence_ref="article:1", *, claim_text=CLAIM_TEXT, **kwargs):
    """走 Phase 03 的核验（verdict 由规则判定，不是手写）。"""
    item = annotated_evidence(evidence_ref, **kwargs)
    kept, _audit = verify_evidence_batch(
        [item], claim_text=claim_text, terms=["家族办公室", "税收优惠", "利得税宽免"],
        gate="all")
    return kept[0]


def claim_node(claim_id="c1", *, text=CLAIM_TEXT, claim_type="policy", confidence=0.82,
               refs=("article:1",), plan_only=False, status="unverified"):
    return {
        "claim_id": claim_id, "canonical_id": claim_id, "plan_only": plan_only,
        "text": text, "verification_status": status,
        "claim": {"claim_id": claim_id, "text": text, "claim_type": claim_type,
                  "confidence": confidence, "valid_from": "2026-04-01", "valid_to": None,
                  "scope": ["家族办公室"], "evidence_refs": list(refs),
                  "needs_verification": True, "verification_status": status},
    }


def graph(*, refs=("article:1",), claim_id="c1", claim_text=CLAIM_TEXT,
          claim_type="policy", plan_only=False, relation="SUPPORTS",
          evidence_text=EVIDENCE_TEXT, status="supported"):
    """一张最小证据图：claim 节点 + 已核验证据 + 一条图级关系边。"""
    evidence = [verified_evidence(ref, claim_text=claim_text, text=evidence_text)
                for ref in refs]
    edges = []
    for ref in refs:
        edges.append({
            "edge_id": "edge:%s:%s" % (claim_id, ref), "kind": "claim-evidence",
            "src": "claim:%s" % claim_id, "dst": "evidence:%s" % ref,
            "graph_relation": relation, "status": relation, "strength": 0.7,
            "claim_id": claim_id, "evidence_ref": ref,
            "relationship": relation.casefold(), "relevance_score": 0.6,
            "metadata": {"verified": True}, "scope": [],
        })
    return {
        "version": "qa-evidence-graph-v1", "claims": [claim_node(claim_id, text=claim_text,
                                                                claim_type=claim_type,
                                                                refs=refs, plan_only=plan_only)],
        "evidence": evidence, "edges": edges, "conflicts": [], "stats": {},
        "verification": {"stats": {"claims": 1, "confirmed": 1 if status == "supported" else 0}},
    }


def unverified_graph(*, refs=("article:1",), claim_id="c1"):
    """只有"提到"关系的图：写门必须拒收（MASTER_RULES 11）。"""
    return graph(refs=refs, claim_id=claim_id, relation="MENTIONS",
                 evidence_text=UNVERIFIED_EVIDENCE_TEXT, status="unverified")


def store_for(temp_dir, name="phase09.sqlite3"):
    """隔离临时 sqlite 上的 QaStore（setUp 里断言 backend 必须是 sqlite）。"""
    from qa_storage import QaStore
    from sqlite_database import SQLiteDatabase

    database = SQLiteDatabase(os.path.join(temp_dir, name))
    database.connect()
    database.create_tables()
    assert database.backend == "sqlite", "测试必须跑在隔离 sqlite 上"
    store = QaStore(database)
    store.ensure_schema()
    return database, store


@contextlib.contextmanager
def temp_store(name="phase09.sqlite3"):
    """一次性使用的临时库（退出时关连接再删目录 —— Windows 上不关连接删不掉文件）。"""
    with tempfile.TemporaryDirectory() as tmp:
        database, store = store_for(tmp, name)
        try:
            yield database, store
        finally:
            try:
                database.connection.close()
            except Exception:
                pass


def run_meta(run_id="run-p09", **overrides):
    meta = {"id": run_id, **SCOPE}
    meta.update(overrides)
    return meta
