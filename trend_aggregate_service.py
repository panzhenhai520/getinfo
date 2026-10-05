#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""趋势聚合服务：拉激活 pack 的趋势关键词命中 → 按天序列 → 爆发/状态 → 落库。

第一阶段 baseline：关键词来自 article_intel_classifications.score_details_json
的 $.hits.trend（分类器逐词命中），无需 LLM。聚合在 SQL 层用 json_each 完成，
本服务负责把"关键词×天"补成完整序列、调 trend_detect 计算状态、UPSERT 入库。
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Dict, List

import config
from intel_database import IntelRepository
from industry_pack_runtime import active_industry_composition_service
from trend_detect import analyze, apply_bh_correction
from utils import get_china_time

logger = logging.getLogger(__name__)


def _resolve_llm(pack_id: str = '') -> tuple[str, str]:
    """优先用行业包『运行配置』的 LLM base_url/模型；未配置则回退全局 .env / 默认。"""
    try:
        from pack_tenant import pack_runtime
        rt = pack_runtime(pack_id)
        return rt['llm_base_url'], rt['llm_model']
    except Exception:
        pass
    base = str(getattr(config, "INTEL_LLM_BASE_URL", "") or "").strip().rstrip("/") or "http://10.88.0.1:8081/v1"
    model = str(getattr(config, "INTEL_LLM_MODEL", "") or "").strip() or "deepseek-v4-flash"
    return base, model


def _llm_brand_merge(brands, pack_id: str = ''):
    """LLM 品牌消融（第二层）：识别同一品牌不同写法（中英/简称/变体），返回归并映射。失败返回空。
    LLM 地址优先用行业包『分布式服务设置』的 llm_base_url/llm_model。"""
    import re
    import json
    import requests
    brands = sorted(set(b for b in brands if b))
    if len(brands) < 2:
        return {}
    brand_list = "\n".join(f"- {b}" for b in brands)
    prompt = (
        "以下是行业品牌关键词列表，请识别哪些是同一个品牌/实体的不同写法。\n\n"
        f"{brand_list}\n\n"
        "规则：同一实体不同写法才合并（如 UBS/瑞银、Goldman Sachs/高盛、"
        "摩根/摩根大通/J.P. Morgan 是同一家）；不同实体绝不合并"
        "（摩根大通≠摩根士丹利、BlackRock≠Blackstone）；"
        "每组合并到一个核心名（优先中文常用名）。\n"
        '只输出 JSON：{"变体名": "核心名", ...}，无归并输出 {}'
    )
    base_url, model = _resolve_llm(pack_id)
    try:
        r = requests.post(
            f"{base_url}/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 1200, "temperature": 0},
            timeout=40,
        )
        r.raise_for_status()
        content = str(r.json()["choices"][0]["message"].get("content") or "")
        # 推理模型可能带思考链；提取最后一个 JSON 对象
        m = re.findall(r"\{[^{}]*\}", content, re.S)
        if not m:
            return {}
        merge = json.loads(m[-1])
        bset = set(brands)
        return {k: v for k, v in merge.items() if k in bset and v in bset and k != v}
    except Exception as e:
        logger.warning("LLM 品牌消融失败: %s", e)
        return {}


class TrendAggregateService:
    def __init__(self, repository: IntelRepository = None, composition=None):
        self.repository = repository or IntelRepository()
        self.composition = composition or active_industry_composition_service

    def run(
        self,
        *,
        pack_id: str = "",
        days_back: int = None,
        window: int = None,
        dimension: str = "trend_keyword",
    ) -> Dict:
        """对激活 pack 跑一次趋势聚合并落库。返回统计摘要。"""
        snap = self.composition.snapshot()
        pack_id = str(pack_id or snap.get("active_industry_pack_id") or "").strip()
        activation_id = str(snap.get("active_industry_activation_id") or "")
        days_back = int(
            days_back
            if days_back is not None
            else getattr(config, "INTEL_TREND_WINDOW_DAYS", 90)
        )
        window = int(
            window if window is not None else getattr(config, "INTEL_TREND_BURST_WINDOW", 7)
        )

        # 1. SQL 层聚合 (day, keyword) → 计数 + 独立来源数
        hits_field = "$.hits.brand" if dimension == "brand" else "$.hits.trend"
        try:
            raw = self.repository.aggregate_trend_keyword_daily(
                industry_pack_id=pack_id, days_back=days_back, hits_field=hits_field
            )
        except TypeError as exc:
            # Backward-compatible repository/test doubles created before the
            # brand dimension added hits_field.
            if "hits_field" not in str(exc):
                raise
            raw = self.repository.aggregate_trend_keyword_daily(
                industry_pack_id=pack_id, days_back=days_back
            )
        # 品牌消融：前缀算法 + LLM 识别（不硬编码，自动归并同一品牌不同写法）
        _all_kw = {str(r.get("keyword") or "").strip() for r in raw}
        _all_kw = {k for k in _all_kw if k}
        brand_merge: Dict[str, str] = {}
        if dimension == "brand":
            # 第一层：前缀归并（快，核心词+后缀变体，如 时和/时和家办→时和）
            for kw in sorted(_all_kw, key=len, reverse=True):
                for core in sorted(_all_kw, key=len):
                    if len(core) < len(kw) and kw.startswith(core):
                        brand_merge[kw] = core
                        break
            # 第二层：LLM 消融（更强，中英/简称/变体，如 UBS/瑞银）
            # 用并查集合并两层映射：避免前缀层与 LLM 层方向相反时（如 前缀:摩根大通→摩根，
            # LLM:摩根→摩根大通）互相抵消留下两条线；每个集合选最短名作核心。
            _parent = {k: k for k in _all_kw}

            def _find(x):
                while _parent[x] != x:
                    _parent[x] = _parent[_parent[x]]
                    x = _parent[x]
                return x

            for _a, _b in list(brand_merge.items()) + list(_llm_brand_merge(_all_kw, pack_id).items()):
                if _a not in _parent or _b not in _parent:
                    continue
                _ra, _rb = _find(_a), _find(_b)
                if _ra != _rb:
                    if len(_ra) <= len(_rb):
                        _parent[_rb] = _ra
                    else:
                        _parent[_ra] = _rb
            brand_merge = {k: _find(k) for k in _parent if _find(k) != k}
        per_kw: Dict[str, Dict[str, Dict]] = {}
        for r in raw:
            kw = str(r.get("keyword") or "").strip()
            kw = brand_merge.get(kw, kw)
            if not kw:
                continue
            # 兼容层日期：SQL 层 date() 在 Postgres 返回 datetime.date 对象，
            # 而下方 days_sorted 是 ISO 字符串。统一用字符串作 key，避免
            # daymap.get(字符串) 永远 miss，导致所有 article_count 被补成 0、
            # 趋势状态恒为 EMERGING。
            day_key = str(r["day"])[:10]
            per_kw.setdefault(kw, {})[day_key] = {
                "count": int(r.get("article_count") or 0),
                "sources": int(r.get("distinct_source_count") or 0),
            }
        if not per_kw:
            logger.info("trend_aggregate: pack=%s 无命中数据，跳过", pack_id)
            return {"pack_id": pack_id, "dimension": dimension, "keywords": 0, "rows": 0}

        # 2. 完整日期序列（缺失天补 0），保证每关键词序列等长且含当天
        today = get_china_time().date()
        days_sorted = sorted(
            (today - timedelta(days=i)).isoformat() for i in range(days_back)
        )

        # 3. 每关键词算状态、生成行
        rows: List[Dict] = []
        state_summary: Dict[str, str] = {}
        analyses: Dict[str, Dict] = {}
        for kw, daymap in per_kw.items():
            series = [daymap.get(d, {}).get("count", 0) for d in days_sorted]
            analyses[kw] = analyze(series, window)
        apply_bh_correction(list(analyses.values()))
        for kw, daymap in per_kw.items():
            analysis = analyses[kw]
            state_summary[kw] = analysis["state"]
            for d in days_sorted:
                info = daymap.get(d, {"count": 0, "sources": 0})
                rows.append(
                    {
                        "bucket_date": d,
                        "keyword": kw,
                        "article_count": info["count"],
                        "distinct_source_count": info["sources"],
                        "is_burst": 1 if analysis["is_burst"] else 0,
                        "burst_score": analysis["burst_score"],
                        "state": analysis["state"],
                        "trend_test_method": analysis["trend_test_method"],
                        "pettitt_p": analysis["pettitt_p"],
                        "pettitt_q": analysis["pettitt_q"],
                        "delta_BIC": analysis["delta_BIC"],
                        "change_point_index": analysis["change_point_index"],
                        "taskshift_emerging": analysis["taskshift_emerging"],
                        "taskshift_high_confidence": analysis["taskshift_high_confidence"],
                    }
                )

        # 4. 先清该维度旧趋势（品牌归并/关键词变化后旧行残留），再全量写入
        repository_db = getattr(self.repository, "db", None)
        if repository_db is not None:
            with repository_db.lock:
                _cur = repository_db.connection.cursor()
                try:
                    _cur.execute(
                        "DELETE FROM intel_topic_trends WHERE industry_pack_id=? AND dimension=?",
                        (pack_id, dimension),
                    )
                    repository_db.connection.commit()
                finally:
                    _cur.close()
        written = self.repository.upsert_topic_trend_rows(
            industry_pack_id=pack_id,
            dimension=dimension,
            activation_id=activation_id,
            rows=rows,
        )
        bursts = sum(1 for s in state_summary.values() if s == "BURSTING")
        logger.info(
            "trend_aggregate: pack=%s keywords=%d rows=%d bursts=%d",
            pack_id, len(per_kw), written, bursts,
        )
        return {
            "pack_id": pack_id,
            "dimension": dimension,
            "keywords": len(per_kw),
            "rows": written,
            "bursts": bursts,
            "states": state_summary,
        }
