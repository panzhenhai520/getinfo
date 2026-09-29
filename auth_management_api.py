#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
认证管理API
提供认证配置的增删查改接口
"""

from flask import Blueprint, request, jsonify
from auto_login import AutoLogin
import asyncio
import json
import os
from functools import wraps

# 创建蓝图
auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')

# 创建AutoLogin实例
auto_login = AutoLogin()

def async_route(f):
    """装饰器：让Flask路由支持异步函数"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))
    return decorated_function


def _json_data():
    """Return JSON request data without raising on non-JSON or malformed bodies."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _safe_auth_file_path(path):
    """Resolve auth-storage file paths and keep deletion inside auth_storage."""
    if not path:
        return None
    base_dir = os.path.abspath(auto_login.storage_dir)
    candidate = path
    if not os.path.isabs(candidate):
        candidate = os.path.join(os.getcwd(), candidate)
    candidate = os.path.abspath(candidate)
    try:
        if os.path.commonpath([base_dir, candidate]) != base_dir:
            return None
    except ValueError:
        return None
    return candidate


def _delete_auth_storage_files(storage_file=None):
    """Delete a saved auth state and its info file, if both are in auth_storage."""
    deleted = []
    candidates = []
    safe_storage = _safe_auth_file_path(storage_file)
    if safe_storage:
        candidates.append(safe_storage)
        root, ext = os.path.splitext(safe_storage)
        candidates.append(f"{root}_info{ext or '.json'}")

    for candidate in candidates:
        safe_candidate = _safe_auth_file_path(candidate)
        if safe_candidate and os.path.exists(safe_candidate):
            os.remove(safe_candidate)
            deleted.append(os.path.basename(safe_candidate))
    return deleted


def _coerce_id_list(value):
    if value in (None, ''):
        return []
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    result = []
    for item in value:
        try:
            parsed = int(float(item))
        except (TypeError, ValueError):
            continue
        if parsed > 0 and parsed not in result:
            result.append(parsed)
    return result


@auth_bp.route('/configs', methods=['GET'])
def list_auth_configs():
    """
    获取所有认证配置列表（从数据库）
    
    Returns:
        JSON: {
            'success': bool,
            'configs': [
                {
                    'id': int,
                    'name': str,
                    'login_url': str,
                    'description': str,
                    'is_active': bool,
                    'use_count': int,
                    'created_at': str,
                    'last_used_at': str
                }
            ]
        }
    """
    try:
        from sqlite_database import sqlite_db
        
        cursor = sqlite_db.connection.cursor()
        cursor.execute("""
            SELECT id, name, login_url, description, is_active, 
                   use_count, created_at, last_used_at, cookies_count
            FROM auth_configs
            ORDER BY created_at DESC
        """)
        
        configs = []
        for row in cursor.fetchall():
            configs.append({
                'id': row['id'],
                'name': row['name'],
                'login_url': row['login_url'],
                'description': row['description'],
                'is_active': bool(row['is_active']),
                'use_count': row['use_count'] or 0,
                'created_at': row['created_at'],
                'last_used_at': row['last_used_at'],
                'cookies_count': row['cookies_count'] or 0
            })
        
        cursor.close()
        
        return jsonify({
            'success': True,
            'configs': configs,
            'total': len(configs)
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取认证配置失败: {str(e)}'
        }), 500


@auth_bp.route('/configs', methods=['POST'])
@async_route
async def create_auth_config():
    """
    创建新的认证配置
    
    Request Body:
        {
            'name': str,
            'login_url': str,
            'username': str,
            'password': str,
            'username_selector': str,
            'password_selector': str,
            'submit_selector': str,
            'wait_after_submit': int,
            'success_indicator': dict,
            'description': str
        }
    
    Returns:
        JSON: {'success': bool, 'config_id': int}
    """
    try:
        from sqlite_database import sqlite_db
        data = _json_data()
        
        # 验证必填字段
        required_fields = ['name', 'login_url', 'username', 'password',
                          'username_selector', 'password_selector', 'submit_selector']
        
        for field in required_fields:
            if not data.get(field):
                return jsonify({
                    'success': False,
                    'error': f'缺少必填字段: {field}'
                }), 400
        
        # 标准化login_url，确保有协议前缀
        import re
        import hashlib
        from urllib.parse import urlparse
        
        login_url = data['login_url'].strip()
        if not login_url.startswith(('http://', 'https://')):
            login_url = 'https://' + login_url
            data['login_url'] = login_url
            print(f"🔧 URL已标准化: {login_url}")
        
        # 清理文件名中的非法字符
        original_name = data['name'].strip()
        
        # 如果名称为空或者名称看起来像URL，则自动生成一个名称
        if not original_name or '/' in original_name or '?' in original_name or '://' in original_name:
            # 从login_url中提取域名作为名称
            login_url = data['login_url']
            
            # 处理没有协议的URL
            if not login_url.startswith(('http://', 'https://')):
                login_url = 'https://' + login_url
            
            try:
                parsed_url = urlparse(login_url)
                domain = parsed_url.netloc or parsed_url.path.split('/')[0]
                # 移除www.前缀
                domain = re.sub(r'^www\.', '', domain)
                original_name = f"auth_{domain}"
            except:
                # 如果解析失败，使用hash
                url_hash = hashlib.md5(data['login_url'].encode()).hexdigest()[:8]
                original_name = f"config_{url_hash}"
        
        # 移除Windows文件名中的非法字符: < > : " / \ | ? *
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', original_name)
        # 移除多个连续的下划线
        safe_name = re.sub(r'_+', '_', safe_name)
        # 移除开头和结尾的下划线和点
        safe_name = safe_name.strip('_.')
        # 限制文件名长度
        safe_name = safe_name[:50] if len(safe_name) > 50 else safe_name
        
        print(f"📝 创建认证配置 - 原始名称: {data['name']}")
        print(f"📝 创建认证配置 - 安全名称: {safe_name}")
        
        # 执行登录测试
        result = await auto_login.login_and_save(data, safe_name)
        
        if not result.get('success'):
            return jsonify({
                'success': False,
                'error': f'登录测试失败: {result.get("message")}'
            }), 400
        
        # 保存到数据库
        cursor = sqlite_db.connection.cursor()
        
        success_indicator = data.get('success_indicator', {})
        
        cursor.execute("""
            INSERT INTO auth_configs 
            (name, login_url, username, password, username_selector, 
             password_selector, submit_selector, wait_after_submit,
             success_indicator_type, success_indicator_value, description,
             storage_file, cookies_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data['name'],
            data['login_url'],
            data['username'],
            data['password'],
            data['username_selector'],
            data['password_selector'],
            data['submit_selector'],
            data.get('wait_after_submit', 5),
            success_indicator.get('type'),
            success_indicator.get('value'),
            data.get('description', ''),
            result.get('storage_state', ''),
            len(result.get('cookies', []))
        ))
        
        config_id = cursor.lastrowid
        sqlite_db.connection.commit()
        cursor.close()
        
        return jsonify({
            'success': True,
            'message': '认证配置创建成功',
            'config_id': config_id,
            'cookies_count': len(result.get('cookies', []))
        })
        
    except Exception as e:
        print(f"❌ 创建认证配置失败: {e}")
        import traceback
        traceback.print_exc()
        
        return jsonify({
            'success': False,
            'error': f'创建失败: {str(e)}'
        }), 500


@auth_bp.route('/configs/<int:config_id>', methods=['GET'])
def get_auth_config(config_id):
    """获取单个认证配置详情"""
    try:
        from sqlite_database import sqlite_db
        
        cursor = sqlite_db.connection.cursor()
        cursor.execute("""
            SELECT * FROM auth_configs WHERE id = ?
        """, (config_id,))
        
        row = cursor.fetchone()
        cursor.close()
        
        if not row:
            return jsonify({
                'success': False,
                'error': '认证配置不存在'
            }), 404
        
        config = dict(row)
        # 不返回密码
        config['password'] = '******'
        
        return jsonify({
            'success': True,
            'config': config
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取配置失败: {str(e)}'
        }), 500


@auth_bp.route('/configs/<int:config_id>', methods=['DELETE'])
def delete_auth_config(config_id):
    """删除认证配置"""
    try:
        from sqlite_database import sqlite_db
        
        # 检查是否有URL在使用
        cursor = sqlite_db.connection.cursor()
        cursor.execute("""
            SELECT COUNT(*) as count FROM managed_urls
            WHERE auth_config_id = ?
        """, (config_id,))
        
        count = cursor.fetchone()['count']
        
        if count > 0:
            cursor.close()
            return jsonify({
                'success': False,
                'error': f'无法删除，还有 {count} 个URL正在使用此认证配置'
            }), 400
        
        cursor.execute("SELECT storage_file FROM auth_configs WHERE id = ?", (config_id,))
        row = cursor.fetchone()
        if not row:
            cursor.close()
            return jsonify({
                'success': False,
                'error': '认证配置不存在'
            }), 404

        storage_file = row['storage_file'] if 'storage_file' in row.keys() else None

        # 删除配置
        cursor.execute("DELETE FROM auth_configs WHERE id = ?", (config_id,))
        sqlite_db.connection.commit()
        cursor.close()

        deleted_files = _delete_auth_storage_files(storage_file)
        
        return jsonify({
            'success': True,
            'message': '认证配置已删除',
            'deleted_files': deleted_files
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'删除失败: {str(e)}'
        }), 500


@auth_bp.route('/configs/<name>/old-delete', methods=['DELETE'])
def delete_auth_config_old(name):
    """
    删除认证配置（旧版本，仅删除文件）
    
    Args:
        name: 配置名称
    
    Returns:
        JSON: {'success': bool, 'message': str}
    """
    try:
        success = auto_login.delete_auth(name)
        
        if success:
            return jsonify({
                'success': True,
                'message': '认证配置已删除'
            })
        else:
            return jsonify({
                'success': False,
                'error': '删除认证配置失败'
            }), 500
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'删除失败: {str(e)}'
        }), 500


@auth_bp.route('/configs/<name>/test', methods=['POST'])
@async_route
async def test_auth_config(name):
    """
    测试认证配置是否有效
    
    Args:
        name: 配置名称
    
    Request Body:
        {
            'test_url': '要测试访问的URL'
        }
    
    Returns:
        JSON: {'success': bool, 'message': str, 'is_valid': bool}
    """
    try:
        data = _json_data()
        test_url = data.get('test_url')
        
        if not test_url:
            return jsonify({
                'success': False,
                'error': '测试URL不能为空'
            }), 400
        
        # 导入playwright
        from playwright.async_api import async_playwright
        
        async with async_playwright() as p:
            # 创建认证上下文
            context = await auto_login.create_authenticated_context(name, p)
            
            if not context:
                return jsonify({
                    'success': False,
                    'error': '无法创建认证上下文，配置可能不存在',
                    'is_valid': False
                })
            
            try:
                page = await context.new_page()
                await page.goto(test_url, timeout=30000)
                
                # 检查是否被重定向到登录页
                current_url = page.url
                page_content = await page.content()
                
                # 简单检查：如果页面包含"登录"或"login"，可能认证失效
                is_valid = '登录' not in page_content[:500] and 'login' not in current_url.lower()
                
                await context.close()
                
                return jsonify({
                    'success': True,
                    'message': '认证有效' if is_valid else '认证可能已失效',
                    'is_valid': is_valid,
                    'current_url': current_url
                })
                
            except Exception as e:
                await context.close()
                return jsonify({
                    'success': False,
                    'error': f'测试访问失败: {str(e)}',
                    'is_valid': False
                }), 500
                
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'测试失败: {str(e)}',
            'is_valid': False
        }), 500


@auth_bp.route('/test-login', methods=['POST'])
@async_route
async def test_login_config():
    """
    测试登录配置是否有效（不保存配置，仅测试）
    
    Request Body:
        {
            'name': '配置名称',
            'login_url': '登录页面URL',
            'username': '用户名',
            'password': '密码',
            'username_selector': '用户名选择器',
            'password_selector': '密码选择器',
            'submit_selector': '提交按钮选择器',
            'wait_after_submit': 等待时间（秒）,
            'success_indicator': {'type': 'url_contains', 'value': '成功标识'}
        }
    
    Returns:
        JSON: {
            'success': bool,
            'message': str,
            'cookies_count': int,
            'details': dict
        }
    """
    try:
        config = _json_data()
        
        if not config:
            return jsonify({
                'success': False,
                'error': '缺少配置信息'
            }), 400
        
        # 验证必填字段
        required_fields = ['login_url', 'username', 'password', 
                          'username_selector', 'password_selector', 'submit_selector']
        
        for field in required_fields:
            if not config.get(field):
                return jsonify({
                    'success': False,
                    'error': f'缺少必填字段: {field}'
                }), 400
        
        # 标准化login_url，确保有协议前缀
        import re
        import hashlib
        from urllib.parse import urlparse
        
        login_url = config['login_url'].strip()
        if not login_url.startswith(('http://', 'https://')):
            login_url = 'https://' + login_url
            config['login_url'] = login_url
            print(f"🔧 测试登录 - URL已标准化: {login_url}")
        
        # 执行测试登录（使用临时名称，不保存，启用截图）
        # 清理文件名中的非法字符
        
        # 获取原始名称
        original_name = config.get('name', '').strip()
        
        # 如果名称为空或者名称看起来像URL，则自动生成一个名称
        if not original_name or '/' in original_name or '?' in original_name or '://' in original_name:
            # 从login_url中提取域名作为名称
            login_url = config['login_url']
            
            # 处理没有协议的URL
            if not login_url.startswith(('http://', 'https://')):
                login_url = 'https://' + login_url
            
            try:
                parsed_url = urlparse(login_url)
                domain = parsed_url.netloc or parsed_url.path.split('/')[0]
                # 移除www.前缀
                domain = re.sub(r'^www\.', '', domain)
                original_name = f"auth_{domain}"
            except:
                # 如果解析失败，使用hash
                url_hash = hashlib.md5(config['login_url'].encode()).hexdigest()[:8]
                original_name = f"config_{url_hash}"
        
        # 移除Windows文件名中的非法字符: < > : " / \ | ? *
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', original_name)
        # 移除多个连续的下划线
        safe_name = re.sub(r'_+', '_', safe_name)
        # 移除开头和结尾的下划线和点
        safe_name = safe_name.strip('_.')
        # 限制文件名长度
        safe_name = safe_name[:50] if len(safe_name) > 50 else safe_name
        # 确保名称不为空
        if not safe_name:
            safe_name = 'temp'
        
        test_name = f"_test_{safe_name}"
        
        print(f"📝 原始名称: {config.get('name', '(空)')}")
        print(f"📝 安全名称: {safe_name}")
        print(f"📝 测试名称: {test_name}")
        
        print(f"🧪 开始测试登录配置...")
        print(f"   登录URL: {config['login_url']}")
        print(f"   用户名: {config['username']}")
        
        result = await auto_login.login_and_save(config, test_name)
        
        # 测试完成后删除临时文件
        _delete_auth_storage_files(os.path.join(auto_login.storage_dir, f"{test_name}.json"))
        
        if result.get('success'):
            response_data = {
                'success': True,
                'message': result.get('message', '登录测试成功'),
                'cookies_count': len(result.get('cookies', [])),
                'details': {
                    'login_url': config['login_url'],
                    'tested_at': result.get('auth_info', {}).get('created_at')
                }
            }
            
            # 如果有截图，添加到响应
            if result.get('screenshots'):
                response_data['screenshots'] = result['screenshots']
                print(f"📸 返回 {len(result['screenshots'])} 张截图信息")
            
            return jsonify(response_data)
        else:
            return jsonify({
                'success': False,
                'message': result.get('message', '登录测试失败'),
                'error': result.get('message', '未知错误'),
                'details': {
                    'login_url': config['login_url']
                }
            })
            
    except Exception as e:
        print(f"❌ 测试登录异常: {e}")
        import traceback
        traceback.print_exc()
        
        return jsonify({
            'success': False,
            'error': f'测试登录失败: {str(e)}',
            'message': f'发生错误: {str(e)}'
        }), 500


@auth_bp.route('/analyze-login-page', methods=['POST'])
@async_route
async def analyze_login_page():
    """
    智能分析登录页面，自动推荐选择器
    
    Request Body:
        {
            'login_url': str
        }
    
    Returns:
        JSON: {
            'success': bool,
            'suggestions': {
                'username': [str],
                'password': [str],
                'submit': [str]
            },
            'recommended': {
                'username': str,
                'password': str,
                'submit': str
            }
        }
    """
    try:
        data = _json_data()
        login_url = data.get('login_url')
        
        if not login_url:
            return jsonify({
                'success': False,
                'error': '请提供登录URL'
            }), 400
        
        from playwright.async_api import async_playwright
        
        print(f"🔍 开始分析登录页面: {login_url}")
        
        async with async_playwright() as p:
            # 从全局配置读取代理
            import config
            proxy_config = config.get_playwright_proxy()
            
            browser = await p.chromium.launch(
                headless=True,
                proxy=proxy_config,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox'
                ]
            )
            page = await browser.new_page()
            
            try:
                # 访问登录页面
                await page.goto(login_url, wait_until='networkidle', timeout=30000)
                await page.wait_for_timeout(2000)
                
                # 分析页面元素
                username_suggestions = []
                password_suggestions = []
                submit_suggestions = []
                
                # 查找用户名输入框
                username_inputs = await page.query_selector_all('input[type="text"], input[type="email"], input:not([type="password"]):not([type="hidden"]):not([type="submit"])')
                for input_elem in username_inputs[:5]:  # 最多检查5个
                    # 获取属性
                    name_attr = await input_elem.get_attribute('name')
                    id_attr = await input_elem.get_attribute('id')
                    placeholder = await input_elem.get_attribute('placeholder')
                    
                    # 根据属性生成选择器
                    if name_attr:
                        username_suggestions.append(f'input[name="{name_attr}"]')
                    if id_attr:
                        username_suggestions.append(f'#{id_attr}')
                    
                    # 根据placeholder推测
                    if placeholder:
                        placeholder_lower = placeholder.lower()
                        if any(keyword in placeholder_lower for keyword in ['user', 'name', 'email', '用户', '账号', '邮箱']):
                            if name_attr:
                                username_suggestions.insert(0, f'input[name="{name_attr}"]')
                
                # 查找密码输入框
                password_inputs = await page.query_selector_all('input[type="password"]')
                for input_elem in password_inputs[:3]:
                    name_attr = await input_elem.get_attribute('name')
                    id_attr = await input_elem.get_attribute('id')
                    
                    if name_attr:
                        password_suggestions.append(f'input[name="{name_attr}"]')
                    if id_attr:
                        password_suggestions.append(f'#{id_attr}')
                
                # 查找提交按钮
                submit_buttons = await page.query_selector_all('button[type="submit"], input[type="submit"], button')
                for btn in submit_buttons[:5]:
                    btn_type = await btn.get_attribute('type')
                    btn_id = await btn.get_attribute('id')
                    btn_class = await btn.get_attribute('class')
                    btn_text = await btn.inner_text() if await btn.inner_text() else ''
                    
                    if btn_type == 'submit':
                        submit_suggestions.insert(0, 'button[type="submit"]')
                        break
                    elif any(keyword in btn_text.lower() for keyword in ['登录', 'login', 'sign in', '提交']):
                        if btn_id:
                            submit_suggestions.insert(0, f'#{btn_id}')
                        elif btn_class:
                            first_class = btn_class.split()[0] if btn_class.split() else ''
                            if first_class:
                                submit_suggestions.insert(0, f'button.{first_class}')
                
                await browser.close()
                
                # 去重
                username_suggestions = list(dict.fromkeys(username_suggestions))
                password_suggestions = list(dict.fromkeys(password_suggestions))
                submit_suggestions = list(dict.fromkeys(submit_suggestions))
                
                # 如果没找到，使用默认值
                if not password_suggestions:
                    password_suggestions = ['input[type="password"]']
                if not submit_suggestions:
                    submit_suggestions = ['button[type="submit"]', 'button']
                
                # 推荐最佳选择器（第一个）
                recommended = {
                    'username': username_suggestions[0] if username_suggestions else 'input[type="text"]',
                    'password': password_suggestions[0] if password_suggestions else 'input[type="password"]',
                    'submit': submit_suggestions[0] if submit_suggestions else 'button[type="submit"]'
                }
                
                print(f"✅ 分析完成！推荐选择器:")
                print(f"   用户名: {recommended['username']}")
                print(f"   密码: {recommended['password']}")
                print(f"   按钮: {recommended['submit']}")
                
                return jsonify({
                    'success': True,
                    'suggestions': {
                        'username': username_suggestions,
                        'password': password_suggestions,
                        'submit': submit_suggestions
                    },
                    'recommended': recommended,
                    'message': '页面分析完成'
                })
                
            except Exception as e:
                await browser.close()
                print(f"❌ 分析失败: {e}")
                
                return jsonify({
                    'success': False,
                    'error': f'页面分析失败: {str(e)}',
                    'suggestions': {
                        'username': ['input[name="username"]', 'input[type="text"]', '#username'],
                        'password': ['input[name="password"]', 'input[type="password"]', '#password'],
                        'submit': ['button[type="submit"]', 'button', '#submit']
                    },
                    'recommended': {
                        'username': 'input[name="username"]',
                        'password': 'input[type="password"]',
                        'submit': 'button[type="submit"]'
                    },
                    'message': '分析失败，已提供默认推荐'
                })
                
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'分析失败: {str(e)}'
        }), 500


@auth_bp.route('/selector-helper', methods=['POST'])
def get_selector_suggestions():
    """
    获取选择器建议（基于常见模式）
    
    Request Body:
        {
            'field_type': 'username' | 'password' | 'submit'
        }
    
    Returns:
        JSON: {'success': bool, 'suggestions': [str]}
    """
    try:
        data = _json_data()
        field_type = data.get('field_type', 'username')
        
        suggestions = {
            'username': [
                'input[name="username"]',
                'input[name="email"]',
                'input[type="text"]',
                'input[placeholder*="用户名"]',
                'input[placeholder*="邮箱"]',
                'input[placeholder*="手机"]',
                '#username',
                '#email',
                '.username-input',
                'input[name="account"]'
            ],
            'password': [
                'input[name="password"]',
                'input[type="password"]',
                'input[placeholder*="密码"]',
                '#password',
                '.password-input'
            ],
            'submit': [
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("登录")',
                'button:has-text("Login")',
                'button:has-text("Sign in")',
                '.login-button',
                '.submit-button',
                '#submit',
                'button.primary'
            ]
        }
        
        return jsonify({
            'success': True,
            'suggestions': suggestions.get(field_type, [])
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取建议失败: {str(e)}'
        }), 500


@auth_bp.route('/configs/<int:config_id>/refresh', methods=['POST'])
@async_route
async def refresh_auth_config(config_id):
    """
    刷新认证配置（重新登录）
    
    Args:
        config_id: 配置ID
    
    Returns:
        JSON: {'success': bool, 'message': str, 'retry_count': int}
    """
    try:
        from sqlite_database import sqlite_db
        
        # 获取认证配置
        cursor = sqlite_db.connection.cursor()
        cursor.execute("""
            SELECT * FROM auth_configs WHERE id = ?
        """, (config_id,))
        
        config_row = cursor.fetchone()
        cursor.close()
        
        if not config_row:
            return jsonify({
                'success': False,
                'error': '认证配置不存在'
            }), 404
        
        # 构建登录配置
        login_config = {
            'login_url': config_row['login_url'],
            'username': config_row['username'],
            'password': config_row['password'],
            'username_selector': config_row['username_selector'],
            'password_selector': config_row['password_selector'],
            'submit_selector': config_row['submit_selector'],
            'wait_after_submit': config_row['wait_after_submit'] or 5
        }
        
        if config_row['success_indicator_type']:
            login_config['success_indicator'] = {
                'type': config_row['success_indicator_type'],
                'value': config_row['success_indicator_value']
            }
        
        # 使用重试机制重新登录
        print(f"🔄 开始刷新认证配置: {config_row['name']}")
        result = await auto_login.login_with_retry(
            login_config, 
            config_row['name'],
            max_retries=3,
            retry_delays=[5, 10, 30]
        )
        
        if result['success']:
            # 更新数据库
            cursor = sqlite_db.connection.cursor()
            cursor.execute("""
                UPDATE auth_configs
                SET cookies_count = ?,
                    updated_at = datetime('now', 'localtime')
                WHERE id = ?
            """, (len(result.get('cookies', [])), config_id))
            sqlite_db.connection.commit()
            cursor.close()
            
            # 更新所有使用此配置的URL状态
            cursor = sqlite_db.connection.cursor()
            cursor.execute("""
                UPDATE managed_urls
                SET auth_status = 'valid',
                    auth_last_login = datetime('now', 'localtime'),
                    auth_last_check = datetime('now', 'localtime')
                WHERE auth_config_id = ?
            """, (config_id,))
            sqlite_db.connection.commit()
            cursor.close()
            
            return jsonify({
                'success': True,
                'message': '认证已刷新',
                'cookies_count': len(result.get('cookies', [])),
                'retry_count': result.get('retry_count', 0),
                'retry_history': result.get('retry_history', [])
            })
        else:
            return jsonify({
                'success': False,
                'error': result.get('message', '刷新失败'),
                'retry_count': result.get('retry_count', 0),
                'retry_history': result.get('retry_history', [])
            }), 500
            
    except Exception as e:
        print(f"❌ 刷新认证配置失败: {e}")
        import traceback
        traceback.print_exc()
        
        return jsonify({
            'success': False,
            'error': f'刷新失败: {str(e)}'
        }), 500


@auth_bp.route('/configs/export', methods=['POST'])
def export_auth_configs():
    """
    导出认证配置（JSON格式，密码脱敏）
    
    Request Body:
        {
            'config_ids': [1, 2, 3]  # 可选，不提供则导出全部
        }
    
    Returns:
        JSON文件下载
    """
    try:
        from sqlite_database import sqlite_db
        from flask import make_response
        import json
        from datetime import datetime
        from utils import get_china_time
        
        data = _json_data()
        config_ids = _coerce_id_list(data.get('config_ids'))
        
        cursor = sqlite_db.connection.cursor()
        
        if config_ids:
            placeholders = ','.join('?' * len(config_ids))
            cursor.execute(f"""
                SELECT * FROM auth_configs 
                WHERE id IN ({placeholders})
            """, config_ids)
        else:
            cursor.execute("SELECT * FROM auth_configs")
        
        configs = []
        for row in cursor.fetchall():
            config_dict = dict(row)
            # 脱敏处理
            config_dict['password'] = '******'
            config_dict['id'] = None  # 导出时移除ID，导入时重新生成
            configs.append(config_dict)
        
        cursor.close()
        
        export_data = {
            'export_date': get_china_time().isoformat(),
            'version': '1.0',
            'total': len(configs),
            'configs': configs
        }
        
        # 创建响应
        response = make_response(json.dumps(export_data, ensure_ascii=False, indent=2))
        response.headers['Content-Type'] = 'application/json; charset=utf-8'
        response.headers['Content-Disposition'] = f'attachment; filename=auth_configs_export_{get_china_time().strftime("%Y%m%d_%H%M%S")}.json'
        
        return response
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'导出失败: {str(e)}'
        }), 500


@auth_bp.route('/configs/import', methods=['POST'])
def import_auth_configs():
    """
    导入认证配置
    
    Request Body:
        {
            'configs': [...],
            'overwrite': bool,  # 是否覆盖同名配置
            'fill_password': str  # 统一填充的密码（因为导出时脱敏了）
        }
    
    Returns:
        JSON: {'success': bool, 'imported': int, 'skipped': int, 'errors': []}
    """
    try:
        from sqlite_database import sqlite_db
        
        data = _json_data()
        
        if not data or not data.get('configs'):
            return jsonify({
                'success': False,
                'error': '缺少配置数据'
            }), 400
        
        configs = data['configs']
        if not isinstance(configs, list):
            return jsonify({
                'success': False,
                'error': '配置数据格式错误'
            }), 400
        overwrite = data.get('overwrite', False)
        fill_password = data.get('fill_password', '')
        
        imported = 0
        skipped = 0
        errors = []
        
        for config in configs:
            try:
                # 如果密码是脱敏的，使用统一密码
                if config.get('password') == '******' and fill_password:
                    config['password'] = fill_password
                
                cursor = sqlite_db.connection.cursor()
                
                # 检查是否存在同名配置
                cursor.execute("SELECT id FROM auth_configs WHERE name = ?", (config['name'],))
                existing = cursor.fetchone()
                
                if existing:
                    if overwrite:
                        # 更新现有配置
                        cursor.execute("""
                            UPDATE auth_configs SET
                                login_url = ?, username = ?, password = ?,
                                username_selector = ?, password_selector = ?,
                                submit_selector = ?, wait_after_submit = ?,
                                success_indicator_type = ?, success_indicator_value = ?,
                                description = ?, updated_at = datetime('now', 'localtime')
                            WHERE name = ?
                        """, (
                            config['login_url'], config['username'], config['password'],
                            config['username_selector'], config['password_selector'],
                            config['submit_selector'], config.get('wait_after_submit', 5),
                            config.get('success_indicator_type'), config.get('success_indicator_value'),
                            config.get('description', ''), config['name']
                        ))
                        imported += 1
                    else:
                        skipped += 1
                        cursor.close()
                        continue
                else:
                    # 插入新配置
                    cursor.execute("""
                        INSERT INTO auth_configs 
                        (name, login_url, username, password, username_selector,
                         password_selector, submit_selector, wait_after_submit,
                         success_indicator_type, success_indicator_value, description)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        config['name'], config['login_url'], config['username'], config['password'],
                        config['username_selector'], config['password_selector'],
                        config['submit_selector'], config.get('wait_after_submit', 5),
                        config.get('success_indicator_type'), config.get('success_indicator_value'),
                        config.get('description', '')
                    ))
                    imported += 1
                
                sqlite_db.connection.commit()
                cursor.close()
                
            except Exception as e:
                errors.append(f"配置 {config.get('name', '未知')}: {str(e)}")
        
        return jsonify({
            'success': True,
            'message': f'导入完成',
            'imported': imported,
            'skipped': skipped,
            'errors': errors
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'导入失败: {str(e)}'
        }), 500


@auth_bp.route('/check-status', methods=['POST'])
@async_route
async def check_all_auth_status():
    """
    检查所有需要认证的URL的认证状态
    
    Returns:
        JSON: {
            'success': bool,
            'checked_count': int,
            'valid_count': int,
            'expired_count': int,
            'details': []
        }
    """
    try:
        from sqlite_database import sqlite_db
        from datetime import datetime, timedelta
        
        # 获取所有需要认证的URL
        cursor = sqlite_db.connection.cursor()
        cursor.execute("""
            SELECT id, url, name, auth_name, auth_status, auth_last_check
            FROM managed_urls
            WHERE requires_auth = TRUE
        """)
        
        auth_urls = cursor.fetchall()
        cursor.close()
        
        if not auth_urls:
            return jsonify({
                'success': True,
                'message': '没有需要检查的认证URL',
                'checked_count': 0,
                'valid_count': 0,
                'expired_count': 0
            })
        
        checked_count = 0
        valid_count = 0
        expired_count = 0
        details = []
        
        from playwright.async_api import async_playwright
        
        async with async_playwright() as p:
            for url_row in auth_urls:
                url_id = url_row['id']
                url = url_row['url']
                auth_name = url_row['auth_name']
                
                if not auth_name:
                    continue
                
                # 创建认证上下文
                context = await auto_login.create_authenticated_context(auth_name, p)
                
                if not context:
                    details.append({
                        'url_id': url_id,
                        'url': url,
                        'status': 'expired',
                        'message': '无法创建认证上下文'
                    })
                    expired_count += 1
                    checked_count += 1
                    continue
                
                try:
                    page = await context.new_page()
                    await page.goto(url, timeout=30000)
                    
                    # 简单检查：页面是否包含"登录"
                    page_content = await page.content()
                    is_valid = '登录' not in page_content[:500] and 'login' not in page.url.lower()
                    
                    # 更新数据库
                    update_cursor = sqlite_db.connection.cursor()
                    status = 'valid' if is_valid else 'expired'
                    
                    update_cursor.execute("""
                        UPDATE managed_urls
                        SET auth_status = ?,
                            auth_last_check = datetime('now', 'localtime')
                        WHERE id = ?
                    """, (status, url_id))
                    
                    sqlite_db.connection.commit()
                    update_cursor.close()
                    
                    if is_valid:
                        valid_count += 1
                    else:
                        expired_count += 1
                    
                    details.append({
                        'url_id': url_id,
                        'url': url,
                        'status': status,
                        'message': '认证有效' if is_valid else '认证已失效'
                    })
                    
                    await page.close()
                    
                except Exception as e:
                    details.append({
                        'url_id': url_id,
                        'url': url,
                        'status': 'error',
                        'message': f'检查失败: {str(e)}'
                    })
                    expired_count += 1
                
                finally:
                    await context.close()
                
                checked_count += 1
        
        return jsonify({
            'success': True,
            'message': f'已检查 {checked_count} 个URL',
            'checked_count': checked_count,
            'valid_count': valid_count,
            'expired_count': expired_count,
            'details': details
        })
        
    except Exception as e:
        print(f"❌ 检查认证状态失败: {e}")
        import traceback
        traceback.print_exc()
        
        return jsonify({
            'success': False,
            'error': f'检查失败: {str(e)}'
        }), 500


@auth_bp.route('/screenshots/<filename>')
def get_screenshot(filename):
    """
    获取登录流程截图
    
    Args:
        filename: 截图文件名
    
    Returns:
        图片文件
    """
    try:
        from flask import send_file
        import os
        
        screenshot_path = os.path.join(auto_login.screenshot_path, filename)
        
        if not os.path.exists(screenshot_path):
            return jsonify({
                'success': False,
                'error': '截图不存在'
            }), 404
        
        return send_file(screenshot_path, mimetype='image/png')
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取截图失败: {str(e)}'
        }), 500


# 导出蓝图
def register_auth_api(app):
    """注册认证管理API到Flask应用"""
    app.register_blueprint(auth_bp)
    print("✅ 认证管理API已注册")

