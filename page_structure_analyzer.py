"""
智能页面结构分析器（通用版）
在聚合前先分析页面，识别：
1. 主内容区位置
2. 分页类型和方式
3. 日期选择器
4. 文章链接特征
5. 网站类型（新闻、博客、论坛等）

支持各种不同的网站结构

使用的第三方库：
- readability-lxml: Mozilla的文章提取算法
- trafilatura: 强大的网页正文提取
- htmldate: 专门提取发布日期
"""
import asyncio
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

# 第三方库（可选，增强分析能力）
try:
    from readability import Document as ReadabilityDocument
    HAS_READABILITY = True
except ImportError:
    HAS_READABILITY = False

try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False

try:
    import htmldate
    HAS_HTMLDATE = True
except ImportError:
    HAS_HTMLDATE = False

try:
    import justext
    HAS_JUSTEXT = True
except ImportError:
    HAS_JUSTEXT = False

try:
    from boilerpy3 import extractors
    HAS_BOILERPY3 = True
except ImportError:
    HAS_BOILERPY3 = False

try:
    import extruct
    HAS_EXTRUCT = True
except ImportError:
    HAS_EXTRUCT = False

try:
    from selectolax.parser import HTMLParser
    HAS_SELECTOLAX = True
except ImportError:
    HAS_SELECTOLAX = False

# 打印可用库状态
_libs = [
    ('readability-lxml', HAS_READABILITY),
    ('trafilatura', HAS_TRAFILATURA),
    ('htmldate', HAS_HTMLDATE),
    ('justext', HAS_JUSTEXT),
    ('boilerpy3', HAS_BOILERPY3),
    ('extruct', HAS_EXTRUCT),
    ('selectolax', HAS_SELECTOLAX),
]
_available = [name for name, available in _libs if available]
if _available:
    print(f"📚 页面分析库: {', '.join(_available)}")


class PageStructureAnalyzer:
    """通用页面结构分析器"""
    
    def __init__(self):
        self.analysis_result = {}
        
        # 🔥 通用的主内容区选择器（按优先级排序）
        self.main_content_selectors = [
            # ID选择器（最可靠）
            {'selector': '#main-content', 'priority': 10, 'type': 'id'},
            {'selector': '#content', 'priority': 9, 'type': 'id'},
            {'selector': '#main', 'priority': 9, 'type': 'id'},
            {'selector': '#article-list', 'priority': 10, 'type': 'id'},
            {'selector': '#news-list', 'priority': 10, 'type': 'id'},
            {'selector': '#news-listing-wrapper', 'priority': 10, 'type': 'id'},
            {'selector': '#post-list', 'priority': 10, 'type': 'id'},
            
            # Class选择器
            {'selector': '.main-content', 'priority': 8, 'type': 'class'},
            {'selector': '.content', 'priority': 7, 'type': 'class'},
            {'selector': '.article-list', 'priority': 9, 'type': 'class'},
            {'selector': '.news-list', 'priority': 9, 'type': 'class'},
            {'selector': '.post-list', 'priority': 9, 'type': 'class'},
            {'selector': '.story-list', 'priority': 9, 'type': 'class'},
            {'selector': '.feed', 'priority': 8, 'type': 'class'},
            {'selector': '.entries', 'priority': 8, 'type': 'class'},
            {'selector': '.posts', 'priority': 8, 'type': 'class'},
            
            # 语义化标签
            {'selector': 'main', 'priority': 7, 'type': 'tag'},
            {'selector': 'article', 'priority': 6, 'type': 'tag'},
            {'selector': 'section.content', 'priority': 7, 'type': 'combined'},
            {'selector': 'div.container main', 'priority': 7, 'type': 'combined'},
            
            # 通配符选择器（最后尝试）
            {'selector': '[class*="article-list"]', 'priority': 5, 'type': 'wildcard'},
            {'selector': '[class*="news-list"]', 'priority': 5, 'type': 'wildcard'},
            {'selector': '[class*="post-list"]', 'priority': 5, 'type': 'wildcard'},
            {'selector': '[class*="content"]', 'priority': 4, 'type': 'wildcard'},
            {'selector': '[id*="content"]', 'priority': 4, 'type': 'wildcard'},
            {'selector': '[id*="list"]', 'priority': 4, 'type': 'wildcard'},
        ]
        
        # 🔥 需要排除的区域（侧边栏、导航等）
        self.exclude_selectors = [
            'aside', 'nav', 'header', 'footer',
            '.sidebar', '.side-bar', '.aside',
            '.navigation', '.nav', '.menu',
            '.advertisement', '.ad', '.ads',
            '.recommend', '.related', '.hot',
            '.footer', '.header',
            '[class*="sidebar"]', '[class*="side-bar"]',
            '[class*="recommend"]', '[class*="related"]',
            '[class*="advertisement"]', '[class*="widget"]',
        ]
        
        # 🔥 分页相关选择器
        self.pagination_selectors = [
            '.pagination', '.pager', '.page-nav',
            '.page-numbers', '.pages', '.paginator',
            '[class*="pagination"]', '[class*="pager"]',
            'nav[aria-label*="page"]', 'nav[role="navigation"]',
            '.wp-pagenavi',  # WordPress
            '.page-link',  # Bootstrap
        ]
        
        # 🔥 "加载更多"按钮的文本模式
        self.load_more_patterns = [
            '加载更多', '载入更多', '查看更多',
            '展开更多', '显示更多', '加载下一页',
            'Load More', 'Show More', 'View More',
            'Load more', 'Show more', 'View more',
            'See more',
        ]
        
        # 🔥 日期过滤相关的文本模式
        self.date_filter_patterns = [
            '今日', '今天', '昨天', '本周', '本月', '本年',
            '最近', '过去', '时间', '日期',
            'Today', 'Yesterday', 'This Week', 'This Month',
            'Recent', 'Past', 'Date', 'Time',
        ]
    
    async def analyze(self, page, url: str) -> Dict:
        """
        分析页面结构（通用版）
        
        Args:
            page: Playwright页面对象
            url: 页面URL
            
        Returns:
            Dict: 页面结构分析结果
        """
        domain = urlparse(url).netloc.lower()
        
        print(f"\n{'='*60}")
        print(f"🔍 智能页面结构分析（通用版）")
        print(f"{'='*60}")
        print(f"URL: {url}")
        print(f"域名: {domain}")
        
        result = {
            'domain': domain,
            'url': url,
            'site_type': 'unknown',
            'main_content_selector': None,
            'main_content_confidence': 0,
            'article_link_pattern': None,
            'article_link_selector': None,
            'pagination': {
                'type': None,
                'selector': None,
                'total_pages': 1,
                'has_next': False
            },
            'date_filter': {
                'has_date_filter': False,
                'selector': None,
                'type': None
            },
            'article_count': 0,
            'suggestions': [],
            'crawl_strategy': {}
        }
        
        # 0. 获取HTML并使用第三方库分析
        print(f"\n📚 步骤0: 使用第三方库分析...")
        try:
            html_content = await page.content()
            library_analysis = self.analyze_with_libraries(html_content, url)
            result['library_analysis'] = library_analysis
        except Exception as e:
            print(f"   ⚠️ 第三方库分析失败: {e}")
            result['library_analysis'] = {}
        
        # 0.5 检测网站类型
        print(f"\n🌐 步骤0.5: 检测网站类型...")
        site_type = await self._detect_site_type(page, domain)
        result['site_type'] = site_type
        site_type_names = {
            'news': '新闻网站',
            'blog': '博客',
            'forum': '论坛',
            'ecommerce': '电商网站',
            'unknown': '未知类型'
        }
        print(f"   网站类型: {site_type_names.get(site_type, site_type)}")
        
        # 1. 分析主内容区
        print(f"\n📦 步骤1: 分析主内容区...")
        main_content = await self._analyze_main_content(page, domain)
        result['main_content_selector'] = main_content.get('selector')
        result['main_content_confidence'] = main_content.get('confidence', 0)
        result['article_count'] = main_content.get('article_count', 0)
        
        if main_content.get('selector'):
            print(f"   ✅ 主内容区: {main_content['selector']}")
            print(f"   置信度: {main_content.get('confidence', 0)}%")
            print(f"   文章数量: {main_content.get('article_count', 0)}")
        else:
            print(f"   ⚠️ 未识别主内容区，将使用启发式方法")
        
        # 2. 分析分页
        print(f"\n📄 步骤2: 分析分页...")
        pagination = await self._analyze_pagination(page)
        result['pagination'] = pagination
        
        pagination_types = {
            'numbered': '数字分页',
            'load_more': '加载更多',
            'infinite_scroll': '无限滚动',
            'tabs': 'Tab切换',
            None: '无分页'
        }
        print(f"   分页类型: {pagination_types.get(pagination['type'], pagination['type'])}")
        if pagination['total_pages'] > 1:
            print(f"   总页数: {pagination['total_pages']}")
        if pagination.get('has_next'):
            print(f"   有下一页: 是")
        
        # 3. 分析日期选择器
        print(f"\n📅 步骤3: 分析日期选择器...")
        date_filter = await self._analyze_date_filter(page)
        result['date_filter'] = date_filter
        
        if date_filter['has_date_filter']:
            date_types = {
                'dropdown': '下拉框',
                'tabs': 'Tab标签',
                'calendar': '日历组件',
                'range': '时间范围'
            }
            print(f"   ✅ 检测到日期过滤: {date_types.get(date_filter['type'], date_filter['type'])}")
            if date_filter.get('selector'):
                print(f"   选择器: {date_filter['selector']}")
        else:
            print(f"   ❌ 未检测到日期过滤器")
        
        # 4. 分析文章链接特征
        print(f"\n🔗 步骤4: 分析文章链接特征...")
        link_analysis = await self._analyze_article_links(page, domain)
        result['article_link_pattern'] = link_analysis.get('pattern')
        result['article_link_selector'] = link_analysis.get('selector_hint')
        
        if link_analysis.get('pattern'):
            print(f"   链接模式: {link_analysis['pattern']} (发现{link_analysis['count']}个)")
            if link_analysis.get('selector_hint'):
                print(f"   建议选择器: {link_analysis['selector_hint']}")
            if link_analysis.get('patterns_found'):
                print(f"   所有模式: {link_analysis['patterns_found']}")
        else:
            print(f"   ⚠️ 未识别文章链接模式")
        
        # 5. 生成聚合策略
        print(f"\n🎯 步骤5: 生成聚合策略...")
        crawl_strategy = self._generate_crawl_strategy(result)
        result['crawl_strategy'] = crawl_strategy
        result['suggestions'] = crawl_strategy.get('suggestions', [])
        
        for suggestion in result['suggestions']:
            print(f"   • {suggestion}")
        
        print(f"\n{'='*60}")
        print(f"✅ 页面分析完成")
        print(f"{'='*60}\n")
        
        self.analysis_result = result
        return result
    
    def _generate_crawl_strategy(self, analysis: Dict) -> Dict:
        """
        根据分析结果生成聚合策略
        """
        strategy = {
            'use_main_content': False,
            'main_selector': None,
            'pagination_method': 'single_page',
            'date_filter_available': False,
            'suggestions': []
        }
        
        suggestions = []
        
        # 主内容区策略
        if analysis.get('main_content_selector'):
            confidence = analysis.get('main_content_confidence', 0)
            if confidence >= 50:
                strategy['use_main_content'] = True
                strategy['main_selector'] = analysis['main_content_selector']
                suggestions.append(f"✅ 在 {analysis['main_content_selector']} 中提取链接 (置信度{confidence}%)")
            else:
                suggestions.append(f"⚠️ 主内容区识别置信度较低({confidence}%)，可能需要手动验证")
        else:
            suggestions.append("⚠️ 未识别主内容区，将使用全页面提取（可能包含侧边栏内容）")
        
        # 分页策略
        pagination = analysis.get('pagination', {})
        if pagination.get('type') == 'numbered':
            if pagination.get('total_pages', 1) > 1:
                strategy['pagination_method'] = 'numbered'
                suggestions.append(f"📄 检测到{pagination['total_pages']}页，建议逐页聚合")
            elif pagination.get('has_next'):
                strategy['pagination_method'] = 'next_button'
                suggestions.append("📄 检测到下一页按钮，建议翻页聚合")
        elif pagination.get('type') == 'load_more':
            strategy['pagination_method'] = 'load_more'
            suggestions.append("📄 检测到'加载更多'按钮，建议点击加载全部")
        elif pagination.get('type') == 'infinite_scroll':
            strategy['pagination_method'] = 'scroll'
            suggestions.append("📄 检测到无限滚动，建议滚动加载更多")
        elif pagination.get('type') == 'tabs':
            strategy['pagination_method'] = 'tabs'
            suggestions.append("📄 检测到Tab切换，建议遍历所有Tab")
        else:
            suggestions.append("📄 未检测到分页，只聚合当前页")
        
        # 日期过滤策略
        if analysis.get('date_filter', {}).get('has_date_filter'):
            strategy['date_filter_available'] = True
            suggestions.append(f"📅 可用日期过滤器({analysis['date_filter']['type']})筛选日期范围")
        
        # 文章数量提示
        article_count = analysis.get('article_count', 0)
        if article_count > 0:
            suggestions.append(f"📊 预计当前页有 {article_count} 篇文章")
        
        strategy['suggestions'] = suggestions
        return strategy
    
    async def _analyze_main_content(self, page, domain: str) -> Dict:
        """
        分析主内容区（通用版）
        
        策略：
        1. 尝试所有预定义的选择器
        2. 计算每个候选区域的"内容分数"
        3. 选择分数最高的区域
        4. 排除侧边栏等干扰区域
        """
        result = {'selector': None, 'article_count': 0, 'confidence': 0}
        
        best_match = None
        best_score = 0
        
        # 方法1：使用预定义选择器
        for item in self.main_content_selectors:
            try:
                elements = await page.query_selector_all(item['selector'])
                for element in elements:
                    # 检查是否在排除区域内
                    is_excluded = await self._is_in_excluded_area(element)
                    if is_excluded:
                        continue
                    
                    # 计算内容分数
                    score, link_count = await self._calculate_content_score(element, item['priority'])
                    
                    if score > best_score and link_count >= 2:
                        best_score = score
                        best_match = {
                            'selector': item['selector'],
                            'article_count': link_count,
                            'confidence': min(100, score)
                        }
            except:
                continue
        
        # 方法2：如果预定义选择器没找到，使用启发式方法
        if not best_match or best_score < 10:
            heuristic_result = await self._find_main_content_heuristic(page)
            if heuristic_result and heuristic_result.get('score', 0) > best_score:
                best_match = heuristic_result
        
        if best_match:
            result = best_match
        
        return result
    
    async def _is_in_excluded_area(self, element) -> bool:
        """检查元素是否在排除区域内"""
        try:
            # 检查元素自身或父元素是否匹配排除选择器
            is_excluded = await element.evaluate('''(el) => {
                const excludePatterns = [
                    'sidebar', 'side-bar', 'aside', 'nav', 'menu',
                    'footer', 'header', 'advertisement', 'ad-',
                    'recommend', 'related', 'widget', 'hot-'
                ];
                
                let current = el;
                while (current && current !== document.body) {
                    const cls = (current.className || '').toLowerCase();
                    const id = (current.id || '').toLowerCase();
                    const tag = current.tagName.toLowerCase();
                    
                    // 检查标签
                    if (['aside', 'nav', 'footer', 'header'].includes(tag)) {
                        return true;
                    }
                    
                    // 检查class和id
                    for (const pattern of excludePatterns) {
                        if (cls.includes(pattern) || id.includes(pattern)) {
                            return true;
                        }
                    }
                    
                    current = current.parentElement;
                }
                return false;
            }''')
            return is_excluded
        except:
            return False
    
    async def _calculate_content_score(self, element, priority: int) -> Tuple[int, int]:
        """
        计算元素的内容分数
        
        评分标准：
        1. 链接数量（有意义的链接）
        2. 文本密度
        3. 选择器优先级
        4. 元素大小
        """
        try:
            # 获取所有链接
            links = await element.query_selector_all('a[href]')
            
            valid_links = 0
            article_links = 0
            
            for link in links:
                try:
                    text = await link.inner_text()
                    href = await link.get_attribute('href')
                    
                    if not text or not href:
                        continue
                    
                    text = text.strip()
                    
                    # 跳过无意义的链接
                    if len(text) < 5:
                        continue
                    if text.lower() in ['more', '更多', '全文', 'read more', '...']:
                        continue
                    
                    valid_links += 1
                    
                    # 检查是否像文章链接
                    article_patterns = ['/article', '/news', '/post', '/story', '/p/', '/a/', 
                                       '/detail', '/content', '/read', '/view']
                    if any(p in href.lower() for p in article_patterns):
                        article_links += 1
                    # 检查URL是否包含数字ID
                    elif re.search(r'/\d+', href):
                        article_links += 1
                except:
                    continue
            
            # 计算分数
            # 基础分 = 有效链接数 * 优先级
            base_score = valid_links * priority
            
            # 文章链接加分
            if article_links > 0:
                base_score += article_links * 5
            
            # 如果有效链接太少，降低分数
            if valid_links < 3:
                base_score = base_score // 2
            
            return base_score, valid_links
            
        except:
            return 0, 0
    
    async def _find_main_content_heuristic(self, page) -> Optional[Dict]:
        """
        启发式方法查找主内容区
        当预定义选择器都不匹配时使用
        """
        try:
            # 获取页面上所有的div和section
            containers = await page.query_selector_all('div, section')
            
            best_container = None
            best_score = 0
            best_selector = None
            
            for container in containers[:50]:  # 只检查前50个容器
                try:
                    # 检查是否在排除区域
                    is_excluded = await self._is_in_excluded_area(container)
                    if is_excluded:
                        continue
                    
                    # 获取容器的class和id
                    cls = await container.get_attribute('class') or ''
                    cid = await container.get_attribute('id') or ''
                    
                    # 计算分数
                    score, link_count = await self._calculate_content_score(container, 3)
                    
                    if score > best_score and link_count >= 3:
                        best_score = score
                        best_container = container
                        # 生成选择器
                        if cid:
                            best_selector = f'#{cid}'
                        elif cls:
                            first_class = cls.split()[0] if cls else ''
                            best_selector = f'.{first_class}' if first_class else None
                except:
                    continue
            
            if best_container and best_selector:
                return {
                    'selector': best_selector,
                    'article_count': link_count,
                    'confidence': min(80, best_score),  # 启发式方法置信度上限80
                    'score': best_score
                }
        except:
            pass
        
        return None
    
    async def _analyze_pagination(self, page) -> Dict:
        """
        分析分页方式（通用版）
        
        支持的分页类型：
        1. 数字分页（1, 2, 3...）
        2. 上一页/下一页
        3. 加载更多按钮
        4. 无限滚动
        5. Tab切换
        """
        result = {
            'type': None,
            'selector': None,
            'total_pages': 1,
            'has_next': False,
            'has_prev': False
        }
        
        # 方法1：检测数字分页
        for selector in self.pagination_selectors:
            try:
                element = await page.query_selector(selector)
                if element:
                    # 查找页码数字
                    page_numbers = await element.query_selector_all('a, button, span, li')
                    max_page = 1
                    
                    for pn in page_numbers:
                        text = await pn.inner_text()
                        if text:
                            text = text.strip()
                            # 纯数字
                            if text.isdigit():
                                max_page = max(max_page, int(text))
                            # 检测"下一页"
                            elif text in ['下一页', '下一頁', 'Next', '>', '»', '››']:
                                result['has_next'] = True
                            # 检测"上一页"
                            elif text in ['上一页', '上一頁', 'Prev', 'Previous', '<', '«', '‹‹']:
                                result['has_prev'] = True
                    
                    if max_page > 1 or result['has_next']:
                        result['type'] = 'numbered'
                        result['selector'] = selector
                        result['total_pages'] = max_page
                        return result
            except:
                continue
        
        # 方法2：检测"加载更多"按钮
        for pattern in self.load_more_patterns:
            try:
                # 尝试多种选择器
                selectors = [
                    f'button:has-text("{pattern}")',
                    f'a:has-text("{pattern}")',
                    f'div:has-text("{pattern}"):not(:has(*))',  # 叶子节点
                    f'span:has-text("{pattern}")',
                ]
                for sel in selectors:
                    try:
                        button = await page.query_selector(sel)
                        if button:
                            # 确认是可点击的元素
                            is_clickable = await button.evaluate('el => el.offsetWidth > 0 && el.offsetHeight > 0')
                            if is_clickable:
                                result['type'] = 'load_more'
                                result['selector'] = sel
                                return result
                    except:
                        continue
            except:
                continue
        
        # 方法3：检测无限滚动
        try:
            has_infinite = await page.evaluate('''() => {
                // 检查常见的无限滚动标记
                if (document.querySelector('[data-infinite-scroll]') ||
                    document.querySelector('[data-lazy-load]') ||
                    document.querySelector('.infinite-scroll') ||
                    document.querySelector('[class*="infinite"]') ||
                    window.__INFINITE_SCROLL__ ||
                    window.InfiniteScroll) {
                    return true;
                }
                
                // 检查是否有滚动加载的脚本
                const scripts = document.querySelectorAll('script');
                for (const script of scripts) {
                    if (script.src && (script.src.includes('infinite') || script.src.includes('lazy'))) {
                        return true;
                    }
                }
                
                return false;
            }''')
            if has_infinite:
                result['type'] = 'infinite_scroll'
                return result
        except:
            pass
        
        # 方法4：检测Tab切换分页
        try:
            tabs = await page.query_selector_all('[role="tab"], .tab, .nav-tab, [class*="tab"]')
            if len(tabs) >= 2:
                # 检查Tab是否包含日期或类别
                tab_texts = []
                for tab in tabs[:5]:
                    text = await tab.inner_text()
                    if text:
                        tab_texts.append(text.strip())
                
                # 如果Tab看起来像分类
                if any(t for t in tab_texts if len(t) < 20):
                    result['type'] = 'tabs'
                    result['selector'] = '[role="tab"], .tab'
                    result['tab_options'] = tab_texts
                    return result
        except:
            pass
        
        return result
    
    async def _analyze_date_filter(self, page) -> Dict:
        """
        分析日期选择器（通用版）
        
        支持的类型：
        1. 下拉框选择（年/月/日）
        2. 日期Tab/标签
        3. 日历组件
        4. 输入框
        """
        result = {
            'has_date_filter': False,
            'selector': None,
            'type': None,
            'options': []
        }
        
        # 方法1：检测日期下拉框
        try:
            selects = await page.query_selector_all('select')
            for select in selects:
                select_id = await select.get_attribute('id') or ''
                select_name = await select.get_attribute('name') or ''
                select_class = await select.get_attribute('class') or ''
                
                # 检查是否是日期相关的下拉框
                is_date_select = any(kw in (select_id + select_name + select_class).lower() 
                                    for kw in ['year', 'month', 'day', 'date', '年', '月', '日'])
                
                if is_date_select:
                    result['has_date_filter'] = True
                    result['type'] = 'dropdown'
                    result['selector'] = f'#{select_id}' if select_id else None
                    return result
                
                # 检查选项内容
                options = await select.query_selector_all('option')
                for option in options[:5]:
                    text = await option.inner_text()
                    if text and re.search(r'(年|月|日|\d{4}年|\d{4}/\d{2})', text):
                        result['has_date_filter'] = True
                        result['type'] = 'dropdown'
                        return result
        except:
            pass
        
        # 方法2：检测日期Tab/标签
        for pattern in self.date_filter_patterns:
            try:
                selectors = [
                    f'a:has-text("{pattern}")',
                    f'button:has-text("{pattern}")',
                    f'li:has-text("{pattern}")',
                    f'span:has-text("{pattern}")',
                ]
                for sel in selectors:
                    try:
                        element = await page.query_selector(sel)
                        if element:
                            # 确认是可见的
                            is_visible = await element.is_visible()
                            if is_visible:
                                result['has_date_filter'] = True
                                result['type'] = 'tabs'
                                result['selector'] = sel
                                return result
                    except:
                        continue
            except:
                continue
        
        # 方法3：检测日历组件
        try:
            calendar_selectors = [
                '[class*="date-picker"]',
                '[class*="datepicker"]',
                '[class*="calendar"]',
                'input[type="date"]',
                'input[placeholder*="日期"]',
                'input[placeholder*="date"]',
            ]
            for sel in calendar_selectors:
                element = await page.query_selector(sel)
                if element:
                    result['has_date_filter'] = True
                    result['type'] = 'calendar'
                    result['selector'] = sel
                    return result
        except:
            pass
        
        # 方法4：检测时间范围选择器
        try:
            range_selectors = [
                '[class*="time-range"]',
                '[class*="date-range"]',
                '[class*="period"]',
            ]
            for sel in range_selectors:
                element = await page.query_selector(sel)
                if element:
                    result['has_date_filter'] = True
                    result['type'] = 'range'
                    result['selector'] = sel
                    return result
        except:
            pass
        
        return result
    
    async def _analyze_article_links(self, page, domain: str) -> Dict:
        """
        分析文章链接特征（通用版）
        
        返回：
        - 链接模式
        - 文章数量估计
        - 链接选择器建议
        """
        result = {
            'pattern': None,
            'count': 0,
            'selector_hint': None,
            'patterns_found': {}
        }
        
        try:
            # 获取所有链接
            all_links = await page.query_selector_all('a[href]')
            
            # 统计URL模式
            patterns = {}
            pattern_examples = {}
            
            # 常见的文章URL模式
            article_patterns = [
                (r'/article[s]?/', '/article/'),
                (r'/news/', '/news/'),
                (r'/post[s]?/', '/post/'),
                (r'/story/', '/story/'),
                (r'/detail/', '/detail/'),
                (r'/content/', '/content/'),
                (r'/read/', '/read/'),
                (r'/view/', '/view/'),
                (r'/p/', '/p/'),
                (r'/a/', '/a/'),
                (r'/\d{4,}', '/数字ID'),  # 纯数字ID
                (r'\?id=\d+', '?id=数字'),  # Query参数ID
            ]
            
            for link in all_links[:150]:
                try:
                    href = await link.get_attribute('href')
                    text = await link.inner_text()
                    
                    if not href:
                        continue
                    
                    href_lower = href.lower()
                    
                    for regex, pattern_name in article_patterns:
                        if re.search(regex, href_lower):
                            patterns[pattern_name] = patterns.get(pattern_name, 0) + 1
                            if pattern_name not in pattern_examples:
                                pattern_examples[pattern_name] = href
                            break
                except:
                    continue
            
            result['patterns_found'] = patterns
            
            # 找出最常见的模式
            if patterns:
                most_common = max(patterns, key=patterns.get)
                result['pattern'] = most_common
                result['count'] = patterns[most_common]
                
                # 生成选择器提示
                if most_common == '/article/':
                    result['selector_hint'] = 'a[href*="/article"]'
                elif most_common == '/news/':
                    result['selector_hint'] = 'a[href*="/news/"]'
                elif most_common == '/post/':
                    result['selector_hint'] = 'a[href*="/post"]'
                elif most_common == '?id=数字':
                    result['selector_hint'] = 'a[href*="?id="]'
                else:
                    result['selector_hint'] = 'a[href]'
                    
        except Exception as e:
            print(f"      分析链接出错: {e}")
        
        return result
    
    async def _detect_site_type(self, page, domain: str) -> str:
        """
        检测网站类型
        
        类型：
        - news: 新闻网站
        - blog: 博客
        - forum: 论坛
        - ecommerce: 电商
        - corporate: 企业官网
        - unknown: 未知
        """
        try:
            # 检查页面特征
            page_text = await page.inner_text('body')
            page_text_lower = page_text.lower() if page_text else ''
            
            # 新闻网站特征
            news_keywords = ['新闻', '资讯', '头条', '快讯', 'news', 'headline', 'breaking']
            if any(kw in page_text_lower for kw in news_keywords):
                return 'news'
            
            # 博客特征
            blog_keywords = ['博客', 'blog', 'post', '文章', 'author', '作者']
            if any(kw in page_text_lower for kw in blog_keywords):
                return 'blog'
            
            # 论坛特征
            forum_keywords = ['论坛', 'forum', 'bbs', '帖子', 'thread', '回复', 'reply']
            if any(kw in page_text_lower for kw in forum_keywords):
                return 'forum'
            
            # 电商特征
            ecommerce_keywords = ['购物', '商城', 'shop', 'cart', '购买', '价格', 'price']
            if any(kw in page_text_lower for kw in ecommerce_keywords):
                return 'ecommerce'
            
        except:
            pass
        
        return 'unknown'
    
    def analyze_with_libraries(self, html: str, url: str) -> Dict:
        """
        使用第三方库分析HTML内容（增强版）
        
        使用的库：
        - readability-lxml: Mozilla的文章提取算法
        - trafilatura: 强大的网页正文提取
        - htmldate: 专门提取发布日期
        - justext: 基于启发式的正文提取
        - boilerpy3: 样板内容移除（boilerpipe算法）
        - extruct: 提取结构化数据（JSON-LD, Schema.org）
        - selectolax: 超快的CSS选择器
        
        Args:
            html: 页面HTML内容
            url: 页面URL
            
        Returns:
            Dict: 库分析结果
        """
        result = {
            'readability': {},
            'trafilatura': {},
            'htmldate': {},
            'justext': {},
            'boilerpy3': {},
            'extruct': {},
            'structured_data': {},  # JSON-LD, Schema.org 等
            'best_content': None,
            'best_method': None,
            'confidence_score': 0
        }
        
        content_results = []  # 收集所有方法的结果用于比较
        
        # 1. 使用 readability-lxml 分析
        if HAS_READABILITY:
            try:
                doc = ReadabilityDocument(html)
                content = doc.summary() or ''
                result['readability'] = {
                    'title': doc.title(),
                    'short_title': doc.short_title(),
                    'content_length': len(content),
                    'has_content': len(content) > 100
                }
                if len(content) > 100:
                    content_results.append({
                        'method': 'readability',
                        'length': len(content),
                        'score': min(100, 60 + len(content) // 50)
                    })
                print(f"   📚 Readability: {len(content)}字")
            except Exception as e:
                print(f"   ⚠️ Readability失败: {str(e)[:50]}")
        
        # 2. 使用 trafilatura 分析
        if HAS_TRAFILATURA:
            try:
                text = trafilatura.extract(html, include_comments=False, include_tables=False)
                metadata = trafilatura.extract_metadata(html)
                
                result['trafilatura'] = {
                    'content_length': len(text) if text else 0,
                    'has_content': bool(text and len(text) > 100),
                    'title': metadata.title if metadata else None,
                    'author': metadata.author if metadata else None,
                    'date': metadata.date if metadata else None,
                    'sitename': metadata.sitename if metadata else None,
                }
                if text and len(text) > 100:
                    content_results.append({
                        'method': 'trafilatura',
                        'length': len(text),
                        'score': min(100, 70 + len(text) // 30)  # trafilatura通常更准确
                    })
                print(f"   📚 Trafilatura: {len(text) if text else 0}字, 日期={result['trafilatura'].get('date')}")
            except Exception as e:
                print(f"   ⚠️ Trafilatura失败: {str(e)[:50]}")
        
        # 3. 使用 htmldate 提取日期
        if HAS_HTMLDATE:
            try:
                date = htmldate.find_date(html, original_date=True, outputformat='%Y-%m-%d')
                result['htmldate'] = {
                    'date': date,
                    'has_date': bool(date)
                }
                if date:
                    print(f"   📚 htmldate: {date}")
            except Exception as e:
                print(f"   ⚠️ htmldate失败: {str(e)[:50]}")
        
        # 4. 使用 justext 提取正文
        if HAS_JUSTEXT:
            try:
                paragraphs = justext.justext(html.encode('utf-8'), justext.get_stoplist("Chinese"))
                # 只保留非样板内容
                good_paragraphs = [p.text for p in paragraphs if not p.is_boilerplate]
                text = '\n'.join(good_paragraphs)
                
                result['justext'] = {
                    'content_length': len(text),
                    'paragraph_count': len(good_paragraphs),
                    'has_content': len(text) > 100
                }
                if len(text) > 100:
                    content_results.append({
                        'method': 'justext',
                        'length': len(text),
                        'score': min(100, 65 + len(text) // 40)
                    })
                print(f"   📚 justext: {len(text)}字, {len(good_paragraphs)}段")
            except Exception as e:
                print(f"   ⚠️ justext失败: {str(e)[:50]}")
        
        # 5. 使用 boilerpy3 提取正文
        if HAS_BOILERPY3:
            try:
                extractor = extractors.ArticleExtractor()
                text = extractor.get_content(html)
                
                result['boilerpy3'] = {
                    'content_length': len(text) if text else 0,
                    'has_content': bool(text and len(text) > 100)
                }
                if text and len(text) > 100:
                    content_results.append({
                        'method': 'boilerpy3',
                        'length': len(text),
                        'score': min(100, 65 + len(text) // 40)
                    })
                print(f"   📚 boilerpy3: {len(text) if text else 0}字")
            except Exception as e:
                print(f"   ⚠️ boilerpy3失败: {str(e)[:50]}")
        
        # 6. 使用 extruct 提取结构化数据
        if HAS_EXTRUCT:
            try:
                data = extruct.extract(html, base_url=url, syntaxes=['json-ld', 'microdata', 'opengraph'])
                
                structured = {}
                
                # 提取 JSON-LD (Schema.org)
                if data.get('json-ld'):
                    structured['json_ld'] = data['json-ld']
                    # 尝试从 JSON-LD 提取文章信息
                    for item in data['json-ld']:
                        if isinstance(item, dict):
                            item_type = item.get('@type', '')
                            if 'Article' in str(item_type) or 'NewsArticle' in str(item_type):
                                structured['article_schema'] = {
                                    'headline': item.get('headline'),
                                    'author': item.get('author'),
                                    'datePublished': item.get('datePublished'),
                                    'dateModified': item.get('dateModified'),
                                }
                                print(f"   📚 Schema.org: 找到文章结构化数据")
                
                # 提取 OpenGraph
                if data.get('opengraph'):
                    og = data['opengraph'][0] if data['opengraph'] else {}
                    structured['opengraph'] = {
                        'title': og.get('og:title'),
                        'description': og.get('og:description'),
                        'type': og.get('og:type'),
                        'url': og.get('og:url'),
                        'image': og.get('og:image'),
                    }
                    if og.get('og:title'):
                        print(f"   📚 OpenGraph: {og.get('og:title', '')[:30]}...")
                
                result['extruct'] = data
                result['structured_data'] = structured
                
            except Exception as e:
                print(f"   ⚠️ extruct失败: {str(e)[:50]}")
        
        # 7. 使用 selectolax 快速分析DOM结构
        if HAS_SELECTOLAX:
            try:
                tree = HTMLParser(html)
                
                # 快速统计页面结构
                dom_stats = {
                    'total_tags': len(tree.tags('*')),
                    'links': len(tree.tags('a')),
                    'paragraphs': len(tree.tags('p')),
                    'headings': len(tree.tags('h1')) + len(tree.tags('h2')) + len(tree.tags('h3')),
                    'images': len(tree.tags('img')),
                    'forms': len(tree.tags('form')),
                }
                result['dom_stats'] = dom_stats
                print(f"   📚 DOM: {dom_stats['links']}链接, {dom_stats['paragraphs']}段落, {dom_stats['headings']}标题")
            except Exception as e:
                print(f"   ⚠️ selectolax失败: {str(e)[:50]}")
        
        # 8. 选择最佳结果
        if content_results:
            best = max(content_results, key=lambda x: x['score'])
            result['best_method'] = best['method']
            result['confidence_score'] = best['score']
            print(f"   🏆 最佳方法: {best['method']} (分数: {best['score']})")
        
        return result


async def analyze_page_structure(url: str, wait_time: int = 5) -> Dict:
    """
    分析页面结构的便捷函数
    
    Args:
        url: 目标URL
        wait_time: 等待时间（秒）
        
    Returns:
        Dict: 页面结构分析结果
    """
    from playwright.async_api import async_playwright
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            await asyncio.sleep(wait_time)
            
            analyzer = PageStructureAnalyzer()
            result = await analyzer.analyze(page, url)
            
            return result
        except Exception as e:
            print(f"❌ 分析失败: {e}")
            return {'error': str(e)}
        finally:
            await browser.close()


# 测试
if __name__ == '__main__':
    import sys
    
    test_url = sys.argv[1] if len(sys.argv) > 1 else 'https://www.hkej.com/dailynews/headline'
    
    print(f"测试URL: {test_url}")
    result = asyncio.run(analyze_page_structure(test_url))
    
    print("\n" + "=" * 60)
    print("分析结果JSON:")
    print("=" * 60)
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2))
