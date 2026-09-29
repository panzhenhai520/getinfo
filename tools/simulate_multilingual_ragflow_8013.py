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
WORK_DIR = Path(r"C:\tmp\ragflow-8013-multilingual-sim")
OUT = WORK_DIR / "result.json"


DOCS = {
    "multi_sim_zh.txt": """多语言检索模拟文章

家族信托的三项核心价值是资产隔离、定向传承和长期照护。
资产隔离可以降低婚姻变化、债务纠纷、继承争议带来的家庭财产风险。
定向传承可以按照委托人的意愿，把资产有计划地分配给指定受益人。
长期照护可以为未成年子女、老人或需要特别照顾的家庭成员提供持续保障。
结论：家族信托不是只属于高净值人士，也可以服务普通家庭的财富规划。""",
    "multi_sim_en.txt": """Multilingual retrieval simulation article

The three core values of a family trust are asset isolation, targeted inheritance, and long-term care.
Asset isolation can reduce family wealth risks caused by marital changes, debt disputes, and inheritance conflicts.
Targeted inheritance allows assets to be distributed to designated beneficiaries according to the settlor's wishes.
Long-term care can provide continuous protection for minor children, elderly relatives, or family members who need special support.
Conclusion: A family trust is not only for high-net-worth individuals; it can also serve ordinary families in wealth planning.""",
    "multi_sim_yue.txt": """多語言檢索模擬文章

家族信託嘅三個核心價值係資產隔離、定向傳承同長期照顧。
資產隔離可以減低婚姻變化、債務糾紛、繼承爭議帶嚟嘅家庭財產風險。
定向傳承可以按照委託人嘅意願，將資產有計劃咁分配畀指定受益人。
長期照顧可以為未成年子女、老人或者需要特別照顧嘅家庭成員提供持續保障。
總結：家族信託唔係只屬於高資產人士，亦可以服務普通家庭嘅財富規劃。""",
}


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
    return payload


def parse_sse(resp: requests.Response):
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


def write_docs():
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, content in DOCS.items():
        path = WORK_DIR / name
        path.write_text(content, encoding="utf-8")
        paths.append(path)
    return paths


def login():
    session = requests.Session()
    payload = {"email": EMAIL, "password": encrypt_password(PASSWORD)}
    require_ok(session.post(f"{BASE_URL}/api/v1/auth/login", json=payload, timeout=30), "login")
    return session


def create_dataset(session: requests.Session):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    body = {
        "name": f"多语言检索模拟-{stamp}",
        "description": "Codex 临时模拟：中文、英文、粤语同内容文档，用于验证跨语言向量检索。",
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
    payload = require_ok(session.post(f"{BASE_URL}/api/v1/datasets", json=body, timeout=30), "create dataset")
    data = payload["data"]
    return data["id"], data.get("name", body["name"])


def upload_documents(session: requests.Session, dataset_id: str, paths):
    files = []
    handles = []
    try:
        for path in paths:
            fh = path.open("rb")
            handles.append(fh)
            files.append(("file", (path.name, fh, "text/plain; charset=utf-8")))
        payload = require_ok(
            session.post(f"{BASE_URL}/api/v1/datasets/{dataset_id}/documents", files=files, timeout=60),
            "upload documents",
        )
        docs = payload["data"]
        return [{"id": doc["id"], "name": doc["name"]} for doc in docs]
    finally:
        for fh in handles:
            fh.close()


def parse_documents(session: requests.Session, dataset_id: str, document_ids):
    require_ok(
        session.post(f"{BASE_URL}/api/v1/datasets/{dataset_id}/documents/parse", json={"document_ids": document_ids}, timeout=30),
        "parse documents",
    )


def poll_documents(session: requests.Session, dataset_id: str, timeout_seconds: int = 300):
    deadline = time.time() + timeout_seconds
    last_docs = []
    while time.time() < deadline:
        payload = require_ok(
            session.get(f"{BASE_URL}/api/v1/datasets/{dataset_id}/documents?page=1&page_size=30", timeout=30),
            "list documents",
        )
        data = payload.get("data") or {}
        if isinstance(data, dict):
            docs = data.get("docs") or data.get("documents") or data.get("items") or []
        elif isinstance(data, list):
            docs = data
        else:
            docs = []
        last_docs = docs
        if docs and all(
            str(doc.get("run", "")).lower() in {"3", "done"}
            and float(doc.get("progress") or 0) >= 1
            and int(doc.get("chunk_num") or doc.get("chunk_count") or 0) > 0
            for doc in docs
        ):
            return docs
        time.sleep(5)
    raise TimeoutError(f"document parsing timeout, last={last_docs}")


def search_dataset(session: requests.Session, dataset_id: str, question: str, doc_ids=None):
    body = {
        "question": question,
        "doc_ids": doc_ids or [],
        "page": 1,
        "size": 12,
        "top_k": 1024,
        "similarity_threshold": 0.0,
        "vector_similarity_weight": 0.9,
        "keyword": False,
        "cross_languages": [],
    }
    payload = require_ok(session.post(f"{BASE_URL}/api/v1/datasets/{dataset_id}/search", json=body, timeout=120), "search dataset")
    data = payload.get("data") or {}
    chunks = data.get("chunks") or []
    return [
        {
            "document_name": c.get("document_name") or c.get("docnm_kwd") or c.get("doc_name"),
            "content": re.sub(r"\s+", " ", c.get("content") or c.get("content_with_weight") or "")[:260],
            "similarity": c.get("similarity"),
            "vector_similarity": c.get("vector_similarity"),
            "term_similarity": c.get("term_similarity"),
        }
        for c in chunks
    ]


def create_chat(session: requests.Session, dataset_id: str):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    body = {
        "name": f"多语言模拟问答-{stamp}",
        "dataset_ids": [dataset_id],
        "llm_id": "deepseek-r1:1.5b@Ollama",
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
            "cross_languages": [],
        },
    }
    payload = require_ok(session.post(f"{BASE_URL}/api/v1/chats", json=body, timeout=30), "create chat")
    return payload["data"]["id"], payload["data"].get("name", body["name"])


def ask_chat(session: requests.Session, chat_id: str, question: str):
    body = {
        "chat_id": chat_id,
        "messages": [{"role": "user", "content": question, "id": str(uuid.uuid4())}],
        "quote": True,
        "reasoning": False,
        "internet": False,
        "stream": True,
    }
    with session.post(f"{BASE_URL}/api/v1/chat/completions", json=body, stream=True, timeout=240) as resp:
        resp.raise_for_status()
        final = parse_sse(resp)
    answer = final.get("answer") or ""
    reference = final.get("reference") or {}
    doc_names = []
    for chunk in reference.get("chunks") or []:
        name = chunk.get("document_name") or chunk.get("docnm_kwd") or chunk.get("doc_name")
        if name and name not in doc_names:
            doc_names.append(name)
    return {"answer": answer, "reference_doc_names": doc_names, "reference_chunk_count": len(reference.get("chunks") or [])}


def main():
    paths = write_docs()
    session = login()
    dataset_id, dataset_name = create_dataset(session)
    uploaded_docs = upload_documents(session, dataset_id, paths)
    document_ids = [doc["id"] for doc in uploaded_docs]
    parse_documents(session, dataset_id, document_ids)
    parsed_docs = poll_documents(session, dataset_id)

    question = "家族信托的三项核心价值是什么？请用中文回答，并说明是否能从不同语言文档里找到。"
    all_search = search_dataset(session, dataset_id, question)
    english_yue_ids = [doc["id"] for doc in uploaded_docs if not doc["name"].endswith("_zh.txt")]
    english_yue_search = search_dataset(session, dataset_id, question, doc_ids=english_yue_ids)

    chat_id, chat_name = create_chat(session, dataset_id)
    chat_result = ask_chat(session, chat_id, question)

    report = {
        "dataset": {"id": dataset_id, "name": dataset_name},
        "documents": uploaded_docs,
        "parsed_documents": [
            {
                "id": doc.get("id"),
                "name": doc.get("name"),
                "run": doc.get("run"),
                "progress": doc.get("progress"),
                "chunk_num": doc.get("chunk_num") or doc.get("chunk_count"),
            }
            for doc in parsed_docs
        ],
        "retrieval_all_docs": all_search,
        "retrieval_english_yue_only": english_yue_search,
        "chat": {"id": chat_id, "name": chat_name, **chat_result},
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
