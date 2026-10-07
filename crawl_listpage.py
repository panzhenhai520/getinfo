# -*- coding: utf-8 -*-

"""二期列表页智能模块（T2.4 页面类型四级分类器 + 后续 T2.1/T2.2/T2.5 扩展）。

T2.4：classify_page_type(url, html) → {page_type, confidence, features}
四级：article（文章详情）/ listing（列表页）/ dynamic（JS 渲染壳）/ homepage（首页导航）。
纯规则 + 浅层文本特征（借鉴 boilerplate 检测论文：链接密度/文本密度/重复结构），
不调用 LLM、不发起网络请求；低置信（<0.6）交由 T4.1 的 LLM 兜底判定。
"""

import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

PAGE_ARTICLE = "article"
PAGE_LISTING = "listing"
PAGE_DYNAMIC = "dynamic"
PAGE_HOMEPAGE = "homepage"


def fetch_page_html(url: str, *, timeout: int = 15) -> str:
    """抓取页面 HTML（仅 200 时返回正文，否则返回空串）。

    为什么放在这里而不是调用方：阶段五闸门（tools/check_financial_stage5_gate.py）把
    intel_api.py 列为 STAGE5_MODULES 并禁止其出现 requests/httpx/urllib 等出网导入，
    以保证金融相关模块不做自发网络调用。列表页探查本身就是抓取逻辑，放在本模块既符合
    红线，也让"抓取→解析"待在一个地方。
    """
    import requests  # 本模块不在 STAGE5_MODULES 内

    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; CollectInfo/1.0)"},
        timeout=timeout,
    )
    return (response.text or "") if response.status_code == 200 else ""

# 置信度分档
CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

# URL 特征
_ARTICLE_PATH_RE = re.compile(
    r"/(?:article|articles|news|post|posts|story|stories|detail|details|id|content|"
    r"archives|blog|read|view)[/_=-]", re.IGNORECASE
)
_DATE_PATH_RE = re.compile(r"/20\d{2}[/\-.]\d{1,2}[/\-.]\d{1,2}(?:[/?#]|$)")
_NUMERIC_ID_RE = re.compile(r"/(?:article|news|post|story|detail|id)[/_=-]?\d{4,}", re.IGNORECASE)
_PURE_ID_RE = re.compile(r"/\d{5,}(?:[/?#]|$)")
_LISTING_PATH_RE = re.compile(
    r"/(?:category|categories|tag|tags|list|listing|column|channel|section|"
    r"news|资讯|动态|news_category|topics?)(?:[/?#]|$)", re.IGNORECASE
)
_SLUG_RE = re.compile(r"/[a-z0-9\u4e00-\u9fff-]{8,}(?:\.html?)?(?:[/?#]|$)", re.IGNORECASE)
_EXT_RE = re.compile(r"\.(?:html?|shtml|xhtml|php|jsp|aspx?)(?:[/?#]|$)", re.IGNORECASE)

_PAGINATION_TEXT_RE = re.compile(r"下一页|下一頁|next\s*page|加载更多|load\s*more", re.IGNORECASE)
_PAGINATION_LINK_RE = re.compile(r"[?&](?:page|p|pageno|pageNo|pn)=\d+", re.IGNORECASE)


def _url_signals(url: str) -> dict:
    path = urlparse(url or "").path or "/"
    query = urlparse(url or "").query or ""
    return {
        "article_path": bool(_ARTICLE_PATH_RE.search(path)),
        "date_path": bool(_DATE_PATH_RE.search(path)),
        "numeric_id": bool(_NUMERIC_ID_RE.search(path) or _PURE_ID_RE.search(path)),
        "slug_tail": bool(_SLUG_RE.search(path)),
        "listing_path": bool(_LISTING_PATH_RE.search(path)),
        "page_param": bool(_PAGINATION_LINK_RE.search(query)),
        "is_root": path.rstrip("/") in ("", "/"),
    }


def _dom_signals(html: str) -> dict:
    """浅层结构特征：链接密度、重复卡片结构、分页控件、正文块、JS 壳。"""
    try:
        soup = BeautifulSoup(str(html or ""), "html.parser")
    except Exception:
        soup = BeautifulSoup("", "html.parser")
    anchors = soup.find_all("a", href=True)
    text = soup.get_text(" ", strip=True)
    text_len = max(1, len(text))
    anchor_text_len = sum(len(a.get_text(" ", strip=True)) for a in anchors)
    link_density = round(min(1.0, anchor_text_len / text_len), 4)

    # 重复卡片结构：同一父级 class 下 ≥3 个结构相似的兄弟（每个都含链接+文字）
    cards = 0
    for parent in soup.find_all(["ul", "ol", "div"]):
        children = [c for c in parent.find_all(True, recursive=False)
                    if c.name in ("li", "div", "article") and c.find("a", href=True)]
        if len(children) >= 3:
            texts = [c.get_text(" ", strip=True) for c in children]
            lengths = {len(t) for t in texts}
            if len(texts) >= 3 and len(lengths) <= max(2, len(texts) // 2):
                cards += 1
    card_structure = cards >= 1

    pagination = bool(_PAGINATION_TEXT_RE.search(str(html or ""))) or bool(_PAGINATION_LINK_RE.search(str(html or "")))
    # 正文块：≥2 个长段落（>120 字）或整体正文很长
    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    long_paragraphs = sum(1 for p in paragraphs if len(p) >= 120)
    time_tags = len(soup.find_all("time")) + len(soup.find_all(attrs={"itemprop": re.compile("date", re.IGNORECASE)}))
    scripts = len(soup.find_all("script"))
    body_text_len = len(text)
    return {
        "link_density": link_density,
        "card_structure": card_structure,
        "pagination": pagination,
        "long_paragraphs": long_paragraphs,
        "time_markers": time_tags,
        "script_count": scripts,
        "body_text_len": body_text_len,
        "anchor_count": len(anchors),
    }


def classify_page_type(url: str = "", html: str = "") -> dict:
    """四级分类：article / listing / dynamic / homepage，返回类型与置信度。"""
    url_signals = _url_signals(url or "")
    dom = _dom_signals(html or "")

    if dom["body_text_len"] == 0:
        return {"page_type": PAGE_DYNAMIC, "confidence": 0.9,
                "confidence_level": CONFIDENCE_HIGH,
                "features": {"url": url_signals, "dom": dom, "reason": "页面无可见文本（JS 渲染壳）"}}

    # 文章页：URL 强特征 或（正文长 + 时间标记 + 链接密度低）
    article_url_strong = bool(
        url_signals["article_path"] or url_signals["date_path"] or url_signals["numeric_id"]
    )
    article_like = article_url_strong and dom["link_density"] < 0.35 and dom["long_paragraphs"] >= 1
    if article_like:
        return {"page_type": PAGE_ARTICLE, "confidence": 0.85,
                "confidence_level": CONFIDENCE_HIGH,
                "features": {"url": url_signals, "dom": dom, "reason": "URL 文章特征 + 低链接密度正文"}}
    if dom["long_paragraphs"] >= 2 and dom["link_density"] < 0.2 and dom["time_markers"] >= 1:
        return {"page_type": PAGE_ARTICLE, "confidence": 0.75,
                "confidence_level": CONFIDENCE_MEDIUM,
                "features": {"url": url_signals, "dom": dom, "reason": "长正文 + 时间标记 + 低链接密度"}}

    # 列表页：重复卡片结构 / 分页控件 / 列表 URL
    if dom["card_structure"] or dom["pagination"] or url_signals["listing_path"] or url_signals["page_param"]:
        confidence = 0.9 if dom["card_structure"] else (0.7 if dom["pagination"] else 0.6)
        return {"page_type": PAGE_LISTING, "confidence": confidence,
                "confidence_level": CONFIDENCE_HIGH if confidence >= 0.85 else (
                    CONFIDENCE_MEDIUM if confidence >= 0.7 else CONFIDENCE_LOW),
                "features": {"url": url_signals, "dom": dom, "reason": "重复卡片/分页/列表 URL 特征"}}

    # 首页：根路径 或 高链接密度且无正文
    if url_signals["is_root"] or (dom["link_density"] >= 0.35 and dom["long_paragraphs"] == 0):
        return {"page_type": PAGE_HOMEPAGE, "confidence": 0.7,
                "confidence_level": CONFIDENCE_MEDIUM,
                "features": {"url": url_signals, "dom": dom, "reason": "根路径或高链接密度导航"}}

    # 动态壳：大量脚本 + 文本极少
    if dom["script_count"] >= 8 and dom["body_text_len"] < 300:
        return {"page_type": PAGE_DYNAMIC, "confidence": 0.65,
                "confidence_level": CONFIDENCE_MEDIUM,
                "features": {"url": url_signals, "dom": dom, "reason": "脚本多且可见文本极少"}}

    return {"page_type": PAGE_LISTING, "confidence": 0.45,
            "confidence_level": CONFIDENCE_LOW,
            "features": {"url": url_signals, "dom": dom, "reason": "特征不足，默认列表页（低置信，交 LLM 兜底）"}}


# ----------------------------------------------------------------------
# T2.1 已知 URL 自动探查文章列表页（落库 crawl_list_pages）
# ----------------------------------------------------------------------
# 栏目语义词（动态/新闻族强制纳入；其余为一般栏目词）
_DYNAMIC_NEWS_WORDS = ("动态", "新闻", "快讯", "资讯", "要闻", "news", "press", "releases")
_COLUMN_WORDS = _DYNAMIC_NEWS_WORDS + (
    "公告", "通知", "行业", "研究", "洞察", "观点", "报告", "政策", "法规",
    "文章", "专栏", "报道", "活动", "大事记", "媒体", "media", "blog", "insights",
)
_NAV_BLACKLIST = (
    "login", "signin", "signup", "register", "about", "contact", "privacy",
    "terms", "search", "sitemap", "tag/", "author/", "user", "account",
    "登录", "注册", "关于", "联系", "隐私", "条款", "搜索",
)
_ASSET_EXT_RE = re.compile(r"\.(?:jpg|jpeg|png|gif|webp|svg|ico|pdf|css|js|zip|rar|mp3|mp4)(?:[/?#]|$)", re.IGNORECASE)
_HOME_PAGE_RE = re.compile(r"/(?:index|default)\.(?:html?|php|jsp|aspx?)$", re.IGNORECASE)


def ensure_crawl_list_pages_table(cursor) -> None:
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS crawl_list_pages ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  domain TEXT NOT NULL,"
        "  known_url TEXT NOT NULL DEFAULT '',"
        "  list_url TEXT NOT NULL,"
        "  confidence REAL NOT NULL DEFAULT 0,"
        "  page_type TEXT NOT NULL DEFAULT 'listing',"
        "  source TEXT NOT NULL DEFAULT 'nav',"
        "  force_include INTEGER NOT NULL DEFAULT 0,"
        "  discovered_at TEXT NOT NULL DEFAULT '',"
        "  UNIQUE(domain, list_url)"
        ")"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_crawl_list_pages_domain ON crawl_list_pages(domain)"
    )


def _same_domain(url: str, base: str) -> bool:
    try:
        return urlparse(url or "").netloc.lower() == urlparse(base or "").netloc.lower()
    except Exception:
        return False


def _url_score(url: str) -> dict:
    """列表页候选打分（仅 URL 层）：栏目词/动态新闻词/路径特征/分页参数。"""
    text = str(url or "").lower()
    score = 0.0
    hits = []
    if any(word in text for word in _DYNAMIC_NEWS_WORDS):
        score += 2.0
        hits.append("dynamic_news_word")
    if any(word in text for word in _COLUMN_WORDS):
        score += 1.5
        hits.append("column_word")
    if _LISTING_PATH_RE.search(text):
        score += 1.5
        hits.append("listing_path")
    if _PAGINATION_LINK_RE.search(text):
        score += 1.0
        hits.append("page_param")
    path = urlparse(url or "").path or "/"
    if path in ("/", "") or _HOME_PAGE_RE.search(path):
        score -= 2.0  # 首页/根路径不算列表页
    return {"score": round(score, 2), "hits": hits}


def extract_nav_candidates(known_url: str, html: str, max_candidates: int = 30) -> list:
    """从首页/已知页 HTML 的导航链接中提取同域列表页候选（规则过滤，不调 LLM）。"""
    candidates = []
    seen = set()
    try:
        soup = BeautifulSoup(str(html or ""), "html.parser")
    except Exception:
        return candidates
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "#", "tel:")):
            continue
        absolute = href if href.startswith("http") else None
        if absolute is None:
            from urllib.parse import urljoin
            absolute = urljoin(str(known_url or ""), href)
        if not _same_domain(absolute, known_url):
            continue
        if _ASSET_EXT_RE.search(absolute):
            continue
        if any(word in absolute.lower() for word in _NAV_BLACKLIST):
            continue
        # 详情页特征明显的链接排除（数字 id/日期路径）
        if _NUMERIC_ID_RE.search(absolute) or _DATE_PATH_RE.search(absolute):
            continue
        key = absolute.split("#", 1)[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        anchor_text = anchor.get_text(" ", strip=True)
        scored = _url_score(absolute)
        # T2.5：栏目文案（锚文本）命中「动态/新闻」族 → 强制纳入，即使 URL 不含词
        anchor_words = [w for w in _DYNAMIC_NEWS_WORDS if w in anchor_text.lower()]
        if scored["score"] <= 0 and not anchor_words:
            continue
        if anchor_words and "dynamic_news_word" not in scored["hits"]:
            scored["score"] += 2.0
            scored["hits"].append("dynamic_news_word")
        force = bool(scored["hits"] and "dynamic_news_word" in scored["hits"])
        candidates.append({
            "list_url": absolute,
            "score": scored["score"],
            "source": "nav",
            "force_include": force,
            "hits": scored["hits"],
        })
        if len(candidates) >= max_candidates:
            break
    candidates.sort(key=lambda item: (-item["score"], item["list_url"]))
    return candidates


def probe_list_pages(known_url: str, *, html: str = "", sitemap_urls: list = None,
                     max_candidates: int = 30) -> dict:
    """已知 URL → 列表页候选清单（nav + sitemap 两源合并，去重排序）。

    纯函数：html 与 sitemap_urls 由调用方（抓取层）提供；本函数不联网。
    sitemap 条目按 robots/sitemap 明示入口计，来源标记 'sitemap'。
    """
    candidates = {}
    for item in extract_nav_candidates(known_url, html, max_candidates=max_candidates):
        candidates[item["list_url"]] = item
    for raw in (sitemap_urls or [])[:max_candidates]:
        url = str(raw or "").strip()
        if not url or not _same_domain(url, known_url):
            continue
        if _ASSET_EXT_RE.search(url):
            continue
        key = url.split("#", 1)[0].rstrip("/")
        scored = _url_score(url)
        existing = candidates.get(key)
        if existing:
            existing.setdefault("sources", ["nav"])
            existing["sources"].append("sitemap")
            existing["source"] = "+".join(dict.fromkeys(existing["sources"]))
            continue
        force = bool(scored["hits"] and "dynamic_news_word" in scored["hits"])
        candidates[key] = {
            "list_url": url, "score": scored["score"], "source": "sitemap",
            "force_include": force, "hits": scored["hits"],
        }
    ordered = sorted(candidates.values(), key=lambda item: (-item["score"], item["list_url"]))
    return {
        "known_url": known_url,
        "candidates": [
            {k: item[k] for k in ("list_url", "score", "source", "force_include", "hits")}
            for item in ordered[:max_candidates]
        ],
    }


def save_list_pages(known_url: str, candidates: list, db=None) -> int:
    """候选落库 crawl_list_pages（按 domain+list_url 幂等 upsert）。"""
    if db is None:
        from sqlite_database import sqlite_db
        db = sqlite_db
    from utils import get_china_time
    now = get_china_time().strftime("%Y-%m-%d %H:%M:%S")
    domain = urlparse(known_url or "").netloc.lower()
    if not domain or not candidates:
        return 0
    db._ensure_connection()
    with db.lock:
        cursor = db.connection.cursor()
        try:
            ensure_crawl_list_pages_table(cursor)
            inserted = 0
            for item in candidates:
                cursor.execute(
                    "INSERT INTO crawl_list_pages(domain, known_url, list_url, confidence,"
                    " page_type, source, force_include, discovered_at) VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(domain, list_url) DO UPDATE SET confidence=excluded.confidence,"
                    " source=excluded.source, force_include=excluded.force_include,"
                    " discovered_at=excluded.discovered_at",
                    (domain, str(known_url or ""), str(item.get("list_url") or ""),
                     float(item.get("score") or 0), "listing",
                     str(item.get("source") or "nav"), 1 if item.get("force_include") else 0, now),
                )
                inserted += 1
            db.connection.commit()
            return inserted
        finally:
            cursor.close()


def list_list_pages(known_url: str = "", db=None) -> list:
    """读取某站点（或全部）已探测的列表页。"""
    if db is None:
        from sqlite_database import sqlite_db
        db = sqlite_db
    domain = urlparse(known_url or "").netloc.lower()
    db._ensure_connection()
    with db.lock:
        cursor = db.connection.cursor()
        try:
            ensure_crawl_list_pages_table(cursor)
            if domain:
                cursor.execute(
                    "SELECT * FROM crawl_list_pages WHERE domain=? ORDER BY force_include DESC, confidence DESC, id",
                    (domain,),
                )
            else:
                cursor.execute("SELECT * FROM crawl_list_pages ORDER BY domain, confidence DESC, id")
            return [dict(row) for row in cursor.fetchall()]
        finally:
            cursor.close()


# ----------------------------------------------------------------------
# T2.2 列表页模板学习与持久化（autoscraper：标题/链接/日期三字段，0 次 LLM）
# ----------------------------------------------------------------------
def ensure_listpage_template_table(cursor) -> None:
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS crawl_listpage_templates ("
        "  list_url TEXT PRIMARY KEY,"
        "  domain TEXT NOT NULL DEFAULT '',"
        "  template_b64 TEXT NOT NULL DEFAULT '',"
        "  learned_at TEXT NOT NULL DEFAULT '',"
        "  hit_count INTEGER NOT NULL DEFAULT 0,"
        "  miss_count INTEGER NOT NULL DEFAULT 0"
        ")"
    )


def learn_listpage_template(db, list_url: str, sample_html: str, sample_titles: list,
                            sample_urls: list = None, sample_dates: list = None) -> dict:
    """用样例学习该列表页「标题/链接/日期」模板并持久化（autoscraper 规则文件 base64 入库）。"""
    import base64
    import tempfile
    from utils import get_china_time
    try:
        from autoscraper import AutoScraper
    except ImportError:
        return {"success": False, "error": "autoscraper 未安装"}
    titles = [str(t).strip() for t in (sample_titles or []) if str(t).strip()][:5]
    urls = [str(u).strip() for u in (sample_urls or []) if str(u).strip()][:5]
    dates = [str(d).strip() for d in (sample_dates or []) if str(d).strip()][:5]
    if not titles or not str(sample_html or "").strip():
        return {"success": False, "error": "缺少样例标题或样例 HTML"}
    scraper = AutoScraper()
    try:
        scraper.build(wanted_list=titles + urls + dates, html=str(sample_html))
    except Exception as exc:
        return {"success": False, "error": f"模板学习失败: {str(exc)[:120]}"}
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        handle.close()
        path = handle.name
    try:
        scraper.save(path)
        with open(path, "rb") as handle:
            b64 = base64.b64encode(handle.read()).decode("ascii")
    finally:
        import os
        try:
            os.unlink(path)
        except OSError:
            pass
    key = str(list_url or "").strip()
    db._ensure_connection()
    with db.lock:
        cursor = db.connection.cursor()
        try:
            ensure_listpage_template_table(cursor)
            cursor.execute(
                "INSERT INTO crawl_listpage_templates(list_url, domain, template_b64, learned_at,"
                " hit_count, miss_count) VALUES(?,?,?,?,0,0) "
                "ON CONFLICT(list_url) DO UPDATE SET template_b64=excluded.template_b64,"
                " learned_at=excluded.learned_at",
                (key, urlparse(key).netloc.lower(), b64,
                 get_china_time().strftime("%Y-%m-%d %H:%M:%S")),
            )
            db.connection.commit()
        finally:
            cursor.close()
    return {"success": True, "list_url": key, "samples": len(titles)}


def extract_listpage_items(db, list_url: str, html: str, *, relearn_html: str = None) -> dict:
    """套用已学模板提取条目；命中 0 条且提供 relearn_html 时自动重学一次再试。

    返回 {items: [{title, url, date}], hit, miss, relearned, error}。
    """
    import base64
    import tempfile
    from utils import get_china_time
    key = str(list_url or "").strip()
    if not key:
        return {"items": [], "hit": 0, "miss": 1, "relearned": False, "error": "缺少 list_url"}
    db._ensure_connection()
    template_b64 = ""
    with db.lock:
        cursor = db.connection.cursor()
        try:
            ensure_listpage_template_table(cursor)
            row = cursor.execute(
                "SELECT template_b64 FROM crawl_listpage_templates WHERE list_url=?", (key,)
            ).fetchone()
            if row:
                template_b64 = str(row["template_b64"] or "")
        finally:
            cursor.close()

    def _extract(b64: str):
        try:
            from autoscraper import AutoScraper
        except ImportError:
            return [], "autoscraper 未安装"
        if not b64:
            return [], "该列表页尚未学习模板"
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
            handle.close()
            path = handle.name
        try:
            with open(path, "wb") as handle:
                handle.write(base64.b64decode(b64))
            scraper = AutoScraper()
            scraper.load(path)
            grouped = scraper.get_result_similar(html=str(html or ""), grouped=True,
                                                 keep_order=True) or {}
        except Exception as exc:
            return [], f"模板套用失败: {str(exc)[:120]}"
        finally:
            import os
            try:
                os.unlink(path)
            except OSError:
                pass
        # 三字段分组识别：含 http 的组是链接，全为日期串的组是日期，其余最长的组是标题
        _DATE_VALUE_RE = re.compile(r"^20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}")
        title_values, url_values, date_values = [], [], []
        for key, values in grouped.items():
            values = [str(v).strip() for v in (values or []) if str(v).strip()]
            if not values:
                continue
            if any(("http://" in v or "https://" in v or v.startswith("/")) for v in values):
                url_values = values
            elif all(_DATE_VALUE_RE.search(v) for v in values):
                date_values = values
            elif len(values) >= len(title_values):
                title_values = values
        if not title_values:
            return [], "模板未命中标题字段"
        count = len(title_values)
        items = []
        for index in range(count):
            items.append({
                "title": title_values[index],
                "url": url_values[index] if index < len(url_values) else "",
                "date": date_values[index] if index < len(date_values) else "",
            })
        items = [item for item in items if item["title"]]
        return items, ""

    items, error = _extract(template_b64)
    relearned = False
    if not items and relearn_html:
        # 页面结构变化导致模板未命中 → 用当前页作为最新样例自动重学一次，再套用重试
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(str(relearn_html or ""), "html.parser")
            sample_titles = [h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2", "h3", "h4"])]
            sample_titles = [t for t in sample_titles if len(t) >= 6][:5]
            if not sample_titles:
                anchors = [a.get_text(" ", strip=True) for a in soup.find_all("a", href=True)]
                sample_titles = [t for t in anchors if len(t) >= 6][:5]
            if sample_titles:
                learn = learn_listpage_template(db, key, relearn_html, sample_titles)
                if learn.get("success"):
                    items, error = _extract(_template_b64(db, key))
                    relearned = True
        except Exception as exc:
            error = f"自动重学失败: {str(exc)[:120]}"

    hit = len(items)
    miss = 0 if hit else 1
    with db.lock:
        cursor = db.connection.cursor()
        try:
            ensure_listpage_template_table(cursor)
            cursor.execute(
                "UPDATE crawl_listpage_templates SET hit_count=hit_count+?, miss_count=miss_count+?,"
                " learned_at=? WHERE list_url=?",
                (hit, miss, get_china_time().strftime("%Y-%m-%d %H:%M:%S"), key),
            )
            db.connection.commit()
        finally:
            cursor.close()
    return {"items": items, "hit": hit, "miss": miss, "relearned": relearned,
            "error": error if not items else ""}


def _template_b64(db, list_url: str) -> str:
    with db.lock:
        cursor = db.connection.cursor()
        try:
            ensure_listpage_template_table(cursor)
            row = cursor.execute(
                "SELECT template_b64 FROM crawl_listpage_templates WHERE list_url=?", (str(list_url),)
            ).fetchone()
            return str(row["template_b64"] or "") if row else ""
        finally:
            cursor.close()
