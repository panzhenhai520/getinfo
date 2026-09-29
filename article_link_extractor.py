#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通用文章链接提取器 - 增强版
从栏目页发现文章链接并逐篇抽取正文
支持智能提取 + 智能验证 + 自动入库
核心功能：
1. 提取链接后，先打开验证是否为真正的文章
2. 使用多种Python库（newspaper3k、trafilatura）综合判断
3. 只保存确认为文章的内容
"""

import re
import json
import html as html_lib
import os
import requests
from collections import Counter
from urllib.parse import urljoin, urlparse
from typing import List, Dict, Tuple, Optional
import time
from datetime import datetime, timedelta
from utils import get_china_time
import config
from crawl_options import normalize_crawl_options
from outbound_url_policy import (
    install_playwright_url_guard,
    install_sync_playwright_url_guard,
    safe_request_get,
    safe_request_head,
    validate_outbound_url,
)


def _requests_get_with_proxy_intent(url, proxies=None, **kwargs):
    """Use requests.get while treating {} as explicit direct connection."""
    kwargs['proxies'] = proxies
    if isinstance(proxies, dict) and not proxies:
        with requests.Session() as session:
            session.trust_env = False
            return safe_request_get(url, session=session, **kwargs)
    return safe_request_get(url, **kwargs)


def _requests_head_with_proxy_intent(url, proxies=None, **kwargs):
    """Use requests.head while treating {} as explicit direct connection."""
    kwargs['proxies'] = proxies
    if isinstance(proxies, dict) and not proxies:
        with requests.Session() as session:
            session.trust_env = False
            return safe_request_head(url, session=session, **kwargs)
    return safe_request_head(url, **kwargs)

# 导入现有的数据库模块
from sqlite_database import SQLiteDatabase

# 🔥 导入关键词过滤器（支持简繁体）
from keyword_filter import KeywordFilter

# 导入智能验证器
try:
    from universal_article_validator import UniversalArticleValidator
    HAS_VALIDATOR = True
    print("✅ 智能验证器已加载")
except ImportError:
    HAS_VALIDATOR = False
    print("⚠️ 智能验证器未加载，将使用基础验证")

class ArticleLinkExtractor:
    """文章链接提取器 - 支持智能提取和自动入库"""
    
    def __init__(
        self,
        db: Optional[SQLiteDatabase] = None,
        enable_smart_validation: bool = True,
        crawl_options: Optional[Dict] = None
    ):
        """
        初始化提取器
        
        Args:
            db: SQLiteDatabase实例，如果为None则自动创建
            enable_smart_validation: 是否启用智能验证（默认True）
            crawl_options: 聚合运行配置，用于同步代理等任务级开关
        """
        # Legacy attributes kept only so old code can inspect them safely.
        self.firecrawl_crawl_url = ''
        self.firecrawl_scrape_url = ''
        self.firecrawl_api_url = ''
        self.crawl_options = normalize_crawl_options(crawl_options or {})
        print("✅ ArticleLinkExtractor 已初始化：主流程使用 Playwright，不调用 Firecrawl")
        
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
        })
        
        # 使用现有数据库或创建新的
        if db is None:
            self.db = SQLiteDatabase()
            self.db.connect()
            print(f"✅ 使用数据库: {self.db.db_path}")
        else:
            self.db = db
            print(f"✅ 使用外部数据库实例")
        
        # 初始化智能验证器
        self.enable_smart_validation = enable_smart_validation and HAS_VALIDATOR
        if self.enable_smart_validation:
            self.validator = UniversalArticleValidator(proxies=self._validator_proxies())
            print(f"✅ 智能验证器已启用")
        else:
            self.validator = None
            if enable_smart_validation and not HAS_VALIDATOR:
                print(f"⚠️ 智能验证器未安装，将使用基础验证")

    def _validator_proxies(self):
        """Return validator proxy intent: dict for proxy, {} for explicit direct."""
        try:
            proxy_enabled = bool((self.crawl_options or {}).get('proxy_enabled'))
            return config.get_proxies(enabled=proxy_enabled) if proxy_enabled else {}
        except Exception:
            return {}

    def _sync_validator_proxy(self):
        if self.enable_smart_validation and HAS_VALIDATOR:
            self.validator = UniversalArticleValidator(proxies=self._validator_proxies())
    
    def extract_links_from_markdown(self, markdown_content: str, base_url: str = "") -> List[Dict]:
        """
        从markdown内容中提取文章链接
        
        Args:
            markdown_content: markdown文本内容
            base_url: 基础URL，用于处理相对链接
            
        Returns:
            List[Dict]: 包含链接信息的字典列表
        """
        links = []
        seen_urls = set()  # 用于去重
        seen_base_urls = set()  # 用于检测重复链接（如"全文"）
        
        # 特殊处理：匹配君合网站的链接格式（包含日期）
        # 格式: [### 标题\\\n    \\\n    日期](URL)
        junhe_pattern = r'\[\s*###\s*([^\\\n]+)\s*\\\s*\\\s*(\d{4}\.\d{1,2}\.\d{1,2})\s*\]\(([^)]+)\)'
        junhe_matches = re.findall(junhe_pattern, markdown_content)
        
        for title, date_str, url in junhe_matches:
            # 跳过JavaScript链接和锚点链接
            if url.startswith('javascript:') or url.startswith('#') or url.startswith('mailto:'):
                continue
                
            # 处理相对链接
            if base_url and not url.startswith('http'):
                url = urljoin(base_url, url)
            
            # 🔥 方案2: 通用URL规范化（防止编码导致404）
            url = self._normalize_url_encoding(url)
            
            # 去重检查
            if url in seen_urls:
                continue
            seen_urls.add(url)
            
            # 🔥 方案3: 对包含编码的URL进行验证
            if '%E' in url or '%e' in url or '%2' in url:
                if not self._validate_url_before_crawl(url):
                    # 验证失败，尝试智能重试
                    retry_url = self._smart_retry_url(url)
                    if retry_url:
                        url = retry_url
                    else:
                        continue  # 重试也失败，跳过此链接
            
            # 转换日期格式 YYYY.MM.DD -> YYYY-MM-DD
            publish_date = None
            try:
                parts = date_str.split('.')
                if len(parts) == 3:
                    publish_date = f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"
            except:
                pass
            
            links.append({
                'title': title.strip(),
                'url': url,
                'text': title.strip(),
                'publish_date': publish_date
            })
        
        # 匹配普通markdown链接格式 [文本](链接)
        markdown_link_pattern = r'\[([^\]]+)\]\(([^)]+)\)'
        matches = re.findall(markdown_link_pattern, markdown_content)
        
        for text, url in matches:
            # 跳过JavaScript链接和锚点链接
            if url.startswith('javascript:') or url.startswith('#') or url.startswith('mailto:'):
                continue
                
            # 处理相对链接
            if base_url and not url.startswith('http'):
                url = urljoin(base_url, url)
            
            # 🔥 方案2: 通用URL规范化（防止编码导致404）
            url = self._normalize_url_encoding(url)
            
            # 去重检查（已被君合格式提取过的会被跳过）
            if url in seen_urls:
                continue
            seen_urls.add(url)
            
            # 🔥 方案3: 对包含编码的URL进行验证
            if '%E' in url or '%e' in url or '%2' in url:
                if not self._validate_url_before_crawl(url):
                    retry_url = self._smart_retry_url(url)
                    if retry_url:
                        url = retry_url
                    else:
                        continue
            
            # 检查是否为重复链接（如"全文"）
            if self._is_duplicate_link(text, url, seen_base_urls):
                continue
            
            # 只排除明显不是文章的链接（宽松过滤）
            if not self._is_obviously_not_article(text, url):
                links.append({
                    'title': text.strip(),
                    'url': url,
                    'text': text.strip(),
                    'publish_date': None  # 普通格式没有日期
                })
        
        # 也匹配HTML格式的链接 <a href="...">文本</a>
        html_link_pattern = r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]+)</a>'
        html_matches = re.findall(html_link_pattern, markdown_content, re.IGNORECASE)
        
        for url, text in html_matches:
            if url.startswith('javascript:') or url.startswith('#') or url.startswith('mailto:'):
                continue
                
            if base_url and not url.startswith('http'):
                url = urljoin(base_url, url)
            
            # 🔥 方案2: 通用URL规范化（防止编码导致404）
            url = self._normalize_url_encoding(url)
            
            # 去重检查
            if url in seen_urls:
                continue
            seen_urls.add(url)
            
            # 🔥 方案3: 对包含编码的URL进行验证
            if '%E' in url or '%e' in url or '%2' in url:
                if not self._validate_url_before_crawl(url):
                    retry_url = self._smart_retry_url(url)
                    if retry_url:
                        url = retry_url
                    else:
                        continue
            
            # 检查是否为重复链接（如"全文"）
            if self._is_duplicate_link(text, url, seen_base_urls):
                continue
                
            # 只排除明显不是文章的链接（宽松过滤）
            if not self._is_obviously_not_article(text, url):
                links.append({
                    'title': text.strip(),
                    'url': url,
                    'text': text.strip(),
                    'publish_date': None  # HTML格式没有日期
                })
        
        return links
    
    def _has_auth_storage(self, url: str) -> bool:
        """
        检查URL对应的域名是否有认证存储（通用方法）
        
        Args:
            url: 网页URL
            
        Returns:
            bool: True=有认证配置, False=无认证配置
        """
        try:
            import os
            from urllib.parse import urlparse
            
            domain = urlparse(url).netloc
            domain_key = domain.replace('www.', '').replace('.', '_')
            storage_dir = "auth_storage"
            
            if not os.path.exists(storage_dir):
                return False
            
            # 遍历存储目录，查找匹配的认证文件
            for filename in os.listdir(storage_dir):
                # 支持两种格式：*_storage.json 和 *.json（排除_info.json）
                if filename.endswith('.json') and not filename.endswith('_info.json'):
                    filename_lower = filename.lower()
                    domain_parts = domain_key.split('_')
                    
                    for part in domain_parts:
                        if part and len(part) > 3 and part in filename_lower:
                            return True
            
            return False
        except:
            return False
    
    def _is_duplicate_link(self, text: str, url: str, seen_base_urls: set) -> bool:
        """
        检查是否为重复链接（如"全文"链接）
        
        Args:
            text: 链接文本
            url: 链接URL
            seen_base_urls: 已见过的基础URL集合
            
        Returns:
            bool: True表示是重复链接，应该跳过
        """
        # 检查是否为"全文"、"阅读全文"等常见重复链接文本
        duplicate_texts = ['全文', '阅读全文', 'read more', 'more', '更多', '详情', '查看详情']
        text_lower = text.lower().strip()
        
        if any(dup in text_lower for dup in duplicate_texts):
            # 提取URL的基础部分（去掉查询参数）
            from urllib.parse import urlparse
            parsed = urlparse(url)
            base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            
            # 如果基础URL已经见过，说明是重复的
            if base_url in seen_base_urls:
                return True
            
            # 将基础URL加入已见集合
            seen_base_urls.add(base_url)
        
        return False
    
    def _is_obviously_not_article(self, text: str, url: str) -> bool:
        """
        判断链接是否明显不是文章（宽松过滤，只排除显而易见的非文章链接）
        
        Args:
            text: 链接文本
            url: 链接URL
            
        Returns:
            bool: True表示明显不是文章，应该跳过
        """
        text = text.strip()
        text_lower = text.lower()
        url_lower = url.lower()
        
        # 1. 排除空文本或超短文本
        if len(text) < 5:
            return True
        
        # 2. 排除纯数字、纯符号
        if re.match(r'^[\d\.\,\-\+\%\s]+$', text):
            return True
        
        # 3. 排除明显的功能性URL
        exclude_url_patterns = [
            r'/login', r'/register', r'/signup', r'/signin',
            r'/logout', r'/search', r'/sitemap',
            r'\.(pdf|doc|docx|xls|xlsx|zip|rar|png|jpg|jpeg|gif|svg|webp)$',  # 文件下载和图片
        ]
        
        for pattern in exclude_url_patterns:
            if re.search(pattern, url_lower):
                return True
        
        # 3.5 排除明显静态资源域名；不按具体站点域名硬编码，避免误杀真实栏目/文章。
        try:
            host = (urlparse(url).hostname or '').lower()
            if host.startswith(('static.', 'assets.', 'cdn.', 'img.', 'image.', 'media.')):
                return True
        except Exception:
            pass
        
        # 4. 排除明显的导航链接（更严格的关键词）
        obvious_nav_keywords = [
            '登录', '注册', 'login', 'register', 'sign up', 'sign in',
            'sitemap', '网站地图', 'search', '搜索',
        ]
        
        if any(keyword in text_lower for keyword in obvious_nav_keywords):
            return True
        
        # 默认：可能是文章，不排除
        return False
    
    def _evaluate_content_quality(self, content_result: Dict) -> int:
        """
        评估聚合内容的质量，判断是否为真正的文章
        
        Args:
            content_result: crawl_article_content的返回结果
            
        Returns:
            int: 质量分数 (0-100)，>60认为是有效文章
        """
        if not content_result.get('success'):
            return 0
        
        content = content_result.get('content', '')
        title = content_result.get('title', '')
        
        # 🚫 优先检测404/错误页（直接返回0分）
        error_keywords = ['not found', '404', 'page not found', 'does not exist', 
                         '找不到', '页面不存在', '页面未找到', '无法找到']
        content_lower = content.lower()
        title_lower = title.lower()
        
        for keyword in error_keywords:
            if keyword in title_lower or keyword in content_lower[:200]:
                print(f"      🚫 检测到错误页关键词: '{keyword}'")
                return 0
        
        # 🚫 检测单词导航标题（Stock、Currency、Etf等）
        # 去除网站名后的标题
        title_clean = title.split('|')[0].split('-')[0].strip()
        meaningless_single_words = [
            'stock', 'currency', 'etf', 'international', 'current', 'smart',
            'forex', 'crypto', 'bonds', 'commodities', 'index', 'market'
        ]
        
        # 检查是否是单个无意义单词
        if len(title_clean) <= 15 and title_clean.lower() in meaningless_single_words:
            print(f"      🚫 单词导航标题: '{title_clean}'")
            return 0
        
        # 检测纯日期标题（11月5日）
        if re.match(r'^\d{1,2}月\d{1,2}日$', title_clean):
            print(f"      🚫 纯日期标题: '{title_clean}'")
            return 0
        
        score = 0
        
        # 1. 内容长度评分 (最高40分) - 降低最低要求
        content_length = len(content)
        if content_length >= 500:
            score += 40
        elif content_length >= 300:
            score += 35
        elif content_length >= 200:
            score += 30
        elif content_length >= 150:
            score += 25
        elif content_length >= 100:
            score += 20
        elif content_length >= 50:  # 降低最低要求从100到50
            score += 10
        else:
            # 内容很短，但如果有好标题，可能仍然是文章
            if title and len(title) > 10:
                score += 5  # 给一个基础分
            else:
                return 0  # 内容和标题都太短，判定为无效
        
        # 2. 标题质量评分 (最高20分) - 提高权重
        if title and title != '无标题' and title != 'Untitled':
            title_length = len(title)
            if 10 <= title_length <= 100:
                score += 20  # 提高从15到20
            elif 5 <= title_length < 10 or title_length > 100:
                score += 10  # 提高从8到10
            
            # 额外奖励：如果标题包含明显的新闻特征
            news_keywords = ['蚀讓', '租出', '售出', '成交', '回報', '升值', '下跌', 
                           '新盘', '盘点', '楼市', '物业', '房产', '地产',
                           '签署', '完成', '助力', '发布', '获得', '达成']
            if any(kw in title for kw in news_keywords):
                score += 10  # 新闻标题特征加分
        
        # 3. 内容结构评分 (最高30分) - 调整评分策略
        # 检查段落数量（降低段落长度要求）
        paragraphs = [p.strip() for p in content.split('\n') if len(p.strip()) > 20]  # 从30降到20
        if len(paragraphs) >= 3:
            score += 15
        elif len(paragraphs) >= 2:
            score += 12
        elif len(paragraphs) >= 1:
            score += 8  # 提高单段落分数
        
        # 检查句子完整性（降低句子长度要求）
        sentences = re.split(r'[。！？.!?、，]', content)  # 增加更多分隔符
        complete_sentences = [s for s in sentences if len(s.strip()) > 10]  # 从15降到10
        if len(complete_sentences) >= 5:
            score += 15
        elif len(complete_sentences) >= 3:
            score += 10
        elif len(complete_sentences) >= 2:  # 新增：2个句子也给分
            score += 8
        elif len(complete_sentences) >= 1:  # 新增：1个句子也给分
            score += 5
        
        # 4. 内容质量评分 (最高25分)
        # 检查中文字符比例
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', content))
        if chinese_chars > 0:
            chinese_ratio = chinese_chars / max(content_length, 1)
            if chinese_ratio > 0.5:
                score += 15  # 提高从10到15
            elif chinese_ratio > 0.3:
                score += 10  # 提高从5到10
            elif chinese_ratio > 0.1:
                score += 5  # 新增：少量中文也给分
        
        # 检查是否包含大量日期（可能是文章列表）
        date_patterns = [
            r'20\d{2}[/-]\d{1,2}[/-]\d{1,2}',
            r'20\d{2}\s*/\s*\d+\s*/\s*\d+',
            r'\d{4}年\d{1,2}月\d{1,2}日'
        ]
        total_dates = sum(len(re.findall(pattern, content)) for pattern in date_patterns)
        if total_dates <= 3:  # 放宽到3个日期（考虑引用其他日期）
            score += 10
        elif total_dates <= 6:
            score += 5
        else:  # 太多日期，可能是列表页
            score -= 15  # 减轻惩罚从-20到-15
        
        # 5. 负面内容检测（减分项）
        # 检查是否主要是导航/声明内容
        unwanted_patterns = [
            r'印刷版出报日', r'休刊日', r'假期安排',
            r'版权所有', r'All Rights Reserved',
            r'Cookie Policy', r'隐私政策', r'服务条款',
            r'相关文章.*相关文章.*相关文章',  # 多个"相关文章"
            r'更多.*更多.*更多',  # 多个"更多"
        ]
        
        unwanted_count = sum(len(re.findall(pattern, content, re.IGNORECASE)) 
                           for pattern in unwanted_patterns)
        if unwanted_count > 3:
            score -= 30
        elif unwanted_count > 0:
            score -= 10
        
        # 6. URL重复度检测（减分项）
        urls = re.findall(r'https?://[^\s]+', content)
        if len(urls) > 15:  # 放宽到15个URL（考虑参考链接）
            score -= 20
        elif len(urls) > 10:
            score -= 10  # 中等数量URL减少惩罚
        
        # 7. 短新闻特殊处理（新增）
        # 如果内容很短但质量很高（有好标题+中文内容+正常结构），可能是短新闻
        if 50 <= content_length < 200:
            if title and len(title) > 10 and chinese_chars > 30:
                score += 15  # 短新闻补偿分
                print(f"      💡 检测到短新闻，额外加分")
        
        final_score = max(0, min(100, score))
        
        # 调试信息：显示评分详情
        if final_score > 0:
            print(f"      📊 评分详情: 长度={content_length}, 中文={chinese_chars}, 段落={len(paragraphs)}, 句子={len(complete_sentences)}, 日期={total_dates}, URLs={len(urls)}")
        
        return final_score

    def _assess_extracted_content_integrity(self, content: str, title: str = '', extract_result: Dict = None) -> Dict:
        """Generic safety gate before local DB insert and RAGFlow upload."""
        extract_result = extract_result or {}
        inherited = extract_result.get('integrity') if isinstance(extract_result.get('integrity'), dict) else None
        try:
            from smart_article_extractor import assess_content_integrity
            current = assess_content_integrity(content or '', title=title or '')
        except Exception:
            issues = []
            stripped = (content or '').strip()
            if not stripped:
                issues.append('empty_content')
            if re.match(r'^[)）\]】},，。；;：:！？!?、]+', stripped):
                issues.append('leading_orphan_punctuation')
            current = {
                'ok': not issues,
                'issues': issues,
                'reason': ','.join(issues),
            }

        merged_issues = []
        for source in (inherited, current):
            if isinstance(source, dict):
                for issue in source.get('issues') or []:
                    if issue and issue not in merged_issues:
                        merged_issues.append(issue)

        ok = bool(current.get('ok', True))
        if isinstance(inherited, dict) and inherited.get('ok') is False:
            ok = False

        return {
            'ok': ok,
            'issues': merged_issues,
            'reason': ','.join(merged_issues),
        }
    
    def crawl_article_content(self, url: str, timeout: int = 120) -> Dict:
        """
        聚合文章内容（Playwright + 多解析器方案）
        
        Args:
            url: 文章URL
            timeout: 超时时间
            
        Returns:
            Dict: 包含文章内容的字典
        """
        try:
            print(f"🌐 正在使用智能文章提取器聚合: {url[:80]}...")
            from smart_article_extractor import extract_article_content_from_url

            proxy_enabled = bool((self.crawl_options or {}).get('proxy_enabled'))
            proxies = config.get_proxies(enabled=proxy_enabled) if proxy_enabled else {}
            result = extract_article_content_from_url(
                url,
                proxies=proxies,
                skip_db_check=True,
                timeout=timeout,
            )
            if result.get('success'):
                return {
                    'success': True,
                    'url': result.get('url', url),
                    'title': result.get('title', 'Untitled'),
                    'content': result.get('content', ''),
                    'publish_date': result.get('publish_date'),
                    'authors': result.get('authors', []),
                    'extraction_method': result.get('method', 'smart_article_extractor'),
                    'quality_score': result.get('score', 0),
                    'crawled_at': get_china_time().isoformat()
                }
            
            return {
                'success': False,
                'url': url,
                'error': result.get('error', '智能文章提取失败'),
                'crawled_at': get_china_time().isoformat()
            }
        except Exception as e:
            print(f"❌ 智能文章提取异常: {e}")
            return {
                'success': False,
                'url': url,
                'error': str(e),
                'crawled_at': get_china_time().isoformat()
            }

    def _crawl_with_playwright_fallback(self, url: str) -> Dict:
        """
        使用 Playwright 作为降级方案聚合文章（用于动态渲染、反爬较强的网站）
        
        Args:
            url: 文章URL
            
        Returns:
            Dict: 包含文章内容的字典
        """
        try:
            import asyncio
            from playwright.async_api import async_playwright
            
            print(f"🎭 启动 Playwright 降级方案...")
            
            async def fetch_with_playwright():
                from crawler_resource_manager import playwright_slot

                async with playwright_slot(f"article-fallback:{url}"):
                    async with async_playwright() as p:
                    # 启动浏览器
                        browser = await p.chromium.launch(
                            headless=True,
                            args=[
                                '--disable-blink-features=AutomationControlled',
                                '--no-sandbox',
                                '--disable-setuid-sandbox'
                            ]
                        )
                    
                    # 创建上下文
                        context = await browser.new_context(
                            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                            viewport={'width': 1920, 'height': 1080}
                        )
                        await install_playwright_url_guard(context)
                    
                    # 🔑 通用认证：自动为所有网站加载已保存的cookies
                    try:
                        import os
                        from urllib.parse import urlparse
                        
                        # 提取域名
                        domain = urlparse(url).netloc
                        # 移除 www. 前缀
                        domain_key = domain.replace('www.', '').replace('.', '_')
                        
                        # 查找对应的认证存储文件
                        storage_dir = "auth_storage"
                        if os.path.exists(storage_dir):
                            # 遍历存储目录，找到匹配域名的cookies文件
                            for filename in os.listdir(storage_dir):
                                # 支持两种格式：*_storage.json 和 *.json（排除_info.json）
                                if filename.endswith('.json') and not filename.endswith('_info.json'):
                                    # 检查文件名是否包含域名关键词
                                    filename_lower = filename.lower()
                                    # 提取域名的主要部分用于匹配认证存储文件
                                    domain_parts = domain_key.split('_')
                                    
                                    # 如果文件名包含域名的任意部分，就认为匹配
                                    matched = False
                                    for part in domain_parts:
                                        if part and len(part) > 3 and part in filename_lower:
                                            matched = True
                                            break
                                    
                                    if matched:
                                        storage_path = os.path.join(storage_dir, filename)
                                        print(f"   🔐 找到认证配置: {filename}")
                                        
                                        # 加载cookies到上下文
                                        with open(storage_path, 'r', encoding='utf-8') as f:
                                            storage_state = json.load(f)
                                            if 'cookies' in storage_state:
                                                await context.add_cookies(storage_state['cookies'])
                                                print(f"   ✅ 已加载 {len(storage_state['cookies'])} 个cookies")
                                        break
                    except Exception as e:
                        print(f"   ⚠️  加载cookies失败: {e}，使用未认证状态")
                    
                    # 打开页面
                    page = await context.new_page()
                    
                    try:
                        print(f"   🌐 访问页面: {url[:80]}...")
                        await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                        
                        try:
                            await page.wait_for_selector(
                                'article, main, [itemprop="articleBody"], .article-content, .article-body, .entry-content, .post-content, #article-content',
                                timeout=5000,
                            )
                            await asyncio.sleep(2)
                        except Exception:
                            pass
                        
                        # 获取 HTML
                        html_content = await page.content()
                        
                        print(f"   ✅ Playwright 获取成功（HTML: {len(html_content)} 字符）")
                        
                        # 使用 newspaper3k 提取
                        try:
                            from newspaper import Article as NewspaperArticle
                            
                            article_obj = NewspaperArticle(url, language='zh')
                            article_obj.download_state = 2
                            article_obj.html = html_content
                            article_obj.parse()
                            
                            extracted_content = article_obj.text
                            extracted_title = article_obj.title
                            extracted_date = article_obj.publish_date.strftime('%Y-%m-%d') if article_obj.publish_date else ''
                            
                            if extracted_content and len(extracted_content) >= 100:
                                score = min(100, len(extracted_content) // 10)
                                print(f"   ✅ Playwright + newspaper3k 提取成功（质量: {score}，内容: {len(extracted_content)} 字符）")
                                
                                return {
                                    'success': True,
                                    'url': url,
                                    'title': extracted_title or 'Untitled',
                                    'content': extracted_content,
                                    'publish_date': extracted_date,
                                    'extraction_method': 'playwright+newspaper3k',
                                    'quality_score': score,
                                    'html': html_content,
                                    'crawled_at': get_china_time().isoformat()
                                }
                            else:
                                print(f"   ⚠️ Playwright 提取内容太短: {len(extracted_content)} 字符")
                        except Exception as e:
                            print(f"   ⚠️ newspaper3k 处理异常: {e}")
                        
                        # 如果提取失败，返回原始 HTML
                        return {
                            'success': False,
                            'url': url,
                            'error': 'Playwright 提取内容不足',
                            'html': html_content,
                            'crawled_at': get_china_time().isoformat()
                        }
                        
                    finally:
                        await browser.close()
            
            # 运行异步函数
            return asyncio.run(fetch_with_playwright())
            
        except Exception as e:
            print(f"   ❌ Playwright 降级失败: {e}")
            return {
                'success': False,
                'url': url,
                'error': f'Playwright 降级失败: {str(e)}',
                'crawled_at': get_china_time().isoformat()
            }
    
    def _extract_title_from_content(self, content: str) -> str:
        """从内容中提取标题"""
        lines = content.split('\n')
        
        # 首先尝试从URL中提取标题信息
        # 如果内容包含URL，尝试从URL中提取信息
        for line in lines:
            if 'mailto:' in line and 'subject=' in line:
                # 从mailto链接中提取标题
                import urllib.parse
                try:
                    # 提取subject参数
                    if 'subject=' in line:
                        subject_part = line.split('subject=')[1].split('&')[0]
                        title = urllib.parse.unquote(subject_part)
                        if title and len(title) > 5:
                            return title[:100]
                except:
                    pass
        
        # 然后尝试从内容中提取标题
        for line in lines:
            line = line.strip()
            # 跳过空行、markdown链接、javascript代码等
            if (line and 
                not line.startswith('#') and 
                not line.startswith('[](') and 
                not line.startswith('javascript:') and
                not line.startswith('mailto:') and
                not line.startswith('http') and
                not line.startswith('![](http') and
                not line.startswith('2025 /') and  # 跳过日期
                not line.startswith('2024 /') and
                not line.startswith('2023 /') and
                len(line) > 10 and
                len(line) < 200):  # 标题不应该太长
                
                # 移除markdown格式
                title = re.sub(r'[#*_`\[\]()]', '', line).strip()
                # 移除多余的空白字符
                title = re.sub(r'\s+', ' ', title).strip()
                
                # 检查是否包含中文或英文单词（避免纯数字或特殊字符）
                if (title and 
                    (re.search(r'[\u4e00-\u9fff]', title) or  # 包含中文
                     re.search(r'[a-zA-Z]{3,}', title))):     # 包含3个以上英文字母
                    return title[:100]  # 限制标题长度
        
        return "无标题"
    
    def _extract_publish_date(self, content: str) -> str:
        """从内容中提取发布时间"""
        lines = content.split('\n')
        
        # 查找日期格式：2025 / 10 / 11 或 2025/10/11
        date_patterns = [
            r'(\d{4})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})',  # 2025 / 10 / 11
            r'(\d{4})-(\d{1,2})-(\d{1,2})',              # 2025-10-11
            r'(\d{4})年(\d{1,2})月(\d{1,2})日',           # 2025年10月11日
        ]
        
        # 🔥 活动类标签（会议时间/报名时间等）行不是发布时间，跳过
        activity_labels = ('会议时间', '会议日期', '举办时间', '活动时间', '召开时间',
                           '报名时间', '展会时间', '开始时间', '结束时间')
        for line in lines:
            line = line.strip()
            if line and not any(label in line for label in activity_labels):
                for pattern in date_patterns:
                    match = re.search(pattern, line)
                    if match:
                        year, month, day = match.groups()
                        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
        
        return "未知时间"
    
    def _clean_article_content(self, content: str) -> str:
        """清洗文章内容，只保留核心内容"""
        # 首先全局清理空链接和无关内容
        content = re.sub(r'\[\]\([^)]*\)', '', content)  # 移除空链接 [](xxx)
        content = re.sub(r'\[mailto:\?[^\]]*\]\([^)]*\)', '', content)  # 移除mailto链接
        content = re.sub(r'\[javascript:[^\]]*\]\([^)]*\)', '', content)  # 移除javascript链接
        
        # 检测并截断文章底部的导航和相关链接部分
        navigation_markers = [
            r'上一篇[：:]\s*',
            r'下一篇[：:]\s*',
            r'更多阅读',
            r'相关阅读',
            r'推荐阅读',
            r'相关文章',
            r'相关专业人士',
            r'方达团队',
            r'严正声明',
            r'我们特别提请',
            r'\*\*李荣\*\*.*\*\*纪东\*\*',
            r'【往期精彩】',
            r'声明全文'
        ]
        
        # 找到最早出现的导航标记位置
        earliest_position = len(content)
        for marker in navigation_markers:
            match = re.search(marker, content, re.IGNORECASE)
            if match:
                earliest_position = min(earliest_position, match.start())
        
        # 如果找到导航标记，截断内容
        if earliest_position < len(content):
            content = content[:earliest_position]
        
        lines = content.split('\n')
        cleaned_lines = []
        
        # 标记是否开始提取正文内容
        content_started = False
        content_ended = False
        
        for line in lines:
            line = line.strip()
            
            # 跳过空行
            if not line:
                continue
                
            # 跳过明显的非内容行
            if (line.startswith('[](') or 
                line.startswith('javascript:') or
                line.startswith('mailto:') or
                line.startswith('![](http') or
                line.startswith('相关专业人士') or
                line.startswith('上一篇') or
                line.startswith('下一篇') or
                line.startswith('更多阅读') or
                line.startswith('首页') or
                line.startswith('严正声明') or
                line.startswith('我们特别提请') or
                line.startswith('声明全文') or
                '方达律师事务所订阅号' in line or
                '方达律师事务所服务号' in line):
                continue
            
            # 检测正文开始（通常是日期后的第一段有意义的内容）
            if not content_started:
                # 如果遇到日期格式，说明正文即将开始
                if re.search(r'\d{4}\s*/\s*\d{1,2}\s*/\s*\d{1,2}', line):
                    content_started = True
                    continue
                # 如果遇到长文本（可能是正文），开始提取
                elif len(line) > 50 and re.search(r'[\u4e00-\u9fff]', line):
                    content_started = True
                    cleaned_lines.append(line)
                continue
            
            # 检测正文结束（遇到导航或相关文章链接）
            if (line.startswith('[') and '](' in line and 
                ('details34_' in line or 'print34_' in line)):
                content_ended = True
                break
            
            # 如果正文已开始且未结束，添加内容
            if content_started and not content_ended:
                # 移除markdown格式
                clean_line = re.sub(r'[#*_`\[\]()]', '', line).strip()
                if clean_line:
                    cleaned_lines.append(clean_line)
        
        return '\n'.join(cleaned_lines)
    
    
    def _is_article_page(self, url: str) -> bool:
        """
        通过页面结构判断是否为文章详情页
        
        Args:
            url: 页面URL
            
        Returns:
            bool: True=文章页, False=列表页/导航页
        """
        try:
            from playwright.sync_api import sync_playwright
            import time
            
            from crawler_resource_manager import sync_playwright_slot

            with sync_playwright_slot(f"is-article-page:{url}"):
                with sync_playwright() as p:
                # 启动浏览器
                    import config
                    proxy_config = config.get_playwright_proxy(enabled=self.crawl_options.get('proxy_enabled'))
                
                    browser = p.chromium.launch(headless=True, proxy=proxy_config)
                    context = browser.new_context(
                        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    )
                    install_sync_playwright_url_guard(context)
                    page = context.new_page()
                
                # 访问页面 - 等待网络空闲，确保内容加载完成
                try:
                    page.goto(url, wait_until='networkidle', timeout=15000)
                except:
                    # 如果networkidle超时，回退到domcontentloaded
                    page.goto(url, wait_until='domcontentloaded', timeout=10000)
                
                # 🔥 增加等待时间，确保动态内容加载完成
                time.sleep(6)  # 从2秒增加到6秒
                
                # 🔥 额外等待：尝试等待文章内容元素出现
                try:
                    # 等待常见的文章内容容器
                    page.wait_for_selector('article, .article, .content, .post-content, main', timeout=3000)
                except:
                    pass  # 如果没有这些元素，继续处理
                
                # 获取页面内容
                page_content = page.content()
                page_text = page.inner_text('body')
                
                # 🚫 优先检测明显的非文章页
                # 1. 检测404错误页
                error_patterns = [
                    'not found', '404', 'page not found', 'file not found',
                    '找不到', '页面不存在', '错误', 'error'
                ]
                page_text_lower = page_text.lower()
                page_title = page.title().lower()
                
                # 🔑 通用处理：检查是否有认证配置，如果有则跳过错误页检测
                if not self._has_auth_storage(url):
                    # 没有认证配置，执行正常的错误页检测
                    for pattern in error_patterns:
                        if pattern in page_text_lower[:200] or pattern in page_title:
                            print(f"      🚫 检测到错误页: '{pattern}' in 页面")
                            browser.close()
                            return False
                else:
                    print(f"      ⏭️  检测到认证配置，跳过错误页检测")
                
                # 2. 检测标题是否过短或无意义（导航页特征）
                title = page.title()
                if title:
                    # 去除网站名
                    title_clean = title.split('|')[0].split('-')[0].strip()
                    
                    # 标题太短（无论是否有数字）
                    if len(title_clean) <= 10:
                        # 检查是否是无意义的单词或日期
                        meaningless_titles = [
                            'stock', 'currency', 'etf', 'international', 'current', 'smart',
                            'home', 'index', 'news', 'about', 'contact'
                        ]
                        # 或者是纯日期格式（如"11月5日"、"2025-11-07"）
                        import re
                        is_date_only = bool(re.match(r'^\d{1,2}月\d{1,2}日$', title_clean) or 
                                          re.match(r'^\d{4}[-/]\d{1,2}[-/]\d{1,2}$', title_clean))
                        
                        if title_clean.lower() in meaningless_titles or is_date_only:
                            print(f"      🚫 无意义标题: '{title_clean}'")
                            browser.close()
                            return False
                
                # 3. 检测是否是纯导航页面（内容极少）- 提高阈值
                # 🔥 但是如果URL明显是文章详情页格式，即使内容少也应该尝试聚合
                is_obvious_article_url = any([
                    '/article/' in url.lower(),
                    '/post/' in url.lower(),
                    '/news/' in url.lower() and re.search(r'/\d{6,}', url),  # 新闻+ID
                    '/detail/' in url.lower(),
                    re.search(r'/id/\d+', url),  # /id/数字
                    re.search(r'/\d{7,}/', url),  # 7位以上数字（文章ID）
                ])
                
                if len(page_text.strip()) < 200:  # 从100提高到200
                    if is_obvious_article_url:
                        print(f"      ℹ️  内容较少({len(page_text)} 字符)，但URL格式像文章页，直接判定为文章页")
                        browser.close()
                        return True  # 🔥 直接返回True，不再进行后续评分
                    else:
                        print(f"      🚫 内容过少: {len(page_text)} 字符")
                        browser.close()
                        return False
                
                # 统计页面特征
                article_score = 0
                list_score = 0
                
                # 🔥 如果URL格式明显是文章页，给予高分加成
                if is_obvious_article_url:
                    article_score += 50
                    print(f"      ✅ URL格式像文章页，文章分+50")
                
                # 1. 检查文章容器类名（高权重）
                article_indicators = [
                    'article', 'post-content', 'entry-content', 'detail-content',
                    'article-body', 'post-body', 'content-body', 'main-content'
                ]
                for indicator in article_indicators:
                    if indicator in page_content.lower():
                        article_score += 30
                        break
                
                # 2. 检查列表特征（负面指标）
                list_indicators = ['news-list', 'article-list', 'list-item', 'pagination']
                for indicator in list_indicators:
                    if indicator in page_content.lower():
                        list_score += 20
                
                # 3. 统计链接数量（列表页有很多链接）
                link_count = len(page.query_selector_all('a'))
                if link_count > 50:
                    list_score += 30
                elif link_count < 20:
                    article_score += 20
                
                # 4. 检查日期出现次数（列表页有多个日期）
                import re
                date_pattern = r'20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}'
                dates = re.findall(date_pattern, page_text)
                if len(dates) > 5:
                    list_score += 30  # 多个日期，可能是列表
                elif len(dates) == 1:
                    article_score += 20  # 单个日期，可能是文章发布日期
                
                # 5. 检查段落数量（文章页有较多段落）
                p_tags = page.query_selector_all('p')
                paragraph_count = len([p for p in p_tags if len(p.inner_text()) > 20])
                if paragraph_count > 5:
                    article_score += 30
                elif paragraph_count < 3:
                    list_score += 20
                
                # 6. 检查文本长度（文章页内容较长）
                text_length = len(page_text)
                if text_length > 1000:
                    article_score += 30
                elif text_length < 300:
                    list_score += 20
                
                browser.close()
                
                # 判断结果
                is_article = article_score > list_score
                
                print(f"      页面分析: 文章分={article_score}, 列表分={list_score} → {'文章页' if is_article else '列表页'}")
                
                return is_article
                
        except Exception as e:
            print(f"      ⚠️ 页面类型判断异常: {e}")
            return True  # 出错时默认认为是文章页，尝试聚合
    
    def _normalize_url_encoding(self, url: str) -> str:
        """
        🔥 通用URL规范化（防止编码导致的404）
        
        清理URL中可能导致404的编码问题：
        - 移除末尾的编码中文或特殊字符（保留文章ID）
        - 规范化URL编码格式
        
        Args:
            url: 原始URL
            
        Returns:
            str: 规范化后的URL
        """
        import re
        from urllib.parse import urlparse, urlunparse
        
        parsed = urlparse(url)
        path = parsed.path
        
        # 通用清理模式：保留ID，清除后面的编码内容
        patterns = [
            (r'(/article/id/\d+)/[^/]*$', r'\1/'),
            (r'(/article/\d+)/[^/]*$', r'\1/'),
            (r'(/news/\d+)/[^/]*$', r'\1/'),
            (r'(/post/\d+)/[^/]*$', r'\1/'),
            (r'(/\w+/article/\d+)/[^/]*$', r'\1/'),
            (r'(/\w+/\w+/article/\d+)/[^/]*$', r'\1/'),
        ]
        
        for pattern, replacement in patterns:
            new_path = re.sub(pattern, replacement, path)
            if new_path != path:
                path = new_path
                break
        
        return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, parsed.fragment))
    
    def _validate_url_before_crawl(self, url: str) -> bool:
        """
        🔥 聚合前验证URL（防止404）
        
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
            proxy_enabled = bool(self.crawl_options.get('proxy_enabled'))
            proxies = config.get_proxies(enabled=proxy_enabled) if proxy_enabled else {}
            response = _requests_head_with_proxy_intent(url, timeout=5, allow_redirects=True, proxies=proxies)
            
            if response.status_code == 404:
                print(f"      ⚠️ URL返回404，跳过: {url[:80]}...")
                return False
            
            return True
        except Exception as e:
            # 验证失败时仍然尝试聚合（网络问题不应阻止聚合）
            return True
    
    def _smart_retry_url(self, url: str) -> Optional[str]:
        """
        🔥 通用智能重试URL（404时尝试简化版本）
        
        当URL访问失败时，尝试简化URL重试
        
        Args:
            url: 失败的URL
            
        Returns:
            Optional[str]: 有效的URL或None
        """
        import re
        import requests
        from urllib.parse import urlparse
        
        parsed = urlparse(url)
        domain = f"{parsed.scheme}://{parsed.netloc}"
        
        # 提取文章ID
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
        
        # 通用简化URL格式
        simplified_urls = [
            f"{domain}/article/id/{article_id}/",
            f"{domain}/article/{article_id}/",
            f"{domain}/news/{article_id}/",
            f"{domain}/post/{article_id}/",
        ]
        
        # 尝试每个URL
        for test_url in simplified_urls:
            try:
                proxy_enabled = bool(self.crawl_options.get('proxy_enabled'))
                proxies = config.get_proxies(enabled=proxy_enabled) if proxy_enabled else {}
                response = _requests_head_with_proxy_intent(test_url, timeout=5, allow_redirects=True, proxies=proxies)
                if 200 <= response.status_code < 400:
                    print(f"      ✅ 智能重试成功: {test_url}")
                    return test_url
            except:
                continue
        
        return None
    
    def _quick_get_publish_date(self, url: str) -> str:
        """
        🚀 快速获取文章发布日期（不聚合完整内容）
        
        使用轻量级方式获取页面，只提取日期信息
        速度比完整聚合快很多，用于日期预检
        
        Args:
            url: 文章URL
            
        Returns:
            str: 发布日期字符串，获取失败返回None
        """
        import re
        import requests
        import config
        
        try:
            # 1. 发送轻量级请求（只获取部分内容）
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html',
                'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            }
            
            proxy_enabled = bool(self.crawl_options.get('proxy_enabled'))
            proxies = config.get_proxies(enabled=proxy_enabled) if proxy_enabled else {}
            
            # 使用 stream=True 只获取部分内容，加快速度
            response = _requests_get_with_proxy_intent(url, headers=headers, timeout=10, stream=True, proxies=proxies)
            
            if response.status_code != 200:
                return None
            
            # 只读取前 50KB 的内容（日期通常在页面头部）
            content = ''
            for chunk in response.iter_content(chunk_size=1024, decode_unicode=True):
                if chunk:
                    content += chunk if isinstance(chunk, str) else chunk.decode('utf-8', errors='ignore')
                if len(content) > 50000:  # 50KB 足够找到日期了
                    break
            
            # 2. 在 HTML 中搜索日期
            # 常见的日期元素
            date_patterns = [
                # HTML5 time 元素
                r'<time[^>]*datetime=["\']([^"\']+)["\']',
                # meta 标签中的日期
                r'<meta[^>]*(?:property|name)=["\'](?:article:published_time|datePublished|pubdate|publish_date|date)["\'][^>]*content=["\']([^"\']+)["\']',
                r'<meta[^>]*content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:article:published_time|datePublished|pubdate|publish_date|date)["\']',
                # JSON-LD 中的日期
                r'"datePublished"\s*:\s*"([^"]+)"',
                r'"publishedTime"\s*:\s*"([^"]+)"',
                # 分离式中文日期格式: "出版日: 2025年12月" + "3日"
                r'出版日[：:]\s*(\d{4})年(\d{1,2})月[^>]*?(\d{1,2})日',
                # 分离式年月/日下拉框选中的日期值
                r'<option[^>]*selected[^>]*>(\d{4})年(\d{1,2})月</option>',
                r'<option[^>]*selected[^>]*>(\d{1,2})日',
                # 常见的日期 class
                r'class=["\'][^"\']*(?:date|time|publish)[^"\']*["\'][^>]*>([^<]+)<',
                # 中文日期格式
                r'(\d{4}年\d{1,2}月\d{1,2}日)',
                r'(\d{4}[/-]\d{1,2}[/-]\d{1,2}\s+\d{1,2}:\d{2})',
                r'(\d{4}[/-]\d{1,2}[/-]\d{1,2})',
            ]
            
            # 处理分开的年月和日
            year_month_match = re.search(r'(\d{4})年(\d{1,2})月', content)
            day_match = re.search(r'>(\d{1,2})日[,，]?\s*星期', content)
            if year_month_match and day_match:
                year = year_month_match.group(1)
                month = year_month_match.group(2)
                day = day_match.group(1)
                return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
            
            for pattern in date_patterns:
                match = re.search(pattern, content, re.IGNORECASE)
                if match:
                    date_str = match.group(1).strip()
                    # 验证是否是有效的日期字符串
                    if re.search(r'\d{4}', date_str):  # 至少包含年份
                        return date_str
            
            return None
            
        except Exception as e:
            # 静默失败，不影响主流程
            return None
    
    def _quick_get_publish_date_playwright(self, url: str) -> str:
        """
        🚀 使用 Playwright 快速获取发布日期（针对 JavaScript 动态页面）
        
        比完整聚合快，只获取日期信息
        """
        try:
            from playwright.sync_api import sync_playwright
            import config
            import re
            
            from crawler_resource_manager import sync_playwright_slot

            with sync_playwright_slot(f"quick-date:{url}"):
                with sync_playwright() as p:
                    proxy_config = config.get_playwright_proxy(enabled=self.crawl_options.get('proxy_enabled'))
                    browser = p.chromium.launch(headless=True, proxy=proxy_config)
                    try:
                        context = browser.new_context(
                            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                        )
                        install_sync_playwright_url_guard(context)
                        page = context.new_page()

                        # 只加载 DOM，不等待所有资源
                        page.goto(url, wait_until='domcontentloaded', timeout=15000)

                        # 获取页面内容
                        content = page.content()
                    finally:
                        browser.close()
                
                # 搜索日期
                # 1. 分离式中文日期控件
                year_month_match = re.search(r'(\d{4})年(\d{1,2})月', content)
                day_match = re.search(r'>(\d{1,2})日[,，]?\s*星期', content)
                if year_month_match and day_match:
                    year = year_month_match.group(1)
                    month = year_month_match.group(2)
                    day = day_match.group(1)
                    return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
                
                # 2. 其他常见格式
                date_patterns = [
                    r'"datePublished"\s*:\s*"([^"]+)"',
                    r'(\d{4}年\d{1,2}月\d{1,2}日)',
                    r'(\d{4}[/-]\d{1,2}[/-]\d{1,2})',
                ]
                
                for pattern in date_patterns:
                    match = re.search(pattern, content)
                    if match:
                        return match.group(1)
                        
        except Exception as e:
            pass
        
        return None
    
    def _parse_date_string(self, date_str: str) -> datetime:
        """
        🔥 超级日期解析器 - 支持各种常见日期格式
        
        支持的格式：
        1. 标准格式: 2025-12-03, 2025/12/03, 2025.12.03
        2. 带时间: 2025-12-03 12:48, 2025/12/03 12:48:00
        3. 中文格式: 2025年12月3日, 2025年12月03日
        4. ISO格式: 2025-12-03T12:48:00, 2025-12-03T12:48:00Z
        5. 英文格式: Dec 3, 2025, December 3, 2025, 3 Dec 2025
        6. 香港英式: 03/12/2025 (日/月/年)
        7. 相对时间: 刚刚, 1分钟前, 2小时前, 3天前, 1小時前, 2日前
        8. 简写格式: 12月3日, 12-03
        
        Args:
            date_str: 日期字符串
            
        Returns:
            datetime: 解析后的日期时间对象，解析失败返回None
        """
        import re
        from datetime import datetime, timedelta
        
        if not date_str:
            return None
        
        date_str = date_str.strip()
        now = get_china_time()
        
        try:
            # ===== 1. 相对时间处理 =====
            # 中文相对时间
            relative_patterns = [
                # 刚刚/剛剛
                (r'^(刚刚|剛剛|just now|just)$', lambda m: now),
                # X秒前
                (r'^(\d+)\s*(秒|秒钟|秒鐘|seconds?|secs?)\s*(前|ago)?$', lambda m: now - timedelta(seconds=int(m.group(1)))),
                # X分钟前
                (r'^(\d+)\s*(分钟|分鐘|分|minutes?|mins?)\s*(前|ago)?$', lambda m: now - timedelta(minutes=int(m.group(1)))),
                # X小时前
                (r'^(\d+)\s*(小时|小時|时|時|hours?|hrs?)\s*(前|ago)?$', lambda m: now - timedelta(hours=int(m.group(1)))),
                # X天前
                (r'^(\d+)\s*(天|日|days?)\s*(前|ago)?$', lambda m: now - timedelta(days=int(m.group(1)))),
                # X周前
                (r'^(\d+)\s*(周|週|weeks?)\s*(前|ago)?$', lambda m: now - timedelta(weeks=int(m.group(1)))),
                # 今天/今日
                (r'^(今天|今日|today)$', lambda m: now),
                # 昨天/昨日
                (r'^(昨天|昨日|yesterday)$', lambda m: now - timedelta(days=1)),
                # 前天
                (r'^(前天|前日)$', lambda m: now - timedelta(days=2)),
            ]
            
            for pattern, calc_func in relative_patterns:
                match = re.match(pattern, date_str, re.IGNORECASE)
                if match:
                    return calc_func(match)
            
            # ===== 2. 清理日期字符串 =====
            date_clean = date_str
            
            # 移除时间部分（保留日期）
            # 格式: "2025-12-03 12:48:00" -> "2025-12-03"
            if 'T' in date_clean:
                date_clean = date_clean.split('T')[0]
            elif ' ' in date_clean and re.search(r'\d{1,2}:\d{2}', date_clean):
                date_clean = date_clean.split(' ')[0]
            
            # ===== 3. 中文日期格式 =====
            # 2025年12月3日 或 2025年12月03日
            cn_full = re.match(r'(\d{4})年(\d{1,2})月(\d{1,2})日?', date_clean)
            if cn_full:
                year, month, day = cn_full.groups()
                return datetime(int(year), int(month), int(day))
            
            # 12月3日（无年份，使用今年）
            cn_short = re.match(r'^(\d{1,2})月(\d{1,2})日?$', date_clean)
            if cn_short:
                month, day = cn_short.groups()
                year = now.year
                # 如果月份大于当前月份，可能是去年的
                if int(month) > now.month:
                    year -= 1
                return datetime(year, int(month), int(day))
            
            # ===== 4. 英文日期格式 =====
            month_names = {
                'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
                'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6,
                'jul': 7, 'july': 7, 'aug': 8, 'august': 8, 'sep': 9, 'september': 9,
                'oct': 10, 'october': 10, 'nov': 11, 'november': 11, 'dec': 12, 'december': 12
            }
            
            # Dec 3, 2025 或 December 3, 2025
            en_mdy = re.match(r'^([a-zA-Z]+)\s+(\d{1,2}),?\s+(\d{4})$', date_clean)
            if en_mdy:
                month_str, day, year = en_mdy.groups()
                month = month_names.get(month_str.lower())
                if month:
                    return datetime(int(year), month, int(day))
            
            # 3 Dec 2025 或 03 December 2025
            en_dmy = re.match(r'^(\d{1,2})\s+([a-zA-Z]+)\s+(\d{4})$', date_clean)
            if en_dmy:
                day, month_str, year = en_dmy.groups()
                month = month_names.get(month_str.lower())
                if month:
                    return datetime(int(year), month, int(day))
            
            # ===== 5. 数字日期格式 =====
            # 尝试多种格式
            formats = [
                '%Y-%m-%d',      # 2025-12-03
                '%Y/%m/%d',      # 2025/12/03
                '%Y.%m.%d',      # 2025.12.03
                '%d/%m/%Y',      # 03/12/2025 (香港英式: 日/月/年)
                '%m/%d/%Y',      # 12/03/2025 (美式: 月/日/年)
                '%d-%m-%Y',      # 03-12-2025
                '%d.%m.%Y',      # 03.12.2025
                '%Y%m%d',        # 20251203
            ]
            
            for fmt in formats:
                try:
                    return datetime.strptime(date_clean[:10], fmt)
                except:
                    continue
            
            # ===== 6. 简写格式 =====
            # 12-03 或 12/03（无年份）
            short_date = re.match(r'^(\d{1,2})[-/](\d{1,2})$', date_clean)
            if short_date:
                month, day = short_date.groups()
                year = now.year
                if int(month) > now.month:
                    year -= 1
                return datetime(year, int(month), int(day))
            
        except Exception as e:
            pass
        
        return None
    
    def _filter_existing_articles(self, links: List[Dict]) -> Tuple[List[Dict], int]:
        """
        过滤掉本地数据库中已存在的文章
        
        检查方式（按优先级）：
        1. URL完全匹配
        2. 标题完全匹配
        3. 标题相似度匹配（>90%）
        
        Args:
            links: 待检查的链接列表
            
        Returns:
            Tuple[List[Dict], int]: (过滤后的链接列表, 重复数量)
        """
        if not links:
            return links, 0
        
        new_links = []
        duplicate_count = 0
        
        # 获取本地所有已存在的文章URL和标题
        existing_urls = set()
        existing_titles = set()

        try:
            from supplemental_link_discovery import canonicalize_candidate_url
        except Exception:
            canonicalize_candidate_url = None

        def _url_keys(raw_url: str) -> set:
            keys = set()
            if not raw_url:
                return keys
            normalized = str(raw_url).strip().lower().rstrip('/')
            if normalized:
                keys.add(normalized)
            if canonicalize_candidate_url:
                canonical = canonicalize_candidate_url(str(raw_url).strip())
                if canonical:
                    keys.add(canonical.lower().rstrip('/'))
            return keys
        
        try:
            # 确保数据库连接可用
            self.db._ensure_connection()
            
            # 获取所有已存在的URL
            cursor = self.db.connection.cursor()
            cursor.execute("SELECT url, title FROM articles")
            for row in cursor.fetchall():
                if row[0]:
                    existing_urls.update(_url_keys(row[0]))
                if row[1]:
                    existing_titles.add(row[1].strip())
            cursor.close()
        except Exception as e:
            print(f"   ⚠️ 获取本地文章失败: {e}")
            return links, 0
        
        print(f"   📚 本地已有 {len(existing_urls)} 篇文章")
        
        for link in links:
            url_keys = _url_keys(link.get('url', ''))
            title = link.get('title', '').strip()
            
            is_duplicate = False
            
            # 1. URL匹配
            if url_keys and url_keys.intersection(existing_urls):
                is_duplicate = True
                print(f"   🔄 URL重复: {title[:30]}...")
            
            # 2. 标题完全匹配
            elif title and title in existing_titles:
                is_duplicate = True
                print(f"   🔄 标题重复: {title[:30]}...")
            
            # 3. 标题相似度匹配（简单实现）
            elif title:
                for existing_title in existing_titles:
                    if self._title_similarity(title, existing_title) > 0.9:
                        is_duplicate = True
                        print(f"   🔄 标题相似: {title[:30]}... ≈ {existing_title[:30]}...")
                        break
            
            if is_duplicate:
                duplicate_count += 1
            else:
                new_links.append(link)
        
        return new_links, duplicate_count
    
    def _title_similarity(self, title1: str, title2: str) -> float:
        """
        计算两个标题的相似度（简单实现）
        
        Returns:
            float: 相似度 0-1
        """
        if not title1 or not title2:
            return 0.0
        
        # 简单的字符重叠比例
        t1 = set(title1)
        t2 = set(title2)
        
        intersection = len(t1 & t2)
        union = len(t1 | t2)
        
        if union == 0:
            return 0.0
        
        return intersection / union
    
    def _crawl_article_details(
        self, 
        links: List[Dict],
        limit: int,
        source_url: str,
        keywords: str = '',
        kb_id: str = None,
        days_limit: int = 7,
        start_date: str = None,
        end_date: str = None,
        should_stop=None,
        crawl_options: Dict = None,
        task_id: str = None
    ) -> Dict:
        """
        聚合文章详情的通用方法（智能验证版）
        
        流程：
        1. 提取链接（已通过宽松过滤）
        2. 聚合每个链接的实际内容
        3. 评估内容质量
        4. 只保存质量达标的文章
        
        Args:
            links: 文章链接列表
            limit: 仅用于显示（不限制实际聚合数量）
            source_url: 来源URL
            keywords: 关键词过滤
            kb_id: RAGFlow知识库ID
            days_limit: 日期限制（只聚合最近N天的文章），默认7天，0表示不限制
            start_date: 指定起始日期（YYYY-MM-DD，可选，优先于days_limit）
            end_date: 指定结束日期（YYYY-MM-DD，可选）
            
        Returns:
            Dict: 聚合结果
        """
        crawl_options = normalize_crawl_options(crawl_options)
        self.crawl_options = crawl_options
        self._sync_validator_proxy()
        total_links = len(links)
        candidate_total_before_db = total_links
        audit_started_at = get_china_time().isoformat()
        audit_items = {}

        def _audit_key(link: Dict) -> str:
            return (link.get('url') or '').strip().lower().rstrip('/')

        def _mark_audit(link: Dict, status: str, **extra):
            key = _audit_key(link)
            if not key:
                return
            item = audit_items.setdefault(key, {
                'url': link.get('url', ''),
                'title': link.get('title') or link.get('text') or '',
                'source_method': link.get('source_method') or link.get('source_type') or 'unknown',
                'discovery_source_url': link.get('discovery_source_url'),
                'content_hint_source': link.get('content_hint_source'),
                'candidate_publish_date': link.get('publish_date'),
                'status': 'candidate',
                'reason': None,
            })
            item['status'] = status
            item['updated_at'] = get_china_time().isoformat()
            for field, value in extra.items():
                if value is not None:
                    item[field] = value

        for audit_link in links:
            _mark_audit(audit_link, 'candidate')
        
        # 🔥🔥🔥 优化：先和本地数据库对比去重，跳过已存在的文章 🔥🔥🔥
        print(f"\n{'='*70}")
        print(f"【阶段1.5】本地数据库去重检查")
        print(f"{'='*70}")
        
        links, duplicate_count = self._filter_existing_articles(links)

        retained_keys = {_audit_key(link) for link in links}
        for key, item in audit_items.items():
            if key not in retained_keys:
                item['status'] = 'duplicate'
                item['reason'] = 'already_exists_or_similar_title'
                item['updated_at'] = get_china_time().isoformat()
        
        if duplicate_count > 0:
            print(f"   ✅ 跳过 {duplicate_count} 篇已存在的文章")
            print(f"   📊 剩余 {len(links)} 篇新文章待聚合")
        else:
            print(f"   📊 全部 {len(links)} 篇都是新文章")
        
        total_links = len(links)  # 更新为去重后的数量
        
        # 🔥 使用 KeywordFilter 支持简繁体转换
        keyword_filter_detail = KeywordFilter(keywords or '')
        keyword_list = keyword_filter_detail.keywords  # 兼容旧代码
        
        # 计算日期限制的截止日期。显式起止日期优先，其次使用最近N天。
        date_cutoff = None
        date_upper = None
        date_window_label = "不限制"
        if start_date:
            parsed_start = self._parse_date_string(str(start_date))
            if parsed_start:
                date_cutoff = parsed_start
        if end_date:
            parsed_end = self._parse_date_string(str(end_date))
            if parsed_end:
                date_upper = parsed_end
        if not date_cutoff and days_limit and days_limit > 0:
            date_cutoff = get_china_time() - timedelta(days=days_limit)
        if date_cutoff or date_upper:
            if start_date or end_date:
                date_window_label = f"{date_cutoff.strftime('%Y-%m-%d') if date_cutoff else '不限'} ~ {date_upper.strftime('%Y-%m-%d') if date_upper else '不限'}"
            elif days_limit and days_limit > 0:
                date_window_label = f"{date_cutoff.strftime('%Y-%m-%d')} 之后（最近{days_limit}天）"
        prefilter_candidate_dates = bool(crawl_options.get('candidate_date_prefilter', False))

        def _date_out_of_range_reason(date_value):
            if not date_value or not (date_cutoff or date_upper):
                return None, None
            parsed_date = self._parse_date_string(str(date_value))
            if not parsed_date:
                return None, None
            publish_day = parsed_date.date()
            cutoff_date = date_cutoff.date() if date_cutoff and hasattr(date_cutoff, 'date') else date_cutoff
            upper_date = date_upper.date() if date_upper and hasattr(date_upper, 'date') else date_upper
            normalized = parsed_date.strftime('%Y-%m-%d')
            if cutoff_date and publish_day < cutoff_date:
                return normalized, f"{normalized} < {date_cutoff.strftime('%Y-%m-%d')}"
            if upper_date and publish_day > upper_date:
                return normalized, f"{normalized} > {date_upper.strftime('%Y-%m-%d')}"
            return normalized, None
        
        print(f"\n{'='*70}")
        print(f"【阶段2】聚合文章详细内容（共{total_links}个候选链接）")
        print(f"{'='*70}")
        print(f"🔍 策略：智能单一聚合（准确性优先）")
        if self.enable_smart_validation:
            print(f"📊 使用：智能验证器（全部用Playwright，保证质量）")
        else:
            print(f"📊 使用：智能提取器（Playwright/多解析器），不调用 Firecrawl")
        if keyword_list:
            print(f"🎯 关键词过滤：标题或内容包含 {', '.join(keyword_list)}")
        if date_cutoff or date_upper:
            print(f"📅 日期限制：只保存 {date_window_label} 的文章")
        print(f"⚡ 优化：单次聚合 + 关键词过滤 = 速度快且准确")
        
        articles = []
        success_count = 0
        skipped_count = 0  # 质量不达标被跳过的
        failed_count = 0   # 聚合失败的
        date_skipped_count = 0  # 因日期限制跳过的
        
        # 🔥 RAGFlow上传相关变量（改为入库一篇上传一篇）
        ragflow_stats = {
            'uploaded': 0,
            'skipped_existing': 0,
            'skipped_empty': 0,
            'failed': 0,
            'disabled': False,
            'errors': []
        }
        
        # 🔍 调试：打印kb_id
        if kb_id:
            print(f"🔍 已配置知识库ID: {kb_id}，将启用实时上传（入库一篇上传一篇）")
        else:
            print(f"⚠️ 未配置知识库ID，不会上传到RAGFlow")
        
        # 🔥🔥🔥 新策略：边爬边存，100%准确日期，不做快速检查 🔥🔥🔥
        print(f"\n{'='*70}")
        print(f"【阶段2】边爬边存策略（共{total_links}个链接）")
        print(f"{'='*70}")
        print(f"💡 策略说明：")
        print(f"   1. 逐个打开文章详情页")
        print(f"   2. 尽量提取发布日期")
        print(f"   3. 只有明确日期超限才跳过")
        print(f"   4. 日期未知的文章继续入库并进入统计，避免静默遗漏")
        print(f"   5. 符合条件立即入库和上传")
        if date_cutoff or date_upper:
            print(f"   📅 日期限制: 只保存 {date_window_label} 的文章")
            if prefilter_candidate_dates:
                print(f"   ⚡ 候选日期预过滤: 已开启（列表页日期明确超限则不打开详情页）")
            else:
                print(f"   ⚡ 候选日期预过滤: 已关闭")
        
        consecutive_old_count = 0  # 连续超出日期的计数
        # 不再因为连续旧文章提前停止；很多栏目页不是严格按日期排序，提前停会漏文章。
        max_consecutive_old = None
        date_unknown_count = 0
        date_parse_failed_count = 0
        candidate_date_prefilter_count = 0
        keyword_skipped_count = 0
        quality_skipped_count = 0
        too_short_skipped_count = 0
        integrity_failed_count = 0
        db_failed_count = 0
        extraction_retry_count = 0
        inline_content_count = 0
        max_extract_attempts = int(crawl_options.get('detail_max_retries', 2))
        detail_wait_seconds = max(1, int((crawl_options.get('wait_for_ms') or 8000) / 1000))
        detail_timeout_seconds = max(30, detail_wait_seconds + 20)
        detail_proxies = None
        try:
            import config
            proxy_enabled = bool(crawl_options.get('proxy_enabled'))
            detail_proxies = config.get_proxies(enabled=proxy_enabled) if proxy_enabled else {}
        except Exception as proxy_error:
            print(f"   ⚠️ 详情页代理配置读取失败，继续直连: {proxy_error}")
        if detail_proxies:
            print("   🔧 详情页正文抽取将使用代理")

        def _stop_requested():
            try:
                return bool(should_stop and should_stop())
            except Exception:
                return False

        stopped_count = 0
        
        for i, link in enumerate(links, 1):
            if _stop_requested():
                stopped_count += total_links - i + 1
                print(f"\n   Stop requested, stopping before article {i}/{total_links}")
                for remaining_link in links[i - 1:]:
                    _mark_audit(remaining_link, 'stopped', reason='stop_requested')
                break

            print(f"\n[{i}/{total_links}] {link.get('title', 'N/A')[:60]}...")
            print(f"   URL: {link['url'][:80]}...")
            _mark_audit(link, 'processing')

            if prefilter_candidate_dates and link.get('publish_date'):
                candidate_publish_date, candidate_out_reason = _date_out_of_range_reason(link.get('publish_date'))
                if candidate_out_reason:
                    print(f"   ⏭️ 跳过: 候选日期已超出限制 ({candidate_out_reason})")
                    date_skipped_count += 1
                    candidate_date_prefilter_count += 1
                    _mark_audit(
                        link,
                        'date_out_of_range_candidate',
                        publish_date=candidate_publish_date,
                        reason=candidate_out_reason,
                        date_status='candidate_known',
                    )
                    time.sleep(0.1)
                    continue
            
            # 🔥 步骤1: 提取文章内容和日期
            print(f"   🎯 提取文章内容...")
            extract_result = None
            last_extract_error = ''
            inline_content = self._clean_inline_candidate_content(link.get('content_hint') or '')

            if inline_content:
                inline_content_count += 1
                inline_title = link.get('title', '无标题') or self._extract_title_from_content(inline_content) or '无标题'
                inline_score = self._evaluate_content_quality({
                    'success': True,
                    'title': inline_title,
                    'content': inline_content,
                })
                if len(inline_content) >= 300:
                    inline_score = max(inline_score, 70)
                elif len(inline_content) >= 120:
                    inline_score = max(inline_score, 60)

                extract_result = {
                    'success': True,
                    'title': inline_title,
                    'content': inline_content,
                    'publish_date': link.get('publish_date'),
                    'authors': link.get('authors', []),
                    'url': link['url'],
                    'method': link.get('source_method') or 'network_json_inline',
                    'score': min(max(inline_score, 0), 100),
                    'content_length': len(inline_content),
                }
                print(f"   ✅ 使用接口JSON内联正文: {len(inline_content)} 字")
            else:
                for attempt in range(1, max_extract_attempts + 1):
                    try:
                        from smart_article_extractor import extract_article_content_from_url

                        if attempt > 1:
                            extraction_retry_count += 1
                            print(f"   🔁 第{attempt}次重试提取...")

                        extract_result = extract_article_content_from_url(
                            link['url'],
                            proxies=detail_proxies,
                            skip_db_check=True,
                            wait_time=detail_wait_seconds,
                            timeout=detail_timeout_seconds,
                        )

                        if extract_result.get('success'):
                            break

                        last_extract_error = extract_result.get('error', '未知')
                        print(f"   ⚠️ 第{attempt}次提取失败: {last_extract_error}")
                    except Exception as e:
                        last_extract_error = str(e)
                        extract_result = {
                            'success': False,
                            'error': last_extract_error,
                            'method': 'exception'
                        }
                        print(f"   ⚠️ 第{attempt}次提取异常: {e}")

                    if attempt < max_extract_attempts:
                        time.sleep(1.0 * attempt)

            if not extract_result or not extract_result.get('success'):
                print(f"   ❌ 提取失败: {last_extract_error or '未知'}")
                failed_count += 1
                _mark_audit(
                    link,
                    'extract_failed',
                    reason=last_extract_error or 'unknown',
                    extraction_method=(extract_result or {}).get('method'),
                    extraction_attempts=max_extract_attempts,
                )
                time.sleep(0.5)
                continue

            # 提取的数据
            title = extract_result.get('title', link.get('title', '无标题'))
            content = extract_result.get('content', '')
            publish_date = extract_result.get('publish_date') or link.get('publish_date')
            date_meta = {}
            if not publish_date:
                # T2.3 分层日期提取兜底：JSON-LD → meta → 微数据 → <time> → URL → 相对时间
                try:
                    from crawl_date_extract import extract_layered_date
                    _raw_html = str(extract_result.get('html') or extract_result.get('raw_html') or '')
                    _layered = extract_layered_date(_raw_html, link.get('url') or '') if _raw_html else {}
                    if _layered.get('date'):
                        publish_date = _layered['date']
                        date_meta = {
                            'published_precision': _layered.get('precision') or 'day',
                            'published_time_source': f"layered:{_layered.get('source') or 'unknown'}",
                        }
                        print(f"   📅 发布日期（分层提取 {date_meta['published_time_source']}）: {publish_date}")
                except Exception as _date_exc:
                    print(f"   ⚠️ 分层日期提取失败（跳过）: {_date_exc}")
            if publish_date:
                try:
                    parsed_publish_date = self._parse_date_string(str(publish_date))
                    if parsed_publish_date:
                        publish_date = parsed_publish_date.strftime('%Y-%m-%d')
                except Exception:
                    pass

            print(f"   ✅ 内容提取成功: {len(content)} 字")

            integrity_check = self._assess_extracted_content_integrity(content, title, extract_result)
            if not integrity_check.get('ok'):
                reason = integrity_check.get('reason') or 'unknown_integrity_issue'
                print(f"   ❌ 正文完整性校验失败: {reason} ({content[:30]}...)")
                integrity_failed_count += 1
                _mark_audit(
                    link,
                    'content_integrity_failed',
                    reason=reason,
                    integrity_issues=integrity_check.get('issues', []),
                    content_length=len(content),
                    publish_date=publish_date,
                    extracted_title=title,
                    extraction_method=extract_result.get('method', 'unknown'),
                )
                time.sleep(0.5)
                continue
            
            # 🔥 步骤2: 检查日期。日期提取不到时继续处理，避免漏掉真实文章。
            if not publish_date:
                date_unknown_count += 1
                print(f"   📅 发布日期: 未识别，继续保存为日期未知")
                _mark_audit(link, 'processing', date_status='unknown')
            else:
                print(f"   📅 发布日期: {publish_date}")
                _mark_audit(link, 'processing', publish_date=publish_date, date_status='known')
            
            # 🔥 步骤3: 检查日期是否在范围内
            if (date_cutoff or date_upper) and publish_date:
                try:
                    normalized_publish_date, out_of_range_reason = _date_out_of_range_reason(publish_date)
                    if normalized_publish_date:
                        publish_date = normalized_publish_date

                    if out_of_range_reason:
                        print(f"   ⏭️ 跳过: 日期超出限制 ({out_of_range_reason})")
                        date_skipped_count += 1
                        consecutive_old_count += 1
                        _mark_audit(
                            link,
                            'date_out_of_range',
                            publish_date=publish_date,
                            reason=out_of_range_reason,
                        )
                        time.sleep(0.5)
                        continue
                    else:
                        # 日期符合要求，重置计数
                        consecutive_old_count = 0
                except Exception as e:
                    print(f"   ⚠️ 日期解析失败: {e}，继续保存为日期未知")
                    publish_date = None
                    date_unknown_count += 1
                    date_parse_failed_count += 1
                    _mark_audit(link, 'processing', date_status='parse_failed', reason=str(e))
            
            # 🔥 步骤4: 检查内容质量
            if len(content) < 50:
                print(f"   ⏭️ 跳过: 内容太短 ({len(content)} 字)")
                skipped_count += 1
                too_short_skipped_count += 1
                _mark_audit(
                    link,
                    'too_short_skipped',
                    reason=f"content_length={len(content)}",
                    content_length=len(content),
                    publish_date=publish_date,
                    extracted_title=title,
                )
                time.sleep(0.5)
                continue
            
            # 构建文章数据
            best_result = {
                'success': True,
                'url': link['url'],
                'title': title,
                'content': content,
                'publish_date': publish_date,
                **date_meta,
                'authors': extract_result.get('authors', []),
                'extraction_method': extract_result.get('method', 'playwright_smart'),
                'quality_score': extract_result.get('score', 80),
                'crawled_at': get_china_time().isoformat()
            }
            
            # 🔥 只用质量评分判断，不使用硬性字数限制
            content_length = len(best_result.get('content', ''))
            best_score = best_result.get('quality_score', 80)
            method_name = best_result.get('extraction_method', 'playwright_smart')
            print(f"   📊 内容长度: {content_length}字，质量分数: {best_score}")
            
            if best_score < 50:
                print(f"   ⏭️  质量不达标 (分数: {best_score})，跳过")
                skipped_count += 1
                quality_skipped_count += 1
                _mark_audit(
                    link,
                    'quality_skipped',
                    reason=f"quality_score={best_score}",
                    quality_score=best_score,
                    content_length=content_length,
                    publish_date=publish_date,
                    extracted_title=best_result.get('title'),
                    extraction_method=method_name,
                )
                time.sleep(0.3)
                continue
            
            print(f"   🏆 选择: {method_name} (质量: {best_score})")
            article_result = best_result
            quality_score = best_score
            
            # 🎯 关键词内容过滤（入库前最终检查）
            if keyword_list:
                match_result = keyword_filter_detail.get_matched_keywords_by_location(
                    article_result.get('title', ''),
                    article_result.get('content', '')
                )
                title_matches = match_result.get('title_keywords', [])
                content_matches = match_result.get('content_keywords', [])
                
                # 标题或内容包含关键词即可
                has_keyword = bool(title_matches or content_matches)
                
                if not has_keyword:
                    print(f"   ⏭️  关键词过滤: 标题和内容都不包含关键词，跳过入库")
                    print(f"      标题: {article_result.get('title', '')[:50]}...")
                    print(f"      期望关键词: {', '.join(keyword_list)}")
                    skipped_count += 1
                    keyword_skipped_count += 1
                    _mark_audit(
                        link,
                        'keyword_skipped',
                        reason=f"missing_keywords={','.join(keyword_list)}",
                        quality_score=quality_score,
                        content_length=content_length,
                        publish_date=publish_date,
                        extracted_title=article_result.get('title'),
                        extraction_method=method_name,
                    )
                    time.sleep(0.3)
                    continue
                else:
                    # 显示详细匹配信息
                    match_info = []
                    if title_matches:
                        match_info.append(f"标题({','.join(title_matches)})")
                    if content_matches:
                        match_info.append(f"内容({','.join(content_matches)})")
                    print(f"   ✅ 关键词匹配: {' + '.join(match_info)}")
                    article_result['matched_keywords'] = match_result.get('matched_keywords_str', '')
            
            # 📅 日期已在聚合前检查过，这里不再重复检查（提高效率）
            
            # 2. 保存到数据库
            article_id = self.db.insert_article(article_result)
            
            if article_id:
                if task_id:
                    try:
                        self.db.link_article_to_task(article_id, task_id)
                    except Exception as link_error:
                        print(f"   ⚠️ 文章任务关联失败: {link_error}")
                article_entry = {
                    'link_info': link,
                    'content': article_result,
                    'db_id': article_id,
                    'quality_score': quality_score
                }
                articles.append(article_entry)
                success_count += 1
                print(f"   💾 已入库 (ID: {article_id}, 质量: {quality_score})")
                _mark_audit(
                    link,
                    'saved',
                    db_id=article_id,
                    quality_score=quality_score,
                    content_length=content_length,
                    publish_date=publish_date,
                    extracted_title=article_result.get('title'),
                    final_url=article_result.get('url'),
                    extraction_method=method_name,
                )
                
                # 🔥 入库一篇就上传一篇到RAGFlow
                if kb_id and not _stop_requested():
                    try:
                        # 立即上传这一篇文章
                        upload_result = self._upload_single_article_to_ragflow(article_entry, kb_id)
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
                        print(f"   ⚠️ 上传失败: {upload_error}")
                        ragflow_stats['failed'] += 1
                        ragflow_stats['errors'].append({
                            'title': article_result.get('title', ''),
                            'url': article_result.get('url', ''),
                            'error': str(upload_error)
                        })
                        # 上传失败不影响聚合，继续处理
                elif kb_id:
                    ragflow_stats['stopped_before_upload'] = ragflow_stats.get('stopped_before_upload', 0) + 1
            else:
                print(f"   ⚠️ 内容质量达标但入库失败")
                skipped_count += 1
                db_failed_count += 1
                _mark_audit(
                    link,
                    'db_failed',
                    reason='insert_article_returned_none',
                    quality_score=quality_score,
                    content_length=content_length,
                    publish_date=publish_date,
                    extracted_title=article_result.get('title'),
                    extraction_method=method_name,
                )
            
            if _stop_requested():
                stopped_count += max(0, total_links - i)
                print(f"\n   Stop requested, stopping after article {i}/{total_links}")
                for remaining_link in links[i:]:
                    _mark_audit(remaining_link, 'stopped', reason='stop_requested')
                break

            # 避免请求过快
            time.sleep(1)
        
        # 🔥 显示上传统计
        if kb_id:
            print(
                f"\n📤 RAGFlow上传统计: 上传 {ragflow_stats['uploaded']} | "
                f"已存在 {ragflow_stats['skipped_existing']} | "
                f"空内容 {ragflow_stats['skipped_empty']} | "
                f"失败 {ragflow_stats['failed']}"
            )
        else:
            print(f"\n⚠️ 未配置知识库ID，跳过上传")
        
        # 统计结果
        source_site = urlparse(source_url).netloc
        stats = self.db.get_statistics(domain=source_site)
        audit_list = list(audit_items.values())
        audit_status_counts = {}
        for item in audit_list:
            status = item.get('status', 'unknown')
            audit_status_counts[status] = audit_status_counts.get(status, 0) + 1
        audit = {
            'source_url': source_url,
            'started_at': audit_started_at,
            'completed_at': get_china_time().isoformat(),
            'total_candidates_before_db_dedupe': candidate_total_before_db,
            'total_candidates_after_db_dedupe': total_links,
            'status_counts': audit_status_counts,
            'ragflow': ragflow_stats,
            'crawl_options': crawl_options,
            'items': audit_list,
        }
        date_unknown_review_count = date_unknown_count if (date_cutoff or date_upper) else 0
        ragflow_failed_risk = 0
        if kb_id:
            ragflow_failed_risk += int(ragflow_stats.get('failed', 0) or 0)
            ragflow_failed_risk += int(ragflow_stats.get('stopped_before_upload', 0) or 0)
            if ragflow_stats.get('disabled') and success_count > 0:
                ragflow_failed_risk += success_count

        risk_reasons = {
            'extract_failed': failed_count,
            'content_integrity_failed': integrity_failed_count,
            'db_failed': db_failed_count,
            'stopped': stopped_count,
            'quality_skipped': quality_skipped_count,
            'too_short_skipped': too_short_skipped_count,
            'date_unknown_in_window': date_unknown_review_count,
            'ragflow_delivery_risk': ragflow_failed_risk,
        }
        risk_reasons = {key: value for key, value in risk_reasons.items() if value}
        recall_risk_count = (
            failed_count
            + integrity_failed_count
            + db_failed_count
            + stopped_count
            + quality_skipped_count
            + too_short_skipped_count
            + date_unknown_review_count
            + ragflow_failed_risk
        )
        recall_status = 'needs_review' if recall_risk_count > 0 else 'complete'
        recall_health = self._compute_recall_health(
            total_candidates=candidate_total_before_db,
            processed_candidates=total_links,
            success_count=success_count,
            risk_reasons=risk_reasons,
            source_method_counts=self._count_source_methods(links),
            date_window_enabled=bool(date_cutoff or date_upper),
        )
        audit['recall_status'] = recall_status
        audit['recall_risk_count'] = recall_risk_count
        audit['recall_risk_reasons'] = risk_reasons
        audit['recall_health_score'] = recall_health['score']
        audit['recall_health_level'] = recall_health['level']
        audit['recall_health_label'] = recall_health['label']
        audit['recall_health_reasons'] = recall_health['reasons']
        audit['recall_health_metrics'] = recall_health['metrics']
        
        print(f"\n{'='*70}")
        print(f"✅ 智能验证完成!")
        print(f"{'='*70}")
        print(f"📋 处理统计:")
        print(f"   候选链接: {candidate_total_before_db}")
        if candidate_total_before_db != total_links:
            print(f"   去重后待处理: {total_links}")
        if duplicate_count > 0:
            print(f"   🔁 数据库已存在: {duplicate_count}")
        print(f"   ✅ 成功入库: {success_count} ({success_count/total_links*100:.1f}%)" if total_links > 0 else "")
        print(f"   ⏭️  跳过合计: {skipped_count} ({skipped_count/total_links*100:.1f}%)" if total_links > 0 else "")
        if quality_skipped_count > 0:
            print(f"      - 质量不达标: {quality_skipped_count}")
        if too_short_skipped_count > 0:
            print(f"      - 内容太短: {too_short_skipped_count}")
        if keyword_skipped_count > 0:
            print(f"      - 关键词不匹配: {keyword_skipped_count}")
        if db_failed_count > 0:
            print(f"      - 入库失败/重复: {db_failed_count}")
        if integrity_failed_count > 0:
            print(f"      - 正文完整性失败: {integrity_failed_count}")
        if extraction_retry_count > 0:
            print(f"      - 提取重试: {extraction_retry_count}")
        if inline_content_count > 0:
            print(f"      - JSON内联正文: {inline_content_count}")
        if date_skipped_count > 0:
            print(f"   📅 日期超限: {date_skipped_count} ({date_skipped_count/total_links*100:.1f}%)")
        if candidate_date_prefilter_count > 0:
            print(f"      - 候选日期预过滤: {candidate_date_prefilter_count}")
        if date_unknown_count > 0:
            print(f"   📅 日期未知但已继续处理: {date_unknown_count}")
        if stopped_count > 0:
            print(f"   ⏹️ 停止未处理: {stopped_count}")
        if recall_risk_count > 0:
            print(f"   ⚠️ 需复核候选: {recall_risk_count} 个（详情见审计文件）")
        print(f"   🩺 召回健康度: {recall_health['score']} 分（{recall_health['label']}）")
        print(f"   ❌ 聚合失败: {failed_count} ({failed_count/total_links*100:.1f}%)" if total_links > 0 else "")
        print(f"\n💾 数据库状态:")
        print(f"   当前网站文章数: {stats.get('total_articles', 0)}")
        print(f"   数据库路径: {self.db.db_path}")
        
        return {
            'success': True,
            'needs_review': recall_risk_count > 0,
            'recall_status': recall_status,
            'recall_risk_count': recall_risk_count,
            'recall_health_score': recall_health['score'],
            'recall_health_level': recall_health['level'],
            'recall_health_label': recall_health['label'],
            'recall_health_reasons': recall_health['reasons'],
            'articles': articles,
            'stats': {
                'total_links': total_links,
                'candidate_links': candidate_total_before_db,
                'duplicates': duplicate_count,
                'crawled': total_links,
                'success': success_count,
                'skipped': skipped_count,
                'date_skipped': date_skipped_count,
                'candidate_date_prefilter_skipped': candidate_date_prefilter_count,
                'date_unknown': date_unknown_count,
                'date_parse_failed': date_parse_failed_count,
                'keyword_skipped': keyword_skipped_count,
                'quality_skipped': quality_skipped_count,
                'too_short_skipped': too_short_skipped_count,
                'content_integrity_failed': integrity_failed_count,
                'db_failed': db_failed_count,
                'extraction_retries': extraction_retry_count,
                'inline_content_used': inline_content_count,
                'failed': failed_count,
                'stopped': stopped_count,
                'ragflow': ragflow_stats,
                'recall_risk_count': recall_risk_count,
                'recall_health_score': recall_health['score'],
                'recall_health_level': recall_health['level'],
                'recall_health_label': recall_health['label'],
                'recall_health_reasons': recall_health['reasons'],
                'recall_health_metrics': recall_health['metrics'],
                'needs_review': recall_risk_count > 0,
                'db_total': stats.get('total_articles', 0),
                'crawl_options': crawl_options,
            },
            'audit': audit
        }
    
    def crawl_realtime(self, list_url: str, days_limit: int = 7, keywords: str = '', kb_id: str = None, wait_time: int = 8) -> Dict:
        """
        🚀 实时聚合模式：边提取链接边聚合文章，遇到超出日期范围的立即停止
        
        这是最快的聚合模式，适合只需要最新文章的场景：
        1. 用 Playwright 打开列表页
        2. 逐个提取链接
        3. 每提取一个链接，立即聚合文章内容
        4. 检查文章日期，如果超出范围就停止整个流程
        5. 如果在范围内，保存到数据库并上传到RAGFlow
        
        Args:
            list_url: 新闻列表页URL
            days_limit: 日期限制（只聚合最近N天的文章），默认7天
            keywords: 关键词过滤（可选）
            kb_id: RAGFlow知识库ID（可选）
            wait_time: 页面等待时间（秒）
            
        Returns:
            Dict: {'success': bool, 'articles': list, 'stats': dict}
        """
        print("ℹ️ crawl_realtime 已切换为通用日期优先聚合流程")
        return self.crawl_news_site(
            list_url=list_url,
            limit=float('inf'),
            wait_for=max(1000, int(wait_time or 8) * 1000),
            keywords=keywords,
            kb_id=kb_id,
            days_limit=days_limit,
        )

    def _is_likely_article_url(self, url: str, base_domain: str) -> bool:
        """判断URL是否可能是文章链接"""
        url_lower = url.lower()
        
        # 排除的模式
        exclude_patterns = [
            '/login', '/register', '/search', '/tag/', '/category/',
            '/page/', '/author/', '/about', '/contact', '/privacy',
            '.pdf', '.jpg', '.png', '.gif', '.css', '.js',
            'javascript:', 'mailto:', '#',
            'static.', 'assets.', 'cdn.'
        ]
        
        for pattern in exclude_patterns:
            if pattern in url_lower:
                return False
        
        # 必须包含基础域名
        if base_domain not in url:
            return False
        
        # 通用文章URL特征：常见路径、日期路径、长数字ID或slug。
        article_patterns = ['/article/', '/articles/', '/news/', '/post/', '/posts/', '/story/', '/detail/', '/id/']
        for pattern in article_patterns:
            if pattern in url_lower:
                return True
        
        if re.search(r'/20\d{2}/\d{1,2}/\d{1,2}/', url_lower):
            return True
        if re.search(r'/(?:article|news|post|story|detail|id)[/_=-]?\d{4,}', url_lower):
            return True
        if re.search(r'/\d{5,}(?:[/?#]|$)', url_lower):
            return True
        path_tail = urlparse(url).path.rstrip('/').split('/')[-1]
        if re.search(r'[a-zA-Z\u4e00-\u9fff]{8,}', path_tail) and '-' in path_tail:
            return True
        
        return False

    def _clean_inline_candidate_content(self, content: str) -> str:
        """Clean article body carried directly by a discovery candidate."""
        if not content:
            return ''
        text = str(content)
        if '<' in text and '>' in text:
            text = re.sub(r'(?i)<br\s*/?>', '\n', text)
            text = re.sub(r'<[^>]+>', ' ', text)
        text = html_lib.unescape(text).replace('\xa0', ' ')
        text = re.sub(r'\r\n?', '\n', text)
        text = re.sub(r'[ \t\f\v]+', ' ', text)
        text = re.sub(r'\n\s*\n+', '\n\n', text)
        text = '\n'.join(line.strip() for line in text.splitlines())
        return text.strip()

    def _normalize_crawl_limit(self, limit):
        if limit in (None, '', 'inf', 'infinite', 'unlimited', '无限制'):
            return float('inf')
        try:
            if isinstance(limit, str) and limit.strip().lower() in ('inf', 'infinity', 'unlimited'):
                return float('inf')
            value = int(float(limit))
            return value if value > 0 else float('inf')
        except Exception:
            return float('inf')

    def _normalize_days_limit(self, days_limit):
        try:
            if days_limit in (None, '', 'none'):
                return 0
            return max(0, int(float(days_limit)))
        except Exception:
            return 7

    def _env_bool(self, name: str, default: bool = False) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        return str(value).strip().lower() in ('1', 'true', 'yes', 'on')

    def _date_window_enabled(self, days_limit: int = 0, start_date: str = None, end_date: str = None) -> bool:
        return bool((days_limit and days_limit > 0) or start_date or end_date)

    def _format_date_window(self, days_limit: int = 0, start_date: str = None, end_date: str = None) -> str:
        if start_date or end_date:
            return f"{start_date or '不限'} ~ {end_date or '不限'}"
        if days_limit and days_limit > 0:
            return f"最近{days_limit}天"
        return "不限制"

    def _count_source_methods(self, links: List[Dict]) -> Dict[str, int]:
        counts = Counter()
        for link in links or []:
            raw_method = link.get('source_method') or link.get('source_type') or 'unknown'
            for method in str(raw_method).split(','):
                method = method.strip() or 'unknown'
                counts[method] += 1
        return dict(sorted(counts.items()))

    def _build_discovery_risk_reasons(
        self,
        *,
        playwright_error=None,
        playwright_link_count=0,
        playwright_stats=None,
        supplemental_stats=None,
        source_method_counts=None,
        auto_profile=None,
        candidate_count_before_limit=0,
        final_link_count=0,
        limit_applied=False,
        date_window_enabled=False,
    ) -> Dict[str, int]:
        playwright_stats = playwright_stats or {}
        supplemental_stats = supplemental_stats or {}
        source_method_counts = source_method_counts or {}
        auto_profile = auto_profile or {}

        reasons = {}
        if not auto_profile.get('enabled'):
            reasons['auto_profile_missing'] = 1
        else:
            if not auto_profile.get('main_content_selector') or int(auto_profile.get('main_content_confidence') or 0) < 50:
                reasons['auto_main_content_low_confidence'] = 1
            if not auto_profile.get('article_link_pattern') and not auto_profile.get('article_link_selector'):
                reasons['auto_article_link_pattern_unclear'] = 1
        if playwright_error:
            reasons['playwright_discovery_error'] = 1
        if int(playwright_stats.get('network_json_errors', 0) or 0) > 0:
            reasons['network_json_errors'] = int(playwright_stats.get('network_json_errors', 0) or 0)

        supplemental_errors = supplemental_stats.get('errors') or []
        if supplemental_errors:
            reasons['supplemental_discovery_errors'] = len(supplemental_errors)

        active_sources = [name for name, count in source_method_counts.items() if count > 0]
        if candidate_count_before_limit > 0 and len(active_sources) <= 1 and (
            playwright_error or supplemental_errors or reasons.get('network_json_errors')
        ):
            reasons['single_source_with_discovery_warnings'] = 1

        if date_window_enabled and limit_applied and candidate_count_before_limit > final_link_count:
            reasons['date_window_candidates_truncated'] = candidate_count_before_limit - final_link_count

        if not playwright_link_count and not candidate_count_before_limit:
            reasons['no_playwright_or_supplemental_candidates'] = 1

        return {key: value for key, value in reasons.items() if value}

    def _compute_recall_health(
        self,
        *,
        total_candidates=0,
        processed_candidates=0,
        success_count=0,
        risk_reasons=None,
        discovery_risk_reasons=None,
        source_method_counts=None,
        auto_profile=None,
        date_window_enabled=False,
    ) -> Dict:
        """把发现、抽取、入库、上传风险压缩成一个可展示的召回健康分。"""
        risk_reasons = risk_reasons or {}
        discovery_risk_reasons = discovery_risk_reasons or {}
        source_method_counts = source_method_counts or {}
        auto_profile = auto_profile or {}

        def _to_int(value, default=0):
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return default

        total_candidates = _to_int(total_candidates)
        processed_candidates = _to_int(processed_candidates)
        success_count = _to_int(success_count)
        denominator = max(processed_candidates, total_candidates, success_count, 1)

        merged_reasons = dict(risk_reasons)
        for key, value in discovery_risk_reasons.items():
            merged_reasons[key] = _to_int(merged_reasons.get(key)) + _to_int(value)

        severe_keys = {
            'extract_failed',
            'content_integrity_failed',
            'db_failed',
            'stopped',
            'ragflow_delivery_risk',
            'playwright_discovery_error',
            'no_playwright_or_supplemental_candidates',
        }
        medium_keys = {
            'quality_skipped',
            'too_short_skipped',
            'date_unknown_in_window',
            'date_window_candidates_truncated',
            'supplemental_discovery_errors',
            'network_json_errors',
        }
        soft_keys = {
            'auto_profile_missing',
            'auto_main_content_low_confidence',
            'auto_article_link_pattern_unclear',
            'single_source_with_discovery_warnings',
        }

        severe_count = sum(_to_int(merged_reasons.get(key)) for key in severe_keys)
        medium_count = sum(_to_int(merged_reasons.get(key)) for key in medium_keys)
        soft_count = sum(_to_int(merged_reasons.get(key)) for key in soft_keys)

        score = 100.0
        score -= min(55.0, severe_count / denominator * 85.0)
        score -= min(32.0, medium_count / denominator * 55.0)
        score -= min(16.0, soft_count * 6.0)

        if total_candidates == 0:
            score = min(score, 20.0)
        elif processed_candidates > 0 and success_count == 0:
            score = min(score, 35.0)

        active_sources = [name for name, count in source_method_counts.items() if _to_int(count) > 0]
        if processed_candidates >= 5 and len(active_sources) <= 1:
            score -= 6.0
            merged_reasons.setdefault('single_discovery_source', 1)

        if date_window_enabled:
            unknown_dates = _to_int(merged_reasons.get('date_unknown_in_window'))
            if processed_candidates and unknown_dates / max(processed_candidates, 1) >= 0.5:
                score -= 10.0
                merged_reasons.setdefault('many_unknown_dates_in_window', unknown_dates)

        if auto_profile.get('enabled'):
            confidence = _to_int(auto_profile.get('main_content_confidence'))
            if confidence >= 85:
                score += 3.0
            elif confidence < 50:
                score -= 8.0
        else:
            score -= 5.0

        score = max(0, min(100, round(score)))
        if score >= 85:
            level = 'good'
            label = '健康'
        elif score >= 65:
            level = 'warning'
            label = '注意'
        else:
            level = 'risky'
            label = '高风险'

        reason_labels = {
            'extract_failed': '部分文章详情打开或正文抽取失败',
            'content_integrity_failed': '正文完整性校验失败',
            'db_failed': '文章入库失败或重复异常',
            'stopped': '任务中途停止，仍有候选未处理',
            'quality_skipped': '有文章质量分偏低被跳过',
            'too_short_skipped': '有文章正文过短被跳过',
            'date_unknown_in_window': '日期范围任务中存在发布时间未知的文章',
            'ragflow_delivery_risk': 'RAGFlow 上传存在失败或禁用风险',
            'playwright_discovery_error': '动态页面发现阶段报错',
            'network_json_errors': '网络接口候选解析有错误',
            'supplemental_discovery_errors': 'RSS/Sitemap/静态页补充发现有错误',
            'single_source_with_discovery_warnings': '候选来源单一且发现阶段有警告',
            'single_discovery_source': '候选主要来自单一发现来源',
            'date_window_candidates_truncated': '日期范围任务被数量限制截断过候选',
            'no_playwright_or_supplemental_candidates': '没有发现任何文章候选',
            'auto_profile_missing': '未拿到页面自动识别画像',
            'auto_main_content_low_confidence': '栏目主区域自动识别置信度偏低',
            'auto_article_link_pattern_unclear': '文章链接规则不够清晰',
            'many_unknown_dates_in_window': '日期范围内未知日期占比偏高',
        }
        readable_reasons = []
        for key, value in sorted(merged_reasons.items()):
            count = _to_int(value)
            if count <= 0:
                continue
            label_text = reason_labels.get(key, key)
            readable_reasons.append(f"{label_text}：{count}")

        return {
            'score': score,
            'level': level,
            'label': label,
            'reasons': readable_reasons,
            'raw_reasons': {key: value for key, value in merged_reasons.items() if _to_int(value) > 0},
            'metrics': {
                'total_candidates': total_candidates,
                'processed_candidates': processed_candidates,
                'success_count': success_count,
                'source_count': len(active_sources),
                'date_window_enabled': bool(date_window_enabled),
            }
        }

    def crawl_news_site(
        self,
        list_url: str,
        limit: int = 10,
        wait_for: int = 8000,
        keywords: str = '',
        kb_id: str = None,
        days_limit: int = 7,
        start_date: str = None,
        end_date: str = None,
        should_stop=None,
        crawl_options: Dict = None,
        task_id: str = None
    ) -> Dict:
        """
        通用栏目文章聚合：只使用 Playwright 发现栏目页文章链接。

        Args:
            list_url: 新闻/文章栏目页URL
            limit: 保留兼容；实际文章发现默认不限制，避免漏文章
            wait_for: JavaScript渲染等待时间(ms)，默认8000
            keywords: 关键词过滤（入库前检查）
            kb_id: RAGFlow知识库ID（可选）
            days_limit: 日期限制（只保存最近N天文章），0表示不限制
            start_date: 指定起始日期（YYYY-MM-DD，可选，优先于days_limit）
            end_date: 指定结束日期（YYYY-MM-DD，可选）
            task_id: 当前聚合任务ID，用于写入 article_tasks 关联

        Returns:
            Dict: {'success': bool, 'articles': list, 'stats': dict}
        """
        # All list-page browser work belongs to the VPN service.  Delegate to
        # the dual-channel wrapper before any Playwright module is imported.
        from hybrid_crawler import HybridCrawler
        return HybridCrawler(db=self.db).crawl_news_site(
            list_url=list_url,
            limit=limit,
            wait_for=wait_for,
            keywords=keywords,
            kb_id=kb_id,
            days_limit=days_limit,
            start_date=start_date,
            end_date=end_date,
            crawl_options=crawl_options,
            task_id=task_id,
        )
        crawl_options = normalize_crawl_options(crawl_options)
        self.crawl_options = crawl_options
        self._sync_validator_proxy()
        limit = self._normalize_crawl_limit(limit)
        max_articles_per_task = int(crawl_options.get('max_articles_per_task') or 100)
        days_limit = self._normalize_days_limit(days_limit)
        wait_for = int(crawl_options.get('wait_for_ms') or wait_for)
        has_date_window = self._date_window_enabled(days_limit, start_date, end_date)
        respect_limit_with_date = bool(crawl_options.get('respect_limit_with_date_range', False))
        limit_applies = (limit != float('inf')) and (not has_date_window or respect_limit_with_date)

        print("=" * 70)
        print("Playwright 栏目文章提取器")
        print("=" * 70)
        print(f"目标URL: {list_url}")
        print(f"等待时间: {wait_for}ms")
        print(f"日期范围: {self._format_date_window(days_limit, start_date, end_date)}")
        print(
            "策略配置: "
            f"max_pages={crawl_options.get('max_pages')}, "
            f"max_articles_per_task={max_articles_per_task}, "
            f"max_empty_pages={crawl_options.get('max_empty_pages')}, "
            f"detail_retries={crawl_options.get('detail_max_retries')}, "
            f"supplemental={'on' if crawl_options.get('supplemental_enabled') else 'off'}, "
            f"network_json={'on' if crawl_options.get('network_json_enabled') else 'off'}, "
            f"proxy={'on' if crawl_options.get('proxy_enabled') else 'off'}"
        )
        if limit != float('inf'):
            if limit_applies:
                print(f"文章数量限制: {limit}")
            else:
                print(f"文章数量限制: {limit}（已按日期范围优先，不用于截断候选）")
        print("策略：只用 Playwright 遍历栏目页、分页、Tab、加载更多；不调用 Firecrawl")

        links = []
        extraction_method = 'playwright'

        print(f"\n{'='*70}")
        print("【阶段1】使用 Playwright 智能提取栏目文章链接")
        print(f"{'='*70}")

        playwright_error = None
        playwright_stats = {}
        auto_profile = {}
        candidate_count_before_limit = 0
        limit_applied = False

        try:
            print("使用 PlaywrightLinkExtractor 智能提取...")
            print(f"   目标URL: {list_url}")
            print(f"   等待时间: {wait_for}ms")

            from playwright_link_extractor import PlaywrightLinkExtractor
            import asyncio

            extractor_pl = PlaywrightLinkExtractor(crawl_options=crawl_options)

            def _read_discovery_int(name: str, default: int, min_value: int = 1, max_value: int = 10000) -> int:
                try:
                    value = int(os.getenv(name, str(default)))
                except (TypeError, ValueError):
                    value = default
                return max(min_value, min(value, max_value))

            if limit_applies:
                discovery_max_articles = min(max_articles_per_task, max(1, int(limit)))
                discovery_max_pages = _read_discovery_int('CRAWL_LINK_DISCOVERY_MAX_PAGES_WITH_LIMIT', 10, 1, 1000)
            else:
                discovery_max_articles = max_articles_per_task
                discovery_max_pages = _read_discovery_int('CRAWL_LINK_DISCOVERY_MAX_PAGES', 30, 1, 1000)
            discovery_max_pages = int(crawl_options.get('max_pages') or discovery_max_pages)

            print(f"   链接发现上限: max_articles={discovery_max_articles}, max_pages={discovery_max_pages}")

            async def extract_links_async():
                return await extractor_pl.extract_links_from_url(
                    url=list_url,
                    max_articles=discovery_max_articles,
                    max_pages=discovery_max_pages,
                    wait_time=max(1, wait_for // 1000)
                )

            pl_result = asyncio.run(extract_links_async())

            if not pl_result.get('success'):
                playwright_error = pl_result.get('error', '未知错误')
                print(f"   PlaywrightLinkExtractor 失败: {playwright_error}")
                print("   将继续尝试补充发现：HTML/RSS/Sitemap/结构化数据")
                pl_result = {'articles': [], 'stats': {}}

            stats = pl_result.get('stats', {})
            playwright_stats = stats
            auto_profile = stats.get('auto_profile') if isinstance(stats.get('auto_profile'), dict) else {}

            for article in pl_result.get('articles', []):
                article_url = article.get('url', '')
                if not article_url:
                    continue
                link_info = {
                    'title': article.get('title', ''),
                    'url': article_url,
                    'text': article.get('title', ''),
                    'publish_date': article.get('publish_date', ''),
                    'source_method': article.get('source_method') or 'playwright'
                }
                for field in ('content_hint', 'content_hint_source', 'discovery_source_url', 'authors'):
                    if article.get(field):
                        link_info[field] = article.get(field)
                links.append(link_info)

            # 去重但不做强过滤，避免静默遗漏。
            deduped_links = []
            seen_urls = set()
            for link in links:
                url_key = link.get('url', '').strip().lower()
                if not url_key or url_key in seen_urls:
                    continue
                seen_urls.add(url_key)
                deduped_links.append(link)
            links = deduped_links

            print("   Playwright 提取成功")
            print("   统计信息:")
            print(f"      - 访问页数: {stats.get('pages_visited', 0)}")
            print(f"      - 候选链接: {len(links)} 个")
            if stats.get('network_json_candidates'):
                print(f"      - 网络JSON候选: {stats.get('network_json_candidates', 0)}")
                print(f"      - JSON内联正文: {stats.get('network_json_inline_candidates', 0)}")

        except Exception as e:
            print(f"   Playwright 提取异常: {e}")
            playwright_error = str(e)
            print("   将继续尝试补充发现：HTML/RSS/Sitemap/结构化数据")

        playwright_link_count = len(links)
        supplemental_links = []
        supplemental_stats = {}

        print(f"\n{'='*70}")
        print("【阶段1.2】补充发现：HTML/RSS/Sitemap/结构化数据")
        print(f"{'='*70}")
        try:
            from supplemental_link_discovery import discover_supplemental_article_links, merge_link_candidates

            supplemental_result = discover_supplemental_article_links(
                list_url,
                crawl_options=crawl_options
            )
            supplemental_links = supplemental_result.get('links', [])
            supplemental_stats = supplemental_result.get('stats', {})
            links = merge_link_candidates(links, supplemental_links)
            extraction_method = 'playwright+supplemental'

            print(f"   补充候选: {len(supplemental_links)} 个")
            print(f"      - 静态HTML: {supplemental_stats.get('html_static_candidates', 0)}")
            if supplemental_stats.get('selectolax_candidates'):
                print(f"      - selectolax高速解析: {supplemental_stats.get('selectolax_candidates', 0)}")
            print(f"      - 属性/按钮: {supplemental_stats.get('attribute_candidates', 0)}")
            print(f"      - 结构化数据: {supplemental_stats.get('structured_candidates', 0)}")
            print(f"      - 脚本内URL: {supplemental_stats.get('embedded_script_candidates', 0)}")
            print(f"      - 静态分页: {supplemental_stats.get('static_pagination_candidates', 0)}")
            print(f"      - RSS/Atom: {supplemental_stats.get('feed_candidates', 0)}")
            if supplemental_stats.get('feed_inline_candidates'):
                print(f"      - Feed内联正文: {supplemental_stats.get('feed_inline_candidates', 0)}")
            print(f"      - Sitemap: {supplemental_stats.get('sitemap_candidates', 0)}")
            print(f"   合并后候选: {len(links)} 个")
            if supplemental_stats.get('errors'):
                print(f"   ⚠️ 补充发现警告: {len(supplemental_stats.get('errors', []))} 条")
        except Exception as e:
            supplemental_stats = {'errors': [str(e)]}
            print(f"   ⚠️ 补充发现失败，继续使用Playwright结果: {e}")

        candidate_count_before_limit = len(links)
        source_method_counts_before_limit = self._count_source_methods(links)
        detail_limit = min(max_articles_per_task, int(limit)) if limit_applies else max_articles_per_task
        if len(links) > detail_limit:
            print(f"   单任务详情上限={detail_limit}，候选从 {len(links)} 个截断到 {detail_limit} 个用于详情抓取")
            links = links[:detail_limit]
            limit_applied = True
        elif has_date_window and limit != float('inf'):
            print(f"   日期范围已启用，当前候选 {len(links)} 个未超过单任务详情上限 {detail_limit}")
        source_method_counts = self._count_source_methods(links)
        discovery_risk_reasons = self._build_discovery_risk_reasons(
            playwright_error=playwright_error,
            playwright_link_count=playwright_link_count,
            playwright_stats=playwright_stats,
            supplemental_stats=supplemental_stats,
            source_method_counts=source_method_counts_before_limit,
            auto_profile=auto_profile,
            candidate_count_before_limit=candidate_count_before_limit,
            final_link_count=len(links),
            limit_applied=limit_applied,
            date_window_enabled=has_date_window,
        )
        discovery_risk_count = sum(int(value or 0) for value in discovery_risk_reasons.values())
        if source_method_counts:
            print(f"   候选来源分布: {source_method_counts}")
        if discovery_risk_reasons:
            print(f"   ⚠️ 发现阶段需复核: {discovery_risk_reasons}")

        # 最终统计和显示
        print(f"\n{'='*70}")
        print(f"📊 链接提取完成")
        print(f"{'='*70}")
        print(f"✅ 提取方式: {extraction_method.upper() if extraction_method else '未知'}")
        print(f"📝 共提取: {len(links)} 个文章链接")
        print(f"{'='*70}")
        
        if not links:
            print("⚠️ 最终没有找到文章链接")
            verification_signals = playwright_stats.get('network_verification_signals') or []
            if verification_signals:
                error_message = '没有找到文章链接；检测到页面/API需要验证或登录'
            elif playwright_stats.get('network_json_responses_checked', 0) > 0 and playwright_stats.get('network_json_candidates', 0) == 0:
                error_message = '没有找到文章链接；已检查网络接口但未解析到文章候选'
            else:
                error_message = '没有找到文章链接'
            if playwright_error:
                error_message = f'{error_message}; Playwright失败: {playwright_error}'
            no_candidate_reasons = {'no_playwright_or_supplemental_candidates': 1}
            if verification_signals:
                no_candidate_reasons['network_verification_required'] = len(verification_signals)
            if playwright_stats.get('network_json_responses_checked', 0) > 0:
                no_candidate_reasons['network_json_checked_without_candidates'] = playwright_stats.get('network_json_responses_checked', 0)
            recall_health = self._compute_recall_health(
                total_candidates=0,
                processed_candidates=0,
                success_count=0,
                risk_reasons=no_candidate_reasons,
                discovery_risk_reasons=discovery_risk_reasons,
                source_method_counts=source_method_counts,
                auto_profile=auto_profile,
                date_window_enabled=has_date_window,
            )
            audit = {
                'source_url': list_url,
                'started_at': get_china_time().isoformat(),
                'completed_at': get_china_time().isoformat(),
                'recall_status': 'needs_review',
                'recall_risk_count': max(1, discovery_risk_count),
                'recall_risk_reasons': recall_health['raw_reasons'],
                'recall_health_score': recall_health['score'],
                'recall_health_level': recall_health['level'],
                'recall_health_label': recall_health['label'],
                'recall_health_reasons': recall_health['reasons'],
                'recall_health_metrics': recall_health['metrics'],
                'total_candidates_before_db_dedupe': 0,
                'total_candidates_after_db_dedupe': 0,
                'status_counts': {'no_candidates': 1},
                'items': [{
                    'url': list_url,
                    'title': '',
                    'source_method': 'discovery',
                    'status': 'no_candidates',
                    'reason': error_message,
                    'updated_at': get_china_time().isoformat(),
                }],
                'discovery_stats': {
                    'playwright_links': playwright_link_count,
                    'playwright_error': playwright_error,
                    'auto_profile': auto_profile,
                    'network_json_candidates': playwright_stats.get('network_json_candidates', 0),
                    'network_json_inline_candidates': playwright_stats.get('network_json_inline_candidates', 0),
                    'network_json_responses_checked': playwright_stats.get('network_json_responses_checked', 0),
                    'network_json_responses_used': playwright_stats.get('network_json_responses_used', 0),
                    'network_json_errors': playwright_stats.get('network_json_errors', 0),
                    'network_jsonp_responses': playwright_stats.get('network_jsonp_responses', 0),
                    'network_script_responses_checked': playwright_stats.get('network_script_responses_checked', 0),
                    'network_html_error_responses': playwright_stats.get('network_html_error_responses', 0),
                    'network_verification_signals': playwright_stats.get('network_verification_signals', []),
                    'network_response_samples': playwright_stats.get('network_response_samples', []),
                    'network_endpoint_stats': playwright_stats.get('network_endpoint_stats', {}),
                    'site_profile': playwright_stats.get('site_profile', 'generic'),
                    'supplemental_links': len(supplemental_links),
                    'html_static_candidates': supplemental_stats.get('html_static_candidates', 0),
                    'selectolax_candidates': supplemental_stats.get('selectolax_candidates', 0),
                    'attribute_candidates': supplemental_stats.get('attribute_candidates', 0),
                    'structured_candidates': supplemental_stats.get('structured_candidates', 0),
                    'embedded_script_candidates': supplemental_stats.get('embedded_script_candidates', 0),
                    'static_pagination_candidates': supplemental_stats.get('static_pagination_candidates', 0),
                    'feed_candidates': supplemental_stats.get('feed_candidates', 0),
                    'feed_inline_candidates': supplemental_stats.get('feed_inline_candidates', 0),
                    'sitemap_candidates': supplemental_stats.get('sitemap_candidates', 0),
                    'sitemaps_checked': supplemental_stats.get('sitemaps_checked', 0),
                    'feeds_checked': supplemental_stats.get('feeds_checked', 0),
                    'supplemental_cache_enabled': supplemental_stats.get('cache_enabled', False),
                    'supplemental_cache_hits': supplemental_stats.get('cache_hits', 0),
                    'supplemental_retry_enabled': supplemental_stats.get('retry_enabled', False),
                    'candidate_links_before_limit': candidate_count_before_limit,
                    'source_method_counts': source_method_counts,
                    'source_method_counts_before_limit': source_method_counts_before_limit,
                    'discovery_risk_reasons': discovery_risk_reasons,
                    'discovery_risk_count': discovery_risk_count,
                    'recall_health_score': recall_health['score'],
                    'recall_health_level': recall_health['level'],
                    'recall_health_label': recall_health['label'],
                    'recall_health_reasons': recall_health['reasons'],
                    'recall_health_metrics': recall_health['metrics'],
                    'limit_requested': None if limit == float('inf') else limit,
                    'limit_applied': limit_applied,
                    'date_window_enabled': has_date_window,
                    'days_limit': days_limit,
                    'start_date': start_date,
                    'end_date': end_date,
                    'crawl_options': crawl_options,
                    'errors': supplemental_stats.get('errors', []),
                }
            }
            return {
                'success': False,
                'error': error_message,
                'needs_review': True,
                'recall_status': 'needs_review',
                'recall_health_score': recall_health['score'],
                'recall_health_level': recall_health['level'],
                'recall_health_label': recall_health['label'],
                'recall_health_reasons': recall_health['reasons'],
                'stats': audit['discovery_stats'],
                'audit': audit,
            }
        
        # 显示链接列表（显示前10个）
        print(f"\n📰 文章列表预览（共{len(links)}篇，显示前10篇）:")
        for i, link in enumerate(links[:10], 1):
            print(f"   {i:2d}. {link['title'][:60]}...")
            print(f"       URL: {link['url'][:80]}...")
        if len(links) > 10:
            print(f"\n   ... 还有 {len(links)-10} 篇文章")
        
        # ✅ 不在链接提取时预过滤关键词，只在入库时过滤
        # 让所有提取到的链接都被聚合，关键词过滤会在入库时自动进行
        if keywords and keywords.strip():
            keyword_list = [kw.strip() for kw in keywords.split(',') if kw.strip()]
            print(f"\n🎯 关键词过滤配置: {', '.join(keyword_list)}")
            print(f"   💡 所有 {len(links)} 个链接都会被聚合，入库时会自动检查关键词")
        
        # 🔥 阶段2: 聚合每篇文章详情
        print(f"\n{'='*70}")
        print(f"【阶段2】聚合每篇文章详情")
        print(f"{'='*70}")
        detail_result = self._crawl_article_details(
            links,
            limit,
            list_url,
            keywords=keywords,
            kb_id=kb_id,
            days_limit=days_limit,
            start_date=start_date,
            end_date=end_date,
            should_stop=should_stop,
            crawl_options=crawl_options,
            task_id=task_id
        )
        if isinstance(detail_result, dict):
            detail_stats = detail_result.setdefault('stats', {})
            detail_stats.update({
                'playwright_links': playwright_link_count,
                'playwright_error': playwright_error,
                'auto_profile': auto_profile,
                'network_json_candidates': playwright_stats.get('network_json_candidates', 0),
                'network_json_inline_candidates': playwright_stats.get('network_json_inline_candidates', 0),
                'network_json_responses_checked': playwright_stats.get('network_json_responses_checked', 0),
                'network_json_responses_used': playwright_stats.get('network_json_responses_used', 0),
                'network_json_errors': playwright_stats.get('network_json_errors', 0),
                'network_jsonp_responses': playwright_stats.get('network_jsonp_responses', 0),
                'network_script_responses_checked': playwright_stats.get('network_script_responses_checked', 0),
                'network_html_error_responses': playwright_stats.get('network_html_error_responses', 0),
                'network_verification_signals': playwright_stats.get('network_verification_signals', []),
                'network_response_samples': playwright_stats.get('network_response_samples', []),
                'network_endpoint_stats': playwright_stats.get('network_endpoint_stats', {}),
                'site_profile': playwright_stats.get('site_profile', 'generic'),
                'supplemental_links': len(supplemental_links),
                'candidate_links_before_limit': candidate_count_before_limit,
                'source_method_counts': source_method_counts,
                'source_method_counts_before_limit': source_method_counts_before_limit,
                'discovery_risk_reasons': discovery_risk_reasons,
                'discovery_risk_count': discovery_risk_count,
                'limit_requested': None if limit == float('inf') else limit,
                'limit_applied': limit_applied,
                'date_window_enabled': has_date_window,
                'days_limit': days_limit,
                'start_date': start_date,
                'end_date': end_date,
                'merged_links': len(links),
                'html_static_candidates': supplemental_stats.get('html_static_candidates', 0),
                'selectolax_candidates': supplemental_stats.get('selectolax_candidates', 0),
                'attribute_candidates': supplemental_stats.get('attribute_candidates', 0),
                'structured_candidates': supplemental_stats.get('structured_candidates', 0),
                'embedded_script_candidates': supplemental_stats.get('embedded_script_candidates', 0),
                'static_pagination_candidates': supplemental_stats.get('static_pagination_candidates', 0),
                'static_pages_checked': supplemental_stats.get('static_pages_checked', 0),
                'feed_candidates': supplemental_stats.get('feed_candidates', 0),
                'feed_inline_candidates': supplemental_stats.get('feed_inline_candidates', 0),
                'sitemap_candidates': supplemental_stats.get('sitemap_candidates', 0),
                'sitemaps_checked': supplemental_stats.get('sitemaps_checked', 0),
                'feeds_checked': supplemental_stats.get('feeds_checked', 0),
                'supplemental_cache_enabled': supplemental_stats.get('cache_enabled', False),
                'supplemental_cache_hits': supplemental_stats.get('cache_hits', 0),
                'supplemental_retry_enabled': supplemental_stats.get('retry_enabled', False),
                'crawl_options': crawl_options,
            })
            detail_audit = detail_result.get('audit') if isinstance(detail_result.get('audit'), dict) else {}
            if detail_audit is not None:
                detail_audit.setdefault('discovery_stats', {})
                detail_audit['discovery_stats'].update({
                    'playwright_links': playwright_link_count,
                    'playwright_error': playwright_error,
                    'auto_profile': auto_profile,
                    'network_json_candidates': playwright_stats.get('network_json_candidates', 0),
                    'network_json_inline_candidates': playwright_stats.get('network_json_inline_candidates', 0),
                    'network_json_responses_checked': playwright_stats.get('network_json_responses_checked', 0),
                    'network_json_responses_used': playwright_stats.get('network_json_responses_used', 0),
                    'network_json_errors': playwright_stats.get('network_json_errors', 0),
                    'network_jsonp_responses': playwright_stats.get('network_jsonp_responses', 0),
                    'network_script_responses_checked': playwright_stats.get('network_script_responses_checked', 0),
                    'network_html_error_responses': playwright_stats.get('network_html_error_responses', 0),
                    'network_verification_signals': playwright_stats.get('network_verification_signals', []),
                    'network_response_samples': playwright_stats.get('network_response_samples', []),
                    'network_endpoint_stats': playwright_stats.get('network_endpoint_stats', {}),
                    'site_profile': playwright_stats.get('site_profile', 'generic'),
                    'supplemental_links': len(supplemental_links),
                    'source_method_counts': source_method_counts,
                    'source_method_counts_before_limit': source_method_counts_before_limit,
                    'selectolax_candidates': supplemental_stats.get('selectolax_candidates', 0),
                    'supplemental_cache_enabled': supplemental_stats.get('cache_enabled', False),
                    'supplemental_cache_hits': supplemental_stats.get('cache_hits', 0),
                    'supplemental_retry_enabled': supplemental_stats.get('retry_enabled', False),
                    'candidate_links_before_limit': candidate_count_before_limit,
                    'limit_requested': None if limit == float('inf') else limit,
                    'limit_applied': limit_applied,
                    'date_window_enabled': has_date_window,
                    'discovery_risk_reasons': discovery_risk_reasons,
                    'discovery_risk_count': discovery_risk_count,
                    'errors': supplemental_stats.get('errors', []),
                })
                if discovery_risk_count:
                    existing_reasons = detail_audit.setdefault('recall_risk_reasons', {})
                    for key, value in discovery_risk_reasons.items():
                        existing_reasons[key] = int(existing_reasons.get(key, 0) or 0) + int(value or 0)
                    detail_audit['recall_risk_count'] = int(detail_audit.get('recall_risk_count', 0) or 0) + discovery_risk_count
                    detail_audit['recall_status'] = 'needs_review'
            if discovery_risk_count:
                detail_result['needs_review'] = True
                detail_result['recall_status'] = 'needs_review'
                detail_result['recall_risk_count'] = int(detail_result.get('recall_risk_count', 0) or 0) + discovery_risk_count
                detail_stats['needs_review'] = True
                detail_stats['recall_risk_count'] = int(detail_stats.get('recall_risk_count', 0) or 0) + discovery_risk_count
            final_risk_reasons = {}
            if isinstance(detail_audit, dict):
                final_risk_reasons.update(detail_audit.get('recall_risk_reasons') or {})
            else:
                final_risk_reasons.update(discovery_risk_reasons)

            final_recall_health = self._compute_recall_health(
                total_candidates=detail_stats.get('candidate_links') or candidate_count_before_limit,
                processed_candidates=detail_stats.get('total_links') or len(links),
                success_count=detail_stats.get('success', 0),
                risk_reasons=final_risk_reasons,
                source_method_counts=source_method_counts_before_limit or source_method_counts,
                auto_profile=auto_profile,
                date_window_enabled=has_date_window,
            )

            if isinstance(detail_audit, dict):
                detail_audit['recall_health_score'] = final_recall_health['score']
                detail_audit['recall_health_level'] = final_recall_health['level']
                detail_audit['recall_health_label'] = final_recall_health['label']
                detail_audit['recall_health_reasons'] = final_recall_health['reasons']
                detail_audit['recall_health_metrics'] = final_recall_health['metrics']
                detail_audit['recall_health_raw_reasons'] = final_recall_health['raw_reasons']

            detail_result['recall_health_score'] = final_recall_health['score']
            detail_result['recall_health_level'] = final_recall_health['level']
            detail_result['recall_health_label'] = final_recall_health['label']
            detail_result['recall_health_reasons'] = final_recall_health['reasons']
            detail_stats['recall_health_score'] = final_recall_health['score']
            detail_stats['recall_health_level'] = final_recall_health['level']
            detail_stats['recall_health_label'] = final_recall_health['label']
            detail_stats['recall_health_reasons'] = final_recall_health['reasons']
            detail_stats['recall_health_metrics'] = final_recall_health['metrics']
            if final_recall_health['level'] == 'risky':
                detail_result['needs_review'] = True
                detail_result['recall_status'] = 'needs_review'
                detail_stats['needs_review'] = True
        return detail_result
    
    def process_firecrawl_result(self, firecrawl_url: str) -> Dict:
        """Legacy external Firecrawl result processor; intentionally disabled."""
        return {
            'success': False,
            'disabled': True,
            'error': 'Legacy Firecrawl result processing is disabled. Use crawl_news_site() with the Playwright article crawler.',
            'replacement': 'ArticleLinkExtractor.crawl_news_site',
            'processed_at': get_china_time().isoformat()
        }

    def _upload_articles_to_ragflow(self, articles: List[Dict], kb_id: str):
        """
        上传文章到RAGFlow知识库
        
        Args:
            articles: 文章列表，每个元素包含 {'content': {...}, 'db_id': ...}
            kb_id: RAGFlow知识库ID
        """
        if not articles or not kb_id:
            print(f"   ⚠️ 跳过上传: articles={len(articles) if articles else 0}, kb_id={kb_id}")
            return {
                'success': True,
                'stats': {
                    'uploaded': 0,
                    'skipped_empty': len(articles) if articles else 0,
                    'failed': 0
                }
            }
        
        print(f"   🔄 开始上传 {len(articles)} 篇文章到知识库 {kb_id}...")
        
        try:
            from ragflow_client import RagflowClient
            import tempfile
            import os
            
            print(f"   📦 创建RAGFlow客户端...")
            ragflow_client = RagflowClient()
            
            uploaded = 0
            skipped = 0
            failed = 0
            document_ids = []  # 收集上传成功的文档ID
            
            # 创建临时目录
            import tempfile
            tmpdir = tempfile.mkdtemp(prefix='ragflow_upload_')
            
            # 为每篇文章创建临时文件并上传
            for i, article_entry in enumerate(articles, 1):
                try:
                    article_content = article_entry.get('content', {})
                    title = article_content.get('title', '无标题')
                    content = article_content.get('content', '')
                    url = article_content.get('url', '')
                    
                    if not content:
                        print(f"      ⏭️  [{i}/{len(articles)}] 跳过空内容: {title[:30]}...")
                        skipped += 1
                        continue
                    
                    # 生成稳定文件名，避免同标题文章互相覆盖/跳过
                    file_name = ragflow_client.build_document_name(
                        title,
                        url,
                        article_entry.get('db_id') or i
                    )
                    
                    # 🔥 检查文档是否已存在
                    try:
                        list_result = ragflow_client.list_documents(kb_id, page=1, page_size=10, name=file_name)
                        if isinstance(list_result, dict):
                            existing_docs = list_result.get('docs', [])
                        else:
                            existing_docs = list_result if isinstance(list_result, list) else []
                        
                        # 检查是否有完全匹配的文件名
                        exact_existing_docs = [doc for doc in existing_docs if doc.get('name') == file_name]
                        if exact_existing_docs:
                            if getattr(config, 'RAGFLOW_REUPLOAD_EXISTING', True):
                                doc_ids = [doc.get('id') for doc in exact_existing_docs if doc.get('id')]
                                if doc_ids:
                                    ragflow_client.delete_documents(kb_id, doc_ids)
                                    print(f"      ♻️  [{i}/{len(articles)}] 已删除旧文档，准备重传: {title[:30]}...")
                            else:
                                print(f"      ⏭️  [{i}/{len(articles)}] 跳过已存在: {title[:30]}...")
                                skipped += 1
                                continue
                    except Exception as check_error:
                        # 检查失败不影响上传，继续尝试
                        pass
                    
                    # 创建临时文件（使用有意义的文件名）
                    tmp_file_path = os.path.join(tmpdir, file_name)
                    with open(tmp_file_path, 'w', encoding='utf-8') as f:
                        f.write(f"{title}\n\n")
                        f.write(f"来源: {url}\n\n")
                        f.write(content)
                    
                    try:
                        # 上传到RAGFlow
                        result = ragflow_client.upload_document(kb_id, tmp_file_path)
                        
                        if result and isinstance(result, dict):
                            # 提取文档ID
                            doc_data = result.get('data')
                            doc_id = None
                            if doc_data:
                                if isinstance(doc_data, list) and len(doc_data) > 0:
                                    doc_id = doc_data[0].get('id')
                                elif isinstance(doc_data, dict):
                                    doc_id = doc_data.get('id')
                                
                                if doc_id:
                                    document_ids.append(doc_id)
                                    print(f"      ✅ [{i}/{len(articles)}] 上传成功: {title[:30]}... (ID: {doc_id})")
                                else:
                                    print(f"      ✅ [{i}/{len(articles)}] 上传成功: {title[:30]}... (未获取到文档ID)")
                            else:
                                print(f"      ✅ [{i}/{len(articles)}] 上传成功: {title[:30]}... (响应无data字段)")
                            
                            uploaded += 1
                        else:
                            failed += 1
                            print(f"      ❌ [{i}/{len(articles)}] 上传失败: {title[:30]}...")
                    except Exception as upload_error:
                        failed += 1
                        print(f"      ❌ [{i}/{len(articles)}] 上传失败: {upload_error}")
                        
                except Exception as article_error:
                    failed += 1
                    print(f"      ❌ [{i}/{len(articles)}] 处理失败: {article_error}")
            
            # 清理临时目录
            try:
                import shutil
                shutil.rmtree(tmpdir)
            except:
                pass
            
            print(f"   ✅ RAGFlow上传完成: 成功{uploaded} | 跳过{skipped} | 失败{failed}")
            
            # 🔥 触发文档解析
            print(f"   🔍 调试: 收集到 {len(document_ids)} 个文档ID")
            if document_ids:
                print(f"   📊 触发文档解析: {len(document_ids)} 个文档")
                print(f"   📝 文档ID列表: {document_ids[:3]}...")  # 显示前3个
                try:
                    parse_result = ragflow_client.parse_documents(kb_id, document_ids)
                    print(f"   📝 解析API响应: {parse_result}")
                    if parse_result.get('code') == 0:
                        print(f"   ✅ 文档解析已触发")
                    else:
                        print(f"   ⚠️  触发解析失败: {parse_result.get('message', '未知错误')}")
                except Exception as parse_error:
                    print(f"   ⚠️  触发解析异常: {parse_error}")
                    import traceback
                    traceback.print_exc()
            else:
                print(f"   ⚠️  没有收集到文档ID，无法触发解析")
            
            # 🔥 触发文档解析（RAGFlow需要手动触发解析才能使用）
            if uploaded > 0:
                print(f"   📊 提示：请在RAGFlow界面中手动触发文档解析")
                print(f"   💡 或者等待RAGFlow自动解析（可能需要几分钟）")
            return {
                'success': True,
                'stats': {
                    'uploaded': uploaded,
                    'skipped_existing': skipped,
                    'failed': failed
                }
            }
                
        except Exception as e:
            print(f"   ⚠️ RAGFlow上传异常: {e}")
            import traceback
            traceback.print_exc()
            return {
                'success': False,
                'error': str(e),
                'stats': {
                    'uploaded': 0,
                    'failed': len(articles) if articles else 0
                }
            }
    
    def _upload_single_article_to_ragflow(self, article_entry: Dict, kb_id: str):
        """
        上传单篇文章到RAGFlow并立即触发解析
        
        Args:
            article_entry: 文章条目 {'content': {...}, 'db_id': ...}
            kb_id: RAGFlow知识库ID
        """
        if not article_entry or not kb_id:
            return {'status': 'skipped_config', 'uploaded': False}
        
        try:
            import config
            from ragflow_client import RagflowClient

            if not getattr(config, 'RAGFLOW_UPLOAD_ENABLED', True):
                return {'status': 'disabled', 'uploaded': False}
            
            ragflow_client = RagflowClient()
            
            article_content = article_entry.get('content', {})
            title = article_content.get('title', '无标题')
            content = article_content.get('content', '')
            url = article_content.get('url', '')
            db_id = article_entry.get('db_id')
            
            if not content:
                return {'status': 'skipped_empty', 'uploaded': False, 'title': title, 'url': url}
            
            file_name = ragflow_client.build_document_name(title, url, db_id)
            
            # 检查文档是否已存在；默认覆盖重传，避免RAGFlow保留旧正文。
            try:
                existing_docs = ragflow_client.find_documents_by_name(kb_id, file_name)
                if existing_docs and getattr(config, 'RAGFLOW_REUPLOAD_EXISTING', True):
                    doc_ids = [doc.get('id') for doc in existing_docs if doc.get('id')]
                    if doc_ids:
                        ragflow_client.delete_documents(kb_id, doc_ids)
                        print(f"   ♻️  已删除RAGFlow旧文档，准备重传: {title[:30]}...")
                elif existing_docs:
                    print(f"   ⏭️  跳过已存在: {title[:30]}...")
                    return {
                        'status': 'skipped_existing',
                        'uploaded': False,
                        'file_name': file_name,
                        'title': title,
                        'url': url
                    }
            except Exception as check_error:
                print(f"   ⚠️ RAGFlow查重失败，继续尝试上传: {check_error}")
            
            result = ragflow_client.upload_document_content(
                kb_id,
                file_name,
                content,
                auto_parse=getattr(config, 'RAGFLOW_AUTO_PARSE', True),
            )
            document_ids = ragflow_client.extract_document_ids(result)
            parse_result = result.get('parse_result') or {}
            parse_ok = bool(parse_result) and parse_result.get('code') == 0
            sync_status = 'parsed' if parse_ok else 'uploaded'
            if document_ids:
                ragflow_client._record_article_document_mapping(db_id, kb_id, document_ids, file_name, sync_status)
            if document_ids:
                print(f"   📤 上传成功: {title[:30]}... (ID: {document_ids[0]})")
            else:
                print(f"   📤 上传成功: {title[:30]}... (未获取到文档ID)")

            return {
                'status': 'uploaded',
                'uploaded': True,
                'file_name': file_name,
                'document_ids': document_ids,
                'parse_submitted': parse_ok,
                'parse_error': result.get('parse_error', ''),
                'title': title,
                'url': url
            }
                    
        except Exception as e:
            print(f"   ⚠️ 单篇上传异常: {e}")
            return {
                'status': 'failed',
                'uploaded': False,
                'title': article_entry.get('content', {}).get('title', ''),
                'url': article_entry.get('content', {}).get('url', ''),
                'error': str(e)
            }

def main():
    """命令行提示。正式使用请通过 Web UI 或调度任务调用 crawl_news_site。"""
    print("ArticleLinkExtractor uses the Playwright-first crawler. Use crawl_news_site(list_url=...) for manual tests.")

if __name__ == "__main__":
    main()
