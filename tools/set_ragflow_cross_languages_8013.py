import json
import time
from pathlib import Path

import pymysql


HOST = "192.168.1.246"
PORT = 5457
USER = "root"
PASSWORD = "infini_rag_flow"
DB = "rag_flow"
CHAT_ID = "eb78273e615f11f19380e5c7605e0f71"
LANGUAGES = ["English", "Chinese", "Cantonese"]
BACKUP_DIR = Path(r"\\192.168.1.246\docker-data\ragflow\ragflow-v0.25.6-fresh-8013\codex-backups")


def as_json(value):
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return {}
    return json.loads(value)


def main():
    stamp = time.strftime("%Y%m%d-%H%M%S")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    conn = pymysql.connect(
        host=HOST,
        port=PORT,
        user=USER,
        password=PASSWORD,
        database=DB,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("select id,name,prompt_config from dialog where id=%s", (CHAT_ID,))
            row = cur.fetchone()
            if not row:
                raise RuntimeError(f"dialog not found: {CHAT_ID}")

            backup_path = BACKUP_DIR / f"dialog-cross-languages-before-{stamp}.json"
            backup_path.write_text(json.dumps(row, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

            prompt_config = as_json(row["prompt_config"])
            before = prompt_config.get("cross_languages")
            prompt_config["cross_languages"] = LANGUAGES

            cur.execute(
                "update dialog set prompt_config=%s, update_time=%s where id=%s",
                (json.dumps(prompt_config, ensure_ascii=False), int(time.time() * 1000), CHAT_ID),
            )
        conn.commit()
        print(json.dumps({"ok": True, "cross_languages_before": before, "cross_languages_after": LANGUAGES, "backup": str(backup_path)}, ensure_ascii=False, indent=2))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
