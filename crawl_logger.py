#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
聚合任务日志记录器
用于调试和追踪聚合过程
"""

import os
import logging
from datetime import datetime
from pathlib import Path

class CrawlLogger:
    """聚合任务日志记录器"""
    
    def __init__(self, task_id: str, log_dir: str = "crawl_logs"):
        """
        初始化日志记录器
        
        Args:
            task_id: 任务ID
            log_dir: 日志目录
        """
        self.task_id = task_id
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        
        # 创建日志文件
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.log_file = self.log_dir / f"crawl_{task_id}_{timestamp}.log"
        
        # 配置logger
        self.logger = logging.getLogger(f"crawl_{task_id}")
        self.logger.setLevel(logging.DEBUG)
        
        # 清除已有的handlers
        self.logger.handlers.clear()
        
        # 文件handler
        file_handler = logging.FileHandler(self.log_file, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        
        # 格式化
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(formatter)
        
        self.logger.addHandler(file_handler)
        
        # 同时输出到控制台
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)
        
        self.info(f"日志记录器已初始化: {self.log_file}")
    
    def debug(self, msg: str):
        """调试信息"""
        self.logger.debug(msg)
    
    def info(self, msg: str):
        """一般信息"""
        self.logger.info(msg)
    
    def warning(self, msg: str):
        """警告信息"""
        self.logger.warning(msg)
    
    def error(self, msg: str):
        """错误信息"""
        self.logger.error(msg)
    
    def critical(self, msg: str):
        """严重错误"""
        self.logger.critical(msg)
    
    def section(self, title: str):
        """章节标题"""
        separator = "=" * 80
        self.info(separator)
        self.info(f"  {title}")
        self.info(separator)
    
    def step(self, step_num: int, description: str):
        """步骤标记"""
        self.info(f"[步骤 {step_num}] {description}")
    
    def result(self, success: bool, message: str):
        """结果记录"""
        if success:
            self.info(f"✅ {message}")
        else:
            self.error(f"❌ {message}")
    
    def stats(self, stats_dict: dict):
        """统计信息"""
        self.info("📊 统计信息:")
        for key, value in stats_dict.items():
            self.info(f"   - {key}: {value}")
    
    def close(self):
        """关闭日志记录器"""
        for handler in self.logger.handlers:
            handler.close()
        self.logger.handlers.clear()
        self.info(f"日志已保存到: {self.log_file}")


# 全局日志记录器字典
_loggers = {}

def get_crawl_logger(task_id: str) -> CrawlLogger:
    """
    获取或创建聚合任务的日志记录器
    
    Args:
        task_id: 任务ID
        
    Returns:
        CrawlLogger: 日志记录器实例
    """
    if task_id not in _loggers:
        _loggers[task_id] = CrawlLogger(task_id)
    return _loggers[task_id]

def close_crawl_logger(task_id: str):
    """
    关闭并移除日志记录器
    
    Args:
        task_id: 任务ID
    """
    if task_id in _loggers:
        _loggers[task_id].close()
        del _loggers[task_id]
