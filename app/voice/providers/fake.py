"""Scripted STT / silent TTS for tests and load tests. Never used in production
(providers.get_providers refuses it when APP_ENV=production)."""

import asyncio
from typing import AsyncIterator, Optional


class FakeSTTStream:
    """Each `send()` of audio advances nothing; transcripts are pushed by the test via say()."""

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.received_bytes = 0

    async def send(self, pcm: bytes) -> None:
        self.received_bytes += len(pcm)

    def say(self, text: str, lang: str = "te-IN") -> None:
        self.queue.put_nowait(("speech_start", None))
        self.queue.put_nowait(("transcript", (text, lang)))

    async def events(self) -> AsyncIterator[tuple]:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item

    async def close(self) -> None:
        self.queue.put_nowait(None)


class FakeSTT:
    def __init__(self):
        self.streams = []

    async def open(self, language: Optional[str], sample_rate: int = 8000) -> FakeSTTStream:
        s = FakeSTTStream()
        self.streams.append(s)
        return s


class FakeTTS:
    """20 ms of silence per character (capped at 2 s), in 3200-byte chunks."""

    async def synthesize(self, text: str, language: str, speaker: Optional[str], pace: float,
                         sample_rate: int = 8000) -> AsyncIterator[bytes]:
        total = min(len(text) * 320, sample_rate * 2 * 2)
        for i in range(0, total, 3200):
            await asyncio.sleep(0)
            yield b"\x00" * min(3200, total - i)
