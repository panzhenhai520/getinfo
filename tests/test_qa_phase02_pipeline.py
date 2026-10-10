#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 02 · 集成验收（P02-01…P02-04 接进真实链路）。

单测证明"函数对"，这里证明"**真的接上了**"：
  1. `_apply_evidence_layer` 在真 QaStore + 临时 sqlite 上跑通：标注 → 登记 → 跨轮去重；
  2. 被拒来源（垃圾）第二轮不再进入证据包，而且**身份留在库里**（不是只留计数）；
  3. 作用域隔离：换一个会话/用户，同一条证据不会被上一轮的拒收误伤；
  4. `level1_retrieval` 阶段函数**真的调用了证据层**（stats 里有回执、库里有 seen 行）；
  5. 失败路径：存储层坏掉时证据原样返回、问答不中断。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_evidence as evidence_layer  # noqa: E402
import qa_pipeline  # noqa: E402
from qa_contracts import validate_level1_result  # noqa: E402
from qa_level1 import empty_level1_result  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

SCOPE = {"owner_user_id": "u1", "session_id": "s1", "industry_pack_id": "health"}


def _evidence(ref, *, title=None, content=None, article_id=None, **overrides):
    article_id = article_id if article_id is not None else int(ref.split(":")[-1])
    item = {
        "evidence_ref": ref,
        "source_type": "article",
        "title": title or ("标题 " + ref),
        "source_url": "https://example.com/%s" % ref,
        "article_id": article_id,
        "content_excerpt": content or ("正文 %s，讲的是医保新规对民营医院的影响。" % ref),
        "published_at": "2026-10-01",
        "authority_level": 60,
        "score": 30.0,
        "retrieval_method": "keyword",
        "match_reason": "标题命中：医保",
        "relationship": "supports",
        "metadata": {"matched_keywords": ["医保新规"]},
    }
    item.update(overrides)
    return item


class ApplyEvidenceLayerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "phase02-e2e.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.run_meta = {"id": "run-1", **SCOPE}

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _apply(self, evidence, rejected, *, run_meta=None, question="医保新规对民营医院的影响"):
        return qa_pipeline._apply_evidence_layer(
            list(evidence), rejected=list(rejected), question=question, plan={},
            run_meta=run_meta or self.run_meta, store=self.store, corpus_version="corpus-1")

    def test_first_run_annotates_and_records_both_sides(self):
        kept, audit = self._apply([_evidence("article:1"), _evidence("article:2")],
                                  [_evidence("article:9")])
        self.assertEqual(len(kept), 2)
        self.assertEqual(audit["evidence_layer"], "qa-evidence-v1")
        self.assertEqual(audit["recorded"], 3)
        self.assertEqual((audit["seen_dropped"], audit["dedupe_dropped"]), (0, 0))
        for item in kept:
            layer = item["metadata"]["evidence_layer"]
            self.assertEqual(layer["status"], "SUPPORTED")
            self.assertEqual(layer["provenance"]["run_id"], "run-1")
            self.assertEqual(layer["provenance"]["corpus_version"], "corpus-1")
            self.assertTrue(layer["span"]["quote"])
        # 被拒的那条：身份真的落库了（旧实现只有计数）
        seen = self.store.seen_evidence(source_fingerprints=[
            evidence_layer.source_fingerprint(_evidence("article:9"))], **SCOPE)
        self.assertEqual(list(seen.values()), ["rejected"])

    def test_previously_rejected_source_is_dropped_next_run(self):
        """核心验收：第一轮被闸门拒掉的垃圾，第二轮不再进证据包。"""
        junk = _evidence("article:9", title="活动花絮：点赞转发赢好礼")
        kept, _audit = self._apply([_evidence("article:1")], [junk])
        self.assertEqual([item["evidence_ref"] for item in kept], ["article:1"])

        # 第二轮（新 run，同一会话）：闸门这次"放它进来了"，证据层的 seen 去重必须拦住
        self.run_meta = {"id": "run-2", **SCOPE}
        kept2, audit2 = self._apply([_evidence("article:1"), junk], [])
        self.assertEqual([item["evidence_ref"] for item in kept2], ["article:1"])
        self.assertEqual(audit2["seen_dropped"], 1)

    def test_previously_accepted_source_is_kept_across_runs(self):
        """默认强度只丢"被拒的"：上一轮用过的有用来源，追问时必须还能用。"""
        good = _evidence("article:1")
        self._apply([good], [])
        self.run_meta = {"id": "run-2", **SCOPE}
        kept, audit = self._apply([_evidence("article:1")], [])
        self.assertEqual(len(kept), 1)
        self.assertEqual(audit["seen_dropped"], 0)
        self.assertTrue(kept[0]["metadata"]["evidence_layer"]["repeat"],
                        "重复使用要留痕，但不许把它丢掉")

    def test_other_session_is_not_affected(self):
        """作用域隔离：别的会话拒过的东西，本会话照用。"""
        junk = _evidence("article:9")
        self._apply([], [junk])
        other = {**SCOPE, "session_id": "s2"}
        kept, audit = self._apply([junk], [], run_meta={"id": "run-3", **other})
        self.assertEqual([item["evidence_ref"] for item in kept], ["article:9"])
        self.assertEqual(audit["seen_dropped"], 0)

    def test_other_industry_pack_is_not_affected(self):
        junk = _evidence("article:9")
        self._apply([], [junk])
        other = {**SCOPE, "industry_pack_id": "auto"}
        kept, _audit = self._apply([junk], [], run_meta={"id": "run-4", **other})
        self.assertEqual(len(kept), 1)

    def test_mode_all_can_drop_everything_but_never_returns_empty(self):
        """极端配置（all）把证据清空时退回原证据，绝不给用户空证据包。"""
        self._apply([_evidence("article:1")], [])
        os.environ["QA_EVIDENCE_SEEN_DEDUPE"] = "all"
        try:
            kept, audit = self._apply([_evidence("article:1")], [], run_meta={"id": "run-5", **SCOPE})
        finally:
            os.environ.pop("QA_EVIDENCE_SEEN_DEDUPE", None)
        self.assertEqual(len(kept), 1)
        self.assertEqual(audit["reason"], "seen_dedupe_emptied_evidence_fallback")
        self.assertTrue(kept[0]["metadata"]["evidence_layer"]["span"]["quote"])

    def test_store_failure_returns_original_evidence(self):
        """失败路径：存储层坏掉 → 证据原样返回，审计写明原因，不抛异常。"""
        broken = mock.Mock()
        broken.record_seen_evidence.side_effect = RuntimeError("db down")
        broken.seen_evidence.side_effect = RuntimeError("db down")
        evidence = [_evidence("article:1")]
        kept, audit = qa_pipeline._apply_evidence_layer(
            evidence, rejected=[], question="医保新规", plan={}, run_meta=self.run_meta,
            store=broken)
        self.assertEqual([item["evidence_ref"] for item in kept], ["article:1"])
        self.assertIn("db down", audit["record_error"])
        # 查不到 seen 时按"没见过"处理：证据一条不少
        self.assertEqual(audit["seen_dropped"], 0)

    def test_disabled_flag_skips_layer_entirely(self):
        os.environ["QA_EVIDENCE_LAYER_ENABLED"] = "0"
        try:
            kept, audit = self._apply([_evidence("article:1")], [])
        finally:
            os.environ.pop("QA_EVIDENCE_LAYER_ENABLED", None)
        self.assertNotIn("evidence_layer", kept[0]["metadata"])
        self.assertEqual(audit["evidence_layer"], "skipped")
        self.assertEqual(audit["recorded"], 0)
        self.assertEqual(self.db.connection.execute(
            "SELECT COUNT(*) FROM qa_evidence_seen").fetchone()[0], 0)

    def test_annotated_evidence_passes_level1_contract(self):
        kept, _audit = self._apply([_evidence("article:1")], [_evidence("article:9")])
        result = validate_level1_result(empty_level1_result("草稿", kept))
        self.assertEqual(len(result["evidence"]), 1)
        self.assertEqual(result["evidence"][0]["metadata"]["evidence_layer"]["status"], "SUPPORTED")


class _FakeRetriever:
    def __init__(self, evidence):
        self.evidence = evidence
        self.calls = []

    def retrieve(self, plan, **kwargs):
        self.calls.append(dict(plan))
        return {"evidence": [dict(item) for item in self.evidence],
                "stats": {"eligible": len(self.evidence), "adopted": len(self.evidence)}}


class _FakeWebSearch:
    def search(self, queries, *, enabled=True, limit=8):
        return {"evidence": [], "status": "disabled", "providers": [], "errors": []}


class Level1RetrievalWiringTests(unittest.TestCase):
    """接线验收：阶段函数真的调用了证据层（不是"写了函数没人用"）。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "phase02-wiring.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.retriever = _FakeRetriever([_evidence("article:1", content="医保新规对民营医院的影响：报销比例下调。"),
                                         _evidence("article:2", content="医保新规的结算细则说明。")])
        self.handlers = qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=self.retriever, web_search=_FakeWebSearch(),
            store=self.store)

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _context(self, run_id="run-1"):
        return {
            "request": {"question": "医保新规对民营医院有什么影响？", "industry_pack_id": "health",
                        "mode": "standard", "page_context": {}},
            "run": {"id": run_id, **SCOPE},
            "outputs": {"plan": {"question": "医保新规对民营医院有什么影响？",
                                 "standalone_question": "医保新规对民营医院有什么影响？",
                                 "queries": ["医保新规 民营医院"], "entities": ["医保新规"],
                                 "needs_local_articles": True, "needs_web": False,
                                 "decomposition": {}}},
        }

    def test_stage_reports_evidence_layer_and_records_seen(self):
        result = self.handlers["level1_retrieval"](self._context())
        summary = result["stats"]["evidence_layer"]
        self.assertEqual(summary["evidence_layer"], "qa-evidence-v1")
        self.assertEqual(summary["annotated"], len(result["evidence"]))
        self.assertGreaterEqual(summary["recorded"], 1)
        for item in result["evidence"]:
            self.assertIn("evidence_layer", item["metadata"])
        rows = self.db.connection.execute(
            "SELECT status, COUNT(*) FROM qa_evidence_seen"
            " WHERE owner_user_id='u1' AND session_id='s1' AND industry_pack_id='health'"
            " GROUP BY status").fetchall()
        self.assertTrue(rows, "阶段函数没有把 seen 身份落库")
        validate_level1_result(empty_level1_result("草稿", result["evidence"]))

    def test_stage_drops_previously_rejected_source(self):
        """阶段级验收：上一轮被拒的来源，这一轮即使被闸门放行也不进证据包。"""
        junk = _evidence("article:9", title="某地会议纪要")
        good = _evidence("article:1", content="医保新规对民营医院的影响：报销比例下调。")
        self.store.record_seen_evidence(
            records=[{"source_fingerprint": evidence_layer.source_fingerprint(junk),
                      "source_type": "article", "evidence_ref": junk["evidence_ref"],
                      "status": "rejected"}], run_id="run-old", **SCOPE)
        self.retriever.evidence = [junk, good]

        result = self.handlers["level1_retrieval"](self._context("run-new"))

        self.assertEqual([item["evidence_ref"] for item in result["evidence"]], ["article:1"])
        self.assertEqual(result["stats"]["evidence_layer"]["seen_dropped"], 1)


if __name__ == "__main__":
    unittest.main()
