#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
增量聚合管理模块
"""

import os
import json
import hashlib
import threading
from datetime import datetime, timedelta
from utils import get_china_time
from urllib.parse import urlparse
from typing import Dict, List, Optional, Tuple

class IncrementalCrawler:
    """增量聚合管理器"""
    
    def __init__(self, tracking_file: str = "static/article_tracking.json"):
        self.tracking_file = tracking_file
        self.lock = threading.Lock()
        self.tracking_data = self._load_tracking_data()
    
    def _load_tracking_data(self) -> Dict:
        """加载文章跟踪数据"""
        if os.path.exists(self.tracking_file):
            try:
                with open(self.tracking_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"加载跟踪数据失败: {e}")
                return {}
        return {}
    
    def _save_tracking_data(self):
        """保存文章跟踪数据"""
        with self.lock:
            try:
                # 确保目录存在
                dir_path = os.path.dirname(self.tracking_file)
                if dir_path:  # 只有当目录路径不为空时才创建
                    os.makedirs(dir_path, exist_ok=True)
                
                with open(self.tracking_file, 'w', encoding='utf-8') as f:
                    json.dump(self.tracking_data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"保存跟踪数据失败: {e}")
    
    def _domain_from_url(self, url: str) -> str:
        """从URL提取域名"""
        try:
            parsed = urlparse(url)
            return parsed.netloc.replace('www.', '')
        except:
            return 'unknown'
    
    def _generate_content_hash(self, content: str) -> str:
        """生成内容哈希"""
        if not content:
            return ""
        return hashlib.md5(content.encode('utf-8')).hexdigest()
    
    def is_article_crawled(self, url: str, publish_date: str = None, content_hash: str = None) -> bool:
        """
        检查文章是否已经聚合过
        
        Args:
            url: 文章URL
            publish_date: 发布日期
            content_hash: 内容哈希
            
        Returns:
            bool: 是否已聚合过
        """
        domain = self._domain_from_url(url)
        
        if domain not in self.tracking_data:
            return False
        
        for article in self.tracking_data[domain]:
            if article.get('url') == url:
                # 如果提供了发布日期，检查是否有更新
                if publish_date and article.get('publish_date'):
                    if publish_date != article.get('publish_date'):
                        return False  # 发布日期不同，需要重新聚合
                
                # 如果提供了内容哈希，检查内容是否有变化
                if content_hash and article.get('content_hash'):
                    if content_hash != article.get('content_hash'):
                        return False  # 内容有变化，需要重新聚合
                
                return True
        
        return False
    
    def add_crawled_article(self, url: str, title: str, publish_date: str = None, 
                          content: str = None, content_hash: str = None):
        """
        添加已聚合的文章记录
        
        Args:
            url: 文章URL
            title: 文章标题
            publish_date: 发布日期
            content: 文章内容
            content_hash: 内容哈希
        """
        domain = self._domain_from_url(url)
        
        if domain not in self.tracking_data:
            self.tracking_data[domain] = []
        
        # 生成内容哈希
        if content and not content_hash:
            content_hash = self._generate_content_hash(content)
        
        # 检查是否已存在
        for article in self.tracking_data[domain]:
            if article.get('url') == url:
                # 更新现有记录
                article['title'] = title
                article['publish_date'] = publish_date
                article['content_hash'] = content_hash
                article['last_crawled'] = get_china_time().isoformat()
                article['crawl_count'] = article.get('crawl_count', 0) + 1
                self._save_tracking_data()
                return
        
        # 添加新记录
        self.tracking_data[domain].append({
            'url': url,
            'title': title,
            'publish_date': publish_date,
            'content_hash': content_hash,
            'first_crawled': get_china_time().isoformat(),
            'last_crawled': get_china_time().isoformat(),
            'crawl_count': 1
        })
        
        self._save_tracking_data()
    
    def get_crawled_articles(self, domain: str = None) -> List[Dict]:
        """
        获取已聚合的文章列表
        
        Args:
            domain: 域名，如果为None则返回所有域名的文章
            
        Returns:
            List[Dict]: 文章列表
        """
        if domain:
            return self.tracking_data.get(domain, [])
        
        all_articles = []
        for domain_articles in self.tracking_data.values():
            all_articles.extend(domain_articles)
        
        return all_articles
    
    def get_domain_statistics(self) -> Dict:
        """
        获取域名统计信息
        
        Returns:
            Dict: 统计信息
        """
        stats = {}
        for domain, articles in self.tracking_data.items():
            stats[domain] = {
                'total_articles': len(articles),
                'last_crawled': max([article.get('last_crawled', '') for article in articles]) if articles else '',
                'first_crawled': min([article.get('first_crawled', '') for article in articles]) if articles else ''
            }
        return stats
    
    def clean_old_records(self, days: int = 30):
        """
        清理旧的记录
        
        Args:
            days: 保留天数
        """
        cutoff_date = get_china_time() - timedelta(days=days)
        cutoff_str = cutoff_date.isoformat()
        
        for domain in list(self.tracking_data.keys()):
            self.tracking_data[domain] = [
                article for article in self.tracking_data[domain]
                if article.get('last_crawled', '') > cutoff_str
            ]
            
            # 如果域名下没有文章了，删除域名
            if not self.tracking_data[domain]:
                del self.tracking_data[domain]
        
        self._save_tracking_data()
    
    def filter_new_articles(self, articles: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        """
        过滤出新文章和已存在的文章
        
        Args:
            articles: 文章列表
            
        Returns:
            Tuple[List[Dict], List[Dict]]: (新文章列表, 已存在文章列表)
        """
        new_articles = []
        existing_articles = []
        
        for article in articles:
            url = article.get('url')
            publish_date = article.get('publish_date')
            content = article.get('content', '')
            content_hash = self._generate_content_hash(content) if content else None
            
            if self.is_article_crawled(url, publish_date, content_hash):
                existing_articles.append(article)
            else:
                new_articles.append(article)
        
        return new_articles, existing_articles
    
    def update_article_records(self, articles: List[Dict]):
        """
        批量更新文章记录
        
        Args:
            articles: 文章列表
        """
        for article in articles:
            url = article.get('url')
            title = article.get('title', '无标题')
            publish_date = article.get('publish_date')
            content = article.get('content', '')
            
            self.add_crawled_article(url, title, publish_date, content)
    
    def get_incremental_summary(self, domain: str = None) -> Dict:
        """
        获取增量聚合摘要
        
        Args:
            domain: 域名
            
        Returns:
            Dict: 摘要信息
        """
        articles = self.get_crawled_articles(domain)
        
        if not articles:
            return {
                'total_articles': 0,
                'domains': 0,
                'last_crawled': None,
                'oldest_article': None,
                'newest_article': None
            }
        
        # 按域名统计
        domain_stats = self.get_domain_statistics()
        
        # 获取时间范围
        last_crawled = max([article.get('last_crawled', '') for article in articles])
        first_crawled = min([article.get('first_crawled', '') for article in articles])
        
        return {
            'total_articles': len(articles),
            'domains': len(domain_stats),
            'last_crawled': last_crawled,
            'oldest_article': first_crawled,
            'newest_article': last_crawled,
            'domain_stats': domain_stats
        }
    
    def delete_article(self, url: str) -> bool:
        """删除指定的文章"""
        with self.lock:
            try:
                domain = self._domain_from_url(url)
                if domain in self.tracking_data:
                    original_count = len(self.tracking_data[domain])
                    self.tracking_data[domain] = [
                        article for article in self.tracking_data[domain]
                        if article.get('url') != url
                    ]
                    
                    # 如果删除了文章，保存数据
                    if len(self.tracking_data[domain]) < original_count:
                        self._save_tracking_data()
                        
                        # 如果域名下没有文章了，删除域名
                        if not self.tracking_data[domain]:
                            del self.tracking_data[domain]
                        
                        return True
                
                return False
            except Exception as e:
                print(f"删除文章失败: {e}")
                return False

# 全局实例
incremental_crawler = IncrementalCrawler()
