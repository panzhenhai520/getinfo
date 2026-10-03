#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""markdown 信源配置解析器回归测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industry_pack_markdown import (  # noqa: E402
    merge_into_pack,
    parse_industry_pack_markdown,
)

SAMPLE = """# 测试行业包配置

## 一、关键词门控

### 1.1 核心关键词（权重 3）

```
投资管理
股权投资
```

### 1.2 扩展关键词（权重 1）

```
天使轮
A轮
```

### 1.3 趋势关键词（权重 2）

```
监管
新规
```

### 1.4 事件关键词（权重 2）

```
IPO
并购
```

### 1.5 行业锚点词（硬门槛）

```
私募基金管理人
母基金
```

### 1.6 备案机构（硬门槛）

```
中国证券投资基金业协会
```

### 1.8 重点品牌（不参与准入，供品牌趋势 / SERP / 主题命名）

```
华为投资控股有限公司
腾讯投资
```

### 1.9 趋势主题（每行一个，留空则不显示主题维度）

```
国资与政府引导基金
监管政策与合规
```

### 1.10 搜索发现查询（每行一条完整查询）

**行业通用查询（15 条）**

```
投资管理 行业 最新动态
私募股权 募资 最新
```

**关注主体定向查询**

```
华为投资控股 投资管理 动态
```

---

## 二、内部主题标签

| 主题 key   | 显示名称    | 归属关键词（每行一个）        | 发现搜索词 1   | 发现搜索词 2  | 发现搜索词 3  |
| -------- | ------- | ------------------ | --------- | -------- | -------- |
| topic_1  | 监管与备案   | 监管 / 新规 / 基金备案     | 私募基金 备案 新规 | 基金业协会 自律处分 | 私募基金管理人 注销 |
| topic_12 | 关注主体动态  | 上述 34 家主体简称（每行一个） | 关注主体 + 投资管理 动态 | 关注主体 + 基金 设立 | 关注主体 + 股权投资 |

---

## 三、来源管理

### 3.1 媒体与聚合源（RSS 已实测）

| 名称          | URL                              | 来源角色 | 类型  | 频率   | 可信度 |
| ----------- | -------------------------------- | ---- | --- | ---- | --- |
| 钛媒体 ✅       | <https://www.tmtpost.com/rss.xml> | 行业媒体 | RSS | 720  | 3   |

### 3.3 关注主体官网

`来源角色 = 企业官网`，`类型 = 网站`，`频率 = 1440`，`可信度 = 4`

| 名称             | URL                              |
| -------------- | -------------------------------- |
| 华为投资控股         | <https://www.huawei.com/>        |
"""


class IndustryPackMarkdownTest(unittest.TestCase):
    def setUp(self):
        self.parsed = parse_industry_pack_markdown(SAMPLE)

    def test_keyword_groups(self):
        p = self.parsed
        self.assertEqual(p["core_keywords"], ["投资管理", "股权投资"])
        self.assertEqual(p["expanded_keywords"], ["天使轮", "A轮"])
        self.assertEqual(p["trend_keywords"], ["监管", "新规"])
        self.assertEqual(p["event_keywords"], ["IPO", "并购"])

    def test_anchor_keywords_merge_hard_gates(self):
        # 1.5 锚点词 + 1.6 备案机构 合并进同一门禁
        self.assertEqual(
            self.parsed["anchor_keywords"],
            ["私募基金管理人", "母基金", "中国证券投资基金业协会"],
        )

    def test_brands_and_topics_and_queries(self):
        p = self.parsed
        self.assertEqual(p["brands"], ["华为投资控股有限公司", "腾讯投资"])
        self.assertEqual(p["trend_topics"], ["国资与政府引导基金", "监管政策与合规"])
        # 1.10 有两段代码块，都要收集
        self.assertEqual(
            p["serpapi_queries"],
            ["投资管理 行业 最新动态", "私募股权 募资 最新", "华为投资控股 投资管理 动态"],
        )

    def test_fixed_topics_and_placeholder_resolution(self):
        topics = {t["key"]: t for t in self.parsed["fixed_topics"]}
        self.assertEqual(topics["topic_1"]["name"], "监管与备案")
        self.assertEqual(topics["topic_1"]["keywords"], ["监管", "新规", "基金备案"])
        self.assertEqual(topics["topic_1"]["search_terms"][0], "私募基金 备案 新规")
        # topic_12 的占位描述应替换为重点品牌
        self.assertEqual(topics["topic_12"]["keywords"], self.parsed["brands"])

    def test_default_sources(self):
        sources = {s["name"]: s for s in self.parsed["default_sources"]}
        rss = sources["钛媒体"]
        self.assertEqual(rss["url"], "https://www.tmtpost.com/rss.xml")
        self.assertEqual(rss["source_type"], "rss")
        self.assertEqual(rss["source_role"], "professional_trade_media")
        self.assertEqual(rss["polling_interval_minutes"], 720)
        self.assertEqual(rss["authority_level"], 3)
        # 3.3（只有名称|URL）用段内声明的默认值补齐
        official = sources["华为投资控股"]
        self.assertEqual(official["source_role"], "issuer_official")
        self.assertEqual(official["source_type"], "website")
        self.assertEqual(official["polling_interval_minutes"], 1440)
        self.assertEqual(official["authority_level"], 4)

    def test_merge_into_pack(self):
        pack = {
            "core_keywords": ["已有词"],
            "fixed_topics": [{"key": "topic_1", "name": "旧名", "keywords": ["旧词"]}],
            "default_sources": [{"name": "已有源", "url": "https://existing.example.com"}],
        }
        merged = merge_into_pack(pack, self.parsed)
        self.assertIn("已有词", merged["core_keywords"])
        self.assertIn("投资管理", merged["core_keywords"])
        self.assertEqual(merged["candidate_gate"]["anchor_keywords"], self.parsed["anchor_keywords"])
        topic1 = [t for t in merged["fixed_topics"] if t["key"] == "topic_1"][0]
        self.assertIn("旧词", topic1["keywords"])
        self.assertIn("监管", topic1["keywords"])
        urls = [s["url"] for s in merged["default_sources"]]
        self.assertIn("https://existing.example.com", urls)
        self.assertIn("https://www.tmtpost.com/rss.xml", urls)

        replaced = merge_into_pack(pack, self.parsed, replace_sources=True)
        self.assertEqual(
            [s["url"] for s in replaced["default_sources"]],
            [s["url"] for s in self.parsed["default_sources"]],
        )

    def test_empty_input_is_safe(self):
        p = parse_industry_pack_markdown("")
        self.assertEqual(p["core_keywords"], [])
        self.assertEqual(p["default_sources"], [])
        self.assertTrue(p["warnings"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
