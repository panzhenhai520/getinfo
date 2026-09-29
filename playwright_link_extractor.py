#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通用 Playwright 分页识别和文章链接提取器
自动识别分页，提取所有文章链接给到 newspaper3k 处理
"""

import asyncio
import hashlib
import html as html_lib
import json
import os
import re
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse, urlunparse
from typing import List, Dict, Optional
from datetime import datetime
from utils import get_china_time
from outbound_url_policy import install_playwright_url_guard, safe_request_head, validate_outbound_url

_APP_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', os.path.join(_APP_BASE_DIR, 'data', 'ms-playwright'))


def _requests_head_with_proxy_intent(url, proxies=None, **kwargs):
    """Use requests.head while treating {} as explicit direct connection."""
    import requests

    kwargs['proxies'] = proxies
    if isinstance(proxies, dict) and not proxies:
        with requests.Session() as session:
            session.trust_env = False
            return safe_request_head(url, session=session, **kwargs)
    return safe_request_head(url, **kwargs)


class PlaywrightLinkExtractor:
    """使用 Playwright 识别分页并提取所有文章链接"""
    
    def __init__(self, crawl_options: Optional[Dict] = None):
        """初始化提取器"""
        from crawl_options import normalize_crawl_options

        self.crawl_options = normalize_crawl_options(crawl_options)
        self.playwright_available = False
        self.click_timeout_ms = self._read_click_timeout_ms()
        try:
            from playwright.async_api import async_playwright
            self.playwright_available = True
            print("✅ Playwright 可用")
        except ImportError:
            print("❌ Playwright 未安装")
            print("请运行: pip install playwright")
            print("然后运行: playwright install chromium")

    def _read_click_timeout_ms(self) -> int:
        try:
            import os
            value = int(os.getenv('CRAWL_PLAYWRIGHT_CLICK_TIMEOUT_MS', '1200'))
            return max(250, min(value, 5000))
        except Exception:
            return 1200

    def _summarize_page_analysis(self) -> Dict:
        """Return a JSON-safe summary of the auto-detected page rules."""
        analysis = getattr(self, '_page_analysis', {}) or {}
        pagination = analysis.get('pagination') if isinstance(analysis.get('pagination'), dict) else {}
        date_filter = analysis.get('date_filter') if isinstance(analysis.get('date_filter'), dict) else {}
        crawl_strategy = analysis.get('crawl_strategy') if isinstance(analysis.get('crawl_strategy'), dict) else {}
        library_analysis = analysis.get('library_analysis') if isinstance(analysis.get('library_analysis'), dict) else {}

        return {
            'enabled': bool(analysis),
            'site_type': analysis.get('site_type') or 'unknown',
            'main_content_selector': analysis.get('main_content_selector'),
            'main_content_confidence': analysis.get('main_content_confidence', 0),
            'article_count_estimate': analysis.get('article_count', 0),
            'article_link_pattern': analysis.get('article_link_pattern'),
            'article_link_selector': analysis.get('article_link_selector'),
            'pagination_type': pagination.get('type') or pagination.get('pagination_type'),
            'pagination_selector': pagination.get('selector'),
            'pagination_total_pages': pagination.get('total_pages', 1),
            'pagination_has_next': bool(pagination.get('has_next')),
            'date_filter_available': bool(date_filter.get('has_date_filter')),
            'date_filter_type': date_filter.get('type'),
            'date_filter_selector': date_filter.get('selector'),
            'strategy': {
                'use_main_content': bool(crawl_strategy.get('use_main_content')),
                'main_selector': crawl_strategy.get('main_selector'),
                'pagination_method': crawl_strategy.get('pagination_method'),
                'date_filter_available': bool(crawl_strategy.get('date_filter_available')),
            },
            'suggestions': list(analysis.get('suggestions') or [])[:10],
            'library_best_method': library_analysis.get('best_method'),
            'library_best_score': library_analysis.get('best_score'),
        }

    async def _safe_click(self, element, label: str = '', timeout_ms: Optional[int] = None) -> bool:
        timeout_ms = timeout_ms or self.click_timeout_ms
        try:
            if not element:
                return False
            try:
                if not await element.is_visible():
                    return False
            except Exception:
                return False
            try:
                if not await element.is_enabled():
                    return False
            except Exception:
                pass
            try:
                box = await element.bounding_box()
                if not box or box.get('width', 0) <= 0 or box.get('height', 0) <= 0:
                    return False
            except Exception:
                pass
            try:
                await element.scroll_into_view_if_needed(timeout=min(timeout_ms, 1000))
            except Exception:
                pass
            await element.click(timeout=timeout_ms)
            return True
        except Exception as e:
            if label:
                print(f"      safe click failed ({label}): {str(e)[:120]}")
            return False
    
    async def extract_links_from_url(
        self, 
        url: str, 
        max_articles: float = float('inf'),  # 无限制
        max_pages: float = float('inf'),     # 无限制
        wait_time: int = 3
    ) -> Dict:
        """
        从URL提取所有文章链接（自动识别分页）
        
        Args:
            url: 目标URL（列表页）
            max_articles: 最多提取的文章数
            max_pages: 最多聚合的页数
            wait_time: 页面加载等待时间（秒）
            
        Returns:
            Dict: {'success': bool, 'articles': list, 'stats': dict}
        """
        if not self.playwright_available:
            return {
                'success': False,
                'error': 'Playwright 未安装',
                'articles': []
            }
        
        from playwright.async_api import async_playwright
        
        print(f"\n{'='*70}")
        print(f"🚀 Playwright 分页识别和链接提取器")
        print(f"{'='*70}")
        print(f"目标URL: {url}")
        print(f"最多提取: {max_articles} 篇文章")
        print(f"最多页数: {max_pages} 页")
        print(f"等待时间: {wait_time} 秒\n")
        
        # 🔥 存储到实例变量，供其他方法使用
        self._max_articles = max_articles
        self._max_pages = max_pages
        
        all_article_links = []
        base_domain = urlparse(url).netloc
        page_num = 1  # 🔥 修复：初始化 page_num 变量，避免未定义错误
        pages_to_crawl = 1  # 🔥 修复：初始化 pages_to_crawl 变量
        empty_pages_count = 0  # 🔥 新增：连续空页计数器
        max_empty_pages = int(self.crawl_options.get('max_empty_pages', 5))
        network_tasks = set()
        self._network_article_candidates = []
        self._network_json_seen = set()
        self._network_json_responses_checked = 0
        self._network_json_responses_used = 0
        self._network_json_errors = 0
        self._network_jsonp_responses = 0
        self._network_script_responses_checked = 0
        self._network_html_error_responses = 0
        self._network_verification_signals = []
        self._network_response_samples = []
        self._network_endpoint_stats = {}
        self._site_profile = self._detect_site_profile(url)
        
        from crawler_resource_manager import playwright_slot

        async with playwright_slot(f"link-discovery:{url}"):
            # Keep the Playwright driver alive for the whole discovery flow.
            # The following link-processing code intentionally lives outside the
            # small launch block; using an ``async with`` here closed Chromium
            # before response handlers, init scripts and page navigation ran.
            p = await async_playwright().start()
            # 启动浏览器（无头模式，添加反检测参数）
            # 从全局配置读取代理
            import config
            proxy_config = config.get_playwright_proxy(enabled=self.crawl_options.get('proxy_enabled'))
            
            browser = await p.chromium.launch(
                headless=True,
                proxy=proxy_config,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox'
                ]
            )
            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                viewport={'width': 1920, 'height': 1080},
                locale='zh-CN',
                timezone_id='Asia/Shanghai'
            )
            await install_playwright_url_guard(context)

            def schedule_network_capture(response):
                try:
                    task = asyncio.create_task(
                        self._capture_network_json_response(response, url, base_domain)
                    )
                    network_tasks.add(task)
                    task.add_done_callback(lambda done: network_tasks.discard(done))
                except Exception:
                    pass

            if self.crawl_options.get('network_json_enabled', True):
                try:
                    context.on("response", schedule_network_capture)
                except Exception as e:
                    print(f"   ⚠️ 网络JSON监听启用失败: {e}")
            else:
                print("   ℹ️ 网络JSON候选捕获: 已关闭")
            
            # 添加反检测脚本
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """)
            
            page = await context.new_page()
            
            try:
                # 访问页面（增加超时时间）
                print(f"🔍 访问页面: {url}")
                print(f"   代理: {proxy_config}")
                
                try:
                    # 尝试加载页面（30秒超时，快速失败）
                    await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                    print(f"   ✅ 页面加载成功")
                except Exception as e:
                    print(f"   ❌ 页面加载超时: {e}")
                    raise  # 抛出异常，让外层catch处理
                
                # 等待内容加载（包括Cloudflare验证）
                print(f"⏳ 等待页面加载和Cloudflare验证 {wait_time} 秒...")
                await asyncio.sleep(wait_time)
                
                # 检查是否还在Cloudflare验证页面
                page_content = await page.content()
                if 'checking your browser' in page_content.lower() or 'just a moment' in page_content.lower():
                    print(f"   检测到Cloudflare验证，额外等待 {wait_time} 秒...")
                    await asyncio.sleep(wait_time)
                
                # 🔥 新增：智能页面结构分析
                print(f"\n{'='*60}")
                print(f"🔍 智能页面结构分析")
                print(f"{'='*60}")
                try:
                    from page_structure_analyzer import PageStructureAnalyzer
                    analyzer = PageStructureAnalyzer()
                    page_analysis = await analyzer.analyze(page, url)
                    
                    # 保存分析结果，供后续使用
                    self._page_analysis = page_analysis
                    
                    # 根据分析结果设置主内容区选择器
                    if page_analysis.get('main_content_selector'):
                        self._main_content_selector = page_analysis['main_content_selector']
                        print(f"   ✅ 将在 {self._main_content_selector} 中提取链接")
                    else:
                        self._main_content_selector = None
                except Exception as e:
                    print(f"   ⚠️ 页面分析失败，使用默认策略: {e}")
                    self._page_analysis = {}
                    self._main_content_selector = None
                
                # 等待页面完全加载（包括分页元素）
                print(f"\n⏳ 等待分页元素加载...")
                await asyncio.sleep(2)  # 额外等待分页元素渲染
                
                # 滚动到页面底部，确保分页元素可见
                try:
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(1)  # 等待滚动完成
                    print(f"   ↓ 已滚动到页面底部")
                except:
                    pass
                
                # 检测分页信息
                pagination_info = await self._detect_pagination(page)
                print(f"📄 分页检测结果:")
                print(f"   - 检测到页数: {pagination_info.get('total_pages', '未知')}")
                print(f"   - 分页类型: {pagination_info.get('pagination_type', '未知')}")
                
                # 🔥 特殊处理：如果检测到Tab切换或下拉框筛选
                if pagination_info.get('pagination_type') in ['tab_navigation', 'year_tabs', 'select_dropdown']:
                    pagination_type = pagination_info.get('pagination_type')
                    
                    # 下拉框筛选模式
                    if pagination_type == 'select_dropdown':
                        print(f"   🎯 使用下拉框筛选模式提取\n")
                        select_elem = pagination_info.get('select_element')
                        options = pagination_info.get('tab_elements', [])
                        
                        # 🔥 修复：将 max_pages 转换为整数索引
                        max_pages_int = None if max_pages == float('inf') else int(max_pages)
                        pages_to_crawl = len(options[:max_pages_int])  # 🔥 修复：设置总页数用于统计
                        for opt_idx, option in enumerate(options[:max_pages_int], 1):
                            page_num = opt_idx  # 🔥 修复：更新 page_num 用于统计
                            print(f"{'='*60}")
                            try:
                                opt_text = await option.text_content()
                                opt_value = await option.get_attribute('value')
                                print(f"📋 正在聚合选项 {opt_idx}/{len(options[:max_pages_int])}: {opt_text.strip()}")
                            except:
                                print(f"📋 正在聚合选项 {opt_idx}/{len(options[:max_pages_int])}")
                            print(f"{'='*60}")
                            
                            try:
                                # 选择option
                                await select_elem.select_option(value=opt_value) if opt_value else await select_elem.select_option(index=opt_idx-1)
                                await asyncio.sleep(wait_time)  # 等待内容加载
                                
                                # 提取当前选项的链接
                                page_links, _ = await self._extract_links_from_page(page, url, base_domain)
                                
                                # 添加到总列表（去重）
                                new_links = 0
                                for link in page_links:
                                    if not any(a['url'] == link['url'] for a in all_article_links):
                                        all_article_links.append(link)
                                        new_links += 1
                                        print(f"   ✅ [{len(all_article_links)}] {link['title'][:60]}...")
                                
                                print(f"\n   📊 本选项新增 {new_links} 篇文章，总计 {len(all_article_links)} 篇")
                                
                                # 🔥 如果设置了有限的max_articles才检查
                                if max_articles != float('inf') and len(all_article_links) >= max_articles:
                                    print(f"\n   ✅ 已获取足够文章 ({len(all_article_links)} 篇)，停止聚合")
                                    break
                                    
                            except Exception as e:
                                print(f"   ⚠️ 选项 {opt_idx} 处理失败: {e}")
                                continue
                    
                    # Tab切换模式（标准Tab和年份Tab）
                    else:
                        print(f"   🎯 使用Tab切换模式提取\n")
                        tab_texts = pagination_info.get('tab_texts', [])
                        tab_selector = pagination_info.get('tab_selector', '')
                        
                        # 🔥 修复：将 max_pages 转换为整数索引
                        max_pages_int = None if max_pages == float('inf') else int(max_pages)
                        pages_to_crawl = len(tab_texts[:max_pages_int])  # 🔥 修复：设置总页数用于统计
                        
                        for tab_idx in range(1, pages_to_crawl + 1):
                            page_num = tab_idx  # 🔥 修复：更新 page_num 用于统计
                            print(f"{'='*60}")
                            tab_text = tab_texts[tab_idx - 1] if tab_idx <= len(tab_texts) else f"Tab {tab_idx}"
                            print(f"🔖 正在聚合 Tab {tab_idx}/{pages_to_crawl}: {tab_text.strip()}")
                            print(f"{'='*60}")
                            
                            # 🔥 关键修复：每次都重新查找 Tab 元素，避免 stale element reference
                            # 🔥 优化：增加重试机制
                            max_retries = 2  # 最多重试2次
                            retry_count = 0
                            success = False
                            
                            while retry_count <= max_retries and not success:
                                try:
                                    if retry_count > 0:
                                        print(f"   🔄 重试 {retry_count}/{max_retries}...")
                                    
                                    # 重新查找所有 Tab 元素
                                    container = await page.query_selector(tab_selector)
                                    if not container:
                                        print(f"   ⚠️ 无法找到 Tab 容器: {tab_selector}")
                                        break
                                    
                                    # 重新获取所有 Tab
                                    all_tabs = await container.query_selector_all(
                                        'a, button, li[role="tab"], div[role="tab"], span[role="tab"], '
                                        '[class*="tab-item"], [class*="tab-button"], '
                                        '[data-tab], [data-toggle="tab"]'
                                    )
                                    
                                    # 找到当前索引的 Tab（注意：tab_idx 从 1 开始，数组索引从 0 开始）
                                    if tab_idx - 1 < len(all_tabs):
                                        tab_elem = all_tabs[tab_idx - 1]
                                    else:
                                        print(f"   ⚠️ Tab 索引超出范围: {tab_idx}")
                                        break
                                    
                                    clicked = await self._safe_click(tab_elem, f"tab {tab_idx}")
                                    if not clicked:
                                        raise RuntimeError("tab element is not clickable")
                                    await asyncio.sleep(wait_time)  # 等待内容加载
                                    success = True  # 点击成功
                                    
                                except Exception as click_error:
                                    retry_count += 1
                                    if retry_count > max_retries:
                                        print(f"   ❌ Tab {tab_idx} 点击失败（已重试{max_retries}次）: {click_error}")
                                        break
                                    else:
                                        await asyncio.sleep(2)  # 等待2秒后重试
                                        continue
                            
                            if not success:
                                print(f"   ⚠️ Tab {tab_idx} 处理失败: 无法点击")
                                continue
                            
                            # 点击成功后，处理该Tab的内容（可能有嵌套分页）
                            try:
                                tab_start_count = len(all_article_links)  # 记录Tab开始时的文章数
                                print(f"   🔍 检查 Tab 内部是否有分页...")
                                
                                # 🔥 新增：检测 Tab 内部的分页
                                inner_pagination = await self._detect_pagination(page)
                                inner_total_pages = inner_pagination.get('total_pages', 1)
                                inner_pagination_type = inner_pagination.get('pagination_type', 'none')
                                
                                if inner_total_pages > 1 and inner_pagination_type not in ['tab_navigation', 'year_tabs', 'select_dropdown']:
                                    print(f"   📄 检测到 Tab 内部分页: {inner_total_pages} 页 (类型: {inner_pagination_type})")
                                    
                                    # 限制每个 Tab 最多聚合的页数（避免过多）
                                    max_inner_pages = min(inner_total_pages, 10)  # 最多10页
                                    print(f"   📊 将聚合 {max_inner_pages} 页")
                                    
                                    # 逐页提取链接
                                    for inner_page_num in range(1, max_inner_pages + 1):
                                        print(f"      📖 Tab内第 {inner_page_num}/{max_inner_pages} 页")
                                        
                                        # 提取当前页链接
                                        page_links, _ = await self._extract_links_from_page(page, url, base_domain)
                                        
                                        # 添加到总列表（去重）
                                        for link in page_links:
                                            if not any(a['url'] == link['url'] for a in all_article_links):
                                                all_article_links.append(link)
                                                print(f"      ✅ [{len(all_article_links)}] {link['title'][:50]}...")
                                        
                                        # 如果不是最后一页，翻到下一页
                                        if inner_page_num < max_inner_pages:
                                            print(f"      🔄 翻到第 {inner_page_num + 1} 页...")
                                            success = await self._go_to_next_page(page, inner_pagination, inner_page_num)
                                            if not success:
                                                print(f"      ⚠️ 无法翻页，停止")
                                                break
                                            await asyncio.sleep(2)  # 等待页面加载
                                else:
                                    # 没有内部分页，直接提取当前页
                                    if inner_total_pages == 1:
                                        print(f"   ℹ️  Tab 内部无分页，提取当前页")
                                    
                                    page_links, _ = await self._extract_links_from_page(page, url, base_domain)
                                    
                                    # 添加到总列表（去重）
                                    for link in page_links:
                                        if not any(a['url'] == link['url'] for a in all_article_links):
                                            all_article_links.append(link)
                                            print(f"   ✅ [{len(all_article_links)}] {link['title'][:60]}...")
                                
                                tab_new_count = len(all_article_links) - tab_start_count
                                print(f"\n   📊 本Tab新增 {tab_new_count} 篇，总计 {len(all_article_links)} 篇文章")
                                
                                # 🔥 如果设置了有限的max_articles才检查
                                if max_articles != float('inf') and len(all_article_links) >= max_articles:
                                    print(f"\n   ✅已获取足够文章 ({len(all_article_links)} 篇)，停止聚合")
                                    break
                                    
                            except Exception as e:
                                print(f"   ⚠️ Tab {tab_idx} 提取链接失败: {e}")
                                continue
                
                else:
                    # 标准分页模式（数字分页、下一页按钮、URL参数等）
                    detected_pages = pagination_info.get('total_pages', 1)
                    pagination_type = pagination_info.get('pagination_type', 'none')
                    
                    # 对于未知页数的情况（如只有"下一页"按钮），限制最大页数
                    # 🔥 修复：处理 float('inf') 情况
                    max_pages_finite = 999 if max_pages == float('inf') else int(max_pages)
                    
                    if detected_pages == 999:  # 未知页数标记
                        pages_to_crawl = max_pages_finite
                        print(f"   - 未知总页数，将尝试聚合: {pages_to_crawl} 页")
                    else:
                        pages_to_crawl = min(max_pages_finite, detected_pages) if detected_pages else max_pages_finite
                        print(f"   - 实际聚合: {pages_to_crawl} 页")
                    
                    print(f"   - 分页方式: {pagination_type}\n")
                    
                    # 逐页提取链接
                    for page_num in range(1, pages_to_crawl + 1):
                        print(f"{'='*60}")
                        print(f"📖 正在聚合第 {page_num}/{pages_to_crawl} 页")
                        print(f"{'='*60}")
                        
                        # 提取当前页的文章链接
                        page_links, has_more = await self._extract_links_from_page(page, url, base_domain)
                        
                        # 🔥 如果聚合了"更多"链接，添加所有链接后停止主页面翻页
                        if has_more:
                            print(f"\n   ✅ 已聚合'更多'链接（提取了{len(page_links)}个链接），停止主页面翻页")
                            # 添加所有链接
                            for link in page_links:
                                if not any(a['url'] == link['url'] for a in all_article_links):
                                    all_article_links.append(link)
                            print(f"   📊 总计 {len(all_article_links)} 篇文章")
                            break  # 退出主循环
                        
                        # 添加到总列表（去重）
                        new_links = 0
                        for link in page_links:
                            if not any(a['url'] == link['url'] for a in all_article_links):
                                all_article_links.append(link)
                                new_links += 1
                                print(f"   ✅ [{len(all_article_links)}] {link['title'][:60]}...")
                        
                        print(f"\n   📊 本页新增 {new_links} 篇文章，总计 {len(all_article_links)} 篇")
                        
                        # 🔥 检测连续空页
                        if new_links == 0:
                            empty_pages_count += 1
                            print(f"\n   ⚠️ 本页无新文章 (连续 {empty_pages_count}/{max_empty_pages} 页)")
                            
                            if empty_pages_count >= max_empty_pages:
                                print(f"\n   🛑 连续 {max_empty_pages} 页没有文章，停止翻页")
                                print(f"   ℹ️  已提取 {len(all_article_links)} 篇文章，将继续处理...")
                                break
                        else:
                            empty_pages_count = 0  # 重置计数器
                        
                        # 🔥 检查是否已达到文章数量限制
                        if max_articles != float('inf') and len(all_article_links) >= max_articles:
                            print(f"\n   ✅ 已获取足够文章 ({len(all_article_links)} 篇)，停止聚合")
                            break
                        
                        # 如果不是最后一页，尝试翻页
                        if page_num < pages_to_crawl:
                            print(f"\n   🔄 准备翻到第 {page_num + 1} 页...")
                            success = await self._go_to_next_page(page, pagination_info, page_num)
                            if not success:
                                print(f"   ⚠️ 无法翻页，停止聚合")
                                print(f"   💡 可能原因: 1) 已到最后一页 2) 分页元素改变 3) 需要等待更长时间")
                                break
                            
                            # 等待新页面加载
                            print(f"   ⏳ 等待新页面加载 {wait_time} 秒...")
                            await asyncio.sleep(wait_time)
                            
                            # 滚动到顶部，确保能看到新内容
                            try:
                                await page.evaluate("window.scrollTo(0, 0)")
                                await asyncio.sleep(0.5)
                            except:
                                pass
                            
                            print(f"   ✅ 已切换到第 {page_num + 1} 页")
                await self._finalize_network_json_candidates(network_tasks, all_article_links)

            except Exception as e:
                print(f"❌ 聚合出错: {e}")
                await self._finalize_network_json_candidates(network_tasks, all_article_links)
                return {
                    'success': False,
                    'error': str(e),
                    'articles': all_article_links,
                    'stats': {
                        'total_found': len(all_article_links),
                        'network_json_enabled': self.crawl_options.get('network_json_enabled', True),
                        'network_json_candidates': len(getattr(self, '_network_article_candidates', [])),
                        'network_json_inline_candidates': self._count_network_inline_candidates(),
                        'network_json_responses_checked': getattr(self, '_network_json_responses_checked', 0),
                        'network_json_responses_used': getattr(self, '_network_json_responses_used', 0),
                        'network_json_errors': getattr(self, '_network_json_errors', 0),
                        'network_jsonp_responses': getattr(self, '_network_jsonp_responses', 0),
                        'network_script_responses_checked': getattr(self, '_network_script_responses_checked', 0),
                        'network_html_error_responses': getattr(self, '_network_html_error_responses', 0),
                        'network_verification_signals': getattr(self, '_network_verification_signals', []),
                        'network_response_samples': getattr(self, '_network_response_samples', []),
                    }
                }
            finally:
                await browser.close()
                await p.stop()
        
        print(f"\n{'='*70}")
        print(f"✅ 聚合完成！")
        print(f"📊 总共提取: {len(all_article_links)} 篇文章")
        print(f"{'='*70}\n")
        
        return {
            'success': True,
            'articles': all_article_links,
                'stats': {
                    'total_found': len(all_article_links),
                    'pages_crawled': min(page_num, pages_to_crawl),
                    'pages_visited': min(page_num, pages_to_crawl),
                    'network_json_enabled': self.crawl_options.get('network_json_enabled', True),
                    'network_json_candidates': len(getattr(self, '_network_article_candidates', [])),
                'network_json_inline_candidates': self._count_network_inline_candidates(),
                'network_json_responses_checked': getattr(self, '_network_json_responses_checked', 0),
                'network_json_responses_used': getattr(self, '_network_json_responses_used', 0),
                'network_json_errors': getattr(self, '_network_json_errors', 0),
                'network_jsonp_responses': getattr(self, '_network_jsonp_responses', 0),
                'network_script_responses_checked': getattr(self, '_network_script_responses_checked', 0),
                'network_html_error_responses': getattr(self, '_network_html_error_responses', 0),
                'network_verification_signals': getattr(self, '_network_verification_signals', []),
                'network_response_samples': getattr(self, '_network_response_samples', []),
                'network_endpoint_stats': getattr(self, '_network_endpoint_stats', {}),
                'site_profile': getattr(self, '_site_profile', 'generic'),
                'auto_profile': self._summarize_page_analysis(),
                'extracted_at': get_china_time().isoformat()
            }
        }

    async def _finalize_network_json_candidates(self, network_tasks: set, all_article_links: List[Dict]) -> int:
        """Wait for pending response parsing and merge network JSON candidates."""
        await self._drain_network_json_tasks(network_tasks)

        candidates = getattr(self, '_network_article_candidates', []) or []
        if not candidates:
            return 0

        seen_urls = {
            (item.get('url') or '').strip().lower().rstrip('/')
            for item in all_article_links
            if item.get('url')
        }
        added = 0
        for candidate in candidates:
            url = (candidate.get('url') or '').strip()
            key = url.lower().rstrip('/')
            if not url or key in seen_urls:
                continue
            seen_urls.add(key)
            all_article_links.append(candidate)
            added += 1

        if added:
            inline_count = self._count_network_inline_candidates()
            print(f"   🌐 网络接口JSON补充 {added} 个候选（其中内联正文 {inline_count} 个）")
        return added

    async def _drain_network_json_tasks(self, network_tasks: set):
        for _ in range(3):
            pending = [task for task in list(network_tasks) if not task.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    async def _capture_network_json_response(self, response, list_url: str, base_domain: str):
        try:
            status = getattr(response, 'status', 0) or 0
            response_url = getattr(response, 'url', '') or ''
            if not response_url or not self._is_allowed_network_source(response_url, list_url):
                return

            headers = getattr(response, 'headers', {}) or {}
            content_type = str(headers.get('content-type', '')).lower()
            is_json_like_endpoint = self._looks_like_json_endpoint(response_url)
            is_script_like = any(token in content_type for token in ('javascript', 'ecmascript', 'text/plain'))
            is_html_like = 'html' in content_type
            if not ('json' in content_type or is_json_like_endpoint or is_script_like):
                return
            if status and not (200 <= status < 400) and not is_json_like_endpoint:
                return

            content_length = self._safe_int(headers.get('content-length'))
            if content_length and content_length > 6_000_000:
                return

            text = await response.text()
            if not text or len(text) > 6_000_000:
                return

            self._remember_network_sample(response_url, status, content_type, text)
            if is_script_like and 'json' not in content_type:
                self._network_script_responses_checked += 1
            if is_html_like and self._looks_like_html_error(text):
                self._network_html_error_responses += 1

            data, parse_mode = self._parse_json_like_response(text)
            self._network_json_responses_checked += 1
            self._record_network_endpoint(response_url, parse_mode)
            if parse_mode == 'jsonp':
                self._network_jsonp_responses += 1
            if data is None:
                return

            self._collect_verification_signals(data, response_url)
            candidates = self._extract_network_json_candidates(data, response_url, list_url, base_domain)
            if not candidates:
                return

            self._network_json_responses_used += 1
            for candidate in candidates:
                url = candidate.get('url') or ''
                key = url.lower().rstrip('/')
                if not key or key in self._network_json_seen:
                    continue
                self._network_json_seen.add(key)
                self._network_article_candidates.append(candidate)
        except Exception:
            self._network_json_errors += 1

    def _extract_network_json_candidates(self, data, response_url: str, list_url: str, base_domain: str) -> List[Dict]:
        candidates = []
        for item in self._iter_json_dicts(data):
            if len(candidates) >= 800:
                break

            candidate = self._network_json_item_to_candidate(item, response_url, list_url, base_domain)
            if candidate:
                candidates.append(candidate)

        return candidates

    def _network_json_item_to_candidate(self, item: Dict, response_url: str, list_url: str, base_domain: str) -> Optional[Dict]:
        if not isinstance(item, dict):
            return None

        title = self._first_json_string(
            item,
            (
                'title', 'headline', 'name', 'subject', 'caption', 'seoTitle',
                'postTitle', 'articleTitle', 'newsTitle'
            )
        )
        title = self._clean_json_text(title, max_len=200)

        content = self._first_json_string(
            item,
            (
                'content', 'body', 'articleBody', 'html', 'markdown', 'fullText',
                'text', 'detail', 'description', 'summary', 'excerpt', 'abstract'
            )
        )
        content = self._clean_json_text(content, max_len=120000)

        raw_url = self._first_json_url(
            item,
            (
                'url', 'link', 'href', 'permalink', 'canonical', 'path',
                'articleUrl', 'detailUrl', 'shareUrl', 'mobileUrl', 'webUrl'
            )
        )

        if not title and content:
            title = self._title_from_content(content)

        if not self._looks_like_article_title(title):
            return None

        publish_date = self._parse_date_from_text(
            self._first_json_string(
                item,
                (
                    'datePublished', 'publishedAt', 'publishDate', 'pubDate',
                    'createdAt', 'createTime', 'updatedAt', 'date', 'time'
                )
            )
            or content[:300]
        )

        url = self._normalize_json_candidate_url(raw_url, response_url, list_url)
        has_inline_content = len(content) >= 50

        if url:
            if not self._is_allowed_candidate_url(url, list_url):
                return None
            if not (self._is_article_link(title, url) or has_inline_content):
                return None
            url = self._normalize_url_encoding(url).replace('+', '%20')
            source_method = 'network_json_inline' if has_inline_content else 'network_json'
        elif has_inline_content:
            url = self._make_synthetic_article_url(list_url, item, title, publish_date, content)
            source_method = 'network_json_inline'
        else:
            url = self._build_site_candidate_url(item, response_url, list_url)
            if not url:
                return None
            source_method = 'network_json'

        candidate = {
            'title': title,
            'url': url,
            'text': title,
            'publish_date': publish_date,
            'source_method': source_method,
            'discovery_source_url': response_url,
        }
        if has_inline_content:
            candidate['content_hint'] = content
            candidate['content_hint_source'] = response_url
        author = self._first_json_string(item, ('author', 'authors', 'creator', 'byline'))
        if author:
            candidate['authors'] = [self._clean_json_text(author, max_len=100)]
        return candidate

    async def _extract_attribute_candidates_from_current_page(
        self,
        page,
        base_url: str,
        base_domain: str,
        seen_urls: set,
    ) -> List[Dict]:
        links = []
        attrs = [
            'data-url', 'data-href', 'data-link', 'data-target', 'data-permalink',
            'data-share-url', 'data-article-url', 'data-detail-url', 'data-path'
        ]
        selector = ', '.join(f'[{attr}]' for attr in attrs) + ', [onclick]'

        try:
            elements = await page.query_selector_all(selector)
        except Exception:
            return links

        for elem in elements[:2000]:
            try:
                raw_urls = []
                for attr in attrs:
                    value = await elem.get_attribute(attr)
                    if value:
                        raw_urls.append(value.strip())

                onclick = await elem.get_attribute('onclick') or ''
                raw_urls.extend(self._extract_urls_from_js(onclick))
                if not raw_urls:
                    continue

                text = await elem.text_content() or ''
                title = text.strip()[:200]

                for raw_url in raw_urls:
                    absolute_url = urljoin(base_url, raw_url)
                    if not self._is_same_site_like(absolute_url, base_url):
                        continue
                    absolute_url = self._normalize_url_encoding(absolute_url).replace('+', '%20')
                    if absolute_url in seen_urls:
                        continue
                    if not self._is_article_link(title, absolute_url):
                        continue

                    seen_urls.add(absolute_url)
                    links.append({
                        'title': title or self._guess_title_from_url(absolute_url),
                        'url': absolute_url,
                        'publish_date': await self._extract_publish_date_from_element(elem),
                        'source_method': 'playwright_attribute',
                    })
            except Exception:
                continue

        return links

    def _iter_json_dicts(self, obj, max_nodes: int = 10000):
        stack = [obj]
        seen = set()
        count = 0
        while stack and count < max_nodes:
            current = stack.pop()
            count += 1
            obj_id = id(current)
            if obj_id in seen:
                continue
            seen.add(obj_id)

            if isinstance(current, dict):
                yield current
                for value in current.values():
                    if isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(current, list):
                for value in reversed(current):
                    if isinstance(value, (dict, list)):
                        stack.append(value)

    def _first_json_string(self, item: Dict, keys: tuple) -> str:
        for key in keys:
            if key in item:
                value = item.get(key)
                text = self._json_value_to_text(value)
                if text:
                    return text
        wanted = {key.lower() for key in keys}
        for key, value in item.items():
            if str(key).lower() in wanted:
                text = self._json_value_to_text(value)
                if text:
                    return text
        return ''

    def _first_json_url(self, item: Dict, keys: tuple) -> str:
        for key in keys:
            if key in item:
                url = self._json_value_to_url(item.get(key))
                if url:
                    return url
        wanted = {key.lower() for key in keys}
        for key, value in item.items():
            if str(key).lower() in wanted:
                url = self._json_value_to_url(value)
                if url:
                    return url
        return ''

    def _json_value_to_text(self, value) -> str:
        if value is None:
            return ''
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, dict):
            for key in ('name', 'title', 'headline', 'text', 'content', 'url', '@id', 'id'):
                text = self._json_value_to_text(value.get(key))
                if text:
                    return text
        if isinstance(value, list):
            parts = []
            for entry in value[:8]:
                text = self._json_value_to_text(entry)
                if text:
                    parts.append(text)
            return ' '.join(parts).strip()
        return ''

    def _json_value_to_url(self, value) -> str:
        if isinstance(value, str):
            return value.replace('\\/', '/').strip()
        if isinstance(value, dict):
            for key in ('url', 'href', 'link', '@id', 'path', 'slug'):
                url = self._json_value_to_url(value.get(key))
                if url:
                    return url
        if isinstance(value, list):
            for entry in value:
                url = self._json_value_to_url(entry)
                if url:
                    return url
        return ''

    def _normalize_json_candidate_url(self, raw_url: str, response_url: str, list_url: str) -> str:
        raw_url = (raw_url or '').strip()
        if not raw_url:
            return ''
        if raw_url.startswith(('javascript:', 'mailto:', 'tel:', '#')):
            return ''
        if re.fullmatch(r'\d{1,12}', raw_url):
            return ''
        if re.match(r'^[A-Za-z0-9_-]{1,80}$', raw_url) and '/' not in raw_url and '.' not in raw_url:
            return ''
        if raw_url.startswith(('http://', 'https://', '//')):
            return urljoin(list_url, raw_url)
        return urljoin(list_url, raw_url)

    def _make_synthetic_article_url(
        self,
        list_url: str,
        item: Dict,
        title: str,
        publish_date: Optional[str],
        content: str,
    ) -> str:
        stable_id = self._first_json_string(item, ('id', 'uuid', 'guid', 'articleId', 'newsId', 'nid', 'feed_id', 'slug'))
        seed = '|'.join([stable_id, title or '', publish_date or '', content[:500]])
        digest = hashlib.sha1(seed.encode('utf-8', errors='ignore')).hexdigest()[:16]
        return f"{list_url.split('#')[0]}#network-article-{digest}"

    def _clean_json_text(self, text: str, max_len: int = 1000) -> str:
        if not text:
            return ''
        text = str(text)
        if '<' in text and '>' in text:
            text = re.sub(r'(?i)<br\s*/?>', '\n', text)
            text = re.sub(r'<[^>]+>', ' ', text)
        text = html_lib.unescape(text).replace('\xa0', ' ')
        text = re.sub(r'\r\n?', '\n', text)
        text = re.sub(r'[ \t\f\v]+', ' ', text)
        text = re.sub(r'\n\s*\n+', '\n\n', text)
        text = '\n'.join(line.strip() for line in text.splitlines())
        return text.strip()[:max_len]

    def _looks_like_article_title(self, title: str) -> bool:
        title = (title or '').strip()
        if len(title) < 5 or len(title) > 200:
            return False
        lowered = title.lower()
        if lowered in {'more', 'read more', 'view all', 'login', 'search', 'home', 'next'}:
            return False
        if re.fullmatch(r'[\d\s.,/%+-]+', title):
            return False
        cjk = len(re.findall(r'[\u4e00-\u9fff]', title))
        words = len(re.findall(r'\b[A-Za-z][A-Za-z0-9\'-]{2,}\b', title))
        return cjk >= 4 or words >= 3 or len(title) >= 12

    def _title_from_content(self, content: str) -> str:
        for line in (content or '').splitlines():
            line = line.strip()
            if self._looks_like_article_title(line):
                return line[:200]
        return (content or '').strip()[:80]

    def _parse_date_from_text(self, text: str) -> Optional[str]:
        if not text:
            return None
        text = str(text)
        patterns = [
            r'(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})',
            r'(20\d{2})年(\d{1,2})月(\d{1,2})日?',
            r'(20\d{2})(\d{2})(\d{2})',
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            year, month, day = match.groups()
            try:
                return datetime(int(year), int(month), int(day)).strftime('%Y-%m-%d')
            except ValueError:
                continue
        return None

    def _extract_urls_from_js(self, js_text: str) -> List[str]:
        if not js_text:
            return []
        urls = []
        patterns = [
            r"(?:location\.href|window\.location|document\.location)\s*=\s*['\"]([^'\"]+)['\"]",
            r"(?:open|navigate|push|replace)\(\s*['\"]([^'\"]+)['\"]",
            r"['\"]((?:https?:\\?/\\?/|/)[^'\"\s<>]{6,260})['\"]",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, js_text, flags=re.IGNORECASE):
                urls.append(match.group(1).replace('\\/', '/'))
        return urls

    def _looks_like_json_endpoint(self, url: str) -> bool:
        url_lower = (url or '').lower()
        return any(token in url_lower for token in (
            '/api/', '/graphql', '/json', '/ajax', '/rest/', '/feed',
            'article', 'news', 'post', 'content', 'publication', 'list',
            '/webpage?', 'action=dynamic', 'action=index', 'action=home'
        ))

    def _parse_json_like_response(self, text: str):
        stripped = (text or '').strip()
        if not stripped:
            return None, 'empty'
        if stripped.startswith('{') or stripped.startswith('['):
            try:
                return json.loads(stripped), 'json'
            except Exception:
                return None, 'json_error'

        jsonp_match = re.match(r'^[\w.$]+\s*\(\s*(.*)\s*\)\s*;?\s*$', stripped, flags=re.DOTALL)
        if jsonp_match:
            payload = jsonp_match.group(1).strip()
            try:
                return json.loads(payload), 'jsonp'
            except Exception:
                return None, 'jsonp_error'

        assignment_match = re.search(
            r'(?:window\.)?[\w$][\w.$]*\s*=\s*({.*?}|\[.*?\])\s*;?\s*(?:</script>)?$',
            stripped,
            flags=re.DOTALL,
        )
        if assignment_match:
            try:
                return json.loads(assignment_match.group(1)), 'js_assignment'
            except Exception:
                return None, 'js_assignment_error'

        return None, 'not_json'

    def _collect_verification_signals(self, data, response_url: str):
        for item in self._iter_json_dicts(data, max_nodes=3000):
            foe = item.get('foe') if isinstance(item, dict) else None
            if isinstance(foe, dict) and foe.get('is_need_foe'):
                self._add_verification_signal('baidu_foe_verification_required', response_url)
            for key in ('verify', 'verification', 'captcha', 'login_required', 'need_login'):
                if key in item and item.get(key):
                    self._add_verification_signal(f'{key}_required', response_url)

    def _add_verification_signal(self, reason: str, response_url: str):
        signals = getattr(self, '_network_verification_signals', [])
        entry = {'reason': reason, 'url': response_url}
        if entry not in signals and len(signals) < 10:
            signals.append(entry)
        self._network_verification_signals = signals

    def _remember_network_sample(self, response_url: str, status: int, content_type: str, text: str):
        samples = getattr(self, '_network_response_samples', [])
        if len(samples) >= 20:
            return
        parsed = urlparse(response_url)
        samples.append({
            'host': parsed.netloc,
            'path': parsed.path[:120],
            'query_keys': sorted(parse_qs(parsed.query).keys())[:20],
            'status': status,
            'content_type': content_type[:80],
            'length': len(text or ''),
            'looks_like_html_error': self._looks_like_html_error(text),
        })
        self._network_response_samples = samples

    def _record_network_endpoint(self, response_url: str, parse_mode: str):
        parsed = urlparse(response_url)
        key = f"{parsed.netloc}{parsed.path}"
        stats = getattr(self, '_network_endpoint_stats', {})
        item = stats.setdefault(key, {'checked': 0, 'parse_modes': {}})
        item['checked'] += 1
        modes = item.setdefault('parse_modes', {})
        modes[parse_mode] = modes.get(parse_mode, 0) + 1
        self._network_endpoint_stats = stats

    def _looks_like_html_error(self, text: str) -> bool:
        lowered = (text or '').lower()[:2000]
        return '<html' in lowered and any(token in lowered for token in (
            '404', 'not found', '页面不存在', '迷路', '验证', 'captcha', 'login'
        ))

    def _detect_site_profile(self, list_url: str) -> str:
        host = (urlparse(list_url).hostname or '').lower()
        if host == 'author.baidu.com':
            return 'baidu_author'
        return 'generic'

    def _is_allowed_network_source(self, response_url: str, list_url: str) -> bool:
        if self._is_same_site_like(response_url, list_url):
            return True
        profile = getattr(self, '_site_profile', 'generic')
        host = (urlparse(response_url).hostname or '').lower()
        if profile == 'baidu_author':
            return host in {'mbd.baidu.com', 'author.baidu.com', 'ext.baidu.com'} or host.endswith('.bdstatic.com')
        return False

    def _is_allowed_candidate_url(self, candidate_url: str, list_url: str) -> bool:
        if self._is_same_site_like(candidate_url, list_url):
            return True
        profile = getattr(self, '_site_profile', 'generic')
        host = (urlparse(candidate_url).hostname or '').lower()
        if profile == 'baidu_author':
            return host in {'baijiahao.baidu.com', 'mbd.baidu.com', 'author.baidu.com'}
        return False

    def _build_site_candidate_url(self, item: Dict, response_url: str, list_url: str) -> str:
        profile = getattr(self, '_site_profile', 'generic')
        if profile != 'baidu_author':
            return ''
        article_id = self._first_json_string(item, ('nid', 'feed_id', 'id', 'article_id', 'articleId'))
        article_id = (article_id or '').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{6,80}', article_id):
            return ''
        if article_id.isdigit() and len(article_id) >= 10:
            return f"https://baijiahao.baidu.com/s?id={quote(article_id)}"
        return f"{list_url.split('#')[0]}#baidu-network-{quote(article_id)}"

    def _is_same_site_like(self, url: str, base_url: str) -> bool:
        host = (urlparse(url).hostname or '').lower()
        base_host = (urlparse(base_url).hostname or '').lower()
        if not host or not base_host:
            return False

        def normalize(hostname: str) -> str:
            for prefix in ('www.', 'm.', 'mobile.', 'amp.'):
                if hostname.startswith(prefix):
                    return hostname[len(prefix):]
            return hostname

        host_norm = normalize(host)
        base_norm = normalize(base_host)
        return (
            host_norm == base_norm
            or host_norm.endswith('.' + base_norm)
            or base_norm.endswith('.' + host_norm)
        )

    def _guess_title_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        segment = parsed.path.rstrip('/').split('/')[-1] if parsed.path else parsed.netloc
        segment = re.sub(r'\.html?$', '', segment, flags=re.IGNORECASE)
        return re.sub(r'[-_]+', ' ', unquote(segment)).strip()[:200] or url

    def _safe_int(self, value) -> int:
        try:
            return int(value)
        except Exception:
            return 0

    def _count_network_inline_candidates(self) -> int:
        return sum(
            1
            for item in getattr(self, '_network_article_candidates', []) or []
            if item.get('content_hint')
        )
    
    async def _detect_pagination(self, page) -> Dict:
        """
        🚀 超级通用分页检测系统 - 兼容99%的网站
        
        检测顺序（优先级从高到低）：
        1. Tab切换（年份、分类等）
        2. 文本指示器（"共X页"、"Total: X pages"）
        3. 数字分页按钮/链接
        4. 下一页按钮
        5. URL参数分页
        6. 无限滚动/加载更多
        
        Returns:
            Dict: 分页信息
        """
        pagination_info = {
            'total_pages': 1,
            'pagination_type': 'none',
            'tab_selectors': [],
            'pagination_element': None
        }
        
        try:
            page_content = await page.content()
            current_url = page.url
            
            # 🎯 优先级1: 检测Tab切换（最高优先级）
            tab_info = await self._detect_tabs(page, page_content)
            if tab_info['has_tabs']:
                pagination_info.update(tab_info)
                print(f"   🎯 检测到Tab切换: {len(tab_info.get('tab_elements', []))} 个标签")
                return pagination_info
            
            # 🔢 优先级2: 数字分页按钮（最可靠，优先于文本指示器）
            pagination_containers = [
                # 标准容器
                '.pagination', '.pager', '.page-list', '.page-nav', '.page-navigation',
                '.paginator', '.paging', '.page-numbers', '.wp-pagenavi',
                # 属性匹配
                '[class*="pag"]', '[class*="page"]', '[id*="pag"]', '[id*="page"]',
                # ARIA标签
                'nav[aria-label*="page" i]', 'nav[aria-label*="pag" i]',
                '[role="navigation"]',
                # 通用导航
                'nav', 'ul.nav', 'div.nav',
                # 中文
                'div:has-text("页")', 'div:has-text("頁")',
            ]
            
            print(f"   🔢 查找数字分页...")
            best_pagination = None
            max_page_num = 0
            
            for selector in pagination_containers:
                try:
                    container = await page.query_selector(selector)
                    if not container:
                        continue
                    
                    # 获取容器中的所有链接和按钮
                    elements = await container.query_selector_all('a, button, span, li')
                    page_numbers = []
                    page_texts = []
                    
                    for elem in elements:
                        text = await elem.text_content()
                        if not text:
                            continue
                        
                        text = text.strip()
                        page_texts.append(text)
                        
                        # 提取数字
                        if text.isdigit():
                            num = int(text)
                            if 1 <= num <= 999:  # 合理的页码范围
                                page_numbers.append(num)
                        # 也尝试从文本中提取数字（如 "第2页"）
                        elif re.search(r'\d+', text):
                            nums = re.findall(r'\d+', text)
                            for n in nums:
                                num = int(n)
                                if 1 <= num <= 999:
                                    page_numbers.append(num)
                    
                    if page_numbers:
                        detected_max = max(page_numbers)
                        if detected_max > max_page_num:
                            max_page_num = detected_max
                            best_pagination = {
                                'container': selector,
                                'page_numbers': page_numbers,
                                'page_texts': page_texts[:10]
                            }
                    
                except Exception:
                    continue
            
            if best_pagination and max_page_num > 1:
                pagination_info['total_pages'] = max_page_num
                pagination_info['pagination_type'] = 'numbered_buttons'
                print(f"   ✅ 数字分页检测到: {max_page_num} 页")
                print(f"      容器: {best_pagination['container']}")
                print(f"      页码: {sorted(set(best_pagination['page_numbers']))}")
                return pagination_info
            
            # 📝 优先级3: 文本指示器（作为备选，可能不准确）
            text_patterns = [
                # 中文
                r'共\s*(\d+)\s*页',
                r'总共\s*(\d+)\s*页',
                r'总页数[：:]\s*(\d+)',
                r'(\d+)\s*页\s*/',
                # 英文
                r'[Tt]otal\s*[Pp]ages?[：:]\s*(\d+)',
                r'[Pp]age\s+\d+\s+of\s+(\d+)',
                r'(\d+)\s+pages?\s+total',
                # 数字范围
                r'(\d+)\s*/\s*(\d+)',  # "1/10"
            ]
            
            for pattern in text_patterns:
                match = re.search(pattern, page_content)
                if match:
                    total_pages = int(match.group(1) if len(match.groups()) == 1 else match.group(2))
                    if total_pages > 1 and total_pages < 1000:  # 合理范围
                        pagination_info['total_pages'] = total_pages
                        pagination_info['pagination_type'] = 'text_indicator'
                        print(f"   📝 文本指示器检测到: {total_pages} 页（可能不准确，建议检查实际分页）")
                        return pagination_info
            
            # ➡️ 优先级4: "下一页"按钮（至少说明有2页）
            next_page_patterns = [
                # 中文
                'text="下一页"', 'text="下一頁"', 'text="下页"', 'text="次ページ"',
                'text="下一步"', 'text="后一页"',
                # 英文
                'text="Next"', 'text="Next Page"', 'text="next"', 
                # 符号
                'text="»"', 'text="›"', 'text=">"', 'text="→"',
                # 属性
                'a[rel="next"]', 'link[rel="next"]',
                'button[aria-label*="next" i]', 'a[aria-label*="next" i]',
                'a[title*="next" i]', 'a[title*="下一页" i]',
                # Class
                '.next-page', '.next', 'a.next', 'button.next',
                '[class*="next"]:not([class*="disabled"]):not([disabled])',
            ]
            
            for selector in next_page_patterns:
                try:
                    elem = await page.query_selector(selector)
                    if elem:
                        # 检查是否可用
                        class_name = await elem.get_attribute('class') or ''
                        is_disabled = 'disabled' in class_name.lower()
                        
                        if not is_disabled:
                            is_visible = await elem.is_visible()
                            if is_visible:
                                pagination_info['total_pages'] = 999  # 未知页数，设置为大数
                                pagination_info['pagination_type'] = 'next_button'
                                print(f'   ➡️ 检测到"下一页"按钮 (未知总页数)')
                                return pagination_info
                except:
                    continue
            
            # 🔗 优先级5: URL参数分页
            url_page_patterns = [
                r'[?&]page=(\d+)',
                r'[?&]p=(\d+)',
                r'[?&]pageNum=(\d+)',
                r'[?&]pageNo=(\d+)',
                r'[?&]pg=(\d+)',
                r'[?&]paged=(\d+)',
                r'/page/(\d+)',
                r'/p(\d+)',
            ]
            
            for pattern in url_page_patterns:
                match = re.search(pattern, current_url)
                if match:
                    current_page_num = int(match.group(1))
                    # 如果URL中有页码，说明支持URL分页
                    pagination_info['total_pages'] = 999  # 未知，设大数
                    pagination_info['pagination_type'] = 'url_parameter'
                    pagination_info['url_pattern'] = pattern
                    print(f"   🔗 检测到URL参数分页 (当前: 第{current_page_num}页)")
                    return pagination_info
            
            # 📜 优先级6: 无限滚动/加载更多按钮
            load_more_patterns = [
                'text="加载更多"', 'text="載入更多"', 'text="查看更多"',
                'text="Load More"', 'text="Show More"', 'text="More"',
                '.load-more', '.show-more', '[class*="load-more"]',
                'button:has-text("更多")', 'a:has-text("更多")',
            ]
            
            for selector in load_more_patterns:
                try:
                    elem = await page.query_selector(selector)
                    if elem:
                        is_visible = await elem.is_visible()
                        if is_visible:
                            pagination_info['total_pages'] = 999
                            pagination_info['pagination_type'] = 'load_more'
                            print(f'   📜 检测到"加载更多"按钮')
                            return pagination_info
                except:
                    continue
            
            # ❌ 未检测到分页
            print(f"   ℹ️ 未检测到分页（单页或需要手动指定）")
            
        except Exception as e:
            print(f"   ⚠️ 分页检测异常: {e}")
            import traceback
            traceback.print_exc()
        
        return pagination_info
    
    async def _detect_tabs(self, page, page_content: str) -> Dict:
        """
        🎯 超级通用Tab切换检测系统
        
        支持检测：
        1. Bootstrap标签页 (nav-tabs, nav-pills)
        2. 年份分类 (2024, 2023, 2022...)
        3. 分类标签 (全部, 最新, 热门, 推荐)
        4. 行业/领域分类
        5. Select下拉框切换
        6. 自定义Tab样式
        
        Returns:
            Dict: Tab信息
        """
        tab_info = {
            'has_tabs': False,
            'pagination_type': 'none',
            'total_pages': 1,
            'tab_selectors': [],
            'tab_elements': []
        }
        
        try:
            # 🔍 方法1: 标准Tab容器
            tab_container_patterns = [
                # Bootstrap & 常见框架
                {'selector': 'ul.nav-tabs', 'name': 'Bootstrap Tabs'},
                {'selector': 'ul.nav-pills', 'name': 'Bootstrap Pills'},
                {'selector': '.tabs', 'name': 'Generic Tabs'},
                {'selector': '.tab-list', 'name': 'Tab List'},
                {'selector': '[role="tablist"]', 'name': 'ARIA Tablist'},
                # 自定义样式
                {'selector': '[class*="tab-"]', 'name': 'Custom Tab'},
                {'selector': '[class*="category-"]', 'name': 'Category Tabs'},
                {'selector': '[class*="filter-"]', 'name': 'Filter Tabs'},
                # 中文
                {'selector': '[class*="标签"]', 'name': 'Chinese Tab'},
                {'selector': '[class*="分类"]', 'name': 'Chinese Category'},
            ]
            
            for pattern in tab_container_patterns:
                try:
                    container = await page.query_selector(pattern['selector'])
                    if not container:
                        continue
                    
                    # 查找Tab元素
                    tab_candidates = await container.query_selector_all(
                        'a, button, li[role="tab"], div[role="tab"], span[role="tab"], '
                        '[class*="tab-item"], [class*="tab-button"], '
                        '[data-tab], [data-toggle="tab"]'
                    )
                    
                    if len(tab_candidates) < 2:
                        continue
                    
                    # 验证是否为有效的Tab
                    valid_tabs = []
                    tab_texts = []
                    
                    for tab in tab_candidates:
                        text = await tab.text_content()
                        if not text:
                            continue
                        
                        text = text.strip()
                        if len(text) < 1 or len(text) > 50:  # 合理的文本长度
                            continue
                        
                        tab_texts.append(text)
                        
                        # 检查是否是有效Tab（不是纯装饰性元素）
                        # 1. 年份
                        if re.search(r'20\d{2}', text):
                            valid_tabs.append(tab)
                            continue
                        
                        # 2. 常见分类关键词
                        category_keywords = [
                            # 通用
                            '全部', '全部', '最新', '热门', '推荐', '精选', '置顶',
                            'All', 'Latest', 'Popular', 'Featured', 'Top',
                            # 时间相关
                            '本周', '本月', '本年', '今天', '昨天',
                            'Today', 'This Week', 'This Month', 'This Year',
                            # 状态
                            '已发布', '草稿', '归档',
                            'Published', 'Draft', 'Archived',
                            # 行业/领域（可扩展）
                            '新闻', '公告', '资讯', '动态', '活动', '通知',
                            'News', 'Updates', 'Events', 'Announcements',
                            # 🔥 新增：新闻/文章相关
                            '业绩', '声誉', '法评', '人文', '发布', '文章',
                        ]
                        
                        # 🔥 新增：排除非新闻类的导航 Tab
                        exclude_keywords = [
                            '关于', '概览', '办公', '网络', '平台', '荣誉', '排名', '奖项',
                            '榜单', '责任', 'ESG', '报告', '领域', '人员', '专业领域', '专业人员',
                            'About', 'Overview', 'Office', 'Network', 'Platform', 'Awards',
                            'Ranking', 'Team', 'People', 'Practice', 'Professionals'
                        ]
                        
                        # 检查是否是需要排除的 Tab
                        if any(keyword in text for keyword in exclude_keywords):
                            continue
                        
                        if any(keyword in text for keyword in category_keywords):
                            valid_tabs.append(tab)
                            continue
                        
                        # 3. 如果有data-tab或类似属性，也认为是有效Tab
                        has_tab_attr = await tab.get_attribute('data-tab') or \
                                       await tab.get_attribute('data-toggle') or \
                                       await tab.get_attribute('data-target')
                        if has_tab_attr:
                            valid_tabs.append(tab)
                            continue
                        
                        # 不再仅凭短文本判断为Tab。普通站点导航也常是短文本，
                        # 误判后会跳到关于我们/团队等页面，导致栏目抓取变慢且偏离主题。
                    
                    # 如果找到至少2个有效Tab
                    if len(valid_tabs) >= 2:
                        # 🔥 修复：保存选择器和文本信息，而不是元素引用
                        tab_info['has_tabs'] = True
                        tab_info['pagination_type'] = 'tab_navigation'
                        tab_info['total_pages'] = len(valid_tabs)
                        tab_info['tab_elements'] = valid_tabs  # 仍然保存初次的元素引用
                        tab_info['tab_selector'] = pattern['selector']  # 🔥 新增：保存容器选择器
                        tab_info['tab_texts'] = tab_texts  # 🔥 新增：保存Tab文本列表
                        tab_info['tab_pattern'] = pattern['name']
                        
                        print(f"   🎯 检测到 {pattern['name']}: {len(valid_tabs)} 个标签")
                        print(f"      标签: {', '.join(tab_texts[:8])}{'...' if len(tab_texts) > 8 else ''}")
                        return tab_info
                    
                except Exception:
                    continue
            
            # 🔍 方法2: 年份链接（即使不在容器中）
            try:
                year_links = await page.query_selector_all('a')
                year_tabs = []
                seen_years = set()
                
                for link in year_links:
                    try:
                        text = await link.text_content()
                        if not text:
                            continue
                        
                        text = text.strip()
                        
                        # 精确匹配年份（避免误判）
                        year_match = re.fullmatch(r'(20\d{2})', text)
                        if year_match:
                            year = year_match.group(1)
                            current_year = datetime.now().year
                            year_int = int(year)
                            
                            # 合理的年份范围（2000-当前年份+1）
                            if 2000 <= year_int <= current_year + 1:
                                if year not in seen_years:
                                    seen_years.add(year)
                                    year_tabs.append(link)
                    except:
                        continue
                
                if len(year_tabs) >= 2:
                    tab_info['has_tabs'] = True
                    tab_info['pagination_type'] = 'year_tabs'
                    tab_info['total_pages'] = len(year_tabs)
                    tab_info['tab_elements'] = year_tabs
                    
                    print(f"   🎯 检测到年份标签: {len(year_tabs)} 个年份 ({', '.join(sorted(seen_years, reverse=True))})")
                    return tab_info
            except:
                pass
            
            # 🔍 方法3: Select下拉框切换
            try:
                selects = await page.query_selector_all('select')
                for select_elem in selects:
                    try:
                        # 获取select的name/id，判断是否是分类/筛选相关
                        select_name = await select_elem.get_attribute('name') or ''
                        select_id = await select_elem.get_attribute('id') or ''
                        
                        filter_indicators = ['category', 'filter', 'type', 'year', '分类', '筛选', '类型']
                        is_filter_select = any(ind in select_name.lower() or ind in select_id.lower() 
                                              for ind in filter_indicators)
                        
                        if is_filter_select:
                            # 获取option数量
                            options = await select_elem.query_selector_all('option')
                            if len(options) >= 2:
                                option_texts = []
                                for opt in options[:10]:  # 最多显示10个
                                    opt_text = await opt.text_content()
                                    if opt_text:
                                        option_texts.append(opt_text.strip())
                                
                                tab_info['has_tabs'] = True
                                tab_info['pagination_type'] = 'select_dropdown'
                                tab_info['total_pages'] = len(options)
                                tab_info['tab_elements'] = options
                                tab_info['select_element'] = select_elem
                                
                                print(f"   🎯 检测到下拉框筛选: {len(options)} 个选项")
                                print(f"      选项: {', '.join(option_texts[:5])}{'...' if len(option_texts) > 5 else ''}")
                                return tab_info
                    except:
                        continue
            except:
                pass
            
        except Exception as e:
            print(f"   ⚠️ Tab检测异常: {e}")
        
        return tab_info
    
    async def _extract_links_from_page(self, page, base_url: str, base_domain: str) -> tuple:
        """
        从当前页面提取文章链接（支持跟随"更多"链接）
        
        Returns:
            tuple: (链接列表, 是否聚合了"更多"链接)
        """
        import time
        start_time = time.time()
        links = []
        seen_urls = set()
        self._crawled_more_links = False  # 🔥 重置标记
        
        try:
            print(f"   📝 [日志] 开始提取链接: {base_url[:80]}...")
            
            # 🔥 新增：先检测并跟随"更多"链接
            print(f"   📝 [日志] 步骤1: 检测'更多'链接...")
            find_more_start = time.time()
            more_links = await self._find_more_links(page, base_url, base_domain)
            print(f"   📝 [日志] 步骤1完成，耗时: {time.time() - find_more_start:.2f}秒")
            
            if more_links:
                print(f"   🔗 检测到 {len(more_links)} 个'更多'链接，将跟随聚合...")
                self._crawled_more_links = True  # 🔥 设置标记
                for idx, more_link in enumerate(more_links, 1):
                    print(f"      📝 [日志] 处理第 {idx}/{len(more_links)} 个'更多'链接...")
                    print(f"      ➡️ 跟随: {more_link['text']} -> {more_link['url'][:80]}...")
                    more_page = None
                    try:
                        # 在新标签页打开"更多"链接
                        print(f"         📝 [日志] 创建新标签页...")
                        more_page = await page.context.new_page()
                        
                        # 设置更短的超时时间，避免卡住
                        print(f"         📝 [日志] 正在访问'更多'页面（超时20秒）...")
                        goto_start = time.time()
                        await more_page.goto(more_link['url'], wait_until='domcontentloaded', timeout=20000)
                        print(f"         📝 [日志] 页面加载完成，耗时: {time.time() - goto_start:.2f}秒")
                        
                        print(f"         📝 [日志] 等待2秒让内容加载...")
                        await asyncio.sleep(2)
                        
                        # 🔥 新增：检测"更多"页面是否也有分页
                        print(f"         📝 [日志] 检测'更多'页面的分页...")
                        more_pagination = await self._detect_pagination(more_page)
                        more_total_pages = more_pagination.get('total_pages', 1)
                        
                        if more_total_pages > 1:
                            print(f"         📄 '更多'页面有 {more_total_pages} 页，将逐页提取...")
                            # 聚合所有页面，不限制
                            max_more_pages = more_total_pages
                            
                            for more_page_num in range(1, max_more_pages + 1):
                                print(f"            📖 '更多'页面第 {more_page_num}/{max_more_pages} 页")
                                
                                # 🔥 每10页重新创建页面对象，防止内存泄漏（第11、21、31...页）
                                if more_page_num > 1 and more_page_num % 10 == 1:
                                    print(f"            🔄 重新创建页面对象（防止内存泄漏）...")
                                    current_url = more_page.url
                                    try:
                                        if not more_page.is_closed():
                                            await more_page.close()
                                    except:
                                        pass
                                    
                                    # 创建新页面并导航到当前URL
                                    more_page = await page.context.new_page()
                                    
                                    # 🔥 增加超时时间和重试机制
                                    max_retries = 3
                                    for retry in range(max_retries):
                                        try:
                                            print(f"            📝 [日志] 尝试加载页面（第{retry+1}/{max_retries}次）...")
                                            await more_page.goto(current_url, wait_until='domcontentloaded', timeout=30000)
                                            
                                            # 等待页面完全加载
                                            await asyncio.sleep(3)
                                            
                                            # 验证页面是否正常加载（检查链接数量）
                                            test_links = await more_page.query_selector_all('a[href]')
                                            print(f"            📝 [日志] 页面加载完成，找到 {len(test_links)} 个链接")
                                            
                                            if len(test_links) < 10:
                                                print(f"            ⚠️ 页面链接数量异常（{len(test_links)}个），可能加载不完整")
                                                if retry < max_retries - 1:
                                                    print(f"            🔄 等待5秒后重试...")
                                                    await asyncio.sleep(5)
                                                    continue
                                            
                                            print(f"            ✅ 页面对象已重新创建")
                                            break
                                        except Exception as e:
                                            print(f"            ⚠️ 页面加载失败（第{retry+1}次）: {e}")
                                            if retry < max_retries - 1:
                                                print(f"            🔄 等待5秒后重试...")
                                                await asyncio.sleep(5)
                                            else:
                                                print(f"            ❌ 页面加载失败，已重试{max_retries}次，停止聚合")
                                                raise
                                
                                # 🔥 记录翻页前的URL，用于检测是否真正翻页成功
                                url_before_flip = more_page.url
                                
                                # 提取当前页链接
                                more_page_links = await self._extract_links_from_current_page(more_page, more_link['url'], base_domain, seen_urls)
                                
                                # 🔥 诊断：如果提取的链接数量异常少，输出警告
                                if len(more_page_links) < 3 and more_page_num > 1:
                                    print(f"            ⚠️ 警告：本页只提取到 {len(more_page_links)} 个链接，可能页面加载不完整")
                                    # 尝试重新加载页面
                                    print(f"            🔄 尝试重新加载页面...")
                                    try:
                                        await more_page.reload(wait_until='domcontentloaded', timeout=30000)
                                        await asyncio.sleep(3)
                                        # 重新提取
                                        more_page_links = await self._extract_links_from_current_page(more_page, more_link['url'], base_domain, seen_urls)
                                        print(f"            📝 重新加载后提取到 {len(more_page_links)} 个链接")
                                    except Exception as e:
                                        print(f"            ⚠️ 重新加载失败: {e}")
                                
                                # 🔥 如果连续3页都没有提取到新链接，才认为已经到底了
                                if len(more_page_links) == 0:
                                    if not hasattr(self, '_empty_page_count'):
                                        self._empty_page_count = 0
                                    self._empty_page_count += 1
                                    print(f"            ⚠️ 本页无新链接（连续{self._empty_page_count}页）")
                                    
                                    if self._empty_page_count >= 3:
                                        print(f"            ⚠️ 连续3页无新链接，可能已到最后一页，停止聚合")
                                        break
                                else:
                                    self._empty_page_count = 0  # 重置计数器
                                
                                links.extend(more_page_links)
                                print(f"            ✅ 提取了 {len(more_page_links)} 个链接")
                                
                                # 如果不是最后一页，翻页
                                if more_page_num < max_more_pages:
                                    print(f"            🔄 翻到第 {more_page_num + 1} 页...")
                                    success = await self._go_to_next_page(more_page, more_pagination, more_page_num)
                                    if not success:
                                        print(f"            ⚠️ 无法翻页，停止")
                                        break
                                    
                                    await asyncio.sleep(2)
                                    
                                    # 🔥 验证是否真正翻页成功（URL应该改变）
                                    url_after_flip = more_page.url
                                    if url_after_flip == url_before_flip:
                                        print(f"            ⚠️ 翻页后URL未改变，可能已到最后一页，停止聚合")
                                        break
                                    
                                    # 🔥 检测是否循环回到之前的页面（通过URL参数判断）
                                    import re
                                    page_num_match_before = re.search(r'[?&]page=(\d+)', url_before_flip)
                                    page_num_match_after = re.search(r'[?&]page=(\d+)', url_after_flip)
                                    
                                    if page_num_match_before and page_num_match_after:
                                        page_num_before = int(page_num_match_before.group(1))
                                        page_num_after = int(page_num_match_after.group(1))
                                        
                                        # 如果翻页后的页码小于等于翻页前，说明循环了
                                        if page_num_after <= page_num_before:
                                            print(f"            ⚠️ 检测到页码循环（{page_num_before} → {page_num_after}），停止聚合")
                                            break
                        else:
                            # 没有分页，直接提取
                            print(f"         📝 [日志] '更多'页面无分页，提取当前页...")
                            more_page_links = await self._extract_links_from_current_page(more_page, more_link['url'], base_domain, seen_urls)
                            links.extend(more_page_links)
                            print(f"      ✅ 从'更多'页面提取了 {len(more_page_links)} 个链接")
                        
                    except asyncio.TimeoutError:
                        print(f"      ⚠️ 跟随'更多'链接超时（20秒），跳过")
                    except Exception as e:
                        print(f"      ⚠️ 跟随'更多'链接失败: {e}")
                        import traceback
                        print(f"         📝 [日志] 错误详情:\n{traceback.format_exc()}")
                    finally:
                        # 确保关闭页面，避免资源泄漏
                        if more_page:
                            try:
                                print(f"         📝 [日志] 关闭'更多'页面...")
                                # 检查页面是否还有效
                                if not more_page.is_closed():
                                    await more_page.close()
                                    print(f"         📝 [日志] 页面已关闭")
                                else:
                                    print(f"         📝 [日志] 页面已经关闭，跳过")
                            except Exception as e:
                                # 静默处理关闭错误，不影响主流程
                                print(f"         📝 [日志] 关闭页面时出现异常（已忽略）: {str(e)[:100]}")
            else:
                print(f"   ℹ️  未检测到'更多'链接，直接提取当前页")
            
            # 提取当前页面的链接
            print(f"   📝 [日志] 步骤2: 提取当前页面的链接...")
            current_start = time.time()
            current_page_links = await self._extract_links_from_current_page(page, base_url, base_domain, seen_urls)
            print(f"   📝 [日志] 步骤2完成，耗时: {time.time() - current_start:.2f}秒")
            
            links.extend(current_page_links)
            print(f"   📝 [日志] 当前页面提取了 {len(current_page_links)} 个链接")

            print(f"   📝 [日志] 步骤3: 提取动态属性链接...")
            attr_start = time.time()
            attribute_links = await self._extract_attribute_candidates_from_current_page(
                page, base_url, base_domain, seen_urls
            )
            print(f"   📝 [日志] 步骤3完成，耗时: {time.time() - attr_start:.2f}秒")
            if attribute_links:
                links.extend(attribute_links)
                print(f"   📝 [日志] 动态属性链接补充 {len(attribute_links)} 个")
            
            total_time = time.time() - start_time
            print(f"   📝 [日志] 总计提取 {len(links)} 个链接，总耗时: {total_time:.2f}秒")
        
        except Exception as e:
            print(f"   ⚠️ 提取链接出错: {e}")
            import traceback
            print(f"   📝 [日志] 错误详情:\n{traceback.format_exc()}")
        
        # 🔥 返回链接列表和是否聚合了"更多"链接的标记
        has_more_links = hasattr(self, '_crawled_more_links') and self._crawled_more_links
        return links, has_more_links
    
    async def _find_more_links(self, page, base_url: str, base_domain: str) -> List[Dict]:
        """
        查找页面中的"更多"链接（会跳转到新页面的那种）
        
        专门用于列表页底部的"更多"/"查看更多"链接，不会误判文章标题
        
        Args:
            page: Playwright页面对象
            base_url: 基础URL
            base_domain: 基础域名
            
        Returns:
            List[Dict]: "更多"链接列表
        """
        import time
        more_links = []
        
        try:
            print(f"   🔍 查找'更多'链接...")
            print(f"      📝 [日志] 当前页面: {base_url[:80]}...")
            
            # 策略1: 查找页面底部的"更多"链接（最常见的位置）
            # 先滚动到页面底部，确保"更多"链接可见
            try:
                print(f"      📝 [日志] 正在滚动到页面底部（超时5秒）...")
                scroll_start = time.time()
                # 添加超时保护
                await asyncio.wait_for(
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)"),
                    timeout=5.0
                )
                print(f"      📝 [日志] 滚动完成，耗时: {time.time() - scroll_start:.2f}秒")
                await asyncio.sleep(1)
            except asyncio.TimeoutError:
                print(f"      ⚠️ 滚动页面超时（5秒），继续处理")
            except Exception as e:
                print(f"      ⚠️ 滚动页面失败: {e}")
            
            # 查找所有链接
            print(f"      📝 [日志] 正在查找所有链接...")
            find_start = time.time()
            all_links = await page.query_selector_all('a[href]')
            print(f"      📝 [日志] 找到 {len(all_links)} 个链接，耗时: {time.time() - find_start:.2f}秒")
            
            # 精确的"更多"文本模式（只匹配纯文本，避免误判文章标题）
            exact_more_patterns = [
                '更多', '查看更多', '查看全部', '显示更多', '加载更多',
                'more', 'view more', 'view all', 'show more', 'load more', 'see all'
            ]
            
            print(f"      📝 [日志] 开始遍历链接，查找'更多'...")
            checked_count = 0
            for link_elem in all_links:
                checked_count += 1
                if checked_count % 50 == 0:
                    print(f"      📝 [日志] 已检查 {checked_count}/{len(all_links)} 个链接...")
                try:
                    text = await link_elem.text_content()
                    href = await link_elem.get_attribute('href')
                    
                    if not text or not href:
                        continue
                    
                    text_stripped = text.strip()
                    text_lower = text_stripped.lower()
                    
                    # 🔥 关键：只匹配非常短的文本（"更多"链接通常只有2-10个字符）
                    if len(text_stripped) > 20:
                        continue  # 太长，可能是文章标题
                    
                    # 检查是否精确匹配"更多"文本
                    is_more_link = False
                    for pattern in exact_more_patterns:
                        # 精确匹配（忽略大小写）
                        if text_lower == pattern.lower():
                            is_more_link = True
                            break
                        # 或者文本很短（<=10字符）且包含关键词
                        elif len(text_stripped) <= 10 and pattern.lower() in text_lower:
                            is_more_link = True
                            break
                    
                    if not is_more_link:
                        continue
                    
                    # 处理相对链接
                    # 🔥 简单方案：直接使用urljoin，让后续的Playwright处理重定向
                    if not href.startswith('http'):
                        href = urljoin(base_url, href)
                    
                    # 只保留同域名的链接
                    if urlparse(href).netloc != base_domain:
                        continue
                    
                    # 排除锚点链接和JavaScript链接
                    if href.startswith('#') or href.startswith('javascript:'):
                        continue
                    
                    # 排除当前页面（避免循环）
                    if href == base_url or href.rstrip('/') == base_url.rstrip('/'):
                        continue

                    if not self._is_related_more_link(base_url, href):
                        print(f"      ⏭️ 跳过跨栏目'更多'链接: {text_stripped} -> {href[:80]}...")
                        continue
                    
                    # 🔥 简化策略：只要文本匹配就添加，不检查位置
                    # 因为位置检测可能误判（有些网站的"更多"链接在顶部）
                    more_links.append({
                        'title': text_stripped,
                        'url': href,
                        'text': text_stripped
                    })
                    print(f"      ✅ 发现'更多'链接: {text_stripped} -> {href[:80]}...")
                        
                except Exception as e:
                    continue
            
            print(f"      📝 [日志] 链接遍历完成，共检查 {checked_count} 个链接")
            
            if not more_links:
                print(f"      ℹ️  未找到'更多'链接")
            else:
                print(f"      📊 共找到 {len(more_links)} 个'更多'链接")
                for idx, link in enumerate(more_links, 1):
                    print(f"         {idx}. {link['text']} -> {link['url'][:80]}...")
                
        except Exception as e:
            print(f"      ⚠️ 查找'更多'链接失败: {e}")
            import traceback
            print(f"      📝 [日志] 错误详情:\n{traceback.format_exc()}")
        
        return more_links

    def _is_related_more_link(self, base_url: str, href: str) -> bool:
        try:
            base = urlparse(base_url)
            target = urlparse(href)
            if target.netloc and target.netloc != base.netloc:
                return False

            base_parts = [p for p in base.path.split('/') if p]
            target_parts = [p for p in target.path.split('/') if p]
            target_first = target_parts[0].lower() if target_parts else ''
            base_first = base_parts[0].lower() if base_parts else ''

            non_article_sections = {
                'about', 'contact', 'contacts', 'career', 'careers', 'join',
                'people', 'team', 'teams', 'professionals', 'lawyers',
                'practice', 'practices', 'service', 'services',
                'office', 'offices', 'network', 'overview', 'profile',
                'search', 'login', 'member', 'members', 'subscribe',
            }
            if target_first in non_article_sections:
                return False

            if base_first and target_first and target_first != base_first:
                return False

            return True
        except Exception:
            return True
    
    async def _extract_links_from_current_page(self, page, base_url: str, base_domain: str, seen_urls: set) -> List[Dict]:
        """
        从当前页面提取文章链接（不跟随"更多"链接）
        
        Args:
            page: Playwright页面对象
            base_url: 基础URL
            base_domain: 基础域名
            seen_urls: 已见过的URL集合（用于去重）
            
        Returns:
            List[Dict]: 文章链接列表
        """
        import time
        links = []
        
        try:
            print(f"         📝 [日志] 开始提取当前页面链接...")
            
            # 🔥 使用智能分析结果的主内容区选择器
            main_selector = getattr(self, '_main_content_selector', None)
            
            # 获取所有链接
            find_start = time.time()
            
            if main_selector:
                # 使用分析器识别的主内容区
                main_container = await page.query_selector(main_selector)
                if main_container:
                    all_links = await main_container.query_selector_all('a[href]')
                    print(f"         🎯 智能识别: 只在 {main_selector} 中提取链接")
                else:
                    # 如果主容器不存在，回退到全页面提取
                    all_links = await page.query_selector_all('a[href]')
                    print(f"         ⚠️ 主内容区 {main_selector} 未找到，回退到全页面提取")
            else:
                all_links = await page.query_selector_all('a[href]')
                print(f"         📝 使用全页面提取（未识别主内容区）")
            
            print(f"         📝 [日志] 找到 {len(all_links)} 个链接，耗时: {time.time() - find_start:.2f}秒")
            
            processed_count = 0
            for link_elem in all_links:
                processed_count += 1
                if processed_count % 100 == 0:
                    print(f"         📝 [日志] 已处理 {processed_count}/{len(all_links)} 个链接，已提取 {len(links)} 个文章链接...")
                try:
                    href = await link_elem.get_attribute('href')
                    text = await link_elem.text_content()
                    
                    if not href or not text:
                        continue
                    
                    # 转换为绝对URL，并修复常见栏目页/移动页误拼接格式。
                    absolute_url = self._resolve_article_href(base_url, href, processed_count)
                    if not absolute_url:
                        continue
                    
                    # 🔥 通用过滤：跳过图片和静态资源
                    static_patterns = ['/images/', '/img/', '/static/', 'static.', '.jpg', '.png', '.gif', '.css', '.js']
                    if any(pattern in absolute_url.lower() for pattern in static_patterns):
                        continue
                    
                    # 🔥 通用过滤：跳过移动版导航页
                    mobile_nav_patterns = ['/instantnewsmob/', '/mobilemob/', '/landing/mob', '/m/index', '/mobile/index']
                    if any(pattern in absolute_url.lower() for pattern in mobile_nav_patterns):
                        continue
                    
                    absolute_url = self._repair_common_article_url_format(
                        absolute_url,
                        base_url,
                        silent=True,
                    )
                    
                    # 🔥 方案2: 通用URL规范化（防止编码导致404）
                    absolute_url = self._normalize_url_encoding(absolute_url)
                    
                    # 🔥 修复URL编码：将 + 转为 %20（空格的正确编码）
                    absolute_url = absolute_url.replace('+', '%20')
                    
                    # 🔥 关键修复：URL修复后再去重（避免同一文章的多个链接被重复提取）
                    if absolute_url in seen_urls:
                        continue
                    
                    # 检查是否为同域名
                    link_domain = urlparse(absolute_url).netloc
                    if link_domain != base_domain:
                        continue
                    
                    # 使用智能过滤判断是否为文章链接
                    is_article = self._is_article_link(text, absolute_url)
                    
                    # 🔥 调试：记录被过滤的链接（每100个记录一次）
                    if not is_article and processed_count % 100 == 0:
                        print(f"         🔍 [调试] 链接被过滤: {text[:30]}... -> {absolute_url[:80]}...")
                    
                    if is_article:
                        # 🔥 方案3: 聚合前验证URL（通用，对所有包含编码的URL进行验证）
                        # 检测URL是否包含可能导致404的编码字符
                        if '%E' in absolute_url or '%e' in absolute_url or '%2' in absolute_url:
                            if not self._validate_url_before_crawl(absolute_url):
                                # 验证失败，尝试智能重试
                                retry_url = self._smart_retry_url(absolute_url, base_url=base_url)
                                if retry_url:
                                    absolute_url = retry_url
                                else:
                                    continue  # 重试也失败，跳过此链接
                        
                        # 🔥 标题清洗：跳过无效标题（空、"全文"等）
                        clean_title = text.strip()[:200] if text else ''
                        
                        # 跳过无意义的标题
                        skip_titles = ['全文', '更多', 'more', '...', '>>']
                        if not clean_title or clean_title.lower() in [t.lower() for t in skip_titles]:
                            if any(pattern in absolute_url.lower() for pattern in ['/article/', '/news/', '/post/', '/detail/', '/id/']):
                                from urllib.parse import unquote
                                path_parts = [part for part in urlparse(absolute_url).path.split('/') if part]
                                guessed_title = unquote(path_parts[-1]) if path_parts else ''
                                if not guessed_title or guessed_title.isdigit():
                                    guessed_title = absolute_url
                                clean_title = guessed_title[:200]
                            else:
                                continue
                        
                        # 尝试提取发布日期
                        publish_date = await self._extract_publish_date_from_element(link_elem)
                        clean_title = self._clean_article_link_title(clean_title, publish_date)
                        
                        seen_urls.add(absolute_url)
                        links.append({
                            'title': clean_title,
                            'url': absolute_url,
                            'publish_date': publish_date,
                            'source_method': 'playwright_dom'
                        })
                except Exception as e:
                    # 静默跳过单个链接的错误
                    continue
            
            print(f"         📝 [日志] 链接处理完成，共处理 {processed_count} 个，提取 {len(links)} 个文章链接")
            
            # 🔥 调试：打印前3个URL，确认格式
            if links:
                print(f"         🔍 [调试] 提取到的URL示例（前3个）:")
                for idx, link in enumerate(links[:3], 1):
                    url = link['url']
                    has_articlelist = '/articlelist/' in url
                    has_article_id = '/article/id/' in url
                    status = "❌ 错误" if has_articlelist else ("✅ 正确" if has_article_id else "⚠️ 未知")
                    print(f"            {idx}. {status} {url[:80]}")
        
        except Exception as e:
            print(f"   ⚠️ 提取链接出错: {e}")
            import traceback
            print(f"   📝 [日志] 错误详情:\n{traceback.format_exc()}")
        
        # 🔥 只返回链接列表（不返回has_more_links，那是_extract_links_from_page的返回值）
        return links

    def _clean_article_link_title(self, title: str, publish_date: Optional[str] = None) -> str:
        if not title:
            return ''
        cleaned = html_lib.unescape(str(title)).replace('\xa0', ' ')
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()

        date_patterns = [
            r'20\d{2}[./-]\d{1,2}[./-]\d{1,2}',
            r'20\d{2}年\d{1,2}月\d{1,2}日?',
        ]
        if publish_date:
            try:
                year, month, day = publish_date.split('-')[:3]
                month_i = int(month)
                day_i = int(day)
                date_patterns.extend([
                    rf'{year}[./-]0?{month_i}[./-]0?{day_i}',
                    rf'{year}年0?{month_i}月0?{day_i}日?',
                ])
            except Exception:
                pass

        for pattern in date_patterns:
            cleaned = re.sub(rf'\s*{pattern}\s*$', '', cleaned).strip()
        return cleaned or title.strip()
    
    async def _extract_publish_date_from_element(self, element) -> Optional[str]:
        """
        🔥 从元素附近提取发布日期 - 支持各种常见格式
        
        支持的格式：
        1. 标准格式: 2025-12-03, 2025/12/03, 2025.12.03
        2. 带时间: 2025/12/03 12:48
        3. 中文格式: 2025年12月3日
        4. 英文格式: Dec 3, 2025
        5. 相对时间: 1小時前, 2天前
        """
        try:
            from datetime import datetime, timedelta
            from utils import get_china_time
            
            # 获取父元素、祖父元素、曾祖父元素的文本
            parent = await element.evaluate_handle('el => el.parentElement')
            parent_text = await parent.as_element().text_content() if parent else ""
            
            grandparent = await element.evaluate_handle('el => el.parentElement?.parentElement')
            grandparent_text = await grandparent.as_element().text_content() if grandparent else ""
            
            great_grandparent = await element.evaluate_handle('el => el.parentElement?.parentElement?.parentElement')
            great_grandparent_text = await great_grandparent.as_element().text_content() if great_grandparent else ""
            
            combined_text = f"{parent_text} {grandparent_text} {great_grandparent_text}"
            now = get_china_time()
            
            # ===== 1. 相对时间 =====
            relative_patterns = [
                # X秒前
                (r'(\d+)\s*(秒|秒钟|秒鐘|seconds?)\s*(前|ago)?', lambda m: now - timedelta(seconds=int(m.group(1)))),
                # X分钟前
                (r'(\d+)\s*(分钟|分鐘|分|minutes?|mins?)\s*(前|ago)?', lambda m: now - timedelta(minutes=int(m.group(1)))),
                # X小时前
                (r'(\d+)\s*(小时|小時|时|時|hours?|hrs?)\s*(前|ago)?', lambda m: now - timedelta(hours=int(m.group(1)))),
                # X天前
                (r'(\d+)\s*(天|日|days?)\s*(前|ago)?', lambda m: now - timedelta(days=int(m.group(1)))),
            ]
            
            for pattern, calc_func in relative_patterns:
                match = re.search(pattern, combined_text, re.IGNORECASE)
                if match:
                    result_date = calc_func(match)
                    return result_date.strftime('%Y-%m-%d')
            
            # ===== 2. 绝对日期格式 =====
            date_patterns = [
                # 2025/12/03 12:48 (带时间)
                r'(\d{4})/(\d{1,2})/(\d{1,2})\s+\d{1,2}:\d{2}',
                # 2025-12-03 12:48 (带时间)
                r'(\d{4})-(\d{1,2})-(\d{1,2})\s+\d{1,2}:\d{2}',
                # 2025/12/03
                r'(\d{4})/(\d{1,2})/(\d{1,2})',
                # 2025-12-03
                r'(\d{4})-(\d{1,2})-(\d{1,2})',
                # 2025.12.03
                r'(\d{4})\.(\d{1,2})\.(\d{1,2})',
                # 2025年12月03日 或 2025年12月3日
                r'(\d{4})年(\d{1,2})月(\d{1,2})日?',
            ]
            
            for pattern in date_patterns:
                match = re.search(pattern, combined_text)
                if match:
                    year, month, day = match.groups()[:3]
                    return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
            
            # ===== 3. 英文日期格式 =====
            month_names = {
                'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
                'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6,
                'jul': 7, 'july': 7, 'aug': 8, 'august': 8, 'sep': 9, 'september': 9,
                'oct': 10, 'october': 10, 'nov': 11, 'november': 11, 'dec': 12, 'december': 12
            }
            
            # Dec 3, 2025
            en_mdy = re.search(r'([a-zA-Z]+)\s+(\d{1,2}),?\s+(\d{4})', combined_text)
            if en_mdy:
                month_str, day, year = en_mdy.groups()
                month = month_names.get(month_str.lower())
                if month:
                    return f"{year}-{str(month).zfill(2)}-{day.zfill(2)}"
            
            # 3 Dec 2025
            en_dmy = re.search(r'(\d{1,2})\s+([a-zA-Z]+)\s+(\d{4})', combined_text)
            if en_dmy:
                day, month_str, year = en_dmy.groups()
                month = month_names.get(month_str.lower())
                if month:
                    return f"{year}-{str(month).zfill(2)}-{day.zfill(2)}"
            
            # ===== 4. 无年份的中文日期（使用今年） =====
            cn_short = re.search(r'(\d{1,2})月(\d{1,2})日', combined_text)
            if cn_short:
                month, day = cn_short.groups()
                year = now.year
                if int(month) > now.month:
                    year -= 1
                return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
                
        except:
            pass
        
        return None
    
    def _resolve_article_href(self, base_url: str, href: str, processed_count: int = 0) -> str:
        """Resolve an href without letting bare栏目URL被当成文件路径。"""
        href = (href or '').strip()
        if not href:
            return ''
        if href.lower().startswith(('javascript:', 'mailto:', 'tel:', '#')):
            return ''

        base_for_join = base_url
        parsed_base = urlparse(base_url)
        if (
            not href.startswith(('/', './', '../'))
            and not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', href)
            and parsed_base.path
            and not parsed_base.path.endswith('/')
        ):
            last_segment = parsed_base.path.rsplit('/', 1)[-1]
            if '.' not in last_segment:
                base_for_join = urlunparse((
                    parsed_base.scheme,
                    parsed_base.netloc,
                    parsed_base.path.rstrip('/') + '/',
                    parsed_base.params,
                    parsed_base.query,
                    parsed_base.fragment,
                ))

        absolute_url = urljoin(base_for_join, href)
        return self._repair_common_article_url_format(
            absolute_url,
            base_url,
            silent=processed_count > 5,
        )

    def _repair_common_article_url_format(self, url: str, base_url: str = '', silent: bool = False) -> str:
        """
        修复常见文章URL误拼接，不绑定具体网站。

        覆盖几类高频情况：
        - /articlelist/article/123 -> /article/id/123
        - article/123 从栏目页拼接时保留栏目路径
        - /mobarticle*/id/123 尝试映射回栏目文章路径
        """
        if not url:
            return ''

        parsed = urlparse(url)
        path = parsed.path or '/'
        original_path = path

        list_marker_re = r'/(?:articlelist|newslist|postlist|storylist|list)/'
        match = re.search(list_marker_re + r'(article|news|post|story)/(\d+)(/.*)?$', path, re.IGNORECASE)
        if match:
            article_type, article_id, title_part = match.groups()
            prefix = path[:match.start()].rstrip('/')
            normalized_type = 'article' if article_type.lower() == 'article' else article_type.lower()
            if normalized_type == 'article':
                path = f"{prefix}/article/id/{article_id}{title_part or ''}"
            else:
                path = f"{prefix}/{normalized_type}/{article_id}{title_part or ''}"

        mobile_match = re.search(r'/(?:m|mobile|mob|wap|landing)/(?:mob)?article\w*/id/(\d+)(/.*)?$', path, re.IGNORECASE)
        if mobile_match and base_url:
            article_id, title_part = mobile_match.groups()
            section_parts = self._section_parts_from_base_url(base_url)
            if section_parts:
                section_path = '/'.join(section_parts[:2])
                path = f"/{section_path}/article/{article_id}{title_part or ''}".rstrip('/')

        if path != original_path:
            fixed_url = urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, parsed.fragment))
            if not silent:
                print(f"      🔧 通用URL修复: {original_path[:70]} -> {path[:70]}")
            return fixed_url

        return url

    def _section_parts_from_base_url(self, base_url: str) -> List[str]:
        parsed_base = urlparse(base_url or '')
        drop_segments = {
            'articlelist', 'newslist', 'postlist', 'storylist',
            'list', 'index', 'page', 'pages', 'm', 'mobile', 'mob', 'wap'
        }
        parts = []
        for part in parsed_base.path.strip('/').split('/'):
            if not part or part.lower() in drop_segments:
                continue
            if re.fullmatch(r'\d+', part):
                continue
            parts.append(part)
        return parts
    
    def _normalize_url_encoding(self, url: str) -> str:
        """
        🔥 方案2: 通用URL规范化（防止编码导致的404）
        
        清理URL中可能导致404的编码问题：
        1. 保留路径结构和slug
        2. 只规范化URL编码格式
        
        Args:
            url: 原始URL
            
        Returns:
            str: 规范化后的URL
        """
        parsed = urlparse(url)
        path = parsed.path

        try:
            # 只规范编码，不删除 slug。许多站点需要 /news/123/title 才能正常打开。
            decoded_path = unquote(path)
            normalized_path = quote(decoded_path, safe="/:@!$&'()*+,;=-._~")
            if normalized_path:
                path = normalized_path
        except Exception:
            pass

        return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, parsed.fragment))
    
    def _validate_url_before_crawl(self, url: str) -> bool:
        """
        🔥 方案3: 聚合前验证URL（防止404）
        
        Args:
            url: 要验证的URL
            
        Returns:
            bool: URL是否有效
        """
        try:
            verdict = validate_outbound_url(url, resolve_dns=True)
            if not verdict.allowed:
                print(f"      URL blocked by outbound policy: {verdict.reason}")
                return False
            import config
            proxy_enabled = bool(self.crawl_options.get('proxy_enabled'))
            proxies = config.get_proxies(enabled=proxy_enabled) if proxy_enabled else {}
            response = _requests_head_with_proxy_intent(url, timeout=5, allow_redirects=True, proxies=proxies)
            
            if response.status_code == 404:
                print(f"      ⚠️ URL返回404，跳过: {url[:80]}...")
                return False
            
            return True
        except Exception as e:
            # 验证失败时仍然尝试聚合（网络问题不应阻止聚合）
            print(f"      ⚠️ URL验证异常（将继续尝试）: {str(e)[:50]}...")
            return True
    
    def _smart_retry_url(self, url: str, base_url: str = None) -> Optional[str]:
        """
        🔥 方案5: 通用智能重试URL（404时尝试简化版本）
        
        当URL访问失败时，尝试简化URL重试：
        1. 提取文章ID
        2. 尝试各种可能的URL格式
        3. 返回第一个有效的URL
        
        Args:
            url: 失败的URL
            
        Returns:
            Optional[str]: 有效的URL或None
        """
        parsed = urlparse(url)
        domain = f"{parsed.scheme}://{parsed.netloc}"
        
        # 提取文章ID（支持多种模式）
        id_patterns = [
            r'/article/id/(\d+)',
            r'/article/(\d+)',
            r'/news/(\d+)',
            r'/post/(\d+)',
            r'/id/(\d+)',
        ]
        
        article_id = None
        for pattern in id_patterns:
            match = re.search(pattern, url)
            if match:
                article_id = match.group(1)
                break
        
        if not article_id:
            return None
        
        def _add_candidate(candidates, candidate):
            if candidate and candidate not in candidates and candidate != url:
                candidates.append(candidate)

        simplified_urls = []
        stripped_path = re.sub(
            r'(/(?:article/id|article|news|post|story|detail|id)/\d+)/[^/?#]+/?$',
            r'\1/',
            parsed.path,
            flags=re.IGNORECASE,
        )
        if stripped_path != parsed.path:
            _add_candidate(
                simplified_urls,
                urlunparse((parsed.scheme, parsed.netloc, stripped_path, parsed.params, parsed.query, parsed.fragment)),
            )

        repaired = self._repair_common_article_url_format(url, base_url or url, silent=True)
        _add_candidate(simplified_urls, repaired)

        if base_url:
            section_parts = self._section_parts_from_base_url(base_url)
            if section_parts:
                section_path = '/'.join(section_parts[:2])
                _add_candidate(simplified_urls, f"{domain}/{section_path}/article/id/{article_id}/")
                _add_candidate(simplified_urls, f"{domain}/{section_path}/article/{article_id}/")
                _add_candidate(simplified_urls, f"{domain}/{section_path}/news/{article_id}/")

        # 通用简化URL格式（按优先级尝试）
        for candidate in (
            f"{domain}/article/id/{article_id}/",
            f"{domain}/article/{article_id}/",
            f"{domain}/news/{article_id}/",
            f"{domain}/post/{article_id}/",
            f"{domain}/story/{article_id}/",
            f"{domain}/detail/{article_id}/",
        ):
            _add_candidate(simplified_urls, candidate)
        
        # 尝试每个URL
        for test_url in simplified_urls:
            try:
                import config
                proxy_enabled = bool(self.crawl_options.get('proxy_enabled'))
                proxies = config.get_proxies(enabled=proxy_enabled) if proxy_enabled else {}
                response = _requests_head_with_proxy_intent(test_url, timeout=5, allow_redirects=True, proxies=proxies)
                if 200 <= response.status_code < 400:
                    print(f"      ✅ 智能重试成功: {test_url}")
                    return test_url
            except:
                continue
        
        return None
    
    def _is_article_link(self, text: str, url: str) -> bool:
        """
        智能判断是否为文章链接
        
        Args:
            text: 链接文本
            url: 链接URL
            
        Returns:
            bool: 是否为文章链接
        """
        import re
        text = text.strip()
        text_lower = text.lower()
        url_lower = url.lower()
        
        is_obvious_article_url = any([
            '/article/' in url_lower,
            '/post/' in url_lower,
            '/news/' in url_lower and re.search(r'/\d{6,}', url),
            '/detail/' in url_lower,
            re.search(r'/id/\d+', url),
            re.search(r'/\d{7,}/', url),  # 7位以上数字（文章ID）
        ])
        
        # 🚫 通用过滤：排除移动版导航和图片链接；移动版文章详情保留为候选。
        # 移动版URL特征（通用）
        mobile_patterns = [
            '/m/', '/mobile/', '/mobi/', '/wap/', '/landing/mob', '/mobarticle',
            '.m.', 'mobile.', 'm.'  # m.example.com, mobile.example.com
        ]
        if any(pattern in url_lower for pattern in mobile_patterns) and not is_obvious_article_url:
            return False
        
        # 图片/静态资源链接（通用）
        static_patterns = [
            '/images/', '/img/', '/static/', '/assets/', '/media/', '/uploads/',
            '/photo/', '/picture/', '/gallery/', '/thumbnail/', '/thumb/',
            '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.ico', '.bmp', '.tiff',
            '.css', '.js', '.woff', '.ttf', '.pdf', '.doc', '.docx', '.xls', '.xlsx',
            '.mp3', '.mp4', '.avi', '.mov', '.wmv', '.flv',
            'data:image',  # base64图片
        ]
        if any(pattern in url_lower for pattern in static_patterns):
            return False
        
        # 1. 文本长度检查 - 太短的通常是导航
        # 🔥 但是如果URL明显是文章详情页格式，即使文本短也应该保留
        if len(text) < 5 and not is_obvious_article_url:
            return False
        
        # 2. 排除明显的导航和功能链接
        exclude_keywords = [
            '首页', '关于', '联系', '招聘', '法律声明', '隐私', '网站地图', 
            '订阅', '服务号', '微信', '微博', '返回', '上一页', '下一页',
            'home', 'about', 'contact', 'career', 'privacy', 'terms', 
            'sitemap', 'login', 'register', 'search', 'prev', 'next',
            'facebook', 'twitter', 'linkedin', 'instagram', 'youtube',
            '备案', 'icp', 'beian', '更多', 'more', '查看全部', 'view all',
            '业务领域', '专业领域', '团队', '律师', '办公室'
        ]
        
        if any(keyword in text_lower for keyword in exclude_keywords):
            return False
        
        # 3. 排除业务领域/专业领域等导航链接
        if len(text) < 15:
            business_keywords = [
                '反垄断', '竞争法', '资产证券化', '银行', '融资', '合规', '监管',
                '能源', '基础设施', '并购', '重组', '上市', '诉讼', '仲裁',
                '知识产权', '房地产', '建设工程', '劳动', '税务', '外商投资'
            ]
            if any(text.startswith(kw) for kw in business_keywords):
                return False
        
        # 4. URL路径检查 - 排除常见的非文章路径
        exclude_url_patterns = [
            '/about', '/contact', '/career', '/job', '/recruit',
            '/privacy', '/terms', '/login', '/register', '/search',
            '/tag', '/category', '/author', '/page/', '/business/',
            '/service', '/practice', '/team', '/office', '/location',
            '/lawyer', '/attorney', '/professional',
            # 工具、行情、组件类页面一般不是正文文章
            '/currency', '/smart', '/tools/', '/calculator',
            '/weather', '/stock', '/quote', '/chart', '/portfolio',
            '/realtime', '/streaming', '/widget', '/embed',
            # 其他非文章页面
            '/subscribe', '/newsletter', '/rss', '/feed', '/api/',
            '/app/', '/download', '/install', '/help/', '/faq/',
        ]
        
        if any(pattern in url_lower for pattern in exclude_url_patterns):
            return False
        
        # 5. 优先识别新闻/文章特征
        article_indicators = [
            # URL特征（不区分大小写）
            '/news/', '/article/', '/insights/', '/publication/', 
            '/content/', '/details', '/post/', '/blog/', '/press/',
            # 文本特征
            '助力', '完成', '成功', '协助', '达成', '发布', '获得',
            '签署', '参与', '担任', '荣获', '入选', '解读', '分析',
            '评析', '观察', '指南', '研究', '报告', '活动', '回顾',
            '业绩', '荣誉', '资讯', '党建', '致辞'
        ]
        
        has_article_feature = any(
            indicator in url_lower or indicator in text_lower 
            for indicator in article_indicators
        )
        
        # 特别处理：如果URL包含年份路径（如 /2024/、/2025/），很可能是文章
        import re
        if re.search(r'/20\d{2}/', url):
            has_article_feature = True
        
        # 6. 综合判断
        if len(text) > 15 and has_article_feature:
            return True
        
        news_url_patterns = ['/news/', '/article/', '/insights/', '/content/', '/details']
        if any(pattern in url_lower for pattern in news_url_patterns):
            return True
        
        return len(text) > 10
    
    async def _go_to_next_page(self, page, pagination_info: Dict, current_page: int) -> bool:
        """
        翻到下一页（通用方法 - 支持URL参数翻页）
        
        Args:
            page: Playwright page对象
            pagination_info: 分页信息
            current_page: 当前页码
            
        Returns:
            bool: 是否成功翻页
        """
        try:
            current_url = page.url
            
            # 🔥 方法0: URL参数翻页（最可靠的通用方法）
            try:
                from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
                
                print(f"      尝试URL参数翻页...")
                parsed = urlparse(current_url)
                query_params = parse_qs(parsed.query)
                
                # 检测常见的分页参数
                page_param_names = ['page', 'p', 'pageNum', 'pageNo', 'pg']
                url_modified = False
                current_page_num = None
                
                # 🔥 先从URL中提取当前页码
                for param_name in page_param_names:
                    if param_name in query_params:
                        try:
                            current_page_num = int(query_params[param_name][0])
                            break
                        except:
                            pass
                
                # 如果没有找到页码，使用传入的current_page
                if current_page_num is None:
                    current_page_num = current_page
                
                next_page = current_page_num + 1
                
                for param_name in page_param_names:
                    if param_name in query_params or param_name.lower() in [k.lower() for k in query_params.keys()]:
                        # 找到分页参数，修改它
                        query_params[param_name] = [str(next_page)]
                        url_modified = True
                        break
                
                # 如果URL中没有分页参数，尝试添加
                if not url_modified:
                    # 猜测最可能的参数名
                    query_params['page'] = [str(next_page)]
                    url_modified = True
                
                if url_modified:
                    # 重新构建URL
                    new_query = urlencode(query_params, doseq=True)
                    new_parsed = parsed._replace(query=new_query)
                    new_url = urlunparse(new_parsed)
                    
                    print(f"      🔗 新URL: {new_url[:100]}...")
                    
                    # 导航到新URL
                    await page.goto(new_url, wait_until='domcontentloaded', timeout=15000)
                    await page.wait_for_timeout(1000)
                    
                    print(f"      ✅ URL参数翻页成功（页码 {next_page}）")
                    return True
                    
            except Exception as e:
                print(f"      ⚠️ URL参数翻页失败: {e}")
            
            # 方法1: 检查是否是 JavaScript 函数翻页（如 getNews）
            try:
                # 查找包含 JavaScript 函数的链接
                js_link_selectors = [
                    f'a[href*="javascript"][href*="{next_page}"]',
                    f'a[onclick*="{next_page}"]',
                    f'a:has-text("{next_page}")',
                ]
                
                for selector in js_link_selectors:
                    try:
                        elem = await page.query_selector(selector)
                        if elem:
                            href = await elem.get_attribute('href') or ''
                            onclick = await elem.get_attribute('onclick') or ''
                            
                            # 检查是否是 JavaScript 函数调用
                            if 'javascript:' in href or onclick:
                                print(f"   🔍 检测到 JavaScript 翻页函数")
                                
                                clicked = await self._safe_click(elem, f"javascript page {next_page}")
                                if not clicked:
                                    continue
                                
                                # 等待 AJAX 加载完成
                                print(f"   ⏳ 等待 AJAX 加载...")
                                await page.wait_for_timeout(2000)
                                
                                # 等待网络空闲
                                try:
                                    await page.wait_for_load_state('networkidle', timeout=5000)
                                except:
                                    pass
                                
                                print(f"   ✅ JavaScript 翻页成功（页码 {next_page}）")
                                return True
                    except Exception as e:
                        continue
            except Exception as e:
                print(f"   ⚠️ JavaScript 翻页检测失败: {e}")
            
            # 方法2: 点击页码数字（普通链接）
            try:
                # 尝试多种选择器（按优先级排序）
                selectors = [
                    # 精确匹配文本（最常见）
                    f'a:text-is("{next_page}")',  # 精确文本匹配
                    f'a:has-text("{next_page}")',  # 包含文本
                    f'text="{next_page}"',
                    # 按钮
                    f'button:text-is("{next_page}")',
                    f'button:has-text("{next_page}")',
                    # 属性匹配
                    f'a[title="{next_page}"]',
                    f'[data-page="{next_page}"]',
                    # URL参数
                    f'a[href*="page={next_page}"]',
                    f'a[href*="p={next_page}"]',
                    f'a[href*="&page={next_page}"]',
                    f'a[href*="?page={next_page}"]',
                    # 通用链接（在分页容器中）
                    f'.pagination a:has-text("{next_page}")',
                    f'[class*="pag"] a:has-text("{next_page}")',
                    f'nav a:has-text("{next_page}")',
                ]
                
                print(f"      尝试点击页码 {next_page}...")
                for i, selector in enumerate(selectors):
                    try:
                        elem = await page.query_selector(selector)
                        if elem:
                            # 检查元素是否可见和可点击
                            if await self._safe_click(elem, f"page number {next_page}"):
                                print(f"      ✅ 使用选择器 #{i+1} 成功点击页码 {next_page}")
                                return True
                            else:
                                print(f"      ⚠️ 选择器 #{i+1} 找到元素但不可见")
                    except Exception as e:
                        continue
                
                print(f"      ⚠️ 所有页码选择器都未能点击")
            except Exception as e:
                print(f"      ⚠️ 页码点击异常: {e}")
            
            # 方法3: 点击"下一页"按钮或"»"符号
            try:
                print(f"      尝试点击'下一页'按钮...")
                next_selectors = [
                    # 中文
                    'a:text-is("下一页")',
                    'text="下一页"',
                    'button:has-text("下一页")',
                    # 英文
                    'a:text-is("Next")',
                    'text="Next"',
                    # 符号
                    'a:text-is("»")',
                    'text="»"',
                    'a:text-is("›")',
                    'text="›"',
                    'a:text-is(">")',
                    'text=">"',
                    # 属性
                    'a[rel="next"]',
                    'button[aria-label*="next"]',
                    'a[title*="下一页"]',
                    'a[title*="Next"]',
                    # Class
                    '.next-page',
                    'a.next',
                    '[class*="next"]:not([class*="disabled"])',
                    # 在分页容器中
                    '.pagination a.next',
                    '[class*="pag"] a:has-text("»")',
                ]
                
                for i, selector in enumerate(next_selectors):
                    try:
                        elem = await page.query_selector(selector)
                        if elem:
                            # 检查是否被禁用
                            class_name = await elem.get_attribute('class') or ''
                            if 'disabled' not in class_name.lower():
                                if await self._safe_click(elem, f"next page {next_page}"):
                                    print(f"      ✅ 使用'下一页'选择器 #{i+1} 成功")
                                    return True
                    except:
                        continue
                
                print(f"      ⚠️ 所有'下一页'选择器都未能点击")
            except Exception as e:
                print(f"      ⚠️ '下一页'点击异常: {e}")
            
            # 方法4: 直接执行 JavaScript 函数（如果能找到）
            try:
                # 尝试常见的翻页函数名
                js_functions = ['getNews', 'goPage', 'changePage', 'loadPage']
                for func_name in js_functions:
                    try:
                        # 检查函数是否存在
                        func_exists = await page.evaluate(f'typeof {func_name} === "function"')
                        if func_exists:
                            print(f"   🔍 找到 JavaScript 函数: {func_name}")
                            # 执行翻页函数
                            await page.evaluate(f'{func_name}("", {next_page})')
                            await page.wait_for_timeout(2000)
                            print(f"   ✅ 执行 {func_name}('', {next_page}) 成功")
                            return True
                    except:
                        continue
            except:
                pass
            
            # 如果所有方法都失败，打印调试信息
            print(f"      ❌ 所有翻页方法都失败")
            print(f"      尝试过的方法:")
            print(f"         1. JavaScript 函数翻页")
            print(f"         2. 点击页码 {next_page}")
            print(f"         3. 点击'下一页'按钮")
            print(f"         4. 执行 JavaScript 翻页函数")
            print(f"      💡 建议: 检查页面HTML结构，确认分页元素")
            return False
            
        except Exception as e:
            print(f"   ❌ 翻页出错: {e}")
            import traceback
            traceback.print_exc()
            return False


def extract_links_with_playwright(url: str, max_articles: float = float('inf'), max_pages: float = float('inf')) -> Dict:
    """
    同步包装函数：使用 Playwright 提取文章链接
    
    Args:
        url: 目标URL
        max_articles: 最多提取的文章数
        max_pages: 最多聚合的页数
        
    Returns:
        Dict: 提取结果
    """
    extractor = PlaywrightLinkExtractor()
    
    if not extractor.playwright_available:
        return {
            'success': False,
            'error': 'Playwright 未安装',
            'articles': []
        }
    
    # 运行异步函数
    try:
        result = asyncio.run(extractor.extract_links_from_url(url, max_articles, max_pages))
        return result
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'articles': []
        }


# 测试函数
def test_playwright_extractor():
    """测试 Playwright 提取器"""
    test_urls = [
        "https://www.fangdalaw.com/news/",
        "https://www.junhe.com/legal-updates"
    ]
    
    for url in test_urls:
        print(f"\n测试URL: {url}")
        result = extract_links_with_playwright(url)  # 不限制
        
        if result['success']:
            print(f"\n✅ 成功提取 {len(result['articles'])} 篇文章")
            print(f"前5篇:")
            for i, article in enumerate(result['articles'][:5], 1):
                print(f"{i}. {article['title']}")
                print(f"   URL: {article['url']}")
                if article.get('publish_date'):
                    print(f"   日期: {article['publish_date']}")
        else:
            print(f"❌ 提取失败: {result.get('error')}")


if __name__ == "__main__":
    test_playwright_extractor()

