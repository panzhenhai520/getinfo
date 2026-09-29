#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SQLite数据库API模块
提供RESTful接口访问SQLite数据库
"""

from flask import Blueprint, request, jsonify
from sqlite_database import sqlite_db
import json
from datetime import datetime
from utils import coerce_int
from industry_pack_runtime import active_industry_composition_service

# 创建蓝图
sqlite_bp = Blueprint('sqlite', __name__)

# API路由：获取文章列表
@sqlite_bp.route('/api/sqlite/articles', methods=['GET'])
def get_articles():
    """获取文章列表"""
    try:
        page = coerce_int(request.args.get('page'), 1, 1)
        per_page = coerce_int(request.args.get('per_page'), 20, 1, 500)
        domain = request.args.get('domain')
        search = request.args.get('search')
        
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
        }), 500

# API路由：获取文章详情
@sqlite_bp.route('/api/sqlite/article/<int:article_id>', methods=['GET'])
def get_article_by_id(article_id):
    """根据ID获取文章详情"""
    try:
        article = sqlite_db.get_article_by_id(article_id)
        if article:
            return jsonify({
                'success': True,
                'article': article
            })
        else:
            return jsonify({
                'success': False,
                'error': '文章不存在'
            }), 404
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取文章详情失败: {str(e)}'
        }), 500

# API路由：根据URL获取文章详情
@sqlite_bp.route('/api/sqlite/article-by-url', methods=['GET'])
def get_article_by_url():
    """根据URL获取文章详情"""
    try:
        url = request.args.get('url')
        if not url:
            return jsonify({
                'success': False,
                'error': '缺少URL参数'
            }), 400
        
        article = sqlite_db.get_article_by_url(url)
        if article:
            return jsonify({
                'success': True,
                'article': article
            })
        else:
            return jsonify({
                'success': False,
                'error': '文章不存在'
            }), 404
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取文章详情失败: {str(e)}'
        }), 500

# API路由：插入文章
@sqlite_bp.route('/api/sqlite/articles', methods=['POST'])
def insert_article():
    """插入文章"""
    try:
        data = request.get_json(silent=True) or {}
        if not data:
            return jsonify({
                'success': False,
                'error': '缺少文章数据'
            }), 400
        
        article_id = sqlite_db.insert_article(data)
        if article_id:
            return jsonify({
                'success': True,
                'article_id': article_id,
                'message': '文章插入成功'
            })
        else:
            return jsonify({
                'success': False,
                'error': '文章插入失败'
            }), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'插入文章失败: {str(e)}'
        }), 500

# API路由：更新文章
@sqlite_bp.route('/api/sqlite/articles/<int:article_id>', methods=['PUT'])
def update_article(article_id):
    """更新文章"""
    try:
        data = request.get_json(silent=True) or {}
        if not data:
            return jsonify({
                'success': False,
                'error': '缺少文章数据'
            }), 400
        
        success = sqlite_db.update_article(article_id, data)
        if success:
            return jsonify({
                'success': True,
                'message': '文章更新成功'
            })
        else:
            return jsonify({
                'success': False,
                'error': '文章更新失败'
            }), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'更新文章失败: {str(e)}'
        }), 500

# API路由：删除文章
@sqlite_bp.route('/api/sqlite/articles/<int:article_id>', methods=['DELETE'])
def delete_article(article_id):
    """删除文章"""
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
        success = sqlite_db.delete_article(article_id)
        if success:
            return jsonify({
                'success': True,
                'message': '文章删除成功'
            })
        else:
            return jsonify({
                'success': False,
                'error': '文章删除失败'
            }), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'删除文章失败: {str(e)}'
        }), 500

# API路由：根据URL删除文章
@sqlite_bp.route('/api/sqlite/articles-by-url', methods=['DELETE'])
def delete_article_by_url():
    """根据URL删除文章"""
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
        data = request.get_json(silent=True) or {}
        url = data.get('url') if data else request.args.get('url')
        
        if not url:
            return jsonify({
                'success': False,
                'error': '缺少URL参数'
            }), 400
        
        success = sqlite_db.delete_article_by_url(url)
        if success:
            return jsonify({
                'success': True,
                'message': '文章删除成功'
            })
        else:
            return jsonify({
                'success': False,
                'error': '文章删除失败'
            }), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'删除文章失败: {str(e)}'
        }), 500

# API路由：批量删除文章
@sqlite_bp.route('/api/sqlite/articles/batch-delete', methods=['POST'])
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
        data = request.get_json(silent=True) or {}
        article_ids = data.get('article_ids', [])
        urls = data.get('urls', [])
        
        if not article_ids and not urls:
            return jsonify({
                'success': False,
                'error': '缺少文章ID或URL列表'
            }), 400
        
        deleted_count = 0
        failed_items = []
        
        # 删除指定ID的文章
        for article_id in article_ids:
            if sqlite_db.delete_article(article_id):
                deleted_count += 1
            else:
                failed_items.append(f"ID: {article_id}")
        
        # 删除指定URL的文章
        for url in urls:
            if sqlite_db.delete_article_by_url(url):
                deleted_count += 1
            else:
                failed_items.append(f"URL: {url}")
        
        return jsonify({
            'success': True,
            'deleted_count': deleted_count,
            'failed_items': failed_items,
            'message': f'成功删除 {deleted_count} 篇文章'
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'批量删除失败: {str(e)}'
        }), 500

# API路由：获取统计信息
@sqlite_bp.route('/api/sqlite/statistics', methods=['GET'])
def get_statistics():
    """获取统计信息"""
    try:
        domain = request.args.get('domain')
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
        }), 500

# API路由：检查文章是否存在
@sqlite_bp.route('/api/sqlite/articles/exists', methods=['GET'])
def check_article_exists():
    """检查文章是否存在"""
    try:
        url = request.args.get('url')
        if not url:
            return jsonify({
                'success': False,
                'error': '缺少URL参数'
            }), 400
        
        exists = sqlite_db.is_article_exists(url)
        return jsonify({
            'success': True,
            'exists': exists
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'检查文章存在性失败: {str(e)}'
        }), 500

# API路由：初始化数据库
@sqlite_bp.route('/api/sqlite/init', methods=['POST'])
def init_database():
    """初始化数据库"""
    try:
        success = sqlite_db.create_tables()
        if success:
            return jsonify({
                'success': True,
                'message': 'SQLite数据库初始化成功'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'SQLite数据库初始化失败'
            }), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'SQLite数据库初始化失败: {str(e)}'
        }), 500

# API路由：测试数据库连接
@sqlite_bp.route('/api/sqlite/test-connection', methods=['GET'])
def test_connection():
    """测试数据库连接"""
    try:
        if sqlite_db.connect():
            sqlite_db.disconnect()
            return jsonify({
                'success': True,
                'message': 'SQLite数据库连接成功'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'SQLite数据库连接失败'
            }), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'SQLite数据库连接测试失败: {str(e)}'
        }), 500

# API路由：获取数据库信息
@sqlite_bp.route('/api/sqlite/info', methods=['GET'])
def get_database_info():
    """获取数据库信息"""
    try:
        import os
        db_path = sqlite_db.db_path
        db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
        
        return jsonify({
            'success': True,
            'info': {
                'db_path': db_path,
                'db_size': db_size,
                'db_size_mb': round(db_size / (1024 * 1024), 2),
                'exists': os.path.exists(db_path)
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取数据库信息失败: {str(e)}'
        }), 500
