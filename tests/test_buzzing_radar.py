# -*- coding: utf-8 -*-
"""buzzing.cc 海外标题雷达单测（不发真实网络请求，全部用构造数据）。

覆盖：
  1. feed 解析：保留出处（category）、英文原标题、发布时间；坏 XML 不抛异常
  2. 术语抽取：英文只认专有名词形态（淘汰 to/in/as/com 与域名碎片）、中文分词带停用词过滤
  3. 出处排行与热词计数
  4. 覆盖度审计：两个口径（标题命中 / 趋势命中）与 covered 的三态语义
  5. 报告结构完整，且**绝不写库、不产生候选**（用只读的替身数据库验证）
"""

import json
import os
import sqlite3
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_TYPE", "sqlite")

import buzzing_radar as br  # noqa: E402

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>国外财经新闻</title>
  <entry>
    <id>https://i.buzzing.cc/finance/posts/1/</id>
    <title>受霍尔木兹海峡袭击影响，油价攀升至100美元以上 - Yahoo Finance</title>
    <updated>2026-10-07T14:32:10.631Z</updated>
    <published>2026-10-07T14:32:10.631Z</published>
    <content type="html"><![CDATA[<div>Oil prices climb above 100 (<a href="https://news.google.com/rss/articles/AAA">news.google.com</a>)</div><footer>#Yahoo Finance</footer>]]></content>
    <link rel="alternate" href="https://news.google.com/rss/articles/AAA"/>
    <summary>Oil prices climb above 100 on Strait of Hormuz attacks</summary>
    <category term="Yahoo Finance"/>
  </entry>
  <entry>
    <title>SpaceX 正就借款 400 亿美元购买英伟达芯片进行谈判 - Bloomberg.com</title>
    <published>2026-10-07T07:56:52.400Z</published>
    <link rel="alternate" href="https://news.google.com/rss/articles/BBB"/>
    <summary>SpaceX in Talks to Borrow 40 Billion to Buy Nvidia Chips</summary>
    <category term="Bloomberg.com"/>
  </entry>
</feed>
"""


class FeedParsingTests(unittest.TestCase):
    def test_entries_keep_publisher_and_english_title(self):
        entries = br.parse_feed_entries(ATOM.encode("utf-8"))
        self.assertEqual(len(entries), 2)
        first = entries[0]
        self.assertEqual(first["publisher"], "Yahoo Finance")
        self.assertIn("油价", first["title"])
        self.assertIn("Oil prices", first["english_title"])
        self.assertTrue(first["published_at"])
        self.assertIn("news.google.com", first["url"])

    def test_bad_xml_returns_empty_instead_of_raising(self):
        self.assertEqual(br.parse_feed_entries(b"<not-xml"), [])
        self.assertEqual(br.parse_feed_entries(b""), [])

    def test_limit_is_honoured(self):
        self.assertEqual(len(br.parse_feed_entries(ATOM.encode("utf-8"), limit=1)), 1)

    def test_fetch_feed_reports_error_without_raising(self):
        br.clear_cache()
        with patch("intel_http.SafeHTTPClient.get", side_effect=RuntimeError("boom")):
            result = br.fetch_feed("https://finance.buzzing.cc/feed.xml")
        self.assertEqual(result["entries"], [])
        self.assertIn("boom", result["error"])


class TermExtractionTests(unittest.TestCase):
    def test_english_only_keeps_proper_nouns(self):
        """英文功能词、域名碎片不能进热词（实测踩过的坑：to/in/as/com 排到榜首）。"""
        text = "SpaceX in Talks to Borrow 40 Billion to Buy Nvidia Chips from news.google.com"
        terms = br._extract_terms(text, [], latin_mode=True)
        self.assertIn("SpaceX", terms)
        self.assertIn("Nvidia", terms)
        for noise in ("to", "in", "as", "com", "google", "from"):
            self.assertNotIn(noise, terms, f"{noise} 不该被当成实体")

    def test_title_case_noise_needs_repetition_to_rank(self):
        """标题体（每个词都大写）会带进 Borrow/Billion 这类词：只出现一次就不该上榜。"""
        entries = [
            {"title": f"SpaceX 动作 {i}", "english_title": f"SpaceX in Talks to Borrow {i}",
             "publisher": "BBC"}
            for i in range(1)
        ]
        ranked = br.hot_terms(entries, limit=20)
        terms = [row["term"] for row in ranked]
        self.assertIn("SpaceX", terms)          # 通用词库里的确定实体
        self.assertNotIn("Borrow", terms)       # 只出现一次的普通词被条数下限挡掉
        self.assertNotIn("Billion", terms)

    def test_same_term_in_both_titles_counts_once_per_entry(self):
        """一篇文章的中文标题与英文原标题都提到同一个词时，只能算一条。"""
        entries = [{"title": "OpenAI 发布新模型", "english_title": "OpenAI ships a new model",
                    "publisher": "Axios"}]
        ranked = br.hot_terms(entries, limit=5)
        openai = next(row for row in ranked if row["term"] == "OpenAI")
        self.assertEqual(openai["count"], 1)

    def test_domains_are_stripped_before_tokenizing(self):
        self.assertEqual(br._strip_urls("see https://news.google.com/rss/articles/AAA now"),
                         "see   now")

    def test_chinese_tokens_filter_stopwords(self):
        terms = br._extract_terms("随着人工智能建设取代关税成为核心通胀的主要驱动因素", [])
        self.assertTrue(any("人工智能" in t or "关税" in t or "通胀" in t for t in terms),
                        terms)
        self.assertNotIn("随着", terms)

    def test_vocabulary_hits_always_kept(self):
        terms = br._extract_terms("家族办公室新政解读", ["家族办公室"])
        self.assertIn("家族办公室", terms)

    def test_hot_terms_counts_and_flags(self):
        entries = [
            {"title": "OpenAI 发布新模型", "english_title": "OpenAI ships a new model",
             "publisher": "Axios"},
            {"title": "OpenAI 再获融资", "english_title": "OpenAI raises again",
             "publisher": "Fortune"},
            {"title": "捷豹发布新车", "english_title": "Jaguar unveils a car",
             "publisher": "BBC"},
        ]
        ranked = br.hot_terms(entries, vocabulary=["捷豹"], limit=10)
        by_term = {row["term"]: row for row in ranked}
        self.assertEqual(by_term["OpenAI"]["count"], 2)
        self.assertFalse(by_term["OpenAI"]["in_vocabulary"])
        self.assertTrue(by_term["OpenAI"]["in_lexicon"])
        self.assertTrue(by_term["捷豹"]["in_vocabulary"])
        self.assertEqual(by_term["捷豹"]["count"], 1)
        # 出处随热词一起带出，便于人工核对
        self.assertIn("Axios", by_term["OpenAI"]["publishers"])

    def test_publisher_ranking_sorted(self):
        entries = [
            {"publisher": "Reuters"}, {"publisher": "Reuters"}, {"publisher": "CNBC"},
            {"url": "https://www.wsj.com/a"},  # 没出处时用域名兜底
        ]
        ranked = br.publisher_ranking(entries)
        self.assertEqual(ranked[0], {"publisher": "Reuters", "count": 2})
        self.assertIn({"publisher": "wsj.com", "count": 1}, ranked)


class CoverageAuditTests(unittest.TestCase):
    """覆盖度审计的三态语义：已覆盖 / 缺口 / 无法判定。"""

    def _terms(self):
        return [
            {"term": "投资", "count": 10, "in_vocabulary": True},
            {"term": "OpenAI", "count": 21, "in_vocabulary": False},
        ]

    def test_covered_and_missing_and_unknown(self):
        def fake_title_hits(pack_id, term):
            return 3 if term == "投资" else None

        with patch.object(br, "_our_title_hits", side_effect=fake_title_hits), \
                patch("intel_database.intel_repository") as repo:
            repo.list_articles_by_trend_keyword.return_value = ([], 0)
            rows = br.coverage_audit("family_office", self._terms(), days=2)

        invest = next(row for row in rows if row["term"] == "投资")
        self.assertEqual(invest["our_title_hits"], 3)
        self.assertTrue(invest["covered"])
        openai = next(row for row in rows if row["term"] == "OpenAI")
        # 词表外的词拿不到趋势口径 → None；标题口径也查不到 → None（未知，不能谎报缺口）
        self.assertIsNone(openai["our_trend_articles"])
        self.assertIsNone(openai["covered"])

    def test_missing_when_both_counts_are_zero(self):
        with patch.object(br, "_our_title_hits", return_value=0), \
                patch("intel_database.intel_repository") as repo:
            repo.list_articles_by_trend_keyword.return_value = ([], 0)
            rows = br.coverage_audit("family_office", self._terms(), days=2)
        invest = next(row for row in rows if row["term"] == "投资")
        self.assertEqual(invest["our_title_hits"], 0)
        self.assertEqual(invest["our_trend_articles"], 0)
        self.assertFalse(invest["covered"])

    def test_title_hits_query_failure_is_unknown_not_missing(self):
        with patch.object(br, "_our_title_hits", return_value=None), \
                patch("intel_database.intel_repository") as repo:
            repo.list_articles_by_trend_keyword.side_effect = RuntimeError("db down")
            rows = br.coverage_audit("family_office", [{"term": "投资", "in_vocabulary": True}])
        self.assertIsNone(rows[0]["covered"])


class RadarReportTests(unittest.TestCase):
    def test_report_structure_and_no_writes(self):
        """用只读连接跑一遍：雷达只要有任何写操作就会直接抛错。"""
        import shutil
        import tempfile

        # 专用临时目录 + 显式清理：不要用系统临时目录里的裸文件，
        # 也不要依赖解释器退出时的自动 rmtree（Windows 上偶发把 pytest 收尾炸掉）。
        work_dir = tempfile.mkdtemp(prefix="buzzing-radar-")
        self.addCleanup(shutil.rmtree, work_dir, ignore_errors=True)
        db_path = os.path.join(work_dir, "radar.sqlite3")
        writer = sqlite3.connect(db_path)
        writer.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY, title TEXT, status TEXT)")
        writer.execute(
            "CREATE TABLE article_intel_classifications ("
            " article_id INTEGER, industry_pack_id TEXT, score_details_json TEXT)"
        )
        writer.execute("INSERT INTO articles(id,title,status) VALUES(1,'家族办公室投资趋势','active')")
        writer.execute(
            "INSERT INTO article_intel_classifications VALUES(1,'family_office','{}')"
        )
        writer.commit()
        writer.close()

        read_only = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        read_only.row_factory = sqlite3.Row
        self.addCleanup(read_only.close)

        class ReadOnlyDb:
            connection = read_only
            lock = __import__("threading").RLock()

            @staticmethod
            def _ensure_connection():
                return None

        class Repo:
            db = ReadOnlyDb()

            @staticmethod
            def list_articles_by_trend_keyword(**kwargs):
                return [], 0

        with patch.object(br, "fetch_feed", return_value={
            "feed": "https://finance.buzzing.cc/feed.xml",
            "entries": br.parse_feed_entries(ATOM.encode("utf-8")),
            "error": "",
        }), patch("intel_database.intel_repository", Repo):
            report = br.radar_report("family_office", days=2, top=5)

        self.assertEqual(report["industry_pack_id"], "family_office")
        self.assertEqual(report["entry_count"], 2)
        self.assertEqual(len(report["feeds"]), 1)
        self.assertTrue(report["publishers"])
        self.assertTrue(report["hot_terms"])
        for key in ("missing_terms", "new_terms", "vocabulary_size", "generated_at", "days"):
            self.assertIn(key, report)
        # 真实库里那一篇标题含"投资"的文章必须被算成已覆盖
        invest = [row for row in report["hot_terms"] + report["missing_terms"]
                  if row["term"] == "投资"]
        if invest:
            self.assertGreater(invest[0]["our_title_hits"], 0)
        # 结果必须可 JSON 序列化（接口直接返回它）
        json.dumps(report, ensure_ascii=False)

    def test_feeds_come_from_config_with_alias_support(self):
        with patch.object(br.config, "BUZZING_RADAR_FEEDS", "finance,stocks"):
            self.assertEqual(
                br.configured_feeds(),
                ["https://finance.buzzing.cc/feed.xml", "https://stocks.buzzing.cc/feed.xml"],
            )
        with patch.object(br.config, "BUZZING_RADAR_FEEDS", "https://example.com/feed.xml"):
            self.assertEqual(br.configured_feeds(), ["https://example.com/feed.xml"])

    def test_api_route_is_registered(self):
        import firecrawl_app

        rules = {str(rule.rule) for rule in firecrawl_app.app.url_map.iter_rules()}
        self.assertIn("/api/intel/buzzing-radar", rules)

    def test_view_returns_a_serializable_payload(self):
        """真实调用视图（跳过登录装饰器）：参数解析 + 取数 + JSON 序列化整条链都要通。"""
        import json as _json

        import firecrawl_app
        import intel_api

        view = getattr(intel_api.buzzing_headline_radar, "__wrapped__", None)
        self.assertIsNotNone(view, "视图应保留 __wrapped__（login_required 用了 functools.wraps）")

        with patch.object(br, "radar_report", return_value={
            "industry_pack_id": "family_office", "days": 3, "generated_at": "x",
            "feeds": [], "entry_count": 0, "publishers": [], "hot_terms": [],
            "missing_terms": [], "new_terms": [], "vocabulary_size": 0,
        }) as mocked:
            with firecrawl_app.app.test_request_context(
                "/api/intel/buzzing-radar?industry_pack_id=family_office&top=7&days=3"
            ):
                response = view()
        payload = _json.loads(response.get_data(as_text=True))
        self.assertTrue(payload["success"])
        self.assertEqual(payload["industry_pack_id"], "family_office")
        self.assertIn("request_id", payload)
        # 参数必须真的透传到取数函数（而不是被视图吞掉）
        self.assertEqual(mocked.call_args.kwargs.get("days"), 3)
        self.assertEqual(mocked.call_args.kwargs.get("top"), 7)


if __name__ == "__main__":
    unittest.main()
