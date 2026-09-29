#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""主体话题归并（T5）：高置信度规则（稳定、无波动）+ LLM 长尾（自动）。

纯 LLM 归并在话题级边界会摇摆（"国家税务总局 vs 税务部门"时合时不合），
违背主体一致性。改为：高置信度关键词规则锁死最该合的（摩根/税务/金融监管
等确定性归并），LLM 只处理规则未覆盖的长尾。规则几条、维护量小，兼顾
"稳定一致"与"减少人为"。
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

import config
from intel_database import IntelRepository
from intel_llm_client import intel_llm_client
from industry_pack_runtime import active_industry_composition_service

logger = logging.getLogger(__name__)

# 高置信度规则：关键词正则 → 话题级 canonical（确定性，无 LLM 波动）
# 命中即归并，覆盖最该合的常见主体；未命中的长尾交给 LLM
SUBJECT_RULES: List = [
    (re.compile(r"摩根|morgan|j\.?\s*p\.?\s*morgan", re.I), "摩根大通"),
    (re.compile(r"税务|税务总局|税务局|税务部门|征税", re.I), "税务"),
    (re.compile(r"证监|银监|保监|金融监管|监管当局|外汇管理", re.I), "金融监管"),
    (re.compile(r"瑞银|ubs\b", re.I), "瑞银"),
    (re.compile(r"阿尔诺|arnault", re.I), "阿尔诺"),
    (re.compile(r"贝佐斯|贝索斯|bezos", re.I), "贝佐斯"),
    (re.compile(r"普莱姆基|premji", re.I), "普莱姆基家族办公室"),
    (re.compile(r"弗里德金|friedkin", re.I), "弗里德金"),
]


def _rule_normalize(subject: str) -> Optional[str]:
    """规则归并：命中返回 canonical，未命中返回 None。"""
    s = str(subject or "")
    for pattern, canonical in SUBJECT_RULES:
        if pattern.search(s):
            return canonical
    return None


def _norm_key(s: str) -> str:
    s = str(s or "").lower()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[（(].*?[)）]", "", s)
    s = re.sub(r"[^一-鿿a-z0-9]", "", s)
    return s


class SubjectNormalizeService:
    def __init__(self, repository: IntelRepository = None, composition=None):
        self.repository = repository or IntelRepository()
        self.composition = composition or active_industry_composition_service

    def run(self, *, pack_id: str = "", batch_size: int = 8) -> Dict:
        """规则归并（稳定）+ LLM 长尾归并（自动），写 canonical 表。"""
        pack_id = str(pack_id or "").strip()
        if not pack_id:
            try:
                pack_id = str(self.composition.snapshot().get("active_industry_pack_id") or "")
            except Exception:
                pack_id = ""

        subjects = self.repository.list_event_subjects(pack_id=pack_id)
        if not subjects:
            logger.info("subject_normalize: pack=%s 无 subject", pack_id or "*")
            return {"pack_id": pack_id, "subjects": 0, "canonicals": 0}
        subject_texts = [s["subject"] for s in subjects]

        # 1. 高置信度规则归并（确定性，消除 LLM 波动）
        rule_canonical: Dict[str, str] = {}
        llm_subjects: List[str] = []
        for s in subject_texts:
            canon = _rule_normalize(s)
            if canon:
                rule_canonical[s] = canon
            else:
                llm_subjects.append(s)

        # 2. LLM 归并长尾（规则未覆盖）
        llm_canonical: Dict[str, str] = {}
        batch_size = max(4, min(int(batch_size), 20))
        for i in range(0, len(llm_subjects), batch_size):
            batch = llm_subjects[i:i + batch_size]
            try:
                llm_canonical.update(intel_llm_client.normalize_subjects(batch))
            except Exception as exc:
                logger.warning("subject_normalize LLM batch %d 失败: %s", i, exc)
                for s in batch:
                    llm_canonical.setdefault(s, s)

        all_canonical = {**llm_canonical, **rule_canonical}
        for s in subject_texts:
            all_canonical.setdefault(s, s)
        mappings = [
            (s, all_canonical.get(s, s), _norm_key(all_canonical.get(s, s)))
            for s in subject_texts
        ]
        written = self.repository.save_subject_canonical(
            industry_pack_id=pack_id, mappings=mappings
        )
        distinct = len(set(all_canonical.values()))
        logger.info(
            "subject_normalize: pack=%s subjects=%d 规则覆盖=%d LLM长尾=%d → canonical=%d",
            pack_id or "*", len(subject_texts), len(rule_canonical), len(llm_subjects), distinct,
        )
        return {
            "pack_id": pack_id,
            "subjects": len(subject_texts),
            "rule_covered": len(rule_canonical),
            "llm_tail": len(llm_subjects),
            "canonicals": distinct,
            "written": written,
        }
