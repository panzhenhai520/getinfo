# -*- coding: utf-8 -*-

"""T2.5 「动态/新闻」栏目非标准结构兜底抽取（0 次 LLM）。

对没有标准文章列表卡片结构的页面（滚动快讯流、表格型列表、公告栏、
JS 数据渲染页）按以下链路抽取条目 {title, url, date}：
  1. JSON-LD / 微数据：ItemList、NewsArticle、ListItem 条目
  2. 嵌入 JS 数据对象：<script> 内 JSON（含 title/url/date 类字段）
  3. DOM 行对聚类：标题行（≥8 字锚文本）+ 相邻日期行（日期正则）
  4. 表格型公告栏：<table> 行内链接 + 日期单元格
全部失败返回空列表；调用方（扫描层）再走 VPN OCR 兜底。
"""

import json
import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

_DATE_VALUE_RE = re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})")
_JSON_BLOCK_RE = re.compile(
    r"<script[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL
)
# JS 数据对象里常见的文章字段名
_TITLE_KEYS = ("title", "name", "headline", "标题", "bt", "subject")
_URL_KEYS = ("url", "link", "href", "url_show", "链接")
_DATE_KEYS = ("date", "pubDate", "publish_date", "published_at", "time", "日期", "fbsj", "created")


def _norm_date(value) -> str:
    match = _DATE_VALUE_RE.search(str(value or ""))
    if not match:
        return ""
    return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def _dedupe(items: list) -> list:
    seen = set()
    result = []
    for item in items:
        key = (item.get("title") or "", item.get("url") or "")
        if not item.get("title") or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _extract_structured_data(soup: BeautifulSoup, base_url: str) -> list:
    """JSON-LD ItemList/NewsArticle/ListItem。"""
    items = []
    for script in soup.find_all("script", type="application/ld+json"):
        text = str(script.string or script.get_text() or "").strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            continue
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph") or []
            entries = []
            for key in ("itemListElement", "items", "articles", "news"):
                if isinstance(node.get(key), list):
                    entries.extend(node[key])
            for graph_node in graph:
                if isinstance(graph_node, dict):
                    for key in ("itemListElement", "items", "articles", "news"):
                        if isinstance(graph_node.get(key), list):
                            entries.extend(graph_node[key])
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                inner = entry.get("item") if isinstance(entry.get("item"), dict) else entry
                title = str(inner.get("name") or inner.get("headline") or entry.get("name") or "").strip()
                url = str(inner.get("url") or entry.get("url") or "").strip()
                date = _norm_date(inner.get("datePublished") or entry.get("datePublished") or "")
                if title:
                    items.append({"title": title, "url": urljoin(base_url, url) if url else "", "date": date})
            if isinstance(node, dict) and node.get("@type") == "NewsArticle":
                title = str(node.get("headline") or node.get("name") or "").strip()
                url = str(node.get("url") or "").strip()
                date = _norm_date(node.get("datePublished") or "")
                if title:
                    items.append({"title": title, "url": urljoin(base_url, url) if url else "", "date": date})
    return _dedupe(items)


def _extract_embedded_json(html: str, base_url: str) -> list:
    """<script> 内嵌 JSON 数据（含 title/url/date 字段的对象或数组）。"""
    items = []
    for block in _JSON_BLOCK_RE.findall(str(html or "")):
        text = block.strip()
        if not text or text.startswith(("function", "(", "/*")):
            continue
        if text.startswith(("window.", "var ", "let ", "const ")):
            # JS 赋值语句：取第一个 '=' 之后的 JSON 片段
            eq = text.find("=")
            if eq < 0:
                continue
            text = text[eq + 1:].strip().rstrip(";").strip()
        candidates = []
        start = text.find("{")
        if start < 0:
            start = text.find("[")
        if start < 0:
            continue
        try:
            candidates = [json.loads(text[start:])]
        except (TypeError, ValueError):
            # 逐段提取每个 {...} 再试
            depth = 0
            begin = None
            for index, ch in enumerate(text):
                if ch == "{":
                    if depth == 0:
                        begin = index
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0 and begin is not None:
                        try:
                            candidates.append(json.loads(text[begin:index + 1]))
                        except (TypeError, ValueError):
                            pass
                        begin = None
        for candidate in candidates:
            if isinstance(candidate, list):
                for row in candidate:
                    if isinstance(row, dict):
                        items.append(_row_to_item(row, base_url))
            elif isinstance(candidate, dict):
                items.append(_row_to_item(candidate, base_url))
                for key in ("data", "list", "items", "result", "rows", "newsList", "articleList"):
                    value = candidate.get(key)
                    if isinstance(value, list):
                        for row in value:
                            if isinstance(row, dict):
                                items.append(_row_to_item(row, base_url))
    return _dedupe(items)


def _row_to_item(row: dict, base_url: str) -> dict:
    title = ""
    for key in _TITLE_KEYS:
        if str(row.get(key) or "").strip():
            title = str(row[key]).strip()
            break
    url = ""
    for key in _URL_KEYS:
        if str(row.get(key) or "").strip():
            url = str(row[key]).strip()
            break
    date = ""
    for key in _DATE_KEYS:
        parsed = _norm_date(row.get(key) or "")
        if parsed:
            date = parsed
            break
    if not title:
        return {}
    return {"title": title, "url": urljoin(base_url, url) if url else "", "date": date}


def _extract_line_pairs(soup: BeautifulSoup, base_url: str) -> list:
    """DOM 行对聚类：标题行（锚文本或父级文本）+ 相邻/父级日期行。"""
    items = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        if href.startswith(("javascript:", "mailto:", "#")):
            continue
        title = anchor.get_text(" ", strip=True)
        if len(title) < 6:
            # 快讯流常见「短锚文本 + 父级完整句子」：取父级文本作标题
            parent = anchor.parent
            parent_text = parent.get_text(" ", strip=True) if parent else ""
            title = parent_text
            if len(title) < 6:
                continue
        date = ""
        parent = anchor.parent
        for _ in range(4):
            if parent is None:
                break
            parent_text = parent.get_text(" ", strip=True)
            parsed = _norm_date(parent_text)
            if parsed:
                date = parsed
                break
            parent = parent.parent
        items.append({"title": title[:120], "url": urljoin(base_url, href), "date": date})
    return _dedupe(items)


def _extract_table_rows(soup: BeautifulSoup, base_url: str) -> list:
    """表格型公告栏：每行一个链接 + 日期单元格。"""
    items = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            anchor = tr.find("a", href=True)
            if anchor is None:
                continue
            title = anchor.get_text(" ", strip=True)
            if len(title) < 6:
                continue
            row_text = tr.get_text(" ", strip=True)
            items.append({
                "title": title[:120],
                "url": urljoin(base_url, str(anchor.get("href") or "")),
                "date": _norm_date(row_text),
            })
    return _dedupe(items)


def extract_nonstandard_items(html: str, base_url: str = "") -> dict:
    """非标准结构兜底抽取主入口：返回 {items, method}。"""
    try:
        soup = BeautifulSoup(str(html or ""), "html.parser")
    except Exception:
        soup = BeautifulSoup("", "html.parser")
    items = _extract_structured_data(soup, base_url or "")
    if items:
        return {"items": items, "method": "structured_data"}
    items = _extract_embedded_json(str(html or ""), base_url or "")
    if items:
        return {"items": items, "method": "embedded_json"}
    items = _extract_line_pairs(soup, base_url or "")
    if items:
        return {"items": items, "method": "line_pairs"}
    items = _extract_table_rows(soup, base_url or "")
    if items:
        return {"items": items, "method": "table_rows"}
    return {"items": [], "method": "none"}
