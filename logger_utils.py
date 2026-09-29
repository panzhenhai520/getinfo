#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
日志工具模块
提供统一的日志记录功能
"""

import logging
import traceback
from datetime import datetime
from utils import get_china_time

# 配置日志格式
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger(__name__)

def log_error(error: Exception, context: str = "", request_data: dict = None):
    """
    记录详细的错误日志
    
    Args:
        error: 异常对象
        context: 错误上下文描述
        request_data: 请求数据（可选）
    """
    timestamp = get_china_time().strftime('%Y-%m-%d %H:%M:%S')
    
    error_msg = f"""
{'='*80}
❌ 错误时间: {timestamp}
📍 错误位置: {context}
⚠️  错误类型: {type(error).__name__}
💬 错误信息: {str(error)}
"""
    
    if request_data:
        error_msg += f"📦 请求数据: {request_data}\n"
    
    error_msg += f"""
📚 详细堆栈:
{traceback.format_exc()}
{'='*80}
"""
    
    logger.error(error_msg)
    print(error_msg)
    
def log_info(message: str, context: str = ""):
    """
    记录信息日志
    
    Args:
        message: 日志消息
        context: 上下文描述
    """
    timestamp = get_china_time().strftime('%Y-%m-%d %H:%M:%S')
    log_msg = f"[{timestamp}] [{context}] {message}"
    logger.info(log_msg)
    print(log_msg)

def log_warning(message: str, context: str = ""):
    """
    记录警告日志
    
    Args:
        message: 日志消息
        context: 上下文描述
    """
    timestamp = get_china_time().strftime('%Y-%m-%d %H:%M:%S')
    log_msg = f"⚠️  [{timestamp}] [{context}] {message}"
    logger.warning(log_msg)
    print(log_msg)

def log_success(message: str, context: str = ""):
    """
    记录成功日志
    
    Args:
        message: 日志消息
        context: 上下文描述
    """
    timestamp = get_china_time().strftime('%Y-%m-%d %H:%M:%S')
    log_msg = f"✅ [{timestamp}] [{context}] {message}"
    logger.info(log_msg)
    print(log_msg)

