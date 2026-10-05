#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
自动登录模块
提供网站自动登录功能
"""

import asyncio
import json
import os
from datetime import datetime
from utils import get_china_time


def _to_bool(value, default=None):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {'1', 'true', 'yes', 'on', 'y'}:
        return True
    if normalized in {'0', 'false', 'no', 'off', 'n', ''}:
        return False
    return default


def _crawl_proxy_default_enabled():
    return _to_bool(os.getenv('CRAWL_USE_PROXY_DEFAULT'), False)


class AutoLogin:
    """自动登录类"""
    
    def __init__(self):
        """初始化"""
        self.storage_dir = "auth_storage"
        self.screenshot_path = os.path.join(self.storage_dir, "screenshots")
        os.makedirs(self.storage_dir, exist_ok=True)
        os.makedirs(self.screenshot_path, exist_ok=True)

    def _storage_file(self, file_name):
        """Resolve a file inside auth_storage and reject path traversal."""
        base_dir = os.path.abspath(self.storage_dir)
        candidate = os.path.abspath(os.path.join(base_dir, file_name))
        if os.path.commonpath([base_dir, candidate]) != base_dir:
            raise ValueError("Invalid auth storage file name")
        return candidate
    
    async def login_and_save(self, auth_config, auth_name, proxy_enabled=None):
        """
        执行登录并保存认证信息
        
        Args:
            auth_config: 认证配置
            auth_name: 认证名称
            
        Returns:
            dict: 登录结果
        """
        try:
            from playwright.async_api import async_playwright
            
            print(f"🚀 开始自动登录: {auth_config.get('login_url')}")
            print(f"   用户名: {auth_config.get('username')}")
            
            async with async_playwright() as p:
                # 启动浏览器（无头模式，适配服务器环境）
                # 代理配置：优先从auth_config读取，其次从全局config读取
                import config
                
                proxy_config = None
                task_proxy_enabled = _to_bool(proxy_enabled, None)
                auth_use_proxy_present = 'use_proxy' in auth_config
                auth_use_proxy = _to_bool(auth_config.get('use_proxy'), False)
                if task_proxy_enabled is False:
                    print(f"🌐 任务已关闭代理，自动登录直连")
                elif auth_use_proxy and auth_config.get('proxy_server'):
                    proxy_config = {"server": auth_config['proxy_server']}
                    print(f"🌐 使用认证配置的代理: {auth_config['proxy_server']}")
                elif auth_use_proxy_present and not auth_use_proxy:
                    print(f"🌐 认证配置已关闭代理，自动登录直连")
                else:
                    # 从聚合默认代理开关读取，避免“代理已配置”被误当成“任务必须用代理”。
                    default_proxy_enabled = (
                        task_proxy_enabled
                        if task_proxy_enabled is not None
                        else _crawl_proxy_default_enabled()
                    )
                    proxy_config = config.get_playwright_proxy(enabled=default_proxy_enabled)
                    if proxy_config:
                        print(f"🌐 使用聚合默认代理: {proxy_config['server']}")
                    else:
                        print(f"🌐 不使用代理（直连）")
                
                browser = await p.chromium.launch(
                    headless=True,  # 服务器环境必须使用无头模式
                    proxy=proxy_config,
                    args=[
                        '--disable-blink-features=AutomationControlled',
                        '--disable-dev-shm-usage',
                        '--no-sandbox'
                    ]
                )
                
                # 创建浏览器上下文
                context = await browser.new_context(
                    viewport={'width': 1920, 'height': 1080},
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                )
                
                page = await context.new_page()
                
                try:
                    screenshots = []  # 存储截图信息
                    
                    # 1. 访问登录页面
                    print(f"📄 正在访问登录页面...")
                    await page.goto(auth_config['login_url'], wait_until='domcontentloaded', timeout=60000)  # 增加到60秒
                    await page.wait_for_timeout(3000)  # 等待页面稳定和动态内容加载
                    
                    # 截图1：登录页面（可选，超时则跳过）
                    try:
                        screenshot_1 = os.path.join(self.screenshot_path, f"{auth_name}_01_login_page.png")
                        await page.screenshot(path=screenshot_1, full_page=True, timeout=10000)
                        screenshots.append({'step': '登录页面', 'file': os.path.basename(screenshot_1)})
                        print(f"📸 已保存截图: 登录页面")
                    except Exception as e:
                        print(f"⚠️ 截图跳过: {str(e)[:50]}")
                    
                    # 1.5 尝试切换到密码登录模式（如果有多种登录方式）
                    print(f"🔄 检查是否需要切换登录方式...")
                    # 常见的切换按钮文本
                    switch_keywords = ['账号登录', '密码登录', '账户登录', 'password', 'account', '账密登录']
                    
                    # 查找所有可点击的元素（包括所有可能的标签）
                    clickable_elements = await page.query_selector_all('a, span, div, button, p, label')
                    
                    print(f"   共找到 {len(clickable_elements)} 个可点击元素")
                    
                    switched = False
                    for i, elem in enumerate(clickable_elements):
                        try:
                            text = await elem.inner_text()
                            text = text.strip() if text else ''
                            
                            # 检查是否包含切换关键词（精确匹配）
                            for keyword in switch_keywords:
                                if keyword == text or (keyword in text and len(text) < 20):
                                    is_visible = await elem.is_visible()
                                    print(f"   元素 {i}: 文本='{text}', 可见={is_visible}")
                                    
                                    if is_visible:
                                        print(f"   🎯 找到切换按钮: '{text}'")
                                        await elem.click()
                                        await page.wait_for_timeout(1500)
                                        print(f"   ✅ 已点击切换按钮")
                                        
                                        # 截图：切换后（可选，超时则跳过）
                                        try:
                                            screenshot_1_5 = os.path.join(self.screenshot_path, f"{auth_name}_01.5_after_switch.png")
                                            await page.screenshot(path=screenshot_1_5, full_page=True, timeout=10000)
                                            screenshots.append({'step': '切换登录方式', 'file': os.path.basename(screenshot_1_5)})
                                            print(f"📸 已保存截图: 切换登录方式")
                                        except Exception as e:
                                            print(f"⚠️ 截图跳过: {str(e)[:50]}")
                                        switched = True
                                        break
                        except Exception as e:
                            continue
                        
                        if switched:
                            break
                    
                    if not switched:
                        print(f"   ⚠️ 未找到切换按钮，可能已经在密码登录模式")
                    
                    # 2. 填写用户名
                    print(f"✍️  正在填写用户名...")
                    username_selector = auth_config['username_selector']
                    
                    # 等待2秒让页面完全切换
                    await page.wait_for_timeout(2000)
                    
                    # 🔧 智能查找用户名输入框（尝试多种选择器）
                    username_selectors = [
                        username_selector,  # 用户配置的选择器
                        'input[name="login_email"]',
                        'input[id="login_email"]',
                        'input[name="username"]',
                        'input[name="email"]',
                        'input[type="email"]',
                        'input[type="text"]',
                    ]
                    
                    username_input = None
                    for selector in username_selectors:
                        try:
                            username_inputs = await page.query_selector_all(selector)
                            print(f"   尝试选择器 '{selector}': 找到 {len(username_inputs)} 个")
                            
                            for i, inp in enumerate(username_inputs):
                                is_visible = await inp.is_visible()
                                if is_visible:
                                    # 🔥 排除submit/button/hidden等不可填写的input类型
                                    input_type = await inp.get_attribute('type') or 'text'
                                    if input_type.lower() in ['submit', 'button', 'hidden', 'checkbox', 'radio', 'file', 'image', 'reset']:
                                        continue
                                    username_input = inp
                                    print(f"   ✅ 找到可见的用户名框: {selector} (type={input_type})")
                                    break
                            
                            if username_input:
                                break
                        except:
                            continue
                    
                    if username_input:
                        # 先点击激活输入框
                        await username_input.click()
                        await page.wait_for_timeout(300)
                        await username_input.fill(auth_config['username'])
                        await page.wait_for_timeout(500)
                        print(f"   ✅ 用户名已填写")
                    else:
                        raise Exception(f"未找到可用的用户名输入框，尝试了 {len(username_selectors)} 个选择器")
                    
                    # 3. 填写密码
                    print(f"🔑 正在填写密码...")
                    password_selector = auth_config['password_selector']
                    
                    # 🔧 智能查找密码输入框（尝试多种选择器）
                    password_selectors = [
                        password_selector,  # 用户配置的选择器
                        'input[name="password"]',  # 最常见
                        'input[id="login_password"]',
                        'input[type="password"]',
                    ]
                    
                    password_input = None
                    for selector in password_selectors:
                        try:
                            password_inputs = await page.query_selector_all(selector)
                            print(f"   尝试选择器 '{selector}': 找到 {len(password_inputs)} 个")
                            
                            for i, inp in enumerate(password_inputs):
                                is_visible = await inp.is_visible()
                                if is_visible:
                                    password_input = inp
                                    print(f"   ✅ 找到可见的密码框: {selector}")
                                    break
                            
                            if password_input:
                                break
                        except:
                            continue
                    
                    if password_input:
                        # 🔥 等待页面稳定，然后重新获取密码框（避免DOM脱离问题）
                        await page.wait_for_timeout(500)
                        
                        # 重新查找密码框（使用多种选择器）
                        recheck_selectors = [
                            'input[type="password"]:visible',
                            'input[type="password"]',
                            'input[name="password"]',
                            'input[id="login_password"]',
                            'input#password',
                        ]
                        
                        password_input = None
                        for sel in recheck_selectors:
                            try:
                                password_input = await page.query_selector(sel)
                                if password_input:
                                    is_visible = await password_input.is_visible()
                                    if is_visible:
                                        print(f"   🔄 重新找到密码框: {sel}")
                                        break
                                    password_input = None
                            except:
                                continue
                        
                        # 最后备选：查找第二个可见的input框（假设第一个是用户名，第二个是密码）
                        if not password_input:
                            all_inputs = await page.query_selector_all('input:visible')
                            for i, inp in enumerate(all_inputs):
                                inp_type = await inp.get_attribute('type') or 'text'
                                if inp_type.lower() not in ['submit', 'button', 'hidden', 'checkbox', 'radio']:
                                    if i >= 1:  # 跳过第一个（用户名框）
                                        password_input = inp
                                        print(f"   🔄 使用第{i+1}个可见input作为密码框")
                                        break
                        
                        if password_input:
                            await password_input.click()
                            await page.wait_for_timeout(200)
                            await password_input.fill(auth_config['password'])
                            print(f"   ✅ 密码已填写")
                        else:
                            raise Exception("重新查找密码框失败")
                    else:
                        raise Exception(f"未找到可用的密码输入框，尝试了 {len(password_selectors)} 个选择器")
                    
                    await page.wait_for_timeout(500)
                    
                    # 截图2：填写完表单（可选，超时则跳过）
                    try:
                        screenshot_2 = os.path.join(self.screenshot_path, f"{auth_name}_02_form_filled.png")
                        await page.screenshot(path=screenshot_2, full_page=True, timeout=10000)
                        screenshots.append({'step': '表单已填写', 'file': os.path.basename(screenshot_2)})
                        print(f"📸 已保存截图: 表单已填写")
                    except Exception as e:
                        print(f"⚠️ 截图跳过: {str(e)[:50]}")
                    
                    # 4. 点击登录按钮
                    print(f"🖱️  正在点击登录按钮...")
                    submit_selector = auth_config.get('submit_selector', '')
                    print(f"   配置的提交按钮选择器: '{submit_selector}'")
                    
                    # 🔥 使用多种选择器查找提交按钮
                    submit_selectors = [
                        submit_selector,  # 用户配置的选择器
                        'input[type="submit"]',
                        'button[type="submit"]',
                        'input[value*="登"]',  # 登录、登入
                        'input[value*="Login"]',
                        'input[value*="login"]',
                        'button:has-text("登录")',
                        'button:has-text("登入")',
                        'button:has-text("Login")',
                        '.login-btn',
                        '#login-btn',
                        '#loginBtn',
                        'input.submit',
                        'button.submit',
                    ]
                    
                    submit_button = None
                    for selector in submit_selectors:
                        if not selector:
                            continue
                        try:
                            buttons = await page.query_selector_all(selector)
                            print(f"   尝试选择器 '{selector}': 找到 {len(buttons)} 个")
                            
                            for btn in buttons:
                                is_visible = await btn.is_visible()
                                if is_visible:
                                    submit_button = btn
                                    print(f"   ✅ 找到可见的提交按钮: {selector}")
                                    break
                            
                            if submit_button:
                                break
                        except:
                            continue
                    
                    if submit_button:
                        await submit_button.click()
                        print(f"   ✅ 提交按钮已点击")
                    else:
                        raise Exception("未找到可见的提交按钮")
                    
                    # 5. 等待登录完成
                    wait_time = auth_config.get('wait_after_submit', 5)
                    print(f"⏳ 等待登录完成 ({wait_time}秒)...")
                    await page.wait_for_timeout(wait_time * 1000)
                    
                    # 截图3：登录后页面（可选，超时则跳过）
                    try:
                        screenshot_3 = os.path.join(self.screenshot_path, f"{auth_name}_03_after_login.png")
                        await page.screenshot(path=screenshot_3, full_page=True, timeout=10000)
                        screenshots.append({'step': '登录后页面', 'file': os.path.basename(screenshot_3)})
                        print(f"📸 已保存截图: 登录后页面")
                    except Exception as e:
                        print(f"⚠️ 截图跳过: {e}")
                    
                    # 6. 检查登录是否成功
                    current_url = page.url
                    print(f"🌐 当前URL: {current_url}")
                    
                    success_indicator = auth_config.get('success_indicator', {})
                    is_login_success = True
                    
                    if success_indicator:
                        indicator_type = success_indicator.get('type')
                        indicator_value = success_indicator.get('value')
                        
                        if indicator_type == 'url_contains' and indicator_value:
                            is_login_success = indicator_value in current_url
                            print(f"✅ URL检查: {'通过' if is_login_success else '失败'} (期望包含: {indicator_value})")
                        elif indicator_type == 'element_exists' and indicator_value:
                            try:
                                await page.wait_for_selector(indicator_value, timeout=3000)
                                is_login_success = True
                                print(f"✅ 元素检查: 通过 (找到: {indicator_value})")
                            except:
                                is_login_success = False
                                print(f"❌ 元素检查: 失败 (未找到: {indicator_value})")
                    else:
                        # 简单检查：页面是否还包含"登录"字样
                        page_content = await page.content()
                        is_login_success = '登录' not in page_content[:1000]
                    
                    if not is_login_success:
                        await browser.close()
                        return {
                            'success': False,
                            'message': '登录失败：未检测到成功标识',
                            'current_url': current_url
                        }
                    
                    # 7. 获取Cookie
                    cookies = await context.cookies()
                    print(f"🍪 成功获取 {len(cookies)} 个Cookie")
                    
                    # 8. 保存认证状态
                    storage_file = self._storage_file(f"{auth_name}.json")
                    storage_state = await context.storage_state(path=storage_file)
                    
                    # 9. 保存认证信息（不含密码）
                    auth_info = {
                        'login_url': auth_config['login_url'],
                        'username': auth_config['username'],
                        'username_selector': auth_config['username_selector'],
                        'password_selector': auth_config['password_selector'],
                        'submit_selector': auth_config['submit_selector'],
                        'wait_after_submit': auth_config.get('wait_after_submit', 5),
                        'success_indicator': auth_config.get('success_indicator', {}),
                        'created_at': get_china_time().isoformat(),
                        'cookies_count': len(cookies)
                    }
                    
                    info_file = self._storage_file(f"{auth_name}_info.json")
                    with open(info_file, 'w', encoding='utf-8') as f:
                        json.dump(auth_info, f, ensure_ascii=False, indent=2)
                    
                    await browser.close()
                    
                    print(f"✅ 登录成功！")
                    
                    return {
                        'success': True,
                        'message': '登录成功',
                        'auth_name': auth_name,
                        'cookies': cookies,
                        'cookies_count': len(cookies),
                        'storage_state': storage_file,
                        'current_url': current_url,
                        'timestamp': get_china_time().isoformat(),
                        'auth_info': auth_info,
                        'screenshots': screenshots
                    }
                    
                except Exception as e:
                    print(f"❌ 登录过程出错: {e}")
                    await browser.close()
                    import traceback
                    traceback.print_exc()
                    
                    return {
                        'success': False,
                        'message': f'登录失败: {str(e)}'
                    }
            
        except Exception as e:
            print(f"❌ 初始化失败: {e}")
            import traceback
            traceback.print_exc()
            
            return {
                'success': False,
                'message': f'登录失败: {str(e)}'
            }
    
    def get_auth_info(self, auth_name):
        """
        获取认证信息
        
        Args:
            auth_name: 认证名称
            
        Returns:
            dict: 认证信息
        """
        try:
            auth_file = self._storage_file(f"{auth_name}.json")
            if os.path.exists(auth_file):
                with open(auth_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return None
        except Exception as e:
            print(f"获取认证信息失败: {e}")
            return None
    
    def delete_auth(self, auth_name):
        """
        删除认证信息
        
        Args:
            auth_name: 认证名称
            
        Returns:
            bool: 是否成功
        """
        try:
            auth_file = self._storage_file(f"{auth_name}.json")
            if os.path.exists(auth_file):
                os.remove(auth_file)
            
            info_file = self._storage_file(f"{auth_name}_info.json")
            if os.path.exists(info_file):
                os.remove(info_file)
                
            return True
        except Exception as e:
            print(f"删除认证信息失败: {e}")
            return False
    
    async def create_authenticated_context(self, auth_name, playwright_instance, proxy_enabled=None):
        """
        创建带认证状态的浏览器上下文
        
        Args:
            auth_name: 认证名称
            playwright_instance: Playwright实例
            proxy_enabled: 是否启用代理；None 时使用聚合默认代理开关
            
        Returns:
            BrowserContext: 浏览器上下文，如果认证不存在则返回None
        """
        try:
            storage_file = self._storage_file(f"{auth_name}.json")
            
            if not os.path.exists(storage_file):
                print(f"❌ 认证状态文件不存在: {storage_file}")
                return None
            
            # 启动浏览器，并允许单个聚合任务覆盖代理开关
            import config
            
            proxy_intent = _to_bool(proxy_enabled, None)
            if proxy_intent is None:
                proxy_intent = _crawl_proxy_default_enabled()
            proxy_config = config.get_playwright_proxy(enabled=proxy_intent)
            if proxy_config:
                print(f"🌐 认证聚合使用代理: {proxy_config.get('server')}")
            else:
                print("🌐 认证聚合直连")
            
            browser = await playwright_instance.chromium.launch(
                headless=True,
                proxy=proxy_config,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox'
                ]
            )
            
            # 使用保存的认证状态创建上下文
            context = await browser.new_context(
                storage_state=storage_file,
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            
            print(f"✅ 已创建带认证的浏览器上下文: {auth_name}")
            return context
            
        except Exception as e:
            print(f"❌ 创建认证上下文失败: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    async def login_with_retry(self, auth_config, auth_name, max_retries=3, retry_delays=None, proxy_enabled=None):
        """
        带重试机制的登录
        
        Args:
            auth_config: 认证配置
            auth_name: 认证名称
            max_retries: 最大重试次数
            retry_delays: 重试延迟列表（秒）
            
        Returns:
            dict: 登录结果，包含重试信息
        """
        if retry_delays is None:
            retry_delays = [5, 10, 30]
        
        retry_history = []
        
        for attempt in range(max_retries):
            print(f"🔄 登录尝试 {attempt + 1}/{max_retries}")
            
            result = await self.login_and_save(auth_config, auth_name, proxy_enabled=proxy_enabled)
            
            retry_history.append({
                'attempt': attempt + 1,
                'success': result.get('success'),
                'message': result.get('message'),
                'timestamp': get_china_time().isoformat()
            })
            
            if result.get('success'):
                result['retry_count'] = attempt
                result['retry_history'] = retry_history
                return result
            
            # 如果不是最后一次尝试，等待后重试
            if attempt < max_retries - 1:
                delay = retry_delays[attempt] if attempt < len(retry_delays) else retry_delays[-1]
                print(f"⏳ 等待 {delay} 秒后重试...")
                await asyncio.sleep(delay)
        
        # 所有尝试都失败
        return {
            'success': False,
            'message': f'登录失败：已重试 {max_retries} 次',
            'retry_count': max_retries,
            'retry_history': retry_history
        }

