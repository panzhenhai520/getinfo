#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SQLite数据库管理模块
用于管理文章数据的本地存储和查询
"""

import sqlite3
import json
import hashlib
import os
import re
import threading
from datetime import datetime
import config
from utils import coerce_int, get_china_time, strip_url_presentation_params
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse
from url_validation_helper import normalize_task_url, validate_http_url

_SOURCE_TASK_PLACEHOLDERS = {'source task', 'sourcetask', '来源任务'}


def _is_placeholder_source_task(value) -> bool:
    normalized = re.sub(r'\s+', ' ', str(value or '').strip()).lower()
    compact = normalized.replace(' ', '')
    return normalized in _SOURCE_TASK_PLACEHOLDERS or compact in _SOURCE_TASK_PLACEHOLDERS


def _split_keyword_value(value) -> List[str]:
    return [item.strip() for item in re.split(r'[,，;；、\s]+', str(value or '')) if item.strip()]


def _add_parsed_keyword(target: set, keyword) -> None:
    clean = str(keyword or '').strip()
    if not clean:
        return
    if clean.startswith('[标]') or clean.startswith('[標]'):
        clean = clean[3:].strip()
    elif clean.startswith('[文]'):
        clean = clean[3:].strip()
    if clean and not re.fullmatch(r'\d+', clean):
        target.add(clean)


def _parse_matched_keyword_text(value) -> List[str]:
    """Parse legacy and structured matched_keywords values into unique keyword names."""
    if not value:
        return []

    parsed = set()
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _add_parsed_keyword(parsed, item)
        return sorted(parsed)

    if isinstance(value, dict):
        for key in ('title_keywords', 'content_keywords', 'all_keywords'):
            for item in value.get(key) or []:
                _add_parsed_keyword(parsed, item)
        return sorted(parsed)

    raw = str(value).strip()
    if not raw:
        return []

    if raw.startswith('{') or raw.startswith('['):
        try:
            return _parse_matched_keyword_text(json.loads(raw))
        except Exception:
            pass

    location_pattern = re.compile(r'(标题|標題|title|内容|內容|正文|content)\s*[\(:：]\s*([^）)]+)', re.I)
    for match in location_pattern.finditer(raw):
        for item in _split_keyword_value(match.group(2)):
            _add_parsed_keyword(parsed, item)

    for token in [item.strip() for item in re.split(r'[,，;；]+', raw) if item.strip()]:
        _add_parsed_keyword(parsed, token)

    return sorted(parsed)


def _load_dotenv_file(path='.env'):
    if not os.path.exists(path):
        return

    try:
        with open(path, 'r', encoding='utf-8') as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                if line.startswith('export '):
                    line = line[7:].strip()
                key, value = line.split('=', 1)
                key = key.strip()
                if not key or key in os.environ:
                    continue
                os.environ[key] = value.strip().strip('"').strip("'")
    except Exception as exc:
        print(f"Warning: failed to load .env: {exc}")


_load_dotenv_file()


def _normalize_schedule_list_value(value, min_value, max_value):
    result = []
    if isinstance(value, (list, tuple, set)):
        raw_items = value
    else:
        raw_items = str(value or '').split(',')
    for item in raw_items:
        parsed = coerce_int(item, None)
        if parsed is not None and min_value <= parsed <= max_value and parsed not in result:
            result.append(parsed)
    return ','.join(str(item) for item in sorted(result))


def _normalize_schedule_time_value(value):
    parts = str(value or '00:00:00').split(':')
    hour = coerce_int(parts[0] if len(parts) > 0 else 0, 0, 0, 23)
    minute = coerce_int(parts[1] if len(parts) > 1 else 0, 0, 0, 59)
    second = coerce_int(parts[2] if len(parts) > 2 else 0, 0, 0, 59)
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def _normalize_schedule_fields(task_data):
    normalized = dict(task_data or {})
    schedule_type = normalized.get('schedule_type', 'daily')
    if schedule_type == 'weekly':
        weekdays = _normalize_schedule_list_value(normalized.get('schedule_weekdays', ''), 0, 6)
        normalized['schedule_weekdays'] = weekdays or str(get_china_time().weekday())
        normalized['schedule_monthdays'] = ''
    elif schedule_type == 'monthly':
        monthdays = _normalize_schedule_list_value(normalized.get('schedule_monthdays', ''), 1, 31)
        normalized['schedule_monthdays'] = monthdays or str(get_china_time().day)
        normalized['schedule_weekdays'] = ''
    else:
        normalized['schedule_weekdays'] = ''
        normalized['schedule_monthdays'] = ''
    return normalized


def clean_article_markdown(text):
    """入库前清洗正文：去掉源站导航/面包屑/页头页脚纯链接行/分页/空列表标记/语言切换/页脚锅炉板，压缩连续空行。

    只清理展示噪音（导航链接、面包屑、分页、空列表标记、页头导航词、版权/备案/联系页脚等），
    不改变正文主体；仅作用于 Markdown 结构，对 HTML 内容原样放行，避免误伤。
    """
    if not text:
        return ""
    cleaned = []
    for raw_line in str(text).replace("\r", "").split("\n"):
        line = raw_line.rstrip()
        t = line.strip()
        if not t:
            cleaned.append(line)
            continue
        # 面包屑：一行内多个 ">" 且含 Markdown 链接（首页 > 新服务 > ... > 目标）
        if t.count(">") >= 2 and re.search(r"\]\([^)]*\)", t):
            continue
        # JS 空链接：整行以 [xxx](javascript: 开头
        if re.match(r"^\s*\[[^\]]*\]\(\s*javascript:", t, re.I):
            continue
        # JS 导航锚点：* [xxx](javascript:void...)
        if re.match(r"^\*\s*\[[^\]]+\]\([^)]*javascript:void", t, re.I):
            continue
        # 纯图片行删除（图片属展示噪音：正文统一走 LLM 精炼摘要，不再保留图片行）
        if re.match(r"^\s*(!\[[^\]]*\]\([^)]*\)|\s)+$", t):
            continue
        if re.sub(r"!?\[[^\]]*\]\([^)]*\)", "", t).replace(">", "").replace("→", "").strip() == "":
            continue
        # 内嵌图片 token 剔除（保留行内文字）
        if re.search(r"!\[[^\]]*\]\([^)]*\)", t):
            line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line).strip()
            t = line.strip()
            if not t:
                continue
        # 纯链接列表项：* [xxx](url)  或  - [xxx](url)
        if re.match(r"^\s*[*\-]\s*\[[^\]]*\]\([^)]*\)\s*$", t):
            continue
        # 分页噪声：* N / * 第N页 / 下一页 / 上一页
        if re.match(r"^\s*\*\s*第?\d+\s*页?\s*$", t):
            continue
        if t in ("下一页", "上一页", "首页", "末页"):
            continue
        # 空列表标记：* / -（非水平线）
        if re.match(r"^\s*(?:\*(\s+\*)*|-)\s*$", t):
            continue
        # 页头导航/语言切换等短裸词（主管单位/社会公众/媒体/应聘者/简繁/ENG 等），含 markdown 标题前缀
        if re.match(
            r"^#{0,6}\s*(主管单位|社会公众|应聘者|媒体|新闻媒体|简体|繁体|英文|English|Logout|登录|注册|搜索|网站地图)\s*$",
            t,
            re.I,
        ):
            continue
        # 语言切换行：简·繁·ENG 这种（仅含简繁中英ENG和·），可带标题前缀
        if len(t) <= 28 and re.match(r"^#{0,6}\s*[简繁中英ENG·\s]+$", t):
            continue
        # 页脚锅炉板：版权/备案号/联系方式/举报入口/扫描/权利声明 等
        if re.search(
            r"copyright|©|all rights reserved|备案号|\bicp\b|ICP备|举报平台|平台入口|微信扫描|新浪微博扫描|常见问题解答|网站标识|咨询热线|投诉举报",
            t,
            re.I,
        ):
            continue
        cleaned.append(line)
    out = "\n".join(cleaned)
    # 浏览器提示条/作者卡 + 连续短行块（导航菜单/行情碎片）整块删除（与 content_handlers 共用实现）
    try:
        from content_handlers import _drop_short_line_blocks, _drop_browser_and_author_noise
        out = "\n".join(_drop_browser_and_author_noise(out.split("\n")))
        out = _drop_short_line_blocks(out)
    except Exception:
        pass
    return re.sub(r"\n{3,}", "\n\n", out)


def _dedup_title_lines(md_text, title, max_removed=3):
    """标题噪音清理：删除 Markdown 中与文章标题重复的标题行（# 标题）或纯文本标题行。

    抓取原文常把 <h1> 标题转成 Markdown 标题行，或把网页标题原样放进正文
    开头；详情页已单独展示标题，正文里再出现一遍大标题属于重复噪音；
    最多删 max_removed 行防误伤。
    """
    title_norm = re.sub(r"[\s#*_\-—–|·]", "", str(title or "")).casefold()
    if not title_norm:
        return md_text
    out = []
    removed = 0
    for line in str(md_text).split("\n"):
        m = re.match(r"^\s{0,4}#{1,6}\s+(.+?)\s*$", line)
        if (
            m
            and removed < max_removed
            and re.sub(r"[\s#*_\-—–|·]", "", m.group(1)).casefold() == title_norm
        ):
            removed += 1
            continue
        # 纯文本标题行（非 heading）：与标题完全相同且足够长才删除，
        # 避免误删正文中恰好等于标题的短句
        s = line.strip()
        if (
            not m
            and removed < max_removed
            and len(s) >= 10
            and re.sub(r"[\s#*_\-—–|·]", "", s).casefold() == title_norm
        ):
            removed += 1
            continue
        out.append(line)
    return "\n".join(out)


# 兼容旧引用（标题去重函数更名后保持别名）
_dedup_title_heading_lines = _dedup_title_lines


class SQLiteDatabase:
    """SQLite数据库管理类"""
    
    def __init__(self, db_path: str = None):
        """
        初始化数据库连接
        
        Args:
            db_path: 数据库文件路径
        """
        self.db_path = (
            db_path
            or os.getenv('SQLITE_BACKUP_PATH')
            or os.getenv('DATABASE_PATH')
            or os.path.join(os.getcwd(), 'crawler_articles.db')
        )
        configured_type = str(getattr(config, 'DATABASE_TYPE', 'sqlite') or 'sqlite').strip().lower()
        self.backend = 'postgres' if configured_type in {'postgres', 'postgresql', 'pg'} else 'sqlite'
        self.lock = threading.RLock()  # 使用可重入锁，避免死锁
        self.local = threading.local()  # 线程本地存储
        self.connection = None

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        func = _STORAGE_PROXY.get(name)
        if func is None:
            raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")
        def _bound(*args, **kwargs):
            return func(self, *args, **kwargs)
        return _bound

    def _ensure_connection(self):
        """确保数据库连接已建立且可用"""
        # Worker heartbeat、job runner 和 HTTP 请求会共享此对象。连接健康
        # 检查与重连必须和实际查询使用同一把 RLock，否则多个线程可同时
        # 在同一 sqlite connection 上创建健康探针并让测试/停机清理竞态。
        with self.lock:
            need_reconnect = self.connection is None
            if not need_reconnect:
                try:
                    self.connection.execute("SELECT 1").fetchone()
                except Exception as exc:
                    print(f"🔄 数据库连接无效，需要重连: {exc}")
                    need_reconnect = True

            if need_reconnect:
                try:
                    if self.connection is not None:
                        self.connection.close()
                except Exception:
                    pass
                self.connection = None
                if not self.connect():
                    raise RuntimeError("无法连接到数据库")
    
    def connect(self) -> bool:
        """
        连接数据库
        
        Returns:
            bool: 连接是否成功
        """
        if self.backend == 'postgres':
            try:
                return self._connect_postgres()
            except Exception as e:
                print(f"❌ PostgreSQL主库连接失败: {e}")
                if not getattr(config, 'DATABASE_FALLBACK_TO_SQLITE', False):
                    return False
                print("🔄 回退到 SQLite 备份数据库")
                self.backend = 'sqlite'
                return self._connect_sqlite()
        return self._connect_sqlite()

    def _connect_postgres(self) -> bool:
        from db_connection import connect_postgres_primary
        self.connection = connect_postgres_primary()
        self.backend = 'postgres'
        print(
            f"✅ PostgreSQL主库连接成功: "
            f"{getattr(config, 'POSTGRES_HOST', '127.0.0.1')}:"
            f"{getattr(config, 'POSTGRES_PORT', 5432)}/"
            f"{getattr(config, 'POSTGRES_DB', 'collectinfo')}"
        )
        # SQLite 路径的连接初始化会跑 _migrate_existing_schema 建齐 schema，
        # 而 PG 路径此前只连库不建表，导致后加的 intel 表（主题/证据/个人门禁）
        # 在生产 PG 上缺失。这里按函数逐个幂等补建并独立容错：
        # 单个函数失败只记日志，不影响其它表的补建与本次连接。
        try:
            from intel_schema import (
                ensure_intel_topic_search_test_tables,
                ensure_intel_topic_tables,
                ensure_intel_evidence_tables,
                ensure_user_gate_tables,
            )
            cursor = self.connection.cursor()
            for _name, _fn in (
                ('topic_search_test', ensure_intel_topic_search_test_tables),
                ('topic', ensure_intel_topic_tables),
                ('evidence', ensure_intel_evidence_tables),
                ('user_gate', ensure_user_gate_tables),
            ):
                try:
                    _fn(cursor)
                except Exception as _exc:
                    print(f"⚠️ PG 幂等补建 {_name} 表失败（不影响连接）: {_exc}")
            # 阶段1/阶段3 增量迁移：articles 补列 + 动态转换缓存表（幂等，PG/SQLite 通用）
            try:
                self._ensure_article_markdown_columns(cursor)
                from dynamic_link_converter import ensure_dynamic_converted_table
                ensure_dynamic_converted_table(cursor)
                # 阶段4：信源检查一键学习模型表
                from site_scraper_models import ensure_site_scraper_models_table
                ensure_site_scraper_models_table(cursor)
            except Exception as _exc:
                print(f"⚠️ PG 幂等补列/动态转换表失败（不影响连接）: {_exc}")
        except Exception as _exc:
            print(f"⚠️ PG 幂等 schema 补全失败（不影响连接）: {_exc}")
        return True

    def _connect_sqlite(self) -> bool:
        try:
            db_dir = os.path.dirname(self.db_path)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)

            self.connection = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=config.SQLITE_BUSY_TIMEOUT_MS / 1000.0,
                isolation_level=None  # 自动提交模式
            )
            self.connection.row_factory = sqlite3.Row

            cursor = self.connection.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA synchronous = NORMAL")
            cursor.execute(
                f"PRAGMA busy_timeout = {int(config.SQLITE_BUSY_TIMEOUT_MS)}"
            )
            self._migrate_existing_schema(cursor)
            cursor.close()

            print(f"✅ SQLite数据库连接成功: {self.db_path}")
            return True
        except Exception as e:
            print(f"❌ SQLite数据库连接失败: {e}")
            return False
    
    def disconnect(self):
        """断开数据库连接"""
        try:
            with self.lock:
                if self.connection is not None:
                    self.connection.close()
                self.connection = None
            label = "PostgreSQL" if self.backend == "postgres" else "SQLite"
            print(f"🔌 {label}数据库连接已断开")
        except Exception as e:
            print(f"❌ 断开数据库连接失败: {e}")
    
    def create_tables(self) -> bool:
        """
        创建数据库表
        
        Returns:
            bool: 创建是否成功
        """
        if self.backend == 'postgres':
            print("✅ PostgreSQL主库模式：跳过 SQLite DDL，使用已迁移 schema")
            return True
        try:
            with self.lock:
                # 创建文章表
                create_articles_table = """
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    content TEXT,
                    domain TEXT,
                    category_id INTEGER,
                    source_url_id INTEGER,
                    publish_date DATE,
                    -- 原站发布时间与其精度必须分开保存，避免把“某日”伪装成零点。
                    published_at_utc TEXT,
                    published_timezone TEXT,
                    published_precision TEXT,
                    published_time_source TEXT,
                    content_hash TEXT,
                    crawler_engine_used TEXT,
                    crawler_engines TEXT,
                    crawler_attempts INTEGER DEFAULT 0,
                    fallback_trigger_reason TEXT,
                    source_method TEXT,
                    configured_url TEXT,
                    resolved_target_url TEXT,
                    canonical_url TEXT,
                    source_task_id TEXT,
                    source_task_name TEXT,
                    -- Crawl instants are UTC.  Do not depend on the process TZ:
                    -- workers and test runners may use different local zones.
                    first_crawled TIMESTAMP DEFAULT (datetime('now')),
                    last_crawled TIMESTAMP DEFAULT (datetime('now')),
                    crawl_count INTEGER DEFAULT 1,
                    content_length INTEGER DEFAULT 0,
                    extraction_method TEXT,
                    quality_score REAL DEFAULT 0,
                    matched_keywords TEXT,
                    matched_keywords_raw TEXT,
                    keyword_match_detail TEXT,
                    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'deleted', 'archived')),
                    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                    updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                    FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE SET NULL,
                    FOREIGN KEY (source_url_id) REFERENCES managed_urls(id) ON DELETE SET NULL
                )
                """

                # 创建聚合尝试记录表
                create_crawl_attempts_table = """
                CREATE TABLE IF NOT EXISTS crawl_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_id INTEGER,
                    task_id TEXT,
                    article_id INTEGER,
                    configured_url TEXT,
                    resolved_target_url TEXT,
                    canonical_url TEXT,
                    crawler_engine TEXT NOT NULL,
                    phase TEXT,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'completed', 'failed', 'skipped')),
                    fallback_trigger_reason TEXT,
                    articles_found INTEGER DEFAULT 0,
                    candidate_urls INTEGER DEFAULT 0,
                    keyword_hits INTEGER DEFAULT 0,
                    error_message TEXT,
                    metadata JSON,
                    started_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                    completed_at TIMESTAMP,
                    duration_seconds REAL,
                    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                    FOREIGN KEY (schedule_id) REFERENCES scheduled_tasks(id) ON DELETE SET NULL,
                    FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE SET NULL,
                    FOREIGN KEY (task_id) REFERENCES crawl_tasks(task_id) ON DELETE SET NULL
                )
                """
                
                # 创建聚合任务表
                create_tasks_table = """
                CREATE TABLE IF NOT EXISTS crawl_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL UNIQUE,
                    target_url TEXT NOT NULL,
                    task_name TEXT,
                    crawl_depth INTEGER DEFAULT 1,
                    crawl_mode TEXT DEFAULT 'standard',
                    page_limit INTEGER DEFAULT 50,
                    incremental_mode BOOLEAN DEFAULT FALSE,
                    keywords TEXT,
                    industry_pack_id TEXT NOT NULL DEFAULT '',
                    industry_pack_version_id INTEGER,
                    activation_id TEXT NOT NULL DEFAULT '',
                    ownership_type TEXT NOT NULL DEFAULT 'legacy',
                    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
                    progress INTEGER DEFAULT 0,
                    articles_found INTEGER DEFAULT 0,
                    articles_processed INTEGER DEFAULT 0,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    error_message TEXT,
                    initialization_batch_id TEXT,
                    initialization_from TEXT,
                    initialization_to TEXT,
                    resource_priority INTEGER DEFAULT 50,
                    deferred_until TIMESTAMP,
                    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                    updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
                )
                """
                
                # 创建文章-任务关联表
                create_article_tasks_table = """
                CREATE TABLE IF NOT EXISTS article_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_id INTEGER NOT NULL,
                    task_id TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                    FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE,
                    FOREIGN KEY (task_id) REFERENCES crawl_tasks(task_id) ON DELETE CASCADE,
                    UNIQUE(article_id, task_id)
                )
                """

                create_article_spacetime_profiles_table = """
                CREATE TABLE IF NOT EXISTS article_spacetime_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_id INTEGER NOT NULL UNIQUE,
                    time_value TEXT,
                    time_type TEXT,
                    time_confidence REAL DEFAULT 0,
                    time_evidence TEXT,
                    location_name TEXT,
                    location_lat REAL,
                    location_lng REAL,
                    location_type TEXT,
                    location_confidence REAL DEFAULT 0,
                    location_evidence TEXT,
                    source_location_name TEXT,
                    source_location_lat REAL,
                    source_location_lng REAL,
                    event_location_name TEXT,
                    event_location_lat REAL,
                    event_location_lng REAL,
                    jurisdiction_name TEXT,
                    spacetime_status TEXT DEFAULT 'ready',
                    analysis_version TEXT,
                    analyzed_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                    metadata TEXT,
                    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                    updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                    FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE
                )
                """
                
                # 创建统计表
                create_stats_table = """
                CREATE TABLE IF NOT EXISTS crawl_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date DATE NOT NULL UNIQUE,
                    total_articles INTEGER DEFAULT 0,
                    new_articles INTEGER DEFAULT 0,
                    total_domains INTEGER DEFAULT 0,
                    total_tasks INTEGER DEFAULT 0,
                    completed_tasks INTEGER DEFAULT 0,
                    failed_tasks INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                    updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
                )
                """
                
                # 创建分类表
                create_categories_table = """
                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT,
                    display_order INTEGER DEFAULT 0,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                    updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
                )
                """

                create_auth_configs_table = """
                CREATE TABLE IF NOT EXISTS auth_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    login_url TEXT NOT NULL,
                    username TEXT,
                    password TEXT,
                    username_selector TEXT,
                    password_selector TEXT,
                    submit_selector TEXT,
                    wait_after_submit INTEGER DEFAULT 5,
                    success_indicator_type TEXT,
                    success_indicator_value TEXT,
                    description TEXT,
                    storage_file TEXT,
                    cookies_count INTEGER DEFAULT 0,
                    is_active BOOLEAN DEFAULT TRUE,
                    use_count INTEGER DEFAULT 0,
                    last_used_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                    updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
                )
                """
                
                # 创建URL管理表
                create_urls_table = """
                CREATE TABLE IF NOT EXISTS managed_urls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL UNIQUE,
                    name TEXT,
                    description TEXT,
                    category_id INTEGER,
                    category TEXT,
                    parent_url_id INTEGER,
                    domain TEXT,
                    is_active BOOLEAN DEFAULT TRUE,
                    auto_crawl BOOLEAN DEFAULT FALSE,
                    crawl_frequency TEXT,
                    auth_config TEXT,
                    requires_auth BOOLEAN DEFAULT FALSE,
                    auth_config_id INTEGER,
                    keywords TEXT,
                    days_limit INTEGER DEFAULT 7,
                    industry_pack_id TEXT NOT NULL DEFAULT '',
                    industry_pack_version_id INTEGER,
                    activation_id TEXT NOT NULL DEFAULT '',
                    ownership_type TEXT NOT NULL DEFAULT 'legacy',
                    access_status TEXT,
                    access_checked_at TIMESTAMP,
                    access_status_code INTEGER,
                    access_error TEXT,
                    auth_status TEXT,
                    auth_last_login TIMESTAMP,
                    auth_last_check TIMESTAMP,
                    last_crawled TIMESTAMP,
                    next_crawl TIMESTAMP,
                    total_crawls INTEGER DEFAULT 0,
                    success_crawls INTEGER DEFAULT 0,
                    failed_crawls INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                    updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                    FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE SET NULL,
                    FOREIGN KEY (auth_config_id) REFERENCES auth_configs(id) ON DELETE SET NULL,
                    FOREIGN KEY (parent_url_id) REFERENCES managed_urls(id) ON DELETE CASCADE
                )
                """
                
                # 创建定时任务表
                create_schedules_table = """
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_name TEXT NOT NULL,
                    task_type TEXT NOT NULL CHECK (task_type IN ('crawl', 'extract', 'export')),
                    target_url TEXT,
                    url_id INTEGER,
                    schedule_type TEXT NOT NULL CHECK (schedule_type IN ('once', 'daily', 'weekly', 'monthly', 'cron')),
                    schedule_time TIME,
                    schedule_day INTEGER,
                    cron_expression TEXT,
                    keywords TEXT,
                    industry_pack_id TEXT NOT NULL DEFAULT '',
                    industry_pack_version_id INTEGER,
                    activation_id TEXT NOT NULL DEFAULT '',
                    ownership_type TEXT NOT NULL DEFAULT 'legacy',
                    is_active BOOLEAN DEFAULT TRUE,
                    last_run TIMESTAMP,
                    next_run TIMESTAMP,
                    total_runs INTEGER DEFAULT 0,
                    success_runs INTEGER DEFAULT 0,
                    failed_runs INTEGER DEFAULT 0,
                    ragflow_kb_id TEXT,
                    days_limit INTEGER DEFAULT 7,
                    running_lock_id TEXT,
                    running_started_at TIMESTAMP,
                    config JSON,
                    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                    updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                    FOREIGN KEY (url_id) REFERENCES managed_urls(id) ON DELETE CASCADE
                )
                """
                
                # 创建任务执行历史表
                create_task_history_table = """
                CREATE TABLE IF NOT EXISTS task_execution_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_id INTEGER,
                    task_id TEXT,
                    status TEXT CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled', 'timeout', 'skipped')),
                    run_key TEXT,
                    scheduled_for TIMESTAMP,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    duration_seconds INTEGER,
                    articles_found INTEGER DEFAULT 0,
                    error_message TEXT,
                    result_summary JSON,
                    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                    FOREIGN KEY (schedule_id) REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
                    FOREIGN KEY (task_id) REFERENCES crawl_tasks(task_id) ON DELETE SET NULL
                )
                """

                # 创建智能分析派生目标 URL 表
                create_smart_targets_table = """
                CREATE TABLE IF NOT EXISTS smart_target_urls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_id INTEGER NOT NULL,
                    configured_url TEXT NOT NULL,
                    resolved_target_url TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    target_type TEXT,
                    source TEXT,
                    score REAL DEFAULT 0,
                    match_count INTEGER DEFAULT 0,
                    sample_total INTEGER DEFAULT 0,
                    matched_keywords TEXT,
                    verified_articles JSON,
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'expired', 'disabled')),
                    generated_by TEXT DEFAULT 'smart_resolver',
                    expires_at TIMESTAMP,
                    last_verified_at TIMESTAMP,
                    metadata JSON,
                    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                    updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                    FOREIGN KEY (schedule_id) REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
                    UNIQUE(schedule_id, canonical_url)
                )
                """
                
                # 执行创建表的SQL
                tables = [
                    create_categories_table,
                    create_auth_configs_table,
                    create_articles_table,
                    create_article_spacetime_profiles_table,
                    create_tasks_table,
                    create_article_tasks_table,
                    create_stats_table,
                    create_urls_table,
                    create_schedules_table,
                    create_task_history_table,
                    create_crawl_attempts_table,
                    create_smart_targets_table
                ]
                
                cursor = self.connection.cursor()
                try:
                    for table_sql in tables:
                        cursor.execute(table_sql)

                    self._run_schema_migrations(cursor)
                    # 创建索引
                    self._create_indexes(cursor)
                    
                    self.connection.commit()
                finally:
                    cursor.close()
                print("✅ SQLite数据库表创建成功")
                return True
                
        except Exception as e:
            print(f"❌ 创建数据库表失败: {e}")
            return False
    
    def _create_indexes(self, cursor=None):
        """创建索引"""
        indexes = [
            # 分类表索引
            "CREATE INDEX IF NOT EXISTS idx_categories_name ON categories(name)",
            "CREATE INDEX IF NOT EXISTS idx_categories_is_active ON categories(is_active)",
            # 文章表索引
            "CREATE INDEX IF NOT EXISTS idx_articles_url ON articles(url)",
            "CREATE INDEX IF NOT EXISTS idx_articles_domain ON articles(domain)",
            "CREATE INDEX IF NOT EXISTS idx_articles_category_id ON articles(category_id)",
            "CREATE INDEX IF NOT EXISTS idx_articles_source_url_id ON articles(source_url_id)",
            "CREATE INDEX IF NOT EXISTS idx_articles_source_task_id ON articles(source_task_id)",
            "CREATE INDEX IF NOT EXISTS idx_articles_publish_date ON articles(publish_date)",
            "CREATE INDEX IF NOT EXISTS idx_articles_last_crawled ON articles(last_crawled)",
            "CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status)",
            "CREATE INDEX IF NOT EXISTS idx_articles_canonical_url ON articles(canonical_url)",
            "CREATE INDEX IF NOT EXISTS idx_articles_crawler_engine ON articles(crawler_engine_used)",
            "CREATE INDEX IF NOT EXISTS idx_spacetime_article_id ON article_spacetime_profiles(article_id)",
            "CREATE INDEX IF NOT EXISTS idx_spacetime_time_value ON article_spacetime_profiles(time_value)",
            "CREATE INDEX IF NOT EXISTS idx_spacetime_location ON article_spacetime_profiles(location_name)",
            "CREATE INDEX IF NOT EXISTS idx_spacetime_status ON article_spacetime_profiles(spacetime_status)",
            # 聚合任务表索引
            "CREATE INDEX IF NOT EXISTS idx_tasks_task_id ON crawl_tasks(task_id)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_status ON crawl_tasks(status)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON crawl_tasks(created_at)",
            # 文章-任务关联表索引
            "CREATE INDEX IF NOT EXISTS idx_article_tasks_article_id ON article_tasks(article_id)",
            "CREATE INDEX IF NOT EXISTS idx_article_tasks_task_id ON article_tasks(task_id)",
            # 统计表索引
            "CREATE INDEX IF NOT EXISTS idx_stats_date ON crawl_stats(date)",
            # URL管理表索引
            "CREATE INDEX IF NOT EXISTS idx_urls_url ON managed_urls(url)",
            "CREATE INDEX IF NOT EXISTS idx_urls_domain ON managed_urls(domain)",
            "CREATE INDEX IF NOT EXISTS idx_urls_category_id ON managed_urls(category_id)",
            "CREATE INDEX IF NOT EXISTS idx_urls_parent_url_id ON managed_urls(parent_url_id)",
            "CREATE INDEX IF NOT EXISTS idx_urls_is_active ON managed_urls(is_active)",
            "CREATE INDEX IF NOT EXISTS idx_urls_auto_crawl ON managed_urls(auto_crawl)",
            "CREATE INDEX IF NOT EXISTS idx_urls_access_status ON managed_urls(access_status)",
            # 定时任务表索引
            "CREATE INDEX IF NOT EXISTS idx_schedules_is_active ON scheduled_tasks(is_active)",
            "CREATE INDEX IF NOT EXISTS idx_schedules_next_run ON scheduled_tasks(next_run)",
            "CREATE INDEX IF NOT EXISTS idx_schedules_url_id ON scheduled_tasks(url_id)",
            "CREATE INDEX IF NOT EXISTS idx_schedules_running_lock ON scheduled_tasks(running_lock_id)",
            # 任务执行历史表索引
            "CREATE INDEX IF NOT EXISTS idx_history_schedule_id ON task_execution_history(schedule_id)",
            "CREATE INDEX IF NOT EXISTS idx_history_task_id ON task_execution_history(task_id)",
            "CREATE INDEX IF NOT EXISTS idx_history_status ON task_execution_history(status)",
            "CREATE INDEX IF NOT EXISTS idx_history_started_at ON task_execution_history(started_at)",
            "CREATE INDEX IF NOT EXISTS idx_history_scheduled_for ON task_execution_history(scheduled_for)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_history_run_key_unique ON task_execution_history(run_key) WHERE run_key IS NOT NULL AND TRIM(run_key) != ''",
            # 聚合尝试记录表索引
            "CREATE INDEX IF NOT EXISTS idx_crawl_attempts_schedule_id ON crawl_attempts(schedule_id)",
            "CREATE INDEX IF NOT EXISTS idx_crawl_attempts_task_id ON crawl_attempts(task_id)",
            "CREATE INDEX IF NOT EXISTS idx_crawl_attempts_article_id ON crawl_attempts(article_id)",
            "CREATE INDEX IF NOT EXISTS idx_crawl_attempts_engine ON crawl_attempts(crawler_engine)",
            "CREATE INDEX IF NOT EXISTS idx_crawl_attempts_status ON crawl_attempts(status)",
            "CREATE INDEX IF NOT EXISTS idx_crawl_attempts_started_at ON crawl_attempts(started_at)",
            # 智能分析派生目标 URL 表索引
            "CREATE INDEX IF NOT EXISTS idx_smart_targets_schedule_id ON smart_target_urls(schedule_id)",
            "CREATE INDEX IF NOT EXISTS idx_smart_targets_canonical_url ON smart_target_urls(canonical_url)",
            "CREATE INDEX IF NOT EXISTS idx_smart_targets_status ON smart_target_urls(status)",
            "CREATE INDEX IF NOT EXISTS idx_smart_targets_expires_at ON smart_target_urls(expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_smart_targets_verified_at ON smart_target_urls(last_verified_at)",
            # 关键词治理表索引
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_keyword_rules_source_active ON keyword_canonical_rules(source_keyword) WHERE status = 'active'",
            "CREATE INDEX IF NOT EXISTS idx_keyword_rules_canonical ON keyword_canonical_rules(canonical_keyword)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_keyword_blocklist_active ON keyword_blocklist(keyword) WHERE status = 'active'",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_keyword_node_states_keyword ON keyword_node_states(keyword)",
            "CREATE INDEX IF NOT EXISTS idx_keyword_operations_type_created ON keyword_operation_logs(operation_type, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_keyword_delete_jobs_status ON keyword_delete_jobs(status)",
            "CREATE INDEX IF NOT EXISTS idx_keyword_delete_jobs_keyword ON keyword_delete_jobs(keyword)",
            "CREATE INDEX IF NOT EXISTS idx_article_ragflow_article ON article_ragflow_documents(article_id)",
            "CREATE INDEX IF NOT EXISTS idx_article_ragflow_kb_doc ON article_ragflow_documents(kb_id, document_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_article_ragflow_unique_doc ON article_ragflow_documents(kb_id, document_id) WHERE document_id IS NOT NULL AND TRIM(document_id) != ''",
            # 政策检索用（统一 QA）：按文档类型/文号/来源 URL 过滤
            "CREATE INDEX IF NOT EXISTS idx_article_ragflow_doc_type ON article_ragflow_documents(doc_type)",
            "CREATE INDEX IF NOT EXISTS idx_article_ragflow_doc_no ON article_ragflow_documents(doc_no)",
            "CREATE INDEX IF NOT EXISTS idx_article_ragflow_source_url ON article_ragflow_documents(source_url)"
        ]
        
        if cursor is None:
            cursor = self.connection.cursor()
            cursor_created = True
        else:
            cursor_created = False
            
        try:
            for index_sql in indexes:
                cursor.execute(index_sql)
        finally:
            if cursor_created:
                cursor.close()

    def _run_schema_migrations(self, cursor=None):
        """运行增量表结构迁移"""
        cursor_created = False
        if cursor is None:
            cursor = self.connection.cursor()
            cursor_created = True
        try:
            self._ensure_column_exists(cursor, 'scheduled_tasks', 'ragflow_kb_id', 'TEXT')
            self._ensure_column_exists(cursor, 'scheduled_tasks', 'days_limit', 'INTEGER DEFAULT 7')  # 🔥 添加日期限制字段
            self._ensure_column_exists(cursor, 'scheduled_tasks', 'schedule_weekdays', 'TEXT')  # 每周哪几天执行
            self._ensure_column_exists(cursor, 'scheduled_tasks', 'schedule_monthdays', 'TEXT')  # 每月哪几天执行
            self._ensure_column_exists(cursor, 'scheduled_tasks', 'running_lock_id', 'TEXT')
            self._ensure_column_exists(cursor, 'scheduled_tasks', 'running_started_at', 'TIMESTAMP')
            self._ensure_column_exists(cursor, 'scheduled_tasks', 'industry_pack_id', "TEXT NOT NULL DEFAULT ''")
            self._ensure_column_exists(cursor, 'scheduled_tasks', 'industry_pack_version_id', 'INTEGER')
            self._ensure_column_exists(cursor, 'scheduled_tasks', 'activation_id', "TEXT NOT NULL DEFAULT ''")
            self._ensure_column_exists(cursor, 'scheduled_tasks', 'ownership_type', "TEXT NOT NULL DEFAULT 'legacy'")
            self._ensure_column_exists(cursor, 'crawl_tasks', 'industry_pack_id', "TEXT NOT NULL DEFAULT ''")
            self._ensure_column_exists(cursor, 'crawl_tasks', 'industry_pack_version_id', 'INTEGER')
            self._ensure_column_exists(cursor, 'crawl_tasks', 'activation_id', "TEXT NOT NULL DEFAULT ''")
            self._ensure_column_exists(cursor, 'crawl_tasks', 'ownership_type', "TEXT NOT NULL DEFAULT 'legacy'")
            self._ensure_column_exists(cursor, 'crawl_tasks', 'initialization_batch_id', 'TEXT')
            self._ensure_column_exists(cursor, 'crawl_tasks', 'initialization_from', 'TEXT')
            self._ensure_column_exists(cursor, 'crawl_tasks', 'initialization_to', 'TEXT')
            self._ensure_column_exists(cursor, 'crawl_tasks', 'resource_priority', 'INTEGER DEFAULT 50')
            self._ensure_column_exists(cursor, 'crawl_tasks', 'deferred_until', 'TIMESTAMP')
            self._ensure_column_exists(cursor, 'scheduled_tasks', 'deferred_until', 'TIMESTAMP')
            self._ensure_column_exists(cursor, 'managed_urls', 'industry_pack_id', "TEXT NOT NULL DEFAULT ''")
            self._ensure_column_exists(cursor, 'managed_urls', 'industry_pack_version_id', 'INTEGER')
            self._ensure_column_exists(cursor, 'managed_urls', 'activation_id', "TEXT NOT NULL DEFAULT ''")
            self._ensure_column_exists(cursor, 'managed_urls', 'ownership_type', "TEXT NOT NULL DEFAULT 'legacy'")
            self._ensure_column_exists(cursor, 'managed_urls', 'category', 'TEXT')
            self._ensure_column_exists(cursor, 'managed_urls', 'requires_auth', 'BOOLEAN DEFAULT FALSE')
            self._ensure_column_exists(cursor, 'managed_urls', 'auth_config_id', 'INTEGER')
            self._ensure_column_exists(cursor, 'managed_urls', 'days_limit', 'INTEGER DEFAULT 7')
            self._ensure_column_exists(cursor, 'managed_urls', 'auth_status', 'TEXT')
            self._ensure_column_exists(cursor, 'managed_urls', 'auth_last_login', 'TIMESTAMP')
            self._ensure_column_exists(cursor, 'managed_urls', 'auth_last_check', 'TIMESTAMP')
            # Records created before industry packages are the original family-office
            # installation. Preserve them as history, but never let them appear or
            # execute as if they belonged to a newly activated industry.
            cursor.execute(
                """
                UPDATE scheduled_tasks
                SET industry_pack_id='family_office',
                    ownership_type=CASE
                        WHEN ownership_type IN ('', 'legacy') THEN 'legacy_family'
                        ELSE ownership_type
                    END
                WHERE TRIM(COALESCE(industry_pack_id, ''))=''
                """
            )
            cursor.execute(
                """
                UPDATE crawl_tasks
                SET industry_pack_id='family_office',
                    ownership_type=CASE
                        WHEN ownership_type IN ('', 'legacy') THEN 'legacy_family'
                        ELSE ownership_type
                    END
                WHERE TRIM(COALESCE(industry_pack_id, ''))=''
                """
            )
            cursor.execute(
                """
                UPDATE managed_urls
                SET industry_pack_id='family_office',
                    ownership_type=CASE
                        WHEN ownership_type IN ('', 'legacy') THEN 'legacy_family'
                        ELSE ownership_type
                    END
                WHERE TRIM(COALESCE(industry_pack_id, ''))=''
                """
            )
            # The migration below also has to work against a brand-new database.
            # Create the base table before attempting to add/backfill pack columns.
            from chat_storage import ensure_chat_tables
            ensure_chat_tables(cursor)
            # chat_history：AI 助手会话历史按行业包隔离；旧的无包标记数据归 family_office
            self._ensure_column_exists(cursor, 'chat_history', 'industry_pack_id', "TEXT NOT NULL DEFAULT ''")
            cursor.execute(
                "UPDATE chat_history SET industry_pack_id='family_office' WHERE industry_pack_id='' OR industry_pack_id IS NULL"
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_history_pack ON chat_history(industry_pack_id, session_id)")
            self._ensure_column_exists(cursor, 'managed_urls', 'access_status', 'TEXT')
            self._ensure_column_exists(cursor, 'managed_urls', 'access_checked_at', 'TIMESTAMP')
            self._ensure_column_exists(cursor, 'managed_urls', 'access_status_code', 'INTEGER')
            self._ensure_column_exists(cursor, 'managed_urls', 'access_error', 'TEXT')
            self._ensure_column_exists(cursor, 'articles', 'matched_keywords', 'TEXT')  # 🔥 文章匹配的关键词
            self._ensure_column_exists(cursor, 'articles', 'matched_keywords_raw', 'TEXT')
            self._ensure_column_exists(cursor, 'articles', 'keyword_match_detail', 'TEXT')
            self._ensure_column_exists(cursor, 'articles', 'crawler_engine_used', 'TEXT')
            self._ensure_column_exists(cursor, 'articles', 'crawler_engines', 'TEXT')
            self._ensure_column_exists(cursor, 'articles', 'crawler_attempts', 'INTEGER DEFAULT 0')
            self._ensure_column_exists(cursor, 'articles', 'fallback_trigger_reason', 'TEXT')
            self._ensure_column_exists(cursor, 'articles', 'source_method', 'TEXT')
            self._ensure_column_exists(cursor, 'articles', 'configured_url', 'TEXT')
            self._ensure_column_exists(cursor, 'articles', 'resolved_target_url', 'TEXT')
            self._ensure_column_exists(cursor, 'articles', 'canonical_url', 'TEXT')
            self._ensure_column_exists(cursor, 'articles', 'source_task_id', 'TEXT')
            self._ensure_column_exists(cursor, 'articles', 'source_task_name', 'TEXT')
            self._ensure_column_exists(cursor, 'articles', 'published_at_utc', 'TEXT')
            self._ensure_column_exists(cursor, 'articles', 'published_timezone', 'TEXT')
            self._ensure_column_exists(cursor, 'articles', 'published_precision', 'TEXT')
            self._ensure_column_exists(cursor, 'articles', 'published_time_source', 'TEXT')
            # 🔥 阶段1：正文统一 Markdown —— content_markdown 仅展示用；raw_content 保存原始正文快照
            self._ensure_article_markdown_columns(cursor)
            # 🔥 阶段3：实时动态 HTML 链接转换缓存表
            from dynamic_link_converter import ensure_dynamic_converted_table
            ensure_dynamic_converted_table(cursor)
            # 🔥 阶段4：信源检查一键学习（AutoScraper）模型表
            from site_scraper_models import ensure_site_scraper_models_table
            ensure_site_scraper_models_table(cursor)
            self._ensure_column_exists(cursor, 'task_execution_history', 'run_key', 'TEXT')
            self._ensure_column_exists(cursor, 'task_execution_history', 'scheduled_for', 'TIMESTAMP')
            self._ensure_crawl_attempts_table(cursor)
            self._ensure_smart_target_urls_table(cursor)
            self._ensure_keyword_governance_tables(cursor)
            from intel_schema import ensure_intel_core_tables
            ensure_intel_core_tables(cursor)
            # Financial schema migrations only run from this explicit startup
            # initialization path.  Normal connection/request paths must not
            # perform these additive DDL migrations.
            from financial_schema import ensure_financial_tables
            ensure_financial_tables(cursor)
            self._backfill_article_source_tasks(cursor)
            self._migrate_task_execution_history_statuses(cursor)
            # 在线助手历史对话表
            from chat_storage import ensure_chat_tables
            ensure_chat_tables(cursor)
        finally:
            if cursor_created:
                cursor.close()

    def _ensure_crawl_attempts_table(self, cursor):
        """Create the crawler attempt audit table for AI fallback tracing."""
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crawl_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_id INTEGER,
                task_id TEXT,
                article_id INTEGER,
                configured_url TEXT,
                resolved_target_url TEXT,
                canonical_url TEXT,
                crawler_engine TEXT NOT NULL,
                phase TEXT,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'completed', 'failed', 'skipped')),
                fallback_trigger_reason TEXT,
                articles_found INTEGER DEFAULT 0,
                candidate_urls INTEGER DEFAULT 0,
                keyword_hits INTEGER DEFAULT 0,
                error_message TEXT,
                metadata JSON,
                started_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                completed_at TIMESTAMP,
                duration_seconds REAL,
                created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (schedule_id) REFERENCES scheduled_tasks(id) ON DELETE SET NULL,
                FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE SET NULL,
                FOREIGN KEY (task_id) REFERENCES crawl_tasks(task_id) ON DELETE SET NULL
            )
        """)

    def _ensure_smart_target_urls_table(self, cursor):
        """Create the smart resolver derived target URL table."""
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS smart_target_urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_id INTEGER NOT NULL,
                configured_url TEXT NOT NULL,
                resolved_target_url TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                target_type TEXT,
                source TEXT,
                score REAL DEFAULT 0,
                match_count INTEGER DEFAULT 0,
                sample_total INTEGER DEFAULT 0,
                matched_keywords TEXT,
                verified_articles JSON,
                status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'expired', 'disabled')),
                generated_by TEXT DEFAULT 'smart_resolver',
                expires_at TIMESTAMP,
                last_verified_at TIMESTAMP,
                metadata JSON,
                created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (schedule_id) REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
                UNIQUE(schedule_id, canonical_url)
            )
        """)

    def _ensure_keyword_governance_tables(self, cursor):
        """Create keyword governance and RAGFlow document mapping tables."""
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS keyword_canonical_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_keyword TEXT NOT NULL,
                canonical_keyword TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
                created_by TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS keyword_blocklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,
                reason TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
                created_by TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS keyword_node_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL DEFAULT 'visible' CHECK (state IN ('visible', 'hidden')),
                reason TEXT DEFAULT '',
                created_by TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS keyword_operation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_type TEXT NOT NULL,
                payload_json TEXT DEFAULT '',
                affected_articles INTEGER DEFAULT 0,
                affected_tasks INTEGER DEFAULT 0,
                affected_ragflow_docs INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed', 'failed', 'partial_failed')),
                error_message TEXT DEFAULT '',
                created_by TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS keyword_delete_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,
                confirm_token TEXT DEFAULT '',
                payload_json TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'completed', 'failed', 'partial_failed', 'cancelled')),
                ragflow_delete_status TEXT DEFAULT 'pending' CHECK (ragflow_delete_status IN ('pending', 'skipped', 'completed', 'failed', 'partial_failed')),
                error_message TEXT DEFAULT '',
                created_by TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS article_ragflow_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER,
                kb_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                document_name TEXT NOT NULL,
                sync_status TEXT NOT NULL DEFAULT 'uploaded' CHECK (sync_status IN ('uploaded', 'parsed', 'delete_pending', 'deleted', 'delete_failed')),
                error_message TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                -- 政策文档元数据（统一 QA 的政策检索按这些列过滤，见 qa_retrieval / qa_policy_evidence）
                doc_type TEXT DEFAULT '',
                issuer TEXT DEFAULT '',
                doc_no TEXT DEFAULT '',
                article_no TEXT DEFAULT '',
                policy_title TEXT DEFAULT '',
                publish_date TEXT DEFAULT '',
                effective_date TEXT DEFAULT '',
                source_url TEXT DEFAULT '',
                authority_level INTEGER DEFAULT 0,
                metadata_json TEXT DEFAULT '',
                FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE SET NULL
            )
        """)
        self._ensure_article_ragflow_policy_columns(cursor)

    @staticmethod
    def _ensure_article_ragflow_policy_columns(cursor) -> None:
        """老库补列：article_ragflow_documents 的政策元数据列与索引。

        统一 QA（AI 助手）的政策检索会 `COALESCE(ard.doc_type,'')` 这类过滤，
        缺列会直接 UndefinedColumn 让问答失败，所以老库必须补上。
        PG / SQLite 都不支持 ADD COLUMN IF NOT EXISTS 的通用写法，逐个 try 即可（已存在就跳过）。
        """
        for name, ddl in (
            ("doc_type", "TEXT DEFAULT ''"),
            ("issuer", "TEXT DEFAULT ''"),
            ("doc_no", "TEXT DEFAULT ''"),
            ("article_no", "TEXT DEFAULT ''"),
            ("policy_title", "TEXT DEFAULT ''"),
            ("publish_date", "TEXT DEFAULT ''"),
            ("effective_date", "TEXT DEFAULT ''"),
            ("source_url", "TEXT DEFAULT ''"),
            ("authority_level", "INTEGER DEFAULT 0"),
            ("metadata_json", "TEXT DEFAULT ''"),
        ):
            try:
                cursor.execute(
                    "ALTER TABLE article_ragflow_documents ADD COLUMN %s %s" % (name, ddl)
                )
            except Exception:
                continue  # 列已存在
        for index_sql in (
            "CREATE INDEX IF NOT EXISTS idx_article_ragflow_doc_type ON article_ragflow_documents(doc_type)",
            "CREATE INDEX IF NOT EXISTS idx_article_ragflow_doc_no ON article_ragflow_documents(doc_no)",
            "CREATE INDEX IF NOT EXISTS idx_article_ragflow_source_url ON article_ragflow_documents(source_url)",
        ):
            try:
                cursor.execute(index_sql)
            except Exception:
                continue

    def _resolve_task_source(self, cursor, task_id: str) -> Tuple[str, Optional[str]]:
        """Return a user-facing task name and schedule id for an exact task id."""
        if not task_id:
            return '', None

        task_name = ''
        schedule_id = None
        schedule_match = re.match(r'^schedule_(\d+)_', str(task_id))
        if schedule_match:
            schedule_id = schedule_match.group(1)
            cursor.execute(
                "SELECT task_name FROM scheduled_tasks WHERE id = ?",
                (schedule_id,)
            )
            schedule_row = cursor.fetchone()
            if schedule_row and schedule_row['task_name'] and not _is_placeholder_source_task(schedule_row['task_name']):
                task_name = schedule_row['task_name']

        if not task_name:
            cursor.execute(
                "SELECT task_name FROM crawl_tasks WHERE task_id = ?",
                (task_id,)
            )
            task_row = cursor.fetchone()
            if task_row and task_row['task_name'] and not _is_placeholder_source_task(task_row['task_name']):
                task_name = task_row['task_name']

        return task_name or str(task_id), schedule_id

    def _backfill_article_source_tasks(self, cursor):
        """Populate article source task fields from exact article_tasks links."""
        cursor.execute(
            """
            SELECT a.id, latest.task_id
            FROM articles a
            JOIN (
                SELECT at1.article_id, at1.task_id
                FROM article_tasks at1
                JOIN (
                    SELECT article_id, MAX(id) AS latest_id
                    FROM article_tasks
                    GROUP BY article_id
                ) latest_link ON latest_link.latest_id = at1.id
            ) latest ON latest.article_id = a.id
            WHERE (a.source_task_id IS NULL OR TRIM(a.source_task_id) = '')
               OR (a.source_task_name IS NULL OR TRIM(a.source_task_name) = '')
               OR LOWER(TRIM(a.source_task_name)) = 'source task'
               OR REPLACE(LOWER(TRIM(a.source_task_name)), ' ', '') = 'sourcetask'
               OR TRIM(a.source_task_name) = '来源任务'
            """
        )
        rows = cursor.fetchall()
        for row in rows:
            task_id = row['task_id']
            task_name, _schedule_id = self._resolve_task_source(cursor, task_id)
            cursor.execute(
                """
                UPDATE articles
                SET source_task_id = ?,
                    source_task_name = ?
                WHERE id = ?
                """,
                (task_id, task_name, row['id'])
            )

    def _migrate_task_execution_history_statuses(self, cursor):
        """Rebuild old history tables whose CHECK constraint lacks current statuses."""
        cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'task_execution_history'"
        )
        row = cursor.fetchone()
        table_sql = ''
        if row:
            try:
                table_sql = row['sql'] if isinstance(row, sqlite3.Row) else row[0]
            except Exception:
                table_sql = ''
        if not table_sql or "'completed'" in table_sql:
            return

        backup_table = 'task_execution_history_migration_backup'
        cursor.execute(f"DROP TABLE IF EXISTS {backup_table}")
        cursor.execute(f"ALTER TABLE task_execution_history RENAME TO {backup_table}")
        cursor.execute(
            """
            CREATE TABLE task_execution_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_id INTEGER,
                task_id TEXT,
                status TEXT CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled', 'timeout', 'skipped')),
                run_key TEXT,
                scheduled_for TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                duration_seconds INTEGER,
                articles_found INTEGER DEFAULT 0,
                error_message TEXT,
                result_summary JSON,
                created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (schedule_id) REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
                FOREIGN KEY (task_id) REFERENCES crawl_tasks(task_id) ON DELETE SET NULL
            )
            """
        )
        cursor.execute(
            f"""
            INSERT INTO task_execution_history (
                id, schedule_id, task_id, status, run_key, scheduled_for, started_at, completed_at,
                duration_seconds, articles_found, error_message, result_summary, created_at
            )
            SELECT
                id,
                schedule_id,
                task_id,
                CASE WHEN status = 'success' THEN 'completed' ELSE status END,
                NULL,
                NULL,
                started_at,
                completed_at,
                duration_seconds,
                articles_found,
                error_message,
                result_summary,
                created_at
            FROM {backup_table}
            """
        )
        cursor.execute(f"DROP TABLE {backup_table}")

    def _ensure_column_exists(self, cursor, table_name: str, column_name: str, column_definition: str):
        """如果缺少列则自动添加"""
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = [row['name'] if isinstance(row, sqlite3.Row) else row[1] for row in cursor.fetchall()]
        if column_name not in columns:
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")

    @staticmethod
    def _ensure_article_markdown_columns(cursor):
        """阶段1：articles 补列 raw_content / content_markdown（兼容 SQLite / PostgreSQL 双模式）。

        Postgres 用 ADD COLUMN IF NOT EXISTS；SQLite 不支持该语法，退化为直接 ADD
        （列已存在时报重复列错误并被吞掉），与 intel_schema 的补列模式一致。
        """
        for _sql in (
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS raw_content TEXT",
            "ALTER TABLE articles ADD COLUMN raw_content TEXT",
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS content_markdown TEXT",
            "ALTER TABLE articles ADD COLUMN content_markdown TEXT",
        ):
            try:
                cursor.execute(_sql)
            except Exception:
                continue

    def record_crawl_attempt(self, attempt_data: Dict) -> Optional[int]:
        """Record one primary crawler or Crawl4AI attempt for later audit."""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    metadata = attempt_data.get('metadata')
                    if isinstance(metadata, (dict, list, tuple)):
                        metadata = json.dumps(metadata, ensure_ascii=False)
                    elif metadata is None:
                        metadata = ''

                    insert_sql = """
                    INSERT INTO crawl_attempts (
                        schedule_id, task_id, article_id, configured_url, resolved_target_url,
                        canonical_url, crawler_engine, phase, status, fallback_trigger_reason,
                        articles_found, candidate_urls, keyword_hits, error_message, metadata,
                        started_at, completed_at, duration_seconds
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                    values = (
                        coerce_int(attempt_data.get('schedule_id'), None),
                        attempt_data.get('task_id') or None,
                        coerce_int(attempt_data.get('article_id'), None),
                        attempt_data.get('configured_url') or '',
                        attempt_data.get('resolved_target_url') or '',
                        attempt_data.get('canonical_url') or '',
                        attempt_data.get('crawler_engine') or 'primary',
                        attempt_data.get('phase') or '',
                        attempt_data.get('status') or 'pending',
                        attempt_data.get('fallback_trigger_reason') or '',
                        coerce_int(attempt_data.get('articles_found'), 0),
                        coerce_int(attempt_data.get('candidate_urls'), 0),
                        coerce_int(attempt_data.get('keyword_hits'), 0),
                        attempt_data.get('error_message') or '',
                        metadata,
                        attempt_data.get('started_at') or get_china_time().strftime('%Y-%m-%d %H:%M:%S'),
                        attempt_data.get('completed_at'),
                        attempt_data.get('duration_seconds')
                    )
                    cursor.execute(insert_sql, values)
                    attempt_id = cursor.lastrowid
                    self.connection.commit()
                    return attempt_id
                finally:
                    cursor.close()
        except Exception as e:
            print(f"⚠️ 记录聚合尝试失败: {e}")
            return None

    def get_crawl_attempts_for_task(self, task_id: str) -> List[Dict]:
        """Return crawler attempt audit records for one crawl task id."""
        if not task_id:
            return []
        try:
            self._ensure_connection()
            cursor = self.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT *
                    FROM crawl_attempts
                    WHERE task_id = ?
                    ORDER BY started_at DESC, id DESC
                    """,
                    (task_id,)
                )
                return [dict(row) for row in cursor.fetchall()]
            finally:
                cursor.close()
        except Exception as e:
            print(f"⚠️ 查询聚合尝试失败: {e}")
            return []

    def get_crawl_attempts_for_schedule(self, schedule_id: int, limit: int = 20) -> List[Dict]:
        """Return crawler attempt audit records for one scheduled task."""
        schedule_id = coerce_int(schedule_id, None)
        if not schedule_id:
            return []
        try:
            self._ensure_connection()
            cursor = self.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT *
                    FROM crawl_attempts
                    WHERE schedule_id = ?
                    ORDER BY started_at DESC, id DESC
                    LIMIT ?
                    """,
                    (schedule_id, coerce_int(limit, 20, 1, 200))
                )
                results = []
                for row in cursor.fetchall():
                    item = dict(row)
                    if item.get('metadata'):
                        try:
                            item['metadata'] = json.loads(item['metadata'])
                        except Exception:
                            pass
                    results.append(item)
                return results
            finally:
                cursor.close()
        except Exception as e:
            print(f"⚠️ 查询调度任务聚合尝试失败: {e}")
            return []

    def save_smart_target_urls(
        self,
        schedule_id: int,
        configured_url: str,
        targets: List[Dict],
        generated_by: str = 'smart_resolver',
        expires_at=None,
    ) -> int:
        """Upsert derived smart target URLs for one scheduled task."""
        schedule_id = coerce_int(schedule_id, None)
        if not schedule_id:
            return 0

        saved = 0
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    for target in targets or []:
                        resolved_url = (target.get('url') or target.get('resolved_target_url') or '').strip()
                        canonical = (target.get('canonical_url') or '').strip()
                        if not canonical:
                            try:
                                from smart_target_resolver import canonical_url as _smart_canonical_url
                                canonical = _smart_canonical_url(resolved_url)
                            except Exception:
                                canonical = resolved_url.strip()
                        if not resolved_url or not canonical:
                            continue

                        matched_keywords = target.get('matched_keywords') or []
                        if isinstance(matched_keywords, (list, tuple, set)):
                            matched_keywords = ','.join(str(item) for item in matched_keywords if str(item).strip())
                        verified_articles = target.get('verified_articles') or []
                        if isinstance(verified_articles, (list, tuple, dict)):
                            verified_articles = json.dumps(verified_articles, ensure_ascii=False)
                        metadata = target.get('metadata') or {}
                        if isinstance(metadata, (list, tuple, dict)):
                            metadata = json.dumps(metadata, ensure_ascii=False)

                        cursor.execute(
                            """
                            INSERT INTO smart_target_urls (
                                schedule_id, configured_url, resolved_target_url, canonical_url,
                                target_type, source, score, match_count, sample_total,
                                matched_keywords, verified_articles, status, generated_by,
                                expires_at, last_verified_at, metadata, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, datetime('now', 'localtime'), ?, datetime('now', 'localtime'))
                            ON CONFLICT(schedule_id, canonical_url) DO UPDATE SET
                                configured_url = excluded.configured_url,
                                resolved_target_url = excluded.resolved_target_url,
                                target_type = excluded.target_type,
                                source = excluded.source,
                                score = excluded.score,
                                match_count = excluded.match_count,
                                sample_total = excluded.sample_total,
                                matched_keywords = excluded.matched_keywords,
                                verified_articles = excluded.verified_articles,
                                status = 'active',
                                generated_by = excluded.generated_by,
                                expires_at = excluded.expires_at,
                                last_verified_at = excluded.last_verified_at,
                                metadata = excluded.metadata,
                                updated_at = datetime('now', 'localtime')
                            """,
                            (
                                schedule_id,
                                configured_url or '',
                                resolved_url,
                                canonical,
                                target.get('target_type') or '',
                                target.get('source') or '',
                                float(target.get('score') or 0),
                                coerce_int(target.get('match_count'), 0),
                                coerce_int(target.get('sample_total'), 0),
                                matched_keywords,
                                verified_articles,
                                generated_by,
                                expires_at,
                                metadata,
                            )
                        )
                        saved += 1
                    self.connection.commit()
                    return saved
                finally:
                    cursor.close()
        except Exception as e:
            print(f"⚠️ 保存智能派生URL失败: {e}")
            return saved

    def get_active_smart_target_urls(self, schedule_id: int, include_expired: bool = False) -> List[Dict]:
        """Return active derived target URLs for one scheduled task."""
        schedule_id = coerce_int(schedule_id, None)
        if not schedule_id:
            return []
        try:
            self._ensure_connection()
            cursor = self.connection.cursor()
            try:
                where = "schedule_id = ?"
                params = [schedule_id]
                if not include_expired:
                    where += " AND status = 'active' AND (expires_at IS NULL OR expires_at > datetime('now', 'localtime'))"
                cursor.execute(
                    f"""
                    SELECT *
                    FROM smart_target_urls
                    WHERE {where}
                    ORDER BY match_count DESC, score DESC, last_verified_at DESC, id DESC
                    """,
                    params,
                )
                results = []
                for row in cursor.fetchall():
                    item = dict(row)
                    for key in ('verified_articles', 'metadata'):
                        if item.get(key):
                            try:
                                item[key] = json.loads(item[key])
                            except Exception:
                                pass
                    results.append(item)
                return results
            finally:
                cursor.close()
        except Exception as e:
            print(f"⚠️ 查询智能派生URL失败: {e}")
            return []

    def expire_smart_target_urls(self, schedule_id: int) -> bool:
        """Mark derived target URLs for one task as expired."""
        schedule_id = coerce_int(schedule_id, None)
        if not schedule_id:
            return False
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    cursor.execute(
                        """
                        UPDATE smart_target_urls
                        SET status = 'expired',
                            updated_at = datetime('now', 'localtime')
                        WHERE schedule_id = ? AND status = 'active'
                        """,
                        (schedule_id,)
                    )
                    self.connection.commit()
                    return True
                finally:
                    cursor.close()
        except Exception as e:
            print(f"⚠️ 过期智能派生URL失败: {e}")
            return False
    
    def insert_article(self, article_data: Dict, *, skip_pipeline: bool = False) -> Optional[int]:
        """
        插入文章数据

        Args:
            article_data: 文章数据字典
            skip_pipeline: 手动发文场景置 True——跳过统一 enrich 精炼与自动分类入队
                （文章已由编辑者定好标签/主题/正文，不再送 LLM 清洗加工）

        Returns:
            Optional[int]: 插入的文章ID，失败返回None
        """
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    # 标题前缀清洗（最终入库闸门）：汽车之家等站点的"[图] / 【图】"
                    # 是图集标记而非标题内容，统一剥掉，避免污染标题与后续检索。
                    _raw_title = str(article_data.get('title') or '')
                    _clean_title = re.sub(r'^\s*[\[【]\s*图\s*[\]】]\s*', '', _raw_title)
                    if _clean_title != _raw_title:
                        article_data['title'] = _clean_title

                    # URL 语言参数清洗（最终入库闸门）：X 等站点把 ?lang=xx 写进链接后，
                    # 抓到的是界面语言版本，标题会混进印地语/保加利亚语等外文（看着像乱码），
                    # 同一篇文章也会被当成不同 URL。入库前统一剥掉这类语言/展示参数。
                    _raw_url = str(article_data.get('url') or '')
                    _clean_url = strip_url_presentation_params(_raw_url)
                    if _clean_url != _raw_url:
                        article_data['url'] = _clean_url
                        if str(article_data.get('canonical_url') or '') == _raw_url:
                            article_data['canonical_url'] = _clean_url

                    # Final ingestion guard: every crawler path must provide
                    # at least one keyword hit.  This protects the article
                    # store even if a legacy/incremental crawler forgot its
                    # earlier keyword filter.
                    raw_matched_keywords = article_data.get('matched_keywords', '')
                    has_keyword_match = bool(
                        _parse_matched_keyword_text(raw_matched_keywords)
                    )
                    if config.CRAWL_REQUIRE_KEYWORD_MATCH and not has_keyword_match:
                        print(
                            "⏭️ 跳过无关键词命中的文章（最终入库闸门）: "
                            f"{str(article_data.get('title') or '无标题')[:50]}..."
                        )
                        return None

                    # 未来日期校验（最终入库闸门）：所有聚合路径都必须经过这里。
                    # 抽出来的日期若晚于今天，说明解析到了错误的时间源
                    # （例如把"2026年度会议"的会议日期当成了发布日期），
                    # 这类日期不可采信——直接抹掉，宁可显示"未知"，也不存一个假日期。
                    _publish_date = str(article_data.get('publish_date') or '').strip()
                    if _publish_date and _is_implausible_future_date(_publish_date):
                        print(
                            "⚠️ 发布日期在未来，已丢弃该日期: "
                            f"{_publish_date} · {str(article_data.get('title') or '无标题')[:40]}"
                        )
                        article_data['publish_date'] = ''
                    # 无发布日期补救（信源水位线，最终入库闸门）：
                    # 同一域名下的 URL 经去重后仍是新链接，说明发布时间落在
                    # (上次获取时间, 本次获取时间] 区间内，可用本次聚合日期近似。
                    # 区间越窄越可信：≤3 天=高置信；≤30 天=中置信；>30 天或无基线
                    # （该域名首次抓取）不推断，保持无日期。freebuf/cnpc 这类整站
                    # 抽不到日期的信源，过去会因无日期被整批丢弃，现在有了可信近似。
                    if not str(article_data.get('publish_date') or '').strip():
                        try:
                            _wm_domain = str(article_data.get('domain') or '').strip() or self._extract_domain(str(article_data.get('url') or ''))
                            _wm_gap = _source_crawl_gap_days(cursor, _wm_domain)
                            if _wm_gap is not None and _wm_gap <= _WATERMARK_MAX_GAP_DAYS:
                                _wm_level = 'high' if _wm_gap <= _WATERMARK_HIGH_GAP_DAYS else 'medium'
                                article_data['publish_date'] = get_china_time().strftime('%Y-%m-%d')
                                article_data['published_precision'] = 'day'
                                article_data['published_time_source'] = 'crawl_watermark:%s' % _wm_level
                                print(
                                    "🗓️ 无发布日期，按信源水位线近似（%s 置信，上次获取距今 %d 天）: %s · %s"
                                    % (_wm_level, _wm_gap, article_data['publish_date'], str(article_data.get('title') or '无标题')[:40])
                                )
                        except Exception as _wm_exc:
                            print(f"⚠️ 信源水位线近似失败，按无日期处理: {_wm_exc}")
                    # 时效闸门（最终入库闸门）：发布日期超过保留窗口的内容不再入库。
                    # 运营口径：只保留 1 年内的内容。所有聚合路径都经过这里，
                    # 因此定时任务 / VPN 流水线等旁路也一并受约束。
                    _keep_date = str(article_data.get('publish_date') or '').strip()
                    if _keep_date and _is_stale_publish_date(_keep_date, _ARTICLE_RETENTION_DAYS):
                        print(
                            "⏭️ 跳过超出保留窗口的文章（最终入库闸门）: "
                            f"{_keep_date} · {str(article_data.get('title') or '无标题')[:40]}"
                        )
                        return None

                    # Final protection for legacy crawlers which still call
                    # insert_article directly.  They do not have candidate
                    # audit context, but must never persist a link directory
                    # as an industry article.
                    try:
                        from intel_content_quality_gate import assess_article_quality
                        final_quality = assess_article_quality(article_data, {})
                        _hard_issues = final_quality.get("issues", [])
                        if "content_is_link_directory" in _hard_issues:
                            print(
                                "⏭️ 跳过链接目录页（最终入库闸门）: "
                                f"{str(article_data.get('title') or '无标题')[:50]}..."
                            )
                            return None
                        # 作者主页/聚合列表页：不是单篇文章，拒绝入库
                        if "content_is_author_profile_or_title_list" in _hard_issues:
                            print(
                                "⏭️ 跳过作者主页/聚合列表页（最终入库闸门）: "
                                f"{str(article_data.get('title') or '无标题')[:50]}..."
                            )
                            return None
                    except Exception as quality_exc:
                        print(f"⚠️ 最终链接目录检查失败，继续由上游准入处理: {quality_exc}")

                    # 获取原始URL
                    url = article_data.get('url', '')
                    
                    # 🔥 过滤图片URL
                    image_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg', '.ico')
                    if url.lower().endswith(image_extensions) or '/upload/editor-images/' in url.lower():
                        print(f"⏭️ 跳过图片URL: {url[:80]}...")
                        return None
                    
                    # 🔧 URL标准化：已禁用
                    # 原因：我们使用Playwright的page.url，这已经是正确的URL了
                    # 不需要再次转换，否则会把正确的URL转换成错误的
                    # try:
                    #     from url_transformation_rules import transform_url
                    #     normalized_url = transform_url(url, verbose=False, verify=False)
                    #     if normalized_url != url:
                    #         print(f"🔄 URL标准化: {url[:60]}... → {normalized_url[:60]}...")
                    #         url = normalized_url
                    #         article_data['url'] = url
                    # except Exception as e:
                    #     print(f"⚠️  URL标准化失败: {e}，使用原始URL")
                    
                    # 生成内容哈希
                    content = article_data.get('content', '')
                    # 入库清洗：去掉源站导航/面包屑/页头页脚链接/分页噪音，让存储正文干净
                    content = clean_article_markdown(content)
                    # 正文开头的重复标题行（抓取器常把网页标题原样塞进正文）删除
                    content = _dedup_title_lines(content, article_data.get('title') or '')
                    article_data['content'] = content
                    content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()

                    # 🔥 阶段1：正文统一 Markdown —— 保存原始正文快照并生成展示用 Markdown
                    raw_content = str(article_data.get('raw_content') or '').strip() or content
                    # vpn_ocr：OCR 原文含导航噪声，展示 Markdown 优先用 content（摘要/精炼文），
                    # 原文仍存 raw_content 供"原文比对"备查
                    _md_raw = (
                        '' if str(article_data.get('extraction_method') or '') == 'vpn_ocr'
                        else article_data.get('raw_content')
                    )
                    try:
                        from content_handlers import build_article_markdown
                        content_markdown = build_article_markdown(_md_raw, content)
                    except Exception as _md_exc:
                        content_markdown = content
                        print(f"⚠️ Markdown 转换失败，退回纯文本: {_md_exc}")

                    # 提取域名（允许显式覆盖，供 agent-search 等来源打标；默认从 URL 推导）
                    domain = str(article_data.get('domain') or '').strip() or self._extract_domain(url)
                    
                    # 获取标题
                    title = article_data.get('title', '无标题')
                    # 标题噪音清理：Markdown 里与文章标题重复的标题行（# 标题）删除，
                    # 避免详情页正文开头再出现一遍大标题
                    content_markdown = _dedup_title_heading_lines(content_markdown, title)
                    
                    
                    # 处理发布日期
                    publish_date = article_data.get('publish_date')
                    if publish_date and isinstance(publish_date, str):
                        try:
                            publish_date = datetime.strptime(publish_date, '%Y-%m-%d').date()
                        except:
                            publish_date = None
                    
                    # ========== 🔥 多重去重检查 ==========
                    existing_id = None
                    
                    # 检查1: 通过URL去重
                    existing_id = self.get_article_id_by_url(url)
                    if existing_id:
                        print(f"🔁 检测到重复文章(URL相同): {title[:30]}... (ID: {existing_id})")
                        return self.update_article(existing_id, article_data)
                    
                    # 检查2: 通过标题+域名去重
                    check_sql = """
                    SELECT id FROM articles 
                    WHERE title = ? AND domain = ? AND status = 'active'
                    LIMIT 1
                    """
                    cursor.execute(check_sql, (title, domain))
                    result = cursor.fetchone()
                    if result:
                        existing_id = result[0]
                        print(f"🔁 检测到重复文章(标题+域名相同): {title[:30]}... (ID: {existing_id})")
                        print(f"   已存在URL与新URL不同，更新为新URL: {url[:60]}...")
                        return self.update_article(existing_id, article_data)
                    
                    # 检查3: 通过内容哈希+域名去重（防止标题略有不同但内容相同）
                    # 🔥 只有当内容足够长（>=300字）时才进行内容哈希去重，避免提取失败导致误判
                    if content and len(content) >= 300:
                        check_sql = """
                        SELECT id, title, content_length FROM articles 
                        WHERE content_hash = ? AND domain = ? AND status = 'active'
                        LIMIT 1
                        """
                        cursor.execute(check_sql, (content_hash, domain))
                        result = cursor.fetchone()
                        if result:
                            existing_id = result[0]
                            existing_title = result[1]
                            existing_length = result[2] or 0
                            # 🔥 只有当已存在文章也足够长时才认为是重复
                            if existing_length >= 300:
                                print(f"🔁 检测到重复文章(内容哈希+域名相同): {title[:30]}...")
                                print(f"   已存在文章: {existing_title[:30]}... (ID: {existing_id})")
                                print(f"   标题或URL略有不同，更新为新数据")
                                return self.update_article(existing_id, article_data)
                    
                    # 插入新文章
                    # 手动设置中国时间，确保时区正确
                    china_time = get_china_time().strftime('%Y-%m-%d %H:%M:%S')
                    # 获取匹配的关键词（如果有的话）
                    matched_keywords = article_data.get('matched_keywords', '')
                    if isinstance(matched_keywords, list):
                        matched_keywords = ','.join(matched_keywords)
                    matched_keywords_raw = article_data.get('matched_keywords_raw') or matched_keywords
                    try:
                        from keyword_governance import get_keyword_governance, parse_keyword_text
                        raw_keyword_list = parse_keyword_text(matched_keywords_raw)
                        canonical_keyword_list = get_keyword_governance().normalize_keyword_list(raw_keyword_list)
                        keyword_match_detail = article_data.get('keyword_match_detail') or json.dumps([
                            {'raw': raw, 'canonical': get_keyword_governance().normalize_keyword(raw)}
                            for raw in raw_keyword_list
                            if get_keyword_governance().normalize_keyword(raw)
                        ], ensure_ascii=False)
                        matched_keywords = ','.join(canonical_keyword_list)
                    except Exception:
                        keyword_match_detail = article_data.get('keyword_match_detail') or ''
                    
                    insert_sql = """
                    INSERT INTO articles (
                        url, title, content, domain, category_id, source_url_id, 
                        publish_date, content_hash, content_length, extraction_method, quality_score,
                        matched_keywords, matched_keywords_raw, keyword_match_detail,
                        crawler_engine_used, crawler_engines, crawler_attempts,
                        fallback_trigger_reason, source_method, configured_url, resolved_target_url,
                        canonical_url, source_task_id, source_task_name,
                        first_crawled, last_crawled, created_at, updated_at,
                        published_time_source, published_precision, raw_content, content_markdown
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        datetime('now'), datetime('now'), ?, ?, ?, ?, ?, ?
                    )
                    """
                    
                    crawler_engine = article_data.get('crawler_engine_used') or article_data.get('crawler_engine') or 'primary'
                    crawler_engines = article_data.get('crawler_engines') or crawler_engine
                    if isinstance(crawler_engines, (list, tuple, set)):
                        crawler_engines = ','.join(str(item) for item in crawler_engines if str(item).strip())

                    values = (
                        url,
                        title,
                        content,
                        domain,
                        article_data.get('category_id'),
                        article_data.get('source_url_id'),
                        publish_date,
                        content_hash,
                        len(content),
                        article_data.get('extraction_method', 'unknown'),
                        article_data.get('quality_score', 0),
                        matched_keywords,
                        matched_keywords_raw,
                        keyword_match_detail,
                        crawler_engine,
                        crawler_engines,
                        coerce_int(article_data.get('crawler_attempts'), 1),
                        article_data.get('fallback_trigger_reason') or '',
                        article_data.get('source_method') or '',
                        article_data.get('configured_url') or '',
                        article_data.get('resolved_target_url') or '',
                        article_data.get('canonical_url') or url,
                        article_data.get('source_task_id') or '',
                        article_data.get('source_task_name') or '',
                        china_time,
                        china_time,
                        article_data.get('published_time_source') or '',
                        article_data.get('published_precision') or '',
                        raw_content,
                        content_markdown
                    )
                    
                    # 框架页判废（最终入库闸门，所有入库路径共享）：
                    # 正文整页都是分享/评论/热文推荐等模板文字、几乎没有有效段落时直接不入库。
                    # 实例：2016亚太财富论坛…风云榜，410 字全是页面框架，曾进库并污染知识库。
                    try:
                        from intel_boilerplate import assess as _assess_boilerplate

                        _boiler = _assess_boilerplate(content, str(article_data.get('title') or ''))
                        if _boiler.get("is_boilerplate"):
                            print("⏭️ 跳过页面框架（无有效正文）: %s · %s" % (
                                str(article_data.get('title') or '无标题')[:40],
                                str(_boiler.get("reason") or "")[:80],
                            ))
                            return None
                    except Exception:
                        pass
                    cursor.execute(insert_sql, values)
                    article_id = cursor.lastrowid
                    self.connection.commit()
                    
                    print(f"✅ 文章入库成功: {title[:30]}... (ID: {article_id})")
                    self.analyze_article_spacetime_profile(article_id)
                    # 归属必填（收口在这里，所有入库路径共享）：
                    # 分类任务是异步的、且关键词门禁不达标时不写分类行，所以先落一条
                    # 兜底归属（该包的「其他」分类），异步分类之后可升级为真实分类。
                    # 不这么做就会出现"页面看得到、AI 搜不到"（列表按关键词筛、
                    # 检索按包归属筛，两条链路口径不一致）。
                    # skip_pipeline 也要走这一步——编辑者定的是标签/正文，不是包归属。
                    try:
                        from intel_attribution import ensure_pack_attribution

                        ensure_pack_attribution(self, article_id, article_data)
                    except Exception as exc:
                        print("⚠️ 归属兜底异常: %s" % str(exc)[:120])
                    # 手动发文（skip_pipeline）：不送 LLM 精炼、不跑自动分类——编辑者已定好标签/主题
                    if skip_pipeline:
                        return article_id
                    self._enqueue_intel_classification(article_id)
                    # 🔥 统一 LLM 精炼入队：所有文章入库后必须经 VPN 精炼（refined_content 300~600字）
                    # 才能作为正文展示；此处是唯一入库漏斗，保证"所有文章都经过 LLM 总结"。
                    # dedupe_key 只含 article_id，与 candidate_crawler_adapter 的入队去重互通。
                    try:
                        if config.REMOTE_PIPELINE_ENRICH or config.REMOTE_PIPELINE_TTS:
                            from intel_database import IntelRepository
                            IntelRepository(self).enqueue_job(
                                "enrich",
                                f"enrich:{int(article_id)}",
                                {
                                    "article_id": int(article_id),
                                    "url": str(url or ""),
                                    "title": str(title or ""),
                                    "content": str(content or ""),
                                    "keywords": [],
                                    "task_id": str(article_data.get('source_task_id') or ''),
                                },
                                priority=-10,
                            )
                    except Exception:
                        pass
                    # 🔥 阶段3：无正文的 HTML 链接条目 → 入队异步转换任务（低优先级，
                    # 失败自动重试 ≤2 次，绝不阻塞入库；转换结果只读展示不写回 content）
                    try:
                        if str(url or '').startswith(('http://', 'https://')) and len(str(content or '').strip()) < 80:
                            from intel_database import IntelRepository
                            IntelRepository(self).enqueue_job(
                                "dynamic_convert",
                                f"dynamic-convert:{url}",
                                {"url": str(url), "article_id": int(article_id)},
                                priority=-20,
                                max_attempts=3,
                                request_id=f"insert-article:{article_id}",
                            )
                    except Exception:
                        pass
                    return article_id
                finally:
                    cursor.close()
                
        except Exception as e:
            print(f"❌ 插入文章失败: {e}")
            return None
    
    def link_article_to_task(self, article_id: int, task_id: str) -> bool:
        """
        创建文章与任务的关联
        
        Args:
            article_id: 文章ID
            task_id: 任务ID
            
        Returns:
            bool: 是否成功
        """
        if not article_id or not task_id:
            return False
            
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    # 使用INSERT OR IGNORE避免重复插入
                    cursor.execute("""
                        INSERT OR IGNORE INTO article_tasks (article_id, task_id)
                        VALUES (?, ?)
                    """, (article_id, task_id))
                    source_task_name, _schedule_id = self._resolve_task_source(cursor, task_id)
                    cursor.execute(
                        """
                        UPDATE articles
                        SET source_task_id = ?,
                            source_task_name = ?,
                            updated_at = datetime('now', 'localtime')
                        WHERE id = ?
                        """,
                        (task_id, source_task_name, article_id)
                    )
                    self.connection.commit()
                    return True
                finally:
                    cursor.close()
        except Exception as e:
            print(f"⚠️ 创建文章-任务关联失败: {e}")
            return False
    
    def update_article(self, article_id: int, article_data: Dict) -> Optional[int]:
        """
        更新文章数据
        
        Args:
            article_id: 文章ID
            article_data: 文章数据字典
            
        Returns:
            Optional[int]: 成功返回文章ID，失败返回None
        """
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    # 生成内容哈希
                    content = article_data.get('content', '')
                    # 入库清洗：去掉源站导航/面包屑/页头页脚链接/分页噪音，让存储正文干净
                    content = clean_article_markdown(content)
                    # 正文开头的重复标题行（抓取器常把网页标题原样塞进正文）删除
                    content = _dedup_title_lines(content, article_data.get('title') or '')
                    article_data['content'] = content
                    content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()

                    # 🔥 阶段1：正文统一 Markdown —— 同步刷新展示用 Markdown 与原始快照
                    raw_content = str(article_data.get('raw_content') or '').strip() or content
                    # vpn_ocr：OCR 原文含导航噪声，展示 Markdown 优先用 content（摘要/精炼文）
                    _md_raw = (
                        '' if str(article_data.get('extraction_method') or '') == 'vpn_ocr'
                        else article_data.get('raw_content')
                    )
                    try:
                        from content_handlers import build_article_markdown
                        content_markdown = build_article_markdown(_md_raw, content)
                    except Exception as _md_exc:
                        content_markdown = content
                        print(f"⚠️ Markdown 转换失败，退回纯文本: {_md_exc}")
                    # 标题噪音清理：与 insert_article 保持一致，去掉 Markdown 里重复的标题行
                    content_markdown = _dedup_title_heading_lines(
                        content_markdown, article_data.get('title')
                    )
                    
                    
                    # 处理发布日期
                    publish_date = article_data.get('publish_date')
                    if publish_date and isinstance(publish_date, str):
                        try:
                            publish_date = datetime.strptime(publish_date, '%Y-%m-%d').date()
                        except:
                            publish_date = None

                    matched_keywords = article_data.get('matched_keywords')
                    if isinstance(matched_keywords, list):
                        matched_keywords = ','.join(matched_keywords)
                    matched_keywords_raw = article_data.get('matched_keywords_raw') or matched_keywords
                    try:
                        from keyword_governance import get_keyword_governance, parse_keyword_text
                        raw_keyword_list = parse_keyword_text(matched_keywords_raw)
                        canonical_keyword_list = get_keyword_governance().normalize_keyword_list(raw_keyword_list)
                        keyword_match_detail = article_data.get('keyword_match_detail') or json.dumps([
                            {'raw': raw, 'canonical': get_keyword_governance().normalize_keyword(raw)}
                            for raw in raw_keyword_list
                            if get_keyword_governance().normalize_keyword(raw)
                        ], ensure_ascii=False)
                        matched_keywords = ','.join(canonical_keyword_list)
                    except Exception:
                        keyword_match_detail = article_data.get('keyword_match_detail') or None

                    crawler_engine = article_data.get('crawler_engine_used') or article_data.get('crawler_engine')
                    crawler_engines = article_data.get('crawler_engines') or crawler_engine
                    if isinstance(crawler_engines, (list, tuple, set)):
                        crawler_engines = ','.join(str(item) for item in crawler_engines if str(item).strip())
                    
                    update_sql = """
                    UPDATE articles SET
                        title = ?,
                        content = ?,
                        category_id = ?,
                        source_url_id = ?,
                        publish_date = ?,
                        content_hash = ?,
                        content_length = ?,
                        content_markdown = ?,
                        raw_content = ?,
                        extraction_method = ?,
                        quality_score = ?,
                        source_task_id = CASE
                            WHEN ? IS NULL OR ? = '' THEN source_task_id
                            ELSE ?
                        END,
                        source_task_name = CASE
                            WHEN ? IS NULL OR ? = '' THEN source_task_name
                            ELSE ?
                        END,
                        matched_keywords = CASE
                            WHEN ? IS NULL OR ? = '' THEN matched_keywords
                            ELSE ?
                        END,
                        matched_keywords_raw = CASE
                            WHEN ? IS NULL OR ? = '' THEN matched_keywords_raw
                            ELSE ?
                        END,
                        keyword_match_detail = CASE
                            WHEN ? IS NULL OR ? = '' THEN keyword_match_detail
                            ELSE ?
                        END,
                        crawler_engine_used = CASE
                            WHEN ? IS NULL OR ? = '' THEN crawler_engine_used
                            ELSE ?
                        END,
                        crawler_engines = CASE
                            WHEN ? IS NULL OR ? = '' THEN crawler_engines
                            WHEN crawler_engines IS NULL OR crawler_engines = '' THEN ?
                            WHEN instr(',' || crawler_engines || ',', ',' || ? || ',') > 0 THEN crawler_engines
                            ELSE crawler_engines || ',' || ?
                        END,
                        crawler_attempts = COALESCE(crawler_attempts, 0) + ?,
                        fallback_trigger_reason = CASE
                            WHEN ? IS NULL OR ? = '' THEN fallback_trigger_reason
                            ELSE ?
                        END,
                        source_method = CASE
                            WHEN ? IS NULL OR ? = '' THEN source_method
                            ELSE ?
                        END,
                        configured_url = CASE
                            WHEN ? IS NULL OR ? = '' THEN configured_url
                            ELSE ?
                        END,
                        resolved_target_url = CASE
                            WHEN ? IS NULL OR ? = '' THEN resolved_target_url
                            ELSE ?
                        END,
                        canonical_url = CASE
                            WHEN ? IS NULL OR ? = '' THEN canonical_url
                            ELSE ?
                        END,
                        crawl_count = crawl_count + 1,
                        last_crawled = datetime('now'),
                        updated_at = datetime('now', 'localtime')
                    WHERE id = ?
                    """
                    
                    values = (
                        article_data.get('title', '无标题'),
                        content,
                        article_data.get('category_id'),
                        article_data.get('source_url_id'),
                        publish_date,
                        content_hash,
                        len(content),
                        content_markdown,
                        raw_content,
                        article_data.get('extraction_method', 'unknown'),
                        article_data.get('quality_score', 0),
                        article_data.get('source_task_id'),
                        article_data.get('source_task_id'),
                        article_data.get('source_task_id'),
                        article_data.get('source_task_name'),
                        article_data.get('source_task_name'),
                        article_data.get('source_task_name'),
                        matched_keywords,
                        matched_keywords,
                        matched_keywords,
                        matched_keywords_raw,
                        matched_keywords_raw,
                        matched_keywords_raw,
                        keyword_match_detail,
                        keyword_match_detail,
                        keyword_match_detail,
                        crawler_engine,
                        crawler_engine,
                        crawler_engine,
                        crawler_engines,
                        crawler_engines,
                        crawler_engines,
                        crawler_engines,
                        crawler_engines,
                        coerce_int(article_data.get('crawler_attempts'), 1),
                        article_data.get('fallback_trigger_reason'),
                        article_data.get('fallback_trigger_reason'),
                        article_data.get('fallback_trigger_reason'),
                        article_data.get('source_method'),
                        article_data.get('source_method'),
                        article_data.get('source_method'),
                        article_data.get('configured_url'),
                        article_data.get('configured_url'),
                        article_data.get('configured_url'),
                        article_data.get('resolved_target_url'),
                        article_data.get('resolved_target_url'),
                        article_data.get('resolved_target_url'),
                        article_data.get('canonical_url'),
                        article_data.get('canonical_url'),
                        article_data.get('canonical_url'),
                        article_id
                    )
                    
                    cursor.execute(update_sql, values)
                    self.connection.commit()
                    print(f"✅ 文章更新成功: ID {article_id}")
                    self.analyze_article_spacetime_profile(article_id)
                    # 归属必填：去重命中走的是 update 分支（不经过 insert 的归属收口），
                    # 存量无归属的老文章重新抓到时会在这里补上兜底归属。
                    try:
                        from intel_attribution import ensure_pack_attribution

                        ensure_pack_attribution(self, article_id, article_data)
                    except Exception as exc:
                        print("⚠️ 归属兜底异常: %s" % str(exc)[:120])
                    self._enqueue_intel_classification(article_id)
                    return article_id
                finally:
                    cursor.close()
                
        except Exception as e:
            print(f"❌ 更新文章失败: {e}")
            return None

    def _enqueue_intel_classification(self, article_id: int) -> None:
        """Best-effort post-commit classification enqueue; never fail article persistence."""
        try:
            import config as app_config
            if not app_config.INTEL_CLASSIFICATION_ENABLED:
                return
            from intel_database import IntelRepository
            IntelRepository(self).enqueue_classification(
                article_id,
                app_config.INTEL_DEFAULT_INDUSTRY_PACK,
            )
        except Exception as exc:
            print(f"⚠️ 市场资讯分类待办写入失败，文章已保留: {exc}")
    
    def get_article_id_by_url(self, url: str) -> Optional[int]:
        """
        根据URL获取文章ID
        
        Args:
            url: 文章URL
            
        Returns:
            Optional[int]: 文章ID，不存在返回None
        """
        try:
            # 🔧 URL标准化：已禁用
            # 原因：我们使用Playwright的page.url，这已经是正确的URL了
            # 不需要再次转换
            # try:
            #     from url_transformation_rules import transform_url
            #     normalized_url = transform_url(url, verbose=False, verify=False)
            #     if normalized_url != url:
            #         url = normalized_url
            # except:
            #     pass
            
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    select_sql = (
                        "SELECT id FROM articles WHERE status = 'active' AND "
                        "LOWER(REPLACE(REPLACE(url, 'https://www.', 'https://'), 'http://www.', 'http://')) = ? "
                        "LIMIT 1"
                    )
                    cursor.execute(select_sql, (_normalize_article_url(url),))
                    result = cursor.fetchone()
                    return result['id'] if result else None
                finally:
                    cursor.close()
        except Exception as e:
            print(f"❌ 查询文章ID失败: {e}")
            return None
    
    def check_article_exists(self, url: str) -> bool:
        """
        检查文章是否已存在
        
        Args:
            url: 文章URL
            
        Returns:
            bool: 文章是否存在
        """
        try:
            article_id = self.get_article_id_by_url(url)
            return article_id is not None
        except Exception as e:
            print(f"❌ 检查文章是否存在失败: {e}")
            return False
    
    def is_article_exists(self, url: str) -> bool:
        """
        检查文章是否存在
        
        Args:
            url: 文章URL
            
        Returns:
            bool: 文章是否存在
        """
        return self.get_article_id_by_url(url) is not None

    def _lookup_keywords_for_article(self, cursor, article: Dict) -> str:
        """Best-effort keyword lookup for old articles without matched_keywords."""
        try:
            source_url_id = article.get('source_url_id')
            if source_url_id:
                cursor.execute("SELECT keywords FROM managed_urls WHERE id = ?", (source_url_id,))
                row = cursor.fetchone()
                if row and row['keywords']:
                    return row['keywords']

                cursor.execute("SELECT keywords FROM scheduled_tasks WHERE url_id = ? ORDER BY id DESC LIMIT 1", (source_url_id,))
                row = cursor.fetchone()
                if row and row['keywords']:
                    return row['keywords']

            article_id = article.get('id')
            if article_id:
                cursor.execute(
                    """
                    SELECT ct.keywords
                    FROM article_tasks at
                    JOIN crawl_tasks ct ON at.task_id = ct.task_id
                    WHERE at.article_id = ? AND ct.keywords IS NOT NULL AND TRIM(ct.keywords) != ''
                    ORDER BY ct.id DESC
                    LIMIT 1
                    """,
                    (article_id,)
                )
                row = cursor.fetchone()
                if row and row['keywords']:
                    return row['keywords']

            domain = article.get('domain')
            if domain:
                cursor.execute(
                    """
                    SELECT keywords
                    FROM managed_urls
                    WHERE domain = ? AND keywords IS NOT NULL AND TRIM(keywords) != ''
                    ORDER BY parent_url_id IS NOT NULL, id DESC
                    LIMIT 1
                    """,
                    (domain,)
                )
                row = cursor.fetchone()
                if row and row['keywords']:
                    return row['keywords']
        except Exception as e:
            print(f"⚠️ 推断文章关键词配置失败: {e}")

        return ''

    def _hydrate_matched_keywords_for_display(self, cursor, article: Dict) -> None:
        """Populate matched_keywords for display when old rows did not store it."""
        if not article or article.get('matched_keywords'):
            return

        keywords = self._lookup_keywords_for_article(cursor, article)
        if not keywords:
            return

        try:
            from keyword_filter import KeywordFilter
            keyword_filter = KeywordFilter(keywords)
            if not keyword_filter.is_enabled():
                return

            match_result = keyword_filter.get_matched_keywords_by_location(
                article.get('title', ''),
                article.get('content', '')
            )
            article['matched_keywords'] = match_result.get('matched_keywords_str', '')
        except Exception as e:
            print(f"⚠️ 计算文章匹配关键词失败: {e}")

    def _article_dedupe_key(self, article: Dict) -> str:
        """Return a stable key for duplicate article rows."""
        if not article:
            return ''

        for field in ('canonical_url', 'resolved_target_url', 'url'):
            value = str(article.get(field) or '').strip().lower()
            if value:
                value = value.split('#', 1)[0].rstrip('/')
                if value:
                    return f'url:{value}'

        title = re.sub(r'\s+', ' ', str(article.get('title') or '').strip().lower())
        domain = str(article.get('domain') or '').strip().lower()
        if title:
            return f'title:{domain}:{title}'
        return f'id:{article.get("id") or ""}'

    def _is_better_article_representative(self, candidate: Dict, current: Dict) -> bool:
        """Prefer the richest and most recent row when multiple rows represent one article."""
        if not current:
            return True

        candidate_score = candidate.get('quality_score') or 0
        current_score = current.get('quality_score') or 0
        if candidate_score != current_score:
            return candidate_score > current_score

        candidate_time = str(candidate.get('last_crawled') or candidate.get('created_at') or '')
        current_time = str(current.get('last_crawled') or current.get('created_at') or '')
        if candidate_time != current_time:
            return candidate_time > current_time

        return (candidate.get('id') or 0) > (current.get('id') or 0)
    
    def get_articles(self, page: int = 1, per_page: int = 20,
                    domain: str = None, category_id: int = None, source_url_id: int = None, search: str = None,
                    keyword: str = None, industry_pack_id: str = None,
                    activation_id: str = None) -> Tuple[List[Dict], int]:
        """
        获取文章列表
        
        Args:
            page: 页码
            per_page: 每页数量
            domain: 域名过滤
            category_id: 分类过滤
            source_url_id: 来源URL过滤
            search: 搜索关键词
            keyword: 匹配关键词过滤
            
        Returns:
            Tuple[List[Dict], int]: (文章列表, 总数)
        """
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    # 构建查询条件
                    where_conditions = ["a.status = 'active'"]
                    params = []
                    classification_join = ""
                    classification_columns = "NULL AS industry_matched_keywords_json,"
                    if industry_pack_id:
                        classification_join = (
                            "JOIN article_intel_classifications ic "
                            "ON ic.article_id=a.id AND ic.industry_pack_id=?"
                        )
                        params.append(str(industry_pack_id))
                        classification_columns = (
                            "ic.matched_keywords_json AS industry_matched_keywords_json,"
                        )
                        where_conditions.extend([
                            "COALESCE(json_array_length(json_extract(ic.score_details_json, '$.hits.anchor')), 0) > 0",
                            "NOT EXISTS ("
                            "SELECT 1 FROM intel_evidence_group_articles ega "
                            "JOIN intel_evidence_groups eg ON eg.id=ega.evidence_group_id "
                            "WHERE ega.article_id=a.id "
                            "AND eg.industry_pack_id=ic.industry_pack_id "
                            "AND eg.representative_article_id!=a.id)",
                        ])
                        if activation_id:
                            where_conditions.append("ic.activation_id = ?")
                            params.append(str(activation_id))
                    
                    if domain:
                        where_conditions.append("a.domain = ?")
                        params.append(domain)
                    
                    if category_id:
                        where_conditions.append("a.category_id = ?")
                        params.append(category_id)
                    
                    if source_url_id:
                        where_conditions.append("a.source_url_id = ?")
                        params.append(source_url_id)
                    
                    if search:
                        where_conditions.append("(a.title LIKE ? OR a.content LIKE ?)")
                        search_param = f"%{search}%"
                        params.extend([search_param, search_param])

                    if keyword:
                        keyword_values = keyword if isinstance(keyword, (list, tuple, set)) else [keyword]
                        keyword_values = [
                            str(item or '').strip()
                            for item in keyword_values
                            if str(item or '').strip() and not re.fullmatch(r'\d+', str(item or '').strip())
                        ]
                        if not keyword_values:
                            return [], 0
                        keyword_conditions = []
                        for item in keyword_values:
                            keyword_conditions.append(
                                "ic.matched_keywords_json LIKE ?"
                                if industry_pack_id else "a.matched_keywords LIKE ?"
                            )
                            params.append(f"%{item}%")
                        where_conditions.append(f"({' OR '.join(keyword_conditions)})")
                    
                    where_clause = " AND ".join(where_conditions)
                    
                    # 获取文章列表（关联分类和来源URL信息）
                    select_sql = f"""
                    SELECT
                        a.*,
                        {classification_columns}
                        c.name as category_name,
                        COALESCE(mu.name,
                            (SELECT mu2.name FROM managed_urls mu2
                             WHERE REPLACE(REPLACE(a.url,'https://',''),'http://','')
                                   LIKE REPLACE(REPLACE(mu2.url,'https://',''),'http://','') || '%'
                             ORDER BY LENGTH(mu2.url) DESC LIMIT 1)
                        ) as source_url_name,
                        (
                            SELECT at.task_id
                            FROM article_tasks at
                            WHERE at.article_id = a.id
                            ORDER BY at.created_at DESC, at.id DESC
                            LIMIT 1
                        ) AS latest_task_id,
                        (
                            SELECT ct.task_name
                            FROM article_tasks at
                            LEFT JOIN crawl_tasks ct ON at.task_id = ct.task_id
                            WHERE at.article_id = a.id
                            ORDER BY at.created_at DESC, at.id DESC
                            LIMIT 1
                        ) AS latest_task_name,
                        (
                            SELECT ct.target_url
                            FROM article_tasks at
                            LEFT JOIN crawl_tasks ct ON at.task_id = ct.task_id
                            WHERE at.article_id = a.id
                            ORDER BY at.created_at DESC, at.id DESC
                            LIMIT 1
                        ) AS latest_task_target_url
                    FROM articles a
                    {classification_join}
                    LEFT JOIN categories c ON a.category_id = c.id
                    LEFT JOIN managed_urls mu ON a.source_url_id = mu.id
                    WHERE {where_clause}
                    ORDER BY COALESCE(a.publish_date, a.first_crawled, a.created_at) DESC, a.quality_score DESC, a.id DESC
                    """
                    cursor.execute(select_sql, params)
                    raw_articles = [dict(row) for row in cursor.fetchall()]
                    
                    # 转换日期格式
                    articles_by_key = {}
                    for article in raw_articles:
                        if article['publish_date']:
                            article['publish_date'] = str(article['publish_date'])
                        if article['first_crawled']:
                            article['first_crawled'] = article['first_crawled']
                        if article['last_crawled']:
                            article['last_crawled'] = article['last_crawled']
                        self._hydrate_matched_keywords_for_display(cursor, article)
                        if industry_pack_id:
                            try:
                                industry_keywords = json.loads(
                                    article.get('industry_matched_keywords_json') or '[]'
                                )
                            except (TypeError, ValueError, json.JSONDecodeError):
                                industry_keywords = []
                            article['classification_keywords'] = industry_keywords
                            article['matched_keywords'] = industry_keywords
                        task_id = article.get('source_task_id') or article.get('latest_task_id') or ''
                        schedule_match = re.match(r'^schedule_(\d+)_', task_id)
                        article['latest_schedule_id'] = schedule_match.group(1) if schedule_match else None
                        scheduled_task_name = ''
                        if article['latest_schedule_id']:
                            cursor.execute(
                                "SELECT task_name FROM scheduled_tasks WHERE id = ?",
                                (article['latest_schedule_id'],)
                            )
                            schedule_row = cursor.fetchone()
                            scheduled_task_name = schedule_row['task_name'] if schedule_row and schedule_row['task_name'] and not _is_placeholder_source_task(schedule_row['task_name']) else ''
                        article['latest_task_id'] = task_id
                        source_task_name = article.get('source_task_name')
                        if _is_placeholder_source_task(source_task_name):
                            source_task_name = ''
                        latest_task_name = article.get('latest_task_name')
                        if _is_placeholder_source_task(latest_task_name):
                            latest_task_name = ''
                        article['latest_task_display_name'] = (
                            source_task_name
                            or scheduled_task_name
                            or latest_task_name
                            or task_id
                            or ''
                        )

                        dedupe_key = self._article_dedupe_key(article)
                        current = articles_by_key.get(dedupe_key)
                        if self._is_better_article_representative(article, current):
                            articles_by_key[dedupe_key] = article
                    
                    deduped_articles = sorted(
                        articles_by_key.values(),
                        key=lambda item: (
                            str(item.get('last_crawled') or ''),
                            item.get('quality_score') or 0,
                            item.get('id') or 0,
                        ),
                        reverse=True
                    )
                    total = len(deduped_articles)
                    offset = (page - 1) * per_page
                    return deduped_articles[offset:offset + per_page], total
                finally:
                    cursor.close()
                
        except Exception as e:
            print(f"❌ 获取文章列表失败: {e}")
            return [], 0

    def get_keyword_map(self, limit: int = 500, *, industry_pack_id: str = None,
                        activation_id: str = None) -> List[Dict]:
        """Build a keyword information map from active articles."""
        try:
            self._ensure_connection()
            limit = coerce_int(limit, 500, 1, 5000)
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    from keyword_governance import KeywordGovernance, keyword_key

                    governance = KeywordGovernance(self.connection)
                    hidden_keywords = set()
                    try:
                        cursor.execute("SELECT keyword FROM keyword_node_states WHERE state = 'hidden'")
                        hidden_keywords = {keyword_key(row['keyword']) for row in cursor.fetchall() if row['keyword']}
                    except Exception:
                        hidden_keywords = set()

                    cursor.execute("PRAGMA table_info(articles)")
                    article_columns = {row['name'] for row in cursor.fetchall()}
                    source_task_expr = 'a.source_task_id' if 'source_task_id' in article_columns else "''"
                    raw_keyword_expr = 'a.matched_keywords_raw' if 'matched_keywords_raw' in article_columns else "''"
                    classification_join = ""
                    classification_where = ""
                    classification_params = []
                    keyword_expr = "a.matched_keywords"
                    raw_keyword_select = raw_keyword_expr
                    if industry_pack_id:
                        classification_join = (
                            "JOIN article_intel_classifications ic "
                            "ON ic.article_id=a.id AND ic.industry_pack_id=?"
                        )
                        classification_params.append(str(industry_pack_id))
                        classification_where = """
                          AND COALESCE(json_array_length(json_extract(ic.score_details_json, '$.hits.anchor')), 0) > 0
                          AND NOT EXISTS (
                            SELECT 1 FROM intel_evidence_group_articles ega
                            JOIN intel_evidence_groups eg ON eg.id=ega.evidence_group_id
                            WHERE ega.article_id=a.id
                              AND eg.industry_pack_id=ic.industry_pack_id
                              AND eg.representative_article_id!=a.id
                          )
                        """
                        if activation_id:
                            classification_where += " AND ic.activation_id = ?"
                            classification_params.append(str(activation_id))
                        keyword_expr = "ic.matched_keywords_json"
                        raw_keyword_select = "ic.matched_keywords_json"
                    cursor.execute("""
                        SELECT
                            a.id,
                            a.url,
                            a.canonical_url,
                            a.resolved_target_url,
                            a.domain,
                            a.title,
                            a.content,
                            {keyword_expr} AS matched_keywords,
                            {raw_keyword_select} AS matched_keywords_raw,
                            a.quality_score,
                            a.last_crawled,
                            a.created_at,
                            {source_task_expr} AS source_task_id,
                            (
                                SELECT at.task_id
                                FROM article_tasks at
                                WHERE at.article_id = a.id
                                ORDER BY at.created_at DESC, at.id DESC
                                LIMIT 1
                            ) AS latest_task_id
                        FROM articles a
                        {classification_join}
                        WHERE a.status = 'active'
                        {classification_where}
                    """.format(
                        source_task_expr=source_task_expr,
                        keyword_expr=keyword_expr,
                        raw_keyword_select=raw_keyword_select,
                        classification_join=classification_join,
                        classification_where=classification_where,
                    ), classification_params)
                    keyword_stats = {}
                    for row in cursor.fetchall():
                        article = dict(row)
                        self._hydrate_matched_keywords_for_display(cursor, article)
                        raw_keywords = _parse_matched_keyword_text(article.get('matched_keywords_raw')) or _parse_matched_keyword_text(article.get('matched_keywords'))
                        task_id = article.get('source_task_id') or article.get('latest_task_id') or ''
                        for raw_keyword in raw_keywords:
                            keyword = governance.normalize_keyword(raw_keyword)
                            if not keyword or keyword_key(keyword) in hidden_keywords:
                                continue
                            item = keyword_stats.setdefault(keyword, {
                                'keyword': keyword,
                                'source_keywords': set(),
                                'article_keys': set(),
                                'task_ids': set(),
                                'latest_crawled': ''
                            })
                            item['source_keywords'].add(raw_keyword)
                            item['source_keywords'].add(keyword)
                            item['article_keys'].add(self._article_dedupe_key(article))
                            if task_id:
                                item['task_ids'].add(task_id)
                            last_crawled = str(article.get('last_crawled') or '')
                            if last_crawled and last_crawled > item['latest_crawled']:
                                item['latest_crawled'] = last_crawled

                    result = []
                    for item in keyword_stats.values():
                        source_keywords = sorted(item['source_keywords'], key=lambda value: value.lower())
                        result.append({
                            'keyword': item['keyword'],
                            'canonical_keyword': item['keyword'],
                            'source_keywords': source_keywords,
                            'filter_keywords': source_keywords,
                            'merged_keyword_count': len(source_keywords),
                            'article_count': len(item['article_keys']),
                            'task_count': len(item['task_ids']),
                            'latest_crawled': item['latest_crawled']
                        })

                    result.sort(key=lambda item: (-item['article_count'], item['keyword'].lower()))
                    return result[:limit]
                finally:
                    cursor.close()
        except Exception as e:
            print(f"❌ 获取关键词信息图谱失败: {e}")
            return []

    def get_articles_by_task_id(self, task_id: str, page: int = 1, per_page: int = 100) -> Tuple[List[Dict], int]:
        """获取指定聚合任务关联的文章列表。"""
        if not task_id:
            return [], 0

        try:
            self._ensure_connection()
            page = coerce_int(page, 1, 1)
            per_page = coerce_int(per_page, 100, 1, 500)
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    direct_count_sql = """
                        SELECT COUNT(*) AS total
                        FROM articles a
                        INNER JOIN article_tasks at ON a.id = at.article_id
                        WHERE at.task_id = ? AND a.status = 'active'
                    """
                    direct_select_sql = """
                        SELECT
                            a.*,
                            at.created_at AS task_linked_at,
                            ct.keywords AS task_keywords,
                            ct.task_name,
                            ct.target_url AS task_target_url
                        FROM articles a
                        INNER JOIN article_tasks at ON a.id = at.article_id
                        LEFT JOIN crawl_tasks ct ON at.task_id = ct.task_id
                        WHERE at.task_id = ? AND a.status = 'active'
                        ORDER BY at.created_at DESC, a.created_at DESC, a.id DESC
                        LIMIT ? OFFSET ?
                        """

                    cursor.execute(direct_count_sql, (task_id,))
                    total = cursor.fetchone()['total']
                    if total == 0:
                        repaired = self._repair_article_task_links_from_audit(cursor, task_id)
                        if not repaired:
                            repaired = self._repair_article_task_links_for_task(cursor, task_id)
                        if repaired:
                            self.connection.commit()
                            cursor.execute(direct_count_sql, (task_id,))
                            total = cursor.fetchone()['total']

                    offset = (page - 1) * per_page
                    cursor.execute(direct_select_sql, (task_id, per_page, offset))
                    articles = [dict(row) for row in cursor.fetchall()]

                    for article in articles:
                        if article.get('publish_date'):
                            article['publish_date'] = str(article['publish_date'])
                        self._hydrate_matched_keywords_for_display(cursor, article)

                    return articles, total
                finally:
                    cursor.close()
        except Exception as e:
            print(f"❌ 获取任务文章列表失败: {e}")
            return [], 0

    def _repair_article_task_links_from_audit(self, cursor, task_id: str) -> int:
        """Repair exact article-task links from this task's audit JSON."""
        if not task_id:
            return 0

        results_dir = os.getenv('CRAWL_RESULTS_DIR') or os.path.join(os.getcwd(), 'crawl_results')
        audit_path = os.path.join(results_dir, f'{task_id}_audit.json')
        if not os.path.exists(audit_path):
            return 0

        try:
            with open(audit_path, 'r', encoding='utf-8') as audit_file:
                audit = json.load(audit_file)
        except Exception as exc:
            print(f"⚠️ 读取任务审计文件失败: {audit_path} ({exc})")
            return 0

        items = audit.get('items') if isinstance(audit, dict) else None
        if not isinstance(items, list):
            return 0

        task = self._get_task_context_for_link_repair(cursor, task_id)
        task_name, _schedule_id = self._resolve_task_source(cursor, task_id)
        linked = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get('status') not in {'saved', 'duplicate'}:
                continue

            article_id = coerce_int(item.get('db_id'), None)
            if not article_id:
                url = item.get('final_url') or item.get('url')
                if not url:
                    continue
                cursor.execute(
                    """
                    SELECT id
                    FROM articles
                    WHERE status = 'active' AND url = ?
                    LIMIT 1
                    """,
                    (url,)
                )
                row = cursor.fetchone()
                article_id = row['id'] if row else None

            if not article_id:
                continue

            cursor.execute(
                "INSERT OR IGNORE INTO article_tasks (article_id, task_id) VALUES (?, ?)",
                (article_id, task_id)
            )
            cursor.execute(
                """
                UPDATE articles
                SET source_task_id = ?,
                    source_task_name = ?,
                    updated_at = datetime('now', 'localtime')
                WHERE id = ?
                """,
                (task_id, task_name or (task or {}).get('task_name') or task_id, article_id)
            )
            linked += 1

        return linked

    def _split_task_keywords(self, value) -> List[str]:
        keywords = []
        seen = set()
        for item in re.split(r'[,，、;\n\r]+', str(value or '')):
            keyword = item.strip()
            keyword = re.sub(r'^\[[^\]]+\]', '', keyword).strip()
            keyword = re.sub(r'^(标题|標題|正文|内容|內容|文)\s*[:：]', '', keyword).strip()
            key = keyword.lower()
            if keyword and key not in seen:
                keywords.append(keyword)
                seen.add(key)
        return keywords

    def _get_task_context_for_link_repair(self, cursor, task_id: str) -> Optional[Dict]:
        cursor.execute(
            "SELECT task_id, task_name, target_url, keywords, status FROM crawl_tasks WHERE task_id = ?",
            (task_id,)
        )
        row = cursor.fetchone()
        if row:
            task = dict(row)
        else:
            task = {'task_id': task_id, 'task_name': '', 'target_url': '', 'keywords': '', 'status': 'completed'}

        schedule_match = re.match(r'^schedule_(\d+)_', str(task_id or ''))
        if schedule_match:
            cursor.execute(
                "SELECT id, task_name, target_url, keywords FROM scheduled_tasks WHERE id = ?",
                (schedule_match.group(1),)
            )
            schedule_row = cursor.fetchone()
            if schedule_row:
                schedule = dict(schedule_row)
                task['task_name'] = schedule.get('task_name') or task.get('task_name') or ''
                task['target_url'] = schedule.get('target_url') or task.get('target_url') or ''
                task['keywords'] = schedule.get('keywords') or task.get('keywords') or ''

        if not task.get('target_url') or not task.get('keywords'):
            return None

        if not self._crawl_task_exists(cursor, task_id):
            china_time = get_china_time().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute(
                """
                INSERT INTO crawl_tasks (
                    task_id, target_url, task_name, crawl_depth, crawl_mode,
                    page_limit, incremental_mode, keywords, status, progress,
                    articles_found, articles_processed, created_at, updated_at
                ) VALUES (?, ?, ?, 1, 'article_crawl', 100, 0, ?, ?, 0, 0, 0, ?, ?)
                """,
                (
                    task_id,
                    normalize_task_url(task.get('target_url', '')),
                    task.get('task_name') or task_id,
                    task.get('keywords') or '',
                    task.get('status') or 'completed',
                    china_time,
                    china_time,
                )
            )

        return task

    def _crawl_task_exists(self, cursor, task_id: str) -> bool:
        cursor.execute("SELECT 1 FROM crawl_tasks WHERE task_id = ? LIMIT 1", (task_id,))
        return cursor.fetchone() is not None

    def _repair_article_task_links_for_task(self, cursor, task_id: str) -> int:
        """Create missing exact article-task links using this task's own URL and keywords."""
        task = self._get_task_context_for_link_repair(cursor, task_id)
        if not task:
            return 0

        target_url = task.get('target_url') or ''
        domain = urlparse(target_url).netloc.lower()
        domain_without_www = domain[4:] if domain.startswith('www.') else domain
        keywords = self._split_task_keywords(task.get('keywords'))
        if not domain_without_www or not keywords:
            return 0

        keyword_conditions = []
        params = []
        for keyword in keywords:
            like = f"%{keyword}%"
            keyword_conditions.append("(a.title LIKE ? OR a.content LIKE ? OR a.matched_keywords LIKE ?)")
            params.extend([like, like, like])

        domain_like = f"%{domain_without_www}%"
        sql = f"""
            SELECT a.id
            FROM articles a
            WHERE a.status = 'active'
              AND (LOWER(a.domain) = ? OR LOWER(a.domain) = ? OR LOWER(a.domain) LIKE ?)
              AND ({' OR '.join(keyword_conditions)})
              AND NOT EXISTS (
                  SELECT 1
                  FROM article_tasks at
                  WHERE at.article_id = a.id AND at.task_id = ?
              )
            ORDER BY COALESCE(a.publish_date, a.first_crawled, a.created_at) DESC, a.id DESC
            LIMIT 500
        """
        cursor.execute(sql, [domain, domain_without_www, domain_like, *params, task_id])
        article_ids = [row['id'] for row in cursor.fetchall()]
        if not article_ids:
            return 0

        task_name, _schedule_id = self._resolve_task_source(cursor, task_id)
        for article_id in article_ids:
            cursor.execute(
                "INSERT OR IGNORE INTO article_tasks (article_id, task_id) VALUES (?, ?)",
                (article_id, task_id)
            )
            cursor.execute(
                """
                UPDATE articles
                SET source_task_id = ?,
                    source_task_name = ?,
                    updated_at = datetime('now', 'localtime')
                WHERE id = ?
                """,
                (task_id, task_name, article_id)
            )
        return len(article_ids)
    
    def get_article_by_id(self, article_id: int) -> Optional[Dict]:
        """
        根据ID获取文章详情
        
        Args:
            article_id: 文章ID
            
        Returns:
            Optional[Dict]: 文章详情，不存在返回None
        """
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    select_sql = "SELECT * FROM articles WHERE id = ? AND status = 'active'"
                    cursor.execute(select_sql, (article_id,))
                    article = cursor.fetchone()
                    
                    if article:
                        article = dict(article)
                        # Detail views need the same source identity as list/dashboard
                        # cards. Prefer a configured source name, then a readable domain.
                        cursor.execute(
                            """SELECT name FROM managed_urls
                               WHERE REPLACE(REPLACE(?,'https://',''),'http://','')
                                     LIKE REPLACE(REPLACE(url,'https://',''),'http://','') || '%'
                               ORDER BY LENGTH(url) DESC LIMIT 1""",
                            (article.get('url') or '',),
                        )
                        source = cursor.fetchone()
                        article['source_url_name'] = (source['name'] if source and source['name'] else article.get('domain') or '未提供')
                        article['site_name'] = article['source_url_name']
                        article['publish_date_display'] = article['publish_date'] or '未提供'
                        article['metadata_status'] = 'complete' if article.get('publish_date') else 'incomplete'
                        if article['publish_date']:
                            article['publish_date'] = str(article['publish_date'])
                        if article['first_crawled']:
                            article['first_crawled'] = article['first_crawled']
                        if article['last_crawled']:
                            article['last_crawled'] = article['last_crawled']
                        self._hydrate_matched_keywords_for_display(cursor, article)
                    
                    return article
                finally:
                    cursor.close()
        except Exception as e:
            print(f"❌ 获取文章详情失败: {e}")
            return None
    
    def get_article_by_url(self, url: str) -> Optional[Dict]:
        """
        根据URL获取文章详情
        
        Args:
            url: 文章URL
            
        Returns:
            Optional[Dict]: 文章详情，不存在返回None
        """
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    select_sql = "SELECT * FROM articles WHERE url = ? AND status = 'active'"
                    cursor.execute(select_sql, (url,))
                    article = cursor.fetchone()
                    
                    if article:
                        article = dict(article)
                        if article['publish_date']:
                            article['publish_date'] = str(article['publish_date'])
                        if article['first_crawled']:
                            article['first_crawled'] = article['first_crawled']
                        if article['last_crawled']:
                            article['last_crawled'] = article['last_crawled']
                        self._hydrate_matched_keywords_for_display(cursor, article)
                    
                    return article
                finally:
                    cursor.close()
        except Exception as e:
            print(f"❌ 获取文章详情失败: {e}")
            return None
    
    def delete_article(self, article_id: int) -> bool:
        """
        删除文章（软删除）
        
        Args:
            article_id: 文章ID
            
        Returns:
            bool: 删除是否成功
        """
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    update_sql = "UPDATE articles SET status = 'deleted', updated_at = datetime('now', 'localtime') WHERE id = ?"
                    cursor.execute(update_sql, (article_id,))
                    self.connection.commit()
                    print(f"✅ 文章删除成功: ID {article_id}")
                    return True
                finally:
                    cursor.close()
        except Exception as e:
            print(f"❌ 删除文章失败: {e}")
            return False
    
    def delete_article_by_url(self, url: str) -> bool:
        """
        根据URL删除文章（软删除）
        
        Args:
            url: 文章URL
            
        Returns:
            bool: 删除是否成功
        """
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    update_sql = "UPDATE articles SET status = 'deleted', updated_at = datetime('now', 'localtime') WHERE url = ?"
                    cursor.execute(update_sql, (url,))
                    self.connection.commit()
                    print(f"✅ 文章删除成功: {url}")
                    return True
                finally:
                    cursor.close()
        except Exception as e:
            print(f"❌ 删除文章失败: {e}")
            return False
    
    def clear_local_articles(self) -> Dict:
        """Hard-delete all locally stored articles for test resets."""
        result = {
            'success': False,
            'active_articles': 0,
            'total_articles': 0,
            'article_tasks': 0,
            'deleted_articles': 0,
            'deleted_article_tasks': 0
        }

        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    cursor.execute("SELECT COUNT(*) AS total FROM articles WHERE status = 'active'")
                    result['active_articles'] = cursor.fetchone()['total']

                    cursor.execute("SELECT COUNT(*) AS total FROM articles")
                    result['total_articles'] = cursor.fetchone()['total']

                    cursor.execute("SELECT COUNT(*) AS total FROM article_tasks")
                    result['article_tasks'] = cursor.fetchone()['total']

                    cursor.execute("DELETE FROM article_tasks")
                    result['deleted_article_tasks'] = cursor.rowcount if cursor.rowcount is not None else result['article_tasks']

                    cursor.execute("DELETE FROM articles")
                    result['deleted_articles'] = cursor.rowcount if cursor.rowcount is not None else result['total_articles']

                    try:
                        cursor.execute("DELETE FROM sqlite_sequence WHERE name IN ('articles', 'article_tasks')")
                    except Exception:
                        pass

                    self.connection.commit()
                    result['success'] = True
                    print(
                        "Cleared local articles: "
                        f"{result['deleted_articles']} articles, "
                        f"{result['deleted_article_tasks']} article-task links"
                    )
                    return result
                finally:
                    cursor.close()
        except Exception as e:
            print(f"Failed to clear local articles: {e}")
            return result

    def get_recent_articles(self, limit: int = 50, *, industry_pack_id: str = None,
                            activation_id: str = None) -> List[Dict]:
        """Return recently crawled active articles for legacy pages."""
        try:
            self._ensure_connection()
            limit = coerce_int(limit, 50, 1, 500)
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    join_sql = ""
                    conditions = ["a.status = 'active'"]
                    params = []
                    keyword_column = "a.matched_keywords"
                    if industry_pack_id:
                        join_sql = (
                            "JOIN article_intel_classifications ic "
                            "ON ic.article_id=a.id AND ic.industry_pack_id=?"
                        )
                        params.append(str(industry_pack_id))
                        conditions.extend([
                            "COALESCE(json_array_length(json_extract(ic.score_details_json, '$.hits.anchor')), 0) > 0",
                            "NOT EXISTS ("
                            "SELECT 1 FROM intel_evidence_group_articles ega "
                            "JOIN intel_evidence_groups eg ON eg.id=ega.evidence_group_id "
                            "WHERE ega.article_id=a.id "
                            "AND eg.industry_pack_id=ic.industry_pack_id "
                            "AND eg.representative_article_id!=a.id)",
                        ])
                        if activation_id:
                            conditions.append("ic.activation_id=?")
                            params.append(str(activation_id))
                        keyword_column = "ic.matched_keywords_json"
                    params.append(limit)
                    cursor.execute(
                        f"""
                        SELECT
                            a.id, a.url, a.title, a.content, a.domain, a.publish_date,
                            a.content_length, a.extraction_method, a.quality_score,
                            a.first_crawled, a.last_crawled, a.created_at, a.updated_at,
                            {keyword_column} AS matched_keywords
                        FROM articles a
                        {join_sql}
                        WHERE {' AND '.join(conditions)}
                        ORDER BY COALESCE(a.publish_date, a.first_crawled, a.created_at) DESC, a.id DESC
                        LIMIT ?
                        """,
                        params,
                    )
                    articles = [dict(row) for row in cursor.fetchall()]
                    for article in articles:
                        if article.get('publish_date'):
                            article['publish_date'] = str(article['publish_date'])
                        extracted_at = (
                            article.get('last_crawled')
                            or article.get('created_at')
                            or article.get('first_crawled')
                        )
                        article['extracted_at'] = extracted_at
                        if not article.get('content_length') and article.get('content'):
                            article['content_length'] = len(article['content'])
                    return articles
                finally:
                    cursor.close()
        except Exception as e:
            print(f"Failed to get recent articles: {e}")
            return []

    def get_statistics(self, domain: str = None, *, industry_pack_id: str = None,
                       activation_id: str = None) -> Dict:
        """
        获取统计信息
        
        Args:
            domain: 域名过滤
            
        Returns:
            Dict: 统计信息
        """
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    alias = "a." if industry_pack_id else ""
                    join_sql = ""
                    where_conditions = [f"{alias}status = 'active'"]
                    params = []
                    if industry_pack_id:
                        join_sql = (
                            "JOIN article_intel_classifications ic "
                            "ON ic.article_id=a.id AND ic.industry_pack_id=?"
                        )
                        params.append(str(industry_pack_id))
                        where_conditions.extend([
                            "COALESCE(json_array_length(json_extract(ic.score_details_json, '$.hits.anchor')), 0) > 0",
                            "NOT EXISTS ("
                            "SELECT 1 FROM intel_evidence_group_articles ega "
                            "JOIN intel_evidence_groups eg ON eg.id=ega.evidence_group_id "
                            "WHERE ega.article_id=a.id "
                            "AND eg.industry_pack_id=ic.industry_pack_id "
                            "AND eg.representative_article_id!=a.id)",
                        ])
                        if activation_id:
                            where_conditions.append("ic.activation_id=?")
                            params.append(str(activation_id))
                    
                    if domain:
                        where_conditions.append(f"{alias}domain = ?")
                        params.append(domain)
                    where_clause = "WHERE " + " AND ".join(where_conditions)
                    from_sql = f"FROM articles {'a' if industry_pack_id else ''} {join_sql}"
                    
                    # 域名数以 URL 主机名为准：补全旧数据的空 domain，且将
                    # www.example.com 与 example.com 归为同一个发布主体。
                    cursor.execute(
                        f"""
                        SELECT {alias}id AS id, {alias}domain AS domain,
                               {alias}url AS url,
                               {alias}canonical_url AS canonical_url,
                               {alias}resolved_target_url AS resolved_target_url,
                               {alias}title AS title,
                               {alias}quality_score AS quality_score,
                               {alias}last_crawled AS last_crawled,
                               {alias}first_crawled AS first_crawled,
                               {alias}created_at AS created_at
                        {from_sql} {where_clause}
                        """,
                        params,
                    )
                    raw_stat_articles = [dict(row) for row in cursor.fetchall()]
                    stat_articles_by_key = {}
                    for article in raw_stat_articles:
                        key = self._article_dedupe_key(article)
                        current = stat_articles_by_key.get(key)
                        if self._is_better_article_representative(article, current):
                            stat_articles_by_key[key] = article
                    domain_rows = list(stat_articles_by_key.values())
                    total_articles = len(domain_rows)
                    canonical_domains = {}
                    for row in domain_rows:
                        raw_domain = str(row['domain'] or '').strip().lower()
                        if not raw_domain:
                            raw_domain = self._extract_domain(str(row['url'] or ''))
                        raw_domain = raw_domain.split(':', 1)[0].removeprefix('www.')
                        if raw_domain and raw_domain != 'unknown':
                            canonical_domains[raw_domain] = canonical_domains.get(raw_domain, 0) + 1
                    domains = len(canonical_domains)
                    # Registered sources are a different metric from domains that
                    # have actually produced an active article.  Expose both so
                    # the article page cannot make an onboarding gap look like a
                    # counting defect.
                    source_scope_sql = ""
                    source_scope_params = []
                    if industry_pack_id:
                        from industry_packs import industry_pack_loader
                        effective_ids = [
                            item['id']
                            for item in industry_pack_loader.effective_pack_set(industry_pack_id)
                        ]
                        placeholders = ','.join('?' for _ in effective_ids)
                        source_scope_sql = (
                            " AND EXISTS (SELECT 1 FROM intel_source_industries si "
                            "WHERE si.source_id=intel_sources.id AND si.is_active=1 "
                            f"AND si.industry_pack_id IN ({placeholders}))"
                        )
                        source_scope_params = effective_ids
                    cursor.execute(
                        f"SELECT source_url FROM intel_sources WHERE is_enabled=1{source_scope_sql}",
                        source_scope_params,
                    )
                    registered_source_domains = set()
                    for source_row in cursor.fetchall():
                        source_domain = self._extract_domain(str(source_row['source_url'] or '')).split(':', 1)[0].removeprefix('www.')
                        if source_domain and source_domain != 'unknown':
                            registered_source_domains.add(source_domain)

                    # 聚合时间统一按 UTC 存储和输出。历史数据的 created_at
                    # 是香港本地时间，仅在缺少聚合时间时回退并换算为 UTC。
                    last_sql = f"""
                    SELECT MAX(
                        COALESCE(
                            NULLIF({alias}last_crawled, ''),
                            NULLIF({alias}first_crawled, ''),
                            datetime({alias}created_at, '-8 hours')
                        )
                    ) as last_crawled
                    {from_sql} {where_clause}
                    """
                    cursor.execute(last_sql, params)
                    last_crawled = cursor.fetchone()['last_crawled']
                    
                    # “今日新增”按香港自然日统计首次抓取时间。
                    # 香港当天 [00:00, 次日 00:00) 先换算成 UTC，再与
                    # first_crawled 比较，避免午夜后 8 小时被归入前一天。
                    today_sql = f"""
                    SELECT COUNT(*) as today_new {from_sql}
                    {where_clause}
                      AND datetime(
                            COALESCE(
                                NULLIF({alias}first_crawled, ''),
                                datetime({alias}created_at, '-8 hours')
                            )
                          ) >= datetime('now', '+8 hours', 'start of day', '-8 hours')
                      AND datetime(
                            COALESCE(
                                NULLIF({alias}first_crawled, ''),
                                datetime({alias}created_at, '-8 hours')
                            )
                          ) < datetime('now', '+8 hours', 'start of day', '+1 day', '-8 hours')
                    """
                    cursor.execute(today_sql, params)
                    today_new = cursor.fetchone()['today_new']
                    
                    # 域名统计
                    domain_stats_sql = f"""
                    SELECT {alias}domain AS domain, COUNT(*) as count
                    {from_sql} {where_clause}
                    GROUP BY {alias}domain
                    ORDER BY count DESC
                    """
                    cursor.execute(domain_stats_sql, params)
                    # Return the same canonical aggregation used by the headline.
                    domain_stats = dict(sorted(canonical_domains.items(), key=lambda item: (-item[1], item[0])))
                    
                    return {
                        'total_articles': total_articles,
                        'domains': domains,
                        'registered_source_domains': len(registered_source_domains),
                        'raw_domain_rows': len({str(row['domain'] or '').strip().lower() for row in domain_rows if str(row['domain'] or '').strip()}),
                        'last_crawl_time': last_crawled,
                        'today_new_articles': today_new,
                        'today_date': cursor.execute(
                            "SELECT date('now', '+8 hours')"
                        ).fetchone()[0],
                        'timezone': 'Asia/Hong_Kong',
                        'domain_stats': domain_stats
                    }
                finally:
                    cursor.close()
                
        except Exception as e:
            print(f"❌ 获取统计信息失败: {e}")
            return {}
    
    def _extract_domain(self, url: str) -> str:
        """
        从URL提取域名
        
        Args:
            url: URL
            
        Returns:
            str: 域名
        """
        try:
            parsed = urlparse(url)
            return parsed.netloc.lower()
        except:
            return 'unknown'
    
    def __enter__(self):
        """上下文管理器入口"""
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        self.disconnect()
    
    # ==================== 分类管理相关方法 ====================
    
    def insert_category(self, category_data: Dict) -> Optional[int]:
        """插入分类"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    # 手动设置中国时间，确保时区正确
                    china_time = get_china_time().strftime('%Y-%m-%d %H:%M:%S')

                    insert_sql = """
                    INSERT INTO categories (name, description, display_order, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """
                    
                    values = (
                        category_data.get('name', ''),
                        category_data.get('description', ''),
                        category_data.get('display_order', 0),
                        category_data.get('is_active', True),
                        china_time,
                        china_time
                    )
                    
                    cursor.execute(insert_sql, values)
                    category_id = cursor.lastrowid
                    self.connection.commit()
                    
                    print(f"✅ 分类入库成功: {category_data.get('name', '')} (ID: {category_id})")
                    return category_id
                finally:
                    cursor.close()
                
        except sqlite3.IntegrityError:
            print(f"⚠️ 分类已存在: {category_data.get('name', '')}")
            return None
        except Exception as e:
            print(f"❌ 插入分类失败: {e}")
            return None
    
    def get_categories(self, is_active: bool = None) -> List[Dict]:
        """获取分类列表"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    where_conditions = []
                    params = []
                    
                    if is_active is not None:
                        where_conditions.append("is_active = ?")
                        params.append(is_active)
                    
                    where_clause = " AND ".join(where_conditions) if where_conditions else "1=1"
                    
                    select_sql = f"""
                    SELECT * FROM categories 
                    WHERE {where_clause}
                    ORDER BY display_order, name
                    """
                    
                    cursor.execute(select_sql, params)
                    categories = [dict(row) for row in cursor.fetchall()]
                    
                    return categories
                finally:
                    cursor.close()
        except Exception as e:
            print(f"❌ 获取分类列表失败: {e}")
            return []
    
    def get_category_by_id(self, category_id: int) -> Optional[Dict]:
        """根据ID获取分类"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    select_sql = "SELECT * FROM categories WHERE id = ?"
                    cursor.execute(select_sql, (category_id,))
                    result = cursor.fetchone()
                    return dict(result) if result else None
                finally:
                    cursor.close()
        except Exception as e:
            print(f"❌ 获取分类失败: {e}")
            return None
    
    def update_category(self, category_id: int, category_data: Dict) -> bool:
        """更新分类"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    update_sql = """
                    UPDATE categories SET
                        name = ?,
                        description = ?,
                        display_order = ?,
                        is_active = ?,
                        updated_at = datetime('now', 'localtime')
                    WHERE id = ?
                    """
                    
                    values = (
                        category_data.get('name', ''),
                        category_data.get('description', ''),
                        category_data.get('display_order', 0),
                        category_data.get('is_active', True),
                        category_id
                    )
                    
                    cursor.execute(update_sql, values)
                    self.connection.commit()
                    print(f"✅ 分类更新成功: ID {category_id}")
                    return True
                finally:
                    cursor.close()
        except Exception as e:
            print(f"❌ 更新分类失败: {e}")
            return False
    
    def delete_category(self, category_id: int) -> bool:
        """删除分类"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    delete_sql = "DELETE FROM categories WHERE id = ?"
                    cursor.execute(delete_sql, (category_id,))
                    self.connection.commit()
                    print(f"✅ 分类删除成功: ID {category_id}")
                    return True
                finally:
                    cursor.close()
        except Exception as e:
            print(f"❌ 删除分类失败: {e}")
            return False
    
    # ==================== URL管理相关方法 ====================
    
    def insert_managed_url(self, url_data: Dict) -> Optional[int]:
        """插入管理的URL"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    is_valid_url, url, url_error = validate_http_url(url_data.get('url', ''))
                    if not is_valid_url:
                        print(f"❌ URL格式无效，拒绝入库: {url_data.get('url', '')} ({url_error})")
                        return None
                    domain = self._extract_domain(url)
                    
                    # 🔍 调试：打印传入的完整数据
                    print(f"🔍 insert_managed_url 收到数据: {url_data}")
                    
                    # 🔥 修复：检查是否有category字段而不是category_id
                    category_id = url_data.get('category_id')
                    if category_id is None and 'category' in url_data:
                        # 如果收到的是category名称，需要转换为ID
                        category_name = url_data.get('category')
                        if category_name and category_name != '默认分类':
                            # 根据分类名称查找ID
                            cursor_temp = self.connection.cursor()
                            cursor_temp.execute("SELECT id FROM categories WHERE name = ?", (category_name,))
                            result = cursor_temp.fetchone()
                            if result:
                                category_id = result['id']
                                print(f"🔄 分类名称转换: '{category_name}' -> ID: {category_id}")
                            cursor_temp.close()
                    
                    print(f"🔍 最终的category_id: {category_id} (类型: {type(category_id)})")
                    
                    # 🔥 修复：根据category_id查询分类名称
                    category_text = '默认分类'
                    if category_id:
                        cursor_temp = self.connection.cursor()
                        cursor_temp.execute("SELECT name FROM categories WHERE id = ?", (category_id,))
                        result = cursor_temp.fetchone()
                        if result:
                            category_text = result[0] if isinstance(result, tuple) else result['name']
                            print(f"🔄 从数据库获取分类名称: ID {category_id} -> '{category_text}'")
                        cursor_temp.close()
                    
                    # 如果url_data中有category字段，也可以使用（向后兼容）
                    if 'category' in url_data and url_data['category']:
                        category_text = url_data['category']
                        print(f"🔄 使用传入的category文本: '{category_text}'")
                    
                    # 手动设置中国时间，确保时区正确
                    china_time = get_china_time().strftime('%Y-%m-%d %H:%M:%S')
                    runtime_rows = self.connection.execute(
                        """
                        SELECT setting_key, setting_value FROM intel_runtime_settings
                        WHERE setting_key IN (
                            'active_industry_pack_id',
                            'active_industry_pack_version_id',
                            'active_industry_activation_id'
                        )
                        """
                    ).fetchall()
                    runtime = {str(row[0]): str(row[1]) for row in runtime_rows}
                    
                    insert_sql = """
                    INSERT INTO managed_urls (
                        url, name, description, category_id, category, parent_url_id, domain, is_active,
                        auto_crawl, crawl_frequency, auth_config, requires_auth, auth_config_id, 
                        keywords, days_limit, industry_pack_id,
                        industry_pack_version_id, activation_id, ownership_type,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                    
                    values = (
                        url,
                        url_data.get('name', ''),
                        url_data.get('description', ''),
                        category_id,  # 直接使用提取的值
                        category_text,  # 添加category文本字段
                        url_data.get('parent_url_id'),
                        domain,
                        url_data.get('is_active', True),
                        url_data.get('auto_crawl', False),
                        url_data.get('crawl_frequency', ''),
                        url_data.get('auth_config'),
                        url_data.get('requires_auth', False),  # 🔐 添加认证标志
                        url_data.get('auth_config_id'),  # 🔐 添加认证配置ID
                        url_data.get('keywords', ''),  # 🔥 关键词过滤
                        url_data.get('days_limit', 7),  # 🔥 日期限制（默认7天）
                        url_data.get('industry_pack_id') or runtime.get('active_industry_pack_id', ''),
                        url_data.get('industry_pack_version_id') or runtime.get('active_industry_pack_version_id') or None,
                        url_data.get('activation_id') or runtime.get('active_industry_activation_id', ''),
                        url_data.get('ownership_type') or 'protected_manual',
                        china_time,
                        china_time
                    )
                    
                    # 调试日志：打印实际插入的值
                    print(f"💾 数据库插入 - category_id: {category_id}, parent_url_id: {url_data.get('parent_url_id')}")
                    print(f"💾 完整插入值: {values}")
                    
                    cursor.execute(insert_sql, values)
                    url_id = cursor.lastrowid
                    self.connection.commit()
                    
                    # 验证插入：读取刚插入的记录
                    cursor.execute("SELECT category_id FROM managed_urls WHERE id = ?", (url_id,))
                    saved_category = cursor.fetchone()
                    if saved_category:
                        print(f"✅ URL入库成功: {url_data.get('name', url)[:30]}... (ID: {url_id}, category_id已保存: {saved_category['category_id']})")
                    else:
                        print(f"✅ URL入库成功: {url_data.get('name', url)[:30]}... (ID: {url_id})")
                    return url_id
                finally:
                    cursor.close()
                
        except sqlite3.IntegrityError:
            print(f"⚠️ URL已存在: {url}")
            return None
        except Exception as e:
            print(f"❌ 插入URL失败: {e}")
            return None
    
    def get_managed_urls(self, page: int = 1, per_page: int = 20,
                         category_id: int = None, parent_url_id: int = None,
                         is_active: bool = None,
                         industry_pack_id: str = None,
                         effective_pack_ids: Optional[Iterable[str]] = None) -> Tuple[List[Dict], int]:
        """获取管理的URL列表（支持分类和父级URL筛选）"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    where_conditions = []
                    params = []
                    
                    if category_id:
                        where_conditions.append("mu.category_id = ?")
                        params.append(category_id)
                    
                    if parent_url_id is not None:
                        if parent_url_id == 0:
                            # parent_url_id = 0 表示查询顶级URL（没有父级）
                            where_conditions.append("mu.parent_url_id IS NULL")
                        else:
                            where_conditions.append("mu.parent_url_id = ?")
                            params.append(parent_url_id)
                    
                    if is_active is not None:
                        where_conditions.append("mu.is_active = ?")
                        params.append(is_active)
                    scoped_pack_ids = list(
                        dict.fromkeys(
                            str(value or '').strip()
                            for value in (effective_pack_ids or [])
                            if str(value or '').strip()
                        )
                    )
                    if not scoped_pack_ids and industry_pack_id:
                        scoped_pack_ids = [str(industry_pack_id)]
                    if scoped_pack_ids:
                        placeholders = ",".join("?" for _ in scoped_pack_ids)
                        where_conditions.append(
                            "("
                            f"COALESCE(NULLIF(TRIM(mu.industry_pack_id), ''), 'family_office') IN ({placeholders}) "
                            "OR EXISTS ("
                            "SELECT 1 FROM intel_source_origins iso "
                            "JOIN intel_source_industries isi ON isi.source_id=iso.source_id "
                            "WHERE iso.managed_url_id=mu.id AND iso.is_active=1 "
                            "AND isi.is_active=1 "
                            f"AND isi.industry_pack_id IN ({placeholders})"
                            "))"
                        )
                        params.extend([*scoped_pack_ids, *scoped_pack_ids])
                    
                    where_clause = " AND ".join(where_conditions) if where_conditions else "1=1"
                    
                    # 获取总数
                    count_sql = f"SELECT COUNT(*) as total FROM managed_urls mu WHERE {where_clause}"
                    cursor.execute(count_sql, params)
                    total = cursor.fetchone()['total']
                    
                    # 获取URL列表（关联分类、父级URL和认证配置信息）
                    offset = (page - 1) * per_page
                    select_sql = f"""
                    SELECT 
                        mu.*,
                        c.name as category_name,
                        parent.name as parent_url_name,
                        ac.name as auth_config_name,
                        ac.login_url as auth_login_url,
                        ac.username as auth_username,
                        ac.password as auth_password,
                        ac.username_selector as auth_username_selector,
                        ac.password_selector as auth_password_selector,
                        ac.submit_selector as auth_submit_selector,
                        ac.wait_after_submit as auth_wait_after_submit
                    FROM managed_urls mu
                    LEFT JOIN categories c ON mu.category_id = c.id
                    LEFT JOIN managed_urls parent ON mu.parent_url_id = parent.id
                    LEFT JOIN auth_configs ac ON mu.auth_config_id = ac.id
                    WHERE {where_clause}
                    ORDER BY mu.created_at DESC
                    LIMIT ? OFFSET ?
                    """
                    params.extend([per_page, offset])
                    
                    cursor.execute(select_sql, params)
                    urls = []
                    for row in cursor.fetchall():
                        url_dict = dict(row)
                        # 如果有auth_config_id，构建auth_config JSON对象
                        if url_dict.get('auth_config_id') and url_dict.get('auth_login_url'):
                            url_dict['auth_config'] = {
                                'name': url_dict.get('auth_config_name'),
                                'login_url': url_dict.get('auth_login_url'),
                                'username': url_dict.get('auth_username'),
                                'password': url_dict.get('auth_password'),
                                'username_selector': url_dict.get('auth_username_selector'),
                                'password_selector': url_dict.get('auth_password_selector'),
                                'submit_selector': url_dict.get('auth_submit_selector'),
                                'wait_after_submit': url_dict.get('auth_wait_after_submit', 5)
                            }
                        # 移除临时字段
                        for key in ['auth_config_name', 'auth_login_url', 'auth_username', 'auth_password',
                                   'auth_username_selector', 'auth_password_selector', 'auth_submit_selector', 'auth_wait_after_submit']:
                            url_dict.pop(key, None)
                        urls.append(url_dict)
                    
                    return urls, total
                finally:
                    cursor.close()
                
        except Exception as e:
            print(f"❌ 获取URL列表失败: {e}")
            return [], 0
    
    def get_managed_url_by_id(self, url_id: int) -> Optional[Dict]:
        """根据ID获取管理的URL（包含认证配置详情）"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    select_sql = """
                    SELECT 
                        mu.*,
                        ac.name as auth_config_name,
                        ac.login_url as auth_login_url,
                        ac.username as auth_username,
                        ac.password as auth_password,
                        ac.username_selector as auth_username_selector,
                        ac.password_selector as auth_password_selector,
                        ac.submit_selector as auth_submit_selector,
                        ac.wait_after_submit as auth_wait_after_submit
                    FROM managed_urls mu
                    LEFT JOIN auth_configs ac ON mu.auth_config_id = ac.id
                    WHERE mu.id = ?
                    """
                    cursor.execute(select_sql, (url_id,))
                    result = cursor.fetchone()
                    if result:
                        url_dict = dict(result)
                        # 如果有auth_config_id，构建auth_config JSON对象
                        if url_dict.get('auth_config_id') and url_dict.get('auth_login_url'):
                            url_dict['auth_config'] = {
                                'name': url_dict.get('auth_config_name'),
                                'login_url': url_dict.get('auth_login_url'),
                                'username': url_dict.get('auth_username'),
                                'password': url_dict.get('auth_password'),
                                'username_selector': url_dict.get('auth_username_selector'),
                                'password_selector': url_dict.get('auth_password_selector'),
                                'submit_selector': url_dict.get('auth_submit_selector'),
                                'wait_after_submit': url_dict.get('auth_wait_after_submit', 5)
                            }
                        # 移除临时字段
                        for key in ['auth_config_name', 'auth_login_url', 'auth_username', 'auth_password',
                                   'auth_username_selector', 'auth_password_selector', 'auth_submit_selector', 'auth_wait_after_submit']:
                            url_dict.pop(key, None)
                        return url_dict
                    return None
                finally:
                    cursor.close()
        except Exception as e:
            print(f"❌ 根据ID获取URL失败: {e}")
            return None
    
    def get_managed_url_by_url(self, url: str) -> Optional[Dict]:
        """根据URL获取管理的URL"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    select_sql = """
                    SELECT mu.*, c.name as category_name
                    FROM managed_urls mu
                    LEFT JOIN categories c ON mu.category_id = c.id
                    WHERE mu.url = ?
                    """
                    cursor.execute(select_sql, (url,))
                    result = cursor.fetchone()
                    return dict(result) if result else None
                finally:
                    cursor.close()
        except Exception as e:
            print(f"❌ 根据URL获取URL失败: {e}")
            return None
    
    def find_source_url_info(self, article_url: str) -> Optional[Dict]:
        """
        根据文章URL查找对应的来源URL信息
        匹配逻辑：找到与文章URL domain相同的managed_url
        
        Returns:
            Dict: {'url_id': int, 'category_id': int, 'category_name': str} 或 None
        """
        try:
            from urllib.parse import urlparse
            article_domain = urlparse(article_url).netloc.lower()
            
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    # 查找domain匹配且parent_url_id为NULL的managed_url（主URL）
                    select_sql = """
                    SELECT mu.id as url_id, mu.category_id, c.name as category_name
                    FROM managed_urls mu
                    LEFT JOIN categories c ON mu.category_id = c.id
                    WHERE mu.domain = ? AND mu.parent_url_id IS NULL
                    LIMIT 1
                    """
                    cursor.execute(select_sql, (article_domain,))
                    result = cursor.fetchone()
                    
                    if result:
                        return {
                            'url_id': result[0],
                            'category_id': result[1],
                            'category_name': result[2]
                        }
                    return None
                finally:
                    cursor.close()
        except Exception as e:
            print(f"⚠️ 查找来源URL失败: {e}")
            return None
    
    def update_managed_url(self, url_id: int, url_data: Dict) -> bool:
        """更新管理的URL"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    # 🔥 先获取原有数据，用于填充未传入的字段
                    cursor.execute("SELECT url, name, description, category_id, category FROM managed_urls WHERE id = ?", (url_id,))
                    existing = cursor.fetchone()
                    if not existing:
                        print(f"❌ URL不存在: ID {url_id}")
                        return False
                    
                    existing_url = existing['url'] if isinstance(existing, dict) else existing[0]
                    existing_name = existing['name'] if isinstance(existing, dict) else existing[1]
                    
                    # 🔥 修复：根据category_id查询分类名称
                    category_id = url_data.get('category_id')
                    category_text = '默认分类'
                    
                    if category_id:
                        cursor_temp = self.connection.cursor()
                        cursor_temp.execute("SELECT name FROM categories WHERE id = ?", (category_id,))
                        result = cursor_temp.fetchone()
                        if result:
                            category_text = result[0] if isinstance(result, tuple) else result['name']
                            print(f"🔄 更新URL - 从数据库获取分类名称: ID {category_id} -> '{category_text}'")
                        cursor_temp.close()
                    
                    # 如果url_data中有category字段，也可以使用（向后兼容）
                    if 'category' in url_data and url_data['category']:
                        category_text = url_data['category']
                        print(f"🔄 更新URL - 使用传入的category文本: '{category_text}'")
                    
                    # 🔥 获取要更新的url值，如果没传入则保留原值
                    new_url = url_data.get('url') if url_data.get('url') else existing_url
                    is_valid_url, new_url, url_error = validate_http_url(new_url)
                    if not is_valid_url:
                        print(f"❌ URL格式无效，拒绝更新: {url_data.get('url', '')} ({url_error})")
                        return False
                    print(f"🔄 更新URL地址: '{existing_url}' -> '{new_url}'")
                    
                    update_sql = """
                    UPDATE managed_urls SET
                        url = ?,
                        name = ?,
                        description = ?,
                        category_id = ?,
                        category = ?,
                        parent_url_id = ?,
                        is_active = ?,
                        auto_crawl = ?,
                        crawl_frequency = ?,
                        auth_config = ?,
                        auth_config_id = ?,
                        requires_auth = ?,
                        keywords = ?,
                        days_limit = ?,
                        updated_at = datetime('now', 'localtime')
                    WHERE id = ?
                    """
                    
                    values = (
                        new_url,  # 🔥 修复：添加url字段更新
                        url_data.get('name') if url_data.get('name') else existing_name,
                        url_data.get('description', ''),
                        category_id,
                        category_text,  # 🔥 添加category文本字段
                        url_data.get('parent_url_id'),
                        url_data.get('is_active', True),
                        url_data.get('auto_crawl', False),
                        url_data.get('crawl_frequency', ''),
                        url_data.get('auth_config'),
                        url_data.get('auth_config_id'),
                        True if url_data.get('requires_auth') else False,
                        url_data.get('keywords', ''),  # 🔥 关键词过滤
                        url_data.get('days_limit', 7),  # 🔥 日期限制
                        url_id
                    )
                    
                    cursor.execute(update_sql, values)
                    self.connection.commit()
                    print(f"✅ URL更新成功: ID {url_id}, category_id={category_id}, category='{category_text}'")
                    return True
                finally:
                    cursor.close()
                
        except Exception as e:
            print(f"❌ 更新URL失败: {e}")
            return False
    
    def delete_managed_url(self, url_id: int) -> bool:
        """删除管理的URL"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    delete_sql = "DELETE FROM managed_urls WHERE id = ?"
                    cursor.execute(delete_sql, (url_id,))
                    self.connection.commit()
                    print(f"✅ URL删除成功: ID {url_id}")
                    return True
                finally:
                    cursor.close()
        except Exception as e:
            print(f"❌ 删除URL失败: {e}")
            return False
    
    def update_url_crawl_stats(self, url_id: int, success: bool, last_crawled: datetime = None):
        """更新URL聚合统计信息"""
        try:
            self._ensure_connection()
            with self.lock:
                if last_crawled is None:
                    last_crawled = get_china_time()
                
                update_sql = """
                UPDATE managed_urls SET
                    total_crawls = total_crawls + 1,
                    success_crawls = success_crawls + ?,
                    failed_crawls = failed_crawls + ?,
                    last_crawled = ?,
                    updated_at = datetime('now', 'localtime')
                WHERE id = ?
                """
                
                values = (
                    1 if success else 0,
                    0 if success else 1,
                    last_crawled.isoformat() if isinstance(last_crawled, datetime) else last_crawled,
                    url_id
                )
                
                cursor = self.connection.cursor()
                try:
                    cursor.execute(update_sql, values)
                    self.connection.commit()
                    return True
                finally:
                    cursor.close()
                
        except Exception as e:
            print(f"❌ 更新URL聚合统计失败: {e}")
            return False
    
    # ==================== 定时任务相关方法 ====================
    
    def insert_scheduled_task(self, task_data: Dict) -> Optional[int]:
        """插入定时任务"""
        try:
            task_data = _normalize_schedule_fields(task_data)
            self._ensure_connection()
            with self.lock:
                # 手动设置中国时间，确保时区正确
                china_time = get_china_time().strftime('%Y-%m-%d %H:%M:%S')
                
                insert_sql = """
                INSERT INTO scheduled_tasks (
                    task_name, task_type, target_url, url_id,
                    schedule_type, schedule_time, schedule_day, cron_expression,
                    keywords, industry_pack_id, industry_pack_version_id,
                    activation_id, ownership_type, is_active, ragflow_kb_id, days_limit,
                    schedule_weekdays, schedule_monthdays, config, next_run, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
                runtime_rows = self.connection.execute(
                    """
                    SELECT setting_key, setting_value FROM intel_runtime_settings
                    WHERE setting_key IN (
                        'active_industry_pack_id',
                        'active_industry_pack_version_id',
                        'active_industry_activation_id'
                    )
                    """
                ).fetchall()
                runtime = {str(row[0]): str(row[1]) for row in runtime_rows}
                
                values = (
                    task_data.get('task_name', ''),
                    task_data.get('task_type', 'crawl'),
                    normalize_task_url(task_data.get('target_url', '')),
                    task_data.get('url_id'),
                    task_data.get('schedule_type', 'daily'),
                    _normalize_schedule_time_value(task_data.get('schedule_time')),
                    task_data.get('schedule_day'),
                    task_data.get('cron_expression'),
                    task_data.get('keywords', ''),
                    task_data.get('industry_pack_id') or runtime.get('active_industry_pack_id', ''),
                    task_data.get('industry_pack_version_id') or runtime.get('active_industry_pack_version_id') or None,
                    task_data.get('activation_id') or runtime.get('active_industry_activation_id', ''),
                    task_data.get('ownership_type') or 'protected_manual',
                    task_data.get('is_active', True),
                    task_data.get('ragflow_kb_id'),
                    coerce_int(task_data.get('days_limit', 7), 7, 0, 3650),
                    _normalize_schedule_list_value(task_data.get('schedule_weekdays', ''), 0, 6),  # 🔥 每周执行日
                    _normalize_schedule_list_value(task_data.get('schedule_monthdays', ''), 1, 31),  # 🔥 每月执行日
                    json.dumps(task_data.get('config', {})),
                    task_data.get('next_run'),  # 🔥 下次执行时间
                    china_time,
                    china_time
                )
                
                cursor = self.connection.cursor()
                try:
                    cursor.execute(insert_sql, values)
                    task_id = cursor.lastrowid
                    self.connection.commit()
                    print(f"✅ 定时任务入库成功: {task_data.get('task_name', '')} (ID: {task_id})")
                    return task_id
                finally:
                    cursor.close()
                
        except Exception as e:
            print(f"❌ 插入定时任务失败: {e}")
            return None
    
    def get_scheduled_tasks(self, page: int = 1, per_page: int = 20,
                           is_active: bool = None,
                           industry_pack_id: str = None) -> Tuple[List[Dict], int]:
        """获取定时任务列表"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    where_conditions = []
                    params = []

                    if is_active is not None:
                        where_conditions.append("st.is_active = ?")
                        params.append(is_active)
                    if industry_pack_id:
                        where_conditions.append(
                            "COALESCE(NULLIF(TRIM(st.industry_pack_id), ''), 'family_office') = ?"
                        )
                        params.append(str(industry_pack_id))

                    where_clause = " AND ".join(where_conditions) if where_conditions else "1=1"

                    # 获取总数
                    count_sql = f"SELECT COUNT(*) as total FROM scheduled_tasks st LEFT JOIN managed_urls mu ON st.url_id = mu.id WHERE {where_clause}"
                    cursor.execute(count_sql, params)
                    total = cursor.fetchone()['total']
                    
                    # 获取任务列表（优先按url_id匹配，无则按target_url前缀匹配managed_urls）
                    offset = (page - 1) * per_page
                    select_sql = f"""
                    SELECT st.*,
                        COALESCE(
                            (SELECT name FROM managed_urls WHERE id = st.url_id LIMIT 1),
                            (SELECT name FROM managed_urls
                             WHERE REPLACE(REPLACE(st.target_url,'https://',''),'http://','')
                                   LIKE REPLACE(REPLACE(url,'https://',''),'http://','') || '%'
                             ORDER BY LENGTH(url) DESC LIMIT 1)
                        ) as url_display_name
                    FROM scheduled_tasks st
                    WHERE {where_clause}
                    ORDER BY st.created_at DESC
                    LIMIT ? OFFSET ?
                    """
                    params.extend([per_page, offset])
                    
                    cursor.execute(select_sql, params)
                    tasks = []
                    for row in cursor.fetchall():
                        task = dict(row)
                        if task['config']:
                            try:
                                task['config'] = json.loads(task['config'])
                            except:
                                task['config'] = {}
                        ingest_stats = self._get_schedule_ingest_stats(cursor, task)
                        task['ingest_stats'] = ingest_stats
                        task['matched_article_count'] = ingest_stats['matched_article_count']
                        task['ragflow_article_count'] = ingest_stats['ragflow_article_count']
                        tasks.append(task)
                    
                    return tasks, total
                finally:
                    cursor.close()
                
        except Exception as e:
            print(f"❌ 获取定时任务列表失败: {e}")
            return [], 0

    def get_scheduled_task(self, task_id: int) -> Optional[Dict]:
        """根据ID获取单个定时任务"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    cursor.execute("""
                        SELECT st.*,
                            COALESCE(
                                (SELECT name FROM managed_urls WHERE id = st.url_id LIMIT 1),
                                (SELECT name FROM managed_urls
                                 WHERE REPLACE(REPLACE(st.target_url,'https://',''),'http://','')
                                       LIKE REPLACE(REPLACE(url,'https://',''),'http://','') || '%'
                                 ORDER BY LENGTH(url) DESC LIMIT 1)
                            ) as url_display_name
                        FROM scheduled_tasks st
                        WHERE st.id = ?
                    """, (task_id,))
                    row = cursor.fetchone()
                    if not row:
                        return None
                    task = dict(row)
                    if task.get('config'):
                        try:
                            task['config'] = json.loads(task['config'])
                        except Exception:
                            task['config'] = {}
                    return task
                finally:
                    cursor.close()
        except Exception as e:
            print(f"❌ 获取定时任务失败: {e}")
            return None
    
    def update_scheduled_task(self, task_id: int, task_data: Dict) -> bool:
        """更新定时任务"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    # 🔥 先获取现有任务数据（用于填充缺失字段）
                    cursor.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,))
                    row = cursor.fetchone()
                    if not row:
                        print(f"❌ 任务不存在: ID {task_id}")
                        return False
                    
                    # 获取现有任务的完整数据
                    columns = [desc[0] for desc in cursor.description]
                    existing_task = dict(zip(columns, row))
                    
                    # 合并现有数据和更新数据（更新数据优先）
                    full_task_data = existing_task.copy()
                    full_task_data.update(task_data)
                    full_task_data = _normalize_schedule_fields(full_task_data)
                    if full_task_data.get('target_url'):
                        full_task_data['target_url'] = normalize_task_url(full_task_data.get('target_url'))
                    
                    # 🔥 用完整数据重新计算next_run
                    next_run = self._calculate_next_run_for_task(full_task_data)
                    
                    update_sql = """
                    UPDATE scheduled_tasks SET
                        task_name = ?,
                        target_url = ?,
                        is_active = ?,
                        schedule_type = ?,
                        schedule_time = ?,
                        schedule_day = ?,
                        cron_expression = ?,
                        keywords = ?,
                        industry_pack_id = ?,
                        industry_pack_version_id = ?,
                        activation_id = ?,
                        ownership_type = ?,
                        ragflow_kb_id = ?,
                        days_limit = ?,
                        schedule_weekdays = ?,
                        schedule_monthdays = ?,
                        config = ?,
                        next_run = ?,
                        updated_at = datetime('now', 'localtime')
                    WHERE id = ?
                    """
                    
                    # 🔥 使用合并后的完整数据，避免字段被清空
                    values = (
                        full_task_data.get('task_name', ''),
                        normalize_task_url(full_task_data.get('target_url', '')),
                        full_task_data.get('is_active', True),
                        full_task_data.get('schedule_type', 'daily'),
                        _normalize_schedule_time_value(full_task_data.get('schedule_time')),
                        full_task_data.get('schedule_day'),
                        full_task_data.get('cron_expression'),
                        full_task_data.get('keywords', ''),
                        full_task_data.get('industry_pack_id', ''),
                        full_task_data.get('industry_pack_version_id'),
                        full_task_data.get('activation_id', ''),
                        full_task_data.get('ownership_type', 'legacy'),
                        full_task_data.get('ragflow_kb_id'),
                        coerce_int(full_task_data.get('days_limit', 7), 7, 0, 3650),
                        _normalize_schedule_list_value(full_task_data.get('schedule_weekdays', ''), 0, 6),  # 🔥 每周执行日
                        _normalize_schedule_list_value(full_task_data.get('schedule_monthdays', ''), 1, 31),  # 🔥 每月执行日
                        json.dumps(full_task_data.get('config', {})) if isinstance(full_task_data.get('config'), dict) else full_task_data.get('config', '{}'),
                        next_run.isoformat() if next_run else None,
                        task_id
                    )
                    
                    cursor.execute(update_sql, values)
                    self.connection.commit()
                    print(f"✅ 定时任务更新成功: ID {task_id}, next_run={next_run}")
                    return True
                finally:
                    cursor.close()
                
        except Exception as e:
            print(f"❌ 更新定时任务失败: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _calculate_next_run_for_task(self, task_data: Dict):
        """Calculate the next run time using the same rules as the schedule API."""
        import calendar
        from datetime import timedelta

        current_time = get_china_time()
        schedule_type = task_data.get('schedule_type', 'daily')
        schedule_time = _normalize_schedule_time_value(task_data.get('schedule_time'))
        schedule_day = task_data.get('schedule_day')

        def parse_time(value):
            parts = str(value or '00:00:00').split(':')
            return (
                coerce_int(parts[0] if len(parts) > 0 else 0, 0, 0, 23),
                coerce_int(parts[1] if len(parts) > 1 else 0, 0, 0, 59),
                coerce_int(parts[2] if len(parts) > 2 else 0, 0, 0, 59),
            )

        def parse_int_list(value, min_value, max_value):
            result = []
            if isinstance(value, (list, tuple, set)):
                raw_items = value
            else:
                raw_items = str(value or '').split(',')
            for item in raw_items:
                parsed = coerce_int(item, None)
                if parsed is not None and min_value <= parsed <= max_value and parsed not in result:
                    result.append(parsed)
            return sorted(result)

        hour, minute, second = parse_time(schedule_time)

        if schedule_type == 'once':
            next_run = current_time.replace(hour=hour, minute=minute, second=second, microsecond=0)
            return next_run if next_run > current_time else current_time

        if schedule_type == 'daily':
            next_run = current_time.replace(hour=hour, minute=minute, second=second, microsecond=0)
            if next_run <= current_time:
                next_run += timedelta(days=1)
            return next_run

        if schedule_type == 'weekly':
            weekdays = parse_int_list(task_data.get('schedule_weekdays'), 0, 6)
            if not weekdays and schedule_day is not None:
                weekdays = [coerce_int(schedule_day, current_time.weekday(), 0, 6)]
            if not weekdays:
                weekdays = [current_time.weekday()]

            for days_ahead in range(0, 8):
                candidate = current_time + timedelta(days=days_ahead)
                if candidate.weekday() not in weekdays:
                    continue
                next_run = candidate.replace(hour=hour, minute=minute, second=second, microsecond=0)
                if next_run > current_time:
                    return next_run
            return (current_time + timedelta(days=7)).replace(hour=hour, minute=minute, second=second, microsecond=0)

        if schedule_type == 'monthly':
            monthdays = parse_int_list(task_data.get('schedule_monthdays'), 1, 31)
            if not monthdays and schedule_day is not None:
                monthdays = [coerce_int(schedule_day, current_time.day, 1, 31)]
            if not monthdays:
                monthdays = [current_time.day]

            for days_ahead in range(0, 62):
                candidate = current_time + timedelta(days=days_ahead)
                max_day = calendar.monthrange(candidate.year, candidate.month)[1]
                valid_days = {min(day, max_day) for day in monthdays}
                if candidate.day not in valid_days:
                    continue
                next_run = candidate.replace(hour=hour, minute=minute, second=second, microsecond=0)
                if next_run > current_time:
                    return next_run
            return (current_time + timedelta(days=30)).replace(hour=hour, minute=minute, second=second, microsecond=0)

        return current_time + timedelta(hours=1)
    
    def delete_scheduled_task(self, task_id: int) -> bool:
        """删除定时任务"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    delete_sql = "DELETE FROM scheduled_tasks WHERE id = ?"
                    cursor.execute(delete_sql, (task_id,))
                    self.connection.commit()
                    print(f"✅ 定时任务删除成功: ID {task_id}")
                    return True
                finally:
                    cursor.close()
        except Exception as e:
            print(f"❌ 删除定时任务失败: {e}")
            return False

    def _get_schedule_ingest_stats(self, cursor, task: Dict) -> Dict:
        """Count keyword-matched and RAGFlow-synced articles for one schedule target."""
        target_url = (task.get('target_url') or task.get('url') or '').strip()
        target_url_no_slash = target_url.rstrip('/')
        schedule_id = coerce_int(task.get('id'), 0)
        url_id = coerce_int(task.get('url_id'), 0)

        source_conditions = []
        source_params = []

        if schedule_id:
            source_conditions.append("a.source_task_id LIKE ?")
            source_params.append(f"schedule_{schedule_id}_%")

        if url_id:
            source_conditions.append("a.source_url_id = ?")
            source_params.append(url_id)

        url_variants = []
        for value in (target_url, target_url_no_slash):
            if value and value not in url_variants:
                url_variants.append(value)

        for column in ('a.configured_url', 'a.resolved_target_url', 'a.url', 'a.canonical_url'):
            for value in url_variants:
                source_conditions.append(f"TRIM(COALESCE({column}, '')) = ?")
                source_params.append(value)

        if not source_conditions:
            return {
                'matched_article_count': 0,
                'ragflow_article_count': 0,
                'source': 'none'
            }

        article_filter = f"""
            COALESCE(a.status, 'active') != 'deleted'
            AND a.matched_keywords IS NOT NULL
            AND TRIM(a.matched_keywords) != ''
            AND ({' OR '.join(source_conditions)})
        """

        cursor.execute(f"""
            SELECT COUNT(DISTINCT a.id) AS total
            FROM articles a
            WHERE {article_filter}
        """, source_params)
        matched_count = cursor.fetchone()['total'] or 0

        ragflow_params = list(source_params)
        kb_filter = ''
        kb_id = (task.get('ragflow_kb_id') or '').strip()
        if kb_id:
            kb_filter = "AND ard.kb_id = ?"
            ragflow_params.append(kb_id)

        cursor.execute(f"""
            SELECT COUNT(DISTINCT a.id) AS total
            FROM articles a
            INNER JOIN article_ragflow_documents ard ON ard.article_id = a.id
            WHERE {article_filter}
              AND ard.document_id IS NOT NULL
              AND TRIM(ard.document_id) != ''
              AND COALESCE(ard.sync_status, '') NOT IN ('deleted', 'delete_failed')
              {kb_filter}
        """, ragflow_params)
        ragflow_count = cursor.fetchone()['total'] or 0

        return {
            'matched_article_count': int(matched_count),
            'ragflow_article_count': int(ragflow_count),
            'source': 'schedule_id' if schedule_id else ('source_url_id' if url_id else 'target_url')
        }

    def upsert_article_ragflow_document(
        self,
        article_id,
        kb_id: str,
        document_id: str,
        document_name: str,
        sync_status: str = 'uploaded',
        error_message: str = ''
    ) -> bool:
        """Persist the exact local article -> RAGFlow document mapping."""
        kb_id = str(kb_id or '').strip()
        document_id = str(document_id or '').strip()
        document_name = str(document_name or '').strip()
        if not kb_id or not document_id or not document_name:
            return False

        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    cursor.execute(
                        "SELECT id FROM article_ragflow_documents WHERE kb_id = ? AND document_id = ? LIMIT 1",
                        (kb_id, document_id)
                    )
                    existing = cursor.fetchone()
                    values = (
                        coerce_int(article_id, None),
                        document_name,
                        sync_status or 'uploaded',
                        error_message or '',
                    )
                    if existing:
                        cursor.execute("""
                            UPDATE article_ragflow_documents
                            SET article_id = ?,
                                document_name = ?,
                                sync_status = ?,
                                error_message = ?,
                                updated_at = datetime('now', 'localtime')
                            WHERE id = ?
                        """, (*values, existing['id']))
                    else:
                        cursor.execute("""
                            INSERT INTO article_ragflow_documents (
                                article_id, kb_id, document_id, document_name,
                                sync_status, error_message, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'), datetime('now', 'localtime'))
                        """, (
                            coerce_int(article_id, None),
                            kb_id,
                            document_id,
                            document_name,
                            sync_status or 'uploaded',
                            error_message or '',
                        ))
                    self.connection.commit()
                    return True
                finally:
                    cursor.close()
        except Exception as e:
            print(f"⚠️ 写入 RAGFlow 文档映射失败: {e}")
            return False

    def update_article_ragflow_document_status(
        self,
        kb_id: str,
        document_id: str,
        sync_status: str,
        error_message: str = ''
    ) -> bool:
        kb_id = str(kb_id or '').strip()
        document_id = str(document_id or '').strip()
        if not kb_id or not document_id:
            return False
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    cursor.execute("""
                        UPDATE article_ragflow_documents
                        SET sync_status = ?,
                            error_message = ?,
                            updated_at = datetime('now', 'localtime')
                        WHERE kb_id = ? AND document_id = ?
                    """, (sync_status, error_message or '', kb_id, document_id))
                    self.connection.commit()
                    return cursor.rowcount > 0
                finally:
                    cursor.close()
        except Exception as e:
            print(f"⚠️ 更新 RAGFlow 文档映射状态失败: {e}")
            return False

    def get_article_ragflow_documents(self, article_id=None, kb_id: str = '') -> List[Dict]:
        """Return RAGFlow document mappings for diagnostics and deletion preview."""
        conditions = []
        params = []
        if article_id is not None:
            conditions.append("article_id = ?")
            params.append(coerce_int(article_id, 0))
        if kb_id:
            conditions.append("kb_id = ?")
            params.append(kb_id)
        where = " AND ".join(conditions) if conditions else "1=1"

        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    cursor.execute(f"""
                        SELECT *
                        FROM article_ragflow_documents
                        WHERE {where}
                        ORDER BY updated_at DESC, id DESC
                    """, params)
                    return [dict(row) for row in cursor.fetchall()]
                finally:
                    cursor.close()
        except Exception as e:
            print(f"⚠️ 查询 RAGFlow 文档映射失败: {e}")
            return []

    def _iter_keyword_field_rows(self, cursor, table_name: str) -> List[Dict]:
        if not self._table_exists(cursor, table_name):
            return []
        id_column = 'task_id' if table_name == 'crawl_tasks' else 'id'
        cursor.execute(f"SELECT {id_column} AS row_id, keywords FROM {table_name} WHERE keywords IS NOT NULL AND TRIM(keywords) != ''")
        return [dict(row) for row in cursor.fetchall()]

    def preview_keyword_merge(self, source_keywords: List[str], target_keyword: str) -> Dict:
        """Preview article/task impact for a canonical keyword merge."""
        from keyword_governance import clean_keyword, keyword_key, parse_keyword_text

        target = clean_keyword(target_keyword)
        sources = []
        seen = set()
        for item in source_keywords or []:
            keyword = clean_keyword(item)
            key = keyword_key(keyword)
            if keyword and key != keyword_key(target) and key not in seen:
                seen.add(key)
                sources.append(keyword)

        source_keys = {keyword_key(item) for item in sources}
        if not target or not source_keys:
            return {
                'source_keywords': sources,
                'target_keyword': target,
                'affected_articles': 0,
                'affected_keyword_fields': {},
                'samples': [],
            }

        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    affected_articles = 0
                    samples = []
                    cursor.execute("""
                        SELECT id, title, matched_keywords, matched_keywords_raw
                        FROM articles
                        WHERE COALESCE(status, 'active') = 'active'
                          AND matched_keywords IS NOT NULL
                          AND TRIM(matched_keywords) != ''
                    """)
                    for row in cursor.fetchall():
                        article = dict(row)
                        keywords = parse_keyword_text(article.get('matched_keywords_raw')) or parse_keyword_text(article.get('matched_keywords'))
                        if any(keyword_key(keyword) in source_keys for keyword in keywords):
                            affected_articles += 1
                            if len(samples) < 10:
                                samples.append({
                                    'id': article.get('id'),
                                    'title': article.get('title'),
                                    'matched_keywords': article.get('matched_keywords'),
                                })

                    affected_keyword_fields = {}
                    for table_name in ('scheduled_tasks', 'crawl_tasks', 'managed_urls'):
                        rows = self._iter_keyword_field_rows(cursor, table_name)
                        affected = 0
                        for row in rows:
                            keywords = parse_keyword_text(row.get('keywords'))
                            if any(keyword_key(keyword) in source_keys for keyword in keywords):
                                affected += 1
                        affected_keyword_fields[table_name] = affected

                    return {
                        'source_keywords': sources,
                        'target_keyword': target,
                        'affected_articles': affected_articles,
                        'affected_keyword_fields': affected_keyword_fields,
                        'samples': samples,
                    }
                finally:
                    cursor.close()
        except Exception as e:
            print(f"❌ 预览关键词合并失败: {e}")
            return {
                'source_keywords': sources,
                'target_keyword': target,
                'affected_articles': 0,
                'affected_keyword_fields': {},
                'samples': [],
                'error': str(e),
            }

    def _upsert_keyword_canonical_rule(self, cursor, source_keyword: str, target_keyword: str, created_by: str = '') -> None:
        cursor.execute("""
            SELECT id
            FROM keyword_canonical_rules
            WHERE lower(source_keyword) = lower(?)
              AND status = 'active'
            LIMIT 1
        """, (source_keyword,))
        existing = cursor.fetchone()
        if existing:
            cursor.execute("""
                UPDATE keyword_canonical_rules
                SET canonical_keyword = ?,
                    created_by = CASE WHEN ? != '' THEN ? ELSE created_by END,
                    updated_at = datetime('now', 'localtime')
                WHERE id = ?
            """, (target_keyword, created_by or '', created_by or '', existing['id']))
        else:
            cursor.execute("""
                INSERT INTO keyword_canonical_rules (
                    source_keyword, canonical_keyword, status, created_by, created_at, updated_at
                ) VALUES (?, ?, 'active', ?, datetime('now', 'localtime'), datetime('now', 'localtime'))
            """, (source_keyword, target_keyword, created_by or ''))

    def merge_keywords(
        self,
        source_keywords: List[str],
        target_keyword: str,
        apply_to_tasks: bool = True,
        created_by: str = ''
    ) -> Dict:
        """Persist canonical merge rules and sync task keyword fields."""
        from keyword_governance import KeywordGovernance, clean_keyword, keyword_key

        preview = self.preview_keyword_merge(source_keywords, target_keyword)
        if preview.get('error'):
            return {'success': False, 'error': preview['error'], 'preview': preview}

        target = preview['target_keyword']
        sources = preview['source_keywords']
        if not target or not sources:
            return {'success': False, 'error': 'source_keywords 和 target_keyword 不能为空'}

        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    for source in sources:
                        self._upsert_keyword_canonical_rule(cursor, source, target, created_by)

                    updated_fields = {}
                    if apply_to_tasks:
                        governance = KeywordGovernance(self.connection)
                        governance.reload()
                        for table_name in ('scheduled_tasks', 'crawl_tasks', 'managed_urls'):
                            rows = self._iter_keyword_field_rows(cursor, table_name)
                            updated = 0
                            for row in rows:
                                before = row.get('keywords') or ''
                                after = governance.apply_keyword_rules_to_task_keyword_text(before)
                                if after != before:
                                    id_column = 'task_id' if table_name == 'crawl_tasks' else 'id'
                                    cursor.execute(
                                        f"UPDATE {table_name} SET keywords = ?, updated_at = datetime('now', 'localtime') WHERE {id_column} = ?",
                                        (after, row.get('row_id'))
                                    )
                                    updated += 1
                            updated_fields[table_name] = updated

                    payload = {
                        'source_keywords': sources,
                        'target_keyword': target,
                        'apply_to_tasks': bool(apply_to_tasks),
                    }
                    cursor.execute("""
                        INSERT INTO keyword_operation_logs (
                            operation_type, payload_json, affected_articles, affected_tasks,
                            affected_ragflow_docs, status, created_by, created_at
                        ) VALUES (?, ?, ?, ?, 0, 'completed', ?, datetime('now', 'localtime'))
                    """, (
                        'merge',
                        json.dumps(payload, ensure_ascii=False),
                        preview.get('affected_articles', 0),
                        sum(updated_fields.values()) if updated_fields else 0,
                        created_by or '',
                    ))
                    self.connection.commit()
                    return {
                        'success': True,
                        'source_keywords': sources,
                        'target_keyword': target,
                        'preview': preview,
                        'updated_keyword_fields': updated_fields,
                    }
                except Exception:
                    self.connection.rollback()
                    raise
                finally:
                    cursor.close()
        except Exception as e:
            print(f"❌ 合并关键词失败: {e}")
            return {'success': False, 'error': str(e), 'preview': preview}

    def set_keyword_node_state(self, keyword: str, state: str = 'hidden', reason: str = '', created_by: str = '') -> Dict:
        """Hide or show a keyword circle without changing crawl rules."""
        from keyword_governance import clean_keyword
        keyword = clean_keyword(keyword)
        if not keyword or state not in ('visible', 'hidden'):
            return {'success': False, 'error': 'invalid keyword/state'}
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    cursor.execute("SELECT id FROM keyword_node_states WHERE keyword = ? LIMIT 1", (keyword,))
                    existing = cursor.fetchone()
                    if existing:
                        cursor.execute("""
                            UPDATE keyword_node_states
                            SET state = ?, reason = ?, created_by = ?, updated_at = datetime('now', 'localtime')
                            WHERE id = ?
                        """, (state, reason or '', created_by or '', existing['id']))
                    else:
                        cursor.execute("""
                            INSERT INTO keyword_node_states(keyword, state, reason, created_by, created_at, updated_at)
                            VALUES (?, ?, ?, ?, datetime('now', 'localtime'), datetime('now', 'localtime'))
                        """, (keyword, state, reason or '', created_by or ''))
                    self.connection.commit()
                    return {'success': True, 'keyword': keyword, 'state': state}
                finally:
                    cursor.close()
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def _keyword_delete_preview_payload(self, cursor, keyword: str) -> Dict:
        from keyword_governance import KeywordGovernance, keyword_key, parse_keyword_text

        governance = KeywordGovernance(self.connection)
        target = governance.normalize_keyword(keyword) or keyword
        target_key = keyword_key(target)

        delete_article_ids = []
        retain_article_ids = []
        samples = []
        cursor.execute("""
            SELECT id, title, matched_keywords, matched_keywords_raw
            FROM articles
            WHERE COALESCE(status, 'active') = 'active'
              AND matched_keywords IS NOT NULL
              AND TRIM(matched_keywords) != ''
        """)
        for row in cursor.fetchall():
            article = dict(row)
            raw_keywords = parse_keyword_text(article.get('matched_keywords_raw')) or parse_keyword_text(article.get('matched_keywords'))
            normalized = [governance.normalize_keyword(item) for item in raw_keywords]
            normalized = [item for item in normalized if item]
            if not any(keyword_key(item) == target_key or keyword_key(raw) == target_key for item, raw in zip(normalized, raw_keywords)):
                continue
            remaining = [item for item in normalized if keyword_key(item) != target_key]
            if remaining:
                retain_article_ids.append(article['id'])
            else:
                delete_article_ids.append(article['id'])
                if len(samples) < 10:
                    samples.append({
                        'id': article['id'],
                        'title': article.get('title'),
                        'matched_keywords': article.get('matched_keywords'),
                    })

        ragflow_docs = []
        if delete_article_ids:
            placeholders = ','.join(['?'] * len(delete_article_ids))
            cursor.execute(f"""
                SELECT article_id, kb_id, document_id, document_name, sync_status
                FROM article_ragflow_documents
                WHERE article_id IN ({placeholders})
                  AND document_id IS NOT NULL
                  AND TRIM(document_id) != ''
                  AND COALESCE(sync_status, '') NOT IN ('deleted', 'delete_failed')
            """, delete_article_ids)
            ragflow_docs = [dict(row) for row in cursor.fetchall()]

        affected_keyword_fields = {}
        for table_name in ('scheduled_tasks', 'crawl_tasks', 'managed_urls'):
            rows = self._iter_keyword_field_rows(cursor, table_name)
            affected = 0
            for row in rows:
                keywords = parse_keyword_text(row.get('keywords'))
                normalized = [governance.normalize_keyword(item) or item for item in keywords]
                if any(keyword_key(item) == target_key for item in normalized):
                    affected += 1
            affected_keyword_fields[table_name] = affected

        return {
            'keyword': target,
            'delete_article_ids': delete_article_ids,
            'retain_article_ids': retain_article_ids,
            'affected_articles_delete': len(delete_article_ids),
            'affected_articles_retain': len(retain_article_ids),
            'affected_keyword_fields': affected_keyword_fields,
            'ragflow_documents': ragflow_docs,
            'ragflow_document_count': len(ragflow_docs),
            'samples': samples,
        }

    def create_keyword_delete_preview(self, keyword: str, created_by: str = '') -> Dict:
        """Create a pending delete job and return a confirm token."""
        from keyword_governance import clean_keyword
        keyword = clean_keyword(keyword)
        if not keyword:
            return {'success': False, 'error': 'keyword 不能为空'}
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    preview = self._keyword_delete_preview_payload(cursor, keyword)
                    token_source = f"{preview['keyword']}:{preview['affected_articles_delete']}:{preview['ragflow_document_count']}:{get_china_time().isoformat()}"
                    confirm_token = hashlib.sha256(token_source.encode('utf-8')).hexdigest()[:16]
                    cursor.execute("""
                        INSERT INTO keyword_delete_jobs(
                            keyword, confirm_token, payload_json, status, ragflow_delete_status,
                            created_by, created_at, updated_at
                        ) VALUES (?, ?, ?, 'pending', 'pending', ?, datetime('now', 'localtime'), datetime('now', 'localtime'))
                    """, (
                        preview['keyword'],
                        confirm_token,
                        json.dumps(preview, ensure_ascii=False),
                        created_by or '',
                    ))
                    job_id = cursor.lastrowid
                    self.connection.commit()
                    return {'success': True, 'job_id': job_id, 'confirm_token': confirm_token, 'preview': preview}
                finally:
                    cursor.close()
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def execute_keyword_delete_job(self, job_id: int, confirm_token: str, created_by: str = '') -> Dict:
        """Apply keyword blocklist, sync keyword fields, and soft-delete affected local articles."""
        from keyword_governance import KeywordGovernance

        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    cursor.execute("""
                        SELECT *
                        FROM keyword_delete_jobs
                        WHERE id = ? AND confirm_token = ? AND status = 'pending'
                        LIMIT 1
                    """, (coerce_int(job_id, 0), confirm_token or ''))
                    job = cursor.fetchone()
                    if not job:
                        return {'success': False, 'error': '删除任务不存在、已执行或确认 token 不正确'}
                    payload = json.loads(job['payload_json'] or '{}')
                    keyword = payload.get('keyword') or job['keyword']

                    cursor.execute("""
                        UPDATE keyword_delete_jobs
                        SET status = 'running', updated_at = datetime('now', 'localtime')
                        WHERE id = ?
                    """, (job['id'],))

                    cursor.execute("""
                        SELECT id FROM keyword_blocklist
                        WHERE lower(keyword) = lower(?) AND status = 'active'
                        LIMIT 1
                    """, (keyword,))
                    if not cursor.fetchone():
                        cursor.execute("""
                            INSERT INTO keyword_blocklist(keyword, reason, status, created_by, created_at, updated_at)
                            VALUES (?, 'keyword graph delete', 'active', ?, datetime('now', 'localtime'), datetime('now', 'localtime'))
                        """, (keyword, created_by or ''))

                    governance = KeywordGovernance(self.connection)
                    governance.reload()
                    updated_fields = {}
                    disabled_tasks = 0
                    for table_name in ('scheduled_tasks', 'crawl_tasks', 'managed_urls'):
                        rows = self._iter_keyword_field_rows(cursor, table_name)
                        updated = 0
                        for row in rows:
                            before = row.get('keywords') or ''
                            after = governance.apply_keyword_rules_to_task_keyword_text(before)
                            if after != before:
                                id_column = 'task_id' if table_name == 'crawl_tasks' else 'id'
                                cursor.execute(
                                    f"UPDATE {table_name} SET keywords = ?, updated_at = datetime('now', 'localtime') WHERE {id_column} = ?",
                                    (after, row.get('row_id'))
                                )
                                updated += 1
                                if not after and table_name in ('scheduled_tasks', 'managed_urls'):
                                    cursor.execute(
                                        f"UPDATE {table_name} SET is_active = FALSE, updated_at = datetime('now', 'localtime') WHERE {id_column} = ?",
                                        (row.get('row_id'),)
                                    )
                                    disabled_tasks += 1
                        updated_fields[table_name] = updated

                    article_ids = payload.get('delete_article_ids') or []
                    if article_ids:
                        placeholders = ','.join(['?'] * len(article_ids))
                        cursor.execute(
                            f"UPDATE articles SET status = 'deleted', updated_at = datetime('now', 'localtime') WHERE id IN ({placeholders})",
                            article_ids
                        )

                    cursor.execute("""
                        UPDATE keyword_delete_jobs
                        SET status = 'completed',
                            updated_at = datetime('now', 'localtime')
                        WHERE id = ?
                    """, (job['id'],))
                    cursor.execute("""
                        INSERT INTO keyword_operation_logs(
                            operation_type, payload_json, affected_articles, affected_tasks,
                            affected_ragflow_docs, status, created_by, created_at
                        ) VALUES ('delete', ?, ?, ?, ?, 'completed', ?, datetime('now', 'localtime'))
                    """, (
                        json.dumps({'job_id': job['id'], 'keyword': keyword}, ensure_ascii=False),
                        len(article_ids),
                        sum(updated_fields.values()),
                        len(payload.get('ragflow_documents') or []),
                        created_by or '',
                    ))
                    self.connection.commit()
                    return {
                        'success': True,
                        'job_id': job['id'],
                        'keyword': keyword,
                        'soft_deleted_articles': len(article_ids),
                        'updated_keyword_fields': updated_fields,
                        'disabled_empty_keyword_tasks': disabled_tasks,
                        'ragflow_documents': payload.get('ragflow_documents') or [],
                    }
                except Exception:
                    self.connection.rollback()
                    raise
                finally:
                    cursor.close()
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def update_keyword_delete_job_status(self, job_id: int, status: str, ragflow_delete_status: str = None, error_message: str = '') -> bool:
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    if ragflow_delete_status is None:
                        cursor.execute("""
                            UPDATE keyword_delete_jobs
                            SET status = ?, error_message = ?, updated_at = datetime('now', 'localtime')
                            WHERE id = ?
                        """, (status, error_message or '', coerce_int(job_id, 0)))
                    else:
                        cursor.execute("""
                            UPDATE keyword_delete_jobs
                            SET status = ?, ragflow_delete_status = ?, error_message = ?, updated_at = datetime('now', 'localtime')
                            WHERE id = ?
                        """, (status, ragflow_delete_status, error_message or '', coerce_int(job_id, 0)))
                    self.connection.commit()
                    return cursor.rowcount > 0
                finally:
                    cursor.close()
        except Exception as e:
            print(f"⚠️ 更新关键词删除 job 状态失败: {e}")
            return False

    def _table_exists(self, cursor, table_name: str) -> bool:
        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        return cursor.fetchone() is not None

    def _migrate_existing_schema(self, cursor) -> None:
        """Apply lightweight migrations when connecting to an existing database."""
        if self._table_exists(cursor, 'managed_urls'):
            self._ensure_column_exists(cursor, 'managed_urls', 'access_status', 'TEXT')
            self._ensure_column_exists(cursor, 'managed_urls', 'access_checked_at', 'TIMESTAMP')
            self._ensure_column_exists(cursor, 'managed_urls', 'access_status_code', 'INTEGER')
            self._ensure_column_exists(cursor, 'managed_urls', 'access_error', 'TEXT')
            try:
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_urls_access_status ON managed_urls(access_status)")
            except Exception as exc:
                print(f"⚠️ 创建 access_status 索引失败: {exc}")

        if not self._table_exists(cursor, 'articles'):
            return
        self._ensure_column_exists(cursor, 'articles', 'matched_keywords', 'TEXT')
        self._ensure_column_exists(cursor, 'articles', 'matched_keywords_raw', 'TEXT')
        self._ensure_column_exists(cursor, 'articles', 'keyword_match_detail', 'TEXT')
        self._ensure_column_exists(cursor, 'articles', 'crawler_engine_used', 'TEXT')
        self._ensure_column_exists(cursor, 'articles', 'crawler_engines', 'TEXT')
        self._ensure_column_exists(cursor, 'articles', 'crawler_attempts', 'INTEGER DEFAULT 0')
        self._ensure_column_exists(cursor, 'articles', 'fallback_trigger_reason', 'TEXT')
        self._ensure_column_exists(cursor, 'articles', 'source_method', 'TEXT')
        self._ensure_column_exists(cursor, 'articles', 'configured_url', 'TEXT')
        self._ensure_column_exists(cursor, 'articles', 'resolved_target_url', 'TEXT')
        self._ensure_column_exists(cursor, 'articles', 'canonical_url', 'TEXT')
        self._ensure_column_exists(cursor, 'articles', 'source_task_id', 'TEXT')
        self._ensure_column_exists(cursor, 'articles', 'source_task_name', 'TEXT')
        self._ensure_column_exists(cursor, 'articles', 'published_at_utc', 'TEXT')
        self._ensure_column_exists(cursor, 'articles', 'published_timezone', 'TEXT')
        self._ensure_column_exists(cursor, 'articles', 'published_precision', 'TEXT')
        self._ensure_column_exists(cursor, 'articles', 'published_time_source', 'TEXT')
        # 🔥 阶段1：正文统一 Markdown —— content_markdown 仅展示用；raw_content 保存原始正文快照
        self._ensure_article_markdown_columns(cursor)
        # 🔥 阶段3：实时动态 HTML 链接转换缓存表
        from dynamic_link_converter import ensure_dynamic_converted_table
        ensure_dynamic_converted_table(cursor)
        # 🔥 阶段4：信源检查一键学习（AutoScraper）模型表
        from site_scraper_models import ensure_site_scraper_models_table
        ensure_site_scraper_models_table(cursor)
        self._ensure_crawl_attempts_table(cursor)
        self._ensure_smart_target_urls_table(cursor)
        self._ensure_keyword_governance_tables(cursor)
        from article_spacetime_storage import _ensure_article_spacetime_profiles_table
        _ensure_article_spacetime_profiles_table(cursor)
        from intel_schema import ensure_intel_core_tables
        ensure_intel_core_tables(cursor)
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_articles_source_task_id ON articles(source_task_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_articles_canonical_url ON articles(canonical_url)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_articles_crawler_engine ON articles(crawler_engine_used)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_spacetime_article_id ON article_spacetime_profiles(article_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_spacetime_time_value ON article_spacetime_profiles(time_value)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_spacetime_location ON article_spacetime_profiles(location_name)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_spacetime_status ON article_spacetime_profiles(spacetime_status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_crawl_attempts_task_id ON crawl_attempts(task_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_crawl_attempts_engine ON crawl_attempts(crawler_engine)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_crawl_attempts_started_at ON crawl_attempts(started_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_smart_targets_schedule_id ON smart_target_urls(schedule_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_smart_targets_status ON smart_target_urls(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_smart_targets_expires_at ON smart_target_urls(expires_at)")
        except Exception as exc:
            print(f"⚠️ 创建聚合追踪索引失败: {exc}")






    
    # ==================== 聚合任务相关方法 ====================
    
    def insert_crawl_task(self, task_data: Dict) -> Optional[int]:
        """插入聚合任务"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    # 手动设置中国时间，确保时区正确
                    china_time = get_china_time().strftime('%Y-%m-%d %H:%M:%S')
                    
                    insert_sql = """
                    INSERT INTO crawl_tasks (
                        task_id, target_url, task_name, crawl_depth, crawl_mode,
                        page_limit, incremental_mode, keywords, industry_pack_id,
                        industry_pack_version_id, activation_id, ownership_type,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                    runtime_rows = self.connection.execute(
                        """
                        SELECT setting_key, setting_value FROM intel_runtime_settings
                        WHERE setting_key IN (
                            'active_industry_pack_id',
                            'active_industry_pack_version_id',
                            'active_industry_activation_id'
                        )
                        """
                    ).fetchall()
                    runtime = {str(row[0]): str(row[1]) for row in runtime_rows}
                    
                    values = (
                        task_data.get('task_id', ''),
                        normalize_task_url(task_data.get('target_url', '')),
                        task_data.get('task_name', ''),
                        task_data.get('crawl_depth', 1),
                        task_data.get('crawl_mode', 'standard'),
                        task_data.get('page_limit', 50),
                        task_data.get('incremental_mode', False),
                        task_data.get('keywords', ''),
                        task_data.get('industry_pack_id') or runtime.get('active_industry_pack_id', ''),
                        task_data.get('industry_pack_version_id') or runtime.get('active_industry_pack_version_id') or None,
                        task_data.get('activation_id') or runtime.get('active_industry_activation_id', ''),
                        task_data.get('ownership_type') or 'protected_manual',
                        task_data.get('status', 'pending'),
                        china_time,
                        china_time
                    )
                    
                    cursor.execute(insert_sql, values)
                    task_db_id = cursor.lastrowid
                    self.connection.commit()
                    
                    print(f"✅ 聚合任务入库成功: {task_data.get('task_name', task_data.get('task_id', ''))} (ID: {task_db_id})")
                    return task_db_id
                finally:
                    cursor.close()
                
        except sqlite3.IntegrityError:
            print(f"⚠️ 任务ID已存在: {task_data.get('task_id', '')}")
            return None
        except Exception as e:
            print(f"❌ 插入聚合任务失败: {e}")
            return None
    
    def update_crawl_task_status(self, task_id: str, status: str, 
                                 progress: int = None, articles_found: int = None,
                                 articles_processed: int = None, error_message: str = None):
        """更新聚合任务状态"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    update_parts = ["status = ?", "updated_at = datetime('now', 'localtime')"]
                    values = [status]
                    
                    if progress is not None:
                        update_parts.append("progress = ?")
                        values.append(progress)
                    
                    if articles_found is not None:
                        update_parts.append("articles_found = ?")
                        values.append(articles_found)
                    
                    if articles_processed is not None:
                        update_parts.append("articles_processed = ?")
                        values.append(articles_processed)
                    
                    if error_message is not None:
                        update_parts.append("error_message = ?")
                        values.append(error_message)
                    
                    if status == 'running' and not any('started_at' in p for p in update_parts):
                        update_parts.append("started_at = datetime('now', 'localtime')")
                    
                    if status in ['completed', 'failed', 'cancelled']:
                        update_parts.append("completed_at = datetime('now', 'localtime')")
                    
                    update_sql = f"""
                    UPDATE crawl_tasks SET {', '.join(update_parts)}
                    WHERE task_id = ?
                    """
                    values.append(task_id)
                    
                    cursor.execute(update_sql, values)
                    self.connection.commit()
                    return True
                finally:
                    cursor.close()
                
        except Exception as e:
            print(f"❌ 更新聚合任务状态失败: {e}")
            return False

    def reset_crawl_task_for_retry(
        self,
        task_id: str,
        target_url: str = None,
        task_name: str = None,
        crawl_depth: int = None,
        crawl_mode: str = 'article_crawl',
        page_limit: int = None,
        incremental_mode: bool = None,
        keywords: str = None,
    ) -> bool:
        """Reset an existing crawl task row so retry reuses the original task_id."""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    cursor.execute("SELECT * FROM crawl_tasks WHERE task_id = ?", (task_id,))
                    row = cursor.fetchone()
                    existing = dict(row) if row else None
                    normalized_url = normalize_task_url(target_url or (existing or {}).get('target_url', ''))
                    china_time = get_china_time().strftime('%Y-%m-%d %H:%M:%S')

                    if existing:
                        update_sql = """
                        UPDATE crawl_tasks SET
                            target_url = COALESCE(NULLIF(?, ''), target_url),
                            task_name = COALESCE(NULLIF(?, ''), task_name),
                            crawl_depth = COALESCE(?, crawl_depth),
                            crawl_mode = COALESCE(NULLIF(?, ''), crawl_mode),
                            page_limit = COALESCE(?, page_limit),
                            incremental_mode = COALESCE(?, incremental_mode),
                            keywords = COALESCE(?, keywords),
                            status = 'pending',
                            progress = 0,
                            articles_found = 0,
                            articles_processed = 0,
                            started_at = NULL,
                            completed_at = NULL,
                            error_message = NULL,
                            updated_at = ?
                        WHERE task_id = ?
                        """
                        cursor.execute(
                            update_sql,
                            (
                                normalized_url,
                                task_name or '',
                                crawl_depth,
                                crawl_mode or '',
                                page_limit,
                                None if incremental_mode is None else int(bool(incremental_mode)),
                                keywords,
                                china_time,
                                task_id,
                            ),
                        )
                    else:
                        runtime_rows = self.connection.execute(
                            """
                            SELECT setting_key, setting_value FROM intel_runtime_settings
                            WHERE setting_key IN (
                                'active_industry_pack_id',
                                'active_industry_pack_version_id',
                                'active_industry_activation_id'
                            )
                            """
                        ).fetchall()
                        runtime = {str(item[0]): str(item[1]) for item in runtime_rows}
                        insert_sql = """
                        INSERT INTO crawl_tasks (
                            task_id, target_url, task_name, crawl_depth, crawl_mode,
                            page_limit, incremental_mode, keywords, industry_pack_id,
                            industry_pack_version_id, activation_id, ownership_type,
                            status, progress,
                            articles_found, articles_processed, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'protected_manual',
                                  'pending', 0, 0, 0, ?, ?)
                        """
                        cursor.execute(
                            insert_sql,
                            (
                                task_id,
                                normalized_url,
                                task_name or f'重试任务-{normalized_url}',
                                crawl_depth or 1,
                                crawl_mode or 'article_crawl',
                                page_limit or 50,
                                int(bool(incremental_mode)),
                                keywords or '',
                                runtime.get('active_industry_pack_id', ''),
                                runtime.get('active_industry_pack_version_id') or None,
                                runtime.get('active_industry_activation_id', ''),
                                china_time,
                                china_time,
                            ),
                        )

                    self.connection.commit()
                    return True
                finally:
                    cursor.close()
        except Exception as e:
            print(f"❌ 重置聚合任务重试状态失败: {e}")
            return False
    
    def get_crawl_tasks(self, page: int = 1, per_page: int = 20,
                       status: str = None,
                       industry_pack_id: str = None) -> Tuple[List[Dict], int]:
        """获取聚合任务列表"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    where_conditions = []
                    params = []
                    
                    if status:
                        where_conditions.append("status = ?")
                        params.append(status)
                    if industry_pack_id:
                        where_conditions.append(
                            "COALESCE(NULLIF(TRIM(industry_pack_id), ''), 'family_office') = ?"
                        )
                        params.append(str(industry_pack_id))
                    
                    where_clause = " AND ".join(where_conditions) if where_conditions else "1=1"
                    
                    # 获取总数
                    count_sql = f"SELECT COUNT(*) as total FROM crawl_tasks WHERE {where_clause}"
                    cursor.execute(count_sql, params)
                    total = cursor.fetchone()['total']
                    
                    # 获取任务列表（附带managed_urls中文名）
                    offset = (page - 1) * per_page
                    select_sql = f"""
                    SELECT ct.*,
                        (SELECT name FROM managed_urls
                         WHERE REPLACE(REPLACE(ct.target_url,'https://',''),'http://','')
                               LIKE REPLACE(REPLACE(url,'https://',''),'http://','') || '%'
                         ORDER BY LENGTH(url) DESC LIMIT 1) as url_display_name
                    FROM crawl_tasks ct
                    WHERE {where_clause}
                    ORDER BY ct.created_at DESC
                    LIMIT ? OFFSET ?
                    """
                    params.extend([per_page, offset])
                    
                    cursor.execute(select_sql, params)
                    tasks = [dict(row) for row in cursor.fetchall()]
                    
                    return tasks, total
                finally:
                    cursor.close()
                
        except Exception as e:
            print(f"❌ 获取聚合任务列表失败: {e}")
            return [], 0
    
    def get_crawl_task_by_task_id(self, task_id: str) -> Optional[Dict]:
        """根据task_id获取聚合任务"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    select_sql = "SELECT * FROM crawl_tasks WHERE task_id = ?"
                    cursor.execute(select_sql, (task_id,))
                    task = cursor.fetchone()
                    return dict(task) if task else None
                finally:
                    cursor.close()
        except Exception as e:
            print(f"❌ 获取聚合任务失败: {e}")
            return None
    
    # ==================== 任务执行历史相关方法 ====================
    
    def insert_task_execution(self, execution_data: Dict) -> Optional[int]:
        """插入任务执行历史"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    # 手动设置中国时间，确保时区正确
                    china_time = get_china_time().strftime('%Y-%m-%d %H:%M:%S')
                    
                    insert_sql = """
                    INSERT INTO task_execution_history (
                        schedule_id, task_id, status, run_key, scheduled_for, started_at, completed_at,
                        duration_seconds, articles_found, error_message, result_summary, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                    
                    values = (
                        execution_data.get('schedule_id'),
                        execution_data.get('task_id'),
                        execution_data.get('status', 'running'),
                        execution_data.get('run_key'),
                        execution_data.get('scheduled_for'),
                        execution_data.get('started_at', china_time),
                        execution_data.get('completed_at'),
                        execution_data.get('duration_seconds'),
                        execution_data.get('articles_found', 0),
                        execution_data.get('error_message'),
                        json.dumps(execution_data.get('result_summary', {})),
                        china_time
                    )
                    
                    cursor.execute(insert_sql, values)
                    execution_id = cursor.lastrowid
                    self.connection.commit()
                    
                    return execution_id
                finally:
                    cursor.close()
                
        except Exception as e:
            print(f"❌ 插入任务执行历史失败: {e}")
            return None

    def get_task_executions(self, page: int = 1, per_page: int = 20, 
                           status: str = None, schedule_id: str = None) -> Tuple[List[Dict], int]:
        """获取任务执行记录列表"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    # 构建WHERE条件
                    where_conditions = []
                    params = []
                    
                    if status:
                        where_conditions.append("status = ?")
                        params.append(status)
                    
                    if schedule_id:
                        where_conditions.append("schedule_id = ?")
                        params.append(schedule_id)
                    
                    where_clause = ""
                    if where_conditions:
                        where_clause = f"WHERE {' AND '.join(where_conditions)}"
                    
                    # 获取总数
                    count_sql = f"SELECT COUNT(*) FROM task_execution_history {where_clause}"
                    cursor.execute(count_sql, params)
                    total = cursor.fetchone()[0]
                    
                    # 获取分页数据（关联scheduled_tasks表获取任务名称）
                    offset = (page - 1) * per_page
                    select_sql = f"""
                    SELECT 
                        teh.id, teh.schedule_id, teh.task_id, teh.status, teh.started_at, teh.completed_at,
                        teh.duration_seconds, teh.articles_found, teh.error_message, teh.result_summary,
                        teh.created_at,
                        st.task_name
                    FROM task_execution_history teh
                    LEFT JOIN scheduled_tasks st ON teh.schedule_id = st.id
                    {where_clause}
                    ORDER BY teh.created_at DESC 
                    LIMIT ? OFFSET ?
                    """
                    
                    params.extend([per_page, offset])
                    cursor.execute(select_sql, params)
                    
                    executions = []
                    for row in cursor.fetchall():
                        # 安全解析JSON，处理可能的格式错误
                        result_summary = {}
                        if row[9]:
                            try:
                                parsed = json.loads(row[9])
                                # 确保解析结果是dict
                                if isinstance(parsed, dict):
                                    result_summary = parsed
                                else:
                                    result_summary = {}
                            except (json.JSONDecodeError, TypeError, ValueError) as e:
                                print(f"⚠️ 解析result_summary失败 (ID: {row[0]}): {e}, 原始数据: {row[9][:100] if row[9] else 'None'}")
                                result_summary = {}
                        
                        # 优先使用result_summary中的消息，然后是error_message
                        message = result_summary.get('message', '') if isinstance(result_summary, dict) else ''
                        if not message:
                            message = row[8] or f"任务执行状态: {row[3]}"
                        
                        execution = {
                            'id': str(row[0]),
                            'schedule_id': str(row[1]) if row[1] else None,
                            'task_id': row[2],
                            'task_name': row[11] or '-',  # 从scheduled_tasks表获取的任务名称
                            'status': row[3],
                            'started_at': row[4],
                            'completed_at': row[5],
                            'duration_seconds': row[6],
                            'articles_found': row[7],
                            'error_message': row[8],
                            'result_summary': result_summary,
                            'created_at': row[10],
                            'updated_at': row[5] or row[10],  # 使用completed_at或created_at作为updated_at
                            'message': message  # 使用result_summary.message或error_message或默认消息
                        }
                        executions.append(execution)
                    
                    return executions, total
                finally:
                    cursor.close()
                
        except Exception as e:
            print(f"❌ 获取任务执行记录失败: {e}")
            return [], 0

    def update_task_execution(self, execution_id: str, update_data: Dict) -> bool:
        """更新任务执行记录"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    # 构建更新字段
                    update_fields = []
                    params = []
                    
                    for field in ['task_id', 'status', 'run_key', 'scheduled_for', 'started_at',
                                  'completed_at', 'duration_seconds', 'articles_found',
                                  'error_message', 'result_summary']:
                        if field in update_data:
                            update_fields.append(f"{field} = ?")
                            if field == 'result_summary':
                                params.append(json.dumps(update_data[field]))
                            else:
                                params.append(update_data[field])
                    
                    if not update_fields:
                        return True
                    
                    params.append(int(execution_id))
                    
                    update_sql = f"""
                    UPDATE task_execution_history 
                    SET {', '.join(update_fields)}
                    WHERE id = ?
                    """
                    
                    cursor.execute(update_sql, params)
                    self.connection.commit()
                    
                    return cursor.rowcount > 0
                finally:
                    cursor.close()
                
        except Exception as e:
            print(f"❌ 更新任务执行记录失败: {e}")
            return False

    def delete_task_execution(self, execution_id: str) -> bool:
        """删除单个任务执行记录"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    # 先检查记录是否存在
                    check_sql = "SELECT id FROM task_execution_history WHERE id = ?"
                    cursor.execute(check_sql, (int(execution_id),))
                    exists = cursor.fetchone()
                    
                    print(f"🔍 检查记录 {execution_id} 是否存在: {exists is not None}")
                    
                    if not exists:
                        print(f"⚠️ 记录 {execution_id} 不存在")
                        return False
                    
                    # 执行删除
                    delete_sql = "DELETE FROM task_execution_history WHERE id = ?"
                    cursor.execute(delete_sql, (int(execution_id),))
                    self.connection.commit()
                    
                    deleted_count = cursor.rowcount
                    print(f"🗑️ 删除了 {deleted_count} 条记录")
                    
                    return deleted_count > 0
                finally:
                    cursor.close()
                
        except Exception as e:
            print(f"❌ 删除任务执行记录失败: {e}")
            return False

    def clear_task_executions(self) -> bool:
        """清空所有任务执行记录"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    delete_sql = "DELETE FROM task_execution_history"
                    cursor.execute(delete_sql)
                    self.connection.commit()
                    return True
                finally:
                    cursor.close()
                
        except Exception as e:
            print(f"❌ 清空任务执行记录失败: {e}")
            return False

    def delete_crawl_task_by_task_id(self, task_id: str) -> bool:
        """通过task_id删除聚合任务记录"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    # 先检查记录是否存在
                    check_sql = "SELECT task_id FROM crawl_tasks WHERE task_id = ?"
                    cursor.execute(check_sql, (task_id,))
                    exists = cursor.fetchone()
                    
                    print(f"🔍 检查聚合任务 {task_id} 是否存在: {exists is not None}")
                    
                    if not exists:
                        print(f"⚠️ 聚合任务 {task_id} 不存在")
                        return True  # 不存在也算成功
                    
                    # 执行删除
                    delete_sql = "DELETE FROM crawl_tasks WHERE task_id = ?"
                    cursor.execute(delete_sql, (task_id,))
                    self.connection.commit()
                    
                    deleted_count = cursor.rowcount
                    print(f"🗑️ 删除了 {deleted_count} 条聚合任务记录")
                    
                    return deleted_count > 0
                finally:
                    cursor.close()
                
        except Exception as e:
            print(f"❌ 删除聚合任务记录失败: {e}")
            return False
    
    def get_all_auth_configs(self) -> list:
        """获取所有认证配置"""
        try:
            self._ensure_connection()
            with self.lock:
                cursor = self.connection.cursor()
                try:
                    cursor.execute("SELECT * FROM auth_configs WHERE is_active = TRUE")
                    configs = []
                    for row in cursor.fetchall():
                        configs.append(dict(row))
                    return configs
                finally:
                    cursor.close()
        except Exception as e:
            print(f"❌ 获取认证配置失败: {e}")
            return []


















# 存储子模块方法代理：被拆出到 chat_storage / article_spacetime_storage /
# task_run_storage 的方法仍以 sqlite_db.<name>(...) 形式可调用（零行为变化）。
from chat_storage import PROXY_METHODS as _CHAT_PROXY
from article_spacetime_storage import PROXY_METHODS as _SPACETIME_PROXY
from task_run_storage import PROXY_METHODS as _TASKRUN_PROXY
_STORAGE_PROXY = {}
_STORAGE_PROXY.update(_CHAT_PROXY)
_STORAGE_PROXY.update(_SPACETIME_PROXY)
_STORAGE_PROXY.update(_TASKRUN_PROXY)


# 全局数据库实例
sqlite_db = SQLiteDatabase()

from article_identity import (  # 兼容旧引用，保持 sqlite_database.xxx 可访问
    _ARTICLE_RETENTION_DAYS, _WATERMARK_HIGH_GAP_DAYS, _WATERMARK_MAX_GAP_DAYS,
    _is_implausible_future_date, _is_stale_publish_date, _normalize_article_url,
    _source_crawl_gap_days,
)