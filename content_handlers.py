#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
内容处理模块
包含文章提取、清洗等功能
"""

import re
import requests
from newspaper import Article
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse


def extract_with_newspaper3k(url):
    """使用 Newspaper3k 提取内容"""
    try:
        # 🔧 标准化URL（转换移动版为桌面版）
        original_url = url
        try:
            from url_transformation_rules import transform_url
            url = transform_url(url, verbose=False, verify=False)
            if url != original_url:
                print(f"🔄 URL已标准化: {original_url[:50]}... → {url[:50]}...")
        except:
            pass  # 如果转换失败，使用原URL
        
        # 创建Article对象
        article = Article(url, language='zh')
        
        # 下载和解析
        article.download()
        article.parse()
        
        # 获取页面源代码来提取链接
        links = []
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
            }
            response = requests.get(url, headers=headers, timeout=30)
            soup = BeautifulSoup(response.content, 'html.parser')
            for link in soup.find_all('a', href=True):
                href = link.get('href', '')
                text_content = link.get_text(strip=True)
                if href and text_content:
                    absolute_url = urljoin(url, href)
                    # 🔧 也转换链接中的移动版URL
                    try:
                        absolute_url = transform_url(absolute_url, verbose=False, verify=False)
                    except:
                        pass
                    links.append({'url': absolute_url, 'text': text_content})
        except:
            pass
        
        return {
            'method': 'Newspaper3k',
            'url': url,  # 返回标准化后的URL
            'success': True,
            'title': article.title,
            'author': ', '.join(article.authors) if article.authors else None,
            'date': str(article.publish_date) if article.publish_date else None,
            'text_content': article.text,
            'text_length': len(article.text) if article.text else 0,
            'links_found': len(links),
            'sample_links': links[:5],  # 前5个链接作为示例
            'summary': article.summary if hasattr(article, 'summary') else None
        }
    except Exception as e:
        return {
            'method': 'Newspaper3k',
            'url': url,
            'success': False,
            'error': str(e)
        }


def extract_article_links_from_list_page(list_url):
    """从列表页面智能提取文章链接"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
        }
        response = requests.get(list_url, headers=headers, timeout=30)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        base_domain = urlparse(list_url).netloc
        
        # 移除导航、页脚、侧边栏等非内容区域
        for element in soup(['nav', 'footer', 'aside', 'header']):
            element.decompose()
        
        # 查找所有链接
        all_links = soup.find_all('a', href=True)
        article_links = []
        seen_urls = set()
        
        for link in all_links:
            href = link.get('href', '').strip()
            text = link.get_text(strip=True)
            
            if not href or not text or len(text) < 5:
                continue
            
            # 转换为绝对URL
            absolute_url = urljoin(list_url, href)
            
            # 🔧 转换移动版URL为桌面版（防止爬到移动版链接）
            try:
                from url_transformation_rules import transform_url
                absolute_url = transform_url(absolute_url, verbose=False, verify=False)
            except:
                pass  # 如果转换失败，使用原URL
            
            # 基本过滤条件
            if absolute_url in seen_urls:
                continue
            
            # 检查URL是否在同一个域名下（或相关子域）
            link_domain = urlparse(absolute_url).netloc
            if base_domain not in link_domain and link_domain not in base_domain:
                continue
            
            # 智能识别文章链接的特征
            is_article_link = False
            
            # 1. URL路径特征
            url_path = urlparse(absolute_url).path.lower()
            article_path_patterns = [
                r'/news/',
                r'/article/',
                r'/post/',
                r'/blog/',
                r'/detail/',
                r'/content/',
                r'/story/',
                r'/press/',
                r'/publication/',
                r'/insights/',
                r'/legal-update/',
                r'/expertise/',
                r'/experience/',
                r'/\d{4}/',  # 年份
                r'/\d{4}-\d{2}/',  # 年月
                r'/\d{4}/\d{2}/',  # 年/月
                r'\.html',
                r'\.htm',
                r'\.aspx',
                r'\.php'
            ]
            
            for pattern in article_path_patterns:
                if re.search(pattern, url_path):
                    is_article_link = True
                    break
            
            # 2. 链接文本特征
            if not is_article_link:
                text_lower = text.lower()
                article_text_indicators = [
                    # 中文特征
                    r'[\u4e00-\u9fff].*[\u4e00-\u9fff]',  # 包含中文字符
                    # 标题特征
                    r'.{10,}',  # 较长的文本
                ]
                
                # 排除导航类文本
                navigation_keywords = [
                    '首页', '主页', 'home', 'index',
                    '关于', 'about', '联系', 'contact',
                    '服务', 'service', '产品', 'product',
                    '新闻中心', '更多', 'more', '全部',
                    '上一页', '下一页', 'prev', 'next',
                    '登录', 'login', '注册', 'register',
                    '搜索', 'search', '帮助', 'help',
                    '网站地图', 'sitemap', '法律声明',
                    '隐私政策', 'privacy', '免责声明',
                    '版权', 'copyright', '友情链接',
                    '返回顶部', 'top', '分享', 'share',
                    '打印', 'print', '收藏', 'favorite',
                    '中文', 'en', 'english', '繁体',
                    '简体', '语言', 'language'
                ]
                
                is_navigation = any(keyword in text_lower for keyword in navigation_keywords)
                
                if not is_navigation:
                    for pattern in article_text_indicators:
                        if re.search(pattern, text, re.IGNORECASE):
                            is_article_link = True
                            break
            
            # 3. HTML结构特征
            if not is_article_link:
                # 检查链接是否在文章列表容器中
                parent_classes = []
                parent = link.parent
                while parent and parent.name:
                    if parent.get('class'):
                        parent_classes.extend(parent.get('class'))
                    if parent.get('id'):
                        parent_classes.append(parent.get('id'))
                    parent = parent.parent
                    if len(parent_classes) > 20:  # 避免无限循环
                        break
                
                article_container_keywords = [
                    'news', 'article', 'post', 'blog', 'content',
                    'list', 'item', 'entry', 'story', 'publication',
                    'insight', 'update', 'press', 'media'
                ]
                
                parent_classes_str = ' '.join(parent_classes).lower()
                for keyword in article_container_keywords:
                    if keyword in parent_classes_str:
                        is_article_link = True
                        break
            
            # 4. 过滤明显不是文章的链接
            excluded_extensions = ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.zip', '.rar']
            excluded_keywords = ['mailto:', 'javascript:', 'tel:', '#', 'void(0)']
            
            should_exclude = False
            for ext in excluded_extensions:
                if ext in absolute_url.lower():
                    should_exclude = True
                    break
            
            if not should_exclude:
                for keyword in excluded_keywords:
                    if keyword in absolute_url.lower():
                        should_exclude = True
                        break
            
            if should_exclude:
                continue
            
            # 添加符合条件的链接
            if is_article_link and len(text) >= 5:
                article_links.append({
                    'url': absolute_url,
                    'text': text[:100],  # 限制文本长度
                    'confidence': 'high' if len(text) > 20 else 'medium'
                })
                seen_urls.add(absolute_url)
        
        # 按置信度和文本长度排序
        article_links.sort(key=lambda x: (
            x['confidence'] == 'high',
            len(x['text']),
            -len(x['url'])
        ), reverse=True)
        
        return {
            'success': True,
            'links': article_links[:20],  # 最多返回20个链接
            'total_found': len(article_links),
            'extraction_method': 'intelligent_pattern_matching'
        }
        
    except Exception as e:
        return {
            'success': False,
            'error': f'提取文章链接失败: {str(e)}'
        }


def _looks_like_listing_or_contentless(content, title=""):
    """判断是否为栏目/列表页或空洞内容：以标题列表/链接为主、缺少正文段落。

    判据（真实文章几乎无链接、正文段落充足，误判率很低）：
    - 内容里链接 ≥5 个 → 聚合/列表页；
    - 几乎无正文段落(<40字)，却有一堆短标题行 → 栏目/列表页；
    - 长文本却几乎没有正文段落 → 异常；
    - 标题形如“栏目名 - 站点名” 且正文也少 → 栏目页。
    """
    import re
    text = str(content or "")
    if not text.strip():
        return True
    lines = [ln.strip() for ln in text.split('\n') if ln.strip()]
    urls = re.findall(r'https?://[^\s<>"\'\)]+|www\.[^\s<>"\']+', text)
    prose = [ln for ln in lines if len(ln) > 40]                       # 正文段落
    # 链接多 且 几乎没有正文段落 → 聚合/列表页。
    # crawl4ai 的 markdown 会保留站点导航/图片链接，真实文章链接也很多，但正文段落充足，
    # 不能只看链接数就误杀。只有"链接多 + 正文段落极少"才是真列表页。
    if len(urls) >= 5 and len(prose) < 3:
        return True
    if len(text.strip()) > 2000 and len(prose) < 3:                    # 长文本却几乎没有正文 → 异常
        return True
    # WordPress 新闻站（hit180 等）列表页：正文被提取成 N 条
    # "2026-09-18 作者 阅读(N) 评论(N) 赞(N)" 条目行，标题却取自列表第一条。
    # 这类页面的日期行会≥3 条，真实文章正文几乎不会出现 3 行以上以日期开头的行。
    _dated_rows = [
        ln for ln in lines
        if re.match(r'^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}', ln)
    ]
    if len(_dated_rows) >= 3:
        return True
    _listing_rows = 0
    for ln in lines:
        if re.match(r'^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}', ln) and re.search(
            r'阅读\(\d+\)|评论\(\d+\)|赞\(\d+\)|浏览\(\d+\)|点赞\(\d+\)', ln
        ):
            _listing_rows += 1
    if _listing_rows >= 3:
        return True
    t = str(title or "").strip()
    m = re.match(r'^([^\-–—|·]{1,12})\s*[-–—|·]\s*(\S{2,})$', t)
    if m and len(prose) < 4:                                           # 标题为“栏目名 - 站点”且正文少 → 栏目页
        head = m.group(1).strip()
        generic = {"首页","栏目","列表","分类","专题","媒体","新闻","快讯","资讯","中心","平台",
                   "服务","机构","官网","VR","AR","VR/AR","智能家居","智能产业","行业","专题报道","媒体平台"}
        if head.casefold() in {g.casefold() for g in generic}:
            return True
    return False


def is_valid_article_content(content, title=""):
    """
    检查内容是否是有效的文章内容 - 改进版本
    """
    if not content or len(content.strip()) < 50:  # 提高最小长度要求
        return False
    
    # 检查是否包含明显的中文内容
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', content))
    if chinese_chars < 5:  # 至少包含5个中文字符
        return False
    
    # 过滤掉明显不是文章的内容
    invalid_patterns = [
        r'^\[!\[.*?\]\(.*?\)\]\(.*?\)$',  # 纯图片链接
        r'^en\s*$',  # 单独的"en"
        r'^[a-zA-Z\s]+$',  # 纯英文且很短
        r'^[\d\s\-_\.]+$',  # 纯数字和符号
        r'^[^\u4e00-\u9fff]*$',  # 不包含中文
        r'^.{1,20}$',  # 太短的内容
    ]
    
    for pattern in invalid_patterns:
        if re.match(pattern, content.strip(), re.IGNORECASE):
            return False
    
    # 新增：栏目/列表页或空洞内容 → 直接拒绝
    if _looks_like_listing_or_contentless(content, title):
        return False
    
    # 检查是否包含明显的文章内容特征
    article_indicators = [
        r'年\d+月\d+日',  # 日期格式
        r'近日|最近|近期',  # 时间指示词
        r'据悉|据了解|据报道',  # 新闻常用词
        r'律师|法律|法规|政策',  # 法律相关词
        r'公司|企业|机构|组织',  # 实体词
        r'项目|交易|融资|上市',  # 业务词
        r'成功|完成|获得|荣获',  # 成就词
        r'表示|指出|强调|认为',  # 观点词
        r'根据|按照|依据|规定',  # 依据词
        r'本次|此次|该|此',  # 指代词
        r'君合|合伙人|律师',  # 君合特定词
        r'助力|协助|担任',  # 服务词
        r'两江潮涌|重庆分所|开业盛典',  # 具体文章标题
        r'高端制造|法总闭门会',  # 具体文章标题
        r'破局重生|破产重整',  # 具体文章标题
        r'中企出海|国际规则',  # 具体文章标题
        r'诚挚欢迎|顾问|律师',  # 人员相关
        r'西雅图|华人律师协会',  # 具体内容
        r'溯江而上|聚渝都',  # 具体内容
        r'伦敦国际仲裁院',  # 具体内容
    ]
    
    # 如果包含文章特征，认为是有效内容
    for pattern in article_indicators:
        if re.search(pattern, content, re.IGNORECASE):
            return True
    
    # 如果内容长度足够，也认为是有效的
    if len(content.strip()) > 100:
        return True
    
    return False


# ============================================================
# 正文统一 Markdown 转换（阶段1：详情页展示用，不影响 content 原始字段）
# ============================================================

MAX_MARKDOWN_CHARS = 50000


def _looks_like_html(text):
    """判断文本是否为 HTML 片段/整页（含常见块级标签即视为 HTML）。"""
    return bool(re.search(r'<(p|div|h[1-6]|article|section|table|ul|ol|li|span|a|img|br)\b[^>]*>', str(text or ""), re.IGNORECASE))


def _looks_like_markdown(text):
    """判断文本是否已具备 Markdown 结构（标题/粗体/列表/链接/表格）。"""
    t = str(text or "")
    if re.search(r'(?m)^#{1,6}\s+\S', t):
        return True
    if re.search(r'\*\*[^*\n]{2,}\*\*', t):
        return True
    if re.search(r'(?m)^\s*[-*+]\s+\S', t) or re.search(r'(?m)^\s*\d+[.、)]\s+\S', t):
        return True
    if re.search(r'\[[^\]\n]{1,80}\]\((https?://[^\s)]+)\)', t):
        return True
    if t.count('|') >= 4 and re.search(r'(?m)^\s*\|.*\|\s*$', t):
        return True
    return False


def _looks_like_js_junk(text):
    """判断文本是否为 JS/JSON 序列化垃圾（转义符、花括号密布、中文极少）。"""
    t = str(text or "")
    if not t.strip():
        return True
    chinese = len(re.findall(r'[\u4e00-\u9fff]', t))
    if chinese >= 20:
        return False
    braces = t.count('{') + t.count('}') + t.count('\\')
    quotes = t.count('"') + t.count("'")
    total = max(len(t), 1)
    # 转义/花括号/引号占比过高且几乎没有中文 → 垃圾
    if (braces + quotes) / total > 0.25:
        return True
    if re.search(r'(function\s*\(|window\.|document\.|\\u[0-9a-fA-F]{4}|\[object\s)', t):
        return True
    return False


def _line_is_all_links(s):
    """整行去掉所有 Markdown 链接/图片与分隔符后为空 → 导航条（可多个链接同行）。"""
    if not re.search(r'\]\([^)]*\)', s):
        return False
    stripped = re.sub(r'!?\[[^\]]*\]\([^)]*\)', '', s)
    stripped = re.sub(r'[\s*\-–—·•|,，、>]+', '', stripped)
    return stripped == ''


# 浏览器兼容提示条（IE 低版本/升级浏览器/账号安全提示等长句）
_BROWSER_NOTICE_RE = re.compile(
    r"(?:正在使用)?IE\s*低版|低版本浏览器|浏览器版本过低|强烈建议使用|请升级.{0,8}浏览器|"
    r"为了您的.{0,24}(?:账号安全|产品体验|体验)|更快更安全的浏览器|使用.{0,6}浏览器访问"
)
# 作者卡片/互动区行（认证作者/文章数/点赞/TA 的文章与评论）
_AUTHOR_CARD_RE = re.compile(
    r"TA的文章|评论TA的|TA的评论|认证作者|点赞\s*\d|粉丝\s*\d|文章\s*\d+\s*篇|"
    r"关注\s*TA"
)
_DATE_ROW_RE = re.compile(
    r"^\s*\d{4}[/\-年]\d{1,2}[/\-月]\d{1,2}日?(?:\s+\d{1,2}:\d{2})?\s*$", re.M
)


def _drop_browser_and_author_noise(lines):
    """删除浏览器兼容提示条与作者卡/互动区行（含紧邻的空行）。

    特征词足够特化（IE 低版本/TA的文章/评论TA的/认证作者/点赞 N…），
    且仅当行长 ≤64 时才判定，避免误删正文句子。
    """
    out = []
    for line in lines:
        s = line.strip()
        if len(s) <= 64 and (_BROWSER_NOTICE_RE.search(s) or _AUTHOR_CARD_RE.search(s)):
            continue
        out.append(line)
    # 压缩连续空行
    cleaned, blank = [], False
    for line in out:
        if line.strip() == "":
            if not blank:
                cleaned.append("")
            blank = True
        else:
            cleaned.append(line)
            blank = False
    return cleaned


def looks_like_title_list(text, min_date_rows=5):
    """识别"标题列表/作者主页聚合页"式正文：出现 ≥min_date_rows 条日期行。

    作者主页、栏目页、聚合页的正文特征是「一句话标题 + 日期（+链接）」重复出现；
    真正的文章正文几乎不会出现 5 条以上的独立日期行。
    供质量门禁使用：命中时应拒绝作为单篇文章入库，或按列表处理。
    """
    if not text:
        return False
    date_rows = len(_DATE_ROW_RE.findall(str(text)))
    return date_rows >= min_date_rows


def _drop_short_line_blocks(text, min_run=8, max_len=8):
    """删除连续超短行块（站内导航/股票行情碎片等）。

    网页抽取失败时会把导航菜单（新浪首页/新闻/体育…）或股票行情碎片
    （( 514.980 , 11.72 , 2.33% )）混进正文，特征都是「连续多行且每行
    ≤max_len 字符」。正文段落通常 ≥10 字，连续 min_run 个短行几乎不可能
    是正文，整块删除。空行计入块内（允许短行间夹空行）。
    """
    lines = str(text).split("\n")
    out = []
    run = []
    for line in lines:
        s = line.strip()
        if not s or len(s) <= max_len:
            run.append(line)
            continue
        if sum(1 for _l in run if _l.strip()) >= min_run:
            out.append("")  # 删除整块，保留一个空行分隔上下文
        else:
            out.extend(run)
        run = []
        out.append(line)
    if run:
        if sum(1 for _l in run if _l.strip()) < min_run:
            out.extend(run)
        else:
            out.append("")
    return "\n".join(out)


def _clean_markdown_noise(md_text):
    """轻清洗 Markdown：去掉纯图片行、导航链接行（含多链接同行）、导航词残留与垃圾短行。"""
    drop_prefixes = ('首页', '登录', '注册', '关于我们', '联系我们', '网站地图', '回到顶部',
                     '返回顶部', '上一篇', '下一篇', '分享到', '阅读原文', '扫码', '关注',
                     '热门推荐', '相关推荐', '编辑推荐', '推荐阅读')
    # 导航/栏目词：行内去掉链接与符号后仅剩这些词 → 整行丢弃
    nav_words = ('首页', '登录', '注册', '关于我们', '联系我们', '网站地图', '回到顶部', '返回顶部',
                 '上一篇', '下一篇', '分享到', '阅读原文', '扫码', '关注', '新闻', '行业', '趋势',
                 '访谈', '专栏', '视频', '专题', '更多', '推荐', '热门', '首页上一页下一页尾页')
    nav_chars = set(''.join(nav_words))
    lines = []
    for ln in str(md_text or "").split('\n'):
        s = ln.strip()
        if not s:
            lines.append('')
            continue
        if re.match(r'^!\[[^\]]*\]\([^)]*\)$', s):          # 纯图片行
            continue
        if _line_is_all_links(s):                            # 导航条（多个链接同行也算）
            continue
        # 行内去掉链接/图片/符号后仅剩导航词 → 丢弃
        leftover = re.sub(r'!?\[[^\]]*\]\([^)]*\)', '', s)
        leftover = re.sub(r'[\[\]()*#>\s,，、|·•\-–—:：]+', '', leftover)
        if leftover and leftover in nav_words:
            continue
        if leftover and set(leftover) <= nav_chars:
            continue
        # 垃圾短行（几乎无中文且不是标题/表格行）
        if (len(re.findall(r'[\u4e00-\u9fff]', leftover)) < 2
                and not re.match(r'^#{1,6}\s', s)
                and not re.match(r'^\s*\|', s)
                and len(leftover) < 4):
            continue
        if s.startswith(drop_prefixes):
            continue
        lines.append(ln)
    # 浏览器兼容提示条 / 作者卡互动区
    lines = _drop_browser_and_author_noise(lines)
    # 压缩连续空行为单个空行
    out, blank = [], False
    for ln in lines:
        if ln.strip() == '':
            if not blank:
                out.append('')
            blank = True
        else:
            out.append(ln)
            blank = False
    return '\n'.join(out).strip()


def _plain_text_to_markdown(text):
    """纯文本 → 结构化 Markdown：短小节标题（无数字、无句末标点）作二级标题，
    冒号结尾短块加粗，长块为段落。"""
    blocks = [b.strip() for b in re.split(r'\n\s*\n', str(text or "")) if b.strip()]
    if not blocks:
        return str(text or "")
    parts = []
    for i, block in enumerate(blocks):
        inner = re.sub(r'[ \t]+', ' ', block)
        single = '\n' not in inner
        # 冒号结尾短行 → 加粗小节题
        if single and len(inner) <= 40 and inner.endswith(('：', ':')):
            parts.append(f'**{inner}**')
            continue
        # 短小节标题（无数字、无句末标点）→ 二级标题
        if single and 6 <= len(inner) <= 40 and not re.search(r'\d', inner) and inner[-1] not in '。！？!?；;':
            parts.append(f'## {inner}')
            continue
        # 首块 60 字内无换行 → 视作导语标题
        if i == 0 and single and len(inner) <= 60:
            parts.append(f'## {inner}')
            continue
        parts.append(inner.replace('\n', '\n\n'))
    return '\n\n'.join(parts)


def build_article_markdown(raw_content, content):
    """把文章正文统一转换为 Markdown 文本（仅展示用）。

    优先级：
    1. raw_content 已是 Markdown → 轻清洗后直接用；
    2. raw_content 是 HTML → markdownify 转换后清洗；
    3. raw_content 是 JS/JSON 垃圾或无 raw → 退回 content（摘要）结构化处理；
    结果超长按 MAX_MARKDOWN_CHARS 截断。
    """
    raw = str(raw_content or "").strip()
    fallback = str(content or "").strip()
    result = None

    if raw and not _looks_like_js_junk(raw):
        if _looks_like_markdown(raw) and not _looks_like_html(raw):
            result = _clean_markdown_noise(raw)
        elif _looks_like_html(raw):
            try:
                from markdownify import markdownify as _mdify
                converted = _mdify(raw, heading_style='ATX', bullets='-')
                result = _clean_markdown_noise(converted)
            except Exception:
                result = None
        else:
            # 纯文本原文 → 结构化（小节标题/段落）
            result = _plain_text_to_markdown(raw)

    # 转换结果太空洞（导航被剥光）或无法转换 → 退回 content 结构化
    if not result or len(re.findall(r'[\u4e00-\u9fff]', result)) < 10:
        if fallback:
            result = _plain_text_to_markdown(fallback)
        else:
            result = _plain_text_to_markdown(raw) if raw else ''

    result = (result or '').strip()
    # 图片噪音：剔除内嵌图片 token（正文展示统一走 LLM 精炼摘要，不保留图片引用）
    result = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', result)
    # 浏览器提示条 / 作者卡互动区
    result = "\n".join(_drop_browser_and_author_noise(result.split("\n")))
    # 导航/行情碎片：连续超短行块整块删除
    result = _drop_short_line_blocks(result)
    result = re.sub(r'\n{3,}', '\n\n', result).strip()
    if len(result) > MAX_MARKDOWN_CHARS:
        result = result[:MAX_MARKDOWN_CHARS] + '\n\n*（内容过长，已截断）*'
    return result

