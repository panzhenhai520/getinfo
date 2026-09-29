#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
URL管理验证辅助工具
用于确保定时任务的URL来源一致性
"""

import re
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse, urlunparse
from outbound_url_policy import validate_outbound_url


def normalize_task_url(url: str) -> str:
    """Normalize a user-entered task URL without changing meaningful path/query."""
    value = str(url or '').strip()
    if not value:
        return ''

    # Fix accidental double schemes such as https://https://example.com.
    while re.match(r'^(https?://)(https?://)', value, flags=re.IGNORECASE):
        value = re.sub(r'^(https?://)(https?://)', r'\2', value, count=1, flags=re.IGNORECASE)

    if not re.match(r'^https?://', value, flags=re.IGNORECASE):
        value = 'https://' + value

    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return ''

    return urlunparse(parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower()))


def validate_http_url(url: str) -> Tuple[bool, str, str]:
    """Return (is_valid, normalized_url, message) for managed/task URLs."""
    normalized = normalize_task_url(url)
    if not normalized:
        return False, '', 'URL格式无效'

    verdict = validate_outbound_url(normalized, resolve_dns=True)
    if not verdict.allowed:
        return False, verdict.normalized_url or normalized, f'URL不允许访问: {verdict.reason}'
    return True, verdict.normalized_url, ''

def validate_task_url_consistency(task_data: Dict) -> Tuple[bool, str, Optional[str]]:
    """
    验证定时任务的URL一致性
    
    Args:
        task_data: 定时任务数据
        
    Returns:
        Tuple[bool, str, Optional[str]]: (是否有效, 错误信息, 最终URL)
    """
    url_id = task_data.get('url_id')
    target_url = normalize_task_url(task_data.get('target_url'))
    
    # 情况1: 两者都为空
    if not url_id and not target_url:
        return False, '必须提供url_id或target_url其中之一', None
    
    # 情况2: 两者都有值 - 警告但允许（使用url_id优先）
    if url_id and target_url:
        return True, '', None  # 由数据库查询url_id对应的URL
    
    # 情况3: 只有url_id - 最佳实践
    if url_id and not target_url:
        return True, '', None  # 由数据库查询url_id对应的URL
    
    # 情况4: 只有target_url - 临时URL
    if not url_id and target_url:
        return True, '', target_url
    
    return False, '未知错误', None

def get_effective_url(task_data: Dict, sqlite_db) -> Optional[str]:
    """
    获取定时任务的有效URL
    
    优先级：url_id > target_url
    
    Args:
        task_data: 定时任务数据
        sqlite_db: 数据库实例
        
    Returns:
        Optional[str]: 有效的URL，如果无法获取则返回None
    """
    url_id = task_data.get('url_id')
    target_url = normalize_task_url(task_data.get('target_url'))
    
    # 优先使用url_id
    if url_id:
        try:
            managed_url = sqlite_db.get_managed_url_by_id(url_id)
            if managed_url:
                return managed_url.get('url')
        except Exception as e:
            print(f"⚠️  获取url_id对应的URL失败: {e}")
    
    # 回退到target_url
    if target_url:
        return target_url
    
    return None

def suggest_url_source(task_data: Dict) -> str:
    """
    建议URL来源的最佳实践
    
    Args:
        task_data: 定时任务数据
        
    Returns:
        str: 建议信息
    """
    url_id = task_data.get('url_id')
    target_url = normalize_task_url(task_data.get('target_url'))
    
    if url_id and target_url:
        return "⚠️  建议：同时设置了url_id和target_url，系统将优先使用url_id"
    elif url_id and not target_url:
        return "✅ 推荐：使用URL管理列表中的URL（url_id）"
    elif not url_id and target_url:
        return "ℹ️  使用临时URL（target_url），建议添加到URL管理列表以便复用"
    else:
        return "❌ 错误：必须提供URL来源"

# 使用示例
if __name__ == '__main__':
    # 测试用例
    test_cases = [
        {'url_id': 1, 'target_url': None},
        {'url_id': None, 'target_url': 'https://example.com'},
        {'url_id': 1, 'target_url': 'https://example.com'},
        {'url_id': None, 'target_url': None},
    ]
    
    for i, case in enumerate(test_cases, 1):
        print(f"\n测试用例 {i}: {case}")
        is_valid, error, url = validate_task_url_consistency(case)
        print(f"  有效性: {is_valid}")
        if error:
            print(f"  错误: {error}")
        print(f"  建议: {suggest_url_source(case)}")

