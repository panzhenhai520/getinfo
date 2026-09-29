#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
分类管理API模块
提供分类的增删改查功能
"""

from flask import Blueprint, request, jsonify
from sqlite_database import sqlite_db

# 创建蓝图
category_bp = Blueprint('category', __name__, url_prefix='/api/categories')


def _json_data():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}

@category_bp.route('', methods=['GET'])
def get_categories():
    """获取分类列表"""
    try:
        is_active = request.args.get('is_active')
        if is_active is not None:
            is_active = is_active.lower() == 'true'
        
        categories = sqlite_db.get_categories(is_active=is_active)
        
        return jsonify({
            'success': True,
            'categories': categories
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取分类列表失败: {str(e)}'
        }), 500

@category_bp.route('', methods=['POST'])
def add_category():
    """添加分类"""
    try:
        data = _json_data()
        
        if not data or not data.get('name'):
            return jsonify({
                'success': False,
                'error': '分类名称不能为空'
            }), 400
        
        category_id = sqlite_db.insert_category(data)
        
        if category_id:
            return jsonify({
                'success': True,
                'category_id': category_id,
                'message': '分类添加成功'
            })
        else:
            return jsonify({
                'success': False,
                'error': '分类添加失败（可能已存在）'
            }), 400
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'添加分类失败: {str(e)}'
        }), 500

@category_bp.route('/<int:category_id>', methods=['PUT'])
def update_category(category_id):
    """更新分类"""
    try:
        data = _json_data()
        
        if not data:
            return jsonify({
                'success': False,
                'error': '缺少更新数据'
            }), 400
        
        success = sqlite_db.update_category(category_id, data)
        
        if success:
            return jsonify({
                'success': True,
                'message': '分类更新成功'
            })
        else:
            return jsonify({
                'success': False,
                'error': '分类更新失败'
            }), 400
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'更新分类失败: {str(e)}'
        }), 500

@category_bp.route('/<int:category_id>', methods=['DELETE'])
def delete_category(category_id):
    """删除分类"""
    try:
        success = sqlite_db.delete_category(category_id)
        
        if success:
            return jsonify({
                'success': True,
                'message': '分类删除成功'
            })
        else:
            return jsonify({
                'success': False,
                'error': '分类删除失败'
            }), 400
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'删除分类失败: {str(e)}'
        }), 500

@category_bp.route('/<int:category_id>', methods=['GET'])
def get_category(category_id):
    """获取单个分类详情"""
    try:
        category = sqlite_db.get_category_by_id(category_id)
        
        if category:
            return jsonify({
                'success': True,
                'category': category
            })
        else:
            return jsonify({
                'success': False,
                'error': '分类不存在'
            }), 404
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取分类失败: {str(e)}'
        }), 500

