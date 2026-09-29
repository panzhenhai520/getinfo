#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
智能文章内容提取器
"""

import requests
from bs4 import BeautifulSoup
import re
import json
import time
from urllib.parse import urlparse, urlunparse
from outbound_url_policy import safe_request_get, validate_outbound_url

def _requests_get_with_proxy_intent(url, proxies=None, **kwargs):
    """Use requests.get while treating {} as explicit direct connection."""
    kwargs['proxies'] = proxies
    if isinstance(proxies, dict) and not proxies:
        with requests.Session() as session:
            session.trust_env = False
            return safe_request_get(url, session=session, **kwargs)
    return safe_request_get(url, **kwargs)

# 尝试导入newspaper3k（可选依赖）
try:
    from newspaper import Article
    HAS_NEWSPAPER3K = True
    print("✅ newspaper3k已导入，将提供增强的文章提取功能")
except ImportError as e:
    HAS_NEWSPAPER3K = False
    print(f"⚠️ newspaper3k导入失败: {e}")
    print("📝 将使用基础的BeautifulSoup提取功能")

# 尝试导入trafilatura（更强大的文章提取库）
try:
    import trafilatura
    HAS_TRAFILATURA = True
    print("✅ trafilatura已导入，将提供增强的正文提取功能")
except ImportError:
    HAS_TRAFILATURA = False

# 尝试导入readability（Mozilla的文章提取算法）
try:
    from readability import Document as ReadabilityDocument
    HAS_READABILITY = True
    print("✅ readability-lxml已导入")
except ImportError:
    HAS_READABILITY = False

# 尝试导入htmldate（日期提取专家）
try:
    import htmldate
    HAS_HTMLDATE = True
    print("✅ htmldate已导入")
except ImportError:
    HAS_HTMLDATE = False

# 尝试导入extruct（结构化数据提取：JSON-LD/Microdata/OpenGraph）
try:
    import extruct
    HAS_EXTRUCT = True
    print("✅ extruct已导入")
except ImportError:
    HAS_EXTRUCT = False

# 尝试导入justext（基于启发式的正文提取）
try:
    import justext
    HAS_JUSTEXT = True
    print("✅ justext已导入")
except ImportError:
    HAS_JUSTEXT = False

# 尝试导入boilerpy3（样板内容移除）
try:
    from boilerpy3 import extractors as bp3_extractors
    HAS_BOILERPY3 = True
    print("✅ boilerpy3已导入")
except ImportError:
    HAS_BOILERPY3 = False

# 导入Cloudflare绕过模块
try:
    from cloudflare_bypass import fetch_url, is_cloudflare_protected
    HAS_CLOUDFLARE_BYPASS = True
    print("✅ Cloudflare绕过模块已加载")
except ImportError as e:
    HAS_CLOUDFLARE_BYPASS = False
    print(f"⚠️ Cloudflare绕过模块导入失败: {e}")

# 导入通用URL转换模块
try:
    from url_transformation_rules import transform_url, fix_newspaper3k_url_bug
    print("✅ 已加载通用URL转换规则")
except ImportError as e:
    print(f"⚠️ 无法加载URL转换规则模块: {e}")
    # 提供后备函数
    def transform_url(url, verbose=True):
        return url
    
    def fix_newspaper3k_url_bug(url):
        """修复 URL 双点问题（后备版本）"""
        if not url:
            return url
        parsed = urlparse(url)
        fixed_netloc = re.sub(r'\.{2,}', '.', parsed.netloc)
        fixed_url = urlunparse((parsed.scheme, fixed_netloc, parsed.path, 
                               parsed.params, parsed.query, parsed.fragment))
        return fixed_url
    
def _ensure_html_text(html_content):
    if isinstance(html_content, bytes):
        return html_content.decode('utf-8', errors='ignore')
    return html_content or ''

def _normalize_text_line(text):
    text = re.sub(r'[ \t\f\v\r]+', ' ', text or '')
    text = re.sub(r' *\n *', '\n', text)
    return text.strip()


def _span_value(cell, attr):
    """读取 rowspan/colspan，非法值按 1 处理。"""
    try:
        return max(1, int(str(cell.get(attr) or '1').strip()))
    except (TypeError, ValueError):
        return 1


def _html_table_to_markdown(table):
    """把 <table> 转成 GFM markdown 表格，并展开 rowspan/colspan。

    单元格被逐行压平时，跨行合并单元格（如中标公告里“第一中标候选人”横跨 7 位评委）
    只会出现一次，后续几行就失去归属、无法阅读。这里先把合并单元格补全到它覆盖的每一行，
    再输出 markdown 表格——前端已启用 marked 的 gfm 表格渲染，可直接显示成表格。
    """
    rows = table.find_all('tr')
    if not rows:
        return ''
    grid, spans = [], {}
    for tr in rows:
        cells = tr.find_all(['td', 'th'], recursive=False) or tr.find_all(['td', 'th'])
        row, col = [], 0

        def drain_spans():
            # 把上方 rowspan 延续到本行的合并单元格补进来
            nonlocal col
            while col in spans:
                text, left = spans[col]
                row.append(text)
                if left > 1:
                    spans[col] = (text, left - 1)
                else:
                    del spans[col]
                col += 1

        drain_spans()
        for cell in cells:
            text = cell.get_text(' ', strip=True)
            rowspan, colspan = _span_value(cell, 'rowspan'), _span_value(cell, 'colspan')
            for offset in range(colspan):
                row.append(text)
                if rowspan > 1:
                    spans[col + offset] = (text, rowspan - 1)
            col += colspan
            drain_spans()
        drain_spans()
        if any(value.strip() for value in row):
            grid.append(row)
    if not grid:
        return ''
    width = max(len(row) for row in grid)
    grid = [row + [''] * (width - len(row)) for row in grid]

    def cell_text(value):
        # markdown 表格里竖线要转义，单元格内换行压平
        return re.sub(r'\s+', ' ', str(value or '')).replace('|', '\\|').strip()

    # 首行若是一个跨满整行的标题单元格（colspan 到全宽），当表格标题处理，
    # 真正的表头用下一行，避免标题被重复成一行表头。
    # 注意：补齐宽度会引入空单元格，所以只比较非空值。
    title = ''
    first_nonempty = [value for value in grid[0] if value.strip()] if grid else []
    if len(grid) > 1 and len(first_nonempty) > 1 and len(set(first_nonempty)) == 1:
        title = cell_text(first_nonempty[0])
        grid = grid[1:]

    lines = []
    if title:
        lines.append(f'**{title}**')
    lines.append('| ' + ' | '.join(cell_text(v) for v in grid[0]) + ' |')
    lines.append('| ' + ' | '.join('---' for _ in grid[0]) + ' |')
    for row in grid[1:]:
        lines.append('| ' + ' | '.join(cell_text(v) for v in row) + ' |')
    return '\n'.join(lines)


def _replace_tables_with_markdown(element):
    """把正文里顶层的 <table> 换成含 GFM 表格文本的 <p>，供后续按块提取。"""
    if not element:
        return
    for table in element.find_all('table'):
        if table.find_parent('table') is not None:
            continue                      # 嵌套表格由外层表格的单元格文本一并处理
        markdown = _html_table_to_markdown(table)
        if not markdown:
            table.decompose()
            continue
        wrapper = BeautifulSoup('<p></p>', 'lxml').p
        wrapper.string = markdown         # 走 .string 让特殊字符自动转义
        table.replace_with(wrapper)


def _extract_block_text_preserving_inline(element):
    """Extract block text while keeping inline links inside the original sentence."""
    if not element:
        return ''

    for br in element.find_all('br'):
        br.replace_with('\n')

    # 表格先行转成 markdown，避免 td/th 被当成普通块逐格压平成一堆孤立行
    _replace_tables_with_markdown(element)

    block_tags = ['p', 'li', 'blockquote', 'td', 'th']
    blocks = element.find_all(block_tags)
    lines = []

    if blocks:
        for block in blocks:
            nested_parent = block.find_parent(block_tags)
            if nested_parent and nested_parent is not element:
                continue
            text = _normalize_text_line(block.get_text('', strip=False))
            if text:
                lines.extend(line.strip() for line in text.split('\n') if line.strip())
        joined = '\n'.join(lines)
        # 完整性守卫：正文大量用 div 分段（只有导语是 <p>）时，块级提取会只拿到
        # 开头一小段（曾出现 3456 字符正文只入库 163 字导语）。块级结果明显短于
        # 容器全文时回退到容器全文，保证正文完整。
        raw_full = _normalize_text_line(element.get_text('\n', strip=False))
        if len(joined) < 200 and len(raw_full) > len(joined) * 2:
            return raw_full
        return joined

    return _normalize_text_line(element.get_text('\n', strip=False))

def _repair_inline_newline_splits(text):
    """Repair inline anchor text split by get_text(separator='\\n'), such as 新地(\\n00016\\n)."""
    if not text:
        return ''

    text = re.sub(
        r'([^\n]{1,30})\(\s*\n\s*([A-Za-z0-9.\-]{1,12})\s*\n\s*\)',
        lambda m: f"{m.group(1).rstrip()}({m.group(2)})",
        text
    )
    text = re.sub(
        r'\(\s*\n\s*([A-Za-z0-9.\-]{1,12})\s*\n\s*\)',
        lambda m: f"({m.group(1)})",
        text
    )
    return text

def _looks_truncated_at_start(text):
    if not text:
        return False
    stripped = text.lstrip()
    return bool(re.match(r'^[)）]\S+', stripped))

def _compact_text_for_compare(text):
    return re.sub(r'\s+', '', text or '')

def assess_content_integrity(content, title='', html_content=None, page_text=None):
    """Return generic integrity warnings for extracted article text."""
    issues = []
    content = content or ''
    stripped = content.strip()

    if not stripped:
        return {
            'ok': False,
            'issues': ['empty_content'],
            'reason': 'empty_content',
        }

    if re.match(r'^[)）\]】},，。；;：:！？!?、]+', stripped):
        issues.append('leading_orphan_punctuation')

    first_chunk = stripped[:1200]
    blocking_patterns = [
        (r'请先登录|請先登入|登录后查看|登入後查看|会员登录|會員登入', 'login_required'),
        (r'sign in to continue|login to continue|subscribe to continue', 'login_or_subscribe_required'),
        (r'阅读完整内容需订阅|閱讀完整內容需訂閱|订阅后继续阅读|訂閱後繼續閱讀', 'subscription_required'),
        (r'access denied|forbidden|403 forbidden', 'access_denied'),
        (r'captcha|验证码|驗證碼|人机验证|人機驗證', 'captcha_required'),
        (r'please enable javascript|请启用javascript|請啟用javascript', 'javascript_required'),
        (r'404 not found|page not found|页面不存在|頁面不存在|找不到页面|找不到頁面', 'not_found_page'),
        (r'暂无内容|暫無內容|no content available', 'no_content_page'),
    ]
    title_and_head = f"{title or ''}\n{first_chunk}".lower()
    for pattern, reason in blocking_patterns:
        if re.search(pattern, title_and_head, re.IGNORECASE):
            issues.append(reason)

    rendered_text = page_text
    if rendered_text is None and html_content:
        try:
            rendered_text = BeautifulSoup(_ensure_html_text(html_content), 'lxml').get_text('\n', strip=False)
        except Exception:
            rendered_text = None

    if rendered_text and len(stripped) >= 80:
        rendered_compact = _compact_text_for_compare(rendered_text)
        probes = []
        for line in stripped.splitlines():
            line = re.sub(r'\s+', '', line.strip())
            if len(line) >= 16 and line not in probes:
                probes.append(line)
            if len(probes) >= 5:
                break
        if probes and not any(probe[:80] in rendered_compact for probe in probes):
            issues.append('content_not_found_in_rendered_page')

    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if len(lines) >= 10:
        avg_line_length = sum(len(line) for line in lines) / len(lines)
        short_ratio = sum(1 for line in lines if len(line) <= 12) / len(lines)
        if avg_line_length < 16 and short_ratio > 0.45:
            issues.append('fragmented_or_list_like_text')

    critical_prefixes = {
        'leading_orphan_punctuation',
        'login_required',
        'login_or_subscribe_required',
        'subscription_required',
        'access_denied',
        'captcha_required',
        'javascript_required',
        'not_found_page',
        'no_content_page',
        'content_not_found_in_rendered_page',
    }
    ok = not any(issue in critical_prefixes for issue in issues)
    return {
        'ok': ok,
        'issues': issues,
        'reason': ','.join(issues),
    }

def _iter_structured_dicts(value):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _iter_structured_dicts(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_structured_dicts(item)

def _first_structured_text(item, keys):
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for nested_key in ('@id', 'url', 'name', 'headline'):
                nested = value.get(nested_key)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, str) and entry.strip():
                    return entry.strip()
                if isinstance(entry, dict):
                    for nested_key in ('name', 'headline', 'title', '@id', 'url'):
                        nested_value = entry.get(nested_key)
                        if isinstance(nested_value, str) and nested_value.strip():
                            return nested_value.strip()
                    nested = _first_structured_text(entry, keys)
                    if nested:
                        return nested
    return None

def _normalize_date_string(date_value):
    if not date_value:
        return None
    date_text = str(date_value).strip()
    date_match = re.search(r'(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})', date_text)
    if date_match:
        try:
            from datetime import datetime as dt
            return dt(int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))).strftime('%Y-%m-%d')
        except ValueError:
            return None
    date_match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日?', date_text)
    if date_match:
        try:
            from datetime import datetime as dt
            return dt(int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))).strftime('%Y-%m-%d')
        except ValueError:
            return None
    return None

def _is_generic_article_title(title, url=''):
    if not title:
        return True
    text = re.sub(r'\s+', ' ', str(title)).strip()
    normalized = re.sub(r'[\s|｜\-–—_]+', '', text).lower()
    if not normalized:
        return True

    generic_titles = {
        '首页', '首頁', 'home', 'news', 'article', 'articles',
        '即時新聞', '即时新闻', '財經新聞', '财经新闻',
    }
    compact_generic = {re.sub(r'[\s|｜\-–—_]+', '', item).lower() for item in generic_titles}
    if text in generic_titles or normalized in compact_generic:
        return True

    generic_title_bits = (
        '即時新聞', '即时新闻', '今日新聞', '今日新闻',
        '財經新聞', '财经新闻', '新聞網', '新闻网',
        'news portal', 'latest news',
    )
    if len(text) <= 24 and any(bit.lower() in text.lower() for bit in generic_title_bits):
        return True

    return False

def _first_specific_title(url, *candidates):
    fallback = None
    for candidate in candidates:
        if not candidate:
            continue
        candidate_text = str(candidate).strip()
        if not fallback:
            fallback = candidate_text
        if not _is_generic_article_title(candidate_text, url):
            return clean_title(candidate_text)
    return clean_title(fallback) if fallback else None

def extract_structured_article_metadata(html_content, base_url=''):
    metadata = {
        'title': None,
        'publish_date': None,
        'author': None,
    }
    if not html_content:
        return metadata

    html_text = _ensure_html_text(html_content)

    # Built-in JSON-LD fallback. Some deployments may not have extruct
    # installed even when requirements.txt lists it.
    try:
        soup = BeautifulSoup(html_text, 'lxml')
        for script in soup.select('script[type="application/ld+json"]'):
            json_text = script.string or script.get_text()
            if not json_text:
                continue
            try:
                data = json.loads(json_text)
            except json.JSONDecodeError:
                continue
            for item in _iter_structured_dicts(data):
                if not metadata['title']:
                    title_candidate = _first_structured_text(item, ('headline', 'name', 'title'))
                    if not _is_generic_article_title(title_candidate, base_url):
                        metadata['title'] = title_candidate
                if not metadata['publish_date']:
                    metadata['publish_date'] = _normalize_date_string(
                        _first_structured_text(item, ('datePublished', 'dateCreated', 'dateModified'))
                    )
                if not metadata['author']:
                    metadata['author'] = _first_structured_text(item, ('author', 'creator'))
                if metadata['title'] and metadata['publish_date'] and metadata['author']:
                    return metadata
    except Exception as e:
        print(f"⚠️ JSON-LD结构化数据提取失败: {e}")

    if not HAS_EXTRUCT:
        return metadata

    try:
        data = extruct.extract(
            html_text,
            base_url=base_url,
            syntaxes=['json-ld', 'microdata', 'opengraph'],
            uniform=True,
        )
        for item in _iter_structured_dicts(data):
            if not metadata['title']:
                title_candidate = _first_structured_text(item, ('headline', 'name', 'title', 'og:title'))
                if not _is_generic_article_title(title_candidate, base_url):
                    metadata['title'] = title_candidate
            if not metadata['publish_date']:
                metadata['publish_date'] = _normalize_date_string(
                    _first_structured_text(item, ('datePublished', 'dateCreated', 'dateModified', 'article:published_time'))
                )
            if not metadata['author']:
                metadata['author'] = _first_structured_text(item, ('author', 'creator', 'dc:creator'))
            if metadata['title'] and metadata['publish_date'] and metadata['author']:
                break
    except Exception as e:
        print(f"⚠️ extruct结构化数据提取失败: {e}")
    return metadata

# 活动类日期标签：会议/活动/报名等时间不是文章发布时间，误抽会把旧文变成"未来新文"
_ACTIVITY_DATE_LABELS = (
    '会议时间', '会议日期', '举办时间', '活动时间', '召开时间', '报名时间',
    '展会时间', '开班时间', '培训时间', '大赛时间', '截稿时间', '投稿时间',
    '直播时间', '回放时间', '开始时间', '结束时间', '活动日期', '报名截止',
)


def _is_activity_date_label(text, pos, lookback=14):
    """判断 pos 处的日期是否紧跟在活动类标签之后（会议/报名等，不是发布时间）"""
    if not text or pos <= 0:
        return False
    prefix = text[max(0, pos - lookback):pos]
    return any(label in prefix for label in _ACTIVITY_DATE_LABELS)


def extract_publish_date_from_html(soup, text_content=""):
    """
    智能提取文章发布日期
    优先级：
    1. JSON-LD结构化数据（新闻站常用，可靠度高）
    2. HTML meta标签
    3. 文章正文开头的日期
    4. time标签
    5. 常见日期class
    """
    import datetime
    from datetime import datetime as dt
    import json
    
    # 0. 优先从JSON-LD提取（新闻/内容站常用这种格式）
    json_ld_scripts = soup.select('script[type="application/ld+json"]')
    for script in json_ld_scripts:
        try:
            json_text = script.string
            if json_text:
                data = json.loads(json_text)
                # 处理数组格式
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and 'datePublished' in item:
                            data = item
                            break
                
                # 提取datePublished
                if isinstance(data, dict):
                    date_str = data.get('datePublished', '')
                    if date_str:
                        # 解析ISO格式: 2025-12-04T15:02:00+08:00
                        date_match = re.search(r'(\d{4})-(\d{2})-(\d{2})', date_str)
                        if date_match:
                            result = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"
                            print(f"📅 从JSON-LD提取日期: {result}")
                            return result
        except (json.JSONDecodeError, TypeError):
            pass
    
    # 🔥 0.5 直接用正则从HTML中提取JSON-LD的datePublished（备选方案）
    html_str = str(soup)
    jsonld_match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', html_str)
    if jsonld_match:
        date_str = jsonld_match.group(1)
        date_match = re.search(r'(\d{4})-(\d{2})-(\d{2})', date_str)
        if date_match:
            result = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"
            print(f"📅 从JSON-LD正则提取日期: {result}")
            return result

    # 0.55 使用extruct兜底处理@graph、Microdata、OpenGraph等结构化日期
    structured_meta = extract_structured_article_metadata(html_str)
    if structured_meta.get('publish_date'):
        print(f"📅 从结构化数据提取日期: {structured_meta['publish_date']}")
        return structured_meta['publish_date']
    
    # 0.6 处理相对日期（如"昨日"、"今日"、"前日"等）
    from datetime import timedelta
    relative_date_selectors = ['.date', '.time', '.article-date', '.publish-date', '.post-date']
    for selector in relative_date_selectors:
        date_elem = soup.select_one(selector)
        if date_elem:
            date_text = date_elem.get_text(strip=True)
            today = dt.now()
            
            # 匹配相对日期
            if '今日' in date_text or '今天' in date_text:
                result = today.strftime('%Y-%m-%d')
                print(f"📅 从相对日期提取（今日）: {result}")
                return result
            elif '昨日' in date_text or '昨天' in date_text:
                result = (today - timedelta(days=1)).strftime('%Y-%m-%d')
                print(f"📅 从相对日期提取（昨日）: {result}")
                return result
            elif '前日' in date_text or '前天' in date_text:
                result = (today - timedelta(days=2)).strftime('%Y-%m-%d')
                print(f"📅 从相对日期提取（前日）: {result}")
                return result
            # 匹配 "X天前" 格式
            days_ago_match = re.search(r'(\d+)\s*天前', date_text)
            if days_ago_match:
                days = int(days_ago_match.group(1))
                result = (today - timedelta(days=days)).strftime('%Y-%m-%d')
                print(f"📅 从相对日期提取（{days}天前）: {result}")
                return result
            # 匹配 "X小时前" 格式（当天）
            hours_ago_match = re.search(r'(\d+)\s*小[時时]前', date_text)
            if hours_ago_match:
                result = today.strftime('%Y-%m-%d')
                print(f"📅 从相对日期提取（小时前=今日）: {result}")
                return result
            # 匹配 "X分钟前" 格式（当天）
            mins_ago_match = re.search(r'(\d+)\s*分[鐘钟]前', date_text)
            if mins_ago_match:
                result = today.strftime('%Y-%m-%d')
                print(f"📅 从相对日期提取（分钟前=今日）: {result}")
                return result
    
    # 1. 尝试从meta标签提取
    meta_selectors = [
        ('meta[property="article:published_time"]', 'content'),
        ('meta[name="article:published_time"]', 'content'),
        ('meta[property="og:published_time"]', 'content'),
        ('meta[name="publishdate"]', 'content'),
        ('meta[name="publication_date"]', 'content'),
        ('meta[name="DC.date.issued"]', 'content'),
        ('meta[itemprop="datePublished"]', 'content'),
        ('time[itemprop="datePublished"]', 'datetime'),
        ('time[datetime]', 'datetime'),
    ]
    
    for selector, attr in meta_selectors:
        elements = soup.select(selector)
        for element in elements:
            date_str = element.get(attr, '').strip()
            if date_str:
                try:
                    # 尝试解析ISO格式日期
                    parsed_date = dt.fromisoformat(date_str.replace('Z', '+00:00'))
                    # 只接受合理的日期（2000年之后，不能是未来日期）
                    if 2000 <= parsed_date.year <= dt.now().year:
                        print(f"📅 从meta标签提取日期: {parsed_date.strftime('%Y-%m-%d')}")
                        return parsed_date.strftime('%Y-%m-%d')
                except:
                    pass
    
    # 2. 从文章正文开头提取日期（最可靠的方法）
    # 检查文章开头的前500个字符
    text_to_check = text_content[:500] if text_content else soup.get_text()[:500]
    
    # 多种日期格式
    date_patterns = [
        # 2023 / 11 / 17 格式（方达律所使用的格式）
        (r'(\d{4})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})', lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"),
        # 2023/11/17 格式
        (r'(\d{4})/(\d{1,2})/(\d{1,2})', lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"),
        # 2023.11.17 格式（君合律所使用的格式）
        (r'(\d{4})\.(\d{1,2})\.(\d{1,2})', lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"),
        # 2023-11-17 格式
        (r'(\d{4})-(\d{1,2})-(\d{1,2})', lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"),
        # 2023年11月17日 格式
        (r'(\d{4})年(\d{1,2})月(\d{1,2})日', lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"),
    ]
    
    for pattern, formatter in date_patterns:
        for match in re.finditer(pattern, text_to_check):
            # 🔥 跳过"会议时间/报名时间"等活动类日期，它们不是发布时间
            if _is_activity_date_label(text_to_check, match.start()):
                continue
            try:
                date_str = formatter(match)
                # 验证日期有效性
                parsed_date = dt.strptime(date_str, '%Y-%m-%d')
                # 只接受合理的日期（2000年之后，不能是未来日期）
                if 2000 <= parsed_date.year <= dt.now().year:
                    print(f"📅 从文章正文提取日期: {date_str} (位置: {match.start()})")
                    return date_str
            except ValueError:
                continue
    
    # 🔥 2.5 通用：从页面文本中智能提取日期（适用于所有网站）
    # 扩大搜索范围，从页面前3000字符中提取
    full_text = soup.get_text()[:3000]
    
    # 收集所有匹配的日期
    found_dates = []
    for pattern, formatter in date_patterns:
        for match in re.finditer(pattern, full_text):
            # 🔥 活动类标签（会议时间/报名时间等）后的日期不是发布时间，直接排除
            if _is_activity_date_label(full_text, match.start()):
                continue
            try:
                date_str = formatter(match)
                parsed_date = dt.strptime(date_str, '%Y-%m-%d')
                # 允许到明年（处理跨年情况）
                if 2000 <= parsed_date.year <= dt.now().year + 1:
                    found_dates.append((date_str, parsed_date, match.start()))
            except ValueError:
                continue
    
    if found_dates:
        # 🔥 智能选择最佳日期：
        # 1. 优先选择位置靠前的（通常是发布日期）
        # 2. 在位置靠前的日期中选择最新的
        found_dates.sort(key=lambda x: x[2])  # 按位置排序
        
        # 取前5个日期中最新的（位置靠前的更可能是发布日期）
        top_dates = found_dates[:5]
        top_dates.sort(key=lambda x: x[1], reverse=True)  # 按日期排序，最新的在前
        
        best_date = top_dates[0][0]
        print(f"📅 从页面文本智能提取日期: {best_date} (找到{len(found_dates)}个日期)")
        return best_date
    
    # 3. 从常见的日期class中提取
    date_class_selectors = [
        '.publish-date', '.post-date', '.entry-date', '.article-date',
        '.date', '.time', '.published', '.create-time', '.pub-time',
        '.article-time', '.news-date', '.story-date',
        '.article-info', '.article-meta',  # 🔥 添加更多选择器
    ]
    
    for selector in date_class_selectors:
        elements = soup.select(selector)
        for element in elements:
            date_text = element.get_text(strip=True)
            # 尝试从文本中提取日期
            for pattern, formatter in date_patterns:
                match = re.search(pattern, date_text)
                # 🔥 跳过活动类日期（.article-info 里常带"会议时间"）
                if match and not _is_activity_date_label(date_text, match.start()):
                    try:
                        date_str = formatter(match)
                        parsed_date = dt.strptime(date_str, '%Y-%m-%d')
                        if 2000 <= parsed_date.year <= dt.now().year:
                            print(f"📅 从日期class提取: {date_str}")
                            return date_str
                    except ValueError:
                        continue

    # 4. 最后使用htmldate做全页面兜底，适合日期藏得很深的站点
    if HAS_HTMLDATE:
        try:
            html_date = htmldate.find_date(
                html_str,
                outputformat='%Y-%m-%d',
                extensive_search=True,
                original_date=True,
            )
        except TypeError:
            try:
                html_date = htmldate.find_date(html_str, outputformat='%Y-%m-%d')
            except Exception:
                html_date = None
        except Exception:
            html_date = None

        if html_date:
            try:
                parsed_date = dt.strptime(html_date, '%Y-%m-%d')
                if 2000 <= parsed_date.year <= dt.now().year + 1:
                    print(f"📅 从htmldate提取日期: {html_date}")
                    return html_date
            except ValueError:
                pass
    
    print("⚠️ 未能提取发布日期")
    return None

def extract_with_newspaper3k(url, proxies=None):
    """
    使用newspaper3k提取文章内容（支持Cloudflare绕过和代理）
    
    Args:
        url: 文章URL
        proxies: 代理配置（可选），格式：{'http': 'http://...', 'https': 'http://...'}
    """
    if not HAS_NEWSPAPER3K:
        return {
            'success': False,
            'error': 'newspaper3k未安装',
            'method': 'newspaper3k',
            'url': url
        }
    
    try:
        # 保存原始URL（不经过任何修复处理）
        original_url = url
        
        print(f"🗞️ 使用newspaper3k提取: {url}")
        
        # 创建Article对象（使用默认配置）
        article = Article(url, language='zh')
        force_direct = isinstance(proxies, dict) and not proxies
        
        # 如果提供了代理，配置到article
        if proxies:
            article.config.proxies = proxies
            print(f"   配置代理: {proxies.get('https', proxies.get('http', 'N/A'))}")
        
        # 让newspaper3k直接下载（使用自己的请求机制，通常效果更好）
        print(f"   让newspaper3k直接下载和解析...")
        try:
            if force_direct and HAS_CLOUDFLARE_BYPASS:
                print(f"   🌐 已关闭代理，先用直连抓取HTML再交给newspaper3k解析")
                fetch_result = fetch_url(url, proxies=proxies)
                if not fetch_result['success']:
                    return {
                        'success': False,
                        'error': f'直连获取页面失败: {fetch_result.get("error")}',
                        'method': 'newspaper3k',
                        'url': url
                    }
                article.html = fetch_result['html']
                article.download_state = 2
                print(f"   ✅ 使用 {fetch_result.get('method', 'unknown')} 方法直连获取内容")
            else:
                article.download()
                print(f"   ✅ newspaper3k下载成功")
        except Exception as e:
            # 如果newspaper3k直接下载失败，再尝试使用Cloudflare绕过
            print(f"   ⚠️ newspaper3k直接下载失败: {e}")
            if HAS_CLOUDFLARE_BYPASS:
                try:
                    print(f"   🔄 尝试使用Cloudflare绕过...")
                    fetch_result = fetch_url(url, proxies=proxies)
                    if fetch_result['success']:
                        article.html = fetch_result['html']
                        article.download_state = 2  # 标记为已下载
                        print(f"   ✅ 使用 {fetch_result.get('method', 'unknown')} 方法获取内容")
                    else:
                        return {
                            'success': False,
                            'error': f'所有下载方法都失败: {fetch_result.get("error")}',
                            'method': 'newspaper3k',
                            'url': url
                        }
                except Exception as e2:
                    return {
                        'success': False,
                        'error': f'所有下载方法都失败: {str(e2)}',
                        'method': 'newspaper3k',
                        'url': url
                    }
            else:
                raise
        
        # 解析文章
        article.parse()
        
        # 尝试NLP处理
        try:
            article.nlp()
        except:
            pass  # NLP可能失败，但不影响基本提取
        
        # 检查提取结果
        if not article.text or len(article.text.strip()) < 50:
            return {
                'success': False,
                'error': 'newspaper3k提取的内容太短或为空',
                'method': 'newspaper3k',
                'url': url
            }
        
        # 智能提取发布日期（优先从HTML和正文提取）
        from bs4 import BeautifulSoup
        from datetime import datetime as dt
        soup = BeautifulSoup(article.html, 'lxml')
        publish_date = extract_publish_date_from_html(soup, article.text)
        
        # 如果智能提取失败，才使用newspaper3k的日期
        if not publish_date and article.publish_date:
            publish_date = article.publish_date.isoformat()[:10]  # 只取日期部分
            print(f"📅 使用newspaper3k提取的日期: {publish_date}")
        
        # 🔥 如果还是没有日期，不使用当天日期，保持为None让调用方处理
        if not publish_date:
            print(f"⚠️ 未能提取发布日期")
        
        # 使用原始提取的内容
        cleaned_content = article.text
        
        # 如果内容全是假期通知等无关内容，返回失败让BeautifulSoup尝试
        unwanted_content_markers = [
            r'《信報》印刷版出報日',
            r'印刷版出報日為星期',
            r'除以下公眾假期外',
            r'休刊日[：:]',
            # 🔥 添加免责声明检测
            r'股票及指[數数]資料.*?財經.*?提供',
            r'期貨指[數数]資料.*?天滙財經',
            r'本網站的內容概不構成任何投資意見',
            r'投資者不應只按本網站內容進行投資',
            r'並不保證資料絕對無誤',
        ]
        
        # 检查是否内容主要是无关的
        unwanted_match_count = 0
        for marker in unwanted_content_markers:
            if re.search(marker, cleaned_content, re.IGNORECASE):
                unwanted_match_count += 1
        
        # 🔥 特别检查：如果内容开头就是免责声明，直接判定为失败
        disclaimer_start_patterns = [
            r'^股票及指[數数]資料',
            r'^期貨指[數数]資料',
        ]
        for pattern in disclaimer_start_patterns:
            if re.search(pattern, cleaned_content[:100], re.IGNORECASE):
                print(f"⚠️ 检测到提取内容开头即为免责声明，newspaper3k提取失败")
                return {
                    'success': False,
                    'error': 'newspaper3k提取的内容为免责声明，非正文内容',
                    'method': 'newspaper3k',
                    'url': original_url
                }
        
        # 如果匹配了多个无关标记且内容很短，说明提取失败
        if unwanted_match_count >= 2 and len(cleaned_content) < 500:
            print(f"⚠️ 检测到提取内容主要是网站声明/假期通知，newspaper3k提取失败")
            return {
                'success': False,
                'error': 'newspaper3k提取的内容主要是网站声明，非正文内容',
                'method': 'newspaper3k',
                'url': original_url
            }

        # 计算质量分数（使用清理后的内容）
        score = 60  # 基础分数
        text_length = len(cleaned_content)
        
        if text_length > 1000:
            score += 30
        elif text_length > 500:
            score += 20
        elif text_length > 200:
            score += 10
        
        if article.title and len(article.title) > 10:
            score += 15
        
        if publish_date:
            score += 10
        
        # 如果内容太短，降低分数
        if text_length < 100:
            score -= 30
        
        return {
            'success': True,
            'title': article.title or "无标题",
            'content': cleaned_content,
            'publish_date': publish_date,
            'authors': article.authors if article.authors else [],
            'url': original_url,  # 使用原始URL，不使用newspaper3k可能修改过的URL
            'method': 'newspaper3k',
            'score': min(max(score, 0), 100),
            'content_length': text_length
        }
        
    except Exception as e:
        # 尝试使用original_url，如果不存在则使用url
        final_url = original_url if 'original_url' in locals() else url
        return {
            'success': False,
            'error': f'newspaper3k提取失败: {str(e)}',
            'method': 'newspaper3k',
            'url': final_url
        }


def _trafilatura_readability_candidates(html_content, url, title):
    """newspaper3k 与 BeautifulSoup 都失效时的兜底：trafilatura + readability。

    二者对中文站点（尤其 newspaper3k 中文分句失效、BeautifulSoup 选择器打不到
    正文容器的页面）通常仍能抽出正文。返回与 fallback_candidates 同构的候选列表。
    """
    candidates = []
    if HAS_TRAFILATURA:
        try:
            text = trafilatura.extract(
                html_content, include_comments=False, include_tables=True,
                favor_precision=True,
            )
            if text and len(text.strip()) > 50:
                text = clean_extracted_text(text)
                if text and len(text) > 50:
                    candidates.append({
                        'success': True, 'title': title, 'content': text,
                        'publish_date': '', 'url': url, 'method': 'trafilatura',
                        'score': 70, 'content_length': len(text),
                    })
        except Exception:
            pass
    if HAS_READABILITY:
        try:
            doc = ReadabilityDocument(_ensure_html_text(html_content))
            readable_soup = BeautifulSoup(doc.summary(), 'lxml')
            text = _extract_block_text_preserving_inline(readable_soup)
            text = clean_extracted_text(text)
            if text and len(text) > 50:
                candidates.append({
                    'success': True, 'title': title, 'content': text,
                    'publish_date': '', 'url': url, 'method': 'readability',
                    'score': 65, 'content_length': len(text),
                })
        except Exception:
            pass
    return candidates


def extract_article_content_from_url(url, proxies=None, skip_db_check=False, wait_time=None, timeout=None):
    """
    从URL中智能提取文章内容（增强版本 - 集成newspaper3k和代理支持）
    
    Args:
        url: 文章URL
        proxies: 代理配置（可选），格式：{'http': 'http://...', 'https': 'http://...'}
        skip_db_check: 是否跳过数据库检查（默认False，会检查URL是否已存在）
    """
    # 保存原始URL（不做任何转换，避免破坏URL编码）
    outbound_verdict = validate_outbound_url(url, resolve_dns=True)
    if not outbound_verdict.allowed:
        return {
            'success': False,
            'error': f'URL被安全策略阻止: {outbound_verdict.reason}',
            'url': str(url or ''),
            'method': 'outbound_url_policy',
        }
    original_url = outbound_verdict.normalized_url
    
    # 1. 应用通用URL转换规则（移动版->桌面版等）
    # 注意：不调用fix_newspaper3k_url_bug，因为它会破坏URL编码
    # 🔥 重要：不要转换URL！Playwright会自动处理重定向，我们会使用page.url获取最终URL
    # url = transform_url(url, verbose=True)
    # 保持原始URL，让Playwright打开后获取实际URL
    
    # 3. 快速检查：URL是否已在数据库中（避免重复处理）
    if not skip_db_check:
        try:
            from sqlite_database import sqlite_db
            existing_article = sqlite_db.get_article_by_url(url)
            if existing_article:
                print(f"⏭️  URL已存在数据库，跳过: {url[:80]}...")
                return {
                    'success': False,
                    'error': 'URL已存在于数据库',
                    'method': 'db_check',
                    'url': url,
                    'skip_reason': 'duplicate_url'
                }
        except Exception as e:
            # 数据库检查失败不影响主流程
            pass
    
    print(f"🔍 开始智能提取文章: {url}")
    
    # 如果没有提供代理，尝试从配置读取
    if proxies is None:
        try:
            from config import get_proxies
            proxies = get_proxies()
        except:
            pass  # 配置不可用，继续使用无代理
    
    # Browser automation is a remote-only capability.  The development host
    # must remain functional even when Playwright is not installed.
    try:
        import config as app_config
        browser_enabled = bool(app_config.LOCAL_BROWSER_ENABLED)
    except Exception:
        browser_enabled = False
    try:
        timeout_seconds = max(10, min(int(float(timeout if timeout is not None else 30)), 180))
    except (TypeError, ValueError):
        timeout_seconds = 30
    
    if HAS_CLOUDFLARE_BYPASS and browser_enabled:
        print("🎯 使用 Playwright + 多方法解析（保证最佳准确性）")
        
        # 步骤1：用Playwright获取完整HTML
        from cloudflare_bypass import CloudflareBypass
        import asyncio
        
        # 🔥 获取URL对应的认证Cookie（优先使用最新的有效Cookie）
        auth_cookies = None
        try:
            from urllib.parse import urlparse
            import os
            import json
            import glob
            import time
            
            domain = urlparse(original_url).netloc.lower()
            # 提取根域名 (如 www.example.com -> example.com)
            domain_parts = domain.split('.')
            root_domain = '.'.join(domain_parts[-2:]) if len(domain_parts) >= 2 else domain
            
            # 🔥 方法1：扫描auth_storage目录，找到包含该域名的最新有效Cookie文件
            best_cookie_file = None
            best_mtime = 0
            
            for storage_file in glob.glob('auth_storage/*.json'):
                if '_info.json' in storage_file:
                    continue
                # 检查文件名是否包含域名
                if root_domain in storage_file.lower() or domain in storage_file.lower():
                    mtime = os.path.getmtime(storage_file)
                    if mtime > best_mtime:
                        # 检查Cookie是否有效
                        with open(storage_file, 'r', encoding='utf-8') as f:
                            storage_data = json.load(f)
                            cookies = storage_data.get('cookies', [])
                            if cookies:
                                # 检查关键Cookie是否过期
                                now = time.time()
                                has_valid_key_cookie = False
                                for c in cookies:
                                    if c.get('name') in ['ej_mid', 'ej_ssr', 'PHPSESSID']:
                                        expires = c.get('expires', -1)
                                        if expires < 0 or expires > now:
                                            has_valid_key_cookie = True
                                            break
                                if has_valid_key_cookie:
                                    best_cookie_file = storage_file
                                    best_mtime = mtime
            
            if best_cookie_file:
                with open(best_cookie_file, 'r', encoding='utf-8') as f:
                    storage_data = json.load(f)
                    auth_cookies = storage_data.get('cookies', [])
                    print(f"🔐 找到有效Cookie文件: {best_cookie_file}，加载 {len(auth_cookies)} 个Cookie")
            else:
                print(f"⚠️ 未找到 {root_domain} 的有效Cookie文件")
                
        except Exception as e:
            print(f"⚠️ 获取认证配置失败: {e}")
        
        try:
            wait_seconds = max(1, min(int(float(wait_time if wait_time is not None else 6)), 60))
        except (TypeError, ValueError):
            wait_seconds = 6
        async def fetch_with_playwright():
            bypass = CloudflareBypass(proxies=proxies)
            # Use caller-provided render wait so detail pages honor the same runtime config as list pages.
            return await bypass.bypass_with_playwright(
                original_url,
                wait_time=wait_seconds,
                timeout=timeout_seconds,
                cookies=auth_cookies
            )
        
        # 🔥 检测是否已经在事件循环中运行
        try:
            loop = asyncio.get_running_loop()
            # 如果已经在事件循环中，使用nest_asyncio或创建新线程
            try:
                import nest_asyncio
                nest_asyncio.apply()
                fetch_result = asyncio.run(fetch_with_playwright())
            except ImportError:
                # 如果没有nest_asyncio，使用线程池
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(lambda: asyncio.run(fetch_with_playwright()))
                    fetch_result = future.result(timeout=max(120, timeout_seconds + wait_seconds + 30))
        except RuntimeError:
            # 没有运行中的事件循环，直接使用asyncio.run
            fetch_result = asyncio.run(fetch_with_playwright())
        
        if not fetch_result['success']:
            return {
                'success': False,
                'error': f"获取页面失败: {fetch_result.get('error')}",
                'url': original_url
            }
        
        html_content = fetch_result['html']
        # 🔥 使用Playwright实际访问后的URL（可能经过重定向）
        final_url = fetch_result.get('url', original_url)
        if final_url != original_url:
            print(f"🔄 URL已重定向: {original_url} → {final_url}")
        structured_metadata = extract_structured_article_metadata(html_content, final_url)
        if structured_metadata.get('title') or structured_metadata.get('publish_date'):
            print(
                "   ✅ 结构化元数据: "
                f"title={bool(structured_metadata.get('title'))}, "
                f"date={structured_metadata.get('publish_date') or 'None'}"
            )
        
        # 步骤2：用多种方法解析这个完整HTML，选择最佳结果
        results = []
        
        # 通用高置信正文选择器。很多新闻/博客/CMS都会把正文放在这些语义容器里；
        # 段内链接不拆行，避免股票代码、脚注、作者链接把一句话切碎。
        precision_selectors = [
            '[itemprop="articleBody"]',
            '[property="articleBody"]',
            'article [itemprop="articleBody"]',
            'article .article-content',
            'article .article-body',
            'article .entry-content',
            'article .post-content',
            '#article-content',
            '#article-body',
            '#article-text',
            '.article-content',
            '.article-body',
            '.article-text',
            '.entry-content',
            '.post-content',
            '.story-content',
            '.news-content',
            '.detail-content',
            '.content-body',
        ]

        try:
            soup = BeautifulSoup(html_content, 'lxml')
            title = _first_specific_title(original_url, structured_metadata.get('title'), extract_article_title(soup, original_url))
            selector_publish_date = structured_metadata.get('publish_date') or extract_publish_date_from_html(soup, '')

            for tag in soup.select('nav, .nav, .menu, .sidebar, .related, .recommend, .ad, .advertisement, style, .share, .social'):
                tag.decompose()
            for tag in soup.select('script'):
                if tag.get('type') != 'application/ld+json':
                    tag.decompose()

            seen_selector_texts = set()
            for selector in precision_selectors:
                for element in soup.select(selector):
                    if not element:
                        continue
                    for tag in element.select('.related-article, .more-link, .recommend, .share, .social'):
                        tag.decompose()

                    text = _extract_block_text_preserving_inline(element)
                    text = clean_extracted_text(text) if text else ""
                    text = text.strip()
                    if not text or len(text) <= 50:
                        continue
                    if _looks_truncated_at_start(text):
                        print(f"   ⚠️ 通用精确选择器疑似截断正文开头: {selector}，跳过该候选")
                        continue

                    text_key = re.sub(r'\s+', '', text[:300])
                    if text_key in seen_selector_texts:
                        continue
                    seen_selector_texts.add(text_key)

                    selector_score = calculate_content_score(element)
                    if selector_score < 45 and len(text) < 120:
                        continue
                    score = min(96, max(82, selector_score + min(12, len(text) // 200)))
                    results.append({
                        'method': f'precision_selector:{selector}',
                        'content': text,
                        'title': title,
                        'score': score,
                        'author': structured_metadata.get('author'),
                        'date': selector_publish_date or extract_publish_date_from_html(soup, text),
                    })
                    print(f"   ✅ 通用精确选择器: {selector}，{len(text)}字，分数{score}")
        except Exception as selector_error:
            print(f"   ⚠️ 通用精确选择器解析失败，继续多解析器: {selector_error}")
        
        # 方法A: 用newspaper3k解析完整HTML（🔥 优先级最高，准确度好）
        if HAS_NEWSPAPER3K:
            try:
                from newspaper import Article
                article = Article(original_url, language='zh')
                article.download_state = 2
                article.html = html_content.encode('utf-8') if isinstance(html_content, str) else html_content
                article.parse()
                
                content = article.text.strip()
                # 清理newspaper3k提取的内容
                content = clean_extracted_text(content)
                
                if content and len(content) > 50 and '股票及指數資料' not in content[:100]:
                    # 🔥 提高newspaper3k的基础分数（因为它对很多网站效果很好）
                    base_score = 80  # 基础分更高
                    length_bonus = min(20, len(content) // 50)  # 长度加分
                    score = min(100, base_score + length_bonus)
                    
                    soup_for_title = BeautifulSoup(html_content, 'lxml')
                    extracted_title = extract_article_title(soup_for_title, original_url)
                    np_title = article.title or ''
                    final_title = _first_specific_title(original_url, np_title, structured_metadata.get('title'), extracted_title)
                    
                    results.append({
                        'method': 'newspaper3k_with_playwright',
                        'content': content,
                        'title': final_title,
                        'score': score,
                        'author': ', '.join(article.authors) if article.authors else structured_metadata.get('author'),
                        'date': article.publish_date.strftime('%Y-%m-%d') if article.publish_date else structured_metadata.get('publish_date')
                    })
                    print(f"   ✅ newspaper3k解析: {len(content)}字，分数{score}")
                else:
                    print(f"   ❌ newspaper3k解析: 内容无效或包含噪音")
            except Exception as e:
                print(f"   ❌ newspaper3k解析失败: {e}")
        
        # 方法A2: 用trafilatura提取（更强大的正文提取）
        if HAS_TRAFILATURA:
            try:
                text = trafilatura.extract(
                    html_content, 
                    include_comments=False, 
                    include_tables=True,
                    favor_precision=True  # 优先精确性
                )
                if text and len(text) > 50:
                    # 提取元数据
                    metadata = trafilatura.extract_metadata(html_content)
                    title = metadata.title if metadata else None
                    extracted_title = extract_article_title(BeautifulSoup(html_content, 'lxml'), original_url)
                    
                    # trafilatura 通常提取很干净，给高分
                    score = min(100, 75 + len(text) // 20)
                    results.append({
                        'method': 'trafilatura',
                        'content': text,
                        'title': _first_specific_title(original_url, title, structured_metadata.get('title'), extracted_title),
                        'score': score,
                        'author': metadata.author if metadata and metadata.author else structured_metadata.get('author'),
                        'date': _normalize_date_string(metadata.date) if metadata and metadata.date else structured_metadata.get('publish_date')
                    })
                    print(f"   ✅ trafilatura解析: {len(text)}字，分数{score}")
                else:
                    print(f"   ❌ trafilatura解析: 内容太短或为空")
            except Exception as e:
                print(f"   ❌ trafilatura解析失败: {e}")

            try:
                recall_text = trafilatura.extract(
                    html_content,
                    include_comments=False,
                    include_tables=True,
                    favor_recall=True
                )
                if recall_text and len(recall_text) > 50:
                    recall_text = clean_extracted_text(recall_text)
                    if recall_text and len(recall_text) > 50:
                        recall_score = min(95, 68 + len(recall_text) // 25)
                        metadata = trafilatura.extract_metadata(html_content)
                        extracted_title = extract_article_title(BeautifulSoup(html_content, 'lxml'), original_url)
                        results.append({
                            'method': 'trafilatura_recall',
                            'content': recall_text,
                            'title': _first_specific_title(original_url, (metadata.title if metadata and metadata.title else None), structured_metadata.get('title'), extracted_title),
                            'score': recall_score,
                            'author': metadata.author if metadata and metadata.author else structured_metadata.get('author'),
                            'date': _normalize_date_string(metadata.date) if metadata and metadata.date else structured_metadata.get('publish_date')
                        })
                        print(f"   ✅ trafilatura召回解析: {len(recall_text)}字，分数{recall_score}")
                else:
                    print(f"   ❌ trafilatura召回解析: 内容太短或为空")
            except Exception as e:
                print(f"   ❌ trafilatura召回解析失败: {e}")

        # 方法A2b: 用readability-lxml提取（阅读视图算法）
        if HAS_READABILITY:
            try:
                doc = ReadabilityDocument(_ensure_html_text(html_content))
                summary_html = doc.summary()
                readable_soup = BeautifulSoup(summary_html, 'lxml')
                text = _extract_block_text_preserving_inline(readable_soup)
                text = clean_extracted_text(text)
                if text and len(text) > 50:
                    score = min(95, 70 + len(text) // 25)
                    extracted_title = extract_article_title(BeautifulSoup(html_content, 'lxml'), original_url)
                    results.append({
                        'method': 'readability',
                        'content': text,
                        'title': _first_specific_title(original_url, doc.short_title(), structured_metadata.get('title'), extracted_title),
                        'score': score,
                        'author': structured_metadata.get('author'),
                        'date': structured_metadata.get('publish_date')
                    })
                    print(f"   ✅ readability解析: {len(text)}字，分数{score}")
                else:
                    print(f"   ❌ readability解析: 内容太短或为空")
            except Exception as e:
                print(f"   ❌ readability解析失败: {e}")
        
        # 方法A3: 用justext提取（基于启发式）
        if HAS_JUSTEXT:
            try:
                # justext 没有中文停用词表，使用空列表（只基于启发式）
                try:
                    stoplist = justext.get_stoplist("Chinese")
                except:
                    stoplist = frozenset()  # 使用空停用词表
                
                paragraphs = justext.justext(html_content.encode('utf-8'), stoplist)
                good_paragraphs = [p.text for p in paragraphs if not p.is_boilerplate]
                text = '\n'.join(good_paragraphs)
                
                if text and len(text) > 50:
                    score = min(100, 65 + len(text) // 30)
                    results.append({
                        'method': 'justext',
                        'content': text,
                        'title': structured_metadata.get('title') or extract_article_title(BeautifulSoup(html_content, 'lxml'), original_url),
                        'score': score
                    })
                    print(f"   ✅ justext解析: {len(text)}字，{len(good_paragraphs)}段，分数{score}")
            except Exception as e:
                print(f"   ❌ justext解析失败: {e}")
        
        # 方法A4: 用boilerpy3提取（boilerpipe算法）
        if HAS_BOILERPY3:
            try:
                extractor = bp3_extractors.ArticleExtractor()
                text = extractor.get_content(html_content)
                
                if text and len(text) > 50:
                    score = min(100, 65 + len(text) // 30)
                    results.append({
                        'method': 'boilerpy3',
                        'content': text,
                        'title': structured_metadata.get('title') or extract_article_title(BeautifulSoup(html_content, 'lxml'), original_url),
                        'score': score
                    })
                    print(f"   ✅ boilerpy3解析: {len(text)}字，分数{score}")
            except Exception as e:
                print(f"   ❌ boilerpy3解析失败: {e}")
        
        # 方法B: 用BeautifulSoup智能提取
        try:
            soup = BeautifulSoup(html_content, 'lxml')
            title = _first_specific_title(original_url, structured_metadata.get('title'), extract_article_title(soup, original_url))
            
            # 移除干扰元素
            for element in soup(["script", "style", "nav", "header", "footer", "aside", "menu", "sidebar"]):
                element.decompose()
            
            # 移除免责声明
            unwanted_patterns = [
                r'股票及指[數数]資料',
                r'本網站的內容概不構成任何投資意見',
            ]
            for element in soup.find_all(['div', 'section', 'p', 'span']):
                element_text = element.get_text(strip=True)
                if any(re.search(p, element_text) for p in unwanted_patterns):
                    if len(element_text) < 500:
                        element.decompose()
            
            # 智能提取内容（通用方法，特定网站已在前面处理）
            article_content = None
            best_score = 0
            
            content_selectors = [
                'article', 'main', '.article-content', '.post-content', 
                '.entry-content', '.content', '.article-body'
            ]
            
            for selector in content_selectors:
                elements = soup.select(selector)
                for element in elements:
                    score = calculate_content_score(element)
                    if score > best_score:
                        best_score = score
                        article_content = element
            
            if not article_content or best_score < 50:
                article_content = find_best_content_heuristic(soup)
                if article_content:
                    best_score = calculate_content_score(article_content)
            
            if article_content and best_score >= 50:
                clean_content_element(article_content)
                text_content = _extract_block_text_preserving_inline(article_content)
                text_content = clean_extracted_text(text_content)
                
                if text_content and len(text_content) > 50:
                    results.append({
                        'method': 'beautifulsoup_smart',
                        'content': text_content,
                        'title': title,
                        'score': best_score
                    })
                    print(f"   ✅ BeautifulSoup智能提取: {len(text_content)}字，分数{best_score}")
                else:
                    print(f"   ❌ BeautifulSoup提取: 内容太短")
            else:
                print(f"   ❌ BeautifulSoup提取: 分数不足({best_score})")
                
        except Exception as e:
            print(f"   ❌ BeautifulSoup解析失败: {e}")
        
        # 步骤3：智能选择最佳结果（考虑内容质量，不只是长度）
        if results:
            page_text = ''
            try:
                page_text = BeautifulSoup(html_content, 'lxml').get_text('\n', strip=False)
            except Exception:
                page_text = ''

            # 🔥 重新评估每个结果的质量分数
            valid_results = []
            integrity_failures = []
            for r in results:
                r['content'] = clean_extracted_text(r.get('content', ''))
                integrity = assess_content_integrity(
                    r.get('content', ''),
                    title=r.get('title', ''),
                    html_content=html_content,
                    page_text=page_text,
                )
                r['integrity'] = integrity
                if not integrity.get('ok'):
                    failure = {
                        'method': r.get('method'),
                        'issues': integrity.get('issues', []),
                        'content_length': len(r.get('content', '')),
                    }
                    integrity_failures.append(failure)
                    print(f"   ⚠️ {r.get('method')} 正文完整性失败: {integrity.get('reason')}")
                    continue

                quality_score = evaluate_content_quality(r['content'])
                r['quality_score'] = quality_score
                # 综合分数 = 基础分数 * 质量系数
                r['final_score'] = r['score'] * (quality_score / 100)
                valid_results.append(r)

            if not valid_results:
                return {
                    'success': False,
                    'error': '所有候选正文均未通过完整性校验',
                    'url': final_url,
                    'method': 'integrity_gate',
                    'integrity_failures': integrity_failures,
                }
            
            # 按综合分数排序
            valid_results.sort(key=lambda x: x['final_score'], reverse=True)
            best_result = valid_results[0]
            
            print(f"\n📊 各方法评分:")
            for r in valid_results[:5]:
                print(f"   {r['method']}: 基础{r['score']} × 质量{r['quality_score']}% = {r['final_score']:.0f}")
            print(f"\n🏆 选择最佳结果: {best_result['method']}，综合分数: {best_result['final_score']:.0f}")
            
            # 提取日期（🔥必须100%提取到）
            soup = BeautifulSoup(html_content, 'lxml')
            publish_date = (
                _normalize_date_string(best_result.get('date'))
                or structured_metadata.get('publish_date')
                or extract_publish_date_from_html(soup, best_result['content'])
            )
            
            # 🔥 如果提取不到日期，不使用当天日期，保持为None
            if not publish_date:
                print(f"⚠️ 未能提取发布日期")
            
            return {
                'success': True,
                'title': best_result['title'],
                'content': best_result['content'],
                'publish_date': publish_date,
                'url': final_url,  # 🔥 使用实际访问的URL
                'method': best_result['method'],
                'score': int(best_result.get('final_score', best_result.get('score', 0))),
                'base_score': best_result.get('score'),
                'quality_score': best_result.get('quality_score'),
                'integrity': best_result.get('integrity'),
                'authors': [best_result.get('author')] if best_result.get('author') else ([structured_metadata.get('author')] if structured_metadata.get('author') else []),
                'content_length': len(best_result['content'])
            }
        else:
            return {
                'success': False,
                'error': '所有解析方法都未能提取有效内容',
                'url': final_url  # 🔥 使用实际访问的URL
            }
    
    # 普通网站降级流程：多方法都作为候选，最后按质量分选择。
    newspaper_result = None
    fallback_candidates = []
    
    # 方法1: 优先使用newspaper3k（如果可用）
    if HAS_NEWSPAPER3K:
        # 使用原始URL，不经过transform_url转换，避免URL被破坏
        newspaper_result = extract_with_newspaper3k(original_url, proxies=proxies)
        if newspaper_result['success']:
            print(f"✅ newspaper3k提取成功，作为候选结果，质量分数: {newspaper_result.get('score', 0)}")
            fallback_candidates.append(newspaper_result)
        else:
            print(f"⚠️ newspaper3k提取失败: {newspaper_result.get('error', '未知错误')}")
    
    # 方法2: 使用BeautifulSoup方法（原有逻辑）
    print("🍜 使用BeautifulSoup方法提取...")
    try:
        # 使用Cloudflare绕过获取页面内容
        if HAS_CLOUDFLARE_BYPASS and browser_enabled:
            print("🎭 使用Playwright获取页面内容...")
            fetch_result = fetch_url(original_url, proxies=proxies)
            
            if not fetch_result['success']:
                return {
                    'success': False,
                    'error': f"获取页面失败: {fetch_result.get('error')}",
                    'url': original_url
                }
            html_content = fetch_result['html']
            # 🔥 使用Playwright实际访问后的URL（可能经过重定向）
            final_url = fetch_result.get('url', original_url)
            if final_url != original_url:
                print(f"🔄 URL已重定向: {original_url} → {final_url}")
                url = final_url  # 更新url变量，后续保存时使用
        else:
            # 降级：使用requests
            # UA 升级到较新 Chrome：部分站点（如 mysteel）按 UA 版本反爬，
            # Chrome/91 会被返回 15KB 壳页（无正文），Chrome/124+ 才返回完整正文。
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1'
            }
            response = _requests_get_with_proxy_intent(
                original_url,
                headers=headers,
                proxies=proxies,
                timeout=max(15, timeout_seconds),
                verify=False,
            )
            response.raise_for_status()
            html_content = response.content
        
        # 解析HTML
        soup = BeautifulSoup(html_content, 'lxml')
        
        # 提取标题（多种方法）
        title = extract_article_title(soup, url)
        
        # 移除不需要的元素
        for element in soup(["script", "style", "nav", "header", "footer", "aside", "menu", "sidebar"]):
            element.decompose()
        
        # 移除广告和无关内容
        for element in soup.find_all(['div', 'section'], class_=re.compile(r'(ad|advertisement|banner|popup|modal|overlay|cookie|consent)', re.I)):
            element.decompose()
        
        # 移除包含特定无关内容的元素（如网站声明、假期通知等）
        unwanted_text_patterns = [
            r'印刷版出報日',
            r'休刊日',
            r'假期安排',
            r'暫停更新',
            r'服務條款',
            r'隱私政策',
            r'Cookie Policy',
            r'版權所有',
            r'All Rights Reserved',
            # 财经/行情类免责声明过滤
            r'股票及指[數数]資料.*?財經.*?提供',
            r'期貨指[數数]資料.*?天滙財經',
            r'本網站的內容概不構成任何投資意見',
            r'投資者不應只按本網站內容進行投資',
            r'並不保證資料絕對無誤',
            r'資料如有錯漏而令閣下蒙受損失.*?概不負責'
        ]
        
        for element in soup.find_all(['div', 'section', 'p', 'span']):
            element_text = element.get_text(strip=True)
            if element_text:
                for pattern in unwanted_text_patterns:
                    if re.search(pattern, element_text, re.IGNORECASE):
                        # 如果这个元素的文本主要是这些无关内容，就移除
                        if len(element_text) < 500:  # 短文本更可能是声明
                            element.decompose()
                            break
        
        # 尝试多种方法提取文章内容
        content_selectors = [
            # 语义化标签
            'article',
            'main',
            # 常见的内容选择器
            '.article-content', '.post-content', '.entry-content', '.content',
            '.main-content', '.article-body', '.post-body', '.entry-body',
            '.story-content', '.news-content', '.article-text', '.post-text',
            '.detail-content', '.news-detail', '.article-detail',
            '.page-content', '.single-content', '.post-single',
            # 通用容器
            '.container .content', '.wrapper .content', '.main .content',
            '#content', '#main-content', '#article-content',
            # 特定网站的选择器
            '.article', '.post', '.entry', '.single', '.page',
            # 更宽泛的选择器
            '.main', '#main', '.container', '.wrapper'
        ]
        
        article_content = None
        best_score = 0
        
        for selector in content_selectors:
            elements = soup.select(selector)
            for element in elements:
                # 计算内容质量分数
                score = calculate_content_score(element)
                if score > best_score:
                    best_score = score
                    article_content = element
        
        print(f"🔍 选择器扫描完成，最佳分数: {best_score}")
        if article_content:
            preview = article_content.get_text(strip=True)[:100]
            print(f"📝 当前最佳内容预览: {preview}...")
        
        # 如果没有找到合适的内容，使用启发式方法
        if not article_content or best_score < 50:
            print(f"⚠️ 分数不足50，使用启发式方法...")
            heuristic_result = find_best_content_heuristic(soup)
            if heuristic_result:
                heuristic_score = calculate_content_score(heuristic_result)
                print(f"🎯 启发式方法找到内容，分数: {heuristic_score}")
                if heuristic_score > best_score:
                    article_content = heuristic_result
                    best_score = heuristic_score
        
        if article_content:
            # 清理内容
            clean_content_element(article_content)
            
            # 提取文本内容
            text_content = _extract_block_text_preserving_inline(article_content)
            
            # 进一步清理文本
            text_content = clean_extracted_text(text_content)
            
            # 智能提取发布日期
            publish_date = extract_publish_date_from_html(soup, text_content)
            
            beautifulsoup_result = {
                'success': True,
                'title': title,
                'content': text_content,
                'publish_date': publish_date,
                'url': url,
                'method': 'universal_smart_extraction',
                'score': best_score,
                'content_length': len(text_content)
            }
            
            print(f"✅ BeautifulSoup提取完成，质量分数: {best_score}")
            fallback_candidates.append(beautifulsoup_result)
            
            for candidate in fallback_candidates:
                quality_score = evaluate_content_quality(candidate.get('content', ''))
                candidate['quality_score'] = quality_score
                candidate['final_score'] = candidate.get('score', 0) * (quality_score / 100)
            
            fallback_candidates.sort(key=lambda item: item.get('final_score', 0), reverse=True)
            best_candidate = fallback_candidates[0]
            print("📊 降级候选评分:")
            for candidate in fallback_candidates:
                print(f"   {candidate.get('method')}: 基础{candidate.get('score', 0)} × 质量{candidate.get('quality_score', 0)}% = {candidate.get('final_score', 0):.0f}")
            print(f"🏆 选择降级最佳结果: {best_candidate.get('method')}")
            return best_candidate
        else:
            # 改进2：newspaper3k + BeautifulSoup 选择器都失效时，用 trafilatura/readability
            # 兜底（对中文站点通常更稳，避免正文明明在 HTML 里却抽不出来）。
            print(f"[debug] else分支 html_content type={type(html_content).__name__} len={len(html_content) if html_content else 0}", flush=True)
            try:
                extra = _trafilatura_readability_candidates(html_content, url, title)
                print(f"[debug] trafilatura/readability 候选数 = {len(extra)}", flush=True)
                fallback_candidates.extend(extra)
            except Exception as _e:
                print(f"[debug] trafilatura兜底异常: {_e}", flush=True)
            if fallback_candidates:
                print("⚠️ BeautifulSoup未找到更好内容，使用已有候选结果")
                fallback_candidates.sort(key=lambda item: item.get('score', 0), reverse=True)
                return fallback_candidates[0]
            return {
                'success': False,
                'error': '所有提取方法都无法找到有效的文章内容',
                'url': url
            }
            
    except Exception as e:
        # 改进2：BeautifulSoup 方法抛异常时，若已拿到 html 仍可试 trafilatura/readability
        try:
            if "html_content" in locals() and html_content:
                fallback_candidates.extend(
                    _trafilatura_readability_candidates(html_content, url, title)
                )
        except Exception:
            pass
        if fallback_candidates:
            print(f"⚠️ BeautifulSoup方法异常，使用已有候选结果: {e}")
            fallback_candidates.sort(key=lambda item: item.get('score', 0), reverse=True)
            return fallback_candidates[0]
        return {
            'success': False,
            'error': f'所有提取方法都失败: {str(e)}',
            'url': url
        }

def extract_article_title(soup, url):
    """
    智能提取文章标题
    """
    # 🔥 通用：从URL中提取域名，用于排除网站名称
    from urllib.parse import urlparse
    try:
        domain = urlparse(url).netloc.lower()
        domain_parts = domain.replace('www.', '').split('.')
        domain_name = domain_parts[0] if domain_parts else ''
    except:
        domain_name = ''
    
    # 🔥 通用排除词（网站名称、导航词等）
    common_exclude = [
        '首页', '首頁', 'home', '登入', '登录', '註冊', '注册', 
        '訂閱', '订阅', '搜索', '搜尋', 'search',
        '即時新聞', '即时新闻', '最新消息', '热门', '熱門',
        '关于我们', '關於我們', 'about', '联系', '聯繫', 'contact',
    ]
    
    # 多种标题选择器，按优先级排序
    title_selectors = [
        '#article-title', '#article-detail h1', '#article-detail h2',
        '#article-detail-wrapper h1', '#article-detail-wrapper h2',
        'article h1', 'article h2', 'main h1', 'main h2',
        'h1.entry-title', 'h1.post-title', 'h1.article-title',
        'h1.page-title', 'h1.single-title', 'h1.news-title',
        'h1',  # 通用h1标签
        '.entry-title', '.post-title', '.article-title',
        '.page-title', '.single-title', '.news-title',
        'title'  # 页面标题作为备选
    ]
    
    for selector in title_selectors:
        elements = soup.select(selector)
        for element in elements:
            title = element.get_text(strip=True)
            if title and is_valid_title(title):
                title_lower = title.lower()
                
                # 🔥 通用排除：网站域名、常见排除词
                should_skip = False
                
                # 排除包含域名的标题
                if domain_name and domain_name in title_lower:
                    should_skip = True
                
                # 排除常见排除词
                if any(ex.lower() in title_lower for ex in common_exclude):
                    should_skip = True
                
                # 排除纯英文网站名称格式（如 "Site Name - Page"）
                if ' - ' in title and len(title.split(' - ')) > 1:
                    parts = title.split(' - ')
                    # 如果第一部分很短且像网站名，跳过
                    if len(parts[0]) < 20 and domain_name in parts[0].lower():
                        should_skip = True
                
                if not should_skip:
                    return clean_title(title)
    
    # 如果都没找到，从URL推断
    return extract_title_from_url(url)

def is_valid_title(title):
    """
    判断标题是否有效
    """
    if not title or len(title) < 3:
        return False
    
    # 过滤掉明显不是标题的内容
    invalid_patterns = [
        r'^[\s\-_=*#|]+$',  # 只包含特殊字符
        r'^[\d\s\-_=*#|]+$',  # 只包含数字和特殊字符
        r'^[A-Z\s]{20,}$',  # 全大写且很长
        r'^[a-z\s]{10,}$',  # 全小写且很长
        r'^https?://',  # 以URL开头
        r'^www\.',  # 以www开头
        r'\.com|\.org|\.net|\.cn',  # 包含域名
        r'首页|主页|Home|首页|导航|Menu',  # 导航相关
        r'登录|Login|注册|Register',  # 功能相关
        r'联系我们|Contact|关于我们|About',  # 页面相关
        r'版权|Copyright|©|All rights reserved',  # 版权相关
        r'隐私政策|Privacy|条款|Terms',  # 法律相关
    ]
    
    for pattern in invalid_patterns:
        if re.search(pattern, title, re.IGNORECASE):
            return False
    
    return True

def clean_title(title):
    """
    清理标题
    """
    # 移除多余的空白字符
    title = re.sub(r'\s+', ' ', title).strip()
    
    # 移除常见的后缀
    suffixes = [' - 首页', ' - Home', ' | 首页', ' | Home', ' - 网站', ' - Website']
    for suffix in suffixes:
        if title.endswith(suffix):
            title = title[:-len(suffix)].strip()
    
    return title

def extract_title_from_url(url):
    """
    从URL推断标题
    """
    try:
        from urllib.parse import urlparse, unquote
        parsed = urlparse(url)
        path = unquote(parsed.path)
        
        # 从路径中提取可能的标题
        path_parts = [part for part in path.split('/') if part and not part.isdigit()]
        if path_parts:
            # 取最后一个非数字部分
            last_part = path_parts[-1]
            # 移除文件扩展名
            title = re.sub(r'\.[a-zA-Z]+$', '', last_part)
            # 替换连字符和下划线为空格
            title = re.sub(r'[-_]', ' ', title)
            return title.title()
        
        return parsed.netloc.replace('www.', '').title()
    except:
        return "未知标题"


def evaluate_content_quality(text: str) -> int:
    """
    评估提取内容的质量（0-100分）
    
    考虑因素：
    1. 噪音比例（导航、菜单、广告等）
    2. 正文连贯性
    3. 重复内容
    4. 无关内容比例
    
    返回:
        int: 质量分数 0-100
    """
    if not text or len(text) < 50:
        return 0
    
    score = 100
    
    # 1. 检测噪音内容
    noise_patterns = [
        r'返回前[頁页]',
        r'相[关關]文章',
        r'推[荐薦]閱讀',
        r'热门文章',
        r'最新文章',
        r'上一[篇頁]|下一[篇頁]',
        r'分享到|分享至',
        r'点击[这這]里|點擊[这這]裡',
        r'订阅|訂閱',
        r'关注我们|關注我們',
        r'版权所有|版權所有',
        r'©\s*\d{4}',
        r'All Rights Reserved',
        r'隐私政策|隱私政策',
        r'用户协议|用戶協議',
    ]
    
    noise_count = 0
    for pattern in noise_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            noise_count += 1
    
    if noise_count > 0:
        penalty = min(40, noise_count * 8)
        score -= penalty
    
    # 2. 检测文章列表特征（多个标题/日期并列）
    date_patterns = [
        r'20\d{2}[/-]\d{1,2}[/-]\d{1,2}',
        r'\d{4}年\d{1,2}月\d{1,2}日',
    ]
    
    date_count = 0
    for pattern in date_patterns:
        date_count += len(re.findall(pattern, text))
    
    if date_count > 3:  # 超过3个日期，可能是文章列表
        penalty = min(30, (date_count - 3) * 10)
        score -= penalty
    
    # 3. 检测重复行
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    if lines:
        unique_lines = set(lines)
        repeat_ratio = 1 - (len(unique_lines) / len(lines))
        if repeat_ratio > 0.2:  # 超过20%重复
            score -= int(repeat_ratio * 30)
    
    # 4. 检测内容密度（正文应该有较长的段落）
    if lines:
        avg_line_length = sum(len(line) for line in lines) / len(lines)
        if avg_line_length < 20:  # 平均行长度太短，可能是菜单/列表
            score -= 15
        elif avg_line_length > 50:  # 正常的文章段落
            score += 5
    
    # 5. 检测是否有意义的中文内容
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
    chinese_ratio = chinese_chars / len(text) if text else 0
    
    # 如果是中文网站，应该有较多中文
    if chinese_ratio > 0.3:
        score += 5
    elif chinese_ratio < 0.1 and len(text) > 100:
        score -= 10  # 中文很少，可能提取错误
    
    return max(0, min(100, score))


def calculate_content_score(element):
    """
    通用内容元素质量分数计算（适用于各种网站）
    """
    if not element:
        return 0
    
    score = 0
    text = element.get_text(strip=True)
    text_length = len(text)

    # 语义化内容容器：类名/id 明确表示"这是正文/详情/日志"的元素，不应因含多个日期或
    # 若干标题行就被误判为"文章列表"而大规模减分（政务/公告页正文常带发布时间/时间线/表格）。
    _cls = " ".join(element.get("class") or []) + " " + str(element.get("id") or "")
    _content_container = bool(
        re.search(
            r"(^|[\s_-])(content|article|detail|entry|post|story|news|main|body|fully|text)([\s_-]|$)",
            _cls,
            re.IGNORECASE,
        )
    ) and not re.search(r"(list|nav|menu|sidebar|footer|related|recommend)", _cls, re.IGNORECASE)

    # ===== 通用日期检测 =====
    # 检测多种日期格式，判断是否为文章列表
    date_patterns = [
        r'20\d{2}[/-]\d{1,2}[/-]\d{1,2}',  # 2024-01-01 或 2024/01/01
        r'20\d{2}\s*/\s*\d+\s*/\s*\d+',   # 2024 / 01 / 01
        r'\d{4}年\d{1,2}月\d{1,2}日',      # 2024年01月01日
        r'\d{1,2}[/-]\d{1,2}[/-]20\d{2}', # 01/01/2024
    ]

    total_dates = 0
    for pattern in date_patterns:
        matches = re.findall(pattern, text)
        total_dates += len(matches)

    if total_dates > 2 and not _content_container:  # 包含3个或更多日期，可能是文章列表
        penalty = min(50, total_dates * 15)  # 根据日期数量递增惩罚
        score -= penalty
        print(f"发现 {total_dates} 个日期，判断为文章列表，减分{penalty}")
    
    # ===== 通用文章列表检测 =====
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    
    # 检测可能的文章标题模式
    title_like_lines = 0
    for line in lines:
        # 标题特征：长度适中，包含常见词汇，不是日期行
        if (10 <= len(line) <= 120 and 
            not any(re.search(pattern, line) for pattern in date_patterns) and
            not re.search(r'^[\d\s\-_=*#|]+$', line)):  # 不是纯符号行
            
            # 检查是否包含常见的标题关键词
            title_keywords = [
                r'助力|成功|完成|发布|发行|签署|达成|获得|实现|推出',
                r'announces?|completes?|launches?|signs?|achieves?',
                r'新闻|消息|通知|公告|声明|报告',
            ]
            
            for keyword_pattern in title_keywords:
                if re.search(keyword_pattern, line, re.IGNORECASE):
                    title_like_lines += 1
                    break
    
    if title_like_lines > 3 and not _content_container:  # 包含过多标题样式的行
        penalty = min(40, title_like_lines * 10)
        score -= penalty
        print(f"发现 {title_like_lines} 个疑似标题行，判断为文章列表，减分{penalty}")
    
    # ===== 通用类名评分 =====
    class_names = element.get('class', [])
    class_str = ' '.join(class_names).lower()
    
    # 优质内容类名（加分）
    positive_class_patterns = [
        (r'\bcontent\b(?!.*list|.*nav|.*menu)', 35),  # content但不包含list/nav/menu
        (r'\barticle[-_]?(content|body|text)\b', 40),
        (r'\bpost[-_]?(content|body|text)\b', 40),
        (r'\bentry[-_]?(content|body|text)\b', 35),
        (r'\bmain[-_]?(content|body|text)\b', 30),
        (r'\bdetail[-_]?(content|body)\b', 35),
        (r'\bdetail\b', 35),  # 政务/公告详情正文容器（ggzy 的 .detail 含完整中标人/金额）
        (r'\bnews[-_]?(content|body)\b', 30),
        (r'\bstory[-_]?(content|body)\b', 30),
        (r'\bfully\b', 38),  # 公共资源交易等政务站详情容器（ggzy 等用 class="fully"），内容块
    ]
    
    # 负面类名（减分）  
    negative_class_patterns = [
        (r'\b(sidebar|aside|nav|menu|header|footer)\b', -25),
        (r'\b(list|links|related|more|recommend)\b', -20),
        (r'\b(ad|advertisement|banner|sponsor)\b', -30),
        (r'\b(comment|reply|feedback)\b', -15),
        (r'\b(share|social|follow)\b', -10),
    ]
    
    for pattern, points in positive_class_patterns:
        if re.search(pattern, class_str):
            score += points
            print(f"发现优质类名模式 '{pattern}'，加分{points}")
            break  # 只匹配一个最高分的
    
    for pattern, points in negative_class_patterns:
        if re.search(pattern, class_str):
            score += points  # points已经是负数
            print(f"发现负面类名模式 '{pattern}'，减分{abs(points)}")
    
    # ===== 文本长度评分 =====
    if 100 <= text_length <= 500:     # 短篇正文
        score += 35
    elif 500 < text_length <= 1500:   # 中篇正文
        score += 40
    elif 1500 < text_length <= 3000:  # 长篇正文
        score += 30
    elif text_length > 3000:          # 过长，可能包含其他内容
        score += 10
    elif text_length > 50:            # 很短但有内容
        score += 15
    
    # ===== 段落结构评分 =====
    paragraphs = element.find_all(['p', 'div'])
    paragraph_count = len([p for p in paragraphs if len(p.get_text(strip=True)) > 20])
    
    if 2 <= paragraph_count <= 10:    # 合理的段落数
        score += 25
    elif paragraph_count > 10:        # 太多段落，可能是列表
        score += 5
    elif paragraph_count == 1:        # 单段落
        score += 10
    
    # ===== 标题结构评分 =====
    headings = element.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
    if len(headings) == 1:             # 一个标题，理想情况
        score += 20
    elif len(headings) == 0:           # 无标题，常见情况
        score += 5
    elif len(headings) > 5:            # 过多标题，可能是目录或列表
        score -= 15
    
    # ===== 链接密度检测 =====
    links = element.find_all('a')
    if text_length > 0:
        link_density = len(links) / text_length * 1000  # 每1000字符的链接数
        if link_density < 5:           # 链接很少，好
            score += 15
        elif link_density < 15:        # 链接适中
            score += 5
        elif link_density > 50:        # 链接过多，可能是导航
            score -= 30
        elif link_density > 25:        # 链接较多
            score -= 15
    
    # ===== 通用负面内容检测 =====
    negative_patterns = [
        # 导航相关
        (r'(首页|主页|Home|导航|Navigation|Menu|返回|Back)', -10),
        (r'(联系|Contact|关于|About|帮助|Help|FAQ)', -15),
        (r'(登录|Login|注册|Register|Sign)', -20),
        
        # 功能按钮
        (r'(分享|Share|打印|Print|下载|Download)', -8),
        (r'(评论|Comment|回复|Reply|点赞|Like)', -8),
        (r'(订阅|Subscribe|关注|Follow)', -8),
        
        # 相关内容区域
        (r'(相关文章|相关新闻|相关阅读|Related|More)', -20),
        (r'(推荐|Recommend|热门|Popular|最新|Latest)', -15),
        (r'(上一篇|下一篇|Previous|Next)', -15),
        
        # 广告和营销
        (r'(广告|Ad|Advertisement|推广|Promotion)', -25),
        (r'(赞助|Sponsor|合作|Partnership)', -15),
    ]
    
    for pattern, penalty in negative_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            score += penalty  # penalty已经是负数
    
    # ===== 内容质量检测 =====
    if text_length > 50:
        # 中文内容检测
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        if chinese_chars > 0:
            chinese_ratio = chinese_chars / text_length
            if chinese_ratio > 0.5:    # 主要是中文内容
                score += 15
            elif chinese_ratio > 0.2:  # 包含较多中文
                score += 8
        
        # 英文内容检测  
        english_words = len(re.findall(r'\b[A-Za-z]+\b', text))
        if english_words > 20 and chinese_chars < text_length * 0.3:
            score += 10  # 英文文章
        
        # 句子完整性检测
        sentences = re.split(r'[。！？.!?]', text)
        complete_sentences = [s for s in sentences if len(s.strip()) > 10]
        if len(complete_sentences) >= 3:
            score += 10
    
    final_score = max(0, score)
    # 只在分数较高时打印详细信息，避免日志过多
    if final_score >= 40:
        print(f"🎯 通用评分: {final_score}，类名: {class_names}, 长度: {text_length}")
    return final_score

def find_best_content_heuristic(soup):
    """
    使用启发式方法找到最佳内容
    """
    # 获取所有可能的内容容器
    candidates = soup.find_all(['div', 'section', 'article', 'main'])
    
    best_element = None
    best_score = 0
    
    for element in candidates:
        # 跳过明显不是内容的元素
        if is_navigation_or_sidebar(element):
            continue
        
        score = calculate_content_score(element)
        if score > best_score:
            best_score = score
            best_element = element
    
    return best_element

def is_navigation_or_sidebar(element):
    """
    判断元素是否是导航或侧边栏
    """
    if not element:
        return True
    
    # 检查class和id
    classes = ' '.join(element.get('class', []))
    element_id = element.get('id', '')
    
    nav_patterns = [
        r'nav', r'menu', r'sidebar', r'header', r'footer',
        r'breadcrumb', r'pagination', r'widget', r'ad',
        r'comment', r'related', r'recommend', r'social'
    ]
    
    for pattern in nav_patterns:
        if re.search(pattern, classes + ' ' + element_id, re.IGNORECASE):
            return True
    
    # 检查内容特征
    text = element.get_text(strip=True)
    if len(text) < 50:  # 内容太短
        return True
    
    # 检查链接密度
    links = element.find_all('a')
    if len(links) > len(text) * 0.2:  # 链接密度太高
        return True
    
    return False

def clean_content_element(element):
    """
    清理内容元素（增强版 - 更严格的噪音过滤）
    保留strong、b、em等格式标签的内容
    """
    if not element:
        return
    
    # 移除不需要的子元素（但保留格式标签如strong、b、em、i等）
    for tag in element.find_all(['script', 'style', 'nav', 'aside', 'header', 'footer', 'iframe', 'noscript']):
        tag.decompose()
    
    # 移除广告相关元素（扩展模式）
    ad_patterns = [
        r'(ad|advertisement|banner|popup|modal|promo|sponsor)',
        r'(广告|推广|赞助)',
        r'(share|social|follow|subscribe)',
        r'(分享|关注|订阅)'
    ]
    
    for pattern in ad_patterns:
        for tag in element.find_all(['div', 'section', 'aside'], class_=re.compile(pattern, re.I)):
            tag.decompose()
        for tag in element.find_all(['div', 'section', 'aside'], id=re.compile(pattern, re.I)):
            tag.decompose()
    
    # 移除包含大量链接的元素（可能是导航或相关文章）
    for tag in element.find_all(['div', 'section', 'ul', 'ol']):
        links = tag.find_all('a')
        text = tag.get_text(strip=True)
        if len(links) > 5 and len(text) < 200:
            # 链接密度太高，可能是导航
            tag.decompose()

    # （原"移除包含JavaScript代码的元素"规则已删除：它用文本正则匹配
    #  window./document./var xxx= 等字样并删除整个父元素，正文里提到
    #  "车窗 window"、"document" 等词会整段误删——NVH 文章曾因此只入库
    #  163 字导语。JS 代码在 script/style 标签内，前面已统一 decompose。）

    # 移除空元素
    for tag in element.find_all():
        if not tag.get_text(strip=True) and not tag.find('img'):
            tag.decompose()

def clean_extracted_text(text):
    """
    清理提取的文本（增强版 - 移除JSON、HTML等噪音）
    """
    if not text:
        return ""
    text = _repair_inline_newline_splits(text)
    
    # 移除导航和无关内容
    noise_patterns = [
        r'«\s*返回前[頁页].*?(?=\n|$)',
        r'返回前[頁页].*?(?=\n|$)',
        r'文章[：:]\s*《.*?》.*?(?=\n|$)',
        r'《信報.*?》.*?──.*?(?=\n|$)',
        r'《.*?攻略》.*?──.*?(?=\n|$)',
        r'《.*?投資》.*?──.*?(?=\n|$)',
        r'──\s*\S+\s*──\s*.*?(?=\n|$)',  # "── 作者 ── 标题"格式
        r'股票及指數資料由.*',
        r'免責聲明.*',
        r'版權所有.*',
        r'相關文章.*',
        r'推薦閱讀.*',
        r'熱門文章.*',
        r'最新文章.*',
        r'更多文章.*',
    ]
    
    for pattern in noise_patterns:
        text = re.sub(pattern, '', text, flags=re.MULTILINE | re.IGNORECASE)
    
    # 移除JSON数据块（常见于网页中嵌入的数据）
    text = re.sub(r'\{["\']?\w+["\']?\s*:\s*["\']?[^}]{50,}\}', '', text, flags=re.DOTALL)
    text = re.sub(r'\[["\']?\w+["\']?\s*,\s*["\']?[^]]{50,}\]', '', text, flags=re.DOTALL)
    
    # 移除HTML标签残留
    text = re.sub(r'<[^>]+>', '', text)
    
    # 移除JavaScript代码残留
    text = re.sub(r'(function\s*\([^)]*\)\s*\{[^}]*\})', '', text)
    text = re.sub(r'(var|let|const)\s+\w+\s*=\s*[^;]+;', '', text)
    text = re.sub(r'window\.\w+', '', text)
    text = re.sub(r'document\.\w+', '', text)
    
    # 移除URL（但保留少量必要的链接）
    url_count = len(re.findall(r'https?://[^\s]+', text))
    if url_count > 5:  # 如果URL过多，移除它们
        text = re.sub(r'https?://[^\s]+', '', text)
    
    # 移除邮箱地址
    text = re.sub(r'\S+@\S+\.\S+', '', text)
    
    # 移除多余的空行
    text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)
    
    # 移除行首行尾的空白
    lines = [line.strip() for line in text.split('\n')]
    
    # 过滤掉太短的行和特殊行
    filtered_lines = []
    for line in lines:
        # 保留空行（用于段落分隔）
        if not line:
            filtered_lines.append(line)
            continue

        # markdown 表格行必须原样保留：否则表头分隔行(| --- | --- |)会被下面的“纯符号行”
        # 规则误删、短单元格行会被“太短的行”规则误删，表格就渲染不出来了
        if line.lstrip().startswith('|'):
            filtered_lines.append(line)
            continue

        # 过滤掉太短的行
        if len(line) < 10:
            continue
        
        # 过滤掉纯符号行
        if re.match(r'^[\s\-_=*#|]+$', line):
            continue
        
        # 过滤掉纯数字行
        if re.match(r'^[\d\s\-_.,]+$', line):
            continue
        
        filtered_lines.append(line)
    
    return '\n'.join(filtered_lines)

def clean_article_content(content, content_type='html'):
    """
    清洗文章内容
    """
    if not content:
        return ""
    
    # 如果是Markdown，先清理markdown特定的格式
    if content_type == 'markdown':
        # 首先移除空链接 [](xxx) - 这些通常是javascript或mailto链接
        content = re.sub(r'\[\]\([^)]*\)', '', content)
        # 移除纯URL编码的mailto链接（通常是分享功能）
        content = re.sub(r'\[mailto:\?[^\]]*\]\([^)]*\)', '', content)
        # 移除javascript链接
        content = re.sub(r'\[javascript:[^\]]*\]\([^)]*\)', '', content)
        
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
            r'声明全文',
            # 财经行情/免责声明等无关内容标记
            r'《信報》印刷版出報日',
            r'印刷版出報日為星期',
            r'除以下公眾假期外',
            r'休刊日[：:]',
            r'《信報》網上版及流動版於休刊日',
            r'暫停更新',
            r'如常更新'
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
    
    # 如果是HTML，先转换为纯文本
    if content_type == 'html':
        # 移除HTML标签
        content = re.sub(r'<[^>]+>', '', content)
        # 移除多余的空白字符
        content = re.sub(r'\s+', ' ', content)
    
    # 预处理：移除明显的无关内容
    content = re.sub(r'^[\s\-_=*#|]{3,}$', '', content, flags=re.MULTILINE)
    content = re.sub(r'^[\d\s\-_=*#|]{5,}$', '', content, flags=re.MULTILINE)
    content = re.sub(r'^[A-Z\s]{10,}$', '', content, flags=re.MULTILINE)
    content = re.sub(r'^[a-z\s]{5,}$', '', content, flags=re.MULTILINE)
    
    # 按行处理内容
    lines = content.split('\n')
    cleaned_lines = []
    seen_lines = set()
    
    for line in lines:
        line = line.strip()
        if not line or len(line) < 15:
            continue
        
        # 去重
        line_lower = line.lower()
        if line_lower in seen_lines:
            continue
        seen_lines.add(line_lower)
        
        # 检查是否包含不想要的内容（只对短行进行严格检查）
        is_unwanted = False
        if len(line) < 80:
            unwanted_patterns = [
                r'联系我们|Contact|关于我们|About',
                r'版权|Copyright|©|All rights reserved',
                r'隐私政策|Privacy|条款|Terms',
                r'登录|Login|注册|Register',
                r'搜索|Search|搜索框',
                r'返回顶部|Back to top',
                r'广告|Advertisement|Ad|赞助',
                r'推荐|Recommend|热门|Popular',
                r'相关文章|Related|更多|More',
                r'分享|Share|Facebook|Twitter',
                r'关注|Follow|订阅|Subscribe',
                r'点赞|Like|收藏|Favorite',
                r'评论|Comment|留言',
                r'页脚|Footer|底部|Bottom',
                r'友情链接|Links|合作伙伴',
                r'法律声明|免责声明|网站声明',
                r'ICP备案|备案号|经营许可证',
                r'Cookie|Cookies|JavaScript',
                r'网站统计|访问统计|流量统计',
                r'用户中心|个人中心|账户',
                r'帮助|Help|FAQ|常见问题',
                r'意见反馈|Feedback|联系我们',
                r'网站公告|Notice|公告|通知',
                r'正在加载|Loading|加载中',
                r'暂无内容|No content|暂无数据',
                r'错误|Error|404|500|页面不存在',
                r'维护中|Under maintenance|系统维护',
                r'Practice Areas|业务领域|专业领域',
                r'Our Team|我们的团队|团队介绍',
                r'Our Lawyers|我们的律师|律师介绍',
                r'Legal Services|法律服务|专业服务',
                r'News & Insights|新闻洞察|最新动态',
                r'Publications|出版物|发表文章',
                r'Awards|奖项|荣誉|认可',
                r'Careers|职业发展|招聘',
                r'Client Login|客户登录|客户门户',
                r'Legal Notice|法律声明|免责声明',
                r'Terms of Use|使用条款|服务条款',
                r'Cookie Policy|Cookie政策',
                r'Disclaimer|免责声明|声明',
                r'Accessibility|无障碍|可访问性',
                r'Sitemap|网站地图|站点地图',
                r'Get in Touch|联系我们|联系',
                r'Follow Us|关注我们|社交媒体',
                r'Social Media|社交媒体|社交网络',
                r'Newsletter|新闻通讯|订阅',
                r'Subscribe|订阅|关注',
                r'Read More|阅读更多|查看更多',
                r'Learn More|了解更多|学习更多',
                r'View All|查看全部|显示全部',
                r'Load More|加载更多|查看更多',
                r'Previous|上一页|上一个',
                r'Next|下一页|下一个',
                r'Page|页|页面|第.*页',
                r'Results|结果|搜索结果',
                r'Filter|筛选|过滤',
                r'Sort|排序|排列',
                r'Search|搜索|查找',
                r'Categories|分类|类别',
                r'Tags|标签|标记',
                r'Topics|主题|话题',
                r'Related Content|相关内容|相关',
                r'You May Also Like|您可能还喜欢',
                r'Similar Articles|相似文章|相关文章',
                r'Recommended Reading|推荐阅读',
                r'Popular Posts|热门文章|热门',
                r'^(Trending|趋势|热门|流行)$',
                r'^(Featured|精选|特色|推荐)$',
                r'^(Latest News|最新新闻|最新)$',
                r'^(Recent Updates|最近更新|最新)$',
                r'Announcements|公告|通知|声明',
                r'Press Releases|新闻稿|媒体发布',
                r'Media|媒体|新闻|传媒',
                r'Events|活动|事件|会议',
                r'Webinars|网络研讨会|在线会议',
                r'Seminars|研讨会|讲座|培训',
                r'Conferences|会议|大会|论坛',
                r'Reports|报告|研究报告',
                r'Whitepapers|白皮书|研究报告',
                r'Case Studies|案例研究|案例分析',
                r'Success Stories|成功案例|成功故事',
                r'Testimonials|客户评价|客户反馈',
                r'Client Reviews|客户评价|客户反馈',
                r'Feedback|反馈|意见|建议',
                r'Ratings|评分|评级|评价',
                r'Office Locations|办公地点|办公室',
                r'Global Presence|全球业务|国际业务',
                r'Worldwide|全球|世界|国际',
                r'Regions|地区|区域|范围',
                r'Countries|国家|地区|国际',
                r'Cities|城市|地区|地点',
                r'Addresses|地址|位置|地点',
                r'Phone Numbers|电话号码|电话|联系',
                r'Email Addresses|邮箱地址|邮箱|邮件',
                r'Directions|方向|路线|导航',
                r'Opening Hours|营业时间|开放时间',
                r'Business Hours|营业时间|工作时间',
                r'Holidays|节假日|假期|休息',
                r'Emergency Contact|紧急联系|应急联系',
                r'24/7 Support|24小时支持|全天候',
                r'Hotline|热线|电话|联系'
            ]
            
            for pattern in unwanted_patterns:
                if re.search(pattern, line, re.IGNORECASE):
                    is_unwanted = True
                    break
        
        # 检查其他条件
        if not is_unwanted:
            # 检查重复字符
            if len(set(line)) <= 2 and len(line) > 5:
                is_unwanted = True
            
            # 检查URL数量
            url_count = len(re.findall(r'https?://[^\s]+', line))
            if url_count > 1:
                is_unwanted = True
            
            # 检查特殊字符比例
            special_char_count = len(re.findall(r'[^\w\s\u4e00-\u9fff]', line))
            if special_char_count > len(line) * 0.25:
                is_unwanted = True
            
            # 检查数字比例
            digit_count = len(re.findall(r'\d', line))
            if digit_count > len(line) * 0.6:
                is_unwanted = True
        
        if not is_unwanted:
            cleaned_lines.append(line)
    
    # 重新组合内容
    cleaned_content = '\n'.join(cleaned_lines)
    
    # 后处理
    cleaned_content = re.sub(r'\n\s*\n\s*\n+', '\n\n', cleaned_content)
    cleaned_content = cleaned_content.strip()
    
    return cleaned_content

def test_universal_extraction():
    """测试通用智能提取功能"""
    
    # 测试多种类型的网站
    test_cases = [
        {
            'name': '律师事务所',
            'url': 'https://www.fangdalaw.com/content/details34_9069.html',
            'expected_keywords': ['金山云', '方达', '融资']
        },
        # 可以添加更多网站测试
        # {
        #     'name': '新闻网站',
        #     'url': 'https://example-news.com/article/123',
        #     'expected_keywords': ['新闻', '报道']
        # }
    ]
    
    print("🧪 通用文章提取算法测试")
    print("=" * 80)
    
    for i, test_case in enumerate(test_cases, 1):
        print(f"\n📋 测试 {i}: {test_case['name']}")
        print(f"URL: {test_case['url']}")
        print("-" * 60)
        
        try:
            result = extract_article_content_from_url(test_case['url'])
            
            if result['success']:
                content = result['content']
                
                print(f"✅ 提取成功")
                print(f"📄 标题: {result.get('title', '无标题')[:60]}...")
                print(f"📏 内容长度: {len(content)} 字符")
                print(f"🎯 质量分数: {result.get('score', 0)}")
                print(f"🔧 提取方法: {result.get('method', '未知')}")
                
                # 内容质量检测
                # 检测日期数量
                date_patterns = [
                    r'20\d{2}[/-]\d{1,2}[/-]\d{1,2}',
                    r'20\d{2}\s*/\s*\d+\s*/\s*\d+',
                    r'\d{4}年\d{1,2}月\d{1,2}日'
                ]
                total_dates = sum(len(re.findall(pattern, content)) for pattern in date_patterns)
                
                # 检测关键词
                found_keywords = []
                for keyword in test_case.get('expected_keywords', []):
                    if keyword in content:
                        found_keywords.append(keyword)
                
                # 质量评估
                quality_score = 0
                if total_dates <= 1:
                    quality_score += 30
                    print("🔍 日期检测: ✅ 纯正文 (日期数≤1)")
                else:
                    print(f"🔍 日期检测: ❌ 可能包含列表 (发现{total_dates}个日期)")
                
                if len(found_keywords) > 0:
                    quality_score += 20
                    print(f"🔍 关键词匹配: ✅ 找到 {found_keywords}")
                else:
                    print("🔍 关键词匹配: ⚠️ 未找到预期关键词")
                
                if 100 <= len(content) <= 2000:
                    quality_score += 25
                    print("🔍 内容长度: ✅ 合理范围")
                else:
                    print(f"🔍 内容长度: ⚠️ {len(content)}字符 (可能过短或过长)")
                
                # 检测是否包含导航内容
                navigation_keywords = ['相关文章', '上一篇', '下一篇', '返回', '首页']
                has_navigation = any(kw in content for kw in navigation_keywords)
                if not has_navigation:
                    quality_score += 15
                    print("🔍 导航内容: ✅ 无导航干扰")
                else:
                    print("🔍 导航内容: ❌ 包含导航元素")
                
                print(f"\n🏆 综合质量评估: {quality_score}/90")
                if quality_score >= 70:
                    print("📊 评估结果: ✅ 优秀")
                elif quality_score >= 50:
                    print("📊 评估结果: ⚠️ 良好")
                else:
                    print("📊 评估结果: ❌ 需要改进")
                
                print(f"\n📝 内容预览:")
                preview = content[:200].replace('\n', ' ')
                print(f"   {preview}...")
                
            else:
                print(f"❌ 提取失败")
                print(f"💥 错误信息: {result.get('error', '未知错误')}")
                
        except Exception as e:
            print(f"❌ 测试异常: {str(e)}")
        
        print("-" * 80)
    
    print(f"\n🎯 测试完成！共测试了 {len(test_cases)} 个网站")
    print("\n💡 如需测试更多网站，请在 test_cases 列表中添加更多测试用例")

def test_smart_extraction():
    """兼容性函数，调用新的通用测试"""
    test_universal_extraction()

if __name__ == "__main__":
    test_smart_extraction()
