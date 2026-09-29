import unittest

import config
from tools.import_automotive_rss_sources import (
    AUTOMOTIVE_EXACT_TOPIC_KEYWORDS,
    AUTOMOTIVE_SERPAPI_QUERIES,
    AUTOMOTIVE_SERPAPI_QUERY_GATES,
)


class AutomotiveSearchQueryTests(unittest.TestCase):
    def test_queries_cover_the_requested_technical_concepts_exactly(self):
        query_text = "\n".join(AUTOMOTIVE_SERPAPI_QUERIES)
        requested_terms = (
            "NVH技术",
            "ANC主动降噪技术",
            "风洞测试技术",
            "低空飞行器测试技术",
            "电控仿真开发技术",
            "车辆风险等级开发技术",
            "科技越野属性开发技术",
            "智能驾驶",
            "智能网联汽车",
            "具身智能驾驶",
            "声振粗糙度",
            "主动噪声控制",
            "空气动力学测试",
            "eVTOL测试",
            "新型飞行器测试",
            "电子控制系统仿真",
            "智能驾驶风险评估",
            "越野智能化技术",
            "自动驾驶",
            "车联网",
            "汽车人形机器人",
            "车载智能体技术",
        )

        for term in requested_terms:
            with self.subTest(term=term):
                self.assertIn(f'"{term}"', query_text)

    def test_daily_queries_are_scoped_and_fit_the_runtime_limit(self):
        self.assertEqual(len(AUTOMOTIVE_SERPAPI_QUERIES), 6)
        self.assertGreaterEqual(
            config.SERPAPI_MAX_QUERIES_PER_RUN,
            len(AUTOMOTIVE_SERPAPI_QUERIES),
        )
        self.assertTrue(all(len(query) <= 500 for query in AUTOMOTIVE_SERPAPI_QUERIES))
        self.assertTrue(all("最新" not in query for query in AUTOMOTIVE_SERPAPI_QUERIES))
        self.assertTrue(all("site:" not in query for query in AUTOMOTIVE_SERPAPI_QUERIES))
        self.assertEqual(
            set(AUTOMOTIVE_SERPAPI_QUERY_GATES),
            set(AUTOMOTIVE_SERPAPI_QUERIES),
        )
        self.assertTrue(all(AUTOMOTIVE_SERPAPI_QUERY_GATES.values()))

    def test_generic_robot_terms_are_scoped_to_automotive(self):
        embodied = AUTOMOTIVE_EXACT_TOPIC_KEYWORDS["embodied_intelligent_driving"]

        self.assertIn("汽车机器人", embodied)
        self.assertIn("车载智能体技术", embodied)
        self.assertNotIn("机器人", embodied)
        self.assertNotIn("智能体", embodied)


if __name__ == "__main__":
    unittest.main()
