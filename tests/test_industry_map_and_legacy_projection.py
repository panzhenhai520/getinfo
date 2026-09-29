import json
import tempfile
import unittest
from pathlib import Path

from intel_database import IntelRepository
from intel_sources import IntelSourceRegistry, canonicalize_source_url
from sqlite_database import SQLiteDatabase
from industry_collection_runtime import finalize_industry_initialization


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class IndustryMapAndLegacyProjectionTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(
            str(Path(self.temp_dir.name) / "industry-map.sqlite3")
        )
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.database.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_configs(
                id INTEGER PRIMARY KEY, name TEXT, login_url TEXT,
                username TEXT, password TEXT, username_selector TEXT,
                password_selector TEXT, submit_selector TEXT,
                wait_after_submit INTEGER
            )
            """
        )
        self.repository = IntelRepository(self.database)
        self.registry = IntelSourceRegistry(self.database)
        for key, value in (
            ("active_industry_pack_id", "education_news"),
            ("active_industry_pack_version_id", "17"),
            ("active_industry_activation_id", "activation-education"),
        ):
            self.database.connection.execute(
                """
                INSERT INTO intel_runtime_settings(setting_key, setting_value)
                VALUES(?, ?) ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value=excluded.setting_value
                """,
                (key, value),
            )
        self.database.connection.commit()

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _article(self, slug, title, pack_id, activation_id, anchor=True):
        article_id = int(
            self.database.connection.execute(
                """
                INSERT INTO articles(
                    url, title, content, domain, publish_date,
                    content_length, matched_keywords, status
                ) VALUES(?, ?, ?, 'example.test', '2026-08-06', 80, ?, 'active')
                """,
                (
                    f"https://example.test/{slug}",
                    title,
                    f"{title} 的行业事件发生在香港。",
                    json.dumps([title], ensure_ascii=False),
                ),
            ).lastrowid
        )
        self.repository.upsert_classification(
            {
                "article_id": article_id,
                "industry_pack_id": pack_id,
                "activation_id": activation_id,
                "industry_pack_version": "1.0.0",
                "classifier_version": "test",
                "article_content_hash": slug,
                "rule_category": "event",
                "rule_confidence": 0.9,
                "score_details": {"hits": {"anchor": [title] if anchor else []}},
                "matched_keywords": [title] if anchor else [],
                "final_category": "event",
                "final_confidence": 0.9,
            }
        )
        self.database.save_article_spacetime_profile(
            article_id,
            {
                "time_value": "2026-08-06T02:00:00Z",
                "time_type": "event_time",
                "time_confidence": 0.9,
                "location_name": "香港",
                "location_lat": 22.3193,
                "location_lng": 114.1694,
                "location_type": "event_location",
                "location_confidence": 0.9,
                "spacetime_status": "ready",
                "analysis_version": "test",
            },
        )
        return article_id

    def test_map_points_share_dashboard_pack_activation_and_dedupe_gate(self):
        representative = self._article(
            "education", "教育政策", "education_news", "activation-education"
        )
        duplicate = self._article(
            "education-copy", "教育政策转载", "education_news", "activation-education"
        )
        self._article(
            "family", "家族办公室", "family_office", "activation-family"
        )
        self._article(
            "unmatched", "普通家具", "education_news", "activation-education", anchor=False
        )
        group_id = int(
            self.database.connection.execute(
                """
                INSERT INTO intel_evidence_groups(
                    industry_pack_id, activation_id, event_key,
                    representative_article_id
                ) VALUES('education_news','activation-education','education-event',?)
                """,
                (representative,),
            ).lastrowid
        )
        self.database.connection.execute(
            """
            INSERT INTO intel_evidence_group_articles(
                evidence_group_id, article_id, relationship
            ) VALUES(?, ?, 'duplicate')
            """,
            (group_id, duplicate),
        )
        self.database.connection.commit()

        rows = self.database.get_article_spacetime_points(
            from_date="2026-08-01T00:00:00Z",
            to_date="2026-08-07T23:59:59Z",
            min_confidence=0.1,
            industry_pack_id="education_news",
            activation_id="activation-education",
        )
        self.assertEqual([row["id"] for row in rows], [representative])
        self.assertEqual(rows[0]["industry_pack_id"], "education_news")

    def test_pack_sources_are_stable_legacy_url_projections_and_restore_by_scope(self):
        source_ids = []
        for url, name, pack_id, ownership in (
            ("https://education.example.test/news", "教育信源", "education_news", "pack_owned"),
            ("https://finance.example.test/feed", "共享金融信源", "financial_markets", "shared_financial"),
        ):
            source_id = int(
                self.database.connection.execute(
                    """
                    INSERT INTO intel_sources(
                        canonical_source_url, source_url, source_name,
                        source_type, content_type, authority_level,
                        polling_interval_minutes, is_enabled, metadata_json
                    ) VALUES(?, ?, ?, 'website', 'media', 3, 1440, 1, '{}')
                    """,
                    (canonicalize_source_url(url), url, name),
                ).lastrowid
            )
            self.database.connection.execute(
                """
                INSERT INTO intel_source_industries(
                    source_id, industry_pack_id, ownership_type, is_active
                ) VALUES(?, ?, ?, 1)
                """,
                (source_id, pack_id, ownership),
            )
            source_ids.append(source_id)
        self.database.connection.execute(
            """
            INSERT INTO managed_urls(
                url, name, is_active, industry_pack_id, ownership_type
            ) VALUES('https://family.example.test/news','家办旧信源',1,
                     'family_office','legacy_family')
            """
        )
        self.database.connection.commit()

        first = self.registry.project_effective_sources_to_managed_urls(
            "education_news",
            effective_pack_ids=["education_news", "financial_markets"],
            activation_id="activation-education",
            industry_pack_version_id=17,
            project_keywords=["教育"],
        )
        second = self.registry.project_effective_sources_to_managed_urls(
            "education_news",
            effective_pack_ids=["education_news", "financial_markets"],
            activation_id="activation-education-2",
            industry_pack_version_id=18,
            project_keywords=["教育"],
        )
        current, current_total = self.database.get_managed_urls(
            page=1,
            per_page=100,
            industry_pack_id="education_news",
            effective_pack_ids=["education_news", "financial_markets"],
        )
        family, family_total = self.database.get_managed_urls(
            page=1,
            per_page=100,
            industry_pack_id="family_office",
            effective_pack_ids=["family_office"],
        )
        self.assertEqual(first["managed_urls_created"], 2)
        self.assertEqual(second["managed_urls_created"], 0)
        self.assertEqual(current_total, 2)
        self.assertEqual({row["name"] for row in current}, {"教育信源", "共享金融信源"})
        self.assertEqual(family_total, 1)
        self.assertEqual(family[0]["name"], "家办旧信源")
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM managed_urls"
            ).fetchone()[0],
            3,
        )

    def test_legacy_task_history_is_filtered_not_deleted(self):
        for pack_id in ("family_office", "education_news"):
            self.database.connection.execute(
                """
                INSERT INTO scheduled_tasks(
                    task_name, task_type, target_url, schedule_type,
                    industry_pack_id, is_active
                ) VALUES(?, 'crawl', ?, 'daily', ?, 1)
                """,
                (pack_id, f"https://{pack_id}.example.test", pack_id),
            )
        self.database.connection.commit()
        education, education_total = self.database.get_scheduled_tasks(
            1, 100, industry_pack_id="education_news"
        )
        family, family_total = self.database.get_scheduled_tasks(
            1, 100, industry_pack_id="family_office"
        )
        self.assertEqual(education_total, 1)
        self.assertEqual(education[0]["task_name"], "education_news")
        self.assertEqual(family_total, 1)
        self.assertEqual(family[0]["task_name"], "family_office")
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM scheduled_tasks"
            ).fetchone()[0],
            2,
        )

    def test_source_projection_creates_restorable_schedule_and_initial_tasks(self):
        source_id = int(
            self.database.connection.execute(
                """
                INSERT INTO intel_sources(
                    canonical_source_url, source_url, source_name,
                    source_type, content_type, authority_level,
                    polling_interval_minutes, is_enabled, metadata_json
                ) VALUES(?, 'https://education.example.test/news', '教育信源',
                         'website', 'media', 3, 1440, 1,
                         '{"preferred_scan_time":"07:20"}')
                """,
                (canonicalize_source_url('https://education.example.test/news'),),
            ).lastrowid
        )
        self.database.connection.execute(
            """
            INSERT INTO intel_source_industries(
                source_id, industry_pack_id, ownership_type, is_active
            ) VALUES(?, 'education_news', 'pack_owned', 1)
            """,
            (source_id,),
        )
        self.database.connection.commit()
        self.registry.project_effective_sources_to_managed_urls(
            'education_news',
            effective_pack_ids=['education_news'],
            activation_id='activation-education',
            industry_pack_version_id=17,
            project_keywords=['教育'],
        )
        first = self.registry.project_effective_sources_to_collection_tasks(
            'education_news',
            effective_pack_ids=['education_news'],
            activation_id='activation-education',
            industry_pack_version_id=17,
            project_keywords=['教育'],
        )
        second = self.registry.project_effective_sources_to_collection_tasks(
            'education_news',
            effective_pack_ids=['education_news'],
            activation_id='activation-education',
            industry_pack_version_id=17,
            project_keywords=['教育'],
        )
        self.assertEqual(first['source_count'], 1)
        self.assertEqual(first['schedules_created'], 1)
        self.assertEqual(first['crawl_tasks_created'], 1)
        self.assertEqual(second['schedules_created'], 0)
        self.assertEqual(second['crawl_tasks_created'], 0)
        schedule = self.database.connection.execute(
            "SELECT * FROM scheduled_tasks WHERE industry_pack_id='education_news'"
        ).fetchone()
        self.assertEqual(schedule['ownership_type'], 'source_registry_projection')
        self.assertEqual(schedule['schedule_time'], '07:20:00')
        self.assertEqual(schedule['is_active'], 0)
        self.assertTrue(json.loads(schedule['config'])['initialization_pending'])
        task_id = first['crawl_task_ids_by_source'][str(source_id)]
        crawl = self.database.connection.execute(
            'SELECT * FROM crawl_tasks WHERE task_id=?', (task_id,)
        ).fetchone()
        self.assertEqual(crawl['status'], 'pending')
        self.assertEqual(crawl['industry_pack_id'], 'education_news')
        promoted = finalize_industry_initialization(
            activation_id='activation-education',
            industry_pack_id='education_news',
            report={'status': 'completed'},
            database=self.database,
        )
        self.assertTrue(promoted['finalized'])
        self.assertEqual(promoted['promoted_schedule_count'], 1)
        schedule = self.database.connection.execute(
            "SELECT * FROM scheduled_tasks WHERE industry_pack_id='education_news'"
        ).fetchone()
        self.assertEqual(schedule['is_active'], 1)
        self.assertFalse(json.loads(schedule['config'])['initialization_pending'])

    def test_legacy_article_projection_is_scoped_to_active_industry(self):
        education = self._article(
            'education-scope', '教育政策', 'education_news', 'activation-education'
        )
        self._article(
            'family-scope', '家族办公室', 'family_office', 'activation-family'
        )
        articles, total = self.database.get_articles(
            1, 100,
            industry_pack_id='education_news',
            activation_id='activation-education',
        )
        self.assertEqual(total, 1)
        self.assertEqual(articles[0]['id'], education)
        self.assertEqual(articles[0]['matched_keywords'], ['教育政策'])
        stats = self.database.get_statistics(
            industry_pack_id='education_news',
            activation_id='activation-education',
        )
        self.assertEqual(stats['total_articles'], 1)
        keywords = self.database.get_keyword_map(
            20,
            industry_pack_id='education_news',
            activation_id='activation-education',
        )
        self.assertEqual([item['keyword'] for item in keywords], ['教育政策'])

    def test_map_popup_reuses_dashboard_card_renderer(self):
        template = (PROJECT_ROOT / "templates" / "mapindex.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("card.innerHTML = renderIntelCard(point.article);", template)
        self.assertIn("bindIntelArticleButtons(card);", template)
        self.assertIn("translateIntelCard(${articleId},event)", template)
        self.assertIn("followIntelArticle(${articleId},event)", template)

    def test_map_popup_closes_as_soon_as_mouse_leaves_card(self):
        template = (PROJECT_ROOT / "templates" / "mapindex.html").read_text(
            encoding="utf-8"
        )
        mouseleave = template.split(
            "detailDrawer.addEventListener('mouseleave'", 1
        )[1].split("});", 1)[0]
        self.assertIn("closeDetailCard();", mouseleave)
        self.assertNotIn("scheduleDetailClose", mouseleave)

    def test_map_point_hover_radiates_and_opens_popup_automatically(self):
        template = (PROJECT_ROOT / "templates" / "mapindex.html").read_text(
            encoding="utf-8"
        )
        for marker in (
            "function drawMapHoverRadiation(point, animationTime)",
            "rgba(29, 127, 104",
            "function startMapHoverAnimation(point)",
            "canvas.addEventListener('pointermove'",
            "hoverMapPoint(nearestVisibleMapPoint(event.clientX, event.clientY))",
            "state.mapHoverCardTimer = setTimeout",
            "openMapPointCard(point)",
            "prefers-reduced-motion: reduce",
        ):
            self.assertIn(marker, template)

    def test_article_selection_can_be_quoted_into_ai_assistant(self):
        template = (PROJECT_ROOT / "templates" / "mapindex.html").read_text(
            encoding="utf-8"
        )
        quote_button = template.index('id="quoteSelectionToAiBtn"')
        translate_button = template.index('id="modalTranslateBtn"')
        self.assertLess(quote_button, translate_button)
        for marker in (
            "function quoteArticleSelectionToAssistant(event)",
            "window.getSelection?.()",
            "document.getElementById('chatInput')",
            "drawer.classList.add('article-quote-open')",
            "setChatOpen(true)",
            "input.value = draft ? `${draft}\\n\\n${selectedText}` : selectedText",
        ):
            self.assertIn(marker, template)

    def test_selected_chat_text_can_be_dragged_into_chat_input(self):
        template = (PROJECT_ROOT / "templates" / "mapindex.html").read_text(
            encoding="utf-8"
        )
        for marker in (
            "function currentSelectionWithin(root)",
            "function insertQuotedTextAtChatCursor(input, quotedText)",
            "chatMessagesDropSource.addEventListener('dragstart'",
            "chatQuoteDropTarget.addEventListener('dragover'",
            "chatQuoteDropTarget.addEventListener('drop'",
            "event.dataTransfer?.getData('text/plain') || draggedChatQuoteText",
        ):
            self.assertIn(marker, template)


if __name__ == "__main__":
    unittest.main()
