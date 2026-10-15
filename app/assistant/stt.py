"""Kriya Ketu voice input: one Sarvam REST speech-to-text call per utterance (ADR-003).

The browser records <= 30 s with MediaRecorder; the router hands the bytes here and drops them
after the call. Nothing is stored and the transcript is never logged (openrouter-and-speech-spec
B.5). Every failure is an SttError with a fixed code, never the provider's text.
"""

import asyncio
import math
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
# MediaRecorder output only: WebM (EBML magic) from Chrome/Edge/Firefox, MP4 ("ftyp" box) from Safari.
_EXT = {"audio/webm": "webm", "audio/mp4": "mp4"}


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


def looks_like(mime: str, audio: bytes) -> bool:
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
        return Heard(text.strip()[:MAX_TEXT], language if isinstance(language, str) else None)


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
