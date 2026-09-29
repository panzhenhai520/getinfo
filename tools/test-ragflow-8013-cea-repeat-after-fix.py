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
CHAT_ID = "cea3926e4ae611f0aaa71e2cb6df5e69"
PUBLIC_KEY = Path("ragflow_v0_25_6_custom_source/conf/public.pem")


def encrypt_password(password: str) -> str:
    key = RSA.import_key(PUBLIC_KEY.read_text(encoding="utf-8"), passphrase="Welcome")
    cipher = PKCS1_v1_5.new(key)
    payload = base64.b64encode(password.encode("utf-8"))
    return base64.b64encode(cipher.encrypt(payload)).decode("utf-8")


def parse_sse(resp):
    final = {}
    chunks = []
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data:"):
            continue
        data = raw[5:].strip()
        if not data or data == "[DONE]":
            continue
        obj = json.loads(data)
        payload = obj.get("data")
        if isinstance(payload, dict):
            answer = payload.get("answer")
            if isinstance(answer, str):
                chunks.append(answer)
                final = payload
    return final, chunks


def repeated_phrase_score(text: str) -> int:
    phrases = re.findall(r"(.{4,12})\1+", text)
    return len(phrases)


def main():
    session = requests.Session()
    login = session.post(
        f"{BASE_URL}/api/v1/auth/login",
        json={"email": EMAIL, "password": encrypt_password(PASSWORD)},
        timeout=30,
    )
    print("login", login.status_code, login.text[:160])
    login.raise_for_status()
    if login.json().get("code") != 0:
        raise RuntimeError(login.text)

    question = (
        "In the document, what does it say about wealthy families worrying "
        "that a child's marriage partner values wealth rather than character? "
        "Answer in concise Chinese, no more than three points."
    )
    body = {
        "chat_id": CHAT_ID,
        "session_id": "",
        "messages": [{"role": "user", "content": question, "id": str(uuid.uuid4())}],
        "pass_all_history_messages": True,
        "reasoning": False,
        "internet": False,
        "stream": True,
    }
    with session.post(
        f"{BASE_URL}/api/v1/chat/completions",
        json=body,
        stream=True,
        timeout=180,
    ) as resp:
        print("chat_status", resp.status_code)
        if resp.status_code != 200:
            print(resp.text[:1000])
            return 1
        final, chunks = parse_sse(resp)

    answer = (final.get("answer") or "").strip()
    result = {
        "answer": answer,
        "length": len(answer),
        "chunk_count": len(chunks),
        "repeat_score": repeated_phrase_score(answer),
        "contains_known_terms": any(x in answer for x in ["信托", "财产", "人品", "婚"]),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if answer and result["repeat_score"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
