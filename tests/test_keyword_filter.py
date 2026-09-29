# -*- coding: utf-8 -*-
"""The article keyword filter must not admit an article on a partial match."""

import unittest

from keyword_filter import OPENCC_AVAILABLE, KeywordFilter

requires_opencc = unittest.skipUnless(
    OPENCC_AVAILABLE,
    "opencc is not installed, so simplified/traditional variants do not exist",
)


FINANCE_ARTICLE = {
    "title": "Quarterly Finance Review",
    "content": (
        "Guidance for institutional investors. Revenue and performance metrics "
        "improved, and the balance sheet remains strong."
    ),
}


class ExactMatchTest(unittest.TestCase):
    """The reported defect, at the level the operator sees it."""

    def test_anc_does_not_admit_a_finance_article(self):
        # `Finance` and `guidance` both contain `anc`; neither is the keyword.
        article = dict(FINANCE_ARTICLE)
        self.assertIn("anc", article["title"].lower())
        self.assertIn("anc", article["content"].lower())

        self.assertFalse(KeywordFilter("anc").match_article(article))

    def test_anc_does_not_report_a_match(self):
        text = f"{FINANCE_ARTICLE['title']} {FINANCE_ARTICLE['content']}"
        self.assertEqual(KeywordFilter("anc").get_matched_keywords(text), [])

    def test_anc_does_admit_an_article_that_really_uses_it(self):
        article = {
            "title": "ANC publishes annual report",
            "content": "The ANC outlined its guidance for the year.",
        }
        self.assertTrue(KeywordFilter("anc").match_article(article))
        self.assertEqual(KeywordFilter("anc").get_matched_keywords(
            f"{article['title']} {article['content']}"), ["anc"])

    def test_a_longer_keyword_containing_a_partial_is_fine(self):
        # `finance` as the keyword legitimately matches this article.
        self.assertTrue(KeywordFilter("finance").match_article(FINANCE_ARTICLE))

    def test_partial_latin_terms_are_rejected_across_the_board(self):
        cases = [
            ("gdp", "GDPR compliance"),
            ("us", "business as usual"),
            ("ipo", "a hippo appeared"),
            ("etf", "netflix and chill"),
            ("ai", "AIGC is growing"),
        ]
        for term, text in cases:
            with self.subTest(term=term, text=text):
                self.assertEqual(KeywordFilter(term).get_matched_keywords(text), [])


class ChineseBehaviourIsUnchangedTest(unittest.TestCase):
    def test_simplified_keyword_matches_inside_a_longer_phrase(self):
        self.assertTrue(
            KeywordFilter("楼市").match_article({"title": "中国楼市最新动态", "content": ""})
        )

    @requires_opencc
    def test_traditional_article_matches_simplified_keyword(self):
        article = {"title": "中國樓市最新動態", "content": "關於中國樓市的分析報告..."}
        self.assertTrue(KeywordFilter("中国楼市").match_article(article))

    @requires_opencc
    def test_simplified_article_matches_traditional_keyword(self):
        article = {"title": "中国楼市分析", "content": "本文分析了中国房产市场..."}
        self.assertTrue(KeywordFilter("中國樓市").match_article(article))

    @requires_opencc
    def test_simplified_keyword_matches_a_traditional_article_without_a_partial(self):
        # The boundary rule must not interfere with opencc variants: a short
        # simplified term still matches inside a traditional sentence.
        article = {"title": "中國動態", "content": "關於樓市的分析"}
        self.assertTrue(KeywordFilter("楼市").match_article(article))

    def test_title_and_content_are_reported_separately(self):
        result = KeywordFilter("楼市,房产").get_matched_keywords_by_location(
            "中国楼市动态", "房产市场分析"
        )
        self.assertEqual(result["title_keywords"], ["楼市"])
        self.assertEqual(result["content_keywords"], ["房产"])
        self.assertEqual(sorted(result["all_keywords"]), ["房产", "楼市"])
        self.assertIn("[标]楼市", result["matched_keywords_str"])
        self.assertIn("[文]房产", result["matched_keywords_str"])


class FilterBehaviourTest(unittest.TestCase):
    def test_no_keywords_means_everything_passes(self):
        empty = KeywordFilter("")
        self.assertFalse(empty.is_enabled())
        self.assertTrue(empty.match_article(FINANCE_ARTICLE))
        self.assertEqual(empty.get_matched_keywords("anything"), [])

    def test_multiple_keywords_report_only_the_ones_present(self):
        kf = KeywordFilter("anc, finance, guidance")
        matched = kf.get_matched_keywords(
            f"{FINANCE_ARTICLE['title']} {FINANCE_ARTICLE['content']}")
        self.assertEqual(sorted(matched), ["finance", "guidance"])
        self.assertNotIn("anc", matched)

    def test_phrases_are_not_split_on_spaces(self):
        # The constructor deliberately keeps "family office" as one keyword.
        kf = KeywordFilter("family office")
        self.assertEqual(kf.keywords, ["family office"])
        self.assertTrue(kf.match_article({"title": "Family Offices in Asia", "content": ""}))

    def test_variants_are_reported_for_a_match(self):
        matched = KeywordFilter("finance").get_matched_keywords_with_variants(
            "Quarterly Finance Review")
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["keyword"], "finance")

    def test_separators_are_all_accepted(self):
        for text in ("a,b", "a;b", "a；b", "a，b", "a\nb"):
            with self.subTest(text=text):
                self.assertEqual(KeywordFilter(text).keywords, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
