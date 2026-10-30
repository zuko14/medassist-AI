"""Kriya Ketu voice input: one speech-to-text call per utterance (ADR-003): Sarvam REST, or an
audio-capable OpenRouter model when ASSISTANT_STT_PROVIDER=openrouter.

The browser records <= 30 s with MediaRecorder; the router hands the bytes here and drops them
after the call. Nothing is stored and the transcript is never logged (openrouter-and-speech-spec
B.5). Every failure is an SttError with a fixed code, never the provider's text.
"""

import asyncio
import base64
import math
import re
from dataclasses import dataclass
from typing import Optional

import httpx

from app.config import settings

SARVAM_URL = "https://api.sarvam.ai/speech-to-text"
MAX_BYTES = 1024 * 1024
MAX_MS = 30_000
MAX_TEXT = 1000  # the composer limit
# Widget language menu -> (Sarvam language_code, mode). "mode" is saaras:v3 only; DP-03 picks the
# default (transcribe vs codemix) after G-KK-ASR.
LANGUAGES = {
    "auto": ("unknown", "transcribe"), "te": ("te-IN", "transcribe"), "hi": ("hi-IN", "transcribe"),
    "en": ("en-IN", "transcribe"), "mixed": ("unknown", "codemix"),
}
# MediaRecorder output: WebM (EBML magic) from Chrome/Edge/Firefox, MP4 ("ftyp" box) from Safari; or the
# 16 kHz mono WAV the widget re-encodes it to (RIFF....WAVE), which every provider accepts.
_EXT = {"audio/webm": "webm", "audio/mp4": "mp4", "audio/wav": "wav"}
# OpenRouter input_audio formats (no WebM container): the widget sends WAV; Safari's MP4 is AAC (m4a).
_OPENROUTER_FORMAT = {"audio/wav": "wav", "audio/mp4": "m4a"}
_OPENROUTER_PROMPT = (
    "Transcribe this hospital staff member's spoken instruction exactly as said. Speech may be English, "
    "Telugu, Hindi or a mix. Write English words in English, Telugu in Telugu script, Hindi in Devanagari. "
    "Keep names, numbers, times, dates and amounts exactly. The room may be noisy (a hospital ward or front "
    "desk): transcribe only the main speaker closest to the microphone and ignore background voices, "
    "announcements, phones, monitor beeps and music. Do not guess words you cannot hear. Do not translate, "
    "answer, summarise or add anything. Output only the transcript; output nothing if there is no clear speech.")
# What models write instead of words for noise or silence: "[inaudible]", "(background noise)", "No speech."
_NOISE_TAG = re.compile(r"\[[^\]]{0,40}\]|\((?:background|inaudible|noise|silence|music|unclear)[^)]{0,30}\)", re.I)
_NO_SPEECH = re.compile(r"^(?:no (?:clear )?speech(?: detected)?|silence|inaudible|unintelligible|\.+|-+)$", re.I)


def clean_transcript(text: str) -> str:
    t = _NOISE_TAG.sub(" ", text).strip().strip('"\'“”').strip()
    t = re.sub(r"\s+", " ", t)
    return "" if _NO_SPEECH.match(t.rstrip(".")) or _NO_SPEECH.match(t) else t


class SttError(Exception):
    def __init__(self, code: str):  # timeout | failed
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Heard:
    text: str
    language: Optional[str]


def audio_mime(content_type: Optional[str]) -> Optional[str]:
    """The base mime if it is one we accept, else None ("audio/webm;codecs=opus" -> "audio/webm")."""
    base = (content_type or "").split(";")[0].strip().lower()
    return base if base in _EXT else None


def provider_name() -> str:
    return "openrouter" if settings.assistant_stt_provider == "openrouter" else "sarvam"


def configured() -> bool:
    return settings.assistant_stt_configured


def model_name() -> str:
    return settings.assistant_stt_openrouter_model if provider_name() == "openrouter" else settings.assistant_stt_model


def looks_like(mime: str, audio: bytes) -> bool:
    if mime == "audio/wav":
        return audio[:4] == b"RIFF" and audio[8:12] == b"WAVE"
    if mime == "audio/webm":
        return audio[:4] == b"\x1a\x45\xdf\xa3"
    return audio[4:8] == b"ftyp"


def cost_paise(duration_ms: int, paise_per_minute: int) -> int:
    return math.ceil(max(0, duration_ms) / 60_000 * max(0, paise_per_minute))


class SarvamRest:
    def __init__(self, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.transport = transport  # tests pass an httpx.MockTransport

    async def _post(self, audio: bytes, mime: str, language_code: str, mode: str) -> httpx.Response:
        async with httpx.AsyncClient(transport=self.transport) as c:
            return await c.post(
                SARVAM_URL,
                headers={"api-subscription-key": settings.sarvam_api_key},
                data={"model": settings.assistant_stt_model, "language_code": language_code, "mode": mode},
                files={"file": (f"speech.{_EXT[mime]}", audio, mime)},  # in memory, never a file
            )

    async def transcribe(self, audio: bytes, mime: str, language_code: str, mode: str) -> Heard:
        try:
            # One total deadline (httpx timeouts are per phase); no retry: the user just presses the mic again.
            r = await asyncio.wait_for(self._post(audio, mime, language_code, mode),
                                       settings.assistant_stt_timeout_seconds)
        except (asyncio.TimeoutError, httpx.TimeoutException):
            raise SttError("timeout")
        except httpx.HTTPError:
            raise SttError("failed")
        if r.status_code != 200:
            raise SttError("failed")
        try:
            body = r.json()
            text = body["transcript"]
        except (ValueError, KeyError, TypeError):
            raise SttError("failed")
        if not isinstance(text, str):
            raise SttError("failed")
        language = body.get("language_code")
        return Heard(clean_transcript(text)[:MAX_TEXT], language if isinstance(language, str) else None)


class OpenRouterAudio:
    """One chat completion with the clip as input_audio; temperature 0, the model only transcribes."""

    def __init__(self, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.transport = transport

    async def _post(self, payload: dict) -> httpx.Response:
        async with httpx.AsyncClient(transport=self.transport) as c:
            return await c.post(settings.openrouter_base_url, json=payload,
                                headers={"Authorization": f"Bearer {settings.openrouter_api_key}"})

    async def transcribe(self, audio: bytes, mime: str, language_code: str, mode: str) -> Heard:
        fmt = _OPENROUTER_FORMAT.get(mime)
        if not fmt:
            raise SttError("failed")  # WebM: the widget re-encodes to WAV first
        hint = {"te-IN": " The speaker is using Telugu.", "hi-IN": " The speaker is using Hindi.",
                "en-IN": " The speaker is using English."}.get(language_code, "")
        payload = {
            "model": settings.assistant_stt_openrouter_model, "temperature": 0, "max_tokens": 400,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": _OPENROUTER_PROMPT + hint},
                {"type": "input_audio", "input_audio": {"data": base64.b64encode(audio).decode(), "format": fmt}},
            ]}],
        }
        try:
            r = await asyncio.wait_for(self._post(payload), settings.assistant_stt_timeout_seconds)
        except (asyncio.TimeoutError, httpx.TimeoutException):
            raise SttError("timeout")
        except httpx.HTTPError:
            raise SttError("failed")
        if r.status_code != 200:
            raise SttError("failed")
        try:
            text = r.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise SttError("failed")
        if not isinstance(text, str):
            raise SttError("failed")
        lang = None if language_code == "unknown" else language_code
        return Heard(clean_transcript(text)[:MAX_TEXT], lang)


def provider():
    return OpenRouterAudio() if provider_name() == "openrouter" else SarvamRest()


class FakeStt:
    """Test double: returns `text` or raises (`error` = an SttError code or any exception)."""

    def __init__(self, text: str = "", language: Optional[str] = "en-IN", error=None):
        self.text, self.language, self.error, self.calls = text, language, error, []

    async def transcribe(self, audio: bytes, mime: str, language_code: str, mode: str) -> Heard:
        self.calls.append((audio, mime, language_code, mode))
        if isinstance(self.error, str):
            raise SttError(self.error)
        if self.error:
            raise self.error
        return Heard(self.text.strip()[:MAX_TEXT], self.language)
