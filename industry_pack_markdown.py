#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解析「行业包配置 markdown」为可写入行业包草稿的字段片段。

支持的文档结构（与《AXLR · 投资管理行业包配置》一致）：

    ## 一、关键词门控
      ### 1.1 核心关键词（权重 3）      → core_keywords
      ### 1.2 扩展关键词（权重 1）      → expanded_keywords
      ### 1.3 趋势关键词（权重 2）      → trend_keywords
      ### 1.4 事件关键词（权重 2）      → event_keywords
      ### 1.5 行业锚点词（硬门槛）      → candidate_gate.anchor_keywords
      ### 1.6 备案机构（硬门槛）        → candidate_gate.anchor_keywords
      ### 1.7 备案机构（补充…）        → candidate_gate.anchor_keywords
      ### 1.8 重点品牌（不参与准入…）  → brands
      ### 1.9 趋势主题（每行一个…）    → trend_topics
      ### 1.10 搜索发现查询            → serpapi_queries
    ## 二、内部主题标签（表格）        → fixed_topics
    ## 三、来源管理（表格）            → default_sources

关键词/主题类段落支持 ``` 代码块或项目符号列表；表格段落按 markdown 表格解析。
解析器只做“尽力而为”的提取，无法识别的内容收集到 warnings，不抛异常。
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

_HEADING_RE = re.compile(r"^(#{2,6})\s+(.*)$")
_URL_RE = re.compile(r"https?://[^\s|<>）)】,，]+")
_MARK_MARKS = "✅⚠️❌✔✖️* "

# 来源角色（中文）→ 行业包 schema 的 source_role 枚举
_ROLE_MAP = {
    "监管机构": "government_regulator",
    "官方数据": "government_regulator",
    "交易所": "exchange_official",
    "自律组织": "industry_association",
    "行业协会": "industry_association",
    "学术研究": "academic_research",
    "科研机构": "academic_research",
    "行业数据": "independent_research",
    "独立研究": "independent_research",
    "企业官网": "issuer_official",
    "官方": "issuer_official",
    "行业媒体": "professional_trade_media",
    "创投媒体": "professional_trade_media",
    "专业媒体": "professional_trade_media",
    "综合财经": "professional_trade_media",
    "综合媒体": "consumer_media",
    "大众媒体": "consumer_media",
    "咨询顾问": "professional_advisor",
    "工程媒体": "engineering_media",
}
_TYPE_MAP = {
    "rss": "rss",
    "RSS": "rss",
    "网站": "website",
    "官网": "website",
    "列表页": "list_page",
    "list_page": "list_page",
}
_VALID_TYPES = {"rss", "website", "list_page"}
_VALID_ROLES = {
    "government_regulator", "official_investigation", "exchange_official",
    "academic_research", "independent_research", "industry_association",
    "issuer_official", "engineering_media", "professional_trade_media",
    "professional_advisor", "consumer_media", "unclassified",
}


def _split_sections(text: str) -> List[Tuple[int, str, List[str]]]:
    """把 markdown 切成 (级别, 标题, 正文行) 列表。"""
    sections: List[Tuple[int, str, List[str]]] = []
    cur = None
    for raw in str(text or "").splitlines():
        m = _HEADING_RE.match(raw.strip())
        if m:
            if cur:
                sections.append(cur)
            cur = (len(m.group(1)), m.group(2).strip(), [])
        elif cur is not None:
            cur[2].append(raw)
    if cur:
        sections.append(cur)
    return sections


def _code_blocks(lines: List[str]) -> List[str]:
    blocks, buf, inside = [], [], False
    for ln in lines:
        if ln.strip().startswith("```"):
            if inside:
                blocks.append("\n".join(buf))
                buf, inside = [], False
            else:
                inside = True
            continue
        if inside:
            buf.append(ln)
    if inside and buf:
        blocks.append("\n".join(buf))
    return blocks


def _list_items(lines: List[str]) -> List[str]:
    """取代码块 / 列表项 / 普通行中的条目（每行一个）。"""
    out: List[str] = []
    blocks = _code_blocks(lines)
    source = []
    if blocks:
        for b in blocks:
            source.extend(b.splitlines())
    else:
        source = list(lines)
    for ln in source:
        s = ln.strip()
        if not s or s.startswith("#") or s.startswith(">") or s.startswith("|"):
            continue
        s = re.sub(r"^[-*+]\s+", "", s)
        s = re.sub(r"^\d+[.)]\s+", "", s)
        if not s:
            continue
        out.append(s)
    return out


def _tables(lines: List[str]) -> List[List[List[str]]]:
    """返回若干 markdown 表格，每个表格是行列表，行是单元格列表（已跳过 `---` 分隔行）。"""
    tables: List[List[List[str]]] = []
    cur: List[List[str]] = []
    for ln in lines:
        s = ln.strip()
        if s.startswith("|") and s.endswith("|") and len(s) > 1:
            cells = [c.strip() for c in s.strip("|").split("|")]
            if cells and all(c and set(c) <= set("-: ") for c in cells):
                continue
            cur.append(cells)
        else:
            if cur:
                tables.append(cur)
                cur = []
    if cur:
        tables.append(cur)
    return tables


def _dedupe(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for raw in items:
        s = str(raw or "").strip()
        s = s.strip(_MARK_MARKS)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _clean_name(value: str) -> str:
    return str(value or "").strip().strip(_MARK_MARKS).strip()


def _extract_url(value: str) -> str:
    v = str(value or "").strip().strip("<>`")
    m = _URL_RE.search(v)
    return (m.group(0) if m else v).rstrip("/。,，)）")


def _to_int(value, default=None):
    m = re.search(r"-?\d+", str(value or ""))
    if not m:
        return default
    try:
        return int(m.group(0))
    except ValueError:
        return default


def _map_role(value: str) -> str:
    v = _clean_name(value)
    if not v:
        return "unclassified"
    if v in _VALID_ROLES:
        return v
    for key, mapped in _ROLE_MAP.items():
        if key in v:
            return mapped
    return "unclassified"


def _map_type(value: str) -> str:
    v = _clean_name(value)
    if not v:
        return "website"
    for key, mapped in _TYPE_MAP.items():
        if key.lower() == v.lower():
            return mapped
    return "website"


def _section_by_keywords(sections, *keywords):
    """按标题包含的关键词返回第一个匹配段落（优先 level 3）。"""
    hits = []
    for level, title, body in sections:
        if all(k in title for k in keywords):
            hits.append((level, title, body))
    hits.sort(key=lambda x: -x[0])
    return hits[0] if hits else None


def _section_with_children(sections, *keywords):
    """返回匹配段落的正文 + 其所有子段落正文（直到同级或更高级标题为止）。

    来源管理是 `## 三、来源管理` 加 `### 3.1/3.2/3.3` 子段落，表格都在子段落里，
    因此必须连同子段落一起收集。
    """
    for idx, (level, title, body) in enumerate(sections):
        if not all(k in title for k in keywords):
            continue
        lines = list(body)
        for sub_level, _sub_title, sub_body in sections[idx + 1:]:
            if sub_level <= level:
                break
            lines.extend(sub_body)
        return lines
    return None


def _section_defaults(lines: List[str]) -> Dict:
    """从段落正文里抓 `来源角色 = 企业官网` 这类默认值。"""
    text = "\n".join(lines)
    out = {}
    for label, key in (("来源角色", "source_role"), ("类型", "source_type"),
                       ("频率", "polling_interval_minutes"), ("可信度", "authority_level")):
        m = re.search(label + r"\s*[=＝:：]\s*`?\s*([^\s`，,、]+)", text)
        if m:
            out[key] = m.group(1).strip()
    return out


def parse_industry_pack_markdown(text: str) -> Dict:
    """解析行业包配置 markdown，返回 {fields..., warnings: [...]}。"""
    sections = _split_sections(text)
    warnings: List[str] = []
    result: Dict = {
        "core_keywords": [], "expanded_keywords": [], "trend_keywords": [],
        "event_keywords": [], "brands": [], "trend_topics": [],
        "serpapi_queries": [], "anchor_keywords": [], "fixed_topics": [],
        "default_sources": [],
    }

    keyword_map = (
        (("核心关键词",), "core_keywords"),
        (("扩展关键词",), "expanded_keywords"),
        (("趋势关键词",), "trend_keywords"),
        (("事件关键词",), "event_keywords"),
        (("行业锚点词",), "anchor_keywords"),
        (("备案机构",), "anchor_keywords"),
        (("重点品牌",), "brands"),
        (("趋势主题",), "trend_topics"),
        (("搜索发现查询",), "serpapi_queries"),
    )
    for keys, field in keyword_map:
        for level, title, body in sections:
            if all(k in title for k in keys):
                result[field].extend(_list_items(body))
    for field in ("core_keywords", "expanded_keywords", "trend_keywords", "event_keywords",
                  "brands", "trend_topics", "serpapi_queries", "anchor_keywords"):
        result[field] = _dedupe(result[field])

    # 二、内部主题标签（表格：key | 名称 | 关键词(/) | 搜索词1 | 搜索词2 | 搜索词3）
    topic_lines = _section_with_children(sections, "内部主题标签") or _section_with_children(sections, "主题标签")
    if topic_lines is not None:
        for table in _tables(topic_lines):
            for row in table:
                if len(row) < 3:
                    continue
                key = _clean_name(row[0])
                name = _clean_name(row[1])
                if not key or not name or key in ("主题 key", "主题key"):
                    continue
                kws = [k.strip() for k in re.split(r"[/、;；,，]", row[2] or "") if k.strip()]
                item = {"key": key, "name": name, "keywords": _dedupe(kws)}
                terms = _dedupe([c for c in row[3:6]])
                if terms:
                    item["search_terms"] = terms
                result["fixed_topics"].append(item)
    else:
        warnings.append("未找到「内部主题标签」表格")

    # 主题里写「上述 34 家主体简称（每行一个）」这类占位时，用重点品牌补齐
    _placeholder = re.compile(r"上述|每行一个|家主体|名单|简称")
    for topic in result["fixed_topics"]:
        kws = topic.get("keywords") or []
        if kws and result["brands"] and all(_placeholder.search(str(k)) for k in kws):
            topic["keywords"] = list(result["brands"])

    # 三、来源管理（表格：名称 | URL | 角色 | 类型 | 频率 | 可信度）
    src_lines = _section_with_children(sections, "来源管理")
    if src_lines is not None:
        for table in _tables(src_lines):
            for row in table:
                if len(row) < 2:
                    continue
                name = _clean_name(row[0])
                url = _extract_url(row[1])
                if not name or not url.startswith("http"):
                    continue
                if name in ("名称", "名称 "):
                    continue
                src = {
                    "name": name,
                    "url": url,
                    "source_type": _map_type(row[3]) if len(row) > 3 else "website",
                    "source_role": _map_role(row[2]) if len(row) > 2 else "unclassified",
                    "is_enabled": True,
                }
                freq = _to_int(row[4]) if len(row) > 4 else None
                if freq:
                    src["polling_interval_minutes"] = max(5, min(10080, freq))
                auth = _to_int(row[5]) if len(row) > 5 else None
                if auth:
                    src["authority_level"] = max(1, min(5, auth))
                result["default_sources"].append(src)
        # 只有「名称 | URL」两列的段落（如关注主体官网）：用段落里声明的默认值补齐
        defaults = _section_defaults(src_lines)
        for src in result["default_sources"]:
            if defaults.get("source_role") and src["source_role"] == "unclassified":
                src["source_role"] = _map_role(defaults["source_role"])
            if defaults.get("source_type") and "polling_interval_minutes" not in src:
                src["source_type"] = _map_type(defaults["source_type"])
            if defaults.get("polling_interval_minutes") and "polling_interval_minutes" not in src:
                v = _to_int(defaults["polling_interval_minutes"])
                if v:
                    src["polling_interval_minutes"] = max(5, min(10080, v))
            if defaults.get("authority_level") and "authority_level" not in src:
                v = _to_int(defaults["authority_level"])
                if v:
                    src["authority_level"] = max(1, min(5, v))
    else:
        warnings.append("未找到「来源管理」段落")

    # 去重来源（同 URL 只保留首个）
    seen_urls = set()
    sources = []
    for src in result["default_sources"]:
        key = src["url"].lower()
        if key in seen_urls:
            continue
        seen_urls.add(key)
        sources.append(src)
    result["default_sources"] = sources

    if not result["core_keywords"] and not result["default_sources"]:
        warnings.append("未解析出任何关键词或来源，请确认 markdown 结构")

    result["warnings"] = warnings
    result["counts"] = {
        "core_keywords": len(result["core_keywords"]),
        "expanded_keywords": len(result["expanded_keywords"]),
        "trend_keywords": len(result["trend_keywords"]),
        "event_keywords": len(result["event_keywords"]),
        "anchor_keywords": len(result["anchor_keywords"]),
        "brands": len(result["brands"]),
        "trend_topics": len(result["trend_topics"]),
        "serpapi_queries": len(result["serpapi_queries"]),
        "fixed_topics": len(result["fixed_topics"]),
        "default_sources": len(result["default_sources"]),
    }
    return result


def merge_into_pack(pack: Dict, parsed: Dict, *, replace_sources: bool = False) -> Dict:
    """把解析结果合并进行业包 manifest（就地修改并返回）。

    · 关键词/品牌/主题/锚点/搜索词：与已有值合并去重（保留原顺序）；
    · fixed_topics：按 key 合并（同 key 覆盖关键词与搜索词）；
    · default_sources：默认与已有来源合并去重；replace_sources=True 时整体替换。
    """
    pack = dict(pack or {})
    for field in ("core_keywords", "expanded_keywords", "trend_keywords", "event_keywords",
                  "brands", "trend_topics", "serpapi_queries"):
        pack[field] = _dedupe(list(pack.get(field) or []) + list(parsed.get(field) or []))

    gate = dict(pack.get("candidate_gate") or {})
    gate["anchor_keywords"] = _dedupe(
        list(gate.get("anchor_keywords") or []) + list(parsed.get("anchor_keywords") or [])
    )
    pack["candidate_gate"] = gate

    existing_topics = {str(t.get("key")): dict(t) for t in (pack.get("fixed_topics") or [])}
    order = [str(t.get("key")) for t in (pack.get("fixed_topics") or [])]
    for topic in parsed.get("fixed_topics") or []:
        key = str(topic.get("key"))
        if key in existing_topics:
            merged = existing_topics[key]
            merged["name"] = topic.get("name") or merged.get("name")
            merged["keywords"] = _dedupe(list(merged.get("keywords") or []) + list(topic.get("keywords") or []))
            if topic.get("search_terms"):
                merged["search_terms"] = _dedupe(list(merged.get("search_terms") or []) + list(topic["search_terms"]))
        else:
            existing_topics[key] = dict(topic)
            order.append(key)
    pack["fixed_topics"] = [existing_topics[k] for k in order if k in existing_topics]

    if replace_sources:
        pack["default_sources"] = list(parsed.get("default_sources") or [])
    else:
        merged_sources = list(pack.get("default_sources") or [])
        seen = {str(s.get("url") or "").lower() for s in merged_sources}
        for src in parsed.get("default_sources") or []:
            key = str(src.get("url") or "").lower()
            if key and key not in seen:
                seen.add(key)
                merged_sources.append(src)
        pack["default_sources"] = merged_sources
    return pack
