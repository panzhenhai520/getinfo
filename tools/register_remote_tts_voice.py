#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Register a custom zero-shot voice on the VPN CosyVoice TTS service.

The gateway stores audio artifacts produced by the VPN TTS service.  To make a
new selectable voice (for example a male Mandarin voice), provide a short WAV of
the target speaker plus the exact transcript of that WAV, and this script will:

  1. POST /v1/voices/add to register the voice.
  2. Verify it appears in /v1/voices.
  3. Optionally synthesize a test sentence and save it locally for listening.

Example (male Mandarin reference audio):
  python tools/register_remote_tts_voice.py \
      --voice-id mandarin_male \
      --display-name "普通话男声" \
      --prompt-wav F:\\samples\\male_mandarin_prompt.wav \
      --prompt-text "你好，欢迎使用语音合成系统。" \
      --test-text "这是一条普通话男声测试。"

After registration, in 系统管理 > 文章朗读 set 语言=中文、中文方言=普通话、
性别=男声 and save; the page maps this to mandarin_male.  Equivalently set
REMOTE_PIPELINE_TTS_LANGUAGE=zh, REMOTE_PIPELINE_TTS_DIALECT=mandarin and
REMOTE_PIPELINE_TTS_GENDER=male in .env.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

import requests


def _load_wav(value: str) -> bytes:
    if value.startswith(("http://", "https://")):
        response = requests.get(value, timeout=60)
        response.raise_for_status()
        return response.content
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"prompt WAV not found: {path}")
    return path.read_bytes()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://10.88.0.1:8005")
    parser.add_argument("--voice-id", required=True)
    parser.add_argument("--display-name", default="")
    parser.add_argument("--prompt-wav", required=True)
    parser.add_argument("--prompt-text", required=True)
    parser.add_argument("--test-text", default="")
    parser.add_argument("--output", default="data/tts_voice_tests")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    wav = _load_wav(args.prompt_wav)
    if not wav.startswith(b"RIFF"):
        raise SystemExit("prompt audio is not a WAV file")

    add_url = f"{base_url}/v1/voices/add"
    payload = {
        "voice_id": args.voice_id,
        "display_name": args.display_name or args.voice_id,
        "prompt_text": args.prompt_text,
        "audio_base64": base64.b64encode(wav).decode("ascii"),
    }
    response = requests.post(add_url, json=payload, timeout=120)
    if response.status_code >= 400:
        raise SystemExit(f"voice registration failed ({response.status_code}): {response.text[:400]}")
    print(f"registered voice: {args.voice_id}")

    voices_response = requests.get(f"{base_url}/v1/voices", timeout=30)
    voices_response.raise_for_status()
    voices = voices_response.json().get("voices") or []
    print("available voices:", json.dumps(voices, ensure_ascii=False))

    if args.test_text:
        speech = requests.post(
            f"{base_url}/v1/audio/speech",
            json={"model": "cosyvoice2", "input": args.test_text, "voice": args.voice_id, "response_format": "wav", "speed": 1.0},
            timeout=180,
        )
        if speech.status_code >= 400 or not speech.content.startswith(b"RIFF"):
            raise SystemExit(f"test synthesis failed ({speech.status_code}): {speech.text[:400]}")
        out_dir = Path(args.output).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{args.voice_id}_test.wav"
        out_path.write_bytes(speech.content)
        print(f"test audio saved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
