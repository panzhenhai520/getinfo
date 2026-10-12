#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全链端到端：**合法拒答在真链路里活着落库**（真 QaOrchestrator + 真 synthesis 处理器）。

与 `test_qa_synthesis_refusal_passthrough.py` 的分工（那个是**单元级**：直接调
`QaFinalSynthesizer._lock_to_graph` 这个静态方法）。本文件升级到**链路级**：

  · 真的 `QaOrchestrator.execute(run_id)` —— 阶段调度、`record_stage`、`update_run(final_answer=...)`
    落库，全部走真代码；
  · 真的 `qa_pipeline.build_qa_stage_handlers` 产出的 `synthesis` / `citation_validation` 处理器；
  · 真的 `QaFinalSynthesizer.generate()`（提示拼装 → 流式解析 → 契约校验 → grounding → 落图）；
  · **打桩只在模型端点这一层**：把 `qa_synthesis.requests.post` 换成一段 SSE 桩，
    回放"模型返回的真实 JSON"（`answer` 字段 = 该 run 逐字还原的 86 字拒答正文）。

夹具（真 run `0b047cc4e0db49078f24e75e44beb28d`，B 机只读 SELECT 取得）：
  · `citation_map` = `[1]→article:1765 … [8]→article:2355`（8 条，逐字）；
  · `plan` 阶段的 `question_plan`（逐字，含 `retrieval_strategy` 里的"法规"——
    这正是让 `_policy_or_risk_question` 为真、从而**当年**命中
    `policy_missing_line_citation` 的那个前提）；
  · 图的 2 条 claim（`c1` 无 refs、`policy-impact-family-office` 带 4 个 refs）与 8 条证据；
  · 被顶掉的模型正文：86 字，由该 run 的 `qa_events.answer_delta`（49 个流式片段，
    `event_id` 34–82）**逐字拼接**还原，本文件用 `_REAL_STREAM_FRAGMENTS` 钉住原始片段序列。

全程零模型 / 零嵌入 / 零网络端点。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_pipeline  # noqa: E402
import qa_synthesis  # noqa: E402
from qa_orchestrator import QaOrchestrator  # noqa: E402
from qa_resilience import QaPersistentResilience  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

# ── 真夹具（全部逐字取自 B 机该 run 的落库数据）────────────────────────────────

QUESTION = "最近融资近3亿元的公司是哪个公司"
"""`qa_runs.question_text`（逐字）。"""

_REAL_STREAM_FRAGMENTS = (
    "根据", "提供的", "证据", "，", "没有", "找到", "任何", "关于", "“", "最近", "融资", "近", "3",
    "亿元", "的公司", "”", "的信息", "。", "所有", "证据", "均", "涉及", "信托", "行业", "、", "财富",
    "管理", "、", "房地产", "交易", "或", "离", "岸", "信托", "税务", "政策", "，", "与", "融资", "事件",
    "无关", "。", "因此", "，", "无法", "确定", "该公司", "名称", "。",
)
"""该 run `qa_events.answer_delta` 的 49 个流式片段（`event_id` 34–82，逐字，含全角标点）。"""

MODEL_REFUSAL_PROSE = "".join(_REAL_STREAM_FRAGMENTS)
"""被顶掉的模型正文 = 上面 49 个片段拼接结果（**86 字**，本文件用 `len()` 钉住）。"""

MODEL_SUMMARY = "无证据支持，无法回答。"
"""该 run 模型返回的 `sections.summary`（逐字）。"""

POLICY_CLAIM_TEXT = "该类离岸信托税务政策会影响家族办公室的信托架构复核、税务申报、客户沟通和跨专业协作。"

REAL_CITATION_MAP = {
    "[1]": "article:1765", "[2]": "article:1794", "[3]": "article:2610",
    "[4]": "article:2855", "[5]": "article:2905", "[6]": "article:2863",
    "[7]": "article:2930", "[8]": "article:2355",
}
"""该 run `details_json.result.citation_map`（逐字）。"""

REAL_CITATIONS = ["article:2355", "article:2855", "article:2905", "article:1794"]
"""该 run `details_json.result.citations`（逐字）。"""

QUESTION_PLAN = {
    "capability": "document_research",
    "categories": [{"key": "fact_check", "label": "事实核验类",
                    "questions": [QUESTION], "question_ids": ["q1"]}],
    "is_followup": False,
    "relationship": "single",
    "subquestions": [{"id": "q1", "text": QUESTION,
                      "category": {"key": "fact_check", "label": "事实核验类"}}],
    "context_focus": "",
    "plan_boundary": "结论必须绑定可引用证据；证据不足时明确说明",
    "task_template": {
        "key": "topic_research", "label": "行业资料检索",
        "boundary": "结论必须绑定可引用证据；证据不足时明确说明",
        "checklist": [{"group": "检索范围", "items": ["行业包内相关文章", "知识库官方原文/报告（若已开启）"]},
                      {"group": "结论", "items": ["事实与数据", "影响与风险", "时间线"]}],
        "capability": "document_research", "matched_terms": 0, "capability_note": "",
    },
    "answer_outline": ["核验问题是否属于当前行业包范围", "检索本行业包内的直接证据", "证据不足时明确说明缺口"],
    "plan_checklist": [{"group": "检索范围", "items": ["行业包内相关文章", "知识库官方原文/报告（若已开启）"]},
                       {"group": "结论", "items": ["事实与数据", "影响与风险", "时间线"]}],
    "question_count": 1,
    "answer_strategy": "围绕当前问题直接检索并回答，结论必须绑定证据。",
    "answer_template": ["核验问题是否属于当前行业包范围", "检索本行业包内的直接证据", "证据不足时明确说明缺口"],
    "relationship_label": "单一问题",
    # 这一条里的"法规"就是把 `_policy_or_risk_question` 点着的那个词（逐字）
    "retrieval_strategy": [{"source": "official_policy",
                            "purpose": "先按法规号、标题、发布机关精确查官方原文，作为结论和引用排序的基础。"}],
    "standalone_question": QUESTION,
    "multi_turn_suggested": False,
}
"""该 run `plan` 阶段 `details_json.result.question_plan`（逐字）。"""

_REAL_EVIDENCE_ROWS = [
    ("article:2355", "信托业三分类收官观察：规模增至34万亿元 标品渐成基本盘", "2026-08-26", 1),
    ("article:1765", "信報地產投資 - 新盤情報 - 新盤丨應天獲買家3億掃兩伙 - 信報網站 hkej.com", "2026-08-04", 3),
    ("article:2930", "深耕“六核七融”党建模式 中粮信托以高质量党建引领保障高质量发展", "2026-10-08", 3),
    ("article:2855", "财富管理“降佣降费”；私人银行业务成绩单出炉；高净值人群偏好“标品投资”", "2026-09-30", 3),
    ("article:2905", "财富管理行业“以客户为中心”的“分层错位”发展之道", "2026-09-30", 3),
    ("article:2863", "美国是如何发展家族信托业务的？我国可以借鉴哪些经验？", "2026-09-30", 3),
    ("article:1794", "內地客港投保收益徵稅 險企劇震 灣區金融發展", "2026-08-06", 3),
    ("article:2610", "8月商廈租賃成交按月跌一成 料本月平穩發展", "2026-09-20", 3),
]


def _evidence():
    return [{
        "evidence_ref": ref, "source_type": "article", "title": title,
        "source_url": "https://example.com/%s" % ref.split(":")[-1],
        "article_id": int(ref.split(":")[-1]),
        "content_excerpt": title, "published_at": published_at,
        "authority_level": authority, "score": 30.0,
        "retrieval_method": "keyword", "match_reason": "标题命中：融资",
        "relationship": "supports", "metadata": {"matched_keywords": ["融资"]},
    } for ref, title, published_at, authority in _REAL_EVIDENCE_ROWS]


def _claims():
    """该 run 图里的两条 claim（逐字）：`c1` 的正文就是**问题本身**、且**没有 refs**。"""
    return [
        {"claim_id": "c1", "text": QUESTION, "claim_type": "current_fact",
         "confidence": 0.5, "valid_from": None, "valid_to": None, "scope": [],
         "evidence_refs": [], "needs_verification": True,
         "verification_status": "insufficient_evidence"},
        {"claim_id": "policy-impact-family-office", "text": POLICY_CLAIM_TEXT,
         "claim_type": "interpretation", "confidence": 0.82, "valid_from": None, "valid_to": None,
         "scope": ["家族办公室"],
         "evidence_refs": ["article:2355", "article:2855", "article:2905", "article:1794"],
         "needs_verification": True, "verification_status": "unverified"},
    ]


def _graph(*, claims=None, evidence=None):
    return {"claims": list(claims if claims is not None else _claims()),
            "evidence": list(evidence if evidence is not None else _evidence()),
            "conflicts": []}


def _model_raw(answer, *, summary=MODEL_SUMMARY, sections=None, citations=None, claims=None):
    """模型返回的真实 JSON：`answer` 就是模型的正文散文（拒答/断言都由调用方给）。"""
    return json.dumps({
        "contract_version": "unified-qa-v1", "status": "ready",
        "answer": answer,
        "sections": dict(sections if sections is not None
                         else {"summary": summary, "key_evidence": "", "timeline": "",
                               "comparison": "", "business_impact": "", "retrieval_scope": ""}),
        "claims": list(claims if claims is not None
                       else [item["claim_id"] for item in _claims()]),
        "conflicts": [],
        "citations": list(citations if citations is not None else REAL_CITATIONS),
    }, ensure_ascii=False)


# ── 零网络：SSE 桩直接顶在 `requests.post` 上（这就是生产 local provider 走的真实那条道）──

class _FakeStreamResponse:
    status_code = 200
    text = ""

    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None

    def close(self):
        return None

    def iter_lines(self):
        body = ('data: {"choices":[{"delta":{"content":%s}}]}'
                % json.dumps(self.content, ensure_ascii=False)).encode("utf-8")
        yield body
        yield b"data: [DONE]"


def _stream_post(content):
    return mock.Mock(side_effect=lambda url, **kwargs: _FakeStreamResponse(content))


class _StubProfile:
    """本地端点桩：`provider_id=local` 才走流式（不需要 api_key），绝不联网。"""

    provider_id = "local"
    model_id = "stub"
    base_url = "http://127.0.0.1:9/v1"
    api_key = ""
    use_proxy = False


class _StubRegistry:
    def resolve(self, role, provider_id, **kwargs):
        return _StubProfile()


class _StubFlags:
    def snapshot(self):
        return {"synthesis_enabled": True, "level2_enabled": True, "enabled": True}


class _StubModelClient:
    """非流式模型客户端桩（**修复重试**那条路走的是它）。

    为什么必须有：`QaFinalSynthesizer.generate()` 的**修复重试**调用的是
    `self.model_client(...)`，只有首答才走流式 `requests.post`。不桩它的话，
    一旦首答被判不合格（例如 grounding 闸门拦住），链路就会去打**真端点** ——
    那既违反"零网络"，也会把"闸门拦住之后会怎样"测成"端点连不上"。
    """

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, profile, messages, timeout=90):
        self.calls.append({"timeout": timeout})
        return self.payload

    @property
    def last_usage(self):
        return {}


class _SpySynthesizer(qa_synthesis.QaFinalSynthesizer):
    """真合成器 + 记录本次真正交给模型的 `question` / `context_pack`。

    只为了证明"政策/风险问句"这个前提**真的成立**（而不是靠测试自己拼一份 question 猜），
    并顺手证明 `_lock_to_graph` 看到的就是真图（2 claims / 8 evidence）。
    """

    def __init__(self, model_client, repair_payload):
        super().__init__(model_client=model_client)
        self.calls = []
        self.repair_payload = repair_payload

    def generate(self, **kwargs):
        self.calls.append({
            "question": kwargs.get("question"),
            "context_pack": kwargs.get("context_pack"),
            "graph": kwargs.get("graph"),
        })
        return super().generate(**kwargs)


# ── 链路装配 ────────────────────────────────────────────────────────────────

_SEEDED_STAGES = ("plan", "level1_retrieval", "logic_validation", "level1_draft",
                  "level2_retrieval", "level2_research", "conflict_review")
"""预置为 completed 的上游阶段：它们的输出形态与 orchestrator 自己写进
`qa_stage_runs.details_json["result"]` 的**逐字一致**，因此 orchestrator 的回放路径
（`store.stage_runs()` → `completed_outputs`）就是生产上"断点续跑"走的那条。
`level2_enabled=True`（standard 模式），所以最终真实执行的是 `synthesis` + `citation_validation`。"""


def _refusal_only_graph():
    """只留一条**没有任何 refs** 的结论（`c1`）。

    为什么需要它：`_ensure_inline_citations` 只要看到"带 refs 的结论"就会补一块
    `关键依据：…[n]`，正文长度随即越过 20 字 —— 于是"极短"那条判据在**完整图**上
    根本不可能命中（真 run `0b047cc4` 当年就是如此，见 `FixtureFidelityTests`）。
    要单独验"极短判据没被关掉"，就得用这张不会补块的小图。
    """
    return _graph(claims=[_claims()[0]])


class _Chain:
    """一次真链路的装配与执行（隔离 sqlite，零网络）。"""

    def __init__(self, test, *, model_raw, graph=None, request_question=QUESTION,
                 question_plan=None, grounding_gate=False, temp_dir=None):
        self.test = test
        self.temp_dir = temp_dir or tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "refusal_e2e.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        assert self.db.backend == "sqlite", "测试必须跑在隔离 sqlite 上"
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.graph = dict(graph if graph is not None else _graph())
        self.question_plan = dict(question_plan if question_plan is not None else QUESTION_PLAN)
        self.request_question = request_question

        request = {"question": request_question, "industry_pack_id": "auto", "mode": "standard"}
        run = self.store.create_run(request, owner_user_id="refusal-e2e",
                                   idempotency_key="refusal-e2e-%s" % id(self))
        self.run_id = str(run["id"])

        self._seed_upstream()
        self.spy = _SpySynthesizer(_StubModelClient(model_raw), repair_payload=model_raw)
        self.handlers = qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=None, web_search=None,
            store=self.store, provider_registry=_StubRegistry(), feature_flags=_StubFlags(),
            level1_generator=None, final_synthesizer=self.spy,
            resilience=QaPersistentResilience(self.db),
        )
        self.orchestrator = QaOrchestrator(self.store, self.handlers)
        # 让 orchestrator 读到 level2_enabled=True（否则会把 level2 三个阶段从链上摘掉）
        self.orchestrator.feature_flags = _StubFlags()
        self.model_raw = model_raw
        self.grounding_gate = grounding_gate
        self.post_stub = None
        self.executed = None
        self.final_answer = None

    # ── 装配 ────────────────────────────────────────────────────────────
    def _seed_upstream(self):
        outputs = {
            "plan": {"question": self.request_question,
                     "standalone_question": self.question_plan.get("standalone_question") or
                                            self.request_question,
                     "question_plan": self.question_plan,
                     "queries": ["最近融资 3亿元 公司"], "entities": ["融资"]},
            "level1_retrieval": {"evidence": list(self.graph.get("evidence") or []),
                                 "stats": {"adopted": len(self.graph.get("evidence") or [])}},
            "logic_validation": {"status": "ok", "missing_links": [], "note": "",
                                 "multi_hop": {"hops": []}},
            "level1_draft": {"contract_version": "unified-qa-v1", "draft_answer": "草稿",
                             "claims": list(self.graph.get("claims") or []),
                             "gaps": [], "evidence": list(self.graph.get("evidence") or [])},
            "level2_retrieval": {"evidence": [], "rag_mode": "RAG检索", "enhanced": False},
            "level2_research": {},
            "conflict_review": dict(self.graph),
        }
        for stage in _SEEDED_STAGES:
            self.store.record_stage(self.run_id, stage, status="completed", attempt=1,
                                    input_hash="seed", output_hash="seed",
                                    details={"result": outputs[stage]})

    # ── 执行 ────────────────────────────────────────────────────────────
    def execute(self):
        """真跑一次 orchestrator。`post_stub` 可先设好（如超时桩），默认是回放本链路模型 JSON 的 SSE 桩。"""
        if getattr(self, "post_stub", None) is None:
            self.post_stub = _stream_post(self.model_raw)
        env_patch = {"QA_GROUNDING_GATE": "1"} if self.grounding_gate else {}
        with mock.patch.object(qa_synthesis.requests, "post", self.post_stub), \
                mock.patch.dict(os.environ, env_patch, clear=False):
            self.executed = self.orchestrator.execute(self.run_id)
        row = self.store.get_run(self.run_id) or {}
        self.run_row = row
        self.final_answer = row.get("final_answer") or {}
        return self.final_answer

    # ── 断言便利 ────────────────────────────────────────────────────────
    @property
    def answer(self):
        return str(self.final_answer.get("answer") or "")

    @property
    def diagnostics(self):
        return ((self.final_answer.get("sections") or {}).get("diagnostics") or {})

    def close(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()


class _Base(unittest.TestCase):
    def setUp(self):
        try:
            from qa_observability import provider_allowed_hosts

            provider_allowed_hosts(force=True)
        except Exception:
            pass
        self._chains = []

    def tearDown(self):
        for chain in self._chains:
            chain.close()

    def _run(self, *, model_raw, **kwargs):
        chain = _Chain(self, model_raw=model_raw, **kwargs)
        self._chains.append(chain)
        chain.execute()
        return chain


# ── 0. 夹具保真：这份夹具凭什么是"真"的 ────────────────────────────────────────


class FixtureFidelityTests(_Base):
    def test_the_refusal_prose_is_exactly_the_86_chars_from_the_real_run(self):
        """被顶掉的模型正文 = 该 run 49 个流式片段逐字拼接（86 字，含全角标点）。"""
        self.assertEqual(49, len(_REAL_STREAM_FRAGMENTS))
        self.assertEqual(86, len(MODEL_REFUSAL_PROSE))
        self.assertEqual(
            "根据提供的证据，没有找到任何关于“最近融资近3亿元的公司”的信息。"
            "所有证据均涉及信托行业、财富管理、房地产交易或离岸信托税务政策，与融资事件无关。"
            "因此，无法确定该公司名称。",
            MODEL_REFUSAL_PROSE)

    def test_the_fixture_arms_the_predicate_but_also_the_old_trigger(self):
        """前提双保险：新谓词判"拒答"为真；旧判据（政策/风险缺行内引用）也**确实会命中**。"""
        self.assertTrue(qa_synthesis._answer_is_refusal_or_no_evidence(MODEL_REFUSAL_PROSE))
        # 旧判据当时看到的正文 = 模型正文 + 程序补的"关键依据"块
        pre = qa_synthesis._ensure_inline_citations(
            MODEL_REFUSAL_PROSE, _claims(), REAL_CITATION_MAP)
        self.assertIn("关键依据：", pre)
        self.assertGreaterEqual(len(pre.strip()), 20, pre)
        self.assertFalse(qa_synthesis._answer_has_internal_noise(pre))
        self.assertTrue(qa_synthesis._answer_lacks_line_citations(pre),
                        "前提：政策/风险缺行内引用这条判据对这份正文**成立**（所以必须靠新谓词放行）")

    def test_fixture_reproduces_the_real_citation_map(self):
        self.assertEqual(REAL_CITATION_MAP, qa_synthesis._citation_map(_evidence()))


# ── 1. 核心：真链路里拒答活着落库 ─────────────────────────────────────────────


class RefusalSurvivesFullChainTests(_Base):
    def test_real_run_refusal_lands_in_the_persisted_answer(self):
        """**主用例**：真 orchestrator + 真 synthesis 处理器 → 落库正文保留模型那句 86 字拒答。"""
        chain = self._run(model_raw=_model_raw(MODEL_REFUSAL_PROSE))

        # 0) 链路真的跑完了（真 orchestrator 的终局落库路径）
        self.assertTrue(chain.executed.get("success"), chain.executed)
        self.assertEqual("completed", chain.executed.get("status"))
        self.assertEqual("completed", chain.run_row.get("status"))
        self.assertEqual(1, len(chain.spy.calls), "模型只被调用一次（首答即过契约）")
        self.assertEqual(1, chain.post_stub.call_count, "流式端点真的被请求过")

        # 1) 逐字断言：正文**以那 86 字拒答开头**，一个字符都没被换掉
        self.assertTrue(chain.answer.startswith(MODEL_REFUSAL_PROSE), repr(chain.answer))
        self.assertEqual(MODEL_REFUSAL_PROSE, chain.answer.split("\n\n")[0])
        # 语义断言（"没有找到"/"无法确定"这类拒答语义必须真的在正文里）
        self.assertIn("没有找到", chain.answer)
        self.assertIn("无法确定", chain.answer)

        # 2) 结构化渲染没有顶掉：两条路的固定首行都不许出现
        self.assertNotIn(qa_synthesis._STRUCTURED_LEAD.format(label="RAG检索"), chain.answer)
        self.assertNotIn(qa_synthesis._FALLBACK_LEAD.format(label="RAG检索"), chain.answer)
        # 问题本身不许再被当成"核心结论"列出来
        self.assertNotIn("- " + QUESTION + "\n", chain.answer)

        # 3) 顶掉留痕不存在（没顶掉 → diagnostics 里不许有这个键）
        self.assertNotIn("prose_override", chain.diagnostics)
        self.assertEqual(["degradation_codes"], sorted(chain.diagnostics))
        self.assertEqual([], chain.diagnostics["degradation_codes"])

        # 4) 模型自己的 summary 照旧保留（它本来就是模型写的）
        self.assertEqual(MODEL_SUMMARY, (chain.final_answer.get("sections") or {}).get("summary"))
        self.assertFalse(chain.final_answer.get("degraded"))
        self.assertEqual([], chain.final_answer.get("degradation_reasons"))
        self.assertEqual("ready", chain.final_answer.get("status"))

    def test_the_full_verbatim_form_of_the_persisted_answer(self):
        """最终正文的**逐字形态**：模型拒答原样 + 程序补的"关键依据"块（追加，不顶替）。"""
        chain = self._run(model_raw=_model_raw(MODEL_REFUSAL_PROSE))

        expected_block = "关键依据：\n- %s [8] [4] [5]" % POLICY_CLAIM_TEXT
        self.assertEqual(MODEL_REFUSAL_PROSE + "\n\n" + expected_block, chain.answer)
        # 首行就是拒答原句（供 UI 一眼可见）
        self.assertEqual(MODEL_REFUSAL_PROSE, chain.answer.splitlines()[0])
        # 程序加的块在拒答**之后**，不影响"模型说了什么"
        self.assertGreater(chain.answer.index("关键依据："), chain.answer.index(MODEL_REFUSAL_PROSE))

    def test_the_chain_really_armed_the_policy_condition(self):
        """前提证明：真链路交给模型的 question 确实点着了"政策/风险"判据（不是测试自己拼的）。"""
        chain = self._run(model_raw=_model_raw(MODEL_REFUSAL_PROSE))
        question = chain.spy.calls[0]["question"]

        self.assertTrue(question.startswith(QUESTION))
        self.assertIn("问题拆解与回答计划：", question)
        self.assertTrue(qa_synthesis._policy_or_risk_question(question))
        # 真图进的是 `_lock_to_graph`：2 条 claim（其中一条带 refs → 会补"关键依据"块）
        self.assertEqual(2, len(chain.spy.calls[0]["graph"]["claims"]))
        self.assertEqual(8, len(chain.spy.calls[0]["graph"]["evidence"]))

    def test_the_answer_did_not_come_from_the_zero_token_fallback(self):
        """这段正文**不是**"模型零 token → 证据摘录"那条路出来的：零 token 的三个指纹都不许出现。

        （路线分隔取证：`EVIDENCE_ONLY_NO_MODEL` 码、正文顶部横幅、账本固定文案。
        反向的构造见 `OtherProseLayersDoNotConflictTests`。）
        """
        chain = self._run(model_raw=_model_raw(MODEL_REFUSAL_PROSE))

        self.assertNotIn(qa_pipeline.EVIDENCE_ONLY_NO_MODEL, chain.diagnostics["degradation_codes"])
        self.assertNotIn(qa_pipeline.EVIDENCE_ONLY_HEADER, chain.answer)
        self.assertNotIn(qa_pipeline.EVIDENCE_ONLY_NOTE,
                         chain.final_answer.get("degradation_reasons") or [])
        self.assertEqual([], chain.run_row.get("degradation") or [])


# ── 2. 反向对照：该顶掉的照旧顶掉（证明没有把该顶的也放行）──────────────────────


class StillOverriddenInFullChainTests(_Base):
    def test_short_non_refusal_prose_is_still_overridden(self):
        """同一个问题、同一条链路：正文换成**非拒答的极短文本** → 照旧被顶掉，判据 = too_short。

        用小图（没有带 refs 的结论 → 不补"关键依据"块），正文长度才真的停在极短档上。
        """
        chain = self._run(model_raw=_model_raw("是的，需要申报。", summary="是的，需要申报。",
                                               claims=["c1"]),
                          graph=_refusal_only_graph())

        self.assertTrue(chain.answer.startswith(
            qa_synthesis._STRUCTURED_LEAD.format(label="RAG检索")), repr(chain.answer))
        self.assertEqual("too_short", chain.diagnostics["prose_override"]["trigger"])
        self.assertNotIn("是的，需要申报。", chain.answer)
        # 留痕记的就是判据当时看到的正文（这张图不补块，所以逐字就是模型原文）
        self.assertEqual("是的，需要申报。",
                         chain.diagnostics["prose_override"]["replaced_excerpt"])

    def test_non_policy_non_risk_question_with_short_prose_is_still_overridden(self):
        """反向对照②：把问题换成**非政策/非风险**问句 + 极短正文 → 照旧顶掉（too_short）。

        这条问句**不带**「问题拆解与回答计划」（plan 输出没有 `question_plan`），
        所以"政策/风险"判据连参与的机会都没有 —— 顶掉完全由"极短"那条判据生效。
        """
        chain = self._run(model_raw=_model_raw("共 12 家。", summary="共 12 家。", claims=["c1"]),
                          graph=_refusal_only_graph(),
                          request_question="一共有多少家公司",
                          question_plan={})

        question = chain.spy.calls[0]["question"]
        self.assertEqual("一共有多少家公司", question, "没有计划 → question 就是原始问句")
        self.assertFalse(qa_synthesis._policy_or_risk_question(question),
                         "前提：这条问句既不政策也不风险（旧判据根本不参与）")
        self.assertTrue(chain.answer.startswith(
            qa_synthesis._STRUCTURED_LEAD.format(label="RAG检索")), repr(chain.answer))
        self.assertEqual("too_short", chain.diagnostics["prose_override"]["trigger"])

    def test_policy_prose_without_line_citations_is_still_overridden(self):
        """反向对照③：政策问句里**非拒答**的正文缺行内引用 → 照旧顶掉，
        判据 = policy_missing_line_citation（就是当年顶掉那句拒答的**同一条判据**）。"""
        prose = "香港家族办公室税收优惠政策对符合条件的管理人给予利得税宽免，需要逐条核对适用范围。"
        chain = self._run(model_raw=_model_raw(prose, summary=prose))

        self.assertTrue(chain.answer.startswith(
            qa_synthesis._STRUCTURED_LEAD.format(label="RAG检索")), repr(chain.answer))
        self.assertEqual("policy_missing_line_citation",
                         chain.diagnostics["prose_override"]["trigger"])
        self.assertNotIn(prose, chain.answer, "被顶掉的原文不许回流到正文")

    def test_internal_noise_still_beats_a_refusal(self):
        """反向对照④：**带内部噪声的拒答**照旧被顶掉（噪声优先级最高，拒答不是万能通行证）。"""
        chain = self._run(model_raw=_model_raw("无法回答 claim= c1", summary="无法回答"))

        self.assertTrue(chain.answer.startswith(
            qa_synthesis._STRUCTURED_LEAD.format(label="RAG检索")), repr(chain.answer))
        self.assertEqual("internal_noise", chain.diagnostics["prose_override"]["trigger"])

    def test_a_conclusion_first_answer_is_untouched(self):
        """对照：正常断言式正文逐字保留（本改动一个字都不该动）。

        刻意**不带阿拉伯数字**：链路上"数字必须可回溯证据"是独立底线
        （`_unsupported_numbers` 会把证据外数字判成契约错误 → 修复重试 → 终局兜底），
        拿带数字的正文当夹具会变成在测那条底线，而不是在测"顶不顶掉"。
        """
        prose = "长实集团旗下港岛半山波老道项目应天获买家连购两伙大宅，成交价创该盘新高。"
        chain = self._run(model_raw=_model_raw(prose, summary=prose))

        self.assertTrue(chain.answer.startswith(prose), repr(chain.answer))
        self.assertNotIn("prose_override", chain.diagnostics)
        self.assertEqual([], chain.diagnostics["degradation_codes"])
        self.assertNotIn(qa_pipeline.EVIDENCE_ONLY_HEADER, chain.answer)


# ── 3. 变异验证：撤掉谓词 → 主用例必须红 ─────────────────────────────────────


class MutationTests(_Base):
    def test_removing_the_refusal_predicate_brings_the_override_back(self):
        """**变异验证**：把"只判形态"的拒答谓词撤掉（恒 False），同一份真实夹具必须**重新被顶掉**。

        这条证明第 1 节那组断言的因果性：断言之所以为真，**只**因为
        `_answer_is_refusal_or_no_evidence` 在场。撤掉它 → 命中
        `policy_missing_line_citation`（当年真实命中的那条判据）→
        `answer` 变成结构化渲染、`prose_override` 出现 —— 也就是
        `RefusalSurvivesFullChainTests` 里每个 `assertNotIn` / `startswith` 全部翻红。
        """
        with mock.patch.object(qa_synthesis, "_answer_is_refusal_or_no_evidence",
                               staticmethod(lambda _answer: False)):
            chain = self._run(model_raw=_model_raw(MODEL_REFUSAL_PROSE))

        # 谓词一旦撤掉：模型那句 86 字拒答被顶掉，正文变成程序话术
        self.assertEqual("policy_missing_line_citation",
                         chain.diagnostics["prose_override"]["trigger"])
        self.assertTrue(chain.answer.startswith(
            qa_synthesis._STRUCTURED_LEAD.format(label="RAG检索")), repr(chain.answer))
        self.assertFalse(chain.answer.startswith(MODEL_REFUSAL_PROSE))
        self.assertNotIn("没有找到", chain.answer)
        # 这就是"被顶掉"的直接证据：留痕里存着那句拒答
        self.assertTrue(chain.diagnostics["prose_override"]["replaced_excerpt"]
                        .startswith(MODEL_REFUSAL_PROSE))

    def test_the_predicate_is_the_only_thing_keeping_the_refusal_alive(self):
        """同一夹具、只切谓词开关：放行 vs 顶掉 —— 两份落库正文必须不一样。"""
        kept = self._run(model_raw=_model_raw(MODEL_REFUSAL_PROSE))
        with mock.patch.object(qa_synthesis, "_answer_is_refusal_or_no_evidence",
                               staticmethod(lambda _answer: False)):
            dropped = self._run(model_raw=_model_raw(MODEL_REFUSAL_PROSE))

        self.assertNotEqual(kept.answer, dropped.answer)
        self.assertTrue(kept.answer.startswith(MODEL_REFUSAL_PROSE))
        self.assertFalse(dropped.answer.startswith(MODEL_REFUSAL_PROSE))
        self.assertNotIn("prose_override", kept.diagnostics)
        self.assertIn("prose_override", dropped.diagnostics)


# ── 4. 其他会改写正文的层：与"拒答被保留"不冲突 ────────────────────────────────


class OtherProseLayersDoNotConflictTests(_Base):
    def test_grounding_gate_block_is_appended_after_the_refusal(self):
        """grounding 闸门（生产 `QA_GROUNDING_GATE=1`）只在**末尾追加**【无证据】块，不顶替拒答。

        真 run `0b047cc4` 的正文末尾就有那块（`【无证据】以下内容没有可回溯证据…`），
        位置在最后一行；本用例钉住"加了块之后拒答依然在首部、且依然不算顶掉"。
        """
        # c1 没有 refs → 闸门会判 CLAIM_WITHOUT_EVIDENCE → 追加块
        chain = self._run(model_raw=_model_raw(MODEL_REFUSAL_PROSE), grounding_gate=True)

        self.assertTrue(chain.answer.startswith(MODEL_REFUSAL_PROSE), repr(chain.answer))
        self.assertEqual(MODEL_REFUSAL_PROSE, chain.answer.split("\n\n")[0])
        self.assertIn("【无证据】", chain.answer)
        self.assertGreater(chain.answer.index("【无证据】"), chain.answer.index(MODEL_REFUSAL_PROSE),
                           "闸门块必须追加在拒答之后")
        self.assertNotIn("prose_override", chain.diagnostics,
                         "闸门追加块 ≠ 顶掉模型散文：这两件事必须分得开")
        # 闸门确实跑过（不然本用例没意义）
        self.assertTrue(chain.final_answer.get("degradation_reasons"), "闸门回执必须留痕")

    def test_evidence_only_banner_and_a_live_refusal_are_mutually_exclusive(self):
        """EVIDENCE_ONLY 横幅是"模型零 token"那条路的标注 —— 它和"模型真的拒答"不可能同时出现。

        构造：模型端点**零 token**（真超时）→ 走 `EVIDENCE_ONLY_NO_MODEL` 兜底。
        此时既没有模型拒答正文、也没有 `prose_override`（谓词根本没机会跑）。
        """
        import requests

        chain = _Chain(self, model_raw="irrelevant")
        self._chains.append(chain)
        chain.post_stub = mock.Mock(side_effect=requests.exceptions.Timeout("read timeout"))
        chain.execute()

        codes = chain.diagnostics["degradation_codes"]
        self.assertIn(qa_pipeline.EVIDENCE_ONLY_NO_MODEL, codes)
        self.assertTrue(chain.answer.startswith(qa_pipeline.EVIDENCE_ONLY_HEADER), repr(chain.answer))
        self.assertNotIn("prose_override", chain.diagnostics,
                         "零 token 这条路根本没有模型散文可顶，不该留顶掉痕迹")
        self.assertNotIn(MODEL_REFUSAL_PROSE, chain.answer)


if __name__ == "__main__":
    unittest.main()
