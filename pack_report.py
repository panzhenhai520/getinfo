# -*- coding: utf-8 -*-

"""
行业包 AI 周报模块（通用 LLM 总结能力）。

职责：
1. 提供一个通用的「时间窗 + 行业包 → LLM Markdown 总结」能力：
   从文章库取该行业包时间窗内的文章 → Python 侧计算时序统计与主题泳道 →
   向量检索（bge-m3，VPN embedding，失败降级关键词）挑选代表文章 →
   套用可配置提示词调用本地 LLM 生成 Markdown 周报 → 落库。
2. 提供提示词设置（按行业包保存，未配置回退默认提示词）。
3. 提供 API：设置读写 / 测试派发（入队 intel_jobs）/ 报告列表 / Markdown 读取。
4. 提供 worker job 入口 run_pack_report_job，供 intel_worker 注册执行；
   每周五由 intel_worker.enqueue_due_periodic_jobs 自动派发（时间窗=最近7天）。

分析/总结算法（确定性统计 + LLM 归纳，两步）：
- 第一步（代码计算，不依赖 LLM 数数）：
  * 文章清单：articles JOIN article_intel_classifications，
    按 industry_pack_id + 时间窗（COALESCE(publish_date, first_crawled, created_at)）过滤；
  * 每日文章数时序：按日期聚合计数，作为「时序变量」；
  * 主题泳道：按文章分类（final_category/主题标签）分堆，每主题逐日计数序列 +
    合计 + 爆发日检测（当日 >= max(3, 均值+2σ) 或 >= 前一日 2 倍）；
  * 代表文章：行业锚点+主题词拼查询 → 向量余弦相似度排序（失败降级关键词命中分），
    每主题取 top3，再按总分补齐。
- 第二步（LLM 归纳）：把上面的统计表 + 代表文章清单填进用户提示词模板，
  由 LLM 输出 Markdown（标题/报告时间自拟，热点/趋势泳道/研判/下周关注）。
"""

import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from flask import Blueprint, Response, jsonify, request

import config
from decorators import login_required
from sqlite_database import sqlite_db

# ----------------------------------------------------------------------
# 默认提示词（精心编写：时间窗 + 时序变量 + 主题泳道 + 热点/趋势 + Markdown）
# 占位符由代码填充：行业名称/行业锚点/主题维度/时间窗/文章总数/每日文章数统计/
# 主题时序统计/代表文章清单。用户可在「信源管理 → AI 周报」修改。
# ----------------------------------------------------------------------
DEFAULT_PACK_REPORT_PROMPT = """你是「{行业名称}」行业的资深情报分析师。请根据下方平台文章库的真实数据，撰写一份行业周报。

【任务】综合统计时间窗内的全部材料，分析行业热点与趋势，输出一份 Markdown 周报。报告时间与报告标题由你根据材料与生成时间自行拟定；所有结论必须来自给定材料，严禁编造材料中没有的文章、数据、时间、机构或结论；材料不足以支撑时如实说明并明确标注「推测」。

【输出结构】
# 报告标题
（标题不超过 20 字，须体现行业与时间窗，例如「汽车行业周报（09-12 ~ 09-18）」）
> 报告时间：YYYY-MM-DD HH:MM ｜ 统计时间窗：起 ~ 止 ｜ 数据来源：平台文章库（共 N 篇）
## 一、本周概览
用 3~5 句话概括行业整体态势，引用总篇数与每日走势（直接引用下方统计表，不要自行推算数字）。
## 二、行业热点（最多 5 个，按热度降序）
每个热点一小节：### 热点标题（不超过 18 字）
- 热度依据：涉及文章数、集中出现的日期
- 要点：2~4 条，每条末尾用「（文章标题，MM-DD）」标注来源文章
## 三、主题趋势泳道
按下方「主题时序统计」逐主题分析：每个主题一小节，说明该主题的每日走势（上升/下降/爆发/平稳，结合爆发标记）、驱动事件与走向判断。
## 四、趋势与风险研判
基于时序变化给出 3~5 条趋势判断（政策/技术/市场方向），每条注明依据；如材料显示风险或降温信号，单独指出。
## 五、下周关注
列出 3~5 个值得继续跟踪的线索（新出现的主题、连续多天出现的主题、权威机构动向），说明理由。
每条线索独立成一行，格式固定为：
- 线索标题（不超过 20 字）：一句话说明为什么值得跟踪。跟踪关键词：词1、词2、词3
「跟踪关键词」必须是可直接用于搜索的具体词（实体名/机构名/技术名/产品名/事件短语），
不要用「关注」「趋势」「进展」这类空词；每条线索给 3~6 个，且不同线索之间尽量不重复。

【写作要求】
1. 全文中文，标题层级用 # / ## / ###。
2. 引用文章必须确实出现在材料中，标注「标题（MM-DD）」。
3. 数字只引用给定统计表，不做外部推算。
4. 不要输出与报告无关的说明、道歉、元信息或代码块。
5. 不要使用 LaTeX 数学符号（如 $...$、\\rightarrow）；趋势方向直接用文字或「→」等普通箭头表达。

【行业背景】
行业名称：{行业名称}
行业锚点：{行业锚点}
主题维度：{主题维度}

【统计时间窗】{时间窗}（共 {文章总数} 篇）

【每日文章数时序】（日期 → 篇数）
{每日文章数统计}

【主题时序统计】（主题 | 每日篇数序列 | 合计 | 爆发日）
{主题时序统计}

【代表文章清单】（编号. 标题 | 日期 | 来源域名 | 主题 | 摘要）
{代表文章清单}

请直接输出 Markdown 周报正文。"""


# 分类中文标签（与聊天召回抽屉一致）
_CATEGORY_LABELS = {
    'today': '行业动态', 'event': '行业动态', 'trend': '趋势观察',
    'policy': '政策法规', 'recent': '最近关注', 'other': '其他资讯',
}


def _label(category: str) -> str:
    return _CATEGORY_LABELS.get(str(category or '').strip(), str(category or '其他资讯').strip() or '其他资讯')


# ----------------------------------------------------------------------
# 建表（id 用 INTEGER PRIMARY KEY AUTOINCREMENT：兼容层在 PG 上自动翻译为 BIGSERIAL）
# ----------------------------------------------------------------------
def _ensure_tables(cursor) -> None:
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS pack_report_settings ("
        "  pack_id TEXT PRIMARY KEY,"
        "  prompt TEXT NOT NULL DEFAULT '',"
        "  updated_at TEXT NOT NULL DEFAULT ''"
        ")"
    )
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS pack_reports ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  pack_id TEXT NOT NULL,"
        "  title TEXT NOT NULL DEFAULT '',"
        "  time_start TEXT NOT NULL DEFAULT '',"
        "  time_end TEXT NOT NULL DEFAULT '',"
        "  generated_at TEXT NOT NULL DEFAULT '',"
        "  content_markdown TEXT NOT NULL DEFAULT '',"
        "  source TEXT NOT NULL DEFAULT 'scheduled',"
        "  article_count INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_pack_reports_pack ON pack_reports(pack_id)"
    )


def _db_execute(sql: str, params=()):
    """在 sqlite_db 事务锁内执行一条语句（自动建表 + 重连）。"""
    sqlite_db._ensure_connection()
    with sqlite_db.lock:
        cursor = sqlite_db.connection.cursor()
        try:
            _ensure_tables(cursor)
            cursor.execute(sql, params)
            sqlite_db.connection.commit()
        finally:
            cursor.close()


# ----------------------------------------------------------------------
# 提示词设置
# ----------------------------------------------------------------------
def get_pack_report_settings(pack_id: str) -> Dict:
    """读取某行业包的周报提示词；未配置返回默认提示词。"""
    pack_id = str(pack_id or '').strip()
    sqlite_db._ensure_connection()
    with sqlite_db.lock:
        cursor = sqlite_db.connection.cursor()
        try:
            _ensure_tables(cursor)
            cursor.execute(
                "SELECT prompt, updated_at FROM pack_report_settings WHERE pack_id=?",
                (pack_id,),
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
    prompt = str(row['prompt'] or '') if row else ''
    return {
        'prompt': prompt or DEFAULT_PACK_REPORT_PROMPT,
        'is_default': not prompt,
        'default_prompt': DEFAULT_PACK_REPORT_PROMPT,
        'updated_at': str(row['updated_at'] or '') if row else '',
    }


def save_pack_report_settings(pack_id: str, prompt: str) -> Dict:
    """保存行业包周报提示词（空则清除，回到默认）。"""
    pack_id = str(pack_id or '').strip()
    if not pack_id:
        raise ValueError('缺少行业包 ID')
    text = str(prompt or '').strip()
    if len(text) > 30000:
        raise ValueError('提示词过长（最多 30000 字符）')
    from utils import get_china_time
    now = get_china_time().strftime('%Y-%m-%d %H:%M:%S')
    _db_execute(
        "INSERT INTO pack_report_settings(pack_id, prompt, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(pack_id) DO UPDATE SET prompt=excluded.prompt, updated_at=excluded.updated_at",
        (pack_id, text, now),
    )
    return {'pack_id': pack_id, 'updated_at': now, 'is_default': not text}


# ----------------------------------------------------------------------
# 第一步：数据准备（文章清单 / 时序统计 / 主题泳道 / 代表文章）
# ----------------------------------------------------------------------
def _load_window_articles(pack_id: str, start: str, end: str, limit: int = 300) -> List[Dict]:
    """时间窗内该行业包的文章行（articles + 分类表，跨包合并去重）。"""
    pack_id = str(pack_id or '').strip()
    sqlite_db._ensure_connection()
    with sqlite_db.lock:
        rows = sqlite_db.connection.execute(
            """
            SELECT a.id, a.title, a.domain, a.url,
                   COALESCE(a.publish_date, a.first_crawled, a.created_at, '') AS article_date,
                   substr(a.content, 1, 260) AS preview,
                   c.matched_keywords_json, c.topic_tags_json, c.final_category,
                   c.trend_summary, c.industry_pack_id
            FROM articles a
            JOIN article_intel_classifications c ON c.article_id = a.id
            WHERE a.status = 'active'
              AND c.industry_pack_id = ?
              AND COALESCE(a.publish_date, a.first_crawled, a.created_at, '') >= ?
              AND COALESCE(a.publish_date, a.first_crawled, a.created_at, '') <= ?
            ORDER BY COALESCE(a.publish_date, a.first_crawled, a.created_at, '') DESC
            LIMIT ?
            """,
            (pack_id, start, end + ' 23:59:59' if len(end) == 10 else end, int(limit)),
        ).fetchall()
    merged: Dict[int, Dict] = {}
    for raw in rows:
        row = dict(raw)
        aid = int(row.get('id') or 0)
        if aid in merged:
            continue
        merged[aid] = row
    return list(merged.values())


def _parse_json_list(value) -> List[str]:
    try:
        data = json.loads(str(value or '') or '[]')
    except (TypeError, ValueError):
        data = []
    if isinstance(data, list):
        return [str(item).strip() for item in data if str(item).strip()]
    return []


def _article_topic(row: Dict, pack_topics: Dict[str, str]) -> str:
    """文章主题：优先 final_category，其次首个主题标签，映射为中文标签。"""
    category = str(row.get('final_category') or '').strip()
    if category:
        return _label(category)
    tags = _parse_json_list(row.get('topic_tags_json'))
    if tags:
        return str(tags[0])[:24]
    return '其他资讯'


def _keyword_score(row: Dict, keywords: List[str]) -> int:
    """关键词命中分（标题权重 ×1.5；锚点+核心词+主题词）。"""
    text = ' '.join([
        str(row.get('title') or ''), str(row.get('preview') or ''),
        str(row.get('trend_summary') or ''),
    ])
    score = 0.0
    for kw in keywords:
        if not kw:
            continue
        if kw in text:
            score += 1.5 if kw in str(row.get('title') or '') else 1.0
    return score


def _semantic_rank(rows: List[Dict], query: str, pack_id: str) -> List[Tuple[int, float]]:
    """向量检索排序：embedding 查询 + 全库矩阵余弦相似度；任何失败返回 []（降级关键词）。"""
    try:
        import numpy as np
        from chat_api import _load_vector_matrix
        from embedding_client import get_embedding_client

        ids, matrix = _load_vector_matrix()
        if not ids or matrix is None or not matrix.shape[0]:
            return []
        client = get_embedding_client(pack_id)
        client.max_retries = 0
        qvec = np.asarray(client.embed(query), dtype=np.float32).reshape(1, -1)
        norm = float(np.linalg.norm(qvec))
        if norm > 0:
            qvec = qvec / norm
        scores = (matrix @ qvec.T).ravel()
        window_ids = {int(r.get('id') or 0) for r in rows}
        return [(int(ids[i]), float(scores[i])) for i in range(len(ids))
                if int(ids[i]) in window_ids and float(scores[i]) > 0.0]
    except Exception as exc:
        print(f"⚠️ 周报向量排序不可用（降级关键词）: {exc}")
        return []


def _build_report_data(pack_id: str, start: str, end: str) -> Dict:
    """计算报告所需的全部统计与代表文章清单。"""
    from industry_packs import industry_pack_loader
    try:
        pack = industry_pack_loader.load(pack_id, enabled_only=False) or {}
    except Exception:
        pack = {}
    gate = pack.get('candidate_gate') or {}
    anchors = [str(x).strip() for x in (gate.get('anchor_keywords') or []) if str(x).strip()]
    if not anchors:
        anchors = [str(x).strip() for x in (pack.get('core_keywords') or []) if str(x).strip()]
    topics = [str(x).strip() for x in (pack.get('trend_topics') or []) if str(x).strip()]
    fixed = pack.get('fixed_topics') or []
    topic_names = [str(t.get('name') or t.get('key') or '').strip() for t in fixed if isinstance(t, dict)]
    topic_names = [t for t in topic_names if t]
    query_words = list(dict.fromkeys(anchors + topics + topic_names))[:24]
    query = ' '.join(query_words) or str(pack.get('name') or '')

    rows = _load_window_articles(pack_id, start, end)
    # 相关度排序：向量检索失败降级关键词命中分
    ranked = _semantic_rank(rows, query, pack_id)
    if ranked:
        score_map = dict(ranked)
        rows = sorted(rows, key=lambda r: score_map.get(int(r.get('id') or 0), 0.0), reverse=True)
    elif query_words:
        rows = sorted(rows, key=lambda r: _keyword_score(r, query_words), reverse=True)

    # 每日文章数时序（时间窗内每天一个点，无文章的天补 0）
    daily: Dict[str, int] = {}
    for row in rows:
        day = str(row.get('article_date') or '')[:10]
        if day:
            daily[day] = daily.get(day, 0) + 1
    try:
        start_dt = datetime.strptime(start[:10], '%Y-%m-%d')
        end_dt = datetime.strptime(end[:10], '%Y-%m-%d')
    except ValueError:
        start_dt, end_dt = datetime.now(), datetime.now()
    day_points: List[str] = []
    cursor = start_dt
    while cursor <= end_dt:
        day_points.append(cursor.strftime('%Y-%m-%d'))
        cursor += timedelta(days=1)
    daily_lines = [f"{d} → {daily.get(d, 0)} 篇" for d in day_points]

    # 主题泳道：逐主题每日计数 + 合计 + 爆发检测
    per_topic: Dict[str, Dict[str, int]] = {}
    topic_of_row: Dict[int, str] = {}
    for row in rows:
        topic = _article_topic(row, {t: t for t in topic_names})
        topic_of_row[int(row.get('id') or 0)] = topic
        per_topic.setdefault(topic, {})
        day = str(row.get('article_date') or '')[:10]
        if day:
            per_topic[topic][day] = per_topic[topic].get(day, 0) + 1
    topic_lines: List[str] = []
    for topic, series in sorted(per_topic.items(), key=lambda kv: -sum(kv[1].values())):
        counts = [series.get(d, 0) for d in day_points]
        total = sum(counts)
        if not counts:
            continue
        mean = total / max(1, len(counts))
        variance = sum((c - mean) ** 2 for c in counts) / max(1, len(counts))
        std = variance ** 0.5
        burst_days = []
        for idx, c in enumerate(counts):
            if c <= 0:
                continue
            prev = counts[idx - 1] if idx > 0 else 0
            if c >= max(3, round(mean + 2 * std)) or (prev > 0 and c >= prev * 2):
                burst_days.append(day_points[idx][5:])
        series_text = ', '.join(f"{d[5:]}:{c}" for d, c in zip(day_points, counts))
        line = f"{topic} | {series_text} | 合计 {total} 篇 | 爆发日 {('、'.join(burst_days)) if burst_days else '无'}"
        topic_lines.append(line)

    # 代表文章清单：每主题 top3 + 按总体排序补齐
    selected: List[Dict] = []
    seen_ids = set()
    per_topic_selected: Dict[str, int] = {}
    for row in rows:
        if len(selected) >= 60:
            break
        aid = int(row.get('id') or 0)
        if aid in seen_ids:
            continue
        topic = topic_of_row.get(aid, '其他资讯')
        if per_topic_selected.get(topic, 0) >= 3:
            continue
        per_topic_selected[topic] = per_topic_selected.get(topic, 0) + 1
        seen_ids.add(aid)
        selected.append(row)
    for row in rows:
        if len(selected) >= 60:
            break
        aid = int(row.get('id') or 0)
        if aid not in seen_ids:
            seen_ids.add(aid)
            selected.append(row)

    article_lines: List[str] = []
    for idx, row in enumerate(selected, start=1):
        title = ' '.join(str(row.get('title') or '').split())[:80]
        day = str(row.get('article_date') or '')[:10]
        domain = str(row.get('domain') or '').replace('https://', '').replace('http://', '').split('/')[0][:40]
        topic = topic_of_row.get(int(row.get('id') or 0), '其他资讯')
        preview = ' '.join(str(row.get('preview') or '').split())[:120]
        article_lines.append(f"{idx}. {title} | {day} | {domain} | {topic} | {preview}")

    return {
        'pack_name': str(pack.get('name') or pack_id),
        'anchors_text': '、'.join(anchors[:16]) or '（未配置锚点词）',
        'topics_text': '、'.join(topic_names[:10]) or '、'.join(topics[:10]) or '（未配置主题维度）',
        'window_text': f"{start[:10]} ~ {end[:10]}",
        'total': len(rows),
        'daily_lines': daily_lines,
        'topic_lines': topic_lines,
        'article_lines': article_lines,
    }


def _fill_prompt(template: str, data: Dict) -> str:
    return (
        str(template)
        .replace('{行业名称}', str(data.get('pack_name') or ''))
        .replace('{行业锚点}', str(data.get('anchors_text') or ''))
        .replace('{主题维度}', str(data.get('topics_text') or ''))
        .replace('{时间窗}', str(data.get('window_text') or ''))
        .replace('{文章总数}', str(int(data.get('total') or 0)))
        .replace('{每日文章数统计}', '\n'.join(data.get('daily_lines') or []) or '（无）')
        .replace('{主题时序统计}', '\n'.join(data.get('topic_lines') or []) or '（无）')
        .replace('{代表文章清单}', '\n'.join(data.get('article_lines') or []) or '（无）')
    )


# ----------------------------------------------------------------------
# 第二步：LLM 生成 Markdown
# ----------------------------------------------------------------------
# 报告正文可读性归一：LLM 偶尔输出 LaTeX 行内公式（如 $\rightarrow$），前端不渲染数学，
# 直接显示原始符号很难读。这里把常见 LaTeX 记号换成 Unicode，其余 $...$ 剥壳保留内文。
_LATEX_TOKEN_MAP = {
    r"\rightarrow": "→", r"\longrightarrow": "→", r"\leftarrow": "←",
    r"\uparrow": "↑", r"\downarrow": "↓", r"\updownarrow": "↕",
    r"\times": "×", r"\cdot": "·", r"\approx": "≈", r"\sim": "~",
    r"\geq": "≥", r"\ge": "≥", r"\leq": "≤", r"\le": "≤",
    r"\neq": "≠", r"\pm": "±", r"\propto": "∝", r"\in": "∈",
    r"\to": "→", r"\leftrightarrow": "↔", r"\cdots": "…", r"\ldots": "…",
}


def _normalize_report_markdown(text: str) -> str:
    """去掉 LaTeX 外壳并替换常见记号，提升纯文本/Markdown 前端可读性。"""
    value = str(text or "")
    for token, replacement in _LATEX_TOKEN_MAP.items():
        value = value.replace(token, replacement)
    # 其余 $...$ 行内数学：剥掉 $ 与反斜杠，保留内文（如 $x$ → x）
    value = re.sub(r"\$([^$\n]{1,80})\$", lambda m: m.group(1).replace("\\", ""), value)
    # 连续多次替换后清理多余空白与残留空段落
    value = re.sub(r"[ \t]+\n", "\n", value)
    return value


def _llm_markdown(prompt: str) -> str:
    """调用首页本地 LLM 生成 Markdown 正文（长超时，一次紧凑重试）。"""
    from intel_llm_client import IntelLLMError, intel_llm_client
    if not intel_llm_client.configured:
        raise IntelLLMError('本地 LLM 尚未完成配置，无法生成周报')
    runtime = intel_llm_client._local_runtime()
    payload = {
        'model': runtime['model_id'],
        'stream': False,
        'temperature': 0.3,
        'max_tokens': 8192,
        'enable_thinking': False,
        'messages': [
            {'role': 'system', 'content': '你是严谨的行业情报分析师，只输出 Markdown 周报正文。'},
            {'role': 'user', 'content': prompt},
        ],
    }
    timeout = max(300, int(getattr(config, 'INTEL_LLM_TIMEOUT_SECONDS', 120)))
    try:
        response = intel_llm_client._request_local(
            runtime, payload, timeout_seconds=timeout, max_retries=0,
        )
    except IntelLLMError:
        retry = dict(payload)
        retry['messages'] = [dict(item) for item in payload['messages']]
        retry['temperature'] = 0.1
        response = intel_llm_client._request_local(
            runtime, retry, timeout_seconds=timeout, max_retries=0,
        )
    try:
        body = response.json()
        content = str((body.get('choices') or [{}])[0].get('message', {}).get('content') or '')
    except (ValueError, IndexError, KeyError, TypeError) as exc:
        raise IntelLLMError('本地 LLM 周报响应无效') from exc
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.IGNORECASE | re.DOTALL).strip()
    content = re.sub(r'^```(?:markdown)?\s*|\s*```$', '', content, flags=re.IGNORECASE).strip()
    if not content:
        raise IntelLLMError('本地 LLM 未返回周报正文')
    return content


# ----------------------------------------------------------------------
# 生成 + 落库（按 pack+时间窗+来源 upsert，重试幂等）
# ----------------------------------------------------------------------
def generate_pack_report(pack_id: str, time_start: str, time_end: str,
                         prompt_override: Optional[str] = None,
                         source: str = 'scheduled') -> Dict:
    pack_id = str(pack_id or '').strip()
    start = str(time_start or '')[:10]
    end = str(time_end or '')[:10]
    if not pack_id or not start or not end:
        raise ValueError('行业包 ID / 起始时间 / 结束时间不能为空')
    if start > end:
        raise ValueError('起始时间不能晚于结束时间')

    data = _build_report_data(pack_id, start, end)
    if int(data.get('total') or 0) <= 0:
        raise ValueError(f"时间窗 {start} ~ {end} 内没有该行业的文章，无法生成报告")

    settings = get_pack_report_settings(pack_id)
    template = str(prompt_override or '').strip() or str(settings.get('prompt') or '') or DEFAULT_PACK_REPORT_PROMPT
    prompt = _fill_prompt(template, data)
    # LaTeX 行内公式可读化（$\rightarrow$ → → 等），保证落库正文无需数学渲染即可读
    markdown = _normalize_report_markdown(_llm_markdown(prompt))

    # 标题取正文首个一级标题；LLM 未给出则按行业名+时间窗兜底
    title_match = re.search(r'^#\s+(.+)$', markdown, flags=re.MULTILINE)
    title = ' '.join(str(title_match.group(1)).split())[:80] if title_match else ''
    if not title:
        title = f"{data.get('pack_name') or '行业'}周报（{start[5:]} ~ {end[5:]}）"

    from utils import get_china_time
    now = get_china_time().strftime('%Y-%m-%d %H:%M:%S')
    # upsert：同 pack+时间窗+来源 已存在则更新（worker 重试幂等，不产生重复报告）
    sqlite_db._ensure_connection()
    with sqlite_db.lock:
        cursor = sqlite_db.connection.cursor()
        try:
            _ensure_tables(cursor)
            cursor.execute(
                "SELECT id FROM pack_reports WHERE pack_id=? AND time_start=? AND time_end=? AND source=?"
                " ORDER BY id DESC LIMIT 1",
                (pack_id, start, end, str(source or 'scheduled')),
            )
            row = cursor.fetchone()
            if row:
                report_id = int(row['id'])
                cursor.execute(
                    "UPDATE pack_reports SET title=?, generated_at=?, content_markdown=?, article_count=? WHERE id=?",
                    (title, now, markdown, int(data.get('total') or 0), report_id),
                )
            else:
                cursor.execute(
                    "INSERT INTO pack_reports(pack_id, title, time_start, time_end, generated_at,"
                    "  content_markdown, source, article_count) VALUES(?,?,?,?,?,?,?,?)",
                    (pack_id, title, start, end, now, markdown, str(source or 'scheduled'), int(data.get('total') or 0)),
                )
                report_id = int(cursor.lastrowid)
            sqlite_db.connection.commit()
        finally:
            cursor.close()
    # 注意力方向：把「下周关注」的线索固化成追踪项 + 动态主题（关键词跟随周报变化）。
    # 失败绝不影响周报本身，只打印告警。
    try:
        from pack_attention import sync_from_report
        sync_from_report(pack_id, report_id, markdown)
    except Exception as _attention_exc:
        print(f"⚠️ 注意力方向同步失败（周报已正常入库）: {_attention_exc}")
    return {
        'id': report_id,
        'pack_id': pack_id,
        'title': title,
        'time_start': start,
        'time_end': end,
        'generated_at': now,
        'source': str(source or 'scheduled'),
        'article_count': int(data.get('total') or 0),
    }


def run_pack_report_job(payload: Dict) -> Dict:
    """intel_worker 的 pack_report job 入口；异常交给 worker 记 last_error。"""
    pack_id = str((payload or {}).get('industry_pack_id') or '').strip()
    start = str((payload or {}).get('time_start') or '').strip()
    end = str((payload or {}).get('time_end') or '').strip()
    source = str((payload or {}).get('source') or 'scheduled')
    report = generate_pack_report(pack_id, start, end, source=source)
    return {
        'success': True,
        'report_id': report['id'],
        'title': report['title'],
        'article_count': report['article_count'],
        'generated_at': report['generated_at'],
    }


# ----------------------------------------------------------------------
# 读取
# ----------------------------------------------------------------------
def list_pack_reports(pack_id: str, limit: int = 50) -> List[Dict]:
    pack_id = str(pack_id or '').strip()
    if not pack_id:
        return []
    sqlite_db._ensure_connection()
    with sqlite_db.lock:
        cursor = sqlite_db.connection.cursor()
        try:
            _ensure_tables(cursor)
            cursor.execute(
                "SELECT id, pack_id, title, time_start, time_end, generated_at, source, article_count"
                " FROM pack_reports WHERE pack_id=? ORDER BY generated_at DESC, id DESC LIMIT ?",
                (pack_id, int(limit)),
            )
            return [dict(r) for r in cursor.fetchall()]
        finally:
            cursor.close()


def get_pack_report_markdown(report_id: int, pack_id: str = '') -> Optional[str]:
    sqlite_db._ensure_connection()
    with sqlite_db.lock:
        cursor = sqlite_db.connection.cursor()
        try:
            _ensure_tables(cursor)
            if pack_id:
                cursor.execute(
                    "SELECT content_markdown FROM pack_reports WHERE id=? AND pack_id=?",
                    (int(report_id), str(pack_id)),
                )
            else:
                cursor.execute(
                    "SELECT content_markdown FROM pack_reports WHERE id=?", (int(report_id),)
                )
            row = cursor.fetchone()
            return str(row['content_markdown']) if row else None
        finally:
            cursor.close()


def get_pack_report(report_id: int, pack_id: str = '') -> Optional[Dict]:
    """读取单份报告元数据（不含正文），供分享落地页/引用使用。"""
    sqlite_db._ensure_connection()
    with sqlite_db.lock:
        cursor = sqlite_db.connection.cursor()
        try:
            _ensure_tables(cursor)
            if pack_id:
                cursor.execute(
                    "SELECT id, pack_id, title, time_start, time_end, generated_at, source,"
                    " article_count FROM pack_reports WHERE id=? AND pack_id=?",
                    (int(report_id), str(pack_id)),
                )
            else:
                cursor.execute(
                    "SELECT id, pack_id, title, time_start, time_end, generated_at, source,"
                    " article_count FROM pack_reports WHERE id=?", (int(report_id),)
                )
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            cursor.close()


def last_report_info(pack_id: str) -> Optional[Dict]:
    """最近一次报告生成日期/标题（设置页展示用）。"""
    reports = list_pack_reports(pack_id, 1)
    return reports[0] if reports else None


# ----------------------------------------------------------------------
# API 蓝图
# ----------------------------------------------------------------------
pack_report_bp = Blueprint('pack_report', __name__, url_prefix='/api/pack-reports')


def _pack_from_request() -> str:
    data = request.get_json(silent=True) or {}
    return str(data.get('industry_pack_id') or request.args.get('industry_pack_id') or '').strip()


@pack_report_bp.route('/settings', methods=['GET'])
@login_required
def report_settings_get():
    pack_id = _pack_from_request()
    if not pack_id:
        return jsonify({'success': False, 'error': '缺少行业包 ID'}), 400
    try:
        settings = get_pack_report_settings(pack_id)
        last = last_report_info(pack_id)
        return jsonify({
            'success': True,
            **settings,
            'last_report': last,
        })
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)[:200]}), 400


@pack_report_bp.route('/settings', methods=['PUT'])
@login_required
def report_settings_put():
    data = request.get_json(silent=True) or {}
    pack_id = str(data.get('industry_pack_id') or '').strip()
    if not pack_id:
        return jsonify({'success': False, 'error': '缺少行业包 ID'}), 400
    try:
        result = save_pack_report_settings(pack_id, data.get('prompt') or '')
        return jsonify({'success': True, **result})
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)[:200]}), 400


@pack_report_bp.route('/generate', methods=['POST'])
@login_required
def report_generate():
    """测试提示词效果：派发一次 LLM 总结任务（时间窗=最近7天，source=test）。"""
    from intel_database import IntelRepository
    from utils import get_china_time
    data = request.get_json(silent=True) or {}
    pack_id = str(data.get('industry_pack_id') or '').strip()
    if not pack_id:
        return jsonify({'success': False, 'error': '缺少行业包 ID'}), 400
    now = get_china_time()
    end = now.strftime('%Y-%m-%d')
    start = (now - timedelta(days=7)).strftime('%Y-%m-%d')
    # 归属当前登录用户（管理员 user_id / 包用户 pack_user_id），便于任务页轮询权限校验
    current = getattr(request, 'current_user', {}) or {}
    created_by = str(current.get('user_id') or current.get('pack_user_id') or '').strip() or 'pack-report-test'
    try:
        repository = IntelRepository()
        job_id, inserted = repository.enqueue_job(
            'pack_report',
            f"pack-report:test:{pack_id}:{uuid.uuid4().hex[:12]}",
            {
                'industry_pack_id': pack_id,
                'time_start': start,
                'time_end': end,
                'source': 'test',
            },
            priority=30,
            created_by=created_by,
        )
        return jsonify({
            'success': True,
            'job_id': job_id,
            'message': f"已派发 LLM 总结任务（{start} ~ {end}），完成后到首页「报告」查看",
        })
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)[:200]}), 400


@pack_report_bp.route('/', methods=['GET'], strict_slashes=False)
@login_required
def report_list():
    pack_id = _pack_from_request()
    if not pack_id:
        return jsonify({'success': False, 'error': '缺少行业包 ID'}), 400
    try:
        reports = list_pack_reports(pack_id)
        return jsonify({'success': True, 'reports': reports})
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)[:200]}), 400


@pack_report_bp.route('/<int:report_id>/markdown', methods=['GET'])
@login_required
def report_markdown(report_id: int):
    pack_id = str(request.args.get('industry_pack_id') or '').strip()
    markdown = get_pack_report_markdown(report_id, pack_id or '')
    if markdown is None:
        return jsonify({'success': False, 'error': '报告不存在'}), 404
    return Response(markdown, mimetype='text/markdown; charset=utf-8')
