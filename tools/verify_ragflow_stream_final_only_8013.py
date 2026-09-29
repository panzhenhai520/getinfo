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
CHAT_ID = "eb78273e615f11f19380e5c7605e0f71"
PUBLIC_KEY = Path("ragflow_v0_25_6_custom_source/conf/public.pem")
QUESTION = "\u8d75\u5ec9\u6167\u53d1\u8868\u7684\u89c2\u70b9\u6709\u54ea\u4e9b\uff1f\u8bf7\u53ea\u7528\u8981\u70b9\u56de\u7b54\u3002"


def encrypt_password(password: str) -> str:
    key = RSA.import_key(PUBLIC_KEY.read_text(encoding="utf-8"), passphrase="Welcome")
    cipher = PKCS1_v1_5.new(key)
    payload = base64.b64encode(password.encode("utf-8"))
    return base64.b64encode(cipher.encrypt(payload)).decode("utf-8")


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
        "messages": [{"role": "user", "content": QUESTION, "id": str(uuid.uuid4())}],
        "quote": True,
        "reasoning": False,
        "internet": False,
        "stream": True,
    }

    non_final_text_events = []
    final = {}
    event_count = 0
    with session.post(f"{BASE_URL}/api/v1/chat/completions", json=body, stream=True, timeout=240) as resp:
        resp.raise_for_status()
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            data = raw[5:].strip()
            if not data or data == "[DONE]":
                continue
            event_count += 1
            payload = json.loads(data).get("data")
            if not isinstance(payload, dict):
                continue
            if payload.get("final") is True:
                final = payload
            elif payload.get("answer"):
                non_final_text_events.append(payload.get("answer"))

    report = {
        "event_count": event_count,
        "non_final_text_event_count": len(non_final_text_events),
        "first_non_final_text": non_final_text_events[0] if non_final_text_events else "",
        "final_answer_preview": (final.get("answer") or "")[:300],
        "final_has_reference": bool((final.get("reference") or {}).get("chunks")),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
