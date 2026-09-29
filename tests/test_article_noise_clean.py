import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

# 测试用 SQLite，避免污染本地 PG
config.DATABASE_TYPE = "sqlite"

from sqlite_database import clean_article_markdown, _dedup_title_heading_lines  # noqa: E402
from content_handlers import build_article_markdown  # noqa: E402


class CleanArticleMarkdownImageTests(unittest.TestCase):
    """入库清洗：图片噪音（纯图片行 + 行内图片 token）必须剔除"""

    def test_pure_image_line_dropped(self):
        text = "正文第一段。\n\n![配图](https://x.com/a.jpg)\n\n正文第二段。"
        out = clean_article_markdown(text)
        self.assertNotIn("![配图]", out)
        self.assertIn("正文第一段", out)
        self.assertIn("正文第二段", out)

    def test_blank_alt_image_line_dropped(self):
        text = "开头\n\n![](https://x.com/a.jpg)\n\n结尾"
        out = clean_article_markdown(text)
        self.assertNotIn("](", out)
        self.assertIn("开头", out)
        self.assertIn("结尾", out)

    def test_inline_image_token_stripped_keeps_text(self):
        text = "电池能量密度提升 30%。![参数表](https://x.com/t.png) 其余说明文字。"
        out = clean_article_markdown(text)
        self.assertNotIn("![", out)
        self.assertIn("电池能量密度提升 30%", out)
        self.assertIn("其余说明文字", out)

    def test_non_image_content_untouched(self):
        text = "正常段落，含[链接](https://x.com/p)与**加粗**。"
        out = clean_article_markdown(text)
        self.assertEqual(out, text)


class DedupTitleHeadingTests(unittest.TestCase):
    """标题噪音：Markdown 中与文章标题重复的标题行应被删除"""

    def test_duplicate_h1_removed(self):
        md = "# 汽车行业周报\n\n正文内容……"
        out = _dedup_title_heading_lines(md, "汽车行业周报")
        self.assertNotIn("# 汽车行业周报", out)
        self.assertIn("正文内容", out)

    def test_h2_duplicate_removed(self):
        md = "## 汽车行业周报\n\n正文内容……"
        out = _dedup_title_heading_lines(md, "汽车行业周报")
        self.assertNotIn("汽车行业周报", out)

    def test_similar_but_not_equal_title_kept(self):
        md = "# 汽车行业周报（第12期）\n\n正文内容……"
        out = _dedup_title_heading_lines(md, "汽车行业周报")
        self.assertIn("# 汽车行业周报（第12期）", out)

    def test_normalized_symbols_match(self):
        md = "# 汽车——行业*周报*\n\n正文内容……"
        out = _dedup_title_heading_lines(md, "汽车行业周报")
        self.assertNotIn("# 汽车——行业*周报*", out)
        self.assertIn("正文内容", out)

    def test_max_removed_cap(self):
        md = "# 同题\n\n# 同题\n\n# 同题\n\n# 同题\n\n正文"
        out = _dedup_title_heading_lines(md, "同题", max_removed=3)
        self.assertEqual(out.count("# 同题"), 1)


class BuildMarkdownImageStripTests(unittest.TestCase):
    """展示 Markdown 统一去除行内图片 token"""

    def test_inline_image_token_stripped(self):
        raw = "<p>公司发布新一代产品。![产品图](https://x.com/p.jpg) 详见官网。</p>"
        out = build_article_markdown(raw, "")
        self.assertNotIn("![", out)
        self.assertIn("公司发布新一代产品", out)

    def test_plain_text_with_image_token_stripped(self):
        content = "新一代产品。![图](https://x.com/p.jpg)"
        out = build_article_markdown(content, content)
        self.assertNotIn("![", out)
        self.assertIn("新一代产品", out)


if __name__ == "__main__":
    unittest.main()
