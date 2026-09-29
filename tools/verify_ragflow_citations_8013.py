import base64
import json
import re
import uuid
from pathlib import Path

import requests
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA


BASE_URL = "http://192.168.1.246:8013"
EMAIL = "admin@ragflow.io"
PASSWORD = "admin"
CHAT_ID = "eb78273e615f11f19380e5c7605e0f71"
PUBLIC_KEY = Path("ragflow_v0_25_6_custom_source/conf/public.pem")


def encrypt_password(password: str) -> str:
    key = RSA.import_key(PUBLIC_KEY.read_text(encoding="utf-8"), passphrase="Welcome")
    cipher = PKCS1_v1_5.new(key)
    payload = base64.b64encode(password.encode("utf-8"))
    return base64.b64encode(cipher.encrypt(payload)).decode("utf-8")


def parse_sse(resp):
    final = None
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
    return final or {}


def main():
    session = requests.Session()
    login = session.post(
        f"{BASE_URL}/api/v1/auth/login",
        json={"email": EMAIL, "password": encrypt_password(PASSWORD)},
        timeout=30,
    )
    login.raise_for_status()
    body = {
        "chat_id": CHAT_ID,
        "messages": [
            {
                "role": "user",
                "content": "赵廉慧发表的观点有哪些？请只用要点回答。",
                "id": str(uuid.uuid4()),
            }
        ],
        "quote": True,
        "reasoning": False,
        "internet": False,
        "stream": True,
    }
    with session.post(f"{BASE_URL}/api/v1/chat/completions", json=body, stream=True, timeout=180) as resp:
        resp.raise_for_status()
        final = parse_sse(resp)
    answer = final.get("answer") or ""
    reference = final.get("reference") or {}
    report = {
        "answer": answer,
        "has_citation_markers": bool(re.search(r"\[ID:\d+\]|\[\d+\]", answer)),
        "marker_count": len(re.findall(r"\[ID:\d+\]|\[\d+\]", answer)),
        "reference_chunk_count": len(reference.get("chunks") or []),
        "reference_doc_count": len(reference.get("doc_aggs") or []),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
