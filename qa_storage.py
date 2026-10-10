#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Short-transaction durable storage used by the unified QA Gateway."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Mapping

from qa_contracts import QA_CONTRACT_VERSION
from qa_schema import ensure_qa_tables


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _minutes_ago_text(minutes: float) -> str:
    """N 分钟前的 UTC 文本（格式与 _now() 一致，便于直接做字符串比较）。"""
    moment = datetime.now(timezone.utc) - timedelta(minutes=float(minutes))
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _decode_json(raw, fallback):
    """JSON 列解码（坏值一律退回 fallback；读图路径绝不能因脏数据抛异常）。"""
    try:
        return json.loads(raw) if raw not in (None, "") else fallback
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _row_dict(row) -> dict | None:
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:
        return {str(index): value for index, value in enumerate(row)}


def _decode_run(row) -> dict | None:
    value = _row_dict(row)
    if value is None:
        return None
    for source, target, fallback in (
        ("request_json", "request", {}),
        ("degradation_json", "degradation", []),
        ("final_answer_json", "final_answer", {}),
    ):
        try:
            decoded = json.loads(value.get(source) or _json(fallback))
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = fallback
        value[target] = decoded
    value["degraded"] = bool(value.get("degraded"))
    return value


# ── graph-rag-v2 通用包 Phase 01 · F-5：版本四元组 ────────────────────────────
# 四元组（corpus_version / model_version / prompt_version / config_hash）用于回放判定：
# 任一版本变化就说明"同样的问句不该按旧结论复用"。四个值都允许为空串（老数据、取不到），
# 一律**只做尽力读取，绝不抛异常**——建 run 是请求的关键路径。
_CONFIG_HASH_LENGTH = 16
_CONFIG_HASH_KEYS = (
    # 检索 / 多跳 / 重规划（config.py 里带默认值的开关与上限）
    "QA_BUSINESS_RULES_ENABLED", "QA_QUERY_DECOMPOSE_ENABLED", "QA_MULTI_HOP_ENABLED",
    "QA_MAX_HOPS", "QA_MULTI_HOP_BUDGET_SECONDS", "QA_RECURSION_MAX_ROUNDS",
    # 检索通道与候选过滤（由各模块直接读环境变量，config 里没有默认值）
    "QA_GRAPH_EVIDENCE_ENABLED", "QA_VECTOR_VARIANTS", "QA_VECTOR_PREFILTER",
    "QA_TIME_EXPAND_LADDER", "QA_RANKING_PROFILE",
    # 政策解析上限与生成侧选择
    "QA_STANDARD_MAX_HOPS", "QA_DEEP_MAX_HOPS", "QA_MAX_QUERIES_PER_HOP", "QA_MAX_EVIDENCE",
    "QA_SYNTHESIS_PROVIDER", "QA_RESEARCH_TIMEOUT_SECONDS", "QA_LLM_SELECTION",
)
"""纳入 config_hash 的配置键：全部是"改了就影响问答链路"的静态配置。"""


def _config_value(key: str):
    """取配置的有效值：优先 config 模块（带默认值），其次环境变量，都取不到为 None。"""
    try:
        import config as _config

        if hasattr(_config, key):
            return getattr(_config, key)
    except Exception:
        pass
    return os.environ.get(key, None)


def _config_hash() -> str:
    """对"与本次问答相关的关键配置"算稳定 sha256 短哈希（16 位十六进制）。

    纳入：排序权重表（含 QA_RANKING_PROFILE 选档与 QA_RANKING_WEIGHT_* 逐项覆盖）、
    `_CONFIG_HASH_KEYS` 里的开关与上限、以及 `QA_CONTRACT_VERSION`（契约变了就不该按旧配置回放）。
    不纳入：时间戳、run_id、用户/包标识、问题文本与 mode —— 这些是"数据"不是"配置"，
    已经落在 request_json 里；纳入它们会让同一份配置算出不同哈希，回放判定直接失效。
    同一份配置必须得到同一个值：payload 用 sort_keys 序列化后再哈希。
    """
    payload: dict = {"contract_version": QA_CONTRACT_VERSION}
    try:
        from qa_ranking_weights import ranking_weights

        payload["ranking_weights"] = ranking_weights()
    except Exception:
        payload["ranking_weights"] = None
    payload["qa_config"] = {key: _config_value(key) for key in _CONFIG_HASH_KEYS}
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()[:_CONFIG_HASH_LENGTH]


def _prompt_version() -> str:
    """提示词/模板版本（F-5 的 prompt_version）：把现成的版本常量拼成稳定串。

    来源都是模块级常量（延迟 import、只读、失败忽略）：
      · `qa_research.RESEARCH_TEMPLATE_VERSION`：研究笔记模板；
      · `qa_reasoning.ADJUDICATION_VERSION`：证据裁决规则。
    一个都取不到就返回空串——宁可空着，也不编一个假版本号。
    """
    parts = []
    for module_name, attr in (("qa_research", "RESEARCH_TEMPLATE_VERSION"),
                              ("qa_reasoning", "ADJUDICATION_VERSION")):
        try:
            value = str(getattr(importlib.import_module(module_name), attr, "") or "").strip()
        except Exception:
            value = ""
        if value:
            parts.append(value)
    return "+".join(parts)


def _provider_model_version(database, provider_id: str) -> str:
    """provider 的模型 id（F-5 的 model_version）；拿不到就空串。

    只读、不联网、不动 provider 层：查 `qa_provider_profiles.model_id`（profile_id 精确匹配）。
    **刻意**不调用 `QaProviderRegistry.resolve`：它对 local provider 会做端点探测（可能几秒），
    建 run 在请求同步路径上，不能为一个版本号把它拖慢（拿不到就留空，回放时按"未知模型"看）。
    """
    profile_id = str(provider_id or "").strip().casefold() or "local"
    try:
        database._ensure_connection()
        with database.lock:
            row = database.connection.execute(
                "SELECT model_id FROM qa_provider_profiles WHERE profile_id=?", (profile_id,),
            ).fetchone()
    except Exception:
        return ""
    if row is None:
        return ""
    try:
        return str(row["model_id"] or "").strip()
    except Exception:
        try:
            return str(row[0] or "").strip()
        except Exception:
            return ""


def _corpus_version(database, pack_id: str) -> str:
    """本地语料指纹（F-5 的 corpus_version）；拿不到就空串。

    与检索缓存的 kb_version **同一个口径**：复用 `qa_pipeline._local_corpus_version`
    （延迟 import + 兜底：qa_pipeline 反过来 import qa_storage，模块级 import 会成环；
    真 import 不到就留空，绝不因此打断建 run）。
    """
    try:
        from qa_pipeline import _local_corpus_version
    except Exception:
        return ""
    try:
        return str(_local_corpus_version(database, pack_id) or "")
    except Exception:
        return ""


class QaStore:
    def __init__(self, database):
        self.database = database

    def ensure_schema(self) -> None:
        self.database._ensure_connection()
        with self.database.lock:
            cursor = self.database.connection.cursor()
            try:
                ensure_qa_tables(cursor)
                self.database.connection.commit()
            except Exception:
                self.database.connection.rollback()
                raise
            finally:
                cursor.close()

    def create_run(
        self,
        request_payload: Mapping,
        *,
        owner_user_id: str,
        idempotency_key: str,
        research_app_id: str = "",
        synthesis_provider_id: str = "local",
        corpus_version: str = "",
        model_version: str = "",
        prompt_version: str = "",
        config_hash: str = "",
    ) -> dict:
        """建 run。

        Phase 01（F-5）：四个版本参数都是**可选**的，默认值让老调用方行为不变——
        不传就尽力推导（语料指纹 / provider 模型 id / 模板版本 / 配置哈希），
        推导不出来就写空串（可回滚：列有默认值，读取侧不依赖非空）。
        """
        self.ensure_schema()
        owner = str(owner_user_id or "")
        idem = str(idempotency_key or "").strip()
        if not idem:
            raise ValueError("idempotency_key is required")
        pack_id = str(request_payload.get("industry_pack_id") or "")
        provider_id = str(request_payload.get("draft_provider") or "local")
        versions = (
            str(corpus_version or "") or _corpus_version(self.database, pack_id),
            str(model_version or "") or _provider_model_version(self.database, provider_id),
            str(prompt_version or "") or _prompt_version(),
            str(config_hash or "") or _config_hash(),
        )
        with self.database.lock:
            run_id = uuid.uuid4().hex
            now = _now()
            question = str(request_payload.get("question") or "")
            values = (
                run_id,
                QA_CONTRACT_VERSION,
                str(request_payload.get("session_id") or ""),
                owner,
                pack_id,
                str(request_payload.get("origin") or "api"),
                str(request_payload.get("mode") or "standard"),
                hashlib.sha256(question.encode("utf-8")).hexdigest(),
                question,
                _json(dict(request_payload)),
                "queued",
                "plan",
                str(request_payload.get("draft_provider") or "local"),
                str(research_app_id or ""),
                str(synthesis_provider_id or "local"),
                idem,
                *versions,
                now,
                now,
            )
            cursor = self.database.connection.execute(
                """
                INSERT INTO qa_runs(
                    id,contract_version,session_id,owner_user_id,industry_pack_id,
                    origin,mode,question_hash,question_text,request_json,status,current_stage,
                    draft_provider_id,research_app_id,synthesis_provider_id,
                    idempotency_key,corpus_version,model_version,prompt_version,config_hash,
                    created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(owner_user_id,idempotency_key) DO NOTHING
                """,
                values,
            )
            self.database.connection.commit()
            created = bool(cursor.rowcount)
            if created:
                result = self.get_run(run_id, owner_user_id=owner)
            else:
                result = self.get_run_by_idempotency(owner, idem)
            result["_created"] = created
            return result

    def get_run(self, run_id: str, *, owner_user_id: str | None = None) -> dict | None:
        self.ensure_schema()
        sql = "SELECT * FROM qa_runs WHERE id=?"
        params: list = [str(run_id)]
        if owner_user_id is not None:
            sql += " AND owner_user_id=?"
            params.append(str(owner_user_id or ""))
        with self.database.lock:
            return _decode_run(self.database.connection.execute(sql, tuple(params)).fetchone())

    def get_run_by_idempotency(self, owner_user_id: str, idempotency_key: str) -> dict | None:
        self.ensure_schema()
        with self.database.lock:
            row = self.database.connection.execute(
                "SELECT * FROM qa_runs WHERE owner_user_id=? AND idempotency_key=?",
                (str(owner_user_id or ""), str(idempotency_key or "")),
            ).fetchone()
        return _decode_run(row)

    def list_runs(
        self,
        *,
        owner_user_id: str,
        industry_pack_id: str = "",
        session_id: str = "",
        limit: int = 50,
    ) -> list[dict]:
        self.ensure_schema()
        conditions = ["owner_user_id=?"]
        params: list = [str(owner_user_id or "")]
        if industry_pack_id:
            conditions.append("industry_pack_id=?")
            params.append(str(industry_pack_id))
        if session_id:
            conditions.append("session_id=?")
            params.append(str(session_id))
        params.append(max(1, min(int(limit or 50), 200)))
        with self.database.lock:
            rows = self.database.connection.execute(
                "SELECT * FROM qa_runs WHERE " + " AND ".join(conditions) +
                " ORDER BY created_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [_decode_run(row) for row in rows]

    def set_job_id(self, run_id: str, job_id: int) -> None:
        self.ensure_schema()
        with self.database.lock:
            self.database.connection.execute(
                "UPDATE qa_runs SET job_id=?, updated_at=? WHERE id=?",
                (int(job_id), _now(), str(run_id)),
            )
            self.database.connection.commit()

    def update_run(self, run_id: str, *, status: str, stage: str, final_answer: Mapping | None = None) -> None:
        self.ensure_schema()
        now = _now()
        completed = now if status in {"completed", "failed", "cancelled"} else None
        with self.database.lock:
            if final_answer is None:
                self.database.connection.execute(
                    """
                    UPDATE qa_runs SET status=?, current_stage=?, updated_at=?,
                        completed_at=COALESCE(?, completed_at) WHERE id=?
                    """,
                    (status, stage, now, completed, str(run_id)),
                )
            else:
                self.database.connection.execute(
                    """
                    UPDATE qa_runs SET status=?, current_stage=?, final_answer_json=?,
                        updated_at=?, completed_at=COALESCE(?, completed_at) WHERE id=?
                    """,
                    (status, stage, _json(final_answer), now, completed, str(run_id)),
                )
            self.database.connection.commit()

    def mark_degraded(self, run_id: str, item: Mapping) -> None:
        run = self.get_run(run_id) or {}
        degradation = list(run.get("degradation") or [])
        degradation.append(dict(item))
        with self.database.lock:
            self.database.connection.execute(
                "UPDATE qa_runs SET degraded=1, degradation_json=?, updated_at=? WHERE id=?",
                (_json(degradation), _now(), str(run_id)),
            )
            self.database.connection.commit()

    def cancel_run(self, run_id: str, *, owner_user_id: str | None = None) -> bool:
        self.ensure_schema()
        params: list = [_now(), _now(), str(run_id)]
        owner_sql = ""
        if owner_user_id is not None:
            owner_sql = " AND owner_user_id=?"
            params.append(str(owner_user_id or ""))
        with self.database.lock:
            cursor = self.database.connection.execute(
                """
                UPDATE qa_runs SET status='cancelled', completed_at=?, updated_at=?
                WHERE id=? AND status IN ('queued','running','retry_wait')
                """ + owner_sql,
                tuple(params),
            )
            self.database.connection.commit()
            return bool(cursor.rowcount)

    def next_event_id(self, run_id: str) -> int:
        self.ensure_schema()
        with self.database.lock:
            row = self.database.connection.execute(
                "SELECT COALESCE(MAX(event_id), 0) + 1 FROM qa_events WHERE run_id=?",
                (str(run_id),),
            ).fetchone()
        return int(row[0] if row else 1)

    def append_event(self, event: Mapping) -> dict:
        self.ensure_schema()
        now = str(event.get("timestamp") or _now())
        with self.database.lock:
            self.database.connection.execute(
                """
                INSERT INTO qa_events(run_id,event_id,event_type,stage,payload_json,created_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(run_id,event_id) DO NOTHING
                """,
                (
                    str(event["run_id"]),
                    int(event["event_id"]),
                    str(event["type"]),
                    str(event["stage"]),
                    _json(dict(event)),
                    now,
                ),
            )
            self.database.connection.commit()
        return dict(event)

    def events_after(self, run_id: str, event_id: int = 0) -> list[dict]:
        self.ensure_schema()
        with self.database.lock:
            rows = self.database.connection.execute(
                "SELECT payload_json FROM qa_events WHERE run_id=? AND event_id>? ORDER BY event_id",
                (str(run_id), int(event_id or 0)),
            ).fetchall()
        result = []
        for row in rows:
            raw = row[0]
            try:
                value = json.loads(raw or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                result.append(value)
        return result

    def stage_runs(self, run_id: str) -> list[dict]:
        self.ensure_schema()
        with self.database.lock:
            rows = self.database.connection.execute(
                "SELECT * FROM qa_stage_runs WHERE run_id=? ORDER BY id", (str(run_id),)
            ).fetchall()
        result = []
        for row in rows:
            value = _row_dict(row)
            try:
                value["details"] = json.loads(value.get("details_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                value["details"] = {}
            result.append(value)
        return result

    def next_stage_attempt(self, run_id: str, stage: str) -> int:
        self.ensure_schema()
        with self.database.lock:
            row = self.database.connection.execute(
                "SELECT COALESCE(MAX(attempt), 0) + 1 FROM qa_stage_runs WHERE run_id=? AND stage=?",
                (str(run_id), str(stage)),
            ).fetchone()
        return int(row[0] if row else 1)

    def count_active_runs(self, owner_user_id: str = "") -> int:
        self.ensure_schema()
        sql = "SELECT COUNT(*) FROM qa_runs WHERE status IN ('queued','running','retry_wait')"
        params: tuple = ()
        if owner_user_id:
            sql += " AND owner_user_id=?"
            params = (str(owner_user_id),)
        with self.database.lock:
            row = self.database.connection.execute(sql, params).fetchone()
        return int(row[0] if row else 0)

    def expire_stale_runs(self, max_age_seconds: int, owner_user_id: str = "") -> list[str]:
        """把长时间没推进的问答运行置为失败，返回被回收的 run_id 列表。

        为什么需要：活跃配额（count_active_runs）把 queued/running/retry_wait 全部计入，
        而 worker 不在跑、或作业入队后没被领取时这些运行永远不会结束——用户会被永久锁在
        429「当前已有问答正在研究」，前端连阻塞的 run_id 都拿不到，无法自助取消。
        实测：本机库里 2 条 queued 的 run（qa_events / qa_stage_runs 都是 0 行）就会让之后
        所有 /api/chat/send 恒返回 USER_CONCURRENCY_LIMIT。
        """
        self.ensure_schema()
        max_age_seconds = max(60, int(max_age_seconds or 0))
        cutoff = _minutes_ago_text(max_age_seconds / 60.0)
        now = _now()
        owner_sql = ""
        select_params: list = [cutoff]
        if owner_user_id:
            owner_sql = " AND owner_user_id=?"
            select_params.append(str(owner_user_id))
        run_ids: list[str] = []
        with self.database.lock:
            cursor = self.database.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT id FROM qa_runs
                     WHERE status IN ('queued','running','retry_wait')
                       AND COALESCE(NULLIF(updated_at,''), created_at) <= ?
                    """ + owner_sql,
                    select_params,
                )
                for row in cursor.fetchall():
                    value = dict(row) if hasattr(row, "keys") else {"id": row[0]}
                    if value.get("id"):
                        run_ids.append(str(value["id"]))
                if run_ids:
                    placeholders = ",".join("?" for _ in run_ids)
                    degradation = _json([{
                        "code": "STALE_RUN_EXPIRED",
                        "message": "该问答长时间未推进，已自动结束，可重新提问。",
                        "stage": "recovery",
                    }])
                    cursor.execute(
                        """
                        UPDATE qa_runs
                           SET status='failed', completed_at=?, updated_at=?,
                               degradation_json=?, degraded=1
                         WHERE id IN (%s)
                        """ % placeholders,
                        [now, now, degradation, *run_ids],
                    )
                self.database.connection.commit()
            finally:
                cursor.close()
        return run_ids

    def persist_level1_result(self, run_id: str, result: Mapping) -> None:
        """Upsert verified L1 evidence/claims in one short transaction."""
        self.ensure_schema()
        now = _now()
        with self.database.lock:
            cursor = self.database.connection.cursor()
            try:
                for evidence in result.get("evidence") or []:
                    cursor.execute(
                        """
                        INSERT INTO qa_evidence(
                            run_id,evidence_ref,source_type,article_id,ragflow_kb_id,
                            document_id,chunk_id,source_url,source_title,published_at,
                            fetched_at,authority_level,content_hash,payload_json,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(run_id,evidence_ref) DO UPDATE SET
                            source_type=excluded.source_type,article_id=excluded.article_id,
                            source_url=excluded.source_url,source_title=excluded.source_title,
                            published_at=excluded.published_at,fetched_at=excluded.fetched_at,
                            authority_level=excluded.authority_level,content_hash=excluded.content_hash,
                            payload_json=excluded.payload_json
                        """,
                        (
                            str(run_id), str(evidence.get("evidence_ref") or ""),
                            str(evidence.get("source_type") or ""), evidence.get("article_id"),
                            evidence.get("ragflow_kb_id"), evidence.get("document_id"), evidence.get("chunk_id"),
                            str(evidence.get("source_url") or ""), str(evidence.get("title") or ""),
                            evidence.get("published_at"), evidence.get("fetched_at"), evidence.get("authority_level"),
                            hashlib.sha256(str(evidence.get("content_excerpt") or "").encode("utf-8")).hexdigest(),
                            _json(evidence), now,
                        ),
                    )
                for claim in result.get("claims") or []:
                    claim_id = str(claim.get("claim_id") or "")
                    cursor.execute(
                        """
                        INSERT INTO qa_claims(
                            run_id,claim_key,stage,claim_text,claim_type,confidence,
                            valid_from,valid_to,scope_json,verification_status,payload_json,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(run_id,claim_key,stage) DO UPDATE SET
                            claim_text=excluded.claim_text,claim_type=excluded.claim_type,
                            confidence=excluded.confidence,valid_from=excluded.valid_from,
                            valid_to=excluded.valid_to,scope_json=excluded.scope_json,
                            verification_status=excluded.verification_status,payload_json=excluded.payload_json
                        """,
                        (
                            str(run_id), claim_id, "level1_draft", str(claim.get("text") or ""),
                            str(claim.get("claim_type") or "background"), float(claim.get("confidence") or 0),
                            claim.get("valid_from"), claim.get("valid_to"), _json(claim.get("scope") or []),
                            str(claim.get("verification_status") or "unverified"), _json(claim), now,
                        ),
                    )
                    for evidence_ref in claim.get("evidence_refs") or []:
                        cursor.execute(
                            """
                            INSERT INTO qa_claim_evidence(run_id,claim_key,evidence_ref,relationship,relevance_score,created_at)
                            VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,claim_key,evidence_ref,relationship) DO NOTHING
                            """,
                            (str(run_id), claim_id, str(evidence_ref), "supports", float(claim.get("confidence") or 0), now),
                        )
                self.database.connection.commit()
            except Exception:
                self.database.connection.rollback()
                raise
            finally:
                cursor.close()

    def persist_level2_result(self, run_id: str, result: Mapping) -> None:
        """Persist verified RAGFlow evidence, claims, links and conflicts."""
        self.ensure_schema()
        now = _now()
        with self.database.lock:
            cursor = self.database.connection.cursor()
            try:
                for evidence in result.get("evidence") or []:
                    cursor.execute(
                        """
                        INSERT INTO qa_evidence(
                            run_id,evidence_ref,source_type,article_id,ragflow_kb_id,
                            document_id,chunk_id,source_url,source_title,published_at,
                            fetched_at,authority_level,content_hash,payload_json,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(run_id,evidence_ref) DO UPDATE SET
                            document_id=excluded.document_id,chunk_id=excluded.chunk_id,
                            source_url=excluded.source_url,source_title=excluded.source_title,
                            published_at=excluded.published_at,fetched_at=excluded.fetched_at,
                            authority_level=excluded.authority_level,content_hash=excluded.content_hash,
                            payload_json=excluded.payload_json
                        """,
                        (
                            str(run_id), str(evidence.get("evidence_ref") or ""),
                            str(evidence.get("source_type") or ""), evidence.get("article_id"),
                            evidence.get("ragflow_kb_id"), evidence.get("document_id"), evidence.get("chunk_id"),
                            str(evidence.get("source_url") or ""), str(evidence.get("title") or ""),
                            evidence.get("published_at"), evidence.get("fetched_at"), evidence.get("authority_level"),
                            hashlib.sha256(str(evidence.get("content_excerpt") or "").encode("utf-8")).hexdigest(),
                            _json(evidence), now,
                        ),
                    )
                for field in ("confirmed_claims", "corrected_claims", "new_findings"):
                    for claim in result.get(field) or []:
                        claim_id = str(claim.get("claim_id") or "")
                        cursor.execute(
                            """
                            INSERT INTO qa_claims(
                                run_id,claim_key,stage,claim_text,claim_type,confidence,
                                valid_from,valid_to,scope_json,verification_status,payload_json,created_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(run_id,claim_key,stage) DO UPDATE SET
                                claim_text=excluded.claim_text,claim_type=excluded.claim_type,
                                confidence=excluded.confidence,verification_status=excluded.verification_status,
                                payload_json=excluded.payload_json
                            """,
                            (
                                str(run_id), claim_id, "level2_research", str(claim.get("text") or ""),
                                str(claim.get("claim_type") or "background"), float(claim.get("confidence") or 0),
                                claim.get("valid_from"), claim.get("valid_to"), _json(claim.get("scope") or []),
                                str(claim.get("verification_status") or "unverified"), _json({"bucket": field, **claim}), now,
                            ),
                        )
                        for evidence_ref in claim.get("evidence_refs") or []:
                            cursor.execute(
                                """
                                INSERT INTO qa_claim_evidence(run_id,claim_key,evidence_ref,relationship,relevance_score,created_at)
                                VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,claim_key,evidence_ref,relationship) DO NOTHING
                                """,
                                (str(run_id), claim_id, str(evidence_ref), "supports", float(claim.get("confidence") or 0), now),
                            )
                for conflict in result.get("conflicts") or []:
                    cursor.execute(
                        """
                        INSERT INTO qa_conflicts(run_id,conflict_key,conflict_type,resolution,rationale,payload_json,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id,conflict_key) DO UPDATE SET
                            conflict_type=excluded.conflict_type,resolution=excluded.resolution,
                            rationale=excluded.rationale,payload_json=excluded.payload_json,updated_at=excluded.updated_at
                        """,
                        (
                            str(run_id), str(conflict.get("conflict_id") or ""), str(conflict.get("conflict_type") or ""),
                            str(conflict.get("resolution") or "unresolved"), str(conflict.get("rationale") or ""),
                            _json(conflict), now, now,
                        ),
                    )
                self.database.connection.commit()
            except Exception:
                self.database.connection.rollback()
                raise
            finally:
                cursor.close()

    def record_reasoning_trace(self, run_id: str, *, hop_index: int, sub_query_id: str = "",
                               sub_query: str = "", depends_on=None, partial_answer: str = "",
                               used_evidence_refs=None, missing_links=None, next_queries=None,
                               status: str = "", round_index: int = 0,
                               latency_ms: int = 0, gap_id: str = "", route: str = "",
                               results: int = 0, accepted: int = 0, rejected: int = 0,
                               new_claims: int = 0, resolved_gap: int = 0) -> None:
        """阶段 10：记录一跳的推理留痕（幂等：同 run + 轮次 + 跳序号覆盖）。

        Phase 01（F-7）：补齐 SearchTrace 字段（gap_id / route / results / accepted /
        rejected / new_claims / resolved_gap），全部是**可选关键字参数**，默认值与原行为
        逐字等价（老调用方一行都不用改）。`round_index` 即 SearchTrace 的 `round`。
        `gap_id` / `new_claims` / `resolved_gap` 属阶段 07 的缺口闭环，现阶段默认 0/空串。

        刻意**不抛异常**：留痕是观测能力，写库失败绝不能影响问答主流程（边界要求）。
        """
        try:
            self.ensure_schema()
            now = _now()
            with self.database.lock:
                cursor = self.database.connection.cursor()
                try:
                    cursor.execute(
                        """
                        INSERT INTO qa_reasoning_traces(
                            run_id,hop_index,sub_query_id,sub_query,depends_on_json,partial_answer,
                            used_evidence_refs_json,missing_links_json,next_queries_json,status,
                            round_index,latency_ms,gap_id,route,results,accepted,rejected,
                            new_claims,resolved_gap,created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(run_id,round_index,hop_index) DO UPDATE SET
                            sub_query_id=excluded.sub_query_id,sub_query=excluded.sub_query,
                            depends_on_json=excluded.depends_on_json,
                            partial_answer=excluded.partial_answer,
                            used_evidence_refs_json=excluded.used_evidence_refs_json,
                            missing_links_json=excluded.missing_links_json,
                            next_queries_json=excluded.next_queries_json,
                            status=excluded.status,latency_ms=excluded.latency_ms,
                            gap_id=excluded.gap_id,route=excluded.route,
                            results=excluded.results,accepted=excluded.accepted,
                            rejected=excluded.rejected,new_claims=excluded.new_claims,
                            resolved_gap=excluded.resolved_gap,
                            updated_at=excluded.updated_at
                        """,
                        (
                            str(run_id), int(hop_index), str(sub_query_id or ""),
                            str(sub_query or "")[:1000], _json(list(depends_on or [])),
                            str(partial_answer or "")[:4000], _json(list(used_evidence_refs or [])),
                            _json(list(missing_links or [])), _json(list(next_queries or [])),
                            str(status or ""), int(round_index), int(latency_ms),
                            str(gap_id or ""), str(route or ""), int(results or 0),
                            int(accepted or 0), int(rejected or 0), int(new_claims or 0),
                            int(resolved_gap or 0), now, now,
                        ),
                    )
                    self.database.connection.commit()
                finally:
                    cursor.close()
        except Exception:
            return

    def reasoning_traces(self, run_id: str) -> list[dict]:
        """取某个 run 的全部推理留痕（按轮次、跳序号排序）。"""
        self.ensure_schema()
        with self.database.lock:
            cursor = self.database.connection.cursor()
            try:
                cursor.execute(
                    "SELECT * FROM qa_reasoning_traces WHERE run_id=?"
                    " ORDER BY round_index, hop_index", (str(run_id),),
                )
                return [dict(row) for row in cursor.fetchall()]
            finally:
                cursor.close()

    def save_session_constraints(self, *, owner_user_id: str, session_id: str,
                                 industry_pack_id: str, constraints: Mapping,
                                 run_id: str = "", source: str = "plan") -> int:
        """阶段 10：把已确认的会话约束固化下来（时间/实体/输出形式/排除项）。

        只落"非空"的约束；同 (用户, 会话, 包, 键) 覆盖。异常一律吞掉（不影响主流程）。
        """
        if not session_id:
            return 0
        try:
            self.ensure_schema()
            now = _now()
            rows = []
            for key, value in (constraints or {}).items():
                # 空值不固化；显式 False 也不固化（False 就是默认，没有信息量）
                if value is False or value in (None, "", [], {}, ()):
                    continue
                rows.append((
                    str(owner_user_id or ""), str(session_id), str(industry_pack_id or ""),
                    str(key)[:64], _json(value), str(source or "plan"), 1, str(run_id or ""),
                    now, now,
                ))
            if not rows:
                return 0
            with self.database.lock:
                cursor = self.database.connection.cursor()
                try:
                    cursor.executemany(
                        """
                        INSERT INTO qa_session_constraints(
                            owner_user_id,session_id,industry_pack_id,constraint_key,
                            constraint_value_json,source,confirmed,run_id,created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(owner_user_id,session_id,industry_pack_id,constraint_key)
                        DO UPDATE SET constraint_value_json=excluded.constraint_value_json,
                            source=excluded.source,confirmed=excluded.confirmed,
                            run_id=excluded.run_id,updated_at=excluded.updated_at
                        """,
                        rows,
                    )
                    self.database.connection.commit()
                    return len(rows)
                finally:
                    cursor.close()
        except Exception:
            return 0

    def session_constraints(self, *, owner_user_id: str, session_id: str,
                            industry_pack_id: str = "") -> dict:
        """取会话约束（键 → 值）；没有就返回空字典。"""
        if not session_id:
            return {}
        self.ensure_schema()
        where = ["owner_user_id=?", "session_id=?"]
        params = [str(owner_user_id or ""), str(session_id)]
        if industry_pack_id:
            where.append("industry_pack_id=?")
            params.append(str(industry_pack_id))
        with self.database.lock:
            cursor = self.database.connection.cursor()
            try:
                cursor.execute(
                    "SELECT constraint_key, constraint_value_json FROM qa_session_constraints"
                    " WHERE " + " AND ".join(where), tuple(params),
                )
                result = {}
                for row in cursor.fetchall():
                    try:
                        result[str(row["constraint_key"])] = json.loads(
                            str(row["constraint_value_json"]) or "null")
                    except Exception:
                        continue
                return result
            finally:
                cursor.close()

    def record_seen_evidence(self, *, owner_user_id: str, session_id: str, industry_pack_id: str,
                             records: list, run_id: str = "", round_index: int = 0) -> dict:
        """Phase 02（P02-03）：登记本轮"见过的证据身份"（含被闸门拒掉的）。

        为什么必须按 (owner_user_id, session_id, industry_pack_id) 落库：
        单次 run 的内存 seen 集合一结束就没了，而"下一轮别再捞同一批垃圾"是跨轮/跨 run 的
        需求（MASTER_RULES 第 14 条）。这三个键一起构成作用域，**任何一个不同都不共享**，
        避免 A 会话的拒收把 B 会话的证据也误伤掉。

        同 (作用域, source_fingerprint) 覆盖更新，语义：
          · status 一旦被 confirmed 过就保持 confirmed（它曾进过证据包，不能因为后来被拒就丢身份）；
          · seen_count / rejected_count 累加，first_* 保留首见信息；
          · round_index 记录最近一次见证的轮次。
        异常一律吞掉并回 {..., "error": ...}：留痕/去重绝不能拖累问答。
        """
        summary = {"recorded": 0, "confirmed": 0, "rejected": 0, "skipped": 0, "error": ""}
        try:
            self.ensure_schema()
            now = _now()
            rows = []
            for record in records or []:
                if not isinstance(record, Mapping):
                    continue
                key = str(record.get("source_fingerprint") or "")
                if not key:
                    summary["skipped"] += 1
                    continue
                status = str(record.get("status") or "seen")
                if status not in ("seen", "confirmed", "rejected"):
                    status = "seen"
                rows.append({
                    "owner_user_id": str(owner_user_id or ""), "session_id": str(session_id or ""),
                    "industry_pack_id": str(industry_pack_id or ""), "key": key,
                    "span": str(record.get("span_fingerprint") or ""),
                    "ref": str(record.get("evidence_ref") or ""),
                    "source_type": str(record.get("source_type") or ""),
                    "status": status, "run_id": str(run_id or ""),
                    "round_index": int(round_index or 0),
                    "payload": _json({"status": status, "evidence_ref": str(record.get("evidence_ref") or "")}),
                    "now": now,
                })
            if not rows:
                return summary
            with self.database.lock:
                cursor = self.database.connection.cursor()
                try:
                    for row in rows:
                        cursor.execute(
                            """
                            INSERT INTO qa_evidence_seen(
                                owner_user_id,session_id,industry_pack_id,source_fingerprint,
                                span_fingerprint,evidence_ref,source_type,status,seen_count,
                                rejected_count,first_run_id,last_run_id,round_index,payload_json,
                                first_seen_at,last_seen_at
                            ) VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?)
                            ON CONFLICT(owner_user_id,session_id,industry_pack_id,source_fingerprint)
                            DO UPDATE SET
                                span_fingerprint=excluded.span_fingerprint,
                                evidence_ref=excluded.evidence_ref,
                                source_type=excluded.source_type,
                                status=CASE
                                    WHEN qa_evidence_seen.status='confirmed' OR excluded.status='confirmed'
                                    THEN 'confirmed' ELSE excluded.status END,
                                seen_count=qa_evidence_seen.seen_count+1,
                                rejected_count=qa_evidence_seen.rejected_count
                                    + CASE WHEN excluded.status='rejected' THEN 1 ELSE 0 END,
                                last_run_id=excluded.last_run_id,
                                round_index=excluded.round_index,
                                payload_json=excluded.payload_json,
                                last_seen_at=excluded.last_seen_at
                            """,
                            (
                                row["owner_user_id"], row["session_id"], row["industry_pack_id"],
                                row["key"], row["span"], row["ref"], row["source_type"],
                                row["status"], 1 if row["status"] == "rejected" else 0,
                                row["run_id"], row["run_id"], row["round_index"], row["payload"],
                                row["now"], row["now"],
                            ),
                        )
                        summary["recorded"] += 1
                        if row["status"] == "confirmed":
                            summary["confirmed"] += 1
                        elif row["status"] == "rejected":
                            summary["rejected"] += 1
                    self.database.connection.commit()
                except Exception:
                    self.database.connection.rollback()
                    raise
                finally:
                    cursor.close()
            return summary
        except Exception as exc:  # noqa: BLE001
            summary["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
            return summary

    def seen_evidence(self, *, owner_user_id: str, session_id: str, industry_pack_id: str,
                      source_fingerprints=(), statuses=()) -> dict:
        """Phase 02（P02-03）：按作用域取"之前见过的身份" → {source_fingerprint: status}。

        三个作用域键**精确匹配**（含空串），不做跨会话合并——这是刻意的：
        "同一篇文章在别的会话里被拒过"不构成在本会话里丢弃它的理由。
        """
        keys = [str(item) for item in source_fingerprints or [] if str(item or "")]
        if not keys:
            return {}
        self.ensure_schema()
        where = ["owner_user_id=?", "session_id=?", "industry_pack_id=?"]
        params = [str(owner_user_id or ""), str(session_id or ""), str(industry_pack_id or "")]
        placeholders = ",".join("?" for _ in keys)
        where.append("source_fingerprint IN (%s)" % placeholders)
        params.extend(keys)
        allowed = [str(item) for item in statuses or [] if str(item or "")]
        if allowed:
            where.append("status IN (%s)" % ",".join("?" for _ in allowed))
            params.extend(allowed)
        with self.database.lock:
            cursor = self.database.connection.cursor()
            try:
                cursor.execute(
                    "SELECT source_fingerprint, status FROM qa_evidence_seen WHERE "
                    + " AND ".join(where), tuple(params),
                )
                return {str(row["source_fingerprint"]): str(row["status"]) for row in cursor.fetchall()}
            except Exception:
                return {}
            finally:
                cursor.close()

    def forget_seen_evidence(self, *, owner_user_id: str = "", session_id: str = "",
                             industry_pack_id: str = "", source_fingerprints=(),
                             all_scopes: bool = False) -> int:
        """删除 seen 身份：给回滚/污染撤销用（返回删除行数）。

        `all_scopes=True` 才允许不带任何作用域键地清空——正常调用必须给出至少一个作用域键，
        否则容易出现"一次误调用把全局去重记忆清掉"的事故。
        """
        keys = [str(item) for item in source_fingerprints or [] if str(item or "")]
        where, params = [], []
        if owner_user_id:
            where.append("owner_user_id=?")
            params.append(str(owner_user_id))
        if session_id:
            where.append("session_id=?")
            params.append(str(session_id))
        if industry_pack_id:
            where.append("industry_pack_id=?")
            params.append(str(industry_pack_id))
        if keys:
            where.append("source_fingerprint IN (%s)" % ",".join("?" for _ in keys))
            params.extend(keys)
        if not where and not all_scopes:
            return 0
        self.ensure_schema()
        with self.database.lock:
            cursor = self.database.connection.cursor()
            try:
                cursor.execute(
                    "DELETE FROM qa_evidence_seen"
                    + (" WHERE " + " AND ".join(where) if where else ""), tuple(params),
                )
                deleted = int(cursor.rowcount or 0)
                self.database.connection.commit()
                return deleted
            finally:
                cursor.close()

    def prune_seen_evidence(self, older_than_days: int | None = None) -> int:
        """Phase 02（缺口 2）：按保留期清理过期的 seen 身份，返回删除行数。

        为什么要有：`qa_evidence_seen` 是**只增不减**的记忆表（每次检索都登记一批身份），
        没有清理入口就会无限长下去（第一版刻意不做 TTL，先看写入量再定策略）。

        保守口径：
          · 只按**时间**删，不按作用域删——`older_than_days` 缺省取环境变量
            `QA_EVIDENCE_SEEN_TTL_DAYS`（默认 30，最小 1），所以作用域不同的行各按自己的
            最后见证时间过期，不会互相影响；
          · 时间列用 `last_seen_at`（最后见证时间，老行缺失时退到 `first_seen_at`）；
          · 维护入口里是**可选调用**，开关 `QA_EVIDENCE_SEEN_PRUNE_ENABLED` 默认关
            （见 `qa_evidence.prune_seen_evidence` 的说明）：这是新表，先观察一段时间再开。
        """
        if older_than_days is None:
            from qa_evidence import seen_ttl_days

            older_than_days = seen_ttl_days()
        try:
            # 最小 1 天：传 0/负数一律按 1 天算，别让"清空全表"这种事发生
            days = max(1, int(older_than_days))
        except (TypeError, ValueError):
            days = 30
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)) \
            .isoformat(timespec="milliseconds").replace("+00:00", "Z")
        self.ensure_schema()
        with self.database.lock:
            cursor = self.database.connection.cursor()
            try:
                cursor.execute(
                    "DELETE FROM qa_evidence_seen"
                    " WHERE COALESCE(NULLIF(last_seen_at,''), first_seen_at) < ?",
                    (cutoff,),
                )
                deleted = int(cursor.rowcount or 0)
                self.database.connection.commit()
                return deleted
            finally:
                cursor.close()

    def get_verification_cache(self, cache_key: str) -> dict | None:
        """阶段 03（P03-04）：读核验缓存（复用既有 `qa_retrieval_cache` 表，**不新增表/列**）。

        为什么要落库：同一条证据会在 level1 / 多跳每一跳 / level2 / claim 级核验里反复比对，
        进程重启或换 worker（gunicorn 多进程）后内存缓存全丢，落库才能跨进程复用。
        namespace 固定为 `qa_verification`，与检索缓存**不串味**；过期行一律当未命中。
        任何异常都返回 None（缓存只是加速器，坏掉不影响正确性）。
        """
        try:
            self.ensure_schema()
            now = _now()
            with self.database.lock:
                row = self.database.connection.execute(
                    "SELECT payload_json FROM qa_retrieval_cache"
                    " WHERE cache_key=? AND namespace=? AND expires_at>?",
                    (str(cache_key), "qa_verification", now),
                ).fetchone()
            if not row:
                return None
            value = json.loads(row[0] or "{}")
            return value if isinstance(value, dict) else None
        except Exception:
            return None

    def put_verification_cache(self, cache_key: str, payload: Mapping,
                               *, ttl_seconds: int = 900) -> None:
        """阶段 03（P03-04）：写核验缓存（TTL 与过期时间进既有 `expires_at` 列）。

        `kb_version` 列填核验版本（复用同一列表达"这条缓存属于哪套规则"，便于换版本自然失效）；
        写失败一律吞掉——缓存绝不能拖累问答。
        """
        try:
            from qa_verifier import CACHE_NAMESPACE, VERIFIER_VERSION
        except Exception:  # noqa: BLE001
            CACHE_NAMESPACE, VERIFIER_VERSION = "qa_verification", ""
        try:
            self.ensure_schema()
            now = datetime.now(timezone.utc)
            expires = now + timedelta(seconds=max(30, min(int(ttl_seconds or 900), 86400)))
            with self.database.lock:
                self.database.connection.execute(
                    """INSERT INTO qa_retrieval_cache(
                        cache_key,namespace,industry_pack_id,kb_version,scope_hash,
                        payload_json,created_at,expires_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(cache_key) DO UPDATE SET payload_json=excluded.payload_json,
                        created_at=excluded.created_at,expires_at=excluded.expires_at""",
                    (str(cache_key), CACHE_NAMESPACE, "", VERIFIER_VERSION, "",
                     _json(dict(payload)),
                     now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                     expires.isoformat(timespec="milliseconds").replace("+00:00", "Z")),
                )
                self.database.connection.commit()
        except Exception:
            return

    def persist_reasoning_graph(self, run_id: str, graph: Mapping) -> None:
        self.ensure_schema()
        now = _now()
        with self.database.lock:
            cursor = self.database.connection.cursor()
            try:
                for node in graph.get("claims") or []:
                    claim = dict(node.get("claim") or {})
                    claim_id = str(node.get("canonical_id") or claim.get("claim_id") or "")
                    cursor.execute(
                        """
                        INSERT INTO qa_claims(
                            run_id,claim_key,stage,claim_text,claim_type,confidence,
                            valid_from,valid_to,scope_json,verification_status,payload_json,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(run_id,claim_key,stage) DO UPDATE SET
                            claim_text=excluded.claim_text,claim_type=excluded.claim_type,
                            confidence=excluded.confidence,verification_status=excluded.verification_status,
                            payload_json=excluded.payload_json
                        """,
                        (
                            str(run_id), claim_id, "conflict_review", str(claim.get("text") or ""),
                            str(claim.get("claim_type") or "background"), float(claim.get("confidence") or 0),
                            claim.get("valid_from"), claim.get("valid_to"), _json(claim.get("scope") or []),
                            str(claim.get("verification_status") or "unverified"), _json(node), now,
                        ),
                    )
                for edge in graph.get("edges") or []:
                    cursor.execute(
                        """
                        INSERT INTO qa_claim_evidence(run_id,claim_key,evidence_ref,relationship,relevance_score,created_at)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,claim_key,evidence_ref,relationship) DO UPDATE SET
                            relevance_score=excluded.relevance_score
                        """,
                        (
                            str(run_id), str(edge.get("claim_id") or ""), str(edge.get("evidence_ref") or ""),
                            str(edge.get("relationship") or "supports"), float(edge.get("relevance_score") or 0), now,
                        ),
                    )
                for conflict in graph.get("conflicts") or []:
                    cursor.execute(
                        """
                        INSERT INTO qa_conflicts(run_id,conflict_key,conflict_type,resolution,rationale,payload_json,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id,conflict_key) DO UPDATE SET
                            conflict_type=excluded.conflict_type,resolution=excluded.resolution,
                            rationale=excluded.rationale,payload_json=excluded.payload_json,updated_at=excluded.updated_at
                        """,
                        (
                            str(run_id), str(conflict.get("conflict_id") or ""), str(conflict.get("conflict_type") or ""),
                            str(conflict.get("resolution") or "unresolved"), str(conflict.get("rationale") or ""),
                            _json(conflict), now, now,
                        ),
                    )
                self.database.connection.commit()
            except Exception:
                self.database.connection.rollback()
                raise
            finally:
                cursor.close()

    def load_reasoning_graph(self, run_id: str) -> dict:
        """读回结论图（graph-rag-v2 通用包 Phase 06 · P06-01 仓储读侧）。

        与 `persist_reasoning_graph()` 对称：**只读、零新表、零迁移**，读四张既有表
        （qa_claims / qa_claim_evidence / qa_conflicts / qa_evidence）的 payload 原文。
        claim 优先取 `stage='conflict_review'`（= canonical claim 节点，由
        `persist_reasoning_graph` 写入），该 stage 没有行时（老 run / fast 路径）
        退回全部行，让调用方自己判断。任何异常都退化成空列表并留 `error`——
        读图绝不能把调用方打断。
        """
        result = {"run_id": str(run_id), "claims": [], "edges": [], "conflicts": [],
                  "evidence": [], "stage": "", "error": ""}
        try:
            with self.database.lock:
                cursor = self.database.connection.cursor()
                try:
                    cursor.execute(
                        "SELECT count(*) FROM qa_claims WHERE run_id=? AND stage='conflict_review'",
                        (str(run_id),))
                    row = cursor.fetchone()
                    has_canonical = bool(int((row[0] if row else 0) or 0))
                    result["stage"] = "conflict_review" if has_canonical else "all"
                    where = "run_id=? AND stage='conflict_review'" if has_canonical else "run_id=?"
                    cursor.execute(
                        "SELECT claim_key,stage,claim_text,claim_type,confidence,valid_from,valid_to,"
                        "scope_json,verification_status,payload_json FROM qa_claims WHERE " + where,
                        (str(run_id),))
                    for item in cursor.fetchall():
                        value = _row_dict(item) or {}
                        result["claims"].append({
                            "claim_key": str(value.get("claim_key") or ""),
                            "stage": str(value.get("stage") or ""),
                            "claim_text": str(value.get("claim_text") or ""),
                            "claim_type": str(value.get("claim_type") or ""),
                            "confidence": value.get("confidence"),
                            "valid_from": value.get("valid_from"),
                            "valid_to": value.get("valid_to"),
                            "scope": _decode_json(value.get("scope_json"), []),
                            "verification_status": str(value.get("verification_status") or ""),
                            "payload": _decode_json(value.get("payload_json"), {}),
                        })
                    cursor.execute(
                        "SELECT claim_key,evidence_ref,relationship,relevance_score "
                        "FROM qa_claim_evidence WHERE run_id=?", (str(run_id),))
                    for item in cursor.fetchall():
                        value = _row_dict(item) or {}
                        result["edges"].append({
                            "claim_key": str(value.get("claim_key") or ""),
                            "evidence_ref": str(value.get("evidence_ref") or ""),
                            "relationship": str(value.get("relationship") or ""),
                            "relevance_score": value.get("relevance_score"),
                        })
                    cursor.execute(
                        "SELECT conflict_key,conflict_type,resolution,rationale,payload_json "
                        "FROM qa_conflicts WHERE run_id=?", (str(run_id),))
                    for item in cursor.fetchall():
                        value = _row_dict(item) or {}
                        payload = _decode_json(value.get("payload_json"), {})
                        conflict = dict(payload) if isinstance(payload, Mapping) else {}
                        conflict.setdefault("conflict_id", str(value.get("conflict_key") or ""))
                        conflict.setdefault("conflict_type", str(value.get("conflict_type") or ""))
                        conflict.setdefault("resolution", str(value.get("resolution") or ""))
                        conflict.setdefault("rationale", str(value.get("rationale") or ""))
                        result["conflicts"].append(conflict)
                    cursor.execute(
                        "SELECT evidence_ref,source_type,source_url,source_title,published_at,"
                        "authority_level,payload_json FROM qa_evidence WHERE run_id=?", (str(run_id),))
                    for item in cursor.fetchall():
                        value = _row_dict(item) or {}
                        result["evidence"].append({
                            "evidence_ref": str(value.get("evidence_ref") or ""),
                            "source_type": str(value.get("source_type") or ""),
                            "source_url": str(value.get("source_url") or ""),
                            "source_title": str(value.get("source_title") or ""),
                            "published_at": value.get("published_at"),
                            "authority_level": value.get("authority_level"),
                            "payload": _decode_json(value.get("payload_json"), {}),
                        })
                finally:
                    cursor.close()
        except Exception as exc:  # noqa: BLE001  读图失败只留痕，不抛
            result["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
        return result

    def prepare_retry(self, run_id: str, stage: str, *, owner_user_id: str) -> dict | None:
        """Remove only the requested stage and downstream snapshots; keep earlier evidence."""
        orders = {
            "level2_retrieval": ("level2_retrieval", "level2_research", "conflict_review", "synthesis", "citation_validation"),
            "level2_research": ("level2_research", "conflict_review", "synthesis", "citation_validation"),
            "synthesis": ("synthesis", "citation_validation"),
        }
        stages = orders.get(str(stage))
        if not stages:
            raise ValueError("不支持重试该阶段")
        run = self.get_run(run_id, owner_user_id=owner_user_id)
        if not run:
            return None
        placeholders = ",".join("?" for _ in stages)
        with self.database.lock:
            cursor = self.database.connection.cursor()
            try:
                cursor.execute(
                    f"DELETE FROM qa_stage_runs WHERE run_id=? AND stage IN ({placeholders})",
                    (str(run_id), *stages),
                )
                if "level2_research" in stages:
                    cursor.execute("DELETE FROM qa_claims WHERE run_id=? AND stage IN ('level2_research','conflict_review')", (str(run_id),))
                    cursor.execute("DELETE FROM qa_conflicts WHERE run_id=?", (str(run_id),))
                    cursor.execute("DELETE FROM qa_claim_evidence WHERE run_id=? AND claim_key NOT IN (SELECT claim_key FROM qa_claims WHERE run_id=?)", (str(run_id), str(run_id)))
                degradation = [
                    item for item in run.get("degradation") or []
                    if str(item.get("stage") or "") not in stages
                ]
                cursor.execute(
                    """
                    UPDATE qa_runs SET status='retry_wait',current_stage=?,completed_at=NULL,
                        final_answer_json='{}',degraded=?,degradation_json=?,updated_at=?
                    WHERE id=? AND owner_user_id=?
                    """,
                    (
                        str(stage), 1 if degradation else 0, _json(degradation), _now(),
                        str(run_id), str(owner_user_id),
                    ),
                )
                self.database.connection.commit()
            except Exception:
                self.database.connection.rollback()
                raise
            finally:
                cursor.close()
        return self.get_run(run_id, owner_user_id=owner_user_id)

    def record_stage(
        self,
        run_id: str,
        stage: str,
        *,
        status: str,
        attempt: int = 1,
        input_hash: str = "",
        output_hash: str = "",
        error_code: str = "",
        details: Mapping | None = None,
        node_id: str = "",
        node_kind: str = "",
        parent_node_id: str = "",
        round_index: int = 0,
    ) -> None:
        """记录一个阶段的执行（幂等：同 run+stage+attempt 覆盖）。

        Phase 01（F-8）：补 node-run 载体。三个 node 参数都是可选的——
        不传时 `node_id` 默认等于 `stage`、`node_kind` 默认 `"execution"`、
        `parent_node_id` 默认空串，因此**现有调用点零改动**即可产出 node-run 行；
        阶段 05 真建执行 DAG 时，谁有真 node_id 谁显式传。
        `round_index` 即 SearchTrace 的 `round`（qa_stage_runs 里没有同义列，attempt 是重试次数）。
        """
        self.ensure_schema()
        now = _now()
        node_id = str(node_id or stage)
        node_kind = str(node_kind or "execution")
        parent_node_id = str(parent_node_id or "")
        round_index = int(round_index or 0)
        completed = now if status in {"completed", "failed", "cancelled", "degraded"} else None
        with self.database.lock:
            prior = self.database.connection.execute(
                "SELECT started_at FROM qa_stage_runs WHERE run_id=? AND stage=? AND attempt=?",
                (str(run_id), str(stage), int(attempt)),
            ).fetchone()
            latency_ms = None
            if completed:
                try:
                    started_text = str(prior[0] if prior else now).replace("Z", "+00:00")
                    completed_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
                    latency_ms = max(0, int((completed_dt - datetime.fromisoformat(started_text)).total_seconds() * 1000))
                except (TypeError, ValueError):
                    latency_ms = None
            token_usage = dict((details or {}).get("token_usage") or {}) if isinstance((details or {}).get("token_usage"), Mapping) else {}
            self.database.connection.execute(
                """
                INSERT INTO qa_stage_runs(
                    run_id,stage,attempt,status,started_at,completed_at,
                    latency_ms,input_hash,output_hash,token_usage_json,error_code,details_json,
                    node_id,node_kind,parent_node_id,round_index
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id,stage,attempt) DO UPDATE SET
                    status=excluded.status,
                    completed_at=excluded.completed_at,
                    latency_ms=excluded.latency_ms,
                    input_hash=excluded.input_hash,
                    output_hash=excluded.output_hash,
                    token_usage_json=excluded.token_usage_json,
                    error_code=excluded.error_code,
                    details_json=excluded.details_json,
                    -- node 字段只在"调用方显式传了非默认值"时才覆盖：
                    -- 否则阶段 05 传过真 node_id 后，后续只更新状态的调用会把它打回 stage。
                    node_id=CASE WHEN excluded.node_id<>excluded.stage
                                 THEN excluded.node_id ELSE qa_stage_runs.node_id END,
                    node_kind=CASE WHEN excluded.node_kind<>'execution'
                                   THEN excluded.node_kind ELSE qa_stage_runs.node_kind END,
                    parent_node_id=CASE WHEN excluded.parent_node_id<>''
                                        THEN excluded.parent_node_id
                                        ELSE qa_stage_runs.parent_node_id END,
                    round_index=CASE WHEN excluded.round_index>qa_stage_runs.round_index
                                     THEN excluded.round_index
                                     ELSE qa_stage_runs.round_index END
                """,
                (
                    str(run_id),
                    str(stage),
                    int(attempt),
                    str(status),
                    now,
                    completed,
                    latency_ms,
                    str(input_hash or ""),
                    str(output_hash or ""),
                    _json(token_usage),
                    str(error_code or ""),
                    _json(details or {}),
                    node_id,
                    node_kind,
                    parent_node_id,
                    round_index,
                ),
            )
            self.database.connection.commit()

    def list_audit_events(self, *, trace_id: str = "", limit: int = 200) -> list[dict]:
        self.ensure_schema()
        sql = "SELECT trace_id,run_id,event_type,actor_id,origin,industry_pack_id,payload_json,created_at FROM qa_audit_events"
        params: list = []
        if trace_id:
            sql += " WHERE trace_id=?"
            params.append(str(trace_id))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self.database.lock:
            rows = self.database.connection.execute(sql, tuple(params)).fetchall()
        result = []
        for row in rows:
            value = _row_dict(row)
            try:
                value["payload"] = json.loads(value.get("payload_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                value["payload"] = {}
            value.pop("payload_json", None)
            result.append(value)
        return result


__all__ = ["QaStore"]
