import unittest
from datetime import datetime, timezone

from intel_content_quality_gate import assess_article_quality
from intel_admission import assess_admission
from intel_llm_client import (
    IntelLLMError,
    build_admission_prompt,
    validate_admission_output,
)
from intel_url_expansion import expand_links


class IntelContentQualityGateTests(unittest.TestCase):
    def test_admission_output_accepts_fenced_json_and_ignores_extra_fields(self):
        result = validate_admission_output(
            """```json
            {"admission":"article","confidence":0.91,"reason":"正文完整", "link_expansion_recommended":false,"extra_note":"ignored"}
            ```"""
        )

        self.assertEqual(result["admission"], "article")
        self.assertEqual(result["confidence"], 0.91)
        self.assertNotIn("extra_note", result)

    def test_admission_output_still_fails_closed_on_missing_or_invalid_fields(self):
        invalid_values = (
            '{"admission":"article","confidence":0.9,"reason":"缺字段"}',
            '{"admission":"article","confidence":0.9,"reason":"类型错误","link_expansion_recommended":"false"}',
        )

        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(IntelLLMError):
                validate_admission_output(value)

    def test_admission_prompt_grounds_future_date_checks_in_server_time(self):
        prompt = build_admission_prompt(
            {
                "title": "智能驾驶测试进展",
                "content": "文章发布于 2026-08-07，企业计划在 2027 年量产。",
                "publish_date": "2026-08-07",
                "url": "https://example.com/news",
            },
            {"name": "汽车行业"},
            now=datetime(2026, 8, 8, 0, 30, tzinfo=timezone.utc),
        )

        self.assertIn(
            "当前服务时间（Asia/Hong_Kong）：2026-08-08T08:30:00+08:00",
            prompt,
        )
        self.assertIn("早于或等于当前服务时间的日期不是未来日期", prompt)
        self.assertIn("未来目标、规划或预测", prompt)
        self.assertIn('"publish_date": "2026-08-07"', prompt)

    def test_rejects_short_truncated_and_invalid_quality(self):
        result = assess_article_quality(
            {"title": "2020年影响家族办公室的十大趋势", "content": "家族办公室正...", "publish_date": "2026-07-22", "quality_score": 120},
            {"title": "2020年影响家族办公室的十大趋势"},
        )
        self.assertFalse(result["passed"])
        self.assertIn("content_too_short", result["issues"])
        self.assertIn("content_looks_truncated", result["issues"])
        # 质量分超界按实现被钳制到 [0,100]，不再产生 issue（避免 out_of_range 误拒）
        self.assertEqual(result["quality_score"], 100.0)
        self.assertIn("title_publish_date_conflict", result["issues"])

    def test_accepts_complete_article_and_marks_missing_metadata(self):
        result = assess_article_quality(
            {"title": "香港家族办公室政策更新", "content": "完整正文。" * 100, "quality_score": 88},
            {},
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["metadata_issues"], ["site_name_missing", "publish_date_missing"])

    def test_directory_is_rejected_and_yields_only_same_origin_industry_links(self):
        page = {"url": "https://hongkong.acclime.com/zh-hant/", "content": "服務 公司註冊 聯絡 " * 100}
        decision = assess_admission(page, {"original_url": page["url"]}, {"name": "家办", "candidate_gate": {"anchor_keywords": ["家族办公室"]}})
        self.assertEqual(decision["admission"], "directory")
        links = expand_links("[家族办公室](https://hongkong.acclime.com/zh-hant/private-clients/family-office/) [外链](https://bad.example/a)", page["url"], {"candidate_gate": {"anchor_keywords": ["家族办公室"]}})
        self.assertEqual([item["url"] for item in links], ["https://hongkong.acclime.com/zh-hant/private-clients/family-office/"])


if __name__ == "__main__":
    unittest.main()
