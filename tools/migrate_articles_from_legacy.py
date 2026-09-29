import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "crawler_articles.db"
DST = ROOT / "data" / "crawler_articles.db"


def table_columns(con, table):
    return [row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()]


def main():
    if not SRC.exists():
        raise SystemExit(f"缺少旧库: {SRC}")
    if not DST.exists():
        raise SystemExit(f"缺少运行库: {DST}")

    con = sqlite3.connect(str(DST), timeout=30)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    try:
        con.execute("PRAGMA busy_timeout=30000")
        cur.execute(f"ATTACH DATABASE ? AS src", (str(SRC),))

        row = cur.execute(
            "SELECT setting_value FROM intel_runtime_settings WHERE setting_key='active_industry_activation_id'"
        ).fetchone()
        activation_id = row[0] if row else ""
        if not activation_id:
            raise SystemExit("运行库没有 active_industry_activation_id，请先切换行业包后再运行本脚本")

        existing = cur.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        if existing:
            print(f"articles 已有 {existing} 条，跳过迁移")
            return

        # 补建衍生表（若运行库缺少）
        for table in ("article_derivatives", "article_audio_manifests"):
            exists = cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                sql = cur.execute(
                    "SELECT sql FROM src.sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()[0]
                cur.execute(sql)
                print(f"created table {table}")

        def copy_table(table, where=""):
            cols = table_columns(cur, table)
            src_cols = [r[1] for r in cur.execute(f"PRAGMA src.table_info({table})").fetchall()]
            if cols != src_cols:
                raise SystemExit(f"{table} 列不一致，停止迁移")
            sql = f"INSERT INTO {table} ({','.join(cols)}) SELECT {','.join(cols)} FROM src.{table}"
            if where:
                sql += f" {where}"
            cur.execute(sql)
            print(f"copied {table}: {cur.rowcount}")

        copy_table("articles")
        copy_table("content_industry_packs")
        copy_table(
            "article_intel_classifications",
            "WHERE article_id IN (SELECT id FROM src.articles)",
        )
        cur.execute(
            "UPDATE article_intel_classifications SET activation_id=? WHERE industry_pack_id='bolean_security_compute'",
            (activation_id,),
        )
        print(f"updated bolean activation rows: {cur.rowcount}")
        copy_table(
            "article_spacetime_profiles",
            "WHERE article_id IN (SELECT id FROM src.articles)",
        )
        copy_table(
            "article_derivatives",
            "WHERE article_id IN (SELECT id FROM src.articles)",
        )
        copy_table(
            "article_audio_manifests",
            "WHERE article_id IN (SELECT id FROM src.articles)",
        )

        con.commit()
        integrity = [str(r[0]) for r in cur.execute("PRAGMA integrity_check").fetchall()]
        print("integrity:", integrity)
        if integrity != ["ok"]:
            raise SystemExit("运行库完整性校验未通过，请不要启动服务")
        counts = {}
        for table in (
            "articles",
            "content_industry_packs",
            "article_intel_classifications",
            "article_spacetime_profiles",
            "article_derivatives",
            "article_audio_manifests",
        ):
            counts[table] = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print("counts:", counts)
        if counts["articles"] == 0:
            raise SystemExit("迁移后 articles 仍为 0，停止")
    finally:
        try:
            cur.execute("DETACH DATABASE src")
        except Exception:
            pass
        con.close()


if __name__ == "__main__":
    main()
