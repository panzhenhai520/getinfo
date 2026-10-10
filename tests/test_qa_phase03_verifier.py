#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 03 · 核验层单测（P03-01…P03-04，纯规则、不调模型）。

覆盖四件事，每条都钉"**证据不足不许默认 SUPPORTED**"（MASTER_RULES 第 11 条）：
  · P03-01 relevance/reranker：标题/正文覆盖度打分、稳定重排；
  · P03-02 entailment/NLI：规则后端的方向性覆盖度、可插拔后端与**保守回落**；
  · P03-03 entity/time/negation/source：否定翻转（反证）、数字不符、实体缺失、
            时间不适用、二手转述/因果夸大降级；
  · P03-04 EvidenceScore/reason/cache：加权分口径、原因码中文解释、缓存命中与失效。

隔离：`DATABASE_TYPE=sqlite` + 临时库；核验层本身**不碰数据库**（缓存层用内存实例），
所以这里的用例连库都不用连。
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_verifier as verifier  # noqa: E402
from qa_evidence import annotate_evidence  # noqa: E402
from qa_graph_contracts import EVIDENCE_STATUSES  # noqa: E402

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
SIX_MONTHS = (NOW - timedelta(days=180)).date().isoformat()


def _item(content, *, title="", published="2026-10-01", authority=60, metadata=None,
          relationship="supports", ref="article:1", source_url="https://example.com/a"):
    return {
        "evidence_ref": ref,
        "source_type": "article",
        "title": title or content[:24],
        "source_url": source_url,
        "article_id": 1,
        "content_excerpt": content,
        "published_at": published,
        "authority_level": authority,
        "score": 30.0,
        "retrieval_method": "keyword",
        "relationship": relationship,
        "metadata": dict(metadata or {}),
    }


class _Base(unittest.TestCase):
    def setUp(self):
        # 每个用例一个独立缓存实例：避免用例之间互相命中（缓存键相同=输入相同）
        self.cache = verifier.VerificationCache()

    def verify(self, item, claim, **kwargs):
        kwargs.setdefault("cache", self.cache)
        kwargs.setdefault("now", NOW)
        return verifier.verify_evidence_item(item, claim_text=claim, **kwargs)


# ── P03-01 relevance / reranker ──────────────────────────────────────────────

class RelevanceTests(_Base):
    def test_title_hit_scores_above_body_only(self):
        claim = "宁德时代股价大涨的原因"
        terms = verifier.term_set(claim)
        title_hit = verifier.relevance_score(
            terms, _item("本文是一篇与该公司无关的行业综述，讨论供应链与库存周期。",
                         title="宁德时代股价大涨原因解析"))
        body_hit = verifier.relevance_score(
            terms, _item("宁德时代股价大涨的原因与固态电池量产有关。", title="行业周报"))
        self.assertGreater(title_hit["relevance"], body_hit["relevance"])
        for value in (title_hit, body_hit):
            self.assertGreaterEqual(value["relevance"], 0.0)
            self.assertLessEqual(value["relevance"], 1.0)

    def test_no_terms_is_zero_not_crash(self):
        self.assertEqual(verifier.relevance_score(set(), _item("随便什么正文"))["relevance"], 0.0)

    def test_kb_similarity_in_unit_range_is_used(self):
        claim = "量子计算对金融风控的影响"
        item = _item("完全不相干的正文。", title="无关")
        item["score"] = 0.88
        self.assertGreaterEqual(verifier.relevance_score(verifier.term_set(claim), item)["relevance"], 0.88)
        # >1 的是排序分不是相似度，不许当相似度用
        item["score"] = 88.0
        self.assertLess(verifier.relevance_score(verifier.term_set(claim), item)["relevance"], 0.5)

    def test_rerank_is_stable_and_orders_by_score(self):
        items = [{"evidence_ref": "a", "metadata": {"evidence_layer": {"verification": {"score": 0.2}}}},
                 {"evidence_ref": "b", "metadata": {"evidence_layer": {"verification": {"score": 0.9}}}},
                 {"evidence_ref": "c", "metadata": {"evidence_layer": {"verification": {"score": 0.2}}}}]
        ordered, audit = verifier.rerank_evidence(items)
        self.assertEqual([row["evidence_ref"] for row in ordered], ["b", "a", "c"], "同分必须保持原顺序")
        self.assertEqual(audit["reordered"], 2)

    def test_rerank_without_scores_keeps_order(self):
        items = [{"evidence_ref": "a"}, {"evidence_ref": "b"}]
        ordered, _audit = verifier.rerank_evidence(items)
        self.assertEqual([row["evidence_ref"] for row in ordered], ["a", "b"])


# ── P03-02 entailment / NLI ──────────────────────────────────────────────────

class EntailmentTests(_Base):
    def test_support_when_span_covers_claim(self):
        result = self.verify(_item("10月9日，宁德时代股价大涨5.2%，主要因为固态电池量产消息与储能订单增长。"),
                             "宁德时代10月9日股价大涨，原因是固态电池量产消息与储能订单增长")
        self.assertEqual(result["verdict"], "SUPPORTED")
        self.assertTrue(result["verified"])
        self.assertIn(verifier.REASON_SUPPORTED, result["reasons"])
        self.assertGreater(result["dimensions"]["entailment"], verifier.support_min())

    def test_low_overlap_is_unverified_not_supported(self):
        """核心守门（MASTER_RULES 第 11 条）：只有关键词像，绝不许判 SUPPORTED。"""
        result = self.verify(_item("10月9日，比亚迪销量创新高，芯片供应紧张，行业整体承压。",
                                   title="宁德时代与比亚迪的差距"),
                             "宁德时代股价大涨的原因")
        self.assertEqual(result["verdict"], "UNVERIFIED")
        self.assertFalse(result["verified"])
        self.assertIn(verifier.REASON_KEYWORD_ONLY, result["reasons"])

    def test_partial_overlap_is_qualified(self):
        result = self.verify(_item("宁德时代股价大涨5%，同期储能板块整体走强，行业景气度回升。"),
                             "宁德时代股价大涨是因为固态电池量产，储能订单增长")
        self.assertEqual(result["verdict"], "QUALIFIED")
        self.assertIn(verifier.REASON_PARTIAL, result["reasons"])

    def test_short_claim_cannot_be_fully_supported(self):
        """实词太少的结论没有证据力：最多判部分支持。"""
        result = self.verify(_item("稀土价格上涨。"), "稀土")
        self.assertNotEqual(result["verdict"], "SUPPORTED")

    def test_empty_claim_or_span(self):
        self.assertEqual(self.verify(_item("有正文"), "")["verdict"], "UNVERIFIED")
        blank = _item("")
        result = verifier.verify_evidence_item(blank, claim_text="有结论", cache=self.cache, now=NOW)
        self.assertEqual(result["verdict"], "UNVERIFIED")

    def test_unknown_backend_falls_back_and_cannot_fully_support(self):
        os.environ["QA_NLI_BACKEND"] = "http://gpu-box:8000/nli"  # 故意写个"像端点"的名字
        try:
            self.assertEqual(verifier.entailment_backend_name(), "rule",
                             "未注册的后端名必须回落到规则后端（不许联网）")
            result = self.verify(_item("10月9日，宁德时代股价大涨5.2%，因为固态电池量产消息。"),
                                 "宁德时代10月9日股价大涨，因为固态电池量产")
            self.assertIn(verifier.REASON_BACKEND_FALLBACK, result["reasons"])
            self.assertNotEqual(result["verdict"], "SUPPORTED")
        finally:
            os.environ.pop("QA_NLI_BACKEND", None)

    def test_registered_backend_is_used_and_failure_is_conservative(self):
        calls = []

        def fake(claim, span):
            calls.append((claim, span))
            return {"label": "SUPPORTED", "entailment": 0.99}

        verifier.register_entailment_backend("unit-fake", fake)
        os.environ["QA_NLI_BACKEND"] = "unit-fake"
        try:
            result = self.verify(_item("宁德时代股价大涨5%。"), "宁德时代股价大涨")
            self.assertEqual(result["nli"]["backend"], "unit-fake")
            self.assertTrue(calls)
        finally:
            os.environ.pop("QA_NLI_BACKEND", None)

        def broken(claim, span):
            raise RuntimeError("backend boom")

        verifier.register_entailment_backend("unit-broken", broken)
        os.environ["QA_NLI_BACKEND"] = "unit-broken"
        try:
            result = self.verify(_item("宁德时代股价大涨5%，因为固态电池量产。"),
                                 "宁德时代股价大涨，因为固态电池量产")
            self.assertIn(verifier.REASON_BACKEND_FALLBACK, result["reasons"])
            self.assertNotEqual(result["verdict"], "SUPPORTED", "后端坏掉时不许判满支持")
        finally:
            os.environ.pop("QA_NLI_BACKEND", None)

    def test_module_never_imports_network_facilities(self):
        """源码级守门：核验层不许有任何联网设施（GPU 端点被明令停用）。

        用 AST 查 import 与字符串字面量，而不是查文本——文档里会提到"不 import requests"，
        纯文本匹配会误伤自己的说明文字。
        """
        import ast

        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "qa_verifier.py")
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        modules, roots = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name)
                    roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
                roots.add(node.module.split(".")[0])
        forbidden = {"requests", "urllib3", "httpx", "aiohttp", "socket", "openai", "anthropic"}
        self.assertFalse(roots & forbidden, "核验层不许 import 联网设施：%s" % sorted(roots & forbidden))
        self.assertNotIn("urllib.request", modules, "核验层不许 import urllib.request")
        urls = [node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and ("http://" in node.value or "https://" in node.value)]
        self.assertFalse(urls, "核验层不许出现网络地址字面量：%s" % urls[:3])
        self.assertIn("urllib.parse", modules, "域名解析只用标准库的 urllib.parse")


# ── P03-03 entity / time / negation / source ─────────────────────────────────

class ChecksTests(_Base):
    def test_negation_flip_is_refuted(self):
        result = self.verify(_item("医保新规适用于民营医院，报销比例下调。"),
                             "医保新规不适用于民营医院")
        self.assertEqual(result["verdict"], "REFUTED")
        self.assertIn(verifier.REASON_NEGATION_FLIP, result["reasons"])
        self.assertGreater(result["dimensions"]["contradiction_risk"], 0.5)

    def test_negation_mismatch_without_overlap_is_not_refuted(self):
        """只是碰巧一处有否定词、两边根本不在说同一件事：不许冤枉成反证。"""
        result = self.verify(_item("医保新规下调了民营医院的报销比例。"),
                             "量子计算不适用于金融风控")
        self.assertEqual(result["verdict"], "UNVERIFIED")
        self.assertNotIn(verifier.REASON_NEGATION_FLIP, result["reasons"])
        self.assertIn(verifier.REASON_NEGATION_UNSCOPED, result["reasons"])
        # 另一种情况：共享词落在否定窗口里（"适用"），但覆盖度根本不够 → 仍然只判未核验
        weak = self.verify(_item("医保新规适用于民营医院，报销比例下调，结算方式调整。"),
                           "量子计算不适用于金融风控")
        self.assertEqual(weak["verdict"], "UNVERIFIED")
        self.assertNotIn(verifier.REASON_NEGATION_FLIP, weak["reasons"])

    def test_procedural_negation_clause_does_not_refute(self):
        """真机踩到的坑：新规原文末尾"逾期不予受理"这种程序性子句，不许把支持的证据判成反证。"""
        claim = "2026年10月出台的新规要求民营医院在30日内完成备案"
        result = self.verify(_item(claim + "，逾期不予受理。"), claim)
        self.assertEqual(result["verdict"], "SUPPORTED")

    def test_unrelated_negation_in_long_text_does_not_refute(self):
        """长文里出现"未来/无法"这类词，不许把整条证据判成含否定（真机 8 条 claim 里 6 条被误判）。"""
        claim = "宁德时代10月9日股价大涨，收盘涨约3.84%"
        evidence = ("10月9日，宁德时代迎来久违的大涨，A股盘中一度涨超5％，截至收盘，仍涨约3.84％。"
                    "此前近两个月，宁德时代股价持续走低。分析师认为车企自研电池短期内无法替代头部供应商。")
        self.assertEqual(self.verify(_item(evidence), claim)["verdict"], "SUPPORTED")

    def test_negation_polarity_is_judged_on_focus_clause(self):
        check = verifier.check_negation("该政策适用于新能源企业", "该政策适用于新能源企业，逾期不予受理。")
        self.assertTrue(check["passed"])
        self.assertFalse(check["claim_negated"])

    def test_number_mismatch_caps_to_unverified(self):
        result = self.verify(_item("2025年新能源补贴提高到20%，覆盖范围扩大。"),
                             "2025年新能源补贴提高到30%")
        self.assertEqual(result["verdict"], "UNVERIFIED")
        self.assertIn(verifier.REASON_NUMBER_MISMATCH, result["reasons"])

    def test_number_check_normalizes_dates_and_spaces(self):
        self.assertTrue(verifier.check_numbers("2026年10月9日大涨", "2026-10-09 大涨")["passed"])
        self.assertTrue(verifier.check_numbers("补贴提高到30%", "补贴提高到30 %")["passed"])
        self.assertFalse(verifier.check_numbers("补贴提高到30%", "补贴提高到20%")["passed"])
        # 与 qa_reasoning 的口径差异（汉字不当词边界）：必须查得出来
        import qa_reasoning

        text = "提高到30%"
        self.assertEqual(qa_reasoning._NUMBER_RE.findall(text), [], "旧口径确实漏了汉字后的数字")
        self.assertEqual(verifier._number_tokens(text), {"30%"})

    def test_entity_missing_caps_to_unverified(self):
        result = self.verify(_item("宁德时代获得固态电池大额订单，产能爬坡中。"),
                             "比亚迪获得固态电池大额订单", required_entities=["比亚迪"])
        self.assertEqual(result["verdict"], "UNVERIFIED")
        self.assertIn(verifier.REASON_ENTITY_MISSING, result["reasons"])

    def test_entity_present_passes(self):
        check = verifier.check_entities("宁德时代获得订单", _item("宁德时代获得订单"), "宁德时代获得订单",
                                        required_entities=["宁德时代"])
        self.assertTrue(check["passed"])

    def test_time_precedes_claim(self):
        result = self.verify(_item("2026年10月出台的新规要求备案。", published="2026-10-01"),
                             "2026年10月出台的新规要求备案", valid_from="2026-11-01")
        self.assertEqual(result["verdict"], "UNVERIFIED")
        self.assertIn(verifier.REASON_TIME_PRECEDES, result["reasons"])

    def test_freshness_decays_by_half_life(self):
        fresh = verifier.check_time("2026-10-09", now=NOW)
        stale = verifier.check_time(SIX_MONTHS, now=NOW)
        self.assertAlmostEqual(stale["freshness"], 0.5, places=2, msg="一个半衰期应当约 0.5")
        self.assertGreater(fresh["freshness"], stale["freshness"])
        missing = verifier.check_time("", now=NOW)
        self.assertEqual(missing["freshness"], 0.5)
        self.assertEqual(missing["reason"], verifier.REASON_TIME_UNKNOWN)

    def test_second_hand_relay_caps_to_qualified(self):
        result = self.verify(_item("宁德时代股价大涨5%，储能订单增长。",
                                   metadata={"doc_type": "ai_qa_summary"}),
                             "宁德时代股价大涨5%")
        self.assertEqual(result["verdict"], "QUALIFIED")
        self.assertIn(verifier.REASON_SECOND_HAND, result["reasons"])

    def test_causal_inflation_caps_to_qualified(self):
        result = self.verify(_item("宁德时代股价大涨5%，固态电池量产消息公布。"),
                             "宁德时代股价大涨是因为固态电池量产")
        self.assertEqual(result["verdict"], "QUALIFIED")
        self.assertIn(verifier.REASON_CAUSAL_INFLATION, result["reasons"])

    def test_missing_authority_is_flagged(self):
        result = self.verify(_item("宁德时代股价大涨5%，储能订单增长。", authority=None),
                             "宁德时代股价大涨5%")
        self.assertIn(verifier.REASON_AUTHORITY_MISSING, result["reasons"])
        self.assertLess(result["dimensions"]["source_quality"], 0.5)

    def test_relationship_contradicts_cannot_be_supported(self):
        result = self.verify(_item("宁德时代股价大涨5%，储能订单增长。", relationship="contradicts"),
                             "宁德时代股价大涨")
        self.assertNotEqual(result["verdict"], "SUPPORTED")
        self.assertIn(verifier.REASON_CONTRADICT_RELATION, result["reasons"])


# ── P03-04 EvidenceScore / reason / cache ────────────────────────────────────

class ScoreAndCacheTests(_Base):
    def test_evidence_score_formula_and_clamp(self):
        perfect = {key: 1.0 for key in verifier.DEFAULT_WEIGHTS}
        self.assertAlmostEqual(verifier.evidence_score(perfect), 1.0, places=3)
        self.assertEqual(verifier.evidence_score({}), 0.0)
        risky = dict(perfect, contradiction_risk=1.0)
        self.assertLess(verifier.evidence_score(risky), verifier.evidence_score(perfect))
        # 越界维度被钳制，不会把总分顶穿
        self.assertLessEqual(verifier.evidence_score(dict(perfect, entailment=99.0)), 1.0)

    def test_score_weights_env_override_changes_config_hash(self):
        before = verifier.config_hash()
        os.environ["QA_VERIFIER_W_ENTAILMENT"] = "0.9"
        try:
            self.assertEqual(verifier.score_weights()["entailment"], 0.9)
            self.assertNotEqual(verifier.config_hash(), before, "调权重必须换配置指纹（缓存自然失效）")
        finally:
            os.environ.pop("QA_VERIFIER_W_ENTAILMENT", None)
        self.assertEqual(verifier.config_hash(), before)

    def test_every_reason_has_chinese_text(self):
        for code in verifier.VERIFICATION_REASONS:
            self.assertIn(code, verifier.REASON_TEXTS)
            self.assertTrue(verifier.REASON_TEXTS[code].strip())

    def test_reason_text_dedups_and_truncates(self):
        text = verifier.reason_text([verifier.REASON_SUPPORTED, verifier.REASON_SUPPORTED])
        self.assertEqual(text.count("。"), 1)
        self.assertLessEqual(len(verifier.reason_text(list(verifier.VERIFICATION_REASONS))), 400)

    def test_verdict_domain_is_the_frozen_enum(self):
        result = self.verify(_item("宁德时代股价大涨5%，储能订单增长。"), "宁德时代股价大涨5%")
        self.assertIn(result["verdict"], EVIDENCE_STATUSES)

    def test_cache_hit_returns_same_verdict(self):
        item = _item("宁德时代股价大涨5%，储能订单增长。")
        first = self.verify(item, "宁德时代股价大涨5%")
        second = self.verify(item, "宁德时代股价大涨5%")
        self.assertEqual(first["cache"], "miss")
        self.assertEqual(second["cache"], "hit")
        self.assertEqual(first["verdict"], second["verdict"])
        self.assertEqual(first["score"], second["score"])
        self.assertGreaterEqual(self.cache.stats()["hits"], 1)

    def test_cache_key_covers_authority_and_time(self):
        """同来源同正文、但权威性/发布时间不同 → 不许互相命中（实测踩过的坑）。"""
        base = _item("宁德时代股价大涨5%，储能订单增长。")
        other = _item("宁德时代股价大涨5%，储能订单增长。", authority=None, published="2020-01-01")
        first = self.verify(base, "宁德时代股价大涨5%")
        second = self.verify(other, "宁德时代股价大涨5%")
        self.assertEqual(second["cache"], "miss", "不同权威性/时间的证据不许命中同一条缓存")
        self.assertNotEqual(first["score"], second["score"])

    def test_cache_disabled_recomputes(self):
        os.environ["QA_VERIFIER_CACHE_ENABLED"] = "0"
        try:
            item = _item("宁德时代股价大涨5%，储能订单增长。")
            result = verifier.verify_evidence_item(
                item, claim_text="宁德时代股价大涨5%", cache=self.cache, now=NOW)
            self.assertEqual(result["cache"], "miss")
            again = verifier.verify_evidence_item(
                item, claim_text="宁德时代股价大涨5%", cache=self.cache, now=NOW)
            self.assertEqual(again["cache"], "miss", "关掉缓存后不许出现命中")
            self.assertEqual(self.cache.stats()["hits"], 0)
        finally:
            os.environ.pop("QA_VERIFIER_CACHE_ENABLED", None)

    def test_cache_expires_by_ttl(self):
        clock = {"now": 1000.0}
        cache = verifier.VerificationCache(ttl_seconds=30, now=lambda: clock["now"])
        key = verifier.VerificationCache.key(claim_text="c", evidence_key="e")
        cache.put(key, {"verdict": "SUPPORTED"})
        self.assertEqual(cache.get(key)["verdict"], "SUPPORTED")
        clock["now"] += 31
        self.assertIsNone(cache.get(key), "过 TTL 必须不再命中")

    def test_cache_lru_bound(self):
        clock = {"now": 0.0}
        cache = verifier.VerificationCache(max_entries=16, ttl_seconds=600, now=lambda: clock["now"])
        for index in range(40):
            cache.put(verifier.VerificationCache.key(claim_text="c%d" % index, evidence_key="e"),
                      {"verdict": "SUPPORTED"})
        self.assertLessEqual(cache.stats()["size"], 16)


# ── batch 入口：闸门、重排、独立性、异常兜底 ─────────────────────────────────

class BatchTests(_Base):
    def _batch(self, items, claim, **kwargs):
        kwargs.setdefault("cache", self.cache)
        kwargs.setdefault("now", NOW)
        return verifier.verify_evidence_batch(items, claim_text=claim, **kwargs)

    def test_batch_annotates_every_item_without_touching_top_level_keys(self):
        items = [_item("宁德时代股价大涨5%，储能订单增长。", ref="article:1"),
                 _item("比亚迪销量创新高，芯片供应紧张。", ref="article:2", source_url="https://other.com/b")]
        before = {key for key in items[0]}
        kept, audit = self._batch(items, "宁德时代股价大涨的原因")
        self.assertEqual(audit["checked"], 2)
        self.assertEqual(audit["gate"], "refuted")
        for item in kept:
            self.assertEqual(set(item.keys()), before, "核验不许动证据顶层键集")
            self.assertIn("verification", item["metadata"]["evidence_layer"])
        self.assertIn("SUPPORTED", audit["verdicts"])

    def test_refuted_evidence_is_gated_out(self):
        items = [_item("宁德时代股价大涨5%，储能订单增长。", ref="article:1"),
                 _item("医保新规适用于民营医院。", ref="article:2", source_url="https://other.com/b")]
        kept, audit = self._batch(items, "医保新规不适用于民营医院", required_entities=[])
        self.assertEqual([item["evidence_ref"] for item in kept], ["article:1"])
        self.assertEqual(audit["dropped"], 1)

    def test_gate_never_empties_the_pack(self):
        items = [_item("医保新规适用于民营医院，报销比例下调。", ref="article:1")]
        kept, audit = self._batch(items, "医保新规不适用于民营医院")
        self.assertEqual(len(kept), 1, "闸门不许把证据包清空（清空就退回原证据）")
        self.assertEqual(audit["dropped"], 0)
        self.assertIn("gate_emptied_evidence_fallback", audit["degraded"])

    def test_gate_off_keeps_everything(self):
        items = [_item("医保新规适用于民营医院。", ref="article:1")]
        kept, audit = self._batch(items, "医保新规不适用于民营医院", gate="off")
        self.assertEqual(len(kept), 1)
        self.assertEqual(audit["dropped"], 0)
        self.assertEqual(audit["verdicts"].get("REFUTED"), 1)

    def test_batch_reranks_by_score(self):
        items = [_item("比亚迪销量创新高，芯片供应紧张。", ref="article:1", source_url="https://a.com/1"),
                 _item("宁德时代股价大涨5%，储能订单增长，固态电池量产。", ref="article:2",
                       source_url="https://b.com/2")]
        kept, audit = self._batch(items, "宁德时代股价大涨的原因")
        self.assertEqual(kept[0]["evidence_ref"], "article:2", "高分证据必须排前面")
        self.assertGreaterEqual(audit["reordered"], 1)

    def test_rerank_can_be_turned_off(self):
        items = [_item("比亚迪销量创新高。", ref="article:1", source_url="https://a.com/1"),
                 _item("宁德时代股价大涨5%，储能订单增长。", ref="article:2", source_url="https://b.com/2")]
        kept, audit = self._batch(items, "宁德时代股价大涨的原因", rerank=False)
        self.assertEqual([item["evidence_ref"] for item in kept], ["article:1", "article:2"])
        self.assertEqual(audit["reordered"], 0)

    def test_same_domain_evidence_has_lower_independence(self):
        items = [_item("宁德时代股价大涨5%，储能订单增长。", ref="article:1", source_url="https://same.com/1"),
                 _item("宁德时代股价大涨5%，储能订单增长。", ref="article:2", source_url="https://same.com/2")]
        kept, _audit = self._batch(items, "宁德时代股价大涨5%")
        self.assertAlmostEqual(kept[0]["metadata"]["evidence_layer"]["verification"]
                               ["dimensions"]["independence"], 0.5, places=3)

    def test_disabled_verifier_returns_items_untouched(self):
        os.environ["QA_VERIFIER_ENABLED"] = "0"
        try:
            items = [_item("宁德时代股价大涨5%", ref="article:1")]
            kept, audit = self._batch(items, "宁德时代股价大涨5%")
            self.assertFalse(audit["enabled"])
            self.assertNotIn("evidence_layer", kept[0]["metadata"])
        finally:
            os.environ.pop("QA_VERIFIER_ENABLED", None)

    def test_unverifiable_input_never_raises(self):
        """失败路径：坏输入（None 正文/缺字段）不许抛异常。"""
        items = [{"evidence_ref": "article:1", "source_type": "article", "title": "",
                  "source_url": "", "content_excerpt": None, "metadata": {}},
                 {"evidence_ref": "article:2"}]
        kept, audit = self._batch(items, "宁德时代股价大涨5%")
        self.assertEqual(len(kept), 2)
        self.assertEqual(audit["checked"], 2)
        self.assertIn("UNVERIFIED", audit["verdicts"])

    def test_verifier_exception_is_swallowed(self):
        """失败路径：核验内部炸掉时原样返回证据 + 审计写原因。"""
        from unittest import mock

        items = [_item("宁德时代股价大涨5%", ref="article:1")]
        with mock.patch.object(verifier, "check_negation", side_effect=RuntimeError("boom")):
            kept, audit = self._batch(items, "宁德时代股价大涨5%")
        self.assertEqual([item["evidence_ref"] for item in kept], ["article:1"])
        self.assertIn("boom", audit["reason"])


# ── claim 级核验（MASTER_RULES 第 11 条）─────────────────────────────────────

class ClaimGraphTests(_Base):
    def _graph(self, claim_text, verification_status, evidence, refs):
        return {
            "version": "qa-adjudication-v1",
            "claims": [{"canonical_id": "c1", "claim": {
                "claim_id": "c1", "text": claim_text, "claim_type": "current_fact",
                "confidence": 0.9, "valid_from": None, "valid_to": None, "scope": [],
                "evidence_refs": list(refs), "needs_verification": True,
                "verification_status": verification_status}}],
            "evidence": list(evidence),
            "edges": [{"claim_id": "c1", "evidence_ref": ref, "relationship": "supports"}
                      for ref in refs],
            "conflicts": [],
        }

    def test_model_self_assessment_is_overridden(self):
        """LLM 自己写的 confirmed 必须被规则核验结果覆盖（真机实测它自评 qualified）。"""
        graph = self._graph("宁德时代股价大涨的原因", "confirmed",
                            [_item("比亚迪销量创新高，芯片供应紧张。", ref="article:1")], ["article:1"])
        summary = verifier.verify_claim_graph(graph, cache=self.cache, now=NOW)
        claim = graph["claims"][0]["claim"]
        self.assertEqual(claim["verification_status"], "unverified")
        self.assertEqual(summary["stats"]["confirmed"], 0)
        self.assertEqual(summary["stats"]["unsupported_claim_rate"], 1.0)

    def test_supported_claim_is_confirmed_with_mass(self):
        graph = self._graph("宁德时代10月9日股价大涨，原因是固态电池量产",
                            "unverified",
                            [_item("10月9日，宁德时代股价大涨5.2%，主要因为固态电池量产消息。",
                                   ref="article:1", source_url="https://a.com/1")],
                            ["article:1"])
        summary = verifier.verify_claim_graph(graph, cache=self.cache, now=NOW)
        node = graph["claims"][0]
        self.assertEqual(node["claim"]["verification_status"], "confirmed")
        self.assertEqual(node["verification"]["support_count"], 1)
        self.assertEqual(node["verification"]["independent_source_count"], 1)
        self.assertGreater(node["verification"]["support_mass"], 0)
        self.assertEqual(summary["stats"]["unsupported_claim_rate"], 0.0)

    def test_conflicting_evidence_marks_claim_conflicted(self):
        graph = self._graph("医保新规不适用于民营医院", "confirmed",
                            [_item("医保新规适用于民营医院，报销比例下调。", ref="article:1")],
                            ["article:1"])
        verifier.verify_claim_graph(graph, cache=self.cache, now=NOW)
        self.assertEqual(graph["claims"][0]["claim"]["verification_status"], "conflicted")

    def test_claim_without_evidence_is_insufficient(self):
        graph = self._graph("没有任何证据的结论", "confirmed", [], [])
        summary = verifier.verify_claim_graph(graph, cache=self.cache, now=NOW)
        self.assertEqual(graph["claims"][0]["claim"]["verification_status"], "insufficient_evidence")
        self.assertEqual(summary["stats"]["claim_without_evidence"], 1)

    def test_dangling_reference_is_reported(self):
        graph = self._graph("引用了不存在的证据", "confirmed", [], ["article:404"])
        verifier.verify_claim_graph(graph, cache=self.cache, now=NOW)
        node = graph["claims"][0]
        self.assertEqual(node["claim"]["verification_status"], "unverified")
        self.assertIn(verifier.REASON_MISSING_EVIDENCE, node["verification"]["pairs"][0]["reasons"])

    def test_verified_evidence_item_passes_evidence_object_schema(self):
        """核验后的证据对象必须还能过 Phase 02 的证据对象契约（含嵌套 verification）。"""
        from qa_graph_contracts import validate

        verified, _audit = verifier.verify_evidence_batch(
            [annotate_evidence(_item("宁德时代股价大涨5%，储能订单增长。"))],
            claim_text="宁德时代股价大涨5%", cache=self.cache, now=NOW)
        layer = verified[0]["metadata"]["evidence_layer"]
        ok, note = validate("evidence_object", layer)
        self.assertTrue(ok, note)
        ok, note = validate("evidence_verification", layer["verification"])
        self.assertTrue(ok, note)


if __name__ == "__main__":
    unittest.main()
