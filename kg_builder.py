#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识图谱归并（阶段 8）：把已有结构化产物连成可遍历的 kg_nodes / kg_edges。

设计边界（照《爬虫改进实施步骤》阶段 8）：
  · **不重复抽取**：数据来源就是 `intel_article_events` + `intel_subject_canonical` + `intel_topics`；
  · 图是**派生视图**，源表为准 —— 归并是幂等 UPSERT，随时可删表重建；
  · 不做在线图数据库，PostgreSQL/SQLite 表 + 索引即可（邻域查询走 src/dst 索引）。

节点：
  · 实体节点：`intel_subject_canonical.subject_key`（同一主体跨文章聚合的关键）；
  · 主题节点：`intel_topics.topic_key`。
边：
  · 事件边：`(subject_key, action, object_key)`，带 article_id / event_time / confidence / evidence_ref。
    object 也要成为节点：客体是"被作用的对象"（机构/产品/政策），本来就该在图里可遍历。
  · 事件边里若某主体只有 `subject_text` 而没有 canonical 行，用 `_norm_key(subject)` 兜底，
    保证"归并覆盖率 ≥95%"这件事在**数据缺失**时也说得清（缺口会落进 `unmapped` 统计）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

PLACEHOLDER_SUBJECTS = ("__no_event__", "__error__")

# 事件类型 → 置信度基线：监管/处罚这类"确定性公文"高于模型推测的 release/market
_EVENT_TYPE_CONFIDENCE = {
    "regulation": 0.9,
    "enforcement": 0.9,
    "release": 0.8,
    "transaction": 0.85,
    "market": 0.6,
    "other": 0.5,
}


def _norm_key(value: str) -> str:
    """与 subject_normalize_service._norm_key 同一口径（去空白/括号/标点、小写）。"""
    text = str(value or "").lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[（(].*?[)）]", "", text)
    text = re.sub(r"[^一-鿿a-z0-9]", "", text)
    return text


def _node_key(value: str) -> str:
    key = _norm_key(value)
    return key[:120]


def _edge_key(src: str, action: str, dst: str, article_id: int) -> str:
    """边身份：同一篇文章里"同一对实体 + 同一动作"只算一条边（重跑归并幂等）。"""
    raw = "|".join([src, _norm_key(action), dst, str(int(article_id or 0))])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class KnowledgeGraphBuilder:
    def __init__(self, repository=None):
        if repository is None:
            from intel_database import IntelRepository

            repository = IntelRepository()
        self.repository = repository
        self.db = repository.db

    # ── 读源表 ──────────────────────────────────────────────────────────
    def _fetch(self, sql: str, params=()) -> List[Dict]:
        self.repository._ensure()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(sql, tuple(params))
                return [dict(row) for row in cursor.fetchall()]
            finally:
                cursor.close()

    def _subject_key_map(self, pack_id: str) -> Dict[str, str]:
        """subject_text → subject_key（按包隔离；同文本可能在不同包有不同规范名）。"""
        params = []
        where = ""
        if pack_id:
            where = "WHERE industry_pack_id=?"
            params.append(pack_id)
        rows = self._fetch(
            f"SELECT industry_pack_id, subject_text, canonical_name, subject_key"
            f" FROM intel_subject_canonical {where}",
            params,
        )
        mapping = {}
        for row in rows:
            key = str(row.get("subject_key") or "") or _norm_key(row.get("canonical_name") or "")
            if not key:
                continue
            mapping[(str(row.get("industry_pack_id") or ""), str(row.get("subject_text") or ""))] = key
        return mapping

    def _events(self, pack_id: str) -> List[Dict]:
        params = list(PLACEHOLDER_SUBJECTS)
        where = "e.subject NOT IN (?, ?)"
        if pack_id:
            where += " AND e.industry_pack_id=?"
            params.append(pack_id)
        return self._fetch(
            f"""
            SELECT e.article_id, e.industry_pack_id, e.subject, e.subject_type, e.action,
                   e.object, e.entities_json, e.event_time, e.event_type,
                   e.state_before, e.state_after, e.event_hash
            FROM intel_article_events e
            JOIN articles a ON a.id=e.article_id AND a.status='active'
            WHERE {where}
            """,
            params,
        )

    # ── 归并 ────────────────────────────────────────────────────────────
    def build(self, *, pack_id: str = "", apply: bool = True) -> Dict:
        """从源表归并出节点与边。apply=False 时只统计不写库（用于只读巡检）。"""
        pack_id = str(pack_id or "").strip()
        subject_keys = self._subject_key_map(pack_id)
        events = self._events(pack_id)
        topics = self._fetch(
            "SELECT industry_pack_id, topic_key, topic_name FROM intel_topics"
            + (" WHERE industry_pack_id=?" if pack_id else ""),
            [pack_id] if pack_id else [],
        )

        nodes: Dict[tuple, Dict] = {}
        edges: List[Dict] = []
        unmapped_subjects: Dict[str, int] = {}
        unmapped_objects = 0

        def add_node(key: str, node_type: str, label: str, pack: str) -> None:
            if not key:
                return
            ident = (pack, node_type, key)
            node = nodes.setdefault(ident, {
                "node_key": key, "node_type": node_type, "label": label or key,
                "industry_pack_id": pack, "article_ids": set(), "event_count": 0,
                "first_seen": "", "last_seen": "",
            })
            if label and (not node["label"] or node["label"] == key):
                node["label"] = label

        for topic in topics:
            key = _node_key(topic.get("topic_key"))
            add_node(key, "topic", str(topic.get("topic_name") or key),
                     str(topic.get("industry_pack_id") or ""))

        for event in events:
            pack = str(event.get("industry_pack_id") or "")
            subject_text = str(event.get("subject") or "").strip()
            object_text = str(event.get("object") or "").strip()
            subject_key = subject_keys.get((pack, subject_text))
            if not subject_key:
                subject_key = _node_key(subject_text)
                if subject_key:
                    unmapped_subjects[subject_text] = unmapped_subjects.get(subject_text, 0) + 1
            object_key = _node_key(object_text)
            if object_text and not object_key:
                unmapped_objects += 1
            article_id = int(event.get("article_id") or 0)
            event_time = str(event.get("event_time") or "")
            confidence = _EVENT_TYPE_CONFIDENCE.get(str(event.get("event_type") or "other"), 0.5)

            add_node(subject_key, "entity", subject_text, pack)
            if object_key:
                add_node(object_key, "entity", object_text, pack)
            for entity in _json_list(event.get("entities_json")):
                entity_key = _node_key(entity)
                # 实体清单里的词只是"出现过"，不造边（避免把共现当因果关系）
                if entity_key and entity_key not in (subject_key, object_key):
                    add_node(entity_key, "entity", entity, pack)

            for item in (subject_key, object_key):
                if not item:
                    continue
                node = nodes.get((pack, "entity", item))
                if node is not None:
                    node["event_count"] += 1
                    if article_id:
                        node["article_ids"].add(article_id)
                    if event_time:
                        if not node["first_seen"] or event_time < node["first_seen"]:
                            node["first_seen"] = event_time
                        if not node["last_seen"] or event_time > node["last_seen"]:
                            node["last_seen"] = event_time

            if not subject_key or not object_key:
                continue
            edges.append({
                "edge_key": _edge_key(subject_key, str(event.get("action") or ""), object_key, article_id),
                "src_key": subject_key,
                "dst_key": object_key,
                "action": str(event.get("action") or "")[:160],
                "event_type": str(event.get("event_type") or "other"),
                "industry_pack_id": pack,
                "article_id": article_id or None,
                "event_time": event_time[:20],
                "confidence": confidence,
                "evidence_ref": f"article:{article_id}" if article_id else "",
                "state_before": str(event.get("state_before") or "")[:120],
                "state_after": str(event.get("state_after") or "")[:120],
            })

        node_rows = []
        for node in nodes.values():
            node_rows.append({
                "node_key": node["node_key"], "node_type": node["node_type"],
                "label": node["label"][:200], "industry_pack_id": node["industry_pack_id"],
                "article_count": len(node["article_ids"]), "event_count": node["event_count"],
                "first_seen": node["first_seen"], "last_seen": node["last_seen"],
            })
        edge_coverage = (len(edges) / float(len(events))) if events else None
        summary = {
            "pack_id": pack_id or "*",
            "events": len(events),
            "nodes": len(node_rows),
            "edges": len(edges),
            "edge_coverage": round(edge_coverage, 4) if edge_coverage is not None else None,
            "topics": len(topics),
            "unmapped_subjects": len(unmapped_subjects),
            "unmapped_subject_samples": sorted(
                unmapped_subjects.items(), key=lambda item: -item[1])[:10],
            "unmapped_objects": unmapped_objects,
            "applied": bool(apply),
        }
        if not apply:
            return summary
        summary["written_nodes"] = self._write_nodes(node_rows)
        summary["written_edges"] = self._write_edges(edges)
        return summary

    # ── 写库（幂等 UPSERT）─────────────────────────────────────────────
    def _write_nodes(self, rows: List[Dict]) -> int:
        if not rows:
            return 0
        now = _utc_now()
        payload = [
            (row["industry_pack_id"], row["node_type"], row["node_key"], row["label"],
             row["article_count"], row["event_count"], row["first_seen"], row["last_seen"],
             now, now)
            for row in rows
        ]
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.executemany(
                    """
                    INSERT INTO kg_nodes (
                        industry_pack_id, node_type, node_key, label,
                        article_count, event_count, first_seen, last_seen, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(industry_pack_id, node_type, node_key) DO UPDATE SET
                        label=excluded.label,
                        article_count=excluded.article_count,
                        event_count=excluded.event_count,
                        first_seen=excluded.first_seen,
                        last_seen=excluded.last_seen,
                        updated_at=excluded.updated_at
                    """,
                    payload,
                )
                self.db.connection.commit()
                return len(payload)
            finally:
                cursor.close()

    def _write_edges(self, rows: List[Dict]) -> int:
        if not rows:
            return 0
        now = _utc_now()
        # 同一批里也可能有重复 edge_key（不同事件行归一后撞车）→ 先按边身份去重
        dedup: Dict[str, Dict] = {}
        for row in rows:
            dedup[row["edge_key"]] = row
        payload = [
            (row["edge_key"], row["src_key"], row["dst_key"], row["action"], row["event_type"],
             row["industry_pack_id"], row["article_id"], row["event_time"], row["confidence"],
             row["evidence_ref"], row["state_before"], row["state_after"], now, now)
            for row in dedup.values()
        ]
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.executemany(
                    """
                    INSERT INTO kg_edges (
                        edge_key, src_key, dst_key, action, event_type, industry_pack_id,
                        article_id, event_time, confidence, evidence_ref, state_before,
                        state_after, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(edge_key) DO UPDATE SET
                        action=excluded.action,
                        event_type=excluded.event_type,
                        event_time=excluded.event_time,
                        confidence=excluded.confidence,
                        evidence_ref=excluded.evidence_ref,
                        state_before=excluded.state_before,
                        state_after=excluded.state_after,
                        updated_at=excluded.updated_at
                    """,
                    payload,
                )
                self.db.connection.commit()
                return len(payload)
            finally:
                cursor.close()

    # ── 图查询 ─────────────────────────────────────────────────────────
    def neighborhood(self, node_key: str, *, pack_id: str = "", depth: int = 1,
                     since: str = "", until: str = "", limit: int = 50) -> Dict:
        """按实体/主题取邻域（默认 1 跳），可按时间过滤。

        返回 {node, neighbors: [...], edges: [...], stats}；图是派生视图，
        每条边都带 article_id / evidence_ref，可回溯到原始事件行与文章。
        """
        key = _node_key(node_key)
        if not key:
            return {"node": None, "neighbors": [], "edges": [], "stats": {"depth": 0}}
        depth = max(1, min(int(depth or 1), 2))
        limit = max(1, min(int(limit or 50), 200))
        pack = str(pack_id or "").strip()
        visited = {key}
        frontier = {key}
        all_edges: List[Dict] = []
        for _ in range(depth):
            next_frontier = set()
            for current in frontier:
                all_edges.extend(self._edges_touching(current, pack, since, until, limit))
            for edge in all_edges:
                for endpoint in (edge["src_key"], edge["dst_key"]):
                    if endpoint not in visited:
                        visited.add(endpoint)
                        next_frontier.add(endpoint)
            frontier = next_frontier
            if not frontier:
                break
        # 去重（同一条边可能在两端各被取到一次）
        unique_edges = {edge["edge_key"]: edge for edge in all_edges}
        node_row = None
        rows = self._fetch(
            "SELECT node_key, node_type, label, industry_pack_id, article_count, event_count,"
            " first_seen, last_seen FROM kg_nodes WHERE node_key=?"
            + (" AND industry_pack_id=?" if pack else ""),
            [key, pack] if pack else [key],
        )
        if rows:
            node_row = rows[0]
        neighbor_keys = sorted(visited - {key})[:limit]
        neighbors = []
        if neighbor_keys:
            marks = ",".join("?" for _ in neighbor_keys)
            params = list(neighbor_keys) + ([pack] if pack else [])
            neighbors = self._fetch(
                f"SELECT node_key, node_type, label, industry_pack_id, article_count, event_count,"
                f" first_seen, last_seen FROM kg_nodes WHERE node_key IN ({marks})"
                + (" AND industry_pack_id=?" if pack else ""),
                params,
            )
        return {
            "node": node_row,
            "neighbors": neighbors,
            "edges": list(unique_edges.values()),
            "stats": {"depth": depth, "edges": len(unique_edges),
                      "neighbors": len(neighbors), "since": since, "until": until},
        }

    def _edges_touching(self, key: str, pack: str, since: str, until: str,
                        limit: int) -> List[Dict]:
        where = ["(src_key=? OR dst_key=?)"]
        params: List = [key, key]
        if pack:
            where.append("industry_pack_id=?")
            params.append(pack)
        if since:
            where.append("COALESCE(event_time,'') >= ?")
            params.append(str(since)[:20])
        if until:
            where.append("COALESCE(event_time,'') <= ?")
            params.append(str(until)[:20])
        params.append(limit)
        return self._fetch(
            "SELECT edge_key, src_key, dst_key, action, event_type, industry_pack_id,"
            " article_id, event_time, confidence, evidence_ref, state_before, state_after"
            " FROM kg_edges WHERE " + " AND ".join(where) +
            " ORDER BY COALESCE(event_time,'') DESC, id DESC LIMIT ?",
            params,
        )


def _json_list(value) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]
