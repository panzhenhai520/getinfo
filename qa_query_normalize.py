#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检索侧的多语言/多写法归一与时间窗口解析（问答检索前置处理）。

为什么需要：

1) 繁简与异体字
   实例：港媒文章《工信部成立人形機器人與具身智能標準化技術委員會》（繁体），
   问题写的是「人形机器人」（简体）。检索侧原先只有字面 in 判断，
   機器人 ≠ 机器人 → 这篇文章在检索眼里等于不存在。检索侧此前完全没有归一。

2) 中英混问
   同一个实体在库里有中文与英文两种写法（工信部 / MIIT / Ministry of Industry
   and Information Technology；人形机器人 / humanoid robot）。只归一繁简不够，
   还要按实体别名把查询词展开成多语言形式。

3) 时间
   「最近/最新/本周/近三个月」原先完全没进检索条件，2026-01-21 的旧文和
   2026-10-04 的新文在同一次打分里裸拼。这里把时间词落成明确窗口，
   窗口要能回写给用户看（"最近"= 近 3 个月，可配）。

设计原则：查询侧展开成"多个候选写法"，任一命中即命中；不改动库里的原文
（避免动数据），也不做单向翻译（繁体原文译成英文会丢掉中文匹配能力）。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- 繁简归一
try:  # opencc 已随项目安装（前端也用它做繁简转换）
    from opencc import OpenCC as _OpenCC

    _T2S = _OpenCC("t2s")
except Exception:  # pragma: no cover - 环境缺库时退回内置映射
    _T2S = None

# 内置兜底：只覆盖高频异体/繁体字，够在没有 opencc 的环境里救急
_FALLBACK_T2S = str.maketrans({
    "機": "机", "器": "器", "與": "与", "體": "体", "標": "标", "準": "准",
    "術": "术", "委": "委", "員": "员", "會": "会", "動": "动", "態": "态",
    "業": "业", "務": "务", "資": "资", "訊": "讯", "網": "网", "絡": "络",
    "電": "电", "產": "产", "專": "专", "題": "题", "報": "报", "導": "导",
    "場": "场", "點": "点", "線": "线", "車": "车", "國": "国", "內": "内",
    "經": "经", "濟": "济", "財": "财", "富": "富", "管": "管", "理": "理",
    "貿": "贸", "易": "易", "銀": "银", "行": "行", "債": "债", "券": "券",
    "價": "价", "值": "值", "數": "数", "據": "据", "軟": "软", "硬": "硬",
    "設": "设", "開": "开", "發": "发", "進": "进", "運": "运", "營": "营",
    "銷": "销", "購": "购", "買": "买", "賣": "卖", "製": "制", "造": "造",
    "環": "环", "境": "境", "標": "标", "準": "准", "監": "监", "測": "测",
    "疫": "疫", "醫": "医", "藥": "药", "療": "疗", "護": "护", "養": "养",
    "學": "学", "校": "校", "課": "课", "程": "程", "師": "师", "學": "学",
})

# ---------------------------------------------------------------- 实体别名
# 同一实体的中/英/别名写法。命中任一写法即把整组写法都作为检索词。
# 后续可从行业包 manifest 的 core/expanded keywords 自动补充，这里先放高频实体。
ENTITY_GROUPS = (
    ("工信部", "工业和信息化部", "MIIT", "Ministry of Industry and Information Technology"),
    ("人形机器人", "人形機器人", "humanoid robot", "humanoid robots", "humanoid"),
    ("具身智能", "embodied intelligence", "embodied AI", "embodied ai"),
    ("发改委", "国家发展改革委", "NDRC", "National Development and Reform Commission"),
    ("科技部", "Ministry of Science and Technology", "MOST"),
    ("证监会", "中国证监会", "CSRC", "China Securities Regulatory Commission"),
    ("央行", "中国人民银行", "PBOC", "People's Bank of China"),
    ("自动驾驶", "智能驾驶", "autonomous driving", "self-driving"),
    ("新能源汽车", "电动车", "EV", "electric vehicle", "new energy vehicle"),
    ("家族办公室", "家办", "family office", "family offices"),
    ("半导体", "芯片", "semiconductor", "chip", "chips"),
    ("大模型", "large language model", "LLM", "LLMs"),
)

DEFAULT_WINDOW_DAYS = 90          # 「最近」默认按近 3 个月
DEFAULT_WINDOW_DAYS_ENV = "QA_TIME_WINDOW_DEFAULT_DAYS"
RECENT_MARKERS = ("最近", "近期", "最新", "近日", "这两天", "这几天", "近来", "recent", "recently", "latest")


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, "") or default))
    except (TypeError, ValueError):
        return default


def default_window_days() -> int:
    return max(1, _env_int(DEFAULT_WINDOW_DAYS_ENV, DEFAULT_WINDOW_DAYS))


def to_simplified(text: str) -> str:
    """繁体 → 简体。缺 opencc 时用内置映射兜底。"""
    value = str(text or "")
    if not value:
        return value
    if _T2S is not None:
        try:
            return _T2S.convert(value)
        except Exception:
            pass
    return value.translate(_FALLBACK_T2S)


def normalize_text(text: str) -> str:
    """全角→半角、繁→简、空白归一。检索比对前统一走这里。"""
    value = str(text or "")
    value = "".join(chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch for ch in value)
    value = to_simplified(value)
    return re.sub(r"\s+", " ", value).strip()


def expand_entity_terms(text: str) -> list:
    """按实体别名把文本展开成多语言/多写法候选词（含简体形）。"""
    normalized = normalize_text(text).casefold()
    out, seen = [], set()
    for group in ENTITY_GROUPS:
        forms = [normalize_text(item) for item in group]
        if any(form.casefold() in normalized for form in forms):
            for form in forms:
                key = form.casefold()
                if key and key not in seen:
                    seen.add(key)
                    out.append(form)
    return out


def expand_query(question: str) -> dict:
    """把问题展开成检索用的多写法词表。返回 dict，供检索侧拼接比对。"""
    normalized = normalize_text(question)
    entities = expand_entity_terms(question)
    return {
        "question": str(question or ""),
        "normalized": normalized,
        "simplified": to_simplified(str(question or "")),
        "entity_terms": entities,
        # 检索时用：原文 + 归一 + 别名写法全部参与匹配，任一命中即命中
        "search_texts": list(dict.fromkeys([str(question or ""), normalized] + entities)),
    }


# ---------------------------------------------------------------- 时间窗口
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}
_REL_DAYS = re.compile(r"(近|最近|过去|前)\s*([0-9]+|[一二两三四五六七八九十]+)\s*(天|日|周|个?月|年)")
_ABS_MONTH = re.compile(r"(20[0-9]{2})\s*年\s*([0-9]{1,2})\s*月")
_ABS_YEAR = re.compile(r"(20[0-9]{2})\s*年")


def _num(raw: str) -> int:
    raw = str(raw or "").strip()
    if raw.isdigit():
        return int(raw)
    return _CN_NUM.get(raw, 0)


def parse_time_window(question: str, now: datetime | None = None) -> dict:
    """从问题里解析时间意图，返回明确窗口。

    返回 {has_time, days, start, end, label, source}
      has_time=False 时 days 为 None（调用方自己决定要不要兜底成默认窗口）
      label 是给用户看的说明，例如「最近 = 近 3 个月（2026-07-08 起）」
    """
    now = now or datetime.now(timezone.utc)
    text = normalize_text(question)
    lower = text.casefold()

    m = _REL_DAYS.search(text)
    if m:
        n = _num(m.group(2)) or 1
        unit = m.group(3)
        days = {"天": n, "日": n, "周": n * 7, "月": n * 30, "个月": n * 30, "年": n * 365}.get(unit, n)
        start = now - timedelta(days=days)
        return {"has_time": True, "days": days, "start": start, "end": now,
                "label": "近 %d 天（%s 起）" % (days, start.strftime("%Y-%m-%d")), "source": "relative"}

    if any(marker in lower for marker in ("本周", "这周", "this week")):
        start = now - timedelta(days=now.weekday())
        return {"has_time": True, "days": 7, "start": start, "end": now,
                "label": "本周（%s 起）" % start.strftime("%Y-%m-%d"), "source": "week"}
    if any(marker in lower for marker in ("本月", "这个月", "this month")):
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return {"has_time": True, "days": (now - start).days or 1, "start": start, "end": now,
                "label": "本月（%s 起）" % start.strftime("%Y-%m-%d"), "source": "month"}
    if any(marker in lower for marker in ("今年", "本年度", "this year")):
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return {"has_time": True, "days": (now - start).days or 1, "start": start, "end": now,
                "label": "今年（%s 起）" % start.strftime("%Y-%m-%d"), "source": "year"}
    if any(marker in lower for marker in ("去年", "上年", "last year")):
        start = now.replace(year=now.year - 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return {"has_time": True, "days": (end - start).days, "start": start, "end": end,
                "label": "去年（%s ~ %s）" % (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")),
                "source": "last_year"}

    m = _ABS_MONTH.search(text)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12:
            start = datetime(year, month, 1, tzinfo=now.tzinfo)
            end = (start + timedelta(days=32)).replace(day=1)
            return {"has_time": True, "days": (end - start).days, "start": start, "end": end,
                    "label": "%d 年 %d 月" % (year, month), "source": "absolute_month"}

    m = _ABS_YEAR.search(text)
    if m:
        year = int(m.group(1))
        start = datetime(year, 1, 1, tzinfo=now.tzinfo)
        end = datetime(year + 1, 1, 1, tzinfo=now.tzinfo)
        return {"has_time": True, "days": 365, "start": start, "end": end,
                "label": "%d 年" % year, "source": "absolute_year"}

    if any(marker in lower for marker in RECENT_MARKERS):
        days = default_window_days()
        start = now - timedelta(days=days)
        return {"has_time": True, "days": days, "start": start, "end": now,
                "label": "最近 = 近 %d 天（%s 起）" % (days, start.strftime("%Y-%m-%d")), "source": "default_recent"}

    return {"has_time": False, "days": None, "start": None, "end": now, "label": "", "source": "none"}


__all__ = ["ENTITY_GROUPS", "default_window_days", "expand_entity_terms", "expand_query",
           "normalize_text", "parse_time_window", "to_simplified"]
