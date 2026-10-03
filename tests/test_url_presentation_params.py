# -*- coding: utf-8 -*-
"""URL 语言/展示参数清洗：入库闸门剥掉 lang/hl/locale 等参数。

背景：X 会把界面语言写进链接，抓到的是语言版本页面，标题被写成
「X पर AI Will: …」（印地语）或「GitHubDaily в X: …」（保加利亚语），
在中文列表里看着像乱码，同一篇文章还会被当成不同 URL。
"""
import unittest

from utils import strip_url_presentation_params


class StripUrlPresentationParamsTest(unittest.TestCase):
    def test_strips_language_param_from_x_status_url(self):
        # 生产实际出现过的两条
        self.assertEqual(
            strip_url_presentation_params(
                "https://x.com/FinanceYF5/status/2086403813666439533?lang=hi"),
            "https://x.com/FinanceYF5/status/2086403813666439533",
        )
        self.assertEqual(
            strip_url_presentation_params("https://x.com/GitHub_Daily/status/1?lang=bg"),
            "https://x.com/GitHub_Daily/status/1",
        )

    def test_keeps_other_params_and_original_encoding(self):
        # 其余参数逐字保留：不把 %20 变成 +，也不重排顺序
        self.assertEqual(
            strip_url_presentation_params(
                "https://example.com/a?b=1%202&lang=en&c=3+4&d=%E4%B8%AD"),
            "https://example.com/a?b=1%202&c=3+4&d=%E4%B8%AD",
        )

    def test_case_insensitive_and_blank_value(self):
        self.assertEqual(strip_url_presentation_params("https://a.com/p?LANG=en"),
                         "https://a.com/p")
        self.assertEqual(strip_url_presentation_params("https://a.com/p?lang=&x=1"),
                         "https://a.com/p?x=1")

    def test_strips_hl_and_locale(self):
        self.assertEqual(
            strip_url_presentation_params("https://a.com/p?hl=zh-CN&locale=zh_CN&id=9"),
            "https://a.com/p?id=9",
        )

    def test_unchanged_when_nothing_to_strip(self):
        for url in (
            "",
            "https://a.com/p",
            "https://a.com/p?id=1",
            "https://a.com/p?language_of_things=1",
        ):
            self.assertEqual(strip_url_presentation_params(url), url)

    def test_fragment_is_kept(self):
        self.assertEqual(
            strip_url_presentation_params("https://a.com/p?lang=en#sec"),
            "https://a.com/p#sec",
        )

    def test_non_string_input_is_tolerated(self):
        self.assertEqual(strip_url_presentation_params(None), "")


if __name__ == "__main__":
    unittest.main()
