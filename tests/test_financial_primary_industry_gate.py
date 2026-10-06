import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import config
from financial_feed import FinancialFeedService
from industry_packs import IndustryPackLoader
from sqlite_database import SQLiteDatabase


ROOT = Path(__file__).resolve().parents[1]


class FinancialPrimaryIndustryGateTest(unittest.TestCase):
    def setUp(self):
        # conftest 的 DATABASE_TYPE=sqlite 会被 .env 覆盖（config 里仍是 postgres），
        # SQLiteDatabase(path) 只改路径不改后端，本文件的快照/文章会真的写进
        # 共享主库；这里强制回到临时 SQLite。
        for item in (
            patch.object(config, "DATABASE_TYPE", "sqlite"),
            patch("db_connection.database_type", lambda: "sqlite"),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        packs = root / "packs"
        packs.mkdir()
        finance = json.loads(
            (ROOT / "config/industry_packs/financial_markets.json").read_text(
                encoding="utf-8"
            )
        )
        automotive = copy.deepcopy(
            json.loads(
                (ROOT / "config/industry_packs/education_news.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        automotive.update(
            {
                "id": "automotive",
                "name": "汽车行业",
                "core_keywords": ["汽车", "新能源汽车"],
                "expanded_keywords": ["智能驾驶"],
                "dashboard_capabilities": {
                    "show_financial_news": True,
                    "show_market_index_cards": True,
                    "show_watched_stock_cards": True,
                },
            }
        )
        for pack in (finance, automotive):
            (packs / f"{pack['id']}.json").write_text(
                json.dumps(pack, ensure_ascii=False), encoding="utf-8"
            )
        self.loader = IndustryPackLoader(str(packs), use_published_store=False)
        self.database = SQLiteDatabase(str(root / "financial-gate.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        for key, value in (
            ("active_industry_pack_id", "automotive"),
            ("active_industry_pack_version_id", "1"),
            ("active_industry_activation_id", "auto-activation"),
        ):
            self.database.connection.execute(
                "INSERT INTO intel_runtime_settings(setting_key,setting_value) VALUES(?,?)",
                (key, value),
            )
        provider_id = self.database.connection.execute(
            """
            INSERT INTO financial_provider_profiles(
                provider_key,display_name,provider_type,access_tier,
                capabilities_json,is_enabled,health_status
            ) VALUES('fixture','Fixture','market_data','free','[]',1,'healthy')
            """
        ).lastrowid
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        now = self.now.isoformat()
        for symbol, name, price in (
            ("AUTO.CN", "新能源汽车产业指数", 1200),
            ("BANK.CN", "银行产业指数", 900),
        ):
            instrument_id = self.database.connection.execute(
                """
                INSERT INTO financial_instruments(
                    canonical_symbol,display_name,asset_type,market,currency
                ) VALUES(?,?,'equity','CN','CNY')
                """,
                (symbol, name),
            ).lastrowid
            payload = json.dumps({"last_price": price}, sort_keys=True)
            self.database.connection.execute(
                """
                INSERT INTO financial_data_snapshots(
                    snapshot_key,instrument_id,provider_profile_id,data_type,
                    observed_at,fetched_at,market_status,currency,timezone,
                    quality_status,payload_json,payload_sha256,source_url
                ) VALUES(?,?,?,'quote',?,?,'open','CNY','Asia/Shanghai',
                         'verified',?,?, 'https://example.test/quote')
                """,
                (
                    f"snapshot-{symbol}", instrument_id, provider_id, now, now,
                    payload, hashlib.sha256(payload.encode()).hexdigest(),
                ),
            )
        self.database.connection.commit()

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def test_non_family_financial_cards_require_primary_industry_keyword(self):
        with patch.object(config, "FINANCIAL_INTELLIGENCE_ENABLED", True), patch.object(
            config, "TRADING_AGENTS_ENABLED", False
        ), patch.object(
            # 生产 .env 默认 FINANCIAL_ROLLOUT_STAGE=off（fail-closed），会先于
            # 行业包闸门把 feed 关掉；本文件验证的是"主行业包关键词闸门"。
            config, "FINANCIAL_ROLLOUT_STAGE", "simulation_backtest"
        ):
            payload = FinancialFeedService(
                self.database,
                pack_loader=self.loader,
                clock=lambda: datetime.now(timezone.utc),
            ).build(industry_pack_id="automotive", time_range="7d")

        self.assertTrue(payload["project_keyword_gate"]["strict_for_financial_aggregation"])
        self.assertEqual(payload["market_overview"], [])
        self.assertEqual(payload["counts"]["market_fact"], 1)
        self.assertEqual(
            [item["scope"]["symbol"] for item in payload["items"]],
            ["AUTO.CN"],
        )
        self.assertEqual(
            payload["items"][0]["matched_keywords"],
            ["汽车", "新能源汽车"],
        )

    def test_automotive_financial_news_uses_primary_article_finance_signals(self):
        article_id = self.database.connection.execute(
            """
            INSERT INTO articles(
                url,title,content,domain,publish_date,status,content_hash,
                content_length,matched_keywords
            ) VALUES(
                'https://auto.example.test/funding',
                '新能源汽车企业完成新一轮融资',
                '新能源汽车企业宣布完成融资并扩大汽车研发投入。',
                'auto.example.test',?,'active','auto-finance-hash',32,
                '新能源汽车,汽车,融资,投资'
            )
            """,
            (self.now.date().isoformat(),),
        ).lastrowid
        self.database.connection.execute(
            """
            INSERT INTO article_intel_classifications(
                article_id,industry_pack_id,activation_id,industry_pack_version,
                classifier_version,article_content_hash,rule_category,
                rule_confidence,rule_reason,score_details_json,
                matched_keywords_json,final_category,final_confidence,final_reason
            ) VALUES(
                ?,'automotive','auto-activation','1.0.0','fixture',
                'auto-finance-hash','event',1,'fixture',?,
                '["汽车","新能源汽车","融资"]','event',1,'fixture'
            )
            """,
            (
                article_id,
                json.dumps({"hits": {"anchor": ["汽车", "新能源汽车"]}}),
            ),
        )
        self.database.connection.commit()

        with patch.object(config, "FINANCIAL_INTELLIGENCE_ENABLED", True), patch.object(
            config, "TRADING_AGENTS_ENABLED", False
        ), patch.object(
            config, "FINANCIAL_ROLLOUT_STAGE", "simulation_backtest"
        ):
            payload = FinancialFeedService(
                self.database,
                pack_loader=self.loader,
                clock=lambda: self.now,
            ).build(industry_pack_id="automotive", time_range="7d")

        self.assertEqual(payload["counts"]["source_document"], 1)
        source = next(
            item for item in payload["items"]
            if item["content_kind"] == "source_document"
        )
        self.assertEqual(source["article_id"], article_id)
        self.assertEqual(source["matched_keywords"], ["汽车", "新能源汽车"])


if __name__ == "__main__":
    unittest.main()
