import base64
import json
import re
import time
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
OUT = Path(r"C:\tmp\ragflow-8013-check\verify-api-result.json")


def encrypt_password(password: str) -> str:
    key = RSA.import_key(PUBLIC_KEY.read_text(encoding="utf-8"), passphrase="Welcome")
    cipher = PKCS1_v1_5.new(key)
    payload = base64.b64encode(password.encode("utf-8"))
    return base64.b64encode(cipher.encrypt(payload)).decode("utf-8")


def parse_sse(resp):
    events = []
    final = None
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data:"):
            continue
        data = raw[5:].strip()
        if not data or data == "[DONE]":
            continue
        obj = json.loads(data)
        events.append(obj)
        payload = obj.get("data")
        if isinstance(payload, dict) and isinstance(payload.get("answer"), str):
            final = payload
    return events, final or {}


def send_chat(session, history, session_id, question):
    msg = {"role": "user", "content": question, "id": str(uuid.uuid4())}
    body = {
        "chat_id": CHAT_ID,
        "session_id": session_id,
        "messages": [*history, msg],
        "pass_all_history_messages": True,
        "reasoning": False,
        "internet": False,
        "stream": True,
    }
    with session.post(f"{BASE_URL}/api/v1/chat/completions", json=body, stream=True, timeout=150) as resp:
        status = resp.status_code
        events, final = parse_sse(resp) if status == 200 else ([], {"answer": resp.text})
    answer = (final.get("answer") or "").strip()
    next_session_id = final.get("session_id") or final.get("conversation_id") or session_id
    history = [*history, msg, {"role": "assistant", "content": answer, "id": final.get("id") or str(uuid.uuid4())}]
    return {
        "status": status,
        "session_id": next_session_id,
        "answer": answer,
        "event_count": len(events),
    }, history, next_session_id


def main():
    s = requests.Session()
    login = s.post(
        f"{BASE_URL}/api/v1/auth/login",
        json={"email": EMAIL, "password": encrypt_password(PASSWORD)},
        timeout=30,
    )
    login_json = login.json()
    if login.status_code != 200 or login_json.get("code") != 0:
        raise RuntimeError(f"login failed: {login.status_code} {login.text}")

    chats = s.get(f"{BASE_URL}/api/v1/chats?page=1&page_size=30", timeout=30).json()
    datasets = s.get(f"{BASE_URL}/api/v1/datasets?page=1&page_size=30", timeout=30).json()

    history = []
    session_id = ""
    q1 = "赵廉慧发表的观点有哪些？请只用要点回答。"
    r1, history, session_id = send_chat(s, history, session_id, q1)
    q2 = "这些观点里，哪一点和普通家庭最相关？"
    r2, history, session_id = send_chat(s, history, session_id, q2)
    q3 = "把第一次回答里的第2点展开说说，不要重新列全部。"
    r3, history, session_id = send_chat(s, history, session_id, q3)

    report = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "login": {"status": login.status_code, "code": login_json.get("code")},
        "datasets_total": datasets.get("total_datasets"),
        "chats_total": (chats.get("data") or {}).get("total"),
        "results": [r1, r2, r3],
        "checks": {
            "q1_has_viewpoints": bool(re.search(r"1[.、].*2[.、]", r1["answer"], re.S)),
            "q1_no_refusal": not any(x in r1["answer"] for x in ["没学会", "不知道", "无法回答"]),
            "q2_selects_ordinary_family": "普通家庭" in r2["answer"] or "第5点" in r2["answer"],
            "q3_uses_first_second_point": "第2点" in r3["answer"] or "长期稳定性" in r3["answer"] or "灵活保护性" in r3["answer"],
            "no_visible_reasoning": not any(
                x in "\n".join(r["answer"] for r in [r1, r2, r3])
                for x in ["我现在需要分析", "首先，", "接下来", "回顾一下"]
            ),
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
