# -*- coding: utf-8 -*-

"""反爬厂商识别器（自研轻量规则引擎）。

设计口径（与 crawl_policy.py 的「反爬处理宪法」一致）：
  · 本模块只做三件事：**识别是谁拦了我们**、**给出处置建议**、**累计统计并止损**；
  · 绝不做绕过：不破解验证码、不做指纹伪装、不轮换代理；
  · 所有判据都是公开事实（Cloudflare 会下发 cf-ray、Akamai 会下发 _abck 这类），
    事实本身不涉及任何第三方代码或规则的复制。

规则来源：config/antibot_rules.json（可随时增删改，无需改代码）。
方法分类沿用业界通行口径：cookie / header / url / content / dom / window / payload。

对外主入口：
  detect_antibot(...)       -> protection dict | None
  preferred_engine(...)     -> 'curl_cffi' | 'browser' | 'manual'
  record_source_block(...)  -> 信源级拦截统计 + 放弃策略（写 intel_sources.metadata_json）
  blocked_vendor_ranking()  -> 看板用：被拦厂商 Top
  source_health_report()    -> 看板用：信源健康度
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parent
DEFAULT_RULES_PATH = ROOT / "config" / "antibot_rules.json"
SCHEMA_VERSION = "antibot-rules-v1"

# 允许的规则方法（与业界分类一致；我们额外支持 payload 供未来主动探测使用）
ALLOWED_METHODS = ("cookie", "header", "url", "content", "dom", "window", "payload")

# 反爬常见状态码（与 crawl_policy.ANTI_BOT_STATUS_CODES 同口径，另加 202/405/521：
# 瑞数、加速乐、阿里云 WAF 常用 202/521 返回挑战页）
BLOCK_STATUS_CODES = frozenset({202, 403, 405, 406, 412, 429, 503, 521})

# 单条规则达到该置信度即可直接判定「被拦截」
_BLOCK_CONFIDENCE = 0.6
# 置信度低于该值且非 weak 的规则不参与厂商判定（纯噪声）
_MIN_CONFIDENCE = 0.2
# 每个厂商最多保留的证据条数
_MAX_EVIDENCE = 6
# 正文参与匹配的最大长度（避免超长页面拖慢正则）
_MAX_BODY_CHARS = 400_000
# 证据片段最大长度（只存片段，绝不落地整页内容）
_EVIDENCE_SNIPPET = 120

# 命中即放弃的默认阈值（按厂商类型区分难度）：
#   验证码型永远过不去 → 2 次；JS 传感器型必须真浏览器 → 3 次；其余给更多机会。
_KIND_GIVE_UP_DEFAULT = {"captcha": 2, "js_sensor": 3, "waf": 5, "fingerprint": 8}

# metadata_json 中使用的键（前缀化，避免和信源其它字段冲突）
META_VENDORS = "antibot_vendors"
META_TOTAL = "antibot_block_count"
META_LAST_VENDOR = "antibot_last_vendor"
META_LAST_LABEL = "antibot_last_vendor_label"
META_LAST_AT = "antibot_last_block_at"
META_STATUS = "antibot_status"
META_REASON = "antibot_reason"
META_NEEDS_PROXY = "antibot_needs_proxy"
META_NEEDS_MANUAL = "antibot_needs_manual"

_RULES_CACHE: Dict[str, dict] = {}
_COMPILED_CACHE: Dict[str, dict] = {}


class AntibotRulesError(ValueError):
    """规则文件不可用。"""


# ────────────────────────────── 规则装载 ──────────────────────────────

def load_antibot_rules(path: str | Path | None = None) -> dict:
    """装载并校验规则文件（带进程内缓存）。"""
    key = str(path or DEFAULT_RULES_PATH)
    cached = _RULES_CACHE.get(key)
    if cached is not None:
        return cached
    raw = json.loads(Path(key).read_text(encoding="utf-8"))
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise AntibotRulesError(
            f"antibot rules version mismatch: {raw.get('schema_version')!r}"
        )
    vendors = raw.get("vendors")
    if not isinstance(vendors, Mapping) or not vendors:
        raise AntibotRulesError("antibot rules has no vendors")
    normalized: Dict[str, dict] = {}
    for vendor_id, spec in vendors.items():
        vendor = dict(spec or {})
        rules = vendor.get("rules")
        if not isinstance(rules, list) or not rules:
            raise AntibotRulesError(f"vendor {vendor_id} has no rules")
        vendor["rules"] = [_normalize_rule(vendor_id, rule) for rule in rules]
        vendor["block_status"] = frozenset(
            int(value) for value in (vendor.get("block_status") or ())
        )
        normalized[str(vendor_id)] = vendor
    _RULES_CACHE[key] = {**raw, "vendors": normalized}
    return _RULES_CACHE[key]


def _normalize_rule(vendor_id: str, rule: Mapping) -> dict:
    method = str(rule.get("method") or "").casefold()
    if method not in ALLOWED_METHODS:
        raise AntibotRulesError(f"{vendor_id}: unsupported method {method!r}")
    pattern = str(rule.get("pattern") or "")
    if not pattern:
        raise AntibotRulesError(f"{vendor_id}: rule {rule.get('id')} has no pattern")
    try:
        # MULTILINE：header 信号是多行 "key: value"，规则用 ^key: 逐行锚定
        compiled = re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        raise AntibotRulesError(
            f"{vendor_id}: rule {rule.get('id')} bad pattern: {exc}"
        ) from exc
    value_pattern = str(rule.get("value_pattern") or "")
    compiled_value = None
    if value_pattern:
        try:
            compiled_value = re.compile(value_pattern, re.MULTILINE)
        except re.error as exc:
            raise AntibotRulesError(
                f"{vendor_id}: rule {rule.get('id')} bad value_pattern: {exc}"
            ) from exc
    return {
        "id": str(rule.get("id") or f"{vendor_id}.{method}"),
        "method": method,
        "confidence": float(rule.get("confidence") or 0.0),
        "weak": bool(rule.get("weak")),
        "note": str(rule.get("note") or ""),
        "compiled": compiled,
        "compiled_value": compiled_value,
    }


def reload_antibot_rules(path: str | Path | None = None) -> dict:
    """清缓存后重新装载（规则文件被编辑后调用）。"""
    _RULES_CACHE.pop(str(path or DEFAULT_RULES_PATH), None)
    return load_antibot_rules(path)


# ────────────────────────────── 信号构造 ──────────────────────────────

def _headers_signal(headers) -> str:
    if not headers:
        return ""
    if isinstance(headers, Mapping):
        items = headers.items()
    else:
        try:
            items = dict(headers).items()
        except (TypeError, ValueError):
            return ""
    return "\n".join(f"{key}: {value}" for key, value in items)


def _cookies_signal(cookies, headers) -> str:
    """cookies 可显式传入；否则从 Set-Cookie 响应头里抽（cookie 名足够做判据）。"""
    if cookies:
        if isinstance(cookies, Mapping):
            return "; ".join(f"{key}={value}" for key, value in cookies.items())
        return str(cookies)
    if not headers:
        return ""
    try:
        mapping = dict(headers)
    except (TypeError, ValueError):
        return ""
    values: List[str] = []
    for key, value in mapping.items():
        if str(key).casefold() in {"set-cookie", "cookie"}:
            values.append(str(value))
    return " ; ".join(values)


def _window_signal(window_globals) -> str:
    if not window_globals:
        return ""
    if isinstance(window_globals, Mapping):
        return " ".join(str(key) for key in window_globals)
    if isinstance(window_globals, (str, bytes)):
        return str(window_globals)
    try:
        return " ".join(str(item) for item in window_globals)
    except TypeError:
        return str(window_globals)


def build_signals(
    *,
    headers=None,
    cookies=None,
    body: str = "",
    url: str = "",
    dom: str = "",
    window_globals=None,
    payload: str = "",
) -> Dict[str, str]:
    """把一次抓取的原始素材整理成 7 类可匹配信号。"""
    text = body if isinstance(body, str) else ""
    if isinstance(body, (bytes, bytearray)):
        text = bytes(body).decode("utf-8", errors="replace")
    if len(text) > _MAX_BODY_CHARS:
        text = text[:_MAX_BODY_CHARS]
    dom_text = dom if isinstance(dom, str) else ""
    if not dom_text:
        dom_text = text
    return {
        "cookie": _cookies_signal(cookies, headers),
        "header": _headers_signal(headers),
        "url": str(url or ""),
        "content": text,
        "dom": dom_text,
        "window": _window_signal(window_globals),
        "payload": str(payload or ""),
    }


# ────────────────────────────── 匹配与裁决 ──────────────────────────────

def _matched_rules(vendor: Mapping, signals: Mapping[str, str]) -> List[dict]:
    hits: List[dict] = []
    for rule in vendor["rules"]:
        text = signals.get(rule["method"]) or ""
        if not text:
            continue
        if rule["compiled"].search(text) is None:
            continue
        if rule["compiled_value"] is not None and rule["compiled_value"].search(text) is None:
            continue
        if rule["confidence"] < _MIN_CONFIDENCE:
            continue
        hits.append(rule)
    return hits


def _decision_settings(settings=None) -> Mapping:
    if settings is not None:
        return settings
    try:
        import config as _config
    except Exception:  # pragma: no cover - 极端环境无 config
        return {}
    return _config


def detect_antibot(
    *,
    status_code: Optional[int] = None,
    headers=None,
    cookies=None,
    body: str = "",
    url: str = "",
    dom: str = "",
    window_globals=None,
    payload: str = "",
    settings=None,
    rules: Optional[dict] = None,
    min_confidence: float = _BLOCK_CONFIDENCE,
) -> Optional[Dict]:
    """识别本次响应的拦截厂商。

    返回 None 表示没有任何厂商痕迹；否则返回：
      {vendor, label, kind, confidence, method, is_block, evidence,
       preferred_engine, fallback_engine, action, status_code, url, reason}
    """
    try:
        settings = _decision_settings(settings)
        if not bool(getattr(settings, "ANTIBOT_DETECTOR_ENABLED", True)):
            return None
        catalog = rules or load_antibot_rules()
        signals = build_signals(
            headers=headers,
            cookies=cookies,
            body=body,
            url=url,
            dom=dom,
            window_globals=window_globals,
            payload=payload,
        )
        status = int(status_code or 0)
        status_block = status in BLOCK_STATUS_CODES
        candidates: List[dict] = []
        for vendor_id, vendor in catalog["vendors"].items():
            hits = _matched_rules(vendor, signals)
            if not hits:
                continue
            strongest = max(hits, key=lambda rule: rule["confidence"])
            strong_signal = any(
                rule["confidence"] >= min_confidence and not rule["weak"] for rule in hits
            )
            vendor_status_block = status in (vendor.get("block_status") or ())
            is_block = bool(strong_signal or (status_block and vendor_status_block) or (strong_signal and status_block))
            candidates.append(
                {
                    "vendor": vendor_id,
                    "label": str(vendor.get("label") or vendor_id),
                    "kind": str(vendor.get("kind") or "unknown"),
                    "confidence": round(float(strongest["confidence"]), 3),
                    "method": strongest["method"],
                    "is_block": is_block,
                    "evidence": [
                        {
                            "id": rule["id"],
                            "method": rule["method"],
                            "confidence": round(float(rule["confidence"]), 3),
                            "weak": bool(rule["weak"]),
                            "note": rule["note"],
                            "snippet": _snippet(signals.get(rule["method"]) or "", rule),
                        }
                        for rule in sorted(hits, key=lambda r: -r["confidence"])[:_MAX_EVIDENCE]
                    ],
                    "preferred_engine": str(vendor.get("preferred_engine") or "browser"),
                    "fallback_engine": str(vendor.get("fallback_engine") or "browser"),
                    "action": str(vendor.get("action") or "retry_browser"),
                    "status_code": status,
                    "url": str(url or ""),
                }
            )
        if not candidates:
            return None
        # 优先取「判定为拦截」的厂商；同为拦截时取置信度更高者；
        # 全是弱信号时也返回（供统计用），但 is_block=False。
        candidates.sort(key=lambda item: (item["is_block"], item["confidence"]), reverse=True)
        best = candidates[0]
        best["reason"] = protection_summary(best)
        best["other_vendors"] = [item["vendor"] for item in candidates[1:]]
        return best
    except Exception as exc:  # 识别器绝不影响主链路
        print(f"[antibot] 识别异常（已忽略）: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
        return None


def _snippet(text: str, rule: Mapping) -> str:
    """只保留命中位置附近的一小段，避免把整页内容带进日志/数据库。"""
    try:
        match = rule["compiled"].search(text)
        if match is None:
            return ""
        start = max(0, match.start() - 20)
        raw = text[start: match.end() + _EVIDENCE_SNIPPET]
        return " ".join(raw.split())[:_EVIDENCE_SNIPPET]
    except Exception:
        return ""


def protection_summary(protection: Optional[Mapping]) -> str:
    """一行人类可读摘要，供日志与信源管理页展示。"""
    if not protection:
        return ""
    evidence = protection.get("evidence") or []
    first = evidence[0] if evidence else {}
    detail = f"{first.get('method', '')}:{first.get('id', '')}" if first else ""
    kind_label = {
        "fingerprint": "指纹型",
        "js_sensor": "JS传感器型",
        "captcha": "验证码型",
        "waf": "规则型WAF",
    }.get(str(protection.get("kind") or ""), str(protection.get("kind") or ""))
    verdict = "已拦截" if protection.get("is_block") else "仅检测到痕迹"
    return (
        f"{protection.get('label') or protection.get('vendor')}"
        f"（{kind_label}，置信度{protection.get('confidence')}，{verdict}"
        + (f"，证据 {detail}" if detail else "")
        + "）"
    )


def preferred_engine(protection: Optional[Mapping]) -> str:
    """下一步该试哪一级引擎。无拦截信息时返回空串（调用方按原逻辑走）。"""
    if not protection:
        return ""
    engine = str(protection.get("preferred_engine") or "")
    if engine == "manual":
        return "manual"
    return engine or "browser"


def should_skip_curl_cffi(protection: Optional[Mapping]) -> bool:
    """JS 传感器型/验证码型的源，curl_cffi 再试多少次都是浪费——直接跳过。"""
    if not protection or not protection.get("is_block"):
        return False
    return str(protection.get("kind") or "") in {"js_sensor", "captcha"}


def needs_manual(protection: Optional[Mapping]) -> bool:
    """验证码型：机器过不去，标人工，不再自动重试。

    必须同时满足「确实判定被拦」——否则登录页/评论框上的验证码挂件会被
    误判成拦截（实测发现过这类误报）。
    """
    if not protection or not protection.get("is_block"):
        return False
    return str(protection.get("action") or "") == "needs_manual" or str(
        protection.get("kind") or ""
    ) == "captcha"


def plan_engines(protection: Optional[Mapping], available: Iterable[str]) -> List[str]:
    """按厂商类型给出本次应该尝试的引擎顺序（只保留可用的）。"""
    available_set = {str(item) for item in available}
    if not protection or not protection.get("is_block"):
        return [item for item in ("curl_cffi", "browser") if item in available_set]
    if needs_manual(protection):
        return []
    first = preferred_engine(protection)
    order = [first, str(protection.get("fallback_engine") or "browser")]
    ordered: List[str] = []
    for engine in order:
        if engine in available_set and engine not in ordered:
            ordered.append(engine)
    if not ordered:
        ordered = [item for item in ("curl_cffi", "browser") if item in available_set]
    return ordered


# ────────────────────────── 信源级统计与放弃策略 ──────────────────────────

def _give_up_threshold(kind: str, total_count: int, settings=None) -> int:
    settings = _decision_settings(settings)
    explicit = int(getattr(settings, "ANTIBOT_GIVE_UP_THRESHOLD", 0) or 0)
    if explicit > 0:
        return explicit
    return int(_KIND_GIVE_UP_DEFAULT.get(str(kind or ""), 5))


def source_block_state(metadata: Optional[Mapping]) -> Dict:
    """从信源 metadata 读出当前的反爬状态（供扫描器/看板读取）。"""
    meta = metadata or {}
    vendors = meta.get(META_VENDORS) or {}
    if not isinstance(vendors, Mapping):
        vendors = {}
    return {
        "vendors": {str(k): int(v or 0) for k, v in vendors.items()},
        "total": int(meta.get(META_TOTAL) or 0),
        "status": str(meta.get(META_STATUS) or ""),
        "reason": str(meta.get(META_REASON) or ""),
        "last_vendor": str(meta.get(META_LAST_VENDOR) or ""),
        "last_label": str(meta.get(META_LAST_LABEL) or ""),
        "last_at": str(meta.get(META_LAST_AT) or ""),
        "needs_proxy": bool(meta.get(META_NEEDS_PROXY)),
        "needs_manual": bool(meta.get(META_NEEDS_MANUAL)),
    }


def should_skip_source(metadata: Optional[Mapping], settings=None) -> bool:
    """放弃策略：该信源已达放弃阈值 → 不再派发任务。"""
    settings = _decision_settings(settings)
    if not bool(getattr(settings, "ANTIBOT_GIVE_UP_ENABLED", True)):
        return False
    return source_block_state(metadata)["status"] == "blocked"


def record_source_block(
    source_id,
    protection: Optional[Mapping],
    *,
    db=None,
    settings=None,
) -> Dict:
    """记录一次拦截：累计厂商计数；达到阈值就把信源标记为「不再派发任务」。

    只写 intel_sources.metadata_json（自由 JSON 字段），不改表结构。
    """
    result: Dict = {"recorded": False}
    try:
        source_id = int(source_id or 0)
        if source_id <= 0 or not protection:
            return result
        if not bool(getattr(_decision_settings(settings), "ANTIBOT_DETECTOR_ENABLED", True)):
            return result
        from utils import get_china_time

        if db is None:
            from sqlite_database import sqlite_db

            db = sqlite_db
        vendor = str(protection.get("vendor") or "")
        if not vendor:
            return result
        db._ensure_connection()
        with db.lock:
            cursor = db.connection.cursor()
            try:
                row = cursor.execute(
                    "SELECT metadata_json FROM intel_sources WHERE id=?", (source_id,)
                ).fetchone()
                if row is None:
                    return result
                try:
                    metadata = json.loads((row["metadata_json"] if row else "") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
                vendors = metadata.get(META_VENDORS)
                if not isinstance(vendors, Mapping):
                    vendors = {}
                vendors = {str(k): int(v or 0) for k, v in vendors.items()}
                vendors[vendor] = vendors.get(vendor, 0) + 1
                total = int(metadata.get(META_TOTAL) or 0) + 1
                now_text = get_china_time().strftime("%Y-%m-%d %H:%M:%S")
                metadata[META_VENDORS] = vendors
                metadata[META_TOTAL] = total
                metadata[META_LAST_VENDOR] = vendor
                metadata[META_LAST_LABEL] = str(protection.get("label") or vendor)
                metadata[META_LAST_AT] = now_text
                if bool(protection.get("is_block")):
                    metadata[META_NEEDS_PROXY] = bool(
                        str(protection.get("kind") or "") in {"captcha", "js_sensor"}
                    )
                    metadata[META_NEEDS_MANUAL] = needs_manual(protection)
                threshold = _give_up_threshold(
                    str(protection.get("kind") or ""), total, settings
                )
                vendor_count = vendors.get(vendor, 0)
                if vendor_count >= threshold:
                    metadata[META_STATUS] = "blocked"
                    metadata[META_REASON] = (
                        f"被{metadata[META_LAST_LABEL]}拦截{vendor_count}次，不再派发任务"
                    )
                elif not str(metadata.get(META_STATUS) or ""):
                    metadata[META_STATUS] = "warn"
                    metadata[META_REASON] = (
                        f"被{metadata[META_LAST_LABEL]}拦截{vendor_count}次"
                        f"（达到{threshold}次后停止派发）"
                    )
                cursor.execute(
                    "UPDATE intel_sources SET metadata_json=?, updated_at=? WHERE id=?",
                    (
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                        now_text,
                        source_id,
                    ),
                )
                db.connection.commit()
                result = {
                    "recorded": True,
                    "source_id": source_id,
                    "vendor": vendor,
                    "vendor_count": vendor_count,
                    "total": total,
                    "threshold": threshold,
                    "status": metadata.get(META_STATUS),
                    "reason": metadata.get(META_REASON),
                }
            finally:
                cursor.close()
    except Exception as exc:
        print(
            f"[antibot] 信源拦截统计写入失败（已忽略）: {type(exc).__name__}: {str(exc)[:160]}",
            flush=True,
        )
    return result


def record_source_success(source_id, *, db=None) -> Dict:
    """该信源这次真的抓到内容了 → 解除放弃标记（累计计数保留，便于看板复盘）。"""
    result: Dict = {"cleared": False}
    try:
        source_id = int(source_id or 0)
        if source_id <= 0:
            return result
        if db is None:
            from sqlite_database import sqlite_db

            db = sqlite_db
        db._ensure_connection()
        with db.lock:
            cursor = db.connection.cursor()
            try:
                row = cursor.execute(
                    "SELECT metadata_json FROM intel_sources WHERE id=?", (source_id,)
                ).fetchone()
                if row is None:
                    return result
                try:
                    metadata = json.loads((row["metadata_json"] if row else "") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
                if str(metadata.get(META_STATUS) or "") != "blocked":
                    return result
                metadata[META_STATUS] = "recovered"
                metadata[META_REASON] = "抓取已恢复，重新纳入轮询"
                metadata[META_NEEDS_PROXY] = False
                metadata[META_NEEDS_MANUAL] = False
                from utils import get_china_time

                now_text = get_china_time().strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute(
                    "UPDATE intel_sources SET metadata_json=?, updated_at=? WHERE id=?",
                    (
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                        now_text,
                        source_id,
                    ),
                )
                db.connection.commit()
                result = {"cleared": True, "source_id": source_id}
            finally:
                cursor.close()
    except Exception:
        pass
    return result


# ────────────────────────── 看板统计 ──────────────────────────

def blocked_vendor_ranking(db=None, *, limit: int = 20) -> List[Dict]:
    """被拦厂商 Top：聚合所有信源 metadata 里的厂商计数。"""
    rows = _all_source_rows(db)
    totals: Dict[str, Dict] = {}
    for row in rows:
        state = source_block_state(_parse_metadata(row.get("metadata_json")))
        for vendor, count in state["vendors"].items():
            item = totals.setdefault(
                vendor, {"vendor": vendor, "label": vendor, "count": 0, "sources": 0}
            )
            item["count"] += int(count)
            item["sources"] += 1
    for item in totals.values():
        item["label"] = _vendor_label(item["vendor"])
    return sorted(totals.values(), key=lambda item: (-item["count"], item["vendor"]))[: max(1, limit)]


def source_health_report(db=None, *, limit: int = 200) -> Dict:
    """信源健康度：被拦/放弃/需人工的信源清单，供行业包信源管理页展示。"""
    rows = _all_source_rows(db)
    blocked: List[Dict] = []
    warn: List[Dict] = []
    healthy = 0
    for row in rows:
        state = source_block_state(_parse_metadata(row.get("metadata_json")))
        record = {
            "source_id": int(row.get("id") or 0),
            "name": str(row.get("name") or ""),
            "source_url": str(row.get("source_url") or ""),
            "authority_level": row.get("authority_level"),
            "enabled": bool(row.get("is_enabled")),
            "status": state["status"],
            "reason": state["reason"],
            "total": state["total"],
            "last_vendor": state["last_vendor"],
            "last_label": state["last_label"],
            "last_at": state["last_at"],
            "needs_proxy": state["needs_proxy"],
            "needs_manual": state["needs_manual"],
            "vendors": state["vendors"],
        }
        if state["status"] == "blocked":
            blocked.append(record)
        elif state["total"] > 0:
            warn.append(record)
        else:
            healthy += 1
    warn.sort(key=lambda item: -item["total"])
    blocked.sort(key=lambda item: -item["total"])
    return {
        "source_total": len(rows),
        "healthy_count": healthy,
        "blocked_count": len(blocked),
        "warn_count": len(warn),
        "blocked_sources": blocked[: max(1, limit)],
        "warn_sources": warn[: max(1, limit)],
        "vendor_ranking": blocked_vendor_ranking(db, limit=20),
    }


# ── 确定性硬错误：不是"被反爬"，而是环境/代码坏了 ──────────────────────
# 这类错误**重试一万次结果都一样**，所以第一次出现就该止损并留痕。
# 它们比"被反爬拦"更该先覆盖：反爬要靠累计次数判断，这里是确定性失败。
# 实测依据（A 机 intel_scan_runs）：镜像缺浏览器导致 1588 次扫描一秒内失败，
# 却长时间没人发现，产能一直在漏。
HARD_ERROR_RULES = (
    ("browser_missing", (
        "executable doesn't exist",
        "please run the following command to download new browsers",
    )),
    ("sync_api_in_async", ("sync api inside the asyncio loop",)),
    ("browser_engine_missing", (
        "playwright is not installed",
        "no module named 'playwright'",
        "no module named 'patchright'",
    )),
    ("disk_or_permission", ("read-only file system", "no space left on device")),
    # 出站策略判定"受限网络地址"：本机没放行该网段（实测 B 机没放行自建 RSSHub 10.88.0.0/24，
    # 而 A 机放行了 → 同一份种子在 A 机正常、在 B 机每轮白跑）。这也是确定性失败，重试无意义。
    ("restricted_network", ("解析到受限网络地址",)),
)
META_HARD_ERROR = "hard_error"
META_HARD_ERROR_DETAIL = "hard_error_detail"
META_HARD_ERROR_AT = "hard_error_at"


def classify_hard_error(error_text: str) -> str:
    """把确定性失败归类（返回标签）；不是硬错误返回空串。"""
    text = str(error_text or "").casefold()
    if not text:
        return ""
    for label, patterns in HARD_ERROR_RULES:
        if any(pattern in text for pattern in patterns):
            return label
    return ""


def record_source_hard_error(source_id, label: str, detail: str = "", *, db=None) -> Dict:
    """确定性硬错误 → 第一次出现就把该信源标记为「不再派发任务」。

    与反爬放弃策略共用同一套 metadata 键，所以扫描器的跳过判定
    （should_skip_source）不需要任何改动就会生效；恢复也自动：
    该信源某轮真的抓到内容时，record_source_success 会解除标记。
    """
    result: Dict = {"recorded": False}
    try:
        source_id = int(source_id or 0)
        if source_id <= 0 or not label:
            return result
        from utils import get_china_time

        if db is None:
            from sqlite_database import sqlite_db

            db = sqlite_db
        db._ensure_connection()
        with db.lock:
            cursor = db.connection.cursor()
            try:
                row = cursor.execute(
                    "SELECT metadata_json FROM intel_sources WHERE id=?", (source_id,)
                ).fetchone()
                if row is None:
                    return result
                try:
                    metadata = json.loads((row["metadata_json"] if row else "") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
                now_text = get_china_time().strftime("%Y-%m-%d %H:%M:%S")
                metadata[META_HARD_ERROR] = label
                metadata[META_HARD_ERROR_DETAIL] = str(detail or "")[:400]
                metadata[META_HARD_ERROR_AT] = now_text
                metadata[META_STATUS] = "blocked"
                metadata[META_REASON] = (
                    f"确定性失败（{label}），第一次出现即停止派发；修复后抓到内容会自动恢复"
                )
                metadata[META_LAST_AT] = now_text
                cursor.execute(
                    "UPDATE intel_sources SET metadata_json=?, updated_at=? WHERE id=?",
                    (json.dumps(metadata, ensure_ascii=False, sort_keys=True), now_text, source_id),
                )
                db.connection.commit()
                result = {"recorded": True, "source_id": source_id, "label": label,
                          "reason": metadata[META_REASON]}
            finally:
                cursor.close()
    except Exception as exc:
        print(f"[antibot] 硬错误标记写入失败（已忽略）: {type(exc).__name__}: {str(exc)[:160]}",
              flush=True)
    return result


def source_hard_error(metadata: Optional[Mapping]) -> Dict:
    """读出一条信源记录过的硬错误（供看板显示）。"""
    meta = metadata or {}
    return {
        "label": str(meta.get(META_HARD_ERROR) or ""),
        "detail": str(meta.get(META_HARD_ERROR_DETAIL) or ""),
        "at": str(meta.get(META_HARD_ERROR_AT) or ""),
    }


def _vendor_label(vendor_id: str) -> str:
    try:
        vendor = load_antibot_rules()["vendors"].get(str(vendor_id)) or {}
        return str(vendor.get("label") or vendor_id)
    except Exception:
        return str(vendor_id)


# ─────────────────── 隐身指纹自检（把规则分类反过来用在自己身上） ───────────────────
# 思路与 Scrapfly 的思路一致但不含其任何代码：既然我们能按 window / JS hook 分类识别
# 别人怎么拦我们，就能用同一套分类检查「我们自己的隐身做得如何」——浏览器里凡是留下
# 自动化痕迹的地方（window 上的 CDP/selenium 变量、navigator.webdriver、native 函数被改写、
# WebGL 渲染器是 SwiftShader 等），都是我们会被反向识别的漏点。

_AUTOMATION_GLOBALS = (
    "cdc_adoQpoasnfa76pfcZLmcfl_",
    "$cdc_asdjflasutopfhvcZLmcfl_",
    "__webdriver_evaluate",
    "__selenium_unwrapped",
    "__driver_evaluate",
    "__playwright",
    "__pw_manual",
    "__PW_inspect",
    "_phantom",
    "callPhantom",
    "__nightmare",
    "domAutomation",
    "domAutomationController",
    "_Selenium_IDE_Recorder",
)


def _automation_globals_js() -> str:
    names = json.dumps(list(_AUTOMATION_GLOBALS))
    return (
        "(() => { const names = %s;"
        " const hit = names.filter(n => { try { return n in window } catch (e) { return false } });"
        " return hit.join(',') })()" % names
    )


# (探测 id, 说明, JS 表达式, 判定函数, 严重级别, 建议)
SELF_CHECK_PROBES = (
    (
        "navigator.webdriver",
        "navigator.webdriver 是否为真（最经典的自动化标志）",
        "(() => String(navigator.webdriver))()",
        lambda v: str(v).casefold() not in {"true", "1"},
        "high",
        "应通过启动参数/补丁置为 false；为真时任何反爬都会一眼识别。",
    ),
    (
        "automation_globals",
        "window 上是否残留 CDP/Selenium/Playwright 变量",
        _automation_globals_js(),
        lambda v: not str(v or "").strip(),
        "high",
        "残留即等价于自报家门，需在上下文初始化脚本里删除。",
    ),
    (
        "user_agent_headless",
        "UA 是否含 HeadlessChrome",
        "(() => navigator.userAgent)()",
        lambda v: "headless" not in str(v).casefold(),
        "high",
        "headless 模式必须使用非 headless 的 UA。",
    ),
    (
        "native_toString",
        "关键原生函数是否仍是 [native code]",
        "(() => Function.prototype.toString.call(navigator.permissions && navigator.permissions.query"
        " ? navigator.permissions.query : Object.keys))()",
        lambda v: "native code" in str(v),
        "medium",
        "被改写的原生函数（如伪造 webdriver 的实现）会用 toString 暴露。",
    ),
    (
        "plugins",
        "navigator.plugins 数量",
        "(() => navigator.plugins.length)()",
        lambda v: int(v or 0) > 0,
        "medium",
        "真机 Chrome 至少 3-5 个插件项；0 是 headless 特征。",
    ),
    (
        "languages",
        "navigator.languages 数量",
        "(() => navigator.languages.length)()",
        lambda v: int(v or 0) > 0,
        "low",
        "语言列表为空属于明显异常。",
    ),
    (
        "hardware_concurrency",
        "navigator.hardwareConcurrency",
        "(() => navigator.hardwareConcurrency || 0)()",
        lambda v: int(v or 0) > 0,
        "low",
        "为 0/缺失属于异常指纹。",
    ),
    (
        "chrome_object",
        "window.chrome 是否存在（Chrome 专有）",
        "(() => typeof window.chrome)()",
        lambda v: str(v) == "object",
        "medium",
        "缺失 window.chrome 是 Chromium 自动化环境的典型特征。",
    ),
    (
        "webgl_renderer",
        "WebGL 渲染器字符串",
        "(() => { try { const c = document.createElement('canvas');"
        " const gl = c.getContext('webgl') || c.getContext('experimental-webgl');"
        " if (!gl) return 'no-webgl';"
        " const e = gl.getExtension('WEBGL_debug_renderer_info');"
        " return e ? String(gl.getParameter(e.UNMASKED_RENDERER_WEBGL)) : 'no-debug-info' }"
        " catch (e) { return 'error:' + e.name } })()",
        lambda v: not any(
            token in str(v).casefold() for token in ("swiftshader", "llvmpipe", "software", "no-webgl")
        ),
        "medium",
        "SwiftShader/软件渲染是 headless 服务器的标志，真实浏览器应报出具体 GPU。",
    ),
    (
        "window_metrics",
        "window.outerWidth/outerHeight 是否非零",
        "(() => [window.outerWidth, window.outerHeight, screen.width, screen.height].join('x'))()",
        lambda v: all(int(part or 0) > 0 for part in str(v).split("x")),
        "low",
        "全部为 0 说明是无头尺寸，容易被识别。",
    ),
    (
        "permission_consistency",
        "Notification 权限与 permissions API 是否一致",
        "(async () => { try { const p = await navigator.permissions.query({name:'notifications'});"
        " return String(p.state) + '/' + String(Notification.permission) }"
        " catch (e) { return 'error:' + e.name } })()",
        lambda v: not str(v).startswith("error:") and "denied/denied" not in str(v),
        "low",
        "headless 常见的权限不一致会暴露自动化环境。",
    ),
    (
        "timezone",
        "时区能否正常解析",
        "(() => Intl.DateTimeFormat().resolvedOptions().timeZone || '')()",
        lambda v: bool(str(v or "").strip()),
        "low",
        "时区缺失或与出口 IP 严重不符都会被风控记录。",
    ),
)


def stealth_self_check(
    url: str = "https://www.baidu.com",
    *,
    timeout_ms: int = 30000,
    headless: bool = True,
) -> Dict:
    """用真实浏览器跑一遍指纹自检，报告我们自己的隐身漏点。

    走 Scrapling 的 StealthyFetcher（我们爬虫实际用的隐身引擎），通过 page_action
    拿到 Playwright page 执行探针。任何异常都降级为 available=False，不影响调用方。

    注意：默认 URL 不能是 about:blank——StealthyFetcher 需要真实导航才能拿到 page。
    """
    result: Dict = {
        "available": False,
        "engine": "scrapling.StealthyFetcher",
        "url": url,
        "checks": [],
        "failed_count": 0,
        "high_severity_count": 0,
        "error": "",
    }
    try:
        from scrapling.fetchers import StealthyFetcher
    except Exception as exc:
        result["error"] = f"Scrapling 不可用：{type(exc).__name__}: {str(exc)[:160]}"
        return result

    captured: Dict[str, object] = {}
    errors: Dict[str, str] = {}

    def _probe(page):
        for probe_id, _label, js, _ok, _severity, _advice in SELF_CHECK_PROBES:
            try:
                captured[probe_id] = page.evaluate(js)
            except Exception as exc:  # 单个探针失败不影响整轮
                errors[probe_id] = f"{type(exc).__name__}: {str(exc)[:120]}"

    try:
        StealthyFetcher.fetch(
            url,
            headless=headless,
            network_idle=False,
            timeout=timeout_ms,
            page_action=_probe,
        )
        result["available"] = True
    except Exception as exc:
        result["error"] = f"浏览器自检失败：{type(exc).__name__}: {str(exc)[:200]}"
        return result

    checks: List[Dict] = []
    for probe_id, label, _js, ok_fn, severity, advice in SELF_CHECK_PROBES:
        if probe_id in errors:
            checks.append(
                {
                    "id": probe_id,
                    "label": label,
                    "value": "",
                    "ok": False,
                    "severity": severity,
                    "advice": advice,
                    "error": errors[probe_id],
                }
            )
            continue
        if probe_id not in captured:
            continue
        value = captured[probe_id]
        try:
            ok = bool(ok_fn(value))
        except Exception:
            ok = False
        checks.append(
            {
                "id": probe_id,
                "label": label,
                "value": str(value)[:200],
                "ok": ok,
                "severity": severity,
                "advice": advice,
            }
        )
    result["checks"] = checks
    result["failed_count"] = sum(1 for item in checks if not item["ok"])
    result["high_severity_count"] = sum(
        1 for item in checks if not item["ok"] and item["severity"] == "high"
    )
    result["passed_count"] = sum(1 for item in checks if item["ok"])
    result["total_count"] = len(checks)
    return result


def _parse_metadata(raw) -> dict:
    try:
        value = json.loads(str(raw or "") or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _all_source_rows(db=None) -> List[Dict]:
    try:
        if db is None:
            from sqlite_database import sqlite_db

            db = sqlite_db
        db._ensure_connection()
        with db.lock:
            cursor = db.connection.cursor()
            try:
                # 列名必须与 intel_schema.intel_sources 一致：信源名称列是 source_name，
                # 不是 name（写错会被下面的 except 吞掉，导致看板恒返回全 0）。
                cursor.execute(
                    "SELECT id, source_name AS name, source_url, authority_level,"
                    " is_enabled, metadata_json FROM intel_sources"
                )
                return [dict(row) for row in cursor.fetchall()]
            finally:
                cursor.close()
    except Exception:
        return []
