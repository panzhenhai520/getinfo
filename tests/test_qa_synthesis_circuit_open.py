#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：synthesis 阶段的熔断特判（"模型没被调用" ≠ "模型答得不好"）。

背景（B 机 qa_runs 交叉验证过的真因）：
    本地模型超时 → NETWORK_UNREACHABLE → 连续失败 → `provider:local` 熔断打开
    → 之后每轮走进 synthesis，`resilience.circuit_before()` 立刻抛 `QaCircuitOpen`，
      **模型根本没被调用**（该 run 综合阶段 0 token）；
    → 改之前该异常落进 synthesis 的通用 `except Exception`，被 `classify_qa_error`
      判成 `INTERNAL_ERROR`，再被改写成 `SYNTHESIS_FALLBACK_USED`（"模型未稳定返回"），
      真因于是被掩盖成"模型答得不好"。

本文件钉住：
  1. 熔断打开 → `qa_runs.degradation` 与答案里的降级码都是 **PROVIDER_CIRCUIT_OPEN**，
     文案带 circuit 原始信息（依赖名 + 重试秒数）并写明"本轮未真正调用模型"，
     **不许**出现 SYNTHESIS_FALLBACK_USED；模型客户端一次都没被调用；
  2. 对照：模型正常返回但契约两次不过 → 仍然走 **SYNTHESIS_FALLBACK_USED**
     （真实兜底不许被这个特判吞掉）。

全程隔离 sqlite + 真熔断状态打桩，零模型 / 零嵌入 / 零网络端点。
"""
import json
import os
import sys
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_pipeline  # noqa: E402
import qa_synthesis  # noqa: E402
from qa_resilience import QaCircuitOpen, QaPersistentResilience  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

QUESTION = "香港家族办公室税收优惠政策对内地高净值客户有什么影响？"
SCOPE = {"owner_user_id": "copen", "session_id": "s1", "industry_pack_id": "auto"}
DEPENDENCY = "provider:stub"


def _evidence(ref, *, authority=60, relation="supports"):
    return {
        "evidence_ref": ref, "source_type": "article",
        "title": "香港家族办公室税收优惠政策 %s" % ref,
        "source_url": "https://example.com/%s" % ref,
        "article_id": int(str(ref).split(":")[-1]),
        "content_excerpt": "香港家族办公室税收优惠政策对符合条件的管理人给予利得税宽免。",
        "published_at": "2026-10-09", "authority_level": authority, "score": 30.0,
        "retrieval_method": "keyword", "match_reason": "标题命中：家族办公室",
        "relationship": relation, "metadata": {"matched_keywords": ["家族办公室"]},
    }


class _FakeRetriever:
    def __init__(self, evidence):
        self.evidence = list(evidence)

    def retrieve(self, plan, **kwargs):
        return {"evidence": [dict(item) for item in self.evidence],
                "stats": {"eligible": len(self.evidence), "adopted": len(self.evidence)}}


class _FakeWebSearch:
    def search(self, queries, *, enabled=True, limit=8):
        return {"evidence": [], "status": "disabled", "providers": [], "errors": []}


class _StubProfile:
    provider_id = "stub"
    model_id = "stub"
    base_url = ""
    api_key = ""


class _StubRegistry:
    def resolve(self, role, provider_id, **kwargs):
        return _StubProfile()


class _StubFlags:
    def snapshot(self):
        return {"synthesis_enabled": True, "level2_enabled": True, "enabled": True}


class _RecordingClient:
    """记录调用次数的模型客户端桩：熔断用例里它**一次都不该被调用**。"""

    def __init__(self, payload=""):
        self.calls = []
        self.payload = payload

    def __call__(self, profile, messages, timeout=90):
        self.calls.append({"provider": getattr(profile, "provider_id", ""), "timeout": timeout})
        return self.payload


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "circuit_open.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.assertEqual(self.db.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.run = self.store.create_run(
            {"industry_pack_id": "auto", "question": QUESTION, "mode": "standard"},
            owner_user_id="copen", idempotency_key="circuit-open-%s" % self.id())
        self.run_id = str(self.run["id"])
        self.resilience = QaPersistentResilience(self.db)

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _handlers(self, model_client):
        """真熔断状态（注入 QaPersistentResilience）+ 桩模型客户端，零网络。"""
        return qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=_FakeRetriever([_evidence("article:1")]),
            web_search=_FakeWebSearch(), store=self.store,
            provider_registry=_StubRegistry(), feature_flags=_StubFlags(),
            final_synthesizer=qa_synthesis.QaFinalSynthesizer(model_client=model_client),
            resilience=self.resilience)

    def _context(self):
        outputs = {
            "plan": {"question": QUESTION, "standalone_question": QUESTION,
                     "queries": ["香港家族办公室 税收优惠"], "entities": ["家族办公室"],
                     "decomposition": {"is_multi_hop": False, "pattern": "single", "hop_count": 1,
                                       "hops": [{"id": "h1", "question": QUESTION, "depends_on": []}]}},
            "level1_draft": {
                "contract_version": "unified-qa-v1", "draft_answer": "草稿",
                "claims": [{
                    "claim_id": "c1",
                    "text": "香港家族办公室税收优惠政策对内地高净值客户有影响",
                    "claim_type": "current_fact", "confidence": 0.82,
                    "valid_from": "2026-10-09", "valid_to": None, "scope": ["家族办公室"],
                    "evidence_refs": ["article:1"], "needs_verification": True,
                    "verification_status": "unverified"}],
                "gaps": [], "evidence": [_evidence("article:1")]},
            "level1_retrieval": {"evidence": [_evidence("article:1")], "stats": {"adopted": 1}},
        }
        return {"request": {"question": QUESTION, "industry_pack_id": "auto", "mode": "standard"},
                "run": {"id": self.run_id, **SCOPE}, "outputs": outputs}

    def _synthesis_degradation(self):
        run = self.store.get_run(self.run_id) or {}
        return [item for item in (run.get("degradation") or [])
                if isinstance(item, dict) and item.get("stage") == "synthesis"]

    def _circuit_state(self, dependency=DEPENDENCY):
        row = self.db.connection.execute(
            "SELECT state FROM qa_circuit_states WHERE dependency=?", (dependency,)).fetchone()
        return str(row[0]) if row else ""


class CircuitOpenIsNotFallbackTests(_Base):
    """熔断打开：模型没被调用 → 降级码必须是 PROVIDER_CIRCUIT_OPEN，不是兜底码。"""

    def test_circuit_open_is_labelled_provider_circuit_open(self):
        # 打桩用**真熔断状态**（不是 mock 掉 circuit_before）：连记 3 次失败 → 走到默认阈值
        # 就把 provider:stub 打开，与生产口径（threshold=3）一致。
        for _ in range(3):
            self.resilience.circuit_failure(DEPENDENCY)
        self.assertEqual(self._circuit_state(), "open")
        client = _RecordingClient(payload=json.dumps(
            {"status": "ready", "answer": "本不该被调用的答案 [1]。", "sections": {},
             "claims": ["c1"], "conflicts": [], "citations": ["article:1"]}, ensure_ascii=False))
        handlers = self._handlers(client)
        context = self._context()
        self.assertNotIn("_emit_stage_event", context,
                         "本用例只走非流式 model_client 路径，不碰流式端点")

        answer = handlers["synthesis"](context)

        # 0) 熔断时**模型一次都没被调用**
        self.assertEqual(client.calls, [], "熔断打开时不许调用模型")

        # 1) 库里的降级记录：码是熔断，文案可读且带 circuit 原始信息（依赖 + 重试秒数）
        items = self._synthesis_degradation()
        self.assertEqual([item.get("code") for item in items], ["PROVIDER_CIRCUIT_OPEN"], items)
        message = items[0]["message"]
        self.assertIn("熔断保护", message)
        self.assertIn(DEPENDENCY, message, "必须带出 circuit 的原始依赖名")
        self.assertRegex(message, r"约 \d+ 秒", "必须带出 circuit 的原始重试时间")
        self.assertIn("本轮未真正调用模型", message)
        self.assertNotIn("SYNTHESIS_FALLBACK_USED", json.dumps(items, ensure_ascii=False))

        # 2) 答案里的降级码：熔断码在，兜底码不在
        codes = answer["sections"]["diagnostics"]["degradation_codes"]
        self.assertIn("PROVIDER_CIRCUIT_OPEN", codes)
        self.assertNotIn("SYNTHESIS_FALLBACK_USED", codes,
                         "熔断不是'模型没答好'，不许标成兜底码")
        self.assertTrue(answer["degraded"])
        reasons = " ".join(answer["degradation_reasons"])
        self.assertIn("熔断保护", reasons)
        self.assertIn(DEPENDENCY, reasons)
        self.assertNotIn("未稳定返回", reasons, "兜底文案不许出现在熔断轮")

        # 3) 既有副作用口径保持：熔断仍是打开的（没被这一轮误关）
        self.assertEqual(self._circuit_state(), "open")
        with self.assertRaises(QaCircuitOpen):
            self.resilience.circuit_before(DEPENDENCY)


class RealFallbackStillWorksTests(_Base):
    """对照：模型正常返回但契约两次不过 → 仍然走 SYNTHESIS_FALLBACK_USED。"""

    def test_two_failed_contract_attempts_still_fall_back(self):
        client = _RecordingClient(payload="这既不是 JSON 也没有大括号")
        handlers = self._handlers(client)
        context = self._context()
        self.assertNotIn("_emit_stage_event", context)

        answer = handlers["synthesis"](context)

        self.assertEqual(len(client.calls), 2, "首答 + 一次修复重试：两次都没过契约")
        codes = answer["sections"]["diagnostics"]["degradation_codes"]
        self.assertIn("SYNTHESIS_FALLBACK_USED", codes, "真兜底不许被熔断特判吞掉")
        self.assertNotIn("PROVIDER_CIRCUIT_OPEN", codes, "没熔断就不许报熔断码")
        self.assertTrue(answer["degraded"])
        self.assertTrue(answer["degradation_reasons"])
        # 熔断状态机没被这一轮碰开
        self.assertEqual(self._circuit_state(), "closed")
        # 真兜底路径不产生 synthesis 熔断记录
        self.assertEqual(self._synthesis_degradation(), [])


if __name__ == "__main__":
    unittest.main()
