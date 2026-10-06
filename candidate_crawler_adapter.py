#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Adapter from an intelligence candidate to the existing article crawler."""

from __future__ import annotations

from typing import Callable, Dict, Optional

import os
import re
import socket
import time
import config
import requests
from intel_candidates import IntelCandidateRepository, quick_score_candidate
from industry_packs import industry_pack_loader
from intel_http import SafeHTTPClient, sanitize_external_error, validate_external_url
from intel_content_quality_gate import assess_article_quality
from intel_admission import assess_admission
from intel_url_expansion import expand_links
from sqlite_database import sqlite_db


# 列表页/聚合页 URL 模式：这类 URL 不是单篇文章，抓回来正文会是"标题条目列表"。
# tag/category/author/search/分页/hotrank(汽车之家热榜) 是业界公认的聚合页路径，命中即拒绝，不抓取。
# 例外：以文章扩展名结尾的 URL 是文章页（如 leiphone.com/category/academic/xxx.html），放行。
_LISTING_PATH_RE = re.compile(
    r"/(tag|tags|category|categories|author|authors|search|hotrank)(/|$)",
    re.IGNORECASE,
)
_LISTING_PAGE_RE = re.compile(r"/page/\d+(/|$)", re.IGNORECASE)
_LISTING_QUERY_RE = re.compile(r"(^|[&?])(p|page)=\d+", re.IGNORECASE)
_ARTICLE_EXT_RE = re.compile(r"\.(html?|php|aspx?|jspx?|shtml?)([?#]|$)", re.IGNORECASE)


def looks_like_listing_url(url: str) -> bool:
    """URL 是否为列表/聚合页（tag、栏目、作者、搜索、分页）。"""
    if not url:
        return False
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(str(url))
    except Exception:
        return False
    path = parts.path or ""
    if _ARTICLE_EXT_RE.search(path):
        return False
    return bool(
        _LISTING_PATH_RE.search(path)
        or _LISTING_PAGE_RE.search(path)
        or _LISTING_QUERY_RE.search(parts.query)
    )


def _record_anti_bot_backoff(candidate: Dict, error_text: str, db=None) -> None:
    """T4.3 反爬降频：命中反爬信号 → 域名退避窗口 +1 档（指数递增）；
    绝不做任何绕过，只降频等待。失败不阻塞主流程。"""
    try:
        from urllib.parse import urlparse
        from crawl_policy import classify_anti_bot, record_backoff
        if not classify_anti_bot(error_text=error_text):
            return
        domain = urlparse(str((candidate or {}).get("original_url") or "")).netloc
        if domain:
            record_backoff(domain, 403, db=db)
    except Exception:
        pass


def _record_anti_bot_success(url: str, db=None) -> None:
    """成功抓取 → 清空该域名退避计数（站点恢复）。失败不阻塞主流程。"""
    try:
        from urllib.parse import urlparse
        from crawl_policy import record_backoff_success
        domain = urlparse(str(url or "")).netloc
        if domain:
            record_backoff_success(domain, db=db)
    except Exception:
        pass


def _is_url_reachable(url: str, *, timeout: float = 6.0) -> bool:
    """可复用的可达性判断：网址"真的能打开"（服务器有响应）才返回 True。

    用于分流逻辑——"网址打不开，就没必要送 VPN"。
    - 能拿到 HTTP 响应（含 4xx/5xx，说明服务器可达、可能只是反爬/慢）→ True
    - 真正不可达（DNS 解析失败，域名不存在）→ False
    - 反爬断开/重置/超时/TLS 被拦（服务器在线但拒绝本客户端指纹）→ True，
      必须送 VPN 真浏览器重试，否则反爬站点会被永久跳过兜底。
    """
    if not url:
        return False
    try:
        requests.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        return True
    except requests.exceptions.ConnectionError as exc:
        # "服务器接受连接后立刻断开"（RemoteDisconnected / ConnectionReset）
        # 恰恰说明服务器在线、只是拒绝了本客户端的 TLS 指纹 → 送 VPN。
        # 只有异常链里出现 DNS 解析失败（socket.gaierror）才是真正不可达。
        cause = exc
        while cause is not None:
            if isinstance(cause, socket.gaierror):
                return False
            cause = cause.__cause__ or cause.__context__
        return True
    except (requests.exceptions.Timeout, requests.exceptions.SSLError):
        # 连接/读取超时、TLS 层被拦，同样可能是反爬或站点较慢，
        # VPN 有真浏览器指纹与更长超时，值得送一次而不是直接判死。
        return True
    except Exception:
        return False


# 厂商站常见的"栏目/列表导航页"：这些是产品/方案/技术文章的栏目索引，不是单篇文章。
# 聚合很容易把"产品中心/解决方案/技术文章"这类栏目页当文章爬进来，标题就成了栏目名。
_LISTING_URL_RE = re.compile(
    r"(?i)(/product(?:s)?/category|/product(?:s)?/all|/category/all|/news/category"
    r"|/solution(?:s)?$|/development$|/technology$|/column$|/channel$)",
)

# 通用栏目名：把整页标题误当成栏目名的兜底列表（title 用这些词且正文也短 -> 判定为栏目/列表页）。
_COLUMN_TITLES = {
    "技术文章", "解决方案", "产品中心", "产品", "产品与服务", "首页", "index", "home",
    "关于我们", "了解我们", "新闻中心", "新闻动态", "新闻资讯", "发展历程", "服务",
    "服务与支持", "技术支持", "下载中心", "资料中心", "市场活动", "案例", "客户案例",
    "合作伙伴", "走进我们", "走进", "公司简介", "中心", "列表", "资讯",
}


def _looks_like_listing_page(url: str, title: str, content: str) -> bool:
    """判断是否为"栏目/列表导航页"而非正文文章。

    用两种信号叠加：URL 命中栏目索引路径（强信号），或标题是通用栏目名且正文很短
    （弱信号，用于没有规范 URL 的站点）。命中则不应作为单篇文章收录。
    """
    u = str(url or "").strip()
    if _LISTING_URL_RE.search(u):
        return True
    t = str(title or "").strip().casefold()
    if t and t in {x.casefold() for x in _COLUMN_TITLES}:
        # 栏目名命中的同时，正文若明显偏短（列表页通常没正文）则判为列表页。
        if len(str(content or "").strip()) < 3000:
            return True
    return False


def _derive_real_title(title: str, content: str, url: str = "") -> str:
    """标题是通用栏目名/过短时，从正文关键句兜底出一个更像"文章标题"的标题。

    优先取正文第一句；若该句太长则截取前 ~28 字。标题像英文/无明显栏目名时保持原样。
    """
    t = str(title or "").strip()
    # 标题看起来正常（不是栏目名、长度 > 2 且有真实含义）就直接用。
    if t and len(t) > 2 and t.casefold() not in {x.casefold() for x in _COLUMN_TITLES}:
        return t
    body = str(content or "").strip()
    if not body:
        return t or url or "无标题"
    import re as _re
    # 跳过“概述/简介/前言”等节标题与“本文/原标题”等前缀，取第一句实质内容。
    _heads = ("概述", "简介", "前言", "导语", "正文", "摘要", "公司简介", "企业简介", "引言")
    raw = _re.sub(r"\s+", " ", body.replace("\r", " ").replace("\n", "\n")).strip()
    frags = [f.strip() for f in _re.split(r"[。！？!?；;]", raw) if f.strip()]
    first = ""
    for f in frags:
        if f in _heads:
            continue
        for pfx in ("本文", "文章", "原标题：", "标题：", "简介："):
            if f.startswith(pfx):
                f = f[len(pfx):].strip()
                break
        if f:
            first = f
            break
    if not first:
        first = raw[:60].strip()
    if len(first) > 40:
        first = first[:28].rstrip("，,、 ") + "…"
    return first if first else (t or url or "无标题")


class CandidateCrawlerAdapter:
    def __init__(
        self,
        *,
        database=None,
        candidate_repository: IntelCandidateRepository = None,
        extractor_factory: Optional[Callable] = None,
        url_validator=validate_external_url,
        redirect_validator: Optional[Callable] = None,
    ):
        self.db = database or sqlite_db
        self.candidates = candidate_repository or IntelCandidateRepository(self.db)
        self.extractor_factory = extractor_factory
        self.url_validator = url_validator
        self.redirect_validator = redirect_validator or SafeHTTPClient().validate_redirect_chain

    @staticmethod
    def task_id(candidate_id: int) -> str:
        return f"intel_candidate_{int(candidate_id)}"

    def _ensure_task(self, candidate: Dict) -> str:
        crawler_task_id = self.task_id(candidate["id"])
        existing = self.db.get_crawl_task_by_task_id(crawler_task_id)
        if not existing:
            keywords = ",".join(
                self.candidates.get_candidate_industry_keywords(int(candidate["id"]))
            )
            created = self.db.insert_crawl_task(
                {
                    "task_id": crawler_task_id,
                    "target_url": candidate["original_url"],
                    "task_name": f"intel candidate {candidate['id']}",
                    "crawl_depth": 1,
                    "crawl_mode": "article",
                    "page_limit": 1,
                    "incremental_mode": True,
                    "keywords": keywords,
                    "status": "pending",
                }
            )
            if not created and not self.db.get_crawl_task_by_task_id(crawler_task_id):
                raise RuntimeError("无法创建 crawler 任务")
        return crawler_task_id

    def _extractor(self):
        if self.extractor_factory:
            return self.extractor_factory(self.db)
        from article_link_extractor import ArticleLinkExtractor

        return ArticleLinkExtractor(db=self.db, enable_smart_validation=False)

    def _enrich_article_remotely(
        self,
        article_id: int,
        *,
        url: str,
        title: str,
        content: str,
        keywords: list[str],
        task_id: str,
        industry_pack_id: str = "",
    ) -> Dict:
        """纯加工：改为**入队 enrich 任务**（异步、不阻塞聚合线程）。

        聚合线程只 enqueue 一个 ``enrich`` job 即返回；由 worker 的 ``_handle_enrich``
        在独立槽位 submit+wait 拿回 refined/translated/audio_manifest 并落库。

        industry_pack_id 必须是候选真正被准入的行业包：VPN 端分层提炼用它做相关性
        锚定，缺省时会被 enqueue_job 盖上当前激活行业的上下文（可能与本候选无关），
        导致精炼空返回（B/C 类兜底）或内容锚定错误。
        """
        if not (config.REMOTE_PIPELINE_ENRICH or config.REMOTE_PIPELINE_TTS):
            return {"enabled": False}
        try:
            from intel_database import IntelRepository

            repo = IntelRepository(self.db)
            dedupe_key = f"enrich:{int(article_id)}"
            job_id, created = repo.enqueue_job(
                "enrich",
                dedupe_key,
                {
                    "article_id": int(article_id),
                    "url": str(url or ""),
                    "title": str(title or ""),
                    "content": str(content or ""),
                    "keywords": (
                        list(keywords) if isinstance(keywords, (list, tuple)) else []
                    ),
                    "task_id": str(task_id or ""),
                    # 显式带上候选真实行业包，避免 _stamp_active_job_context 用
                    # 当前激活行业覆盖（enrich 锚定错包 → VPN 精炼空返回）。
                    "industry_pack_id": str(industry_pack_id or ""),
                },
            )
            return {
                "enabled": True,
                "queued": True,
                "remote_job_id": job_id,
                "created": created,
            }
        except Exception as exc:
            return {"enabled": True, "attached": False, "error": str(exc)}

    def _ensure_vpn_trace_schema(self) -> None:
        """确保 VPN 兜底追踪表存在（candidate → 是否送 VPN → 渠道 → VPN job → 结果）。

        必须用 connection.executescript：postgres_compat 只对 executescript 路径做
        DDL 翻译（INTEGER PRIMARY KEY AUTOINCREMENT → BIGSERIAL PRIMARY KEY），
        直接 cursor.execute 会把 SQLite 方言原样发给 PostgreSQL 导致建表失败。
        """
        self.db._ensure_connection()
        with self.db.lock:
            self.db.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS intel_vpn_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id BIGINT NOT NULL DEFAULT 0,
                    url TEXT NOT NULL DEFAULT '',
                    channel TEXT NOT NULL DEFAULT '',
                    vpn_job_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    content_length INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_vpn_attempts_candidate
                    ON intel_vpn_attempts(candidate_id);
                """
            )
            self.db.connection.commit()

    def _record_vpn_attempt(
        self,
        candidate_id: int,
        url: str,
        *,
        channel: str,
        status: str,
        vpn_job_id: str = "",
        content_length: int = 0,
        error: str = "",
    ) -> None:
        """记录一次 VPN 兜底尝试，让测试聚合期间能追踪"送没送 VPN / 看没看到 / 入没入库"。"""
        try:
            self._ensure_vpn_trace_schema()
            from intel_candidates import utc_text
            with self.db.lock:
                cur = self.db.connection.cursor()
                try:
                    cur.execute(
                        """INSERT INTO intel_vpn_attempts
                           (candidate_id, url, channel, vpn_job_id, status,
                            content_length, error, created_at)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (
                            int(candidate_id or 0),
                            str(url or ""),
                            str(channel or ""),
                            str(vpn_job_id or ""),
                            str(status or ""),
                            max(0, int(content_length or 0)),
                            str(error or "")[:500],
                            utc_text(),
                        ),
                    )
                    self.db.connection.commit()
                finally:
                    cur.close()
        except Exception:
            # 追踪失败绝不能反噬正文抓取主流程
            pass

    def _crawl_article_via_vpn(self, url: str, *, task_id: str = "", candidate_id: int = 0) -> Dict:
        """本地抓取被反爬/动态页失败时的兜底：送 VPN。

        先用 crawl4ai 真实浏览器渲染抓正文（质量最好）；若失败（尤其 robots 拒爬/反爬），
        再转 VPN 独立 OCR（`/v1/pipeline/ocr`，不检查 robots）读取正文。只抓取不加工
        （enrich/tts=False），取回正文后仍走本地 staging + /enrich 精炼/语音。
        """
        if not (config.REMOTE_PIPELINE_URL and config.REMOTE_PIPELINE_TOKEN):
            self._record_vpn_attempt(candidate_id, url, channel="crawl4ai", status="failed", error="VPN 未被配置")
            return {"success": False, "permanent": False, "error": "VPN 未被配置"}
        try:
            from remote_pipeline_client import (
                RemotePipelineError,
                RemotePipelineUnavailable,
                remote_pipeline_client,
            )
        except Exception as exc:
            self._record_vpn_attempt(candidate_id, url, channel="crawl4ai", status="failed", error=f"VPN 客户端导入失败: {exc}")
            return {"success": False, "permanent": False, "error": f"VPN 客户端导入失败: {exc}"}

        # 第一优先：crawl4ai 真实浏览器渲染抓正文
        try:
            # task_id 加唯一后缀：remote_pipeline 按 idempotency_key（含 task_id）幂等去重，
            # 相同 candidate 重爬若沿用旧 task_id 会命中旧缓存（旧配置抽到的空壳正文），
            # 导致改配置/重爬不生效。加时间戳后缀让每次重爬都真正触发 VPN 重新渲染。
            _vpn_task_id = f"{task_id}:{time.time_ns()}" if task_id else task_id
            result = remote_pipeline_client.run(
                url=url,
                mode="article",
                keywords=[],
                limit=1,
                task_id=_vpn_task_id,
                enrich=False,
                tts=False,
            )
            vpn_job_id = str((result or {}).get("job_id") or "")
            articles = (result or {}).get("articles") or []
            if articles:
                art = articles[0]
                content = str(art.get("content") or art.get("raw_content") or "").strip()
                if content:
                    self._record_vpn_attempt(
                        candidate_id, url, channel="crawl4ai", status="success",
                        vpn_job_id=vpn_job_id, content_length=len(content),
                    )
                    return {
                        "success": True,
                        "url": str(art.get("url") or url),
                        "title": str(art.get("title") or ""),
                        "content": content,
                        "publish_date": str(art.get("publish_date") or ""),
                        "extraction_method": str(art.get("extraction_method") or "vpn_crawl"),
                        "quality_score": len(content),
                        "permanent": False,
                        "source_method": "vpn_crawl",
                    }
            # run() 无正文（含 robots 拒爬）：落到 OCR 兜底
            self._record_vpn_attempt(
                candidate_id, url, channel="crawl4ai", status="failed",
                vpn_job_id=vpn_job_id, error="crawl4ai 无正文",
            )
        except (RemotePipelineUnavailable, RemotePipelineError) as exc:
            # run() 失败（含 robots 拒爬）：落到 OCR 兜底
            self._record_vpn_attempt(candidate_id, url, channel="crawl4ai", status="failed", error=f"crawl4ai 失败: {exc}")
        except Exception as exc:
            self._record_vpn_attempt(candidate_id, url, channel="crawl4ai", status="failed", error=f"VPN 聚合异常: {exc}")
            return {"success": False, "permanent": False, "error": f"VPN 聚合异常: {exc}"}

        # 第二优先：VPN 独立 OCR（不检查 robots，用于 robots 拒爬/反爬但页面可见）
        try:
            ocr_result = remote_pipeline_client.ocr(url=url)
            ocr_text = str(ocr_result.get("ocr_text") or "").strip()
            ocr_summary = str(ocr_result.get("summary") or "").strip()
            # OCR 截图常把侧栏导航/推荐阅读/评论区等噪声一起识别进来，原始文本很脏。
            # 入库展示内容优先用 VPN 端 LLM 摘要（干净、已纠错），OCR 原文仅作
            # raw_content 备查；后续 enrich 任务仍会用 refined_content 精炼替换。
            content = ocr_summary if len(ocr_summary) >= 100 else ocr_text
            if not content:
                self._record_vpn_attempt(candidate_id, url, channel="ocr", status="failed", error="VPN OCR 返回空正文")
                return {"success": False, "permanent": False, "error": "VPN OCR 返回空正文"}
            self._record_vpn_attempt(
                candidate_id, url, channel="ocr", status="success", content_length=len(content),
            )
            return {
                "success": True,
                "url": str(ocr_result.get("url") or url),
                "title": str(ocr_result.get("title") or ""),
                "content": content,
                "raw_content": ocr_text,
                "publish_date": "",
                "extraction_method": "vpn_ocr",
                "quality_score": len(content),
                "permanent": False,
                "source_method": "vpn_ocr",
            }
        except (RemotePipelineUnavailable, RemotePipelineError) as exc:
            self._record_vpn_attempt(candidate_id, url, channel="ocr", status="failed", error=f"VPN OCR 失败: {exc}")
            return {"success": False, "permanent": False, "error": f"VPN OCR 失败: {exc}"}
        except Exception as exc:
            self._record_vpn_attempt(candidate_id, url, channel="ocr", status="failed", error=f"VPN OCR 异常: {exc}")
            return {"success": False, "permanent": False, "error": f"VPN OCR 异常: {exc}"}

    def _try_vpn_fallback(self, url: str, *, task_id: str = "", candidate_id: int = 0) -> Dict:
        """VPN 兜底前先判可达性（分流逻辑）：
        - 网址真不可达（DNS 解析失败）→ 不送 VPN，直接失败（省资源、避免无谓失败）。
        - 网址"能打开"（服务器有响应，哪怕反爬/断开/超时）→ 送 VPN 真实浏览器渲染抓正文。
        """
        if not _is_url_reachable(url):
            self._record_vpn_attempt(
                candidate_id, url, channel="skipped", status="skipped",
                error="站点不可达（DNS 解析失败），跳过 VPN",
            )
            return {
                "success": False,
                "permanent": True,
                "unreachable": True,
                "error": "站点不可达，跳过VPN",
            }
        return self._crawl_article_via_vpn(url, task_id=task_id, candidate_id=candidate_id)

    def _direct_text_fallback(
        self, url: str, *, title: str, pack_id: str, candidate_id: int = 0
    ) -> Dict:
        """本地抽取失败时的快速兜底：requests 直连拿 HTML → 去标签纯文本 → 锚点校验。

        主抽取器（newspaper3k/BeautifulSoup）对部分中文站点会返回空/壳内容，但 requests
        直连往往能拿到含锚点词的完整 HTML。此兜底把 HTML 降级成纯文本（带噪声但可用），
        只要命中行业锚点词就当作正文入库，救回被误拦的文章。命中不了才继续走 VPN。
        """
        print(f"[direct] cid={candidate_id} pack={pack_id} url={url}", flush=True)
        if not pack_id:
            return {"success": False, "error": "无行业包，跳过直连兜底"}
        try:
            pack = industry_pack_loader.load(pack_id)
        except Exception:
            return {"success": False, "error": "行业包加载失败，跳过直连兜底"}
        try:
            r = requests.get(
                url,
                timeout=config.INTEL_SCAN_READ_TIMEOUT_SECONDS,
                allow_redirects=True,
                headers={
                    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/124.0 Safari/537.36"),
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
            if r.status_code != 200 or not r.text:
                print(f"[direct] cid={candidate_id} HTTP {r.status_code}", flush=True)
                return {"success": False, "error": f"直连 HTTP {r.status_code}"}
            html = r.text
        except Exception as exc:
            print(f"[direct] cid={candidate_id} EXC {type(exc).__name__}: {str(exc)[:120]}", flush=True)
            return {"success": False, "error": f"直连失败: {sanitize_external_error(exc) or str(exc)}"}
        return self._html_to_validated_content(
            url, title=title, html=html, pack=pack, label=f"direct:{candidate_id}"
        )

    def _html_to_validated_content(
        self, url: str, *, title: str, html: str, pack, label: str = ""
    ) -> Dict:
        """阶段5：本地 HTML → 去标签纯文本 → 行业锚点校验（直连与 Scrapling 共用）。

        关键：保留换行（get_text("\n")）。若把整页压成一行，clean_article_markdown
        的"页脚锅炉板/面包屑"等按行规则会把整行（含正文）误删，锚点词随之丢失。
        """
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "noscript", "iframe", "header", "footer", "nav"]):
                tag.decompose()
            text = soup.get_text("\n")
            lines = []
            for ln in text.split("\n"):
                ln = re.sub(r"[ \t\r\f\v]+", " ", ln).strip()
                if ln:
                    lines.append(ln)
            text = "\n".join(lines)
        except Exception:
            text = re.sub(r"<[^>]+>", "\n", html)
            text = re.sub(r"[ \t\r\f\v]+", " ", text)
            lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
            text = "\n".join(lines)
        print(f"[{label}] html={len(html)} text={len(text)}", flush=True)
        if len(text) < 200:
            # JS 渲染空壳页（如 SPA）拿不到正文，不值得作为正文入库
            return {"success": False, "error": "本地 HTML 纯文本过短（疑似 JS 渲染空壳）"}
        # 锚点校验：纯文本必须命中行业锚点词，否则退给下一梯队
        score = quick_score_candidate(title, text, pack)
        anchor_hits = score.get("anchor_hits") or []
        print(f"[{label}] anchor_hits={anchor_hits} score={score.get('score')}", flush=True)
        if not anchor_hits:
            return {"success": False, "error": "本地 HTML 纯文本未命中行业锚点词"}
        return {
            "success": True,
            "url": url,
            "title": title,
            "content": text,
            "publish_date": "",
            "extraction_method": "scrapling_fetch" if label.startswith("scrapling") else "direct_text",
            "quality_score": len(text),
            "permanent": False,
            "source_method": "scrapling_fetch" if label.startswith("scrapling") else "direct_text_fallback",
        }

    def _scrapling_fallback(
        self, url: str, *, title: str, pack_id: str, candidate_id: int = 0,
        task_id: str = "",
    ) -> Dict:
        """阶段5：Scrapling StealthyFetcher 隐身抓取 —— 本地第一梯队（VPN 之前）。

        浏览器级 TLS 指纹 + 隐身补丁，能过相当一部分 requests 拿不到的反爬；
        无正文/失败静默降级到 VPN，绝不阻塞主链路。默认启用，可用环境变量
        CRAWL_SCRAPLING_TIER_ENABLED=0 关闭（回退原直连→VPN 链路）。
        """
        if not pack_id:
            return {"success": False, "error": "无行业包，跳过 Scrapling"}
        if str(os.environ.get("CRAWL_SCRAPLING_TIER_ENABLED", "1")) == "0":
            return {"success": False, "error": "Scrapling 梯队已通过配置关闭"}
        try:
            pack = industry_pack_loader.load(pack_id)
        except Exception:
            return {"success": False, "error": "行业包加载失败，跳过 Scrapling"}
        started = time.time()
        try:
            from scrapling.fetchers import StealthyFetcher
            fetcher = StealthyFetcher()
            # Scrapling 的 timeout 单位是毫秒；15s 超时上限，避免拖慢兜底链路
            _timeout_ms = max(5000, int((config.INTEL_SCAN_READ_TIMEOUT_SECONDS or 15) * 1000))
            page = fetcher.fetch(
                url,
                headless=True,
                network_idle=False,
                timeout=_timeout_ms,
            )
            html = str(getattr(page, "html_content", "") or page or "")
            status = getattr(page, "status", 0)
        except Exception as exc:
            error = f"Scrapling 抓取失败: {sanitize_external_error(exc) or str(exc)}"
            self._record_scrapling_attempt(candidate_id, url, task_id, status="failed", error=error, elapsed=time.time() - started)
            return {"success": False, "error": error}
        if int(status or 0) >= 400 or len(html) < 200:
            error = f"Scrapling 响应不可用（HTTP {status}, html={len(html)}）"
            self._record_scrapling_attempt(candidate_id, url, task_id, status="failed", error=error, elapsed=time.time() - started)
            return {"success": False, "error": error}
        result = self._html_to_validated_content(
            url, title=title, html=html, pack=pack, label=f"scrapling:{candidate_id}"
        )
        self._record_scrapling_attempt(
            candidate_id, url, task_id,
            status="success" if result.get("success") else "failed",
            error=str(result.get("error") or ""),
            elapsed=time.time() - started,
        )
        return result

    def _record_scrapling_attempt(self, candidate_id: int, url: str, task_id: str, *,
                                  status: str, error: str = "", elapsed: float = 0.0):
        """阶段5：Scrapling 梯队尝试审计（供成功率/耗时/VPN 兜底占比统计）。"""
        try:
            sqlite_db.record_crawl_attempt({
                'schedule_id': None,
                'task_id': str(task_id or f"candidate:{candidate_id}"),
                'configured_url': url,
                'resolved_target_url': url,
                'crawler_engine': 'scrapling',
                'phase': 'fallback',
                'status': status,
                'fallback_trigger_reason': 'candidate_dispatch_scrapling_tier',
                'error_message': str(error or "")[:500],
            })
        except Exception as exc:
            print(f"[scrapling] cid={candidate_id} 审计记录失败: {exc}", flush=True)

    def _staging_upsert(self, *, url: str, title: str, raw_content: str,
                        source_method: str, source_task_id: str, source_task_name: str,
                        industry_pack_id: str, matched_keywords: str,
                        status: str = "staging") -> int:
        """写入/更新临时库 article_raw_staging，返回 staging id。"""
        from remote_result_ingestor import ensure_remote_pipeline_schema
        ensure_remote_pipeline_schema(self.db)
        now = "datetime('now')"
        self.db._ensure_connection()
        with self.db.lock:
            cur = self.db.connection.cursor()
            try:
                cur.execute(
                    """INSERT INTO article_raw_staging(
                        url, canonical_url, title, raw_content, source_method,
                        source_task_id, source_task_name, industry_pack_id,
                        matched_keywords, status, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))
                    ON CONFLICT DO NOTHING""",
                    (url, url, title, raw_content, source_method,
                     source_task_id, source_task_name, industry_pack_id,
                     matched_keywords, status),
                )
                cur.execute(
                    "SELECT id FROM article_raw_staging WHERE url=? ORDER BY id DESC LIMIT 1",
                    (url,),
                )
                row = cur.fetchone()
                self.db.connection.commit()
                return int(row[0]) if row else 0
            finally:
                cur.close()

    def _staging_mark(self, staging_id: int, *, status: str, article_id: int = 0, error: str = "") -> None:
        """更新临时库状态（done/failed）。"""
        if not staging_id:
            return
        self.db._ensure_connection()
        with self.db.lock:
            cur = self.db.connection.cursor()
            try:
                cur.execute(
                    "UPDATE article_raw_staging SET status=?, article_id=?, error=?, updated_at=datetime('now') WHERE id=?",
                    (status, int(article_id or 0), str(error or "")[:1000], int(staging_id)),
                )
                self.db.connection.commit()
            finally:
                cur.close()

    def _enqueue_candidate_classification(
        self, candidate_id: int, article_id: int, *, activation_id: str = ""
    ) -> Dict:
        """Queue every pack that admitted the one canonical candidate."""
        pack_ids = self.candidates.get_candidate_industry_pack_ids(
            candidate_id, activation_id=activation_id
        )
        if not pack_ids:
            return {
                "industry_pack_id": "",
                "job_id": 0,
                "created": False,
                "classification_jobs": [],
            }
        from intel_database import IntelRepository

        repository = IntelRepository(self.db)
        jobs = []
        for pack_id in pack_ids:
            job_id, created = repository.enqueue_classification(
                int(article_id),
                pack_id,
                activation_id=activation_id,
                ragflow_upload=True,
            )
            jobs.append(
                {
                    "industry_pack_id": pack_id,
                    "job_id": int(job_id or 0),
                    "created": bool(created),
                }
            )
        return {**jobs[0], "classification_jobs": jobs}

    def _fetch_ggzy_full_body(self, url: str, content: str) -> str:
        """全国公共资源交易平台公告：`a/…/uuid.html` 只是公告头部汇总，正文在 `b/…/uuid.html` 的 `.detail`
        （含中标人、投标报价/金额、项目负责人/资质、被否决单位、招标人/代理机构等完整信息）。

        规则：同 uuid，仅把路径段 `a/` 换成 `b/`。若 `b` 页 `.detail` 内容更长则用它作正文（保留调用方标题）。
        """
        if not url or "/information/deal/html/a/" not in url:
            return content
        b_url = url.replace("/information/deal/html/a/", "/information/deal/html/b/", 1)
        if b_url == url:
            return content
        from bs4 import BeautifulSoup
        from smart_article_extractor import _extract_block_text_preserving_inline, clean_content_element

        # 快速路径：plain requests 取 .detail；ggzy 在连发时可能对 plain requests 返回反爬页（200 但无 .detail）
        try:
            r = requests.get(
                b_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36", "Accept": "text/html,application/xhtml+xml"},
                timeout=25,
                verify=False,
            )
            if r.status_code == 200 and r.text:
                soup = BeautifulSoup(r.text, "html.parser")
                detail = soup.select_one("div.detail") or soup.select_one(".detail")
                if detail is not None:
                    clean_content_element(detail)
                    body = _extract_block_text_preserving_inline(detail)
                    if body and len(body) > len(content):
                        return body
        except Exception:
            pass
        # 回退②：Playwright 真实浏览器渲染 b 页（能过 ggzy 反爬，稳定拿到 .detail 富正文）。
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return content
        _pw = None
        try:
            _pw = sync_playwright().start()
            browser = _pw.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                context = browser.new_context(
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"),
                )
                page = context.new_page()
                page.goto(b_url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
                soup = BeautifulSoup(page.content(), "html.parser")
                detail = soup.select_one("div.detail") or soup.select_one(".detail")
                if detail is not None:
                    clean_content_element(detail)
                    body = _extract_block_text_preserving_inline(detail)
                    if body and len(body) > len(content):
                        return body
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            if _pw is not None:
                try:
                    _pw.stop()
                except Exception:
                    pass
        return content

    def dispatch(self, candidate: Dict) -> Dict:
        """Synchronously reuse the existing detail extractor and exact article linking."""
        candidate_id = int(candidate["id"])
        crawler_task_id = self._ensure_task(candidate)
        # 列表页 URL 直接永久拒绝：tag/栏目/作者/搜索/分页页不是单篇文章，
        # 抓取后正文是"标题条目列表"（如 hit180.com/tag/xxx），必须在此层拦下。
        _candidate_url = str(candidate.get("original_url") or "")
        if looks_like_listing_url(_candidate_url):
            self.db.update_crawl_task_status(
                crawler_task_id,
                "failed",
                error_message=f"URL 为列表/聚合页（tag/栏目/作者/搜索/分页），非单篇文章: {_candidate_url[:160]}",
            )
            return {
                "success": False,
                "outcome": "listing_url_rejected",
                "crawler_task_id": crawler_task_id,
                "error": "URL 为列表/聚合页，非单篇文章",
                "permanent": True,
            }
        existing_article_id = self.candidates.find_article_for_candidate(candidate)
        # 已软删的文章不算"已存在"：继续抓取新内容，否则对已删文章入队分类会报
        # "article not found"（如 hit180 RSS 里的旧 tag 链接命中软删文章）。
        # 注意 get_article_by_id 只返回 active 文章，这里必须用原生 SQL 查 status。
        if existing_article_id:
            _existing_row = None
            try:
                with self.db.lock:
                    cur = self.db.connection.cursor()
                    cur.execute("SELECT status FROM articles WHERE id=?", (int(existing_article_id),))
                    _existing_row = cur.fetchone()
                    cur.close()
            except Exception:
                pass
            if not _existing_row or str(_existing_row["status"] or "") == "deleted":
                existing_article_id = None
        if existing_article_id:
            self.db.link_article_to_task(existing_article_id, crawler_task_id)
            self.db.update_crawl_task_status(
                crawler_task_id,
                "completed",
                progress=100,
                articles_found=1,
                articles_processed=1,
            )
            existing_article = self.db.get_article_by_id(int(existing_article_id)) or {}
            # enrich 锚定必须用候选真实行业包（与 classification 一致），避免被当前
            # 激活行业上下文覆盖 → VPN 精炼空返回。
            _enrich_pack_id = self.candidates.get_candidate_industry_pack_id(
                candidate_id,
                activation_id=str(candidate.get("activation_id") or ""),
            )
            enrichment = self._enrich_article_remotely(
                int(existing_article_id),
                url=str(existing_article.get("url") or candidate.get("original_url") or ""),
                title=str(existing_article.get("title") or candidate.get("title") or ""),
                content=str(existing_article.get("content") or ""),
                keywords=self.candidates.get_candidate_industry_keywords(candidate_id),
                task_id=crawler_task_id,
                industry_pack_id=_enrich_pack_id,
            )
            classification_job = self._enqueue_candidate_classification(
                candidate_id,
                existing_article_id,
                activation_id=str(candidate.get("activation_id") or ""),
            )
            return {
                "success": True,
                "outcome": "existing_article",
                "crawler_task_id": crawler_task_id,
                "article_id": existing_article_id,
                "classification_job": classification_job,
                "enrichment": enrichment,
            }

        try:
            self.url_validator(candidate["original_url"])
            self.redirect_validator(candidate["original_url"])
        except Exception as exc:
            # URL 校验失败（被拒/不可达等）：更新任务为 failed，避免任务滞留 pending。
            _err = sanitize_external_error(exc) or str(exc)[:1000]
            self.db.update_crawl_task_status(crawler_task_id, "failed", error_message=_err)
            return {"success": False, "outcome": "crawler_failed", "crawler_task_id": crawler_task_id,
                    "error": _err, "permanent": True}
        self.db.update_crawl_task_status(crawler_task_id, "running", progress=10)
        # 提前取行业包：本地抽取失败时，「直连+去标签」兜底要用它做锚点校验。
        pack_id = self.candidates.get_candidate_industry_pack_id(
            candidate_id,
            activation_id=str(candidate.get("activation_id") or ""),
        )
        try:
            extracted = self._extractor().crawl_article_content(
                candidate["original_url"],
                timeout=config.INTEL_SCAN_READ_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            # 本地抓取异常：先试「直连+去标签」兜底（快、本地、无 VPN 成本），命中直接入库；
            # 不命中再走 Scrapling 隐身抓取（本地第一梯队），最后才送 VPN 真浏览器渲染。
            _direct_err = ""
            extracted = self._direct_text_fallback(
                candidate["original_url"],
                title=str(candidate.get("title") or ""),
                pack_id=pack_id,
                candidate_id=candidate_id,
            )
            if not extracted.get("success"):
                _direct_err = str(extracted.get("error") or "")
                # 阶段5：Scrapling 隐身抓取（本地第一梯队，VPN 之前）
                _scrapling_err = ""
                extracted = self._scrapling_fallback(
                    candidate["original_url"],
                    title=str(candidate.get("title") or ""),
                    pack_id=pack_id,
                    candidate_id=candidate_id,
                    task_id=crawler_task_id,
                )
                if not extracted.get("success"):
                    _scrapling_err = str(extracted.get("error") or "")
                    extracted = self._try_vpn_fallback(
                        candidate["original_url"], task_id=crawler_task_id, candidate_id=candidate_id
                    )
            if extracted.get("unreachable") or not extracted.get("success"):
                # 记录直连/Scrapling/VPN 兜底的真实失败原因（站点不可达 / OCR 超时失败等），并打到 worker 日志，
                # 否则 403 反爬候选只看得到本地错误，无从判断是否送过 VPN OCR、以及为什么没救回来。
                _fallback_err = str(extracted.get("error") or "").strip()
                _direct_part = f"；直连兜底失败({_direct_err})" if _direct_err else ""
                _scrapling_part = f"；Scrapling兜底失败({_scrapling_err})" if _scrapling_err else ""
                error = (
                    f"本地聚合异常({sanitize_external_error(exc) or str(exc)})"
                    f"{_direct_part}"
                    f"{_scrapling_part}"
                    f"；VPN兜底失败({_fallback_err or '未知原因'})"
                )
                print(
                    f"⚠️ 候选 {candidate_id} 本地抓取异常 + 直连/Scrapling/VPN 兜底均失败: "
                    f"本地={sanitize_external_error(exc) or str(exc)}；直连={_direct_err or '-'}；"
                    f"Scrapling={_scrapling_err or '-'}；VPN={_fallback_err or '未知'}",
                    flush=True,
                )
                self.db.update_crawl_task_status(
                    crawler_task_id, "failed", error_message=error
                )
                _record_anti_bot_backoff(candidate, error, self.db)
                return {
                    "success": False,
                    "outcome": "crawler_failed",
                    "crawler_task_id": crawler_task_id,
                    "error": error,
                    "permanent": bool(extracted.get("unreachable")),
                }

        # 本地未报异常但未成功（如被拒/内容缺陷）：先试「直连+去标签」兜底，
        # 再试 Scrapling 隐身抓取（阶段5 本地第一梯队），最后才送 VPN。
        if not extracted.get("success"):
            _direct_err = ""
            fallback = self._direct_text_fallback(
                candidate["original_url"],
                title=str(candidate.get("title") or ""),
                pack_id=pack_id,
                candidate_id=candidate_id,
            )
            if not fallback.get("success"):
                _direct_err = str(fallback.get("error") or "")
                _scrapling_err = ""
                fallback = self._scrapling_fallback(
                    candidate["original_url"],
                    title=str(candidate.get("title") or ""),
                    pack_id=pack_id,
                    candidate_id=candidate_id,
                    task_id=crawler_task_id,
                )
                if not fallback.get("success"):
                    _scrapling_err = str(fallback.get("error") or "")
                    fallback = self._try_vpn_fallback(
                        candidate["original_url"], task_id=crawler_task_id, candidate_id=candidate_id
                    )
            if fallback.get("success"):
                extracted = fallback
            else:
                # 本地未成功 + 直连/Scrapling/VPN 兜底也失败：记录兜底真实原因
                _fallback_err = str(fallback.get("error") or "").strip()
                _local_err = sanitize_external_error(
                    extracted.get("error") or "正文聚合失败"
                ) or "正文聚合失败"
                _direct_part = f"；直连兜底失败({_direct_err})" if _direct_err else ""
                _scrapling_part = f"；Scrapling兜底失败({_scrapling_err})" if _scrapling_err else ""
                error = (
                    f"本地聚合失败({_local_err})"
                    f"{_direct_part}"
                    f"{_scrapling_part}"
                    f"；VPN兜底失败({_fallback_err or '未知原因'})"
                )
                print(
                    f"⚠️ 候选 {candidate_id} 直连/Scrapling/VPN 兜底均失败: "
                    f"本地={_local_err}；直连={_direct_err or '-'}；Scrapling={_scrapling_err or '-'}；VPN={_fallback_err or '未知'}",
                    flush=True,
                )
                self.db.update_crawl_task_status(
                    crawler_task_id, "failed", error_message=error
                )
                _record_anti_bot_backoff(candidate, error, self.db)
                return {
                    "success": False,
                    "outcome": "crawler_failed",
                    "crawler_task_id": crawler_task_id,
                    "error": error,
                    "permanent": bool(fallback.get("unreachable") or extracted.get("permanent")),
                }
        content = str(extracted.get("content") or "").strip()
        # ggzy 公告：a 页只是头部汇总，正文在 b 页 .detail（含中标人/金额）。取更完整者作正文。
        content = self._fetch_ggzy_full_body(
            str(extracted.get("url") or candidate.get("original_url") or ""), content
        )
        # 同步回 extracted，令 assess_article_quality 也以富正文为准（否则仍读 a 页短正文判过短）。
        extracted["content"] = content
        # 清洗：折叠连续 3 个及以上换行，避免原文出现过多空白行（抓取常产生 3~6 连换行）。
        try:
            import re as _re
            content = _re.sub(r"\n{3,}", "\n\n", content)
        except Exception:
            pass
        # 行级清洗（面包屑/页头页脚纯链接行/分页/空列表标记）作为基础清洗。
        try:
            from sqlite_database import clean_article_markdown
            content = clean_article_markdown(content)
        except Exception:
            pass
        if not content:
            # 抓到但正文为空：先判可达性，Scrapling 隐身抓取（阶段5 本地第一梯队），再 VPN 兜底一次。
            _empty_fallback = self._scrapling_fallback(
                candidate["original_url"],
                title=str(candidate.get("title") or ""),
                pack_id=pack_id,
                candidate_id=candidate_id,
                task_id=crawler_task_id,
            )
            if not _empty_fallback.get("success"):
                _empty_fallback = self._try_vpn_fallback(
                    candidate["original_url"], task_id=crawler_task_id, candidate_id=candidate_id
                )
            if _empty_fallback.get("success"):
                extracted = _empty_fallback
                content = str(extracted.get("content") or "").strip()
            if not content:
                self.db.update_crawl_task_status(
                    crawler_task_id,
                    "failed",
                    error_message="crawler 返回成功但没有正文",
                )
                return {
                    "success": False,
                    "outcome": "partial_success",
                    "crawler_task_id": crawler_task_id,
                    "error": "crawler 返回成功但没有正文",
                    "permanent": False,
                }

        article_url = str(extracted.get("url") or candidate["original_url"]).strip()
        extracted_title = str(extracted.get("title") or "").strip()
        # pack_id 已在本地抽取前提前获取（供「直连+去标签」兜底做锚点校验）。

        quality = assess_article_quality(extracted, candidate)
        self.candidates.record_extraction_attempt(
            candidate_id,
            strategy=str(extracted.get("extraction_method") or "smart_multi_parser"),
            status="passed" if quality["passed"] else "retryable",
            content_length=quality["content_length"],
            quality_score=quality["quality_score"],
            integrity_issues=quality["issues"],
            metadata={"metadata_issues": quality["metadata_issues"], "publish_date": extracted.get("publish_date") or "",
                      "tier": quality.get("tier") or "full"},
            error=quality["reason"],
        )
        self.candidates.set_candidate_decision(
            candidate_id,
            quality_status="passed" if quality["passed"] else "retrying",
            metadata_status="complete" if not quality["metadata_issues"] else "incomplete",
            metadata_issues=quality["metadata_issues"],
        )
        if not quality["passed"]:
            self.db.update_crawl_task_status(
                crawler_task_id, "failed", error_message=f"正文质量未达标：{quality['reason']}"
            )
            return {
                "success": False, "outcome": "quality_failed", "crawler_task_id": crawler_task_id,
                "error": f"正文质量未达标：{quality['reason']}", "permanent": False,
            }

        # 栏目/列表导航页（产品中心/解决方案/技术文章等）不应作为单篇文章收录：
        # 它们没有真正正文，标题就是栏目名，会污染首页主题卡与详情页。
        if _looks_like_listing_page(article_url, extracted_title, content):
            self.db.update_crawl_task_status(
                crawler_task_id, "failed", error_message="栏目/列表导航页，不作为文章收录"
            )
            return {
                "success": False, "outcome": "listing_page_skipped",
                "crawler_task_id": crawler_task_id,
                "error": "栏目/列表导航页，不作为文章收录", "permanent": True,
            }

        pack = industry_pack_loader.load(pack_id) if pack_id else None
        # 分级字数标准：短行业动态（30~149 字）低于 LLM 处理标准字数 → 跳过 LLM 准入，
        # 直通规则分类（省 LLM 消耗）；行业锚点门禁（确定性）照常执行。
        if quality.get("tier") == "short_dynamic":
            admission = {
                "admission": "article",
                "confidence": 1.0,
                "reason": "短行业动态：字数低于 LLM 处理标准，按分级标准直通",
                "link_expansion_recommended": False,
            }
        else:
            admission = assess_admission({**extracted, "url": article_url, "title": extracted_title}, candidate, pack or {})
        self.candidates.set_candidate_decision(
            candidate_id, admission_status=admission["admission"],
            admission_reason=admission["reason"], admission_confidence=admission["confidence"],
        )
        if admission["admission"] != "article" or admission["confidence"] < 0.65:
            if admission.get("link_expansion_recommended") and pack_id:
                for link in expand_links(content, article_url, pack or {}):
                    self.candidates.add_url_expansion_candidate(
                        industry_pack_id=pack_id, parent_candidate_id=candidate_id,
                        parent_url=article_url, **link,
                    )
            self.db.update_crawl_task_status(crawler_task_id, "failed", error_message=f"内容未准入：{admission['reason']}")
            return {"success": False, "outcome": "admission_rejected", "crawler_task_id": crawler_task_id,
                    "error": f"内容未准入：{admission['reason']}", "permanent": True,
                    "admission": admission}

        # Discovery feeds (Google News/RSS/list pages) provide a publication time
        # before the page is fetched.  Prefer that source timestamp over dates
        # heuristically extracted from page chrome such as calendars or footers.
        # Crawl time remains an audit field only and must not make an old article
        # appear as a new market signal.
        candidate_published_at = str(candidate.get("published_at") or "").strip()
        extracted_published_at = str(extracted.get("publish_date") or "").strip()
        publish_date = candidate_published_at or extracted_published_at
        candidate_title = str(candidate.get("title") or "").strip()
        # Some legacy corporate sites use a generic HTML <title> such as
        # "Index" for every news detail page.  The list-page title is then the
        # authoritative headline and must win, otherwise all news collapses
        # into one duplicate record and cannot be classified as an event.
        generic_titles = {"index", "home", "首页", "主頁", "untitled"}
        title = candidate_title if (
            candidate_title and extracted_title.casefold() in generic_titles
        ) else (extracted_title or candidate_title or article_url)
        # 某些站用栏目名/空泛词充当文章标题，兜底成正文关键句，避免首页卡显示"技术文章"这类栏目名。
        title = _derive_real_title(title, content, article_url)
        # Google candidates already come from the industry's exact configured
        # search phrases, so they bypass the cheap snippet gate.  Validate them
        # once against the extracted full text instead.  This is deterministic
        # (no LLM call): irrelevant search noise is discarded before it can
        # consume classification or RAG parsing resources.
        if pack_id:
            pack = industry_pack_loader.load(pack_id)
            full_text_score = quick_score_candidate(title, content, pack)
            # 硬锚点门控：正文必须命中行业包的主题锚点词才允许落库。
            # 泛词（电力/通信/基础设施/中标/发布 之类）只能给已相关内容加分，不能单独让
            # 无关文章入库——否则采购招标公告会因为施工范围里附带提了一句“配套电力管网”
            # 就被收录。唯一例外：行业包本身没定义锚点词时不强制，避免整包被拦空。
            _anchor_hits = full_text_score.get("anchor_hits") or []
            _anchor_enforced = bool(full_text_score.get("anchor_required"))
            _threshold = float(full_text_score.get("threshold") or 0)
            _score_ok = float(full_text_score.get("score") or 0) >= max(0.5, _threshold * 0.5)
            from intel_content_quality_gate import looks_like_metadata_shell, looks_like_meeting_notice
            from content_handlers import _looks_like_listing_or_contentless
            # 栏目/列表页必须在本层也拦住：这类页面由大量相关标题链接组成，关键词密度极高，
            # 仅靠行业锚点门禁必然放行（它看起来"非常相关"）。既有的 _looks_like_listing_page 会漏判。
            _contentless_listing = _looks_like_listing_or_contentless(content, title)
            _metadata_shell = looks_like_metadata_shell(content)
            # 会议/活动通知页：唯一日期往往是未来的会议时间，会被误当作新文章 → 拦截
            _meeting_notice = looks_like_meeting_notice(content, title)
            if _contentless_listing or _metadata_shell or _meeting_notice or not (_score_ok and (_anchor_hits or not _anchor_enforced)):
                self.db.update_crawl_task_status(
                    crawler_task_id,
                    "failed",
                    error_message=("正文为栏目/列表页，非单篇文章" if _contentless_listing else ("正文仅为公告头/字段表壳，无实际内容" if _metadata_shell else ("正文为会议/活动通知页，非文章" if _meeting_notice else "正文未命中行业锚点词，未进入分类与知识库"))),
                )
                return {
                    "success": False,
                    "outcome": "not_relevant_after_content_check",
                    "crawler_task_id": crawler_task_id,
                    "error": "正文未命中行业锚点词，未进入分类与知识库",
                    "permanent": True,
                }
            matched_keywords = [
                str(item.get("keyword") or "").strip()
                for item in full_text_score.get("matched_keywords") or []
                if str(item.get("keyword") or "").strip()
            ]
        else:
            matched_keywords = self.candidates.get_candidate_industry_keywords(candidate_id)
        from article_ingest import ingest_article

        article_id = ingest_article(
            {
                "url": article_url,
                "canonical_url": candidate["canonical_url"],
                "configured_url": candidate["original_url"],
                "resolved_target_url": article_url,
                "title": title,
                "content": content,
                "raw_content": extracted.get("raw_content") or "",
                "publish_date": publish_date,
                "extraction_method": extracted.get("extraction_method") or "existing_crawler",
                "quality_score": extracted.get("quality_score") or 0,
                "matched_keywords": matched_keywords,
                "source_method": "intel_candidate_dispatch",
                "source_task_id": crawler_task_id,
                "source_task_name": f"intel candidate {candidate_id}",
            },
            source_kind="candidate_crawler",
            db=self.db,
        )
        if not article_id:
            article_id = self.candidates.find_article_for_candidate(candidate)
        if not article_id:
            self.db.update_crawl_task_status(
                crawler_task_id,
                "failed",
                error_message="正文已提取但文章未成功入库",
            )
            return {
                "success": False,
                "outcome": "partial_success",
                "crawler_task_id": crawler_task_id,
                "error": "正文已提取但文章未成功入库",
                "permanent": False,
            }
        self.db.link_article_to_task(article_id, crawler_task_id)
        # T4.3 成功抓取 → 清空该域名反爬退避计数（站点恢复）
        _record_anti_bot_success(article_url, self.db)
        # 临时库：原文入 staging（供核对），加工成功后标记 done；3 天自动清理。
        _staging_id = self._staging_upsert(
            url=article_url, title=title, raw_content=content,
            source_method="intel_candidate_dispatch",
            source_task_id=crawler_task_id,
            source_task_name=f"intel candidate {candidate_id}",
            industry_pack_id=pack_id or "", matched_keywords=",".join(matched_keywords),
            status="enriching",
        )
        enrichment = self._enrich_article_remotely(
            int(article_id),
            url=article_url,
            title=title,
            content=content,
            keywords=matched_keywords,
            task_id=crawler_task_id,
            industry_pack_id=pack_id or "",
        )
        # 正文过短（很可能关键信息在产品图/参数表里）：入队 enrich_repair，交给 VPN OCR 读图
        # + LLM 抽取 产品功能/应用场景/核心参数。仅对确实很短的正文触发，避免刷 VPN。
        if config.REMOTE_PIPELINE_URL and len(str(content or "").strip()) < 400:
            try:
                from intel_database import IntelRepository as _FieldRepo
                _fr = _FieldRepo(self.db)
                _fj, _fc = _fr.enqueue_job(
                    "enrich_repair",
                    f"enrich-repair:{int(article_id)}:{crawler_task_id}",
                    {
                        "article_id": int(article_id), "url": article_url,
                        "title": title, "content": content, "task_id": crawler_task_id,
                    },
                    request_id="intel-candidate", created_by="",
                )
            except Exception:
                pass
        if enrichment.get("attached"):
            self._staging_mark(_staging_id, status="done", article_id=article_id)
        else:
            self._staging_mark(_staging_id, status="failed", error=str(enrichment.get("error") or ""))
        self.candidates.set_candidate_decision(candidate_id, quality_status="passed", admission_status="pending")
        self.db.update_crawl_task_status(
            crawler_task_id,
            "completed",
            progress=100,
            articles_found=1,
            articles_processed=1,
        )
        classification_job = self._enqueue_candidate_classification(
            candidate_id,
            article_id,
            activation_id=str(candidate.get("activation_id") or ""),
        )
        return {
            "success": True,
            "outcome": "crawled",
            "crawler_task_id": crawler_task_id,
            "article_id": int(article_id),
            "classification_job": classification_job,
            "enrichment": enrichment,
        }
