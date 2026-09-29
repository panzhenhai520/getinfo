# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, r"F:\CollectInfo")
from db_connection import connect_postgres_primary

conn = connect_postgres_primary()
cur = conn.cursor()
cur.execute("""
  SELECT tablename FROM pg_catalog.pg_tables
  WHERE schemaname = 'public' ORDER BY tablename
""")
tables = [r[0] for r in cur.fetchall()]
print("total tables:", len(tables))
for t in tables:
    try:
        cur.execute(f'SELECT COUNT(*) FROM "{t}"')
        n = cur.fetchone()[0]
        print(f"  {t:45s} {n}")
    except Exception as e:
        print(f"  {t:45s} ERR {str(e)[:60]}")
cur.close()
conn.close()
