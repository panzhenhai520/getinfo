#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文章入库统一入口（唯一收敛点）。

为什么要收口：过去 12+ 个采集/接口路径各自直接调 `db.insert_article()`（还有 4 处写成
并不存在的 `db.add_article()`，异常被 except 吞掉 → 文章静默丢失）。入口分散导致：
  - 有的路径没盖 source_method，出了问题查不到是谁写进来的；
  - 入库被最终闸门拒绝（无关键词命中 / 框架页 / 超保留窗口 / 链接目录页）时，
    各调用点打印格式不一，运营看不到"这篇文章为什么没进库"；
  - 想统一加一道入库前的处理（清洗、精炼、归属兜底）就得改十几处。

现在所有生产入库路径只允许调用 `ingest_article(article_data, source_kind=...)`：
  - source_kind 必填且必须在 SOURCE_KINDS 白名单内（写错立刻报错，不再静默）；
  - 统一盖 source_method，便于按来源统计/排障；
  - 统一在拒绝时打印带来源的日志；
  - 真正的入库闸门/清洗/归属兜底仍在 sqlite_database.insert_article 内，
    这里只是把"谁在入库"这件事收敛成一个可枚举的事实（并由
    tests/test_ingest_choke_point.py 守护）。
"""
from __future__ import annotations

from typing import Optional

# 允许的生产入库来源。新增采集路径必须先在这里登记来源名。
SOURCE_KINDS = frozenset({
    "scheduled_crawl",          # scheduler.py 定时爬取主链路
    "article_link_crawl",       # article_link_extractor.py 栏目→文章链接聚合
    "candidate_crawler",        # candidate_crawler_adapter.py 候选 URL 派发
    "incremental_crawl",        # incremental_crawl_api.py 增量爬取
    "smart_extraction",         # smart_extraction_api.py 智能抽取
    "smart_extraction_link",    # smart_extraction_api.py 先存链接（正文为空）
    "newspaper3k",              # firecrawl_app.py newspaper3k 抽取/批量抽取
    "remote_result",            # remote_result_ingestor.py 远端（VPN）结果回收
    "report_insight",           # intel_reports.py PDF 报告 LLM 提炼条目
    "ai_chat",                  # chat_api.py AI 助手问答存档
    "ai_chat_evidence",         # chat_api.py AI 助手证据补爬
    "manual_article",           # manual_articles.py 手工发文（skip_pipeline）
    "api_insert",               # sqlite_api.py /api/sqlite/articles
    "db_init_fixture",          # init_sqlite_database.py 初始化自检
})


def ingest_article(
    article_data: dict,
    *,
    source_kind: str,
    db=None,
    skip_pipeline: bool = False,
) -> Optional[int]:
    """所有生产入库路径的唯一入口。

    Args:
        article_data: 文章数据字典（与 sqlite_database.insert_article 同口径）
        source_kind: 入库来源，必须在 SOURCE_KINDS 内
        db: 数据库实例，默认用全局单例 sqlite_db
        skip_pipeline: 手工发文场景置 True（跳过 LLM 精炼与自动分类入队；
            入库闸门与包归属兜底仍然照常执行）

    Returns:
        Optional[int]: 文章 ID；被入库闸门拒绝或写入失败返回 None
    """
    if source_kind not in SOURCE_KINDS:
        raise ValueError(
            "未知入库来源 source_kind=%r，允许值: %s"
            % (source_kind, ", ".join(sorted(SOURCE_KINDS)))
        )
    if db is None:
        from sqlite_database import sqlite_db

        db = sqlite_db
    if not isinstance(article_data, dict):
        raise TypeError("article_data 必须是 dict")

    # 统一盖来源标记：调用点没写 source_method 的，用 source_kind 兜底，
    # 便于按来源排查"这篇文章是谁写进来的"。
    article_data.setdefault("source_method", source_kind)
    if not str(article_data.get("source_method") or "").strip():
        article_data["source_method"] = source_kind

    article_id = db.insert_article(article_data, skip_pipeline=skip_pipeline)
    if not article_id:
        print(
            "⏭️ 未入库（%s）: %s"
            % (source_kind, str(article_data.get("title") or article_data.get("url") or "无标题")[:60])
        )
    return article_id


__all__ = ["ingest_article", "SOURCE_KINDS"]
