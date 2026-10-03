# -*- coding: utf-8 -*-
"""注意力方向：让关键词跟随每周周报的「下周关注」动态变化。

解决的问题：固定主题（fixed_topics）和固定关键词都是人工维护的，周报里每周发现的
新线索没人盯。这里把周报的「下周关注」变成可执行的三件事：

1. **落成【注意力方向】**：每条线索一行（线索标题 + 理由 + 追踪关键词 + 命中数），
   按周保存，下一份周报生成时把上一周收口（status='closed'）并结算命中数，便于回看。
2. **生成动态主题**「上周追踪」：把命中追踪词的文章挂到该主题下，首页主题卡自动多一张
   （主题按 topic_source='watch' 存，既不进 fixed_topics，也不会被固定主题同步 /
   BERTopic 自动主题清理掉）。主题**只有一张、跨周复用**：每周覆盖它的词与文章，
   历史留在 pack_attention_directions 里按周保存，首页不会堆出多张同名的卡。
3. **追踪词进入搜索采集**：扫描器取本包 active 的追踪词作为查询词，主动搜这些线索。

边界（有意为之）：
* 绝不动 industry_anchor_keywords —— 那是"文章属不属于本行业"的硬门禁，塞追踪词会
  把无关文章放进来；追踪词只用于主题归属与搜索，不参与行业准入。
* 绝不动 fixed_topics / manifest：动态主题不进行业包配置，不需要"草稿→发布→激活"。
* 「一篇文章只归一个主题」的既有规则对追踪主题不适用：追踪是叠加层，
  不抢原有主题的文章（见 intel_topics 里对 watch_keyword 的豁免）。
"""

import json
import re
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional

from industry_packs import industry_pack_loader, normalize_intel_text, unique_normalized_keywords
from sqlite_database import sqlite_db
from utils import get_china_time

# 动态主题的身份标识：topic_source 用 'watch'，assignment_method 用 'watch_keyword'。
# 主题**只有一张、跨周复用**（key 固定 watch_track，名字固定「上周追踪」）：
# 每周同步覆盖它的追踪词与关联文章，"哪一周盯了什么、命中几篇"这类历史留在
# pack_attention_directions 里（按周存），首页因此永远只有一张不重名的追踪卡。
WATCH_TOPIC_SOURCE = "watch"
WATCH_ASSIGNMENT_METHOD = "watch_keyword"
WATCH_TOPIC_KEY = "watch_track"
WATCH_TOPIC_NAME = "上周追踪"

# 一份周报的「下周关注」最多留几条线索、每条最多几个追踪词
MAX_DIRECTIONS = 6
MAX_KEYWORDS_PER_DIRECTION = 8
# 挂文章时的回看窗口（天）：太短会漏掉线索刚出现时的文章，太长会让主题卡混入旧文
LINK_LOOKBACK_DAYS = 21
# 追踪词精度过滤：线索里常抽出"具身智能""AI""物理"这类行业通用词，它们命中全库文章，
# 会让追踪卡变成"整包文章列表"（实测 341 篇），完全失去"专门盯"的意义。
# 规则：某词命中率超过 MAX_KEYWORD_DOC_RATIO、且命中篇数不少于 MIN_DOCS_FOR_COMMON，
# 判为通用词剔除；语料不足 MIN_CORPUS_FOR_DF 篇时不做判断（样本太小容易误杀）。
MAX_KEYWORD_DOC_RATIO = 0.35
MIN_DOCS_FOR_COMMON = 10
MIN_CORPUS_FOR_DF = 20
DF_SAMPLE_SIZE = 400

# 「下周关注」小节标题（LLM 可能写成 下一步关注/后续关注）
_SECTION_PATTERN = re.compile(
    r"^#{1,6}\s*(?:[一二三四五六七八九十]+|\d+)?\s*[、.．)）]?\s*(下周关注|下一步关注|后续关注|要持续关注)\s*$",
    re.MULTILINE,
)
# 条目：- / * / 1. / （1）/ **1.** 等
_ITEM_PATTERN = re.compile(r"^\s*(?:[-*•]|(?:\*\*)?\d+[.、)）](?:\*\*)?|（\d+）|\(\d+\))\s*")
# 条目里的「跟踪关键词：A、B、C」一行（提示词要求模型直接给，最准）
_KEYWORD_LINE_PATTERN = re.compile(
    r"(?:跟踪关键词|追踪关键词|关键词|关注词)\s*[:：]\s*([^\n]+)"
)
# 关键词分隔符：**不含半角空格** —— "问界 M9" / "World Labs" 这类词本身带空格，
# 按空格切会把它们拆碎；中文列举用的是 、，；/｜ 与全角空格。
_KEYWORD_SPLIT = re.compile(r"[、,，;；/｜|　]+")

# 规则兜底抽词时的停用词：这些词出现在线索里也不构成"追踪词"
_STOPWORDS = {
    "关注", "持续", "跟踪", "继续", "以及", "相关", "行业", "公司", "企业", "事件", "风险",
    "机会", "动向", "进展", "情况", "影响", "整体", "进一步", "可能", "预计", "方面", "问题",
    "值得", "重点", "主要", "此次", "本次", "近期", "本周", "下周", "上周", "后续", "同时",
    "我们", "建议", "需要", "例如", "包括", "等等", "一些", "各种", "多个", "相关方", "动态",
    "消息", "报道", "新闻", "文章", "数据", "产品", "技术", "市场", "政策", "机构", "厂商",
    "趋势", "方向", "线索", "节点", "节奏", "变化", "落地", "情况如何", "是否",
}

# 规则兜底切出来的片段里，含这些"句子成分"的多半是短语而不是关键词
# （如"李飞飞公司收购后的整合动向""验证其无需重新训练的通用性"），直接丢掉。
# 注意刻意不放 和 / 与 / 及 / 之 / 在 —— 实体名里常见（协和医院、之江实验室）。
_CLAUSE_CHARS = set("的了是否就都还也很更最把被让使并而但因所这那")
_CLAUSE_WORDS = (
    "如何", "什么", "哪些", "为什么", "能否", "以及", "并且", "但是", "因为", "所以",
    "观察", "跟踪", "验证", "缓解", "关注", "表示", "认为", "显示", "成为", "需要", "应该",
)
_CLAUSE_SUFFIXES = ("的", "了", "后", "前", "中", "时", "期间", "背景", "方面", "情况", "之后", "之前")
_CLAUSE_PREFIXES = ("在", "从", "对", "向", "为", "以", "将", "其", "与", "和", "及", "或")


# ----------------------------------------------------------------------
# 周编号与命名
# ----------------------------------------------------------------------
def week_key_of(date_text: str = "") -> str:
    """把日期归到 ISO 周：'2026-W41'。空值取当天。"""
    value = str(date_text or "").strip()[:10]
    try:
        day = datetime.strptime(value, "%Y-%m-%d") if value else get_china_time()
    except ValueError:
        day = get_china_time()
    iso = day.isocalendar()
    return "%04d-W%02d" % (iso[0], iso[1])


def week_label(week_key: str) -> str:
    """'2026-W41' → '第41周'。"""
    match = re.search(r"W(\d{1,2})", str(week_key or ""))
    return "第%d周" % int(match.group(1)) if match else str(week_key or "")


def next_week_topic_week(report_time_end: str) -> str:
    """周报覆盖到 time_end，它「下周关注」盯的是之后那一周（下一个周一所在的 ISO 周）。

    周报按周生成（通常周五出），time_end 之后那一周才是要盯的窗口，所以按"下一个周一"
    归周，避免把一个横跨两周的窗口错标成上一周。
    """
    value = str(report_time_end or "").strip()[:10]
    try:
        day = datetime.strptime(value, "%Y-%m-%d") + timedelta(days=1)
    except ValueError:
        day = get_china_time()
    while day.weekday() != 0:  # 0 = 周一
        day += timedelta(days=1)
    return week_key_of(day.strftime("%Y-%m-%d"))


# ----------------------------------------------------------------------
# 解析「下周关注」
# ----------------------------------------------------------------------
def _clean_markdown_line(line: str) -> str:
    text = str(line or "").strip()
    text = re.sub(r"^\s*[-*•]\s*", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"[`*_>]+", "", text)
    return " ".join(text.split())


def parse_watch_section(markdown: str) -> List[Dict]:
    """从周报 Markdown 里抽出「下周关注」的线索条目。

    返回 [{direction, reason, keywords}]，direction 是线索标题（尽量短），
    keywords 只在报告里显式写了「跟踪关键词：…」时才有值，其余交给抽词环节。
    """
    text = str(markdown or "")
    if not text.strip():
        return []
    match = _SECTION_PATTERN.search(text)
    if not match:
        return []
    body = text[match.end():]
    # 截到下一个同级/更高级标题为止
    stop = re.search(r"^#{1,6}\s+", body, re.MULTILINE)
    if stop:
        body = body[:stop.start()]

    items: List[Dict] = []
    current: List[str] = []

    def flush() -> None:
        if not current:
            return
        block = "\n".join(current).strip()
        current.clear()
        if not block:
            return
        keywords = _keywords_from_line(block)
        plain = _KEYWORD_LINE_PATTERN.sub("", block).strip()
        lines = [_clean_markdown_line(line) for line in plain.splitlines()]
        lines = [line for line in lines if line]
        if not lines:
            return
        head = lines[0]
        # 标题：优先取「：」前的短句，否则取首个分句
        direction = head
        for sep in ("：", ":", "——", "，", ",", "。"):
            if sep in direction:
                candidate = direction.split(sep, 1)[0].strip()
                if 2 <= len(candidate) <= 40:
                    direction = candidate
                break
        if len(direction) > 40:
            direction = direction[:40]
        reason = " ".join(lines[1:]) if len(lines) > 1 else head[len(direction):].lstrip("：:，,。 ").strip()
        items.append({
            "direction": direction.strip(),
            "reason": reason.strip()[:400],
            "keywords": keywords,
        })

    for raw_line in body.splitlines():
        if not raw_line.strip():
            continue
        if _ITEM_PATTERN.match(raw_line):
            flush()
            current.append(_ITEM_PATTERN.sub("", raw_line, count=1))
        elif current:
            # 条目的续行（LLM 换行写的说明）并入上一条
            current.append(raw_line)
        else:
            # 小节开头的说明性文字（如"以下线索值得继续跟踪："）直接丢弃
            continue
    flush()

    result = []
    seen = set()
    for item in items:
        direction = item["direction"]
        if not direction:
            continue
        key = normalize_intel_text(direction)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= MAX_DIRECTIONS:
            break
    return result


def _keywords_from_line(block: str) -> List[str]:
    """取条目里显式的「跟踪关键词：A、B、C」。"""
    match = _KEYWORD_LINE_PATTERN.search(block or "")
    if not match:
        return []
    raw = match.group(1)
    values = [part.strip(" 　·・") for part in _KEYWORD_SPLIT.split(raw) if part.strip(" 　·・")]
    return unique_normalized_keywords(values)[:MAX_KEYWORDS_PER_DIRECTION]


# ----------------------------------------------------------------------
# 抽词：LLM 优先 + 规则兜底
# ----------------------------------------------------------------------
def _pack_vocabulary(pack: Dict) -> List[str]:
    """包内既有词表：线索文本里命中它们的，优先作为追踪词（同义/口径都统一）。"""
    values: List[str] = []
    values += list(pack.get("brands") or [])
    values += list(pack.get("core_keywords") or [])
    values += list(pack.get("expanded_keywords") or [])
    values += list(pack.get("trend_keywords") or [])
    values += list(pack.get("event_keywords") or [])
    gate = pack.get("candidate_gate") or {}
    if isinstance(gate, dict):
        values += list(gate.get("anchor_keywords") or [])
        values += list(gate.get("entity_keywords") or [])
    for topic in pack.get("fixed_topics") or []:
        if isinstance(topic, dict):
            values += list(topic.get("keywords") or [])
    return unique_normalized_keywords(values)


def _looks_like_keyword(piece: str) -> bool:
    """规则切出来的片段是否像个可用的追踪词。"""
    if not (2 <= len(piece) <= 12):
        return False
    if piece in _STOPWORDS:
        return False
    if re.fullmatch(r"[\d.%]+", piece):
        return False
    if any(char in _CLAUSE_CHARS for char in piece):
        return False
    if any(word in piece for word in _CLAUSE_WORDS):
        return False
    if piece.endswith(_CLAUSE_SUFFIXES) or piece.startswith(_CLAUSE_PREFIXES):
        return False
    # 单个字母/数字混排（如 "6 亿"）没有检索价值
    if not re.search(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{2,}", piece):
        return False
    return True


def _rule_keywords(text: str, vocabulary: Iterable[str]) -> List[str]:
    """规则兜底抽词：先用包内既有词表命中（最可靠），再按标点切短语过滤停用词/句子成分。"""
    raw = str(text or "")
    normalized = normalize_intel_text(raw)
    if not normalized:
        return []
    hits = [term for term in vocabulary if normalize_intel_text(term) in normalized]
    hits.sort(key=len, reverse=True)

    segments: List[str] = []
    for piece in re.split(r"[，。；、：:（）()【】\[\]“”\"'‘’\s/｜|!！?？~～—\-]+", raw):
        piece = piece.strip(" 　·・")
        if _looks_like_keyword(piece):
            segments.append(piece)

    ordered = unique_normalized_keywords(hits + segments)
    return ordered[:MAX_KEYWORDS_PER_DIRECTION]


def _llm_keywords(items: List[Dict], pack: Dict) -> Dict[int, List[str]]:
    """让本地 LLM 直接给每条线索的追踪词；未配置/失败时返回空（调用方走规则）。"""
    if not items:
        return {}
    try:
        from intel_llm_client import intel_llm_client
        return intel_llm_client.extract_attention_keywords(items, pack) or {}
    except Exception as exc:
        print("⚠️ 注意力方向：LLM 抽词不可用，改用规则抽词（%s）" % exc)
        return {}


def extract_keywords(items: List[Dict], pack: Dict) -> List[Dict]:
    """给每条线索补上 keywords：报告里写了的直接用，其余 LLM 优先、规则兜底。"""
    vocabulary = _pack_vocabulary(pack)
    llm_result = _llm_keywords(items, pack)
    enriched: List[Dict] = []
    for index, item in enumerate(items, start=1):
        keywords = unique_normalized_keywords(item.get("keywords") or [])
        if len(keywords) < 2:
            for extra in (llm_result.get(index) or []):
                if normalize_intel_text(extra) not in {normalize_intel_text(k) for k in keywords}:
                    keywords.append(str(extra).strip())
        if len(keywords) < 2:
            text = "%s %s" % (item.get("direction") or "", item.get("reason") or "")
            for extra in _rule_keywords(text, vocabulary):
                if normalize_intel_text(extra) not in {normalize_intel_text(k) for k in keywords}:
                    keywords.append(extra)
        enriched.append({
            "direction": item.get("direction") or "",
            "reason": item.get("reason") or "",
            "keywords": unique_normalized_keywords(keywords)[:MAX_KEYWORDS_PER_DIRECTION],
        })
    return enriched


# ----------------------------------------------------------------------
# 落库：注意力方向
# ----------------------------------------------------------------------
def _ensure_connection() -> None:
    sqlite_db._ensure_connection()
    from intel_schema import ensure_pack_attention_tables
    with sqlite_db.lock:
        cursor = sqlite_db.connection.cursor()
        try:
            ensure_pack_attention_tables(cursor)
            sqlite_db.connection.commit()
        finally:
            cursor.close()


def _now_text() -> str:
    return get_china_time().strftime("%Y-%m-%d %H:%M:%S")


def save_directions(pack_id: str, report: Dict, week_key: str, items: List[Dict]) -> List[Dict]:
    """按 (pack, 周, 线索标题) 幂等写入注意力方向。"""
    if not items:
        return []
    _ensure_connection()
    now = _now_text()
    report_id = int(report.get("id") or 0) or None
    report_title = str(report.get("title") or "")
    saved: List[Dict] = []
    with sqlite_db.lock:
        cursor = sqlite_db.connection.cursor()
        try:
            for item in items:
                direction = str(item.get("direction") or "").strip()
                if not direction:
                    continue
                keywords = unique_normalized_keywords(item.get("keywords") or [])
                cursor.execute(
                    """
                    INSERT INTO pack_attention_directions (
                        industry_pack_id, week_key, week_label, report_id, report_title,
                        direction, reason, keywords_json, status, source, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', 'report', ?, ?)
                    ON CONFLICT(industry_pack_id, week_key, direction) DO UPDATE SET
                        report_id=excluded.report_id,
                        report_title=excluded.report_title,
                        reason=excluded.reason,
                        keywords_json=excluded.keywords_json,
                        status='active',
                        closed_at=NULL,
                        updated_at=excluded.updated_at
                    """,
                    (
                        pack_id, week_key, week_label(week_key), report_id, report_title,
                        direction, str(item.get("reason") or ""),
                        json.dumps(keywords, ensure_ascii=False), now, now,
                    ),
                )
                cursor.execute(
                    "SELECT id FROM pack_attention_directions "
                    "WHERE industry_pack_id=? AND week_key=? AND direction=?",
                    (pack_id, week_key, direction),
                )
                row = cursor.fetchone()
                saved.append({
                    "id": int(row["id"]) if row else 0,
                    "direction": direction,
                    "keywords": keywords,
                })
            sqlite_db.connection.commit()
        finally:
            cursor.close()
    return saved


def close_previous_weeks(pack_id: str, keep_week: str) -> int:
    """上一份周报的线索收口：把非当前周的行标 closed 并把命中数结算进 hit_count。"""
    _ensure_connection()
    closed = 0
    with sqlite_db.lock:
        cursor = sqlite_db.connection.cursor()
        try:
            cursor.execute(
                "SELECT id, keywords_json FROM pack_attention_directions "
                "WHERE industry_pack_id=? AND status='active' AND week_key<>?",
                (pack_id, keep_week),
            )
            rows = cursor.fetchall()
            for row in rows:
                keywords = _json_list(row["keywords_json"])
                hits = count_direction_hits(pack_id, keywords, use_lock=False)
                cursor.execute(
                    "UPDATE pack_attention_directions SET status='closed', hit_count=?, closed_at=?, updated_at=? "
                    "WHERE id=?",
                    (int(hits), _now_text(), _now_text(), int(row["id"])),
                )
                closed += 1
            sqlite_db.connection.commit()
        finally:
            cursor.close()
    return closed


def _json_list(value) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        data = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in data] if isinstance(data, list) else []


# ----------------------------------------------------------------------
# 动态主题：建卡 + 挂文章
# ----------------------------------------------------------------------
def _candidate_articles(cursor, pack_id: str, days: int) -> List[Dict]:
    """近 N 天该行业包已分类的文章（与主题聚类取数口径一致）。"""
    since = (get_china_time() - timedelta(days=int(days))).strftime("%Y-%m-%d")
    cursor.execute(
        """
        SELECT a.id AS article_id, a.title, a.content, a.publish_date,
               c.matched_keywords_json
        FROM article_intel_classifications c
        JOIN articles a ON a.id=c.article_id
        WHERE c.industry_pack_id=? AND a.status='active'
          AND COALESCE(a.publish_date,'') >= ?
        ORDER BY c.classified_at DESC, a.id DESC
        LIMIT 2000
        """,
        (pack_id, since),
    )
    return [dict(row) for row in cursor.fetchall()]


def drop_generic_keywords(pack_id: str, items: List[Dict]) -> List[Dict]:
    """剔除"命中全库"的行业通用词，留住真正能盯的词。

    判定用近 N 天语料里的文档命中率（标题+正文子串，够准且比完整匹配快得多）：
    命中率 > MAX_KEYWORD_DOC_RATIO 且命中 >= MIN_DOCS_FOR_COMMON 篇 → 通用词剔除。
    每条线索至少保留命中率最低的一个词，避免整条线索没词可盯。
    """
    _ensure_connection()
    with sqlite_db.lock:
        cursor = sqlite_db.connection.cursor()
        try:
            articles = _candidate_articles(cursor, pack_id, LINK_LOOKBACK_DAYS)
        finally:
            cursor.close()
    return _filter_items_by_ratio(items, articles)


def _filter_items_by_ratio(items: List[Dict], articles: List[Dict]) -> List[Dict]:
    haystacks = [
        normalize_intel_text((row.get("title") or "") + " " + (row.get("content") or "")[:20000])
        for row in articles[:DF_SAMPLE_SIZE]
    ]
    corpus = len(haystacks)
    filtered: List[Dict] = []
    for item in items:
        keywords = list(item.get("keywords") or [])
        if not keywords:
            filtered.append(item)
            continue
        scored = []
        for keyword in keywords:
            needle = normalize_intel_text(keyword)
            hits = sum(1 for text in haystacks if needle and needle in text)
            scored.append((keyword, hits))
        if corpus < MIN_CORPUS_FOR_DF:
            kept = [keyword for keyword, _ in scored]
        else:
            kept = [keyword for keyword, hits in scored
                    if not (hits >= MIN_DOCS_FOR_COMMON and hits / corpus > MAX_KEYWORD_DOC_RATIO)]
            if not kept:
                # 全被判为通用词时留命中率最低的那个，追踪不会整条落空
                kept = [min(scored, key=lambda pair: pair[1])[0]]
        dropped = [keyword for keyword, _ in scored if keyword not in kept]
        if dropped:
            print("🎯 注意力方向：剔除通用词（命中过泛）%s ← %s"
                  % ("、".join(dropped), item.get("direction") or ""))
        filtered.append({**item, "keywords": kept})
    return filtered


def _match_articles(articles: List[Dict], keywords: List[str]) -> List[Dict]:
    """用既有的主题匹配器（同阈值、同判别逻辑）算命中，避免另造一套匹配规则。"""
    terms = unique_normalized_keywords(keywords or [])
    if not terms or not articles:
        return []
    from intel_classifier import match_fixed_topics
    probe_pack = {
        "fixed_topics": [{
            "key": WATCH_TOPIC_KEY + "_probe",
            "name": "追踪",
            "keywords": terms,
        }]
    }
    hits = []
    for article in articles:
        assignments = match_fixed_topics(
            {
                "title": article.get("title") or "",
                "content": article.get("content") or "",
                "matched_keywords": _json_list(article.get("matched_keywords_json")),
            },
            probe_pack,
        )
        if not assignments:
            continue
        best = assignments[0]
        hits.append({
            "article_id": int(article["article_id"]),
            "score": float(best.get("score") or 0),
            "matched_keywords": list(best.get("matched_keywords") or []),
        })
    return hits


def count_direction_hits(pack_id: str, keywords: List[str], *, use_lock: bool = True) -> int:
    """单条线索的命中文章数（结算与面板都用它）。"""
    _ensure_connection()
    if not keywords:
        return 0

    def _run() -> int:
        cursor = sqlite_db.connection.cursor()
        try:
            return len(_match_articles(_candidate_articles(cursor, pack_id, LINK_LOOKBACK_DAYS), keywords))
        finally:
            cursor.close()

    if not use_lock:
        return _run()
    with sqlite_db.lock:
        return _run()


def sync_watch_topic(pack_id: str, week_key: str, items: List[Dict], report_title: str = "") -> Dict:
    """建/更新动态主题「上周追踪」，并把命中追踪词的文章挂上去。

    主题只有一张（固定 key），每周同步覆盖追踪词与关联文章；历史留在
    pack_attention_directions（按周存命中数），所以首页不会堆出多张同名的追踪卡。
    顺带清掉早期按周生成的 watch 主题行（watch_w41 这类），避免旧卡残留。

    返回 {topic_id, topic_key, topic_name, article_count, direction_hits:{线索:命中数}}。
    """
    _ensure_connection()
    all_keywords: List[str] = []
    for item in items:
        all_keywords += list(item.get("keywords") or [])
    all_keywords = unique_normalized_keywords(all_keywords)
    topic_key = WATCH_TOPIC_KEY
    topic_name = WATCH_TOPIC_NAME
    summary = "%s由《%s》的「下周关注」生成：%s" % (
        week_label(week_key), report_title or "上一份周报",
        "；".join(str(item.get("direction") or "") for item in items[:MAX_DIRECTIONS]),
    )
    now = _now_text()
    direction_hits: Dict[str, int] = {}
    with sqlite_db.lock:
        cursor = sqlite_db.connection.cursor()
        try:
            # 早期按周建的主题（watch_w41…）不再使用：连同关联一起删掉，避免同名/过期卡片残留
            cursor.execute(
                "SELECT id FROM intel_topics WHERE industry_pack_id=? AND topic_source=? AND topic_key<>?",
                (pack_id, WATCH_TOPIC_SOURCE, topic_key),
            )
            for stale in cursor.fetchall():
                cursor.execute("DELETE FROM intel_topic_articles WHERE topic_id=?", (int(stale["id"]),))
                cursor.execute("DELETE FROM intel_topics WHERE id=?", (int(stale["id"]),))
            cursor.execute(
                """
                INSERT INTO intel_topics (
                    industry_pack_id, topic_key, topic_name, topic_source, keywords_json,
                    summary, summary_source, summary_version, content_signature,
                    article_count_cache, last_clustered_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'attention', 'v1', ?, 0, ?, ?, ?)
                ON CONFLICT(industry_pack_id, topic_key) DO UPDATE SET
                    topic_name=excluded.topic_name,
                    keywords_json=excluded.keywords_json,
                    summary=excluded.summary,
                    last_clustered_at=excluded.last_clustered_at,
                    updated_at=excluded.updated_at
                """,
                (
                    pack_id, topic_key, topic_name, WATCH_TOPIC_SOURCE,
                    json.dumps(all_keywords, ensure_ascii=False),
                    summary, week_key, now, now, now,
                ),
            )
            cursor.execute(
                "SELECT id FROM intel_topics WHERE industry_pack_id=? AND topic_key=?",
                (pack_id, topic_key),
            )
            topic_id = int(cursor.fetchone()["id"])

            articles = _candidate_articles(cursor, pack_id, LINK_LOOKBACK_DAYS)
            hits = _match_articles(articles, all_keywords)
            cursor.execute(
                "DELETE FROM intel_topic_articles WHERE topic_id=? AND assignment_method!=?",
                (topic_id, "manual"),
            )
            for hit in hits:
                cursor.execute(
                    """
                    INSERT INTO intel_topic_articles (
                        topic_id, article_id, association_score, assignment_method,
                        evidence_json, assigned_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(topic_id, article_id) DO UPDATE SET
                        association_score=excluded.association_score,
                        evidence_json=excluded.evidence_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        topic_id, hit["article_id"], hit["score"], WATCH_ASSIGNMENT_METHOD,
                        json.dumps({"keywords": hit["matched_keywords"], "week": week_key},
                                   ensure_ascii=False),
                        now, now,
                    ),
                )
            cursor.execute(
                "UPDATE intel_topics SET article_count_cache=?, updated_at=? WHERE id=?",
                (len(hits), now, topic_id),
            )
            # 每条线索的命中数：单独匹配一次，面板里能看出"哪条线索真的有人跟"
            for item in items:
                direction = str(item.get("direction") or "")
                direction_hits[direction] = len(_match_articles(
                    articles, list(item.get("keywords") or [])))
            sqlite_db.connection.commit()
        finally:
            cursor.close()
    return {
        "topic_id": topic_id,
        "topic_key": topic_key,
        "topic_name": topic_name,
        "article_count": len(hits),
        "direction_hits": direction_hits,
    }


# ----------------------------------------------------------------------
# 主入口：周报生成后同步
# ----------------------------------------------------------------------
def sync_from_report(pack_id: str, report_id: int, markdown: str = "") -> Dict:
    """周报落库后调用：收口上一周 → 解析「下周关注」→ 抽词落库 → 刷新动态主题。

    任何异常都由调用方兜住：注意力方向同步失败绝不能让周报生成本身失败。
    """
    pack_id = str(pack_id or "").strip()
    if not pack_id or not report_id:
        return {"skipped": True, "reason": "缺少 pack_id / report_id"}

    from pack_report import get_pack_report, get_pack_report_markdown
    report = get_pack_report(int(report_id)) or {}
    text = markdown or get_pack_report_markdown(int(report_id)) or ""
    week_key = next_week_topic_week(str(report.get("time_end") or ""))
    items = parse_watch_section(text)
    if not items:
        return {"skipped": True, "reason": "周报里没有可解析的「下周关注」条目", "week_key": week_key}

    pack = industry_pack_loader.load(pack_id, enabled_only=False) or {}
    enriched = extract_keywords(items, pack)
    enriched = drop_generic_keywords(pack_id, enriched)
    enriched = [item for item in enriched if item.get("keywords")]
    if not enriched:
        return {"skipped": True, "reason": "「下周关注」条目没抽出可用的追踪词", "week_key": week_key}

    closed = close_previous_weeks(pack_id, week_key)
    saved = save_directions(pack_id, report, week_key, enriched)
    topic = sync_watch_topic(pack_id, week_key, enriched, str(report.get("title") or ""))
    for item in saved:
        item["hit_count"] = int(topic.get("direction_hits", {}).get(item["direction"], 0))
        _update_direction_hits(item["id"], item["hit_count"])
    keyword_total = len({
        normalize_intel_text(keyword)
        for item in enriched for keyword in (item.get("keywords") or [])
    })
    print(
        "🎯 注意力方向已更新：%s %s 共 %d 条线索、追踪词 %d 个，动态主题「%s」挂 %d 篇（上一周收口 %d 条）"
        % (pack_id, week_key, len(saved), keyword_total, topic["topic_name"], topic["article_count"], closed)
    )
    return {
        "week_key": week_key,
        "week_label": week_label(week_key),
        "directions": saved,
        "topic": {
            "id": topic["topic_id"],
            "key": topic["topic_key"],
            "name": topic["topic_name"],
            "article_count": topic["article_count"],
        },
        "closed_previous": closed,
    }


def _update_direction_hits(direction_id: int, hits: int) -> None:
    if not direction_id:
        return
    with sqlite_db.lock:
        cursor = sqlite_db.connection.cursor()
        try:
            cursor.execute(
                "UPDATE pack_attention_directions SET hit_count=?, updated_at=? WHERE id=?",
                (int(hits), _now_text(), int(direction_id)),
            )
            sqlite_db.connection.commit()
        finally:
            cursor.close()


# ----------------------------------------------------------------------
# 读取面
# ----------------------------------------------------------------------
def list_directions(pack_id: str, limit: int = 100) -> List[Dict]:
    """面板用：按周倒序返回注意力方向。"""
    _ensure_connection()
    with sqlite_db.lock:
        cursor = sqlite_db.connection.cursor()
        try:
            cursor.execute(
                "SELECT * FROM pack_attention_directions WHERE industry_pack_id=? "
                "ORDER BY week_key DESC, id ASC LIMIT ?",
                (str(pack_id or ""), int(limit)),
            )
            rows = [dict(row) for row in cursor.fetchall()]
        finally:
            cursor.close()
    for row in rows:
        row["keywords"] = _json_list(row.get("keywords_json"))
    return rows


def active_directions(pack_id: str) -> List[Dict]:
    return [row for row in list_directions(pack_id) if str(row.get("status")) == "active"]


def active_watch_keywords(pack_id: str, limit: int = 12) -> List[str]:
    """扫描采集用：当前追踪词（最新一周优先）。"""
    values: List[str] = []
    for row in active_directions(pack_id):
        values += list(row.get("keywords") or [])
        if len(values) >= limit:
            break
    return unique_normalized_keywords(values)[:limit]


def close_direction(pack_id: str, direction_id: int) -> bool:
    """手动收口单条线索（面板按钮）。"""
    _ensure_connection()
    with sqlite_db.lock:
        cursor = sqlite_db.connection.cursor()
        try:
            cursor.execute(
                "UPDATE pack_attention_directions SET status='closed', closed_at=?, updated_at=? "
                "WHERE id=? AND industry_pack_id=?",
                (_now_text(), _now_text(), int(direction_id), str(pack_id or "")),
            )
            changed = bool(cursor.rowcount)
            sqlite_db.connection.commit()
        finally:
            cursor.close()
    return changed


def summary_for_pack(pack_id: str) -> Dict:
    """面板头部：当前周、线索数、命中合计（主题固定一张「上周追踪」）。"""
    rows = list_directions(pack_id)
    active = [row for row in rows if str(row.get("status")) == "active"]
    week_key = str(active[0]["week_key"]) if active else (str(rows[0]["week_key"]) if rows else "")
    return {
        "week_key": week_key,
        "week_label": week_label(week_key) if week_key else "",
        "topic_key": WATCH_TOPIC_KEY,
        "topic_name": WATCH_TOPIC_NAME,
        "active_count": len(active),
        "total_hits": sum(int(row.get("hit_count") or 0) for row in active),
    }
