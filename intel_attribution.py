#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行业包归属兜底：保证每篇入库文章至少有一条行业包归属。

为什么需要：展示链路「最近关注」走 intel_repository.list_dashboard_recent_articles()，
它只按项目关键词筛、**不接收 industry_pack_id**，所以没有包归属的文章照样出现在
包页面上；而问答检索 ArticleRetriever._rows() 要求 article_intel_classifications
有本包记录 → 同一篇文章"页面看得到、AI 搜不到"（实测 id=6910 就是这样被漏掉的）。

分类任务（_enqueue_intel_classification）是异步的，且行业包关键词门禁不达标时
**不会写分类行**，所以入库时必须先落一条兜底归属，异步分类之后可以把它升级成
真实分类。

兜底行**不再硬编码「其他」**：一律落**纯规则**分类器
（intel_classifier.classify_article，不传 LLM、不走 fuse_rule_and_llm）的
final_category / rule_category / matched_keywords / score_details / final_confidence。
历史事故（A 机 invest_mgmt 实测）：原先硬编码 'other' + ON CONFLICT DO NOTHING，让该包
860 行兜底里 448 行永久停在「其他」（classifier_version='fallback-attribution-v1'），
其中 160 篇按当前已发布配置重跑规则会判 event/trend，库内 other 占比（93.37%）比
"用当前配置重跑"口径（74.77%）高 18.6 个百分点。

归属目标包的确定顺序：
  1. article_data 里显式给的 industry_pack_ids / industry_pack_id
  2. 按各行业包关键词表打分，命中最高且达到门槛的包
  3. 兜底用当前激活包（active_industry_pack_id）
  4. 文章已存在**兜底来源**的行时：只升级这些既有行（不外扩、不新增包）；
     已存在**非兜底来源**的行时：一行都不动（真分类/人工/LLM 结论优先）
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

_FALLBACK_SOURCE = "fallback_attribution"
_FALLBACK_CLASSIFIER_VERSION = "fallback-attribution-v1"
_RULE_CLASSIFIER_VERSION = "rule-v1"
_MIN_KEYWORD_HITS = 1
_CATEGORIES = ("trend", "event", "other")

# ON CONFLICT 的「只升级、不破坏」条件：目标行仍是兜底来源时才允许被规则结论覆盖。
# 写成常量拼接（不是用户输入，无注入面），避免把参数顺序耦合进 upsert 的 WHERE 子句。
_UPGRADE_ONLY_PREDICATE = (
    "article_intel_classifications.result_source = '{source}' "
    "OR article_intel_classifications.classifier_version = '{version}'"
).format(source=_FALLBACK_SOURCE, version=_FALLBACK_CLASSIFIER_VERSION)

# 规则结论写库时统一使用的列与顺序（INSERT 的 VALUES、ON CONFLICT 的 SET、
# 存量修复的 UPDATE SET 三处共用，避免列顺序漂移）。
_RULE_VALUE_COLUMNS = (
    "industry_pack_version", "classifier_version", "article_content_hash",
    "rule_category", "rule_confidence", "rule_reason",
    "score_details_json", "matched_keywords_json", "topic_tags_json",
    "final_category", "final_confidence", "final_reason",
    "result_source", "classified_at", "updated_at",
)

_INSERT_COLUMNS = (
    "article_id", "industry_pack_id", "activation_id",
) + _RULE_VALUE_COLUMNS + ("created_at",)

_INSERT_SQL = """
    INSERT INTO article_intel_classifications
        ({columns})
    VALUES ({placeholders})
    ON CONFLICT (article_id, industry_pack_id) DO UPDATE SET
        {assignments}
    WHERE {predicate}
""".format(
    columns=", ".join(_INSERT_COLUMNS),
    placeholders=", ".join("?" for _ in _INSERT_COLUMNS),
    assignments=", ".join("%s = excluded.%s" % (c, c) for c in _RULE_VALUE_COLUMNS),
    predicate=_UPGRADE_ONLY_PREDICATE,
)

_UPDATE_SQL = """
    UPDATE article_intel_classifications
       SET {assignments}
     WHERE id = ?
""".format(assignments=", ".join("%s = ?" % c for c in _RULE_VALUE_COLUMNS))


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_stamp() -> str:
    """备份文件名用的紧凑时间戳（UTC）。"""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _default_backup_dir() -> str:
    """默认备份目录：仓库 data/（与 industry_pack_backups 等运维产物同级）。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _is_fallback_row(result_source, classifier_version) -> bool:
    """该分类行是否是兜底归属写的（两个标记任一命中就算）。"""
    return (
        str(result_source or "") == _FALLBACK_SOURCE
        or str(classifier_version or "") == _FALLBACK_CLASSIFIER_VERSION
    )


def _safe_category(value) -> str:
    """分类值收敛到表上的 CHECK 允许值（trend/event/other）。"""
    text = str(value or "").strip().lower()
    return text if text in _CATEGORIES else "other"


def _safe_confidence(value, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(fallback or 0.0)
    return max(0.0, min(1.0, number))


def _needs_upgrade(current_category, rule_category) -> bool:
    """规则结论是否值得覆盖现存兜底行：分类变了，或规则给出了 trend/event。"""
    if str(rule_category or "") in ("trend", "event"):
        return True
    return str(current_category or "") != str(rule_category or "")


def _rule_classifier_version() -> str:
    try:
        from intel_classifier import CLASSIFIER_VERSION

        return str(CLASSIFIER_VERSION or _RULE_CLASSIFIER_VERSION)
    except Exception:
        return _RULE_CLASSIFIER_VERSION


def _explicit_packs(article_data: dict) -> list:
    out = []
    raw = article_data.get("industry_pack_ids")
    if isinstance(raw, (list, tuple)):
        out.extend(str(item).strip() for item in raw if str(item or "").strip())
    single = str(article_data.get("industry_pack_id") or "").strip()
    if single:
        out.append(single)
    return [p for p in dict.fromkeys(out) if p]


def _active_pack(db) -> str:
    try:
        with db.lock:
            row = db.connection.execute(
                "SELECT setting_value FROM intel_runtime_settings "
                "WHERE setting_key='active_industry_pack_id'"
            ).fetchone()
        return str((row[0] if row else "") or "").strip()
    except Exception:
        return ""


def _keyword_packs(article_data: dict) -> list:
    """按各行业包的核心/扩展关键词打分，返回 [(pack_id, 命中数, [命中词])] 降序。"""
    try:
        from industry_packs import industry_pack_loader
    except Exception:
        return []
    blob = " ".join(str(article_data.get(k) or "") for k in ("title", "matched_keywords"))
    blob = (blob + " " + str(article_data.get("content") or "")[:2000]).casefold()
    if not blob.strip():
        return []
    scored = []
    for pack in industry_pack_loader.list():
        pack_id = str(pack.get("id") or "")
        if not pack_id:
            continue
        words = [str(w) for w in (pack.get("core_keywords") or [])]
        words += [str(w) for w in (pack.get("expanded_keywords") or [])]
        hits = [w for w in words if w and w.casefold() in blob]
        if hits:
            scored.append((pack_id, len(hits), hits))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


def _pack_version(pack_id: str) -> str:
    """取该包当前版本号：article_intel_classifications.industry_pack_version 是 NOT NULL。"""
    try:
        from industry_packs import industry_pack_loader

        pack = industry_pack_loader.load(pack_id)
        return str(pack.get("pack_version") or "")
    except Exception:
        return ""


def _content_hash(article_data: dict) -> str:
    """article_intel_classifications.article_content_hash 是 NOT NULL，口径与
    IntelRepository.article_content_hash 保持一致（优先复用已算好的 hash）。"""
    existing = str(article_data.get("content_hash") or "").strip()
    if existing:
        return existing
    content = str(article_data.get("content") or "")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _rule_classification(pack_id: str, article_data: dict) -> dict:
    """用**纯规则**分类器（intel_classifier.classify_article）算出该包的分类结论。

    为什么必须用真实规则结论、不能硬编码 'other'：兜底行原先一律写 final_category='other'，
    文章永久停在「其他」（A 机 invest_mgmt 实测 448/860 行如此）。这里不传 LLM、
    不走 fuse_rule_and_llm——兜底链路必须纯规则、可离线复现、不产生任何模型调用。

    为什么必须写真实 score_details：问答检索 ArticleRetriever._rows() 有一道质量门
    （intel_topics._classification_admitted），要求 score_details 里有 anchor / core /
    expanded 命中。兜底归属若写空值，文章虽然"有归属行"，AI 依然检索不到，
    "页面看得到、AI 搜不到"的问题只是从"没有行"变成"有空行"。
    """
    try:
        from industry_packs import industry_pack_loader
        from intel_classifier import classify_article

        pack = industry_pack_loader.load(pack_id)
        payload = {
            "title": article_data.get("title") or "",
            "content": article_data.get("content") or "",
            "matched_keywords": article_data.get("matched_keywords") or "",
        }
        # 规则里的"近期兜底分类"（_recent_fallback_category）依赖发布时间：不传时间戳时，
        # 锚点达标但没有趋势/事件词的近期文章会永远落 other，与"用当前配置重跑"口径不一致。
        for key in ("publish_date", "first_crawled", "created_at"):
            value = article_data.get(key)
            if value:
                payload[key] = value
        return classify_article(payload, pack) or {}
    except Exception:
        return {}


def _rule_row_values(pack_id: str, article_data: dict, scored: dict, *, content_hash: str, now: str) -> dict:
    """把规则结论摊平成写库列值（INSERT 与存量修复共用同一口径）。"""
    rule_category = _safe_category(scored.get("rule_category") or scored.get("final_category"))
    final_category = _safe_category(scored.get("final_category") or rule_category)
    rule_confidence = _safe_confidence(scored.get("rule_confidence"))
    final_confidence = _safe_confidence(scored.get("final_confidence"), fallback=rule_confidence)
    rule_reason = str(scored.get("rule_reason") or "")
    final_reason = str(scored.get("final_reason") or rule_reason)
    tags = list(scored.get("topic_tags") or []) or _topic_tags(pack_id, article_data)
    return {
        "industry_pack_version": _pack_version(pack_id),
        "classifier_version": _rule_classifier_version(),
        "article_content_hash": content_hash,
        "rule_category": rule_category,
        "rule_confidence": rule_confidence,
        "rule_reason": rule_reason,
        "score_details_json": json.dumps(scored.get("score_details") or {}, ensure_ascii=False),
        "matched_keywords_json": json.dumps(list(scored.get("matched_keywords") or []), ensure_ascii=False),
        "topic_tags_json": json.dumps(tags, ensure_ascii=False),
        "final_category": final_category,
        "final_confidence": final_confidence,
        "final_reason": final_reason,
        "result_source": str(scored.get("result_source") or "rule"),
        "classified_at": now,
        "updated_at": now,
    }


def _admitted(scored: dict) -> bool:
    """复用检索质量门的判据（intel_topics._classification_admitted）。"""
    try:
        from intel_topics import _classification_admitted

        return bool(_classification_admitted(scored.get("score_details") or {}))
    except Exception:
        return False


def _topic_tags(pack_id: str, article_data: dict) -> list:
    """把文章归到该包设定主题/领域里最匹配的一个（命中即打标）。"""
    try:
        from industry_packs import industry_pack_loader

        pack = industry_pack_loader.load(pack_id)
    except Exception:
        return []
    blob = " ".join(str(article_data.get(k) or "") for k in ("title", "matched_keywords")
                    ).casefold()
    best, best_hits = "", 0
    for topic in (pack.get("fixed_topics") or []):
        words = [str(w) for w in (topic.get("keywords") or [])]
        hits = sum(1 for w in words if w and w.casefold() in blob)
        if hits > best_hits:
            best, best_hits = str(topic.get("name") or topic.get("key") or ""), hits
    return [best] if best else []


def ensure_pack_attribution(db, article_id: int, article_data: dict, *, pack_ids=None) -> dict:
    """保证文章至少有本包归属；兜底行按**纯规则**结论落库（不再硬编码「其他」）。

    Args:
        pack_ids: 显式指定归属包（存量补归属时由调用方给出候选准入包）；
            不传则按「显式字段 → 关键词打分 → 当前激活包」自行选择。
            文章已有兜底行时不看这个参数——只升级既有行，不外扩。

    返回 {attributed: [...], upgraded: [...], source: 'existing'|'existing_fallback'|
    'explicit'|'keyword'|'active'|'none'}。

    写入语义（只升级、不破坏）：
      * 没有任何分类行 → 新写一条规则结论（分类、置信度、命中词、打分证据都来自规则）；
      * 已有行且**不是**兜底来源 → 一行都不动（人工/规则/LLM 的真实结论优先）；
      * 已有行且**是**兜底来源（result_source='fallback_attribution' 或
        classifier_version='fallback-attribution-v1'）→ 允许被真实规则结论升级，
        并把 classifier_version/result_source 一并更新为规则来源。
    """
    result = {"attributed": [], "upgraded": [], "source": "none"}
    try:
        db._ensure_connection()
        with db.lock:
            rows = db.connection.execute(
                "SELECT industry_pack_id, result_source, classifier_version, final_category "
                "FROM article_intel_classifications WHERE article_id=?",
                (int(article_id),),
            ).fetchall()
        existing, fallback_packs, category_by_pack = [], [], {}
        for row in rows:
            pack_id = str(row[0] or "")
            if not pack_id:
                continue
            existing.append(pack_id)
            category_by_pack[pack_id] = str(row[3] or "")
            if _is_fallback_row(row[1], row[2]):
                fallback_packs.append(pack_id)
        if existing and not fallback_packs:
            # 已有真实分类行（人工/规则/LLM）→ 绝不覆盖，保持原有"存在即返回"语义。
            result["attributed"] = existing
            result["source"] = "existing"
            return result

        if fallback_packs:
            # 存量兜底行：候选包就是已存在兜底行的那些包，既不外扩也不新增包。
            packs, source = fallback_packs[:3], "existing_fallback"
        else:
            packs, source = [], ""
            forced = [str(p).strip() for p in (pack_ids or []) if str(p or "").strip()]
            explicit = forced or _explicit_packs(article_data)
            if explicit:
                packs, source = explicit[:3], "explicit"
            else:
                scored_packs = _keyword_packs(article_data)
                strong = [p for p, n, _hits in scored_packs if n >= _MIN_KEYWORD_HITS]
                if strong:
                    packs, source = strong[:2], "keyword"
                else:
                    active = _active_pack(db)
                    if active:
                        packs, source = [active], "active"
        if not packs:
            return result

        now = _utc_now()
        content_hash = _content_hash(article_data)
        # 真实打分放到锁外先算好：classify_article 是纯函数但耗 CPU，
        # 不要在持有写锁时跑全量关键词匹配。
        scored_by_pack = {pack_id: _rule_classification(pack_id, article_data) for pack_id in packs}
        # 关键词粗筛会命中"扩展词"，但真实分类器要求行业锚点才算相关。若某个候选包
        # 拿不出可准入的真实证据，就不要把文章挂到它下面（那只是给别的包添噪声）；
        # 只有在所有候选都拿不到证据时，才保留第一个候选保底归属。
        # 显式指定包不受此影响——那是入库方/编辑者的明确选择。
        # 存量兜底行的升级更严：拿不出证据就什么都不写（它已经有一条归属了，
        # 没有理由把别的包的结论盖上去）。
        if source == "existing_fallback":
            packs = [p for p in packs if _admitted(scored_by_pack.get(p) or {})]
            if not packs:
                result["source"] = source
                return result
        elif source != "explicit":
            backed = [p for p in packs if _admitted(scored_by_pack.get(p) or {})]
            packs = backed or packs[:1]

        with db.lock:
            cursor = db.connection.cursor()
            for pack_id in packs:
                scored = scored_by_pack.get(pack_id) or {}
                values = _rule_row_values(
                    pack_id, article_data, scored,
                    content_hash=content_hash, now=now,
                )
                if source == "existing_fallback" and not _needs_upgrade(
                    category_by_pack.get(pack_id), values["final_category"]
                ):
                    # 规则结论与现值一致（且不是趋势/事件）→ 不动这一行，保持逐行幂等。
                    continue
                try:
                    cursor.execute(
                        _INSERT_SQL,
                        (
                            int(article_id), str(pack_id), "",
                        ) + tuple(values[column] for column in _RULE_VALUE_COLUMNS) + (now,),
                    )
                    result["attributed"].append(pack_id)
                    if source == "existing_fallback":
                        result["upgraded"].append(pack_id)
                except Exception as exc:
                    print("⚠️ 兜底归属写入失败 article=%s pack=%s: %s"
                          % (article_id, pack_id, str(exc)[:160]))
            db.connection.commit()
            cursor.close()
        result["source"] = source
    except Exception as exc:
        print("⚠️ 归属兜底异常 article=%s: %s" % (article_id, str(exc)[:120]))
    return result


def _row_dict(cursor, row) -> dict:
    """把一行结果转成 dict（SQLite 的 Row 与 PG 兼容层都能用）。"""
    names = [str(column[0]) for column in (getattr(cursor, "description", None) or [])]
    try:
        return {name: row[name] for name in names}
    except Exception:
        return dict(zip(names, row))


def repair_fallback_attributions(pack_id=None, *, dry_run=True, limit=500, backup_dir=None) -> dict:
    """存量修复：把兜底归属行按**纯规则**重跑，只升级不破坏。

    Args:
        pack_id: 只修某个行业包；不给则扫全库。
        dry_run: 默认 True —— 一个字节都不写库，只返回"会改哪些行"的预估。
        limit: 单次最多扫描多少条兜底行（默认 500，避免一次性锁库过久）。
        backup_dir: 非 dry_run 时备份 JSON 的落盘目录；默认仓库 data/。

    行为：
      * 只认兜底行（result_source='fallback_attribution' 或
        classifier_version='fallback-attribution-v1'）；
      * 用 intel_classifier.classify_article（纯规则，不调 LLM）重跑；
      * **只有**规则判定与现值不同、或规则判为 trend/event 时才更新——逐行幂等，
        所以第二次运行 upgraded=0；
      * 更新时把 classifier_version/result_source 换成规则来源，
        这些行因此不再被本函数选中，天然幂等；
      * dry_run=False 时先把**将被改动的行**（整行、全列）备份成 JSON，返回 backup_path；
        按备份里的 columns/rows 逐行 UPDATE 回去即可回滚；
      * 只对 articles 表做 SELECT（读正文用来重跑规则），绝不修改 articles、
        不改行业包配置、不激活版本、不发任何网络请求。

    返回::

        dry_run=True:  {"scanned": n, "upgraded": n, "unchanged": n,
                        "samples": [...], "dry_run": True}
        dry_run=False: 上述键 + {"backup_path": "...", "backup_rows": n,
                        "rollback": "..."}
    """
    result = {
        "scanned": 0,
        "upgraded": 0,
        "unchanged": 0,
        "samples": [],
        "dry_run": bool(dry_run),
    }
    try:
        from sqlite_database import sqlite_db

        db = sqlite_db
    except Exception as exc:
        result["error"] = "无法获取数据库实例: %s" % str(exc)[:120]
        return result

    samples: list = []
    try:
        db._ensure_connection()
        filters = [
            "(c.result_source = ? OR c.classifier_version = ?)",
        ]
        params: list = [_FALLBACK_SOURCE, _FALLBACK_CLASSIFIER_VERSION]
        if str(pack_id or "").strip():
            filters.append("c.industry_pack_id = ?")
            params.append(str(pack_id).strip())
        sql = (
            "SELECT c.id, c.article_id, c.industry_pack_id, c.final_category, "
            "       a.title, a.content, a.matched_keywords, a.publish_date, "
            "       a.first_crawled, a.created_at, a.content_hash "
            "  FROM article_intel_classifications c "
            "  LEFT JOIN articles a ON a.id = c.article_id "
            " WHERE " + " AND ".join(filters) + " "
            " ORDER BY c.id LIMIT ?"
        )
        params.append(max(1, int(limit or 1)))
        with db.lock:
            rows = db.connection.execute(sql, tuple(params)).fetchall()

        result["scanned"] = len(rows)
        pending = []          # [(row_id, pack_id, article_data, values_dict)]
        content_hash_cache: dict = {}
        for row in rows:
            row_id = int(row[0])
            article_id = int(row[1] or 0)
            article_pack_id = str(row[2] or "")
            current_category = str(row[3] or "")
            cache_key = article_id
            if cache_key not in content_hash_cache:
                content_hash_cache[cache_key] = {
                    "title": row[4] or "",
                    "content": row[5] or "",
                    "matched_keywords": row[6] or "",
                    "publish_date": row[7] or "",
                    "first_crawled": row[8] or "",
                    "created_at": row[9] or "",
                    "content_hash": row[10] or "",
                }
            article_data = content_hash_cache[cache_key]
            scored = _rule_classification(article_pack_id, article_data)
            values = _rule_row_values(
                article_pack_id, article_data, scored,
                content_hash=_content_hash(article_data), now=_utc_now(),
            )
            if not _needs_upgrade(current_category, values["final_category"]):
                result["unchanged"] += 1
                continue
            pending.append((row_id, article_pack_id, article_data, values))
            if len(samples) < 5:
                samples.append({
                    "id": row_id,
                    "article_id": article_id,
                    "industry_pack_id": article_pack_id,
                    "title": str(article_data.get("title") or "")[:40],
                    "from_category": current_category,
                    "to_category": values["final_category"],
                    "from_result_source": _FALLBACK_SOURCE,
                    "to_result_source": values["result_source"],
                    "final_reason": values["final_reason"][:80],
                })

        result["upgraded"] = len(pending)
        result["samples"] = samples
        if dry_run:
            return result
        if not pending:
            # 没有要改的行 → 不生成备份文件（也就没有需要回滚的东西）。
            return result

        backup_path = ""
        timestamp = _utc_stamp()
        target_dir = str(backup_dir or "").strip() or _default_backup_dir()
        os.makedirs(target_dir, exist_ok=True)
        pending_ids = [item[0] for item in pending]
        with db.lock:
            cursor = db.connection.cursor()
            placeholders = ", ".join("?" for _ in pending_ids)
            cursor.execute(
                "SELECT * FROM article_intel_classifications WHERE id IN (%s)" % placeholders,
                tuple(pending_ids),
            )
            backup_rows = sorted(
                (_row_dict(cursor, row) for row in cursor.fetchall()),
                key=lambda item: int(item.get("id") or 0),
            )
            columns = sorted(
                {key for row in backup_rows for key in row.keys()},
            )
            cursor.close()
        backup_path = os.path.join(
            target_dir, "fallback_attribution_backup_%s.json" % timestamp
        )
        rollback_hint = (
            "回滚：读本文件的 columns 与 rows，对每条记录执行 "
            "UPDATE article_intel_classifications SET <column>=? ... WHERE id=? 即可"
            "（rows 是修改前的整行快照，只涉及 article_intel_classifications，"
            "articles 表未被修改）。"
        )
        with open(backup_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "version": 1,
                    "kind": "fallback_attribution_repair",
                    "created_at": _utc_now(),
                    "table": "article_intel_classifications",
                    "key_column": "id",
                    "pack_id": str(pack_id or ""),
                    "columns": columns,
                    "rows": backup_rows,
                    "rollback": rollback_hint,
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )
        result["backup_path"] = backup_path
        result["backup_rows"] = len(backup_rows)
        result["rollback"] = rollback_hint

        written = 0
        with db.lock:
            cursor = db.connection.cursor()
            for row_id, _article_pack_id, _article_data, values in pending:
                cursor.execute(
                    _UPDATE_SQL,
                    tuple(values[column] for column in _RULE_VALUE_COLUMNS) + (int(row_id),),
                )
                written += 1
            db.connection.commit()
            cursor.close()
        result["upgraded"] = written
    except Exception as exc:
        result["error"] = str(exc)[:200]
        print("⚠️ 存量兜底归属修复异常: %s" % str(exc)[:160])
    return result


__all__ = ["ensure_pack_attribution", "repair_fallback_attributions"]
