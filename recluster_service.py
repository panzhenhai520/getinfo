#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""聚类重算服务：LLM 检查"其它"桶事件 → 建议补 fixed_topic keywords → 更新发布 manifest → reload。

解决"关键词都不匹配 → 落其它"的问题。点【聚类】按钮触发，自动补同义词（持久）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from typing import Dict, List

from intel_database import IntelRepository
from intel_llm_client import intel_llm_client
from industry_pack_runtime import active_industry_composition_service
from industry_packs import industry_pack_loader

logger = logging.getLogger(__name__)


class ReclusterService:
    def __init__(self, repository: IntelRepository = None, llm_client=None, composition=None):
        self.repository = repository or IntelRepository()
        self.llm_client = llm_client or intel_llm_client
        self.composition = composition or active_industry_composition_service

    def run(self, *, pack_id: str = "", event_hash: str = "") -> Dict:
        """检查事件 → LLM 建议补 keywords → 更新 pack + manifest → reload → 返回结果。

        event_hash 非空：针对单个事件聚类（用户选中）；为空：批量"其它"桶。
        """
        pack_id = str(pack_id or "").strip()
        if not pack_id:
            try:
                pack_id = str(self.composition.snapshot().get("active_industry_pack_id") or "")
            except Exception:
                return {"error": "无法确定激活行业包"}

        if event_hash:
            # 单事件模式：查该事件
            import json as _json
            from db_connection import connect_database, is_postgres_connection
            conn = connect_database()
            if not is_postgres_connection(conn):
                conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT subject, action, object, entities_json, event_type FROM intel_article_events "
                "WHERE event_hash=? AND subject NOT IN ('__no_event__','__error__') LIMIT 1",
                (str(event_hash),),
            ).fetchone()
            conn.close()
            if not row:
                return {"pack_id": pack_id, "error": "事件不存在", "event_hash": event_hash}
            ents = _json.loads(row["entities_json"] or "[]")
            desc = " ".join([row["subject"] or "", row["action"] or "", row["object"] or ""])[:200]
            other_events = [{"description": desc, "subject": row["subject"], "action": row["action"],
                             "object": row["object"], "entities": ents}]
            other_before = 1
        else:
            # 批量模式：当前 aggregate（找"其它"桶）
            clusters = self.repository.aggregate_subject_clusters(pack_id=pack_id, days=90)
            other = next((c for c in clusters if c.get("subject_type") == "topic" and c.get("topic_key") == "other"), None)
            other_before = int(other["article_count"]) if other else 0
            other_events = (other or {}).get("events") or []
            if not other_events:
                return {"pack_id": pack_id, "other_before": 0, "other_after": 0, "added": {}, "msg": "无'其它'桶事件"}

        # 2. fixed_topics（当前 keywords）
        snap = self.composition.snapshot()
        fixed_topics = snap.get("primary_pack", {}).get("fixed_topics") or []

        # 3. LLM 建议
        try:
            suggestions = self.llm_client.suggest_topic_keywords(other_events, fixed_topics)
        except Exception as exc:
            logger.warning("recluster LLM 失败: %s", exc)
            return {"pack_id": pack_id, "other_before": other_before, "other_after": other_before, "added": {}, "error": str(exc)}

        if not suggestions:
            return {"pack_id": pack_id, "other_before": other_before, "other_after": other_before, "added": {}, "msg": "LLM 无补充建议"}

        # 4. 更新 pack JSON + manifest
        added = self._update_pack_keywords(pack_id, suggestions)

        # 5. reload（清 loader 缓存）
        industry_pack_loader.clear_cache(pack_id)

        # 6. 重 aggregate（after）
        clusters_after = self.repository.aggregate_subject_clusters(pack_id=pack_id, days=90)
        other_after_c = next((c for c in clusters_after if c.get("subject_type") == "topic" and c.get("topic_key") == "other"), None)
        other_after = int(other_after_c["article_count"]) if other_after_c else 0

        logger.info("recluster: pack=%s added=%d keywords, 其它 %d→%d", pack_id, sum(len(v) for v in added.values()), other_before, other_after)
        return {
            "pack_id": pack_id,
            "other_before": other_before,
            "other_after": other_after,
            "added": added,
        }

    def _update_pack_keywords(self, pack_id: str, suggestions: Dict[str, List[str]]) -> Dict[str, List[str]]:
        """更新 pack JSON 文件 + DB 发布 manifest 的 fixed_topics keywords。"""
        added: Dict[str, List[str]] = {}
        # pack JSON 文件
        pack_file = os.path.join(industry_pack_loader.config_dir, f"{pack_id}.json")
        if os.path.exists(pack_file):
            with open(pack_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            for ft in d.get("fixed_topics", []):
                key = ft.get("key")
                if key in suggestions:
                    old = list(ft.get("keywords") or [])
                    ft["keywords"] = list(dict.fromkeys(old + suggestions[key]))
                    added[key] = suggestions[key]
            with open(pack_file, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
        # DB 发布 manifest
        from db_connection import connect_database
        conn = connect_database()
        try:
            row = conn.execute(
                "SELECT id, manifest_json FROM industry_pack_versions "
                "WHERE industry_pack_id=? ORDER BY published_at DESC, id DESC LIMIT 1",
                (pack_id,),
            ).fetchone()
            if row:
                m = json.loads(row[1])
                for ft in m.get("fixed_topics", []):
                    key = ft.get("key")
                    if key in suggestions:
                        old = list(ft.get("keywords") or [])
                        ft["keywords"] = list(dict.fromkeys(old + suggestions[key]))
                new_manifest = json.dumps(m, ensure_ascii=False, sort_keys=True)
                new_sha = hashlib.sha256(new_manifest.encode("utf-8")).hexdigest()
                conn.execute(
                    "UPDATE industry_pack_versions SET manifest_json=?, content_sha256=? WHERE id=?",
                    (new_manifest, new_sha, row[0]),
                )
                conn.commit()
        finally:
            conn.close()
        return added
