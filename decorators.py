#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
装饰器模块
包含登录验证等装饰器
"""

from functools import wraps
from flask import request, jsonify, redirect, url_for
from user_database import user_db


def login_required(f):
    """需要登录才能访问的装饰器：接受管理员会话，或活跃（未到期）的包用户会话。"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # 检查cookie中的session_token
        token = request.cookies.get('session_token') or request.headers.get('Authorization')
        if token and token.lower().startswith('bearer '):
            token = token[7:].strip()
        session_data = None
        if token:
            session_data = user_db.verify_session(token)
        if not session_data:
            # 包用户会话兜底：活跃且未到期才放行（数据隔离由各接口按 pack_id 强制）
            try:
                from pack_tenant import active_pack_user
                pu = active_pack_user()
            except Exception:
                pu = None
            if pu:
                request.current_user = {
                    'role': 'pack_user', 'pack_user_id': int(pu['id']),
                    'pack_id': str(pu.get('industry_pack_id') or ''),
                    'username': str(pu.get('username') or ''),
                    'user': pu,
                }
                return f(*args, **kwargs)
            if request.is_json or request.path.startswith('/api/') or '/api/' in request.path:
                return jsonify({'success': False, 'error': '请先登录或有包用户会话'}), 401
            return redirect(url_for('login_page'))
        request.current_user = session_data
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    """Require an authenticated administrator."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.cookies.get('session_token') or request.headers.get('Authorization')
        if token and token.lower().startswith('bearer '):
            token = token[7:].strip()
        if not token:
            return jsonify({'success': False, 'error': '未登录'}), 401
        session_data = user_db.verify_session(token)
        if not session_data:
            return jsonify({'success': False, 'error': '会话已过期，请重新登录'}), 401
        if session_data.get('role') != 'admin':
            return jsonify({'success': False, 'error': '权限不足，需要管理员权限'}), 403
        request.current_user = session_data
        return f(*args, **kwargs)
    return decorated_function

