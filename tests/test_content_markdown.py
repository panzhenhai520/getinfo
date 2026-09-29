import unittest

from content_handlers import (
    MAX_MARKDOWN_CHARS,
    _looks_like_html,
    _looks_like_js_junk,
    _looks_like_markdown,
    build_article_markdown,
)


class BuildArticleMarkdownTests(unittest.TestCase):
    """阶段1：正文统一 Markdown 转换函数单元测试"""

    def test_html_raw_converts_to_markdown(self):
        raw = (
            "<div><h1>自动驾驶新规发布</h1>"
            "<p>近日有关部门发布新规，对L3级自动驾驶提出明确要求。</p>"
            "<ul><li>要求一：明确责任划分</li><li>要求二：强化数据安全</li></ul>"
            '<p>参考<a href="https://example.com/doc">文件原文</a></p></div>'
        )
        result = build_article_markdown(raw, "备用摘要")
        self.assertIn("自动驾驶新规发布", result)
        self.assertIn("明确责任划分", result)
        self.assertIn("[文件原文](https://example.com/doc)", result)
        self.assertNotIn("<div>", result)

    def test_markdown_raw_kept_and_nav_noise_stripped(self):
        raw = (
            "# 行业观察：机器人量产提速\n\n"
            "正文第一段内容，长度足够构成有效段落。\n\n"
            "[首页](https://x.com/)\n"
            "![广告图](https://x.com/ad.jpg)\n"
            "**关键结论：产能翻倍**\n"
        )
        result = build_article_markdown(raw, "摘要")
        self.assertIn("# 行业观察", result)
        self.assertIn("正文第一段内容", result)
        self.assertIn("**关键结论：产能翻倍**", result)
        self.assertNotIn("[首页]", result)
        self.assertNotIn("广告图", result)

    def test_js_junk_falls_back_to_content(self):
        raw = 'window.__data={"page":"list"};\\u4e0b\\u4e00\\u9875 (function(){var a=1;return a;})();'
        content = "真正摘要：该公司完成新一轮融资。\n\n融资将用于产线扩建与研发投入。"
        result = build_article_markdown(raw, content)
        self.assertIn("真正摘要", result)
        self.assertNotIn("window.__data", result)

    def test_plain_text_structured_into_heading_and_paragraph(self):
        content = "融资完成\n\n该公司完成新一轮融资，金额数亿元，资金将用于产线扩建与研发投入。"
        result = build_article_markdown(content, content)
        self.assertTrue(result.startswith("## 融资完成"))
        self.assertIn("该公司完成新一轮融资", result)

    def test_plain_short_colon_line_becomes_bold(self):
        # 首个短块作为标题（##），其后的冒号短块加粗（**…**）
        content = "行业观察\n\n融资背景：\n\n该公司深耕智能制造多年，积累了丰富的客户资源与交付经验。"
        result = build_article_markdown(content, content)
        self.assertTrue(result.startswith("## 行业观察"))
        self.assertIn("**融资背景：**", result)

    def test_empty_inputs_return_empty(self):
        self.assertEqual(build_article_markdown("", ""), "")
        self.assertEqual(build_article_markdown(None, None), "")

    def test_overlong_markdown_is_capped(self):
        raw = "长文测试。\n\n" + "字" * (MAX_MARKDOWN_CHARS + 2000)
        result = build_article_markdown(raw, "")
        self.assertLessEqual(len(result), MAX_MARKDOWN_CHARS + 30)
        self.assertIn("已截断", result)

    def test_detectors(self):
        self.assertTrue(_looks_like_html("<p>正文</p>"))
        self.assertTrue(_looks_like_html("<h2>标题</h2><div>内容</div>"))
        self.assertFalse(_looks_like_html("纯文本没有标签"))
        self.assertTrue(_looks_like_markdown("# 标题\n正文"))
        self.assertTrue(_looks_like_markdown("**加粗**文字"))
        self.assertTrue(_looks_like_markdown("- 列表项"))
        self.assertTrue(_looks_like_markdown("[链接](https://a.b/c)"))
        self.assertFalse(_looks_like_markdown("纯文本没有任何标记"))
        self.assertTrue(_looks_like_js_junk('window.x=1;function a(){return "y"}'))
        self.assertFalse(_looks_like_js_junk("这是一段正常的中文正文，包含足够多的汉字用于判定。" * 3))

    def test_markdown_result_keeps_chinese_content(self):
        raw = "<p>近日，某公司宣布量产人形机器人，计划年内下线一千台。</p>"
        result = build_article_markdown(raw, "")
        self.assertIn("人形机器人", result)


if __name__ == "__main__":
    unittest.main()
