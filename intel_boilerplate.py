#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""正文框架（boilerplate）判废：识别「整页都是模板框架」的伪文章。

为什么要它：2026-10-01 抓到的《2016亚太财富论坛暨国际私人/家族财富管理中国风云榜》，
正文 410 字全是分享按钮、评论框、"热文推荐"里别的文章标题、公众号简介
—— 一句正文都没有，却被当文章入库，还上传进了 RAGFlow 的 News 库污染检索。

当时为什么没拦住：入库前的 intel_admission 规则层只认「根路径导航页」和
「/services/ 服务页」，判不了正文框架；真正的判断压给了 LLM，LLM 看着标题
判成"文章"就放行了。这里补一层不依赖 LLM 的硬判据。

判据（保守，宁可放过也不误杀正常短稿）：
  * FRAME_MARKERS 命中 ≥2 个，且有效段落 < 门槛          → 判废
  * FRAME_MARKERS 命中 ≥1 个，且去框架后有效字数 < 60    → 判废
  * 一个框架词都没有，但有效字数 < 40（几乎没内容）      → 判废
其余一律放行。

有效段落 = 逐行切分后，长度达标、且不含框架特征词的行。

环境变量：
    INTEL_BOILERPLATE_ENABLED=0        关闭判废（默认开）
    INTEL_BOILERPLATE_MIN_PARAGRAPHS=2 有效段落数门槛
    INTEL_BOILERPLATE_MIN_CHARS=120    去框架后的有效字数门槛
"""
from __future__ import annotations

import os
import re

# 页面框架/交互/推荐位特征词。命中说明这段文字来自模板而不是正文。
FRAME_MARKERS = (
    "分享海报", "扫一扫", "扫二维码", "分享到", "分享：", "收藏", "点赞",
    "暂无评论", "提交评论", "发表评论", "我要评论", "全部评论", "评论区", "游客，您好", "为本文作者",
    "热文推荐", "热门推荐", "推荐阅读", "相关阅读", "相关文章", "猜你喜欢", "延伸阅读",
    "上一篇", "下一篇", "更多>", "更多 >", "阅读全文", "点击查看", "查看全部",
    "公众号", "关注我们", "订阅号", "转载", "版权声明", "版权所有", "免责声明", "免责条款",
    "打开微信", "分享按钮", "扫码关注", "点击下载", "下载APP", "客户端下载",
)

# 明显是纯导航/链接行（整行只有词和分隔符）
_NAV_ONLY_RE = re.compile(r"^[\s·|/、,，>»\-—_]+$")


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, "") or default))
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    return str(os.getenv("INTEL_BOILERPLATE_ENABLED", "1")).strip().casefold() not in {"0", "false", "no", "off"}


def min_paragraphs() -> int:
    return max(1, _env_int("INTEL_BOILERPLATE_MIN_PARAGRAPHS", 2))


def min_chars() -> int:
    return max(20, _env_int("INTEL_BOILERPLATE_MIN_CHARS", 120))


def frame_hits(text: str) -> list:
    """返回命中的框架特征词（去重、保持出现顺序）。"""
    blob = str(text or "")
    hits, seen = [], set()
    for marker in FRAME_MARKERS:
        if marker in blob and marker not in seen:
            seen.add(marker)
            hits.append(marker)
    return hits


def _lines(text: str) -> list:
    return [ln.strip() for ln in str(text or "").replace("\r", "\n").split("\n")]


def effective_paragraphs(text: str, *, min_chars: int = 40) -> list:
    """去掉框架行与过短行后，剩下的有效段落。"""
    out = []
    for line in _lines(text):
        if len(line) < min_chars:
            continue
        if _NAV_ONLY_RE.match(line):
            continue
        if any(marker in line for marker in FRAME_MARKERS):
            continue
        out.append(line)
    return out


def effective_char_count(text: str) -> int:
    """去框架后的有效字数（含短行正文，用于"是不是几乎没内容"的判断）。"""
    total = 0
    for line in _lines(text):
        if _NAV_ONLY_RE.match(line):
            continue
        if any(marker in line for marker in FRAME_MARKERS):
            continue
        total += len(line)
    return total


def assess(content: str, title: str = "") -> dict:
    """判定正文是否为页面框架。永远返回 dict，任何异常都按"不判废"处理。"""
    verdict = {
        "is_boilerplate": False,
        "reason": "",
        "frame_hits": [],
        "effective_paragraphs": 0,
        "effective_chars": 0,
    }
    try:
        text = str(content or "")
        if not text.strip():
            return verdict
        hits = frame_hits(text)
        paragraphs = effective_paragraphs(text, min_chars=40)
        chars = effective_char_count(text)
        verdict.update({
            "frame_hits": hits,
            "effective_paragraphs": len(paragraphs),
            "effective_chars": chars,
        })

        if not enabled():
            return verdict

        if len(hits) >= 3 and chars < 600:
            # 多个模板特征词同时出现，说明这段文字来自页面模板；有效内容又不多。
            # 实测：2614 那篇命中 12 个特征词、去框架后仅 243 字（页脚那句
            # "《财富管理》杂志是…专业读物"虽长但仍是模板文案，会被当成有效段落，
            # 所以这里不能只看有效段落数，要看特征词命中数）。
            verdict["is_boilerplate"] = True
            verdict["reason"] = (
                "正文命中 %d 个页面框架特征（%s），去框架后仅 %d 字"
                % (len(hits), "、".join(hits[:5]), chars)
            )
        elif len(hits) >= 2 and len(paragraphs) < min_paragraphs():
            verdict["is_boilerplate"] = True
            verdict["reason"] = (
                "正文命中 %d 个页面框架特征（%s），有效段落仅 %d 段"
                % (len(hits), "、".join(hits[:5]), len(paragraphs))
            )
        elif len(hits) >= 1 and chars < 60:
            verdict["is_boilerplate"] = True
            verdict["reason"] = (
                "正文命中页面框架特征（%s），去框架后仅 %d 字" % ("、".join(hits[:3]), chars)
            )
        elif not hits and chars < 40:
            verdict["is_boilerplate"] = True
            verdict["reason"] = "去框架后有效正文仅 %d 字，几乎无内容" % chars
    except Exception:
        return verdict
    return verdict


__all__ = ["FRAME_MARKERS", "assess", "effective_char_count", "effective_paragraphs",
           "enabled", "frame_hits", "min_chars", "min_paragraphs"]
