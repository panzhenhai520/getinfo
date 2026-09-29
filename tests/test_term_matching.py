# -*- coding: utf-8 -*-
"""Term matching must be whole-word for Latin script and substring for CJK."""

import unittest

from term_matching import (
    is_identifier_term,
    normalize_text,
    term_occurs,
    term_occurs_in_normalized,
)


class NormalizeTextTest(unittest.TestCase):
    def test_case_folds(self):
        self.assertEqual(normalize_text("Finance"), "finance")

    def test_nfkc_folds_full_width_forms(self):
        self.assertEqual(normalize_text("ＡＮＣ"), "anc")

    def test_handles_none_and_numbers(self):
        self.assertEqual(normalize_text(None), "")
        self.assertEqual(normalize_text(2024), "2024")


class IdentifierTermTest(unittest.TestCase):
    def test_identifier_shapes(self):
        # `-`, `.`, `_`, `:`, `^` are all inside the identifier alphabet, so a
        # term like `10-year` is whole-word matched rather than substring
        # matched. `s&p` and `c++` contain characters outside it and fall
        # through to substring matching.
        for term in ("anc", "finance", "gdp", "u.s.", "ai_2", "x^y", "a:b", "10", "10-year"):
            with self.subTest(term=term):
                self.assertTrue(is_identifier_term(term))

    def test_non_identifier_shapes(self):
        for term in ("family office", "中国楼市", "ai芯片", "s&p", "c++", ""):
            with self.subTest(term=term):
                self.assertFalse(is_identifier_term(term))


class LatinWholeWordTest(unittest.TestCase):
    """The reported defect: `anc` must not match inside longer English words."""

    def test_anc_does_not_match_finance(self):
        self.assertFalse(term_occurs("anc", "Finance"))

    def test_anc_does_not_match_guidance(self):
        self.assertFalse(term_occurs("anc", "guidance"))

    def test_anc_does_not_match_a_finance_article(self):
        article = (
            "Finance and guidance for institutional investors. "
            "The balance sheet shows significant performance."
        )
        self.assertFalse(term_occurs("anc", article))

    def test_anc_matches_the_standalone_word(self):
        self.assertTrue(term_occurs("anc", "The ANC met today."))

    def test_case_does_not_decide_a_match(self):
        self.assertTrue(term_occurs("anc", "ANC"))
        self.assertTrue(term_occurs("ANC", "anc"))
        self.assertTrue(term_occurs("Anc", "a meeting of anc members"))

    def test_punctuation_counts_as_a_boundary(self):
        for text in ("(anc)", "anc,", "anc.", "anc-led", "\"anc\"", "[anc]", "anc's"):
            with self.subTest(text=text):
                self.assertTrue(term_occurs("anc", text))

    def test_letters_next_to_the_term_block_the_match(self):
        for text in ("Finance", "guidance", "xanc", "ancx", "ANCX", "banc"):
            with self.subTest(text=text):
                self.assertFalse(term_occurs("anc", text))

    def test_digits_next_to_the_term_block_the_match(self):
        for text in ("anc1", "1anc", "a1nc"):
            with self.subTest(text=text):
                self.assertFalse(term_occurs("anc", text))

    def test_cjk_neighbours_do_not_block_the_match(self):
        # Chinese has no spaces, so `AI` must survive `AI芯片`.
        self.assertTrue(term_occurs("AI", "AI芯片"))
        self.assertTrue(term_occurs("AI", "做AI的公司"))
        self.assertTrue(term_occurs("anc", "anc公司"))

    def test_shorter_term_inside_longer_latin_word_is_rejected(self):
        cases = [
            ("ai", "AIGC"),
            ("gdp", "GDPR"),
            ("us", "business"),
            ("ipo", "hippo"),
            ("etf", "netflix"),
        ]
        for term, text in cases:
            with self.subTest(term=term, text=text):
                self.assertFalse(term_occurs(term, text))

    def test_full_width_text_still_matches(self):
        self.assertTrue(term_occurs("anc", "ＡＮＣ"))


class CjkSubstringTest(unittest.TestCase):
    def test_chinese_keyword_matches_inside_a_longer_phrase(self):
        self.assertTrue(term_occurs("楼市", "中国楼市最新动态"))

    def test_chinese_keyword_absent_is_rejected(self):
        self.assertFalse(term_occurs("楼市", "中国股市最新动态"))

    def test_multi_word_phrase_stays_a_substring_match(self):
        # `family office` is not identifier-shaped, so it keeps matching the
        # plural form rather than breaking a working keyword.
        self.assertTrue(term_occurs("family office", "Family Offices in Asia"))
        self.assertTrue(term_occurs("family office", "a family office"))
        self.assertFalse(term_occurs("family office", "the family left the office"))

    def test_mixed_script_keyword_matches_as_a_substring(self):
        self.assertTrue(term_occurs("AI芯片", "国产AI芯片进展"))


class EmptyAndEdgeCasesTest(unittest.TestCase):
    def test_empty_term_never_matches(self):
        self.assertFalse(term_occurs("", "anything"))
        self.assertFalse(term_occurs("   ", "anything"))
        self.assertFalse(term_occurs(None, "anything"))

    def test_empty_haystack_never_matches(self):
        self.assertFalse(term_occurs("anc", ""))
        self.assertFalse(term_occurs("anc", None))

    def test_regex_metacharacters_are_literal(self):
        self.assertTrue(term_occurs("u.s.", "the U.S. market"))
        self.assertFalse(term_occurs("u.s.", "the US market"))
        self.assertTrue(term_occurs("a:b", "ratio a:b here"))

    def test_normalized_variant_helper_agrees_with_the_plain_one(self):
        self.assertEqual(
            term_occurs("anc", "Finance and guidance"),
            term_occurs_in_normalized(normalize_text("anc"), normalize_text("Finance and guidance")),
        )


if __name__ == "__main__":
    unittest.main()
