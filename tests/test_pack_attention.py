# -*- coding: utf-8 -*-
"""注意力方向：解析周报「下周关注」→ 盯防词 → 动态主题。"""
import unittest

from pack_attention import (
    MAX_KEYWORDS_PER_DIRECTION,
    WATCH_TOPIC_KEY,
    WATCH_TOPIC_NAME,
    extract_keywords,
    next_week_topic_week,
    parse_watch_section,
    week_key_of,
    week_label,
)

# 生产周报（embodied_ai，09-25 ~ 10-02）里真实的那一段
REAL_REPORT = """# 具身智能行业周报（09-25 ~ 10-02）

## 一、本周概览
本周具身智能行业呈现出明显的“两端活跃”态势。

## 五、下周关注
1. **李飞飞公司收购后的整合动向**：550 亿巨额交易落地后，其世界模型技术如何与苏姿丰的资源结合。
2. **GPT-6 Astra 的适配潜力**：关注 HomeBody 项目是否能快速迁移至其他硬件平台，验证其无需重新训练的通用性。
3. **物理 AI 数据众包的实际规模**：跟踪“觅蜂派”等平台的参与人数及数据产出质量，观察其能否有效缓解数据瓶颈。
4. **具身智能 IPO 排队企业的应对策略**：在监管趋严背景下，排队中的 50 余家企业如何调整商业化故事以符合盈利审查要求。
"""

REPORT_WITH_KEYWORD_LINE = """# 某行业周报

## 五、下周关注
- 头部厂商量产节奏：关注产能爬坡与交付。跟踪关键词：赛力斯、问界 M9、产能爬坡
- 固态电池上车：跟踪半固态方案的装车进度。跟踪关键词：半固态电池、蔚来、装车
"""


class WeekNamingTest(unittest.TestCase):
    def test_week_key_and_label(self):
        self.assertEqual(week_key_of("2026-10-03"), "2026-W40")
        self.assertEqual(week_label("2026-W40"), "第40周")

    def test_watch_week_is_the_following_monday(self):
        # 09-25~10-02 的周报，下周关注盯的是 10-05 那一周
        self.assertEqual(next_week_topic_week("2026-10-02"), "2026-W41")

    def test_topic_name_is_stable_across_weeks(self):
        # 主题只有一张、跨周复用：名字固定「上周追踪」，不带周号（周号在面板/线索里体现）
        self.assertEqual(WATCH_TOPIC_NAME, "上周追踪")
        self.assertEqual(WATCH_TOPIC_KEY, "watch_track")

    def test_sunday_end_goes_to_next_monday(self):
        self.assertEqual(next_week_topic_week("2026-10-04"), "2026-W41")


class ParseWatchSectionTest(unittest.TestCase):
    def test_parses_real_production_report(self):
        items = parse_watch_section(REAL_REPORT)
        self.assertEqual(len(items), 4)
        self.assertEqual(items[0]["direction"], "李飞飞公司收购后的整合动向")
        self.assertIn("550 亿", items[0]["reason"])
        self.assertEqual(items[2]["direction"], "物理 AI 数据众包的实际规模")
        # 未写「跟踪关键词」时 keywords 留空，交给抽词环节
        self.assertEqual(items[0]["keywords"], [])

    def test_reads_explicit_keyword_line(self):
        items = parse_watch_section(REPORT_WITH_KEYWORD_LINE)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["direction"], "头部厂商量产节奏")
        self.assertEqual(items[0]["keywords"], ["赛力斯", "问界 M9", "产能爬坡"])
        # 「跟踪关键词」那一行不能被当成理由
        self.assertNotIn("跟踪关键词", items[0]["reason"])

    def test_returns_empty_without_section(self):
        self.assertEqual(parse_watch_section("# 只有概览\n本周无事。"), [])

    def test_section_stops_at_next_heading(self):
        text = REAL_REPORT + "\n## 六、附录\n1. **不该被当成线索**：这是附录。\n"
        items = parse_watch_section(text)
        self.assertEqual([item["direction"] for item in items][-1], "具身智能 IPO 排队企业的应对策略")


class ExtractKeywordsTest(unittest.TestCase):
    def test_pack_vocabulary_wins_in_rule_fallback(self):
        pack = {"brands": ["World Labs"], "core_keywords": ["世界模型", "具身智能"]}
        items = [{"direction": "李飞飞公司收购后的整合动向",
                  "reason": "其世界模型技术如何与苏姿丰的资源结合。", "keywords": []}]
        enriched = extract_keywords(items, pack)
        keywords = enriched[0]["keywords"]
        self.assertIn("世界模型", keywords)
        self.assertTrue(len(keywords) <= MAX_KEYWORDS_PER_DIRECTION)

    def test_explicit_keywords_are_kept(self):
        items = [{"direction": "头部厂商量产节奏", "reason": "关注产能。",
                  "keywords": ["赛力斯", "产能爬坡"]}]
        enriched = extract_keywords(items, {})
        self.assertEqual(enriched[0]["keywords"], ["赛力斯", "产能爬坡"])


class GenericKeywordFilterTest(unittest.TestCase):
    """行业通用词（命中全库）必须被剔掉，否则盯防卡会变成"整包文章列表"。"""

    @staticmethod
    def _articles(total, common_hits, rare_hits):
        rows = []
        for index in range(total):
            title = "普通文章"
            if index < common_hits:
                title += " 具身智能"
            if index < rare_hits:
                title += " IPO"
            rows.append({"title": title, "content": "正文" * 50})
        return rows

    def test_drops_common_and_keeps_specific(self):
        from pack_attention import _filter_items_by_ratio
        items = [{"direction": "具身智能 IPO 排队企业", "keywords": ["具身智能", "IPO"]}]
        filtered = _filter_items_by_ratio(items, self._articles(40, common_hits=30, rare_hits=3))
        self.assertEqual(filtered[0]["keywords"], ["IPO"])

    def test_keeps_rarest_when_everything_is_common(self):
        from pack_attention import _filter_items_by_ratio
        items = [{"direction": "全都很泛", "keywords": ["具身智能", "机器人"]}]
        filtered = _filter_items_by_ratio(items, self._articles(40, common_hits=35, rare_hits=25))
        self.assertEqual(len(filtered[0]["keywords"]), 1)
        self.assertEqual(filtered[0]["keywords"][0], "机器人")

    def test_small_corpus_is_left_alone(self):
        from pack_attention import _filter_items_by_ratio
        items = [{"direction": "语料太少", "keywords": ["具身智能", "IPO"]}]
        filtered = _filter_items_by_ratio(items, self._articles(5, common_hits=5, rare_hits=1))
        self.assertEqual(filtered[0]["keywords"], ["具身智能", "IPO"])


if __name__ == "__main__":
    unittest.main()
