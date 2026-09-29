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
PUBLIC_KEY = Path("ragflow_v0_25_6_custom_source/conf/public.pem")
WORK_DIR = Path(r"C:\tmp\ragflow-8013-multilingual-verify")

DOCS = {
    "verify_zh.txt": """多语言验证文章

家族信托的三个核心价值是资产隔离、定向传承和长期照护。
资产隔离可以降低婚姻变化、债务纠纷、继承争议带来的家庭财产风险。
定向传承可以按照委托人的意愿，把资产有计划地分配给指定受益人。
长期照护可以为未成年子女、老人或需要特别照顾的家庭成员提供持续保障。""",
    "verify_en.txt": """Multilingual verification article

The three core values of a family trust are asset isolation, targeted inheritance, and long-term care.
Asset isolation can reduce family wealth risks caused by marital changes, debt disputes, and inheritance conflicts.
Targeted inheritance allows assets to be distributed to designated beneficiaries according to the settlor's wishes.
Long-term care can provide continuous protection for minor children, elderly relatives, or family members who need special support.""",
    "verify_yue.txt": """多語言驗證文章

家族信託嘅三個核心價值係資產隔離、定向傳承同長期照護。
資產隔離可以減低婚姻變化、債務糾紛、繼承爭議帶嚟嘅家庭財產風險。
定向傳承可以按照委託人嘅意願，將資產有計劃咁分配畀指定受益人。
長期照護可以為未成年子女、老人或者需要特別照顧嘅家庭成員提供持續保障。""",
}

QUESTIONS = {
    "zh": "家族信托的三个核心价值是什么？",
    "en": "What are the three core values of a family trust?",
    "yue": "家族信託嘅三個核心價值係咩？",
}
LANGS = ["English", "Chinese", "Cantonese"]


def encrypt_password(password: str) -> str:
    key = RSA.import_key(PUBLIC_KEY.read_text(encoding="utf-8"), passphrase="Welcome")
    cipher = PKCS1_v1_5.new(key)
    payload = base64.b64encode(password.encode("utf-8"))
    return base64.b64encode(cipher.encrypt(payload)).decode("utf-8")


def require_ok(resp: requests.Response, step: str):
    try:
        payload = resp.json()
    except Exception as exc:
        raise RuntimeError(f"{step} failed: HTTP {resp.status_code} non-json {resp.text[:300]}") from exc
    if resp.status_code != 200 or payload.get("code") != 0:
        raise RuntimeError(f"{step} failed: HTTP {resp.status_code} {payload}")
    return payload.get("data")


def login() -> requests.Session:
    session = requests.Session()
    require_ok(
        session.post(
            f"{BASE_URL}/api/v1/auth/login",
            json={"email": EMAIL, "password": encrypt_password(PASSWORD)},
            timeout=30,
        ),
        "login",
    )
    return session


def write_docs():
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, text in DOCS.items():
        path = WORK_DIR / name
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    return paths


def create_dataset(session: requests.Session):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    body = {
        "name": f"codex-multilingual-verify-{stamp}",
        "description": "Temporary UTF-8 Chinese, English, Cantonese verification dataset.",
        "embedding_model": "bge-m3:latest@Ollama",
        "chunk_method": "naive",
        "parser_config": {
            "chunk_token_num": 256,
            "delimiter": "\n\n",
            "layout_recognize": "DeepDOC",
            "html4excel": False,
            "raptor": {"use_raptor": False},
            "graphrag": {"use_graphrag": False},
        },
        "permission": "me",
    }
    data = require_ok(session.post(f"{BASE_URL}/api/v1/datasets", json=body, timeout=30), "create dataset")
    return data["id"], data["name"]


def upload_and_parse(session: requests.Session, dataset_id: str, paths):
    handles = []
    files = []
    try:
        for path in paths:
            handle = path.open("rb")
            handles.append(handle)
            files.append(("file", (path.name, handle, "text/plain; charset=utf-8")))
        docs = require_ok(
            session.post(f"{BASE_URL}/api/v1/datasets/{dataset_id}/documents", files=files, timeout=60),
            "upload documents",
        )
    finally:
        for handle in handles:
            handle.close()
    doc_ids = [doc["id"] for doc in docs]
    require_ok(
        session.post(f"{BASE_URL}/api/v1/datasets/{dataset_id}/documents/parse", json={"document_ids": doc_ids}, timeout=30),
        "parse documents",
    )
    return [{"id": doc["id"], "name": doc["name"]} for doc in docs]


def poll_parse(session: requests.Session, dataset_id: str, timeout_seconds: int = 420):
    deadline = time.time() + timeout_seconds
    last_docs = []
    while time.time() < deadline:
        data = require_ok(
            session.get(f"{BASE_URL}/api/v1/datasets/{dataset_id}/documents?page=1&page_size=30", timeout=30),
            "list documents",
        )
        docs = (data or {}).get("docs") or []
        last_docs = docs
        if docs and all(
            str(doc.get("run", "")).lower() in {"3", "done"}
            and float(doc.get("progress") or 0) >= 1
            and int(doc.get("chunk_num") or doc.get("chunk_count") or 0) > 0
            for doc in docs
        ):
            return docs
        time.sleep(5)
    raise TimeoutError(f"document parsing timeout: {last_docs}")


def search(session: requests.Session, dataset_id: str, question: str, cross_languages=None):
    body = {
        "question": question,
        "doc_ids": [],
        "page": 1,
        "size": 12,
        "top_k": 1024,
        "similarity_threshold": 0.0,
        "vector_similarity_weight": 0.9,
        "keyword": False,
        "cross_languages": cross_languages or [],
    }
    data = require_ok(
        session.post(f"{BASE_URL}/api/v1/datasets/{dataset_id}/search", json=body, timeout=240),
        "search dataset",
    )
    chunks = (data or {}).get("chunks") or []
    return [
        {
            "document_name": chunk.get("document_name") or chunk.get("docnm_kwd") or chunk.get("doc_name"),
            "similarity": chunk.get("similarity"),
            "vector_similarity": chunk.get("vector_similarity"),
            "term_similarity": chunk.get("term_similarity"),
            "content": re.sub(r"\s+", " ", chunk.get("content") or chunk.get("content_with_weight") or "")[:160],
        }
        for chunk in chunks
    ]


def create_chat(session: requests.Session, dataset_id: str):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    body = {
        "name": f"codex-multilingual-verify-chat-{stamp}",
        "dataset_ids": [dataset_id],
        "llm_id": "deepseek-r1:8b@Ollama",
        "llm_setting": {"model_type": "chat", "temperature": 0.05, "top_p": 0.65, "max_tokens": 512},
        "top_n": 12,
        "top_k": 1024,
        "similarity_threshold": 0.0,
        "vector_similarity_weight": 0.9,
        "prompt_config": {
            "system": (
                "你是多语言知识库问答助手。必须只根据【知识库】回答用户问题。"
                "如果中文、英文、粤语文档都有相关内容，请综合回答，并尽量引用不同语言来源。\n\n"
                "【知识库】\n{knowledge}\n【知识库结束】"
            ),
            "parameters": [{"key": "knowledge", "optional": False}],
            "empty_response": "知识库中未找到相关答案。",
            "quote": True,
            "refine_multiturn": True,
            "cross_languages": LANGS,
        },
    }
    data = require_ok(session.post(f"{BASE_URL}/api/v1/chats", json=body, timeout=30), "create chat")
    return data["id"], data["name"]


def ask_chat(session: requests.Session, chat_id: str, question: str):
    body = {
        "chat_id": chat_id,
        "messages": [{"role": "user", "content": question, "id": str(uuid.uuid4())}],
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
            payload = json.loads(data).get("data")
            if isinstance(payload, dict):
                final = payload
    reference = final.get("reference") or {}
    return {
        "answer": final.get("answer"),
        "reference_doc_names": [
            chunk.get("document_name") or chunk.get("docnm_kwd") or chunk.get("doc_name")
            for chunk in reference.get("chunks", [])
        ],
        "reference_chunk_count": len(reference.get("chunks", [])),
    }


def doc_langs(rows):
    names = {row.get("document_name") for row in rows if row.get("document_name")}
    return {
        "zh": any("zh" in name for name in names),
        "en": any("en" in name for name in names),
        "yue": any("yue" in name for name in names),
        "names": sorted(names),
    }


def main():
    session = login()
    dataset_id, dataset_name = create_dataset(session)
    docs = upload_and_parse(session, dataset_id, write_docs())
    parsed_docs = poll_parse(session, dataset_id)

    search_report = {}
    for key, question in QUESTIONS.items():
        rows = search(session, dataset_id, question, cross_languages=LANGS)
        search_report[key] = {"doc_coverage": doc_langs(rows), "top_chunks": rows[:6]}

    chat_id, chat_name = create_chat(session, dataset_id)
    chat_report = ask_chat(
        session,
        chat_id,
        "家族信托的三个核心价值是什么？请用中文回答，并尽量引用中文、英文、粤语三个来源。",
    )

    print(
        json.dumps(
            {
                "dataset": {"id": dataset_id, "name": dataset_name},
                "chat": {"id": chat_id, "name": chat_name},
                "uploaded_docs": docs,
                "parsed_docs": [
                    {
                        "name": doc.get("name"),
                        "run": doc.get("run"),
                        "progress": doc.get("progress"),
                        "chunk_num": doc.get("chunk_num"),
                    }
                    for doc in parsed_docs
                ],
                "search_report": search_report,
                "chat_report": chat_report,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
