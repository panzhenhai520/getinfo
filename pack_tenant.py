#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多租户（行业包级多用户）隔离基础设施：pack_users + 逐用户 LLM 密钥 + 会话归属。

设计：
- pack_users：一个行业包可配多个登录用户（各用户独立授权时长/到期/联系人）。
- pack_user_llm_keys：每个 pack_user 自己的各 LLM api_key（隔离，不共用全局）；
  chat 的 /api/chat/config 与运行配置会按"当前登录包用户"覆盖其密钥。
- 会话归属：聊天/会话记录带 user_id，用户只能看到自己的会话（isolation）。

身份：当前包用户由 request cookie ``pack_user_id`` 决定（由阶段1登录写入）。
尚未启用（无 cookie）时返回 None，chat 回退到全局配置——隔离机制就绪，登录一接入即生效。
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Dict, List, Optional

from sqlite_database import sqlite_db


def _scramble(secret: str) -> str:
    """轻量混淆（非强加密）：把密钥做成不可逆展示 + 原文可还原存储。生产请换成 KMS/对称加密。"""
    key = 'pack-tenant-2026'
    try:
        from Crypto.Cipher import AES  # noqa
        return 'enc:' + secret  # 有 pycryptodome 时可用 AES-GCM；此处保留明文前缀便于现有 chat 读取
    except Exception:
        return secret


def _unscramble(value: str) -> str:
    return value[4:] if str(value or '').startswith('enc:') else str(value or '')


# ----------------------------------------------------------------------
# 建表（postgres 兼容 BIGSERIAL；接口内联调用，自愈）
# ----------------------------------------------------------------------
def ensure_pack_tables(cursor) -> None:
    # id 用 INTEGER PRIMARY KEY AUTOINCREMENT：兼容层在 PG 上自动翻译成 BIGSERIAL，
    # 反向写 BIGSERIAL 会让 SQLite 路径失去自增（id 变 NULL）。
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS pack_users ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  industry_pack_id TEXT NOT NULL,"
        "  username TEXT NOT NULL,"
        "  password_hash TEXT NOT NULL,"
        "  email TEXT NOT NULL,"
        "  nickname TEXT NOT NULL DEFAULT '',"
        "  avatar TEXT NOT NULL DEFAULT '',"
        "  status TEXT NOT NULL DEFAULT 'active'"
        "    CHECK (status IN ('active', 'disabled', 'expired')),"
        "  init_login_at TEXT NOT NULL DEFAULT '',"          # 加密/不可变
        "  activation_at TEXT NOT NULL DEFAULT '',"
        "  auth_days INTEGER NOT NULL DEFAULT 30,"
        "  expire_at TEXT NOT NULL DEFAULT '',"
        "  contact_phone TEXT NOT NULL DEFAULT '',"
        "  translate_enabled INTEGER NOT NULL DEFAULT 1,"
        "  ai_assistant_enabled INTEGER NOT NULL DEFAULT 1,"
        "  company_name TEXT NOT NULL DEFAULT '',"
        "  remind_phone TEXT NOT NULL DEFAULT '',"
        "  activated INTEGER NOT NULL DEFAULT 0,"
        "  email_verify_code TEXT NOT NULL DEFAULT '',"
        "  email_verify_expires TEXT NOT NULL DEFAULT '',"
        "  must_change_password INTEGER NOT NULL DEFAULT 0,"
        "  last_login_at TEXT NOT NULL DEFAULT '',"
        "  created_at TEXT NOT NULL DEFAULT '',"
        "  updated_at TEXT NOT NULL DEFAULT '',"
        "  UNIQUE(industry_pack_id, username),"
        "  UNIQUE(email)"
        ")"
    )
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS pack_user_llm_keys ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  pack_user_id BIGINT NOT NULL,"
        "  model_id TEXT NOT NULL,"
        "  api_key TEXT NOT NULL DEFAULT '',"
        "  created_at TEXT NOT NULL DEFAULT '',"
        "  updated_at TEXT NOT NULL DEFAULT '',"
        "  UNIQUE(pack_user_id, model_id)"
        ")"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_pack_users_pack ON pack_users(industry_pack_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_pack_llm_user ON pack_user_llm_keys(pack_user_id)"
    )
    # 已存在表补新列（阶段1：邮箱验证码/首次强制改密）
    for col in ("email_verify_code TEXT NOT NULL DEFAULT ''",
                "email_verify_expires TEXT NOT NULL DEFAULT ''",
                "must_change_password INTEGER NOT NULL DEFAULT 0",
                "last_login_at TEXT NOT NULL DEFAULT ''",
                "can_delete_articles INTEGER NOT NULL DEFAULT 1",
                "company_name TEXT NOT NULL DEFAULT ''",
                "remind_phone TEXT NOT NULL DEFAULT ''",
                "activated INTEGER NOT NULL DEFAULT 0",
                "reset_token TEXT NOT NULL DEFAULT ''",
                "reset_expires TEXT NOT NULL DEFAULT ''",
                "logo_url TEXT NOT NULL DEFAULT ''",
                "show_user_brand INTEGER NOT NULL DEFAULT 0"):
        try:
            cursor.execute(f"ALTER TABLE pack_users ADD COLUMN IF NOT EXISTS {col}")
        except Exception:
            try:
                cursor.execute("ALTER TABLE pack_users ADD COLUMN " + col)
            except Exception:
                pass


def _ensure(cursor=None):
    sqlite_db._ensure_connection()
    if cursor is None:
        with sqlite_db.lock:
            c = sqlite_db.connection.cursor()
            ensure_pack_tables(c)
            sqlite_db.connection.commit()
            c.close()
    else:
        ensure_pack_tables(cursor)


def ensure_pack_user_uniqueness() -> Dict:
    """幂等迁移：清理重复包用户行，并补齐唯一约束。

    老库的 pack_users 若是早于 UNIQUE 声明的 DDL 建表，`CREATE TABLE IF NOT EXISTS`
    不会再补约束，于是可能残留重复行（同包同名 / 同邮箱），导致登录查找串到别的行。
    本函数：① 按 (industry_pack_id, username) 去重，保留“最有价值”的一行
    （优先 activated=1，其次仍持有验证码，最后取 id 最大）；② 按非空 email 去重；
    ③ 用唯一索引补齐约束（唯一索引在 SQLite/PostgreSQL 都可用）。
    返回 {"removed": 删除行数, "indexes_added": [...]}。
    """
    _ensure()
    removed = 0
    added = []
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        cur.execute("SELECT * FROM pack_users ORDER BY id")
        rows = [dict(r) for r in cur.fetchall()]

        def _score(r):
            return (
                1 if int(r.get('activated') or 0) == 1 else 0,
                1 if str(r.get('email_verify_code') or '') else 0,
                1 if str(r.get('email') or '').strip() else 0,
                int(r.get('id') or 0),
            )

        drop_ids = []
        groups = {}
        for r in rows:
            groups.setdefault(
                (str(r.get('industry_pack_id') or ''), str(r.get('username') or '')), []
            ).append(r)
        for items in groups.values():
            if len(items) <= 1:
                continue
            keep = max(items, key=_score)
            drop_ids += [int(r['id']) for r in items if int(r['id']) != int(keep['id'])]

        by_email = {}
        for r in rows:
            email = str(r.get('email') or '').strip()
            if email:
                by_email.setdefault(email, []).append(r)
        for items in by_email.values():
            if len(items) <= 1:
                continue
            keep = max(items, key=_score)
            for r in items:
                rid = int(r['id'])
                if rid != int(keep['id']) and rid not in drop_ids:
                    drop_ids.append(rid)

        for rid in drop_ids:
            try:
                cur.execute("DELETE FROM pack_users WHERE id=?", (rid,))
                removed += 1
            except Exception:
                pass
        sqlite_db.connection.commit()

        for ddl in (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_pack_users_pack_username"
            " ON pack_users(industry_pack_id, username)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_pack_users_email"
            " ON pack_users(email) WHERE email<>''",
        ):
            try:
                cur.execute(ddl)
                sqlite_db.connection.commit()
                added.append(ddl.split(' ')[5])
            except Exception:
                try:
                    sqlite_db.connection.rollback()
                except Exception:
                    pass
        cur.close()
    return {"removed": removed, "indexes_added": added}


_pack_uniqueness_done = False


def ensure_pack_user_uniqueness_once() -> None:
    """每个进程只跑一次的唯一性迁移（幂等；失败只记录，不影响登录）。"""
    global _pack_uniqueness_done
    if _pack_uniqueness_done:
        return
    _pack_uniqueness_done = True
    try:
        result = ensure_pack_user_uniqueness()
        if result.get("removed"):
            print(f"[pack-user] 唯一性迁移：清理重复包用户 {result['removed']} 行")
    except Exception as exc:
        print(f"[pack-user] 包用户唯一性迁移失败（忽略）: {exc}")


def delete_pack_users(industry_pack_id: str) -> int:
    """删除指定行业包下的全部包用户（用于清理误建/失效租户用户）。返回删除行数。"""
    _ensure()
    pack = str(industry_pack_id or '').strip()
    if not pack:
        raise ValueError('缺 industry_pack_id')
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM pack_users WHERE industry_pack_id=?", (pack,))
        _r = cur.fetchone()
        before = int((dict(_r) if _r else {}).get('n') or 0)
        cur.execute("DELETE FROM pack_users WHERE industry_pack_id=?", (pack,))
        sqlite_db.connection.commit()
        cur.close()
    return before


# ----------------------------------------------------------------------
# 身份
# ----------------------------------------------------------------------
def current_pack_user_id() -> Optional[int]:
    """当前登录包用户 id；由阶段1登录写入 cookie pack_user_id。无则 None（回退全局）。"""
    try:
        from flask import request, session as _session
        raw = str(_session.get('pack_user_id') or request.cookies.get('pack_user_id') or '').strip()
        return int(raw) if raw else None
    except Exception:
        return None


def current_pack_user() -> Optional[Dict]:
    uid = current_pack_user_id()
    if not uid:
        return None
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        cur.execute("SELECT * FROM pack_users WHERE id=?", (int(uid),))
        _row = cur.fetchone()
        row = dict(_row) if _row else None
        cur.close()
        return row


def active_pack_user() -> Optional[Dict]:
    """返回活跃且未到期的包用户；过期/停用/未登录返回 None（用于主应用门控）。"""
    u = current_pack_user()
    if not u:
        return None
    if str(u.get('status')) != 'active':
        return None
    expire_at = str(u.get('expire_at') or '')
    if expire_at and str(expire_at)[:10] < _now_str()[:10]:
        return None
    return u


def current_pack_id_or_none() -> Optional[str]:
    """当前访问者的授权行业包：包用户返回其 pack_id，否则 None（看调用方策略）。"""
    u = active_pack_user()
    return str(u.get('industry_pack_id')) if u else None


# ----------------------------------------------------------------------
# 多租户品牌（白标）：登录用户自己的公司名 + Logo 覆盖首页品牌
# ----------------------------------------------------------------------
_LOGO_ALLOWED_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
_LOGO_UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "uploads")


def set_user_logo(user_id: int, file_storage) -> str:
    """上传租户用户 Logo：存 static/uploads/pack_logo_{uid}_{ts}{ext}，返回相对 URL。

    文件名带时间戳版本：URL 随每次上传变化，浏览器/微信卡片缓存（nginx 7 天缓存、
    微信 og:image 缓存）都会因为新 URL 重新拉取，不会继续显示旧 Logo。
    """
    if not user_id:
        raise ValueError('用户 ID 无效')
    try:
        ext = ""
        if file_storage and file_storage.filename:
            ext = os.path.splitext(str(file_storage.filename))[1].lower()
        ext = ext if ext in _LOGO_ALLOWED_EXT else ".png"
        os.makedirs(_LOGO_UPLOAD_DIR, exist_ok=True)
        import time as _time
        _ts = str(int(_time.time()))
        filename = f"pack_logo_{int(user_id)}_{_ts}{ext}"
        file_storage.save(os.path.join(_LOGO_UPLOAD_DIR, filename))
        rel = f"/static/uploads/{filename}"
        _update_user(int(user_id), logo_url=rel)
        log_activity(int(user_id), 'logo_update', rel[:200])
        # 清理该用户旧版本 logo 文件（含旧格式 pack_logo_{uid}.{ext}），避免上传目录堆积
        try:
            import glob as _glob
            _candidates = set()
            _candidates.update(_glob.glob(os.path.join(_LOGO_UPLOAD_DIR, f"pack_logo_{int(user_id)}.*")))
            _candidates.update(_glob.glob(os.path.join(_LOGO_UPLOAD_DIR, f"pack_logo_{int(user_id)}_*")))
            for old in _candidates:
                if os.path.basename(old) != filename:
                    try:
                        os.remove(old)
                    except OSError:
                        pass
        except Exception:
            pass
        return rel
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Logo 上传失败: {exc}")


def set_user_brand_flag(user_id: int, enabled: bool) -> Dict:
    """管理员开关：该用户首页是否显示自己的公司名与 Logo（show_user_brand）。"""
    _ensure()
    _update_user(int(user_id), show_user_brand=1 if enabled else 0)
    log_activity(int(user_id), 'brand_flag_update', '1' if enabled else '0')
    return get_profile(int(user_id))


def _find_pack_share_brand(industry_pack_id: str) -> Optional[Dict]:
    """文章分享卡片用的租户品牌：该行业包 show_user_brand=1 且配置了 Logo 的用户。

    取最近更新 Logo/品牌的用户；未配置时返回 None（调用方回退平台全局品牌）。
    """
    if not industry_pack_id:
        return None
    _ensure()
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        cur.execute(
            """
            SELECT company_name, logo_url FROM pack_users
            WHERE industry_pack_id=? AND show_user_brand=1
              AND (TRIM(COALESCE(logo_url,'')) != '' OR TRIM(COALESCE(company_name,'')) != '')
            ORDER BY updated_at DESC, id DESC LIMIT 1
            """,
            (str(industry_pack_id),),
        )
        row = cur.fetchone()
        cur.close()
    if not row:
        return None
    return {"name": str(row["company_name"] or "").strip(), "logo_url": str(row["logo_url"] or "").strip()}


def brand_override() -> Optional[Dict]:
    """当前登录包用户的首页品牌覆盖：{name, logo_url} 或 None。

    仅当行业包用户管理里开启了 show_user_brand 且用户配置了公司名/Logo 时生效；
    管理员/平台用户登录时返回 None（沿用平台全局品牌）。
    """
    u = active_pack_user()
    if not u or not bool(u.get('show_user_brand')):
        return None
    name = str(u.get('company_name') or '').strip()
    logo_url = str(u.get('logo_url') or '').strip()
    if not name and not logo_url:
        return None
    return {"name": name, "logo_url": logo_url}


# ----------------------------------------------------------------------
# 逐用户 LLM 密钥
# ----------------------------------------------------------------------
def get_user_llm_keys(user_id: int) -> Dict[str, str]:
    if not user_id:
        return {}
    _ensure()
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        cur.execute("SELECT model_id, api_key FROM pack_user_llm_keys WHERE pack_user_id=?", (int(user_id),))
        rows = {str(r[0]): _unscramble(str(r[1])) for r in cur.fetchall()}
        cur.close()
    return rows


def set_user_llm_key(user_id: int, model_id: str, api_key: str) -> None:
    if not user_id or not model_id:
        raise ValueError('user_id / model_id 不能为空')
    _ensure()
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        cur.execute(
            "INSERT INTO pack_user_llm_keys(pack_user_id, model_id, api_key, created_at, updated_at) "
            "VALUES (?,?,?,datetime('now'),datetime('now')) "
            "ON CONFLICT(pack_user_id, model_id) DO UPDATE SET api_key=excluded.api_key, updated_at=excluded.updated_at",
            (int(user_id), str(model_id), _scramble(str(api_key or '')), ),
        )
        sqlite_db.connection.commit()
        cur.close()


def clear_user_llm_key(user_id: int, model_id: str) -> None:
    _ensure()
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        cur.execute("DELETE FROM pack_user_llm_keys WHERE pack_user_id=? AND model_id=?", (int(user_id), str(model_id)))
        sqlite_db.connection.commit()
        cur.close()


def get_user_settings(user_id: int) -> Dict:
    """当前包用户的辅助设置：代理 + 本地 LLM 提供商/接入点/模型/超时。"""
    _ensure()
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS pack_user_settings (pack_user_id BIGINT PRIMARY KEY, "
                    "proxy_http TEXT NOT NULL DEFAULT '', llm_provider TEXT NOT NULL DEFAULT '', "
                    "llm_base_url TEXT NOT NULL DEFAULT '', llm_model TEXT NOT NULL DEFAULT '', "
                    "llm_timeout INTEGER NOT NULL DEFAULT 0, "
                    "updated_at TEXT NOT NULL DEFAULT '')")
        _ensure_user_settings_columns(cur)
        cur.execute("SELECT proxy_http, llm_provider, llm_base_url, llm_model, llm_timeout FROM pack_user_settings WHERE pack_user_id=?",
                    (int(user_id),))
        _r = cur.fetchone()
        cur.close()
    if _r:
        return {'proxy_http': str(_r[0]), 'llm_provider': str(_r[1]), 'llm_base_url': str(_r[2]),
                'llm_model': str(_r[3]), 'llm_timeout': int(_r[4] or 0)}
    return {'proxy_http': '', 'llm_provider': '', 'llm_base_url': '', 'llm_model': '', 'llm_timeout': 0}


def _ensure_user_settings_columns(cur) -> None:
    """旧表补列迁移：llm_provider/llm_base_url/llm_model/llm_timeout。"""
    for _col in ("llm_provider TEXT NOT NULL DEFAULT ''", "llm_base_url TEXT NOT NULL DEFAULT ''",
                 "llm_model TEXT NOT NULL DEFAULT ''", "llm_timeout INTEGER NOT NULL DEFAULT 0"):
        try:
            cur.execute(f"ALTER TABLE pack_user_settings ADD COLUMN {_col}")
        except Exception:
            pass


def set_user_settings(user_id: int, *, proxy_http: str = '', llm_provider: str = '', llm_base_url: str = '', llm_model: str = '', llm_timeout: int = 0) -> Dict:
    _ensure()
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS pack_user_settings (pack_user_id BIGINT PRIMARY KEY, "
                    "proxy_http TEXT NOT NULL DEFAULT '', llm_provider TEXT NOT NULL DEFAULT '', "
                    "llm_base_url TEXT NOT NULL DEFAULT '', llm_model TEXT NOT NULL DEFAULT '', "
                    "llm_timeout INTEGER NOT NULL DEFAULT 0, "
                    "updated_at TEXT NOT NULL DEFAULT '')")
        _ensure_user_settings_columns(cur)
        cur.execute(
            "INSERT INTO pack_user_settings(pack_user_id, proxy_http, llm_provider, llm_base_url, llm_model, llm_timeout, updated_at) "
            "VALUES (?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(pack_user_id) DO UPDATE SET proxy_http=excluded.proxy_http, llm_provider=excluded.llm_provider, "
            "llm_base_url=excluded.llm_base_url, llm_model=excluded.llm_model, llm_timeout=excluded.llm_timeout, updated_at=datetime('now')",
            (int(user_id), str(proxy_http or '').strip(), str(llm_provider or '').strip(),
             str(llm_base_url or '').strip(), str(llm_model or '').strip(), int(llm_timeout or 0)),
        )
        sqlite_db.connection.commit(); cur.close()
    return get_user_settings(int(user_id))


def set_user_llm_keys(user_id: int, keys: Dict[str, str]) -> int:
    n = 0
    for mid, key in dict(keys or {}).items():
        if not str(mid).strip():
            continue
        if key is None:
            clear_user_llm_key(user_id, mid)
        else:
            set_user_llm_key(user_id, mid, key)
            n += 1
    return n


# ----------------------------------------------------------------------
# 包用户创建（管理员）——供阶段1/3 复用
# ----------------------------------------------------------------------
def create_pack_user(*, industry_pack_id: str, username: str, password: str, email: str,
                     nickname: str = '', auth_days: int = 30, contact_phone: str = '',
                     translate_enabled: bool = True, ai_assistant_enabled: bool = True,
                     can_delete_articles: bool = True, company_name: str = '', remind_phone: str = '') -> int:
    _ensure()
    pw_hash = hashlib.sha256(f'{username}:{password}'.encode('utf-8')).hexdigest()
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        # 先查重：邮箱/用户名冲突给中文提示，不把数据库英文错误抛给前端
        cur.execute("SELECT id FROM pack_users WHERE email=?", (str(email),))
        if cur.fetchone():
            cur.close()
            raise ValueError('邮箱已注册，请勿重复注册')
        cur.execute(
            "SELECT id FROM pack_users WHERE industry_pack_id=? AND username=?",
            (str(industry_pack_id), str(username)),
        )
        if cur.fetchone():
            cur.close()
            raise ValueError('用户名已存在，请更换用户名')
        cur.execute(
            "INSERT INTO pack_users(industry_pack_id, username, password_hash, email, nickname,"
            "  auth_days, contact_phone, company_name, remind_phone, translate_enabled, ai_assistant_enabled,"
            "  can_delete_articles, must_change_password, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,datetime('now'),datetime('now'))",
            (str(industry_pack_id), str(username), pw_hash, str(email), str(nickname),
             int(auth_days), str(contact_phone), str(company_name), str(remind_phone),
             1 if translate_enabled else 0, 1 if ai_assistant_enabled else 0,
             1 if can_delete_articles else 0),
        )
        new_id = int(cur.lastrowid)
        sqlite_db.connection.commit()
        cur.close()
    return new_id


def list_pack_users(industry_pack_id: str) -> List[Dict]:
    _ensure()
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        cur.execute(
            "SELECT id, industry_pack_id, username, email, nickname, status, init_login_at,"
            "  activation_at, auth_days, expire_at, contact_phone, company_name, remind_phone, translate_enabled,"
            "  ai_assistant_enabled, created_at, logo_url, show_user_brand"
            " FROM pack_users WHERE industry_pack_id=? ORDER BY id",
            (str(industry_pack_id),),
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows


def packs_with_bound_users() -> List[str]:
    """所有“有绑定用户”的行业包。

    多用户场景下采集/调度必须覆盖这些包：否则切换全局活跃包会停掉其它行业
    （其它用户）的资讯采集——他们的用户还在，但采集不再排程。
    只返回仍可加载的包，避免为已删除的包空跑。
    """
    _ensure()
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        try:
            cur.execute(
                "SELECT DISTINCT industry_pack_id FROM pack_users WHERE COALESCE(industry_pack_id,'')<>''"
            )
            pack_ids = [str(r[0]) for r in cur.fetchall()]
        finally:
            cur.close()
    from industry_packs import industry_pack_loader
    usable = []
    for pack_id in pack_ids:
        if not str(pack_id).strip():
            continue
        try:
            industry_pack_loader.load(str(pack_id))
        except Exception:
            continue
        if str(pack_id) not in usable:
            usable.append(str(pack_id))
    return usable


# ----------------------------------------------------------------------
# 包用户登录/登出/当前用户（阶段9：登录后按 pack_users 隔离会话与 LLM 密钥）
# ----------------------------------------------------------------------
def _set_session_user(user_id: Optional[int]) -> None:
    try:
        from flask import session as _session
        if user_id:
            _session['pack_user_id'] = int(user_id)
        else:
            _session.pop('pack_user_id', None)
    except Exception:
        pass


def login_pack_user(username: str, password: str, industry_pack_id: str = '') -> Dict:
    """包用户登录：用户名+口令校验（传入 industry_pack_id 时限定行业包），成功写 session.pack_user_id。"""
    _ensure()
    if not username or not password:
        raise ValueError('用户名/口令不能为空')
    pw_hash = hashlib.sha256(f'{username}:{password}'.encode('utf-8')).hexdigest()
    row = _find_user_row(username, pw_hash, industry_pack_id)
    if not row:
        raise ValueError('用户名或口令错误')
    if str(row.get('status')) != 'active':
        raise ValueError('账号状态非激活，禁止登录')
    # 到期校验：expire_at 早于当前则拒绝
    expire_at = str(row.get('expire_at') or '')
    if expire_at:
        try:
            from utils import get_china_time
            if str(get_china_time())[:10] > expire_at[:10]:
                raise ValueError('授权已到期，需延期授权')
        except Exception:
            pass
    _set_session_user(int(row['id']))
    return row


def _set_init_login_once(user_id: int) -> None:
    """首次登录写入激活时间（已存在则不覆盖，保证不可变）。"""
    row = get_profile(int(user_id))
    if not (row.get('init_login_at') or ''):
        _update_user(int(user_id), init_login_at=_now_str())


def _ensure_expire_at(user_id: int) -> None:
    """若无到期时间，按 注册日期 + 授权天数 计算授权到期时间并写入。"""
    row = get_profile(int(user_id))
    if (row.get('expire_at') or ''):
        return
    auth_days = int(row.get('auth_days') or 30)
    created = str(row.get('created_at') or '')[:10] or _now_str()[:10]
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    try:
        base = _dt.strptime(created, '%Y-%m-%d')
    except Exception:
        base = _dt.now()
    exp = (base + _td(days=auth_days)).strftime('%Y-%m-%d %H:%M:%S')
    _update_user(int(user_id), expire_at=exp)


def logout_pack_user() -> None:
    _set_session_user(None)


# ----------------------------------------------------------------------
# 阶段1：邮箱验证码两步登录 + 首次登录强制改密（字母+数字）
# ----------------------------------------------------------------------
import re as _re
import secrets as _secrets
from datetime import timedelta as _td


def _now_str() -> str:
    try:
        from utils import get_china_time
        return get_china_time().strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        from datetime import datetime
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# 邮箱验证码有效期（分钟）。邮件投递 + 用户切设备/手输通常要几分钟，
# 5 分钟过短，容易出现“收到邮件但已过期/提示验证码错误”的失败。
EMAIL_VERIFY_TTL_MINUTES = 15


def _random_code() -> str:
    return ''.join(_secrets.choice('123456789') for _ in range(6))


def _find_user_row(username, password_hash=None, industry_pack_id='', prefer_pending_code=False):
    """按用户名查找用户行（可选按行业包限定），结果确定性。

    同名用户可以合法存在于不同行业包（UNIQUE 是 (industry_pack_id, username)），
    因此必须避免“同名多行时写在一行、读在另一行”：
      · prefer_pending_code=True（校验验证码阶段）优先取仍持有 email_verify_code 的行，
        即发码时写入的那一行；
      · 其余情况按 id 倒序（最新创建优先）。
    传入 industry_pack_id 时进一步限定到该行业包。
    """
    sql = "SELECT * FROM pack_users WHERE username=?"
    params = [str(username)]
    pack = str(industry_pack_id or '').strip()
    if pack:
        sql += " AND industry_pack_id=?"
        params.append(pack)
    if password_hash:
        sql += " AND password_hash=?"
        params.append(password_hash)
    if prefer_pending_code:
        sql += " ORDER BY (CASE WHEN email_verify_code<>'' THEN 0 ELSE 1 END), id DESC"
    else:
        sql += " ORDER BY id DESC"
    sql += " LIMIT 1"
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        cur.execute(sql, tuple(params))
        _r = cur.fetchone()
        row = dict(_r) if _r else None
        cur.close()
    return row


def _update_user(user_id, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [_now_str(), int(user_id)]
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        cur.execute(f"UPDATE pack_users SET {sets}, updated_at=? WHERE id=?", vals)
        sqlite_db.connection.commit()
        cur.close()


def _ensure_authorized(row) -> None:
    if str(row.get('status')) != 'active':
        raise ValueError('账号状态非激活，禁止登录')
    expire_at = str(row.get('expire_at') or '')
    if expire_at and str(expire_at)[:10] < _now_str()[:10]:
        raise ValueError('授权已到期，需延期授权')


def send_email_verify_code(email: str, code: str) -> bool:
    """发送邮箱验证码。已配置 SMTP 则发送并返回 True；否则打在日志并返回 False（测试/未配置邮件）。
    私有邮箱（QQ/163/Gmail/Outlook 等）均可：465 隐式SSL，587 STARTTLS。"""
    try:
        import config
        host = str(getattr(config, 'SMTP_HOST', '') or '').strip()
        if host:
            from email.mime.text import MIMEText
            from email.utils import formataddr
            msg = MIMEText(
                f'您的登录验证码是 {code}，{EMAIL_VERIFY_TTL_MINUTES} 分钟内有效。\n\n'
                '提示：若您收到多封验证码邮件，里面的验证码相同，使用任意一封均可。\n'
                '若这不是您本人操作，请忽略本邮件。',
                'plain', 'utf-8',
            )
            msg['Subject'] = '灵蹊智能 - 账号登录验证码'
            msg['From'] = formataddr(('灵蹊智能', str(getattr(config, 'SMTP_USER', '') or host)))
            msg['To'] = email
            port = int(getattr(config, 'SMTP_PORT', 465) or 465)
            user = str(getattr(config, 'SMTP_USER', '') or '')
            pwd = str(getattr(config, 'SMTP_PASS', '') or '')
            if port == 465:
                from smtplib import SMTP_SSL
                with SMTP_SSL(host, port, timeout=20) as s:
                    if user and pwd:
                        s.login(user, pwd)
                    s.send_message(msg)
            else:
                from smtplib import SMTP
                with SMTP(host, port, timeout=20) as s:
                    s.starttls()
                    if user and pwd:
                        s.login(user, pwd)
                    s.send_message(msg)
            print(f"[pack-user] 邮箱验证码已发送 -> {email}")
            return True
        print(f"[pack-user] 邮箱验证码 -> {email}: {code}")
        return False
    except Exception as exc:
        print(f"[pack-user] 验证码发送失败: {exc}")
        return False


def begin_login(username: str, password: str, email: str = '', industry_pack_id: str = '') -> Dict:
    """登录：校验口令。已激活用户直接写 session 登录；未激活用户需绑定邮箱→发验证码。
    若传入 email 则绑定到该用户（邮箱验证绑定）。
    industry_pack_id：多租户登录页所属行业包，传入后按该包限定查找，避免跨包同名误命中。"""
    _ensure()
    ensure_pack_user_uniqueness_once()
    if not username or not password:
        raise ValueError('用户名/口令不能为空')
    pw_hash = hashlib.sha256(f'{username}:{password}'.encode('utf-8')).hexdigest()
    row = _find_user_row(username, pw_hash, industry_pack_id)
    if not row:
        raise ValueError('用户名或口令错误')
    _row_pack = str(row.get('industry_pack_id') or '')
    _ensure_authorized(row)
    # 已激活：直接登录，无需再发验证码
    if int(row.get('activated') or 0) == 1:
        _set_session_user(int(row['id']))
        _update_user(int(row['id']), last_login_at=_now_str())
        return {'user_id': int(row['id']), 'username': str(row.get('username')), 'email': str(row.get('email')),
                'industry_pack_id': _row_pack,
                'activated': True, 'step': 'done', 'verification_required': False}
    # 未激活：需绑定邮箱。未提供合法邮箱则不发送（仅提示需绑定）；提供则绑定并发送验证码。
    if not (email and _re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$', str(email))):
        return {'user_id': int(row['id']), 'username': str(row.get('username')), 'email': str(row.get('email') or ''),
                'industry_pack_id': _row_pack,
                'activated': False, 'step': 'email_verify', 'need_email': True,
                'must_change_password': bool(row.get('must_change_password')), 'verification_required': True,
                'email_sent': False, 'dev_code': None, 'message': '请绑定邮箱'}
    mail_to = str(email).strip()
    # 邮箱已被其他用户占用时给中文提示，避免把数据库英文错误抛给前端
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        cur.execute("SELECT id FROM pack_users WHERE email=? AND id<>?", (mail_to, int(row['id'])))
        if cur.fetchone():
            cur.close()
            raise ValueError('邮箱已注册，请勿重复注册')
        cur.close()
    _update_user(int(row['id']), email=mail_to)
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    _now19 = _now_str()[:19]
    # 重发时若上一枚验证码仍在有效期内 → 复用同一枚。
    # 这样新旧邮件里的码完全一致，用户拿哪一封都能登录，
    # 从根上消除"用了重发前那封邮件里的码 → 验证码错误"这一失败模式。
    _prev_code = str(row.get('email_verify_code') or '')
    _prev_exp = str(row.get('email_verify_expires') or '')[:19]
    code = _prev_code if (_prev_code and _prev_exp >= _now19) else _random_code()
    exp = (_dt.strptime(_now19, '%Y-%m-%d %H:%M:%S') + _td(minutes=EMAIL_VERIFY_TTL_MINUTES)).strftime('%Y-%m-%d %H:%M:%S')
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        cur.execute("UPDATE pack_users SET email_verify_code=?, email_verify_expires=?, updated_at=? WHERE id=?",
                    (code, exp, _now_str(), int(row['id'])))
        sqlite_db.connection.commit(); cur.close()
    _sent = send_email_verify_code(mail_to, code)
    return {'user_id': int(row['id']), 'username': str(row.get('username')), 'email': mail_to,
            'industry_pack_id': _row_pack,
            'activated': False, 'step': 'email_verify', 'need_email': True,
            'must_change_password': bool(row.get('must_change_password')), 'verification_required': True,
            'email_sent': _sent, 'dev_code': code if not _sent else None}  # 未配置SMTP时把验证码给前端显示，生产应配置SMTP


def verify_email_code(username: str, code: str, industry_pack_id: str = '') -> Dict:
    """登录第二步：校验邮箱验证码；首次登录则返回需强制改密，否则写 session 完成登录。
    industry_pack_id 与 begin_login 保持一致；查询优先取仍持有验证码的那一行，
    保证读到的是发码时写入的行（同名多行也不会串行）。"""
    _ensure()
    row = _find_user_row(username, None, industry_pack_id, prefer_pending_code=True)
    if not row:
        raise ValueError('用户不存在')
    stored = str(row.get('email_verify_code') or '')
    if not stored:
        raise ValueError('请先点击「发送验证码」获取验证码，再输入并登录')
    if stored != str(code):
        raise ValueError('验证码错误：请核对邮件里的 6 位数字（重复发送不会更换验证码）')
    if str(row.get('email_verify_expires') or '')[:19] and str(row.get('email_verify_expires'))[:19] < _now_str()[:19]:
        raise ValueError(f'验证码已过期（有效期 {EMAIL_VERIFY_TTL_MINUTES} 分钟），请重新点击「发送验证码」')
    _update_user(int(row['id']), email_verify_code='', email_verify_expires='', activated=1)
    _ensure_expire_at(int(row['id']))  # 首次激活：按注册日期+授权天数写授权到期时间
    if bool(row.get('must_change_password')):
        return {'step': 'change_password', 'user_id': int(row['id']), 'username': str(row.get('username'))}
    _set_session_user(int(row['id']))
    _set_init_login_once(int(row['id']))
    _update_user(int(row['id']), last_login_at=_now_str())
    log_activity(int(row['id']), 'login', '登录成功')
    return {'step': 'done', 'user_id': int(row['id']), 'username': str(row.get('username'))}


def complete_password_change(username: str, old_password: str, new_password: str, industry_pack_id: str = '') -> Dict:
    """首次登录强制改密：校验原口令，新口令需字母+数字组合，改密后写 session 完成登录。"""
    _ensure()
    row = _find_user_row(username, None, industry_pack_id, prefer_pending_code=False)
    if not row:
        raise ValueError('用户不存在')
    if hashlib.sha256(f'{username}:{old_password}'.encode('utf-8')).hexdigest() != str(row.get('password_hash')):
        raise ValueError('原口令错误')
    if not new_password or len(new_password) < 6:
        raise ValueError('新口令至少6位')
    if not _re.search(r'[A-Za-z]', new_password) or not _re.search(r'\d', new_password):
        raise ValueError('新口令需包含字母和数字组合')
    _update_user(int(row['id']),
                 password_hash=hashlib.sha256(f'{username}:{new_password}'.encode('utf-8')).hexdigest(),
                 must_change_password=0, last_login_at=_now_str())
    _set_session_user(int(row['id']))
    _set_init_login_once(int(row['id']))
    log_activity(int(row['id']), 'login_after_first_change', '首次改密后登录')
    return {'step': 'done', 'user_id': int(row['id']), 'username': str(row.get('username'))}


# ----------------------------------------------------------------------
# 重置口令 / 找回口令（邮箱发信）
# ----------------------------------------------------------------------
def _send_email(to: str, subject: str, body: str) -> bool:
    """通用发信：已配置 SMTP 则发送并返回 True，否则打日志返回 False。465=SSL / 587=STARTTLS。"""
    try:
        import config
        host = str(getattr(config, 'SMTP_HOST', '') or '').strip()
        if not host:
            print(f"[pack-user] (未配置SMTP，模拟发信) {to} <- {subject}: {body[:80]}")
            return False
        from email.mime.text import MIMEText
        from email.utils import formataddr
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = subject
        msg['From'] = formataddr(('灵蹊智能', str(getattr(config, 'SMTP_USER', '') or host)))
        msg['To'] = to
        port = int(getattr(config, 'SMTP_PORT', 465) or 465)
        user = str(getattr(config, 'SMTP_USER', '') or '')
        pwd = str(getattr(config, 'SMTP_PASS', '') or '')
        if port == 465:
            from smtplib import SMTP_SSL
            with SMTP_SSL(host, port, timeout=20) as s:
                if user and pwd:
                    s.login(user, pwd)
                s.send_message(msg)
        else:
            from smtplib import SMTP
            with SMTP(host, port, timeout=20) as s:
                s.starttls()
                if user and pwd:
                    s.login(user, pwd)
                s.send_message(msg)
        print(f"[pack-user] 已发送邮件 -> {to} ({subject})")
        return True
    except Exception as exc:
        print(f"[pack-user] 发信失败: {exc}")
        return False


def _random_token() -> str:
    return _secrets.token_urlsafe(24)


def admin_reset_password(user_id: int, new_password: str = None, send_email: bool = True) -> Dict:
    """管理员重置口令：指定新口令，否则生成随机口令；可选发到用户邮箱。"""
    _ensure()
    row = get_profile(int(user_id))
    un = str(row.get('username') or '')
    if not new_password:
        new_password = ''.join(_secrets.choice('ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789') for _ in range(10))
    if len(new_password) < 6 or not _re.search(r'[A-Za-z]', new_password) or not _re.search(r'\d', new_password):
        raise ValueError('口令需字母+数字且至少6位')
    _update_user(int(user_id), password_hash=hashlib.sha256(f'{un}:{new_password}'.encode('utf-8')).hexdigest(),
                 must_change_password=0, reset_token='', reset_expires='')
    log_activity(int(user_id), 'admin_reset_password', '管理员重置口令')
    sent = False
    if send_email and str(row.get('email') or ''):
        sent = _send_email(str(row.get('email')), '情报系统口令重置',
                           f'您的新登录口令是 {new_password}，请立即登录并尽快修改。')
    return {'user_id': int(user_id), 'username': un, 'new_password': new_password, 'email_sent': sent}


def request_password_reset(email: str) -> Dict:
    """找回口令：按邮箱生成重置 Token 并发重置链接邮件。"""
    _ensure()
    if not email:
        raise ValueError('邮箱不能为空')
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        cur.execute("SELECT id, username FROM pack_users WHERE email=?", (str(email),))
        _r = cur.fetchone(); row = dict(_r) if _r else None; cur.close()
    if not row:
        raise ValueError('该邮箱未绑定任何用户')
    token = _random_token()
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    exp = (_dt.strptime(_now_str()[:19], '%Y-%m-%d %H:%M:%S') + _td(minutes=30)).strftime('%Y-%m-%d %H:%M:%S')
    _update_user(int(row['id']), reset_token=token, reset_expires=exp)
    link = f'{_base_url()}/api/pack-users/reset?token={token}'
    sent = _send_email(str(email), '找回口令',
                       f'您正在找回口令，请点击以下链接重置（30分钟内有效）：\n{link}\n若不是您本人操作请忽略。')
    return {'sent': sent, 'email': str(email)}


def reset_password_by_token(token: str, new_password: str) -> Dict:
    """找回口令：校验重置 Token 并设置新口令。"""
    _ensure()
    if not token or not new_password:
        raise ValueError('参数缺失')
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        cur.execute("SELECT * FROM pack_users WHERE reset_token=?", (str(token),))
        _r = cur.fetchone(); row = dict(_r) if _r else None; cur.close()
    if not row:
        raise ValueError('链接无效或已失效')
    if str(row.get('reset_expires') or '')[:19] and str(row.get('reset_expires'))[:19] < _now_str()[:19]:
        raise ValueError('链接已过期')
    if len(new_password) < 6 or not _re.search(r'[A-Za-z]', new_password) or not _re.search(r'\d', new_password):
        raise ValueError('口令需字母+数字且至少6位')
    un = str(row.get('username') or '')
    _update_user(int(row['id']), password_hash=hashlib.sha256(f'{un}:{new_password}'.encode('utf-8')).hexdigest(),
                 reset_token='', reset_expires='', must_change_password=0)
    log_activity(int(row['id']), 'reset_password_by_token', '找回口令重置成功')
    return {'user_id': int(row['id']), 'username': un}


def _base_url() -> str:
    try:
        from flask import request
        if request and request.host_url:
            return request.host_url.rstrip('/')
    except Exception:
        pass
    try:
        import config
        return f"http://127.0.0.1:{int(getattr(config, 'FLASK_PORT', 8003) or 8003)}"
    except Exception:
        return 'http://127.0.0.1:8003'


def me_pack_user() -> Optional[Dict]:
    uid = current_pack_user_id()
    if not uid:
        return None
    u = current_pack_user()
    if not u:
        return None
    try:
        _ensure_expire_at(int(uid))  # 展示前确保授权到期时间已计算
        _set_init_login_once(int(uid))  # 展示前确保首次登录激活时间已写
    except Exception:
        pass
    u = current_pack_user()
    return {
        'id': u.get('id'), 'username': u.get('username'), 'nickname': u.get('nickname'),
        'email': u.get('email'), 'avatar': u.get('avatar'), 'status': u.get('status'),
        'industry_pack_id': u.get('industry_pack_id'), 'pack_name': _pack_display_name(u.get('industry_pack_id')), 'expire_at': u.get('expire_at'),
        'company_name': u.get('company_name'), 'nickname': u.get('nickname'),
        'remind_phone': u.get('remind_phone'), 'contact_phone': u.get('contact_phone'),
        'translate_enabled': bool(u.get('translate_enabled')), 'ai_assistant_enabled': bool(u.get('ai_assistant_enabled')),
        'can_delete_articles': bool(u.get('can_delete_articles')),
        'logo_url': u.get('logo_url') or '', 'show_user_brand': bool(u.get('show_user_brand')),
    }


# ----------------------------------------------------------------------
# 阶段2：用户中心（资料读写 / 头像64x64裁切 / 活动日志；邮箱不可改）
# ----------------------------------------------------------------------
def _ensure_activity_table(cursor) -> None:
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS user_activity_logs ("
        "  id BIGSERIAL PRIMARY KEY,"
        "  pack_user_id BIGINT NOT NULL,"
        "  action TEXT NOT NULL,"
        "  detail TEXT NOT NULL DEFAULT '',"
        "  created_at TEXT NOT NULL DEFAULT ''"
        ")"
    )


def log_activity(user_id: int, action: str, detail: str = '') -> None:
    if not user_id:
        return
    _ensure()
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        _ensure_activity_table(cur)
        cur.execute("INSERT INTO user_activity_logs(pack_user_id, action, detail, created_at) VALUES (?,?,?,?)",
                    (int(user_id), str(action), str(detail)[:2000], _now_str()))
        sqlite_db.connection.commit(); cur.close()


def get_profile(user_id: int) -> Dict:
    _ensure()
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        cur.execute("SELECT id, username, email, nickname, avatar, status, init_login_at, activation_at,"
                    "  auth_days, expire_at, contact_phone, company_name, remind_phone, translate_enabled,"
                    "  ai_assistant_enabled, can_delete_articles, created_at, logo_url, show_user_brand"
                    " FROM pack_users WHERE id=?", (int(user_id),))
        _r = cur.fetchone(); row = dict(_r) if _r else None
        cur.close()
    if not row:
        raise ValueError('用户不存在')
    return row


def update_profile(user_id: int, *, nickname: str = '', new_password: str = '', company_name: str = '') -> Dict:
    _ensure()
    if not nickname and not new_password and not company_name:
        raise ValueError('无可更新内容')
    if new_password:
        if len(new_password) < 6 or not _re.search(r'[A-Za-z]', new_password) or not _re.search(r'\d', new_password):
            raise ValueError('口令需字母+数字且至少6位')
        # 按 id 取 username 以算 hash
        with sqlite_db.lock:
            cur = sqlite_db.connection.cursor()
            cur.execute("SELECT username FROM pack_users WHERE id=?", (int(user_id),))
            _u = cur.fetchone(); username = _u[0] if _u else ''
            cur.close()
        _update_user(int(user_id), password_hash=hashlib.sha256(f'{username}:{new_password}'.encode('utf-8')).hexdigest())
        log_activity(int(user_id), 'change_password', '修改口令')
    if nickname:
        _update_user(int(user_id), nickname=str(nickname).strip())
        log_activity(int(user_id), 'update_profile', f'昵称 -> {nickname}')
    if company_name:
        _update_user(int(user_id), company_name=str(company_name).strip())
        log_activity(int(user_id), 'update_profile', f'公司名称 -> {company_name}')
    return get_profile(int(user_id))


def set_avatar(user_id: int, file_storage) -> str:
    """上传头像：校验图片类型/大小，居中裁切并缩放为 64x64（小图放大/大图缩小），保存到用户头像目录。"""
    _ensure()
    try:
        from PIL import Image, ImageOps
    except Exception as exc:
        raise ValueError(f'头像处理依赖缺失: {exc}')
    if not file_storage or not file_storage.filename:
        raise ValueError('未选择头像文件')
    data = file_storage.read()
    if len(data) > 5 * 1024 * 1024:
        raise ValueError('头像超过5MB限制')
    import io as _io
    try:
        img = Image.open(_io.BytesIO(data))
    except Exception:
        raise ValueError('不是有效图片')
    img = ImageOps.exif_transpose(img).convert('RGB')
    w, h = img.size
    # 居中裁切为正方形，再缩放到64
    side = min(w, h)
    left = (w - side) // 2; top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((64, 64), Image.LANCZOS)
    av_dir = os.path.join('data', 'user_avatars')
    os.makedirs(av_dir, exist_ok=True)
    path = os.path.join(av_dir, f'u{user_id}_{int(_now_str().replace(":", "").replace("-", "").replace(" ", ""))}.png')
    img.save(path, 'PNG')
    rel = 'data/user_avatars/' + os.path.basename(path)
    _update_user(int(user_id), avatar=rel)
    log_activity(int(user_id), 'update_avatar', '更换头像')
    return rel


# ----------------------------------------------------------------------
# 阶段3：行业包用户管理（授权配置 / 用户状态时长 / 延期 / 收款二维码）
# ----------------------------------------------------------------------
def _ensure_auth_config_table(cursor) -> None:
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS pack_authorization_config ("
        "  id BIGSERIAL PRIMARY KEY,"
        "  industry_pack_id TEXT NOT NULL UNIQUE,"
        "  default_auth_days INTEGER NOT NULL DEFAULT 30,"
        "  renewal_days INTEGER NOT NULL DEFAULT 30,"
        "  renewal_amount TEXT NOT NULL DEFAULT '',"
        "  contact_phone TEXT NOT NULL DEFAULT '',"
        "  qr_wechat TEXT NOT NULL DEFAULT '',"
        "  qr_alipay TEXT NOT NULL DEFAULT '',"
        "  created_at TEXT NOT NULL DEFAULT '',"
        "  updated_at TEXT NOT NULL DEFAULT ''"
        ")"
    )


def delete_pack_user(user_id: int) -> None:
    """删除包用户及其关联数据（LLM密钥/活动日志/支付记录）。"""
    _ensure()
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        cur.execute("DELETE FROM pack_user_llm_keys WHERE pack_user_id=?", (int(user_id),))
        cur.execute("DELETE FROM user_activity_logs WHERE pack_user_id=?", (int(user_id),))
        cur.execute("DELETE FROM payments WHERE user_id=?", (int(user_id),))
        cur.execute("DELETE FROM pack_users WHERE id=?", (int(user_id),))
        sqlite_db.connection.commit(); cur.close()


def get_auth_config(pack_id: str) -> Dict:
    _ensure()
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        _ensure_auth_config_table(cur)
        cur.execute("SELECT * FROM pack_authorization_config WHERE industry_pack_id=?", (str(pack_id),))
        _r = cur.fetchone(); row = dict(_r) if _r else {}
        cur.close()
    if not row:
        return {'industry_pack_id': str(pack_id), 'default_auth_days': 30, 'renewal_days': 30,
                'renewal_amount': '', 'contact_phone': '', 'qr_wechat': '', 'qr_alipay': ''}
    return row


def set_auth_config(pack_id: str, *, default_auth_days=None, renewal_days=None, renewal_amount='',
                    contact_phone='', qr_wechat='', qr_alipay='') -> Dict:
    _ensure()
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        _ensure_auth_config_table(cur)
        cur.execute(
            "INSERT INTO pack_authorization_config(industry_pack_id, default_auth_days, renewal_days,"
            "  renewal_amount, contact_phone, qr_wechat, qr_alipay, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,datetime('now'),datetime('now'))"
            " ON CONFLICT(industry_pack_id) DO UPDATE SET"
            " default_auth_days=COALESCE(excluded.default_auth_days, pack_authorization_config.default_auth_days),"
            " renewal_days=COALESCE(excluded.renewal_days, pack_authorization_config.renewal_days),"
            " renewal_amount=excluded.renewal_amount, contact_phone=excluded.contact_phone,"
            " qr_wechat=excluded.qr_wechat, qr_alipay=excluded.qr_alipay, updated_at=datetime('now')",
            (str(pack_id), int(default_auth_days or 30), int(renewal_days or 30),
             str(renewal_amount or ''), str(contact_phone or ''), str(qr_wechat or ''), str(qr_alipay or '')),
        )
        sqlite_db.connection.commit(); cur.close()
    return get_auth_config(str(pack_id))


def update_pack_user(user_id: int, *, status: str = '', auth_days: int = 0, expire_at: str = '',
                     contact_phone: str = '', translate_enabled: bool | None = None,
                     ai_assistant_enabled: bool | None = None,
                     can_delete_articles: bool | None = None,
                     company_name: str = '', remind_phone: str = '', email: str = '',
                     show_user_brand: bool | None = None) -> Dict:
    """管理员管理用户：状态(active/disabled/expired)/授权时长/到期时间/联系人/邮箱/功能开关/删除权限/公司名/到期提醒手机号。"""
    _ensure()
    fields = {}
    if status in ('active', 'disabled', 'expired'):
        fields['status'] = str(status)
    if auth_days:
        fields['auth_days'] = int(auth_days)
    if expire_at:
        fields['expire_at'] = str(expire_at)[:19]
    if contact_phone:
        fields['contact_phone'] = str(contact_phone).strip()
    if company_name:
        fields['company_name'] = str(company_name).strip()
    if remind_phone is not None:
        fields['remind_phone'] = str(remind_phone).strip()
    if email:
        fields['email'] = str(email).strip()
    # 邮箱改绑前查重：被其他用户占用给中文提示
    if fields.get('email'):
        with sqlite_db.lock:
            cur = sqlite_db.connection.cursor()
            cur.execute(
                "SELECT id FROM pack_users WHERE email=? AND id<>?",
                (str(fields['email']), int(user_id)),
            )
            if cur.fetchone():
                cur.close()
                raise ValueError('邮箱已注册，请勿重复注册')
            cur.close()
    if translate_enabled is not None:
        fields['translate_enabled'] = 1 if translate_enabled else 0
    if ai_assistant_enabled is not None:
        fields['ai_assistant_enabled'] = 1 if ai_assistant_enabled else 0
    if can_delete_articles is not None:
        fields['can_delete_articles'] = 1 if can_delete_articles else 0
    if show_user_brand is not None:
        fields['show_user_brand'] = 1 if show_user_brand else 0
    if fields:
        _update_user(int(user_id), **fields)
        log_activity(int(user_id), 'admin_update', json.dumps(fields, ensure_ascii=False)[:500])
    return get_profile(int(user_id))


def extend_pack_user(user_id: int, *, extra_days: int, invoice_no: str, amount: str) -> Dict:
    """延期授权：校验发票号+金额，按当前到期时间（或今天）顺延 extra_days 天，写入日志。"""
    _ensure()
    if not invoice_no:
        raise ValueError('延期必须提供发票号')
    if not amount or float(amount) <= 0:
        raise ValueError('延期必须填写付费金额')
    if extra_days <= 0:
        raise ValueError('延期天数必须 >0')
    from datetime import datetime as _dt
    row = get_profile(int(user_id))
    base = row.get('expire_at') or _now_str()
    try:
        base_dt = _dt.strptime(str(base)[:10], '%Y-%m-%d')
    except Exception:
        base_dt = _dt.now()
    new_expire = base_dt.replace(hour=23, minute=59, second=59)
    new_days = extra_days
    from datetime import timedelta as _t
    new_expire = new_expire + _t(days=int(new_days))
    _update_user(int(user_id), expire_at=new_expire.strftime('%Y-%m-%d %H:%M:%S'), status='active')
    log_activity(int(user_id), 'renewal', f'延期{new_days}天 发票:{invoice_no} 金额:{amount}')
    return {'user_id': int(user_id), 'new_expire_at': new_expire.strftime('%Y-%m-%d %H:%M:%S'),
            'invoice_no': str(invoice_no), 'amount': str(amount)}


# ----------------------------------------------------------------------
# 阶段4：到期续费（微信/支付宝二维码支付）
# ----------------------------------------------------------------------
def _ensure_payments_table(cursor) -> None:
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS payments ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  user_id BIGINT NOT NULL,"
        "  pack_id TEXT NOT NULL,"
        "  amount TEXT NOT NULL DEFAULT '',"
        "  method TEXT NOT NULL DEFAULT 'wechat'"
        "    CHECK (method IN ('wechat', 'alipay')),"
        "  qr TEXT NOT NULL DEFAULT '',"
        "  invoice_no TEXT NOT NULL DEFAULT '',"
        "  status TEXT NOT NULL DEFAULT 'pending'"
        "    CHECK (status IN ('pending', 'paid', 'cancelled')),"
        "  new_expire_at TEXT NOT NULL DEFAULT '',"
        "  paid_at TEXT NOT NULL DEFAULT '',"
        "  created_at TEXT NOT NULL DEFAULT ''"
        ")"
    )


def get_my_expiry_info(user_id: int, pack_id: str) -> Dict:
    _ensure()
    row = get_profile(int(user_id))
    cfg = get_auth_config(pack_id)
    return {
        'user_id': int(user_id), 'username': row.get('username'), 'industry_pack_id': pack_id,
        'expire_at': row.get('expire_at'), 'status': row.get('status'),
        'contact_phone': cfg.get('contact_phone'), 'renewal_amount': cfg.get('renewal_amount'),
        'renewal_days': cfg.get('renewal_days'), 'qr_wechat': cfg.get('qr_wechat'), 'qr_alipay': cfg.get('qr_alipay'),
    }


def initiate_payment(user_id: int, pack_id: str, method: str = 'wechat') -> Dict:
    _ensure()
    if method not in ('wechat', 'alipay'):
        raise ValueError('支付方式只支持微信或支付宝')
    cfg = get_auth_config(pack_id)
    amount = str(cfg.get('renewal_amount') or '')
    if not amount or float(amount) <= 0:
        raise ValueError('尚未设置续费金额')
    qr = str(cfg.get('qr_wechat') if method == 'wechat' else cfg.get('qr_alipay') or '')
    if not qr:
        raise ValueError('该支付方式二维码未配置')
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        _ensure_payments_table(cur)
        cur.execute(
            "INSERT INTO payments(user_id, pack_id, amount, method, qr, status, created_at)"
            " VALUES (?,?,?,?,?,'pending',?)",
            (int(user_id), str(pack_id), amount, str(method), str(qr), _now_str()),
        )
        pid = int(cur.lastrowid)
        sqlite_db.connection.commit(); cur.close()
    log_activity(int(user_id), 'payment_initiate', f'{method} {amount}元')
    return {'payment_id': pid, 'method': method, 'amount': amount, 'qr': qr,
            'account': row_username(user_id), 'industry_pack_id': pack_id}


def confirm_payment(user_id: int, payment_id: int) -> Dict:
    _ensure()
    from datetime import datetime as _dt
    from datetime import timedelta as _t
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        _ensure_payments_table(cur)
        cur.execute("SELECT * FROM payments WHERE id=? AND user_id=?", (int(payment_id), int(user_id)))
        _r = cur.fetchone(); pay = dict(_r) if _r else None
        cur.close()
    if not pay:
        raise ValueError('支付记录不存在')
    if pay.get('status') != 'pending':
        raise ValueError('该支付已处理')
    row = get_profile(int(user_id))
    base = row.get('expire_at') or _now_str()
    try:
        base_dt = _dt.strptime(str(base)[:10], '%Y-%m-%d')
    except Exception:
        base_dt = _dt.now()
    days = int(get_auth_config(pay.get('pack_id') or '').get('renewal_days') or 30)
    new_expire = (base_dt.replace(hour=23, minute=59, second=59) + _t(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        cur.execute("UPDATE payments SET status='paid', paid_at=?, new_expire_at=? WHERE id=?",
                    (_now_str(), new_expire, int(payment_id)))
        sqlite_db.connection.commit(); cur.close()
    _update_user(int(user_id), expire_at=new_expire, status='active')
    log_activity(int(user_id), 'payment_paid', f'支付{pay.get("amount")}元，延期至{new_expire}')
    return {'payment_id': int(payment_id), 'amount': pay.get('amount'), 'method': pay.get('method'),
            'new_expire_at': new_expire, 'account': row_username(int(user_id)),
            'industry_pack_id': pay.get('pack_id')}


def row_username(user_id: int) -> str:
    try:
        r = get_profile(int(user_id))
        return str(r.get('username') or '')
    except Exception:
        return ''


# ----------------------------------------------------------------------
# 阶段6：翻译策略（行业包级 translation_strategy: pre/realtime/none）
# 供前端按"包策略 × 用户 translate_enabled"决定是否显示翻译按钮、是否读缓存译文。
# ----------------------------------------------------------------------
def _ensure_pack_intel_settings_table(cursor) -> None:
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS pack_intel_settings ("
        "  id BIGSERIAL PRIMARY KEY,"
        "  industry_pack_id TEXT NOT NULL,"
        "  setting_key TEXT NOT NULL,"
        "  setting_value TEXT NOT NULL DEFAULT '',"
        "  updated_at TEXT NOT NULL DEFAULT '',"
        "  UNIQUE(industry_pack_id, setting_key)"
        ")"
    )


def get_pack_setting(pack_id: str, key: str, default: str = '') -> str:
    _ensure()
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        _ensure_pack_intel_settings_table(cur)
        cur.execute("SELECT setting_value FROM pack_intel_settings WHERE industry_pack_id=? AND setting_key=?",
                    (str(pack_id), str(key)))
        _r = cur.fetchone()
        cur.close()
        return str(_r[0]) if _r else default


def set_pack_setting(pack_id: str, key: str, value: str) -> None:
    _ensure()
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        _ensure_pack_intel_settings_table(cur)
        cur.execute(
            "INSERT INTO pack_intel_settings(industry_pack_id, setting_key, setting_value, updated_at)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(industry_pack_id, setting_key) DO UPDATE SET setting_value=excluded.setting_value, updated_at=excluded.updated_at",
            (str(pack_id), str(key), str(value), _now_str()),
        )
        sqlite_db.connection.commit(); cur.close()


def get_translation_strategy(pack_id: str) -> str:
    v = get_pack_setting(pack_id, 'translation_strategy', 'realtime')
    return v if v in ('pre', 'realtime', 'none') else 'realtime'


# ----------------------------------------------------------------------
# 阶段A：分布式服务设置（per-pack 服务器IP/SSH/LLM/TTS） + capabilities 探查 + 登录生效
# ----------------------------------------------------------------------
def _ensure_remote_config_table(cursor) -> None:
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS pack_remote_config ("
        "  id BIGSERIAL PRIMARY KEY,"
        "  industry_pack_id TEXT NOT NULL UNIQUE,"
        "  server_ip TEXT NOT NULL DEFAULT '',"
        "  server_port INTEGER NOT NULL DEFAULT 11236,"
        "  ssh_host TEXT NOT NULL DEFAULT '',"
        "  ssh_port INTEGER NOT NULL DEFAULT 22,"
        "  ssh_user TEXT NOT NULL DEFAULT '',"
        "  ssh_password TEXT NOT NULL DEFAULT '',"
        "  llm_base_url TEXT NOT NULL DEFAULT '',"
        "  llm_model TEXT NOT NULL DEFAULT '',"
        "  tts_voice TEXT NOT NULL DEFAULT '',"
        "  tts_gender TEXT NOT NULL DEFAULT 'female'"
        "    CHECK (tts_gender IN ('male','female')),"
        "  tts_dialect TEXT NOT NULL DEFAULT 'mandarin'"
        "    CHECK (tts_dialect IN ('mandarin','cantonese')),"
        "  db_url TEXT NOT NULL DEFAULT '',"
        "  db_name TEXT NOT NULL DEFAULT '',"
        "  settings_json TEXT NOT NULL DEFAULT '{}',"
        "  enabled INTEGER NOT NULL DEFAULT 1,"
        "  updated_at TEXT NOT NULL DEFAULT ''"
        ")"
    )
    try:
        # 兼容旧表：补充缺失列（db_url/db_name/settings_json）
        for _col in ("db_url TEXT NOT NULL DEFAULT ''", "db_name TEXT NOT NULL DEFAULT ''",
                     "settings_json TEXT NOT NULL DEFAULT '{}'"):
            try:
                cursor.execute(f"ALTER TABLE pack_remote_config ADD COLUMN {_col}")
            except Exception:
                pass
    except Exception:
        pass


# 扩展字段（存 settings_json）：VPN pipeline / LLM 附加 / TTS 附加 / Embedding / 代理 / 知识库
_PACK_EXTRA_KEYS = (
    'vpn_pipeline_url', 'vpn_pipeline_token', 'vpn_enrich', 'vpn_tts',
    'llm_api_key', 'llm_provider', 'ragflow_app_id', 'ragflow_kb_id',
    'tts_base_url', 'tts_engine', 'tts_voice_profile', 'tts_speed',
    'embedding_base_url', 'embedding_model',
    'proxy_http', 'proxy_https', 'playwright_proxy',
)


def get_pack_remote_config(pack_id: str) -> Dict:
    _ensure()
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        _ensure_remote_config_table(cur)
        cur.execute("SELECT * FROM pack_remote_config WHERE industry_pack_id=?", (str(pack_id),))
        _r = cur.fetchone(); row = dict(_r) if _r else {}
        cur.close()
    if not row:
        row = {'industry_pack_id': str(pack_id), 'server_ip': '', 'server_port': 11236, 'ssh_host': '', 'ssh_port': 22,
               'ssh_user': '', 'ssh_password': '', 'llm_base_url': '', 'llm_model': '', 'tts_voice': '',
               'tts_gender': 'female', 'tts_dialect': 'mandarin', 'db_url': '', 'db_name': '', 'enabled': 1,
               'settings_json': '{}'}
    # 合并 settings_json 展开为顶层字段
    try:
        import json
        extra = json.loads(row.get('settings_json') or '{}')
        for k in _PACK_EXTRA_KEYS:
            row.setdefault(k, extra.get(k, ''))
    except Exception:
        pass
    return row


def set_pack_remote_config(pack_id: str, **kw) -> Dict:
    _ensure()
    fields = dict(kw)
    # 拆分核心列与扩展字段
    core = {k: fields[k] for k in ('server_ip', 'server_port', 'ssh_host', 'ssh_port', 'ssh_user', 'ssh_password',
                                   'llm_base_url', 'llm_model', 'tts_voice', 'tts_gender', 'tts_dialect',
                                   'db_url', 'db_name') if k in fields}
    extra = {k: fields[k] for k in _PACK_EXTRA_KEYS if k in fields}
    import json
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        _ensure_remote_config_table(cur)
        existing = cur.execute("SELECT settings_json FROM pack_remote_config WHERE industry_pack_id=?",
                               (str(pack_id),)).fetchone()
        merged = {}
        if existing:
            try:
                merged = json.loads(existing[0] or '{}')
            except Exception:
                merged = {}
        merged.update({k: str(v) for k, v in extra.items()})
        cur.execute(
            "INSERT INTO pack_remote_config(industry_pack_id, server_ip, server_port, ssh_host, ssh_port,"
            "  ssh_user, ssh_password, llm_base_url, llm_model, tts_voice, tts_gender, tts_dialect, db_url, db_name,"
            "  settings_json, enabled, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))"
            " ON CONFLICT(industry_pack_id) DO UPDATE SET"
            " server_ip=COALESCE(excluded.server_ip, pack_remote_config.server_ip),"
            " server_port=COALESCE(excluded.server_port, pack_remote_config.server_port),"
            " ssh_host=COALESCE(excluded.ssh_host, pack_remote_config.ssh_host),"
            " ssh_port=COALESCE(excluded.ssh_port, pack_remote_config.ssh_port),"
            " ssh_user=excluded.ssh_user, ssh_password=excluded.ssh_password,"
            " llm_base_url=excluded.llm_base_url, llm_model=excluded.llm_model,"
            " tts_voice=excluded.tts_voice, tts_gender=excluded.tts_gender, tts_dialect=excluded.tts_dialect,"
            " db_url=excluded.db_url, db_name=excluded.db_name, settings_json=excluded.settings_json,"
            " enabled=excluded.enabled, updated_at=datetime('now')",
            (str(pack_id), str(core.get('server_ip', '') or ''), int(core.get('server_port', 11236) or 11236),
             str(core.get('ssh_host', '') or ''), int(core.get('ssh_port', 22) or 22),
             str(core.get('ssh_user', '') or ''), str(core.get('ssh_password', '') or ''),
             str(core.get('llm_base_url', '') or '').rstrip('/'), str(core.get('llm_model', '') or ''),
             str(core.get('tts_voice', '') or ''), str(core.get('tts_gender', 'female') or 'female'),
             str(core.get('tts_dialect', 'mandarin') or 'mandarin'), str(core.get('db_url', '') or '').rstrip('/'),
             str(core.get('db_name', '') or ''), json.dumps(merged, ensure_ascii=False),
             1 if fields.get('enabled', True) else 0),
        )
        sqlite_db.connection.commit(); cur.close()
    return get_pack_remote_config(str(pack_id))


def pack_runtime(pack_id: str) -> Dict:
    """统一解析：返回该行业包运行配置（per-pack 优先，未配置回退全局 .env）。所有租户级消费方统一读此。"""
    cfg = get_pack_remote_config(pack_id)
    import config as _cfg
    gg = lambda name, default='': str(getattr(_cfg, name, '') or '') or default
    return {
        # 服务连接
        'server_ip': cfg.get('server_ip', ''), 'server_port': int(cfg.get('server_port', 11236) or 11236),
        'ssh_host': cfg.get('ssh_host', ''), 'ssh_port': int(cfg.get('ssh_port', 22) or 22),
        'ssh_user': cfg.get('ssh_user', ''), 'ssh_password': cfg.get('ssh_password', ''),
        # VPN pipeline（回退全局）
        'vpn_pipeline_url': cfg.get('vpn_pipeline_url', '') or gg('REMOTE_PIPELINE_URL', 'http://10.88.0.1:11236'),
        'vpn_pipeline_token': cfg.get('vpn_pipeline_token', '') or gg('REMOTE_PIPELINE_TOKEN'),
        'vpn_enrich': str(cfg.get('vpn_enrich') or '') or str(int(_cfg.REMOTE_PIPELINE_ENRICH)),
        'vpn_tts': str(cfg.get('vpn_tts') or '') or str(int(_cfg.REMOTE_PIPELINE_TTS)),
        # LLM（回退全局）
        'llm_base_url': cfg.get('llm_base_url', '') or gg('INTEL_LLM_BASE_URL', 'http://192.168.0.64:8106/v1'),
        'llm_model': cfg.get('llm_model', '') or gg('INTEL_LLM_MODEL', 'deepseek-v4-flash'),
        'llm_api_key': cfg.get('llm_api_key', '') or gg('INTEL_LLM_API_KEY', ''),
        'llm_provider': cfg.get('llm_provider', '') or gg('INTEL_LLM_PROVIDER', 'local'),
        'ragflow_app_id': cfg.get('ragflow_app_id', '') or gg('RAGFLOW_LLM_APP_ID', ''),
        'ragflow_kb_id': cfg.get('ragflow_kb_id', '') or gg('FINANCIAL_RAGFLOW_KB_ID', gg('RAGFLOW_KB_ID', '')),
        # TTS
        'tts_base_url': cfg.get('tts_base_url', '') or gg('INTEL_TTS_BASE_URL', _cfg.RAGFLOW_BASE_URL),
        'tts_voice': cfg.get('tts_voice', ''),
        'tts_gender': cfg.get('tts_gender', '') or gg('INTEL_TTS_GENDER', 'male'),
        'tts_dialect': cfg.get('tts_dialect', '') or gg('INTEL_TTS_DIALECT', 'mandarin'),
        'tts_engine': cfg.get('tts_engine', '') or gg('INTEL_TTS_ENGINE', 'CosyVoice3'),
        'tts_voice_profile': cfg.get('tts_voice_profile', '') or gg('INTEL_TTS_VOICE_PROFILE', 'male_mandarin_01'),
        'tts_speed': cfg.get('tts_speed', '') or str(_cfg.INTEL_TTS_SPEED),
        # Embedding
        'embedding_base_url': cfg.get('embedding_base_url', '') or gg('INTEL_EMBEDDING_BASE_URL', 'http://192.168.0.64:9997'),
        'embedding_model': cfg.get('embedding_model', '') or gg('INTEL_EMBEDDING_MODEL', 'bge-m3'),
        # 代理
        'proxy_http': cfg.get('proxy_http', '') or gg('PROXY_HTTP', ''),
        'proxy_https': cfg.get('proxy_https', '') or gg('PROXY_HTTPS', ''),
        'playwright_proxy': cfg.get('playwright_proxy', '') or gg('PLAYWRIGHT_PROXY', ''),
        # 数据库
        'db_url': cfg.get('db_url', ''), 'db_name': cfg.get('db_name', ''),
        # 启用
        'enabled': bool(cfg.get('enabled', True)),
    }


def probe_remote_capabilities(pack_id: str = '') -> Dict:
    """探查当前（或指定包）远端 VPN pipeline 的 capabilities：voices/llm/健康。"""
    cfg = get_pack_remote_config(pack_id) if pack_id else {}
    try:
        import requests as _req
        import config as _cfg
        from config import REMOTE_PIPELINE_URL, REMOTE_PIPELINE_TOKEN
        base = str(cfg.get('server_ip') or '').strip()
        if base:
            port = str(cfg.get('server_port') or '')
            base = 'http://' + base + ((':' + port) if port else '')
        else:
            base = str(REMOTE_PIPELINE_URL).rstrip('/')
        hdrs = {'Authorization': f'Bearer {REMOTE_PIPELINE_TOKEN}'} if REMOTE_PIPELINE_TOKEN else {}
        out = {'ok': False, 'version': None, 'voices': [], 'llm_models': [], 'tts_ok': False, 'error': ''}
        try:
            r = _req.get(f'{base}/v1/health', timeout=4, headers=hdrs)
            if r.status_code == 200:
                j = r.json(); out['ok'] = bool(j.get('ok')); out['version'] = j.get('version')
        except Exception as exc:
            out['error'] = str(exc)[:120]
        try:
            rc = _req.get(f'{base}/v1/capabilities', timeout=4, headers=hdrs)
            if rc.status_code == 200:
                cj = rc.json(); out['voices'] = [str(v.get('id')) for v in (cj.get('tts_voices') or [])];
                _comp = cj.get('components') or {}
                out['tts_ok'] = bool((_comp.get('tts') or {}).get('ok'))
                if isinstance(cj.get('llm_models'), list):
                    out['llm_models'] = [str(m) for m in cj.get('llm_models')]
                elif cj.get('model'):
                    out['llm_models'] = [str(cj.get('model'))]
        except Exception:
            pass
        out['probe_url'] = base
        # ── 额外探针：Embedding / 数据库 / 代理（用解析后的值测，未配置则跳过） ──
        rt = pack_runtime(pack_id)
        # Embedding（填写值 + 启用/未启用状态）
        try:
            emb_url = str(rt.get('embedding_base_url') or '').rstrip('/')
            emb_model = str(rt.get('embedding_model') or '')
            ok = False
            if emb_url:
                er = _req.get(emb_url.rstrip('/') + '/v1/models', timeout=4)
                ok = er.status_code == 200
            emb_enabled = bool(int(getattr(_cfg, 'INTEL_EMBEDDING_ENABLED', 0)) or 0) or bool(int(getattr(_cfg, 'INTEL_BERTOPIC_ENABLED', 0)) or 0)
            out['embedding'] = {'base_url': emb_url, 'model': emb_model, 'ok': ok, 'enabled': emb_enabled}
        except Exception:
            out['embedding'] = {'ok': False, 'base_url': '', 'model': '', 'enabled': False}
        # 数据库（集中部署 / 按行业分库 + 主库位置 + 备份）
        try:
            db_url = str(rt.get('db_url') or '').rstrip('/')
            db_name = str(rt.get('db_name') or '')
            db_ok = False
            if db_url:
                import sqlalchemy as _sa
                eng = _sa.create_engine(db_url, pool_pre_ping=True)
                with eng.connect() as _c:
                    db_ok = True
            mode = 'per_pack' if db_url else 'centralized'
            loc = f"{getattr(_cfg, 'POSTGRES_HOST', '?')}:{getattr(_cfg, 'POSTGRES_PORT', 5432)}/{getattr(_cfg, 'POSTGRES_DB', '?')}"
            backup_path = str(getattr(_cfg, 'SQLITE_BACKUP_PATH', 'data/crawler_articles.db') or '')
            # backup_exists：行业包激活切换会备份（activation backup），此处标记是否存在备份位；periodic 当前无定时 pg_dump
            backup_exists = bool(backup_path)
            out['database'] = {'url': db_url, 'name': db_name, 'ok': db_ok, 'mode': mode,
                               'location': loc, 'backup_exists': backup_exists, 'backup_path': backup_path,
                               'periodic': False}
        except Exception:
            out['database'] = {'ok': False, 'url': '', 'name': '', 'mode': 'centralized', 'location': '', 'backup_exists': False, 'backup_path': '', 'periodic': False}
        # 代理
        try:
            ph = str(rt.get('proxy_http') or '')
            pg = str(rt.get('playwright_proxy') or '')
            pk = False
            if ph:
                pr = _req.get('http://www.gstatic.com/generate_204', proxies={'http': ph, 'https': ph}, timeout=5)
                pk = pr.status_code == 204 or pr.status_code < 400
            out['proxy'] = {'http': ph, 'playwright': pg, 'ok': pk}
        except Exception:
            out['proxy'] = {'ok': False, 'http': '', 'playwright': ''}
        # 解析后的全字段（供前端回填）
        out['resolved'] = {k: rt.get(k, '') for k in (
            'server_ip', 'server_port', 'ssh_host', 'ssh_port', 'ssh_user', 'ssh_password',
            'vpn_pipeline_url', 'vpn_pipeline_token', 'llm_base_url', 'llm_model', 'llm_api_key',
            'llm_provider', 'tts_voice', 'tts_gender', 'tts_dialect', 'tts_base_url', 'tts_engine',
            'tts_voice_profile', 'tts_speed', 'embedding_base_url', 'embedding_model',
            'db_url', 'db_name', 'proxy_http', 'proxy_https', 'playwright_proxy', 'enabled')}
        return out
    except Exception as exc:
        return {'ok': False, 'error': str(exc)[:120], 'voices': [], 'llm_models': []}



def _pack_display_name(pack_id) -> str:
    """该用户绑定行业包的显示名——跨租户隔离：标题/界面只能用用户自己的包名，
    绝不能用全局激活包的名字，否则不同行业的租户会看到别人的行业名。"""
    pack_id = str(pack_id or '').strip()
    if not pack_id:
        return ''
    try:
        from industry_packs import industry_pack_loader
        return str(industry_pack_loader.load(pack_id).get('name') or pack_id)
    except Exception:
        return pack_id