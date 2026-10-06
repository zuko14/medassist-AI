"""Speech provider adapters. Business logic never imports a vendor module
directly: it receives an STTProvider / TTSProvider from get_providers()."""

from typing import AsyncIterator, Optional, Protocol


class STTStream(Protocol):
    """One live recognition stream for one call."""

    async def send(self, pcm: bytes) -> None: ...
    def events(self) -> AsyncIterator[tuple]: ...   # ("speech_start", None) | ("speech_end", None) | ("transcript", (text, lang))
    async def close(self) -> None: ...


class STTProvider(Protocol):
    async def open(self, language: Optional[str], sample_rate: int) -> STTStream: ...


class TTSProvider(Protocol):
    def synthesize(self, text: str, language: str, speaker: Optional[str], pace: float,
                   sample_rate: int) -> AsyncIterator[bytes]: ...   # raw 16-bit LE mono PCM chunks


def get_providers():
    """(stt, tts) for this deployment. The fake pair is refused in production."""
    from app.config import settings
    if (settings.app_env or "").lower() != "production" and not settings.sarvam_api_key:
        from .fake import FakeSTT, FakeTTS
        return FakeSTT(), FakeTTS()
    from .sarvam import SarvamSTT, SarvamTTS
    return SarvamSTT(), SarvamTTS()
