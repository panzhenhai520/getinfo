# -*- coding: utf-8 -*-
"""Before/after for the reported defect: keyword ``anc`` vs an English article.

Run from the CollectInfo root:

    python tools/demo_keyword_exact_match.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from keyword_filter import KeywordFilter  # noqa: E402

ARTICLE = {
    "title": "Quarterly Finance Review",
    "content": (
        "Guidance for institutional investors. Revenue and performance improved "
        "and the balance sheet remains strong. Management reiterated its "
        "full-year guidance."
    ),
}

REAL_ANC_ARTICLE = {
    "title": "ANC publishes annual report",
    "content": "The ANC set out its guidance for the year ahead.",
}


def old_behaviour(text: str, keyword: str) -> bool:
    """The rule this module used to apply: bare substring, case sensitive."""

    return keyword in text


def main() -> int:
    text = f"{ARTICLE['title']} {ARTICLE['content']}"
    print("keyword 'anc' vs an English finance article")
    print(f"  article length            : {len(text)} chars")
    print(f"  contains substring 'anc'  : {'anc' in text.casefold()}")
    print(f"  OLD match_article         : {old_behaviour(text, 'anc')}   <-- the false positive")
    print(f"  NEW match_article         : {KeywordFilter('anc').match_article(ARTICLE)}")
    print(f"  NEW matched keywords      : {KeywordFilter('anc').get_matched_keywords(text)}")
    print()
    print("per-keyword results on the same article")
    for keyword in ("anc", "finance", "guidance", "performance", "sheet"):
        kf = KeywordFilter(keyword)
        matched = kf.get_matched_keywords(text)
        print(f"  {keyword:<12} match_article={str(kf.match_article(ARTICLE)):<5} matched={matched}")
    print()
    print("and a keyword that really occurs as a word still matches")
    kf = KeywordFilter("anc")
    real = f"{REAL_ANC_ARTICLE['title']} {REAL_ANC_ARTICLE['content']}"
    print(f"  NEW match_article         : {kf.match_article(REAL_ANC_ARTICLE)}")
    print(f"  NEW matched keywords      : {kf.get_matched_keywords(real)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
