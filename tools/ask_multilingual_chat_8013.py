import base64
import json
import uuid
from pathlib import Path

import requests
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA


BASE_URL = "http://192.168.1.246:8013"
EMAIL = "admin@ragflow.io"
PASSWORD = "admin"
PUBLIC_KEY = Path("ragflow_v0_25_6_custom_source/conf/public.pem")
CHAT_ID = "070b560061bb11f187f8f776bde46f3f"


def encrypt_password(password: str) -> str:
    key = RSA.import_key(PUBLIC_KEY.read_text(encoding="utf-8"), passphrase="Welcome")
    cipher = PKCS1_v1_5.new(key)
    payload = base64.b64encode(password.encode("utf-8"))
    return base64.b64encode(cipher.encrypt(payload)).decode("utf-8")


def main():
    session = requests.Session()
    session.post(
        f"{BASE_URL}/api/v1/auth/login",
        json={"email": EMAIL, "password": encrypt_password(PASSWORD)},
        timeout=30,
    ).raise_for_status()
    body = {
        "chat_id": CHAT_ID,
        "messages": [
            {
                "role": "user",
                "content": "知识库原文里写的“家族信托的三项核心价值”是哪三项？请只回答三个词，并保留引用。",
                "id": str(uuid.uuid4()),
            }
        ],
        "quote": True,
        "reasoning": False,
        "internet": False,
        "stream": True,
    }
    final = {}
    with session.post(f"{BASE_URL}/api/v1/chat/completions", json=body, stream=True, timeout=240) as resp:
        resp.raise_for_status()
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            data = raw[5:].strip()
            if not data or data == "[DONE]":
                continue
            obj = json.loads(data)
            payload = obj.get("data")
            if isinstance(payload, dict):
                final = payload
    reference = final.get("reference") or {}
    print(
        json.dumps(
            {
                "answer": final.get("answer"),
                "doc_names": [
                    c.get("document_name") or c.get("docnm_kwd") or c.get("doc_name")
                    for c in reference.get("chunks", [])
                ],
                "chunk_count": len(reference.get("chunks", [])),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
