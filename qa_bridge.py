#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Short-lived, replay-safe identity bridge for the RAGFlow Web host.

The RAGFlow backend (never browser JavaScript) signs a one-time assertion.  The
Gateway exchanges it for an HttpOnly bridge session, so EventSource can resume
without exposing a service credential to the browser.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Mapping

from flask import jsonify, request

from qa_schema import ensure_qa_tables


BRIDGE_ASSERTION_AUDIENCE = "unified-qa-gateway"
BRIDGE_ASSERTION_TTL_SECONDS = 90
BRIDGE_SESSION_TTL_SECONDS = 1800
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:@-]{1,200}$")
_SAFE_PACK = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_manager_override = None


class QaBridgeError(ValueError):
    def __init__(self, message: str, *, code: str = "BRIDGE_INVALID", status: int = 401):
        super().__init__(message)
        self.code = code
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _time_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _secret(value: str | None = None) -> bytes:
    raw = str(value if value is not None else os.getenv("QA_RAGFLOW_BRIDGE_SECRET", "")).encode("utf-8")
    if len(raw) < 32:
        raise QaBridgeError(
            "RAGFlow 身份桥接尚未配置，请联系管理员。",
            code="BRIDGE_NOT_CONFIGURED",
            status=503,
        )
    return raw


def encode_bridge_token(claims: Mapping, *, secret: str | None = None) -> str:
    body = _b64encode(json.dumps(dict(claims), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = _b64encode(hmac.new(_secret(secret), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def decode_bridge_token(token: str, *, secret: str | None = None) -> dict:
    try:
        body, supplied = str(token or "").split(".", 1)
        expected = _b64encode(hmac.new(_secret(secret), body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(supplied, expected):
            raise ValueError("signature")
        value = json.loads(_b64decode(body).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("payload")
        return value
    except QaBridgeError:
        raise
    except Exception as exc:
        raise QaBridgeError("RAGFlow 登录桥接签名无效。", code="BRIDGE_SIGNATURE_INVALID") from exc


def issue_bridge_assertion(
    *, subject_id: str, owner_user_id: str, industry_pack_id: str,
    allowed_kb_ids: list[str] | None = None, secret: str | None = None,
    now: datetime | None = None, nonce: str | None = None,
) -> str:
    """Reference issuer used by the RAGFlow BFF integration and tests."""
    instant = now or _now()
    claims = {
        "typ": "qa_bridge_assertion_v1",
        "aud": BRIDGE_ASSERTION_AUDIENCE,
        "origin": "ragflow_ui",
        "sub": str(subject_id),
        "owner_user_id": str(owner_user_id),
        "industry_pack_id": str(industry_pack_id),
        "allowed_kb_ids": list(allowed_kb_ids or []),
        "iat": int(instant.timestamp()),
        "exp": int((instant + timedelta(seconds=BRIDGE_ASSERTION_TTL_SECONDS)).timestamp()),
        "nonce": str(nonce or secrets.token_urlsafe(24)),
    }
    return encode_bridge_token(claims, secret=secret)


class QaBridgeManager:
    def __init__(self, database, *, secret: str | None = None, now_provider=None):
        self.database = database
        self.secret = secret
        self.now_provider = now_provider or _now

    def _ensure(self):
        self.database._ensure_connection()
        with self.database.lock:
            cursor = self.database.connection.cursor()
            try:
                ensure_qa_tables(cursor)
                self.database.connection.commit()
            finally:
                cursor.close()

    def _validate_assertion(self, token: str) -> dict:
        claims = decode_bridge_token(token, secret=self.secret)
        now = int(self.now_provider().timestamp())
        if claims.get("typ") != "qa_bridge_assertion_v1" or claims.get("aud") != BRIDGE_ASSERTION_AUDIENCE or claims.get("origin") != "ragflow_ui":
            raise QaBridgeError("RAGFlow 登录桥接用途无效。", code="BRIDGE_CLAIMS_INVALID")
        try:
            issued = int(claims.get("iat"))
            expires = int(claims.get("exp"))
        except (TypeError, ValueError) as exc:
            raise QaBridgeError("RAGFlow 登录桥接时间无效。", code="BRIDGE_CLAIMS_INVALID") from exc
        if issued > now + 30 or expires <= now:
            raise QaBridgeError("RAGFlow 登录桥接已过期，请刷新页面重新进入。", code="BRIDGE_EXPIRED")
        if expires - issued > BRIDGE_ASSERTION_TTL_SECONDS + 5:
            raise QaBridgeError("RAGFlow 登录桥接有效期异常。", code="BRIDGE_CLAIMS_INVALID")
        for key in ("sub", "owner_user_id", "nonce"):
            if not _SAFE_ID.fullmatch(str(claims.get(key) or "")):
                raise QaBridgeError("RAGFlow 登录桥接身份无效。", code="BRIDGE_CLAIMS_INVALID")
        pack = str(claims.get("industry_pack_id") or "")
        if not _SAFE_PACK.fullmatch(pack):
            raise QaBridgeError("RAGFlow 登录桥接行业包无效。", code="BRIDGE_CLAIMS_INVALID")
        kb_ids = claims.get("allowed_kb_ids") or []
        if not isinstance(kb_ids, list) or any(not _SAFE_ID.fullmatch(str(item)) for item in kb_ids[:20]):
            raise QaBridgeError("RAGFlow 登录桥接知识库范围无效。", code="BRIDGE_CLAIMS_INVALID")
        claims["allowed_kb_ids"] = list(dict.fromkeys(str(item) for item in kb_ids))[:20]
        return claims

    def exchange(self, assertion: str) -> tuple[str, dict]:
        claims = self._validate_assertion(assertion)
        self._ensure()
        now = self.now_provider()
        nonce_hash = hashlib.sha256(str(claims["nonce"]).encode("utf-8")).hexdigest()
        with self.database.lock:
            try:
                self.database.connection.execute(
                    "INSERT INTO qa_bridge_assertion_nonces(nonce_hash,subject_id,expires_at,used_at) VALUES (?,?,?,?)",
                    (nonce_hash, str(claims["sub"]), _time_text(datetime.fromtimestamp(int(claims["exp"]), timezone.utc)), _time_text(now)),
                )
                self.database.connection.commit()
            except Exception as exc:
                self.database.connection.rollback()
                raise QaBridgeError("该 RAGFlow 登录桥接已使用，请刷新页面重新进入。", code="BRIDGE_REPLAYED") from exc

        session_nonce = secrets.token_urlsafe(32)
        expires = now + timedelta(seconds=BRIDGE_SESSION_TTL_SECONDS)
        session_claims = {
            "typ": "qa_bridge_session_v1", "origin": "ragflow_ui",
            "sub": claims["sub"], "owner_user_id": claims["owner_user_id"],
            "industry_pack_id": claims["industry_pack_id"],
            "allowed_kb_ids": claims["allowed_kb_ids"],
            "iat": int(now.timestamp()), "exp": int(expires.timestamp()), "nonce": session_nonce,
        }
        token = encode_bridge_token(session_claims, secret=self.secret)
        session_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.database.lock:
            self.database.connection.execute(
                """INSERT INTO qa_bridge_sessions(
                    session_hash,subject_id,owner_user_id,industry_pack_id,
                    allowed_kb_ids_json,created_at,expires_at,revoked_at
                ) VALUES (?,?,?,?,?,?,?,NULL)""",
                (
                    session_hash, str(claims["sub"]), str(claims["owner_user_id"]),
                    str(claims["industry_pack_id"]), json.dumps(claims["allowed_kb_ids"]),
                    _time_text(now), _time_text(expires),
                ),
            )
            self.database.connection.commit()
        return token, session_claims

    def authenticate(self, token: str) -> dict:
        claims = decode_bridge_token(token, secret=self.secret)
        now = int(self.now_provider().timestamp())
        if claims.get("typ") != "qa_bridge_session_v1" or claims.get("origin") != "ragflow_ui":
            raise QaBridgeError("RAGFlow 问答会话无效。", code="BRIDGE_SESSION_INVALID")
        try:
            if int(claims.get("exp")) <= now:
                raise QaBridgeError("RAGFlow 问答会话已过期，请重新进入。", code="BRIDGE_SESSION_EXPIRED")
        except (TypeError, ValueError) as exc:
            raise QaBridgeError("RAGFlow 问答会话无效。", code="BRIDGE_SESSION_INVALID") from exc
        session_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
        self._ensure()
        with self.database.lock:
            row = self.database.connection.execute(
                "SELECT * FROM qa_bridge_sessions WHERE session_hash=? AND revoked_at IS NULL",
                (session_hash,),
            ).fetchone()
        if not row:
            raise QaBridgeError("RAGFlow 问答会话已失效，请重新进入。", code="BRIDGE_SESSION_REVOKED")
        value = dict(row)
        if value.get("owner_user_id") != claims.get("owner_user_id") or value.get("industry_pack_id") != claims.get("industry_pack_id"):
            raise QaBridgeError("RAGFlow 问答会话身份不一致。", code="BRIDGE_SESSION_INVALID")
        return claims

    def revoke(self, token: str) -> None:
        session_hash = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()
        self._ensure()
        with self.database.lock:
            self.database.connection.execute(
                "UPDATE qa_bridge_sessions SET revoked_at=? WHERE session_hash=? AND revoked_at IS NULL",
                (_time_text(self.now_provider()), session_hash),
            )
            self.database.connection.commit()


def set_qa_bridge_manager(manager) -> None:
    global _manager_override
    _manager_override = manager


def get_qa_bridge_manager() -> QaBridgeManager:
    if _manager_override is not None:
        return _manager_override
    from sqlite_database import sqlite_db

    return QaBridgeManager(sqlite_db)


def bridge_token_from_request() -> str:
    return str(request.cookies.get("qa_bridge_session") or request.headers.get("X-QA-Bridge-Session") or "")


def qa_access_required(function):
    @wraps(function)
    def decorated(*args, **kwargs):
        token = bridge_token_from_request()
        if token:
            try:
                claims = get_qa_bridge_manager().authenticate(token)
            except QaBridgeError as exc:
                return jsonify({"success": False, "error": {"code": exc.code, "message": str(exc)}}), exc.status
            request.current_user = {
                "role": "qa_bridge", "subject_id": claims["sub"],
                "owner_user_id": claims["owner_user_id"],
                "pack_id": claims["industry_pack_id"],
                "allowed_kb_ids": claims.get("allowed_kb_ids") or [],
            }
            return function(*args, **kwargs)
        from decorators import login_required

        return login_required(function)(*args, **kwargs)
    return decorated


__all__ = [
    "BRIDGE_ASSERTION_TTL_SECONDS", "BRIDGE_SESSION_TTL_SECONDS", "QaBridgeError",
    "QaBridgeManager", "bridge_token_from_request", "decode_bridge_token",
    "encode_bridge_token", "get_qa_bridge_manager", "issue_bridge_assertion",
    "qa_access_required", "set_qa_bridge_manager",
]
