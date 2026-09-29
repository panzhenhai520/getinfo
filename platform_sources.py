#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""13 个外部信源(平台)的前置配置定义与读写。

平台名单与字段相对稳定；配置在系统设置里以「信源」方式平行列出，界面不出现
任何具体工具名称。每个信源用一个 【启用】+【凭据(secret)】 两组配置项表达，
凭据类型因平台而异（Cookie / Token / API Key / 订阅地址），此处仅以文本承载并做
秘密掩码判定。实际抓取由 agent_pipeline 按信源调用对应上游工具完成。
"""

from __future__ import annotations

from typing import Any


# 13 个信源：id（用于配置键）、label（前台显示中文）、category、credential_label（凭据名称）、hint
PLATFORM_SOURCES: list[dict[str, Any]] = [
    {"id": "xhs", "label": "小红书", "category": "内容社区", "credential_label": "Cookie", "hint": "Cookies 会话（Cookie-Editor 导出）"},
    {"id": "x", "label": "X / 推特", "category": "内容社区", "credential_label": "Token", "hint": "X 认证令牌/会话 Cookie"},
    {"id": "bilibili", "label": "B站", "category": "视频", "credential_label": "Cookie", "hint": "B站登录 Cookie"},
    {"id": "reddit", "label": "Reddit", "category": "内容社区", "credential_label": "Token", "hint": "Reddit 应用凭据/令牌"},
    {"id": "facebook", "label": "Facebook", "category": "内容社区", "credential_label": "Cookie", "hint": "Facebook 会话 Cookie"},
    {"id": "instagram", "label": "Instagram", "category": "内容社区", "credential_label": "Cookie", "hint": "Instagram 会话 Cookie"},
    {"id": "v2ex", "label": "V2EX", "category": "技术社区", "credential_label": "Token", "hint": "V2EX API Token"},
    {"id": "linkedin", "label": "领英", "category": "职场", "credential_label": "Cookie", "hint": "领英会话 Cookie / 需要登录态"},
    {"id": "youtube", "label": "YouTube", "category": "视频", "credential_label": "API Key", "hint": "Google API Key（可选）"},
    {"id": "github", "label": "GitHub", "category": "代码", "credential_label": "Token", "hint": "GitHub 个人访问令牌（代码搜索）"},
    {"id": "xiaoyuzhou", "label": "小宇宙播客", "category": "播客", "credential_label": "Cookie", "hint": "小宇宙登录 Cookie"},
    {"id": "xueqiu", "label": "雪球", "category": "行情", "credential_label": "Cookie", "hint": "雪球登录 Cookie"},
    {"id": "rss", "label": "RSS 订阅", "category": "订阅", "credential_label": "订阅地址", "hint": "逗号分隔的 RSS/Atom 订阅源地址（无需凭据）"},
]


def _env_key(platform_id: str, field: str) -> str:
    return f"PLATFORM_SOURCE_{platform_id.upper()}_{field}"


def to_env_keys(platform_id: str) -> list[str]:
    return [_env_key(platform_id, "ENABLED"), _env_key(platform_id, "AUTH")]


def public_sources(values: dict[str, str]) -> dict[str, Any]:
    """把env值转成后台展示用的信源配置（无 secret 明文，只给是否已配置）。"""
    result = {"sources": [], "configured_count": 0, "total": len(PLATFORM_SOURCES)}
    for p in PLATFORM_SOURCES:
        enabled = str(values.get(_env_key(p["id"], "ENABLED"), "")).strip().lower() in ("1", "true", "yes", "on")
        auth = str(values.get(_env_key(p["id"], "AUTH"), "") or "").strip()
        configured = bool(auth)
        if configured:
            result["configured_count"] += 1
        result["sources"].append({
            "id": p["id"], "label": p["label"], "category": p["category"],
            "credential_label": p["credential_label"], "hint": p["hint"],
            "enabled": enabled, "configured": configured,
            "auth_configured": configured,
        })
    return result


def parse_updates(data: dict[str, Any]) -> dict[str, str]:
    """从设置页提交的 platform_sources 数据生成 env 更新键值。"""
    updates: dict[str, str] = {}
    srcs = data.get("sources") if isinstance(data.get("sources"), list) else []
    if not srcs:
        return updates
    for item in srcs:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id") or "").strip()
        if not pid:
            continue
        # 仅在显式提供时写入（避免把未改动的 secret 清空）
        if "enabled" in item:
            updates[_env_key(pid, "ENABLED")] = "true" if item.get("enabled") else "false"
        if "auth" in item and item.get("auth"):
            updates[_env_key(pid, "AUTH")] = str(item["auth"]).strip()
        if item.get("clear_auth"):
            updates[_env_key(pid, "AUTH")] = ""
    return updates
