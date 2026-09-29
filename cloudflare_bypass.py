#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Cloudflare反聚合绕过模块
自动检测并处理Cloudflare保护的网站
"""

import asyncio
import os
import requests
from typing import Dict, Optional
from urllib.parse import urlparse
import re
from outbound_url_policy import safe_request_get, validate_outbound_url, install_playwright_url_guard

_APP_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', os.path.join(_APP_BASE_DIR, 'data', 'ms-playwright'))

try:
    from browserforge.headers import HeaderGenerator

    HAS_BROWSERFORGE = True
except ImportError:
    HeaderGenerator = None
    HAS_BROWSERFORGE = False

try:
    from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

    HAS_TENACITY = True
except ImportError:
    Retrying = None
    retry_if_exception_type = None
    stop_after_attempt = None
    wait_exponential_jitter = None
    HAS_TENACITY = False


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


class CloudflareBypass:
    """Cloudflare反聚合绕过器"""
    
    def __init__(self, proxies=None):
        """
        初始化绕过器
        
        Args:
            proxies: 代理配置字典，格式：{'http': 'http://127.0.0.1:10808', 'https': 'http://127.0.0.1:10808'}
        """
        self.playwright_available = False
        self.curl_cffi_available = False
        self.force_direct = isinstance(proxies, dict) and not proxies
        self.proxies = proxies  # 保存代理配置；{} 表示调用方明确要求直连
        self.browserforge_enabled = _env_bool("CRAWL_BROWSERFORGE_HEADERS_ENABLED", True) and HAS_BROWSERFORGE
        self.header_generator = HeaderGenerator() if self.browserforge_enabled else None
        
        if proxies:
            print(f"🔧 已配置代理: {proxies}")
        if self.browserforge_enabled:
            print("✅ browserforge 可用，将生成真实浏览器请求头")
        elif not HAS_BROWSERFORGE:
            print("⚠️ browserforge 未安装，使用内置浏览器请求头")
        
        # 尝试导入 curl_cffi（优先，兼容性更好）
        try:
            from curl_cffi import requests as curl_requests
            self.curl_cffi_available = True
            print("✅ curl-cffi 可用，支持Cloudflare绕过（推荐）")
        except ImportError:
            print("⚠️ curl-cffi 未安装")
        
        # 尝试导入 Playwright（备用，需要较新的GLIBC）
        # 注意：仅检查导入，不实际启动（避免GLIBC错误）
        try:
            from playwright.async_api import async_playwright
            # 仅标记为可用，实际使用时才会知道是否真的能工作
            self.playwright_available = True
            print("✅ Playwright 可用，支持Cloudflare绕过")
        except ImportError:
            print("⚠️ Playwright 未安装")
        except Exception as e:
            # 静默处理其他错误（如GLIBC版本问题）
            print(f"⚠️ Playwright 不可用（系统兼容性问题）")

    def _requests_get(self, url: str, **kwargs):
        """Run requests.get while honoring explicit direct-connection requests."""
        kwargs['proxies'] = self.proxies
        if self.force_direct:
            with requests.Session() as session:
                session.trust_env = False
                return safe_request_get(url, session=session, **kwargs)
        return safe_request_get(url, **kwargs)

    def _browser_headers(self, fallback: Dict = None) -> Dict:
        """Build request headers, preferring browserforge when available."""
        headers = dict(fallback or {})
        if not self.browserforge_enabled or not self.header_generator:
            return headers
        try:
            generated = self.header_generator.generate()
            if generated:
                merged = dict(headers)
                merged.update(generated)
                return merged
        except Exception as exc:
            print(f"⚠️ browserforge生成请求头失败，使用内置请求头: {exc}")
        return headers

    def _run_with_retry(self, func, attempts: int):
        attempts = max(1, int(attempts or 1))
        if not HAS_TENACITY or attempts <= 1:
            return func()
        retryer = Retrying(
            stop=stop_after_attempt(attempts),
            wait=wait_exponential_jitter(initial=0.8, max=5.0),
            retry=retry_if_exception_type((requests.RequestException, TimeoutError, OSError)),
            reraise=True,
        )
        return retryer(func)
    
    def detect_cloudflare(self, url: str, timeout: int = 10) -> bool:
        """
        检测URL是否被Cloudflare保护
        
        Args:
            url: 要检测的URL
            timeout: 超时时间（秒）
            
        Returns:
            bool: 是否被Cloudflare保护
        """
        try:
            headers = self._browser_headers({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            })
            
            response = self._requests_get(url, headers=headers, timeout=timeout, allow_redirects=True, verify=False)
            
            # 检测Cloudflare保护的特征
            cloudflare_indicators = [
                response.status_code == 403,
                'cf-mitigated' in response.headers.get('cf-mitigated', '').lower(),
                'cloudflare' in response.headers.get('server', '').lower(),
                'cf-ray' in str(response.headers).lower(),
                'checking your browser' in response.text.lower(),
                'just a moment' in response.text.lower(),
                '__cf_bm' in response.text.lower()
            ]
            
            is_protected = any(cloudflare_indicators)
            
            if is_protected:
                print(f"🛡️ 检测到Cloudflare保护: {url}")
                print(f"   状态码: {response.status_code}")
                print(f"   Server: {response.headers.get('server', 'N/A')}")
            else:
                print(f"✅ 未检测到Cloudflare保护: {url}")
            
            return is_protected
            
        except Exception as e:
            print(f"⚠️ 检测Cloudflare时出错: {e}")
            return False
    
    def bypass_with_curl_cffi(
        self, 
        url: str, 
        timeout: int = 30,
        max_retries: int = 3
    ) -> Dict:
        """
        使用curl-cffi绕过Cloudflare保护（推荐方法）
        
        Args:
            url: 目标URL
            timeout: 超时时间（秒）
            max_retries: 最大重试次数
            
        Returns:
            Dict: {
                'success': bool,
                'html': str,  # 页面HTML
                'text': str,  # 页面文本
                'url': str,   # 最终URL（可能经过重定向）
                'error': str  # 错误信息（如果失败）
            }
        """
        verdict = validate_outbound_url(url, resolve_dns=True)
        if not verdict.allowed:
            return {'success': False, 'error': f'URL被安全策略阻止: {verdict.reason}', 'url': url}
        url = verdict.normalized_url
        if not self.curl_cffi_available:
            return {
                'success': False,
                'error': 'curl-cffi未安装，无法绕过Cloudflare'
            }
        
        from curl_cffi import requests as curl_requests
        import time
        
        print(f"\n{'='*70}")
        print(f"🚀 使用curl-cffi绕过Cloudflare保护")
        print(f"{'='*70}")
        print(f"目标URL: {url}")
        print(f"超时时间: {timeout} 秒")
        print(f"最大重试: {max_retries} 次\n")
        
        # 尝试不同的浏览器指纹
        impersonate_options = ["chrome120", "chrome116", "safari15_5", "edge101"]
        
        for attempt in range(max_retries):
            try:
                impersonate = impersonate_options[attempt % len(impersonate_options)]
                print(f"🔄 尝试 {attempt + 1}/{max_retries} (使用 {impersonate} 指纹)")
                
                # 创建Session以保持cookies
                session = curl_requests.Session()
                if self.force_direct and hasattr(session, 'trust_env'):
                    session.trust_env = False
                
                # 更完整的headers模拟
                headers = self._browser_headers({
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                    'Accept-Language': 'zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Cache-Control': 'max-age=0',
                    'Sec-Ch-Ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                    'Sec-Ch-Ua-Mobile': '?0',
                    'Sec-Ch-Ua-Platform': '"Windows"',
                    'Sec-Fetch-Dest': 'document',
                    'Sec-Fetch-Mode': 'navigate',
                    'Sec-Fetch-Site': 'none',
                    'Sec-Fetch-User': '?1',
                    'Upgrade-Insecure-Requests': '1',
                })
                
                # 第一次请求（支持代理）
                response = session.get(
                    url,
                    impersonate=impersonate,
                    timeout=timeout,
                    allow_redirects=True,
                    headers=headers,
                    proxies=self.proxies,  # 添加代理支持
                    verify=False  # 使用代理时不验证SSL
                )
                
                html_content = response.text
                final_url = str(response.url)
                
                # 检查是否遇到Cloudflare验证页面
                if 'checking your browser' in html_content.lower() or \
                   'just a moment' in html_content.lower() or \
                   '__cf_bm' in html_content.lower():
                    
                    print(f"   检测到Cloudflare验证页面，等待3秒后重试...")
                    time.sleep(3)
                    
                    # 使用相同的session再次请求（保持cookies，支持代理）
                    response = session.get(
                        final_url if final_url else url,
                        impersonate=impersonate,
                        timeout=timeout,
                        allow_redirects=True,
                        headers=headers,
                        proxies=self.proxies,  # 添加代理支持
                        verify=False  # 使用代理时不验证SSL
                    )
                    
                    html_content = response.text
                    final_url = str(response.url)
                
                # 再次检查
                if 'checking your browser' in html_content.lower() or \
                   'just a moment' in html_content.lower():
                    
                    if attempt < max_retries - 1:
                        print(f"   ⚠️ 验证未完成，尝试下一个浏览器指纹...")
                        time.sleep(2)
                        continue
                    else:
                        print(f"   ❌ 所有尝试均失败")
                        return {
                            'success': False,
                            'error': 'Cloudflare验证未完成（已尝试所有方法）'
                        }
                
                # 成功
                print(f"✅ 成功获取页面内容")
                print(f"   使用指纹: {impersonate}")
                print(f"   最终URL: {final_url}")
                print(f"   HTML长度: {len(html_content)}")
                print(f"   状态码: {response.status_code}")
                
                return {
                    'success': True,
                    'html': html_content,
                    'text': html_content,
                    'url': final_url
                }
                
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"   ⚠️ 请求失败: {e}，重试中...")
                    time.sleep(2)
                    continue
                else:
                    print(f"❌ curl-cffi绕过失败: {e}")
                    import traceback
                    traceback.print_exc()
                    return {
                        'success': False,
                        'error': str(e)
                    }
        
        return {
            'success': False,
            'error': '所有重试均失败'
        }
    
    async def bypass_with_playwright(
        self, 
        url: str, 
        wait_time: int = 5,
        timeout: int = 30,
        cookies: list = None  # 🔥 添加Cookie支持
    ) -> Dict:
        """
        使用Playwright绕过Cloudflare保护
        
        Args:
            url: 目标URL
            wait_time: 等待时间（秒）- 用于等待Cloudflare验证
            timeout: 超时时间（秒）
            
        Returns:
            Dict: {
                'success': bool,
                'html': str,  # 页面HTML
                'text': str,  # 页面文本
                'url': str,   # 最终URL（可能经过重定向）
                'error': str  # 错误信息（如果失败）
            }
        """
        if not self.playwright_available:
            return {
                'success': False,
                'error': 'Playwright未安装或不可用（可能是GLIBC版本过低）'
            }
        
        from playwright.async_api import async_playwright
        
        print(f"\n{'='*70}")
        print(f"🚀 使用Playwright绕过Cloudflare保护")
        print(f"{'='*70}")
        print(f"目标URL: {url}")
        print(f"等待时间: {wait_time} 秒\n")
        
        verdict = validate_outbound_url(url, resolve_dns=True)
        if not verdict.allowed:
            return {'success': False, 'error': f'URL被安全策略阻止: {verdict.reason}', 'url': url}
        url = verdict.normalized_url
        try:
            from crawler_resource_manager import playwright_slot

            async with playwright_slot(f"cloudflare:{url}"):
                async with async_playwright() as p:
                    # 准备浏览器启动参数
                    launch_options = {
                        'headless': True,
                        'args': [
                            '--disable-blink-features=AutomationControlled',
                            '--disable-dev-shm-usage',
                            '--no-sandbox'
                        ]
                    }

                    # 只有当明确配置了代理时才使用（不使用硬编码的代理）
                    if self.proxies and self.proxies.get('http'):
                        proxy_url = self.proxies.get('http') or self.proxies.get('https')
                        launch_options['proxy'] = {"server": proxy_url}
                        print(f"🔧 Playwright启动使用代理: {proxy_url}")
                    else:
                        print(f"🔧 Playwright启动不使用代理")

                    # 启动浏览器
                    browser = await p.chromium.launch(**launch_options)

                    try:
                        # 创建上下文（模拟真实浏览器，支持代理）
                        context_options = {
                            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                            'viewport': {'width': 1920, 'height': 1080},
                            'locale': 'zh-CN',
                            'timezone_id': 'Asia/Shanghai',
                            'ignore_https_errors': True  # 使用代理时忽略HTTPS错误
                        }

                        # 如果配置了代理，添加代理设置
                        if self.proxies and self.proxies.get('https'):
                            proxy_url = self.proxies.get('https') or self.proxies.get('http')
                            context_options['proxy'] = {'server': proxy_url}
                            print(f"🔧 Playwright使用代理: {proxy_url}")

                        context = await browser.new_context(**context_options)
                        await install_playwright_url_guard(context)

                        # 🔥 注入认证Cookie（如果有）
                        if cookies:
                            print(f"🔐 注入 {len(cookies)} 个认证Cookie...")
                            await context.add_cookies(cookies)

                        # 添加额外的隐身脚本
                        await context.add_init_script("""
                            Object.defineProperty(navigator, 'webdriver', {
                                get: () => undefined
                            });
                        """)

                        page = await context.new_page()

                        print(f"🔍 访问页面: {url}")
                        goto_timeout_ms = max(5000, int(timeout * 1000))
                        await page.goto(url, wait_until='domcontentloaded', timeout=goto_timeout_ms)

                        # 等待Cloudflare验证完成
                        print(f"⏳ 等待Cloudflare验证 {wait_time} 秒...")
                        await asyncio.sleep(wait_time)

                        try:
                            content_selector = (
                                'article, main, [itemprop="articleBody"], '
                                '.article-content, .article-body, .article-text, '
                                '.entry-content, .post-content, .story-content, '
                                '.news-content, .detail-content, #article-content'
                            )
                            await page.wait_for_selector(
                                content_selector,
                                timeout=max(3000, min(10000, wait_time * 1000)),
                            )
                            try:
                                await page.wait_for_load_state('networkidle', timeout=5000)
                            except Exception:
                                pass
                            await asyncio.sleep(max(1, min(3, wait_time // 2 or 1)))
                            print("✅ 通用正文容器已加载")
                        except Exception as e:
                            print(f"⚠️  等待通用正文容器超时，继续处理: {e}")

                        # 获取页面内容
                        html_content = await page.content()
                        text_content = await page.evaluate('document.body.innerText')
                        final_url = page.url

                        # 检查是否成功绕过
                        if 'checking your browser' in html_content.lower() or \
                           'just a moment' in html_content.lower():
                            print("⚠️ Cloudflare验证可能未完成，尝试延长等待时间...")
                            await asyncio.sleep(wait_time)
                            html_content = await page.content()
                            text_content = await page.evaluate('document.body.innerText')

                        print(f"✅ 成功获取页面内容")
                        print(f"   最终URL: {final_url}")
                        print(f"   HTML长度: {len(html_content)}")
                        print(f"   文本长度: {len(text_content)}")

                        return {
                            'success': True,
                            'html': html_content,
                            'text': text_content,
                            'url': final_url
                        }

                    finally:
                        await browser.close()
                    
        except Exception as e:
            print(f"❌ Playwright绕过失败: {e}")
            import traceback
            traceback.print_exc()
            return {
                'success': False,
                'error': str(e)
            }
    
    def fetch_with_auto_bypass(self, url: str, force_playwright: bool = False, force_curl_cffi: bool = False) -> Dict:
        """
        智能获取网页内容（自动检测并绕过Cloudflare）
        
        Args:
            url: 目标URL
            force_playwright: 强制使用Playwright
            force_curl_cffi: 强制使用curl-cffi
            
        Returns:
            Dict: {
                'success': bool,
                'html': str,
                'text': str,
                'method': str,  # 'requests', 'curl-cffi' 或 'playwright'
                'error': str
            }
        """
        # 如果强制使用curl-cffi
        if force_curl_cffi:
            print("🔧 强制使用curl-cffi模式")
            result = self.bypass_with_curl_cffi(url)
            if result['success']:
                result['method'] = 'curl-cffi'
            return result
        
        # 如果强制使用Playwright
        if force_playwright:
            print("🔧 强制使用Playwright模式")
            result = asyncio.run(self.bypass_with_playwright(url))
            if result['success']:
                result['method'] = 'playwright'
            return result
        
        # 🔥 已移除白名单检查，总是先尝试普通请求
        # 先尝试普通请求
        try:
            print(f"🔍 尝试普通请求: {url}")
            headers = self._browser_headers({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
            })
            
            response = self._run_with_retry(
                lambda: self._requests_get(url, headers=headers, timeout=15, allow_redirects=True, verify=False),
                int(os.getenv("CRAWL_REQUEST_RETRY_ATTEMPTS", "2")),
            )
            
            # 检测Cloudflare保护
            is_cloudflare = (
                response.status_code == 403 or
                'cf-mitigated' in str(response.headers).lower() or
                'checking your browser' in response.text.lower() or
                'just a moment' in response.text.lower()
            )
            
            if is_cloudflare:
                print(f"🛡️ 检测到Cloudflare保护，尝试绕过方法")
                
                # 优先使用curl-cffi（兼容性更好）
                if self.curl_cffi_available:
                    print("🔄 切换到curl-cffi模式")
                    result = self.bypass_with_curl_cffi(url)
                    if result['success']:
                        result['method'] = 'curl-cffi'
                        return result
                
                # 备用：使用Playwright
                if self.playwright_available:
                    print("🔄 切换到Playwright模式")
                    result = asyncio.run(self.bypass_with_playwright(url))
                    if result['success']:
                        result['method'] = 'playwright'
                        return result
                
                return {
                    'success': False,
                    'error': 'Cloudflare保护需要curl-cffi或Playwright，但都不可用'
                }
            
            # 普通请求成功
            print(f"✅ 普通请求成功")
            return {
                'success': True,
                'html': response.text,
                'text': response.text,
                'url': response.url,
                'method': 'requests'
            }
            
        except Exception as e:
            print(f"⚠️ 普通请求失败: {e}")
            
            # 优先尝试curl-cffi
            if self.curl_cffi_available:
                print("🔄 切换到curl-cffi模式")
                result = self.bypass_with_curl_cffi(url)
                if result['success']:
                    result['method'] = 'curl-cffi'
                    return result
            
            # 备用：尝试Playwright
            if self.playwright_available:
                print("🔄 切换到Playwright模式")
                result = asyncio.run(self.bypass_with_playwright(url))
                if result['success']:
                    result['method'] = 'playwright'
                    return result
            
            return {
                'success': False,
                'error': f'普通请求失败且绕过方法不可用: {e}'
            }


# 全局实例（使用配置中的代理，按需懒加载）
def _get_global_bypass_instance():
    """创建全局Cloudflare绕过实例。"""
    try:
        from config import get_proxies
        proxies = get_proxies()
        return CloudflareBypass(proxies=proxies)
    except:
        # 如果config不可用，使用无代理实例
        return CloudflareBypass()

cloudflare_bypass = None


def _get_or_create_global_bypass_instance():
    """获取全局Cloudflare绕过实例，仅在真正需要全局代理语义时初始化。"""
    global cloudflare_bypass
    if cloudflare_bypass is None:
        cloudflare_bypass = _get_global_bypass_instance()
    return cloudflare_bypass


# 便捷函数
def fetch_url(url: str, force_playwright: bool = False, force_curl_cffi: bool = False, proxies=None, max_retries: int = 1) -> Dict:
    """
    获取URL内容（自动处理Cloudflare，支持重试）
    
    Args:
        url: 目标URL
        force_playwright: 是否强制使用Playwright
        force_curl_cffi: 是否强制使用curl-cffi
        proxies: 自定义代理配置（可选），格式：{'http': 'http://...', 'https': 'http://...'}
        max_retries: 失败时最大重试次数（默认1次）
        
    Returns:
        Dict: 包含success, html, text等字段
    """
    # proxies=None 表示使用全局配置；proxies={} 表示调用方明确要求直连。
    if proxies is not None:
        bypass_instance = CloudflareBypass(proxies=proxies)
    else:
        # 使用全局实例
        bypass_instance = _get_or_create_global_bypass_instance()
    
    # 尝试获取，失败时重试
    last_error = None
    for attempt in range(max_retries + 1):
        if attempt > 0:
            print(f"🔄 第 {attempt + 1} 次尝试获取: {url[:80]}...")
        
        result = bypass_instance.fetch_with_auto_bypass(url, force_playwright, force_curl_cffi)
        
        if result['success']:
            return result
        
        # 记录错误
        last_error = result.get('error', '未知错误')
        
        # 如果还有重试次数，继续
        if attempt < max_retries:
            import time
            wait_time = 2 * (attempt + 1)  # 递增等待时间
            print(f"⏳ 等待 {wait_time} 秒后重试...")
            time.sleep(wait_time)
    
    # 所有尝试都失败
    print(f"❌ 获取失败，已重试 {max_retries} 次: {last_error}")
    return {
        'success': False,
        'error': f'失败（已重试{max_retries}次）: {last_error}',
        'html': '',
        'text': ''
    }


def is_cloudflare_protected(url: str) -> bool:
    """
    检测URL是否被Cloudflare保护
    
    Args:
        url: 目标URL
        
    Returns:
        bool: 是否被保护
    """
    return _get_or_create_global_bypass_instance().detect_cloudflare(url)


if __name__ == '__main__':
    # 测试代码
    import sys
    
    if len(sys.argv) > 1:
        test_url = sys.argv[1]
    else:
        test_url = 'https://www.hkej.com/'
    
    print(f"\n测试URL: {test_url}\n")
    
    # 测试检测
    print("="*70)
    print("测试1: 检测Cloudflare保护")
    print("="*70)
    is_protected = is_cloudflare_protected(test_url)
    print(f"\n结果: {'有Cloudflare保护' if is_protected else '无Cloudflare保护'}\n")
    
    # 测试获取
    print("="*70)
    print("测试2: 智能获取页面内容")
    print("="*70)
    result = fetch_url(test_url)
    
    if result['success']:
        print(f"\n✅ 成功!")
        print(f"使用方法: {result.get('method', 'unknown')}")
        print(f"HTML长度: {len(result['html'])}")
        print(f"文本长度: {len(result['text'])}")
        print(f"\n前500字符预览:")
        print("-" * 70)
        print(result['text'][:500])
        print("-" * 70)
    else:
        print(f"\n❌ 失败: {result.get('error', '未知错误')}")
