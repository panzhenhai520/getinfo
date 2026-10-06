#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
认证聚合模块 - 使用Playwright和认证Cookie聚合需要登录的页面
"""

import asyncio
import re
from playwright.async_api import async_playwright
from auto_login import AutoLogin
from article_link_extractor import ArticleLinkExtractor
from crawl_options import normalize_crawl_options
from sqlite_database import sqlite_db
from utils import get_china_time
from keyword_filter import KeywordFilter


class AuthenticatedCrawler:
    """带认证的文章聚合"""
    
    def __init__(self):
        self.auto_login = AutoLogin()
        self.extractor = ArticleLinkExtractor(db=sqlite_db, enable_smart_validation=False)
    
    async def crawl_with_auth(
        self,
        url,
        auth_name,
        max_articles=float('inf'),
        max_pages=float('inf'),
        wait_time=2,
        extract_content=True,
        keywords='',
        kb_id='',
        days_limit=7,
        start_date=None,
        end_date=None,
        crawl_options=None
    ):
        """
        使用认证信息聚合文章
        
        Args:
            url: 目标URL
            auth_name: 认证配置名称
            max_articles: 最大文章数（默认无限制）
            max_pages: 最大页数（默认无限制）
            wait_time: 等待时间（秒，默认2秒）
            extract_content: 是否提取内容
            keywords: 关键词过滤（可选）
            kb_id: RAGFlow知识库ID（可选）
            days_limit: 日期限制（天）
            start_date: 指定起始日期（YYYY-MM-DD，可选，优先于days_limit）
            end_date: 指定结束日期（YYYY-MM-DD，可选）
            crawl_options: 通用聚合配置（等待时间、分页上限、代理开关等）
            
        Returns:
            dict: 聚合结果
        
        注意：
            max_articles 和 max_pages 默认为 float('inf')，完全不限制
        """
        crawl_options = normalize_crawl_options(crawl_options)
        render_wait_ms = int(crawl_options.get('wait_for_ms') or 8000)
        wait_time = max(1, int(render_wait_ms / 1000))
        max_empty_pages = int(crawl_options.get('max_empty_pages') or 5)

        if max_pages == float('inf') and crawl_options.get('max_pages'):
            max_pages = crawl_options.get('max_pages')

        max_pages_limit = None
        try:
            if max_pages != float('inf'):
                max_pages_limit = max(1, int(float(max_pages)))
        except (TypeError, ValueError, OverflowError):
            max_pages_limit = None

        max_articles_limit = None
        try:
            if max_articles != float('inf'):
                max_articles_limit = max(1, int(float(max_articles)))
        except (TypeError, ValueError, OverflowError):
            max_articles_limit = None

        def parse_date_bound(value):
            if not value:
                return None
            text = str(value).strip()
            match = re.search(r'(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})', text)
            if not match:
                return None
            year, month, day = match.groups()
            try:
                return datetime(int(year), int(month), int(day)).date()
            except ValueError:
                return None

        def extract_publish_date(html):
            date_patterns = [
                r'"datePublished"\s*:\s*"([^"]+)"',
                r'"dateModified"\s*:\s*"([^"]+)"',
                r'property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)["\']',
                r'name=["\']publishdate["\'][^>]+content=["\']([^"\']+)["\']',
                r'name=["\']pubdate["\'][^>]+content=["\']([^"\']+)["\']',
                r'(?:发布时间|发布日期|时间)[:：\s]*(\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2})',
                r'(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})',
            ]
            for pattern in date_patterns:
                match = re.search(pattern, html[:8000], re.I)
                if not match:
                    continue
                if len(match.groups()) == 1:
                    date_text = match.group(1)
                else:
                    year, month, day = match.groups()[:3]
                    date_text = f"{year}-{int(month):02d}-{int(day):02d}"
                parsed = parse_date_bound(date_text)
                if parsed and parsed.year >= 2000:
                    return parsed, parsed.strftime('%Y-%m-%d')
            return None, None

        # 解析关键词
        keyword_filter_auth = KeywordFilter(keywords or '')
        if keyword_filter_auth.is_enabled():
            print(f"   🎯 关键词过滤: {keyword_filter_auth.keywords}")
        
        # 起止日期优先，其次使用最近N天；没有日期窗口则不过滤
        from datetime import datetime, timedelta
        date_lower = parse_date_bound(start_date)
        date_upper = parse_date_bound(end_date)
        if date_lower or date_upper:
            print(f"   📅 日期范围: {date_lower or '不限'} ~ {date_upper or '不限'}")
        elif days_limit and days_limit > 0:
            date_lower = (get_china_time() - timedelta(days=days_limit)).date()
            print(f"   📅 日期限制: 只聚合 {date_lower} 之后的文章（最近{days_limit}天）")
        else:
            print(f"   📅 日期限制: 不限制")

        has_date_window = bool(date_lower or date_upper)
        consecutive_old = 0
        
        try:
            print(f"🚀 AuthenticatedCrawler: 开始认证聚合")
            print(f"   URL: {url}")
            print(f"   认证: {auth_name}")
            print(f"   策略: 等待{render_wait_ms}ms, 最大翻页{max_pages_limit or '不限'}, 连续空页{max_empty_pages}, 代理{'开' if crawl_options.get('proxy_enabled') else '关'}")
            
            async with async_playwright() as p:
                # 创建带认证的浏览器上下文
                context = await self.auto_login.create_authenticated_context(
                    auth_name,
                    p,
                    proxy_enabled=crawl_options.get('proxy_enabled')
                )
                
                if not context:
                    return {
                        'success': False,
                        'error': '无法创建认证上下文',
                        'articles': [],
                        'stats': {'new_articles': 0, 'skipped_articles': 0}
                    }
                
                try:
                    page = await context.new_page()
                    
                    # 访问页面
                    print(f"📄 访问页面: {url}")
                    
                    # 🔥 增加超时时间到 90 秒，使用更宽松的等待策略
                    try:
                        # 先尝试 commit（最快，页面开始加载即可）
                        await page.goto(url, wait_until='commit', timeout=90000)
                        print(f"✅ 页面开始加载")
                        # 等待页面内容加载
                        await page.wait_for_timeout(wait_time * 1000 + 3000)  # 额外等待 3 秒
                    except Exception as goto_error:
                        print(f"⚠️ commit 加载超时，尝试 domcontentloaded...")
                        try:
                            await page.goto(url, wait_until='domcontentloaded', timeout=90000)
                            await page.wait_for_timeout(wait_time * 1000)
                        except Exception as e2:
                            print(f"⚠️ domcontentloaded 也超时，继续尝试...")
                            # 直接访问，不等待
                            await page.goto(url, timeout=90000)
                            await page.wait_for_timeout(10000)  # 固定等待 10 秒
                    
                    # 获取页面内容
                    content = await page.content()
                    
                    # 检查是否真的登录成功
                    if '登录' in content[:1000] or '验证码' in content[:1000]:
                        print(f"⚠️ 页面可能需要重新登录")
                        await context.close()
                        return {
                            'success': False,
                            'error': '认证可能已失效，请重新登录',
                            'articles': [],
                            'stats': {'new_articles': 0, 'skipped_articles': 0}
                        }
                    
                    print(f"✅ 页面访问成功，开始智能提取文章链接...")
                    
                    # 🔥 关闭所有可能的广告/弹窗（避免阻挡点击）
                    try:
                        print(f"🚫 尝试关闭广告弹窗...")
                        # 常见的广告/弹窗选择器
                        popup_selectors = [
                            '#ad_popup', '.ad-popup', '.overlay-bg', '.modal', 
                            '.popup', '#popup', '[class*="popup"]', '[id*="popup"]',
                            '.advertisement', '[class*="ad-"]', '.close-ad'
                        ]
                        for selector in popup_selectors:
                            try:
                                popups = await page.query_selector_all(selector)
                                for popup in popups:
                                    # 尝试点击关闭按钮
                                    close_btns = await popup.query_selector_all('button, .close, [class*="close"]')
                                    for btn in close_btns:
                                        try:
                                            await btn.click(timeout=1000)
                                            print(f"   ✅ 点击关闭按钮")
                                        except:
                                            pass
                                    # 直接隐藏元素
                                    try:
                                        await popup.evaluate('element => element.style.display = "none"')
                                        print(f"   ✅ 隐藏弹窗: {selector}")
                                    except:
                                        pass
                            except:
                                continue
                        
                        # JavaScript 强制移除所有遮挡层
                        await page.evaluate('''() => {
                            // 移除所有遮挡层
                            document.querySelectorAll('.overlay-bg, .modal-backdrop, #ad_popup, [class*="popup"]').forEach(el => el.remove());
                            // 恢复body滚动
                            document.body.style.overflow = 'auto';
                        }''')
                        print(f"   ✅ 已清除所有弹窗")
                    except Exception as e:
                        print(f"   ⚠️ 清除弹窗失败: {e}")
                    
                    # 🔥 超级智能通用聚合 - 支持分页、Tab、滚动加载
                    from bs4 import BeautifulSoup
                    from urllib.parse import urlparse
                    import re
                    
                    all_links = []
                    visited_urls = set()  # 防止重复访问
                    
                    print(f"📄 启动智能提取引擎...")
                    
                    # ========== 步骤1: 检测并切换所有Tab/分类 ==========
                    tabs = []
                    try:
                        print(f"🔍 检测页面Tab/分类...")
                        # 常见Tab选择器
                        tab_selectors = [
                            'ul.nav-tabs li a', 'ul.tabs li a', '.tab-list a',
                            '[role="tab"]', '.category-list a', '.nav-item a',
                            'a[data-toggle="tab"]', '.menu-item a'
                        ]
                        
                        for selector in tab_selectors:
                            try:
                                found_tabs = await page.query_selector_all(selector)
                                if found_tabs and len(found_tabs) > 1:
                                    tabs = found_tabs
                                    print(f"   ✅ 找到 {len(tabs)} 个Tab/分类: {selector}")
                                    break
                            except:
                                continue
                    except Exception as e:
                        print(f"   ℹ️ 未检测到Tab: {e}")
                    
                    # 如果没有Tab，就只处理当前页面
                    if not tabs:
                        tabs = [None]  # 占位，表示只有一个"页面"
                    
                    # ========== 步骤2: 遍历每个Tab ==========
                    for tab_index, tab in enumerate(tabs, 1):
                        if tab:
                            try:
                                tab_text = await tab.inner_text()
                                print(f"\n📑 切换到Tab {tab_index}/{len(tabs)}: {tab_text[:30]}")
                                await tab.click()
                                await page.wait_for_timeout(render_wait_ms)
                            except Exception as e:
                                print(f"   ⚠️ Tab切换失败: {e}")
                                continue
                        else:
                            print(f"\n📄 处理主页面（无Tab）")
                        
                        # ========== 步骤3: 在当前Tab中自动翻页 ==========
                        current_page_num = 1
                        consecutive_no_new_links = 0  # 连续没有新链接的次数
                        
                        while True:
                            print(f"   📄 第 {current_page_num} 页...")
                            
                            # 尝试滚动加载（有些网站用滚动而非翻页）
                            try:
                                await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                                await page.wait_for_timeout(min(render_wait_ms, 3000))
                            except:
                                pass
                            
                            # 提取当前页面的链接
                            content = await page.content()
                            soup = BeautifulSoup(content, 'html.parser')
                            
                            # 🔥 只提取主内容区域的文章链接（更智能）
                            # 尝试找到文章列表容器
                            article_containers = [
                                soup.find('main'),
                                soup.find('div', {'class': re.compile(r'article-list|post-list|content-list|news-list|list-container', re.I)}),
                                soup.find('div', {'id': re.compile(r'content|main|articles', re.I)}),
                                soup.find('div', {'class': re.compile(r'content|main', re.I)}),
                                soup  # 最后才用整个页面
                            ]
                            
                            container = None
                            for cont in article_containers:
                                if cont:
                                    container = cont
                                    break
                            
                            page_links = []
                            
                            # 🔥 智能识别文章链接（通用算法）
                            for a_tag in container.find_all('a', href=True):
                                href = a_tag['href']
                                title = a_tag.get_text(strip=True)
                                
                                # 🔥 通用过滤条件
                                # 1. 标题长度合理（放宽）
                                if not title or len(title) < 6 or len(title) > 300:
                                    continue
                                
                                # 2. 排除明显的导航/工具链接（只排除最明显的）
                                skip_keywords_exact = ['登录', '注册', '订阅', '续订', '首页', 'home', '«', '»']
                                if title.strip() in skip_keywords_exact or title.strip().lower() in [k.lower() for k in skip_keywords_exact]:
                                    continue
                                
                                # 3. 排除"剩余时间"这种明显的广告
                                if '剩余时间' in title or '剩餘時間' in title:
                                    continue
                                
                                # 4. 🔥 URL智能判断（通用，不限制必须有数字）
                                # 只要是内部链接且不是这些特殊路径就行
                                skip_paths = ['/tag/', '/topic/', '/author/', '/search/', '/subscribe', '/login', '/register']
                                if any(path in href.lower() for path in skip_paths):
                                    continue
                                
                                # 5. 🔥 处理相对URL
                                if href.startswith('/'):
                                    parsed = urlparse(url)
                                    full_url = f"{parsed.scheme}://{parsed.netloc}{href}"
                                elif href.startswith('http'):
                                    full_url = href
                                    # 确保是同一个网站
                                    parsed_href = urlparse(full_url)
                                    parsed_base = urlparse(url)
                                    if parsed_href.netloc != parsed_base.netloc:
                                        continue  # 跳过外部链接
                                else:
                                    continue
                                
                                # 6. 🔥 去重（使用set更高效）
                                if full_url not in visited_urls:
                                    visited_urls.add(full_url)
                                    page_links.append({
                                        'url': full_url,
                                        'title': title
                                    })
                        
                            all_links.extend(page_links)
                            
                            if len(page_links) > 0:
                                print(f"      ✅ 提取到 {len(page_links)} 个新链接，总计 {len(all_links)} 个")
                                consecutive_no_new_links = 0
                            else:
                                consecutive_no_new_links += 1
                                print(f"      ℹ️ 本页无新链接")
                                
                                # 如果连续3页没有新链接，可能到底了
                                if consecutive_no_new_links >= max_empty_pages:
                                    print(f"      ⚠️ 连续{consecutive_no_new_links}页无新链接，停止翻页")
                                    break
                            
                            # 检查是否达到目标数量
                            if max_articles_limit and len(all_links) >= max_articles_limit:
                                print(f"      ✅ 已达到目标数量，停止")
                                break

                            if max_pages_limit and current_page_num >= max_pages_limit:
                                print(f"      ✅ 已达到最大翻页数 {max_pages_limit}，停止")
                                break
                            
                            # ========== 步骤4: 智能查找"下一页"按钮 ==========
                            next_button = None
                            next_found = False
                            
                            # 方法1: 文本匹配（中英文）
                            text_patterns = [
                                '下一页', '下頁', '下一頁', 'Next', 'next', 'NEXT',
                                '下页', '后一页', '›', '»', '>', '→', '▶'
                            ]
                            for pattern in text_patterns:
                                try:
                                    next_button = await page.query_selector(f'a:has-text("{pattern}")')
                                    if next_button:
                                        is_disabled = await next_button.get_attribute('disabled')
                                        is_disabled_class = await next_button.get_attribute('class')
                                        if not is_disabled and 'disabled' not in str(is_disabled_class):
                                            print(f"      🔍 找到下一页: 文本='{pattern}'")
                                            next_found = True
                                            break
                                except:
                                    continue
                            
                            # 方法2: CSS类名/属性
                            if not next_found:
                                class_selectors = [
                                    'a.next', 'a.next-page', 'a.pagination-next',
                                    'a[rel="next"]', 'button.next', 'li.next a',
                                    '.pagination .next a', '.pager .next a'
                                ]
                                for selector in class_selectors:
                                    try:
                                        next_button = await page.query_selector(selector)
                                        if next_button:
                                            print(f"      🔍 找到下一页: CSS='{selector}'")
                                            next_found = True
                                            break
                                    except:
                                        continue
                            
                            # 方法3: 页码数字（当前页+1）
                            if not next_found:
                                try:
                                    next_page_num = current_page_num + 1
                                    next_button = await page.query_selector(f'a:has-text("{next_page_num}")')
                                    if next_button:
                                        print(f"      🔍 找到下一页: 页码={next_page_num}")
                                        next_found = True
                                except:
                                    pass
                            
                            # 方法4: 包含数字的链接（智能匹配）
                            if not next_found:
                                try:
                                    all_page_links = await page.query_selector_all('a')
                                    for link in all_page_links:
                                        try:
                                            link_text = await link.inner_text()
                                            # 检查是否是纯数字且等于下一页
                                            if link_text.strip().isdigit() and int(link_text.strip()) == current_page_num + 1:
                                                next_button = link
                                                print(f"      🔍 找到下一页: 智能匹配数字={link_text}")
                                                next_found = True
                                                break
                                        except:
                                            continue
                                except:
                                    pass
                            
                            # 点击下一页
                            if next_found and next_button:
                                try:
                                    # 先清除可能的弹窗（避免阻挡）
                                    try:
                                        await page.evaluate('document.querySelectorAll(".overlay-bg, #ad_popup, .modal-backdrop").forEach(el => el.remove())')
                                    except:
                                        pass
                                    
                                    # 🔥 记录翻页前的URL（用于验证是否真的翻页了）
                                    old_url = page.url
                                    old_content_hash = hash(await page.content())
                                    
                                    # 使用 force 选项强制点击（忽略遮挡）
                                    await next_button.click(force=True, timeout=5000)
                                    
                                    # 🔥 等待URL变化或内容变化
                                    try:
                                        # 方法1: 等待URL变化
                                        await page.wait_for_function(f'window.location.href !== "{old_url}"', timeout=5000)
                                        print(f"      ✅ URL已变化")
                                    except:
                                        # 方法2: 等待内容变化
                                        print(f"      ℹ️ URL未变化，等待内容刷新...")
                                    
                                    await page.wait_for_timeout(render_wait_ms)  # 等待页面加载
                                    
                                    # 🔥 验证是否真的翻页了（内容是否变化）
                                    new_content_hash = hash(await page.content())
                                    if new_content_hash == old_content_hash:
                                        print(f"      ⚠️ 页面内容未变化，可能翻页失败")
                                        # 再等待一下
                                        await page.wait_for_timeout(2000)
                                        new_content_hash = hash(await page.content())
                                        if new_content_hash == old_content_hash:
                                            print(f"      ❌ 确认翻页失败，停止")
                                            break
                                    
                                    current_page_num += 1
                                    print(f"      ✅ 翻页成功，当前第 {current_page_num} 页")
                                except Exception as click_error:
                                    print(f"      ⚠️ 点击下一页失败: {click_error}")
                                    break
                            else:
                                print(f"      ℹ️ 没有找到下一页按钮，此Tab完成")
                                break
                        
                        print(f"   📊 Tab {tab_index} 完成：提取了 {current_page_num} 页")
                        if max_articles_limit and len(all_links) >= max_articles_limit:
                            break
                    
                    links = all_links
                    print(f"\n✅ 智能提取完成！总共 {len(links)} 个唯一链接")
                    
                    # 提取文章内容
                    articles_with_content = []
                    new_count = 0
                    skipped_count = 0
                    ragflow_stats = {
                        'uploaded': 0,
                        'skipped_existing': 0,
                        'skipped_empty': 0,
                        'failed': 0,
                        'disabled': False,
                        'errors': []
                    }
                    
                    if extract_content:
                        print(f"📄 开始提取文章内容...")
                        # 🔥 使用认证浏览器访问文章页（避免超时）
                        from bs4 import BeautifulSoup
                        
                        for i, link in enumerate(links, 1):
                            try:
                                print(f"  [{i}/{len(links)}] 提取: {link['title'][:50]}...")
                                
                                article_url = link['url']
                                
                                # 🔥 用认证浏览器访问文章页
                                article_page = await context.new_page()
                                
                                try:
                                    # 访问文章页面
                                    await article_page.goto(
                                        article_url,
                                        wait_until='commit',
                                        timeout=max(60000, render_wait_ms + 30000)
                                    )
                                    
                                    # 🔥 等待内容加载（重要！）
                                    # 方法1: 等待常见的文章容器出现
                                    try:
                                        await article_page.wait_for_selector('article, .article-content, .content, main, .post-content', timeout=10000)
                                    except:
                                        pass
                                    
                                    # 方法2: 固定等待，确保 JS 完全执行
                                    await article_page.wait_for_timeout(render_wait_ms)
                                    
                                    # 方法3: 滚动页面，触发懒加载
                                    try:
                                        await article_page.evaluate('window.scrollTo(0, document.body.scrollHeight / 2)')
                                        await article_page.wait_for_timeout(1000)
                                    except:
                                        pass
                                    
                                    # 获取页面HTML
                                    article_html = await article_page.content()
                                    
                                    # 🔥 使用更强大的提取方法
                                    soup = BeautifulSoup(article_html, 'html.parser')
                                    
                                    # 移除干扰元素
                                    for elem in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe', 'noscript']):
                                        elem.decompose()
                                    
                                    # 🔥 多种选择器尝试（优先级排序）
                                    content = None
                                    selectors = [
                                        ('article', None),
                                        ('div', {'class': re.compile(r'article-content|post-content|entry-content|content-body', re.I)}),
                                        ('div', {'id': re.compile(r'article|content|post', re.I)}),
                                        ('main', None),
                                        ('div', {'class': re.compile(r'content|text', re.I)}),
                                    ]
                                    
                                    for tag, attrs in selectors:
                                        content_tag = soup.find(tag, attrs)
                                        if content_tag:
                                            text = content_tag.get_text(strip=True, separator='\n')
                                            lines = [line.strip() for line in text.split('\n') if line.strip() and len(line.strip()) > 5]
                                            content = '\n'.join(lines)
                                            if len(content) > 200:
                                                print(f"    ✅ 使用选择器: {tag} {attrs}")
                                                break
                                    
                                    # 如果所有选择器都失败，使用全文
                                    if not content or len(content) < 200:
                                        paragraphs = soup.find_all('p')
                                        if paragraphs:
                                            content = '\n\n'.join([p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 20])
                                        
                                        if not content or len(content) < 100:
                                            print(f"    ⚠️ 内容太短 ({len(content) if content else 0} 字符)，跳过")
                                            skipped_count += 1
                                            continue
                                    
                                    # 🔥 关键词过滤
                                    matched_keywords_str = ''
                                    if keyword_filter_auth.is_enabled():
                                        match_result = keyword_filter_auth.get_matched_keywords_by_location(link['title'], content)
                                        title_matches = match_result.get('title_keywords', [])
                                        content_matches = match_result.get('content_keywords', [])
                                        if not title_matches and not content_matches:
                                            print(f"    ⏭️ 关键词不匹配，跳过")
                                            skipped_count += 1
                                            continue
                                        matched_keywords_str = match_result.get('matched_keywords_str', '')
                                    
                                    publish_date_obj, publish_date = extract_publish_date(article_html)

                                    # 提取不到发布日期时保留文章，避免静默漏掉需要人工复核的内容
                                    if has_date_window and publish_date_obj:
                                        if date_lower and publish_date_obj < date_lower:
                                            print(f"    ⏭️ 日期早于范围 ({publish_date} < {date_lower})")
                                            skipped_count += 1
                                            consecutive_old += 1
                                            continue
                                        if date_upper and publish_date_obj > date_upper:
                                            print(f"    ⏭️ 日期晚于范围 ({publish_date} > {date_upper})")
                                            skipped_count += 1
                                            continue
                                        consecutive_old = 0
                                    elif has_date_window:
                                        print("    ⚠️ 未提取到发布日期，先保留入库，避免漏抓")
                                    
                                    # 保存到数据库
                                    article_data = {
                                        'url': article_url,
                                        'title': link['title'],
                                        'content': content,
                                        'source_url': url,
                                        'publish_date': publish_date,
                                        'crawled_at': get_china_time().isoformat(),
                                        'extraction_method': 'authenticated_crawler_playwright',
                                        'content_length': len(content),
                                        'matched_keywords': matched_keywords_str
                                    }
                                    
                                    from article_ingest import ingest_article

                                    saved = ingest_article(
                                        article_data,
                                        source_kind="article_link_crawl",
                                        db=sqlite_db,
                                    )
                                    if saved:
                                        new_count += 1
                                        print(f"    ✅ 成功 (内容: {len(content)} 字符)")
                                        if kb_id:
                                            try:
                                                upload_result = self.extractor._upload_single_article_to_ragflow({
                                                    'content': {
                                                        'title': link['title'],
                                                        'content': content,
                                                        'url': article_url,
                                                        'publish_date': publish_date
                                                    },
                                                    'db_id': saved
                                                }, kb_id)
                                                upload_status = upload_result.get('status')
                                                if upload_result.get('uploaded'):
                                                    ragflow_stats['uploaded'] += 1
                                                elif upload_status == 'skipped_existing':
                                                    ragflow_stats['skipped_existing'] += 1
                                                elif upload_status == 'skipped_empty':
                                                    ragflow_stats['skipped_empty'] += 1
                                                elif upload_status == 'disabled':
                                                    ragflow_stats['disabled'] = True
                                                elif upload_status:
                                                    ragflow_stats['failed'] += 1
                                                    ragflow_stats['errors'].append(upload_result)
                                            except Exception as upload_error:
                                                print(f"    ⚠️ RAGFlow上传失败: {upload_error}")
                                                ragflow_stats['failed'] += 1
                                                ragflow_stats['errors'].append({
                                                    'title': link['title'],
                                                    'url': article_url,
                                                    'error': str(upload_error)
                                                })
                                    else:
                                        skipped_count += 1
                                        print(f"    ⚠️ 已存在")
                                    
                                    # 添加到结果列表
                                    articles_with_content.append({
                                        'url': article_url,
                                        'title': link['title'],
                                        'content': content,
                                        'publish_date': publish_date,
                                        'crawled_at': get_china_time().isoformat()
                                    })
                                    
                                except Exception as inner_e:
                                    print(f"    ⚠️ 内部异常: {inner_e}")
                                finally:
                                    try:
                                        await article_page.close()
                                    except:
                                        pass
                                
                            except Exception as e:
                                print(f"    ❌ 提取失败: {e}")
                                skipped_count += 1
                                continue
                    else:
                        # 不提取内容，只保存链接
                        for link in links:
                            try:
                                # 🔥 关键词过滤（只检查标题）
                                matched_keywords_str = ''
                                if keyword_filter_auth.is_enabled():
                                    match_result = keyword_filter_auth.get_matched_keywords_by_location(link['title'], '')
                                    title_matches = match_result.get('title_keywords', [])
                                    if not title_matches:
                                        print(f"    ⏭️ 关键词不匹配，跳过")
                                        skipped_count += 1
                                        continue
                                    matched_keywords_str = match_result.get('matched_keywords_str', '')
                                
                                article_data = {
                                    'url': link['url'],
                                    'title': link['title'],
                                    'source_url': url,
                                    'crawled_at': get_china_time().isoformat(),
                                    'matched_keywords': matched_keywords_str
                                }
                                from article_ingest import ingest_article

                                saved = ingest_article(
                                    article_data,
                                    source_kind="article_link_crawl",
                                    db=sqlite_db,
                                )
                                if saved:
                                    new_count += 1
                                else:
                                    skipped_count += 1
                                    
                                articles_with_content.append({
                                    'url': link['url'],
                                    'title': link['title'],
                                    'crawled_at': get_china_time().isoformat()
                                })
                            except Exception as e:
                                print(f"保存文章失败: {e}")
                                continue
                    
                    result = {
                        'success': True,
                        'articles': articles_with_content,
                        'stats': {
                            'new_articles': new_count,
                            'skipped_articles': skipped_count,
                            'total_links': len(links),
                            'ragflow': ragflow_stats
                        }
                    }
                    
                    await page.close()
                    await context.close()
                    
                    if result['success']:
                        print(f"✅ 认证聚合成功: {result['stats']['new_articles']} 篇新文章")
                        return result
                    else:
                        print(f"❌ 聚合失败: {result.get('error')}")
                        return result
                    
                except Exception as e:
                    print(f"❌ 认证聚合过程出错: {e}")
                    await context.close()
                    import traceback
                    traceback.print_exc()
                    
                    return {
                        'success': False,
                        'error': f'认证聚合异常: {str(e)}',
                        'articles': [],
                        'stats': {'new_articles': 0, 'skipped_articles': 0}
                    }
                    
        except Exception as e:
            print(f"❌ 认证聚合失败: {e}")
            import traceback
            traceback.print_exc()
            
            return {
                'success': False,
                'error': f'认证聚合异常: {str(e)}',
                'articles': [],
                'stats': {'new_articles': 0, 'skipped_articles': 0}
            }

