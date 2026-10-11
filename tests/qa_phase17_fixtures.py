#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 17 用例的公共构造器（**不是测试文件**：文件名不以 test_ 开头）。

两件事：

  · `synthetic_corpus_db()` —— 隔离临时 sqlite 上的**小语料库**（文章 + 分类 + 事件/属性 + 图），
    让 E2E 的检索侧可以脱离 2.4MB 真机快照单测；素材走 `IntelRepository` 与
    `KnowledgeGraphBuilder` 的真实写入口，不手搓表行；
  · `tiny_answer_snapshot()` —— Phase 08/13 形状的**真机 run 快照**（字段名与 A 机导出一致），
    素材来自 `qa_phase13_fixtures.verified_graph_and_answer()`（证据真的过了 Phase 02 标注、
    claim 图与最终答案都是真实契约形状）。

零模型、零网络、零嵌入端点；所有测试都跑在隔离临时 sqlite 上（setUp 断言 backend）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import qa_phase08_fixtures as fx08  # noqa: E402
import qa_phase13_fixtures as fx13  # noqa: E402

PACK_ID = "automotive_industry"
"""用**真实注册过的**行业包 id：规划器会解析包配置，造一个假包会直接抛 IndustryPackError。"""
EMPTY_PACK_ID = "family_office"
"""已注册但**本测试语料里没有文章**的包：用来测"无命中但不是错误"。"""
QUESTION_ONE = "比亚迪最近有什么动态？"
QUESTION_TWO = "蔚来最近有什么动态？"
ARTICLE_TEXT = (
    "比亚迪近日发布了新一代电池平台，并公布了三季度交付数据。"
    "公司表示新一代平台将优先用于高端车型，交付节奏保持不变。"
) * 3
QUESTION_TERMS = ["比亚迪", "交付"]


def temp_dir(prefix="qa-phase17-"):
    """临时目录（显式创建/显式清理：Windows 上 sqlite 文件可能仍被句柄占着，
    自动清理会抛 NotADirectoryError，故一律 ignore_errors）。"""
    return tempfile.mkdtemp(prefix=prefix)


def cleanup_dir(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def store_for(temp_dir, name="phase17.sqlite3"):
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


def synthetic_corpus_db(temp_dir, name="p17-corpus.sqlite3", *, articles=4):
    """小语料库：文章 + 分类 + 事件/属性 + 物化图（全部走真实写入口）。"""
    import config
    from intel_database import IntelRepository
    from kg_builder import KnowledgeGraphBuilder
    from sqlite_database import SQLiteDatabase

    saved_type = getattr(config, "DATABASE_TYPE", None)
    config.DATABASE_TYPE = "sqlite"
    try:
        database = SQLiteDatabase(os.path.join(temp_dir, name))
        database.connect()
        database.create_tables()
        database.analyze_article_spacetime_profile = lambda _article_id: None
        repository = IntelRepository(database)
        repository._ensure()
    finally:
        if saved_type is not None:
            config.DATABASE_TYPE = saved_type
    assert database.backend == "sqlite", "语料必须落在隔离临时 sqlite 上"

    with database.lock:
        cursor = database.connection.cursor()
        try:
            for index in range(1, int(articles) + 1):
                subject = QUESTION_TERMS[0] if index % 2 else "蔚来"
                cursor.execute(
                    "INSERT OR REPLACE INTO articles(id,url,title,content,domain,publish_date,"
                    "first_crawled,status,quality_score,content_length) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (index, "https://example.invalid/a%d" % index,
                     "%s 发布新一代电池平台（%d）" % (subject, index),
                     ARTICLE_TEXT, "example.invalid", "2026-03-0%d" % min(9, index),
                     "2026-03-10", "active", 70, len(ARTICLE_TEXT)))
                cursor.execute(
                    "INSERT OR REPLACE INTO article_intel_classifications(article_id,"
                    " industry_pack_id, activation_id, industry_pack_version,"
                    " classifier_version, article_content_hash, rule_category, rule_confidence,"
                    " score_details_json, matched_keywords_json, topic_tags_json, final_category,"
                    " result_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (index, PACK_ID, "", "v1", "rule-v1", "hash-%d" % index, "event", 0.8,
                     # 逐字对齐真实分类器的 score_details 形状（`_classification_admitted`
                     # 认 hits.anchor + relevance_score ≥ minimum_relevance_score）
                     json.dumps({"hits": {"anchor": [subject]},
                                 "minimum_relevance_score": 0.2, "relevance_score": 0.8,
                                 "components": {"core": 6.0, "event": 6.0, "expanded": 0.0}},
                                ensure_ascii=False),
                     json.dumps([subject], ensure_ascii=False), "[]", "event", "rule"))
                cursor.execute(
                    "INSERT OR REPLACE INTO intel_article_events(article_id, event_index,"
                    " industry_pack_id, subject, action, object, event_time, event_type,"
                    " subject_type, state_before, state_after, event_hash, content_hash)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (index, 0, PACK_ID, subject, "发布", "新一代电池平台", "2026-03-01",
                     "release", "entity", "", "", "eh-%d" % index, "ch-%d" % index))
                cursor.execute(
                    "INSERT OR REPLACE INTO intel_article_attributes(article_id, attr_index,"
                    " industry_pack_id, subject, attribute, value, value_type, valid_from,"
                    " valid_to, as_of, evidence_quote, content_hash)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (index, 0, PACK_ID, subject, "交付节奏", "保持不变", "text",
                     "2026-03-01", "", "2026-03-01", "交付节奏保持不变", "ch-%d" % index))
            database.connection.commit()
        finally:
            cursor.close()
    built = KnowledgeGraphBuilder(repository=IntelRepository(database)).build(
        pack_id=PACK_ID, apply=True)
    database.synthetic_graph_build = built
    return database


def tiny_questions() -> list:
    return [
        {"id": "q1", "question": QUESTION_ONE, "kind": "event",
         "industry_pack_id": PACK_ID, "expect_terms": QUESTION_TERMS},
        {"id": "q2", "question": QUESTION_TWO, "kind": "event",
         "industry_pack_id": PACK_ID, "expect_terms": ["蔚来"]},
        {"id": "q3", "question": "不存在的包里的问题", "kind": "event",
         "industry_pack_id": EMPTY_PACK_ID, "expect_terms": ["不存在"]},
    ]


def tiny_answer_snapshot(*, run_id="run-p17-1", with_answer=True, question=fx13.QUESTION):
    """Phase 08/13 形状的真机 run 快照（字段名与 A 机导出一致）。"""
    graph, answer = fx13.verified_graph_and_answer(extra_claim=True)
    claims = []
    for node in graph["claims"]:
        claims.append({
            "run_id": run_id, "claim_key": node["claim_id"],
            "claim_text": node["text"],
            "verification_status": node.get("verification_status") or "unverified",
            "payload": {"claim_id": node["claim_id"], "claim": dict(node["claim"]),
                        "plan_only": bool(node.get("plan_only"))},
        })
    evidence = []
    for item in graph["evidence"]:
        evidence.append({
            "run_id": run_id, "evidence_ref": item.get("evidence_ref"),
            "source_type": item.get("source_type"),
            "source_url": item.get("source_url"),
            "source_title": item.get("title"),
            "published_at": item.get("published_at"),
            "authority_level": item.get("authority_level"),
            "payload": dict(item),
        })
    edges = []
    for edge in graph["edges"]:
        edges.append({
            "run_id": run_id, "claim_key": edge.get("claim_id"),
            "evidence_ref": edge.get("evidence_ref"),
            "relationship": str(edge.get("relationship") or "supports").upper(),
            "relevance_score": edge.get("relevance_score"),
            "published_at": edge.get("published_at"),
        })
    run = {"id": run_id, "mode": "standard", "question_text": question,
           "owner_user_id": "user:1", "session_id": "s-p17", "industry_pack_id": PACK_ID}
    if with_answer:
        run["final_answer"] = dict(answer)
    return {"captured_at_utc": "2026-10-11T00:00:00Z",
            "source": {"host": "synthetic", "snapshot_version": "phase17-fixture-v1"},
            "runs": [run], "claims": claims, "evidence": evidence, "edges": edges,
            "conflicts": []}


def alias_answer_snapshot(snapshot):
    """把 `runs/claims/edges/...` 换成 A 机 Phase 12 导出用的 `qa_*` 键名（测归一化）。"""
    mapping = {"runs": "qa_runs", "claims": "qa_claims", "edges": "qa_claim_evidence",
               "evidence": "qa_evidence", "conflicts": "qa_conflicts"}
    return {mapping.get(key, key): value for key, value in dict(snapshot).items()}


def golden_like_texts():
    """金标里那种"人工合成文本"（用来测独立标注集的重叠检测**真的会报**）。"""
    return [fx08.LONG_ARTICLE, fx13.CLAIM_TEXT]
