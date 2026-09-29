#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF report ingestion: download once, extract text, deduplicate by hash.

Reports are deliberately handled outside the daily article-link crawler.  A
scheduled check may revisit the configured PDF URL, but unchanged files do
not create another article or another RAGFlow document.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List

import re as _re
from collections import deque
from urllib.parse import urljoin

from bs4 import BeautifulSoup

import config
from intel_http import SafeHTTPClient, sanitize_external_error
from industry_packs import industry_anchor_keywords, industry_pack_loader, normalize_intel_text
from industry_ragflow import resolve_industry_ragflow_kb_id


class ReportSkipError(ValueError):
    """报告页不应入库（无真实 PDF/目录页）：预期跳过，不算任务失败。"""
from intel_llm_client import intel_llm_client
from sqlite_database import sqlite_db


# 目录/列表页标记：写入 intel_reports.metadata_json。这类页面（栏目页/搜索结果页/首页）
# 不是真报告，报告列表查询据此排除，避免“栏目/目录页”混进报告列表。
LISTING_PAGE_META = '"page_kind": "listing"'
LISTING_PAGE_META_LIKE = '%' + LISTING_PAGE_META + '%'
# 真 PDF 报告标记：报告列表只收录真正下载到 PDF 的报告，其余（HTML 外壳/目录页/付费墙页）一律排除。
REPORT_PDF_META = '"original_format": "pdf"'
REPORT_PDF_META_LIKE = '%' + REPORT_PDF_META + '%'

# ── 阶段2：报告文件内联阅读（markitdown）──────────────────────────────────
# 白名单办公文档格式 + 大小/超时边界（不装 OCR/音频插件，不引入 SSRF 面）
REPORT_OFFICE_SUFFIXES = ('.docx', '.xlsx', '.pptx')
REPORT_OFFICE_FORMATS = ('pdf', 'docx', 'xlsx', 'pptx')
# 报告列表只收录这四类真实文件（元数据标记 LIKE 条件，兼容 SQLite/PostgreSQL）
REPORT_FORMAT_META_LIKE = ' OR '.join(
    f"COALESCE(r.metadata_json,'') LIKE '%\"original_format\": \"{fmt}\"%'" for fmt in REPORT_OFFICE_FORMATS
)
MAX_REPORT_CONVERT_BYTES = 20 * 1024 * 1024      # 单文件转换上限 20MB
MARKDOWN_CONVERT_TIMEOUT = 30                     # 转换超时 30s → 提示下载查看
OFFICE_MIME = {
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
}
# 报告主题判定只看标题区的字数（报告开头），避免长篇报告正文偶然提到主题词就误判相关
REPORT_TOPIC_HEADING_CHARS = 600


def industry_topic_keywords(pack) -> List[str]:
    """行业包的“主题锚点词”：从 candidate_gate.anchor_keywords 中剔除机构名。

    行业包的锚点词里混着机构名（中国信息通信研究院/工业和信息化部/南方电网 等），
    它们会出现在每份机构报告的落款里——用它们判断相关性会让任何报告都“符合主题”，
    所以这里剔除 entity_keywords，只保留 工控安全/算力/网络安全 这类真正的主题词。
    """
    gate = (pack or {}).get('candidate_gate') or {}
    anchors = [str(item).strip() for item in (gate.get('anchor_keywords') or []) if str(item).strip()]
    entities = {str(item).strip().casefold() for item in (gate.get('entity_keywords') or [])}
    return [item for item in anchors if item.casefold() not in entities]


def report_matches_industry_topic(pack, title: str = '', heading: str = '') -> bool:
    """报告是否符合行业包主题：标题或报告开头命中行业“主题锚点词”。"""
    keywords = industry_topic_keywords(pack)
    if not keywords:
        return True                      # 行业包未定义主题词时不强制，避免把整包清空
    probe = normalize_intel_text(f'{title or ""} {heading or ""}')
    return any(normalize_intel_text(item) in probe for item in keywords if item)
# 栏目式标题：“栏目名 - 站点名”，去掉结尾站点名后仅剩短栏目名
_REPORT_COLUMN_TITLE = _re.compile(r'^(.*?)\s*[-–—|·]\s*([^-–—|·]{2,20})$')


class IntelReportService:
    def __init__(self, database=None, http_client=None):
        self.db = database or sqlite_db
        self.http = http_client or SafeHTTPClient()

    @staticmethod
    def default_ragflow_kb_id() -> str:
        """Use the same configured target as normal crawled articles/AI chat."""
        try:
            from chat_api import _load_config
            return str(_load_config().get('ragflow_kb_id') or '').strip()
        except Exception:
            return ''

    def list_reports(self, industry_pack_id: str, page: int = 1, per_page: int = 50):
        self.db._ensure_connection()
        offset = max(0, (int(page) - 1) * int(per_page))
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    f"SELECT COUNT(*) FROM intel_reports r WHERE r.industry_pack_id=? AND ({REPORT_FORMAT_META_LIKE})",
                    (industry_pack_id,),
                )
                total = int(cursor.fetchone()[0])
                cursor.execute(
                    f"""SELECT r.*, s.source_name, a.title AS article_title
                       FROM intel_reports r
                       LEFT JOIN intel_sources s ON s.id=r.source_id
                       LEFT JOIN articles a ON a.id=r.article_id
                       WHERE r.industry_pack_id=? AND ({REPORT_FORMAT_META_LIKE})
                       ORDER BY COALESCE(r.last_changed_at, r.created_at) DESC, r.id DESC
                       LIMIT ? OFFSET ?""",
                    (industry_pack_id, int(per_page), offset),
                )
                return [dict(row) for row in cursor.fetchall()], total
            finally:
                cursor.close()

    def check_known_reports(self, industry_pack_id: str) -> Dict:
        reports, _total = self.list_reports(industry_pack_id, 1, 1000)
        results = {"checked": 0, "changed": 0, "unchanged": 0, "failed": 0}
        for report in reports:
            # Locally uploaded files have no remote URL to poll. They remain
            # analysable articles, but are excluded from daily network checks.
            if str(report.get('report_url') or '').startswith('upload://'):
                continue
            # 目录/列表页不是真报告：不再重复抓取（避免被覆盖成其它状态）
            if LISTING_PAGE_META in str(report.get('metadata_json') or ''):
                continue
            results["checked"] += 1
            try:
                metadata = json.loads(report.get('metadata_json') or '{}')
                outcome = self.ingest(report['report_url'], industry_pack_id, source_id=report.get('source_id'), title=report.get('title') or '', ragflow_kb_id=str(metadata.get('ragflow_kb_id') or ''))
                results["unchanged" if outcome.get('status') == 'unchanged' else "changed"] += 1
            except Exception as exc:
                results["failed"] += 1
                with self.db.lock:
                    cursor = self.db.connection.cursor()
                    try:
                        cursor.execute("UPDATE intel_reports SET status='failed', last_checked_at=datetime('now'), last_error=?, updated_at=datetime('now') WHERE id=?", (sanitize_external_error(exc), report['id']))
                        self.db.connection.commit()
                    finally:
                        cursor.close()
        return results

    def _render_report_via_browser(self, url: str, timeout: int = 45) -> Dict:
        """真浏览器渲染报告 URL，绕过 caict 等站点的 412/反爬（普通 requests 被拒时兜底）。

        返回 {raw, resolved, content_type, title}；raw 为渲染后的完整 HTML。
        若渲染结果为 WAF/防爬拦截页，则抛出 ValueError，避免把拦截页当成报告入库。
        """
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                context = browser.new_context(
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"),
                    locale="zh-CN",
                )
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                page.wait_for_timeout(2500)
                raw = page.content()
                text = _re.sub(r"<[^>]+>", " ", raw)
                text = _re.sub(r"\s+", " ", text).strip()
                blocked = (
                    len(text) < 800
                    or _re.search(r"访问防护|防护规则|访问过于频繁|安全验证|验证码|请于.{0,20}小时后重试|拒绝访问|captcha|人机验证", text, _re.I)
                )
                if blocked:
                    raise ValueError(f"WAF/防爬拦截页（{len(text)}字），跳过: {url}")
                return {
                    "raw": raw,
                    "resolved": str(page.url or url),
                    "content_type": "text/html",
                    "title": (page.title() or "").strip(),
                }
            finally:
                try:
                    browser.close()
                except Exception:
                    pass

    def ingest(self, report_url: str, industry_pack_id: str, *, source_id=None, title: str = '', ragflow_kb_id: str = '') -> Dict:
        ragflow_kb_id = resolve_industry_ragflow_kb_id(
            industry_pack_id, str(ragflow_kb_id or '').strip()
        )
        try:
            response = self.http.get(report_url, headers={"User-Agent": "Market-Intel-ReportBot/1.0"})
            raw, resolved, content_type = response.content, response.url, response.content_type
        except Exception as plain_exc:
            raw = resolved = content_type = None
            # 反爬/412/403 等普通请求失败 → 先走 VPN 真浏览器渲染
            try:
                from remote_pipeline_client import remote_pipeline_client
                vpn = remote_pipeline_client.run(url=report_url, mode="article", enrich=False, tts=False)
                articles = (vpn or {}).get("articles") or []
                if articles:
                    art = articles[0]
                    content = str(art.get("content") or art.get("raw_content") or "").strip()
                    if content:
                        raw = f"<html><body>{content}</body></html>".encode("utf-8")
                        resolved = str(art.get("url") or report_url)
                        content_type = "text/html"
            except Exception:
                pass
            # VPN 未产出 → 本地 Playwright 真浏览器兜底（能过 caict 等 412/反爬）
            if not raw:
                try:
                    rendered = self._render_report_via_browser(report_url)
                    raw = rendered["raw"].encode("utf-8")
                    resolved = str(rendered.get("resolved") or report_url)
                    content_type = str(rendered.get("content_type") or "text/html")
                    title = title or str(rendered.get("title") or "").strip()
                except Exception:
                    raise plain_exc
            if not raw:
                raise plain_exc
        return self._ingest_raw(raw, report_url, resolved, industry_pack_id, source_id=source_id, title=title, ragflow_kb_id=ragflow_kb_id, content_type=content_type)


    def ingest_upload(self, raw: bytes, filename: str, industry_pack_id: str, *, title: str = '', ragflow_kb_id: str = '') -> Dict:
        """Persist and analyse an administrator-uploaded PDF/Word/Excel/PPT report as an article."""
        ragflow_kb_id = resolve_industry_ragflow_kb_id(
            industry_pack_id, str(ragflow_kb_id or '').strip()
        )
        safe_name = ''.join(char for char in str(filename or 'report.pdf') if char.isalnum() or char in '._-')[-180:] or 'report.pdf'
        suffix = Path(safe_name).suffix.casefold()
        office_fmt = self._office_format_hint('', safe_name)
        if suffix == '.pdf':
            content_type = 'application/pdf'
        elif office_fmt:
            content_type = OFFICE_MIME[office_fmt]
        else:
            raise ValueError('仅支持 PDF/Word/Excel/PPT 文件（.pdf/.docx/.xlsx/.pptx）')
        digest = hashlib.sha256(raw).hexdigest()
        report_url = f'upload://{digest}/{safe_name}'
        return self._ingest_raw(raw, report_url, report_url, industry_pack_id, title=title or safe_name, ragflow_kb_id=ragflow_kb_id, content_type=content_type, filename=safe_name)

    @staticmethod
    def _html_to_markdown(raw: bytes) -> tuple[str, str]:
        """Make a portable Markdown copy of an HTML report without web-only chrome."""
        source = raw.decode('utf-8', errors='replace')
        soup = BeautifulSoup(source, 'html.parser')
        document_title = soup.title.get_text(' ', strip=True) if soup.title else ''
        for node in soup(['script', 'style', 'noscript', 'svg', 'iframe', 'nav', 'footer', 'form']):
            node.decompose()
        blocks = []
        for node in soup.find_all(['h1', 'h2', 'h3', 'h4', 'p', 'li', 'blockquote', 'pre']):
            value = node.get_text(' ', strip=True)
            if not value:
                continue
            if node.name.startswith('h'):
                blocks.append('#' * int(node.name[1]) + ' ' + value)
            elif node.name == 'li':
                blocks.append('- ' + value)
            elif node.name == 'blockquote':
                blocks.append('> ' + value)
            else:
                blocks.append(value)
        text = '\n\n'.join(blocks).strip()
        return text, document_title

    @staticmethod
    def _markdown_text(title: str, text: str, source_url: str) -> str:
        return f"# {title}\n\n来源：{source_url}\n\n---\n\n{text.strip()}\n"

    @staticmethod
    def _report_directory() -> Path:
        directory = Path(os.getenv('INTEL_REPORT_STORAGE_DIR', 'data/intel_reports'))
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @staticmethod
    def _office_format_hint(content_type: str = '', filename: str = '') -> str:
        """根据 MIME 或文件后缀推断办公文档格式（docx/xlsx/pptx），识别不出返回空串。"""
        ct = str(content_type or '').casefold()
        if 'wordprocessingml' in ct:
            return 'docx'
        if 'spreadsheetml' in ct:
            return 'xlsx'
        if 'presentationml' in ct:
            return 'pptx'
        suffix = Path(str(filename or '')).suffix.casefold()
        if suffix in REPORT_OFFICE_SUFFIXES:
            return suffix[1:]
        return ''

    def _office_to_markdown(self, path: Path, fmt: str) -> str:
        """本地办公文档 → Markdown（markitdown，阶段2 内联阅读）。

        边界：≤20MB、30s 超时、只转本地已入库文件；超限/超时/失败统一抛 ValueError，
        调用方降级为"请下载查看"，绝不阻塞报告列表与下载。
        """
        if not path.exists():
            raise ValueError('报告原始文件不存在')
        if path.stat().st_size > MAX_REPORT_CONVERT_BYTES:
            raise ValueError('文件过大，请下载查看')
        try:
            from markitdown import MarkItDown
            converter = MarkItDown(enable_plugins=False)
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                future = pool.submit(converter.convert, str(path))
                result = future.result(timeout=MARKDOWN_CONVERT_TIMEOUT)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            text = str(getattr(result, 'text_content', '') or '').strip()
        except ValueError:
            raise
        except Exception as exc:
            # 超时（TimeoutError）、依赖缺失、解析失败等 → 统一降级提示
            raise ValueError(f'{fmt.upper()} 文档转换失败，请下载查看（{type(exc).__name__}）') from exc
        if not text:
            raise ValueError(f'{fmt.upper()} 文档没有可读内容，请下载查看')
        return text

    @staticmethod
    def _find_pdf_download_url(raw_html, base_url):
        """在报告页 HTML 里泛化查找真正的 PDF 下载链接。

        匹配优先级：.pdf 后缀 / download 端点 / 下载 或 PDF 文案 / class 含 download。
        返回绝对 URL；找不到返回 None。
        """
        soup = BeautifulSoup(raw_html, 'html.parser')
        best_score, best_href = 0, None
        for a in soup.find_all('a', href=True):
            href = str(a.get('href') or '').strip()
            href_l = href.casefold()
            if href_l.startswith(('#', 'javascript:', 'mailto:')):
                continue
            if not ('.pdf' in href_l or '/download' in href_l or 'download' in href_l
                    or 'getfile' in href_l or 'file' in href_l):
                continue
            text = a.get_text(' ', strip=True).casefold()
            cls = ' '.join(a.get('class') or []).casefold()
            score = 0
            if '.pdf' in href_l: score += 4
            if '/download' in href_l: score += 3
            if 'download' in href_l: score += 2
            if 'download-link' in cls: score += 3
            if 'pdf' in text: score += 2
            if '下载' in text: score += 2
            if '在线阅读' in text: score += 1
            if score > best_score:
                best_score, best_href = score, urljoin(base_url, href)
        return best_href

    @staticmethod
    def _looks_like_report_listing(raw_html, title: str = '') -> bool:
        """判断报告页是否为“栏目/目录/列表页”（不是真报告，不应入库与展示）。

        在 600+ 条真实数据上验证过的两条判据，命中其一即判定（对真报告误判率极低）：
        1) 标题形如“栏目名 - 站点名”：去掉结尾站点名后，只剩一个短栏目名且不含年份；
        2) 正文以链接为主：链接文字占全部可见文字 ≥ 50%，是典型的目录/导航列表页。
        """
        source = (raw_html.decode('utf-8', errors='replace')
                  if isinstance(raw_html, (bytes, bytearray)) else str(raw_html or ''))
        if not source.strip():
            return True
        # 判据 1：栏目式标题（如“智能家居-中国市场情报中心”“CIC工信安全|研究机构”“qwfb”）
        text_title = str(title or '').strip()
        if text_title:
            matched = _REPORT_COLUMN_TITLE.match(text_title)
            head = (matched.group(1) if matched else text_title).strip()
            if (head and len(head) <= 12 and not _re.search(r'\d{4}', head)
                    and not head.casefold().endswith('.pdf')):
                return True
        # 判据 2：正文以链接为主（目录页的可见文字几乎都是链接标题）
        soup = BeautifulSoup(source, 'html.parser')
        for node in soup(['script', 'style', 'noscript', 'svg', 'iframe']):
            node.decompose()
        visible = soup.get_text(' ', strip=True)
        if len(visible) < 50:
            return False
        link_text = sum(len(a.get_text(' ', strip=True)) for a in soup.find_all('a', href=True))
        return (link_text / len(visible)) >= 0.5

    def _fetch_report_pdf(self, url: str, referer: str = '') -> bytes:
        """下载并校验报告 PDF；返回内容，非 PDF 抛 ValueError。"""
        headers = {"User-Agent": "Market-Intel-ReportBot/1.0"}
        if referer:
            headers['Referer'] = str(referer)
        # 用 requests 直接下载（SafeHTTPClient 流式拉 PDF 偶发连接中断；PDF 下载用普通请求更稳）
        import requests
        resp = requests.get(url, headers=headers, timeout=(15, 120), allow_redirects=True)
        resp.raise_for_status()
        content = resp.content
        if not content.startswith(b'%PDF'):
            raise ValueError('下载到的不是 PDF 文件')
        return content

    def _ingest_raw(self, raw: bytes, report_url: str, resolved_url: str, industry_pack_id: str, *, source_id=None, title: str = '', ragflow_kb_id: str = '', content_type: str = '', filename: str = '') -> Dict:
        pack = industry_pack_loader.load(industry_pack_id)
        is_pdf = raw.startswith(b'%PDF')
        is_html = ('html' in str(content_type or '').casefold()) or raw.lstrip().startswith((b'<!DOCTYPE html', b'<!doctype html', b'<html', b'<HTML'))
        # 阶段2：办公文档（docx/xlsx/pptx，ZIP 容器）+ 格式白名单；识别不出格式直接拒绝
        office_fmt = ''
        if not is_pdf and not is_html and raw.startswith(b'PK\x03\x04'):
            office_fmt = self._office_format_hint(content_type, filename)
            if not office_fmt:
                raise ValueError('无法识别的办公文档格式（仅支持 .docx/.xlsx/.pptx）')
        is_office = bool(office_fmt)
        if not is_pdf and not is_html and not is_office:
            raise ValueError('仅支持 PDF/Word/Excel/PPT 文件或 HTML 报告页面')
        # 报告页是 HTML：优先从页面上找真正的 PDF 下载链接并下载，避免把“目录/营销页”当报告入库
        if is_html:
            pdf_ok = False
            try:
                pdf_url = self._find_pdf_download_url(raw, resolved_url or report_url)
                if pdf_url:
                    pdf_raw = self._fetch_report_pdf(pdf_url, referer=resolved_url or report_url)
                    raw = pdf_raw
                    is_pdf = True
                    resolved_url = pdf_url
                    pdf_ok = True
            except Exception:
                pdf_ok = False
            # 没拿到真 PDF：无论页面是目录页还是"有正文但站点未提供下载"的报告页，一律不入库。
            # 报告列表只允许出现能下载到真 PDF 的报告，所以入库环节就必须把 PDF 作为硬条件。
            if not pdf_ok and self._looks_like_report_listing(raw, title):
                raise ReportSkipError('报告页为栏目/目录列表页、未获得真实 PDF 下载，跳过')
            if not pdf_ok:
                raise ReportSkipError('报告页未提供可下载的真实 PDF（无下载入口或无 PDF 直链），跳过')
        digest = hashlib.sha256(raw).hexdigest()
        self.db._ensure_connection()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("SELECT * FROM intel_reports WHERE report_url=?", (report_url,))
                existing = cursor.fetchone()
                if existing and str(existing['content_sha256'] or '') == digest:
                    try:
                        existing_metadata = json.loads(existing['metadata_json'] or '{}')
                    except (TypeError, ValueError, json.JSONDecodeError):
                        existing_metadata = {}
                    if existing_metadata.get('llm_insight_version') == 'v1':
                        cursor.execute("UPDATE intel_reports SET resolved_url=?, status='unchanged', last_checked_at=datetime('now'), updated_at=datetime('now'), last_error='' WHERE id=?", (resolved_url, existing['id']))
                        self.db.connection.commit()
                        return {"report_id": existing['id'], "status": "unchanged", "article_id": existing['article_id']}
            finally:
                cursor.close()

        directory = self._report_directory()
        ext = 'pdf' if is_pdf else ('html' if is_html else office_fmt)
        raw_path = directory / f'{digest}.{ext}'
        if not raw_path.exists():
            raw_path.write_bytes(raw)
        extracted_title = ''
        if is_pdf:
            with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as output:
                text_path = output.name
            try:
                run = subprocess.run(['pdftotext', '-layout', str(raw_path), text_path], capture_output=True, text=True, timeout=90)
                if run.returncode != 0:
                    raise ValueError('PDF 正文提取失败')
                text = Path(text_path).read_text(encoding='utf-8', errors='replace').strip()
            finally:
                Path(text_path).unlink(missing_ok=True)
        elif is_office:
            # 阶段2：Word/Excel/PPT → markitdown 转换（含表格/标题结构）
            text = self._office_to_markdown(raw_path, office_fmt)
        else:
            text, extracted_title = self._html_to_markdown(raw)
        # 放宽：报告作为独立素材入库，不受"正文≥80字 / 命中行业锚点 / LLM 拆出子文章"硬门槛约束。
        # 即便正文短（扫描件）、未命中锚点、或 LLM 关闭抽不出子文章，报告本身也应入库可查看/下载；
        # 命中锚点时再尽力用 LLM 生成子文章（子文章才走分类）。这是与普通文章准入不同的报告通道。
        kind_label = {'pdf': 'PDF 报告', 'docx': 'Word 报告', 'xlsx': 'Excel 报告', 'pptx': 'PPT 报告', 'html': 'HTML 报告'}
        final_title = title.strip() or extracted_title or Path(resolved_url.split('?', 1)[0]).name or kind_label.get(ext, '报告')
        # 报告必须符合当前行业包主题：标题或报告开头要命中行业主题锚点词，否则不入库。
        # 列表只允许出现“正式且属于当前行业包”的报告。
        if not report_matches_industry_topic(pack, final_title, text[:REPORT_TOPIC_HEADING_CHARS]):
            raise ValueError('报告主题不符合当前行业包，跳过')
        markdown_path = directory / f'{digest}.md'
        markdown_path.write_text(self._markdown_text(final_title, text, resolved_url), encoding='utf-8')
        anchors = industry_anchor_keywords(pack)
        matched = [item for item in anchors if item.casefold() in text.casefold() or item.casefold() in final_title.casefold()]
        # 子文章（LLM 拆解）尽力而为：LLM 关闭/失败/未命中锚点都不阻断报告本身入库。
        insights = []
        try:
            insights = intel_llm_client.extract_report_items(text, pack) or []
        except Exception:
            insights = []
        child_articles = []
        for index, insight in enumerate(insights, start=1):
            try:
                child_text = insight['content']
                child_title = insight['title']
            except (KeyError, TypeError):
                continue
            child_matches = [word for word in anchors if word.casefold() in f"{child_title}\n{child_text}".casefold()]
            if not child_matches:
                continue
            article_id = self.db.insert_article({
                'url': f'{report_url}#insight-{digest[:12]}-{index}',
                'canonical_url': f'{report_url}#insight-{digest[:12]}-{index}',
                'title': child_title,
                'content': child_text,
                'matched_keywords': child_matches,
                'matched_keywords_raw': ','.join(child_matches),
                'extraction_method': 'pdf_llm_insight',
                'quality_score': 0.9,
                'source_method': 'report_pdf_llm',
                'configured_url': report_url,
                'resolved_target_url': resolved_url,
                'report_title': final_title,
                'llm_suggested_category': insight.get('category') or 'other',
            })
            if article_id:
                child_articles.append(int(article_id))
        article_id = child_articles[0] if child_articles else None
        original_format = 'pdf' if is_pdf else ('html' if is_html else office_fmt)
        extraction_method = 'pdftotext' if is_pdf else ('markitdown' if is_office else 'html_markdown')
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    """INSERT INTO intel_reports(source_id, industry_pack_id, report_url, resolved_url, title, article_id, content_sha256, local_path, status, extraction_method, extracted_characters, last_checked_at, last_changed_at, last_error, metadata_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ingested', ?, ?, datetime('now'), datetime('now'), '', ?)
                       ON CONFLICT(report_url) DO UPDATE SET source_id=excluded.source_id, industry_pack_id=excluded.industry_pack_id, resolved_url=excluded.resolved_url, title=excluded.title, article_id=excluded.article_id, content_sha256=excluded.content_sha256, local_path=excluded.local_path, status='ingested', extraction_method=excluded.extraction_method, extracted_characters=excluded.extracted_characters, last_checked_at=datetime('now'), last_changed_at=datetime('now'), last_error='', metadata_json=excluded.metadata_json, updated_at=datetime('now')""",
                    (source_id, industry_pack_id, report_url, resolved_url, final_title, article_id, digest, str(raw_path), extraction_method, len(text), json.dumps({'ragflow_kb_id': ragflow_kb_id, 'llm_insight_version': 'v1', 'child_article_ids': child_articles, 'markdown_path': str(markdown_path), 'original_format': original_format}, ensure_ascii=False)),
                )
                cursor.execute("SELECT id FROM intel_reports WHERE report_url=?", (report_url,))
                report_id = int(cursor.fetchone()[0])
                self.db.connection.commit()
            finally:
                cursor.close()
        ragflow = {'status': 'not_configured'}
        if ragflow_kb_id and child_articles:
            from ragflow_client import get_ragflow_client
            ragflow = get_ragflow_client().upload_article(ragflow_kb_id, final_title, text, report_url, {'db_id': article_id, 'report': True, 'child_article_ids': child_articles})
        return {'report_id': report_id, 'status': 'ingested', 'article_id': article_id, 'child_article_ids': child_articles, 'insight_count': len(child_articles), 'characters': len(text), 'ragflow': ragflow}

    def report_asset(self, report_id: int, asset: str = 'markdown') -> tuple[Dict, Path, str]:
        """Return an authorised report asset, generating Markdown for legacy PDFs once."""
        self.db._ensure_connection()
        with self.db.lock:
            row = self.db.connection.execute('SELECT * FROM intel_reports WHERE id=?', (int(report_id),)).fetchone()
        if not row:
            raise ValueError('报告不存在')
        report = dict(row)
        raw_path = Path(str(report.get('local_path') or ''))
        directory = self._report_directory().resolve()
        if not raw_path.exists() or directory not in raw_path.resolve().parents:
            raise ValueError('报告原始文件不存在')
        try:
            metadata = json.loads(report.get('metadata_json') or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        if asset == 'original':
            suffix = raw_path.suffix.casefold()
            if suffix == '.pdf':
                mimetype = 'application/pdf'
            elif suffix == '.html':
                mimetype = 'text/html; charset=utf-8'
            else:
                mimetype = OFFICE_MIME.get(suffix[1:], 'application/octet-stream')
            return report, raw_path, mimetype
        markdown_path = Path(str(metadata.get('markdown_path') or directory / f"{report.get('content_sha256')}.md"))
        if not markdown_path.exists():
            suffix = raw_path.suffix.casefold()
            if suffix == '.pdf':
                with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as output:
                    text_path = output.name
                try:
                    run = subprocess.run(['pdftotext', '-layout', str(raw_path), text_path], capture_output=True, text=True, timeout=90)
                    if run.returncode != 0:
                        raise ValueError('PDF 正文提取失败')
                    text = Path(text_path).read_text(encoding='utf-8', errors='replace').strip()
                finally:
                    Path(text_path).unlink(missing_ok=True)
            elif suffix in REPORT_OFFICE_SUFFIXES:
                # 阶段2：Word/Excel/PPT → markitdown（超 20MB/超时抛 ValueError 降级"请下载查看"）
                text = self._office_to_markdown(raw_path, suffix[1:])
            else:
                text, _ignored = self._html_to_markdown(raw_path.read_bytes())
            markdown_path.write_text(self._markdown_text(str(report.get('title') or '报告'), text, str(report.get('resolved_url') or report.get('report_url') or '')), encoding='utf-8')
        return report, markdown_path, 'text/markdown; charset=utf-8'


    # ── 报告 URL 探测（发现可下载的 PDF/报告直链）───────────────────────────
    def _fetch_report_text(self, url: str, timeout: int = 20) -> str:
        resp = self.http.get(url, headers={"User-Agent": "Market-Intel-ReportBot/1.0"})
        return str(resp.text or "")

    def _discover_sitemap_urls(self, source_url: str) -> List[str]:
        """robots.txt 的 Sitemap 引用 + 常见 sitemap 路径。"""
        candidates: List[str] = []
        base = str(source_url or "").rstrip("/")
        try:
            robots = self._fetch_report_text(urljoin(base, "/robots.txt"))
            for line in robots.splitlines():
                if line.casefold().startswith("sitemap:"):
                    value = line.split(":", 1)[1].strip()
                    if value:
                        candidates.append(value)
        except Exception:
            pass
        for path in ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
                     "/sitemap-news.xml", "/publication-sitemap.xml",
                     "/publications-sitemap.xml", "/news-sitemap.xml"):
            candidates.append(urljoin(base + "/", path.lstrip("/")))
        out, seen = [], set()
        for candidate in candidates:
            normalized = urljoin(base, candidate).strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                out.append(normalized)
        return out

    @staticmethod
    def _extract_loc(xml: str) -> List[str]:
        return [v.strip() for v in _re.findall(r"<loc>(.*?)</loc>", xml or "", _re.I | _re.S)]

    # 报告栏目/详情页的 URL 关键词（中文/英文/拼音段；避免过宽子串，如 bg/yj/kj 会误配大量导航）
    REPORT_HINTS = (
        "report", "research", "whitepaper", "download", "pdf",
        "报告", "研究", "白皮书", "下载", "权威", "成果", "文献", "资料", "出版物", "智库",
        "kxyj", "qwfb", "yjbg", "zkbg", "zlzx", "ziliao", "yanjiu",
        "researchreport", "industryreport", "baipishu", "chengguo", "publication",
    )

    def discover_report_pdfs(
        self, source_id, industry_pack_id: str, source_url: str, seed_url: str = ""
    ) -> Dict:
        """从信源里发现可下载的 .pdf / HTML 报告页，并登记为报告候选。

        优先用配置的 `seed_url`（信源级"报告下载栏目 URL"）作为聚合种子：只定向爬该报告栏目
        及其 detail 页（可靠、快），不再从首页宽泛递归。无 seed_url 时回退到首页+sitemap。
        每页先普通请求；被反爬(412/403)或抓不到则对该 URL 单独送 VPN 真浏览器渲染拿真实链接。
        """
        pdf_urls: set = set()
        report_html: set = set()
        seen: set = set()
        # 聚合种子：优先信源配置的报告栏目 URL；否则首页 + sitemap。
        if str(seed_url or "").strip():
            queue = deque([str(seed_url).strip()])
        else:
            queue = deque([source_url])
            for sm in self._discover_sitemap_urls(source_url):
                queue.append(sm)
        fetched = 0
        max_pages = 40
        while queue and fetched < max_pages:
            url = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            fetched += 1
            low = url.casefold()
            # 1) 先普通请求
            try:
                html = self._fetch_report_text(url)
                plain_ok = True
            except Exception:
                html = ""
                plain_ok = False
            # 2) 普通请求被反爬/失败 → 该页送 VPN 渲染
            if not plain_ok:
                try:
                    from remote_pipeline_client import remote_pipeline_client
                    vpn = remote_pipeline_client.links(url=url)
                    for loc in (vpn.get("pdf_links") or []):
                        if str(loc).casefold().endswith(".pdf"):
                            pdf_urls.add(str(loc))
                    for loc in (vpn.get("links") or []):
                        l = str(loc)
                        ll = l.casefold()
                        if ll.endswith(".pdf"):
                            pdf_urls.add(l)
                        elif (any(t in ll for t in self.REPORT_HINTS)
                                and not ll.endswith((".css", ".js", ".xml", ".png", ".jpg"))):
                            report_html.add(l)
                            if fetched < max_pages:
                                queue.append(l)
                    continue
                except Exception:
                    continue
            # 3) 普通 200：提取 .pdf + 报告栏目/detail 链接（继续下钻）
            for match in _re.finditer(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', html, _re.I):
                loc = urljoin(url, match.group(1).strip())
                if loc.casefold().endswith(".pdf"):
                    pdf_urls.add(loc)
            for match in _re.finditer(r'href=["\']([^"\']+)["\']', html, _re.I):
                href = urljoin(url, match.group(1).strip())
                ll = href.casefold()
                if ll.endswith(".pdf"):
                    pdf_urls.add(href)
                elif (any(t in ll for t in self.REPORT_HINTS)
                        and "?" not in href and "javascript:" not in ll
                        and not ll.endswith((".css", ".js", ".xml", ".png", ".jpg"))):
                    report_html.add(href)
                    if fetched < max_pages:
                        queue.append(href)
        candidates = []
        for url in sorted(pdf_urls):
            candidates.append(self.register_report_candidate(
                canonical_url=url, report_url=url, source_id=source_id,
                industry_pack_id=industry_pack_id, url_type="pdf"))
        for url in sorted(report_html):
            candidates.append(self.register_report_candidate(
                canonical_url=url, report_url=url, source_id=source_id,
                industry_pack_id=industry_pack_id, url_type="html_report", title_hint=""))
        return {"source_url": source_url, "found": len(candidates),
                "candidate_urls": [c["report_url"] for c in candidates[:20]]}

    def register_report_candidate(
        self, *, canonical_url: str, report_url: str, source_id=None,
        industry_pack_id: str = "", title_hint: str = "", url_type: str = "pdf",
    ) -> Dict:
        self.db._ensure_connection()
        with self.db.lock:
            cur = self.db.connection.cursor()
            try:
                cur.execute(
                    """INSERT INTO intel_report_candidates(canonical_url, report_url, source_id, industry_pack_id, url_type, title_hint, status, first_seen_at, last_seen_at, updated_at)
                       VALUES (?,?,?,?,?,?,'pending', datetime('now'), datetime('now'), datetime('now'))
                       ON CONFLICT(canonical_url) DO UPDATE SET
                           report_url=excluded.report_url, last_seen_at=datetime('now'), updated_at=datetime('now')""",
                    (str(canonical_url or ""), str(report_url or ""), source_id,
                     str(industry_pack_id or ""), str(url_type or "pdf"), str(title_hint or "")[:500]),
                )
                cur.execute("SELECT id, status, content_sha256 FROM intel_report_candidates WHERE canonical_url=?", (str(canonical_url or ""),))
                row = cur.fetchone()
                self.db.connection.commit()
            finally:
                cur.close()
        return {"id": int(row[0]), "status": str(row[1] or ""), "report_url": str(report_url or ""),
                "content_sha256": str(row[2] or "")}

    def pending_report_candidates(self, industry_pack_id: str, limit: int = 100) -> List[Dict]:
        self.db._ensure_connection()
        with self.db.lock:
            cur = self.db.connection.cursor()
            try:
                cur.execute(
                    """SELECT * FROM intel_report_candidates
                       WHERE status IN ('pending','queued') AND industry_pack_id=?
                       ORDER BY id ASC LIMIT ?""",
                    (str(industry_pack_id or ""), int(limit or 100)),
                )
                return [dict(r) for r in cur.fetchall()]
            finally:
                cur.close()

    def mark_report_candidate(self, candidate_id: int, *, status: str, last_error: str = "") -> None:
        if not candidate_id:
            return
        self.db._ensure_connection()
        with self.db.lock:
            cur = self.db.connection.cursor()
            try:
                cur.execute(
                    """UPDATE intel_report_candidates
                       SET status=?, last_error=?, attempt_count=attempt_count+1, updated_at=datetime('now')
                       WHERE id=?""",
                    (str(status or "failed"), str(last_error or "")[:1000], int(candidate_id)),
                )
                self.db.connection.commit()
            finally:
                cur.close()


intel_report_service = IntelReportService()
