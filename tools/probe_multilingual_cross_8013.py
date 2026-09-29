import argparse
import base64
import json
import re
from pathlib import Path

import requests
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA


BASE_URL = "http://192.168.1.246:8013"
EMAIL = "admin@ragflow.io"
PASSWORD = "admin"
PUBLIC_KEY = Path("ragflow_v0_25_6_custom_source/conf/public.pem")

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
    payload = resp.json()
    if resp.status_code != 200 or payload.get("code") != 0:
        raise RuntimeError(f"{step} failed: HTTP {resp.status_code} {payload}")
    return payload.get("data") or {}


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
        session.post(f"{BASE_URL}/api/v1/datasets/{dataset_id}/search", json=body, timeout=300),
        "search dataset",
    )
    chunks = data.get("chunks") or []
    rows = [
        {
            "document_name": chunk.get("document_name") or chunk.get("docnm_kwd") or chunk.get("doc_name"),
            "similarity": chunk.get("similarity"),
            "vector_similarity": chunk.get("vector_similarity"),
            "term_similarity": chunk.get("term_similarity"),
            "content": re.sub(r"\s+", " ", chunk.get("content") or chunk.get("content_with_weight") or "")[:120],
        }
        for chunk in chunks
    ]
    names = {row["document_name"] for row in rows if row.get("document_name")}
    return {
        "coverage": {
            "zh": any("zh" in name for name in names),
            "en": any("en" in name for name in names),
            "yue": any("yue" in name for name in names),
            "names": sorted(names),
        },
        "top_chunks": rows[:6],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset_id")
    args = parser.parse_args()
    session = login()
    report = {}
    for key, question in QUESTIONS.items():
        report[key] = {
            "no_cross": search(session, args.dataset_id, question),
            "with_cross": search(session, args.dataset_id, question, LANGS),
        }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
