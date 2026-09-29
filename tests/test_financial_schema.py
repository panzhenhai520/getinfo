import sqlite3
import tempfile
import unittest
from pathlib import Path

import financial_schema
from financial_schema import (
    FINANCIAL_REQUIRED_TABLES,
    FINANCIAL_SCHEMA_VERSION,
    FINANCIAL_TABLE_DDL,
    ensure_financial_tables,
    financial_schema_checksum,
    get_financial_schema_version,
)
from sqlite_database import SQLiteDatabase


def _table_names(connection):
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


class FinancialSchemaMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "financial-schema.sqlite3"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_connect_does_not_run_financial_ddl_but_startup_init_does(self):
        database = SQLiteDatabase(str(self.db_path))
        self.assertTrue(database.connect())
        self.assertFalse(FINANCIAL_REQUIRED_TABLES & _table_names(database.connection))

        self.assertTrue(database.create_tables())
        self.assertTrue(FINANCIAL_REQUIRED_TABLES <= _table_names(database.connection))
        self.assertEqual(
            get_financial_schema_version(database.connection.cursor()),
            FINANCIAL_SCHEMA_VERSION,
        )
        self.assertEqual(database.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        database.disconnect()

    def test_repeated_initialization_is_idempotent(self):
        database = SQLiteDatabase(str(self.db_path))
        self.assertTrue(database.connect())
        self.assertTrue(database.create_tables())
        before = list(
            database.connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )

        self.assertTrue(database.create_tables())
        after = list(
            database.connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
        self.assertEqual(before, after)
        row = database.connection.execute(
            "SELECT version, schema_checksum, status FROM financial_schema_migrations"
        ).fetchone()
        self.assertEqual(tuple(row), (FINANCIAL_SCHEMA_VERSION, financial_schema_checksum(), "applied"))
        self.assertEqual(
            database.connection.execute("SELECT COUNT(*) FROM financial_schema_migrations").fetchone()[0],
            1,
        )
        database.disconnect()

    def test_interrupted_partial_schema_is_completed_on_rerun(self):
        connection = sqlite3.connect(self.db_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE articles(id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE intel_jobs(id INTEGER PRIMARY KEY)")
        for table_sql in FINANCIAL_TABLE_DDL[:7]:
            connection.execute(table_sql)

        ensure_financial_tables(connection.cursor())

        self.assertTrue(FINANCIAL_REQUIRED_TABLES <= _table_names(connection))
        self.assertEqual(get_financial_schema_version(connection.cursor()), FINANCIAL_SCHEMA_VERSION)
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        connection.close()

    def test_v1_alias_identity_migrates_without_losing_rows(self):
        connection = sqlite3.connect(self.db_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(FINANCIAL_TABLE_DDL[2])
        v1_alias_ddl = FINANCIAL_TABLE_DDL[3].replace(
            "UNIQUE (instrument_id, alias_normalized, market, provider_key, valid_from)",
            "UNIQUE (alias_normalized, market, provider_key, valid_from)",
        )
        connection.execute(v1_alias_ddl)
        connection.execute(
            """
            INSERT INTO financial_instruments(
                canonical_symbol, display_name, asset_type, market, exchange
            ) VALUES('legacy.SH', '旧标的', 'equity', 'CN', 'XSHG')
            """
        )
        connection.execute(
            """
            INSERT INTO financial_instrument_aliases(
                id, instrument_id, alias, alias_normalized, market, valid_from
            ) VALUES(17, 1, '共同名称', '共同名称', 'XSHG', '2020-01-01')
            """
        )

        ensure_financial_tables(connection.cursor())

        alias_row = connection.execute(
            "SELECT id, instrument_id, alias, valid_from "
            "FROM financial_instrument_aliases WHERE id=17"
        ).fetchone()
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='financial_instrument_aliases'"
        ).fetchone()[0]
        self.assertEqual(tuple(alias_row), (17, 1, "共同名称", "2020-01-01"))
        self.assertIn(
            "UNIQUE (instrument_id, alias_normalized, market, provider_key, valid_from)",
            table_sql,
        )
        self.assertEqual(
            get_financial_schema_version(connection.cursor()), FINANCIAL_SCHEMA_VERSION
        )
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        connection.close()

    def test_failed_migration_rolls_back_its_whole_savepoint(self):
        connection = sqlite3.connect(self.db_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        original_indexes = financial_schema.FINANCIAL_INDEX_DDL
        financial_schema.FINANCIAL_INDEX_DDL = original_indexes + ("INVALID MIGRATION SQL",)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                ensure_financial_tables(connection.cursor())
        finally:
            financial_schema.FINANCIAL_INDEX_DDL = original_indexes

        self.assertFalse(FINANCIAL_REQUIRED_TABLES & _table_names(connection))
        self.assertEqual(get_financial_schema_version(connection.cursor()), 0)
        connection.close()

    def test_existing_core_schema_rows_and_old_readers_are_unchanged(self):
        connection = sqlite3.connect(self.db_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE articles(id INTEGER PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL);
            CREATE TABLE chat_history(
                id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
                question TEXT NOT NULL, answer TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT NOT NULL);
            CREATE TABLE intel_jobs(id INTEGER PRIMARY KEY);
            INSERT INTO articles VALUES(1, 'https://example.test/a', 'baseline article');
            INSERT INTO chat_history VALUES(1, 'session-1', 'old question', 'old answer', '2026-07-31');
            INSERT INTO users VALUES(1, 'baseline-admin');
            """
        )
        core_tables = ("articles", "chat_history", "users", "intel_jobs")
        before_schema = {
            name: connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()[0]
            for name in core_tables
        }
        before_counts = {
            name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in core_tables
        }

        ensure_financial_tables(connection.cursor())

        after_schema = {
            name: connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()[0]
            for name in core_tables
        }
        after_counts = {
            name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in core_tables
        }
        old_history_row = connection.execute(
            "SELECT session_id, question, answer FROM chat_history WHERE id=1"
        ).fetchone()
        self.assertEqual(before_schema, after_schema)
        self.assertEqual(before_counts, after_counts)
        self.assertEqual(tuple(old_history_row), ("session-1", "old question", "old answer"))
        connection.close()

    def test_simulation_schema_cannot_be_repurposed_as_real_execution(self):
        database = SQLiteDatabase(str(self.db_path))
        self.assertTrue(database.connect())
        self.assertTrue(database.create_tables())
        connection = database.connection
        connection.execute(
            "INSERT INTO paper_accounts(id, account_name, base_currency, initial_cash, cash_balance) "
            "VALUES('paper-1', '测试账户', 'CNY', 100000, 100000)"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO paper_accounts(id, account_name, base_currency, initial_cash, cash_balance, execution_mode) "
                "VALUES('real-1', '错误账户', 'CNY', 100000, 100000, 'real')"
            )
        database.disconnect()


if __name__ == "__main__":
    unittest.main()
