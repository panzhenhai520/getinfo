#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
用户管理API模块
提供用户登录、注册、管理等功能
"""

from flask import Blueprint, request, jsonify, session, redirect, url_for
from functools import wraps
from user_database import user_db
from utils import coerce_int

# 创建用户管理蓝图
user_bp = Blueprint('user', __name__, url_prefix='/api/user')

def login_required(f):
    """登录验证装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.cookies.get('session_token') or request.headers.get('Authorization')
        
        if not token:
            return jsonify({'success': False, 'error': '未登录'}), 401
        
        # 验证会话
        session_data = user_db.verify_session(token)
        if not session_data:
            return jsonify({'success': False, 'error': '会话已过期，请重新登录'}), 401
        
        # 将用户信息附加到请求上下文
        request.current_user = session_data
        return f(*args, **kwargs)
    
    return decorated_function

def admin_required(f):
    """管理员权限验证装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.cookies.get('session_token') or request.headers.get('Authorization')
        
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

@user_bp.route('/login', methods=['POST'])
def login():
    """用户登录"""
    try:
        data = request.get_json(silent=True) or {}
        username = data.get('username')
        password = data.get('password')
        remember_me = data.get('remember_me', False)
        
        if not username or not password:
            return jsonify({'success': False, 'error': '用户名和密码不能为空'})
        
        # 验证用户
        user = user_db.verify_user(username, password)
        if not user:
            # 记录失败的登录尝试
            user_db.log_action(None, 'login_failed', f'用户名: {username}', request.remote_addr)
            return jsonify({'success': False, 'error': '用户名或密码错误'})
        
        # 创建会话
        expire_hours = 168 if remember_me else 24  # 记住我：7天，否则24小时
        token = user_db.create_session(
            user['id'], 
            request.remote_addr, 
            request.headers.get('User-Agent'),
            expire_hours
        )
        
        if not token:
            return jsonify({'success': False, 'error': '创建会话失败'})
        
        # 记录登录日志
        user_db.log_action(user['id'], 'login', f'登录成功', request.remote_addr)
        
        # 清理行业包相关 session：换用户登录时不得残留上一个用户选的包/包用户身份
        from flask import session as _session
        _session.pop('admin_industry_pack_id', None)
        _session.pop('admin_pack_choice_token', None)
        _session.pop('pack_user_id', None)
        
        response = jsonify({
            'success': True,
            'message': '登录成功',
            'user': {
                'id': user['id'],
                'username': user['username'],
                'email': user['email'],
                'full_name': user['full_name'],
                'role': user['role']
            },
            'token': token
        })
        
        # 设置Cookie - 添加samesite和path参数确保cookie能被正确设置和读取
        response.set_cookie(
            'session_token', 
            token, 
            max_age=expire_hours*3600, 
            httponly=True,
            samesite='Lax',
            path='/'
        )
        
        return response
        
    except Exception as e:
        print(f"❌ 登录失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@user_bp.route('/logout', methods=['POST'])
@login_required
def logout():
    """用户退出"""
    try:
        token = request.cookies.get('session_token') or request.headers.get('Authorization')
        
        # 删除会话
        user_db.delete_session(token)
        
        # 清理行业包相关 session，避免下次登录残留上一个用户的选择
        from flask import session as _session
        _session.pop('admin_industry_pack_id', None)
        _session.pop('admin_pack_choice_token', None)
        _session.pop('pack_user_id', None)
        
        # 记录退出日志
        user_db.log_action(request.current_user['user_id'], 'logout', '退出登录', request.remote_addr)
        
        response = jsonify({'success': True, 'message': '退出成功'})
        response.set_cookie('session_token', '', expires=0)
        
        return response
        
    except Exception as e:
        print(f"❌ 退出失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@user_bp.route('/current', methods=['GET'])
@login_required
def get_current_user():
    """获取当前登录用户信息"""
    try:
        user_id = request.current_user['user_id']
        user = user_db.get_user_by_id(user_id)
        
        if not user:
            return jsonify({'success': False, 'error': '用户不存在'})
        
        return jsonify({
            'success': True,
            'user': user
        })
        
    except Exception as e:
        print(f"❌ 获取当前用户信息失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@user_bp.route('/list', methods=['GET'])
@admin_required
def list_users():
    """获取用户列表（管理员）"""
    try:
        page = coerce_int(request.args.get('page'), 1, 1)
        per_page = coerce_int(request.args.get('per_page'), 20, 1, 500)
        role = request.args.get('role')
        is_active = request.args.get('is_active')
        
        if is_active is not None:
            is_active = is_active.lower() == 'true'
        
        users, total = user_db.get_users(page, per_page, role, is_active)
        
        # 计算分页信息
        total_pages = (total + per_page - 1) // per_page
        start = (page - 1) * per_page + 1 if total > 0 else 0
        end = min(page * per_page, total)
        
        return jsonify({
            'success': True,
            'users': users,
            'pagination': {
                'page': page,
                'pages': total_pages,
                'total': total,
                'start': start,
                'end': end
            }
        })
        
    except Exception as e:
        print(f"❌ 获取用户列表失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@user_bp.route('/create', methods=['POST'])
@admin_required
def create_user():
    """创建用户（管理员）"""
    try:
        data = request.get_json(silent=True) or {}
        username = data.get('username')
        password = data.get('password')
        email = data.get('email')
        full_name = data.get('full_name')
        role = data.get('role', 'user')
        
        if not username or not password:
            return jsonify({'success': False, 'error': '用户名和密码不能为空'})
        
        if len(password) < 6:
            return jsonify({'success': False, 'error': '密码长度至少6位'})
        
        user_id = user_db.create_user(username, password, email, full_name, role)
        
        if not user_id:
            return jsonify({'success': False, 'error': '用户名已存在或创建失败'})
        
        # 记录操作日志
        user_db.log_action(
            request.current_user['user_id'], 
            'create_user', 
            f'创建用户: {username}', 
            request.remote_addr
        )
        
        return jsonify({
            'success': True,
            'message': '用户创建成功',
            'user_id': user_id
        })
        
    except Exception as e:
        print(f"❌ 创建用户失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@user_bp.route('/update/<int:user_id>', methods=['PUT'])
@admin_required
def update_user(user_id):
    """更新用户信息（管理员）"""
    try:
        data = request.get_json(silent=True) or {}
        
        # 不允许修改自己的角色
        if user_id == request.current_user['user_id'] and 'role' in data:
            return jsonify({'success': False, 'error': '不能修改自己的角色'})
        
        success = user_db.update_user(user_id, data)
        
        if not success:
            return jsonify({'success': False, 'error': '更新失败'})
        
        # 记录操作日志
        user_db.log_action(
            request.current_user['user_id'], 
            'update_user', 
            f'更新用户: {user_id}', 
            request.remote_addr
        )
        
        return jsonify({
            'success': True,
            'message': '用户更新成功'
        })
        
    except Exception as e:
        print(f"❌ 更新用户失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@user_bp.route('/delete/<int:user_id>', methods=['DELETE'])
@admin_required
def delete_user(user_id):
    """删除用户（管理员）"""
    try:
        # 不允许删除自己
        if user_id == request.current_user['user_id']:
            return jsonify({'success': False, 'error': '不能删除自己的账号'})
        
        success = user_db.delete_user(user_id)
        
        if not success:
            return jsonify({'success': False, 'error': '删除失败'})
        
        # 记录操作日志
        user_db.log_action(
            request.current_user['user_id'], 
            'delete_user', 
            f'删除用户: {user_id}', 
            request.remote_addr
        )
        
        return jsonify({
            'success': True,
            'message': '用户删除成功'
        })
        
    except Exception as e:
        print(f"❌ 删除用户失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@user_bp.route('/change-password', methods=['POST'])
@login_required
def change_password():
    """修改密码"""
    try:
        data = request.get_json(silent=True) or {}
        old_password = data.get('old_password')
        new_password = data.get('new_password')
        
        if not old_password or not new_password:
            return jsonify({'success': False, 'error': '旧密码和新密码不能为空'})
        
        if len(new_password) < 6:
            return jsonify({'success': False, 'error': '新密码长度至少6位'})
        
        # 验证旧密码
        user_id = request.current_user['user_id']
        user = user_db.get_user_by_id(user_id)
        username = user['username']
        
        if not user_db.verify_user(username, old_password):
            return jsonify({'success': False, 'error': '旧密码错误'})
        
        # 更新密码
        success = user_db.update_user(user_id, {'password': new_password})
        
        if not success:
            return jsonify({'success': False, 'error': '密码修改失败'})
        
        # 记录操作日志
        user_db.log_action(user_id, 'change_password', '修改密码', request.remote_addr)
        
        return jsonify({
            'success': True,
            'message': '密码修改成功'
        })
        
    except Exception as e:
        print(f"❌ 修改密码失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@user_bp.route('/logs', methods=['GET'])
@admin_required
def get_user_logs():
    """获取用户操作日志（管理员）"""
    try:
        user_id = request.args.get('user_id', type=int)
        page = coerce_int(request.args.get('page'), 1, 1)
        per_page = coerce_int(request.args.get('per_page'), 50, 1, 500)
        
        logs, total = user_db.get_user_logs(user_id, page, per_page)
        
        return jsonify({
            'success': True,
            'logs': logs,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': (total + per_page - 1) // per_page
        })
        
    except Exception as e:
        print(f"❌ 获取用户日志失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@user_bp.route('/stats', methods=['GET'])
@admin_required
def get_user_stats():
    """获取用户统计信息（管理员）"""
    try:
        cursor = user_db.connection.cursor()
        
        # 总用户数
        cursor.execute("SELECT COUNT(*) as total FROM users")
        total_users = cursor.fetchone()['total']
        
        # 活跃用户数
        cursor.execute("SELECT COUNT(*) as total FROM users WHERE is_active = TRUE")
        active_users = cursor.fetchone()['total']
        
        # 各角色用户数
        cursor.execute("SELECT role, COUNT(*) as count FROM users GROUP BY role")
        role_stats = {row['role']: row['count'] for row in cursor.fetchall()}
        
        # 今日登录用户数
        cursor.execute("""
            SELECT COUNT(DISTINCT user_id) as count 
            FROM user_logs 
            WHERE action = 'login' AND DATE(created_at) = DATE('now')
        """)
        today_logins = cursor.fetchone()['count']
        
        cursor.close()
        
        return jsonify({
            'success': True,
            'stats': {
                'total_users': total_users,
                'active_users': active_users,
                'inactive_users': total_users - active_users,
                'role_stats': role_stats,
                'today_logins': today_logins
            }
        })
        
    except Exception as e:
        print(f"❌ 获取用户统计失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

