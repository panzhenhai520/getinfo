#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可配置的平台品牌设置：公司名称 + Logo。

公司名称以文本存于 platform_settings.json；Logo 上传后保存到 static/uploads 并记录访问 URL。
供 Flask context_processor 注入到所有模板，以替换【资讯情报系统】。
"""

from __future__ import annotations

import json
import os

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(_BASE_DIR, "platform_settings.json")
UPLOAD_DIR = os.path.join(_BASE_DIR, "static", "uploads")
DEFAULT_NAME = "资讯情报系统"
_ALLOWED_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")


def get_platform_settings() -> dict:
    """返回 {name, logo_url}；文件缺失/异常则回退默认。"""
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        name = str(data.get("name") or DEFAULT_NAME).strip() or DEFAULT_NAME
        logo_url = str(data.get("logo_url") or "").strip()
        return {"name": name, "logo_url": logo_url}
    except Exception:
        return {"name": DEFAULT_NAME, "logo_url": ""}


def save_platform_name(name: str) -> dict:
    """保存公司名称（覆盖旧名称的 logo_url 字段）。"""
    data = get_platform_settings()
    data["name"] = str(name or DEFAULT_NAME).strip() or DEFAULT_NAME
    _write(data)
    return data


def save_platform_logo(file_storage) -> dict:
    """保存上传的 Logo 文件，返回 {name, logo_url}。"""
    try:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        ext = ""
        if file_storage and file_storage.filename:
            ext = os.path.splitext(str(file_storage.filename))[1].lower()
        ext = ext if ext in _ALLOWED_EXT else ".png"
        filename = "platform_logo" + ext
        absolute = os.path.join(UPLOAD_DIR, filename)
        file_storage.save(absolute)
        data = get_platform_settings()
        data["logo_url"] = "/static/uploads/" + filename
        _write(data)
        return data
    except Exception:
        return get_platform_settings()


def _write(data: dict) -> None:
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass
