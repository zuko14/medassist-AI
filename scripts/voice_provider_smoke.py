"""Live check of the Sarvam adapters against the REAL API (STATUS gate G-SARVAM-LIVE).

    SARVAM_API_KEY=... python scripts/voice_provider_smoke.py

1. TTS: synthesises a Telugu sentence at 8 kHz linear16 and checks PCM came back.
2. STT: streams that audio back at 8 kHz and prints the transcript.
Exit 0 only when both legs worked and the transcript is non-empty. Costs a few
paise. Never run in CI. If it fails, the wire format in app/voice/providers/sarvam.py
must be corrected against docs.sarvam.ai BEFORE any production call.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import settings  # noqa: E402
from app.voice.audio import PcmChunker  # noqa: E402
from app.voice.providers.sarvam import SarvamSTT, SarvamTTS  # noqa: E402

TEXT = "నమస్కారం, రేపు ఉదయం గుండె డాక్టర్ అపాయింట్మెంట్ కావాలి."


async def main() -> int:
    if not settings.sarvam_api_key:
        print("SARVAM_API_KEY is not set")
        return 2
    pcm = b""
    async for chunk in SarvamTTS().synthesize(TEXT, "te-IN", None, 1.0, 8000):
        pcm += chunk
    print(f"TTS: {len(pcm)} bytes = {len(pcm) / 16000:.1f} s of 8 kHz audio")
    if len(pcm) < 16000:
        print("FAIL: TTS returned under 1 s of audio")
        return 1
    stream = await SarvamSTT().open("te-IN", 8000)
    transcripts = []

    async def reader():
        async for kind, payload in stream.events():
            print("STT event:", kind, payload)
            if kind == "transcript":
                transcripts.append(payload[0])
                return

    task = asyncio.create_task(reader())
    chunker = PcmChunker()
    for c in list(chunker.feed(pcm)) + list(chunker.flush()):
        await stream.send(c)
        await asyncio.sleep(0.2)          # ~real time
    for _ in range(10):                   # trailing silence lets VAD close the utterance
        await stream.send(b"\x00" * 6400)
        await asyncio.sleep(0.2)
    try:
        await asyncio.wait_for(task, 15)
    except asyncio.TimeoutError:
        pass
    await stream.close()
    print("STT transcript:", transcripts)
    return 0 if transcripts and transcripts[0].strip() else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
