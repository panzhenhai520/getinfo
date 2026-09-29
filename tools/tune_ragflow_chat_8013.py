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


SYSTEM_PROMPT = """你是时和家辦专业AI顾问系统的知识库问答助手。你必须只根据【知识库】和聊天历史回答用户问题。

强制规则：
1. 始终使用中文回答。
2. 只要【知识库】里有相关内容，必须直接回答，禁止说“没学会”“不知道”“无法回答”“问题不清楚”。
3. 如果问题询问某人的“观点、表示、指出、认为、补充”，从【知识库】中提取该人前后相关表述并归纳，最多 8 条，每条一句话。
4. 知识库可能来自 PDF/OCR，个别字可能缺失或是异体字；请按上下文语义归纳，不要因为少数字符异常就拒答。
5. 多轮对话时，用户提到“第一次、上一条、第2点、刚才”等，必须结合聊天历史定位对应内容。
6. 不要输出思考过程、英文说明或无关客套话。
7. 只有当【知识库】完全没有相关内容时，才回答：知识库中未找到您要的答案！

【知识库】
{knowledge}
【知识库结束】"""


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
            cur.execute("select * from dialog where id=%s", (CHAT_ID,))
            dialog = cur.fetchone()
            if not dialog:
                raise RuntimeError(f"dialog not found: {CHAT_ID}")
            cur.execute("select * from knowledgebase where id=%s", (KB_ID,))
            kb = cur.fetchone()
            if not kb:
                raise RuntimeError(f"knowledgebase not found: {KB_ID}")

            backup = {"dialog": dialog, "knowledgebase": kb}
            backup_path = BACKUP_DIR / f"chat-kb-before-8b-tune-{stamp}.json"
            backup_path.write_text(json.dumps(backup, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

            prompt_config = as_json(dialog["prompt_config"])
            prompt_config.update(
                {
                    "system": SYSTEM_PROMPT,
                    "empty_response": "知识库中未找到您要的答案！",
                    "quote": False,
                    "reasoning": False,
                    "refine_multiturn": True,
                    "parameters": [{"key": "knowledge", "optional": False}],
                }
            )

            llm_setting = as_json(dialog["llm_setting"])
            llm_setting.update(
                {
                    "model_type": "chat",
                    "temperature": 0.05,
                    "top_p": 0.65,
                    "max_tokens": 768,
                }
            )

            cur.execute(
                """
                update dialog
                   set llm_id=%s,
                       llm_setting=%s,
                       prompt_config=%s,
                       language=%s,
                       top_n=%s,
                       top_k=%s,
                       similarity_threshold=%s,
                       vector_similarity_weight=%s,
                       update_time=%s
                 where id=%s
                """,
                (
                    "deepseek-r1:8b@Ollama",
                    json.dumps(llm_setting, ensure_ascii=False),
                    json.dumps(prompt_config, ensure_ascii=False),
                    "Chinese",
                    8,
                    1024,
                    0.05,
                    0.2,
                    int(time.time() * 1000),
                    CHAT_ID,
                ),
            )
            cur.execute(
                "update knowledgebase set language=%s, similarity_threshold=%s, vector_similarity_weight=%s, update_time=%s where id=%s",
                ("Chinese", 0.05, 0.2, int(time.time() * 1000), KB_ID),
            )
        conn.commit()
        print(json.dumps({"ok": True, "backup": str(backup_path)}, ensure_ascii=False, indent=2))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
