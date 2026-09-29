import json

import pymysql


HOST = "192.168.1.246"
PORT = 5457
USER = "root"
PASSWORD = "infini_rag_flow"
DB = "rag_flow"
CHAT_ID = "eb78273e615f11f19380e5c7605e0f71"


def loads(value):
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return {}
    return json.loads(value)


def main():
    conn = pymysql.connect(
        host=HOST,
        port=PORT,
        user=USER,
        password=PASSWORD,
        database=DB,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                select id,name,language,similarity_threshold,vector_similarity_weight,embd_id
                  from knowledgebase
                 order by create_time desc
                """
            )
            kbs = cur.fetchall()

            cur.execute(
                """
                select id,name,kb_id,type,parser_id,run,status,progress,chunk_num,token_num
                  from document
                 order by create_time desc
                 limit 30
                """
            )
            docs = cur.fetchall()

            cur.execute(
                """
                select id,name,prompt_config,top_n,top_k,similarity_threshold,vector_similarity_weight
                  from dialog
                 where id=%s
                """,
                (CHAT_ID,),
            )
            dialogs = cur.fetchall()
            for dialog in dialogs:
                prompt_config = loads(dialog.pop("prompt_config"))
                dialog["prompt_config"] = {
                    "quote": prompt_config.get("quote"),
                    "cross_languages": prompt_config.get("cross_languages"),
                    "refine_multiturn": prompt_config.get("refine_multiturn"),
                }

        print(json.dumps({"knowledgebases": kbs, "documents": docs, "dialog": dialogs}, ensure_ascii=False, indent=2, default=str))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
