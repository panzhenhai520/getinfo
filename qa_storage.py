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

    # ── Phase 09（P09-01…P09-06）：Memory Graph Core 仓储 ────────────────────
    # 口径（与 Phase 02 的 seen 表一致）：
    #   · 记忆**不物理覆盖**（§2.3）：每次变更都追加一行 memory_version，memory_item 只是当前视图；
    #   · 每条记忆都要能回到证据（§12 provenance）：memory_evidence_link 存 Phase 02 的
    #     来源指纹/span 指纹 + Phase 03 的 verdict；
    #   · 读侧一律"异常退化成空/错误码"，绝不把召回或留痕的失败冒泡成问答失败。

    def save_memory_item(self, item: Mapping) -> dict:
        """写入/刷新一条记忆（按 `memory_id` 内容寻址 + 作用域内容指纹 upsert）。

        语义（确定性、可复跑）：
          · 同一个 `memory_id` 再写一次**不覆盖历史**：`memory_version` 追加一行，
            `memory_item` 更新成当前视图（version 递增）；
          · `reuse_count` / `recall_count` / `created_*` 由**既有行保留**（写入方不负责回填历史）；
          · 返回写入行（含 version 与是否新建）。异常吞掉并回 `{..., "error": ...}`。
        """
        item = dict(item) if isinstance(item, Mapping) else {}
        now = _now()
        memory_id = str(item.get("memory_id") or "")
        result = {"memory_id": memory_id, "created": False, "version": 0, "error": ""}
        if not memory_id:
            result["error"] = "memory_id 为空"
            return result
        entities = list(item.get("entity_ids") or [])
        evidence_ids = list(item.get("source_evidence_ids") or [])
        try:
            self.ensure_schema()
            with self.database.lock:
                cursor = self.database.connection.cursor()
                try:
                    existing = cursor.execute(
                        "SELECT version, reuse_count, recall_count, created_at FROM memory_item "
                        "WHERE memory_id=?", (memory_id,)).fetchone()
                    current = _row_dict(existing) or {}
                    version = int(current.get("version") or 0) + 1
                    created = not current
                    cursor.execute(
                        """
                        INSERT INTO memory_item(
                            memory_id,memory_type,canonical_content,content_fingerprint,confidence,
                            freshness_class,valid_from,valid_until,last_verified_at,status,scope,
                            scope_key,owner_user_id,session_id,industry_pack_id,entity_ids_json,
                            source_evidence_ids_json,superseded_by,reuse_count,recall_count,
                            created_from_session_id,created_from_run_id,version,decay_score,
                            payload_json,created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(memory_id) DO UPDATE SET
                            canonical_content=excluded.canonical_content,
                            content_fingerprint=excluded.content_fingerprint,
                            confidence=excluded.confidence,
                            freshness_class=excluded.freshness_class,
                            valid_from=excluded.valid_from,
                            valid_until=excluded.valid_until,
                            last_verified_at=excluded.last_verified_at,
                            status=excluded.status,
                            scope=excluded.scope,
                            scope_key=excluded.scope_key,
                            entity_ids_json=excluded.entity_ids_json,
                            source_evidence_ids_json=excluded.source_evidence_ids_json,
                            superseded_by=excluded.superseded_by,
                            version=excluded.version,
                            decay_score=excluded.decay_score,
                            payload_json=excluded.payload_json,
                            updated_at=excluded.updated_at
                        """,
                        (
                            memory_id, str(item.get("memory_type") or "VERIFIED_CLAIM"),
                            str(item.get("canonical_content") or ""),
                            str(item.get("content_fingerprint") or ""),
                            float(item.get("confidence") or 0),
                            str(item.get("freshness_class") or "MEDIUM"),
                            str(item.get("valid_from") or ""), str(item.get("valid_until") or ""),
                            str(item.get("last_verified_at") or ""),
                            str(item.get("status") or "ACTIVE"), str(item.get("scope") or "SESSION"),
                            str(item.get("scope_key") or ""),
                            str(item.get("owner_user_id") or ""), str(item.get("session_id") or ""),
                            str(item.get("industry_pack_id") or ""), _json(entities),
                            _json(evidence_ids), str(item.get("superseded_by") or ""),
                            int(current.get("reuse_count") or 0), int(current.get("recall_count") or 0),
                            str(item.get("created_from_session_id") or ""),
                            str(item.get("created_from_run_id") or ""), version,
                            float(item.get("decay_score") or 0),
                            _json(item.get("metadata") or {}),
                            str(current.get("created_at") or now), now,
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO memory_version(
                            memory_id,version,change,status,confidence,canonical_content,
                            valid_until,last_verified_at,decay_score,reason,payload_json,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(memory_id,version) DO UPDATE SET
                            change=excluded.change, status=excluded.status,
                            confidence=excluded.confidence, decay_score=excluded.decay_score,
                            reason=excluded.reason, payload_json=excluded.payload_json
                        """,
                        (
                            memory_id, version, str(item.get("change") or "CREATE"),
                            str(item.get("status") or "ACTIVE"), float(item.get("confidence") or 0),
                            str(item.get("canonical_content") or ""), str(item.get("valid_until") or ""),
                            str(item.get("last_verified_at") or ""), float(item.get("decay_score") or 0),
                            str(item.get("change_reason") or ""), _json(item.get("metadata") or {}), now,
                        ),
                    )
                    for entity in entities:
                        if isinstance(entity, Mapping):
                            key = str(entity.get("entity_key") or entity.get("text") or "")
                            text = str(entity.get("text") or key)
                            role = str(entity.get("role") or "subject")
                        else:
                            key = text = str(entity or "")
                            role = "subject"
                        if not key:
                            continue
                        cursor.execute(
                            """
                            INSERT INTO memory_entity_link(memory_id,entity_key,entity_text,role,payload_json,created_at)
                            VALUES(?,?,?,?,?,?)
                            ON CONFLICT(memory_id,entity_key,role) DO UPDATE SET
                                entity_text=excluded.entity_text
                            """,
                            (memory_id, key, text, role, _json({}), now),
                        )
                    self.database.connection.commit()
                    result.update({"created": created, "version": version})
                except Exception:
                    self.database.connection.rollback()
                    raise
                finally:
                    cursor.close()
        except Exception as exc:  # noqa: BLE001
            result["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
        return result

    def link_memory_evidence(self, memory_id: str, links: list) -> int:
        """绑定记忆↔证据（§12 memory_evidence_link）：provenance 的唯一载体。

        主键 (memory_id, evidence_ref, source_fingerprint) 保证同一绑定只留一行（幂等）。
        """
        rows = [dict(row) for row in (links or []) if isinstance(row, Mapping)]
        if not rows:
            return 0
        now = _now()
        written = 0
        try:
            self.ensure_schema()
            with self.database.lock:
                cursor = self.database.connection.cursor()
                try:
                    for row in rows:
                        evidence_ref = str(row.get("evidence_ref") or "")
                        if not evidence_ref:
                            continue
                        cursor.execute(
                            """
                            INSERT INTO memory_evidence_link(
                                memory_id,evidence_ref,source_fingerprint,span_fingerprint,run_id,
                                stage,route,corpus_version,verdict,evidence_score,relationship,
                                payload_json,created_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(memory_id,evidence_ref,source_fingerprint) DO UPDATE SET
                                span_fingerprint=excluded.span_fingerprint,
                                verdict=excluded.verdict, evidence_score=excluded.evidence_score,
                                relationship=excluded.relationship, payload_json=excluded.payload_json
                            """,
                            (
                                str(memory_id), evidence_ref,
                                str(row.get("source_fingerprint") or ""),
                                str(row.get("span_fingerprint") or ""), str(row.get("run_id") or ""),
                                str(row.get("stage") or ""), str(row.get("route") or ""),
                                str(row.get("corpus_version") or ""), str(row.get("verdict") or ""),
                                float(row.get("evidence_score") or 0),
                                str(row.get("relationship") or ""),
                                _json(row.get("metadata") or {}), now,
                            ),
                        )
                        written += 1
                    self.database.connection.commit()
                except Exception:
                    self.database.connection.rollback()
                    raise
                finally:
                    cursor.close()
        except Exception:  # noqa: BLE001 —— 绑定失败不冒泡（写入方按"未绑定"处理）
            return 0
        return written

    def add_memory_relation(self, rows: list) -> int:
        """写记忆图关系（§1.4 的九个取值由契约守门；本阶段只写四个自有关系）。"""
        written = 0
        now = _now()
        try:
            self.ensure_schema()
            with self.database.lock:
                cursor = self.database.connection.cursor()
                try:
                    for row in rows or []:
                        if not isinstance(row, Mapping):
                            continue
                        memory_id = str(row.get("memory_id") or "")
                        relation = str(row.get("relation") or "")
                        if not memory_id or not relation:
                            continue
                        cursor.execute(
                            """
                            INSERT INTO memory_relation(
                                memory_id,relation,target_memory_id,target_kind,target_ref,weight,
                                rationale,created_from_run_id,version,payload_json,created_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(memory_id,relation,target_memory_id,target_ref) DO UPDATE SET
                                weight=excluded.weight, rationale=excluded.rationale,
                                payload_json=excluded.payload_json
                            """,
                            (
                                memory_id, relation, str(row.get("target_memory_id") or ""),
                                str(row.get("target_kind") or "memory"), str(row.get("target_ref") or ""),
                                float(row.get("weight") or 0), str(row.get("rationale") or ""),
                                str(row.get("run_id") or ""), int(row.get("version") or 1),
                                _json(row.get("metadata") or {}), now,
                            ),
                        )
                        written += 1
                    self.database.connection.commit()
                except Exception:
                    self.database.connection.rollback()
                    raise
                finally:
                    cursor.close()
        except Exception:  # noqa: BLE001
            return 0
        return written

    def record_memory_write_decision(self, rows: list) -> int:
        """落写决策留痕（P09-03）：**每一条候选记忆**都要有一行（含 DROP）。"""
        written = 0
        now = _now()
        try:
            self.ensure_schema()
            with self.database.lock:
                cursor = self.database.connection.cursor()
                try:
                    for row in rows or []:
                        if not isinstance(row, Mapping):
                            continue
                        decision_id = str(row.get("decision_id") or "")
                        if not decision_id:
                            continue
                        cursor.execute(
                            """
                            INSERT INTO memory_write_decision(
                                decision_id,run_id,memory_type,decision,reason,utility,factors_json,
                                memory_id,content_fingerprint,evidence_refs_json,scope,scope_key,
                                gate_version,payload_json,created_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(decision_id) DO UPDATE SET
                                decision=excluded.decision, reason=excluded.reason,
                                utility=excluded.utility, factors_json=excluded.factors_json,
                                payload_json=excluded.payload_json
                            """,
                            (
                                decision_id, str(row.get("run_id") or ""),
                                str(row.get("memory_type") or ""), str(row.get("decision") or "DROP"),
                                str(row.get("reason") or ""), float(row.get("utility") or 0),
                                _json(row.get("factors") or {}), str(row.get("memory_id") or ""),
                                str(row.get("content_fingerprint") or ""),
                                _json(row.get("evidence_refs") or []), str(row.get("scope") or ""),
                                str(row.get("scope_key") or ""), str(row.get("gate_version") or ""),
                                _json(row.get("metadata") or {}), now,
                            ),
                        )
                        written += 1
                    self.database.connection.commit()
                except Exception:
                    self.database.connection.rollback()
                    raise
                finally:
                    cursor.close()
        except Exception:  # noqa: BLE001
            return 0
        return written

    def record_memory_recall(self, row: Mapping) -> bool:
        """落一条召回日志（P09-04）：谁在什么模式/作用域下召回了什么，可复算。"""
        row = dict(row) if isinstance(row, Mapping) else {}
        recall_id = str(row.get("recall_id") or "")
        if not recall_id:
            return False
        try:
            self.ensure_schema()
            with self.database.lock:
                cursor = self.database.connection.cursor()
                try:
                    cursor.execute(
                        """
                        INSERT INTO memory_recall_log(
                            recall_id,trace_id,run_id,mode,scope_key,owner_user_id,session_id,
                            industry_pack_id,query_fingerprint,channels_json,hits,top_score,
                            counts_json,payload_json,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(recall_id) DO UPDATE SET
                            hits=excluded.hits, top_score=excluded.top_score,
                            counts_json=excluded.counts_json, payload_json=excluded.payload_json
                        """,
                        (
                            recall_id, str(row.get("trace_id") or ""), str(row.get("run_id") or ""),
                            str(row.get("mode") or ""), str(row.get("scope_key") or ""),
                            str(row.get("owner_user_id") or ""), str(row.get("session_id") or ""),
                            str(row.get("industry_pack_id") or ""),
                            str(row.get("query_fingerprint") or ""),
                            _json(row.get("channels") or []), int(row.get("hits") or 0),
                            float(row.get("top_score") or 0), _json(row.get("counts") or {}),
                            _json(row.get("metadata") or {}), _now(),
                        ),
                    )
                    self.database.connection.commit()
                    return True
                except Exception:
                    self.database.connection.rollback()
                    raise
                finally:
                    cursor.close()
        except Exception:  # noqa: BLE001
            return False

    def bump_memory_usage(self, memory_ids, *, day: str = "", recalled: int = 0,
                          used: int = 0, helped: int = 0, reuse: bool = False) -> int:
        """按天累计记忆使用统计（P09-04 `memory_usage_stat` + `memory_item.reuse_count`）。

        `reuse=True` 才递增 `memory_item.reuse_count`（§2.3 的"复用次数"）：
        召回只是"给过提示"，**用过**才算复用 —— 两者分开记，召回分才不会被刷高。
        """
        keys = [str(item) for item in (memory_ids or []) if str(item or "")]
        if not keys:
            return 0
        day = str(day or _now()[:10])
        now = _now()
        written = 0
        try:
            self.ensure_schema()
            with self.database.lock:
                cursor = self.database.connection.cursor()
                try:
                    for memory_id in keys:
                        cursor.execute(
                            """
                            INSERT INTO memory_usage_stat(memory_id,day,recalled,used,helped,created_at,updated_at)
                            VALUES(?,?,?,?,?,?,?)
                            ON CONFLICT(memory_id,day) DO UPDATE SET
                                recalled=memory_usage_stat.recalled+excluded.recalled,
                                used=memory_usage_stat.used+excluded.used,
                                helped=memory_usage_stat.helped+excluded.helped,
                                updated_at=excluded.updated_at
                            """,
                            (memory_id, day, int(recalled or 0), int(used or 0), int(helped or 0), now, now),
                        )
                        cursor.execute(
                            "UPDATE memory_item SET recall_count=recall_count+?, "
                            "reuse_count=reuse_count+?, updated_at=? WHERE memory_id=?",
                            (int(recalled or 0), (int(used or 0) if reuse else 0), now, memory_id),
                        )
                        written += 1
                    self.database.connection.commit()
                except Exception:
                    self.database.connection.rollback()
                    raise
                finally:
                    cursor.close()
        except Exception:  # noqa: BLE001
            return 0
        return written

    def memory_usage(self, memory_ids=(), *, day: str = "") -> dict:
        """按记忆 id 取使用统计（召回/用过/帮上忙）→ {memory_id: {...}}。"""
        keys = [str(item) for item in (memory_ids or []) if str(item or "")]
        if not keys:
            return {}
        try:
            self.ensure_schema()
            where = ["memory_id IN (%s)" % ",".join("?" for _ in keys)]
            params: list = list(keys)
            if day:
                where.append("day=?")
                params.append(str(day))
            with self.database.lock:
                rows = self.database.connection.execute(
                    "SELECT memory_id,SUM(recalled) AS recalled,SUM(used) AS used,"
                    "SUM(helped) AS helped FROM memory_usage_stat WHERE " + " AND ".join(where)
                    + " GROUP BY memory_id", tuple(params)).fetchall()
            return {str((_row_dict(row) or {}).get("memory_id")): {
                "recalled": int((_row_dict(row) or {}).get("recalled") or 0),
                "used": int((_row_dict(row) or {}).get("used") or 0),
                "helped": int((_row_dict(row) or {}).get("helped") or 0),
            } for row in rows}
        except Exception:  # noqa: BLE001
            return {}

    def load_memory_items(self, *, scope_keys=(), memory_ids=(), memory_types=(), statuses=(),
                          owner_user_id: str = "", industry_pack_id: str = "",
                          include_all_scopes: bool = False, limit: int = 500) -> list[dict]:
        """按作用域/类型/状态读记忆（召回与生命周期维护的共同读侧）。

        作用域口径（§14 + MASTER_RULES 12）：默认**只读给定作用域**；`include_all_scopes=True`
        才允许跨作用域读（维护任务/验收统计用），调用方必须自己为此负责。
        坏 JSON 一律退化成默认值，绝不因一条脏行让整次召回失败。
        """
        result: list[dict] = []
        try:
            self.ensure_schema()
            where, params = [], []
            keys = [str(item) for item in (scope_keys or []) if str(item or "")]
            ids = [str(item) for item in (memory_ids or []) if str(item or "")]
            types = [str(item) for item in (memory_types or []) if str(item or "")]
            stats = [str(item) for item in (statuses or []) if str(item or "")]
            if ids:
                where.append("memory_id IN (%s)" % ",".join("?" for _ in ids))
                params.extend(ids)
            if keys and not include_all_scopes:
                where.append("scope_key IN (%s)" % ",".join("?" for _ in keys))
                params.extend(keys)
            if types:
                where.append("memory_type IN (%s)" % ",".join("?" for _ in types))
                params.extend(types)
            if stats:
                where.append("status IN (%s)" % ",".join("?" for _ in stats))
                params.extend(stats)
            if owner_user_id:
                where.append("owner_user_id=?")
                params.append(str(owner_user_id))
            if industry_pack_id:
                where.append("industry_pack_id=?")
                params.append(str(industry_pack_id))
            sql = "SELECT * FROM memory_item"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY updated_at DESC, memory_id LIMIT ?"
            params.append(max(1, min(int(limit), 5000)))
            with self.database.lock:
                rows = self.database.connection.execute(sql, tuple(params)).fetchall()
            for row in rows:
                value = _row_dict(row) or {}
                value["entity_ids"] = _decode_json(value.get("entity_ids_json"), [])
                value["source_evidence_ids"] = _decode_json(value.get("source_evidence_ids_json"), [])
                value["metadata"] = _decode_json(value.get("payload_json"), {})
                result.append(value)
            return result
        except Exception:  # noqa: BLE001
            return result

    def memory_evidence(self, memory_ids=(), *, evidence_refs=()) -> list[dict]:
        """取记忆↔证据绑定行（provenance 读侧；两个过滤条件都可单独用）。"""
        ids = [str(item) for item in (memory_ids or []) if str(item or "")]
        refs = [str(item) for item in (evidence_refs or []) if str(item or "")]
        if not ids and not refs:
            return []
        try:
            self.ensure_schema()
            where, params = [], []
            if ids:
                where.append("memory_id IN (%s)" % ",".join("?" for _ in ids))
                params.extend(ids)
            if refs:
                where.append("evidence_ref IN (%s)" % ",".join("?" for _ in refs))
                params.extend(refs)
            with self.database.lock:
                rows = self.database.connection.execute(
                    "SELECT * FROM memory_evidence_link WHERE " + " AND ".join(where)
                    + " ORDER BY memory_id, evidence_ref", tuple(params)).fetchall()
            out = []
            for row in rows:
                value = _row_dict(row) or {}
                value["metadata"] = _decode_json(value.get("payload_json"), {})
                out.append(value)
            return out
        except Exception:  # noqa: BLE001
            return []

    def memory_relations(self, memory_ids=(), *, relations=()) -> list[dict]:
        """取记忆图关系（P09-06 图通道；Phase 10 的 SUPERSEDES/CONTRADICTS 也从这里读）。"""
        ids = [str(item) for item in (memory_ids or []) if str(item or "")]
        if not ids:
            return []
        try:
            self.ensure_schema()
            where = ["memory_id IN (%s)" % ",".join("?" for _ in ids)]
            params: list = list(ids)
            picked = [str(item) for item in (relations or []) if str(item or "")]
            if picked:
                where.append("relation IN (%s)" % ",".join("?" for _ in picked))
                params.extend(picked)
            with self.database.lock:
                rows = self.database.connection.execute(
                    "SELECT * FROM memory_relation WHERE " + " AND ".join(where), tuple(params)).fetchall()
            return [_row_dict(row) or {} for row in rows]
        except Exception:  # noqa: BLE001
            return []

    def update_memory_status(self, memory_id: str, *, status: str, decay_score: float | None = None,
                             reason: str = "", change: str = "STATUS", now: str = "",
                             append_version: bool = True) -> dict:
        """生命周期迁移的唯一写入口（P09-05）：状态 + 衰减分 + 追加版本行。

        **不物理覆盖历史**：每次**状态迁移**都追加 `memory_version`（change=STATUS/DECAY/EXPIRE）。
        `append_version=False` 只给"纯衰减分刷新"用：衰减分是 (last_verified_at / 时效档 /
        confidence / reuse_count) 的**可复算派生量**（`qa_memory.decay_score()`），
        为它每跑一次维护就追加一行版本会让版本表无限增长而**不携带任何新信息**；
        状态迁移与内容/置信变化仍然一条不落地追加。
        返回 {memory_id, from, to, version, changed}。
        """
        memory_id = str(memory_id or "")
        result = {"memory_id": memory_id, "from": "", "to": str(status or ""), "version": 0,
                  "changed": False, "error": ""}
        if not memory_id:
            result["error"] = "memory_id 为空"
            return result
        stamp = str(now or _now())
        try:
            self.ensure_schema()
            with self.database.lock:
                cursor = self.database.connection.cursor()
                try:
                    row = cursor.execute(
                        "SELECT status,version,confidence,canonical_content,valid_until,"
                        "last_verified_at,decay_score FROM memory_item WHERE memory_id=?",
                        (memory_id,)).fetchone()
                    current = _row_dict(row)
                    if current is None:
                        result["error"] = "记忆不存在"
                        return result
                    before = str(current.get("status") or "")
                    result["from"] = before
                    version = int(current.get("version") or 0)
                    score = float(current.get("decay_score") or 0) if decay_score is None \
                        else float(decay_score)
                    if before == str(status) and decay_score is None:
                        result["version"] = version
                        return result
                    version += 1
                    cursor.execute(
                        "UPDATE memory_item SET status=?, decay_score=?, version=?, updated_at=? "
                        "WHERE memory_id=?",
                        (str(status), score, version, stamp, memory_id),
                    )
                    if append_version:
                        cursor.execute(
                            """
                            INSERT INTO memory_version(
                                memory_id,version,change,status,confidence,canonical_content,
                                valid_until,last_verified_at,decay_score,reason,payload_json,created_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(memory_id,version) DO UPDATE SET
                                change=excluded.change,status=excluded.status,
                                decay_score=excluded.decay_score,reason=excluded.reason
                            """,
                            (
                                memory_id, version, str(change or "STATUS"), str(status),
                                float(current.get("confidence") or 0),
                                str(current.get("canonical_content") or ""),
                                str(current.get("valid_until") or ""),
                                str(current.get("last_verified_at") or ""), score, str(reason or ""),
                                _json({}), stamp,
                            ),
                        )
                    self.database.connection.commit()
                    result.update({"version": version, "changed": True})
                    return result
                except Exception:
                    self.database.connection.rollback()
                    raise
                finally:
                    cursor.close()
        except Exception as exc:  # noqa: BLE001
            result["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
            return result

    def memory_stats(self, *, scope_keys=(), include_all_scopes: bool = True) -> dict:
        """记忆图统计（验收/运维自检）：按类型/状态/时效档/作用域分组 + 时间范围。"""
        stats = {"items": 0, "by_type": {}, "by_status": {}, "by_freshness": {}, "by_scope": {},
                 "links": {"evidence": 0, "entity": 0, "relation": 0},
                 "decisions": {}, "recalls": {"rows": 0, "hits": 0}, "error": ""}
        try:
            self.ensure_schema()
            keys = [str(item) for item in (scope_keys or []) if str(item or "")]
            where, params = "", []
            if keys and not include_all_scopes:
                where = " WHERE scope_key IN (%s)" % ",".join("?" for _ in keys)
                params = list(keys)
            with self.database.lock:
                cursor = self.database.connection.cursor()
                for column, target in (("memory_type", "by_type"), ("status", "by_status"),
                                       ("freshness_class", "by_freshness"), ("scope", "by_scope")):
                    for row in cursor.execute(
                            "SELECT %s AS k, count(*) AS n FROM memory_item%s GROUP BY 1"
                            % (column, where), tuple(params)).fetchall():
                        value = _row_dict(row) or {}
                        stats[target][str(value.get("k") or "")] = int(value.get("n") or 0)
                stats["items"] = sum(stats["by_status"].values())
                stats["links"]["evidence"] = int(cursor.execute(
                    "SELECT count(*) FROM memory_evidence_link").fetchone()[0] or 0)
                stats["links"]["entity"] = int(cursor.execute(
                    "SELECT count(*) FROM memory_entity_link").fetchone()[0] or 0)
                stats["links"]["relation"] = int(cursor.execute(
                    "SELECT count(*) FROM memory_relation").fetchone()[0] or 0)
                for row in cursor.execute(
                        "SELECT decision, count(*) AS n FROM memory_write_decision GROUP BY 1").fetchall():
                    value = _row_dict(row) or {}
                    stats["decisions"][str(value.get("decision") or "")] = int(value.get("n") or 0)
                row = cursor.execute(
                    "SELECT count(*) AS rows, coalesce(sum(hits),0) AS hits FROM memory_recall_log"
                ).fetchone()
                value = _row_dict(row) or {}
                stats["recalls"] = {"rows": int(value.get("rows") or 0),
                                    "hits": int(value.get("hits") or 0)}
                cursor.close()
        except Exception as exc:  # noqa: BLE001
            stats["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
        return stats

    def memory_write_decisions(self, *, run_id: str = "", limit: int = 500) -> list[dict]:
        """读写决策留痕（验收脚本/运维自检用）。"""
        try:
            self.ensure_schema()
            where, params = "", []
            if run_id:
                where = " WHERE run_id=?"
                params.append(str(run_id))
            params.append(max(1, min(int(limit), 5000)))
            with self.database.lock:
                rows = self.database.connection.execute(
                    "SELECT * FROM memory_write_decision" + where
                    + " ORDER BY id LIMIT ?", tuple(params)).fetchall()
            out = []
            for row in rows:
                value = _row_dict(row) or {}
                value["factors"] = _decode_json(value.get("factors_json"), {})
                value["evidence_refs"] = _decode_json(value.get("evidence_refs_json"), [])
                out.append(value)
            return out
        except Exception:  # noqa: BLE001
            return []


__all__ = ["QaStore"]
