#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：模型把 `citations` 填成正文标签 `[n]`、且根本没给 `sections`（B 机真实 run）。

背景（B 机 run d980edf5c1b44949acccbf212daeb3f5，122.7 秒，模型真被调用：`synthesis`
阶段 57853ms、tokens in 20731 / out 164，ds4 侧三次调用全 `finish=stop`、零错误，
答案却仍是模板 `已完成RAG检索和证据图综合。`）：

真因链（本文件钉住前三条）：
  1. 模型把 `citations` 填成了 **`citation_map` 的 key**（正文标签 `[1]`），而字段要的是
     **evidence_ref**（`article:1765`）→ `_lock_to_graph` 判"引用了不存在的 evidence_ref: [1]"
     → 抛 `QaContractError` → 真实答案被丢掉、换成模板；
  2. 模型的原始输出**连 `sections` 键都没有**（只有 answer / claims / citations 三个字段）
     → 即便越过第 1 条，也会立刻撞上 sections 形状守卫；
  3. 修复重试**逐字复读**（两次 raw_excerpt 只差一层 ```json 围栏）—— 旧修复提示只给一句
     error，既没有合法 `evidence_ref` 清单、也没说少了哪些顶层字段，模型无事可做。

夹具 = 该 run `qa_stage_runs.details_json.model_raw_trace` 里的**真实原始输出**（逐字），
证据/结论按同 run 的 `qa_stage_runs.details_json.result` 复原（`citation_map` 与真实值逐一相等，
见 `test_fixture_reproduces_the_real_citation_map`）。

全程零模型 / 零嵌入 / 零网络端点（模型客户端全部打桩）。
"""
import json
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_synthesis  # noqa: E402
from qa_contracts import QaContractError, validate_final_answer  # noqa: E402

QUESTION = "最近融资近3亿元的公司是哪个公司"

# ── 真实夹具（逐字取自 B 机 run d980edf5c1b44949acccbf212daeb3f5 的 model_raw_trace）───
MODEL_ANSWER = (
    "最近融资近3亿元的公司是长实集团，其港岛半山波老道21号超级豪宅项目第2期应天，"
    "获一位买家连购两伙，总成交价近3亿元。"
)
RAW_ATTEMPT_1 = (
    '{\n'
    '  "answer": "最近融资近3亿元的公司是长实集团，其港岛半山波老道21号超级豪宅项目第2期应天，'
    '获一位买家连购两伙，总成交价近3亿元。",\n'
    '  "citations": [\n'
    '    "[1]"\n'
    '  ],\n'
    '  "claims": [\n'
    '    "l1-c1"\n'
    '  ]\n'
    '}'
)
"""第 1 次尝试的原始输出：**只有三个顶层字段**，citations 是标签 `[1]`，没有 sections。"""

RAW_ATTEMPT_2 = "```json\n" + RAW_ATTEMPT_1 + "\n```"
"""第 2 次（修复重试）的原始输出：与第 1 次**逐字相同**，只多了一层代码围栏。"""

MODEL_SUMMARY = "近期以近3亿元总价成交的是长实集团港岛半山波老道21号项目「应天」第2期，获同一买家连购两伙。"
"""模型自己写的 sections.summary（修复后应当由模型给出，而不是程序的模板句）。"""

TEMPLATE_SUMMARY = "已完成RAG检索和证据图综合。"
"""程序骨架答案的模板指纹：最终 summary 命中它 = 又退化成模板了。"""

REAL_CITATION_MAP = {
    "[1]": "article:1765", "[2]": "article:1794", "[3]": "article:2610",
    "[4]": "article:2855", "[5]": "article:2905", "[6]": "article:2863",
    "[7]": "article:2930", "[8]": "article:2355",
}
"""真实 run 的 citation_map（`qa_stage_runs.details_json.result.citation_map`）。"""

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


def _claim():
    """真实 run 的 level1 结论 `l1-c1`：正文与模型答案同句，证据指向 article:1765。"""
    return {
        "claim_id": "l1-c1", "text": MODEL_ANSWER, "claim_type": "current_fact",
        "confidence": 0.95, "valid_from": None, "valid_to": None, "scope": ["a", "l"],
        "evidence_refs": ["article:1765"], "needs_verification": False,
        "verification_status": "unverified",
    }


def _graph():
    return {"claims": [_claim()], "evidence": _evidence(), "conflicts": []}


def _lock(parsed, *, question=QUESTION):
    """直接跑 `_lock_to_graph`（模型返回字段的解析代码本体）。"""
    return qa_synthesis.QaFinalSynthesizer._lock_to_graph(
        parsed, _graph(), {"gaps": []}, {"evidence_gaps": []}, [], {"research": "RAG检索"},
        question=question, context_pack=None,
    )


def _compliance_payload():
    """模型按新提示（字面量 required_shape + 合法 ref 清单）应给出的合规补全版：
    正文与标签沿用真实输出，只补齐顶层字段 —— 也就是"修复重试真能修"之后的样子。"""
    return {
        "contract_version": "unified-qa-v1", "status": "ready",
        "answer": MODEL_ANSWER, "sections": {"summary": MODEL_SUMMARY},
        "claims": ["l1-c1"], "conflicts": [], "citations": ["[1]"],
    }


# ── 1. 夹具保真 + 方向 2：标签逆映射（底线不动）────────────────────────────────


class CitationLabelNormalizationTests(unittest.TestCase):
    def test_fixture_reproduces_the_real_citation_map(self):
        """夹具的证据顺序必须复现真实 run 的 citation_map，否则 [1]→article:1765 无从谈起。"""
        self.assertEqual(REAL_CITATION_MAP, qa_synthesis._citation_map(_evidence()))

    def test_label_is_reverse_mapped_to_evidence_ref(self):
        refs, translated = qa_synthesis._normalize_citation_labels(["[1]"], REAL_CITATION_MAP)
        self.assertEqual(["article:1765"], refs)
        self.assertEqual(["[1]"], translated, "翻译了哪几个标签必须回报，供如实记账")

    def test_real_raw_output_plus_sections_passes_and_is_accounted(self):
        """真实原始输出（只剩 sections 缺失这件事）→ 通过校验，且归一化如实记账。"""
        parsed = json.loads(RAW_ATTEMPT_1)
        parsed["sections"] = {"summary": MODEL_SUMMARY}
        result = validate_final_answer(_lock(parsed))

        # ① 标签被逆映射成 evidence_ref，并通过契约校验（引用完整性也过）
        self.assertEqual(["article:1765"], result["citations"])
        # ② 归一化**不静默**：degradation_reasons + diagnostics.degradation_codes 都留痕
        codes = result["sections"]["diagnostics"]["degradation_codes"]
        self.assertIn("SYNTHESIS_CITATION_LABEL_NORMALIZED", codes, codes)
        reasons = " ".join(result["degradation_reasons"])
        self.assertIn("[1]→article:1765", reasons, reasons)
        self.assertIn("逆映射", reasons)
        # ③ 模型正文被保住（没有被模板替换）
        self.assertIn(MODEL_ANSWER, result["answer"])
        self.assertNotIn(TEMPLATE_SUMMARY, result["answer"])
        self.assertEqual(MODEL_SUMMARY, result["sections"]["summary"])
        # 归一化是"程序兜住了"，不是"模型答得没问题" → 状态照实降级
        self.assertTrue(result["degraded"])
        self.assertEqual("partial", result["status"])

    def test_unmapped_label_still_fails(self):
        """底线不动：映射不到的标签照旧判失败，绝不因为"像标签"就放行。"""
        parsed = json.loads(RAW_ATTEMPT_1)
        parsed["citations"] = ["[9]"]
        parsed["sections"] = {"summary": MODEL_SUMMARY}
        with self.assertRaises(QaContractError) as ctx:
            _lock(parsed)
        self.assertIn("不存在的 evidence_ref: [9]", str(ctx.exception))

    def test_mixed_refs_and_labels_keep_both(self):
        """混合形态：真 ref 原样保留，标签翻译后去重。"""
        parsed = json.loads(RAW_ATTEMPT_1)
        parsed["citations"] = ["article:2355", "[1]", "[1]"]
        parsed["sections"] = {"summary": MODEL_SUMMARY}
        result = _lock(parsed)
        self.assertEqual(["article:2355", "article:1765"], result["citations"])
        self.assertIn("共 1 个", " ".join(result["degradation_reasons"]),
                      "同一标签重复出现算一次翻译")

    def test_no_label_means_byte_identical_behaviour(self):
        """对照：citations 本来就是 evidence_ref 时，账本一个键都不许多。"""
        parsed = json.loads(RAW_ATTEMPT_1)
        parsed["citations"] = ["article:1765"]
        parsed["sections"] = {"summary": MODEL_SUMMARY}
        result = _lock(parsed)
        self.assertEqual("ready", result["status"])
        self.assertFalse(result["degraded"])
        self.assertEqual([], result["degradation_reasons"])


# ── 2. 方向 1 / 3（甲）/ 5：提示端消歧与"真能修"的修复提示 ─────────────────────


def _messages(**kwargs):
    return qa_synthesis.QaFinalSynthesizer._messages(
        question=QUESTION, graph=_graph(), level1={"gaps": []}, level2={"evidence_gaps": []},
        degradation=[], **kwargs)


class PromptDisambiguationTests(unittest.TestCase):
    def test_required_shape_is_a_filled_example_with_an_evidence_ref(self):
        """甲：required_shape 不再是空骨架 —— 顶层字段齐全且都有**合法字面量**示例。"""
        body = json.loads(_messages()[1]["content"])
        shape = body["required_shape"]
        self.assertEqual(
            {"contract_version", "status", "answer", "sections", "claims", "conflicts", "citations"},
            set(shape))
        self.assertEqual("ready", shape["status"], "status 的示例必须是合法枚举值，不是 'ready|partial|…'")
        self.assertEqual(["article:1765"], shape["citations"], "citations 的示例只能是 evidence_ref")
        self.assertEqual(["l1-c1"], shape["claims"], "claims 的示例必须是真的 claim_id，不能是 'claim_id' 占位")
        self.assertTrue(str(shape["sections"]["summary"]).strip(), "sections.summary 的示例不许是空字符串")
        self.assertTrue(str(shape["answer"]).strip())

    def test_no_evidence_does_not_invent_a_reference(self):
        """零证据时不许在示例里凭空给一个 article:NNNN —— 否则模型照抄示例＝提示自己制造违规。"""
        messages = qa_synthesis.QaFinalSynthesizer._messages(
            question=QUESTION, graph={"claims": [], "evidence": [], "conflicts": []},
            level1={}, level2={}, degradation=[])
        body = json.loads(messages[1]["content"])
        self.assertEqual([], body["required_shape"]["citations"])
        self.assertNotIn("article:1765", messages[0]["content"])
        self.assertIn("citation_map 为空", messages[0]["content"])

    def test_system_prompt_forbids_labels_in_citations(self):
        system = _messages()[0]["content"]
        self.assertIn("绝对不要填 [n] 标签", system)
        self.assertIn("article:1765", system)
        self.assertIn("缺任意一个即视为不合格", system, "甲：缺任意顶层字段即不通过必须写明")
        self.assertIn("sections.summary", system)

    def test_repair_prompt_carries_the_legal_values(self):
        """方向 5：修复提示必须带合法 evidence_ref 清单 + 必填顶层字段清单。"""
        messages = _messages(repair_error="QaContractError: 最终模型引用了不存在的 evidence_ref: [1]",
                             prior=RAW_ATTEMPT_1)
        repair = messages[1]["content"].split("只修复 JSON", 1)[1]
        for ref in REAL_CITATION_MAP.values():
            self.assertIn(ref, repair, "合法 evidence_ref 少一个，模型就没有可修的依据：" + repair[:400])
        for field in ("contract_version", "status", "answer", "sections", "claims", "conflicts", "citations"):
            self.assertIn(field, repair, repair[:400])
        self.assertIn("禁止填 [n] 标签", repair)
        self.assertIn(RAW_ATTEMPT_1[:40], repair, "原输出照旧要带上")
        self.assertIn("QaContractError", repair, "出问题的原因照旧要带上")


# ── 3. 核心验收：用真实 raw_excerpt 跑完整 generate() ─────────────────────────


class _StubProfile:
    provider_id = "stub"
    model_id = "stub"
    base_url = ""
    api_key = ""


class _RealModelStub:
    """按本 run 的**真实行为**打桩的模型客户端。

    第 1 次：逐字返回真实原始输出（三个字段、citations=["[1]"]、没有 sections）。
    第 2 次（修复重试）：**只有修复提示里给出合法 evidence_ref 清单与必填顶层字段清单时**
    才补齐成一个合规对象；否则原样复读 —— 这正是本 run 观测到的行为
    （两次 raw_excerpt 逐字相同），用来钉住"修复重试要真能修"。
    """

    def __init__(self):
        self.calls = []

    def __call__(self, profile, messages, timeout=90):
        self.calls.append(messages)
        if len(self.calls) == 1:
            return RAW_ATTEMPT_1
        prompt = str(messages[-1]["content"])
        if "必须返回的顶层字段" in prompt and "合法 evidence_ref 清单" in prompt:
            return json.dumps(_compliance_payload(), ensure_ascii=False)
        return RAW_ATTEMPT_2


class _AlwaysBrokenStub:
    """两次都只回真实原始输出（本 run 的实际情形）：契约照旧不许被放松。"""

    def __init__(self):
        self.calls = []

    def __call__(self, profile, messages, timeout=90):
        self.calls.append(messages)
        return RAW_ATTEMPT_2 if len(self.calls) == 2 else RAW_ATTEMPT_1


class RealTraceEndToEndTests(unittest.TestCase):
    def _generate(self, client, sink=None):
        return qa_synthesis.QaFinalSynthesizer(model_client=client).generate(
            question=QUESTION, graph=_graph(), level1={}, level2={}, degradation=[],
            profile=_StubProfile(), models={"research": "RAG检索", "synthesis": "stub"},
            context_pack=None, raw_trace_sink=sink)

    def test_real_trace_ends_with_the_model_answer_not_the_template(self):
        """核心验收：同一份真实原始输出，修复提示补齐后不再退化成模板。"""
        client = _RealModelStub()
        result = validate_final_answer(self._generate(client))

        self.assertEqual(2, len(client.calls), "首答 + 一次修复重试")
        # ① [1] 被逆映射成 article:1765 并通过校验
        self.assertEqual(["article:1765"], result["citations"])
        # ② sections 缺失按"甲"口径处理：契约不放松，靠**补全后的模型输出**满足；
        #    归一化这件事如实记账（不静默）
        self.assertIn("SYNTHESIS_CITATION_LABEL_NORMALIZED",
                      result["sections"]["diagnostics"]["degradation_codes"])
        # ③ 最终答案不再是模板：summary 是模型写的，正文是模型的
        self.assertEqual(MODEL_SUMMARY, result["sections"]["summary"])
        self.assertNotEqual(TEMPLATE_SUMMARY, result["sections"]["summary"])
        self.assertNotIn(TEMPLATE_SUMMARY, result["answer"])
        self.assertIn(MODEL_ANSWER, result["answer"])
        # 首答没过契约 → 原输出留痕不该再出现（第二次已经过了）
        self.assertNotIn(qa_synthesis.RAW_TRACE_KEY, result)

    def test_repair_attempt_is_what_makes_it_fixable(self):
        """修复提示里必须真的带上"可修的依据"（合法 ref 清单 + 必填字段清单）。

        这条是本文件的**变异哨兵**：把方向 5 的实现撤掉（修复提示退回只给一句 error），
        `_RealModelStub` 就会照真实观测那样复读原输出，本用例连同核心验收一起变红。
        """
        client = _RealModelStub()
        result = validate_final_answer(self._generate(client))
        repair_prompt = str(client.calls[1][-1]["content"])
        self.assertIn("合法 evidence_ref 清单", repair_prompt)
        self.assertIn("必须返回的顶层字段", repair_prompt)
        self.assertIn("article:1765", repair_prompt, "合法清单里必须有本轮真实 evidence_ref")
        self.assertEqual(MODEL_SUMMARY, result["sections"]["summary"])

    def test_both_attempts_broken_still_degrade_and_name_the_missing_field(self):
        """对照：两次都不给 sections → 契约照旧不通过，降级原因必须点名『缺哪个字段』。"""
        client = _AlwaysBrokenStub()
        sink: dict = {}
        result = validate_final_answer(self._generate(client, sink=sink))

        self.assertEqual(2, len(client.calls))
        self.assertTrue(result["degraded"], "契约不合格必须仍然降级")
        codes = result["sections"]["diagnostics"]["degradation_codes"]
        self.assertIn("SYNTHESIS_FALLBACK_USED", codes)
        reasons = " ".join(result["degradation_reasons"])
        self.assertIn(qa_synthesis._FALLBACK_FAILED_NOTE, reasons)
        self.assertIn("sections", reasons, reasons)
        self.assertIn("顶层字段缺失", reasons,
                      "必须读得出是『字段整个没给』而不是『形状怪』：" + reasons)
        self.assertIn(qa_synthesis._value_excerpt(None), reasons)
        # 两次尝试的原始输出照样留痕，且逐字等于真实输出
        traces = sink[qa_synthesis.RAW_TRACE_KEY]
        self.assertEqual([1, 2], [item["attempt"] for item in traces])
        self.assertEqual(RAW_ATTEMPT_1, traces[0]["raw_excerpt"])
        self.assertEqual(RAW_ATTEMPT_2, traces[1]["raw_excerpt"])
        self.assertIn("QaContractError", traces[0]["error"])


if __name__ == "__main__":
    unittest.main()
