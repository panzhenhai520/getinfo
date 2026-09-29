# -*- coding: utf-8 -*-
"""Re-run rule classification for the active pack's articles so score_details
gets the full schema (with hits.anchor), making them visible in the gated list."""
import os
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import config
from industry_pack_runtime import active_industry_composition_service
from intel_classifier import classification_service
from intel_database import intel_repository
from sqlite_database import sqlite_db

pack_id = ""
activation_id = ""


def count_anchor(pack, act):
    db = sqlite_db
    db._ensure_connection()
    with db.lock:
        rows = db.connection.execute(
            "SELECT count(*) FROM article_intel_classifications "
            "WHERE industry_pack_id=? AND activation_id=? "
            "AND COALESCE(json_array_length(json_extract(score_details_json,'$.hits.anchor')),0)>0",
            (pack, act),
        ).fetchall()
    return int(rows[0][0])


def count_total(pack, act):
    db = sqlite_db
    db._ensure_connection()
    with db.lock:
        rows = db.connection.execute(
            "SELECT count(*) FROM article_intel_classifications "
            "WHERE industry_pack_id=? AND activation_id=?",
            (pack, act),
        ).fetchall()
    return int(rows[0][0])


def active_article_ids(pack, act):
    db = sqlite_db
    db._ensure_connection()
    with db.lock:
        rows = db.connection.execute(
            "SELECT a.id FROM articles a "
            "JOIN article_intel_classifications ic ON ic.article_id=a.id "
            "WHERE a.status='active' AND ic.industry_pack_id=? AND ic.activation_id=? "
            "ORDER BY a.id",
            (pack, act),
        ).fetchall()
    return [int(r[0]) for r in rows]


def main():
    global pack_id, activation_id
    snap = active_industry_composition_service.snapshot()
    pack_id = str(snap["active_industry_pack_id"])
    activation_id = str(snap.get("active_industry_activation_id") or "")
    print("active pack:", pack_id, "activation:", activation_id)

    ids = active_article_ids(pack_id, activation_id)
    print("articles to reclassify:", len(ids))

    before_anchor = count_anchor(pack_id, activation_id)
    print("before: anchor>0 =", before_anchor, "| total =", count_total(pack_id, activation_id))

    cat_counts = {}
    errors = []
    removed = 0
    for i, article_id in enumerate(ids):
        try:
            result = classification_service.classify_article_id(
                int(article_id),
                pack_id,
                activation_id=activation_id,
            )
            # 通用行业过滤器：不属于行业包（未命中行业核心词/包实体）→ 删除其在当前包的分类 + 主题关联
            if not result.get("admitted", True):
                with sqlite_db.lock:
                    sqlite_db.connection.execute(
                        "DELETE FROM article_intel_classifications WHERE article_id=? AND industry_pack_id=? AND activation_id=?",
                        (int(article_id), pack_id, activation_id),
                    )
                    sqlite_db.connection.execute(
                        "DELETE FROM intel_topic_articles WHERE article_id=? AND topic_id IN "
                        "(SELECT id FROM intel_topics WHERE industry_pack_id=?)",
                        (int(article_id), pack_id),
                    )
                    sqlite_db.connection.commit()
                removed += 1
                continue
            cat = result.get("final_category") or result.get("rule_category")
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        except Exception as exc:
            errors.append((article_id, str(exc)))
        if (i + 1) % 50 == 0:
            print("  progress", i + 1, "/", len(ids))

    print("reclassified category counts:", cat_counts)
    print("removed non-industry:", removed)
    print("errors:", len(errors))
    if errors[:10]:
        for a, e in errors[:10]:
            print("   ", a, e)
    after_anchor = count_anchor(pack_id, activation_id)
    print("after: anchor>0 =", after_anchor, "| total =", count_total(pack_id, activation_id))


if __name__ == "__main__":
    main()
