import base64
import json
from pathlib import Path

import requests
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA


BASE_URL = "http://192.168.1.246:8013"
EMAIL = "admin@ragflow.io"
PASSWORD = "admin"
PUBLIC_KEY = Path("ragflow_v0_25_6_custom_source/conf/public.pem")
DATASET_ID = "f59e855461ba11f187f8f776bde46f3f"
DOCS = {
    "zh": "f5a6548261ba11f187f8f776bde46f3f",
    "en": "f5b7d51861ba11f187f8f776bde46f3f",
    "yue": "f5c3a29e61ba11f187f8f776bde46f3f",
}


def encrypt_password(password: str) -> str:
    key = RSA.import_key(PUBLIC_KEY.read_text(encoding="utf-8"), passphrase="Welcome")
    cipher = PKCS1_v1_5.new(key)
    payload = base64.b64encode(password.encode("utf-8"))
    return base64.b64encode(cipher.encrypt(payload)).decode("utf-8")


def ok(resp):
    data = resp.json()
    if resp.status_code != 200 or data.get("code") != 0:
        raise RuntimeError(f"{resp.status_code} {data}")
    return data.get("data") or {}


def chunk_summary(data):
    return [
        {
            "document_name": c.get("document_name"),
            "similarity": c.get("similarity"),
            "vector_similarity": c.get("vector_similarity"),
            "term_similarity": c.get("term_similarity"),
            "content": (c.get("content") or "").replace("\n", " ")[:160],
        }
        for c in data.get("chunks") or []
    ]


def search(session, question, doc_ids=None, cross_languages=None):
    body = {
        "question": question,
        "doc_ids": doc_ids or [],
        "page": 1,
        "size": 12,
        "top_k": 1024,
        "similarity_threshold": 0.0,
        "vector_similarity_weight": 0.9,
        "keyword": False,
        "cross_languages": cross_languages or [],
    }
    return chunk_summary(ok(session.post(f"{BASE_URL}/api/v1/datasets/{DATASET_ID}/search", json=body, timeout=180)))


def main():
    session = requests.Session()
    ok(session.post(f"{BASE_URL}/api/v1/auth/login", json={"email": EMAIL, "password": encrypt_password(PASSWORD)}, timeout=30))
    report = {
        "zh_query_all_docs_no_cross": search(session, "家族信托的三项核心价值是什么？"),
        "zh_query_en_only_no_cross": search(session, "家族信托的三项核心价值是什么？", [DOCS["en"]]),
        "zh_query_en_only_cross_english": search(session, "家族信托的三项核心价值是什么？", [DOCS["en"]], ["English"]),
        "english_query_en_only": search(session, "What are the three core values of a family trust?", [DOCS["en"]]),
        "zh_query_yue_only_no_cross": search(session, "家族信托的三项核心价值是什么？", [DOCS["yue"]]),
        "zh_query_yue_only_cross_chinese": search(session, "家族信托的三项核心价值是什么？", [DOCS["yue"]], ["Chinese"]),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
