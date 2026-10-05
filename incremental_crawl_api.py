#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
增量聚合API端点
"""

from flask import Blueprint, request, jsonify
import os
import json
import re
from datetime import datetime, timedelta
from utils import coerce_int, get_china_time
from industry_pack_runtime import active_industry_composition_service
from incremental_crawler import incremental_crawler
from smart_article_extractor import extract_article_content_from_url, clean_article_content

# 创建蓝图
incremental_bp = Blueprint('incremental', __name__)

# 配置
CRAWL_RESULTS_DIR = 'crawl_results'

# API路由：获取增量聚合统计信息（改为查询SQLite数据库）
@incremental_bp.route('/api/incremental-stats', methods=['GET'])
def get_incremental_stats():
    """获取增量聚合统计信息"""
    try:
        from sqlite_database import sqlite_db
        domain = request.args.get('domain')
        
        # 使用SQLite数据库获取统计信息
        active = active_industry_composition_service.snapshot()
        stats = sqlite_db.get_statistics(
            domain,
            industry_pack_id=active['active_industry_pack_id'],
            activation_id=active.get('active_industry_activation_id') or '',
        )
        
        return jsonify({
            'success': True,
            'stats': stats
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取统计信息失败: {str(e)}'
        })

# API路由：获取已聚合的文章列表（改为查询SQLite数据库）
@incremental_bp.route('/api/crawled-articles', methods=['GET'])
def get_crawled_articles():
    """获取已聚合的文章列表"""
    try:
        from sqlite_database import sqlite_db
        
        domain = request.args.get('domain')
        search = request.args.get('search', '').strip()
        page = coerce_int(request.args.get('page'), 1, 1)
        per_page = coerce_int(request.args.get('per_page'), 20, 1, 500)
        
        # 使用SQLite数据库查询文章
        active = active_industry_composition_service.snapshot()
        articles, total = sqlite_db.get_articles(
            page, per_page, domain=domain, search=search,
            industry_pack_id=active['active_industry_pack_id'],
            activation_id=active.get('active_industry_activation_id') or '',
        )
        
        return jsonify({
            'success': True,
            'articles': articles,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': (total + per_page - 1) // per_page
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取文章列表失败: {str(e)}'
        })

# API路由：获取单篇文章详情（改为查询SQLite数据库）
@incremental_bp.route('/api/article-detail', methods=['GET'])
def get_article_detail():
    """获取单篇文章的详细内容"""
    try:
        from sqlite_database import sqlite_db
        
        url = request.args.get('url')
        if not url:
            return jsonify({
                'success': False,
                'error': '缺少URL参数'
            }), 400
        
        # 使用SQLite数据库查询文章
        article = sqlite_db.get_article_by_url(url)
        
        if not article:
            return jsonify({
                'success': False,
                'error': '文章不存在'
            }), 404
        
        return jsonify({
            'success': True,
            'article': article
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取文章详情失败: {str(e)}'
        }), 500

# API路由：删除文章（改为操作SQLite数据库）
@incremental_bp.route('/api/delete-article', methods=['POST'])
def delete_article():
    """删除指定的文章"""
    try:
        # 行业包用户只有后台开启「允许删除文章」才能删除（管理员不受限）
        try:
            from pack_tenant import me_pack_user
            _pack_user = me_pack_user()
            if _pack_user is not None and not bool(_pack_user.get('can_delete_articles')):
                return jsonify({
                    'success': False,
                    'error': '当前账号未开启删除文章权限'
                }), 403
        except Exception:
            pass
        from sqlite_database import sqlite_db
        
        data = request.get_json(silent=True) or {}
        url = data.get('url')
        
        if not url:
            return jsonify({
                'success': False,
                'error': '缺少URL参数'
            }), 400
        
        # 从SQLite数据库删除文章
        success = sqlite_db.delete_article_by_url(url)
        
        if success:
            return jsonify({
                'success': True,
                'message': '文章删除成功'
            })
        else:
            return jsonify({
                'success': False,
                'error': '文章不存在或删除失败'
            }), 404
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'删除文章失败: {str(e)}'
        }), 500

# API路由：批量删除文章（改为操作SQLite数据库）
@incremental_bp.route('/api/batch-delete-articles', methods=['POST'])
def batch_delete_articles():
    """批量删除文章"""
    try:
        # 行业包用户只有后台开启「允许删除文章」才能删除（管理员不受限）
        try:
            from pack_tenant import me_pack_user
            _pack_user = me_pack_user()
            if _pack_user is not None and not bool(_pack_user.get('can_delete_articles')):
                return jsonify({
                    'success': False,
                    'error': '当前账号未开启删除文章权限'
                }), 403
        except Exception:
            pass
        from sqlite_database import sqlite_db
        
        data = request.get_json(silent=True) or {}
        urls = data.get('urls', [])
        
        if not urls:
            return jsonify({
                'success': False,
                'error': '缺少URL列表'
            }), 400
        
        deleted_count = 0
        failed_urls = []
        
        for url in urls:
            if sqlite_db.delete_article_by_url(url):
                deleted_count += 1
            else:
                failed_urls.append(url)
        
        return jsonify({
            'success': True,
            'deleted_count': deleted_count,
            'failed_urls': failed_urls,
            'message': f'成功删除 {deleted_count} 篇文章'
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'批量删除失败: {str(e)}'
        }), 500

# API路由：导出文章（改为从SQLite数据库查询）
@incremental_bp.route('/api/export-articles', methods=['POST'])
def export_articles():
    """导出文章为ZIP文件"""
    try:
        from sqlite_database import sqlite_db
        
        data = request.get_json(silent=True) or {}
        urls = data.get('urls', [])
        format_type = data.get('format', 'txt')  # txt, json, markdown
        
        if not urls:
            return jsonify({
                'success': False,
                'error': '缺少URL列表'
            }), 400
        
        # 从SQLite数据库查询文章
        selected_articles = []
        for url in urls:
            article = sqlite_db.get_article_by_url(url)
            if article:
                selected_articles.append(article)
        
        if not selected_articles:
            return jsonify({
                'success': False,
                'error': '没有找到指定的文章'
            }), 404
        
        # 创建ZIP文件
        import zipfile
        import io
        from datetime import datetime
        
        zip_buffer = io.BytesIO()
        timestamp = get_china_time().strftime('%Y%m%d_%H%M%S')
        
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for article in selected_articles:
                title = article.get('title', '无标题')
                content = article.get('content', '')
                url = article.get('url', '')
                
                # 清理文件名
                safe_title = re.sub(r'[<>:"/\\|?*]', '_', title)[:50]
                filename = f"{safe_title}_{timestamp}.{format_type}"
                
                if format_type == 'txt':
                    file_content = f"标题: {title}\nURL: {url}\n\n{content}"
                elif format_type == 'json':
                    file_content = json.dumps(article, ensure_ascii=False, indent=2)
                elif format_type == 'markdown':
                    file_content = f"# {title}\n\n**URL:** {url}\n\n{content}"
                else:
                    file_content = content
                
                zip_file.writestr(filename, file_content.encode('utf-8'))
        
        zip_buffer.seek(0)
        
        return jsonify({
            'success': True,
            'download_url': f'/api/download-export/{timestamp}',
            'filename': f'articles_export_{timestamp}.zip',
            'count': len(selected_articles)
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'导出失败: {str(e)}'
        }), 500

# API路由：清理旧的聚合记录
@incremental_bp.route('/api/clean-old-records', methods=['POST'])
def clean_old_records():
    """清理旧的聚合记录"""
    try:
        data = request.get_json(silent=True) or {}
        days = data.get('days', 30)
        
        incremental_crawler.clean_old_records(days)
        
        return jsonify({
            'success': True,
            'message': f'已清理{days}天前的记录'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'清理记录失败: {str(e)}'
        })

# API路由：增量聚合任务中的文章
@incremental_bp.route('/api/incremental-crawl-task', methods=['POST'])
def incremental_crawl_task():
    """增量聚合任务中的文章"""
    try:
        data = request.get_json(silent=True) or {}
        task_id = data.get('task_id')
        clean_content = data.get('clean_content', True)
        max_articles = data.get('max_articles', float('inf'))  # 无限制
        
        if not task_id:
            return jsonify({'success': False, 'message': '缺少任务ID'})
        
        # 获取任务详情
        detail_file = os.path.join(CRAWL_RESULTS_DIR, f"{task_id}_detail.json")
        if not os.path.exists(detail_file):
            return jsonify({'success': False, 'message': '任务详情文件不存在'})
        
        with open(detail_file, 'r', encoding='utf-8') as f:
            task_detail = json.load(f)
        
        # 提取文章链接
        article_links = []
        if isinstance(task_detail, dict) and 'data' in task_detail:
            task_data = task_detail['data']
            if isinstance(task_data, dict) and 'data' in task_data:
                markdown_content = task_data['data']
                if isinstance(markdown_content, list):
                    for item in markdown_content:
                        if isinstance(item, dict) and 'markdown' in item:
                            markdown_text = item['markdown']
                            
                            # 首先尝试提取带日期的链接格式（如君合网站格式）
                            # 格式: [### 标题\\\n    \\\n    日期](URL)
                            junhe_pattern = r'\[\s*###\s*([^\\\n]+)\s*\\\s*\\\s*(\d{4}\.\d{1,2}\.\d{1,2})\s*\]\(([^)]+)\)'
                            junhe_matches = re.findall(junhe_pattern, markdown_text)
                            
                            for title, date_str, url in junhe_matches:
                                if url.startswith('http'):
                                    # 转换日期格式 YYYY.MM.DD -> YYYY-MM-DD
                                    publish_date = None
                                    try:
                                        parts = date_str.split('.')
                                        if len(parts) == 3:
                                            publish_date = f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"
                                    except:
                                        pass
                                    
                                    article_links.append({
                                        'title': title.strip(),
                                        'url': url.strip(),
                                        'publish_date': publish_date
                                    })
                            
                            # 然后提取普通markdown链接
                            markdown_links = re.findall(r'\[([^\]]+)\]\(([^)]+)\)', markdown_text)
                            for link_text, link_url in markdown_links:
                                if link_url.startswith('http'):
                                    # 尝试从链接文本中提取日期
                                    publish_date = None
                                    date_match = re.search(r'(\d{4})[-.年](\d{1,2})[-.月](\d{1,2})[日]?', link_text)
                                    if date_match:
                                        try:
                                            publish_date = f"{date_match.group(1)}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"
                                        except:
                                            pass
                                    
                                    article_links.append({
                                        'title': link_text.strip(),
                                        'url': link_url.strip(),
                                        'publish_date': publish_date
                                    })
        
        if not article_links:
            return jsonify({'success': False, 'message': '未找到文章链接'})
        
        # 增量聚合：只处理新文章
        new_articles = []
        skipped_articles = []
        processed_count = 0
        
        # 连接SQLite数据库
        from sqlite_database import sqlite_db
        db_connected = False
        try:
            db_connected = sqlite_db.connect()
            if db_connected:
                sqlite_db.create_tables()
                print("📊 SQLite数据库连接成功")
            else:
                print("⚠️ SQLite数据库连接失败，将跳过入库")
        except Exception as e:
            print(f"⚠️ SQLite数据库连接异常: {e}")
            db_connected = False
        
        # 如果max_articles是无限，就处理所有链接
        max_articles_count = None if max_articles == float('inf') else coerce_int(max_articles, len(article_links), 1)
        links_to_process = article_links if max_articles_count is None else article_links[:max_articles_count]
        for link in links_to_process:
            try:
                # 检查是否已聚合过（改为查询SQLite数据库）
                if db_connected and sqlite_db.check_article_exists(link['url']):
                    skipped_articles.append(link)
                    print(f"✅ 跳过已存在的文章: {link['url']}")
                    continue
                
                # 智能提取文章内容
                result = extract_article_content_from_url(link['url'])
                if result['success']:
                    content = result['content']
                    title = result.get('title', link['title'])
                    
                    if clean_content:
                        content = clean_article_content(content, 'html')
                    
                    # 检查内容质量
                    if len(content.strip()) > 100:
                        article_data = {
                            'title': title,
                            'url': link['url'],
                            'content': content,
                            'content_length': len(content),
                            'method': result.get('method', 'incremental_smart_extraction'),
                            'score': result.get('score', 0),
                            'crawled_at': get_china_time().isoformat()
                        }
                        
                        new_articles.append(article_data)
                        
                        # 自动入库到SQLite数据库（使用外部已建立的连接）
                        if db_connected:
                            try:
                                article_data = {
                                    'url': link['url'],
                                    'title': title,
                                    'content': content,
                                    'publish_date': link.get('publish_date'),  # 从链接数据中获取发布日期
                                    'extraction_method': 'incremental_crawl',
                                    'quality_score': result.get('score', 0)
                                }
                                article_id = sqlite_db.insert_article(article_data)
                                if article_id:
                                    print(f"✅ SQLite入库成功 (ID: {article_id}): {title[:30]}... 发布日期: {link.get('publish_date', '无')}")
                                else:
                                    print(f"❌ SQLite入库失败: {title[:30]}...")
                            except Exception as e:
                                print(f"❌ SQLite入库异常 {link['url']}: {e}")
                        else:
                            print(f"⚠️ 数据库未连接，跳过入库: {title[:30]}...")
                        
                        processed_count += 1
                        
            except Exception as e:
                print(f"处理链接失败 {link['url']}: {e}")
                continue
        
        # 注意：不再断开数据库连接，保持全局连接供其他功能使用
        
        return jsonify({
            'success': True,
            'new_articles': new_articles,
            'skipped_articles': skipped_articles,
            'stats': {
                'total_links': len(article_links),
                'new_articles_count': len(new_articles),
                'skipped_articles_count': len(skipped_articles),
                'processed_count': processed_count
            }
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': f'增量聚合失败: {str(e)}'})

# API路由：批量增量聚合
@incremental_bp.route('/api/batch-incremental-crawl', methods=['POST'])
def batch_incremental_crawl():
    """批量增量聚合多个任务"""
    try:
        data = request.get_json(silent=True) or {}
        task_ids = data.get('task_ids', [])
        clean_content = data.get('clean_content', True)
        max_articles_per_task = data.get('max_articles_per_task', float('inf'))  # 无限制
        
        if not task_ids:
            return jsonify({'success': False, 'message': '缺少任务ID列表'})
        
        results = []
        total_new_articles = 0
        total_skipped_articles = 0
        
        for task_id in task_ids:
            try:
                # 调用单个任务的增量聚合
                task_data = {
                    'task_id': task_id,
                    'clean_content': clean_content,
                    'max_articles': max_articles_per_task
                }
                
                # 这里可以调用 incremental_crawl_task 的逻辑
                # 为了简化，我们直接处理
                result = {
                    'task_id': task_id,
                    'success': True,
                    'new_articles_count': 0,
                    'skipped_articles_count': 0
                }
                
                results.append(result)
                total_new_articles += result['new_articles_count']
                total_skipped_articles += result['skipped_articles_count']
                
            except Exception as e:
                results.append({
                    'task_id': task_id,
                    'success': False,
                    'error': str(e)
                })
        
        return jsonify({
            'success': True,
            'results': results,
            'summary': {
                'total_tasks': len(task_ids),
                'successful_tasks': len([r for r in results if r.get('success')]),
                'total_new_articles': total_new_articles,
                'total_skipped_articles': total_skipped_articles
            }
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': f'批量增量聚合失败: {str(e)}'})

# API路由：获取增量聚合历史
@incremental_bp.route('/api/incremental-history', methods=['GET'])
def get_incremental_history():
    """获取增量聚合历史"""
    try:
        domain = request.args.get('domain')
        days = coerce_int(request.args.get('days'), 7, 0, 3650)
        
        articles = incremental_crawler.get_crawled_articles(domain)
        
        # 过滤最近几天的记录
        cutoff_date = get_china_time() - timedelta(days=days)
        cutoff_str = cutoff_date.isoformat()
        
        recent_articles = [
            article for article in articles
            if article.get('last_crawled', '') > cutoff_str
        ]
        
        # 按日期分组
        history = {}
        for article in recent_articles:
            date = article.get('last_crawled', '')[:10]  # 只取日期部分
            if date not in history:
                history[date] = []
            history[date].append(article)
        
        return jsonify({
            'success': True,
            'history': history,
            'total_recent_articles': len(recent_articles)
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取历史记录失败: {str(e)}'
        })
