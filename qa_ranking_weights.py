#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 10 · 多目标排序权重表（时效 / 权威性 / 覆盖面）从硬编码提成可配。

**等价替换优先**：`balanced` 配置的每个数字都与原来写死在 `qa_retrieval` 里的系数
**逐项相等**，所以切换默认配置时检索结果逐条一致；调优配置（freshness / authority /
coverage）才改变权重。这样"提成配置"这一步本身不带行为变化，出问题容易定位。

用法：
    QA_RANKING_PROFILE=freshness      # 环境变量选档
    ranking_weights(profile="authority")   # 代码内指定
"""
from __future__ import annotations

import os
from typing import Dict

# 与原 qa_retrieval 硬编码系数逐项相等（等价替换）
_BALANCED = {
    "title_phrase": 32.0,      # 标题短语命中
    "title_amount": 36.0,      # 标题数值命中
    "title": 8.0,              # 标题词命中
    "keyword": 5.0,            # 分类词命中
    "body_phrase": 10.0,       # 正文短语命中（每条，封顶 5 条）
    "body_amount": 16.0,       # 正文数值命中（每条，封顶 4 条）
    "body": 1.5,               # 正文词命中（每条，封顶 5 条）
    "anchor_coverage": 12.0,   # 核心词覆盖率（覆盖面）
    "semantic": 10.0,          # 语义相似度
    "in_window_bonus": 8.0,    # 时间落在问题区间内的加分（时效）
    # 三个多目标乘子：1.0 = 与历史行为一致
    "freshness": 1.0,          # 时效
    "authority": 1.0,          # 权威性（用于政策路径的 authority_level 加分）
    "coverage": 1.0,           # 覆盖面（用于核心词覆盖率）
}

PROFILES: Dict[str, Dict[str, float]] = {
    # 默认：与历史逐项相等
    "balanced": dict(_BALANCED),
    # 时效优先：问"最新/最近动态"时，让落在时间窗内的证据更靠前
    "freshness": {**_BALANCED, "in_window_bonus": 16.0, "freshness": 1.5,
                  "authority": 0.8, "coverage": 0.9},
    # 权威性优先：政策/合规类问题，官方原文与高权威来源更靠前
    "authority": {**_BALANCED, "authority": 1.6, "freshness": 0.6, "coverage": 1.1},
    # 覆盖面优先：行业扫描类问题，覆盖更多核心词的证据更靠前
    "coverage": {**_BALANCED, "coverage": 1.5, "anchor_coverage": 16.0,
                 "title_phrase": 38.0, "freshness": 0.9},
}

DEFAULT_PROFILE = "balanced"


def ranking_profile_name(profile: str = "") -> str:
    """解析档位名：显式参数 > 环境变量 > 默认；非法值退回默认。"""
    name = str(profile or os.getenv("QA_RANKING_PROFILE", "") or DEFAULT_PROFILE).strip().lower()
    return name if name in PROFILES else DEFAULT_PROFILE


def ranking_weights(profile: str = "") -> Dict[str, float]:
    """返回该档位的权重（含逐项覆盖环境变量的能力）。

    环境变量覆盖：`QA_RANKING_WEIGHT_<键大写>`，例如 `QA_RANKING_WEIGHT_IN_WINDOW_BONUS=20`。
    """
    weights = dict(PROFILES[ranking_profile_name(profile)])
    for key in list(weights):
        raw = os.getenv("QA_RANKING_WEIGHT_%s" % key.upper())
        if raw is None or str(raw).strip() == "":
            continue
        try:
            weights[key] = float(str(raw).strip())
        except (TypeError, ValueError):
            continue
    return weights


def describe(profile: str = "") -> str:
    name = ranking_profile_name(profile)
    weights = PROFILES[name]
    return ("排序档位=%s（时效 ×%.1f / 权威性 ×%.1f / 覆盖面 ×%.1f，窗内加分 %.0f）"
            % (name, weights["freshness"], weights["authority"],
               weights["coverage"], weights["in_window_bonus"]))


__all__ = ["PROFILES", "DEFAULT_PROFILE", "ranking_profile_name", "ranking_weights", "describe"]
