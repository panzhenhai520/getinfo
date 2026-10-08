#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pack-isolated article and web retrieval for unified QA level one."""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from intel_topics import _classification_admitted
from qa_policy_evidence import detect_policy_anchors, infer_policy_document_metadata


# ── 时间窗硬约束（阶段 4）─────────────────────────────────────────────────
# 原实现只给"区间内"加 8 分、不做过滤：问"2026 年 1 月工信部说了什么"，
# 2026-10-04 的新文照样能被选进证据。这里升级为「硬过滤 + 排序」双条件，
# 并配一把可配的分级扩窗梯子：窗口内一条都没有时逐级放宽，
# 每放宽一级都回报给用户（绝不静默给过期答案，也绝不静默滤空）。
_DISPLAY_ZONE = "Asia/Hong_Kong"
_LADDER_UNITS = {"d": 1, "w": 7, "m": 30, "y": 365}


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().casefold() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(str(os.getenv(name, str(default))).strip())
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _time_ladder() -> list:
    """`QA_TIME_EXPAND_LADDER`（默认 3m,6m,1y）→ [90, 180, 365]，逐级放宽的天数。"""
    raw = str(os.getenv("QA_TIME_EXPAND_LADDER", "3m,6m,1y") or "").strip()
    steps = []
    for token in raw.split(","):
        token = token.strip().casefold()
        match = re.fullmatch(r"(\d+)\s*([dwmy]?)", token)
        if not match:
            continue
        days = int(match.group(1)) * _LADDER_UNITS.get(match.group(2) or "d", 1)
        if days > 0 and days not in steps:
            steps.append(days)
    return sorted(steps) or [90, 180, 365]


def _ladder_label(days: int) -> str:
    if days % 365 == 0:
        return "近 %d 年" % (days // 365)
    if days % 30 == 0:
        return "近 %d 个月" % (days // 30)
    return "近 %d 天" % days


def _zone(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def _parse_day(value) -> "date | None":
    text = str(value or "").strip()
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _article_day(row):
    """文章"源站本地日期"；判断不了返回 None（None = 不参与硬过滤）。

    约定与写入方一致：precision ∈ {date,day,url} 时 published_at_utc 前 10 位就是源站本地日期；
    精确瞬间则换算到 published_timezone；precision='discovered'（只有抓取时间）一律视作未知。
    """
    precision = str(row.get("published_precision") or "").strip().casefold()
    if precision == "discovered":
        return None
    instant = str(row.get("published_at_utc") or "").strip()
    if instant:
        if precision in {"date", "day", "url"} or "T" not in instant:
            return _parse_day(instant)
        parsed = None
        try:
            from financial_evidence import _parse_stored_datetime

            parsed = _parse_stored_datetime(instant)
        except Exception:
            try:
                parsed = datetime.fromisoformat(instant.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        zone = _zone(str(row.get("published_timezone") or "") or _DISPLAY_ZONE)
        return parsed.astimezone(zone).date()
    return _parse_day(row.get("publish_date"))


def _window_days(time_window):
    """时间窗口 → (起, 止) 两个"展示时区日期"；拿不到返回 None。"""
    start = time_window.get("start")
    end = time_window.get("end")
    if not start or not end:
        return None
    zone = _zone(_DISPLAY_ZONE)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return start.astimezone(zone).date(), end.astimezone(zone).date()


def _item_day(item):
    return item[5] if len(item) > 5 else None


# 时间精度 → 给人看的一句话（前端引用/证据卡直接展示；不写"未知"就等于骗人）
_PRECISION_LABELS = {
    "exact": "精确到时分",
    "date": "仅到日",
    "url": "按链接日期推断",
    "discovered": "时间未知（按首次抓取标记）",
}


def normalize_precision(value) -> str:
    """精度归一：兼容历史写法（day/datetime）与空值。"""
    try:
        from publish_time import normalize_precision as _normalize

        return _normalize(value)
    except Exception:
        text = str(value or "").strip().casefold()
        return {"day": "date", "datetime": "exact"}.get(text, text)


def describe_published_precision(precision: str, timezone_name: str = "") -> str:
    """把精度说成一句人话（带源站时区时一并说明）。抽不到就是空串。

    入参先归一化，这样调用方传历史写法（`day`/`datetime`）也不会静默变成空串。
    """
    text = normalize_precision(precision)
    if not text:
        return ""
    label = _PRECISION_LABELS.get(text, "")
    if not label:
        return ""
    zone = str(timezone_name or "").strip()
    if zone and text in {"exact", "date", "url"}:
        return f"{label}（源站时区 {zone}）"
    return label


def _window_from_adjustment(plan: Mapping):
    """用户在调整里明确给出的时间范围 → 检索窗口（阶段 5）；没有则返回 None。

    plan 里存的是 ISO 字符串（plan 会进 SSE 事件，放 datetime 会序列化失败）。
    """
    adjustment = plan.get("time_window_adjustment") if isinstance(plan, Mapping) else None
    if not isinstance(adjustment, Mapping):
        return None
    days = adjustment.get("days")
    start = _parse_iso(str(adjustment.get("start") or ""))
    end = _parse_iso(str(adjustment.get("end") or ""))
    if start is None and end is None:
        try:
            days = int(days)
        except (TypeError, ValueError):
            return None
        if days <= 0:
            return None
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
    if start is None:
        start = (end or datetime.now(timezone.utc)) - timedelta(days=int(days or 30))
    if end is None:
        end = datetime.now(timezone.utc)
    return {
        "has_time": True,
        "days": int(days or max(1, (end - start).days)),
        "start": start,
        "end": end,
        "label": str(adjustment.get("label") or "用户指定的时间范围"),
        "source": "user_adjustment",
    }


def _parse_iso(value: str):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def apply_time_gate(ranked, time_window, *, hard_filter=True, min_in_window=1, ladder=None):
    """时间硬过滤 + 分级扩窗（阶段 4 核心，独立成函数便于单测）。

    ranked 元素形如 (score, 排序日期, article_id, row, reason, 本地日期|None)。
    规则：
      1. 关掉硬过滤 / 问题没带时间 → 原样返回（退回旧的"只加权"行为）；
      2. 先用问题窗口硬过滤；窗口内不足 min_in_window 条 → 按梯子逐级向前放宽
         （默认 3 个月 → 6 个月 → 1 年）；
      3. 本地日期未知的文章**不丢**，排在窗口内证据之后并计数——宁可少过滤，
         也不静默丢证据。
    返回 (保留条目, 回执 dict)；回执带 dropped_out_of_window / unknown_time_kept /
    ladder_step / ladder_days / note，供上层原样回报给用户。
    """
    receipt = {"hard_filter": False, "ladder_step": 0, "ladder_days": 0,
               "dropped_out_of_window": 0, "unknown_time_kept": 0, "note": ""}
    if not hard_filter or not ranked or not time_window.get("has_time"):
        return ranked, receipt
    bounds = _window_days(time_window)
    if bounds is None:
        return ranked, receipt
    start_day, end_day = bounds
    known = [item for item in ranked if _item_day(item) is not None]
    unknown = [item for item in ranked if _item_day(item) is None]
    steps = [0] + list(ladder if ladder is not None else _time_ladder())
    kept, used_days, used_step = [], 0, 0
    for step_index, extra_days in enumerate(steps):
        lower = start_day - timedelta(days=extra_days)
        kept = [item for item in known if lower <= _item_day(item) <= end_day]
        used_days, used_step = extra_days, step_index
        if len(kept) >= int(min_in_window):
            break
    receipt.update({
        "hard_filter": True,
        "ladder_step": used_step,
        "ladder_days": used_days,
        "dropped_out_of_window": max(0, len(known) - len(kept)),
        "unknown_time_kept": len(unknown),
    })
    if used_step > 0:
        receipt["note"] = (
            "在 %s 内的资料不足 %d 条，已把时间窗向前放宽到%s（时间窗内证据 %d 条）"
            % (time_window.get("label") or "指定时间范围", int(min_in_window),
               _ladder_label(used_days), len(kept))
        )
    return kept + unknown, receipt


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{1,48}|[\u3400-\u9fff]{2,24}")
_STOP = {
    "什么", "哪些", "如何", "怎么", "是否", "请问", "介绍", "分析", "一下", "有关",
    "关于", "目前", "当前", "最近", "这个", "那个", "以及", "and", "the", "what",
    "已经", "了吗", "运行", "开始", "现在", "最新", "有没有", "已经在", "运行了",
}
_BROAD_INDUSTRY_TERMS = {
    "行业", "产业", "市场", "政策", "公告", "法规", "影响", "风险", "应对",
    "内容", "具体", "资料", "材料", "证据", "检索", "新闻", "资讯", "研究",
}
_ANCHOR_NOISE_FRAGMENTS = (
    "已经", "了吗", "运行", "目前", "现在", "最新", "是否", "什么", "哪些",
    "如何", "怎么", "有关", "关于",
)
_QUERY_TARGET_TERMS = {
    "哪个", "哪家", "哪个公司", "公司", "企业", "机构", "主体", "对象", "是谁", "什么",
}
_AMOUNT_UNIT_TERMS = {"亿元", "亿", "万元", "万", "千元", "人民币", "美元", "港元", "元"}
_AMOUNT_RE = re.compile(
    r"(?:(?:近|约|超|逾|超过|累计|合计|达到|获得|完成|融了|融资|募资|投资)\s*)?"
    r"\d+(?:\.\d+)?\s*(?:亿元|亿|万元|万|千元|人民币|美元|港元|元)",
    re.I,
)
_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid"}
_MOJIBAKE_MARKERS = ("Ã", "Â", "å", "ä", "ç", "æ", "è", "é", "ï¼", "ã")
_POLICY_NOTICE_RE = re.compile(
    r"(20\d{2})\s*年.{0,12}?(?:公告|第)?\s*(\d{1,4})\s*[号號]|"
    r"(?:公告|第)?\s*(\d{1,4})\s*[号號](?:公告)?",
    re.I,
)
_POLICY_ISSUER_TERMS = (
    "财政部", "財政部", "税务总局", "稅務總局", "国家税务总局", "國家稅務總局",
    "税务局", "稅務局", "国务院", "國務院", "证监会", "證監會", "银保监", "銀保監",
)
_POLICY_TOPIC_TERMS = (
    "离岸信托", "離岸信託", "岸外信托", "岸外信託", "个人所得税", "個人所得稅",
    "个税", "個稅", "征管", "徵管", "家族办公室", "家族辦公室", "家办", "家辦",
    "家族企业", "家族企業", "离岸资产", "離岸資產", "境外资产", "境外資產",
    "信托", "信託", "税收", "稅收", "税务", "稅務", "合规", "合規",
)


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_http_url(value: str) -> str:
    raw = html.unescape(str(value or "").strip())
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        return ""
    if parts.username or parts.password:
        return ""
    host = parts.hostname.casefold().rstrip(".")
    try:
        # Public links may resolve to a private address later, but literal
        # private/loopback IPs are never exposed as external-search results.
        address = ipaddress.ip_address(host)
        if not address.is_global:
            return ""
    except ValueError:
        pass
    try:
        port = parts.port
    except ValueError:
        return ""
    default_port = (parts.scheme.casefold() == "http" and port == 80) or (parts.scheme.casefold() == "https" and port == 443)
    netloc = host if not port or default_port else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.casefold() not in _TRACKING])
    return urlunsplit((parts.scheme.casefold(), netloc, path, query, ""))


def _json(value, default):
    if isinstance(value, (dict, list)):
        return value
    try:
        decoded = json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return decoded if isinstance(decoded, type(default)) else default


def repair_mojibake(value: str) -> str:
    """Repair UTF-8 bytes that were previously decoded as Latin-1."""
    text = str(value or "")
    marker_count = sum(text.count(marker) for marker in _MOJIBAKE_MARKERS)
    if marker_count < 1:
        return text

    def try_repair(part: str) -> str:
        try:
            candidate = part.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return part
        before_cjk = len(re.findall(r"[\u3400-\u9fff]", part))
        after_cjk = len(re.findall(r"[\u3400-\u9fff]", candidate))
        before_markers = sum(part.count(marker) for marker in _MOJIBAKE_MARKERS)
        after_markers = sum(candidate.count(marker) for marker in _MOJIBAKE_MARKERS)
        # Pure punctuation fragments such as ``ï¼`` contain no CJK ideograph,
        # so CJK growth alone would leave them corrupted.  A strictly lower
        # mojibake-marker count is sufficient as long as decoding succeeds and
        # it does not remove existing CJK text.
        return candidate if (
            after_markers < before_markers
            and after_cjk >= before_cjk
        ) else part

    def repair_part(part: str) -> str:
        repaired = try_repair(part)
        if repaired != part:
            return repaired
        # A line may contain a valid Chinese label followed by a corrupted
        # Latin-1-decoded fragment.  Repair each byte-preserving span instead
        # of abandoning the whole line when one valid CJK character cannot be
        # encoded as Latin-1.
        return re.sub(r"[\x00-\xff]+", lambda match: try_repair(match.group(0)), part)

    # Long pages can contain a valid Chinese footer next to a corrupted body;
    # repairing line-by-line preserves the valid portion instead of making the
    # whole conversion fail on one non-Latin-1 character.
    repaired_lines = [repair_part(part) for part in text.split("\n")]

    # HTML extraction can insert a newline in the middle of a UTF-8 byte
    # sequence after the bytes were mis-decoded.  Such individual lines cannot
    # decode, but the consecutive marker-only fragments can be safely joined
    # and repaired as one byte-preserving span.
    joined_lines = []
    index = 0
    while index < len(repaired_lines):
        line = repaired_lines[index]
        has_markers = any(marker in line for marker in _MOJIBAKE_MARKERS)
        has_cjk = bool(re.search(r"[\u3400-\u9fff]", line))
        if not has_markers or has_cjk:
            joined_lines.append(line)
            index += 1
            continue
        fragments = [line]
        cursor = index + 1
        while cursor < len(repaired_lines):
            candidate_line = repaired_lines[cursor]
            if not any(marker in candidate_line for marker in _MOJIBAKE_MARKERS):
                break
            if re.search(r"[\u3400-\u9fff]", candidate_line):
                break
            fragments.append(candidate_line)
            cursor += 1
        combined = "".join(fragments)
        fixed = try_repair(combined)
        if fixed != combined:
            joined_lines.append(fixed)
        else:
            joined_lines.extend(fragments)
        index = cursor
    return "\n".join(joined_lines)


def _terms(values: Iterable[str]) -> list[str]:
    result = []
    for value in values:
        for token in _WORD_RE.findall(str(value or "")):
            token = token.casefold().strip("._-")
            if token and token not in _STOP and token not in result:
                result.append(token)
            if re.fullmatch(r"[\u3400-\u9fff]{4,}", token):
                for width in (2, 3, 4):
                    for index in range(len(token) - width + 1):
                        part = token[index:index + width]
                        if part not in _STOP and part not in result:
                            result.append(part)
    return result[:120]


def _anchor_terms(terms: Iterable[str]) -> list[str]:
    anchors = []
    for raw in terms:
        term = str(raw or "").strip().casefold()
        if not term or term in _STOP or term in _BROAD_INDUSTRY_TERMS or term in _QUERY_TARGET_TERMS:
            continue
        if any(fragment in term for fragment in _ANCHOR_NOISE_FRAGMENTS):
            continue
        if re.fullmatch(r"\d+", term):
            continue
        if re.fullmatch(r"[\u3400-\u9fff]{2}", term):
            continue
        if len(term) < 3:
            continue
        if term not in anchors:
            anchors.append(term)
    return anchors[:12]


def _semantic_anchor_terms(values: Iterable[str]) -> list[str]:
    anchors = []
    try:
        import jieba  # type: ignore
    except Exception:
        return anchors
    for value in values:
        cutter = getattr(jieba, "lcut", None)
        raw_tokens = cutter(str(value or "")) if callable(cutter) else list(jieba.cut(str(value or "")))
        for raw in raw_tokens:
            term = str(raw or "").strip().casefold()
            if not term or term in _STOP or term in _BROAD_INDUSTRY_TERMS or term in _QUERY_TARGET_TERMS:
                continue
            if any(fragment in term for fragment in _ANCHOR_NOISE_FRAGMENTS):
                continue
            if re.fullmatch(r"\d+|[，。！？；、,.!?;:：]+", term):
                continue
            if len(term) < 2:
                continue
            if term not in anchors:
                anchors.append(term)
    return anchors[:12]


def _amount_to_yi(value: str) -> float | None:
    raw = re.sub(r"\s+", "", str(value or "").casefold())
    match = re.search(r"(\d+(?:\.\d+)?)(亿元|亿|万元|万|千元|人民币|美元|港元|元)", raw)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2)
    if unit in {"亿元", "亿"}:
        return number
    if unit in {"万元", "万"}:
        return number / 10000
    if unit == "千元":
        return number / 100000
    return number / 100000000


def _amount_constraints(value: str) -> list[dict]:
    compact = re.sub(r"\s+", "", str(value or "").casefold())
    constraints = []
    seen = set()
    for match in _AMOUNT_RE.finditer(compact):
        raw = match.group(0)
        amount = _amount_to_yi(raw)
        if amount is None:
            continue
        approximate = bool(re.match(r"^(近|约|超|逾|超过)", raw))
        key = (round(amount, 6), approximate)
        if key in seen:
            continue
        seen.add(key)
        constraints.append({"raw": raw, "value_yi": amount, "approximate": approximate})
    return constraints[:8]


def _amount_context_terms(values: Iterable[str]) -> list[str]:
    terms = []
    for value in values:
        for token in _tokenize_query(str(value or "")):
            if token in _AMOUNT_UNIT_TERMS or token in _QUERY_TARGET_TERMS:
                continue
            if token not in terms:
                terms.append(token)
            if re.fullmatch(r"[\u4e00-\u9fff]{2,4}", token):
                root = token[0]
                if root not in terms:
                    terms.append(root)
    return terms[:12]


def _amount_constraint_hits(constraints: list[dict], text: str, *, context_terms: Iterable[str] | None = None) -> list[str]:
    if not constraints:
        return []
    hits = []
    compact = re.sub(r"\s+", "", str(text or "").casefold())
    context = [str(term or "").casefold() for term in (context_terms or []) if str(term or "").strip()]
    doc_amounts = []
    for match in _AMOUNT_RE.finditer(compact):
        raw = match.group(0)
        amount = _amount_to_yi(raw)
        if amount is not None:
            doc_amounts.append((raw, amount, match.start(), match.end()))
    for constraint in constraints:
        target = float(constraint.get("value_yi") or 0)
        if target <= 0:
            continue
        tolerance = 0.25 if constraint.get("approximate") else 0.08
        for raw, amount, start, end in doc_amounts:
            if abs(amount - target) / max(target, 1e-9) <= tolerance:
                if context:
                    window = compact[max(0, start - 30): min(len(compact), end + 30)]
                    if not any(term and term in window for term in context):
                        continue
                label = str(constraint.get("raw") or raw)
                if label not in hits:
                    hits.append(label)
                break
    return hits[:8]


def _tokenize_query(value: str) -> list[str]:
    try:
        import jieba  # type: ignore
        cutter = getattr(jieba, "lcut", None)
        raw_tokens = cutter(str(value or "")) if callable(cutter) else list(jieba.cut(str(value or "")))
    except Exception:
        raw_tokens = _WORD_RE.findall(str(value or ""))
    tokens: list[str] = []
    for raw in raw_tokens:
        term = str(raw or "").strip().casefold()
        if not term or term in _STOP or term in _QUERY_TARGET_TERMS:
            continue
        if re.fullmatch(r"\d+|[，。！？；、,.!?;:：]+", term):
            continue
        if len(term) < 2:
            continue
        if term not in tokens:
            tokens.append(term)
    return tokens[:32]


def _amount_variants(value: str) -> list[str]:
    compact = re.sub(r"\s+", "", str(value or "").casefold())
    variants: list[str] = []
    for match in _AMOUNT_RE.finditer(compact):
        raw = match.group(0)
        items = {raw}
        bare = re.sub(r"^(近|约|超|逾|超过|累计|合计|达到|获得|完成|融了|融资|募资|投资)", "", raw)
        if bare:
            items.add(bare)
        if bare.endswith("亿元"):
            items.add(bare[:-2] + "亿")
        if raw.endswith("亿元"):
            items.add(raw[:-2] + "亿")
        for item in items:
            if item and item not in variants:
                variants.append(item)
    return variants[:16]


def _query_phrases(values: Iterable[str]) -> list[str]:
    phrases: list[str] = []
    for value in values:
        text = re.sub(r"\s+", "", str(value or "").casefold())
        tokens = _tokenize_query(text)
        for width in (4, 3, 2):
            for index in range(0, max(0, len(tokens) - width + 1)):
                phrase = "".join(tokens[index:index + width])
                if len(phrase) >= 4 and phrase not in phrases:
                    phrases.append(phrase)
        for token in tokens:
            if len(token) >= 2 and token not in phrases:
                phrases.append(token)
    return phrases[:40]


def _notice_variants(year: str, number: str) -> list[str]:
    num = str(number or "").lstrip("0") or str(number or "")
    variants = [f"第{num}号", f"{num}号", f"公告{num}号", f"{num}号公告"]
    if year:
        variants.extend([
            f"{year}年第{num}号",
            f"{year}年{num}号",
            f"{year}年公告{num}号",
            f"{year}年第{num}號",
            f"{year}年{num}號",
        ])
    return list(dict.fromkeys(variants))


def _policy_query_context(plan: Mapping) -> str:
    parts = [str(plan.get("question") or "")]
    parts.extend(str(item) for item in plan.get("queries") or [])
    parts.extend(str(item) for item in plan.get("source_queries") or [])
    return " ".join(part for part in parts if part.strip())


def _policy_anchor_spec(plan: Mapping) -> dict:
    text = _policy_query_context(plan)
    anchors = detect_policy_anchors(str(plan.get("question") or text), plan)
    notices = []
    for match in _POLICY_NOTICE_RE.finditer(text):
        year = match.group(1) or ""
        number = match.group(2) or match.group(3) or ""
        if not number:
            continue
        notices.append({"year": year, "number": number.lstrip("0") or number, "variants": _notice_variants(year, number)})
    for notice in anchors.get("notices") or []:
        match = re.search(r"(?:(20\d{2})年)?\s*(\d{1,4})\s*[号號]", str(notice))
        if match:
            year, number = match.group(1) or "", match.group(2) or ""
            notices.append({"year": year, "number": number.lstrip("0") or number, "variants": _notice_variants(year, number)})
    deduped_notices = []
    seen = set()
    for notice in notices:
        key = (notice["year"], notice["number"])
        if key in seen:
            continue
        seen.add(key)
        deduped_notices.append(notice)
    issuers = [term for term in _POLICY_ISSUER_TERMS if term.casefold() in text.casefold()]
    topics = [term for term in _POLICY_TOPIC_TERMS if term.casefold() in text.casefold()]
    title_terms = [
        term for term in _terms([text])
        if len(term) >= 2 and term not in {"公告", "政策", "法规", "法規", "如何", "影响", "影響", "风险", "風險", "应对", "應對"}
    ][:24]
    return {
        "is_policy": bool(anchors.get("is_policy") or notices or issuers),
        "notices": deduped_notices[:8],
        "issuers": list(dict.fromkeys(issuers))[:8],
        "topics": list(dict.fromkeys(topics))[:12],
        "title_terms": title_terms,
    }


def _policy_match_score(row: Mapping, spec: Mapping) -> tuple[float, list[str]]:
    metadata_blob = " ".join(
        str(row.get(key) or "")
        for key in ("policy_doc_no", "policy_title", "policy_issuer", "policy_source_url", "title", "url")
    )
    content_blob = str(row.get("content") or "")[:5000]
    blob = f"{metadata_blob} {content_blob}".casefold()
    score = 0.0
    reasons = []
    doc_type = str(row.get("policy_doc_type") or "")
    if doc_type == "official_policy":
        score += 1000
        reasons.append("官方原文")
    elif doc_type == "official_interpretation":
        score += 700
        reasons.append("官方解读")
    for notice in spec.get("notices") or []:
        variants = [str(item) for item in notice.get("variants") or [] if str(item)]
        exact_doc_no = str(row.get("policy_doc_no") or "")
        if exact_doc_no and any(variant == exact_doc_no for variant in variants):
            score += 900
            reasons.append(f"法规号精确命中：{exact_doc_no}")
        elif any(variant.casefold() in blob for variant in variants):
            score += 520
            reasons.append("法规号命中")
    issuer_hits = [term for term in spec.get("issuers") or [] if str(term).casefold() in blob]
    topic_hits = [term for term in spec.get("topics") or [] if str(term).casefold() in blob]
    title_hits = [term for term in spec.get("title_terms") or [] if str(term).casefold() in metadata_blob.casefold()]
    if issuer_hits:
        score += 70 * min(3, len(issuer_hits))
        reasons.append("发布机关命中：" + "、".join(issuer_hits[:3]))
    if topic_hits:
        score += 80 * min(4, len(topic_hits))
        reasons.append("主题词命中：" + "、".join(topic_hits[:4]))
    if title_hits:
        score += 35 * min(4, len(title_hits))
        reasons.append("标题命中：" + "、".join(title_hits[:4]))
    score += min(100, int(row.get("authority_level") or 1))
    return score, list(dict.fromkeys(reasons))


def _article_evidence(row: Mapping, *, score: float, method: str, reason: str, source_type: str = "article",
                      excerpt_chars: int = 5000) -> dict:
    article_id = int(row.get("id") or 0)
    content = " ".join(repair_mojibake(row.get("content") or row.get("preview") or "").split())
    policy_meta = infer_policy_document_metadata(row)
    policy_meta.update({
        "doc_type": str(row.get("policy_doc_type") or policy_meta.get("doc_type") or ""),
        "issuer": str(row.get("policy_issuer") or policy_meta.get("issuer") or ""),
        "doc_no": str(row.get("policy_doc_no") or policy_meta.get("doc_no") or ""),
        "article_no": str(row.get("policy_article_no") or policy_meta.get("article_no") or ""),
        "policy_title": str(row.get("policy_title") or policy_meta.get("policy_title") or ""),
        "effective_date": str(row.get("policy_effective_date") or policy_meta.get("effective_date") or ""),
        "source_url": str(row.get("policy_source_url") or policy_meta.get("source_url") or ""),
    })
    authority_level = int(row.get("authority_level") or 1)
    if policy_meta.get("doc_type") == "official_policy":
        authority_level = max(authority_level, 100)
    elif policy_meta.get("doc_type") == "official_interpretation":
        authority_level = max(authority_level, 90)
    elif policy_meta.get("doc_type") == "professional_commentary":
        authority_level = max(authority_level, 50)
    elif policy_meta.get("doc_type") == "ai_qa_summary":
        authority_level = min(authority_level, 10)
    article_url = canonical_http_url(str(row.get("url") or ""))
    policy_url = canonical_http_url(str(row.get("policy_source_url") or policy_meta.get("source_url") or ""))
    # 时间精度随证据一起交给前端与合成：只到"日"的时间、以及"只有抓取时间"的时间，
    # 引用时必须让人看出可信度差别（阶段 4：published_precision 要贯通到展示）。
    _precision = normalize_precision(str(row.get("published_precision") or ""))
    _timezone = str(row.get("published_timezone") or "")
    return {
        "evidence_ref": f"page:{article_id}" if source_type == "page_context" else f"article:{article_id}",
        "source_type": source_type,
        "title": repair_mojibake(row.get("title") or "未命名文章")[:1000],
        "source_url": policy_url or article_url,
        "content_excerpt": content[:max(500, int(excerpt_chars or 5000))],
        "published_at": str(row.get("publish_date") or "") or None,
        "published_at_utc": str(row.get("published_at_utc") or "") or None,
        "published_precision": _precision,
        "published_timezone": _timezone,
        "published_time_note": describe_published_precision(_precision, _timezone),
        "fetched_at": str(row.get("first_crawled") or "") or None,
        "article_id": article_id,
        "ragflow_kb_id": None,
        "document_id": None,
        "chunk_id": None,
        "score": round(max(0.0, float(score)), 6),
        "authority_level": authority_level,
        "retrieval_method": method,
        "match_reason": str(reason)[:1000],
        "relationship": "context" if source_type == "page_context" else "supports",
        "metadata": {
            "domain": str(row.get("domain") or ""),
            "category": str(row.get("final_category") or ""),
            "article_url": article_url,
            "article_detail": f"/article-management/api/article/{article_id}" if article_id else "",
            "matched_keywords": _json(row.get("matched_keywords_json"), []),
            "topic_tags": _json(row.get("topic_tags_json"), []),
            **{key: value for key, value in policy_meta.items() if value not in ("", 0, None)},
        },
    }


class ArticleRetriever:
    def __init__(self, database, *, semantic_search: Callable | None = None):
        self.database = database
        self.semantic_search = semantic_search

    def _active_activation(self, pack_id: str) -> str:
        try:
            rows = self.database.connection.execute(
                "SELECT setting_key,setting_value FROM intel_runtime_settings "
                "WHERE setting_key IN ('active_industry_pack_id','active_industry_activation_id')"
            ).fetchall()
            values = {str(row[0]): str(row[1] or "") for row in rows}
            return values.get("active_industry_activation_id", "") if values.get("active_industry_pack_id") == pack_id else ""
        except Exception:
            return ""

    def _rows(self, pack_id: str) -> tuple[list[dict], dict[str, int]]:
        self.database._ensure_connection()
        with self.database.lock:
            active_activation = self._active_activation(pack_id)
            rows = self.database.connection.execute(
                """
                SELECT a.id,a.title,a.url,a.domain,a.content,a.publish_date,a.first_crawled,
                       a.status,a.quality_score,a.content_length,
                       a.published_at_utc,a.published_timezone,a.published_precision,
                       c.industry_pack_id,c.activation_id,c.score_details_json,
                       c.matched_keywords_json,c.topic_tags_json,c.final_category,
                       ard.doc_type AS policy_doc_type, ard.doc_no AS policy_doc_no,
                       ard.issuer AS policy_issuer, ard.article_no AS policy_article_no,
                       ard.policy_title, ard.effective_date AS policy_effective_date,
                       ard.source_url AS policy_source_url,
                       COALESCE(egp.authority_level, ard.authority_level, 1) authority_level
                FROM articles a
                LEFT JOIN (
                    SELECT ega.article_id AS article_id,
                           MAX(ega.authority_level) AS authority_level
                    FROM intel_evidence_group_articles ega
                    JOIN intel_evidence_groups eg ON eg.id=ega.evidence_group_id
                    WHERE eg.industry_pack_id=?
                    GROUP BY ega.article_id
                ) egp ON egp.article_id=a.id
                LEFT JOIN article_intel_classifications c
                  ON c.article_id=a.id AND c.industry_pack_id=?
                LEFT JOIN article_ragflow_documents ard
                  ON ard.article_id=a.id
                 AND COALESCE(ard.sync_status,'') IN ('parsed','uploaded')
                 AND COALESCE(ard.doc_type,'') IN ('official_policy','official_interpretation')
                WHERE (
                    c.industry_pack_id IS NOT NULL
                    OR egp.article_id IS NOT NULL
                    OR EXISTS (
                        SELECT 1 FROM content_industry_packs cip
                        WHERE cip.content_type='article'
                          AND cip.content_id=CAST(a.id AS TEXT)
                          AND cip.industry_pack_id=?
                          AND cip.is_active=1
                    )
                    -- 未归属任何行业包的文章也要进检索池。
                    -- 展示链路（「最近关注」走 project_keywords，不接收 industry_pack_id）
                    -- 会显示这些文章，而检索原先要求有本包分类记录 → 同一篇"页面看得到、
                    -- AI 搜不到"（实测 id=6910 就是这样被漏掉的）。
                    -- 放进池子不会污染结果：下面的关键词打分对不相关的文章得 0 分直接丢弃。
                    OR (
                        c.industry_pack_id IS NULL
                        AND NOT EXISTS (
                            SELECT 1 FROM article_intel_classifications c2
                            WHERE c2.article_id=a.id AND c2.industry_pack_id IS NOT NULL
                        )
                    )
                )
                ORDER BY COALESCE(a.publish_date,a.first_crawled,a.created_at,'') DESC,a.id DESC
                LIMIT 1000
                """,
                (str(pack_id), str(pack_id), str(pack_id)),
            ).fetchall()
        accepted, excluded = [], {"inactive": 0, "stale_activation": 0, "quality_gate": 0, "unsafe_url": 0, "policy_registry": 0}
        seen = set()
        for raw in rows:
            row = dict(raw)
            article_id = int(row.get("id") or 0)
            if article_id in seen:
                continue
            seen.add(article_id)
            is_policy_registry = str(row.get("policy_doc_type") or "") in {"official_policy", "official_interpretation"}
            if row.get("status") != "active":
                excluded["inactive"] += 1
                continue
            if active_activation and row.get("activation_id") and str(row.get("activation_id") or "") != active_activation:
                excluded["stale_activation"] += 1
                continue
            if not is_policy_registry and not _classification_admitted(_json(row.get("score_details_json"), {})):
                excluded["quality_gate"] += 1
                continue
            if not canonical_http_url(row.get("url")):
                excluded["unsafe_url"] += 1
                continue
            if is_policy_registry:
                excluded["policy_registry"] += 1
            accepted.append(row)
        return accepted, excluded

    def _policy_registry_rows(self, spec: Mapping, *, pack_id: str, limit: int = 20) -> list[dict]:
        if not spec.get("is_policy"):
            return []
        notices = spec.get("notices") or []
        if not notices and not (spec.get("issuers") and (spec.get("topics") or spec.get("title_terms"))):
            return []
        self.database._ensure_connection()
        notice_terms = []
        for notice in notices:
            notice_terms.extend(str(item) for item in notice.get("variants") or [] if str(item).strip())
        filter_terms = list(dict.fromkeys([
            *notice_terms,
            *[str(item) for item in spec.get("issuers") or []],
            *[str(item) for item in spec.get("topics") or []],
            *[str(item) for item in spec.get("title_terms") or []],
        ]))[:32]
        where = [
            "COALESCE(ard.sync_status,'') IN ('parsed','uploaded','')",
            "COALESCE(ard.doc_type,'') IN ('official_policy','official_interpretation')",
            "COALESCE(a.status,'active')='active'",
            """(
                EXISTS (
                    SELECT 1 FROM article_intel_classifications c
                    WHERE c.article_id=a.id AND c.industry_pack_id=?
                )
                OR EXISTS (
                    SELECT 1 FROM content_industry_packs cip
                    WHERE cip.content_type='article'
                      AND cip.content_id=CAST(a.id AS TEXT)
                      AND cip.industry_pack_id=?
                      AND cip.is_active=1
                )
                OR EXISTS (
                    SELECT 1 FROM intel_evidence_group_articles ega
                    JOIN intel_evidence_groups eg ON eg.id=ega.evidence_group_id
                    WHERE ega.article_id=a.id AND eg.industry_pack_id=?
                )
            )""",
        ]
        params: list[str | int] = [str(pack_id), str(pack_id), str(pack_id)]
        if filter_terms:
            like_parts = []
            for term in filter_terms[:24]:
                like_parts.append(
                    "(COALESCE(ard.doc_no,'') LIKE ? OR COALESCE(ard.policy_title,'') LIKE ? "
                    "OR COALESCE(ard.issuer,'') LIKE ? OR COALESCE(ard.source_url,'') LIKE ? "
                    "OR COALESCE(a.title,'') LIKE ? OR COALESCE(a.content,'') LIKE ?)"
                )
                params.extend([f"%{term}%"] * 6)
            where.append("(" + " OR ".join(like_parts) + ")")
        sql = f"""
            SELECT a.id,a.title,COALESCE(NULLIF(ard.source_url,''),a.url) AS url,
                   a.domain,a.content,a.publish_date,a.first_crawled,a.status,
                   a.quality_score,a.content_length,
                   a.published_at_utc,a.published_timezone,a.published_precision,
                   NULL AS industry_pack_id,NULL AS activation_id,NULL AS score_details_json,
                   NULL AS matched_keywords_json,NULL AS topic_tags_json,NULL AS final_category,
                   ard.doc_type AS policy_doc_type, ard.doc_no AS policy_doc_no,
                   ard.issuer AS policy_issuer, ard.article_no AS policy_article_no,
                   ard.policy_title, ard.publish_date AS policy_publish_date,
                   ard.effective_date AS policy_effective_date,
                   ard.source_url AS policy_source_url,
                   COALESCE(ard.authority_level,1) authority_level
            FROM article_ragflow_documents ard
            JOIN articles a ON a.id=ard.article_id
            WHERE {" AND ".join(where)}
            ORDER BY COALESCE(ard.authority_level,1) DESC,
                     CASE COALESCE(ard.doc_type,'') WHEN 'official_policy' THEN 0 ELSE 1 END,
                     COALESCE(ard.publish_date,a.publish_date,a.first_crawled,'') DESC,
                     a.id DESC
            LIMIT ?
        """
        params.append(max(1, min(int(limit or 20), 50)))
        with self.database.lock:
            return [dict(row) for row in self.database.connection.execute(sql, tuple(params)).fetchall()]

    def _policy_exact_evidence(self, plan: Mapping, *, industry_pack_id: str, limit: int = 6) -> tuple[list[dict], dict]:
        spec = _policy_anchor_spec(plan)
        audit = {
            "policy_exact_gate": "not_applicable" if not spec.get("is_policy") else "applied",
            "notices": spec.get("notices") or [],
            "issuers": spec.get("issuers") or [],
            "topics": spec.get("topics") or [],
            "candidates": 0,
            "adopted": 0,
        }
        if not spec.get("is_policy"):
            return [], audit
        ranked = []
        for row in self._policy_registry_rows(spec, pack_id=str(industry_pack_id), limit=30):
            score, reasons = _policy_match_score(row, spec)
            if score < 1050:
                continue
            ranked.append((score, int(row.get("id") or 0), row, reasons))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        audit["candidates"] = len(ranked)
        evidence = []
        seen = set()
        for score, _article_id, row, reasons in ranked:
            article_id = int(row.get("id") or 0)
            if not article_id or article_id in seen:
                continue
            seen.add(article_id)
            item = _article_evidence(
                row,
                score=score,
                method="policy_metadata_exact",
                reason="PG政策元数据优先命中：" + "；".join(reasons[:6]),
            )
            meta = dict(item.get("metadata") or {})
            if meta.get("doc_type") == "official_policy":
                meta["source_role"] = "official_original"
                meta["authority_flags"] = list(dict.fromkeys([*(meta.get("authority_flags") or []), "official_original", "policy_metadata_exact"]))
            elif meta.get("doc_type") == "official_interpretation":
                meta["source_role"] = "official_reference"
                meta["authority_flags"] = list(dict.fromkeys([*(meta.get("authority_flags") or []), "official_reference", "policy_metadata_exact"]))
            item["metadata"] = meta
            evidence.append(item)
            if len(evidence) >= max(1, min(int(limit or 6), 12)):
                break
        audit["adopted"] = len(evidence)
        audit["official_original_adopted"] = sum(
            1 for item in evidence if (item.get("metadata") or {}).get("source_role") == "official_original"
        )
        return evidence, audit

    def retrieve(
        self,
        plan: Mapping,
        *,
        industry_pack_id: str,
        page_context: Mapping | None = None,
        limit: int = 12,
    ) -> dict:
        rows, excluded = self._rows(str(industry_pack_id))
        by_id = {int(row["id"]): row for row in rows}
        page_context = dict(page_context or {})
        requested_ids = []
        for raw in [page_context.get("article_id"), *(page_context.get("article_ids") or [])]:
            try:
                article_id = int(raw)
            except (TypeError, ValueError):
                continue
            if article_id > 0 and article_id not in requested_ids:
                requested_ids.append(article_id)

        selected = []
        seen_articles = set()
        page_denied = []
        for article_id in requested_ids:
            row = by_id.get(article_id)
            if not row:
                page_denied.append({"article_id": article_id, "reason": "not_found_or_not_authorized"})
                continue
            selected.append(_article_evidence(row, score=1000, method="page_context", reason="用户当前页面或显式引用", source_type="page_context"))
            seen_articles.add(article_id)

        policy_exact, policy_exact_audit = self._policy_exact_evidence(plan, industry_pack_id=industry_pack_id, limit=6)
        for item in policy_exact:
            article_id = int(item.get("article_id") or 0)
            if article_id and article_id in seen_articles:
                continue
            selected.append(item)
            if article_id:
                seen_articles.add(article_id)

        queries = [str(item) for item in plan.get("queries") or [] if str(item).strip()]
        _qsrc = queries or [str(plan.get("question") or "")]

        def _expand(terms):
            """把检索词展开成多写法（简体/繁体/英文别名），只增不减。

            实例：港媒《工信部成立人形機器人與具身智能標準化技術委員會》是繁体，
            问题多用简体「人形机器人」，原先字面比对命中不了；英文问法（MIIT /
            humanoid robot）同样命中不了中文原文。这里在查询侧展开多写法，
            不去逐条归一 963 篇文章的正文（更便宜，也不动数据）。
            任何异常都退回原词表，行为与以前一致。
            """
            try:
                from qa_query_normalize import expand_terms

                return expand_terms(terms)
            except Exception:
                return list(terms)

        query_terms = _expand(_terms(_qsrc))
        anchor_terms = _expand(_semantic_anchor_terms(_qsrc))
        phrase_terms = _expand(_query_phrases(_qsrc))

        # 时间窗口：把「最近/最新/本周/本月/某年某月」落成明确区间。
        # 阶段 4 起：区间**参与硬过滤**（不再只是加权），并配分级扩窗梯子；
        # 关掉 QA_TIME_HARD_FILTER 时完全回到旧的"只加权不过滤"行为。
        try:
            from qa_query_normalize import parse_time_window

            _time_window = parse_time_window(" ".join(_qsrc))
        except Exception:
            _time_window = {"has_time": False, "days": None, "start": None, "end": None,
                            "label": "", "source": "none"}
        # 用户调整里的时间范围**优先**于问题文本解析结果（阶段 5：调整要真的改变检索）。
        _adjusted_window = _window_from_adjustment(plan)
        if _adjusted_window is not None:
            _time_window = _adjusted_window
        _hard_filter = _env_flag("QA_TIME_HARD_FILTER", True)
        _min_in_window = _env_int("QA_TIME_MIN_IN_WINDOW", 1, 1, 50)
        # 全文通道（阶段 5）：用户要"全文/逐段解释"时，把证据正文扩到库里存的原文全文，
        # 让生成端能逐段过；不新增网络依赖、不绕过证据闸门。
        _need_fulltext = bool(plan.get("must_fetch_fulltext"))
        _excerpt_chars = (
            _env_int("QA_FULLTEXT_EXCERPT_CHARS", 12000, 5000, 40000) if _need_fulltext else 5000
        )
        window_ids = set()

        def _in_window(row):
            """文章是否落在时间区间内；无法判断时返回 None（不参与硬过滤，也不加分）。"""
            if not _time_window.get("has_time") or _time_window.get("start") is None:
                return None
            bounds = _window_days(_time_window)
            day = _article_day(row)
            if bounds is None or day is None:
                return None
            return bounds[0] <= day <= bounds[1]
        amount_constraints = []
        for query in queries or [str(plan.get("question") or "")]:
            for item in _amount_constraints(query):
                key = (round(float(item.get("value_yi") or 0), 6), bool(item.get("approximate")))
                if not any((round(float(existing.get("value_yi") or 0), 6), bool(existing.get("approximate"))) == key for existing in amount_constraints):
                    amount_constraints.append(item)
        amount_context_terms = _amount_context_terms(queries or [str(plan.get("question") or "")])
        semantic_scores = {}
        if self.semantic_search and rows:
            try:
                raw_semantic = self.semantic_search(" ".join(queries), allowed_ids=set(by_id), limit=limit)
                for item in raw_semantic or []:
                    if isinstance(item, (tuple, list)) and len(item) >= 2:
                        semantic_scores[int(item[0])] = max(0.0, float(item[1]))
                    else:
                        semantic_scores[int(item)] = 0.1
            except Exception:
                excluded["semantic_error"] = excluded.get("semantic_error", 0) + 1

        ranked = []
        for row in rows:
            article_id = int(row["id"])
            if article_id in seen_articles:
                continue
            title = str(row.get("title") or "").casefold()
            keywords = " ".join(str(item) for item in _json(row.get("matched_keywords_json"), []))
            topics = " ".join(str(item) for item in _json(row.get("topic_tags_json"), []))
            body = str(row.get("content") or "")[:4000].casefold()
            searchable_blob = f"{title} {keywords.casefold()} {topics.casefold()} {body}"
            anchor_hits = [term for term in anchor_terms if term in searchable_blob]
            title_hits = [term for term in query_terms if term in title]
            keyword_hits = [term for term in query_terms if term in keywords.casefold() or term in topics.casefold()]
            body_hits = [term for term in query_terms if term in body]
            title_phrase_hits = [term for term in phrase_terms if len(term) >= 3 and term in title]
            body_phrase_hits = [term for term in phrase_terms if len(term) >= 3 and term in body]
            title_amount_hits = _amount_constraint_hits(amount_constraints, title, context_terms=amount_context_terms)
            body_amount_hits = _amount_constraint_hits(amount_constraints, body, context_terms=amount_context_terms)
            if amount_constraints and not title_amount_hits and not body_amount_hits:
                excluded["amount_miss"] = excluded.get("amount_miss", 0) + 1
                continue
            semantic = semantic_scores.get(article_id, 0.0)
            anchor_coverage = (len(anchor_hits) / max(1, len(anchor_terms))) if anchor_terms else 0.0
            score = (
                len(title_phrase_hits) * 32
                + len(title_amount_hits) * 36
                + len(title_hits) * 8
                + len(keyword_hits) * 5
                + min(5, len(body_phrase_hits)) * 10
                + min(4, len(body_amount_hits)) * 16
                + min(5, len(body_hits)) * 1.5
                + anchor_coverage * 12
                + semantic * 10
            )
            if score <= 0:
                continue
            in_window = _in_window(row)
            if in_window:
                score += 8          # 时效加权：区间内优先，但不排除区间外（配合扩窗）
                window_ids.add(article_id)
            reasons = []
            if title_phrase_hits:
                reasons.append("标题短语命中：" + "、".join(title_phrase_hits[:6]))
            if title_amount_hits:
                reasons.append("标题数值命中：" + "、".join(title_amount_hits[:4]))
            if title_hits:
                reasons.append("标题命中：" + "、".join(title_hits[:6]))
            if keyword_hits:
                reasons.append("分类词命中：" + "、".join(keyword_hits[:6]))
            if body_phrase_hits:
                reasons.append("正文短语命中：" + "、".join(body_phrase_hits[:6]))
            if body_amount_hits:
                reasons.append("正文数值命中：" + "、".join(body_amount_hits[:4]))
            if anchor_hits:
                reasons.append("核心词命中：" + "、".join(anchor_hits[:4]))
            if semantic:
                reasons.append(f"语义相似度 {semantic:.2f}")
            if in_window:
                reasons.append("时间在问题指定的范围内")
            ranked.append((score, str(row.get("publish_date") or row.get("first_crawled") or ""),
                           article_id, row, "；".join(reasons) or "正文相关",
                           _article_day(row)))
        ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        cap = max(1, min(int(limit or 12), 30))

        # ── 时间硬过滤 + 分级扩窗（阶段 4）──
        # ① 先用问题给的窗口硬过滤；② 窗口内不足 min_in_window 条时，按梯子
        #    （默认 3 个月 → 6 个月 → 1 年）逐级向前放宽；③ 时间未知的文章**不丢**，
        #    排在窗口内证据之后并在回执里计数——宁可少过滤，也不静默丢证据。
        gated, gate_receipt = apply_time_gate(
            ranked, _time_window, hard_filter=_hard_filter, min_in_window=_min_in_window
        )
        for score, _date, article_id, row, reason, _day in gated:
            if len(selected) >= cap:
                break
            selected.append(_article_evidence(
                row, score=score,
                method="hybrid" if article_id in semantic_scores else "keyword",
                reason=reason, excerpt_chars=_excerpt_chars))
            seen_articles.add(article_id)
        # 扩窗判定：问题带了时间、但区间内一条都没进证据 → 说明区间太窄，
        # 把"放宽到了哪一级"明确回报给上层，由答复或追问告知用户，
        # 而不是默默给一个过期答案。
        _tw_out = dict(_time_window)
        # 对外输出必须是 JSON 安全的：start/end 是 datetime，
        # 直接放进阶段结果/SSE 事件负载会让序列化失败（实测 level1_retrieval INTERNAL_ERROR）。
        for _key in ("start", "end"):
            _value = _tw_out.get(_key)
            if hasattr(_value, "isoformat"):
                _tw_out[_key] = _value.isoformat()
        _adopted_in_window = sum(
            1 for item in selected if int(item.get("article_id") or 0) in window_ids
        )
        _tw_out["in_window_adopted"] = _adopted_in_window
        _tw_out.update(gate_receipt)
        _tw_out["expanded"] = bool(_tw_out.get("has_time")) and (
            _adopted_in_window == 0 or gate_receipt["ladder_step"] > 0
        )
        if gate_receipt.get("note"):
            _tw_out["note"] = gate_receipt["note"]
        elif _tw_out["expanded"]:
            _tw_out["note"] = (
                "在 %s 内没有找到直接证据，已自动扩大到全部历史资料"
                % (_tw_out.get("label") or "指定时间范围")
            )
        return {
            "queries": queries,
            "evidence": selected,
            "excluded": {**excluded, "page_context": page_denied, "policy_exact": policy_exact_audit},
            "stats": {"eligible": len(rows), "adopted": len(selected), "keyword_candidates": len(ranked)},
            "time_window": _tw_out,
        }


class WebSearchService:
    """Normalize multiple providers without persisting their results."""

    def __init__(self, providers: Iterable[tuple[str, Callable]] | None = None):
        self.providers = list(providers or [])

    def search(self, queries: Iterable[str], *, enabled: bool, limit: int = 8) -> dict:
        if not enabled:
            return {"status": "disabled", "evidence": [], "providers": [], "errors": []}
        cap = max(1, min(int(limit or 8), 20))
        evidence, seen, provider_states, errors = [], set(), [], []
        for provider_name, provider in self.providers:
            count = 0
            try:
                for query in list(queries)[:3]:
                    for item in provider(query, cap) or []:
                        url = canonical_http_url(item.get("url") or item.get("href"))
                        if not url or url in seen:
                            continue
                        seen.add(url)
                        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
                        evidence.append({
                            "evidence_ref": f"web:{digest}", "source_type": "web",
                            "title": str(item.get("title") or url)[:1000], "source_url": url,
                            "content_excerpt": " ".join(str(item.get("snippet") or item.get("summary") or item.get("body") or "").split())[:3000],
                            "published_at": str(item.get("published_at") or item.get("date") or "") or None,
                            "fetched_at": utc_now_text(), "article_id": None, "ragflow_kb_id": None,
                            "document_id": None, "chunk_id": None, "score": float(item.get("score") or 0),
                            "authority_level": int(item.get("authority_level") or 1),
                            "retrieval_method": provider_name, "match_reason": f"联网搜索：{query[:120]}",
                            "relationship": "context", "metadata": {"provider": provider_name},
                        })
                        count += 1
                        if len(evidence) >= cap:
                            break
                    if len(evidence) >= cap:
                        break
                provider_states.append({"provider": provider_name, "status": "completed", "count": count})
            except Exception as exc:
                provider_states.append({"provider": provider_name, "status": "failed", "count": count})
                errors.append({"provider": provider_name, "code": "SEARCH_PROVIDER_FAILED", "message": "联网搜索供应商暂不可用"})
            if len(evidence) >= cap:
                break
        status = "completed" if evidence else ("failed" if errors else "empty")
        return {"status": status, "evidence": evidence, "providers": provider_states, "errors": errors}


def default_web_search_service() -> WebSearchService:
    providers = []
    try:
        import config
        if config.TAVILY_ENABLED and config.TAVILY_API_KEY:
            def tavily(query, limit):
                from tavily_client import TavilyClient
                return TavilyClient().search(query, max_results=limit)
            providers.append(("tavily", tavily))
        if config.SERPAPI_ENABLED and config.SERPAPI_API_KEY:
            def serpapi(query, limit):
                from serpapi_client import SerpAPIClient
                return SerpAPIClient().search(query, recency_days=0)[:limit]
            providers.append(("serpapi", serpapi))
    except Exception:
        pass
    return WebSearchService(providers)


__all__ = ["ArticleRetriever", "WebSearchService", "canonical_http_url", "default_web_search_service", "repair_mojibake"]
