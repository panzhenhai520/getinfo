import base64
import json
from pathlib import Path

import requests
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA
from playwright.sync_api import sync_playwright


BASE_URL = "http://192.168.1.246:8013"
EMAIL = "admin@ragflow.io"
PASSWORD = "admin"
PUBLIC_KEY = Path("ragflow_v0_25_6_custom_source/conf/public.pem")
OUT_DIR = Path(r"C:\tmp\ragflow-8013-check")


def encrypt_password(password: str) -> str:
    key = RSA.import_key(PUBLIC_KEY.read_text(encoding="utf-8"), passphrase="Welcome")
    cipher = PKCS1_v1_5.new(key)
    payload = base64.b64encode(password.encode("utf-8"))
    return base64.b64encode(cipher.encrypt(payload)).decode("utf-8")


def main():
    s = requests.Session()
    r = s.post(
        f"{BASE_URL}/api/v1/auth/login",
        json={"email": EMAIL, "password": encrypt_password(PASSWORD)},
        timeout=30,
    )
    r.raise_for_status()
    if r.json().get("code") != 0:
        raise RuntimeError(r.text)

    cookies = []
    for c in s.cookies:
        cookies.append(
            {
                "name": c.name,
                "value": c.value,
                "domain": "192.168.1.246",
                "path": c.path or "/",
                "httpOnly": bool(c._rest.get("HttpOnly")),
                "secure": False,
                "sameSite": "Lax",
            }
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1366, "height": 768})
        context.add_cookies(cookies)
        page = context.new_page()
        page.goto(f"{BASE_URL}/chats", wait_until="networkidle", timeout=60000)
        page.screenshot(path=str(OUT_DIR / "after-login-cookie-final.png"), full_page=True)
        text = page.locator("body").inner_text(timeout=10000)
        result = {
            "url": page.url,
            "title": page.title(),
            "html_class": page.locator("html").get_attribute("class"),
            "has_memory_nav": "记忆" in text,
            "has_theme_text": any(x in text for x in ["主题", "Theme", "Dark", "Light"]),
            "contains_ragflow_text": "ragflow" in text.lower(),
            "text_sample": " ".join(text.split())[:1200],
        }
        browser.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
