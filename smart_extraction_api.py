#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
智能提取API端点
"""

from flask import Blueprint, request, jsonify, send_file
import os
import json
import re
import io
import zipfile
from datetime import datetime, timedelta
from utils import get_china_time
from smart_article_extractor import extract_article_content_from_url, clean_article_content, extract_title_from_url

# 导入 Playwright 链接提取器
try:
    from playwright_link_extractor import extract_links_with_playwright
    PLAYWRIGHT_AVAILABLE = True
    print("✅ Playwright 链接提取器已加载")
except ImportError as e:
    PLAYWRIGHT_AVAILABLE = False
    print(f"⚠️ Playwright 链接提取器未加载: {e}")

def get_keywords_match_result(title, content, keywords):
    """Return keyword match status and location-marked keywords for storage."""
    if not keywords or not keywords.strip():
        return {
            'matched': True,
            'title_keywords': [],
            'content_keywords': [],
            'matched_keywords_str': ''
        }

    try:
        from keyword_filter import KeywordFilter
        keyword_filter = KeywordFilter(keywords)
        if not keyword_filter.is_enabled():
            return {
                'matched': True,
                'title_keywords': [],
                'content_keywords': [],
                'matched_keywords_str': ''
            }

        match_result = keyword_filter.get_matched_keywords_by_location(title or '', content or '')
        title_keywords = match_result.get('title_keywords', [])
        content_keywords = match_result.get('content_keywords', [])
        return {
            'matched': bool(title_keywords or content_keywords),
            'title_keywords': title_keywords,
            'content_keywords': content_keywords,
            'matched_keywords_str': match_result.get('matched_keywords_str', '')
        }
    except Exception as exc:
        print(f"⚠️ KeywordFilter不可用，降级为普通关键词匹配: {exc}")

    keyword_list = [kw.strip() for kw in keywords.split(',') if kw.strip()]
    text_to_check = f"{title} {content}".lower()
    matched_keywords = [kw for kw in keyword_list if kw.lower() in text_to_check]
    return {
        'matched': bool(matched_keywords),
        'title_keywords': [],
        'content_keywords': [],
        'matched_keywords_str': ','.join(matched_keywords)
    }

def check_keywords_filter(title, content, keywords):
    """
    检查文章是否包含关键词
    
    Args:
        title: 文章标题
        content: 文章内容
        keywords: 关键词字符串，多个关键词用逗号分隔
        
    Returns:
        bool: 如果没有关键词或包含任一关键词返回True，否则返回False
    """
    # 如果没有设置关键词，直接通过
    if not keywords or not keywords.strip():
        return True
    
    # 分割关键词
    keyword_list = [kw.strip() for kw in keywords.split(',') if kw.strip()]
    if not keyword_list:
        return True
    
    # 组合标题和内容进行检查
    text_to_check = f"{title} {content}".lower()
    
    # 检查是否包含任一关键词（不区分大小写）
    for keyword in keyword_list:
        if keyword.lower() in text_to_check:
            print(f"✅ 文章匹配关键词: '{keyword}'")
            return True
    
    print(f"❌ 文章不包含任何关键词: {', '.join(keyword_list)}")
    return False

def is_article_link(title, url):
    """
    判断链接是否可能是文章链接
    """
    # 过滤掉明显不是文章的链接
    non_article_patterns = [
        # 导航相关
        r'首页|主页|Home|导航|Menu',
        r'联系我们|Contact|关于我们|About',
        r'登录|Login|注册|Register',
        r'搜索|Search|帮助|Help',
        r'版权|Copyright|隐私|Privacy',
        r'法律声明|免责声明|条款|Terms',
        
        # 功能相关
        r'招聘|Careers|工作|Jobs',
        r'办公|Office|地址|Address',
        r'荣誉|Award|奖项|Awards',
        r'概览|Overview|介绍|Introduction',
        r'网络|Network|平台|Platform',
        
        # 文件类型
        r'\.(pdf|doc|docx|xls|xlsx|ppt|pptx|zip|rar)$',
        r'\.(jpg|jpeg|png|gif|bmp|svg)$',
        r'\.(mp4|avi|mov|wmv|flv)$',
        
        # 特殊页面
        r'/careers?/',
        r'/contact',
        r'/about',
        r'/offices?',
        r'/awards?',
        r'/overview',
        r'/network',
        r'/platform',
        r'/sitemap',
        r'/robots\.txt',
    ]
    
    # 检查标题
    for pattern in non_article_patterns:
        if re.search(pattern, title, re.IGNORECASE):
            return False
    
    # 检查URL
    for pattern in non_article_patterns:
        if re.search(pattern, url, re.IGNORECASE):
            return False
    
    # 检查是否为列表页面（新增）
    list_page_patterns = [
        r'/news/?(\?|$)',  # 新闻主页
        r'/news\?page=',   # 分页列表
        r'/news\?year=',   # 年份筛选页
        r'/news\?category=', # 分类页
        r'/articles?/?(\?|$)', # 文章主页
        r'/blog/?(\?|$)',  # 博客主页
        r'page=\d+',       # 通用分页参数
        r'category=\w+',   # 通用分类参数
        r'tag=\w+',        # 标签页
        r'/archive',       # 归档页
        r'/search\?',      # 搜索结果页
    ]
    
    for pattern in list_page_patterns:
        if re.search(pattern, url, re.IGNORECASE):
            print(f"🚫 过滤列表页面: {url} (匹配模式: {pattern})")
            return False
    
    # 检查URL路径长度（太短可能是导航页面）
    from urllib.parse import urlparse
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split('/') if part]
    if len(path_parts) < 2 and not parsed.query:  # 路径太短且没有查询参数
        return False
    
    # 检查是否包含文章相关的关键词
    article_keywords = [
        r'news', r'article', r'post', r'blog',
        r'news', r'文章', r'新闻', r'资讯',
        r'detail', r'content', r'story',
        r'详情', r'内容', r'故事'
    ]
    
    # 如果包含文章关键词，更可能是文章
    for keyword in article_keywords:
        if re.search(keyword, title + ' ' + url, re.IGNORECASE):
            return True
    
    # 检查URL是否包含数字ID（可能是文章ID）
    if re.search(r'/\d+', url):
        return True
    
    # 检查标题长度（太短可能是导航）
    if len(title) < 5:
        return False
    
    # 默认认为是文章链接
    return True

# 创建蓝图
smart_bp = Blueprint('smart', __name__)

# 配置
CRAWL_RESULTS_DIR = 'crawl_results'

# API路由：智能提取单个文章内容
@smart_bp.route('/api/extract-single-article', methods=['POST'])
def extract_single_article():
    """智能提取单个文章内容"""
    try:
        data = request.get_json(silent=True) or {}
        url = data.get('url')
        clean_content = data.get('clean_content', True)
        publish_date = data.get('publish_date')  # 接收链接中的发布日期
        
        if not url:
            return jsonify({'success': False, 'message': '缺少URL参数'})
        
        # 智能提取文章内容
        result = extract_article_content_from_url(url)
        
        if result['success']:
            content = result['content']
            title = result.get('title', '未知标题')
            
            if clean_content:
                content = clean_article_content(content, 'html')
            
            return jsonify({
                'success': True,
                'title': title,
                'content': content,
                'url': url,
                'publish_date': publish_date,  # 返回发布日期
                'content_length': len(content),
                'method': result.get('method', 'universal_smart_extraction'),
                'score': result.get('score', 0)
            })
        else:
            return jsonify({
                'success': False,
                'message': result['error'],
                'url': url
            })
            
    except Exception as e:
        return jsonify({'success': False, 'message': f'提取失败: {str(e)}'})

# API路由：智能提取任务中的文章内容
@smart_bp.route('/api/smart-extract-articles', methods=['POST'])
def smart_extract_articles():
    """智能提取任务中的文章内容"""
    try:
        data = request.get_json(silent=True) or {}
        task_id = data.get('task_id')
        clean_content = data.get('clean_content', True)
        return_json = data.get('return_json', False)  # 是否返回JSON而不是ZIP文件
        
        print(f"🧠 智能提取API被调用")
        print(f"📋 任务ID: {task_id}")
        print(f"🧹 清理内容: {clean_content}")
        print(f"📦 返回格式: {'JSON' if return_json else 'ZIP文件'}")
        
        if not task_id:
            return jsonify({'success': False, 'message': '缺少任务ID'})
        
        # 获取任务的关键词配置
        keywords = ''
        try:
            from sqlite_database import sqlite_db
            task_info = sqlite_db.get_crawl_task_by_task_id(task_id)
            if task_info and task_info.get('keywords'):
                keywords = task_info.get('keywords', '')
                print(f"🔑 任务关键词过滤: {keywords}")
        except Exception as e:
            print(f"⚠️ 获取任务关键词失败: {e}")
        
        # 获取任务详情
        detail_file = os.path.join(CRAWL_RESULTS_DIR, f"{task_id}_detail.json")
        print(f"🔍 查找任务详情文件: {detail_file}")
        
        if not os.path.exists(detail_file):
            print(f"❌ 任务详情文件不存在: {detail_file}")
            return jsonify({'success': False, 'message': '任务详情文件不存在'})
        
        print(f"✅ 任务详情文件存在，开始读取")
        with open(detail_file, 'r', encoding='utf-8') as f:
            task_detail = json.load(f)
        
        print(f"📄 任务详情加载完成")
        
        # 检查任务状态
        if isinstance(task_detail, dict):
            task_status = task_detail.get('status', '')
            print(f"📊 任务状态: {task_status}")
            
            if task_status != 'completed':
                print(f"❌ 任务未完成: {task_status}")
                return jsonify({
                    'success': False, 
                    'message': f'任务未完成，当前状态: {task_status}'
                })
            
            # 检查是否有数据
            task_data = task_detail.get('data')
            if not task_data:
                print(f"❌ 任务没有数据")
                return jsonify({
                    'success': False, 
                    'message': '任务没有聚合到任何数据'
                })
            print(f"✅ 任务数据存在，开始分析")
        else:
            print(f"❌ 任务详情格式错误")
        
        # 提取文章链接 - 优先使用 Playwright 识别分页并提取所有链接
        article_links = []
        
        print(f"🔗 开始提取文章链接")
        
        # 尝试获取原始URL
        original_url = None
        if isinstance(task_detail, dict):
            original_url = task_detail.get('url') or task_detail.get('target_url')
        
        # 方法1: 使用 Playwright 识别分页并提取所有文章链接（新增）
        if PLAYWRIGHT_AVAILABLE and original_url:
            print(f"\n{'='*70}")
            print(f"🎭 使用 Playwright 识别分页并提取文章链接")
            print(f"{'='*70}")
            print(f"原始URL: {original_url}")
            
            try:
                # 使用 Playwright 提取链接（识别分页，提取所有文章链接）
                playwright_result = extract_links_with_playwright(
                    url=original_url
                    # 不限制文章数和页数（使用函数默认值99999）
                )
                
                if playwright_result.get('success') and playwright_result.get('articles'):
                    article_links = playwright_result['articles']
                    print(f"✅ Playwright 成功提取 {len(article_links)} 个文章链接")
                    print(f"📊 统计: {playwright_result.get('stats', {})}")
                    
                    # 显示前5个链接
                    if article_links:
                        print(f"\n📰 前5个文章链接:")
                        for i, link in enumerate(article_links[:5], 1):
                            print(f"   {i}. {link['title'][:60]}...")
                            print(f"      {link['url']}")
                else:
                    print(f"⚠️ Playwright 提取失败: {playwright_result.get('error', '未知错误')}")
                    print(f"📝 将使用传统方法从 markdown 中提取链接")
            except Exception as e:
                print(f"❌ Playwright 提取出错: {e}")
                print(f"📝 将使用传统方法从 markdown 中提取链接")
        elif not PLAYWRIGHT_AVAILABLE:
            print(f"⚠️ Playwright 不可用，使用传统方法提取链接")
        elif not original_url:
            print(f"⚠️ 未找到原始URL，使用传统方法提取链接")
        
        # 方法2: 如果 Playwright 没有提取到链接，使用传统方法从 markdown 中提取
        if not article_links:
            print(f"\n{'='*70}")
            print(f"📝 使用传统方法从 markdown 中提取链接")
            print(f"{'='*70}")
        
        # 分析任务详情结构（传统方法）
        if not article_links and isinstance(task_detail, dict) and 'data' in task_detail:
            task_data = task_detail['data']
            print(f"📋 任务详情结构: 有 'data' 字段")
            
            if isinstance(task_data, dict) and 'data' in task_data:
                markdown_content = task_data['data']
                print(f"📋 任务数据结构: 有内层 'data' 字段，类型: {type(markdown_content)}")
                
                if isinstance(markdown_content, list):
                    print(f"📄 找到 {len(markdown_content)} 个内容项")
                    for idx, item in enumerate(markdown_content):
                        if isinstance(item, dict) and 'markdown' in item:
                            markdown_text = item['markdown']
                            print(f"📝 处理内容项 {idx+1}，markdown长度: {len(markdown_text)}")
                            
                            # 从markdown中提取链接
                            # 支持多种markdown链接格式
                            markdown_links = re.findall(r'\[([^\]]+)\]\(([^)]+)\)', markdown_text)
                            print(f"🔗 找到 {len(markdown_links)} 个链接")
                            
                            for link_text, link_url in markdown_links:
                                if link_url.startswith('http'):
                                    # 过滤掉明显不是文章的链接
                                    if is_article_link(link_text, link_url):
                                        article_links.append({
                                            'title': link_text.strip(),
                                            'url': link_url.strip()
                                        })
                                        print(f"✅ 添加文章链接: {link_text[:30]}... | {link_url}")
                            
                            # 也提取纯URL链接
                            url_links = re.findall(r'https?://[^\s\)]+', markdown_text)
                            for url in url_links:
                                # 清理URL（移除末尾的标点符号）
                                clean_url = re.sub(r'[.,;:!?]+$', '', url)
                                if clean_url not in [link['url'] for link in article_links]:
                                    # 从URL推断标题
                                    title = extract_title_from_url(clean_url)
                                    if is_article_link(title, clean_url):
                                        article_links.append({
                                            'title': title,
                                            'url': clean_url
                                        })
        
        # 去重
        unique_links = []
        seen_urls = set()
        for link in article_links:
            if link['url'] not in seen_urls:
                unique_links.append(link)
                seen_urls.add(link['url'])
        article_links = unique_links
        
        print(f"📝 共提取到 {len(article_links)} 个文章链接")
        if article_links:
            print(f"   示例链接: {article_links[0]['url']}")
        
        # 如果从结果文件中找不到文章链接，尝试从数据库中获取定时任务的文章
        if not article_links and task_id.startswith('schedule_'):
            print(f"🔍 检测到定时任务，尝试从数据库获取文章")
            try:
                from sqlite_database import sqlite_db
                
                # 提取schedule ID
                parts = task_id.split('_')
                if len(parts) >= 2:
                    schedule_id = parts[1]
                    
                    # 从数据库获取相关文章
                    # 获取最近一段时间内该任务相关的文章
                    recent_time = get_china_time() - timedelta(hours=2)  # 最近2小时
                    
                    # 连接数据库
                    sqlite_db.connect()
                    cursor = sqlite_db.connection.cursor()
                    
                    # 查询最近的文章 - 扩大时间范围并优化查询条件
                    recent_time_extended = get_china_time() - timedelta(hours=12)  # 扩大到12小时
                    cursor.execute("""
                        SELECT url, title, content, publish_date, extraction_method, quality_score, created_at
                        FROM articles 
                        WHERE created_at >= ? 
                        AND (
                            extraction_method = 'newspaper3k' 
                            OR extraction_method LIKE '%newspaper3k%'
                            OR quality_score >= 80
                        )
                        ORDER BY created_at DESC 
                        LIMIT 50
                    """, (recent_time_extended.strftime('%Y-%m-%d %H:%M:%S'),))
                    
                    print(f"🔍 查询条件: 时间 >= {recent_time_extended.strftime('%Y-%m-%d %H:%M:%S')}")
                    
                    db_articles = cursor.fetchall()
                    print(f"📊 从数据库找到 {len(db_articles)} 篇最近的文章")
                    
                    # 如果没有找到文章，调试查询所有最近的文章
                    if len(db_articles) == 0:
                        print(f"🔍 调试：查询所有最近的文章")
                        cursor.execute("""
                            SELECT url, title, extraction_method, quality_score, created_at
                            FROM articles 
                            ORDER BY created_at DESC 
                            LIMIT 10
                        """)
                        debug_articles = cursor.fetchall()
                        print(f"📊 调试：数据库中最近的 {len(debug_articles)} 篇文章:")
                        for i, (url, title, method, score, created_at) in enumerate(debug_articles):
                            print(f"   {i+1}. {title[:30]}... | {method} | 分数:{score} | {created_at}")
                        
                        # 如果有文章，使用更宽松的条件重新查询
                        if debug_articles:
                            print(f"🔄 使用更宽松的查询条件重新查询")
                            cursor.execute("""
                                SELECT url, title, content, publish_date, extraction_method, quality_score, created_at
                                FROM articles 
                                WHERE created_at >= ?
                                ORDER BY created_at DESC 
                                LIMIT 20
                            """, (recent_time_extended.strftime('%Y-%m-%d %H:%M:%S'),))
                            db_articles = cursor.fetchall()
                            print(f"📊 宽松条件查询到 {len(db_articles)} 篇文章")
                    
                    # 转换为文章链接格式
                    for article in db_articles:
                        url, title, content, publish_date, extraction_method, quality_score, created_at = article
                        article_links.append({
                            'title': title or '未知标题',
                            'url': url,
                            'content': content,  # 已经是newspaper3k处理过的内容
                            'publish_date': publish_date,
                            'extraction_method': extraction_method,
                            'quality_score': quality_score,
                            'created_at': created_at
                        })
                    
                    print(f"✅ 从数据库获取到 {len(article_links)} 篇文章")
                    
            except Exception as e:
                print(f"⚠️ 从数据库获取文章失败: {str(e)}")
        
        if not article_links:
            print(f"❌ 未找到文章链接")
            return jsonify({'success': False, 'message': '未找到文章链接'})
        
        # 智能提取每个文章的内容
        extracted_articles = []  # 新提取的文章（需要入库）
        all_articles = []  # 所有文章（用于下载，包括已存在的）
        success_count = 0
        skipped_count = 0
        
        # 连接数据库检查已存在的文章
        print(f"🗄️ 开始连接数据库")
        try:
            from sqlite_database import sqlite_db
            db_connected = sqlite_db.connect()
            if db_connected:
                # 确保表已创建
                sqlite_db.create_tables()
                print("📊 数据库连接成功，开始检查文章去重...")
            else:
                print("⚠️ 数据库连接失败，将提取所有文章（不进行去重）")
        except Exception as e:
            db_connected = False
            print(f"⚠️ 数据库连接异常: {e}，将提取所有文章（不进行去重）")
        
        print(f"🔄 开始处理 {len(article_links)} 个文章链接")
        print(f"📊 本次将处理全部 {len(article_links)} 篇文章（无限制）")
        
        # 🔑 关键修改：先保存所有链接到数据库
        if db_connected:
            print(f"💾 先保存所有链接到数据库...")
            link_saved_count = 0
            for link in article_links:
                try:
                    # 检查是否已存在
                    if not sqlite_db.check_article_exists(link['url']):
                        sqlite_db.add_article({
                            'url': link['url'],
                            'title': link['title'],
                            'source_url': data.get('task_url', ''),
                            'crawl_time': get_china_time().strftime('%Y-%m-%d %H:%M:%S'),
                            'extraction_method': 'link_only'
                        })
                        link_saved_count += 1
                except Exception as e:
                    print(f"⚠️ 链接入库失败: {link['url']}: {e}")
            print(f"✅ 成功保存 {link_saved_count} 个链接到数据库")
        
        for i, link in enumerate(article_links):
            try:
                # 如果文章已经包含处理过的内容（从数据库获取的），直接使用
                if 'content' in link and link['content']:
                    content = link['content']
                    title = link['title']
                    print(f"📝 使用数据库中的文章内容: {title[:30]}... (内容长度: {len(content)})")
                    
                    # ✅ 不在这里过滤关键词，只在入库时过滤
                    
                    all_articles.append({
                        'title': title,
                        'url': link['url'],
                        'content': content,
                        'content_length': len(content),
                        'method': link.get('extraction_method', 'from_database'),
                        'score': link.get('quality_score', 0),
                        'publish_date': link.get('publish_date'),
                        'created_at': link.get('created_at')
                    })
                    success_count += 1
                    continue
                
                # 先检查数据库中是否已存在该文章
                existing_article = None
                if db_connected:
                    try:
                        article_exists = sqlite_db.check_article_exists(link['url'])
                        if article_exists:
                            print(f"✅ 文章已存在，从数据库读取: {link['url']}")
                            # 从数据库读取已存在的文章
                            existing_article = sqlite_db.get_article_by_url(link['url'])
                            if existing_article:
                                content = existing_article.get('content', '')
                                # 确保内容不为空
                                if not content or len(content.strip()) < 10:
                                    content = f"文章标题: {existing_article.get('title', '未知标题')}\n\n文章链接: {link['url']}\n\n内容暂无法显示或内容过短。"
                                
                                all_articles.append({
                                    'title': existing_article.get('title', link['title']),
                                    'url': existing_article['url'],
                                    'content': content,
                                    'content_length': len(content),
                                    'method': existing_article.get('extraction_method', 'from_database'),
                                    'score': existing_article.get('quality_score', 0)
                                })
                                print(f"📝 从数据库添加文章: {existing_article.get('title', '')[:30]}... (内容长度: {len(content)})")
                            else:
                                print(f"⚠️ 数据库中未找到文章内容: {link['url']}")
                            skipped_count += 1
                            continue
                        else:
                            print(f"🆕 准备提取新文章: {link['url']}")
                    except Exception as e:
                        print(f"❌ 检查文章是否存在失败: {e}")
                
                result = extract_article_content_from_url(link['url'])
                if result['success']:
                    content = result['content']
                    title = result.get('title', link['title'])  # 使用智能提取的标题
                    
                    if clean_content:
                        # 使用轻度清理，保留更多内容
                        def light_clean_content(text):
                            """轻度清理文章内容，保留核心内容"""
                            if not text:
                                return ""
                            
                            # 只移除最基本的干扰内容
                            import re
                            
                            # 移除HTML标签（如果有）
                            text = re.sub(r'<[^>]+>', '', text)
                            
                            # 移除多余的空白字符
                            text = re.sub(r'\s+', ' ', text)
                            
                            # 移除明显的装饰性分隔线
                            text = re.sub(r'^[\s\-_=*#|]{5,}$', '', text, flags=re.MULTILINE)
                            
                            # 移除多余空行
                            text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)
                            
                            return text.strip()
                        
                        content = light_clean_content(content)
                    
                    # 检查内容质量
                    if len(content.strip()) > 100:  # 至少100个字符
                        # 获取发布日期：优先使用newspaper3k提取的，如果没有则使用Playwright提取的
                        publish_date = result.get('publish_date')
                        if not publish_date and link.get('publish_date'):
                            publish_date = link['publish_date']
                            print(f"📅 使用Playwright提取的发布日期: {publish_date}")
                        
                        # ✅ 不在这里过滤关键词，只在入库时过滤（入库时会自动检查关键词）
                        
                        article_info = {
                            'title': title,  # 使用智能提取的标题
                            'url': link['url'],
                            'content': content,
                            'content_length': len(content),
                            'method': result.get('method', 'universal_smart_extraction'),
                            'score': result.get('score', 0),
                            'publish_date': publish_date
                        }
                        
                        # 添加到新提取列表（用于入库）
                        extracted_articles.append(article_info)
                        # 也添加到所有文章列表（用于下载）
                        all_articles.append(article_info)
                        
                        # 自动入库到SQLite数据库（使用外部已建立的连接）
                        if db_connected:
                            try:
                                # 🔍 入库前关键词检查
                                match_result = get_keywords_match_result(title, content, keywords)
                                if not match_result.get('matched'):
                                    print(f"⏭️ 跳过不包含关键词的文章（入库前检查）: {title[:30]}...")
                                    skipped_count += 1
                                    continue
                                
                                # 查找来源URL信息
                                source_info = sqlite_db.find_source_url_info(link['url'])
                                
                                article_data = {
                                    'url': link['url'],
                                    'title': title,
                                    'content': content,
                                    'publish_date': publish_date,
                                    'category_id': source_info['category_id'] if source_info else None,
                                    'source_url_id': source_info['url_id'] if source_info else None,
                                    'extraction_method': 'smart_extraction',
                                    'quality_score': result.get('score', 0),
                                    'matched_keywords': match_result.get('matched_keywords_str', '')
                                }
                                
                                if source_info and source_info['category_name']:
                                    print(f"📁 自动关联分类: {source_info['category_name']}")
                                
                                article_id = sqlite_db.insert_article(article_data)
                                if article_id:
                                    print(f"✅ SQLite入库成功 (ID: {article_id}): {title[:30]}...")
                                else:
                                    print(f"❌ SQLite入库失败: {title[:30]}...")
                            except Exception as e:
                                print(f"❌ SQLite入库异常 {link['url']}: {e}")
                                import traceback
                                traceback.print_exc()
                        else:
                            print(f"⚠️ 数据库未连接，跳过入库: {title[:30]}...")
                        
                        success_count += 1
                        
            except Exception as e:
                print(f"处理链接失败 {link['url']}: {e}")
                continue
        
        # 注意：不再断开数据库连接，保持全局连接供其他功能使用
        
        if success_count == 0 and skipped_count == 0:
            return jsonify({'success': False, 'message': '没有成功提取到任何文章内容'})
        
        # 如果请求JSON响应（自动提取场景），直接返回统计信息
        if return_json:
            return jsonify({
                'success': True,
                'message': f'智能增量提取完成：新增 {success_count} 篇，跳过 {skipped_count} 篇',
                'stats': {
                    'total_links': len(article_links),
                    'success_count': success_count,
                    'skipped_count': skipped_count,
                    'failed_count': len(article_links) - success_count - skipped_count,
                    'extracted_articles': [
                        {
                            'title': article['title'],
                            'url': article['url'],
                            'content_length': article['content_length'],
                            'score': article['score'],
                            'method': article['method']
                        }
                        for article in extracted_articles
                    ]
                }
            })
        
        # 否则创建并返回ZIP文件（手动下载场景）
        # 使用 all_articles 包含所有文章（新提取的 + 数据库中已有的）
        print(f"📦 准备创建ZIP文件，包含 {len(all_articles)} 篇文章")
        
        if not all_articles:
            print("❌ 没有可下载的文章内容")
            return jsonify({'success': False, 'message': '没有可下载的文章内容'})
        
        try:
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                # 添加总览文件
                overview_content = f"智能提取文章总览\n{'='*50}\n\n"
                overview_content += f"提取时间: {get_china_time().strftime('%Y-%m-%d %H:%M:%S')}\n"
                overview_content += f"成功提取: {len(all_articles)} 篇文章\n"
                if success_count > 0:
                    overview_content += f"  - 新增文章: {success_count} 篇\n"
                if skipped_count > 0:
                    overview_content += f"  - 数据库已有: {skipped_count} 篇\n"
                overview_content += f"总链接数: {len(article_links)}\n"
                overview_content += f"提取方法: 通用智能提取\n\n"
                
                for i, article in enumerate(all_articles, 1):
                    overview_content += f"{i}. {article['title']}\n"
                    overview_content += f"   URL: {article['url']}\n"
                    overview_content += f"   内容长度: {article['content_length']} 字符\n"
                    overview_content += f"   质量分数: {article['score']}\n"
                    overview_content += f"   提取方法: {article['method']}\n\n"
                
                zip_file.writestr("00_智能提取总览.txt", overview_content.encode('utf-8'))
                print("✅ 总览文件已添加到ZIP")
                
                # 添加每篇文章
                for i, article in enumerate(all_articles, 1):
                    try:
                        article_content = f"标题: {article['title']}\n"
                        article_content += f"URL: {article['url']}\n"
                        article_content += f"提取方法: {article['method']}\n"
                        article_content += f"质量分数: {article['score']}\n"
                        article_content += f"内容长度: {article['content_length']} 字符\n"
                        article_content += f"提取时间: {get_china_time().strftime('%Y-%m-%d %H:%M:%S')}\n"
                        article_content += f"{'='*50}\n\n"
                        article_content += article['content']
                        
                        # 🔥 修复：使用标题作为文件名（移除序号前缀）
                        safe_title = "".join(c for c in article['title'] if c.isalnum() or c in (' ', '-', '_', '（', '）', '【', '】')).rstrip()
                        if not safe_title:
                            safe_title = f"article_{i}"
                        # 限制文件名长度，避免过长
                        filename = f"{safe_title[:100]}.txt"
                        
                        # 如果文件名重复，添加序号
                        counter = 1
                        original_filename = filename
                        while filename in [name for name in zip_file.namelist() if name.endswith('.txt')]:
                            filename = f"{safe_title[:100]}_{counter}.txt"
                            counter += 1
                        
                        zip_file.writestr(filename, article_content.encode('utf-8'))
                        print(f"✅ 文章 {i}/{len(all_articles)} 已添加: {safe_title[:30]}...")
                    except Exception as e:
                        print(f"❌ 添加文章 {i} 失败: {e}")
                        # 添加错误占位文件
                        error_content = f"文章 {i} 处理失败\n错误: {str(e)}"
                        zip_file.writestr(f"error_article_{i}.txt", error_content.encode('utf-8'))
            
            # ZIP创建完成
            print(f"📦 ZIP文件创建完成，包含 {len(all_articles)} 篇文章")
            
            # 返回ZIP文件供下载
            zip_buffer.seek(0)
            
            # 🔥 修复：生成文件名（域名+日期时分秒）
            task_url = task_detail.get('url', '')
            if task_url:
                # 从URL提取域名
                import urllib.parse
                parsed_url = urllib.parse.urlparse(task_url)
                domain = parsed_url.netloc.replace('www.', '')
                # 使用完整的日期时分秒
                current_date = get_china_time().strftime('%Y%m%d_%H%M%S')
                # 直接使用域名+时间戳
                filename = f'{domain}_{current_date}.zip'
            else:
                # 没有URL的情况（兜底）
                current_date = get_china_time().strftime('%Y%m%d_%H%M%S')
                filename = f'articles_{current_date}.zip'
            
            print(f"📤 准备下载ZIP文件: {filename}")
            response = send_file(
                zip_buffer,
                mimetype='application/zip',
                as_attachment=True,
                download_name=filename
            )
            # 设置文件名头，供前端使用
            response.headers['X-Suggested-Filename'] = filename
            return response
            
        except Exception as e:
            print(f"❌ 创建ZIP文件失败: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'success': False, 'message': f'创建下载文件失败: {str(e)}'})
        
    except Exception as e:
        return jsonify({'success': False, 'message': f'提取失败: {str(e)}'})


# ─────────────────────────────────────────────
# 智能站点栏目发现 API
# ─────────────────────────────────────────────
@smart_bp.route('/api/smart/discover-sections', methods=['POST'])
def discover_site_sections_api():
    """
    分析网站首页，返回与关键词最相关的内容栏目候选列表。
    Request JSON: {url: str, keywords: str}
    """
    try:
        data = request.get_json() or {}
        homepage_url = (data.get('url') or '').strip()
        keywords_raw = (data.get('keywords') or '').strip()

        if not homepage_url:
            return jsonify({'success': False, 'message': '请提供网站首页 URL'})

        keywords = [k.strip() for k in re.split(r'[,，\s]+', keywords_raw) if k.strip()]

        from smart_site_discovery import discover_site_sections
        sections = discover_site_sections(homepage_url, keywords)

        return jsonify({'success': True, 'sections': sections, 'total': len(sections)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': f'分析失败: {str(e)}'})
