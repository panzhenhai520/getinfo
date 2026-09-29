import json
import re
import sys
import time
import uuid
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


BASE_URL = "http://192.168.1.246:8013"
EMAIL = "admin@ragflow.io"
PASSWORD = "admin"
CHAT_ID = "eb78273e615f11f19380e5c7605e0f71"
OUT_DIR = Path(r"C:\tmp\ragflow-8013-check")


def compact(text: str, limit: int = 900) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit]


def write_json(name: str, data) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / name).write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


CHAT_JS = r"""
async ({ chatId, sessionId, messages, question }) => {
  const id = crypto.randomUUID ? crypto.randomUUID() : String(Date.now());
  const nextMessages = [
    ...messages,
    { role: "user", content: question, id }
  ];
  const resp = await fetch("/api/v1/chat/completions", {
    method: "POST",
    credentials: "include",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      chat_id: chatId,
      session_id: sessionId || "",
      messages: nextMessages,
      pass_all_history_messages: true,
      reasoning: false,
      internet: false,
      stream: true
    })
  });
  const result = {
    status: resp.status,
    ok: resp.ok,
    session_id: sessionId || "",
    events: [],
    final: null,
    raw_tail: ""
  };
  if (!resp.ok || !resp.body) {
    result.raw_tail = await resp.text();
    return result;
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const packets = buffer.split(/\n\n/);
    buffer = packets.pop() || "";
    for (const packet of packets) {
      for (const line of packet.split(/\n/)) {
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (!payload || payload === "[DONE]") continue;
        try {
          const obj = JSON.parse(payload);
          result.events.push(obj);
          if (obj && obj.data && typeof obj.data === "object") {
            if (obj.data.session_id) result.session_id = obj.data.session_id;
            if (obj.data.conversation_id) result.session_id = obj.data.conversation_id;
            if (typeof obj.data.answer === "string") result.final = obj.data;
          }
        } catch (err) {
          result.raw_tail += payload.slice(0, 300);
        }
      }
    }
  }
  result.raw_tail += buffer.slice(-1000);
  if (!result.final) {
    for (let i = result.events.length - 1; i >= 0; i--) {
      const data = result.events[i] && result.events[i].data;
      if (data && typeof data.answer === "string") {
        result.final = data;
        break;
      }
    }
  }
  return result;
}
"""


def chat(page, history, session_id, question):
    payload = {
        "chatId": CHAT_ID,
        "sessionId": session_id,
        "messages": history,
        "question": question,
    }
    result = page.evaluate(CHAT_JS, payload)
    answer = ((result.get("final") or {}).get("answer") or "").strip()
    next_session_id = result.get("session_id") or session_id
    question_msg = {
        "role": "user",
        "content": question,
        "id": str(uuid.uuid4()),
    }
    answer_msg = {
        "role": "assistant",
        "content": answer,
        "id": ((result.get("final") or {}).get("id") or str(uuid.uuid4())),
    }
    next_history = [*history, question_msg, answer_msg]
    return result, next_history, next_session_id


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    started = time.strftime("%Y-%m-%d %H:%M:%S")

    report = {
        "started_at": started,
        "base_url": BASE_URL,
        "chat_id": CHAT_ID,
        "ui": {},
        "api": {},
        "chat_tests": {},
        "notes": [],
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 768})

        page.goto(f"{BASE_URL}/login", wait_until="networkidle", timeout=60000)
        page.screenshot(path=str(OUT_DIR / "login-after-restart.png"), full_page=True)
        login_text = page.locator("body").inner_text(timeout=10000)
        report["ui"]["login"] = {
            "title": page.title(),
            "url": page.url,
            "html_class": page.locator("html").get_attribute("class"),
            "contains_righttime": ("时和" in login_text) or ("時和" in login_text),
            "contains_ragflow_text": bool(re.search(r"ragflow", login_text, re.I)),
            "text_sample": compact(login_text),
        }

        active_form = page.locator('[data-testid="auth-form"][data-active="true"]')
        active_form.get_by_test_id("auth-email").fill(EMAIL)
        active_form.get_by_test_id("auth-password").fill(PASSWORD)
        active_form.get_by_test_id("auth-submit").click()
        try:
            page.wait_for_url(re.compile(r".*/chats.*"), timeout=60000)
        except PlaywrightTimeoutError:
            page.wait_for_load_state("networkidle", timeout=60000)

        page.wait_for_timeout(3000)
        page.screenshot(path=str(OUT_DIR / "after-login-after-restart.png"), full_page=True)
        app_text = page.locator("body").inner_text(timeout=10000)
        report["ui"]["after_login"] = {
            "title": page.title(),
            "url": page.url,
            "html_class": page.locator("html").get_attribute("class"),
            "has_memory_nav": "记忆" in app_text,
            "has_theme_text": any(x in app_text for x in ["主题", "Theme", "Dark", "Light"]),
            "contains_ragflow_text": bool(re.search(r"ragflow", app_text, re.I)),
            "text_sample": compact(app_text, 1500),
        }

        datasets = page.evaluate(
            """async () => {
              const r = await fetch('/api/v1/datasets?page=1&page_size=30', { credentials: 'include' });
              return { status: r.status, json: await r.json() };
            }"""
        )
        chats = page.evaluate(
            """async () => {
              const r = await fetch('/api/v1/chats?page=1&page_size=30', { credentials: 'include' });
              return { status: r.status, json: await r.json() };
            }"""
        )
        report["api"]["datasets"] = datasets
        report["api"]["chats"] = chats

        history = []
        session_id = ""

        q1 = "赵廉慧发表的观点有哪些？请只用要点回答。"
        r1, history, session_id = chat(page, history, session_id, q1)
        a1 = ((r1.get("final") or {}).get("answer") or "").strip()
        report["chat_tests"]["zhao_viewpoints"] = {
            "question": q1,
            "status": r1.get("status"),
            "session_id": session_id,
            "answer": a1,
            "answer_sample": compact(a1, 2000),
            "event_count": len(r1.get("events") or []),
            "has_visible_reasoning_preamble": bool(
                re.search(r"(我现在要分析|首先.*问题|接下来|根据.*问题)", a1)
            ),
            "says_not_learned": any(x in a1 for x in ["没学会", "没有学会", "不知道", "无法回答"]),
        }

        q2 = "这些观点里，哪一点和普通家庭最相关？"
        r2, history, session_id = chat(page, history, session_id, q2)
        a2 = ((r2.get("final") or {}).get("answer") or "").strip()

        q3 = "把第一次回答里的第2点展开说说，不要重新列全部。"
        r3, history, session_id = chat(page, history, session_id, q3)
        a3 = ((r3.get("final") or {}).get("answer") or "").strip()

        report["chat_tests"]["multi_turn"] = {
            "session_id": session_id,
            "questions": [q1, q2, q3],
            "answers": [a1, a2, a3],
            "third_answer_sample": compact(a3, 2000),
            "third_uses_first_reference": bool(
                re.search(r"(第2点|第二点|普通家庭|信托|保护|传承|家庭)", a3)
            )
            and not any(x in a3 for x in ["不知道第一次", "没有上下文", "无法得知第一次"]),
            "event_counts": [
                len(r1.get("events") or []),
                len(r2.get("events") or []),
                len(r3.get("events") or []),
            ],
        }

        browser.close()

    write_json("verify-result-after-restart.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
