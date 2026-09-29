#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
聚合任务管理API模块
提供聚合任务管理和持久化功能
"""

from flask import Blueprint, request, jsonify
from sqlite_database import sqlite_db
from datetime import datetime
from utils import coerce_int
from industry_pack_runtime import active_industry_identity

# 创建蓝图
crawl_task_bp = Blueprint('crawl_task', __name__, url_prefix='/api/crawl-tasks')

@crawl_task_bp.route('/tasks', methods=['GET'])
def get_tasks():
    """获取聚合任务列表"""
    try:
        page = coerce_int(request.args.get('page'), 1, 1)
        per_page = coerce_int(request.args.get('per_page'), 20, 1, 500)
        status = request.args.get('status')
        
        industry_pack_id = str(
            request.args.get('industry_pack_id')
            or active_industry_identity().get('id')
            or 'family_office'
        )
        tasks, total = sqlite_db.get_crawl_tasks(
            page, per_page, status, industry_pack_id=industry_pack_id
        )
        
        return jsonify({
            'success': True,
            'tasks': tasks,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': (total + per_page - 1) // per_page
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取任务列表失败: {str(e)}'
        }), 500

@crawl_task_bp.route('/tasks', methods=['POST'])
def create_task():
    """创建新聚合任务"""
    try:
        data = request.get_json(silent=True) or {}
        
        if not data or not data.get('task_id'):
            return jsonify({
                'success': False,
                'error': '缺少任务ID'
            }), 400
        
        task_db_id = sqlite_db.insert_crawl_task(data)
        
        if task_db_id:
            return jsonify({
                'success': True,
                'task_db_id': task_db_id,
                'task_id': data.get('task_id'),
                'message': '聚合任务创建成功'
            })
        else:
            return jsonify({
                'success': False,
                'error': '任务ID已存在或创建失败'
            }), 400
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'创建任务失败: {str(e)}'
        }), 500

@crawl_task_bp.route('/tasks/<task_id>', methods=['GET'])
def get_task(task_id):
    """获取单个聚合任务详情"""
    try:
        task = sqlite_db.get_crawl_task_by_task_id(task_id)
        
        if task:
            return jsonify({
                'success': True,
                'task': task
            })
        else:
            return jsonify({
                'success': False,
                'error': '任务不存在'
            }), 404
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取任务失败: {str(e)}'
        }), 500

@crawl_task_bp.route('/tasks/<task_id>/status', methods=['PUT'])
def update_task_status(task_id):
    """更新聚合任务状态"""
    try:
        data = request.get_json(silent=True) or {}
        
        if not data or 'status' not in data:
            return jsonify({
                'success': False,
                'error': '缺少状态参数'
            }), 400
        
        success = sqlite_db.update_crawl_task_status(
            task_id,
            data.get('status'),
            progress=data.get('progress'),
            articles_found=data.get('articles_found'),
            articles_processed=data.get('articles_processed'),
            error_message=data.get('error_message')
        )
        
        if success:
            return jsonify({
                'success': True,
                'message': '任务状态更新成功'
            })
        else:
            return jsonify({
                'success': False,
                'error': '任务状态更新失败'
            }), 500
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'更新状态失败: {str(e)}'
        }), 500

@crawl_task_bp.route('/statistics', methods=['GET'])
def get_task_statistics():
    """获取聚合任务统计信息"""
    try:
        industry_pack_id = str(
            request.args.get('industry_pack_id')
            or active_industry_identity().get('id')
            or 'family_office'
        )
        tasks, total = sqlite_db.get_crawl_tasks(
            1, 10000, industry_pack_id=industry_pack_id
        )
        
        # 按状态统计
        status_stats = {}
        for task in tasks:
            status = task.get('status', 'unknown')
            status_stats[status] = status_stats.get(status, 0) + 1
        
        # 按模式统计
        mode_stats = {}
        for task in tasks:
            mode = task.get('crawl_mode', 'unknown')
            mode_stats[mode] = mode_stats.get(mode, 0) + 1
        
        # 统计文章数
        total_articles_found = sum(task.get('articles_found', 0) for task in tasks)
        total_articles_processed = sum(task.get('articles_processed', 0) for task in tasks)
        
        # 成功率
        completed = status_stats.get('completed', 0)
        failed = status_stats.get('failed', 0)
        total_finished = completed + failed
        success_rate = (completed / total_finished * 100) if total_finished > 0 else 0
        
        return jsonify({
            'success': True,
            'statistics': {
                'total_tasks': total,
                'status_stats': status_stats,
                'mode_stats': mode_stats,
                'total_articles_found': total_articles_found,
                'total_articles_processed': total_articles_processed,
                'completed_tasks': completed,
                'failed_tasks': failed,
                'success_rate': round(success_rate, 2)
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取统计信息失败: {str(e)}'
        }), 500

@crawl_task_bp.route('/tasks/<task_id>/articles', methods=['GET'])
def get_task_articles(task_id):
    """获取任务关联的文章（暂时返回空，后续可扩展）"""
    try:
        return jsonify({
            'success': True,
            'articles': [],
            'total': 0
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取文章失败: {str(e)}'
        }), 500

