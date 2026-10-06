"""PCM helpers for the Exotel <-> Sarvam bridge.

Exotel Voicebot (bidirectional) streams 16-bit little-endian mono PCM,
8000 Hz by default, base64 in JSON. Outbound media chunks must be a multiple
of 320 bytes, at least 3200 bytes (100 ms) and at most 100 KB
(developer.exotel.com/docs/agentstream/stream-voicebot-applet).
Sarvam STT accepts pcm_s16le at sample_rate=8000; Sarvam TTS can return
linear16 at speech_sample_rate=8000 — so no resampling or transcoding is needed.
"""

import struct
from typing import Iterator

SAMPLE_RATE = 8000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # 16-bit mono
FRAME_BYTES = 320                   # 20 ms at 8 kHz
MIN_CHUNK_BYTES = 3200              # 100 ms
MAX_CHUNK_BYTES = 32000             # 1 s; well under Exotel's 100 KB ceiling


def strip_wav_header(buf: bytes) -> bytes:
    """Return raw PCM. A RIFF/WAVE container is unwrapped to its 'data' chunk;
    anything else is returned unchanged."""
    if len(buf) < 12 or buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
        return buf
    pos = 12
    while pos + 8 <= len(buf):
        cid = buf[pos:pos + 4]
        size = struct.unpack("<I", buf[pos + 4:pos + 8])[0]
        if cid == b"data":
            return buf[pos + 8:pos + 8 + size] if size and size != 0xFFFFFFFF else buf[pos + 8:]
        pos += 8 + size + (size & 1)
    return b""


class PcmChunker:
    """Re-chunk an arbitrary PCM byte stream into Exotel-legal chunks."""

    def __init__(self, chunk_bytes: int = MIN_CHUNK_BYTES * 2):
        if chunk_bytes % FRAME_BYTES or not MIN_CHUNK_BYTES <= chunk_bytes <= MAX_CHUNK_BYTES:
            raise ValueError("chunk_bytes must be a multiple of 320 within [3200, 32000]")
        self.chunk_bytes = chunk_bytes
        self._buf = bytearray()

    def feed(self, data: bytes) -> Iterator[bytes]:
        self._buf.extend(data)
        while len(self._buf) >= self.chunk_bytes:
            out = bytes(self._buf[:self.chunk_bytes])
            del self._buf[:self.chunk_bytes]
            yield out

    def flush(self) -> Iterator[bytes]:
        """Emit the remainder padded with silence to a legal size."""
        if not self._buf:
            return
        rem = bytes(self._buf)
        self._buf.clear()
        target = max(MIN_CHUNK_BYTES, -(-len(rem) // FRAME_BYTES) * FRAME_BYTES)
        yield rem + b"\x00" * (target - len(rem))


def seconds_of(pcm_bytes: int) -> float:
    return pcm_bytes / BYTES_PER_SECOND
