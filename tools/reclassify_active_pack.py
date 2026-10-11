# -*- coding: utf-8 -*-
"""Re-run rule classification for the active pack's articles so score_details
gets the full schema (with hits.anchor), making them visible in the gated list.

⚠️ 删除护栏（2026-10-11 事故后加）：
本工具原先在 `admitted=False` 时会**直接删掉**该文章的
`article_intel_classifications` 行与 `intel_topic_articles` 关联。对某行业包一次跑下来
就是 84 行被删：分类分母 973→889、前后结论不可比，用户还会"莫名少 84 篇文章"。
因此现在的默认行为是**不删任何行**（未准入的行保持原样，只是不再计入本次统计），
删除必须在命令行上显式授权 `--delete-unadmitted`，并且：
  * 先打印将要删除的行数与样例 article_id；
  * 交互式终端里要求二次确认（输入 yes）；非交互环境（管道/cron）默认拒绝；
  * 完全跳过交互需要再加 `--yes`（双显式参数即视为二次确认）。
"""
import argparse
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


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="重跑激活行业包的规则分类（默认不删除任何行）"
    )
    parser.add_argument(
        "--delete-unadmitted",
        action="store_true",
        help=(
            "删除被 admitted=False 拦下的行（分类行 + 主题关联）。"
            "默认关闭：这些行会原样保留，避免改变分母与用户可见文章数"
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="配合 --delete-unadmitted 使用：跳过交互式二次确认（删除不可逆）",
    )
    return parser.parse_args(argv)


def _interactive_confirm(count):
    """二次确认。非交互环境（stdin 不是终端）一律拒绝，避免管道里静默删数据。"""
    stdin = sys.stdin
    if stdin is None or not getattr(stdin, "isatty", lambda: False)():
        print(
            "非交互环境（stdin 不是终端）：拒绝执行删除。"
            "确需删除请在交互终端里确认，或显式再加 --yes。"
        )
        return False
    try:
        answer = input(
            f"即将删除 {count} 行分类（不可逆）。确认请输入 yes，其它任何输入都会中止: "
        )
    except (EOFError, KeyboardInterrupt):
        print("")
        return False
    return answer.strip().lower() in {"yes", "y", "是"}


def prune_unadmitted_rows(
    article_ids,
    *,
    pack_id,
    activation_id,
    delete_unadmitted=False,
    assume_yes=False,
    confirm=None,
):
    """处理 admitted=False 的行，返回真正删除的行数。

    默认（delete_unadmitted=False）只报告、不执行任何 DELETE。
    删除路径必须同时满足：显式 --delete-unadmitted + （--yes 或交互式确认通过）。
    """
    ids = [int(item) for item in article_ids]
    if not ids:
        return 0
    print("未准入（admitted=False）行数:", len(ids))
    if not delete_unadmitted:
        print(
            "默认不删行：这些行的分类与主题关联保持原样。"
            "（删除会改变分类分母、让用户莫名少文章；历史上有过一次删 84 行的事故）"
        )
        print("确需删除请显式加参数：--delete-unadmitted --yes")
        return 0
    print("即将删除的 article_id（最多列 10 个）:", ids[:10])
    if not assume_yes:
        confirm_fn = confirm or _interactive_confirm
        if not confirm_fn(len(ids)):
            print("未获得二次确认 → 本次不删除任何行。")
            return 0
    removed = 0
    with sqlite_db.lock:
        for article_id in ids:
            sqlite_db.connection.execute(
                "DELETE FROM article_intel_classifications "
                "WHERE article_id=? AND industry_pack_id=? AND activation_id=?",
                (int(article_id), pack_id, activation_id),
            )
            sqlite_db.connection.execute(
                "DELETE FROM intel_topic_articles WHERE article_id=? AND topic_id IN "
                "(SELECT id FROM intel_topics WHERE industry_pack_id=?)",
                (int(article_id), pack_id),
            )
            removed += 1
        sqlite_db.connection.commit()
    print("已删除行数:", removed)
    return removed


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


def main(argv=None):
    global pack_id, activation_id
    args = _parse_args(argv)
    snap = active_industry_composition_service.snapshot()
    pack_id = str(snap["active_industry_pack_id"])
    activation_id = str(snap.get("active_industry_activation_id") or "")
    print("active pack:", pack_id, "activation:", activation_id)
    print(
        "删除未准入行:",
        "开启（--delete-unadmitted%s）" % (" --yes" if args.yes else " + 交互确认"),
        "| 默认行为是不删行",
    )

    ids = active_article_ids(pack_id, activation_id)
    print("articles to reclassify:", len(ids))

    before_anchor = count_anchor(pack_id, activation_id)
    print("before: anchor>0 =", before_anchor, "| total =", count_total(pack_id, activation_id))

    cat_counts = {}
    errors = []
    unadmitted_ids = []
    for i, article_id in enumerate(ids):
        try:
            result = classification_service.classify_article_id(
                int(article_id),
                pack_id,
                activation_id=activation_id,
            )
            # 通用行业过滤器：不属于行业包（未命中行业核心词/包实体）的文章。
            # 先只收集 id、**不在循环里删行**：删除是显式授权的独立动作
            # （见 prune_unadmitted_rows 与文件头的删除护栏说明）。
            if not result.get("admitted", True):
                unadmitted_ids.append(int(article_id))
                continue
            cat = result.get("final_category") or result.get("rule_category")
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        except Exception as exc:
            errors.append((article_id, str(exc)))
        if (i + 1) % 50 == 0:
            print("  progress", i + 1, "/", len(ids))

    removed = prune_unadmitted_rows(
        unadmitted_ids,
        pack_id=pack_id,
        activation_id=activation_id,
        delete_unadmitted=args.delete_unadmitted,
        assume_yes=args.yes,
    )

    print("reclassified category counts:", cat_counts)
    print("unadmitted (admitted=False):", len(unadmitted_ids))
    print("removed non-industry:", removed)
    print("errors:", len(errors))
    if errors[:10]:
        for a, e in errors[:10]:
            print("   ", a, e)
    after_anchor = count_anchor(pack_id, activation_id)
    print("after: anchor>0 =", after_anchor, "| total =", count_total(pack_id, activation_id))


if __name__ == "__main__":
    main()
