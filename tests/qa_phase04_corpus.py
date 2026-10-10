#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 04 测试/回放用的语料构造（**不是测试文件**，pytest 不会收集）。

为什么要单独一份：
  · 隔离：一律临时 sqlite（`DATABASE_TYPE=sqlite` + 临时 `DATABASE_PATH`），
    并且 `setUp` 里断言 `backend=='sqlite'`，避免误连生产库；
  · 形状真实：文章 / 分类 / 事件 / 属性 / 向量都按**生产表结构**写，检索链能真跑通；
  · 不走 `sqlite_database.insert_article`：那个入口会顺带做"兜底归属"分类，
    把 (article_id, industry_pack_id) 抢先占掉（实测 UNIQUE 冲突），
    这里直接用原生 SQL 入库，测试断言才可控。
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import intel_database  # noqa: E402
import sqlite_database  # noqa: E402
from intel_database import IntelRepository  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

DEFAULT_PACK = "family_office"


def make_db(path: str) -> SQLiteDatabase:
    """临时库 + 全部表（含 intel_* / kg_* / 向量表）。"""
    db = SQLiteDatabase(path)
    db.connect()
    db.create_tables()
    # 详情页时空剖面回填会去打外部接口，测试里必须摘掉
    db.analyze_article_spacetime_profile = lambda _article_id: None
    IntelRepository(db)._ensure()
    assert db.backend == "sqlite", "测试必须跑在隔离的 sqlite 上"
    return db


@contextmanager
def patch_sqlite_singletons(db):
    """把模块级单例指向临时库。

    `kg_builder.KnowledgeGraphBuilder()`（不显式传 repository 时）与
    `chat_api._load_vector_matrix()` 读的都是模块级 `sqlite_db`；
    不指过来的话，这些路径会去查真实库，测试就测不到自己要测的分支。
    """
    saved = (sqlite_database.sqlite_db, intel_database.sqlite_db)
    sqlite_database.sqlite_db = db
    intel_database.sqlite_db = db
    try:
        yield db
    finally:
        sqlite_database.sqlite_db, intel_database.sqlite_db = saved


def add_article(db: SQLiteDatabase, *, article_id: int | None = None, url: str = "",
                title: str = "", content: str = "", publish_date: str = "2026-10-01",
                domain: str = "example.com", keywords=(), status: str = "active",
                quality_score: float = 80.0) -> int:
    """原生 SQL 入库；返回 article_id。"""
    url = url or "https://example.com/p04/%s" % (article_id or title[:8] or "x")
    with db.lock:
        cursor = db.connection.cursor()
        try:
            if article_id:
                sql = ("INSERT INTO articles(id,url,title,content,domain,publish_date,first_crawled,"
                       "content_length,quality_score,status,matched_keywords) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)")
                params = (article_id, url, title, content, domain, publish_date, publish_date,
                          len(content), quality_score, status,
                          json.dumps(list(keywords), ensure_ascii=False))
            else:
                sql = ("INSERT INTO articles(url,title,content,domain,publish_date,first_crawled,"
                       "content_length,quality_score,status,matched_keywords) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)")
                params = (url, title, content, domain, publish_date, publish_date,
                          len(content), quality_score, status,
                          json.dumps(list(keywords), ensure_ascii=False))
            cursor.execute(sql, params)
            if not article_id:
                article_id = int(cursor.lastrowid)
            db.connection.commit()
        finally:
            cursor.close()
    return int(article_id)


def add_classification(db: SQLiteDatabase, article_id: int, *, pack_id: str = DEFAULT_PACK,
                       category: str = "event", keywords=(), admitted: bool = True,
                       activation_id: str = "", relevance: float = 9.0) -> None:
    """写一条分类记录；`admitted=True` 时构造能过 `_classification_admitted` 的 score_details。"""
    score_details = {
        "hits": {"anchor": list(keywords) or ["测试锚点"], "core": [], "expanded": []},
        "relevance_score": float(relevance),
        "minimum_relevance_score": 5.0,
    }
    if not admitted:
        score_details = {"hits": {"anchor": [], "core": [], "expanded": []},
                         "relevance_score": 0.0, "minimum_relevance_score": 5.0}
    with db.lock:
        cursor = db.connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO article_intel_classifications(article_id, industry_pack_id,"
                " activation_id, industry_pack_version, classifier_version, article_content_hash,"
                " rule_category, matched_keywords_json, topic_tags_json, final_category,"
                " score_details_json) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(article_id, industry_pack_id) DO UPDATE SET"
                " matched_keywords_json=excluded.matched_keywords_json,"
                " score_details_json=excluded.score_details_json,"
                " final_category=excluded.final_category",
                (int(article_id), str(pack_id), str(activation_id), "v1", "test", "h1",
                 str(category), json.dumps(list(keywords), ensure_ascii=False), "[]",
                 str(category), json.dumps(score_details, ensure_ascii=False)),
            )
            db.connection.commit()
        finally:
            cursor.close()


def add_events(db: SQLiteDatabase, article_id: int, *, pack_id: str = DEFAULT_PACK,
               events=(), attributes=()) -> None:
    """事件 / 属性行（kg_builder 的输入），供图谱 Hunter 的集成用例使用。"""
    with db.lock:
        cursor = db.connection.cursor()
        try:
            for index, event in enumerate(events or []):
                cursor.execute(
                    "INSERT INTO intel_article_events(article_id, event_index, industry_pack_id,"
                    " subject, action, object, event_time, event_type, event_hash, content_hash)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (int(article_id), index, str(pack_id), str(event.get("subject") or ""),
                     str(event.get("action") or ""), str(event.get("object") or ""),
                     str(event.get("event_time") or ""), str(event.get("event_type") or "other"),
                     "h%d" % index, "h1"),
                )
            for index, attribute in enumerate(attributes or []):
                cursor.execute(
                    "INSERT INTO intel_article_attributes(article_id, attr_index, industry_pack_id,"
                    " subject, attribute, value, value_type, valid_from, valid_to, as_of,"
                    " evidence_quote, content_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (int(article_id), index, str(pack_id), str(attribute.get("subject") or ""),
                     str(attribute.get("attribute") or ""), str(attribute.get("value") or ""),
                     str(attribute.get("value_type") or "text"),
                     str(attribute.get("valid_from") or ""), str(attribute.get("valid_to") or ""),
                     str(attribute.get("as_of") or ""), str(attribute.get("evidence_quote") or ""),
                     "h1"),
                )
            db.connection.commit()
        finally:
            cursor.close()


def add_embedding(db: SQLiteDatabase, article_id: int, vector, *, model_id: str = "bge-m3",
                  status: str = "ready") -> None:
    """写一条文章向量（float32 blob，与 `intel_article_embeddings` 生产形状一致）。"""
    import numpy as np

    array = np.asarray(vector, dtype=np.float32).reshape(-1)
    with db.lock:
        cursor = db.connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO intel_article_embeddings(article_id, model_id, embedding_dim,"
                " embedding, content_hash, status) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(article_id, model_id) DO UPDATE SET"
                " embedding=excluded.embedding, embedding_dim=excluded.embedding_dim,"
                " status=excluded.status",
                (int(article_id), str(model_id), int(array.size), array.tobytes(), "h1", str(status)),
            )
            db.connection.commit()
        finally:
            cursor.close()


def add_policy_document(db: SQLiteDatabase, article_id: int, *, doc_type: str = "official_policy",
                        doc_no: str = "", issuer: str = "", policy_title: str = "",
                        authority_level: int = 4, sync_status: str = "parsed",
                        kb_id: str = "kb-test", document_id: str = "") -> None:
    """政策登记表行（结构化通道用的就是这张既有表）。"""
    with db.lock:
        cursor = db.connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO article_ragflow_documents(article_id, kb_id, document_id,"
                " document_name, doc_type, doc_no, issuer, policy_title, authority_level,"
                " sync_status) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (int(article_id), str(kb_id), str(document_id), str(policy_title or doc_no or "doc"),
                 str(doc_type), str(doc_no), str(issuer), str(policy_title),
                 int(authority_level), str(sync_status)),
            )
            db.connection.commit()
        finally:
            cursor.close()


def seed_standard_corpus(db: SQLiteDatabase, *, pack_id: str = DEFAULT_PACK) -> dict:
    """一套标准小语料：3 篇文章（1 篇最相关、1 篇同主题、1 篇无关）+ 事件边。"""
    ids = {}
    ids["primary"] = add_article(
        db, url="https://example.com/p04/primary", title="香港家族办公室税收优惠政策正式落地",
        content="香港特区政府公布家族办公室税收优惠政策：符合条件的家族投资控权工具，"
                "在满足实质活动要求后可享利得税宽免。政策自2026年10月起生效。",
        publish_date="2026-10-01", keywords=["家族办公室"])
    add_classification(db, ids["primary"], pack_id=pack_id, keywords=["家族办公室"])
    ids["sibling"] = add_article(
        db, url="https://example.com/p04/sibling", title="内地高净值客户配置香港资产的新通道",
        content="离岸信托与家办架构成为内地高净值客户配置香港资产的新通道，"
                "税务居民身份与合规申报是关键变量。",
        publish_date="2026-09-20", keywords=["高净值"])
    add_classification(db, ids["sibling"], pack_id=pack_id, keywords=["高净值"])
    ids["unrelated"] = add_article(
        db, url="https://example.com/p04/unrelated", title="汽车芯片供应链季度观察",
        content="车规级芯片供给改善，功率半导体价格回落，整车厂排产环比回升。",
        publish_date="2026-06-01", keywords=["芯片"])
    add_classification(db, ids["unrelated"], pack_id=pack_id, keywords=["芯片"])
    add_events(db, ids["primary"], pack_id=pack_id, events=[
        {"subject": "香港特区政府", "action": "公布", "object": "家族办公室税收优惠政策",
         "event_time": "2026-10-01", "event_type": "regulation"}],
        attributes=[
            {"subject": "家族办公室税收优惠", "attribute": "生效时间", "value": "2026-10",
             "valid_from": "2026-10-01", "as_of": "2026-10-01",
             "evidence_quote": "政策自2026年10月起生效"}])
    return ids
