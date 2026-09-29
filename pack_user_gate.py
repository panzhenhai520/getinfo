"""用户个人门禁覆盖（按用户层，叠加语义）。

设计要点（与产品决策一致）：
  · 官方包仍是唯一权威：`candidate_gate.anchor_keywords` 等由 admin 发布；本模块只做**个人覆盖**。
  · 锚点/机构词**只能收紧不能放宽**：保存时强制校验"必须是官方门禁的子集"，越界直接拒绝。
  · 品牌(brands)/趋势主题(trend_topics) 属个人关注维度，可自由增删；未设置时**继承**官方值。
  · 未设置任何覆盖的用户**不写 anything**、读取面不做任何过滤 = 看全包（保持既有行为）。
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from industry_packs import industry_anchor_keywords, normalize_intel_text
from sqlite_database import sqlite_db
from utils import get_china_time

# 覆盖层里"未设置"与"显式清空"必须区分：None = 未设置(继承)，[] = 显式清空
OVERRIDE_COLUMNS = {
    "anchor_keywords": "anchor_keywords_json",
    "entity_keywords": "entity_keywords_json",
    "brands": "brand_keywords_json",
    "trend_topics": "trend_topics_json",
    "sources": "source_overrides_json",
    "report_seeds": "report_seeds_json",
    "trend_settings": "trend_settings_json",
}


def _loads(value, default):
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _clean_list(values) -> List[str]:
    """去空白、按归一化去重，保留首次出现的原样写法。"""
    if not isinstance(values, (list, tuple)):
        return []
    seen, result = set(), []
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        key = normalize_intel_text(text)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def active_pack_user() -> Optional[Dict]:
    """当前请求的包用户；admin（无绑定）返回 None。"""
    try:
        from pack_tenant import active_pack_user as _active
        return _active()
    except Exception:
        return None


def get_override(pack_user_id: int) -> Dict:
    """读取某用户的覆盖；未设置过的字段为 None（表示继承官方值）。"""
    if not pack_user_id:
        return {}
    sqlite_db._ensure_connection()
    with sqlite_db.lock:
        cursor = sqlite_db.connection.cursor()
        row = cursor.execute(
            "SELECT industry_pack_id, anchor_keywords_json, entity_keywords_json, "
            "brand_keywords_json, trend_topics_json, source_overrides_json, "
            "report_seeds_json, trend_settings_json, updated_at "
            "FROM pack_user_gate_overrides WHERE pack_user_id=?",
            (int(pack_user_id),),
        ).fetchone()
        cursor.close()
    if not row:
        return {}
    data = dict(row)
    override = {"industry_pack_id": str(data.get("industry_pack_id") or "")}
    for field, column in OVERRIDE_COLUMNS.items():
        raw = data.get(column)
        if raw is None or str(raw).strip() == "":
            override[field] = None
            continue
        default = {} if field in ("sources", "report_seeds", "trend_settings") else []
        override[field] = _loads(raw, default)
    override["updated_at"] = str(data.get("updated_at") or "")
    return override


def validate_subset(proposed: List[str], official: List[str]) -> List[str]:
    """返回越界词（不在官方门禁内的）。空列表表示校验通过。"""
    allowed = {normalize_intel_text(item) for item in official if str(item or "").strip()}
    return [word for word in proposed if normalize_intel_text(word) not in allowed]


def save_override(pack_user_id: int, pack: Dict, payload: Dict, actor: str = "") -> Dict:
    """保存个人覆盖。锚点/机构词必须落在官方门禁内，否则抛 ValueError。"""
    if not pack_user_id:
        raise ValueError("需要包用户身份")
    pack_id = str(pack.get("id") or "")
    if not pack_id:
        raise ValueError("行业包不合法")

    anchors = _clean_list(payload.get("anchor_keywords")) if payload.get("anchor_keywords") is not None else []
    entities = _clean_list(payload.get("entity_keywords")) if payload.get("entity_keywords") is not None else []
    official_anchors = _clean_list((pack.get("candidate_gate") or {}).get("anchor_keywords") or [])
    official_entities = _clean_list((pack.get("candidate_gate") or {}).get("entity_keywords") or [])
    allowed = official_anchors + official_entities or _clean_list(industry_anchor_keywords(pack))

    out_of_scope = validate_subset(anchors, allowed) + validate_subset(entities, allowed)
    if out_of_scope:
        raise ValueError(
            "以下词不在官方行业门禁内，个人门禁只能在官方范围内收紧，不能放宽："
            + "、".join(sorted(set(out_of_scope))[:10])
        )

    brands = _clean_list(payload.get("brands")) if payload.get("brands") is not None else []
    trend_topics = _clean_list(payload.get("trend_topics")) if payload.get("trend_topics") is not None else []
    sources = payload.get("sources") if isinstance(payload.get("sources"), dict) else {}
    report_seeds = payload.get("report_seeds") if isinstance(payload.get("report_seeds"), dict) else {}
    trend_settings = payload.get("trend_settings") if isinstance(payload.get("trend_settings"), dict) else {}

    now = get_china_time().strftime("%Y-%m-%d %H:%M:%S")
    sqlite_db._ensure_connection()
    with sqlite_db.lock:
        cursor = sqlite_db.connection.cursor()
        cursor.execute(
            "INSERT INTO pack_user_gate_overrides (pack_user_id, industry_pack_id, "
            "anchor_keywords_json, entity_keywords_json, brand_keywords_json, trend_topics_json, "
            "source_overrides_json, report_seeds_json, trend_settings_json, updated_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(pack_user_id) DO UPDATE SET industry_pack_id=excluded.industry_pack_id, "
            "anchor_keywords_json=excluded.anchor_keywords_json, entity_keywords_json=excluded.entity_keywords_json, "
            "brand_keywords_json=excluded.brand_keywords_json, trend_topics_json=excluded.trend_topics_json, "
            "source_overrides_json=excluded.source_overrides_json, report_seeds_json=excluded.report_seeds_json, "
            "trend_settings_json=excluded.trend_settings_json, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (
                int(pack_user_id), pack_id,
                json.dumps(anchors, ensure_ascii=False), json.dumps(entities, ensure_ascii=False),
                json.dumps(brands, ensure_ascii=False), json.dumps(trend_topics, ensure_ascii=False),
                json.dumps(sources, ensure_ascii=False), json.dumps(report_seeds, ensure_ascii=False),
                json.dumps(trend_settings, ensure_ascii=False), str(actor or ""), now, now,
            ),
        )
        sqlite_db.connection.commit()
        cursor.close()
    return get_override(pack_user_id)


def materialize_visibility(cursor, article_id: int, pack_id: str) -> int:
    """入库/分类后按用户物化可见性，返回写入行数。

    判定规则（叠加语义）：
      · 只处理**有个人门禁设置**的用户；没设置的用户不写行 = 看全包（不改变既有行为）。
      · 用户设置了硬锚点/机构词 → 必须命中，否则该用户看不到这篇（这就是"只能收紧"的落地）。
      · 只设置了 brands（竞争对手）→ 不做可见性收缩，仅记录命中，供"我的竞争对手动态"使用。

    使用调用方传入的 cursor，不自行获取 sqlite_db.lock：本函数从分类事务内部调用，
    重复加锁会与调用方死锁。任何异常由调用方吞掉，绝不影响分类结果。
    """
    if not article_id or not pack_id:
        return 0
    rows = cursor.execute(
        "SELECT o.pack_user_id, o.anchor_keywords_json, o.entity_keywords_json, o.brand_keywords_json, "
        "o.trend_settings_json "
        "FROM pack_user_gate_overrides o "
        "JOIN pack_users u ON u.id = o.pack_user_id "
        "WHERE o.industry_pack_id = ?",
        (str(pack_id),),
    ).fetchall()
    if not rows:
        return 0
    article = cursor.execute(
        "SELECT title, content FROM articles WHERE id = ?", (int(article_id),)
    ).fetchone()
    if not article:
        return 0
    data = dict(article)
    text = normalize_intel_text("%s\n%s" % (data.get("title") or "", data.get("content") or ""))
    now_text = get_china_time().strftime("%Y-%m-%d %H:%M:%S")
    written = 0
    for row in rows:
        item = dict(row)
        anchors = _loads(item.get("anchor_keywords_json"), []) or []
        entities = _loads(item.get("entity_keywords_json"), []) or []
        brands = _loads(item.get("brand_keywords_json"), []) or []
        gate = [word for word in (anchors + entities) if normalize_intel_text(word)]
        # 个人负面词：命中即对**该用户**隐藏（个人层决定，不影响他人）
        settings = _loads(item.get("trend_settings_json"), {}) or {}
        negatives = [word for word in (settings.get("negative_keywords") or []) if normalize_intel_text(word)]
        if negatives and any(normalize_intel_text(word) in text for word in negatives):
            continue
        matched_anchors = [word for word in gate if normalize_intel_text(word) in text]
        if gate and not matched_anchors:
            continue
        matched_brands = [word for word in brands if normalize_intel_text(word) and normalize_intel_text(word) in text]
        # 主题归属按用户算（叠加语义：官方主题 ∪ 我的主题）。
        # 同一篇文章对不同用户可有不同主题，各写各的行 → 互不影响。
        # 只对"自定义了主题"的用户计算；其他人不写 topic_keys，读取时读包级关联。
        topic_keys = []
        personal_topics = [t for t in (settings.get("personal_topics") or []) if isinstance(t, dict)]
        if personal_topics:
            try:
                from intel_classifier import match_fixed_topics
                from industry_packs import industry_pack_loader
                pack = industry_pack_loader.load(pack_id, enabled_only=False)
                effective = list(pack.get("fixed_topics") or []) + personal_topics
                matched = match_fixed_topics(
                    {"title": data.get("title") or "",
                     "content": data.get("content") or "",
                     "matched_keywords": ""},
                    {"fixed_topics": effective},
                )
                topic_keys = [str(m.get("key") or "") for m in matched if m.get("key")]
            except Exception:
                topic_keys = []
        cursor.execute(
            "INSERT INTO article_user_visibility "
            "(article_id, pack_user_id, matched_brands_json, matched_anchors_json, topic_keys_json, created_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(article_id, pack_user_id) DO UPDATE SET "
            "matched_brands_json=excluded.matched_brands_json, "
            "matched_anchors_json=excluded.matched_anchors_json, "
            "topic_keys_json=excluded.topic_keys_json",
            (
                int(article_id),
                int(item["pack_user_id"]),
                json.dumps(matched_brands, ensure_ascii=False),
                json.dumps(matched_anchors, ensure_ascii=False),
                json.dumps(topic_keys, ensure_ascii=False),
                now_text,
            ),
        )
        written += 1
    return written


def current_pack_user_id() -> int:
    """当前请求的包用户 id；admin 或未登录返回 0。"""
    user = active_pack_user() or {}
    try:
        return int(user.get("id") or user.get("pack_user_id") or 0)
    except (TypeError, ValueError):
        return 0


def visibility_filter(pack_user_id: int, column: str = "a.id"):
    """按用户可见性生成 SQL 片段与参数。

    规则（叠加语义）：**有个人门禁设置** → 只保留 `article_user_visibility` 里的文章；
    **没有设置** → 返回空片段，不做任何过滤（看全包，与既有行为一致）。

    返回 (sql片段, 参数列表)，调用方拼进自己的 WHERE 即可，例如：
        clause, params = visibility_filter(uid)
        cursor.execute("SELECT ... WHERE status='active'" + clause, extra + params)
    """
    if not pack_user_id:
        return "", []
    override = get_override(int(pack_user_id))
    if not override:
        return "", []
    gate = override.get("anchor_keywords") or override.get("entity_keywords")
    if not gate:
        # 只设了品牌/趋势（叠加关注），不收缩可见性
        return "", []
    return (
        " AND %s IN (SELECT article_id FROM article_user_visibility WHERE pack_user_id=?)" % column,
        [int(pack_user_id)],
    )


def merge_source_override(pack_user_id: int, pack_id: str, url: str,
                          patch: Optional[Dict] = None, *, delete: bool = False,
                          actor: str = "") -> Dict:
    """字段级合并个人 URL：只改 `source_overrides` 里的这一条，**绝不触碰其它字段**。

    为什么不能用 save_override()：那个是整份覆盖语义（未传的字段会被写成空），
    用它加一个 URL 会把用户已设的门禁锚点、品牌、趋势主题全部抹掉。
    这里只 UPDATE 一列，并在无行时插入一条仅含 source_overrides 的最小行
    （其余列为空数组 = "未设置"，visibility_filter 会视为不过滤，安全）。
    """
    if not pack_user_id:
        raise ValueError("需要包用户身份")
    target = str(url or "").strip()
    if not target:
        raise ValueError("url 不能为空")
    sqlite_db._ensure_connection()
    now = get_china_time().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite_db.lock:
        cursor = sqlite_db.connection.cursor()
        row = cursor.execute(
            "SELECT source_overrides_json FROM pack_user_gate_overrides WHERE pack_user_id=?",
            (int(pack_user_id),),
        ).fetchone()
        overrides = _loads(dict(row).get("source_overrides_json") if row else "", {}) or {}
        if delete:
            overrides.pop(target, None)
        else:
            merged = dict(overrides.get(target) or {})
            merged.update({k: v for k, v in (patch or {}).items() if v is not None})
            merged.setdefault("name", target)
            merged.setdefault("enabled", True)
            overrides[target] = merged
        payload = json.dumps(overrides, ensure_ascii=False)
        if row:
            cursor.execute(
                "UPDATE pack_user_gate_overrides SET source_overrides_json=?, updated_at=? "
                "WHERE pack_user_id=?",
                (payload, now, int(pack_user_id)),
            )
        else:
            cursor.execute(
                "INSERT INTO pack_user_gate_overrides (pack_user_id, industry_pack_id, "
                "anchor_keywords_json, entity_keywords_json, brand_keywords_json, trend_topics_json, "
                "source_overrides_json, report_seeds_json, trend_settings_json, updated_by, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(pack_user_id), str(pack_id), '[]', '[]', '[]', '[]', payload, '{}', '{}',
                 str(actor or ""), now, now),
            )
        sqlite_db.connection.commit()
        cursor.close()
    return overrides


def materialize_user_history(pack_id: str, pack_user_id: int, limit: int = 3000) -> int:
    """用户保存门禁后，**回溯**为该用户物化已有文章的可见性。

    必须做这一步：`materialize_visibility` 只在"分类入库时"写行，用户是在文章入库之后
    才设门禁的，所以已有文章对他就等于不可见（表现为"设了门禁什么也看不到"）。
    这里按包把已分类的活跃文章补一遍，用户保存后立刻就能看到符合自己门禁的内容。
    自己获取连接与锁（不从事务内部调用），返回物化的文章数。
    """
    if not pack_id or not pack_user_id:
        return 0
    sqlite_db._ensure_connection()
    with sqlite_db.lock:
        cursor = sqlite_db.connection.cursor()
        try:
            rows = cursor.execute(
                "SELECT a.id FROM article_intel_classifications c "
                "JOIN articles a ON a.id=c.article_id AND a.status='active' "
                "WHERE c.industry_pack_id=? ORDER BY a.id DESC LIMIT ?",
                (str(pack_id), int(limit)),
            ).fetchall()
            written = 0
            for row in rows:
                written += 1 if materialize_visibility(cursor, int(dict(row)["id"]), pack_id) else 0
            sqlite_db.connection.commit()
            return written
        finally:
            cursor.close()


def user_topic_domains(pack_user_id: int) -> Dict[str, list]:
    """{topic_key: [domain, ...]}：该用户按"自己主题归属"的文章域名分布。

    读取面（首页主题卡 / 主题聚合页 / 文章详情标签）统一用这里的取数实现隔离语义：
    **有个人主题 → 用自己的数据；没有 → 调用方回落包级统计**。
    只对"自定义过主题"的用户有数据，其他人返回空 dict，零影响、零开销。
    """
    if not pack_user_id:
        return {}
    sqlite_db._ensure_connection()
    with sqlite_db.lock:
        cursor = sqlite_db.connection.cursor()
        try:
            rows = cursor.execute(
                "SELECT a.domain AS domain, v.topic_keys_json AS keys "
                "FROM article_user_visibility v "
                "JOIN articles a ON a.id=v.article_id AND a.status='active' "
                "WHERE v.pack_user_id=? AND COALESCE(v.topic_keys_json,'[]') NOT IN ('','[]')",
                (int(pack_user_id),),
            ).fetchall()
        finally:
            cursor.close()
    result: Dict[str, list] = {}
    for row in rows:
        data = dict(row)
        try:
            keys = json.loads(data.get("keys") or "[]")
        except Exception:
            keys = []
        for key in keys or []:
            result.setdefault(str(key), []).append(data.get("domain"))
    return result


def user_topic_article_ids(pack_user_id: int, topic_key: str) -> list:
    """该用户视角下属于某个主题的文章 id 列表（主题聚合页据此过滤）。

    返回空列表时，调用方应回落包级 `intel_topic_articles`——即"该用户没有个人主题"。
    """
    if not pack_user_id or not topic_key:
        return []
    sqlite_db._ensure_connection()
    with sqlite_db.lock:
        cursor = sqlite_db.connection.cursor()
        try:
            rows = cursor.execute(
                "SELECT v.article_id AS article_id, v.topic_keys_json AS keys "
                "FROM article_user_visibility v "
                "JOIN articles a ON a.id=v.article_id AND a.status='active' "
                "WHERE v.pack_user_id=? AND COALESCE(v.topic_keys_json,'[]') NOT IN ('','[]')",
                (int(pack_user_id),),
            ).fetchall()
        finally:
            cursor.close()
    target = str(topic_key)
    ids = []
    for row in rows:
        data = dict(row)
        try:
            keys = json.loads(data.get("keys") or "[]")
        except Exception:
            keys = []
        if target in [str(k) for k in (keys or [])]:
            ids.append(int(data.get("article_id") or 0))
    return ids


def effective_pack(pack: Dict, override: Dict) -> Dict:
    """把个人覆盖叠加到官方包上，返回**副本**（不修改原 manifest）。

    叠加语义：未设置的字段继承官方值；设置了的按其值生效。
    锚点/机构词只可能等于或小于官方集合（保存时已强校验）。
    """
    if not override:
        return pack
    merged = json.loads(json.dumps(pack, ensure_ascii=False))
    gate = merged.setdefault("candidate_gate", {}) if isinstance(merged.get("candidate_gate"), dict) else {}
    if override.get("anchor_keywords") is not None:
        gate["anchor_keywords"] = list(override["anchor_keywords"])
    if override.get("entity_keywords") is not None:
        gate["entity_keywords"] = list(override["entity_keywords"])
    if gate:
        merged["candidate_gate"] = gate
    if override.get("brands") is not None:
        merged["brands"] = list(override["brands"])
    if override.get("trend_topics") is not None:
        merged["trend_topics"] = list(override["trend_topics"])
    if override.get("trend_settings"):
        merged["trend_settings"] = dict(override["trend_settings"])
    if override.get("sources"):
        overrides = override["sources"]
        for source in merged.get("default_sources") or []:
            key = str(source.get("source_import_id") or source.get("url") or "")
            if key in overrides and isinstance(overrides[key], dict):
                source.update(overrides[key])
    if override.get("report_seeds"):
        merged["report_seeds"] = dict(override["report_seeds"])
    merged["_personal_override"] = {
        "applied": True,
        "updated_at": override.get("updated_at") or "",
    }
    return merged
