#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：**合法的模型拒答**被结构化渲染顶掉，以及"结构化渲染"与"终局兜底"首行同名。

背景（B 机 run 0b047cc4e0db49078f24e75e44beb28d，128.7 秒，模型真被调用：`synthesis` 66.3 秒 /
21786 tokens、`level1_draft` 43.2 秒 / 14599 tokens；检索回来的 8 条证据全部离题，模型如实拒答）。
该 run 的**两个字段**必须分清楚（本文件过去把第二个当成了第一个，是事实错误）：

  1. 模型的**正文**（`answer` 字段）= `MODEL_ANSWER_PROSE`，**86 字**的如实说明：
     只读 SELECT `qa_events`（run=0b047cc4、`type=answer_delta`、`stage=synthesis`、
     event_id 34 起）把 `payload.delta` 按 event_id 逐字拼接得到 —— **被顶掉的就是这一句**；
  2. `sections.summary` = `MODEL_REFUSAL`，**另一个字段**，模型写的一句 11 字拒答
     （只读 SELECT `qa_runs.final_answer_json.sections.summary` 逐字复核）；
  3. 顶掉的动作：`_lock_to_graph` 命中判据后用 `_build_structured_answer` 把上面那句 86 字正文
     换成了 `已完成RAG检索和证据图综合。`（旧文案），还把**问题本身**当成"核心结论"第一条列出
     —— 落库的 `qa_runs.final_answer_json.answer` 至今是那个形态；
  4. 那句首行与**终局兜底路径**的固定首行逐字相同，于是"模型答过、正文被程序顶掉"看起来和
     "模型没答成、走了兜底"一模一样（两条路现已分岔，见 `PathFingerprintTests`）。

本文件钉住四件事：
  · 拒答/无证据形态的正文**不再**被两条"内容不足"判据顶掉（极短、政策/风险问句缺行内引用）；
  · **该顶掉的照旧顶掉**（对照）：内部噪声（优先于拒答）、非拒答的极短正文、
    政策问句里"先给结论、末句才补一句证据不足"的正文；
  · 顶掉这件事**留下被顶掉的原文**（`sections.diagnostics.prose_override`：哪一条判据命中 +
    原文片段 + 原长度），不再只能靠推断；不新增数据库列、不改冻结 schema、不改降级口径；
  · 两条路**怎么一眼区分**：按**正文里出现哪一句**（不是首行 —— 证据摘录那条路上
    `【未经模型综合】` 横幅会占掉首行）。

夹具取自该 run 的落库数据（全部只读 SELECT）：`qa_runs.question_text`、
`qa_runs.final_answer_json`（claims / citation_map / evidence / sections.summary）、
`qa_events.answer_delta`（被顶掉的正文逐字拼接）、plan 阶段 `question_plan`：
2 条 claim（`c1` 无 refs、`policy-impact-family-office` 带 4 个 refs）、8 条证据的 `citation_map`
（`[1]→article:1765 … [8]→article:2355`，与库中逐字一致）、模型的 6 个 sections 键
（除 summary 外都是空串）。
`_question_for_synthesis` 会把「问题拆解与回答计划」拼进 question，而该计划含"法规"
→ `_policy_or_risk_question` 为 True：这正是本 run 命中"政策/风险问句缺行内引用"的前提。

全程零模型 / 零嵌入 / 零网络端点。
"""
import json
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_synthesis  # noqa: E402
import qa_pipeline  # noqa: E402  —— 证据摘录横幅（`_mark_evidence_only_payload`）与生产同一段代码
from qa_contracts import validate_final_answer  # noqa: E402

QUESTION = "最近融资近3亿元的公司是哪个公司"
"""该 run 的 `qa_runs.question_text`（逐字）。"""

QUESTION_WITH_PLAN = QUESTION + "\n\n问题拆解与回答计划：" + json.dumps({
    "原始问题": QUESTION,
    "检索用完整问题": QUESTION,
    "问题数量": 1,
    "问题关系": "single",
    # 该 run plan 阶段 question_plan.retrieval_strategy 的原文摘录（"法规"就是命中谓词的那处）
    "检索策略": [{"purpose": "先按法规号、标题、发布机关精确查官方原文，作为结论和引用排序的基础。",
                  "source": "official_policy"}],
    "回答大纲": ["核验问题是否属于当前行业包范围", "检索本行业包内的直接证据", "证据不足时明确说明缺口"],
    "子问题": [{"id": "q1", "text": QUESTION}],
}, ensure_ascii=False)
"""该 run 真正交给综合模型的 question（`_question_for_synthesis` 把计划摘要拼在后面）。"""

MODEL_ANSWER_PROSE = (
    "根据提供的证据，没有找到任何关于“最近融资近3亿元的公司”的信息。"
    "所有证据均涉及信托行业、财富管理、房地产交易或离岸信托税务政策，与融资事件无关。"
    "因此，无法确定该公司名称。"
)
"""该 run 模型写的**正文**（`answer` 字段，86 字）—— **被程序顶掉的就是这一句**。

取值来源（只读 SELECT，B 机）：`qa_events` 里 run=0b047cc4e0db49078f24e75e44beb28d、
`type=answer_delta`、`stage=synthesis` 的 `payload.delta`，按 `event_id`（34 起）逐字拼接；
event_id 6 那条 `source=question_plan` 是计划文本，不属于模型正文，已排除。

**它不等同于** `sections.summary`（那句是 `MODEL_REFUSAL`）—— 两个字段是两句话，
过去本夹具把 summary 当成了被顶掉的正文，那是事实错误；两条断言现在各自独立。
"""

MODEL_REFUSAL = "无证据支持，无法回答。"
"""模型写的 11 字拒答：该 run 的 **`sections.summary`**（`qa_runs.final_answer_json` 逐字复核）。

**它不是被顶掉的正文**（那句见 `MODEL_ANSWER_PROSE`）：`sections.summary` 走的是模型自写字段，
`_model_sections()` 用它；被顶掉的是 `answer` 字段里的 86 字。
"""

REAL_CITATION_MAP = {
    "[1]": "article:1765", "[2]": "article:1794", "[3]": "article:2610",
    "[4]": "article:2855", "[5]": "article:2905", "[6]": "article:2863",
    "[7]": "article:2930", "[8]": "article:2355",
}
"""该 run `details_json.result.citation_map`（逐字）。"""

_REAL_EVIDENCE_ROWS = [
    # (evidence_ref, title, published_at, authority_level)：顺序 = 真实 run 的证据顺序
    ("article:2355", "信托业三分类收官观察：规模增至34万亿元 标品渐成基本盘", "2026-08-26", 1),
    ("article:1765", "信報地產投資 - 新盤情報 - 新盤丨應天獲買家3億掃兩伙 - 信報網站 hke", "2026-08-04", 3),
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
        "content_excerpt": title, "published_at": published_at,
        "authority_level": authority, "score": 30.0,
        "retrieval_method": "keyword", "match_reason": "标题命中：融资",
        "relationship": "supports", "metadata": {"matched_keywords": ["融资"]},
    } for ref, title, published_at, authority in _REAL_EVIDENCE_ROWS]


def _claims():
    """该 run 的两条结论（逐字）：`c1` 的正文就是**问题本身**、且**没有 refs**。"""
    return [
        {"claim_id": "c1", "text": QUESTION, "claim_type": "current_fact",
         "confidence": 0.5, "valid_from": None, "valid_to": None, "scope": [],
         "evidence_refs": [], "needs_verification": True,
         "verification_status": "insufficient_evidence"},
        {"claim_id": "policy-impact-family-office",
         "text": "该类离岸信托税务政策会影响家族办公室的信托架构复核、税务申报、客户沟通和跨专业协作。",
         "claim_type": "interpretation", "confidence": 0.82, "valid_from": None, "valid_to": None,
         "scope": ["家族办公室"],
         "evidence_refs": ["article:2355", "article:2855", "article:2905", "article:1794"],
         "needs_verification": True, "verification_status": "unverified"},
    ]


def _graph(*, claims=None, evidence=None):
    return {"claims": list(claims if claims is not None else _claims()),
            "evidence": list(evidence if evidence is not None else _evidence()),
            "conflicts": []}


def _model_sections():
    """该 run 模型返回的 sections：6 个键，除 summary 外都是空串（逐字）。"""
    return {"summary": MODEL_REFUSAL, "timeline": "", "comparison": "",
            "key_evidence": "", "business_impact": "", "retrieval_scope": ""}


def _parsed(answer, *, sections=None, citations=None, claim_ids=None):
    return {
        "contract_version": "unified-qa-v1", "status": "ready",
        "answer": answer, "sections": dict(sections if sections is not None else _model_sections()),
        "claims": list(claim_ids if claim_ids is not None
                       else [item["claim_id"] for item in _claims()]),
        "conflicts": [], "citations": list(citations or []),
    }


def _lock(answer, *, question=QUESTION, graph=None, sections=None, citations=None):
    """直接跑 `_lock_to_graph`（模型返回字段的解析代码本体）。

    `parsed.claims` 一律按**同一张图**里的 claim_id 生成：模型引用的结论必须真实存在，
    这是既有底线，测试夹具不许绕过它。
    """
    graph = graph if graph is not None else _graph()
    parsed = _parsed(answer, sections=sections, citations=citations,
                     claim_ids=[str(item.get("claim_id")) for item in graph.get("claims") or []])
    return qa_synthesis.QaFinalSynthesizer._lock_to_graph(
        parsed, graph, {"gaps": []}, {"evidence_gaps": []}, [], {"research": "RAG检索"},
        question=question, context_pack=None,
    )


def _refusal_only_graph():
    """只留一条**没有任何 refs** 的结论：`_ensure_inline_citations` 因此不会补"关键依据"块，
    正文长度就停在被顶掉的判据点上（极短判据的对照夹具）。"""
    return _graph(claims=[{
        "claim_id": "c1", "text": QUESTION, "claim_type": "current_fact",
        "confidence": 0.5, "valid_from": None, "valid_to": None, "scope": [],
        "evidence_refs": [], "needs_verification": True,
        "verification_status": "insufficient_evidence",
    }])


# ── 0. 夹具保真：真 run 的 citation_map / question 谓词 ──────────────────────────


class FixtureFidelityTests(unittest.TestCase):
    def test_fixture_reproduces_the_real_citation_map(self):
        self.assertEqual(REAL_CITATION_MAP, qa_synthesis._citation_map(_evidence()))

    def test_run_question_arms_the_policy_condition(self):
        """本 run 真正命中的是"政策/风险问句缺行内引用"：question 带计划 → 谓词为 True。"""
        self.assertTrue(qa_synthesis._policy_or_risk_question(QUESTION_WITH_PLAN))
        self.assertFalse(qa_synthesis._policy_or_risk_question(QUESTION),
                         "对照组：光看原始问题这一条判据是**不**成立的，前提是计划被拼进来")

    def test_structured_render_would_list_the_question_itself(self):
        """被顶掉之后长什么样：结构化渲染把**问题本身**列成第一条核心结论（本 run 的真实产物）。"""
        rendered = qa_synthesis._build_structured_answer(
            question=QUESTION, claims=_claims(), evidence=_evidence(),
            citation_map=REAL_CITATION_MAP, models={"research": "RAG检索"},
        )
        self.assertIn("- " + QUESTION, rendered)

    def test_appended_citation_block_disarms_the_length_condition(self):
        """复刻判据当时看到的正文：程序一定会补"关键依据"块 → 长度早已过 20 字。

        这条钉住一个**排查结论**：该 run 命中的不可能是"极短"那条判据，
        只可能是"政策/风险问句缺行内引用"（内部噪声则取决于模型原文里有没有内部标记）。
        """
        pre_override = qa_synthesis._ensure_inline_citations(
            MODEL_ANSWER_PROSE, _claims(), REAL_CITATION_MAP)

        self.assertIn("关键依据：", pre_override)
        self.assertIn("[8]", pre_override)
        self.assertGreaterEqual(len(pre_override.strip()), 20, pre_override)
        self.assertFalse(qa_synthesis._answer_has_internal_noise(pre_override))
        self.assertTrue(qa_synthesis._answer_is_refusal_or_no_evidence(pre_override))

    def test_answer_field_and_summary_field_are_two_different_sentences(self):
        """夹具保真的**底线**：正文（`answer`）与 `sections.summary` 不是同一句话。

        该 run 的正文是 86 字的如实说明、summary 是 11 字的短拒答；被顶掉的是前者。
        过去夹具拿 summary 当正文，于是"被顶掉的正文"这句话在证据上是错的 ——
        本用例把这个区分钉死（改回去 → 变红）。
        """
        self.assertEqual(86, len(MODEL_ANSWER_PROSE), MODEL_ANSWER_PROSE)
        self.assertNotEqual(MODEL_ANSWER_PROSE, MODEL_REFUSAL)
        self.assertNotIn(MODEL_REFUSAL, MODEL_ANSWER_PROSE)
        # 两句都必须是"拒答/无证据"形态：放行判据对**正文**成立（summary 只作字段断言）
        self.assertTrue(qa_synthesis._answer_is_refusal_or_no_evidence(MODEL_ANSWER_PROSE))
        self.assertTrue(qa_synthesis._answer_is_refusal_or_no_evidence(MODEL_REFUSAL))
        self.assertEqual(MODEL_REFUSAL, _model_sections()["summary"])


# ── 1. 判定谓词：只认首句里的拒答/无证据形态 ────────────────────────────────────


class RefusalPredicateTests(unittest.TestCase):
    def test_refusal_and_no_evidence_forms_are_recognised(self):
        for text in (
            "无证据支持，无法回答。",
            "【无证据】检索到的资料与问题无关，无法确定该公司是哪一家。",
            "现有证据不足以判断该笔融资对应的公司。",
            "抱歉，无法回答该问题。",
            "经检索，包内没有相关证据，不能判断最近融资近3亿元的公司是哪一家。",
        ):
            self.assertTrue(qa_synthesis._answer_is_refusal_or_no_evidence(text), text)

    def test_conclusion_first_forms_are_not_refusals(self):
        for text in (
            "",
            "是的，需要申报。",
            "最近融资近3亿元的公司是长实集团，其港岛半山波老道21号项目第2期应天获买家连购两伙。",
            # 先给结论、末句才补一句"证据不足" —— 首句没有拒答形态，照旧按内容判据处理
            "香港家族办公室税收优惠政策对内地高净值客户有影响，需结合信托架构逐条核验。"
            "但现有证据不足以给出确定结论。",
        ):
            self.assertFalse(qa_synthesis._answer_is_refusal_or_no_evidence(text), text)


# ── 2. 核心：合法拒答不再被顶掉（真 run 夹具 + 极短夹具）────────────────────────


class RefusalProseSurvivesTests(unittest.TestCase):
    def test_real_run_refusal_is_not_replaced_by_structured_render(self):
        """同一份真 run 夹具：模型写的那句 86 字正文必须留在用户看到的正文里。

        （判据维度见后两条：极短判据那一条由 `test_short_refusal_survives_the_length_condition`
        钉住，本 run 真正命中的"政策/风险问句缺行内引用"由
        `test_real_run_trigger_shape_refusal_is_not_replaced` 钉住。本用例另外守住两件事：
        程序补的"关键依据"块在场时也**不许**凭空多出一次顶掉；
        `answer` 与 `sections.summary` 两个字段各按各的断言，不许互相顶替。）
        """
        result = validate_final_answer(_lock(MODEL_ANSWER_PROSE, question=QUESTION_WITH_PLAN))

        self.assertTrue(result["answer"].startswith(MODEL_ANSWER_PROSE), result["answer"])
        self.assertNotIn(qa_synthesis._STRUCTURED_LEAD.format(label="RAG检索"), result["answer"])
        self.assertNotIn(qa_synthesis._FALLBACK_LEAD.format(label="RAG检索"), result["answer"])
        self.assertNotIn("- " + QUESTION + "\n", result["answer"], "问题本身不许再被当成核心结论列出")
        # **另一个字段**（`sections.summary`）照旧保留它自己那句 11 字（模型写的，谁也没动过）
        self.assertEqual(MODEL_REFUSAL, result["sections"]["summary"])
        self.assertNotIn(MODEL_REFUSAL, result["answer"],
                         "summary 那句不是正文：不许被当成'正文在'的替身")
        # 没有顶掉 → 不留顶掉痕迹
        self.assertNotIn("prose_override", result["sections"]["diagnostics"])
        # 正常路径的形状不变：模型的 6 个键 + 程序 3 个键
        self.assertEqual(9, len(result["sections"]))
        self.assertNotIn("SYNTHESIS_FALLBACK_USED", result["sections"]["diagnostics"]["degradation_codes"])

    def test_real_run_trigger_shape_refusal_is_not_replaced(self):
        """**本 run 真正命中的那条判据**（政策/风险问句缺行内引用）上，拒答也要放行。

        为什么必须是这条：该 run 的 question 带「问题拆解与回答计划」（含"法规"）
        → `_policy_or_risk_question` 为 True；而 `_ensure_inline_citations` 一定会给
        带 refs 的结论补"关键依据"块，所以正文**不可能**停在 20 字以内 ——
        "极短"那条判据在本 run 根本不可能命中（见 FixtureFidelityTests）。
        下面前缀是模型正文的形态（首句拒答 + 提到政策/风险主题、无行内引用）。
        """
        prose = ("检索到的证据与本问题无关，无法确定最近融资近3亿元的公司是哪一家；"
                 "现有资料主要涉及离岸信托税务政策与家族办公室合规，需要补充直接证据。")
        result = _lock(prose, question=QUESTION_WITH_PLAN)

        self.assertTrue(result["answer"].startswith(prose), result["answer"])
        self.assertNotIn(qa_synthesis._STRUCTURED_LEAD.format(label="RAG检索"), result["answer"])
        self.assertNotIn("prose_override", result["sections"]["diagnostics"])

    def test_short_refusal_survives_the_length_condition(self):
        """极短判据这一条：正文停在 20 字以内（无 refs 可补）时，拒答同样不被顶掉。

        这里用的是**该 run 的 `sections.summary` 那句 11 字**当正文（它是真话，只是字段不同）
        加一张空图 —— 目的是把"极短"这条判据单独隔离出来。
        真 run 的**正文**（86 字）走的不是这条判据，见 `test_real_run_refusal_is_not_replaced_by_structured_render`。
        """
        result = validate_final_answer(
            _lock(MODEL_REFUSAL, question=QUESTION, graph=_refusal_only_graph(),
                  sections={"summary": MODEL_REFUSAL}, citations=[]))

        self.assertEqual(MODEL_REFUSAL, result["answer"])
        self.assertLess(len(MODEL_REFUSAL.strip()), 20, "本用例的前提就是'极短'")
        self.assertNotIn("prose_override", result["sections"]["diagnostics"])

    def test_no_evidence_lead_is_not_replaced_either(self):
        """同一条路子的另一种措辞（首句明说无证据）照旧放行。"""
        prose = "【无证据】检索到的资料与问题无关，无法确定该公司是哪一家。"
        result = _lock(prose, question=QUESTION_WITH_PLAN, graph=_refusal_only_graph())
        self.assertEqual(prose, result["answer"])


# ── 3. 对照：该顶掉的照旧顶掉 ────────────────────────────────────────────────


class StillOverriddenTests(unittest.TestCase):
    def test_short_non_refusal_answer_is_still_replaced(self):
        """极短判据没被关掉：非拒答的 8 字正文照旧被结构化渲染顶掉。"""
        result = _lock("是的，需要申报。", question=QUESTION, graph=_refusal_only_graph())

        self.assertTrue(result["answer"].startswith(
            qa_synthesis._STRUCTURED_LEAD.format(label="RAG检索")), result["answer"])
        self.assertEqual("too_short", result["sections"]["diagnostics"]["prose_override"]["trigger"])

    def test_internal_noise_still_wins_over_a_refusal(self):
        """噪声优先级最高：**带内部噪声的拒答**照旧被顶掉（拒答放行不是万能通行证）。"""
        result = _lock("无法回答 claim= c1", question=QUESTION, graph=_refusal_only_graph())

        self.assertTrue(result["answer"].startswith(
            qa_synthesis._STRUCTURED_LEAD.format(label="RAG检索")), result["answer"])
        self.assertEqual("internal_noise", result["sections"]["diagnostics"]["prose_override"]["trigger"])

    def test_policy_question_without_line_citations_is_still_replaced(self):
        """政策/风险判据没被关掉：**非拒答**的政策正文缺行内引用，照旧被顶掉。"""
        prose = "香港家族办公室税收优惠政策对符合条件的管理人给予利得税宽免，需要逐条核对适用范围。"
        result = _lock(prose, question=QUESTION_WITH_PLAN)

        self.assertTrue(result["answer"].startswith(
            qa_synthesis._STRUCTURED_LEAD.format(label="RAG检索")), result["answer"])
        record = result["sections"]["diagnostics"]["prose_override"]
        self.assertEqual("policy_missing_line_citation", record["trigger"])
        # 留痕记的是**判据当时看到的全文**：模型正文 + 程序补的"关键依据"块，所以是前缀关系
        self.assertTrue(record["replaced_excerpt"].startswith(prose), record["replaced_excerpt"])
        self.assertGreaterEqual(record["chars"], len(prose))

    def test_conclusion_first_then_no_evidence_is_still_replaced(self):
        """"先给结论、末句才补证据不足"不算拒答：首句是断言，照旧按内容判据顶掉。"""
        prose = ("香港家族办公室税收优惠政策对内地高净值客户有影响，需结合信托架构逐条核验。"
                 "但现有证据不足以给出确定结论。")
        result = _lock(prose, question=QUESTION_WITH_PLAN)

        self.assertTrue(result["answer"].startswith(
            qa_synthesis._STRUCTURED_LEAD.format(label="RAG检索")), result["answer"])
        self.assertEqual("policy_missing_line_citation",
                         result["sections"]["diagnostics"]["prose_override"]["trigger"])

    def test_normal_prose_is_untouched(self):
        """对照：既不长于必要、又正常的非政策正文，逐字保留（本改动一个字都不该动）。"""
        prose = "最近融资近3亿元的公司是长实集团，其港岛半山波老道21号项目第2期应天获买家连购两伙。"
        result = _lock(prose, question=QUESTION, graph=_graph(claims=[], evidence=[]))

        self.assertEqual(prose, result["answer"])
        self.assertNotIn("prose_override", result["sections"]["diagnostics"])


# ── 4. 可观测性：被顶掉的原文与命中的判据必须落进既有载体 ────────────────────────


class ProseOverrideObservabilityTests(unittest.TestCase):
    def test_excerpt_and_trigger_land_in_sections_diagnostics(self):
        """本 run 的正文（拒答 + 程序补的"关键依据"块）被顶掉时，原文片段与判据都要留痕。"""
        # 用一条**非拒答**的政策正文来保证走顶掉分支（拒答分支见第 2 节）
        prose = "离岸信托的税务合规要求需要逐条核对，涉及申报、资料留存与征管口径。"
        result = validate_final_answer(_lock(prose, question=QUESTION_WITH_PLAN))

        record = result["sections"]["diagnostics"]["prose_override"]
        self.assertEqual({"trigger", "chars", "replaced_excerpt"}, set(record))
        self.assertEqual("policy_missing_line_citation", record["trigger"])
        # 留痕记的是**判据当时看到的全文**（模型正文 + 程序补的"关键依据"块），逐字保留在片段里
        self.assertTrue(record["replaced_excerpt"].startswith(prose), record["replaced_excerpt"])
        self.assertGreaterEqual(record["chars"], len(prose))
        self.assertNotIn(prose, result["answer"], "被顶掉的原文不许回流到正文")
        # 留痕只进 diagnostics：降级口径一字未动
        self.assertEqual([], result["sections"]["diagnostics"]["degradation_codes"])
        self.assertFalse(result["degraded"])
        self.assertEqual([], result["degradation_reasons"])

    def test_excerpt_records_program_appended_block_too(self):
        """留痕记录的是**判据当时看到的全文**：程序补的"关键依据"块也在里面（这正是判据依据）。"""
        result = _lock(MODEL_ANSWER_PROSE, question=QUESTION_WITH_PLAN, graph=_graph(claims=[], evidence=[]))
        # 没有 claim → 不会补块，这里用真夹具那条有 refs 的 claim 单独验证一次
        self.assertTrue(result["answer"])  # 空图：答案保持拒答（不被顶掉）
        self.assertIn(MODEL_ANSWER_PROSE, result["answer"])
        record_graph = _graph(claims=[_claims()[1]], evidence=_evidence())
        overridden = _lock("离岸信托税务政策会影响家族办公室的架构复核与申报安排。",
                         question=QUESTION_WITH_PLAN, graph=record_graph)
        excerpt = overridden["sections"]["diagnostics"]["prose_override"]["replaced_excerpt"]
        self.assertIn("关键依据：", excerpt, "程序补的引用块必须在留痕里看得见：" + excerpt)
        self.assertIn("[8]", excerpt)

    def test_excerpt_is_capped_while_chars_keeps_the_full_length(self):
        prose = "离岸信托的税务合规要求需要逐条核对，" + "补充说明" * 120  # 远超 200 字
        result = _lock(prose, question=QUESTION_WITH_PLAN)
        record = result["sections"]["diagnostics"]["prose_override"]

        self.assertEqual(qa_synthesis._OVERRIDDEN_EXCERPT_CHARS, len(record["replaced_excerpt"]))
        self.assertTrue(record["replaced_excerpt"].startswith(prose[:100]))
        self.assertGreater(record["chars"], qa_synthesis._OVERRIDDEN_EXCERPT_CHARS,
                           "chars 报的是**原长度**（用于判断走的是哪条判据），片段才截断")
        self.assertLessEqual(record["chars"], 80000, "留痕不许把载荷撑出冻结契约的 answer 上限口径")

    def test_no_override_means_diagnostics_gets_no_extra_key(self):
        """不顶掉的路径上 diagnostics 只多一个键都不行（"逐字不变"那类对照片的护栏）。"""
        result = _lock(MODEL_ANSWER_PROSE, question=QUESTION_WITH_PLAN)
        self.assertEqual(["degradation_codes"], sorted(result["sections"]["diagnostics"]))


# ── 5. 两条路径必须一眼可分 ───────────────────────────────────────────────────


class PathDistinguishabilityTests(unittest.TestCase):
    """兜底（模型没给出可用输出）与结构化渲染（模型答了但正文被顶掉）不许再同名。"""

    def _fallback(self):
        return validate_final_answer(qa_synthesis.fallback_final_answer(
            graph=_graph(), level1={"gaps": []}, level2={"evidence_gaps": []}, degradation=[],
            models={"research": "RAG检索"},
            reason="最终综合模型两次尝试都未通过契约校验，已改用证据约束的结构化重组。",
            question=QUESTION,
        ))

    def test_two_paths_have_different_first_lines(self):
        render_first = qa_synthesis._STRUCTURED_LEAD.format(label="RAG检索")
        fallback_first = qa_synthesis._FALLBACK_LEAD.format(label="RAG检索")

        self.assertNotEqual(qa_synthesis._STRUCTURED_LEAD, qa_synthesis._FALLBACK_LEAD,
                            "两条路的模板本身就不许相同（把固定首行改回旧文案 → 本用例变红）")
        self.assertNotEqual(render_first, fallback_first)
        # 兜底那条沿用历史文案：它就是这条路自己的指纹
        self.assertEqual("已完成RAG检索和证据图综合。", fallback_first)

    def test_fallback_keeps_its_own_fingerprint(self):
        result = self._fallback()
        codes = result["sections"]["diagnostics"]["degradation_codes"]

        self.assertTrue(result["answer"].startswith(
            qa_synthesis._FALLBACK_LEAD.format(label="RAG检索")), result["answer"])
        self.assertEqual(qa_synthesis._FALLBACK_LEAD.format(label="RAG检索"), result["sections"]["summary"])
        self.assertIn("SYNTHESIS_FALLBACK_USED", codes)
        self.assertEqual(4, len(result["sections"]), "兜底路径的 sections 只有程序自己的 4 个键")
        self.assertTrue(result["degraded"])

    def test_render_path_does_not_look_like_the_old_template(self):
        """渲染路径的首行**不再**是那句旧模板文案（改回去 → 本用例变红）。"""
        result = _lock("是的，需要申报。", question=QUESTION, graph=_refusal_only_graph())
        first_line = result["answer"].splitlines()[0]

        self.assertEqual(qa_synthesis._STRUCTURED_LEAD.format(label="RAG检索"), first_line)
        self.assertNotEqual("已完成RAG检索和证据图综合。", first_line)
        self.assertNotEqual(self._fallback()["answer"].splitlines()[0], first_line)
        # 首行仍是"路径指纹"的载体，但**判别**它的是 `answer_path_of`（按整篇里的那一句，
        # 不按行位置）—— 证据摘录那条路上横幅会顶掉首行，见 PathFingerprintTests
        self.assertEqual(qa_synthesis.ANSWER_PATH_RENDER,
                         qa_synthesis.answer_path_of(result["answer"]))
        # 渲染路径保留模型自己的 summary（模型答过），这条与兜底路径天然不同
        self.assertEqual(MODEL_REFUSAL, result["sections"]["summary"])


# ── 6. 路径判别：看**正文里出现哪一句**，不看首行 ─────────────────────────────


def _render_payload():
    """渲染路径的真产物：非拒答的极短正文被 `_answer_override_trigger` 判为 `too_short` 后重组。"""
    return validate_final_answer(_lock("是的，需要申报。", question=QUESTION, graph=_refusal_only_graph()))


def _fallback_payload():
    """兜底路径的真产物：`fallback_final_answer`（模型两次都没给出可用输出）。"""
    return validate_final_answer(qa_synthesis.fallback_final_answer(
        graph=_graph(), level1={"gaps": []}, level2={"evidence_gaps": []}, degradation=[],
        models={"research": "RAG检索"},
        reason="最终综合模型两次尝试都未通过契约校验，已改用证据约束的结构化重组。",
        question=QUESTION,
    ))


class PathFingerprintTests(unittest.TestCase):
    """**两条路怎么一眼区分**：`answer_path_of` 按"正文里出现的是哪一句"判，不按首行。

    为什么不能用首行：证据摘录那条路（`EVIDENCE_ONLY_NO_MODEL`）上，
    `qa_pipeline._mark_evidence_only_payload` 会把 `【未经模型综合】…` 横幅块插到正文最前面，
    首行于是变成横幅 —— "首行就是路径指纹"这条前提在那条路上整个失效
    （`test_evidence_only_banner_does_not_hide_the_path` 就是这根的钉子）。
    """

    def test_each_path_is_recognised_by_its_own_sentence(self):
        self.assertEqual(qa_synthesis.ANSWER_PATH_RENDER,
                         qa_synthesis.answer_path_of(_render_payload()["answer"]))
        self.assertEqual(qa_synthesis.ANSWER_PATH_FALLBACK,
                         qa_synthesis.answer_path_of(_fallback_payload()["answer"]))

    def test_passthrough_body_belongs_to_neither_path(self):
        """模型正文被原样保留的正文：两条路都不是（返回空串），不许硬认成其中一条。"""
        result = _lock(MODEL_ANSWER_PROSE, question=QUESTION_WITH_PLAN)

        self.assertEqual("", qa_synthesis.answer_path_of(result["answer"]))

    def test_evidence_only_banner_does_not_hide_the_path(self):
        """**证据摘录那条路上的钉子**：横幅占掉首行时，判别照样成立（改回"只看首行" → 变红）。

        直接调生产同一段代码（`qa_pipeline._mark_evidence_only_payload`）来加横幅，
        而不是手写一个像横幅的字符串：这样"横幅长什么样、插在哪"永远与生产一致。
        """
        marked = qa_pipeline._mark_evidence_only_payload(
            _fallback_payload(), stage="synthesis",
            code=qa_pipeline.EVIDENCE_ONLY_NO_MODEL, detail="HTTPConnectionPool: 连接被拒绝")
        answer = marked["answer"]
        fallback_lead = qa_synthesis._FALLBACK_LEAD.format(label="RAG检索")

        self.assertTrue(answer.startswith(qa_pipeline.EVIDENCE_ONLY_HEADER), answer[:80])
        self.assertNotEqual(fallback_lead, answer.splitlines()[0],
                            "前提：横幅已经占掉首行 —— 所以'看首行判路'这条路本身失效了")
        self.assertEqual(qa_synthesis.ANSWER_PATH_FALLBACK, qa_synthesis.answer_path_of(answer))
        self.assertIn(fallback_lead, answer)
        # 横幅与固定文案都如实落到载荷上（判别不许依赖它们的位置）
        self.assertIn(qa_pipeline.EVIDENCE_ONLY_NOTE, answer)
        self.assertEqual(qa_pipeline.EVIDENCE_ONLY_NOTE, marked["degradation_reasons"][0])

    def test_judgement_does_not_depend_on_the_line_the_sentence_sits_on(self):
        """判别只看"整篇里有没有这一句"：挪到中间、挪到末尾、前面垫多少行都照样认得出。"""
        render_lead = qa_synthesis._STRUCTURED_LEAD.format(label="RAG增强检索")
        fallback_lead = qa_synthesis._FALLBACK_LEAD.format(label="RAG增强检索")

        bodies = (
            ("【未经模型综合】本条回答由已核验证据摘录生成，未经模型综合。\n\n前缀说明\n\n"
             + render_lead + "\n核心结论：…"),
            "a\nb\nc\n" + render_lead,
            "【未经模型综合】…\n" + fallback_lead + "\n核心结论：…",
        )
        self.assertEqual(qa_synthesis.ANSWER_PATH_RENDER, qa_synthesis.answer_path_of(bodies[0]))
        self.assertEqual(qa_synthesis.ANSWER_PATH_RENDER, qa_synthesis.answer_path_of(bodies[1]))
        self.assertEqual(qa_synthesis.ANSWER_PATH_FALLBACK, qa_synthesis.answer_path_of(bodies[2]))

    def test_one_implementation_two_paths_as_a_parameter(self):
        """**一条实现、一个入口**：渲染 / 兜底是同一个 `_build_structured_answer` 的 `path` 取值。

        同一份输入下两条路的正文**除首行外逐字相同** —— 这正是"把两条并列路径收敛成
        同一路径的参数"的可断言形态；未知 `path` 直接报错，不给第三条路留门。
        """
        common = dict(question=QUESTION, claims=_claims(), evidence=_evidence(),
                      citation_map=REAL_CITATION_MAP, models={"research": "RAG检索"})

        self.assertEqual({qa_synthesis.ANSWER_PATH_RENDER, qa_synthesis.ANSWER_PATH_FALLBACK},
                         set(qa_synthesis._ANSWER_LEADS))
        render = qa_synthesis._build_structured_answer(
            path=qa_synthesis.ANSWER_PATH_RENDER, **common)
        fallback = qa_synthesis._build_structured_answer(
            path=qa_synthesis.ANSWER_PATH_FALLBACK, **common)

        self.assertEqual(render.split("\n", 1)[1], fallback.split("\n", 1)[1],
                         "两条路只许差首行（其余逐字相同）")
        self.assertEqual([qa_synthesis._STRUCTURED_LEAD.format(label="RAG检索")],
                         render.splitlines()[:1])
        self.assertEqual([qa_synthesis._FALLBACK_LEAD.format(label="RAG检索")],
                         fallback.splitlines()[:1])
        with self.assertRaises(ValueError):
            qa_synthesis._build_structured_answer(path="third_path", **common)


if __name__ == "__main__":
    unittest.main()
