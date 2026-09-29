#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Small server-side adapter for RAGFlow's independent text-to-speech API."""

from __future__ import annotations

import hashlib
import os
import re
import time
import wave
from io import BytesIO
from pathlib import Path
from typing import Tuple

import requests

import config
from intel_llm_client import IntelLLMError


class IntelTTSError(IntelLLMError):
    pass


class IntelTTSService:
    def __init__(self) -> None:
        self.cache_dir = Path('data/intel_tts_cache')
        self._last_prune = 0.0

    @property
    def configured(self) -> bool:
        base_url = str(getattr(config, 'INTEL_TTS_BASE_URL', '') or '')
        api_key = str(getattr(config, 'INTEL_TTS_API_KEY', '') or '')
        protocol = str(getattr(config, 'INTEL_TTS_PROTOCOL', 'ragflow') or 'ragflow').casefold()
        # TTS 总闸：引擎更换改进完成前 SYSTEM_TTS_ENABLED=False，本机朗读合成一律不工作
        return bool(
            getattr(config, 'SYSTEM_TTS_ENABLED', False)
            and config.RAGFLOW_TTS_ENABLED
            and base_url
            and (api_key or protocol == 'openai')
        )

    def _headers(self) -> dict:
        api_key = str(getattr(config, 'INTEL_TTS_API_KEY', '') or '')
        headers = {'Content-Type': 'application/json'}
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'
        return headers

    @staticmethod
    def _effective_language(language: str) -> str:
        mode = getattr(config, 'RAGFLOW_TTS_LANGUAGE_MODE', 'auto')
        return mode if mode in {'zh', 'en'} else language

    def _voice_id(self, language: str, voice: str = '', dialect: str = '', gender: str = '') -> str:
        actual_language = self._effective_language(language)
        # 多租户：行业包配置了显式 voice / 口音 / 性别则优先覆盖
        if voice:
            return str(voice).strip()
        eff_dialect = str(dialect or config.INTEL_TTS_DIALECT).casefold()
        eff_gender = str(gender or config.INTEL_TTS_GENDER).casefold()
        explicit = str(
            getattr(config, 'RAGFLOW_TTS_VOICE_ZH', '')
            if actual_language == 'zh'
            else getattr(config, 'RAGFLOW_TTS_VOICE_EN', '')
            or ''
        ).strip()
        if explicit:
            return explicit
        if actual_language == 'en':
            return 'english_male' if eff_gender == 'male' else 'english_female'
        if eff_dialect == 'cantonese':
            return 'cantonese_male' if eff_gender == 'male' else 'cantonese_female'
        return 'mandarin_male' if eff_gender == 'male' else 'default'

    def _settings_fingerprint(self, language: str, voice: str = '', dialect: str = '', gender: str = '') -> str:
        actual_language = self._effective_language(language)
        voice_zh = voice if voice else str(getattr(config, 'RAGFLOW_TTS_VOICE_ZH', ''))
        if actual_language != 'zh':
            voice_zh = voice if voice else str(getattr(config, 'RAGFLOW_TTS_VOICE_EN', ''))
        protocol = str(getattr(config, 'INTEL_TTS_PROTOCOL', 'ragflow') or 'ragflow').casefold()
        return f"protocol={protocol}|engine={config.INTEL_TTS_ENGINE}|profile={config.INTEL_TTS_VOICE_PROFILE}|dialect={dialect or config.INTEL_TTS_DIALECT}|gender={gender or config.INTEL_TTS_GENDER}|emotion={config.INTEL_TTS_EMOTION}|speed={config.INTEL_TTS_SPEED}|model={getattr(config, 'RAGFLOW_TTS_MODEL', '')}|voice={voice_zh}|language={actual_language}"

    def _cache_path(self, text: str, language: str, kind: str = 'full', voice: str = '', dialect: str = '', gender: str = '') -> Path:
        # v5 invalidates audio produced by the old /speech/sync endpoint,
        # which silently ignored per-request CosyVoice voice profiles.
        digest = hashlib.sha256(f'v5|{kind}|{self._settings_fingerprint(language, voice, dialect, gender)}|{text}'.encode('utf-8')).hexdigest()
        return self.cache_dir / f'{digest}.wav'

    @staticmethod
    def _is_cached(path: Path) -> bool:
        try:
            if path.is_file() and path.stat().st_size > 44:
                # Access time is often mounted with noatime; mtime becomes the
                # stable LRU timestamp for bounded cache eviction instead.
                os.utime(path, None)
                return True
        except OSError:
            pass
        return False

    def prune_cache(self, force: bool = False) -> dict:
        """Evict expired audio and then least-recently-used files by size."""
        now = time.time()
        if not force and now - self._last_prune < 3600:
            return {'removed': 0, 'bytes': 0}
        self._last_prune = now
        if not self.cache_dir.exists():
            return {'removed': 0, 'bytes': 0}
        expiry = now - max(1, int(config.RAGFLOW_TTS_CACHE_DAYS)) * 86400
        entries = []
        removed = 0
        for path in self.cache_dir.glob('*.wav'):
            try:
                stat = path.stat()
                if stat.st_mtime < expiry:
                    path.unlink()
                    removed += 1
                else:
                    entries.append((stat.st_mtime, stat.st_size, path))
            except OSError:
                continue
        limit = max(64, int(config.RAGFLOW_TTS_CACHE_MAX_MB)) * 1024 * 1024
        total = sum(size for _, size, _ in entries)
        for _, size, path in sorted(entries, key=lambda item: item[0]):
            if total <= limit:
                break
            try:
                path.unlink()
                total -= size
                removed += 1
            except OSError:
                continue
        return {'removed': removed, 'bytes': max(total, 0)}

    @staticmethod
    def _split_text(text: str, max_chars: int) -> list[str]:
        """Keep natural sentence/phrase boundaries while enforcing a TTS limit."""
        parts, remaining = [], str(text or '').strip()
        while len(remaining) > max_chars:
            candidate = remaining[:max_chars]
            # First prefer terminal punctuation; then use a natural phrase
            # boundary instead of cutting in the middle of Chinese text.
            boundaries = [item.end() for item in re.finditer(r'[。！？!?；;](?:[”’"\')）】]*\s*)|\n+', candidate)]
            if not boundaries:
                boundaries = [item.end() for item in re.finditer(r'[，、,:：](?:\s*)', candidate)]
            cut = next((point for point in reversed(boundaries) if point >= int(max_chars * 0.55)), max_chars)
            parts.append(remaining[:cut].strip())
            remaining = remaining[cut:].lstrip()
        if remaining:
            parts.append(remaining)
        return parts

    @classmethod
    def playback_fragments(cls, text: str, max_chars: int | None = None) -> list[str]:
        """Return independently synthesizable units for progressive playback.

        Chinese uses the configured character ceiling; English uses a word
        ceiling. Both preserve sentence boundaries whenever possible.
        """
        normalized = str(text or '').strip()
        if not normalized:
            return []
        is_chinese = any('\u4e00' <= char <= '\u9fff' for char in normalized)
        if is_chinese:
            limit = max_chars or config.INTEL_TTS_SEGMENT_MAX_CHARS_ZH
        else:
            word_limit = max(1, int(getattr(config, 'INTEL_TTS_SEGMENT_MAX_WORDS_EN', 20)))
            words = re.findall(r'\S+', normalized)
            if len(words) <= word_limit:
                return [normalized]
            result, current = [], []
            for word in words:
                current.append(word)
                if len(current) >= word_limit and re.search(r'[.!?;][”’"\')\]]*$', word):
                    result.append(' '.join(current)); current = []
                elif len(current) >= word_limit + max(3, word_limit // 4):
                    result.append(' '.join(current)); current = []
            if current:
                result.append(' '.join(current))
            return result
        sentences = [item.strip() for item in re.findall(r'.+?(?:(?:[。！？!?；;]|(?<!\d)\.)(?:[”’"\')）】]*)|$)', normalized, re.S) if item.strip()]
        result: list[str] = []
        for sentence in sentences or [normalized]:
            result.extend(cls._split_text(sentence, limit))
        return result

    def _synthesize_one(self, text: str, language: str, voice: str = '', dialect: str = '', gender: str = '') -> Tuple[Path, bool]:
        output = self._cache_path(text, language, 'part', voice, dialect, gender)
        if self._is_cached(output):
            return output, True
        base_url = str(getattr(config, 'INTEL_TTS_BASE_URL', '') or '').rstrip('/')
        protocol = str(getattr(config, 'INTEL_TTS_PROTOCOL', 'ragflow') or 'ragflow').casefold()
        eff_dialect = str(dialect or config.INTEL_TTS_DIALECT)
        eff_gender = str(gender or config.INTEL_TTS_GENDER)
        if protocol == 'openai':
            # CosyVoice's OpenAI-compatible endpoint. It accepts concrete voice
            # ids such as mandarin_male/cantonese_female/english_female.
            base = base_url + '/v1/audio/speech'
            payload = {
                'model': str(getattr(config, 'RAGFLOW_TTS_MODEL', '') or 'cosyvoice2'),
                'input': text,
                'voice': self._voice_id(language, voice, eff_dialect, eff_gender),
                'response_format': 'wav',
                'speed': float(config.INTEL_TTS_SPEED),
            }
        else:
            # This is the endpoint used by Panython's own CosyVoice preview.
            # In contrast to the older sync-job endpoint it applies tts_config.
            base = base_url + '/api/v1/chat/audio/speech'
            payload = {'text': text}
            # Exact Panython CosyVoice request envelope. It never changes
            # platform-wide /dev/tts-engine-settings values unless per-pack override.
            payload['tts_config'] = {
                'voice_profile': config.INTEL_TTS_VOICE_PROFILE,
                'dialect': eff_dialect,
                'gender': eff_gender,
                'emotion': config.INTEL_TTS_EMOTION,
                'speed': config.INTEL_TTS_SPEED,
            }
        try:
            response = requests.post(base, headers=self._headers(), json=payload, timeout=config.RAGFLOW_TTS_TIMEOUT)
        except requests.RequestException as exc:
            raise IntelTTSError('朗读服务连接失败') from exc
        if response.status_code >= 400 or not response.content.startswith(b'RIFF'):
            message = '未能生成朗读音频'
            try:
                error_data = response.json()
                if isinstance(error_data, dict):
                    message = str(error_data.get('message') or message)
            except ValueError:
                pass
            raise IntelTTSError(message)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix('.tmp')
        temporary.write_bytes(response.content)
        temporary.replace(output)
        return output, False

    @staticmethod
    def _combine_wav(parts: list[Path], output: Path) -> None:
        """Join same-format RAGFlow WAV fragments with no browser-side gap."""
        try:
            with wave.open(str(parts[0]), 'rb') as first:
                params = (first.getnchannels(), first.getsampwidth(), first.getframerate(), first.getcomptype())
                frames = [first.readframes(first.getnframes())]
            for path in parts[1:]:
                with wave.open(str(path), 'rb') as item:
                    current = (item.getnchannels(), item.getsampwidth(), item.getframerate(), item.getcomptype())
                    if current != params:
                        raise IntelTTSError('朗读片段音频格式不一致')
                    frames.append(item.readframes(item.getnframes()))
            temporary = output.with_suffix('.tmp')
            with wave.open(str(temporary), 'wb') as writer:
                writer.setnchannels(params[0]); writer.setsampwidth(params[1]); writer.setframerate(params[2]); writer.setcomptype(params[3], 'not compressed')
                for frame in frames:
                    writer.writeframes(frame)
            temporary.replace(output)
        except (wave.Error, OSError) as exc:
            raise IntelTTSError('无法合并朗读音频') from exc

    @staticmethod
    def _combine_wav_bytes(parts: list[bytes]) -> bytes:
        """Join all chunks RAGFlow returns for one sentence without a gap."""
        if len(parts) == 1:
            return parts[0]
        try:
            frames = []
            with wave.open(BytesIO(parts[0]), 'rb') as first:
                params = (first.getnchannels(), first.getsampwidth(), first.getframerate(), first.getcomptype())
                frames.append(first.readframes(first.getnframes()))
            for raw in parts[1:]:
                with wave.open(BytesIO(raw), 'rb') as item:
                    current = (item.getnchannels(), item.getsampwidth(), item.getframerate(), item.getcomptype())
                    if current != params:
                        raise IntelTTSError('朗读片段音频格式不一致')
                    frames.append(item.readframes(item.getnframes()))
            output = BytesIO()
            with wave.open(output, 'wb') as writer:
                writer.setnchannels(params[0]); writer.setsampwidth(params[1]); writer.setframerate(params[2]); writer.setcomptype(params[3], 'not compressed')
                for frame in frames:
                    writer.writeframes(frame)
            return output.getvalue()
        except wave.Error as exc:
            raise IntelTTSError('无法合并朗读音频') from exc

    def synthesize(self, text: str, language: str = 'auto', voice: str = '', dialect: str = '', gender: str = '') -> Tuple[Path, bool]:
        text = str(text or '').strip()
        if not self.configured:
            raise IntelTTSError('RAGFlow 朗读服务尚未配置')
        if not text:
            raise IntelTTSError('没有可朗读的文本')
        output = self._cache_path(text, language, 'full', voice, dialect, gender)
        self.prune_cache()
        if self._is_cached(output):
            return output, True
        # RAGFlow's limit varies by configured voice. Use conservative,
        # sentence-aligned fragments and merge them server-side into one WAV.
        fragments = self._split_text(text, config.RAGFLOW_TTS_MAX_CHARS)
        paths = [self._synthesize_one(fragment, language, voice, dialect, gender)[0] for fragment in fragments]
        if len(paths) == 1:
            return paths[0], False
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._combine_wav(paths, output)
        return output, False


intel_tts_service = IntelTTSService()
