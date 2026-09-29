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
KB_ID = "114757cc616111f19380e5c7605e0f71"
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
            cur.execute("select id,name,prompt_config,top_n,top_k,similarity_threshold,vector_similarity_weight from dialog where id=%s", (CHAT_ID,))
            dialog = cur.fetchone()
            cur.execute("select id,name,language,similarity_threshold,vector_similarity_weight,embd_id from knowledgebase where id=%s", (KB_ID,))
            kb = cur.fetchone()
            if not dialog or not kb:
                raise RuntimeError("dialog or knowledgebase not found")

            backup_path = BACKUP_DIR / f"multilingual-vector-before-{stamp}.json"
            backup_path.write_text(json.dumps({"dialog": dialog, "knowledgebase": kb}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

            prompt_config = as_json(dialog["prompt_config"])
            before_cross_languages = prompt_config.get("cross_languages")
            prompt_config["cross_languages"] = []
            prompt_config["quote"] = True
            prompt_config["refine_multiturn"] = True

            now = int(time.time() * 1000)
            cur.execute(
                """
                update dialog
                   set prompt_config=%s,
                       top_n=%s,
                       top_k=%s,
                       similarity_threshold=%s,
                       vector_similarity_weight=%s,
                       update_time=%s
                 where id=%s
                """,
                (
                    json.dumps(prompt_config, ensure_ascii=False),
                    10,
                    1024,
                    0.03,
                    0.7,
                    now,
                    CHAT_ID,
                ),
            )
            cur.execute(
                """
                update knowledgebase
                   set similarity_threshold=%s,
                       vector_similarity_weight=%s,
                       update_time=%s
                 where id=%s
                """,
                (0.03, 0.7, now, KB_ID),
            )
        conn.commit()
        print(
            json.dumps(
                {
                    "ok": True,
                    "cross_languages_before": before_cross_languages,
                    "cross_languages_after": [],
                    "vector_similarity_weight_after": 0.7,
                    "similarity_threshold_after": 0.03,
                    "backup": str(backup_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
