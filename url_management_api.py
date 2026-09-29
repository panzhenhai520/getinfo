#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
URL管理API模块
提供URL管理和持久化功能
"""

from flask import Blueprint, request, jsonify
from sqlite_database import sqlite_db
from datetime import datetime
import json
import asyncio
import requests
from auto_login import AutoLogin
from urllib.parse import urlparse
from utils import coerce_int
from intel_sources import canonicalize_source_url, intel_source_registry
from intel_database import intel_repository
from industry_packs import industry_pack_loader

# 创建蓝图
url_management_bp = Blueprint('url_management_new', __name__, url_prefix='/api/url-management')

# 创建AutoLogin实例
auto_login = AutoLogin()


def sync_source_registry_after_url_change():
    """Keep the source registry as the canonical, immediately usable view of URL edits."""
    try:
        return intel_source_registry.sync_legacy_sources(dry_run=False)
    except Exception as exc:
        # URL registration remains successful even if the derived registry sync is retried later.
        print(f"⚠️ 信源注册同步失败，将由后台任务重试: {exc}")
        return None


def enqueue_initial_source_scan(source_url, industry_pack_id=""):
    """Queue a newly registered source once; recurring scans remain unified."""
    try:
        canonical_url = canonicalize_source_url(source_url)
        intel_source_registry._ensure()
        with intel_source_registry.db.lock:
            row = intel_source_registry.db.connection.execute(
                "SELECT id FROM intel_sources WHERE canonical_source_url=?",
                (canonical_url,),
            ).fetchone()
        if not row:
            return None
        source_id = int(row["id"])
        runtime = intel_repository.active_runtime_context()
        pack_id = str(
            industry_pack_id
            or runtime.get("primary_industry_pack_id")
            or "family_office"
        )
        activation_id = str(runtime.get("activation_id") or "")
        job_id, _ = intel_repository.enqueue_job(
            "light_scan",
            f"initial-source-scan:{pack_id}:{activation_id or 'seed'}:{source_id}",
            {
                "initial_source_scan": True,
                "industry_pack_id": pack_id,
                "source_ids": [source_id],
                "include_serpapi": False,
                "max_sources": 1,
            },
        )
        return job_id
    except Exception as exc:
        print(f"⚠️ 新信源初始扫描创建失败，将由每日扫描覆盖: {exc}")
        return None

def get_category_id_by_name(category_name):
    """
    根据分类名称获取分类ID
    
    Args:
        category_name: 分类名称
        
    Returns:
        int or None: 分类ID，不存在返回None
    """
    if not category_name:
        return None
    
    try:
        sqlite_db.connect()
        cursor = sqlite_db.connection.cursor()
        cursor.execute("SELECT id FROM categories WHERE name = ?", (category_name,))
        result = cursor.fetchone()
        cursor.close()
        
        if result:
            return result[0]
        return None
    except Exception as e:
        print(f"⚠️ 查询分类ID失败: {e}")
        return None

def get_category_name_by_id(category_id):
    """
    根据分类ID获取分类名称
    
    Args:
        category_id: 分类ID
        
    Returns:
        str: 分类名称，不存在返回'默认分类'
    """
    if category_id is None:
        return '默认分类'
    
    try:
        sqlite_db.connect()
        cursor = sqlite_db.connection.cursor()
        cursor.execute("SELECT name FROM categories WHERE id = ?", (category_id,))
        result = cursor.fetchone()
        cursor.close()
        
        if result:
            return result[0]
        return '默认分类'
    except Exception as e:
        print(f"⚠️ 获取分类名称失败: {e}")
        return '默认分类'

def validate_and_fix_url(url):
    """
    验证并修复URL格式
    
    Args:
        url: 原始URL
        
    Returns:
        tuple: (fixed_url, is_valid, error_message)
    """
    if not url:
        return None, False, "URL不能为空"
    
    url = url.strip()
    
    if not url:
        return None, False, "URL不能为空"
    
    # 检查是否包含危险协议
    dangerous_protocols = ['javascript:', 'data:', 'file:', 'ftp:']
    for protocol in dangerous_protocols:
        if url.lower().startswith(protocol):
            return url, False, f"不支持的协议: {protocol}"
    
    # 处理协议相对URL（//example.com）
    if url.startswith('//'):
        url = 'https:' + url
        print(f"🔧 URL已转换协议相对URL: {url}")
    # 如果没有协议前缀，自动添加https://
    elif not url.startswith(('http://', 'https://')):
        url = 'https://' + url
        print(f"🔧 URL已自动添加协议前缀: {url}")
    
    # 验证URL格式
    try:
        parsed = urlparse(url)
        
        # 检查协议是否有效
        if parsed.scheme not in ('http', 'https'):
            return url, False, f"URL协议无效：{parsed.scheme}，只支持http和https"
        
        # 检查是否有域名
        if not parsed.netloc:
            return url, False, "URL格式无效：缺少域名"
        
        # 检查域名是否合法（至少包含一个点，除非是localhost）
        if '.' not in parsed.netloc and parsed.netloc != 'localhost':
            return url, False, f"域名格式无效：{parsed.netloc}"
        
        return url, True, None
        
    except Exception as e:
        return url, False, f"URL格式验证失败: {str(e)}"

def check_url_accessibility(url, timeout=8):
    """Check whether a managed URL is reachable enough for users to keep it."""
    fixed_url, is_valid, error_message = validate_and_fix_url(url)
    if not is_valid:
        return {
            'status': 'unreachable',
            'status_code': None,
            'message': error_message or 'URL格式无效',
            'final_url': fixed_url or url
        }

    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/125.0 Safari/537.36'
        ),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    }

    try:
        response = requests.get(
            fixed_url,
            timeout=timeout,
            allow_redirects=True,
            headers=headers,
            stream=True
        )
        response.close()
        status_code = response.status_code

        if 200 <= status_code < 400:
            status = 'reachable'
            message = '可访问'
        elif status_code in (401, 403):
            status = 'restricted'
            message = f'网站可达，但访问受限（HTTP {status_code}）'
        else:
            status = 'unreachable'
            message = f'访问异常（HTTP {status_code}）'

        return {
            'status': status,
            'status_code': status_code,
            'message': message,
            'final_url': response.url or fixed_url
        }
    except requests.exceptions.Timeout:
        return {
            'status': 'unreachable',
            'status_code': None,
            'message': '访问超时',
            'final_url': fixed_url
        }
    except requests.exceptions.RequestException as exc:
        return {
            'status': 'unreachable',
            'status_code': None,
            'message': str(exc),
            'final_url': fixed_url
        }

# 异步执行登录的辅助函数
def execute_auth_login(auth_config_json, auth_name, proxy_enabled=None):
    """执行认证登录（同步包装异步函数）"""
    async def _async_login():
        try:
            auth_config = json.loads(auth_config_json) if isinstance(auth_config_json, str) else auth_config_json
            result = await auto_login.login_and_save(auth_config, auth_name, proxy_enabled=proxy_enabled)
            return result
        except Exception as e:
            return {
                'success': False,
                'message': f'登录失败: {str(e)}'
            }
    
    return asyncio.run(_async_login())

@url_management_bp.route('/urls', methods=['GET'])
def get_urls():
    """获取URL列表"""
    try:
        page = coerce_int(request.args.get('page'), 1, 1)
        per_page = coerce_int(request.args.get('per_page'), 20, 1, 500)
        category_id = request.args.get('category_id')
        parent_url_id = request.args.get('parent_url_id')
        is_active = request.args.get('is_active')
        industry_pack_id = str(request.args.get('industry_pack_id') or '').strip()
        if not industry_pack_id and request.args.get('scope') != 'all':
            industry_pack_id = str(
                intel_repository.active_runtime_context().get('primary_industry_pack_id')
                or 'family_office'
            )
        
        # 转换category_id为整数
        if category_id:
            category_id = coerce_int(category_id, None, 1)
        
        # 转换parent_url_id为整数
        if parent_url_id:
            parent_url_id = coerce_int(parent_url_id, None, 1)
        
        # 转换is_active为布尔值
        if is_active is not None:
            is_active = is_active.lower() in ('true', '1', 'yes')
        
        effective_pack_ids = None
        if industry_pack_id:
            effective_pack_ids = [
                pack['id']
                for pack in industry_pack_loader.effective_pack_set(industry_pack_id)
            ]
        urls, total = sqlite_db.get_managed_urls(
            page,
            per_page,
            category_id,
            parent_url_id,
            is_active,
            industry_pack_id=industry_pack_id or None,
            effective_pack_ids=effective_pack_ids,
        )
        
        return jsonify({
            'success': True,
            'urls': urls,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': (total + per_page - 1) // per_page
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取URL列表失败: {str(e)}'
        }), 500

@url_management_bp.route('/urls/check-accessibility', methods=['POST'])
def check_urls_accessibility():
    """批量检测URL可访问性，并将结果写回managed_urls。"""
    try:
        data = request.get_json(silent=True) or {}
        raw_ids = data.get('url_ids') or []

        url_ids = []
        for raw_id in raw_ids:
            try:
                url_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if url_id > 0 and url_id not in url_ids:
                url_ids.append(url_id)

        if not url_ids:
            return jsonify({
                'success': False,
                'message': '请选择或加载需要检测的URL'
            }), 400

        if len(url_ids) > 200:
            return jsonify({
                'success': False,
                'message': '单次最多检测200个URL，请缩小范围后重试'
            }), 400

        sqlite_db.connect()
        with sqlite_db.lock:
            cursor = sqlite_db.connection.cursor()
            placeholders = ','.join(['?'] * len(url_ids))
            cursor.execute(
                f"SELECT id, url, name FROM managed_urls WHERE id IN ({placeholders})",
                url_ids
            )
            rows = [dict(row) for row in cursor.fetchall()]
            cursor.close()

        if not rows:
            return jsonify({
                'success': False,
                'message': '未找到需要检测的URL'
            }), 404

        results = []
        checked_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        for row in rows:
            check_result = check_url_accessibility(row.get('url'))
            message = (check_result.get('message') or '')[:500]
            result = {
                'id': row['id'],
                'url': row.get('url'),
                'name': row.get('name'),
                'access_status': check_result.get('status'),
                'access_status_code': check_result.get('status_code'),
                'access_error': message,
                'access_checked_at': checked_at,
                'final_url': check_result.get('final_url')
            }
            results.append(result)

            with sqlite_db.lock:
                cursor = sqlite_db.connection.cursor()
                cursor.execute(
                    """
                    UPDATE managed_urls
                    SET access_status = ?,
                        access_status_code = ?,
                        access_error = ?,
                        access_checked_at = ?,
                        updated_at = datetime('now', 'localtime')
                    WHERE id = ?
                    """,
                    (
                        result['access_status'],
                        result['access_status_code'],
                        result['access_error'],
                        result['access_checked_at'],
                        row['id']
                    )
                )
                sqlite_db.connection.commit()
                cursor.close()

        summary = {
            'total': len(results),
            'reachable': sum(1 for item in results if item['access_status'] == 'reachable'),
            'restricted': sum(1 for item in results if item['access_status'] == 'restricted'),
            'unreachable': sum(1 for item in results if item['access_status'] == 'unreachable'),
        }

        return jsonify({
            'success': True,
            'results': results,
            'summary': summary
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'检测URL可访问性失败: {str(e)}'
        }), 500

@url_management_bp.route('/urls', methods=['POST'])
def add_url():
    """添加新URL"""
    try:
        # 🔥 强制修复：直接处理数据转换问题
        print("🔥 进入add_url函数")
        
        data = request.get_json(silent=True) or {}
        print(f"🔥 收到数据: {data}")
        
        # 🔥 强制修复：如果收到category而不是category_id，立即修复
        if 'category' in data and 'category_id' not in data:
            print(f"🔥 检测到数据被转换！强制修复")
            # 检查原始请求数据
            raw_data = request.get_data(as_text=True)
            print(f"🔥 原始数据: {raw_data}")
            
            # 强制设置category_id
            if '"category_id":3' in raw_data:
                data['category_id'] = 3
                del data['category']
                print(f"🔥 强制修复：设置category_id = 3")
            elif '"category_id":1' in raw_data:
                data['category_id'] = 1
                del data['category']
                print(f"🔥 强制修复：设置category_id = 1")
            elif '"category_id":2' in raw_data:
                data['category_id'] = 2
                del data['category']
                print(f"🔥 强制修复：设置category_id = 2")
        
        print(f"🔥 修复后数据: {data}")
        
        # 🔥 修复：统一处理category_id
        final_category_id = None
        
        # 情况1：直接有category_id
        if 'category_id' in data and data['category_id'] is not None:
            final_category_id = data['category_id']
            print(f"🔍 收到category_id: {final_category_id}")
        
        # 情况2：有category名称需要转换
        elif 'category' in data:
            category_name = data.get('category')
            print(f"🔍 检测到category字段: '{category_name}'")
            
            # 从数据库中查找分类ID
            final_category_id = get_category_id_by_name(category_name)
            
            if final_category_id:
                print(f"🔄 分类转换: '{category_name}' -> category_id: {final_category_id}")
            else:
                print(f"⚠️  分类 '{category_name}' 不存在，设置为None")
            
            del data['category']
        
        # 强制设置最终的category_id和对应的category文本字段
        data['category_id'] = final_category_id
        
        # 同步更新category文本字段（从数据库获取）
        data['category'] = get_category_name_by_id(final_category_id)
        
        print(f"🔥 最终设置category_id: {final_category_id}, category: {data['category']}")
        
        if not data or not data.get('url'):
            return jsonify({
                'success': False,
                'error': '缺少URL参数'
            }), 400
        
        # 验证并修复URL格式
        original_url = data.get('url')
        fixed_url, is_valid, error_msg = validate_and_fix_url(original_url)
        
        if not is_valid:
            return jsonify({
                'success': False,
                'error': f'URL格式错误: {error_msg}'
            }), 400
        
        # 使用修复后的URL
        data['url'] = fixed_url
        print(f"✅ URL验证通过: {fixed_url}")
        
        # 检查是否有认证配置
        auth_config = data.get('auth_config')
        auth_config_id = data.get('auth_config_id')
        auth_result = None
        
        print(f"\n🔍 认证配置检查:")
        print(f"   auth_config_id = {auth_config_id} (类型: {type(auth_config_id).__name__})")
        print(f"   auth_config = {'有' if auth_config else '无'}")
        
        # 方式1：使用已有认证配置
        if auth_config_id:
            print(f"\n{'='*50}")
            print(f"🔥 检测到 auth_config_id: {auth_config_id}")
            print(f"{'='*50}")
            # 🔥 获取认证配置并刷新Cookie
            from sqlite_database import sqlite_db as db
            db._ensure_connection()
            cursor = db.connection.cursor()
            cursor.execute("SELECT * FROM auth_configs WHERE id = ?", (auth_config_id,))
            row = cursor.fetchone()
            
            if row:
                # 获取认证配置信息
                success_indicator = {}
                if row['success_indicator_type'] and row['success_indicator_value']:
                    success_indicator = {
                        'type': row['success_indicator_type'],
                        'value': row['success_indicator_value']
                    }
                
                auth_config_dict = {
                    'login_url': row['login_url'],
                    'username': row['username'],
                    'password': row['password'],
                    'username_selector': row['username_selector'],
                    'password_selector': row['password_selector'],
                    'submit_selector': row['submit_selector'],
                    'wait_after_submit': row['wait_after_submit'] or 5,
                    'success_indicator': success_indicator
                }
                auth_name = row['name']
                
                print(f"🔐 刷新认证Cookie: {auth_name}")
                try:
                    refresh_result = execute_auth_login(auth_config_dict, auth_name)
                    if refresh_result.get('success'):
                        print(f"✅ Cookie刷新成功: {auth_name}")
                    else:
                        print(f"⚠️ Cookie刷新失败: {refresh_result.get('message')}")
                except Exception as refresh_err:
                    print(f"⚠️ Cookie刷新出错: {refresh_err}")
            
            # 更新使用次数
            cursor.execute("""
                UPDATE auth_configs 
                SET use_count = use_count + 1,
                    last_used_at = datetime('now', 'localtime')
                WHERE id = ?
            """, (auth_config_id,))
            db.connection.commit()
            cursor.close()
        
        # 方式2：创建新的认证配置
        elif auth_config:
            # 生成认证名称（使用URL的ID或名称）
            auth_name = data.get('name', data.get('url', 'unknown')).replace('/', '_').replace(':', '_')[:50]
            
            print(f"🔐 检测到新认证配置，开始执行登录...")
            auth_result = execute_auth_login(auth_config, auth_name)
            
            if not auth_result['success']:
                return jsonify({
                    'success': False,
                    'error': f'认证登录失败: {auth_result["message"]}'
                }), 500
            
            print(f"✅ 认证登录成功: {auth_name}")
            
            # 将认证配置保存到auth_configs表
            if isinstance(auth_config, str):
                auth_config_dict = json.loads(auth_config)
            else:
                auth_config_dict = auth_config
            
            try:
                cursor = sqlite_db.connection.cursor()
                success_indicator = auth_config_dict.get('success_indicator', {})
                
                cursor.execute("""
                    INSERT INTO auth_configs 
                    (name, login_url, username, password, username_selector,
                     password_selector, submit_selector, wait_after_submit,
                     success_indicator_type, success_indicator_value,
                     storage_file, cookies_count, use_count, last_used_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, datetime('now', 'localtime'))
                """, (
                    auth_name,
                    auth_config_dict.get('login_url', ''),
                    auth_config_dict.get('username', ''),
                    auth_config_dict.get('password', ''),
                    auth_config_dict.get('username_selector', ''),
                    auth_config_dict.get('password_selector', ''),
                    auth_config_dict.get('submit_selector', ''),
                    auth_config_dict.get('wait_after_submit', 5),
                    success_indicator.get('type') if isinstance(success_indicator, dict) else None,
                    success_indicator.get('value') if isinstance(success_indicator, dict) else None,
                    auth_result.get('storage_state', ''),
                    len(auth_result.get('cookies', []))
                ))
                
                auth_config_id = cursor.lastrowid
                data['auth_config_id'] = auth_config_id
                
                sqlite_db.connection.commit()
                cursor.close()
                
                print(f"💾 认证配置已保存到数据库，ID: {auth_config_id}")
                
            except Exception as e:
                print(f"⚠️  保存认证配置到数据库失败: {e}")
                # 继续执行，使用JSON格式保存
                auth_config_dict['auth_name'] = auth_name
                data['auth_config'] = json.dumps(auth_config_dict, ensure_ascii=False)
        
        # 字段名转换：前端使用parent_id，后端需要parent_url_id
        if 'parent_id' in data and 'parent_url_id' not in data:
            data['parent_url_id'] = data.pop('parent_id')
        
        # 🔥 强制保留category_id（防止被其他代码覆盖）
        category_id = data.get('category_id')
        
        # 调试日志：打印接收到的分类ID
        print(f"📝 添加URL - 接收到的数据:")
        print(f"   URL: {data.get('url')}")
        print(f"   名称: {data.get('name')}")
        print(f"   category_id: {category_id} (类型: {type(category_id)})")
        print(f"   parent_url_id: {data.get('parent_url_id')}")
        
        # 🔥 确保category_id被传递
        if category_id is not None:
            data['category_id'] = category_id
        
        # 保存URL到数据库
        url_id = sqlite_db.insert_managed_url(data)
        
        if url_id:
            sync_source_registry_after_url_change()
            initial_scan_job_id = enqueue_initial_source_scan(
                data.get('url'), data.get('industry_pack_id')
            )
            response_data = {
                'success': True,
                'url_id': url_id,
                'message': 'URL添加成功，已同步到统一信源并安排初始扫描'
            }
            if initial_scan_job_id:
                response_data['initial_scan_job_id'] = initial_scan_job_id
            
            if auth_result:
                response_data['auth_status'] = {
                    'success': True,
                    'message': '认证配置已保存并登录成功',
                    'cookies_count': len(auth_result.get('cookies', []))
                }
            
            return jsonify(response_data)
        else:
            return jsonify({
                'success': False,
                'error': 'URL已存在或添加失败'
            }), 400
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': f'添加URL失败: {str(e)}'
        }), 500

@url_management_bp.route('/urls/<int:url_id>', methods=['GET'])
def get_url(url_id):
    """获取单个URL信息"""
    try:
        url = sqlite_db.get_managed_url_by_id(url_id)
        if url:
            return jsonify({
                'success': True,
                'url': url
            })
        else:
            return jsonify({
                'success': False,
                'message': 'URL不存在'
            }), 404
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取URL失败: {str(e)}'
        }), 500

@url_management_bp.route('/urls/<int:url_id>', methods=['PUT'])
def update_url(url_id):
    """更新URL"""
    try:
        data = request.get_json(silent=True) or {}
        
        # 🔍 立即打印原始数据
        print(f"🔍 PUT /api/urls/{url_id} - 原始数据: {data}")
        
        # 🔍 调试：打印接收到的数据
        print(f"\n{'='*70}")
        print(f"📥 收到更新URL请求: ID={url_id}")
        print(f"📦 接收到的数据键: {list(data.keys()) if data else 'None'}")
        if data and 'auth_config' in data:
            print(f"🔐 auth_config 存在: {data['auth_config'][:100] if data['auth_config'] else 'None'}...")
        else:
            print(f"⚠️ auth_config 不存在于请求数据中")
        print(f"{'='*70}\n")
        
        if not data:
            return jsonify({
                'success': False,
                'error': '缺少更新数据'
            }), 400
        
        # 如果更新URL，验证格式
        if 'url' in data and data['url']:
            original_url = data['url']
            fixed_url, is_valid, error_msg = validate_and_fix_url(original_url)
            
            if not is_valid:
                return jsonify({
                    'success': False,
                    'error': f'URL格式错误: {error_msg}'
                }), 400
            
            data['url'] = fixed_url
            print(f"✅ URL验证通过: {fixed_url}")
        
        # 处理认证配置（支持新旧两种方式）
        auth_config_id = data.get('auth_config_id')
        auth_config = data.get('auth_config')
        auth_result = None
        
        # 方式1：使用已有认证配置（新结构，推荐）
        if auth_config_id:
            print(f"📌 使用已有认证配置 ID: {auth_config_id}")
            # 🔥 获取认证配置并刷新Cookie
            try:
                cursor = sqlite_db.connection.cursor()
                cursor.execute("SELECT * FROM auth_configs WHERE id = ?", (auth_config_id,))
                row = cursor.fetchone()
                
                if row:
                    # 获取认证配置信息
                    success_indicator = {}
                    if row['success_indicator_type'] and row['success_indicator_value']:
                        success_indicator = {
                            'type': row['success_indicator_type'],
                            'value': row['success_indicator_value']
                        }
                    
                    auth_config_dict = {
                        'login_url': row['login_url'],
                        'username': row['username'],
                        'password': row['password'],
                        'username_selector': row['username_selector'],
                        'password_selector': row['password_selector'],
                        'submit_selector': row['submit_selector'],
                        'wait_after_submit': row['wait_after_submit'] or 5,
                        'success_indicator': success_indicator
                    }
                    auth_name = row['name']
                    
                    print(f"🔐 刷新认证Cookie: {auth_name}")
                    try:
                        refresh_result = execute_auth_login(auth_config_dict, auth_name)
                        if refresh_result.get('success'):
                            print(f"✅ Cookie刷新成功: {auth_name}")
                        else:
                            print(f"⚠️ Cookie刷新失败: {refresh_result.get('message')}")
                    except Exception as refresh_err:
                        print(f"⚠️ Cookie刷新出错: {refresh_err}")
                
                # 更新认证配置的使用次数
                cursor.execute("""
                    UPDATE auth_configs 
                    SET use_count = use_count + 1,
                        last_used_at = datetime('now', 'localtime')
                    WHERE id = ?
                """, (auth_config_id,))
                sqlite_db.connection.commit()
                cursor.close()
                print(f"✅ 认证配置使用次数已更新")
            except Exception as e:
                print(f"⚠️ 更新认证配置失败: {e}")
        
        # 方式2：使用旧的认证配置JSON（兼容旧代码）
        elif auth_config:
            # 生成认证名称
            auth_name = data.get('name', f'url_{url_id}').replace('/', '_').replace(':', '_')[:50]
            
            print(f"🔐 检测到认证配置，开始执行登录...")
            auth_result = execute_auth_login(auth_config, auth_name)
            
            if not auth_result['success']:
                return jsonify({
                    'success': False,
                    'error': f'认证登录失败: {auth_result["message"]}'
                }), 500
            
            print(f"✅ 认证登录成功: {auth_name}")
            # 更新数据中的auth_name
            if isinstance(auth_config, str):
                auth_config_dict = json.loads(auth_config)
            else:
                auth_config_dict = auth_config
            
            auth_config_dict['auth_name'] = auth_name
            data['auth_config'] = json.dumps(auth_config_dict, ensure_ascii=False)
        
        success = sqlite_db.update_managed_url(url_id, data)
        
        if success:
            sync_source_registry_after_url_change()
            response_data = {
                'success': True,
                'message': 'URL更新成功'
            }
            
            if auth_result:
                response_data['auth_status'] = {
                    'success': True,
                    'message': '认证配置已保存并登录成功',
                    'cookies_count': len(auth_result.get('cookies', []))
                }
            
            return jsonify(response_data)
        else:
            return jsonify({
                'success': False,
                'error': 'URL更新失败'
            }), 500
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': f'更新URL失败: {str(e)}'
        }), 500

@url_management_bp.route('/urls/<int:url_id>', methods=['DELETE'])
def delete_url(url_id):
    """删除URL"""
    try:
        success = sqlite_db.delete_managed_url(url_id)
        
        if success:
            sync_source_registry_after_url_change()
            return jsonify({
                'success': True,
                'message': 'URL删除成功'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'URL删除失败'
            }), 500
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'删除URL失败: {str(e)}'
        }), 500

@url_management_bp.route('/urls/<int:url_id>/crawl-stats', methods=['POST'])
def update_crawl_stats(url_id):
    """更新URL聚合统计"""
    try:
        data = request.get_json(silent=True) or {}
        success = data.get('success', True)
        
        result = sqlite_db.update_url_crawl_stats(url_id, success)
        
        if result:
            return jsonify({
                'success': True,
                'message': '统计信息更新成功'
            })
        else:
            return jsonify({
                'success': False,
                'error': '统计信息更新失败'
            }), 500
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'更新统计信息失败: {str(e)}'
        }), 500

@url_management_bp.route('/categories', methods=['GET'])
def get_categories():
    """获取所有分类"""
    try:
        # 获取所有URL并提取唯一分类
        urls, _ = sqlite_db.get_managed_urls(1, 10000)
        categories = list(set(url['category'] for url in urls if url.get('category')))
        
        return jsonify({
            'success': True,
            'categories': sorted(categories)
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取分类失败: {str(e)}'
        }), 500

@url_management_bp.route('/statistics', methods=['GET'])
def get_url_statistics():
    """获取URL管理统计信息"""
    try:
        urls, total = sqlite_db.get_managed_urls(1, 10000)
        
        active_count = sum(1 for url in urls if url.get('is_active'))
        auto_crawl_count = sum(1 for url in urls if url.get('auto_crawl'))
        categories = list(set(url['category'] for url in urls if url.get('category')))
        
        # 统计聚合成功率
        total_crawls = sum(url.get('total_crawls', 0) for url in urls)
        success_crawls = sum(url.get('success_crawls', 0) for url in urls)
        success_rate = (success_crawls / total_crawls * 100) if total_crawls > 0 else 0
        
        return jsonify({
            'success': True,
            'statistics': {
                'total_urls': total,
                'active_urls': active_count,
                'inactive_urls': total - active_count,
                'auto_crawl_urls': auto_crawl_count,
                'total_categories': len(categories),
                'total_crawls': total_crawls,
                'success_crawls': success_crawls,
                'success_rate': round(success_rate, 2)
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取统计信息失败: {str(e)}'
        }), 500

