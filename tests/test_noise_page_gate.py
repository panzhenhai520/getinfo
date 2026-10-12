#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行业包噪声页面闸门回归（离线夹具，不联网）。

夹具全部取自 A 机 invest_mgmt 行业包 976 行的**真实 URL / 标题**，所以用例既是单元
测试也是"这段语料不会再进库"的回归：任何一条被判过的页面重新可入库，用例即失败。

覆盖三层：
  1. 信源层 URL 形态（intel_content_quality_gate.url_shape_denied_reason）
  2. 抓取层刊头聚合标题（looks_like_digest_index_title）
  3. 落库闸门（sqlite_database.insert_article，sqlite 临时库隔离）与候选派发提前拒绝

变异验证（关掉规则，用例必须失败）：
  INTEL_REJECT_URL_SHAPE=0       → UrlShapeGateTests / 集成与入库用例失败
  INTEL_REJECT_PERIOD_DIGEST=0   → DigestTitleGateTests 之外的集成与入库用例失败
  INTEL_URL_DENY_PATH_PATTERNS=  → 机构公告栏目被放行，入库用例失败
"""
import os
import sys
import tempfile
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

config.DATABASE_TYPE = "sqlite"

from intel_content_quality_gate import (  # noqa: E402
    assess_article_quality,
    deny_path_patterns,
    looks_like_channel_or_index_url,
    looks_like_digest_index_title,
    url_shape_denied_reason,
)
from utils import get_china_time  # noqa: E402

# ------------------------------------------------------------------
# 真噪声夹具（A 机 invest_mgmt 976 行里被当文章入库的导航/列表/聚合页）
# ------------------------------------------------------------------
CHANNEL_URL_CASES = [
    # (真实 URL, 期望原因, 说明)
    ("https://www.amac.org.cn", "home", "中基协站点首页，入库标题=机构名"),
    ("https://developer.mthreads.com/", "home", "摩尔线程开发者首页"),
    ("https://www.amac.org.cn/fwdt/wyb/jgdjhcpbeian/", "directory", "办事大厅目录页"),
    ("https://www.cnnc.com.cn/cnnc/lxwm87/index.html", "index", "联系方式 index.html"),
    ("http://www.sasac.gov.cn/n2588035/n2588320/n2588340/index.html", "index", "国资委政策解读栏目"),
    ("https://www.csrc.gov.cn/csrc/zwxx/index.shtml?channelid=d60512bc2c824021b21a088e3b2e3195",
     "index", "政府网站年度报表栏目"),
    ("https://www.sdicfof.com/gtch/zjch/gltd/A200102index_1.htm", "index", "管理团队（CMS 模板页）"),
    ("https://www.sdicfof.com/gtch/zjch/lsyg/A200103index_1.htm", "index", "历史沿革"),
    ("https://www.sdicfof.com/gtch/zjch/A2001index_1.htm", "index", "公司简介"),
    ("https://www.sdicfof.com/gtch/zpyc/jbxx/A200401index_1.htm", "index", "基本信息"),
    ("https://www.cyzone.cn/album/848459", "section", "2026DEMO CHINA 专题策划页"),
    ("https://www.cyzone.cn/album/831826", "section", "创业政策专题页"),
    ("https://news.pedaily.cn/zt/558050.shtml", "section", "中国股权投资年度大会专题页"),
    ("https://www.stcn.com/article/list/djsj.html", "section", "证券时报栏目列表页"),
    ("https://www.jiemian.com/lists/118.html", "section", "界面新闻栏目列表页"),
    ("https://www.secrss.com/articles?tag=APT", "listing_query", "标签列表页"),
    ("https://www.secrss.com/articles?tag=%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD",
     "listing_query", "标签列表页（URL 编码）"),
    ("https://www.cdb-capital.com/news/dynamic?keyword=", "listing_query", "列表页空查询串"),
    ("https://m.pedaily.cn/news/c2", "channel", "移动端 vcpe 频道页"),
    # 信源栏目排除表（机构公告/定期披露）
    ("https://www.amac.org.cn/zlgl/sljg/sljgclgg/202609/t20260907_28070.html",
     "deny_path", "中基协注销失联私募基金管理人登记公告"),
    ("https://www.boc.cn/cbservice/bi2/202609/t20260901_25688957.html",
     "deny_path", "中国银行企业年金集合计划季度管理情况"),
    ("https://www.ggzy.gov.cn/information/deal/html/a/370000/0104/20261007/0037e37e.html",
     "deny_path", "公共资源交易中标结果公示"),
    ("https://www.amac.org.cn/xwfb/tzgg/202609/t20260929_28129.html",
     "deny_path", "中基协通知公告栏目"),
]

# 聚合刊头页（标题即"多条并列快讯的总目录"）
DIGEST_TITLE_CASES = [
    "投资界24h | 姚顺雨，坐在马化腾旁边；黄仁勋女婿火速晋升；紫燕食品又做LP了",
    "投资界24h| 严文韬正式入职DeepSeek；雷军否认打新宇树挣100亿；广东LP出资路威凯腾",
    "投资界AI周报|姚顺雨上桌了",
    "投资界AI周报 | DeepSeek掀起融资风暴",
    "投资界LP周报 | 上海国资又出钱了",
    "投资界LP周报|深圳成立100亿基金",
    "投资界募资月报 | 高榕五源经纬，都募资了",
]

# **不许误伤**：真文章 URL（与上面同域名/同模板）
REAL_ARTICLE_URL_CASES = [
    "https://news.pedaily.cn/202609/569704.shtml",          # 郑州战新母基金遴选结果公示
    "https://news.pedaily.cn/202610/570017.shtml",          # 百趣生物完成近亿元B轮融资
    "https://news.pedaily.cn/202609/569745.shtml",          # 标题兜底为 569745，但正文 2989 字
    "https://www.szvc.com.cn/article/3232",                 # 粤港澳大湾区创投引导基金拟出资子基金公告
    "https://www.szvc.com.cn/article/3205",                 # 宝安区产业投资引导基金拟出资子基金公示
    "https://www.wuxicapital.com.cn/news_60/561.html",      # 无锡战新母基金拟投子基金对外公示
    "https://www.secrss.com/articles/91401",
    "https://www.amac.org.cn/hydj/xxgcddesdjs/202510/t20251029_27002.html",
    "https://www.amac.org.cn/xwfb/hydt/202608/t20260819_28033.html",
    "https://www.amac.org.cn/news/xhyw/202609/t20260918_28097.html",   # 同域名、非排除栏目
    # 短频道规则的两条实测误杀（曾把真文章判成频道页，固化为回归夹具）：
    "https://research.example/news/20",                     # 纯数字尾段=文章ID，不是频道
    "https://www.qianxin.com/news/detail?news_id=14563",    # detail=详情动作词，不是频道
    "https://www.qianxin.com/news/detail?news_id=14743",
    # `/topic/<slug>` 是大量 CMS 的文章详情路径，不是栏目页（曾误伤既有用例）
    "https://example.com/topic/one",
    "https://example.com/topic/event",
    "https://example.com/topic/concurrent",
    # 多页文章的 `?page=2` 是同一篇的后半段，不是列表页
    "https://news.example.com/2026/09/18/big-story.html?page=2",
    # 非网页伪 URL 不做形态判定（manual_articles 用 `manual://<id>` 当文章地址，
    # 曾因 urlsplit 把 netloc=1/path='' 解析成"站点首页"被误拦）
    "manual://1",
]

# **不许误伤**：真事件/报纸转载标题
REAL_TITLE_CASES = [
    "郑州战新母基金子基金管理机构遴选结果公示",
    "烟台市人工智能产业投资基金管理机构遴选结果公示",
    "江苏泰州大健康先进制造产业专项母基金拟投子基金及子基金管理机构遴选结果公示",
    "百趣生物完成近亿元B轮融资，加码多组学+AI4S研发与全球布局",
    "关于粤港澳大湾区创业投资引导基金拟出资子基金的公告",
    "【人民日报 | 树立和践行正确政绩观 推动金融高质量发展（调查与思考）",
    "经济日报：上半年熊猫债外资发行人募资占比首超中资",
    "投资界独家 | 严文韬正式入职DeepSeek",     # 竖线但无刊期词
    "首发| 他以AI颠覆临床试验格局，璞睿创智完成亿元A轮首关",
    "深创投副总裁马楠：风险投资助力产业科技创新",
]


def _long_body(topic: str, times: int = 12) -> str:
    """凑一段能过"最小字数/段落/句子"检查的正文（不含框架词）。"""
    return "".join(
        f"第{i}段：{topic}的行业进展说明，涉及机构出资、基金备案与项目落地情况。"
        for i in range(1, times + 1)
    )


class UrlShapeGateTests(unittest.TestCase):
    """信源层：URL 形态判定（纯函数，离线）"""

    def test_channel_like_urls_are_denied_with_expected_reason(self):
        for url, reason, note in CHANNEL_URL_CASES:
            with self.subTest(url=url, note=note):
                self.assertEqual(url_shape_denied_reason(url), reason)
                self.assertTrue(looks_like_channel_or_index_url(url))

    def test_deny_path_patterns_match_host_and_path_only(self):
        patterns = deny_path_patterns()
        self.assertIn("boc.cn/cbservice/bi2", patterns)
        self.assertIn("ggzy.gov.cn/information/deal", patterns)
        # 排除表按 host+path 子串匹配，是"站点 + 栏目"级别：
        # 同路径挂到别的域不该命中（否则会把第三方站点的同路径文章误杀）。
        self.assertFalse(looks_like_channel_or_index_url(
            "https://www.icbc.com.cn/cbservice/bi2/202609/t20260901_1.html"))
        self.assertFalse(looks_like_channel_or_index_url(
            "https://www.boc.cn/cbservice/bi3/202609/t20260901_1.html"))
        # query 里的绝对地址不参与匹配（只看 host+path），避免外链参数误杀
        self.assertFalse(looks_like_channel_or_index_url(
            "https://news.example.com/p/1?from=https://www.boc.cn/cbservice/bi2/x.html"))

    def test_real_article_urls_are_not_denied(self):
        for url in REAL_ARTICLE_URL_CASES:
            with self.subTest(url=url):
                self.assertEqual(url_shape_denied_reason(url), "")
                self.assertFalse(looks_like_channel_or_index_url(url))

    def test_deny_list_is_configurable_and_takes_effect(self):
        url = "https://example.test/notice/list-page.html"
        self.assertFalse(looks_like_channel_or_index_url(url))
        self.assertEqual(
            url_shape_denied_reason(url, deny_patterns=("example.test/notice",)), "deny_path")


class DigestTitleGateTests(unittest.TestCase):
    """抓取层：刊头式聚合索引标题（纯函数，离线）"""

    def test_digest_head_titles_are_flagged(self):
        for title in DIGEST_TITLE_CASES:
            with self.subTest(title=title):
                self.assertTrue(looks_like_digest_index_title(title))

    def test_real_event_and_newspaper_titles_are_not_flagged(self):
        for title in REAL_TITLE_CASES:
            with self.subTest(title=title):
                self.assertFalse(looks_like_digest_index_title(title))


class QualityGateIntegrationTests(unittest.TestCase):
    """assess_article_quality 端到端：噪声页面必须 passed=False，真事件必须 passed=True"""

    def _assess(self, *, url="", title="", content=""):
        return assess_article_quality(
            {"url": url, "title": title, "content": content, "publish_date": "2026-01-01"},
            {"original_url": url, "title": title},
        )

    def test_channel_page_gets_new_issue_even_with_long_content(self):
        # 办事大厅目录页实测 1729 字、国资委政策解读栏目 1347 字，都能穿过字数闸门
        result = self._assess(
            url="https://www.amac.org.cn/fwdt/wyb/jgdjhcpbeian/",
            title="办事大厅",
            content=_long_body("基金行业服务事项"),
        )
        self.assertIn("url_is_channel_or_index_page", result["issues"])
        self.assertEqual(result["url_shape_reason"], "directory")
        self.assertFalse(result["passed"])

    def test_deny_path_notice_page_gets_new_issue(self):
        result = self._assess(
            url="https://www.boc.cn/cbservice/bi2/202609/t20260901_25688957.html",
            title="中国银行2026年2季度企业年金集合计划管理情况",
            content=_long_body("企业年金集合计划管理情况"),
        )
        self.assertIn("url_is_channel_or_index_page", result["issues"])
        self.assertEqual(result["url_shape_reason"], "deny_path")
        self.assertFalse(result["passed"])

    def test_digest_title_gets_new_issue(self):
        result = self._assess(
            url="https://news.pedaily.cn/202610/570010.shtml",
            title="投资界24h | 姚顺雨，坐在马化腾旁边；黄仁勋女婿火速晋升；紫燕食品又做LP了",
            content=_long_body("快讯"),
        )
        self.assertIn("title_is_period_digest", result["issues"])
        self.assertFalse(result["passed"])

    def test_real_event_passes(self):
        result = self._assess(
            url="https://news.pedaily.cn/202609/569704.shtml",
            title="郑州战新母基金子基金管理机构遴选结果公示",
            content=_long_body("郑州战新母基金子基金管理机构遴选"),
        )
        self.assertEqual(result["issues"], [])
        self.assertTrue(result["passed"])
        self.assertEqual(result["url_shape_reason"], "")

    def test_newspaper_reprint_passes(self):
        result = self._assess(
            url="https://www.ndrc.gov.cn/xwdt/dt/sjdt/202607/t20260717_1406558.html",
            title="【人民日报 | 树立和践行正确政绩观 推动金融高质量发展（调查与思考）",
            content=_long_body("金融高质量发展"),
        )
        self.assertEqual(result["issues"], [])
        self.assertTrue(result["passed"])


class SourceLinkFilterTests(unittest.TestCase):
    """信源层：列表页链接在"候选→文章"过滤阶段就被跳过（不发请求）"""

    def _extractor(self):
        from article_link_extractor import ArticleLinkExtractor

        return ArticleLinkExtractor.__new__(ArticleLinkExtractor)  # 跳过 __init__（避免拉起网络/浏览器）

    def test_channel_links_are_filtered_out(self):
        extractor = self._extractor()
        for url in ("https://www.amac.org.cn/fwdt/wyb/jgdjhcpbeian/",
                    "https://www.cyzone.cn/album/848459",
                    "https://www.cdb-capital.com/news/dynamic?keyword=",
                    "https://www.boc.cn/cbservice/bi2/202609/t20260901_25688957.html"):
            with self.subTest(url=url):
                self.assertTrue(extractor._is_obviously_not_article("基金行业服务事项办理", url))

    def test_real_article_links_are_kept(self):
        extractor = self._extractor()
        for url in ("https://news.pedaily.cn/202609/569704.shtml",
                    "https://www.szvc.com.cn/article/3232",
                    "https://www.wuxicapital.com.cn/news_60/561.html"):
            with self.subTest(url=url):
                self.assertFalse(
                    extractor._is_obviously_not_article("郑州战新母基金遴选结果公示", url))

    def test_new_filter_is_superset_of_existing_listing_url_rule(self):
        """候选派发层取两条规则的并集：旧规则命中的必须仍被拦下（不能回退）。"""
        from candidate_crawler_adapter import is_non_article_url, looks_like_listing_url

        samples = [
            "https://hit180.com/tag/xxx",
            "https://a.example/category/ai",
            "https://a.example/author/bob",
            "https://a.example/search?q=ai",
            "https://a.example/page/3",
            "https://a.example/list?p=2",
        ]
        for url in samples:
            with self.subTest(url=url):
                self.assertTrue(looks_like_listing_url(url), "旧规则应命中（基线）")
                self.assertTrue(is_non_article_url(url), "并集必须覆盖旧规则")

    def test_non_article_url_covers_new_shapes_only(self):
        from candidate_crawler_adapter import is_non_article_url, looks_like_listing_url

        new_only = [
            "https://www.amac.org.cn",
            "https://www.amac.org.cn/fwdt/wyb/jgdjhcpbeian/",
            "https://www.cyzone.cn/album/848459",
            "https://www.cdb-capital.com/news/dynamic?keyword=",
            "https://www.boc.cn/cbservice/bi2/202609/t20260901_25688957.html",
        ]
        for url in new_only:
            with self.subTest(url=url):
                self.assertFalse(looks_like_listing_url(url), "旧规则本来拦不住（这就是缺口）")
                self.assertTrue(is_non_article_url(url), "新并集应拦下")

    def test_non_article_url_keeps_real_articles(self):
        from candidate_crawler_adapter import is_non_article_url

        for url in ("https://news.pedaily.cn/202609/569704.shtml",
                    "https://www.szvc.com.cn/article/3232",
                    "https://www.wuxicapital.com.cn/news_60/561.html"):
            with self.subTest(url=url):
                self.assertFalse(is_non_article_url(url))


class IngestGateTests(unittest.TestCase):
    """落库终局闸门：噪声页面不得写进 articles（临时 sqlite，隔离且不联网）"""

    def setUp(self):
        from sqlite_database import SQLiteDatabase

        self._tmp = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self._tmp.name, "noise_gate.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())

    def tearDown(self):
        try:
            self.db.disconnect()
        finally:
            self._tmp.cleanup()

    def _payload(self, *, url, title, content):
        # 保留窗口是 1 年：用"今天 - 1 天"保证不被时效闸门拦掉
        publish = (get_china_time() - timedelta(days=1)).strftime("%Y-%m-%d")
        return {"url": url, "title": title, "content": content,
                "matched_keywords": ["基金"], "publish_date": publish,
                "source_method": "article_link_crawl"}

    def test_channel_page_and_digest_are_not_persisted(self):
        cases = [
            self._payload(url="https://www.amac.org.cn/fwdt/wyb/jgdjhcpbeian/",
                          title="办事大厅", content=_long_body("基金行业服务事项")),
            self._payload(url="https://www.boc.cn/cbservice/bi2/202609/t20260901_25688957.html",
                          title="中国银行2026年2季度企业年金集合计划管理情况",
                          content=_long_body("企业年金集合计划管理情况")),
            self._payload(url="https://www.sdicfof.com/gtch/zjch/gltd/A200102index_1.htm",
                          title="管理团队", content=_long_body("管理团队介绍")),
            self._payload(url="https://news.pedaily.cn/202610/570010.shtml",
                          title="投资界24h | 姚顺雨，坐在马化腾旁边；黄仁勋女婿火速晋升；紫燕食品又做LP了",
                          content=_long_body("快讯")),
        ]
        for payload in cases:
            with self.subTest(url=payload["url"]):
                self.assertIsNone(self.db.insert_article(payload))

    def test_real_event_is_still_persisted(self):
        article_id = self.db.insert_article(self._payload(
            url="https://news.pedaily.cn/202609/569704.shtml",
            title="郑州战新母基金子基金管理机构遴选结果公示",
            content=_long_body("郑州战新母基金子基金管理机构遴选"),
        ))
        self.assertIsNotNone(article_id)
        row = self.db.get_article_by_id(article_id)
        self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
