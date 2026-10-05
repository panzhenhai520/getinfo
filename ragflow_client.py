#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ragflow API 客户端封装"""

from __future__ import annotations

import io
import hashlib
import os
import re
import sqlite3
import time
from typing import Dict, List, Optional

import requests

import config


class RagflowClient:
    """简单封装 Ragflow HTTP API"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[int] = None,
        retries: Optional[int] = None,
    ):
        self.base_url = (base_url or config.RAGFLOW_BASE_URL or '').rstrip('/')
        self.api_key = api_key or config.RAGFLOW_API_KEY
        self.timeout = timeout if timeout is not None else getattr(config, 'RAGFLOW_TIMEOUT', 45)
        self.retries = retries if retries is not None else getattr(config, 'RAGFLOW_UPLOAD_RETRIES', 1)
        self.proxies = config.get_ragflow_proxies() if hasattr(config, 'get_ragflow_proxies') else None

        self.configured = bool(self.base_url and self.api_key)

    # ------------------------- 基础工具 -------------------------
    def _upload_disabled_response(self) -> Dict:
        if not self.base_url:
            message = 'RAGFlow upload disabled: RAGFLOW_BASE_URL missing'
        elif not self.api_key:
            message = 'RAGFlow upload disabled: RAGFLOW_API_KEY missing'
        else:
            message = 'RAGFlow upload disabled'
        return {'code': 0, 'data': [], 'skipped': True, 'disabled': True, 'message': message}

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            'Authorization': f'Bearer {self.api_key}' if self.api_key else '',
        }
        headers = {k: v for k, v in headers.items() if v}
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _rewind_files(files):
        """Rewind requests file payloads before a retry."""
        if not files:
            return
        for item in files:
            file_obj = None
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                value = item[1]
                if isinstance(value, (tuple, list)) and len(value) >= 2:
                    file_obj = value[1]
                else:
                    file_obj = value
            if hasattr(file_obj, 'seek'):
                try:
                    file_obj.seek(0)
                except Exception:
                    pass

    @staticmethod
    def _should_retry_response(response) -> bool:
        return response.status_code in (408, 429) or response.status_code >= 500

    def _request(self, method: str, path: str, **kwargs):
        if not self.base_url:
            raise ValueError("RAGFLOW_BASE_URL 未配置")
        if not self.api_key:
            raise ValueError("RAGFLOW_API_KEY 未配置")

        url = f"{self.base_url}{path}"
        request_kwargs = dict(kwargs)
        request_kwargs.setdefault('timeout', self.timeout)
        if self.proxies and 'proxies' not in request_kwargs:
            request_kwargs['proxies'] = self.proxies

        last_error = None
        max_attempts = max(0, int(self.retries)) + 1
        for attempt in range(max_attempts):
            if attempt > 0:
                self._rewind_files(request_kwargs.get('files'))
                time.sleep(min(2 * attempt, 5))

            try:
                response = requests.request(method, url, **request_kwargs)
                if self._should_retry_response(response) and attempt < max_attempts - 1:
                    last_error = requests.HTTPError(
                        f"Ragflow HTTP {response.status_code}: {response.text[:200]}",
                        response=response,
                    )
                    continue
                response.raise_for_status()
                return response
            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
            ) as exc:
                last_error = exc
                if attempt >= max_attempts - 1:
                    raise
            except requests.exceptions.HTTPError:
                raise

        if last_error:
            raise last_error
        raise RuntimeError("Ragflow request failed")

    @staticmethod
    def build_document_name(title: str, url: str = '', article_id=None) -> str:
        """Build a stable RAGFlow file name and avoid title-only collisions."""
        raw_title = (title or 'untitled').strip()
        safe_title = re.sub(r'[\\/*?:"<>|]', '_', raw_title)
        safe_title = re.sub(r'\s+', ' ', safe_title).strip(' ._')
        safe_title = safe_title[:70] or 'article'
        identity = str(article_id or url or raw_title)
        digest = hashlib.sha1(identity.encode('utf-8', errors='ignore')).hexdigest()[:10]
        return f"{safe_title}_{digest}.txt"

    @staticmethod
    def extract_document_ids(data: Dict) -> List[str]:
        docs = data.get('data') if isinstance(data, dict) else []
        if isinstance(docs, dict):
            docs = [docs]
        doc_ids = []
        if isinstance(docs, list):
            for doc in docs:
                if isinstance(doc, dict) and doc.get('id'):
                    doc_ids.append(doc['id'])
        return doc_ids

    @staticmethod
    def _record_article_document_mapping(article_id, kb_id: str, document_ids: List[str], file_name: str, sync_status: str = 'uploaded') -> None:
        if not article_id or not kb_id or not document_ids or not file_name:
            return
        try:
            from sqlite_database import sqlite_db
            for doc_id in document_ids:
                sqlite_db.upsert_article_ragflow_document(
                    article_id=article_id,
                    kb_id=kb_id,
                    document_id=doc_id,
                    document_name=file_name,
                    sync_status=sync_status,
                )
        except Exception as exc:
            print(f"⚠️ 保存 RAGFlow 文档映射失败: {exc}")

    @staticmethod
    def _mark_document_mappings_deleted(kb_id: str, document_ids: List[str], error_message: str = '') -> None:
        if not kb_id or not document_ids:
            return
        try:
            from sqlite_database import sqlite_db
            for doc_id in document_ids:
                sqlite_db.update_article_ragflow_document_status(
                    kb_id=kb_id,
                    document_id=doc_id,
                    sync_status='deleted',
                    error_message=error_message,
                )
        except Exception as exc:
            print(f"⚠️ 更新 RAGFlow 文档映射删除状态失败: {exc}")

    # ------------------------- API -------------------------
    def list_datasets(self, page: int = 1, page_size: int = 50, keywords: str = '') -> List[Dict]:
        params = {
            'page': page,
            'page_size': page_size,
        }
        if keywords:
            params['name'] = keywords

        resp = self._request('GET', '/api/v1/datasets', params=params, headers=self._headers())
        data = resp.json()
        if data.get('code') != 0:
            raise ValueError(data.get('message') or 'Ragflow 列表获取失败')
        payload = data.get('data')
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ('datasets', 'docs', 'items', 'list', 'records', 'results'):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
            if payload.get('id'):
                return [payload]
        return []

    def list_documents(self, kb_id: str, keywords: str = '', page: int = 1, page_size: int = 10, name: str = '') -> Dict:
        params = {
            'page': page,
            'page_size': page_size,
        }
        if keywords:
            params['keywords'] = keywords
        if name:
            params['name'] = name

        resp = self._request('GET', f"/api/v1/datasets/{kb_id}/documents", params=params, headers=self._headers())
        data = resp.json()
        if data.get('code') != 0:
            raise ValueError(data.get('message') or 'Ragflow 文档列表获取失败')
        return data.get('data') or {}

    def search_dataset(
        self,
        kb_id: str,
        question: str,
        *,
        top_n: int = 20,
        similarity_threshold: float = 0.2,
        vector_similarity_weight: float = 0.7,
    ) -> Dict:
        """Search the existing RAGFlow dataset for candidate chunks.

        Callers remain responsible for authorization and factual validation;
        this method intentionally returns the provider's chunk identities and
        scores without treating their text as trusted application evidence.
        """
        dataset_id = str(kb_id or '').strip()
        query = str(question or '').strip()
        if not dataset_id:
            raise ValueError('Ragflow 知识库ID不能为空')
        if not query:
            raise ValueError('Ragflow 检索问题不能为空')
        size = max(1, min(50, int(top_n)))
        body = {
            'question': query,
            'doc_ids': [],
            'page': 1,
            'size': size,
            'top_k': 1024,
            'similarity_threshold': max(0.0, min(1.0, float(similarity_threshold))),
            'vector_similarity_weight': max(0.0, min(1.0, float(vector_similarity_weight))),
            'keyword': False,
            'cross_languages': [],
        }
        response = self._request(
            'POST',
            f'/api/v1/datasets/{dataset_id}/search',
            headers=self._headers({'Content-Type': 'application/json'}),
            json=body,
        )
        payload = response.json()
        if payload.get('code') != 0:
            raise ValueError(payload.get('message') or 'Ragflow 检索失败')
        data = payload.get('data') or {}
        if isinstance(data, list):
            chunks = data
            total = len(data)
        elif isinstance(data, dict):
            chunks = data.get('chunks') or data.get('items') or []
            total = data.get('total') if data.get('total') is not None else len(chunks)
        else:
            chunks, total = [], 0
        return {
            'chunks': [item for item in chunks if isinstance(item, dict)],
            'total': int(total or 0),
        }

    def upload_document(self, kb_id: str, file_path: str) -> Dict:
        if not getattr(config, 'RAGFLOW_UPLOAD_ENABLED', True) or not self.configured:
            return self._upload_disabled_response()

        if not os.path.exists(file_path):
            raise FileNotFoundError(file_path)

        with open(file_path, 'rb') as f:
            files = [('file', (os.path.basename(file_path), f, 'text/plain'))]
            resp = self._request(
                'POST',
                f"/api/v1/datasets/{kb_id}/documents",
                headers=self._headers(),
                files=files,
            )
        data = resp.json()
        if data.get('code') != 0:
            raise ValueError(data.get('message') or 'Ragflow 上传失败')
        return data

    def upload_document_content(self, kb_id: str, file_name: str, content: str, auto_parse: Optional[bool] = None) -> Dict:
        """
        上传文档内容到知识库
        
        Args:
            kb_id: 知识库ID
            file_name: 文件名
            content: 文档内容
            auto_parse: 是否自动触发解析，默认True
            
        Returns:
            上传结果
        """
        if not getattr(config, 'RAGFLOW_UPLOAD_ENABLED', True) or not self.configured:
            return self._upload_disabled_response()

        if auto_parse is None:
            auto_parse = getattr(config, 'RAGFLOW_AUTO_PARSE', True)

        file_bytes = content.encode('utf-8')
        buffer = io.BytesIO(file_bytes)
        files = [('file', (file_name, buffer, 'text/plain'))]
        resp = self._request(
            'POST',
            f"/api/v1/datasets/{kb_id}/documents",
            headers=self._headers(),
            files=files,
        )
        data = resp.json()
        if data.get('code') != 0:
            raise ValueError(data.get('message') or 'Ragflow 上传失败')
        
        # 自动触发解析
        if auto_parse and data.get('data'):
            doc_ids = self.extract_document_ids(data)
            if doc_ids:
                try:
                    data['parse_result'] = self.parse_documents(kb_id, doc_ids)
                except Exception as e:
                    data['parse_error'] = str(e)
                    print(f"⚠️ 触发解析失败: {e}")
        
        return data

    def document_exists(self, kb_id: str, document_name: str) -> bool:
        return bool(self.find_documents_by_name(kb_id, document_name))

    def find_documents_by_name(self, kb_id: str, document_name: str) -> List[Dict]:
        if not self.configured:
            return []

        try:
            data = self.list_documents(kb_id, page=1, page_size=20, name=document_name)
            if isinstance(data, dict):
                docs = data.get('docs') or []
                return [doc for doc in docs if isinstance(doc, dict) and doc.get('name') == document_name]
            # 如果API直接返回列表
            if isinstance(data, list):
                return [doc for doc in data if isinstance(doc, dict) and doc.get('name') == document_name]
            return []
        except ValueError as e:
            # RAGFlow API 返回 "don't own" 错误时，实际上是文档不存在
            # 返回空列表允许上传
            error_msg = str(e).lower()
            if "don't own" in error_msg or "not found" in error_msg:
                return []
            # 其他错误则抛出
            raise

    def delete_documents_by_name(self, kb_id: str, document_name: str) -> Dict:
        docs = self.find_documents_by_name(kb_id, document_name)
        doc_ids = [doc.get('id') for doc in docs if doc.get('id')]
        if not doc_ids:
            return {'code': 0, 'data': [], 'deleted': 0}
        return self.delete_documents(kb_id, doc_ids)

    def delete_documents(self, kb_id: str, document_ids: List[str]) -> Dict:
        """Delete documents from a dataset by document id."""
        if not getattr(config, 'RAGFLOW_UPLOAD_ENABLED', True) or not self.configured:
            return self._upload_disabled_response()
        ids = [doc_id for doc_id in (document_ids or []) if doc_id]
        if not ids:
            return {'code': 0, 'data': [], 'deleted': 0}
        resp = self._request(
            'DELETE',
            f"/api/v1/datasets/{kb_id}/documents",
            headers=self._headers({'Content-Type': 'application/json'}),
            json={'ids': ids},
        )
        data = resp.json()
        if data.get('code') != 0:
            raise ValueError(data.get('message') or 'Ragflow 删除文档失败')
        data['deleted'] = len(ids)
        self._mark_document_mappings_deleted(kb_id, ids)
        return data

    
    def parse_documents(self, kb_id: str, document_ids: List[str]) -> Dict:
        """触发文档解析"""
        ids = [doc_id for doc_id in (document_ids or []) if doc_id]
        if not ids:
            return {'code': 0, 'data': [], 'message': 'no document ids'}
        api_url = f"/api/v1/datasets/{kb_id}/documents/parse"
        payload = {'document_ids': ids}
        resp = self._request(
            'POST',
            api_url,
            headers=self._headers({'Content-Type': 'application/json'}),
            json=payload,
        )
        data = resp.json()
        if data.get('code') != 0:
            raise ValueError(data.get('message') or 'Ragflow 文档解析失败')
        return data

    def upload_articles_to_dataset(self, articles: List[Dict], dataset_id: str) -> Dict:
        """
        批量上传文章到知识库
        
        Args:
            articles: 文章列表，每篇文章包含 title, content, url, publish_date 等字段
            dataset_id: 知识库ID
            
        Returns:
            包含上传结果的字典
        """
        stats = {
            'uploaded': 0,
            'skipped_existing': 0,
            'skipped_empty': 0,
            'failed': 0,
            'parse_submitted': 0,
            'parse_failed': 0,
            'errors': [],
            'uploaded_files': []
        }

        if not getattr(config, 'RAGFLOW_UPLOAD_ENABLED', True) or not self.configured:
            stats['disabled'] = True
            disabled = self._upload_disabled_response()
            return {
                'success': True,
                'stats': stats,
                'message': disabled.get('message')
            }
        
        for article in articles:
            try:
                title = article.get('title', 'untitled')
                content = article.get('content', '')
                url = article.get('url', '')
                article_id = article.get('id') or article.get('db_id')

                if not content:
                    stats['skipped_empty'] += 1
                    continue
                
                file_name = self.build_document_name(title, url, article_id)
                
                # 检查是否已存在。默认覆盖重传，避免知识库残留旧正文。
                existing_docs = self.find_documents_by_name(dataset_id, file_name)
                if existing_docs:
                    if getattr(config, 'RAGFLOW_REUPLOAD_EXISTING', True):
                        self.delete_documents(dataset_id, [doc.get('id') for doc in existing_docs if doc.get('id')])
                    else:
                        stats['skipped_existing'] += 1
                        continue
                
                # 🔥 只上传纯正文内容，不包含标题、URL、日期等元数据
                # 上传文档
                upload_result = self.upload_document_content(
                    dataset_id,
                    file_name,
                    content,
                    auto_parse=getattr(config, 'RAGFLOW_AUTO_PARSE', True),
                )
                doc_ids = self.extract_document_ids(upload_result)
                parse_result = upload_result.get('parse_result') or {}
                parse_ok = bool(parse_result) and parse_result.get('code') == 0
                sync_status = 'parsed' if parse_ok else 'uploaded'
                if parse_ok:
                    stats['parse_submitted'] += 1
                elif upload_result.get('parse_error'):
                    stats['parse_failed'] += 1
                    stats['errors'].append({
                        'title': title,
                        'url': url,
                        'error': f"RAGFlow解析触发失败: {upload_result.get('parse_error')}"
                    })
                self._record_article_document_mapping(article_id, dataset_id, doc_ids, file_name, sync_status)
                stats['uploaded'] += 1
                stats['uploaded_files'].append(file_name)
                
            except Exception as e:
                stats['failed'] += 1
                stats['errors'].append({
                    'title': article.get('title', 'untitled'),
                    'url': article.get('url', ''),
                    'error': str(e)
                })
        
        return {
            'success': True,
            'stats': stats
        }

    # ------------------------- 兼容旧脚本 -------------------------
    def check_document_exists(self, kb_id: str, url_or_name: str) -> bool:
        """兼容旧代码：按文档名或URL关键词判断文档是否存在。"""
        if not url_or_name:
            return False
        if str(url_or_name).endswith('.txt'):
            return self.document_exists(kb_id, url_or_name)
        try:
            data = self.list_documents(kb_id, keywords=url_or_name, page=1, page_size=1)
            if isinstance(data, dict):
                return bool(data.get('total', 0) or data.get('docs'))
            return bool(data)
        except Exception:
            return False

    def upload_article(
        self,
        kb_id: str,
        title: str,
        content: str,
        url: str = '',
        metadata: Optional[Dict] = None,
    ) -> Dict:
        """兼容旧代码：上传单篇文章。"""
        metadata = metadata or {}
        if not content:
            return {'success': False, 'error': 'empty content'}

        file_name = self.build_document_name(title, url, metadata.get('db_id'))
        existing_docs = self.find_documents_by_name(kb_id, file_name)
        if existing_docs and getattr(config, 'RAGFLOW_REUPLOAD_EXISTING', True):
            self.delete_documents(kb_id, [doc.get('id') for doc in existing_docs if doc.get('id')])
        elif existing_docs:
            return {'success': True, 'skipped_existing': True, 'file_name': file_name}

        data = self.upload_document_content(
            kb_id,
            file_name,
            content,
            auto_parse=getattr(config, 'RAGFLOW_AUTO_PARSE', True),
        )
        doc_ids = self.extract_document_ids(data)
        parse_result = data.get('parse_result') or {}
        sync_status = 'parsed' if parse_result.get('code') == 0 else 'uploaded'
        self._record_article_document_mapping(metadata.get('db_id'), kb_id, doc_ids, file_name, sync_status)
        return {
            'success': True,
            'file_name': file_name,
            'document_ids': doc_ids,
            'data': data,
        }

    def batch_upload_articles_from_db(self, kb_id: str, domain: str = '', batch_size: int = 100) -> Dict:
        """兼容旧手动同步脚本：从本地 articles 表批量上传。"""
        from db_connection import connect_database, is_postgres_connection

        stats = {
            'uploaded': 0,
            'skipped_existing': 0,
            'skipped_empty': 0,
            'failed': 0,
            'errors': []
        }

        conn = connect_database()
        if not is_postgres_connection(conn):
            conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            if domain:
                cursor.execute(
                    """
                    SELECT id, title, url, content, publish_date, authors, domain
                    FROM articles
                    WHERE domain = ?
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (domain, batch_size)
                )
            else:
                cursor.execute(
                    """
                    SELECT id, title, url, content, publish_date, authors, domain
                    FROM articles
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (batch_size,)
                )
            articles = [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

        result = self.upload_articles_to_dataset(articles, kb_id)
        stats.update(result.get('stats', {}))
        return stats


_client_instance: Optional[RagflowClient] = None


def get_ragflow_client() -> RagflowClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = RagflowClient()
    return _client_instance
