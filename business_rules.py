#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 9 · 行业规则引擎：把"条件 → 动作"表化（`intel_business_rules`）。

规则从哪来（`source` 留痕）：
  · `pack_config`：行业包的 `core_keywords`（关键词条件）与 `fixed_topics`（主题条件）
  · `attention`  ：`pack_attention_directions` 里 active 的追踪方向（含关键词）
动作只做**检索侧增强**（补检索式 / 加权词 / 要求全文），不改证据闸门、不改引用校验；
也不改变单跳问题的既有路径——没有规则命中时，规划结果与以前**逐字一致**。
"""
from __future__ import annotations

import json
from typing import Dict, Iterable, List, Mapping

MAX_ACTION_QUERIES = 4
MAX_BOOST_TERMS = 8


def _json_load(value, default):
    if isinstance(value, (dict, list)):
        return value
    text = str(value or "").strip()
    if not text:
        return default
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _clean_terms(values: Iterable) -> List[str]:
    terms: List[str] = []
    for item in values or []:
        text = " ".join(str(item or "").split())
        if text and text not in terms:
            terms.append(text)
    return terms


class BusinessRuleEngine:
    """规则的读写与命中判定（纯规则，不调用 LLM）。"""

    def __init__(self, repository=None):
        if repository is None:
            from intel_database import intel_repository

            repository = intel_repository
        self.repository = repository

    # ── 写：从包配置/追踪方向迁移 ──────────────────────────────────────
    def sync_from_pack(self, pack_id: str, pack: Mapping) -> Dict:
        """把包的配置项迁移成规则（幂等：同 rule_key 覆盖，保留人工加的规则）。"""
        pack_id = str(pack_id or "").strip()
        pack = pack or {}
        version = str(pack.get("version") or "")
        rows: List[Dict] = []

        for term in _clean_terms(pack.get("core_keywords")):
            rows.append({
                "rule_key": "keyword:%s" % term,
                "rule_type": "keyword",
                "condition": {"any_terms": [term]},
                "action": {"boost_terms": [term], "reason": "行业核心关键词"},
                "source": "pack_config",
                "priority": 20,
            })
        for topic in pack.get("fixed_topics") or []:
            name = ""
            queries: List[str] = []
            if isinstance(topic, Mapping):
                name = str(topic.get("name") or topic.get("title") or topic.get("topic") or "").strip()
                queries = _clean_terms(topic.get("search_queries") or topic.get("queries") or [])
            else:
                name = str(topic or "").strip()
            if not name:
                continue
            rows.append({
                "rule_key": "topic:%s" % name,
                "rule_type": "topic",
                "condition": {"any_terms": [name]},
                "action": {"retrieval_queries": queries[:MAX_ACTION_QUERIES],
                           "boost_terms": [name], "reason": "包内固定主题"},
                "source": "pack_config",
                "priority": 30,
            })

        # 追踪方向（按周存放、只取 active）：方向文本本身是条件，关键词是动作增强
        try:
            from pack_attention import list_directions

            for item in list_directions(pack_id) or []:
                if str(item.get("status") or "active") != "active":
                    continue
                direction = str(item.get("direction") or "").strip()
                if not direction:
                    continue
                keywords = _clean_terms(_json_load(item.get("keywords_json"), []))
                rows.append({
                    "rule_key": "attention:%s:%s" % (str(item.get("week_key") or ""), direction),
                    "rule_type": "attention",
                    "condition": {"any_terms": [direction] + keywords},
                    "action": {"boost_terms": keywords[:MAX_BOOST_TERMS],
                               "note": str(item.get("reason") or ""),
                               "reason": "本周追踪方向"},
                    "source": "attention",
                    "priority": 40,
                })
        except Exception:
            pass

        written = self.repository.upsert_business_rules(pack_id=pack_id, rules=rows, version=version)
        return {"pack_id": pack_id, "rules": len(rows), "written": written}

    # ── 读：命中判定 ───────────────────────────────────────────────────
    def list_rules(self, pack_id: str = "", *, enabled_only: bool = True) -> List[Dict]:
        return self.repository.list_business_rules(pack_id=pack_id, enabled_only=enabled_only)

    def match(self, text: str, *, pack_id: str = "", category: str = "",
              limit_rules: int = 3) -> Dict:
        """按问题文本命中规则，汇总成一份**规划补丁**（不改闸门，只补检索式与加权词）。

        返回 {"matched": [...], "retrieval_queries": [...], "boost_terms": [...],
              "require_fulltext": bool, "note": "..."}；没命中就返回空补丁。
        """
        haystack = str(text or "")
        matched: List[Dict] = []
        queries: List[str] = []
        boosts: List[str] = []
        require_fulltext = False
        notes: List[str] = []

        for rule in self.list_rules(pack_id):
            condition = _json_load(rule.get("condition_json"), {})
            terms = _clean_terms(condition.get("any_terms"))
            if not terms or not any(term in haystack for term in terms):
                continue
            action = _json_load(rule.get("action_json"), {})
            matched.append({
                "rule_key": str(rule.get("rule_key") or ""),
                "rule_type": str(rule.get("rule_type") or ""),
                "source": str(rule.get("source") or ""),
                "hit_terms": [term for term in terms if term in haystack][:4],
            })
            for query in _clean_terms(action.get("retrieval_queries")):
                if query not in queries:
                    queries.append(query)
            for term in _clean_terms(action.get("boost_terms")):
                if term not in boosts:
                    boosts.append(term)
            if action.get("require_fulltext"):
                require_fulltext = True
            if action.get("note"):
                notes.append(str(action["note"])[:80])
            if len(matched) >= max(1, int(limit_rules)):
                break

        return {
            "matched": matched,
            "retrieval_queries": queries[:MAX_ACTION_QUERIES],
            "boost_terms": boosts[:MAX_BOOST_TERMS],
            "require_fulltext": bool(require_fulltext),
            "note": "；".join(notes)[:200],
            "category": str(category or ""),
        }


business_rule_engine = BusinessRuleEngine()

__all__ = ["BusinessRuleEngine", "business_rule_engine"]
