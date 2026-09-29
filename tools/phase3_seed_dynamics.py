# -*- coding: utf-8 -*-
"""阶段3 验收准备：为 bolean 包造两条「无正文」动态条目。

- A：URL 指向真实可达的 https://example.com/ → 验收「在线转换阅读」按钮实时转换；
- B：URL 指向内网地址 → 预置 dynamic_converted 缓存 → 验收 converted=true 直接渲染；
- C：URL 指向不可达域名 → 验收转换失败静默降级（显示原文链接）。
"""
import sys

sys.path.insert(0, ".")

from sqlite_database import sqlite_db
from intel_database import IntelRepository

PACK = "bolean_security_compute"


def make_classification(article_id):
    repo = IntelRepository(sqlite_db)
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        try:
            cur.execute(
                """INSERT OR IGNORE INTO article_intel_classifications(
                       article_id, industry_pack_id, activation_id, industry_pack_version,
                       classifier_version, article_content_hash, rule_category, rule_confidence,
                       rule_reason, score_details_json, matched_keywords_json, llm_category,
                       llm_confidence, llm_reason, why_important, trend_summary, topic_tags_json,
                       final_category, final_confidence, final_reason, result_source, fusion_version,
                       llm_model_id, llm_prompt_version, llm_error, classified_at)
                   VALUES (?, ?, '', 'phase3-accept', 'phase3', '', 'other', 1.0, '验收条目',
                           '{}', '[\"网络安全\"]', NULL, NULL, '', '', '', '[]',
                           'other', 1.0, '验收条目', 'rule', '', '', '', '', datetime('now'))""",
                (int(article_id), PACK),
            )
            sqlite_db.connection.commit()
        finally:
            cur.close()


def add_article(title, url, content=""):
    aid = sqlite_db.insert_article({
        "url": url,
        "title": title,
        "content": content,
        "matched_keywords": ["网络安全"],
        "matched_keywords_raw": "网络安全",
        "publish_date": "2026-09-18",
        "extraction_method": "agent-search",
        "source_method": "phase3-accept",
    })
    if aid:
        make_classification(aid)
    print(f"article {aid}: {title} url={url}")
    return aid


# A：真实可达 URL（实时转换）
aid_a = add_article("验收动态：可实时转换条目", "https://example.com/")
# B：内网 URL（预置转换缓存 → converted=true 直接渲染）
aid_b = add_article("验收动态：已转换条目", "http://192.168.10.5/report.html")
_b_md = (
    "# 验收动态：已转换条目\n\n"
    "这是一段预置的转换结果正文，包含**网络安全**要点。\n\n"
    "| 项 | 值 |\n"
    "| --- | --- |\n"
    "| 等级 | 高 |\n"
)
with sqlite_db.lock:
    cur = sqlite_db.connection.cursor()
    try:
        cur.execute(
            """INSERT INTO dynamic_converted(url, markdown, source_format, converted_at, last_error)
               VALUES ('http://192.168.10.5/report.html', ?, 'text/html', datetime('now'), '')
               ON CONFLICT(url) DO UPDATE SET markdown=excluded.markdown, converted_at=datetime('now')""",
            (_b_md,),
        )
        sqlite_db.connection.commit()
    finally:
        cur.close()
print("seeded converted cache for B")
# C：不可达 URL（转换失败 → 降级原文链接）
aid_c = add_article("验收动态：转换失败条目", "https://nonexistent-domain-9f3k2.example.invalid/page")
print("done", aid_a, aid_b, aid_c)
