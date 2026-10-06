"""Sarvam AI streaming STT / TTS over WebSocket.

Protocol as documented at docs.sarvam.ai (checked 2026-10-06):
  STT  wss://api.sarvam.ai/speech-to-text/ws  header Api-Subscription-Key
       query: language-code, model, mode, sample_rate (8000 allowed), input_audio_codec,
              vad_signals, high_vad_sensitivity
       send  {"audio": {"data": <b64>, "encoding": "audio/wav"}}   (8 kHz is set on the URL only)
       recv  {"type": "data", "data": {"transcript", "language_code", ...}}
             {"type": "events", "data": {"signal_type": "START_SPEECH" | "END_SPEECH"}}
             {"type": "error", "data": {"error", "code"}}
  TTS  wss://api.sarvam.ai/text-to-speech/ws?model=bulbul:v3&send_completion_event=true
       send  {"type": "config", "data": {...}} once, then {"type": "text", ...} + {"type": "flush"}
       recv  {"type": "audio", "data": {"audio": <b64>}} ... {"type": "event", "data": {"event_type": "final"}}
STATUS gate G-SARVAM-LIVE: scripts/voice_provider_smoke.py must pass against the real API
before any production call; the shapes above are re-checked there.
"""

import asyncio
import base64
import json
import logging
from typing import AsyncIterator, Optional
from urllib.parse import urlencode

from websockets.asyncio.client import connect

from app.config import settings

from ..audio import strip_wav_header

logger = logging.getLogger(__name__)

_LANGS = {"te-IN", "hi-IN", "en-IN", "ta-IN", "kn-IN", "ml-IN", "mr-IN", "gu-IN", "bn-IN", "pa-IN", "od-IN"}


def _headers() -> dict:
    return {"Api-Subscription-Key": settings.sarvam_api_key}


class _SarvamSTTStream:
    def __init__(self, ws):
        self.ws = ws

    async def send(self, pcm: bytes) -> None:
        await self.ws.send(json.dumps({"audio": {"data": base64.b64encode(pcm).decode("ascii"),
                                                 "encoding": "audio/wav"}}))

    async def events(self) -> AsyncIterator[tuple]:
        async for raw in self.ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            kind, data = msg.get("type"), msg.get("data") or {}
            if kind == "data" and (data.get("transcript") or "").strip():
                yield ("transcript", (data["transcript"].strip(), data.get("language_code")))
            elif kind == "events":
                sig = data.get("signal_type")
                if sig == "START_SPEECH":
                    yield ("speech_start", None)
                elif sig == "END_SPEECH":
                    yield ("speech_end", None)
            elif kind == "error":
                logger.error(f"SARVAM_STT_ERROR code={data.get('code')} {str(data.get('error'))[:200]}")
                yield ("error", data.get("code"))

    async def close(self) -> None:
        try:
            await self.ws.close()
        except Exception:
            pass


class SarvamSTT:
    async def open(self, language: Optional[str], sample_rate: int = 8000) -> _SarvamSTTStream:
        q = {"language-code": language if language in _LANGS else "unknown", "model": settings.sarvam_stt_model,
             "mode": settings.sarvam_stt_mode, "sample_rate": str(sample_rate), "input_audio_codec": "pcm_s16le",
             "vad_signals": "true", "high_vad_sensitivity": "true"}
        ws = await asyncio.wait_for(connect(f"{settings.sarvam_stt_url}?{urlencode(q)}",
                                            additional_headers=_headers(), max_size=2 ** 22), timeout=5)
        return _SarvamSTTStream(ws)


class SarvamTTS:
    async def synthesize(self, text: str, language: str, speaker: Optional[str], pace: float,
                         sample_rate: int = 8000) -> AsyncIterator[bytes]:
        q = urlencode({"model": settings.sarvam_tts_model, "send_completion_event": "true"})
        async with connect(f"{settings.sarvam_tts_url}?{q}", additional_headers=_headers(),
                           max_size=2 ** 22, open_timeout=5) as ws:
            await ws.send(json.dumps({"type": "config", "data": {
                "language_code": language if language in _LANGS else "en-IN",
                "speaker": speaker or settings.sarvam_tts_speaker, "pace": max(0.5, min(float(pace or 1.0), 2.0)),
                "speech_sample_rate": str(sample_rate), "output_audio_codec": "linear16",
                "enable_preprocessing": True, "min_buffer_size": 30, "max_chunk_length": 150}}))
            await ws.send(json.dumps({"type": "text", "data": {"text": text[:2500]}}))
            await ws.send(json.dumps({"type": "flush"}))
            first = True
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                msg = json.loads(raw)
                kind, data = msg.get("type"), msg.get("data") or {}
                if kind == "audio":
                    pcm = base64.b64decode(data.get("audio") or "")
                    if first:
                        pcm, first = strip_wav_header(pcm), False
                    if pcm:
                        yield pcm
                elif kind == "event" and data.get("event_type") == "final":
                    return
                elif kind == "error":
                    raise RuntimeError(f"Sarvam TTS error {data.get('code')}: {str(data.get('message'))[:200]}")
