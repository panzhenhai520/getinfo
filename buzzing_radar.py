#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""buzzing.cc 海外财经/科技标题雷达：只用标题，不抓正文、不占爬取槽。

为什么是"只用标题"：
buzzing.cc 是聚合站，每条只给「中文译名 + 英文原标题 + 出处 + 时间 + 原文跳转链」，
没有正文。而我们的证据池要求正文达字数线并过关键词闸门，所以标题本身当不了证据——
硬当候选只会白占一个全文抓取槽。它的正确用法是**信号**，不是**证据**：

  1. 趋势信号：把标题里的实体/主题按天计数，看海外今天在炒什么；
  2. 覆盖度审计（最有价值的一项）：拿这些热词去问"我们自己这两天有没有覆盖"，
     直接暴露"海外在炒、我们一篇都没写"的缺口；
  3. 词表缺口：海外在炒、但我们行业包词表里根本没有的词——提示该补词。

本模块**不写数据库、不产生候选、不发候选派发**：全部按需实时计算 + 进程内短缓存，
所以它对爬取产能是零成本。真正的"抓正文"路径（解析 Google News 跳转、
只放行可抓域名、过许可证 profile）是后续独立的一步，不在本模块里。
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

import config

# 默认只订阅财经子站（与我们最相关的海外财经标题雷达）
DEFAULT_FEEDS = ("https://finance.buzzing.cc/feed.xml",)

# 已核实的 buzzing 子站（需要在 .env 里显式配置才会订阅）
KNOWN_FEEDS = {
    "finance": "https://finance.buzzing.cc/feed.xml",
    "stocks": "https://stocks.buzzing.cc/feed.xml",
    "bbg": "https://bbg.buzzing.cc/feed.xml",
    "ft": "https://ft.buzzing.cc/feed.xml",
    "wsj": "https://wsj.buzzing.cc/feed.xml",
    "tech": "https://tech.buzzing.cc/feed.xml",
    "crypto": "https://crypto.buzzing.cc/feed.xml",
}

# 通用财经/科技实体词：即使行业包词表里没有，也先认出来，避免"新词"里全是噪声
FINANCE_LEXICON = (
    "英伟达", "Nvidia", "台积电", "TSMC", "阿斯麦", "ASML", "三星", "SK海力士",
    "OpenAI", "Anthropic", "谷歌", "Google", "微软", "Microsoft", "苹果", "Apple",
    "Meta", "亚马逊", "Amazon", "特斯拉", "Tesla", "SpaceX", "马斯克", "Musk",
    "美联储", "Fed", "鲍威尔", "Powell", "欧洲央行", "ECB", "日本央行", "BOJ",
    "美国国债", "国债收益率", "油价", "原油", "黄金", "铜价", "比特币", "Bitcoin",
    "加密货币", "美联储降息", "加息", "通胀", "CPI", "非农", "关税", "贸易战",
    "半导体", "芯片", "算力", "数据中心", "核电", "稀土", "锂电", "光伏",
    "IPO", "财报", "回购", "并购", "破产", "违约", "主权基金", "对冲基金",
    "私募股权", "家族办公室", "财富管理", "REITs", "ETF", "标普500", "纳斯达克",
    "道琼斯", "恒生指数", "日经", "美元指数", "人民币", "日元",
)

# 停用词/噪声词：中文标题里高频但无信息量的词，以及英文功能词。
# 英文标题只接受"首字母大写/全大写缩写"的词，这里再兜一层常见噪声。
STOPWORDS = frozenset({
    # 中文：通用叙述词、无指向的地域/主体词
    "为什么", "怎么", "如何", "什么", "今日", "今日股市", "市场", "市场综述", "报道",
    "消息", "分析", "观点", "最新", "重磅", "突发", "注意", "或将", "可能", "预计",
    "美国", "中国", "全球", "投资者", "公司", "经济", "股市", "本周", "上月", "今年",
    "分析师", "数据显示", "上涨", "下跌", "创下", "首次", "计划", "表示", "认为",
    "一个", "这家", "该项", "这种", "目前", "已经", "正在", "依然", "仍然", "继续",
    "新高", "新低", "股价", "随着", "亿美元", "亿元", "历史", "美股", "港股", "A股",
    "大涨", "暴跌", "收盘", "开盘", "盘中", "涨幅", "跌幅", "头条", "消息面", "年内",
    "华尔街", "投资者们", "美联储主席", "这家公司", "该公司", "数据显示",
    # 英文：功能词 + 聚合站/新闻站常见噪声
    "the", "and", "for", "but", "not", "you", "are", "its", "has", "have", "was",
    "were", "will", "would", "could", "should", "with", "from", "that", "this",
    "these", "those", "after", "before", "amid", "into", "onto", "over", "under",
    "while", "when", "where", "what", "why", "how", "who", "whom", "which", "than",
    "then", "there", "here", "they", "them", "their", "his", "her", "our", "your",
    "says", "said", "report", "reports", "news", "market", "markets", "stock",
    "stocks", "today", "week", "month", "year", "new", "also", "more", "most",
    "some", "such", "about", "against", "between", "because", "been", "being",
    "google", "com", "www", "http", "https", "html", "amp", "us", "uk", "eu", "ceo",
    "cfo", "ipo", "etf", "q1", "q2", "q3", "q4", "day", "days", "week", "weeks",
    # 标题体（Title Case）里会被大写、但不是实体的常见动词/泛名词
    "talks", "borrow", "buys", "buy", "buying", "billion", "million", "trillion",
    "unveils", "ships", "raises", "hits", "falls", "rises", "plans", "deal", "deals",
    "says", "warns", "sees", "eyes", "set", "gets", "takes", "makes", "gives",
    "chips", "shares", "sales", "profit", "profits", "loss", "losses", "growth",
    "price", "prices", "rate", "rates", "jobs", "cut", "cuts", "hike", "hikes",
    "first", "best", "worst", "top", "big", "biggest", "small", "major",
    "amid", "ahead", "back", "down", "up", "out", "off", "over", "under", "again",
    "why", "how", "what", "when", "where", "who", "all", "any", "can", "may",
    # 媒体名（已在"出处排行"里单独统计，不该混进热词）
    "reuters", "bloomberg", "financial", "times", "journal", "street", "cnbc",
    "yahoo", "finance", "cnn", "fox", "nbc", "cnbc.com", "verge", "seeking",
    "alpha", "motley", "fool", "insider", "business", "investor", "daily",
})

_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9&.\-]{1,24}")
# 英文标题里值得当实体的词：首字母大写（Nvidia/SpaceX）或全大写缩写（AI/CPI/Fed）
_PROPER_NOUN_RE = re.compile(r"^(?:[A-Z][a-z][A-Za-z0-9&.\-]*|[A-Z]{2,6})$")

# feed 拉取结果的进程内缓存：避免同一页面/接口反复请求同一个 feed
_FEED_CACHE: Dict[str, Dict[str, Any]] = {}


def _strip_urls(text: str) -> str:
    """去掉标题里的域名/URL：否则 news.google.com 会贡献出 "google"/"com" 这种噪声词。"""
    return re.sub(r"\b(?:https?://)?(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,6}(?:/\S*)?\b", " ", str(text or ""))


def configured_feeds() -> List[str]:
    raw = str(getattr(config, "BUZZING_RADAR_FEEDS", "") or "").strip()
    if not raw:
        return list(DEFAULT_FEEDS)
    feeds: List[str] = []
    for token in raw.replace("，", ",").split(","):
        value = token.strip()
        if not value:
            continue
        # 允许写子站别名（finance/stocks/...），也允许直接写完整 URL
        feeds.append(KNOWN_FEEDS.get(value.casefold(), value))
    return list(dict.fromkeys(feeds))


def _local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1].casefold()


def _element_text(element, names: Iterable[str]) -> str:
    wanted = {name.casefold() for name in names}
    for child in element:
        if _local_name(child.tag) in wanted:
            return " ".join("".join(child.itertext()).split())
    return ""


def parse_feed_entries(content: bytes, *, limit: int = 200) -> List[Dict[str, Any]]:
    """解析 Atom/RSS，保留出处（category）——这是原 rss_feed_contract 不返回的字段。"""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []
    entries: List[Dict[str, Any]] = []
    for element in root.iter():
        if _local_name(element.tag) not in {"item", "entry"}:
            continue
        title = _element_text(element, ("title",))
        # buzzing 的正文里是「英文原标题 + (news.google.com) + 时间 + #出处」，
        # summary 才是干净的英文原标题；两者都留，方便后续人工核对。
        english = _element_text(element, ("summary",))
        link = ""
        for child in list(element):
            if _local_name(child.tag) == "link":
                link = str(child.attrib.get("href") or "").strip() or _element_text(element, ("link",))
                if link:
                    break
        if not link:
            link = _element_text(element, ("link",))
        publisher = ""
        for child in list(element):
            if _local_name(child.tag) == "category":
                publisher = str(child.attrib.get("term") or "").strip() or "".join(child.itertext()).strip()
                if publisher:
                    break
        if not publisher:
            match = re.search(r"#([^#]{2,40})$", _element_text(element, ("content", "description")))
            publisher = match.group(1).strip() if match else ""
        published = _element_text(element, ("published", "pubdate", "updated", "date"))
        if not title and not english:
            continue
        entries.append({
            "title": title,
            "english_title": english,
            "publisher": publisher,
            "published_at": published,
            "url": link,
        })
        if len(entries) >= max(1, int(limit)):
            break
    return entries


def fetch_feed(feed_url: str, *, timeout: Optional[int] = None) -> Dict[str, Any]:
    """拉取一个 buzzing feed（带短缓存）。任何失败都返回空结果，不抛异常。"""
    url = str(feed_url or "").strip()
    result: Dict[str, Any] = {"feed": url, "entries": [], "error": ""}
    if not url:
        result["error"] = "未配置 feed"
        return result
    cache_seconds = int(getattr(config, "BUZZING_RADAR_CACHE_SECONDS", 600) or 600)
    cached = _FEED_CACHE.get(url)
    now = time.monotonic()
    if cached and now - cached["at"] < cache_seconds:
        return {**cached["value"], "cached": True}
    try:
        from intel_http import SafeHTTPClient

        client = SafeHTTPClient()
        # 海外站可能很慢：实测 A 机拉 finance.buzzing.cc 要 26 秒（本地几秒），
        # 用扫描器默认的 20 秒读取会直接超时，所以这里给雷达自己的更宽预算。
        timeout = (
            int(getattr(config, "BUZZING_RADAR_CONNECT_TIMEOUT_SECONDS", 10) or 10),
            int(getattr(config, "BUZZING_RADAR_READ_TIMEOUT_SECONDS", 60) or 60),
        )
        response = client.get(
            url,
            headers={"User-Agent": "MarketIntelRadar/1.0 (+headline-radar)",
                     "Accept": "application/atom+xml, application/xml, text/xml"},
            timeout=timeout,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        if not 200 <= status < 300:
            result["error"] = f"HTTP {status}"
            return result
        entries = parse_feed_entries(
            bytes(getattr(response, "content", b"") or b""),
            limit=int(getattr(config, "BUZZING_RADAR_MAX_ENTRIES", 200) or 200),
        )
        result["entries"] = entries
        _FEED_CACHE[url] = {"at": now, "value": result}
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return result


def clear_cache() -> None:
    """清缓存（测试与手工刷新用）。"""
    _FEED_CACHE.clear()


def _publisher_from_url(url: str) -> str:
    host = (urlsplit(str(url or "")).hostname or "").casefold()
    return host[4:] if host.startswith("www.") else host


def publisher_ranking(entries: Iterable[Dict], *, limit: int = 15) -> List[Dict[str, Any]]:
    """出处排行：哪些媒体今天在密集发声（海外信源覆盖度的直接证据）。"""
    counts: Dict[str, int] = {}
    for entry in entries or []:
        name = str(entry.get("publisher") or "").strip() or _publisher_from_url(entry.get("url") or "")
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
    return [
        {"publisher": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[: max(1, limit)]
    ]


def pack_vocabulary(pack_ids: Iterable[str]) -> List[str]:
    """行业包词表（核心/扩展/趋势/事件/品牌/锚点）——覆盖度审计的判定基准。"""
    terms: List[str] = []
    try:
        from industry_packs import industry_pack_loader
        from intel_candidates import industry_anchor_keywords

        for pack_id in pack_ids or []:
            try:
                pack = industry_pack_loader.load(str(pack_id))
            except Exception:
                continue
            for field in ("core_keywords", "expanded_keywords", "trend_keywords",
                          "event_keywords", "brands"):
                terms.extend(str(value).strip() for value in (pack.get(field) or []) if str(value).strip())
            terms.extend(str(value).strip() for value in industry_anchor_keywords(pack) if str(value).strip())
    except Exception:
        pass
    return list(dict.fromkeys(terms))


def _extract_terms(text: str, vocabulary: Iterable[str], *, latin_mode: bool = False) -> List[str]:
    """从标题里抽实体/主题。

    · 中文标题：分词后在停用词/长度/字符集上过滤；
    · 英文标题：只认"首字母大写或全大写缩写"的词——这正是 Nvidia/SpaceX/AI/CPI 的形态，
      而 to/in/as/com 这类功能词与域名碎片会被这条规则直接淘汰；
    · 词表（行业包关键词 + 通用财经词）命中一律保留，保证已知实体不因分词差异漏掉。
    """
    raw = _strip_urls(text)
    if not raw:
        return []
    lowered = raw.casefold()
    hits: List[str] = []
    for term in vocabulary or []:
        value = str(term).strip()
        if value and value.casefold() in lowered:
            hits.append(value)
    if latin_mode:
        candidates = _LATIN_RE.findall(raw)
        for token in candidates:
            if _PROPER_NOUN_RE.match(token) and token.casefold() not in STOPWORDS:
                hits.append(token)
    else:
        try:
            import jieba

            tokens = list(jieba.cut(raw))
        except Exception:
            tokens = []
            for chunk in _CJK_RE.findall(raw):
                tokens.append(chunk)
                tokens.extend(chunk[i:i + 3] for i in range(max(0, len(chunk) - 2)))
            tokens.extend(_LATIN_RE.findall(raw))
        for token in tokens:
            value = str(token or "").strip()
            if len(value) < 2 or len(value) > 25:
                continue
            if value.casefold() in STOPWORDS or value.isdigit():
                continue
            if re.fullmatch(r"[\u4e00-\u9fff]+", value):
                hits.append(value)
            elif _PROPER_NOUN_RE.match(value) and value in FINANCE_LEXICON:
                # 中文标题里夹带的英文只认已知实体，避免噪声
                hits.append(value)
    return list(dict.fromkeys(hits))


def _count_term(counts: Dict[str, Dict[str, Any]], term: str, entry: Dict) -> None:
    item = counts.setdefault(term, {"term": term, "count": 0, "publishers": set(), "titles": []})
    item["count"] += 1
    publisher = str(entry.get("publisher") or "").strip()
    if publisher:
        item["publishers"].add(publisher)
    if len(item["titles"]) < 3 and entry.get("title"):
        item["titles"].append(str(entry["title"])[:120])


def hot_terms(
    entries: Iterable[Dict],
    *,
    vocabulary: Iterable[str] = (),
    limit: int = 20,
    min_count: int = 1,
) -> List[Dict[str, Any]]:
    """标题热词榜：词表命中 + 分词新词统一计数，标注是否在词表内。"""
    vocab = [str(value) for value in (vocabulary or [])]
    vocab_folded = {value.casefold() for value in vocab}
    lexicon = list(FINANCE_LEXICON)
    counts: Dict[str, Dict[str, Any]] = {}
    for entry in entries or []:
        # 中英标题分别用对应的抽取规则：中文走分词，英文只认专有名词形态。
        # 同一篇文章里同一个词只算一次（否则中文标题与英文原标题会把它算两遍，
        # 导致"OpenAI x21"这类条数虚高一倍）。
        terms = list(dict.fromkeys(
            _extract_terms(str(entry.get("title") or ""), vocab + lexicon)
            + _extract_terms(str(entry.get("english_title") or ""), vocab + lexicon, latin_mode=True)
        ))
        for term in terms:
            _count_term(counts, term, entry)
    ranked: List[Dict[str, Any]] = []
    for item in counts.values():
        term = item["term"]
        in_vocab = term.casefold() in vocab_folded
        in_lex = term in FINANCE_LEXICON
        is_acronym = bool(re.fullmatch(r"[A-Z]{2,6}", term))
        # 词表/通用词库/全大写缩写是"确定的实体"；其余多半是标题体带来的普通词，
        # 只有反复出现（默认 >=3 条）才值得占版面——避免 Borrow/Billion 这类噪声上榜。
        floor = max(1, int(min_count))
        if not (in_vocab or in_lex or is_acronym):
            floor = max(floor, 3)
        if item["count"] < floor:
            continue
        ranked.append({
            "term": term,
            "count": item["count"],
            "publishers": sorted(item["publishers"])[:5],
            "titles": item["titles"],
            "in_vocabulary": in_vocab,
            "in_lexicon": in_lex,
        })
    # 先按"确定实体优先"，再按条数
    ranked.sort(key=lambda row: (
        not (row["in_vocabulary"] or row["in_lexicon"]), -row["count"], row["term"]
    ))
    return ranked[: max(1, int(limit))]


def _our_title_hits(industry_pack_id: str, term: str) -> Optional[int]:
    """我方文章标题里命中该词的篇数（按行业包收口）。

    用最朴素的 LIKE 计数：标准 SQL，SQLite 与 PostgreSQL 都能跑，
    避免依赖 json_each / datetime() 这类只在 SQLite 生效的写法。
    返回 None 表示查询不可用（不把它当成"没覆盖"，以免给出错误的缺口结论）。
    """
    keyword = str(term or "").strip()
    if not keyword:
        return None
    try:
        from intel_database import intel_repository

        db = intel_repository.db
        db._ensure_connection()
        with db.lock:
            cursor = db.connection.cursor()
            try:
                cursor.execute(
                    "SELECT COUNT(*) AS c FROM articles a "
                    "JOIN article_intel_classifications _c "
                    "ON _c.article_id = a.id AND _c.industry_pack_id = ? "
                    "WHERE a.status='active' AND a.title LIKE ?",
                    (str(industry_pack_id or ""), f"%{keyword}%"),
                )
                return int(cursor.fetchone()["c"])
            finally:
                cursor.close()
    except Exception:
        return None


def coverage_audit(
    industry_pack_id: str,
    terms: Iterable[Dict],
    *,
    days: int = 2,
    db=None,
) -> List[Dict[str, Any]]:
    """覆盖度审计：这些海外热词，我们自己写过没有。

    两个口径同时给，避免单一口径给出误导性结论：
      · our_title_hits —— 标题含该词的文章篇数（历史累计、跨库通用，所有词都能算）；
      · our_trend_articles —— 近 days 天"趋势命中"该词的篇数（与趋势页下钻同口径，
        只对行业包词表内的词有意义）。
    covered 以"有没有写过"为准：两个口径任一 > 0 即算已覆盖；两个都拿不到时给 None，
    而不是谎报"没覆盖"。
    """
    audited: List[Dict[str, Any]] = []
    try:
        from intel_database import intel_repository
    except Exception:
        intel_repository = None
    for row in terms or []:
        term = str(row.get("term") or "")
        if not term:
            continue
        title_hits = _our_title_hits(industry_pack_id, term)
        trend_articles: Optional[int] = None
        if intel_repository is not None and row.get("in_vocabulary"):
            try:
                _articles, total = intel_repository.list_articles_by_trend_keyword(
                    industry_pack_id=str(industry_pack_id or ""),
                    keyword=term,
                    days=days,
                    page=1,
                    per_page=1,
                )
                trend_articles = int(total or 0)
            except Exception:
                trend_articles = None
        if title_hits is None and trend_articles is None:
            covered: Optional[bool] = None
        else:
            covered = bool((title_hits or 0) > 0 or (trend_articles or 0) > 0)
        audited.append({
            **row,
            "our_title_hits": title_hits,
            "our_trend_articles": trend_articles,
            "covered": covered,
        })
    return audited


def radar_report(
    industry_pack_id: str,
    *,
    days: int = 2,
    top: int = 15,
    feeds: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """生成雷达报告：热词榜 + 出处排行 + 覆盖度缺口。纯读取，不写库、不产生候选。"""
    feed_list = list(feeds or configured_feeds())
    entries: List[Dict[str, Any]] = []
    feed_status: List[Dict[str, Any]] = []
    for feed_url in feed_list:
        fetched = fetch_feed(feed_url)
        feed_status.append({
            "feed": feed_url,
            "entry_count": len(fetched.get("entries") or []),
            "cached": bool(fetched.get("cached")),
            "error": fetched.get("error") or "",
        })
        entries.extend(fetched.get("entries") or [])

    vocabulary = pack_vocabulary([industry_pack_id] if industry_pack_id else [])
    terms = coverage_audit(
        industry_pack_id, hot_terms(entries, vocabulary=vocabulary, limit=max(1, top)), days=days
    )
    missing = [row for row in terms if row.get("in_vocabulary") and row.get("covered") is False]
    new_terms = [row for row in terms if not row.get("in_vocabulary")]
    return {
        "industry_pack_id": str(industry_pack_id or ""),
        "days": int(days),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "feeds": feed_status,
        "entry_count": len(entries),
        "publishers": publisher_ranking(entries, limit=max(1, top)),
        "hot_terms": terms,
        "missing_terms": missing,
        "new_terms": new_terms[: max(1, top)],
        "vocabulary_size": len(vocabulary),
    }
