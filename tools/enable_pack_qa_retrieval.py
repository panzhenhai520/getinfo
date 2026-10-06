#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把所有行业包的 ragflow_policy.qa_retrieval_enabled 打开（发布新包版本，不激活）。

为什么要这一步：`needs_ragflow = needs_retrieval and ragflow_qa_enabled and mode != fast`，
而所有已发布包的 `qa_retrieval_enabled` 都是 False → 任何机器、任何问题都不走 RAGFlow 增强检索。
种子文件里 family_office 本来是 true，但已发布版本漂移成了 false（已发布优先）。

只发布、**不激活**：`industry_pack_loader` 读的是已发布 manifest，改开关不需要动信源/运行时设置，
也就不触发重扫（激活才会动 sources/associations）。

用法：
    python tools/enable_pack_qa_retrieval.py            # 预演（只打印）
    python tools/enable_pack_qa_retrieval.py --apply    # 真正发布
"""
from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, __file__.rsplit("tools", 1)[0])


def _switch_active_version(db, pack_id: str, version_id: int) -> dict:
    """轻量切换 active_industry_pack_version_id（与 /activate-version 接口同款）。

    为什么必须做：`published_manifest_for_loader` 对**当前激活包**返回的是
    `active_industry_pack_version_id` 指向的那一版，而不是"最新已发布版"。
    只发布不切版本，激活包的开关不会生效（实测 family_office 已发布 v11=True 但仍读到 False）。
    这个切换只改一个 runtime setting，不触发全量重扫。
    """
    active_pack = ""
    try:
        with db.lock:
            row = db.connection.execute(
                "SELECT setting_value FROM intel_runtime_settings "
                "WHERE setting_key='active_industry_pack_id'").fetchone()
        active_pack = str((row[0] if row else "") or "")
    except Exception:
        active_pack = ""
    if active_pack != str(pack_id):
        return {"switched": False, "reason": "非当前激活包（active=%s）" % (active_pack or "-")}
    with db.lock:
        cursor = db.connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO intel_runtime_settings(setting_key, setting_value, updated_at)
                VALUES ('active_industry_pack_version_id', ?, datetime('now'))
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value=excluded.setting_value, updated_at=excluded.updated_at
                """,
                (str(int(version_id)),),
            )
            db.connection.commit()
        finally:
            cursor.close()
    return {"switched": True, "active_version_id": int(version_id)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真正发布新版本（默认只预演）")
    parser.add_argument("--pack", action="append", default=[], help="只处理指定包（可多次）")
    parser.add_argument("--actor", default="qa-retrieval-enable")
    args = parser.parse_args(argv)

    from industry_pack_admin import IndustryPackAdminService, industry_pack_version_store
    from industry_packs import industry_pack_loader
    from qa_policy import QaPolicyResolver
    from sqlite_database import sqlite_db

    admin = IndustryPackAdminService(industry_pack_version_store, industry_pack_loader,
                                     url_validator=lambda value: str(value))
    resolver = QaPolicyResolver()
    packs = [str(item.get("id") or "") for item in industry_pack_loader.list()]
    if args.pack:
        packs = [item for item in packs if item in set(args.pack)]

    print("待处理行业包: %s" % packs)
    results = []
    for pack_id in packs:
        try:
            pack = industry_pack_loader.load(pack_id, enabled_only=False)
        except Exception as exc:
            results.append({"pack": pack_id, "action": "skip", "reason": "包无法加载: %s" % str(exc)[:80]})
            continue
        policy_block = dict(pack.get("ragflow_policy") or {})
        kb_note = ""
        try:
            policy = resolver.resolve(pack_id)
            kb_id = str(getattr(policy, "ragflow_kb_id", "") or "")
            app_id = str(getattr(policy, "ragflow_app_id", "") or "")
        except Exception as exc:
            kb_id = app_id = ""
            kb_note = "策略解析失败: %s" % str(exc)[:60]
        if not (kb_id and app_id):
            # 本机没有可用知识库时仍然发布开关：loader 读的是已发布版本，
            # 以后配好知识库即可直接生效；运行期会走 _kb_not_configured 兜底（不是故障）。
            kb_note = kb_note or "本机未配置可用知识库（运行期走 kb_not_configured 兜底）"

        latest = industry_pack_version_store.latest_published(pack_id)
        manifest = dict((latest or {}).get("manifest") or pack)
        policy_block["qa_retrieval_enabled"] = True
        manifest["ragflow_policy"] = policy_block
        plan = {"pack": pack_id, "action": "publish" if args.apply else "would_publish",
                "from_version": str(manifest.get("pack_version") or ""),
                "kb": kb_id[:12], "note": kb_note,
                "upload_crawled_articles": policy_block.get("upload_crawled_articles")}
        if not args.apply:
            results.append(plan)
            continue
        try:
            draft = admin.get_or_create_draft(pack_id, actor=args.actor)
            saved = admin.save_draft(pack_id, manifest, expected_revision=int(draft["revision"]),
                                     actor=args.actor)
            published = admin.publish_draft(pack_id, expected_revision=int(saved["revision"]),
                                            actor=args.actor)
            industry_pack_loader.clear_cache(pack_id)
            plan["published_version_id"] = published.get("id")
            plan["published_version"] = published.get("version_number")
            # 激活包必须切版本，否则 loader 仍读旧版本
            plan["active_switch"] = _switch_active_version(sqlite_db, pack_id, int(published.get("id")))
            industry_pack_loader.clear_cache(pack_id)
            after = industry_pack_loader.load(pack_id, enabled_only=False) or {}
            plan["qa_retrieval_enabled_after"] = (after.get("ragflow_policy") or {}).get("qa_retrieval_enabled")
            if plan["qa_retrieval_enabled_after"] is not True:
                plan["action"] = "published_but_not_effective"
        except Exception as exc:
            plan.update({"action": "failed", "error": "%s: %s" % (type(exc).__name__, str(exc)[:160])})
        results.append(plan)

    print(json.dumps(results, ensure_ascii=False, indent=1))
    effective = [item for item in results if item.get("qa_retrieval_enabled_after") is True]
    print("\n汇总：开关已生效 %d 个 / 已发布待生效 %d 个 / 跳过 %d 个 / 失败 %d 个"
          % (len(effective),
             len([item for item in results if item.get("action") == "published_but_not_effective"]),
             len([item for item in results if item.get("action") == "skip"]),
             len([item for item in results if item.get("action") == "failed"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
