#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
定时任务执行记录管理API模块
提供定时任务执行记录的管理和持久化功能
"""

from flask import Blueprint, request, jsonify
from sqlite_database import sqlite_db
from datetime import datetime
import json
from utils import coerce_int

# 创建蓝图
schedule_execution_bp = Blueprint('schedule_execution', __name__, url_prefix='/api/schedule-executions')

@schedule_execution_bp.route('', methods=['GET'])
def get_executions():
    """获取执行记录列表"""
    try:
        page = coerce_int(request.args.get('page'), 1, 1)
        per_page = coerce_int(request.args.get('per_page'), 20, 1, 500)
        status = request.args.get('status')
        schedule_id = request.args.get('schedule_id')
        
        executions, total = sqlite_db.get_task_executions(
            page=page, 
            per_page=per_page, 
            status=status,
            schedule_id=schedule_id
        )
        
        return jsonify({
            'success': True,
            'executions': executions,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': (total + per_page - 1) // per_page
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取执行记录失败: {str(e)}'
        }), 500

@schedule_execution_bp.route('', methods=['POST'])
def add_execution():
    """添加新的执行记录"""
    try:
        data = request.get_json(silent=True) or {}
        
        if not data or not data.get('schedule_id'):
            return jsonify({
                'success': False,
                'error': '缺少定时任务ID'
            }), 400
        
        execution_id = sqlite_db.insert_task_execution(data)
        
        if execution_id:
            return jsonify({
                'success': True,
                'execution_id': execution_id,
                'message': '执行记录添加成功'
            })
        else:
            return jsonify({
                'success': False,
                'error': '执行记录添加失败'
            }), 400
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'添加执行记录失败: {str(e)}'
        }), 500

@schedule_execution_bp.route('/<string:execution_id>', methods=['PUT'])
def update_execution(execution_id):
    """更新执行记录"""
    try:
        data = request.get_json(silent=True) or {}
        
        if not data:
            return jsonify({
                'success': False,
                'error': '缺少更新数据'
            }), 400
        
        success = sqlite_db.update_task_execution(execution_id, data)
        
        if success:
            return jsonify({
                'success': True,
                'message': '执行记录更新成功'
            })
        else:
            return jsonify({
                'success': False,
                'error': '执行记录更新失败'
            }), 500
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'更新执行记录失败: {str(e)}'
        }), 500

@schedule_execution_bp.route('/<string:execution_id>', methods=['DELETE'])
def delete_execution(execution_id):
    """删除单个执行记录"""
    try:
        print(f"🗑️ 尝试删除执行记录: {execution_id} (类型: {type(execution_id)})")
        
        success = sqlite_db.delete_task_execution(execution_id)
        
        print(f"🗑️ 删除结果: {success}")
        
        if success:
            return jsonify({
                'success': True,
                'message': f'执行记录删除成功: {execution_id}'
            })
        else:
            return jsonify({
                'success': False,
                'error': f'执行记录删除失败: 未找到ID为 {execution_id} 的记录'
            }), 500
            
    except Exception as e:
        print(f"❌ 删除执行记录异常: {e}")
        return jsonify({
            'success': False,
            'error': f'删除执行记录失败: {str(e)}'
        }), 500

@schedule_execution_bp.route('', methods=['DELETE'])
def clear_executions():
    """清空所有执行记录"""
    try:
        success = sqlite_db.clear_task_executions()
        
        if success:
            return jsonify({
                'success': True,
                'message': '所有执行记录已清空'
            })
        else:
            return jsonify({
                'success': False,
                'error': '清空执行记录失败'
            }), 500
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'清空执行记录失败: {str(e)}'
        }), 500

@schedule_execution_bp.route('/statistics', methods=['GET'])
def get_execution_statistics():
    """获取执行记录统计信息（支持筛选）"""
    try:
        # 🔥 修复：接收筛选参数
        status = request.args.get('status')
        schedule_id = request.args.get('schedule_id')
        
        executions, total = sqlite_db.get_task_executions(
            page=1, 
            per_page=10000,
            status=status,
            schedule_id=schedule_id
        )
        
        # 按状态统计
        status_stats = {}
        for execution in executions:
            status = execution.get('status', 'unknown')
            status_stats[status] = status_stats.get(status, 0) + 1
        
        # 按日期统计（最近7天）
        from datetime import datetime, timedelta
        
        today = datetime.now().date()
        daily_stats = {}
        
        for i in range(7):
            date = today - timedelta(days=i)
            daily_stats[date.strftime('%Y-%m-%d')] = 0
        
        for execution in executions:
            if execution.get('created_at'):
                try:
                    exec_date = datetime.fromisoformat(execution['created_at']).date()
                    date_str = exec_date.strftime('%Y-%m-%d')
                    if date_str in daily_stats:
                        daily_stats[date_str] += 1
                except:
                    continue
        
        # 成功率统计
        # 🔥 修复：使用'success'而不是'completed'
        success_count = status_stats.get('success', 0) + status_stats.get('completed', 0)
        needs_review_count = status_stats.get('needs_review', 0)
        failed_count = status_stats.get('failed', 0) + status_stats.get('error', 0)
        total_finished = success_count + needs_review_count + failed_count
        success_rate = (success_count / total_finished * 100) if total_finished > 0 else 0
        
        return jsonify({
            'success': True,
            'statistics': {
                'total_executions': total,
                'status_distribution': status_stats,
                'daily_executions': daily_stats,
                'success_rate': round(success_rate, 2),
                'success_count': success_count,
                'needs_review_count': needs_review_count,
                'failed_count': failed_count,
                'running_count': status_stats.get('running', 0)
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取统计信息失败: {str(e)}'
        }), 500

@schedule_execution_bp.route('/by-schedule/<string:schedule_id>', methods=['GET'])
def get_executions_by_schedule(schedule_id):
    """获取指定定时任务的执行记录"""
    try:
        page = coerce_int(request.args.get('page'), 1, 1)
        per_page = coerce_int(request.args.get('per_page'), 20, 1, 500)
        
        executions, total = sqlite_db.get_task_executions(
            page=page, 
            per_page=per_page, 
            schedule_id=schedule_id
        )
        
        return jsonify({
            'success': True,
            'executions': executions,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': (total + per_page - 1) // per_page,
            'schedule_id': schedule_id
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取执行记录失败: {str(e)}'
        }), 500

@schedule_execution_bp.route('/recent/<int:limit>', methods=['GET'])
def get_recent_executions(limit=10):
    """获取最近的执行记录"""
    try:
        # 限制最大数量
        if limit > 100:
            limit = 100
            
        executions, total = sqlite_db.get_task_executions(1, limit)
        
        return jsonify({
            'success': True,
            'executions': executions,
            'total': total,
            'limit': limit
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取最近执行记录失败: {str(e)}'
        }), 500

