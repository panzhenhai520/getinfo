#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""主题搜索词测试：当场搜一轮 → 派发聚合 → 观察入库进度。

运营在「内部主题」里调搜索词时，原先只能"存草稿 → 发布新版本 → 等下一轮采集"
才知道词配得对不对。这里把反馈环缩短成三步：

  1. preview  用草稿里的词当场搜一轮，分两组返回（上组 Google、下组 Tavily；
              界面不显示引擎名，只用位置区分）
  2. crawl    把勾选的结果派发成候选，走与正式采集完全相同的门禁→抓正文→分类→归主题链路
  3. status   按扫描批次回读每个候选的进度

每个主题最多测试 TOPIC_SEARCH_TEST_LIMIT 次。
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Dict, List, Optional

import config
from industry_pack_admin import IndustryPackAdminService, industry_pack_version_store
from industry_packs import industry_pack_loader
from intel_candidates import intel_candidate_repository
from intel_contracts import utc_now, utc_text
from intel_database import intel_repository
from serpapi_client import SerpAPIClient
from tavily_client import TavilyClient
from utils import coerce_int

# 每个主题可用的测试次数：调词是高频动作，但不能无限烧搜索额度
TOPIC_SEARCH_TEST_LIMIT = 5
# 单次派发的上限，防止一次勾选把聚合队列打满
MAX_DISPATCH_ITEMS = 40
# 测试预览时 SerpAPI 的时间窗：正式采集用 SERPAPI_RECENCY_DAYS(默认 3 天)做增量，
# 但"调试搜索词"要看完整覆盖。注意不能用 90 天——Google 的 cdr 自定义日期范围
# 配合 lang_zh-CN 会把 90 天窗口判成"无结果"(SerpAPI 报 hasn't returned any results)。
# 传 0 = 不加时间窗，返回最全结果。
TOPIC_TEST_SERPAPI_RECENCY_DAYS = 0

# 分组顺序即界面顺序：索引 0 渲染在上、1 渲染在下
_GROUP_ENGINES = ("serpapi", "tavily")


class TopicSearchTestError(ValueError):
    """可直接回给前端的测试请求错误。"""


def _normalize_terms(value) -> List[str]:
    """统一成 [词1, 词2, 词3]，兼容旧的单字符串格式。"""
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _text(value, limit: int = 0) -> str:
    result = str(value or "").strip()
    return result[:limit] if limit else result


def _classify_crawl_error(error: str, extraction_method: str = "") -> str:
    """把候选的失败原因归入前端可直接展示的状态桶。"""
    text = str(error or "").strip()
    method = str(extraction_method or "").strip().lower()
    vpn_used = "vpn" in method or "ocr" in method
    if not text and not vpn_used:
        return ""
    # 网络层连不上（站点不可达/被墙）
    if any(key in text for key in ("Max retries", "HTTPSConnectionPool", "不可达", "站点不可达", "Connection")):
        return "unreachable"
    # 走了 VPN/OCR 且明确失败（未取回正文）
    if vpn_used and ("VPN" in text or "OCR" in text or "兜底" in text or "空正文" in text):
        return "vpn_fail"
    # 门禁拒（能到这里说明正文已取回，无论是否走 VPN）
    if "未成功入库" in text or ("已提取" in text and "未" in text):
        return "ingest_fail"
    if "未命中行业锚点词" in text:
        return "anchor"
    if "content_too_short" in text or "质量未达标" in text or "太短" in text:
        return "short"
    if "栏目" in text or "列表" in text or "导航页" in text:
        return "listing"
    if "lease expired" in text or "派发中断" in text:
        return "stale"
    # 走了 VPN 但结局未明
    if vpn_used:
        return "vpn"
    return "other"


class TopicSearchTestService:
    def __init__(
        self,
        *,
        serpapi_client=None,
        tavily_client=None,
        repository=None,
        admin_service=None,
    ):
        self.serpapi = serpapi_client or SerpAPIClient()
        self.tavily = tavily_client or TavilyClient()
        self.repository = repository or intel_candidate_repository
        self.db = self.repository.db
        self.admin = admin_service or IndustryPackAdminService(
            industry_pack_version_store,
            industry_pack_loader,
        )
        self._table_ready = False

    # ── 基础设施 ────────────────────────────────────────────
    def _ensure_table(self) -> None:
        """幂等建表：连接建立后只跑一次。"""
        if self._table_ready:
            return
        from intel_schema import ensure_intel_topic_search_test_tables

        self.repository._ensure()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                ensure_intel_topic_search_test_tables(cursor)
            finally:
                cursor.close()
        self._table_ready = True

    def _fetch_all(self, sql: str, params) -> List[Dict]:
        self._ensure_table()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(sql, params)
                return [dict(row) for row in cursor.fetchall()]
            finally:
                cursor.close()

    # ── 测试额度 ────────────────────────────────────────────
    def usage(self, industry_pack_id: str, topic_key: str) -> Dict:
        rows = self._fetch_all(
            "SELECT COUNT(*) AS used FROM intel_topic_search_tests "
            "WHERE industry_pack_id=? AND topic_key=?",
            (industry_pack_id, topic_key),
        )
        used = int(rows[0].get("used") or 0) if rows else 0
        return {
            "used": used,
            "limit": TOPIC_SEARCH_TEST_LIMIT,
            "remaining": max(0, TOPIC_SEARCH_TEST_LIMIT - used),
        }

    def _record_test(
        self,
        industry_pack_id: str,
        topic_key: str,
        query_text: str,
        groups: List[Dict],
        *,
        created_by: str = "",
    ) -> None:
        self._ensure_table()
        google_count = len(groups[0]["results"]) if len(groups) > 0 else 0
        tavily_count = len(groups[1]["results"]) if len(groups) > 1 else 0
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO intel_topic_search_tests (
                        industry_pack_id, topic_key, query_text,
                        google_count, tavily_count, created_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        industry_pack_id,
                        topic_key,
                        query_text,
                        google_count,
                        tavily_count,
                        created_by,
                        utc_text(),
                    ),
                )
            finally:
                cursor.close()

    # ── 草稿里的搜索词 ──────────────────────────────────────
    def _draft_terms(self, industry_pack_id: str, topic_key: str) -> List[str]:
        """从草稿读该主题的搜索词——测试必须用"正在编辑、尚未发布"的词才有意义。"""
        try:
            draft = self.admin.get_or_create_draft(industry_pack_id)
        except Exception:  # noqa: BLE001
            return []
        manifest = (draft or {}).get("manifest") or {}
        mapping = manifest.get("topic_search_queries") or {}
        if not isinstance(mapping, dict):
            return []
        return _normalize_terms(mapping.get(topic_key))

    # ── 搜索预览 ────────────────────────────────────────────
    def _run_engine(self, engine: str, query: str) -> Dict:
        # 来源可能是"没配密钥"，也可能只是"整个来源被关掉了"。两者都必须如实回给运营：
        # 否则界面只会显示"没有结果"，运营会误判成自己写的搜索词不行。
        if engine == "serpapi":
            # SerpAPIClient.search 自己会因 SERPAPI_ENABLED=False 直接返回空列表；
            # 先判一次，否则界面会把"这个来源被关掉了"显示成"搜索词没结果"。
            if not bool(getattr(config, "SERPAPI_ENABLED", False)):
                return {"results": [], "error": "该来源未启用（SERPAPI_ENABLED=False）"}
            if not self.serpapi.configured:
                return {"results": [], "error": "该来源未配置搜索密钥"}
            client = self.serpapi
        else:
            # TavilyClient.search 只看密钥是否配置，不受 TAVILY_ENABLED 影响，此处保持一致
            if not self.tavily.configured:
                return {"results": [], "error": "该来源未配置搜索密钥"}
            client = self.tavily
        try:
            if engine == "serpapi":
                items = client.search(
                    query, recency_days=TOPIC_TEST_SERPAPI_RECENCY_DAYS
                ) or []
            else:
                items = client.search(query) or []
        except Exception as exc:  # noqa: BLE001
            return {"results": [], "error": _text(exc, 200)}
        results = []
        for item in items:
            url = _text(item.get("url"))
            if not url:
                continue
            results.append(
                {
                    "url": url,
                    "title": _text(item.get("title"), 300),
                    "summary": _text(
                        item.get("summary") or item.get("snippet"), 150
                    ),
                    "published_at": _text(item.get("published_at"), 40),
                    "engine": engine,
                }
            )
        return {"results": results, "error": ""}

    def preview(
        self,
        industry_pack_id: str,
        topic_key: str,
        *,
        terms=None,
        created_by: str = "",
    ) -> Dict:
        pack_id = _text(industry_pack_id)
        key = _text(topic_key)
        if not pack_id or not key:
            raise TopicSearchTestError("缺少行业包或主题 key")

        status = self.usage(pack_id, key)
        if status["remaining"] <= 0:
            raise TopicSearchTestError(
                f"这个主题的测试次数已用完（共 {TOPIC_SEARCH_TEST_LIMIT} 次）"
            )

        # 优先用前端传来的（用户可能刚改了还没保存），否则回落到草稿里的词
        resolved = _normalize_terms(terms) or self._draft_terms(pack_id, key)
        if not resolved:
            raise TopicSearchTestError("这个主题还没填搜索词，先填一个再测试")
        query = " ".join(resolved)

        groups = [self._run_engine(engine, query) for engine in _GROUP_ENGINES]
        self._record_test(pack_id, key, query, groups, created_by=created_by)

        return {
            "topic_key": key,
            "query": query,
            "terms": resolved,
            "usage": self.usage(pack_id, key),
            "groups": groups,
        }

    # ── 派发聚合 ────────────────────────────────────────────
    def _reactivate_candidate(self, candidate_id: int) -> None:
        """把废弃/失败的候选重置为可派发，供重复测试时再次聚合。

        discover 对已存在候选的"重新激活"要求 activation_id 非空且变化，测试场景
        不满足，会导致之前标记为 discarded 的候选永远复用旧状态、不再派发。
        """
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    """
                    UPDATE intel_candidates
                    SET status='queued', attempt_count=0, next_retry_at=NULL,
                        lease_owner=NULL, lease_expires_at=NULL, last_error='',
                        updated_at=?
                    WHERE id=? AND status IN ('discarded', 'failed', 'retry_wait')
                    """,
                    (utc_text(), candidate_id),
                )
            finally:
                cursor.close()

    def dispatch_crawl(
        self,
        industry_pack_id: str,
        topic_key: str,
        *,
        query_text: str = "",
        items=None,
        created_by: str = "",
    ) -> Dict:
        pack_id = _text(industry_pack_id)
        key = _text(topic_key)
        if not pack_id or not key:
            raise TopicSearchTestError("缺少行业包或主题 key")
        rows = items if isinstance(items, list) else []
        if not rows:
            raise TopicSearchTestError("先勾选要聚合的结果")
        # 两个引擎可能搜到同一个 URL，派发前按 URL 去重，避免重复候选、重复聚合
        seen_urls = set()
        deduped_rows = []
        for row in rows:
            url = _text((row or {}).get("url"))
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            deduped_rows.append(row)
        rows = deduped_rows
        if not rows:
            raise TopicSearchTestError("勾选的结果没有有效链接")
        if len(rows) > MAX_DISPATCH_ITEMS:
            raise TopicSearchTestError(f"单次最多派发 {MAX_DISPATCH_ITEMS} 条")
        query = _text(query_text, 500)

        # 每次测试独立一个扫描批次，前端按 run_id 回读这批候选的进度。
        # scanner_type/observation_type 复用正式采集已在用的枚举值（serpapi/tavily），
        # 是否"测试批次"由 metadata.origin 表达，避免依赖部署库的 CHECK 约束。
        run_id = int(
            self.repository.create_scan_run(
                source_id=None,
                industry_pack_id=pack_id,
                scanner_type="serpapi",
                metadata={
                    "origin": "topic_search_test",
                    "topic_key": key,
                    "query_text": query,
                },
                scan_window_key=f"topic-test:{pack_id}:{key}:{uuid.uuid4().hex}",
                requested_pack_ids=[pack_id],
            )
            or 0
        )

        candidate_ids: List[int] = []
        skipped = 0
        for row in rows:
            row = row if isinstance(row, dict) else {}
            url = _text(row.get("url"))
            if not url:
                skipped += 1
                continue
            engine = _text(row.get("engine"))
            observation_type = engine if engine in _GROUP_ENGINES else "serpapi"
            try:
                result = self.repository.discover(
                    {
                        "url": url,
                        "title": _text(row.get("title"), 1000),
                        "summary": _text(row.get("summary"), 5000),
                        "published_at": _text(row.get("published_at"), 40),
                    },
                    industry_pack_id=pack_id,
                    source_id=None,
                    scan_run_id=run_id,
                    observation_type=observation_type,
                    query_text=query,
                    # 搜索发现是即时结果，摘要里通常没有可靠发布时间，
                    # 时效判定推迟到抓正文之后（与正式搜索采集一致）。
                    bypass_industry_gate=True,
                )
            except Exception:  # noqa: BLE001
                skipped += 1
                continue
            candidate_id = coerce_int(result.get("candidate_id"), None, 1)
            if candidate_id:
                # 之前被废弃/失败的候选（如清理残留时标记的），重新激活为可派发，
                # 否则重复测试同一搜索词时会一直复用旧状态、永远不再爬。
                self._reactivate_candidate(int(candidate_id))
                candidate_ids.append(int(candidate_id))
            else:
                skipped += 1

        job_id = 0
        if candidate_ids:
            job_id, _created = intel_repository.enqueue_job(
                "candidate_dispatch",
                f"topic-search-test:{pack_id}:{key}:{uuid.uuid4().hex}",
                {
                    "candidate_ids": candidate_ids,
                    "manual": True,
                    "industry_pack_id": pack_id,
                    "origin": "topic_search_test",
                    "topic_key": key,
                    # 显式置空：enqueue 会用 setdefault 注入当前激活版本 id，
                    # 而测试候选不绑定激活版本，若不置空，worker 派发时会按
                    # activation_id 过滤，导致候选一个都领不到、永远"排队"。
                    "activation_id": "",
                },
                request_id="",
                created_by=created_by,
            )
            # 聚合交给独立情报 worker 消费（本地与生产都是独立 worker），
            # 不再在 Web 进程内起线程：Web 一重启就会中断聚合，造成大量"派发中断"。

        return {
            "scan_run_id": run_id,
            "candidate_ids": candidate_ids,
            "dispatched": len(candidate_ids),
            "skipped": skipped,
            "job_id": int(job_id or 0),
        }

    # ── 聚合进度 ────────────────────────────────────────────
    def _release_stale_dispatch(self, scan_run_id: int, stale_seconds: int = 600) -> int:
        """把派发后长时间未完成的候选标记为失败并释放租约。

        本地/单容器部署下，进程内派发线程可能因 web 重启被强杀，候选会停在
        dispatching（租约未释放）。轮询进度时顺带回收这些"僵尸"，让数据恢复干净，
        不再出现永远"聚合中"的假象。
        """
        cutoff = utc_text(utc_now() - timedelta(seconds=stale_seconds))
        now_text = utc_text()
        self.repository._ensure()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    """
                    UPDATE intel_candidates
                    SET status='failed', lease_owner=NULL, lease_expires_at=NULL,
                        last_error=CASE WHEN last_error=''
                            THEN '派发中断，未完成' ELSE last_error END,
                        updated_at=?
                    WHERE id IN (
                        SELECT o.candidate_id FROM intel_candidate_observations o
                        WHERE o.scan_run_id=?
                    )
                      AND status='dispatching'
                      AND updated_at <= ?
                    """,
                    (now_text, scan_run_id, cutoff),
                )
                released = int(getattr(cursor, "rowcount", 0) or 0)
            finally:
                cursor.close()
        return released

    def _extraction_methods(self, candidate_ids) -> Dict[int, str]:
        """批量取每个候选最新一次提取的策略（strategy=vpn_ocr 等）。"""
        if not candidate_ids:
            return {}
        self.repository._ensure()
        placeholders = ",".join("?" for _ in candidate_ids)
        rows = self._fetch_all(
            f"""
            SELECT ea.candidate_id AS candidate_id, ea.strategy AS strategy
            FROM intel_extraction_attempts ea
            WHERE ea.candidate_id IN ({placeholders})
            ORDER BY ea.id
            """,
            list(candidate_ids),
        )
        result: Dict[int, str] = {}
        for row in rows:
            result[int(row.get("candidate_id") or 0)] = _text(row.get("strategy"))
        return result

    def _article_topics(self, article_ids) -> Dict[int, str]:
        """批量取文章归属的主题（一个文章可归多个主题，用顿号连接）。"""
        if not article_ids:
            return {}
        self.repository._ensure()
        placeholders = ",".join("?" for _ in article_ids)
        rows = self._fetch_all(
            f"""
            SELECT ta.article_id AS article_id, t.topic_name AS topic_name
            FROM intel_topic_articles ta
            JOIN intel_topics t ON t.id = ta.topic_id
            WHERE ta.article_id IN ({placeholders})
            ORDER BY ta.article_id, ta.association_score DESC
            """,
            list(article_ids),
        )
        grouped: Dict[int, List[str]] = {}
        for row in rows:
            aid = int(row.get("article_id") or 0)
            name = _text(row.get("topic_name"))
            if name and name not in grouped.setdefault(aid, []):
                grouped[aid].append(name)
        return {aid: "、".join(names) for aid, names in grouped.items()}

    def crawl_status(self, scan_run_id) -> Dict:
        run_id = coerce_int(scan_run_id, 0, 1)
        if not run_id:
            raise TopicSearchTestError("缺少扫描批次号")
        # 轮询进度前先回收"派发后失联"的候选，避免永远停在"聚合中"
        self._release_stale_dispatch(run_id)
        rows = self._fetch_all(
            """
            SELECT c.id AS candidate_id, c.original_url AS url, c.title AS title,
                   c.status AS status, c.article_id AS article_id,
                   c.last_error AS last_error, c.attempt_count AS attempt_count,
                   o.observation_type AS observation_type
            FROM intel_candidate_observations o
            JOIN intel_candidates c ON c.id = o.candidate_id
            WHERE o.scan_run_id = ?
            ORDER BY c.id
            """,
            (run_id,),
        )
        # 同一候选可能被两个引擎各记一条 observation，按 candidate_id 去重，
        # 否则进度列表里同一个 URL 会出现两次。
        seen_ids = set()
        deduped_rows = []
        for row in rows:
            cid = int(row.get("candidate_id") or 0)
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            deduped_rows.append(row)
        rows = deduped_rows
        candidate_ids = [int(row.get("candidate_id") or 0) for row in rows]
        extraction_map = self._extraction_methods(candidate_ids)

        items = []
        kind_counts: Dict[str, int] = {}
        for row in rows:
            error = _text(row.get("last_error"), 300)
            method = extraction_map.get(int(row.get("candidate_id") or 0), "")
            kind = _classify_crawl_error(error, method)
            items.append(
                {
                    "candidate_id": int(row.get("candidate_id") or 0),
                    "url": _text(row.get("url")),
                    "title": _text(row.get("title"), 300),
                    "status": _text(row.get("status")),
                    "article_id": int(row.get("article_id") or 0),
                    "error": error,
                    "attempts": int(row.get("attempt_count") or 0),
                    "engine": _text(row.get("observation_type")),
                    "extraction_method": method,
                    "error_kind": kind,
                }
            )
            if kind:
                kind_counts[kind] = kind_counts.get(kind, 0) + 1
        # 已入库的文章 → 归属主题（跟踪"进到哪个主题"）
        topic_map = self._article_topics(
            [item["article_id"] for item in items if item["article_id"]]
        )
        for item in items:
            item["topic"] = topic_map.get(item["article_id"], "")
        # 状态口径与候选状态机保持一致：crawled=已入库，其余按进行中/失败归类
        queued = sum(1 for item in items if item["status"] in ("discovered", "queued"))
        running = sum(1 for item in items if item["status"] == "dispatching")
        crawled = sum(1 for item in items if item["status"] == "crawled")
        failed = sum(
            1
            for item in items
            if item["status"] in ("failed", "discarded", "retry_wait")
        )
        return {
            "scan_run_id": run_id,
            "items": items,
            "total": len(items),
            "queued": queued,
            "running": running,
            "crawled": crawled,
            "failed": failed,
            "finished": bool(items) and (queued + running) == 0,
            "kinds": kind_counts,
        }

    # ── 关闭窗口清理 ────────────────────────────────────────
    def cleanup(self, scan_run_id) -> Dict:
        """关闭测试窗口时清理该批次：未入库的候选标记废弃、释放租约。"""
        run_id = coerce_int(scan_run_id, 0, 1)
        if not run_id:
            raise TopicSearchTestError("缺少扫描批次号")
        self.repository._ensure()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    """
                    UPDATE intel_candidates
                    SET status='discarded', lease_owner=NULL, lease_expires_at=NULL,
                        next_retry_at=NULL, updated_at=?
                    WHERE id IN (
                        SELECT o.candidate_id FROM intel_candidate_observations o
                        WHERE o.scan_run_id=?
                    ) AND status NOT IN ('crawled')
                    """,
                    (utc_text(), run_id),
                )
                cleaned = int(getattr(cursor, "rowcount", 0) or 0)
            finally:
                cursor.close()
        return {"scan_run_id": run_id, "cleaned": cleaned}

    # ── 重置额度（调试用，前端不暴露）────────────────────────
    def reset_usage(self, industry_pack_id: str, topic_key: str) -> int:
        self._ensure_table()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    "DELETE FROM intel_topic_search_tests "
                    "WHERE industry_pack_id=? AND topic_key=?",
                    (industry_pack_id, topic_key),
                )
                deleted = int(getattr(cursor, "rowcount", 0) or 0)
            finally:
                cursor.close()
        return deleted


topic_search_test_service = TopicSearchTestService()

__all__ = [
    "TOPIC_SEARCH_TEST_LIMIT",
    "MAX_DISPATCH_ITEMS",
    "TopicSearchTestError",
    "TopicSearchTestService",
    "topic_search_test_service",
]
