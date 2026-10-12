#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：**同一个功能不许有并列的多条路径**（QA 前端只保留一条，后端为准）。

钉住两件事（都是静态断言 + 一处后端行为断言，零浏览器、零网络、零模型）：

  1. **前端不再"重组"结构化正文**：`templates/mapindex.html` 里那份
     `structuredAnswerJsonToMarkdown`（把结构化 JSON 再拼一遍 markdown）已删除。
     为什么可以删、后端凭什么已经给出干净正文：
       · 后端 `qa_synthesis._render_structured_answer_value` 是正文渲染的唯一实现
         （模型 `answer` 字段无论是 JSON 还是散文，出去的都是 markdown 散文）；
       · 后端 `QaFinalSynthesizer.generate`（`qa_synthesis.py` 流式分支）一见模型输出
         首字符是 `{`/`[` 就置 `structured_answer_stream = True` 并**停发** `answer_delta`，
         所以前端连"半截 JSON"都收不到（收不到就没有"重组"的需求）；
       · 前端拿 `qaFinalResult.answer`（后端载荷）直接显示；JSON 万一真显示出来也按原文显示，
         **绝不重组**（重组一次就会和后端正文措辞不一致 = 第二条路径）。
  2. **文案替换规则只有一份真源**：真源是后端 `qa_synthesis._public_text`。
     前端过去那份逐字重复的规则表（`cleanChatUiText`）已整表删除，只留"把任意值变成字符串"
     的 `chatUiText()` 兜底 —— 本文件按"规则行"逐条断言它不会长回来。

变异验证（把任一条改回去 → 本文件必须变红）：
  · 前端加回 `structuredAnswerJsonToMarkdown` / `normalizeAssistantAnswerText`；
  · 前端加回任一 `replace(/…)` 旧规则（RAGFlow / 降级摘要 / 一级模型返回格式异常 /
    一级分析未稳定返回结构化结果 / 研究模型格式未完全通过校验 / 证据锚定摘要 / 连接 local 超时）；
  · 把后端渲染实现删掉（第 3 节的"后端为准"断言就无源可依）。
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_synthesis  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "templates", "mapindex.html")

with open(TEMPLATE, encoding="utf-8") as _fh:
    SOURCE = _fh.read()

REMOVED_RULE_LINES = (
    # 前端旧 `cleanChatUiText` 的 12 条规则：逐条点名，任一条回来都算"第二条路径"
    "replace(/RAGFlow",
    "replace(/RAG检索增强",
    "replace(/RAG增强检索",
    "replace(/降级摘要",
    "replace(/一级模型返回格式异常",
    "replace(/一级分析未稳定返回结构化结果",
    "replace(/[^。；]*研究模型格式未完全通过校验",
    "replace(/证据锚定摘要",
    "replace(/连接",
)
"""后端 `_public_text` 已经拥有、前端不许再抄一份的规则行（前缀逐字）。"""


def _function_body(source, name):
    """粗粒度取出 `function <name>(…) {…}` 的函数体（按大括号配平，够用且不引依赖）。"""
    match = re.search(r"function\s+%s\s*\([^)]*\)\s*\{" % re.escape(name), source)
    if not match:
        return ""
    depth, index = 0, match.end() - 1
    while index < len(source):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[match.end():index]
        index += 1
    return ""


class FrontendNoReassemblyTests(unittest.TestCase):
    """前端不再把结构化 JSON 拼成 markdown（正文以后端为准）。

    断言统一用 `assertTrue(... not in SOURCE)` 而不是 `assertNotIn(..., SOURCE)`：
    后者失败时会把 400KB 的模板整篇打进报告，看不出到底哪一条挂了。
    """

    def test_json_to_markdown_function_is_gone(self):
        self.assertTrue("function structuredAnswerJsonToMarkdown" not in SOURCE,
                        "前端的 JSON→markdown 重组函数长回来了")
        self.assertTrue("function normalizeAssistantAnswerText" not in SOURCE,
                        "前端的重组包装函数长回来了")

    def test_no_call_sites_of_the_removed_helpers(self):
        # 只允许注释里提到旧名字（解释"为什么删"），不允许任何调用形态
        self.assertTrue("structuredAnswerJsonToMarkdown(" not in SOURCE)
        self.assertTrue("normalizeAssistantAnswerText(" not in SOURCE)

    def test_answer_render_path_uses_backend_text_verbatim(self):
        """正文渲染只做字符串兜底：`chatUiText(后端答案)`，不解析、不重组。"""
        body = _function_body(SOURCE, "renderAssistantContent")

        self.assertTrue(body, "renderAssistantContent 必须还在（正文渲染入口）")
        self.assertTrue("const uiContent = chatUiText(content || '');" in body)
        self.assertTrue("JSON.parse" not in body, "正文渲染路径不许解析 JSON")

    def test_chat_answer_flow_has_no_json_parsing_of_the_body(self):
        """旧函数名只许活在"解释为什么删"的注释里；任何非注释行出现它 = 有人把重组加回来了。"""
        offenders = [
            "%d: %s" % (number, line.strip())
            for number, line in enumerate(SOURCE.splitlines(), 1)
            if "structuredAnswerJsonToMarkdown" in line and not line.strip().startswith("//")
        ]

        self.assertEqual([], offenders)


class FrontendTextRulesSingleSourceTests(unittest.TestCase):
    """文案替换规则：真源在后端 `qa_synthesis._public_text`，前端不再抄第二份。"""

    def test_legacy_replace_rules_are_not_in_the_template(self):
        offenders = [rule for rule in REMOVED_RULE_LINES if rule in SOURCE]

        self.assertEqual([], offenders, "旧规则长回来了（= 第二份规则表）")

    def test_old_cleaner_function_is_gone(self):
        self.assertTrue("cleanChatUiText" not in SOURCE)

    def test_remaining_helper_is_pure_string_coercion(self):
        body = _function_body(SOURCE, "chatUiText")

        self.assertTrue(body, "chatUiText 必须还在（UI 文本的字符串兜底）")
        self.assertTrue(".replace(" not in body, "兜底函数不许再带任何替换规则")
        self.assertEqual("return String(value || '');", body.strip())

    def test_template_points_at_the_backend_rule_source(self):
        """删掉规则必须**指名道姓**指向真源，否则下一个人只会在前端又抄一份。"""
        self.assertTrue("qa_synthesis._public_text" in SOURCE)

    def test_backend_rule_source_still_owns_the_rules(self):
        """前端不再替换的那些内部文案，后端一条不少（删前端不是删能力）。"""
        cases = (
            ("RAGFlow 深度检索", "RAG增强检索"),
            ("一级模型返回格式异常：坏形状", "资料已按证据锚定方式整理。"),
            ("降级摘要", "证据锚定摘要"),
            ("连接 local 超时，请检查网络后重试。", "综合模型响应超时，已改用证据约束重组。"),
        )
        for raw, expected in cases:
            self.assertIn(expected, qa_synthesis._public_text(raw), raw)


class BackendOwnsTheBodyTests(unittest.TestCase):
    """后端是正文渲染的唯一实现：结构化 JSON 进来，出去的是 markdown 散文。"""

    STRUCTURED = (
        '{"summary": "核心结论一句话。", '
        '"sections": [{"title": "分析", "content": "分节正文。\\n降级摘要"}], '
        '"conflicts": [{"subject": "冲突"}]}'
    )

    def test_structured_json_becomes_markdown_at_the_backend(self):
        rendered = qa_synthesis._render_structured_answer_value(
            self.STRUCTURED, {"answer": self.STRUCTURED})

        self.assertIn("核心结论：", rendered)
        self.assertIn("核心结论一句话。", rendered)
        self.assertIn("分析", rendered)
        self.assertIn("证据锚定摘要", rendered, "后端负责把内部文案洗成对外文案")
        self.assertNotIn("{", rendered)
        self.assertNotIn('"sections"', rendered)
        # 结构化 JSON 的正文在**后端**已经变成散文 → 前端没有任何"重组"的必要
        self.assertTrue(rendered.startswith("核心结论："), rendered[:40])

    def test_plain_prose_passes_through_untouched(self):
        prose = "最近融资近3亿元的公司是长实集团。"

        self.assertEqual(prose, qa_synthesis._render_structured_answer_value(
            prose, {"answer": prose}))


if __name__ == "__main__":
    unittest.main()
