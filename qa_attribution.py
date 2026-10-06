#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""证据解析（答案分解 + 归因）与词元影响力。

为什么需要：用户看到答案却不知道"为什么召回这几条证据、为什么这句话引这篇"。
本模块把最终答案拆成信息单元，再把每个单元映射回**本次 run 的证据文档里的具体句子**，
给出关系（支持/反驳/无关）与置信度；词元影响力（第 7 项一期）复用同一套
"单元 → 证据句"结构，只是在链路上多记一层"哪几个词推动了这句话"。

设计要点（与项目既有契约对齐，尽量不动主干）：
  - 复用 `qa_evidence` / `qa_claims` / `qa_claim_evidence`，不另立一套证据体系；
  - 复用 `qa_level1.OpenAIJsonModelClient` + `QaProviderRegistry` 走既有的本地/远端模型路由；
  - 复用 `qa_resilience` 的熔断与预算，避免归因把 LLM 额度吃光；
  - 结果落库并与 run 绑定，重复点击不重复算（除非 force）。

降级策略（很关键，归因是"锦上添花"，绝不能拖垮问答）：
  1. LLM 不可用 → 走纯规则的"句级召回 + 词汇重叠"，relation='mention'，method='lexical'；
  2. 证据片段太短（只有一句话）→ 取 `content_excerpt`，再从原文补足候选句；
  3. 任何异常都吞掉并返回已有结果，绝不让按钮点了报 500。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Mapping

# 单个 run 最多分解多少个信息单元、每个单元最多判定多少条候选句
_MAX_UNITS = 24
_MAX_CANDIDATES = 6
_MAX_LLM_CALLS = 26
_MIN_UNIT_CHARS = 6

# 句子切分：中英句末标点 + 换行/项目符号
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*|\n+")
_INLINE_CITATION = re.compile(r"\[\d{1,2}\]|【\d{1,2}】")
_BULLET_PREFIX = re.compile(r"^\s*(?:[-*•·]|\d+[.、)]|第[一二三四五六七八九十]+[、.])\s*")
_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{1,}|[\u4e00-\u9fff]{2,}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(text: object) -> str:
    value = _INLINE_CITATION.sub("", str(text or ""))
    value = _BULLET_PREFIX.sub("", value)
    return " ".join(value.split()).strip()


def _terms(text: str) -> set:
    """中英混合的粗粒度词元：英文按词、中文按 2 字以上连续片段 + 2-gram。"""
    out = set()
    for token in _TERM_RE.findall(str(text or "")):
        lowered = token.casefold()
        out.add(lowered)
        if len(token) >= 4 and re.fullmatch(r"[\u4e00-\u9fff]+", token):
            for index in range(len(token) - 1):
                out.add(token[index:index + 2])
    return out


def _overlap(left: str, right: str) -> float:
    a, b = _terms(left), _terms(right)
    if not a or not b:
        return 0.0
    return len(a & b) / float(max(1, min(len(a), len(b))))


def split_sentences(text: object, *, limit: int = 60) -> list:
    out = []
    for chunk in _SENTENCE_SPLIT.split(str(text or "")):
        cleaned = _clean(chunk)
        if len(cleaned) >= _MIN_UNIT_CHARS:
            out.append(cleaned)
        if len(out) >= limit:
            break
    return out


class QaAttributionService:
    """答案分解 + 证据归因 + 词元影响力（一期近似法）。"""

    def __init__(self, database, *, store=None, provider_registry=None, llm_client=None):
        from sqlite_database import sqlite_db  # 延迟导入避免循环依赖

        self.database = database or sqlite_db
        if store is None:
            from qa_storage import QaStore

            store = QaStore(self.database)
        self.store = store
        if provider_registry is None:
            from qa_provider_registry import QaProviderRegistry

            provider_registry = QaProviderRegistry()
        self.provider_registry = provider_registry
        self._llm_client = llm_client

    # ------------------------------------------------------------------ 对外接口
    def build(self, run_id: str, *, force: bool = False) -> dict:
        """生成（或读缓存）某个 run 的证据解析结果。"""
        run_id = str(run_id or "")
        if not force:
            cached = self.get(run_id)
            if cached.get("status") == "ready":
                cached["cached"] = True
                return cached
        try:
            result = self._build(run_id)
        except Exception as exc:  # 归因失败不能影响问答本身
            return {
                "success": False, "status": "failed", "run_id": run_id,
                "message": "证据解析未能完成：%s" % str(exc)[:160],
                "units": [], "stats": {},
            }
        return result

    def get(self, run_id: str) -> dict:
        run_id = str(run_id or "")
        self.store.ensure_schema()
        with self.database.lock:
            meta = self.database.connection.execute(
                "SELECT * FROM qa_attribution_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            units = self.database.connection.execute(
                "SELECT * FROM qa_attribution_units WHERE run_id=? ORDER BY order_index,id",
                (run_id,),
            ).fetchall()
            links = self.database.connection.execute(
                "SELECT * FROM qa_attribution_links WHERE run_id=? ORDER BY unit_id,confidence DESC",
                (run_id,),
            ).fetchall()
            tokens = self.database.connection.execute(
                "SELECT unit_id,evidence_ref,sentence_index,token,influence FROM qa_token_influence "
                "WHERE run_id=? ORDER BY influence DESC", (run_id,),
            ).fetchall()
        if not meta and not units:
            return {"success": True, "status": "not_computed", "run_id": run_id,
                    "units": [], "stats": {}}
        meta = dict(meta) if meta else {}
        payload = json.loads(meta.get("payload_json") or "{}")
        return {
            "success": True,
            "status": str(meta.get("status") or "ready"),
            "run_id": run_id,
            "method": str(meta.get("method") or ""),
            "computed_at": str(meta.get("computed_at") or ""),
            "units": self._public_units(
                [dict(item) for item in units],
                [dict(item) for item in links],
                [dict(item) for item in tokens],
            ),
            "stats": payload.get("stats") or {},
            "cached": True,
        }

    def token_influence(self, run_id: str, *, unit_ids=None, top_sentences: int = 3,
                        force: bool = False) -> dict:
        """第 7 项一期：遮挡法近似词元影响力（复用第 6 项的单元→证据句结构）。"""
        run_id = str(run_id or "")
        attribution = self.get(run_id)
        if attribution.get("status") == "not_computed":
            attribution = self.build(run_id)
        if attribution.get("status") != "ready":
            return {"success": False, "status": attribution.get("status") or "failed",
                    "run_id": run_id, "units": [], "message": attribution.get("message") or ""}
        if not force:
            cached = self._cached_influence(run_id)
            if cached:
                cached["cached"] = True
                return cached
        try:
            return self._build_influence(run_id, attribution, unit_ids=unit_ids,
                                         top_sentences=top_sentences)
        except Exception as exc:
            return {"success": False, "status": "failed", "run_id": run_id,
                    "units": [], "message": "词元影响力计算失败：%s" % str(exc)[:160]}

    # ------------------------------------------------------------------ 内部实现
    def _run_row(self, run_id: str) -> dict:
        self.store.ensure_schema()
        with self.database.lock:
            row = self.database.connection.execute(
                "SELECT * FROM qa_runs WHERE id=?", (run_id,)
            ).fetchone()
        if not row:
            raise ValueError("run 不存在")
        return dict(row)

    def _evidence_rows(self, run_id: str) -> list:
        with self.database.lock:
            rows = self.database.connection.execute(
                "SELECT * FROM qa_evidence WHERE run_id=?", (run_id,)
            ).fetchall()
        return [dict(item) for item in rows]

    def _article_content(self, article_id) -> str:
        """证据片段可能只有一句话；从原文章补足候选句。"""
        try:
            aid = int(article_id or 0)
        except (TypeError, ValueError):
            return ""
        if not aid:
            return ""
        try:
            with self.database.lock:
                row = self.database.connection.execute(
                    "SELECT content FROM articles WHERE id=?", (aid,)
                ).fetchone()
            return str((dict(row).get("content") if row else "") or "")[:6000]
        except Exception:
            return ""

    def _candidate_sentences(self, evidence: list) -> list:
        """构造候选句池：证据片段 + 原文补足，按证据分组。"""
        pool = []
        for item in evidence:
            ref = str(item.get("evidence_ref") or "")
            payload = {}
            try:
                payload = json.loads(item.get("payload_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            excerpt = str(payload.get("content_excerpt") or payload.get("excerpt") or "")
            sentences = split_sentences(excerpt, limit=8)
            if len(" ".join(sentences)) < 120:
                full = self._article_content(item.get("article_id"))
                for extra in split_sentences(full, limit=20):
                    if extra not in sentences:
                        sentences.append(extra)
            if not sentences and excerpt:
                sentences = [excerpt[:200]]
            pool.append({
                "evidence_ref": ref,
                "article_id": item.get("article_id"),
                "title": str(item.get("source_title") or payload.get("title") or ""),
                "url": str(item.get("source_url") or ""),
                "sentences": sentences[:16],
            })
        return pool

    def _decompose_rule(self, answer: str, claims: list) -> list:
        units = []
        for index, text in enumerate(split_sentences(answer, limit=_MAX_UNITS), start=1):
            claim = self._best_claim(text, claims)
            units.append({
                "unit_id": "u%d" % index,
                "order_index": index,
                "text": text,
                "claim_type": str((claim or {}).get("claim_type") or ""),
                "claim_key": str((claim or {}).get("claim_key") or ""),
                "needs_evidence": True,
                "method": "rule",
            })
        return units

    @staticmethod
    def _best_claim(text: str, claims: list):
        best, best_score = None, 0.0
        for claim in claims:
            score = _overlap(text, claim.get("claim_text") or "")
            if score > best_score:
                best, best_score = claim, score
        return best if best_score >= 0.35 else None

    def _llm(self):
        if self._llm_client is not None:
            return self._llm_client
        from qa_level1 import OpenAIJsonModelClient

        return OpenAIJsonModelClient()

    def _profile(self, run: Mapping):
        return self.provider_registry.resolve(
            "draft",
            str(run.get("draft_provider_id") or run.get("synthesis_provider_id") or "local"),
            industry_pack_id=str(run.get("industry_pack_id") or ""),
        )

    def _decompose_llm(self, answer: str, claims: list, run: Mapping, budget: dict) -> list:
        """LLM 细化：给单元打 claim_type 并判断是否需要证据。失败返回空列表，由规则结果兜底。"""
        if budget["calls"] >= _MAX_LLM_CALLS:
            return []
        from qa_level1 import extract_json_object

        body = {
            "answer": answer[:4000],
            "claims": [
                {"claim_id": item.get("claim_key"), "text": str(item.get("claim_text") or "")[:300],
                 "type": item.get("claim_type")}
                for item in claims[:12]
            ],
            "required_json_shape": {
                "units": [{"text": "答案中的一个信息单元（尽量保持原文）",
                           "claim_type": "policy_content|current_fact|trend|risk|background|opinion",
                           "needs_evidence": True}]
            },
        }
        budget["calls"] += 1
        raw = self._llm()(
            self._profile(run),
            [
                {"role": "system",
                 "content": "你是答案分解器。只把给定答案拆成信息单元，不新增内容、不回答问题。"
                            "只输出一个 JSON 对象，不要 Markdown。"},
                {"role": "user", "content": json.dumps(body, ensure_ascii=False)},
            ],
            timeout=40,
        )
        parsed = extract_json_object(raw) or {}
        units = []
        for index, item in enumerate(parsed.get("units") or [], start=1):
            text = _clean((item or {}).get("text"))
            if len(text) < _MIN_UNIT_CHARS:
                continue
            units.append({
                "unit_id": "u%d" % index,
                "order_index": index,
                "text": text,
                "claim_type": str((item or {}).get("claim_type") or ""),
                "claim_key": "",
                "needs_evidence": bool((item or {}).get("needs_evidence", True)),
                "method": "llm",
            })
            if len(units) >= _MAX_UNITS:
                break
        return units

    def _recall(self, unit_text: str, pool: list) -> list:
        candidates = []
        for group in pool:
            for index, sentence in enumerate(group["sentences"]):
                score = _overlap(unit_text, sentence)
                if score <= 0:
                    continue
                candidates.append({
                    "evidence_ref": group["evidence_ref"],
                    "article_id": group["article_id"],
                    "title": group["title"],
                    "url": group["url"],
                    "sentence_index": index,
                    "sentence_text": sentence,
                    "recall_score": round(score, 4),
                })
        candidates.sort(key=lambda item: item["recall_score"], reverse=True)
        return candidates[:_MAX_CANDIDATES]

    def _judge_llm(self, unit_text: str, candidates: list, run: Mapping, budget: dict) -> dict:
        """一次调用判定该单元与所有候选句的关系。失败返回空 dict。"""
        if not candidates or budget["calls"] >= _MAX_LLM_CALLS:
            return {}
        from qa_level1 import extract_json_object

        body = {
            "unit": unit_text[:600],
            "candidates": [
                {"id": index + 1, "sentence": item["sentence_text"][:300]}
                for index, item in enumerate(candidates)
            ],
            "required_json_shape": {
                "judgements": [{"id": 1, "relation": "support|refute|irrelevant",
                                "confidence": 0.0, "reason": "一句话"}]
            },
        }
        budget["calls"] += 1
        raw = self._llm()(
            self._profile(run),
            [
                {"role": "system",
                 "content": "你是证据归因判定器。判断每个候选句对给定信息单元是支持、反驳还是无关。"
                            "只依据句子本身，不要引入外部知识；只输出一个 JSON 对象。"},
                {"role": "user", "content": json.dumps(body, ensure_ascii=False)},
            ],
            timeout=40,
        )
        parsed = extract_json_object(raw) or {}
        out = {}
        for item in parsed.get("judgements") or []:
            try:
                index = int((item or {}).get("id") or 0)
            except (TypeError, ValueError):
                continue
            relation = str((item or {}).get("relation") or "").strip().lower()
            if relation not in {"support", "refute", "irrelevant"}:
                relation = "irrelevant"
            try:
                confidence = float((item or {}).get("confidence") or 0)
            except (TypeError, ValueError):
                confidence = 0.0
            out[index] = {
                "relation": relation,
                "confidence": max(0.0, min(1.0, confidence)),
                "reason": str((item or {}).get("reason") or "")[:200],
            }
        return out

    def _build(self, run_id: str) -> dict:
        run = self._run_row(run_id)
        final = json.loads(run.get("final_answer_json") or "{}")
        answer = str(final.get("answer") or final.get("markdown") or "")
        if not answer.strip():
            return {"success": True, "status": "no_answer", "run_id": run_id,
                    "units": [], "stats": {}, "message": "该回答还没有最终答案，无法做证据解析。"}
        claims = self._claim_rows(run_id)
        evidence = self._evidence_rows(run_id)
        if not evidence:
            return {"success": True, "status": "no_evidence", "run_id": run_id,
                    "units": [], "stats": {},
                    "message": "该回答没有可用证据（可能是未启用检索的直答）。"}
        pool = self._candidate_sentences(evidence)
        budget = {"calls": 0}
        # LLM 不可用（端点被安全策略拦、超时、返回非法 JSON）时必须退化为规则分解 +
        # 词汇重叠归因，而不是让整个【证据解析】失败——归因是"看得更清楚"的增强能力。
        try:
            llm_units = self._decompose_llm(answer, claims, run, budget)
        except Exception as exc:
            print("⚠️ 证据解析 LLM 分解失败，改用规则分解: %s" % str(exc)[:120])
            llm_units = []
        units = llm_units or self._decompose_rule(answer, claims)
        method = "llm" if units and units[0].get("method") == "llm" else "lexical"
        links = []
        for unit in units:
            candidates = self._recall(unit["text"], pool)
            judgements = {}
            if candidates:
                try:
                    judgements = self._judge_llm(unit["text"], candidates, run, budget)
                except Exception as exc:
                    print("⚠️ 证据解析 LLM 判定失败，改用词汇重叠: %s" % str(exc)[:100])
                    judgements = {}
            if not candidates:
                continue
            for index, candidate in enumerate(candidates, start=1):
                verdict = judgements.get(index) or {}
                relation = str(verdict.get("relation") or ("mention" if judgements else "mention"))
                confidence = float(verdict.get("confidence") or candidate["recall_score"])
                links.append({
                    "unit_id": unit["unit_id"], **candidate,
                    "relation": relation,
                    "confidence": round(min(1.0, max(0.0, confidence)), 4),
                    "method": "llm" if verdict else "lexical",
                    "reason": str(verdict.get("reason") or ""),
                })
            if judgements:
                method = "llm"
        self._persist(run_id, units, links, method=method, llm_calls=budget["calls"])
        return self.get(run_id) | {"cached": False}

    def _claim_rows(self, run_id: str) -> list:
        with self.database.lock:
            rows = self.database.connection.execute(
                "SELECT * FROM qa_claims WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        seen, out = set(), []
        for row in rows:
            item = dict(row)
            key = str(item.get("claim_key") or "")
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    def _persist(self, run_id: str, units: list, links: list, *, method: str, llm_calls: int) -> None:
        self.store.ensure_schema()
        now = _now()
        linked_units = {item["unit_id"] for item in links if item.get("relation") == "support"}
        stats = {
            "units": len(units),
            "links": len(links),
            "supported_units": len(linked_units),
            "coverage": round(len(linked_units) / float(len(units)), 4) if units else 0.0,
            "llm_calls": int(llm_calls),
        }
        with self.database.lock:
            cursor = self.database.connection.cursor()
            try:
                cursor.execute("DELETE FROM qa_attribution_links WHERE run_id=?", (run_id,))
                cursor.execute("DELETE FROM qa_attribution_units WHERE run_id=?", (run_id,))
                for unit in units:
                    cursor.execute(
                        """
                        INSERT INTO qa_attribution_units(
                            run_id,unit_id,order_index,text,claim_type,needs_evidence,method,payload_json,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?)
                        """,
                        (run_id, unit["unit_id"], int(unit.get("order_index") or 0), unit["text"],
                         str(unit.get("claim_type") or ""), 1 if unit.get("needs_evidence", True) else 0,
                         str(unit.get("method") or ""), json.dumps(unit, ensure_ascii=False), now),
                    )
                for link in links:
                    cursor.execute(
                        """
                        INSERT INTO qa_attribution_links(
                            run_id,unit_id,evidence_ref,article_id,sentence_index,sentence_text,
                            relation,confidence,recall_score,method,payload_json,created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(run_id,unit_id,evidence_ref,sentence_index) DO UPDATE SET
                            relation=excluded.relation,confidence=excluded.confidence,
                            sentence_text=excluded.sentence_text,recall_score=excluded.recall_score,
                            method=excluded.method,payload_json=excluded.payload_json,
                            updated_at=excluded.updated_at
                        """,
                        (run_id, link["unit_id"], link["evidence_ref"], link.get("article_id"),
                         int(link.get("sentence_index") or 0), link["sentence_text"],
                         link["relation"], float(link["confidence"]), float(link["recall_score"]),
                         link["method"], json.dumps(link, ensure_ascii=False), now, now),
                    )
                cursor.execute(
                    """
                    INSERT INTO qa_attribution_runs(run_id,status,method,units,links,llm_calls,payload_json,computed_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        status=excluded.status,method=excluded.method,units=excluded.units,
                        links=excluded.links,llm_calls=excluded.llm_calls,
                        payload_json=excluded.payload_json,updated_at=excluded.updated_at
                    """,
                    (run_id, "ready", method, len(units), len(links), int(llm_calls),
                     json.dumps({"stats": stats}, ensure_ascii=False), now, now),
                )
                self.database.connection.commit()
            except Exception:
                self.database.connection.rollback()
                raise
            finally:
                cursor.close()

    @staticmethod
    def _public_units(units: list, links: list, tokens: list = None) -> list:
        token_index = {}
        for row in tokens or []:
            token_index.setdefault(
                (str(row.get("unit_id") or ""), str(row.get("evidence_ref") or ""),
                 int(row.get("sentence_index") or 0)), []
            ).append({
                "token": str(row.get("token") or ""),
                "influence": float(row.get("influence") or 0),
            })
        by_unit = {}
        for link in links:
            key = (str(link.get("unit_id") or ""), str(link.get("evidence_ref") or ""),
                   int(link.get("sentence_index") or 0))
            by_unit.setdefault(str(link.get("unit_id")), []).append({
                "evidence_ref": str(link.get("evidence_ref") or ""),
                "article_id": link.get("article_id"),
                "sentence_index": int(link.get("sentence_index") or 0),
                "sentence": str(link.get("sentence_text") or ""),
                "relation": str(link.get("relation") or ""),
                "confidence": float(link.get("confidence") or 0),
                "recall_score": float(link.get("recall_score") or 0),
                "method": str(link.get("method") or ""),
                "influence_score": (
                    float(link["influence_score"]) if link.get("influence_score") is not None else None
                ),
                "influence_method": str(link.get("influence_method") or ""),
                "tokens": token_index.get(key, []),
            })
        out = []
        for unit in units:
            payload = {}
            try:
                payload = json.loads(unit.get("payload_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            out.append({
                "unit_id": str(unit.get("unit_id") or ""),
                "order_index": int(unit.get("order_index") or 0),
                "text": str(unit.get("text") or ""),
                "claim_type": str(unit.get("claim_type") or ""),
                "needs_evidence": bool(unit.get("needs_evidence")),
                "method": str(unit.get("method") or payload.get("method") or ""),
                "links": by_unit.get(str(unit.get("unit_id")), []),
            })
        return out

    # ------------------------------------------------------------------ 第 7 项一期
    def _cached_influence(self, run_id: str) -> dict:
        with self.database.lock:
            rows = self.database.connection.execute(
                "SELECT * FROM qa_attribution_links WHERE run_id=? AND influence_method != ''",
                (run_id,),
            ).fetchall()
        if not rows:
            return {}
        attribution = self.get(run_id)
        attribution["influence_ready"] = True
        return attribution

    def _build_influence(self, run_id: str, attribution: dict, *, unit_ids=None,
                         top_sentences: int = 3) -> dict:
        """遮挡法（leave-one-out）近似词元影响力。

        真实梯度接口拿不到（生产用 llama.cpp/OpenAI 兼容 HTTP，只暴露生成接口），
        所以一期用**遮挡 + 对数概率差**：把某条证据句从上下文里拿掉，重算目标文本的
        平均对数概率（token 级），差值即该句的影响力；再把差值按句内词元做归因
        （去掉某个词元后同样重算），得到"词元 → 证据句 → 文章"的链路。
        拿不到对数概率时退化为"相似度落差"代理，并在结果里标明 method。
        """
        run = self._run_row(run_id)
        selected = set(str(item) for item in (unit_ids or []) if str(item or "").strip())
        units = [unit for unit in attribution.get("units") or [] if not selected or unit["unit_id"] in selected]
        scorer = self._make_scorer(run)
        updated = 0
        degraded = 0
        for unit in units:
            all_links = list(unit.get("links") or [])
            relevant = [item for item in all_links
                        if item.get("relation") in {"support", "mention", "refute"}]
            # 优先算"确实相关"的证据句；若该单元全是 irrelevant（LLM 判为无关），
            # 也要给出影响力——"这句话对结论几乎没有影响"本身就是用户想知道的信息。
            links = (relevant or all_links)[:max(1, int(top_sentences))]
            if not links:
                continue
            sentences = [item["sentence"] for item in links]
            # 对数概率路径对"续写能否命中目标"敏感：某个单元算不出来时，单独退化到代理法，
            # 而不是让整个 run 的词元影响力失败（这本来就是"近似"能力）。
            unit_scorer = scorer
            try:
                baseline = float(unit_scorer(unit["text"], sentences))
            except Exception:
                unit_scorer = _OverlapScorer()
                degraded += 1
                baseline = float(unit_scorer(unit["text"], sentences))
            for index, link in enumerate(links):
                others = [text for position, text in enumerate(sentences) if position != index]
                try:
                    without = float(unit_scorer(unit["text"], others))
                except Exception:
                    continue
                influence = round(baseline - without, 6)
                try:
                    token_scores = unit_scorer.token_influence(unit["text"], sentences, index)
                except Exception:
                    token_scores = []
                self._store_influence(run_id, unit["unit_id"], link, influence,
                                      unit_scorer.method, token_scores)
                updated += 1
        return {
            "success": True, "status": "ready", "run_id": run_id,
            "method": scorer.method, "updated_links": updated,
            "degraded_units": degraded,
            "units": self.get(run_id).get("units") or [],
            "scorer_note": scorer.note or (
                "有 %d 个单元的续写不可比，已单独退化为词元重叠代理。" % degraded if degraded else ""
            ),
        }

    def _make_scorer(self, run: Mapping):
        """优先用能给出 token 对数概率的打分接口；否则退化为相似度落差代理。"""
        try:
            scorer = _LogprobScorer(self, run)
            if scorer.available():
                return scorer
        except Exception:
            pass
        return _OverlapScorer()

    def _store_influence(self, run_id: str, unit_id: str, link: dict, influence: float,
                         method: str, token_scores: list) -> None:
        now = _now()
        with self.database.lock:
            cursor = self.database.connection.cursor()
            try:
                cursor.execute(
                    """
                    UPDATE qa_attribution_links
                       SET influence_score=?, influence_method=?, updated_at=?
                     WHERE run_id=? AND unit_id=? AND evidence_ref=? AND sentence_index=?
                    """,
                    (float(influence), method, now, run_id, unit_id,
                     str(link.get("evidence_ref") or ""), int(link.get("sentence_index") or 0)),
                )
                for token, value in token_scores:
                    cursor.execute(
                        """
                        INSERT INTO qa_token_influence(
                            run_id,unit_id,evidence_ref,sentence_index,token,influence,method,created_at
                        ) VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(run_id,unit_id,evidence_ref,sentence_index,token) DO UPDATE SET
                            influence=excluded.influence,method=excluded.method
                        """,
                        (run_id, unit_id, str(link.get("evidence_ref") or ""),
                         int(link.get("sentence_index") or 0), str(token), float(value), method, now),
                    )
                self.database.connection.commit()
            except Exception:
                self.database.connection.rollback()
                raise
            finally:
                cursor.close()


class _OverlapScorer:
    """无对数概率时的代理打分：用词元重叠度当"支持度"，遮挡后落差即影响力。

    这是方案第 7 项一期明确允许的近似法（"实务上多用近似"）。它衡量的是
    "这条证据句与结论的词汇关联有多强"，不是模型内部概率，所以 method 里必须写明 proxy，
    前端也要显示来源，避免把代理值当成真实梯度归因。
    """

    method = "occlusion_overlap_proxy"
    note = "本地打分接口/可反向传播模型不可用，已退化为词元重叠代理（方案第 7 项一期允许的近似法）。"

    def __call__(self, target: str, sentences: list) -> float:
        if not sentences:
            return 0.0
        return sum(_overlap(target, item) for item in sentences) / float(len(sentences))

    def token_influence(self, target: str, sentences: list, index: int) -> list:
        """句内词元归因：把该句里命中的目标词元逐个去掉，看重叠度落差。"""
        sentence = str(sentences[index] or "")
        base = _overlap(target, sentence)
        scored = []
        for token in sorted(_terms(target), key=len, reverse=True)[:12]:
            if token not in sentence.casefold():
                continue
            reduced = sentence.replace(token, "", 1)
            scored.append((token, round(base - _overlap(target, reduced), 6)))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored


class _LogprobScorer:
    """遮挡 + token 对数概率打分（第 7 项一期的正解路径）。

    两条后端，按可用性自动选择：
      1. HTTP 打分：OpenAI 兼容 /v1/completions 的 `echo+logprobs`（能拿到 prompt token 的
         对数概率）；失败则试 llama.cpp /completion 的 `logprobs/n_probs`（生成式打分）。
      2. 本地 transformers：配置 `QA_INFLUENCE_MODEL_PATH` 后本地加载小模型做前向打分
         （不需要梯度，只要 logits）。
    两者都拿不到时 `available()` 返回 False，由上层退化到重叠代理。
    """

    method = "occlusion_logprob"
    note = ""
    _model_lock = None

    def __init__(self, service: "QaAttributionService", run: Mapping):
        self.service = service
        self.run = run
        self.profile = service._profile(run)
        self.note = ""
        self._backend = ""
        self._probed = None
        self._model = None
        self._tokenizer = None
        self._split = {}

    # ---------------------------------------------------------------- 能力探测
    def _base_url(self) -> str:
        profile = self.profile
        value = getattr(profile, "base_url", "") or ""
        if not value and isinstance(profile, Mapping):
            value = profile.get("base_url") or ""
        return str(value or "").rstrip("/")

    def _native_base(self) -> str:
        """llama.cpp 原生接口的根（去掉配置里可能带的 /v1）。"""
        base = self._base_url()
        return base[:-3].rstrip("/") if base.endswith("/v1") else base

    def _openai_base(self) -> str:
        """OpenAI 兼容接口前缀（配置里可能已经带了 /v1，避免拼成 /v1/v1）。"""
        base = self._base_url()
        return base if base.endswith("/v1") else base + "/v1"

    def _api_key(self) -> str:
        profile = self.profile
        value = getattr(profile, "api_key", "") or ""
        if not value and isinstance(profile, Mapping):
            value = profile.get("api_key") or ""
        return str(value or "")

    def available(self) -> bool:
        if self._probed is None:
            self._probed = self._probe()
        return bool(self._probed)

    def _probe(self) -> bool:
        import os

        if self._base_url() and self._probe_http():
            self._backend = "http"
            self.method = "occlusion_logprob_http"
            return True
        if os.environ.get("QA_INFLUENCE_MODEL_PATH", "").strip() and self._probe_local():
            self._backend = "local"
            self.method = "occlusion_logprob_local"
            return True
        return False

    def _probe_http(self) -> bool:
        """探测：只要有任意一种接口能吐出 token 对数概率，就走真·对数概率路径。"""
        probe_prompt = "参考资料：\n- 香港税务局发布新规。\n\n结论：香港税务局发布新规。"
        for payload, url in (
            ({"model": self._model_id(), "prompt": probe_prompt, "max_tokens": 4,
              "temperature": 0, "logprobs": True, "top_logprobs": 1},
             self._openai_base() + "/completions"),
            ({"prompt": probe_prompt, "n_predict": 4, "temperature": 0,
              "logprobs": True, "n_probs": 1},
             self._native_base() + "/completion"),
        ):
            try:
                body = self._post_json(url, payload, timeout=15)
            except Exception:
                continue
            text, values = self._extract_logprobs(body)
            if values:
                return True
        return False

    @staticmethod
    def _extract_logprobs(body: Mapping):
        """从 OpenAI 兼容或 llama.cpp 响应里取出 (生成文本, [token 对数概率])。

        兼容三种格式：
          - OpenAI 新格式：choices[0].text + choices[0].logprobs.content[].logprob
          - OpenAI 旧格式：choices[0].logprobs.token_logprobs
          - llama.cpp：content + completion_probabilities[].logprob（或 probs[0].prob）
        """
        import math

        choice = (body.get("choices") or [{}])[0]
        text = str(choice.get("text") or body.get("content") or "")
        logprobs = choice.get("logprobs") or {}
        content = logprobs.get("content") or []
        values = [float(item.get("logprob")) for item in content
                  if isinstance(item, Mapping) and item.get("logprob") is not None]
        if values:
            return text, values
        legacy = [item for item in (logprobs.get("token_logprobs") or []) if item is not None]
        if legacy:
            return text, [float(item) for item in legacy]
        out = []
        for item in body.get("completion_probabilities") or []:
            if not isinstance(item, Mapping):
                continue
            if item.get("logprob") is not None:
                out.append(float(item["logprob"]))
                continue
            probs = item.get("probs") or []
            probability = float((probs[0] or {}).get("prob") or 0) if probs else 0.0
            if probability > 0:
                out.append(math.log(probability))
        return text, out

    def _score_http(self, prompt: str, target: str):
        """教师强制式遮挡打分。

        把目标文本的一部分拼进 prompt（剩余部分留给模型续写），取续写首 token 的对数概率。
        遮挡前后比的是**同一段续写**的生成概率，差值才可解释为"这条证据的影响力"。

        切分点按目标文本缓存：一次 run 内所有遮挡条件必须用**同一个切分点**，
        否则不同条件下的分数不可比。某个条件在该切分点生成不出目标续写时返回 None
        （宁可退化为代理，也不给不可解释的数字）。
        """
        clean = " ".join(str(target or "").split())
        if len(clean) < 6:
            return None
        if clean in self._split:
            candidates = [self._split[clean]]
        else:
            # 未定切分点：从 1/3 处往 80% 处逐字试，找到第一个模型真能续写目标的位置。
            # 只影响基线（选点），选定后所有遮挡条件复用同一点，保证分数可比。
            low = max(2, len(clean) // 3)
            high = max(low + 1, int(len(clean) * 0.85))
            candidates = list(range(low, min(high, len(clean) - 1)))[:16]
        for split_at in candidates:
            prefix, suffix = clean[:split_at], clean[split_at:]
            if not suffix:
                continue
            value = self._continue_logprob(prompt + prefix, suffix)
            if value is None:
                continue
            self._split.setdefault(clean, int(split_at))
            return value
        return None

    def _continue_logprob(self, full_prompt: str, suffix: str):
        payloads = (
            (self._openai_base() + "/completions",
             {"model": self._model_id(), "prompt": full_prompt,
              "max_tokens": max(8, min(64, len(suffix) * 3)), "temperature": 0,
              "logprobs": True, "top_logprobs": 1}),
            (self._native_base() + "/completion",
             {"prompt": full_prompt, "n_predict": max(8, min(64, len(suffix) * 3)),
              "temperature": 0, "logprobs": True, "n_probs": 1, "cache_prompt": True}),
        )
        for url, payload in payloads:
            try:
                body = self._post_json(url, payload, timeout=30)
            except Exception:
                continue
            generated, values = self._extract_logprobs(body)
            if not values:
                continue
            matched, used = self._match_prefix(suffix, generated, values)
            if matched <= 0:
                continue
            return sum(used) / float(len(used))
        return None

    @staticmethod
    def _match_prefix(suffix: str, generated: str, values: list):
        """把续写文本与目标后缀对齐，返回 (匹配字符数, 对应的对数概率列表)。

        生成式打分无法对任意固定字符串取对数概率（接口不返回 prompt 的 logprobs），
        所以只要求**续写的首字符与目标后缀一致**——这时首个 token 的条件概率
        就是"给定这些证据，结论后半句有多容易被生成"。两边不一致就判为不可比（返回 0），
        宁可退化为代理，也不给出不可解释的数字。
        """
        generated_clean = " ".join(str(generated or "").split())
        generated_clean = re.sub(r"^[\s\-•·:：,，。]+", "", generated_clean)
        if not values:
            # 没有对数概率就没有影响力可言，直接判为不可比
            return 0, []
        matched = 0
        for left, right in zip(generated_clean, suffix):
            if left != right:
                break
            matched += 1
        if matched < 1:
            return 0, []
        keep = max(1, min(len(values), matched))
        return matched, values[:keep]

    def _probe_local(self) -> bool:
        import os

        path = os.environ.get("QA_INFLUENCE_MODEL_PATH", "").strip()
        if not path:
            return False
        try:
            import torch  # noqa: F401
            from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: F401
        except Exception:
            return False
        if not os.path.isdir(path):
            return False
        try:
            self._load_local()
            return True
        except Exception:
            return False

    def _model_id(self) -> str:
        profile = self.profile
        value = getattr(profile, "model_id", "") or ""
        if not value and isinstance(profile, Mapping):
            value = profile.get("model_id") or ""
        return str(value or "local")

    @staticmethod
    def _post_json(url: str, payload: dict, *, timeout: int = 20) -> dict:
        import json as _json
        import urllib.request

        request = urllib.request.Request(
            url,
            data=_json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _json.loads(response.read().decode("utf-8", "replace") or "{}")

    # ---------------------------------------------------------------- 打分
    def __call__(self, target: str, sentences: list) -> float:
        prompt = self._prompt(sentences)
        value = self._score(prompt, target)
        if value is None:
            raise RuntimeError("打分接口未返回可用对数概率")
        return float(value)

    def _prompt(self, sentences: list) -> str:
        body = "\n".join("- " + str(item)[:400] for item in sentences if str(item or "").strip())
        return "参考资料：\n%s\n\n结论：" % body

    def _score(self, prompt: str, target: str):
        if self._backend == "local":
            return self._score_local(prompt, target)
        return self._score_http(prompt, target)

    # ---------------------------------------------------------------- 本地模型
    def _load_local(self):
        import os
        import threading

        if _LogprobScorer._model_lock is None:
            _LogprobScorer._model_lock = threading.Lock()
        with _LogprobScorer._model_lock:
            if self._model is not None:
                return
            from transformers import AutoModelForCausalLM, AutoTokenizer

            path = os.environ["QA_INFLUENCE_MODEL_PATH"].strip()
            self._tokenizer = AutoTokenizer.from_pretrained(path)
            self._model = AutoModelForCausalLM.from_pretrained(path)
            self._model.eval()

    def _score_local(self, prompt: str, target: str):
        import torch

        self._load_local()
        full = prompt + target
        encoded = self._tokenizer(full, return_tensors="pt")
        prompt_ids = self._tokenizer(prompt, return_tensors="pt")["input_ids"]
        with torch.no_grad():
            logits = self._model(**encoded).logits
        import torch.nn.functional as functional

        log_probs = functional.log_softmax(logits[:, :-1, :], dim=-1)
        ids = encoded["input_ids"][:, 1:]
        token_log_probs = log_probs.gather(-1, ids.unsqueeze(-1)).squeeze(-1)[0]
        start = max(0, int(prompt_ids.shape[1]) - 1)
        values = token_log_probs[start:]
        if values.numel() == 0:
            return None
        return float(values.mean().item())

    def token_influence(self, target: str, sentences: list, index: int) -> list:
        """句内词元归因：逐个遮挡该句里的目标词元，重算对数概率，落差即该词元影响力。"""
        sentence = str(sentences[index] or "")
        try:
            baseline = float(self(target, sentences))
        except Exception:
            return []
        scored = []
        for token in sorted(_terms(target), key=len, reverse=True)[:8]:
            if token not in sentence.casefold():
                continue
            reduced = [
                (item.replace(token, "") if position == index else item)
                for position, item in enumerate(sentences)
            ]
            try:
                value = self(target, reduced)
            except Exception:
                continue
            scored.append((token, round(float(baseline - value), 6)))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored


__all__ = ["QaAttributionService", "split_sentences"]
